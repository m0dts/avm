#!/usr/bin/env python3
"""
Wavelet-codec counterpart to media_source_video.py -- captures raw
YUV 4:2:0 frames via ffmpeg and encodes each one with wavelet_codec.py
into a packet of EXACTLY --frame-bytes (or bitrate/fps/8) bytes: true
per-frame constant bitrate, leaky temporal prediction, no lookahead
(see wavelet_codec.py's docstring).

stdout: a sequence of [2-byte big-endian length][packet] records, read
by media_tx_framer.py --video-codec wavelet. As with the other sources,
launch this only via media_tx_framer.py's own subprocess orchestration,
never through a raw PowerShell pipe:

    python media_tx_framer.py --fragment-size 1024 --video-codec wavelet \\
        --video-framerate 12 --video-resolution 256x144 \\
        --video-source-cmd "python media_source_wavelet.py --resolution 256x144 --framerate 12 --video-bitrate 16" \\
        ...
"""
import argparse
import shutil
import struct
import subprocess
import sys
import time

import numpy as np

import avm_threads
from media_source import build_video_args, list_devices
from wavelet_codec import WaveletCodec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["device", "test"], default="device")
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--video-device", type=str, default="Integrated Camera")
    ap.add_argument("--resolution", type=str, default="256x144",
                    help="WxH, multiple of 32x16 (default 256x144 = 144p).")
    ap.add_argument("--framerate", type=float, default=12.0)
    ap.add_argument("--video-bitrate", type=float, default=16.0,
                    help="Video bitrate in kbps (default 16) -- sets the per-frame byte budget "
                         "to round(kbps*1000/8/fps), unless --frame-bytes is given.")
    ap.add_argument("--frame-bytes", type=int, default=None,
                    help="Exact encoded bytes per frame, overriding --video-bitrate.")
    ap.add_argument("--leak", type=float, default=0.996,
                    help="Maximum temporal prediction strength, 0..0.996 (default 0.996). Higher = "
                         "better picture on static scenes; recovery is bounded by --refresh-seconds. "
                         "0 = intra-only (every frame independent).")
    ap.add_argument("--refresh-seconds", type=float, default=5.0,
                    help="Re-send the whole picture from scratch, spread evenly over every N "
                         "seconds (default 5; max 255 frames; 0 = off). Bounds late-join and "
                         "loss recovery -- with a high --leak and 0 here, a late receiver never "
                         "catches up. Shorter = faster join, lower quality.")
    ap.add_argument("--rd-trial", action="store_true",
                    help="Also trial-encode each frame with every weight at the full leak and keep "
                         "the better one: ~+0.1 dB, ~1.5x encode time (~23 -> ~35 ms/frame at "
                         "256x144), but ~2x slower recovery after a lost packet. Same bitstream.")
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--preview", type=str, default=None,
                    help="Also publish every input frame to this shared-memory file for a live "
                         "preview (see preview_shm.py; used by the touch GUI).")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return
    if shutil.which("ffmpeg") is None:
        print("ERROR: ffmpeg not found on PATH.", file=sys.stderr)
        sys.exit(1)

    w, h = (int(x) for x in args.resolution.lower().split("x"))
    frame_bytes = args.frame_bytes or round(args.video_bitrate * 1000 / 8 / args.framerate)
    codec = WaveletCodec(w, h, frame_bytes, leak=args.leak,
                         refresh_frames=round(args.refresh_seconds * args.framerate), rd_trial=args.rd_trial)
    # JIT warm-up before capture starts: quick once compiled, but the first
    # run on a machine compiles the codec (minutes on a slow CPU). The GUI
    # shows these two lines in its preview.
    print("[media_source_wavelet] preparing video codec...", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    codec.encode(np.zeros((h, w), np.uint8), np.zeros((h // 2, w // 2), np.uint8),
                 np.zeros((h // 2, w // 2), np.uint8))
    print(f"[media_source_wavelet] video codec ready ({time.perf_counter() - t0:.1f} s)",
          file=sys.stderr, flush=True)
    codec.seq = 0
    codec.reset()

    # Low-delay input: no probing/buffering beyond what's needed.
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
           "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32"]
    cmd += build_video_args(args)
    if args.duration:
        cmd += ["-t", str(args.duration)]
    if args.source == "device":
        cmd += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                       f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={args.framerate}"]
    cmd += ["-pix_fmt", "yuv420p", "-f", "rawvideo", "-"]
    print(f"[media_source_wavelet] {frame_bytes} B/frame = "
          f"{frame_bytes * 8 * args.framerate / 1000:.2f} kbps; running: {' '.join(cmd)}", file=sys.stderr)

    preview = None
    if args.preview:
        from preview_shm import PreviewWriter
        try:
            preview = PreviewWriter(args.preview, w, h)
        except OSError as e:
            print(f"[media_source_wavelet] preview disabled: {e}", file=sys.stderr)

    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, bufsize=0, **avm_threads.die_with_parent())
    ysz, csz = w * h, (w // 2) * (h // 2)
    fsz = ysz + 2 * csz
    out = sys.stdout.buffer
    buf = bytearray()
    n = 0
    t_enc = 0.0
    try:
        while True:
            while len(buf) < fsz:
                chunk = proc.stdout.read(fsz - len(buf))
                if not chunk:
                    return
                buf.extend(chunk)
            frame = bytes(buf[:fsz])
            f = np.frombuffer(frame, np.uint8)
            del buf[:fsz]
            if preview is not None:
                preview.write(frame)
            t0 = time.perf_counter()
            pkt = codec.encode(f[:ysz].reshape(h, w),
                               f[ysz:ysz + csz].reshape(h // 2, w // 2),
                               f[ysz + csz:].reshape(h // 2, w // 2))
            t_enc += time.perf_counter() - t0
            out.write(struct.pack(">H", len(pkt)) + pkt)
            out.flush()
            n += 1
            if n % 100 == 0:
                print(f"[media_source_wavelet] {n} frames, avg encode {t_enc / n * 1000:.1f} ms",
                      file=sys.stderr)
    except (BrokenPipeError, OSError):
        pass
    finally:
        proc.kill()


if __name__ == "__main__":
    main()
