"""A pre-loaded Python process for the GUI's TX/RX scripts (see warm_pool.py).

    python warm_python.py hf_ofdm_rx.py      (AVM_WARM_FD / AVM_WARM_STATUS_FD set)

Imports the script as a module -- everything it imports (numpy, numba and its
cached kernels, the modem) loads now, but its `if __name__ == "__main__"`
block does not run, so the radio is NOT touched. Writes "ready" on the status
pipe, then waits on the control pipe for one JSON line {"argv": [...]} and
runs the script as __main__ with that command line -- exactly as if started
fresh, minus the ~3.5 s of imports on a Raspberry Pi 4. EOF on the control
pipe (the GUI closed, or threw this one away) just exits."""
import json
import os
import runpy
import sys


def main():
    script = sys.argv[1]
    ctl_fd = int(os.environ.pop("AVM_WARM_FD"))
    status_fd = int(os.environ.pop("AVM_WARM_STATUS_FD", "-1"))
    here = os.path.dirname(os.path.abspath(script))
    if here not in sys.path:
        sys.path.insert(0, here)
    name = os.path.splitext(os.path.basename(script))[0]
    try:
        __import__(name)
    except Exception as e:   # the real run below reports it properly
        print(f"[warm] preloading {name} failed: {e!r}", file=sys.stderr)
    # AVM_WARM_PREOPEN=driver|uri|RX: a PlutoSDR is also opened now (no
    # configuring, no stream: its iiod serves several clients, so an idle
    # open blocks nothing). The script's own preopen at launch then finds it.
    spec = os.environ.pop("AVM_WARM_PREOPEN", "")
    if spec.count("|") == 2:
        driver, uri, what = spec.split("|")
        try:
            import pluto_soapy_sink
            pluto_soapy_sink.preopen(driver, uri or None, what)
        except Exception as e:
            print(f"[warm] opening the radio in advance failed: {e!r}", file=sys.stderr)
    if status_fd >= 0:
        try:
            os.write(status_fd, b"ready\n")
            os.close(status_fd)
        except OSError:
            pass
    with os.fdopen(ctl_fd, "r") as ctl:
        line = ctl.readline()
    if not line.strip():
        return 0
    sys.argv = [script] + list(json.loads(line)["argv"])
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
