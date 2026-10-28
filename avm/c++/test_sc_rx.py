"""Mode S receive kernels (sc_rx_cpp) against hf_sc.py's, on bursts from
the Python transmitter through noise, an echo, a frequency offset and a
timing offset: detect, sync, header, decode -- same decisions, metrics to
rounding (the Python is single precision). Then speed.

    python test_sc_rx.py [trials_per_config]"""
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
import sc_rx_cpp as C

trials = int(sys.argv[1]) if len(sys.argv) > 1 else 6
rng = np.random.default_rng(13)
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


for occ in (10, 20, 40):
    for mod in ("qpsk", "8psk"):
        cfg = ofdm.build_config("S", occ, data_modulation=mod, fec_scheme="ldpc")
        for tr in range(trials):
            n = int(rng.choice([6, 30, 300, 1030]))
            payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
            w = ofdm.build_frame(cfg, payload).astype(np.complex128)
            if tr % 2:
                w[12:] += 0.4 * np.exp(1j * 1.3) * w[:-12]        # echo 3 symbols late
            lead = int(rng.integers(300, 3000))
            x = np.concatenate([np.zeros(lead), w, np.zeros(2000)])
            cfo_cyc = rng.uniform(-0.05, 0.05)                    # cycles / symbol
            x = x * np.exp(2j * np.pi * cfo_cyc * np.arange(len(x)) / sc.SPS)
            snr = (3.0 if mod == "qpsk" else 8.0) + rng.uniform(0, 10)
            x = x + np.sqrt(10 ** (-snr / 10) / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
            x = x.astype(np.complex64)

            t = time.perf_counter()
            za = sc._mf(x)
            ha = sc.detect(za)
            t1 = time.perf_counter()
            zb = C.mf(x)
            hb = C.detect(zb)
            t2 = time.perf_counter()
            tp["mf + detect"] += t1 - t
            tc["mf + detect"] += t2 - t1
            worst["mf"] = max(worst["mf"], float(np.max(np.abs(za - zb))))
            check("detect", (ha is None) == (hb is None) and (ha is None or (ha[0] == hb[0]
                  and abs(ha[1] - hb[1]) < 1e-4 and abs(ha[2] - hb[2]) < 1e-6)), f"{ha} / {hb}")
            if ha is None or hb is None:
                continue
            i, _, cfo1 = ha
            t = time.perf_counter()
            t0a, cfoa, zza = sc._sync(x, i, cfo1)
            refa = np.vdot(sc.PREAMBLE, sc._cubic(zza, t0a + np.arange(sc.PRE_LEN) * sc.SPS)) / sc.PRE_LEN
            refa = refa / max(abs(refa), 1e-12)
            na = sc._header(zza, t0a, refa)
            t1 = time.perf_counter()
            t0b, cfob, refb = C.sync(x, i, cfo1)
            nb = C.header()
            t2 = time.perf_counter()
            tp["sync + header"] += t1 - t
            tc["sync + header"] += t2 - t1
            worst["t0"] = max(worst["t0"], abs(t0a - t0b))
            worst["cfo"] = max(worst["cfo"], abs(cfoa - cfob))
            check("sync", abs(t0a - t0b) < 1e-3 and abs(cfoa - cfob) < 1e-6 and abs(refa - refb) < 1e-4,
                  f"t0 {t0a} / {t0b}, cfo {cfoa} / {cfob}")
            check("header", na == nb, f"{na} / {nb}")
            if na is None or na != nb:
                continue
            t = time.perf_counter()
            zz = sc._derotated_mf(x, cfoa)
            a = sc.decode(zz, t0a, refa, na, mod)
            t1 = time.perf_counter()
            b = C.decode(x, t0a, cfoa, na, mod)
            t2 = time.perf_counter()
            tp["decode"] += t1 - t
            tc["decode"] += t2 - t1
            crc[0] += a[1]
            crc[1] += b[1]
            same = a[1] == b[1] and (not a[1] or a[0] == b[0])
            worst["ber"] = max(worst["ber"], abs(a[2] - b[2]))
            worst["evm"] = max(worst["evm"], abs(a[3] - b[3]) / a[3])
            check("decode", same and abs(a[2] - b[2]) < 0.01 and abs(a[3] - b[3]) < 0.01 * a[3],
                  f"{occ} {mod} {n} B {snr:.1f} dB: crc {a[1]}/{b[1]} ber {a[2]:.4f}/{b[2]:.4f} "
                  f"evm {a[3]:.2f}/{b[3]:.2f}")
print(f"{'step':16s} {'cases':>6s} {'differ':>7s}")
for k in n_case:
    print(f"{k:16s} {n_case[k]:6d} {bad[k]:7d}")
print("CRC ok: python", crc[0], "C++", crc[1])
print("worst differences: " + ", ".join(f"{k} {v:.1e}" for k, v in worst.items()))
for k in tp:
    print(f"{k:16s} python {1000 * tp[k]:7.0f} ms   C++ {1000 * tc[k]:7.0f} ms")
