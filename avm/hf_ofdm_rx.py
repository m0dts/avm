#!/usr/bin/env python3
"""
HF OFDM receiver. Reads raw complex64 IQ samples from stdin (as produced
by hf_ofdm_tx.py, optionally passed through a channel, or streamed live
from an SDR flowgraph like rx.py), finds each frame via Schmidl & Cox
preamble correlation, estimates and corrects fractional and integer CFO,
demodulates, equalizes using periodic training symbols, and writes the
decoded payload bytes to stdout.

Every frame tx-side carries a small fragmentation header (fragment index,
total fragments, total message length) -- transparent for a normal,
single-frame message (total fragments = 1), and lets hf_ofdm_tx.py split a
large payload into several independently-synced frames (see its
--fragment-size) so a long transfer isn't riding on one uninterrupted
frame for its entire duration, which is vulnerable to oscillator drift/
sample-clock offset accumulating over a very long single burst. This
script waits for and reassembles all fragments before writing output.

Attempts a real decode as soon as enough samples have arrived to look
promising, rather than waiting for stdin to close -- important for a
live/streaming source (e.g. rx.py) that never closes its own stdout, where
waiting for EOF would mean decoding never happens at all.

All diagnostics go to stderr so stdout carries only the reassembled
message bytes.

Usage:
    python3 hf_ofdm_rx.py --mode B < frame.iq > recovered.bin
"""
import argparse
import os
import queue
import struct
import sys
import threading
import time

# One BLAS thread -- see hf_ofdm_tx.py: OpenBLAS's worker threads busy-wait
# between the small matmuls here. Must be set before numpy is imported.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import numpy as np

import hf_ofdm_common as ofdm

FRAG_HEADER = struct.Struct(">HHI")  # frag_index, total_frags, total_len


class StdinReader:
    """Continuously drains stdin on its own background thread into a
    growing list of chunks, completely decoupled from however long the
    main thread spends decoding. This matters a lot on a live SDR pipe:
    the OS pipe buffer is small (on Windows, commonly ~64KB, which fills
    in well under 50ms at 192kHz complex64), so any processing-side stall
    longer than that -- a sync retry, a slow decode attempt -- stops
    draining the pipe, which blocks the upstream writer (rx.py), which
    can't then drain the SDR's own hardware buffer either -- a REAL sample
    drop (observed as libiio/SoapySDR 'O' overflow markers) at exactly the
    moment decoding was busy, not a receiver-side artifact. Reading on its
    own thread means the pipe is always being drained as fast as the OS
    can deliver bytes, regardless of decode load."""

    def __init__(self, chunk_size=1 << 15):
        self.chunks = ofdm.ChunkList()
        self.total_len = 0
        self._lock = threading.Lock()
        self._new_data = threading.Event()
        self.eof = False
        self._thread = threading.Thread(target=self._run, args=(chunk_size,), daemon=True, name="rx-stdin")
        self._thread.start()

    def _run(self, chunk_size):
        while True:
            chunk = sys.stdin.buffer.read(chunk_size)
            with self._lock:
                if chunk:
                    self.chunks.append(chunk)
                    self.total_len += len(chunk)
                else:
                    self.eof = True
            self._new_data.set()
            if not chunk:
                return

    def wait_for_more(self, since_len, timeout=None):
        """Blocks until more bytes than since_len have arrived, or EOF."""
        while True:
            with self._lock:
                if self.eof or self.total_len > since_len:
                    return
            self._new_data.clear()
            self._new_data.wait(timeout=timeout)

    def close(self):
        pass  # nothing to release -- stdin closes with the process


def make_reader(args, cfg):
    """The IQ source, wrapped in the compiled front end (rx_frontend.py)
    whenever the input rate differs from the mode's own rate (unless
    --legacy-frontend): the front end then does IQ correction, LO-offset
    removal, anti-alias filtering and resampling as samples arrive, and
    everything downstream sees input already at cfg.fs. args.sample_rate
    is cleared to say so (the capture rate is kept in
    args.capture_sample_rate), and args.frontend_active is set so
    try_decode skips its own native-rate IQ correction."""
    capture_fs = args.sample_rate or cfg.fs
    use_frontend = capture_fs != cfg.fs and not args.legacy_frontend
    args.capture_sample_rate = capture_fs
    args.frontend_active = use_frontend
    if args.input == "pipe":
        reader = StdinReader()
        fe_lo = 0.0  # pipe input: --lo-offset-hz ignored, as before
    else:
        # Lazy import: SoapySDR (and the PlutoSDR support module) is only
        # needed when actually asked for -- most uses of this script are the
        # pipe-based path, which shouldn't require it to be installed at all.
        from pluto_soapy_sink import PlutoRxSource
        # With the front end, the LO-offset shift happens there (a small
        # phasor table, after IQ correction) instead of in the source (a
        # complex exp per sample).
        reader = PlutoRxSource(freq_hz=args.rf_freq, sample_rate_hz=capture_fs,
                               rx_gain_db=args.rx_gain, agc=args.rx_agc, uri=args.pluto_uri,
                               bandwidth_hz=args.pluto_bandwidth, stream_bufflen=args.pluto_bufflen,
                               lo_offset_hz=0.0 if use_frontend else args.lo_offset_hz,
                               tune_offset_hz=args.lo_offset_hz,
                               warmup_discard_s=args.rx_warmup_s,
                               driver=args.sdr, antenna=args.sdr_antenna, gain_file=args.gain_file,
                               ppm=args.sdr_ppm, freq_offset_file=args.freq_offset_file)
        fe_lo = args.lo_offset_hz
        if reader.sample_rate_hz != capture_fs:
            # the radio only offers listed rates (Airspy, SDRplay) and picked another
            capture_fs = reader.sample_rate_hz
            use_frontend = capture_fs != cfg.fs and not args.legacy_frontend
            args.capture_sample_rate = capture_fs
            args.frontend_active = use_frontend
            args.sample_rate = capture_fs
            # the LO-offset shift: the front end's job if there is one, else the source's
            reader.lo_offset_hz = 0.0 if use_frontend else args.lo_offset_hz
    if not use_frontend:
        return reader
    from rx_frontend import FrontEnd, FrontEndReader
    taps = ofdm.design_lowpass_fir(min(cfg.fs / 2, 0.45 * capture_fs), capture_fs, numtaps=129)
    args.sample_rate = None
    print(f"Front end: {capture_fs / 1e3:.1f} kS/s -> {cfg.fs / 1e3:.2f} kS/s (compiled IQ correction, "
          f"LO shift {fe_lo / 1e3:+.1f} kHz, 129-tap FIR at output points only)", file=sys.stderr)
    return FrontEndReader(reader, FrontEnd(capture_fs, cfg.fs, taps, lo_offset_hz=fe_lo),
                          stats_interval_s=5.0 if args.verbose else None)


class StdoutPacer:
    """Writes each fragment's bytes to stdout spread steadily over a given
    duration instead of in one instantaneous burst -- fixes an audible
    "break" landing once per fragment on an otherwise perfectly clean link
    (confirmed for real: a run with zero CRC failures, zero sequence gaps,
    zero deep fades, and BER ~0.002 still had a stall every ~0.7s, exactly
    matching --fragment-size's own real-time duration). The root cause: a
    live link can only ever produce one fragment's bytes all at once, the
    instant that fragment's own real-time-limited audio has fully arrived
    and decoded -- there is nothing to write in between. A player fed via
    a live pipe doesn't necessarily have enough of its own buffering to
    survive that idle-then-burst pattern, and audibly stalls right when
    its buffer runs dry between bursts. Trickling each fragment's bytes
    out over roughly the time until the next one is expected turns that
    into steady delivery, which any normal playback buffer handles fine,
    without needing the downstream player reconfigured at all.

    Runs on its own thread so pacing one fragment's output never blocks
    the main thread from immediately starting the NEXT fragment's decode
    (which is itself already real-time-paced by physical arrival, so
    there's no cost to overlapping the two)."""

    def __init__(self, queue_depth=2, pace=True):
        self._pace = pace
        # Bounded, deliberately small: an unbounded queue here can
        # silently absorb an unpredictable amount of backlog (e.g. a
        # transient hiccup on this thread) with zero visible sign of it
        # -- exactly the kind of "why is TX-to-RX lag randomly up to N
        # fragments" symptom this was found causing (see submit()'s
        # docstring for the matching bound on the TX side). Small on
        # purpose: submit() blocking briefly on a full queue is a much
        # more honest failure mode than an ever-growing silent one.
        self._queue = queue.Queue(maxsize=queue_depth)
        # Persistent target start time for the NEXT fragment -- see
        # _run's own comment for why this (not a fresh time.monotonic()
        # per fragment) is what actually stops per-fragment jitter from
        # accumulating forever.
        self._schedule_t = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="rx-output")
        self._thread.start()

    def submit(self, data, duration_s, debug_tag=None):
        """Enqueues data for paced output. Blocks if the queue is
        already at queue_depth -- normal operation keeps this nearly
        empty (the writer drains roughly as fast as fragments arrive),
        so blocking here means the writer has genuinely fallen behind,
        not a bug to route around with a bigger buffer.

        debug_tag (optional): an identifier (e.g. the fragment's seq)
        logged alongside the wall-clock time this thread actually
        FINISHES writing this fragment's last byte to stdout -- lets a
        caller diff that against a downstream reader's own receipt
        timestamp for the same seq, isolating whether a latency lives in
        decode/this queue (this timestamp itself is already late) or
        purely in the OS pipe/reader beyond this process's control (this
        timestamp is on time, but the reader sees it much later)."""
        submit_wall = time.time() if debug_tag is not None else None
        self._queue.put((data, duration_s, debug_tag, submit_wall))

    def drain(self):
        """Blocks until every submitted fragment has been fully written --
        call before the process exits, since the writer thread is a daemon
        and would otherwise be killed mid-write (truncating the last
        fragment or two) the moment main() returns."""
        self._queue.join()

    def _run(self):
        # ~20ms chunks -- fine enough to feel continuous. CRITICAL: sleep
        # against an absolute wall-clock TARGET, not a fixed per-chunk
        # duration -- time.sleep(0.02) on Windows can genuinely take
        # ~30ms (confirmed for real: measured ~56% overshoot, timer
        # resolution is coarse), and summing ~35 such calls per fragment
        # blindly compounds that into the pacer falling behind real time
        # by more than half, session-wide, with nothing anywhere to make
        # that visible (the queue is unbounded, so it just silently
        # backs up) -- confirmed for real: fragments kept decoding
        # cleanly the whole time, yet the receiver was still playing
        # audio 50-60s AFTER the transmitter had stopped, and no
        # --stats ratio at ANY encode bitrate ever got close to 1.0x,
        # both dead giveaways that time was leaking somewhere with no
        # backpressure to reveal it sooner. Computing each sleep as
        # "time left until the next absolute target" instead cancels
        # out any one call's overshoot on the next iteration, so drift
        # never accumulates WITHIN one fragment's own chunks no matter how
        # imprecise any single sleep is -- but confirmed for real this
        # was NOT the whole story: resetting fragment_start fresh to
        # time.monotonic() on every dequeued fragment only ever cancels
        # overshoot inside that one fragment, never carries a fragment
        # that started a few ms late into the NEXT fragment's own
        # schedule. At a bitrate with genuine zero packing margin, even
        # a few ms of unrecovered lag per fragment (ordinary OS
        # scheduling jitter -- unavoidable, not a bug in any single
        # sleep) compounds fragment after fragment with nothing to ever
        # claw it back, producing exactly the slow, ever-growing
        # playback-buffer deficit seen in real testing (confirmed via an
        # isolated, zero-RF paced-loopback test: audio_decoded/real_time
        # held steady right around 1.0x with no rate cap imposed, but
        # drifted to a persistent ~0.96x and a buffer margin diverging
        # past -1s the instant a fixed fragment-delivery rate matching
        # this link's own cadence was imposed -- proving the fixed-RATE
        # PACING itself, not the framer or player, was where time leaked
        # with no way back). self._schedule_t is a single persistent
        # target line advanced by exactly duration_s each fragment,
        # never resynced to "now" -- so a fragment that started late
        # computes its sub-chunk targets against where it SHOULD have
        # started, writing its remaining chunks back-to-back with no
        # sleep until real time catches back up to the schedule, instead
        # of unconditionally granting every fragment a fresh full
        # duration_s regardless of how far behind the last one already
        # ran.
        CHUNK_INTERVAL_S = 0.02
        # Resync instead of catching up once behind by this much -- a
        # real gap (TX restart, genuine dead air) should read as silence
        # and resume cleanly, not turn into a burst of many fragments'
        # worth of unpaced bytes all catching up at once, which would
        # defeat the whole reason this class exists.
        MAX_SCHEDULE_LAG_S = 1.0
        # A gap smaller than this recovers gradually rather than sitting
        # frozen forever. Confirmed for real this was a genuine bug, not
        # just theory: one single slow fragment (a retry) knocked the
        # schedule ~0.13s behind, and it stayed EXACTLY that far behind
        # for the next 100+ fragments -- because once real submission
        # cadence and the schedule both advance by the same duration_s
        # each fragment, the gap between them never naturally shrinks on
        # its own, no matter how small it is. That's not the "genuine
        # gap, resync cleanly" case MAX_SCHEDULE_LAG_S handles -- it's
        # every subsequent fragment silently spending its first
        # SMALL_LAG_TOLERANCE_S worth of chunks bursting out with zero
        # sleep (their targets are already in the past) before resuming
        # normal per-chunk trickling for the rest -- audible as exactly
        # the "bytes arrive in one lump" break this whole class exists
        # to prevent, just partial instead of total, and invisible to
        # any margin/average-byte-rate accounting since the total bytes
        # delivered per second is unaffected. Clamping the gap down to
        # this small a value every fragment (instead of leaving whatever
        # gap already exists untouched) forces it to actually shrink
        # back toward zero within a couple of fragments' worth of chunks
        # rather than persisting indefinitely.
        SMALL_LAG_TOLERANCE_S = 0.05
        while True:
            data, duration_s, debug_tag, submit_wall = self._queue.get()
            if data and not self._pace:
                # --no-output-pacing: the reader buffers for itself (the media
                # player's audio has its own playback buffer), so trickling
                # only delays -- measured ~0.4 s at 250 kHz (0.16 s trickle +
                # ~0.26 s waiting behind the previous fragment's trickle).
                try:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                except OSError as e:
                    print(f"Output pipe closed ({e}) -- downstream reader exited. Exiting.", file=sys.stderr)
                    self._queue.task_done()
                    os._exit(0)
                data = None
            if data:
                try:
                    now = time.monotonic()
                    if self._schedule_t is None or now - self._schedule_t > MAX_SCHEDULE_LAG_S:
                        self._schedule_t = now
                    elif now - self._schedule_t > SMALL_LAG_TOLERANCE_S:
                        self._schedule_t = now - SMALL_LAG_TOLERANCE_S
                    elif self._schedule_t - now > SMALL_LAG_TOLERANCE_S:
                        # The mirror case: a burst of fragments (decode
                        # catching up after a stall) pushes the schedule
                        # AHEAD of real time, and every later fragment then
                        # waited for its slot -- measured as a standing
                        # +0.25 s on every fragment at 250 kHz, pure delay
                        # (audio has its own playback buffer). Clamp it too.
                        self._schedule_t = now + SMALL_LAG_TOLERANCE_S
                    fragment_start = self._schedule_t
                    n_chunks = max(1, int(duration_s / CHUNK_INTERVAL_S))
                    chunk_size = max(1, -(-len(data) // n_chunks))  # ceil div
                    per_chunk_duration = duration_s / n_chunks
                    chunk_idx = 0
                    for pos in range(0, len(data), chunk_size):
                        sys.stdout.buffer.write(data[pos:pos + chunk_size])
                        sys.stdout.buffer.flush()
                        chunk_idx += 1
                        if pos + chunk_size < len(data):
                            target = fragment_start + chunk_idx * per_chunk_duration
                            remaining = target - time.monotonic()
                            if remaining > 0:
                                time.sleep(remaining)
                    self._schedule_t = fragment_start + duration_s
                    if debug_tag is not None:
                        done_wall = time.time()
                        print(f"  [pacer] seq={debug_tag} queued_at={submit_wall:.3f} "
                              f"flushed_at={done_wall:.3f} "
                              f"(queue_delay={  (done_wall - submit_wall) - duration_s:+.3f}s "
                              f"beyond its own {duration_s:.3f}s trickle)", file=sys.stderr)
                except OSError as e:
                    # The downstream reader (e.g. ffplay) exited or stopped
                    # reading -- Windows reports this as a generic "Invalid
                    # argument" (errno 22) instead of a clean broken-pipe
                    # error, but it's the same underlying event. There's
                    # nothing useful left to do once stdout is gone, and
                    # leaving this exception uncaught would both spam a
                    # traceback per future chunk AND deadlock any later
                    # drain() call forever (its queue.join() waits on a
                    # task_done() this thread would never reach again).
                    # Exiting the whole process immediately is the correct,
                    # clean response to the pipe going away.
                    print(f"Output pipe closed ({e}) -- downstream reader exited "
                          f"or stopped reading. Exiting.", file=sys.stderr)
                    self._queue.task_done()
                    os._exit(0)
            self._queue.task_done()


RAW_BUFFER_TRIM_MARGIN = 500_000    # native samples of safety margin kept behind trim_reference
RAW_BUFFER_TRIM_INTERVAL = 1_000_000  # only actually trim once this much MORE has accumulated
# (avoids paying a compact/copy on every single call for a marginal size
# reduction -- trimming in big, infrequent steps is cheaper overall than
# trimming a little every time).
RAW_BUFFER_HARD_CAP = 50_000_000  # native samples (~800MB) -- an extra, unconditional safety
# net on top of RAW_BUFFER_TRIM_INTERVAL's normal forward-progress-gated
# trimming. NOT a mathematical guarantee against unbounded growth: trimming
# can only discard data BEHIND the current search position, never the
# backlog AHEAD of it that a stuck search still needs to look at, so a
# single long-enough stuck stretch (search_start frozen while new samples
# keep arriving) still grows this buffer for as long as it's stuck --
# confirmed directly by testing this cap against exactly that scenario.
# What this DOES fix: reclaiming whatever's left over once search_start
# resumes advancing normally, on THIS buffer's own schedule rather than
# only whenever the slower periodic trim next happens to fire, keeping the
# steady-state size far below what let a real session reach 526M native
# samples (17.8GB) before a doubling-copy crashed the whole decode thread.


def get_rx_raw_incremental(reader, cache, trim_reference=None):
    """Returns the complex128 array of everything the reader has received
    so far, without re-joining/re-converting bytes already processed on a
    previous call -- unlike the naive `b"".join(reader.chunks)` +
    frombuffer this replaces, which redoes that full-buffer work on EVERY
    single retry. Confirmed for real to matter a lot: on a 64-fragment,
    ~14M-native-sample session, per-fragment compute grew to 1-2+ seconds
    by fragment 2 (against a ~55ms isolated-profile baseline) purely from
    reprocessing an ever-larger cumulative buffer from scratch every
    retry, not from any of the actually-incremental stages downstream
    (resampling, IQ correction, anti-alias filtering) that this was
    feeding fresh giant arrays into each time. `cache` should be a dict
    scoped to the whole session (persisted across fragments, not just
    retries within one), since it represents the cumulative stream.

    trim_reference: the current native_search_start (or None to disable
    trimming, e.g. for a bounded/non-live source that will never run long
    enough for this to matter). Everything before trim_reference minus a
    generous safety margin is physically discarded -- decode only ever
    searches FORWARD from native_search_start, never back into it, so
    this margin only needs to comfortably cover the small +-1/+-2 sample
    resync probe and one retry's worth of slack, not anything close to a
    whole fragment. This is what actually bounds this buffer's memory (at
    the NATIVE SDR rate -- often tens of times bigger than the same
    duration at cfg.fs, since resampling downward hasn't happened yet)
    for a live session of any length -- confirmed for real without this:
    a multi-minute session hit a single ~12s compute spike from one
    doubling-copy of an already many-hundred-MB buffer. See
    GrowableComplexArray.trim_before's own docstring for why this is
    safe to do here specifically (length/indices keep their existing
    meaning; only physical storage shrinks)."""
    with reader._lock:
        n_chunks_now = len(reader.chunks)
        new_chunks = reader.chunks[cache.get("n_chunks", 0):]
        # This is the decode loop's only steady consumer of the reader, so
        # it frees what it has taken (see ofdm.ChunkList).
        discard = getattr(reader.chunks, "discard_before", None)
        if discard:
            discard(n_chunks_now)
    if new_chunks:
        new_bytes = cache.get("leftover", b"") + b"".join(new_chunks)
        usable = len(new_bytes) - (len(new_bytes) % 8)
        if usable > 0:
            # Kept as complex64 (the SDR's own precision) rather than widened
            # to complex128: half the memory traffic through sync, CFO
            # correction and the FFTs, with float32 rounding far below the
            # signal's noise.
            new_arr = np.frombuffer(new_bytes[:usable], dtype=np.complex64)
            if not np.isfinite(new_arr).all():
                new_arr = np.where(np.isfinite(new_arr), new_arr, 0).astype(np.complex64)
            grown = cache.setdefault("grown", ofdm.GrowableComplexArray(np.complex64))
            # Safety-net trim BEFORE append, independent of the normal
            # periodic trim below -- that one only fires once
            # trim_reference (== search_start) has advanced far enough,
            # but search_start sits FROZEN for the whole duration of a
            # mismatch-retry storm or a long TX-restart recovery wait
            # (it only moves forward on an actual successful decode), while
            # this buffer keeps growing from newly-arrived raw samples the
            # entire time regardless. Confirmed for real: exactly that
            # combination let this buffer reach ~526M samples before a
            # doubling-copy tried to allocate 15.7GB and killed the whole
            # decode thread. Trimming to trim_reference here is still
            # correct even though that reference is "stale" during such a
            # wait -- decode only ever searches forward from wherever
            # search_start currently is, so anything before it (minus the
            # margin) is provably safe to discard no matter how long it's
            # been sitting there.
            if (trim_reference is not None and grown._buf is not None
                    and len(grown._buf) > RAW_BUFFER_HARD_CAP):
                grown.trim_before(trim_reference - RAW_BUFFER_TRIM_MARGIN)
                cache["last_trim_at"] = trim_reference
            grown.append(new_arr)
        cache["leftover"] = new_bytes[usable:]
        cache["n_chunks"] = n_chunks_now
    grown = cache.get("grown")
    if grown is None:
        return np.zeros(0, dtype=np.complex64)
    if trim_reference is not None:
        last_trim = cache.get("last_trim_at", 0)
        if trim_reference - last_trim >= RAW_BUFFER_TRIM_INTERVAL:
            grown.trim_before(trim_reference - RAW_BUFFER_TRIM_MARGIN)
            cache["last_trim_at"] = trim_reference
    return grown


CFO_EMA_ALPHA = 0.3  # weight given to each newly-confirmed fragment's CFO
                     # when updating the smoothed prior fed to the next
                     # fragment's resolve_integer_cfo/CFO_JUMP_LIMIT_HZ gate
                     # -- see its update site for why this is smoothed
                     # rather than just the latest confirmed value.
CFO_JUMP_LIMIT_HZ = 100.0  # a real oscillator can't jump by more than this
                           # between two back-to-back fragments; a bigger
                           # jump on a marginal-confidence lock is the
                           # signature of a spurious correlation peak on
                           # ordinary data content, not a real preamble --
                           # see the "last_cfo_hz" check below.


SLOW_STAGE_REPORT_THRESHOLD_S = 1.0


def _report_if_slow(stage_times):
    """Unconditional (not gated behind --verbose) per-stage timing
    breakdown for a single try_decode() call, printed only when the
    total is abnormally slow -- every multi-second stall found this
    session so far turned out to be a single unbounded-buffer stage
    once actually profiled, but each one needed a separate manual
    profiling session to pin down. This makes the next one (if there is
    one) self-diagnosing from a normal run's own stderr."""
    total = sum(stage_times.values())
    if total < SLOW_STAGE_REPORT_THRESHOLD_S:
        return
    breakdown = ", ".join(f"{name}={t:.2f}s" for name, t in stage_times.items())
    print(f"  ! slow decode ({total:.2f}s total) -- stage breakdown: {breakdown}", file=sys.stderr)


def _search_integer_cfo(cfg, rx, start_index, cfo_hz, prior_k, cfo_memo):
    """ofdm.resolve_integer_cfo on this lock (fractional CFO corrected),
    memoised per start_index (see try_decode). Only the preamble and header
    symbols are corrected -- all the search reads -- not the several frames'
    worth it used to be handed."""
    n_header = ofdm._header_symbol_plan(cfg)[-1] + 1
    seg = rx[start_index:start_index + cfg.preamble_len + n_header * cfg.symbol_len]
    after_preamble = ofdm.apply_cfo_correction(seg, cfo_hz, cfg.fs)[cfg.preamble_len:]
    k, confidence = ofdm.resolve_integer_cfo(cfg, after_preamble, prior_k=prior_k)
    if cfo_memo is not None:
        cfo_memo[start_index] = (k, confidence)
    return k, confidence


def try_decode(rx_raw_full, cfg, args, native_search_start=0, cache=None,
                resample_cache=None, iq_cache=None, lpf_cache=None, cfo_memo=None, last_cfo_hz=None,
                stage_totals=None):
    """One full sync+decode attempt for the CURRENT fragment, given ALL raw
    bytes read so far this session (rx_raw_full, at args.sample_rate or
    cfg.fs -- resampling to cfg.fs happens in here) and the native-domain
    sample index to start searching from (i.e. just past whatever prior
    fragments already consumed). Returns a dict with status
    ("ok"/"incomplete"/"failed") and, on "ok", payload/crc_ok/cfo_hz/ber/
    next_native_search_start.

    Always resamples from the FULL buffer's true start (never a trimmed
    slice) -- linear_resample's output at a given time only depends on
    nearby input samples (unlike fft_resample), so growing the buffer at
    the end never changes already-computed earlier samples, making this
    safe to keep doing every retry. Trimming the input before resampling
    was tried and reverted: it shifts WHERE each output sample's
    interpolation grid falls relative to the true signal by a fraction of
    a sample, and that's enough to occasionally corrupt a fragment's sync
    even at good SNR -- a real, reproduced bug, not a theoretical one.

    cache: an optional dict this function fills in and reuses across
    retries on the SAME fragment (while waiting for more of it to arrive),
    to avoid redoing the sync + integer-CFO search on every single retry.

    stage_totals: an optional dict (persisted by the CALLER across every
    mismatch-retry attempt for the same fragment, unlike `cache`/
    `cfo_memo` which are cleared between them -- see receive_one_
    fragment) that this call's own per-stage timing gets ADDED into.
    Needed because a mismatch-retry storm's real cost is spread across
    several separate try_decode() calls, each individually fast enough
    to hide from a per-call-only threshold check -- confirmed for real:
    a fragment with retries=3 and compute=1.90s total triggered no
    report at all under the original per-call version of this, since no
    single one of those 3-4 calls individually crossed 1s alone."""
    _t0 = time.time()

    def _mark(stage_name):
        nonlocal _t0
        now = time.time()
        if stage_totals is not None:
            stage_totals[stage_name] = stage_totals.get(stage_name, 0.0) + (now - _t0)
        _t0 = now

    # native_search_start is, DESPITE ITS NAME, actually an index into the
    # RESAMPLED (cfg.fs) signal -- it comes straight out of
    # schmidl_cox_sync's search over `rx` (see below), which only exists
    # after resampling. correct_iq_imbalance/apply_lowpass_filter_
    # incremental below operate on the TRUE native-rate buffer
    # (args.sample_rate, e.g. a PlutoSDR's own 550kHz vs. this mode's own
    # ~80-90kHz), so handing them native_search_start directly as their
    # trim_reference silently understates it by the resample ratio
    # whenever the two rates differ -- confirmed for real: trimming
    # firing ~7x less often than intended (matching a ~550/80 ratio) let
    # these two caches grow to sizes never exercised by this fix's own
    # verification test (which used matched-enough rates to not expose
    # the bug), producing exactly the growing multi-second "iq_correct"/
    # "lpf_resample" stalls the new stage-timing instrumentation caught.
    in_fs = args.sample_rate or cfg.fs
    native_trim_reference = int(native_search_start * in_fs / cfg.fs)

    # Correct I/Q gain/phase imbalance at the NATIVE rate, before any
    # resampling -- it's a property of the analog front-end (confirmed
    # for real via second-order I/Q statistics on a Pluto capture: ~27.5dB
    # image rejection, below what a well-calibrated radio should hit), so
    # it belongs at the point closest to where it was actually introduced.
    # (Skipped when the compiled front end is active: it already did this,
    # on the raw samples, and rx_raw_full is then its modem-rate output.)
    if not getattr(args, "frontend_active", False):
        rx_raw_full = ofdm.correct_iq_imbalance(rx_raw_full, cache=iq_cache,
                                                trim_reference=native_trim_reference)
    _mark("iq_correct")

    if in_fs != cfg.fs:
        # Reject out-of-band noise BEFORE resampling, at the native rate,
        # while there's still a wide gap between the capture bandwidth and
        # the signal's own occupied bandwidth to work with -- our causal
        # linear_resample trades away brick-wall filtering (see its
        # docstring), so without this, noise from well outside the signal
        # band aliases straight through instead of being rejected,
        # diluting the correlator's effective SNR at exactly the step
        # (sync acquisition) that's most sensitive to it. See
        # apply_lowpass_filter_incremental's docstring for the real
        # capture that confirmed this gap.
        # The carrier plan is NOT symmetric around DC (e.g. Mode B's
        # K=-99..311), so the cutoff has to cover whichever side extends
        # further from DC, not half the total span (which would clip the
        # wider side's edge carriers).
        #
        # A windowed-sinc filter's rolloff isn't a brick wall right at the
        # nominal cutoff -- a Hamming window's transition width is roughly
        # 3.3*fs/numtaps, so the point where the passband actually reaches
        # full gain sits BELOW the nominal cutoff by about half that. The
        # original 129-tap/1.15x-margin version put the true full-gain
        # edge just UNDER this mode's actual outermost carrier -- mildly
        # attenuating/distorting it on every symbol, not just during
        # acquisition. 129 taps at a correctly-sized 1.5x margin measures
        # out to unity gain (1.0006) right at the true edge -- same
        # compute cost as the original bug, just with the margin actually
        # sized correctly this time, rather than paying for 257 taps to
        # paper over an undersized margin.
        # The cutoff used to be 1.5x max(|data_bins|)*spacing, which is wrong
        # twice over: data_bins are the RAW (un-recentred) carrier indices,
        # but the transmitter recentres them about DC (see _fft_bins_for), so
        # the signal really occupies only +-max(|fft_bins|)*spacing (~9.6 kHz
        # for mode B/20 kHz, not 14.6 kHz); and 1.5x on top put the cutoff
        # (~21.9 kHz) far ABOVE the native rate's Nyquist (10.3 kHz).
        # linear_resample below has no anti-alias filtering of its own, so
        # all the noise between Nyquist and that cutoff folded straight into
        # the band -- about +3 dB of in-band noise. Measured on 243 B LDPC
        # frames in AWGN at 192 kHz input: 0% decoded at 4-5 dB and 10% at
        # 6 dB with the old cutoff, versus 82-95% at 4-6 dB with the cutoff
        # at the native Nyquist. The recentred signal always fits inside
        # cfg.fs/2 by construction, so that is the right cutoff for any mode.
        cutoff_hz = min(cfg.fs / 2, 0.45 * in_fs)
        if lpf_cache is not None:
            if "taps" not in lpf_cache:
                lpf_cache["taps"] = ofdm.design_lowpass_fir(cutoff_hz, in_fs, numtaps=129)
            rx_raw_full = ofdm.apply_lowpass_filter_incremental(rx_raw_full, lpf_cache["taps"], lpf_cache,
                                                                 trim_reference=native_trim_reference)
        # linear_resample_incremental's own trim_reference contract is
        # "an index in x's OWN (fs_in / native) domain" -- same
        # native_trim_reference as above, NOT native_search_start (which
        # is already in the cfg.fs/fs_out domain this function converts
        # TO internally; passing it here would apply that conversion an
        # extra, wrong time).
        rx = (ofdm.linear_resample_incremental(rx_raw_full, in_fs, cfg.fs, resample_cache,
                                                trim_reference=native_trim_reference)
              if resample_cache is not None else ofdm.linear_resample(rx_raw_full, in_fs, cfg.fs))
    else:
        rx = rx_raw_full
    _mark("lpf_resample")

    if len(rx) < native_search_start + cfg.preamble_len + cfg.symbol_len * 2:
        return {"status": "incomplete"}

    # apply_cfo_correction's cost is proportional to the length of
    # whatever it's given -- it was being handed rx[start_index:], i.e.
    # EVERYTHING from the lock point to the end of the whole (ever-
    # growing) resampled buffer, when every caller below only actually
    # needs at most this many samples. Confirmed for real via cProfile
    # against a real hardware capture: this one unbounded slice was
    # responsible for 8.3 of a 23s decode-time profile on its own, a cost
    # that (like the raw-buffer and IQ-imbalance issues fixed earlier
    # this session) grows with total session length even though nothing
    # about the actual DECODE needs more than a bounded window of data.
    if args.fragment_size:
        expected_payload = args.fragment_size + FRAG_HEADER.size
        max_wait_samples = min(int(cfg.fs * 300),
                                (ofdm.frame_symbol_count(cfg, expected_payload * 3) + 2) * cfg.symbol_len)
    else:
        max_wait_samples = int(cfg.fs * 300)
    max_needed_len = max_wait_samples + cfg.symbol_len + cfg.preamble_len

    if cache and "start_index" in cache:
        start_index, cfo_hz, extra_k, confidence = (
            cache["start_index"], cache["cfo_hz"], cache["extra_k"], cache["confidence"])
        assumed_k, prior_k = cache.get("assumed_k", False), cache.get("prior_k")
    else:
        # Bound the search the same way apply_cfo_correction's own slice
        # was bounded above -- schmidl_cox_sync defaults to scanning the
        # ENTIRE remaining buffer on every single call when search_len
        # isn't given, computing a correlation array over however much
        # has ever accumulated, not just the handful of frame-lengths
        # ahead any real candidate could plausibly be. Confirmed for
        # real: fixing prefer_earliest's own refine-window bug (see
        # schmidl_cox_sync's docstring) started finding every fragment
        # instead of skipping most of them, which paid this same
        # already-present unbounded-scan cost on nearly every fragment
        # instead of a small fraction of them -- turning a real but
        # previously-hidden inefficiency into an outright real-time
        # overrun.
        #
        # max_needed_len alone is too tight for this: the very first
        # search (native_search_start=0, no lock yet) may legitimately
        # need to look much further ahead than one frame -- e.g. past
        # leading dead air before the real signal starts, or (confirmed
        # for real, --input pipe reading a whole file at once) simply
        # because the true first lock sits tens of thousands of samples
        # in. Bounding to exactly one frame-length there starved
        # acquisition entirely (0 fragments decoded). Use whichever is
        # larger: that frame-based minimum, or a couple of this link's
        # OWN fragment times -- generous enough for genuine initial
        # acquisition and an ordinary retry, while far cheaper than a
        # flat 5 real seconds regardless of fragment size. That flat 5s
        # used to dominate schmidl_cox_sync's own cost on every retry
        # (confirmed for real via profiling: sync/CFO search was 73-81%
        # of total decode time, and this unbounded-relative-to-fragment-
        # duration search window was the biggest single piece of it) --
        # at a short fragment duration, 5s could be 20+ fragment-times'
        # worth of samples to correlate over for what's usually just a
        # brief, nearby re-acquisition.
        if args.fragment_size:
            one_frame_len = ofdm.frame_symbol_count(cfg, expected_payload) * cfg.symbol_len + cfg.preamble_len
            generous_floor = 2 * one_frame_len
        else:
            generous_floor = int(cfg.fs * 5)
        search_len_bound = max(max_needed_len, generous_floor)
        # Fast path: while locked, the next preamble starts right where the
        # last frame ended -- at the very start of this window -- yet the
        # full window is several frames long and its correlation was ~15%
        # of decode time (more at 320 kHz, where frames are short). Search
        # SYNC_FAST_SYMBOLS first; prefer_earliest makes the answer
        # identical to the full search whenever it finds the preamble early
        # enough to leave its whole refine window (4 symbols) inside.
        metric = -1.0
        fast_len = SYNC_FAST_SYMBOLS * cfg.symbol_len + cfg.preamble_len - cfg.symbol_len
        if 0 < fast_len < search_len_bound and len(rx) - native_search_start >= fast_len + cfg.n_fft:
            start_index, cfo_hz, metric = ofdm.schmidl_cox_sync(
                rx, cfg, search_start=native_search_start, search_len=fast_len, prefer_earliest=True)
            if start_index + cfg.n_cp - native_search_start > fast_len - 4 * cfg.symbol_len:
                metric = -1.0  # too near the end to be sure: full search
        if metric < ofdm.SYNC_METRIC_MIN:
            start_index, cfo_hz, metric = ofdm.schmidl_cox_sync(
                rx, cfg, search_start=native_search_start, search_len=search_len_bound, prefer_earliest=True)
        if metric < ofdm.SYNC_METRIC_MIN:
            # "incomplete" (wait for more data, then re-scan) is only
            # correct while the buffer genuinely hasn't filled this whole
            # bounded window yet -- once it has, and STILL nothing scored
            # above threshold anywhere in it, there is truly no signal in
            # this stretch, and this call needs to say so instead of
            # returning "incomplete" again. Confirmed for real this was a
            # genuine hang, not a deadlock: with no signal for a long
            # stretch (a TX restart's dead-air gap), native_search_start
            # never advances on "incomplete", so every subsequent call
            # re-scans the EXACT SAME ~5s window -- an unproductive
            # cumsum over the same few hundred thousand samples, over and
            # over, as fast as the CPU allows, forever. run_decode_loop's
            # own "5 consecutive failures -> clear stale CFO prior"
            # recovery (added earlier this session for exactly this TX-
            # restart scenario) never even runs, because this function
            # never returns to it at all -- looks identical to a frozen
            # process from the outside. Advancing past this whole
            # scanned-and-empty window and reporting a real "failed"
            # lets both that recovery AND ordinary forward progress
            # happen once a fragment is truly this far behind.
            L = cfg.n_fft // 2
            max_search_len = len(rx) - native_search_start - 2 * L
            if max_search_len >= search_len_bound:
                return {"status": "failed",
                        "next_native_search_start": native_search_start + search_len_bound}
            return {"status": "incomplete"}

        # A real narrowband interferer/spur can produce a moderate, STABLE
        # false correlation across a WIDE span of search_start positions
        # (confirmed for real: the identical start_index rediscovered
        # across 5+ consecutive escalating retries before finally being
        # escaped) -- schmidl_cox_sync keeps re-landing on the exact same
        # samples every time, so the 61-candidate CFO search below would
        # otherwise redo identical, deterministic work on every one of
        # those retries. Memoize by start_index (scoped to this fragment's
        # own attempt, not persisted across fragments -- a stale start_
        # index from an EARLIER fragment could coincidentally recur and
        # shouldn't reuse an unrelated result) to pay for it once.
        prior_k = (round((cfo_hz - last_cfo_hz) / cfg.carrier_spacing)
                   if last_cfo_hz is not None else None)
        assumed_k = False
        if cfo_memo is not None and start_index in cfo_memo:
            extra_k, confidence = cfo_memo[start_index]
        elif prior_k is not None and ASSUME_PRIOR_INTEGER_CFO:
            # Locked onto a steady carrier: the last fragment's integer CFO
            # is almost always this one's too (a real oscillator drifts a
            # few Hz between fragments, a carrier spacing is tens of Hz), so
            # go straight to decode_frame with it -- its own header checksum
            # and the payload CRC check it -- and only run the candidate
            # search (and the CFO correction feeding it) if that decode
            # fails. The search was ~30% of the decode thread at 320 kHz.
            extra_k, confidence, assumed_k = prior_k, 1.0, True
        else:
            # Schmidl & Cox above only resolved the FRACTIONAL part of the
            # CFO (within +-0.5 carrier spacing) -- a real oscillator
            # offset of many carrier spacings (e.g. an intentionally-
            # detuned RX to dodge its own DC spike) leaves the whole
            # constellation sitting on the wrong carriers even after that
            # correction. Search integer multiples of carrier_spacing and
            # pick whichever gives the most self-consistent header before
            # committing to a full decode.
            # A real oscillator's CFO drifts smoothly fragment to fragment
            # (see CFO_JUMP_LIMIT_HZ above) -- when we know the last
            # CONFIRMED total CFO, derive the extra_k candidate that would
            # reproduce it and use it as a search prior (see
            # resolve_integer_cfo's docstring) rather than treating all 61
            # candidates as equally likely a priori.
            extra_k, confidence = _search_integer_cfo(cfg, rx, start_index, cfo_hz, prior_k, cfo_memo)
        total_cfo_hz = cfo_hz - extra_k * cfg.carrier_spacing
        if args.verbose:
            print(f"  [sync] search_start={native_search_start} start_index={start_index} "
                  f"metric={metric:.3f} frac_cfo={cfo_hz:+.1f}Hz "
                  f"integer_cfo={extra_k:+d}x{cfg.carrier_spacing:.1f}Hz "
                  f"header_confidence={confidence:.2f}", file=sys.stderr)

        # A real oscillator's CFO drifts smoothly fragment to fragment; a
        # marginal-confidence lock whose resolved CFO jumps by hundreds of
        # Hz from the last CONFIRMED fragment is a spurious correlation
        # peak on ordinary data content, not a real preamble -- confirmed
        # for real on capture logs (integer_cfo of +19x/-21x/+15x etc.
        # alongside confidence 0.68-0.78, vs. a stable +2x/+0x for every
        # genuine lock in the same run). Failing this immediately (no
        # decode_frame call, no retries against more buffered data --
        # more data won't change a deterministic sync result) is both
        # more correct and much faster than the previous behavior of
        # burning up to 8 retries and a full header decode on every one.
        if (last_cfo_hz is not None and confidence < 0.95
                and abs(total_cfo_hz - last_cfo_hz) > CFO_JUMP_LIMIT_HZ):
            if args.verbose:
                print(f"  [sync] REJECTED: CFO {total_cfo_hz:+.1f}Hz implausibly far from "
                      f"last confirmed {last_cfo_hz:+.1f}Hz at this confidence -- "
                      f"spurious lock, not retrying", file=sys.stderr)
            return {"status": "failed"}

        # NOTE: this used to hard-reject any lock with confidence below 0.9,
        # based on an analysis that low confidence correlated with 10-60%
        # of a fragment's points getting blown up by zero-forcing. That
        # correlation turned out to be a symptom of a real bug elsewhere
        # (estimate_and_equalize interpolating h's real/imaginary parts
        # separately instead of magnitude+phase, which fabricated fake
        # deep fades whenever the true channel's phase rotated across
        # carriers -- see its docstring), not something confidence itself
        # was reliably measuring. With that fixed, a genuinely low-but-real
        # confidence candidate can decode fine, and gating on it here
        # was permanently skipping recoverable fragments (confirmed for
        # real: two fragments in one transfer were found at the right
        # position with confidence 0.70 and never even attempted).
        # decode_frame's own payload_len sanity check plus the CRC already
        # protect against genuine false locks, so there's no need to
        # pre-reject here at all -- just let a bad one fail downstream.

        if cache is not None:
            cache.update(start_index=start_index, cfo_hz=cfo_hz, extra_k=extra_k, confidence=confidence,
                         assumed_k=assumed_k, prior_k=prior_k)
    _mark("sync_cfo")
    sync_start_index = start_index  # before refine_frame_start: what the CFO search keys on

    def decode_with(k):
        total = cfo_hz - k * cfg.carrier_spacing
        # Schmidl & Cox only locates the preamble to within its ~n_cp-wide
        # plateau at low SNR (sometimes outside the CP entirely); sharpen it
        # against the known preamble waveform. See ofdm.refine_frame_start.
        refined = ofdm.refine_frame_start(cfg, rx, sync_start_index, total)
        if args.verbose and refined != sync_start_index:
            print(f"  [sync] timing refined by {refined - sync_start_index:+d} samples "
                  f"({sync_start_index} -> {refined})", file=sys.stderr)
        after = ofdm.apply_cfo_correction(rx[refined:refined + max_needed_len], total, cfg.fs)[cfg.preamble_len:]
        # max_wait_samples (see its computation near the top of this
        # function) doubles as this frame-length sanity cap: a false lock's
        # garbage header can still parse to a "sane" (small enough)
        # payload_len that implies an absurdly long frame -- observed for
        # real, one implying ~5x the entire capture's length, which would
        # otherwise stall reception forever waiting for data that will
        # never arrive.
        return refined, total, ofdm.decode_frame(cfg, after, verbose=args.verbose,
                                                 max_wait_samples=max_wait_samples)

    start_index, total_cfo_hz, (payload, crc_ok, incomplete, ber, evm, constellation) = decode_with(extra_k)
    if assumed_k and not incomplete and (payload is None or not crc_ok):
        # The assumed (previous fragment's) integer CFO didn't decode: run
        # the real candidate search, exactly as without the shortcut, and
        # decode again only if it picks something else.
        real_k, confidence = _search_integer_cfo(cfg, rx, sync_start_index, cfo_hz, prior_k, cfo_memo)
        if cache is not None:
            cache.update(extra_k=real_k, confidence=confidence, assumed_k=False)
        if real_k != extra_k:
            if (last_cfo_hz is not None and confidence < 0.95
                    and abs((cfo_hz - real_k * cfg.carrier_spacing) - last_cfo_hz) > CFO_JUMP_LIMIT_HZ):
                return {"status": "failed"}  # spurious lock (see the same gate above)
            extra_k = real_k
            start_index, total_cfo_hz, (payload, crc_ok, incomplete, ber, evm, constellation) = \
                decode_with(extra_k)
    _mark("decode_frame")
    if payload is None:
        return {"status": "incomplete" if incomplete else "failed"}

    # There's no return channel to ask for a retransmit, so a CRC failure
    # on a genuinely-locked fragment (real integer_cfo, high confidence --
    # not the false-lock case handled above) is the last chance to recover
    # it. Schmidl & Cox only estimates start_index to the nearest sample;
    # being off by 1-2 samples still passes the correlation threshold but
    # shifts every symbol's FFT window slightly off the true boundary,
    # which can be enough residual ISI to push a handful of soft bits past
    # the Viterbi decoder's error-correcting capacity. Nudging the sync
    # point by a few samples and re-running the SAME captured data costs
    # microseconds next to the round-trip a retransmit would need (which
    # doesn't exist here anyway), and has recovered real fragments that
    # were otherwise unrecoverable from the same samples.
    if not crc_ok and confidence >= 0.95:
        # A plain +-2-sample probe (each paying for a FULL decode_frame --
        # a whole-payload Viterbi decode) only ever covered a tiny window,
        # missing a real, confirmed failure mode: being off by close to a
        # full CP length (n_cp) from the true optimal alignment can still
        # pass the sync threshold (even score HIGHER there) yet decode
        # with a bursty, uncorrectable error pattern, while an offset a
        # couple hundred samples away -- a real CP-length sync ambiguity,
        # not a hardware fault -- decodes cleanly. Covering that whole
        # range by trying a full decode_frame at every candidate would be
        # expensive; resolve_timing_offset scores every candidate using
        # only the header's own soft Viterbi metric (header-sized, far
        # cheaper than the whole payload) exactly the way
        # resolve_integer_cfo already does for CFO candidates, so the
        # WIDE search stays cheap and only the single winning offset ever
        # pays for a real decode_frame call.
        step = max(1, cfg.n_cp // 24)
        candidate_deltas = [d for d in range(-cfg.n_cp, cfg.n_cp + 1, step) if d != 0]
        best_delta, _, header_ok = ofdm.resolve_timing_offset(
            cfg, rx, start_index, total_cfo_hz, max_needed_len, candidate_deltas)
        if header_ok and best_delta != 0:
            probe_start = start_index + best_delta
            probe_after = ofdm.apply_cfo_correction(
                rx[probe_start:probe_start + max_needed_len], total_cfo_hz, cfg.fs)[cfg.preamble_len:]
            probe = ofdm.decode_frame(cfg, probe_after, verbose=False, max_wait_samples=max_wait_samples)
            if probe[0] is not None and (probe[1] or ber is None or (probe[3] is not None and probe[3] < ber)):
                payload, crc_ok, incomplete, ber, evm, constellation = probe
    _mark("nudge_retry")

    n_symbols = ofdm.frame_symbol_count(cfg, len(payload))
    next_native_search_start = start_index + cfg.preamble_len + n_symbols * cfg.symbol_len

    return {"status": "ok", "payload": payload, "crc_ok": crc_ok, "cfo_hz": total_cfo_hz,
            "ber": ber, "evm": evm, "constellation": constellation,
            "next_native_search_start": next_native_search_start,
            "frame_start_index": start_index}


def receive_one_fragment(cfg, args, reader, state):
    """Reads more of stdin (via the shared background `reader`) until one
    fragment's frame is fully decoded, fails for real, or stdin closes.
    `state["native_search_start"]` is the native-domain sample index prior
    fragments have already consumed up to (see try_decode) -- it only ever
    advances, and the FULL raw buffer is always resampled from its true
    start (see try_decode's docstring for why). Returns a try_decode-style
    result dict, plus {"status": "eof"} if stdin closed with nothing more
    to try."""
    live_native_fs = args.sample_rate or cfg.fs
    live_L = max(4, int(round(cfg.n_fft / 2 * live_native_fs / cfg.fs)))
    live_tail = np.zeros(0, dtype=np.complex64)
    live_n_chunks_scanned = 0
    live_leftover = b""
    # Same anti-alias filtering try_decode applies before resampling (see
    # its docstring) -- this pre-lock scan works directly on the native-
    # rate stream and would otherwise miss out on it entirely, silently
    # needing a much stronger real signal to ever cross the lock
    # threshold than try_decode needs once locked. Confirmed for real:
    # winding RX gain up to acquire, then back down to a level that
    # never acquires from cold, still kept decoding fine -- proving the
    # decode path was fine at that gain all along and this SEPARATE
    # unfiltered detection path was the actual bottleneck.
    live_lpf_taps = None
    live_lpf_state = {}
    if live_native_fs != cfg.fs:
        # Same cutoff as try_decode's filter (see the long comment there): the
        # old 1.5x max(|data_bins|) put it ~2x above the native Nyquist.
        live_lpf_taps = ofdm.design_lowpass_fir(min(cfg.fs / 2, 0.45 * live_native_fs), live_native_fs,
                                                numtaps=129)
    # A prior fragment's over-read (the reader thread runs ahead independent
    # of decode progress, so there's often already plenty buffered) may have
    # already left enough buffered data to decode THIS fragment immediately
    # -- so we're "locked" from the start if so, and must try that data
    # before ever waiting for more (stdin could even be at EOF already while
    # unconsumed data is sitting right there).
    locked = reader.total_len > 0
    sync_cache = {}
    last_bad_position = None  # see the mismatch-retry branch below
    resample_cache = state.setdefault("resample_cache", {})
    iq_cache = state.setdefault("iq_cache", {})
    lpf_cache = state.setdefault("lpf_cache", {})
    rx_raw_cache = state.setdefault("rx_raw_cache", {})
    last_cfo_hz = state.get("last_cfo_hz")
    mismatch_result = None
    mismatch_retries = 0
    MAX_MISMATCH_RETRIES = 8
    # Compute cap on retrying a real frame that decoded with a bad CRC: one
    # retry often rescues a marginal fragment (a different sync candidate),
    # but stacking up to 8 full decodes on one stuck fragment (seen: 0.85-1.1s
    # of compute against a 0.24s fragment) puts the receiver behind real time
    # and costs the fragments after it. Cheap "no signal" scanning retries
    # (status "failed") are exempt -- they need the full count.
    retry_compute_budget_s = None
    # HF_OFDM_RX_WAIT_EOF (test-only, see run_decode_loop): no time budget, so
    # the number of retries -- and so the result -- depends only on the input,
    # not on how fast this machine or this version of the code happens to be.
    if args.fragment_size and not os.environ.get("HF_OFDM_RX_WAIT_EOF"):
        _n_sym = ofdm.frame_symbol_count(cfg, args.fragment_size + FRAG_HEADER.size)
        retry_compute_budget_s = 1.5 * _n_sym * cfg.symbol_len / cfg.fs
    search_start = state["native_search_start"]
    cfo_memo = {}
    stage_totals = {}  # accumulates across every mismatch-retry attempt below -- see try_decode's docstring
    compute_time = 0.0  # cumulative time actually spent IN try_decode across
                         # every attempt (incl. retries) -- excludes time
                         # blocked on reader.wait_for_more(), so this is the
                         # part elapsed-vs-budget can't distinguish on its
                         # own: elapsed also includes real waiting for the
                         # fragment's own audio to arrive, which is an
                         # unavoidable floor, not something to optimize.
    # Counts surfaced in the per-fragment status line so a slow fragment's
    # cause is visible directly instead of guessed at: a high retry/wait
    # count means the channel itself is generating lots of false starts
    # (a hard SNR problem, not a computational one -- confirmed for real
    # by profiling every relevant decode stage at ~55ms total, nowhere
    # near enough to explain multi-second overshoots seen on a difficult
    # link), while a low count with a slow time points elsewhere.
    wait_count = 0

    def _annotate(result):
        # NOTE: this used to also report an "input_starved_frac" here,
        # comparing bytes actually received during this call against how
        # many a steady native-rate stream should have delivered in that
        # wall-clock time. Retracted: confirmed for real that it was a
        # false positive -- it flagged a real run as short on input bytes
        # while pluto_loopback.py's own console showed zero libiio/
        # SoapySDR 'O'/'U' overflow markers (the actual, authoritative
        # signal for real hardware sample drops) anywhere near the same
        # time. Windows pipes/thread scheduling can deliver data in
        # bursts rather than perfectly steady pacing with zero true loss,
        # which this metric couldn't tell apart from real loss -- not
        # reliable enough to report as a finding.
        result["retries"] = mismatch_retries
        result["waits"] = wait_count
        result["compute_time"] = compute_time
        _report_if_slow(stage_totals)
        return result

    while True:
        snapshot_len = None  # buffer length try_decode's data was taken at -- see prev_len below
        while locked:
            # Snapshot BEFORE the decode reads the buffer: anything that arrives
            # after this point was not necessarily seen by try_decode, so the
            # wait/EOF logic below must compare against THIS length, not a fresh
            # reader.total_len read after try_decode returns "incomplete".
            snapshot_len = reader.total_len
            # search_start (== native_search_start passed to try_decode
            # below) is, despite the name, in the RESAMPLED (cfg.fs)
            # domain -- see try_decode's own matching comment. This raw
            # buffer lives at the TRUE native rate (live_native_fs), so
            # trimming it needs the same domain conversion try_decode
            # already applies for its own native-rate caches. Missing
            # this here specifically crashed for real: trimming fired so
            # rarely that the raw buffer grew to ~597M samples (17.8GB)
            # before a doubling-copy tried to allocate that much and
            # raised ArrayMemoryError, killing the whole decode thread.
            raw_trim_reference = int(search_start * live_native_fs / cfg.fs)
            rx_raw = get_rx_raw_incremental(reader, rx_raw_cache, trim_reference=raw_trim_reference)

            _t_compute0 = time.time()
            result = try_decode(rx_raw, cfg, args, native_search_start=search_start, cache=sync_cache,
                                 resample_cache=resample_cache, iq_cache=iq_cache, lpf_cache=lpf_cache,
                                 cfo_memo=cfo_memo, last_cfo_hz=last_cfo_hz, stage_totals=stage_totals)
            compute_time += time.time() - _t_compute0
            if result["status"] in ("ok", "failed") and (result["status"] == "failed" or not result["crc_ok"]):
                # A CRC mismatch or a header that fails its own sanity check
                # means decode_frame ran to completion (it had >= the
                # samples it asked for) and the result is what it is --
                # deterministic given this exact sync point, so retrying
                # with MORE buffered data changes nothing (observed for
                # real: byte-identical CRC across 6+ retries). Treat it the
                # same as a false lock: clear the cache and advance
                # search_start to try a genuinely different candidate.
                if (result["status"] == "ok" and result.get("ber") is not None
                        and result["ber"] >= ofdm.HOPELESS_BER):
                    # A genuine frame (header checksum and length were fine)
                    # decoded far below the FEC's capability: a different
                    # sync candidate won't change that, and every retry
                    # costs a full sync+decode -- enough, on a run of weak
                    # fragments, to fall behind real time and lose good ones.
                    return _annotate(result)
                mismatch_retries += 1
                mismatch_result = result
                # Capture exactly where THIS attempt's sync locked (set
                # unconditionally in try_decode right after sync resolves,
                # before decode_frame even runs) before clearing it --
                # needed below to tell a genuinely NEW mismatch apart from
                # re-finding the identical false lock again.
                bad_position = sync_cache.get("start_index")
                sync_cache.clear()
                # schmidl_cox_sync itself already does a full forward scan
                # and returns the NEAREST qualifying candidate from
                # wherever it's told to start -- so a small nudge just past
                # the position that just failed lets IT find whatever the
                # true next candidate actually is, at whatever distance,
                # instead of us guessing a jump size and risking a real,
                # nearby, perfectly decodable frame getting skipped over
                # entirely (confirmed for real: exactly this cost a fully
                # recoverable fragment -- CRC-clean, BER=0.0002 when
                # decoded directly -- because an earlier fixed-size jump
                # landed past it, and schmidl_cox_sync never looks
                # backward). Only escalate to a bigger jump once we see
                # the SAME exact position come back again, which is the
                # actual signature of a genuine wide false lock (a real
                # narrowband spur producing a stable false correlation
                # across a wide span) rather than an ordinary one-off
                # mismatch -- that's the one case a small nudge alone
                # would otherwise waste many retries re-finding.
                if bad_position is not None and bad_position == last_bad_position:
                    step_symbols = 8
                    if args.fragment_size:
                        frame_len_symbols = ofdm.frame_symbol_count(
                            cfg, args.fragment_size + FRAG_HEADER.size)
                        step_symbols = min(8, max(1, frame_len_symbols))
                    search_start = bad_position + cfg.symbol_len * step_symbols
                elif bad_position is not None:
                    search_start = bad_position + cfg.n_fft // 4
                else:
                    # No exact position on record (e.g. the CFO-continuity
                    # gate rejected before ever caching one) -- fall back
                    # to a small, harmless step past the current search
                    # point rather than not moving at all.
                    search_start += cfg.n_fft // 4
                last_bad_position = bad_position
                over_budget = (retry_compute_budget_s is not None and result["status"] == "ok"
                               and compute_time > retry_compute_budget_s)
                if mismatch_retries >= MAX_MISMATCH_RETRIES or over_budget:
                    # "failed" results have no next_native_search_start of
                    # their own (no frame was ever successfully parsed);
                    # "ok"-but-mismatched ones already computed a real one
                    # from actual symbol counts, which is more accurate
                    # than this coarse bump -- don't clobber it.
                    mismatch_result.setdefault("next_native_search_start", search_start)
                    return _annotate(mismatch_result)
                # Retry immediately against whatever's ALREADY buffered --
                # only block on a fresh (real-time-paced) stdin read once
                # that's actually insufficient for the new search_start.
                # Blocking here every retry regardless was the other half
                # of the observed multi-second stalls: each pointless
                # retry-with-more-data still waited out a real ~0.7s of
                # live stream arrival for no benefit.
                continue
            elif result["status"] != "incomplete":
                return _annotate(result)
            break  # "incomplete": fall through to read more from stdin

        # Race fixed here: this used to always be a fresh reader.total_len. If the
        # reader thread pulled in more data (even the rest of the file, ending in
        # EOF) between try_decode's snapshot and this line, wait_for_more returned
        # at once and the EOF check below fired -- returning "eof" without ever
        # decoding the data that had just arrived. Observed for real when
        # replaying a file: the identical file decoded 16/16 fragments on one run
        # and 0 on the next. Comparing against the pre-decode snapshot makes the
        # newly arrived data count as "more", so the loop retries try_decode.
        prev_len = snapshot_len if snapshot_len is not None else reader.total_len
        reader.wait_for_more(prev_len)
        wait_count += 1

        if not locked:
            # Scan whatever's new (even the final batch right before EOF --
            # a signal could be sitting in it) before deciding there's
            # nothing left to do. Only join/convert chunks NOT already
            # processed (tracked by list index, not byte offset) -- same
            # fix as get_rx_raw_incremental, and matters here too: a slow
            # pre-acquisition phase (seen for real taking 100+ waits) would
            # otherwise re-join the whole growing chunk list from scratch
            # on every single one of them.
            with reader._lock:
                n_chunks_now = len(reader.chunks)
                new_chunks = reader.chunks[live_n_chunks_scanned:]
            live_n_chunks_scanned = n_chunks_now
            new_bytes = live_leftover + b"".join(new_chunks)
            usable = len(new_bytes) - (len(new_bytes) % 8)
            live_leftover = new_bytes[usable:]
            chunk_c = np.frombuffer(new_bytes[:usable], dtype=np.complex64)
            chunk_c = np.where(np.isfinite(chunk_c), chunk_c, 0).astype(np.complex64)
            if live_lpf_taps is not None:
                chunk_c = ofdm.stream_lowpass_filter(chunk_c, live_lpf_taps, live_lpf_state)
            segment = np.concatenate([live_tail, chunk_c])
            _, live_metric = ofdm.quick_sc_scan(segment, live_L)
            live_tail = segment[-2 * live_L:] if len(segment) >= 2 * live_L else segment
            if live_metric > ofdm.SYNC_METRIC_MIN:
                locked = True
                print(f"Signal acquired (metric={live_metric:.3f}), decoding...", file=sys.stderr)

        if reader.eof and reader.total_len <= prev_len:
            # Genuinely nothing more arrived and the stream is closed.
            if mismatch_result is not None:
                mismatch_result.setdefault("next_native_search_start", search_start)
                return _annotate(mismatch_result)
            return _annotate({"status": "eof"})


def _retry_note(result):
    """Formats the retry/wait counts onto a status line, so a slow or
    failed fragment's cause is visible directly -- a lot of retries means
    the channel itself is generating false starts (an SNR problem, not a
    computational one: every relevant decode stage profiled at ~55ms
    total, nowhere near enough to explain a multi-second overshoot on a
    difficult link)."""
    retries = result.get("retries", 0)
    waits = result.get("waits", 0)
    note = f"  [retries={retries} waits={waits}]" if (retries or waits) else ""
    compute = result.get("compute_time")
    if compute is not None:
        note += f"  compute={compute:.2f}s"
    return note


def _peek_latest_samples(reader, n_samples):
    """Thread-safe peek at roughly the most recent n_samples complex64 IQ
    samples the reader has received, for the --gui spectrum display only
    -- never touches decode state. Safe to call concurrently with the
    decode loop's own independent consumption of the same reader.chunks:
    both are pure readers of an append-only list (chunks are only ever
    appended, never removed/mutated), the same reasoning that already
    lets the pre-lock quick-scan and post-lock decode paths both read it
    independently elsewhere in this file."""
    with reader._lock:
        chunks = reader.chunks[-64:]
    data = b"".join(chunks)
    needed_bytes = n_samples * 8  # complex64 = 8 bytes/sample
    if len(data) > needed_bytes:
        data = data[-needed_bytes:]
    usable = len(data) - (len(data) % 8)
    if usable <= 0:
        return np.zeros(0, dtype=np.complex64)
    return np.frombuffer(data[-usable:], dtype=np.complex64).copy()


# 1024 bins is still more points than the GUI's plot is pixels wide (it
# was 4096, redrawn ~7x/s: costly on a Raspberry Pi for a monitoring view).
SPECTRUM_FFT_SIZE = 1024
# Per-radio spectrum display offset (dB), so different radios' plots read alike.
SPECTRUM_OFFSET_DB = {"rtlsdr": -50.0}


def _build_spectrum_computer(cfg, args, reader):
    """Shared by run_gui's own QTimer-driven spectrum widget and
    --spectrum-stderr's plain background thread (see run_spectrum_stderr)
    -- decimation filter design, FFT windowing/averaging and the
    frequency axis are identical either way, only WHERE the result goes
    differs. Returns (freqs, compute), where compute() returns a
    magnitude-dB array over freqs, or None if not enough samples have
    accumulated yet."""
    FFT_SIZE = SPECTRUM_FFT_SIZE
    SPECTRUM_AVERAGES = args.spectrum_averages
    # --spectrum-averages-file: a new value from the GUI applies live (one
    # stat() per spectrum update, ~4 a second)
    avg_state = {"key": None, "n": SPECTRUM_AVERAGES}

    def _live_averages():
        path = getattr(args, "spectrum_averages_file", None)
        if path:
            try:
                st = os.stat(path)
                key = (st.st_mtime_ns, st.st_size)
                if key != avg_state["key"]:
                    avg_state["key"] = key
                    with open(path) as f:
                        avg_state["n"] = max(1, min(256, int(f.read().strip())))
            except (OSError, ValueError):
                pass
        return avg_state["n"]
    fs = args.sample_rate or cfg.fs
    span = getattr(args, "spectrum_span_hz", 0.0) or 0.0
    source = reader
    lo_shift = None
    if span > fs and hasattr(reader, "_inner") and hasattr(reader, "_fe"):
        # Wider than the modem rate: use the raw SDR samples the front end
        # reads (it keeps the most recent chunks for exactly this kind of
        # peek), shifted by the front end's own LO table so the signal is
        # centred as in the decoder's view. Skips the front end's IQ
        # correction -- fine for a display.
        source = reader._inner
        fs = reader._fe.in_fs
        lo_shift = (reader._fe.lo_table, reader._fe.lo_offset_hz)

    # Decimate the spectrum feed down to ~2x the signal's own occupied
    # bandwidth before FFTing it. Two wins from this: FFT_SIZE bins now
    # span a much narrower slice of spectrum (same bin count, less
    # bandwidth-per-bin = finer resolution ON the actual signal, instead
    # of most of those bins being spent on empty band the SDR's full
    # sample rate covers but the signal doesn't use), and the per-update
    # anti-alias filter is far cheaper than an equivalently-fine FFT
    # would be directly at full rate. A short windowed-sinc FIR
    # (cutoff at the new Nyquist) runs before the decimating stride so
    # out-of-band energy folds out instead of aliasing into the display.
    occupied_bw = cfg.carrier_spacing * (int(np.max(cfg.data_bins)) - int(np.min(cfg.data_bins)) + 1)
    if lo_shift is not None:
        decim = max(1, int(fs // span))
    else:
        decim = max(1, int(fs // (2 * occupied_bw))) if occupied_bw > 0 else 1
    disp_fs = fs / decim
    if decim > 1:
        taps = 8 * decim + 1  # a few sidelobes' worth per decimated sample is plenty for a monitoring display
        n = np.arange(taps) - taps // 2
        cutoff = 1.0 / decim  # normalized to the ORIGINAL fs/2
        aa_kernel = np.sinc(cutoff * n) * cutoff
        aa_kernel *= np.hanning(taps)
        aa_kernel /= aa_kernel.sum()
        print(f"Spectrum: decimating {fs/1e3:.1f}kHz -> {disp_fs/1e3:.1f}kHz "
              f"(occupied bandwidth ~{occupied_bw/1e3:.1f}kHz)", file=sys.stderr)
    else:
        aa_kernel = None
    raw_needed = FFT_SIZE * decim + (len(aa_kernel) - 1 if aa_kernel is not None else 0)
    freqs = np.fft.fftshift(np.fft.fftfreq(FFT_SIZE, d=1.0 / disp_fs))
    # Decimation is by a whole number, so the display rate can overshoot the
    # requested span (550 kS/s / 4 = 137.5 kHz for 120): trim to the span.
    keep = slice(None)
    if lo_shift is not None:
        idx = np.nonzero(np.abs(freqs) <= span / 2)[0]
        if len(idx) > 1:
            keep = slice(int(idx[0]), int(idx[-1]) + 1)
            freqs = freqs[keep]

    # Averaged in LINEAR power (not dB -- averaging dB values directly is
    # not the same as averaging power, and skews the result), over the
    # last SPECTRUM_AVERAGES frames -- smooths the noisy per-frame FFT
    # into something actually readable, same as any real spectrum
    # analyzer's averaging mode.
    power_history = []
    # Display only: radios scale their samples differently. An RTL-SDR's
    # 8-bit samples sit near full scale (~50 dB above a PlutoSDR's at the
    # same signal), which read as positive dB; shift it to read alike.
    display_offset_db = SPECTRUM_OFFSET_DB.get(getattr(args, "sdr", "pluto"), 0.0) \
        if getattr(args, "input", "pipe") == "pluto" else 0.0

    def compute():
        samples = _peek_latest_samples(source, raw_needed)
        if len(samples) < raw_needed:
            return None
        if lo_shift is not None:
            table, lo_hz = lo_shift
            if table is not None:
                samples = samples * np.resize(table, len(samples))
            elif lo_hz:
                samples = samples * np.exp(2j * np.pi * lo_hz * np.arange(len(samples)) / fs)
        if aa_kernel is not None:
            samples = np.convolve(samples, aa_kernel, mode="valid")[::decim]
        windowed = samples[-FFT_SIZE:] * np.hanning(FFT_SIZE)
        spectrum = np.fft.fftshift(np.fft.fft(windowed))
        power_history.append(np.abs(spectrum) ** 2)
        averages = _live_averages()
        while len(power_history) > averages:
            power_history.pop(0)
        avg_power = np.mean(power_history, axis=0)
        return 10 * np.log10(avg_power[keep] + 1e-12) + display_offset_db

    return freqs, compute


def run_spectrum_stderr(cfg, args, reader):
    """--spectrum-stderr: for an external GUI (media_rx_gui.py's combined
    window) that wants to draw its OWN spectrum widget instead of this
    script opening one -- periodically prints a spectrum snapshot to
    stderr as a single tagged line, then runs the ordinary (non-GUI)
    decode loop directly on this thread. No Qt/pyqtgraph dependency in
    this mode at all.

    Line format: "[spectrum] <freq0_hz> <freq_step_hz> <n_points> <b64>"
    where <b64> base64-decodes to n_points little-endian float32
    magnitude-dB values, in ascending-frequency order (mirrors what
    run_gui's own pyqtgraph curve plots)."""
    import base64

    freqs, compute = _build_spectrum_computer(cfg, args, reader)
    freq0, freq_step = float(freqs[0]), float(freqs[1] - freqs[0])
    UPDATE_S = 0.25  # 4 updates/s -- plenty for a monitoring display, and the
    # GUI process redraws the plot for every one

    def spectrum_loop():
        spent, n, since = 0.0, 0, time.monotonic()
        while True:
            t0 = time.thread_time()
            mag_db = compute()
            if mag_db is not None:
                b64 = base64.b64encode(mag_db.astype("<f4").tobytes()).decode("ascii")
                print(f"[spectrum] {freq0} {freq_step} {len(mag_db)} {b64}", file=sys.stderr, flush=True)
            spent += time.thread_time() - t0
            n += 1
            if time.monotonic() - since >= SPECTRUM_REPORT_S:
                # this thread's own CPU per update (excludes the sleep)
                print(f"[spectrum-cpu] {1000 * spent / n:.1f} ms CPU per update, {n} updates "
                      f"= {100 * spent / (time.monotonic() - since):.1f}% of a core", file=sys.stderr)
                spent, n, since = 0.0, 0, time.monotonic()
            time.sleep(UPDATE_S)

    threading.Thread(target=spectrum_loop, daemon=True, name="rx-spectrum").start()
    run_decode_loop(cfg, args, reader)


SPECTRUM_REPORT_S = 30.0


def run_gui(cfg, args, reader):
    """Opens a live spectrum plot (+ a gain slider, for --input pluto
    without --rx-agc) in its own window, while the actual decode/output
    keeps running exactly as it always has on a background thread (see
    run_decode_loop) -- the GUI is purely a monitoring/control layer on
    top, not a replacement for anything in the decode path itself."""
    from PyQt5 import QtCore, QtWidgets
    import pyqtgraph as pg

    UPDATE_MS = 150

    app = QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle(f"hf_ofdm_rx -- Mode {cfg.mode} spectrum")
    central = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(central)

    plot = pg.PlotWidget()
    plot.setLabel("bottom", "Frequency", units="Hz")
    plot.setLabel("left", "Magnitude", units="dB")
    plot.showGrid(x=True, y=True, alpha=0.3)
    curve = plot.plot(pen="y")
    layout.addWidget(plot)

    # Fixed vertical scale: SPECTRUM_DB_PER_DIV dB per division, SPECTRUM_DIVS
    # divisions, top edge at the reference level (adjustable live below --
    # the trace's absolute level is uncalibrated, so the right reference
    # depends on RX gain and signal strength). Autorange and vertical mouse
    # panning/zooming are disabled so the scale never moves on its own.
    SPECTRUM_DB_PER_DIV = args.spectrum_db_per_div
    SPECTRUM_DIVS = 10
    y_axis = plot.getAxis("left")
    y_axis.setTickSpacing(major=SPECTRUM_DB_PER_DIV, minor=SPECTRUM_DB_PER_DIV)
    plot.enableAutoRange(axis="y", enable=False)
    plot.setMouseEnabled(x=True, y=False)

    def apply_ref_level(top_db):
        plot.setYRange(top_db - SPECTRUM_DIVS * SPECTRUM_DB_PER_DIV, top_db, padding=0)
        plot.setLabel("left", f"Magnitude ({SPECTRUM_DB_PER_DIV:g} dB/div)", units="dB")

    ref_row = QtWidgets.QWidget()
    ref_layout = QtWidgets.QHBoxLayout(ref_row)
    ref_layout.setContentsMargins(0, 0, 0, 0)
    ref_layout.addWidget(QtWidgets.QLabel("Reference level (top of screen):"))
    ref_spin = QtWidgets.QDoubleSpinBox()
    ref_spin.setRange(-200.0, 200.0)
    ref_spin.setSingleStep(SPECTRUM_DB_PER_DIV)
    ref_spin.setDecimals(1)
    ref_spin.setSuffix(" dB")
    ref_spin.setValue(args.spectrum_ref_db)
    ref_spin.valueChanged.connect(apply_ref_level)
    ref_layout.addWidget(ref_spin)
    ref_layout.addStretch(1)
    layout.addWidget(ref_row)
    apply_ref_level(args.spectrum_ref_db)

    freqs, compute_spectrum = _build_spectrum_computer(cfg, args, reader)

    can_set_gain = hasattr(reader, "set_gain") and not getattr(reader, "agc", False)
    if can_set_gain:
        gain_row = QtWidgets.QWidget()
        gain_layout = QtWidgets.QHBoxLayout(gain_row)
        gain_label = QtWidgets.QLabel(f"RX gain: {reader.rx_gain_db:.0f} dB")
        gain_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        gain_slider.setMinimum(0)
        gain_slider.setMaximum(73)  # AD9363's RX gain range is roughly 0-73dB
        gain_slider.setValue(int(reader.rx_gain_db))

        def on_gain_changed(value):
            reader.set_gain(float(value))
            gain_label.setText(f"RX gain: {value} dB")

        gain_slider.valueChanged.connect(on_gain_changed)
        gain_layout.addWidget(gain_label)
        gain_layout.addWidget(gain_slider)
        layout.addWidget(gain_row)
    elif getattr(reader, "agc", False):
        layout.addWidget(QtWidgets.QLabel("RX gain: AGC (hardware-controlled -- no manual slider "
                                           "while --rx-agc is set)"))

    win.setCentralWidget(central)
    win.resize(900, 500)

    def update_spectrum():
        mag_db = compute_spectrum()
        if mag_db is not None:
            curve.setData(freqs, mag_db)

    timer = QtCore.QTimer()
    timer.timeout.connect(update_spectrum)
    timer.start(UPDATE_MS)

    decode_thread = threading.Thread(target=run_decode_loop, args=(cfg, args, reader), daemon=True,
                                     name="rx-decode")
    decode_thread.start()

    win.show()
    app.exec_()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=list(ofdm.DRM_MODES), default="B")
    ap.add_argument("--occupancy", type=float, default=20,
                     help="Spectrum occupancy in kHz (default 20); must match the transmitter's "
                          "--occupancy. One of DRM's official values (4.5, 5, 9, 10, 18, 20) uses "
                          "its exact spec'd carrier range; anything else (e.g. 40, 80) falls back "
                          "to a symmetric carrier-count scaling instead.")
    ap.add_argument("--modulation", choices=["qpsk", "16qam"], default="qpsk",
                     help="PAYLOAD modulation (default qpsk) -- must match whatever hf_ofdm_tx.py "
                          "was given, or every payload symbol will decode to noise (the preamble/"
                          "pilots/header stay QPSK either way, so sync itself won't be affected, "
                          "but every payload CRC will fail).")
    ap.add_argument("--fec", choices=["auto", "viterbi", "ldpc"], default="auto",
                     help="Forward Error Correction scheme for PAYLOAD data (default auto). "
                          "'auto': detects Viterbi vs LDPC automatically from the frame header flag. "
                          "'viterbi': forces rate-1/2 K=7 convolutional decode. "
                          "'ldpc': forces IEEE 802.11n rate-1/2 QC-LDPC decode.")
    ap.add_argument("--sample-rate", type=float, default=None,
                     help="Sample rate of the incoming IQ in Hz (e.g. an SDR's fixed capture rate). "
                          "The stream is resampled down to the mode's native rate before decoding. "
                          "Default: assume the input is already at the mode's native rate.")
    ap.add_argument("--legacy-frontend", action="store_true",
                     help="When --sample-rate differs from the mode's rate, use the original numpy "
                          "IQ-correction/filter/resample chain instead of the compiled streaming front "
                          "end (rx_frontend.py). Several times more CPU; kept for comparison.")
    ap.add_argument("--fragment-size", type=int, default=None,
                     help="The --fragment-size passed to hf_ofdm_tx.py, if any. Lets the receiver "
                          "immediately reject a header whose decoded length is wildly bigger than "
                          "expected (a few flipped header bits landing on another 'sane' value) "
                          "instead of wasting real seconds buffering and waiting through the whole "
                          "implied frame before its CRC finally fails -- important for keeping up "
                          "with a live source with no return channel to request a retransmit.")
    ap.add_argument("--fragment-gap-ms", type=float, default=0.0,
                     help="The --fragment-gap-ms passed to hf_ofdm_tx.py, if any -- informational "
                          "only (purely cosmetic, for the startup effective-bitrate estimate to match "
                          "hf_ofdm_tx.py's); decoding itself doesn't need or use this, since the gap "
                          "is silence the sync search just scans past.")
    ap.add_argument("--verbose", action="store_true",
                     help="Print full per-attempt sync/decode diagnostics instead of the concise summary.")
    ap.add_argument("--dump-constellation", type=str, default=None,
                     help="Append the equalized, zero-forced data-carrier points (real,imag CSV, one "
                          "fragment_index,real,imag row per point) from every completed decode attempt "
                          "(success or CRC mismatch) to this file, for offline plotting -- a random "
                          "symmetric scatter around the 4 QPSK points means real channel noise; a "
                          "rotated, smeared, or off-center cloud instead points to residual CFO/timing "
                          "drift or IQ imbalance.")
    ap.add_argument("--input", choices=["pipe", "pluto"], default="pipe",
                     help="'pipe' (default): read raw IQ bytes from stdin, for piping from a "
                          "separate process (pluto_loopback.py's GNU Radio flowgraph, a file, rx.py). "
                          "'pluto': skip the pipe entirely and receive straight from a PlutoSDR via "
                          "SoapySDR. Requires --rf-freq; see --pluto-uri/--rx-gain/--rx-agc/"
                          "--pluto-bandwidth. If this is the SAME physical Pluto hf_ofdm_tx.py is "
                          "also using (e.g. a loopback test), both sides need --pluto-uri "
                          "'ip:<address>' (a NETWORK connection) -- libiio only allows one process "
                          "at a time to open a Pluto over USB, so two separate processes (this "
                          "script + hf_ofdm_tx.py) can't both hold it there, but the iiod network "
                          "daemon is built to serve multiple simultaneous client sessions.")
    ap.add_argument("--rf-freq", type=float, default=None,
                     help="Center frequency in Hz for --input pluto (required with it).")
    ap.add_argument("--rx-gain", type=float, default=30.0,
                     help="PlutoSDR RX gain in dB for --input pluto (default 30). Ignored if --rx-agc.")
    ap.add_argument("--rx-agc", action="store_true",
                     help="Use the PlutoSDR's hardware AGC for --input pluto instead of a fixed "
                          "--rx-gain.")
    ap.add_argument("--rx-warmup-s", type=float, default=0.75, metavar="S",
                     help="Seconds of raw samples to silently discard right after --input pluto "
                          "activates the stream (and again after any live gain change), before the "
                          "decoder ever sees them (default 0.75). Covers the PlutoSDR's own analog "
                          "gain-control/DC-offset/quadrature-tracking settling time -- confirmed for "
                          "real: a freshly-started RX decoded far worse than the identical settings "
                          "moments later after just restarting the RX process. 0 disables it.")
    ap.add_argument("--sdr", choices=["pluto", "lime", "rtlsdr", "airspy", "airspyhf", "sdrplay"], default="pluto",
                     help="Radio for --input pluto: 'pluto' (PlutoSDR, default), 'lime' "
                          "(LimeSDR-USB or LimeSDR Mini; manual gain only, no AGC) or 'rtlsdr' "
                          "(RTL2832U dongle, receive only; --sample-rate must be 225-300 kS/s or "
                          "0.9-3.2 MS/s, e.g. 1024000), 'airspy' (Airspy R2/Mini), 'airspyhf' "
                          "(Airspy HF+) or 'sdrplay' (SDRplay RSP), receive only, at their nearest "
                          "offered sample rate.")
    ap.add_argument("--sdr-ppm", type=float, default=0.0,
                     help="RTL-SDR crystal correction in ppm (a dongle without a TCXO can be "
                          "+-50 ppm: 21 kHz at 432 MHz, beyond the HF modes' CFO search).")
    ap.add_argument("--sdr-antenna", type=str, default=None,
                     help="LimeSDR RX antenna port (LNAL/LNAW/LNAH). Default: chosen by frequency "
                          "from the ports the board reports (LNAL below 1.5 GHz on a LimeSDR-USB; "
                          "the Mini has no LNAL and uses LNAW).")
    ap.add_argument("--gain-file", type=str, default=None,
                     help="Watch this file for a new --rx-gain value (dB) while running -- live "
                          "gain from a GUI, for radios without an outside control route (LimeSDR).")
    ap.add_argument("--freq-offset-file", type=str, default=None,
                     help="Watch this file for a receive frequency offset (Hz) while running: the "
                          "radio re-tunes by that much (the GUI's Offset stepper).")
    ap.add_argument("--pluto-uri", type=str, default=None,
                     help="SoapySDR device URI for --input pluto, e.g. 'ip:192.168.2.1' for a "
                          "network-attached Pluto (required if hf_ofdm_tx.py is using the SAME "
                          "physical device at the same time -- see --input's help). Default: "
                          "auto-select (fine with exactly one Pluto attached over USB).")
    ap.add_argument("--pluto-bandwidth", type=float, default=None,
                     help="Analog front-end bandwidth in Hz for --input pluto. Default: let the "
                          "driver pick (usually close to the sample rate).")
    ap.add_argument("--no-output-pacing", action="store_true",
                     help="Write each decoded fragment to stdout at once instead of trickling it "
                          "out over its own air time. For a reader with its own playback buffer "
                          "(media_rx_player.py, as the GUIs run it): pacing then only adds delay.")
    ap.add_argument("--rx-queue-depth", type=int, default=2,
                     help="How many fragments StdoutPacer will queue up before submit() blocks "
                          "(default 2). Bounding this tightly (rather than letting it grow "
                          "unbounded) trades some jitter-absorption robustness for predictable "
                          "TX-to-RX latency -- see --tx-queue-depth on hf_ofdm_tx.py for the "
                          "matching bound on the other end of the link.")
    ap.add_argument("--drop-if-slow", action="store_true",
                     help="If a fragment's own decode (search+compute) took longer than its "
                          "real-time budget, discard it instead of still writing it out. Without "
                          "this, a late fragment is written anyway -- correct data, but arriving "
                          "later than its real-time slot, which pushes every fragment after it "
                          "later too (StdoutPacer/the downstream player fall further and further "
                          "behind live instead of recovering, since there's no mechanism that "
                          "skips ahead on its own). Dropping a late one trades that fragment's "
                          "content (a brief audio/video glitch) for staying caught up to live "
                          "instead of accumulating a permanently growing lag -- worth it for a "
                          "real-time stream, not for something where every byte matters (e.g. a "
                          "one-shot file transfer, where the default of writing it late anyway "
                          "is what you want). Only drops once decoding is more than "
                          f"{DROP_IF_SLOW_LAG_S:g}s behind the live input; a single slow fragment "
                          "short of that is kept and caught up on over the next ones.")
    ap.add_argument("--pluto-bufflen", type=int, default=None,
                     help="For --input pluto: SoapyPlutoSDR's own hardware-side RX buffer size, "
                          "in SAMPLES. Default: driver auto-picks sample_rate/60 rounded to a "
                          "power of 2 (its own 'Auto setting Buffer Size' log line). Real "
                          "hardware latency on top of anything in this project's own queues -- "
                          "smaller means less of it, at real risk of overflow if this process "
                          "can't drain the hardware at that finer granularity.")
    ap.add_argument("--gui", action="store_true",
                     help="Open a live spectrum display + gain slider window alongside the normal "
                          "decode/output (which keeps running exactly as before, on its own "
                          "background thread, printing the same status lines to stderr). The "
                          "slider directly adjusts --input pluto's real RX gain -- disabled if "
                          "--rx-agc is set, since there's no manual gain to adjust then. Requires "
                          "PyQt5 and pyqtgraph (only imported when this is used).")
    ap.add_argument("--spectrum-stderr", action="store_true",
                     help="For an external GUI that wants to draw its own spectrum display instead "
                          "of this script opening one (see media_rx_gui.py's combined window): "
                          "periodically prints a '[spectrum] ...' snapshot line to stderr (see "
                          "run_spectrum_stderr's own docstring for the exact format) instead of "
                          "opening a window. Uses the same --spectrum-averages/--spectrum-db-per-div "
                          "settings as --gui, but ignores --spectrum-ref-db (no window to set a "
                          "reference level on). Mutually exclusive with --gui; no PyQt5/pyqtgraph "
                          "import needed in this mode.")
    ap.add_argument("--spectrum-span-hz", type=float, default=0.0, metavar="HZ",
                     help="Spectrum display span. 0 (default): the modem's own sample rate, "
                          "which a full-occupancy signal fills edge to edge. Larger: computed "
                          "from the raw SDR samples (when the front end resamples, e.g. Pluto "
                          "at 550 kS/s) shifted by the LO offset, so the signal sits centred "
                          "with noise floor either side -- e.g. 1.5x the occupancy.")
    ap.add_argument("--spectrum-averages-file", type=str, default=None, metavar="PATH",
                     help="Watch this file for a new --spectrum-averages value while running "
                          "(the GUI's live setting).")
    ap.add_argument("--spectrum-averages", type=int, default=8, metavar="N",
                     help="Number of FFT frames to average for the --gui spectrum display "
                          "(default 8). Higher values smooth the trace but slow its response "
                          "to signal changes; lower values give a noisier but more reactive "
                          "display. Has no effect without --gui.")
    ap.add_argument("--spectrum-db-per-div", type=float, default=2.5, metavar="DB",
                     help="Vertical scale of the --gui spectrum, in dB per division (default 2.5; "
                          "10 divisions are shown, autorange is off). Has no effect without --gui.")
    ap.add_argument("--spectrum-ref-db", type=float, default=-20.0, metavar="DB",
                     help="Level at the TOP of the --gui spectrum display (default -20). The trace is "
                          "uncalibrated (dB of the FFT power, which depends on RX gain and signal), so "
                          "this can also be changed live with the spin box under the plot. Has no "
                          "effect without --gui.")
    ap.add_argument("--lo-offset-hz", type=float, default=0.0,
                     help="For --input pluto: tune the PlutoSDR's actual LO this far away from "
                          "--rf-freq (must match the transmitter's --lo-offset-hz), and digitally "
                          "correct received samples back by the opposite amount, so the AD9363's "
                          "own DC/LO leakage spike (which sits wherever the LO is ACTUALLY tuned) "
                          "lands off to the side of the signal instead of in the middle of it. "
                          "Default 0 (no offset).")
    args = ap.parse_args()
    import avm_threads
    avm_threads.install(main_name="rx-decode")  # thread names visible in top -H

    if args.input == "pluto" and not args.rf_freq:
        print("ERROR: --input pluto requires --rf-freq.", file=sys.stderr)
        sys.exit(1)

    fec_choice = "viterbi" if args.fec == "auto" else args.fec
    cfg = ofdm.build_config(args.mode, args.occupancy, data_modulation=args.modulation, fec_scheme=fec_choice)
    ofdm.print_config(cfg, fragment_size_bytes=args.fragment_size, fragment_gap_ms=args.fragment_gap_ms)

    if not args.fragment_size:
        # Missed this exact flag more than once this session -- without
        # it, a garbled/CRC-mismatched header can still parse to a
        # "sane" but wrong payload_len within the much looser 300s
        # fallback cap, and decode_frame will patiently process (or even
        # probe-retry) a far bigger implied frame than it should,
        # confirmed for real to cost multi-second stalls on an otherwise
        # healthy link. Not fatal -- just very easy to forget when TX
        # has its own --fragment-size and this one doesn't automatically
        # match it.
        print("WARNING: --fragment-size not set -- a bad header can still stall for "
              "multiple seconds on a wrongly-implied huge frame instead of failing fast. "
              "Pass the same --fragment-size used on the TX side unless you're truly "
              "sending one unfragmented message.", file=sys.stderr)

    if args.dump_constellation:
        import os
        if not os.path.exists(args.dump_constellation):
            with open(args.dump_constellation, "w") as f:
                f.write("fragment_index,real,imag\n")

    reader = make_reader(args, cfg)

    if args.gui:
        run_gui(cfg, args, reader)
    elif args.spectrum_stderr:
        run_spectrum_stderr(cfg, args, reader)
    else:
        run_decode_loop(cfg, args, reader)


REACQUISITION_TROUBLE_LIMIT = 5  # see the call sites' own comment for what counts


def _note_reacquisition_trouble(state):
    """Shared by both the 'failed' (no lock at all) and the 'ok status
    but CRC bad' (locked onto something, just the wrong thing) call
    sites -- see their own comments for why both need to count toward
    the same escape hatch. Clears state["last_cfo_hz"] once
    REACQUISITION_TROUBLE_LIMIT of either kind happen in a row (any
    CRC-OK success resets the counter to 0 elsewhere), giving the next
    lock attempt a genuinely fresh, unbiased CFO search instead of
    staying anchored to a prior that may simply be wrong."""
    state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
    if state["consecutive_failures"] >= REACQUISITION_TROUBLE_LIMIT and state.get("last_cfo_hz") is not None:
        print(f"  ! {REACQUISITION_TROUBLE_LIMIT}+ consecutive fragments without a confirmed decode -- "
              f"clearing the CFO-continuity prior in case the transmitter was restarted (a real "
              f"oscillator re-tune can shift CFO enough to either deadlock the jump-limit gate, or "
              f"bias resolve_integer_cfo's own search onto a wrong-but-plausible candidate, against "
              f"a now-stale prior).", file=sys.stderr)
        state["last_cfo_hz"] = None
        state["consecutive_failures"] = 0


# --drop-if-slow only drops a slow fragment once decoding has fallen this
# far behind the live input (see its use in run_decode_loop).
DROP_IF_SLOW_LAG_S = 1.0
# Symbols searched for the next preamble before the full window (see
# receive_one_fragment's sync fast path).
# Decode with the previous fragment's integer CFO before searching (see
# try_decode). 0: always search first (regression baseline).
ASSUME_PRIOR_INTEGER_CFO = os.environ.get("HF_RX_ASSUME_PRIOR_CFO", "1") != "0"
SYNC_FAST_SYMBOLS = int(os.environ.get("HF_RX_SYNC_FAST_SYMBOLS", "6"))  # 0: off (regression baseline)
# ... and once any attempt (good or bad) leaves it this far behind, the
# backlog is skipped, keeping the last SKIP_KEEP_S to resync in.
# Both scale with the fragment's air time (0.24 s in mode A): keep enough
# for the next whole fragment, skip once a bit more than that has piled up.
SKIP_KEEP_FRAGMENTS = 1.5
SKIP_EXTRA_LAG_S = 0.6


def _skip_limits(cfg, args):
    """(lag that triggers a skip, backlog kept), in seconds."""
    try:
        period = args.fragment_size * 8 / ofdm.estimate_effective_bitrate(
            cfg, args.fragment_size, fragment_gap_ms=args.fragment_gap_ms)
    except Exception:
        period = 0.6
    keep = SKIP_KEEP_FRAGMENTS * period + 0.1
    return keep + SKIP_EXTRA_LAG_S, keep


def run_decode_loop(cfg, args, reader):
    """The actual receive/decode/output loop -- unchanged from before
    --gui existed, just extracted out of main() so it can be run either
    directly (the default) or on a background thread underneath a Qt
    event loop (see run_gui), without duplicating any of this logic."""
    if os.environ.get("HF_OFDM_RX_WAIT_EOF"):
        # Test-only (rx_regression.py): start decoding only once ALL input
        # has arrived, so results don't depend on how the pipe happened to
        # chunk it or how far ahead decoding got -- makes identical input
        # give identical output, so optimisations can be checked exactly.
        while not reader.eof:
            reader.wait_for_more(reader.total_len, timeout=0.5)
    state = {"native_search_start": 0}
    # Streaming mode: for a continuous, indefinite source (live audio,
    # no defined end) there's no "total_frags" completion condition to
    # wait for, so each fragment's data is written to stdout the moment
    # it's decoded instead of being buffered until a full, known-length
    # message is assembled. frag_idx (a uint16 in FRAG_HEADER) is treated
    # purely as a rolling sequence counter for gap detection/logging --
    # it wraps at 65536 by construction, which is fine here since nothing
    # depends on it ever reaching a specific total. Fragments are already
    # written in the correct order: try_decode only ever searches forward
    # through the stream, so one fragment is always fully resolved before
    # the next is even attempted.
    fragments_written = 0
    fragments_dropped = 0
    bytes_written = 0
    expected_next_frag_idx = None
    pacer = StdoutPacer(queue_depth=args.rx_queue_depth, pace=not args.no_output_pacing)
    skip_lag_s, skip_keep_s = _skip_limits(cfg, args)
    if args.drop_if_slow:
        print(f"Catch-up: skip to live once {skip_lag_s:.2f}s behind (keeping {skip_keep_s:.2f}s)",
              file=sys.stderr)

    while True:
        search_start_before = state["native_search_start"]
        t0 = time.time()
        result = receive_one_fragment(cfg, args, reader, state)
        elapsed = time.time() - t0

        if "next_native_search_start" in result:
            # Advance to exactly past this frame -- not further (which
            # could skip the next fragment's own preamble) and not less
            # (which could re-lock onto this same, now-consumed preamble
            # forever, since every fragment's preamble is identical). For a
            # "failed" result this is the coarser bump receive_one_fragment
            # applied internally while exhausting its own retries.
            state["native_search_start"] = result["next_native_search_start"]

        if result["status"] == "eof":
            break
        # Low SNR: a fragment that fails CRC costs ~0.35-0.55 s of decode
        # (LDPC runs to its iteration limit, then a resync retry) against
        # 0.24 s of air time, so a bad patch built up seconds of lag --
        # and the drop rule below only ever looks at GOOD fragments. Once
        # this far behind, skip the backlog (likely undecodable anyway) and
        # resync near live, keeping SKIP_KEEP_S so the next preamble isn't cut.
        if args.drop_if_slow:
            live = reader.total_len / 8 / (args.sample_rate or cfg.fs) * cfg.fs
            lag_s = (live - state["native_search_start"]) / cfg.fs
            if lag_s > skip_lag_s:
                state["native_search_start"] = int(live - skip_keep_s * cfg.fs)
                print(f"  -> SKIPPED {lag_s - skip_keep_s:.2f}s of backlog to catch up with live "
                      f"(decoding fell {lag_s:.2f}s behind)", file=sys.stderr)
        # state["last_cfo_hz"] only ever gets updated on a CRC-OK SUCCESS
        # (see below) and otherwise persists forever -- fine as long as
        # the transmitter keeps running, but if TX gets restarted (a real
        # oscillator re-tune, which can easily shift the true CFO by more
        # than CFO_JUMP_LIMIT_HZ from whatever was last confirmed) while
        # this RX process keeps running, every subsequent low-confidence
        # lock attempt either gets rejected outright for jumping "too far"
        # from a prior that's now simply wrong, OR (a second, originally-
        # missed failure mode: confirmed for real via a link that never
        # printed a single "FAILED" line throughout a long, ~80-fragment
        # bad stretch, yet decoded almost nothing) still finds a REAL
        # preamble/header lock every time -- resolve_integer_cfo's own
        # prior_k search just gets biased toward the WRONG integer CFO
        # candidate by the stale prior, corrupting every subcarrier's
        # phase by a wrong multiple of carrier_spacing. That shows up as
        # "ok" status with moderate EVM but anomalously high BER (a
        # systematic misalignment, not plain noise) -- CRC fails every
        # time, but never as an outright "FAILED", so the failure-count
        # escape hatch below never used to even see it. Since nothing can
        # succeed to correct a stale prior on its own, either failure mode
        # is a permanent deadlock without this: count BOTH a "failed"
        # scan and an "ok"-but-CRC-bad decode as evidence something is
        # still wrong, and clear the stale prior after enough of either
        # in a row -- cheap even if triggered a little early on ordinary
        # marginal-SNR CRC losses (a stale-prior-free search just costs a
        # wider, unbiased candidate sweep on the next lock, not a wrong
        # answer), and the only way to actually escape a true stale-prior
        # deadlock instead of waiting on a "FAILED" that may never come.
        if result["status"] == "failed":
            signal_duration_s = (state["native_search_start"] - search_start_before) / cfg.fs
            print(f"Fragment: FAILED (garbage header -- bad sync or no signal)  "
                  f"Time={elapsed:.2f}s (scanned {signal_duration_s:.2f}s)"
                  f"{_retry_note(result)}", file=sys.stderr)
            _note_reacquisition_trouble(state)
            continue
        if result["status"] == "incomplete":
            # Only reachable if stdin closed mid-fragment.
            print(f"Fragment: INCOMPLETE (stream ended before enough samples arrived)  "
                  f"Time={elapsed:.2f}s", file=sys.stderr)
            break

        payload, crc_ok, cfo_hz, ber, evm = (
            result["payload"], result["crc_ok"], result["cfo_hz"], result["ber"], result["evm"])
        if len(payload) < FRAG_HEADER.size:
            print("Fragment: FAILED (payload too short to contain fragment header)", file=sys.stderr)
            continue
        frag_idx, frag_total, frag_total_len = FRAG_HEADER.unpack(payload[:FRAG_HEADER.size])
        data = payload[FRAG_HEADER.size:]

        if args.dump_constellation and result["constellation"] is not None:
            with open(args.dump_constellation, "a") as f:
                for pt in result["constellation"]:
                    f.write(f"{frag_idx},{pt.real},{pt.imag}\n")

        fade_frac = ofdm.deep_fade_fraction(result["constellation"]) if result["constellation"] is not None else None
        status_word = "OK" if crc_ok else "CRC MISMATCH"
        # search_start_before to next_native_search_start spans however far
        # the search had to look to FIND this fragment -- after a real TX
        # gap (silence/dead air while the transmitter was off), that span
        # includes the whole gap, not just this one fragment's own frame.
        # Good for the diagnostic "budget" figure below (it's an honest
        # measure of how much ground the search had to cover), but WRONG
        # for pacing playback: passing this to the pacer told it to stretch
        # one normal fragment's payload out over the ENTIRE gap duration
        # (confirmed for real: a 2048-byte fragment paced over a reported
        # "budget" of 7-12+ seconds right after a TX restart) -- which is
        # exactly what produced "hangs, recovers, then buffers up the
        # player": that one fragment dribbles out in slow motion while
        # everything decoded after it queues up behind it. This fragment's
        # own true audio duration is only the span from where ITS OWN
        # preamble actually starts to where it ends, independent of
        # whatever gap preceded that preamble.
        search_duration_s = (state["native_search_start"] - search_start_before) / cfg.fs
        frame_start = result.get("frame_start_index")
        signal_duration_s = ((state["native_search_start"] - frame_start) / cfg.fs
                              if frame_start is not None else search_duration_s)
        # A live receiver structurally can't finish decoding a fragment
        # before that fragment's own over-the-air audio has finished
        # arriving -- so elapsed is ALWAYS at least budget, plus real
        # decode compute (a genuine tail cost paid after the last needed
        # sample lands, confirmed for real at ~55ms) plus a little stdin
        # read-granularity latency. A small, consistent overshoot in that
        # ~50-100ms range is the expected, healthy floor, not falling
        # behind -- only flag it once the gap is big enough to actually
        # mean something (accumulating retries/false locks, not just
        # this unavoidable tail).
        REALTIME_OVERSHOOT_FLOOR_S = 0.15
        slow = elapsed > search_duration_s + REALTIME_OVERSHOOT_FLOOR_S
        gap_note = f"  (spans a {search_duration_s:.2f}s search gap)" if search_duration_s > signal_duration_s + 1.0 else ""
        time_note = (f"  Time={elapsed:.2f}s (budget {search_duration_s:.2f}s)"
                     f"{' <-- SLOWER THAN REALTIME' if slow else ''}{_retry_note(result)}{gap_note}")
        print(f"Fragment seq={frag_idx}: {status_word}  "
              f"CFO={cfo_hz:+.1f}Hz  BER={ber:.4f}  EVM={evm:.1f}%  "
              f"DeepFade={fade_frac*100:.2f}%{time_note}", file=sys.stderr)

        if not crc_ok:
            _note_reacquisition_trouble(state)
            continue  # don't accept a corrupted fragment; wait to see if a retry/next attempt helps
        # Smooth the confirmed-CFO prior with an EMA instead of trusting
        # the single latest fragment outright -- a marginal-EVM fragment's
        # own CFO estimate carries real per-fragment noise (confirmed for
        # real: the header's whole-sequence path metric is much cleaner
        # than any single per-symbol estimate would be, but it's still one
        # noisy sample), and both resolve_integer_cfo's prior_k bias and
        # the CFO_JUMP_LIMIT_HZ gate above feed directly off this value --
        # a noisy spike here otherwise propagates straight into the NEXT
        # fragment's own resync, making it more (not less) likely to need
        # a retry. A real oscillator drifts smoothly, so smoothing the
        # tracked value costs nothing against genuine drift while damping
        # single-fragment noise.
        prev_cfo_hz = state.get("last_cfo_hz")
        state["last_cfo_hz"] = (cfo_hz if prev_cfo_hz is None
                                 else CFO_EMA_ALPHA * cfo_hz + (1 - CFO_EMA_ALPHA) * prev_cfo_hz)
        state["consecutive_failures"] = 0

        if expected_next_frag_idx is not None and frag_idx != expected_next_frag_idx:
            gap = (frag_idx - expected_next_frag_idx) % 65536
            print(f"  ! sequence gap: expected seq={expected_next_frag_idx}, got seq={frag_idx} "
                  f"({gap} fragment(s) lost -- no return channel to request a resend)", file=sys.stderr)
        expected_next_frag_idx = (frag_idx + 1) % 65536

        # Only drop when actually BEHIND live -- one slow fragment is
        # normally caught up over the next few (their samples are already
        # buffered), so dropping it on its own slowness alone threw away a
        # good fragment (an audible A/V break) without saving any time. Lag
        # = received-but-not-yet-decoded signal.
        lag_s = (reader.total_len / 8 / (args.sample_rate or cfg.fs)
                 - state["native_search_start"] / cfg.fs)
        if slow and args.drop_if_slow and lag_s > DROP_IF_SLOW_LAG_S:
            fragments_dropped += 1
            print(f"  -> DROPPED (slower than realtime, {lag_s:.2f}s behind live, seq={frag_idx}, "
                  f"{fragments_dropped} dropped so far)", file=sys.stderr)
            continue
        if slow and args.drop_if_slow:
            print(f"  -> kept (only {lag_s:.2f}s behind live)", file=sys.stderr)

        pacer.submit(data, signal_duration_s, debug_tag=frag_idx)
        fragments_written += 1
        bytes_written += len(data)
        print(f"  -> wrote {len(data)} bytes (seq={frag_idx}, {bytes_written} total so far)",
              file=sys.stderr)

    pacer.drain()
    reader.close()
    print(f"Stream ended: wrote {bytes_written} bytes across {fragments_written} fragment(s)"
          f"{f', dropped {fragments_dropped} slow fragment(s)' if fragments_dropped else ''}.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
