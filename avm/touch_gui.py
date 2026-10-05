#!/usr/bin/env python3
"""
Touch GUI for AVM, the Audio Video Modem: one full-screen window with a TX view and
an RX view (toggle at the top), big touch controls, dark grey theme --
designed for a 7" 800x480 touchscreen, scaling with the screen. A trimmed
front end over the full GUIs (media_tx_gui.py / media_rx_gui.py), which run
hidden as the engines; those stay available for everything this leaves out.

The toggle only switches the view: TX and RX each keep running until
stopped. Gains apply live; other changes while running turn the
start/stop button into RESTART TO APPLY. Settings are remembered in
~/.config/hfmodem/touch.json.

    python touch_gui.py                  # full screen
    HF_TOUCH_SIZE=800x480 python touch_gui.py   # window, e.g. to try the
                                                # 7" layout on a big monitor
"""
import json
import os
import sys

# One BLAS thread per process (see hf_ofdm_tx.py); before numpy loads.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # engines start their scripts by relative path

import avm_version
import gui_layout

# The desktop theme (qt5ct) would override our fonts; and on a Wayland
# desktop run natively (Xwayland costs ~a third of a Pi 4 core).
os.environ["HF_GUI_COMPACT"] = "0"  # the hidden engines mustn't restyle the app
if os.environ.get("QT_QPA_PLATFORMTHEME") == "qt5ct":
    del os.environ["QT_QPA_PLATFORMTHEME"]
if os.environ.get("WAYLAND_DISPLAY") and "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "wayland"

from PyQt5 import QtCore, QtWidgets

import touch_widgets as tw
from touch_rx_page import RxPage
from touch_tx_page import TxPage

CPU_UPDATE_MS = 2000


def _cpu_times():
    """(busy, total) jiffies over all cores from /proc/stat; None where
    there's no /proc (Windows)."""
    try:
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
    return sum(v) - idle, sum(v)


SETTINGS_PATH =os.path.join(os.path.expanduser("~"), ".config", "hfmodem", "touch.json")


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


class TouchWindow(QtWidgets.QWidget):
    def __init__(self, app, scale):
        super().__init__()
        self.app = app
        self.scale = scale
        tw.SCALE = scale
        app.setStyleSheet(tw.stylesheet(scale, tw.RX_ACCENT))  # pages are built under the right sizes
        self.settings = load_settings()
        self.setWindowTitle(avm_version.TITLE)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(8, 6, 8, 2)
        self.view = tw.Segmented([("tx", "TX"), ("rx", "RX")], self.settings.get("view", "rx"))
        for b in self.view._buttons.values():
            b.setObjectName("toggle")
        self.view.changed.connect(self._show_view)
        bar.addWidget(self.view)
        bar.addSpacing(16)
        self.indicator = QtWidgets.QLabel()
        self.indicator.setObjectName("value")
        # may shrink to nothing, so the radio picker and quit always fit
        self.indicator.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        bar.addWidget(self.indicator, 1)
        # whole-system CPU (all cores), every CPU_UPDATE_MS
        self.cpu = QtWidgets.QLabel("CPU --")
        self.cpu.setObjectName("dim")
        bar.addWidget(self.cpu)
        bar.addSpacing(12)
        self._cpu_last = _cpu_times()
        self._cpu_timer = QtCore.QTimer(self)
        self._cpu_timer.timeout.connect(self._update_cpu)
        self._cpu_timer.start(CPU_UPDATE_MS)
        quit_btn = QtWidgets.QPushButton("✕")
        quit_btn.setObjectName("toggle")
        quit_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        quit_btn.setFixedWidth(round(56 * scale))
        quit_btn.clicked.connect(self._quit)
        bar.addWidget(quit_btn)
        root.addLayout(bar)

        self.pages = QtWidgets.QStackedWidget()
        self.tx = TxPage(self.settings)
        self.rx = RxPage(self.settings)
        # each page checks the other before starting: one LimeSDR can't be
        # opened by TX and RX at once (separate processes; a Pluto can)
        self.tx.peer, self.rx.peer = self.rx, self.tx
        self.pages.addWidget(self.tx)
        self.pages.addWidget(self.rx)
        root.addWidget(self.pages, 1)
        for p in (self.tx, self.rx):
            p.running_changed.connect(lambda _: self._update_indicator())
        # Each page keeps its own accent colour (checked buttons), set once:
        # re-applying the app-wide stylesheet on every TX/RX switch restyled
        # every widget, hidden engines included -- a visibly slow switch.
        for page, accent in ((self.tx, tw.TX_ACCENT), (self.rx, tw.RX_ACCENT)):
            page.setStyleSheet(tw.accent_stylesheet(accent))

        self._show_view(self.view.value() or "rx")
        self._update_indicator()

    def _update_cpu(self):
        now = _cpu_times()
        if now and self._cpu_last:
            busy, total = now[0] - self._cpu_last[0], now[1] - self._cpu_last[1]
            if total > 0:
                self.cpu.setText(f"CPU {100 * busy / total:.0f}%")
        self._cpu_last = now

    def _show_view(self, which):
        self.pages.setCurrentWidget(self.tx if which == "tx" else self.rx)
        self.view.setStyleSheet(tw.accent_stylesheet(tw.TX_ACCENT if which == "tx" else tw.RX_ACCENT))
        self.settings["view"] = which

    def _update_indicator(self):
        parts = []
        if self.tx.is_running():
            parts.append(f"<span style='color:{tw.STOP}'>● ON AIR</span>")
        if self.rx.is_running():
            parts.append(f"<span style='color:{tw.RX_ACCENT}'>● RX</span>")
        self.indicator.setText("&nbsp;&nbsp;&nbsp;".join(parts) or
                               f"<span style='color:{tw.TEXT_DIM}'>{avm_version.SHORT_TITLE}</span>")

    def _quit(self):
        if self.tx.is_running() or self.rx.is_running():
            box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Question, "Quit",
                                        "TX/RX is running. Stop and quit?",
                                        QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, self)
            if box.exec_() != QtWidgets.QMessageBox.Yes:
                return
        self.close()

    def closeEvent(self, event):
        self.tx.shutdown()
        self.rx.shutdown()
        save_settings(self.settings)
        event.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    import avm_threads
    avm_threads.install(main_name="avm-gui")  # thread names visible in top -H
    gui_layout.set_app_id("hfmodem-touch")
    app.setStyle("Fusion")
    size = os.environ.get("HF_TOUCH_SIZE")
    if size:
        w, h = (int(v) for v in size.lower().split("x"))
    else:
        geo = app.primaryScreen().geometry()
        w, h = geo.width(), geo.height()
    scale = max(0.75, h / 480)
    win = TouchWindow(app, scale)
    # SIGTERM / Ctrl-C: close normally, so TX/RX and their helper processes
    # are stopped -- killed outright, they used to carry on as orphans. Qt
    # only lets Python handle a signal between events, hence the timer.
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: win.close())
    wake = QtCore.QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)
    if size:
        win.resize(w, h)
        win.show()
    else:
        win.showFullScreen()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
