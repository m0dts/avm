#!/usr/bin/env python3
"""
Compiled receive front end: turns the SDR's native-rate IQ (e.g. a
PlutoSDR's 550 kS/s) into the modem's own rate (e.g. ~80 kS/s for mode A)
as it arrives, in one pass per chunk:

    IQ imbalance correction -> LO-offset removal -> 129-tap low-pass FIR
    -> linear resampling to the modem rate

It wraps the SDR/stdin reader and presents the same interface
(`chunks` of complex64 bytes, `total_len`, `eof`, `wait_for_more`, ...),
so hf_ofdm_rx.py's decode path runs unchanged on the smaller stream -- its
own native-rate IQ/filter/resample stages simply see input already at the
modem rate.

Why: on a Raspberry Pi 4 the per-sample work at 550 kS/s dominated the
receiver. The same stages done with numpy over complex128 arrays cost
~25 ms per 0.24 s fragment on the dev PC; here:
  * the FIR is evaluated only at the two input samples each output
    sample interpolates between (the linear resampler never looks at
    the others), ~7x less filtering at 550 -> 80 kS/s;
  * the LO offset uses a small repeating phasor table instead of a
    complex exp per sample (100 kHz at 550 kS/s repeats every 11
    samples);
  * the kernel releases the GIL, so this runs on its own core in
    parallel with decoding.

The maths matches the numpy chain it replaces (correct_iq_imbalance ->
apply_lowpass_filter_incremental -> linear_resample_incremental, same
taps, same zero-history start, same interpolation grid) to float
rounding, with one deliberate difference: IQ correction runs BEFORE the
LO-offset shift, which is the physically right order (the imbalance is
in the raw I/Q; the numpy chain corrected after the Pluto source had
already shifted the samples).
"""
import sys
import threading

import numpy as np
from numba import njit

import hf_ofdm_common as ofdm

# Blind IQ-imbalance estimate: pass samples through uncorrected for the
# first IQ_FIRST_S seconds, then correct with coefficients re-estimated from
# ALL samples so far every IQ_UPDATE_S seconds. Measured on a perfectly
# balanced (TX->RX pipe) signal, the estimator's own noise imposes an image
# of -32.5 dB from 0.09 s of data (the old 50k-sample freeze point), -41 dB
# from 1 s, -44 dB from 6 s -- so a short frozen estimate cost ~2.5% EVM on
# a clean link. The numpy chain had to freeze early (it re-processes its
# whole buffer on every retry, so changing coefficients would change
# samples it had already used); this front end emits each sample exactly
# once, so it can keep refining. Updates happen at fixed sample counts
# (chunks are split there), so results never depend on input chunking.
IQ_FIRST_S = 1.0
IQ_UPDATE_S = 0.25


@njit(cache=True, nogil=True)
def _iq_sums(x, acc):
    """Accumulate I, Q, I^2, Q^2, I*Q sums (for the IQ-imbalance estimate)."""
    for i in range(x.shape[0]):
        I = np.float64(x[i].real)
        Q = np.float64(x[i].imag)
        acc[0] += I
        acc[1] += Q
        acc[2] += I * I
        acc[3] += Q * Q
        acc[4] += I * Q


@njit(cache=True, nogil=True, fastmath={"reassoc", "contract"})
def _process(x, hist, taps_rev, n_in0, n_out0, step, iq, lo_on, lo_table, lo_phase0, out):
    """One chunk of native-rate samples -> as many modem-rate samples as
    the data so far allows.

    x: new native samples (complex64). hist: the previous nt processed
    (IQ-corrected, LO-shifted) native samples, zeros at stream start --
    nt, not nt-1: an output interpolating between the last sample of the
    previous chunk and the first of this one needs y[j] for j = that last
    sample, i.e. nt-1 samples BEFORE it as well.
    n_in0: global index of x[0]. n_out0: global index of the next output.
    step: native samples per output sample (fs_in / fs_out).
    iq: [apply, mean_I, mean_Q, slope, scale]. lo_on: apply the LO shift,
    x[i] *= lo_table[(lo_phase0 + i) % len(lo_table)].
    Writes into out, returns (n_written, new hist).

    Single precision: samples are held as separate float32 real/imag arrays
    and the FIR runs over time-reversed taps (taps_rev[m] = taps[nt-1-m]),
    so its inner sum reads memory forwards and contiguously and LLVM can
    vectorise it 4 lanes wide on ARM NEON (fastmath reassoc/contract allows
    the reordered, fused sum). The IQ correction and LO shift per sample
    stay in double precision. Float32 rounding (~1e-7) is far below any
    received signal's noise."""
    nt = taps_rev.shape[0]
    nh = nt
    n = x.shape[0]
    wr = np.empty(nh + n, np.float32)
    wi = np.empty(nh + n, np.float32)
    for i in range(nh):
        wr[i] = hist[i].real
        wi[i] = hist[i].imag
    period = lo_table.shape[0]
    ph = lo_phase0
    apply_iq = iq[0] != 0.0
    for i in range(n):
        I = np.float64(x[i].real)
        Q = np.float64(x[i].imag)
        if apply_iq:
            I0 = I - iq[1]
            Q1 = ((Q - iq[2]) - iq[3] * I0) * iq[4]
            I = I0 + iq[1]
            Q = Q1 + iq[2]
        v = complex(I, Q)
        if lo_on:
            v = v * lo_table[ph]
            ph += 1
            if ph == period:
                ph = 0
        wr[nh + i] = v.real
        wi[nh + i] = v.imag
    base = n_in0 - nh          # global native index of wr[0]
    last = n_in0 + n - 1       # last global native index available
    k_out = 0
    m = n_out0
    while True:
        pos = m * step
        j = int(np.floor(pos))
        if j + 1 > last:
            break
        # FIR output at native j and j+1 (causal: y[j] = sum taps[k] x[j-k]
        # = sum taps_rev[q] x[j-nt+1+q])
        yr0 = np.float32(0.0)
        yi0 = np.float32(0.0)
        yr1 = np.float32(0.0)
        yi1 = np.float32(0.0)
        a0 = j - base - nt + 1
        for q in range(nt):
            t = taps_rev[q]
            yr0 += t * wr[a0 + q]
            yi0 += t * wi[a0 + q]
            yr1 += t * wr[a0 + 1 + q]
            yi1 += t * wi[a0 + 1 + q]
        frac = np.float32(pos - j)
        out[k_out] = complex(yr0 + frac * (yr1 - yr0), yi0 + frac * (yi1 - yi0))
        k_out += 1
        m += 1
    new_hist = np.empty(nh, np.complex64)
    for i in range(nh):
        new_hist[i] = complex(wr[n + i], wi[n + i])
    return k_out, new_hist


lo_phasor_table = ofdm.lo_phasor_table


class FrontEnd:
    """The streaming state; process() takes native complex64 samples and
    returns modem-rate complex64 samples."""

    def __init__(self, in_fs, out_fs, taps, lo_offset_hz=0.0, iq_correct=True):
        self.step = float(in_fs) / float(out_fs)
        self.taps = np.ascontiguousarray(taps, np.float64)
        self.taps_rev = np.ascontiguousarray(self.taps[::-1], np.float32)
        self.hist = np.zeros(len(taps), np.complex64)
        self.n_in = 0
        self.n_out = 0
        self.iq_correct = iq_correct
        self.iq = np.zeros(5)           # [apply, mean_I, mean_Q, slope, scale]
        self._iq_acc = np.zeros(5)
        self._iq_count = 0
        self._iq_first = max(1, int(round(IQ_FIRST_S * in_fs)))
        self._iq_step = max(1, int(round(IQ_UPDATE_S * in_fs)))
        # None = no short exact period (unusual offset/rate pair): process()
        # then builds each chunk's phasor with exp instead -- slower, same result.
        self.lo_table = lo_phasor_table(lo_offset_hz, in_fs)
        self.lo_offset_hz = lo_offset_hz
        self.in_fs = float(in_fs)

    def _next_iq_boundary(self):
        """Sample count at which the IQ coefficients are next (re)computed."""
        if self._iq_count < self._iq_first:
            return self._iq_first
        return self._iq_first + ((self._iq_count - self._iq_first) // self._iq_step + 1) * self._iq_step

    def _update_iq(self, x):
        """Accumulate x into the IQ statistics; at each fixed boundary (see
        IQ_FIRST_S/IQ_UPDATE_S) recompute the coefficients from everything
        so far -- same estimator as ofdm.correct_iq_imbalance."""
        if not self.iq_correct:
            return
        boundary = self._next_iq_boundary()
        _iq_sums(x, self._iq_acc)
        self._iq_count += len(x)
        if self._iq_count < boundary:
            return
        s = self._iq_acc / self._iq_count
        mean_I, mean_Q = s[0], s[1]
        var_I = s[2] - mean_I ** 2
        var_Q = s[3] - mean_Q ** 2
        cross = s[4] - mean_I * mean_Q
        if var_I <= 0:
            return  # e.g. silence so far: keep whatever we had, try again next update
        slope = cross / var_I
        var_Q1 = var_Q - 2 * slope * cross + slope ** 2 * var_I
        if var_Q1 <= 0:
            return
        self.iq[:] = (1.0, mean_I, mean_Q, slope, np.sqrt(var_I / var_Q1))

    def process(self, x):
        x = np.ascontiguousarray(x, np.complex64)
        outs = []
        while self.iq_correct and len(x):
            # split exactly at each coefficient-update point, so the result
            # never depends on how the input was chunked
            need = self._next_iq_boundary() - self._iq_count
            if need >= len(x):
                break
            outs.append(self._process_chunk(x[:need]))
            x = x[need:]
        outs.append(self._process_chunk(x))
        return outs[0] if len(outs) == 1 else np.concatenate(outs)

    def _process_chunk(self, x):
        # process with the CURRENT coefficients first, then let these samples
        # count toward the estimate -- so the samples the estimate is made
        # from pass through uncorrected, like correct_iq_imbalance's
        n_max = int((self.n_in + len(x)) / self.step) - self.n_out + 2
        out = np.empty(max(n_max, 1), np.complex64)
        if self.lo_table is not None:
            table, phase0 = self.lo_table, self.n_in % len(self.lo_table)
        else:
            table = np.exp(1j * 2 * np.pi * self.lo_offset_hz * (self.n_in + np.arange(len(x))) / self.in_fs)
            phase0 = 0
        k, self.hist = _process(x, self.hist, self.taps_rev, self.n_in, self.n_out, self.step,
                                self.iq, bool(self.lo_offset_hz), table, phase0, out)
        self.n_in += len(x)
        self.n_out += k
        self._update_iq(x)
        return out[:k]


class FrontEndReader:
    """Wraps an SDR/stdin reader (anything with chunks/_lock/total_len/eof/
    wait_for_more) and exposes the same interface, carrying the front
    end's modem-rate output as complex64 bytes. Runs on its own thread."""

    def __init__(self, inner, frontend, stats_interval_s=None):
        """stats_interval_s: if set, log the raw input level (RMS and peak,
        dB relative to full scale 1.0) every that many seconds of input --
        shows at a glance whether a real SDR is delivering no signal, a
        weak one, or one that's clipping."""
        self._inner = inner
        self._fe = frontend
        self._stats_n = int(stats_interval_s * frontend.in_fs) if stats_interval_s else 0
        self._stats_acc = [0.0, 0.0, 0]  # sum |x|^2, peak |x|, count
        self.chunks = ofdm.ChunkList()
        self.total_len = 0
        self.eof = False
        self._lock = threading.Lock()
        self._new_data = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="rx-frontend")
        self._thread.start()

    def _run(self):
        taken = 0
        seen_len = 0
        leftover = b""
        while True:
            self._inner.wait_for_more(seen_len, timeout=0.5)
            with self._inner._lock:
                new = self._inner.chunks[taken:]
                taken = len(self._inner.chunks)
                discard = getattr(self._inner.chunks, "discard_before", None)
                if discard:
                    discard(taken)
                seen_len = self._inner.total_len
                inner_eof = self._inner.eof
            if new:
                data = leftover + b"".join(new)
                usable = len(data) - len(data) % 8
                leftover = data[usable:]
                if usable:
                    x = np.frombuffer(data[:usable], np.complex64)
                    x = np.where(np.isfinite(x), x, 0).astype(np.complex64)
                    if self._stats_n:
                        self._log_level(x)
                    y = self._fe.process(x)
                    if len(y):
                        chunk = y.astype(np.complex64, copy=False).tobytes()
                        with self._lock:
                            self.chunks.append(chunk)
                            self.total_len += len(chunk)
                        self._new_data.set()
            if inner_eof and taken == len(self._inner.chunks):
                with self._lock:
                    self.eof = True
                self._new_data.set()
                return

    def _log_level(self, x):
        a = self._stats_acc
        mag2 = x.real.astype(np.float64) ** 2 + x.imag.astype(np.float64) ** 2
        a[0] += float(mag2.sum())
        a[1] = max(a[1], float(np.sqrt(mag2.max())) if len(mag2) else 0.0)
        a[2] += len(x)
        if a[2] >= self._stats_n:
            rms = np.sqrt(a[0] / a[2])
            db = lambda v: 20 * np.log10(v) if v > 0 else -999.0
            print(f"[frontend] input level over {a[2] / self._fe.in_fs:.1f} s: RMS {db(rms):6.1f} dBFS, "
                  f"peak {db(a[1]):6.1f} dBFS{'  <-- CLIPPING' if a[1] >= 0.99 else ''}"
                  f"{'  <-- no signal?' if rms < 1e-4 else ''}", file=sys.stderr, flush=True)
            self._stats_acc = [0.0, 0.0, 0]

    def wait_for_more(self, since_len, timeout=None):
        """Blocks until more bytes than since_len have arrived, or EOF."""
        while True:
            with self._lock:
                if self.eof or self.total_len > since_len:
                    return
            self._new_data.clear()
            self._new_data.wait(timeout=timeout)

    def close(self):
        close = getattr(self._inner, "close", None)
        if close:
            close()

    def __getattr__(self, name):
        # anything else (rx_gain_db, set_gain, ...) belongs to the SDR reader
        return getattr(self._inner, name)
