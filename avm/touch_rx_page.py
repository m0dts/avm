"""
RX view of the touch GUI (touch_gui.py). The receive chain is the full
media_rx_gui.MediaRxWindow, created hidden and used as an engine; its own
video pane and spectrum plot are moved into this page, so decoding, playback
and display are exactly those of the full GUI. Input is always the PlutoSDR.
"""
import collections
import math

import numpy as np
import re
import time

from PyQt5 import QtCore, QtWidgets

import hf_ofdm_common as ofdm
import media_rx_gui
import touch_widgets as tw
from touch_tx_page import s_get

# Spectrum span as a multiple of the selected bandwidth: the signal sits in
# the middle with noise floor visible either side.
SPECTRUM_SPAN_X = 1.5
# FFT frames averaged (power) in the spectrum: at 4 updates/s, ~4 s of history.
SPECTRUM_AVERAGES = 16
# Audio buffer: the mode's fragment period plus this margin (at least the minimum).
# Was +0.08 s / 0.2 s minimum: on the fast settings (fragments every 0.1-0.2
# s) that left ~0.1 s for decode-time jitter, a failed fragment or a busy Pi,
# and the logs showed 7-83 audio breaks (crackles) per run. +0.25 s costs a
# quarter second of delay and covers a missed fragment's worth.
AUDIO_LATENCY_MARGIN_S = 0.25
AUDIO_LATENCY_MIN_S = 0.5
# MER shown on the status line: mean of the last this-many fragments (dB)
MER_AVERAGE = 8
# ... shown as 0 once no fragment has given an EVM for this long
MER_STALE_S = 2.0
# good/bad fragment counts on the status line cover the last this-many seconds
STATUS_WINDOW_S = 5
# spectrum Auto: re-fit every this-many seconds; manual mode uses this dB/div
AUTOSCALE_S = 5
MANUAL_DB_PER_DIV = 2.5
AUTOSCALE_MAX_DB_PER_DIV = 5.0  # cap: a very low floor drops off the bottom instead


def _station_text(engine_text):
    """The engine's station-ID label ("'ID'" confirmed, "'ID' (unconfirmed)"
    while its CRC doesn't check yet) as shown here: just the ID, *ID* while
    unconfirmed. Unconfirmed bytes can be anything -- non-printable ones
    became boxes -- so everything outside printable ASCII is blanked, and
    the padding spaces are trimmed so it stays centred."""
    m = re.match(r"^'(.*)'( \(unconfirmed\))?$", engine_text, re.S)
    if not m:
        return ""  # "(none received yet)"
    sid = "".join(c if 32 <= ord(c) <= 126 else " " for c in m.group(1)).strip()
    if not sid:
        return ""
    return f"*{sid}*" if m.group(2) else sid


class _RxEngine(media_rx_gui.MediaRxWindow):
    """The full RX window, never shown; adds a wider spectrum span and the
    radio choice (PlutoSDR, LimeSDR or RTL-SDR)."""
    span_hz = 0.0
    sdr = "pluto"
    rtl_ppm = 0.0
    lime_port = "Auto"

    def _build_rx_cmd(self):
        cmd = super()._build_rx_cmd()
        # the player buffers audio itself; pacing the output only delayed video ~0.4 s
        cmd += ["--no-output-pacing"]
        if self.span_hz and "--spectrum-stderr" in cmd:
            cmd += ["--spectrum-span-hz", f"{self.span_hz:.0f}"]
        if self.sdr in ("lime", "rtlsdr"):
            cmd += ["--sdr", self.sdr, "--gain-file", tw.gain_file_path("rx")]
        if self.sdr == "rtlsdr" and self.rtl_ppm:
            cmd += ["--sdr-ppm", f"{self.rtl_ppm:g}"]
        if self.sdr == "lime" and self.lime_port != "Auto":
            cmd += ["--sdr-antenna", self.lime_port]
        return cmd

    def _flush_log_buffer(self):
        # its log widget is never shown (~30 lines/s in the fast modes):
        # just the session log file, if HF_RX_GUI_LOG names one
        if self._log_pending_lines:
            self._write_log_file(self._log_pending_lines)
            self._log_pending_lines = []


class RxPage(QtWidgets.QWidget):
    running_changed = QtCore.pyqtSignal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.engine = _RxEngine()
        self.engine.hide()
        self.engine.log_stream.line_received.connect(self._on_log)
        self.live_gain = tw.LiveGain("rx")
        self._ok = self._lost = self._bad = 0
        self._mer = []
        self._cfo = []
        self._events = collections.deque()  # (time, decoded ok) per fragment

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 8)
        root.setSpacing(10)

        # left: the engine's own video pane and spectrum plot
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(4)
        video = self.engine.video_label
        video.setParent(None)
        video.setMinimumSize(160, 90)
        # the transmitter's callsign/message (station ID), above its picture;
        # fixed height so the video doesn't jump when it appears
        self.station = tw.ElideLabel("")
        self.station.setObjectName("big")
        self.station.setAlignment(QtCore.Qt.AlignCenter)
        self.station.setFixedHeight(round(28 * tw.SCALE))
        left.addWidget(self.station)
        left.addWidget(video, 3)
        # tap the video: full screen; tap again: back (see _toggle_fullscreen)
        self._video, self._left, self._overlay = video, left, None
        video.installEventFilter(self)
        self.status = tw.ElideLabel("Idle")
        self.status.setObjectName("value")
        left.addWidget(self.status)
        spec = self.engine.spectrum_plot
        spec.setParent(None)
        spec.setMinimumHeight(80)
        # compact axes for a small plot: no axis titles, small tick labels
        tick_font = self.font()
        tick_font.setPixelSize(max(9, round(11 * tw.SCALE)))
        for side in ("left", "bottom"):
            spec.setLabel(side, "")
            spec.getAxis(side).setStyle(tickFont=tick_font)
        left.addWidget(spec, 2)
        root.addLayout(left, 3)

        # right: settings, station, start/stop (bottom right, same place as TX's)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(5)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        self.freq = tw.FreqButton("RX frequency", s_get(settings, "rx_freq", 145500000))
        self.mode = tw.Segmented([(m, m) for m in tw.MODES], tw.saved_mode(s_get(settings, "rx_mode", "A")))
        self.bw = tw.Segmented([(b, b) for b in tw.BANDWIDTHS_KHZ], tw.saved_bandwidth(s_get(settings, "rx_bw", "80")))
        self.mod = tw.Segmented([("qpsk", "QPSK"), ("16qam", "16QAM")], s_get(settings, "rx_mod", "qpsk"))
        self.gain = tw.Stepper(0, 73, 1, s_get(settings, "rx_gain", 40), " dB")
        self.audio_out = tw.Picker("Audio output", self._audio_outputs,
                                   s_get(settings, "rx_audio", self.engine.audio_device.currentText()))
        # spectrum reference level (top of the plot), live
        self.ref = tw.Stepper(-100, 10, 5, s_get(settings, "rx_ref", -25), " dB")
        # Auto: re-fit ref level and dB/div to the spectrum every AUTOSCALE_S
        self.ref_auto = QtWidgets.QPushButton("Auto")
        self.ref_auto.setCheckable(True)
        self.ref_auto.setFocusPolicy(QtCore.Qt.NoFocus)
        self.ref_auto.setChecked(bool(settings.get("rx_ref_auto", True)))
        ref_row = QtWidgets.QWidget()
        ref_row.setObjectName("seg")
        rl = QtWidgets.QHBoxLayout(ref_row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        rl.addWidget(self.ref_auto, 2)
        rl.addWidget(self.ref, 5)
        self.radio = tw.RadioPicker("RX radio (connected now)", settings.get("rx_sdr", settings.get("sdr", "pluto")),
                                     rx=True)
        # RTL-SDR crystal correction (no TCXO); only shown with an RTL-SDR
        self.ppm = tw.Stepper(-200, 200, 1, int(s_get(settings, "rx_rtl_ppm", 60)), " ppm")
        self.engine.rtl_ppm = float(self.ppm.value())
        self.radio.radio_changed.connect(self.set_radio)
        # LimeSDR RX port (LNAL/LNAW/LNAH or Auto): only shown with the LimeSDR
        self.lime_port = tw.lime_port_picker("rx", s_get(settings, "rx_lime_port", "Auto"))
        self.engine.lime_port = self.lime_port.value()
        self.lime_port.changed.connect(self._lime_port_changed)
        # the radio on its own row (the RX column is too narrow to share Freq's),
        # with the radio's own extra: the LimeSDR port or the RTL-SDR's PPM
        rows = [("Freq", self.freq),
                ("Radio", tw.freq_radio_row(self.radio, self.lime_port, self.ppm)),
                ("Mode", self.mode), ("kHz", self.bw), ("Modul.", self.mod),
                ("RX gain", self.gain), ("Ref level", ref_row), ("Audio", self.audio_out)]
        for r, (label, w) in enumerate(rows):
            grid.addWidget(tw.row_label(label), r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(4)
        right.addLayout(grid)
        right.addStretch(1)
        self.run = tw.RunButton("RX")
        self.run.clicked.connect(self._run_clicked)
        right.addWidget(self.run)
        root.addLayout(right, 2)

        for w in (self.mode, self.bw, self.mod, self.freq, self.audio_out):
            w.changed.connect(self._changed)
        self.gain.changed.connect(self._gain_changed)
        self.ppm.changed.connect(self._ppm_changed)
        self.ref.changed.connect(self._ref_changed)
        self.ref_auto.toggled.connect(self._ref_auto_toggled)
        self._autoscale_timer = QtCore.QTimer(self)
        self._autoscale_timer.timeout.connect(self._autoscale)
        self._autoscale_timer.start(AUTOSCALE_S * 1000)

        self.set_radio(self.radio.sdr, force=True)
        self._apply_to_engine()
        self._set_scale(self.ref.value(), MANUAL_DB_PER_DIV)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(500)

    def eventFilter(self, obj, event):
        # a touch arrives as a synthesized mouse release
        if obj is self._video and event.type() == QtCore.QEvent.MouseButtonRelease:
            self._toggle_fullscreen()
            return True
        return super().eventFilter(obj, event)

    def _toggle_fullscreen(self):
        """Moves the video into a black overlay over the whole window (top
        bar included) and back. The decoder scales frames to the label's
        size, so full screen is decoded at full resolution, not stretched."""
        if self._overlay is None:
            win = self.window()
            self._overlay = QtWidgets.QWidget(win)
            self._overlay.setStyleSheet("background: #000;")
            lay = QtWidgets.QVBoxLayout(self._overlay)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.addWidget(self._video)
            self._overlay.setGeometry(win.rect())
            self._overlay.show()
            self._overlay.raise_()
        else:
            self._left.insertWidget(0, self._video, 3)
            self._overlay.hide()  # now: deleteLater only acts on a later event-loop pass
            self._overlay.deleteLater()
            self._overlay = None

    def _audio_outputs(self):
        self.engine._refresh_audio_devices()
        c = self.engine.audio_device
        return [c.itemText(i) for i in range(c.count())]

    # ------------------------------------------------------------------
    def _apply_to_engine(self):
        tw.enforce_mode_bandwidth(self.mode, self.bw)
        e = self.engine
        e.input_mode.setCurrentText("pluto")
        e.rf_freq.setText(str(self.freq.hz()))
        e.mode.setCurrentText(self.mode.value())
        e.occupancy.setCurrentText(self.bw.value())
        e.lo_offset_hz.setValue(tw.lo_offset_hz(self.bw.value()))
        e.pluto_sample_rate.setCurrentText(str(tw.RTL_SAMPLE_RATE if e.sdr == "rtlsdr" else tw.sdr_rate(self.bw.value())))
        e.fragment_size.setValue(tw.fragment_size(self.bw.value()))  # before the audio latency below
        e.span_hz = SPECTRUM_SPAN_X * float(self.bw.value()) * 1000
        e.spectrum_averages.setValue(SPECTRUM_AVERAGES)
        # Audio arrives one fragment at a time, so the buffer must outlast
        # the gap between fragments: 0.2 s ran dry continuously in mode C
        # (a fragment every 360 ms) -- heard as no audio.
        try:
            cfg = ofdm.build_config(self.mode.value(), float(self.bw.value()),
                                    data_modulation=self.mod.value(), fec_scheme="ldpc")
            period = int(e.fragment_size.value()) * 8 / ofdm.estimate_effective_bitrate(
                cfg, int(e.fragment_size.value()), fragment_gap_ms=0.0)
            e.audio_latency.setValue(max(AUDIO_LATENCY_MIN_S, period + AUDIO_LATENCY_MARGIN_S))
        except Exception:
            pass
        e.modulation.setCurrentText(self.mod.value())
        e.rx_gain.setValue(self.gain.value())
        idx = e.audio_device.findText(self.audio_out.value())
        if idx >= 0:
            e.audio_device.setCurrentIndex(idx)
        self._save()

    def _save(self):
        self.s.update(rx_freq=self.freq.hz(),
                      rx_mode=self.mode.value(), rx_bw=self.bw.value(), rx_mod=self.mod.value(),
                      rx_gain=self.gain.value(), rx_audio=self.audio_out.value(),
                      rx_ref=self.ref.value() if hasattr(self, "ref") else -25,
                      rx_ref_auto=self.ref_auto.isChecked() if hasattr(self, "ref_auto") else True,
                      rx_sdr=self.engine.sdr,
                      rx_rtl_ppm=self.ppm.value() if hasattr(self, "ppm") else 60,
                      rx_lime_port=self.lime_port.value() if hasattr(self, "lime_port") else "Auto")

    def _lime_port_changed(self, port):
        """Applied when RX (re)starts."""
        self.engine.lime_port = port
        self._save()
        if self.is_running() and self.engine.sdr == "lime":
            self.run.set_state("pending")

    def _ppm_changed(self, *_):
        """Applied when RX (re)starts: mark a running RTL RX pending."""
        self.engine.rtl_ppm = float(self.ppm.value())
        self._save()
        if self.is_running() and self.engine.sdr == "rtlsdr":
            self.run.set_state("pending")

    def _changed(self, *_):
        self._apply_to_engine()
        if self.is_running():
            self.run.set_state("pending")

    def _ref_changed(self, db):
        # a manual -/+ tap takes over from Auto
        if self.ref_auto.isChecked():
            self.ref_auto.setChecked(False)
        self.engine.spectrum_ref_spin.setValue(db)  # the engine redraws the plot's range
        self._save()

    def _ref_auto_toggled(self, on):
        if on:
            self._autoscale()
        else:
            self._set_scale(self.ref.value(), MANUAL_DB_PER_DIV)
        self._save()

    def _set_scale(self, ref_db, db_per_div):
        e = self.engine
        e.spectrum_db_per_div.setValue(db_per_div)
        e.spectrum_ref_spin.setValue(ref_db)
        e._apply_spectrum_ref_level(ref_db)  # also when only the dB/div changed
        e.spectrum_plot.setLabel("left", "")  # the engine re-adds its axis title; no room here
        # grid line every division, label every second one -- left to itself
        # pyqtgraph gridded every 10 dB, which looked like 10 dB/div
        e.spectrum_plot.getAxis("left").setTickSpacing(major=2 * db_per_div, minor=db_per_div)

    def _autoscale(self):
        """Ref level just above the spectrum's peak (5 dB steps), dB/div the
        smallest multiple of 2.5 whose 10 divisions also reach the noise floor."""
        if not self.ref_auto.isChecked():
            return
        _, y = self.engine.spectrum_curve.getData()
        if y is None or len(y) < 16:
            return
        y = np.asarray(y, float)
        y = y[np.isfinite(y)]
        if len(y) < 16:
            return
        peak, floor = np.percentile(y, 99), np.percentile(y, 10)
        ref = math.ceil((peak + 2) / 5) * 5
        div = min(AUTOSCALE_MAX_DB_PER_DIV, max(2.5, math.ceil((ref - (floor - 3)) / 10 / 2.5) * 2.5))
        self.ref.set_value(ref)          # display only (no emit: stays on Auto)
        self._set_scale(self.ref.value(), div)

    def set_radio(self, sdr, force=False):
        """'pluto', 'lime' or 'rtlsdr' -- from the Radio picker. RX gain tops
        out at 61 dB on a LimeSDR, ~49 on an RTL-SDR, 73 on the Pluto."""
        self.radio.set_sdr(sdr)
        self.lime_port.setVisible(sdr == "lime")
        self.ppm.setVisible(sdr == "rtlsdr")
        self.gain.hi = {"lime": 61, "rtlsdr": 49}.get(sdr, 73)
        self.gain.set_value(self.gain.value(), emit=True)
        if force or sdr != self.engine.sdr:
            self.engine.sdr = sdr
            if not force:
                self._apply_to_engine()  # the RTL-SDR's own sample rate
            self._save()
            if self.is_running():
                self.run.set_state("pending")

    def _gain_changed(self, db):
        self.engine.rx_gain.setValue(db)
        self._save()
        if self.engine.sdr in ("lime", "rtlsdr"):
            tw.write_gain_file("rx", db)  # the running RX process picks it up
            return
        if self.is_running():
            if tw.LiveGain.available():
                self.live_gain.set(self.engine.pluto_uri.text().strip() or "ip:192.168.2.1", db)
            else:
                self.run.set_state("pending")

    # ------------------------------------------------------------------
    def is_running(self):
        return bool(self.engine.procs)

    def _run_clicked(self):
        if self.run.state == "stopped":
            self.start()
        elif self.run.state == "pending":
            self.stop()
            self.start()
        else:
            self.stop()

    def _lime_busy(self):
        """True (and says so) if TX holds the LimeSDR this RX wants."""
        peer = getattr(self, "peer", None)
        if (self.engine.sdr == "lime" and peer is not None and peer.is_running()
                and peer.engine.sdr == "lime"):
            self.status.setText("LimeSDR busy: TX is using it (one Lime can't do both)")
            return True
        return False

    def start(self):
        if self._lime_busy():
            return
        self._apply_to_engine()
        self._ok = self._lost = self._bad = 0
        self._mer = []
        self._cfo = []
        self._events.clear()
        tw.write_gain_file("rx", self.gain.value())  # so a stale value can't apply at start
        self.engine.start()
        if self.is_running():
            self.run.set_state("running")
            self.status.setText("Searching...")
            self.running_changed.emit(True)

    def stop(self):
        self.engine.stop()
        self.run.set_state("stopped")
        self.status.setText(f"Stopped  ·  {self._ok} OK, {self._lost} lost, {self._bad} bad"
                            f"  ·  {self._mer_text()}  ·  {self._cfo_text()}")
        self.running_changed.emit(False)

    def _on_log(self, line):
        # MER from each fragment's EVM (RMS error / ideal): MER = -20 log10(EVM)
        m = re.search(r"Fragment seq=\d+: .*?EVM=([\d.]+)%", line)
        if m and float(m.group(1)) > 0:
            if self._mer and time.monotonic() - self._mer_t > MER_STALE_S:
                self._mer = []  # back after a gap: don't average in pre-gap values
            self._mer.append(-20 * math.log10(float(m.group(1)) / 100))
            del self._mer[:-MER_AVERAGE]
            self._mer_t = time.monotonic()
        now = time.monotonic()
        if re.search(r"Fragment seq=\d+: OK", line):
            # CFO only from good fragments: a failed one's can be a wrong lock
            c = re.search(r"CFO=([+-]?[\d.]+)Hz", line)
            if c:
                self._cfo.append(float(c.group(1)))
                del self._cfo[:-MER_AVERAGE]
            self._ok += 1
            self._events.append((now, True))
        elif "CRC MISMATCH" in line:
            self._bad += 1
            self._events.append((now, False))
        m = re.search(r"\((\d+) fragment\(s\) lost", line)
        if m:
            self._lost += int(m.group(1))
            self._events.extend([(now, False)] * int(m.group(1)))

    def _tick(self):
        e = self.engine
        sid = e.station_id_label.text()
        self.station.setText(_station_text(sid))
        if self.is_running():
            if e.procs[0].poll() is not None:
                self.stop()
                self.status.setText("RX stopped unexpectedly")
                return
            self.status.setText(self._status_line())

    def _status_line(self):
        """One row: MER, video fps, video queue, good/bad over STATUS_WINDOW_S."""
        cutoff = time.monotonic() - STATUS_WINDOW_S
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        good = sum(1 for _, ok in self._events if ok)
        bad = len(self._events) - good
        m = re.search(r"Video: (\d+)/", self.engine.video_stats_label.text())
        fps = f"{m.group(1)} fps" if m else "-- fps"
        video = getattr(self.engine, "_video", None)
        queue = f"Q {video.queue_depth()}" if video is not None else "Q --"
        # single-spaced separators: with CFO the line just fits the 7" panel
        ok = f"{100 * good / (good + bad):.0f}% ok" if good + bad else "--% ok"  # over STATUS_WINDOW_S
        return f"{self._mer_text(live=True)} · {self._cfo_text()} · {fps} · {queue} · {ok}"

    def _mer_text(self, live=False):
        # live: nothing decoded for MER_STALE_S (signal gone) reads 0, not the last value
        if live and self._mer and time.monotonic() - self._mer_t > MER_STALE_S:
            return "MER 0.0 dB"
        if not self._mer:
            return "MER --"
        return f"MER {sum(self._mer) / len(self._mer):.1f} dB"

    def _cfo_text(self):
        """Mean carrier frequency offset of the last good fragments (TX vs
        RX oscillator difference, after the LO offset)."""
        if not self._cfo:
            return "CFO --"
        return f"CFO {sum(self._cfo) / len(self._cfo):+.0f} Hz"

    def shutdown(self):
        self.engine.stop()
