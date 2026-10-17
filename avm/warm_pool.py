"""Pre-loaded ("warm") processes for hf_ofdm_tx.py / hf_ofdm_rx.py, so a
TX/RX start doesn't pay their ~3.5 s of imports (numpy, numba, the modem) on
a Raspberry Pi 4 -- see warm_python.py. Linux only (pass_fds); elsewhere, or
with AVM_NO_WARM=1, take() returns None and the GUI starts processes as
before.

A warm process is made with the stdio the GUI would give the real one (the
RX: pipes; the TX: its stdin a pipe whose write end goes to the framer at
start), sits idle (no CPU, no radio) until launched, and is made only while
nothing is starting -- at GUI start and a few seconds after each stop -- so
preloading never competes with a live start.

state(kind) -> "off" | "loading" | "ready" (for the TX/RX pages' status)."""
import json
import os
import subprocess
import sys
import threading

import avm_threads

ENABLED = sys.platform.startswith("linux") and os.environ.get("AVM_NO_WARM") != "1"
SCRIPTS = {"rx": "hf_ofdm_rx.py", "tx": "hf_ofdm_tx.py"}


# radios safe to open in advance: a PlutoSDR (iiod: several clients at once).
# Not a LimeSDR (opening claims its USB device: the other side couldn't).
PREOPEN_DRIVERS = ("pluto",)


class _Warm:
    def __init__(self, kind, radio=None):
        self.kind = kind
        self.radio = radio
        self.ready = False
        self.stdin_w = None
        ctl_r, self.ctl_w = os.pipe()
        st_r, st_w = os.pipe()
        if kind == "tx":
            data_r, self.stdin_w = os.pipe()   # the framer's stdout, at start
            stdin = data_r
        else:
            stdin = subprocess.PIPE
        env = dict(os.environ, AVM_WARM_FD=str(ctl_r), AVM_WARM_STATUS_FD=str(st_w))
        if radio is not None:
            env["AVM_WARM_PREOPEN"] = f"{radio[0]}|{radio[1] or ''}|{kind.upper()}"
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "warm_python.py", SCRIPTS[kind]], stdin=stdin,
                stdout=subprocess.DEVNULL if kind == "tx" else subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, pass_fds=(ctl_r, st_w),
                **avm_threads.die_with_parent())
        finally:
            os.close(ctl_r)
            os.close(st_w)
            if kind == "tx":
                os.close(data_r)
        threading.Thread(target=self._wait_ready, args=(st_r,), daemon=True,
                         name=f"warm-{kind}").start()

    def _wait_ready(self, fd):
        with os.fdopen(fd, "rb") as f:
            self.ready = f.readline().strip() == b"ready"

    def alive(self):
        return self.proc.poll() is None

    def launch(self, argv):
        """Run it with this command line (argv without the python/script)."""
        os.write(self.ctl_w, (json.dumps({"argv": list(argv)}) + "\n").encode())
        os.close(self.ctl_w)
        self.ctl_w = None
        return self.proc

    def discard(self):
        for fd in (self.ctl_w, self.stdin_w):
            if fd is not None:
                try:
                    os.close(fd)   # EOF: it exits by itself
                except OSError:
                    pass
        self.ctl_w = self.stdin_w = None
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


_pool = {}
_lock = threading.Lock()


def prewarm(kind, driver=None, uri=None):
    """Make a warm process of this kind if there isn't a live one (for this
    radio). driver/uri: the radio it will use -- a PlutoSDR is opened in
    advance too. Call from the GUI thread (the process is tied to its
    parent thread's life)."""
    if not ENABLED:
        return
    radio = (driver, uri or None) if driver in PREOPEN_DRIVERS else None
    with _lock:
        w = _pool.get(kind)
        if w is not None and w.alive() and w.radio == radio:
            return
        if w is not None:
            _pool.pop(kind)
            w.discard()   # made for another radio
        try:
            _pool[kind] = _Warm(kind, radio)
        except OSError:
            _pool.pop(kind, None)


def take(kind):
    """A warm process (removed from the pool), or None."""
    with _lock:
        w = _pool.pop(kind, None)
    if w is None:
        return None
    if not w.alive():
        w.discard()
        return None
    return w


def state(kind):
    if not ENABLED:
        return "off"
    w = _pool.get(kind)
    if w is None or not w.alive():
        return "off"
    return "ready" if w.ready else "loading"


def shutdown():
    with _lock:
        ws = list(_pool.values())
        _pool.clear()
    for w in ws:
        w.discard()
