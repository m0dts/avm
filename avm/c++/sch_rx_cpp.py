"""hf_sch's receive kernels backed by modem_rx.cpp (modem.dll /
libmodem.so): detect, header and the whole-frame decode (fine frequency,
header, turbo decode) for mode SH. Same decisions as the Python's, metrics
to rounding (the Python works partly in single precision)."""
import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _LL, _I, _D = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_int, ctypes.c_double
_PD, _PI = ctypes.POINTER(_D), ctypes.POINTER(_I)
_lib.tx_sc_new.argtypes = [_I, _I, _I]
_lib.tx_sc_new.restype = _P
_lib.shrx_new.argtypes = [_P]
_lib.shrx_new.restype = _P
_lib.shrx_detect.argtypes = [_P, _P, _LL, _D, _PD, _PD, _PI]
_lib.shrx_detect.restype = _I
_lib.shrx_header.argtypes = [_P, _P, _LL, _D, _D, _I]
_lib.shrx_header.restype = _I
_lib.shrx_decode.argtypes = [_P, _P, _LL, _D, _D, _I, _I, _PI, _PD, _PD, _PD]
_lib.shrx_decode.restype = _LL
_lib.shrx_decode_copy.argtypes = [_P, _P, _P]

_BPS = {"qpsk": 2, "8psk": 3, "16psk": 4}
_handles = {}


def _h(cfg):
    key = (int(round(cfg.occupancy_hz / 10000)), cfg.data_modulation)
    h = _handles.get(key)
    if h is None:
        h = _handles[key] = _lib.shrx_new(_lib.tx_sc_new(1, key[0], _BPS[key[1]]))
    return h


def _c(x):
    return np.ascontiguousarray(x, np.complex128)


def detect(cfg, x):
    """hf_sch._detect: (t0, cfo, variant), "short", or None."""
    x = _c(x)
    t0, cfo, v = _D(), _D(), _I()
    got = _lib.shrx_detect(_h(cfg), x.ctypes.data, len(x), float(cfg.symbol_rate), ctypes.byref(t0),
                           ctypes.byref(cfo), ctypes.byref(v))
    if got == 2:
        return "short"
    return (t0.value, cfo.value, v.value) if got == 1 else None


def header(cfg, x, t0, cfo, variant):
    """Payload length from the header (hf_sch._header on the grid), or None."""
    x = _c(x)
    n = _lib.shrx_header(_h(cfg), x.ctypes.data, len(x), float(t0), float(cfo), int(variant))
    return None if n < 0 else n


def decode(cfg, x, t0, cfo, variant, n_bytes):
    """The frame as receive_one_fragment decodes it (fine frequency from the
    UWs, header, turbo decode): (payload, crc_ok, ber, evm %, constellation,
    refined cfo)."""
    h = _h(cfg)
    x = _c(x)
    ok, ber, cfo2, snr = _I(), _D(), _D(), _D()
    n_d = _lib.shrx_decode(h, x.ctypes.data, len(x), float(t0), float(cfo), int(variant), int(n_bytes),
                           ctypes.byref(ok), ctypes.byref(ber), ctypes.byref(cfo2), ctypes.byref(snr))
    payload = np.empty(n_bytes, np.uint8)
    d = np.empty(n_d, np.complex128)
    _lib.shrx_decode_copy(h, payload.ctypes.data, d.ctypes.data)
    return (payload.tobytes(), bool(ok.value), ber.value, 100 * 10 ** (-snr.value / 20), d.astype(np.complex64),
            cfo2.value)
