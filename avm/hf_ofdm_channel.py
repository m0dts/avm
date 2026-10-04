#!/usr/bin/env python3
"""
Test-only channel simulator: reads complex64 IQ from stdin, applies AWGN
and (optionally) a two-path Watterson-style multipath/Doppler channel,
writes complex64 IQ to stdout. Meant to sit between hf_ofdm_tx.py and
hf_ofdm_rx.py in a pipeline for testing.

Usage:
    ... | python3 hf_ofdm_channel.py --snr 15 > out.iq
    ... | python3 hf_ofdm_channel.py --snr 12 --delay-ms 0,6 \\
              --gains-db 0,-3 --doppler-hz 1.5,4 --fs 20531.25 > out.iq

--fs must match the tx's sample rate (see its stderr output) so delay/
Doppler values are interpreted correctly; if omitted, delay/Doppler are
skipped and only AWGN is applied.
"""
import argparse
import sys

import numpy as np


def doppler_fading(n_samples, fs, doppler_spread_hz, rng):
    if doppler_spread_hz <= 0:
        return np.ones(n_samples, dtype=complex)
    freqs = np.fft.fftfreq(n_samples, d=1.0 / fs)
    psd_shape = np.exp(-0.5 * (freqs / doppler_spread_hz) ** 2)
    noise_freq = rng.standard_normal(n_samples) + 1j * rng.standard_normal(n_samples)
    shaped = np.fft.ifft(noise_freq * np.sqrt(psd_shape)) * n_samples
    return shaped / np.sqrt(np.mean(np.abs(shaped) ** 2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snr", type=float, default=20, help="output SNR in dB")
    ap.add_argument("--fs", type=float, default=None, help="sample rate (Hz), required for multipath")
    ap.add_argument("--delay-ms", type=str, default=None, help="comma-separated path delays, e.g. 0,6")
    ap.add_argument("--gains-db", type=str, default=None, help="comma-separated path gains, e.g. 0,-3")
    ap.add_argument("--doppler-hz", type=str, default=None, help="comma-separated path Doppler spreads, e.g. 1.5,4")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    raw = sys.stdin.buffer.read()
    x = np.frombuffer(raw, dtype=np.complex64).astype(complex)
    print(f"Channel: {len(x)} samples in", file=sys.stderr)

    if args.delay_ms and args.fs:
        delays = [float(v) for v in args.delay_ms.split(",")]
        gains = [float(v) for v in args.gains_db.split(",")] if args.gains_db else [0.0] * len(delays)
        dopplers = [float(v) for v in args.doppler_hz.split(",")] if args.doppler_hz else [0.0] * len(delays)
        delay_samples = [int(round(d * args.fs / 1000)) for d in delays]
        n = len(x) + max(delay_samples)
        y = np.zeros(n, dtype=complex)
        for d, g, dop in zip(delay_samples, gains, dopplers):
            fading = doppler_fading(len(x), args.fs, dop, rng) * 10 ** (g / 20)
            y[d:d + len(x)] += x * fading
        print(f"Channel: applied {len(delays)}-path multipath, delays={delays} ms, "
              f"gains={gains} dB, doppler={dopplers} Hz", file=sys.stderr)
    else:
        y = x.copy()

    sig_power = np.mean(np.abs(y) ** 2)
    noise_power = sig_power / (10 ** (args.snr / 10))
    noise = np.sqrt(noise_power / 2) * (rng.standard_normal(len(y)) + 1j * rng.standard_normal(len(y)))
    y = y + noise
    print(f"Channel: SNR={args.snr} dB, {len(y)} samples out", file=sys.stderr)

    sys.stdout.buffer.write(y.astype(np.complex64).tobytes())


if __name__ == "__main__":
    main()
