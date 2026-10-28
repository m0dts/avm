"""Python access to framing.cpp (framing.dll / libframing.so beside this
file): Framer (media_tx_framer.py's packing) and Deframer
(media_rx_player.py's record handling)."""
import ctypes
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
from _avmlib import lib as _avm_lib  # noqa: E402

_lib = _avm_lib()
_P, _I, _D = ctypes.c_void_p, ctypes.c_int, ctypes.c_double
_lib.fr_create.restype = _P
_lib.fr_create.argtypes = [_I, ctypes.c_char_p, _I]
_lib.fr_free.argtypes = [_P]
_lib.fr_set_audio_profile.argtypes = [_P, _I]
_lib.fr_set_video_config.argtypes = [_P, ctypes.c_char_p, _I]
_lib.fr_set_message.argtypes = [_P, ctypes.c_char_p, _I]
_lib.fr_add_audio.argtypes = [_P, ctypes.c_char_p, _I]
_lib.fr_add_video.argtypes = [_P, ctypes.c_char_p, _I, _I]
_lib.fr_tick.argtypes = [_P, _D]
_lib.fr_tick.restype = _I
_lib.fr_fragment.argtypes = [_P, _I, ctypes.c_char_p]
_lib.df_create.restype = _P
_lib.df_create.argtypes = [_I]
_lib.df_free.argtypes = [_P]
_lib.df_push.argtypes = [_P, ctypes.c_char_p, _I, _D]
_lib.df_push.restype = _I
_lib.df_event_type.argtypes = [_P, _I]
_lib.df_event_type.restype = _I
_lib.df_event_len.argtypes = [_P, _I]
_lib.df_event_len.restype = _I
_lib.df_event_data.argtypes = [_P, _I, ctypes.c_char_p]


class Framer:
    def __init__(self, fragment_size, station_id=None, msg_id0=0):
        self.fragment_size = fragment_size
        self._h = _lib.fr_create(fragment_size, (station_id or "").encode("ascii"), msg_id0)

    def __del__(self):
        if getattr(self, "_h", None):
            _lib.fr_free(self._h)

    def set_audio_profile(self, pid):
        _lib.fr_set_audio_profile(self._h, pid)

    def set_video_config(self, record):
        _lib.fr_set_video_config(self._h, record, len(record))

    def set_message(self, data):
        d = data or b""
        _lib.fr_set_message(self._h, d, len(d))

    def tick(self, audio_items, video_items, now):
        for a in audio_items:
            _lib.fr_add_audio(self._h, a, len(a))
        for v, key in video_items:
            _lib.fr_add_video(self._h, v, len(v), 1 if key else 0)
        n = _lib.fr_tick(self._h, now)
        out = []
        for i in range(n):
            b = ctypes.create_string_buffer(self.fragment_size)
            _lib.fr_fragment(self._h, i, b)
            out.append(b.raw)
        return out


class Deframer:
    def __init__(self, fragment_size):
        self._h = _lib.df_create(fragment_size)

    def __del__(self):
        if getattr(self, "_h", None):
            _lib.df_free(self._h)

    def push(self, block, now):
        n = _lib.df_push(self._h, block, len(block), now)
        ev = []
        for i in range(n):
            ln = _lib.df_event_len(self._h, i)
            b = ctypes.create_string_buffer(max(ln, 1))
            _lib.df_event_data(self._h, i, b)
            ev.append((_lib.df_event_type(self._h, i), b.raw[:ln]))
        return ev
