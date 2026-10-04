# AVM: Audio Video Modem

AVM sends live video and audio over a radio link using SDR hardware. A camera
and microphone at one end, a picture and sound at the other, carried by an
OFDM modem. It has a full-screen touch interface and is built to run on a
Raspberry Pi 4 with a 7" touch screen, which can transmit and receive at the
same time. It also runs on an Ubuntu/Debian PC, and should run on Windows.

## Features

- **OFDM modem** with LDPC error correction, QPSK or 16QAM. Robust DRM-style
  modes A to D, plus mode VU for VHF/UHF mobile and troposcatter paths.
  Signal widths of 20, 40, 80, 160 and 250 kHz.
- **Wavelet video codec** designed for low, fixed bitrates. It degrades
  gracefully, and a receiver joining mid-stream builds up the picture within
  a few seconds.
- **Opus or Codec2 audio**, kept in sync with the video.
- **Touch GUI**: TX and RX pages, live spectrum, signal quality (MER and
  frequency offset), station callsign, camera preview.
- **Radios** (via SoapySDR): ADALM-Pluto and LimeSDR for TX and RX, and
  RTL-SDR for RX only.

On narrow settings where there's no room for video, it sends audio only and
tells you so.

## Install

**Linux (Ubuntu 22.04+, Debian 12+, Raspberry Pi OS, 64-bit):**

```bash
wget https://raw.githubusercontent.com/m0dts/avm/main/install_avm.sh
bash install_avm.sh
```

This downloads AVM into `~/avm` and installs everything it needs. It also
adds an **AVM** icon to the menu and desktop. Run it again with `--update` to
get the latest version.

**Windows 10/11 (64-bit):**

1. Download
   [install_avm.bat](https://raw.githubusercontent.com/m0dts/avm/main/install_avm.bat)
   (right-click → Save link as).
2. Double-click it. If Windows SmartScreen warns about it, click
   **More info → Run anyway**.
3. Start AVM from the **AVM** shortcut on the desktop or in the Start menu.

The installer needs no admin rights. It does the following:

- downloads AVM into `%USERPROFILE%\avm`;
- uses radioconda if you have it (or another conda), and otherwise installs
  Miniforge just for you;
- sets up a private Python environment with the SDR drivers for Pluto, Lime
  and RTL-SDR, in `%LOCALAPPDATA%\AVM`;
- uses your ffmpeg if it finds one (on PATH or in `C:\ffmpeg\bin`), and
  otherwise downloads one.

The first install downloads about 0.5 GB. Run `install_avm.bat -Update` to
get the latest version of AVM.

An RTL-SDR on Windows also needs its USB driver switched to WinUSB once, with
[Zadig](https://zadig.akeo.ie).

The first transmit or receive after installing takes a minute or two while
the modem and codec are compiled for your machine. After that, starts are
quick.

## Use

1. Plug in your radio and start AVM.
2. **TX page:** pick the radio, frequency, mode and width, then the camera and
   microphone (or the test pattern), and press **TX**.
3. **RX page:** set the same frequency, mode and width, and press **RX**.

Both ends must use the same mode, width and modulation.

Transmitting requires an appropriate licence (e.g. an amateur radio
licence), and you must stay within your band and power limits.

## Author

Rob, M0DTS
