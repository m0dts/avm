"""Config tab of the touch GUI: the set-up choices that rarely change once
a station is running -- radios, capture and playback devices, what TX
sends, the callsign, spectrum averaging -- so the TX and RX tabs keep only
what's adjusted on air.

The radio, device and callsign controls are the TX and RX pages' own
widgets, placed here: everything behind them (saved settings, live gain,
radio checks, the Lime port / RTL ppm rows that show for their radio)
works exactly as before. Content and spectrum averaging are new here and
go through TxPage.set_content / RxPage.set_spectrum_averages."""
from PyQt5 import QtWidgets

import touch_widgets as tw
from touch_tx_page import s_get

CONTENT = [("auto", "Auto"), ("audio", "Audio only"), ("video", "Video only")]
SPECTRUM_AVERAGES = ("4", "8", "16", "32")
MODE_TYPES = [("hf", "HF"), ("vu", "VHF/UHF"), ("sat", "SAT")]
LEFT_COLUMN_STRETCH = 46  # the settings column: % of the page width
RIGHT_COLUMN_STRETCH = 44  # the "Very Experimental!" column
EXPERIMENTAL_COLOUR = "#ff8c1a"


class ConfigPage(QtWidgets.QWidget):
    def __init__(self, settings, tx, rx, parent=None):
        super().__init__(parent)
        self.s, self.tx, self.rx = settings, tx, rx
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(8, round(28 * tw.SCALE), 8, 8)  # some space below the tabs
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)

        # mode family: not yet -- shown so it's clear where it will go
        self.mode_type = tw.Segmented(MODE_TYPES)  # none selected: not available yet
        self.mode_type.setEnabled(False)
        mode_row = QtWidgets.QWidget()
        mode_row.setObjectName("seg")
        ml = QtWidgets.QHBoxLayout(mode_row)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.addWidget(self.mode_type, 1)
        tbc = QtWidgets.QLabel("TBC")
        tbc.setObjectName("dim")
        ml.addWidget(tbc)

        self.content = tw.Segmented(CONTENT, s_get(settings, "tx_content", "auto"))
        self.content.changed.connect(lambda v: self.tx.set_content(v))
        avg = str(s_get(settings, "rx_spec_avg", 16))
        self.averages = tw.Segmented([(a, a) for a in SPECTRUM_AVERAGES],
                                     avg if avg in SPECTRUM_AVERAGES else "16")
        self.averages.changed.connect(lambda v: self.rx.set_spectrum_averages(int(v)))

        rows = [
            ("Mode type", mode_row),
            ("TX radio", tw.freq_radio_row(tx.radio, tx.lime_port)),
            ("RX radio", tw.freq_radio_row(rx.radio, rx.lime_port, rx.ppm)),
            ("Camera", tx.camera),
            ("Mic", tx.mic),
            ("Audio out", rx.audio_out),
            ("Content", self.content),
            ("Callsign", tx.callsign),
            ("Spectrum avg", self.averages),
        ]
        for r, (label, w) in enumerate(rows):
            grid.addWidget(tw.row_label(label), r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(1, 1)
        # one column, a little under half the width, on the left with some
        # padding -- full width looked stretched (and leaves room for a
        # right-hand column later)
        cols = QtWidgets.QHBoxLayout()
        cols.setContentsMargins(round(16 * tw.SCALE), 0, round(16 * tw.SCALE), 0)
        cols.addLayout(grid, LEFT_COLUMN_STRETCH)
        cols.addStretch(100 - LEFT_COLUMN_STRETCH - RIGHT_COLUMN_STRETCH)
        cols.addLayout(self._experimental_column(), RIGHT_COLUMN_STRETCH)
        root.addLayout(cols)
        root.addStretch(1)

        self._refresh_duplex()

        # the saved choices take effect from the start
        self.tx.set_content(self.content.value())
        self.rx.set_spectrum_averages(int(self.averages.value()))

    def _experimental_column(self):
        """Right-hand column: features that work in tests but aren't
        finished -- same-channel duplex (TDD) so far."""
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(5)
        head = QtWidgets.QLabel("Very Experimental!")
        head.setStyleSheet(f"color: {EXPERIMENTAL_COLOUR}; font-weight: bold;")
        col.addWidget(head)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        self.duplex = tw.Segmented(tw.DUPLEX_ROLES, self.s.get("duplex", "off"))
        self.duplex.changed.connect(self._duplex_changed)
        grid.addWidget(tw.row_label("Duplex (TDD)"), 0, 0)
        grid.addWidget(self.duplex, 0, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)
        self.duplex_note = QtWidgets.QLabel()
        self.duplex_note.setObjectName("dim")
        self.duplex_note.setWordWrap(True)
        col.addWidget(self.duplex_note)
        col.addStretch(1)
        return col

    def _duplex_changed(self, role):
        """Both ends on one frequency, taking turns: Master keeps its own
        timing, Slave fits its slot into the master's gaps. Applied at once:
        a running receiver is restarted (in duplex it also transmits, so a
        stale one left a second transmitter on the radio), and TX with it.
        Turned Off: both just stop (nothing restarted in simplex)."""
        was_duplex = self.s.get("duplex", "off") != "off"
        self.s["duplex"] = role
        tx_on, rx_on = self.tx.is_running(), self.rx.is_running()
        if tx_on:
            self.tx.stop()
        if rx_on:
            self.rx.stop()
        for page in (self.tx, self.rx):
            page._apply_to_engine()
        if role == "off" and was_duplex:
            tx_on = rx_on = False
        if rx_on:
            self.rx.start()
        if tx_on:
            self.tx.start()   # (in duplex this brings RX up itself)
        self._refresh_duplex()

    def _refresh_duplex(self):
        why = tw.duplex_why_not(self.tx.mode.value(), self.rx.mode.value(),
                                self.tx.engine.sdr, self.rx.engine.sdr)
        self.duplex.setEnabled(not why)
        role = self.s.get("duplex", "off")
        if why:
            self.duplex_note.setText(f"Not available: {why}.")
        elif role == "off":
            self.duplex_note.setText("Both stations on one frequency, taking turns "
                                     f"({tw.TDD_SLOT_S:g} s slots every {tw.TDD_CYCLE_S:g} s). "
                                     "One end Master, the other Slave; same mode and width both ends.")
        else:
            self.duplex_note.setText(f"{role.capitalize()}: TX starts RX too (the receiver drives the "
                                     "radio for both). Each way gets under half the normal rate, "
                                     "with about a second more delay. Tested: VU 160 kHz 16QAM.")

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_duplex()  # the TX/RX pages' mode or radio may have changed
