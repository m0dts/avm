"""Python wavelet codec vs the C++ one (wavelet_codec_cpp) on the same
frames: packets and decoded pictures must be bit-identical, and each must
decode the other's packets. Prints encode/decode time per frame.

    python compare_codecs.py [CLIP.bgr [frames]]
CLIP: raw BGR 512x288 frames (as the codec tests use); none: a synthetic one."""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))           # wavelet_codec_cpp + the library
sys.path.insert(1, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))   # C:\avm: wavelet_codec
import cv2
import wavelet_codec as py
import wavelet_codec_cpp as cpp

clip = sys.argv[1] if len(sys.argv) > 1 else None
nf = int(sys.argv[2]) if len(sys.argv) > 2 else 40
if clip:
    src = np.fromfile(clip, np.uint8).reshape(-1, 288, 512, 3)
else:
    # no clip: a synthetic one -- moving gradients, a moving block and noise,
    # so motion search, refresh and every subband get exercised
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:288, 0:512]
    src = np.empty((nf * 2, 288, 512, 3), np.uint8)
    for k in range(len(src)):
        base = 128 + 60 * np.sin((xx + 3 * k) / 37.0) * np.cos((yy - 2 * k) / 23.0)
        img = np.stack([base, np.roll(base, k, 1), 255 - base], -1)
        img[60 + k % 90:120 + k % 90, 100 + 2 * k:180 + 2 * k] = (30, 200, 90)
        src[k] = np.clip(img + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8)


def frames(W, H, step):
    for img in src[::step][:nf]:
        yuv = cv2.cvtColor(cv2.resize(img, (W, H), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2YUV_I420)
        yield yuv[:H], yuv[H:H + H // 4].reshape(H // 2, W // 2), yuv[H + H // 4:].reshape(H // 2, W // 2)


all_ok = True
for (W, H, fb, step) in ((192, 112, 125, 6), (256, 144, 442, 2), (384, 224, 1500, 1), (352, 192, 300, 3)):
    ep, ec = py.WaveletCodec(W, H, fb), cpp.WaveletCodec(W, H, fb)
    dp, dc = py.WaveletCodec(W, H, fb), cpp.WaveletCodec(W, H, fb)
    xp, xc = py.WaveletCodec(W, H, fb), cpp.WaveletCodec(W, H, fb)   # cross: C++ decodes Python, Python decodes C++
    # warm-up (numba compile) outside the timing, then back to the start as AVM's TX does
    for e in (ep, ec):
        e.encode(np.zeros((H, W), np.uint8), np.zeros((H // 2, W // 2), np.uint8), np.zeros((H // 2, W // 2), np.uint8))
        e.seq = 0
        e.reset()
    dp.decode(ep.encode(*next(frames(W, H, step)))); dp.reset(); ep.reset(); ep.seq = 0
    ec.encode(*next(frames(W, H, step))); ec.reset(); ec.seq = 0
    te = {"py": 0.0, "cpp": 0.0}
    td = {"py": 0.0, "cpp": 0.0}
    n = pk_bad = pic_bad = cross_bad = 0
    moved = fields = 0
    for f in frames(W, H, step):
        t = time.perf_counter(); a = ep.encode(*f); te["py"] += time.perf_counter() - t
        t = time.perf_counter(); b = ec.encode(*f); te["cpp"] += time.perf_counter() - t
        pk_bad += a != b
        moved += bool(a[6] & 0x80)
        fields += bool(a[5] & 0x80)
        t = time.perf_counter(); pa = dp.decode(a); td["py"] += time.perf_counter() - t
        t = time.perf_counter(); pb = dc.decode(b); td["cpp"] += time.perf_counter() - t
        pic_bad += any(not np.array_equal(x, y) for x, y in zip(pa, pb))
        qa, qb = xc.decode(a), xp.decode(b)
        cross_bad += any(not np.array_equal(x, y) for x, y in zip(qa, pa)) + any(not np.array_equal(x, y) for x, y in zip(qb, pb))
        n += 1
    ok = pk_bad == pic_bad == cross_bad == 0
    all_ok &= ok
    print(f"{W}x{H} {fb} B/frame, {n} frames ({moved} moved, {fields} with block motion): "
          f"packets differ {pk_bad}, pictures differ {pic_bad}, cross-decode differ {cross_bad} -> {'OK' if ok else 'MISMATCH'}")
    print(f"   encode  python {1000 * te['py'] / n:6.2f} ms   C++ {1000 * te['cpp'] / n:6.2f} ms   "
          f"({te['py'] / max(te['cpp'], 1e-9):.1f}x)")
    print(f"   decode  python {1000 * td['py'] / n:6.2f} ms   C++ {1000 * td['cpp'] / n:6.2f} ms   "
          f"({td['py'] / max(td['cpp'], 1e-9):.1f}x)")
print("ALL BIT-IDENTICAL" if all_ok else "SOME MISMATCHES")
