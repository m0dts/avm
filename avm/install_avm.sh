#!/bin/bash
# AVM installer for Ubuntu (22.04+), Debian (12+) and Raspberry Pi OS
# (bookworm/trixie), on x86_64 or ARM64.
#
# On a new machine, just this (downloads AVM from GitHub into ~/avm):
#   wget https://raw.githubusercontent.com/m0dts/avm/main/install_avm.sh
#   bash install_avm.sh
#
#   bash install_avm.sh --update     # also fetch the latest AVM from GitHub
#   bash install_avm.sh --check      # only report what's there / missing
#   bash install_avm.sh --yes        # don't ask before installing
#   bash install_avm.sh --rotate180  # touch screen mounted upside down (--no-rotate: normal)
#   (AVM_INSTALL_DIR=/some/dir to put AVM elsewhere than ~/avm)
#
# It first surveys the machine (the --check report: what's already there,
# what it would install or change) and asks before doing anything.
#
# What it does (each step is skipped if already done; safe to re-run):
#   0. AVM itself: from GitHub, unless this script sits in / beside an AVM
#      folder already (a copied release folder)
#   1. apt packages: Python libs, Qt5, SoapySDR + Lime and RTL-SDR modules,
#      libiio, ffmpeg (with Opus and Codec2), arecord, pw-cat, v4l2-ctl
#   2. SoapyPlutoSDR: from apt if offered, else built from source
#   3. ~/venv (with system site-packages) + pyqtgraph and sounddevice
#   4. udev rules / RTL-SDR TV-driver blacklist, user groups
#   5. desktop launcher (menu + desktop icon) for the touch GUI
# Then it checks every module imports and lists the radios Soapy can see.
# The whole script is one { } block, so bash reads it all before running:
# --update replaces this file while it runs.
{
set -u
AVM_REPO="${AVM_REPO:-m0dts/avm}"
AVM_BRANCH="${AVM_BRANCH:-main}"
CHECK_ONLY=0
UPDATE=0
ASSUME_YES=0
ROTATE=""   # "180" / "0" from --rotate180 / --no-rotate; "" = ask (or keep)
for a in "$@"; do
    case "$a" in
        --check) CHECK_ONLY=1 ;;
        --update) UPDATE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --rotate180) ROTATE=180 ;;
        --no-rotate) ROTATE=0 ;;
        *) echo "unknown option $a (use --check, --update, --yes, --rotate180 or --no-rotate)"; exit 1 ;;
    esac
done
# Where AVM is: beside this script (a copied release folder: ./ or ./avm),
# else ~/avm (or AVM_INSTALL_DIR), fetched from GitHub if not there yet.
SCRIPT_DIR=""
[ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] && SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/touch_gui.py" ]; then
    AVM_DIR="$SCRIPT_DIR"
elif [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/avm/touch_gui.py" ]; then
    AVM_DIR="$SCRIPT_DIR/avm"
else
    AVM_DIR="${AVM_INSTALL_DIR:-$HOME/avm}"
fi
VENV="${AVM_VENV:-$HOME/venv}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*"; exit 1; }

# Look before touching anything: run this script's own --check survey, show
# it, and ask. (Skipped with --yes, or when this script isn't a file, e.g.
# piped into bash -- there's nothing to re-run then.)
if [ $CHECK_ONLY = 0 ] && [ $ASSUME_YES = 0 ] && [ -n "$SCRIPT_DIR" ]; then
    printf '\033[1mAVM installer: checking this machine first (nothing is changed yet)...\033[0m\n'
    bash "${BASH_SOURCE[0]}" --check $([ $UPDATE = 1 ] && echo --update) || exit 1
    printf '\n\033[1mInstall / change what is listed above as missing? [y/N] \033[0m'
    ans=""
    { read -r ans </dev/tty; } 2>/dev/null || true
    case "$ans" in
        y|Y|yes|YES) echo ;;
        *) echo "Nothing changed."; exit 0 ;;
    esac
fi

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
[ "$(id -u)" = 0 ] && die "Run as your normal user, not root (it uses sudo where needed)."
SUDO=sudo
# Debian installed with a root password leaves the user without sudo
if [ $CHECK_ONLY = 0 ] && ! { command -v sudo >/dev/null && sudo -v; }; then
    die "This needs sudo for your user ($USER). As root, run once:
     su -c 'apt-get install -y sudo && usermod -aG sudo $USER'
   then log out and back in, and run this installer again."
fi

# ---------------------------------------------------------------- 0. AVM from GitHub
say "0. AVM program files"
if [ -f "$AVM_DIR/touch_gui.py" ] && [ $UPDATE = 0 ]; then
    echo "present ($AVM_DIR); --update fetches the latest from GitHub"
    # how it compares with GitHub (one line; nothing if offline)
    [ -f "$AVM_DIR/avm_update.py" ] && python3 "$AVM_DIR/avm_update.py" 2>/dev/null | head -n 1
elif [ $CHECK_ONLY = 1 ]; then
    echo "would download github.com/$AVM_REPO ($AVM_BRANCH) into $AVM_DIR"
else
    URL="${AVM_URL:-https://github.com/$AVM_REPO/archive/refs/heads/$AVM_BRANCH.tar.gz}"
    TMP="$(mktemp -d)"
    echo "downloading $URL"
    if command -v wget >/dev/null; then
        wget -q -O "$TMP/avm.tar.gz" "$URL"
    elif command -v curl >/dev/null; then
        curl -fsSL -o "$TMP/avm.tar.gz" "$URL"
    else
        $SUDO apt-get install -y wget </dev/null >/dev/null && wget -q -O "$TMP/avm.tar.gz" "$URL"
    fi || die "download failed: $URL"
    tar -xzf "$TMP/avm.tar.gz" -C "$TMP" || die "couldn't unpack the download"
    SRC="$(find "$TMP" -maxdepth 3 -name touch_gui.py -printf '%h\n' | head -n 1)"
    [ -n "$SRC" ] || die "the download has no touch_gui.py ($URL)"
    # copy over the top: settings live in ~/.config, logs in gui_logs/ are kept
    mkdir -p "$AVM_DIR"
    cp -a "$SRC"/. "$AVM_DIR"/ || die "couldn't copy into $AVM_DIR"
    rm -rf "$TMP"
    echo "installed AVM into $AVM_DIR"
    # Carry on with the installer just downloaded, not this (older) copy --
    # bash has this one's text in memory (see the { } block), so its newer
    # steps would only run next time. Without --update, so this happens once;
    # --yes: the survey was already shown and answered.
    if [ -f "$AVM_DIR/install_avm.sh" ]; then
        echo "continuing with the updated installer..."
        exec bash "$AVM_DIR/install_avm.sh" --yes
    fi
fi
[ -f "$AVM_DIR/touch_gui.py" ] || [ $CHECK_ONLY = 1 ] || die "touch_gui.py not found in $AVM_DIR."

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
    # apt's own output only when it fails: a warning about one repository
    # (e.g. a signing key the Pi doesn't have yet) reads like a failure,
    # though apt carries on with that repository's previous lists.
    APT_LOG="$(mktemp)"
    if ! $SUDO apt-get update >"$APT_LOG" 2>&1; then
        cat "$APT_LOG"; rm -f "$APT_LOG"
        die "apt-get update failed"
    fi
    echo "package lists updated"
    if grep -qiE 'missing key|NO_PUBKEY|signature' "$APT_LOG"; then
        BAD="$(grep -oE 'https?://[^ ]+' "$APT_LOG" | grep -iE 'InRelease|Release' \
               | sed -E 's#/dists/.*##' | sort -u | tr '\n' ' ')"
        BAD="${BAD:-a package source }"
        echo "note: couldn't check the signature of ${BAD% } -- its"
        echo "      signing key isn't on this system (usually the source has a new key"
        echo "      that isn't released yet). Its previous package lists are used; AVM"
        echo "      doesn't need it. Nothing to do: a later system update fixes it."
    fi
    rm -f "$APT_LOG"
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
# Airspy R2/Mini and HF+ (receive only), where the distro has them
AIRSPY_PKG=soapysdr${SOAPY_ABI}-module-airspy
AIRSPYHF_PKG=soapysdr${SOAPY_ABI}-module-airspyhf
have_pkg "$AIRSPY_PKG" && PKGS="$PKGS $AIRSPY_PKG"
have_pkg "$AIRSPYHF_PKG" && PKGS="$PKGS $AIRSPYHF_PKG"

# Radio drivers SoapySDR already has (from any source -- e.g. DragonOS ships
# its own LimeSuite / Soapy builds) are left alone: the distro's packages
# would try to overwrite the same files, and dpkg refuses.
SOAPY_MODS="$(SoapySDRUtil --info 2>/dev/null | grep -i 'module found' | tr 'A-Z' 'a-z')"
KEPT=""
drop() { for x in "$@"; do PKGS="$(printf '%s
' $PKGS | grep -vx "$x" | tr '
' ' ')"; done; }
if echo "$SOAPY_MODS" | grep -q lms7support; then
    drop "soapysdr${SOAPY_ABI}-module-lms7" limesuite-udev
    KEPT="${KEPT}LimeSDR driver already installed (from this system) -- keeping it\n"
fi
if echo "$SOAPY_MODS" | grep -q rtlsdrsupport; then
    drop "soapysdr${SOAPY_ABI}-module-rtlsdr"
    KEPT="${KEPT}RTL-SDR driver already installed (from this system) -- keeping it\n"
fi
command -v rtl_test >/dev/null && drop rtl-sdr
echo "$SOAPY_MODS" | grep -q "airspysupport" && drop "$AIRSPY_PKG"
echo "$SOAPY_MODS" | grep -q "airspyhfsupport" && drop "$AIRSPYHF_PKG"
if echo "$SOAPY_MODS" | grep -q plutosdrsupport; then
    drop "$PLUTO_PKG"
    KEPT="${KEPT}PlutoSDR driver already installed (from this system) -- keeping it\n"
fi

MISSING=""
PRESENT=0
for p in $PKGS; do
    if ! have_pkg "$p"; then
        # these come from pip in step 3 when apt lacks them: nothing to report
        case "$p" in
            python3-numba|python3-pyqtgraph|python3-sounddevice|python3-opencv|python3-av) ;;
            *) warn "package $p not offered by apt on this system -- skipped" ;;
        esac
    elif ! installed "$p"; then
        MISSING="$MISSING $p"
    else
        PRESENT=$((PRESENT + 1))
    fi
done
say "1. apt packages"
[ -n "$KEPT" ] && printf '%b' "$KEPT"
echo "already installed: $PRESENT package(s)"
if [ -z "$MISSING" ]; then
    echo "nothing to install"
elif [ $CHECK_ONLY = 1 ]; then
    echo "to install:$MISSING"
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
# Keep numpy on the system's major version for every pip install here: apt's
# own modules (OpenCV, PyAV...) are built for it, and a numpy 1 <-> 2 switch
# breaks them ("_ARRAY_API not found"). pip would otherwise pull numpy 2 in
# with an upgraded numba on numpy-1 systems (Ubuntu 22.04, DragonOS).
SYS_NP="$(/usr/bin/python3 -c 'import numpy; print(numpy.__version__.split(".")[0])' 2>/dev/null)"
NP_PIN="$(mktemp)"
case "$SYS_NP" in
    1) echo "numpy<2" > "$NP_PIN" ;;
    2) echo "numpy>=2" > "$NP_PIN" ;;
esac
export PIP_CONSTRAINT="$NP_PIN"
if [ $CHECK_ONLY = 0 ]; then
    [ -x "$VENV/bin/python" ] || python3 -m venv --system-site-packages "$VENV" || die "venv creation failed"
    "$VENV/bin/pip" install -q --upgrade pyqtgraph sounddevice || die "pip install failed"
fi
# repair a venv where an earlier run let pip switch numpy's major version
VENV_NP="$("$VENV/bin/python" -c 'import numpy; print(numpy.__version__.split(".")[0])' 2>/dev/null)"
if [ -n "$SYS_NP" ] && [ -n "$VENV_NP" ] && [ "$VENV_NP" != "$SYS_NP" ]; then
    if [ $CHECK_ONLY = 1 ]; then
        echo "  numpy $VENV_NP.x in the venv but the system's is $SYS_NP.x (would be fixed)"
    else
        echo "  numpy: venv has $VENV_NP.x, system modules need $SYS_NP.x -- fixing"
        "$VENV/bin/pip" install -q "$(cat "$NP_PIN")" || warn "numpy fix failed"
    fi
fi
PY="$VENV/bin/python"
[ -x "$PY" ] || PY=python3
# Anything apt couldn't supply (or supplied too old) comes from pip instead.
for spec in "numpy numpy" "scipy scipy" "numba numba" "cv2 opencv-python-headless" "av av" \
            "PyQt5 PyQt5" "pyqtgraph pyqtgraph" "sounddevice sounddevice" "SoapySDR -"; do
    set -- $spec
    if "$PY" -c "import $1" 2>/dev/null; then
        :  # importable: its version is reported below
    elif [ "$2" = "-" ]; then
        warn "$1 won't import: needs python3-soapysdr (apt) and a venv with --system-site-packages"
    elif [ $CHECK_ONLY = 1 ]; then
        printf '  %-12s MISSING\n' "$1"
    else
        echo "  $1: installing $2 from pip"
        "$VENV/bin/pip" install -q "$2" || warn "pip install $2 failed"
    fi
done
# Debian 12's apt numba (0.56) predates Python 3.11 support: use pip's instead
if ! "$PY" -c 'import numba, sys; v = tuple(map(int, numba.__version__.split(".")[:2])); sys.exit(v < (0, 57))' 2>/dev/null; then
    if [ $CHECK_ONLY = 1 ]; then
        echo "  numba older than 0.57 (would be upgraded from pip)"
    else
        echo "  numba: upgrading from pip (apt's is too old)"
        "$VENV/bin/pip" install -q "numba>=0.59" || warn "pip install numba failed"
    fi
fi
# Versions, not just presence: what's actually there vs what AVM needs.
echo
echo "  versions (as AVM will use them):"
SYS_NP="$SYS_NP" "$PY" - <<'PYVER'
import importlib, os, sys
def ver(mod):
    try:
        m = importlib.import_module(mod)
    except Exception as e:
        return None, f"won't import: {str(e).splitlines()[0][:60]}"
    if mod == "PyQt5":
        from PyQt5 import QtCore
        return QtCore.PYQT_VERSION_STR, None
    if mod == "SoapySDR":
        return m.getAPIVersion(), None
    return getattr(m, "__version__", "?"), None
def tup(v):
    out = []
    for p in str(v).split("."):
        d = "".join(c for c in p if c.isdigit())
        out.append(int(d) if d else 0)
    return tuple(out)
sys_np = os.environ.get("SYS_NP", "")
# (module, label, requirement text, check(version) -> bool)
checks = [
    ("numpy", "numpy", f"{sys_np}.x, as the system's" if sys_np else "any",
     lambda v: not sys_np or str(v).split(".")[0] == sys_np),
    ("numba", "numba", ">= 0.57", lambda v: tup(v) >= (0, 57)),
    ("scipy", "scipy", "any", None),
    ("cv2", "OpenCV", "any", None),
    ("av", "PyAV", "any", None),
    ("PyQt5", "PyQt5", "5.x", lambda v: str(v).startswith("5.")),
    ("pyqtgraph", "pyqtgraph", "any", None),
    ("sounddevice", "sounddevice", "any", None),
    ("SoapySDR", "SoapySDR", "API 0.8", lambda v: str(v).startswith("0.8")),
]
bad = 0
pyv = ".".join(map(str, sys.version_info[:3]))
pyok = sys.version_info >= (3, 9)
bad += not pyok
print(f"    {'Python':<12} {pyv:<12} needs >= 3.9{'':<12} {'ok' if pyok else 'TOO OLD'}")
for mod, label, need, check in checks:
    v, err = ver(mod)
    if err:
        print(f"    {label:<12} {'-':<12} {err}")
        bad += 1
        continue
    good = check is None or check(v)
    bad += not good
    print(f"    {label:<12} {str(v):<12} needs {need:<18} {'ok' if good else 'WRONG VERSION'}")
sys.exit(1 if bad else 0)
PYVER
[ $? = 0 ] || warn "something above isn't what AVM needs -- re-run the installer to fix it, or fix it by hand"
# ffmpeg: its version and the audio encoders AVM uses
if command -v ffmpeg >/dev/null; then
    FFV="$(ffmpeg -hide_banner -version 2>/dev/null | head -n1 | awk '{print $3}')"
    FFE="$(ffmpeg -hide_banner -encoders 2>/dev/null)"
    printf '    %-12s %-12s %s\n' ffmpeg "$FFV" \
        "Opus: $(echo "$FFE" | grep -q libopus && echo yes || echo NO)  Codec2: $(echo "$FFE" | grep -q libcodec2 && echo yes || echo 'no (Opus only)')"
    echo "$FFE" | grep -q libopus || warn "ffmpeg has no Opus encoder: AVM's audio won't work"
    # Debian / Ubuntu / Raspberry Pi OS ffmpeg includes Codec2; a custom or
    # third-party build (some SDR distributions) may not
    echo "$FFE" | grep -q libcodec2 || warn "this ffmpeg ($(command -v ffmpeg)) has no Codec2: AVM offers Opus only.
   For Codec2, use the distribution's ffmpeg: sudo apt install --reinstall ffmpeg libcodec2-dev"
else
    printf '    %-12s %s\n' ffmpeg "not installed yet"
fi

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

# Pluto's USB network link (it's ip:192.168.2.1 on it). A desktop's network
# manager brings it up by DHCP; a minimal install has nothing that does, so
# a udev rule gives it a fixed address whenever a Pluto is plugged in.
# (AVM also falls back to opening the Pluto over plain USB without it.)
PLUTO_RULE=/etc/udev/rules.d/90-avm-pluto-net.rules
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    echo "Pluto network: handled by NetworkManager"
elif [ -f "$PLUTO_RULE" ]; then
    echo "Pluto network: udev rule present"
elif [ $CHECK_ONLY = 1 ]; then
    echo "Pluto network: no network manager -- a udev rule would be added"
else
    printf '%s\n' '# AVM: bring up an ADALM-Pluto'"'"'s USB network link (the Pluto is 192.168.2.1)' \
        'ACTION=="add", SUBSYSTEM=="net", ATTRS{idVendor}=="0456", ATTRS{idProduct}=="b673", RUN+="/bin/sh -c '"'"'ip addr add 192.168.2.10/24 dev %k; ip link set %k up'"'"'"' \
        | $SUDO tee "$PLUTO_RULE" >/dev/null
    echo "Pluto network: udev rule added ($PLUTO_RULE)"
fi
[ $CHECK_ONLY = 0 ] && $SUDO udevadm control --reload-rules 2>/dev/null && $SUDO udevadm trigger 2>/dev/null
# a Pluto already plugged in: bring its link up now too
if [ $CHECK_ONLY = 0 ] && [ -f "$PLUTO_RULE" ]; then
    $SUDO udevadm trigger --action=add --subsystem-match=net 2>/dev/null
fi

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
# an 'avm' command too (in ~/.local/bin, on PATH from the next login)
if [ $CHECK_ONLY = 0 ]; then
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/avm" <<AVMCMD
#!/bin/sh
# AVM touch GUI (written by install_avm.sh)
cd "$AVM_DIR" || exit 1
export HF_RX_GUI_LOG="$AVM_DIR/gui_logs/rx_session.log"
exec "$VENV/bin/python" touch_gui.py "\$@"
AVMCMD
    chmod +x "$HOME/.local/bin/avm"
    echo "'avm' command: $HOME/.local/bin/avm"
fi

# ---------------------------------------------------------------- 6. screen
say "6. Screen orientation"
# For a touch screen mounted the other way up: AVM turns the picture and the
# touch input 180 degrees while it runs (screen_rotate.py), back when it quits.
ROT_FILE="$AVM_DIR/screen_rotation"
ROT_NOW="$(cat "$ROT_FILE" 2>/dev/null)"
[ "$ROT_NOW" = 180 ] || ROT_NOW=0
if [ $CHECK_ONLY = 1 ]; then
    [ "$ROT_NOW" = 180 ] && echo "upside down (rotated 180)" || echo "normal (not rotated)"
else
    if [ -z "$ROTATE" ] && [ $ASSUME_YES = 0 ]; then
        def="n"; [ "$ROT_NOW" = 180 ] && def="y"
        printf 'Turn the screen upside down (rotate 180) while AVM runs? [%s] '             "$([ $def = y ] && echo Y/n || echo y/N)"
        ans=""
        { read -r ans </dev/tty; } 2>/dev/null || true
        [ -z "$ans" ] && ans=$def
        case "$ans" in y|Y|yes|YES) ROTATE=180 ;; *) ROTATE=0 ;; esac
    fi
    [ -z "$ROTATE" ] && ROTATE=$ROT_NOW   # --yes without a choice: keep as it is
    echo "$ROTATE" > "$ROT_FILE"
    if [ "$ROTATE" = 180 ]; then
        echo "AVM will turn the screen upside down while it runs"
        # the tools it uses: xrandr + xinput (X11), wlr-randr (Wayland)
        NEED=""
        for p in x11-xserver-utils xinput wlr-randr; do
            have_pkg "$p" && ! installed "$p" && NEED="$NEED $p"
        done
        [ -n "$NEED" ] && { $SUDO apt-get install -y $NEED </dev/null || warn "couldn't install:$NEED"; }
    else
        echo "screen left as it is"
    fi
fi

# ---------------------------------------------------------------- summary
say "Radios Soapy can see now"
timeout 20 SoapySDRUtil --find 2>/dev/null | grep -E 'driver|label' | sed 's/^ */  /' || echo "  (none found)"
say "Done"
echo "Start AVM from the menu / desktop icon, or:  cd $AVM_DIR && $VENV/bin/python touch_gui.py"
echo "(use $VENV/bin/python, not plain python3: some modules, e.g. numba on Ubuntu 24.04, are only in the venv)"
echo "The first TX/RX start compiles the modem and codec (a minute or two); later starts are quick."
echo "If this run added you to any groups, log out and back in first."
exit 0
}
