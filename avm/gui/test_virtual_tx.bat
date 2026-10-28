@echo off
rem A test transmission on the virtual radio (virtual_radio.py: UDP on this
rem machine) for avm_gui's RX page with Radio = Virtual, Freq 144 (MHz):
rem test video (256x144, 6 fps) + a 650 Hz Codec2 tone, station M0DTS,
rem mode A 80 kHz QPSK LDPC, for 10 minutes. Close the window to stop.
setlocal
set "ENV=%LOCALAPPDATA%\AVM\env"
set "PATH=C:\ffmpeg\bin;%ENV%;%ENV%\Library\bin;%ENV%\Scripts;%PATH%"
set "PY=%ENV%\python.exe"
cd /d "%~dp0.."
set SECS=600
set MODEM=--mode A --occupancy 80 --modulation qpsk --fec ldpc --fragment-size 1024 --sample-rate 550000
"%PY%" media_tx_framer.py --fragment-size 1024 --station-id M0DTS --video-codec wavelet --video-framerate 6 --video-resolution 256x144 --video-source-cmd "\"%PY%\" media_source_wavelet.py --source test --resolution 256x144 --framerate 6 --frame-bytes 400 --duration %SECS%" --audio-codec codec2 --audio-codec2-mode 3200 --audio-source-cmd "\"%PY%\" media_source_audio.py --source test --audio-codec codec2 --codec2-mode 3200 --tone-hz 650 --duration %SECS%" 2>nul | "%PY%" hf_ofdm_tx.py %MODEM% --fragment-gap-ms 0 --amplitude 0.1 --stream --output pluto --sdr virtual --rf-freq 144000000 --max-input-backlog 8
