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
from numba import njit

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
HEADER_BYTES = 4 + 3  # [top | weights] (32 bits), leak, refresh phase, refresh period
REFRESH_TILE = 16   # rolling-refresh tile, luma pixels (>= the coarsest scale, 16)
REFRESH_SEED = 0x5754  # fixed tile order: both ends must agree (WVT4)

# ---- entropy-coder model (part of the bitstream format: both ends must match) ----
# Initial P(bit == 0) per context, 16-bit, from train_wavelet_probs.py
# (webcam + synthetic, 8-25 kbps). Worth +1.1 to +3.4 dB over a flat 0.5
# start on held-out webcam clips: a frame is only ~1300 coded bits, too
# few for most contexts to adapt from scratch. 32768 = never used.
INIT_PROBS = np.array([
    59190, 52770, 32768, 32768, 62924, 53383, 32768, 32768, 64512, 57845, 60285, 46827,
    64512, 56305, 61820, 47193, 64512, 59482, 63874, 49715, 64500, 52139, 32768, 32768,
    64512, 52670, 32768, 32768, 64512, 57573, 63440, 49804, 64512, 56294, 64216, 41929,
    61905, 32768, 60193, 32768, 60214, 32768, 48285, 32768, 46538, 32768, 36857, 32768,
    61129, 32768, 60881, 32768, 59127, 32768, 51655, 32768, 45835, 32768, 41365, 32768,
    61385, 55938, 61577, 53698, 60064, 51705, 52848, 46970, 46502, 46242, 43339, 38698,
    61208, 56804, 61448, 55880, 58201, 51449, 53377, 49511, 44815, 43625, 40824, 36609,
    61020, 56952, 61650, 56003, 56835, 51254, 53948, 46176, 44998, 43266, 39824, 37577,
    61064, 32768, 60813, 32768, 56929, 32768, 49668, 32768, 41243, 32768, 35901, 32768,
    60072, 32768, 60339, 32768, 55211, 32768, 53134, 32768, 44675, 32768, 40222, 32768,
    60931, 53683, 63453, 57781, 50362, 51064, 59802, 55224, 34646, 44580, 35019, 39322,
    61782, 54808, 64512, 64199, 41775, 19946, 64512, 62747, 39322, 37449, 46811, 32768,
    32654, 42274, 35713, 46273, 37462, 47799, 40659, 48112, 42214, 50528, 44146, 49253,
    47243, 54015, 42114, 53926, 41478, 56663, 57344,
    # CTX_SB, CTX_GRP (WVT3): starting guesses -- an empty subband/group
    # usually stays empty; retrain with train_wavelet_probs.py
    60000, 60000, 60000, 60000, 60000, 60000, 60000, 60000, 60000,
    62000, 50000, 62000, 50000, 62000, 50000, 62000, 50000, 62000, 50000,
    62000, 50000, 62000, 50000, 62000, 50000, 62000, 50000,
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
@njit(cache=True, nogil=True)
def _fwd_axis0_int(c, h, w, T):
    hh = h // 2
    for i in range(hh):
        for j in range(w):
            T[i, j] = c[2 * i, j]
            T[hh + i, j] = c[2 * i + 1, j]
    for i in range(hh):
        ip = min(i + 1, hh - 1)
        for j in range(w):
            T[hh + i, j] += (_A_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
    for i in range(hh):
        im = max(i - 1, 0)
        for j in range(w):
            T[i, j] += (_B_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
    for i in range(hh):
        ip = min(i + 1, hh - 1)
        for j in range(w):
            T[hh + i, j] += (_G_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
    for i in range(hh):
        im = max(i - 1, 0)
        for j in range(w):
            T[i, j] += (_D_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
    for i in range(hh):
        for j in range(w):
            c[i, j] = (T[i, j] * _KI_FX + _LH) >> _LS
            c[hh + i, j] = (T[hh + i, j] * _K_FX + _LH) >> _LS


@njit(cache=True, nogil=True)
def _inv_axis0_int(c, h, w, T):
    hh = h // 2
    for i in range(hh):
        for j in range(w):
            T[i, j] = (c[i, j] * _K_FX + _LH) >> _LS
            T[hh + i, j] = (c[hh + i, j] * _KI_FX + _LH) >> _LS
    for i in range(hh):
        im = max(i - 1, 0)
        for j in range(w):
            T[i, j] -= (_D_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
    for i in range(hh):
        ip = min(i + 1, hh - 1)
        for j in range(w):
            T[hh + i, j] -= (_G_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
    for i in range(hh):
        im = max(i - 1, 0)
        for j in range(w):
            T[i, j] -= (_B_FX * (T[hh + im, j] + T[hh + i, j]) + _LH) >> _LS
    for i in range(hh):
        ip = min(i + 1, hh - 1)
        for j in range(w):
            T[hh + i, j] -= (_A_FX * (T[i, j] + T[ip, j]) + _LH) >> _LS
    for i in range(hh):
        for j in range(w):
            c[2 * i, j] = T[i, j]
            c[2 * i + 1, j] = T[hh + i, j]


# The planes are int32 (coefficients stay within ~+-2^21: pixels << PIX_FRAC
# with a few bits of growth over 4 levels) while each lifting product is
# formed in 64 bits -- the same integers, so bit-identical to all-int64, but
# int32 x const -> int64 maps to ARM NEON's widening multiply. NEON has no
# 64-bit vector multiply: with int64 planes these transforms ran ~8x slower
# on a Pi 4 than on the dev PC (the rest of the codec ~3x), the largest cost
# in both encoder and decoder there.
@njit(cache=True, nogil=True)
def _dwt2_fwd_int(c, levels):
    H, W = c.shape
    T = np.empty_like(c)
    t = np.empty_like(c[0])
    h, w = H, W
    for _ in range(levels):
        _fwd_axis0_int(c, h, w, T)
        for i in range(h):
            _fwd_1d_int(c[i], t, w)
        h //= 2
        w //= 2


@njit(cache=True, nogil=True)
def _dwt2_inv_int(c, levels):
    H, W = c.shape
    T = np.empty_like(c)
    t = np.empty_like(c[0])
    for l in range(levels - 1, -1, -1):
        h, w = H >> l, W >> l
        for i in range(h):
            _inv_1d_int(c[i], t, w)
        _inv_axis0_int(c, h, w, T)


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
            for xx in range(w):
                j = off + yy * w + xx
                if sigp[j] >= 0 or blks[sb_boff[s] + (yy >> 2) * nbw + (xx >> 2)] == 0:
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
              mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig):
    """Refinement: bit p of every coefficient significant in an earlier
    plane. Returns True when out of room."""
    for s in range(sb_off.shape[0]):
        if sb_nsig[s] == 0:
            continue  # nothing significant yet in this subband
        off = sb_off[s]; cls = sb_cls[s]
        for j in range(off, off + sb_h[s] * sb_w[s]):
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
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig):
                break
            if _pass_clean(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                           mag, neg, sigp, lastp, vis, blks, grps, p, sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff,
                           sb_goff, sb_nsig, sb_nblk, blkmax, grpmax, sbmax):
                break
        elif order == 2:  # refinement, then significance
            if _pass_ref(mode, st, sh, enc_st, probs, uses, adapt, counts, buf, shbuf, limit,
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig):
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
                         mag, sigp, lastp, p, sb_off, sb_h, sb_w, sb_cls, sb_nsig):
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
                 rd_trial=False):
        if frame_bytes < HEADER_BYTES + 8:
            raise ValueError("frame_bytes too small")
        self.lay = lay = _Layout(width, height)
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
        self.init_probs, self.adapt = INIT_PROBS, ADAPT
        self.counts = np.zeros((N_CTX, 2), np.int64)  # encoder-side symbol stats, for training
        # Rolling refresh (see _predict): each frame, every n_bands-th wavelet
        # coefficient is coded from scratch, so every coefficient is refreshed
        # once per n_bands frames. refresh_frames=0 disables it (recovery then
        # relies on the leak alone). Capped at 255 (one header byte).
        self.n_bands = max(1, min(int(refresh_frames), 255)) if refresh_frames else 0
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

    def _round_trip(self, new_ref):
        """Reference -> whole clipped pixels (the displayed picture) -> back
        to coefficients. Integer throughout; run identically by both ends."""
        pics_flat = np.empty(self._plane_off[-1], np.uint8)
        self.ref = _round_trip_kernel(new_ref, self._gidx, self._geo, pics_flat)
        self.last_picture = [pics_flat[self._plane_off[ci]:self._plane_off[ci + 1]].reshape(H, W)
                             for ci, (H, W, _L, _cw, _c) in enumerate(self.lay.comps)]

    def picture(self):
        """The current reference as (y, u, v) uint8 planes, for display."""
        return self.last_picture

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

    def _encode_trial(self, X, k, band):
        """Code X with class weights k. Returns (distortion, k, top, body,
        new reference, symbol counts) -- nothing committed yet. The new
        reference comes straight from what was coded (no self-decode)."""
        P = self._prediction(self.leak_q, k, band, self.n_bands)
        n = self.lay.n_coef
        mag, neg = np.empty(n, np.int64), np.empty(n, np.int64)
        top = _quantise(X, P, self._wflat, mag, neg)
        assert top < (1 << TOP_BITS), top
        body = np.zeros(self.frame_bytes - HEADER_BYTES, np.uint8)
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
        band = self.seq % self.n_bands if self.n_bands else 0
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
            trial = self._encode_trial(X, k, band)
            if best is None or trial[0] < best[0]:
                best = trial
        _, k, top, body, new_ref, counts = best
        self._round_trip(new_ref)
        self.counts += counts
        packed = top
        for kc in k:
            packed = (packed << WEIGHT_BITS) | int(kc)
        hdr = packed.to_bytes(4, "big") + bytes([self.leak_q, band, self.n_bands])
        self.seq += 1
        return hdr + body.tobytes()

    def decode(self, pkt):
        """Returns (y, u, v) uint8 planes; raises ValueError on a malformed packet.
        Packets must be fed in order; a missing one is simply skipped (the
        leak and rolling refresh repair the resulting reference error)."""
        if len(pkt) < HEADER_BYTES:
            raise ValueError("short packet")
        packed = int.from_bytes(pkt[0:4], "big")
        top = packed >> (N_CLS * WEIGHT_BITS)
        k = np.array([(packed >> (WEIGHT_BITS * (N_CLS - 1 - c))) & WEIGHT_LEVELS for c in range(N_CLS)],
                     np.int64)
        leak_q, band, n_bands = pkt[4], pkt[5], pkt[6]
        if n_bands and band >= n_bands:
            raise ValueError("bad header")
        body = np.frombuffer(pkt, np.uint8, offset=HEADER_BYTES).copy()
        P = self._prediction(leak_q, k, band, n_bands)
        n = self.lay.n_coef
        mag, neg = np.zeros(n, np.int64), np.zeros(n, np.int64)
        sigp, lastp, _, _ = self._run_coder(1, mag, neg, top, body, np.zeros((N_CTX, 2), np.int64))
        new_ref = np.empty(n, np.int64)
        _update_ref(P, mag, neg, sigp, lastp, self._invw, new_ref)
        self._round_trip(new_ref)
        return self.last_picture

    def decode_bgr(self, pkt, cv2=None):
        if cv2 is None:  # e.g. media_rx_gui.py's VideoWorker, which has no cv2 window of its own
            import cv2
        y, u, v = self.decode(pkt)
        i420 = np.concatenate([y, u.reshape(-1, y.shape[1]), v.reshape(-1, y.shape[1])])
        return cv2.cvtColor(i420, cv2.COLOR_YUV2BGR_I420)
