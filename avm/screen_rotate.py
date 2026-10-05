"""Turn the screen (and its touch input) upside down while AVM runs, for a
touch screen mounted the other way up. Linux only; the installer asks and
writes the choice to `screen_rotation` beside this file ("180" or "0").

X11 (Raspberry Pi OS desktop on X, LXDE, a bare startx): xrandr turns the
picture and xinput the touch input, so taps still land where they look.
Wayland (wlroots desktops: labwc, Wayfire): wlr-randr turns the output; the
compositor maps touch to it. Everything is put back when AVM quits.

    python screen_rotate.py on|off     # try it by hand
"""
import os
import shutil
import subprocess
import sys

SETTING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_rotation")
# touch / pen: x -> 1-x, y -> 1-y (and back to normal)
_MATRIX_180 = ["-1", "0", "1", "0", "-1", "1", "0", "0", "1"]
_MATRIX_0 = ["1", "0", "0", "0", "1", "0", "0", "0", "1"]


def wanted():
    """True if the installer was told to turn the screen upside down."""
    try:
        with open(SETTING) as f:
            return f.read().strip() == "180"
    except OSError:
        return False


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _x11_outputs():
    """Connected X outputs, e.g. ['DSI-1'] or ['HDMI-1']."""
    return [line.split()[0] for line in _run(["xrandr", "--query"]).splitlines()
            if " connected" in line]


def _x11_touch_ids():
    """xinput ids of touchscreens / tablets (pointers with absolute axes)."""
    ids = []
    for line in _run(["xinput", "list", "--short"]).splitlines():
        low = line.lower()
        if ("touch" in low or "ft5x06" in low or "goodix" in low or "ilitek" in low
                or "pen" in low or "stylus" in low) and "id=" in line and "pointer" in low:
            ids.append(line.split("id=")[1].split()[0])
    return ids


def _wayland_outputs():
    return [line.split()[0] for line in _run(["wlr-randr"]).splitlines()
            if line and not line[0].isspace()]


def apply(on):
    """Upside down (on=True) or normal. Returns a short description of what
    was done, or "" if nothing could be (no tool, not Linux, no display)."""
    if not sys.platform.startswith("linux"):
        return ""
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wlr-randr"):
        outs = _wayland_outputs()
        for out in outs:
            _run(["wlr-randr", "--output", out, "--transform", "180" if on else "normal"])
        return f"Wayland outputs {', '.join(outs)}" if outs else ""
    if os.environ.get("DISPLAY") and shutil.which("xrandr"):
        outs = _x11_outputs()
        for out in outs:
            _run(["xrandr", "--output", out, "--rotate", "inverted" if on else "normal"])
        touch = _x11_touch_ids() if shutil.which("xinput") else []
        for dev in touch:
            _run(["xinput", "set-prop", dev, "Coordinate Transformation Matrix"]
                 + (_MATRIX_180 if on else _MATRIX_0))
        return (f"X11 outputs {', '.join(outs)}" + (f", touch {len(touch)} device(s)" if touch else "")
                if outs else "")
    return ""


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("on", "off"):
        print(__doc__)
        sys.exit(2)
    print(apply(sys.argv[1] == "on") or "nothing to rotate (no xrandr / wlr-randr, or no display)")
