"""End to end: hf_ofdm_tx.py -> channel -> hf_ofdm_rx.py's main loop (through
e2e_rx.py: input in fixed steps, so runs are repeatable), as processes, with
the pure-Python modem and with AVM_CPP=1 (the C++ kernels), per mode:
transmitted IQ compared, received bytes compared with each other and with
what was sent, the per-fragment log lines (BER, EVM, CFO) compared, and
wall time of each receiver.

    python test_e2e.py [mode ...]      e.g. A:20:qpsk B:80:16qam S:20:8psk"""
import os
import re
import subprocess
import sys
import time

import numpy as np

AVM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
FS = 550_000
FRAG = 1024
N_BYTES = 30_000
work = os.environ.get("E2E_DIR", os.path.join(AVM, "c++", "_e2e"))
os.makedirs(work, exist_ok=True)

configs = sys.argv[1:] or ["A:20:qpsk", "B:80:16qam", "C:20:qpsk", "D:20:qpsk", "VU:160:qpsk",
                           "S:20:8psk", "SH:40:16psk"]
rng = np.random.default_rng(23)
payload = rng.integers(0, 256, N_BYTES, dtype=np.uint8).tobytes()
src = os.path.join(work, "payload.bin")
open(src, "wb").write(payload)


def run(script, args, stdin_path, stdout_path, cpp):
    env = dict(os.environ, AVM_CPP="1" if cpp else "0", PYTHONUNBUFFERED="1")
    t0 = time.perf_counter()
    with open(stdin_path, "rb") as fi, open(stdout_path, "wb") as fo:
        p = subprocess.run([sys.executable, os.path.join(AVM, script)] + args, stdin=fi, stdout=fo,
                           stderr=subprocess.PIPE, env=env, cwd=AVM)
    dt = time.perf_counter() - t0
    if p.returncode:
        print(p.stderr.decode(errors="replace")[-1500:])
    return dt, p.stderr.decode(errors="replace")


def run_rx(args, stdin_path, stdout_path, cpp):
    """hf_ofdm_rx.py's main loop through e2e_rx.py (deterministic input steps)."""
    env = dict(os.environ, AVM_CPP="1" if cpp else "0", PYTHONUNBUFFERED="1")
    t0 = time.perf_counter()
    with open(stdout_path, "wb") as fo:
        p = subprocess.run([sys.executable, os.path.join(AVM, "c++", "e2e_rx.py"), stdin_path] + args,
                           stdout=fo, stderr=subprocess.PIPE, env=env, cwd=AVM)
    dt = time.perf_counter() - t0
    if p.returncode:
        print(p.stderr.decode(errors="replace")[-1500:])
    return dt, p.stderr.decode(errors="replace")


def matched_prefix(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return 0
    diff = np.flatnonzero(np.frombuffer(a[:n], np.uint8) != np.frombuffer(b[:n], np.uint8))
    return int(diff[0]) if len(diff) else n


print(f"{'config':14s} {'TX IQ diff':>11s} {'RX py':>14s} {'RX C++':>14s} {'same out':>9s} {'same log':>8s} {'py s':>6s} {'C++ s':>6s}")
for spec in configs:
    mode, occ, mod = spec.split(":")
    common = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc",
              "--sample-rate", str(FS), "--fragment-size", str(FRAG)]
    iq = {}
    for cpp in (False, True):
        out = os.path.join(work, f"tx_{mode}_{cpp}.iq")
        run("hf_ofdm_tx.py", common + ["--stream", "--tx-warmup-s", "0", "--amplitude", "0.3"], src, out, cpp)
        iq[cpp] = np.fromfile(out, np.complex64)
    n = min(len(iq[False]), len(iq[True]))
    tx_diff = (float(np.max(np.abs(iq[False][:n] - iq[True][:n]))) / max(float(np.max(np.abs(iq[False]))), 1e-12)
               if n else float("nan"))
    if len(iq[False]) != len(iq[True]):
        tx_diff = float("inf")
    # channel: noise (~15 dB in band), frequency offset, a weak echo
    x = iq[False].astype(np.complex128)
    x = np.concatenate([np.zeros(FS // 5), x, np.zeros(FS // 2)])
    x[40:] += 0.25 * x[:-40]
    x *= np.exp(2j * np.pi * 37.0 * np.arange(len(x)) / FS)
    sig = float(np.mean(np.abs(iq[False]) ** 2))
    x += np.sqrt(sig * 10 ** (-15 / 10) / 2) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
    rx_in = os.path.join(work, f"rx_{mode}.iq")
    x.astype(np.complex64).tofile(rx_in)
    res = {}
    logs = {}
    for cpp in (False, True):
        out = os.path.join(work, f"out_{mode}_{cpp}.bin")
        dt, err = run_rx(common + ["--no-output-pacing"], rx_in, out, cpp)
        got = open(out, "rb").read()
        res[cpp] = (got, dt, matched_prefix(got, payload))
        logs[cpp] = [re.sub(r"\s*Time=.*", "", ln) for ln in err.splitlines() if ln.startswith("Fragment seq=")]
    same = res[False][0] == res[True][0]
    same_log = logs[False] == logs[True]
    print(f"{spec:14s} {tx_diff:11.1e} {res[False][2]:6d}/{N_BYTES:<7d} {res[True][2]:6d}/{N_BYTES:<7d} "
          f"{'yes' if same else 'NO':>9s} {'yes' if same_log else 'NO':>8s} {res[False][1]:6.1f} {res[True][1]:6.1f}")
    if not same_log:
        for a, b in zip(logs[False], logs[True]):
            if a != b:
                print("   python:", a)
                print("   C++   :", b)
                break
