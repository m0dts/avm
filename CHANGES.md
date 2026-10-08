# AVM changes

Newest first. Each release adds a section; AVM's Update button shows the
ones newer than the version you have.

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
