"""
Shared code for the HF OFDM tx/rx pair: DRM-exact carrier ranges and
scattered pilot geometry, QPSK mapping, OFDM mod/demod, a Schmidl & Cox
preamble for frame synchronization, and simple byte framing with a
length + CRC32 header.

See hf_ofdm_tx.py / hf_ofdm_rx.py for the actual stdin/stdout tools.

--------------------------------------------------------------------
Carrier ranges and pilot geometry sourced from the Dream DRM project
(github.com/Drm-tools/dream, GPL-2.0), src/tables/TableCarMap.cpp:
    iTableCarrierKmin / iTableCarrierKmax  -> exact carrier index ranges
    RM*_SCAT_PIL_FREQ_INT / TIME_INT       -> scattered pilot spacing
Row/column order confirmed against the ESpecOcc enum and
GetNominalBandwidth() in src/Parameter.cpp (SO_0=4.5kHz ... SO_5=20kHz)
and cross-checked against independently-sourced carrier counts (410 for
Mode B and 178 for Mode D at 20 kHz) before use. Values are reproduced
here as plain data (not copied code), so this file isn't a derivative
of Dream's GPL-licensed source -- but if you pull actual algorithm code
from Dream into this project later, that code will carry GPL-2.0 terms.
--------------------------------------------------------------------
"""

import sys
import zlib
from dataclasses import dataclass

import numpy as np

import hf_ofdm_ldpc as ldpc

try:
    import numba
    from numba import njit
    _HAVE_NUMBA = True
except ImportError:
    _HAVE_NUMBA = False

    def njit(*args, **kwargs):  # plain-Python fallback: same results, just slower
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        return lambda f: f


class ChunkList:
    """The `chunks` list every live reader (StdinReader, PlutoRxSource,
    FrontEndReader) appends received bytes to, with the SAME absolute
    indexing a plain list has -- len() counts every chunk ever appended and
    slices are by that absolute index -- but whose consumer can discard what
    it has already processed. A plain list kept every chunk for the whole
    session: on a Raspberry Pi 4 (2 GB) the raw 550 kS/s Pluto stream alone
    grew ~4.4 MB/s, filled RAM and swap within minutes and slowed every
    process on the box. Only slices are supported, which is all any caller
    uses (chunks[n:], chunks[-64:])."""

    KEEP_TAIL = 64  # recent chunks always kept, for _peek_latest_samples' chunks[-64:]

    def __init__(self):
        self._items = []
        self._origin = 0  # absolute index of self._items[0]

    def append(self, chunk):
        self._items.append(chunk)

    def __len__(self):
        return self._origin + len(self._items)

    def __getitem__(self, key):
        if not isinstance(key, slice) or key.step not in (None, 1):
            raise TypeError("ChunkList only supports contiguous slices")
        start, stop, _ = key.indices(len(self))
        return self._items[max(start - self._origin, 0):max(stop - self._origin, 0)]

    def discard_before(self, index):
        """Drops chunks before absolute index `index` (the consumer's own
        next-unread position), except the last KEEP_TAIL. Call with the
        owning reader's _lock held."""
        cut = min(index, len(self) - self.KEEP_TAIL) - self._origin
        if cut > 0:
            del self._items[:cut]
            self._origin += cut


class GrowableComplexArray:
    """Amortized O(1) append, like a dynamic array/list doubling its
    capacity, instead of the naive `np.concatenate([prev, new])` every
    incremental cache in this file used to do -- that reallocates and
    copies the ENTIRE cumulative array on every single call, which grows
    with total session length even once the actual math feeding it
    (interpolation, convolution, statistics) is already correctly
    incremental. Confirmed for real: on a multi-minute live hardware
    capture, per-fragment compute kept climbing (0.39s -> 0.81s and
    higher) purely from this copy cost, well after the math itself had
    been fixed to only touch new samples. Doubling capacity instead of
    growing to the exact new size turns a full copy into an O(log n)-
    amortized-total cost across the whole session, without touching a
    single absolute sample index anywhere else -- unlike trimming old
    data from the front, which would need every position (start_index,
    search_start, etc.) rebased against the trim point, and would shift
    linear_resample's position-dependent time grid the same way trimming
    before resampling already proved unsafe once this session (see
    linear_resample's own docstring)."""

    def __init__(self, dtype=complex):
        self._dtype = dtype  # the receiver's raw sample buffer uses complex64
        self._buf = None
        self._len = 0
        self._origin = 0  # logical index that physical position 0 corresponds
        # to -- 0 for every caller that never calls trim_before() (i.e.
        # every existing use of this class before RX-side buffer trimming
        # was added), so __len__/array below are completely unchanged for
        # them. See trim_before()'s docstring for the one caller that does
        # use this (the RX-side raw sample buffer, the single biggest one
        # by far since it's at the native SDR rate rather than cfg.fs).

    def append(self, new_part):
        n_new = len(new_part)
        if n_new == 0:
            return
        if self._buf is None:
            self._buf = np.empty(max(n_new, 4096), dtype=self._dtype)
        needed = self._len + n_new
        if needed > len(self._buf):
            new_cap = max(len(self._buf) * 2, needed)
            grown = np.empty(new_cap, dtype=self._dtype)
            grown[:self._len] = self._buf[:self._len]
            self._buf = grown
        self._buf[self._len:needed] = new_part
        self._len = needed

    @property
    def array(self):
        return self._buf[:self._len] if self._buf is not None else np.zeros(0, dtype=self._dtype)

    @property
    def real(self):
        return self.array.real

    @property
    def imag(self):
        return self.array.imag

    def __array__(self, dtype=None):
        return self.array if dtype is None else self.array.astype(dtype)

    def __len__(self):
        return self._origin + self._len

    def __getitem__(self, key):
        """LOGICAL slicing -- key values are absolute sample indices since
        the start of the session (the same convention every existing
        caller like schmidl_cox_sync already uses), translated internally
        to wherever that data actually lives physically after trimming.
        Only slices are supported (every real caller already only ever
        does rx[a:b] / rx[a:] -- confirmed by reading every access site
        before adding this) -- not single-index access, which nothing
        needs."""
        if not isinstance(key, slice):
            raise TypeError("GrowableComplexArray only supports slice indexing")
        start = key.start if key.start is not None else self._origin
        stop = key.stop if key.stop is not None else len(self)
        phys_start = start - self._origin
        phys_stop = stop - self._origin
        if phys_start < 0 or phys_stop < 0:
            raise IndexError(f"GrowableComplexArray: requested logical range "
                              f"[{start}:{stop}] starts before the retained "
                              f"origin {self._origin} -- that data has already "
                              f"been trimmed (see trim_before()).")
        return self.array[phys_start:phys_stop]

    def trim_before(self, logical_index):
        """Discards physically-stored samples before `logical_index` (an
        absolute sample index using the SAME convention as every existing
        caller's own indices, e.g. hf_ofdm_rx.py's native_search_start) --
        keeps this array's memory (and the cost of its own occasional
        doubling-copy) bounded for a live, indefinitely-running session
        instead of growing forever. __len__/__getitem__ above keep
        reporting/accepting the same LOGICAL indices as before trimming,
        so every existing caller (schmidl_cox_sync, apply_cfo_correction's
        call sites, etc.) needs zero changes -- they were already only
        ever doing len(rx) and rx[a:b]/rx[a:], never direct arithmetic on
        the whole array, confirmed by reading every access site in
        hf_ofdm_rx.py before adding this. Only called on the RX-side raw
        sample buffer specifically (see get_rx_raw_incremental) -- the
        IQ-imbalance/lowpass-filter/resample caches downstream of it each
        keep their own SEPARATE, never-trimmed accumulator (already far
        smaller once resampling is in play, since they run at cfg.fs
        instead of the native SDR rate), so nothing about their own
        indexing changes either.

        Naively trimming the front of a live buffer already broke
        linear_resample once (see its docstring): its output timing is
        computed FROM the input's own length, so if you trim without
        keeping that length meaning "true count since the start", every
        output timestamp after the trim point silently shifts by a
        fraction of a sample. That's exactly what the LOGICAL vs
        PHYSICAL distinction here avoids -- length and indices keep
        meaning what they always meant; only the physical storage
        shrinks."""
        phys_cut = logical_index - self._origin
        if phys_cut <= 0:
            return
        phys_cut = min(phys_cut, self._len)
        remaining = self._len - phys_cut
        # Reallocate to a right-sized buffer rather than shifting within
        # the existing (possibly much larger) one -- otherwise the huge
        # allocation from before trimming started never actually gets
        # freed, it just sits mostly empty forever. append()'s own
        # doubling naturally regrows this as needed, same as a fresh
        # array would.
        new_buf = np.empty(max(remaining * 2, 4096), dtype=self._dtype)
        if remaining > 0:
            new_buf[:remaining] = self._buf[phys_cut:self._len]
        self._buf = new_buf
        self._len = remaining
        self._origin += phys_cut

# ---------------------------------------------------------------------------
# DRM mode table: carrier spacing, guard/useful fraction, and scattered
# pilot geometry (frequency spacing, time period) from ETSI ES 201 980.
# ---------------------------------------------------------------------------
DRM_MODES = {
    "A": {"spacing_hz": 125 / 3,  "guard_fraction": 1 / 9,  "freq_int": 4, "time_int": 5,
          "note": "ground wave, benign"},
    "B": {"spacing_hz": 375 / 8,  "guard_fraction": 1 / 4,  "freq_int": 2, "time_int": 3,
          "note": "HF sky wave, general purpose"},
    "C": {"spacing_hz": 750 / 11, "guard_fraction": 4 / 11, "freq_int": 2, "time_int": 2,
          "note": "HF, long distance, more Doppler margin"},
    "D": {"spacing_hz": 750 / 7,  "guard_fraction": 11 / 14, "freq_int": 1, "time_int": 3,
          "note": "HF, severe delay + Doppler spread"},
    # Not DRM: VHF/UHF (144/432 MHz) mobile and troposcatter. Carrier spacing
    # set by Doppler -- 52 Hz at 432 MHz and 130 km/h is 2% of 2.5 kHz
    # (inter-carrier interference ~-28 dB); the HF modes' 42-107 Hz spacing
    # can't survive it. Guard 1/12 = 33 us covers hilly-terrain and
    # troposcatter delay spreads (HF modes spend 3-15 ms there). A +-1.1 kHz
    # oscillator error at 432 MHz stays within half a carrier, so no integer
    # CFO search. See sim_hf_channel.py's VHF/UHF channel presets.
    # preamble_halves 32 (was 16): with 16, the short (3.2 ms) preamble was
    # the weak point -- on AWGN at 80 kHz, 2-3 in 24 fragments went
    # undetected at 2.2-3 dB SNR where mode A found all 24, ~1 dB worse on
    # air. 32 (6.4 ms) detects all 24 from 2.2 dB, matching A, for ~3 ms
    # more airtime per fragment. Changes VU's on-air format: both ends must
    # use the same value.
    "VU": {"spacing_hz": 2500.0, "guard_fraction": 1 / 12, "freq_int": 4, "time_int": 4, "preamble_halves": 32,
          "min_occupancy_khz": 80,  # 20/40 kHz (8/15 carriers) failed to decode even on a clean channel
          "note": "VHF/UHF mobile + troposcatter (not DRM)"},
}
# Experiments only (sim_hf_channel.py sweeps): override mode VU's pilot comb /
# guard without editing the table. Both ends must agree.
import os as _os  # noqa: E402
if _os.environ.get("HF_MODE_VU_FREQ_INT"):
    DRM_MODES["VU"]["freq_int"] = int(_os.environ["HF_MODE_VU_FREQ_INT"])
if _os.environ.get("HF_MODE_VU_GUARD"):
    DRM_MODES["VU"]["guard_fraction"] = float(_os.environ["HF_MODE_VU_GUARD"])
if _os.environ.get("HF_MODE_VU_PREAMBLE_HALVES"):  # experiment: sync sensitivity vs airtime
    DRM_MODES["VU"]["preamble_halves"] = int(_os.environ["HF_MODE_VU_PREAMBLE_HALVES"])

# Exact carrier index ranges [Kmin, Kmax] by (mode, occupancy_khz), from
# Dream's iTableCarrierKmin/Kmax (6 occupancies x 4 modes; rows in the
# order 4.5, 5, 9, 10, 18, 20 kHz; only nonzero entries kept here).
_OCCUPANCIES = [4.5, 5, 9, 10, 18, 20]
_KMIN_TABLE = {  # mode -> {occupancy: kmin}
    "A": {4.5: 2,    5: 2,    9: -102, 10: -114, 18: -98,  20: -110},
    "B": {4.5: 1,    5: 1,    9: -91,  10: -103, 18: -87,  20: -99},
    "C": {10: -69,  20: -67},
    "D": {10: -44,  20: -43},
}
_KMAX_TABLE = {
    "A": {4.5: 102,  5: 114,  9: 102,  10: 114,  18: 314,  20: 350},
    "B": {4.5: 91,   5: 103,  9: 91,   10: 103,  18: 279,  20: 311},
    "C": {10: 69,   20: 213},
    "D": {10: 44,   20: 135},
}

PILOT_SEQ_SEED = 12345         # deterministic scattered/training pilot values
PREAMBLE_SEED = 999            # deterministic Schmidl & Cox preamble sequence
HEADER_LEN_BITS = 32
HEADER_CRC_BITS = 32
# A checksum over the header's OWN fields (payload_len + expected_crc),
# independent of the payload_len range-sanity check. The range check can
# only catch corruption that happens to land out of range -- a flipped
# bit that still produces a plausible-looking payload_len sails through
# it silently (confirmed for real: a strong-metric, correctly-CFO-
# resolved preamble lock still occasionally produced a garbage header
# undetected by the range check alone). Modeled on DRM's own FAC (its
# header-equivalent fast-access channel), which is validated purely by
# its own CRC rather than a plausibility check on its decoded fields
# (src/FAC/FAC.cpp in the Dream reference receiver). 8 bits is cheap
# (added to a short header already carried by its own FEC + repetition)
# and catches ~255/256 of random corruption outright.
HEADER_OWN_CRC_BITS = 8
HEADER_BITS = HEADER_LEN_BITS + HEADER_CRC_BITS + HEADER_OWN_CRC_BITS


def _header_checksum8(header_fields_bits):
    """8-bit checksum over the header's payload_len+expected_crc bits,
    used to validate the header itself independent of any plausibility
    check on its decoded fields -- see HEADER_OWN_CRC_BITS."""
    header_bytes = np.packbits(header_fields_bits).tobytes()
    return zlib.crc32(header_bytes) & 0xFF

# ---------------------------------------------------------------------------
# FEC: rate-1/2, constraint-length-7 convolutional code, generator
# polynomials 171/133 (octal) -- the standard textbook code used in
# 802.11, DVB-S, GPS C/A, etc. Public domain, implemented from scratch
# here (not derived from any particular codebase). Hard-decision Viterbi
# decoding: corrects up to roughly 5-8% raw bit error rate, which is
# enough to clean up the fade-driven residual errors from an uncoded
# link (see README). Zero-padded ("flushed") to force the encoder back
# to the all-zero state at the end of every message, which the decoder
# relies on for its final traceback state.
# ---------------------------------------------------------------------------
FEC_K = 7                       # constraint length
FEC_G1 = 0o171
FEC_G2 = 0o133
FEC_N_MEM = FEC_K - 1
FEC_N_STATES = 1 << FEC_N_MEM


def _poly_bits(g, K):
    return [(g >> i) & 1 for i in reversed(range(K))]


_FEC_G1_BITS = _poly_bits(FEC_G1, FEC_K)
_FEC_G2_BITS = _poly_bits(FEC_G2, FEC_K)


def _build_fec_trellis():
    next_state = np.zeros((FEC_N_STATES, 2), dtype=int)
    output = np.zeros((FEC_N_STATES, 2), dtype=int)  # 0..3, encoding (o1,o2)
    for s in range(FEC_N_STATES):
        reg_state = [(s >> i) & 1 for i in range(FEC_N_MEM - 1, -1, -1)]
        for b in (0, 1):
            reg = [b] + reg_state
            o1 = sum(r * g for r, g in zip(reg, _FEC_G1_BITS)) % 2
            o2 = sum(r * g for r, g in zip(reg, _FEC_G2_BITS)) % 2
            new_reg = reg[:-1]
            ns = 0
            for bit in new_reg:
                ns = (ns << 1) | bit
            next_state[s, b] = ns
            output[s, b] = o1 * 2 + o2
    return next_state, output


_FEC_NEXT_STATE, _FEC_OUTPUT = _build_fec_trellis()


def _build_fec_predecessors():
    """For each state, the (at most 2) edges that lead INTO it: which state
    they came from, which input bit was on that edge, and what it output.
    Lets the Viterbi recursion below update all FEC_N_STATES states at once
    with numpy array ops instead of a Python loop over states -- a rate-1/2,
    K=7 code has exactly 2 incoming edges per state."""
    prev_state = np.zeros((FEC_N_STATES, 2), dtype=int)
    prev_bit = np.zeros((FEC_N_STATES, 2), dtype=np.int8)
    prev_output = np.zeros((FEC_N_STATES, 2), dtype=int)
    slot = np.zeros(FEC_N_STATES, dtype=int)
    for s in range(FEC_N_STATES):
        for b in (0, 1):
            ns = _FEC_NEXT_STATE[s, b]
            k = slot[ns]
            prev_state[ns, k] = s
            prev_bit[ns, k] = b
            prev_output[ns, k] = _FEC_OUTPUT[s, b]
            slot[ns] += 1
    return prev_state, prev_bit, prev_output


_FEC_PREV_STATE, _FEC_PREV_BIT, _FEC_PREV_OUTPUT = _build_fec_predecessors()

if _HAVE_NUMBA:
    @numba.njit(cache=True)
    def _viterbi_hard_numba(recv, prev_state, prev_bit, dist_from_prev):
        n_steps = recv.shape[0]
        n_states = prev_state.shape[0]
        path_metric = np.zeros(n_states, dtype=np.int64)
        path_metric[1:] = 1 << 30
        backptr = np.zeros((n_steps, n_states), dtype=np.int8)
        prevstate_arr = np.zeros((n_steps, n_states), dtype=np.int64)
        for t in range(n_steps):
            r = recv[t]
            new_metric = np.empty(n_states, dtype=np.int64)
            for ns in range(n_states):
                s0 = prev_state[ns, 0]
                s1 = prev_state[ns, 1]
                m0 = path_metric[s0] + dist_from_prev[ns, 0, r]
                m1 = path_metric[s1] + dist_from_prev[ns, 1, r]
                if m0 <= m1:
                    new_metric[ns] = m0
                    prevstate_arr[t, ns] = s0
                    backptr[t, ns] = prev_bit[ns, 0]
                else:
                    new_metric[ns] = m1
                    prevstate_arr[t, ns] = s1
                    backptr[t, ns] = prev_bit[ns, 1]
            path_metric = new_metric
        return backptr, prevstate_arr

    @numba.njit(cache=True)
    def _viterbi_soft_numba(r1_arr, r2_arr, prev_state, prev_bit, ideal_from_prev):
        n_steps = r1_arr.shape[0]
        n_states = prev_state.shape[0]
        path_metric = np.full(n_states, np.inf)
        path_metric[0] = 0.0
        backptr = np.zeros((n_steps, n_states), dtype=np.int8)
        prevstate_arr = np.zeros((n_steps, n_states), dtype=np.int64)
        for t in range(n_steps):
            r1 = r1_arr[t]
            r2 = r2_arr[t]
            new_metric = np.empty(n_states, dtype=np.float64)
            for ns in range(n_states):
                s0 = prev_state[ns, 0]
                s1 = prev_state[ns, 1]
                bm0 = (r1 - ideal_from_prev[ns, 0, 0]) ** 2 + (r2 - ideal_from_prev[ns, 0, 1]) ** 2
                bm1 = (r1 - ideal_from_prev[ns, 1, 0]) ** 2 + (r2 - ideal_from_prev[ns, 1, 1]) ** 2
                m0 = path_metric[s0] + bm0
                m1 = path_metric[s1] + bm1
                if m0 <= m1:
                    new_metric[ns] = m0
                    prevstate_arr[t, ns] = s0
                    backptr[t, ns] = prev_bit[ns, 0]
                else:
                    new_metric[ns] = m1
                    prevstate_arr[t, ns] = s1
                    backptr[t, ns] = prev_bit[ns, 1]
            path_metric = new_metric
        return backptr, prevstate_arr, path_metric[0]

    @numba.njit(cache=True)
    def _traceback_numba(backptr, prevstate_arr, msg_len_bits):
        n_steps = backptr.shape[0]
        state = 0
        bits = np.zeros(n_steps, dtype=np.uint8)
        for t in range(n_steps - 1, -1, -1):
            bits[t] = backptr[t, state]
            state = prevstate_arr[t, state]
        return bits[:msg_len_bits]

    # Trigger JIT compilation now (at import time) rather than on the first
    # real decode -- a live receiver's first fragment shouldn't eat a
    # one-off ~1s compile stall on top of everything else going on.
    _traceback_numba(
        _viterbi_hard_numba(np.zeros(4, dtype=np.int64), _FEC_PREV_STATE, _FEC_PREV_BIT,
                             np.zeros((FEC_N_STATES, 2, 4), dtype=np.int64))[0],
        _viterbi_hard_numba(np.zeros(4, dtype=np.int64), _FEC_PREV_STATE, _FEC_PREV_BIT,
                             np.zeros((FEC_N_STATES, 2, 4), dtype=np.int64))[1],
        1)
    _soft_warmup = _viterbi_soft_numba(np.zeros(4), np.zeros(4), _FEC_PREV_STATE, _FEC_PREV_BIT,
                                        np.zeros((FEC_N_STATES, 2, 2)))
    _traceback_numba(_soft_warmup[0], _soft_warmup[1], 1)

    @numba.njit(cache=True)
    def _fec_encode_numba(bits, g1, g2, n_mem):
        n_steps = len(bits) + n_mem
        out = np.empty(2 * n_steps, dtype=np.uint8)
        reg = np.zeros(n_mem + 1, dtype=np.uint8)
        for t in range(n_steps):
            b = bits[t] if t < len(bits) else 0
            for i in range(n_mem, 0, -1):
                reg[i] = reg[i - 1]
            reg[0] = b
            o1 = 0
            o2 = 0
            for i in range(n_mem + 1):
                o1 ^= reg[i] & g1[i]
                o2 ^= reg[i] & g2[i]
            out[2 * t] = o1
            out[2 * t + 1] = o2
        return out

    _fec_encode_numba(np.zeros(1, dtype=np.uint8),
                       np.zeros(FEC_K, dtype=np.uint8), np.zeros(FEC_K, dtype=np.uint8), FEC_N_MEM)


_FEC_G1_ARR = np.array(_FEC_G1_BITS, dtype=np.uint8)
_FEC_G2_ARR = np.array(_FEC_G2_BITS, dtype=np.uint8)


def fec_encode(bits: np.ndarray) -> np.ndarray:
    """Rate-1/2 convolutional encode with zero-flush termination.
    Output length = 2 * (len(bits) + FEC_N_MEM).

    Dispatches to a numba-JIT'd version when available -- this isn't just
    the TX-side encoder, decode_frame also calls it on every completed
    decode to compute its BER estimate (re-encoding the decoded message
    and comparing against the received bits), and the original pure-
    Python nested-generator-sum loop was never optimized the way every
    OTHER FEC function in this file already was. Confirmed for real via
    cProfile against a real hardware capture: this one function alone
    accounted for 40 of 63 total seconds (63%) of decode time -- by far
    the single largest cost in the whole receive pipeline, not the sync/
    CFO/filtering work this session had otherwise been focused on."""
    if _HAVE_NUMBA:
        return _fec_encode_numba(np.asarray(bits, dtype=np.uint8), _FEC_G1_ARR, _FEC_G2_ARR, FEC_N_MEM)
    shift_reg = [0] * FEC_N_MEM
    out = []
    padded = list(bits) + [0] * FEC_N_MEM
    for b in padded:
        reg = [int(b)] + shift_reg
        o1 = sum(r * g for r, g in zip(reg, _FEC_G1_BITS)) % 2
        o2 = sum(r * g for r, g in zip(reg, _FEC_G2_BITS)) % 2
        out.append(o1)
        out.append(o2)
        shift_reg = reg[:-1]
    return np.array(out, dtype=np.uint8)


def fec_coded_len(msg_len_bits: int) -> int:
    return 2 * (msg_len_bits + FEC_N_MEM)


def fec_decode(coded_bits: np.ndarray, msg_len_bits: int) -> np.ndarray:
    """Hard-decision Viterbi decode. coded_bits length must equal
    fec_coded_len(msg_len_bits). Returns msg_len_bits decoded bits."""
    n_steps = msg_len_bits + FEC_N_MEM
    symbols = coded_bits[:2 * n_steps].reshape(-1, 2).astype(int)
    recv = symbols[:, 0] * 2 + symbols[:, 1]

    INF = 1 << 30
    path_metric = np.full(FEC_N_STATES, INF)
    path_metric[0] = 0
    backptr = np.zeros((n_steps, FEC_N_STATES), dtype=np.int8)
    prevstate = np.zeros((n_steps, FEC_N_STATES), dtype=int)

    # precompute Hamming distance lookup: dist[output_sym, recv_sym]
    dist = np.array([[bin(a ^ b).count("1") for b in range(4)] for a in range(4)])
    # dist_from_prev[ns, k, r] = branch metric of the k-th incoming edge into
    # ns, for each possible received symbol r -- lets each step below do one
    # vectorized lookup instead of a Python loop over states.
    dist_from_prev = dist[_FEC_PREV_OUTPUT]  # shape (FEC_N_STATES, 2, 4)

    if _HAVE_NUMBA:
        backptr, prevstate = _viterbi_hard_numba(
            recv.astype(np.int64), _FEC_PREV_STATE, _FEC_PREV_BIT, dist_from_prev.astype(np.int64))
        return _traceback_numba(backptr, prevstate, msg_len_bits)

    for t in range(n_steps):
        r = recv[t]
        cand = path_metric[_FEC_PREV_STATE] + dist_from_prev[:, :, r]  # (FEC_N_STATES, 2)
        choose0 = cand[:, 0] <= cand[:, 1]
        path_metric = np.where(choose0, cand[:, 0], cand[:, 1])
        prevstate[t] = np.where(choose0, _FEC_PREV_STATE[:, 0], _FEC_PREV_STATE[:, 1])
        backptr[t] = np.where(choose0, _FEC_PREV_BIT[:, 0], _FEC_PREV_BIT[:, 1])

    state = 0  # flush guarantees the encoder ends at the zero state
    bits = np.zeros(n_steps, dtype=np.uint8)
    for t in range(n_steps - 1, -1, -1):
        bits[t] = backptr[t, state]
        state = prevstate[t, state]
    return bits[:msg_len_bits]


# ideal[out_sym, k] = the +-1 ideal value of the k-th (0 or 1) output bit
# when out_sym (0..3, packed as o1*2+o2) is the trellis's expected output.
# bit value 0 -> +1, bit value 1 -> -1 (matches qpsk_map). Then the ideal
# value pair for each state's two incoming edges: shape (FEC_N_STATES, 2, 2).
# A constant -- was rebuilt (list comprehension) on every decode call, which
# resolve_integer_cfo makes 61 of per fragment.
_FEC_IDEAL = np.array([[1 - 2 * ((sym >> (1 - k)) & 1) for k in range(2)] for sym in range(4)], dtype=float)
_FEC_IDEAL_FROM_PREV = np.ascontiguousarray(_FEC_IDEAL[_FEC_PREV_OUTPUT])
_FEC_IDEAL_FROM_PREV.setflags(write=False)


def fec_decode_soft(soft_values: np.ndarray, msg_len_bits: int, return_metric: bool = False):
    """Soft-decision Viterbi decode. soft_values are the UN-SLICED
    equalized real/imag components (see qpsk_soft) in the same order
    fec_decode expects hard bits, length 2*(msg_len_bits + FEC_N_MEM).
    Positive = likely 0, negative = likely 1 (matches qpsk_map's
    '1 - 2*bit' convention), magnitude = confidence. Branch metric is
    squared Euclidean distance to the ideal +-1 point implied by each
    trellis edge's expected output bits -- this is the standard
    simplification for antipodal-per-bit modulation (no explicit noise
    variance / LLR scaling needed: a low-confidence sample near zero
    naturally contributes a small metric difference between the two
    hypotheses, which is exactly the soft-weighting effect we want).
    Typically ~2dB better than fec_decode's hard-decision Hamming metric.

    return_metric: also return the winning path's final metric (summed
    squared distance, lower = better fit) at the decoder's flushed zero
    state -- a much more principled per-candidate quality score than a
    plain repetition-agreement count, since it reflects how well the
    ENTIRE received sequence actually fits a valid codeword rather than
    just how consistent repeated copies are with each other. Used by
    resolve_integer_cfo to pick the right integer CFO candidate (see its
    docstring for why picking wrong there, at low SNR, was silently
    costing whole fragments)."""
    n_steps = msg_len_bits + FEC_N_MEM
    symbols = soft_values[:2 * n_steps].reshape(-1, 2).astype(float)
    ideal_from_prev = _FEC_IDEAL_FROM_PREV

    if _HAVE_NUMBA:
        r1_arr = np.ascontiguousarray(symbols[:, 0])
        r2_arr = np.ascontiguousarray(symbols[:, 1])
        backptr, prevstate, final_metric = _viterbi_soft_numba(
            r1_arr, r2_arr, _FEC_PREV_STATE, _FEC_PREV_BIT, ideal_from_prev)
        bits = _traceback_numba(backptr, prevstate, msg_len_bits)
        return (bits, float(final_metric)) if return_metric else bits

    path_metric = np.full(FEC_N_STATES, np.inf)
    path_metric[0] = 0.0
    backptr = np.zeros((n_steps, FEC_N_STATES), dtype=np.int8)
    prevstate = np.zeros((n_steps, FEC_N_STATES), dtype=int)

    for t in range(n_steps):
        r1, r2 = symbols[t]
        bm = (r1 - ideal_from_prev[:, :, 0]) ** 2 + (r2 - ideal_from_prev[:, :, 1]) ** 2  # (FEC_N_STATES, 2)
        cand = path_metric[_FEC_PREV_STATE] + bm
        choose0 = cand[:, 0] <= cand[:, 1]
        path_metric = np.where(choose0, cand[:, 0], cand[:, 1])
        prevstate[t] = np.where(choose0, _FEC_PREV_STATE[:, 0], _FEC_PREV_STATE[:, 1])
        backptr[t] = np.where(choose0, _FEC_PREV_BIT[:, 0], _FEC_PREV_BIT[:, 1])

    state = 0
    bits = np.zeros(n_steps, dtype=np.uint8)
    for t in range(n_steps - 1, -1, -1):
        bits[t] = backptr[t, state]
        state = prevstate[t, state]
    bits = bits[:msg_len_bits]
    return (bits, float(path_metric[0])) if return_metric else bits


# ---------------------------------------------------------------------------
# Interleaver: a pseudo-random permutation applied to the FEC-coded bit
# stream before it's mapped onto carriers, and inverted after demapping
# and before Viterbi decoding. Without this, a deep fade that kills a
# run of adjacent carriers kills a run of ADJACENT bits in the code's
# original order -- exactly the burst-error case a convolutional decoder
# is weak against. Interleaving scatters those adjacent code bits across
# far-apart carrier/symbol positions so a localized fade looks like
# scattered, quasi-random errors instead, which Viterbi handles well.
# This is conceptually what DRM's own ~2-second cell interleaver does,
# though DRM uses a specific pseudo-random generator and a fixed time
# span across many frames; this is a simpler whole-message permutation,
# deterministic from message length alone so no side information needs
# to be transmitted for the receiver to invert it.
# ---------------------------------------------------------------------------
INTERLEAVER_SEED = 424242


_INTERLEAVE_PERM_CACHE = {}


def _interleave_perm(n):
    """Cached per length (read-only): it depends only on n, and was being
    regenerated from the RNG on every call -- ~3% of the receiver's and 2%
    of the transmitter's CPU on a Raspberry Pi 4."""
    perm = _INTERLEAVE_PERM_CACHE.get(n)
    if perm is None:
        perm = np.random.default_rng(INTERLEAVER_SEED).permutation(n)
        perm.setflags(write=False)
        if len(_INTERLEAVE_PERM_CACHE) > 64:
            _INTERLEAVE_PERM_CACHE.clear()
        _INTERLEAVE_PERM_CACHE[n] = perm
    return perm


def interleave(bits: np.ndarray) -> np.ndarray:
    return bits[_interleave_perm(len(bits))]


def deinterleave(bits: np.ndarray) -> np.ndarray:
    perm = _interleave_perm(len(bits))
    out = np.empty_like(bits)
    out[perm] = bits
    return out


# Payload whitening (energy dispersal, as DRM does for its MSC/FAC): the
# interleaved coded bits -- INCLUDING the zero padding that fills the last
# partly-used symbol -- are XORed with a fixed PRBS. Without it, low-entropy
# content (zero padding, a config-only fragment, silence) FEC-encodes to
# long constant runs, every carrier of a symbol maps to the same QPSK point
# and the time-domain waveform collapses into a single huge spike (measured:
# 24-30 dB PAPR vs ~9 dB for a normal symbol, ~17-30x the RMS). At a normal
# --amplitude that spike exceeds DAC full scale, gets clipped, and the
# fragment is lost -- observed as exactly every 6th fragment (the framer's
# near-empty one) failing regardless of FEC scheme or SNR.
# 15-bit LFSR x^15 + x^14 + 1 (period 32767), tiled for longer frames.
def _build_prbs_table():
    state = 0x7FFF
    out = np.empty(32767, dtype=np.uint8)
    for i in range(32767):
        bit = ((state >> 14) ^ (state >> 13)) & 1
        out[i] = state & 1
        state = ((state << 1) | bit) & 0x7FFF
    return out


_PRBS_TABLE = _build_prbs_table()


def _prbs_bits(n, start=0):
    idx = (np.arange(start, start + n)) % len(_PRBS_TABLE)
    return _PRBS_TABLE[idx]


def _descramble_soft(soft):
    """Undo the TX whitening on soft values (sign flip where PRBS bit is 1)."""
    return soft * (1.0 - 2.0 * _prbs_bits(len(soft)))


@dataclass
class OFDMConfig:
    mode: str
    fs: float
    n_fft: int
    n_cp: int
    carrier_spacing: float
    data_bins: np.ndarray    # exact DRM carrier indices (Kmin..Kmax, DC excluded)
    fft_bins: np.ndarray     # data_bins recentered to actual IFFT bin indices (see build_config)
    freq_int: int            # scattered pilot frequency spacing (carriers)
    time_int: int            # scattered pilot time period (symbols)
    data_modulation: str = "qpsk"  # modulation for PAYLOAD data only -- the
    # preamble, pilots, and header always stay QPSK regardless of this (see
    # _header_symbol_plan's docstring on why the header specifically never
    # inherits the payload's own choice here, same reasoning as DRM's FAC
    # never inheriting the MSC's chosen scheme). "16qam" doubles payload
    # bits/carrier at the cost of needing roughly 6-7dB more SNR for the
    # same error rate -- an explicit per-link opt-in, not a default,
    # because this is exactly the axis a real fading HF channel punishes.
    fec_scheme: str = "viterbi"  # FEC scheme for PAYLOAD data: "viterbi" (rate-1/2
    # K=7 convolutional code) or "ldpc" (IEEE 802.11n rate-1/2 QC-LDPC). The
    # frame header always stays Viterbi regardless of this setting for fast,
    # robust integer CFO and timing acquisition across candidate sweeps.

    preamble_halves: int = 2  # Schmidl & Cox preamble: this many identical half-symbols (see build_preamble)

    @property
    def symbol_len(self):
        return self.n_fft + self.n_cp

    @property
    def preamble_len(self):
        """Samples in the preamble, CP included (one symbol_len for 2 halves)."""
        return self.n_cp + self.preamble_halves * (self.n_fft // 2)

    @property
    def sc_window(self):
        """Schmidl & Cox correlation window: every half-symbol of the preamble
        against the next (lag n_fft/2). One half (n_fft/2) for 2 halves."""
        return (self.preamble_halves - 1) * (self.n_fft // 2)

    @property
    def n_data(self):
        return len(self.data_bins)

    @property
    def uses_full_training(self):
        # freq_int == 1 means the spec itself can't decimate in frequency
        # for this mode (true for Mode D) -- fall back to periodic full
        # training symbols instead of a per-symbol comb.
        return self.freq_int <= 1


def _fft_bins_for(data_bins):
    """Recenter DRM carrier indices (which need not be symmetric about K=0)
    onto the actual IFFT bin indices used for modulation. ofdm_modulate puts
    freq[fft_bins] = symbols, then ifftshift()s before the ifft -- so the
    physical frequency of a bin only comes out right if the occupied range
    is centered on 0 going in. Using the raw (possibly asymmetric) DRM K
    values directly as bin indices instead splits the occupied band into
    two pieces landing at the wrong physical frequencies, with a real,
    unpopulated gap where they don't meet -- recentering here is the fix.
    Used to also drop whichever carrier landed exactly on the recentered
    DC bin, as a defense against real hardware LO leakage landing on a
    live carrier -- no longer needed now that pluto_soapy_sink.py's
    lo_offset_hz tunes the actual LO away from the signal band instead,
    so nothing here needs to keep the center carrier clear anymore."""
    mid = int((data_bins.min() + data_bins.max()) // 2)
    fft_bins = data_bins - mid
    return data_bins, fft_bins


LO_TABLE_MAX_PERIOD = 1 << 16


def lo_phasor_table(lo_offset_hz, fs):
    """exp(+j*2*pi*lo*n/fs) for n over one full period, exactly, when lo/fs
    is a ratio with a manageable period -- true for any sensible LO
    offset/sample-rate pair (100 kHz at 550 kS/s = 2/11: period 11). Lets
    the per-sample LO-offset shift at the SDR rate be a table lookup
    instead of a complex exp per sample. Returns None if there's no short
    period (callers then fall back to exp); a 1-element [1] for no offset."""
    from fractions import Fraction
    if not lo_offset_hz:
        return np.ones(1, np.complex128)
    try:
        r = Fraction(lo_offset_hz).limit_denominator(10**6) / Fraction(fs).limit_denominator(10**6)
    except (ValueError, ZeroDivisionError):
        return None
    if abs(float(r) - lo_offset_hz / fs) > 1e-12 or r.denominator > LO_TABLE_MAX_PERIOD:
        return None
    n = np.arange(r.denominator)
    return np.exp(1j * 2 * np.pi * (r.numerator * n % r.denominator) / r.denominator)


def _max_prime_factor(n):
    m, d = 1, 2
    while d * d <= n:
        while n % d == 0:
            m, n = d, n // d
        d += 1
    return max(m, n) if n > 1 else m


_FAST_PAD_CACHE = {}


def fft_resample_rate(x, fs_in, fs_out):
    """fft_resample to the rate fs_out, first appending the fewest zero
    samples that make both FFT lengths free of large prime factors.
    Without this the output length can be a large prime times a few small
    ones (mode A 80 kHz -> 550 kS/s: 132027 = 3*7*6287), which numpy's FFT
    handles ~3x slower; e.g. 4 zeros (0.05 ms) -> 132055 = 5*7^4*11 cut
    the TX modulator's resample from ~35 to ~11 ms per fragment. The pad
    is a negligible extra gap after the frame, and measured out-of-band
    emission is slightly LOWER than without it (the zeros soften the
    wrap-around the Fourier method assumes at the fragment boundary)."""
    n_in = len(x)
    key = (n_in, float(fs_in), float(fs_out))
    pad = _FAST_PAD_CACHE.get(key)
    if pad is None:
        pad = None
        # Strict rule first, then -- if no length within 4096 samples meets
        # it -- accept any input length as long as the (~7x longer, so
        # dominant) output length is fast. Without the fallback, mode C at
        # 80 kHz -> 550 kS/s found nothing, used pad 0 and an output length
        # with a prime factor of 383: ~3x slower, enough to push the
        # Raspberry Pi 4's TX modulator to ~96% CPU and underrun.
        for max_in_prime in (100, None):
            for p in range(0, 4096):
                n_out = int(round((n_in + p) * fs_out / fs_in))
                if _max_prime_factor(n_out) <= 11 and (max_in_prime is None
                                                       or _max_prime_factor(n_in + p) <= max_in_prime):
                    pad = p
                    break
            if pad is not None:
                break
        pad = pad or 0
        _FAST_PAD_CACHE[key] = pad
    if pad:
        x = np.concatenate([x, np.zeros(pad, dtype=x.dtype)])
    return fft_resample(x, int(round(len(x) * fs_out / fs_in)))


def fft_resample(x, n_out):
    """Fourier-method resample (like scipy.signal.resample) to n_out samples."""
    n_in = len(x)
    if n_out == n_in:
        return x
    X = np.fft.fft(x)
    if n_out > n_in:
        pad = n_out - n_in
        left = (n_in + 1) // 2
        X_new = np.concatenate([X[:left], np.zeros(pad, dtype=X.dtype), X[left:]])
    else:
        left = (n_out + 1) // 2
        right = n_out - left
        X_new = np.concatenate([X[:left], X[n_in - right:]])
    return np.fft.ifft(X_new) * (n_out / n_in)


def linear_resample(x, fs_in, fs_out):
    """Causal/local resample (real and imaginary parts interpolated
    separately): unlike fft_resample, every output sample depends only on
    its nearby input samples, not the whole buffer. fft_resample is a
    global operation -- every output sample depends on the ENTIRE input via
    the FFT -- which is fine for a one-shot, complete buffer (tx.py's
    output) but wrong for a streaming/growing RX buffer: appending new
    trailing samples retroactively changes the resampled values at
    earlier, already-decided positions too, which showed up as sporadic
    bit errors that didn't go away no matter how much extra data was
    buffered. This trades ideal brick-wall filtering for causality; at the
    ~9x oversampling typical of an SDR capture rate vs. a mode's occupied
    bandwidth here, the mild passband ripple/imaging this introduces is
    negligible next to real channel noise."""
    n_in = len(x)
    dur = (n_in - 1) / fs_in
    n_out = int(round(dur * fs_out)) + 1
    t_in = np.arange(n_in) / fs_in
    t_out = np.arange(n_out) / fs_out
    re = np.interp(t_out, t_in, x.real)
    im = np.interp(t_out, t_in, x.imag)
    return re + 1j * im


MIN_SAMPLES_FOR_STABLE_IQ_ESTIMATE = 50_000


@njit(cache=True)
def _iq_apply(vals, mean_I, mean_Q, slope, scale):
    """correct_iq_imbalance's frozen-coefficient transform in one compiled
    pass (it runs on every native-rate sample -- 550 kS/s from a Pluto);
    same operations in the same order as the numpy expression it replaced:
    I0 = re - mI; Q1 = ((im - mQ) - slope*I0) * scale; out = (I0 + mI) + j(Q1 + mQ)."""
    out = np.empty_like(vals)
    for i in range(vals.shape[0]):
        I0 = vals[i].real - mean_I
        Q0 = vals[i].imag - mean_Q
        Q1 = (Q0 - slope * I0) * scale
        out[i] = complex(I0 + mean_I, Q1 + mean_Q)
    return out


def correct_iq_imbalance(x, cache=None, trim_reference=None):
    """Blind gain/phase IQ-imbalance correction via Gram-Schmidt
    orthogonalization of the I and Q channels -- no calibration tone or
    known reference needed, works directly on the live OFDM signal.

    A real front-end's I and Q paths rarely have exactly matched gain and
    exactly 90-degree quadrature phase; the mismatch leaks each subcarrier
    into its mirror image, showing up as a fixed noise-like EVM floor that
    doesn't improve with SNR (confirmed for real: measuring I/Q second-
    order statistics on a Pluto capture found ~27.5dB image rejection --
    below the 30-40dB+ a well-calibrated AD9363-class radio should hit --
    with an image amplitude ratio, ~4.2%, suspiciously close to a chunk of
    the EVM floor observed at high SNR). Correction: treat I as the
    reference channel, then subtract Q's projection onto I (removing the
    phase-skew correlation) and rescale the result to match I's power
    (removing the gain imbalance), leaving two channels that are both
    equal-power and uncorrelated -- the property a properly balanced
    front-end would have had all along.

    `cache`: an optional dict (persisted across calls on the SAME logical
    stream, e.g. in a per-session state dict) that freezes the estimated
    correction coefficients once a large-enough window has made them
    stable. Without this, recomputing the statistics fresh from an
    ever-growing live buffer would shift EVERY already-corrected sample by
    a slightly different amount each retry -- the same class of bug
    already hit (and fixed) with non-causal resampling on a growing
    buffer. The underlying imbalance is a fixed hardware property, not
    something that should keep changing as more data arrives.

    trim_reference: same convention as get_rx_raw_incremental's (an
    absolute NATIVE sample index; None disables trimming). This
    function's own `grown` output cache runs at the native SDR rate
    (this is the FIRST stage applied to the raw buffer, before any
    lowpass/resample) and was, until this parameter existed, never
    trimmed at all -- confirmed for real to matter: on a long enough
    live session (hundreds of fragments) it grew large enough for its
    own doubling-copy to cost multiple seconds, a stall indistinguishable
    from the raw-buffer growth bug already fixed elsewhere, just one
    processing stage downstream of where that fix actually applies."""
    if cache is not None and "slope" in cache:
        # Once the coefficients are frozen, applying them is a pointwise
        # (memoryless) transform -- no neighboring-sample dependency at
        # all, unlike resampling or FIR filtering -- so unlike those,
        # this doesn't even need an overlap margin: just apply to
        # whatever's new and append. Confirmed for real this matters:
        # without it, re-applying the (cheap-per-sample) transform to the
        # ENTIRE growing buffer on every single retry was still a real,
        # measurable cost by the time a session reached tens of millions
        # of accumulated samples, even though the coefficients themselves
        # were already frozen and stable.
        mean_I, mean_Q, slope, scale = (cache["mean_I"], cache["mean_Q"], cache["slope"], cache["scale"])

        def _apply(vals):
            return _iq_apply(np.ascontiguousarray(vals, np.complex128), mean_I, mean_Q, slope, scale)

        prev_n = cache.get("out_n", 0)
        grown = cache.get("grown")
        if grown is not None and len(x) >= prev_n:
            if len(x) == prev_n:
                return grown
            if (trim_reference is not None and grown._buf is not None
                    and len(grown._buf) > INCREMENTAL_CACHE_HARD_CAP):
                grown.trim_before(trim_reference - INCREMENTAL_CACHE_TRIM_MARGIN)
                cache["last_trim_at"] = trim_reference
            grown.append(_apply(x[prev_n:]))
        else:
            grown = GrowableComplexArray()
            grown.append(_apply(x))
            cache["grown"] = grown
        cache["out_n"] = len(x)
        if trim_reference is not None:
            last_trim = cache.get("last_trim_at", 0)
            if trim_reference - last_trim >= INCREMENTAL_CACHE_TRIM_INTERVAL:
                grown.trim_before(trim_reference - INCREMENTAL_CACHE_TRIM_MARGIN)
                cache["last_trim_at"] = trim_reference
        return grown

    if cache is not None and len(x) < MIN_SAMPLES_FOR_STABLE_IQ_ESTIMATE:
        # Not enough data yet for a trustworthy estimate. The naive thing
        # -- recompute slope/scale from whatever partial buffer exists
        # and apply it anyway -- doesn't just give a slightly-off
        # correction, it gives a DIFFERENT one on every single retry as
        # the live buffer grows (the estimate is a function of the whole
        # x each time, since nothing is cached yet), so the SAME early
        # samples (e.g. fragment 1's header) get reprocessed with a
        # different pointwise transform on every retry. Confirmed for
        # real: on a short, genuinely clean/balanced session (well under
        # this threshold), a strong, correctly-CFO-resolved preamble lock
        # still produced a garbage header on every retry -- caused by
        # this function's own noise-driven, retry-to-retry-inconsistent
        # "correction" being applied to a signal that had no real
        # imbalance to correct in the first place. Passing samples
        # through unmodified until the estimate can actually stabilize
        # and freeze is strictly safer than guessing from too little data.
        return x

    I = x.real
    Q = x.imag
    mean_I = np.mean(I)
    mean_Q = np.mean(Q)
    I0 = I - mean_I
    Q0 = Q - mean_Q
    var_I = np.mean(I0 ** 2)
    if var_I <= 0:
        return x
    cross = np.mean(I0 * Q0)
    slope = cross / var_I
    Q1 = Q0 - slope * I0
    var_Q1 = np.mean(Q1 ** 2)
    if var_Q1 <= 0:
        return x
    scale = np.sqrt(var_I / var_Q1)
    if cache is not None:
        cache.update(mean_I=mean_I, mean_Q=mean_Q, slope=slope, scale=scale)
    return (I0 + mean_I) + 1j * (Q1 * scale + mean_Q)


def design_lowpass_fir(cutoff_hz, fs, numtaps=129):
    """Windowed-sinc causal FIR low-pass filter (Hamming window), unity DC
    gain. Written from scratch -- see the module docstring's note on only
    ever taking plain data, never algorithm code, from the Dream DRM
    project (GPL-2.0); this is a standard, generic DSP technique, not
    derived from any particular codebase."""
    n = np.arange(numtaps) - (numtaps - 1) / 2
    fc = cutoff_hz / fs
    with np.errstate(invalid="ignore"):
        h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.hamming(numtaps)
    h /= np.sum(h)
    return h


INCREMENTAL_CACHE_TRIM_MARGIN = 500_000    # native samples of safety margin (matches RAW_BUFFER_TRIM_MARGIN)
INCREMENTAL_CACHE_TRIM_INTERVAL = 1_000_000  # native samples between trim passes
INCREMENTAL_CACHE_HARD_CAP = 50_000_000  # see hf_ofdm_rx.py's RAW_BUFFER_HARD_CAP for the full
# rationale and its honest limits -- same pragmatic (not airtight) safety net here.


def apply_lowpass_filter_incremental(x, taps, cache, trim_reference=None):
    """Causal FIR filtering (see design_lowpass_fir) of a growing buffer,
    computing only the NEW output samples each call from cached filter
    state (the previous call's trailing numtaps-1 input samples) instead
    of reconvolving the whole buffer from scratch. Safe to do -- unlike
    the resampling/IQ-imbalance-statistics caches elsewhere in this file,
    a causal FIR filter's output at a given sample depends only on that
    sample and the ones before it, so appending more input can never
    change an already-computed output sample; this only avoids redundant
    work, it isn't needed for correctness the way those other caches are.

    Restricting the signal to its own occupied bandwidth BEFORE any
    correlation-based sync search matters a lot when the native capture
    rate is much wider than the signal itself (e.g. a 192kHz SDR capture
    of a ~19kHz-wide signal): our own linear_resample trades ideal brick-
    wall filtering for causality (see its docstring), so out-of-band
    noise from that wide a gap isn't fully rejected by resampling alone
    and aliases straight into the signal band, directly diluting the
    Schmidl & Cox correlation metric's effective SNR. Confirmed for real:
    a capture with a properly wideband-measured true SNR of ~8.2dB was
    still only producing a sync metric of ~0.27, well short of the ~0.85
    a clean AWGN channel at that SNR should give -- consistent with
    exactly this alias/noise-dilution gap, not a fundamentally low true
    SNR. This is the same reason a real DRM receiver (see Dream's
    TimeSync.cpp) band-pass filters before its own guard-interval
    correlation, rather than correlating the full-bandwidth signal."""
    numtaps = len(taps)
    prev_n = cache.get("n", 0)
    if prev_n == 0 or len(x) < prev_n:
        padded = np.concatenate([np.zeros(numtaps - 1, dtype=complex), x[:len(x)]])
        y = np.convolve(padded, taps, mode="valid")
        grown = GrowableComplexArray()
        grown.append(y)
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
    y_new = np.convolve(padded_new, taps, mode="valid")
    if (trim_reference is not None and grown._buf is not None
            and len(grown._buf) > INCREMENTAL_CACHE_HARD_CAP):
        grown.trim_before(trim_reference - INCREMENTAL_CACHE_TRIM_MARGIN)
        cache["last_trim_at"] = trim_reference
    grown.append(y_new)
    cache["n"] = len(x)
    if trim_reference is not None:
        last_trim = cache.get("last_trim_at", 0)
        if trim_reference - last_trim >= INCREMENTAL_CACHE_TRIM_INTERVAL:
            grown.trim_before(trim_reference - INCREMENTAL_CACHE_TRIM_MARGIN)
            cache["last_trim_at"] = trim_reference
    return grown


def stream_lowpass_filter(new_samples, taps, state):
    """Streaming (chunk-by-chunk) causal FIR filtering for a caller that
    only ever sees each NEW chunk once and doesn't keep the full
    cumulative buffer around (unlike apply_lowpass_filter_incremental,
    which re-derives its state from the whole buffer each call) -- e.g.
    a live pre-lock scan that intentionally only looks at each freshly-
    arrived stdin chunk plus a short tail, to stay cheap while waiting
    for a real signal to show up. `state` carries just the trailing
    numtaps-1 input samples between calls. Always returns exactly
    len(new_samples) outputs, zero-padding history on the very first
    call (the same convention as the other incremental helpers here)."""
    numtaps = len(taps)
    history = state.get("history")
    if history is None:
        history = np.zeros(numtaps - 1, dtype=complex)
    padded = np.concatenate([history, new_samples])
    y = np.convolve(padded, taps, mode="valid")
    state["history"] = padded[-(numtaps - 1):] if numtaps > 1 else np.zeros(0, dtype=complex)
    return y


def linear_resample_incremental(x, fs_in, fs_out, cache, trim_reference=None):
    """Same output as linear_resample(x, fs_in, fs_out), but for a growing
    buffer (x only ever gaining samples at its end call to call, as with a
    live stdin reader) it avoids redoing the full interpolation from
    scratch every call. np.interp only uses the two xp points bracketing
    each query point, so appending more input samples never changes an
    already-computed output sample at an earlier time -- only the cached
    tail needs recomputing, plus a small margin of old input samples so
    the new stretch has correct bracketing points right at the seam.
    `cache` is an ordinary dict the caller keeps around across calls on
    the SAME logical stream (e.g. in a per-session state dict); an empty
    dict on the first call is fine.

    trim_reference: an absolute index in x's OWN (fs_in, i.e. usually
    native SDR rate) domain, same convention as get_rx_raw_incremental's
    -- converted internally to this cache's own fs_out-domain indexing
    before trimming, since that's the domain this function's own `grown`
    output actually lives in. Was never trimmed at all before this
    parameter existed; see apply_lowpass_filter_incremental's matching
    note for why that stopped being safe to assume on a long-running
    session."""
    n_in = len(x)
    prev_n_in = cache.get("n_in", 0)
    grown = cache.get("grown")
    if grown is None or n_in < prev_n_in:
        out = linear_resample(x[:n_in], fs_in, fs_out)
        grown = GrowableComplexArray()
        grown.append(out)
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
    t_in_slice = np.arange(start, n_in) / fs_in
    t_out_new = np.arange(n_out_prev, n_out_new) / fs_out
    x_tail = x[start:]
    re = np.interp(t_out_new, t_in_slice, x_tail.real)
    im = np.interp(t_out_new, t_in_slice, x_tail.imag)
    if (trim_reference is not None and grown._buf is not None
            and len(grown._buf) > INCREMENTAL_CACHE_HARD_CAP * fs_out / fs_in):
        out_trim_reference = int(trim_reference * fs_out / fs_in)
        out_margin = int(INCREMENTAL_CACHE_TRIM_MARGIN * fs_out / fs_in)
        grown.trim_before(out_trim_reference - out_margin)
        cache["last_trim_at"] = trim_reference
    grown.append(re + 1j * im)
    cache["n_in"] = n_in
    if trim_reference is not None:
        last_trim = cache.get("last_trim_at", 0)
        if trim_reference - last_trim >= INCREMENTAL_CACHE_TRIM_INTERVAL:
            out_trim_reference = int(trim_reference * fs_out / fs_in)
            out_margin = int(INCREMENTAL_CACHE_TRIM_MARGIN * fs_out / fs_in)
            grown.trim_before(out_trim_reference - out_margin)
            cache["last_trim_at"] = trim_reference
    return grown


def build_config(mode, spectrum_occupancy_khz=20, edge_guard_frac=0.03, data_modulation="qpsk", fec_scheme="viterbi"):
    """Config using DRM's exact carrier range for the given mode/occupancy.
    DRM's own spec tops out at 20kHz -- for anything wider (e.g. 40/80kHz,
    useful with a wideband SDR and no DRM-compatibility requirement),
    fall back to build_config_scaled's symmetric extension instead of a
    table lookup, since there's no official Kmin/Kmax to look up."""
    min_khz = DRM_MODES.get(mode, {}).get("min_occupancy_khz")
    if min_khz and spectrum_occupancy_khz < min_khz:
        raise ValueError(f"mode {mode} needs at least {min_khz:g} kHz occupancy "
                         f"(got {spectrum_occupancy_khz:g}): too few carriers to frame and sync")
    if mode not in _KMIN_TABLE or spectrum_occupancy_khz not in _KMIN_TABLE[mode]:
        return build_config_scaled(mode, spectrum_occupancy_khz * 1000,
                                    edge_guard_frac=edge_guard_frac, data_modulation=data_modulation,
                                    fec_scheme=fec_scheme)
    spec = DRM_MODES[mode]
    kmin = _KMIN_TABLE[mode][spectrum_occupancy_khz]
    kmax = _KMAX_TABLE[mode][spectrum_occupancy_khz]
    data_bins = np.concatenate([np.arange(kmin, 0), np.arange(1, kmax + 1)])
    data_bins, fft_bins = _fft_bins_for(data_bins)

    span = kmax - kmin
    n_fft = int(np.ceil(span / (1 - 2 * edge_guard_frac)))
    if n_fft % 2:
        n_fft += 1
    fs = spec["spacing_hz"] * n_fft
    n_cp = int(round(n_fft * spec["guard_fraction"]))

    return OFDMConfig(mode=mode, fs=fs, n_fft=n_fft, n_cp=n_cp,
                       carrier_spacing=spec["spacing_hz"], data_bins=data_bins,
                       fft_bins=fft_bins,
                       freq_int=spec["freq_int"], time_int=spec["time_int"],
                       preamble_halves=spec.get("preamble_halves", 2),
                       data_modulation=data_modulation,
                       fec_scheme=fec_scheme)


def build_config_scaled(mode, target_bandwidth_hz, edge_guard_frac=0.03, data_modulation="qpsk", fec_scheme="viterbi"):
    """Go wider than DRM's official ceiling: same spacing/guard fraction/pilot
    geometry, carrier range scaled (symmetrically -- we don't have an exact
    spec range to extend for non-standard bandwidths) to fill the wider band."""
    spec = DRM_MODES[mode]
    n_carriers = int(round(target_bandwidth_hz * (1 - 2 * edge_guard_frac) / spec["spacing_hz"]))
    half = n_carriers // 2
    data_bins = np.concatenate([np.arange(-half, 0), np.arange(1, n_carriers - half + 1)])
    data_bins, fft_bins = _fft_bins_for(data_bins)
    span = data_bins.max() - data_bins.min()
    n_fft = int(np.ceil(span / (1 - 2 * edge_guard_frac)))
    if n_fft % 2:
        n_fft += 1
    fs = spec["spacing_hz"] * n_fft
    n_cp = int(round(n_fft * spec["guard_fraction"]))
    return OFDMConfig(mode=mode, fs=fs, n_fft=n_fft, n_cp=n_cp,
                       carrier_spacing=spec["spacing_hz"], data_bins=data_bins,
                       fft_bins=fft_bins,
                       freq_int=spec["freq_int"], time_int=spec["time_int"],
                       preamble_halves=spec.get("preamble_halves", 2),
                       data_modulation=data_modulation,
                       fec_scheme=fec_scheme)


def print_config(cfg, file=None, fragment_size_bytes=None, fragment_gap_ms=0.0):
    import sys
    file = file or sys.stderr
    print(f"Mode {cfg.mode} ({DRM_MODES[cfg.mode]['note']})", file=file)
    print(f"  Sample rate / bandwidth : {cfg.fs:.1f} Hz", file=file)
    print(f"  FFT size                : {cfg.n_fft}", file=file)
    print(f"  Cyclic prefix           : {cfg.n_cp} samples = {cfg.n_cp / cfg.fs * 1000:.2f} ms", file=file)
    print(f"  OFDM symbol             : {cfg.symbol_len / cfg.fs * 1000:.2f} ms", file=file)
    print(f"  Carrier spacing         : {cfg.carrier_spacing:.3f} Hz", file=file)
    print(f"  Carrier range           : K = {cfg.data_bins.min()} .. {cfg.data_bins.max()} "
          f"({cfg.n_data} carriers, DC excluded)", file=file)
    if cfg.uses_full_training:
        print(f"  Pilot scheme            : full training symbol every {cfg.time_int} symbols "
              f"(freq_int=1 -- DRM's own spec can't decimate this mode in frequency)", file=file)
    else:
        print(f"  Pilot scheme            : scattered comb, every symbol, "
              f"1-in-{cfg.freq_int} carriers", file=file)
    bitrate = estimate_payload_bitrate(cfg)
    print(f"  Payload modulation      : {cfg.data_modulation} "
          f"(preamble/pilots/header always QPSK)", file=file)
    fec_name = getattr(cfg, "fec_scheme", "viterbi").upper()
    print(f"  Payload FEC             : {fec_name} (rate 1/2)", file=file)
    print(f"  Payload bitrate         : {bitrate / 1000:.2f} kbps "
          f"(after FEC, excludes header/fragment-gap overhead)", file=file)
    if fragment_size_bytes:
        effective = estimate_effective_bitrate(cfg, fragment_size_bytes, fragment_gap_ms)
        print(f"  Effective bitrate       : {effective / 1000:.2f} kbps "
              f"(after header + {fragment_gap_ms:.0f}ms fragment-gap overhead, "
              f"at --fragment-size {fragment_size_bytes})", file=file)


# ---------------------------------------------------------------------------
# QPSK mapping
# ---------------------------------------------------------------------------
def qpsk_map(bits):
    bits = bits.reshape(-1, 2).astype(np.int64)
    i = 1 - 2 * bits[:, 0]
    q = 1 - 2 * bits[:, 1]
    return (i + 1j * q) / np.sqrt(2)


def qpsk_demap(symbols):
    b0 = (symbols.real < 0).astype(np.uint8)
    b1 = (symbols.imag < 0).astype(np.uint8)
    return np.stack([b0, b1], axis=1).reshape(-1)


def qpsk_soft(symbols):
    """Soft (un-sliced) equivalent of qpsk_demap: interleaved real/imag
    values in the same bit order, positive meaning 'likely 0', negative
    meaning 'likely 1', magnitude carrying confidence. Used for soft-
    decision Viterbi instead of hard-decision bits."""
    return np.stack([symbols.real, symbols.imag], axis=1).reshape(-1)


# ---------------------------------------------------------------------------
# 16-QAM mapping -- an opt-in, higher-throughput alternative to QPSK for
# PAYLOAD data only (see OFDMConfig.data_modulation's docstring on why the
# preamble/pilots/header never use this regardless of the setting). Square
# 16-QAM Gray-codes cleanly into two INDEPENDENT 4-level PAM rails (I and
# Q), each carrying 2 of the symbol's 4 bits -- standard, textbook
# constructions, implemented from scratch here like the rest of this
# module's DSP (see the module docstring's note on Dream).
# ---------------------------------------------------------------------------
_QAM16_NORM = np.sqrt(10.0)  # average power of {+-1,+-3}x{+-1,+-3} is 10


def _pam4_map(b0, b1):
    """Gray-coded 4-level PAM: (b0,b1) -> level in {-3,-1,+1,+3}. b0 is the
    sign bit (0 -> positive, matching qpsk_map's convention); b1 is 0 for
    the inner level (magnitude 1), 1 for the outer level (magnitude 3)."""
    return (1 - 2 * b0) * (2 - (1 - 2 * b1))


def qam16_map(bits):
    bits = bits.reshape(-1, 4).astype(np.int64)
    i = _pam4_map(bits[:, 0], bits[:, 1])
    q = _pam4_map(bits[:, 2], bits[:, 3])
    return (i + 1j * q) / _QAM16_NORM


def qam16_demap(symbols):
    i = symbols.real * _QAM16_NORM
    q = symbols.imag * _QAM16_NORM
    b0 = (i < 0).astype(np.uint8)
    b1 = (np.abs(i) < 2).astype(np.uint8)
    b2 = (q < 0).astype(np.uint8)
    b3 = (np.abs(q) < 2).astype(np.uint8)
    return np.stack([b0, b1, b2, b3], axis=1).reshape(-1)


def qam16_soft(symbols):
    """Soft equivalent of qam16_demap, same sign convention as qpsk_soft
    (positive = likely 0, negative = likely 1). The sign bit's soft value
    is just the rail value itself (it's exactly the sign, no approximation
    needed); the magnitude bit uses the standard simplified/piecewise
    Gray-16QAM soft-demapping approximation -- LLR ~= threshold - |rail|,
    positive (|rail| near the inner level) meaning 'likely inner (bit 0)',
    negative (|rail| near the outer level) meaning 'likely outer (bit 1)'.
    Not the exact log-likelihood ratio (which needs a log of a sum of
    exponentials), but the standard linear approximation used widely for
    Gray-mapped square QAM, and consistent with the same
    positive=0/negative=1 convention the Viterbi decoder already expects."""
    i = symbols.real * _QAM16_NORM
    q = symbols.imag * _QAM16_NORM
    b0 = i
    b1 = 2 - np.abs(i)
    b2 = q
    b3 = 2 - np.abs(q)
    return np.stack([b0, b1, b2, b3], axis=1).reshape(-1)


def data_bits_per_symbol(cfg):
    return 4 if cfg.data_modulation == "16qam" else 2


def data_map(cfg, bits):
    return qam16_map(bits) if cfg.data_modulation == "16qam" else qpsk_map(bits)


def data_demap(cfg, symbols):
    return qam16_demap(symbols) if cfg.data_modulation == "16qam" else qpsk_demap(symbols)


def data_soft(cfg, symbols):
    return qam16_soft(symbols) if cfg.data_modulation == "16qam" else qpsk_soft(symbols)


def _cfg_cache(store, cfg, key, build):
    """Per-config memo for arrays that depend only on the config (and key).
    Keyed by id(cfg) but also holds cfg itself, so a recycled id can never
    return another config's entry. Cached arrays are made read-only so a
    caller that tries to modify a shared one fails loudly instead of
    silently corrupting every later call."""
    entry = store.get((id(cfg), key))
    if entry is None or entry[0] is not cfg:
        val = build()
        val.setflags(write=False)
        entry = (cfg, val)
        store[(id(cfg), key)] = entry
    return entry[1]


_PILOT_SEQ_CACHE = {}
_PILOT_MASK_CACHE = {}


def pilot_sequence(cfg):
    """One deterministic known QPSK value per carrier position (local index
    within cfg.data_bins), reused whenever that position carries a pilot.
    Cached per config (it was regenerated from the RNG on every call --
    ~60 times per fragment, ~11 ms/fragment of receiver time)."""
    def build():
        rng = np.random.default_rng(PILOT_SEQ_SEED)
        bits = rng.integers(0, 2, size=cfg.n_data * 2)
        return qpsk_map(bits)
    return _cfg_cache(_PILOT_SEQ_CACHE, cfg, None, build)


# ---------------------------------------------------------------------------
# Byte <-> bit helpers
# ---------------------------------------------------------------------------
def bytes_to_bits(data: bytes) -> np.ndarray:
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8))


def bits_to_bytes(bits: np.ndarray) -> bytes:
    n = (len(bits) // 8) * 8
    return np.packbits(bits[:n].astype(np.uint8)).tobytes()


# ---------------------------------------------------------------------------
# Schmidl & Cox preamble
# ---------------------------------------------------------------------------
def build_preamble(cfg):
    rng = np.random.default_rng(PREAMBLE_SEED)
    freq = np.zeros(cfg.n_fft, dtype=complex)
    # Repeated half-symbol structure (needed for Schmidl & Cox) means only
    # every OTHER carrier, in true standard FFT bin order, can be
    # populated. Since cfg.fft_bins is used directly as a (possibly
    # negative) Python index with no shift (see ofdm_modulate), its
    # standard-order position is just cfg.fft_bins % n_fft, and n_fft is
    # always even, so that position's parity always matches the parity of
    # cfg.fft_bins itself -- no separate shift/modulo dance needed.
    periodic_bins = cfg.fft_bins[cfg.fft_bins % 2 == 0]
    bits = rng.integers(0, 2, size=len(periodic_bins) * 2)
    freq[periodic_bins] = qpsk_map(bits) * np.sqrt(2)
    time_sig = np.fft.ifft(freq) * np.sqrt(cfg.n_fft)  # see ofdm_modulate: no ifftshift
    if cfg.preamble_halves != 2:
        # More identical halves (mode VU): a short symbol's single pair of
        # n_fft/2 halves is too little to correlate -- noise alone often
        # cleared SYNC_METRIC_MIN (16 samples per half at 80 kHz). The lag
        # stays n_fft/2, so the CFO range is unchanged; schmidl_cox_sync
        # correlates over all of them (cfg.sc_window).
        time_sig = np.tile(time_sig[:cfg.n_fft // 2], cfg.preamble_halves)
    with_cp = np.concatenate([time_sig[-cfg.n_cp:], time_sig])
    return with_cp


def quick_sc_scan(x, L):
    """Vectorized Schmidl & Cox metric over every valid offset in x, using a
    fixed half-symbol length L (in samples). Much faster than the per-offset
    Python loop in schmidl_cox_sync -- used for cheap live progress scanning
    of incoming chunks, not for the final, authoritative sync (which also
    needs the CFO phase at the winning offset)."""
    n = len(x)
    max_d = n - 2 * L
    if max_d <= 0:
        return 0, 0.0
    metric, _, _ = _sc_metric(np.ascontiguousarray(x), L, max_d)
    d = int(np.argmax(metric))
    return d, float(metric[d])


# Minimum Schmidl-Cox metric to call a preamble "found". For a preamble at
# SNR x (linear) the metric is ~ (x/(1+x))^2 -- 0.5 needs ~4 dB, which is
# WORSE than what the payload FEC can decode (LDPC works to ~1-2 dB), so a
# 0.5 threshold made sync/"garbage header" the limiting factor on weak links
# (observed: LDPC fragments decoding at 7.5% raw BER while most preambles
# were rejected at metric ~0.45). Noise-only input peaks around 1/L ~ 0.01-0.02
# (L = n_fft/2 samples), so 0.3 (~0.8 dB) keeps a wide margin over noise;
# false locks are still rejected by the header checksum and payload CRC.
SYNC_METRIC_MIN = 0.3


@njit(cache=True)
def _sc_metric(w, L, n_out, W=-1):
    """Schmidl & Cox over offsets d = 0..n_out-1 of w (needs n_out + L + W
    samples), lag L, window W (default L -- the classic two-half form):
    P[d] = sum_{i<W} conj(w[d+i]) w[d+i+L], E12[d] = E1[d] E2[d] with
    E1[d] = sum_{i<W} |w[d+i]|^2, E2[d] = sum_{i<W} |w[d+i+L]|^2,
    metric = |P|^2 / (E12 + 1e-12). A longer window (a preamble of more
    identical halves, see build_preamble) averages over more samples at the
    same lag. One pass of running sums in double precision, re-summed
    exactly every 4096 offsets so rounding can't drift -- instead of numpy's
    cumsum route, which builds a dozen full-length temporaries per call
    (~15% of a Raspberry Pi 4's decode time). With W == L the arithmetic is
    exactly the classic version's."""
    if W < 0:
        W = L
    metric = np.empty(n_out)
    P = np.empty(n_out, np.complex128)
    E12 = np.empty(n_out)
    p = 0j
    e1 = 0.0
    e2 = 0.0
    for d in range(n_out):
        if d % 4096 == 0:
            # exact re-sum
            p = 0j
            e1 = 0.0
            e2 = 0.0
            for i in range(W):
                a = complex(w[d + i])
                b = complex(w[d + i + L])
                p += a.conjugate() * b
                e1 += a.real * a.real + a.imag * a.imag
                e2 += b.real * b.real + b.imag * b.imag
        else:
            # slide by one: pair (w[d-1], w[d-1+L]) leaves, pair
            # (w[d-1+W], w[d-1+W+L]) enters (W == L: w[d-1+L] just moves
            # from the second half-window into the first)
            a0 = complex(w[d - 1])
            aL = complex(w[d - 1 + L])
            aW = complex(w[d - 1 + W])
            b = complex(w[d - 1 + W + L])
            p += aW.conjugate() * b - a0.conjugate() * aL
            pa0 = a0.real * a0.real + a0.imag * a0.imag
            paL = aL.real * aL.real + aL.imag * aL.imag
            paW = aW.real * aW.real + aW.imag * aW.imag
            e1 += paW - pa0
            e2 += (b.real * b.real + b.imag * b.imag) - paL
        P[d] = p
        E12[d] = e1 * e2
        metric[d] = (p.real * p.real + p.imag * p.imag) / (e1 * e2 + 1e-12)
    return metric, P, E12


def schmidl_cox_sync(rx, cfg, search_start=0, search_len=None, prefer_earliest=False):
    """Vectorized (moving-sum) search over every offset in the requested
    range -- equivalent to the old per-offset Python loop but orders of
    magnitude faster, since it computes P/R for every d at once instead of
    one np.sum() call per d.

    prefer_earliest: every frame's preamble is bit-for-bit identical (same
    seed), so if several frames are already sitting in the search window
    (e.g. a fragmented transfer where one big read easily covers more than
    one fragment), the default (picking the single GLOBAL best match) can
    lock onto a LATER frame instead of the nearest one -- fine for a
    one-shot decode, but wrong for a caller that needs to advance a stream
    reader past exactly the frame it just found. Set True to instead take
    the first offset whose metric clears a solid threshold, guaranteeing
    the nearest frame is the one returned."""
    L = cfg.n_fft // 2
    W = cfg.sc_window  # == L for the classic two-half preamble
    max_search_len = len(rx) - search_start - (L + W)
    if search_len is None:
        search_len = max_search_len
    search_len = max(1, min(search_len, max_search_len))
    search_end = search_start + search_len

    window = np.ascontiguousarray(rx[search_start:search_end + L + W])
    metric, P, E12 = _sc_metric(window, L, search_len, W)

    if prefer_earliest:
        # Guard against near-zero-energy windows (e.g. an exact-silence gap
        # between fragments): P and E1*E2 both -> 0 there, and the ratio
        # can spuriously spike above the metric threshold from floating
        # point noise alone, even though there's no real correlation.
        # Median of every 64th value: this floor only has to be the right
        # order of magnitude (1e-9 x typical), and a full median over the
        # whole search window was ~10% of a Raspberry Pi 4's decode time.
        energy_floor = 1e-9 * max(np.median(E12[::64]), 1e-30)
        above = np.where((metric > SYNC_METRIC_MIN) & (E12 > energy_floor))[0]
        if len(above):
            # The metric ramps up smoothly through a transition (e.g.
            # silence into a real preamble) rather than stepping cleanly,
            # so the first index to CROSS the threshold can be a partially-
            # overlapping, worse-than-peak position. Refine by taking the
            # local peak within a window after that crossing -- generous
            # (a few symbol lengths, not just one preamble-half): a sharp
            # silence/signal edge run through a single global tx-side
            # resample (see fft_resample's docstring on why that's fine
            # there but not for a streaming rx buffer) rings for hundreds
            # of samples past the crossing, not just L or 2L of them --
            # confirmed directly on a real capture, where a too-narrow
            # window here locked onto a ramp-partial local max instead of
            # the true ~1.0 peak, landing ~one CP-length off and corrupting
            # the whole frame despite a deceptively fine-looking metric.
            # Still far short of the typical multi-thousand-sample gap
            # between fragments, so this can't jump to a different one.
            d0 = int(above[0])
            # Cap the refine window at wherever metric first drops back
            # below threshold after this crossing (the real end of THIS
            # hump), not just a fixed generous distance -- a fixed
            # cfg.symbol_len*4 window is wide enough, for a short frame
            # (few symbols per fragment), to reach clean past this frame's
            # own peak and into the NEXT frame's preamble entirely.
            # Confirmed for real: with fragment_size=256 (frames only ~3
            # symbols apart), a fixed 4-symbol window spanned a whole
            # separate, later frame whose metric happened to be a hair
            # higher (0.985 vs 0.981), so argmax silently picked THAT one
            # instead -- prefer_earliest returning the wrong, later frame
            # despite a fully valid, closer candidate sitting right there.
            # A real same-frame ramp stays above/near threshold throughout
            # its own hump (confirmed: gaps between genuinely separate
            # frames measure near-zero metric, not a partial ramp), so the
            # next below-threshold sample is a reliable hump boundary.
            hard_cap = min(len(metric), d0 + cfg.symbol_len * 4)
            below_after = np.where(metric[d0:hard_cap] <= SYNC_METRIC_MIN)[0]
            window_end = d0 + int(below_after[0]) if len(below_after) else hard_cap
            window_end = max(window_end, d0 + 1)
            d_rel = d0 + int(np.argmax(metric[d0:window_end]))
        else:
            d_rel = int(np.argmax(metric))
    else:
        d_rel = int(np.argmax(metric))
    best_d = search_start + d_rel
    best_metric = float(metric[d_rel])
    best_P = P[d_rel]

    epsilon = np.angle(best_P) / np.pi
    cfo_hz = epsilon * cfg.carrier_spacing
    start_index = max(0, best_d - cfg.n_cp)
    return start_index, cfo_hz, best_metric


@njit(cache=True)
def _rotate(x, step):
    """x[n] * exp(-1j * step * n) by a rotating phasor (one complex multiply
    per sample instead of a complex exp), recomputed exactly at the start
    of every 1024-sample block so rounding can't accumulate (stays within
    ~1e-13 of the exp version). The output keeps x's precision (complex64
    in the receiver); the phasor itself is always double."""
    n = x.shape[0]
    out = np.empty_like(x)
    w = complex(np.cos(step), -np.sin(step))
    for b0 in range(0, n, 1024):
        ph = step * b0
        z = complex(np.cos(ph), -np.sin(ph))
        for i in range(b0, min(b0 + 1024, n)):
            out[i] = x[i] * z
            z = z * w
    return out


def apply_cfo_correction(x, cfo_hz, fs):
    if _HAVE_NUMBA:
        return _rotate(np.ascontiguousarray(x), 2 * np.pi * cfo_hz / fs)
    n = np.arange(len(x))
    return x * np.exp(-1j * 2 * np.pi * cfo_hz * n / fs)


_FAST_LEN_CACHE = {}


def _fast_fft_len(n):
    """Smallest length >= n whose prime factors are all <= 7 (fast FFT)."""
    m = _FAST_LEN_CACHE.get(n)
    if m is None:
        m = n
        while _max_prime_factor(m) > 7:
            m += 1
        _FAST_LEN_CACHE[n] = m
    return m


def _correlate_valid(a, v):
    """np.correlate(a, v, mode="valid") -- sum_n a[n+k] * conj(v[n]) for
    k = 0..len(a)-len(v) -- computed with FFTs. The direct form costs
    len(v) multiplies per output: ~5.5 million complex multiplies per
    fragment for refine_frame_start's preamble match, ~11% of a Raspberry
    Pi 4's per-fragment decode time on a clean link."""
    n, m = len(a), len(v)
    nfft = _fast_fft_len(n)
    r = np.fft.ifft(np.fft.fft(a, nfft) * np.conj(np.fft.fft(v, nfft)))
    return r[:n - m + 1]


# Safety margin, in samples, the refined frame start is placed EARLY of the
# correlation peak: inside the cyclic prefix an early FFT window is harmless
# (EVM is flat for 0..-150 samples) whereas a late one adds ISI immediately.
TIMING_REFINE_EARLY_MARGIN = 16  # capped at half the CP (mode VU's is only a few samples)
_PREAMBLE_TEMPLATE_CACHE = {}


def refine_frame_start(cfg, rx, start_index, total_cfo_hz, min_peak_ratio=5.0):
    """Sharpen the Schmidl-Cox start estimate by cross-correlating against
    the KNOWN preamble waveform (after CFO correction).

    Schmidl & Cox's metric is flat across the whole cyclic prefix, so at low
    SNR its peak lands anywhere on a ~n_cp-wide plateau (measured at 4-6 dB:
    median 60-190 samples early, ~5% of frames 500+ samples early -- outside
    the CP, so ISI wrecks the frame). The preamble is identical every frame,
    so a matched-filter correlation gives a single sharp peak with ~33 dB
    of processing gain. Returns the refined start_index; falls back to the
    original if the peak isn't clearly dominant (never makes things worse)."""
    key = id(cfg)
    tmpl = _PREAMBLE_TEMPLATE_CACHE.get(key)
    if tmpl is None:
        tmpl = build_preamble(cfg).astype(np.complex64)
        _PREAMBLE_TEMPLATE_CACHE[key] = tmpl
    m = len(tmpl)
    span = 3 * cfg.n_cp
    lo = max(0, start_index - span)
    seg = rx[lo:start_index + m + span]
    if len(seg) < m + 1:
        return start_index
    seg = apply_cfo_correction(seg, total_cfo_hz, cfg.fs)
    corr = np.abs(_correlate_valid(seg, tmpl))
    k = int(np.argmax(corr))
    # Peak must clearly dominate the correlation floor; else keep original.
    floor = np.median(corr) + 1e-12
    if corr[k] / floor < min_peak_ratio:
        return start_index
    return max(0, lo + k - min(TIMING_REFINE_EARLY_MARGIN, cfg.n_cp // 2))


# ---------------------------------------------------------------------------
# Per-symbol pilot mask. For modes with freq_int > 1: a comb that shifts
# by one carrier each symbol, cycling with period freq_int, so every
# symbol carries SOME pilots (better Doppler tracking) and full frequency
# coverage accumulates every freq_int symbols. For freq_int == 1 (Mode D):
# DRM's own spec can't decimate this mode in frequency, so fall back to a
# full-density training symbol every time_int symbols instead.
# ---------------------------------------------------------------------------
def pilot_mask(cfg, symbol_index):
    """Returns a boolean array over cfg.data_bins (local order): True = pilot.
    Cached (read-only) -- there are only freq_int (or 2) distinct masks."""
    if cfg.uses_full_training:
        is_training = (symbol_index % cfg.time_int == 0)
        return _cfg_cache(_PILOT_MASK_CACHE, cfg, ("train", is_training),
                          lambda: np.full(cfg.n_data, is_training))
    shift = symbol_index % cfg.freq_int
    return _cfg_cache(_PILOT_MASK_CACHE, cfg, ("comb", shift),
                      lambda: (np.arange(cfg.n_data) % cfg.freq_int) == shift)


def is_training_symbol(cfg, symbol_index):
    """True only for Mode D style whole-symbol training. For comb-pilot
    modes there's no such thing as a 'training symbol' -- every symbol
    carries both pilots and data -- so this is only meaningful/used for D."""
    return cfg.uses_full_training and (symbol_index % cfg.time_int == 0)


# ---------------------------------------------------------------------------
# OFDM modulator / demodulator
# ---------------------------------------------------------------------------
def ofdm_modulate(cfg, data_bits_per_symbol, symbol_modulations=None):
    """data_bits_per_symbol: list of length n_symbols, each element a 1D
    array of modulation-ready bits for that symbol's DATA carriers (length =
    bits-per-symbol * number of non-pilot carriers in that symbol). Pilot
    carriers are filled automatically.

    symbol_modulations: optional parallel list of "qpsk"/"16qam" tags, one
    per symbol -- lets the header (always QPSK) and payload (whatever
    cfg.data_modulation is) coexist in the same frame. Defaults to all
    QPSK when omitted."""
    n_symbols = len(data_bits_per_symbol)
    if symbol_modulations is None:
        symbol_modulations = ["qpsk"] * n_symbols
    if n_symbols and USE_BATCHED_MODULATOR:
        return _ofdm_modulate_batched(cfg, data_bits_per_symbol, symbol_modulations)
    pilots = pilot_sequence(cfg)
    tx = np.zeros(n_symbols * cfg.symbol_len, dtype=complex)
    for k in range(n_symbols):
        mask = pilot_mask(cfg, k)
        sym = np.zeros(cfg.n_data, dtype=complex)
        sym[mask] = pilots[mask]
        if not np.all(mask):
            mapper = qam16_map if symbol_modulations[k] == "16qam" else qpsk_map
            sym[~mask] = mapper(data_bits_per_symbol[k])
        freq = np.zeros(cfg.n_fft, dtype=complex)
        freq[cfg.fft_bins] = sym
        # NOT an ifftshift()'d array -- cfg.fft_bins already contains
        # negative Python indices, which numpy resolves via ordinary
        # end-wraparound indexing (freq[-5] is the 5th-from-last
        # element). That's already exactly standard (non-shifted) FFT
        # bin order: index -5 lands at the same physical position numpy's
        # own FFT calls frequency -5*fs/n_fft. Adding a real ifftshift()
        # on top (as this used to) re-shifts an array that was never in
        # shifted order to begin with, rotating the whole occupied band
        # by n_fft//2 bins and splitting it into two pieces landing away
        # from true center -- confirmed for real: a ~29-bin (~1200Hz)
        # gap opening up right at true DC for Mode A/20kHz, despite
        # cfg.fft_bins itself being a clean, contiguous, DC-centered
        # range (see _fft_bins_for). ofdm_demodulate must skip the
        # matching fftshift() for the same reason.
        time_sig = np.fft.ifft(freq) * np.sqrt(cfg.n_fft)
        with_cp = np.concatenate([time_sig[-cfg.n_cp:], time_sig])
        tx[k * cfg.symbol_len:(k + 1) * cfg.symbol_len] = with_cp
    return tx


# ofdm_modulate builds all symbols at once (False: the per-symbol reference
# loop above, kept for A/B checks). Same output either way.
USE_BATCHED_MODULATOR = True


def _ofdm_modulate_batched(cfg, data_bits_per_symbol, symbol_modulations):
    """ofdm_modulate for all symbols at once: one frequency-domain array,
    one mapper call per modulation (bits concatenated in symbol order, which
    is how a boolean selection over the array lays them out), one inverse
    FFT along the rows. Element-wise / per-row throughout, so identical to
    the per-symbol loop -- which cost mode VUU (~400 symbols per fragment)
    about a whole Raspberry Pi 4 core in hf_ofdm_tx.py."""
    n = len(data_bits_per_symbol)
    period = cfg.time_int if cfg.uses_full_training else cfg.freq_int
    phase_masks = np.stack([pilot_mask(cfg, p) for p in range(period)])
    masks = phase_masks[np.arange(n) % period]
    syms = np.zeros((n, cfg.n_data), dtype=complex)
    syms[masks] = np.broadcast_to(pilot_sequence(cfg), (n, cfg.n_data))[masks]
    for mod, mapper in (("qpsk", qpsk_map), ("16qam", qam16_map)):
        rows = np.array([k for k in range(n) if (symbol_modulations[k] == "16qam") == (mod == "16qam")
                         and not masks[k].all()], dtype=np.int64)
        if len(rows) == 0:
            continue
        sel = np.zeros((n, cfg.n_data), dtype=bool)
        sel[rows] = ~masks[rows]
        syms[sel] = mapper(np.concatenate([np.asarray(data_bits_per_symbol[k]) for k in rows]))
    freq = np.zeros((n, cfg.n_fft), dtype=complex)
    freq[:, cfg.fft_bins] = syms  # standard (non-shifted) bin order -- see the loop version's comment
    time_sig = np.fft.ifft(freq, axis=1) * np.sqrt(cfg.n_fft)
    return np.concatenate([time_sig[:, cfg.n_fft - cfg.n_cp:], time_sig], axis=1).reshape(-1)


def ofdm_demodulate(cfg, rx, n_symbols):
    """All n_symbols FFT windows (CP stripped) in one batched FFT -- one
    np.fft call per symbol was fine for the HF modes' few dozen symbols per
    frame but cost mode VUU (~400 per fragment) heavily. Each row is still
    transformed on its own, so the result is the same."""
    if n_symbols <= 0:
        return np.zeros((0, cfg.n_data), dtype=complex)
    starts = np.arange(n_symbols) * cfg.symbol_len + cfg.n_cp
    windows = np.asarray(rx)[starts[:, None] + np.arange(cfg.n_fft)[None, :]]
    freq = np.fft.fft(windows, axis=1) / np.sqrt(cfg.n_fft)  # see ofdm_modulate: no fftshift, must mirror it exactly
    return freq[:, cfg.fft_bins].astype(complex, copy=False)


# Compiled comb-pilot equalizer (see _equalize_comb). False = the original
# numpy code below, kept as the readable reference and for A/B testing.
USE_COMPILED_EQUALIZER = _HAVE_NUMBA


@njit(cache=True)
def _unwrap(p):
    """np.unwrap (period 2*pi), same algorithm, for a 1-D float array."""
    out = np.empty_like(p)
    if p.shape[0] == 0:
        return out
    out[0] = p[0]
    corr = 0.0
    for i in range(1, p.shape[0]):
        dd = p[i] - p[i - 1]
        ddmod = (dd + np.pi) % (2 * np.pi) - np.pi
        if ddmod == -np.pi and dd > 0:
            ddmod = np.pi
        if abs(dd) >= np.pi:
            corr += ddmod - dd
        out[i] = p[i] + corr
    return out


@njit(cache=True)
def _interp_clamped(n, xp, fp, out):
    """np.interp(arange(n), xp, fp) for increasing integer xp (clamped ends)."""
    j = 0
    m = xp.shape[0]
    for i in range(n):
        if i <= xp[0]:
            out[i] = fp[0]
        elif i >= xp[m - 1]:
            out[i] = fp[m - 1]
        else:
            while xp[j + 1] < i:
                j += 1
            t = (i - xp[j]) / (xp[j + 1] - xp[j])
            out[i] = fp[j] + t * (fp[j + 1] - fp[j])


@njit(cache=True)
def _equalize_comb(rx_freq, pilots, freq_int, half_window, use_dd, dd_ref):
    """Comb-pilot branch of estimate_and_equalize (see there for the why of
    each step), compiled: it's called ~60 times per fragment (mostly by
    resolve_integer_cfo's candidate sweep) on small arrays, where numpy's
    per-call overhead dominated. use_dd[k] selects the decision-directed
    reference dd_ref[k] for symbol k."""
    n_sym, n = rx_freq.shape
    h_raw = np.empty((n_sym, n), np.complex128)
    mag_i = np.empty(n)
    ph_i = np.empty(n)
    for k in range(n_sym):
        shift = k % freq_int
        if use_dd[k]:
            for c in range(n):
                ref = pilots[c] if c % freq_int == shift else dd_ref[k, c]
                h_raw[k, c] = rx_freq[k, c] / ref
            continue
        n_p = 0
        for c in range(shift, n, freq_int):
            n_p += 1
        xp = np.empty(n_p, np.int64)
        mag = np.empty(n_p)
        ang = np.empty(n_p)
        j = 0
        for c in range(shift, n, freq_int):
            h = rx_freq[k, c] / pilots[c]
            xp[j] = c
            mag[j] = abs(h)
            ang[j] = np.arctan2(h.imag, h.real)
            j += 1
        ph = _unwrap(ang)
        _interp_clamped(n, xp, mag, mag_i)
        _interp_clamped(n, xp, ph, ph_i)
        for c in range(n):
            h_raw[k, c] = mag_i[c] * np.exp(1j * ph_i[c])
    eq = np.empty((n_sym, n), np.complex128)
    eq_soft = np.empty((n_sym, n), np.complex128)
    nan = complex(np.nan, 0.0)  # what np.full(..., np.nan, dtype=complex) holds
    for k in range(n_sym):
        lo = max(0, k - half_window)
        hi = min(n_sym, k + half_window + 1)
        shift = k % freq_int
        for c in range(n):
            s = 0j
            for r in range(lo, hi):
                s += h_raw[r, c]
            h = s / (hi - lo)
            if c % freq_int == shift:
                eq[k, c] = nan
                eq_soft[k, c] = nan
            else:
                eq[k, c] = rx_freq[k, c] / h
                eq_soft[k, c] = rx_freq[k, c] * np.conj(h)
    return eq, eq_soft


@njit(cache=True)
def _equalize_comb_batch(rx3, pilots, freq_int, half_window):
    """_equalize_comb for each of rx3's leading-axis slices (e.g. every
    integer-CFO candidate at once): one call instead of ~61."""
    n_c, n_sym, n = rx3.shape
    eq = np.empty((n_c, n_sym, n), np.complex128)
    eq_soft = np.empty((n_c, n_sym, n), np.complex128)
    use_dd = np.zeros(n_sym, np.bool_)
    dd = np.zeros((1, n), np.complex128)
    for c in range(n_c):
        e, s = _equalize_comb(rx3[c], pilots, freq_int, half_window, use_dd, dd)
        eq[c] = e
        eq_soft[c] = s
    return eq, eq_soft


@njit(cache=True)
def _score_candidates(full_fft, cands, fft_bins, starts, n_fft, pilots, freq_int, half_window,
                      sym_idx, data_bins, data_off, coded_len):
    """resolve_integer_cfo's batch_arrays, compiled: for each integer-CFO
    candidate, shift the header FFT bins, apply the per-symbol window phase,
    equalize, then repetition-combine the soft bits and score the hard-bit
    agreement -- the same arithmetic in the same order as the numpy version
    (identical results), without its ~2.8 ms per fragment of temporaries on
    a Raspberry Pi 4. sym_idx[i] is the i-th header symbol's row in
    full_fft, and data_bins[data_off[i]:data_off[i+1]] its data carriers
    (pilots excluded). Returns (coded_all, conf_all)."""
    n_c = cands.shape[0]
    n_sym = full_fft.shape[0]
    n = fft_bins.shape[0]
    n_hdr = data_off.shape[0] - 1
    n_soft = 2 * data_off[n_hdr]
    n_full = n_soft // coded_len
    rem = n_soft - n_full * coded_len
    n_rep = (n_soft // coded_len) * coded_len
    coded_all = np.zeros((n_c, coded_len))
    conf_all = np.zeros(n_c)
    use_dd = np.zeros(n_sym, np.bool_)
    dd = np.zeros((1, n), np.complex128)
    hf = np.empty((n_sym, n), np.complex128)
    soft = np.empty(n_soft)
    hard = np.empty(n_soft, np.uint8)
    for ci in range(n_c):
        cand = cands[ci]
        for s in range(n_sym):
            ph = np.exp(1j * 2 * np.pi * cand * starts[s] / n_fft)
            for j in range(n):
                hf[s, j] = full_fft[s, (fft_bins[j] - cand) % n_fft] * ph
        eq, eqs = _equalize_comb(hf, pilots, freq_int, half_window, use_dd, dd)
        # soft (from eq_soft) and hard (from eq) bits, [re, im] per data carrier
        p = 0
        for i in range(n_hdr):
            row = sym_idx[i]
            for q in range(data_off[i], data_off[i + 1]):
                c = data_bins[q]
                soft[p] = eqs[row, c].real
                soft[p + 1] = eqs[row, c].imag
                hard[p] = 1 if eq[row, c].real < 0 else 0
                hard[p + 1] = 1 if eq[row, c].imag < 0 else 0
                p += 2
        # repetition-combine: plain sequential sums, as numpy's axis-1 sum
        for r in range(n_full):
            for b in range(coded_len):
                coded_all[ci, b] += soft[r * coded_len + b]
        for b in range(rem):
            coded_all[ci, b] += soft[n_full * coded_len + b]
        for b in range(coded_len):
            coded_all[ci, b] /= max(n_full, 1) + (1 if b < rem else 0)
        if n_rep == 0:
            conf_all[ci] = 0.0
        else:
            n_r = n_rep // coded_len
            agree = 0
            for b in range(coded_len):
                ones = 0
                for r in range(n_r):
                    ones += hard[r * coded_len + b]
                maj = 1 if ones / n_r >= 0.5 else 0
                for r in range(n_r):
                    if hard[r * coded_len + b] == maj:
                        agree += 1
            conf_all[ci] = agree / n_rep
    return coded_all, conf_all


TIME_SMOOTHING_HALF_WINDOW = 3
# Alternative (shorter) smoothing half-windows decode_frame retries with when
# the first pass fails CRC. A long window averages away pilot noise (best on
# a static channel at low SNR) but smears a fast-fading channel: measured on
# a 4 Hz-Doppler two-path channel, +-1 symbol cut LDPC frame errors from 22%
# to 0% at 10 dB versus +-3, while +-3 stays better for Viterbi on static
# AWGN at low SNR. Trying both, only after a failure, gets the best of each.
TIME_SMOOTHING_RETRY_HALF_WINDOWS = (1,)


def estimate_and_equalize(cfg, rx_freq, n_symbols, dd_ref=None, time_half_window=None):
    """Per-symbol channel estimate + equalization. Returns (eq, eq_soft):
    eq is the standard zero-forced equalized symbol (rx/h) used for hard
    decisions; eq_soft is a matched-filter-weighted version (rx*conj(h),
    equal to eq*|h|^2) used for soft-decision decoding.

    dd_ref: optional {symbol_index: reference_array} for decision-directed
    re-estimation (comb-pilot modes only -- see decode_frame's retry
    path). reference_array must cover EVERY carrier in cfg.n_data (pilot
    positions get the known pilot value, data positions get the
    decoder's own re-encoded guess from a first decode pass). When
    given for a symbol, that symbol's channel estimate comes from
    dividing directly at every carrier instead of interpolating from
    sparse pilots alone -- strictly more information when the guess is
    right, at the usual decision-directed risk of a wrong guess
    corrupting its own carrier's estimate (bounded by only ever
    accepting this retry's result if it goes on to pass CRC -- see
    decode_frame).

    Why two versions: zero-forcing divides by h, which at a deep fade
    (small |h|) amplifies noise right along with the signal -- a mostly-
    noise sample can come out at a magnitude that LOOKS like a confident
    symbol even though it carries almost no information. The matched-
    filter version naturally shrinks low-|h| (unreliable) samples instead
    of blowing them up, which is what a soft-decision metric needs to
    correctly down-weight bad carriers. The two have identical sign (so
    hard decisions are unaffected either way) and differ only in
    magnitude -- see qpsk_soft / fec_decode_soft.

    For comb-pilot modes: each symbol's own pilots (no time dependency).
    For full-training modes (D): time-interpolate between the nearest
    full training symbols, as before."""
    pilots = pilot_sequence(cfg)
    eq = np.full((n_symbols, cfg.n_data), np.nan, dtype=complex)
    eq_soft = np.full((n_symbols, cfg.n_data), np.nan, dtype=complex)
    all_idx = np.arange(cfg.n_data)

    if cfg.uses_full_training:
        training_idx = [k for k in range(n_symbols) if is_training_symbol(cfg, k)]
        h_at_training = np.array([rx_freq[k] / pilots for k in training_idx]) if training_idx else None
        for k in range(n_symbols):
            if is_training_symbol(cfg, k):
                continue
            prev_idxs = [i for i in training_idx if i <= k]
            next_idxs = [i for i in training_idx if i >= k]
            if prev_idxs and next_idxs:
                i0, i1 = prev_idxs[-1], next_idxs[0]
            elif prev_idxs:
                i0 = i1 = prev_idxs[-1]
            else:
                i0 = i1 = next_idxs[0]
            h0 = h_at_training[training_idx.index(i0)]
            h1 = h_at_training[training_idx.index(i1)]
            h = h0 if i1 == i0 else (1 - (k - i0) / (i1 - i0)) * h0 + ((k - i0) / (i1 - i0)) * h1
            eq[k] = rx_freq[k] / h
            eq_soft[k] = rx_freq[k] * np.conj(h)
    elif USE_COMPILED_EQUALIZER:
        half_window = TIME_SMOOTHING_HALF_WINDOW if time_half_window is None else time_half_window
        use_dd = np.zeros(n_symbols, np.bool_)
        dd_arr = np.zeros((n_symbols if dd_ref else 1, cfg.n_data), np.complex128)
        if dd_ref:
            for k, ref in dd_ref.items():
                if 0 <= k < n_symbols:
                    use_dd[k] = True
                    dd_arr[k] = ref
        eq, eq_soft = _equalize_comb(np.ascontiguousarray(rx_freq[:n_symbols], np.complex128),
                                     np.ascontiguousarray(pilots, np.complex128),
                                     cfg.freq_int, half_window, use_dd, dd_arr)
    else:
        h_raw = np.empty((n_symbols, cfg.n_data), dtype=complex)
        for k in range(n_symbols):
            mask = pilot_mask(cfg, k)
            if dd_ref is not None and k in dd_ref:
                ref = np.where(mask, pilots, dd_ref[k])
                h_raw[k] = rx_freq[k] / ref
                continue
            pilot_idx = all_idx[mask]
            h_at_pilots = rx_freq[k][mask] / pilots[mask]
            # Interpolate MAGNITUDE and (unwrapped) PHASE separately, not
            # real/imaginary parts directly: a residual timing offset (even
            # a small one, invisible in the sync metric) makes h's phase
            # rotate steadily across carriers while its magnitude stays
            # essentially constant. Linearly interpolating real/imag
            # independently between two pilots that are far apart in phase
            # draws a straight line BETWEEN two points on a circle, which
            # cuts through the middle -- producing a spurious near-zero
            # magnitude "dip" partway between them even though the true
            # channel never faded there. That fake dip then gets
            # zero-forced/matched-filtered like a real deep fade, silently
            # destroying the soft-decision FEC input for every carrier near
            # it (confirmed directly: h_at_pilots stayed 0.099 everywhere,
            # h_interp dropped to 0.005 between two of them, and that
            # alone was enough to corrupt an otherwise-perfect decode).
            h_mag_interp = np.interp(all_idx, pilot_idx, np.abs(h_at_pilots))
            h_phase_interp = np.interp(all_idx, pilot_idx, np.unwrap(np.angle(h_at_pilots)))
            h_raw[k] = h_mag_interp * np.exp(1j * h_phase_interp)

        # Smooth each carrier's channel estimate across nearby symbols in
        # TIME before equalizing. Each symbol's h_raw is a noisy, INDEPENDENT
        # measurement of the same slowly-varying channel (an HF channel's
        # coherence time is typically well over 100ms -- several OFDM
        # symbols here, at 26.69ms each -- outside unusually fast Doppler),
        # so estimating it fresh from scratch every single symbol throws
        # away free averaging gain a real DRM-class receiver's 2D (time+
        # frequency) Wiener filtering exploits. Confirmed for real: a
        # synthetic, exactly-known 10dB AWGN test (h truly constant at 1.0
        # throughout, zero fading) still only decoded 49/64 fragments before
        # this fix, well short of DRM's own published ~2-5dB threshold for
        # the identical modulation/code rate at that SNR -- consistent with
        # unnecessary pilot-estimation noise being the gap, not a genuine
        # code-rate/SNR mismatch. A plain symmetric moving average over a
        # modest window (half-window below) cancels pilot noise by roughly
        # sqrt(window) without needing to know the true coherence time; it's
        # deliberately narrow enough to still track real HF fading rather
        # than smearing across a genuinely fast-changing channel.
        half_window = TIME_SMOOTHING_HALF_WINDOW if time_half_window is None else time_half_window
        for k in range(n_symbols):
            lo = max(0, k - half_window)
            hi = min(n_symbols, k + half_window + 1)
            h_smooth = np.mean(h_raw[lo:hi], axis=0)
            mask = pilot_mask(cfg, k)
            eq[k] = rx_freq[k] / h_smooth
            eq_soft[k] = rx_freq[k] * np.conj(h_smooth)
            eq[k][mask] = np.nan
            eq_soft[k][mask] = np.nan

    return eq, eq_soft


# ---------------------------------------------------------------------------
# Framing: preamble + a stream of OFDM symbols, each carrying a mix of
# pilots (auto-inserted) and data (header first, then payload).
# ---------------------------------------------------------------------------
def _repetition_encode(bits, n_slots):
    reps = int(np.ceil(n_slots / len(bits)))
    return np.tile(bits, reps)[:n_slots]


def _repetition_decode(bits, orig_len):
    n = (len(bits) // orig_len) * orig_len
    bits = bits[:n].reshape(-1, orig_len)
    return (bits.mean(axis=0) >= 0.5).astype(np.uint8)


def _repetition_soft_combine(soft_values, orig_len):
    """Like _repetition_decode, but averages the continuous soft (un-
    sliced) values instead of hard-majority-voting bits -- the correct way
    to combine repeated noisy copies of the SAME transmitted value before
    a soft-decision decode, since it keeps confidence information the
    hard-decision version throws away. Used for the header, which (unlike
    the payload) is short enough to fit multiple repeated copies of its
    FEC-coded bits inside its one OFDM symbol -- see build_frame.

    Folds a trailing PARTIAL repeat into the average (giving its covered
    positions one extra real observation) instead of discarding it
    outright. With a short header symbol capacity relative to the coded
    length, that tail isn't a rounding sliver -- confirmed for real: FEC-
    coding the header dropped its repeat count from ~6x to ~2x within the
    same symbol capacity, and naively truncating the resulting ~30%
    leftover capacity actually made 10dB-SNR decode WORSE than before the
    header even had FEC at all, by throwing away most of the averaging
    that used to compensate for having no coding gain."""
    n_full = len(soft_values) // orig_len
    total = soft_values[:n_full * orig_len].reshape(n_full, orig_len).sum(axis=0)
    counts = np.full(orig_len, max(n_full, 1), dtype=float)
    remainder = len(soft_values) - n_full * orig_len
    if remainder > 0:
        total[:remainder] += soft_values[n_full * orig_len:]
        counts[:remainder] += 1
    return total / counts


def _repetition_confidence(bits, orig_len):
    """Fraction of repeated copies that agree with the majority vote -- a
    reference-free quality score (no payload/CRC needed) for how clean the
    header symbol's repetition-coded bits are. Used to pick the right
    INTEGER carrier-spacing CFO offset:
    Schmidl & Cox only resolves the fractional part of a frequency offset
    (within +-half a carrier spacing), so a real oscillator offset of many
    carrier spacings leaves the whole constellation sitting on the wrong
    carriers even after 'correction' -- the header decodes to noise at the
    wrong integer offset and cleanly at the right one, which this measures."""
    n = (len(bits) // orig_len) * orig_len
    if n == 0:
        return 0.0
    bits = bits[:n].reshape(-1, orig_len)
    majority = (bits.mean(axis=0) >= 0.5)
    return float((bits == majority).mean())


CFO_PRIOR_PENALTY_PER_CARRIER = 2.0
# See resolve_integer_cfo's prior-first shortcut. False = always the full search.
PRIOR_FIRST_CFO_SEARCH = True


def resolve_integer_cfo(cfg, rx_after_preamble, search_range=30, prior_k=None):
    """Search extra integer multiples of carrier_spacing on top of the
    already fractional-CFO-corrected samples, picking whichever makes the
    header's FEC-coded bits fit a valid codeword best. Returns (best_k,
    best_confidence); the caller should re-apply apply_cfo_correction
    with an extra best_k * cfg.carrier_spacing Hz.

    Scores each of the up to 61 candidates by the soft Viterbi decoder's
    own final path metric (see fec_decode_soft's return_metric) rather
    than a plain repetition-agreement count. This matters specifically at
    low SNR: confirmed for real at an exact, synthetic 10dB AWGN SNR
    (zero fading, zero hardware artifacts) that _repetition_confidence's
    coarse hard-bit-agreement heuristic occasionally rated a WRONG
    candidate as good as or better than the true one, silently locking
    onto the wrong integer CFO for otherwise perfectly decodable
    fragments -- the path metric reflects how well the ENTIRE received
    sequence fits a valid codeword, a much stronger signal than counting
    how often repeated copies merely agree with each other.

    prior_k (optional): the caller's best guess at the true candidate
    (e.g. derived from the previous fragment's CONFIRMED CFO -- a real
    oscillator can't jump by hundreds of Hz between two adjacent
    fragments, confirmed throughout real captures this project has taken:
    CFO always drifts smoothly, at most a few Hz per fragment). When
    given, candidates are penalized in proportion to their distance from
    it -- inspired by the DRM reference receiver Dream averaging its own
    coarse-CFO decision over multiple symbols rather than trusting any
    single noisy observation (src/sync/FreqSyncAcq.cpp); this is our
    equivalent using the run of PAST fragments as that extra evidence,
    since averaging within one fragment isn't available before its
    header is known. A small tie-breaking nudge, not an override: still
    loses to a clearly-better metric on a distant candidate, but tips the
    close, low-SNR-driven ties this was added for."""
    header_symbol_indices = _header_symbol_plan(cfg)
    n_symbols_for_header = header_symbol_indices[-1] + 1
    needed_samples = n_symbols_for_header * cfg.symbol_len
    if len(rx_after_preamble) < needed_samples:
        return 0, 0.0

    MAX_REASONABLE_PAYLOAD_BYTES = 10_000_000
    header_coded_len = fec_coded_len(HEADER_BITS)
    masks = [pilot_mask(cfg, idx) for idx in header_symbol_indices]
    # Fast path: shifting by a whole number of carrier spacings (fs/n_fft)
    # is, inside each FFT window, exactly a circular shift of the FFT bins
    # times a known per-symbol phase (the window starts at sample
    # s*symbol_len + n_cp, not 0). So do the header FFTs ONCE and turn each
    # of the 61 candidates into an index shift + multiply, instead of 61
    # frequency shifts + 61 sets of FFTs. Mathematically identical to
    # apply_cfo_correction + ofdm_demodulate per candidate.
    exact_bins = abs(cfg.carrier_spacing * cfg.n_fft - cfg.fs) <= 1e-9 * cfg.fs
    if exact_bins:
        seg = rx_after_preamble[:needed_samples]
        starts = np.arange(n_symbols_for_header) * cfg.symbol_len + cfg.n_cp
        full_fft = np.fft.fft(np.stack([seg[s:s + cfg.n_fft] for s in starts]), axis=1) / np.sqrt(cfg.n_fft)
    batched = exact_bins and not cfg.uses_full_training and USE_COMPILED_EQUALIZER

    if batched:
        sym_idx_arr = np.asarray(header_symbol_indices, np.int64)
        data_lists = [np.nonzero(~m)[0] for m in masks]
        data_bins_arr = np.concatenate(data_lists).astype(np.int64)
        data_off_arr = np.concatenate([[0], np.cumsum([len(d) for d in data_lists])]).astype(np.int64)
        fft_bins_arr = np.asarray(cfg.fft_bins, np.int64)
        starts_arr = np.asarray(starts, np.int64)
        pilots_arr = np.ascontiguousarray(pilot_sequence(cfg), np.complex128)
        full_fft_c = np.ascontiguousarray(full_fft, np.complex128)

    def batch_arrays(cand_arr):
        # Compiled batched path (comb-pilot modes): see _score_candidates;
        # same results as _batch_arrays_numpy below.
        return _score_candidates(full_fft_c, np.asarray(cand_arr, np.int64), fft_bins_arr, starts_arr,
                                 cfg.n_fft, pilots_arr, cfg.freq_int, TIME_SMOOTHING_HALF_WINDOW,
                                 sym_idx_arr, data_bins_arr, data_off_arr, header_coded_len)

    def _batch_arrays_numpy(cand_arr):
        # Batched path (comb-pilot modes): equalize, soft-extract, repetition-
        # combine and score the given candidates as arrays, in the same
        # arithmetic order as the per-candidate path below; only the Viterbi
        # decode and header parse stay per candidate.
        n_c = len(cand_arr)
        bins_all = (cfg.fft_bins[None, :] - cand_arr[:, None]) % cfg.n_fft
        phase_all = np.exp(1j * 2 * np.pi * cand_arr[:, None] * starts[None, :] / cfg.n_fft)
        hf3 = np.ascontiguousarray(full_fft[:, bins_all].transpose(1, 0, 2) * phase_all[:, :, None])
        eq3, eqs3 = _equalize_comb_batch(hf3, np.ascontiguousarray(pilot_sequence(cfg), np.complex128),
                                         cfg.freq_int, TIME_SMOOTHING_HALF_WINDOW)
        soft_all = np.concatenate([
            np.stack([eqs3[:, idx, ~m].real, eqs3[:, idx, ~m].imag], axis=2).reshape(n_c, -1)
            for idx, m in zip(header_symbol_indices, masks)], axis=1)
        n_full = soft_all.shape[1] // header_coded_len
        coded_all = soft_all[:, :n_full * header_coded_len].reshape(n_c, n_full, header_coded_len).sum(axis=1)
        counts = np.full(header_coded_len, max(n_full, 1), dtype=float)
        rem = soft_all.shape[1] - n_full * header_coded_len
        if rem > 0:
            coded_all[:, :rem] += soft_all[:, n_full * header_coded_len:]
            counts[:rem] += 1
        coded_all = coded_all / counts
        hard_all = np.concatenate([
            np.stack([eq3[:, idx, ~m].real < 0, eq3[:, idx, ~m].imag < 0], axis=2).reshape(n_c, -1).astype(np.uint8)
            for idx, m in zip(header_symbol_indices, masks)], axis=1)
        n_rep = (hard_all.shape[1] // header_coded_len) * header_coded_len
        if n_rep == 0:
            conf_all = np.zeros(n_c)
        else:
            reps = hard_all[:, :n_rep].reshape(n_c, -1, header_coded_len)
            majority = reps.mean(axis=1) >= 0.5
            conf_all = (reps == majority[:, None, :]).mean(axis=(1, 2))
        return coded_all, conf_all

    def score(cand_list):
        """Returns (best_k, best_conf, best_sane_k, best_sane_conf) over
        cand_list, visited in the given (ascending) order."""
        best_k, best_metric, best_conf = 0, np.inf, -1.0
        best_sane_k, best_sane_metric, best_sane_conf = None, np.inf, -1.0
        if batched:
            coded_all, conf_all = batch_arrays(np.asarray(cand_list))
        for ci, cand in enumerate(cand_list):
            if batched:
                header_coded_soft = coded_all[ci]
                conf = float(conf_all[ci])
            else:
                if exact_bins:
                    bins = (cfg.fft_bins - cand) % cfg.n_fft
                    phase = np.exp(1j * 2 * np.pi * cand * starts / cfg.n_fft)
                    header_freq = full_fft[:, bins] * phase[:, None]
                else:
                    shifted = apply_cfo_correction(rx_after_preamble[:needed_samples],
                                                    -cand * cfg.carrier_spacing, cfg.fs)
                    header_freq = ofdm_demodulate(cfg, shifted, n_symbols_for_header)
                header_eq, header_eq_soft = estimate_and_equalize(cfg, header_freq, n_symbols_for_header)
                header_symbol_soft = np.concatenate([
                    qpsk_soft(header_eq_soft[idx][~m]) for idx, m in zip(header_symbol_indices, masks)
                ])
                header_coded_soft = _repetition_soft_combine(header_symbol_soft, header_coded_len)
                # Callers (e.g. hf_ofdm_rx.py's CFO-continuity gate, verbose
                # logging) expect a familiar 0-1 "confidence" score, which the raw
                # path metric (an unbounded summed squared distance) isn't --
                # report the repetition-agreement score for whichever candidate
                # the (much more reliable) path metric actually picks, rather
                # than changing what "confidence" means to every caller.
                header_demap = np.concatenate([
                    qpsk_demap(header_eq[idx][~m]) for idx, m in zip(header_symbol_indices, masks)
                ])
                conf = _repetition_confidence(header_demap, header_coded_len)
            header_bits, metric = fec_decode_soft(header_coded_soft, HEADER_BITS, return_metric=True)
            biased_metric = metric
            if prior_k is not None:
                biased_metric += abs(cand - prior_k) * CFO_PRIOR_PENALTY_PER_CARRIER
            if biased_metric < best_metric:
                best_metric, best_k, best_conf = biased_metric, cand, conf
            # Prefer a candidate whose header actually parses to a sane,
            # checksum-valid header over pure path-metric, which can
            # occasionally pick a wrong candidate by a hair when the sync
            # itself is a little imperfect (e.g. metric high but not 1.0).
            # The checksum is a much stronger "wrong" signal than the range
            # check alone: a range check only catches a payload_len that
            # happens to land out of bounds, while the checksum catches any
            # corruption of the header's own fields directly.
            header_fields_bits = header_bits[:HEADER_LEN_BITS + HEADER_CRC_BITS]
            raw_len = int(np.packbits(header_bits[:HEADER_LEN_BITS]).view(">u4")[0])
            payload_len = int(raw_len & 0x7FFFFFFF)
            header_checksum = int(np.packbits(header_bits[HEADER_LEN_BITS + HEADER_CRC_BITS:])[0])
            header_ok = (0 <= payload_len <= MAX_REASONABLE_PAYLOAD_BYTES
                         and header_checksum == _header_checksum8(header_fields_bits))
            if header_ok and biased_metric < best_sane_metric:
                best_sane_metric, best_sane_k, best_sane_conf = biased_metric, cand, conf
        return best_k, best_conf, best_sane_k, best_sane_conf

    # Prior-first shortcut: a real oscillator drifts at most a few Hz between
    # fragments (see prior_k above), so the true candidate is almost always
    # prior_k or a neighbour. Score just those; if one yields a sane,
    # checksum-valid header, take it -- the full search would have to find
    # a sane candidate farther away with a path metric better by more than
    # CFO_PRIOR_PENALTY_PER_CARRIER per carrier of distance to overrule it.
    # Only if none of them gives a sane header, fall back to the full
    # search. Cuts the per-fragment search from 61 candidates to 3 in
    # steady state -- the difference between fitting a Raspberry Pi 4's
    # real-time budget or not.
    if PRIOR_FIRST_CFO_SEARCH and prior_k is not None:
        local = [k for k in (prior_k - 1, prior_k, prior_k + 1) if -search_range <= k <= search_range]
        if local:
            _, _, sane_k, sane_conf = score(local)
            if sane_k is not None:
                return sane_k, sane_conf
    best_k, best_conf, best_sane_k, best_sane_conf = score(list(range(-search_range, search_range + 1)))
    if best_sane_k is not None:
        return best_sane_k, best_sane_conf
    return best_k, best_conf


def resolve_timing_offset(cfg, rx, start_index, total_cfo_hz, max_needed_len, candidate_deltas):
    """Same technique as resolve_integer_cfo (score candidates by the
    header's own soft Viterbi path metric), applied to small TIMING
    offsets around a Schmidl & Cox lock instead of integer CFO. Exists so
    a caller can search a wide range of candidate sample offsets cheaply
    -- scoring each needs only a header-sized demod+equalize+Viterbi,
    nowhere near the cost of a full decode_frame() over the whole
    payload -- and then pay for exactly ONE real decode_frame() call, at
    whichever offset wins, instead of running the full expensive decode
    at every candidate the way the old +-2-sample nudge_retry probe did.

    Motivated by a real capture: being off by ~1 CP length (n_cp) from
    the true optimal alignment can land on a HIGHER schmidl_cox_sync
    metric yet decode with an uncorrectable, bursty error pattern, while
    an offset a couple hundred samples away (lower raw sync metric) can
    decode cleanly -- a classic OFDM CP-length sync ambiguity. The old
    nudge only ever tried +-2 samples, nowhere near enough range to find
    that; sweeping the header metric across the full +-n_cp range at
    header-only cost makes covering it affordable.

    Returns (best_delta, best_metric, best_header_ok) -- best_delta is 0
    if candidate_deltas is empty or nothing in range could be scored."""
    header_symbol_indices = _header_symbol_plan(cfg)
    n_symbols_for_header = header_symbol_indices[-1] + 1
    needed_samples = n_symbols_for_header * cfg.symbol_len
    header_coded_len = fec_coded_len(HEADER_BITS)
    masks = [pilot_mask(cfg, idx) for idx in header_symbol_indices]

    best_delta, best_metric = 0, np.inf
    best_sane_delta, best_sane_metric = None, np.inf
    # Only the header-length prefix after the preamble is ever read below
    # -- unlike max_needed_len (sized for a full decode_frame() over the
    # WHOLE payload), so slice down to exactly that before paying for
    # apply_cfo_correction's per-sample complex rotation, instead of
    # rotating the entire (much longer) fragment on every one of the
    # candidates in candidate_deltas just to throw away all but its first
    # few thousand samples each time.
    probe_len = cfg.preamble_len + needed_samples
    for delta in candidate_deltas:
        probe_start = start_index + delta
        if probe_start < 0:
            continue
        probe_slice = rx[probe_start:probe_start + probe_len]
        if len(probe_slice) < probe_len:
            continue
        probe_corrected = apply_cfo_correction(probe_slice, total_cfo_hz, cfg.fs)
        after_preamble = probe_corrected[cfg.preamble_len:]
        header_freq = ofdm_demodulate(cfg, after_preamble[:needed_samples], n_symbols_for_header)
        _, header_eq_soft = estimate_and_equalize(cfg, header_freq, n_symbols_for_header)
        header_symbol_soft = np.concatenate([
            qpsk_soft(header_eq_soft[idx][~m]) for idx, m in zip(header_symbol_indices, masks)
        ])
        header_coded_soft = _repetition_soft_combine(header_symbol_soft, header_coded_len)
        header_bits, metric = fec_decode_soft(header_coded_soft, HEADER_BITS, return_metric=True)
        if metric < best_metric:
            best_metric, best_delta = metric, delta
        # Same "prefer a checksum-valid header" preference as
        # resolve_integer_cfo, and for the same reason -- pure path
        # metric can occasionally favor a wrong-but-close candidate.
        header_fields_bits = header_bits[:HEADER_LEN_BITS + HEADER_CRC_BITS]
        raw_len = int(np.packbits(header_bits[:HEADER_LEN_BITS]).view(">u4")[0])
        payload_len = int(raw_len & 0x7FFFFFFF)
        header_checksum = int(np.packbits(header_bits[HEADER_LEN_BITS + HEADER_CRC_BITS:])[0])
        header_ok = (0 <= payload_len <= 10_000_000
                     and header_checksum == _header_checksum8(header_fields_bits))
        if header_ok and metric < best_sane_metric:
            best_sane_metric, best_sane_delta = metric, delta
    if best_sane_delta is not None:
        return best_sane_delta, best_sane_metric, True
    return best_delta, best_metric, False


def _n_data_carriers(cfg, symbol_index):
    """Non-pilot carriers in a symbol. pilot_mask repeats every freq_int
    symbols (time_int for full-training modes), so this is memoised per
    config on that phase -- it was called per symbol by every frame-length
    loop: ~56k calls for 40 mode VU fragments, a quarter of the receiver's
    time (cheap for the HF modes' few dozen symbols per frame, not for
    mode VU's ~400). Same values as summing the mask each time."""
    period = cfg.time_int if cfg.uses_full_training else cfg.freq_int
    cache = cfg.__dict__.setdefault("_n_data_cache", {})
    phase = symbol_index % period
    n = cache.get(phase)
    if n is None:
        n = int((~pilot_mask(cfg, symbol_index)).sum())
        cache[phase] = n
    return n


def _symbols_until(cfg, k0, needed_bits):
    """Index just past the payload symbols, starting at symbol k0, that
    hold needed_bits -- the same as adding _payload_bits_capacity one
    symbol at a time, but whole pilot periods at once (the capacity
    repeats with the pilot pattern)."""
    period = cfg.time_int if cfg.uses_full_training else cfg.freq_int
    per_period = sum(_payload_bits_capacity(cfg, k0 + i) for i in range(period))
    k, got = k0, 0
    if per_period > 0:
        full = max(0, needed_bits // per_period - 1)
        k, got = k0 + full * period, full * per_period
    while got < needed_bits:
        got += _payload_bits_capacity(cfg, k)
        k += 1
    return k


def _data_bits_capacity(cfg, symbol_index):
    """Bit capacity at the QPSK rate the preamble/pilots/header always use,
    regardless of cfg.data_modulation -- see _payload_bits_capacity for the
    PAYLOAD's own (possibly higher) rate."""
    return _n_data_carriers(cfg, symbol_index) * 2


def _payload_bits_capacity(cfg, symbol_index):
    """Bit capacity for a PAYLOAD symbol at cfg.data_modulation's rate --
    double _data_bits_capacity's when data_modulation is 16qam."""
    return _n_data_carriers(cfg, symbol_index) * data_bits_per_symbol(cfg)


def estimate_payload_bitrate(cfg):
    """Average usable PAYLOAD bitrate (bps) after the rate-1/2 FEC, i.e.
    what's actually left for your own data once pilots and coding
    overhead are paid for. Averaged over enough symbols to correctly
    account for Mode D's periodic all-pilot training symbols (a single
    symbol's capacity would badly over- or under-estimate depending on
    whether it landed on one); every other mode's capacity is already
    identical symbol to symbol, so the average is exact there regardless
    of window size. Doesn't subtract the header's own (small, fixed)
    overhead -- see HEADER_MIN_REPEATS/_header_symbol_plan -- since that
    amortizes differently depending on --fragment-size, which this
    function doesn't know about."""
    window = max(40, 4 * cfg.time_int)
    avg_cap_bits = sum(_payload_bits_capacity(cfg, k) for k in range(window)) / window
    return (avg_cap_bits / 2) / (cfg.symbol_len / cfg.fs)


def estimate_effective_bitrate(cfg, fragment_size_bytes, fragment_gap_ms=0.0):
    """Real end-to-end payload bitrate for a given --fragment-size (and,
    if known, --fragment-gap-ms): fragment bytes divided by the ACTUAL
    on-air time one fragment costs, header symbols and FEC/interleaver
    flush overhead included via frame_symbol_count, plus any silence gap
    between fragments -- unlike estimate_payload_bitrate's per-symbol
    ceiling, which is what you'd get with infinite fragment size and zero
    gap. Smaller fragments cost proportionally more here since the fixed-
    size header is paid for more often; see hf_ofdm_tx.py's --fragment-
    size help for that trade-off.

    +1 symbol: frame_symbol_count only counts header+data symbols --
    build_frame prepends a full-length Schmidl & Cox preamble symbol
    (build_preamble returns exactly n_cp+n_fft samples, i.e. one whole
    cfg.symbol_len) to EVERY frame, which this estimate has to include
    too or it silently understates real on-air time. Confirmed for
    real: this was a genuine, previously undiscovered bug, and not a
    small one -- at Mode A/80% occupancy/QPSK/1024B fragments, omitting
    it understated the real per-fragment budget by 213ms vs the true
    240ms (a real one-symbol/8 ~= 12.5% overestimate of capacity), which
    was consumed as ZERO actual margin (not the ~11% this function's
    callers believed they had) for any bitrate landing exactly on a
    packing boundary -- e.g. 31.6kbps needing exactly 12 packets/
    fragment, whose required and (wrongly) modeled available fragment
    rates came out numerically equal, when the TRUE available rate was
    lower still. That's a flat-out capacity overestimate, not timing
    jitter -- distinct from (and on top of) TIMING_MARGIN_FRAC's own
    real-world-jitter margin in media_tx_gui.py."""
    n_symbols = frame_symbol_count(cfg, fragment_size_bytes)
    frame_time_s = (n_symbols * cfg.symbol_len + cfg.preamble_len) / cfg.fs  # preamble: one symbol_len for 2 halves
    total_time_s = frame_time_s + fragment_gap_ms / 1000.0
    return (fragment_size_bytes * 8) / total_time_s


HEADER_MIN_REPEATS = 3
HEADER_MAX_SYMBOLS = 4


def _header_symbol_plan(cfg):
    """The consecutive symbol indices assigned to the header, chosen so
    their combined data-bit capacity holds at least HEADER_MIN_REPEATS
    full repeats of the header's FEC-coded bits -- instead of whatever
    repetition happened to fit in exactly one symbol, which is fully
    dictated by the payload's own occupancy/rate choice (more carriers
    at wide occupancy incidentally means more header repeats, fewer at
    narrow occupancy incidentally means less). Confirmed for real: even
    at the widest (20kHz) occupancy, one symbol only fit ~2x repetition,
    and a strong-metric, correctly-CFO-resolved preamble lock still
    produced a garbage header at that repeat count (see
    HEADER_OWN_CRC_BITS). Modeled on DRM, which never lets its FAC (the
    header-equivalent fast-access channel) inherit the MSC's (payload's)
    chosen protection level -- the FAC always gets its own fixed,
    conservative budget regardless of channel/rate conditions
    (src/FAC/FAC.cpp in the Dream reference receiver). Capped at
    HEADER_MAX_SYMBOLS so a pathologically narrow occupancy can't make
    the header balloon to dominate the frame."""
    header_coded_len = fec_coded_len(HEADER_BITS)
    target = HEADER_MIN_REPEATS * header_coded_len
    indices = []
    total = 0
    k = 0
    while total < target and len(indices) < HEADER_MAX_SYMBOLS:
        cap = _data_bits_capacity(cfg, k)
        if cap > 0:
            indices.append(k)
            total += cap
        elif indices:
            break
        k += 1
    return indices


def frame_symbol_count(cfg, payload_len_bytes):
    """Total OFDM symbols (header + data) a frame carrying a payload of
    this length occupies -- mirrors decode_frame's own accounting, so a
    caller that already knows a decoded payload's length (e.g. to advance
    a stream reader past exactly this frame, for a multi-frame/fragmented
    transfer) doesn't have to duplicate this logic or guess."""
    header_symbol_indices = _header_symbol_plan(cfg)
    is_ldpc = getattr(cfg, "fec_scheme", "viterbi").lower() == "ldpc"
    if is_ldpc:
        needed_coded_bits = ldpc.ldpc_coded_len(payload_len_bytes * 8)
    else:
        needed_coded_bits = fec_coded_len(payload_len_bytes * 8)
    return _symbols_until(cfg, header_symbol_indices[-1] + 1, needed_coded_bits)


def build_frame(cfg, payload: bytes):
    payload_bits = bytes_to_bits(payload)
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    is_ldpc = getattr(cfg, "fec_scheme", "viterbi").lower() == "ldpc"
    raw_len = len(payload)
    if is_ldpc:
        raw_len |= 0x80000000

    header_fields_bits = np.concatenate([
        np.unpackbits(np.array([raw_len], dtype=">u4").view(np.uint8)),
        np.unpackbits(np.array([crc], dtype=">u4").view(np.uint8)),
    ])
    header_checksum = _header_checksum8(header_fields_bits)
    header_bits = np.concatenate([
        header_fields_bits,
        np.unpackbits(np.array([header_checksum], dtype=np.uint8)),
    ])

    # FEC-encode the payload, then interleave -- the header carries the
    # ORIGINAL byte length, so the receiver can deterministically
    # recompute both the coded bit count and the interleaver permutation
    # without needing separate fields for either.
    if is_ldpc:
        coded_bits = ldpc.ldpc_encode(payload_bits)
    else:
        coded_bits = fec_encode(payload_bits)
    coded_bits = interleave(coded_bits)

    symbol_bits_list = []
    symbol_modulations = []
    k = 0

    # skip past any symbols with zero data capacity (all-pilot, shouldn't
    # normally happen but keep this robust)
    while _data_bits_capacity(cfg, k) == 0:
        symbol_bits_list.append(np.zeros(0, dtype=np.uint8))
        symbol_modulations.append("qpsk")
        k += 1
    header_symbol_indices = _header_symbol_plan(cfg)
    header_capacities = [_data_bits_capacity(cfg, idx) for idx in header_symbol_indices]
    header_total_capacity = sum(header_capacities)
    # The header used to be protected by plain repetition alone -- no FEC
    # at all, unlike the payload's rate-1/2 convolutional code -- making
    # it the weakest link in the whole frame. Confirmed for real: at a
    # clean, exactly-known 10dB AWGN SNR (no fading, no hardware
    # involved), the large majority of lost fragments traced directly
    # back to the header's repetition-only decode landing on a wrong
    # payload_len/expected_crc, not the payload's own FEC being
    # exhausted. Give the header the same convolutional code, then still
    # repeat the CODED bits to fill at least HEADER_MIN_REPEATS worth of
    # capacity, spanning multiple symbols if one isn't enough (see
    # _header_symbol_plan) -- the receiver soft-combines the repeated
    # copies before Viterbi decoding (see _repetition_soft_combine) for
    # the benefit of both repetition AND coding gain instead of
    # repetition alone.
    header_coded_bits = fec_encode(header_bits)
    header_all_bits = _repetition_encode(header_coded_bits, header_total_capacity)
    offset = 0
    for cap in header_capacities:
        symbol_bits_list.append(header_all_bits[offset:offset + cap])
        symbol_modulations.append("qpsk")
        offset += cap
    k = header_symbol_indices[-1] + 1

    remaining = coded_bits
    scramble_pos = 0
    while len(remaining) > 0:
        cap = _payload_bits_capacity(cfg, k)
        if cap == 0:
            symbol_bits_list.append(np.zeros(0, dtype=np.uint8))
            symbol_modulations.append(cfg.data_modulation)
            k += 1
            continue
        chunk = remaining[:cap]
        if len(chunk) < cap:
            chunk = np.concatenate([chunk, np.zeros(cap - len(chunk), dtype=np.uint8)])
        chunk = chunk ^ _prbs_bits(cap, scramble_pos)  # whiten (see _descramble_soft)
        scramble_pos += cap
        symbol_bits_list.append(chunk)
        symbol_modulations.append(cfg.data_modulation)
        remaining = remaining[cap:]
        k += 1

    preamble = build_preamble(cfg)
    frame = ofdm_modulate(cfg, symbol_bits_list, symbol_modulations)
    return np.concatenate([preamble, frame])


def _pam_levels_for(modulation):
    if modulation == "16qam":
        return np.array([-3.0, -1.0, 1.0, 3.0]) / _QAM16_NORM
    return np.array([-1.0, 1.0]) / np.sqrt(2)


def _nearest_ideal_point(points, modulation):
    """Nearest ideal constellation point per axis -- exact for QPSK (a
    sign check) and for square 16-QAM (separable into two independent
    4-level PAM rails, so nearest-per-axis IS the nearest 2D point)."""
    levels = _pam_levels_for(modulation)
    real_idx = np.argmin(np.abs(points.real[:, None] - levels[None, :]), axis=1)
    imag_idx = np.argmin(np.abs(points.imag[:, None] - levels[None, :]), axis=1)
    return levels[real_idx] + 1j * levels[imag_idx]


def _evm_percent(points, modulation="qpsk"):
    """RMS EVM (%) of equalized (zero-forced) points against the nearest
    ideal constellation point -- the standard definition (RMS error
    magnitude / RMS ideal magnitude * 100). A real, scattered-noise channel
    gives a small, symmetric EVM; a systematic problem (residual CFO/timing
    drift, IQ imbalance) instead shows up as a rotated, smeared, or
    off-center point cloud rather than just a bigger circle -- see the
    constellation dump (hf_ofdm_rx.py's --dump-constellation) for that.

    Uses the median, not the mean, squared error: zero-forcing (rx/h)
    amplifies noise right along with the signal at a deep fade (small
    |h|), so a real frequency-selective channel can occasionally produce
    a handful of huge-magnitude outlier points that carry almost no
    actual information (this is exactly why the FEC path uses the
    separate matched-filter eq_soft instead, which shrinks rather than
    amplifies those). A mean-based EVM lets a tiny number of such points
    dominate the whole statistic; the median reflects the typical,
    representative point instead."""
    if len(points) == 0:
        return None
    # Was sqrt(median err^2) / (1/sqrt 2): the divisor is one AXIS of a
    # QPSK point, not the constellation's RMS magnitude (1 -- both QPSK
    # and 16-QAM are unit power), so EVM read sqrt 2 high (MER 3 dB low);
    # and the median of err^2 is ~0.69x its mean for noise (+1.6 dB). Net
    # ~1.5 dB pessimistic, off by a varying amount. Now standard RMS EVM
    # over the points, leaving out only deep-fade blowups (as counted by
    # deep_fade_fraction) -- the outliers the median was there to ignore.
    pts = np.ascontiguousarray(points, np.complex128)
    levels = _pam_levels_for(modulation)
    err2 = _nearest_err2(pts, levels)
    axis_max2 = float(np.max(levels)) ** 2
    keep = (pts.real ** 2 + pts.imag ** 2) <= DEEP_FADE_MAG_RATIO ** 2 * axis_max2
    if not np.any(keep):
        return None
    measured = float(np.mean(err2[keep]))
    return float(100 * np.sqrt(_true_err2(measured, modulation)))


_DD_TABLES = {}


def _true_err2(measured, modulation):
    """Measuring error against the NEAREST ideal point reads low once noise
    pushes points past a decision boundary -- at true SNR 2 dB QPSK
    measured 3.8 dB. Maps the measured mean error power back to the true
    noise power, via a curve simulated once (unit-power constellation +
    AWGN, fixed seed) per modulation. Monotonic, so np.interp inverts it."""
    table = _DD_TABLES.get(modulation)
    if table is None:
        rng = np.random.default_rng(12345)
        levels = _pam_levels_for(modulation)
        n = 20000
        true_db = np.arange(-6.0, 30.5, 0.5)
        meas = []
        sym = rng.choice(levels, n) + 1j * rng.choice(levels, n)
        unit = rng.normal(size=n) + 1j * rng.normal(size=n)
        for snr in true_db:
            pts = sym + unit * np.sqrt(10 ** (-snr / 10) / 2)
            meas.append(float(np.mean(_nearest_err2(pts, levels))))
        true_err2 = 10 ** (-true_db / 10)
        order = np.argsort(meas)
        table = (np.asarray(meas)[order], true_err2[order])
        _DD_TABLES[modulation] = table
    meas, true_err2 = table
    if measured >= meas[-1]:
        return float(true_err2[-1] * measured / meas[-1])  # beyond -6 dB: proportional
    return float(np.interp(measured, meas, true_err2))


@njit(cache=True)
def _nearest_err2(points, levels):
    """|point - nearest ideal point|^2 per point, nearest per axis like
    _nearest_ideal_point (first level wins a tie, as argmin) -- one pass
    instead of numpy's (points x levels) temporaries."""
    n = points.shape[0]
    out = np.empty(n)
    for i in range(n):
        re = points[i].real
        im = points[i].imag
        br = 1e300
        bi = 1e300
        for lv in levels:
            dr = (re - lv) * (re - lv)
            di = (im - lv) * (im - lv)
            if dr < br:
                br = dr
            if di < bi:
                bi = di
        out[i] = br + bi
    return out


DEEP_FADE_MAG_RATIO = 3.0


def deep_fade_fraction(points, magnitude_ratio=DEEP_FADE_MAG_RATIO, modulation="qpsk"):
    """Fraction of points whose magnitude exceeds magnitude_ratio times the
    ideal (outermost, for 16-QAM) constellation magnitude -- these are the
    zero-forcing deep-fade blowups _evm_percent's median deliberately
    ignores; reported separately since a high fraction here is itself a
    meaningful real-channel signal (frequent/severe frequency-selective
    fading), just not one that belongs mixed into a "typical point" noise-
    floor number."""
    if len(points) == 0:
        return None
    levels = _pam_levels_for(modulation)
    ideal_axis = np.abs(levels).max()
    return float(np.mean(np.abs(points) > magnitude_ratio * ideal_axis))


# A CRC-failed frame whose decoder-vs-received disagreement is at or above
# this is below the FEC's capability, not merely mis-estimated: the retry
# passes (shorter time smoothing, decision-directed channel estimate) buy
# 1-2 dB and can't rescue it, and each one costs a full FEC decode (LDPC:
# 9 blocks x 50 non-converging iterations). Measured on a live link: frames
# that decoded fine sat at BER ~0.11, hopeless ones at 0.21-0.30. Skipping
# the retries keeps the failure path cheap enough that a run of bad
# fragments can't push the receiver behind real time and cost good ones.
HOPELESS_BER = 0.18


def _ldpc_decode_reencoded(llr, n_bits):
    """LDPC-decode, and return the decoded bits together with their
    interleaved re-encoding (for the BER readout). When every block
    converged, the decoder's own codewords ARE that re-encoding, so
    ldpc_encode is skipped -- it was ~8% of a Raspberry Pi 4's decode time
    on good fragments. Otherwise re-encode as before, so a failing
    fragment's BER (which gates the retry passes) is unchanged."""
    bits, codewords = ldpc.ldpc_decode_soft_codeword(llr, n_bits)
    if codewords is None:
        codewords = ldpc.ldpc_encode(bits)
    return bits, interleave(codewords)


def decode_frame(cfg, rx_after_preamble, verbose=False, max_wait_samples=None):
    """rx_after_preamble: samples starting right after the preamble.
    Returns (payload_bytes_or_None, crc_ok, incomplete, ber, evm_percent,
    constellation_points). incomplete=True means the only problem was not
    having enough samples yet -- worth retrying once more data has arrived
    (e.g. a live/streaming receiver); incomplete=False with
    payload_bytes=None means a real failure (garbage header) that won't be
    fixed by waiting for more samples. ber is the fraction of coded bits
    the Viterbi decoder had to correct (comparing the hard-sliced received
    bits against the re-encoded, decoded message). evm_percent and
    constellation_points (the raw equalized, zero-forced data-carrier
    points as a complex array) are only populated once a full decode
    attempt actually ran (i.e. not on an early "not enough samples yet" or
    "garbage header" exit) -- None otherwise."""
    def dbg(msg):
        if verbose:
            print(f"  [decode] {msg}", file=sys.stderr)

    header_symbol_indices = _header_symbol_plan(cfg)
    n_symbols_for_header = header_symbol_indices[-1] + 1
    dbg(f"header occupies symbols {header_symbol_indices}, need {n_symbols_for_header} symbols "
        f"({n_symbols_for_header * cfg.symbol_len} samples), have {len(rx_after_preamble)}")
    if len(rx_after_preamble) < n_symbols_for_header * cfg.symbol_len:
        dbg("FAIL: not enough samples for the header symbol(s)")
        return None, False, True, None, None, None

    header_freq = ofdm_demodulate(cfg, rx_after_preamble, n_symbols_for_header)
    header_eq, header_eq_soft = estimate_and_equalize(cfg, header_freq, n_symbols_for_header)
    # Soft-combine the repeated copies of the FEC-coded header bits (see
    # build_frame), then Viterbi-decode with soft decisions -- the same
    # coding gain the payload already gets, instead of the old plain
    # majority-vote repetition decode that left the header far more
    # error-prone than the payload at low SNR.
    header_coded_len = fec_coded_len(HEADER_BITS)
    header_symbol_soft = np.concatenate([
        qpsk_soft(header_eq_soft[idx][~pilot_mask(cfg, idx)]) for idx in header_symbol_indices
    ])
    header_coded_soft = _repetition_soft_combine(header_symbol_soft, header_coded_len)
    header_bits = fec_decode_soft(header_coded_soft, HEADER_BITS)
    header_fields_bits = header_bits[:HEADER_LEN_BITS + HEADER_CRC_BITS]
    raw_len = int(np.packbits(header_bits[:HEADER_LEN_BITS]).view(">u4")[0])
    is_ldpc = bool(raw_len & 0x80000000) or (getattr(cfg, "fec_scheme", "viterbi").lower() == "ldpc")
    payload_len = int(raw_len & 0x7FFFFFFF)
    expected_crc = int(np.packbits(header_bits[HEADER_LEN_BITS:HEADER_LEN_BITS + HEADER_CRC_BITS]).view(">u4")[0])
    header_checksum = int(np.packbits(header_bits[HEADER_LEN_BITS + HEADER_CRC_BITS:])[0])
    header_checksum_ok = header_checksum == _header_checksum8(header_fields_bits)
    fec_name = "LDPC" if is_ldpc else "Viterbi"
    dbg(f"header decoded: payload_len={payload_len}, fec={fec_name}, expected_crc=0x{expected_crc:08x}, "
        f"checksum={'OK' if header_checksum_ok else 'MISMATCH'}")

    if not header_checksum_ok:
        dbg("FAIL: header checksum mismatch -- header is corrupted (caught independent of "
            "whether payload_len happens to look plausible)")
        return None, False, False, None, None, None

    MAX_REASONABLE_PAYLOAD_BYTES = 10_000_000
    if payload_len < 0 or payload_len > MAX_REASONABLE_PAYLOAD_BYTES:
        dbg(f"FAIL: payload_len {payload_len} out of sane range -- header is almost "
            f"certainly garbage (bad sync, wrong mode/occupancy, or no signal)")
        return None, False, False, None, None, None

    if is_ldpc:
        needed_coded_bits = ldpc.ldpc_coded_len(payload_len * 8)
    else:
        needed_coded_bits = fec_coded_len(payload_len * 8)
    n_symbols = _symbols_until(cfg, header_symbol_indices[-1] + 1, needed_coded_bits)

    needed_samples = n_symbols * cfg.symbol_len
    dbg(f"need {needed_coded_bits} coded bits across {n_symbols} symbols total "
        f"({needed_samples} samples), have {len(rx_after_preamble)}")
    if max_wait_samples is not None and needed_samples > max_wait_samples:
        # A false lock's garbage header can still parse to a "sane" (small
        # enough) payload_len that implies an absurdly long frame -- left
        # unchecked, that reads as "just needs more data" forever, since
        # that much data may never arrive (observed for real: a false
        # header implying a frame ~5x longer than the entire capture,
        # stalling reception indefinitely instead of moving on). Callers
        # that know frames should be short (e.g. a fragmented transfer)
        # can pass a cap so this is treated as a real failure instead.
        dbg(f"FAIL: implied frame ({needed_samples} samples) exceeds max_wait_samples "
            f"({max_wait_samples}) -- treating as a false lock, not real data to wait for")
        return None, False, False, None, None, None
    if len(rx_after_preamble) < needed_samples:
        dbg("FAIL: not enough samples captured for the full frame yet")
        return None, False, True, None, None, None

    all_freq = ofdm_demodulate(cfg, rx_after_preamble, n_symbols)
    all_eq, all_eq_soft = estimate_and_equalize(cfg, all_freq, n_symbols)

    # 16-QAM's magnitude bit (see qam16_soft) needs its input sitting at
    # the TRUE constellation scale ({+-1,+-3}/_QAM16_NORM) to compare
    # against its fixed threshold -- all_eq_soft (rx*conj(h) = eq*|h|^2)
    # is scaled by the channel gain instead, which QPSK's sign-only soft
    # decision never notices (scale-invariant) but silently corrupts
    # 16-QAM's magnitude bit into near-random noise. Confirmed for real:
    # a clean link (EVM 4%, zero deep fades) still saw ~21% BER on every
    # single fragment -- almost exactly the ~25% you'd get from 2 of
    # every 4 bits per symbol being decoded blind. all_eq (zero-forced)
    # is correctly scaled, at the cost of losing eq_soft's deep-fade
    # noise suppression for 16-QAM's payload specifically -- an
    # acceptable trade given 16-QAM is already an explicit higher-SNR
    # opt-in, not the robust default.
    payload_soft_source = all_eq if cfg.data_modulation == "16qam" else all_eq_soft

    # Every payload symbol's data carriers at once, in symbol-then-carrier
    # order (what a per-symbol loop of concatenations gave): the pilot mask
    # repeats every `period` symbols, so one boolean array selects them all.
    # data_soft works point by point, so one call over the lot is the same.
    # (Per symbol, this was ~10k Python iterations per second of mode VU.)
    first = header_symbol_indices[-1] + 1
    period = cfg.time_int if cfg.uses_full_training else cfg.freq_int
    phase_masks = np.stack([pilot_mask(cfg, p) for p in range(period)])
    rows = np.arange(first, n_symbols)
    data_sel = ~phase_masks[rows % period]
    constellation = all_eq[first:n_symbols][data_sel]
    coded_soft = data_soft(cfg, payload_soft_source[first:n_symbols][data_sel])
    evm = _evm_percent(constellation, cfg.data_modulation)
    if verbose and len(rows):
        constellation_chunks = [all_eq[k][data_sel[i]] for i, k in enumerate(rows) if data_sel[i].any()]
        # Where is a bad fragment bad? Uniform across symbols = whole-frame
        # SNR/power dip; a few symbols = time glitch (dropout, buffer
        # overrun); concentrated in some frequency bands = spur/null/filter.
        per_sym = [_evm_percent(c, cfg.data_modulation) for c in constellation_chunks]
        n_bands = 8
        band_evm = []
        for b in range(n_bands):
            band = np.concatenate([c[b * len(c) // n_bands:(b + 1) * len(c) // n_bands]
                                   for c in constellation_chunks])
            band_evm.append(_evm_percent(band, cfg.data_modulation))
        dbg("EVM per payload symbol (%): " + " ".join(f"{e:.0f}" for e in per_sym))
        dbg("EVM per frequency band, low->high carrier (%): " + " ".join(f"{e:.0f}" for e in band_evm))

    coded_soft = _descramble_soft(coded_soft)[:needed_coded_bits]
    if len(coded_soft) < needed_coded_bits:
        dbg(f"FAIL: only assembled {len(coded_soft)}/{needed_coded_bits} coded bits")
        return None, False, True, None, evm, constellation

    coded_soft_deint = deinterleave(coded_soft)
    if is_ldpc:
        # Normalize soft LLRs to unit RMS before LDPC decoding.
        # The NMS decoder (alpha=0.75) assumes a roughly consistent LLR scale:
        # too large -> decoder over-trusts every bit, stops correcting errors;
        # too small -> messages wash out across iterations, slow/no convergence.
        # The raw soft values (eq_soft = rx*conj(h) = eq*|h|^2) are scaled by
        # channel gain and vary widely frame-to-frame and subcarrier-to-subcarrier.
        # Viterbi is scale-invariant (only ever compares path metrics) so it
        # never needed this -- LDPC does. Normalizing to unit RMS makes the
        # decoder independent of channel gain, similar to how proper AWGN LLRs
        # are scaled by 2/sigma^2, but without needing an explicit noise estimate.
        llr_rms = np.sqrt(np.mean(coded_soft_deint ** 2))
        if llr_rms > 0:
            coded_soft_deint = coded_soft_deint / llr_rms
        payload_bits, re_encoded = _ldpc_decode_reencoded(coded_soft_deint, payload_len * 8)
    else:
        payload_bits = fec_decode_soft(coded_soft_deint, payload_len * 8)
        re_encoded = interleave(fec_encode(payload_bits))
    payload_bytes = bits_to_bytes(payload_bits)

    # BER estimate: re-encode the decoded message and compare against the
    # hard-sliced received bits -- the fraction that disagree is how much
    # the FEC decoder had to correct, a useful link-quality signal even
    # when the CRC passes (0 residual errors) or fails (too many to fix).
    received_hard = (coded_soft < 0).astype(np.uint8)
    ber = float(np.mean(re_encoded[:len(received_hard)] != received_hard))

    actual_crc = zlib.crc32(payload_bytes) & 0xFFFFFFFF
    crc_ok = actual_crc == expected_crc
    dbg(f"FEC+CRC done: actual_crc=0x{actual_crc:08x}, {'MATCH' if crc_ok else 'MISMATCH'}, "
        f"ber={ber:.4f}, evm={evm:.1f}%, "
        f"deep_fade_frac={deep_fade_fraction(constellation, modulation=cfg.data_modulation):.3f}")

    # Shorter-time-smoothing retry (comb-pilot modes only, first-pass CRC
    # failure only): see TIME_SMOOTHING_RETRY_HALF_WINDOWS. Same decode as
    # the first pass, just a different channel-estimate averaging window.
    if not crc_ok and ber >= HOPELESS_BER:
        dbg(f"CRC failed with ber={ber:.3f} >= {HOPELESS_BER} -- below FEC capability, skipping retries")
        return payload_bytes, crc_ok, False, ber, evm, constellation

    if not crc_ok and not cfg.uses_full_training:
        for hw in TIME_SMOOTHING_RETRY_HALF_WINDOWS:
            if hw == TIME_SMOOTHING_HALF_WINDOW:
                continue
            eq_w, eq_soft_w = estimate_and_equalize(cfg, all_freq, n_symbols, time_half_window=hw)
            src_w = eq_w if cfg.data_modulation == "16qam" else eq_soft_w
            chunks_w, const_w = [], []
            for sym_idx in range(header_symbol_indices[-1] + 1, n_symbols):
                mask = pilot_mask(cfg, sym_idx)
                if np.all(mask):
                    continue
                chunks_w.append(data_soft(cfg, src_w[sym_idx][~mask]))
                const_w.append(eq_w[sym_idx][~mask])
            soft_w = _descramble_soft(np.concatenate(chunks_w) if chunks_w else np.zeros(0, dtype=float))[:needed_coded_bits]
            if len(soft_w) < needed_coded_bits:
                continue
            llr_w = deinterleave(soft_w)
            if is_ldpc:
                rms_w = np.sqrt(np.mean(llr_w ** 2))
                if rms_w > 0:
                    llr_w = llr_w / rms_w
                bits_w, re_w = _ldpc_decode_reencoded(llr_w, payload_len * 8)
            else:
                bits_w = fec_decode_soft(llr_w, payload_len * 8)
                re_w = interleave(fec_encode(bits_w))
            bytes_w = bits_to_bytes(bits_w)
            if (zlib.crc32(bytes_w) & 0xFFFFFFFF) == expected_crc:
                constellation_w = np.concatenate(const_w) if const_w else np.zeros(0, dtype=complex)
                ber_w = float(np.mean(re_w[:len(soft_w)] != (soft_w < 0).astype(np.uint8)))
                dbg(f"time-smoothing retry (half-window {hw}) SUCCEEDED: ber={ber_w:.4f}")
                return bytes_w, True, False, ber_w, _evm_percent(constellation_w, cfg.data_modulation), constellation_w

    # Decision-directed retry: only on a first-pass CRC failure, and only
    # for comb-pilot modes (uses_full_training modes use a different,
    # time-interpolated channel estimate not wired up to dd_ref -- see
    # estimate_and_equalize). re_encoded is already the decoder's own
    # best guess at every payload bit, re-mapped back to the symbol each
    # payload carrier is believed to carry -- using that as an extra
    # per-carrier reference (alongside the real pilots) gives a dense,
    # interpolation-free channel estimate instead of the original sparse-
    # pilot one, which typically buys a couple of dB right where pilot
    # density is the limiting factor. Only ever kept if it goes on to
    # pass CRC -- a wrong guess corrupting its own carrier's estimate is
    # the standard risk of this technique, bounded here to "no worse than
    # the original failure" rather than silently swapped in on faith.
    if not crc_ok and not cfg.uses_full_training:
        dd_ref = {}
        bit_pos = 0
        bps = data_bits_per_symbol(cfg)
        # re_encoded is in the unwhitened domain; the symbols actually carry
        # the whitened bits, so re-apply the PRBS before mapping to references.
        re_encoded = re_encoded ^ _prbs_bits(len(re_encoded))
        for sym_idx in range(header_symbol_indices[-1] + 1, n_symbols):
            mask = pilot_mask(cfg, sym_idx)
            if np.all(mask):
                continue
            n_carriers = int((~mask).sum())
            n_bits = n_carriers * bps
            bits_slice = re_encoded[bit_pos: bit_pos + n_bits]
            bit_pos += n_bits
            if len(bits_slice) < n_bits:
                break  # ran out of coded bits (needed_coded_bits was capped/truncated)
            ref = np.full(cfg.n_data, np.nan, dtype=complex)
            ref[~mask] = data_map(cfg, bits_slice)
            dd_ref[sym_idx] = ref

        pilots = pilot_sequence(cfg)
        all_eq2, all_eq_soft2 = estimate_and_equalize(cfg, all_freq, n_symbols, dd_ref=dd_ref)
        payload_soft_source2 = all_eq2 if cfg.data_modulation == "16qam" else all_eq_soft2

        coded_bit_chunks2 = []
        constellation_chunks2 = []
        for sym_idx in range(header_symbol_indices[-1] + 1, n_symbols):
            mask = pilot_mask(cfg, sym_idx)
            if np.all(mask):
                continue
            coded_bit_chunks2.append(data_soft(cfg, payload_soft_source2[sym_idx][~mask]))
            constellation_chunks2.append(all_eq2[sym_idx][~mask])

        coded_soft2 = np.concatenate(coded_bit_chunks2) if coded_bit_chunks2 else np.zeros(0, dtype=float)
        coded_soft2 = _descramble_soft(coded_soft2)[:needed_coded_bits]
        if len(coded_soft2) == needed_coded_bits:
            if is_ldpc:
                llr2 = deinterleave(coded_soft2)
                llr2_rms = np.sqrt(np.mean(llr2 ** 2))
                if llr2_rms > 0:
                    llr2 = llr2 / llr2_rms
                payload_bits2, re_encoded2 = _ldpc_decode_reencoded(llr2, payload_len * 8)
            else:
                payload_bits2 = fec_decode_soft(deinterleave(coded_soft2), payload_len * 8)
                re_encoded2 = interleave(fec_encode(payload_bits2))
            payload_bytes2 = bits_to_bytes(payload_bits2)
            actual_crc2 = zlib.crc32(payload_bytes2) & 0xFFFFFFFF
            if actual_crc2 == expected_crc:
                constellation2 = np.concatenate(constellation_chunks2) if constellation_chunks2 else np.zeros(0, dtype=complex)
                evm2 = _evm_percent(constellation2, cfg.data_modulation)
                received_hard2 = (coded_soft2 < 0).astype(np.uint8)
                ber2 = float(np.mean(re_encoded2[:len(received_hard2)] != received_hard2))
                dbg(f"decision-directed retry SUCCEEDED (first pass failed CRC): "
                    f"ber={ber2:.4f}, evm={evm2:.1f}%")
                return payload_bytes2, True, False, ber2, evm2, constellation2
        dbg("decision-directed retry did not recover a valid CRC -- keeping original failure")

    return payload_bytes, crc_ok, False, ber, evm, constellation
