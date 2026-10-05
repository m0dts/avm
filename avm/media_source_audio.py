#!/usr/bin/env python3
"""
Audio-only counterpart to the old media_source.py -- writes a raw
Ogg-Opus elementary stream (no Matroska container) to stdout, so
media_tx_framer.py can frame it itself instead of relying on ffmpeg's
Matroska muxer. See media_source_video.py's docstring for the full
rationale (Matroska's muxer refuses some codecs, e.g. Codec2, entirely
-- a raw elementary stream has no such restriction).

Usage: see media_source_video.py's docstring -- both are launched BY
media_tx_framer.py, not piped into it.

IMPORTANT (Windows): PowerShell's native `|` corrupts binary data
passed between processes -- run this only via media_tx_framer.py's own
subprocess.Popen orchestration, never chained with a raw PowerShell pipe.
"""
import argparse
import os
import shutil
import subprocess
import sys

import avm_threads
from media_source import build_audio_args, list_devices


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["device", "test"], default="device",
                     help="'device' (default): capture from real hardware (--audio-device). "
                          "'test': synthetic tone, no hardware needed -- see --tone-hz.")
    ap.add_argument("--list-devices", action="store_true",
                     help="Print available capture device names (Windows/dshow) and exit.")
    ap.add_argument("--audio-device", type=str, default="Microphone",
                     help="Capture device name for --source device (default 'Microphone').")
    ap.add_argument("--audio-codec", choices=["opus", "codec2"], default="opus",
                     help="default opus. Codec2 (700-3200bps) trades audio quality for a much "
                          "lower, genuinely fixed-size-per-frame bitrate -- see media_tx_framer.py's "
                          "AUDIO_PROFILES for why that fits this link's framing well, and note it's "
                          "voice-only (8kHz mono), not suitable for music/tones.")
    ap.add_argument("--codec2-mode", type=str, default="3200",
                     help="Codec2 mode (default 3200) -- must have a matching AUDIO_PROFILES entry "
                          "in both media_tx_framer.py and media_rx_player.py.")
    ap.add_argument("--audio-bitrate", type=float, default=10.0,
                     help="Audio (Opus) bitrate in kbps (default 10). Ignored for --audio-codec codec2 "
                          "(use --codec2-mode instead).")
    ap.add_argument("--audio-rate", type=int, default=48000,
                     help="Audio sample rate in Hz (default 48000). Forced to 8000 for --audio-codec "
                          "codec2 (libcodec2 only supports 8kHz input).")
    ap.add_argument("--audio-frame-duration", type=float, default=20.0,
                     choices=[2.5, 5.0, 10.0, 20.0, 40.0, 60.0],
                     help="Opus frame duration in ms (default 20). See the old media_source.py's "
                          "docstring for how this affects packing density in hf_ofdm_rx.py's "
                          "fixed-size fragments. Ignored for --audio-codec codec2.")
    ap.add_argument("--tone-hz", type=float, default=650.0,
                     help="Test tone frequency in Hz for --source test (default 650).")
    ap.add_argument("--duration", type=float, default=None,
                     help="Stop after this many seconds (default: run until interrupted).")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        sys.exit(1)

    if args.audio_codec == "codec2":
        args.audio_rate = 8000  # libcodec2's only supported input rate -- see -h encoder=libcodec2

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
    rec = _arecord_cmd(args)
    if rec:
        # arecord captures, ffmpeg only encodes from the pipe. ffmpeg's own
        # ALSA input cost 9.4% of a Pi 4 core just to capture a C920 mic
        # (arecord: 0.1%) -- the whole mic chain went from ~15% to ~10%.
        # plughw delivers exactly the encoder's rate, mono, so ffmpeg does
        # no resampling. No wall-clock stretching either: on the raw
        # device the capture runs at true real time (20.0 s of audio per
        # 20 s), and the receiver absorbs clock drift itself.
        cmd += ["-f", "s16le", "-ar", str(args.audio_rate), "-ac", "1", "-i", "-"]
    else:
        if args.source == "device":
            # Lock a real capture device to the wall clock: timestamp input by
            # arrival time and let aresample pad/stretch to match. Measured on a
            # Raspberry Pi with a Logitech C920 via ALSA "default": the capture
            # delivered only 95.5% of real time, so the receiver's audio buffer
            # drained and broke every ~22 s although the radio link lost
            # nothing. With this: 99.9%.
            cmd += ["-use_wallclock_as_timestamps", "1"]
        cmd += build_audio_args(args)
        if args.source == "device":
            cmd += ["-af", "aresample=async=1000"]
    if args.duration:
        cmd += ["-t", str(args.duration)]
    if args.audio_codec == "codec2":
        # Headerless raw output (codec2raw), not the .c2 file format
        # (which has its own small header) -- confirmed for real this
        # muxer exists and produces exactly frame_bytes-per-frame with
        # nothing else. ffmpeg auto-resamples a --source device capture
        # to 8kHz to feed the encoder even if the device's own native
        # rate differs (only --source test's sine needs args.audio_rate
        # forced above, since that's an explicit -i rate, not negotiated).
        cmd += ["-c:a", "libcodec2", "-mode", args.codec2_mode, "-ac", "1",
                "-flush_packets", "1", "-f", "codec2raw", "-"]
    else:
        audio_kbps = f"{args.audio_bitrate}k"
        # -flush_packets 1: see media_source_video.py's identical flag for
        # the full rationale (an untested-but-plausible fix for ffmpeg
        # batching several encoded packets in its own output buffer
        # before actually writing them to the pipe).
        cmd += [
            # complexity 5 (default 10): ~3% less of a Pi 4 core, no audible
            # difference for speech at these bitrates
            "-c:a", "libopus", "-compression_level", "5", "-b:a", audio_kbps, "-vbr", "off", "-ac", "1",
            "-frame_duration", f"{args.audio_frame_duration:g}", "-ar", str(args.audio_rate),
            # -page_duration 20000 (us): one Ogg page per 20ms packet. The
            # muxer's default is ~1s pages, which delivered audio in 1s
            # bursts (measured: max inter-packet gap 1016ms vs 47ms with
            # this) -- a full second of added latency, and bursts that
            # overflowed the framer's per-tick fragment budget.
            "-flush_packets", "1", "-page_duration", "20000", "-f", "ogg", "-",
        ]

    print(f"[media_source_audio] running: {' '.join(cmd)}", file=sys.stderr)
    # See media_source_video.py's identical change for the full rationale
    # -- narrows the scheduling-priority gap against hf_ofdm_tx.py's own
    # boosted priority (media_tx_gui.py), rather than leaving audio's
    # real-time encode at the bottom of the ladder.
    priority = {"creationflags": subprocess.ABOVE_NORMAL_PRIORITY_CLASS} if sys.platform == "win32" else {}
    if rec:
        print(f"[media_source_audio] capture: {' '.join(rec)}", file=sys.stderr)
        # arecord -> ffmpeg. If either end goes (TX stopped: the framer closes
        # ffmpeg's output), the other follows via EOF / SIGPIPE.
        capture = subprocess.Popen(rec, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **avm_threads.die_with_parent())
        try:
            proc = subprocess.run(cmd, stdin=capture.stdout, **priority, **avm_threads.die_with_parent())
        finally:
            capture.kill()
        sys.exit(proc.returncode)
    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, **priority, **avm_threads.die_with_parent())
    sys.exit(proc.returncode)


def _arecord_cmd(args):
    """arecord capture for an ALSA device on Linux (hw:..., plughw:... or a
    card name), or None to let ffmpeg capture (test source, "default" --
    which goes through the sound server, see media_source.build_audio_args --,
    Windows, no arecord, or HF_AUDIO_CAPTURE=ffmpeg)."""
    if (args.source != "device" or not sys.platform.startswith("linux")
            or os.environ.get("HF_AUDIO_CAPTURE") == "ffmpeg" or not shutil.which("arecord")):
        return None
    dev = args.audio_device or ""
    if not dev or dev == "default":
        return None
    if dev.startswith("hw:"):
        dev = "plug" + dev  # ALSA converts to the encoder's rate/mono (cheap)
    # 20 ms periods, 200 ms buffer: arrives smoothly, one Opus frame at a time
    return ["arecord", "-q", "-D", dev, "-f", "S16_LE", "-r", str(args.audio_rate), "-c", "1",
            "-t", "raw", "--period-time=20000", "--buffer-time=200000"]


if __name__ == "__main__":
    main()
