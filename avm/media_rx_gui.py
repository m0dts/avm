#!/usr/bin/env python3
"""
Combined GUI front-end for the RX media chain: video, live spectrum, and
the link/RF/playback control form all in ONE window -- the RX-side
counterpart to media_tx_gui.py.

hf_ofdm_rx.py still runs as a background subprocess (its PlutoSDR I/O and
FEC decode are complex, carefully-tuned real-time code -- reimplementing
that here would just risk regressing it for no benefit). What changed
from the old three-window version: this process no longer launches
media_rx_player.py as a SECOND subprocess piped after the first --
instead it reads hf_ofdm_rx.py's stdout pipe directly and runs
media_rx_player's own read/decode/playback loop (run_player_loop) on a
background thread INSIDE this process, with a Qt-native video widget in
place of media_rx_player's standalone cv2 window. Likewise, hf_ofdm_rx.py
runs with --spectrum-stderr instead of --gui: it prints periodic spectrum
snapshots to its stderr (already piped here) instead of opening its own
pyqtgraph window, and this window draws them in its own embedded plot.

Known limitation vs. the old separate hf_ofdm_rx.py --gui window: the
live RX-gain SLIDER is gone (there's no channel back into the rx
subprocess to change its gain on the fly). --rx-gain in the form still
sets the STARTING gain, same as before.

Usage:
    python media_rx_gui.py
"""
import os
import re
import shlex
import subprocess
import sys
import threading
import time

import numpy as np
import pyqtgraph as pg
from PyQt5 import QtCore, QtGui, QtWidgets

import avm_version
import gui_layout
import hf_ofdm_common as ofdm
import media_rx_player

try:
    import cv2 as _cv2  # optional: fast frame scaling in _video_frame_sink
except ImportError:
    _cv2 = None

PYTHON = sys.executable
# Audio output picked by default when present: the Raspberry Pi's "tv" ALSA
# alias (~/.asoundrc) for HDMI1 audio.
PREFERRED_AUDIO_DEVICE = "tv"
# Inherited by every child process: one BLAS thread each (see hf_ofdm_tx.py --
# OpenBLAS's idle worker threads busy-wait and cost ~1 core on a Pi 4).
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

VIDEO_ASPECT_W, VIDEO_ASPECT_H = 16, 9


class AspectRatioVideoLabel(QtWidgets.QLabel):
    """Displays whatever frame it's given letterboxed/pillarboxed to fit
    its own current size while preserving VIDEO_ASPECT_W:VIDEO_ASPECT_H
    -- never stretched to fill an arbitrary rectangle (setScaledContents
    would do that, distorting anything that isn't already exactly that
    shape). The pane's OWN minimum size is also kept at that ratio (see
    heightForWidth/hasHeightForWidth below) so the space it's given tends
    to already be 16:9-shaped, with the frame-level letterboxing as the
    fallback for whatever mismatch remains (e.g. a source encoded at a
    different resolution/ratio than the transmit side was configured
    for, or the splitter simply not landing on an exact 16:9 rect)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._raw_pixmap = None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return int(width * VIDEO_ASPECT_H / VIDEO_ASPECT_W)

    # The label's size as a plain tuple, readable from the video worker
    # thread, which pre-scales each frame to it (see _video_frame_sink).
    target_size = (0, 0)

    def set_frame(self, pixmap):
        self._raw_pixmap = pixmap
        self._rescale()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.target_size = (self.width(), self.height())
        self._rescale()

    def _rescale(self):
        if self._raw_pixmap is None or self._raw_pixmap.isNull():
            self.clear()  # drops any previously-displayed frame instead of leaving it stuck on screen
            return
        pw, ph = self._raw_pixmap.width(), self._raw_pixmap.height()
        if pw <= self.width() and ph <= self.height() and (self.width() - pw <= 2 or self.height() - ph <= 2):
            # Already scaled to fit by the worker thread -- show as is.
            super().setPixmap(self._raw_pixmap)
            return
        scaled = self._raw_pixmap.scaled(self.size(), QtCore.Qt.KeepAspectRatio,
                                          QtCore.Qt.SmoothTransformation)
        super().setPixmap(scaled)


def list_audio_output_devices():
    """Enumerates audio OUTPUT devices via sounddevice (the same library
    media_rx_player.py itself plays through), for the GUI's device
    dropdown -- returns [(index, name), ...], only devices with at least
    one output channel. Imported lazily (only when this GUI runs, not by
    every script that happens to import this module) since sounddevice
    touches real audio hardware/drivers on load."""
    import sounddevice as sd
    devices = []
    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev.get("max_output_channels", 0) > 0:
                devices.append((i, dev["name"]))
    except Exception:
        pass
    return devices


class LogStream(QtCore.QObject):
    line_received = QtCore.pyqtSignal(str)


class VideoSignal(QtCore.QObject):
    """A frame decoded on media_rx_player's VideoWorker thread has to
    reach the Qt widget on the MAIN thread -- Qt signals/slots are the
    standard, thread-safe way to hand it across (an AutoConnection
    promotes to a queued one automatically when emitter and receiver
    live on different threads, exactly like this file's existing
    pump_stderr -> log_stream.line_received pattern already relies on)."""
    frame_ready = QtCore.pyqtSignal(QtGui.QImage)
    config_ready = QtCore.pyqtSignal(int, int)


class SpectrumSignal(QtCore.QObject):
    data_ready = QtCore.pyqtSignal(object, object)  # (freqs: np.ndarray, mag_db: np.ndarray)


def pump_stderr(proc, tag, log_stream, spectrum_signal=None, spectrum_re=None):
    """See media_tx_gui.py's identical function -- runs on its own thread
    per process since stderr is a blocking pipe. When spectrum_signal is
    given, lines matching spectrum_re (hf_ofdm_rx.py's --spectrum-stderr
    output -- see its own docstring for the format) are decoded and
    routed there instead of the log, so the log doesn't get spammed with
    a multi-KB base64 blob ~7x/second."""
    import base64
    for raw in iter(proc.stderr.readline, b""):
        try:
            text = raw.decode(errors="replace").rstrip()
        except Exception:
            continue
        if not text:
            continue
        if spectrum_signal is not None:
            m = spectrum_re.match(text)
            if m:
                try:
                    freq0, freq_step, n, b64 = m.groups()
                    mag_db = np.frombuffer(base64.b64decode(b64), dtype="<f4")
                    freqs = float(freq0) + np.arange(int(n)) * float(freq_step)
                    spectrum_signal.data_ready.emit(freqs, mag_db)
                except Exception:
                    pass  # a torn/partial line from a mid-write read -- just skip it, next one's fine
                continue
        log_stream.line_received.emit(f"[{tag}] {text}")


class MediaRxWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{avm_version.SHORT_TITLE} — RX")
        self.procs = []
        self.threads = []
        self._player_thread = None
        self._video = None
        self._audio_worker = None
        self._record = None
        self.log_stream = LogStream()
        self.log_stream.line_received.connect(self._append_log)
        # Batches log-widget text updates instead of one appendPlainText()
        # call per incoming line -- confirmed for real this matters: a
        # healthy session still prints ~3 log lines per fragment (roughly
        # 12+/s), each a synchronous Qt widget mutation on the SAME main
        # thread that also has to paint each decoded video frame (queued
        # there via video_signal.frame_ready). Video's own decode/delivery
        # counters (see _update_video_stats) can look perfectly smooth
        # the whole time -- they're updated on VideoWorker's own thread at
        # decode time, not when a frame actually reaches the screen --
        # while the PICTURE still visibly stalls, because the main thread
        # is busy working through a backlog of log appends before it gets
        # to the queued paint. Coalescing many lines into one widget
        # update every ~150ms (matching the spectrum plot's own refresh
        # rate) cuts the number of separate widget mutations competing
        # with video painting by an order of magnitude, without changing
        # what ends up in the log itself. The regex-driven station-ID/
        # frag-stat parsing in _append_log is unaffected -- that's cheap,
        # plain-Python work, not a Qt widget operation, and still runs
        # immediately per line.
        self._log_pending_lines = []
        self._log_flush_timer = QtCore.QTimer()
        self._log_flush_timer.timeout.connect(self._flush_log_buffer)
        self._log_flush_timer.start(150)
        self.video_signal = VideoSignal()
        self.video_signal.frame_ready.connect(self._display_video_frame)
        self.video_signal.config_ready.connect(self._on_video_config)
        self.spectrum_signal = SpectrumSignal()
        self.spectrum_signal.data_ready.connect(self._display_spectrum)
        self._spectrum_line_re = re.compile(r"^\[spectrum\] (\S+) (\S+) (\d+) (\S+)$")

        # Deliberately matches ONLY "seq=X: OK" -- a bare "Fragment seq=X:"
        # prefix (no status check) also matches hf_ofdm_rx.py's "CRC
        # MISMATCH" lines, which silently counted null-padded (corrupted,
        # audibly-glitchy) fragments as "received OK" here, hiding real
        # content loss the sequence-gap counter alone can't see (a
        # CRC-mismatched fragment still has the RIGHT seq number, so it
        # never trips the gap check below either).
        self._frag_re = re.compile(r"^\[rx\] Fragment seq=(\d+): OK\b")
        self._frag_crc_re = re.compile(r"^\[rx\] Fragment seq=(\d+): CRC MISMATCH\b")
        self._gap_re = re.compile(r"^\[rx\]\s*! sequence gap: expected seq=(\d+), got seq=(\d+) "
                                   r"\((\d+) fragment\(s\) lost -- no return channel")
        # media_rx_player's run_player_loop runs in THIS process now, not
        # a separate subprocess, but its _log() calls are still routed
        # through log_stream tagged "[player]" (see start()) so these
        # regexes -- and everything downstream of them -- are unchanged.
        self._station_id_re = re.compile(r"^\[player\] Station ID: '(.*)'$")
        self._station_id_tentative_re = re.compile(
            r"^\[player\] Station ID: '(.*)' \(unconfirmed -- CRC not yet valid\)$")
        self._station_id_reset_re = re.compile(r"^\[player\] Station ID: signal gap of")
        self._reset_frag_stats()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # Left side: video (top) + spectrum (bottom), stacked -- the two
        # "live view" widgets. Right side: the scrollable control form,
        # unchanged in content from the old three-window version.
        left = QtWidgets.QSplitter(QtCore.Qt.Vertical)

        video_container = QtWidgets.QWidget()
        video_container_layout = QtWidgets.QVBoxLayout(video_container)
        video_container_layout.setContentsMargins(0, 0, 0, 0)
        self.video_label = AspectRatioVideoLabel("(no video)")
        self.video_label.setAlignment(QtCore.Qt.AlignCenter)
        self.video_label.setMinimumSize(320, 320 * VIDEO_ASPECT_H // VIDEO_ASPECT_W)
        self.video_label.setStyleSheet("background-color: #111; color: #888;")
        video_container_layout.addWidget(self.video_label)
        self.video_stats_label = QtWidgets.QLabel("Video: --")
        self.video_stats_label.setStyleSheet("color: #888;")
        # Its text length changes every second (and grows a warning when fps
        # drops): never let that resize the layout -- it's clipped to
        # whatever width the video pane has, one line high, with the full
        # text in the tooltip.
        self.video_stats_label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.video_stats_label.setWordWrap(False)
        video_container_layout.addWidget(self.video_stats_label)
        left.addWidget(video_container)

        # Polls VideoWorker's own counters once a second -- see
        # _update_video_stats and _boost_current_thread_priority's
        # docstring (media_rx_player.py) for the burst-stutter failure
        # mode this is meant to make visible: decode can be running
        # smoothly (steady OK fps) or falling behind (rising "dropped"
        # count, queue depth pinned near its cap) even while the RX link
        # itself is decoding every fragment cleanly, since video sharing
        # this process with the GUI/audio is a SEPARATE bottleneck from
        # the OFDM link.
        self._video_stats_prev_decoded = 0
        self._video_stats_prev_received = 0
        self._video_stats_tick = 0
        self._video_stats_timer = QtCore.QTimer()
        self._video_stats_timer.timeout.connect(self._update_video_stats)
        self._video_stats_timer.start(1000)

        spectrum_container = QtWidgets.QWidget()
        spectrum_layout = QtWidgets.QVBoxLayout(spectrum_container)
        spectrum_layout.setContentsMargins(0, 0, 0, 0)
        self.spectrum_plot = pg.PlotWidget()
        self.spectrum_plot.setLabel("bottom", "Frequency", units="Hz")
        self.spectrum_plot.showGrid(x=True, y=True, alpha=0.3)
        self.spectrum_curve = self.spectrum_plot.plot(pen="y")
        self.spectrum_plot.enableAutoRange(axis="y", enable=False)
        self.spectrum_plot.setMouseEnabled(x=True, y=False)
        spectrum_layout.addWidget(self.spectrum_plot)

        ref_row = QtWidgets.QWidget()
        ref_layout = QtWidgets.QHBoxLayout(ref_row)
        ref_layout.setContentsMargins(0, 0, 0, 0)
        ref_layout.addWidget(QtWidgets.QLabel("Reference level (top of screen):"))
        self.spectrum_ref_spin = QtWidgets.QDoubleSpinBox()
        self.spectrum_ref_spin.setRange(-200.0, 200.0)
        self.spectrum_ref_spin.setDecimals(1)
        self.spectrum_ref_spin.setSuffix(" dB")
        self.spectrum_ref_spin.setValue(-25.0)
        self.spectrum_ref_spin.valueChanged.connect(self._apply_spectrum_ref_level)
        ref_layout.addWidget(self.spectrum_ref_spin)
        ref_layout.addStretch(1)
        spectrum_layout.addWidget(ref_row)
        left.addWidget(spectrum_container)
        left.setStretchFactor(0, 2)
        left.setStretchFactor(1, 1)
        root.addWidget(left, stretch=3)

        form_container = QtWidgets.QWidget()
        form_scroll = QtWidgets.QScrollArea()
        form_scroll.setWidget(form_container)
        form_scroll.setWidgetResizable(True)
        form_scroll.setMinimumWidth(340 if gui_layout.is_compact() else 420)
        root.addWidget(form_scroll, stretch=2)
        form = QtWidgets.QFormLayout(form_container)
        gui_layout.tighten_form(form)

        form.addRow(QtWidgets.QLabel("--- link/RF settings (must match the transmitter) ---"))

        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["A", "B", "C", "D", "VU"])  # VU: VHF/UHF mobile + tropo (not DRM)
        form.addRow("Mode", self.mode)

        self.occupancy = QtWidgets.QComboBox()
        self.occupancy.setEditable(True)
        self.occupancy.addItems(["80", "20", "40", "160", "250"])
        form.addRow("Occupancy (kHz)", self.occupancy)

        self.modulation = QtWidgets.QComboBox()
        self.modulation.addItems(["qpsk", "16qam"])
        form.addRow("Modulation", self.modulation)

        self.fec = QtWidgets.QComboBox()
        self.fec.addItems(["auto", "viterbi", "ldpc"])
        form.addRow("FEC", self.fec)

        self.fragment_size = QtWidgets.QSpinBox()
        self.fragment_size.setRange(64, 65536)
        self.fragment_size.setValue(1024)
        form.addRow("Fragment size (bytes)", self.fragment_size)

        self.fragment_gap_ms = QtWidgets.QDoubleSpinBox()
        self.fragment_gap_ms.setRange(0, 5000)
        self.fragment_gap_ms.setValue(0)
        form.addRow("Fragment gap (ms)", self.fragment_gap_ms)

        self.rx_queue_depth = QtWidgets.QSpinBox()
        self.rx_queue_depth.setRange(1, 64)
        self.rx_queue_depth.setValue(1)  # was 2; 1 saves ~0.24s -- raise if fragments start dropping
        form.addRow("RX queue depth (fragments)", self.rx_queue_depth)

        self.drop_if_slow = QtWidgets.QCheckBox("Drop fragment if slower than realtime (--drop-if-slow)")
        self.drop_if_slow.setChecked(True)
        self.drop_if_slow.setToolTip("If a fragment's own decode took longer than its real-time "
                                      "budget, discard it instead of writing it out late -- keeps "
                                      "the player caught up to live instead of accumulating a "
                                      "growing lag. Trades that one fragment's content (a brief "
                                      "glitch) for staying in real time. Checked by default since "
                                      "this GUI is for a live stream, not a one-shot transfer where "
                                      "every byte matters.")
        form.addRow(self.drop_if_slow)

        self.link_capacity_label = QtWidgets.QLabel("")
        form.addRow("Link capacity", self.link_capacity_label)
        for widget, signal_name in (
            (self.mode, "currentTextChanged"), (self.occupancy, "currentTextChanged"),
            (self.occupancy, "editTextChanged"), (self.modulation, "currentTextChanged"),
            (self.fec, "currentTextChanged"),
            (self.fragment_size, "valueChanged"), (self.fragment_gap_ms, "valueChanged"),
        ):
            getattr(widget, signal_name).connect(self._update_link_capacity_label)

        form.addRow(QtWidgets.QLabel("--- PlutoSDR settings ---"))

        self.input_mode = QtWidgets.QComboBox()
        self.input_mode.addItems(["pluto", "pipe"])
        self.input_mode.currentTextChanged.connect(self._update_input_fields)
        form.addRow("Input", self.input_mode)

        self.rf_freq = QtWidgets.QLineEdit("145500000")
        self.rf_freq.setPlaceholderText("Hz, required for --input pluto")
        form.addRow("RF freq", self.rf_freq)

        self.rx_agc = QtWidgets.QCheckBox("Use AGC (hardware-controlled gain)")
        self.rx_agc.stateChanged.connect(lambda _: self.rx_gain.setEnabled(not self.rx_agc.isChecked()))
        form.addRow(self.rx_agc)

        self.rx_gain = QtWidgets.QDoubleSpinBox()
        self.rx_gain.setRange(0, 73)
        self.rx_gain.setValue(35)
        form.addRow("RX gain (dB, at start -- no live slider in this combined view)", self.rx_gain)

        self.pluto_sample_rate = QtWidgets.QComboBox()
        self.pluto_sample_rate.setEditable(True)
        self.pluto_sample_rate.addItems(["550000", "(mode native rate)", "1000000", "2000000"])
        self.pluto_sample_rate.setToolTip("--sample-rate: must match the transmitter's Pluto sample "
                                           "rate exactly.")
        form.addRow("Pluto sample rate (Hz)", self.pluto_sample_rate)

        self.lo_offset_hz = QtWidgets.QDoubleSpinBox()
        self.lo_offset_hz.setRange(0, 400000)
        self.lo_offset_hz.setValue(100000)
        self.lo_offset_hz.setToolTip("--lo-offset-hz: must match the transmitter's exactly.")
        form.addRow("LO offset (Hz)", self.lo_offset_hz)

        self.pluto_uri = QtWidgets.QLineEdit("ip:192.168.2.1")
        self.pluto_uri.setPlaceholderText("(auto)")
        form.addRow("Pluto URI", self.pluto_uri)

        self.show_spectrum = QtWidgets.QCheckBox("Compute live spectrum (--spectrum-stderr)")
        self.show_spectrum.setChecked(True)
        form.addRow(self.show_spectrum)

        self.spectrum_averages = QtWidgets.QSpinBox()
        self.spectrum_averages.setRange(1, 64)
        self.spectrum_averages.setValue(8)
        self.spectrum_averages.setToolTip(
            "--spectrum-averages: number of FFT frames averaged for the spectrum display. "
            "Higher = smoother trace, slower response; lower = noisier but more reactive.")
        form.addRow("Spectrum averages", self.spectrum_averages)

        self.spectrum_db_per_div = QtWidgets.QDoubleSpinBox()
        self.spectrum_db_per_div.setRange(0.5, 20.0)
        self.spectrum_db_per_div.setSingleStep(0.5)
        self.spectrum_db_per_div.setValue(2.5)
        self.spectrum_db_per_div.setSuffix(" dB/div")
        form.addRow("Spectrum vertical scale", self.spectrum_db_per_div)

        self.input_file = QtWidgets.QLineEdit()
        self.input_file.setPlaceholderText("(only used for --input pipe -- a file path to read IQ from, "
                                            "blank = stdin)")
        form.addRow("Pipe input file", self.input_file)

        self.station_id_label = QtWidgets.QLabel("(none received yet)")
        form.addRow("Station ID", self.station_id_label)

        form.addRow(QtWidgets.QLabel("--- playback ---"))

        self.enable_video = QtWidgets.QCheckBox("Display video")
        self.enable_video.setChecked(True)
        form.addRow(self.enable_video)

        self.audio_device = QtWidgets.QComboBox()
        self.audio_device.setEditable(True)
        self.audio_device.addItem("(system default)", "")
        form.addRow("Audio output device", self.audio_device)

        refresh_audio_btn = QtWidgets.QPushButton("Refresh audio output devices")
        refresh_audio_btn.clicked.connect(self._refresh_audio_devices)
        form.addRow(refresh_audio_btn)

        self.audio_latency = QtWidgets.QDoubleSpinBox()
        self.audio_latency.setRange(0.05, 5.0)
        self.audio_latency.setSingleStep(0.05)
        # 0.5s, then 1.5s, both repeatedly proved too thin for real
        # sessions -- confirmed for real (this session) that the actual
        # cause isn't lost/starved audio at all: every single test run,
        # including ones that still BROKE, reported 0 lost fragments and
        # 0 CRC-corrupted fragments at the OFDM layer, which rules out
        # data loss anywhere in the pipeline. What's actually happening
        # is accumulated LATENCY DRIFT -- hf_ofdm_rx.py's own decode
        # compute time regularly running right at (occasionally just
        # over) its 0.24s per-fragment budget (see its "waits=9,
        # Time=0.27s" log lines), plus media_tx_framer.py's assembler
        # tick scheduling under real system load (competing with
        # hf_ofdm_tx.py's own CPU-heavy OFDM modulation, which an
        # isolated synthetic test never has to contend with) -- neither
        # loses a single byte, but each shaves a little real time off
        # whatever buffer margin exists, and with only ~1.5s of slack
        # that adds up to a BREAK over a long enough session even on a
        # perfectly clean RF link (0.0000 BER, ~14% EVM). Since nothing
        # is being lost, buffering is the right tool for this (absorbing
        # jitter) rather than more packing/scheduling changes -- 3.0s
        # gives meaningfully more headroom than 1.5s did, at the cost of
        # that much extra end-to-end delay, a non-issue for a one-way
        # broadcast-style link like this one.
        #
        # 0.25s since media_source_audio.py's Ogg bursts were fixed with
        # -page_duration (confirmed working on air with Codec2 + wavelet
        # video) -- raise it again if BREAKs return.
        #
        # 0.2s: chosen on the Pi once the C920 mic capture was fixed (see
        # media_source.py's pulse input). hf_ofdm_rx.py's --drop-if-slow
        # keeps a late fragment unless decoding is >1s behind, so a single
        # slow decode can still break audio at this setting -- raise it if
        # BREAK lines show up in the log.
        self.audio_latency.setValue(0.2)
        form.addRow("Audio output latency (s)", self.audio_latency)

        self.show_stats = QtWidgets.QCheckBox("Show throughput stats")
        form.addRow(self.show_stats)

        self.record_enabled = QtWidgets.QCheckBox("Record received AUDIO to file (video not recorded yet)")
        form.addRow(self.record_enabled)

        record_row = QtWidgets.QHBoxLayout()
        self.record_path = QtWidgets.QLineEdit()
        self.record_path.setPlaceholderText("output.mkv")
        record_browse_btn = QtWidgets.QPushButton("Browse...")
        record_browse_btn.clicked.connect(self._browse_record_path)
        record_row.addWidget(self.record_path)
        record_row.addWidget(record_browse_btn)
        form.addRow("Recording file", record_row)

        btn_row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        form.addRow(btn_row)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(120 if gui_layout.is_compact() else 220)
        # Unbounded otherwise -- QPlainTextEdit.appendPlainText() gets
        # progressively slower as its document grows, and a stuck RX
        # (failing to reacquire sync) prints a FAILED line per retry with
        # nothing to throttle it the way a successful decode's own real-
        # time fragment arrival does. Confirmed for real this can make
        # the whole GUI event loop appear completely frozen -- not a
        # backend hang at all, just this widget falling further and
        # further behind its own unbounded backlog. A fixed cap keeps
        # each append cheap regardless of how long a bad run keeps going.
        self.log.setMaximumBlockCount(5000)
        form.addRow(self.log)

        self._update_input_fields(self.input_mode.currentText())
        self._apply_spectrum_ref_level(self.spectrum_ref_spin.value())
        self.resize(1200, 820)
        gui_layout.place(self, gui_layout.TX_WIDTH_FRACTION, 1.0 - gui_layout.TX_WIDTH_FRACTION)
        self._update_link_capacity_label()
        self._refresh_audio_devices()

    # ------------------------------------------------------------------
    # Spectrum widget
    # ------------------------------------------------------------------

    def _apply_spectrum_ref_level(self, top_db):
        div = self.spectrum_db_per_div.value()
        self.spectrum_plot.setYRange(top_db - 10 * div, top_db, padding=0)
        self.spectrum_plot.setLabel("left", f"Magnitude ({div:g} dB/div)", units="dB")

    def _display_spectrum(self, freqs, mag_db):
        self.spectrum_curve.setData(freqs, mag_db)

    # ------------------------------------------------------------------
    # Video widget
    # ------------------------------------------------------------------

    def _update_video_stats(self):
        video = self._video
        if video is None:
            self.video_stats_label.setText("Video: --")
            self._video_stats_prev_decoded = 0
            self._video_stats_prev_received = 0
            return
        decoded = video.frames_decoded
        received = video.packets_received
        # Both deltas over the timer's own 1000ms interval, so each delta
        # IS the fps directly. Comparing the two tells you which side of
        # this thread the bottleneck is actually on: if recv and decoded
        # track together (both low/variable), packets simply aren't
        # ARRIVING any faster -- the source encoder, media_tx_framer.py's
        # audio/video packing, or the OFDM link itself, not this process.
        # If recv stays near the configured target while decoded lags
        # behind it, the problem is genuinely in decode/pacing here.
        decoded_fps = decoded - self._video_stats_prev_decoded
        received_fps = received - self._video_stats_prev_received
        self._video_stats_prev_decoded = decoded
        self._video_stats_prev_received = received
        depth = video.queue_depth()
        note, note_detail = "", ""
        if depth >= media_rx_player.VIDEO_QUEUE_DEPTH - 1:
            note, note_detail = "  QUEUE FULL", "decode queue full, falling behind (a LOCAL bottleneck)"
        elif decoded_fps == 0 and decoded > 0:
            note, note_detail = "  STALLED", "no frames decoded in the last second"
        elif video.configured_fps and received_fps < 0.7 * video.configured_fps:
            note, note_detail = "  SLOW", ("packets arriving slower than the source's own configured "
                                           "rate -- check TX/link")
        target = f"{video.configured_fps:g}" if video.configured_fps else "?"
        # Short labels so it usually fits on one line; the tooltip has the
        # spelled-out version.
        text = (f"Video: {decoded_fps}/{received_fps}/{target} fps (shown/recv/target)  |  "
                f"queue {depth}/{media_rx_player.VIDEO_QUEUE_DEPTH}  |  "
                f"{video.frames_dropped_queue_full} dropped  |  "
                f"{video.packets_dropped_decode_error} errors{note}")
        self.video_stats_label.setText(text)
        self.video_stats_label.setToolTip(
            f"{decoded_fps} decoded fps, {received_fps} received fps, target {target} fps\n"
            f"decode queue {depth}/{media_rx_player.VIDEO_QUEUE_DEPTH}\n"
            f"{video.frames_dropped_queue_full} frames dropped because the queue was full\n"
            f"{video.packets_dropped_decode_error} decode errors"
            + (f"\n{note_detail}" if note_detail else ""))
        # The live label updates every second and is easy to miss/too
        # fast to screenshot -- also written into the persistent, scrollable
        # log (throttled to every 2nd tick, i.e. ~2s, matching
        # media_tx_framer.py's own reporting cadence) so a run can be
        # reviewed or copied/pasted afterward instead of needing to catch
        # it live. Tagged "[video-stats]" -- doesn't match any of
        # _append_log's other regexes, so it's just appended as plain text.
        self._video_stats_tick += 1
        if self._video_stats_tick % 2 == 0:
            self._append_log(f"[video-stats] {text}" + (f" -- {note_detail}" if note_detail else ""))

    def _on_video_config(self, width, height):
        self.video_label.setText("")
        # The pane itself stays a fixed 16:9 shape regardless of the
        # source's own encoded resolution (which is typically much
        # smaller, e.g. 160x90) -- AspectRatioVideoLabel scales each
        # frame up to fill it while preserving proportions, rather than
        # sizing the display area down to the source's tiny native pixels.

    def _display_video_frame(self, qimg):
        self.video_label.set_frame(QtGui.QPixmap.fromImage(qimg))

    def _video_frame_sink(self, img_bgr):
        """Runs on media_rx_player.VideoWorker's OWN thread -- must not
        touch any Qt widget directly (see VideoSignal's docstring).
        Builds the QImage here (cheap, no GUI object involved) and hands
        it across via a queued signal for the main thread to display."""
        tw, th = self.video_label.target_size
        if _cv2 is not None and tw > 0 and th > 0:
            # Scale to the display size here, on this worker thread, with
            # OpenCV, straight into Qt's native 32-bit layout (BGRA in memory
            # = Format_RGB32): the GUI thread then only has to show it. Qt's
            # smooth scale on the GUI thread was ~7 ms/frame on a Pi 4.
            h, w = img_bgr.shape[:2]
            s = min(tw / w, th / h)
            nw, nh = max(1, int(w * s)), max(1, int(h * s))
            bgra = _cv2.cvtColor(img_bgr, _cv2.COLOR_BGR2BGRA)
            if (nw, nh) != (w, h):
                bgra = _cv2.resize(bgra, (nw, nh), interpolation=_cv2.INTER_LINEAR)
            qimg = QtGui.QImage(bgra.data, nw, nh, 4 * nw, QtGui.QImage.Format_RGB32).copy()
        else:
            rgb = np.ascontiguousarray(img_bgr[:, :, ::-1])  # BGR (PyAV/cv2 convention) -> RGB
            h, w, _ch = rgb.shape
            qimg = QtGui.QImage(rgb.data, w, h, 3 * w, QtGui.QImage.Format_RGB888).copy()
        self.video_signal.frame_ready.emit(qimg)

    def _video_config_sink(self, width, height):
        self.video_signal.config_ready.emit(width, height)

    # ------------------------------------------------------------------
    # Ordinary control-form plumbing (unchanged from the old version)
    # ------------------------------------------------------------------

    def _refresh_audio_devices(self):
        try:
            devices = list_audio_output_devices()
        except Exception as e:
            self._append_log(f"[error] listing audio devices: {e}")
            return
        current = self.audio_device.currentText()
        self.audio_device.clear()
        self.audio_device.addItem("(system default)", "")
        for index, name in devices:
            self.audio_device.addItem(f"{index}: {name}", str(index))
        if current == "(system default)":
            # Prefer PREFERRED_AUDIO_DEVICE when it exists (on the Pi, the
            # ALSA alias that reaches the HDMI TV -- PipeWire's own default
            # can't drive the Pi's HDMI audio); a device picked by hand is
            # kept as before.
            for index, name in devices:
                if name == PREFERRED_AUDIO_DEVICE:
                    current = f"{index}: {name}"
                    break
        idx = self.audio_device.findText(current)
        if idx >= 0:
            self.audio_device.setCurrentIndex(idx)
        else:
            self.audio_device.setEditText(current)
        self._append_log(f"[gui] found {len(devices)} audio output device(s).")

    def _update_input_fields(self, mode):
        is_pluto = mode == "pluto"
        for w in (self.rf_freq, self.rx_agc, self.rx_gain, self.pluto_sample_rate,
                  self.lo_offset_hz, self.pluto_uri):
            w.setEnabled(is_pluto)
        self.input_file.setEnabled(not is_pluto)
        if is_pluto:
            self.rx_gain.setEnabled(not self.rx_agc.isChecked())

    def _update_link_capacity_label(self):
        try:
            occupancy = float(self.occupancy.currentText())
            fec_choice = "viterbi" if self.fec.currentText() == "auto" else self.fec.currentText()
            cfg = ofdm.build_config(self.mode.currentText(), occupancy,
                                     data_modulation=self.modulation.currentText(),
                                     fec_scheme=fec_choice)
            total_bps = ofdm.estimate_effective_bitrate(cfg, self.fragment_size.value(),
                                                          fragment_gap_ms=self.fragment_gap_ms.value())
        except Exception as e:
            self.link_capacity_label.setText(f"(couldn't compute: {e})")
            return
        self.link_capacity_label.setText(f"~{total_bps/1000:.1f}kbps effective")

    def _reset_frag_stats(self):
        self._frag_first_seq = None
        self._frag_last_seq = None
        self._frag_ok_count = 0
        self._frag_crc_count = 0
        self._frag_lost_count = 0

    def _flush_log_buffer(self):
        if not self._log_pending_lines:
            return
        self.log.appendPlainText("\n".join(self._log_pending_lines))
        self._write_log_file(self._log_pending_lines)
        self._log_pending_lines = []

    def _write_log_file(self, lines):
        """Also appends the log to the file named by HF_RX_GUI_LOG, if set --
        the widget only keeps the most recent lines, which isn't enough to
        look back at what happened around an occasional break."""
        path = os.environ.get("HF_RX_GUI_LOG")
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                stamp = time.strftime("%H:%M:%S")
                f.write("".join(f"{stamp} {line}\n" for line in lines))
        except OSError:
            pass

    def _append_log(self, text):
        self._log_pending_lines.append(text)
        if self._station_id_reset_re.match(text):
            self.station_id_label.setText("(none received yet)")
            self.station_id_label.setStyleSheet("")
            return
        m = self._station_id_tentative_re.match(text)
        if m:
            self.station_id_label.setText(f"'{m.group(1)}' (unconfirmed)")
            self.station_id_label.setStyleSheet("color: #b70;")
            return
        m = self._station_id_re.match(text)
        if m:
            self.station_id_label.setText(f"'{m.group(1)}'")
            self.station_id_label.setStyleSheet("")
            return
        m = self._frag_re.match(text)
        if m:
            seq = int(m.group(1))
            if self._frag_first_seq is None:
                self._frag_first_seq = seq
            self._frag_last_seq = seq
            self._frag_ok_count += 1
            return
        m = self._frag_crc_re.match(text)
        if m:
            seq = int(m.group(1))
            if self._frag_first_seq is None:
                self._frag_first_seq = seq
            self._frag_last_seq = seq
            self._frag_crc_count += 1
            return
        m = self._gap_re.match(text)
        if m:
            self._frag_lost_count += int(m.group(3))

    def _frag_summary_text(self):
        if self._frag_first_seq is None:
            return "[gui] summary: no fragments were received."
        span = self._frag_last_seq - self._frag_first_seq + 1
        total = self._frag_ok_count + self._frag_crc_count + self._frag_lost_count
        pct = ((self._frag_crc_count + self._frag_lost_count) / total * 100) if total else 0.0
        return (f"[gui] summary: seq {self._frag_first_seq}..{self._frag_last_seq} "
                f"({span} expected) -- {self._frag_ok_count} received OK, "
                f"{self._frag_crc_count} CRC-corrupted (null-padded), "
                f"{self._frag_lost_count} lost -- {pct:.1f}% of content affected")

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self):
        if self.procs:
            return
        try:
            rx_cmd = self._build_rx_cmd()
            player_args = self._build_player_args()
        except ValueError as e:
            self._append_log(f"[error] {e}")
            return

        self._reset_frag_stats()
        self.station_id_label.setText("(none received yet)")
        self.station_id_label.setStyleSheet("")
        self.video_label.set_frame(QtGui.QPixmap())
        self.video_label.setText("(no video)")
        self._append_log(f"[gui] rx: {' '.join(shlex.quote(c) for c in rx_cmd)}")

        rx_stdin = subprocess.PIPE
        self._rx_in_file = None
        if self.input_mode.currentText() == "pipe" and self.input_file.text().strip():
            self._rx_in_file = open(self.input_file.text().strip(), "rb")
            rx_stdin = self._rx_in_file

        # See the old version's identical comment: raising RX's own
        # process priority lets it compete on more even footing once the
        # audio device write thread gets Windows' MMCSS "Pro Audio" boost
        # (that thread now lives in THIS process instead of a separate
        # media_rx_player.py one, but the same OS-level scheduling
        # interaction applies regardless of which process it's in).
        rx_priority = {"creationflags": subprocess.HIGH_PRIORITY_CLASS} if sys.platform == "win32" else {}
        p_rx = subprocess.Popen(rx_cmd, stdin=rx_stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 **rx_priority)

        self.procs = [p_rx]
        t = threading.Thread(target=pump_stderr, args=(p_rx, "rx", self.log_stream,
                                                         self.spectrum_signal, self._spectrum_line_re),
                              daemon=True)
        t.start()
        self.threads.append(t)

        # media_rx_player._log is a MODULE-LEVEL function reference -- see
        # its own docstring. Redirecting it here routes every status line
        # from the in-process player loop (station ID, audio buffer
        # margin, "Playing audio", etc.) into the SAME log widget as the
        # rx subprocess's own stderr, tagged "[player]" to match exactly
        # what the old separate-subprocess version produced, so the
        # regexes above keep working unchanged.
        media_rx_player._log = lambda *a, **k: self.log_stream.line_received.emit(
            "[player] " + " ".join(str(x) for x in a))

        record = None
        if self.record_enabled.isChecked():
            path = self.record_path.text().strip()
            if not path:
                self._append_log("[error] Recording is enabled but no recording file path was given.")
                p_rx.terminate()
                self.procs = []
                return
            import av
            record = {"container": av.open(path, mode="w"), "lock": threading.Lock(),
                       "start_t": time.monotonic(), "video_stream": None}
            self._append_log(f"[gui] Recording audio to {path} (video not recorded)")
        self._record = record

        video = None
        if self.enable_video.isChecked():
            video = media_rx_player.VideoWorker(frame_sink=self._video_frame_sink,
                                                 config_sink=self._video_config_sink, record=record)
        self._video = video

        device_arg = self.audio_device.currentData()
        if not device_arg:
            device_arg = self.audio_device.currentText().strip()
        if not device_arg or device_arg == "(system default)":
            device_arg = None
        elif device_arg.isdigit():
            device_arg = int(device_arg)
        LOW_BUFFER_WARNING_S = 0.2
        audio_worker = media_rx_player.AudioPlaybackWorker(
            self.audio_latency.value(), device_arg, LOW_BUFFER_WARNING_S)
        self._audio_worker = audio_worker

        self._player_thread = threading.Thread(
            target=media_rx_player.run_player_loop,
            args=(p_rx.stdout, player_args, video, audio_worker, record), daemon=True)
        self._player_thread.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._append_log("[gui] started.")

    def stop(self):
        # Terminate the RX process first -- closing its stdout delivers a
        # natural EOF to the in-process player loop's fh.read() calls, so
        # run_player_loop exits through its own normal end-of-stream
        # path (which itself calls audio_worker.stop()/video.stop() and
        # closes any recording container) instead of this method having
        # to duplicate that teardown. Matters for --record-to exactly as
        # it did in the old separate-subprocess version: the graceful
        # path is what finalizes the output container's trailer/index.
        if self.procs:
            rx_proc = self.procs[0]
            if rx_proc.poll() is None:
                rx_proc.terminate()
            if self._player_thread is not None:
                self._player_thread.join(timeout=5)
            try:
                rx_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                rx_proc.kill()
        self.procs = []
        self.threads = []
        self._player_thread = None
        self._video = None
        self._audio_worker = None
        self._record = None
        if getattr(self, "_rx_in_file", None) is not None:
            self._rx_in_file.close()
            self._rx_in_file = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._append_log("[gui] stopped.")
        self._append_log(self._frag_summary_text())
        self._flush_log_buffer()  # show these two lines immediately, not after the next 150ms tick

    def closeEvent(self, event):
        self.stop()
        event.accept()

    def _build_rx_cmd(self):
        cmd = [PYTHON, "hf_ofdm_rx.py",
               "--mode", self.mode.currentText(),
               "--occupancy", self.occupancy.currentText(),
               "--modulation", self.modulation.currentText(),
               "--fec", self.fec.currentText(),
               "--fragment-size", str(self.fragment_size.value()),
               "--fragment-gap-ms", str(self.fragment_gap_ms.value()),
               "--rx-queue-depth", str(self.rx_queue_depth.value()),
               "--input", self.input_mode.currentText()]
        if self.drop_if_slow.isChecked():
            cmd += ["--drop-if-slow"]
        if self.input_mode.currentText() == "pluto":
            if not self.rf_freq.text().strip():
                raise ValueError("RF freq is required for --input pluto.")
            cmd += ["--rf-freq", self.rf_freq.text().strip()]
            if self.rx_agc.isChecked():
                cmd += ["--rx-agc"]
            else:
                cmd += ["--rx-gain", str(self.rx_gain.value())]
            sample_rate_text = self.pluto_sample_rate.currentText().strip()
            if sample_rate_text and not sample_rate_text.startswith("("):
                cmd += ["--sample-rate", sample_rate_text]
            if self.lo_offset_hz.value() > 0:
                cmd += ["--lo-offset-hz", str(self.lo_offset_hz.value())]
            if self.pluto_uri.text().strip():
                cmd += ["--pluto-uri", self.pluto_uri.text().strip()]
            if self.show_spectrum.isChecked():
                cmd += ["--spectrum-stderr", "--spectrum-averages", str(self.spectrum_averages.value())]
        return cmd

    def _build_player_args(self):
        """A tiny stand-in for argparse's Namespace -- run_player_loop
        only ever reads .fragment_size, .stats and .record_to off it
        (see its own source); video/audio_worker/record are passed as
        separate objects, not derived from this."""
        class _Args:
            pass
        a = _Args()
        a.fragment_size = self.fragment_size.value()
        a.stats = self.show_stats.isChecked()
        a.record_to = self.record_path.text().strip() if self.record_enabled.isChecked() else None
        return a

    def _browse_record_path(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Recording file", self.record_path.text().strip() or "output.mkv",
            "Matroska (*.mkv);;All files (*)")
        if path:
            self.record_path.setText(path)


def main():
    gui_layout.prepare_environment()
    if gui_layout.use_opengl():
        pg.setConfigOptions(useOpenGL=True)
    app = QtWidgets.QApplication(sys.argv)
    gui_layout.set_app_id("hfmodem-rx")
    gui_layout.apply_compact_style(app)
    win = MediaRxWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
