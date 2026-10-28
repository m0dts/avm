#include "touch.h"

#include <QApplication>
#include <QGridLayout>
#include <QPointer>
#include <QElapsedTimer>
#include <QNetworkDatagram>
#include <QNetworkInterface>
#include <QTcpSocket>
#include <QUdpSocket>
#include <tuple>
#include <QScrollArea>
#include <QRegularExpression>
#include <QTime>
#include <QHBoxLayout>
#include <QKeyEvent>
#include <QLibrary>
#include <QMessageBox>
#include <QScroller>
#include <QTimer>
#include <QVBoxLayout>
#include <cmath>
#include <thread>

#include "pages.h"
#include "style.h"

namespace avm {

void coverWindow(QDialog* d, QWidget* parent) {
    QWidget* top = parent ? parent->window() : nullptr;
    if (top && ui.touch) {
        // on the touchscreen: drawn inside the main window, not as a window of its
        // own -- labwc's touch-as-mouse put a new window's first press where the
        // pointer last was. Covering the whole window, it still blocks what's under it.
        d->setParent(top, Qt::Widget);
        d->setAttribute(Qt::WA_StyledBackground);
        d->setAutoFillBackground(true);
        d->setGeometry(top->rect());
        d->raise();
    } else if (top) d->setGeometry(top->geometry());
    else d->resize(ui.px(800), ui.px(480));
}

// a USB keyboard's keys to this dialog (its buttons never take the focus): once
// it's up -- drawn inside the window, as a window of its own
static void takeKeyboard(QDialog* d) {
    d->setFocusPolicy(Qt::StrongFocus);
    QTimer::singleShot(0, d, [d] {
        d->activateWindow();
        d->setFocus(Qt::OtherFocusReason);
    });
}

void embedDialog(QDialog* d, QWidget* parent) {
    QWidget* top = parent ? parent->window() : nullptr;
    if (!top || !ui.touch) return;
    auto* back = new QWidget(top);
    back->setAttribute(Qt::WA_StyledBackground);
    back->setStyleSheet("background: rgba(0, 0, 0, 150);");
    back->setGeometry(top->rect());
    back->show();
    back->raise();
    QObject::connect(d, &QObject::destroyed, back, &QObject::deleteLater);
    QObject::connect(d, &QDialog::finished, back, &QObject::deleteLater);
    QSize want = d->size().isValid() && d->testAttribute(Qt::WA_Resized) ? d->size() : d->sizeHint();
    d->setParent(top, Qt::Widget);
    d->setAttribute(Qt::WA_StyledBackground);
    d->setAutoFillBackground(true);
    want = want.boundedTo(top->size());
    d->setGeometry(QRect(QPoint((top->width() - want.width()) / 2, (top->height() - want.height()) / 2), want));
    d->raise();
}

static QDialog* frameless(QDialog* d) {
    d->setWindowFlags(Qt::FramelessWindowHint | Qt::Dialog);
    d->setModal(true);
    return d;
}

// ---------------------------------------------------------------- TouchListDialog
TouchListDialog::TouchListDialog(const QString& title, const QStringList& items, const QString& current,
                                 QWidget* parent, std::function<QStringList()> refresh)
    : QDialog(parent), refresh_(std::move(refresh)), current_(current) {
    frameless(this);
    auto* lay = new QVBoxLayout(this);
    auto* top = new QHBoxLayout;
    auto* t = new QLabel(title);
    t->setObjectName("big");
    top->addWidget(t, 1);
    if (refresh_) {
        refreshBtn_ = new QPushButton("Refresh");
        connect(refreshBtn_, &QPushButton::clicked, this, &TouchListDialog::doRefresh);
        top->addWidget(refreshBtn_);
    }
    auto* cancel = new QPushButton("Cancel");
    connect(cancel, &QPushButton::clicked, this, &QDialog::reject);
    top->addWidget(cancel);
    lay->addLayout(top);
    auto* scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    QScroller::grabGesture(scroll->viewport(), QScroller::LeftMouseButtonGesture);
    grid_ = new QWidget;
    new QGridLayout(grid_);
    scroll->setWidget(grid_);
    lay->addWidget(scroll, 1);
    if (items.isEmpty() && refresh_) QTimer::singleShot(0, this, &TouchListDialog::doRefresh);   // once it's up
    else fill(items);
    coverWindow(this, parent);
}

// the buttons: 4 columns for short names (frame rates, ports), fewer as they
// get longer, one for device names; the current choice lit
void TouchListDialog::fill(const QStringList& items, bool enabled) {
    auto* gl = static_cast<QGridLayout*>(grid_->layout());
    while (QLayoutItem* it = gl->takeAt(0)) {
        delete it->widget();
        delete it;
    }
    int longest = 0;
    for (const QString& s : items) longest = std::max(longest, (int)s.size());
    const int cols = longest <= 8 ? 4 : longest <= 14 ? 3 : longest <= 24 ? 2 : 1;
    gl->setSpacing(ui.px(8));
    gl->setContentsMargins(0, ui.px(4), 0, 0);
    for (int i = 0; i < items.size(); i++) {
        auto* b = new QPushButton(items[i]);
        b->setFocusPolicy(Qt::NoFocus);
        b->setStyleSheet(QString("min-height: %1px;").arg(ui.px(56)));   // (the style's would win otherwise)
        b->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);
        b->setCheckable(true);
        b->setChecked(items[i] == current_);
        b->setEnabled(enabled);
        connect(b, &QPushButton::clicked, this, [this, v = items[i]] {
            result_ = v;
            accept();
        });
        gl->addWidget(b, i / cols, i % cols);
    }
    for (int c = 0; c < 4; c++) gl->setColumnStretch(c, c < cols ? 1 : 0);
    gl->setRowStretch(gl->rowCount(), 1);     // the buttons at the top
}

void TouchListDialog::doRefresh() {
    refreshBtn_->setEnabled(false);
    refreshBtn_->setText("Searching...");
    if (grid_->layout()->count() == 0) fill({"Searching..."}, false);
    int n = ++search_;
    auto fn = refresh_;
    QPointer<TouchListDialog> self(this);
    std::thread([self, fn, n] {
        QStringList items = fn();
        QMetaObject::invokeMethod(qApp, [self, items, n] { if (self) self->searchDone(items, n); }, Qt::QueuedConnection);
    }).detach();
}

void TouchListDialog::searchDone(const QStringList& items, int n) {
    if (n != search_) return;          // an older search
    fill(items);
    refreshBtn_->setText("Refresh");
    refreshBtn_->setEnabled(true);
}

// ---------------------------------------------------------------- Picker
Picker::Picker(const QString& title, const QStringList& items, const QString& current, QWidget* parent)
    : QPushButton(parent), title_(title), items_(items) {
    setFocusPolicy(Qt::NoFocus);
    setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Fixed);    // long names elided, never widening the layout
    setValue(current);
    connect(this, &QPushButton::clicked, this, &Picker::pick);
}

Picker::Picker(const QString& title, std::function<QStringList()> itemsFn, const QString& current, QWidget* parent)
    : Picker(title, QStringList(), current, parent) {
    itemsFn_ = std::move(itemsFn);
}

void Picker::setValue(const QString& v) {
    value_ = v;
    showText();
}

void Picker::resizeEvent(QResizeEvent* e) {
    QPushButton::resizeEvent(e);
    showText();
}

void Picker::showText() {
    QString full = shown(value_);
    if (full.isEmpty()) full = "(none)";
    const QString arrow = QString::fromUtf8("  ▾");
    int room = std::max(20, width() - fontMetrics().horizontalAdvance(arrow) - ui.px(24));
    setText(fontMetrics().elidedText(full, Qt::ElideMiddle, room) + arrow);
}

void Picker::pick() {
    TouchListDialog d(title_, itemsFn_ ? QStringList() : items_, value_, this, itemsFn_);
    if (d.exec() && !d.result().isEmpty() && d.result() != value_) {
        setValue(d.result());
        emit changed(value_);
    }
}

// ---------------------------------------------------------------- Stepper
Stepper::Stepper(double lo, double hi, double step, double value, const QString& suffix, QWidget* parent)
    : QWidget(parent), lo_(lo), hi_(hi), step_(step), suffix_(suffix) {
    setObjectName("seg");
    auto* lay = new QHBoxLayout(this);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(ui.px(4));
    auto* minus = new QPushButton(QString::fromUtf8("−"));
    auto* plus = new QPushButton("+");
    label_ = new QLabel;
    label_->setObjectName("value");
    label_->setAlignment(Qt::AlignCenter);
    for (auto* b : {minus, plus}) {
        b->setAutoRepeat(true);
        b->setAutoRepeatDelay(400);
        b->setAutoRepeatInterval(80);
        b->setFocusPolicy(Qt::NoFocus);
        b->setMinimumWidth(ui.px(72));          // wide: easy to hit
        b->setStyleSheet(QString("font-size: %1px; font-weight: bold;").arg(ui.px(24)));
    }
    minus_ = minus;
    plus_ = plus;
    connect(minus, &QPushButton::clicked, this, [this] { setValue(value_ - step_, true); });
    connect(plus, &QPushButton::clicked, this, [this] { setValue(value_ + step_, true); });
    lay->addWidget(minus, 2);
    lay->addWidget(label_, 3);
    lay->addWidget(plus, 2);
    setValue(value);
}

void Stepper::setArrowWidth(int px) {
    minus_->setMinimumWidth(px);
    plus_->setMinimumWidth(px);
}

void Stepper::setValue(double v, bool emitChange) {
    v = std::round(std::round(v / step_) * step_ * 1e6) / 1e6;
    v = std::min(hi_, std::max(lo_, v));
    if (v == value_) return;
    value_ = v;
    QString t = QString::number(v, 'g', 6);
    label_->setText((sign && v > 0 ? "+" : "") + t + suffix_);
    if (emitChange) emit changed(v);
}

// ---------------------------------------------------------------- KeypadDialog
KeypadDialog::KeypadDialog(const QString& title, const QString& text, QWidget* parent) : QDialog(parent) {
    frameless(this);
    auto* lay = new QVBoxLayout(this);
    auto* t = new QLabel(title);
    t->setObjectName("big");
    lay->addWidget(t);
    display_ = new QLabel(text);
    display_->setObjectName("big");
    display_->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    lay->addWidget(display_);
    auto* grid = new QGridLayout;
    const QStringList keys = {"7", "8", "9", "4", "5", "6", "1", "2", "3", ".", "0", QString::fromUtf8("⌫")};
    for (int i = 0; i < keys.size(); ++i) {
        auto* b = new QPushButton(keys[i]);
        b->setObjectName("run");
        b->setFocusPolicy(Qt::NoFocus);
        b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
        QString k = keys[i];
        connect(b, &QPushButton::clicked, this, [this, k] { key(k); });
        grid->addWidget(b, i / 3, i % 3);
    }
    lay->addLayout(grid, 1);
    auto* row = new QHBoxLayout;
    auto* cancel = new QPushButton("Cancel");
    auto* ok = new QPushButton("OK  (MHz)");
    for (auto* b : {cancel, ok}) { b->setObjectName("run"); b->setFocusPolicy(Qt::NoFocus); row->addWidget(b); }
    connect(cancel, &QPushButton::clicked, this, &QDialog::reject);
    connect(ok, &QPushButton::clicked, this, &QDialog::accept);
    lay->addLayout(row);
    coverWindow(this, parent);
    takeKeyboard(this);
}

void KeypadDialog::keyPressEvent(QKeyEvent* e) {
    const int k = e->key();
    if (k >= Qt::Key_0 && k <= Qt::Key_9) key(QString(QChar('0' + (k - Qt::Key_0))));
    else if (k == Qt::Key_Period || k == Qt::Key_Comma) key(".");
    else if (k == Qt::Key_Backspace || k == Qt::Key_Delete) key(QString::fromUtf8("⌫"));
    else if (k == Qt::Key_Return || k == Qt::Key_Enter) accept();
    else if (k == Qt::Key_Escape) reject();
    else QDialog::keyPressEvent(e);
}

void KeypadDialog::key(const QString& k) {
    QString s = fresh_ ? QString() : display_->text();    // the first key replaces the old value
    fresh_ = false;
    if (k == QString::fromUtf8("⌫")) s.chop(1);
    else if (k == "." && s.contains('.')) return;
    else if (s.size() < 11) s += k;
    display_->setText(s);
}

bool KeypadDialog::value(double* v) const {
    bool ok = false;
    *v = display_->text().toDouble(&ok);
    return ok;
}

// ---------------------------------------------------------------- FreqButton
FreqButton::FreqButton(const QString& title, double hz, QWidget* parent) : QPushButton(parent), title_(title) {
    setFocusPolicy(Qt::NoFocus);
    setHz(hz);
    connect(this, &QPushButton::clicked, this, &FreqButton::edit);
}

void FreqButton::setHz(double hz) {
    hz_ = std::round(hz);
    setText(QString("%1 MHz").arg(hz_ / 1e6, 0, 'f', 4));
}

void FreqButton::edit() {
    QString cur = QString::number(hz_ / 1e6, 'f', 4);
    while (cur.contains('.') && (cur.endsWith('0') || cur.endsWith('.'))) cur.chop(1);
    KeypadDialog d(title_, cur, this);
    double mhz;
    if (!d.exec() || !d.value(&mhz)) return;
    if (mhz < lo_ || mhz > hi_) {
        QMessageBox::information(this, "Frequency", QString("%1 MHz is outside the %2's range (%3-%4 MHz).")
                                                        .arg(mhz).arg(radio_).arg(lo_).arg(hi_));
        return;
    }
    double hz = std::round(mhz * 1e6);
    if (hz != hz_) {
        setHz(hz);
        emit changed(hz);
    }
}

// ---------------------------------------------------------------- KeyboardDialog
const QStringList KeyboardDialog::ROWS = {"1234567890", "QWERTYUIOP", "ASDFGHJKL/", "ZXCVBNM-.:"};   // (host:port too; space: its own bar)
const QStringList KeyboardDialog::MESSAGE_ROWS = {"1234567890", "QWERTYUIOP", "ASDFGHJKL/", "ZXCVBNM-.,", "?!'()@:#=+"};

KeyboardDialog::KeyboardDialog(const QString& title, const QString& text, int maxLen, QWidget* parent,
                               const QStringList& rows, bool spaceBar)
    : QDialog(parent), max_(maxLen) {
    frameless(this);
    auto* lay = new QVBoxLayout(this);
    lay->setSpacing(ui.px(4));
    auto* t = new QLabel(QString("%1  (up to %2 characters)").arg(title).arg(maxLen));
    t->setObjectName("dim");
    lay->addWidget(t);
    display_ = new QLabel;
    display_->setObjectName("big");
    display_->setStyleSheet(QString("background: %1; padding: 4px 8px; font-family: monospace;").arg(PANEL));
    display_->setWordWrap(true);
    lay->addWidget(display_);
    auto* grid = new QGridLayout;
    grid->setSpacing(ui.px(4));
    auto addKey = [&](const QString& k, int r, int c, int span) {
        auto* b = new QPushButton(k == " " ? QString("space") : k);
        b->setFocusPolicy(Qt::NoFocus);
        b->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
        connect(b, &QPushButton::clicked, this, [this, k] { if (text_.size() < max_) set(text_ + k); });
        grid->addWidget(b, r, c, 1, span);
    };
    for (int r = 0; r < rows.size(); ++r)
        for (int c = 0; c < rows[r].size(); ++c) addKey(QString(rows[r][c]), r, c, 1);
    if (spaceBar) addKey(" ", rows.size(), 2, 6);           // a phone-style space bar
    lay->addLayout(grid, 1);
    auto* row = new QHBoxLayout;
    auto button = [&](const QString& label, std::function<void()> fn) {
        auto* b = new QPushButton(label);
        b->setObjectName("run");
        b->setFocusPolicy(Qt::NoFocus);
        connect(b, &QPushButton::clicked, this, fn);
        row->addWidget(b);
    };
    button(QString::fromUtf8("⌫"), [this] { set(text_.left(text_.size() - 1)); });
    button("Clear", [this] { set(QString()); });
    button("Cancel", [this] { reject(); });
    button("OK", [this] { accept(); });
    lay->addLayout(row);
    allowed_ = rows.join(QString()) + (spaceBar ? " " : "");
    set(text);
    coverWindow(this, parent);
    takeKeyboard(this);
}

void KeyboardDialog::keyPressEvent(QKeyEvent* e) {
    const int k = e->key();
    if (k == Qt::Key_Backspace) { set(text_.left(text_.size() - 1)); return; }
    if (k == Qt::Key_Return || k == Qt::Key_Enter) { accept(); return; }
    if (k == Qt::Key_Escape) { reject(); return; }
    for (QChar c : e->text()) {
        if (!allowed_.contains(c) && allowed_.contains(c.toUpper())) c = c.toUpper();   // (callsigns: capitals only)
        if (!allowed_.contains(c) && allowed_.contains(c.toLower())) c = c.toLower();
        if (allowed_.contains(c) && text_.size() < max_) set(text_ + c);
    }
    if (e->text().isEmpty()) QDialog::keyPressEvent(e);
}

void KeyboardDialog::set(const QString& t) {
    text_ = t.left(max_);
    display_->setText(text_ + QString::fromUtf8("▏"));
}

// ---------------------------------------------------------------- TextButton
TextButton::TextButton(const QString& title, const QString& text, int maxLen, const QString& placeholder, QWidget* parent)
    : QPushButton(parent), title_(title), placeholder_(placeholder), max_(maxLen) {
    setFocusPolicy(Qt::NoFocus);
    setValue(text);
    connect(this, &QPushButton::clicked, this, [this] {
        KeyboardDialog d(title_, value_, max_, this, KeyboardDialog::ROWS, true);
        if (d.exec() && d.text() != value_) {
            setValue(d.text());
            emit changed(value_);
        }
    });
}

void TextButton::setValue(const QString& t) {
    value_ = t.toUpper().left(max_);
    setText(value_.isEmpty() ? placeholder_ : value_);
}

// ---------------------------------------------------------------- Notices
Notices& Notices::get() {
    static Notices* n = new Notices;
    return *n;
}

void Notices::add(const QString& source, const QString& text) {
    QString t = text.simplified().left(300);
    if (t.isEmpty()) return;
    QString now = QTime::currentTime().toString("HH:mm:ss");
    if (!items_.isEmpty() && items_.last().source == source && items_.last().text == t) {
        items_.last().time = now;
        items_.last().count++;
    } else {
        items_.append({now, source, t, 1});
        while (items_.size() > 200) items_.removeFirst();
    }
    emit changed();
}

// as touch_widgets._ERROR_RE: "ERROR", an ...Error / ...Exception, not "0 errors"
// nor ffmpeg's complaints as its output pipe closes at a stop
void Notices::addIfError(const QString& source, const QString& line) {
    static const QRegularExpression re(R"(\b[A-Za-z_]*(Error|Exception)\b|\bERROR\b|^XX )");
    static const char* ignore[] = {"Broken pipe", "Error muxing", "Error writing trailer", "Error closing file"};
    if (!re.match(line).hasMatch()) return;
    for (const char* i : ignore)
        if (line.contains(i)) return;
    QString t = line;
    if (t.startsWith("ERROR:")) t = t.mid(6);
    add(source, t);
}

void Notices::clear() {
    items_.clear();
    emit changed();
}

int Notices::count() const {
    int n = 0;
    for (const Item& i : items_) n += i.count;
    return n;
}

NoticesDialog::NoticesDialog(QWidget* parent) : QDialog(parent) {
    frameless(this);
    auto* lay = new QVBoxLayout(this);
    auto* top = new QHBoxLayout;
    auto* t = new QLabel("Errors and warnings");
    t->setObjectName("big");
    top->addWidget(t, 1);
    auto* clear = new QPushButton("Clear errors");
    connect(clear, &QPushButton::clicked, this, [this] { Notices::get().clear(); fill(); });
    top->addWidget(clear);
    auto* close = new QPushButton("Close");
    connect(close, &QPushButton::clicked, this, &QDialog::accept);
    top->addWidget(close);
    lay->addLayout(top);
    list_ = new QListWidget;
    list_->setWordWrap(true);
    list_->setVerticalScrollMode(QAbstractItemView::ScrollPerPixel);
    QScroller::grabGesture(list_->viewport(), QScroller::LeftMouseButtonGesture);
    lay->addWidget(list_, 1);
    fill();
    coverWindow(this, parent);
}

void NoticesDialog::fill() {
    list_->clear();
    const auto& items = Notices::get().items();
    if (items.isEmpty()) { list_->addItem("No errors."); return; }
    for (int i = items.size() - 1; i >= 0; i--) {
        const auto& it = items[i];
        list_->addItem(QString("%1  %2: %3").arg(it.time, it.source, it.text) +
                       (it.count > 1 ? QString::fromUtf8("   ×%1").arg(it.count) : QString()));
    }
}

// ---------------------------------------------------------------- radios
void radioRange(const QString& d, double* lo, double* hi) {
    struct R { const char* d; double lo, hi; };
    static const R ranges[] = {{"pluto", 70, 6000},     {"lime", 0.1, 3800},     {"rtlsdr", 24, 1766},
                               {"airspy", 24, 1800},    {"airspyhf", 0.01, 260}, {"sdrplay", 0.001, 2000},
                               {"virtual", 0.001, 6000}, {"kiwi", 0.01, 32},     {"hpsdr", 0.01, 55}};
    *lo = 70;
    *hi = 6000;
    for (const R& r : ranges)
        if (d == r.d) { *lo = r.lo; *hi = r.hi; }
}

QString radioName(const QString& d) {
    static const QMap<QString, QString> names = {{"pluto", "PlutoSDR"}, {"lime", "LimeSDR"}, {"rtlsdr", "RTL-SDR"},
                                                 {"airspy", "Airspy"},  {"airspyhf", "Airspy HF+"},
                                                 {"sdrplay", "SDRplay"}, {"virtual", "Virtual radio"}};
    return names.value(d, d);
}

const char* RadioPicker::VIRTUAL_LABEL = "Virtual radio (this machine, no hardware)";

// the radios connected now: libavm's search, "driver\tlabel\turi" lines
// openHPSDR radios on the network (hpsdr_source.discover): protocol 2 (ANAN) and
// protocol 1 (Hermes-Lite 2, Radioberry) discovery broadcast, answers for ~0.6 s.
// [(ip, board name, busy)]; run on the picker's worker thread (blocking sockets)
static QList<std::tuple<QString, QString, bool>> discoverHpsdr() {
    static const QMap<int, QString> p1 = {{0, "Metis"}, {1, "Hermes"}, {2, "Griffin"}, {4, "Angelia"},
                                          {5, "Orion"}, {6, "Hermes Lite"}, {10, "Orion MkII"}};
    static const QMap<int, QString> p2 = {{0, "Atlas"}, {1, "Hermes"}, {2, "Hermes II"}, {3, "Angelia"},
                                          {4, "Orion"}, {5, "Orion MkII"}, {6, "Hermes Lite"}, {10, "Saturn"}};
    QUdpSocket sk;
    if (!sk.bind(QHostAddress::AnyIPv4, 0)) return {};
    QByteArray d1(63, 0), d2(60, 0);
    d1[0] = (char)0xEF; d1[1] = (char)0xFE; d1[2] = 0x02;
    d2[4] = 0x02;
    QList<QHostAddress> dests = {QHostAddress::Broadcast};
    for (const QNetworkInterface& ni : QNetworkInterface::allInterfaces())     // each network's own broadcast too
        for (const QNetworkAddressEntry& e : ni.addressEntries())
            if (e.ip().protocol() == QAbstractSocket::IPv4Protocol && !e.broadcast().isNull()) dests << e.broadcast();
    for (const QHostAddress& a : dests) {
        sk.writeDatagram(d2, a, 1024);
        sk.writeDatagram(d1, a, 1024);
    }
    QMap<QString, std::tuple<QString, QString, bool>> found;
    QElapsedTimer t;
    t.start();
    while (t.elapsed() < 600) {
        if (!sk.hasPendingDatagrams() && !sk.waitForReadyRead(600 - (int)t.elapsed())) break;
        while (sk.hasPendingDatagrams()) {
            QNetworkDatagram g = sk.receiveDatagram();
            QByteArray d = g.data();
            QString ip = QHostAddress(g.senderAddress().toIPv4Address()).toString();
            if (d.size() >= 12 && d.startsWith(QByteArray(4, 0)) && (d[4] == 2 || d[4] == 3))       // protocol 2
                found[ip] = {ip, p2.value((uchar)d[11], QString("board %1").arg((uchar)d[11])), d[4] == 3};
            else if (d.size() >= 11 && (uchar)d[0] == 0xEF && (uchar)d[1] == 0xFE && (d[2] == 2 || d[2] == 3) &&
                     !found.contains(ip))                                                          // protocol 1
                found[ip] = {ip, p1.value((uchar)d[10], QString("board %1").arg((uchar)d[10])), d[2] == 3};
        }
    }
    return found.values();
}

static QStringList findRadios(bool rx, QMap<QString, QPair<QString, QString>>* meaning) {
    using Fn = int (*)(int, char*, int);
    static QLibrary lib(avmDir() + "/c++/avm");
    auto fn = (Fn)((lib.isLoaded() || lib.load()) ? lib.resolve("avm_sdr_find") : nullptr);
    QStringList labels;
    if (fn) {
        std::vector<char> buf(8192);
        int need = fn(rx, buf.data(), (int)buf.size());
        if (need > (int)buf.size()) { buf.resize(need); fn(rx, buf.data(), need); }
        QList<QStringList> found;
        bool plutoIp = false;
        for (const QString& line : QString::fromUtf8(buf.data()).split('\n', Qt::SkipEmptyParts)) {
            QStringList f = line.split('\t');
            if (f.size() < 2) continue;
            found << f;
            plutoIp = plutoIp || (f[0] == "pluto" && f.value(2).startsWith("ip:"));
        }
        // a Pluto shows twice on Linux (USB and its network address over the same
        // cable): the IP one only. Windows' search finds no network ones: the USB one then
        if (!plutoIp) {
            // ... and a Pluto at its usual address answering, though not found (no
            // network search on Windows; or no USB driver there): offered by IP too
            QTcpSocket s;
            s.connectToHost("192.168.2.1", 30431);           // (its IIO server's port)
            if (s.waitForConnected(400)) {
                found << QStringList{"pluto", "PlutoSDR (IP 192.168.2.1)", "ip:192.168.2.1"};
                plutoIp = true;
            }
        }
        for (const QStringList& f : found) {
            if (f[0] == "pluto" && f.value(2).startsWith("usb:") && plutoIp) continue;
            labels << f[1];
            (*meaning)[f[1]] = {f[0], f.value(2)};
        }
    }
    for (const auto& [ip, board, busy] : discoverHpsdr()) {     // (TX: protocol 1 ones, avm_tx's hpsdr1.h)
        QString l = QString("HPSDR %1 %2").arg(board, ip) + (busy ? " (in use)" : "");
        labels << l;
        (*meaning)[l] = {"hpsdr", ip};
    }
    if (rx) {   // a KiwiSDR on the network: the one set in Config (Receiver)
        QString kiwi = settings().value("kiwi_server").toString().trimmed();
        QString l = kiwi.isEmpty() ? QString("KiwiSDR (set its server: Config, Receiver)") : "KiwiSDR " + kiwi;
        labels << l;
        if (!kiwi.isEmpty()) (*meaning)[l] = {"kiwi", kiwi};
    }
    if (labels.isEmpty() && !fn) labels << "(radio search unavailable -- AVM library too old)";
    labels << RadioPicker::VIRTUAL_LABEL;
    (*meaning)[RadioPicker::VIRTUAL_LABEL] = {"virtual", ""};
    return labels;
}

RadioPicker::RadioPicker(const QString& title, bool rx, const QString& driver, const QString& uri, QWidget* parent)
    : Picker(title, QStringList(), QString(), parent), driver_(driver), uri_(uri) {
    // the search's labels -> what they mean, shared with the picked handler
    auto meaning = std::make_shared<QMap<QString, QPair<QString, QString>>>();
    auto search = [rx, meaning] {
        QMap<QString, QPair<QString, QString>> m;
        QStringList l = findRadios(rx, &m);
        *meaning = m;      // (read on the GUI thread only after the search's results arrive)
        return l;
    };
    setItemsFn(search);
    setValue(driver_);
    connect(this, &Picker::changed, this, [this, meaning](const QString& label) {
        auto it = meaning->find(label);
        if (it == meaning->end()) { setValue(driver_); return; }   // "(no radios...)": keep the old one
        driver_ = it->first;
        uri_ = it->second;
        setValue(driver_);
        emit radioChanged(driver_, uri_);
    });
}

QString RadioPicker::shown(const QString&) const {
    if (driver_ == "pluto" && uri_.startsWith("usb:")) return "Pluto USB";
    if (driver_ == "pluto" && uri_.startsWith("ip:")) return "Pluto IP";
    static const QMap<QString, QString> shortName = {{"pluto", "Pluto"}, {"lime", "Lime"}, {"rtlsdr", "RTL"},
                                                     {"airspy", "Airspy"}, {"airspyhf", "HF+"},
                                                     {"sdrplay", "SDRplay"}, {"hpsdr", "HPSDR"}, {"kiwi", "Kiwi"},
                                                     {"virtual", "Virtual"}};
    return shortName.value(driver_, driver_);
}

}  // namespace avm
