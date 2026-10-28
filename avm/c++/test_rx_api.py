"""The in-process receiver (rx_engine.h's avm_rx_* API, as the GUI uses it)
against avm_rx the program: the same IQ through both, the same payload.

    python test_rx_api.py [IQ_FILE]   (default ../gui/test/A80_video.iq,
                                       mode A 80 kHz QPSK LDPC at 550 kS/s)

Also checks a stop part-way through returns promptly (avm_rx_destroy)."""
import ctypes
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXT = ".exe" if sys.platform == "win32" else ""
IQ = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "gui", "test", "A80_video.iq")
ARGS = ["--mode", "A", "--occupancy", "80", "--modulation", "qpsk", "--fec", "ldpc",
        "--sample-rate", "550000", "--fragment-size", "1024", "--no-output-pacing"]

DATA = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_size_t)
LOG = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p)
DONE = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


class CB(ctypes.Structure):
    _fields_ = [("data", DATA), ("log", LOG), ("done", DONE), ("user", ctypes.c_void_p)]


def load():
    from _avmlib import lib
    L = lib()
    L.avm_rx_create.restype = ctypes.c_void_p
    L.avm_rx_create.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_char_p), ctypes.POINTER(CB)]
    L.avm_rx_push.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    L.avm_rx_end_input.argtypes = [ctypes.c_void_p]
    L.avm_rx_destroy.argtypes = [ctypes.c_void_p]
    return L


def run_api(L, iq, stop_after=None):
    out, lines, done = bytearray(), [], threading.Event()
    cb = CB(DATA(lambda u, p, n: out.extend(ctypes.string_at(p, n))),
            LOG(lambda u, t: lines.append(t.decode(errors="replace"))),
            DONE(lambda u: done.set()), None)
    argv = (ctypes.c_char_p * len(ARGS))(*[a.encode() for a in ARGS])
    h = L.avm_rx_create(len(ARGS), argv, ctypes.byref(cb))
    assert h, "avm_rx_create failed:\n" + "\n".join(lines)
    t0 = time.perf_counter()
    step = 1 << 16
    for i in range(0, len(iq), step):
        L.avm_rx_push(h, iq[i:i + step], len(iq[i:i + step]))
    if stop_after is None:
        L.avm_rx_end_input(h)
        done.wait(600)
    else:
        time.sleep(stop_after)
    ts = time.perf_counter()
    L.avm_rx_destroy(h)
    return bytes(out), lines, time.perf_counter() - t0, time.perf_counter() - ts


def main():
    iq = open(IQ, "rb").read()
    p = subprocess.run([os.path.join(HERE, "avm_rx" + EXT)] + ARGS, input=iq, capture_output=True)
    L = load()
    out, lines, dt, _ = run_api(L, iq)
    ok_lines = sum(1 for l in lines if ": OK" in l)
    same = out == p.stdout
    print(f"program: {len(p.stdout)} bytes; API: {len(out)} bytes, {ok_lines} OK fragments, {dt:.1f} s -> "
          f"{'SAME' if same else 'DIFFERENT'}")
    _, _, _, stop_s = run_api(L, iq, stop_after=0.05)
    print(f"stop part-way: avm_rx_destroy took {stop_s:.2f} s")
    sys.exit(0 if same and len(out) > 0 and stop_s < 3 else 1)


if __name__ == "__main__":
    main()
