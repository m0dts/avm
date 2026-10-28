// main.cpp -- avm_gui: AVM's GUI in C++/Qt (touch_gui.py's layout).
//
//   avm_gui [--touch | --desktop] [--demo] [--avm-dir DIR]
//
// --touch: the Pi's touchscreen look -- fullscreen, everything scaled from
// an 800x480 screen (the default on ARM Linux). --desktop: a normal
// resizable window with mouse-sized controls (the default elsewhere).
// --demo: a moving test picture and fake lock events on the RX page.
// --media-test FILE SECONDS [camera]: no window -- the C++ media chain
// (media_tx.h: test pattern + tone, or the camera + microphone set in
// Config) framed into FILE for SECONDS, for checking it decodes.
#include <QApplication>
#include <QDateTime>
#include <QDir>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QSettings>
#include <QLibrary>
#include <QListWidget>
#include <QMouseEvent>
#include <QPainter>
#include <QPointer>
#include <QVariantAnimation>
#include <QThread>
#include <QTimer>
#include <cstdio>
#include <QScreen>
#include <QStringList>

#include "mainwindow.h"
#include "link_plan.h"
#include "media_tx.h"
#include "recorder.h"
#include "pages.h"
#include "style.h"

// A ring that grows and fades for 1/4 s where the screen was pressed, so a
// press on the touchscreen shows where it landed. Drawn as a child of the
// pressed widget's window (dialogs included), transparent to the mouse.
class TouchMark : public QWidget {
public:
    TouchMark(QWidget* win, QPoint at) : QWidget(win) {
        int d = (int)(64 * avm::ui.scale);
        setAttribute(Qt::WA_TransparentForMouseEvents);
        setAttribute(Qt::WA_NoSystemBackground);
        setGeometry(at.x() - d / 2, at.y() - d / 2, d, d);
        auto* a = new QVariantAnimation(this);
        a->setDuration(250);
        a->setStartValue(0.0);
        a->setEndValue(1.0);
        connect(a, &QVariantAnimation::valueChanged, this, [this](const QVariant& v) { t_ = v.toDouble(); update(); });
        connect(a, &QVariantAnimation::finished, this, &QObject::deleteLater);
        show();
        raise();
        a->start();
    }
protected:
    void paintEvent(QPaintEvent*) override {
        QPainter p(this);
        p.setRenderHint(QPainter::Antialiasing);
        double r = width() / 2.0 * (0.35 + 0.6 * t_);
        int alpha = (int)(200 * (1.0 - t_));
        p.setPen(QPen(QColor(255, 255, 255, alpha), 3 * avm::ui.scale));
        p.setBrush(QColor(255, 255, 255, alpha / 3));
        p.drawEllipse(QPointF(width() / 2.0, height() / 2.0), r, r);
    }
private:
    double t_ = 0;
};

class TouchMarker : public QObject {
protected:
    bool eventFilter(QObject* o, QEvent* e) override {
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
        // AVM_TOUCH_LOG=1: the window's raw presses, moves, releases and touches
        static const bool tlog = qEnvironmentVariableIntValue("AVM_TOUCH_LOG") != 0;
        if (tlog && o->isWindowType()) {
            const char* n = nullptr;
            switch (e->type()) {
            case QEvent::MouseButtonPress: n = "press"; break;
            case QEvent::MouseButtonRelease: n = "release"; break;
            case QEvent::MouseMove: n = "move"; break;
            case QEvent::TouchBegin: n = "touch-begin"; break;
            case QEvent::TouchEnd: n = "touch-end"; break;
            case QEvent::Enter: n = "enter"; break;
            case QEvent::Leave: n = "leave"; break;
            default: break;
            }
            if (n) {
                QString pos;
                if (auto* te = dynamic_cast<QTouchEvent*>(e)) {
                    if (!te->points().isEmpty())
                        pos = QString(" %1,%2").arg(te->points()[0].globalPosition().x()).arg(te->points()[0].globalPosition().y());
                } else if (auto* pe = dynamic_cast<QSinglePointEvent*>(e)) {
                    pos = QString(" %1,%2 touchdev=%3").arg(pe->globalPosition().x()).arg(pe->globalPosition().y())
                              .arg(pe->pointingDevice() && pe->pointingDevice()->type() == QInputDevice::DeviceType::TouchScreen);
                }
                fprintf(stderr, "[touch] %lld %s%s\n", (long long)(QDateTime::currentMSecsSinceEpoch() % 100000), n,
                        qPrintable(pos));
            }
        }
#endif
        if (e->type() == QEvent::MouseButtonPress && o->isWidgetType()) {
            auto* me = static_cast<QMouseEvent*>(e);
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
            QPoint g = me->globalPosition().toPoint();
#else
            QPoint g = me->globalPos();
#endif
            // a press the widget ignores reaches its parent too: mark once
            qint64 now = clock_.isValid() ? clock_.elapsed() : 0;
            if (!clock_.isValid()) clock_.start();
            if (g != last_ || now - lastT_ > 100) {
                last_ = g;
                lastT_ = now;
                QWidget* win = static_cast<QWidget*>(o)->window();
                new TouchMark(win, win->mapFromGlobal(g));
            }
        }
        return false;
    }
private:
    QElapsedTimer clock_;
    QPoint last_{-1, -1};
    qint64 lastT_ = -1000;
};

// Once, on the first start after an update from the Python AVM: its settings
// (~/.config/hfmodem/touch.json) carried over -- callsign, frequencies,
// radios, modes, gains... Only into settings never set up here (no RX
// frequency yet); either way it's marked done and not looked at again. Left
// out: the microphone (ALSA names there, PipeWire / Pulse names here) and the
// spectrum's reference level (Auto only now).
static void importPythonSettings() {
    QSettings& s = avm::settings();
    if (s.value("imported_python", false).toBool()) return;
    QFile f(QDir::homePath() + "/.config/hfmodem/touch.json");
    if (!f.open(QIODevice::ReadOnly)) return;
    s.setValue("imported_python", true);
    if (s.contains("rx/freq_hz") || s.contains("tx/freq_hz")) return;     // set up here already
    const QJsonObject o = QJsonDocument::fromJson(f.readAll()).object();
    if (o.isEmpty()) return;
    // touch.json key -> this GUI's
    static const QList<QPair<const char*, const char*>> map = {
        {"tx_callsign", "callsign"},  {"mode_type", "mode_type"},   {"kiwi_server", "kiwi_server"},
        {"duplex", "duplex"},         {"experimental", "experimental"}, {"view", "view"},
        {"tx_gain", "tx_gain"},       {"rx_gain", "rx_gain"},       {"rx_audio", "audio_device"},
        {"tx_freq", "tx/freq_hz"},    {"tx_mode", "tx/mode"},       {"tx_bw", "tx/bw"},
        {"tx_mod", "tx/mod"},         {"tx_sdr", "tx/radio"},       {"tx_pluto_uri", "tx/uri"},
        {"tx_lime_port", "tx/port"},  {"tx_source", "tx/video"},    {"tx_res", "tx/resolution"},
        {"tx_fps", "tx/fps"},         {"tx_camera", "tx/video_device"}, {"tx_audio", "tx/audio_codec"},
        {"tx_content", "tx/content"}, {"tx_mono", "tx/mono"},       {"tx_papr", "tx/papr_db"},
        {"tx_focus", "tx/focus"},     {"tx_focus_mode", "tx/focus_mode"},
        {"rx_freq", "rx/freq_hz"},    {"rx_mode", "rx/mode"},       {"rx_bw", "rx/bw"},
        {"rx_mod", "rx/mod"},         {"rx_sdr", "rx/radio"},       {"rx_pluto_uri", "rx/uri"},
        {"rx_lime_port", "rx/port"},  {"rx_rtl_ppm", "rx/ppm"},     {"rx_offset_khz", "rx/offset_khz"},
        {"rx_spec_avg", "rx/spec_avg"}, {"rx_invert", "rx/invert"}};
    int n = 0;
    for (const auto& kv : map) {
        QJsonValue v = o.value(kv.first);
        if (v.isUndefined() || v.isNull()) continue;
        s.setValue(kv.second, v.toVariant());
        ++n;
    }
    s.sync();
    std::fprintf(stderr, "settings: %d carried over from the Python AVM (%s)\n", n, qPrintable(f.fileName()));
}

static int media_test(const QString& path, double seconds, bool camera) {
    QFile f(path);
    if (!f.open(QIODevice::WriteOnly)) return 1;
    QSettings& s = avm::settings();
    avm::MediaTxConfig c;
    c.station_id = "TEST";
    if (camera) {
        c.video = "camera";
        c.audio = "mic";
        c.video_device = s.value("tx/video_device").toString().toStdString();
        c.audio_device = s.value("tx/audio_device").toString().toStdString();
#ifdef _WIN32
        const char* fmt = "dshow";
#else
        const char* fmt = "v4l2";
#endif
        for (const auto& d : avm::Capture::devices(fmt, true)) std::fprintf(stderr, "camera: %s\n", d.c_str());
        for (const auto& d : avm::Capture::devices(fmt, false)) std::fprintf(stderr, "microphone: %s\n", d.c_str());
        if (c.audio_device.empty()) {    // none set in Config: the first listed
            auto mics = avm::Capture::devices(fmt, false);
            if (!mics.empty()) c.audio_device = mics[0];
        }
    }
    avm::MediaTx tx;
    std::mutex mu;
    if (qApp->arguments().contains("--opus")) c.audio_codec = "opus";
    int pi = qApp->arguments().indexOf("--link");    // MODE,KHZ,MOD: the automatic plan's settings
    if (pi >= 0 && pi + 1 < qApp->arguments().size()) {
        QStringList l = qApp->arguments()[pi + 1].split(',');
        QLibrary lib(avm::avmDir() + "/c++/avm");
        auto fn = (double (*)(const char*, double, const char*, const char*, int, double))lib.resolve("avm_link_bitrate");
        avm::LinkPlan p = avm::planLink(fn ? fn(l.value(0).toUtf8(), l.value(1).toDouble(), l.value(2).toUtf8(), "ldpc", 1024, 0) : 0,
                                        1024, l.value(1).toDouble(), true);
        c.audio_codec = p.audio_codec.toStdString();
        c.opus_kbps = p.opus_kbps;
        c.width = p.width;
        c.height = p.height;
        c.fps = p.fps;
        c.frame_bytes = p.frame_bytes;
        c.fragment_period_s = p.fragment_s;
        std::fprintf(stderr, "%s\n", qPrintable(p.summary()));
    }
    int ii = qApp->arguments().indexOf("--image");
    if (ii >= 0 && ii + 1 < qApp->arguments().size()) { c.video = "image"; c.image_path = qApp->arguments()[ii + 1].toStdString(); }
    int mi = qApp->arguments().indexOf("--message");
    if (mi >= 0 && mi + 1 < qApp->arguments().size()) tx.setMessage(qApp->arguments()[mi + 1]);
    QImage last;      // the TX preview: the last picture saved beside FILE
    tx.setPreview([&](const QImage& img) { std::lock_guard<std::mutex> g(mu); last = img; });
    long long n = 0;
    bool ok = tx.start(c, [&](const std::vector<uint8_t>& frag) {
        std::lock_guard<std::mutex> g(mu);
        f.write((const char*)frag.data(), (qint64)frag.size());
        ++n;
    }, [](const std::string& l) { std::fprintf(stderr, "%s\n", l.c_str()); });
    if (!ok) return 1;
    QThread::msleep((unsigned long)(seconds * 1000));
    tx.stop();
    std::fprintf(stderr, "media-test: %lld fragments\n", n);
    if (!last.isNull() && last.save(path + ".preview.png")) std::fprintf(stderr, "preview: %s.preview.png\n", qPrintable(path));
    return 0;
}

int main(int argc, char** argv) {
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    QApplication::setAttribute(Qt::AA_EnableHighDpiScaling);
#endif
    QApplication app(argc, argv);
    app.setApplicationName("avm_gui");
    {   // the Windows release: SoapySDR's radio modules beside the program (its
        // DLL is found there anyway), not wherever SoapySDR was built to look
        QString mods = QCoreApplication::applicationDirPath() + "/SoapySDR/modules0.8";
        if (QDir(mods).exists() && qEnvironmentVariableIsEmpty("SOAPY_SDR_PLUGIN_PATH"))
            qputenv("SOAPY_SDR_PLUGIN_PATH", QDir::toNativeSeparators(mods).toLocal8Bit());
    }
    QStringList a = app.arguments();
    {   // before anything reads the settings
        int k = a.indexOf("--settings-file");
        if (k >= 0 && k + 1 < a.size()) avm::setSettingsFile(a[k + 1]);
        else if (!a.contains("--screenshots") && !a.contains("--plan")) importPythonSettings();
        avm::setVideoSmooth(avm::settings().value("tx/video_smooth", false).toBool());   // (link_plan.h)
        k = a.indexOf("--log");
        if (k >= 0 && k + 1 < a.size()) avm::setLogFile(a[k + 1]);
        else if (!a.contains("--screenshots") && !a.contains("--plan")) {
            // always a session log (the modems' lines, the audio buffer each second), as the Python GUI's
            QDir().mkpath(avm::avmDir() + "/gui_logs");
            avm::setLogFile(avm::avmDir() + "/gui_logs/cpp_session.log");
        }
    }
    bool touch =
#if defined(__aarch64__) || defined(__arm__)
        true;
#else
        false;
#endif
    if (a.contains("--touch")) touch = true;
    if (a.contains("--desktop")) touch = false;
    int i = a.indexOf("--avm-dir");
    if (i >= 0 && i + 1 < a.size()) avm::settings().setValue("avm_dir", a[i + 1]);
    i = a.indexOf("--record-test");
    if (i >= 0 && i + 2 < a.size()) {   // IN (a framed stream, --media-test's) -> the player -> OUT.mp4
        QFile in(a[i + 1]);
        if (!in.open(QIODevice::ReadOnly)) return 1;
        avm::MediaDecoder dec;
        avm::Recorder rec;
        QString out = a[i + 2];
        dec.audio().tap = [&](const float* p, size_t n) { rec.sound(p, n); };
        QObject::connect(&dec, &avm::MediaDecoder::frame, [&](const QImage& img) {
            QString err;
            if (!rec.recording() && !rec.start(out, img.width(), img.height(), &err))
                std::fprintf(stderr, "record: %s\n", qPrintable(err));
            rec.picture(img);
        });
        dec.start(1024);
        {   // open now (an audio-only stream has no picture to size it by): pictures are scaled to it
            QString err;
            if (!rec.start(out, 256, 144, &err)) std::fprintf(stderr, "record: %s\n", qPrintable(err));
        }
        while (!in.atEnd()) {
            dec.feed(in.read(1024));
            for (int k = 0; k < 48; ++k) { QThread::msleep(5); qApp->processEvents(); }   // the fragments at their pace (the display timer runs)
        }
        rec.stop();
        dec.stop();
        std::fprintf(stderr, "record-test: %s\n", qPrintable(out));
        return 0;
    }
    if (a.contains("--plan")) {    // the link plans for a range of links (link_plan.h), for checking
        QLibrary lib(avm::avmDir() + "/c++/avm");
        auto fn = (double (*)(const char*, double, const char*, const char*, int, double))lib.resolve("avm_link_bitrate");
        if (!fn) return 1;
        const char* links[][3] = {{"SH", "10", "qpsk"}, {"SH", "10", "8psk"}, {"SH", "10", "16psk"}, {"D", "10", "qpsk"}, {"A", "10", "qpsk"}, {"A", "20", "qpsk"}, {"B", "20", "16qam"},
                                  {"A", "40", "qpsk"}, {"B", "80", "16qam"}, {"VU", "160", "qpsk"}, {"S", "20", "8psk"},
                                  {"SH", "40", "16psk"}, {"VU", "250", "16qam"}};
        for (auto& l : links) {
            double bps = fn(l[0], atof(l[1]), l[2], "ldpc", 1024, 0);
            avm::LinkPlan p = avm::planLink(bps, 1024, atof(l[1]), true);
            std::fprintf(stderr, "%-3s %4s kHz %-6s %s, %d B/frame, tick %.3f s\n", l[0], l[1], l[2],
                         qPrintable(p.summary()), p.frame_bytes, p.fragment_s);
        }
        return 0;
    }
    i = a.indexOf("--media-test");
    if (i >= 0 && i + 2 < a.size()) return media_test(a[i + 1], a[i + 2].toDouble(), a.contains("camera"));

    // --screenshots DIR: each page at the Pi's 800x480 touch size, saved (no window shown)
    i = a.indexOf("--screenshots");
    QString shots = i >= 0 && i + 1 < a.size() ? a[i + 1] : QString();
    if (!shots.isEmpty()) touch = true;
    avm::ui.touch = touch;
    QRect screen = app.primaryScreen()->geometry();
    avm::ui.scale = touch ? screen.height() / 480.0 : 1.0;
    if (!shots.isEmpty()) avm::ui.scale = 1.0;
    app.setStyleSheet(avm::stylesheet(avm::RX_ACCENT));

    avm::settings().remove("tx/message");   // the TX message lasts one session: none until one is set
    avm::MainWindow w(a.contains("--demo"));
    TouchMarker marker;
    if (shots.isEmpty()) app.installEventFilter(&marker);
    if (!shots.isEmpty()) {
        w.setAttribute(Qt::WA_DontShowOnScreen);
        w.resize(800, 480);
        w.show();
        for (const char* v : {"rx", "tx", "config"}) {
            w.setView(v);
            for (int k = 0; k < 5; ++k) app.processEvents();
            w.grab().save(shots + "/" + v + ".png");
        }
        if (auto* sections = w.findChild<QListWidget*>()) {     // and each of Config's sections
            w.setView("config");
            for (int r = 1; r < sections->count(); ++r) {
                sections->setCurrentRow(r);
                for (int k = 0; k < 5; ++k) app.processEvents();
                w.grab().save(shots + "/config_" + sections->item(r)->text().toLower().replace(' ', '_') + ".png");
            }
            sections->setCurrentRow(0);
        }
        w.setView("rx");   // and the RX picture full window (a tap on it)
        QMetaObject::invokeMethod(w.rx(), "toggleBigVideo");
        for (int k = 0; k < 5; ++k) app.processEvents();
        w.grab().save(shots + "/rx_full.png");
        for (auto pk : QList<QPair<const char*, QStringList>>{     // and the pickers' button grid
                 {"picker_fps", {"1", "2", "5", "10", "15", "20", "25", "30"}},
                 {"picker_res", {"320x180", "480x270", "640x360", "960x540", "1280x720"}},
                 {"picker_dev", {"HD Pro Webcam C920 (usb-0000:01:00.0-1.2)", "USB Camera (usb-0000:01:00.0-1.3)"}}}) {
            avm::TouchListDialog d("Choose", pk.second, pk.second[1], &w);
            d.setAttribute(Qt::WA_DontShowOnScreen);
            d.show();
            for (int k = 0; k < 5; ++k) app.processEvents();
            d.grab().save(shots + "/" + pk.first + ".png");
        }
        w.buildSplash();      // and the start screen
        for (int k = 0; k < 5; ++k) app.processEvents();
        w.grab().save(shots + "/splash.png");
        return 0;
    }
    if (touch) {
        w.setWindowFlag(Qt::FramelessWindowHint);
        w.showFullScreen();
    } else {
        w.resize(1000, 600);
        w.show();
    }
    // --auto rx,tx [--quit-after S]: the buttons pressed by script (a trial run):
    // RX first, TX 3 s later, recording from 8 s; at the end TX then RX stopped
    i = a.indexOf("--auto");
    if (i >= 0 && i + 1 < a.size()) {
        QStringList what = a[i + 1].split(',');
        int q = a.indexOf("--quit-after");
        double quit_s = q >= 0 && q + 1 < a.size() ? a[q + 1].toDouble() : 0;
        if (what.contains("rx")) QTimer::singleShot(500, &w, [&w] { avm::guiLog("GUI", "auto: RX start"); w.rx()->start(); });
        if (what.contains("tx")) QTimer::singleShot(3000, &w, [&w] { avm::guiLog("GUI", "auto: TX start"); w.tx()->start(); });
        if (what.contains("rec")) QTimer::singleShot(8000, &w, [&w] { avm::guiLog("GUI", "auto: record"); w.rx()->record(); });
        auto* watch = new QTimer(&w);      // the pages' status lines as they change
        QObject::connect(watch, &QTimer::timeout, &w, [&w] {
            static QString lr, lt;
            if (w.rx()->statusText() != lr) avm::guiLog("RX status", lr = w.rx()->statusText());
            if (w.tx()->statusText() != lt) avm::guiLog("TX status", lt = w.tx()->statusText());
        });
        watch->start(200);
        int si = a.indexOf("--shot");      // a screenshot 1 s before the end (the live view)
        if (si >= 0 && si + 1 < a.size() && quit_s > 1) {
            QString shotFile = a[si + 1];
            QTimer::singleShot((int)((quit_s - 1) * 1000), &w, [&w, shotFile] { w.setView("rx"); w.grab().save(shotFile); });
        }
        if (quit_s > 0)
            QTimer::singleShot((int)(quit_s * 1000), &w, [&w, &app] {
                avm::guiLog("GUI", "auto: stop");
                w.tx()->stop();
                QTimer::singleShot(2500, &w, [&w, &app] {
                    w.rx()->stop();
                    avm::guiLog("GUI", QString("auto: done -- %1 pictures, %2 messages").arg(w.rx()->pictures()).arg(w.rx()->messages()));
                    QTimer::singleShot(500, &app, &QApplication::quit);
                });
            });
    }
    return app.exec();
}
