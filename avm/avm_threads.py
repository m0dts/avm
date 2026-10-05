"""Make Python thread names visible to the OS (top -H, ps -L), so CPU per
thread can be attributed: all Python threads otherwise show as "python".

    import avm_threads; avm_threads.install()
    threading.Thread(target=..., name="rx-spectrum")

Linux only (prctl PR_SET_NAME, 15 characters); a no-op elsewhere."""
import os
import sys
import threading

_PR_SET_NAME = 15
_installed = False


def _set_native_name(name):
    try:
        import ctypes
        libc = ctypes.CDLL(None)
        libc.prctl(_PR_SET_NAME, name.encode("ascii", "replace")[:15], 0, 0, 0)
    except Exception:
        pass


_PR_SET_PDEATHSIG = 1
_prctl = None


def die_with_parent():
    """subprocess.Popen/run kwargs so the child gets SIGTERM when its parent
    dies (Linux): an ffmpeg capturing a camera that delivers no frames never
    writes, so it never sees the closed pipe and would otherwise hold the
    camera forever after TX stops. The signal is tied to the thread that
    started the child: only use from a thread that lives as long as it.
    Empty elsewhere."""
    global _prctl
    if not sys.platform.startswith("linux"):
        return {}
    if _prctl is None:
        try:
            import ctypes
            _prctl = ctypes.CDLL(None, use_errno=True).prctl  # looked up before fork
        except Exception:
            return {}
    import signal
    sig = int(signal.SIGTERM)
    return {"preexec_fn": lambda: _prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0)}


def install(main_name=None):
    """Patch threading.Thread so every thread started afterwards carries its
    Python name at the OS level; optionally name the calling (main) thread."""
    global _installed
    if not sys.platform.startswith("linux"):
        return
    if main_name:
        _set_native_name(main_name)
    if _installed:
        return
    _installed = True
    orig_bootstrap = threading.Thread._bootstrap_inner

    def _bootstrap_inner(self):
        _set_native_name(self.name)
        orig_bootstrap(self)

    threading.Thread._bootstrap_inner = _bootstrap_inner


# The ffmpeg the installer chose: it writes the folder to ffmpeg_dir.txt
# beside this file (Windows: a "full" build, with Codec2). None without that
# file (Linux: the system ffmpeg, found on PATH as usual).
def _installed_ffmpeg():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_dir.txt")) as f:
            d = f.read().strip()
    except OSError:
        return None
    exe = os.path.join(d, "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    return exe if d and os.path.isfile(exe) else None


INSTALLED_FFMPEG = _installed_ffmpeg()
if INSTALLED_FFMPEG:  # also first on PATH, for anything that searches
    os.environ["PATH"] = os.path.dirname(INSTALLED_FFMPEG) + os.pathsep + os.environ.get("PATH", "")


def ffmpeg_exe():
    """The ffmpeg AVM runs: the installer's, else the first on PATH."""
    import shutil
    return INSTALLED_FFMPEG or shutil.which("ffmpeg") or "ffmpeg"


# Windows: AVM's GUI has no console, so every console program it starts
# (python helpers, ffmpeg, ...) would otherwise pop up its own black console
# window -- and their own helpers likewise. Every AVM process that starts
# others imports this module, so this one patch covers them all: each new
# process is created with CREATE_NO_WINDOW (added to any creationflags given,
# e.g. a priority class). It also runs a bare "ffmpeg" as the installer's
# ffmpeg by full path: Windows' own search can find another one first (e.g.
# the one conda puts in AVM's Python environment, without Codec2). Elsewhere:
# nothing.
if sys.platform == "win32":
    import subprocess as _subprocess

    _CREATE_NO_WINDOW = 0x08000000
    _popen_init = _subprocess.Popen.__init__

    def _popen_init_no_window(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        cmd = args[0] if args else kwargs.get("args")
        if (INSTALLED_FFMPEG and isinstance(cmd, (list, tuple)) and cmd
                and str(cmd[0]).lower() in ("ffmpeg", "ffmpeg.exe")):
            cmd = [INSTALLED_FFMPEG] + list(cmd[1:])
            if args:
                args = (cmd,) + args[1:]
            else:
                kwargs["args"] = cmd
        _popen_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _popen_init_no_window
