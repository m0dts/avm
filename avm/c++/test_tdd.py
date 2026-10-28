"""Duplex (TDD) in the C++ engines, without radios: avm_tx --tdd-cycle-s
against hf_ofdm_tx.py --tdd-cycle-s (the same slot plan; every cycle exactly
cycle_s long), then the C++ TDD stream through the Python and the C++
receivers in TDD mode (the real fragments come out, filler aside), and as
station 0 itself (its own fragments: nothing comes out).

    python test_tdd.py [MODE:KHZ:MOD]     default VU:160:16qam"""
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
spec = sys.argv[1] if len(sys.argv) > 1 else "VU:160:16qam"
mode, occ, mod = spec.split(":")
FS = 550000
CYCLE, SLOT = 1.0, 0.47
rng = np.random.default_rng(7)
payload = rng.integers(1, 256, 1024 * 12, dtype=np.uint8).tobytes()   # (no zero bytes: filler is all zero)
base = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc", "--stream",
        "--fragment-size", "1024", "--tx-warmup-s", "0", "--amplitude", "0.3", "--sample-rate", str(FS),
        "--fragment-gap-ms", "0", "--max-input-backlog", "100",
        "--tdd-cycle-s", str(CYCLE), "--tdd-slot-s", str(SLOT), "--tdd-station", "0"]
env = dict(os.environ, AVM_CPP="0", PYTHONUNBUFFERED="1")


def tx(cmd):
    # the input once the transmitter has dropped its warmup backlog (sent
    # before that, it is all discarded as stale)
    import threading
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=AVM)
    out, err, ready = [], [], threading.Event()

    def errs():
        for ln in p.stderr:
            t = ln.decode(errors="replace")
            err.append(t)
            if "discarded the warmup backlog" in t:
                ready.set()
        ready.set()
    te = threading.Thread(target=errs)
    to = threading.Thread(target=lambda: out.append(p.stdout.read()))
    te.start()
    to.start()
    ready.wait(120)
    p.stdin.write(payload)
    p.stdin.close()
    to.join()
    te.join()
    p.wait()
    return np.frombuffer(out[0], np.complex64), "".join(err)


def plan(err):
    m = re.search(r"\[tdd\] cycle ([\d.]+) ms: (\d+) fragment\(s\) \(([\d.]+) ms\)", err)
    return m.groups() if m else None


bad = 0
py_iq, py_err = tx([sys.executable, os.path.join(AVM, "hf_ofdm_tx.py")] + base)
cc_iq, cc_err = tx([os.path.join(HERE, "avm_tx" + EXT)] + base)
cyc = int(round(CYCLE * FS))
print(f"slot plan   python {plan(py_err)}   C++ {plan(cc_err)}")
if plan(py_err) != plan(cc_err) or plan(cc_err) is None:
    bad += 1
    print(cc_err[-800:])
for name, iq in (("python", py_iq), ("C++", cc_iq)):
    # whole cycles, but for the last: the input ended part-way through a slot
    whole = len(iq) >= cyc
    # the bursts: each cycle starts with the slot, the rest is silence
    k = len(iq) // cyc
    on = [int(np.count_nonzero(np.abs(iq[i * cyc:(i + 1) * cyc]) > 0)) for i in range(k)]
    print(f"{name:7s} {len(iq)} samples = {len(iq) / cyc:.3f} cycles; non-zero per cycle {sorted(set(on))}")
    bad += not whole

if len(py_iq) == len(cc_iq) and len(py_iq):
    d = float(np.max(np.abs(py_iq - cc_iq))) / float(np.max(np.abs(py_iq)))
    print(f"IQ python vs C++: worst difference {d:.1e} of full scale")
    bad += d > 1e-4
else:
    print("IQ python vs C++: different lengths (the input landed in different slots)")

# the C++ stream through both receivers, in TDD mode
x = np.concatenate([np.zeros(30000, np.complex64), cc_iq, np.zeros(400000, np.complex64)])
sig = float(np.sqrt(np.mean(np.abs(cc_iq[np.abs(cc_iq) > 0]) ** 2)))
x = x + (0.01 * sig / np.sqrt(2)) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
iq = os.path.join(WORK, "tdd.iq")
x.astype(np.complex64).tofile(iq)
rx_args = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc", "--sample-rate", str(FS),
           "--fragment-size", "1024", "--no-output-pacing", "--tdd-cycle-s", str(CYCLE), "--tdd-slot-s", str(SLOT)]


def strip(out):
    frags = [out[i:i + 1024] for i in range(0, len(out), 1024)]
    return b"".join(f for f in frags if any(f))


for name, cmd, extra_env in (("python RX", [sys.executable, os.path.join(AVM, "hf_ofdm_rx.py")] + rx_args,
                              {"HF_OFDM_RX_WAIT_EOF": "1"}),
                             ("C++ RX", [os.path.join(HERE, "avm_rx" + EXT)] + rx_args + ["--test-step", "131072"], {})):
    for station in (None, 0):
        c = cmd + (["--tdd-station", str(station)] if station is not None else [])
        with open(iq, "rb") as fi:
            p = subprocess.run(c, stdin=fi, capture_output=True, env=dict(env, **extra_env), cwd=AVM)
        err = p.stderr.decode(errors="replace")
        got = strip(p.stdout)
        far = re.search(r"\[tdd\] far end sends (\d+) fragment", err)
        if station is None:
            ok = got == payload
            print(f"{name:10s} as a third party: {len(p.stdout)} B out, real data {'IDENTICAL' if ok else 'DIFFERS'}"
                  f" (far end: {far.group(1) if far else '?'} a slot)")
        else:
            ok = len(p.stdout) == 0
            print(f"{name:10s} as station 0 (its own): {len(p.stdout)} B out {'(right: none)' if ok else '(WRONG)'}")
        if not ok:
            bad += 1
            print("\n".join(err.splitlines()[-12:]))
print("PASS" if not bad else f"FAIL ({bad})")
sys.exit(1 if bad else 0)
