"""avm_tx (the C++ transmitter program) against hf_ofdm_tx.py --stream on the
same input: the IQ compared sample by sample (to float rounding) and the
status lines, for every mode and the output variants (native rate,
resampled with a fragment gap, resampled and crossfade-joined, peak
reduction); then the C++ IQ through avm_rx to check it decodes.

    python test_avm_tx.py [mode ...]      e.g. A:20:qpsk S:20:8psk"""
import os
import re
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
AVM = os.path.normpath(os.path.join(HERE, ".."))
WORK = os.path.join(HERE, "_e2e")
os.makedirs(WORK, exist_ok=True)
EXT = ".exe" if sys.platform == "win32" else ""
configs = sys.argv[1:] or ["A:20:qpsk", "B:80:16qam", "C:20:qpsk", "D:20:qpsk", "VU:160:qpsk",
                           "S:20:8psk", "SH:40:16psk"]
rng = np.random.default_rng(31)
src = os.path.join(WORK, "tx_payload.bin")
payload = rng.integers(0, 256, 9000, dtype=np.uint8).tobytes()
open(src, "wb").write(payload)
VARIANTS = [("native", []),
            ("550k gap", ["--sample-rate", "550000", "--fragment-gap-ms", "30"]),
            ("550k join", ["--sample-rate", "550000", "--fragment-gap-ms", "0"]),
            ("papr 6dB", ["--sample-rate", "550000", "--fragment-gap-ms", "0", "--papr-db", "6"])]


def run(cmd, env_extra=None):
    env = dict(os.environ, AVM_CPP="0", PYTHONUNBUFFERED="1", **(env_extra or {}))
    t0 = time.perf_counter()
    with open(src, "rb") as fi:
        p = subprocess.run(cmd, stdin=fi, capture_output=True, env=env, cwd=AVM)
    if p.returncode:
        print(p.stderr.decode(errors="replace")[-600:])
    return p.stdout, p.stderr.decode(errors="replace"), time.perf_counter() - t0


def lines(err):
    return [ln for ln in err.splitlines() if ln.startswith(("Streamed", "[warmup]", "Stream ended"))]


print(f"{'config':14s} {'variant':10s} {'samples':>9s} {'worst diff':>11s} {'same log':>9s} {'py s':>6s} "
      f"{'C++ s':>6s}  decodes")
bad = 0
for spec in configs:
    mode, occ, mod = spec.split(":")
    base = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc", "--stream",
            "--fragment-size", "1024", "--tx-warmup-s", "0", "--amplitude", "0.3"]
    for name, extra in VARIANTS:
        if "papr" in name and mode in ("S", "SH"):
            continue
        py = run([sys.executable, os.path.join(AVM, "hf_ofdm_tx.py")] + base + extra)
        cc = run([os.path.join(HERE, "avm_tx" + EXT)] + base + extra)
        a = np.frombuffer(py[0], np.complex64)
        b = np.frombuffer(cc[0], np.complex64)
        diff = float(np.max(np.abs(a - b))) / float(np.max(np.abs(a))) if len(a) == len(b) and len(a) else float("inf")
        same_log = lines(py[1]) == lines(cc[1])
        # the C++ waveform through the C++ receiver
        dec = ""
        if "550k" in name or "papr" in name:
            iq = os.path.join(WORK, "txc.iq")
            # a little noise (40 dB down): exact digital silence makes the
            # S detector divide rounding residue by ~zero energy
            x = np.concatenate([np.zeros(20000, np.complex64), b, np.zeros(200000, np.complex64)])
            sig = float(np.sqrt(np.mean(np.abs(b) ** 2)))
            x = x + (0.01 * sig / np.sqrt(2)) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
            x.astype(np.complex64).tofile(iq)
            with open(iq, "rb") as fi:
                p = subprocess.run([os.path.join(HERE, "avm_rx" + EXT), "--mode", mode, "--occupancy", occ,
                                    "--modulation", mod, "--fec", "ldpc", "--sample-rate", "550000",
                                    "--fragment-size", "1024", "--no-output-pacing", "--test-step", "131072"],
                                   stdin=fi, capture_output=True)
            dec = "yes" if p.stdout == payload else f"NO ({len(p.stdout)} B)"
        # single-precision FFT rounding: ~4e-6 on x86, up to ~2e-5 on ARM (numpy's
        # FFT rounds differently there); a 12-bit DAC step is 2.4e-4
        ok = diff < 1e-4 and same_log and dec in ("", "yes")
        bad += not ok
        print(f"{spec:14s} {name:10s} {len(b):9d} {diff:11.1e} {'yes' if same_log else 'NO':>9s} {py[2]:6.1f} "
              f"{cc[2]:6.1f}  {dec}")
        if not same_log:
            for x1, y1 in zip(lines(py[1]) + ["(end)"], lines(cc[1]) + ["(end)"]):
                if x1 != y1:
                    print("   python:", x1)
                    print("   C++   :", y1)
                    break
print("ALL MATCH" if bad == 0 else f"{bad} MISMATCHES")
