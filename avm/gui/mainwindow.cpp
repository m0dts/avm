#include "mainwindow.h"

#include <QApplication>
#include <QBuffer>
#include <QDialog>
#include <QLabel>
#include <QPlainTextEdit>
#include <QPushButton>
#include <QScroller>
#include <QCloseEvent>
#include <QSettings>
#include <QDir>
#include <QMessageBox>
#include <QFile>
#include <QLibrary>
#include <QPointer>
#include <QHBoxLayout>
#include <QRegularExpression>
#include <QTimer>
#include <QVBoxLayout>

#include "style.h"
#include "web.h"

#ifdef Q_OS_WIN
#include <windows.h>
#endif

namespace avm {

MainWindow::MainWindow(bool demo, QWidget* parent) : QWidget(parent) {
    setWindowTitle("AVM");
    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(0, 0, 0, 0);
    root->setSpacing(0);
    setMinimumSize(480, 288);

    auto* bar = new QHBoxLayout;
    bar->setContentsMargins(ui.px(8), ui.px(6), ui.px(8), ui.px(2));
    view_ = new Segmented({{"config", "CONFIG"}, {"tx", "TX"}, {"rx", "RX"}}, settings().value("view", "rx").toString());
    for (const char* v : {"config", "tx", "rx"}) view_->button(v)->setObjectName("toggle");
    connect(view_, &Segmented::changed, this, &MainWindow::showView);
    bar->addWidget(view_);
    bar->addSpacing(ui.px(16));
    indicator_ = new QLabel;
    indicator_->setObjectName("value");
    indicator_->setSizePolicy(QSizePolicy::Ignored, QSizePolicy::Preferred);
    bar->addWidget(indicator_, 1);
    auto* notice = new QPushButton(QString::fromUtf8("⚠"));
    notice->setObjectName("toggle");
    notice->setFocusPolicy(Qt::NoFocus);
    notice->setFixedWidth(ui.px(80));    // room for "⚠ 99"
    notice->setToolTip("Errors and warnings");
    connect(notice, &QPushButton::clicked, this, [this] { NoticesDialog(this).exec(); });
    // red with its count while there's something to read, plain otherwise
    auto showCount = [notice] {
        int n = Notices::get().count();
        notice->setText(n ? QString::fromUtf8("⚠ %1").arg(n) : QString::fromUtf8("⚠"));
        notice->setStyleSheet(n ? QString("background: %1; color: white; padding: 0px;").arg(STOP)
                                : QString("background: %1; color: %2; padding: 0px;").arg(BUTTON, TEXT_DIM));
    };
    connect(&Notices::get(), &Notices::changed, notice, showCount);
    showCount();
    bar->addWidget(notice);
    // a newer AVM on GitHub (updater.h): shown once found
    update_ = new QPushButton;
    update_->setObjectName("toggle");
    update_->setFocusPolicy(Qt::NoFocus);
    update_->setStyleSheet(QString("background: %1; color: white; padding: 0px %2px;").arg(RX_ACCENT).arg(ui.px(8)));
    update_->hide();
    connect(update_, &QPushButton::clicked, this, &MainWindow::offerUpdate);
    bar->addSpacing(ui.px(8));
    bar->addWidget(update_);
    bar->addSpacing(ui.px(12));
    cpu_ = new QLabel("CPU --");
    cpu_->setObjectName("dim");
    bar->addWidget(cpu_);
    bar->addSpacing(ui.px(12));
    auto* quit = new QPushButton(QString::fromUtf8("✕"));
    quit->setObjectName("toggle");
    quit->setFocusPolicy(Qt::NoFocus);
    quit->setFixedWidth(ui.px(56));
    connect(quit, &QPushButton::clicked, this, [this] {
        if (tx_->running() || rx_->running()) {
            QMessageBox box(QMessageBox::Question, "Quit", "TX/RX is running. Stop and quit?",
                            QMessageBox::Yes | QMessageBox::No, this);
            embedDialog(&box, this);
            if (box.exec() != QMessageBox::Yes) return;
        }
        close();
    });
    bar->addWidget(quit);
    root->addLayout(bar);

    pages_ = new QStackedWidget;
    config_ = new ConfigPage;
    tx_ = new TxPage;
    rx_ = new RxPage;
    pages_->addWidget(config_);
    pages_->addWidget(tx_);
    pages_->addWidget(rx_);
    root->addWidget(pages_, 1);
    config_->setStyleSheet(accentStylesheet(CONFIG_ACCENT));
    tx_->setStyleSheet(accentStylesheet(TX_ACCENT));
    rx_->setStyleSheet(accentStylesheet(RX_ACCENT));
    connect(tx_, &TxPage::runningChanged, this, &MainWindow::updateIndicator);
    // duplex: RX's TX+RX runs both; TX's own button hidden
    rx_->setPeer(tx_);
    auto refreshDuplex = [this] { rx_->refreshDuplex(); tx_->refreshDuplex(); };
    connect(config_, &ConfigPage::duplexChanged, this, refreshDuplex);
    connect(&cpuTimer_, &QTimer::timeout, this, refreshDuplex);     // (TX / RX settings changed)
    QTimer::singleShot(0, this, refreshDuplex);
    connect(rx_, &RxPage::runningChanged, this, &MainWindow::updateIndicator);
    connect(config_, &ConfigPage::callsignChanged, tx_, &TxPage::callsignChanged);
    connect(config_, &ConfigPage::rxSettingChanged, rx_, &RxPage::rxSettingChanged);
    connect(config_, &ConfigPage::txSettingChanged, tx_, &TxPage::txSettingChanged);
    connect(config_, &ConfigPage::motionChanged, tx_, &TxPage::replan);
    connect(config_, &ConfigPage::spectrumAveragesChanged, rx_, &RxPage::setSpectrumAverages);
    connect(config_, &ConfigPage::modeTypeChanged, this, [this] {
        tx_->applyModeType();
        rx_->applyModeType();
    });
    showView(view_->value().isEmpty() ? "rx" : view_->value());
    if (demo) rx_->setDemo(true);
    updateIndicator();

    if (!qApp->arguments().contains("--screenshots") && !qApp->arguments().contains("--auto")) buildSplash();
    connect(&cpuTimer_, &QTimer::timeout, this, &MainWindow::updateCpu);
    cpuTimer_.start(2000);
    connect(&usbTimer_, &QTimer::timeout, this, &MainWindow::usbTick);
    usbTimer_.start(1000);
    if (!qApp->arguments().contains("--screenshots") && !qApp->arguments().contains("--auto")) {
        startWeb();
        // is there a newer AVM? (a few seconds in: start-up first)
        updater_ = new Updater(this);
        connect(updater_, &Updater::found, this, [this](const QString& v, const QString& notes) {
            updateVersion_ = v;
            updateNotes_ = notes;
            update_->setText(QString::fromUtf8("⬆ v%1").arg(v));
            update_->show();
            guiLog("GUI", "update available: v" + v);
        });
        QTimer::singleShot(5000, updater_, &Updater::check);
    }
}

// the newer version's notes; Update now: TX / RX stopped, the installer run (updater.h)
void MainWindow::offerUpdate() {
    QDialog d(this);
    auto* lay = new QVBoxLayout(&d);
    auto* title = new QLabel(QString("AVM v%1 is available (you have v%2)").arg(updateVersion_, Updater::localVersion()));
    title->setObjectName("big");
    title->setWordWrap(true);
    lay->addWidget(title);
    lay->addWidget(new QLabel("What's new:"));
    auto* text = new QPlainTextEdit(updateNotes_);
    text->setReadOnly(true);
    text->setTextInteractionFlags(Qt::NoTextInteraction);     // a drag scrolls, never selects
    QScroller::grabGesture(text->viewport(), QScroller::LeftMouseButtonGesture);
    lay->addWidget(text, 1);
    auto* note = new QLabel("Updating stops TX/RX, downloads the new version, installs it and restarts AVM.");
    note->setObjectName("dim");
    note->setWordWrap(true);
    lay->addWidget(note);
    auto* row = new QHBoxLayout;
    auto* no = new QPushButton("Not now");
    auto* yes = new QPushButton("Update now");
    connect(no, &QPushButton::clicked, &d, &QDialog::reject);
    connect(yes, &QPushButton::clicked, &d, &QDialog::accept);
    row->addWidget(no);
    row->addWidget(yes);
    lay->addLayout(row);
    coverWindow(&d, this);
    if (d.exec() != QDialog::Accepted) return;
    tx_->stop();
    rx_->stop();
    update_->setText("Updating...");
    update_->setEnabled(false);
    qApp->processEvents();
    if (!updater_->apply(this)) {
        update_->setText(QString::fromUtf8("⬆ v%1").arg(updateVersion_));
        update_->setEnabled(true);
    }
}

// ---- the USB watchdog (touch_gui._usb_tick): a radio or camera that drops off
// USB while TX / RX runs stops that side (with a notice), which starts again
// once the device has been back USB_RESUME_S; every device vanishing at once
// is the USB system itself failing. Linux only (sysfs).
static const double USB_RESUME_S = 4.0;

// (vendor, product) of every USB device attached now, root hubs excluded;
// empty and *ok false where that can't be read (not Linux)
static QSet<QString> usbDevices(bool* ok) {
    QSet<QString> out;
    QDir base("/sys/bus/usb/devices");
    *ok = base.exists();
    if (!*ok) return out;
    for (const QString& d : base.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
        QFile v(base.filePath(d + "/idVendor")), p(base.filePath(d + "/idProduct"));
        if (!v.open(QIODevice::ReadOnly) || !p.open(QIODevice::ReadOnly)) continue;   // interfaces
        QString vid = QString::fromLatin1(v.readAll()).trimmed(), pid = QString::fromLatin1(p.readAll()).trimmed();
        if (vid != "1d6b") out.insert(vid + ":" + pid);    // (Linux Foundation: the root hubs)
    }
    return out;
}

// the radio's USB IDs (touch_widgets.USB_IDS); a radio not on USB isn't watched
static QStringList radioUsbIds(const QString& sdr) {
    static const QMap<QString, QStringList> ids = {
        {"pluto", {"0456:b673"}},
        {"lime", {"1d50:6108", "0403:601f"}},
        {"rtlsdr", {"0bda:2838", "0bda:2832"}},
        {"airspy", {"1d50:60a1"}},
        {"airspyhf", {"03eb:800c"}},
        {"sdrplay", {"1df7:2500", "1df7:3000", "1df7:3010", "1df7:3020", "1df7:3030", "1df7:3050", "1df7:3060"}},
    };
    return ids.value(sdr);
}

void MainWindow::usbTick() {
    bool ok;
    QSet<QString> devs = usbDevices(&ok);
    if (!ok) { usbTimer_.stop(); return; }    // not Linux: nothing to watch
    if (!devs.isEmpty()) usbSeen_ = true;
    bool lost = usbSeen_ && devs.isEmpty();
    if (lost && !usbLost_)
        Notices::get().add("USB", "Every USB device disappeared at once: the USB system has failed -- reboot (check cables / power)");
    usbLost_ = lost;
    QSettings& s = settings();
    for (int side = 0; side < 2; ++side) {
        UsbWatch& st = usb_[side];
        const bool isTx = side == 0;
        const QString name = isTx ? "TX" : "RX";
        // what this side uses, attached now
        QSet<QString> attached;
        QString sdr = s.value(isTx ? "tx/radio" : "rx/radio").toString();
        QStringList ids = radioUsbIds(sdr);
        for (const QString& id : ids)
            if (devs.contains(id)) { attached.insert(radioName(sdr)); break; }
        QString cam = s.value("tx/video_device").toString();
        if (isTx && s.value("tx/video").toString() == "camera" && cam.startsWith("/dev/") && QFile::exists(cam))
            attached.insert("Camera");
        const bool running = isTx ? tx_->running() : rx_->running();
        if (running) {
            st.resume.clear();
            st.backT = -1;
            if (!st.watching) { st.watch = attached; st.watching = true; }   // only what was there at start
            QSet<QString> gone = st.watch - attached;
            if (!gone.isEmpty()) {
                QStringList g = gone.values();
                g.sort();
                const QString what = g.join(" and ");
                if (isTx) tx_->stop(); else rx_->stop();
                Notices::get().add("USB", QString("%1 disconnected from USB while %2 was running (%2 stopped; restarts when %3 back)")
                                              .arg(what, name, g.size() == 1 ? "it is" : "they are"));
                if (isTx) tx_->setStatus(what + " disconnected from USB -- TX restarts when it is back");
                else rx_->setStatus(what + " disconnected from USB -- RX restarts when it is back");
                st.watching = false;
                st.resume = gone;
            }
        } else {
            st.watching = false;
            if (!st.resume.isEmpty()) {
                if (attached.contains(st.resume)) {
                    if (!usbClock_.isValid()) usbClock_.start();
                    if (st.backT < 0) st.backT = usbClock_.elapsed();
                    if (usbClock_.elapsed() - st.backT >= USB_RESUME_S * 1000) {
                        st.resume.clear();
                        st.backT = -1;
                        if (isTx) tx_->start(); else rx_->start();
                    }
                } else {
                    st.backT = -1;
                }
            }
        }
    }
}

void MainWindow::showView(const QString& v) {
    pages_->setCurrentIndex(v == "config" ? 0 : v == "tx" ? 1 : 2);
    view_->setStyleSheet(accentStylesheet(v == "config" ? CONFIG_ACCENT : v == "tx" ? TX_ACCENT : RX_ACCENT));
    settings().setValue("view", v);
}

// what's running: the tabs themselves -- TX red while transmitting, RX green
// while receiving (the bar's text summary is gone; indicator_ just spaces the bar)
void MainWindow::updateIndicator() {
    auto lit = [](QPushButton* b, bool on, const char* colour) {
        b->setStyleSheet(on ? QString("QPushButton { background: %1; color: white; font-weight: bold; }").arg(colour)
                            : QString());
    };
    lit(view_->button("tx"), tx_->running(), STOP);
    lit(view_->button("rx"), rx_->running(), GO);
    indicator_->clear();
}

// whole-machine CPU use
void MainWindow::updateCpu() {
    unsigned long long idle = 0, total = 0;
#ifdef Q_OS_WIN
    FILETIME fi, fk, fu;
    if (!GetSystemTimes(&fi, &fk, &fu)) return;
    auto v = [](const FILETIME& f) { return ((unsigned long long)f.dwHighDateTime << 32) | f.dwLowDateTime; };
    idle = v(fi);
    total = v(fk) + v(fu);       // kernel time includes idle
#else
    QFile f("/proc/stat");
    if (!f.open(QIODevice::ReadOnly)) return;
    QList<QByteArray> p = f.readLine().simplified().split(' ');
    for (int i = 1; i < p.size(); ++i) total += p[i].toULongLong();
    if (p.size() > 5) idle = p[4].toULongLong() + p[5].toULongLong();
#endif
    if (lastTotal_ && total > lastTotal_) {
        double busy = 1.0 - double(idle - lastIdle_) / double(total - lastTotal_);
        cpu_->setText(QString("CPU %1%").arg(qRound(busy * 100)));
    }
    lastIdle_ = idle;
    lastTotal_ = total;
}

// "AVM v1.0.21": the AVM folder's VERSION file ("1.0.21", as the repository's);
// an older folder without one: avm_version.py's BUILD, read as text
static QString shortTitle() {
    QFile v(avmDir() + "/VERSION");
    if (v.open(QIODevice::ReadOnly)) {
        auto m = QRegularExpression(R"(^\s*(\d+\.\d+\.\d+))").match(QString::fromUtf8(v.readAll()));
        if (m.hasMatch()) return "AVM v" + m.captured(1);
    }
    QFile f(avmDir() + "/avm_version.py");
    if (f.open(QIODevice::ReadOnly)) {
        auto m = QRegularExpression(R"(^BUILD\s*=\s*(\d+))", QRegularExpression::MultilineOption).match(QString::fromUtf8(f.readAll()));
        if (m.hasMatch()) return QString("AVM v1.0.%1").arg(m.captured(1));
    }
    return "AVM";
}

void MainWindow::startWeb() {
    WebServer::Hooks h;
    h.kiwi = [] { return settings().value("kiwi_server").toString().trimmed(); };
    h.setKiwi = [this](const QString& s) { config_->setKiwi(s); };
    h.image = [] { return settings().value("tx/image").toString(); };
    h.setImage = [this](const QString& p) { config_->setImage(p); };
    h.screenshot = [this] {
        QByteArray png;
        QBuffer b(&png);
        b.open(QIODevice::WriteOnly);
        grab().save(&b, "PNG");
        return png;
    };
    new WebServer(shortTitle(), h, this);
}

// rich text with the logo's colours on A, V and M (each once), the rest in rest
static QString logoHtml(const QString& text, const char* rest) {
    static const QMap<QChar, QString> colours = {{'A', "#e53935"}, {'V', "#43a047"}, {'M', "#1e88e5"}};
    QString out;
    QSet<QChar> done;
    for (QChar c : text) {
        if (colours.contains(c) && !done.contains(c)) {
            done.insert(c);
            out += QString("<span style='color:%1'>%2</span>").arg(colours[c], QString(c));
        } else {
            out += QString("<span style='color:%1'>%2</span>").arg(rest, QString(c).toHtmlEscaped());
        }
    }
    return out;
}

void MainWindow::buildSplash() {
    splash_ = new QWidget(this);
    splash_->setObjectName("splash");
    splash_->setStyleSheet(QString("QWidget#splash { background: %1; }").arg(BG));
    auto* lay = new QVBoxLayout(splash_);
    lay->setContentsMargins(ui.px(16), 0, ui.px(16), ui.px(10));
    lay->addStretch(2);
    auto* title = new QLabel(logoHtml("AudioVideoModem", TEXT));
    title->setAlignment(Qt::AlignCenter);
    title->setStyleSheet(QString("font-size: %1px; font-weight: bold; background: transparent;").arg(ui.px(52)));
    lay->addWidget(title);
    lay->addSpacing(ui.px(18));
    auto* status = new QLabel("Starting...");
    status->setAlignment(Qt::AlignCenter);
    status->setStyleSheet(QString("color: %1; background: transparent;").arg(TEXT_DIM));
    lay->addWidget(status);
    lay->addStretch(3);
    auto* credit = new QLabel(logoHtml(shortTitle(), TEXT_DIM) +
                              QString("<span style='color:%1'>  \u00b7  by M0DTS and AI!</span>").arg(TEXT_DIM));
    credit->setAlignment(Qt::AlignRight);
    credit->setStyleSheet(QString("font-size: %1px; background: transparent;").arg(ui.px(12)));
    lay->addWidget(credit);
    splash_->setGeometry(rect());
    splash_->installEventFilter(this);
    splash_->show();
    splash_->raise();
    // the start-up check (the Python GUI's codec warm-up: nothing to compile here,
    // but a modem library that won't load is better said now than at the first start)
    QTimer::singleShot(50, this, [status = QPointer<QLabel>(status)] {
        if (status) status->setText("Loading the modem library...");
        QLibrary lib(avmDir() + "/c++/avm");
        if (!lib.load()) {
            Notices::get().add("Start", "The AVM modem library didn't load: " + lib.errorString());
            if (status) status->setText("The modem library didn't load -- see the warnings (top bar)");
        } else {
            if (status) status->setText("Ready");
        }
    });
    QTimer::singleShot(1500, this, [this] { if (splash_) { splash_->deleteLater(); splash_ = nullptr; } });
}

bool MainWindow::eventFilter(QObject* o, QEvent* e) {
    if (o == splash_ && e->type() == QEvent::MouseButtonRelease) {     // a tap: straight in
        splash_->deleteLater();
        splash_ = nullptr;
        return true;
    }
    return QWidget::eventFilter(o, e);
}

void MainWindow::resizeEvent(QResizeEvent* e) {
    QWidget::resizeEvent(e);
    if (splash_) splash_->setGeometry(rect());
}

void MainWindow::closeEvent(QCloseEvent* e) {
    e->accept();
    qApp->quit();
}

}  // namespace avm
