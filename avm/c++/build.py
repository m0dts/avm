"""Builds the AVM C++ library, one shared library from all the sources, and
the receiver program on it:

    python build.py          avm.dll + avm_rx.exe (Windows, zig's clang)
                             libavm.so + avm_rx (g++)

Sources: wavelet_core.cpp (video codec), framing.cpp (framer/deframer),
ldpc.cpp (LDPC code), avm_engines.cpp (the receiver and transmitter engines, rx_engine.cpp and tx_engine.cpp, and their C APIs;
it includes modem_rx.cpp, the modem: transmitter, receivers, front end,
which includes modem_tx.cpp). -ffp-contract=off keeps the float
results identical to the Python reference (the comparison tests)."""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = ["wavelet_core.cpp", "framing.cpp", "ldpc.cpp", "avm_engines.cpp"]
FLAGS = ["-O3", "-std=c++17", "-shared", "-ffp-contract=off", "-fno-fast-math"]


def main():
    if sys.platform == "win32":
        out = "avm.dll"
        cmd = [sys.executable, "-m", "ziglang", "c++"] + FLAGS + ["-target", "x86_64-windows-gnu", "-o", out, "-lws2_32"]
    else:
        out = "libavm.so"
        cxx = os.environ.get("CXX") or shutil.which("g++") or "c++"
        cmd = [cxx] + FLAGS + ["-fPIC", "-mcpu=native" if os.uname().machine.startswith(("aarch64", "arm"))
                               else "-march=native", "-o", out]
    cmd += SOURCES
    print(" ".join(cmd))
    r = subprocess.run(cmd, cwd=HERE)
    if r.returncode:
        sys.exit(r.returncode)
    # the per-part libraries this replaces
    for old in ("wavelet_core", "framing", "ldpc", "modem", "modem_tx"):
        for name in (f"{old}.dll", f"{old}.lib", f"{old}.pdb", f"lib{old}.so"):
            p = os.path.join(HERE, name)
            if os.path.exists(p):
                os.remove(p)
    print("built", out)
    # the programs, standalone: the engine / modem compiled in
    for prog, srcs in (("avm_rx", ["avm_rx.cpp", "rx_engine.cpp", "ldpc.cpp"]), ("avm_tx", ["avm_tx.cpp", "tx_engine.cpp", "ldpc.cpp"])):
        exe = prog + (".exe" if sys.platform == "win32" else "")
        if sys.platform == "win32":
            cmd2 = [sys.executable, "-m", "ziglang", "c++", "-O3", "-std=c++17", "-ffp-contract=off",
                    "-fno-fast-math", "-target", "x86_64-windows-gnu", "-o", exe, "-lws2_32"] + srcs
        else:
            cmd2 = [cmd[0], "-O3", "-std=c++17", "-ffp-contract=off", "-fno-fast-math",
                    "-mcpu=native" if os.uname().machine.startswith(("aarch64", "arm")) else "-march=native",
                    "-o", exe] + srcs + ["-pthread"]
        print(" ".join(cmd2))
        r = subprocess.run(cmd2, cwd=HERE)
        if r.returncode:
            sys.exit(r.returncode)
        print("built", exe)


if __name__ == "__main__":
    main()
