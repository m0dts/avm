#!/usr/bin/env python3
"""
Combined audio+video counterpart to opus_rx_player.py -- reads
hf_ofdm_rx.py's output (fixed --fragment-size blocks produced by
media_tx_framer.py, see its docstring for the framing design) and both
plays the audio and displays the video. Each block is fully self-
contained TLV records tagged by type, so a lost fragment just drops
whichever audio/video packets happened to be in it -- everything after
resyncs on its own at the next successfully-received fragment, same as
opus_rx_player.py.

Video decode + cv2 display run on their OWN thread, fed through a
bounded queue, instead of inline in the main read/decode loop. Confirmed
for real this matters: with everything on one thread, the player's own
buffer margin (see --stats) drained steadily and substantially (-0.04s
down to -1.59s over about 20 real seconds) even though the RX side was
decoding every fragment on time with zero overruns -- H.264 decode and
cv2.imshow/waitKey time was landing squarely in between audio packets,
delaying every audio_out.write() call behind it. Audio's own real-time
pacing can't "catch up" once delayed like that, so it just falls further
behind, fragment after fragment. Moving video off to its own thread means
a slow video frame can only ever make the PICTURE lag, never steal time
from audio. The queue is bounded and drops (never blocks) when full --
video falling behind is fine (a paused/dropped frame), audio blocking on
a full video queue would recreate the exact problem this exists to fix.

Video specifically can only resume cleanly after a gap once a full
picture refresh has happened (fundamental to how H.264 inter-frame
prediction works, not something framing can work around) -- with a
periodic-keyframe encoder that's the next full I-frame; with an
intra-refresh encoder (see media_tx_framer.py's docstring) it's the
next full rolling-refresh cycle instead, and the picture visibly
"heals" macroblock-row by macroblock-row rather than snapping clean
all at once. Either way, media_tx_framer.py resends VIDEO_CONFIG
(SPS/PPS) both on real keyframes and periodically by packet count, so
a resync is never waiting on a one-time header that already went by.

Usage:
    python hf_ofdm_rx.py --mode B --occupancy 40 --modulation 16qam ... \\
        | python media_rx_player.py --fragment-size 1024

Needs opencv-python (cv2) for the video window, in addition to this
project's existing av/sounddevice dependencies.
"""
import argparse
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import zlib
from collections import Counter, deque
from fractions import Fraction

import av
import numpy as np
import sounddevice as sd


def _log(*args, **kwargs):
    """Every status/warning line in this module goes through here instead
    of a literal print(..., file=sys.stderr) -- standalone CLI use (see
    main()) never touches this function at all, so its behavior there is
    unchanged. media_rx_gui.py, when it runs this module's loop in its
    OWN process instead of launching it as a subprocess (see
    run_player_loop), reassigns this module-level name to a callback that
    routes each line into its own Qt log widget instead -- a single
    swap point rather than threading a logger through every class."""
    print(*args, file=sys.stderr, **kwargs)


RECORD_TIME_BASE = Fraction(1, 90000)  # common MPEG-style time base, shared by both streams

AUDIO_CONFIG_TYPE = 0x01
AUDIO_PACKET_TYPE = 0x02
VIDEO_CONFIG_TYPE = 0x03
VIDEO_PACKET_TYPE = 0x04
VIDEO_PACKET_START_TYPE = 0x05
VIDEO_PACKET_CONT_TYPE = 0x06
WAVELET_CODEC_TAG = b"WVT4"  # VIDEO_CONFIG extradata marking media_source_wavelet.py's stream
AUDIO_PACKET_START_TYPE = 0x07
AUDIO_PACKET_CONT_TYPE = 0x08
AUDIO_PACKET_FIXED_TYPE = 0x0A  # see media_tx_framer.py's own comment on this type
STATION_ID_TYPE = 0x0B  # see media_tx_framer.py's own comment on this type
STATION_ID_LEN = 20
# Every station-ID byte that reaches this script already rode inside a
# fragment whose OWN CRC32 (the payload-level one in hf_ofdm_common.py --
# see decode_frame) already passed, so bit errors within a single byte
# aren't really the risk (a false CRC32 accept is ~1-in-4-billion per
# fragment). What majority-voting over the last few cycles actually
# guards against: two different transmitters sharing the same channel
# (each individually CRC-clean) landing genuinely different byte values
# at the same index, or a straggler byte from a PREVIOUS --station-id
# value still sitting in the buffer right after an operator changes it.
# Either way, voting on the most common of the last STATION_ID_VOTE_CYCLES
# received values per index (ties broken by the most recent) converges on
# whichever value keeps recurring, rather than whatever happened to land
# last.
STATION_ID_VOTE_CYCLES = 4
# If no fragment-sized block arrives at all for this long, treat it as the
# signal having genuinely unlocked (not just an ordinary one-or-two-fragment
# loss) and throw away whatever partial/confirmed station ID was built up --
# a torn mix of bytes from before and after a real outage is more likely to
# be wrong than either generation on its own, and a display stuck on a
# now-stale ID through a long dead patch is misleading. media_rx_player.py
# has no direct visibility into hf_ofdm_rx.py's own sync state (nothing
# crosses the pipe except successfully CRC-passed fragment payloads), so
# this is inferred purely from the gap between consecutive blocks reaching
# stdin at all -- a routine single dropped fragment is nowhere near this
# long (fragments run well under 1s each in every configuration this link
# supports), so a flat multi-second threshold doesn't false-trigger on
# ordinary loss.
STATION_ID_RESET_GAP_S = 3.0
RECORD_HEADER_LEN = 3
# Must match media_tx_framer.py's own AUDIO_PROFILES table exactly --
# see its comment for why this is safe to freeze (OpusHead extradata is
# bit-for-bit identical across bitrate/frame_duration for a given
# sample rate/channel count). AUDIO_CONFIG_TYPE now carries just a
# 1-byte profile ID, looked up here, instead of the full rate+extradata
# blob repeated in-stream.
#
# "codec2" entries decode via a persistent system ffmpeg.exe subprocess
# (Codec2Decoder below), not PyAV -- confirmed for real that PyAV's own
# bundled ffmpeg build has no Codec2 support at all
# (av.CodecContext.create("libcodec2") raises UnknownCodecError even
# though the system ffmpeg binary encodes/decodes it fine), and
# building pycodec2 (direct libcodec2 bindings) failed locally too --
# it needs libcodec2's C headers/library installed standalone, which
# this environment doesn't have (only ffmpeg's bundled copy exists).
AUDIO_PROFILES = {
    0x00: {"codec": "opus", "rate": 48000,
           "extradata": bytes.fromhex("4f707573486561640101380180bb0000000000")},  # 48kHz mono Opus
    0x01: {"codec": "codec2", "rate": 8000, "mode": "3200",
           "frame_bytes": 8, "pcm_bytes": 320},  # 3200bps mode, 20ms frames, 160 samples/frame
}
STATS_INTERVAL_S = 5.0
VIDEO_CATCHUP_FRAMES = 1  # more than this many waiting (beyond one arrival burst): show without pacing
BURST_GAP_S = 0.05  # video packets closer than this arrived in the same fragment (see VideoWorker)
VIDEO_QUEUE_DEPTH = 90  # several seconds' worth of small compressed packets -- see module
# docstring for why full=drop, never block. Sized generously now that VideoWorker paces its
# OWN display to the source frame rate (a deliberately slow, steady drain): the old value (8)
# was sized for the pre-pacing "drains instantly" model, where a queue that shallow never
# built up a backlog. Now an ordinary per-fragment burst of several packets sits in this
# queue for the whole time it takes to display them one at a time -- confirmed for real that
# the small queue started dropping frames (frames_dropped_queue_full) under completely normal
# bursts once pacing made the drain rate deliberately match playback speed instead of racing
# ahead of it.


def read_exact(fh, size):
    """See opus_rx_player.py's identical function -- a plain blocking
    read is safe here because our own framing is fixed-size blocks by
    design, no probing/partial-read ambiguity. fh: any file-like object
    with a .read(n) that blocks for more (sys.stdin.buffer for standalone
    CLI use, or a subprocess's stdout pipe when media_rx_gui.py runs
    run_player_loop in-process against its own hf_ofdm_rx.py subprocess)."""
    buf = bytearray()
    while len(buf) < size:
        chunk = fh.read(size - len(buf))
        if not chunk:
            return bytes(buf) if buf else b""
        buf.extend(chunk)
    return bytes(buf)


def parse_records(data, audio_fixed_len_state):
    """audio_fixed_len_state: a single-key dict {"len": <int or None>}
    tracking the current AUDIO_PACKET_FIXED_TYPE size -- a plain
    parameter captured once wouldn't reflect an update from an
    AUDIO_PACKET_TYPE record EARLIER IN THIS SAME data blob (the common
    case: the very first fragment of a session carries an explicit-
    length packet immediately followed by same-fragment fixed-length
    ones), so this needs to be read live, not snapshotted at call time.
    The caller updates it in place whenever it sees a real packet's true
    length (an explicit AUDIO_PACKET_TYPE, or a completed split) -- see
    media_tx_framer.py's matching AUDIO_PACKET_FIXED_TYPE comment for
    why this is always safe: libopus's CBR output is bit-for-bit the
    same size for every packet at a given bitrate/frame_duration."""
    pos = 0
    while pos < len(data):
        rtype = data[pos]
        if rtype == AUDIO_PACKET_FIXED_TYPE:
            n = audio_fixed_len_state["len"]
            if n is None or pos + 1 + n > len(data):
                break  # never established (or truncated) -- nothing safe to do with it
            yield rtype, data[pos + 1:pos + 1 + n]
            pos += 1 + n
            continue
        if pos + RECORD_HEADER_LEN > len(data):
            break
        rtype, rlen = struct.unpack_from(">BH", data, pos)
        pos += RECORD_HEADER_LEN
        if pos + rlen > len(data):
            break
        if rtype == AUDIO_PACKET_TYPE:
            audio_fixed_len_state["len"] = rlen
        yield rtype, data[pos:pos + rlen]
        pos += rlen


def _boost_current_thread_priority(highest=False):
    """Windows only: raises the CALLING thread's OS scheduling priority
    above normal. Called from VideoWorker's own thread (see _run) for the
    same reason hf_ofdm_rx.py's subprocess already gets HIGH_PRIORITY_CLASS
    from media_rx_gui.py -- confirmed for real (this session) that video
    decode/display, sharing a process with the Qt GUI (spectrum redraws,
    log widget churn) and the audio output thread, stutters in bursts:
    several seconds of smooth decode, then a burst of queue-full drops,
    then smooth again -- classic CPU-starvation-then-catch-up, not a
    data/link problem (the OFDM fragment decode itself can be 100% clean
    throughout).

    `highest=True` (used by AudioPlaybackWorker) asks for
    THREAD_PRIORITY_HIGHEST rather than ABOVE_NORMAL -- confirmed for
    real that ABOVE_NORMAL alone wasn't enough to stop audio buffer
    breaks after the GUI merge put the Qt main thread (video paint,
    spectrum redraws, log widget updates) and the video decode thread in
    the SAME process as audio. PortAudio's own MMCSS "Pro Audio" boost
    (if the active backend even grants it) only covers its internal
    native render thread -- it does nothing for THIS Python thread, whose
    job is to dequeue the next PCM chunk and call the blocking write();
    if THIS thread can't get scheduled promptly under GIL contention from
    the other two, PortAudio's ring buffer still drains and the device
    still audibly underruns regardless of how prioritized its native
    render thread is. An audible dropout is worse than a dropped video
    frame, so audio outranks video here instead of matching it. A no-op,
    not an error, on any other OS."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        THREAD_PRIORITY_ABOVE_NORMAL = 1
        THREAD_PRIORITY_HIGHEST = 2
        priority = THREAD_PRIORITY_HIGHEST if highest else THREAD_PRIORITY_ABOVE_NORMAL
        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), priority)
    except Exception:
        pass  # cosmetic -- decode/display still works fine without the boost


def _majority_vote(history):
    """Most common value in `history` (a deque of recently received bytes
    for one station-ID buffer index); ties go to whichever tied value was
    received most recently."""
    counts = Counter(history)
    best = max(counts.values())
    tied = {v for v, c in counts.items() if c == best}
    for v in reversed(history):
        if v in tied:
            return v


class VideoWorker:
    """Owns every video-side object (decoder, display, reassembly state)
    and touches them from exactly one thread -- its own -- so the main
    thread's audio path can hand off work via a bounded, non-blocking
    queue and never wait on video decode/render time. See the module
    docstring for the real drain this fixes.

    Rendering is either a cv2 window (cv2_module given -- standalone CLI
    use) or a plain callback (frame_sink given -- media_rx_gui.py's
    combined single-window mode, which owns its own Qt widget and just
    wants each decoded BGR frame handed to it). Exactly one of the two
    should be given; frame_sink takes priority if somehow both are."""

    def __init__(self, cv2_module=None, record=None, frame_sink=None, config_sink=None):
        self.cv2 = cv2_module
        self.frame_sink = frame_sink
        self.config_sink = config_sink  # optional callable(width, height), called once per CONFIG
        self.record = record  # optional dict: {"container", "lock", "start_t", "video_stream"}
        self.queue = queue.Queue(maxsize=VIDEO_QUEUE_DEPTH)
        self.frames_decoded = 0
        self.frames_dropped_queue_full = 0
        self.packets_dropped_decode_error = 0
        # packets_received counts a full, reassembled access unit REACHING
        # the decoder (i.e. every _decode_packet call) -- separate from
        # frames_decoded (which only counts what the decoder actually
        # handed back and got displayed). If the two track together, a
        # variable/low fps is a SUPPLY problem (packets simply aren't
        # arriving faster -- the TX-side encoder/framer/link, not this
        # thread); if packets_received stays steady while frames_decoded
        # lags behind it, the problem is in decode/pacing here instead.
        # configured_fps is whatever the last VIDEO_CONFIG record said the
        # source's own frame rate is, for comparing against BOTH of the
        # above -- see media_rx_gui.py's video stats readout.
        self.packets_received = 0
        self.configured_fps = None
        self._decoder = None
        self._wavelet = False  # True once a WAVELET_CODEC_TAG config selects wavelet_codec.py
        self._config_key = None  # (width, height, extradata) the decoder was built for
        self._window_open = False
        self._reassembly = None
        # Paces display to the source's own frame rate instead of
        # showing every decoded frame the instant it's decoded -- a
        # fragment can carry a whole burst of video packets at once (the
        # channel only delivers data once per fragment), so decoding
        # them all immediately produces the exact "clump of frames, then
        # nothing" pattern that made this project add a byte-level pacer
        # (StdoutPacer) and a prebuffer for audio; video had neither.
        # None until a CONFIG record supplies a real frame rate, in
        # which case frames display immediately as before (matches
        # pre-pacing behavior for an older TX that doesn't send one).
        self._frame_interval_s = None
        self._next_display_t = None
        # Frames per arrival burst: a slow mode delivers one fragment every
        # ~0.75 s carrying several frames at once. That many queued is the
        # normal state between fragments, not a backlog to rush through --
        # see _decode_packet. Decays slowly back down if bursts shrink.
        self._burst = 0.0
        self._burst_n = 0
        self._burst_t = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="rx-video")
        self._thread.start()

    def submit(self, kind, payload):
        if kind in ("packet", "packet_start"):
            now = time.monotonic()
            if now - self._burst_t > BURST_GAP_S:  # previous burst complete
                n = self._burst_n
                self._burst = n if n > self._burst else self._burst + 0.25 * (n - self._burst)
                self._burst_n = 0
            self._burst_n += 1
            self._burst_t = now
        try:
            self.queue.put_nowait((kind, payload))
        except queue.Full:
            self.frames_dropped_queue_full += 1
            # Logged in bursts, not per-drop (a real stall drops many
            # packets in a row, and a per-drop _log call would itself add
            # more work for the very thread that's already falling
            # behind) -- see _boost_current_thread_priority's docstring
            # for the failure mode this is diagnosing. Lets the log
            # timeline show exactly when/how often congestion happens,
            # to correlate against spectrum updates, RX fragment lines,
            # etc. happening around the same time.
            if self.frames_dropped_queue_full % 20 == 1:
                _log(f"[video] queue full -- {self.frames_dropped_queue_full} packet(s) dropped so far "
                     f"(this thread is falling behind real time)")

    def queue_depth(self):
        """Current backlog -- for an external GUI's own health/fps
        readout (see media_rx_gui.py). Approximate (queue.Queue.qsize()
        is documented as such under concurrent access) but plenty
        precise for a once-a-second display."""
        return self.queue.qsize()

    def stop(self):
        self.queue.put(("stop", None))
        self._thread.join(timeout=2.0)

    def _record_packet(self, raw):
        """Stream-copies this already-compressed H.264 packet into the
        recording container, if one is active -- muxes the exact
        received bytes, no re-encoding, timestamped from wall-clock
        arrival time on the SAME shared clock origin as audio's own
        recorded packets (see main()'s record["start_t"]) for reasonable
        (not sample-accurate, but not fixed-up-after-the-fact either) A/V
        sync in the recorded file. Runs on this thread; the container
        itself is shared with the main (audio) thread, so every mux call
        goes through record["lock"]."""
        if self.record is None or self.record.get("video_stream") is None:
            return
        pkt = av.Packet(bytes(raw))
        pkt.stream = self.record["video_stream"]
        pts = int((time.monotonic() - self.record["start_t"]) / RECORD_TIME_BASE)
        pkt.pts = pkt.dts = pts
        pkt.time_base = RECORD_TIME_BASE
        with self.record["lock"]:
            try:
                self.record["container"].mux(pkt)
            except av.error.FFmpegError as e:
                _log(f"[record] video mux error: {e}")

    def _decode_packet(self, raw):
        self.packets_received += 1
        if self._wavelet:
            try:
                images = [self._decoder.decode_bgr(bytes(raw), self.cv2)]
            except Exception as e:  # never let one bad packet kill this thread
                self.packets_dropped_decode_error += 1
                if self.packets_dropped_decode_error % 20 == 1:
                    _log(f"[video] wavelet decode error: {e!r}")
                return
        else:
            self._record_packet(raw)
            try:
                images = (f.to_ndarray(format="bgr24") for f in self._decoder.decode(av.Packet(bytes(raw))))
            except av.error.InvalidDataError:
                self.packets_dropped_decode_error += 1
                return
        for img in images:
            if self._frame_interval_s:
                now = time.monotonic()
                # Resync instead of catching up if display has fallen far
                # behind (e.g. this thread was starved for a while) --
                # same reasoning as StdoutPacer's own MAX_SCHEDULE_LAG_S:
                # a real gap should just resume at the live frame, not
                # dump a burst of stale ones back-to-back.
                # Also: a frame later than its slot (the gap before the next
                # fragment's burst) restarts the schedule from now; owing
                # that gap would rush the burst's first frames.
                if self._next_display_t is None or now > self._next_display_t:
                    self._next_display_t = now
                target = self._next_display_t
                remaining = target - time.monotonic()
                # Catch up when frames have piled up (e.g. after a slow
                # first decode): pacing at exactly the source rate could
                # never drain a backlog -- it grew to the queue limit and
                # video fell ~7 s behind audio. With more than one frame
                # queued, show without waiting until it's back down. (The
                # old ~0.5 s threshold left a standing 3-frame queue --
                # 0.375 s of delay at 8 fps -- that never drained.)
                # A fragment's own burst of frames isn't a backlog: pace
                # those (no rush then a freeze); only frames beyond it are.
                # A queue between bursts plays slightly fast (3%) so a
                # TX clock a touch faster than ours can't build up delay.
                queued = self.queue.qsize()
                # (capped at 1 s of frames: a dump after a dropout is a backlog)
                burst = min(self._burst, 1.0 / self._frame_interval_s)
                backlog = queued > VIDEO_CATCHUP_FRAMES + round(burst)
                if backlog:
                    target = time.monotonic()
                elif remaining > 0:
                    time.sleep(remaining)
                self._next_display_t = target + self._frame_interval_s * (0.97 if queued else 1.0)
            if self.frame_sink is not None:
                self.frame_sink(img)
            else:
                self.cv2.imshow("hf_ofdm_rx -- video", img)
                self.cv2.waitKey(1)  # pumps the window's event loop; does not block playback
            self.frames_decoded += 1

    def _run(self):
        _boost_current_thread_priority()
        while True:
            kind, payload = self.queue.get()
            if kind == "stop":
                break
            elif kind == "config":
                width, height, extradata, framerate = payload
                if framerate > 0:
                    self._frame_interval_s = 1.0 / framerate
                    self.configured_fps = framerate
                # Only create the decoder ONCE, on the first CONFIG
                # record -- media_tx_framer.py resends this periodically
                # (not just on real keyframes, needed for intra-refresh
                # encoding where there may be no more keyframes after the
                # first), and rebuilding an already-running decoder
                # throws away its reference-frame state, which the many
                # inter-predicted frames intra-refresh relies on then
                # can't decode at all. Confirmed for real: rebuilding on
                # every periodic resend dropped ~70% of frames.
                #
                # But DO rebuild when the config itself changes -- the
                # transmitter restarted with a different resolution or
                # codec. Keeping the first decoder meant a TX resolution
                # change was never picked up (a wavelet decoder of the old
                # size can't decode the new stream at all) until the RX was
                # restarted. Periodic resends are identical, so they still
                # never trigger this.
                config_key = (width, height, bytes(extradata))
                if self._decoder is not None and config_key != self._config_key:
                    _log(f"[video] stream changed to {width}x{height} "
                         f"{'wavelet' if extradata.startswith(WAVELET_CODEC_TAG) else 'h264'} "
                         f"-- new decoder")
                    self._decoder = None
                    self._wavelet = False
                    self._reassembly = None
                    self._window_open = False  # re-announce the new size to the display
                self._config_key = config_key
                if (self._decoder is None and extradata.startswith(b"WVT")
                        and not extradata.startswith(WAVELET_CODEC_TAG)):
                    # A different wavelet bitstream version: decoding it would
                    # only show garbage. Say so (once per config) instead.
                    if getattr(self, "_version_warned", None) != config_key:
                        self._version_warned = config_key
                        _log(f"[video] transmitter's wavelet codec is {bytes(extradata[:4]).decode(errors='replace')},"
                             f" this receiver decodes {WAVELET_CODEC_TAG.decode()} -- update TX and RX to the same "
                             f"AVM version. No video until then.")
                    continue
                if self._decoder is None:
                    if extradata.startswith(WAVELET_CODEC_TAG):
                        from wavelet_codec import WaveletCodec
                        self._decoder = WaveletCodec(width, height, 64)
                        self._wavelet = True
                    else:
                        self._decoder = av.CodecContext.create("h264", "r")
                        self._decoder.extradata = extradata
                # NOTE: video is deliberately NOT recorded (see
                # _record_packet, which no-ops without a video_stream) --
                # confirmed for real that muxing these raw Annex-B H.264
                # packets (start-code-prefixed, exactly what our own
                # decoder wants) directly into an MKV video stream crashed
                # the whole player process with no catchable Python
                # exception at all -- a native FFmpeg-level crash, almost
                # certainly because container muxers generally expect
                # H.264 extradata/packets in the structured avcC format,
                # not raw Annex-B, and feeding Annex-B directly is a
                # known corruption/crash source. Needs a proper bitstream
                # filter conversion and real hardware testing before
                # re-enabling -- audio-only recording is safe and stays on.
                if not self._window_open:
                    if self.config_sink is not None:
                        self.config_sink(width, height)
                    elif self.cv2 is not None:
                        self.cv2.namedWindow("hf_ofdm_rx -- video", self.cv2.WINDOW_NORMAL)
                        self.cv2.resizeWindow("hf_ofdm_rx -- video", width, height)
                    self._window_open = True
            elif kind == "packet":
                self._reassembly = None  # a lone packet mid-split means the split was interrupted
                if self._decoder is not None:
                    self._decode_packet(payload)
            elif kind == "packet_start":
                total_len, first_chunk = payload
                self._reassembly = {"total_len": total_len, "buf": bytearray(first_chunk)}
            elif kind == "packet_cont":
                if self._reassembly is None:
                    continue  # missed the START chunk (lost fragment) -- nothing to append to
                self._reassembly["buf"].extend(payload)
                if len(self._reassembly["buf"]) >= self._reassembly["total_len"]:
                    complete = bytes(self._reassembly["buf"][:self._reassembly["total_len"]])
                    self._reassembly = None
                    if self._decoder is not None:
                        self._decode_packet(complete)
        if self._window_open and self.cv2 is not None:
            self.cv2.destroyAllWindows()


class Codec2Decoder:
    """Decodes Codec2 frames via a persistent system ffmpeg.exe
    subprocess instead of PyAV (see AUDIO_PROFILES's own comment on
    why). Codec2's frame->PCM ratio is exact and constant per mode
    (unlike a general audio codec), so the reader thread just needs to
    keep pulling pcm_bytes_per_frame-sized chunks off ffmpeg's stdout
    -- no packet/frame boundary detection needed on the way out."""

    def __init__(self, mode, sample_rate, pcm_bytes_per_frame, on_pcm):
        self.pcm_bytes_per_frame = pcm_bytes_per_frame
        self.on_pcm = on_pcm
        # Diagnostic only -- see close()'s own comment on why this exists.
        self.frames_submitted = 0
        self.frames_emitted = 0
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-f", "codec2raw", "-mode", mode, "-i", "-",
             "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="codec2-dec")
        self._thread.start()

    def decode(self, raw_frame):
        self.frames_submitted += 1
        try:
            self.proc.stdin.write(raw_frame)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass  # ffmpeg exited -- nothing useful to do, _read_loop below will also just end

    def _read_loop(self):
        buf = bytearray()
        while True:
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                break
            buf.extend(chunk)
            while len(buf) >= self.pcm_bytes_per_frame:
                pcm = bytes(buf[:self.pcm_bytes_per_frame])
                del buf[:self.pcm_bytes_per_frame]
                self.frames_emitted += 1
                self.on_pcm(np.frombuffer(pcm, dtype="<i2").reshape(-1, 1))

    def close(self):
        """Closes ffmpeg's stdin (its cue there's no more input) and
        waits for it to flush any still-decoding frames back through
        _read_loop before returning -- called at shutdown so the final
        few frames of a session aren't silently lost to the decoder
        subprocess getting killed mid-flight (decode is asynchronous
        relative to this class's own decode() calls)."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self._thread.join(timeout=2)
        if self.frames_submitted != self.frames_emitted:
            _log(f"[audio] codec2 decoder: {self.frames_submitted} frame(s) submitted, "
                 f"only {self.frames_emitted} emitted as PCM -- "
                 f"{self.frames_submitted - self.frames_emitted} lost inside the decode "
                 f"subprocess (backlog never caught up before shutdown)")


class _Stretch:
    """Plays int16 PCM a little faster or slower (ratio = input samples per
    output sample, e.g. 1.003) by linear interpolation, carrying the read
    position and last sample across calls so chunk joins stay seamless.
    ratio 1.0 passes audio through untouched."""

    def __init__(self):
        self._prev = None   # last input sample (row) of the previous chunk
        # read position of the next output sample, in [prev] + chunk (index
        # 0 = prev); 1.0 = "just after prev", i.e. nothing pending
        self._phase = 1.0

    def process(self, pcm, ratio):
        x = np.asarray(pcm)
        if len(x) == 0:
            return pcm
        if ratio == 1.0 and self._phase == 1.0:
            # in step: pass through untouched
            self._prev = x[-1].astype(np.float64).reshape(-1)
            return pcm
        xf = x.astype(np.float64).reshape(len(x), -1)
        if self._prev is None:
            xf = np.concatenate([xf[:1], xf])  # first call: treat sample 0 as the "previous"
        else:
            xf = np.concatenate([self._prev.reshape(1, -1), xf])
        n = len(xf)
        pos = np.arange(self._phase, n - 1, ratio)
        if len(pos) == 0:
            self._phase -= n - 1
            self._prev = xf[-1]
            return x[:0]
        idx = np.arange(n)
        out = np.stack([np.interp(pos, idx, xf[:, c]) for c in range(xf.shape[1])], axis=1)
        self._phase = pos[-1] + ratio - (n - 1)
        self._prev = xf[-1]
        out = np.clip(np.round(out), -32768, 32767).astype(np.int16)
        return out.reshape(-1) if x.ndim == 1 else out


def _use_pw_cat(device):
    """Play through PipeWire's own pw-cat instead of PortAudio when the
    output is the system default and PipeWire is there. On a busy Pi 4,
    PortAudio -> PipeWire's ALSA plugin crackled (clean decoded audio,
    PipeWire reporting no xruns) where a native PipeWire client playing at
    the same time stayed clean. HF_RX_AUDIO_PWCAT=0 turns this off."""
    import shutil
    if os.environ.get("HF_RX_AUDIO_PWCAT", "1") == "0" or not shutil.which("pw-cat"):
        return False
    if not os.environ.get("XDG_RUNTIME_DIR") or not os.path.exists(
            os.path.join(os.environ["XDG_RUNTIME_DIR"], "pipewire-0")):
        return False
    if device is None:
        return True
    try:
        name = sd.query_devices(device)["name"] if isinstance(device, int) else str(device)
    except Exception:
        return False
    return name.split(" ")[0].lower() in ("default", "pipewire", "pulse", "sysdefault")


class _PwCatStream:
    """Just enough of sd.OutputStream for AudioPlaybackWorker, backed by a
    pw-cat process reading raw s16 PCM on stdin. Writes block on the pipe,
    which pw-cat drains at the playback rate -- the same pacing a blocking
    PortAudio write gives. The pipe is shrunk to ~16 KB (~0.17 s mono at
    48 kHz): Linux's default 64 KB would add the better part of a second."""
    F_SETPIPE_SZ = 1031

    def __init__(self, sample_rate, channels):
        self._p = subprocess.Popen(
            ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", str(int(sample_rate)),
             "--channels", str(int(channels)), "--latency", "100ms", "--media-category", "Playback", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            import fcntl
            fcntl.fcntl(self._p.stdin.fileno(), self.F_SETPIPE_SZ, 16384)
        except (ImportError, OSError):
            pass

    def start(self):
        pass

    def write(self, pcm):
        if self._p.poll() is not None:
            raise sd.PortAudioError(f"pw-cat exited ({self._p.returncode})")
        try:
            self._p.stdin.write(np.ascontiguousarray(pcm, np.int16).tobytes())
            self._p.stdin.flush()
        except OSError as e:
            raise sd.PortAudioError(f"pw-cat pipe: {e}")
        return False  # underflows, if any, are PipeWire's own (pw-top ERR)

    def stop(self):
        try:
            self._p.stdin.close()
            self._p.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            self._p.kill()

    def abort(self):
        self._p.kill()

    def close(self):
        if self._p.poll() is None:
            self._p.kill()
        try:
            self._p.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


class AudioPlaybackWorker:
    """Owns the actual audio OUTPUT device and every blocking write() to
    it, on its own thread -- exactly the same fix VideoWorker above
    already applies to video, and for the identical reason (see this
    file's own module docstring): running audio device I/O inline on the
    main read/decode loop means every write() call's real-time blocking
    directly delays reading the NEXT stdin fragment. Confirmed for real
    this is a genuine bug, not just theory: a 100%-clean, zero-RF pipe
    loopback test (ffmpeg sine tone -> media_tx_framer.py ->
    media_rx_player.py, no hf_ofdm_tx/rx or radio involved at all) still
    showed a persistent -0.02s to -0.05s buffer margin and an audible
    live blip, THE ENTIRE TIME the recorded output file's decoded
    content was verified bit-for-bit continuous (a 440Hz test tone with
    zero amplitude dips or phase discontinuities anywhere except the
    expected end-of-stream tail). That rules out the framer/reassembly/
    decode path entirely -- the bug is purely in this single-threaded
    loop's own coupling between reading input and writing output.
    Moving the actual device write() calls here, fed through a queue,
    means a momentary decode/read hiccup can only ever delay how far
    AHEAD the queue is, never directly stall the device itself, and vice
    versa: a slow write() can no longer block reading the next fragment.

    Also owns the pre-roll buffering (hold PCM until --latency seconds
    have accumulated before actually starting the device) and the live
    buffer-margin tracking, since both concern exactly when real
    playback started and how far ahead of real time it is -- now
    something only this thread can answer, since the main thread no
    longer touches the device at all."""

    def __init__(self, latency_target_s, device, low_warning_s):
        self.latency_target_s = latency_target_s
        self.device = device
        self.low_warning_s = low_warning_s
        # Unbounded on purpose: PCM chunks are tiny (a few KB/s at most),
        # and applying backpressure here would silently recreate the
        # exact main-thread stall this class exists to remove.
        self.queue = queue.Queue()
        self.decoded_total_s = 0.0
        self.start_wall_t = None
        self._prebuffer = []
        self._prebuffer_s = 0.0
        self._stream = None
        self._sample_rate = None
        self._channels = None
        self._last_warn_t = 0.0
        # True once a real break (margin actually hit 0, not just the
        # LOW warning threshold) has been detected -- while set, incoming
        # PCM is held in _prebuffer instead of written, exactly like the
        # initial startup pre-roll, so playback resumes with a fresh
        # cushion instead of limping along right at (or below) zero
        # margin indefinitely, glitching on every subsequent hiccup.
        self._recovering = False
        self._stopping = False
        self._open_failed = False
        # Fixed-latency playback (see _hold_latency)
        self._queued_s = 0.0          # decoded audio waiting in self.queue
        self._backlog_ema = None
        self._stretch = _Stretch()
        self._sync_started_t = None   # playback start, for the one-off start-up trim
        self._sync_trimmed = False
        self._sync_log_t = 0.0
        self._sync_rate_sum = self._sync_rate_n = 0
        period_s = max(0.25, latency_target_s - 0.25)  # the GUIs set latency = fragment period + 0.25
        self.sync_target_s = float(os.environ.get("HF_RX_AUDIO_BACKLOG_S", max(0.2, 0.6 * period_s + 0.05)))
        self._thread = threading.Thread(target=self._run, daemon=True, name="rx-audio")
        self._thread.start()

    def submit(self, pcm, sample_rate, channels):
        self._queued_s += len(pcm) / sample_rate
        self.queue.put((pcm, sample_rate, channels))
        dump = os.environ.get("HF_RX_AUDIO_DUMP")  # diagnostics: raw PCM as decoded, before the device
        if dump:
            try:
                with open(dump, "ab") as f:
                    f.write(np.ascontiguousarray(pcm, np.int16).tobytes())
            except OSError:
                pass

    def stop(self):
        """Asks the playback thread to finish and close the device. The
        stream is only ever touched by that thread: this used to close it
        from here after a 2 s join timeout, while the playback thread could
        still be inside a blocking write() (a post-break refill writes the
        whole latency's worth in one go) -- PortAudio use-after-free, seen
        as "Invalid stream pointer" or the whole GUI process dying on Stop.
        Writes are chunked (see _write) so the thread notices quickly."""
        self._stopping = True
        self.queue.put(None)
        self._thread.join(timeout=3.0)

    def margin_s(self):
        """None until real playback has actually started."""
        if self.start_wall_t is None:
            return None
        return self.decoded_total_s - (time.monotonic() - self.start_wall_t)

    def _open_stream(self):
        if _use_pw_cat(self.device):
            self._stream = _PwCatStream(self._sample_rate, self._channels)
            _log("[audio] output via pw-cat (native PipeWire)")
            return
        self._stream = sd.OutputStream(samplerate=self._sample_rate, channels=self._channels,
                                        dtype="int16", latency="high", device=self.device)
        self._stream.start()

    def _write(self, pcm):
        """Writes to the device, surviving a PortAudioError instead of
        letting it kill this whole (daemon) thread outright. Confirmed
        for real: an uncaught exception here used to silently end all
        further playback for the rest of the process's life -- decode
        and recording kept working fine (they don't touch this thread at
        all), so nothing else looked wrong, but the user just stopped
        hearing anything with no error surfaced anywhere. One reopen
        attempt covers a transient device hiccup (the common case); if
        that also fails, this chunk of audio is dropped but the thread
        stays alive to keep trying on the next one, rather than going
        silent for good.

        Written in WRITE_CHUNK_S pieces, checking for stop() between them,
        so a multi-second refill can't hold up shutdown."""
        step = max(1, int(self._sample_rate * self.WRITE_CHUNK_S))
        for i in range(0, len(pcm), step):
            if self._stopping:
                return
            self._write_chunk(pcm[i:i + step])

    WRITE_CHUNK_S = 0.1

    UNDERFLOW_LOG_S = 5.0

    def _note_underflow(self, underflowed):
        """The device itself ran dry before this write (an audible click) --
        separate from the decoded-vs-wall-clock margin above, which can
        look healthy while the OS buffer briefly empties (e.g. this thread
        held up by the GIL). Counted, logged at most every UNDERFLOW_LOG_S."""
        if underflowed:
            self._underflows = getattr(self, "_underflows", 0) + 1
        now = time.monotonic()
        if getattr(self, "_underflows", 0) and now - getattr(self, "_underflow_log_t", 0.0) >= self.UNDERFLOW_LOG_S:
            _log(f"[audio] output device underflow x{self._underflows} in the last "
                 f"{self.UNDERFLOW_LOG_S:g} s (clicks)")
            self._underflows = 0
            self._underflow_log_t = now

    def _write_chunk(self, pcm):
        try:
            self._note_underflow(self._stream.write(pcm))
            return
        except sd.PortAudioError as e:
            _log(f"[audio] output stream error ({e}) -- reopening device")
        try:
            try:
                self._stream.close()
            except sd.PortAudioError:
                pass
            self._open_stream()
            self._stream.write(pcm)
        except sd.PortAudioError as e:
            _log(f"[audio] failed to reopen output stream ({e}) -- dropping this chunk")

    def _run(self):
        _boost_current_thread_priority(highest=True)
        try:
            self._loop()
        finally:
            # Only this thread ever touches the stream -- see stop().
            if self._stream is not None:
                try:
                    self._stream.abort() if self._stopping else self._stream.stop()
                    self._stream.close()
                except sd.PortAudioError:
                    pass  # already dead (e.g. device unplugged) -- nothing left to clean up

    # Fixed-latency playback. Audio used to play "pre-roll, then free-run":
    # how much ended up queued depended on where the first packets fell, so
    # the audio delay -- and with it the A/V offset -- differed every run,
    # and the mic's and sound card's clocks drifting apart crept it further
    # until a break or overflow reset it. Instead, hold the audio waiting in
    # this player's queue at sync_target_s: pw-cat's pipe / the device buffer
    # downstream of it fill to a fixed size and then block at the playback
    # rate, so this queue is exactly the variable part of the delay.
    SYNC_TAU_S = 2.0          # smoothing of the (bursty: one fragment at a time) backlog
    SYNC_GAIN = 0.1           # rate correction per second of backlog error (0.5% at 50 ms) ...
    SYNC_MAX_ADJ = 0.005      # ... capped at +-0.5% (inaudible)
    SYNC_SETTLE_S = 2.0       # after this long playing, trim any start-up excess once
    SYNC_LOG_S = 5.0

    def _hold_latency(self, pcm, frame_s):
        now = time.monotonic()
        if self._sync_started_t is None:
            self._sync_started_t = now
        backlog = self._queued_s  # audio still waiting behind this chunk
        a = min(1.0, frame_s / self.SYNC_TAU_S)
        self._backlog_ema = backlog if self._backlog_ema is None else self._backlog_ema + a * (
            backlog - self._backlog_ema)
        err = self._backlog_ema - self.sync_target_s
        if (not self._sync_trimmed and now - self._sync_started_t >= self.SYNC_SETTLE_S):
            self._sync_trimmed = True
            if err > 0.05:
                # one-off: drop the start-up excess now rather than spend a
                # minute playing it off at 0.5% -- a single small skip, early
                drop = err
                _log(f"[audio sync] start-up: {drop:.2f}s more queued than the {self.sync_target_s:.2f}s "
                     f"target -- skipping it")
                while drop > 0:
                    try:
                        item = self.queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is None:
                        self.queue.put(None)
                        break
                    d = len(item[0]) / item[1]
                    self._queued_s = max(0.0, self._queued_s - d)
                    drop -= d
                self._backlog_ema -= err - max(0.0, drop)
                return None
        # >0: too much queued -> play a touch fast (fewer output samples)
        adj = max(-self.SYNC_MAX_ADJ, min(self.SYNC_MAX_ADJ, err * self.SYNC_GAIN))
        self._sync_rate_sum += adj
        self._sync_rate_n += 1
        if now - self._sync_log_t >= self.SYNC_LOG_S:
            if self._sync_log_t:
                _log(f"[audio sync] queued {self._backlog_ema:.3f}s (target {self.sync_target_s:.2f}s), "
                     f"rate {100 * self._sync_rate_sum / max(1, self._sync_rate_n):+.2f}%")
            self._sync_log_t = now
            self._sync_rate_sum = self._sync_rate_n = 0
        return self._stretch.process(pcm, 1.0 + adj)

    def _restart_for_format(self, sample_rate, channels):
        """The audio codec changed mid-stream (Codec2 8 kHz <-> Opus 48 kHz):
        the device stream was opened for the old rate -- writing the new
        PCM into it played at the wrong speed (heard as garbage/nothing).
        Close it and pre-roll afresh, exactly like the very first start."""
        _log(f"[audio] format changed {self._sample_rate}Hz/{self._channels}ch -> "
             f"{sample_rate}Hz/{channels}ch: reopening output")
        if self._stream is not None:
            try:
                self._stream.abort()
                self._stream.close()
            except sd.PortAudioError:
                pass
        self._stream = None
        self._sample_rate, self._channels = sample_rate, channels
        self._prebuffer = []  # old-format audio can't go to the new stream
        self._prebuffer_s = 0.0
        self.decoded_total_s = 0.0
        self.start_wall_t = None
        self._recovering = False
        self._open_failed = False
        self._stretch = _Stretch()
        self._backlog_ema = self._sync_started_t = None
        self._sync_trimmed = False

    def _loop(self):
        while not self._stopping:
            item = self.queue.get()
            if item is None:
                break
            pcm, sample_rate, channels = item
            if self._sample_rate is None:
                self._sample_rate, self._channels = sample_rate, channels
            elif (sample_rate, channels) != (self._sample_rate, self._channels):
                self._restart_for_format(sample_rate, channels)  # also retries a failed open
            frame_s = len(pcm) / sample_rate
            self._queued_s = max(0.0, self._queued_s - frame_s)
            if self._open_failed:
                continue  # no usable device; keep draining so submit() never backs up
            if self._stream is None or self._recovering:
                # Still pre-rolling (either the very first start, or
                # refilling after a detected break): hold this frame's
                # audio rather than play it yet, so playback (re)starts
                # with a real head-start buffer instead of resuming right
                # at zero margin, where the very next small hiccup just
                # breaks again.
                self._prebuffer.append(pcm)
                self._prebuffer_s += frame_s
                if self._stream is None:
                    self.decoded_total_s += frame_s
                if self._prebuffer_s < self.latency_target_s:
                    continue
                # "high" (a larger device buffer): confirmed for real
                # that "low" forced many more, smaller write() calls,
                # each carrying its own OS wake/scheduling overhead (the
                # same coarse-Windows-timer overshoot StdoutPacer's own
                # 20ms-chunk sleeps already had to work around), which
                # compounded into a steady growing delay. A raw
                # multi-second custom latency value is worse still --
                # confirmed for real that negotiating a buffer that
                # large made sd.OutputStream(...).start() itself block
                # for several real seconds. "high" is a bounded preset:
                # opens fast, but needs far fewer/larger write() calls.
                if self._stream is None:
                    self._sample_rate, self._channels = sample_rate, channels
                    try:
                        self._open_stream()
                    except sd.PortAudioError as e:
                        # e.g. a raw ALSA hw: device (Pi HDMI) that can't
                        # take 8 kHz mono int16 -- used to kill this thread
                        # with an unlogged traceback and play nothing.
                        self._stream = None
                        self._open_failed = True
                        _log(f"[audio] can't open output device {self.device!r} ({e}) -- NO AUDIO. "
                             f"Pick another device (a plug/default one, e.g. 'tv' or 'default', "
                             f"not a raw hw: one).")
                        continue
                    _log(f"Playing audio: {sample_rate}Hz, {channels} channel(s) "
                          f"(pre-buffered {self._prebuffer_s:.2f}s)")
                else:
                    _log(f"[buffer] recovered: refilled {self._prebuffer_s:.2f}s -- "
                          f"resuming playback")
                    self.decoded_total_s = self._prebuffer_s
                self._write(np.concatenate(self._prebuffer, axis=0))
                self.start_wall_t = time.monotonic()
                self._prebuffer = []
                self._prebuffer_s = 0.0
                self._recovering = False
                continue

            pcm = self._hold_latency(pcm, frame_s)
            if pcm is None:
                continue  # dropped in the one-off start-up trim
            self._write(pcm)
            self.decoded_total_s += len(pcm) / sample_rate
            margin = self.margin_s()
            if margin < 0:
                # A real break, not just the LOW warning below -- margin
                # actually ran out, so the device has underrun or is
                # underrunning right now. Re-buffering here (instead of
                # continuing to feed it just-in-time, pinned at/below
                # zero) is what lets it recover to a healthy margin
                # instead of glitching on every subsequent small hiccup
                # indefinitely.
                _log(f"[buffer] BREAK: {margin:.2f}s behind -- refilling "
                      f"{self.latency_target_s:.2f}s before resuming playback")
                self._recovering = True
                continue
            if margin < self.low_warning_s:
                now = time.monotonic()
                if now - self._last_warn_t > 1.0:  # don't spam every single packet while it's low
                    _log(f"[buffer] LOW: {margin:.2f}s of audio margin left "
                          f"(latency={self.latency_target_s:.2f}s) -- a break is imminent or already "
                          f"happening")
                    self._last_warn_t = now


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fragment-size", type=int, required=True,
                     help="Must match the --fragment-size given to hf_ofdm_tx.py/hf_ofdm_rx.py.")
    ap.add_argument("--latency", type=float, default=3.0,
                     help="PortAudio output buffer target in seconds (default 3.0 -- confirmed for "
                          "real that BREAKs happen with 0% actual data loss at the OFDM layer, so "
                          "this is purely about absorbing accumulated real-time latency drift across "
                          "the TX encode/framer/RX decode pipeline, not lost/starved content; see "
                          "media_rx_gui.py's matching spinbox default for the full story).")
    ap.add_argument("--device", type=str, default=None,
                     help="Audio output device name or index (default: system default).")
    ap.add_argument("--no-video", action="store_true",
                     help="Ignore video records entirely (audio-only playback, no cv2 needed).")
    ap.add_argument("--stats", action="store_true",
                     help="Every 5s, print input rate and audio/video decode-vs-real-time ratios "
                          "-- see opus_rx_player.py's --stats for what a ratio below 1.0 means.")
    ap.add_argument("--record-to", default=None,
                     help="Also stream-copy the received AUDIO packets (no re-encoding) into this "
                          "file (container format inferred from its extension, e.g. .mkv) as they "
                          "arrive, alongside live playback. Video is NOT recorded (only displayed "
                          "live) -- muxing raw Annex-B H.264 straight into a container crashed the "
                          "whole process with no catchable exception at all (needs a bitstream "
                          "filter conversion to the avcC format containers expect; not done yet). "
                          "Timestamps are wall-clock-derived, not sample-accurate.")
    args = ap.parse_args()
    device = int(args.device) if args.device is not None and args.device.isdigit() else args.device

    record = None
    if args.record_to:
        record = {"container": av.open(args.record_to, mode="w"), "lock": threading.Lock(),
                   "start_t": time.monotonic(), "video_stream": None}
        _log(f"Recording audio to {args.record_to} (video not recorded, see --help)")

    video = None
    if not args.no_video:
        import cv2 as _cv2
        video = VideoWorker(_cv2, record=record)

    # Once margin drops this close to empty, warn regardless of --stats.
    # See AudioPlaybackWorker for why the device write() and margin
    # tracking both live on their own thread now.
    LOW_BUFFER_WARNING_S = 0.2
    audio_worker = AudioPlaybackWorker(args.latency, device, LOW_BUFFER_WARNING_S)

    run_player_loop(sys.stdin.buffer, args, video, audio_worker, record)


def run_player_loop(fh, args, video, audio_worker, record=None):
    """The actual read/decode/playback loop -- extracted out of main() so
    media_rx_gui.py can run it directly against its own hf_ofdm_rx.py
    subprocess's stdout pipe (fh), in-process, instead of launching this
    whole script as a second subprocess piped after the first. Standalone
    CLI use (main(), above) is unchanged: it just calls this once with
    sys.stdin.buffer. video/audio_worker/record are built by the caller
    (main() or media_rx_gui.py) since what they're configured with -- a
    cv2 window vs. a Qt frame_sink, a real output device vs. one picked
    by the GUI -- differs between the two callers."""
    audio_decoder = None  # set for an "opus" profile (a PyAV CodecContext)
    codec2_decoder = None  # set for a "codec2" profile (a Codec2Decoder -- see its own docstring)
    audio_profile_id = None  # AUDIO_PROFILES key the current decoder was built for
    audio_channels = None
    blocks_read = 0
    audio_packets_decoded = 0
    audio_packets_dropped = 0

    start_t = time.monotonic()
    last_stats_t = start_t
    bytes_since_stats = 0
    audio_s_since_stats = 0.0
    video_frames_prev = 0

    audio_rec_samples = 0  # cumulative decoded sample count, for the recording's own packet pts
    audio_reassembly = None  # in-progress AUDIO_PACKET_START/CONT split, if any (see media_tx_framer.py)
    audio_fixed_len_state = {"len": None}  # see parse_records's own docstring

    # Station ID: reassembled from the serial 1-byte/fragment stream (see
    # media_tx_framer.py's STATION_ID_TYPE comment). Indexed writes mean a
    # lost fragment only ever costs its one byte position -- the next
    # cycle's same-index byte overwrites it on its own, no explicit
    # "restart the buffer" logic needed. station_id_shown only tracks what
    # was last printed, so a steady, unchanging ID doesn't spam the log
    # every ~5s cycle.
    station_id_buf = bytearray(STATION_ID_LEN + 4)
    station_id_history = [deque(maxlen=STATION_ID_VOTE_CYCLES) for _ in range(STATION_ID_LEN + 4)]
    station_id_shown = None
    station_id_tentative_shown = None  # last-printed pre-CRC guess, so reprints only on an actual change
    last_block_t = None  # see STATION_ID_RESET_GAP_S below

    def _on_codec2_pcm(pcm):
        # Runs on Codec2Decoder's own reader thread, not this loop's --
        # matches how AudioPlaybackWorker.submit() is always called from
        # off-thread already, so this is nothing new for that class.
        # NOTE: unlike Opus, Codec2 audio is NOT recorded via --record-to
        # yet (would need its own container-friendly encoding of the raw
        # frames; not done, same "known gap" treatment as video recording
        # got until its own bitstream-filter work happens).
        nonlocal audio_s_since_stats, audio_packets_decoded
        audio_s_since_stats += len(pcm) / codec2_sample_rate[0]
        audio_packets_decoded += 1
        audio_worker.submit(pcm, codec2_sample_rate[0], 1)

    codec2_sample_rate = [None]  # mutable cell -- set once, read from _on_codec2_pcm's closure

    def _process_audio_packet(raw):
        """Decodes, plays, and (if --record-to) records one COMPLETE
        Opus packet -- called either directly for an ordinary
        AUDIO_PACKET_TYPE record, or with the reassembled bytes once an
        AUDIO_PACKET_START/CONT split completes. Factored out of the main
        loop so both paths share identical handling. For a "codec2"
        profile, delegates to Codec2Decoder instead (see its docstring
        and AUDIO_PROFILES's comment for why it's a separate path)."""
        nonlocal audio_decoder, audio_channels
        nonlocal audio_s_since_stats
        nonlocal audio_packets_decoded, audio_packets_dropped, audio_rec_samples
        if codec2_decoder is not None:
            codec2_decoder.decode(raw)
            return
        try:
            frames = audio_decoder.decode(av.Packet(raw))
        except av.error.InvalidDataError:
            audio_packets_dropped += 1
            frames = []
        if record is not None and record.get("audio_stream") is not None:
            # Stream-copy the exact received compressed packet -- recorded
            # regardless of whether OUR local decode above succeeded, same
            # reasoning as video's own _record_packet: recording shouldn't
            # depend on successful local playback. pts is the cumulative
            # REAL decoded sample count up to (not including) this packet
            # -- accurate and exactly what Ogg/Opus expects, unlike a
            # wall-clock approximation.
            pkt = av.Packet(raw)
            pkt.stream = record["audio_stream"]
            pkt.pts = pkt.dts = audio_rec_samples
            pkt.time_base = record["audio_stream"].time_base
            with record["lock"]:
                try:
                    record["container"].mux(pkt)
                except av.error.FFmpegError as e:
                    _log(f"[record] audio mux error: {e}")
            audio_rec_samples += sum(f.samples for f in frames)
        for frame in frames:
            if audio_channels is None:
                audio_channels = len(frame.layout.channels)
            pcm = frame.to_ndarray().reshape(-1, audio_channels)
            audio_s_since_stats += len(pcm) / frame.sample_rate
            audio_packets_decoded += 1
            # Hand off to AudioPlaybackWorker's own thread -- a plain
            # queue.put(), essentially instant, so decode/read never
            # blocks on real-time device I/O. See that class's docstring
            # for why this decoupling is the actual fix, not the device
            # buffer size or pre-buffer target (both still configurable,
            # just no longer entangled with this loop's own pacing).
            audio_worker.submit(pcm, frame.sample_rate, audio_channels)

    _log("Waiting for framed audio+video blocks on stdin...")
    while True:
        block = read_exact(fh, args.fragment_size)
        if not block:
            break
        blocks_read += 1
        bytes_since_stats += len(block)

        now_t = time.monotonic()
        if last_block_t is not None and now_t - last_block_t > STATION_ID_RESET_GAP_S:
            gap_s = now_t - last_block_t
            had_state = station_id_shown is not None or any(station_id_history)
            station_id_buf[:] = bytes(len(station_id_buf))
            for h in station_id_history:
                h.clear()
            station_id_shown = None
            station_id_tentative_shown = None
            if had_state:
                _log(f"Station ID: signal gap of {gap_s:.1f}s -- resetting "
                      f"(discarding any partial/confirmed ID)")
        last_block_t = now_t

        if args.stats:
            now = time.monotonic()
            elapsed = now - last_stats_t
            if elapsed >= STATS_INTERVAL_S:
                input_kbps = bytes_since_stats * 8 / elapsed / 1000
                audio_ratio = audio_s_since_stats / elapsed if elapsed > 0 else 0.0
                video_frames_now = video.frames_decoded if video is not None else 0
                fps = (video_frames_now - video_frames_prev) / elapsed if elapsed > 0 else 0.0
                video_frames_prev = video_frames_now
                margin = audio_worker.margin_s()
                margin_note = f"  buffer_margin={margin:+.2f}s" if margin is not None else ""
                _log(f"[stats] input={input_kbps:.2f}kbps  "
                      f"audio_decoded/real_time={audio_ratio:.2f}x"
                      f"{'  <-- FALLING BEHIND' if audio_ratio < 0.97 else ''}"
                      f"{margin_note}  "
                      f"video={fps:.1f}fps")
                last_stats_t = now
                bytes_since_stats = 0
                audio_s_since_stats = 0.0

        if len(block) < 2:
            continue
        real_len = struct.unpack_from(">H", block, 0)[0]
        payload = block[2:2 + real_len]

        # media_tx_framer.py always emits a split's continuation in the
        # VERY NEXT fragment-sized block, never later -- so if a
        # reassembly already pending BEFORE this block didn't grow by the
        # end of it, this link's own known loss mode (no return channel,
        # confirmed all night via "sequence gap" warnings) ate whichever
        # OFDM fragment should have carried it. Discard it right here
        # rather than let it keep waiting: confirmed for real that
        # leaving a stale reassembly around let an UNRELATED later
        # split's own CONT chunk get absorbed into it instead, splicing
        # two different packets' bytes together and feeding the Opus
        # decoder outright garbage -- audible as glitches, not a clean
        # single dropped packet the way loss used to look before
        # splitting existed.
        reassembly_before = audio_reassembly
        buf_len_before = len(audio_reassembly["buf"]) if audio_reassembly else None

        for rtype, rpayload in parse_records(payload, audio_fixed_len_state):
            if rtype == AUDIO_CONFIG_TYPE and len(rpayload) >= 1:
                profile = AUDIO_PROFILES.get(rpayload[0])
                if profile is None:
                    _log(f"[warn] unknown audio profile ID {rpayload[0]} -- add it to "
                          f"AUDIO_PROFILES (must match media_tx_framer.py's table)")
                    continue
                # The current AUDIO_PACKET_FIXED_TYPE slot length rides
                # along in every CONFIG record too (not just the profile
                # ID) -- relearning it here, from literally every
                # fragment, is what lets a receiver that joins mid-
                # session (TX has almost always already switched to the
                # no-length FIXED type by then) actually decode any
                # FIXED record at all, instead of only an explicit-
                # length one it may never see again. A real, confirmed
                # bug otherwise: late join decoded nothing.
                if len(rpayload) >= 3:
                    slot_len = struct.unpack_from(">H", rpayload, 1)[0]
                    if slot_len > 0:
                        audio_fixed_len_state["len"] = slot_len
                # TX changed audio codec/mode (Codec2 <-> Opus, or another
                # Codec2 mode/Opus rate) without RX restarting: drop the old
                # decoder so the one for the new profile gets built below --
                # it used to keep the first one forever (garbage/silence).
                if audio_profile_id is not None and rpayload[0] != audio_profile_id:
                    _log(f"Audio codec changed (profile {audio_profile_id} -> {rpayload[0]}): "
                         f"switching decoder")
                    if codec2_decoder is not None:
                        codec2_decoder.close()
                    codec2_decoder = None
                    audio_decoder = None
                    audio_channels = None
                    audio_reassembly = None
                audio_profile_id = rpayload[0]
                if profile["codec"] == "codec2":
                    if codec2_decoder is None:
                        codec2_sample_rate[0] = profile["rate"]
                        codec2_decoder = Codec2Decoder(
                            profile["mode"], profile["rate"], profile["pcm_bytes"], _on_codec2_pcm)
                        _log(f"Audio config received: Codec2 mode {profile['mode']}, "
                              f"{profile['rate']}Hz")
                    continue  # no PyAV decoder/recording setup needed for this codec
                rate, extradata = profile["rate"], profile["extradata"]
                if audio_decoder is None:
                    audio_decoder = av.CodecContext.create("libopus", "r")
                    audio_decoder.extradata = extradata
                    audio_decoder.sample_rate = rate
                    _log(f"Audio config received: {rate}Hz, extradata={len(extradata)} bytes")
                if record is not None and record.get("audio_stream") is None:
                    with record["lock"]:
                        aus = record["container"].add_stream("opus", rate=rate)
                        aus.codec_context.extradata = extradata
                        # Ogg/Opus muxing is sample-count based (RFC 7845's
                        # granule position), not wall-clock based -- the
                        # muxer needs the stream's time_base to be exactly
                        # 1/rate to interpret packet pts correctly.
                        # Confirmed for real: using an arbitrary shared
                        # time_base here (matching video's, fine for MKV)
                        # made the Ogg-Opus muxer specifically reject
                        # nearly every packet with EINVAL.
                        aus.time_base = Fraction(1, rate)
                        record["audio_stream"] = aus

            elif (rtype in (AUDIO_PACKET_TYPE, AUDIO_PACKET_FIXED_TYPE)
                  and (audio_decoder is not None or codec2_decoder is not None)):
                audio_reassembly = None  # a lone packet mid-split means the split was interrupted
                _process_audio_packet(bytes(rpayload))

            elif (rtype == AUDIO_PACKET_START_TYPE and len(rpayload) >= 4
                  and (audio_decoder is not None or codec2_decoder is not None)):
                total_len = struct.unpack_from(">I", rpayload, 0)[0]
                audio_reassembly = {"total_len": total_len, "buf": bytearray(rpayload[4:])}

            elif rtype == AUDIO_PACKET_CONT_TYPE and (audio_decoder is not None or codec2_decoder is not None):
                if audio_reassembly is None:
                    continue  # missed the START chunk (lost fragment) -- nothing to append to
                audio_reassembly["buf"].extend(rpayload)
                if len(audio_reassembly["buf"]) >= audio_reassembly["total_len"]:
                    complete = bytes(audio_reassembly["buf"][:audio_reassembly["total_len"]])
                    audio_reassembly = None
                    # A completed split's real length is just as valid a
                    # "last known audio packet size" as an ordinary
                    # explicit-length packet -- see parse_records's own
                    # docstring on why AUDIO_PACKET_FIXED_TYPE needs this
                    # kept current.
                    audio_fixed_len_state["len"] = len(complete)
                    _process_audio_packet(complete)

            elif rtype == STATION_ID_TYPE and len(rpayload) >= 2:
                idx, data_byte = rpayload[0], rpayload[1]
                if idx < len(station_id_buf):
                    station_id_history[idx].append(data_byte)
                    station_id_buf[idx] = _majority_vote(station_id_history[idx])
                    crc_expected = struct.unpack_from(">I", station_id_buf, STATION_ID_LEN)[0]
                    crc_actual = zlib.crc32(bytes(station_id_buf[:STATION_ID_LEN])) & 0xFFFFFFFF
                    if crc_actual == crc_expected:
                        station_id = bytes(station_id_buf[:STATION_ID_LEN]).decode("ascii", "replace")
                        if station_id != station_id_shown:
                            station_id_shown = station_id
                            station_id_tentative_shown = None
                            _log(f"Station ID: '{station_id}'")
                    elif station_id_shown is None:
                        # Only shown before the FIRST successful decode -- once
                        # a good ID has been shown, keep displaying it through
                        # any later reacquire glitch rather than flashing back
                        # to an unconfirmed one (see media_rx_gui.py's matching
                        # sticky-label behavior). Show the CURRENT best guess
                        # (whatever's sitting in the buffer right now, un-
                        # received positions and all) rather than a bare "no
                        # valid ID yet" -- lets the operator watch it fill in
                        # and read a mostly-right ID well before the last
                        # straggler byte needed for CRC to actually pass
                        # arrives, on a link losing enough fragments that a
                        # full clean cycle takes a long time. Only reprinted
                        # when the guess actually changes, not on every byte.
                        tentative = bytes(station_id_buf[:STATION_ID_LEN]).decode("ascii", "replace")
                        if tentative != station_id_tentative_shown:
                            station_id_tentative_shown = tentative
                            _log(f"Station ID: '{tentative}' (unconfirmed -- CRC not yet valid)")

            elif rtype == VIDEO_CONFIG_TYPE and len(rpayload) >= 6 and video is not None:
                width, height, fps_x1000 = struct.unpack_from(">HHH", rpayload, 0)
                extradata = bytes(rpayload[6:])
                framerate = fps_x1000 / 1000.0
                video.submit("config", (width, height, extradata, framerate))

            elif rtype == VIDEO_PACKET_TYPE and video is not None:
                video.submit("packet", rpayload)

            elif rtype == VIDEO_PACKET_START_TYPE and video is not None and len(rpayload) >= 4:
                total_len = struct.unpack_from(">I", rpayload, 0)[0]
                video.submit("packet_start", (total_len, rpayload[4:]))

            elif rtype == VIDEO_PACKET_CONT_TYPE and video is not None:
                video.submit("packet_cont", rpayload)

        if (reassembly_before is not None and audio_reassembly is reassembly_before
                and len(audio_reassembly["buf"]) == buf_len_before):
            audio_reassembly = None

    if codec2_decoder is not None:
        codec2_decoder.close()
    audio_worker.stop()
    if video is not None:
        video.stop()
    if record is not None:
        record["container"].close()
        _log(f"Recording closed: {args.record_to}")
    video_frames_decoded = video.frames_decoded if video is not None else 0
    video_packets_dropped = video.packets_dropped_decode_error if video is not None else 0
    video_dropped_queue_full = video.frames_dropped_queue_full if video is not None else 0
    _log(f"Stream ended: read {blocks_read} block(s), "
          f"decoded {audio_packets_decoded} audio packet(s) ({audio_packets_dropped} dropped), "
          f"decoded {video_frames_decoded} video frame(s) ({video_packets_dropped} decode errors, "
          f"{video_dropped_queue_full} dropped because the video queue was full).")


if __name__ == "__main__":
    main()
