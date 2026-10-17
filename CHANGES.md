# AVM changes

Newest first. Each release adds a section; AVM's Update button shows the
ones newer than the version you have.

## 1.0.19 (2026-10-17)
- Compatible over the air with 1.0.18 except video: the video format is new, so both ends need 1.0.19 for pictures (audio and data still work with 1.0.18)
- Better video at low bitrates (about 2-3.5 dB at 5 kbps on camera video), with motion compensation, and a B/W option in Config
- Much faster start: TX and RX are loaded in advance and a PlutoSDR is opened ready -- RX now starts in under a second (was 5-6 s). Coloured blocks in the video area show start-up progress
- Lower CPU on the Raspberry Pi: the transmitter is about 3x faster, and video encoding and decoding are lighter
- Cleaner transmitted spectrum: the shelf or 'tone' either side of the signal is gone (about 40 dB lower)
- RX automatic frequency control: a signal several kHz off tune now pulls in and decodes normally (most useful in VU). The CFO shown is the full offset
- RX status shows measured/required MER -- required is the plain-noise threshold for your mode, width and modulation
- Tap the RX status line for a live constellation display
- Spectrum: smoother (no spikes from preambles), auto scale in 1/2/4/5 dB per division with the noise near the bottom, and no accidental scrolling on the touchscreen
- ANAN-10/100 (openHPSDR) as a receive radio -- experimental
- Fixes: log noise on every TX restart, band-edge guides now the same colour, a spur on the VU 80 kHz spectrum

## 1.0.18 (2026-10-13)
- Compatible over the air with 1.0.17, except the new 32QAM (both ends need 1.0.18 to use it)
- New 32QAM modulation: about 20% more throughput than 16QAM for about 2 dB more signal. Tested on air at 10 kHz -- higher bandwidths untested at 32QAM
- New 10 kHz width, with 1 and 2 fps video and a 128x80 resolution: video works even in QPSK at 10 kHz
- KiwiSDR as a receive radio: set its address under Config, KiwiSDR, then pick KiwiSDR as the RX radio (10 kHz, modes A-D)
- Better HF reception in modes A, B and C: a new channel estimate copes far better with multipath and frequency-selective fading (receiver only, works with any transmitter)
- TX peak reduction (Config, TX peak reduce): 2-4 dB more effective power from the same amplifier peak, decodable by any AVM receiver
- RX spectrum Normal/Inverted option (Config) for receive paths that mirror the spectrum
- At low frame rates the first picture now appears within a few seconds (was up to 25 s at 1 fps)
- Radio problems now go to the errors list, whose button turns red when it has something
- TX/RX refuse to start on a frequency outside the radio's range
- Spectrum: auto level follows the true peak, wider level range, averaging None option, and the averaging choice now sticks
- Lock lamps react faster
- Fixes: Opus warning shown while using Codec2, false video warning at 1 fps, error pop-up when stopping TX, LimeSDR calibration pop-ups at HF
- Config page: newer settings moved to the right-hand column

## 1.0.17 (2026-10-11)
- NOT compatible with earlier versions over the air: update both ends
- All modes: fragment header scrambled -- fixes spectral leakage either side of mode A (worst at 160/250 kHz) and its header decode failures
- VU: the centre carrier is now used too (slightly higher bitrate)
- Decoder no longer holds up other work on the Pi (smoother TX/RX, fewer gaps)
- Smaller gaps between transmitted fragments
- Header lamp now only judges headers (no longer turns orange when fragments are lost as the signal improves)
- USB audio devices that don't support 48 kHz now play at their own rate
- GUI no longer freezes after a very large fragment loss
- Quicker first start (codec and modem warm-up)

## 1.0.16 (2026-10-10)
- 16QAM and VU 80/160 kHz: receiver found each frame's start on the first try again (since 1.0.1 most fragments were decoded twice) -- about half the RX CPU, no more breaks at 16QAM 160
- TX timing now matches each fragment's real air time (the framer ran 1.6% fast and the transmitter dropped the excess)
- groundwork for same-channel duplex (TDD), off by default

## 1.0.15 (2026-10-10)
- 16QAM video fixed: video frames bigger than a fragment are now spread over the following fragments instead of sent all at once, which made the transmitter drop most of the stream

## 1.0.14 (2026-10-10)
- SDRplay: fixed RX not starting (RSPduo 'no match')
- README: supported hardware tables and SDRplay install steps

## 1.0.13 (2026-10-10)
- SDRplay RSPduo listed once (it showed once per mode) and used in single-tuner mode
- CPU figure turns orange-red above 90%
- narrower warning button

## 1.0.12 (2026-10-10)
- Radio list opens at once and searches in the background
- SDRplay RSPs supported as RX radios (installer offers SDRplay's API from a USB drive or Downloads)
- update notes in a scrolling full-width window
- on a Pi the update's installer runs inside AVM (full screen, no terminal or on-screen keyboard) and AVM restarts by itself
- installer says when it's checking for updates

## 1.0.11 (2026-10-10)
- Radio list: a Pluto found at a numeric address (e.g. 192.168.2.1) is listed once, without its duplicate USB and pluto.local entries

## 1.0.10 (2026-10-10)
- Radio list: Refresh button to search again
- pop-up lists and keypad sit below the tabs instead of over them
- option buttons wide enough for their labels (Auto was cut off)

## 1.0.9 (2026-10-10)
- Screen rotation removed from AVM and the installer: set the display up in the system instead

## 1.0.8 (2026-10-10)
- Installer: a package source's signing-key warning (e.g. Raspberry Pi's new key) shown as a plain note, not apt's error text

## 1.0.7 (2026-10-10)
- Airspy R2/Mini and Airspy HF+ supported as RX radios

## 1.0.6 (2026-10-10)
- LibreSDR support: Pluto-firmware radios found at 192.168.2.1 or 192.168.1.10 (Ethernet), whichever answers
- AVM logo colours (A red, V green, M blue)

## 1.0.5 (2026-10-08)
- Audio only: Opus up to 48 kbps (was 16)
- TX says AUDIO ONLY instead of NO VIDEO when that's the Content setting
- TX warns when audio only leaves most of the link unused (a narrower kHz reaches further)

## 1.0.4 (2026-10-08)
- New Config tab: radios, camera, mic, audio out, content (auto/audio only/video only), callsign, spectrum averaging
- RX Header and Frame lock lamps (green/orange/red over 2 s) replace % ok
- spectrum averaging applies live
- settings saved to disk as you go

## 1.0.3 (2026-10-07)
- RX tuning indicator also follows automatic width changes (e.g. switching to VU)
- README: experimental release, designed for HF and unstable paths, screenshots fixed

## 1.0.2 (2026-10-07)
- Update check compares version numbers (no GitHub API, never offers an older version) and shows what's new
- CHANGES.md lists each release

## 1.0.1 (2026-10-07)
- Version numbering: 1.0.x, shown in the title bar; Update shows the new version and what changed
- RX spectrum tuning indicator: signal width and frequency pull-in zones
- RX frequency offset (+-100 kHz, 1 kHz steps, live), applied from start-up
- Mode VU: longer preamble, weak signals found ~1 dB better at 80 kHz (both ends need this version for VU)
- Radio list shows a Pluto as USB or IP; radio checked before TX/RX start
- Windows: no console windows; installer uses an ffmpeg with Codec2 and installs to your user folder
