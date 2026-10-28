"""Mode SH receive kernels (sch_rx_cpp) against hf_sch.py's, on bursts from
the Python transmitter through noise, two echoes, a frequency offset: detect
(t0, cfo, variant), header, and the frame decode as receive_one_fragment
runs it (fine frequency, header, turbo decode). Same decisions, metrics to
rounding. Then speed.

    python test_sch_rx.py [trials_per_config]"""
import os
import sys
import time
from collections import defaultdict

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as ofdm
import hf_sc as sc
import hf_sch as sh
import sch_rx_cpp as C

trials = int(sys.argv[1]) if len(sys.argv) > 1 else 4
rng = np.random.default_rng(17)
bad = defaultdict(int)
n_case = defaultdict(int)
tp = defaultdict(float)
tc = defaultdict(float)
worst = defaultdict(float)
crc = [0, 0]


def check(name, ok, detail=""):
    n_case[name] += 1
    if not ok:
        bad[name] += 1
        print(f"  {name}: {detail}")


def py_decode(cfg, seg, t0, cfo, n_bytes):
    """receive_one_fragment's decode steps."""
    n_blocks = sh._n_blocks(n_bytes, cfg.bits_per_symbol)
    n_sym = sh.PRE_HF_LEN + sh.N * (1 + n_blocks)
    r = sh._grid(seg, t0, cfo, n_sym)
    uws = [r[sh.PRE_HF_LEN - sh.UW_LEN + b * sh.N:sh.PRE_HF_LEN + b * sh.N] for b in range(n_blocks + 2)]
    cfo += np.angle(sum(np.vdot(uws[k], uws[k + 1]) for k in range(len(uws) - 1))) / (2 * np.pi * sh.N)
    r = sh._grid(seg, t0, cfo, n_sym)
    _, H, nv = sh._header(r)
    payload, crc_ok, ber, evm, d = sh._decode(r, cfg, n_bytes, H, nv)
    return payload, crc_ok, ber, 100 * 10 ** (-sh.PRE_SNR_DB / 20), d, cfo


for occ in (10, 20, 40):
    for mod in ("qpsk", "8psk", "16psk"):
        cfg = ofdm.build_config("SH", occ, data_modulation=mod, fec_scheme="ldpc")
        for tr in range(trials):
            n = int(rng.choice([6, 100, 600, 1030]))
            payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
            w = ofdm.build_frame(cfg, payload).astype(np.complex128)
            if tr % 2:
                d1, d2 = 8 * occ // 10, 40 * occ // 10        # two echoes, 1 and 5 symbols late
                w[d1:] += 0.5 * np.exp(0.7j) * w[:-d1]
                w[d2:] += 0.3 * np.exp(2.1j) * w[:-d2]
            lead = int(rng.integers(500, 4000))
            x = np.concatenate([np.zeros(lead), w, np.zeros(3000)])
            cfo_hz = rng.uniform(-1500, 1500)
            x = x * np.exp(2j * np.pi * cfo_hz * np.arange(len(x)) / cfg.fs)
            snr = {"qpsk": 4.0, "8psk": 9.0, "16psk": 15.0}[mod] + rng.uniform(0, 10)
            x = x + np.sqrt(10 ** (-snr / 10) / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
            x = x.astype(np.complex64)
            sh._set(cfg)

            t = time.perf_counter()
            a = sh._detect(x, cfg.symbol_rate)
            va = sh.VARIANT
            t1 = time.perf_counter()
            b = C.detect(cfg, x)
            t2 = time.perf_counter()
            tp["detect"] += t1 - t
            tc["detect"] += t2 - t1
            same = (a is None) == (b is None) and (a == "short") == (b == "short")
            if same and isinstance(a, tuple):
                worst["t0"] = max(worst["t0"], abs(a[0] - b[0]))
                worst["cfo"] = max(worst["cfo"], abs(a[1] - b[1]))
                same = abs(a[0] - b[0]) < 1e-3 and abs(a[1] - b[1]) < 1e-6 and va == b[2]
            check("detect", same, f"{occ} {mod}: {a} v{va} / {b}")
            if not isinstance(a, tuple) or not same:
                continue
            t0, cfo = a
            t = time.perf_counter()
            r = sh._grid(x, t0, cfo, sh.PRE_HF_LEN + sh.N)
            na = sh._header(r)[0]
            t1 = time.perf_counter()
            nb = C.header(cfg, x, t0, cfo, va)
            t2 = time.perf_counter()
            tp["header"] += t1 - t
            tc["header"] += t2 - t1
            check("header", na == nb, f"{na} / {nb}")
            if na is None or na != nb:
                continue
            sh._use(va)
            t = time.perf_counter()
            pa = py_decode(cfg, x, t0, cfo, na)
            t1 = time.perf_counter()
            pb = C.decode(cfg, x, t0, cfo, va, na)
            t2 = time.perf_counter()
            tp["decode"] += t1 - t
            tc["decode"] += t2 - t1
            crc[0] += pa[1]
            crc[1] += pb[1]
            worst["ber"] = max(worst["ber"], abs(pa[2] - pb[2]))
            worst["evm"] = max(worst["evm"], abs(pa[3] - pb[3]) / pa[3])
            same = pa[1] == pb[1] and (not pa[1] or pa[0] == pb[0]) and abs(pa[2] - pb[2]) < 0.01
            check("decode", same, f"{occ} {mod} {n} B {snr:.1f} dB: crc {pa[1]}/{pb[1]} ber {pa[2]:.4f}/{pb[2]:.4f}")
print(f"{'step':10s} {'cases':>6s} {'differ':>7s}")
for k in n_case:
    print(f"{k:10s} {n_case[k]:6d} {bad[k]:7d}")
print("CRC ok: python", crc[0], "C++", crc[1])
print("worst differences: " + ", ".join(f"{k} {v:.1e}" for k, v in worst.items()))
for k in tp:
    print(f"{k:10s} python {1000 * tp[k]:7.0f} ms   C++ {1000 * tc[k]:7.0f} ms")
