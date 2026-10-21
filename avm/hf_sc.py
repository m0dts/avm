"""S: a DVB-S2-style single-carrier mode for fixed and line-of-sight paths
(satellite, terrestrial point to point) -- not for HF multipath.

One RRC-shaped carrier (roll-off ALPHA) per AVM fragment, as a burst:

    preamble   PRE_LEN known BPSK symbols (m-sequence): detection, timing,
               frequency (to +-symbol_rate/2), phase
    header     the payload length + a checksum, BPSK, each bit repeated
               HDR_REPEAT times, scrambled
    data       payload + CRC-32, AVM's LDPC (rate 1/2), interleaved and
               whitened as the OFDM modes do, QPSK; PILOT_LEN known pilot
               symbols before, between every DATA_BLOCK data symbols, and
               after -- for phase and timing tracking

Width -> symbol rate: occupancy / (1 + ALPHA): 10 kHz = 8 kBd. Worked at
SPS samples per symbol (cfg.fs = SPS * symbol rate).

Compared with the OFDM modes at the same width: about 3 dB less peak to
average (an amplifier gives more power), far less sensitive to phase noise
and frequency error, but no defence against echoes. hf_ofdm_common
dispatches here for mode "S" (build_config, build_frame and the bitrate
estimates); hf_ofdm_rx calls receive_one_fragment."""
import time
import zlib

import numpy as np

import hf_ofdm_ldpc as ldpc

MODE = "S"
WIDTHS_KHZ = (10, 20, 40)
ALPHA = 0.25
SPS = 4
RRC_SPAN = 8                    # symbols either side
PRE_LEN = 256
HDR_BITS = 24                   # 16-bit length + 8-bit checksum
HDR_REPEAT = 8
DATA_BLOCK = 128
PILOT_LEN = 8
LAG_FINE = 16                   # preamble frequency estimate: lag 1 (range) then lag 16 (accuracy)
FINE_LAGS = (16, 64, 128)       # ... then refined over these lags in turn (_sync)
DETECT_THRESHOLD = 0.32         # |correlation| / energy: noise alone peaks ~0.07, rare extremes ~0.25; a burst at 0 dB ~0.5
SEARCH_CHUNK_S = 2.0            # look for a preamble this far at a time
LLR_CLIP = 40.0
PHASE_SMOOTH = 5                # pilot blocks averaged for the first phase track
DD_WINDOW = 32                  # data-aided phase refinement: symbols per estimate
DD_SMOOTH = 5                   # ... and estimates averaged
SOFT_DD = True                  # data-aided phase from soft (expected) symbols, not hard decisions
EXACT_LLR = True                # exact soft bits (log-sum-exp over all points), not max-log
EQUALISE = True                 # short per-frame equaliser (_equalise)
EQ_PRE, EQ_POST = 3, 8          # its taps before / after the main path (symbols)
EQ_RIDGE = 0.01                 # its damping (relative to the signal power)
EQ_MIN_GAIN_DB = 0.5            # used only if it cuts the pilots' error by this much


class SCConfig:
    """Duck-types the parts of hf_ofdm_common.OFDMConfig the rest of AVM
    reads: fs, the occupied band (data_bins x carrier_spacing: spectrum
    view, tuning guides, AFC range), symbol_len/preamble_len (frame length
    sums) -- and is_sc for the dispatch."""
    is_sc = True
    sc_module = "hf_sc"     # single-carrier modes: the module hf_ofdm_common dispatches to

    def __init__(self, occupancy_khz, data_modulation="qpsk", fec_scheme="ldpc"):
        if int(round(occupancy_khz)) not in WIDTHS_KHZ:
            raise ValueError(f"mode S is {', '.join(map(str, WIDTHS_KHZ))} kHz wide (got {occupancy_khz:g})")
        self.mode = MODE
        self.occupancy_hz = float(occupancy_khz) * 1000
        self.symbol_rate = self.occupancy_hz / (1 + ALPHA)
        self.fs = SPS * self.symbol_rate
        if data_modulation not in S_MODULATIONS:
            raise ValueError(f"mode S has {', '.join(S_MODULATIONS)} (got {data_modulation})")
        self.data_modulation = data_modulation
        self.bits_per_symbol = BITS_PER_SYMBOL[data_modulation]
        self.fec_scheme = "ldpc"
        self.symbol_len = SPS
        self.preamble_len = PRE_LEN * SPS
        self.n_cp = SPS
        # a notional carrier grid covering the occupied band; 31 of these
        # spacings = the frequency pull-in (+-symbol_rate/2, the preamble's
        # lag-1 estimate), as 31 carriers is the OFDM modes' (RX tuning guides)
        self.n_fft = 128
        self.carrier_spacing = self.symbol_rate / 62
        k = int(round(self.occupancy_hz / 2 / self.carrier_spacing - 0.5))
        self.data_bins = np.arange(-k, k + 1)
        self.fft_bins = self.data_bins % self.n_fft

    def __repr__(self):
        return f"SCConfig({self.occupancy_hz / 1e3:g} kHz, {self.symbol_rate:.0f} Bd)"


def is_sc(cfg):
    return getattr(cfg, "is_sc", False)


# ---------------------------------------------------------------- sequences
def _lfsr_bits(n, taps=(8, 6, 5, 4), seed=1):
    """An m-sequence from an 8-bit LFSR (period 255)."""
    state, out = seed, np.empty(n, np.uint8)
    for i in range(n):
        bit = 0
        for t in taps:
            bit ^= (state >> (t - 1)) & 1
        out[i] = state & 1
        state = ((state << 1) | bit) & 0xFF
    return out


PREAMBLE = (1.0 - 2.0 * np.concatenate([_lfsr_bits(255), [0]])).astype(np.complex64)
_rng = np.random.default_rng(20261019)
_QPSK = (np.array([1 + 1j, -1 + 1j, -1 - 1j, 1 - 1j]) / np.sqrt(2)).astype(np.complex64)
PILOTS = _QPSK[_rng.integers(0, 4, 4096)]          # pilot block b uses PILOTS[b*PILOT_LEN ...]
HDR_SCRAMBLE = _rng.integers(0, 2, HDR_BITS * HDR_REPEAT).astype(np.uint8)


def _gray_psk(m):
    """Unit-power m-PSK, Gray-coded: entry v = the point for bit value v
    (first bit most significant). Neighbouring points differ in one bit."""
    pts = np.empty(m, np.complex64)
    for k in range(m):
        pts[k ^ (k >> 1)] = np.exp(1j * (2 * np.pi * k / m + (np.pi / 4 if m == 4 else 0.0)))
    return pts


# data constellations (pilots and preamble stay QPSK/BPSK whatever the data)
CONSTELLATIONS = {"qpsk": _gray_psk(4), "8psk": _gray_psk(8), "16psk": _gray_psk(16)}
BITS_PER_SYMBOL = {"qpsk": 2, "8psk": 3, "16psk": 4}
S_MODULATIONS = ("qpsk", "8psk")     # mode S; 16PSK is mode SH only


def _rrc(alpha=ALPHA, sps=SPS, span=RRC_SPAN):
    t = np.arange(-span * sps, span * sps + 1) / sps
    h = np.empty(len(t))
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                         + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = ((np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha)))
                    / (np.pi * x * (1 - (4 * alpha * x) ** 2)))
    return (h / np.sqrt(np.sum(h ** 2))).astype(np.float32)


RRC = _rrc()
RRC_DELAY = len(RRC) // 2


def _crc8(bits):
    c = 0
    for b in bits:
        c ^= int(b) << 7
        c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
    return c


def _header_bits(length):
    f = np.unpackbits(np.array([length >> 8, length & 0xFF], np.uint8))
    return np.concatenate([f, np.unpackbits(np.array([_crc8(f)], np.uint8))])


# ---------------------------------------------------------------- frame layout
def _coded_bits(n_payload_bytes):
    return ldpc.ldpc_coded_len((n_payload_bytes + 4) * 8)


def _layout(n_payload_bytes, bps=2):
    """(n_data_symbols, n_pilot_blocks, total symbols) for a payload at bps
    coded bits per data symbol."""
    n_data = -(-_coded_bits(n_payload_bytes) // bps)
    n_blocks = -(-n_data // DATA_BLOCK)
    n_pilot_blocks = n_blocks + 1
    total = PRE_LEN + HDR_BITS * HDR_REPEAT + n_data + n_pilot_blocks * PILOT_LEN
    return n_data, n_pilot_blocks, total


def frame_samples(cfg, n_payload_bytes):
    """A frame's length on air at cfg.fs (filter tails included)."""
    return _layout(n_payload_bytes, cfg.bits_per_symbol)[2] * SPS + 2 * RRC_DELAY - (SPS - 1)


def frame_symbol_count(cfg, n_payload_bytes):
    """As hf_ofdm_common.frame_symbol_count: n * symbol_len + preamble_len =
    the frame's samples."""
    return -(-(frame_samples(cfg, n_payload_bytes) - cfg.preamble_len) // SPS)


def payload_bitrate(cfg):
    """Ceiling (no preamble/header): rate 1/2 less the pilots."""
    return cfg.symbol_rate * cfg.bits_per_symbol * 0.5 * DATA_BLOCK / (DATA_BLOCK + PILOT_LEN)


def _data_positions(n_data, n_pilot_blocks):
    """Symbol indices (from the start of the data section) of the data, and
    where each pilot block starts: pilots, DATA_BLOCK data, pilots, ...,
    the last data block possibly short, then the closing pilots."""
    is_pilot, starts, pos = [], [], 0
    for blk in range(n_pilot_blocks):
        starts.append(pos)
        is_pilot += [True] * PILOT_LEN
        pos += PILOT_LEN
        if blk < n_pilot_blocks - 1:
            k = min(DATA_BLOCK, n_data - blk * DATA_BLOCK)
            is_pilot += [False] * k
            pos += k
    return np.flatnonzero(~np.array(is_pilot)), np.array(starts)


# ---------------------------------------------------------------- transmit
def build_frame(cfg, payload: bytes):
    """One fragment as a burst at cfg.fs, unit RMS, filter tails included."""
    import hf_ofdm_common as ofdm
    n = len(payload)
    if n >= 1 << 16:
        raise ValueError("S payload over 65535 bytes")
    data = payload + zlib.crc32(payload).to_bytes(4, "big")
    coded = ldpc.ldpc_encode(ofdm.bytes_to_bits(data))
    coded = ofdm.interleave(coded)
    coded = coded ^ ofdm._prbs_bits(len(coded))
    bps = cfg.bits_per_symbol
    if len(coded) % bps:
        coded = np.concatenate([coded, np.zeros(bps - len(coded) % bps, np.uint8)])
    v = coded.reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))
    data_sym = CONSTELLATIONS[cfg.data_modulation][v]
    n_data, n_pb, total = _layout(n, bps)
    data_pos, pilot_starts = _data_positions(n_data, n_pb)
    body = np.empty(n_data + n_pb * PILOT_LEN, np.complex64)
    body[data_pos] = data_sym
    for i, s in enumerate(pilot_starts):
        body[s:s + PILOT_LEN] = PILOTS[i * PILOT_LEN:(i + 1) * PILOT_LEN]
    hdr = np.repeat(_header_bits(n), HDR_REPEAT) ^ HDR_SCRAMBLE
    syms = np.concatenate([PREAMBLE, (1 - 2.0 * hdr).astype(np.complex64), body])
    up = np.zeros(len(syms) * SPS, np.complex64)
    up[::SPS] = syms
    w = np.convolve(up, RRC)[:len(up) + 2 * RRC_DELAY - (SPS - 1)]
    return (w / np.sqrt(np.mean(np.abs(w) ** 2))).astype(np.complex64)


def spectrum_cut(cfg):
    """(cut_hz, taper_hz) for the TX resampler: just past the RRC edge."""
    edge = cfg.symbol_rate * (1 + ALPHA) / 2
    return edge * 1.04, cfg.symbol_rate * 0.04


# ---------------------------------------------------------------- receive
def _mf(x):
    """RRC matched filter, delay removed (output index = input index)."""
    return np.convolve(x, RRC)[RRC_DELAY:RRC_DELAY + len(x)].astype(np.complex64)


def _xcorr_valid(sig, tmpl):
    """sum_k tmpl[k] * sig[n + k] for every n with the template inside sig."""
    n, m = len(sig), len(tmpl)
    if n < m:
        return np.zeros(0, np.complex128)
    nf = 1 << int(np.ceil(np.log2(n + m)))
    c = np.fft.ifft(np.fft.fft(sig, nf) * np.conj(np.fft.fft(np.conj(tmpl), nf)))
    return c[:n - m + 1]


_PD1 = (PREAMBLE[1:] * np.conj(PREAMBLE[:-1])).real
_PDL = (PREAMBLE[LAG_FINE:] * np.conj(PREAMBLE[:-LAG_FINE])).real


def _stride_template(vals, sps=SPS):
    t = np.zeros((len(vals) - 1) * sps + 1, np.complex128)
    t[::sps] = vals
    return t


_T1 = _stride_template(_PD1)


def detect(z):
    """First preamble in matched-filter samples z: (index of symbol 0's
    peak, metric, coarse CFO in cycles/symbol) or None."""
    if len(z) < PRE_LEN * SPS + SPS:
        return None
    d = z[SPS:] * np.conj(z[:-SPS])
    c = _xcorr_valid(d, _T1)
    # energy under the template: a running sum (|d| varies slowly next to
    # the symbol stride, so all samples / SPS ~ the strided ones)
    cs = np.concatenate([[0.0], np.cumsum(np.abs(d), dtype=np.float64)])
    span = len(_T1)
    e = (cs[span:span + len(c)] - cs[:len(c)]) / SPS
    m = np.abs(c) / np.maximum(e, 1e-12)
    over = np.flatnonzero(m > DETECT_THRESHOLD)
    if not len(over):
        return None
    i0 = over[0]
    win = slice(i0, min(len(m), i0 + PRE_LEN * SPS // 2))
    i = win.start + int(np.argmax(m[win]))
    return i, float(m[i]), float(np.angle(c[i]) / (2 * np.pi))


def _cubic(z, t):
    """z sampled at fractional positions t (4-point Lagrange)."""
    i = np.floor(t).astype(np.int64)
    mu = (t - i).astype(np.float32)
    i = np.clip(i, 1, len(z) - 3)
    zm1, z0, z1, z2 = z[i - 1], z[i], z[i + 1], z[i + 2]
    c0 = -mu * (mu - 1) * (mu - 2) / 6
    c1 = (mu + 1) * (mu - 1) * (mu - 2) / 2
    c2 = -(mu + 1) * mu * (mu - 2) / 2
    c3 = (mu + 1) * mu * (mu - 1) / 6
    return zm1 * c0 + z0 * c1 + z1 * c2 + z2 * c3


def _fine_timing(z, t_nominal, known):
    """Fractional sample offset that best matches `known` symbols expected at
    t_nominal (spaced SPS): coherent correlation at -2..+2 samples, parabola."""
    offs = np.arange(-2, 3)
    v = np.array([np.abs(np.vdot(known, _cubic(z, t_nominal + o))) for o in offs])
    k = int(np.argmax(v))
    if 0 < k < len(v) - 1:
        den = v[k - 1] - 2 * v[k] + v[k + 1]
        return offs[k] + (0.5 * (v[k - 1] - v[k + 1]) / den if den else 0.0)
    return float(offs[k])


def _derotated_mf(x, cfo):
    """x moved by -cfo (cycles/symbol), THEN matched-filtered: a large offset
    filtered first would push part of the signal out of the filter."""
    n = np.arange(len(x))
    return _mf((x * np.exp(-2j * np.pi * cfo * n / SPS)).astype(np.complex64))


def _sync(x, i, cfo1):
    """Preamble found near sample i of raw x: (t0, cfo in cycles/symbol,
    matched-filtered x on that frequency), t0 the fractional position of
    symbol 0."""
    zz = _derotated_mf(x, cfo1)
    t0 = i + _fine_timing(zz, i + np.arange(PRE_LEN) * SPS, PREAMBLE)
    # Refined in steps over longer and longer spans of the preamble, each
    # re-measured after the last correction: lag 16 (ambiguity +-1/32
    # cycles/symbol), then 64 and 128 (+-1/128, +-1/256), each well inside
    # the one before's error. Lag 16 alone left 20-50 Hz at 40 kHz when the
    # coarse estimate was ~150 Hz out -- ~50 deg over the header, which then
    # failed its checksum (1 frame in 40 at 3 dB, where all should decode).
    cfo = cfo1
    for lag in FINE_LAGS:
        s = _cubic(zz, t0 + np.arange(PRE_LEN) * SPS) * PREAMBLE      # ~constant phase if on frequency
        cfo += np.angle(np.vdot(s[:-lag], s[lag:])) / (2 * np.pi * lag)
        zz = _derotated_mf(x, cfo)
        t0 = i + _fine_timing(zz, i + np.arange(PRE_LEN) * SPS, PREAMBLE)
    return t0, cfo, zz


def _header(zz, t0, ref):
    """Decoded payload length, or None (checksum)."""
    t = t0 + (PRE_LEN + np.arange(HDR_BITS * HDR_REPEAT)) * SPS
    y = (_cubic(zz, t) * np.conj(ref)).real * (1 - 2.0 * HDR_SCRAMBLE)
    soft = y.reshape(HDR_BITS, HDR_REPEAT).sum(axis=1)
    bits = (soft < 0).astype(np.uint8)
    if _crc8(bits[:16]) != int(np.packbits(bits[16:])[0]):
        return None
    return int(np.packbits(bits[:16]).view(">u2")[0])


def _smooth(v, n):
    """Centred moving average over n points (shorter at the ends)."""
    if n <= 1 or len(v) < 2:
        return v
    k = np.ones(n)
    return np.convolve(v, k, mode="same") / np.convolve(np.ones(len(v)), k, mode="same")


def _slice(y, const):
    """Nearest constellation point to each of y."""
    return const[np.argmin(np.abs(y[:, None] - const[None, :]), axis=1)]


def _equalise(y, known, pref, const):
    """A short linear equaliser (EQ_PRE taps before the main path, EQ_POST
    after, symbol spaced), least-squares trained on this frame's pilots,
    then once more on all its symbols with the data decided. For a short
    echo, as an HF sky-wave path gives (measured through the IL38 KiwiSDR
    at 29 MHz: one echo 3 symbols late at 8 kBd, -4 to -12 dB, ruining the
    frames where it was strong). Lightly damped (EQ_RIDGE) so that on a
    clean path it stays a plain pass-through."""
    n = len(y)
    offs = np.arange(-EQ_PRE, EQ_POST + 1)
    pad = np.concatenate([np.zeros(EQ_POST, y.dtype), y, np.zeros(EQ_PRE, y.dtype)])
    # column j: y delayed by offs[j] (row k sees y[k - offs[j]])
    Y = np.stack([pad[EQ_POST - o:EQ_POST - o + n] for o in offs], axis=1)
    ridge = EQ_RIDGE * np.eye(len(offs))
    out = y
    for target_mask in (known, np.ones(n, bool)):
        if target_mask is known:
            t = pref[known]
        else:
            t = np.where(known, pref, _slice(out, const))
        A = Y[target_mask]
        w = np.linalg.solve(A.conj().T @ A + ridge * len(t), A.conj().T @ t)
        if target_mask is known:
            # only where there's an echo to take out: on a clean path its
            # taps just fit noise (measured: 0.3 dB lost at threshold in AWGN)
            # compared with each scaled by its own best gain: least squares
            # also shrinks its output, which alone 'gains' ~2 dB at 1.5 dB SNR
            def fit_err(v):
                g = np.vdot(v, t) / max(np.vdot(v, v).real, 1e-12)
                return np.mean(np.abs(g * v - t) ** 2)
            if fit_err(A @ w) > fit_err(y[known]) * 10 ** (-EQ_MIN_GAIN_DB / 10):
                return y
        out = (Y @ w).astype(np.complex64)
    return out


def _demod_data(zz, t0, ref, n_bytes, modulation="qpsk"):
    """(LLRs in transmitted coded-bit order, equalised data symbols, MER dB)."""
    const = CONSTELLATIONS[modulation]
    bps = BITS_PER_SYMBOL[modulation]
    n_data, n_pb, total = _layout(n_bytes, bps)
    data_pos, pilot_starts = _data_positions(n_data, n_pb)
    base = PRE_LEN + HDR_BITS * HDR_REPEAT
    # timing: each pilot block's own fine offset, a straight line through them
    # (the two radios' clocks differ by a steady ppm)
    centres, offs, phs = [], [], []
    for b, s in enumerate(pilot_starts):
        known = PILOTS[b * PILOT_LEN:(b + 1) * PILOT_LEN]
        tn = t0 + (base + s + np.arange(PILOT_LEN)) * SPS
        o = _fine_timing(zz, tn, known)
        centres.append(base + s + PILOT_LEN / 2)
        offs.append(o)
    centres = np.array(centres)
    offs = np.array(offs)
    if len(centres) >= 3:
        # robust line: refit without the worst outliers
        p = np.polyfit(centres, offs, 1)
        r = np.abs(offs - np.polyval(p, centres))
        keep = r <= max(0.25, np.percentile(r, 80))
        p = np.polyfit(centres[keep], offs[keep], 1)
    else:
        p = np.array([0.0, float(np.mean(offs))])
    n_body = n_data + n_pb * PILOT_LEN
    sym_idx = base + np.arange(n_body)
    y = _cubic(zz, t0 + sym_idx * SPS + np.polyval(p, sym_idx))
    # Phase: each pilot block's own estimate is noisy (8 symbols: ~8 deg at
    # 5 dB) while the radios' phase wanders only a few degrees over many
    # blocks (measured on air, Pluto -> converter -> Lime) -- following every
    # block cost 2.3 dB of MER. So: pilot phases smoothed over PHASE_SMOOTH
    # blocks, then refined from the data itself (decisions, windows of
    # DD_WINDOW symbols, smoothed again). Gain: one for the whole frame.
    g = np.array([np.vdot(PILOTS[b * PILOT_LEN:(b + 1) * PILOT_LEN], y[s:s + PILOT_LEN]) / PILOT_LEN
                  for b, s in enumerate(pilot_starts)])
    ph = _smooth(np.unwrap(np.angle(g)), PHASE_SMOOTH)
    pc = pilot_starts + PILOT_LEN / 2
    y = y * np.exp(-1j * np.interp(np.arange(n_body), pc, ph))
    known = np.zeros(n_body, bool)
    pref = np.zeros(n_body, np.complex64)
    for b, s in enumerate(pilot_starts):
        known[s:s + PILOT_LEN] = True
        pref[s:s + PILOT_LEN] = PILOTS[b * PILOT_LEN:(b + 1) * PILOT_LEN]
    # provisional scale and noise from the pilots (for the soft decisions)
    amp0 = max(np.real(np.vdot(pref[known], y[known])) / np.sum(known), 1e-9)
    nv0 = max(float(np.mean(np.abs(y[known] / amp0 - pref[known]) ** 2)), 1e-3)
    for _ in range(2):
        if SOFT_DD:
            # each symbol's expected value given the noise: a doubtful one
            # (between points) counts little, a clear one fully -- hard
            # decisions near threshold pulled the phase off (8PSK most)
            yn = y / amp0
            w = np.exp(-(np.abs(yn[:, None] - const[None, :]) ** 2 - 0.0) / nv0)
            soft = (w @ const) / np.maximum(w.sum(axis=1), 1e-30)
            dec = np.where(known, pref, soft)
        else:
            dec = np.where(known, pref, _slice(y, const))
        n_win = -(-n_body // DD_WINDOW)
        prod = np.zeros(n_win * DD_WINDOW, np.complex64)
        prod[:n_body] = y * np.conj(dec)
        e = prod.reshape(n_win, DD_WINDOW).sum(axis=1)
        dph = _smooth(np.unwrap(np.angle(e)), DD_SMOOTH)
        centres = np.arange(n_win) * DD_WINDOW + DD_WINDOW / 2
        y = y * np.exp(-1j * np.interp(np.arange(n_body), centres, dph))
    amp = np.real(np.vdot(pref[known], y[known])) / np.sum(known)
    y = y / max(amp, 1e-9)
    if EQUALISE:
        y = _equalise(y, known, pref, const)
    d = y[data_pos]
    # noise from the pilots (known) and the data (decided)
    pil = np.concatenate([y[s:s + PILOT_LEN] - PILOTS[b * PILOT_LEN:(b + 1) * PILOT_LEN]
                          for b, s in enumerate(pilot_starts)])
    hard = _slice(d, const)
    nv = float(np.mean(np.concatenate([np.abs(pil) ** 2, np.abs(d - hard) ** 2])))
    nv = max(nv, 1e-4)
    # The readout: from the pilots alone (known symbols, ~500 a frame). The
    # decided data errors above (fine for the soft bits) read high near the
    # threshold -- a wrong decision looks like a small error -- so S showed
    # ~2 dB more than SH at the same SNR (SH measures its preamble the same way)
    mer = -10 * np.log10(max(float(np.mean(np.abs(pil) ** 2)), 1e-4))
    # soft bits: exact (log of the summed likelihoods of every point with the
    # bit 0, less with it 1) or max-log (nearest point each way)
    dist = np.abs(d[:, None] - const[None, :]) ** 2
    vals = np.arange(len(const))
    llr = np.empty((len(d), bps), np.float32)
    m = -dist / nv
    for i in range(bps):
        one = ((vals >> (bps - 1 - i)) & 1).astype(bool)
        if EXACT_LLR:
            llr[:, i] = np.logaddexp.reduce(m[:, ~one], axis=1) - np.logaddexp.reduce(m[:, one], axis=1)
        else:
            llr[:, i] = (dist[:, one].min(axis=1) - dist[:, ~one].min(axis=1)) / nv
    return np.clip(llr.reshape(-1), -LLR_CLIP, LLR_CLIP), d, mer


def decode(zz, t0, ref, n_bytes, modulation="qpsk"):
    """(payload or None, crc_ok, ber, evm %, constellation)."""
    import hf_ofdm_common as ofdm
    llr, d, mer = _demod_data(zz, t0, ref, n_bytes, modulation)
    n_coded = _coded_bits(n_bytes)
    llr = llr[:n_coded] * (1.0 - 2.0 * ofdm._prbs_bits(n_coded))     # undo the whitening
    llr = ofdm.deinterleave(llr)
    n_bits = (n_bytes + 4) * 8
    bits, cw = ldpc.ldpc_decode_soft_codeword(llr, n_bits)
    data = ofdm.bits_to_bytes(bits)
    payload, crc = data[:n_bytes], data[n_bytes:n_bytes + 4]
    crc_ok = zlib.crc32(payload).to_bytes(4, "big") == crc
    if cw is None:
        cw = ldpc.ldpc_encode(bits)
    ber = float(np.mean((llr < 0) != cw[:n_coded].astype(bool)))
    evm = 100 * 10 ** (-mer / 20)
    return payload, crc_ok, ber, evm, d


def receive_one_fragment(cfg, args, reader, state, get_samples):
    """hf_ofdm_rx's receive_one_fragment for S: waits for samples, finds
    the next burst at or after state["native_search_start"], decodes it.
    get_samples() -> everything received so far at cfg.fs (indexable)."""
    t_start = time.time()
    fs = cfg.fs
    chunk = int(SEARCH_CHUNK_S * fs)
    margin = 2 * RRC_DELAY + 8 * SPS
    while True:
        rx = get_samples()
        start = state["native_search_start"]
        avail = len(rx) - start
        need = PRE_LEN * SPS + margin
        if avail < need:
            if reader.eof:
                return {"status": "eof"}
            reader.wait_for_more(reader.total_len, timeout=0.5)
            continue
        # where the last burst ended the next usually starts: look there first
        quick = state.pop("sc_expect_next", False) and avail >= 3 * PRE_LEN * SPS + margin
        seg_end = start + (3 * PRE_LEN * SPS + margin if quick else min(avail, chunk))
        seg = np.asarray(rx[start:seg_end], np.complex64)
        z = _mf(seg)
        hit = detect(z[:len(z) - margin])
        if hit is None and quick:
            continue                      # not where expected: the full search
        if hit is None:
            if avail < chunk and not reader.eof:
                reader.wait_for_more(reader.total_len, timeout=0.5)
                # rescan only what's new (keep a preamble's worth of overlap)
                state["native_search_start"] = max(start, seg_end - PRE_LEN * SPS - margin)
                continue
            state["native_search_start"] = max(start + 1, seg_end - PRE_LEN * SPS - margin)
            if reader.eof and seg_end >= len(rx):
                return {"status": "eof"}
            continue
        i, metric, cfo1 = hit
        frame_start = start + i - RRC_DELAY
        # the header first: its length says how much more to wait for
        hdr_end = start + i + (PRE_LEN + HDR_BITS * HDR_REPEAT) * SPS + margin
        while len(get_samples()) < hdr_end and not reader.eof:
            reader.wait_for_more(reader.total_len, timeout=0.5)
        rx = get_samples()
        lo = max(start + i - margin, getattr(rx, "_origin", 0), 0)
        lead = start + i - lo           # where the preamble starts in seg
        seg = np.asarray(rx[lo:min(len(rx), hdr_end + margin)], np.complex64)
        t0, cfo, zz = _sync(seg, lead, cfo1)
        ref = np.vdot(PREAMBLE, _cubic(zz, t0 + np.arange(PRE_LEN) * SPS)) / PRE_LEN
        ref = ref / max(abs(ref), 1e-12)
        n_bytes = _header(zz, t0, ref)
        if n_bytes is None or (args.fragment_size and n_bytes > 2 * (args.fragment_size + 6)):
            return {"status": "failed", "next_native_search_start": start + i + PRE_LEN * SPS // 2,
                    "retry_count": 0}
        end = frame_start + frame_samples(cfg, n_bytes) + margin
        while len(get_samples()) < end and not reader.eof:
            reader.wait_for_more(reader.total_len, timeout=0.5)
        rx = get_samples()
        # (at the very end of a stream the burst's last filter tail -- no
        # symbols in it -- may be cut by the front end's own filter delay)
        if len(rx) < end - margin - 2 * RRC_DELAY:
            return {"status": "incomplete"}
        seg = np.asarray(rx[lo:min(end, len(rx))], np.complex64)
        if len(seg) < end - lo:
            # the stream ended right after the burst: all of it is here, only
            # the look-ahead margin is missing -- silence will do
            seg = np.concatenate([seg, np.zeros(end - lo - len(seg), np.complex64)])
        zz = _derotated_mf(seg, cfo)
        payload, crc_ok, ber, evm, d = decode(zz, t0, ref, n_bytes, cfg.data_modulation)
        state["sc_expect_next"] = True
        return {"status": "ok", "payload": payload, "crc_ok": crc_ok, "cfo_hz": cfo * cfg.symbol_rate,
                "ber": ber, "evm": evm, "constellation": d, "frame_start_index": frame_start,
                "next_native_search_start": frame_start + frame_samples(cfg, n_bytes) - 2 * SPS,
                "retry_count": 0, "decode_s": time.time() - t_start, "metric": metric}
