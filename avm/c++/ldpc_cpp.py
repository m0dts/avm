"""hf_ofdm_ldpc's public functions backed by ldpc.cpp (in the AVM
library, _avmlib): same names, same results (codewords, decoded
bits, convergence). Needs no numba and no table cache file."""
import ctypes
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _LL = ctypes.c_void_p, ctypes.c_longlong
_lib.ldpc_coded_len.argtypes = [_LL]
_lib.ldpc_coded_len.restype = _LL
_lib.ldpc_encode.argtypes = [_P, _LL, _P]
_lib.ldpc_decode.argtypes = [_P, _LL, _LL, ctypes.c_int, ctypes.c_double, ctypes.c_int, _P, _P, _P]
_lib.ldpc_decode.restype = ctypes.c_int

LDPC_MAX_ITER = 100
LDPC_STALL_ITER = 30


def ldpc_block_params(msg_len_bits):
    if msg_len_bits <= 0:
        return 27, 648, 324, 0
    Z = 27 if msg_len_bits <= 324 else 54 if msg_len_bits <= 648 else 81
    K, N = 12 * Z, 24 * Z
    return Z, N, K, max(1, -(-msg_len_bits // K))


def ldpc_coded_len(msg_len_bits):
    return int(_lib.ldpc_coded_len(int(msg_len_bits)))


def ldpc_encode(bits):
    b = np.ascontiguousarray(bits, np.uint8)
    out = np.empty(ldpc_coded_len(len(b)), np.uint8)
    if len(b):
        _lib.ldpc_encode(b.ctypes.data, len(b), out.ctypes.data)
    return out


def _decode(llr, msg_len_bits, max_iter, alpha, want_cw):
    if msg_len_bits <= 0:
        return np.zeros(0, np.uint8), None
    llr = np.ascontiguousarray(llr, np.float64)
    need = ldpc_coded_len(msg_len_bits)
    if len(llr) < need:
        raise ValueError(f"Insufficient LLRs: need {need}, got {len(llr)}")
    bits = np.empty(msg_len_bits, np.uint8)
    cw = np.empty(need, np.uint8) if want_cw else None
    ok = _lib.ldpc_decode(llr.ctypes.data, len(llr), msg_len_bits, max_iter, alpha, LDPC_STALL_ITER,
                          bits.ctypes.data, cw.ctypes.data if want_cw else None, None)
    return bits, (cw if ok == 1 else None)


def ldpc_decode_soft(llr, msg_len_bits, max_iter=LDPC_MAX_ITER, alpha=0.85):
    return _decode(llr, msg_len_bits, max_iter, alpha, False)[0]


def ldpc_decode_soft_codeword(llr, msg_len_bits):
    return _decode(llr, msg_len_bits, LDPC_MAX_ITER, 0.85, True)
