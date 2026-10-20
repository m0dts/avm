# AVM modes: which one to use

AVM has seven modes. They trade speed against how much the path can
throw at them: weak signals, echoes (multipath) and fading. Pick the
**mode type** in Config first (HF, VHF/UHF or SAT): the TX and RX pages
then only offer that type's modes. Both ends must use the same mode, width
and modulation.

## Quick choice

| Your path | Start with | If it struggles |
|---|---|---|
| HF ground wave, NVIS, or a steady sky-wave path | **A** 16QAM | **SH** 8PSK, then A QPSK |
| HF sky wave, typical | **SH** 8PSK, or **B** 16QAM | SH QPSK or B QPSK |
| HF long distance, fast fading (flutter) | **C** QPSK | **D** QPSK |
| HF with long echoes (multi-hop, round the world) | **D** QPSK | narrower width |
| VHF/UHF: line of sight, mobile, tropo, rain scatter | **VU** 16QAM | VU QPSK |
| Satellite (e.g. QO-100), cable, or any steady path | **S** 8PSK | S QPSK |

**Speed at a glance:** 10 kHz carries about 6-16 kbit/s, depending on mode
and modulation. Video needs roughly 5 kbit/s or more on top of the audio.
Codec2 audio needs 3.2 kbit/s.

## How to read the numbers

- **MER needed** is what the RX status line shows ("MER meas/req"). It is
  where 90% of frames decode in plain noise, measured with AVM's own
  receiver. The SNR in the signal's own bandwidth is about the same, within
  about 1 dB. On a fading path, allow **3-6 dB more**.
- **Echo span** is the longest echo delay the mode handles cleanly. Echoes
  are the same signal arriving again later, from another path (multi-hop HF,
  or reflections on VHF). An echo longer than this adds interference.
- **Fading** is how fast the path may change. On HF this is the Doppler
  spread: about 0.1-1 Hz on a quiet path, 1-2 Hz on a disturbed one, up to
  5-10 Hz for polar or flutter fading. On VHF/UHF it comes from movement:
  about 50 Hz at 432 MHz and 130 km/h. These limits are design limits, from
  how often each mode measures the path. Real fading needs some margin below
  them.
- **Modulations:**
  - QPSK is the most robust.
  - 8PSK and 16PSK carry more data. They are for the single-carrier modes
    only: 8PSK for S and SH, 16PSK for SH.
  - 16QAM and 32QAM are the fastest, and need the strongest signal. They are
    for every mode except S and SH.

## The modes

### Mode A: HF, gentle paths

DRM-style OFDM with many closely spaced carriers (41.7 Hz apart) and the
shortest guard time. It is the fastest of the HF OFDM modes.

- **Good for:** ground wave, NVIS, and steady single-hop sky wave; a strong
  signal you want the most data through.
- **Echo span:** 2.7 ms. **Fading:** slow only, up to about 2 Hz.
- **Widths:** 10-250 kHz.

### Mode B: HF, general purpose

Like A, but with twice the guard time and denser channel measurement, so
it handles longer echoes and faster fading. In exchange, it carries about
40% less data than A.

- **Good for:** most sky-wave HF.
- **Echo span:** 5.3 ms. **Fading:** up to about 3 Hz.
- **Widths:** 10-250 kHz.

### Mode C: HF, long distance and fast fading

Wider carrier spacing (68 Hz) and channel measurement on every other
symbol, so it follows fast fading best of the HF OFDM modes.

- **Good for:** long-distance sky wave, disturbed paths, flutter.
- **Echo span:** 5.3 ms. **Fading:** up to about 6 Hz.
- **Widths:** 10-250 kHz.

### Mode D: HF, severe echoes and fading

The longest guard time of all the OFDM modes (it is longer than the
useful part of each symbol) and the widest carrier spacing (107 Hz).

- **Good for:** the worst HF paths: multi-hop with long echoes, plus fast
  fading. It carries about half the data of mode A.
- **Echo span:** 7.3 ms. **Fading:** up to about 5 Hz.
- **Widths:** 10-250 kHz.

### Mode SH: HF, single carrier

One carrier instead of many. Its peak power is about 6 dB lower than the
OFDM modes, so it gets more out of an amplifier. Its receiver measures the
path from a known sequence sent every 32 ms (at 10 kHz), and cleans up the
measurement using the decoded data.

- **Good for:** HF where the signal is weak but the path changes slowly.
  It needs the lowest MER of any AVM mode: QPSK decodes at 1.8 dB.
- **Not for:** fast fading. It copes with gentle fading (about 1 Hz), but
  falls apart on flutter. Use C or D there.
- **Echo span:** 8 ms at every width. **Fading:** about 1 Hz.
- **Widths:** 10, 20, 40 kHz. **Modulation:** QPSK, 8PSK, 16PSK.
- A frame lasts about 1 s at 10 kHz, so a deep fade can lose a whole frame.
  On a fading path, QPSK survives fades that 8PSK does not.

### Mode VU: VHF/UHF

Not DRM: wide carrier spacing (2.5 kHz) so it survives the Doppler from
moving vehicles, and a short guard for the short echoes found above 30 MHz.

- **Good for:** 144/432 MHz and up: line of sight, mobile, troposcatter,
  aircraft and rain scatter. The fastest mode.
- **Not for:** HF. Its echo span is far too short for sky wave.
- **Echo span:** 0.033 ms (about 10 km of extra path).
  **Fading:** fast, over 100 Hz.
- **Widths:** 80, 160, 250 kHz.

### Mode S: satellite and steady paths

Single carrier with a short equaliser, for paths with no real fading and
no long echoes.

- **Good for:** QO-100 and other satellites, cable links, and steady
  line-of-sight paths. Its peaks are lower than OFDM's (about 4.5 dB above
  average), which suits a satellite uplink amplifier.
- **Not for:** HF or mobile: it has no defence against long echoes or fast
  fading.
- **Echo span:** about 1 ms. **Fading:** steady paths only.
- **Widths:** 10, 20, 40 kHz. **Modulation:** QPSK, 8PSK.

## Thresholds and data rates

The MER needed is for 90% of frames in plain noise, measured. Where it
differs between widths, the range is shown. The data rate is the payload
the mode carries at 10 kHz. Other widths scale with width (20 kHz is about
2×, 40 kHz about 4×). VU is shown at 80 kHz.

| Mode | Modulation | MER needed (dB) | Data rate | Echo span | Fading |
|---|---|---|---|---|---|
| A | QPSK | 3.1-3.7 | 6.4 kbit/s | 2.7 ms | ~2 Hz |
| A | 16QAM | 6.0-7.2 | 12.8 kbit/s | | |
| A | 32QAM | 8.6-10.4 | 16.0 kbit/s | | |
| B | QPSK | 3.0 | 3.9 kbit/s | 5.3 ms | ~3 Hz |
| B | 16QAM | 5.9-6.3 | 7.7 kbit/s | | |
| B | 32QAM | 8.8-9.4 | 9.7 kbit/s | | |
| C | QPSK | 2.9-3.0 | 3.4 kbit/s | 5.3 ms | ~6 Hz |
| C | 16QAM | 5.9-6.6 | 6.9 kbit/s | | |
| C | 32QAM | 8.5-9.3 | 8.6 kbit/s | | |
| D | QPSK | 3.2-3.3 | 3.4 kbit/s | 7.3 ms | ~5 Hz |
| D | 16QAM | 5.3-5.9 | 6.9 kbit/s | | |
| D | 32QAM | 7.9-8.6 | 8.6 kbit/s | | |
| SH | QPSK | 1.8 | 6.0 kbit/s | 8 ms | ~1 Hz |
| SH | 8PSK | 4.9-5.3 | 9.0 kbit/s | | |
| SH | 16PSK | 8.5-8.6 | 12.0 kbit/s | | |
| S | QPSK | 3.2-3.4 | 7.5 kbit/s | ~1 ms | steady |
| S | 8PSK | 6.9 | 11.3 kbit/s | | |
| VU (80 kHz) | QPSK | 3.0 | 53 kbit/s | 0.033 ms | >100 Hz |
| VU (80 kHz) | 16QAM | 6.9-7.0 | 106 kbit/s | | |
| VU (80 kHz) | 32QAM | 9.9-10.1 | 133 kbit/s | | |

Notes:

- The data rates are what the mode carries while transmitting. The
  preamble and header of each fragment take a little more, so expect
  5-10% less in practice. The TX page shows the real link rate for your
  settings.
- Each step up in modulation needs about 3 dB more signal. A clear
  **16QAM** link at 12 dB MER has room to try **32QAM**; at 6 dB, drop to
  **QPSK**.
- A narrower width puts the same power into less bandwidth. Halving the
  width gains about 3 dB of MER, at half the data rate.
- The RX status line shows both numbers, as "MER measured/required" for
  your mode, width and modulation. Aim for a few dB above the required
  figure on a fading path.
