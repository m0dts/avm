"""hf_ofdm_rx.py's real main loop on an IQ file, deterministically: its
stdin reader is replaced by one that adds exactly one CHUNK-byte chunk per
wait_for_more() call (and a first one up front), so every run sees the same
buffer states at the same points -- the live reader's timing made two runs
of the same file differ (a decode tried on a partly arrived fragment).
Same command line as hf_ofdm_rx.py, plus the file:

    [AVM_CPP=1] python e2e_rx.py IQFILE --mode A ... > out.bin"""
import os
import sys
import threading

AVM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, AVM)
CHUNK = int(os.environ.get("E2E_CHUNK", str(1 << 17)))

path = sys.argv.pop(1)
sys.argv[0] = os.path.join(AVM, "hf_ofdm_rx.py")
import hf_ofdm_common as ofdm  # noqa: E402
import hf_ofdm_rx as rx  # noqa: E402


class ScriptedReader:
    def __init__(self, chunk_size=1 << 15):
        self._data = open(path, "rb").read()
        self._pos = 0
        self.chunks = ofdm.ChunkList()
        self.total_len = 0
        self.eof = False
        self._lock = threading.Lock()
        self._add()

    def _add(self):
        with self._lock:
            if self._pos >= len(self._data):
                self.eof = True
                return
            c = self._data[self._pos:self._pos + CHUNK]
            self._pos += len(c)
            self.chunks.append(c)
            self.total_len += len(c)
            if self._pos >= len(self._data):
                self.eof = True

    def wait_for_more(self, since_len, timeout=None):
        if self.total_len <= since_len and not self.eof:
            self._add()

    def close(self):
        pass


rx.StdinReader = ScriptedReader

import numpy as np  # noqa: E402
import rx_frontend  # noqa: E402


class SyncFrontEndReader:
    """rx_frontend.FrontEndReader without its thread: wait_for_more() pulls
    the next input chunk through the front end itself, so the modem-rate
    buffer grows in the same steps on every run."""

    def __init__(self, inner, frontend, stats_interval_s=None):
        self._inner = inner
        self._fe = frontend
        self.chunks = ofdm.ChunkList()
        self.total_len = 0
        self.eof = False
        self._lock = threading.Lock()
        self._taken = 0
        self._leftover = b""
        self._pull()

    def _pull(self):
        new = self._inner.chunks[self._taken:]
        self._taken = len(self._inner.chunks)
        if new:
            data = self._leftover + b"".join(new)
            usable = len(data) - len(data) % 8
            self._leftover = data[usable:]
            if usable:
                x = np.frombuffer(data[:usable], np.complex64)
                x = np.where(np.isfinite(x), x, 0).astype(np.complex64)
                y = self._fe.process(x)
                if len(y):
                    c = y.astype(np.complex64, copy=False).tobytes()
                    self.chunks.append(c)
                    self.total_len += len(c)
        if self._inner.eof and self._taken == len(self._inner.chunks):
            self.eof = True

    def wait_for_more(self, since_len, timeout=None):
        while not (self.eof or self.total_len > since_len):
            self._inner.wait_for_more(self._inner.total_len)
            self._pull()

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


rx_frontend.FrontEndReader = SyncFrontEndReader

if os.environ.get("E2E_TRACE"):
    # every modem call with a short digest of its arguments and result, to
    # find where two runs part (stderr, "TRACE" lines)
    import numpy as np

    def digest(v):
        if isinstance(v, np.ndarray) or hasattr(v, "__len__") and hasattr(v, "_buf"):
            a = np.asarray(v[:len(v)]) if not isinstance(v, np.ndarray) else v
            if a.size == 0:
                return "[]"
            if np.iscomplexobj(a):
                return f"[{len(a)} {complex(a[0]):.6g}..{complex(a[-1]):.6g} s={complex(np.sum(a)):.6g}]"
            return f"[{len(a)} s={float(np.sum(a.astype(float))):.6g}]"
        if isinstance(v, (bytes, bytearray)):
            return f"b[{len(v)}]"
        if isinstance(v, float):
            return f"{v:.9g}"
        if isinstance(v, tuple):
            return "(" + ", ".join(digest(x) for x in v) + ")"
        return repr(v)[:60]

    def wrap(name):
        f = getattr(ofdm, name)

        def g(*a, **k):
            r = f(*a, **k)
            args = ", ".join(digest(x) for x in a if not hasattr(x, "n_fft")) + (f" {k}" if k else "")
            print(f"TRACE {name}({args}) -> {digest(r)}", file=sys.stderr)
            return r
        setattr(ofdm, name, g)

    for name in ("schmidl_cox_sync", "refine_frame_start", "resolve_integer_cfo", "resolve_timing_offset",
                 "decode_frame", "linear_resample_incremental", "apply_lowpass_filter_incremental",
                 "correct_iq_imbalance"):
        wrap(name)

rx.main()
