#!/usr/bin/env python3
"""
Thin wrapper that builds and launches the ffmpeg command feeding
media_tx_framer.py -- so resolution/framerate/bitrate/device choices are
plain CLI flags here instead of a long ffmpeg command line to hand-edit
each time. Writes the same Matroska (mkv) audio+video stream ffmpeg
would, straight through to stdout; this script does no byte handling of
its own (ffmpeg's stdout is inherited directly), so it adds no risk of
its own to the binary data in transit.

Video defaults to CBR + intra-refresh (no periodic huge keyframe) --
see media_tx_framer.py's docstring for why that matters on this link.

Usage:
    python media_source.py --video-bitrate 25.2 --audio-bitrate 10 \\
        --resolution 160x120 --framerate 12.5 \\
        --video-device "Integrated Camera" --audio-device "Microphone" \\
    | python media_tx_framer.py --fragment-size 1024 \\
    | python hf_ofdm_tx.py --mode A --occupancy 80 --modulation qpsk \\
          --fragment-size 1024 --fragment-gap-ms 0 --stream ...

Test source, no camera/mic needed (a synthetic test pattern + tone --
see media_rx_player.py's earlier note that a pure tone is a poor judge
of audio quality at low bitrate; useful for exercising the pipeline
itself, not for judging codec fidelity):
    python media_source.py --source test --tone-hz 440 ...

List available capture device names (Windows/dshow only):
    python media_source.py --list-devices

IMPORTANT (Windows): pipe this into media_tx_framer.py via `cmd /c` or
a .bat file, NOT PowerShell's native `|` -- PowerShell's pipeline
corrupts binary data passed between processes (confirmed repeatedly
throughout this project's testing). Plain `cmd` pipes and `>`/`<`
redirection do not have this problem.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys


def build_video_args(args):
    if args.source == "test":
        # -re: without it, ffmpeg generates+encodes a synthetic lavfi
        # source as fast as the CPU allows, not paced to real time --
        # everything downstream (the framer, hf_ofdm_tx.py --stream) is
        # built assuming content trickles in at its natural real-time
        # rate, so an unpaced source would dump way more data than the
        # link could ever carry instead of the steady stream this whole
        # pipeline expects. Real capture devices (dshow/v4l2, the other
        # branch below) already pace themselves in real time via the
        # hardware's own frame timing, so -re is neither needed nor used
        # there -- it's specifically a test-source-only concern.
        return ["-re", "-f", "lavfi", "-i", f"testsrc=size={args.resolution}:rate={args.framerate}"]
    # Deliberately NOT forcing -video_size/-framerate on the device input
    # itself -- many devices (virtual cameras especially, e.g. OBS Virtual
    # Camera) only support ONE fixed mode and refuse to open at all if
    # asked for anything else (confirmed for real: dshow's "Could not set
    # video options" / "Error opening input: I/O error" on exactly this).
    # Capture at whatever the device's own default is, then convert to the
    # requested --resolution/--framerate via a filter afterward (see
    # main()'s -vf) -- decouples "what this device can capture" from
    # "what this link should encode at" entirely.
    if sys.platform == "win32":
        return ["-f", "dshow", "-i", f"video={args.video_device}"]
    # Linux (v4l2). Unlike the dshow case above, a v4l2 device lists the
    # exact sizes it supports, so ask for one of those: a camera's default
    # mode is usually far bigger than this link's tiny frames (a Logitech
    # C920 defaults to 640x480, and 4:3 -- the 16:9 frame then got black
    # side bars), costing CPU to scale down every frame. Falls back to the
    # device default if the sizes can't be read.
    size = _v4l2_capture_size(args.video_device, getattr(args, "resolution", None))
    if size:
        # YUYV (uncompressed). MJPEG cut a C920's camera-to-preview latency
        # from 0.5 to 0.4 s, but decoding it cost ffmpeg ~10% more of a Pi 4
        # core (15% vs ~5%) -- too much with TX and RX on one Pi.
        # HF_V4L2_INPUT_FORMAT=mjpeg uses it where the camera offers it.
        fmt = os.environ.get("HF_V4L2_INPUT_FORMAT") or "yuyv422"
        if fmt == "mjpeg" and not _v4l2_has_mjpeg(args.video_device, size):
            fmt = "yuyv422"
        return ["-f", "v4l2", "-input_format", fmt, "-video_size", size, "-i", args.video_device]
    return ["-f", "v4l2", "-i", args.video_device]


def _v4l2_has_mjpeg(device, size):
    """True if the v4l2 device lists MJPG at exactly `size` (WxH)."""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", device, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    in_mjpg = False
    for line in out.splitlines():
        if re.match(r"\s*\[\d+\]:", line):
            in_mjpg = "MJPG" in line
        elif in_mjpg and f"Size: Discrete {size}" in line:
            return True
    return False


def _v4l2_capture_size(device, resolution):
    """Smallest uncompressed (YUYV) capture size the device lists that is at
    least `resolution` (WxH) in both dimensions, preferring the same aspect
    ratio (within 3%) -- e.g. 320x180 for 256x144 on a C920. None if
    v4l2-ctl isn't installed, the device lists nothing usable, etc., or
    HF_V4L2_NATIVE_SIZE=0 (capture at the device default, as before)."""
    if os.environ.get("HF_V4L2_NATIVE_SIZE") == "0":
        return None
    try:
        tw, th = (int(v) for v in str(resolution).lower().split("x"))
    except (TypeError, ValueError):
        return None
    if shutil.which("v4l2-ctl") is None:
        return None
    try:
        out = subprocess.run(["v4l2-ctl", "-d", device, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    sizes, in_yuyv = set(), False
    for line in out.splitlines():
        if re.match(r"\s*\[\d+\]:", line):
            in_yuyv = "YUYV" in line
        m = re.search(r"Size: Discrete (\d+)x(\d+)", line)
        if in_yuyv and m:
            sizes.add((int(m.group(1)), int(m.group(2))))
    fits = [(w, h) for w, h in sizes if w >= tw and h >= th]
    if not fits:
        return None
    target = tw / th
    w, h = min(fits, key=lambda s: (abs(s[0] / s[1] - target) / target > 0.03, s[0] * s[1]))
    return f"{w}x{h}"


def build_audio_args(args):
    if args.source == "test":
        return ["-re", "-f", "lavfi", "-i", f"sine=f={args.tone_hz}:r={args.audio_rate}"]
    if sys.platform == "win32":
        # -audio_buffer_size 20 (ms): dshow's default capture buffer is the
        # device's own, measured at 500ms-1000ms on this machine's mics --
        # audio then arrives in 0.5-1s blocks (max inter-packet gap 1000ms
        # vs 32ms with this), which is that much added audio latency and
        # left audio ~0.5s behind video at the receiver.
        return ["-f", "dshow", "-audio_buffer_size", "20", "-i", f"audio={args.audio_device}"]
    if args.audio_device == "default" and _pulse_available():
        # The desktop's sound server (PipeWire's PulseAudio interface, or
        # PulseAudio itself) rather than ALSA "default": ffmpeg's ALSA input
        # asks for tiny periods, and PipeWire then ran the whole graph at a
        # 128-sample quantum -- on a loaded Raspberry Pi that overran
        # constantly (thousands of xruns) and a C920 mic delivered only
        # ~95% of real time, heard as dropouts/slowed audio. Measured
        # through the pulse input: 100.0%.
        return ["-f", "pulse", "-i", "default"]
    return ["-f", "alsa", "-i", args.audio_device]


def _pulse_available():
    """Whether ffmpeg has a pulse input and a PulseAudio-compatible server
    (PipeWire's, on Raspberry Pi OS) is running for this user."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not os.path.exists(os.path.join(runtime, "pulse", "native")):
        return False
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-devices"], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[1:2] == ["pulse"] and "D" in line.split()[0]
               for line in out.splitlines() if len(line.split()) > 1)


def list_dshow_devices():
    """Runs ffmpeg's own dshow device enumeration and parses the device
    names out of its stderr, for anything (e.g. media_tx_gui.py's device
    dropdowns) that wants them as plain strings instead of a human to read
    off a terminal dump. Returns {"video": [...], "audio": [...]} -- empty
    lists (not an exception) if ffmpeg isn't found or the platform isn't
    Windows, so a caller can always safely iterate the result.

    ffmpeg's list_devices output always "fails" (it's not actually opening
    "dummy", just enumerating and then reporting it couldn't find a device
    by that name) -- the device names are printed to stderr regardless,
    which is what's parsed here rather than the (expected, meaningless)
    exit code."""
    if sys.platform.startswith("linux"):
        return list_linux_devices()
    if sys.platform != "win32" or shutil.which("ffmpeg") is None:
        return {"video": [], "audio": []}
    result = subprocess.run(["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                             capture_output=True, text=True, timeout=15)
    devices = {"video": [], "audio": []}
    # Newer ffmpeg builds print one flat line per device, no section
    # headers: `"Device Name" (audio, video)` / `(video)` / `(audio)` /
    # `(none)` -- classify by what's inside the parens rather than
    # assuming older ffmpeg's separate "DirectShow video/audio devices"
    # section-header format (confirmed for real this build uses the flat
    # style; "Alternative name" lines have no parens and are skipped by
    # the capability check finding nothing to classify).
    for line in result.stderr.splitlines():
        m = re.search(r'"([^"]+)"\s*\(([^)]*)\)', line)
        if not m:
            continue
        name, caps = m.group(1), m.group(2)
        if "video" in caps:
            devices["video"].append(name)
        if "audio" in caps:
            devices["audio"].append(name)
    return devices


def list_linux_devices():
    """Linux counterpart of the dshow listing: v4l2 capture nodes as
    /dev/videoN paths (what build_video_input's v4l2 branch takes) and ALSA
    capture PCMs as hw:CARD=...,DEV=... names (what -f alsa takes). Skips the
    Raspberry Pi's own bcm2835 codec/ISP and rpi-hevc-dec nodes, which are memory-to-memory
    devices, not cameras, and only keeps the first node of each camera (later
    ones are usually metadata nodes)."""
    devices = {"video": [], "audio": []}
    by_name = {}
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0)):
        try:
            with open(f"/sys/class/video4linux/{os.path.basename(path)}/name") as f:
                name = f.read().strip()
        except OSError:
            name = ""
        if name.startswith(("bcm2835", "rpi-")) or name in by_name:
            continue
        by_name[name] = path
        devices["video"].append(path)
    if shutil.which("arecord"):
        try:
            out = subprocess.run(["arecord", "-L"], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        devices["audio"] = ["default"] + [line for line in out.splitlines()
                                           if line.startswith("hw:CARD=")]
    return devices


def list_devices():
    if sys.platform != "win32" and not sys.platform.startswith("linux"):
        print("--list-devices only implemented for Windows/dshow and Linux", file=sys.stderr)
        sys.exit(1)
    devices = list_dshow_devices()
    print("Video devices:", file=sys.stderr)
    for name in devices["video"]:
        print(f"  {name!r}", file=sys.stderr)
    print("Audio devices:", file=sys.stderr)
    for name in devices["audio"]:
        print(f"  {name!r}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["device", "test"], default="device",
                     help="'device' (default): capture from real hardware (--video-device/"
                          "--audio-device). 'test': synthetic test pattern + tone, no hardware "
                          "needed -- see --tone-hz.")
    ap.add_argument("--list-devices", action="store_true",
                     help="Print available capture device names (Windows/dshow) and exit -- "
                          "use the names it prints as --video-device/--audio-device.")
    ap.add_argument("--video-device", type=str, default="Integrated Camera",
                     help="Capture device name for --source device (default 'Integrated Camera'). "
                          "See --list-devices.")
    ap.add_argument("--audio-device", type=str, default="Microphone",
                     help="Capture device name for --source device (default 'Microphone'). "
                          "See --list-devices.")
    ap.add_argument("--resolution", type=str, default="160x90",
                     help="Video resolution as WxH (default 160x90 -- 16:9, matching the combined "
                          "RX GUI's video pane; kept small deliberately: this link's whole video "
                          "budget is ~25kbps, so a bigger frame just means coarser per-pixel "
                          "quality at the same bitrate, not a better picture). Any WxH works -- "
                          "the scale+pad filter below letterboxes whatever the source's native "
                          "aspect ratio is to fit exactly this resolution.")
    ap.add_argument("--framerate", type=float, default=12.0,
                     help="Video capture/encode framerate (default 12fps -- lower spends the "
                          "fixed video bitrate on quality per frame instead of frame count).")
    ap.add_argument("--video-bitrate", type=float, default=25.0,
                     help="Video bitrate in kbps (default 25).")
    ap.add_argument("--audio-bitrate", type=float, default=10.0,
                     help="Audio (Opus) bitrate in kbps (default 10).")
    ap.add_argument("--audio-rate", type=int, default=48000,
                     help="Audio sample rate in Hz (default 48000).")
    ap.add_argument("--audio-frame-duration", type=float, default=20.0,
                     choices=[2.5, 5.0, 10.0, 20.0, 40.0, 60.0],
                     help="Opus frame duration in ms (default 20 -- libopus's usual sweet spot). "
                          "Smaller values give finer-grained packet sizes, which matters for how "
                          "evenly Opus packets pack into hf_ofdm_rx.py's fixed-size fragments: a "
                          "packet that no longer fits drops packing density in a whole step (e.g. "
                          "3 packets/fragment -> 2), and that step is proportionally SMALLER (so a "
                          "gentler cliff) the more packets normally fit per fragment to begin with "
                          "-- which is exactly what a shorter frame duration buys, at the cost of "
                          "somewhat worse Opus compression efficiency and slightly more per-packet "
                          "TLV framing overhead (paid more often per second). See media_tx_gui.py's "
                          "packing-aware bitrate calculator, which needs to match this value.")
    ap.add_argument("--tone-hz", type=float, default=440.0,
                     help="Test tone frequency in Hz for --source test (default 440).")
    ap.add_argument("--gop-seconds", type=float, default=2.0,
                     help="With intra-refresh (the default -- see --no-intra-refresh), how often "
                          "the FULL picture has finished one rolling refresh cycle (default 2s); "
                          "also how often a late-joining/resynced receiver can expect a clean "
                          "picture. Converted to a frame count via --framerate.")
    ap.add_argument("--video-vbv-seconds", type=float, default=2.0,
                     help="x264's VBV buffer size (-bufsize), as a multiple of --video-bitrate's own "
                          "1-second budget (default 2.0). See media_source_video.py's identical "
                          "option for the full rationale -- a too-tight buffer (the old fixed 0.6s) "
                          "forces x264's own rate control to skip/delay frames under ordinary scene "
                          "complexity, confirmed for real via media_tx_framer.py's 'video source "
                          "rate' log line running well under --framerate in bursts even with the "
                          "link/framer/RX all otherwise healthy.")
    ap.add_argument("--no-intra-refresh", action="store_true",
                     help="Use conventional periodic full I-frames (-g, from --gop-seconds) "
                          "instead of x264's rolling intra-refresh. Simpler, but produces a much "
                          "bigger keyframe periodically -- media_tx_framer.py splits an oversized "
                          "packet across fragments automatically, but it costs more bandwidth in "
                          "that instant than intra-refresh's steady CBR size. See "
                          "media_tx_framer.py's docstring for the full tradeoff.")
    ap.add_argument("--duration", type=float, default=None,
                     help="Stop after this many seconds (default: run until interrupted). "
                          "Mainly useful for --source test.")
    ap.add_argument("--no-video", action="store_true",
                     help="Audio only -- skip capturing/encoding video entirely (e.g. when the "
                          "configured link capacity leaves too little for a usable video bitrate). "
                          "media_tx_framer.py/media_rx_player.py already handle an audio-only "
                          "stream with no changes needed.")
    ap.add_argument("--no-audio", action="store_true",
                     help="Video only -- skip capturing/encoding audio entirely. "
                          "media_tx_framer.py/media_rx_player.py already handle a video-only "
                          "stream with no changes needed (media_rx_player.py just never receives "
                          "an AUDIO_CONFIG record). Mutually exclusive with --no-video in practice "
                          "(one of them sending nothing would leave no stream at all).")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    if args.no_video and args.no_audio:
        print("ERROR: --no-video and --no-audio together would send nothing at all.",
              file=sys.stderr)
        sys.exit(1)

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        sys.exit(1)

    gop_frames = max(1, round(args.gop_seconds * args.framerate))
    if args.no_intra_refresh:
        x264_params = f"repeat-headers=1"
        gop_args = ["-g", str(gop_frames)]
    else:
        # bframes=0:ref=1 explicitly, matching what x264 forces anyway with
        # intra-refresh on -- silences its "not supported" warnings for
        # b-pyramid/ref>1 rather than leaving them looking like a problem.
        x264_params = "nal-hrd=cbr:force-cfr=1:intra-refresh=1:repeat-headers=1:bframes=0:ref=1"
        gop_args = ["-g", str(gop_frames)]  # x264 uses -g as the intra-refresh period too

    audio_kbps = f"{args.audio_bitrate}k"

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
    if not args.no_video:
        cmd += build_video_args(args)
    if not args.no_audio:
        cmd += build_audio_args(args)
    if args.duration:
        cmd += ["-t", str(args.duration)]

    if not args.no_video:
        if args.source == "device":
            # Convert whatever the device actually captures at down to the
            # requested link resolution/framerate here instead of at the
            # device input (see build_video_args) -- scale keeps aspect
            # ratio (padding to exactly fill --resolution) and fps does
            # simple frame drop/dup to hit the target rate regardless of
            # the device's own native one.
            w, h = args.resolution.split("x")
            cmd += ["-vf",
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={args.framerate}"]
        video_kbps = f"{args.video_bitrate}k"
        cmd += [
            "-pix_fmt", "yuv420p",
            "-c:v", "libx264", "-x264-params", x264_params,
            "-b:v", video_kbps, "-minrate", video_kbps, "-maxrate", video_kbps,
            "-bufsize", f"{args.video_bitrate * args.video_vbv_seconds:.1f}k",
        ] + gop_args
    else:
        cmd += ["-vn"]  # no video stream at all, not even a default one from an accidental video input

    if not args.no_audio:
        cmd += [
            "-c:a", "libopus", "-b:a", audio_kbps, "-vbr", "off", "-ac", "1",
            "-frame_duration", f"{args.audio_frame_duration:g}", "-ar", str(args.audio_rate),
        ]
    else:
        cmd += ["-an"]  # no audio stream at all, not even a default one from an accidental audio input
    # -flush_packets 1: see media_source_video.py's identical flag for the
    # full rationale (an untested-but-plausible fix for ffmpeg batching
    # several encoded packets in its own output buffer before actually
    # writing them to the pipe).
    cmd += ["-flush_packets", "1", "-f", "matroska", "-"]

    print(f"[media_source] running: {' '.join(cmd)}", file=sys.stderr)
    # See media_source_video.py's identical change for the full rationale.
    priority = {"creationflags": subprocess.ABOVE_NORMAL_PRIORITY_CLASS} if sys.platform == "win32" else {}
    proc = subprocess.run(cmd, **priority)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
