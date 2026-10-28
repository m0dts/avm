"""hf_ofdm_common.decode_frame backed by modem_rx.cpp (modem.dll /
libmodem.so beside this file, which holds the transmitter too): same
arguments and return tuple. OFDM modes (A, B, C, D, VU)."""
import ctypes
import os
import sys

import numpy as np

import hf_ofdm_common as _ofdm

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _LL, _I, _D = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_int, ctypes.c_double
_lib.tx_cfg_new.argtypes = [_I, _I, _I, _P, _I, _I, _I, _I, _I]
_lib.tx_cfg_new.restype = _P
_lib.rx_new.argtypes = [_P, _I, _D, _D]
_PLL, _PD, _PI = ctypes.POINTER(_LL), ctypes.POINTER(_D), ctypes.POINTER(_I)
_lib.rx_sc_sync.argtypes = [_P, _P, _LL, _LL, _LL, _LL, _LL, _I, _PLL, _PD, _PD]
_lib.rx_quick_sc_scan.argtypes = [_P, _LL, _I, _PLL, _PD]
_lib.rx_rotate.argtypes = [_P, _LL, _D, _P, _I]
_lib.rx_refine_frame_start.argtypes = [_P, _P, _LL, _LL, _LL, _D, _D, _I]
_lib.rx_refine_frame_start.restype = _LL
_lib.rx_resolve_integer_cfo.argtypes = [_P, _P, _LL, _I, _I, _I, _PI, _PD, _I]
_lib.rx_resolve_timing_offset.argtypes = [_P, _P, _LL, _LL, _LL, _D, _P, _I, _PLL, _PD, _PI, _I]
_lib.rx_new.restype = _P
_lib.rx_decode_frame.argtypes = [_P, _P, _LL, _LL, ctypes.POINTER(_D), ctypes.POINTER(_D),
                                 ctypes.POINTER(_LL), ctypes.POINTER(_LL)]
_lib.rx_decode_frame.restype = _I
_lib.rx_copy.argtypes = [_P, _P, _P]

_handles = {}


def _handle(cfg):
    key = (cfg.mode, cfg.n_fft, cfg.n_cp, tuple(np.asarray(cfg.fft_bins).tolist()), cfg.freq_int, cfg.time_int,
           cfg.preamble_halves, cfg.data_modulation, getattr(cfg, "fec_scheme", "viterbi").lower())
    h = _handles.get(key)
    if h is None:
        bins = np.ascontiguousarray(cfg.fft_bins, np.int64)
        tx = _lib.tx_cfg_new(cfg.n_fft, cfg.n_cp, len(bins), bins.ctypes.data, cfg.freq_int, cfg.time_int,
                             cfg.preamble_halves, _ofdm.data_bits_per_symbol(cfg), int(key[-1] == "ldpc"))
        h = _handles[key] = _lib.rx_new(tx, int(_ofdm.time_first_default(cfg)), float(cfg.fs),
                                       float(cfg.carrier_spacing))
    return h


def decode_frame(cfg, rx_after_preamble, verbose=False, max_wait_samples=None):
    if _ofdm.single_carrier(cfg):
        return _ofdm.decode_frame(cfg, rx_after_preamble, verbose, max_wait_samples)
    h = _handle(cfg)
    x = np.ascontiguousarray(rx_after_preamble, np.complex128)
    ber, evm, n_pay, n_pts = _D(), _D(), _LL(), _LL()
    flags = _lib.rx_decode_frame(h, x.ctypes.data, len(x), -1 if max_wait_samples is None else int(max_wait_samples),
                                 ctypes.byref(ber), ctypes.byref(evm), ctypes.byref(n_pay), ctypes.byref(n_pts))
    payload = np.empty(n_pay.value, np.uint8)
    points = np.empty(n_pts.value, np.complex128)
    _lib.rx_copy(h, payload.ctypes.data, points.ctypes.data)
    has_points = n_pts.value > 0 or not np.isnan(evm.value)
    return (payload.tobytes() if flags & 1 else None, bool(flags & 2), bool(flags & 4),
            None if np.isnan(ber.value) else ber.value,
            None if np.isnan(evm.value) else evm.value,
            points if has_points else None)


def _c128(x):
    """(complex128 array, was-complex64 flag) for the C++."""
    x = np.asarray(x)
    return np.ascontiguousarray(x, np.complex128), int(x.dtype == np.complex64)


# rx may be the receiver's GrowableComplexArray: its indices are global
# stream positions (old samples trimmed away), so only the part a call reads
# is sliced out -- by global index, as the Python does -- and passed with its
# starting position.

def schmidl_cox_sync(rx, cfg, search_start=0, search_len=None, prefer_earliest=False):
    L = cfg.n_fft // 2
    W = cfg.sc_window
    n_total = len(rx)
    max_search_len = n_total - search_start - (L + W)
    sl = max_search_len if search_len is None else search_len
    sl = max(1, min(sl, max_search_len))
    x, _ = _c128(rx[search_start:search_start + sl + L + W])
    st, cfo, m = _LL(), _D(), _D()
    _lib.rx_sc_sync(_handle(cfg), x.ctypes.data, int(search_start), len(x), n_total, int(search_start), int(sl),
                    int(prefer_earliest), ctypes.byref(st), ctypes.byref(cfo), ctypes.byref(m))
    return st.value, cfo.value, m.value


def quick_sc_scan(x, L):
    x, _ = _c128(x)
    d, m = _LL(), _D()
    _lib.rx_quick_sc_scan(x.ctypes.data, len(x), int(L), ctypes.byref(d), ctypes.byref(m))
    return d.value, m.value


def apply_cfo_correction(x, cfo_hz, fs):
    x0 = np.asarray(x)
    x, c64 = _c128(x0)
    out = np.empty(len(x), np.complex128)
    _lib.rx_rotate(x.ctypes.data, len(x), 2 * np.pi * cfo_hz / fs, out.ctypes.data, c64)
    return out.astype(np.complex64) if c64 else out


def refine_frame_start(cfg, rx, start_index, total_cfo_hz, min_peak_ratio=5.0):
    span = 3 * cfg.n_cp
    lo = max(0, start_index - span)
    x, c64 = _c128(rx[lo:start_index + cfg.preamble_len + span])
    return int(_lib.rx_refine_frame_start(_handle(cfg), x.ctypes.data, lo, lo + len(x), int(start_index),
                                          float(total_cfo_hz), float(min_peak_ratio), c64))


def resolve_integer_cfo(cfg, rx_after_preamble, search_range=30, prior_k=None):
    x, c64 = _c128(rx_after_preamble)
    k, conf = _I(), _D()
    _lib.rx_resolve_integer_cfo(_handle(cfg), x.ctypes.data, len(x), int(search_range), int(prior_k is not None),
                                int(prior_k or 0), ctypes.byref(k), ctypes.byref(conf), c64)
    return k.value, conf.value


def resolve_timing_offset(cfg, rx, start_index, total_cfo_hz, max_needed_len, candidate_deltas):
    d = np.ascontiguousarray(list(candidate_deltas), np.int64)
    n_hdr = _ofdm._header_symbol_plan(cfg)[-1] + 1
    probe_len = cfg.preamble_len + n_hdr * cfg.symbol_len
    lo = max(0, start_index + (int(d.min()) if len(d) else 0))
    hi = start_index + (int(d.max()) if len(d) else 0) + probe_len
    x, c64 = _c128(rx[lo:hi])
    delta, metric, ok = _LL(), _D(), _I()
    _lib.rx_resolve_timing_offset(_handle(cfg), x.ctypes.data, lo, lo + len(x), int(start_index),
                                  float(total_cfo_hz),
                                  d.ctypes.data, len(d), ctypes.byref(delta), ctypes.byref(metric),
                                  ctypes.byref(ok), c64)
    return delta.value, metric.value, bool(ok.value)
