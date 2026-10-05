"""
Live camera preview through shared memory, for the touch GUI's TX view.

The camera is owned by the video encoder (media_source_wavelet.py) while
transmitting, so the GUI can't open it itself. Instead the encoder, given
--preview PATH, copies every frame it reads (YUV 4:2:0, already scaled to
the encode resolution -- exactly the picture being compressed) into a small
memory-mapped file; the GUI polls it. Costs one ~55 KB copy per frame.

File layout: 16-byte header ("HFPV", uint32 sequence, uint16 width,
uint16 height, 4 spare bytes), then the Y, U and V planes. The writer bumps
the sequence number after writing each frame; a reader that sees the same
number before and after copying knows it got a whole frame.
"""
import mmap
import os
import struct
import tempfile

import numpy as np

HEADER = struct.Struct("<4sIHH4x")
MAGIC = b"HFPV"


def default_path():
    base = "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir()
    return os.path.join(base, f"hfmodem_preview_{os.getuid() if hasattr(os, 'getuid') else 0}")


class PreviewWriter:
    def __init__(self, path, w, h):
        self.w, self.h = w, h
        self.size = HEADER.size + w * h * 3 // 2
        with open(path, "wb") as f:
            f.truncate(self.size)
        self._f = open(path, "r+b")
        self._mm = mmap.mmap(self._f.fileno(), self.size)
        self.seq = 0
        self._mm[:HEADER.size] = HEADER.pack(MAGIC, 0, w, h)

    def write(self, yuv_bytes):
        """yuv_bytes: one frame, Y then U then V planes."""
        self._mm[HEADER.size:self.size] = yuv_bytes
        self.seq += 1
        self._mm[:HEADER.size] = HEADER.pack(MAGIC, self.seq, self.w, self.h)


class PreviewReader:
    """read() returns a new frame as an RGB uint8 array (h, w, 3), or None
    if there's no new complete frame since the last call."""

    def __init__(self, path):
        self.path = path
        self._mm = None
        self._last_seq = None

    def _open(self):
        try:
            f = open(self.path, "rb")
        except OSError:
            return False
        size = os.fstat(f.fileno()).st_size
        if size < HEADER.size:
            f.close()
            return False
        self._mm = mmap.mmap(f.fileno(), size, access=mmap.ACCESS_READ)
        f.close()
        return True

    def read(self):
        if self._mm is None and not self._open():
            return None
        try:
            magic, seq, w, h = HEADER.unpack(self._mm[:HEADER.size])
        except (ValueError, struct.error):
            self.close()
            return None
        if magic != MAGIC or seq == 0 or seq == self._last_seq:
            return None
        n = w * h * 3 // 2
        if len(self._mm) < HEADER.size + n:  # the writer restarted at a bigger size
            self.close()
            return None
        data = bytes(self._mm[HEADER.size:HEADER.size + n])
        if HEADER.unpack(self._mm[:HEADER.size])[1] != seq:
            return None  # overwritten while copying: take the next one
        self._last_seq = seq
        return yuv420_to_rgb(np.frombuffer(data, np.uint8), w, h)

    def close(self):
        if self._mm is not None:
            self._mm.close()
        self._mm = None
        self._last_seq = None  # a new writer counts from 1 again


def yuv420_to_rgb(f, w, h):
    """BT.601 limited-range YUV 4:2:0 planes -> RGB, nearest-neighbour chroma."""
    ysz, csz = w * h, (w // 2) * (h // 2)
    y = f[:ysz].reshape(h, w).astype(np.float32) - 16.0
    u = f[ysz:ysz + csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
    v = f[ysz + csz:ysz + 2 * csz].reshape(h // 2, w // 2).astype(np.float32) - 128.0
    u = u.repeat(2, 0).repeat(2, 1)
    v = v.repeat(2, 0).repeat(2, 1)
    yy = 1.164 * y
    rgb = np.empty((h, w, 3), np.float32)
    rgb[..., 0] = yy + 1.596 * v
    rgb[..., 1] = yy - 0.392 * u - 0.813 * v
    rgb[..., 2] = yy + 2.017 * u
    return np.clip(rgb, 0, 255).astype(np.uint8)
