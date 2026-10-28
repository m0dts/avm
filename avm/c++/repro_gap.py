"""Mode D (or any) with a fragment gap: the C++ transmitter's waveform, 40 dB
noise, through avm_rx in test steps -- the case test_avm_tx.py's "550k gap"
loses fragments on. Prints the receiver's fragment lines.

    python repro_gap.py [MODE OCC MOD GAP_MS] [--python]   (default D 20 qpsk 30)"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
AVM = os.path.dirname(HERE)
EXT = ".exe" if sys.platform == "win32" else ""
WORK = os.path.join(HERE, "_e2e")
os.makedirs(WORK, exist_ok=True)
args = [a for a in sys.argv[1:] if not a.startswith("--")]
mode, occ, mod, gap = (args + ["D", "20", "qpsk", "30"][len(args):])[:4]
rng = np.random.default_rng(31)
payload = rng.integers(0, 256, 9000, dtype=np.uint8).tobytes()
tx = subprocess.run([os.path.join(HERE, "avm_tx" + EXT), "--mode", mode, "--occupancy", occ, "--modulation", mod,
                     "--fec", "ldpc", "--stream", "--fragment-size", "1024", "--tx-warmup-s", "0", "--amplitude", "0.3",
                     "--sample-rate", "550000", "--fragment-gap-ms", gap], input=payload, capture_output=True)
b = np.frombuffer(tx.stdout, np.complex64)
x = np.concatenate([np.zeros(20000, np.complex64), b, np.zeros(200000, np.complex64)])
sig = float(np.sqrt(np.mean(np.abs(b) ** 2)))
x = x + (0.01 * sig / np.sqrt(2)) * (rng.standard_normal(len(x)) + 1j * rng.standard_normal(len(x)))
iq = os.path.join(WORK, "gap.iq")
x.astype(np.complex64).tofile(iq)
rxa = ["--mode", mode, "--occupancy", occ, "--modulation", mod, "--fec", "ldpc", "--sample-rate", "550000",
       "--fragment-size", "1024", "--no-output-pacing"]
if "--python" in sys.argv:
    cmd = [sys.executable, os.path.join(HERE, "e2e_rx.py"), iq] + rxa     # (E2E_CHUNK: 131072, the same steps)
else:
    cmd = [os.path.join(HERE, "avm_rx" + EXT)] + rxa + ["--test-step", "131072"]
with open(iq, "rb") as fi:
    p = subprocess.run(cmd, stdin=fi, capture_output=True, env=dict(os.environ, HF_OFDM_RX_WAIT_EOF="1"))
for ln in p.stderr.decode(errors="replace").splitlines():
    if ln.startswith(("Fragment", "  !", "Stream ended")):
        print(ln[:150])
print(f"decoded {len(p.stdout)} of {len(payload)} bytes; {len(b) / 550000:.2f} s of signal")
