#!/bin/bash
# AVM installer for Ubuntu (22.04+), Debian (12+) and Raspberry Pi OS
# (bookworm/trixie), on x86_64 or ARM64.
#
#   cd ~/avm && bash install_avm.sh          # install / update everything
#   bash install_avm.sh --check              # only report what's missing
#
# What it does (each step is skipped if already done; safe to re-run):
#   1. apt packages: Python libs, Qt5, SoapySDR + Lime and RTL-SDR modules,
#      libiio, ffmpeg (with Opus and Codec2), arecord, pw-cat, v4l2-ctl
#   2. SoapyPlutoSDR: from apt if offered, else built from source
#   3. ~/venv (with system site-packages) + pyqtgraph and sounddevice
#   4. udev rules / RTL-SDR TV-driver blacklist, user groups
#   5. desktop launcher (menu + desktop icon) for the touch GUI
# Then it checks every module imports and lists the radios Soapy can see.
set -u
AVM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# run from a release folder: the AVM files are in ./avm beside this script
[ ! -f "$AVM_DIR/touch_gui.py" ] && [ -f "$AVM_DIR/avm/touch_gui.py" ] && AVM_DIR="$AVM_DIR/avm"
VENV="${AVM_VENV:-$HOME/venv}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*"; exit 1; }

# ---------------------------------------------------------------- platform
[ -r /etc/os-release ] || die "No /etc/os-release: unsupported system."
. /etc/os-release
FAMILY="$ID ${ID_LIKE:-}"
case "$FAMILY" in
    *debian*|*ubuntu*) ;;
    *) die "This installer supports Ubuntu / Debian / Raspberry Pi OS (found: $PRETTY_NAME).
   Elsewhere, install by hand: Python 3 with numpy, scipy, numba, opencv, av, PyQt5,
   pyqtgraph, sounddevice; SoapySDR with Python bindings and the PlutoSDR / LMS7 /
   RTL-SDR modules; ffmpeg (libopus, libcodec2); arecord; pw-cat; v4l2-ctl." ;;
esac
IS_PI=0
grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null && IS_PI=1
echo "System: $PRETTY_NAME, $(uname -m)$([ $IS_PI = 1 ] && echo ', Raspberry Pi')"
echo "AVM:    $AVM_DIR"
echo "venv:   $VENV"
[ -f "$AVM_DIR/touch_gui.py" ] || die "Run this from the AVM folder (touch_gui.py not found in $AVM_DIR)."
[ "$(id -u)" = 0 ] && die "Run as your normal user, not root (it uses sudo where needed)."
SUDO=sudo

# numba (the modem's and codec's compiler) only exists for 64-bit systems
if [ "$(getconf LONG_BIT)" != 64 ]; then
    die "This is a 32-bit system ($(uname -m)). AVM needs 64-bit (x86_64 or ARM64):
   numba, which compiles the modem and video codec, has no 32-bit builds.
   In a VM: create it as 'Ubuntu (64-bit)' from a 64-bit ISO. If the VM software
   only offers 32-bit, enable virtualisation (VT-x / AMD-V) in the PC's BIOS, and
   on Windows check Hyper-V / Memory Integrity isn't blocking it.
   On a Raspberry Pi: use the 64-bit Raspberry Pi OS."
fi

# Ubuntu: most of what AVM needs (numba, SoapySDR...) is in 'universe';
# enable it and refresh the lists BEFORE checking what apt offers.
if [ $CHECK_ONLY = 0 ]; then
    if [ "$ID" = ubuntu ] && ! grep -rqs '^deb .* universe' /etc/apt/sources.list /etc/apt/sources.list.d/ \
        && ! grep -rqs 'Components:.*universe' /etc/apt/sources.list.d/; then
        $SUDO apt-get install -y software-properties-common >/dev/null 2>&1
        $SUDO add-apt-repository -y universe
    fi
    $SUDO apt-get update || die "apt-get update failed"
fi

# offered = has an installable candidate (Ubuntu 24.04 lists python3-numba
# but has none -- numba then comes from pip, step 3)
have_pkg() {
    local c
    c="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    [ -n "$c" ] && [ "$c" != "(none)" ]
}
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

# SoapySDR's module ABI (0.8 on current releases, 0.7 on Ubuntu 22.04)
SOAPY_ABI=0.8
have_pkg soapysdr0.8-module-lms7 || { have_pkg soapysdr0.7-module-lms7 && SOAPY_ABI=0.7; }

# ---------------------------------------------------------------- 1. apt
PKGS="python3 python3-venv python3-pip python3-numpy python3-scipy python3-numba
      python3-opencv python3-av python3-pyqt5 python3-pyqtgraph python3-sounddevice libportaudio2
      python3-soapysdr soapysdr-tools libsoapysdr-dev
      soapysdr${SOAPY_ABI}-module-lms7 soapysdr${SOAPY_ABI}-module-rtlsdr rtl-sdr
      libiio0 libiio-utils libiio-dev libad9361-dev
      ffmpeg alsa-utils pipewire-bin v4l-utils
      git cmake g++ pkg-config"
# libiio0 was renamed on newer releases
have_pkg libiio0 || PKGS="${PKGS/libiio0/libiio1}"
have_pkg limesuite-udev && PKGS="$PKGS limesuite-udev"
PLUTO_PKG=soapysdr${SOAPY_ABI}-module-plutosdr
have_pkg "$PLUTO_PKG" && PKGS="$PKGS $PLUTO_PKG"

MISSING=""
for p in $PKGS; do
    if ! have_pkg "$p"; then
        warn "package $p not offered by apt on this system -- skipped"
    elif ! installed "$p"; then
        MISSING="$MISSING $p"
    fi
done
say "1. apt packages"
if [ -z "$MISSING" ]; then
    echo "all present"
elif [ $CHECK_ONLY = 1 ]; then
    echo "missing:$MISSING"
else
    $SUDO apt-get install -y $MISSING || die "apt install failed"
fi

# ---------------------------------------------------------------- 2. SoapyPlutoSDR
say "2. SoapyPlutoSDR"
pluto_ok() { SoapySDRUtil --info 2>/dev/null | grep -qi 'PlutoSDRSupport'; }
if pluto_ok; then
    echo "present"
elif [ $CHECK_ONLY = 1 ]; then
    echo "missing (will be built from source)"
else
    SRC="$HOME/src/SoapyPlutoSDR"
    mkdir -p "$HOME/src"
    [ -d "$SRC" ] || git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git "$SRC" || die "git clone failed"
    cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release >/dev/null \
        && make -C "$SRC/build" -j"$(nproc)" \
        && $SUDO make -C "$SRC/build" install && $SUDO ldconfig || die "SoapyPlutoSDR build failed"
    pluto_ok && echo "built and installed" || warn "built, but SoapySDRUtil still doesn't list it"
fi

# ---------------------------------------------------------------- 3. venv
say "3. Python venv ($VENV)"
if [ $CHECK_ONLY = 0 ]; then
    [ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV" || die "venv creation failed"
    "$VENV/bin/pip" install -q --upgrade pyqtgraph sounddevice || die "pip install failed"
fi
PY="$VENV/bin/python"
[ -x "$PY" ] || PY=python3
# Anything apt couldn't supply (or supplied too old) comes from pip instead.
for spec in "numpy numpy" "scipy scipy" "numba numba" "cv2 opencv-python-headless" "av av" \
            "PyQt5 PyQt5" "pyqtgraph pyqtgraph" "sounddevice sounddevice" "SoapySDR -"; do
    set -- $spec
    if "$PY" -c "import $1" 2>/dev/null; then
        printf '  %-12s ok\n' "$1"
    elif [ "$2" = "-" ]; then
        warn "$1 won't import: needs python3-soapysdr (apt) and a venv with --system-site-packages"
    elif [ $CHECK_ONLY = 1 ]; then
        printf '  %-12s MISSING\n' "$1"
    else
        echo "  $1: installing $2 from pip"
        "$VENV/bin/pip" install -q "$2" || warn "pip install $2 failed"
    fi
done
"$PY" -c 'import numba, sys; v = tuple(map(int, numba.__version__.split(".")[:2])); sys.exit(v < (0, 57))' 2>/dev/null \
    || warn "numba older than 0.57: if the modem fails to compile, run: $VENV/bin/pip install -U numba"

# ---------------------------------------------------------------- 4. USB access
say "4. USB access"
BL=/etc/modprobe.d/avm-rtlsdr-blacklist.conf
if ls /lib/modprobe.d/*rtl* /etc/modprobe.d/*rtl* >/dev/null 2>&1 \
   && grep -qs dvb_usb_rtl28xxu /lib/modprobe.d/*rtl* /etc/modprobe.d/*rtl*; then
    echo "RTL-SDR TV driver already blacklisted"
elif [ $CHECK_ONLY = 1 ]; then
    echo "RTL-SDR TV driver (dvb_usb_rtl28xxu) not blacklisted"
else
    printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' | $SUDO tee $BL >/dev/null
    $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null
    echo "blacklisted the RTL-SDR TV driver ($BL)"
fi
for g in plugdev dialout audio video; do
    getent group $g >/dev/null || continue
    if id -nG | grep -qw $g; then continue; fi
    if [ $CHECK_ONLY = 1 ]; then echo "not in group $g"
    else $SUDO usermod -aG $g "$USER" && echo "added $USER to $g (log out and back in)"; fi
done
[ $CHECK_ONLY = 0 ] && $SUDO udevadm control --reload-rules 2>/dev/null && $SUDO udevadm trigger 2>/dev/null

# ---------------------------------------------------------------- 5. launcher
say "5. Desktop launcher"
mkdir -p "$AVM_DIR/gui_logs"
DESK="[Desktop Entry]
Type=Application
Name=AVM
Comment=AVM -- Audio Video Modem, touch GUI (TX and RX, full screen)
Path=$AVM_DIR
Exec=env HF_RX_GUI_LOG=$AVM_DIR/gui_logs/rx_session.log $VENV/bin/python $AVM_DIR/touch_gui.py
Icon=network-transmit-receive
Terminal=false
Categories=Network;HamRadio;"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
# an existing launcher (any name) for touch_gui.py is kept, not duplicated
OLD="$(grep -l 'touch_gui.py' "$HOME"/.local/share/applications/*.desktop "$DESKTOP_DIR"/*.desktop 2>/dev/null \
       | grep -v '/avm.desktop$' | head -n 1)"
if [ -n "$OLD" ]; then
    echo "existing launcher kept: $OLD"
elif [ $CHECK_ONLY = 1 ]; then
    [ -f "$HOME/.local/share/applications/avm.desktop" ] && echo "present" || echo "missing"
else
    mkdir -p "$HOME/.local/share/applications"
    echo "$DESK" > "$HOME/.local/share/applications/avm.desktop"
    if [ -d "$DESKTOP_DIR" ]; then
        echo "$DESK" > "$DESKTOP_DIR/avm.desktop"
        chmod +x "$DESKTOP_DIR/avm.desktop"
        # GNOME (Ubuntu) only runs desktop icons it's told are trusted
        gio set "$DESKTOP_DIR/avm.desktop" metadata::trusted true 2>/dev/null
    fi
    echo "menu entry + desktop icon 'AVM' -> $AVM_DIR/touch_gui.py"
fi

# ---------------------------------------------------------------- summary
say "Radios Soapy can see now"
timeout 20 SoapySDRUtil --find 2>/dev/null | grep -E 'driver|label' | sed 's/^ */  /' || echo "  (none found)"
say "Done"
echo "Start AVM from the menu / desktop icon, or:  cd $AVM_DIR && $VENV/bin/python touch_gui.py"
echo "(use $VENV/bin/python, not plain python3: some modules, e.g. numba on Ubuntu 24.04, are only in the venv)"
echo "The first TX/RX start compiles the modem and codec (a minute or two); later starts are quick."
echo "If this run added you to any groups, log out and back in first."
