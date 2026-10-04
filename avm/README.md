# AVM -- Audio Video Modem (v1.0)

Live video + audio over an OFDM radio link (DRM-style modes A-D, 20-250 kHz
wide), PlutoSDR or LimeSDR, running on a Raspberry Pi 4 with a 7" touch
screen. The modem scripts keep their `hf_ofdm_*` names; AVM is the product
around them. Name and version live in `avm_version.py`.

## Layout

| Path | What |
|---|---|
| `*.py` | All source. Main pieces below. |
| `rx_regression/` | Test recordings + baseline for `rx_regression.py` |
| `testdata/` | Pluto IQ captures, simulated HF capture, test audio |
| `refs/` | Wavelet codec reference / comparison videos |
| `tools/` | Test and measurement scripts (latency, audio, CPU, layout) |
| `deploy_pi.ps1` | Copy changed files to the Pi |
| `_ldpc_tables_*.npz` | LDPC table cache (rebuilt automatically if missing) |

### Main pieces

- **Modem:** `hf_ofdm_tx.py`, `hf_ofdm_rx.py`, `hf_ofdm_common.py`,
  `hf_ofdm_ldpc.py`, `rx_frontend.py`, `pluto_soapy_sink.py` (Pluto/Lime I/O)
- **Media TX:** `media_tx_framer.py` (packs audio/video/station ID into
  fragments), `media_source_wavelet.py` + `wavelet_codec.py` (video),
  `media_source_audio.py` (Codec2/Opus), `media_source.py` (capture args)
- **Media RX:** `media_rx_player.py` (decode, playback, A/V)
- **GUIs:** touch GUI `touch_gui.py`, `touch_tx_page.py`, `touch_rx_page.py`,
  `touch_widgets.py` (the AVM app); full desktop GUIs `media_tx_gui.py`,
  `media_rx_gui.py` (also the touch GUI's hidden engines)

## Installing on a new machine

Published at github.com/m0dts/avm: `python tools/make_release.py` builds
`release/` (a git repo pushed to GitHub: `avm/` + both installers). Each
installer on its own downloads AVM from GitHub; run from a copied release
folder, it uses that copy instead. Both are safe to re-run: `--update` /
`-Update` fetches the latest AVM, and `--check` / `-Check` only reports.

- **Linux** (`install_avm.sh`): Ubuntu 22.04+, Debian 12+ or Raspberry Pi OS,
  x86_64 or ARM64. AVM goes in `~/avm`. Installs the apt packages, builds
  SoapyPlutoSDR if apt lacks it, makes `~/venv`, sets up USB access (RTL-SDR
  TV-driver blacklist, groups) and an AVM launcher.
- **Windows** (`install_avm.bat`, which runs `install_avm_windows.ps1`): AVM
  goes in `%USERPROFILE%\avm`. Uses radioconda / any conda, else installs
  Miniforge. Builds a conda-forge env in `%LOCALAPPDATA%\AVM\env`. Uses an
  existing ffmpeg, else downloads one. Writes `AVM.bat` plus Desktop and
  Start-menu shortcuts. No admin rights needed.

## Raspberry Pi (rob@192.168.1.36)

- `~/avm` -- the same source as here (plus `tools/`), run by the AVM desktop
  shortcut; logs in `~/avm/gui_logs/` (`rx_session.log`, `tx_session.log`).
  The large test data stays on the PC.
- Python: `~/venv/bin/python`. Settings: `~/.config/hfmodem/touch.json`.
- Deploy: `.\deploy_pi.ps1 file.py ...` (or no arguments: every .py changed
  since the last deploy; `-All`: all source + tools). GUI-side changes need
  the AVM GUI restarted; `hf_ofdm_*` changes take effect on the next TX/RX start.
- `~/v3_ag`, `~/v3_ag_touch` -- the old folders, kept for now; not deployed to.
- The Pi 4 runs TX and RX together; CPU cost matters for every change.

## Checks

- `python rx_regression.py check` -- receiver output must stay IDENTICAL
  (`make` / `baseline` to regenerate). Run after any `hf_ofdm_rx`/common change.
- `tools/` -- see the docstring at the top of each script; the `.sh` ones run
  on the Pi (`/tmp`), e.g. `lat_test.py` (radio-link latency),
  `cam_latency.py` (camera capture delay), `after_check.sh` (audio/video health
  of the current RX run).
