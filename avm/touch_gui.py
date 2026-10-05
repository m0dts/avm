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
import threading
import time

# One BLAS thread per process (see hf_ofdm_tx.py); before numpy loads.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # engines start their scripts by relative path

import avm_threads
import avm_update
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
UPDATE_CHECK_DELAY_MS = 5000  # after start-up: GitHub check runs in the background
# Start screen: past this, it says the codec is compiling (a first start). A
# cached warm-up is ~1 s on a PC, 3.1 s on an Atom x5 (measured); a compile minutes.
SPLASH_DELAY_MS = 8000
RESCALE_DELAY_MS = 300  # resizable window: re-scale this long after resizing stops
SPLASH_HOLD_MS = 1000   # start screen stays this long after the codec is ready


# Compiles (first run) or loads (cached) the wavelet codec's encoder and
# decoder: the kernels are shape-generic, so one small size covers them all.
_WARMUP_CODE = "; ".join([
    "import numpy as np",
    "from wavelet_codec import WaveletCodec",
    "w, h = 192, 112",
    "z = lambda a, b: np.zeros((b, a), np.uint8)",
    "enc, dec = WaveletCodec(w, h, 200), WaveletCodec(w, h, 200)",
    "dec.decode(enc.encode(z(w, h), z(w // 2, h // 2), z(w // 2, h // 2)))",
])


def _start_codec_warmup():
    """Run the codec warm-up in its own process (None if it can't start)."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        return subprocess.Popen([sys.executable, "-c", _WARMUP_CODE], cwd=here,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **avm_threads.die_with_parent())
    except OSError:
        return None


class _UpdateResult(QtCore.QObject):
    """Carries the background GitHub check's result to the GUI thread."""
    done = QtCore.pyqtSignal(list)
USB_WATCH_MS = 2000   # USB watchdog poll (sysfs only: microseconds)
USB_RESUME_S = 4.0    # a device must be back this long before TX/RX restarts


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
        # a resizable window may shrink below its contents' current minimum:
        # _rescale then shrinks fonts and controls to fit
        root.setSizeConstraint(QtWidgets.QLayout.SetNoConstraint)
        self.setMinimumSize(480, 288)

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
        # shown only when GitHub has newer AVM files (see _start_update_check)
        self.update_btn = QtWidgets.QPushButton("⬆ Update")
        self.update_btn.setObjectName("toggle")
        self.update_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self.update_btn.setStyleSheet(f"color: {tw.RX_ACCENT};")
        self.update_btn.clicked.connect(self._show_update)
        self.update_btn.hide()
        bar.addWidget(self.update_btn)
        bar.addSpacing(8)
        self._update_files = []
        self._update_result = _UpdateResult()
        self._update_result.done.connect(self._update_checked)
        QtCore.QTimer.singleShot(UPDATE_CHECK_DELAY_MS, self._start_update_check)
        # warning button: errors seen this session (tw.notices); tap for the list
        self.notice_btn = QtWidgets.QPushButton("⚠")
        self.notice_btn.setObjectName("toggle")
        self.notice_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self.notice_btn.setFixedWidth(round(72 * scale))  # room for "⚠ 99"
        self.notice_btn.clicked.connect(lambda: tw.NoticesDialog(self).exec_())
        tw.notices().changed.connect(self._update_notice_btn)
        bar.addWidget(self.notice_btn)
        bar.addSpacing(12)
        self._update_notice_btn()
        # whole-system CPU (all cores), every CPU_UPDATE_MS
        self.cpu = QtWidgets.QLabel("CPU --")
        self.cpu.setObjectName("dim")
        bar.addWidget(self.cpu)
        bar.addSpacing(12)
        self._cpu_last = _cpu_times()
        self._cpu_timer = QtCore.QTimer(self)
        self._cpu_timer.timeout.connect(self._update_cpu)
        self._cpu_timer.start(CPU_UPDATE_MS)
        self.quit_btn = quit_btn = QtWidgets.QPushButton("✕")
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
        # Video codec warm-up, in the background from launch: the first run on
        # a machine compiles it (minutes on a slow CPU; seconds once cached).
        # TX waits for it rather than going on air without a picture.
        self._warmup = _start_codec_warmup()
        self.tx.codec_busy = lambda: self._warmup is not None and self._warmup.poll() is None
        self._warmup_timer = QtCore.QTimer(self)
        self._warmup_timer.timeout.connect(self._warmup_poll)
        self._warmup_timer.start(500)
        # Start screen over the whole window while that runs: a second or
        # two normally, and it stays, explaining, if this start compiles.
        self._warmup_t0 = time.monotonic()
        self._splash = self._build_splash()
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
        # USB watchdog: a radio or camera dropping off USB (a bad cable, a
        # browning-out device, or a Pi whose USB hub has crashed outright)
        # stops TX/RX cleanly with a message, and restarts it when the device
        # is back; losing every USB device at once is shown in the title bar.
        self._usb_seen = False   # any USB device seen since start
        self._usb_lost = False   # all of them gone: the USB system itself failed
        for page in (self.tx, self.rx):
            page._usb = {"watch": None, "resume": None, "back": None}
            # the user's own START/STOP cancels a pending automatic restart
            page.run.clicked.connect(lambda *_, p=page: p._usb.update(resume=None, back=None))
        self._usb_timer = QtCore.QTimer(self)
        self._usb_timer.timeout.connect(self._usb_tick)
        self._usb_timer.start(USB_WATCH_MS)
        self._update_indicator()
        if not self._splash.isHidden():
            self._splash.raise_()  # above everything built after it

    def _update_cpu(self):
        now = _cpu_times()
        if now and self._cpu_last:
            busy, total = now[0] - self._cpu_last[0], now[1] - self._cpu_last[1]
            if total > 0:
                self.cpu.setText(f"CPU {100 * busy / total:.0f}%")
        self._cpu_last = now

    def _build_splash(self):
        """Full-window start screen: the name large in the upper middle, a
        status line under it, the credit small at the bottom right."""
        w = QtWidgets.QWidget(self)
        w.setAutoFillBackground(True)
        w.setStyleSheet(f"background: {tw.BG};")
        lay = QtWidgets.QVBoxLayout(w)
        lay.setContentsMargins(round(16 * self.scale), 0, round(16 * self.scale), round(10 * self.scale))
        lay.addStretch(2)
        title = QtWidgets.QLabel("AudioVideoModem")
        title.setAlignment(QtCore.Qt.AlignCenter)
        title.setStyleSheet(f"color: {tw.TEXT}; font-size: {round(52 * self.scale)}px; font-weight: bold;")
        lay.addWidget(title)
        lay.addSpacing(round(18 * self.scale))
        self._splash_status = QtWidgets.QLabel("Preparing codecs...")
        self._splash_status.setAlignment(QtCore.Qt.AlignCenter)
        self._splash_status.setWordWrap(True)
        self._splash_status.setStyleSheet(f"color: {tw.TEXT_DIM};")
        lay.addWidget(self._splash_status)
        lay.addStretch(3)
        credit = QtWidgets.QLabel(f"{avm_version.SHORT_TITLE}  ·  by M0DTS and AI!")
        credit.setAlignment(QtCore.Qt.AlignRight)
        credit.setStyleSheet(f"color: {tw.TEXT_DIM}; font-size: {round(12 * self.scale)}px;")
        lay.addWidget(credit)
        w.setGeometry(self.rect())
        w.show()
        w.raise_()
        return w

    def _update_splash(self):
        secs = int(time.monotonic() - self._warmup_t0)
        if secs * 1000 < SPLASH_DELAY_MS:
            self._splash_status.setText("Preparing codecs...")
        else:  # still going: this start is compiling the codec
            self._splash_status.setText(
                "Preparing codecs, please wait...\n\nThe first start on this machine compiles "
                f"the video codec:\nthis can take a few minutes. Later starts are quick.\n\n{secs} s")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_splash"):
            self._splash.setGeometry(self.rect())
        # a resizable window (Windows): re-scale once the resizing stops
        if getattr(self, "scalable", False):
            if not hasattr(self, "_rescale_timer"):
                self._rescale_timer = QtCore.QTimer(self)
                self._rescale_timer.setSingleShot(True)
                self._rescale_timer.timeout.connect(self._rescale)
            self._rescale_timer.start(RESCALE_DELAY_MS)

    def _rescale(self):
        """Fonts and controls to suit the window's new height (1.0 = 480 px):
        the stylesheet carries most sizes; the few fixed ones are reset."""
        scale = max(0.75, self.height() / 480)
        if abs(scale - self.scale) < 0.03:
            return
        self.scale = tw.SCALE = scale
        self.app.setStyleSheet(tw.stylesheet(scale, tw.RX_ACCENT))
        self.notice_btn.setFixedWidth(round(72 * scale))
        self.quit_btn.setFixedWidth(round(56 * scale))
        self.rx.station.setFixedHeight(round(28 * scale))
        self.tx.fit_left_labels()
        tick_font = self.rx.font()
        tick_font.setPixelSize(max(9, round(11 * scale)))
        for side in ("left", "bottom"):
            self.rx.engine.spectrum_plot.getAxis(side).setStyle(tickFont=tick_font)

    def _warmup_poll(self):
        if self._warmup is not None and self._warmup.poll() is None:
            self._update_splash()
            return
        self._warmup_timer.stop()
        QtCore.QTimer.singleShot(SPLASH_HOLD_MS, self._splash.hide)  # a moment longer to read
        if self._warmup is not None and self._warmup.returncode:
            tw.notices().add("TX", "Video codec warm-up failed (exit "
                                   f"{self._warmup.returncode}); TX will compile it when it starts")
        self.tx.codec_ready()

    def _start_update_check(self):
        """Compare this AVM with GitHub on a background thread (a few small
        HTTPS requests); no network: no button, nothing else happens."""
        def run():
            try:
                changed = avm_update.check()
            except Exception:
                return
            self._update_result.done.emit(changed)
        threading.Thread(target=run, daemon=True, name="update-check").start()

    def _update_checked(self, changed):
        self._update_files = changed
        self.update_btn.setVisible(bool(changed))

    def _show_update(self):
        """Ask, then: stop TX/RX, fetch the new files from GitHub, restart."""
        files = self._update_files
        listed = "\n".join("   " + f for f in files[:10]) + ("\n   ..." if len(files) > 10 else "")
        box = QtWidgets.QMessageBox(
            QtWidgets.QMessageBox.Question, "Update available",
            f"A newer AVM is on GitHub ({len(files)} file(s) differ):\n{listed}\n\n"
            "Update now? TX/RX stop, AVM downloads the new files and restarts.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, self)
        if box.exec_() != QtWidgets.QMessageBox.Yes:
            return
        self.tx.shutdown()
        self.rx.shutdown()
        save_settings(self.settings)
        self.update_btn.setText("Updating...")
        self.update_btn.setEnabled(False)
        self.app.processEvents()
        try:
            avm_update.apply()
        except Exception as e:
            self.update_btn.setText("⬆ Update")
            self.update_btn.setEnabled(True)
            QtWidgets.QMessageBox.warning(self, "Update failed",
                                          f"Nothing was changed.\n\n{e}\n\nYou can also update with:\n"
                                          f"   {avm_update.UPDATE_CMD}")
            return
        # restart: the same program, arguments and environment, now updated
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _update_notice_btn(self):
        n = tw.notices().count()
        self.notice_btn.setText(f"⚠ {n}" if n else "⚠")
        # red while there's something to read, dim otherwise
        self.notice_btn.setStyleSheet(f"color: {tw.STOP if n else tw.TEXT_DIM};")

    def _usb_needs(self, page, devs):
        """[(name, attached now)] for the USB devices this page uses."""
        sdr = page.engine.sdr
        need = [(tw.RADIO_NAMES.get(sdr, sdr), bool(tw.USB_IDS.get(sdr, set()) & devs))]
        if page is self.tx and page._source_key() == "device":
            cam = page.camera.value() or ""
            if cam.startswith("/dev/"):
                need.append(("Camera", os.path.exists(cam)))
        return need

    def _usb_tick(self):
        devs = tw.usb_devices()
        if devs is None:
            return  # not Linux: nothing to watch
        if devs:
            self._usb_seen = True
        lost = self._usb_seen and not devs
        if lost != self._usb_lost:
            self._usb_lost = lost
            if lost:
                tw.notices().add("USB", "Every USB device disappeared at once: the USB system "
                                        "has failed -- reboot (check cables / power)")
            self._update_indicator()
        for page in (self.tx, self.rx):
            st = page._usb
            need = self._usb_needs(page, devs)
            attached = {name for name, ok in need if ok}
            side = "TX" if page is self.tx else "RX"
            if page.is_running():
                st["resume"] = st["back"] = None
                if st["watch"] is None:
                    st["watch"] = attached  # watch only what was there at start
                gone = sorted(st["watch"] - attached)
                if gone:
                    page.stop()
                    tw.notices().add("USB", f"{' and '.join(gone)} disconnected from USB while "
                                            f"{side} was running ({side} stopped; restarts when it is back)")
                    page.status.setText(f"{' and '.join(gone)} disconnected from USB -- "
                                        f"{side} restarts when {'it is' if len(gone) == 1 else 'they are'} back")
                    st["watch"], st["resume"] = None, set(gone)
            else:
                st["watch"] = None
                if st["resume"]:
                    if st["resume"] <= attached:
                        st["back"] = st["back"] or time.monotonic()
                        if time.monotonic() - st["back"] >= USB_RESUME_S:
                            st["resume"] = st["back"] = None
                            page.start()
                            if page.is_running():
                                st["watch"] = attached  # watched from the start
                    else:
                        st["back"] = None

    def _show_view(self, which):
        self.pages.setCurrentWidget(self.tx if which == "tx" else self.rx)
        self.view.setStyleSheet(tw.accent_stylesheet(tw.TX_ACCENT if which == "tx" else tw.RX_ACCENT))
        self.settings["view"] = which

    def _update_indicator(self):
        parts = []
        if getattr(self, "_usb_lost", False):
            parts.append(f"<span style='color:{tw.STOP}'>⚠ USB stopped responding: reboot "
                         f"(check cables / power)</span>")
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
    # Windows: a normal, resizable window (not full screen) that re-scales
    # to its size; 80% of the screen height to start, in the 7" panel's shape
    windowed = sys.platform == "win32" and not size
    if size:
        w, h = (int(v) for v in size.lower().split("x"))
    elif windowed:
        geo = app.primaryScreen().availableGeometry()
        h = round(geo.height() * 0.8)
        w = min(round(h * 800 / 480), geo.width())
    else:
        geo = app.primaryScreen().geometry()
        w, h = geo.width(), geo.height()
    scale = max(0.75, h / 480)
    win = TouchWindow(app, scale)
    win.scalable = windowed
    # SIGTERM / Ctrl-C: close normally, so TX/RX and their helper processes
    # are stopped -- killed outright, they used to carry on as orphans. Qt
    # only lets Python handle a signal between events, hence the timer.
    import signal
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: win.close())
    wake = QtCore.QTimer()
    wake.timeout.connect(lambda: None)
    wake.start(250)
    if size or windowed:
        win.resize(w, h)
        win.show()
    else:
        win.showFullScreen()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
