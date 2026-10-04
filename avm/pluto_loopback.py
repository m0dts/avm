#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Single-Pluto TX+RX loopback test: both chains are the SAME physical
# device sharing one on-board oscillator, unlike the HackRF(TX)+Pluto(RX)
# setup where two independent, free-running oscillators were confirmed (via
# a dedicated CW-tone capture) to contribute real phase noise. If that
# phase noise drops sharply here, it confirms unsynchronized TX/RX clocks
# as the dominant cause; if it doesn't, the noise is coming from somewhere
# else (e.g. the channel/cabling itself, or AD9363 phase noise that isn't
# actually clock-sync-dependent).
#
# Requires a cable from the Pluto's TX SMA to its RX SMA WITH AN INLINE
# ATTENUATOR (20-30dB) - feeding TX straight into RX at full power will
# overload/saturate the receiver front end.
#
# Usage (same stdin/stdout streaming convention as tx.py/rx.py):
#   python stanag_file_tx.py | python pluto_loopback.py | python stanag_file_rx.py > out.bin

import os
import sys
import signal
import threading
import numpy as np

# Do this before importing gnuradio/iio: libiio (and possibly other native
# deps) write some of their own diagnostics (e.g. the "pagesize :error: no
# info..." line, and libiio/SoapySDR's 'O'/'U' overflow/underflow markers)
# directly to this process's real OS-level stdout file descriptor,
# completely bypassing Python's sys.stderr -- so redirecting sys.stdout in
# Python does nothing to stop it. Since RawStdoutSink below also writes our
# actual raw IQ data to that same fd, any such native output lands IN THE
# MIDDLE of the binary stream, corrupting samples -- the same bug already
# fixed in rx.py, just never carried over to this flowgraph (confirmed for
# real: a capture made before this fix showed sample magnitudes up near
# float32's max, consistent with stray bytes landing mid-stream and being
# reinterpreted as garbage floats). Fix: duplicate the real, pipe-connected
# fd 1 aside for our own exclusive use, then point fd 1 itself at fd 2
# (stderr) so anything writing there natively becomes a visible diagnostic
# instead of silently corrupting the stream.
_real_stdout_fd = os.dup(1)
os.dup2(2, 1)

# This process has to keep draining the Pluto's hardware RX buffer in
# real time no matter what else is running on the machine. NOTE: this was
# originally added chasing a specific downstream symptom (an apparent
# input-byte shortfall reported by hf_ofdm_rx.py) that turned out to be a
# false positive -- pluto_loopback's own console showed zero libiio/
# SoapySDR 'O'/'U' overflow markers (the real, authoritative signal for
# actual hardware sample drops) anywhere near the same time, so that
# specific diagnosis wasn't confirmed. Left in anyway as cheap, harmless
# insurance: this process sharing a machine with a CPU-heavy Python/numpy
# decoder in a separate process is a real scenario where scheduling
# delays COULD cause genuine drops, even though we don't have confirmed
# evidence it's happening here. ABOVE_NORMAL rather than HIGH/REALTIME:
# enough to win contention against an ordinary background process
# without being able to starve the rest of the system outright.
try:
    import ctypes
    ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
    if not ctypes.windll.kernel32.SetPriorityClass(
            ctypes.windll.kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS):
        print("WARNING: could not raise process priority (SetPriorityClass failed).", file=sys.stderr)
except AttributeError:
    pass  # not on Windows -- ctypes.windll doesn't exist; not worth a warning
except Exception as exc:
    print(f"WARNING: could not raise process priority: {exc}", file=sys.stderr)

from PyQt5 import Qt
import sip

from gnuradio import gr
from gnuradio import qtgui
from gnuradio.fft import window
from gnuradio import filter
from gnuradio import analog
from gnuradio import blocks
from gnuradio import iio

RF_FREQUENCY = 145500000
# rx.py (Pluto RX paired with a separate HackRF TX) intentionally tunes
# its RX away from TX for exactly this reason: our carrier plan
# (K=-99..311) sits close to DC, right where a zero-IF receiver's own
# RX-side DC-correction (set_rfdc/set_bbdc below) notches out real signal
# energy if TX and RX share the same center frequency. This flowgraph
# tuned both chains to the identical RF_FREQUENCY, missing that offset
# entirely. 25kHz is far too wide for resolve_integer_cfo's own +-30-
# carrier (~1406Hz) search range to absorb as if it were ordinary CFO, so
# a digital rotator (see freq_correct below) shifts the RX baseband back
# by this exact, known amount before the modem ever sees it -- done at
# PLUTO_SAMPLE_RATE, comfortably inside its Nyquist, and well before the
# rx_resampler's tighter (Â±96kHz) passband would otherwise clip it.
RX_FREQ_OFFSET_HZ = 25000
PLUTO_SAMPLE_RATE = 1000000
MODEM_SAMPLE_RATE = 192000
TX_ATTENUATION_DB = 18  # 0 = max TX power; raise this if RX front end saturates
RX_GAIN_DB = 5


class RawStdinSource(gr.sync_block):
    """Same as tx.py's RawStdinSource - see that file's docstring."""

    def __init__(self):
        gr.sync_block.__init__(self, name="raw_stdin_source", in_sig=None, out_sig=[np.complex64])
        self.stdin = sys.stdin.buffer
        self.itemsize = 8
        self.leftover = b""

    def work(self, input_items, output_items):
        out = output_items[0]
        max_items = len(out)
        needed = max_items * self.itemsize - len(self.leftover)
        chunk = self.stdin.read(needed) if needed > 0 else b""
        if not chunk and not self.leftover:
            return -1
        data = self.leftover + chunk
        n = len(data) // self.itemsize
        usable = n * self.itemsize
        self.leftover = data[usable:]
        if n == 0:
            return 0
        out[:n] = np.frombuffer(data[:usable], dtype=np.complex64)
        return n


class RawStdoutSink(gr.sync_block):
    """Writes complex64 samples directly to this process's real stdout fd
    (saved above as _real_stdout_fd, before fd 1 got repointed at stderr)
    -- see that redirect's comment for why sys.stdout.buffer isn't safe
    to use here."""

    def __init__(self):
        gr.sync_block.__init__(self, name="raw_stdout_sink", in_sig=[np.complex64], out_sig=None)
        self.stdout = os.fdopen(_real_stdout_fd, "wb", buffering=0)

    def work(self, input_items, output_items):
        data = input_items[0]
        try:
            self.stdout.write(data.tobytes())
            self.stdout.flush()
        except (BrokenPipeError, OSError):
            return -1
        return len(data)


class pluto_loopback(gr.top_block, Qt.QWidget):
    def __init__(self):
        gr.top_block.__init__(self, "pluto_loopback", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Pluto TX+RX loopback")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "pluto_loopback")
        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        self.flowgraph_started = threading.Event()

        uri = iio.get_pluto_uri()

        self.stdin_source = RawStdinSource()
        # 192000 * 125/24 = 1,000,000 exactly (modem rate -> Pluto TX rate).
        self.tx_resampler = filter.rational_resampler_ccc(
            interpolation=125, decimation=24, taps=[], fractional_bw=0)
        # 1,000,000 * 24/125 = 192,000 exactly (Pluto RX rate -> modem rate).
        self.rx_resampler = filter.rational_resampler_ccc(
            interpolation=24, decimation=125, taps=[], fractional_bw=0)
        # Undo the deliberate RX_FREQ_OFFSET_HZ retune (see its comment
        # above) digitally, at the Pluto's native rate, before the
        # resampler's own passband could otherwise clip a signal sitting
        # 25kHz off-center. y[n] = x[n] * exp(j*2*pi*offset*n/fs) shifts
        # the received spectrum back up by exactly the amount the RX LO
        # was tuned away from TX, landing our signal back at 0Hz same as
        # if TX and RX shared one frequency.
        self.freq_correct = blocks.rotator_cc(2 * np.pi * RX_FREQ_OFFSET_HZ / PLUTO_SAMPLE_RATE)
        # With no gain ceiling, an exact/near-zero sample (e.g. right at
        # startup before the Pluto source is actually locked and
        # streaming) makes agc_cc compute gain = reference/~0 -> inf.
        # Because the gain is recursive internal state, one inf/nan
        # sample poisons every sample after it for the rest of the run --
        # the same bug already fixed in rx.py, just never carried over to
        # this flowgraph's own AGC instance.
        self.agc = analog.agc_cc((5e-2), 0.2, 1)
        try:
            # This block's reference level is 1e-2, tiny next to a Pluto's
            # ~1.0 full-scale ADC input -- ordinary signal levels need
            # only a small gain to reach it, so a cap of 65536 left far
            # more headroom than legitimate and still let a real capture
            # run away to float32's max (~3.4e38), confirmed directly in
            # a saved capture. A much tighter cap still comfortably covers
            # any real signal level here while actually bounding a
            # near-zero-sample runaway.
            self.agc.set_max_gain(1000)
        except AttributeError:
            print("WARNING: this GNU Radio build's agc_cc has no set_max_gain() -- "
                  "gain is still unbounded, NaN latch-up is still possible.",
                  file=sys.stderr)
        self.stdout_sink = RawStdoutSink()

        # Spectrum display on the RX side, post-AGC - same style/settings as
        # rx.py's qtgui_freq_sink_x_0, so the plot reads the same way here
        # as it did there.
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            1024,  # size
            window.WIN_BLACKMAN_hARRIS,  # wintype
            0,  # fc
            MODEM_SAMPLE_RATE,  # bw
            "",  # name
            1,
            None  # parent
        )
        self.qtgui_freq_sink_x_0.set_update_time(0.10)
        self.qtgui_freq_sink_x_0.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_0.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_0.enable_autoscale(False)
        self.qtgui_freq_sink_x_0.enable_grid(False)
        self.qtgui_freq_sink_x_0.set_fft_average(0.2)
        self.qtgui_freq_sink_x_0.enable_axis_labels(True)
        self.qtgui_freq_sink_x_0.enable_control_panel(False)
        self.qtgui_freq_sink_x_0.set_fft_window_normalized(True)
        self.qtgui_freq_sink_x_0.set_line_label(0, "Data 0")
        self.qtgui_freq_sink_x_0.set_line_width(0, 1)
        self.qtgui_freq_sink_x_0.set_line_color(0, "blue")
        self.qtgui_freq_sink_x_0.set_line_alpha(0, 1.0)
        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_freq_sink_x_0_win)

        # One IIO context, two independent chains (TX and RX), same clock.
        self.pluto_sink = iio.fmcomms2_sink_fc32(uri, [True, True], 32768, False)
        self.pluto_sink.set_bandwidth(20000000)
        self.pluto_sink.set_frequency(RF_FREQUENCY)
        self.pluto_sink.set_samplerate(PLUTO_SAMPLE_RATE)
        self.pluto_sink.set_attenuation(0, TX_ATTENUATION_DB)
        self.pluto_sink.set_filter_params('Auto', '', 0, 0)

        # Live TX attenuation / RX gain controls -- these were fixed
        # constants requiring a full restart to change, which made
        # dialing in a working level (avoiding front-end saturation on
        # too little attenuation, or too little RX gain to see anything)
        # a slow, blind, restart-every-guess process. Plain PyQt5 spin
        # boxes instead of gnuradio's qtgui.Range/RangeWidget -- this
        # build's RangeWidget tries to publish its change over a message
        # port that doesn't exist on a standalone widget outside a proper
        # hier_block2 message-passing setup (confirmed for real: dragging
        # the slider raised "RangeWidget object has no attribute
        # message_port_pub"). A spin box's plain Qt valueChanged signal
        # sidesteps that GNU Radio message-passing machinery entirely.
        self.tx_attenuation_db = TX_ATTENUATION_DB
        tx_atten_row = Qt.QHBoxLayout()
        tx_atten_row.addWidget(Qt.QLabel("TX attenuation (dB, 0=max power)"))
        self._tx_attenuation_spin = Qt.QDoubleSpinBox()
        self._tx_attenuation_spin.setRange(0, 89.75)
        self._tx_attenuation_spin.setSingleStep(0.25)
        self._tx_attenuation_spin.setValue(TX_ATTENUATION_DB)
        self._tx_attenuation_spin.valueChanged.connect(self.set_tx_attenuation_db)
        tx_atten_row.addWidget(self._tx_attenuation_spin)
        self.top_layout.addLayout(tx_atten_row)

        self.rx_gain_db = RX_GAIN_DB
        rx_gain_row = Qt.QHBoxLayout()
        rx_gain_row.addWidget(Qt.QLabel("RX gain (dB, manual)"))
        self._rx_gain_spin = Qt.QDoubleSpinBox()
        self._rx_gain_spin.setRange(0, 70)
        self._rx_gain_spin.setSingleStep(0.5)
        self._rx_gain_spin.setValue(RX_GAIN_DB)
        self._rx_gain_spin.valueChanged.connect(self.set_rx_gain_db)
        rx_gain_row.addWidget(self._rx_gain_spin)
        self.top_layout.addLayout(rx_gain_row)

        self.pluto_source = iio.fmcomms2_source_fc32(uri, [True, True], 32768)
        self.pluto_source.set_len_tag_key('packet_len')
        self.pluto_source.set_frequency(RF_FREQUENCY + RX_FREQ_OFFSET_HZ)
        self.pluto_source.set_samplerate(PLUTO_SAMPLE_RATE)
        self.pluto_source.set_gain_mode(0, 'manual')
        self.pluto_source.set_gain(0, self.rx_gain_db)
        self.pluto_source.set_quadrature(True)
        self.pluto_source.set_rfdc(True)
        self.pluto_source.set_bbdc(True)
        self.pluto_source.set_filter_params('Auto', '', 0, 0)

        self.connect((self.stdin_source, 0), (self.tx_resampler, 0))
        self.connect((self.tx_resampler, 0), (self.pluto_sink, 0))
        self.connect((self.pluto_source, 0), (self.freq_correct, 0))
        self.connect((self.freq_correct, 0), (self.rx_resampler, 0))
        self.connect((self.rx_resampler, 0), (self.agc, 0))
        self.connect((self.agc, 0), (self.stdout_sink, 0))
        self.connect((self.agc, 0), (self.qtgui_freq_sink_x_0, 0))

    def set_tx_attenuation_db(self, tx_attenuation_db):
        self.tx_attenuation_db = tx_attenuation_db
        self.pluto_sink.set_attenuation(0, self.tx_attenuation_db)

    def set_rx_gain_db(self, rx_gain_db):
        self.rx_gain_db = rx_gain_db
        self.pluto_source.set_gain(0, self.rx_gain_db)

    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "pluto_loopback")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()
        event.accept()


def main(top_block_cls=pluto_loopback, options=None):
    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()
        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()


if __name__ == "__main__":
    main()
