"""fft.h against numpy.fft: forward and inverse, double and float, for
many lengths (small primes, large primes via Bluestein, AVM's sizes)."""
import ctypes
import os
import subprocess
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "_fft_test.cpp")
open(src, "w").write(r'''
#include "fft.h"
#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif
API void fwd_d(const std::complex<double>* in, std::complex<double>* out, int n, int inv) {
    auto& p = fft::plan<double>(n); if (inv) p.inverse(in, out); else p.forward(in, out);
}
API void fwd_f(const std::complex<float>* in, std::complex<float>* out, int n, int inv) {
    auto& p = fft::plan<float>(n); if (inv) p.inverse(in, out); else p.forward(in, out);
}
''')
lib_name = os.path.join(here, "_fft_test.dll" if sys.platform == "win32" else "lib_fft_test.so")
if sys.platform == "win32":
    cmd = [sys.executable, "-m", "ziglang", "c++", "-O3", "-std=c++17", "-shared", "-target", "x86_64-windows-gnu",
           "-o", lib_name, src]
else:
    cmd = ["g++", "-O3", "-std=c++17", "-shared", "-fPIC", "-o", lib_name, src]
subprocess.run(cmd, check=True, cwd=here)
lib = ctypes.CDLL(lib_name)
P = ctypes.c_void_p
lib.fwd_d.argtypes = lib.fwd_f.argtypes = [P, P, ctypes.c_int, ctypes.c_int]

rng = np.random.default_rng(3)
worst = {np.complex128: 0.0, np.complex64: 0.0}
sizes = list(range(1, 130)) + [256, 490, 498, 997, 1009, 2048, 3 * 7 * 6287, 132055, 4096 * 3, 65536, 17 * 19 * 23]
for n in sizes:
    x = rng.standard_normal(n) + 1j * rng.standard_normal(n)
    for dt, fn in ((np.complex128, lib.fwd_d), (np.complex64, lib.fwd_f)):
        xi = x.astype(dt)
        for inv in (0, 1):
            ref = np.fft.ifft(x) * n if inv else np.fft.fft(x)
            o = np.empty(n, dt)
            fn(xi.ctypes.data, o.ctypes.data, n, inv)
            err = np.max(np.abs(o - ref)) / (np.sqrt(n) * np.max(np.abs(x)))
            worst[dt] = max(worst[dt], err)
print(f"worst relative error: double {worst[np.complex128]:.2e}, float {worst[np.complex64]:.2e}")
for n in (490, 132055, 132027):
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    o = np.empty(n, np.complex64)
    fn = lib.fwd_f
    fn(x.ctypes.data, o.ctypes.data, n, 0)
    reps = 20 if n > 1000 else 2000
    t0 = time.perf_counter()
    for _ in range(reps):
        np.fft.fft(x)
    tn = (time.perf_counter() - t0) / reps
    t0 = time.perf_counter()
    for _ in range(reps):
        fn(x.ctypes.data, o.ctypes.data, n, 0)
    tc = (time.perf_counter() - t0) / reps
    print(f"n={n}: numpy {tn * 1e6:.0f} us, C++ float {tc * 1e6:.0f} us")
