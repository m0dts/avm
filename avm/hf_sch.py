"""SH: single carrier for HF -- SC-FDE (single carrier, frequency-domain
equalisation) at 8 kBd in 10 kHz, for 8-10 kbit/s on sky-wave paths with
echoes up to 8 ms. Mode S's pulse shape, LDPC and constellations (hf_sc);
its own frame and receiver, built for multipath (tools/schf_proto.py has
the simulations: against mode A 16QAM, about 4 dB less SNR in AWGN and on
good/moderate HF channels at 3/4 of its rate, and ~4.5 dB peak to average
against 8.5-12).

    preamble   PERIOD-symbol sequence x SHORT_REPEATS, then the UW x PRE_UWS
    header     one block: the payload length (24 bits, each x8, BPSK), UW
    data       blocks of D_LEN symbols (LDPC 1/2, QPSK or 8PSK), each + UW

The UW (64 known symbols, 8 ms) ends every block, so each block's FFT
window [data, UW] is cyclic -- the UW before it is its cyclic prefix -- and
one 256-point MMSE equaliser per block takes out echoes up to ~8 ms.
Receiver: detection and frequency from the periodic preamble parts (echoes
leave a periodic signal periodic: no bias), timing from the first UW, the
first channel from the repeated UWs; then per block: equalise, decide,
re-estimate the channel, smooth the estimates across neighbouring blocks,
and on a CRC failure re-estimate from the decoder's own output (TURBO)."""
import time
import zlib

import numpy as np

import hf_ofdm_ldpc as ldpc
import hf_sc as sc

MODE = "SH"
WIDTHS_KHZ = (10, 20, 40)
SPS = sc.SPS
TRACK = 0.7            # pass 1: weight of each block's own channel estimate
SMOOTH_W = (1, 2, 3, 2, 1)   # pass 2: neighbour blocks' estimates, both sides
TURBO = 2              # re-estimates from the decoder's output on a CRC fail
DETECT_THRESHOLD = 0.35  # preamble correlation coefficient to count as found
UW_MATCH = 0.25         # a candidate's UWs must match this well (normalised; noise ~0.07)
RUN_END_FRACTION = 0.75 # ... and the preamble ends where it falls below this x its peak
TAP_THRESHOLD = 9.0    # keep channel taps above this x their noise (power; 3 sigma)
HDR_BITS = 24          # 16-bit length + 8-bit checksum, repeated to fill one block
SEARCH_CHUNK_S = 2.0
PERIOD, SHORT_REPEATS, PRE_UWS = 16, 8, 3
SHORT_LEN = PERIOD * SHORT_REPEATS
# Sets of known sequences, cycled fragment to fragment (each fragment's own
# checksum picks one; the receiver tries them all while it syncs). Within a
# fragment the UW can't change -- each is the next block's cyclic prefix --
# but one UW for every fragment repeated every 32 ms forever: a fixed comb
# of lines 31.25 Hz apart on the spectrum.
N_VARIANTS = 8
_KIND = None


def _geometry(k):
    """Block sizes for a width k x 10 kHz (k x 8 kBd): everything in symbols
    scales with k, so the UW -- the echo span the equaliser takes -- stays
    8 ms at every width. Sets the module's globals (one width at a time)."""
    global _KIND, N, UW_LEN, D_LEN, L_TAPS, PRE_TAPS, HDR_REPEAT, HDR_SCRAMBLE, PRE_HF_LEN, _UWS, _SHORTS
    if _KIND == k:
        return
    _KIND = k
    N, UW_LEN = 256 * k, 64 * k
    D_LEN = N - UW_LEN
    L_TAPS = UW_LEN                   # channel taps kept (the UW's length, 8 ms)
    PRE_TAPS = 24 * k                 # of them before the path the timing locks to
    HDR_REPEAT = D_LEN // HDR_BITS
    HDR_SCRAMBLE = np.random.default_rng(777 + k).integers(0, 2, HDR_BITS * HDR_REPEAT).astype(np.uint8)
    PRE_HF_LEN = SHORT_LEN + PRE_UWS * UW_LEN
    rng = np.random.default_rng(424242 + k)
    # UWs: flat spectrum AND near-constant amplitude, built by alternating
    # the two projections from a random start. The channel estimate divides
    # by the UW's spectrum: a random QPSK UW with a near-zero bin failed the
    # same two fragments at 15 dB. (Zadoff-Chu sequences are flat too, but to
    # them a frequency offset looks like a time shift: the sync then chose
    # wrong frequency steps; these, from random phases, don't.)
    _UWS = []
    for _ in range(N_VARIANTS):
        u = np.exp(2j * np.pi * rng.random(UW_LEN))
        for _ in range(300):
            F = np.fft.fft(u)
            u = np.fft.ifft(F / np.maximum(np.abs(F), 1e-12))     # flat spectrum
            u = u / np.maximum(np.abs(u), 1e-12)                    # constant amplitude
        _UWS.append(u.astype(np.complex64))
    _SHORTS = [sc._gray_psk(4)[rng.integers(0, 4, PERIOD)].astype(np.complex64) for _ in range(N_VARIANTS)]
    _use(0)


def _use(v):
    """Make variant v's sequences the current ones (UW, SHORT_SEQ, PRE_HF)."""
    global UW, SHORT_SEQ, PRE_HF, VARIANT
    VARIANT = v
    UW, SHORT_SEQ = _UWS[v], _SHORTS[v]
    PRE_HF = np.concatenate([np.tile(SHORT_SEQ, SHORT_REPEATS), np.tile(UW, PRE_UWS)]).astype(np.complex64)


def _set(cfg):
    _geometry(int(round(cfg.occupancy_hz / 10000)))


_geometry(1)


class SHConfig:
    """As hf_sc.SCConfig: what the rest of AVM reads from a mode config."""
    is_sc = False
    sc_module = "hf_sch"

    def __init__(self, occupancy_khz, data_modulation="8psk", fec_scheme="ldpc"):
        if int(round(occupancy_khz)) not in WIDTHS_KHZ:
            raise ValueError(f"mode SH is {', '.join(map(str, WIDTHS_KHZ))} kHz wide (got {occupancy_khz:g})")
        if data_modulation not in sc.CONSTELLATIONS:
            raise ValueError(f"mode SH has {', '.join(sc.CONSTELLATIONS)} (got {data_modulation})")
        self.mode = MODE
        self.occupancy_hz = float(occupancy_khz) * 1000
        self.symbol_rate = self.occupancy_hz / (1 + sc.ALPHA)
        self.fs = SPS * self.symbol_rate
        self.data_modulation = data_modulation
        self.bits_per_symbol = sc.BITS_PER_SYMBOL[data_modulation]
        self.const = sc.CONSTELLATIONS[data_modulation]
        self.fec_scheme = "ldpc"
        self.symbol_len = SPS
        k = int(round(occupancy_khz / 10))
        self.preamble_len = (SHORT_LEN + PRE_UWS * 64 * k) * SPS
        self.n_cp = SPS
        self.n_fft = 128
        self.carrier_spacing = self.symbol_rate / 62
        k = int(round(self.occupancy_hz / 2 / self.carrier_spacing - 0.5))
        self.data_bins = np.arange(-k, k + 1)
        self.fft_bins = self.data_bins % self.n_fft

    def __repr__(self):
        return f"SHConfig({self.occupancy_hz / 1e3:g} kHz, {self.symbol_rate:.0f} Bd {self.data_modulation})"


# ---------------------------------------------------------------- layout
def _n_blocks(n_bytes, bps):
    return -(-ldpc.ldpc_coded_len((n_bytes + 4) * 8) // (bps * D_LEN))


def frame_samples(cfg, n_bytes):
    _set(cfg)
    n_sym = PRE_HF_LEN + N * (1 + _n_blocks(n_bytes, cfg.bits_per_symbol))
    return n_sym * SPS + 2 * sc.RRC_DELAY - (SPS - 1)


def frame_symbol_count(cfg, n_bytes):
    return -(-(frame_samples(cfg, n_bytes) - cfg.preamble_len) // SPS)


def payload_bitrate(cfg):
    _set(cfg)
    return cfg.symbol_rate * cfg.bits_per_symbol * 0.5 * D_LEN / N


def spectrum_cut(cfg):
    return sc.spectrum_cut(cfg)


# ---------------------------------------------------------------- transmit
def _header_symbols(n_bytes):
    bits = sc._header_bits(n_bytes)
    return (1 - 2.0 * (np.repeat(bits, HDR_REPEAT) ^ HDR_SCRAMBLE)).astype(np.complex64)


def _data_blocks(coded, cfg, n_blocks):
    """Coded (interleaved, whitened) bits -> the data blocks [data, UW]."""
    bps = cfg.bits_per_symbol
    coded = np.concatenate([coded, np.zeros(max(0, n_blocks * D_LEN * bps - len(coded)), np.uint8)])
    v = coded[:n_blocks * D_LEN * bps].reshape(-1, bps) @ (1 << np.arange(bps - 1, -1, -1))
    return [np.concatenate([d, UW]) for d in cfg.const[v].reshape(-1, D_LEN)]


def _coded(payload):
    import hf_ofdm_common as ofdm
    data = payload + zlib.crc32(payload).to_bytes(4, "big")
    cw = ldpc.ldpc_encode(ofdm.bytes_to_bits(data))
    return ofdm.interleave(cw) ^ ofdm._prbs_bits(len(cw))


def build_frame(cfg, payload: bytes):
    n = len(payload)
    if n >= 1 << 16:
        raise ValueError("SH payload over 65535 bytes")
    _set(cfg)
    _use(zlib.crc32(payload) % N_VARIANTS)
    blocks = _data_blocks(_coded(payload), cfg, _n_blocks(n, cfg.bits_per_symbol))
    syms = np.concatenate([PRE_HF, _header_symbols(n), UW] + blocks).astype(np.complex64)
    up = np.zeros(len(syms) * SPS, np.complex64)
    up[::SPS] = syms
    w = np.convolve(up, sc.RRC)[:len(up) + 2 * sc.RRC_DELAY - (SPS - 1)]
    return (w / np.sqrt(np.mean(np.abs(w) ** 2))).astype(np.complex64)


# ---------------------------------------------------------------- receive
def _taps_to_H(h, pre):
    g = np.zeros(N, complex)
    g[:len(h) - pre] = h[pre:]
    g[N - pre:] = h[:pre]
    return np.fft.fft(g)


def _xc(z, seq, lo, hi, first_of=0.6):
    """Start (fractional) of seq in z within lo..hi: the EARLIEST correlation
    peak reaching first_of x the strongest (the UWs repeat)."""
    t = np.zeros((len(seq) - 1) * SPS + 1, complex)
    t[::SPS] = np.conj(seq)
    c = np.abs(sc._xcorr_valid(z[lo:hi + len(t)], t))
    k0 = int(np.flatnonzero(c >= first_of * c.max())[0])
    k = k0 + int(np.argmax(c[k0:k0 + 2 * SPS + 1]))
    den = (c[k - 1] - 2 * c[k] + c[k + 1]) if 0 < k < len(c) - 1 else 0.0
    return lo + k + (0.5 * (c[k - 1] - c[k + 1]) / den if den else 0.0)


MAX_CFO_HZ = 4000.0      # frequency error searched for (TX, RX and any converter together)


def _detect(x, cfg_rate=None):
    """First preamble in raw x: (t0 = fractional sample of its symbol 0,
    cfo in cycles/symbol), "short" if it may run past the end of x, or None.
    Frequency: lag PERIOD on the periodic part (unbiased by echoes,
    +-1/(2 PERIOD) cycle), its 1/PERIOD step chosen by which one makes the
    known UW correlate coherently, then lag UW_LEN over the repeated UWs."""
    S = SPS
    z = sc._mf(x)
    lag = PERIOD * S
    win = (SHORT_REPEATS - 1) * lag
    if len(z) < win + lag + 10:
        return None
    prod = z[:-lag] * np.conj(z[lag:])
    cs = np.concatenate([[0], np.cumsum(prod)])
    ce = np.concatenate([[0], np.cumsum(np.abs(z) ** 2)])
    n = len(prod) - win
    P = cs[win:win + n] - cs[:n]
    E1 = ce[win:win + n] - ce[:n]
    E2 = ce[lag + win:lag + win + n] - ce[lag:lag + n]
    m = np.abs(P) / np.sqrt(np.maximum(E1 * E2, 1e-30))     # level-free: a preamble in a fade still scores
    # a real preamble scores ~SNR/(1+SNR): 0.7 missed nearly every burst on air at
    # ~2 dB (3 of 237 found); noise alone ~0.05 over this window. Anything
    # steady -- a tone, a spur in the gaps between frames -- scores like a
    # preamble too (on air: a hump ~45 ms before each real one): so each run
    # is only a candidate, accepted when its UWs are really there (_check).
    pos = 0
    while True:
        over = np.flatnonzero(m[pos:] > DETECT_THRESHOLD)
        if not len(over):
            return None
        i0 = pos + int(over[0])
        # high while the window slides onto the preamble, low once it reaches
        # the UWs: the start is one lag before the END of the high run. "Low" is
        # relative to the run's own peak: an echo exactly PERIOD symbols late
        # makes ANY signal match itself PERIOD symbols on (up to 0.5 for two
        # equal paths), so the run never ended and the start landed thousands
        # of symbols into the data (20 kHz with a 1 ms echo: 3 of 10 found)
        peak = float(np.max(m[i0:i0 + 2 * SHORT_LEN * S]))
        high = m[i0:] > max(DETECT_THRESHOLD, RUN_END_FRACTION * peak)
        j0 = int(np.argmax(high))                 # the run starts where it first gets that high
        if high[j0:].all():
            return "short"
        run_end = i0 + j0 + int(np.argmin(high[j0:]))
        i = max(i0, run_end - lag)
        got = _check(x, P, i, cfg_rate)
        if got is not None:
            return got
        pos = run_end + lag                       # not a preamble: on past this run


def _check(x, P, i, cfg_rate):
    """A candidate preamble starting near sample i: (t0, cfo) if its UWs are
    there (coherent match over all of them >= UW_MATCH), "short" if x ends
    too soon, else None."""
    S = SPS
    exp = i + SHORT_LEN * S
    lo, hi = max(0, exp - 4 * PRE_TAPS * S), exp + 2 * PRE_TAPS * S   # (< one UW ahead)
    if hi + (PRE_UWS * UW_LEN + 64) * S >= len(x):
        return "short"
    c16 = -np.angle(P[i]) / (2 * np.pi * PERIOD)
    seg = x[max(0, lo - 64):hi + PRE_UWS * UW_LEN * S + 64]
    off = lo - max(0, lo - 64)
    best = (-1.0, 0.0, 0)
    # the 1/PERIOD steps covering +-MAX_CFO_HZ: all 16 at 8 kBd (500 Hz each),
    # +-2 at 32 kBd (2 kHz each) -- each step costs N_VARIANTS correlations
    reach = min(PERIOD // 2, int(np.ceil(MAX_CFO_HZ * PERIOD / cfg_rate))) if cfg_rate else PERIOD // 2
    for step in range(-reach + (1 if reach == PERIOD // 2 else 0), reach + 1):
        cand = c16 + step / PERIOD
        zc = sc._derotated_mf(seg, cand)[off:]
        for v in range(N_VARIANTS):            # which UW this fragment uses, too: against
            seq = np.tile(_UWS[v], PRE_UWS)     # all the preamble's UWs (one alone, at
            t = np.zeros((len(seq) - 1) * S + 1, complex)   # ~1 dB, often chose wrongly)
            t[::S] = np.conj(seq)
            c = np.abs(sc._xcorr_valid(zc[:(hi - lo) + len(t)], t))
            if c.max() > best[0]:
                best = (float(c.max()), cand, v)
    cfo = best[1]
    _use(best[2])
    zz = sc._derotated_mf(x[:hi + (PRE_UWS * UW_LEN + 64) * S], cfo)
    t0 = _xc(zz, UW, lo, hi) - SHORT_LEN * S
    u = [sc._cubic(zz, t0 + (SHORT_LEN + j * UW_LEN + np.arange(UW_LEN)) * S) for j in range(PRE_UWS)]
    fine = np.angle(sum(np.vdot(u[j], u[j + 1]) for j in range(PRE_UWS - 1))) / (2 * np.pi * UW_LEN)
    # that's only +-1/(2 UW_LEN): at 40 kHz (UW 256) +-62 Hz, finer than the
    # step search lands -- so which multiple of 1/UW_LEN, chosen as before by
    # the most coherent match of the preamble's UWs
    seq = np.tile(UW, PRE_UWS)
    k = np.arange(len(seq))
    rx = sc._cubic(zz, t0 + (SHORT_LEN + k) * S)
    score = lambda j: abs(np.vdot(seq, rx * np.exp(-2j * np.pi * (fine + j / UW_LEN) * k)))
    j = max(range(-2, 3), key=score)
    if score(j) < UW_MATCH * np.linalg.norm(seq) * np.linalg.norm(rx):
        return None
    return t0, cfo + fine + j / UW_LEN


def _denoise(H, var):
    """Keep only the channel taps standing above their own noise (power var
    per tap): on a one-path channel the 63 others are pure estimation noise,
    which the equaliser applied to every block (measured: SH QPSK 2.5 dB
    behind mode S in plain noise). The strongest tap always stays."""
    g = np.fft.ifft(H)
    p = np.abs(g) ** 2
    keep = p > TAP_THRESHOLD * var
    keep[int(np.argmax(p))] = True
    return np.fft.fft(np.where(keep, g, 0))


def _block_channel(yw, block):
    """Least-squares channel (L_TAPS taps) of one block from its cyclic window
    and its symbols [data, UW]. The normal equations straight from FFTs: the
    columns are cyclic shifts of one sequence, so M^H M is its circular
    autocorrelation and M^H y a cross-correlation -- the same answer as a
    full lstsq, but an L x L solve (the 40 kHz blocks: 1024 x 256 lstsq was
    too slow for the Pi)."""
    c = np.roll(block, PRE_TAPS)
    Sf = np.fft.fft(c)
    a = np.fft.ifft(np.conj(Sf) * Sf)                     # a[m] = sum conj(c[k]) c[k+m]
    bcorr = np.fft.ifft(np.conj(Sf) * np.fft.fft(yw))     # sum conj(c[k]) y[k+m]
    d = np.arange(L_TAPS) - PRE_TAPS                      # each tap's delay
    b = bcorr[d % N]
    try:
        # Hermitian Toeplitz (R[i, j] = a[i - j]): Levinson, O(L^2) -- the
        # general solve was most of a 40 kHz frame's decode time on the Pi
        from scipy.linalg import solve_toeplitz
        hb = solve_toeplitz((a[:L_TAPS], a[(-np.arange(L_TAPS)) % N]), b)
    except Exception:
        R = a[(d[:, None] - d[None, :]) % N]
        hb = np.linalg.solve(R + 1e-9 * np.eye(L_TAPS), b)
    return _taps_to_H(hb, PRE_TAPS)


def _mmse(Y, H, nv):
    G = np.conj(H) / (np.abs(H) ** 2 + nv)
    mu = np.mean((np.abs(H) ** 2) / (np.abs(H) ** 2 + nv))     # MMSE bias
    xh = np.roll(np.fft.ifft(Y * G) / max(mu, 1e-6), -PRE_TAPS)
    return xh * np.exp(-1j * np.angle(np.vdot(UW, xh[D_LEN:])))


def _smooth_blocks(Hs):
    n, k = len(Hs), len(SMOOTH_W) // 2
    out = []
    for b in range(n):
        acc, wsum = 0, 0.0
        for j, wt in enumerate(SMOOTH_W):
            bb = b + j - k
            if 0 <= bb < n:
                acc = acc + wt * Hs[bb]
                wsum += wt
        out.append(acc / wsum)
    return out


def _llrs(xh, nve, cfg):
    vals = np.arange(len(cfg.const))
    d = xh[:D_LEN]
    m = -np.abs(d[:, None] - cfg.const[None, :]) ** 2 / nve
    ll = np.empty((D_LEN, cfg.bits_per_symbol), np.float32)
    for k in range(cfg.bits_per_symbol):
        one = ((vals >> (cfg.bits_per_symbol - 1 - k)) & 1).astype(bool)
        ll[:, k] = np.logaddexp.reduce(m[:, ~one], axis=1) - np.logaddexp.reduce(m[:, one], axis=1)
    return ll.reshape(-1)


def _grid(x, t0, cfo, n_sym):
    """Symbol-rate samples of x from PRE_TAPS before symbol 0 to n_sym + L_TAPS."""
    zz = sc._derotated_mf(x, cfo)
    return sc._cubic(zz, t0 + np.arange(-PRE_TAPS, n_sym + L_TAPS) * SPS)[PRE_TAPS:]


def _first_channel(r):
    """(H, noise) from the preamble's last two UWs (cyclic: a UW before each)."""
    ys = [r[SHORT_LEN + j * UW_LEN - PRE_TAPS:SHORT_LEN + (j + 1) * UW_LEN - PRE_TAPS]
          for j in (PRE_UWS - 2, PRE_UWS - 1)]
    # the window is the UW (shifted PRE_TAPS) circularly convolved with the
    # channel: a circulant system, solved exactly by dividing spectra (was a
    # UW_LEN x UW_LEN solve built from as many shifted copies)
    g = np.fft.ifft(np.fft.fft((ys[0] + ys[1]) / 2) / np.fft.fft(np.roll(UW, PRE_TAPS)))
    h = g[(np.arange(UW_LEN) - PRE_TAPS) % UW_LEN]
    p = float(np.mean(np.abs(ys[0]) ** 2 + np.abs(ys[1]) ** 2)) / 2 + 1e-30
    # sent twice: the difference is noise (floors relative to the level: the
    # front end's output can be far below 1, an absolute floor read -50 dB)
    nv = max(float(np.mean(np.abs(ys[0] - ys[1]) ** 2)) / 2, 1e-4 * p)
    # The SNR for the readout, from these two copies alone: the difference is
    # noise, the average signal (+ half the noise). The receiver's own error
    # after equalising (the old readout) depends on its decisions: below the
    # threshold it collapsed (MER -1 dB 1 dB under one that read 5.7)
    global PRE_SNR_DB
    sig = max(float(np.mean(np.abs((ys[0] + ys[1]) / 2) ** 2)) - nv / 2, 1e-5 * p)
    PRE_SNR_DB = 10 * np.log10(sig / nv)
    return _denoise(_taps_to_H(h[:L_TAPS], PRE_TAPS), nv / (2 * UW_LEN)), nv


def _window(r, b):
    """Block b's cyclic FFT window (b = 0: the header block)."""
    s = PRE_HF_LEN + b * N - PRE_TAPS
    return r[s:s + N]


def _header(r):
    """(payload length or None, H after the header block, noise)."""
    H, nv = _first_channel(r)
    yw = _window(r, 0)
    xh = _mmse(np.fft.fft(yw), H, nv)
    soft = (xh[:D_LEN].real * (1 - 2.0 * HDR_SCRAMBLE)).reshape(HDR_BITS, HDR_REPEAT).sum(axis=1)
    bits = (soft < 0).astype(np.uint8)
    if sc._crc8(bits[:16]) != int(np.packbits(bits[16:])[0]):
        return None, H, nv
    n = int(np.packbits(bits[:16]).view(">u2")[0])
    # the header block is known now: its own channel estimate
    Hh = _block_channel(yw, np.concatenate([_header_symbols(n), UW]))
    return n, (1 - TRACK) * H + TRACK * Hh, nv


def _decode(r, cfg, n_bytes, H, nv):
    """(payload, crc_ok, ber, evm %, constellation) for a frame whose header
    gave n_bytes."""
    import hf_ofdm_common as ofdm
    n_blocks = _n_blocks(n_bytes, cfg.bits_per_symbol)
    windows = [_window(r, 1 + b) for b in range(n_blocks)]
    n_coded = ldpc.ldpc_coded_len((n_bytes + 4) * 8)
    known_blocks = None
    for turbo in range(1 + TURBO):
        Hs = []
        if known_blocks is None:
            Hc = H
            for yw in windows:                # pass 1: track on own decisions
                xh = _mmse(np.fft.fft(yw), Hc, nv)
                dec = np.concatenate([sc._slice(xh[:D_LEN].astype(np.complex64), cfg.const), UW])
                Hb = _block_channel(yw, dec)
                Hc = (1 - TRACK) * Hc + TRACK * _denoise(Hb, nv / N)
                Hs.append(Hb)
        else:                                 # the decoder's output as the data
            Hs = [_block_channel(windows[b], known_blocks[b]) for b in range(n_blocks)]
        Hs = _smooth_blocks(Hs)
        w = np.asarray(SMOOTH_W, float)
        Hs = [_denoise(Hb, nv / N * np.sum(w ** 2) / np.sum(w) ** 2) for Hb in Hs]
        llrs, errs, syms = [], [], []
        for yw, Hb in zip(windows, Hs):
            xh = _mmse(np.fft.fft(yw), Hb, nv)
            nve = max(float(np.mean(np.abs(xh[D_LEN:] - UW) ** 2)), 1e-4)
            llrs.append(_llrs(xh, nve, cfg))
            errs.append(nve)
            syms.append(xh[:D_LEN])
        llr = np.clip(np.concatenate(llrs), -40, 40)[:n_coded] * (1.0 - 2.0 * ofdm._prbs_bits(n_coded))
        llr = ofdm.deinterleave(llr)
        bits, cw = ldpc.ldpc_decode_soft_codeword(llr, (n_bytes + 4) * 8)
        data = ofdm.bits_to_bytes(bits)
        payload = data[:n_bytes]
        crc_ok = zlib.crc32(payload).to_bytes(4, "big") == data[n_bytes:n_bytes + 4]
        if cw is None:
            cw = ldpc.ldpc_encode(bits)
        if crc_ok or turbo == TURBO:
            break
        known_blocks = _data_blocks(ofdm.interleave(cw.astype(np.uint8)) ^ ofdm._prbs_bits(len(cw)), cfg, n_blocks)
    ber = float(np.mean((llr < 0) != cw[:n_coded].astype(bool)))
    evm = 100 * np.sqrt(float(np.mean(errs)))
    return payload, crc_ok, ber, evm, np.concatenate(syms).astype(np.complex64)


def receive_one_fragment(cfg, args, reader, state, get_samples):
    """hf_ofdm_rx's receive_one_fragment for SH (as hf_sc's for S)."""
    t_start = time.time()
    _set(cfg)
    chunk = int(SEARCH_CHUNK_S * cfg.fs)
    keep = (PRE_HF_LEN + N + 64) * SPS          # overlap kept between search chunks
    margin = 2 * sc.RRC_DELAY + 8 * SPS
    while True:
        rx = get_samples()
        start = state["native_search_start"]
        avail = len(rx) - start
        if avail < keep + margin:
            if reader.eof:
                return {"status": "eof"}
            reader.wait_for_more(reader.total_len, timeout=0.5)
            continue
        seg_end = start + min(avail, chunk)
        seg = np.asarray(rx[start:seg_end], np.complex64)
        hit = _detect(seg, cfg.symbol_rate)
        if hit == "short" or hit is None:
            if hit == "short" and avail < chunk and not reader.eof:
                reader.wait_for_more(reader.total_len, timeout=0.5)
                continue
            if avail < chunk and not reader.eof:
                reader.wait_for_more(reader.total_len, timeout=0.5)
                state["native_search_start"] = max(start, seg_end - keep - margin)
                continue
            state["native_search_start"] = max(start + 1, seg_end - keep - margin)
            if reader.eof and seg_end >= len(rx):
                return {"status": "eof"}
            continue
        t0, cfo = hit
        lo = max(start + int(t0) - margin, getattr(rx, "_origin", 0), 0)
        t0 = start + t0 - lo
        # the header first: its length says how far the frame runs
        hdr_end = lo + int(t0) + (PRE_HF_LEN + N + L_TAPS + 8) * SPS + margin
        while len(get_samples()) < hdr_end and not reader.eof:
            reader.wait_for_more(reader.total_len, timeout=0.5)
        rx = get_samples()
        seg = np.asarray(rx[lo:min(len(rx), hdr_end)], np.complex64)
        r = _grid(seg, t0, cfo, PRE_HF_LEN + N)
        n_bytes, H, nv = _header(r)
        if n_bytes is None or (args.fragment_size and n_bytes > 2 * (args.fragment_size + 6)):
            return {"status": "failed", "next_native_search_start": lo + int(t0) + PRE_HF_LEN * SPS // 2,
                    "retry_count": 0}
        frame_start = lo + t0 - sc.RRC_DELAY
        end = int(frame_start + frame_samples(cfg, n_bytes) + L_TAPS * SPS + margin)
        while len(get_samples()) < end and not reader.eof:
            reader.wait_for_more(reader.total_len, timeout=0.5)
        rx = get_samples()
        if len(rx) < end - margin - L_TAPS * SPS - 2 * sc.RRC_DELAY:
            return {"status": "incomplete"}
        seg = np.asarray(rx[lo:min(end, len(rx))], np.complex64)
        if len(seg) < end - lo:
            seg = np.concatenate([seg, np.zeros(end - lo - len(seg), np.complex64)])
        n_blocks = _n_blocks(n_bytes, cfg.bits_per_symbol)
        n_sym = PRE_HF_LEN + N * (1 + n_blocks)
        r = _grid(seg, t0, cfo, n_sym)
        # fine frequency: every UW against the next (N symbols apart, same channel)
        uws = [r[PRE_HF_LEN - UW_LEN + b * N:PRE_HF_LEN + b * N] for b in range(n_blocks + 2)]
        cfo += np.angle(sum(np.vdot(uws[k], uws[k + 1]) for k in range(len(uws) - 1))) / (2 * np.pi * N)
        r = _grid(seg, t0, cfo, n_sym)
        _, H, nv = _header(r)
        payload, crc_ok, ber, evm, d = _decode(r, cfg, n_bytes, H, nv)
        evm = 100 * 10 ** (-PRE_SNR_DB / 20)     # the readout: the preamble's SNR (see _first_channel)
        return {"status": "ok", "payload": payload, "crc_ok": crc_ok, "cfo_hz": cfo * cfg.symbol_rate,
                "ber": ber, "evm": evm, "constellation": d, "frame_start_index": int(frame_start),
                "next_native_search_start": int(frame_start + frame_samples(cfg, n_bytes)) - 2 * SPS,
                "retry_count": 0, "decode_s": time.time() - t_start}
