"""hf_sc's receive kernels backed by modem_rx.cpp (modem.dll /
libmodem.so): mf, detect, sync (+ ref), header and decode for mode S, same
arguments and results as the Python's (to rounding: the Python works in
single precision). receive_one_fragment stays the Python's, calling these
through install(hf_sc)."""
import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _LL, _I, _D = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_int, ctypes.c_double
_PD, _PI, _PLL = ctypes.POINTER(_D), ctypes.POINTER(_I), ctypes.POINTER(_LL)
_lib.tx_sc_new.argtypes = [_I, _I, _I]
_lib.tx_sc_new.restype = _P
_lib.srx_new.argtypes = [_P]
_lib.srx_new.restype = _P
_lib.srx_mf.argtypes = [_P, _P, _LL, _P]
_lib.srx_derotated_mf.argtypes = [_P, _P, _LL, _D, _P]
_lib.srx_detect.argtypes = [_P, _P, _LL, _PLL, _PD, _PD]
_lib.srx_detect.restype = _I
_lib.srx_sync.argtypes = [_P, _P, _LL, _LL, _D, _PD, _PD, _PD, _PD]
_lib.srx_header.argtypes = [_P]
_lib.srx_header.restype = _I
_lib.srx_decode.argtypes = [_P, _P, _LL, _D, _D, _I, _PI, _PD, _PD]
_lib.srx_decode.restype = _LL
_lib.srx_decode_copy.argtypes = [_P, _P, _P]

_BPS = {"qpsk": 2, "8psk": 3, "16psk": 4}
_handles = {}


def _h(modulation="qpsk"):
    h = _handles.get(modulation)
    if h is None:
        h = _handles[modulation] = _lib.srx_new(_lib.tx_sc_new(0, 1, _BPS[modulation]))
    return h


def _c(x):
    return np.ascontiguousarray(x, np.complex128)


def mf(x):
    x = _c(x)
    out = np.empty(len(x), np.complex128)
    _lib.srx_mf(_h(), x.ctypes.data, len(x), out.ctypes.data)
    return out.astype(np.complex64)


def detect(z):
    z = _c(z)
    i, m, c = _LL(), _D(), _D()
    if not _lib.srx_detect(_h(), z.ctypes.data, len(z), ctypes.byref(i), ctypes.byref(m), ctypes.byref(c)):
        return None
    return i.value, m.value, c.value


def sync(x, i, cfo1):
    """(t0, cfo, ref): hf_sc._sync's t0 and cfo, and the preamble's
    reference phase the receiver computes from them."""
    x = _c(x)
    t0, cfo, rr, ri = _D(), _D(), _D(), _D()
    _lib.srx_sync(_h(), x.ctypes.data, len(x), int(i), float(cfo1), ctypes.byref(t0), ctypes.byref(cfo),
                  ctypes.byref(rr), ctypes.byref(ri))
    return t0.value, cfo.value, complex(rr.value, ri.value)


def header():
    """The header's length from the last sync, or None."""
    n = _lib.srx_header(_h())
    return None if n < 0 else n


def decode(x, t0, cfo, n_bytes, modulation="qpsk"):
    """hf_sc.decode on raw samples x (derotated + matched-filtered here):
    (payload, crc_ok, ber, evm %, constellation)."""
    h = _h(modulation)
    x = _c(x)
    ok, ber, evm = _I(), _D(), _D()
    n_d = _lib.srx_decode(h, x.ctypes.data, len(x), float(t0), float(cfo), int(n_bytes), ctypes.byref(ok),
                          ctypes.byref(ber), ctypes.byref(evm))
    payload = np.empty(n_bytes, np.uint8)
    d = np.empty(n_d, np.complex128)
    _lib.srx_decode_copy(h, payload.ctypes.data, d.ctypes.data)
    return payload.tobytes(), bool(ok.value), ber.value, evm.value, d.astype(np.complex64)
