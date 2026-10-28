"""avm_rx (the C++ receiver program) against hf_ofdm_rx.py on the same IQ
files, both with input arriving in the same fixed steps (e2e_rx.py for the
Python, --test-step for avm_rx): output bytes and the per-fragment status
lines (BER, EVM, CFO, retries) compared, and the wall time. Twice: input
stepped in as decoding goes, and all of it first (HF_OFDM_RX_WAIT_EOF=1).
Uses the channel files test_e2e.py writes (c++/_e2e/rx_<mode>.iq).

    python test_avm_rx.py [mode ...]      e.g. A:20:qpsk S:20:8psk"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AVM = os.path.normpath(os.path.join(HERE, ".."))
WORK = os.path.join(HERE, "_e2e")
EXE = os.path.join(HERE, "avm_rx.exe" if sys.platform == "win32" else "avm_rx")
STEP = 1 << 17
configs = sys.argv[1:] or ["A:20:qpsk", "B:80:16qam", "C:20:qpsk", "D:20:qpsk", "VU:160:qpsk",
                           "S:20:8psk", "SH:40:16psk"]


def status_lines(err):
    out = []
    for ln in err.splitlines():
        if ln.startswith(("Fragment", "  ! ", "  -> wrote", "Signal acquired", "[afc]", "Stream ended")):
            out.append(re.sub(r"\s*Time=.*", "", ln))
    return out


def run(cmd, iq, env_extra):
    env = dict(os.environ, AVM_CPP="0", PYTHONUNBUFFERED="1", **env_extra)
    t0 = time.perf_counter()
    with open(iq, "rb") as fi:
        p = subprocess.run(cmd, stdin=fi, capture_output=True, env=env, cwd=AVM)
    return p.stdout, p.stderr.decode(errors="replace"), time.perf_counter() - t0, p.returncode


print(f"{'config':14s} {'input':8s} {'python':>14s} {'C++':>14s} {'same out':>9s} {'same log':>9s} "
      f"{'py s':>6s} {'C++ s':>6s}")
bad = 0
for spec in configs:
    mode, occ, mod = spec.split(":")
    iq = os.path.join(WORK, f"rx_{mode}.iq")
    if not os.path.exists(iq):
        print(f"{spec}: no {iq} (run test_e2e.py first)")
        continue
    args = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc", "--sample-rate", "550000",
            "--fragment-size", "1024", "--no-output-pacing"]
    for label, extra in (("stepped", {}), ("all", {"HF_OFDM_RX_WAIT_EOF": "1"})):
        py = run([sys.executable, os.path.join(HERE, "e2e_rx.py"), iq] + args, iq,
                 dict(extra, E2E_CHUNK=str(STEP)))
        cc = run([EXE] + args + ["--test-step", str(STEP)], iq, extra)
        if py[3] or cc[3]:
            print(f"  exit codes python {py[3]} C++ {cc[3]}")
            print(py[1][-800:] if py[3] else cc[1][-800:])
        lp, lc = status_lines(py[1]), status_lines(cc[1])
        same_out, same_log = py[0] == cc[0], lp == lc
        bad += not (same_out and same_log)
        print(f"{spec:14s} {label:8s} {len(py[0]):6d} bytes   {len(cc[0]):6d} bytes   {'yes' if same_out else 'NO':>9s} "
              f"{'yes' if same_log else 'NO':>9s} {py[2]:6.1f} {cc[2]:6.1f}")
        if not same_log:
            for x, y in zip(lp + ["(end)"], lc + ["(end)"]):
                if x != y:
                    print("   python:", x)
                    print("   C++   :", y)
                    break
print("ALL MATCH" if bad == 0 else f"{bad} MISMATCHES")
