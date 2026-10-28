"""rx_frontend.FrontEnd with its numba kernel against the same with the C++
kernel (frontend_cpp.process): random-sized chunks of a noisy 550 kS/s
stream, with and without an LO offset, IQ correction kicking in after 1 s;
then speed per second of input."""
import os
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as ofdm
import rx_frontend
import frontend_cpp

numba_process = rx_frontend._process
rng = np.random.default_rng(29)
fs = 550_000.0
n = int(2.5 * fs)
x = (0.2 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
x.imag += 0.05 * x.real                                  # some IQ imbalance to correct
for mode, occ, lo in (("A", 20, 0.0), ("B", 80, 100_000.0), ("VU", 160, 0.0), ("S", 40, 100_000.0)):
    cfg = ofdm.build_config(mode, occ)
    taps = ofdm.design_lowpass_fir(min(cfg.fs / 2, 0.45 * fs), fs, numtaps=129)
    outs = {}
    for name, kern in (("numba", numba_process), ("C++", frontend_cpp.process)):
        rx_frontend._process = kern
        fe = rx_frontend.FrontEnd(fs, cfg.fs, taps, lo_offset_hz=lo)
        crng = np.random.default_rng(1)
        parts, i = [], 0
        while i < n:
            k = int(crng.integers(500, 40000))
            parts.append(fe.process(x[i:i + k]))
            i += k
        outs[name] = np.concatenate(parts)
    a, b = outs["numba"], outs["C++"]
    d = float(np.max(np.abs(a - b))) / float(np.max(np.abs(a))) if len(a) == len(b) else float("inf")
    print(f"{mode} {occ} kHz LO {lo / 1e3:.0f} kHz: {len(a)} / {len(b)} samples, worst relative difference {d:.1e}")
rx_frontend._process = numba_process

cfg = ofdm.build_config("A", 20)
taps = ofdm.design_lowpass_fir(cfg.fs / 2, fs, numtaps=129)
for name, kern in (("numba", numba_process), ("C++", frontend_cpp.process)):
    rx_frontend._process = kern
    fe = rx_frontend.FrontEnd(fs, cfg.fs, taps, lo_offset_hz=100_000.0)
    fe.process(x[:20000])
    t0 = time.perf_counter()
    for i in range(20000, 20000 + int(fs), 32768):
        fe.process(x[i:i + 32768])
    print(f"1 s of 550 kS/s -> mode A 20 kHz, LO shift: {name} {1000 * (time.perf_counter() - t0):.1f} ms")
