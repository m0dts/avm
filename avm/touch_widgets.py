"""
Theme and touch controls for the touch GUI (touch_gui.py): a dark grey
theme, and controls sized for a finger on a 7" 800x480 screen -- segmented
buttons, list pickers, a frequency keypad and a gain stepper. Everything
scales with the window height (scale 1.0 = 480 px), so the same layout
works on a 1080p monitor.
"""
import os
import re
import shutil
import subprocess
import threading
import time

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
CONFIG_ACCENT = "#8e7cc3"  # violet: the Config tab
# The name's letters, as a logo: A red, V green, M blue
LOGO_COLOURS = {"A": "#e53935", "V": "#43a047", "M": "#1e88e5"}


def logo_html(text, rest_colour=None):
    """Rich text with the name coloured as the logo: in "AVM v1.0.5" the
    A, V and M; in "AudioVideoModem" the capital A, V and M. Other text in
    rest_colour (or the label's own colour)."""
    def esc(c):
        return {"<": "&lt;", ">": "&gt;", "&": "&amp;"}.get(c, c)
    out, coloured = [], set()
    for c in text:
        if c in LOGO_COLOURS and c not in coloured:  # each letter once: the name, not later text
            coloured.add(c)
            out.append(f"<span style='color:{LOGO_COLOURS[c]}'>{c}</span>")
        else:
            out.append(f"<span style='color:{rest_colour}'>{esc(c)}</span>" if rest_colour else esc(c))
    return "".join(out)
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

    def showEvent(self, e):
        # the selected button's label is bold: room for that on every button,
        # so a short one ("Auto") isn't clipped when picked
        super().showEvent(e)
        for b in self._buttons.values():
            b.ensurePolished()
            f = QtGui.QFont(b.font())
            f.setBold(True)
            b.setMinimumWidth(QtGui.QFontMetrics(f).horizontalAdvance(b.text()) + round(20 * SCALE))

    def value(self):
        for v, b in self._buttons.items():
            if b.isChecked():
                return v
        return None

    def set_value(self, v):
        b = self._buttons.get(v)
        if b is not None:
            b.setChecked(True)


class Notices(QtCore.QObject):
    """Errors and warnings seen this session, for the title bar's warning
    button (touch_gui). A repeat of the newest entry from the same source
    just bumps its count, so a looping error can't flood the list."""
    changed = QtCore.pyqtSignal()
    MAX = 200

    def __init__(self):
        super().__init__()
        self.items = []  # [time, source, text, count], oldest first

    def add(self, source, text):
        text = " ".join(str(text).split())[:300]
        if not text:
            return
        now = time.strftime("%H:%M:%S")
        if self.items and self.items[-1][1] == source and self.items[-1][2] == text:
            self.items[-1][0] = now
            self.items[-1][3] += 1
        else:
            self.items.append([now, source, text, 1])
            del self.items[:-self.MAX]
        self.changed.emit()

    def clear(self):
        self.items = []
        self.changed.emit()

    def count(self):
        return sum(item[3] for item in self.items)


_notices = None


def notices():
    """The session's one Notices store."""
    global _notices
    if _notices is None:
        _notices = Notices()
    return _notices


# A process log line that reports a real problem: the final line of a Python
# traceback ("RuntimeError: ..."), an "Error"/"ERROR" message, or an "XX"
# installer-style failure. Not stats lines that merely count errors
# ("0 errors" -- plural never matches), nor ffmpeg's routine complaints when
# its output pipe closes as TX stops.
_ERROR_RE = re.compile(r"\b[A-Za-z_]*(Error|Exception)\b|\bERROR\b|^XX ")
_ERROR_IGNORE = ("Broken pipe", "Error muxing", "Error writing trailer", "Error closing file",
                 "error code: -32", "[video-stats]", "Traceback (most recent call last)",
                 # SoapySDR's Pluto driver looking for network Plutos while
                 # opening: harmless (no network Pluto, no avahi-daemon, Windows: no "local")
                 "Unable to scan", "Avahi DNS-SD client")


def error_line(line):
    """The message to report if this log line is an error, else None."""
    if not _ERROR_RE.search(line) or any(x in line for x in _ERROR_IGNORE):
        return None
    return line.strip()


class NoticesDialog(QtWidgets.QDialog):
    """Full-window list of the session's errors, newest first, with a button
    to clear them."""

    def __init__(self, parent):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel("Errors and warnings")
        t.setObjectName("big")
        top.addWidget(t, 1)
        clear = QtWidgets.QPushButton("Clear errors")
        clear.clicked.connect(self._clear)
        top.addWidget(clear)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        top.addWidget(close)
        lay.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setWordWrap(True)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        QtWidgets.QScroller.grabGesture(self.list.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        lay.addWidget(self.list, 1)
        self._fill()
        self.setGeometry(dialog_geometry(parent))

    def _fill(self):
        self.list.clear()
        items = notices().items
        if not items:
            self.list.addItem("No errors.")
            return
        for t, source, text, n in reversed(items):
            self.list.addItem(f"{t}  {source}: {text}" + (f"   ×{n}" if n > 1 else ""))

    def _clear(self):
        notices().clear()
        self._fill()


def dialog_geometry(widget):
    """Where a full-window dialog goes: over the page area of the main
    window (below its CONFIG / TX / RX tabs) if it has one, else the
    whole window."""
    win = widget.window()
    pages = getattr(win, "pages", None)
    if pages is not None and pages.isVisible():
        return QtCore.QRect(pages.mapToGlobal(QtCore.QPoint(0, 0)), pages.size())
    return win.geometry()


class TouchListDialog(QtWidgets.QDialog):
    """Full-window list to pick one item by tapping it. refresh: a callable
    returning a new list (e.g. a device search) -- adds a Refresh button.
    background=True: refresh runs on a worker thread (it must not touch Qt),
    so the window shows at once and the list fills in when it's done;
    items=None then starts with that search."""
    _found = QtCore.pyqtSignal(object, int)  # (items, search number)

    def __init__(self, title, items, current, parent, refresh=None, background=False):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        t = QtWidgets.QLabel(title)
        t.setObjectName("big")
        top.addWidget(t, 1)
        self._refresh = refresh
        self._background = background
        self._search = 0  # only the latest search's result is shown
        self._current = current
        self._found.connect(self._search_done)
        if refresh:
            self.refresh_button = QtWidgets.QPushButton("Refresh")
            self.refresh_button.clicked.connect(self._do_refresh)
            top.addWidget(self.refresh_button)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        top.addWidget(cancel)
        lay.addLayout(top)
        self.list = QtWidgets.QListWidget()
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        QtWidgets.QScroller.grabGesture(self.list.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        if items is None and refresh:
            QtCore.QTimer.singleShot(0, self._do_refresh)  # once the window is up
        else:
            self._fill(items or [])
        self.list.itemClicked.connect(lambda it: self.done_with(it.text()))
        lay.addWidget(self.list, 1)
        self.result_text = None
        self.setGeometry(dialog_geometry(parent))

    def _fill(self, items):
        self.list.clear()
        for item in items:
            self.list.addItem(item)
            if item == self._current:
                self.list.setCurrentRow(self.list.count() - 1)

    def _do_refresh(self):
        """Search again (a second or so)."""
        self.refresh_button.setEnabled(False)
        self.refresh_button.setText("Searching...")
        if not self._background:
            QtWidgets.QApplication.processEvents()  # show that first: this blocks
            try:
                self._fill(self._refresh())
            finally:
                self._search_done(None, -1)
            return
        if self.list.count() == 0:
            self.list.addItem("Searching...")
            self.list.item(0).setFlags(QtCore.Qt.NoItemFlags)
        self._search += 1
        n = self._search

        def work():
            try:
                items = self._refresh()
            except Exception as e:
                items = [f"(search failed: {e})"]
            try:
                self._found.emit(items, n)
            except RuntimeError:
                pass  # the window was closed meanwhile

        threading.Thread(target=work, daemon=True, name="picker-search").start()

    def _search_done(self, items, n):
        if items is not None:
            if n != self._search:
                return  # an older search
            self._fill(items)
        self.refresh_button.setText("Refresh")
        self.refresh_button.setEnabled(True)

    def done_with(self, text):
        self.result_text = text
        self.accept()


class Picker(QtWidgets.QPushButton):
    """A button showing the current choice; tapping opens a full-window
    list. items: list of strings, or a callable returning one (refreshed on
    every tap, e.g. device lists). Emits changed(text)."""
    changed = QtCore.pyqtSignal(str)
    search_in_background = False  # True: items() is slow and Qt-free (radio search)

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
        if callable(self._items):
            # a slow search runs after the window is up, not before
            items = None if self.search_in_background else self._items()
        else:
            items = self._items
        d = TouchListDialog(self._title, items, self._value, self,
                            refresh=self._items if callable(self._items) else None,
                            background=self.search_in_background)
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
        self.setGeometry(dialog_geometry(parent))

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
        self.setGeometry(dialog_geometry(parent))

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


# Tuning range per radio, MHz. Pluto: AD9363 70-6000 with the common
# firmware tweak (325-3800 stock). LimeSDR: LMS7002M via LimeSuite down to
# 0.1 MHz (below ~30 MHz by NCO offset; the Mini is specified from 10 MHz).
# RTL-SDR: R820T tuner range (no HF direct sampling here). Airspy R2/Mini:
# R820T2, 24-1800. Airspy HF+: 0.5 kHz-31 MHz and 60-260 MHz (gap between).
# SDRplay RSP: 1 kHz-2 GHz.
FREQ_RANGE_MHZ = {"pluto": (70.0, 6000.0), "lime": (0.1, 3800.0), "rtlsdr": (24.0, 1766.0),
                  "airspy": (24.0, 1800.0), "airspyhf": (0.01, 260.0),
                  "sdrplay": (0.001, 2000.0)}


class FreqButton(QtWidgets.QPushButton):
    """Shows a frequency in MHz; tapping opens the keypad. Value in Hz."""
    changed = QtCore.pyqtSignal(int)

    def __init__(self, title, hz, parent=None):
        super().__init__(parent)
        self._title = title
        self._range = FREQ_RANGE_MHZ["pluto"]
        self._radio = "PlutoSDR"
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.set_hz(hz)
        self.clicked.connect(self._edit)

    def set_radio(self, sdr):
        """Tuning range of this radio (FREQ_RANGE_MHZ) for keypad entries."""
        self._range = FREQ_RANGE_MHZ.get(sdr, FREQ_RANGE_MHZ["pluto"])
        self._radio = RADIO_NAMES.get(sdr, sdr)

    def in_range(self, hz=None):
        lo, hi = self._range
        return lo <= (self._hz if hz is None else hz) / 1e6 <= hi

    def hz(self):
        return self._hz

    def set_hz(self, hz):
        self._hz = int(hz)
        self.setText(f"{self._hz / 1e6:.4f} MHz")

    def _edit(self):
        d = KeypadDialog(self._title, f"{self._hz / 1e6:.4f}".rstrip("0").rstrip("."), self)
        if d.exec_():
            mhz = d.mhz()
            if mhz is None:
                return
            hz = int(round(mhz * 1e6))
            if not self.in_range(hz):
                lo, hi = self._range
                QtWidgets.QMessageBox.information(
                    self, "Frequency", f"{mhz:g} MHz is outside the {self._radio}'s range "
                                       f"({lo:g}-{hi:g} MHz).")
                return
            if hz != self._hz:
                self.set_hz(hz)
                self.changed.emit(hz)


class Stepper(QtWidgets.QWidget):
    """[-] value [+] with auto-repeat when held. Emits changed(value)."""
    changed = QtCore.pyqtSignal(float)

    signed = False  # show "+3" for positive values (offsets)

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
        self.label.setText(f"{v:+g}{self.suffix}" if self.signed and v else f"{v:g}{self.suffix}")
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


def spectrum_averages_file_path():
    """Where the touch GUI writes the live spectrum averaging (frames)."""
    return gain_file_path("rx_spectrum_averages")


def write_spectrum_averages_file(n):
    try:
        with open(spectrum_averages_file_path(), "w") as f:
            f.write(f"{int(n)}\n")
    except OSError:
        pass


def freq_offset_file_path():
    """Where the touch GUI writes the live RX frequency offset (Hz)."""
    return gain_file_path("rx_freq_offset")


def write_freq_offset_file(hz):
    try:
        with open(freq_offset_file_path(), "w") as f:
            f.write(f"{hz:.0f}\n")
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


_ffmpeg_encoders = None


FFMPEG_PATH = None  # the ffmpeg ffmpeg_has_encoder() asked (for messages)


def ffmpeg_has_encoder(name):
    """Whether the ffmpeg on PATH has this encoder (e.g. "libcodec2"),
    checked once. True if ffmpeg can't be asked: don't hide an option just
    because the check itself failed."""
    global _ffmpeg_encoders, FFMPEG_PATH
    if _ffmpeg_encoders is None:
        import shutil
        import subprocess
        try:
            import avm_threads
            FFMPEG_PATH = avm_threads.ffmpeg_exe()  # the installer's, else PATH's
        except ImportError:
            FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"
        try:
            out = subprocess.run([FFMPEG_PATH, "-hide_banner", "-encoders"], capture_output=True,
                                 text=True, errors="replace", timeout=10, stdin=subprocess.DEVNULL).stdout
            _ffmpeg_encoders = {line.split()[1] for line in out.splitlines() if len(line.split()) > 1}
        except (OSError, subprocess.TimeoutExpired):
            _ffmpeg_encoders = set()
    return not _ffmpeg_encoders or name in _ffmpeg_encoders


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


RADIO_NAMES = {"pluto": "PlutoSDR", "lime": "LimeSDR", "rtlsdr": "RTL-SDR",
               "airspy": "Airspy", "airspyhf": "Airspy HF+", "sdrplay": "SDRplay"}
# USB vendor:product IDs per radio, for the USB watchdog (touch_gui). A radio
# that isn't on USB at all (e.g. a Pluto on the network) simply isn't watched.
USB_IDS = {
    "pluto": {("0456", "b673")},                      # ADALM-Pluto
    "lime": {("1d50", "6108"), ("0403", "601f")},     # LimeSDR-USB; LimeSDR Mini (FTDI FT601)
    "rtlsdr": {("0bda", "2838"), ("0bda", "2832")},   # RTL2832U dongles
    "airspy": {("1d50", "60a1")},                     # Airspy R2 / Mini
    "airspyhf": {("03eb", "800c")},                   # Airspy HF+ Discovery / Dual
    # SDRplay RSP1, RSP1A, RSP2, RSPduo, RSPdx, RSP1B, RSPdx-R2
    "sdrplay": {("1df7", p) for p in ("2500", "3000", "3010", "3020", "3030", "3050", "3060")},
}


def usb_devices():
    """(vendor, product) IDs of every USB device attached now, root hubs
    excluded; None where that can't be read (not Linux). Reads sysfs only --
    microseconds, fine to poll."""
    base = "/sys/bus/usb/devices"
    if not os.path.isdir(base):
        return None
    out = set()
    for d in os.listdir(base):
        try:
            with open(os.path.join(base, d, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(base, d, "idProduct")) as f:
                pid = f.read().strip()
        except OSError:
            continue  # interfaces, or a device going away mid-read
        if vid != "1d6b":  # Linux Foundation: the root hubs themselves
            out.add((vid, pid))
    return out
# Network addresses a Pluto-firmware radio answers at out of the box:
# PlutoSDR's USB network link, and LibreSDR's Ethernet port.
PLUTO_DEFAULT_HOSTS = ("192.168.2.1", "192.168.1.10")
PLUTO_PROBE_CACHE_S = 20
_pluto_probe = {"t": -1e9, "host": None}


def iiod_answers(host, timeout=0.5):
    """Is an iiod (libiio server, port 30431) listening at host?"""
    import socket
    try:
        socket.create_connection((host, 30431), timeout=timeout).close()
        return True
    except OSError:
        return False


def default_pluto_uri():
    """'ip:<addr>' for the first default address that answers (PlutoSDR,
    then LibreSDR), or '' to let libiio search (USB). Cached briefly so
    applying settings doesn't keep probing."""
    now = time.monotonic()
    if now - _pluto_probe["t"] > PLUTO_PROBE_CACHE_S:
        _pluto_probe["host"] = next((h for h in PLUTO_DEFAULT_HOSTS if iiod_answers(h)), None)
        _pluto_probe["t"] = now
    return f"ip:{_pluto_probe['host']}" if _pluto_probe["host"] else ""


RX_ONLY_RADIOS = ("rtlsdr", "airspy", "airspyhf", "sdrplay")
# radios whose live gain goes via a file the RX process watches (no iio_attr)
GAIN_FILE_RADIOS = ("lime", "rtlsdr", "airspy", "airspyhf", "sdrplay")
RTL_SAMPLE_RATE = 1024000  # an RTL-SDR rate (0.9-3.2 MS/s) that fits every bandwidth + LO offset


def radio_present(sdr, uri=None):
    """Is the selected radio there? Checked before TX/RX starts, so a
    missing one is reported plainly instead of as a driver traceback.
    Returns (True/False, how it was found / what to check).
    Linux: the USB device list (microseconds); a Pluto not on USB is tried
    at its network address (iiod, port 30431, 0.5 s). Elsewhere: SoapySDR
    asked for that driver (~1 s). If the check itself can't run, True --
    never block a start just because the check failed."""
    import socket
    name = RADIO_NAMES.get(sdr, sdr)
    devs = usb_devices()
    if devs is not None:
        if USB_IDS.get(sdr, set()) & devs:
            return True, f"{name} on USB"
        if sdr == "pluto":
            hosts = list(PLUTO_DEFAULT_HOSTS)
            if uri and uri.startswith("ip:"):
                hosts.insert(0, uri.split(":", 1)[1])
            for host in dict.fromkeys(hosts):
                if iiod_answers(host):
                    return True, f"{name} at {host}"
        return False, f"{name} not found -- check it's plugged in (USB) and powered"
    try:
        import SoapySDR
        SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_ERROR)
        driver = {"pluto": "plutosdr"}.get(sdr, sdr)
        if SoapySDR.Device.enumerate(f"driver={driver}"):
            return True, f"{name} found"
        return False, f"{name} not found -- check it's plugged in (USB) and powered"
    except Exception:
        return True, "not checked"


def detect_rx_radios():
    """detect_radios() plus receive-only radios (RTL-SDR, Airspy, SDRplay)."""
    found = [r for r in detect_radios() if not r.startswith("(no radios")]
    try:
        import SoapySDR
        for kw in SoapySDR.Device.enumerate("driver=rtlsdr"):
            kw = dict(kw)
            found.append(f"RTL-SDR {kw.get('serial', '').lstrip('0')[-8:]}".strip())
    except Exception as e:
        found.append(f"(RTL-SDR search failed: {e})")
    # Airspy R2/Mini and HF+ (SoapyAirspy / SoapyAirspyHF modules)
    for driver, name in (("airspy", "Airspy"), ("airspyhf", "Airspy HF+")):
        try:
            import SoapySDR
            for kw in SoapySDR.Device.enumerate(f"driver={driver}"):
                kw = dict(kw)
                found.append(f"{name} {kw.get('serial', '').lstrip('0')[-8:]}".strip())
        except Exception:
            pass  # module not installed
    # SDRplay RSPs (SoapySDRPlay3, needs SDRplay's API service running)
    try:
        import SoapySDR
        for kw in SoapySDR.Device.enumerate("driver=sdrplay"):
            kw = dict(kw)
            # label e.g. "SDRplay Dev0 RSP1A 2105090A1B"
            model = next((w for w in kw.get("label", "").split() if w.startswith("RSP")), "RSP")
            found.append(f"SDRplay {model} {kw.get('serial', '')[-6:]}".strip())
    except Exception:
        pass
    return found or ["(no radios found -- check USB / power)"]


# "PlutoSDR (USB)" / "PlutoSDR (IP 192.168.2.1)" -> the SoapySDR uri it was
# found at (filled in by detect_radios)
PLUTO_URIS = {}


def pluto_label(uri):
    """List entry for a Pluto found at this uri ("usb:1.6.5", "ip:...")."""
    if uri.startswith("usb:"):
        return "PlutoSDR (USB)"
    if uri.startswith("ip:"):
        return f"PlutoSDR (IP {uri[3:]})"
    return "PlutoSDR"


def detect_radios():
    """The SDRs connected right now, for the Radio pickers: 'PlutoSDR' and/or
    one entry per LimeSDR (its model, e.g. LimeSDR-USB or LimeSDR Mini, and
    serial). Takes ~1.7 s on a Pi 4 (mostly looking for Plutos on USB and
    the network)."""
    gui_thread = threading.current_thread() is threading.main_thread()
    if gui_thread:
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    found = []
    try:
        import SoapySDR
        # errors only: the Pluto driver warns about every way it looks that
        # doesn't apply here ('Unable to scan "ip"', 'local: -19' on Windows)
        SoapySDR.setLogLevel(SoapySDR.SOAPY_SDR_ERROR)
        # one entry per way a Pluto was found: a USB Pluto usually shows
        # twice, as its cable also carries a network link (192.168.2.1)
        for kw in SoapySDR.Device.enumerate("driver=plutosdr"):
            label = pluto_label(dict(kw).get("uri", ""))
            if label not in found:
                found.append(label)
                PLUTO_URIS[label] = dict(kw).get("uri", "")
        # libiio's own search doesn't look on Ethernet (LibreSDR 192.168.1.10)
        for host in PLUTO_DEFAULT_HOSTS:
            label = pluto_label(f"ip:{host}")
            if label not in found and iiod_answers(host, 0.3):
                found.append(label)
                PLUTO_URIS[label] = f"ip:{host}"
        # one entry per Pluto: its numeric address is the clearest. A USB
        # Pluto's own network link is 192.168.2.1, so with that listed the
        # USB entry is the same radio; a name (pluto.local) is too. A USB
        # entry stays when only another address answers (a separate radio).
        numeric = [l for l in found if re.fullmatch(r"PlutoSDR \(IP [\d.]+\)", l)]
        if numeric:
            found = [l for l in found if not (
                (l.startswith("PlutoSDR (IP ") and l not in numeric)
                or (l == "PlutoSDR (USB)" and pluto_label("ip:192.168.2.1") in numeric))]
        for kw in SoapySDR.Device.enumerate("driver=lime"):
            kw = dict(kw)
            name = kw.get("name") or kw.get("label", "LimeSDR").split(" [")[0]
            serial = kw.get("serial", "").lstrip("0")
            found.append(f"{name} {serial[-6:]}".strip() if name.startswith("Lime") else f"LimeSDR {name}")
    except Exception as e:
        found.append(f"(search failed: {e})")
    finally:
        if gui_thread:
            QtWidgets.QApplication.restoreOverrideCursor()
    return found or ["(no radios found -- check USB / power)"]


class RadioPicker(Picker):
    """Picker for the SDR one direction uses; tapping lists the radios
    connected now. Emits radio_changed('pluto' | 'lime' | 'rtlsdr'); a '(no
    radios found)' pick keeps the previous choice. rx=True also offers
    receive-only radios (RTL-SDR)."""
    radio_changed = QtCore.pyqtSignal(str)
    search_in_background = True  # the radio search takes ~1.7 s on a Pi 4

    SHORT = {"pluto": "Pluto", "lime": "Lime", "rtlsdr": "RTL", "airspy": "Airspy", "airspyhf": "HF+",
             "sdrplay": "SDRplay"}  # fits beside the frequency

    def __init__(self, title, sdr, rx=False, pluto_uri=""):
        allowed = RADIO_NAMES if rx else {k: v for k, v in RADIO_NAMES.items() if k not in RX_ONLY_RADIOS}
        sdr = sdr if sdr in allowed else "pluto"
        self.pluto_uri = pluto_uri or ""  # how the Pluto is reached ("" = default)
        super().__init__(title, detect_rx_radios if rx else detect_radios, self.SHORT[sdr])
        self.sdr = sdr
        self.set_sdr(sdr)
        self.changed.connect(self._picked)

    def _short(self, sdr):
        if sdr == "pluto" and self.pluto_uri.startswith("usb:"):
            return "Pluto USB"
        if sdr == "pluto" and self.pluto_uri.startswith("ip:"):
            return "Pluto IP"
        return self.SHORT[sdr]

    def set_sdr(self, sdr):
        self.sdr = sdr
        self.set_value(self._short(sdr))

    def _picked(self, label):
        sdr = ("lime" if label.startswith("Lime") else "pluto" if label.startswith("Pluto")
               else "rtlsdr" if label.startswith("RTL-SDR")
               else "airspyhf" if label.startswith("Airspy HF+")
               else "airspy" if label.startswith("Airspy")
               else "sdrplay" if label.startswith("SDRplay") else None)
        if sdr == "pluto":
            self.pluto_uri = PLUTO_URIS.get(label, "")
        self.set_sdr(sdr or self.sdr)  # "(no radios found)" keeps the previous choice
        if sdr:
            self.radio_changed.emit(sdr)


def freq_radio_row(freq, radio, port=None):
    """Freq button and radio picker sharing one row (plus the LimeSDR port
    picker, shown only while the LimeSDR is the radio)."""
    w = QtWidgets.QWidget()
    w.setObjectName("seg")
    lay = QtWidgets.QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(freq, 3)
    lay.addWidget(radio, 2)
    if port is not None:
        lay.addWidget(port, 2)
    return w


# LimeSDR antenna ports by LimeSuite name. "Auto" picks by frequency (see
# pluto_soapy_sink._lime_configure); a port the board lacks (e.g. LNAL on a
# LimeSDR Mini) falls back to Auto at start.
LIME_PORTS = {"tx": ["Auto", "BAND1", "BAND2"], "rx": ["Auto", "LNAL", "LNAW", "LNAH"]}


def lime_port_picker(kind, current):
    """Picker for the LimeSDR's TX or RX antenna port ("tx" / "rx")."""
    items = LIME_PORTS[kind]
    return Picker(f"LimeSDR {kind.upper()} port", items, current if current in items else "Auto")


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
