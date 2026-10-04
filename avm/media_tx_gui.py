#!/usr/bin/env python3
"""
GUI front-end for the TX media chain (media_tx_framer.py | hf_ofdm_tx.py,
with media_tx_framer.py itself launching media_source_video.py/
media_source_audio.py -- see its own module docstring for why) -- a
form for the settings that were previously several separate command
lines to keep in sync by hand, plus a Start/Stop button.

Chains the two processes directly in Python (Popen with stdout=PIPE
fed straight into the next process's stdin) rather than via a shell
pipe -- this sidesteps the PowerShell native-pipe binary-corruption
issue documented throughout this project entirely, since the bytes
never pass through any shell's pipeline at all, only OS-level pipes
Python hands off directly between the child processes.

Needs PyQt5 (only imported here, not by the scripts it launches).

Usage:
    python media_tx_gui.py
"""
import math
import shlex
import subprocess
import sys
import threading

from PyQt5 import QtCore, QtWidgets

import avm_threads
import avm_version
import gui_layout
import hf_ofdm_common as ofdm
import media_source
import media_tx_framer

PYTHON = sys.executable

# Resolution presets, all 16:9 or as close as each codec allows (the RX
# GUI's video pane is 16:9, so these fill it with little or no letterbox).
H264_RESOLUTIONS = ["160x90", "192x108", "256x144", "320x180", "384x216"]
# The wavelet codec needs width % 32 == 0 and height % 16 == 0, which exact
# 16:9 only meets at 256x144 and 512x288; the rest are the nearest sizes
# that fit (aspect 1.71-1.83 against 16:9's 1.78). Capped at 384x224: larger
# frames cost more Pi 4 CPU than the links can usefully fill.
WAVELET_RESOLUTIONS = ["192x112", "224x128", "256x144", "288x160", "320x176", "352x192",
                       "384x224"]
# Inherited by every child process: one BLAS thread each (see hf_ofdm_tx.py --
# OpenBLAS's idle worker threads busy-wait and cost ~1 core on a Pi 4).
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    __import__("os").environ.setdefault(_var, "1")

# The single source of truth for Opus's packet cadence on the TX side --
# passed explicitly to media_source.py below (never relying on ITS OWN
# default) and used identically in every overhead/packing calculation in
# this file, so the two can never silently diverge again. Confirmed for
# real this exact divergence caused a genuine on-air failure: this file
# used to hardcode a stale 0.06s (60ms) packet interval left over from
# before media_source.py grew a configurable --audio-frame-duration
# (default 20ms) -- three times the packets/sec this file assumed, which
# understated real per-packet TLV header overhead and AUDIO_CONFIG
# resend frequency by the same 3x, letting the calculator suggest a
# bitrate (33.6kbps on a QPSK link) that looked safely under the raw
# effective-bitrate ceiling but wasn't once real framing overhead was
# counted at the ACTUAL packet rate -- a slow, steadily growing playback
# buffer deficit with zero fragments lost or corrupted, since the link
# itself was decoding everything correctly; it just couldn't carry
# audio bytes as fast as they were actually being produced.
AUDIO_FRAME_DURATION_MS = 20.0
AUDIO_PACKET_INTERVAL_S = AUDIO_FRAME_DURATION_MS / 1000
# Codec2's bitrate is fixed by its mode, not a free value like Opus --
# the calculator below needs the REAL number regardless of whatever the
# (disabled, when Codec2 is selected) Opus bitrate spinbox last held.
CODEC2_MODE_KBPS = {"3200": 3.2, "2400": 2.4, "1600": 1.6, "1400": 1.4,
                     "1300": 1.3, "1200": 1.2, "700": 0.7, "700B": 0.7, "700C": 0.7}
# media_tx_framer.py now seeds this (a 1-byte AUDIO_PROFILES ID plus a
# 2-byte current AUDIO_PACKET_FIXED_TYPE slot length -- both needed so a
# receiver that joins mid-session can decode anything at all, not just
# the profile ID -- not the old ~26-byte rate+extradata blob) into every
# block's own budget once, not amortized across AUDIO_CONFIG_REPEAT_EVERY
# packets -- that whole constant is gone from media_tx_framer.py now
# that config is cheap enough to send every fragment outright instead
# of periodically.
AUDIO_PROFILE_CONFIG_BYTES = media_tx_framer.RECORD_HEADER_LEN + 1 + 2  # profile ID + slot length


class LogStream(QtCore.QObject):
    line_received = QtCore.pyqtSignal(str)


def pump_stderr(proc, tag, log_stream):
    """Runs on its own thread per process -- stderr is a blocking pipe, so
    reading it must not happen on the Qt main thread or the GUI would
    freeze waiting for the next line exactly like a live decode loop
    would freeze the UI if run there directly (see hf_ofdm_rx.py's own
    background-thread decode loop for the same reasoning)."""
    for raw in iter(proc.stderr.readline, b""):
        try:
            text = raw.decode(errors="replace").rstrip()
        except Exception:
            continue
        if text:
            log_stream.line_received.emit(f"[{tag}] {text}")


class MediaTxWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{avm_version.SHORT_TITLE} — TX")
        self.procs = []
        self.threads = []
        self.log_stream = LogStream()
        self.log_stream.line_received.connect(self._append_log)

        # Settings scroll; Start/Stop and the log stay below them, always
        # visible, however small the screen (see gui_layout.py).
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        form_container = QtWidgets.QWidget()
        form_scroll = QtWidgets.QScrollArea()
        form_scroll.setWidget(form_container)
        form_scroll.setWidgetResizable(True)
        form_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        outer.addWidget(form_scroll, stretch=5)
        # Short settings share a row (gui_layout.GridForm) so the whole form
        # fits a 1080p screen without scrolling; their full names are in
        # each field's tooltip.
        form = gui_layout.GridForm(form_container)

        # --- media_source.py fields ---
        self.source_combo = QtWidgets.QComboBox()
        self.source_combo.addItems(["test", "device"])
        self.source_combo.currentTextChanged.connect(self._update_source_fields)

        self.video_device = QtWidgets.QComboBox()
        self.video_device.setEditable(True)
        self.video_device.addItem("Integrated Camera" if sys.platform == "win32" else "/dev/video0")
        self.audio_device = QtWidgets.QComboBox()
        self.audio_device.setEditable(True)
        self.audio_device.addItem("Microphone" if sys.platform == "win32" else "default")
        self.tone_hz = QtWidgets.QDoubleSpinBox()
        self.tone_hz.setRange(20, 20000)
        self.tone_hz.setValue(650)
        self.tone_hz.setToolTip("Test tone (Hz)")
        for combo in (self.video_device, self.audio_device):
            # don't let a long device name set the window's minimum width
            combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        form.addPair("Source", self.source_combo, "Tone (Hz)", self.tone_hz)
        form.addRow("Video device", self.video_device)
        form.addRow("Audio device", self.audio_device)

        list_devices_btn = QtWidgets.QPushButton("Refresh device list")
        list_devices_btn.clicked.connect(self._refresh_devices)
        form.addRow(list_devices_btn)

        self.resolution = QtWidgets.QComboBox()
        self.resolution.setEditable(True)
        # 16:9 presets -- matches the combined RX GUI's video pane (see
        # media_rx_gui.py's AspectRatioVideoLabel), so the received
        # picture fills it edge-to-edge with no letterbox bars rather than
        # a 4:3 source getting pillarboxed inside a 16:9 display. Any
        # WxH still works here (the field is editable) -- media_source.py/
        # media_source_video.py's own scale+pad filter letterboxes
        # whatever the source's native aspect ratio is to fit exactly
        # this resolution regardless, so a non-16:9 value just means the
        # RX side will show bars for that mismatch instead of this GUI
        # rejecting it.
        self.resolution.addItems(H264_RESOLUTIONS)

        self.framerate = QtWidgets.QDoubleSpinBox()
        self.framerate.setRange(1, 60)
        self.framerate.setValue(12)
        self.framerate.setToolTip("Framerate (fps)")
        form.addPair("Resolution", self.resolution, "FPS", self.framerate)

        self.video_bitrate = QtWidgets.QDoubleSpinBox()
        self.video_bitrate.setRange(1, 1000)
        self.video_bitrate.setValue(25)
        self.video_bitrate.valueChanged.connect(self._check_video_bitrate)
        self.video_bitrate.setToolTip("Video bitrate (kbps)")

        # _recalculate_bitrates() unconditionally overwrote this field on
        # every recalc (mode/occupancy/fragment-size change, audio codec
        # change, etc.) -- confirmed for real this matters: a deliberate
        # manual test value (e.g. "try 20kbps and see what happens") got
        # silently reset back to the auto-suggested value before the very
        # next session even started, with no visible sign it had happened
        # (the field just showed the auto value with no indication it
        # wasn't what was typed). Checking this box is the only way a
        # typed value actually survives to the next start().
        self.video_bitrate_manual = QtWidgets.QCheckBox("Lock value")
        self.video_bitrate_manual.setToolTip("Lock the video bitrate (don't auto-recalculate)")
        form.addPair("Video kbps", self.video_bitrate, None, self.video_bitrate_manual)

        self.video_bitrate_warning = QtWidgets.QLabel("")
        self.video_bitrate_warning.setStyleSheet("color: #b00;")
        self.video_bitrate_warning.setWordWrap(True)
        form.addRow(self.video_bitrate_warning)

        self.audio_codec = QtWidgets.QComboBox()
        self.audio_codec.addItems(["opus", "codec2"])
        self.audio_codec.currentTextChanged.connect(self._on_audio_codec_changed)

        self.codec2_mode = QtWidgets.QComboBox()
        # Only modes with a verified frame_bytes/pcm_bytes entry in both
        # media_tx_framer.py's and media_rx_player.py's AUDIO_PROFILES
        # tables belong here -- ffmpeg supports more (2400/1600/1400/
        # 1300/1200/700/700B/700C), but those byte sizes haven't been
        # confirmed for real yet, and TX hard-errors on an unlisted mode
        # rather than silently produce something media_rx_player.py
        # can't decode.
        self.codec2_mode.addItems(["3200"])
        self.codec2_mode.setEnabled(False)
        self.codec2_mode.currentTextChanged.connect(self._recalculate_bitrates)
        self.codec2_mode.setToolTip("Codec2 mode (bps)")
        form.addPair("Audio codec", self.audio_codec, "Codec2", self.codec2_mode)

        self.audio_bitrate = QtWidgets.QDoubleSpinBox()
        self.audio_bitrate.setRange(1, 512)
        self.audio_bitrate.setValue(10)
        self.audio_bitrate.valueChanged.connect(self._check_audio_packing)
        self.audio_bitrate.valueChanged.connect(self._check_video_bitrate)  # audio's share affects video's headroom
        self.audio_bitrate.setToolTip("Audio bitrate (kbps)")

        # "wavelet" = media_source_wavelet.py / wavelet_codec.py: every
        # frame exactly bitrate/fps/8 bytes, leaky temporal prediction
        # instead of H.264's reference chain (see wavelet_codec.py).
        self.video_codec = QtWidgets.QComboBox()
        self.video_codec.addItems(["h264", "wavelet"])
        self.video_codec.currentTextChanged.connect(self._update_codec_fields)
        self.video_codec.currentTextChanged.connect(self._recalculate_bitrates)

        self.intra_refresh = QtWidgets.QCheckBox("Intra-refresh")
        self.intra_refresh.setToolTip("Use intra-refresh (recommended, h264 only)")
        self.intra_refresh.setChecked(True)
        form.addPair("Audio kbps", self.audio_bitrate, "Video codec", self.video_codec)

        self.wavelet_leak = QtWidgets.QDoubleSpinBox()
        self.wavelet_leak.setDecimals(3)
        self.wavelet_leak.setRange(0.0, 0.996)
        self.wavelet_leak.setSingleStep(0.01)
        self.wavelet_leak.setValue(0.996)
        self.wavelet_leak.setToolTip("Wavelet leak: maximum temporal prediction strength: higher = better "
                                     "static picture; recovery is bounded by the refresh period. "
                                     "0 = intra-only.")

        self.wavelet_refresh_s = QtWidgets.QDoubleSpinBox()
        self.wavelet_refresh_s.setRange(0.0, 20.0)
        self.wavelet_refresh_s.setSingleStep(0.5)
        self.wavelet_refresh_s.setValue(5.0)
        self.wavelet_refresh_s.setToolTip("Wavelet refresh (s): re-send the whole picture from scratch, "
                                          "spread over every N s (0 = off). Bounds late-join/loss "
                                          "recovery; shorter = faster join, lower quality. Needed with "
                                          "a high leak.")
        form.addPair("Wavelet leak", self.wavelet_leak, "Refresh (s)", self.wavelet_refresh_s)
        self._update_codec_fields()

        self.force_audio_only = QtWidgets.QCheckBox("Audio only")
        self.force_audio_only.setToolTip("Force audio-only (--no-video), ignoring the bitrate calc")
        self.force_audio_only.toggled.connect(self._on_force_audio_only_toggled)

        self.force_video_only = QtWidgets.QCheckBox("Video only")
        self.force_video_only.setToolTip("Force video-only (--no-audio), ignoring the bitrate calc")
        self.force_video_only.toggled.connect(self._on_force_video_only_toggled)

        checks = QtWidgets.QHBoxLayout()
        for box in (self.intra_refresh, self.force_audio_only, self.force_video_only):
            checks.addWidget(box)
        checks.addStretch(1)
        form.addRow(checks)

        self.link_capacity_label = QtWidgets.QLabel("")
        self.link_capacity_label.setWordWrap(True)
        form.addRow("Link capacity", self.link_capacity_label)

        self.audio_packing_warning = QtWidgets.QLabel("")
        self.audio_packing_warning.setStyleSheet("color: #b00;")
        self.audio_packing_warning.setWordWrap(True)
        form.addRow(self.audio_packing_warning)

        self.station_id = QtWidgets.QLineEdit()
        self.station_id.setMaxLength(media_tx_framer.STATION_ID_LEN)
        self.station_id.setPlaceholderText(f"(optional, up to {media_tx_framer.STATION_ID_LEN} ASCII chars, "
                                            f"e.g. a callsign)")
        form.addRow("Station ID", self.station_id)

        form.addRow(QtWidgets.QLabel("--- link/RF settings ---"))

        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["A", "B", "C", "D", "VU"])  # VU: VHF/UHF mobile + tropo (not DRM)

        self.occupancy = QtWidgets.QComboBox()
        self.occupancy.setEditable(True)
        self.occupancy.addItems(["80", "20", "40", "160", "250"])
        self.occupancy.setToolTip("Occupancy (kHz)")
        form.addPair("Mode", self.mode, "kHz", self.occupancy)

        self.modulation = QtWidgets.QComboBox()
        self.modulation.addItems(["qpsk", "16qam"])

        self.fec = QtWidgets.QComboBox()
        self.fec.addItems(["viterbi", "ldpc"])
        self.fec.setCurrentText("ldpc")
        form.addPair("Modulation", self.modulation, "FEC", self.fec)

        self.fragment_size = QtWidgets.QSpinBox()
        self.fragment_size.setRange(64, 65536)
        self.fragment_size.setValue(1024)
        self.fragment_size.setToolTip("Fragment size (bytes)")

        self.fragment_gap_ms = QtWidgets.QDoubleSpinBox()
        self.fragment_gap_ms.setRange(0, 5000)
        self.fragment_gap_ms.setValue(0)
        self.fragment_gap_ms.setToolTip("Fragment gap (ms)")
        form.addPair("Fragment B", self.fragment_size, "Gap ms", self.fragment_gap_ms)

        # Recompute the audio/video bitrate split whenever anything that
        # affects the link's own effective capacity changes -- overwrites
        # any bitrate you've typed by hand, same as any other derived
        # field here (e.g. changing mode also changes what --occupancy
        # values make sense). editTextChanged covers the editable combos'
        # (occupancy) manual typing too, not just picking a preset.
        for widget, signal_name in (
            (self.mode, "currentTextChanged"), (self.occupancy, "currentTextChanged"),
            (self.occupancy, "editTextChanged"), (self.modulation, "currentTextChanged"),
            (self.fec, "currentTextChanged"),
            (self.fragment_size, "valueChanged"), (self.fragment_gap_ms, "valueChanged"),
            # the wavelet bitrate is per-frame bytes x fps, so FPS and the
            # codec matter too
            (self.framerate, "valueChanged"), (self.video_codec, "currentTextChanged"),
        ):
            getattr(widget, signal_name).connect(self._recalculate_bitrates)
            getattr(widget, signal_name).connect(self._check_video_bitrate)

        self.tx_queue_depth = QtWidgets.QSpinBox()
        self.tx_queue_depth.setRange(1, 64)
        self.tx_queue_depth.setValue(2)
        self.tx_queue_depth.setToolTip("TX queue depth (fragments), --tx-queue-depth: how many fragments PlutoTxSink builds ahead "
                                        "of what's actually transmitting. hf_ofdm_tx.py's own default "
                                        "is 8, which alone is several seconds of TX-side latency at a "
                                        "typical fragment duration -- this GUI defaults it lower (2) "
                                        "for less latency. Confirmed for real that 2 has been too low "
                                        "before -- it can starve the TX queue faster than a heavier mode/occupancy's "
                                        "own waveform-build time, filling most of the air with silence "
                                        "instead of real frames -- watch the TX console for a "
                                        "'silence-filled' warning and raise this if you see one.")

        self.trim_warmup_backlog = QtWidgets.QCheckBox("Trim warmup")
        self.trim_warmup_backlog.setChecked(True)
        self.trim_warmup_backlog.setToolTip("Trim TX warmup backlog (~2s lower latency), "
                                            "--max-input-backlog: discard the input that queues up "
                                            "during TX warmup. Untick to "
                                            "compare against the old behaviour.")
        form.addPair("TX queue", self.tx_queue_depth, None, self.trim_warmup_backlog)

        self.output_mode = QtWidgets.QComboBox()
        self.output_mode.addItems(["pluto", "pipe"])
        self.output_mode.currentTextChanged.connect(self._update_output_fields)

        self.rf_freq = QtWidgets.QLineEdit("145500000")
        self.rf_freq.setPlaceholderText("Hz, required for --output pluto")
        self.rf_freq.setToolTip("RF frequency (Hz)")
        form.addPair("Output", self.output_mode, "RF Hz", self.rf_freq)

        self.tx_gain = QtWidgets.QDoubleSpinBox()
        self.tx_gain.setRange(-90, 0)
        self.tx_gain.setValue(-10)
        self.tx_gain.setToolTip("TX gain (dB re full power)")

        self.amplitude = QtWidgets.QDoubleSpinBox()
        self.amplitude.setRange(0.001, 1.0)
        self.amplitude.setSingleStep(0.01)
        self.amplitude.setDecimals(3)
        self.amplitude.setValue(0.1)
        self.amplitude.setToolTip(
            "--amplitude: RMS amplitude scale applied to the normalised waveform "
            "(0.001–1.0, default 0.1).  After RMS normalisation, 1.0 sets the "
            "RMS to full scale -- OFDM peaks are ~12–16 dB above RMS, so keep "
            "this well below 1 to avoid clipping.  Tune alongside TX gain to "
            "find the right power level for your SDR/PA chain.")
        form.addPair("TX gain dB", self.tx_gain, "Amplitude", self.amplitude)

        self.pluto_sample_rate = QtWidgets.QComboBox()
        self.pluto_sample_rate.setEditable(True)
        self.pluto_sample_rate.addItems(["550000", "(mode native rate)", "1000000", "2000000"])
        self.pluto_sample_rate.setToolTip("--sample-rate: resamples the modem's own IQ output to this "
                                           "rate in Hz before it reaches the PlutoSDR -- use this if "
                                           "your Pluto/SoapySDR setup needs a specific fixed sample "
                                           "rate rather than whatever rate the chosen mode/occupancy "
                                           "natively produces. Leave as-is for no resampling.")

        self.lo_offset_hz = QtWidgets.QDoubleSpinBox()
        self.lo_offset_hz.setRange(0, 400000)
        self.lo_offset_hz.setValue(100000)
        self.lo_offset_hz.setToolTip("LO offset (Hz)")
        form.addPair("Rate Hz", self.pluto_sample_rate, "LO offs.", self.lo_offset_hz)

        self.pluto_uri = QtWidgets.QLineEdit("ip:192.168.2.1")
        self.pluto_uri.setPlaceholderText("(auto)")
        self.pluto_uri.setToolTip("Pluto URI")

        self.output_file = QtWidgets.QLineEdit()
        self.output_file.setPlaceholderText("(only used for --output pipe -- a file path to save IQ to)")
        self.output_file.setToolTip("Pipe output file: only used for --output pipe -- a file path to save IQ to")
        form.addPair("Pluto URI", self.pluto_uri, "Pipe file", self.output_file)
        # No taller than its contents: any spare height goes to the log.
        gui_layout.fit_scroll_to_contents(form_scroll, form_container)

        btn_row = QtWidgets.QHBoxLayout()
        self.start_btn = QtWidgets.QPushButton("Start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        outer.addLayout(btn_row)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(100 if gui_layout.is_compact() else 220)
        outer.addWidget(self.log, stretch=1)

        self._update_source_fields(self.source_combo.currentText())
        self._update_output_fields(self.output_mode.currentText())
        self.resize(560, 900)
        gui_layout.place(self, 0.0, gui_layout.TX_WIDTH_FRACTION)
        self._refresh_devices()
        # Defaults: Codec2 audio + wavelet video. Set last, once every widget
        # exists, so the codec-change handlers (field enabling, 256x144
        # resolution, bitrate split) all run as if picked by hand.
        self.audio_codec.setCurrentText("codec2")
        self.video_codec.setCurrentText("wavelet")
        self._recalculate_bitrates()

    def _on_audio_codec_changed(self, codec):
        # Codec2's bitrate is fixed by its mode (see --codec2-mode), not
        # a free kbps value like Opus -- swap which control is live
        # rather than showing an Opus-only field that does nothing.
        # NOTE: the packing-aware bitrate calculator below (_recalculate_
        # bitrates/_check_audio_packing) still assumes Opus's per-packet
        # TLV overhead model when estimating audio's share of the link --
        # harmless for Codec2 in practice (its real bitrate, 700-3200bps,
        # is far below anything the calculator would flag), just not
        # literally accurate for it yet.
        is_codec2 = (codec == "codec2")
        self.codec2_mode.setEnabled(is_codec2)
        self.audio_bitrate.setEnabled(not is_codec2 and getattr(self, "_audio_enabled", True))
        self._recalculate_bitrates()

    def _on_force_audio_only_toggled(self, checked):
        # Mutually exclusive with force_video_only -- both checked at
        # once would ask media_source.py for --no-video AND --no-audio
        # together, which it now rejects outright (nothing left to send).
        if checked and self.force_video_only.isChecked():
            self.force_video_only.setChecked(False)
        self._recalculate_bitrates()

    def _on_force_video_only_toggled(self, checked):
        if checked and self.force_audio_only.isChecked():
            self.force_audio_only.setChecked(False)
        self._recalculate_bitrates()

    def _recalculate_bitrates(self):
        """Splits this config's own effective link capacity between audio
        and video automatically, instead of leaving two independent
        bitrate fields the user has to keep consistent with mode/
        occupancy/modulation/fragment-size by hand. Audio gets a fixed
        share of the USABLE capacity, clamped to [10, 20]kbps -- floored
        at 10 because much below that Opus quality degrades fast (see the
        earlier session's low-bitrate tone-quality investigation), capped
        at 20 so a wider/higher-throughput mode spends its extra capacity
        on video instead of audio quality past the point of much return.
        Video gets whatever's left, up to the raw rate minus all known
        header overhead (OFDM fragment header, already baked into
        estimate_effective_bitrate, plus TLV framing overhead subtracted
        below) -- no additional percentage margin on top of that. This
        link has no return channel, so a fragment either decodes within
        its own real-time budget or is simply lost; there's no retry-
        driven slowdown to reserve headroom against, so the ceiling is
        exactly raw-rate-minus-headers, not some fraction of it."""
        try:
            occupancy = float(self.occupancy.currentText())
            cfg = ofdm.build_config(self.mode.currentText(), occupancy,
                                     data_modulation=self.modulation.currentText(),
                                     fec_scheme=self.fec.currentText())
            total_bps = ofdm.estimate_effective_bitrate(cfg, self.fragment_size.value(),
                                                          fragment_gap_ms=self.fragment_gap_ms.value())
        except Exception as e:
            self.link_capacity_label.setText(f"(couldn't compute: {e})")
            return
        total_kbps = total_bps / 1000

        # estimate_effective_bitrate already accounts for this link's own
        # header/preamble/fragment-gap overhead -- it knows nothing about
        # media_tx_framer.py's OWN TLV framing sitting on top of that
        # payload (a 2-byte length prefix per fragment, a 3-byte type+
        # length header on every single audio/video packet record, and
        # periodic AUDIO_CONFIG/VIDEO_CONFIG resends), which is real
        # payload space that never reaches ffmpeg's encoders. Estimated
        # from typical packet cadence (AUDIO_FRAME_DURATION_MS's own
        # interval -- ALWAYS passed to media_source.py explicitly below,
        # never left to its own default, so this estimate can't silently
        # drift out of sync with the real packet rate again -- H.264
        # assumed ~1 NAL/encoded frame at --framerate) rather than
        # measured exactly, since real packet sizes/counts vary with
        # content -- a deliberately slightly-pessimistic estimate is
        # safer here than an optimistic one that leaves the encoder still
        # oversubscribed.
        fragment_size_bytes = self.fragment_size.value()
        fragment_duration_s = fragment_size_bytes * 8 / total_bps if total_bps > 0 else 0
        VIDEO_CONFIG_RECORD_BYTES = 50   # ~4B width/height + ~40B h264 SPS/PPS + 3B TLV header (varies)
        # 1 byte (AUDIO_PACKET_FIXED_TYPE, no length field), not the full
        # 3-byte RECORD_HEADER_LEN -- true for every audio packet after
        # the first one each session (CBR Opus's packet size never
        # changes), so this is the realistic steady-state cost, not a
        # one-off underestimate.
        AUDIO_RECORD_HEADER_LEN = 1
        audio_packets_per_frag = fragment_duration_s / AUDIO_PACKET_INTERVAL_S
        video_packets_per_frag = fragment_duration_s * self.framerate.value()
        tlv_overhead_bytes = (
            2  # per-fragment real_length prefix
            + AUDIO_PROFILE_CONFIG_BYTES  # once per fragment now, not per packet -- see its own comment
            + audio_packets_per_frag * AUDIO_RECORD_HEADER_LEN
            + video_packets_per_frag * media_tx_framer.RECORD_HEADER_LEN
            + video_packets_per_frag / media_tx_framer.VIDEO_CONFIG_REPEAT_EVERY * VIDEO_CONFIG_RECORD_BYTES
        )
        tlv_overhead_frac = min(tlv_overhead_bytes / fragment_size_bytes, 0.5) if fragment_size_bytes else 0
        total_kbps *= (1 - tlv_overhead_frac)

        # A real, separate margin IS needed here -- not the "retry
        # slowdown" margin an earlier version of this comment rejected
        # (that reasoning still holds: this link has no return channel,
        # so a fragment either decodes in its own budget or is simply
        # lost, with nothing to retry). This is a different thing:
        # StdoutPacer's own real-time fragment delivery has genuine,
        # unavoidable per-fragment timing overhead (confirmed via an
        # isolated zero-RF paced-loopback test AND on real hardware) that
        # the byte/overhead accounting above can't see at all, since it's
        # about TIME, not bytes.
        #
        # 0.85 (a 15% margin) was confirmed for real (this session) to
        # still be too thin: media_tx_framer.py's own output_queue depth
        # diagnostic showed a steady, unbounded climb (roughly 25-33% per
        # session, not just a one-off transient) at a video bitrate this
        # calculator itself had already suggested as packing-safe --
        # meaning content was being silently queued faster than
        # hf_ofdm_tx.py could ever transmit it in real time, with
        # whatever was still backlogged when a session stopped simply
        # never sent at all. That's a genuine, sustained oversubscription
        # of the real link, not a one-time startup blip a bigger margin
        # alone would paper over -- but since nothing else in this
        # calculator's own byte/overhead accounting explains a 25-33%
        # gap, tightening this margin to match what was actually measured
        # is the direct, honest fix available here without a deeper
        # audit of where exactly that overhead is going.
        #
        # 0.65 was confirmed for real to still be too thin -- output_queue
        # depth grew more slowly (~20%/session vs ~30% at 0.85) but still
        # climbed steadily rather than flattening, meaning the real usable
        # capacity for audio+video combined is lower than either margin
        # assumed. Extrapolating from both measured points (allocating
        # ~28kbps combined grew the backlog ~30%; ~21kbps combined grew it
        # ~20%) puts the real combined ceiling around 17kbps. 0.50 was
        # tested at that point with a since-fixed VBV misconfiguration
        # (bufsize=1x fragment period, too tight -- forced real libx264
        # "VBV underflow" bit-starvation) and its backlog was already
        # DRAINING rather than just flat, meaning it had slack to spare.
        # Two real fixes landed since: VBV widened to 2x the fragment
        # period (still tight enough to keep access units fragment-sized,
        # per a direct sweep -- see media_source_video.py's own -tune
        # zerolatency comment), and -tune zerolatency itself. That same
        # sweep found 0 VBV underflows starting at 16kbps video with
        # negligible size-safety cost vs 13.3kbps (774B vs 775B max) --
        # 0.60 here targets that 16kbps point directly (16 + ~3.2kbps
        # audio = ~19.2kbps combined, still under the drained 0.50 run's
        # implied headroom).
        TIMING_MARGIN_FRAC = 0.60
        # The wavelet codec's frames are exactly sized, so none of the
        # above (x264 VBV overshoot, reactive-packing extra fragments)
        # applies: its video share is computed from exact per-fragment
        # bytes instead -- see _wavelet_video_kbps.
        wavelet = self.video_codec.currentText() == "wavelet"
        if wavelet:
            TIMING_MARGIN_FRAC = 1.0
        usable_kbps = total_kbps * TIMING_MARGIN_FRAC

        if self.audio_codec.currentText() == "codec2":
            # None of the Opus-specific packing-cliff/AUDIO_SHARE math
            # below applies -- Codec2's bitrate is fixed by its mode
            # (a few kbps at most), not something to auto-balance
            # against video, and its frames are tiny/fixed-size enough
            # that the packing cliff Opus can hit essentially never
            # matters here. Video simply gets whatever's left.
            forced_video_only = getattr(self, "force_video_only", None) is not None \
                and self.force_video_only.isChecked()
            forced_audio_only = getattr(self, "force_audio_only", None) is not None \
                and self.force_audio_only.isChecked()
            audio_kbps = 0.0 if forced_video_only else CODEC2_MODE_KBPS.get(self.codec2_mode.currentText(), 3.2)
            self._audio_enabled = not forced_video_only
            video_kbps = usable_kbps if forced_audio_only else max(0.0, usable_kbps - audio_kbps)
            if wavelet and not forced_audio_only:
                video_kbps = self._wavelet_video_kbps(fragment_duration_s, audio_kbps)
            self._video_enabled = not forced_audio_only
            self._set_video_widgets_enabled(self._video_enabled)
            if not self.video_bitrate_manual.isChecked():
                self.video_bitrate.setValue(round(video_kbps, 1))
            self.link_capacity_label.setText(
                f"~{total_kbps:.1f}kbps after TLV overhead (~{tlv_overhead_frac*100:.0f}%) and "
                f"{(1 - TIMING_MARGIN_FRAC) * 100:.0f}% timing margin -> "
                f"audio (Codec2 {self.codec2_mode.currentText()}bps fixed) + video {video_kbps:.1f}kbps"
                f"{' (locked at ' + str(self.video_bitrate.value()) + 'kbps)' if self.video_bitrate_manual.isChecked() else ''}")
            return

        AUDIO_SHARE = 0.30
        # Opus share scales with the link (and so with video's rate), 8-16
        # kbps: mono speech gains little above 16, and on the slow links
        # every kbps under 10 goes to video instead.
        AUDIO_MIN_KBPS, AUDIO_MAX_KBPS = 8.0, 16.0
        audio_kbps = min(max(usable_kbps * AUDIO_SHARE, AUDIO_MIN_KBPS), AUDIO_MAX_KBPS)

        # A smooth kbps-vs-kbps comparison misses a real, discrete effect.
        # media_tx_framer.py's append_audio_packet now DOES split an
        # audio packet across fragments (AUDIO_PACKET_START/CONT, added
        # after the packing-cliff failure below was first found), which
        # already eliminates most of the waste this search exists to
        # avoid -- kept anyway as a conservative extra check, since it
        # can only ever clamp to an equal-or-lower bitrate, never suggest
        # one that's unsafe. As a candidate bitrate
        # rises, packet size grows continuously but packets-per-fragment
        # only ever drops in integer steps. Confirmed for real: at this
        # fragment size, 45kbps packed 3 packets/fragment (needing ~5.56
        # fragments/s) and played back fine; 50kbps packed only 2/fragment
        # (needing ~8.33 fragments/s) and failed outright, despite both
        # looking comfortably under the naive effective-bitrate ceiling.
        # Clamp to the highest bitrate that still keeps the REQUIRED
        # fragment rate (packet rate / packets actually packed per
        # fragment) at or under this link's real fragment rate, found by
        # direct simulation rather than a formula, since packing is a
        # step function -- deriving its inverse analytically is more
        # error-prone than just trying candidates.
        link_fragment_rate = 1 / fragment_duration_s if fragment_duration_s > 0 else 0

        def _packing_safe_kbps(candidate_kbps):
            packet_bytes = candidate_kbps * 1000 * AUDIO_PACKET_INTERVAL_S / 8
            # AUDIO_PROFILE_CONFIG_BYTES is seeded into every block ONCE
            # (see media_tx_framer.py's flush_block/_fresh_block) -- a
            # fixed per-fragment cost now, not a per-packet amortized one
            # the way the old ~26-byte periodic blob was.
            usable_fragment_bytes = fragment_size_bytes - 2 - AUDIO_PROFILE_CONFIG_BYTES
            # 1 byte (AUDIO_PACKET_FIXED_TYPE), the steady-state cost for
            # every packet after the session's very first one -- see
            # AUDIO_RECORD_HEADER_LEN's own comment above.
            effective_record_bytes = packet_bytes + 1
            packets_per_fragment = max(1, int(usable_fragment_bytes // effective_record_bytes))
            required_fragment_rate = (1 / AUDIO_PACKET_INTERVAL_S) / packets_per_fragment
            return required_fragment_rate <= link_fragment_rate

        if link_fragment_rate > 0 and not _packing_safe_kbps(audio_kbps):
            step = 0.1
            safe_kbps = AUDIO_MIN_KBPS
            candidate = AUDIO_MIN_KBPS
            while candidate <= audio_kbps:
                if _packing_safe_kbps(candidate):
                    safe_kbps = candidate
                candidate += step
            audio_kbps = safe_kbps
        video_kbps = usable_kbps - audio_kbps
        if wavelet:
            video_kbps = self._wavelet_video_kbps(fragment_duration_s, audio_kbps)

        # Below this, video isn't worth sending at all -- an unwatchably
        # coarse/slow picture that just eats into audio's own margin and
        # risks starving the TX queue (see the earlier session's
        # "silence-filled" queue-underrun finding) for no real benefit.
        # Audio-only at full budget instead: audio_kbps can then use the
        # WHOLE usable capacity rather than just its 30% share, up to its
        # own 20kbps ceiling.
        VIDEO_MIN_KBPS = 20.0
        # The wavelet codec degrades smoothly down to a few hundred bits
        # per frame (its per-frame size is exact at any rate), unlike
        # x264 which starves its VBV well before that.
        if self.video_codec.currentText() == "wavelet":
            VIDEO_MIN_KBPS = 6.0
        forced_audio_only = getattr(self, "force_audio_only", None) is not None \
            and self.force_audio_only.isChecked()
        forced_video_only = getattr(self, "force_video_only", None) is not None \
            and self.force_video_only.isChecked()
        self._audio_enabled = not forced_video_only
        self.audio_bitrate.setEnabled(self._audio_enabled)
        if forced_video_only:
            # No packing-cliff/TLV-per-packet concern here the way audio
            # has (video already splits across fragments via its own
            # START/CONT mechanism, unconditionally) -- the full usable
            # capacity is safe to hand it directly.
            if not self.video_bitrate_manual.isChecked():
                self.video_bitrate.setValue(round(
                    self._wavelet_video_kbps(fragment_duration_s, 0.0) if wavelet else usable_kbps, 1))
            self._set_video_widgets_enabled(True)
            self._video_enabled = True
            self.link_capacity_label.setText(
                f"~{total_kbps:.1f}kbps after TLV overhead (~{tlv_overhead_frac*100:.0f}%) and "
                f"{(1 - TIMING_MARGIN_FRAC) * 100:.0f}% timing margin -- forced video-only, "
                f"sending VIDEO ONLY at {usable_kbps:.1f}kbps")
            return

        self._video_enabled = (video_kbps >= VIDEO_MIN_KBPS) and not forced_audio_only
        self._set_video_widgets_enabled(self._video_enabled)

        if self._video_enabled:
            self.audio_bitrate.setValue(round(audio_kbps, 1))
            if not self.video_bitrate_manual.isChecked():
                self.video_bitrate.setValue(round(video_kbps, 1))
            self.link_capacity_label.setText(
                f"~{total_kbps:.1f}kbps after TLV overhead (~{tlv_overhead_frac*100:.0f}%) and "
                f"{(1 - TIMING_MARGIN_FRAC) * 100:.0f}% timing margin -> "
                f"audio {audio_kbps:.1f}kbps + video {video_kbps:.1f}kbps")
        else:
            # Audio-only: still capped at AUDIO_MAX_KBPS (more buys little
            # for mono speech), and the packing cliff bites (see above), so
            # search the packing-safe range up to that rather than assuming
            # a flat value is safe.
            audio_only_kbps = AUDIO_MIN_KBPS
            if link_fragment_rate > 0:
                candidate = AUDIO_MIN_KBPS
                while candidate <= min(usable_kbps, AUDIO_MAX_KBPS):
                    if _packing_safe_kbps(candidate):
                        audio_only_kbps = candidate
                    candidate += 0.1
            else:
                audio_only_kbps = min(usable_kbps, AUDIO_MAX_KBPS)
            self.audio_bitrate.setValue(round(audio_only_kbps, 1))
            reason = "forced audio-only" if forced_audio_only else \
                f"too little left for video after audio's {AUDIO_MIN_KBPS:.0f}kbps floor"
            self.link_capacity_label.setText(
                f"~{total_kbps:.1f}kbps effective -- {reason}, sending AUDIO ONLY at "
                f"{audio_only_kbps:.1f}kbps")

    def _wavelet_video_kbps(self, fragment_duration_s, audio_kbps):
        """Exact video rate for the wavelet codec: whatever one fragment
        per framer tick has left after its fixed contents. Per tick
        (= one fragment's on-air time) the framer packs: the 2-byte
        length prefix, the station-ID and audio-config records, every
        audio packet produced in that time (1-byte header each), then
        video frames (3-byte header each, plus a periodic config record).
        No timing margin -- the only slack is QUEUE_SLACK, so the
        framer's carry-over queue drains rather than random-walks when
        the camera's and the radio's clocks differ slightly."""
        QUEUE_SLACK = 0.01
        if fragment_duration_s <= 0:
            return 0.0
        fps = self.framerate.value()
        budget = self.fragment_size.value() - 2
        if getattr(self, "station_id", None) is not None and self.station_id.text().strip():
            budget -= media_tx_framer.RECORD_HEADER_LEN + 2
        if audio_kbps > 0:
            budget -= AUDIO_PROFILE_CONFIG_BYTES
            if self.audio_codec.currentText() == "codec2":
                pkt = media_tx_framer.AUDIO_PROFILES[0x01]["frame_bytes"]
            else:
                pkt = math.ceil(audio_kbps * 1000 * AUDIO_PACKET_INTERVAL_S / 8)
            budget -= fragment_duration_s / AUDIO_PACKET_INTERVAL_S * (pkt + 1)
        frames_per_tick = fps * fragment_duration_s
        config_bytes = media_tx_framer.RECORD_HEADER_LEN + 6 + len(media_tx_framer.WAVELET_CODEC_TAG)
        budget -= frames_per_tick / media_tx_framer.VIDEO_CONFIG_REPEAT_EVERY * config_bytes
        frame_bytes = math.floor(budget * (1 - QUEUE_SLACK) / frames_per_tick - media_tx_framer.RECORD_HEADER_LEN)
        return max(0.0, frame_bytes * 8 * fps / 1000)

    def _check_audio_packing(self):
        """Live packing-cliff warning for whatever's actually in the audio
        bitrate box right now -- including a value the user just typed by
        hand, which _recalculate_bitrates's own clamp never sees (that
        clamp only runs when occupancy/mode/fragment-size change, and a
        manual edit doesn't touch any of those). Confirmed for real this
        matters: 45kbps auto-suggested fine, but manually typing 50kbps
        packed one fewer Opus packet per fragment than 45kbps did, which
        alone pushed the required fragment rate past this link's own
        ceiling and failed outright -- silently, with no indication
        anything was wrong until it was tried live."""
        if getattr(self, "audio_codec", None) is not None and self.audio_codec.currentText() == "codec2":
            self.audio_packing_warning.setText("")  # Opus-specific cliff -- doesn't apply to Codec2
            return
        try:
            occupancy = float(self.occupancy.currentText())
            cfg = ofdm.build_config(self.mode.currentText(), occupancy,
                                     data_modulation=self.modulation.currentText(),
                                     fec_scheme=self.fec.currentText())
            fragment_size_bytes = self.fragment_size.value()
            total_bps = ofdm.estimate_effective_bitrate(cfg, fragment_size_bytes,
                                                          fragment_gap_ms=self.fragment_gap_ms.value())
            fragment_duration_s = fragment_size_bytes * 8 / total_bps if total_bps > 0 else 0
            link_fragment_rate = 1 / fragment_duration_s if fragment_duration_s > 0 else 0
        except Exception:
            self.audio_packing_warning.setText("")
            return
        if link_fragment_rate <= 0:
            self.audio_packing_warning.setText("")
            return

        candidate_kbps = self.audio_bitrate.value()
        packet_bytes = candidate_kbps * 1000 * AUDIO_PACKET_INTERVAL_S / 8
        # Same fixed once-per-fragment AUDIO_PROFILE_CONFIG_BYTES cost as
        # _recalculate_bitrates's own _packing_safe_kbps -- see its
        # comment for why this can't be skipped.
        usable_fragment_bytes = fragment_size_bytes - 2 - AUDIO_PROFILE_CONFIG_BYTES
        effective_record_bytes = packet_bytes + 1  # AUDIO_PACKET_FIXED_TYPE steady-state cost
        packets_per_fragment = max(1, int(usable_fragment_bytes // effective_record_bytes))
        required_fragment_rate = (1 / AUDIO_PACKET_INTERVAL_S) / packets_per_fragment

        if required_fragment_rate > link_fragment_rate:
            self.audio_packing_warning.setText(
                f"WARNING: at {candidate_kbps:.1f}kbps, only {packets_per_fragment} Opus packet(s) "
                f"pack per fragment, needing {required_fragment_rate:.2f} fragments/s -- this link "
                f"only sustains {link_fragment_rate:.2f}/s. Will fail on air, not just run tight.")
        else:
            self.audio_packing_warning.setText("")

    def _check_video_bitrate(self):
        """Live oversubscription warning for whatever's actually in the
        video bitrate box right now -- including a value typed by hand
        AFTER _recalculate_bitrates last set a safe one (that auto-calc
        only re-runs when mode/occupancy/fragment-size/etc. change, not
        on a direct edit to this field, exactly the gap
        _check_audio_packing already covers for audio's own box).
        Unlike audio, video has no discrete packing-cliff step (see
        _recalculate_bitrates's own comment: it already splits across
        fragments unconditionally) -- this is a plain continuous
        bitrate-budget check: does audio + video actually fit in this
        link's own usable capacity. Confirmed for real this class of gap
        matters: an oversubscribed encoder doesn't fail outright the way
        a packing-cliff does -- ffmpeg just keeps trying to hit a bitrate
        the link can't carry, backing up through the framer/TX pipeline
        as a growing, uneven lag (frames visibly arriving late/bursty at
        the receiver) rather than a clean, obvious failure."""
        try:
            occupancy = float(self.occupancy.currentText())
            cfg = ofdm.build_config(self.mode.currentText(), occupancy,
                                     data_modulation=self.modulation.currentText(),
                                     fec_scheme=self.fec.currentText())
            fragment_size_bytes = self.fragment_size.value()
            total_bps = ofdm.estimate_effective_bitrate(cfg, fragment_size_bytes,
                                                          fragment_gap_ms=self.fragment_gap_ms.value())
        except Exception:
            self.video_bitrate_warning.setText("")
            return
        if total_bps <= 0:
            self.video_bitrate_warning.setText("")
            return
        total_kbps = total_bps / 1000
        fragment_duration_s = fragment_size_bytes * 8 / total_bps

        # Same TLV-overhead and timing-margin accounting as
        # _recalculate_bitrates -- see its own comments for why each
        # piece is there. Kept in sync by hand (duplicated, like
        # _check_audio_packing already duplicates its own capacity math)
        # rather than sharing state, since this runs from a different
        # trigger (a live edit) than the auto-calc's own recompute.
        VIDEO_CONFIG_RECORD_BYTES = 50
        AUDIO_RECORD_HEADER_LEN = 1
        audio_packets_per_frag = fragment_duration_s / AUDIO_PACKET_INTERVAL_S
        video_packets_per_frag = fragment_duration_s * self.framerate.value()
        tlv_overhead_bytes = (
            2 + AUDIO_PROFILE_CONFIG_BYTES
            + audio_packets_per_frag * AUDIO_RECORD_HEADER_LEN
            + video_packets_per_frag * media_tx_framer.RECORD_HEADER_LEN
            + video_packets_per_frag / media_tx_framer.VIDEO_CONFIG_REPEAT_EVERY * VIDEO_CONFIG_RECORD_BYTES
        )
        tlv_overhead_frac = min(tlv_overhead_bytes / fragment_size_bytes, 0.5) if fragment_size_bytes else 0
        total_kbps *= (1 - tlv_overhead_frac)
        TIMING_MARGIN_FRAC = 0.85
        usable_kbps = total_kbps * TIMING_MARGIN_FRAC

        if getattr(self, "force_video_only", None) is not None and self.force_video_only.isChecked():
            effective_audio_kbps = 0.0
        elif self.audio_codec.currentText() == "codec2":
            effective_audio_kbps = CODEC2_MODE_KBPS.get(self.codec2_mode.currentText(), 3.2)
        else:
            effective_audio_kbps = self.audio_bitrate.value()

        # If video is currently disabled (audio-only mode -- see
        # _recalculate_bitrates's own "else" branch), the box's value is
        # stale/moot: nothing is actually being encoded or sent at it.
        video_kbps = self.video_bitrate.value() if getattr(self, "_video_enabled", True) else 0.0
        total_configured_kbps = video_kbps + effective_audio_kbps

        if self.video_codec.currentText() == "wavelet":
            # Wavelet frames are exactly sized, so none of the generic margin
            # above applies: check the exact per-frame budget instead (the
            # same one _recalculate_bitrates fills the box from), compared in
            # whole bytes per frame the way the encoder is configured.
            fps = self.framerate.value()
            if video_kbps <= 0 or fps <= 0:
                self.video_bitrate_warning.setText("")
                return
            max_kbps = self._wavelet_video_kbps(fragment_duration_s, effective_audio_kbps)
            frame_bytes = math.floor(video_kbps * 1000 / 8 / fps)
            max_frame_bytes = math.floor(round(max_kbps * 1000 / 8 / fps, 6))
            if frame_bytes > max_frame_bytes:
                self.video_bitrate_warning.setText(
                    f"WARNING: wavelet video {video_kbps:.1f}kbps ({frame_bytes} B/frame) is more than "
                    f"fits each fragment alongside the audio (max {max_kbps:.1f}kbps, "
                    f"{max_frame_bytes} B/frame at {fps:g} fps) -- frames will queue up in the framer "
                    f"and arrive later and later.")
            else:
                self.video_bitrate_warning.setText("")
            return

        if total_configured_kbps > usable_kbps:
            self.video_bitrate_warning.setText(
                f"WARNING: video {video_kbps:.1f}kbps + audio {effective_audio_kbps:.1f}kbps = "
                f"{total_configured_kbps:.1f}kbps exceeds this link's ~{usable_kbps:.1f}kbps usable "
                f"capacity. The encoder will keep trying to hit this bitrate anyway -- expect content "
                f"to back up and arrive late/unevenly (not cleanly dropped) rather than a hard failure.")
        else:
            self.video_bitrate_warning.setText("")

    def _update_source_fields(self, source):
        is_device = source == "device"
        self.video_device.setEnabled(is_device)
        self.audio_device.setEnabled(is_device)
        self.tone_hz.setEnabled(not is_device)

    def _update_output_fields(self, output):
        is_pluto = output == "pluto"
        self.rf_freq.setEnabled(is_pluto)
        self.tx_gain.setEnabled(is_pluto)
        self.pluto_sample_rate.setEnabled(is_pluto)
        self.lo_offset_hz.setEnabled(is_pluto)
        self.pluto_uri.setEnabled(is_pluto)
        self.output_file.setEnabled(not is_pluto)

    def _append_log(self, text):
        self.log.appendPlainText(text)

    def _refresh_devices(self):
        """Populates the video/audio device dropdowns by calling
        media_source.list_dshow_devices() directly (in-process, same
        interpreter this GUI itself runs under) rather than spawning a
        subprocess to parse -- it's a plain function, no need to shell out
        twice. Keeps whatever the user already typed/selected if it's
        still in the refreshed list; otherwise leaves the current text
        alone rather than silently overwriting a custom entry."""
        try:
            devices = media_source.list_dshow_devices()
        except Exception as e:
            self._append_log(f"[error] listing devices: {e}")
            return
        if not devices["video"] and not devices["audio"]:
            self._append_log("[gui] no capture devices found (no webcam/microphone plugged in?).")
            return
        for combo, names in ((self.video_device, devices["video"]), (self.audio_device, devices["audio"])):
            current = combo.currentText()
            combo.clear()
            combo.addItems(names)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(current)
        self._append_log(f"[gui] found {len(devices['video'])} video device(s), "
                          f"{len(devices['audio'])} audio device(s).")

    def start(self):
        if self.procs:
            return
        try:
            framer_cmd = [PYTHON, "media_tx_framer.py", "--fragment-size", str(self.fragment_size.value())]
            # Tells media_tx_framer.py's assembler thread the REAL on-air
            # duration of one fragment under the current mode/occupancy/
            # modulation/fec/fragment-size, so its audio/video interleave
            # cadence matches actual transmit timing instead of its own
            # 0.24s fallback default -- same effective-bitrate math as
            # _recalculate_bitrates/_check_video_bitrate above (duplicated
            # rather than shared, same reasoning as those).
            fragment_period_s = None
            try:
                occupancy = float(self.occupancy.currentText())
                cfg = ofdm.build_config(self.mode.currentText(), occupancy,
                                         data_modulation=self.modulation.currentText(),
                                         fec_scheme=self.fec.currentText())
                total_bps = ofdm.estimate_effective_bitrate(cfg, self.fragment_size.value(),
                                                              fragment_gap_ms=self.fragment_gap_ms.value())
                if total_bps > 0:
                    fragment_period_s = self.fragment_size.value() * 8 / total_bps
                    framer_cmd += ["--fragment-period-s", f"{fragment_period_s:.4f}"]
            except Exception:
                pass  # falls back to media_tx_framer.py's own default -- cosmetic, not fatal
            video_enabled = getattr(self, "_video_enabled", True)
            audio_enabled = getattr(self, "_audio_enabled", True)
            if video_enabled:
                # 2x fragment_period_s, not 1x -- confirmed for real via a
                # direct A/B measurement of x264's own output access-unit
                # sizes that exactly 1 fragment period (0.24s here) is too
                # tight: it hit the SAME max-access-unit-size ceiling as
                # 2x, but with real "VBV underflow" warnings from libx264
                # (the encoder starved for bits and forced to degrade
                # quality) that 2x didn't show, for no measured benefit.
                vbv_seconds = fragment_period_s * 2 if fragment_period_s else None
                framer_cmd += [
                    "--video-source-cmd",
                    media_tx_framer.quote_cmdline(self._build_video_source_cmd(vbv_seconds=vbv_seconds)),
                    "--video-framerate", str(self.framerate.value()),
                    "--video-resolution", self.resolution.currentText(),
                    "--video-codec", self.video_codec.currentText(),
                ]
            if audio_enabled:
                framer_cmd += ["--audio-source-cmd", media_tx_framer.quote_cmdline(self._build_audio_source_cmd()),
                                "--audio-codec", self.audio_codec.currentText()]
                if self.audio_codec.currentText() == "codec2":
                    framer_cmd += ["--audio-codec2-mode", self.codec2_mode.currentText()]
            station_id = self.station_id.text().strip()
            if station_id:
                framer_cmd += ["--station-id", station_id]
            tx_cmd = self._build_tx_cmd()
        except ValueError as e:
            self._append_log(f"[error] {e}")
            return

        self._append_log(f"[gui] framer: {' '.join(shlex.quote(c) for c in framer_cmd)}")
        self._append_log(f"[gui] tx:     {' '.join(shlex.quote(c) for c in tx_cmd)}")

        tx_stdout = subprocess.DEVNULL
        self._tx_out_file = None
        if self.output_mode.currentText() == "pipe" and self.output_file.text().strip():
            self._tx_out_file = open(self.output_file.text().strip(), "wb")
            tx_stdout = self._tx_out_file
        elif self.output_mode.currentText() == "pipe":
            self._append_log("[gui] warning: --output pipe with no output file set -- "
                              "raw IQ will be discarded (fine for a framer/link-rate smoke "
                              "test, not for anything you actually want to receive).")

        # media_tx_framer.py now owns its own video/audio source
        # subprocesses directly (see its own module docstring) instead
        # of being piped into from a single upstream media_source.py --
        # only two processes to chain here now, not three.
        p_framer = subprocess.Popen(framer_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    **avm_threads.die_with_parent())
        # Raises hf_ofdm_tx.py's OS scheduling priority above its sibling
        # ffmpeg encoder process(es) and media_tx_framer.py, same reasoning
        # as media_rx_gui.py's identical boost for hf_ofdm_rx.py: this is
        # the one process with a genuine real-time deadline (each
        # fragment's waveform has to reach PlutoTxSink's queue before its
        # own on-air slot), everything else here (video/audio encode,
        # TLV framing) has real but softer timing needs. Investigating a
        # report of cyclic low video fps traced (via
        # media_tx_framer.py's own "video source rate" log line) to the
        # SOURCE's own frame production rate wobbling on the real TX
        # machine, but NOT reproducible in an isolated test here (no
        # jitter at all, with or without a tighter VBV buffer) -- the one
        # real difference is that hf_ofdm_tx.py itself wasn't competing
        # for CPU in that isolated test the way it does on a live run.
        # Unverified whether this alone fixes it; a genuine measured
        # improvement either way, and matches the RX side's own fix.
        tx_priority = {"creationflags": subprocess.HIGH_PRIORITY_CLASS} if sys.platform == "win32" else {}
        p_tx = subprocess.Popen(tx_cmd, stdin=p_framer.stdout, stdout=tx_stdout,
                                 stderr=subprocess.PIPE, **tx_priority, **avm_threads.die_with_parent())
        p_framer.stdout.close()

        self.procs = [p_framer, p_tx]
        for proc, tag in zip(self.procs, ("framer", "tx")):
            t = threading.Thread(target=pump_stderr, args=(proc, tag, self.log_stream), daemon=True)
            t.start()
            self.threads.append(t)

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._append_log("[gui] started.")

    def stop(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        self.procs = []
        self.threads = []
        if getattr(self, "_tx_out_file", None) is not None:
            self._tx_out_file.close()
            self._tx_out_file = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._append_log("[gui] stopped.")

    def closeEvent(self, event):
        self.stop()
        event.accept()

    def _set_video_widgets_enabled(self, enabled):
        self._video_widgets_on = enabled
        for w in (self.video_bitrate, self.resolution, self.framerate, self.video_codec):
            w.setEnabled(enabled)
        self._update_codec_fields()

    def _update_codec_fields(self, *_):
        on = getattr(self, "_video_widgets_on", True)
        wavelet = self.video_codec.currentText() == "wavelet"
        self.intra_refresh.setEnabled(on and not wavelet)
        self.wavelet_leak.setEnabled(on and wavelet)
        self.wavelet_refresh_s.setEnabled(on and wavelet)
        # Each codec's own preset list; keep the current size if it's valid
        # for the new codec, else fall back to 256x144 (in both lists).
        presets = WAVELET_RESOLUTIONS if wavelet else H264_RESOLUTIONS
        if [self.resolution.itemText(i) for i in range(self.resolution.count())] != presets:
            current = self.resolution.currentText()
            self.resolution.clear()
            self.resolution.addItems(presets)
            self.resolution.setCurrentText(current)
        if wavelet and not self._wavelet_resolution_ok():
            self.resolution.setCurrentText("256x144")

    def _wavelet_resolution_ok(self):
        try:
            w, h = (int(x) for x in self.resolution.currentText().lower().split("x"))
        except ValueError:
            return False
        return w % 32 == 0 and h % 16 == 0

    def _build_video_source_cmd(self, vbv_seconds=None):
        if self.video_codec.currentText() == "wavelet":
            if not self._wavelet_resolution_ok():
                raise ValueError("wavelet codec needs a resolution that's a multiple of 32x16 "
                                 "(e.g. 256x144)")
            cmd = [PYTHON, "media_source_wavelet.py",
                   "--source", self.source_combo.currentText(),
                   "--resolution", self.resolution.currentText(),
                   "--framerate", str(self.framerate.value()),
                   # exact bytes, floored, so a rounded kbps can never overshoot
                   "--frame-bytes", str(math.floor(self.video_bitrate.value() * 1000 / 8
                                                   / self.framerate.value() + 1e-6)),
                   "--leak", f"{self.wavelet_leak.value():.3f}",
                   "--refresh-seconds", f"{self.wavelet_refresh_s.value():.2f}"]
            if self.source_combo.currentText() == "device":
                cmd += ["--video-device", self.video_device.currentText()]
            return cmd
        return self._build_h264_source_cmd(vbv_seconds)

    def _build_h264_source_cmd(self, vbv_seconds=None):
        """vbv_seconds: x264's VBV buffer size, as a multiple of
        --video-bitrate -- passed explicitly as the real fragment period
        (matching --fragment-period-s) rather than left at
        media_source_video.py's own 2.0s default. Confirmed for real this
        matters: a VBV window much longer than the channel's actual
        transmission quantization (one fragment every ~240ms, a hard
        fixed rate with no way to ever catch up) lets x264's own rate
        control run over nominal bitrate for a frame or two as long as
        it's compensated somewhere in the NEXT 2 SECONDS -- but the
        channel only ever has ~240ms of real slack per slot, so those
        temporary overshoots show up as fragments hf_ofdm_tx.py can't
        drain in time, building a backlog that (unlike the encoder's own
        VBV buffer) never gets a chance to recover. Matching the VBV
        window to the real slot size forces the encoder's rate control to
        already respect the channel's actual granularity, the same
        principle DVB/ATSC/DRM's own HRD-aware multiplexers rely on."""
        cmd = [PYTHON, "media_source_video.py",
               "--source", self.source_combo.currentText(),
               "--resolution", self.resolution.currentText(),
               "--framerate", str(self.framerate.value()),
               "--video-bitrate", str(self.video_bitrate.value())]
        if vbv_seconds is not None and vbv_seconds > 0:
            cmd += ["--video-vbv-seconds", f"{vbv_seconds:.4f}"]
        if self.source_combo.currentText() == "device":
            cmd += ["--video-device", self.video_device.currentText()]
        if not self.intra_refresh.isChecked():
            cmd += ["--no-intra-refresh"]
        return cmd

    def _build_audio_source_cmd(self):
        cmd = [PYTHON, "media_source_audio.py", "--source", self.source_combo.currentText(),
               "--audio-codec", self.audio_codec.currentText()]
        if self.audio_codec.currentText() == "codec2":
            cmd += ["--codec2-mode", self.codec2_mode.currentText()]
        else:
            cmd += [
                "--audio-bitrate", str(self.audio_bitrate.value()),
                # Explicit, not relying on media_source_audio.py's own default
                # -- AUDIO_PACKET_INTERVAL_S above assumes exactly this value
                # everywhere it estimates TLV/overhead cost, and the two
                # silently drifting apart is exactly what undercounted
                # real overhead by 3x and let an oversubscribed bitrate
                # through uncaught (see AUDIO_FRAME_DURATION_MS's comment).
                "--audio-frame-duration", str(AUDIO_FRAME_DURATION_MS),
            ]
        if self.source_combo.currentText() == "device":
            cmd += ["--audio-device", self.audio_device.currentText()]
        else:
            cmd += ["--tone-hz", str(self.tone_hz.value())]
        return cmd

    def _build_tx_cmd(self):
        cmd = [PYTHON, "hf_ofdm_tx.py",
               "--mode", self.mode.currentText(),
               "--occupancy", self.occupancy.currentText(),
               "--modulation", self.modulation.currentText(),
               "--fec", self.fec.currentText(),
               "--fragment-size", str(self.fragment_size.value()),
               "--fragment-gap-ms", str(self.fragment_gap_ms.value()),
               "--amplitude", str(self.amplitude.value()),
               "--stream",
               "--output", self.output_mode.currentText()]
        if self.trim_warmup_backlog.isChecked():
            # Live media: the framer makes one fragment per on-air fragment
            # time, so the 2s TX-warmup backlog would otherwise be permanent
            # delay -- see hf_ofdm_tx.py's _LiveInput. 8 (~2s) is only a
            # safety net; a tight cap dropped fragments on ordinary jitter.
            cmd += ["--max-input-backlog", "8"]
        if self.output_mode.currentText() == "pluto":
            if not self.rf_freq.text().strip():
                raise ValueError("RF freq is required for --output pluto.")
            cmd += ["--rf-freq", self.rf_freq.text().strip(), "--tx-gain", str(self.tx_gain.value()),
                    "--tx-queue-depth", str(self.tx_queue_depth.value())]
            sample_rate_text = self.pluto_sample_rate.currentText().strip()
            if sample_rate_text and not sample_rate_text.startswith("("):
                cmd += ["--sample-rate", sample_rate_text]
            if self.lo_offset_hz.value() > 0:
                cmd += ["--lo-offset-hz", str(self.lo_offset_hz.value())]
            if self.pluto_uri.text().strip():
                cmd += ["--pluto-uri", self.pluto_uri.text().strip()]
        return cmd


def main():
    gui_layout.prepare_environment()
    app = QtWidgets.QApplication(sys.argv)
    gui_layout.set_app_id("hfmodem-tx")
    gui_layout.apply_compact_style(app)
    win = MediaTxWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
