"""Run AVM's modem on the C++ kernels (the AVM library: avm.dll / libavm.so
beside this file, build.py): install() swaps them into hf_ofdm_common, hf_sc and hf_sch in place
of the Python versions, same names and signatures, so hf_ofdm_tx.py and
hf_ofdm_rx.py run unchanged on top. Switched on by AVM_CPP=1 (see the
hook at the top of hf_ofdm_rx.py / hf_ofdm_tx.py).

Swapped in:
  transmit   build_frame, reduce_papr, fft_resample_rate (complex64)
  OFDM RX    decode_frame (not --verbose: its debug prints are the
             Python's), schmidl_cox_sync, quick_sc_scan,
             apply_cfo_correction, refine_frame_start, resolve_integer_cfo,
             resolve_timing_offset
  front end  rx_frontend's kernel (IQ, LO shift, FIR + resample), and the
             --legacy-frontend chain (IQ transform, low-pass, resampling)
  S          _mf, detect, _sync, _derotated_mf, decode
  SH         _detect, _grid, _header, _decode
"""
import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_installed = False


def install():
    global _installed
    if _installed:
        return
    import hf_ofdm_common as ofdm
    import hf_sc
    import hf_sch
    import frontend_cpp
    import modem_rx_cpp as rx
    import sc_rx_cpp
    import sch_rx_cpp

    import modem_tx_cpp as tx
    py_decode_frame = ofdm.decode_frame
    py_fft_resample_rate = ofdm.fft_resample_rate

    def decode_frame(cfg, rx_after_preamble, verbose=False, max_wait_samples=None):
        if verbose or ofdm.single_carrier(cfg):
            return py_decode_frame(cfg, rx_after_preamble, verbose, max_wait_samples)
        return rx.decode_frame(cfg, rx_after_preamble, False, max_wait_samples)

    def build_frame(cfg, payload):
        return tx.build_frame(cfg, payload)

    def fft_resample_rate(x, fs_in, fs_out, cut_hz=None, taper_hz=0.0):
        if np.asarray(x).dtype == np.complex64:
            return tx.fft_resample_rate(x, fs_in, fs_out, cut_hz, taper_hz)
        return py_fft_resample_rate(x, fs_in, fs_out, cut_hz, taper_hz)

    ofdm.build_frame = build_frame
    ofdm.reduce_papr = tx.reduce_papr
    ofdm.fft_resample_rate = fft_resample_rate
    ofdm.decode_frame = decode_frame
    ofdm.schmidl_cox_sync = rx.schmidl_cox_sync
    ofdm.quick_sc_scan = rx.quick_sc_scan
    ofdm.apply_cfo_correction = rx.apply_cfo_correction
    ofdm.refine_frame_start = rx.refine_frame_start
    ofdm.resolve_integer_cfo = rx.resolve_integer_cfo
    ofdm.resolve_timing_offset = rx.resolve_timing_offset
    frontend_cpp.install(ofdm)            # the --legacy-frontend chain
    import rx_frontend                    # the default (streaming) front end's kernel
    rx_frontend._process = frontend_cpp.process

    # ---- mode S: hf_sc.receive_one_fragment unchanged, on these helpers
    def s_sync(x, i, cfo1):
        x = np.ascontiguousarray(x, np.complex128)
        t0, cfo, _ = sc_rx_cpp.sync(x, i, cfo1)
        zz = np.empty(len(x), np.complex128)
        sc_rx_cpp._lib.srx_zz_copy(sc_rx_cpp._h(), zz.ctypes.data)
        return t0, cfo, zz.astype(np.complex64)

    def s_derotated_mf(x, cfo):
        x = np.ascontiguousarray(x, np.complex128)
        out = np.empty(len(x), np.complex128)
        sc_rx_cpp._lib.srx_derotated_mf(sc_rx_cpp._h(), x.ctypes.data, len(x), float(cfo), out.ctypes.data)
        return out.astype(np.complex64)

    s_lib = sc_rx_cpp._lib
    s_lib.srx_zz_copy.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    s_lib.srx_decode_zz.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_longlong, ctypes.c_double,
                                    ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_double),
                                    ctypes.POINTER(ctypes.c_double)]
    s_lib.srx_decode_zz.restype = ctypes.c_longlong

    def s_decode(zz, t0, ref, n_bytes, modulation="qpsk"):
        h = sc_rx_cpp._h(modulation)
        z = np.ascontiguousarray(zz, np.complex128)
        ok, ber, evm = ctypes.c_int(), ctypes.c_double(), ctypes.c_double()
        n_d = s_lib.srx_decode_zz(h, z.ctypes.data, len(z), float(t0), int(n_bytes), ctypes.byref(ok),
                                  ctypes.byref(ber), ctypes.byref(evm))
        payload = np.empty(n_bytes, np.uint8)
        d = np.empty(n_d, np.complex128)
        s_lib.srx_decode_copy(h, payload.ctypes.data, d.ctypes.data)
        return payload.tobytes(), bool(ok.value), ber.value, evm.value, d.astype(np.complex64)

    hf_sc._mf = sc_rx_cpp.mf
    hf_sc.detect = sc_rx_cpp.detect
    hf_sc._sync = s_sync
    hf_sc._derotated_mf = s_derotated_mf
    hf_sc.decode = s_decode

    # ---- mode SH: hf_sch.receive_one_fragment unchanged, on these helpers
    h_lib = sch_rx_cpp._lib
    P, LL, D, I = ctypes.c_void_p, ctypes.c_longlong, ctypes.c_double, ctypes.c_int
    h_lib.shrx_grid.argtypes = [P, P, LL, D, D, LL, P]
    h_lib.shrx_header_r.argtypes = [P, P, LL, I, ctypes.POINTER(D)]
    h_lib.shrx_header_r.restype = I
    h_lib.shrx_decode_r.argtypes = [P, P, LL, I, I, ctypes.POINTER(I), ctypes.POINTER(D), ctypes.POINTER(D)]
    h_lib.shrx_decode_r.restype = LL
    current = {"cfg": None}

    def sh_handle():
        return sch_rx_cpp._h(current["cfg"])

    py_set = hf_sch._set

    def sh_set(cfg):
        current["cfg"] = cfg
        py_set(cfg)

    def sh_detect(x, cfg_rate=None):
        got = sch_rx_cpp.detect(current["cfg"], x)
        if got is None or got == "short":
            return got
        t0, cfo, v = got
        hf_sch._use(v)
        return t0, cfo

    def sh_grid(x, t0, cfo, n_sym):
        x = np.ascontiguousarray(x, np.complex128)
        out = np.empty(n_sym + hf_sch.L_TAPS, np.complex128)
        h_lib.shrx_grid(sh_handle(), x.ctypes.data, len(x), float(t0), float(cfo), int(n_sym), out.ctypes.data)
        return out.astype(np.complex64)

    def sh_header(r):
        r = np.ascontiguousarray(r, np.complex128)
        snr = D()
        n = h_lib.shrx_header_r(sh_handle(), r.ctypes.data, len(r), int(hf_sch.VARIANT), ctypes.byref(snr))
        hf_sch.PRE_SNR_DB = snr.value
        return (None if n < 0 else n), None, None

    def sh_decode(r, cfg, n_bytes, H, nv):
        h = sh_handle()
        r = np.ascontiguousarray(r, np.complex128)
        ok, ber, snr = I(), D(), D()
        n_d = h_lib.shrx_decode_r(h, r.ctypes.data, len(r), int(hf_sch.VARIANT), int(n_bytes), ctypes.byref(ok),
                                  ctypes.byref(ber), ctypes.byref(snr))
        hf_sch.PRE_SNR_DB = snr.value
        payload = np.empty(n_bytes, np.uint8)
        d = np.empty(n_d, np.complex128)
        h_lib.shrx_decode_copy(h, payload.ctypes.data, d.ctypes.data)
        return payload.tobytes(), bool(ok.value), ber.value, 100 * 10 ** (-snr.value / 20), d.astype(np.complex64)

    hf_sch._set = sh_set
    hf_sch._detect = sh_detect
    hf_sch._grid = sh_grid
    hf_sch._header = sh_header
    hf_sch._decode = sh_decode
    _installed = True


def install_if_enabled():
    """install() when AVM_CPP=1 (and the library is there)."""
    if os.environ.get("AVM_CPP", "0") in ("", "0"):
        return False
    install()
    print("AVM: modem running on the C++ kernels (AVM_CPP=1)", file=sys.stderr)
    return True
