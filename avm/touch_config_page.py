"""Config tab of the touch GUI: the set-up choices that rarely change once
a station is running -- radios, capture and playback devices, what TX
sends, the callsign, spectrum averaging -- so the TX and RX tabs keep only
what's adjusted on air.

The radio, device and callsign controls are the TX and RX pages' own
widgets, placed here: everything behind them (saved settings, live gain,
radio checks, the Lime port / RTL ppm rows that show for their radio)
works exactly as before. Content and spectrum averaging are new here and
go through TxPage.set_content / RxPage.set_spectrum_averages."""
from PyQt5 import QtCore, QtWidgets

import touch_widgets as tw
import touch_tx_page
from touch_tx_page import s_get

CONTENT = [("auto", "Auto"), ("audio", "Audio only"), ("video", "Video only")]
SPECTRUM_AVERAGES = ("1", "4", "8", "16", "32")  # 1: none
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

        # mode family: what the TX and RX pages offer (tw.MODE_TYPE_SETS) --
        # HF: A-D, VHF/UHF: VU, SAT: S; the rest of their buttons are hidden
        if "mode_type" not in settings:
            settings["mode_type"] = tw.mode_type_of(s_get(settings, "tx_mode", "A"))
        self.mode_type = tw.Segmented(MODE_TYPES, settings["mode_type"])
        self.mode_type.changed.connect(self._mode_type_changed)
        mode_row = self.mode_type

        self.content = tw.Segmented(CONTENT, s_get(settings, "tx_content", "auto"))
        self.content.changed.connect(lambda v: self.tx.set_content(v))
        avg = str(s_get(settings, "rx_spec_avg", 16))
        self.averages = tw.Segmented([(a, "None" if a == "1" else a) for a in SPECTRUM_AVERAGES],
                                     avg if avg in SPECTRUM_AVERAGES else "16")
        self.averages.changed.connect(lambda v: self.rx.set_spectrum_averages(int(v)))

        # KiwiSDR to receive from (RX radio "KiwiSDR"): host:port
        tw.KIWI_SERVER = s_get(settings, "kiwi_server", "")
        self.kiwi = tw.TextButton("KiwiSDR server (host:port)", tw.KIWI_SERVER, max_len=40,
                                  placeholder="(none -- e.g. KIWI.EXAMPLE.ORG:8073)",
                                  rows=tw.KeyboardDialog.HOST_ROWS)
        self.kiwi.changed.connect(self._kiwi_changed)

        # TX peak reduction (clip and filter, receiver-transparent): measured
        # at the same amplifier peak, mode A 10 kHz decoded with 2-4 dB less
        # signal at 7 dB, MER ceiling still ~30 dB; ~15% of a core at VU 160
        self.papr = tw.Segmented([("0", "Off"), ("7", "On")], str(s_get(settings, "tx_papr", "0")))
        self.papr.changed.connect(self._papr_changed)

        # black and white video: every bit to brightness (low bitrates)
        self.mono = tw.Segmented([("colour", "Colour"), ("bw", "B/W")],
                                 "bw" if s_get(settings, "tx_mono", False) else "colour")
        self.mono.changed.connect(self._mono_changed)

        # receive paths that mirror the spectrum (sync triggers, every header fails)
        self.invert = tw.Segmented([("normal", "Normal"), ("inverted", "Inverted")],
                                   "inverted" if s_get(settings, "rx_invert", False) else "normal")
        self.invert.changed.connect(self._invert_changed)

        rows = [
            ("Mode type", mode_row),
            ("TX radio", tw.freq_radio_row(tx.radio, tx.lime_port)),
            ("RX radio", tw.freq_radio_row(rx.radio, rx.lime_port, rx.ppm, rx.anan_ant)),
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
        right = self._experimental_column()
        # the left column is full: later settings go at the bottom of the right
        more = QtWidgets.QGridLayout()
        more.setHorizontalSpacing(8)
        more.setVerticalSpacing(5)
        for r, (label, w) in enumerate((("Video", self.mono), ("KiwiSDR", self.kiwi),
                                        ("RX spectrum", self.invert))):
            more.addWidget(tw.row_label(label), r, 0)
            more.addWidget(w, r, 1)
        more.setColumnStretch(1, 1)
        right.addLayout(more)
        cols.addLayout(right, RIGHT_COLUMN_STRETCH)
        root.addLayout(cols)
        root.addStretch(1)

        self._refresh_duplex()
        self._apply_experimental()
        self._mode_type_changed(self.s["mode_type"])

        # the saved choices take effect from the start
        self.tx.set_content(self.content.value())
        self.rx.set_spectrum_averages(int(self.averages.value()))
        self.rx.engine.invert_spectrum = self.invert.value() == "inverted"
        self.tx.engine.papr_db = float(self.papr.value() or 0) if self.experimental() else 0.0
        self.tx.engine.mono = self.mono.value() == "bw"

    def _mode_type_changed(self, value):
        self.s["mode_type"] = value
        for page in (self.tx, self.rx):
            if tw.apply_mode_type(page, value):
                page._changed()          # a hidden setting moved: apply (and save) it
            else:
                page._apply_to_engine()  # widths/modulations greyed per mode again
        self._refresh_duplex()

    def _mono_changed(self, value):
        self.s["tx_mono"] = value == "bw"
        self.tx.set_mono(value == "bw")

    def _papr_changed(self, value):
        self.s["tx_papr"] = value
        if self.experimental():
            self.tx.set_papr(value)

    def _invert_changed(self, value):
        self.s["rx_invert"] = value == "inverted"
        self.rx.set_invert_spectrum(value == "inverted")

    def _kiwi_changed(self, server):
        self.s["kiwi_server"] = server
        tw.KIWI_SERVER = server
        if self.rx.engine.sdr == "kiwi":
            self.rx._apply_to_engine()  # next start uses it

    def _experimental_column(self):
        """Right-hand column: features that can badly distort a transmission
        if misused -- duplex (TDD), TX peak reduction and predistortion
        (DPD). All off and greyed until "Experimental" is switched on, which
        asks first."""
        outer = QtWidgets.QVBoxLayout()
        outer.setSpacing(5)
        # the experimental settings in an orange box
        box = QtWidgets.QFrame()
        box.setObjectName("experimental")
        box.setStyleSheet(f"QFrame#experimental {{ border: 2px solid {EXPERIMENTAL_COLOUR}; border-radius: 6px; }}")
        col = QtWidgets.QVBoxLayout(box)
        col.setContentsMargins(6, 6, 6, 6)
        col.setSpacing(5)
        outer.addWidget(box)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(5)
        self.exp_switch = tw.Segmented([("off", "Off"), ("on", "On")],
                                       "on" if self.s.get("experimental", False) else "off")
        self.exp_switch.changed.connect(self._experimental_changed)
        self.duplex = tw.Segmented(tw.DUPLEX_ROLES, self.s.get("duplex", "off"))
        self.duplex.changed.connect(self._duplex_changed)
        # TX predistortion: its own popup (touch_dpd); the button shows on/off
        self.dpd_btn = QtWidgets.QPushButton()
        self.dpd_btn.setFocusPolicy(QtCore.Qt.NoFocus)
        self.dpd_btn.clicked.connect(self._open_dpd)
        for r, (label, w) in enumerate((("Experimental", self.exp_switch), ("Duplex (TDD)", self.duplex),
                                        ("TX peak reduce", self.papr), ("TX DPD", self.dpd_btn))):
            grid.addWidget(tw.row_label(label), r, 0)
            grid.addWidget(w, r, 1)
        grid.setColumnStretch(1, 1)
        col.addLayout(grid)
        self.duplex_note = QtWidgets.QLabel()
        self.duplex_note.setObjectName("dim")
        self.duplex_note.setWordWrap(True)
        col.addWidget(self.duplex_note)
        outer.addStretch(1)
        return outer

    def experimental(self):
        return bool(self.s.get("experimental", False))

    def _experimental_changed(self, value):
        if value == "on" and not self.experimental():
            if not tw.ask_yes_no(self, "Enable experimental features?", "These settings can severely distort transmissions if used incorrectly.\n\nDon't use them unless you understand how to monitor your transmissions before use on air.",
                                 yes="Enable", no="Cancel"):
                self.exp_switch.set_value("off")
                return
        self.s["experimental"] = value == "on"
        self._apply_experimental()

    def _apply_experimental(self):
        """Experimental off: duplex, peak reduction and DPD all switched Off
        (DPD keeps its curve, switched off) and their controls greyed. Each
        has to be switched on again by hand after re-enabling."""
        on = self.experimental()
        for w in (self.duplex, self.papr, self.dpd_btn):
            w.setEnabled(on)
        if not on:
            if self.s.get("duplex", "off") != "off":
                self.duplex.set_value("off")
                self._duplex_changed("off")
            self.papr.set_value("0")
            self.s["tx_papr"] = "0"
            import dpd
            enabled, gains, phases = dpd.load()
            if enabled:
                try:
                    dpd.save(False, gains, phases)   # a running TX drops it at its next fragment
                except OSError as e:
                    tw.notices().add("TX", f"DPD could not be switched off: {e}")
        self.tx.set_papr((self.papr.value() or 0) if on else 0)
        self.tx.engine.dpd_allowed = on
        self._show_dpd()
        if on:
            self._refresh_duplex()   # its own per-role greying

    def _show_dpd(self):
        import dpd
        on = self.experimental() and dpd.load()[0]
        self.dpd_btn.setText("On -- adjust..." if on else "Off -- adjust...")
        self.dpd_btn.setStyleSheet(f"background: {EXPERIMENTAL_COLOUR}; color: #111;" if on else "")

    def _open_dpd(self):
        import touch_dpd
        touch_dpd.DpdDialog(self, rms=touch_tx_page.TX_AMPLITUDE).exec_()
        self._show_dpd()

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
        # Off always selectable: greying the whole control locked it on
        # Master/Slave whenever duplex couldn't run (e.g. mode A)
        for role_key, button in self.duplex._buttons.items():
            button.setEnabled(role_key == "off" or (not why and self.experimental()))
        role = self.s.get("duplex", "off")
        if why:
            self.duplex_note.setText(f"TDD: not available: {why}.")
        elif role == "off":
            self.duplex_note.setText("TDD: both stations on one frequency, taking turns "
                                     f"({tw.TDD_SLOT_S:g} s slots every {tw.TDD_CYCLE_S:g} s). "
                                     "One end Master, the other Slave; same mode and width both ends.")
        else:
            self.duplex_note.setText(f"TDD {role.capitalize()}: TX starts RX too (the receiver drives the "
                                     "radio for both). Each way gets under half the normal rate, "
                                     "with about a second more delay. Tested: VU 160 kHz 16QAM.")

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh_duplex()  # the TX/RX pages' mode or radio may have changed
