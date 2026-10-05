#!/usr/bin/env python3
"""
Combined audio+video version of opus_tx_framer.py -- see that file's
docstring for the full rationale on why a custom TLV framing replaces
a real container on this link (late-join needing a one-time header
forever, and a single lost fragment tearing arbitrary container
structure). Same fix here, extended to two streams instead of one:
every record is tagged with a 1-byte type so hf_ofdm_rx.py's opaque
fixed-size fragments can carry a mix of audio and video without either
stream needing to know the other exists.

Owns TWO independent ffmpeg-wrapping source processes itself (given as
full command strings via --video-source-cmd/--audio-source-cmd,
launched here rather than piped in from outside) and reads each one's
RAW elementary stream (H.264 Annex-B for video, Ogg-wrapped Opus
packets for audio) on its own thread -- not one Matroska container
demuxed by PyAV, which this project used until now. That container
approach worked, but hard-blocked any codec its muxer doesn't have a
tag for -- confirmed for real that ffmpeg's Matroska muxer flatly
refuses Codec2 audio ("No wav codec tag found for codec codec2") even
though ffmpeg encodes Codec2 fine standalone. Reading each stream's own
raw format removes the container as an integration point entirely, so
adding a new codec later only needs its own small elementary-stream
reader, not a container-muxer entry.

The two source threads share this file's existing
append_audio_packet/append_video_packet/flush_block/_fresh_block
logic unchanged (a single threading.Lock guards the shared `block`
they build together) -- output on stdout is byte-for-byte the same
fragment-sized TLV stream as before, so hf_ofdm_tx.py and everything
downstream needs no changes at all.

Usage (paired with media_source_video.py/media_source_audio.py):
    python media_tx_framer.py --fragment-size 1024 \\
        --video-source-cmd "python media_source_video.py --resolution 160x120 --framerate 12" \\
        --video-framerate 12 \\
        --audio-source-cmd "python media_source_audio.py --audio-bitrate 10" \\
    | python hf_ofdm_tx.py --mode A --occupancy 80 --modulation qpsk \\
          --fragment-size 1024 --fragment-gap-ms 0 --stream ...

Encoding notes (strict CBR + intra-refresh, not a periodic full
keyframe): x264's intra-refresh spreads intra-coded macroblocks evenly
across every frame instead of periodically inserting one huge full
I-frame -- avoids the multi-KB keyframe spike that used to need
VIDEO_PACKET_START/CONT splitting (see below; kept as a safety net,
but shouldn't normally trigger under this encoding). The tradeoff:
with intra-refresh there is no longer a single frame that's fully
self-decodable on its own -- ffmpeg/x264 typically only marks the
very first frame as a real keyframe (confirmed for real against a raw
capture: SPS/PPS appear exactly once, at the very start, never
repeated even with x264's repeat-headers=1), so VIDEO_CONFIG can't
rely on "resend on every keyframe" alone for late-join robustness
anymore. See VIDEO_CONFIG_REPEAT_EVERY below: it's resent periodically
by packet count too, same idea as audio's CONFIG record riding along
in every fragment, regardless of which encoding style is in use.
"""
import argparse
import os
import queue
import shlex
import struct
import subprocess
import sys
import threading
import time
import zlib

import avm_threads


def _split_cmdline(cmdline):
    """shlex.split()'s default POSIX mode treats backslash as an escape
    character, which mangles ordinary Windows paths (e.g.
    "C:\\Users\\rob\\python.exe" loses backslashes entirely) --
    confirmed for real: every source subprocess failed to launch with
    FileNotFoundError until this was fixed. posix=False leaves
    backslashes alone but also leaves quote characters IN the tokens
    (needed for spaces in a device name like "Integrated Camera"), so
    strip a token's own surrounding quotes afterward."""
    if os.name != "nt":
        return shlex.split(cmdline)
    tokens = shlex.split(cmdline, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in tokens]


def quote_cmdline(argv):
    """Inverse of _split_cmdline -- builds a single command-line string
    from an argv list (e.g. for media_tx_gui.py to pass as
    --video-source-cmd/--audio-source-cmd), quoting only tokens that
    need it (contain whitespace) so plain paths stay exactly as-is."""
    return " ".join(f'"{a}"' if any(c.isspace() for c in a) else a for a in argv)

AUDIO_CONFIG_TYPE = 0x01
AUDIO_PACKET_TYPE = 0x02
VIDEO_CONFIG_TYPE = 0x03
VIDEO_PACKET_TYPE = 0x04
VIDEO_PACKET_START_TYPE = 0x05  # first chunk of a video packet too big for one fragment
VIDEO_PACKET_CONT_TYPE = 0x06   # later chunk(s) of the same
AUDIO_PACKET_START_TYPE = 0x07  # first chunk of an audio packet split across fragments
AUDIO_PACKET_CONT_TYPE = 0x08   # later chunk(s) of the same
AUDIO_PACKET_FIXED_TYPE = 0x0A  # like AUDIO_PACKET_TYPE, but no length field at all --
# confirmed for real (this session): libopus's CBR (--vbr off) output is
# bit-for-bit IDENTICAL in size for every packet at a given bitrate/
# frame_duration, not just close -- 501-667 packets sampled per
# bitrate/frame_duration combination, zero variance. So once one real
# packet's length is known (an ordinary AUDIO_PACKET_TYPE record, which
# still carries an explicit length), every SUBSEQUENT packet of that
# same length can drop the 2-byte length field entirely -- the receiver
# already knows it from the last one. Saves 2 of the usual 3 header
# bytes on nearly every audio packet, for free, with automatic fallback
# to the explicit-length type for anything that doesn't match (a real
# bitrate change, or in principle a rare non-CBR-conforming frame).
VIDEO_CONFIG_REPEAT_EVERY = 25  # video packets (~2s at 12.5fps) -- see the intra-refresh note above
RECORD_HEADER_LEN = 3           # 1 byte type + 2 byte length, per TLV record

STATION_ID_TYPE = 0x0B  # next free record type after AUDIO_PACKET_FIXED_TYPE (0x0A)
# Station/operator identification (e.g. an amateur radio callsign), carried
# as a fixed 20-byte ASCII string + a 4-byte CRC32 over those 20 bytes.
# Rather than spend a whole 24-byte record in one fragment (real cost on
# every single fragment, forever, for something that only needs to arrive
# once every few seconds -- see the header's own no-free-space discussion),
# send it SERIALLY: one [index, data_byte] pair per fragment, 2 bytes of
# payload (5 bytes on the wire with the TLV header) cycling through all 24
# bytes of id+crc over 24 fragments. Explicitly indexed (not just appended
# in order) so a lost fragment only ever costs that one byte position, not
# the whole cycle -- the next cycle's same-index byte overwrites it, and
# the CRC only needs to pass once every 24 fragments' worth of bytes
# happens to all be current, not on every single cycle.
STATION_ID_LEN = 20


def _build_station_id_payload(station_id: str) -> bytes:
    """Encode a station ID string into the fixed 24-byte [20-byte ASCII
    string, space-padded/truncated][4-byte big-endian CRC32] payload that
    gets streamed 1 byte/fragment by _fresh_block below."""
    raw = station_id.encode("ascii")
    if len(raw) > STATION_ID_LEN:
        print(f"WARNING: --station-id '{station_id}' is {len(raw)} bytes, truncating to "
              f"{STATION_ID_LEN}.", file=sys.stderr)
        raw = raw[:STATION_ID_LEN]
    else:
        raw = raw.ljust(STATION_ID_LEN, b" ")
    crc = zlib.crc32(raw) & 0xFFFFFFFF
    return raw + struct.pack(">I", crc)

# Frozen at build time -- confirmed for real (this session) that Opus's
# OpusHead extradata is bit-for-bit identical across every bitrate and
# --audio-frame-duration combination tested, for a given sample rate/
# channel count: it only ever encodes sample rate/channels/mapping,
# never bitrate or frame timing. Carrying this SAME frozen table on
# both TX (here) and RX (media_rx_player.py's matching AUDIO_PROFILES)
# means a receiver never needs the full ~26-byte config blob (4-byte
# rate + ~19-byte extradata) repeated in-stream at all -- just this
# 1-byte ID, which is cheap enough to seed into literally EVERY block
# (see flush_block below) instead of only periodically, dropping
# late-join config latency from "up to several packets late" to "the
# very next fragment", at a fraction of the old per-occurrence cost.
# Add an entry here (and the matching one in media_rx_player.py) before
# using any audio format not already listed.
#
# "codec2" entries: no extradata concept (unlike Opus) -- profile
# identity is just (codec, mode). frame_bytes is fixed by the Codec2
# spec per mode (confirmed for real: 3200bps mode = exactly 8
# bytes/20ms frame, headerless, via ffmpeg's `-f codec2raw` muxer).
AUDIO_PROFILES = {
    0x00: {"codec": "opus", "rate": 48000,
           "extradata": bytes.fromhex("4f707573486561640101380180bb0000000000")},  # 48kHz mono Opus
    0x01: {"codec": "codec2", "rate": 8000, "mode": "3200",
           "frame_bytes": 8, "pcm_bytes": 320},  # 3200bps mode, 20ms frames, 160 samples/frame
}


def read_h264_units(pipe):
    """Reads a raw Annex-B H.264 elementary stream (ffmpeg's `-f h264`
    output -- start-code-delimited NALs, NOT the length-prefixed avcC
    format a container like Matroska stores), yielding (nal_bytes,
    nal_type) for each NAL unit in order. nal_bytes includes the 1-byte
    NAL header; the 3- or 4-byte start code itself is stripped.

    A NAL's true end is only known once the NEXT start code (or EOF)
    appears, so this buffers across read1() calls rather than assuming
    one call returns a whole NAL -- ffmpeg trickles bytes in slowly on
    a live/real-time encode, same reasoning as the old StdinIO class
    this replaces."""
    buf = b""
    while True:
        chunk = pipe.read1(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            start = buf.find(b"\x00\x00\x01")
            if start == -1:
                break
            next_start = buf.find(b"\x00\x00\x01", start + 3)
            if next_start == -1:
                break  # this NAL might not be complete yet -- wait for more data
            nal = buf[start + 3:next_start]
            if nal.endswith(b"\x00"):
                nal = nal[:-1]  # trailing byte of a 4-byte start code (00 00 00 01)
            if nal:
                yield nal, nal[0] & 0x1F
            buf = buf[next_start:]
    start = buf.find(b"\x00\x00\x01")  # final trailing NAL at EOF, no following start code
    if start != -1:
        nal = buf[start + 3:]
        if nal.endswith(b"\x00"):
            nal = nal[:-1]
        if nal:
            yield nal, nal[0] & 0x1F


def read_ogg_opus_packets(pipe):
    """Reads a raw Ogg-Opus stream (ffmpeg's `-f ogg` output) page by
    page, yielding each contained Opus packet's raw bytes in order.
    The first two packets are always OpusHead and OpusTags (RFC 7845)
    -- the caller must treat those specially, not as audio.

    Standard Ogg page format: "OggS" + version(1) + header_type(1) +
    granule_position(8) + serial(4) + seq(4) + checksum(4) +
    page_segments(1) + segment_table(page_segments) + payload. A
    packet spans segments until one shorter than 255 bytes ends it
    (a run of 255-byte segments means the packet continues on the
    NEXT page)."""
    buf = bytearray()

    def _read_at_least(n):
        while len(buf) < n:
            chunk = pipe.read1(65536)
            if not chunk:
                return False
            buf.extend(chunk)
        return True

    pending = bytearray()
    while True:
        if not _read_at_least(27):
            return
        if buf[:4] != b"OggS":
            del buf[0:1]  # resync -- shouldn't happen with ffmpeg's own output
            continue
        page_segments = buf[26]
        if not _read_at_least(27 + page_segments):
            return
        seg_table = bytes(buf[27:27 + page_segments])
        header_len = 27 + page_segments
        payload_len = sum(seg_table)
        if not _read_at_least(header_len + payload_len):
            return
        payload = bytes(buf[header_len:header_len + payload_len])
        del buf[0:header_len + payload_len]

        pos = 0
        for seg_len in seg_table:
            pending.extend(payload[pos:pos + seg_len])
            pos += seg_len
            if seg_len < 255:
                yield bytes(pending)
                pending = bytearray()


def read_codec2_frames(pipe, frame_bytes):
    """Reads ffmpeg's raw, headerless `-f codec2raw` output -- fixed
    frame_bytes-sized frames back-to-back, no framing/header of any
    kind (confirmed for real: 3200bps mode produces exactly 8
    bytes/20ms frame, byte-for-byte). Buffers across read1() calls the
    same way read_h264_units/read_ogg_opus_packets do, since a live
    encode trickles bytes in rather than delivering whole frames per
    read."""
    buf = bytearray()
    while True:
        while len(buf) < frame_bytes:
            chunk = pipe.read1(65536)
            if not chunk:
                return  # a partial trailing frame at EOF is discarded, not padded/guessed at
            buf.extend(chunk)
        yield bytes(buf[:frame_bytes])
        del buf[:frame_bytes]


WAVELET_CODEC_TAG = b"WVT4"  # VIDEO_CONFIG extradata for media_source_wavelet.py's stream


def read_length_prefixed(pipe):
    """media_source_wavelet.py's output: [2-byte big-endian length][packet]
    records, one per frame."""
    buf = bytearray()
    while True:
        while len(buf) >= 2:
            n = struct.unpack_from(">H", buf, 0)[0]
            if len(buf) < 2 + n:
                break
            yield bytes(buf[2:2 + n])
            del buf[:2 + n]
        chunk = pipe.read1(65536)
        if not chunk:
            return
        buf.extend(chunk)


def _popen_from_cmdline(cmdline):
    return subprocess.Popen(_split_cmdline(cmdline), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **avm_threads.die_with_parent())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fragment-size", type=int, required=True,
                     help="Must match the --fragment-size given to hf_ofdm_tx.py.")
    ap.add_argument("--fragment-period-s", type=float, default=0.24,
                     help="How often (real seconds) assembler_tick() drains the audio/video queues "
                          "and packs+sends whatever's accumulated -- should match hf_ofdm_tx.py's own "
                          "real on-air duration per fragment (fragment_size * 8 / effective_bitrate_bps) "
                          "as closely as practical; media_tx_gui.py computes and passes the real value "
                          "for the current mode/occupancy/modulation/fec/fragment-size combination. "
                          "Doesn't need to be exact -- it only sets the cadence audio/video get "
                          "interleaved at, not the real transmit timing (hf_ofdm_tx.py's own stdin "
                          "read still paces actual transmission) -- but a period much shorter than the "
                          "true on-air duration just wastes CPU on tiny, mostly-empty ticks, and one "
                          "much longer reintroduces some of the burstiness this whole mechanism exists "
                          "to remove (default 0.24, matching this project's typical Mode A configs).")
    ap.add_argument("--video-source-cmd", type=str, default=None,
                     help="Full command line for the video source (e.g. media_source_video.py), "
                          "launched and read by this script directly -- omit for audio-only.")
    ap.add_argument("--video-framerate", type=float, default=0.0,
                     help="The video source's own frame rate, for the VIDEO_CONFIG record (see "
                          "media_rx_player.py's VideoWorker display pacing). Not derivable from "
                          "the raw H.264 stream itself, so this must match --video-source-cmd's "
                          "own --framerate. Required if --video-source-cmd is given.")
    ap.add_argument("--video-resolution", type=str, default=None,
                     help="WxH, for the VIDEO_CONFIG record -- width/height aren't cheaply "
                          "recoverable from the raw NAL stream without a real SPS bit-parser, so "
                          "this must match --video-source-cmd's own --resolution. Required if "
                          "--video-source-cmd is given.")
    ap.add_argument("--video-codec", choices=["h264", "wavelet"], default="h264",
                     help="What --video-source-cmd emits: 'h264' (media_source_video.py, Annex-B) "
                          "or 'wavelet' (media_source_wavelet.py, exact-size intra frames).")
    ap.add_argument("--audio-source-cmd", type=str, default=None,
                     help="Full command line for the audio source (e.g. media_source_audio.py), "
                          "launched and read by this script directly -- omit for video-only.")
    ap.add_argument("--audio-codec", choices=["opus", "codec2"], default="opus",
                     help="Must match --audio-source-cmd's own --audio-codec (default opus).")
    ap.add_argument("--audio-codec2-mode", type=str, default="3200",
                     help="Codec2 mode, for --audio-codec codec2 -- must match --audio-source-cmd's "
                          "own --codec2-mode, and have a matching AUDIO_PROFILES entry here (and in "
                          "media_rx_player.py's table) for this exact mode string (default 3200).")
    ap.add_argument("--station-id", type=str, default=None, metavar="STR",
                     help="Optional station/operator identification (e.g. a callsign), up to "
                          f"{STATION_ID_LEN} ASCII characters (space-padded/truncated to exactly "
                          f"{STATION_ID_LEN}, CRC32-protected). Sent 1 byte/fragment, cycling every "
                          f"{STATION_ID_LEN + 4} fragments -- see STATION_ID_TYPE's comment above. "
                          "Omit to not send one at all.")
    args = ap.parse_args()
    if args.station_id is not None:
        try:
            args.station_id.encode("ascii")
        except UnicodeEncodeError:
            print("ERROR: --station-id must be ASCII.", file=sys.stderr)
            sys.exit(1)

    if not args.video_source_cmd and not args.audio_source_cmd:
        print("ERROR: need at least one of --video-source-cmd/--audio-source-cmd.", file=sys.stderr)
        sys.exit(1)
    if args.video_source_cmd and not args.video_framerate:
        print("ERROR: --video-source-cmd requires --video-framerate.", file=sys.stderr)
        sys.exit(1)
    video_width = video_height = None
    if args.video_source_cmd:
        if not args.video_resolution:
            print("ERROR: --video-source-cmd requires --video-resolution.", file=sys.stderr)
            sys.exit(1)
        video_width, video_height = (int(x) for x in args.video_resolution.lower().split("x"))

    budget = args.fragment_size - 2  # 2-byte real_length prefix on every block
    if args.station_id is not None and RECORD_HEADER_LEN + 2 > budget:
        print(f"ERROR: --fragment-size {args.fragment_size} is too small to fit the "
              f"station ID record ({RECORD_HEADER_LEN + 2} bytes needed).", file=sys.stderr)
        sys.exit(1)
    lock = threading.Lock()

    # Mutated by the two source threads below (audio_profile_id once
    # its OpusHead is read; video_config_record once the first
    # access unit's SPS/PPS are read) -- both start None, exactly like
    # the old astream/vstream-is-None cases, so a fragment sent before
    # either thread finishes its own startup just omits that config
    # record until it's ready (self-heals on the very next fragment).
    state = {"audio_profile_id": None, "video_config_record": None, "last_audio_packet_len": None,
              "station_id_payload": _build_station_id_payload(args.station_id) if args.station_id else None,
              "station_id_seq": 0}

    def _fresh_block():
        # Seeds every new block with the (still tiny, 6-byte-max) audio
        # config record -- profile ID AND the audio packet length
        # currently in use -- so literally every fragment sent over the
        # air carries both, not just one resent periodically by packet
        # count. The length has to be relearned here too, not just the
        # profile: a receiver that joins after TX has already switched
        # to AUDIO_PACKET_FIXED_TYPE (its steady state almost the whole
        # session) would otherwise never see an explicit-length packet
        # at all and could never decode a single FIXED record -- the
        # exact same "one-time header, late joiner misses it forever"
        # mistake this whole TLV design exists to avoid, just
        # reintroduced in miniature for this one field. Confirmed for
        # real this actually happens: a live test joining after stream
        # start decoded nothing.
        out = bytearray()
        # Station ID: one [index, data_byte] pair per fragment, unconditional
        # on the audio profile being known yet -- see STATION_ID_TYPE's own
        # comment above for why this rides serially instead of all at once.
        id_payload = state["station_id_payload"]
        if id_payload is not None:
            idx = state["station_id_seq"] % len(id_payload)
            state["station_id_seq"] += 1
            rec_payload = bytes([idx, id_payload[idx]])
            out.extend(struct.pack(">BH", STATION_ID_TYPE, len(rec_payload)) + rec_payload)
        if state["audio_profile_id"] is None:
            return out
        slot_len = state["last_audio_packet_len"] or 0
        payload = bytes([state["audio_profile_id"]]) + struct.pack(">H", slot_len)
        out.extend(struct.pack(">BH", AUDIO_CONFIG_TYPE, len(payload)) + payload)
        return out

    block = _fresh_block()  # station ID (if any) starts from fragment 1; audio config joins once known
    fragments_sent = 0
    video_packets_since_config = 0
    stats = {"audio_packets": 0, "audio_bytes": 0, "video_packets": 0, "video_bytes": 0}

    # A completed fragment's actual bytes go OUT to stdout on this
    # dedicated thread, never on the audio/video ingestion threads
    # themselves -- confirmed for real this matters: flush_block() used
    # to call sys.stdout.buffer.write() directly, INSIDE the same `lock`
    # append_video_packet/append_audio_packet hold while mutating `block`.
    # hf_ofdm_tx.py's downstream stdin read is paced to real time (one
    # fragment's own on-air duration, e.g. 240ms), which is completely
    # normal and expected -- but the moment that pacing put ANY
    # backpressure on this write() call, it blocked for however long
    # hf_ofdm_tx.py took to catch up, holding `lock` the whole time and
    # freezing BOTH threads' ability to ingest their next packet, even
    # though ffmpeg was still happily producing frames into its own pipe
    # completely independently. That showed up as an extremely
    # consistent ~1.2s "stall" in media_source_video.py's own frame
    # timing (measured via video_thread_fn's per-frame gap tracking) --
    # too regular to be real encode/capture jitter, and unaffected by
    # -flush_packets 1 (confirmed for real: identical to the millisecond
    # with or without it), which is exactly what you'd expect from a
    # LOCK contention artifact rather than anything ffmpeg itself is
    # doing. Handing the write off to this thread via an unbounded queue
    # means normal, expected downstream backpressure just makes this
    # queue grow (bounded only by memory, same reasoning as
    # AudioPlaybackWorker's own unbounded queue elsewhere in this
    # project) instead of ever blocking the threads that are supposed to
    # keep encoding/packing in real time regardless of how fast the link
    # can currently drain.
    output_queue = queue.Queue()

    def output_writer_fn():
        # Diagnostic: if this queue's depth trends upward over a session
        # instead of staying near 0-1, the framer is producing fragments
        # faster than hf_ofdm_tx.py's stdin read can drain them in real
        # time -- i.e. audio+video's real combined bitrate need exceeds
        # the link's actual effective capacity, silently building a
        # backlog that (being unbounded on purpose, see this queue's own
        # comment above) never blocks/errors, but means anything still
        # sitting in it when the session is stopped was never sent at
        # all. That would show up exactly as "some audio missing" with
        # 0% loss reported at the OFDM layer, indistinguishable from a
        # real bug without directly measuring this depth over time.
        n = 0
        while True:
            item = output_queue.get()
            if item is None:
                break
            n += 1
            if n % 20 == 0:
                print(f"  [framer] output_queue depth: {output_queue.qsize()} "
                      f"(after {n} fragments written)", file=sys.stderr)
            sys.stdout.buffer.write(item)
            sys.stdout.buffer.flush()

    output_writer = threading.Thread(target=output_writer_fn, daemon=True)
    output_writer.start()

    # audio_thread_fn/video_thread_fn only ever put() raw, already-decoded
    # packets/access-units onto these -- neither ever touches `block`,
    # append_record/append_video_packet/append_audio_packet, or
    # flush_block() directly anymore. See assembler_tick's own docstring
    # for why packing moved off the ingestion threads entirely (real
    # muxers -- MPEG-TS, ffmpeg's own interleaved writer, DRM's MSC
    # sub-channels -- interleave by a fixed output cadence/deadline
    # across streams, not reactively whenever one stream's own buffer
    # happens to overflow; a reactive design let a bursty video access
    # unit's own split loop -- which runs in a handful of Python
    # bytecodes, no real time elapsing between its own flush_block()
    # calls -- structurally out-compete audio for the shared fragment
    # budget, since audio is only ever produced once every real 20ms on
    # its own thread and literally can't have anything new to drain
    # during that instant. Confirmed for real: even an explicit
    # AUDIO_RESERVE_BYTES set aside on every fragment still left a ~15%
    # audio delivery shortfall on an actual link, because a cycle's
    # reserved bytes went out as pure zero-padding whenever audio_queue
    # was momentarily empty, and that wasted capacity was never
    # recovered. Time-based assembly fixes this at the root: every
    # fragment period, whatever audio arrived in that period is packed
    # FIRST (always fits -- codec2/Opus are a few kbps against a
    # fragment that's tens of kbps), before video gets a chance at
    # whatever's left, so audio's own real production rate is preserved
    # regardless of how bursty video's is.
    audio_queue = queue.Queue()
    video_queue = queue.Queue()
    video_carry = []  # video items held over to the next tick -- see assembler_tick
    # Largest thing _fresh_block() seeds a fragment with: station-ID record
    # (2-byte payload) + audio config record (3-byte payload).
    FRESH_BLOCK_MAX_BYTES = (RECORD_HEADER_LEN + 2) + (RECORD_HEADER_LEN + 3)

    def flush_block():
        nonlocal block, fragments_sent
        if not block:
            return
        out = bytearray(struct.pack(">H", len(block)))
        out.extend(block)
        out.extend(b"\x00" * (args.fragment_size - len(out)))
        output_queue.put(bytes(out))
        fragments_sent += 1
        audio_kbps = stats["audio_bytes"] * 8 / 0.06 / max(1, stats["audio_packets"]) if stats["audio_packets"] else 0
        print(f"  [framer] fragment {fragments_sent}: "
              f"{stats['audio_packets']} audio pkt ({stats['audio_bytes']}B), "
              f"{stats['video_packets']} video pkt ({stats['video_bytes']}B)", file=sys.stderr)
        block = _fresh_block()
        for k in stats:
            stats[k] = 0

    def append_record(rec):
        nonlocal block
        if len(rec) > budget:
            print(f"WARNING: dropping a {len(rec)}-byte record that doesn't fit in one "
                  f"fragment (budget {budget}B) -- --fragment-size too small for this "
                  f"bitrate/resolution?", file=sys.stderr)
            return
        if len(block) + len(rec) > budget:
            flush_block()
        block.extend(rec)

    def append_video_packet(raw):
        """A single H.264 access-unit packet, split across consecutive
        fragments if it's bigger than one fragment can ever hold -- keyframes
        in particular can spike well past a fragment's budget on a busy
        scene even when typical P-frames fit easily (confirmed for real: a
        synthetic test's very first I-frame silently dropped a whole GOP's
        worth of video before this existed). Losing any ONE fragment in the
        middle of a split packet still corrupts that packet same as always
        (no different from a lost fragment corrupting a single-fragment
        packet) -- the decoder just discards it and waits for the next
        keyframe, the same graceful-degradation model as everywhere else in
        this design, not a new failure mode.

        Only ever called from assembler_tick(), never directly from
        video_thread_fn -- see assembler_tick's own docstring for why:
        audio for this cycle is already packed into `block` before this
        runs, so it's never at risk of being squeezed out by a video
        split, without needing any reserved/off-limits byte range here."""
        if len(raw) <= budget - RECORD_HEADER_LEN:
            append_record(struct.pack(">BH", VIDEO_PACKET_TYPE, len(raw)) + raw)
            return
        # Doesn't fit in a lone fragment at all -- flush whatever's pending
        # first so the split starts clean at a fragment boundary, then lay
        # each chunk into its OWN fresh fragment (not sharing space with
        # other records) so the receiver can reassemble it from a
        # deterministic run of consecutive fragments.
        flush_block()
        # budget - RECORD_HEADER_LEN alone would assume an EMPTY block --
        # wrong now that flush_block() always reseeds block with a fresh
        # CONFIG record (see _fresh_block's own docstring), so len(block)
        # here is that record's size, not zero. Sizing a chunk against
        # the stale "empty block" assumption overflows the fragment by
        # however big that CONFIG record is -- confirmed for real: a
        # 2048-byte-fragment run produced a 2052-byte "fragment" that
        # corrupted the fragment-length prefix and permanently desynced
        # the receiver's parser for the rest of the session (silently --
        # every subsequent fragment decoded as OK at the OFDM layer,
        # since this is a framing bug, not a transmission error).
        max_first_chunk = budget - len(block) - RECORD_HEADER_LEN - 4  # START carries a 4-byte total_len prefix
        first, rest = raw[:max_first_chunk], raw[max_first_chunk:]
        payload = struct.pack(">I", len(raw)) + first
        block.extend(struct.pack(">BH", VIDEO_PACKET_START_TYPE, len(payload)) + payload)
        # Only flush here (and after each CONT chunk below) when there's
        # MORE of this split still to come -- matches append_audio_packet's
        # own identical fix, and for the same reason: flushing after the
        # split's FINAL chunk regardless was zero-padding out whatever
        # fraction of the fragment was left unused, every single time,
        # instead of leaving `block` open for the next queued item (more
        # video from this tick, or the next tick's own content) to fill
        # that space. Confirmed for real this was a major, systematic
        # source of wasted channel capacity -- practically every video
        # access unit needs at least a 2-chunk split at typical
        # resolutions/bitrates, so this ran on nearly every single frame.
        if rest:
            flush_block()
        while rest:
            max_cont_chunk = budget - len(block) - RECORD_HEADER_LEN
            chunk, rest = rest[:max_cont_chunk], rest[max_cont_chunk:]
            block.extend(struct.pack(">BH", VIDEO_PACKET_CONT_TYPE, len(chunk)) + chunk)
            if rest:
                flush_block()

    def append_audio_packet(raw):
        """Like append_video_packet, but for audio the case that actually
        matters isn't "this packet is bigger than a whole fragment" (rare
        -- only at very high bitrates) -- it's "this packet doesn't fit in
        whatever's LEFT of the CURRENT, already-partially-filled block".
        append_video_packet's own flush-then-split approach throws that
        leftover space away before starting the split; doing that for
        EVERY audio packet that doesn't land exactly on a fragment
        boundary would just move the packing-cliff waste from "whole
        packets deferred" to "a flush's worth of padding on every split"
        -- no real improvement. Using whatever room is ACTUALLY left for
        the split's first chunk instead is what turns "doesn't fit ->
        waste the rest of this fragment" into "doesn't fit -> use every
        remaining byte of this fragment for it", which is the actual fix:
        every fragment byte gets used regardless of bitrate, eliminating
        the discrete packing cliff entirely instead of just shrinking it
        (see media_tx_gui.py's packing-aware bitrate calculator's own
        docstring for the full story of that cliff). Deliberately does
        NOT flush after the FINAL chunk of a split, so whatever comes
        next (a config resend, the next audio packet, video) keeps
        packing into the same partially-filled block -- flushing there
        would reintroduce the exact waste this exists to remove.

        Whole (unsplit) packets use AUDIO_PACKET_FIXED_TYPE (1-byte
        header, no length) whenever this packet's length matches the
        immediately preceding one, falling back to the explicit-length
        AUDIO_PACKET_TYPE otherwise -- see that type's own comment.
        Splitting always uses the explicit-length START/CONT types
        regardless, since a split's total_len field already makes the
        record self-describing and reusing the fixed-size shortcut
        there would only save a byte or two for real added complexity."""
        if len(raw) == state["last_audio_packet_len"]:
            rec_whole = struct.pack(">B", AUDIO_PACKET_FIXED_TYPE) + raw
        else:
            rec_whole = struct.pack(">BH", AUDIO_PACKET_TYPE, len(raw)) + raw
        if len(block) + len(rec_whole) <= budget:
            block.extend(rec_whole)
            state["last_audio_packet_len"] = len(raw)
            return
        if len(rec_whole) > budget:
            # Doesn't fit in even a whole, empty fragment (only possible
            # at very high bitrates) -- nothing left in the current block
            # is usable for it either way, so start clean like video does.
            flush_block()
        header_len = RECORD_HEADER_LEN + 4  # type+length + 4-byte total_len prefix
        remaining_in_block = budget - len(block)
        if remaining_in_block <= header_len:
            # Not even enough room here for a meaningful START chunk --
            # flush this near-empty leftover and split starting fresh.
            flush_block()
            remaining_in_block = budget - len(block)
        first_chunk_len = remaining_in_block - header_len
        first, rest = raw[:first_chunk_len], raw[first_chunk_len:]
        payload = struct.pack(">I", len(raw)) + first
        block.extend(struct.pack(">BH", AUDIO_PACKET_START_TYPE, len(payload)) + payload)
        flush_block()
        while rest:
            # Recomputed every iteration, not hoisted before the loop --
            # see append_video_packet's matching comment: block isn't
            # empty right after flush_block() (it's reseeded with a
            # fresh CONFIG record), so a chunk size fixed against the
            # empty-block assumption overflows the fragment.
            max_cont_chunk = budget - len(block) - RECORD_HEADER_LEN
            chunk, rest = rest[:max_cont_chunk], rest[max_cont_chunk:]
            block.extend(struct.pack(">BH", AUDIO_PACKET_CONT_TYPE, len(chunk)) + chunk)
            if rest:
                flush_block()
        state["last_audio_packet_len"] = len(raw)

    def assembler_tick():
        """Runs on its own dedicated thread, once every
        --fragment-period-s, and is the ONLY place that ever touches
        `block`/flush_block()/append_video_packet/append_audio_packet --
        video_thread_fn and audio_thread_fn just put() raw items onto
        video_queue/audio_queue and otherwise never come near packing at
        all. This is the actual fix for the audio-starvation problem
        that AUDIO_RESERVE_BYTES only ever partially covered: instead of
        reactively building/flushing a fragment whenever whichever
        ingestion thread's own append call happens to overflow it (an
        accident of thread scheduling, not the streams' real relative
        rates), this drains and packs on a fixed real-time cadence, the
        same principle a real muxer uses (MPEG-TS/ffmpeg's own
        interleaved writer pick whichever stream's packet is next due by
        deadline; DRM's MSC divides each fixed logical frame into
        per-stream sub-channels sized to each stream's own bitrate) --
        every cycle's audio is packed FIRST, unconditionally, before
        video gets whatever's left, so audio's real production rate
        survives regardless of how bursty video's own access units are.
        A cycle that needs more than one physical fragment (a big video
        access unit) still emits several via append_video_packet's own
        split loop, same as before -- but that cycle's audio is already
        safely in `block` before the split even starts, so it rides out
        in the FIRST of those fragments rather than being at risk."""
        nonlocal video_packets_since_config
        with lock:
            audio_items = []
            while True:
                try:
                    audio_items.append(audio_queue.get_nowait())
                except queue.Empty:
                    break
            video_items = video_carry[:]
            video_carry.clear()
            while True:
                try:
                    video_items.append(video_queue.get_nowait())
                except queue.Empty:
                    break
            for raw in audio_items:
                append_audio_packet(raw)
                stats["audio_packets"] += 1
                stats["audio_bytes"] += len(raw)
            # The link drains exactly one fragment per tick, so any extra
            # fragment flushed mid-tick is a permanent +1 on the output
            # backlog. Instead, a video packet that doesn't fit what's
            # left of this tick's fragment is SPLIT at the boundary: a
            # START record fills the remaining bytes and the rest waits
            # at the head of video_carry as a CONT record for the next
            # tick (after that tick's audio -- media_rx_player only sees
            # video records, so its reassembly doesn't mind). Every
            # fragment then goes out full, with no extra ones.
            MIN_START_CHUNK = 16
            for i, (au, is_keyframe) in enumerate(video_items):
                if is_keyframe == "cont":  # remainder of a packet split last tick
                    room = budget - len(block) - RECORD_HEADER_LEN
                    chunk, rest = au[:room], au[room:]
                    block.extend(struct.pack(">BH", VIDEO_PACKET_CONT_TYPE, len(chunk)) + chunk)
                    if rest:
                        video_carry.append((rest, "cont"))
                        video_carry.extend(video_items[i + 1:])
                        break
                    continue
                if len(au) + RECORD_HEADER_LEN > budget - FRESH_BLOCK_MAX_BYTES:
                    append_video_packet(au)  # bigger than a whole fragment -- old split path
                else:
                    if is_keyframe or video_packets_since_config == 0:
                        cfg = state["video_config_record"]
                        if len(block) + len(cfg) > budget:
                            video_carry.extend(video_items[i:])
                            break
                        block.extend(cfg)
                    room = budget - len(block)
                    if RECORD_HEADER_LEN + len(au) <= room:
                        block.extend(struct.pack(">BH", VIDEO_PACKET_TYPE, len(au)) + au)
                    elif room - RECORD_HEADER_LEN - 4 >= MIN_START_CHUNK:
                        n = room - RECORD_HEADER_LEN - 4
                        payload = struct.pack(">I", len(au)) + au[:n]
                        block.extend(struct.pack(">BH", VIDEO_PACKET_START_TYPE, len(payload)) + payload)
                        video_carry.append((au[n:], "cont"))
                        video_carry.extend(video_items[i + 1:])
                        video_packets_since_config = (video_packets_since_config + 1) % VIDEO_CONFIG_REPEAT_EVERY
                        stats["video_packets"] += 1
                        stats["video_bytes"] += len(au)
                        break
                    else:
                        # Too little room left to bother splitting. (A config
                        # record just added above is harmless: it's resent
                        # with this packet next tick.)
                        video_carry.extend(video_items[i:])
                        break
                video_packets_since_config = (video_packets_since_config + 1) % VIDEO_CONFIG_REPEAT_EVERY
                stats["video_packets"] += 1
                stats["video_bytes"] += len(au)
            flush_block()

    assembler_stop = threading.Event()

    def assembler_thread_fn():
        # Drift-corrected against an absolute next_tick, not a plain
        # time.sleep(args.fragment_period_s) in a loop -- the latter
        # would accumulate this thread's own scheduling/GC jitter every
        # cycle, same reasoning as StdoutPacer elsewhere in this project.
        next_tick = time.monotonic()
        while not assembler_stop.is_set():
            assembler_tick()
            next_tick += args.fragment_period_s
            sleep_for = next_tick - time.monotonic()
            if sleep_for > 0:
                assembler_stop.wait(sleep_for)
            else:
                # Fell behind (a slow tick, or the process was paused/
                # descheduled) -- resync to now instead of firing a burst
                # of back-to-back catch-up ticks with no real time
                # between them, which would just reintroduce the exact
                # "no time for audio to arrive" problem this exists to fix.
                next_tick = time.monotonic()

    threads = []
    procs = []

    if args.video_source_cmd:
        def video_thread_fn():
            proc = _popen_from_cmdline(args.video_source_cmd)
            procs.append(proc)
            if args.video_codec == "wavelet":
                # Every frame is independently decodable, so there's no
                # keyframe logic -- the config record is just the codec
                # tag plus size/fps, resent periodically for late joiners.
                fps_x1000 = min(65535, max(0, round(args.video_framerate * 1000)))
                payload = struct.pack(">HHH", video_width, video_height, fps_x1000) + WAVELET_CODEC_TAG
                state["video_config_record"] = struct.pack(">BH", VIDEO_CONFIG_TYPE, len(payload)) + payload
                for pkt in read_length_prefixed(proc.stdout):
                    video_queue.put((pkt, False))
                proc.wait()
                return
            units = read_h264_units(proc.stdout)
            pending_non_vcl = bytearray()
            sps_pps = bytearray()
            width = height = None
            # Measures the SOURCE's own real output timing -- how often a
            # whole access unit (decodable frame) actually comes OUT of
            # media_source_video.py's ffmpeg process -- independent of
            # everything downstream (fragment packing/budget, the OFDM
            # link, the RX side).
            #
            # Reports MAX/MEAN inter-frame gap, not just an average fps
            # over a fixed window -- a fixed-window fps bucket count
            # ALIASES against any periodicity close to its own window
            # length (confirmed for real: with --gop-seconds 2.0 driving
            # x264's intra-refresh cycle and a 2.0s report window here,
            # consecutive windows alternated ~13fps/~9fps in a suspiciously
            # regular pattern -- exactly what aliasing between two similar
            # periods looks like, not necessarily N genuinely slow
            # seconds). The actual per-frame gap tells the real story: a
            # steady source shows a small, consistent gap close to
            # 1/--video-framerate every time; a genuine stall shows a
            # handful of individual gaps far larger than that, regardless
            # of what any fixed-window average reports either way.
            VIDEO_RATE_REPORT_INTERVAL_S = 2.0
            frame_count_window = 0
            window_start_t = time.monotonic()
            last_frame_t = None
            gap_sum_s = 0.0
            gap_max_s = 0.0
            gap_n = 0
            for nal, nal_type in units:
                if nal_type in (7, 8):  # SPS, PPS
                    sps_pps.extend(b"\x00\x00\x01" + nal)
                    pending_non_vcl.extend(b"\x00\x00\x01" + nal)
                    continue
                if nal_type not in (1, 5):  # anything but a VCL slice (SEI, AUD, etc.)
                    pending_non_vcl.extend(b"\x00\x00\x01" + nal)
                    continue
                # A VCL slice -- this NAL plus whatever non-VCL NALs
                # immediately preceded it form one access unit (one
                # decodable frame). SPS/PPS only ever appear before the
                # very first frame under this project's encoding (see
                # module docstring), so this only needs to run once in
                # practice, but stays general rather than assuming that.
                if state["video_config_record"] is None:
                    if width is None:
                        # Parsing the SPS bits for width/height would need a
                        # real SPS bit-parser -- simpler to just ask for the
                        # same --resolution already given to
                        # media_source_video.py directly (see --video-resolution).
                        width, height = video_width, video_height
                    fps_x1000 = min(65535, max(0, round(args.video_framerate * 1000)))
                    payload = struct.pack(">HHH", width, height, fps_x1000) + bytes(sps_pps)
                    rec = struct.pack(">BH", VIDEO_CONFIG_TYPE, len(payload)) + payload
                    if len(rec) > budget:
                        print(f"ERROR: --fragment-size {args.fragment_size} is too small to fit "
                              f"the video CONFIG record ({len(rec)} bytes needed).", file=sys.stderr)
                        os._exit(1)
                    state["video_config_record"] = rec
                is_keyframe = (nal_type == 5)
                au = bytes(pending_non_vcl) + b"\x00\x00\x01" + nal
                pending_non_vcl = bytearray()
                video_queue.put((au, is_keyframe))
                frame_count_window += 1
                now = time.monotonic()
                if last_frame_t is not None:
                    gap = now - last_frame_t
                    gap_sum_s += gap
                    gap_max_s = max(gap_max_s, gap)
                    gap_n += 1
                last_frame_t = now
                elapsed = now - window_start_t
                if elapsed >= VIDEO_RATE_REPORT_INTERVAL_S:
                    measured_fps = frame_count_window / elapsed
                    target_gap_ms = 1000 / args.video_framerate if args.video_framerate > 0 else 0
                    mean_gap_ms = (gap_sum_s / gap_n * 1000) if gap_n else 0.0
                    max_gap_ms = gap_max_s * 1000
                    note = ""
                    # A real stall: some individual gap far exceeds the
                    # target spacing (not just the window-average fps
                    # dipping, which -- see this block's own comment above
                    # -- can just be sampling-window aliasing).
                    if target_gap_ms > 0 and max_gap_ms > 3 * target_gap_ms:
                        note = (f"  <-- a real stall: one gap was {max_gap_ms:.0f}ms against an "
                                 f"expected ~{target_gap_ms:.0f}ms -- the SOURCE itself (camera "
                                 f"capture/ffmpeg encode) paused, not just window-average noise")
                    print(f"  [framer] video source timing: {measured_fps:.1f}fps avg  |  "
                          f"gap mean={mean_gap_ms:.0f}ms max={max_gap_ms:.0f}ms  |  "
                          f"target {args.video_framerate:g}fps (~{target_gap_ms:.0f}ms){note}",
                          file=sys.stderr)
                    frame_count_window = 0
                    window_start_t = now
                    gap_sum_s = 0.0
                    gap_max_s = 0.0
                    gap_n = 0
            proc.wait()

        threads.append(threading.Thread(target=video_thread_fn, daemon=True))

    if args.audio_source_cmd:
        def audio_thread_fn():
            proc = _popen_from_cmdline(args.audio_source_cmd)
            procs.append(proc)

            if args.audio_codec == "codec2":
                # No extradata/handshake to read (unlike Opus's OpusHead)
                # -- Codec2's raw output is headerless, so profile
                # identity is just (codec, mode), both already known from
                # CLI args rather than parsed from the stream itself.
                profile_id = next((pid for pid, p in AUDIO_PROFILES.items()
                                    if p["codec"] == "codec2" and p["mode"] == args.audio_codec2_mode), None)
                if profile_id is None:
                    print(f"ERROR: Codec2 mode {args.audio_codec2_mode!r} isn't in AUDIO_PROFILES -- "
                          f"add it there (and to media_rx_player.py's matching table) before using "
                          f"it on this link.", file=sys.stderr)
                    os._exit(1)
                frame_bytes = AUDIO_PROFILES[profile_id]["frame_bytes"]
                packets = read_codec2_frames(proc.stdout, frame_bytes)
            else:
                opus_packets = read_ogg_opus_packets(proc.stdout)
                try:
                    opus_head = next(opus_packets)
                    next(opus_packets)  # OpusTags -- not needed
                except StopIteration:
                    proc.wait()
                    return
                # Opus always operates at 48kHz internally regardless of
                # the source's own sample rate (RFC 7845) -- matches
                # AUDIO_PROFILES's own key, so this is a constant, not
                # something to parse out of opus_head.
                audio_rate = 48000
                profile_id = next((pid for pid, p in AUDIO_PROFILES.items()
                                    if p["codec"] == "opus" and p["rate"] == audio_rate
                                    and p["extradata"] == opus_head), None)
                if profile_id is None:
                    print(f"ERROR: this audio format (extradata={opus_head.hex()}) isn't in "
                          f"AUDIO_PROFILES -- add it there (and to media_rx_player.py's matching "
                          f"table) before using it on this link.", file=sys.stderr)
                    os._exit(1)
                packets = opus_packets

            AUDIO_CONFIG_MAX_BYTES = RECORD_HEADER_LEN + 1 + 2  # profile ID + current slot length
            if AUDIO_CONFIG_MAX_BYTES > budget:
                print(f"ERROR: --fragment-size {args.fragment_size} is too small to fit the "
                      f"audio CONFIG record ({AUDIO_CONFIG_MAX_BYTES} bytes needed).", file=sys.stderr)
                os._exit(1)
            state["audio_profile_id"] = profile_id
            for raw in packets:
                audio_queue.put(raw)
            proc.wait()

        threads.append(threading.Thread(target=audio_thread_fn, daemon=True))

    assembler_thread = threading.Thread(target=assembler_thread_fn, daemon=True)
    for t in threads:
        t.start()
    assembler_thread.start()
    for t in threads:
        t.join()

    # Sources are done -- stop the ticker and run one last tick by hand
    # so whatever they queued in their final real cycle (which the
    # ticker's own sleeping thread might not get to before assembler_stop
    # is observed) still gets packed and sent, not silently dropped.
    assembler_stop.set()
    assembler_thread.join(timeout=2.0)
    assembler_tick()
    with lock:
        flush_block()
    # Sentinel + join so the writer thread's own queue is fully drained
    # (the final flush_block() above may have just enqueued one more
    # fragment) before this process exits -- otherwise a fragment queued
    # right at shutdown could be silently lost if the process ends before
    # output_writer_fn gets to it.
    output_queue.put(None)
    output_writer.join()
    print(f"Stream ended: sent {fragments_sent} fragment-sized block(s).", file=sys.stderr)


if __name__ == "__main__":
    main()
