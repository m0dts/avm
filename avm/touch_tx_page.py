"""
TX view of the touch GUI (touch_gui.py). The transmit chain itself is the
full media_tx_gui.MediaTxWindow, created hidden and used as an engine: this
page sets its fields and calls start()/stop(), so command building, bitrate
maths and process handling are exactly those of the full GUI. Video is
always the wavelet codec; the output is always the PlutoSDR.
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

try:
    import cv2 as _cv2
except ImportError:
    _cv2 = None

import media_source
import media_tx_framer
import media_tx_gui
import preview_shm
import touch_widgets as tw

PREVIEW_PATH = preview_shm.default_path() + "_tx"
SOURCES = {"test": "Test pattern", "device": "Camera + mic"}  # engine source -> label
TX_QUEUE_DEPTH = 1  # fragments built ahead of the radio (see _apply_to_engine)


class _TxEngine(media_tx_gui.MediaTxWindow):
    """The full TX window, never shown; adds the live camera preview and
    the radio choice (PlutoSDR or LimeSDR)."""
    sdr = "pluto"

    def _append_log(self, text):
        # its log widget is never shown (the page reads log_stream itself);
        # keep a TX session log beside the RX one when that's enabled --
        # minus the per-fragment line, which would be ~6 lines a second
        rx_log = os.environ.get("HF_RX_GUI_LOG")
        if not rx_log or "Streamed fragment" in text:
            return
        try:
            with open(os.path.join(os.path.dirname(rx_log), "tx_session.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
        except OSError:
            pass

    def _build_tx_cmd(self):
        cmd = super()._build_tx_cmd()
        if self.sdr == "lime":
            cmd += ["--sdr", "lime", "--gain-file", tw.gain_file_path("tx")]
        return cmd

    def _build_video_source_cmd(self, vbv_seconds=None):
        cmd = super()._build_video_source_cmd(vbv_seconds)
        if any("media_source_wavelet.py" in c for c in cmd):
            cmd += ["--preview", PREVIEW_PATH]
        return cmd


def _is_c920(device):
    """True if this v4l2 device is a Logitech C920 (manual focus supported)."""
    try:
        with open(f"/sys/class/video4linux/{os.path.basename(device)}/name") as f:
            return "C920" in f.read()
    except OSError:
        return False


class CameraFocus:
    """Sets a C920's focus with v4l2-ctl -- works while the camera is
    streaming, so it applies live. Requests run on a background thread,
    newest wins, so holding the stepper never stalls the UI."""

    def __init__(self):
        self._pending = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def set(self, device, auto, position):
        with self._lock:
            self._pending = (device, auto, position)
        self._event.set()

    def _run(self):
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                job, self._pending = self._pending, None
            if job is None:
                continue
            device, auto, position = job
            # Two separate calls: the C920 rejects focus_absolute while
            # autofocus is on, and one combined call fails as a whole.
            steps = ["focus_automatic_continuous=1"] if auto else \
                ["focus_automatic_continuous=0", f"focus_absolute={int(position)}"]
            for ctrl in steps:
                try:
                    subprocess.run(["v4l2-ctl", "-d", device, "-c", ctrl], capture_output=True, timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            threading.Event().wait(0.1)


def _video_devices():
    return media_source.list_dshow_devices()["video"] or ["/dev/video0"]


def _audio_devices():
    return media_source.list_dshow_devices()["audio"] or ["default"]


class TxPage(QtWidgets.QWidget):
    running_changed = QtCore.pyqtSignal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.s = settings
        self.engine = _TxEngine()
        self.engine.hide()
        self.engine.log_stream.line_received.connect(self._on_log)
        self.live_gain = tw.LiveGain("tx")
        self._sent = 0
        self._last_error = ""
        self._preview = preview_shm.PreviewReader(PREVIEW_PATH)

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(8, 4, 8, 6)
        root.setSpacing(10)

        # left: preview, bitrates, start/stop
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(4)
        self.preview = tw.AspectLabel("Camera preview\nwhile transmitting")
        left.addWidget(self.preview, 3)
        info = tw.panel()
        il = QtWidgets.QVBoxLayout(info)
        il.setContentsMargins(10, 4, 10, 4)
        il.setSpacing(2)
        # one line each, elided -- a long error message must not widen the window
        self.rates = tw.ElideLabel()
        self.rates.setObjectName("value")
        self.link = tw.ElideLabel()
        self.link.setObjectName("dim")
        self.warning = tw.ElideLabel()
        self.warning.setObjectName("warn")
        self.status = tw.ElideLabel("Idle")
        self.status.setObjectName("dim")
        for w in (self.rates, self.link, self.warning, self.status):
            il.addWidget(w)
        left.addWidget(info)
        # C920 focus: only shown for that camera (see _update_focus_row)
        self.focus_cam = CameraFocus()
        self.focus_row = QtWidgets.QWidget()
        self.focus_row.setObjectName("seg")
        fl = QtWidgets.QHBoxLayout(self.focus_row)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(6)
        fl.addWidget(tw.row_label("Focus"))
        self.focus_mode = tw.Segmented([("auto", "Auto"), ("manual", "Manual")],
                                       s_get(settings, "tx_focus_mode", "auto"))
        self.focus = tw.Stepper(0, 250, 5, s_get(settings, "tx_focus", 0))
        fl.addWidget(self.focus_mode, 3)
        fl.addWidget(self.focus, 2)
        left.addWidget(self.focus_row)
        self.focus_mode.changed.connect(self._focus_changed)
        self.focus.changed.connect(self._focus_changed)
        # camera and mic under the preview
        self.camera = tw.Picker("Camera", _video_devices, s_get(settings, "tx_camera", "/dev/video0"))
        self.mic = tw.Picker("Microphone", _audio_devices, s_get(settings, "tx_mic", "default"))
        labels = [self.focus_row.layout().itemAt(0).widget()]
        for label, w in (("Camera", self.camera), ("Mic", self.mic)):
            row = QtWidgets.QWidget()
            row.setObjectName("seg")
            rl = QtWidgets.QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(6)
            labels.append(tw.row_label(label))
            rl.addWidget(labels[-1])
            rl.addWidget(w, 1)
            left.addWidget(row)
        width = max(lab.sizeHint().width() for lab in labels)  # line the three rows up
        for lab in labels:
            lab.setFixedWidth(width)
        root.addLayout(left, 4)

        # right: settings
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)  # 9 rows + START must fit 480 px
        self.source = tw.Picker("Source", list(SOURCES.values()),
                                SOURCES.get(s_get(settings, "tx_source", "test"), SOURCES["test"]))
        self.res = tw.Picker("Resolution (16:9)", media_tx_gui.WAVELET_RESOLUTIONS,
                             _valid_res(s_get(settings, "tx_res", "256x144")))
        self.audio =tw.Segmented([("codec2", "Codec2"), ("opus", "Opus")], s_get(settings, "tx_audio", "codec2"))
        # Not every ffmpeg has a Codec2 encoder (e.g. the standard Windows
        # builds): TX would fail at start ("Error opening output file").
        self._no_codec2 = not tw.ffmpeg_has_encoder("libcodec2")
        if self._no_codec2:
            self.audio._buttons["codec2"].setEnabled(False)
            if self.audio.value() == "codec2":
                self.audio.set_value("opus")
        self.fps = tw.Segmented([(f, f) for f in ("4", "6", "8", "10", "12", "15")], s_get(settings, "tx_fps", "12"))
        self.freq = tw.FreqButton("TX frequency", s_get(settings, "tx_freq", 145500000))
        self.mode = tw.Segmented([(m, m) for m in tw.MODES], tw.saved_mode(s_get(settings, "tx_mode", "A")))
        self.bw = tw.Segmented([(b, b) for b in tw.BANDWIDTHS_KHZ], tw.saved_bandwidth(s_get(settings, "tx_bw", "80")))
        self.mod = tw.Segmented([("qpsk", "QPSK"), ("16qam", "16QAM")], s_get(settings, "tx_mod", "qpsk"))
        self.gain = tw.Stepper(-89, 0, 1, s_get(settings, "tx_gain", -10), " dB")
        self.radio = tw.RadioPicker("TX radio (connected now)", settings.get("tx_sdr", settings.get("sdr", "pluto")))
        self.radio.radio_changed.connect(self.set_radio)
        src_row = QtWidgets.QWidget()
        src_row.setObjectName("seg")
        sl = QtWidgets.QHBoxLayout(src_row)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(4)
        sl.addWidget(self.source, 3)
        sl.addWidget(self.res, 2)
        # callsign / short message, sent as the station ID (media_tx_framer
        # --station-id: 20 characters, repeated every 24 fragments)
        self.callsign = tw.TextButton("Callsign / message", s_get(settings, "tx_callsign", ""),
                                      max_len=media_tx_framer.STATION_ID_LEN)
        rows = [("Call", self.callsign), ("Source", src_row), ("Audio", self.audio), ("FPS", self.fps),
                ("Freq", tw.freq_radio_row(self.freq, self.radio)), ("TX gain", self.gain),
                ("Mode", self.mode), ("kHz", self.bw), ("Modul.", self.mod)]
        for r, (label, w) in enumerate(rows):
            grid.addWidget(tw.row_label(label), r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(1, 1)
        right = QtWidgets.QVBoxLayout()
        right.addLayout(grid)
        right.addStretch(1)
        self.run = tw.RunButton("TX")  # bottom right, same place as RX's
        self.run.clicked.connect(self._run_clicked)
        right.addWidget(self.run)
        root.addLayout(right, 5)

        for w in (self.source, self.res, self.audio, self.fps, self.mode, self.bw, self.mod):
            w.changed.connect(self._changed)
        for w in (self.camera, self.mic, self.freq, self.callsign):
            w.changed.connect(self._changed)
        self.gain.changed.connect(self._gain_changed)

        self.engine.sdr = self.radio.sdr
        self._apply_to_engine()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(100)

    # ------------------------------------------------------------------
    def _apply_to_engine(self):
        tw.enforce_mode_bandwidth(self.mode, self.bw)
        e = self.engine
        e.source_combo.setCurrentText(self._source_key())
        e.station_id.setText(self.callsign.value())
        e.video_device.setEditText(self.camera.value())
        e.audio_device.setEditText(self.mic.value())
        e.audio_codec.setCurrentText(self.audio.value())
        e.video_codec.setCurrentText("wavelet")
        e.resolution.setCurrentText(self.res.value())  # after the codec: that resets the presets
        e.framerate.setValue(float(self.fps.value()))  # the engine recalculates the video bitrate
        e.output_mode.setCurrentText("pluto")
        e.rf_freq.setText(str(self.freq.hz()))
        e.mode.setCurrentText(self.mode.value())
        e.occupancy.setCurrentText(self.bw.value())
        e.lo_offset_hz.setValue(tw.lo_offset_hz(self.bw.value()))
        e.pluto_sample_rate.setCurrentText(str(tw.sdr_rate(self.bw.value())))
        e.fragment_size.setValue(tw.fragment_size(self.bw.value()))
        # One fragment built ahead, not two: each queued fragment is a whole
        # fragment-time of delay (0.24 s at 80 kHz). No TX underruns at 1 in
        # radio-link tests at 250 kHz.
        e.tx_queue_depth.setValue(TX_QUEUE_DEPTH)
        e.modulation.setCurrentText(self.mod.value())
        e.tx_gain.setValue(self.gain.value())
        device = self._source_key() == "device"
        self.camera.setEnabled(device)
        self.mic.setEnabled(device)
        self._update_focus_row()
        self._refresh_info()
        self._save()

    def _source_key(self):
        return next((k for k, v in SOURCES.items() if v == self.source.value()), "test")

    def _update_focus_row(self):
        show = (self._source_key() == "device" and _is_c920(self.camera.value())
                and shutil.which("v4l2-ctl") is not None)
        self.focus_row.setVisible(show)
        self.focus.setEnabled(self.focus_mode.value() == "manual")
        return show

    def _focus_changed(self, *_):
        self.focus.setEnabled(self.focus_mode.value() == "manual")
        self._save()
        if self._update_focus_row():
            self.focus_cam.set(self.camera.value(), self.focus_mode.value() == "auto", self.focus.value())

    def _save(self):
        self.s.update(tx_source=self._source_key(), tx_res=self.res.value(), tx_callsign=self.callsign.value(),
                      tx_camera=self.camera.value(), tx_mic=self.mic.value(),
                      tx_audio=self.audio.value(), tx_fps=self.fps.value(), tx_freq=self.freq.hz(),
                      tx_mode=self.mode.value(),
                      tx_focus_mode=self.focus_mode.value() if hasattr(self, "focus_mode") else "auto",
                      tx_focus=self.focus.value() if hasattr(self, "focus") else 0,
                      tx_bw=self.bw.value(), tx_mod=self.mod.value(), tx_gain=self.gain.value(),
                      tx_sdr=self.engine.sdr)

    def _refresh_info(self):
        e = self.engine
        audio = 3.2 if self.audio.value() == "codec2" else e.audio_bitrate.value()
        video = getattr(e, "_video_enabled", True)
        self.rates.setText(f"Video {e.video_bitrate.value():.1f} kbps   Audio {audio:.1f} kbps" if video
                           else f"NO VIDEO   Audio {audio:.1f} kbps")
        m = re.search(r"~([\d.]+)\s*kbps", e.link_capacity_label.text())
        link = f"Link {m.group(1)} kbps" if m else "Link --"
        self.link.setText(f"{link}  ·  {e.resolution.currentText()} @ {e.framerate.value():g} fps"
                          f"  ·  {'LimeSDR' if e.sdr == 'lime' else 'PlutoSDR'}")
        warn = e.video_bitrate_warning.text() or e.audio_packing_warning.text()
        if not video:
            warn = "NO VIDEO: link too slow, sending audio only -- use a wider kHz or faster mode"
        elif not warn and getattr(self, "_no_codec2", False):
            warn = "Codec2 unavailable: this ffmpeg has no Codec2 encoder (using Opus)"
        # the placeholder shows while there is no camera picture
        new_text = self._preview_idle_text()
        if new_text != getattr(self, "_preview_text", None):
            self._preview_text = new_text
            if not self.is_running() or not video:
                self.preview.clear_image(new_text)
        self.warning.setText(warn[:140])
        self.warning.setVisible(bool(warn))

    def _preview_idle_text(self):
        if getattr(self.engine, "_video_enabled", True):
            return "Camera preview\nwhile transmitting"
        return "NO VIDEO\naudio only at this mode / kHz"

    def _changed(self, *_):
        self._apply_to_engine()
        if self.is_running():
            self.run.set_state("pending")

    def set_radio(self, sdr):
        """'pluto' or 'lime' -- from the Radio picker."""
        self.radio.set_sdr(sdr)
        if sdr != self.engine.sdr:
            self.engine.sdr = sdr
            self._refresh_info()
            self._save()
            if self.is_running():
                self.run.set_state("pending")

    def _gain_changed(self, db):
        self.engine.tx_gain.setValue(db)
        self._save()
        if self.engine.sdr == "lime":
            tw.write_gain_file("tx", db)  # the running TX process picks it up
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

    def start(self):
        self._apply_to_engine()
        self._sent = 0
        self._last_error = ""
        tw.write_gain_file("tx", self.gain.value())  # so a stale value can't apply at start
        self.engine.start()
        if self.is_running():
            self.run.set_state("running")
            self.status.setText("Starting...")
            # the camera may reset its controls when ffmpeg opens it
            QtCore.QTimer.singleShot(2500, self._focus_changed)
            self.running_changed.emit(True)
        else:
            self.status.setText(self._last_error or "Could not start")

    def stop(self):
        # The framer's own children (video/audio sources) otherwise only
        # exit once they next write to the closed pipe -- seconds later, or
        # much longer if one is still JIT-compiling at start-up.
        if sys.platform.startswith("linux"):
            for p in self.engine.procs:
                subprocess.run(["pkill", "-TERM", "-P", str(p.pid)], capture_output=True)
        self.engine.stop()
        self.run.set_state("stopped")
        self.preview.clear_image(self._preview_idle_text())
        self.status.setText(f"Stopped ({self._sent} fragments sent)")
        self.running_changed.emit(False)

    def _on_log(self, line):
        if "Streamed fragment" in line:
            self._sent += 1
        # the video source's warm-up (media_source_wavelet.py): the first
        # start on a machine compiles the codec, minutes on a slow CPU
        if "preparing video codec" in line:
            self.preview.clear_image("Preparing video codec...\n"
                                     "(first start on this machine: can take a few minutes)")
        elif "video codec ready" in line:
            self.preview.clear_image("Starting video...")
        low = line.lower()
        if "error" in low or "traceback" in low or "silence-filled" in low:
            self._last_error = line.strip()[:120]

    def _tick(self):
        if self.is_running():
            dead = [p for p in self.engine.procs if p.poll() is not None]
            if dead:
                self.stop()
                self.status.setText(("TX stopped: " + self._last_error) if self._last_error
                                    else "TX stopped unexpectedly")
                return
            self.status.setText(f"On air  ·  {self._sent} fragments sent"
                                + (f"\n{self._last_error}" if self._last_error else ""))
            if not self.isVisible():
                return  # on the RX view: nobody sees the preview
            rgb = self._preview.read()
            if rgb is not None:
                self.preview.set_image(_fit_qimage(rgb, self.preview.width(), self.preview.height()))

    def shutdown(self):
        # the full stop(): engine.stop() alone left the framer's video/audio
        # sources running when the GUI was closed while on air
        if self.is_running():
            self.stop()
        else:
            self.engine.stop()


def _fit_qimage(rgb, tw, th):
    """RGB frame -> QImage scaled to fit tw x th. With OpenCV when it's
    there: Qt's smooth scale on the GUI thread costs ~7 ms a frame on a Pi 4."""
    h, w, _ = rgb.shape
    s = min(tw / w, th / h) if tw > 0 and th > 0 else 1.0
    nw, nh = max(1, int(w * s)), max(1, int(h * s))
    if _cv2 is not None and (nw, nh) != (w, h):
        rgb = _cv2.resize(rgb, (nw, nh), interpolation=_cv2.INTER_LINEAR)
        w, h = nw, nh
    rgb = np.ascontiguousarray(rgb)
    return QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()


def _valid_res(res):
    """A saved resolution no longer offered (the list is capped) -> the largest that is."""
    return res if res in media_tx_gui.WAVELET_RESOLUTIONS else media_tx_gui.WAVELET_RESOLUTIONS[-1]


def s_get(settings, key, default):
    v = settings.get(key, default)
    return type(default)(v) if v is not None else default
