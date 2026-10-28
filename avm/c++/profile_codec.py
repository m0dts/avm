"""Where the C++ codec's time goes, per stage (wvt_profile), on real footage.

    python profile_codec.py CLIP.bgr WxH frame_bytes [frames] [step]"""
import ctypes
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import cv2
import wavelet_codec_cpp as cpp

clip, size, fb = sys.argv[1], sys.argv[2], int(sys.argv[3])
nf = int(sys.argv[4]) if len(sys.argv) > 4 else 30
step = int(sys.argv[5]) if len(sys.argv) > 5 else 2
W, H = (int(v) for v in size.split("x"))
src = np.fromfile(clip, np.uint8).reshape(-1, 288, 512, 3)[::step][:nf]
fr = []
for img in src:
    yuv = cv2.cvtColor(cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2YUV_I420)
    fr.append((yuv[:H], yuv[H:H + H // 4].reshape(H // 2, W // 2), yuv[H + H // 4:].reshape(H // 2, W // 2)))
prof = (ctypes.c_double * 16)()
cpp._lib.wvt_profile.argtypes = [ctypes.c_void_p]
enc, dec = cpp.WaveletCodec(W, H, fb), cpp.WaveletCodec(W, H, fb)
cpp._lib.wvt_profile(prof)          # clear
te = td = 0.0
for f in fr:
    t = time.perf_counter(); p = enc.encode(*f); te += time.perf_counter() - t
    t = time.perf_counter(); dec.decode(p); td += time.perf_counter() - t
cpp._lib.wvt_profile(prof)
n = len(fr)
names = {0: "motion search", 1: "move reference", 2: "analysis DWT", 3: "class weights", 4: "predict+quantise",
         5: "bit-plane coder", 6: "update+round trip", 8: "move reference", 9: "predict", 10: "bit-plane coder",
         11: "update+round trip", 12: "picture (inverse DWT)"}
print(f"{W}x{H} {fb} B, {n} frames: encode {1000 * te / n:.2f} ms, decode {1000 * td / n:.2f} ms per frame")
print("  encoder:", ", ".join(f"{names[i]} {1000 * prof[i] / n:.2f}" for i in range(7)))
print("  decoder:", ", ".join(f"{names[i]} {1000 * prof[i] / n:.2f}" for i in range(8, 13)))
