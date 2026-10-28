"""wvt_create_auto (the codec's layout made in C++) against wavelet_codec.py:
frames encoded by the PYTHON codec, decoded by a C++ codec built with
wvt_create_auto -- the pictures must be bit-identical, which needs every
table (subband weights, order, refresh tiles, ...) to match exactly. Several
sizes, with motion.

    python test_wvt_auto.py"""
import ctypes
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, ".."))
import wavelet_codec as py
from _avmlib import lib

L = lib()
P, I64 = ctypes.c_void_p, ctypes.c_int64
L.wvt_create_auto.restype = P
L.wvt_create_auto.argtypes = [I64, I64, I64, ctypes.c_double, I64, ctypes.c_int, ctypes.c_int, ctypes.c_int]
L.wvt_decode.argtypes = [P, P, I64, P, P, P]
L.wvt_decode.restype = ctypes.c_int
L.wvt_free.argtypes = [P]

rng = np.random.default_rng(9)
bad = 0
for (W, H, fb) in ((192, 112, 125), (256, 144, 442), (384, 224, 1500), (352, 192, 300), (640, 352, 3000)):
    enc = py.WaveletCodec(W, H, fb)
    dec = L.wvt_create_auto(W, H, 64, 0.996, 60, 1, 1, -1)
    ref = py.WaveletCodec(W, H, 64)
    yy, xx = np.mgrid[0:H, 0:W]
    diff = 0
    for k in range(24):
        y = (128 + 60 * np.sin((xx + 5 * k) / 23.0) * np.cos((yy - 3 * k) / 17.0)
             + rng.normal(0, 3, (H, W))).clip(0, 255).astype(np.uint8)
        u = np.full((H // 2, W // 2), 120 + k, np.uint8)
        v = np.full((H // 2, W // 2), 136 - k, np.uint8)
        pkt = enc.encode(y, u, v)
        a = ref.decode(pkt)
        oy, ou, ov = (np.empty((H, W), np.uint8), np.empty((H // 2, W // 2), np.uint8),
                      np.empty((H // 2, W // 2), np.uint8))
        buf = np.frombuffer(pkt, np.uint8)
        L.wvt_decode(dec, buf.ctypes.data, len(buf), oy.ctypes.data, ou.ctypes.data, ov.ctypes.data)
        diff += int(np.count_nonzero(oy != a[0]) + np.count_nonzero(ou != a[1]) + np.count_nonzero(ov != a[2]))
    L.wvt_free(dec)
    bad += diff != 0
    print(f"{W}x{H}: 24 frames, {diff} differing pixels")
print("ALL BIT-IDENTICAL" if bad == 0 else f"{bad} SIZES DIFFER")
