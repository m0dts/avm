#!/usr/bin/env python3
"""AVM is a C++ program now (avm_gui). This file stays only for AVMs up to
1.0.x, which were Python: their Update button downloads the new files,
runs the installer (which builds or fetches avm_gui) and restarts
"python touch_gui.py" -- which lands here and starts the C++ AVM.

If avm_gui isn't there (the installer couldn't finish), it says how to
finish the update instead."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = (os.path.join(HERE, "gui", "build", "avm_gui"),       # Linux: built by install_avm.sh
              os.path.join(HERE, "app", "avm_gui.exe"))            # Windows: the release zip


def main():
    # what the Python GUI set for itself, inherited through its restart: it forced
    # QT_QPA_PLATFORM=wayland under Wayland, and the C++ AVM's Qt (no Wayland
    # plug-in on a Pi) then quit at once instead of using X11 -- Qt chooses
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    for exe in CANDIDATES:
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            if sys.platform == "win32":
                import subprocess
                subprocess.Popen([exe] + sys.argv[1:], cwd=os.path.dirname(exe), env=env)
                return
            os.chdir(os.path.dirname(exe))
            os.execve(exe, [exe] + sys.argv[1:], env)
    if sys.platform == "win32":
        how = "run install_avm.bat (in your avm folder) again"
    else:
        how = f"in a terminal, run:\n\n    bash {HERE}/install_avm.sh"
    text = ("AVM has been updated to the new C++ version, but it isn't installed yet "
            f"(the installer didn't finish).\n\nTo finish the update, {how}")
    print(text, file=sys.stderr)
    try:
        from PyQt5 import QtWidgets
        app = QtWidgets.QApplication(sys.argv)
        QtWidgets.QMessageBox.warning(None, "AVM", text)
    except Exception:
        pass
    sys.exit(1)


if __name__ == "__main__":
    main()
