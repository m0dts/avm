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
#   (AVM_INSTALL_DIR=/some/dir to put AVM elsewhere than ~/avm)
#
# It first surveys the machine (the --check report: what's already there,
# what it would install or change) and asks before doing anything.
#
# What it does (each step is skipped if already done; safe to re-run):
#   0. AVM itself: from GitHub, unless this script sits in / beside an AVM
#      folder already (a copied release folder)
#   1. apt packages: the compiler and CMake, Qt 6, the FFmpeg, Opus and Codec2
#      libraries, SoapySDR + Lime and RTL-SDR modules, libiio, pw-cat, v4l2-ctl
#   2. SoapyPlutoSDR: from apt if offered, else built from source
#   3. AVM built from its source (C++: the GUI and the modem library;
#      ~10 minutes on a Pi 4 the first time, quick after that)
#   4. udev rules / RTL-SDR TV-driver blacklist, user groups
#   5. desktop launcher (menu + desktop icon) and an 'avm' command
# Then it lists the radios Soapy can see. AVM needs no Python.
# The whole script is one { } block, so bash reads it all before running:
# --update replaces this file while it runs.
{
set -u
AVM_REPO="${AVM_REPO:-m0dts/avm}"
AVM_BRANCH="${AVM_BRANCH:-}"     # (default below: the remembered test branch, else main)
CHECK_ONLY=0
UPDATE=0
ASSUME_YES=0
for a in "$@"; do
    case "$a" in
        --check) CHECK_ONLY=1 ;;
        --update) UPDATE=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --rotate180|--no-rotate) ;;  # screen rotation was removed (1.0.9): set the display up yourself
        *) echo "unknown option $a (use --check, --update or --yes)"; exit 1 ;;
    esac
done
# Where AVM is: beside this script (a copied release folder: ./ or ./avm),
# else ~/avm (or AVM_INSTALL_DIR), fetched from GitHub if not there yet.
SCRIPT_DIR=""
[ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ] && SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# (an AVM folder: the one with gui/CMakeLists.txt, the C++ source)
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/gui/CMakeLists.txt" ]; then
    AVM_DIR="$SCRIPT_DIR"
elif [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/avm/gui/CMakeLists.txt" ]; then
    AVM_DIR="$SCRIPT_DIR/avm"
else
    AVM_DIR="${AVM_INSTALL_DIR:-$HOME/avm}"
fi
BUILD_DIR="$AVM_DIR/gui/build"
AVM_GUI="$BUILD_DIR/avm_gui"
# no AVM_BRANCH given: the one this AVM was installed from (a test branch), else main
[ -z "$AVM_BRANCH" ] && [ -f "$AVM_DIR/BRANCH" ] && AVM_BRANCH="$(head -n 1 "$AVM_DIR/BRANCH")"
AVM_BRANCH="${AVM_BRANCH:-main}"

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[31mXX %s\033[0m\n' "$*"; [ -n "${LOG:-}" ] && echo "Install log: $LOG"; exit 1; }
# What an install would add, collected by --check for its summary:
# plan "what" "where" "approx MB"
PLAN=""
PLAN_MB=0
plan() { PLAN="${PLAN}$(printf '  %-44s %-30s %s' "$1" "$2" "${3:+~$3 MB}")\n"; PLAN_MB=$((PLAN_MB + ${3:-0})); }

# Everything an install prints also goes to a log file (colours stripped),
# kept after the install; a re-run of the updated installer appends to it
# (the updated installer, started by this one, inherits the same log and the
# same output, already going to it)
if [ $CHECK_ONLY = 0 ]; then
    if [ -n "${AVM_INSTALL_LOG:-}" ]; then
        LOG="$AVM_INSTALL_LOG"
    else
        LOG="$HOME/avm_install.log"
        export AVM_INSTALL_LOG="$LOG"
        echo "AVM install $(date '+%Y-%m-%d %H:%M:%S')" > "$LOG"
        exec > >(tee >(sed -u 's/\x1b\[[0-9;]*m//g' >> "$LOG")) 2>&1
    fi
fi

# Look before touching anything: run this script's own --check survey, show
# it, and ask. (Skipped with --yes, or when this script isn't a file, e.g.
# piped into bash -- there's nothing to re-run then.)
if [ $CHECK_ONLY = 0 ] && [ $ASSUME_YES = 0 ] && [ -n "$SCRIPT_DIR" ]; then
    printf '\033[1mAVM installer: checking this machine first (nothing is changed yet)...\033[0m\n'
    bash "${BASH_SOURCE[0]}" --check $([ $UPDATE = 1 ] && echo --update) || exit 1
    printf '\n\033[1mInstall / change what is listed above? [y/N] \033[0m'
    ans=""
    { read -r ans </dev/tty; } 2>/dev/null || true
    case "$ans" in
        y|Y|yes|YES) echo "Installing (log: $LOG)" ;;
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
   Elsewhere, install by hand: a C++17 compiler, CMake, Qt 6 (or 5) development files,
   the FFmpeg, Opus and Codec2 libraries (development files), SoapySDR with the
   PlutoSDR / LMS7 / RTL-SDR modules, pw-cat and v4l2-ctl; then build AVM:
   cmake -S gui -B gui/build -DCMAKE_BUILD_TYPE=Release && cmake --build gui/build" ;;
esac
IS_PI=0
grep -qi 'raspberry pi' /proc/device-tree/model 2>/dev/null && IS_PI=1
echo "System: $PRETTY_NAME, $(uname -m)$([ $IS_PI = 1 ] && echo ', Raspberry Pi')"
echo "AVM:    $AVM_DIR"
[ "$(id -u)" = 0 ] && die "Run as your normal user, not root (it uses sudo where needed)."
SUDO=sudo
# Debian installed with a root password leaves the user without sudo
# (sudo -n true first: "sudo -v" asks for a password whenever ANY rule for the
# user wants one -- e.g. the sudo group's -- even with a NOPASSWD: ALL rule too,
# and from AVM's Update button there's no terminal to type it in)
if [ $CHECK_ONLY = 0 ] && ! { command -v sudo >/dev/null && { sudo -n true 2>/dev/null || sudo -v; }; }; then
    die "This needs sudo for your user ($USER). As root, run once:
     su -c 'apt-get install -y sudo && usermod -aG sudo $USER'
   then log out and back in, and run this installer again."
fi

# ---------------------------------------------------------------- 0. AVM from GitHub
say "0. AVM program files"
if [ -f "$AVM_DIR/gui/CMakeLists.txt" ] && [ $UPDATE = 0 ]; then
    echo "present ($AVM_DIR, version $(cat "$AVM_DIR/VERSION" 2>/dev/null || echo '?')); --update fetches the latest from GitHub"
elif [ $CHECK_ONLY = 1 ]; then
    echo "would download github.com/$AVM_REPO ($AVM_BRANCH) into $AVM_DIR"
    plan "AVM program files (from GitHub)" "$AVM_DIR" 3
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
    # (the folder holding gui/CMakeLists.txt)
    SRC="$(find "$TMP" -maxdepth 4 -path '*/gui/CMakeLists.txt' -printf '%h\n' | head -n 1)"
    SRC="${SRC%/gui}"
    [ -n "$SRC" ] || die "the download has no AVM source (gui/CMakeLists.txt) ($URL)"
    # copy over the top: settings live in ~/.config, logs in gui_logs/ are kept.
    # Only the files that changed: the rest keep their dates, so the build
    # (step 3) recompiles just what's new -- a download dates every file now,
    # and copying them all rebuilt everything on every update
    mkdir -p "$AVM_DIR"
    (cd "$SRC" && find . -type f -print0) | while IFS= read -r -d '' f; do
        if ! cmp -s "$SRC/$f" "$AVM_DIR/$f"; then
            mkdir -p "$AVM_DIR/$(dirname "$f")" && cp -p "$SRC/$f" "$AVM_DIR/$f" || exit 1
        fi
    done || die "couldn't copy into $AVM_DIR"
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
[ -f "$AVM_DIR/gui/CMakeLists.txt" ] || [ $CHECK_ONLY = 1 ] || die "AVM source (gui/CMakeLists.txt) not found in $AVM_DIR."
# a test branch (AVM_BRANCH=...): remembered, so AVM's Update button follows it too
if [ $CHECK_ONLY = 0 ]; then
    if [ "$AVM_BRANCH" != main ]; then echo "$AVM_BRANCH" > "$AVM_DIR/BRANCH"; else rm -f "$AVM_DIR/BRANCH"; fi
fi

# 64-bit only (what AVM is built and tested on)
if [ "$(getconf LONG_BIT)" != 64 ]; then
    die "This is a 32-bit system ($(uname -m)). AVM needs 64-bit (x86_64 or ARM64).
   In a VM: create it as 'Ubuntu (64-bit)' from a 64-bit ISO. If the VM software
   only offers 32-bit, enable virtualisation (VT-x / AMD-V) in the PC's BIOS, and
   on Windows check Hyper-V / Memory Integrity isn't blocking it.
   On a Raspberry Pi: use the 64-bit Raspberry Pi OS."
fi

# Ubuntu: some of what AVM needs (SoapySDR, Codec2...) is in 'universe';
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
    echo "checking for package updates (apt-get update, up to a minute)..."
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

# offered = has an installable candidate (a package can be listed with none)
have_pkg() {
    local c
    c="$(apt-cache policy "$1" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    [ -n "$c" ] && [ "$c" != "(none)" ]
}
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

echo "checking which packages are installed and offered (a minute or two on a Pi)..."
# SoapySDR's module ABI (0.8 on current releases, 0.7 on Ubuntu 22.04)
SOAPY_ABI=0.8
have_pkg soapysdr0.8-module-lms7 || { have_pkg soapysdr0.7-module-lms7 && SOAPY_ABI=0.7; }

# ---------------------------------------------------------------- 1. apt
# What AVM is built with (step 3) and uses at run time: the compiler and
# CMake, Qt, the FFmpeg libraries (camera, microphone, recordings), Opus and
# Codec2, SoapySDR (loaded at run time) with the radio modules. The tools to
# build a radio driver from source are added below, only when one is needed.
PKGS="g++ cmake pkg-config ccache qt6-base-dev
      libavdevice-dev libavformat-dev libavcodec-dev libavfilter-dev libswscale-dev libavutil-dev
      libopus-dev libcodec2-dev
      libsoapysdr${SOAPY_ABI} soapysdr-tools
      soapysdr${SOAPY_ABI}-module-lms7 soapysdr${SOAPY_ABI}-module-rtlsdr rtl-sdr
      libiio0 libiio-utils
      pipewire-bin v4l-utils"
BUILD_PKGS="git cmake g++ pkg-config libsoapysdr-dev libiio-dev libad9361-dev"
# Qt 6 where offered (all supported releases); Qt 5 otherwise -- AVM builds with either
have_pkg qt6-base-dev || PKGS="${PKGS/qt6-base-dev/qtbase5-dev}"
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
# HackRF (receive and transmit; its udev rules come with libhackrf)
HACKRF_PKG=soapysdr${SOAPY_ABI}-module-hackrf
have_pkg "$HACKRF_PKG" && PKGS="$PKGS $HACKRF_PKG"

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
echo "$SOAPY_MODS" | grep -q "hackrfsupport" && drop "$HACKRF_PKG"
if echo "$SOAPY_MODS" | grep -q plutosdrsupport; then
    drop "$PLUTO_PKG"
    KEPT="${KEPT}PlutoSDR driver already installed (from this system) -- keeping it\n"
fi

# A driver built from source (step 2: PlutoSDR when apt has no module for
# it; SDRplay when its API is installed): then the build tools too
NEED_BUILD=0
if ! echo "$SOAPY_MODS" | grep -q plutosdrsupport && ! have_pkg "$PLUTO_PKG"; then NEED_BUILD=1; fi
if [ -f /usr/local/include/sdrplay_api.h ] && ! echo "$SOAPY_MODS" | grep -q sdrplaysupport; then NEED_BUILD=1; fi
[ $NEED_BUILD = 1 ] && PKGS="$PKGS $BUILD_PKGS"

MISSING=""
PRESENT=0
for p in $PKGS; do
    if ! have_pkg "$p"; then
        warn "package $p not offered by apt on this system -- skipped"
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
    # with what apt would pull in alongside them, and their installed size
    ALL="$(apt-get -s install $MISSING 2>/dev/null | awk '/^Inst /{print $2}')"
    KB="$(apt-cache show --no-all-versions $ALL 2>/dev/null | awk '/^Installed-Size:/{s+=$2} END{print s+0}')"
    NDEP=$(( $(echo $ALL | wc -w) - $(echo $MISSING | wc -w) ))
    [ $NDEP -lt 0 ] && NDEP=0
    echo "  (+ $NDEP package(s) they need; about $((KB / 1024)) MB once installed)"
    for p in $MISSING; do plan "apt: $p" "system (/usr)"; done
    [ $NDEP -gt 0 ] && plan "apt: $NDEP other package(s) these need" "system (/usr)"
    plan "  apt packages in total" "" $((KB / 1024))
else
    # Progress: apt's own bar needs a terminal (this output also goes to the
    # log), so count its download and set-up lines against the total instead
    TOTAL="$(apt-get -s install $MISSING 2>/dev/null | grep -c '^Inst ')"
    echo "installing $TOTAL package(s) with what they need -- this can take 5-15 minutes on a Pi"
    set -o pipefail
    # (apt's full output to the log as well; the screen gets the progress)
    $SUDO apt-get install -y $MISSING 2>&1 | tee -a "${LOG:-/dev/null}" | awk -v n="$TOTAL" '
        /^Get:/        { g++; if (g == 1) print "  downloading..."
                         if (n && (g % 10 == 0 || g == n)) printf "  downloaded %d/%d\n", g, n; fflush(); next }
        /^Setting up / { s++; printf "  [%3d/%d] set up %s\n", s, n, $3; fflush(); next }
        /^(E:|W:|dpkg: |Errors|Need to get|After this operation)/ { print "  " $0; fflush() }
    ' || die "apt install failed (the log has the details)"
    set +o pipefail
fi

# ---------------------------------------------------------------- 2. SoapyPlutoSDR
say "2. SoapyPlutoSDR"
pluto_ok() { SoapySDRUtil --info 2>/dev/null | grep -qi 'PlutoSDRSupport'; }
if pluto_ok; then
    echo "present"
elif [ $CHECK_ONLY = 1 ]; then
    echo "missing (will be built from source)"
    plan "PlutoSDR driver (SoapyPlutoSDR) source" "$HOME/src/SoapyPlutoSDR" 5
    plan "PlutoSDR driver, built" "/usr/local/lib" 1
else
    SRC="$HOME/src/SoapyPlutoSDR"
    mkdir -p "$HOME/src"
    [ -d "$SRC" ] || git clone --depth 1 https://github.com/pothosware/SoapyPlutoSDR.git "$SRC" || die "git clone failed"
    cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release >/dev/null \
        && make -C "$SRC/build" -j"$(nproc)" \
        && $SUDO make -C "$SRC/build" install && $SUDO ldconfig || die "SoapyPlutoSDR build failed"
    pluto_ok && echo "built and installed" || warn "built, but SoapySDRUtil still doesn't list it"
fi

# SDRplay RSPs (receive only): SoapySDRPlay3 sits on SDRplay's own API
# service, which isn't in any distro (sdrplay.com, under SDRplay's licence).
# With that installed, the Soapy module is built here; without it, a note.
#
# SDRplay's API installer, if the user downloaded it (SDRplay_RSP_API-Linux-
# 3.15.2.run) to a USB drive, ~/Downloads or the AVM folder: offered when the
# API isn't installed, or that file is newer than the installed one. Never
# with --yes (its licence needs a person to accept).
sdrplay_api_ver() {  # installed API version, e.g. 3.15 ("" if none)
    ls /usr/local/lib/libsdrplay_api.so.* 2>/dev/null | sed -E 's/.*\.so\.//' | sort -V | tail -1
}
SDRPLAY_RUN="$(ls -1 /media/"$USER"/*/SDRplay_RSP_API-Linux-*.run /media/*/SDRplay_RSP_API-Linux-*.run \
    "$HOME"/Downloads/SDRplay_RSP_API-Linux-*.run "$AVM_DIR"/SDRplay_RSP_API-Linux-*.run 2>/dev/null \
    | awk -F/ '{print $NF "\t" $0}' | sort -V | tail -1 | cut -f2)"
if [ -n "$SDRPLAY_RUN" ]; then
    RUN_VER="$(basename "$SDRPLAY_RUN" | sed -E 's/.*Linux-([0-9.]+)\.run/\1/')"
    HAVE_VER="$(sdrplay_api_ver)"
    # newer = its major.minor sorts above the installed one (the .so is major.minor)
    RUN_MM="$(echo "$RUN_VER" | cut -d. -f1-2)"
    if [ -z "$HAVE_VER" ] || [ "$(printf '%s\n%s\n' "$HAVE_VER" "$RUN_MM" | sort -V | tail -1)" != "$HAVE_VER" ]; then
        what="install"; [ -n "$HAVE_VER" ] && what="upgrade (installed: $HAVE_VER)"
        if [ $CHECK_ONLY = 1 ]; then
            echo "SDRplay API $RUN_VER found ($SDRPLAY_RUN): will offer to $what"
        elif [ $ASSUME_YES = 1 ]; then
            echo "SDRplay API $RUN_VER found ($SDRPLAY_RUN) -- to $what it, run this"
            echo "  installer without --yes (its licence needs you to accept it)"
        else
            printf '\033[1mSDRplay API %s found (%s): %s it? [y/N] \033[0m' "$RUN_VER" "$SDRPLAY_RUN" "$what"
            ans=""
            { read -r ans </dev/tty; } 2>/dev/null || true
            case "$ans" in
                y|Y|yes|YES)
                    # its own installer: shows the licence, asks, uses sudo itself
                    TMP_RUN="$(mktemp -d)"
                    cp "$SDRPLAY_RUN" "$TMP_RUN/" && chmod +x "$TMP_RUN/$(basename "$SDRPLAY_RUN")"
                    ( cd "$TMP_RUN" && "./$(basename "$SDRPLAY_RUN")" </dev/tty ) \
                        || warn "SDRplay API installer didn't finish"
                    rm -rf "$TMP_RUN"
                    # a newer API needs the Soapy module rebuilt against it
                    [ -n "$HAVE_VER" ] && rm -rf "$HOME/src/SoapySDRPlay3/build" \
                        && SDRPLAY_REBUILD=1
                    ;;
                *) echo "SDRplay API left as it is" ;;
            esac
        fi
    fi
fi

sdrplay_ok() { SoapySDRUtil --info 2>/dev/null | grep -qi 'sdrPlaySupport'; }
if sdrplay_ok && [ -z "${SDRPLAY_REBUILD:-}" ]; then
    echo "SDRplay driver: present"
elif [ ! -f /usr/local/include/sdrplay_api.h ]; then
    echo "SDRplay driver: not set up (only needed for an SDRplay RSP: install SDRplay's"
    echo "  API for Linux from sdrplay.com, then run this installer again)"
elif [ $CHECK_ONLY = 1 ]; then
    echo "SDRplay driver: missing (will be built from source)"
    plan "SDRplay driver (SoapySDRPlay3) source" "$HOME/src/SoapySDRPlay3" 5
    plan "SDRplay driver, built" "/usr/local/lib" 1
else
    SRC="$HOME/src/SoapySDRPlay3"
    # the build tools (step 1 only added them if a build was known to be needed then)
    [ $NEED_BUILD = 1 ] || $SUDO apt-get install -y $BUILD_PKGS >/dev/null || warn "couldn't install the build tools"
    mkdir -p "$HOME/src"
    [ -d "$SRC" ] || git clone --depth 1 https://github.com/pothosware/SoapySDRPlay3.git "$SRC" \
        || warn "git clone SoapySDRPlay3 failed"
    if [ -d "$SRC" ]; then
        cmake -S "$SRC" -B "$SRC/build" -DCMAKE_BUILD_TYPE=Release >/dev/null \
            && make -C "$SRC/build" -j"$(nproc)" >/dev/null \
            && $SUDO make -C "$SRC/build" install >/dev/null && $SUDO ldconfig \
            && sdrplay_ok && echo "SDRplay driver: built and installed" \
            || warn "SoapySDRPlay3 build failed (SDRplay RSPs won't be found)"
    fi
fi

# ---------------------------------------------------------------- 3. build AVM
say "3. AVM build"
# The GUI and the modem library (c++/libavm.so) from the source, tuned for
# this machine's CPU. The first build takes ~10 minutes on a Pi 4; after an
# update only what changed is rebuilt. Always run (it's quick when nothing
# changed), so an update's new source is built before AVM next starts.
if [ $CHECK_ONLY = 1 ]; then
    if [ -x "$AVM_GUI" ]; then echo "built ($AVM_GUI)"; else echo "not built yet"; fi
    plan "AVM, built from source (~10 min on a Pi 4)" "$BUILD_DIR" 40
else
    # a Pi with little memory: fewer compilers at once (each can take ~0.5 GB)
    JOBS="$(nproc)"
    MEM_MB="$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)"
    [ "$MEM_MB" -lt 3000 ] && [ "$JOBS" -gt 2 ] && JOBS=2
    [ "$MEM_MB" -lt 1500 ] && JOBS=1     # a 1 GB Pi: one at a time (the big files need most of it)
    echo "building AVM with $JOBS job(s) -- only what changed; the first build takes ~5-10 minutes on a Pi 4"
    BUILD_LOG="$(mktemp)"
    cmake -S "$AVM_DIR/gui" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release >"$BUILD_LOG" 2>&1
    # the build in the background; here, each file as it starts ("[ 38%] compiling
    # audio.cpp", "linking avm_gui") and, while one takes long, "still working
    # (1 min 20 s)" every 15 s -- a big file compiles for a minute or more
    ( nice cmake --build "$BUILD_DIR" -j"$JOBS" >>"$BUILD_LOG" 2>&1 ) &
    BUILD_PID=$!
    T0=$(date +%s); LAST=$T0; SHOWN=0
    show_new() {
        local n
        n=$(grep -c -E '^\[ *[0-9]+%\] (Building|Linking)' "$BUILD_LOG")
        if [ "$n" -gt "$SHOWN" ]; then
            grep -E '^\[ *[0-9]+%\] (Building|Linking)' "$BUILD_LOG" | tail -n +$((SHOWN + 1)) \
                | sed -E 's#^(\[ *[0-9]+%\]) Building C(XX)? object .*/([^/]+)\.o$#  \1 compiling \3#;
                          s#^(\[ *[0-9]+%\]) Linking C(XX)? (shared library|executable) (.*/)?([^/ ]+)$#  \1 linking \5#'
            SHOWN=$n; LAST=$(date +%s)
        fi
    }
    while kill -0 $BUILD_PID 2>/dev/null; do
        sleep 2
        show_new
        NOW=$(date +%s)
        if [ $((NOW - LAST)) -ge 15 ]; then
            echo "      ... still working ($(( (NOW - T0) / 60 )) min $(( (NOW - T0) % 60 )) s)"
            LAST=$NOW
        fi
    done
    wait $BUILD_PID
    show_new
    echo "  build time: $(( ($(date +%s) - T0) / 60 )) min $(( ($(date +%s) - T0) % 60 )) s"
    cat "$BUILD_LOG" >> "${LOG:-/dev/null}"
    if [ -x "$AVM_GUI" ] && { [ -f "$AVM_DIR/c++/libavm.so" ] || [ -f "$AVM_DIR/c++/avm.dll" ]; } \
        && ! grep -q -E '^(CMake Error|.*error:)' "$BUILD_LOG"; then
        echo "built: $AVM_GUI"
    else
        grep -E 'error|Error' "$BUILD_LOG" | head -n 20
        rm -f "$BUILD_LOG"
        die "the AVM build failed (the log has the details)"
    fi
    rm -f "$BUILD_LOG"
fi

# ---------------------------------------------------------------- 4. USB access
say "4. USB access"
BL=/etc/modprobe.d/avm-rtlsdr-blacklist.conf
if ls /lib/modprobe.d/*rtl* /etc/modprobe.d/*rtl* >/dev/null 2>&1 \
   && grep -qs dvb_usb_rtl28xxu /lib/modprobe.d/*rtl* /etc/modprobe.d/*rtl*; then
    echo "RTL-SDR TV driver already blacklisted"
elif [ $CHECK_ONLY = 1 ]; then
    echo "RTL-SDR TV driver (dvb_usb_rtl28xxu) not blacklisted"
    plan "RTL-SDR: block the TV driver" "$BL"
else
    printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' | $SUDO tee $BL >/dev/null
    $SUDO modprobe -r dvb_usb_rtl28xxu 2>/dev/null
    echo "blacklisted the RTL-SDR TV driver ($BL)"
fi
for g in plugdev dialout audio video; do
    getent group $g >/dev/null || continue
    if id -nG | grep -qw $g; then continue; fi
    if [ $CHECK_ONLY = 1 ]; then echo "not in group $g"; plan "add $USER to group $g (USB/audio access)" "/etc/group"
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
    plan "Pluto USB network rule" "$PLUTO_RULE"
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
Comment=AVM -- Audio Video Modem (TX and RX, full screen)
Path=$AVM_DIR
Exec=$AVM_GUI
Icon=network-transmit-receive
Terminal=false
Categories=Network;HamRadio;"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
# launchers an earlier AVM made under other names (hfmodem-touch.desktop...)
# for the Python GUI -- or already switched to this one by a 1.1.0 test
# build: removed, avm.desktop (written below) is the one AVM icon
OLD="$(grep -l -e 'touch_gui.py' -e "Exec=$AVM_GUI" "$HOME"/.local/share/applications/*.desktop \
       "$DESKTOP_DIR"/*.desktop 2>/dev/null | grep -v '/avm.desktop$')"
if [ $CHECK_ONLY = 1 ]; then
    [ -f "$HOME/.local/share/applications/avm.desktop" ] && grep -q "$AVM_GUI" "$HOME/.local/share/applications/avm.desktop" \
        && echo "present" || echo "missing or for the Python GUI (would be set to $AVM_GUI)"
    plan "menu entry + desktop icon" "~/.local/share/applications, Desktop"
    plan "'avm' command" "$HOME/.local/bin/avm"
    for f in $OLD; do echo "old AVM launcher (would be removed): $f"; done
else
    mkdir -p "$HOME/.local/share/applications"
    echo "$DESK" > "$HOME/.local/share/applications/avm.desktop"
    for f in $OLD; do
        rm -f "$f" && echo "old AVM launcher removed: $f"
    done
    if [ -d "$DESKTOP_DIR" ]; then
        echo "$DESK" > "$DESKTOP_DIR/avm.desktop"
        chmod +x "$DESKTOP_DIR/avm.desktop"
        # GNOME (Ubuntu) only runs desktop icons it's told are trusted
        gio set "$DESKTOP_DIR/avm.desktop" metadata::trusted true 2>/dev/null
    fi
    echo "menu entry + desktop icon 'AVM' -> $AVM_GUI"
fi
# an 'avm' command too (in ~/.local/bin, on PATH from the next login)
if [ $CHECK_ONLY = 0 ]; then
    mkdir -p "$HOME/.local/bin"
    cat > "$HOME/.local/bin/avm" <<AVMCMD
#!/bin/sh
# AVM (written by install_avm.sh)
cd "$AVM_DIR" || exit 1
exec "$AVM_GUI" "\$@"
AVMCMD
    chmod +x "$HOME/.local/bin/avm"
    echo "'avm' command: $HOME/.local/bin/avm"
fi


# ---------------------------------------------------------------- summary
if [ $CHECK_ONLY = 1 ]; then
    say "What installing will add or change"
    if [ -z "$PLAN" ]; then
        echo "  nothing: everything AVM needs is already here"
    else
        printf '  %-44s %-30s %s\n' "WHAT" "WHERE" "SIZE"
        printf '%b' "$PLAN"
        echo "  ------------------------------------------------------------"
        echo "  about $PLAN_MB MB of disk space in total (approximate)"
        echo "  free now: $(df -BM --output=avail "$HOME" 2>/dev/null | tail -n 1 | tr -d ' ') in $HOME"
        echo "  Settings go in ~/.config/hfmodem; logs in $AVM_DIR/gui_logs"
    fi
fi
say "Radios Soapy can see now"
timeout 20 SoapySDRUtil --find 2>/dev/null | grep -E 'driver|label' | sed 's/^ */  /' || echo "  (none found)"
say "Done"
echo "Start AVM from the menu / desktop icon, or with the command:  avm"
echo "If this run added you to any groups, log out and back in first."
[ -n "${LOG:-}" ] && echo "Install log (everything above): $LOG"
exit 0
}
