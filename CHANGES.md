# AVM changes

Newest first. Each release adds a section; AVM's Update button shows the
ones newer than the version you have.

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
