#!/usr/bin/env python3
"""Start-up warm-up, run by touch_gui.py behind its "Preparing codecs"
splash: compiles (and caches on disk) the code TX and RX would otherwise
compile on their first start -- after every update, since new files mean a
fresh compile. Without the modem part, the first RX start after an update
spent 15-30 s compiling (on a Raspberry Pi 4) while live samples piled up.

1. the wavelet video codec: one tiny frame encoded and decoded;
2. the modem: two fragments built as TX builds them (LDPC, resampled to a
   radio rate) and decoded by the real receiver, through its front end.
"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))


def warm_codec():
    from wavelet_codec import WaveletCodec
    w, h = 192, 112
    z = lambda a, b: np.zeros((b, a), np.uint8)
    enc, dec = WaveletCodec(w, h, 200), WaveletCodec(w, h, 200)
    dec.decode(enc.encode(z(w, h), z(w // 2, h // 2), z(w // 2, h // 2)))


def warm_modem():
    import hf_ofdm_common as ofdm
    radio_fs = 550000
    cfg = ofdm.build_config("VU", 160, data_modulation="16qam", fec_scheme="ldpc")
    frame = ofdm.build_frame(cfg, bytes(8 + 1024)).astype(np.complex64)
    w = ofdm.fft_resample_rate(frame, cfg.fs, radio_fs)
    gap = np.zeros(radio_fs // 10, np.complex64)
    iq = (np.concatenate([gap, w, w, gap]) * 0.1).astype(np.complex64)
    subprocess.run([sys.executable, os.path.join(HERE, "hf_ofdm_rx.py"),
                    "--mode", "VU", "--occupancy", "160", "--modulation", "16qam", "--fec", "auto",
                    "--fragment-size", "1024", "--sample-rate", str(radio_fs),
                    "--lo-offset-hz", "100000", "--input", "pipe"],
                   input=iq.tobytes(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   cwd=HERE, timeout=600)


if __name__ == "__main__":
    warm_codec()
    warm_modem()
