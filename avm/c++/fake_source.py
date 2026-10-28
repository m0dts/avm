"""Deterministic stand-in sources for media_tx_framer.py (testing only).

    python fake_source.py video SEED FPS   -> [2-byte length][packet] records (as media_source_wavelet.py)
    python fake_source.py audio SEED       -> 8-byte Codec2-sized frames, 50 a second

The content comes from SEED, so a test can regenerate exactly what was sent
(packets(), frames())."""
import random
import struct
import sys
import time


def gen_packets(seed):
    rnd = random.Random(seed)
    i = 0
    while True:
        size = rnd.choice([rnd.randrange(40, 400), rnd.randrange(400, 1500), rnd.randrange(1500, 4000)])
        yield struct.pack(">I", i) + rnd.randbytes(size - 4)
        i += 1


def gen_frames(seed):
    rnd = random.Random(seed + 1)
    i = 0
    while True:
        yield struct.pack(">I", i) + rnd.randbytes(4)
        i += 1


def packets(seed, n):
    g = gen_packets(seed)
    return [next(g) for _ in range(n)]


def frames(seed, n):
    g = gen_frames(seed)
    return [next(g) for _ in range(n)]


if __name__ == "__main__":
    kind, seed = sys.argv[1], int(sys.argv[2])
    out = sys.stdout.buffer
    t0 = time.monotonic()
    if kind == "video":
        fps = float(sys.argv[3])
        for i, p in enumerate(gen_packets(seed)):
            time.sleep(max(0.0, t0 + i / fps - time.monotonic()))
            out.write(struct.pack(">H", len(p)) + p)
            out.flush()
    else:
        for i, f in enumerate(gen_frames(seed)):
            time.sleep(max(0.0, t0 + i * 0.02 - time.monotonic()))
            out.write(f)
            out.flush()
