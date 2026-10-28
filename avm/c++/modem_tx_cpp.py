"""hf_ofdm_common's transmit functions backed by modem_tx.cpp (modem_tx.dll
/ libmodem_tx.so beside this file): build_frame, reduce_papr and
fft_resample_rate, same arguments and results (to rounding), every mode
(A, B, C, D, VU; single-carrier S and SH)."""
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
_lib.tx_cfg_free.argtypes = [_P]
_lib.tx_build_frame.argtypes = [_P, ctypes.c_char_p, _LL]
_lib.tx_build_frame.restype = _LL
_lib.tx_reduce_papr.argtypes = [_P, _D, _I]
_lib.tx_frame_len.argtypes = [_P]
_lib.tx_frame_len.restype = _LL
_lib.tx_frame_copy.argtypes = [_P, _P]
_lib.tx_frame_set.argtypes = [_P, _P, _LL]
_lib.tx_resample_len.argtypes = [_LL, _D, _D]
_lib.tx_resample_len.restype = _LL
_lib.tx_resample.argtypes = [_P, _LL, _D, _D, _D, _D, _P]
_lib.tx_sc_new.argtypes = [_I, _I, _I]
_lib.tx_sc_new.restype = _P
_lib.tx_sc_free.argtypes = [_P]
_lib.tx_sc_build.argtypes = [_P, ctypes.c_char_p, _LL]
_lib.tx_sc_build.restype = _LL
_lib.tx_sc_copy.argtypes = [_P, _P]

_handles = {}


class _Handle:
    def __init__(self, cfg):
        bins = np.ascontiguousarray(cfg.fft_bins, np.int64)
        self.h = _lib.tx_cfg_new(cfg.n_fft, cfg.n_cp, len(bins), bins.ctypes.data, cfg.freq_int, cfg.time_int,
                                 cfg.preamble_halves, _ofdm.data_bits_per_symbol(cfg),
                                 int(getattr(cfg, "fec_scheme", "viterbi").lower() == "ldpc"))

    def __del__(self):
        try:
            if getattr(self, "h", None):
                _lib.tx_cfg_free(self.h)
        except Exception:
            pass        # interpreter shutdown: the library may already be gone


def _handle(cfg):
    key = (cfg.n_fft, cfg.n_cp, tuple(np.asarray(cfg.fft_bins).tolist()), cfg.freq_int, cfg.time_int,
           cfg.preamble_halves, cfg.data_modulation, getattr(cfg, "fec_scheme", "viterbi").lower())
    h = _handles.get(key)
    if h is None:
        h = _handles[key] = _Handle(cfg)
    return h.h


def _frame_out(h):
    out = np.empty(_lib.tx_frame_len(h), np.complex128)
    _lib.tx_frame_copy(h, out.ctypes.data)
    return out


class _SCHandle:
    def __init__(self, sh, k10, bps):
        self.h = _lib.tx_sc_new(sh, k10, bps)

    def __del__(self):
        try:
            if getattr(self, "h", None):
                _lib.tx_sc_free(self.h)
        except Exception:
            pass


def _build_sc(cfg, payload):
    payload = bytes(payload)
    if len(payload) >= 1 << 16:
        raise ValueError(f"{cfg.mode} payload over 65535 bytes")
    key = (cfg.mode, int(round(cfg.occupancy_hz / 10000)), cfg.bits_per_symbol)
    h = _handles.get(key)
    if h is None:
        h = _handles[key] = _SCHandle(int(cfg.mode == "SH"), *key[1:])
    out = np.empty(_lib.tx_sc_build(h.h, payload, len(payload)), np.complex64)
    _lib.tx_sc_copy(h.h, out.ctypes.data)
    return out


def build_frame(cfg, payload):
    if _ofdm.single_carrier(cfg):
        return _build_sc(cfg, payload)
    h = _handle(cfg)
    payload = bytes(payload)
    _lib.tx_build_frame(h, payload, len(payload))
    return _frame_out(h)


def reduce_papr(cfg, frame, target_db, iterations=_ofdm.PAPR_ITERATIONS):
    if _ofdm.single_carrier(cfg) or not target_db:
        return frame
    h = _handle(cfg)
    f = np.ascontiguousarray(frame, np.complex128)
    _lib.tx_frame_set(h, f.ctypes.data, len(f))
    _lib.tx_reduce_papr(h, float(target_db), int(iterations))
    return _frame_out(h)


def fft_resample_rate(x, fs_in, fs_out, cut_hz=None, taper_hz=0.0):
    if np.asarray(x).dtype != np.complex64:
        return _ofdm.fft_resample_rate(x, fs_in, fs_out, cut_hz, taper_hz)
    x = np.ascontiguousarray(x)
    out = np.empty(_lib.tx_resample_len(len(x), fs_in, fs_out), np.complex64)
    _lib.tx_resample(x.ctypes.data, len(x), float(fs_in), float(fs_out),
                     -1.0 if cut_hz is None else float(cut_hz), float(taper_hz), out.ctypes.data)
    return out
