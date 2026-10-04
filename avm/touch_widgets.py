"""
Theme and touch controls for the touch GUI (touch_gui.py): a dark grey
theme, and controls sized for a finger on a 7" 800x480 screen -- segmented
buttons, list pickers, a frequency keypad and a gain stepper. Everything
scales with the window height (scale 1.0 = 480 px), so the same layout
works on a 1080p monitor.
"""
import shutil
import subprocess
import threading

from PyQt5 import QtCore, QtGui, QtWidgets

SCALE = 1.0  # set by touch_gui.py before building pages (1.0 = 480 px tall)

# Colours
BG = "#25272b"           # window
PANEL = "#2f3237"        # grouped areas
BUTTON = "#3c4046"
BUTTON_PRESSED = "#50555c"
TEXT = "#e6e8eb"
TEXT_DIM = "#9aa0a8"
TX_ACCENT = "#e07a2e"    # orange
RX_ACCENT = "#2ea8b0"    # teal
GO = "#2f9e5a"
STOP = "#c0392b"
WARN = "#e0b030"


def stylesheet(scale, accent):
    """The whole app's stylesheet at a given scale (1.0 = 800x480)."""
    f = lambda px: max(1, round(px * scale))
    return f"""
    QWidget {{ background: {BG}; color: {TEXT}; font-size: {f(15)}px; }}
    QLabel#dim {{ color: {TEXT_DIM}; font-size: {f(13)}px; }}
    QLabel#rowlabel {{ color: {TEXT_DIM}; font-size: {f(14)}px; }}
    QLabel#value {{ font-size: {f(16)}px; }}
    QLabel#big {{ font-size: {f(20)}px; font-weight: bold; }}
    QLabel#warn {{ color: {WARN}; font-size: {f(13)}px; }}
    QFrame#panel {{ background: {PANEL}; border-radius: {f(8)}px; }}
    QFrame#panel QLabel, QFrame#panel QWidget#seg {{ background: transparent; }}
    QPushButton {{
        background: {BUTTON}; color: {TEXT}; border: none; border-radius: {f(6)}px;
        min-height: {f(37)}px; padding: 0 {f(8)}px; font-size: {f(15)}px;
    }}
    QPushButton:pressed {{ background: {BUTTON_PRESSED}; }}
    QPushButton:checked {{ background: {accent}; color: #111; font-weight: bold; }}
    QPushButton:disabled {{ color: #666; }}
    QPushButton#run {{ font-size: {f(20)}px; font-weight: bold; min-height: {f(56)}px; }}
    QPushButton#toggle {{ font-size: {f(18)}px; font-weight: bold; min-width: {f(90)}px; }}
    QListWidget {{ background: {PANEL}; border: none; font-size: {f(18)}px; }}
    QListWidget::item {{ min-height: {f(52)}px; padding-left: {f(12)}px; border-bottom: 1px solid {BG}; }}
    QListWidget::item:selected {{ background: {accent}; color: #111; }}
    QScrollBar:vertical {{ width: {f(18)}px; background: {PANEL}; }}
    QScrollBar::handle:vertical {{ background: {BUTTON_PRESSED}; border-radius: {f(6)}px; min-height: {f(40)}px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    """


def accent_stylesheet(accent):
    """Just the accent-coloured rules, for a page or widget: cheap to set,
    unlike re-applying stylesheet() to the whole application."""
    return (f"QPushButton:checked {{ background: {accent}; color: #111; font-weight: bold; }}"
            f"QListWidget::item:selected {{ background: {accent}; color: #111; }}")


class Segmented(QtWidgets.QWidget):
    """A row of exclusive buttons -- one tap selects. options: list of
    (value, label). Emits changed(value)."""
    changed = QtCore.pyqtSignal(object)

    def __init__(self, options, value=None, parent=None):
        super().__init__(parent)
        self.setObjectName("seg")
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons = {}
        for v, label in options:
            b = QtWidgets.QPushButton(label)
            b.setCheckable(True)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            lay.addWidget(b)
            self._group.addButton(b)
            self._buttons[v] = b
            b.clicked.connect(lambda _=False, v=v: self.changed.emit(v))
        if value is not None:
            self.set_value(value)

    def value(self):
        for v, b in self._buttons.items():
            if b.isChecked():
                return v
        return None

    def set_value(self, v):
        b = self._buttons.get(v)
        if b is not None:
            b.setChecked(True)


class TouchListDialog(QtWidgets.QDialog):
    """Full-window list to pick one item by tapping it."""

    def __init__(self, title, items, current, parent):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel(title)
        t.setObjectName("big")
        top.addWidget(t, 1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        top.addWidget(cancel)
        lay.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        QtWidgets.QScroller.grabGesture(self.list.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        for item in items:
            self.list.addItem(item)
            if item == current:
                self.list.setCurrentRow(self.list.count() - 1)
        self.list.itemClicked.connect(lambda it: self.done_with(it.text()))
        lay.addWidget(self.list, 1)
        self.result_text = None
        self.setGeometry(parent.window().geometry())

    def done_with(self, text):
        self.result_text = text
        self.accept()


class Picker(QtWidgets.QPushButton):
    """A button showing the current choice; tapping opens a full-window
    list. items: list of strings, or a callable returning one (refreshed on
    every tap, e.g. device lists). Emits changed(text)."""
    changed = QtCore.pyqtSignal(str)

    def __init__(self, title, items, current="", parent=None):
        super().__init__(parent)
        self._title = title
        self._items = items
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        # long device names are elided, never allowed to widen the layout
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.set_value(current)
        self.clicked.connect(self._pick)

    def value(self):
        return self._value

    def set_value(self, text):
        self._value = text
        self._show_text()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._show_text()

    def _show_text(self):
        full = f"{self._value or '(none)'}"
        room = max(20, self.width() - self.fontMetrics().horizontalAdvance("  ▾") - 24)
        self.setText(self.fontMetrics().elidedText(full, QtCore.Qt.ElideMiddle, room) + "  ▾")

    def _pick(self):
        items = self._items() if callable(self._items) else self._items
        d = TouchListDialog(self._title, items, self._value, self)
        if d.exec_() and d.result_text is not None and d.result_text != self._value:
            self.set_value(d.result_text)
            self.changed.emit(d.result_text)


class KeypadDialog(QtWidgets.QDialog):
    """Full-window numeric keypad for a frequency in MHz."""

    def __init__(self, title, mhz_text, parent):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QLabel(title)
        t.setObjectName("big")
        lay.addWidget(t)
        self.display = QtWidgets.QLabel(mhz_text)
        self.display.setObjectName("big")
        self.display.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        lay.addWidget(self.display)
        self._fresh = True  # first key press replaces the old value
        grid = QtWidgets.QGridLayout()
        keys = ["7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", "⌫"]
        for i, k in enumerate(keys):
            b = QtWidgets.QPushButton(k)
            b.setObjectName("run")
            b.clicked.connect(lambda _=False, k=k: self._key(k))
            grid.addWidget(b, i // 3, i % 3)
        lay.addLayout(grid, 1)
        row = QtWidgets.QHBoxLayout()
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.setObjectName("run")
        cancel.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton("OK  (MHz)")
        ok.setObjectName("run")
        ok.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(ok)
        lay.addLayout(row)
        self.setGeometry(parent.window().geometry())

    def _key(self, k):
        s = "" if self._fresh else self.display.text()
        self._fresh = False
        if k == "⌫":
            s = s[:-1]
        elif k == "." and "." in s:
            return
        elif len(s) < 11:
            s += k
        self.display.setText(s)

    def mhz(self):
        try:
            return float(self.display.text())
        except ValueError:
            return None


class KeyboardDialog(QtWidgets.QDialog):
    """Full-window on-screen keyboard for short uppercase text (callsign /
    station ID): digits, A-Z, / - . and space."""
    ROWS = ["1234567890", "QWERTYUIOP", "ASDFGHJKL/", "ZXCVBNM-. "]

    def __init__(self, title, text, max_len, parent):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        self._max = max_len
        lay = QtWidgets.QVBoxLayout(self)
        lay.setSpacing(4)
        t = QtWidgets.QLabel(f"{title}  (up to {max_len} characters)")
        t.setObjectName("dim")
        lay.addWidget(t)
        self.display = QtWidgets.QLabel()
        self.display.setObjectName("big")
        self.display.setStyleSheet(f"background: {PANEL}; padding: 4px 8px; font-family: monospace;")
        lay.addWidget(self.display)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)
        for r, keys in enumerate(self.ROWS):
            for c, k in enumerate(keys):
                b = QtWidgets.QPushButton("space" if k == " " else k)
                b.setFocusPolicy(QtCore.Qt.NoFocus)
                b.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
                b.clicked.connect(lambda _=False, k=k: self._key(k))
                grid.addWidget(b, r, c)
        lay.addLayout(grid, 1)
        row = QtWidgets.QHBoxLayout()
        for label, slot in (("⌫", lambda: self._set(self._text[:-1])), ("Clear", lambda: self._set("")),
                            ("Cancel", self.reject), ("OK", self.accept)):
            b = QtWidgets.QPushButton(label)
            b.setObjectName("run")
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.clicked.connect(slot)
            row.addWidget(b)
        lay.addLayout(row)
        self._set(text)
        self.setGeometry(parent.window().geometry())

    def _set(self, text):
        self._text = text[:self._max]
        self.display.setText(self._text + "▏")

    def _key(self, k):
        if len(self._text) < self._max:
            self._set(self._text + k)

    def text(self):
        return self._text.strip()


class TextButton(QtWidgets.QPushButton):
    """Shows short text; tapping opens the on-screen keyboard. Emits changed(text)."""
    changed = QtCore.pyqtSignal(str)

    def __init__(self, title, text="", max_len=20, placeholder="(none)", parent=None):
        super().__init__(parent)
        self._title, self._max, self._placeholder = title, max_len, placeholder
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.set_text(text)
        self.clicked.connect(self._edit)

    def value(self):
        return self._value

    def set_text(self, text):
        self._value = (text or "").upper()[:self._max]
        self.setText(self._value or self._placeholder)

    def _edit(self):
        d = KeyboardDialog(self._title, self._value, self._max, self)
        if d.exec_() and d.text() != self._value:
            self.set_text(d.text())
            self.changed.emit(self._value)


class FreqButton(QtWidgets.QPushButton):
    """Shows a frequency in MHz; tapping opens the keypad. Value in Hz."""
    changed = QtCore.pyqtSignal(int)

    def __init__(self, title, hz, parent=None):
        super().__init__(parent)
        self._title = title
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.set_hz(hz)
        self.clicked.connect(self._edit)

    def hz(self):
        return self._hz

    def set_hz(self, hz):
        self._hz = int(hz)
        self.setText(f"{self._hz / 1e6:.4f} MHz")

    def _edit(self):
        d = KeypadDialog(self._title, f"{self._hz / 1e6:.4f}".rstrip("0").rstrip("."), self)
        if d.exec_():
            mhz = d.mhz()
            # the Pluto (AD9363) tunes 325-3800 MHz (70-6000 with the common firmware tweak)
            if mhz is not None and 70.0 <= mhz <= 6000.0:
                hz = int(round(mhz * 1e6))
                if hz != self._hz:
                    self.set_hz(hz)
                    self.changed.emit(hz)


class Stepper(QtWidgets.QWidget):
    """[-] value [+] with auto-repeat when held. Emits changed(value)."""
    changed = QtCore.pyqtSignal(float)

    def __init__(self, lo, hi, step, value, suffix="", parent=None):
        super().__init__(parent)
        self.setObjectName("seg")
        self.lo, self.hi, self.step, self.suffix = lo, hi, step, suffix
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.minus = QtWidgets.QPushButton("−")
        self.plus = QtWidgets.QPushButton("+")
        self.label = QtWidgets.QLabel()
        self.label.setObjectName("value")
        self.label.setAlignment(QtCore.Qt.AlignCenter)
        for b, d in ((self.minus, -1), (self.plus, 1)):
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(80)
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.clicked.connect(lambda _=False, d=d: self.set_value(self._value + d * self.step, emit=True))
        lay.addWidget(self.minus, 1)
        lay.addWidget(self.label, 2)
        lay.addWidget(self.plus, 1)
        self._value = None
        self.set_value(value)

    def value(self):
        return self._value

    def set_value(self, v, emit=False):
        v = min(self.hi, max(self.lo, round(v / self.step) * self.step))
        if v == self._value:
            return
        self._value = v
        self.label.setText(f"{v:g}{self.suffix}")
        if emit:
            self.changed.emit(v)


class RunButton(QtWidgets.QPushButton):
    """Big START / STOP button. States: 'stopped', 'running', 'pending'
    (running with changed settings -> tap restarts to apply)."""

    def __init__(self, what, parent=None):
        super().__init__(parent)
        self.setObjectName("run")
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self._what = what
        self.set_state("stopped")

    def set_state(self, state):
        self.state = state
        text, colour = {"stopped": (f"START {self._what}", GO),
                        "running": (f"STOP {self._what}", STOP),
                        "pending": ("RESTART TO APPLY", WARN)}[state]
        self.setText(text)
        self.setStyleSheet(f"QPushButton#run {{ background: {colour}; color: #111; }}")


def gain_file_path(direction):
    """Where the touch GUI writes live gain for a LimeSDR (see
    pluto_soapy_sink.GainFileWatcher)."""
    import os
    import tempfile
    base = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    return os.path.join(base, f"hfmodem_{direction}_gain")


def write_gain_file(direction, db):
    try:
        with open(gain_file_path(direction), "w") as f:
            f.write(f"{db:.2f}\n")
    except OSError:
        pass


BANDWIDTHS_KHZ = ("20", "40", "80", "160", "250")
MODES = ("A", "B", "C", "D", "VU")  # VU: VHF/UHF (144/432 MHz) mobile + troposcatter, not DRM


def saved_mode(value):
    """A saved mode from an older option set (V was renamed VU)."""
    value = {"V": "VU"}.get(str(value), str(value))
    return value if value in MODES else "A"


def lo_offset_hz(bw_khz):
    """LO offset for a signal bw_khz wide -- TX and RX both use this, so
    they always match. The SDR's DC spike sits this far below the signal
    centre; it must clear the RX spectrum view (1.5x the bandwidth wide,
    so offset > 0.75 x bandwidth) and the signal must stay inside the SDR
    rate's +-fs/2:
      <= 80 kHz: 100 kHz at 550 kS/s (as always)
      160 kHz:   130 kHz at 550 kS/s -- signal 50-210 kHz (13/55: 55-sample LO table)
      250 kHz:   190 kHz at 800 kS/s -- signal 65-315 kHz (19/80)"""
    bw = float(bw_khz)
    if bw > 200:
        return 190000.0
    if bw > 100:
        return 130000.0
    return 100000.0


def sdr_rate(bw_khz):
    """SDR sample rate for a signal bw_khz wide (see lo_offset_hz): 250 kHz
    plus its offset doesn't fit in 550 kS/s. A higher rate is more
    front-end work on both sides, so only as much as needed."""
    return 800000 if float(bw_khz) > 200 else 550000


def fragment_size(bw_khz):
    """Bytes per fragment (TX and RX must match). 250 kHz: 2048 -- each
    fragment's fixed decode work (sync, header, channel estimate) then
    covers twice the data; at 1024 a wide-mode fragment lasts only ~0.1 s
    and the Pi 4's decoder ran at ~90% of real time (measured at 320 kHz).
    Costs about a fragment-time more latency."""
    return 2048 if float(bw_khz) > 200 else 1024


def enforce_mode_bandwidth(mode_seg, bw_seg):
    """Bandwidths a mode can't use are greyed out (and left if selected):
    mode VU needs at least its min_occupancy_khz (see hf_ofdm_common)."""
    import hf_ofdm_common as ofdm
    min_khz = ofdm.DRM_MODES.get(mode_seg.value(), {}).get("min_occupancy_khz", 0)
    for value, button in bw_seg._buttons.items():
        button.setEnabled(float(value) >= min_khz)
    if bw_seg.value() is not None and float(bw_seg.value()) < min_khz:
        bw_seg.set_value(next(v for v in BANDWIDTHS_KHZ if float(v) >= min_khz))


def saved_bandwidth(value):
    """A saved bandwidth from an older option set (150 -> 160, 320 -> 250)."""
    value = {"150": "160", "320": "250"}.get(str(value), str(value))
    return value if value in BANDWIDTHS_KHZ else "80"


RADIO_NAMES = {"pluto": "PlutoSDR", "lime": "LimeSDR", "rtlsdr": "RTL-SDR"}
RX_ONLY_RADIOS = ("rtlsdr",)
RTL_SAMPLE_RATE = 1024000  # an RTL-SDR rate (0.9-3.2 MS/s) that fits every bandwidth + LO offset


def detect_rx_radios():
    """detect_radios() plus RTL-SDR dongles (receive only)."""
    found = [r for r in detect_radios() if not r.startswith("(no radios")]
    try:
        import SoapySDR
        for kw in SoapySDR.Device.enumerate("driver=rtlsdr"):
            kw = dict(kw)
            found.append(f"RTL-SDR {kw.get('serial', '').lstrip('0')[-8:]}".strip())
    except Exception as e:
        found.append(f"(RTL-SDR search failed: {e})")
    return found or ["(no radios found -- check USB / power)"]


def detect_radios():
    """The SDRs connected right now, for the Radio pickers: 'PlutoSDR' and/or
    one entry per LimeSDR (its model, e.g. LimeSDR-USB or LimeSDR Mini, and
    serial). Takes ~1.7 s on a Pi 4 (mostly looking for Plutos on USB and
    the network)."""
    QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    found = []
    try:
        import SoapySDR
        if SoapySDR.Device.enumerate("driver=plutosdr"):
            found.append("PlutoSDR")
        for kw in SoapySDR.Device.enumerate("driver=lime"):
            kw = dict(kw)
            name = kw.get("name") or kw.get("label", "LimeSDR").split(" [")[0]
            serial = kw.get("serial", "").lstrip("0")
            found.append(f"{name} {serial[-6:]}".strip() if name.startswith("Lime") else f"LimeSDR {name}")
    except Exception as e:
        found.append(f"(search failed: {e})")
    finally:
        QtWidgets.QApplication.restoreOverrideCursor()
    return found or ["(no radios found -- check USB / power)"]


class RadioPicker(Picker):
    """Picker for the SDR one direction uses; tapping lists the radios
    connected now. Emits radio_changed('pluto' | 'lime' | 'rtlsdr'); a '(no
    radios found)' pick keeps the previous choice. rx=True also offers
    receive-only radios (RTL-SDR)."""
    radio_changed = QtCore.pyqtSignal(str)

    SHORT = {"pluto": "Pluto", "lime": "Lime", "rtlsdr": "RTL"}  # fits beside the frequency

    def __init__(self, title, sdr, rx=False):
        allowed = RADIO_NAMES if rx else {k: v for k, v in RADIO_NAMES.items() if k not in RX_ONLY_RADIOS}
        sdr = sdr if sdr in allowed else "pluto"
        super().__init__(title, detect_rx_radios if rx else detect_radios, self.SHORT[sdr])
        self.sdr = sdr
        self.changed.connect(self._picked)

    def set_sdr(self, sdr):
        self.sdr = sdr
        self.set_value(self.SHORT[sdr])

    def _picked(self, label):
        sdr = ("lime" if label.startswith("Lime") else "pluto" if label.startswith("Pluto")
               else "rtlsdr" if label.startswith("RTL-SDR") else None)
        self.set_sdr(sdr or self.sdr)  # "(no radios found)" keeps the previous choice
        if sdr:
            self.radio_changed.emit(sdr)


def freq_radio_row(freq, radio):
    """Freq button and radio picker sharing one row."""
    w = QtWidgets.QWidget()
    w.setObjectName("seg")
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(freq, 3)
    lay.addWidget(radio, 2)
    return w


class LiveGain:
    """Sets the PlutoSDR's TX or RX gain while a chain is running, through
    libiio's iio_attr talking to the Pluto's own iiod server -- the running
    hf_ofdm_tx/rx processes share the device, so no restart is needed. The
    units already match the GUI: TX 'hardwaregain' is attenuation in dB re
    full power (-89..0, what --tx-gain means), RX is the manual gain in dB
    (0..73; the RX process puts the AGC in manual mode at start). Requests
    go out on a background thread, newest value wins, at most ~7 per
    second, so holding the stepper never stalls the UI.
    available() is False where iio_attr isn't installed (e.g. Windows):
    callers then fall back to restart-to-apply."""

    def __init__(self, direction):
        self.flag = "-o" if direction == "tx" else "-i"
        self._pending = None
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @staticmethod
    def available():
        return shutil.which("iio_attr") is not None

    def set(self, uri, db):
        with self._lock:
            self._pending = (uri, db)
        self._event.set()

    def _run(self):
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                job, self._pending = self._pending, None
            if job is None:
                continue
            uri, db = job
            try:
                subprocess.run(["iio_attr", "-u", uri, self.flag, "-c", "ad9361-phy", "voltage0",
                                "hardwaregain", f"{db:.2f}"], capture_output=True, timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass
            threading.Event().wait(0.15)


class ElideLabel(QtWidgets.QLabel):
    """One-line label that never widens the layout: text that doesn't fit is
    cut short with '...' (the full text is in the tooltip). For status and
    error lines, whose length varies -- a long error message used to push
    the window wider than the screen."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setWordWrap(False)
        self._full = ""
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setToolTip(self._full if len(self._full) > 40 else "")
        self._elide()

    def text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def minimumSizeHint(self):
        return QtCore.QSize(0, super().minimumSizeHint().height())

    def _elide(self):
        line = self._full.replace("\n", "  ·  ")
        super().setText(self.fontMetrics().elidedText(line, QtCore.Qt.ElideRight, max(10, self.width())))


def panel():
    f = QtWidgets.QFrame()
    f.setObjectName("panel")
    return f


def row_label(text):
    lab = QtWidgets.QLabel(text)
    lab.setObjectName("rowlabel")
    return lab


class AspectLabel(QtWidgets.QLabel):
    """Shows a picture scaled to fit while keeping its proportions, on a
    16:9 black background."""

    def __init__(self, placeholder=""):
        super().__init__(placeholder)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.setStyleSheet("background: #000; color: #777;")
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        self.setMinimumSize(160, 90)
        self._pix = None

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return w * 9 // 16

    def set_image(self, qimage):
        self._pix = QtGui.QPixmap.fromImage(qimage)
        self._show()

    def clear_image(self, text):
        self._pix = None
        self.clear()
        self.setText(text)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._show()

    def _show(self):
        if self._pix is not None:
            self.setPixmap(self._pix.scaled(self.size(), QtCore.Qt.KeepAspectRatio,
                                            QtCore.Qt.SmoothTransformation))
