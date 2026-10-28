"""Glitches in a received test tone: a recording's audio (avm_gui's RX
recorder: exactly what was played), checked 10 ms at a time against a
steady tone -- dropouts (silence), level jumps, and phase breaks.

    python check_tone.py RECORDING.mp4 [--tone 800]   (needs ffmpeg)"""
import argparse
import subprocess
import sys

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("file")
ap.add_argument("--tone", type=float, default=800)
ap.add_argument("--ffmpeg", default="ffmpeg")
a = ap.parse_args()
RATE = 48000
pcm = subprocess.run([a.ffmpeg, "-v", "error", "-i", a.file, "-vn", "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
                     capture_output=True).stdout
x = np.frombuffer(pcm, np.int16).astype(float)
print(f"{len(x) / RATE:.1f} s of audio")
blk = RATE // 100
n = len(x) // blk
rms = np.array([np.sqrt(np.mean(x[i * blk:(i + 1) * blk] ** 2)) for i in range(n)])
level = np.median(rms[rms > 0]) if np.any(rms > 0) else 0
silent = rms < 0.1 * level
# phase continuity: demodulate the tone, look for jumps in its phase between blocks
t = np.arange(len(x)) / RATE
z = x * np.exp(-2j * np.pi * a.tone * t)
ph = np.array([np.angle(np.sum(z[i * blk:(i + 1) * blk])) for i in range(n)])
dph = np.angle(np.exp(1j * np.diff(ph)))
drift = np.median(dph)
jumps = np.abs(np.angle(np.exp(1j * (dph - drift)))) > 0.5
start = np.argmax(~silent)
end = n - np.argmax(~silent[::-1])
mid = slice(start + 50, end - 50)       # past the start, before the end
idx = np.arange(n)[mid]
print(f"level {level:.0f}; within the steady part ({(end - start) / 100:.1f} s):")
print(f"  silent 10 ms blocks: {int(silent[mid].sum())}  at {[round(i / 100, 2) for i in idx[silent[mid]]][:20]}")
pj = idx[:-1][jumps[mid.start:mid.stop - 1]]
print(f"  phase breaks: {len(pj)}  at {[round(i / 100, 2) for i in pj][:20]}")
sys.exit(0 if not silent[mid].any() and len(pj) == 0 else 1)
