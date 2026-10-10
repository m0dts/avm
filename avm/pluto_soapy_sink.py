#!/usr/bin/env python3
"""
Direct SoapySDR TX/RX for a PlutoSDR -- lets hf_ofdm_tx.py/hf_ofdm_rx.py
talk straight to the hardware instead of piping raw IQ bytes through a
separate GNU Radio flowgraph process (pluto_loopback.py). Removes a whole
extra process + pipe hop (and GNU Radio's own scheduler/buffering) from
the chain, which is worth trying if you're chasing buffering/latency
issues -- but it's a straight replacement for what that flowgraph was
doing on each side, not a redesign: same float32 complex IQ convention
(normalized to +-1.0 full scale on TX, matching build_frame's own
output), same "push/pull samples" usage pattern.

PlutoTxSink and PlutoRxSource each talk to their own single-direction
SoapySDR stream -- neither attempts full duplex on its own, so running
both from two separate processes against the same physical Pluto (one
per direction) is the supported way to do a real over-the-air test, same
as you'd run separate tx.py/rx.py flowgraphs today.

Requires the SoapySDR Python bindings AND the PlutoSDRSupport module to be
importable/registered (confirmed present in this project's radioconda
environment: `python -c "import SoapySDR; print(SoapySDR.listModules())"`
should list a PlutoSDRSupport entry). Import is deliberately lazy (done
inside each class's __init__, not at module load) so hf_ofdm_tx.py/
hf_ofdm_rx.py don't require SoapySDR at all unless actually asked to talk
to a Pluto directly.
"""
import os
import queue
import sys
import threading
import time

import numpy as np


AD936X_TX_MAX_ATTEN_DB = 89.0


def _tx_gain_for_driver(sdr, direction, tx_gain_db):
    """--tx-gain is dB relative to full output (0 = full power, negative =
    attenuation, e.g. -30), i.e. the AD936x's own TX 'hardwaregain'.
    SoapyPlutoSDR has used two conventions for the value setGain() takes:
    older builds take that attenuation directly (range [-89, 0]); current
    builds (e.g. built from source in 2026) take 0..89 with 89 = full power.
    Passing -30 to the latter clamps to 0 = MAXIMUM attenuation (-89 dB) --
    confirmed for real: a TX->RX cable loop received only the noise floor.
    Detect which one this driver uses from its reported range."""
    try:
        rng = sdr.getGainRange(direction, 0)
        lo, hi = rng.minimum(), rng.maximum()
    except Exception:
        return tx_gain_db
    if lo >= 0 and hi >= AD936X_TX_MAX_ATTEN_DB - 1:  # 0..89 convention
        value = AD936X_TX_MAX_ATTEN_DB + tx_gain_db
        print(f"PlutoSDR TX: driver gain range [{lo:g}, {hi:g}] dB -> --tx-gain {tx_gain_db:g} dB "
              f"(re full power) set as {value:g}", file=sys.stderr)
        return min(max(value, lo), hi)
    return tx_gain_db


AIRSPY_DRIVERS = {"airspy": "Airspy", "airspyhf": "Airspy HF+"}


def _nearest_supported_rate(sdr, direction, want):
    """An Airspy only samples at a few fixed rates (R2 2.5/10 MS/s, Mini 3/6,
    HF+ 192-912 kS/s): the lowest one at or above want (else the highest).
    Less to resample is less CPU."""
    try:
        rates = sorted(float(r) for r in sdr.listSampleRates(direction, 0))
    except Exception:
        return want
    if not rates:
        return want
    return next((r for r in rates if r >= want), rates[-1])


def _open_sdr(driver, uri, what):
    """Opens the SoapySDR device for driver 'pluto' (PlutoSDR, at uri),
    'lime' (LimeSDR-USB or LimeSDR Mini, via the LMS7 SoapySDR module) or
    'rtlsdr' (an RTL2832U dongle, receive only, via SoapyRTLSDR), 'airspy'
    (Airspy R2 / Mini, SoapyAirspy) or 'airspyhf' (Airspy HF+, SoapyAirspyHF),
    both receive only."""
    import SoapySDR
    if driver in AIRSPY_DRIVERS:
        name = AIRSPY_DRIVERS[driver]
        print(f"Opening {name} {what}...", file=sys.stderr)
        return SoapySDR.Device(f"driver={driver}")
    if driver == "rtlsdr":
        print(f"Opening RTL-SDR {what}...", file=sys.stderr)
        sdr = SoapySDR.Device("driver=rtlsdr")
        try:
            print(f"RTL-SDR {what}: tuner {dict(sdr.getHardwareInfo()).get('tuner', '?')}", file=sys.stderr)
        except Exception:
            pass
        return sdr
    if driver == "lime":
        print(f"Opening LimeSDR {what}...", file=sys.stderr)
        sdr = SoapySDR.Device("driver=lime")
        try:
            hw = dict(sdr.getHardwareInfo())
            print(f"LimeSDR {what}: board {sdr.getHardwareKey()}, gateware {hw.get('gatewareVersion', '?')}",
                  file=sys.stderr)
        except Exception:
            pass
        return sdr
    args = {"driver": "plutosdr"}
    if uri:
        args["uri"] = uri
    print(f"Opening PlutoSDR {what} (uri={uri or 'auto'})...", file=sys.stderr)
    try:
        sdr = SoapySDR.Device(_device_args_string(args))
    except RuntimeError as e:
        if not uri:
            raise
        # The chosen route failed: an IP address that doesn't answer (a USB
        # Pluto whose network link isn't up, e.g. a minimal Linux with no
        # network manager), or a USB address that changed after re-plugging.
        # libiio still finds the Pluto by itself.
        print(f"PlutoSDR {what}: no answer at {uri} ({e}) -- looking for it...", file=sys.stderr)
        sdr = None
        # the other default address: PlutoSDR 192.168.2.1, LibreSDR 192.168.1.10
        for alt in ("ip:192.168.2.1", "ip:192.168.1.10"):
            if alt != uri:
                try:
                    sdr = SoapySDR.Device(_device_args_string({"driver": "plutosdr", "uri": alt}))
                    print(f"PlutoSDR {what}: found at {alt}", file=sys.stderr)
                    uri = alt
                    break
                except RuntimeError:
                    pass
        if sdr is None:
            sdr = SoapySDR.Device(_device_args_string({"driver": "plutosdr"}))
            uri = None
    _ensure_fdd_mode(uri)
    return sdr


def _lime_configure(sdr, direction, freq_hz, antenna=None):
    """LimeSDR antenna port and analogue filter. The port names differ by
    board -- the LimeSDR-USB has RX LNAH/LNAL/LNAW, the LimeSDR Mini only
    LNAH/LNAW (no LNAL) -- so unless one is given, pick from what this
    board reports, by frequency: RX LNAL below 1.5 GHz if present (the
    USB's RX1_L connector; measured: the only one of the three that heard
    a 145 MHz signal on a cable to it), else LNAW below 2 GHz, else LNAH;
    TX BAND1 below 2 GHz, else BAND2. Override with --sdr-antenna if the
    cable is on another connector."""
    from SoapySDR import SOAPY_SDR_RX
    rx = direction == SOAPY_SDR_RX
    ports = [a for a in sdr.listAntennas(direction, 0) if a != "NONE"]
    # environment override, e.g. AVM_LIME_TX_ANTENNA=BAND2 for a board whose
    # ports map differently (the LimeSDR Mini routes one TX socket via BAND1/2)
    antenna = antenna or os.environ.get("AVM_LIME_RX_ANTENNA" if rx else "AVM_LIME_TX_ANTENNA") or None
    if antenna and ports and antenna not in ports:
        print(f"LimeSDR {'RX' if rx else 'TX'}: this board has no port {antenna} -- choosing automatically",
              file=sys.stderr)
        antenna = None
    if not antenna:
        if rx:
            if "LNAL" in ports and freq_hz < 1.5e9:
                antenna = "LNAL"
            else:
                antenna = "LNAW" if freq_hz < 2.0e9 else "LNAH"
        else:
            antenna = "BAND1" if freq_hz < 2.0e9 else "BAND2"
        if antenna not in ports and ports:
            antenna = ports[0]
    sdr.setAntenna(direction, 0, antenna)
    try:
        # smallest analogue filters (the LMS7 decimates/filters digitally below that)
        sdr.setBandwidth(direction, 0, 1.5e6 if rx else 5e6)
    except Exception:
        pass
    print(f"LimeSDR {'RX' if rx else 'TX'}: antenna port {antenna} (board offers {', '.join(ports)})",
          file=sys.stderr)


def _lime_tx_gain(sdr, direction, tx_gain_db):
    """--tx-gain (dB re full power, 0 = full) on the LMS7's absolute TX gain
    scale (-12..64 dB, top = full power)."""
    rng = sdr.getGainRange(direction, 0)
    return min(max(rng.maximum() + tx_gain_db, rng.minimum()), rng.maximum())


class GainFileWatcher:
    """Live gain for any radio: the GUI writes a number (dB, same meaning
    as --tx-gain/--rx-gain) to this file; within ~0.2 s it's passed to
    apply(). For the Pluto the GUI can instead set gain through libiio's
    iio_attr; the LimeSDR has no such outside route, so it goes through
    the process that holds the device."""

    def __init__(self, path, apply):
        self.path, self.apply = path, apply
        self._last = None
        threading.Thread(target=self._run, daemon=True, name="gain-watch").start()

    def _run(self):
        import os
        while True:
            try:
                st = os.stat(self.path)
                key = (st.st_mtime_ns, st.st_size)
                if key != self._last:
                    self._last = key
                    with open(self.path) as f:
                        self.apply(float(f.read().strip()))
            except (OSError, ValueError):
                pass
            time.sleep(0.2)


def _device_args_string(args):
    """SoapySDR device args as a "key=value,key=value" string. Some builds
    of the SoapySDR Python bindings (confirmed: Debian 13's python3-soapysdr
    on a Raspberry Pi) silently drop a Python dict's contents -- every
    module gets probed as if no driver were given and make() fails with
    "no match" -- while the string form always works (it's also what
    SoapySDRUtil --make uses)."""
    return ",".join(f"{k}={v}" for k, v in args.items())


def _ensure_fdd_mode(uri):
    """Puts the AD936x state machine back in FDD mode if it isn't. A
    streaming process killed mid-run (e.g. a GUI stopped abruptly) can leave
    the Pluto in "alert" mode, where control still works but no RX samples
    ever arrive -- seen on a Pi: readStream only timed out, even after a USB
    replug, until ensm_mode was set back to fdd. SoapyPlutoSDR doesn't touch
    it, so this uses libiio's iio_attr tool when it's installed and does
    nothing otherwise."""
    import shutil
    import subprocess
    if not uri or shutil.which("iio_attr") is None:
        return
    base = ["iio_attr", "-u", uri, "-d", "ad9361-phy", "ensm_mode"]
    try:
        mode = subprocess.run(base, capture_output=True, text=True, timeout=5).stdout.strip()
        if mode and mode != "fdd":
            subprocess.run(base + ["fdd"], capture_output=True, timeout=5)
            print(f"PlutoSDR: radio was in '{mode}' mode (no streaming) -- set back to fdd.",
                  file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired):
        pass


class PlutoTxSink:
    """Opens a PlutoSDR TX stream via SoapySDR and accepts complex
    waveform arrays to transmit.

    write() only ENQUEUES the waveform and returns immediately -- the
    actual writeStream() calls happen on a dedicated background thread.
    This matters a lot for a live/streaming source: building each
    fragment (FEC encode, OFDM modulate, optional resample) is real CPU
    time, and with a synchronous write, that compute happened entirely
    BETWEEN two writeStream calls -- a genuine gap in what's being fed to
    the hardware, during which the SDR's own TX buffer runs dry and RF
    output actually breaks. Confirmed for real: audible/RF breaks at
    every fragment boundary even with --fragment-gap-ms 0 (no
    intentional gap in the waveform DATA itself -- the gap was in when
    bytes reached the hardware, not in the bytes themselves). A
    background writer thread continuously draining a queue means the
    caller (e.g. hf_ofdm_tx.py's run_stream) can immediately start
    building the NEXT fragment while this one is still being streamed
    out, keeping the hardware fed as long as building a fragment is
    faster than transmitting it -- true here by a wide margin (FEC/OFDM
    modulate is milliseconds; a fragment's own airtime is hundreds of ms
    to seconds).

    uri: SoapySDR device args string, e.g. "ip:192.168.2.1" for a
    network-attached Pluto, or None to let SoapySDR auto-select (fine
    when exactly one Pluto is attached; be explicit if more than one
    could be found, e.g. over both USB and network)."""

    def __init__(self, freq_hz, sample_rate_hz, tx_gain_db=-10.0,
                 uri=None, bandwidth_hz=None, buffer_size=1 << 16, queue_depth=8,
                 stream_bufflen=None, lo_offset_hz=0.0, driver="pluto", antenna=None, gain_file=None):
        import SoapySDR
        from SoapySDR import SOAPY_SDR_TX, SOAPY_SDR_CF32

        self._SoapySDR = SoapySDR
        self._SOAPY_SDR_TX = SOAPY_SDR_TX
        self.driver = driver

        self.sdr = _open_sdr(driver, uri, "TX")
        self.sdr.setSampleRate(SOAPY_SDR_TX, 0, sample_rate_hz)
        # Tune the actual LO lo_offset_hz away from the requested
        # frequency, and pre-shift each waveform by the opposite amount
        # in baseband (see write()) so the transmitted RF signal still
        # lands exactly at freq_hz -- the AD9363's own DC/LO leakage
        # spike sits at whatever the LO is ACTUALLY tuned to, so with no
        # offset it lands right in the middle of the signal (confirmed
        # for real: a big spike at center, since the old GNU Radio
        # flowgraph this replaced did carry this same trick -- 25kHz
        # offset + blocks.rotator_cc -- and it was never carried over
        # when rebuilding TX/RX directly against SoapySDR).
        self.lo_offset_hz = lo_offset_hz
        self.sdr.setFrequency(SOAPY_SDR_TX, 0, freq_hz + lo_offset_hz)
        if driver == "lime":
            _lime_configure(self.sdr, SOAPY_SDR_TX, freq_hz + lo_offset_hz, antenna)
        self.set_gain(tx_gain_db)
        if bandwidth_hz:
            self.sdr.setBandwidth(SOAPY_SDR_TX, 0, bandwidth_hz)
        if gain_file:
            GainFileWatcher(gain_file, self.set_gain)

        self.buffer_size = buffer_size
        self.sample_rate_hz = sample_rate_hz
        # SoapyPlutoSDR's own hardware-side buffer, in SAMPLES -- the
        # "bufflen" stream arg (undocumented outside its own source;
        # confirmed via PlutoSDR_Streaming.cpp in the SoapyPlutoSDR
        # repo). Without it, the driver auto-picks sample_rate/60
        # rounded to a power of 2 (its own printed "Auto setting Buffer
        # Size" line) -- e.g. 32768 samples (~33ms) at 1MSPS. This is
        # genuine hardware-side latency separate from anything in this
        # project's own queues, and libiio queuing a few of these
        # buffers in flight can multiply it. Smaller = less latency,
        # at real risk of underrun if writeStream can't keep up.
        stream_args = {"bufflen": str(stream_bufflen)} if stream_bufflen and driver != "lime" else {}
        self.stream = self.sdr.setupStream(SOAPY_SDR_TX, SOAPY_SDR_CF32, [0], stream_args)
        self.sdr.activateStream(self.stream)
        print(f"{'LimeSDR' if driver == 'lime' else 'PlutoSDR'} TX active: freq={freq_hz/1e6:.4f}MHz "
              f"(LO tuned to {(freq_hz + lo_offset_hz)/1e6:.4f}MHz, {lo_offset_hz/1e3:+.1f}kHz "
              f"offset) rate={sample_rate_hz/1e3:.1f}kHz gain={tx_gain_db}dB", file=sys.stderr)

        # Bounded so a genuinely-stuck writer (e.g. a hung device) blocks
        # the producer eventually instead of growing memory forever,
        # while still giving several fragments' worth of lookahead
        # cushion for normal jitter in either side's timing.
        self._queue = queue.Queue(maxsize=queue_depth)
        self._thread = threading.Thread(target=self._run, daemon=True, name="tx-sdr-write")
        self._thread.start()

    def set_gain(self, tx_gain_db):
        """TX gain in dB re full power (0 = full), on whichever radio."""
        if self.driver == "lime":
            value = _lime_tx_gain(self.sdr, self._SOAPY_SDR_TX, tx_gain_db)
        else:
            value = _tx_gain_for_driver(self.sdr, self._SOAPY_SDR_TX, tx_gain_db)
        self.sdr.setGain(self._SOAPY_SDR_TX, 0, value)

    def write(self, waveform):
        """Enqueues `waveform` (any complex dtype, normalized to +-1.0
        full scale) for transmission -- does NOT block on hardware I/O
        (see class docstring). Blocks only if the queue is already full
        (queue_depth fragments deep), which under normal operation means
        the caller is producing faster than real-time transmission can
        drain -- backpressure, not a bug.

        If lo_offset_hz is set, pre-shifts this waveform DOWN in baseband
        by that same amount before it's transmitted -- the LO is tuned
        UP by lo_offset_hz (see __init__), so the two cancel out and the
        signal still lands exactly at the requested RF frequency. Reset
        fresh (t=0) for every call rather than tracking phase across
        fragments -- fine because nothing in this protocol assumes phase
        continuity between fragments anyway (each one resyncs
        independently via its own preamble; see hf_ofdm_common.py)."""
        waveform = np.ascontiguousarray(waveform, dtype=np.complex64)
        if self.lo_offset_hz:
            waveform = waveform * self._lo_phasor(len(waveform))
        self._queue.put(waveform)

    def _lo_phasor(self, n):
        """exp(-j*2*pi*lo*t) for t = 0..n-1 samples, complex64 -- built from
        a short exact repeating table (see hf_ofdm_common.lo_phasor_table)
        and cached per length (every fragment is the same length), instead
        of a complex exp over every sample of every fragment."""
        cache = self.__dict__.setdefault("_lo_phasor_cache", {})
        ph = cache.get(n)
        if ph is None:
            import hf_ofdm_common as ofdm
            table = ofdm.lo_phasor_table(-self.lo_offset_hz, self.sample_rate_hz)
            if table is not None:
                ph = np.resize(table, n).astype(np.complex64)
            else:
                t = np.arange(n) / self.sample_rate_hz
                ph = np.exp(-1j * 2 * np.pi * self.lo_offset_hz * t).astype(np.complex64)
            cache.clear()
            cache[n] = ph
        return ph

    def drain(self):
        """Blocks until every enqueued waveform has been fully
        transmitted -- call before close()/process exit."""
        self._queue.join()

    def _run(self):
        # If content arrives slower than the link's max fragment rate
        # (e.g. a lower-bitrate source, comfortably within the link's
        # capacity but not filling every fragment slot back-to-back),
        # the queue can genuinely run empty between fragments -- that's
        # normal, not a bug. But a plain blocking queue.get() here would
        # leave the SDR's activated TX stream fed with NOTHING at all
        # during that wait, and an idle activated stream's own hardware
        # DMA/FIFO isn't guaranteed to handle that gracefully -- it can
        # underrun and produce a real, glitchy RF discontinuity instead
        # of clean silence. Confirmed as the actual cause of a reported
        # "TX stalling" specifically at a lower (well within link
        # capacity) bitrate -- lower bitrate content leaves the queue
        # idle more often, which is exactly when this bites. Filling
        # those gaps with explicit zero-valued chunks keeps the hardware
        # continuously fed no matter how sparse the real content is.
        SILENCE_CHUNK_S = 0.02
        silence_chunk = np.zeros(max(1, int(self.sample_rate_hz * SILENCE_CHUNK_S)), dtype=np.complex64)
        # Tracks what fraction of recent airtime was silence-filled rather
        # than real content, purely for visibility -- this path used to be
        # completely silent about it, which made "is the queue starving
        # because the source is genuinely sparse (fine) or because the
        # build/encode pipeline can't keep up with real-time (a real
        # problem, e.g. an undersized --tx-queue-depth)" impossible to
        # tell apart from the TX console alone. Confirmed for real this
        # matters: a receiver seeing constant false-locks on noise with
        # wildly non-sequential fragment numbers traced back to exactly
        # this -- a --tx-queue-depth dropped too low for how long this
        # mode's own waveform build takes, so the SDR was transmitting
        # mostly silence with real frames scattered sparsely through it.
        REPORT_INTERVAL_S = 5.0
        window_start = time.monotonic()
        silence_s_in_window = 0.0
        while True:
            try:
                samples = self._queue.get(timeout=SILENCE_CHUNK_S)
            except queue.Empty:
                self._write_all(silence_chunk)
                silence_s_in_window += SILENCE_CHUNK_S
                now = time.monotonic()
                elapsed = now - window_start
                if elapsed >= REPORT_INTERVAL_S:
                    frac = silence_s_in_window / elapsed
                    if frac > 0.10:
                        print(f"PlutoSDR TX: {frac*100:.0f}% of the last {elapsed:.0f}s was "
                              f"silence-filled (queue underrun) -- the build/encode pipeline "
                              f"isn't keeping up with real-time at this bitrate/occupancy, or "
                              f"--tx-queue-depth is too shallow to absorb its jitter. A receiver "
                              f"will see mostly dead air with real frames sparse in between.",
                              file=sys.stderr)
                    window_start = now
                    silence_s_in_window = 0.0
                continue
            self._write_all(samples)
            self._queue.task_done()

    # LimeSDR: how far ahead of real time the writer may get. LimeSuite
    # accepts many seconds of TX into its buffers without ever blocking, so
    # the start-up burst sat there for the whole session -- measured ~8 s of
    # extra delay end to end. Pacing to the clock keeps the bounded queue
    # (and the --max-input-backlog trim) in charge, as with the Pluto.
    LIME_MAX_AHEAD_S = float(os.environ.get("HF_LIME_MAX_AHEAD_S", "0.25"))

    def _pace(self, n):
        """Lime only: count n samples handed to the driver, and sleep if
        that's more than LIME_MAX_AHEAD_S ahead of the wall clock."""
        now = time.monotonic()
        if getattr(self, "_pace_t0", None) is None:
            self._pace_t0, self._pace_sent = now, 0
        self._pace_sent += n
        ahead = self._pace_sent / self.sample_rate_hz - (now - self._pace_t0)
        if ahead < -1.0:  # fell behind (e.g. an underrun): restart the reference
            self._pace_t0, self._pace_sent = now, n
        elif ahead > self.LIME_MAX_AHEAD_S:
            time.sleep(ahead - self.LIME_MAX_AHEAD_S)

    def _write_all(self, samples):
        if self.driver == "lime":
            self._pace(len(samples))
        pos = 0
        n = len(samples)
        while pos < n:
            chunk = samples[pos:pos + self.buffer_size]
            sr = self.sdr.writeStream(self.stream, [chunk], len(chunk))
            if sr.ret == self._SoapySDR.SOAPY_SDR_TIMEOUT:
                continue
            if sr.ret < 0:
                print(f"SoapySDR writeStream error: "
                      f"{self._SoapySDR.errToStr(sr.ret)} -- dropping the rest of "
                      f"this chunk.", file=sys.stderr)
                break
            pos += sr.ret

    def close(self):
        self.drain()
        self.sdr.deactivateStream(self.stream)
        self.sdr.closeStream(self.stream)


class PlutoRxSource:
    """Duck-types hf_ofdm_rx.py's StdinReader public interface (chunks,
    total_len, _lock, eof, wait_for_more) so it drops straight into the
    existing incremental-buffer decode code with zero changes there --
    reads live IQ from a PlutoSDR via SoapySDR on its own background
    thread instead of stdin, for the same reason StdinReader itself runs
    on its own thread: the hardware's own RX buffer has to keep draining
    in real time no matter how long a decode attempt takes.

    Appends raw complex64 BYTES per chunk (not a numpy array) to
    `.chunks`, matching what StdinReader appends from
    sys.stdin.buffer.read() -- get_rx_raw_incremental and the rest of
    hf_ofdm_rx.py's decode path already know how to consume that shape
    and don't need to know or care where the bytes came from."""

    def __init__(self, freq_hz, sample_rate_hz, rx_gain_db=30.0, agc=False,
                 uri=None, bandwidth_hz=None, buffer_size=1 << 15, stream_bufflen=None,
                 lo_offset_hz=0.0, warmup_discard_s=0.75, tune_offset_hz=None,
                 driver="pluto", antenna=None, gain_file=None, ppm=0.0, freq_offset_file=None):
        """lo_offset_hz: shift received samples back by this much (see _run).
        ppm: RTL-SDR crystal correction (ignored by the other radios).
        tune_offset_hz: tune the actual LO this far from freq_hz (default:
        lo_offset_hz). They differ when a later stage does the shift instead
        -- hf_ofdm_rx.py's compiled front end (rx_frontend.py) passes
        lo_offset_hz=0 and tune_offset_hz=<offset>."""
        import SoapySDR
        from SoapySDR import SOAPY_SDR_RX, SOAPY_SDR_CF32
        if tune_offset_hz is None:
            tune_offset_hz = lo_offset_hz

        self._SoapySDR = SoapySDR
        self.driver = driver

        self.sdr = _open_sdr(driver, uri, "RX")
        if driver == "rtlsdr" and not (225001 <= sample_rate_hz <= 300000 or 900001 <= sample_rate_hz <= 3200000):
            raise ValueError(f"RTL-SDR can't sample at {sample_rate_hz:g} S/s: use 225-300 kS/s or "
                             f"0.9-3.2 MS/s (e.g. --sample-rate 1024000)")
        if driver in AIRSPY_DRIVERS:
            got = _nearest_supported_rate(self.sdr, SOAPY_SDR_RX, sample_rate_hz)
            if got != sample_rate_hz:
                print(f"[rx] {AIRSPY_DRIVERS[driver]}: {sample_rate_hz/1e3:g} kS/s not offered, "
                      f"using {got/1e3:g} kS/s", file=sys.stderr)
            sample_rate_hz = got  # callers read .sample_rate_hz for the real rate
        self.sdr.setSampleRate(SOAPY_SDR_RX, 0, sample_rate_hz)
        # Tune the actual LO lo_offset_hz away from the requested
        # frequency, and digitally shift received samples back by the
        # opposite amount (see _run()) -- see PlutoTxSink's matching
        # comment for why: the AD9363's own DC/LO leakage spike sits at
        # wherever the LO is ACTUALLY tuned, landing right on the signal
        # with no offset. Must match whatever lo_offset_hz TX used, or
        # this correction shifts the signal to the wrong place instead
        # of fixing it.
        self.lo_offset_hz = lo_offset_hz
        self.sample_rate_hz = sample_rate_hz
        self._sample_count = 0
        self.sdr.setFrequency(SOAPY_SDR_RX, 0, freq_hz + tune_offset_hz)
        self._tune_hz = freq_hz + tune_offset_hz  # the LO, before any live offset
        self._freq_offset_hz = 0.0
        if driver == "rtlsdr" and ppm:
            # Older SoapyRTLSDR silently ignores setFrequencyCorrection; its
            # "CORR" frequency component (librtlsdr's ppm) works on all.
            try:
                self.sdr.setFrequencyCorrection(SOAPY_SDR_RX, 0, float(ppm))
            except Exception:
                pass
            self.sdr.setFrequency(SOAPY_SDR_RX, 0, "CORR", float(ppm))
            try:
                got = self.sdr.getFrequency(SOAPY_SDR_RX, 0, "CORR")
            except Exception:
                got = "?"
            print(f"[rx] RTL-SDR ppm correction {ppm:g} (driver reports {got})", file=sys.stderr)
        if driver == "lime":
            _lime_configure(self.sdr, SOAPY_SDR_RX, freq_hz + tune_offset_hz, antenna)
            agc = False  # the LMS7 has no RX AGC
        self.agc = agc
        self.rx_gain_db = rx_gain_db
        if agc:
            self.sdr.setGainMode(SOAPY_SDR_RX, 0, True)
        elif driver == "lime":
            self.sdr.setGain(SOAPY_SDR_RX, 0, rx_gain_db)
        elif driver == "airspyhf":
            # the HF+ has its own AGC and attenuator; leave them in charge
            try:
                self.sdr.setGainMode(SOAPY_SDR_RX, 0, True)
            except Exception:
                pass
        elif driver in ("rtlsdr", "airspy"):
            # manual tuner gain (0-~50 dB in the tuner's own steps; the
            # driver picks the nearest)
            self.sdr.setGainMode(SOAPY_SDR_RX, 0, False)
            self.sdr.setGain(SOAPY_SDR_RX, 0, rx_gain_db)
        else:
            # Explicitly force manual mode before setting a gain value --
            # without this, setGain() alone can be silently overridden by
            # whatever gain control mode the driver already defaulted to
            # (not necessarily manual just because --rx-agc wasn't
            # passed), which is exactly the kind of thing that makes a
            # gain slider look like it's doing nothing.
            self.sdr.setGainMode(SOAPY_SDR_RX, 0, False)
            self.sdr.setGain(SOAPY_SDR_RX, 0, rx_gain_db)
        if bandwidth_hz and driver not in ("rtlsdr",) + tuple(AIRSPY_DRIVERS):
            # the RTL's and Airspys' filters follow the sample rate
            self.sdr.setBandwidth(SOAPY_SDR_RX, 0, bandwidth_hz)

        import hf_ofdm_common
        self.chunks = hf_ofdm_common.ChunkList()  # consumers discard what they've read
        self.total_len = 0
        self._lock = threading.Lock()
        self._new_data = threading.Event()
        self.eof = False
        self._buffer_size = buffer_size

        # The AD9363's own gain-control/DC-offset/quadrature-correction
        # tracking loops need real settling time after the stream is
        # activated (or the gain is changed -- see set_gain below), not
        # just after setGain()/activateStream() return. Confirmed for
        # real: a fresh RX start decoded far worse (BER 0.15-0.30, mostly
        # CRC MISMATCH, EVM 82-90%) than the SAME settings immediately
        # after stopping and restarting just the RX *process* (no power
        # cycle, no antenna/gain change) -- the second run's own first
        # fragments were already clean (BER ~0.09-0.12, EVM 78-83%), so
        # the difference tracked process uptime, not the channel. The most
        # likely explanation is that the FIRST run's early buffers were
        # captured during this settling transient (the driver doesn't
        # expose that as any queryable/waitable state), while a restart
        # soon after tends to land past it purely because Pluto's analog
        # front end was already receiving real signal for a while. Simply
        # discarding a short warmup window of raw samples after every
        # (re)activation removes the transient from what the decoder ever
        # sees, instead of relying on happening to restart late enough.
        self._warmup_discard_s = warmup_discard_s
        self._warmup_samples = max(0, int(round(sample_rate_hz * warmup_discard_s)))

        # See PlutoTxSink's matching "bufflen" comment -- same
        # hardware-side buffer-size stream arg, RX side.
        stream_args = {"bufflen": str(stream_bufflen)} if stream_bufflen and driver == "pluto" else {}
        self.stream = self.sdr.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32, [0], stream_args)
        if gain_file:
            GainFileWatcher(gain_file, self.set_gain)
        self.sdr.activateStream(self.stream)
        if freq_offset_file:
            # Live receive offset from the GUI (Hz): re-tunes the radio. Only
            # now, after set-up: the RTL's ppm correction and the Lime's port
            # set-up re-tune it from the base frequency, which silently undid
            # an offset applied earlier (the stepper showed it, unused).
            GainFileWatcher(freq_offset_file, self.set_freq_offset)
        name = {"lime": "LimeSDR", "rtlsdr": "RTL-SDR", **AIRSPY_DRIVERS}.get(driver, "PlutoSDR")
        print(f"{name} RX active: freq={freq_hz/1e6:.4f}MHz "
              f"(LO tuned to {(freq_hz + tune_offset_hz)/1e6:.4f}MHz, {tune_offset_hz/1e3:+.1f}kHz "
              f"offset) rate={sample_rate_hz/1e3:.1f}kHz "
              f"gain={'AGC' if agc else str(rx_gain_db) + 'dB'}", file=sys.stderr)

        self._thread = threading.Thread(target=self._run, daemon=True, name="rx-sdr-read")
        self._thread.start()

    def _run(self):
        SoapySDR = self._SoapySDR
        buf = np.empty(self._buffer_size, dtype=np.complex64)
        consecutive_timeouts = 0
        while True:
            sr = self.sdr.readStream(self.stream, [buf], len(buf), timeoutUs=1_000_000)
            if sr.ret > 0:
                if consecutive_timeouts >= 5:
                    print(f"PlutoSDR RX: samples resumed after "
                          f"{consecutive_timeouts} second(s) of nothing.", file=sys.stderr)
                consecutive_timeouts = 0
                samples = buf[:sr.ret].copy()
                if self.lo_offset_hz:
                    # Undo the TX-side pre-shift, with a running sample
                    # counter (not reset per-chunk) so the correction
                    # stays phase-continuous across the whole live
                    # stream -- unlike TX, RX has no per-fragment
                    # resync point to anchor a fresh t=0 to. Runs on the
                    # FULL raw buffer, before the warmup discard below, so
                    # this phase reference is unaffected by however many
                    # samples get dropped off the front.
                    n = len(samples)
                    t = (np.arange(n) + self._sample_count) / self.sample_rate_hz
                    samples = (samples * np.exp(1j * 2 * np.pi * self.lo_offset_hz * t)).astype(np.complex64)
                    self._sample_count += n
                if self._warmup_samples > 0:
                    # See __init__'s comment -- silently drop the front of
                    # the stream right after (re)activation/a gain change
                    # instead of ever handing it to the decoder.
                    drop = min(self._warmup_samples, len(samples))
                    samples = samples[drop:]
                    self._warmup_samples -= drop
                    if len(samples) == 0:
                        continue
                chunk = samples.tobytes()
                with self._lock:
                    self.chunks.append(chunk)
                    self.total_len += len(chunk)
                self._new_data.set()
            elif sr.ret == SoapySDR.SOAPY_SDR_TIMEOUT:
                # Silent by itself (a stalled/misconfigured link can sit
                # here forever with NO other symptom -- confirmed for
                # real: readStream just returns TIMEOUT indefinitely,
                # never an error, if the device is open but genuinely
                # not delivering samples), so surface it periodically
                # rather than hanging with zero indication anything's
                # wrong.
                consecutive_timeouts += 1
                if consecutive_timeouts % 5 == 0:
                    print(f"PlutoSDR RX: no samples for {consecutive_timeouts}s -- "
                          f"device is open but not delivering data. Check RF freq/sample "
                          f"rate match TX, antenna/cable connection, and whether the "
                          f"driver needs a FIR filter loaded at this sample rate (see "
                          f"the '[NOTICE] sample rate needs a FIR setting loaded' message "
                          f"at startup, if you saw one).", file=sys.stderr)
                continue
            elif sr.ret == SoapySDR.SOAPY_SDR_OVERFLOW:
                # A real hardware sample drop -- the RX buffer wasn't
                # drained fast enough (e.g. this process got descheduled,
                # or the network link to a remote Pluto briefly choked).
                # Keep going: skipping the lost span is the same
                # situation a lost fragment over the air already is, and
                # there's no return channel here either way.
                print("WARNING: PlutoSDR RX overflow -- samples dropped.", file=sys.stderr)
                continue
            else:
                print(f"PlutoSDR RX stream error: {SoapySDR.errToStr(sr.ret)} -- stopping.",
                      file=sys.stderr)
                with self._lock:
                    self.eof = True
                self._new_data.set()
                return

    def wait_for_more(self, since_len, timeout=None):
        """Blocks until more bytes than since_len have arrived, or EOF."""
        while True:
            with self._lock:
                if self.eof or self.total_len > since_len:
                    return
            self._new_data.clear()
            self._new_data.wait(timeout=timeout)

    def set_freq_offset(self, hz):
        """Shift the receive frequency by hz (live, from the GUI's Offset
        stepper): the LO moves, the digital LO-offset correction stays."""
        from SoapySDR import SOAPY_SDR_RX
        if hz == self._freq_offset_hz:
            return
        self._freq_offset_hz = hz
        self.sdr.setFrequency(SOAPY_SDR_RX, 0, self._tune_hz + hz)
        print(f"RX frequency offset {hz / 1e3:+.0f} kHz", file=sys.stderr)

    def set_gain(self, db):
        """Adjusts real RX gain live, e.g. from a GUI slider -- safe to
        call anytime on an already-activated stream. No-op with a
        warning if AGC is active, since there's no manual gain value for
        a slider to mean anything against then."""
        if self.agc:
            print("WARNING: set_gain() ignored -- AGC is active (--rx-agc), there's no "
                  "manual gain to adjust.", file=sys.stderr)
            return
        if self.driver == "airspyhf":
            return  # runs its own AGC
        if self.driver in ("lime", "rtlsdr", "airspy"):
            # no AGC to fight and no tracking loops to re-settle: just set it
            self.sdr.setGain(self._SoapySDR.SOAPY_SDR_RX, 0, db)
            self.rx_gain_db = db
            return
        self.sdr.setGainMode(self._SoapySDR.SOAPY_SDR_RX, 0, False)  # re-assert manual mode, see __init__
        self.sdr.setGain(self._SoapySDR.SOAPY_SDR_RX, 0, db)
        self.rx_gain_db = db
        # Re-arm the same startup settling discard (see __init__) -- a
        # live gain change (e.g. the GUI slider) perturbs the same
        # gain-control/DC-offset/quadrature tracking loops, just less
        # than a fresh activation. Written here and decremented in _run()
        # on its own thread with no lock -- worst case a gain change
        # lands mid-buffer and this races by a few hundred samples, which
        # only ever means slightly more or less gets discarded, never a
        # correctness issue.
        self._warmup_samples = max(0, int(round(self.sample_rate_hz * self._warmup_discard_s)))

    def close(self):
        self.sdr.deactivateStream(self.stream)
        self.sdr.closeStream(self.stream)
