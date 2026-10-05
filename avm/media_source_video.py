#!/usr/bin/env python3
"""
Video-only counterpart to the old media_source.py -- writes a raw H.264
Annex-B elementary stream (no container) to stdout, so media_tx_framer.py
can frame it itself instead of relying on ffmpeg's Matroska muxer (which
is also why this project's audio got split into media_source_audio.py:
Matroska's muxer refuses some codecs -- e.g. Codec2 -- entirely, even
though ffmpeg can encode them fine standalone. A raw elementary stream
has no such restriction on either side).

Usage (paired with media_source_audio.py -- both are launched BY
media_tx_framer.py now, not piped into it, so it can read them
concurrently on two threads instead of needing OS-level pipe chaining
of two processes into one child's stdin):
    python media_tx_framer.py --fragment-size 1024 \\
        --video-source-cmd "python media_source_video.py --resolution 160x120 --framerate 12" \\
        --audio-source-cmd "python media_source_audio.py --audio-bitrate 10" \\
    | python hf_ofdm_tx.py --mode A --occupancy 80 --modulation qpsk \\
          --fragment-size 1024 --fragment-gap-ms 0 --stream ...

IMPORTANT (Windows): PowerShell's native `|` corrupts binary data
passed between processes -- run this only via media_tx_framer.py's own
subprocess.Popen orchestration (as above), never chained with a raw
PowerShell pipe.
"""
import argparse
import shutil
import subprocess
import sys

import avm_threads  # noqa: F401 -- Windows: helpers start without console windows

from media_source import build_video_args, list_devices


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["device", "test"], default="device",
                     help="'device' (default): capture from real hardware (--video-device). "
                          "'test': synthetic test pattern, no hardware needed.")
    ap.add_argument("--list-devices", action="store_true",
                     help="Print available capture device names (Windows/dshow) and exit.")
    ap.add_argument("--video-device", type=str, default="Integrated Camera",
                     help="Capture device name for --source device (default 'Integrated Camera').")
    ap.add_argument("--resolution", type=str, default="160x90",
                     help="Video resolution as WxH (default 160x90, 16:9 -- matching the combined "
                          "RX GUI's video pane). Any WxH works -- for --source device this is "
                          "already letterboxed/pillarboxed from whatever the camera's native aspect "
                          "ratio is (see the -vf filter below); --source test generates exactly "
                          "this resolution directly.")
    ap.add_argument("--framerate", type=float, default=12.0,
                     help="Video capture/encode framerate (default 12fps).")
    ap.add_argument("--video-bitrate", type=float, default=25.0,
                     help="Video bitrate in kbps (default 25).")
    ap.add_argument("--gop-seconds", type=float, default=2.0,
                     help="With intra-refresh (the default), how often the full picture has "
                          "finished one rolling refresh cycle (default 2s).")
    ap.add_argument("--video-vbv-seconds", type=float, default=2.0,
                     help="x264's VBV buffer size (-bufsize), as a multiple of --video-bitrate's "
                          "own 1-second budget (default 2.0, i.e. 2 seconds' worth). Confirmed for "
                          "real: the previous fixed 0.6s buffer was tight enough that ordinary scene "
                          "complexity forced x264's own rate control to skip/delay frames well below "
                          "--framerate in bursts (measured directly via media_tx_framer.py's "
                          "'video source rate' log line -- 9-13fps against a 12fps target, not a "
                          "link/packing/RX problem). A looser buffer lets it borrow bits across a "
                          "longer window instead of clamping so readily -- this link's own framer "
                          "already handles variably-sized frames/bursts fine (see append_video_packet), "
                          "so there's no downside to loosening this beyond a slightly bigger transient "
                          "backlog if a run of frames is genuinely complex. Lower this back toward the "
                          "old ~0.6 if you'd rather have looser frame timing than any startup/backlog "
                          "latency at all.")
    ap.add_argument("--no-intra-refresh", action="store_true",
                     help="Use conventional periodic full I-frames instead of x264's rolling "
                          "intra-refresh -- see the old media_source.py's docstring for the tradeoff.")
    ap.add_argument("--duration", type=float, default=None,
                     help="Stop after this many seconds (default: run until interrupted).")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        sys.exit(1)

    gop_frames = max(1, round(args.gop_seconds * args.framerate))
    if args.no_intra_refresh:
        x264_params = "repeat-headers=1"
    else:
        x264_params = "nal-hrd=cbr:force-cfr=1:intra-refresh=1:repeat-headers=1:bframes=0:ref=1"

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning"]
    cmd += build_video_args(args)
    if args.duration:
        cmd += ["-t", str(args.duration)]
    if args.source == "device":
        w, h = args.resolution.split("x")
        cmd += ["-vf",
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={args.framerate}"]
    video_kbps = f"{args.video_bitrate}k"
    cmd += [
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        # zerolatency: standard x264 tuning for exactly this situation --
        # real-time encode feeding a small-buffer, low-latency downstream
        # (no return channel to wait out a stall, minimal VBV slack) --
        # confirmed for real via a direct A/B measurement of access-unit
        # sizes (the thing that actually determines whether a frame needs
        # a second fragment) that it's a small, consistent improvement
        # over the CBR/intra-refresh params alone, with no downside for
        # this use case (this project already forces bframes=0 and reads
        # frames as they're encoded, so the reduced-reordering-delay
        # behavior zerolatency enables costs nothing extra here).
        "-tune", "zerolatency",
        "-x264-params", x264_params,
        "-b:v", video_kbps, "-minrate", video_kbps, "-maxrate", video_kbps,
        "-bufsize", f"{args.video_bitrate * args.video_vbv_seconds:.1f}k",
        "-g", str(gop_frames),
        # Untested hypothesis, but the numbers line up closely: a report
        # of the encoder's OWN per-frame timing (measured directly by
        # media_tx_framer.py's video_thread_fn, upstream of this link/
        # framer/RX entirely) showing a strikingly consistent ~1.2s gap
        # recurring throughout a session -- at 24.8kbps (~3100 B/s), a
        # 4096-byte stdio/AVIO output buffer takes ~1.32s to fill, close
        # to the observed value. ffmpeg's raw "-f h264 -" muxer writing
        # to a PIPE (not a tty) may be internally buffering several
        # frames before actually writing them out, rather than flushing
        # each access unit as it's encoded -- which would make
        # media_tx_framer.py see nothing for over a second, then a whole
        # burst at once, with NOTHING actually lost (matches "no drops,
        # no CRC errors" reports so far) but visibly uneven live
        # playback. -flush_packets 1 tells ffmpeg to flush its output
        # after every packet instead of batching. Not reproduced on this
        # dev machine (steady 12.0fps/~94ms max gap with or without this
        # flag here) -- likely ffmpeg-build- or OS-scheduling-dependent,
        # so this is a real experiment, not a confirmed fix. Harmless if
        # wrong: forcing more frequent flushes can only ever help or be a
        # no-op for a real-time low-bitrate stream like this one.
        "-flush_packets", "1",
        "-f", "h264", "-",
    ]

    print(f"[media_source_video] running: {' '.join(cmd)}", file=sys.stderr)
    # media_tx_gui.py now raises hf_ofdm_tx.py's OWN priority above
    # normal (it's the one process with a genuine real-time deadline --
    # see its own comment) -- confirmed for real that doing so alone
    # didn't fix a report of cyclic video slowdowns, and CPU usage stays
    # comfortably unmaxed throughout, which together point at Windows
    # SCHEDULING (not aggregate CPU%) as the actual mechanism: a
    # high-priority process can preempt/starve a normal-priority one in
    # bursts frequently enough to disrupt real-time pacing (ffmpeg's -re
    # here) without ever showing up as high aggregate CPU load. Matching
    # ffmpeg's own priority up narrows that gap instead of leaving it at
    # the bottom of the ladder relative to hf_ofdm_tx.py.
    priority = {"creationflags": subprocess.ABOVE_NORMAL_PRIORITY_CLASS} if sys.platform == "win32" else {}
    proc = subprocess.run(cmd, **priority)
    sys.exit(proc.returncode)


if __name__ == "__main__":
    main()
