#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: Not titled yet
# GNU Radio version: 3.10.12.0

import os
import sys

# Do this before importing gnuradio/iio: libiio (and possibly other native
# deps) write some of their own diagnostics (e.g. the "pagesize :error: no
# info..." line seen at the start of every capture, and -- important --
# libiio/SoapySDR's conventional single-character 'O'/'U' overflow/underflow
# markers) directly to this process's real OS-level stdout file descriptor,
# completely bypassing Python's sys.stderr -- so redirecting sys.stdout in
# Python does nothing to stop it. Since RawStdoutSink below also writes our
# actual raw IQ data to that same fd, any such native output lands IN THE
# MIDDLE of the binary stream, corrupting samples (this is what caused the
# periodic NaN bursts lining up with every Pluto buffer refill). Fix:
# duplicate the real, pipe-connected fd 1 aside for our own exclusive use,
# then point fd 1 itself at fd 2 (stderr) so anything writing there
# natively becomes a VISIBLE diagnostic instead of silently vanishing (an
# earlier version of this pointed fd 1 at the null device instead, which
# also suppressed genuine overflow warnings -- exactly the signal you'd
# want when samples are dropping mid-frame).
_real_stdout_fd = os.dup(1)
os.dup2(2, 1)

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import iio
import sip
import threading
import numpy as np


class RawStdoutSink(gr.sync_block):
    """Writes complex64 samples directly to this process's real stdout fd
    (saved above as _real_stdout_fd, before fd 1 got repointed at the null
    device) - mirrors tx.py's RawStdinSource: blocks.file_sink(..., '-', ...)
    doesn't actually work as a stdout convention (GNU Radio just tries to
    open a file literally named '-'), so a plain write() is used instead,
    which is a normal, reliable pipe write on Windows.
    Handles the downstream (stanag_file_rx.py) closing its stdin early -
    e.g. because it already decoded everything it needs - by stopping
    cleanly (WORK_DONE) instead of raising, since that's a normal way for
    a pipeline to end, not a real error."""

    def __init__(self):
        gr.sync_block.__init__(self, name="raw_stdout_sink", in_sig=[np.complex64], out_sig=None)
        self.stdout = os.fdopen(_real_stdout_fd, "wb", buffering=0)

    def work(self, input_items, output_items):
        data = input_items[0]
        try:
            self.stdout.write(data.tobytes())
            self.stdout.flush()
        except (BrokenPipeError, OSError):
            return -1  # WORK_DONE: downstream closed its stdin
        return len(data)



class rx(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "Not titled yet", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("Not titled yet")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "rx")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 192000
        self.RxGain = RxGain = 35

        ##################################################
        # Blocks
        ##################################################

        self._RxGain_range = qtgui.Range(0, 64, 0.001, 15, 200)
        self._RxGain_win = qtgui.RangeWidget(self._RxGain_range, self.set_RxGain, "'RxGain'", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._RxGain_win)
        # 24/125 (not 12/125): the Pluto source below runs at 1,000,000 sps,
        # not 2,000,000 like the HackRF TX side - 1,000,000 * 24/125 = 192,000
        # exactly, matching stanag_file_rx.py's assumed rate. The old 12/125
        # ratio only makes 192kHz from a 2Msps input, so it was silently
        # producing 96kHz instead - every timing/frequency assumption in the
        # decoder was off by 2x as a result.
        self.rational_resampler_xxx_0_0 = filter.rational_resampler_ccc(
                interpolation=24,
                decimation=125,
                taps=[],
                fractional_bw=0)
        self.qtgui_freq_sink_x_0 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            "", #name
            1,
            None # parent
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



        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_0_win = sip.wrapinstance(self.qtgui_freq_sink_x_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_freq_sink_x_0_win)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32('' if '' else iio.get_pluto_uri(), [True, True], 32768)
        self.iio_pluto_source_0.set_len_tag_key('packet_len')
        self.iio_pluto_source_0.set_frequency(145500820)  # 1kHz offset from tx.py's 145500000 is intentional/known
        self.iio_pluto_source_0.set_samplerate(1000000)
        self.iio_pluto_source_0.set_gain_mode(0, 'manual')
        self.iio_pluto_source_0.set_gain(0, RxGain)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.blocks_file_sink_0 = RawStdoutSink()
        # With no gain ceiling, an exact/near-zero sample (e.g. right at
        # startup before the Pluto source is actually locked and streaming)
        # makes agc_cc compute gain = reference/~0 -> inf. Because the gain
        # is recursive internal state, one inf/nan sample poisons every
        # sample after it for the rest of the run -- this is why decoding
        # saw NaN from the very first chunk onward. This build's constructor
        # doesn't take max_gain as an argument, but the block still exposes
        # set_max_gain() to cap it after construction without changing
        # anything else about how this block behaves.
        self.analog_agc_xx_0 = analog.agc_cc((1e-2), 0.2, 1)
        try:
            self.analog_agc_xx_0.set_max_gain(65536)
        except AttributeError:
            print("WARNING: this GNU Radio build's agc_cc has no set_max_gain() -- "
                  "gain is still unbounded, NaN latch-up is still possible.",
                  file=sys.stderr)


        ##################################################
        # Connections
        ##################################################
        self.connect((self.analog_agc_xx_0, 0), (self.blocks_file_sink_0, 0))
        self.connect((self.analog_agc_xx_0, 0), (self.qtgui_freq_sink_x_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.rational_resampler_xxx_0_0, 0))
        self.connect((self.rational_resampler_xxx_0_0, 0), (self.analog_agc_xx_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "rx")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.qtgui_freq_sink_x_0.set_frequency_range(0, self.samp_rate)

    def get_RxGain(self):
        return self.RxGain

    def set_RxGain(self, RxGain):
        self.RxGain = RxGain
        self.iio_pluto_source_0.set_gain(0, self.RxGain)




def main(top_block_cls=rx, options=None):

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

if __name__ == '__main__':
    main()
