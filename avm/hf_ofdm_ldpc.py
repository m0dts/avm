#!/usr/bin/env python3
"""
hf_ofdm_ldpc.py

Quasi-Cyclic Low-Density Parity-Check (QC-LDPC) forward error correction
for HF OFDM modem payloads.

Standard: IEEE 802.11n / 802.11ac rate-1/2 LDPC code specification.
Public domain construction, implemented from scratch using NumPy and Numba.

Features:
  - Rate-1/2 code with circulant sizes Z in {27, 54, 81} corresponding to
    codeword lengths N in {648, 1296, 1944} bits (info bits K in {324, 648, 972}).
  - Fast linear-time encoding via dual-diagonal parity check structure.
  - Multi-block chunking with zero-padding and LLR shortening for arbitrary
    payload lengths.
  - High-performance Normalized Min-Sum (NMS) soft-decision belief propagation
    decoder with early syndrome termination, JIT-compiled with Numba.
  - Typically provides ~1.5 to 2.5 dB coding gain over rate-1/2 K=7 convolutional
    code with soft Viterbi decoding in AWGN and HF multipath channels.
"""

import sys
import numpy as np

try:
    import numba
    _HAVE_NUMBA = True
except ImportError:
    _HAVE_NUMBA = False

# ---------------------------------------------------------------------------
# Rate 1/2 Prototype Base Matrix (12 x 24), 802.11n-style QC structure
# (dual-diagonal parity part). Info-part shifts were found by local search so
# the expanded H has NO 4-cycles at Z=27, 54 and 81 and every info column has
# degree >= 3. (The previous table had degree-1 columns and 243 four-cycles,
# which cost ~2 dB vs Viterbi instead of gaining ~2 dB.) NOT the literal
# 802.11n table -- both ends must use the same matrix.
# -1 denotes an all-zero Z x Z submatrix.
# Values >= 0 denote right circular shift of a Z x Z identity matrix.
# For Z=81, shift values are used directly.
# For Z in {27, 54}, standard 802.11n scaling applies: P(Z) = floor(P(81) * Z / 81).
# ---------------------------------------------------------------------------
_H_BG_81 = np.array([
    [20, -1, -1, -1, -1, -1, 19, 46, 75, -1, -1, -1,  1,  0, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1],
    [-1, -1, 41, -1, 24, -1, -1, 67, -1,  9, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1, -1, -1],
    [-1, 61, 47, -1, -1, 30, -1, -1, -1, 76, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1, -1],
    [58, -1, 76, -1, -1, -1, 10, -1, 16, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1],
    [80, -1, -1, -1, 54, -1, 73, -1, -1, -1, -1,  0, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1],
    [-1, 74, 18, -1, 24, -1, -1, -1, -1, 72, -1, 66, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1],
    [-1, -1, -1, 52, -1, -1, -1, 30,  8, -1, -1, -1,  0, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1],
    [-1, 19, 10, -1, -1, 17, -1, -1, -1, -1, 25, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1],
    [-1, 65, -1, 30, -1, 68, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1],
    [56, 60, -1, -1, 24, -1, -1, -1, -1, -1, 35, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1],
    [36, -1, -1, 35, -1, -1, 40, -1, -1, -1, -1, 56, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0],
    [24, -1, -1, 69, -1, 72, -1, -1, -1, -1, 58, -1,  1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0]
], dtype=int)


def _expand_h(h_bg: np.ndarray, z: int) -> np.ndarray:
    """Expand prototype base matrix into binary parity check matrix H."""
    m_blocks, n_blocks = h_bg.shape
    h = np.zeros((m_blocks * z, n_blocks * z), dtype=np.uint8)
    for i in range(m_blocks):
        for j in range(n_blocks):
            shift = h_bg[i, j]
            if shift >= 0:
                s = shift % z
                h[i * z:(i + 1) * z, j * z:(j + 1) * z] = np.roll(np.eye(z, dtype=np.uint8), s, axis=1)
    return h


def _gf2_inv(a: np.ndarray) -> np.ndarray:
    """Gaussian elimination over GF(2) to invert matrix A. The row
    elimination for each column is one vectorised XOR over all rows that
    have a 1 there (was a Python loop over every row, ~1M iterations at
    Z=81 -- a large part of this module's multi-second import time)."""
    n = a.shape[0]
    aug = np.hstack([a.copy(), np.eye(n, dtype=np.uint8)])
    for c in range(n):
        pivot = np.where(aug[c:, c])[0]
        if len(pivot) == 0:
            raise ValueError("Matrix is singular over GF(2)")
        r = c + pivot[0]
        if r != c:
            aug[[c, r]] = aug[[r, c]]
        rows = np.where(aug[:, c])[0]
        rows = rows[rows != c]
        if len(rows):
            aug[rows] ^= aug[c]
    return aug[:, n:]


# ---------------------------------------------------------------------------
# Precomputed Tables for Z in {27, 54, 81}
# ---------------------------------------------------------------------------
_LDPC_TABLES = {}

# The tables below never change, but building them took ~3 s at import on a
# PC (~12 s on a Raspberry Pi 4), delaying every TX/RX start. They're cached
# in a file next to this module, keyed by a hash of the base matrix (so a
# changed code can never load stale tables); if the file is missing, stale
# or unwritable, they're simply rebuilt (now ~10x faster than before too).
import hashlib as _hashlib
import os as _os

_CACHE_VERSION = 1
_CACHE_KEY = _hashlib.sha1(_H_BG_81.astype(np.int64).tobytes() + bytes([_CACHE_VERSION])).hexdigest()[:16]
_CACHE_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), f"_ldpc_tables_{_CACHE_KEY}.npz")


def _load_cached_g_parity():
    try:
        with np.load(_CACHE_PATH) as f:
            return {int(k[1:]): f[k] for k in f.files}
    except (OSError, ValueError, KeyError):
        return None


_CACHED_G = _load_cached_g_parity()
_NEW_G = {}

for _Z in (27, 54, 81):
    _h_bg = np.floor(_H_BG_81 * (_Z / 81.0)).astype(int)
    _h_bg[_H_BG_81 == -1] = -1
    _H = _expand_h(_h_bg, _Z)
    _M, _N = _H.shape
    _K = _N - _M

    # Parity generator: HB * p = HA * u (mod 2) -> p = (HB^-1 * HA) * u (mod 2)
    if _CACHED_G is not None and _Z in _CACHED_G and _CACHED_G[_Z].shape == (_M, _K):
        _G_parity = _CACHED_G[_Z]
    else:
        _HA = _H[:, :_K]
        _HB = _H[:, _K:]
        _HB_inv = _gf2_inv(_HB)
        # float32 product (BLAS): exact, every sum is at most K <= 972 --
        # the uint8 version ran as plain loops, ~1e9 operations at Z=81
        _G_parity = ((_HB_inv.astype(np.float32) @ _HA.astype(np.float32)) % 2).astype(np.uint8)
        _NEW_G[_Z] = _G_parity
    
    # Graph edge representation for fast Min-Sum traversal
    _rows, _cols = np.where(_H)
    _n_edges = len(_rows)
    
    _check_edges_list = [[] for _ in range(_M)]
    _var_edges_list = [[] for _ in range(_N)]
    for _e in range(_n_edges):
        _check_edges_list[_rows[_e]].append(_e)
        _var_edges_list[_cols[_e]].append(_e)
        
    _max_dc = max(len(_l) for _l in _check_edges_list)
    _max_dv = max(len(_l) for _l in _var_edges_list)
    
    _check_edges = np.full((_M, _max_dc), -1, dtype=np.int32)
    _check_deg = np.zeros(_M, dtype=np.int32)
    for _i in range(_M):
        _l = _check_edges_list[_i]
        _check_deg[_i] = len(_l)
        _check_edges[_i, :len(_l)] = _l
        
    _var_edges = np.full((_N, _max_dv), -1, dtype=np.int32)
    _var_deg = np.zeros(_N, dtype=np.int32)
    for _j in range(_N):
        _l = _var_edges_list[_j]
        _var_deg[_j] = len(_l)
        _var_edges[_j, :len(_l)] = _l
        
    _edge_var = _cols.astype(np.int32)
    _edge_check = _rows.astype(np.int32)
    
    _LDPC_TABLES[_Z] = {
        "H": _H,
        "G_parity": _G_parity,
        "M": _M,
        "N": _N,
        "K": _K,
        "check_edges": _check_edges,
        "check_deg": _check_deg,
        "var_edges": _var_edges,
        "var_deg": _var_deg,
        "edge_var": _edge_var,
        "edge_check": _edge_check,
    }

if _NEW_G:
    try:
        _tmp = _CACHE_PATH + f".{_os.getpid()}.tmp.npz"
        np.savez(_tmp, **{f"z{z}": g for z, g in
                          {**(_CACHED_G or {}), **_NEW_G}.items()})
        _os.replace(_tmp, _CACHE_PATH)  # atomic: concurrent starts never see a half-written file
    except OSError:
        pass  # read-only install etc.: just rebuild next time


# ---------------------------------------------------------------------------
# Min-Sum Belief Propagation Decoder
# ---------------------------------------------------------------------------
if _HAVE_NUMBA:
    @numba.njit(cache=True)
    def _min_sum_decode_numba(llr, check_edges, check_deg, var_edges, var_deg,
                              edge_var, edge_check, max_iter, alpha, stall_iter):
        M = check_edges.shape[0]
        N = var_edges.shape[0]
        n_edges = edge_var.shape[0]

        # Variable-to-check messages initialized with channel LLRs
        v2c = np.empty(n_edges, dtype=np.float64)
        for e in range(n_edges):
            v2c[e] = llr[edge_var[e]]
        c2v = np.zeros(n_edges, dtype=np.float64)

        dec = np.zeros(N, dtype=np.uint8)
        best_unsat = M + 1
        stall = 0

        for it in range(max_iter):
            # 1. Check-node update: find min1, min2, and overall sign product
            for i in range(M):
                deg = check_deg[i]
                min1 = 1e9
                min2 = 1e9
                min1_idx = -1
                prod_sign = 1
                for k in range(deg):
                    e = check_edges[i, k]
                    val = v2c[e]
                    s = 1 if val >= 0 else -1
                    prod_sign *= s
                    a = abs(val)
                    if a < min1:
                        min2 = min1
                        min1 = a
                        min1_idx = k
                    elif a < min2:
                        min2 = a
                
                # Check-to-variable message calculation with normalization factor alpha
                for k in range(deg):
                    e = check_edges[i, k]
                    val = v2c[e]
                    s = 1 if val >= 0 else -1
                    other_sign = prod_sign * s
                    m = min2 if k == min1_idx else min1
                    c2v[e] = other_sign * m * alpha
            
            # 2. Variable-node update: accumulate incoming check messages
            for j in range(N):
                deg = var_deg[j]
                tot = llr[j]
                for k in range(deg):
                    e = var_edges[j, k]
                    tot += c2v[e]
                dec[j] = 0 if tot >= 0 else 1
                for k in range(deg):
                    e = var_edges[j, k]
                    v2c[e] = tot - c2v[e]
            
            # 3. Early syndrome termination: check if H * dec == 0 (mod 2)
            unsat = 0
            for i in range(M):
                deg = check_deg[i]
                s = 0
                for k in range(deg):
                    e = check_edges[i, k]
                    s ^= dec[edge_var[e]]
                unsat += s
            if unsat == 0:
                return dec, it + 1, True
            # 4. Give up on a block that has stopped converging: no new
            # lowest unsatisfied-check count for stall_iter iterations.
            if unsat < best_unsat:
                best_unsat = unsat
                stall = 0
            else:
                stall += 1
                if stall >= stall_iter:
                    return dec, it + 1, False

        return dec, max_iter, False

    @numba.njit(cache=True)
    def _layered_min_sum_numba(llr, check_edges, check_deg, edge_var, max_iter, alpha, stall_iter, post, c2v):
        """Row-layered (serial-C) normalized min-sum: each check row is
        updated in turn against the bit posteriors the rows before it in the
        same iteration already refreshed, instead of all rows from the
        previous iteration's messages (flooding) -- typically converges in
        about half the iterations. post (length N) and c2v (one per edge)
        are work arrays whose dtype sets the message precision (float32
        lets ARM NEON do twice as much per instruction). Same returns, early
        syndrome stop and stall abandonment as _min_sum_decode_numba."""
        M = check_edges.shape[0]
        N = post.shape[0]
        for j in range(N):
            post[j] = llr[j]
        for e in range(c2v.shape[0]):
            c2v[e] = 0.0
        dec = np.zeros(N, dtype=np.uint8)
        a = post.dtype.type(alpha)
        best_unsat = M + 1
        stall = 0
        t = np.empty(check_edges.shape[1], dtype=post.dtype)
        for it in range(max_iter):
            for i in range(M):
                deg = check_deg[i]
                min1 = post.dtype.type(1e30)
                min2 = post.dtype.type(1e30)
                min1_idx = -1
                neg = 0
                for k in range(deg):
                    e = check_edges[i, k]
                    v = post[edge_var[e]] - c2v[e]
                    t[k] = v
                    if v < 0:
                        neg ^= 1
                    av = abs(v)
                    if av < min1:
                        min2 = min1
                        min1 = av
                        min1_idx = k
                    elif av < min2:
                        min2 = av
                for k in range(deg):
                    e = check_edges[i, k]
                    v = t[k]
                    m = (min2 if k == min1_idx else min1) * a
                    # sign of the product of all OTHER inputs
                    s = neg ^ (1 if v < 0 else 0)
                    new = -m if s else m
                    c2v[e] = new
                    post[edge_var[e]] = v + new
            for j in range(N):
                dec[j] = 0 if post[j] >= 0 else 1
            unsat = 0
            for i in range(M):
                deg = check_deg[i]
                s = 0
                for k in range(deg):
                    s ^= dec[edge_var[check_edges[i, k]]]
                unsat += s
            if unsat == 0:
                return dec, it + 1, True
            if unsat < best_unsat:
                best_unsat = unsat
                stall = 0
            else:
                stall += 1
                if stall >= stall_iter:
                    return dec, it + 1, False
        return dec, max_iter, False

    # JIT warm-up at import time
    _t_warm = _LDPC_TABLES[27]
    _min_sum_decode_numba(
        np.zeros(_t_warm["N"], dtype=np.float64),
        _t_warm["check_edges"], _t_warm["check_deg"],
        _t_warm["var_edges"], _t_warm["var_deg"],
        _t_warm["edge_var"], _t_warm["edge_check"],
        2, 0.75, 2
    )
    _layered_min_sum_numba(
        np.zeros(_t_warm["N"], dtype=np.float64),
        _t_warm["check_edges"], _t_warm["check_deg"], _t_warm["edge_var"],
        2, 0.75, 2, np.empty(_t_warm["N"], np.float32), np.empty(len(_t_warm["edge_var"]), np.float32)
    )


def _min_sum_decode_py(llr, check_edges, check_deg, var_edges, var_deg,
                       edge_var, edge_check, max_iter, alpha, stall_iter):
    """Pure-Python fallback for Normalized Min-Sum decoding."""
    M = check_edges.shape[0]
    N = var_edges.shape[0]
    n_edges = edge_var.shape[0]

    v2c = np.empty(n_edges, dtype=np.float64)
    for e in range(n_edges):
        v2c[e] = llr[edge_var[e]]
    c2v = np.zeros(n_edges, dtype=np.float64)

    dec = np.zeros(N, dtype=np.uint8)
    best_unsat = M + 1
    stall = 0
    for it in range(max_iter):
        for i in range(M):
            deg = check_deg[i]
            min1 = 1e9
            min2 = 1e9
            min1_idx = -1
            prod_sign = 1
            for k in range(deg):
                e = check_edges[i, k]
                val = v2c[e]
                s = 1 if val >= 0 else -1
                prod_sign *= s
                a = abs(val)
                if a < min1:
                    min2 = min1
                    min1 = a
                    min1_idx = k
                elif a < min2:
                    min2 = a
            for k in range(deg):
                e = check_edges[i, k]
                val = v2c[e]
                s = 1 if val >= 0 else -1
                other_sign = prod_sign * s
                m = min2 if k == min1_idx else min1
                c2v[e] = other_sign * m * alpha
        for j in range(N):
            deg = var_deg[j]
            tot = llr[j]
            for k in range(deg):
                e = var_edges[j, k]
                tot += c2v[e]
            dec[j] = 0 if tot >= 0 else 1
            for k in range(deg):
                e = var_edges[j, k]
                v2c[e] = tot - c2v[e]
        unsat = 0
        for i in range(M):
            deg = check_deg[i]
            s = 0
            for k in range(deg):
                e = check_edges[i, k]
                s ^= dec[edge_var[e]]
            unsat += s
        if unsat == 0:
            return dec, it + 1, True
        if unsat < best_unsat:
            best_unsat = unsat
            stall = 0
        else:
            stall += 1
            if stall >= stall_iter:
                return dec, it + 1, False
    return dec, max_iter, False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ldpc_block_params(msg_len_bits: int):
    """Determine block size Z, codeword length N, info length K, and block count
    for a message of length msg_len_bits.
    
    Uses standard IEEE 802.11n rate-1/2 block sizing:
      - msg_len_bits <= 324:  Z = 27 (N = 648,  K = 324)
      - msg_len_bits <= 648:  Z = 54 (N = 1296, K = 648)
      - msg_len_bits > 648:   Z = 81 (N = 1944, K = 972)
    """
    if msg_len_bits <= 0:
        return 27, 648, 324, 0
    if msg_len_bits <= 324:
        Z = 27
    elif msg_len_bits <= 648:
        Z = 54
    else:
        Z = 81
    K = 12 * Z
    N = 24 * Z
    n_blocks = max(1, -(-msg_len_bits // K))  # ceil div
    return Z, N, K, n_blocks


def ldpc_coded_len(msg_len_bits: int) -> int:
    """Total coded bits produced by ldpc_encode for a message of msg_len_bits."""
    if msg_len_bits <= 0:
        return 0
    _, N, _, n_blocks = ldpc_block_params(msg_len_bits)
    return n_blocks * N


def ldpc_encode_block(u: np.ndarray, Z: int = 81) -> np.ndarray:
    """Encode a single information block u of length K = 12*Z into codeword of length N = 24*Z."""
    table = _LDPC_TABLES[Z]
    K = table["K"]
    if len(u) != K:
        raise ValueError(f"Expected info block length {K}, got {len(u)}")
    p = (table["G_parity"] @ u) % 2
    return np.concatenate([u, p]).astype(np.uint8)


def ldpc_encode(bits: np.ndarray) -> np.ndarray:
    """Encode arbitrary-length bit sequence into concatenated LDPC codewords.
    Zero-pads the final block if its length does not divide K evenly.
    Output length is ldpc_coded_len(len(bits)).
    """
    bits = np.asarray(bits, dtype=np.uint8)
    if len(bits) == 0:
        return np.zeros(0, dtype=np.uint8)
    
    Z, N, K, n_blocks = ldpc_block_params(len(bits))
    padded_len = n_blocks * K
    if len(bits) < padded_len:
        padded = np.concatenate([bits, np.zeros(padded_len - len(bits), dtype=np.uint8)])
    else:
        padded = bits
        
    # All blocks in one float32 matrix product (BLAS): exact, since every
    # sum is at most K <= 972 ones, far inside float32's exact-integer
    # range -- and ~20x faster than one uint8 product per block, which
    # numpy can't hand to BLAS (the receiver re-encodes every decoded
    # fragment for its BER figure, so this was ~20 ms per fragment).
    table = _LDPC_TABLES[Z]
    g = table.get("G_parity_f32")
    if g is None:
        g = table["G_parity_f32"] = table["G_parity"].astype(np.float32)
    u = padded.reshape(n_blocks, K)
    p = (u.astype(np.float32) @ g.T) % 2
    return np.concatenate([u, p.astype(np.uint8)], axis=1).ravel()


# Default iteration cap. Measured at 1.8 dB SNR (200 frames): 50 iterations
# decode ~66% of fragments, 100 iterations ~77%, 200 iterations ~80%. Blocks
# that converge still stop early (syndrome check), so only blocks at the
# edge of the waterfall pay for the extra iterations; 100 keeps most of the
# gain at half the worst-case cost of 200. alpha (0.80-0.90) made no
# measurable difference.
LDPC_MAX_ITER = 100

# A block that hasn't set a new low in unsatisfied parity checks for this
# many iterations is abandoned. Blocks that never converge used to burn the
# full LDPC_MAX_ITER every time -- on a bad fragment that was most of the
# receiver's compute, enough to push it past real time (and on a Pi, to
# drop the next good fragment too). Measured on the multipath regression
# captures: 30 keeps every fragment at 6 dB (LDPC time 0.70 -> 0.39 s) and
# loses 1 of 6 at a very marginal 3 dB (0.69 -> 0.42 s); 12 lost 2 there.
LDPC_STALL_ITER = 30

# Row-layered min-sum with float32 messages (see _layered_min_sum_numba)
# instead of flooding with float64. Measured, rate 1/2 Z=81, BPSK/AWGN, same
# alpha 0.85: equal or slightly FEWER block errors (1.0 dB: 67 vs 74 of 150;
# 1.5 dB: 5 vs 6) at about half the iterations, and per block on a
# Raspberry Pi 4 1.3-0.43 ms instead of 5.4-1.3 ms (1.5-3.5 dB) -- 2.7-3.6x
# faster. False restores flooding.
LDPC_LAYERED = True
LDPC_MSG_DTYPE = np.float32


def ldpc_decode_block(llr: np.ndarray, Z: int = 81, max_iter: int = LDPC_MAX_ITER, alpha: float = 0.85,
                      stall_iter: int = None):
    """Soft-decision Normalized Min-Sum decode of a single block of length N = 24*Z.
    Positive LLR = likely 0, negative LLR = likely 1.
    Returns (decoded_bits, iterations, converged).
    """
    table = _LDPC_TABLES[Z]
    llr_arr = np.ascontiguousarray(llr, dtype=np.float64)
    if stall_iter is None:
        stall_iter = LDPC_STALL_ITER

    if _HAVE_NUMBA and LDPC_LAYERED:
        work = table.get("_layered_work")
        if work is None:  # reused across calls (one decoder per process/thread)
            work = table["_layered_work"] = (np.empty(table["N"], LDPC_MSG_DTYPE),
                                             np.empty(len(table["edge_var"]), LDPC_MSG_DTYPE))
        return _layered_min_sum_numba(llr_arr, table["check_edges"], table["check_deg"], table["edge_var"],
                                      max_iter, alpha, stall_iter, work[0], work[1])
    if _HAVE_NUMBA:
        return _min_sum_decode_numba(
            llr_arr,
            table["check_edges"], table["check_deg"],
            table["var_edges"], table["var_deg"],
            table["edge_var"], table["edge_check"],
            max_iter, alpha, stall_iter
        )
    return _min_sum_decode_py(
        llr_arr,
        table["check_edges"], table["check_deg"],
        table["var_edges"], table["var_deg"],
        table["edge_var"], table["edge_check"],
        max_iter, alpha, stall_iter
    )


def ldpc_decode_soft_codeword(llr: np.ndarray, msg_len_bits: int):
    """ldpc_decode_soft, plus the full decoded codewords concatenated
    (exactly ldpc_encode(decoded bits)) when EVERY block converged, else
    None. Lets a caller that wants the re-encoded codeword (the receiver's
    BER readout) skip re-encoding on good fragments; a converged block's
    decision is a valid codeword with these info bits by definition."""
    return ldpc_decode_soft(llr, msg_len_bits, _codeword=True)


def ldpc_decode_soft(llr: np.ndarray, msg_len_bits: int, max_iter: int = LDPC_MAX_ITER, alpha: float = 0.85,
                     _codeword=False) -> np.ndarray:
    """Soft-decision decode concatenated LDPC blocks.
    
    llr: array of soft channel values (positive = likely 0, negative = likely 1)
         matching the order of ldpc_encode. Length must be >= ldpc_coded_len(msg_len_bits).
    msg_len_bits: exact original unpadded payload length in bits.
    
    Returns decoded information bits of length msg_len_bits.
    """
    if msg_len_bits <= 0:
        return np.zeros(0, dtype=np.uint8)
        
    Z, N, K, n_blocks = ldpc_block_params(msg_len_bits)
    expected_coded = n_blocks * N
    if len(llr) < expected_coded:
        raise ValueError(f"Insufficient LLRs: need {expected_coded}, got {len(llr)}")
        
    out_bits = []
    codewords = []
    all_ok = True
    # Shortening prior: known zero-padded bit positions in the last block
    # are set to a high positive confidence (+100.0)
    last_block_valid_info = msg_len_bits - (n_blocks - 1) * K

    for b in range(n_blocks):
        block_llr = llr[b * N:(b + 1) * N].copy()
        if b == n_blocks - 1 and last_block_valid_info < K:
            # Set padded bits to known 0 with high certainty
            block_llr[last_block_valid_info:K] = 100.0

        dec, it, ok = ldpc_decode_block(block_llr, Z=Z, max_iter=max_iter, alpha=alpha)
        info_bits = dec[:K]
        out_bits.append(info_bits)
        codewords.append(dec)
        all_ok = all_ok and ok

    all_info = np.concatenate(out_bits)[:msg_len_bits]
    if _codeword:
        # a padded info bit decoded as 1 isn't what ldpc_encode would give
        # (it pads with zeros), so treat that like a non-converged block
        if all_ok and last_block_valid_info < K and codewords[-1][last_block_valid_info:K].any():
            all_ok = False
        return all_info, (np.concatenate(codewords) if all_ok else None)
    return all_info


def self_test():
    """Run verification self-tests on LDPC encoder and decoder."""
    print("Running LDPC self-test...")
    # Test all block sizes: Z in {27, 54, 81}
    for Z in (27, 54, 81):
        table = _LDPC_TABLES[Z]
        K = table["K"]
        N = table["N"]
        u = np.random.randint(0, 2, K, dtype=np.uint8)
        c = ldpc_encode_block(u, Z=Z)
        syndrome = (table["H"] @ c) % 2
        assert np.sum(syndrome) == 0, f"Z={Z} syndrome check failed!"
        
        # Test decoder on noisy LLR
        llr = (1.0 - 2.0 * c) + 0.5 * np.random.randn(N)
        dec, it, ok = ldpc_decode_block(llr, Z=Z)
        assert np.array_equal(dec[:K], u), f"Z={Z} decode failed to recover information bits!"
        
    # Test arbitrary multi-block payload
    test_lengths = [1, 10, 324, 325, 648, 649, 1024, 8192]
    for length in test_lengths:
        bits = np.random.randint(0, 2, length, dtype=np.uint8)
        coded = ldpc_encode(bits)
        assert len(coded) == ldpc_coded_len(length)
        # BPSK LLR with moderate noise
        llr = (1.0 - 2.0 * coded) + 0.4 * np.random.randn(len(coded))
        recovered = ldpc_decode_soft(llr, length)
        assert np.array_equal(recovered, bits), f"Multi-block decode failed for length={length}!"
        
    print("LDPC self-test PASSED successfully.")


if __name__ == "__main__":
    self_test()

