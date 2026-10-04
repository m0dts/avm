"""
Screen-fitting helpers shared by media_tx_gui.py and media_rx_gui.py.

On a small screen (e.g. a Raspberry Pi on a 1920x1080 TV) the two GUIs are
meant to sit side by side, TX in the left quarter and RX in the rest, so:
  - compact mode shrinks the font and the padding of every input widget,
  - place() sizes and positions a window to fill its strip of the screen.

Compact mode switches on automatically when the screen is 1300 px tall or
less; HF_GUI_COMPACT=1 / HF_GUI_COMPACT=0 forces it on or off.
"""
import os

from PyQt5 import QtCore, QtWidgets

COMPACT_MAX_SCREEN_HEIGHT = 1300
COMPACT_FONT_SCALE = 0.8
COMPACT_BASE_FONT_PT = 9.0
TX_WIDTH_FRACTION = 0.25  # TX gets the left quarter, RX the rest

_compact = None


def _available_geometry():
    return QtWidgets.QApplication.primaryScreen().availableGeometry()


def is_compact():
    """Whether compact mode is on. Call after the QApplication exists."""
    global _compact
    if _compact is None:
        forced = os.environ.get("HF_GUI_COMPACT")
        if forced in ("0", "1"):
            _compact = forced == "1"
        else:
            _compact = _available_geometry().height() <= COMPACT_MAX_SCREEN_HEIGHT
    return _compact


def prepare_environment():
    """Call before creating the QApplication. Drops the qt5ct platform
    theme the Raspberry Pi desktop sets: it re-applies the desktop's 12 pt
    font (and gtk2 style) once the event loop starts, overriding
    apply_compact_style, so the GUIs overflowed a 1080p screen when started
    from the desktop although they fitted when started from a shell.
    HF_GUI_COMPACT=0 keeps it."""
    if os.environ.get("HF_GUI_COMPACT") != "0" and os.environ.get("QT_QPA_PLATFORMTHEME") == "qt5ct":
        del os.environ["QT_QPA_PLATFORMTHEME"]
    if os.environ.get("HF_GUI_WAYLAND") == "1" and os.environ.get("WAYLAND_DISPLAY"):
        # Native Wayland client instead of X11 through Xwayland, which
        # copies every repaint (~17-23% of a Raspberry Pi 4 core with the RX
        # GUI's video + spectrum). Wayland doesn't let a client place its
        # own window, so place() does nothing then -- the compositor's
        # window rules (by app_id, see set_app_id) do the layout instead.
        os.environ["QT_QPA_PLATFORM"] = "wayland"


def set_app_id(name):
    """Call after creating the QApplication: the Wayland app_id (and X11
    WM_CLASS), so compositor window rules can match the window."""
    from PyQt5 import QtGui
    QtGui.QGuiApplication.setDesktopFileName(name)


def use_opengl():
    """HF_GUI_OPENGL=1: draw plots through OpenGL (the GPU)."""
    return os.environ.get("HF_GUI_OPENGL") == "1"


def apply_compact_style(app):
    """Smaller font and tighter input widgets, app-wide, in compact mode."""
    if not is_compact():
        return
    # Qt's own Fusion style rather than the desktop theme's: launched from
    # the Raspberry Pi desktop, qt5ct selects the gtk2 style, whose taller
    # combo/spin boxes alone made the TX form overflow the screen.
    app.setStyle("Fusion")
    # Scaled from at most COMPACT_BASE_FONT_PT, not from whatever the
    # desktop set: launched from the Raspberry Pi desktop, Qt gets the
    # theme's 12 pt font (via qt5ct) instead of its own 9 pt default, and
    # 80% of that no longer fit.
    font = app.font()
    if font.pointSizeF() > 0:
        base = min(font.pointSizeF(), COMPACT_BASE_FONT_PT)
        font.setPointSizeF(max(7.0, base * COMPACT_FONT_SCALE))
    else:
        base = min(font.pixelSize(), round(COMPACT_BASE_FONT_PT * 96 / 72))
        font.setPixelSize(max(9, round(base * COMPACT_FONT_SCALE)))
    app.setFont(font)
    app.setStyleSheet(
        "QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding: 0px 2px; }"
        "QPushButton { padding: 2px 8px; }")


def tighten_form(form):
    """Less space between form rows, in compact mode, and a field goes
    under its label when the label is too long to fit beside it."""
    if is_compact():
        form.setVerticalSpacing(2)
        form.setContentsMargins(4, 4, 4, 4)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)


class GridForm:
    """A QFormLayout stand-in on a 4-column grid (label, field, label,
    field), so short settings can share a row and the form needs less
    height. addRow(label, field) spans the field across the rest of the
    row; addRow(widget) spans the whole row; addPair puts two settings side
    by side."""

    def __init__(self, parent):
        self.grid = QtWidgets.QGridLayout(parent)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(3, 1)
        if is_compact():
            self.grid.setVerticalSpacing(2)
            self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setRowStretch(1000, 1)  # rows packed at the top
        self._row = 0

    def _add(self, item, col, span):
        if isinstance(item, QtWidgets.QLayout):
            self.grid.addLayout(item, self._row, col, 1, span)
        else:
            if isinstance(item, str):
                item = QtWidgets.QLabel(item)
            self.grid.addWidget(item, self._row, col, 1, span)

    def addRow(self, label, field=None):
        if field is None:
            self._add(label, 0, 4)
        else:
            self._add(label, 0, 1)
            self._add(field, 1, 3)
        self._row += 1

    def addPair(self, label1, field1, label2, field2):
        """label2 None: field2 (e.g. a checkbox) takes both right-hand columns."""
        self._add(label1, 0, 1)
        self._add(field1, 1, 1)
        if label2 is None:
            self._add(field2, 2, 2)
        else:
            self._add(label2, 2, 1)
            self._add(field2, 3, 1)
        self._row += 1


class _FitScrollToContents(QtCore.QObject):
    def __init__(self, scroll, container):
        super().__init__(container)
        self._scroll = scroll
        self._container = container
        container.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() in (QtCore.QEvent.LayoutRequest, QtCore.QEvent.Resize):
            QtCore.QTimer.singleShot(0, self.fit)
        return False

    def fit(self):
        layout = self._container.layout()
        width = self._scroll.viewport().width()
        if layout.hasHeightForWidth():
            height = layout.totalHeightForWidth(width)
        else:
            height = layout.totalSizeHint().height()
        self._scroll.setMaximumHeight(height + 2 * self._scroll.frameWidth())


def fit_scroll_to_contents(scroll, container):
    """Keeps a QScrollArea no taller than its contents (re-measured whenever
    they change, e.g. a wrapped warning label growing), so spare height goes
    to whatever else is in the window; it still scrolls if the screen is too
    short for the contents."""
    return _FitScrollToContents(scroll, container)


def place(window, x_fraction, width_fraction):
    """Fills a full-height vertical strip of the available screen area,
    from x_fraction across, width_fraction wide (compact mode only -- on a
    big screen the window keeps its own default size)."""
    if not is_compact():
        return
    geo = _available_geometry()
    x = geo.x() + round(geo.width() * x_fraction)
    width = round(geo.width() * width_fraction)
    window.move(x, geo.y())
    # resize() sets the client area; leave room for the title bar and frame
    frame_w = window.frameGeometry().width() - window.geometry().width()
    frame_h = window.frameGeometry().height() - window.geometry().height()
    window.resize(width - max(frame_w, 0), geo.height() - max(frame_h, 30))
