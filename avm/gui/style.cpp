#include "style.h"

#include <algorithm>
#include <cmath>

namespace avm {

Ui ui;

// touch: the Python's sizes at the screen's scale; desktop (a window): the
// same sizes as the Pi's 800x480 screen
int Ui::px(double v) const {
    double s = touch ? scale : 1.0;   // the same sizes on a desktop as on the Pi (0.62 was too small)
    return std::max(1, (int)std::lround(v * s));
}

QString stylesheet(const char* accent) {
    auto f = [](double v) { return QString::number(ui.px(v)); };
    return QString(
               "QWidget { background: %1; color: %2; font-size: %3px; }"
               "QLabel#dim { color: %4; font-size: %5px; }"
               "QLabel#rowlabel { color: %4; font-size: %6px; }"
               "QLabel#value { font-size: %7px; }"
               "QLabel#big { font-size: %8px; font-weight: bold; }"
               "QLabel#warn { color: %9; font-size: %5px; }"
               "QFrame#panel { background: %10; border-radius: %11px; }"
               "QFrame#panel QLabel, QFrame#panel QWidget#seg { background: transparent; }")
               .arg(BG, TEXT, f(15), TEXT_DIM, f(17), f(18), f(16), f(20), WARN)   // (dim / warn / row labels: 4 px up)
               .arg(PANEL, f(8)) +
           QString(
               "QPushButton { background: %1; color: %2; border: none; border-radius: %3px;"
               "  min-height: %4px; padding: 0 %5px; font-size: %6px; }"
               "QPushButton:pressed { background: %7; }"
               "QPushButton:checked { background: %8; color: #111; font-weight: bold; }"
               "QPushButton:disabled { color: #666; }"
               "QPushButton#run { font-size: %9px; font-weight: bold; min-height: %10px; }"
               "QPushButton#msg { font-size: %11px; min-height: %10px; }"
               "QPushButton#toggle { font-size: %12px; font-weight: bold; min-width: %13px; }")
               .arg(BUTTON, TEXT, f(6), f(37), f(8), f(15), BUTTON_PRESSED, accent, f(20))
               .arg(f(56), f(32), f(18), f(90)) +
           QString(
               "QLineEdit, QComboBox { background: %1; border: none; border-radius: %2px;"
               "  min-height: %3px; padding: 0 %4px; }"
               "QListWidget { background: %5; border: none; font-size: %6px; }"
               "QListWidget::item { min-height: %7px; padding-left: %8px; border-bottom: 1px solid %9; }"
               "QListWidget::item:selected { background: %10; color: #111; }"
               "QScrollBar:vertical { width: %11px; background: %5; }"
               "QScrollBar::handle:vertical { background: %12; border-radius: %2px; min-height: %13px; }"
               "QScrollBar::add-line, QScrollBar::sub-line { height: 0; }")
               .arg(BUTTON, f(6), f(37), f(8), PANEL, f(18), f(52), f(12), BG)
               .arg(accent, f(18), BUTTON_PRESSED, f(40));
}

QString accentStylesheet(const char* accent) {
    return QString("QPushButton:checked { background: %1; color: #111; font-weight: bold; }"
                   "QListWidget::item:selected { background: %1; color: #111; }")
        .arg(accent);
}

}  // namespace avm
