"""modem_tx.cpp against hf_ofdm_common.py: build_frame, reduce_papr and
fft_resample_rate (+ tx_spectrum_cut) for the OFDM modes, every
modulation and FEC scheme, several bandwidths and payload lengths; then
speed of the whole per-fragment chain.

    python test_modem_tx.py"""
import os
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as ofdm
import modem_tx_cpp as M

rng = np.random.default_rng(5)
worst = {"frame": 0.0, "papr": 0.0, "resample": 0.0}
bad = 0
cases = 0
for mode, occs in (("A", (4.5, 10, 20, 40, 250)), ("B", (5, 20, 80)), ("C", (10, 20, 40)),
                   ("D", (10, 20, 40)), ("VU", (80, 160))):
    for occ in occs:
        for mod in ("qpsk", "16qam", "32qam"):
            for fec in ("viterbi", "ldpc"):
                cfg = ofdm.build_config(mode, occ, data_modulation=mod, fec_scheme=fec)
                for n in (0, 1, 37, 300, 1030):
                    payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
                    a = ofdm.build_frame(cfg, payload)
                    b = M.build_frame(cfg, payload)
                    cases += 1
                    if a.shape != b.shape:
                        print(f"  {mode} {occ} {mod} {fec} {n}: length {len(a)} vs {len(b)}")
                        bad += 1
                        continue
                    scale = np.max(np.abs(a))
                    e = np.max(np.abs(a - b)) / scale
                    worst["frame"] = max(worst["frame"], e)
                    if e > 1e-5:
                        print(f"  {mode} {occ} {mod} {fec} {n}: frame differs by {e:.2e}")
                        bad += 1
                        continue
                    if n == 300:
                        pa = ofdm.reduce_papr(cfg, a, 6.0)
                        pb = M.reduce_papr(cfg, a, 6.0)
                        e = np.max(np.abs(pa - pb)) / scale
                        worst["papr"] = max(worst["papr"], e)
                        if e > 1e-5:
                            print(f"  {mode} {occ} {mod} {fec}: papr differs by {e:.2e}")
                            bad += 1
                        fs_out = 550_000 if occ < 200 else 2_000_000
                        x = pa.astype(np.complex64)
                        ra = ofdm.fft_resample_rate(x, cfg.fs, fs_out, *ofdm.tx_spectrum_cut(cfg))
                        rb = M.fft_resample_rate(x, cfg.fs, fs_out, *ofdm.tx_spectrum_cut(cfg))
                        if ra.shape != rb.shape or ra.dtype != rb.dtype:
                            print(f"  {mode} {occ}: resample {ra.shape} {ra.dtype} vs {rb.shape} {rb.dtype}")
                            bad += 1
                            continue
                        e = np.max(np.abs(ra - rb)) / np.max(np.abs(ra))
                        worst["resample"] = max(worst["resample"], e)
                        if e > 1e-4:
                            print(f"  {mode} {occ}: resample differs by {e:.2e}")
                            bad += 1

# single carrier: S (qpsk, 8psk), SH (qpsk, 8psk, 16psk), every width
worst["single carrier"] = 0.0
for mode, mods in (("S", ("qpsk", "8psk")), ("SH", ("qpsk", "8psk", "16psk"))):
    for occ in (10, 20, 40):
        for mod in mods:
            cfg = ofdm.build_config(mode, occ, data_modulation=mod, fec_scheme="ldpc")
            for n in (1, 20, 300, 1030, 3000):
                payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
                a = ofdm.build_frame(cfg, payload)
                b = M.build_frame(cfg, payload)
                cases += 1
                if a.shape != b.shape or a.dtype != b.dtype:
                    print(f"  {mode} {occ} {mod} {n}: {a.shape} {a.dtype} vs {b.shape} {b.dtype}")
                    bad += 1
                    continue
                e = np.max(np.abs(a - b)) / np.max(np.abs(a))
                worst["single carrier"] = max(worst["single carrier"], e)
                if e > 1e-5:
                    print(f"  {mode} {occ} {mod} {n}: differs by {e:.2e}")
                    bad += 1
print(f"{cases} frames: {'ALL MATCH' if bad == 0 else f'{bad} MISMATCHES'}; worst relative error "
      + ", ".join(f"{k} {v:.1e}" for k, v in worst.items()))

# speed: build + PAPR + resample to 550 kS/s, one 1 KB fragment
for mode, occ in (("A", 20), ("B", 80), ("VU", 160)):
    cfg = ofdm.build_config(mode, occ, data_modulation="qpsk", fec_scheme="ldpc")
    payload = rng.integers(0, 256, 1030, dtype=np.uint8).tobytes()
    cut = ofdm.tx_spectrum_cut(cfg)
    fs_out = 550_000 if occ < 200 else 2_000_000
    res = {}
    for name, mod in (("python", ofdm), ("C++", M)):
        mod.fft_resample_rate(mod.reduce_papr(cfg, mod.build_frame(cfg, payload), 6.0).astype(np.complex64),
                              cfg.fs, fs_out, *cut)
        reps = 10
        t0 = time.perf_counter()
        for _ in range(reps):
            w = mod.build_frame(cfg, payload)
            t1 = time.perf_counter()
            w = mod.reduce_papr(cfg, w, 6.0)
            t2 = time.perf_counter()
            mod.fft_resample_rate(w.astype(np.complex64), cfg.fs, fs_out, *cut)
        tot = (time.perf_counter() - t0) / reps
        res[name] = tot
    print(f"{mode} {occ} kHz 1 KB fragment, build+papr+resample: python {1000 * res['python']:.1f} ms, "
          f"C++ {1000 * res['C++']:.1f} ms")
