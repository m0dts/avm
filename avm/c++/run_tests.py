"""Every C++-vs-Python comparison test against the built AVM library, in
turn (short runs), each test's summary lines printed; then the
end-to-end check.

    python run_tests.py [--quick] [--no-e2e]"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
quick = "--quick" in sys.argv
TESTS = [
    ["test_npy_rng.py"], ["test_fft.py"], ["test_ldpc.py", "40" if quick else "150"],
    ["compare_codecs.py"], ["test_framing.py"],
    ["test_modem_tx.py"], ["test_modem_rx.py", "1" if quick else "3"], ["test_sync.py", "1" if quick else "3"],
    ["test_frontend.py"], ["test_rx_frontend.py"], ["test_sc_rx.py", "2" if quick else "4"],
    ["test_sch_rx.py", "1" if quick else "2"],
]
if "--no-e2e" not in sys.argv:
    some = ["A:20:qpsk", "S:20:8psk"] if quick else []
    TESTS.append(["test_e2e.py"] + some)
    TESTS.append(["test_avm_rx.py"] + some)      # uses the channel files test_e2e.py writes
    TESTS.append(["test_avm_tx.py"] + some)
    if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gui", "test", "A80_video.iq")):
        TESTS.append(["test_rx_api.py"])         # the in-process receiver (the GUI's), on the GUI's test recording
if quick:
    TESTS = [t for t in TESTS if t[0] != "test_framing.py"]     # 600 streams: minutes on a Pi

for t in TESTS:
    t0 = time.time()
    print(f"===== {' '.join(t)}", flush=True)
    p = subprocess.run([sys.executable] + t, cwd=HERE, capture_output=True, text=True)
    lines = [ln for ln in (p.stdout + p.stderr).splitlines()
             if ln.strip() and not ln.startswith(("  File", "Traceback")) and "~~" not in ln and "^^" not in ln]
    for ln in lines[-14:]:
        print("   " + ln)
    print(f"   ({time.time() - t0:.0f} s{'' if p.returncode == 0 else f', EXIT {p.returncode}'})", flush=True)
