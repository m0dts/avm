"""wavelet_codec.WaveletCodec with the per-frame work in C++ (wavelet_core.cpp,
built as wavelet_core.dll / libwavelet_core.so beside this file). Same
constructor, encode, decode, decode_bgr, picture, reset and seq; packets
and pictures are bit-identical to the Python codec's, so either end can use
either.

The one-time layout (subband order, weights, refresh-tile order) is still
made by wavelet_codec.py and handed to the library, so both use identical
tables. rd_trial (an encoder experiment, off in AVM) is not supported here."""
import ctypes
import os
import sys

import numpy as np

import wavelet_codec as _wc

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P = ctypes.c_void_p
_I64 = ctypes.c_int64
_lib.wvt_create.restype = _P
_lib.wvt_create.argtypes = [_I64] * 5 + [ctypes.c_int] * 3 + [_I64] * 4 + [_P] * 14
_lib.wvt_free.argtypes = [_P]
_lib.wvt_reset.argtypes = [_P]
_lib.wvt_set_seq.argtypes = [_P, _I64]
_lib.wvt_get_seq.argtypes = [_P]
_lib.wvt_get_seq.restype = _I64
_lib.wvt_encode.argtypes = [_P, _P, _P, _P, _P]
_lib.wvt_encode.restype = ctypes.c_int
_lib.wvt_decode.argtypes = [_P, _P, _I64, _P, _P, _P]
_lib.wvt_decode.restype = ctypes.c_int
_lib.wvt_picture.argtypes = [_P, _P, _P, _P]


def _ptr(a):
    return a.ctypes.data_as(ctypes.c_void_p)


class WaveletCodec(_wc.WaveletCodec):
    def __init__(self, width, height, frame_bytes, leak=0.996, refresh_frames=60, adaptive=True,
                 rd_trial=False, motion=True, block_motion=None):
        if rd_trial:
            raise ValueError("rd_trial isn't supported by the C++ codec")
        self._h = None
        super().__init__(width, height, frame_bytes, leak=leak, refresh_frames=refresh_frames,
                         adaptive=adaptive, rd_trial=False, motion=motion, block_motion=block_motion)
        lay = self.lay
        i64 = lambda a: np.ascontiguousarray(a, np.int64)
        f64 = lambda a: np.ascontiguousarray(a, np.float64)
        # kept alive for the library's lifetime (it copies them, but cheaply)
        self._tabs = [i64(lay.sb_off), i64(lay.sb_h), i64(lay.sb_w), i64(lay.sb_cls), i64(lay.sb_par),
                      i64(lay.sb_boff), i64(lay.sb_goff), i64(self._gidx), f64(self._wflat), i64(self._invw),
                      f64(self._ana_scale), i64(lay.coef_cls), i64(lay.coef_tile), i64(self._geo).reshape(-1)]
        self._h = _lib.wvt_create(width, height, frame_bytes, self.leak_q, self.n_bands,
                                  int(bool(adaptive)), int(bool(motion)), int(bool(self.block_motion)),
                                  lay.n_coef, lay.n_blocks, lay.n_groups, len(lay.bands),
                                  *[_ptr(a) for a in self._tabs])
        self._shapes = [(H, W) for (H, W, _L, _cw, _c) in lay.comps]
        _lib.wvt_set_seq(self._h, self.__dict__.get("_seq0", 0))

    def __del__(self):
        h = getattr(self, "_h", None)
        if h:
            _lib.wvt_free(h)
            self._h = None

    # seq lives in the library once it exists (the TX sets it back to 0 after a warm-up)
    @property
    def seq(self):
        return _lib.wvt_get_seq(self._h) if self._h else self.__dict__.get("_seq0", 0)

    @seq.setter
    def seq(self, value):
        if getattr(self, "_h", None):
            _lib.wvt_set_seq(self._h, int(value))
        else:
            self.__dict__["_seq0"] = int(value)

    def reset(self):
        if getattr(self, "_h", None):
            _lib.wvt_reset(self._h)
        else:
            super().reset()

    def _planes(self):
        return [np.empty(s, np.uint8) for s in self._shapes]

    def picture(self):
        y, u, v = self._planes()
        _lib.wvt_picture(self._h, _ptr(y), _ptr(u), _ptr(v))
        return [y, u, v]

    def encode(self, y, u, v):
        y = np.ascontiguousarray(y, np.uint8)
        u = np.ascontiguousarray(u, np.uint8)
        v = np.ascontiguousarray(v, np.uint8)
        out = np.empty(self.frame_bytes, np.uint8)
        r = _lib.wvt_encode(self._h, _ptr(y), _ptr(u), _ptr(v), _ptr(out))
        if r != self.frame_bytes:
            raise RuntimeError(f"wavelet_core encode failed ({r})")
        return out.tobytes()

    def decode(self, pkt):
        data = np.frombuffer(bytes(pkt), np.uint8)
        y, u, v = self._planes()
        r = _lib.wvt_decode(self._h, _ptr(data), len(data), _ptr(y), _ptr(u), _ptr(v))
        if r != 0:
            raise ValueError("bad wavelet packet")
        return [y, u, v]
