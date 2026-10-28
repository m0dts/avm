"""hf_ofdm_common's receive front end with its per-sample work in
modem_rx.cpp (modem.dll / libmodem.so): install(ofdm) swaps in versions of
_iq_apply, apply_lowpass_filter_incremental, stream_lowpass_filter,
linear_resample and linear_resample_incremental that keep the Python's
caching and trimming logic line for line and call the C++ kernels for the
arithmetic. The IQ transform and the resampling are bit-identical; the
FIR matches to rounding (numpy's convolve sums in BLAS order)."""
import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _LL, _I, _D = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_int, ctypes.c_double
_lib.fe_iq_apply.argtypes = [_P, _LL, _D, _D, _D, _D, _P]
_lib.fe_fir_valid.argtypes = [_P, _LL, _P, _I, _P]
_lib.fe_interp.argtypes = [_P, _LL, _LL, _D, _D, _LL, _LL, _P]


def iq_apply(vals, mean_I, mean_Q, slope, scale):
    x = np.ascontiguousarray(vals, np.complex128)
    out = np.empty_like(x)
    _lib.fe_iq_apply(x.ctypes.data, len(x), float(mean_I), float(mean_Q), float(slope), float(scale),
                     out.ctypes.data)
    return out


def convolve_valid(x, taps):
    """np.convolve(x, taps, mode="valid") for complex x, real taps."""
    x = np.ascontiguousarray(x, np.complex128)
    t = np.ascontiguousarray(taps, np.float64)
    n = max(0, len(x) - len(t) + 1)
    out = np.empty(n, np.complex128)
    if n:
        _lib.fe_fir_valid(x.ctypes.data, len(x), t.ctypes.data, len(t), out.ctypes.data)
    return out


def interp(x, i0, fs_in, fs_out, o0, o1):
    """np.interp of x (input times arange(i0, i0+len(x))/fs_in) at
    arange(o0, o1)/fs_out, real and imaginary parts."""
    x = np.ascontiguousarray(x, np.complex128)
    out = np.empty(max(0, o1 - o0), np.complex128)
    if len(out):
        _lib.fe_interp(x.ctypes.data, len(x), int(i0), float(fs_in), float(fs_out), int(o0), int(o1),
                       out.ctypes.data)
    return out


_lib.fe_process.argtypes = [_P, _LL, _P, _P, _I, _LL, _LL, _D, _P, _I, _P, _LL, _LL, _P]
_lib.fe_process.restype = _LL


def process(x, hist, taps_rev, n_in0, n_out0, step, iq, lo_on, lo_table, lo_phase0, out):
    """rx_frontend._process, same arguments and (k_out, new_hist)."""
    x = np.ascontiguousarray(x, np.complex64)
    h = np.array(hist, np.complex64)              # a copy: replaced in place by the C++
    t = np.ascontiguousarray(taps_rev, np.float32)
    q = np.ascontiguousarray(iq, np.float64)
    tab = np.ascontiguousarray(lo_table, np.complex128)
    k = _lib.fe_process(x.ctypes.data, len(x), h.ctypes.data, t.ctypes.data, len(t), int(n_in0), int(n_out0),
                        float(step), q.ctypes.data, int(bool(lo_on)), tab.ctypes.data, len(tab), int(lo_phase0),
                        out.ctypes.data)
    return int(k), h


def install(ofdm):
    """Swap the C++-backed front end into the hf_ofdm_common module."""
    G = ofdm.GrowableComplexArray

    def linear_resample(x, fs_in, fs_out):
        n_in = len(x)
        dur = (n_in - 1) / fs_in
        n_out = int(round(dur * fs_out)) + 1
        return interp(x, 0, fs_in, fs_out, 0, n_out)

    def apply_lowpass_filter_incremental(x, taps, cache, trim_reference=None):
        numtaps = len(taps)
        prev_n = cache.get("n", 0)
        if prev_n == 0 or len(x) < prev_n:
            padded = np.concatenate([np.zeros(numtaps - 1, dtype=complex), x[:len(x)]])
            grown = G()
            grown.append(convolve_valid(padded, taps))
            cache["n"] = len(x)
            cache["grown"] = grown
            return grown
        grown = cache["grown"]
        if len(x) == prev_n:
            return grown
        new_x = x[prev_n:]
        history_start = max(0, prev_n - (numtaps - 1))
        history = x[history_start:prev_n]
        pad_needed = (numtaps - 1) - len(history)
        padded_new = np.concatenate([np.zeros(max(pad_needed, 0), dtype=complex), history, new_x])
        y_new = convolve_valid(padded_new, taps)
        if (trim_reference is not None and grown._buf is not None
                and len(grown._buf) > ofdm.INCREMENTAL_CACHE_HARD_CAP):
            grown.trim_before(trim_reference - ofdm.INCREMENTAL_CACHE_TRIM_MARGIN)
            cache["last_trim_at"] = trim_reference
        grown.append(y_new)
        cache["n"] = len(x)
        if trim_reference is not None:
            last_trim = cache.get("last_trim_at", 0)
            if trim_reference - last_trim >= ofdm.INCREMENTAL_CACHE_TRIM_INTERVAL:
                grown.trim_before(trim_reference - ofdm.INCREMENTAL_CACHE_TRIM_MARGIN)
                cache["last_trim_at"] = trim_reference
        return grown

    def stream_lowpass_filter(new_samples, taps, state):
        numtaps = len(taps)
        history = state.get("history")
        if history is None:
            history = np.zeros(numtaps - 1, dtype=complex)
        padded = np.concatenate([history, new_samples])
        y = convolve_valid(padded, taps)
        state["history"] = padded[-(numtaps - 1):] if numtaps > 1 else np.zeros(0, dtype=complex)
        return y

    def linear_resample_incremental(x, fs_in, fs_out, cache, trim_reference=None):
        n_in = len(x)
        prev_n_in = cache.get("n_in", 0)
        grown = cache.get("grown")
        if grown is None or n_in < prev_n_in:
            grown = G()
            grown.append(linear_resample(x[:n_in], fs_in, fs_out))
            cache["n_in"] = n_in
            cache["grown"] = grown
            return grown
        if n_in == prev_n_in:
            return grown
        n_out_new = int(round(((n_in - 1) / fs_in) * fs_out)) + 1
        n_out_prev = len(grown)
        if n_out_new <= n_out_prev:
            return grown[:n_out_new]
        margin = 4
        start = max(0, prev_n_in - margin)
        new = interp(x[start:], start, fs_in, fs_out, n_out_prev, n_out_new)
        if (trim_reference is not None and grown._buf is not None
                and len(grown._buf) > ofdm.INCREMENTAL_CACHE_HARD_CAP * fs_out / fs_in):
            grown.trim_before(int(trim_reference * fs_out / fs_in)
                              - int(ofdm.INCREMENTAL_CACHE_TRIM_MARGIN * fs_out / fs_in))
            cache["last_trim_at"] = trim_reference
        grown.append(new)
        cache["n_in"] = n_in
        if trim_reference is not None:
            last_trim = cache.get("last_trim_at", 0)
            if trim_reference - last_trim >= ofdm.INCREMENTAL_CACHE_TRIM_INTERVAL:
                grown.trim_before(int(trim_reference * fs_out / fs_in)
                                  - int(ofdm.INCREMENTAL_CACHE_TRIM_MARGIN * fs_out / fs_in))
                cache["last_trim_at"] = trim_reference
        return grown

    ofdm._iq_apply = iq_apply
    ofdm.linear_resample = linear_resample
    ofdm.apply_lowpass_filter_incremental = apply_lowpass_filter_incremental
    ofdm.stream_lowpass_filter = stream_lowpass_filter
    ofdm.linear_resample_incremental = linear_resample_incremental
