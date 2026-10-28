"""AVM -- Audio Video Modem: name and version, shown by the GUIs.

The modem itself keeps its hf_ofdm_* script names (a UHF variant may
follow); this is the product name around it."""
NAME = "AVM"
FULL_NAME = "Audio Video Modem"
# 1.0.BUILD: BUILD goes up by one each release -- tools/make_release.py bumps
# it (and writes it here) every time it builds the release folder.
BUILD = 21
VERSION = "1.1.0"   # the C++ AVM (BUILD: the 1.0.x Python series)

SHORT_TITLE = f"{NAME} v{VERSION}"                   # e.g. top bar
TITLE = f"{NAME} — {FULL_NAME} v{VERSION}"      # window titles
