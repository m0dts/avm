"""npy_rng.h against numpy itself: default_rng(seed).integers / .random /
.permutation, for AVM's seeds and others, many lengths."""
import ctypes
import os
import subprocess
import sys

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "_npy_rng_test.cpp")
open(src, "w").write(r'''
#include "npy_rng.h"
#include <cstring>
#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif
API void ints(unsigned long long seed, long long lo, long long hi, long long n, long long* out) {
    npy::Generator g(seed); auto v = g.integers(lo, hi, n); std::memcpy(out, v.data(), 8 * n);
}
API void rnd(unsigned long long seed, long long n, double* out) {
    npy::Generator g(seed); auto v = g.random(n); std::memcpy(out, v.data(), 8 * n);
}
API void perm(unsigned long long seed, long long n, long long* out) {
    npy::Generator g(seed); auto v = g.permutation(n); std::memcpy(out, v.data(), 8 * n);
}
''')
lib_name = os.path.join(here, "_npy_rng_test.dll" if sys.platform == "win32" else "lib_npy_rng_test.so")
if sys.platform == "win32":
    cmd = [sys.executable, "-m", "ziglang", "c++", "-O2", "-std=c++17", "-shared", "-target", "x86_64-windows-gnu",
           "-o", lib_name, src]
else:
    cmd = ["g++", "-O2", "-std=c++17", "-shared", "-fPIC", "-o", lib_name, src]
subprocess.run(cmd, check=True, cwd=here, stderr=subprocess.DEVNULL)
lib = ctypes.CDLL(lib_name)
P, LL, U = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_ulonglong
lib.ints.argtypes = [U, LL, LL, LL, P]
lib.rnd.argtypes = [U, LL, P]
lib.perm.argtypes = [U, LL, P]

bad = 0
seeds = [424242, 12345, 999, 4242, 20261019, 777, 778, 780, 424243, 424246, 0, 1, 2 ** 32 + 5, 123456789012]
for seed in seeds:
    for n in (1, 2, 7, 100, 648, 1296, 8254, 16416, 65537):
        for lo, hi in ((0, 2), (0, 4)):
            o = np.empty(n, np.int64)
            lib.ints(seed, lo, hi, n, o.ctypes.data)
            bad += not np.array_equal(o, np.random.default_rng(seed).integers(lo, hi, size=n))
        o = np.empty(n, np.float64)
        lib.rnd(seed, n, o.ctypes.data)
        bad += not np.array_equal(o, np.random.default_rng(seed).random(n))
        o = np.empty(n, np.int64)
        lib.perm(seed, n, o.ctypes.data)
        bad += not np.array_equal(o, np.random.default_rng(seed).permutation(n))
print("npy_rng:", "ALL IDENTICAL to numpy" if bad == 0 else f"{bad} MISMATCHES")
