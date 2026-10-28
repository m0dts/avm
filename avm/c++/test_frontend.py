"""frontend_cpp's kernels against hf_ofdm_common's: the IQ transform,
linear_resample, the incremental low-pass and resampler fed a growing
buffer in random-sized chunks (as the live receiver does), and the
streaming low-pass; then speed per second of 550 kS/s input."""
import os
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as P
import frontend_cpp

# a second, patched copy of the module's functions (P keeps the originals)
orig = {k: getattr(P, k) for k in ("_iq_apply", "linear_resample", "apply_lowpass_filter_incremental",
                                    "stream_lowpass_filter", "linear_resample_incremental")}


class Patched:
    pass


Patched.GrowableComplexArray = P.GrowableComplexArray
for k in ("INCREMENTAL_CACHE_HARD_CAP", "INCREMENTAL_CACHE_TRIM_MARGIN", "INCREMENTAL_CACHE_TRIM_INTERVAL"):
    setattr(Patched, k, getattr(P, k))
frontend_cpp.install(Patched)

rng = np.random.default_rng(3)
fs_in = 550_000.0
x = ((rng.standard_normal(1_200_000) + 1j * rng.standard_normal(1_200_000)) * 0.3).astype(np.complex64)


def report(name, a, b, exact):
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        print(f"{name:34s} LENGTH {a.shape} vs {b.shape}")
        return
    d = float(np.max(np.abs(a - b))) if len(a) else 0.0
    ok = d == 0 if exact else d < 1e-12
    print(f"{name:34s} {'identical' if d == 0 else f'max diff {d:.1e}'}  {'OK' if ok else 'FAIL'}")


report("iq_apply", orig["_iq_apply"](x.astype(np.complex128), 0.01, -0.02, 0.03, 1.04),
       Patched._iq_apply(x, 0.01, -0.02, 0.03, 1.04), True)
for fs_out in (80_000 * 1.0, 250_000.0, 41_666.666666666664, 26_666.666666666668):
    report(f"linear_resample -> {fs_out:.0f}", orig["linear_resample"](x[:300_000], fs_in, fs_out),
           Patched.linear_resample(x[:300_000], fs_in, fs_out), True)

taps = P.design_lowpass_fir(45_000, fs_in)
for name, fs_out in (("A 20 kHz", P.build_config("A", 20).fs), ("VU 160 kHz", P.build_config("VU", 160).fs)):
    ca, cb, ra, rb = {}, {}, {}, {}
    n = 0
    while n < len(x):
        n = min(len(x), n + int(rng.integers(1000, 60000)))
        ya = orig["apply_lowpass_filter_incremental"](x[:n], taps, ca)
        yb = Patched.apply_lowpass_filter_incremental(x[:n], taps, cb)
        za = orig["linear_resample_incremental"](ya[:len(ya)], fs_in, fs_out, ra)
        zb = Patched.linear_resample_incremental(yb[:len(yb)], fs_in, fs_out, rb)
    report(f"incremental lowpass ({name})", ya[:len(ya)], yb[:len(yb)], False)
    report(f"incremental resample ({name})", za[:len(za)], zb[:len(zb)], False)
sa, sb = {}, {}
outa, outb = [], []
for i in range(0, 200_000, 7000):
    outa.append(orig["stream_lowpass_filter"](x[i:i + 7000], taps, sa))
    outb.append(Patched.stream_lowpass_filter(x[i:i + 7000], taps, sb))
report("stream lowpass", np.concatenate(outa), np.concatenate(outb), False)

# speed: one second of 550 kS/s input through IQ + low-pass + resample
one = x[:550_000]
for label, F in (("python", orig), ("C++", {k: getattr(Patched, k) for k in orig})):
    t0 = time.perf_counter()
    for _ in range(3):
        y = F["_iq_apply"](one.astype(np.complex128), 0.01, -0.02, 0.03, 1.04)
        y = F["apply_lowpass_filter_incremental"](y, taps, {})
        F["linear_resample"](y[:len(y)], fs_in, 80_000.0)
    print(f"front end, 1 s of 550 kS/s: {label} {(time.perf_counter() - t0) / 3 * 1000:.0f} ms")
