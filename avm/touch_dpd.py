"""The TX page's DPD popup: amplitude and phase predistortion (dpd.py), each
set by a row of adjusters, one per amplitude point, with the resulting curve drawn
below them. Every change is saved straight away and the transmitter picks
it up within a fragment, so it can be tuned live while watching the signal
on another receiver."""
import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

import dpd
import touch_widgets as tw

# OFDM's peaks sit ~10 dB above its RMS: where on the curve the signal lives
PEAK_OVER_RMS = 3.2


class _Curve(QtWidgets.QWidget):
    """Output amplitude against input (the corrected transfer curve) and the
    gain itself, over 0..full scale, with the signal's RMS and typical peak."""

    def __init__(self, rms):
        super().__init__()
        self.gains = [1.0] * dpd.POINTS
        self.phases = [0.0] * dpd.POINTS
        self.enabled = False
        self.rms = rms
        self.setMinimumHeight(120)

    def set(self, gains, phases, enabled):
        self.gains, self.phases, self.enabled = list(gains), list(phases), enabled
        self.update()

    def paintEvent(self, _):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.rect().adjusted(36, 6, -8, -18)
        p.fillRect(self.rect(), QtGui.QColor("#1c1d20"))
        p.setPen(QtGui.QColor("#3c4046"))
        for i in range(5):
            x = r.left() + r.width() * i / 4
            y = r.top() + r.height() * i / 4
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.drawLine(QtCore.QPointF(r.left(), y), QtCore.QPointF(r.right(), y))
        p.setPen(QtGui.QColor("#9aa0a8"))
        f = p.font()
        f.setPixelSize(max(9, round(11 * tw.SCALE)))
        p.setFont(f)
        p.drawText(QtCore.QRectF(r.left(), r.bottom() + 2, r.width(), 16), QtCore.Qt.AlignCenter, "input amplitude (of full scale)")
        # the signal: RMS and typical peaks (amplitude setting)
        for a, label in ((self.rms, "rms"), (min(1.0, self.rms * PEAK_OVER_RMS), "peaks")):
            x = r.left() + r.width() * a
            p.setPen(QtGui.QPen(QtGui.QColor("#f0c040"), 1, QtCore.Qt.DashLine))
            p.drawLine(QtCore.QPointF(x, r.top()), QtCore.QPointF(x, r.bottom()))
            p.drawText(QtCore.QPointF(x + 3, r.top() + 12), label)
        a = np.linspace(0, 1, 200)
        g = dpd.curve(self.gains, 200) if self.enabled else np.ones(200, np.float32)
        ph = dpd.curve(self.phases, 200) if self.enabled else np.zeros(200, np.float32)
        g_lo, g_hi = dpd.GAIN_MIN, dpd.GAIN_MAX

        def line(ys, lo, hi, colour, width=2):
            pts = [QtCore.QPointF(r.left() + r.width() * x, r.bottom() - r.height() * (y - lo) / (hi - lo))
                   for x, y in zip(a, ys)]
            p.setPen(QtGui.QPen(QtGui.QColor(colour), width))
            p.drawPolyline(QtGui.QPolygonF(pts))
        line(a, 0, 1, "#555", 1)                 # straight through (no correction)
        line(np.clip(a * g, 0, 1), 0, 1, "#2f9e5a")   # output amplitude
        line(g, g_lo, g_hi, "#5aa0ff")           # the gain, own scale
        line(ph, -dpd.PHASE_MAX, dpd.PHASE_MAX, "#e07030")   # the phase, own scale
        p.setPen(QtGui.QColor("#5aa0ff"))
        p.drawText(QtCore.QPointF(2, r.top() + 10), f"{g_hi:g}")
        p.drawText(QtCore.QPointF(2, r.bottom()), f"{g_lo:g}")
        p.setPen(QtGui.QColor("#2f9e5a"))
        p.drawText(QtCore.QPointF(2, r.center().y() - 4), "out")
        p.setPen(QtGui.QColor("#e07030"))
        p.drawText(QtCore.QPointF(2, r.center().y() + 12), f"±{dpd.PHASE_MAX:g}°")


class _Stepper(QtCore.QObject):
    """One adjuster: a + button above a - button (sliders were hard to move
    a step at a time on the touchscreen). Held down, they repeat. The same
    value()/setValue()/valueChanged as the slider it replaces."""
    valueChanged = QtCore.pyqtSignal(int)

    def __init__(self, lo, hi, value):
        super().__init__()
        self.lo, self.hi, self._v = lo, hi, value
        self.plus, self.minus = QtWidgets.QPushButton("+"), QtWidgets.QPushButton("−")
        for b, d in ((self.plus, 1), (self.minus, -1)):
            b.setFocusPolicy(QtCore.Qt.NoFocus)
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(90)
            b.setMinimumSize(1, round(40 * tw.SCALE))
            b.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Expanding)
            b.setStyleSheet("padding: 0; font-size: 18px; font-weight: bold;")
            b.clicked.connect(lambda _=False, d=d: self.setValue(self._v + d))

    def value(self):
        return self._v

    def setValue(self, v):
        v = max(self.lo, min(self.hi, int(v)))
        if v != self._v:
            self._v = v
            if not self.signalsBlocked():
                self.valueChanged.emit(v)


class DpdDialog(QtWidgets.QDialog):
    def __init__(self, parent, rms=0.2):
        super().__init__(parent, QtCore.Qt.FramelessWindowHint | QtCore.Qt.Dialog)
        self.setModal(True)
        enabled, gains, phases = dpd.load()
        lay = QtWidgets.QVBoxLayout(self)
        top = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("TX predistortion")
        title.setObjectName("big")
        top.addWidget(title, 1)
        self.on = QtWidgets.QPushButton()
        self.on.setCheckable(True)
        self.on.setChecked(enabled)
        self.on.toggled.connect(self._changed)
        top.addWidget(self.on)
        flat = QtWidgets.QPushButton("Flat")
        flat.clicked.connect(self._flat)
        top.addWidget(flat)
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(self.accept)
        top.addWidget(close)
        lay.addLayout(top)

        # gain (left) and phase (right): one adjuster per amplitude point each
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(2)
        self.sliders, self.values = self._group(row, "Gain", "#5aa0ff", gains, 100,
                                                dpd.GAIN_MIN, dpd.GAIN_MAX, "{:.2f}")
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.VLine)
        sep.setStyleSheet("color: #555;")
        row.addWidget(sep)
        self.p_sliders, self.p_values = self._group(row, "Phase °", "#e07030", phases, 2,
                                                    -dpd.PHASE_MAX, dpd.PHASE_MAX, "{:+.1f}")
        lay.addLayout(row, 3)
        self.curve = _Curve(rms)
        lay.addWidget(self.curve, 2)
        hint = QtWidgets.QLabel("Gain and phase at each input amplitude (1 and 0 = none). Applied live while transmitting.")
        hint.setStyleSheet("color: #9aa0a8;")
        lay.addWidget(hint)
        # saved a moment after the last change, not on every slider step
        self._save_timer = QtCore.QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(150)
        self._save_timer.timeout.connect(self._save)
        self._changed(save=False)
        self.setGeometry(tw.dialog_geometry(parent))

    def _group(self, row, name, colour, values, steps, lo, hi, fmt):
        """One set of adjusters, its name above and the amplitudes below.
        steps: slider steps per unit (gain: 0.01, phase: 0.5 degree)."""
        sliders, labels = [], []
        box = QtWidgets.QVBoxLayout()
        box.setSpacing(1)
        head = QtWidgets.QLabel(name)
        head.setAlignment(QtCore.Qt.AlignCenter)
        head.setStyleSheet(f"color: {colour}; font-weight: bold;")
        box.addWidget(head)
        cols = QtWidgets.QHBoxLayout()
        cols.setSpacing(2)
        for amp, x in zip(dpd.amplitudes(), values):
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(1)
            v = QtWidgets.QLabel(fmt.format(x))
            v.setAlignment(QtCore.Qt.AlignCenter)
            # 2 x POINTS columns on an 800-pixel screen: small labels
            v.setStyleSheet(f"font-size: {max(9, round(12 * tw.SCALE))}px;")
            v.setMinimumWidth(1)
            s = _Stepper(round(lo * steps), round(hi * steps), round(x * steps))
            s.valueChanged.connect(self._changed)
            a = QtWidgets.QLabel("0" if amp == 0 else "1" if amp >= 1 else f"{amp:.2f}".lstrip("0"))
            a.setAlignment(QtCore.Qt.AlignCenter)
            a.setStyleSheet(f"color: #9aa0a8; font-size: {max(9, round(12 * tw.SCALE))}px;")
            a.setMinimumWidth(1)
            col.addWidget(s.plus, 1)
            col.addWidget(v)
            col.addWidget(s.minus, 1)
            col.addWidget(a)
            cols.addLayout(col)
            sliders.append(s)
            labels.append(v)
        box.addLayout(cols, 1)
        row.addLayout(box, 1)
        return sliders, labels

    def gains(self):
        return [s.value() / 100 for s in self.sliders]

    def phases(self):
        return [s.value() / 2 for s in self.p_sliders]

    def _changed(self, *_, save=True):
        g, ph = self.gains(), self.phases()
        for v, x in zip(self.values, g):
            v.setText(f"{x:.2f}")
        for v, x in zip(self.p_values, ph):
            v.setText(f"{x:+.1f}")
        self.on.setText("DPD on" if self.on.isChecked() else "DPD off")
        self.on.setStyleSheet("background: #2f9e5a; color: #111;" if self.on.isChecked() else "")
        self.curve.set(g, ph, self.on.isChecked())
        if save:
            self._save_timer.start()

    def _flat(self):
        for s in self.sliders + self.p_sliders:
            s.blockSignals(True)
            s.setValue(100 if s in self.sliders else 0)
            s.blockSignals(False)
        self._changed()

    def _save(self):
        try:
            dpd.save(self.on.isChecked(), self.gains(), self.phases())
        except OSError as e:
            tw.notices().add("TX", f"DPD settings not saved: {e}")

    def done(self, r):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()
        super().done(r)
