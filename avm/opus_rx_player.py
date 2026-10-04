#!/usr/bin/env python3
"""
Reads hf_ofdm_rx.py's output -- fixed --fragment-size blocks produced
by opus_tx_framer.py, see its docstring for the whole design -- and
plays the reconstructed Opus audio. A lost fragment just means fewer
packets decoded (a brief gap), not corrupted parsing of everything
after it: each block is fully self-contained, so the stream resyncs on
its own at the very next successfully-received fragment.

Usage:
    python hf_ofdm_rx.py --mode A --fragment-size 1024 ... \\
        | python opus_rx_player.py --fragment-size 1024
"""
import argparse
import struct
import sys
import time

import av
import sounddevice as sd

CONFIG_TYPE = 0x01
OPUS_PACKET_TYPE = 0x02
RECORD_HEADER_LEN = 3
STATS_INTERVAL_S = 5.0


def read_exact(size):
    """A normal blocking read(size) -- accumulates until exactly `size`
    bytes or EOF. Safe (unlike opus_player.py's PyAV-facing reader,
    which needed read1() to avoid over-buffering) because our own
    framing is fixed-size blocks by design: there's no probing or
    partial-read ambiguity, we always know exactly how many bytes make
    up the next block."""
    buf = bytearray()
    while len(buf) < size:
        chunk = sys.stdin.buffer.read(size - len(buf))
        if not chunk:
            return bytes(buf) if buf else b""
        buf.extend(chunk)
    return bytes(buf)


def parse_records(data):
    pos = 0
    while pos + RECORD_HEADER_LEN <= len(data):
        rtype, rlen = struct.unpack_from(">BH", data, pos)
        pos += RECORD_HEADER_LEN
        if pos + rlen > len(data):
            break
        yield rtype, data[pos:pos + rlen]
        pos += rlen


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fragment-size", type=int, required=True,
                     help="Must match the --fragment-size given to hf_ofdm_tx.py/hf_ofdm_rx.py.")
    ap.add_argument("--latency", type=float, default=0.5,
                     help="PortAudio output buffer target in seconds (default 0.5).")
    ap.add_argument("--device", type=str, default=None,
                     help="Output device name or index (default: system default). "
                          "See `python -m sounddevice` for a list.")
    ap.add_argument("--stats", action="store_true",
                     help="Every 5s, print the actual input byte rate and the ratio of audio "
                          "decoded per real wall-clock second -- the direct way to check whether "
                          "enough data is arriving to sustain real-time playback (a ratio "
                          "consistently below 1.0 means the source is encoding faster than this "
                          "link's effective bitrate can carry, which is what accumulates into "
                          "periodic playback breaks -- see hf_ofdm_tx.py/hf_ofdm_rx.py's own "
                          "startup 'Effective bitrate' line for the link's actual ceiling).")
    args = ap.parse_args()
    device = int(args.device) if args.device is not None and args.device.isdigit() else args.device

    decoder = None
    channels = None
    out = None
    frames_played = 0
    blocks_read = 0
    packets_decoded = 0
    packets_dropped = 0

    start_t = time.monotonic()
    last_stats_t = start_t
    bytes_since_stats = 0
    audio_s_since_stats = 0.0

    print("Waiting for framed Opus blocks on stdin...", file=sys.stderr)
    while True:
        block = read_exact(args.fragment_size)
        if not block:
            break
        blocks_read += 1
        bytes_since_stats += len(block)

        if args.stats:
            now = time.monotonic()
            elapsed = now - last_stats_t
            if elapsed >= STATS_INTERVAL_S:
                input_kbps = bytes_since_stats * 8 / elapsed / 1000
                ratio = audio_s_since_stats / elapsed if elapsed > 0 else 0.0
                print(f"[stats] input={input_kbps:.2f}kbps  "
                      f"audio_decoded/real_time={ratio:.2f}x"
                      f"{'  <-- FALLING BEHIND' if ratio < 0.97 else ''}",
                      file=sys.stderr)
                last_stats_t = now
                bytes_since_stats = 0
                audio_s_since_stats = 0.0

        if len(block) < 2:
            continue
        real_len = struct.unpack_from(">H", block, 0)[0]
        payload = block[2:2 + real_len]

        for rtype, rpayload in parse_records(payload):
            if rtype == CONFIG_TYPE and len(rpayload) >= 4:
                rate = struct.unpack_from(">I", rpayload, 0)[0]
                extradata = bytes(rpayload[4:])
                if decoder is None:
                    decoder = av.CodecContext.create("libopus", "r")
                    decoder.extradata = extradata
                    decoder.sample_rate = rate
                    print(f"Config received: {rate}Hz, "
                          f"extradata={len(extradata)} bytes", file=sys.stderr)
            elif rtype == OPUS_PACKET_TYPE and decoder is not None:
                try:
                    frames = decoder.decode(av.Packet(bytes(rpayload)))
                except av.error.InvalidDataError:
                    packets_dropped += 1
                    continue  # a corrupted/partial packet -- skip it, don't crash the stream
                for frame in frames:
                    if out is None:
                        channels = len(frame.layout.channels)
                        out = sd.OutputStream(samplerate=frame.sample_rate, channels=channels,
                                               dtype="int16", latency=args.latency, device=device)
                        out.start()
                        print(f"Playing: {frame.sample_rate}Hz, {channels} channel(s)",
                              file=sys.stderr)
                    pcm = frame.to_ndarray().reshape(-1, channels)
                    out.write(pcm)
                    frames_played += len(pcm)
                    audio_s_since_stats += len(pcm) / frame.sample_rate
                    packets_decoded += 1

    played_s = frames_played / out.samplerate if out is not None else 0.0
    if out is not None:
        out.stop()
        out.close()
    print(f"Stream ended: read {blocks_read} block(s), decoded {packets_decoded} packet(s), "
          f"dropped {packets_dropped} corrupted packet(s), "
          f"played {played_s:.1f}s of audio.",
          file=sys.stderr)


if __name__ == "__main__":
    main()
