"""modem_rx.cpp's sync against hf_ofdm_common.py, step by step on the same
complex64 stream (a frame at a random offset in noise, integer + fractional
frequency offset, an echo): schmidl_cox_sync (both search kinds),
quick_sc_scan, apply_cfo_correction, refine_frame_start,
resolve_integer_cfo (with and without a prior), resolve_timing_offset, and
the decode at the end. Then speed of each.

    python test_sync.py [trials_per_config]"""
import os
import sys
import time
from collections import defaultdict

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as ofdm
import modem_rx_cpp as R

trials = int(sys.argv[1]) if len(sys.argv) > 1 else 4
rng = np.random.default_rng(11)
bad = defaultdict(int)
total = defaultdict(int)
tp = defaultdict(float)
tc = defaultdict(float)
worst_rot = 0.0


def both(name, f_py, f_c, cmp=lambda a, b: a == b):
    t0 = time.perf_counter()
    a = f_py()
    t1 = time.perf_counter()
    b = f_c()
    t2 = time.perf_counter()
    tp[name] += t1 - t0
    tc[name] += t2 - t1
    total[name] += 1
    if not cmp(a, b):
        bad[name] += 1
        print(f"  {name}: python {a} / C++ {b}")
    return a


def close(a, b, tol=1e-9):
    return all(abs(x - y) <= tol * max(1.0, abs(x)) for x, y in zip(a, b))


for mode, occ in (("A", 10), ("A", 20), ("B", 20), ("B", 80), ("C", 20), ("D", 20), ("VU", 80), ("VU", 160)):
    for fec in ("viterbi", "ldpc"):
        cfg = ofdm.build_config(mode, occ, data_modulation="qpsk", fec_scheme=fec)
        for tr in range(trials):
            payload = rng.integers(0, 256, int(rng.choice([40, 300])), dtype=np.uint8).tobytes()
            frame = ofdm.build_frame(cfg, payload)
            if tr % 2:
                d = max(1, cfg.n_cp // 3)
                frame = frame.copy()
                frame[d:] += 0.4 * frame[:-d]
            lead = int(rng.integers(cfg.symbol_len, 4 * cfg.symbol_len))
            x = np.concatenate([np.zeros(lead), frame, np.zeros(3 * cfg.symbol_len)]).astype(complex)
            k_true = int(rng.integers(-4, 5)) if mode != "VU" else 0
            cfo = (k_true + rng.uniform(-0.3, 0.3)) * cfg.carrier_spacing
            x *= np.exp(2j * np.pi * cfo * np.arange(len(x)) / cfg.fs)
            snr = rng.uniform(3, 20)
            x += np.sqrt(np.mean(np.abs(frame) ** 2) / 10 ** (snr / 10) / 2) * (
                rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
            x = x.astype(np.complex64)

            both("quick_sc_scan", lambda: ofdm.quick_sc_scan(x, cfg.n_fft // 2),
                 lambda: R.quick_sc_scan(x, cfg.n_fft // 2), lambda a, b: a[0] == b[0] and close(a[1:], b[1:]))
            both("sync (earliest)", lambda: ofdm.schmidl_cox_sync(x, cfg, prefer_earliest=True),
                 lambda: R.schmidl_cox_sync(x, cfg, prefer_earliest=True),
                 lambda a, b: a[0] == b[0] and close(a[1:], b[1:]))
            start, frac, _ = both("sync", lambda: ofdm.schmidl_cox_sync(x, cfg), lambda: R.schmidl_cox_sync(x, cfg),
                                  lambda a, b: a[0] == b[0] and close(a[1:], b[1:]))
            a = ofdm.apply_cfo_correction(x, frac, cfg.fs)
            b = R.apply_cfo_correction(x, frac, cfg.fs)
            worst_rot = max(worst_rot, float(np.max(np.abs(a - b))))
            start = both("refine_frame_start", lambda: ofdm.refine_frame_start(cfg, x, start, frac),
                         lambda: R.refine_frame_start(cfg, x, start, frac))
            seg = ofdm.apply_cfo_correction(x[start:], frac, cfg.fs)[cfg.preamble_len:]
            k, _ = both("integer CFO", lambda: ofdm.resolve_integer_cfo(cfg, seg),
                        lambda: R.resolve_integer_cfo(cfg, seg), lambda a, b: a[0] == b[0] and close(a[1:], b[1:]))
            both("integer CFO (prior)", lambda: ofdm.resolve_integer_cfo(cfg, seg, prior_k=k_true),
                 lambda: R.resolve_integer_cfo(cfg, seg, prior_k=k_true),
                 lambda a, b: a[0] == b[0] and close(a[1:], b[1:]))
            total_cfo = frac - k * cfg.carrier_spacing
            deltas = range(-cfg.n_cp, cfg.n_cp + 1, max(1, cfg.n_cp // 8))
            delta, _, _ = both("timing offset",
                               lambda: ofdm.resolve_timing_offset(cfg, x, start, total_cfo, 0, deltas),
                               lambda: R.resolve_timing_offset(cfg, x, start, total_cfo, 0, deltas),
                               lambda a, b: a[0] == b[0] and a[2] == b[2] and close(a[1:2], b[1:2], 1e-6))
            body = ofdm.apply_cfo_correction(x[start + delta:], total_cfo, cfg.fs)[cfg.preamble_len:]
            both("decode", lambda: ofdm.decode_frame(cfg, body)[:3], lambda: R.decode_frame(cfg, body)[:3],
                 lambda a, b: a == b or (not a[1] and not b[1] and a[2] == b[2]))  # failed: garbage may differ
            total["k right"] += bool(R.decode_frame(cfg, body)[1])
print(f"{'step':22s} {'cases':>6s} {'differ':>7s} {'python ms':>10s} {'C++ ms':>8s}")
for name in tp:
    print(f"{name:22s} {total[name]:6d} {bad[name]:7d} {1000 * tp[name] / total[name]:10.2f} "
          f"{1000 * tc[name] / total[name]:8.2f}")
print(f"rotation: worst difference {worst_rot:.1e}; frames decoded (CRC ok) {total['k right']}/{total['decode']}")
