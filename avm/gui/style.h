// style.h -- AVM's look (touch_widgets.py): colours, and the stylesheet at
// a scale. Two sizes: touch (the Pi's screen, scaled from 800x480) and
// desktop (smaller buttons and fonts, for a mouse).
#pragma once
#include <QString>

namespace avm {

const char* const BG = "#25272b";
const char* const PANEL = "#2f3237";
const char* const BUTTON = "#3c4046";
const char* const BUTTON_PRESSED = "#50555c";
const char* const TEXT = "#e6e8eb";
const char* const TEXT_DIM = "#9aa0a8";
const char* const TX_ACCENT = "#e07a2e";
const char* const RX_ACCENT = "#2ea8b0";
const char* const CONFIG_ACCENT = "#8e7cc3";
const char* const GO = "#2f9e5a";
const char* const STOP = "#c0392b";
const char* const WARN = "#e0b030";

struct Ui {
    bool touch = false;
    double scale = 1.0;            // touch: screen height / 480
    int px(double v) const;        // a size at the current scale and mode
};
extern Ui ui;

QString stylesheet(const char* accent);
QString accentStylesheet(const char* accent);

}  // namespace avm
