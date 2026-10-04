#!/usr/bin/env python3
"""
HF OFDM transmitter. Reads raw bytes from stdin, builds a frame (Schmidl &
Cox preamble + training/header/data OFDM symbols), and writes the complex
baseband waveform to stdout as raw interleaved float32 IQ (i.e. numpy
complex64 bytes, no header) at the mode's sample rate.

Usage:
    echo "hello hf" | python3 hf_ofdm_tx.py --mode B > frame.iq
    cat file.bin   | python3 hf_ofdm_tx.py --mode D --occupancy 20 > frame.iq

Pipe straight into hf_ofdm_rx.py, optionally through hf_ofdm_channel.py:
    cat msg.txt | python3 hf_ofdm_tx.py --mode B | python3 hf_ofdm_rx.py --mode B

--amplitude-sweep transmits the SAME message repeatedly at a list of
amplitudes back to back (with a gap of silence between each), so you can
capture one continuous recording and compare decode quality/SNR across TX
levels in a single run -- handy for finding whether a real SDR link is
being clipped/compressed at the amplitude you'd normally use:
    echo test | python3 hf_ofdm_tx.py --mode B --sample-rate 192000 \\
        --amplitude-sweep 0.02,0.05,0.1,0.2,0.4 | python3 tx.py
"""
import argparse
import os
import struct
import sys
import time

# One BLAS thread: the only BLAS work here (LDPC encode) is a small matmul,
# and OpenBLAS's extra worker threads busy-wait between calls -- measured on
# a Pi 4 as three threads at ~30% CPU each, doing nothing useful. Must be
# set before numpy is imported.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np

import hf_ofdm_common as ofdm

FRAG_HEADER = struct.Struct(">HHI")  # frag_index, total_frags, total_len -- see hf_ofdm_rx.py


class PipeSink:
    """The original output path: raw complex64 IQ bytes to stdout, for
    piping into a separate process (pluto_loopback.py's GNU Radio
    flowgraph, hf_ofdm_channel.py, hf_ofdm_rx.py directly, a file, etc)."""

    def write(self, waveform):
        out_bytes = waveform.astype(np.complex64, copy=False).tobytes()
        chunk = 1 << 16  # avoid Windows OSError 22 on single large pipe writes
        for i in range(0, len(out_bytes), chunk):
            sys.stdout.buffer.write(out_bytes[i:i + chunk])
        sys.stdout.buffer.flush()

    def close(self):
        pass


def make_sink(args, out_fs):
    if args.output == "pipe":
        return PipeSink()
    # Lazy import: SoapySDR (and the PlutoSDR support module) is only
    # needed when actually asked for -- most uses of this script are the
    # pipe-based path, which shouldn't require it to be installed at all.
    from pluto_soapy_sink import PlutoTxSink
    return PlutoTxSink(freq_hz=args.rf_freq, sample_rate_hz=out_fs,
                        tx_gain_db=args.tx_gain, uri=args.pluto_uri,
                        bandwidth_hz=args.pluto_bandwidth, queue_depth=args.tx_queue_depth,
                        stream_bufflen=args.pluto_bufflen, lo_offset_hz=args.lo_offset_hz,
                        driver=args.sdr, antenna=args.sdr_antenna, gain_file=args.gain_file)


def calibrate_fixed_peak(cfg, fragment_size, trials=200):
    """Estimates a single, fixed peak reference for --tx-normalize fixed
    by building `trials` synthetic frames from RANDOM payload data at the
    real --fragment-size and taking the max peak observed (with a small
    margin). OFDM's peak-to-average ratio is data-dependent -- how the
    specific bits happen to add up constructively/destructively across
    subcarriers -- so real fragments' peaks vary fragment to fragment;
    normalizing each one to ITS OWN peak (the default 'per-fragment'
    mode) means their AVERAGE power drifts by several dB even though
    peak power is always exactly full scale (confirmed for real: 6dB of
    spread across 30 random-payload fragments in the same config). Random
    bytes are a reasonably conservative proxy for real (e.g. Opus-
    encoded) content -- maximum-entropy data tends to sit close to a
    mode's worst-case PAPR, though this is a calibration heuristic, not a
    hard guarantee, hence the safety margin and the hard-clip fallback in
    run_stream() for the rare fragment that still exceeds it."""
    rng = np.random.default_rng()
    peaks = []
    for _ in range(trials):
        payload = FRAG_HEADER.pack(0, 0, 0) + rng.integers(0, 256, size=fragment_size, dtype=np.uint8).tobytes()
        waveform = ofdm.build_frame(cfg, payload)
        peaks.append(np.max(np.abs(waveform)))
    return max(peaks) * 1.05  # 5% margin above the worst observed in calibration


def run_stream(cfg, args, sink, fixed_peak=None):
    """Reads and transmits one --fragment-size chunk of stdin at a time,
    each as an independent fragment, writing its waveform immediately
    instead of batching the whole message (impossible for a source with
    no defined end) -- see --stream's help text. frag_idx is a rolling
    uint16 sequence counter (matches hf_ofdm_rx.py's streaming mode,
    which uses it purely for gap detection/logging, not a fixed total);
    total_frags/total_len in FRAG_HEADER are meaningless here and sent as
    0.

    Before consuming any real stdin data, optionally transmits
    --tx-warmup-s worth of REAL, full-power on-air fragments built from
    inert filler (see _WARMUP_FILLER below) -- see that constant's own
    comment for why."""
    frag_idx = 0
    clipped_count = 0
    # Started BEFORE warmup so input produced meanwhile is drained, not queued.
    backlog = _LiveInput(args.fragment_size, 1) if args.max_input_backlog else None

    def transmit_fragment(data: bytes, tag: str = ""):
        nonlocal frag_idx, clipped_count
        frag_payload = FRAG_HEADER.pack(frag_idx % 65536, 0, 0) + data
        waveform = ofdm.build_frame(cfg, frag_payload)

        out_fs = cfg.fs
        if args.sample_rate is not None and args.sample_rate != cfg.fs:
            # Single precision from here on: both sinks send complex64 anyway
            # (a 12-bit DAC on the Pluto), and the resample's large FFTs run
            # ~2x faster in float32 (numpy >= 2 keeps the dtype) -- ~10 ms
            # per fragment on a Raspberry Pi 4.
            waveform = ofdm.fft_resample_rate(waveform.astype(np.complex64), cfg.fs, args.sample_rate)
            out_fs = args.sample_rate

        # Normalize by RMS (not peak) so that --amplitude controls average
        # transmitted power consistently regardless of FEC scheme or payload
        # content.  Peak normalization is data-dependent: OFDM frames built
        # with LDPC vs Viterbi have different PAPR statistics, so after peak-
        # normalizing each to 1.0 they end up with different average power --
        # confirmed 5 dB spread for 1024-byte payloads.  RMS normalization
        # removes that dependence: unit-RMS waveform * amplitude delivers the
        # same average RF power regardless of how the FEC packed the bits.
        #
        # --tx-normalize fixed still divides by a fixed reference established
        # at startup (see calibrate_fixed_peak); the hard-clip fallback fires
        # only when an unusual frame exceeds that reference.
        if fixed_peak is not None:
            waveform = waveform / fixed_peak
            peak_now = np.max(np.abs(waveform))
            if peak_now > 1.0:
                clipped_count += 1
                waveform = np.clip(waveform.real, -1.0, 1.0) + 1j * np.clip(waveform.imag, -1.0, 1.0)
                print(f"WARNING: fragment seq={frag_idx % 65536} exceeded the fixed peak "
                      f"reference ({peak_now:.3f}x) -- hard-clipped ({clipped_count} total "
                      f"this session).", file=sys.stderr)
        else:
            # One dot product for the RMS, then ONE multiply by the combined
            # scale (was |x|^2, mean, divide, multiply: four passes over the
            # 550 kS/s fragment -- measurable on a Raspberry Pi 4).
            rms = np.sqrt(np.vdot(waveform, waveform).real / max(len(waveform), 1))
            scale = args.amplitude / rms if rms > 0 else args.amplitude
            if scale != 1.0:
                waveform = waveform * waveform.real.dtype.type(scale)
        if fixed_peak is not None and args.amplitude != 1.0:
            waveform = waveform * args.amplitude

        if args.fragment_gap_ms > 0:
            gap_len = int(round(out_fs * args.fragment_gap_ms / 1000))
            if gap_len > 0:
                waveform = np.concatenate([waveform, np.zeros(gap_len, dtype=waveform.dtype)])

        sink.write(waveform)
        print(f"{tag}Streamed fragment seq={frag_idx % 65536} ({len(data)} bytes, "
              f"{len(waveform) / out_fs * 1000:.1f} ms)", file=sys.stderr)
        frag_idx += 1

    if args.tx_warmup_s > 0:
        # Real RF output at full configured power, same as any other
        # fragment -- just carrying inert filler (a media_tx_framer.py
        # block with real_len=0, i.e. its first 2 bytes zero: parse_records
        # sees an empty block and does nothing with it) instead of stdin
        # data, so nothing real is held back or lost during this window.
        # Exists because of a real, confirmed-for-real symptom: a
        # receiver that was ALREADY locked and running failed to decode
        # (BER 0.17-0.30, mostly CRC MISMATCH, for dozens of fragments)
        # against a transmitter that had JUST been (re)started, then
        # started decoding cleanly once that transmitter had been running
        # a while -- with the SAME RF settings throughout, on a receiver
        # that never itself restarted. That rules out anything on the RX
        # side (already covered by PlutoRxSource's own warmup discard --
        # see its docstring) and points at the transmit chain still
        # settling: most plausibly the PA/mixer's own thermal/electrical
        # characteristics drifting for a while after real RF power starts
        # flowing (a physically real effect for analog TX front ends,
        # distinct from PlutoRxSource's digital AGC/DC-offset/quadrature
        # tracking loops, and NOT necessarily on the same timescale --
        # this default is a starting guess, not a measured value, and may
        # need lengthening; see --tx-warmup-s's own help text).
        _WARMUP_FILLER = bytes(args.fragment_size)
        deadline = time.time() + args.tx_warmup_s
        n_warmup = 0
        print(f"Warmup: transmitting ~{args.tx_warmup_s:.1f}s of on-air filler before consuming "
              f"real input (see --tx-warmup-s) -- covers the transmit chain's own settling time.",
              file=sys.stderr)
        while time.time() < deadline:
            transmit_fragment(_WARMUP_FILLER, tag="[warmup] ")
            n_warmup += 1
        print(f"Warmup done ({n_warmup} filler fragment(s)) -- now sending real input.",
              file=sys.stderr)

    if not args.max_input_backlog:
        while True:
            data = sys.stdin.buffer.read(args.fragment_size)
            if not data:
                break
            transmit_fragment(data)
    else:
        backlog.end_warmup(args.max_input_backlog)
        for data in backlog.drain():
            transmit_fragment(data)
        if backlog.dropped:
            print(f"Dropped {backlog.dropped} stale input fragment(s) in total (--max-input-backlog).",
                  file=sys.stderr)

    print(f"Stream ended: sent {frag_idx} fragment(s).", file=sys.stderr)


class _LiveInput:
    """--max-input-backlog: reads stdin on its own thread from the moment
    it's created and keeps only the newest `cap` fragments, dropping the
    oldest. For a LIVE source (media_tx_framer.py produces exactly one
    fragment per on-air fragment time) any backlog that forms -- most of
    all the --tx-warmup-s window, when nothing is read but the framer keeps
    producing -- can never drain, because input and output run at the same
    rate. Without this it stays as permanent end-to-end delay (2s for the
    default warmup). Not for non-live data: dropped fragments are gone."""

    def __init__(self, fragment_size, cap):
        import collections
        import threading
        self.fragment_size, self.cap = fragment_size, cap
        self.buf = collections.deque()
        self.cv = threading.Condition()
        self.eof = False
        self.dropped = 0
        threading.Thread(target=self._reader, daemon=True, name="tx-stdin").start()

    def _reader(self):
        while True:
            data = sys.stdin.buffer.read(self.fragment_size)
            with self.cv:
                if not data:
                    self.eof = True
                    self.cv.notify()
                    return
                self.buf.append(data)
                while len(self.buf) > self.cap:
                    self.buf.popleft()
                    self.dropped += 1
                    if self.dropped == 1 or self.dropped % 20 == 0:
                        print(f"[latency cap] dropped a stale input fragment ({self.dropped} so far)",
                              file=sys.stderr)
                self.cv.notify()

    def end_warmup(self, cap, keep=0):
        """Drop what queued up during warmup except the newest `keep`
        fragments (the rest is stale, and at equal in/out rates would be
        permanent delay), then switch to `cap` -- a loose safety net, NOT a
        tight limit: a tight cap also fires on ordinary framer tick jitter,
        and every drop costs video frames plus the temporal prediction's
        recovery time. keep=0: a 2-fragment cushion was tried while chasing
        a video-quality drop that turned out to be a 6 vs 12 fps change;
        PlutoTxSink's own --tx-queue-depth already absorbs jitter."""
        with self.cv:
            while len(self.buf) > keep:
                self.buf.popleft()
                self.dropped += 1
            self.cap = cap
        print(f"[latency cap] discarded the warmup backlog; {self.dropped} stale fragment(s) "
              f"dropped so far", file=sys.stderr)

    # A backlog that forms AFTER warmup (a CPU stall, the framer starting
    # late or running a hair fast) can't drain either, and the loose cap
    # let it sit at up to `cap` fragments -- measured ~1.2 s of the 2.4 s
    # camera-to-screen delay at 250 kHz. Trim it, but only once it has
    # persisted: ordinary framer tick jitter is momentary.
    STANDING_MIN = 2         # fragments waiting at once ...
    STANDING_S = 3.0         # ... continuously for this long -> drop the oldest
    REPORT_S = 10.0

    def drain(self):
        standing_since = None
        last_report = time.monotonic()
        peak = 0
        while True:
            with self.cv:
                while not self.buf and not self.eof:
                    self.cv.wait()
                if not self.buf:
                    return
                now = time.monotonic()
                waiting = len(self.buf)
                peak = max(peak, waiting)
                if waiting >= self.STANDING_MIN:
                    if standing_since is None:
                        standing_since = now
                    elif now - standing_since >= self.STANDING_S:
                        # proven standing: clear it in one go, back to one waiting
                        while len(self.buf) > self.STANDING_MIN - 1:
                            self.buf.popleft()
                            self.dropped += 1
                        standing_since = None
                        print(f"[latency cap] {waiting} fragments had been waiting {self.STANDING_S:g}+ s: "
                              f"dropped {waiting - len(self.buf)} ({self.dropped} dropped so far)",
                              file=sys.stderr)
                else:
                    standing_since = None
                if now - last_report >= self.REPORT_S:
                    print(f"[latency] input backlog {waiting} now, peak {peak} in the last "
                          f"{self.REPORT_S:g} s", file=sys.stderr)
                    last_report, peak = now, 0
                data = self.buf.popleft()
            yield data


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=list(ofdm.DRM_MODES), default="B")
    ap.add_argument("--occupancy", type=float, default=20,
                     help="Spectrum occupancy in kHz (default 20). One of DRM's official values "
                          "(4.5, 5, 9, 10, 18, 20) uses its exact spec'd carrier range; anything "
                          "else (e.g. 40, 80 -- wider than DRM ever standardized, needs a "
                          "correspondingly wideband SDR) falls back to a symmetric carrier-count "
                          "scaling instead of an exact spec range.")
    ap.add_argument("--modulation", choices=["qpsk", "16qam"], default="qpsk",
                     help="PAYLOAD modulation (default qpsk). 16qam doubles payload bits/carrier "
                          "(roughly double throughput at a given occupancy) but needs about "
                          "6-7dB more SNR for the same error rate -- an explicit per-link choice, "
                          "not a safe default, since that's exactly the margin a fading HF channel "
                          "eats into. The preamble/pilots/header always stay QPSK regardless of "
                          "this setting (they're not the bottleneck and need to stay robust). "
                          "Must match on both hf_ofdm_tx.py and hf_ofdm_rx.py.")
    ap.add_argument("--fec", choices=["viterbi", "ldpc"], default="viterbi",
                     help="Forward Error Correction scheme for PAYLOAD data (default viterbi). "
                          "'viterbi' uses rate-1/2 K=7 convolutional code. 'ldpc' uses IEEE 802.11n "
                          "rate-1/2 QC-LDPC (~1.5-2.5dB coding gain over Viterbi). The frame header "
                          "signals the scheme so an auto-configured receiver detects it automatically.")
    ap.add_argument("--sample-rate", type=float, default=None,
                     help="Resample output IQ to this rate in Hz (e.g. to match an SDR's fixed rate). "
                          "Default: emit at the mode's native rate with no resampling.")
    ap.add_argument("--amplitude", type=float, default=0.1,
                     help="Fraction of full scale, 0-1 (default 0.1): the waveform is normalized "
                          "to a true unit peak first, so 1.0 just touches clipping and never "
                          "exceeds it, regardless of this mode's own PAPR. Keep a couple of dB "
                          "of margin below 1.0 for real-world headroom (e.g. 0.7-0.9).")
    ap.add_argument("--amplitude-sweep", type=str, default=None,
                     help="Comma-separated list of amplitudes, e.g. '0.02,0.05,0.1,0.2'. Transmits "
                          "the same message once per amplitude, back to back with a gap of silence "
                          "between each, in one continuous output stream. Overrides --amplitude.")
    ap.add_argument("--sweep-gap-ms", type=float, default=500,
                     help="Silence gap in ms between amplitude-sweep segments (default 500).")
    ap.add_argument("--fragment-size", type=int, default=None,
                     help="Split a large payload into multiple independently-synced frames of at "
                          "most this many bytes each, sent back to back with a gap between them "
                          "(see --fragment-gap-ms). Recommended for large payloads: a single very "
                          "long frame has no way to re-sync mid-transmission, so it's vulnerable to "
                          "oscillator drift/sample-clock offset accumulating over its whole duration. "
                          "Default: send as one frame no matter the size.")
    ap.add_argument("--fragment-gap-ms", type=float, default=300,
                     help="Silence gap in ms between fragments (default 300).")
    ap.add_argument("--stream", action="store_true",
                     help="Continuous streaming mode for an indefinite source (e.g. live audio) "
                          "instead of reading all of stdin before transmitting anything -- the "
                          "normal mode blocks on a single read() until EOF, which never happens "
                          "for a source that doesn't end. Reads and transmits one --fragment-size "
                          "chunk at a time as soon as it's available, looping until stdin closes. "
                          "Requires --fragment-size; incompatible with --amplitude-sweep (which "
                          "needs the whole waveform known upfront).")
    ap.add_argument("--tx-warmup-s", type=float, default=2.0, metavar="S",
                     help="With --stream: seconds of real, full-power on-air filler fragments "
                          "(inert -- see run_stream's own comment) transmitted BEFORE any real "
                          "stdin data, to cover the transmit chain's own settling time (default "
                          "2.0; 0 disables it). This is a starting guess, not a measured value -- "
                          "confirmed for real that an already-locked receiver failed to decode "
                          "against a just-started transmitter for well over 10s before it settled, "
                          "so if fragments still fail for a while right after TX starts, try a "
                          "larger value.")
    ap.add_argument("--tx-normalize", choices=["per-fragment", "fixed"], default="per-fragment",
                     help="How each --stream fragment's waveform is scaled to fit full scale "
                          "(default per-fragment, the same as this script has always done). "
                          "OFDM's peak-to-average ratio is data-dependent, so normalizing each "
                          "fragment to ITS OWN peak (per-fragment) means average transmitted "
                          "power drifts by several dB fragment to fragment even though peak "
                          "power is always exactly full scale -- confirmed for real: ~6dB of "
                          "spread across 30 random-payload fragments in the same config. 'fixed' "
                          "instead calibrates one peak reference at startup (from synthetic "
                          "random-payload frames -- a conservative proxy, not a hard guarantee) "
                          "and reuses it for every fragment, giving steady average power at the "
                          "cost of occasionally hard-clipping a fragment whose true peak exceeds "
                          "that reference (a warning prints on the rare fragment this happens "
                          "to). Only affects --stream; the batch (non-stream) path already "
                          "normalizes its whole multi-fragment waveform once, together, so it "
                          "never had this drift.")
    ap.add_argument("--output", choices=["pipe", "pluto"], default="pipe",
                     help="'pipe' (default): write raw IQ bytes to stdout, for piping into a "
                          "separate process (pluto_loopback.py's GNU Radio flowgraph, "
                          "hf_ofdm_channel.py, hf_ofdm_rx.py directly, a file). "
                          "'pluto': skip the pipe entirely and transmit straight to a PlutoSDR "
                          "via SoapySDR, one process/hop fewer between this script and the "
                          "hardware. Requires --rf-freq; see --pluto-uri/--tx-gain/"
                          "--pluto-bandwidth. Needs the SoapySDR Python bindings and the "
                          "PlutoSDR support module installed (only imported when this is used).")
    ap.add_argument("--rf-freq", type=float, default=None,
                     help="Center frequency in Hz for --output pluto (required with it).")
    ap.add_argument("--tx-gain", type=float, default=-10.0,
                     help="PlutoSDR TX gain/attenuation in dB for --output pluto (default -10).")
    ap.add_argument("--sdr", choices=["pluto", "lime"], default="pluto",
                    help="Radio for --output pluto: 'pluto' (PlutoSDR, default) or 'lime' "
                         "(LimeSDR-USB or LimeSDR Mini). --tx-gain keeps its meaning (dB re full "
                         "power) on either.")
    ap.add_argument("--sdr-antenna", type=str, default=None,
                    help="LimeSDR TX antenna port (BAND1/BAND2). Default: chosen by frequency "
                         "from the ports the board reports.")
    ap.add_argument("--gain-file", type=str, default=None,
                    help="Watch this file for a new --tx-gain value (dB) while running -- live "
                         "gain from a GUI, for radios without an outside control route (LimeSDR).")
    ap.add_argument("--pluto-uri", type=str, default=None,
                     help="SoapySDR device URI for --output pluto, e.g. 'ip:192.168.2.1' for a "
                          "network-attached Pluto. Default: auto-select (fine with exactly one "
                          "Pluto attached).")
    ap.add_argument("--pluto-bandwidth", type=float, default=None,
                     help="Analog front-end bandwidth in Hz for --output pluto. Default: let the "
                          "driver pick (usually close to the sample rate).")
    ap.add_argument("--max-input-backlog", type=int, default=0, metavar="N",
                     help="With --stream, for LIVE sources only: discard input that queues up "
                          "during --tx-warmup-s (it would otherwise be permanent latency), then "
                          "keep at most N unsent fragments as a loose safety net, dropping the "
                          "oldest. Default 0 = never drop (required for non-live data).")
    ap.add_argument("--tx-queue-depth", type=int, default=8,
                     help="For --output pluto: how many fragments PlutoTxSink will build ahead "
                          "of what's actually been transmitted (default 8). This is real, "
                          "significant end-to-end latency, not just a safety margin -- at "
                          "--fragment-size 1024/~0.7-1s per fragment, a full queue means up to "
                          "~6-8s between building a fragment and it actually going out over the "
                          "air. Lower it (e.g. 2-3) for less TX-side latency; you still get the "
                          "gap-filling/underrun protection this queue exists for (see "
                          "PlutoTxSink's docstring), just with less lookahead cushion against "
                          "timing jitter on the input side.")
    ap.add_argument("--pluto-bufflen", type=int, default=None,
                     help="For --output pluto: SoapyPlutoSDR's own hardware-side TX buffer size, "
                          "in SAMPLES. Default: driver auto-picks sample_rate/60 rounded to a "
                          "power of 2 (its own 'Auto setting Buffer Size' log line -- e.g. 32768 "
                          "samples at 1MSPS, ~33ms). This is real hardware latency on top of "
                          "--tx-queue-depth, not the same thing -- smaller means less of it, at "
                          "real risk of underrun if writeStream can't keep the hardware fed at "
                          "that finer granularity.")
    ap.add_argument("--lo-offset-hz", type=float, default=0.0,
                     help="For --output pluto: tune the PlutoSDR's actual LO this far away from "
                          "--rf-freq, pre-shifting the transmitted baseband digitally to "
                          "compensate, so the signal lands at --rf-freq while the AD9363's own "
                          "DC/LO leakage spike (which sits wherever the LO is ACTUALLY tuned) "
                          "lands off to the side of it instead of in the middle of the signal. "
                          "Must match the receiver's --lo-offset-hz. Default 0 (no offset, spike "
                          "sits mid-band -- matches pluto_loopback.py's old default of a 25kHz "
                          "offset, which this replaces).")
    args = ap.parse_args()
    import avm_threads
    avm_threads.install(main_name="tx-modulate")  # thread names visible in top -H

    if args.output == "pluto" and not args.rf_freq:
        print("ERROR: --output pluto requires --rf-freq.", file=sys.stderr)
        sys.exit(1)

    cfg = ofdm.build_config(args.mode, args.occupancy, data_modulation=args.modulation, fec_scheme=args.fec)
    ofdm.print_config(cfg, fragment_size_bytes=args.fragment_size, fragment_gap_ms=args.fragment_gap_ms)
    out_fs = args.sample_rate if args.sample_rate is not None else cfg.fs
    sink = make_sink(args, out_fs)

    if args.stream:
        if not args.fragment_size:
            print("ERROR: --stream requires --fragment-size.", file=sys.stderr)
            sys.exit(1)
        if args.amplitude_sweep is not None:
            print("ERROR: --stream is incompatible with --amplitude-sweep.", file=sys.stderr)
            sys.exit(1)
        fixed_peak = None
        if args.tx_normalize == "fixed":
            print("Calibrating fixed peak reference...", file=sys.stderr)
            fixed_peak = calibrate_fixed_peak(cfg, args.fragment_size)
            print(f"Fixed peak reference: {fixed_peak:.4f}", file=sys.stderr)
        try:
            run_stream(cfg, args, sink, fixed_peak=fixed_peak)
        finally:
            sink.close()
        return

    payload = sys.stdin.buffer.read()
    print(f"Encoding {len(payload)} bytes...", file=sys.stderr)

    frag_size = args.fragment_size or len(payload) or 1
    n_frags = max(1, -(-len(payload) // frag_size))  # ceil div
    frames = []
    for i in range(n_frags):
        frag_data = payload[i * frag_size:(i + 1) * frag_size]
        frag_payload = FRAG_HEADER.pack(i, n_frags, len(payload)) + frag_data
        frames.append(ofdm.build_frame(cfg, frag_payload))
    if n_frags > 1:
        print(f"Split into {n_frags} fragments of up to {frag_size} bytes each", file=sys.stderr)
        gap = np.zeros(int(round(cfg.fs * args.fragment_gap_ms / 1000)), dtype=complex)
        parts = []
        for f in frames:
            parts.append(f)
            parts.append(gap)
        waveform = np.concatenate(parts)
    else:
        waveform = frames[0]
    print(f"Frame duration: {len(waveform) / cfg.fs * 1000:.1f} ms "
          f"({len(waveform)} samples @ {cfg.fs:.0f} Hz)", file=sys.stderr)

    out_fs = cfg.fs
    if args.sample_rate is not None and args.sample_rate != cfg.fs:
        waveform = ofdm.fft_resample_rate(waveform, cfg.fs, args.sample_rate)
        out_fs = args.sample_rate
        print(f"Resampled {cfg.fs:.1f} Hz -> {out_fs:.1f} Hz "
              f"({len(waveform)} samples)", file=sys.stderr)

    # Normalize by RMS so that --amplitude controls average transmitted power
    # consistently, regardless of FEC scheme (LDPC vs Viterbi) or payload
    # content.  Peak normalization is data-dependent: OFDM frames from LDPC
    # and Viterbi have different PAPR statistics, so dividing each by its own
    # peak leaves them at different average power levels -- confirmed ~5 dB
    # spread for the same payload size.  RMS normalization removes that
    # dependency: a unit-RMS waveform * amplitude delivers equal average RF
    # power regardless of coding scheme.  Note: RMS normalization gives no
    # hard guarantee against exceeding full scale on any individual sample
    # (OFDM peaks can be ~12-16 dB above RMS); keep --amplitude well below 1
    # to leave headroom for peaks, or use --tx-normalize fixed to track the
    # calibrated per-scheme peak instead.
    rms = np.sqrt(np.mean(np.abs(waveform) ** 2))
    if rms > 0:
        waveform = waveform / rms


    if args.amplitude_sweep is not None:
        amplitudes = [float(a) for a in args.amplitude_sweep.split(",")]
        gap = np.zeros(int(round(out_fs * args.sweep_gap_ms / 1000)), dtype=complex)
        segments = []
        t_ms = 0.0
        for amp in amplitudes:
            print(f"Sweep segment @ amplitude {amp}: starts at {t_ms:.1f} ms, "
                  f"{len(waveform) / out_fs * 1000:.1f} ms long", file=sys.stderr)
            segments.append(waveform * amp)
            segments.append(gap)
            t_ms += (len(waveform) + len(gap)) / out_fs * 1000
        waveform = np.concatenate(segments)
    elif args.amplitude != 1.0:
        waveform = waveform * args.amplitude
        print(f"Scaled amplitude by {args.amplitude}", file=sys.stderr)

    try:
        sink.write(waveform)
    finally:
        sink.close()


if __name__ == "__main__":
    main()
