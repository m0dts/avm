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
        cols.setContentsMargins(round(16 * tw.SCALE), 0, 0, 0)
        cols.addLayout(grid, LEFT_COLUMN_STRETCH)
        cols.addStretch(100 - LEFT_COLUMN_STRETCH)
        root.addLayout(cols)
        root.addStretch(1)

        # the saved choices take effect from the start
        self.tx.set_content(self.content.value())
        self.rx.set_spectrum_averages(int(self.averages.value()))
