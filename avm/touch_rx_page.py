"""
RX view of the touch GUI (touch_gui.py). The receive chain is the full
media_rx_gui.MediaRxWindow, created hidden and used as an engine; its own
video pane and spectrum plot are moved into this page, so decoding, playback
and display are exactly those of the full GUI. Input is always the PlutoSDR.
"""
import collections
import json
import math
import os
import stat

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
LAMP_MAX_LOST = 200  # lost fragments counted for the lamps per reported gap
DUPLEX_RESTART_DELAY_MS = 2000  # duplex RX died: TX+RX restart after this
# Header / Frame lock lamps under the video: over the last LOCK_WINDOW_S
# (longer if fragments come further apart), all / some / none decoded
LOCK_WINDOW_S = 1.0   # halved (was 2.0): the lamps react faster
LAMP_ALL, LAMP_SOME, LAMP_NONE, LAMP_OFF = "#43a047", "#fb8c00", "#e53935", "#4a4d52"
# spectrum Auto: re-fit every this-many seconds; manual mode uses this dB/div
AUTOSCALE_S = 5
TUNE_BAR_COLOUR = "#4fc3f7"  # spectrum tuning indicator: signal band (light blue, not the yellow trace)
TUNE_OK_COLOUR = "#66bb6a"   # ...and the +- pull-in zones at the centre and edges (green)
MANUAL_DB_PER_DIV = 2.5
# Auto: dB/div from these, the smallest whose 10 divisions fit noise..peak
# (10 and 20 only when 5 can't: a very strong signal shouldn't run off the top)
AUTOSCALE_DIVS = (1.0, 2.0, 4.0, 5.0, 10.0, 20.0)
AUTOSCALE_NOISE_LIFT_DB = 1.0   # the noise mean this far above the bottom line
AUTOSCALE_STEP_DB = 0.5         # the scale's bottom (and so its top) moves in these steps


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



MESSAGE_RE = re.compile(r"^\[player\] Message: (\{.*\})$")   # media_rx_player

class _RxEngine(media_rx_gui.MediaRxWindow):
    """The full RX window, never shown; adds a wider spectrum span and the
    radio choice (PlutoSDR, LimeSDR, RTL-SDR, Airspy or SDRplay)."""
    span_hz = 0.0
    sdr = "pluto"
    rtl_ppm = 0.0
    lime_port = "Auto"
    anan_antenna = "ANT1"
    invert_spectrum = False  # the Config tab's RX spectrum: Inverted
    tdd = None          # duplex (touch_widgets.tdd_config): this receiver also transmits
    tdd_tx_gain = -10.0

    def _build_rx_cmd(self):
        cmd = super()._build_rx_cmd()
        # the player buffers audio itself; pacing the output only delayed video ~0.4 s
        cmd += ["--no-output-pacing"]
        if self.span_hz and "--spectrum-stderr" in cmd:
            cmd += ["--spectrum-span-hz", f"{self.span_hz:.0f}"]
        if self.sdr in tw.GAIN_FILE_RADIOS:
            cmd += ["--sdr", self.sdr, "--gain-file", tw.gain_file_path("rx")]
        if self.invert_spectrum:
            cmd += ["--invert-spectrum"]
        # live receive offset (the page's Offset stepper), any radio
        cmd += ["--freq-offset-file", tw.freq_offset_file_path()]
        # live spectrum averaging (the Config tab)
        cmd += ["--spectrum-averages-file", tw.spectrum_averages_file_path()]
        if self.sdr == "rtlsdr" and self.rtl_ppm:
            cmd += ["--sdr-ppm", f"{self.rtl_ppm:g}"]
        if self.sdr == "lime" and self.lime_port != "Auto":
            cmd += ["--sdr-antenna", self.lime_port]
        if self.sdr == "hpsdr":
            cmd += ["--sdr-antenna", self.anan_antenna]
        if self.tdd:
            # same-channel duplex: listen between our own slots, and transmit
            # (on this radio) what the TX page's modulator writes to the FIFO
            t = self.tdd
            cmd += ["--tdd-cycle-s", f"{t['cycle']:g}", "--tdd-slot-s", f"{t['slot']:g}",
                    "--tdd-station", str(t["station"]), "--tdd-tx-fifo", t["fifo"],
                    "--tdd-tx-gain", f"{self.tdd_tx_gain:g}"]
            if t["adjust"]:
                cmd += ["--tdd-adjust-file", t["adjust"]]
            if t["report"]:
                cmd += ["--tdd-report-file", t["report"]]
        return cmd

    def _flush_log_buffer(self):
        # its log widget is never shown (~30 lines/s in the fast modes):
        # just the session log file, if HF_RX_GUI_LOG names one
        if self._log_pending_lines:
            self._write_log_file(self._log_pending_lines)
            self._log_pending_lines = []


class RxPage(QtWidgets.QWidget):
    running_changed = QtCore.pyqtSignal(bool)
    _kiwi_info_ready = QtCore.pyqtSignal(str, str)   # server, text (from a worker thread)

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
        # Header / Frame lock lamps, then the status line, on one row
        status_row = QtWidgets.QHBoxLayout()
        status_row.setSpacing(6)
        self._lamps = {}
        for name in ("Header", "Frame"):
            lamp = QtWidgets.QLabel()
            size = round(14 * tw.SCALE)
            lamp.setFixedSize(size, size)
            lamp.setToolTip(f"{name} lock over the last {LOCK_WINDOW_S:g} s: green all, orange some, red none")
            self._lamps[name] = lamp
            status_row.addWidget(lamp)
            text = QtWidgets.QLabel(name)
            text.setObjectName("dim")
            status_row.addWidget(text)
            status_row.addSpacing(4)
        # start-up progress, in the video area while there's no picture:
        # Python preloaded / radio open / samples arriving (orange until the
        # first spectrum update -- made from received samples -- then green).
        # Positioned by eventFilter (video resize).
        self.stages = tw.StageBlocks(["Python", "Radio", "Samples"], parent=video, block_size=(34, 8))
        self.engine.spectrum_signal.data_ready.connect(self._samples_arriving)
        self.stages.adjustSize()
        status_row.addWidget(self.status, 1)
        left.addLayout(status_row)
        # tap the status line: live constellation (tw.ConstellationDialog)
        self.status.installEventFilter(self)
        self._stage_timer = QtCore.QTimer(self)
        self._stage_timer.timeout.connect(self._idle_stages)
        self._stage_timer.start(500)
        self._set_lamps(None)
        spec = self.engine.spectrum_plot
        spec.setParent(None)
        spec.setMinimumHeight(80)
        # a display only on the touchscreen: a touch nearby panned it sideways
        # (the desktop RX window keeps its pan/zoom)
        spec.setMouseEnabled(x=False, y=False)
        spec.setMenuEnabled(False)
        spec.hideButtons()
        # compact axes for a small plot: no axis titles, small tick labels
        tick_font = self.font()
        tick_font.setPixelSize(max(9, round(11 * tw.SCALE)))
        for side in ("left", "bottom"):
            spec.setLabel(side, "")
            spec.getAxis(side).setStyle(tickFont=tick_font)
        left.addWidget(spec, 2)
        root.addLayout(left, 3)
        self._add_tuning_bar(spec)

        # right: settings, station, start/stop (bottom right, same place as TX's)
        right = QtWidgets.QVBoxLayout()
        right.setSpacing(5)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        self.freq = tw.FreqButton("RX frequency", s_get(settings, "rx_freq", 145500000))
        self.mode = tw.Segmented([(m, m) for m in tw.MODES], tw.saved_mode(s_get(settings, "rx_mode", "A")))
        self.bw = tw.Segmented([(b, b) for b in tw.BANDWIDTHS_KHZ], tw.saved_bandwidth(s_get(settings, "rx_bw", "80")))
        self.mod = tw.Segmented([("qpsk", "QPSK"), ("8psk", "8PSK"), ("16psk", "16PSK"), ("16qam", "16QAM"), ("32qam", "32QAM")],
                              s_get(settings, "rx_mod", "qpsk"))   # 8PSK: modes S/SH; 16PSK: SH
        self.gain = tw.Stepper(0, 73, 1, s_get(settings, "rx_gain", 40), " dB")
        self.audio_out = tw.Picker("Audio output", self._audio_outputs,
                                   s_get(settings, "rx_audio", self.engine.audio_device.currentText()))
        # spectrum reference level (top of the plot), live
        # wide: a KiwiSDR that keeps its own AGC on delivers ~-16 dBFS, which
        # draws at +5..+20 dB here (planet3-style manual gain: ~-60)
        self.ref = tw.Stepper(-140, 40, 5, s_get(settings, "rx_ref", -25), " dB")
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
                                     rx=True, pluto_uri=settings.get("rx_pluto_uri", ""))
        self._pluto_uri = self.radio.pluto_uri  # to notice a change of route
        # RTL-SDR crystal correction (no TCXO); only shown with an RTL-SDR
        self.ppm = tw.Stepper(-200, 200, 1, int(s_get(settings, "rx_rtl_ppm", 60)), " ppm")
        self.engine.rtl_ppm = float(self.ppm.value())
        self.radio.radio_changed.connect(self.set_radio)
        # LimeSDR RX port (LNAL/LNAW/LNAH or Auto): only shown with the LimeSDR
        self.lime_port = tw.lime_port_picker("rx", s_get(settings, "rx_lime_port", "Auto"))
        self.engine.lime_port = self.lime_port.value()
        self.lime_port.changed.connect(self._lime_port_changed)
        # ANAN / openHPSDR receive antenna (ANT1-3): only shown with that radio
        self.anan_ant = tw.Picker("ANAN RX antenna", tw.ANAN_ANTENNAS,
                                  s_get(settings, "rx_anan_antenna", "ANT1"))
        self.engine.anan_antenna = self.anan_ant.value()
        self.anan_ant.changed.connect(self._anan_ant_changed)
        # the radio on its own row (the RX column is too narrow to share Freq's),
        # with the radio's own extra: the LimeSDR port or the RTL-SDR's PPM
        # receive offset, live: nudges the receiver onto an off-frequency signal
        # (its step follows the mode type: tw.RX_OFFSET_STEP_KHZ, set by apply_mode_type)
        self.offset = tw.Stepper(-100, 100, 0.1, s_get(settings, "rx_offset_khz", 0), " kHz")
        self.offset.signed = True
        self.offset.set_value(self.offset.value())
        self.offset.changed.connect(self._offset_changed)
        # radio (with its Lime port / RTL ppm) and audio output live on the
        # Config tab (touch_config_page)
        rows = [("Freq", tw.freq_radio_row(self.freq, self.offset)),
                ("Mode", self.mode), ("kHz", self.bw), ("Modul.", self.mod),
                ("RX gain", self.gain), ("Ref level", ref_row)]
        for r, (label, w) in enumerate(rows):
            grid.addWidget(tw.row_label(label), r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(4)
        right.addLayout(grid)
        right.addStretch(1)
        # KiwiSDR only: which receiver this is (its /status page's name), so
        # it is obvious whose Kiwi the RX uses
        self.kiwi_info = QtWidgets.QLabel()
        self.kiwi_info.setWordWrap(True)
        self.kiwi_info.setAlignment(QtCore.Qt.AlignCenter)
        self.kiwi_info.setStyleSheet(f"color: #f0c040; font-size: {max(10, round(13 * tw.SCALE))}px;")
        self.kiwi_info.hide()
        self._kiwi_info = {}   # server -> text
        self._kiwi_info_ready.connect(self._kiwi_info_got)
        right.addWidget(self.kiwi_info)
        self.run = tw.RunButton("RX")
        # received text messages (tw.messages): always here, grey until one arrives
        self.msg_btn = QtWidgets.QPushButton("✉")
        self.msg_btn.setObjectName("msg")      # its big symbol (tw's stylesheet)
        self.msg_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self.msg_btn.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Expanding)
        self.msg_btn.clicked.connect(lambda: tw.MessagesDialog(self).exec_())
        tw.messages().changed.connect(self._show_messages)
        self.run.clicked.connect(self._run_clicked)
        run_row = QtWidgets.QHBoxLayout()   # RX button, then the message button (a fifth of the row)
        run_row.addWidget(self.run, 4)
        run_row.addWidget(self.msg_btn, 1)
        right.addLayout(run_row)
        self._show_messages()
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
        self._show_idle_warning()

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
        if obj is self._video and event.type() == QtCore.QEvent.Resize:
            self._place_stages()
        if obj is self.status and event.type() == QtCore.QEvent.MouseButtonRelease:
            tw.ConstellationDialog(self, self.engine.spectrum_signal, self.mod.value()).show()
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
        # a KiwiSDR's ~12 kS/s stream carries 10 kHz only
        tw.enforce_mode_bandwidth(self.mode, self.bw, only_khz="10" if self.engine.sdr == "kiwi" else None,
                                  mod_seg=self.mod)
        # after the width check: it can change the width (e.g. VU needs 80 kHz+)
        self._update_tuning_bar()
        e = self.engine
        # how the Pluto is reached: the route picked in the radio list, else
        # its usual network address (pluto_soapy_sink falls back to a search)
        e.pluto_uri.setText(tw.KIWI_SERVER if e.sdr == "kiwi" else "" if e.sdr == "hpsdr"
                            else (self.radio.pluto_uri or tw.default_pluto_uri()))
        e.input_mode.setCurrentText("pluto")
        e.rf_freq.setText(str(self.freq.hz()))
        e.mode.setCurrentText(self.mode.value())
        e.occupancy.setCurrentText(self.bw.value())
        # Kiwi / ANAN: IQ arrives centred (the radio's own DDC), no LO offset
        e.lo_offset_hz.setValue(0 if e.sdr in ("kiwi", "hpsdr") else tw.lo_offset_hz(self.bw.value()))
        e.pluto_sample_rate.setCurrentText(str(tw.RTL_SAMPLE_RATE if e.sdr == "rtlsdr" else tw.sdr_rate(self.bw.value())))
        e.fragment_size.setValue(tw.fragment_size(self.bw.value()))  # before the audio latency below
        e.span_hz = SPECTRUM_SPAN_X * float(self.bw.value()) * 1000
        if e.sdr == "kiwi":
            e.span_hz = min(e.span_hz, 11000.0)  # all of a ~12 kS/s Kiwi stream, no more
        self._show_kiwi_info()
        # the Config tab's choice (was reset to the default here, and RX start
        # then wrote that default to the live file: the choice never stuck)
        e.spectrum_averages.setValue(getattr(self, "_spec_avg", SPECTRUM_AVERAGES))
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
        e.tdd = self.duplex()
        if e.tdd and tw.USB_IDS["pluto"] & (tw.usb_devices() or set()):
            # duplex drives RX and TX through one connection: over the Pluto's
            # network link (ip:) that is one socket, one request at a time,
            # and a waiting RX read held up TX writes -- the transmitter ran
            # ~20% short and its bursts went out late and broken. The USB
            # data connection has separate channels for each direction.
            e.pluto_uri.setText("usb:")
        if e.tdd:
            # bursts arrive a cycle apart: the player must ride over the gaps
            e.audio_latency.setValue(max(e.audio_latency.value(), tw.TDD_PLAYER_LATENCY_S))
            peer = getattr(self, "peer", None)
            if peer is not None:
                e.tdd_tx_gain = peer.gain.value()
        idx = e.audio_device.findText(self.audio_out.value())
        if idx >= 0:
            e.audio_device.setCurrentIndex(idx)
        self._save()
        if getattr(self, "peer", None) is not None:
            tw.refresh_duplex_buttons(self.peer, self)

    def _save(self):
        self.s.update(rx_freq=self.freq.hz(),
                      rx_mode=self.mode.value(), rx_bw=self.bw.value(), rx_mod=self.mod.value(),
                      rx_gain=self.gain.value(), rx_audio=self.audio_out.value(),
                      rx_ref=self.ref.value() if hasattr(self, "ref") else -25,
                      rx_ref_auto=self.ref_auto.isChecked() if hasattr(self, "ref_auto") else True,
                      rx_sdr=self.engine.sdr,
                      rx_pluto_uri=self.radio.pluto_uri if hasattr(self, "radio") else "",
                      rx_rtl_ppm=self.ppm.value() if hasattr(self, "ppm") else 60,
                      rx_offset_khz=self.offset.value() if hasattr(self, "offset") else 0,
                      rx_lime_port=self.lime_port.value() if hasattr(self, "lime_port") else "Auto",
                      rx_anan_antenna=self.anan_ant.value() if hasattr(self, "anan_ant") else "ANT1")

    def _offset_changed(self, khz):
        """Live: the running receiver re-tunes by this much (~0.2 s)."""
        tw.write_freq_offset_file(khz * 1000)
        self._save()

    def _anan_ant_changed(self, ant):
        """Applied when RX (re)starts."""
        self.engine.anan_antenna = ant
        self._save()
        if self.is_running() and self.engine.sdr == "hpsdr":
            self.run.set_state("pending")

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

    def _add_tuning_bar(self, spec):
        """Tuning indicator behind the spectrum trace. Blue band: where the
        signal should sit (the mode's occupied width), dashed centre line.
        Green zones, +- the CFO pull-in (see _tuning_geometry) at each band
        edge: tune so the signal's edges sit inside them, and it locks
        without degrading.
        Follows mode and width."""
        import pyqtgraph as pg
        from PyQt5 import QtGui
        band = QtGui.QColor(TUNE_BAR_COLOUR).getRgb()[:3]
        ok = QtGui.QColor(TUNE_OK_COLOUR).getRgb()[:3]
        self._tune_inner = pg.LinearRegionItem(movable=False, brush=pg.mkBrush(*band, 35), pen=pg.mkPen(None))
        self._tune_zones = [pg.LinearRegionItem(movable=False, brush=pg.mkBrush(*ok, 80), pen=pg.mkPen(None))
                            for _ in range(2)]  # low edge, high edge
        self._tune_centre = pg.InfiniteLine(pos=0, angle=90, movable=False,
                                            pen=pg.mkPen(*band, 130, width=1, style=QtCore.Qt.DashLine))
        for z, item in [(-30, self._tune_inner)] + [(-20, zn) for zn in self._tune_zones] + [(-10, self._tune_centre)]:
            item.setZValue(z)  # behind the trace
            spec.addItem(item)
        self._update_tuning_bar()

    _tuning_cache = {}

    @classmethod
    def _tuning_geometry(cls, mode, khz):
        """(signal low Hz, signal high Hz, pull-in Hz) relative to the tuned
        frequency: how far off tune a signal is still found and decoded (the
        AFC then brings it back to centre). Measured through the real TX and
        RX at 550/800 kS/s, 30 dB SNR (2026-10-19):
          OFDM: the integer-CFO search (+-30 carriers) -- A 10 kHz decodes at
                0.8 kHz off, not 1.3 (= 31 x 41.7 Hz) -- or a quarter of the
                width, whichever is less: VU 80 at 20 kHz but not 30, VU 160
                at 40, VU 250 at 60. (The old limit, the room left inside the
                mode's own rate, was ~10x too strict: VU 80 showed +-1.25 kHz;
                losing an outer carrier's edge is shrugged off by the FEC.)
          S:    the preamble's frequency estimate, +-half the symbol rate."""
        key = (mode, khz)
        if key not in cls._tuning_cache:
            cfg = ofdm.build_config(mode, float(khz), fec_scheme="ldpc")
            sp = cfg.carrier_spacing
            bins = [b if b < cfg.n_fft // 2 else b - cfg.n_fft for b in cfg.fft_bins]
            lo, hi = (min(bins) - 0.5) * sp, (max(bins) + 0.5) * sp
            if ofdm.single_carrier(cfg):
                pull = cfg.symbol_rate / 2
            else:
                pull = min(29 * sp, (hi - lo) / 4)
            cls._tuning_cache[key] = (lo, hi, pull)
        return cls._tuning_cache[key]

    def _update_tuning_bar(self):
        if not hasattr(self, "_tune_inner"):
            return
        try:
            lo, hi, pull = self._tuning_geometry(self.mode.value(), self.bw.value())
        except Exception:
            return  # an invalid combination being switched through: keep the last bar
        if self.mode.value() in tw.SINGLE_CARRIER_MODES:
            # single carrier: its pull-in (+-half the symbol rate) is most of
            # its own width, so edge guides overlapped into a muddle -- one
            # band instead: anywhere in it, the signal is found and decoded
            self._tune_inner.setRegion((lo - pull, hi + pull))
            for zone in self._tune_zones:
                zone.setVisible(False)
            return
        for zone in self._tune_zones:
            zone.setVisible(True)
        # the band shading stops where the edge guides start: overlapping
        # half of each, it tinted them unequally (the two looked different)
        self._tune_inner.setRegion((lo + pull, hi - pull))
        for zone, at in zip(self._tune_zones, (lo, hi)):
            zone.setRegion((at - pull, at + pull))

    def _changed(self, *_):
        self._apply_to_engine()  # (updates the tuning indicator too)
        if self.is_running():
            self.run.set_state("pending")
        else:
            self._show_idle_warning()

    def _show_idle_warning(self):
        warn = tw.cpu_warning(self.mode.value(), self.bw.value(), self.mod.value(), self.duplex())
        if warn or self.status.text().startswith("Duplex 32QAM"):
            self.status.setText(warn or "Idle")

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
        """Bottom of the plot at the mean noise level (the spectrum's two
        ends, outside the signal, averaged in power); dB/div the smallest of
        AUTOSCALE_DIVS whose 10 divisions reach from there past the peak."""
        if not self.ref_auto.isChecked():
            return
        _, y = self.engine.spectrum_curve.getData()
        if y is None or len(y) < 16:
            return
        y = np.asarray(y, float)
        y = y[np.isfinite(y)]
        if len(y) < 16:
            return
        # the view is 1.5x the signal's width: its outer ~4% each side is band
        # noise (power mean, not dB mean -- a dB mean sits ~2.5 dB low)
        k = max(2, len(y) * 4 // 100)   # outer 4% (KiwiSDR: 8% reached the signal)
        edges = np.concatenate([y[:k], y[-k:]])
        noise = 10 * np.log10(np.mean(10 ** (edges / 10)))
        # ...unless the edges aren't clean noise: a narrow view (the
        # KiwiSDR's ~11 kHz) catches the signal's skirts and other stations
        # there, reading the floor too high -- the real floor then sat below
        # the bottom line. The quietest quarter of all bins is a floor that
        # can't be fooled that way; take the lower of the two.
        quiet = y[y <= np.percentile(y, 10)]   # quietest tenth: a quarter included signal (KiwiSDR)
        if len(quiet):
            noise = min(noise, 10 * np.log10(np.mean(10 ** (quiet / 10))))
        peak = float(np.max(y))    # the real peak, narrow carriers and spurs too
        bottom = noise - AUTOSCALE_NOISE_LIFT_DB   # the noise mean just above the bottom line
        bottom = round(bottom / AUTOSCALE_STEP_DB) * AUTOSCALE_STEP_DB   # moves in whole steps only
        span = peak + 1.0 - bottom  # a little headroom over the peak
        div = next((d for d in AUTOSCALE_DIVS if 10 * d >= span), AUTOSCALE_DIVS[-1])
        # while RX runs, dB/div only ever widens (peak hold): a scale that
        # jumped back down between bursts made the picture pump; reset at
        # each RX start
        if self.is_running():
            div = max(div, getattr(self, "_held_div", 0.0))
            self._held_div = div
        top = bottom + 10 * div
        self.ref.set_value(int(round(top)))   # display only (no emit: stays on Auto)
        self._set_scale(top, div)

    def _show_kiwi_info(self):
        server = tw.KIWI_SERVER if self.engine.sdr == "kiwi" else ""
        if not server:
            self.kiwi_info.hide()
            return
        self.kiwi_info.setText(self._kiwi_info.get(server, f"KiwiSDR {server}"))
        self.kiwi_info.show()
        if server not in self._kiwi_info:
            self._kiwi_info[server] = f"KiwiSDR {server}"   # one fetch per server
            import threading
            threading.Thread(target=self._fetch_kiwi_info, args=(server,), daemon=True).start()

    def _fetch_kiwi_info(self, server):
        """Worker thread: the Kiwi's public /status page (name=, antenna=...)."""
        import urllib.request
        host = server if ":" in server else server + ":8073"
        try:
            with urllib.request.urlopen(f"http://{host}/status", timeout=6) as r:
                txt = r.read(8192).decode("utf-8", "replace")
        except Exception:
            self._kiwi_info_ready.emit(server, f"KiwiSDR {server}\n(no status page)")
            return
        st = dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)
        name = st.get("name", "").strip() or server
        extra = " · ".join(x for x in (
            f"users {st['users']}/{st['users_max']}" if st.get("users") and st.get("users_max") else "",
            "OFFLINE" if st.get("offline") == "yes" else "") if x)
        self._kiwi_info_ready.emit(server, "KiwiSDR: " + name + (f"\n{extra}" if extra else ""))

    def _kiwi_info_got(self, server, text):
        self._kiwi_info[server] = text
        self._show_kiwi_info()

    def set_radio(self, sdr, force=False):
        """'pluto', 'lime', 'rtlsdr', 'airspy', 'airspyhf' or 'sdrplay' -- from the Radio
        picker. RX gain tops out at 61 dB on a LimeSDR, ~49 on an RTL-SDR,
        45 on an Airspy (LNA+mixer+VGA), 73 on the Pluto. The HF+ and SDRplay
        run their own AGC (the gain setting is ignored)."""
        self.radio.set_sdr(sdr)
        self.freq.set_radio(sdr)
        if not self.freq.in_range():
            msg = (f"{self.freq.hz() / 1e6:g} MHz is outside the {tw.RADIO_NAMES.get(sdr, sdr)}'s "
                   f"range -- set a new RX frequency")
            tw.notices().add("RX", msg)  # radio messages: errors list only
        self.lime_port.setVisible(sdr == "lime")
        self.anan_ant.setVisible(sdr == "hpsdr")
        self.ppm.setVisible(sdr == "rtlsdr")
        # ANAN: 0-31 drives its step attenuator (31 = none)
        self.gain.hi = {"lime": 61, "rtlsdr": 49, "airspy": 45, "airspyhf": 45, "kiwi": 120,
                        "hpsdr": 31}.get(sdr, 73)
        # a KiwiSDR's manual gain 0 is silence (its output scales from nothing)
        self.gain.lo = 10 if sdr == "kiwi" else 0
        self.gain.set_value(self.gain.value(), emit=True)
        uri = self.radio.pluto_uri
        changed_uri = uri != getattr(self, "_pluto_uri", uri)
        self._pluto_uri = uri
        if force or sdr != self.engine.sdr or changed_uri:
            self.engine.sdr = sdr
            if not force:
                self._apply_to_engine()  # the RTL-SDR's own sample rate
            self._save()
            if self.is_running():
                self.run.set_state("pending")

    def _gain_changed(self, db):
        self.engine.rx_gain.setValue(db)
        self._save()
        if self.engine.sdr in tw.GAIN_FILE_RADIOS:
            tw.write_gain_file("rx", db)  # the running RX process picks it up
            return
        if self.is_running():
            if tw.LiveGain.available():
                self.live_gain.set(self.engine.pluto_uri.text().strip() or tw.default_pluto_uri() or "ip:192.168.2.1", db)
            else:
                self.run.set_state("pending")

    # ------------------------------------------------------------------
    running_tdd = None   # duplex settings the running receiver was started with

    def duplex(self):
        """This station's duplex settings (touch_widgets.tdd_config), or None."""
        peer = getattr(self, "peer", None)
        role = self.s.get("duplex", "off")
        if peer is None or not tw.duplex_active(role, peer.mode.value(), self.mode.value(),
                                                peer.engine.sdr, self.engine.sdr):
            return None
        return tw.tdd_config(role)

    def is_running(self):
        return bool(self.engine.procs)

    def _run_clicked(self):
        self.run.run_pressed(self._run_clicked_now)

    def _run_clicked_now(self):
        tx = self.peer
        if self.duplex():
            # duplex: this button is TX+RX (TX's own is hidden). (Asked
            # fresh, not the engine's copy: at start-up the engine's was set
            # before the pages were linked -- "no duplex" -- so the first
            # press started RX only, and TX never.)
            if self.run.state == "stopped":
                tw.offer_duplex_match(self, tx, "RX", "TX")
                if tx.is_running():
                    tx.stop()
                self.start()
                if self.is_running():
                    tx.start()     # waits for this receiver, which transmits for it
            elif self.run.state == "pending":
                self.stop()        # (stops TX too)
                self.start()
                if self.is_running():
                    tx.start()
            else:
                self.stop()        # (stops TX too)
            return
        if self.run.state == "stopped":
            restart_tx = tw.offer_duplex_match(self, tx, "RX", "TX") and tx.is_running()
            if restart_tx:
                tx.stop()      # apply the match now: TX restarts with RX's settings ...
            self.start()
            if restart_tx and self.is_running():
                tx.start()     # ... once this receiver (its radio, in duplex) is up
        elif self.run.state == "pending":
            # restart to apply: in duplex stopping RX also stops TX (this
            # receiver is its radio) -- bring TX back too if it was on
            tx = self.peer
            tx_was_on = bool(self.engine.tdd) and tx.is_running()
            self.stop()
            self.start()
            if tx_was_on and self.is_running():
                tx.start()
        else:
            self.stop()

    def _lime_busy(self):
        """True (and says so) if TX holds the LimeSDR this RX wants."""
        peer = getattr(self, "peer", None)
        if (self.engine.sdr == "lime" and peer is not None and peer.is_running()
                and peer.engine.sdr == "lime"):
            tw.notices().add("RX", "Not started: the LimeSDR is in use by TX (one Lime can't do both)")
            return True
        return False

    def start(self):
        if self._lime_busy():
            return
        if not self.freq.in_range():
            # the driver would quietly open at the nearest frequency it can tune
            lo, hi = self.freq._range
            tw.notices().add("RX", f"Not started: {self.freq.hz() / 1e6:g} MHz is outside the "
                                    f"{tw.RADIO_NAMES.get(self.engine.sdr, self.engine.sdr)}'s range "
                                    f"({lo:g}-{hi:g} MHz) -- set a new RX frequency")
            return
        if self.engine.sdr == "kiwi" and (self.bw.value() != "10" or self.mode.value() == "VU"):
            tw.notices().add("RX", "Not started: the KiwiSDR only carries 10 kHz -- set 10 kHz, mode A-D")
            return
        found, why = tw.radio_present(self.engine.sdr, self.engine.pluto_uri.text().strip() or None)
        if not found:
            tw.notices().add("RX", f"Not started: {why}")  # radio messages: errors list only
            return
        self._apply_to_engine()
        self._radio_error = ""
        self._ok = self._lost = self._bad = 0
        self._mer = []
        self._cfo = []
        self._events.clear()
        tw.write_gain_file("rx", self.gain.value())  # so a stale value can't apply at start
        tw.write_freq_offset_file(self.offset.value() * 1000)
        tw.write_spectrum_averages_file(self.engine.spectrum_averages.value())
        if self.engine.tdd:
            # the FIFO the TX page's modulator writes into; a slave starts
            # from no slot shift
            fifo, adjust, report = self.engine.tdd["fifo"], self.engine.tdd["adjust"], self.engine.tdd["report"]
            try:
                if os.path.exists(fifo) and not stat.S_ISFIFO(os.stat(fifo).st_mode):
                    os.remove(fifo)
                if not os.path.exists(fifo):
                    os.mkfifo(fifo)
                for stale in (adjust, report):   # (never a previous run's)
                    if stale and os.path.exists(stale):
                        os.remove(stale)
            except OSError as e:
                self.status.setText(f"Not started: duplex FIFO: {e}")
                return
        self.engine.start()
        # how the running receiver was started: the engine's settings follow
        # the page (e.g. Duplex switched off) before it is restarted
        self.running_tdd = self.engine.tdd if self.is_running() else None
        if self.is_running():
            self.run.set_state("running")
            self._held_div = 0.0   # the spectrum's held dB/div starts afresh (see _autoscale)
            self.status.setText("Searching...")
            self.stages.set_states(["busy", "todo", "todo"])
            self.running_changed.emit(True)

    def stop(self):
        # duplex: this receiver is also the transmitter's radio
        peer = getattr(self, "peer", None)
        if self.running_tdd and peer is not None and peer.is_running():
            peer.stop()
        self.engine.stop()
        self.running_tdd = None
        self._set_lamps(None)
        self.run.set_state("stopped")
        self.status.setText(f"Stopped  ·  {self._ok} OK, {self._lost} lost, {self._bad} bad"
                            f"  ·  {self._mer_text()}  ·  {self._cfo_text()}")
        self.running_changed.emit(False)

    def _place_stages(self):
        """Centred, a little below the middle (under the "(no video)" text)."""
        v, s = self._video, self.stages
        s.move((v.width() - s.width()) // 2, v.height() // 2 + round(18 * tw.SCALE))

    def _idle_stages(self):
        """The bar shows only while there's no picture; while stopped, its
        first block shows the preload (warm_pool.py)."""
        pm = getattr(self._video, "_raw_pixmap", None)
        self.stages.setVisible(pm is None or pm.isNull())
        if self.is_running():
            return
        import warm_pool
        st = warm_pool.state("rx")
        self.stages.set_states([{"ready": "done", "loading": "busy"}.get(st, "todo"), "todo", "todo"])

    def _stage_from_log(self, line):
        if "started from the preloaded process" in line or "] Mode " in line:
            self.stages.set(0, "done")
        elif "Opening" in line and "RX" in line:
            self.stages.set(1, "busy")
        elif "RX active" in line:
            self.stages.set(1, "done")
            self.stages.set(2, "busy")

    def _samples_arriving(self, *_):
        if self.is_running() and self.stages.states[2] != "done":
            self.stages.set(2, "done")

    def _show_messages(self):
        m = tw.messages()
        self.msg_btn.setText(f"✉ {m.unread}" if m.unread else "✉")
        self.msg_btn.setEnabled(bool(m.items))      # grey (and nothing to open) with none
        tw.set_style(self.msg_btn, f"background: {tw.RX_ACCENT}; color: white;" if m.unread else "")

    def _on_log(self, line):
        self._stage_from_log(line)
        m = MESSAGE_RE.match(line)
        if m:   # a text message (reported once per session by the player)
            try:
                msg = json.loads(m.group(1))
                tw.messages().add(msg["ts"], msg["text"],
                                  getattr(self.engine, "_confirmed_station_id", None) or "")
            except (ValueError, KeyError, TypeError):
                pass
            return
        err = tw.error_line(line)
        if err:
            tw.notices().add("RX", err, exception=tw.is_exception_line(line))
        # KiwiSDR trouble (refused, full, password, dropped...): to the errors
        # list, and kept as the reason if RX then stops
        tag = next((t for t in ("KiwiSDR", "ANAN/HPSDR") if t in line), None)
        if tag and "RX active" not in line and "attenuator now" not in line:
            self._radio_error = line[line.index(tag):].strip()
            if not err:
                tw.notices().add("RX", self._radio_error)
        # MER from each fragment's EVM (RMS error / ideal): MER = -20 log10(EVM)
        m = re.search(r"Fragment seq=\d+: .*?EVM=([\d.]+)%", line)
        if m and float(m.group(1)) > 0:
            if self._mer and time.monotonic() - self._mer_t > MER_STALE_S:
                self._mer = []  # back after a gap: don't average in pre-gap values
            self._mer.append(-20 * math.log10(float(m.group(1)) / 100))
            del self._mer[:-MER_AVERAGE]
            self._mer_t = time.monotonic()
        now = time.monotonic()
        # CFO from every fragment whose header decoded (OK or CRC MISMATCH:
        # the header's checksum held, so its lock and CFO are real -- far off
        # tune those may be all there is); not from a garbage-header failure
        if re.search(r"Fragment seq=\d+: (OK|CRC MISMATCH)", line):
            c = re.search(r"CFO=([+-]?[\d.]+)Hz", line)
            if c:
                self._cfo.append(float(c.group(1)))
                del self._cfo[:-MER_AVERAGE]
        if re.search(r"Fragment seq=\d+: OK", line):
            self._ok += 1
            self._events.append((now, True, True))   # (time, header ok, frame ok)
        elif "CRC MISMATCH" in line:
            self._bad += 1
            self._events.append((now, True, False))  # header decoded, payload didn't
        elif "FAILED (garbage header" in line:
            self._events.append((now, False, False))  # header itself failed
        m = re.search(r"\((\d+) fragment\(s\) lost", line)
        if m:
            self._lost += int(m.group(1))
            # the lamps only judge the last few seconds: a few hundred is
            # "all red" already (63604 at once froze the GUI for good).
            # Frame lamp only (header None: not judged): a gap shows only
            # once good fragments arrive, and may be decoder backlog skipped
            # -- counted against the header it turned that lamp orange as
            # the signal got BETTER, while at MER 3 (no gaps seen) it stayed
            # green. Header failures are counted as they happen, above.
            self._events.extend([(now, None, False)] * min(int(m.group(1)), LAMP_MAX_LOST))

    def _tick(self):
        e = self.engine
        sid = e.station_id_label.text()
        self.station.setText(_station_text(sid))
        if self.is_running():
            if e.procs[0].poll() is not None:
                duplex = bool(self.running_tdd)
                tx_was_on = duplex and self.peer.is_running()
                self.stop()
                if duplex:
                    # (e.g. its stream stalled and it exited): duplex restarts
                    # itself -- a station must not stay off, or worse, half on
                    tw.notices().add("RX", "Duplex RX stopped unexpectedly -- restarting TX+RX")
                    self.status.setText("RX stopped unexpectedly -- restarting...")
                    def restart():
                        if self.run.state == "stopped":
                            self.start()
                            if tx_was_on and self.is_running():
                                self.peer.start()
                    QtCore.QTimer.singleShot(DUPLEX_RESTART_DELAY_MS, restart)
                    return
                why = getattr(self, "_radio_error", "")
                self.status.setText("RX stopped unexpectedly")
                tw.notices().add("RX", f"RX stopped: {why}" if why else "RX stopped unexpectedly")
                return
            self.status.setText(self._status_line())
            self._update_lamps()

    def set_invert_spectrum(self, on):
        """The Config tab's RX spectrum (Normal / Inverted): applied at once
        -- a running receiver is restarted."""
        if on == self.engine.invert_spectrum:
            return
        self.engine.invert_spectrum = on
        if self.is_running():
            self.stop()
            self.start()

    def set_spectrum_averages(self, n):
        """FFT frames averaged per spectrum update (the Config tab: 4, 8,
        16 or 32). Live: the running receiver re-reads it (no restart)."""
        self._spec_avg = int(n)
        self.engine.spectrum_averages.setValue(int(n))
        self.s["rx_spec_avg"] = int(n)
        tw.write_spectrum_averages_file(n)

    def _status_line(self):
        """One row: MER measured/required, CFO, video fps (lock: the lamps)."""
        m = re.search(r"Video: (\d+)/", self.engine.video_stats_label.text())
        fps = f"{m.group(1)} fps" if m else "-- fps"
        return f"{self._mer_text(live=True)} · {self._cfo_text()} · {fps}"

    def _lock_window(self):
        """Seconds the lamps judge over: LOCK_WINDOW_S, or longer when
        fragments are further apart than that (slow modes), so a window
        always spans at least one of them (1.25x the gap; was 2.5x)."""
        times = [e[0] for e in self._events]
        gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
        typical = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
        return max(LOCK_WINDOW_S, 1.25 * typical)

    def _update_lamps(self):
        cutoff = time.monotonic() - max(STATUS_WINDOW_S, self._lock_window())
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        since = time.monotonic() - self._lock_window()   # once, not per event
        recent = [e for e in self._events if e[0] >= since]
        self._set_lamps(recent)

    def _set_lamps(self, events):
        """events: (time, header ok, frame ok) in the window; None = idle."""
        for i, name in ((1, "Header"), (2, "Frame")):
            if events is None:
                colour = LAMP_OFF
            else:
                judged = [e[i] for e in events if e[i] is not None]
                good = sum(judged)
                colour = (LAMP_OFF if not judged else LAMP_NONE if not good
                          else LAMP_ALL if good == len(judged) else LAMP_SOME)
            r = self._lamps[name].width() // 4
            tw.set_style(self._lamps[name], f"background: {colour}; border-radius: {r}px;")

    def _mer_required(self):
        """MER (dB) at which 90% of frames decode in plain AWGN for the
        current mode / width / modulation (mer_thresholds.json), or None."""
        t = tw.mer_threshold(self.mode.value(), self.bw.value(), self.mod.value())
        return t and t.get("mer_frame90")

    def _mer_text(self, live=False):
        """"MER measured/required dB" -- required: see _mer_required."""
        req = self._mer_required()
        need = f"{req:.1f}" if req is not None else "--"
        # live: nothing decoded for MER_STALE_S (signal gone) reads 0, not the last value
        if live and self._mer and time.monotonic() - self._mer_t > MER_STALE_S:
            return f"0.0/{need} dB"
        if not self._mer:
            return f"--/{need} dB"
        return f"{sum(self._mer) / len(self._mer):.1f}/{need} dB"

    def _cfo_text(self):
        """Mean carrier frequency offset of the last fragments whose header
        decoded (TX vs RX oscillator difference, after the LO offset)."""
        if not self._cfo:
            return "-- Hz"
        return f"{sum(self._cfo) / len(self._cfo):+.0f} Hz"

    def shutdown(self):
        self.engine.stop()
