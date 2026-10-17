#!/usr/bin/env python3
"""
Wavelet video codec with an EXACT per-frame byte budget -- every
encoded frame is precisely `frame_bytes` long, so the video stream's
bitrate is exactly frame_bytes*8*fps with zero variance, no VBV
buffer, no filler NALs.

Low latency by construction: each frame is encoded the moment it's
captured, with no lookahead, no B-frames, no rate-control buffer --
added delay is one frame period plus the (few ms) encode time.

Temporal prediction, built for a link with no return channel: each
frame codes the residual against a LEAKY prediction from the previous
reconstructed frame, pred = 128 + a*(prev - 128), applied in the wavelet
domain with its own weight a per subband class (9 classes: luma LL and
each detail level, chroma likewise). The encoder picks each a per frame
by least squares, clipped to [0, leak] and sent as 3 bits (k/7 of leak)
-- a scene cut or motion drops the affected classes towards 0, a static
scene takes the full leak. Measured with test_wavelet_codec.py: +0.5 to
+1.2 dB over one fixed weight at the same bitrate, and faster recovery
after a lost packet (most classes sit below the cap). The encoder
tracks the decoder's reconstruction exactly (it builds the same integer
reference from what it coded -- see "integer DWT + reference" below --
so the two ends stay bit-identical even on different CPUs, e.g. an x86
encoder and a Raspberry Pi decoder), so with no loss there's no drift.
When a packet IS lost the
decoder's reference is wrong, and two mechanisms clean that up with no
feedback: the leak shrinks any reference error by `leak` every frame,
and a rolling refresh codes every `refresh_frames`-th wavelet coefficient
from scratch (predicted from flat grey) each frame, so the whole picture
is re-sent once per `refresh_frames` frames, bounding the worst-case
recovery time. A receiver that joins late recovers the same way. The
refresh is what allows a high leak (0.996): the leak alone would take
~15 s to clear an error. leak=0 gives the pure intra-only codec.

Pipeline, per frame (YUV 4:2:0 input, applied to the residual):
  1. CDF 9/7 lifting wavelet (JPEG2000's lossy filter) in fixed-point
     integer arithmetic, 4 levels on luma, 3 on chroma, so every
     component's LL band ends up the same size (16x9 at 256x144).
     Everything per-frame is numba-compiled and releases the GIL (so a
     decode thread runs alongside a GUI); at 352x192 on a Raspberry Pi 4,
     ~16 ms to encode and ~10 ms to decode a frame at 876 B/frame.
  2. Coefficients scaled by each subband's synthesis-basis L2 norm
     (measured numerically at init, see _subband_weights), so one unit
     of quantization error costs the same pixel MSE in every subband --
     makes plain "highest bit-plane first" ordering MSE-optimal.
     Chroma additionally down-weighted (CHROMA_WEIGHT).
  3. Embedded bit-plane coding, highest plane first across ALL
     subbands, with a context-adaptive binary range coder (LZMA-style,
     16-bit probabilities, starting each frame from a trained table --
     INIT_PROBS). Significance is flagged hierarchically -- per subband
     while it is still all-zero, per 16x16 group, per 4x4 block (WVT3;
     WVT2 had block flags only) -- so the huge mass of all-zero
     fine-subband coefficients costs almost nothing, in bits or in
     time. Within each plane,
     coefficients next to already-significant ones go first, then
     refinement bits, then the rest (PASS_ORDER, JPEG2000-style).
  4. Encoding simply STOPS when the next symbol might not fit the
     byte budget. Because the stream is embedded, whatever was coded
     up to that point is the best image that many bytes can buy -- this
     is where the exact CBR comes from: no rate-control loop, no
     guessing at a quantizer. The decoder finds the same stopping
     point itself and mirrors the encoder exactly.

Packet layout (exactly frame_bytes long):
    [top_plane (5 bits) | 9 per-class weights (3 bits each)] (4 bytes, big-endian) |
    leak*256 (1) | refresh phase (1) | refresh period in frames, 0 = off (1) |
    range-coder bytes | zero pad
No symbol count is sent: the decoder re-encodes what it decodes (a
shadow encoder, see _sym) and so stops exactly where the encoder did.
The range coder's always-zero first byte is not sent, and its final
flush is rounded so only one byte beyond the pending ones is needed.
"""
import numpy as np
import os
from numba import njit, prange
import numba as _numba

# The transforms can run on several threads (rows / column chunks / planes
# are independent: bit-identical to one thread). Only worth it for large
# pictures: on a Pi 4 at 256x144, 3 threads saved ~1 ms a frame but cost
# ~50% more CPU (encode 14 -> 20 ms, decode 9.5 -> 15 ms CPU) -- CPU the
# modem needs, most of all in duplex. So pictures up to SMALL_PICTURE_PIXELS
# use one thread and larger ones AVM_CODEC_THREADS (default 3 of a Pi 4's
# 4 cores, leaving one for the modem; setting it also overrides the small-
# picture rule). numba's thread count is per calling thread, so each
# encode/decode applies it on whichever thread it runs on (the receiver
# decodes on its video thread, which otherwise got numba's default: all cores).
SMALL_PICTURE_PIXELS = 384 * 240
_ENV_THREADS = os.environ.get("AVM_CODEC_THREADS")


def _threads_for(width, height):
    n = int(_ENV_THREADS) if _ENV_THREADS else (1 if width * height <= SMALL_PICTURE_PIXELS else 3)
    return max(1, min(_numba.config.NUMBA_NUM_THREADS, n))


def _use_threads(n):
    try:
        if _numba.get_num_threads() != n:
            _numba.set_num_threads(n)
    except Exception:
        pass
COL_CHUNK = 64   # columns per thread task in the vertical pass

CHROMA_WEIGHT = 0.6
# Where in its remaining uncertainty interval a partly-decoded coefficient
# is reconstructed (0.5 = midpoint; lower suits the Laplacian distribution).
REC_BIAS = 0.4
# Pass order within each bit-plane (see _code): 0 = significance then
# refinement, 1 = propagation / refinement / cleanup (JPEG2000-style),
# 2 = refinement then significance.
PASS_ORDER = 1
Q_SCALE = 2.0  # integer coefficient = round(weighted_coef * Q_SCALE)

# CDF 9/7 lifting constants
_A = -1.586134342059924
_B = -0.052980118572961
_G = 0.882911075530934
_D = 0.443506852043971
_K = 1.230174104914001

# ---- context layout ----
N_CLS = 9  # Y: LL, L4..L1 details (0..4); chroma: LL, L3..L1 details (5..8)
CTX_BLK = 0
CTX_SIG = CTX_BLK + N_CLS * 4
CTX_SIGN = CTX_SIG + N_CLS * 3 * 2 * 2
CTX_REF = CTX_SIGN + 1
CTX_SB = CTX_REF + N_CLS * 2   # "subband still all-zero" flag, per class (see _pass_clean)
CTX_GRP = CTX_SB + N_CLS       # block-group flag, per class x parent-group-flagged
N_CTX = CTX_GRP + N_CLS * 2
GROUP = 4  # a group is GROUP x GROUP blocks (16x16 coefficients)

# ---- packet header ----
# Per-class prediction weight, as k/WEIGHT_LEVELS of the leak cap.
WEIGHT_BITS = 3
WEIGHT_LEVELS = (1 << WEIGHT_BITS) - 1
TOP_BITS = 5  # highest coded bit-plane, 0..31
assert TOP_BITS + N_CLS * WEIGHT_BITS == 32
HEADER_BYTES = 4 + 3  # [top | weights] (32 bits), leak, refresh phase, [motion flag | refresh period]
MOTION_BYTES = 2      # motion dx, dy (half pixels) -- only when the flag (top bit of byte 6) is set
MOTION_FLAG = 0x80    # so the refresh period is at most 127 frames
# Below this many coded bits per luma pixel the reference is rounded to whole
# pixels each frame (an inverse + forward DWT, as before WVT5): at ~0.05 bit/
# pixel (125 B at 192x112) that rounding acts as mild denoising (measured
# +0.2-0.3 dB); above, it only costs CPU. Decided from the packet size, which
# both ends know.
SNAP_BITS_PER_PIXEL = 0.08
# Block (local) motion: one vector per BLK x BLK luma block, a correction of
# +-BLK_RANGE half pixels on top of the global motion. Each pixel's motion is
# interpolated between the four nearest block centres (a smooth field: no
# block edges for the wavelets to spend bits on). Flagged by the top bit of
# the refresh-phase byte; then [n][bitmap of blocks with a vector]
# [one byte per such block: dx, dy as signed 4-bit half pixels]; n is 2 bytes.
BLK = 16
BLK_RANGE = 7
BLOCK_FLAG = 0x80
BLK_MAX_SHARE = 0.25     # the block field may take at most this share of a frame
BLK_MIN_GAIN = 0.15      # a block takes a vector only if it cuts its error by this fraction...
BLK_MIN_ABS = 1.5        # ...and by at least this many grey levels per pixel (tuned: 0.10/1.0 fitted noise on stills, -0.07 dB)
MC_RANGE = 8           # global motion search, +- pixels (luma); sent in half-pixel units
MC_GAIN = 0.92         # use a shift only if it cuts the luma difference to this fraction of no shift
REFRESH_TILE = 16   # rolling-refresh tile, luma pixels (>= the coarsest scale, 16)
REFRESH_SEED = 0x5754  # fixed tile order: both ends must agree (WVT4)

# ---- entropy-coder model (part of the bitstream format: both ends must match) ----
# Initial P(bit == 0) per context, 16-bit. WVT8: retrained (two passes) on
# the webcam recording + synthetic clips at the low-rate settings actually
# used -- 128x80 to 256x144, 1-5 fps, 125-600 B/frame, motion compensation
# on -- which also trained the subband/group contexts (hand-set until now).
# Held-out webcam clips (t=300/900 s): +0.14 to +0.26 dB at 5 kbps and
# +0.14 dB at 256x144/12 fps/16 kbps over the previous table.
INIT_PROBS = np.array([
    51629, 51293, 32768, 32768, 52228, 49312, 32768, 32768, 62586, 56577, 52629, 45733,
    62577, 51981, 52177, 46194, 62013, 49366, 48967, 40074, 52726, 47345, 32768, 32768,
    51950, 48506, 32768, 32768, 61569, 56537, 51760, 45896, 61744, 54310, 52766, 41401,
    62101, 32768, 61096, 32768, 60545, 32768, 51260, 32768, 51027, 32768, 36641, 32768,
    60824, 32768, 60106, 32768, 58759, 32768, 51083, 32768, 48771, 32768, 40119, 32768,
    62219, 52084, 60523, 54160, 56289, 47546, 56732, 48036, 45272, 41240, 44197, 37998,
    60903, 55452, 58543, 55862, 52890, 46812, 52520, 48976, 45876, 40792, 41356, 36370,
    61007, 56189, 59765, 57251, 49880, 44819, 54740, 50118, 42879, 36817, 34756, 34679,
    60060, 32768, 58804, 32768, 55159, 32768, 48398, 32768, 41440, 32768, 35420, 32768,
    59481, 32768, 60012, 32768, 54659, 32768, 50950, 32768, 41460, 32768, 37967, 32768,
    61332, 51072, 62303, 58982, 53964, 44166, 58701, 55555, 41354, 41545, 40317, 34920,
    61535, 51687, 63779, 62973, 49987, 36255, 61308, 56765, 35747, 34280, 34257, 25486,
    32595, 40771, 35995, 44475, 38462, 45885, 40057, 47362, 40979, 49956, 43352, 45523,
    41897, 50358, 48173, 57415, 48545, 58027, 57344, 2800, 46893, 49740, 52579, 56806,
    57048, 61517, 63398, 64512, 1024, 50000, 1024, 50000, 32982, 36142, 60302, 43050,
    62794, 50298, 1024, 50000, 1024, 50000, 31467, 33056, 57403, 41552,
], np.int64)
assert INIT_PROBS.shape == (N_CTX,)
# Adaptation: shift = min(ADAPT[0] + uses // ADAPT[2], ADAPT[1]) -- 1/16 for a
# context's first 16 uses in a frame, 1/32 for the next 16, then 1/64.
ADAPT = np.array([4, 6, 16], np.int64)


# ---------------------------------------------------------------- DWT
def _fwd_axis0(x):
    s = x[0::2].copy()
    d = x[1::2].copy()
    d += _A * (s + np.concatenate([s[1:], s[-1:]]))
    s += _B * (np.concatenate([d[:1], d[:-1]]) + d)
    d += _G * (s + np.concatenate([s[1:], s[-1:]]))
    s += _D * (np.concatenate([d[:1], d[:-1]]) + d)
    return np.concatenate([s * (1.0 / _K), d * _K])


def _inv_axis0(y):
    n = y.shape[0] // 2
    s = y[:n] * _K
    d = y[n:] * (1.0 / _K)
    s -= _D * (np.concatenate([d[:1], d[:-1]]) + d)
    d -= _G * (s + np.concatenate([s[1:], s[-1:]]))
    s -= _B * (np.concatenate([d[:1], d[:-1]]) + d)
    d -= _A * (s + np.concatenate([s[1:], s[-1:]]))
    x = np.empty((2 * n,) + y.shape[1:], dtype=y.dtype)
    x[0::2] = s
    x[1::2] = d
    return x


def dwt2(img, levels):
    c = img.astype(np.float64).copy()
    h, w = c.shape
    for _ in range(levels):
        c[:h, :w] = _fwd_axis0(c[:h, :w])
        c[:h, :w] = _fwd_axis0(c[:h, :w].T).T
        h //= 2
        w //= 2
    return c


def idwt2(c, levels):
    c = c.copy()
    H, W = c.shape
    for l in range(levels - 1, -1, -1):
        h, w = H >> l, W >> l
        c[:h, :w] = _inv_axis0(c[:h, :w].T).T
        c[:h, :w] = _inv_axis0(c[:h, :w])
    return c


# ---------------------------------------------------------------- integer DWT + reference
# The prediction reference is held as integer wavelet coefficients of
# pixel values with PIX_FRAC fractional bits, and everything that touches
# it -- prediction, residual reconstruction, and the per-frame round trip
# through whole clipped pixels -- is integer arithmetic (fixed-point
# lifting, arithmetic shifts): bit-exact on every CPU, so an x86 encoder
# and an ARM (Pi) decoder can never drift apart.
#
# The round trip matters for quality, not just display: snapping the
# reference to whole 0..255 pixels every frame (as the source is) lets it
# match static areas exactly, so nothing is left to code there -- worth
# ~+0.3 dB on static webcam scenes at 8-16 kbps vs never rounding.
PIX_FRAC = 8
_LS = 14  # lifting-constant fixed point
_LH = 1 << (_LS - 1)
_A_FX = int(round(_A * (1 << _LS)))
_B_FX = int(round(_B * (1 << _LS)))
_G_FX = int(round(_G * (1 << _LS)))
_D_FX = int(round(_D * (1 << _LS)))
_K_FX = int(round(_K * (1 << _LS)))
_KI_FX = int(round((1.0 / _K) * (1 << _LS)))
REC_FRAC = 8  # fixed point of a reconstructed residual value in the weighted domain
_REC_BIAS_FX = int(round(REC_BIAS * (1 << REC_FRAC)))
_INVW_FRAC = 16  # fixed point of 1/weight
_A_DEN = 256 * WEIGHT_LEVELS  # prediction weight = leak_q * k / (256 * WEIGHT_LEVELS)


# The 1-D (horizontal) lifting steps keep their edge cases (min/max of the
# neighbour index) out of the inner loops: the same integer operations on
# the same values, so bit-identical, but loops the compiler can vectorise --
# with the clamps inside, this pass was the slowest part of the codec on a
# Pi 4. s = t[:hh] (even samples), d = t[hh:] (odd).
@njit(cache=True, nogil=True, inline="always")
def _lift_d(t, hh, k, sign):
    """d[i] +=/-= k*(s[i] + s[i+1]) (s[hh] := s[hh-1])."""
    for i in range(hh - 1):
        t[hh + i] += sign * ((k * (t[i] + t[i + 1]) + _LH) >> _LS)
    t[2 * hh - 1] += sign * ((k * (t[hh - 1] + t[hh - 1]) + _LH) >> _LS)


@njit(cache=True, nogil=True, inline="always")
def _lift_s(t, hh, k, sign):
    """s[i] +=/-= k*(d[i-1] + d[i]) (d[-1] := d[0])."""
    t[0] += sign * ((k * (t[hh] + t[hh]) + _LH) >> _LS)
    for i in range(1, hh):
        t[i] += sign * ((k * (t[hh + i - 1] + t[hh + i]) + _LH) >> _LS)


@njit(cache=True, nogil=True)
def _fwd_1d_int(x, t, n):
    hh = n // 2
    for i in range(hh):
        t[i] = x[2 * i]
        t[hh + i] = x[2 * i + 1]
    _lift_d(t, hh, _A_FX, 1)
    _lift_s(t, hh, _B_FX, 1)
    _lift_d(t, hh, _G_FX, 1)
    _lift_s(t, hh, _D_FX, 1)
    for i in range(hh):
        x[i] = (t[i] * _KI_FX + _LH) >> _LS
        x[hh + i] = (t[hh + i] * _K_FX + _LH) >> _LS


@njit(cache=True, nogil=True)
def _inv_1d_int(x, t, n):
    hh = n // 2
    for i in range(hh):
        t[i] = (x[i] * _K_FX + _LH) >> _LS
        t[hh + i] = (x[hh + i] * _KI_FX + _LH) >> _LS
    _lift_s(t, hh, _D_FX, -1)
    _lift_d(t, hh, _G_FX, -1)
    _lift_s(t, hh, _B_FX, -1)
    _lift_d(t, hh, _A_FX, -1)
    for i in range(hh):
        x[2 * i] = t[i]
        x[2 * i + 1] = t[hh + i]


# The vertical (axis-0) pass works on whole rows at a time -- contiguous
# inner loops -- instead of copying each column out to a line buffer and
# back (strided, cache-unfriendly): same integer operations in the same
# order per element, so bit-identical, ~2x faster on the dev PC.
# The vertical pass, fused: the four lifting steps run in ONE sweep down the
# rows, each step a row or two behind the one it depends on (d1, s1 at row
# i; d2 at i-1; s2 at i-2 -- the inverse likewise in reverse), instead of a
# full pass over the plane per step. The same integer operations on the same
# values, so bit-identical -- three passes over memory instead of six, on a
# transform that is memory-bound on a Pi 4.
@njit(cache=True, nogil=True)
def _fwd_axis0_int(c, h, w, T, j0, j1):
    hh = h // 2
    for i in range(hh):
        for j in range(j0, j1):
            T[i, j] = c[2 * i, j]
            T[hh + i, j] = c[2 * i + 1, j]
    for k in range(hh + 2):
        i = k
        if i < hh:
            ip = min(i + 1, hh - 1)
            for j in range(j0, j1):
                T[hh + i, j] += (_A_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
            im = max(i - 1, 0)
            for j in range(j0, j1):
                T[i, j] += (_B_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
        i = k - 1
        if 0 <= i < hh:
            ip = min(i + 1, hh - 1)
            for j in range(j0, j1):
                T[hh + i, j] += (_G_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
        i = k - 2
        if 0 <= i < hh:
            im = max(i - 1, 0)
            for j in range(j0, j1):
                T[i, j] += (_D_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
                c[i, j] = (T[i, j] * _KI_FX + _LH) >> _LS
                c[hh + i, j] = (T[hh + i, j] * _K_FX + _LH) >> _LS


@njit(cache=True, nogil=True)
def _inv_axis0_int(c, h, w, T, j0, j1):
    hh = h // 2
    for i in range(hh):
        for j in range(j0, j1):
            T[i, j] = (c[i, j] * _K_FX + _LH) >> _LS
            T[hh + i, j] = (c[hh + i, j] * _KI_FX + _LH) >> _LS
    for k in range(hh + 3):
        i = k
        if i < hh:
            im = max(i - 1, 0)
            for j in range(j0, j1):
                T[i, j] -= (_D_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
        i = k - 1
        if 0 <= i < hh:
            ip = min(i + 1, hh - 1)
            for j in range(j0, j1):
                T[hh + i, j] -= (_G_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
        i = k - 2
        if 0 <= i < hh:
            im = max(i - 1, 0)
            for j in range(j0, j1):
                T[i, j] -= (_B_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
        i = k - 3
        if 0 <= i < hh:
            ip = min(i + 1, hh - 1)
            for j in range(j0, j1):
                T[hh + i, j] -= (_A_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
                c[2 * i, j] = T[i, j]
                c[2 * i + 1, j] = T[hh + i, j]


# The planes are int32 (coefficients stay within ~+-2^21: pixels << PIX_FRAC
# with a few bits of growth over 4 levels) while each lifting product is
# formed in 64 bits -- the same integers, so bit-identical to all-int64, but
# int32 x const -> int64 maps to ARM NEON's widening multiply. NEON has no
# 64-bit vector multiply: with int64 planes these transforms ran ~8x slower
# on a Pi 4 than on the dev PC (the rest of the codec ~3x), the largest cost
# in both encoder and decoder there.
@njit(cache=True, nogil=True, parallel=True)
def _dwt2_fwd_int(c, levels):
    H, W = c.shape
    T = np.empty_like(c)
    h, w = H, W
    for _ in range(levels):
        nch = (w + COL_CHUNK - 1) // COL_CHUNK
        for ch in prange(nch):
            _fwd_axis0_int(c, h, w, T, ch * COL_CHUNK, min(w, (ch + 1) * COL_CHUNK))
        for i in prange(h):
            t = np.empty(w, c.dtype)
            _fwd_1d_int(c[i], t, w)
        h //= 2
        w //= 2


@njit(cache=True, nogil=True, parallel=True)
def _dwt2_inv_int(c, levels):
    H, W = c.shape
    T = np.empty_like(c)
    for l in range(levels - 1, -1, -1):
        h, w = H >> l, W >> l
        for i in prange(h):
            t = np.empty(w, c.dtype)
            _inv_1d_int(c[i], t, w)
        nch = (w + COL_CHUNK - 1) // COL_CHUNK
        for ch in prange(nch):
            _inv_axis0_int(c, h, w, T, ch * COL_CHUNK, min(w, (ch + 1) * COL_CHUNK))


@njit(cache=True, nogil=True)
def _to_pixels(c, out):
    """Integer coefficients (after the inverse DWT) -> whole clipped pixels,
    and back to the forward-transform input scale in place."""
    half = 1 << (PIX_FRAC - 1)
    H, W = c.shape
    for i in range(H):
        for j in range(W):
            p = ((c[i, j] + half) >> PIX_FRAC) + 128
            p = min(max(p, 0), 255)
            out[i, j] = p
            c[i, j] = (p - 128) << PIX_FRAC


# geo: one row per component, [H, W, levels, offset into the concatenated
# plane buffer]. gidx maps flat coefficient i -> plane-buffer position.
@njit(cache=True, nogil=True)
def _round_trip_kernel(new_ref, gidx, geo, pics_flat):
    """WaveletCodec._round_trip in one compiled pass: scatter the flat
    reference into planes, inverse DWT, snap to whole clipped pixels
    (written to pics_flat, the displayed picture), forward DWT, gather."""
    total = pics_flat.shape[0]
    buf = np.empty(total, np.int32)  # see _dwt2_fwd_int
    for i in range(gidx.shape[0]):
        buf[gidx[i]] = new_ref[i]
    for ci in range(geo.shape[0]):
        H, W, L, off = geo[ci, 0], geo[ci, 1], geo[ci, 2], geo[ci, 3]
        c = buf[off:off + H * W].reshape((H, W))
        _dwt2_inv_int(c, L)
        _to_pixels(c, pics_flat[off:off + H * W].reshape((H, W)))
        _dwt2_fwd_int(c, L)
    out = np.empty(gidx.shape[0], np.int64)
    for i in range(gidx.shape[0]):
        out[i] = buf[gidx[i]]
    return out


@njit(cache=True, nogil=True)
def _picture_kernel(ref, gidx, geo, pics_flat):
    """The reference's picture (display only): scatter into planes, inverse
    DWT, whole clipped pixels into pics_flat. The reference itself stays as
    coefficients (see WaveletCodec._round_trip)."""
    total = pics_flat.shape[0]
    buf = np.empty(total, np.int32)  # see _dwt2_fwd_int
    for i in range(gidx.shape[0]):
        buf[gidx[i]] = ref[i]
    for ci in range(geo.shape[0]):
        H, W, L, off = geo[ci, 0], geo[ci, 1], geo[ci, 2], geo[ci, 3]
        c = buf[off:off + H * W].reshape((H, W))
        _dwt2_inv_int(c, L)
        _to_pixels(c, pics_flat[off:off + H * W].reshape((H, W)))


@njit(cache=True, nogil=True)
def _shift_plane(src, qx, qy, out):
    """The picture moved by (qx, qy) QUARTER pixels: out[i, j] = src at
    (i - qy/4, j - qx/4), bilinear, edges repeated. Integer arithmetic,
    identical at both ends."""
    H, W = src.shape
    for i in range(H):
        py = 4 * i - qy
        y0 = py >> 2
        fy = py & 3
        ya = min(max(y0, 0), H - 1)
        yb = min(max(y0 + 1, 0), H - 1)
        for j in range(W):
            px = 4 * j - qx
            x0 = px >> 2
            fx = px & 3
            xa = min(max(x0, 0), W - 1)
            xb = min(max(x0 + 1, 0), W - 1)
            top = (4 - fx) * np.int32(src[ya, xa]) + fx * np.int32(src[ya, xb])
            bot = (4 - fx) * np.int32(src[yb, xa]) + fx * np.int32(src[yb, xb])
            out[i, j] = ((4 - fy) * top + fy * bot + 8) >> 4


@njit(cache=True, nogil=True)
def _sad(cur, prev, dx, dy, step):
    """Sum of |cur - prev shifted by (dx, dy)| over every step-th pixel of
    the overlap (encoder only: motion search)."""
    H, W = cur.shape
    acc = 0
    n = 0
    for i in range(max(0, dy), min(H, H + dy), step):
        for j in range(max(0, dx), min(W, W + dx), step):
            acc += abs(np.int32(cur[i, j]) - np.int32(prev[i - dy, j - dx]))
            n += 1
    return acc / max(n, 1)


@njit(cache=True, nogil=True)
def _sad_full(cur, ref):
    acc = 0
    H, W = cur.shape
    for i in range(H):
        for j in range(W):
            acc += abs(np.int32(cur[i, j]) - np.int32(ref[i, j]))
    return acc / (H * W)


@njit(cache=True, nogil=True)
def _global_motion_hp(cur, prev, rng):
    """Whole-pixel search (_global_motion), then +-1 half pixel around it on
    bilinear-shifted pictures. Returns (hx, hy) in half pixels, cost, cost
    at (0, 0)."""
    bx, by, _, _ = _global_motion(cur, prev, rng)
    tmp = np.empty_like(prev)
    best, hx, hy = 1e30, 2 * bx, 2 * by
    for ty in range(2 * by - 1, 2 * by + 2):
        for tx in range(2 * bx - 1, 2 * bx + 2):
            if abs(tx) > 2 * rng or abs(ty) > 2 * rng:
                continue
            _shift_plane(prev, 2 * tx, 2 * ty, tmp)
            c = _sad_full(cur, tmp)
            if c < best:
                best, hx, hy = c, tx, ty
    return hx, hy, best, _sad_full(cur, prev)


@njit(cache=True, nogil=True)
def _global_motion(cur, prev, rng):
    """Whole-picture shift (dx, dy) of prev that best matches cur: every
    shift in +-rng on a 2-pixel grid of samples, then +-1 around the best
    on every pixel. Returns (dx, dy, cost, cost at (0, 0))."""
    best, bx, by = 1e30, 0, 0
    step = 2 if cur.shape[0] * cur.shape[1] <= 50000 else 4   # coarse sampling
    for dy in range(-rng, rng + 1):
        for dx in range(-rng, rng + 1):
            c = _sad(cur, prev, dx, dy, step)
            if c < best:
                best, bx, by = c, dx, dy
    cx, cy = bx, by
    best = 1e30
    for dy in range(cy - 1, cy + 2):
        for dx in range(cx - 1, cx + 2):
            if abs(dx) <= rng and abs(dy) <= rng:
                c = _sad(cur, prev, dx, dy, 1)
                if c < best:
                    best, bx, by = c, dx, dy
    return bx, by, best, _sad(cur, prev, 0, 0, 1)


@njit(cache=True, nogil=True)
def _down2(a):
    """2x2 average (motion search pyramid)."""
    H, W = a.shape[0] // 2, a.shape[1] // 2
    out = np.empty((H, W), np.uint8)
    for i in range(H):
        for j in range(W):
            out[i, j] = (np.int32(a[2 * i, 2 * j]) + a[2 * i, 2 * j + 1] + a[2 * i + 1, 2 * j]
                         + a[2 * i + 1, 2 * j + 1] + 2) >> 2
    return out


@njit(cache=True, nogil=True)
def _sad_q(cur, prev, qx, qy, step):
    """Mean |cur - prev moved by (qx, qy) quarter pixels|, bilinear, on every
    step-th pixel (edges excluded)."""
    H, W = cur.shape
    m = 2 + (abs(qx) >> 2)
    mv = 2 + (abs(qy) >> 2)
    acc = 0
    n = 0
    for i in range(mv, H - mv, step):
        py = 4 * i - qy
        y0 = py >> 2
        fy = py & 3
        for j in range(m, W - m, step):
            px = 4 * j - qx
            x0 = px >> 2
            fx = px & 3
            top = (4 - fx) * np.int32(prev[y0, x0]) + fx * np.int32(prev[y0, x0 + 1])
            bot = (4 - fx) * np.int32(prev[y0 + 1, x0]) + fx * np.int32(prev[y0 + 1, x0 + 1])
            acc += abs(np.int32(cur[i, j]) - (((4 - fy) * top + fy * bot + 8) >> 4))
            n += 1
    return acc / max(n, 1)


@njit(cache=True, nogil=True)
def _global_motion_fast(cur, prev, rng):
    """Whole-picture shift, coarse to fine: every shift at quarter size, +-1
    at half and full size, then +-1 half pixel -- on samples, not every
    pixel. Returns (hx, hy) half pixels, cost, cost at (0, 0)."""
    c1, p1 = _down2(cur), _down2(prev)
    c2, p2 = _down2(c1), _down2(p1)
    r2 = (rng + 3) // 4
    best, bx, by = 1e30, 0, 0
    for dy in range(-r2, r2 + 1):
        for dx in range(-r2, r2 + 1):
            c = _sad(c2, p2, dx, dy, 1)
            if c < best:
                best, bx, by = c, dx, dy
    for lev in range(2):
        cc, pp = (c1, p1) if lev == 0 else (cur, prev)
        step = 1 if lev == 0 else 2
        cx, cy = 2 * bx, 2 * by
        best = 1e30
        for dy in range(cy - 1, cy + 2):
            for dx in range(cx - 1, cx + 2):
                if abs(dx) <= rng and abs(dy) <= rng:
                    c = _sad(cc, pp, dx, dy, step)
                    if c < best:
                        best, bx, by = c, dx, dy
    hx, hy = 2 * bx, 2 * by
    best = 1e30
    cx, cy = hx, hy
    for ty in range(cy - 1, cy + 2):
        for tx in range(cx - 1, cx + 2):
            if abs(tx) <= 2 * rng and abs(ty) <= 2 * rng:
                c = _sad_q(cur, prev, 2 * tx, 2 * ty, 2)
                if c < best:
                    best, hx, hy = c, tx, ty
    return hx, hy, best, _sad_q(cur, prev, 0, 0, 2)


@njit(cache=True, nogil=True)
def _block_search_fast(cur, prev, gx, gy, blk, rng, min_gain, min_abs, vx, vy):
    """Per block, like _block_search but from a few candidates (no local
    motion, no motion at all, the left and upper blocks' vectors), then
    +-1 pixel and +-1 half pixel around the best: ~20 positions per block
    instead of ~70, each on every other pixel each way."""
    nby, nbx = vx.shape
    cand = np.zeros((4, 2), np.int64)
    for by in range(nby):
        for bx in range(nbx):
            base = _block_sad(cur, prev, by, bx, blk, 2 * gx, 2 * gy, 2)
            if base < min_abs:
                # can't improve by min_abs: no vector whatever the search
                # finds (most blocks of a still or static-background scene)
                vx[by, bx] = 0
                vy[by, bx] = 0
                continue
            best, bdx, bdy = base, 0, 0
            nc = 0
            if abs(gx) <= rng and abs(gy) <= rng:
                cand[nc, 0], cand[nc, 1] = -gx, -gy
                nc += 1
            if bx > 0:
                cand[nc, 0], cand[nc, 1] = vx[by, bx - 1], vy[by, bx - 1]
                nc += 1
            if by > 0:
                cand[nc, 0], cand[nc, 1] = vx[by - 1, bx], vy[by - 1, bx]
                nc += 1
            for c in range(nc):
                dx, dy = cand[c, 0], cand[c, 1]
                if dx == 0 and dy == 0:
                    continue
                v = _block_sad(cur, prev, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy), 2)
                if v < best:
                    best, bdx, bdy = v, dx, dy
            for stp in (2, 1):
                cx, cy = bdx, bdy
                for dy in range(cy - stp, cy + stp + 1, stp):
                    for dx in range(cx - stp, cx + stp + 1, stp):
                        if (dx != cx or dy != cy) and abs(dx) <= rng and abs(dy) <= rng:
                            v = _block_sad(cur, prev, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy), 2)
                            if v < best:
                                best, bdx, bdy = v, dx, dy
            if (bdx != 0 or bdy != 0) and best < (1 - min_gain) * base and base - best >= min_abs:
                vx[by, bx] = bdx
                vy[by, bx] = bdy
            else:
                vx[by, bx] = 0
                vy[by, bx] = 0


@njit(cache=True, nogil=True)
def _block_sad(cur, prev, by, bx, blk, qx, qy, step=1):
    """Mean |cur - prev moved by (qx, qy) quarter pixels| over one block
    (every step-th pixel each way)."""
    H, W = cur.shape
    acc = 0
    n = 0
    i0, i1 = by * blk, min(H, (by + 1) * blk)
    j0, j1 = bx * blk, min(W, (bx + 1) * blk)
    if (qx & 3) == 0 and (qy & 3) == 0:
        # whole-pixel offset: the bilinear weights are (16, 0, 0, 0), so the
        # sample is the pixel itself -- the same sums without the filter
        sy, sx = qy >> 2, qx >> 2
        for i in range(i0, i1, step):
            ya = min(max(i - sy, 0), H - 1)
            for j in range(j0, j1, step):
                xa = min(max(j - sx, 0), W - 1)
                acc += abs(np.int32(cur[i, j]) - np.int32(prev[ya, xa]))
                n += 1
        return acc / max(n, 1)
    for i in range(i0, i1, step):
        py = 4 * i - qy
        y0 = py >> 2
        fy = py & 3
        ya = min(max(y0, 0), H - 1)
        yb = min(max(y0 + 1, 0), H - 1)
        for j in range(j0, j1, step):
            px = 4 * j - qx
            x0 = px >> 2
            fx = px & 3
            xa = min(max(x0, 0), W - 1)
            xb = min(max(x0 + 1, 0), W - 1)
            top = (4 - fx) * np.int32(prev[ya, xa]) + fx * np.int32(prev[ya, xb])
            bot = (4 - fx) * np.int32(prev[yb, xa]) + fx * np.int32(prev[yb, xb])
            v = ((4 - fy) * top + fy * bot + 8) >> 4
            acc += abs(np.int32(cur[i, j]) - v)
            n += 1
    return acc / max(n, 1)


@njit(cache=True, nogil=True)
def _block_search(cur, prev, gx, gy, blk, rng, min_gain, min_abs, vx, vy):
    """Per block: the local correction (half pixels, +-rng) to the global
    motion (gx, gy half pixels) that best matches cur -- whole pixels first,
    then +-1 half pixel. Writes vx, vy (0 where it doesn't pay)."""
    nby, nbx = vx.shape
    for by in range(nby):
        for bx in range(nbx):
            base = _block_sad(cur, prev, by, bx, blk, 2 * gx, 2 * gy)
            best, bdx, bdy = base, 0, 0
            for dy in range(-rng + (rng & 1), rng + 1, 2):
                for dx in range(-rng + (rng & 1), rng + 1, 2):
                    c = _block_sad(cur, prev, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy))
                    if c < best:
                        best, bdx, bdy = c, dx, dy
            cx, cy = bdx, bdy
            for dy in range(cy - 1, cy + 2):
                for dx in range(cx - 1, cx + 2):
                    if abs(dx) <= rng and abs(dy) <= rng and (dx != cx or dy != cy):
                        c = _block_sad(cur, prev, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy))
                        if c < best:
                            best, bdx, bdy = c, dx, dy
            if (bdx != 0 or bdy != 0) and best < (1 - min_gain) * base and base - best >= min_abs:
                vx[by, bx] = bdx
                vy[by, bx] = bdy
            else:
                vx[by, bx] = 0
                vy[by, bx] = 0


@njit(cache=True, nogil=True)
def _warp_plane(src, fx, fy, blk, sub, out):
    """Motion-compensated plane: each pixel moved by the motion field
    interpolated (bilinear, between block centres) from fx, fy (TOTAL motion
    per block, half luma pixels). sub = 1 for luma, 2 for chroma (half size,
    so half the motion in its own pixels). Positions to 1/64 pixel, bilinear
    sampling to 1/16, all integer: identical at both ends."""
    H, W = src.shape
    nby, nbx = fx.shape
    bs = blk // sub                      # block size in this plane's pixels
    for i in range(H):
        # centre-to-centre interpolation weight along y, in 1/bs units
        cy = i - bs // 2
        b0 = cy // bs if cy >= 0 else -1
        wy1 = cy - b0 * bs if cy >= 0 else 0
        y0 = min(max(b0, 0), nby - 1)
        y1 = min(max(b0 + 1, 0), nby - 1)
        if cy < 0:
            wy1 = 0
        for j in range(W):
            cx = j - bs // 2
            a0 = cx // bs if cx >= 0 else -1
            wx1 = cx - a0 * bs if cx >= 0 else 0
            x0 = min(max(a0, 0), nbx - 1)
            x1 = min(max(a0 + 1, 0), nbx - 1)
            wx0 = bs - wx1
            wy0 = bs - wy1
            # motion here, in half luma pixels x bs*bs
            mx = (wy0 * (wx0 * fx[y0, x0] + wx1 * fx[y0, x1]) + wy1 * (wx0 * fx[y1, x0] + wx1 * fx[y1, x1]))
            my = (wy0 * (wx0 * fy[y0, x0] + wx1 * fy[y0, x1]) + wy1 * (wx0 * fy[y1, x0] + wx1 * fy[y1, x1]))
            # -> this plane's pixels in 1/16 units: half luma px = 8/sub sixteenths
            den = bs * bs
            qx = (mx * 8 // sub + (den >> 1)) // den if mx >= 0 else -((-mx * 8 // sub + (den >> 1)) // den)
            qy = (my * 8 // sub + (den >> 1)) // den if my >= 0 else -((-my * 8 // sub + (den >> 1)) // den)
            px = 16 * j - qx
            py = 16 * i - qy
            xa0 = px >> 4
            fxx = px & 15
            ya0 = py >> 4
            fyy = py & 15
            xa = min(max(xa0, 0), W - 1)
            xb = min(max(xa0 + 1, 0), W - 1)
            ya = min(max(ya0, 0), H - 1)
            yb = min(max(ya0 + 1, 0), H - 1)
            top = (16 - fxx) * np.int32(src[ya, xa]) + fxx * np.int32(src[ya, xb])
            bot = (16 - fxx) * np.int32(src[yb, xa]) + fxx * np.int32(src[yb, xb])
            out[i, j] = ((16 - fyy) * top + fyy * bot + 128) >> 8


@njit(cache=True, nogil=True)
def _pixels_to_ref(pics_flat, gidx, geo):
    """Whole pixels (the three planes, concatenated) -> reference
    coefficients (integer, unweighted): what the reference would be had it
    been this picture. Both ends, after a motion shift."""
    buf = np.empty(pics_flat.shape[0], np.int32)  # see _dwt2_fwd_int
    for i in range(buf.shape[0]):
        buf[i] = (np.int32(pics_flat[i]) - 128) << PIX_FRAC
    for ci in range(geo.shape[0]):
        H, W, L, off = geo[ci, 0], geo[ci, 1], geo[ci, 2], geo[ci, 3]
        _dwt2_fwd_int(buf[off:off + H * W].reshape((H, W)), L)
    out = np.empty(gidx.shape[0], np.int64)
    for i in range(gidx.shape[0]):
        out[i] = buf[gidx[i]]
    return out


@njit(cache=True, nogil=True)
def _analyse_kernel(y, u, v, gidx, geo, scale):
    """WaveletCodec._analyse in one compiled pass: weighted wavelet
    coefficients of a frame (encoder only)."""
    total = geo[2, 3] + geo[2, 0] * geo[2, 1]
    buf = np.empty(total, np.int32)  # see _dwt2_fwd_int
    for ci in range(3):
        pl = y if ci == 0 else (u if ci == 1 else v)
        H, W, L, off = geo[ci, 0], geo[ci, 1], geo[ci, 2], geo[ci, 3]
        c = buf[off:off + H * W].reshape((H, W))
        for i in range(H):
            for j in range(W):
                c[i, j] = (np.int32(pl[i, j]) - 128) << PIX_FRAC
        _dwt2_fwd_int(c, L)
    X = np.empty(gidx.shape[0], np.float64)
    for i in range(gidx.shape[0]):
        X[i] = buf[gidx[i]] * scale[i]
    return X


@njit(cache=True, nogil=True)
def _class_sums(X, ref, wflat, coef_cls, band, n_bands, phase, xr, rr):
    """Per-class <X, R> and <R, R> (weighted domain) for the least-squares
    weights. Encoder only, so float is fine."""
    xr[:] = 0.0
    rr[:] = 0.0
    inv = 1.0 / (1 << PIX_FRAC)
    for i in range(X.shape[0]):
        if n_bands > 0 and phase[i] == band:
            continue  # refreshed this frame: predicted from grey
        r = ref[i] * inv * wflat[i]
        c = coef_cls[i]
        xr[c] += X[i] * r
        rr[c] += r * r


@njit(cache=True, nogil=True)
def _predict(ref, coef_cls, a_num, band, n_bands, phase, P):
    """P = round(a[class] * ref), integer, with this frame's refresh set
    (the tiles whose phase is `band`, see _Layout) predicted from grey (0)."""
    half = _A_DEN // 2
    for i in range(ref.shape[0]):
        if n_bands > 0 and phase[i] == band:
            P[i] = 0
        else:
            P[i] = (ref[i] * a_num[coef_cls[i]] + half) // _A_DEN


@njit(cache=True, nogil=True)
def _quantise(X, P, wflat, mag, neg):
    """Integer residual in the weighted domain (encoder only). Returns the top bit-plane."""
    inv = 1.0 / (1 << PIX_FRAC)
    mx = 0
    for i in range(X.shape[0]):
        q = np.rint(X[i] - P[i] * inv * wflat[i])
        if q < 0:
            neg[i] = 1
            m = np.int64(-q)
        else:
            neg[i] = 0
            m = np.int64(q)
        mag[i] = m
        if m > mx:
            mx = m
    top = 0
    while (mx >> (top + 1)) > 0:
        top += 1
    return top


@njit(cache=True, nogil=True)
def _update_ref(P, mag, neg, sigp, lastp, invw, out):
    """New reference = prediction + what the decoder can reconstruct: the
    coded bits of each significant coefficient (down to its last coded
    plane) plus REC_BIAS of the remaining interval, converted from the
    weighted domain by the integer 1/weight table. Encoder and decoder run
    this on identical integers."""
    sh = REC_FRAC + _INVW_FRAC - PIX_FRAC
    half = np.int64(1) << (sh - 1)
    for i in range(P.shape[0]):
        if sigp[i] >= 0:
            lp = lastp[i]
            v = (((mag[i] >> lp) << lp) << REC_FRAC) + _REC_BIAS_FX * ((np.int64(1) << lp) - 1)
            v = (v * invw[i] + half) >> sh
            out[i] = P[i] - v if neg[i] else P[i] + v
        else:
            out[i] = P[i]


# ---------------------------------------------------------------- layout
def _component_subbands(H, W, levels):
    """(y0, x0, h, w, level) in Mallat layout, level 0 = LL."""
    out = [(0, 0, H >> levels, W >> levels, 0)]
    for l in range(levels, 0, -1):
        h, w = H >> l, W >> l
        out += [(0, w, h, w, l), (h, 0, h, w, l), (h, w, h, w, l)]
    return out


def _subband_weights(H, W, levels):
    """Synthesis-basis L2 norm of each subband, from an impulse at its centre."""
    ws = []
    for (y0, x0, h, w, _l) in _component_subbands(H, W, levels):
        c = np.zeros((H, W))
        c[y0 + h // 2, x0 + w // 2] = 1.0
        ws.append(np.sqrt(np.sum(idwt2(c, levels) ** 2)))
    return ws


class _Layout:
    def __init__(self, width, height):
        if width % 32 or height % 16:
            raise ValueError("resolution must be a multiple of 32x16 (e.g. 256x144)")
        self.width, self.height = width, height
        comps = [(height, width, 4, 1.0, 0),
                 (height // 2, width // 2, 3, CHROMA_WEIGHT, 5),
                 (height // 2, width // 2, 3, CHROMA_WEIGHT, 5)]
        self.comps = comps
        # every subband of every component, tagged, then ordered coarse -> fine
        bands = []
        for ci, (H, W, L, cw, cls0) in enumerate(comps):
            wts = _subband_weights(H, W, L)
            for bi, (y0, x0, h, w, lev) in enumerate(_component_subbands(H, W, L)):
                orient = 0 if lev == 0 else (bi - 1) % 3
                cls = cls0 if lev == 0 else cls0 + 1 + (L - lev)
                bands.append(dict(ci=ci, y0=y0, x0=x0, h=h, w=w, lev=lev, L=L, orient=orient,
                                  cls=cls, wt=wts[bi] * cw, size=h * w))
        bands.sort(key=lambda b: (-(b["lev"] == 0), b["size"], b["ci"], b["orient"]))
        off = boff = goff = 0
        for b in bands:
            b["off"], b["boff"], b["goff"] = off, boff, goff
            off += b["size"]
            nbh, nbw = (b["h"] + 3) >> 2, (b["w"] + 3) >> 2
            boff += nbh * nbw
            goff += ((nbh + GROUP - 1) // GROUP) * ((nbw + GROUP - 1) // GROUP)
        for b in bands:  # parent = same comp/orientation, one level coarser
            b["par"] = -1
            if b["lev"] and b["lev"] < b["L"]:
                for pi, p in enumerate(bands):
                    if p["ci"] == b["ci"] and p["lev"] == b["lev"] + 1 and p["orient"] == b["orient"]:
                        b["par"] = pi
        self.bands = bands
        self.n_coef, self.n_blocks, self.n_groups = off, boff, goff
        self.sb_goff = np.array([b["goff"] for b in bands], np.int64)
        self.sb_off = np.array([b["off"] for b in bands], np.int64)
        self.sb_h = np.array([b["h"] for b in bands], np.int64)
        self.sb_w = np.array([b["w"] for b in bands], np.int64)
        self.sb_cls = np.array([b["cls"] for b in bands], np.int64)
        self.sb_par = np.array([b["par"] for b in bands], np.int64)
        self.sb_boff = np.array([b["boff"] for b in bands], np.int64)
        coef_cls = np.repeat(self.sb_cls, [b["size"] for b in bands])
        self.cls_idx = [np.flatnonzero(coef_cls == c) for c in range(N_CLS)]
        self.coef_cls = coef_cls
        # Refresh tiles (WVT4): every coefficient belongs to the REFRESH_TILE
        # square of luma pixels it sits over, at every scale and in all three
        # components; the tiles are ranked in a fixed scrambled order. The
        # rolling refresh renews whole tiles, so a late joiner sees the
        # picture fill in as a mosaic instead of sweeping lines.
        ty, tx = height // REFRESH_TILE, width // REFRESH_TILE
        rank = np.random.RandomState(REFRESH_SEED).permutation(ty * tx).astype(np.int64)
        tile = np.empty(off, np.int64)
        for b in bands:
            L = b["L"]
            s = (L if b["lev"] == 0 else b["lev"]) + (1 if b["ci"] else 0)  # coef -> luma pixel shift
            yy, xx = np.mgrid[0:b["h"], 0:b["w"]]
            t = ((yy << s) // REFRESH_TILE) * tx + ((xx << s) // REFRESH_TILE)
            tile[b["off"]:b["off"] + b["size"]] = rank[t].ravel()
        self.coef_tile = tile

    def refresh_phase(self, n_bands):
        """Per-coefficient refresh phase (0..n_bands-1) for an n_bands-frame refresh."""
        return self.coef_tile % max(1, n_bands)


# ---------------------------------------------------------------- range coder + bit-plane coder
# Range coder state arrays: [low, range, cache, cache_size, pos, code, n_symbols].
# The encoder starts at pos -1: its first output byte is always 0 (LZMA
# property), so it is not sent and the decoder assumes it.
@njit(cache=True, nogil=True, inline="always")
def _shift_low(st, buf):
    low = st[0]
    if low < 0xFF000000 or low >= 0x100000000:
        carry = low >> 32
        temp = st[2]
        while True:
            if 0 <= st[4] < buf.shape[0]:
                buf[st[4]] = (temp + carry) & 0xFF
            st[4] += 1
            temp = 0xFF
            st[3] -= 1
            if st[3] == 0:
                break
        st[2] = (low >> 24) & 0xFF
    st[3] += 1
    st[0] = (low & 0x00FFFFFF) << 8


@njit(cache=True, nogil=True, inline="always")
def _enc(st, p, bit, buf):
    bound = (st[1] >> 16) * p
    if bit == 0:
        st[1] = bound
    else:
        st[0] += bound
        st[1] -= bound
    while st[1] < 0x1000000:
        st[1] <<= 8
        _shift_low(st, buf)


@njit(cache=True, nogil=True, inline="always")
def _sym(mode, st, sh, probs, uses, adapt, counts, ctx, bit, buf, shbuf):
    """Encode (mode 0) `bit` or decode (mode 1) one; returns the bit. When
    decoding, the same bit is also re-encoded into the shadow encoder `sh`
    (output discarded) so the decoder knows the encoder's byte count and
    stops exactly where it did -- no symbol count needs sending.
    Adaptation rate per context: shift = min(adapt[0] + uses // adapt[2],
    adapt[1]) -- fast while a context is new in this frame, then steadier."""
    p = probs[ctx]
    if mode == 0:
        _enc(st, p, bit, buf)
    else:
        bound = (st[1] >> 16) * p
        if st[5] < bound:
            st[1] = bound
            bit = 0
        else:
            st[5] -= bound
            st[1] -= bound
            bit = 1
        while st[1] < 0x1000000:
            st[1] <<= 8
            b = buf[st[4]] if st[4] < buf.shape[0] else 0
            st[5] = ((st[5] << 8) | b) & 0xFFFFFFFF
            st[4] += 1
        _enc(sh, p, bit, shbuf)
    u = uses[ctx]
    s = min(adapt[0] + u // adapt[2], adapt[1])
    uses[ctx] = u + 1
    counts[ctx, bit] += 1
    if bit == 0:
        probs[ctx] = p + ((65536 - p) >> s)
    else:
        probs[ctx] = p - (p >> s)
    st[6] += 1
    return bit


@njit(cache=True, nogil=True, inline="always")
def _room(enc_st, limit, k):
    # Every _shift_low adds exactly 1 to pos + cache_size, one symbol does at
    # most 2, and _flush emits cache_size + 1 more bytes.
    return enc_st[4] + enc_st[3] + 1 + 2 * k <= limit


@njit(cache=True, nogil=True)
def _flush(st, buf):
    """Minimal flush: round low up to a multiple of 2^24 (still inside
    [low, low+range) since range >= 2^24), so everything after its top
    byte is zero -- the decoder reads zeros past the end anyway."""
    st[0] = (st[0] + 0xFFFFFF) & ~np.int64(0xFFFFFF)
    _shift_low(st, buf)
    _shift_low(st, buf)


@njit(cache=True, nogil=True, inline="always")
def _code_sig(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
              mag, neg, sigp, lastp, j, yy, xx, h, w, cls, par, sb_off, sb_w, p, s, sb_nsig):
    """Significance (+ sign) of one coefficient. Returns False when out of room."""
    if not _room(enc_st, limit, 2):
        return False
    hv = 0
    if yy > 0 and sigp[j - w] >= 0: hv += 1
    if yy < h - 1 and sigp[j + w] >= 0: hv += 1
    if xx > 0 and sigp[j - 1] >= 0: hv += 1
    if xx < w - 1 and sigp[j + 1] >= 0: hv += 1
    dg = 0
    if yy > 0 and xx > 0 and sigp[j - w - 1] >= 0: dg = 1
    elif yy > 0 and xx < w - 1 and sigp[j - w + 1] >= 0: dg = 1
    elif yy < h - 1 and xx > 0 and sigp[j + w - 1] >= 0: dg = 1
    elif yy < h - 1 and xx < w - 1 and sigp[j + w + 1] >= 0: dg = 1
    pf = 0
    if par >= 0 and sigp[sb_off[par] + (yy >> 1) * sb_w[par] + (xx >> 1)] >= 0:
        pf = 1
    ctx = CTX_SIG + ((cls * 3 + min(hv, 2)) * 2 + dg) * 2 + pf
    bit = (mag[j] >> p) & 1 if mode == 0 else 0
    bit = _sym(mode, st, sh, probs, uses, adapt, counts, ctx, bit, buf, shbuf)
    if bit:
        sg = _sym(mode, st, sh, probs, uses, adapt, counts, CTX_SIGN, neg[j], buf, shbuf)
        if mode == 1:
            neg[j] = sg
            mag[j] |= 1 << p
        sigp[j] = p
        lastp[j] = p
        sb_nsig[s] += 1
    return True


@njit(cache=True, nogil=True)
def _maxima(mag, sb_off, sb_h, sb_w, sb_boff, sb_goff, blkmax, grpmax, sbmax):
    """Encoder: largest magnitude per block, block group and subband, so each
    "anything significant at this plane?" flag is one shift, not a rescan."""
    for s in range(sb_off.shape[0]):
        off = sb_off[s]; h = sb_h[s]; w = sb_w[s]
        nbw = (w + 3) >> 2
        ngw = (nbw + GROUP - 1) // GROUP
        for yy in range(h):
            for xx in range(w):
                m = mag[off + yy * w + xx]
                b = sb_boff[s] + (yy >> 2) * nbw + (xx >> 2)
                if m > blkmax[b]:
                    blkmax[b] = m
                g = sb_goff[s] + ((yy >> 2) // GROUP) * ngw + (xx >> 2) // GROUP
                if m > grpmax[g]:
                    grpmax[g] = m
                if m > sbmax[s]:
                    sbmax[s] = m


@njit(cache=True, nogil=True)
def _pass_clean(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                mag, neg, sigp, lastp, vis, blks, grps, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                sb_goff, sb_nsig, sb_nblk, blkmax, grpmax, sbmax):
    """Block-flag significance pass over everything not yet significant (and
    not already coded by this plane's propagation pass). Returns True when
    out of room.

    WVT3: two flag levels above the 4x4 blocks. While a subband has no
    flagged block, one symbol says whether anything in it becomes
    significant at this plane; inside it, one symbol per unflagged group of
    GROUP x GROUP blocks. WVT2 coded a flag for every unflagged block on
    every plane -- tens of thousands of near-certain symbols per frame, ~60%
    of encode and decode time, nearly all for still-empty fine subbands."""
    for s in range(sb_off.shape[0]):
        off = sb_off[s]; h = sb_h[s]; w = sb_w[s]; cls = sb_cls[s]; par = sb_par[s]
        nbh = (h + 3) >> 2; nbw = (w + 3) >> 2
        ngh = (nbh + GROUP - 1) // GROUP; ngw = (nbw + GROUP - 1) // GROUP
        if sb_nblk[s] == 0:
            if not _room(enc_st, limit, 1):
                return True
            bit = 1 if mode == 0 and (sbmax[s] >> p) != 0 else 0
            bit = _sym(mode, st, sh, probs, uses, adapt, counts, CTX_SB + cls, bit, buf, shbuf)
            if bit == 0:
                continue
        for gy in range(ngh):
            for gx in range(ngw):
                gidx = sb_goff[s] + gy * ngw + gx
                if grps[gidx] == 0:
                    if not _room(enc_st, limit, 1):
                        return True
                    gpf = 0
                    if par >= 0:
                        pngw = (((sb_w[par] + 3) >> 2) + GROUP - 1) // GROUP
                        gpf = grps[sb_goff[par] + (gy >> 1) * pngw + (gx >> 1)]
                    bit = 1 if mode == 0 and (grpmax[gidx] >> p) != 0 else 0
                    bit = _sym(mode, st, sh, probs, uses, adapt, counts, CTX_GRP + cls * 2 + gpf,
                               bit, buf, shbuf)
                    if bit == 0:
                        continue
                    grps[gidx] = 1
                for by in range(gy * GROUP, min(gy * GROUP + GROUP, nbh)):
                    for bx in range(gx * GROUP, min(gx * GROUP + GROUP, nbw)):
                        bidx = sb_boff[s] + by * nbw + bx
                        y1 = min(by * 4 + 4, h); x1 = min(bx * 4 + 4, w)
                        if blks[bidx] == 0:
                            if not _room(enc_st, limit, 1):
                                return True
                            pf = 0
                            if par >= 0:
                                pnbw = (sb_w[par] + 3) >> 2
                                pf = blks[sb_boff[par] + (by >> 1) * pnbw + (bx >> 1)]
                            nf = 0
                            if bx > 0 and blks[bidx - 1]:
                                nf = 1
                            if by > 0 and blks[bidx - nbw]:
                                nf = 1
                            bit = 1 if mode == 0 and (blkmax[bidx] >> p) != 0 else 0
                            bit = _sym(mode, st, sh, probs, uses, adapt, counts, CTX_BLK + cls * 4 + pf * 2 + nf,
                                       bit, buf, shbuf)
                            if bit == 0:
                                continue
                            blks[bidx] = 1
                            sb_nblk[s] += 1
                        for yy in range(by * 4, y1):
                            for xx in range(bx * 4, x1):
                                j = off + yy * w + xx
                                if sigp[j] >= 0 or vis[j] == p:
                                    continue
                                if not _code_sig(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf,
                                                 limit, mag, neg, sigp, lastp, j, yy, xx, h, w, cls, par, sb_off,
                                                 sb_w, p, s, sb_nsig):
                                    return True
    return False


@njit(cache=True, nogil=True)
def _pass_prop(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
               mag, neg, sigp, lastp, vis, blks, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
               sb_nsig, sb_nblk):
    """Significance propagation: not-yet-significant coefficients in already
    flagged blocks that have a significant neighbour (or parent) -- the ones
    most likely to become significant, so the most useful bits per byte
    when the frame is cut off mid-plane. Returns True when out of room."""
    for s in range(sb_off.shape[0]):
        if sb_nblk[s] == 0:
            continue  # no flagged blocks: nothing can qualify
        off = sb_off[s]; h = sb_h[s]; w = sb_w[s]; cls = sb_cls[s]; par = sb_par[s]
        nbw = (w + 3) >> 2
        for yy in range(h):
            brow = sb_boff[s] + (yy >> 2) * nbw
            for bx in range(nbw):
                if blks[brow + bx] == 0:
                    continue  # (same raster order: unflagged blocks hold nothing to code)
                for xx in range(bx * 4, min(bx * 4 + 4, w)):
                    j = off + yy * w + xx
                    if sigp[j] >= 0:
                        continue
                    nb = False
                    if yy > 0 and sigp[j - w] >= 0: nb = True
                    elif yy < h - 1 and sigp[j + w] >= 0: nb = True
                    elif xx > 0 and sigp[j - 1] >= 0: nb = True
                    elif xx < w - 1 and sigp[j + 1] >= 0: nb = True
                    elif yy > 0 and xx > 0 and sigp[j - w - 1] >= 0: nb = True
                    elif yy > 0 and xx < w - 1 and sigp[j - w + 1] >= 0: nb = True
                    elif yy < h - 1 and xx > 0 and sigp[j + w - 1] >= 0: nb = True
                    elif yy < h - 1 and xx < w - 1 and sigp[j + w + 1] >= 0: nb = True
                    elif par >= 0 and sigp[sb_off[par] + (yy >> 1) * sb_w[par] + (xx >> 1)] >= 0: nb = True
                    if not nb:
                        continue
                    vis[j] = p
                    if not _code_sig(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                                     mag, neg, sigp, lastp, j, yy, xx, h, w, cls, par, sb_off, sb_w, p,
                                     s, sb_nsig):
                        return True
    return False


@njit(cache=True, nogil=True)
def _pass_ref(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
              mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig, blks, sb_boff):
    """Refinement: bit p of every coefficient significant in an earlier
    plane. Returns True when out of room. Skips unflagged 4x4 blocks
    (nothing in them is significant) in the same raster order."""
    for s in range(sb_off.shape[0]):
        if sb_nsig[s] == 0:
            continue  # nothing significant yet in this subband
        off = sb_off[s]; cls = sb_cls[s]; h = sb_h[s]; w = sb_w[s]
        nbw = (w + 3) >> 2
        for yy in range(h):
            brow = sb_boff[s] + (yy >> 2) * nbw
            for bx in range(nbw):
                if blks[brow + bx] == 0:
                    continue
                for xx in range(bx * 4, min(bx * 4 + 4, w)):
                    j = off + yy * w + xx
                    if sigp[j] > p:
                        if not _room(enc_st, limit, 1):
                            return True
                        ctx = CTX_REF + cls * 2 + (0 if sigp[j] == p + 1 else 1)
                        bit = (mag[j] >> p) & 1 if mode == 0 else 0
                        bit = _sym(mode, st, sh, probs, uses, adapt, counts, ctx, bit, buf, shbuf)
                        if mode == 1:
                            mag[j] |= bit << p
                        lastp[j] = p
    return False


@njit(cache=True, nogil=True)
def _code(mode, mag, neg, sigp, lastp, top, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
          n_blocks, sb_goff, n_groups, buf, limit, init_probs, adapt, counts, order):
    """Shared encoder/decoder traversal, stopping when the next symbol might
    not fit `limit` bytes. mode 0: encode mag/neg into buf. mode 1: decode
    buf into mag/neg/sigp/lastp. Every frame starts from `init_probs`
    (no dependence on earlier frames, so a lost packet can't desync the
    model). counts[ctx, bit] is accumulated for training (see
    train_wavelet_probs.py). Returns (n_symbols, n_bytes)."""
    st = np.zeros(7, np.int64)
    st[1] = 0xFFFFFFFF
    st[3] = 1
    st[4] = -1  # first (always zero) byte not sent
    sh = st.copy()  # decoder's shadow encoder: its byte count drives _room
    shbuf = np.zeros(0, np.uint8)
    if mode == 1:
        for i in range(4):  # 5-byte init; the leading zero byte is implicit
            b = buf[i] if i < buf.shape[0] else 0
            st[5] = ((st[5] << 8) | b) & 0xFFFFFFFF
        st[4] = 4
    enc_st = st if mode == 0 else sh
    probs = init_probs.copy()
    uses = np.zeros(N_CTX, np.int64)
    blks = np.zeros(n_blocks, np.uint8)
    grps = np.zeros(n_groups, np.uint8)
    blkmax = np.zeros(n_blocks, np.int64)
    grpmax = np.zeros(n_groups, np.int64)
    sbmax = np.zeros(sb_off.shape[0], np.int64)
    if mode == 0:
        _maxima(mag, sb_off, sb_h, sb_w, sb_boff, sb_goff, blkmax, grpmax, sbmax)
    vis = np.full(mag.shape[0], -1, np.int64)  # plane in which the propagation pass coded j
    sb_nsig = np.zeros(sb_off.shape[0], np.int64)  # significant coefficients per subband
    sb_nblk = np.zeros(sb_off.shape[0], np.int64)  # flagged blocks per subband
    for p in range(top, -1, -1):
        if order == 1:  # JPEG2000-style: propagation, refinement, cleanup
            if _pass_prop(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                          mag, neg, sigp, lastp, vis, blks, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                           sb_nsig, sb_nblk):
                break
            if _pass_ref(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig, blks, sb_boff):
                break
            if _pass_clean(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                           mag, neg, sigp, lastp, vis, blks, grps, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                           sb_goff, sb_nsig, sb_nblk, blkmax, grpmax, sbmax):
                break
        elif order == 2:  # refinement, then significance
            if _pass_ref(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig, blks, sb_boff):
                break
            if _pass_clean(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                           mag, neg, sigp, lastp, vis, blks, grps, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                           sb_goff, sb_nsig, sb_nblk, blkmax, grpmax, sbmax):
                break
        else:  # significance, then refinement
            if _pass_clean(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                           mag, neg, sigp, lastp, vis, blks, grps, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                           sb_goff, sb_nsig, sb_nblk, blkmax, grpmax, sbmax):
                break
            if _pass_ref(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig, blks, sb_boff):
                break
    if mode == 0:
        _flush(st, buf)
    return st[6], st[4]


# ---------------------------------------------------------------- public API
class WaveletCodec:
    """One instance per stream direction: the encoder and decoder each
    hold the previous reconstruction as their prediction reference (the
    integer array `ref`, see _update_ref)."""

    # Defaults from real webcam footage (256x144, 12 fps, 16 kbps): leak
    # 0.996 with a full refresh every 5 s (60 frames) gives +1.4 dB (moving
    # scene) to +4.2 dB (static scene) over leak 0.95 without refresh, with
    # a late joiner reaching that old quality in ~5 s (was ~4.5 s). A high
    # leak WITHOUT the refresh never lets a late joiner catch up: the fine
    # detail it missed is never re-sent. Shorter refresh = faster join but
    # less quality (3 s: ~+0.5/+2 dB).
    def __init__(self, width, height, frame_bytes, leak=0.996, refresh_frames=60, adaptive=True,
                 rd_trial=False, motion=True, block_motion=None):
        if frame_bytes < HEADER_BYTES + MOTION_BYTES + 8:
            raise ValueError("frame_bytes too small")
        self.lay = lay = _Layout(width, height)
        self.threads = _threads_for(width, height)
        self.frame_bytes = frame_bytes
        self.leak_q = int(min(255, max(0, round(leak * 256))))
        # adaptive: pick each class's prediction weight per frame (see
        # _choose_weights); False = every class at the full leak (the old codec).
        self.adaptive = adaptive
        # rd_trial: also trial-encode "all classes at full leak" and keep the
        # better one (see encode). Off by default: ~+0.1 dB for ~1.5x encode
        # time, and it DOUBLES recovery time after a lost packet on moving
        # webcam footage (2.2 s -> 4.2 s), since it favours the full leak.
        self.rd_trial = rd_trial
        # global motion compensation (encoder side; the decoder follows the
        # header): a camera pan shifts the whole picture, which the leaky
        # coefficient-domain prediction alone can't follow
        self.motion = motion
        self._prev_y = None          # previous source luma (encoder motion search)
        # local (block) motion, on top of the global: on unless asked off
        self.block_motion = motion if block_motion is None else block_motion
        H0, W0 = height, width
        self._nby, self._nbx = (H0 + BLK - 1) // BLK, (W0 + BLK - 1) // BLK
        self.init_probs, self.adapt = INIT_PROBS, ADAPT
        self.counts = np.zeros((N_CTX, 2), np.int64)  # encoder-side symbol stats, for training
        # Rolling refresh (see _predict): each frame, every n_bands-th wavelet
        # coefficient is coded from scratch, so every coefficient is refreshed
        # once per n_bands frames. refresh_frames=0 disables it (recovery then
        # relies on the leak alone). Capped at 255 (one header byte).
        self.n_bands = max(1, min(int(refresh_frames), 127)) if refresh_frames else 0  # see MOTION_FLAG
        self.seq = 0
        self._phases = {}
        # Flat coefficient i <-> plane buffer position gidx[i] (Y, U, V planes
        # concatenated), with weight wflat[i] (see _subband_weights).
        self._plane_off = [0]
        for (H, W, _L, _cw, _c) in lay.comps:
            self._plane_off.append(self._plane_off[-1] + H * W)
        gidx = np.empty(lay.n_coef, np.int64)
        wflat = np.empty(lay.n_coef)
        for b in lay.bands:
            H, W = lay.comps[b["ci"]][:2]
            yy, xx = np.mgrid[b["y0"]:b["y0"] + b["h"], b["x0"]:b["x0"] + b["w"]]
            gidx[b["off"]:b["off"] + b["size"]] = (self._plane_off[b["ci"]] + yy * W + xx).ravel()
            wflat[b["off"]:b["off"] + b["size"]] = b["wt"] * Q_SCALE
        self._gidx, self._wflat = gidx, wflat
        self._invw = np.rint((1 << _INVW_FRAC) / wflat).astype(np.int64)
        self._geo = np.array([[H, W, L, self._plane_off[ci]] for ci, (H, W, L, _cw, _c)
                              in enumerate(lay.comps)], np.int64)
        self._ana_scale = (1.0 / (1 << PIX_FRAC)) * wflat
        self.reset()

    def reset(self):
        """Back to a flat grey reference (what a receiver joining late has)."""
        self.ref = np.zeros(self.lay.n_coef, np.int64)
        self.last_picture = [np.full((H, W), 128, np.uint8) for (H, W, _L, _cw, _c) in self.lay.comps]

    # ---- analysis / reference round trip (integer transforms, compiled)
    def _analyse(self, y, u, v):
        """Weighted wavelet coefficients of the input frame (encoder only)."""
        return _analyse_kernel(np.ascontiguousarray(y, np.uint8), np.ascontiguousarray(u, np.uint8),
                               np.ascontiguousarray(v, np.uint8), self._gidx, self._geo, self._ana_scale)

    def _round_trip(self, new_ref, nbytes=None):
        """The new reference, kept as coefficients (format WVT5). Until WVT4
        it went to whole clipped pixels and back -- an inverse and a forward
        DWT every frame at both ends, ~35% of the encoder's time at 640x352
        on a Pi 4 -- only to round off detail the next frame predicts from
        anyway. The picture is made from it only when asked for (picture())."""
        H, W = self.lay.comps[0][:2]
        if nbytes is not None and nbytes * 8 < SNAP_BITS_PER_PIXEL * H * W:
            pics_flat = np.empty(self._plane_off[-1], np.uint8)
            _picture_kernel(new_ref, self._gidx, self._geo, pics_flat)
            # the snapped coefficients are made only when needed: a moving
            # next frame rebuilds the reference from the moved pixels
            # anyway, so a forward DWT per frame was thrown away
            self._ref, self._ref_pixels = None, pics_flat
            self.last_picture = [pics_flat[self._plane_off[ci]:self._plane_off[ci + 1]].reshape(h, w)
                                 for ci, (h, w, _L, _cw, _c) in enumerate(self.lay.comps)]
            return
        self.ref = new_ref
        self.last_picture = None

    @property
    def ref(self):
        """The reference coefficients (made from the snapped pixels on demand)."""
        if self._ref is None:
            self._ref = _pixels_to_ref(self._ref_pixels, self._gidx, self._geo)
            self._ref_pixels = None
        return self._ref

    @ref.setter
    def ref(self, value):
        self._ref, self._ref_pixels = value, None

    def picture(self):
        """The current reference as (y, u, v) uint8 planes, for display."""
        if self.last_picture is None:
            pics_flat = np.empty(self._plane_off[-1], np.uint8)
            _picture_kernel(self.ref, self._gidx, self._geo, pics_flat)
            self.last_picture = [pics_flat[self._plane_off[ci]:self._plane_off[ci + 1]].reshape(H, W)
                                 for ci, (H, W, _L, _cw, _c) in enumerate(self.lay.comps)]
        return self.last_picture

    def _shift_ref(self, hx, hy):
        """Move the reference picture by (hx, hy) HALF pixels -- luma in
        quarter-pixel units 2*hx, chroma (half size) hx -- and make that the
        reference. Both ends, identically."""
        pics = self.picture()
        out = np.empty(self._plane_off[-1], np.uint8)
        for ci, (H, W, _L, _cw, _c) in enumerate(self.lay.comps):
            sx, sy = (2 * hx, 2 * hy) if ci == 0 else (hx, hy)
            _shift_plane(np.ascontiguousarray(pics[ci]), sx, sy,
                         out[self._plane_off[ci]:self._plane_off[ci + 1]].reshape(H, W))
        self.ref = _pixels_to_ref(out, self._gidx, self._geo)
        self.last_picture = None

    def _warp_ref(self, fx, fy):
        """Reference picture moved by the per-block TOTAL motion field (half
        luma pixels); becomes the reference. Both ends, identically."""
        pics = self.picture()
        out = np.empty(self._plane_off[-1], np.uint8)
        for ci, (H, W, _L, _cw, _c) in enumerate(self.lay.comps):
            _warp_plane(np.ascontiguousarray(pics[ci]), fx, fy, BLK, 1 if ci == 0 else 2,
                        out[self._plane_off[ci]:self._plane_off[ci + 1]].reshape(H, W))
        self.ref = _pixels_to_ref(out, self._gidx, self._geo)
        self.last_picture = None

    def _block_field(self, y, gx, gy):
        """Encoder: local vectors (half pixels) per block on top of the
        global (gx, gy), or None if no block pays."""
        if not self.block_motion or self._prev_y is None:
            return None
        vx = np.zeros((self._nby, self._nbx), np.int64)
        vy = np.zeros((self._nby, self._nbx), np.int64)
        _block_search_fast(np.ascontiguousarray(y), self._prev_y, gx, gy, BLK, BLK_RANGE,
                           BLK_MIN_GAIN, BLK_MIN_ABS, vx, vy)
        if not (vx.any() or vy.any()):
            return None
        return vx, vy

    @staticmethod
    def _pack_field(vx, vy):
        mask = ((vx != 0) | (vy != 0)).reshape(-1)
        bitmap = np.packbits(mask.astype(np.uint8))
        vals = bytes(((int(a) & 0xF) << 4) | (int(b) & 0xF) for a, b in zip(vx.reshape(-1)[mask], vy.reshape(-1)[mask]))
        data = bitmap.tobytes() + vals
        return len(data).to_bytes(2, "big") + data

    def _unpack_field(self, pkt, pos):
        if pos + 2 > len(pkt):
            raise ValueError("bad block field")
        n = int.from_bytes(pkt[pos:pos + 2], "big")
        pos += 1                            # (so pos + 1 is the data, as below)
        nb = self._nby * self._nbx
        nbm = (nb + 7) // 8
        if n < nbm or pos + 1 + n > len(pkt):
            raise ValueError("bad block field")
        mask = np.unpackbits(np.frombuffer(pkt[pos + 1:pos + 1 + nbm], np.uint8))[:nb].astype(bool)
        vals = pkt[pos + 1 + nbm:pos + 1 + n]
        if len(vals) != int(mask.sum()):
            raise ValueError("bad block field")
        vx = np.zeros(nb, np.int64)
        vy = np.zeros(nb, np.int64)
        for k, b in zip(np.flatnonzero(mask), vals):
            a, c = b >> 4, b & 0xF
            vx[k] = a - 16 if a > 7 else a
            vy[k] = c - 16 if c > 7 else c
        return vx.reshape(self._nby, self._nbx), vy.reshape(self._nby, self._nbx), pos + 1 + n

    def _motion_search(self, y):
        """Encoder: the global shift to use for frame y, or (0, 0)."""
        if not self.motion or self._prev_y is None:
            return 0, 0
        # against the previous camera frame, not the decoded picture: no
        # inverse transform just to search, and no coding noise in the match
        dx, dy, cost, cost0 = _global_motion_fast(np.ascontiguousarray(y), self._prev_y, MC_RANGE)
        if (dx, dy) != (0, 0) and cost < MC_GAIN * cost0:
            return int(dx), int(dy)
        return 0, 0

    # ---- prediction
    def _choose_weights(self, X, band):
        """Per-class weight index k (prediction = k/WEIGHT_LEVELS * leak * R)
        minimising the residual energy |X - a*R|^2 -- a least-squares fit,
        clipped to [0, leak] so a lost packet's error still decays by at
        least `leak` per frame. A scene cut drives a class to 0 (no
        prediction), a static scene to the full leak."""
        cap = self.leak_q / 256.0
        k = np.full(N_CLS, WEIGHT_LEVELS, np.int64)
        if not self.adaptive or cap == 0:
            return k
        xr, rr = np.zeros(N_CLS), np.zeros(N_CLS)
        _class_sums(X, self.ref, self._wflat, self.lay.coef_cls, band, self.n_bands,
                    self._phase(self.n_bands), xr, rr)
        for c in range(N_CLS):
            a = xr[c] / rr[c] if rr[c] > 0 else 0.0
            k[c] = int(np.clip(np.rint(a / cap * WEIGHT_LEVELS), 0, WEIGHT_LEVELS))
        return k

    def _prediction(self, leak_q, k, band, n_bands):
        P = np.empty(self.lay.n_coef, np.int64)
        _predict(self.ref, self.lay.coef_cls, leak_q * k.astype(np.int64), band, n_bands,
                 self._phase(n_bands), P)
        return P

    def _phase(self, n_bands):
        """Refresh phase per coefficient, cached per refresh period (the
        decoder takes the period from each packet's header)."""
        ph = self._phases.get(n_bands)
        if ph is None:
            ph = self._phases[n_bands] = self.lay.refresh_phase(n_bands)
        return ph

    def _run_coder(self, mode, mag, neg, top, body, counts):
        lay = self.lay
        sigp = np.full(lay.n_coef, -1, np.int64)
        lastp = np.full(lay.n_coef, -1, np.int64)
        nsym, nbytes = _code(mode, mag, neg, sigp, lastp, top, lay.sb_off, lay.sb_h, lay.sb_w,
                             lay.sb_cls, lay.sb_par, lay.sb_boff, lay.n_blocks, lay.sb_goff, lay.n_groups,
                             body, body.shape[0],
                             self.init_probs, self.adapt, counts, int(PASS_ORDER))
        return sigp, lastp, nsym, nbytes

    def _encode_trial(self, X, k, band, header_bytes=HEADER_BYTES):
        """Code X with class weights k. Returns (distortion, k, top, body,
        new reference, symbol counts) -- nothing committed yet. The new
        reference comes straight from what was coded (no self-decode)."""
        P = self._prediction(self.leak_q, k, band, self.n_bands)
        n = self.lay.n_coef
        mag, neg = np.empty(n, np.int64), np.empty(n, np.int64)
        top = _quantise(X, P, self._wflat, mag, neg)
        assert top < (1 << TOP_BITS), top
        body = np.zeros(self.frame_bytes - header_bytes, np.uint8)
        counts = np.zeros((N_CTX, 2), np.int64)
        sigp, lastp, _, nbytes = self._run_coder(0, mag, neg, top, body, counts)
        assert nbytes <= body.shape[0], (nbytes, body.shape[0])
        new_ref = np.empty(n, np.int64)
        _update_ref(P, mag, neg, sigp, lastp, self._invw, new_ref)
        dist = (float(np.sum((X - new_ref * (1.0 / (1 << PIX_FRAC)) * self._wflat) ** 2))
                if self.rd_trial else 0.0)
        return dist, k, top, body, new_ref, counts

    def encode(self, y, u, v):
        """y: (H,W) uint8, u/v: (H/2,W/2) uint8. Returns exactly frame_bytes bytes."""
        _use_threads(self.threads)
        band = self.seq % self.n_bands if self.n_bands else 0
        mdx, mdy = self._motion_search(y)
        moved = (mdx, mdy) != (0, 0)
        field = self._block_field(y, mdx, mdy)
        fpk = self._pack_field(*field) if field is not None else b""
        # the vectors may not crowd out the picture: at most a quarter of the
        # frame (and the length byte's 255) -- else global motion alone
        if fpk and len(fpk) > BLK_MAX_SHARE * self.frame_bytes:
            field, fpk = None, b""
        if field is not None:
            self._warp_ref(field[0] + mdx, field[1] + mdy)
        elif moved:
            self._shift_ref(mdx, mdy)
        # the motion vector costs its 2 bytes only on frames that moved: at
        # 5 fps / 5 kbps (125 B frames) always sending it lost ~0.3 dB on stills
        hlen = HEADER_BYTES + (MOTION_BYTES if moved else 0) + len(fpk)
        X = self._analyse(y, u, v)
        k_ls = self._choose_weights(X, band)
        # Least squares on residual energy is only a proxy for quality; with
        # rd_trial, also try "every class at the full leak" and keep whichever
        # actually reconstructs better. Encoder-side only.
        cands = [k_ls]
        if self.rd_trial and self.adaptive and self.leak_q and not np.all(k_ls == WEIGHT_LEVELS):
            cands.append(np.full(N_CLS, WEIGHT_LEVELS, np.int64))
        best = None
        for k in cands:
            trial = self._encode_trial(X, k, band, hlen)
            if best is None or trial[0] < best[0]:
                best = trial
        _, k, top, body, new_ref, counts = best
        self._round_trip(new_ref, self.frame_bytes)
        self.counts += counts
        packed = top
        for kc in k:
            packed = (packed << WEIGHT_BITS) | int(kc)
        hdr = packed.to_bytes(4, "big") + bytes([self.leak_q, band | (BLOCK_FLAG if fpk else 0),
                                                 self.n_bands | (MOTION_FLAG if moved else 0)])
        if moved:
            hdr += bytes([mdx & 0xFF, mdy & 0xFF])
        hdr += fpk
        if self.motion or self.block_motion:
            self._prev_y = np.ascontiguousarray(y).copy()
        self.seq += 1
        return hdr + body.tobytes()

    def decode(self, pkt):
        """Returns (y, u, v) uint8 planes; raises ValueError on a malformed packet.
        Packets must be fed in order; a missing one is simply skipped (the
        leak and rolling refresh repair the resulting reference error)."""
        _use_threads(self.threads)
        if len(pkt) < HEADER_BYTES:
            raise ValueError("short packet")
        packed = int.from_bytes(pkt[0:4], "big")
        top = packed >> (N_CLS * WEIGHT_BITS)
        k = np.array([(packed >> (WEIGHT_BITS * (N_CLS - 1 - c))) & WEIGHT_LEVELS for c in range(N_CLS)],
                     np.int64)
        leak_q, band, n_bands = pkt[4], pkt[5] & 0x7F, pkt[6] & 0x7F
        has_field = bool(pkt[5] & BLOCK_FLAG)
        if n_bands and band >= n_bands:
            raise ValueError("bad header")
        hlen = HEADER_BYTES
        mdx = mdy = 0
        if pkt[6] & MOTION_FLAG:
            if len(pkt) < HEADER_BYTES + MOTION_BYTES:
                raise ValueError("short packet")
            mdx, mdy = pkt[7] - 256 * (pkt[7] > 127), pkt[8] - 256 * (pkt[8] > 127)
            hlen += MOTION_BYTES
            if abs(mdx) > 2 * MC_RANGE or abs(mdy) > 2 * MC_RANGE or (mdx, mdy) == (0, 0):
                raise ValueError("bad header")
        field = None
        if has_field:
            fvx, fvy, hlen = self._unpack_field(pkt, hlen)
            field = (fvx + mdx, fvy + mdy)
        if field is not None:
            self._warp_ref(*field)
        elif (mdx, mdy) != (0, 0):
            self._shift_ref(mdx, mdy)
        body = np.frombuffer(pkt, np.uint8, offset=hlen).copy()
        P = self._prediction(leak_q, k, band, n_bands)
        n = self.lay.n_coef
        mag, neg = np.zeros(n, np.int64), np.zeros(n, np.int64)
        sigp, lastp, _, _ = self._run_coder(1, mag, neg, top, body, np.zeros((N_CTX, 2), np.int64))
        new_ref = np.empty(n, np.int64)
        _update_ref(P, mag, neg, sigp, lastp, self._invw, new_ref)
        self._round_trip(new_ref, len(pkt))
        return self.picture()

    def decode_bgr(self, pkt, cv2=None):
        if cv2 is None:  # e.g. media_rx_gui.py's VideoWorker, which has no cv2 window of its own
            import cv2
        y, u, v = self.decode(pkt)
        i420 = np.concatenate([y, u.reshape(-1, y.shape[1]), v.reshape(-1, y.shape[1])])
        return cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
