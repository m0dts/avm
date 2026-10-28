"""Decodes a framed media stream (what goes into the transmitter: the C++
GUI's media chain, avm_gui --media-test FILE SECONDS, or media_tx_framer.py's
output) with the library's deframer and wavelet decoder -- the receiver's
path -- and reports what it holds.

    python check_media_stream.py FILE [--fragment-size 1024] [--ppm OUT.ppm]"""
import argparse
import ctypes
import sys

import numpy as np

from _avmlib import lib

EV = {1: "audio config", 2: "audio packet", 3: "video config", 4: "video packet", 5: "station ID",
      6: "station ID (tentative)", 7: "message", 8: "station ID reset", 9: "audio profile changed"}


def decode_audio(profile, packets):
    """The audio packets decoded with the system's libopus / libcodec2
    (ctypes), as int16 samples and their rate; (None, 0) without them."""
    import ctypes.util
    if profile == 0:
        name = ctypes.util.find_library("opus")
        if not name:
            return None, 0
        o = ctypes.CDLL(name)
        o.opus_decoder_create.restype = ctypes.c_void_p
        o.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        o.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        err = ctypes.c_int()
        dec = o.opus_decoder_create(48000, 1, ctypes.byref(err))
        out, buf = [], (ctypes.c_short * 5760)()
        for p in packets:
            n = o.opus_decode(dec, p, len(p), buf, 5760, 0)
            if n > 0:
                out.append(np.frombuffer(bytes(buf)[:n * 2], np.int16))
        return (np.concatenate(out) if out else None), 48000
    name = ctypes.util.find_library("codec2")
    if not name:
        return None, 0
    c = ctypes.CDLL(name)
    c.codec2_create.restype = ctypes.c_void_p
    c.codec2_decode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p]
    c2 = c.codec2_create(0)      # CODEC2_MODE_3200
    out, buf = [], (ctypes.c_short * 160)()
    for p in packets:
        for i in range(0, len(p) - 7, 8):
            c.codec2_decode(c2, buf, p[i:i + 8])
            out.append(np.frombuffer(bytes(buf), np.int16))
    return (np.concatenate(out) if out else None), 8000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--fragment-size", type=int, default=1024)
    ap.add_argument("--ppm", default=None)
    a = ap.parse_args()
    L = lib()
    L.df_create.restype = ctypes.c_void_p
    L.df_push.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_double]
    L.df_event_type.argtypes = L.df_event_len.argtypes = [ctypes.c_void_p, ctypes.c_int]
    L.df_event_data.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    L.wvt_create_auto.restype = ctypes.c_void_p
    L.wvt_create_auto.argtypes = [ctypes.c_longlong] * 3 + [ctypes.c_double, ctypes.c_longlong] + [ctypes.c_int] * 3
    L.wvt_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_longlong] + [ctypes.c_char_p] * 3
    data = open(a.file, "rb").read()
    df = L.df_create(a.fragment_size)
    counts, wvt, w, h, decoded, failed, station, last = {}, None, 0, 0, 0, 0, None, None
    profile, audio = None, []
    for i in range(0, len(data) - a.fragment_size + 1, a.fragment_size):
        n = L.df_push(df, data[i:i + a.fragment_size], a.fragment_size, i / a.fragment_size * 0.24)
        for k in range(n):
            t = L.df_event_type(df, k)
            buf = ctypes.create_string_buffer(L.df_event_len(df, k))
            L.df_event_data(df, k, buf)
            d = buf.raw
            counts[EV.get(t, t)] = counts.get(EV.get(t, t), 0) + 1
            if t == 3:
                w, h = int.from_bytes(d[0:2], "big"), int.from_bytes(d[2:4], "big")
                if wvt is None:
                    print(f"video config: {w}x{h}, {int.from_bytes(d[4:6], 'big') / 1000} fps, {d[6:10]}")
                    wvt = L.wvt_create_auto(w, h, 64, 0.996, 60, 1, 1, -1)
            elif t == 4 and wvt:
                y = ctypes.create_string_buffer(w * h)
                u = ctypes.create_string_buffer(w * h // 4)
                v = ctypes.create_string_buffer(w * h // 4)
                if L.wvt_decode(wvt, d, len(d), y, u, v) >= 0:
                    decoded += 1
                    last = (y.raw, u.raw, v.raw)
                else:
                    failed += 1
            elif t == 1:
                profile = d[0]
            elif t == 2:
                audio.append(d)
            elif t == 7:
                print(f"message: sent at {int.from_bytes(d[:4], 'big')}, {d[4:].decode(errors='replace')!r}")
            elif t == 5:
                station = d.decode(errors="replace").strip()
    print("events:", ", ".join(f"{k} {v}" for k, v in counts.items()))
    used = [int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data) - a.fragment_size + 1, a.fragment_size)]
    spare = [a.fragment_size - 2 - u for u in used]
    if spare:
        print(f"fragments {len(spare)}: spare bytes min {min(spare)}, median {sorted(spare)[len(spare) // 2]}, "
              f"max {max(spare)}; with >= 10 spare (room for a message piece): {sum(s >= 10 for s in spare)}")
    if profile is not None and audio:
        sizes = sorted({len(p) for p in audio})
        print(f"audio profile {profile} ({'Opus' if profile == 0 else 'Codec2 3200'}), packet sizes {sizes[:6]}")
        pcm, rate = decode_audio(profile, audio)
        if pcm is not None and len(pcm) > rate:
            x = pcm[rate // 2:].astype(float)           # past the start
            spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            peak = np.argmax(spec) * rate / len(x)
            print(f"audio decoded: {len(pcm) / rate:.1f} s, strongest tone {peak:.0f} Hz, "
                  f"RMS {np.sqrt(np.mean(x ** 2)):.0f}")
    print(f"pictures decoded {decoded}, failed {failed}; station ID {station!r}")
    if a.ppm and last:
        Y = np.frombuffer(last[0], np.uint8).reshape(h, w).astype(float)
        U = np.frombuffer(last[1], np.uint8).reshape(h // 2, w // 2).repeat(2, 0).repeat(2, 1) - 128.0
        V = np.frombuffer(last[2], np.uint8).reshape(h // 2, w // 2).repeat(2, 0).repeat(2, 1) - 128.0
        rgb = np.clip(np.stack([Y + 1.402 * V, Y - 0.344 * U - 0.714 * V, Y + 1.772 * U], -1), 0, 255).astype(np.uint8)
        with open(a.ppm, "wb") as f:     # PPM (no image library needed)
            f.write(b"P6 %d %d 255\n" % (w, h) + rgb.tobytes())
        print("last picture ->", a.ppm)
    sys.exit(0 if decoded and not failed else 1)


if __name__ == "__main__":
    main()
