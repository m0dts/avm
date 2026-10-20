"""Amplitude (AM/AM) and phase (AM/PM) predistortion for the transmitter.

A gain curve over the sample amplitude (|x|, 0 .. 1 = the DAC's full
scale), set by POINTS adjusters at POINT_AMPS, and interpolated into a
65536-entry (16-bit) table (flat beyond the last point). Each output sample is multiplied by the table's gain at its own
amplitude, to pre-correct an amplifier's gain compression or expansion,
and turned by its phase offset (degrees), to pre-correct the phase shift
an amplifier adds as it nears compression.

The settings live in one small JSON file, {"enabled": bool, "gains":
[POINTS floats], "phases": [POINTS degrees], "amps": their amplitudes},
written by the TX page's DPD popup (touch_dpd.py).
hf_ofdm_tx.py --dpd-file re-reads it whenever it changes, so the curve can
be adjusted live while transmitting."""
import json
import os

import numpy as np

# Where the adjusters sit: over the signal's own range, denser near its
# peaks. AVM's TX level (0.2 RMS) with PAPR reduction peaks at ~0.53 of full
# scale, and the amplifier measured (dpd/dpd_calibrate.py) linear to ~0.25
# then compressing: evenly spaced over 0..1, half the points were beyond the
# signal and only two covered the compression.
POINT_AMPS = (0.0, 0.05, 0.10, 0.15, 0.20, 0.28, 0.35, 0.42, 0.48, 0.55)
POINTS = len(POINT_AMPS)
TABLE_SIZE = 65536
GAIN_MIN, GAIN_MAX = 0.8, 1.3
PHASE_MAX = 10.0   # degrees either way
DPD_PATH = os.path.join(os.path.expanduser("~"), ".config", "hfmodem", "dpd.json")


def amplitudes():
    """The amplitude (0..1 of full scale) each adjuster sets."""
    return np.array(POINT_AMPS)


def _points(values, lo, hi, flat, amps=None):
    v = [min(hi, max(lo, float(x))) for x in values or []]
    if len(v) < 2:
        return [flat] * POINTS
    # saved at other amplitudes (files without "amps": evenly over 0..1):
    # the same curve, read at this version's points
    old = np.asarray(amps, float) if amps and len(amps) == len(v) else np.linspace(0.0, 1.0, len(v))
    if len(old) != POINTS or not np.allclose(old, amplitudes()):
        v = [float(x) for x in np.interp(amplitudes(), old, v)]
    return v


def load(path=DPD_PATH):
    """(enabled, gains, phases in degrees) from the file; off and flat if
    missing or bad."""
    try:
        with open(path) as f:
            d = json.load(f)
        if len(d.get("gains", [])) < 2:
            raise ValueError
        amps = d.get("amps")
        return (bool(d.get("enabled", False)), _points(d.get("gains"), GAIN_MIN, GAIN_MAX, 1.0, amps),
                _points(d.get("phases"), -PHASE_MAX, PHASE_MAX, 0.0, amps))
    except (OSError, ValueError, TypeError, AttributeError):
        return False, [1.0] * POINTS, [0.0] * POINTS


def save(enabled, gains, phases, path=DPD_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump({"enabled": bool(enabled), "gains": [round(float(g), 4) for g in gains],
                   "phases": [round(float(p), 2) for p in phases], "amps": list(POINT_AMPS)}, f)
    os.replace(tmp, path)   # the TX never reads half a file


def curve(values, n=TABLE_SIZE):
    """Gain (or phase) at n amplitudes evenly over 0..1: the adjusters joined
    by straight lines, flat beyond the last."""
    return np.interp(np.linspace(0.0, 1.0, n), amplitudes(), values).astype(np.float32)


def table(gains, phases):
    """The complex correction per amplitude: gain, turned by the phase."""
    return (curve(gains) * np.exp(1j * np.deg2rad(curve(phases)))).astype(np.complex64)


class Predistorter:
    """Applies the file's curve; checks the file (one stat) on each call."""

    def __init__(self, path):
        self.path = path
        self._mtime = None
        self.table = None   # None: off

    def _refresh(self):
        try:
            m = os.stat(self.path).st_mtime_ns
        except OSError:
            m = None
        if m == self._mtime:
            return
        self._mtime = m
        enabled, gains, phases = load(self.path)
        active = enabled and (any(g != 1.0 for g in gains) or any(p != 0.0 for p in phases))
        self.table = table(gains, phases) if active else None
        print("[dpd] " + ("on: gain " + " ".join(f"{g:.2f}" for g in gains)
                          + " / phase " + " ".join(f"{p:+.1f}" for p in phases) if active else "off"),
              flush=True, file=__import__("sys").stderr)

    def __call__(self, x):
        self._refresh()
        if self.table is None or len(x) == 0:
            return x
        idx = np.minimum((np.abs(x) * (TABLE_SIZE - 1)).astype(np.int32), TABLE_SIZE - 1)
        return x * self.table[idx]
