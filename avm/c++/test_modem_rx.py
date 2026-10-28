"""modem_rx.cpp's decode_frame against hf_ofdm_common.decode_frame: frames
from the Python transmitter through a channel (AWGN at several SNRs, a
two-path echo, slow fading, a small residual frequency offset), then both
decoders on the same samples. Compares payload, CRC, incomplete, BER and
EVM; then speed.

    python test_modem_rx.py [trials_per_config]"""
import os
import sys
import time

import numpy as np

here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
sys.path.insert(0, here)
import hf_ofdm_common as ofdm
import modem_rx_cpp as R

trials = int(sys.argv[1]) if len(sys.argv) > 1 else 3
rng = np.random.default_rng(7)


def channel(x, cfg, snr_db, echo, fade_hz, cfo_hz):
    y = x.copy()
    if echo:
        d = max(1, cfg.n_cp // 3)
        y[d:] += 0.5 * np.exp(1j * rng.uniform(0, 2 * np.pi)) * x[:-d]
    t = np.arange(len(y)) / cfg.fs
    if fade_hz:
        y *= 0.8 + 0.4 * np.cos(2 * np.pi * fade_hz * t + rng.uniform(0, 2 * np.pi))
    if cfo_hz:
        y *= np.exp(2j * np.pi * cfo_hz * t)
    p = np.mean(np.abs(x) ** 2)
    n = np.sqrt(p / 10 ** (snr_db / 10) / 2) * (rng.standard_normal(len(y)) + 1j * rng.standard_normal(len(y)))
    return y + n


stats = {"same": 0, "differ": 0, "crc_py": 0, "crc_c": 0}
worst_ber = worst_evm = 0.0
t_py = t_c = 0.0
for mode, occ in (("A", 10), ("A", 20), ("B", 20), ("B", 80), ("C", 20), ("D", 20), ("VU", 80), ("VU", 160)):
    for mod in ("qpsk", "16qam", "32qam"):
        for fec in ("viterbi", "ldpc"):
            cfg = ofdm.build_config(mode, occ, data_modulation=mod, fec_scheme=fec)
            for tr in range(trials):
                n = int(rng.choice([20, 300, 1030]))
                payload = rng.integers(0, 256, n, dtype=np.uint8).tobytes()
                frame = ofdm.build_frame(cfg, payload)
                snr = {"qpsk": 4.0, "16qam": 12.0, "32qam": 15.0}[mod] + rng.uniform(-3, 8)
                rx = channel(frame, cfg, snr, echo=tr % 2 == 1, fade_hz=0.5 if tr % 3 == 2 else 0,
                             cfo_hz=0.002 * cfg.carrier_spacing)
                rx = np.concatenate([rx, np.zeros(cfg.symbol_len, complex)])[cfg.preamble_len:]
                t0 = time.perf_counter()
                a = ofdm.decode_frame(cfg, rx)
                t1 = time.perf_counter()
                b = R.decode_frame(cfg, rx)
                t2 = time.perf_counter()
                t_py += t1 - t0
                t_c += t2 - t1
                stats["crc_py"] += a[1]
                stats["crc_c"] += b[1]
                same = a[0] == b[0] and a[1] == b[1] and a[2] == b[2] and (a[3] is None) == (b[3] is None)
                if same and a[3] is not None:
                    worst_ber = max(worst_ber, abs(a[3] - b[3]))
                    same = abs(a[3] - b[3]) < 2e-3
                if same and a[4] is not None and b[4] is not None:
                    worst_evm = max(worst_evm, abs(a[4] - b[4]) / a[4])
                    same = abs(a[4] - b[4]) <= 1e-6 * max(1.0, a[4])
                if same:
                    stats["same"] += 1
                else:
                    stats["differ"] += 1
                    print(f"  {mode} {occ} {mod} {fec} {n} B {snr:.1f} dB: python crc {a[1]} ber {a[3]} evm {a[4]}"
                          f" / C++ crc {b[1]} ber {b[3]} evm {b[4]}")
print(f"{stats['same']} frames identical, {stats['differ']} differ; CRC ok python {stats['crc_py']}, "
      f"C++ {stats['crc_c']}; worst BER difference {worst_ber:.1e}, EVM {worst_evm:.1e}")
print(f"decode time: python {1000 * t_py:.0f} ms, C++ {1000 * t_c:.0f} ms ({t_py / max(t_c, 1e-9):.1f}x)")
