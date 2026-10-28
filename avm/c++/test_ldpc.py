"""ldpc.cpp against hf_ofdm_ldpc.py: identical codewords, identical decoded
bits, codeword/None and per-block iteration counts on noisy LLRs, from easy
to failing SNRs; then speed.

    python test_ldpc.py [trials]"""
import ctypes
import os
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import hf_ofdm_ldpc as L

sys.path.insert(0, here)
from _avmlib import lib as _avm_lib  # noqa: E402

lib = _avm_lib()
P = ctypes.c_void_p
lib.ldpc_coded_len.argtypes = [ctypes.c_longlong]
lib.ldpc_coded_len.restype = ctypes.c_longlong
lib.ldpc_encode.argtypes = [P, ctypes.c_longlong, P]
lib.ldpc_decode.argtypes = [P, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_int, ctypes.c_double, ctypes.c_int,
                            P, P, P]
lib.ldpc_decode.restype = ctypes.c_int


def c_encode(bits):
    out = np.empty(lib.ldpc_coded_len(len(bits)), np.uint8)
    b = np.ascontiguousarray(bits, np.uint8)
    lib.ldpc_encode(b.ctypes.data, len(b), out.ctypes.data)
    return out


def c_decode(llr, n):
    llr = np.ascontiguousarray(llr, np.float64)
    bits = np.empty(n, np.uint8)
    cw = np.empty(lib.ldpc_coded_len(n), np.uint8)
    it = ctypes.c_longlong(0)
    ok = lib.ldpc_decode(llr.ctypes.data, len(llr), n, L.LDPC_MAX_ITER, 0.85, L.LDPC_STALL_ITER,
                         bits.ctypes.data, cw.ctypes.data, ctypes.byref(it))
    return bits, (cw if ok == 1 else None), it.value


def py_iters(llr, n):
    """Per-block iterations from the Python decoder (as ldpc_decode_soft runs it)."""
    Z, N, K, nb = L.ldpc_block_params(n)
    tot = 0
    last = n - (nb - 1) * K
    for b in range(nb):
        blk = np.array(llr[b * N:(b + 1) * N], np.float64)
        if b == nb - 1 and last < K:
            blk[last:K] = 100.0
        tot += L.ldpc_decode_block(blk, Z=Z)[1]
    return tot


trials = int(sys.argv[1]) if len(sys.argv) > 1 else 300
rng = np.random.default_rng(1)
bad = 0
n_enc = n_dec = 0
conv = {True: 0, False: 0}
for t in range(trials):
    n = int(rng.choice([1, 10, 200, 324, 325, 500, 648, 649, 1000, 4000, 8224, 16416, rng.integers(1, 20000)]))
    bits = rng.integers(0, 2, n).astype(np.uint8)
    a, b = L.ldpc_encode(bits), c_encode(bits)
    n_enc += 1
    if not np.array_equal(a, b):
        print(f"  encode differs: {n} bits")
        bad += 1
        continue
    for snr_db in (0.5, 1.5, 2.5, 4.0):
        sigma = np.sqrt(1 / (2 * 0.5 * 10 ** (snr_db / 10)))     # rate 1/2, Eb/N0
        y = (1.0 - 2.0 * a) + sigma * rng.standard_normal(len(a))
        llr = 2 * y / sigma ** 2
        pb, pcw = L.ldpc_decode_soft_codeword(llr, n)
        cb, ccw, cit = c_decode(llr, n)
        pit = py_iters(llr, n)
        n_dec += 1
        same = (np.array_equal(pb, cb) and (pcw is None) == (ccw is None)
                and (pcw is None or np.array_equal(pcw, ccw)) and pit == cit)
        conv[pcw is not None] += 1
        if not same:
            print(f"  decode differs: {n} bits, {snr_db} dB (iterations {pit} / {cit})")
            bad += 1
print(f"{n_enc} encodes, {n_dec} decodes ({conv[True]} converged, {conv[False]} failed): "
      f"{'ALL IDENTICAL' if bad == 0 else f'{bad} MISMATCHES'}")

# speed: one 1 KB fragment's worth (8224 bits, 9 blocks at Z=81), at three SNRs
bits = rng.integers(0, 2, 8224).astype(np.uint8)
cw = L.ldpc_encode(bits)
for snr_db in (1.5, 2.5, 5.0):
    sigma = np.sqrt(1 / (2 * 0.5 * 10 ** (snr_db / 10)))
    llr = 2 * ((1.0 - 2.0 * cw) + sigma * rng.standard_normal(len(cw))) / sigma ** 2
    reps = 20
    t0 = time.perf_counter()
    for _ in range(reps):
        L.ldpc_decode_soft_codeword(llr, 8224)
    tp = (time.perf_counter() - t0) / reps
    t0 = time.perf_counter()
    for _ in range(reps):
        c_decode(llr, 8224)
    tc = (time.perf_counter() - t0) / reps
    print(f"decode 1 KB fragment at {snr_db} dB: python {1000 * tp:.2f} ms, C++ {1000 * tc:.2f} ms ({tp / tc:.1f}x)")
t0 = time.perf_counter()
for _ in range(50):
    L.ldpc_encode(bits)
tp = (time.perf_counter() - t0) / 50
t0 = time.perf_counter()
for _ in range(50):
    c_encode(bits)
tc = (time.perf_counter() - t0) / 50
print(f"encode 1 KB fragment: python {1000 * tp:.2f} ms, C++ {1000 * tc:.2f} ms ({tp / tc:.1f}x)")
