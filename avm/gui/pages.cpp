#include "pages.h"

#include "capture.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QLibrary>
#include <cstdio>
#include <vector>
#include <QFileInfo>
#include <QFormLayout>
#include <QGridLayout>
#include <QHBoxLayout>
#include <QListWidget>
#include <QPainter>
#include <QRegularExpression>
#include <QStackedWidget>
#include <QComboBox>
#include <QDateTime>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTime>
#include <QCheckBox>
#include <QDialog>
#include <QMessageBox>
#include <QScrollArea>
#include <QScroller>
#include <QFileDialog>
#include <QInputDialog>
#include <QSlider>
#include <QStandardPaths>
#include <QVBoxLayout>
#include <QMouseEvent>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstring>

#include "style.h"

namespace avm {

// ---------------------------------------------------------------- settings / paths
static QString g_settings_file;

void setSettingsFile(const QString& path) { g_settings_file = path; }

QSettings& settings() {
    static QSettings* s = g_settings_file.isEmpty() ? new QSettings("hfmodem", "avm_gui")
                                                    : new QSettings(g_settings_file, QSettings::IniFormat);
    return *s;
}

static QFile* g_log = nullptr;

void setLogFile(const QString& path) {
    g_log = new QFile(path);
    if (!g_log->open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) { delete g_log; g_log = nullptr; }
}

void guiLog(const QString& who, const QString& line) {
    if (!g_log || line.startsWith("[spectrum]") || line.startsWith("[constellation]")) return;
    g_log->write(QString("%1 %2: %3\n").arg(QTime::currentTime().toString("HH:mm:ss.zzz"), who, line).toUtf8());
    g_log->flush();
}

// The AVM programs: --avm-dir / settings, else the folder above this
// program (c:\avm\gui\build -> c:\avm), else the current directory.
// (the folder that holds the library: c++/libavm.so or c++/avm.dll)
static bool hasAvmLibrary(const QString& d) {
    return QFileInfo::exists(d + "/c++/libavm.so") || QFileInfo::exists(d + "/c++/avm.dll") ||
           QFileInfo::exists(d + "/c++/libavm.dylib");
}
QString avmDir() {
    QString d = settings().value("avm_dir").toString();
    if (!d.isEmpty() && hasAvmLibrary(d)) return d;
    QDir here(QCoreApplication::applicationDirPath());
    for (int i = 0; i < 4; ++i) {
        if (hasAvmLibrary(here.absolutePath())) return here.absolutePath();
        if (!here.cdUp()) break;
    }
    return QDir::currentPath();
}

// the live RX gain file (the receivers' --gain-file)
static QString rxGainFile() { return QDir::tempPath() + "/avm_gui_rx_gain.txt"; }
// the live RX offset (Hz; the receivers' --freq-offset-file)
static QString rxOffsetFile() { return QDir::tempPath() + "/avm_gui_rx_offset.txt"; }
static void writeRxOffset(double khz) {
    QFile f(rxOffsetFile());
    if (f.open(QIODevice::WriteOnly | QIODevice::Truncate)) f.write(QByteArray::number(khz * 1000, 'f', 0) + "\n");
}
// ---- duplex (touch_widgets.tdd_config): this station transmits in a slot once
// a cycle and listens the rest of the time; the master keeps its timing, the
// slave puts its slot half a cycle later. Tested (Python) at VU 160 kHz 16QAM
// between two Pi 4 + Pluto stations.
static const double TDD_CYCLE_S = 1.0;
static const double TDD_SLOT_S = 0.47;         // VU 160 kHz 16QAM: 9 fragments (450 ms), a gap each side
static const double TDD_PLAYER_LATENCY_S = 1.0; // audio/video arrive in bursts a cycle apart

QString duplexWhyNot() {
    QSettings& s = settings();
    if (s.value("tx/mode").toString() != "VU" || s.value("rx/mode").toString() != "VU") return "needs mode VU on TX and RX";
    if (s.value("tx/bw").toString() != s.value("rx/bw").toString() || s.value("tx/mod").toString() != s.value("rx/mod").toString())
        return "needs TX and RX set alike (kHz and modulation)";
    if (s.value("tx/radio").toString() != "pluto" || s.value("rx/radio").toString() != "pluto")
        return "needs a PlutoSDR for TX and RX";
    return QString();
}

QString duplexRole() {
    QSettings& s = settings();
    QString r = s.value("duplex", "off").toString();
    if (!s.value("experimental", false).toBool() || (r != "master" && r != "slave") || !duplexWhyNot().isEmpty()) return QString();
    return r;
}

// the slave's slot-adjust file, the master's report file (/dev/shm where there is one)
static QString tddFile(const QString& name) {
    QString dir = QDir("/dev/shm").exists() ? QString("/dev/shm") : QDir::tempPath();
    return dir + "/" + name;
}

// both engines' TDD options for this station
static QStringList tddArgs() {
    const bool slave = duplexRole() == "slave";
    QStringList a = {"--tdd-cycle-s", QString::number(TDD_CYCLE_S), "--tdd-slot-s", QString::number(TDD_SLOT_S),
                     "--tdd-station", slave ? "1" : "0"};
    if (slave) a << "--tdd-adjust-file" << tddFile("avm_tdd_adjust");
    else a << "--tdd-report-file" << tddFile("avm_tdd_report");   // where the master hears the slave, sent to it
    return a;
}

// a PlutoSDR on USB: duplex drives TX and RX through one connection, and over
// its network link (one socket) a waiting RX read held up TX writes
static bool plutoOnUsb() {
    QDir base("/sys/bus/usb/devices");
    for (const QString& d : base.entryList(QDir::Dirs | QDir::NoDotAndDotDot)) {
        QFile v(base.filePath(d + "/idVendor")), p(base.filePath(d + "/idProduct"));
        if (v.open(QIODevice::ReadOnly) && p.open(QIODevice::ReadOnly) && v.readAll().trimmed() == "0456" &&
            p.readAll().trimmed() == "b673")
            return true;
    }
    return false;
}

// the offset's step per mode type (touch_widgets.RX_OFFSET_STEP_KHZ)
static double offsetStepKhz() {
    QString t = settings().value("mode_type", "hf").toString();
    return t == "vu" ? 0.5 : t == "sat" ? 1.0 : 0.1;
}

static QString cppProgram(const QString& name) {
#ifdef Q_OS_WIN
    return avmDir() + "/c++/" + name + ".exe";
#else
    return avmDir() + "/c++/" + name;
#endif
}

// ---------------------------------------------------------------- Link
Link::Link(QObject* parent) : QObject(parent) {
    connect(this, &Link::line, this, [this](const QString& l) {
        if (!source.isEmpty()) Notices::get().addIfError(source, l);
    });
    proc_.setProcessChannelMode(QProcess::SeparateChannels);
    connect(&proc_, &QProcess::readyReadStandardError, this, [this] {
        partial_ += proc_.readAllStandardError();
        int nl;
        while ((nl = partial_.indexOf('\n')) >= 0) {
            QString l = QString::fromUtf8(partial_.left(nl)).trimmed();
            partial_.remove(0, nl + 1);
            if (!l.isEmpty()) emit line(l);
        }
    });
    connect(&proc_, &QProcess::readyReadStandardOutput, this, [this] { emit data(proc_.readAllStandardOutput()); });
    connect(&proc_, &QProcess::stateChanged, this,
            [this](QProcess::ProcessState s) { emit stateChanged(s != QProcess::NotRunning); });
    connect(&proc_, &QProcess::errorOccurred, this, [this](QProcess::ProcessError) {
        if (!stopping_) emit line("ERROR: " + proc_.errorString());   // (not the stop we asked for)
    });
}

void Link::start(const QString& program, const QStringList& args, const QString& dir, const QString& stdin_file) {
    if (running()) return;
    partial_.clear();
    stopping_ = false;
    if (stdin_file != "|") proc_.setStandardInputFile(stdin_file.isEmpty() ? QProcess::nullDevice() : stdin_file);
    proc_.setWorkingDirectory(dir);
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert("PYTHONUNBUFFERED", "1");
#ifdef Q_OS_WIN
    // SoapySDR and its radio modules (avm_rx's --input pluto): AVM's environment
    QString lib = QDir::toNativeSeparators(QDir::homePath() + "/AppData/Local/AVM/env/Library/bin");
    if (QFileInfo::exists(lib)) env.insert("PATH", lib + ";" + env.value("PATH"));
#endif
    proc_.setProcessEnvironment(env);
    emit line(QString("Starting %1 %2").arg(QFileInfo(program).fileName(), args.join(' ')));
    proc_.start(program, args);
}

// ---- the in-process receiver (libavm: c++/rx_engine.h)
namespace {
typedef struct {
    void (*data)(void*, const unsigned char*, size_t);
    void (*log)(void*, const char*);
    void (*done)(void*);
    void* user;
} EngineCallbacks;
}  // namespace

// an engine's C API in libavm (rx_engine.h / tx_engine.h: the same shape)
struct EngineApi {
    void* (*create)(int, const char* const*, const EngineCallbacks*) = nullptr;
    void (*push)(void*, const void*, size_t) = nullptr;
    void (*end_input)(void*) = nullptr;
    void (*set_gain)(void*, double) = nullptr;
    void (*destroy)(void*) = nullptr;
    QString error;
    bool ok() const { return create && push && end_input && set_gain && destroy; }
};

static EngineApi& engineApi(bool tx) {
    static EngineApi apis[2];
    static bool tried[2] = {false, false};
    EngineApi& api = apis[tx];
    if (!tried[tx]) {
        tried[tx] = true;
        static QLibrary lib(avmDir() + "/c++/avm");
        if (!lib.isLoaded() && !lib.load()) { api.error = "AVM library: " + lib.errorString(); return api; }
        QByteArray pre = tx ? "avm_tx_" : "avm_rx_";
        api.create = (decltype(api.create))lib.resolve(pre + "create");
        api.push = (decltype(api.push))lib.resolve(pre + "push");
        api.end_input = (decltype(api.end_input))lib.resolve(pre + "end_input");
        api.set_gain = (decltype(api.set_gain))lib.resolve(pre + "set_gain");
        api.destroy = (decltype(api.destroy))lib.resolve(pre + "destroy");
        if (!api.ok()) api.error = "AVM library too old -- rebuild AVM (cmake --build)";
    }
    return api;
}

Link::~Link() {
    if (startTh_.joinable()) startTh_.join();     // (a start still under way: finished, then stopped below)
    // no signals from here: at exit the window that listens (the TX/RX indicator)
    // may already be gone -- stateChanged into it crashed the GUI on closing
    blockSignals(true);
    stop();
}

void Link::startRx(const QStringList& args, const QString& iq_file) {
    iqFile_ = iq_file;
    startEngine(false, args);
}

// an IQ recording read into the receiver (once it's up)
void Link::startFile() {
    if (!iqFile_.isEmpty() && iqFile_ != "|") {
        file_stop_ = false;
        void* h = eng_;
        EngineApi* api = api_;
        std::string path = iqFile_.toStdString();
        file_th_ = std::thread([this, h, path, api] {
            if (FILE* f = std::fopen(path.c_str(), "rb")) {
                std::vector<char> buf(1 << 16);
                size_t got;
                while (!file_stop_ && (got = std::fread(buf.data(), 1, buf.size(), f)) > 0) api->push(h, buf.data(), got);
                std::fclose(f);
            }
            api->end_input(h);
        });
    }
}

void Link::startTx(const QStringList& args) { startEngine(true, args); }

bool Link::startEngine(bool tx, const QStringList& args) {
    if (running()) return false;
    EngineApi& api = engineApi(tx);
    if (!api.ok()) { emit line("ERROR: " + api.error); return false; }
#ifdef Q_OS_WIN
    // SoapySDR and its radio modules: AVM's environment, on this process's PATH
    QString lib = QDir::toNativeSeparators(QDir::homePath() + "/AppData/Local/AVM/env/Library/bin");
    QByteArray path = qgetenv("PATH");
    if (QFileInfo::exists(lib) && !path.contains(lib.toLocal8Bit())) qputenv("PATH", lib.toLocal8Bit() + ";" + path);
#endif
    std::vector<QByteArray> store;
    std::vector<const char*> argv;
    for (int i = 0; i < args.size(); ++i) {
        if (args[i] == "--gain-file") { ++i; continue; }   // in-process: setGain() instead
        store.push_back(args[i].toUtf8());
    }
    for (const QByteArray& b : store) argv.push_back(b.constData());
    // the engine's threads -> this (the GUI thread), queued
    EngineCallbacks cb;
    cb.user = this;
    cb.data = [](void* u, const unsigned char* p, size_t n) {
        Link* l = (Link*)u;
        QByteArray b((const char*)p, (int)n);
        QMetaObject::invokeMethod(l, [l, b] { emit l->data(b); }, Qt::QueuedConnection);
    };
    cb.log = [](void* u, const char* t) {
        Link* l = (Link*)u;
        QString s = QString::fromUtf8(t).trimmed();
        if (!s.isEmpty()) QMetaObject::invokeMethod(l, [l, s] { emit l->line(s); }, Qt::QueuedConnection);
    };
    cb.done = [](void* u) {
        Link* l = (Link*)u;
        QMetaObject::invokeMethod(l, [l] { l->engineFinished(); }, Qt::QueuedConnection);
    };
    emit line(QString("Starting the C++ %1 ").arg(tx ? "transmitter" : "receiver") + args.join(' '));
    // created on a worker thread: a radio takes a second or two to open
    starting_ = true;
    stopWanted_ = false;
    early_.clear();
    if (startTh_.joinable()) startTh_.join();
    EngineApi* apiP = &api;
    startTh_ = std::thread([this, apiP, store, cb] {
        std::vector<const char*> av;
        for (const QByteArray& b : store) av.push_back(b.constData());
        void* h = apiP->create((int)av.size(), av.data(), &cb);
        QMetaObject::invokeMethod(this, [this, h, apiP] { engineCreated(h, apiP); }, Qt::QueuedConnection);
    });
    return true;
}

// the worker's result, back on the GUI thread
void Link::engineCreated(void* h, EngineApi* api) {
    starting_ = false;
    if (!h) { early_.clear(); emit stateChanged(false); return; }
    if (stopWanted_) {                  // stopped while it was starting
        api->destroy(h);
        early_.clear();
        emit stateChanged(false);
        return;
    }
    eng_ = h;
    api_ = api;
    if (!early_.isEmpty()) { api_->push(eng_, early_.constData(), (size_t)early_.size()); early_.clear(); }
    emit stateChanged(true);
    startFile();
}

// the engine ended by itself (its input or radio ended)
void Link::engineFinished() {
    if (eng_) stop();
}

void Link::write(const QByteArray& bytes) {
    if (eng_) api_->push(eng_, bytes.constData(), (size_t)bytes.size());
    else if (starting_) { if (early_.size() < (64 << 20)) early_ += bytes; }     // passed on once it's up
    else if (running()) proc_.write(bytes);
}

void Link::setGain(double db) {
    if (eng_) api_->set_gain(eng_, db);
}

void Link::stop() {
    if (starting_) { stopWanted_ = true; return; }   // stopped as soon as it's up (engineCreated)
    if (eng_) {
        file_stop_ = true;
        if (file_th_.joinable()) file_th_.join();
        api_->destroy(eng_);
        eng_ = nullptr;
        emit stateChanged(false);
        return;
    }
    if (!running()) return;
    stopping_ = true;
    proc_.closeWriteChannel();
    proc_.terminate();
    if (!proc_.waitForFinished(1500)) proc_.kill();   // (stopping_ stays set: the error can come later)
}

// ---------------------------------------------------------------- helpers
static const QStringList MODES = {"A", "B", "C", "D", "SH", "VU", "S"};
static const QStringList BANDWIDTHS = {"10", "20", "40", "80", "160", "250"};
static QList<QPair<QString, QString>> pairs(const QStringList& v) {
    QList<QPair<QString, QString>> o;
    for (const auto& s : v) o.append({s, s});
    return o;
}
static const QList<QPair<QString, QString>> MODULATIONS = {
    {"qpsk", "QPSK"}, {"8psk", "8PSK"}, {"16psk", "16PSK"}, {"16qam", "16QAM"}, {"32qam", "32QAM"}};

// the TX's radio rate (its LO on the signal: +-half the width must fit)
int sdrRate(double khz) { return khz > 200 ? 800000 : 550000; }
// the RX LO offset (touch_widgets.lo_offset_hz): its DC spike clear of the RX spectrum view
double loOffsetHz(double khz) { return khz > 200 ? 190000.0 : khz > 100 ? 130000.0 : 100000.0; }

// where a signal is found and decoded, Hz from the tuned frequency (libavm's
// avm_capture_band: the signal plus its pull-in each side); false if unknown
static bool captureBand(const QString& mode, double khz, const QString& mod, double* lo, double* hi) {
    using Fn = int (*)(const char*, double, const char*, double*, double*);
    static QLibrary lib(avmDir() + "/c++/avm");
    static Fn fn = (Fn)((lib.isLoaded() || lib.load()) ? lib.resolve("avm_capture_band") : nullptr);
    return fn && fn(mode.toUtf8().constData(), khz, mod.toUtf8().constData(), lo, hi);
}

int fragmentSize(double khz) { return khz > 200 ? 2048 : 1024; }

QWidget* radioRow(FreqButton* freq, RadioPicker* radio, Picker* port) {
    auto* w = new QWidget;
    w->setObjectName("seg");
    auto* lay = new QHBoxLayout(w);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(ui.px(4));
    lay->addWidget(freq, 3);
    lay->addWidget(radio, 2);
    lay->addWidget(port, 2);
    return w;
}

// LimeSDR antenna ports (touch_widgets.LIME_PORTS); "Auto" picks by frequency
static const QStringList LIME_TX_PORTS = {"Auto", "BAND1", "BAND2"};
static const QStringList LIME_RX_PORTS = {"Auto", "LNAL", "LNAW", "LNAH"};
static const double DEFAULT_FREQ_HZ = 146.5e6;

// the radio arguments both modems take (hf_ofdm_rx/tx's): radio, tuning, LO offset, route, port
// loOffset: tune the LO off the signal (RX and TX, every mode)
static QStringList radioArgs(const QString& dir, double khz, double hz, bool loOffset = true, double offsetHz = -1) {
    QSettings& s = settings();
    QString sdr = s.value(dir + "/radio", "virtual").toString();
    QStringList a = {"--sdr", sdr, "--rf-freq", QString::number(hz, 'f', 0)};
    // (an HPSDR radio's own DDC centres the signal: no LO offset)
    if (sdr != "virtual" && sdr != "hpsdr" && sdr != "kiwi" && loOffset) a << "--lo-offset-hz" << QString::number(offsetHz >= 0 ? offsetHz : loOffsetHz(khz), 'f', 0);
    QString uri = s.value(dir + "/uri").toString();
    if ((sdr == "pluto" || sdr == "hpsdr") && !uri.isEmpty()) a << "--pluto-uri" << uri;
    QString port = s.value(dir + "/port", "Auto").toString();
    if (sdr == "lime" && port != "Auto") a << "--sdr-antenna" << port;
    // KiwiSDR: the server set in Config (Receiver), always the current one
    if (sdr == "kiwi") a << "--pluto-uri" << s.value("kiwi_server").toString().trimmed();
    // RTL-SDR: its crystal's correction (RX page, shown with an RTL-SDR)
    if (sdr == "rtlsdr" && s.value(dir + "/ppm", 0).toDouble() != 0) a << "--sdr-ppm" << s.value(dir + "/ppm").toString();
    return a;
}

struct TypeSet { QStringList modes, widths, mods; };
static TypeSet typeSet(const QString& t) {
    if (t == "vu") return {{"VU"}, {"80", "160", "250"}, {"qpsk", "16qam", "32qam"}};
    if (t == "sat") return {{"S"}, {"10", "20", "40"}, {"qpsk", "8psk"}};
    return {{"A", "B", "C", "D", "SH"}, {"10", "20", "40", "80", "160", "250"},
            {"qpsk", "16qam", "32qam", "8psk", "16psk"}};
}

void enforceModeLimits(Segmented* mode, Segmented* bw, Segmented* mod) {
    const QString m = mode->value();
    QStringList widths = (m == "S" || m == "SH") ? QStringList{"10", "20", "40"}
                         : m == "VU"            ? QStringList{"80", "160", "250"}
                                                : QStringList{"10", "20", "40", "80", "160", "250"};
    QStringList mods = m == "S" ? QStringList{"qpsk", "8psk"} : m == "SH" ? QStringList{"qpsk", "8psk", "16psk"}
                                                                        : QStringList{"qpsk", "16qam", "32qam"};
    for (const QString& w : {"10", "20", "40", "80", "160", "250"})
        if (auto* b = bw->button(w)) b->setEnabled(widths.contains(w));
    if (!widths.contains(bw->value())) {     // the nearest width the mode has
        QString best = widths[0];
        for (const QString& w : widths)
            if (bw->button(w) && bw->button(w)->isVisibleTo(bw) &&
                std::fabs(w.toDouble() - bw->value().toDouble()) < std::fabs(best.toDouble() - bw->value().toDouble()))
                best = w;
        bw->setValue(best);
    }
    for (const QString& v : {"qpsk", "8psk", "16psk", "16qam", "32qam"})
        if (auto* b = mod->button(v)) b->setEnabled(mods.contains(v));
    if (!mods.contains(mod->value())) mod->setValue(mods[0]);
}

void applyModeType(Segmented* mode, Segmented* bw, Segmented* mod) {
    TypeSet t = typeSet(settings().value("mode_type", "hf").toString());
    for (const QString& v : {"A", "B", "C", "D", "SH", "VU", "S"})
        if (auto* b = mode->button(v)) b->setVisible(t.modes.contains(v));
    for (const QString& v : {"10", "20", "40", "80", "160", "250"})
        if (auto* b = bw->button(v)) b->setVisible(t.widths.contains(v));
    for (const QString& v : {"qpsk", "8psk", "16psk", "16qam", "32qam"})
        if (auto* b = mod->button(v)) b->setVisible(t.mods.contains(v));
    if (!t.modes.contains(mode->value())) mode->setValue(t.modes[0]);
    if (!t.widths.contains(bw->value())) {
        QString best = t.widths[0];
        for (const QString& w : t.widths)
            if (std::fabs(w.toDouble() - bw->value().toDouble()) < std::fabs(best.toDouble() - bw->value().toDouble())) best = w;
        bw->setValue(best);
    }
    if (!t.mods.contains(mod->value())) mod->setValue(t.mods[0]);
    enforceModeLimits(mode, bw, mod);
}

static QFrame* panel() {
    auto* f = new QFrame;
    f->setObjectName("panel");
    return f;
}

// ---------------------------------------------------------------- RX page
// the lock lamps (touch_rx_page): judged over max(1 s, 1.25x the typical gap between
// fragments, so slow modes always have one in it); events kept 5 s; a reported gap
// counts against the frame lamp only, at most 200 of it
static const qint64 LOCK_WINDOW_MS = 1000, STATUS_WINDOW_MS = 5000;
static const int LAMP_MAX_LOST = 200;

RxPage::RxPage(QWidget* parent) : QWidget(parent) {
    link_.source = "RX";
    QSettings& s = settings();
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(ui.px(8), ui.px(4), ui.px(8), ui.px(8));
    root->setSpacing(ui.px(8));

    // left: lamps + callsign, the picture, the status line, the spectrum
    auto* left = new QVBoxLayout;
    left_ = left;
    left->setSpacing(ui.px(4));
    header_ = new QWidget;
    auto* top = new QHBoxLayout(header_);
    top->setContentsMargins(0, 0, 0, 0);
    top->setSpacing(ui.px(6));
    int ls = ui.px(14);
    hdrLamp_ = new Lamp(ls);
    frameLamp_ = new Lamp(ls);
    hdrLamp_->setToolTip("Header lock over the last 5 s: green all, orange some, red none");
    frameLamp_->setToolTip("Frame (CRC) lock over the last 5 s: green all, orange some, red none");
    top->addWidget(hdrLamp_);
    auto* hl = new QLabel("Hdr");
    hl->setObjectName("dim");
    top->addWidget(hl);
    top->addSpacing(ui.px(6));
    top->addWidget(frameLamp_);
    auto* fl = new QLabel("Frm");
    fl->setObjectName("dim");
    top->addWidget(fl);
    top->addSpacing(ui.px(10));
    station_ = new QLabel;
    station_->setObjectName("big");    // the callsign (as the Python GUI: 20 px bold)
    top->addWidget(station_, 1);
    left->addWidget(header_);
    video_ = new VideoView;
    video_->clearFrame("No signal");
    connect(video_, &VideoView::tapped, this, &RxPage::toggleBigVideo);
    left->addWidget(video_, 3);
    status_ = new ElideLabel("Idle");
    status_->setObjectName("value");
    status_->setStyleSheet(QString("font-size: %1px;").arg(ui.px(19)));   // (the MER line: a little bigger)
    status_->setAlignment(Qt::AlignCenter);
    status_->installEventFilter(this);      // a tap: the constellation
    left->addWidget(status_);
    spectrum_ = new SpectrumView;
    spectrum_->setAverages(s.value("rx/spec_avg", 1).toInt());
    left->addWidget(spectrum_, 2);
    leftW_ = new QWidget;       // (a widget: its width capped to the picture's 16:9, see resizeEvent)
    leftW_->setLayout(left);
    left->setContentsMargins(0, 0, 0, 0);
    root->addWidget(leftW_, 3);

    // right: the settings, then RX + message
    right_ = new QWidget;
    auto* right = new QVBoxLayout(right_);
    right->setContentsMargins(0, 0, 0, 0);
    auto* grid = new QGridLayout;
    grid->setHorizontalSpacing(ui.px(8));
    grid->setVerticalSpacing(ui.px(6));
    mode_ = new Segmented(pairs(MODES), s.value("rx/mode", "A").toString());
    bw_ = new Segmented(pairs(BANDWIDTHS), s.value("rx/bw", "80").toString());
    mod_ = new Segmented(MODULATIONS, s.value("rx/mod", "qpsk").toString());
    freq_ = new FreqButton("RX frequency", s.value("rx/freq_hz", DEFAULT_FREQ_HZ).toDouble());
    radio_pick_ = new RadioPicker("RX radio (connected now)", true, s.value("rx/radio", "virtual").toString(),
                                  s.value("rx/uri").toString());
    port_ = new Picker("LimeSDR RX port", LIME_RX_PORTS, s.value("rx/port", "Auto").toString());
    gain_ = new Stepper(0, 70, 1, s.value("rx_gain", 30).toDouble(), " dB");
    // RTL-SDR crystal correction (no TCXO): shown with an RTL-SDR only
    ppm_ = new Stepper(-200, 200, 1, s.value("rx/ppm", 0).toDouble(), " ppm");
    ppmLabel_ = rowLabel("PPM");
    // receive offset, live: nudges the receiver onto an off-frequency signal
    offset_ = new Stepper(-100, 100, offsetStepKhz(), 0, " kHz");
    offset_->sign = true;      // (then the saved value: drawn with its sign)
    offset_->setValue(s.value("rx/offset_khz", 0).toDouble());
    spectrum_->setScale(true, 0);    // the spectrum's scale: always Auto (fitted to noise and peak)
    int r = 0;
    for (auto p : QList<QPair<QString, QWidget*>>{{"Freq", radioRow(freq_, radio_pick_, port_)}, {"Offset", offset_}, {"PPM", ppm_},
                                                   {"Gain", gain_},
                                                   {"Mode", mode_}, {"kHz", bw_}, {"Mod", mod_}}) {
        grid->addWidget(p.second == ppm_ ? ppmLabel_ : rowLabel(p.first), r, 0);
        grid->addWidget(p.second, r++, 1);
    }
    connect(ppm_, &Stepper::changed, this, [this](double v) {
        settings().setValue("rx/ppm", v);
        markPending();
    });
    writeRxOffset(offset_->value());
    connect(offset_, &Stepper::changed, this, [](double khz) {
        settings().setValue("rx/offset_khz", khz);
        writeRxOffset(khz);        // the running receiver follows its file
    });
    auto showRadio = [this] {
        ppm_->setVisible(radio_pick_->driver() == "rtlsdr");
        ppmLabel_->setVisible(radio_pick_->driver() == "rtlsdr");
        showKiwiInfo();
        port_->setVisible(radio_pick_->driver() == "lime");
        double lo, hi;
        radioRange(radio_pick_->driver(), &lo, &hi);
        freq_->setRange(lo, hi, radioName(radio_pick_->driver()));
    };
    showRadio();
    connect(radio_pick_, &RadioPicker::radioChanged, this, [this, showRadio](const QString& d, const QString& uri) {
        settings().setValue("rx/radio", d);
        settings().setValue("rx/uri", uri);
        showRadio();
        if (!freq_->inRange()) Notices::get().add("RX", freq_->outOfRange() + " -- set a new RX frequency");
    });
    connect(port_, &Picker::changed, this, [](const QString& v) { settings().setValue("rx/port", v); });
    connect(freq_, &FreqButton::changed, this, [](double hz) { settings().setValue("rx/freq_hz", hz); });
    // RX gain: live -- the in-process receiver directly, the Python one through its file
    connect(gain_, &Stepper::changed, this, [this](double v) {
        link_.setGain(v);
        settings().setValue("rx_gain", v);
        QFile f(rxGainFile());
        if (f.open(QIODevice::WriteOnly | QIODevice::Truncate)) f.write(QByteArray::number(v));
    });
    // volume (the received audio)
    auto* vol = new Stepper(0, 100, 10, s.value("volume", 80).toDouble(), " %");
    grid->addWidget(rowLabel("Volume"), r, 0);
    grid->addWidget(vol, r++, 1);
    media_.audio().setVolume((float)(vol->value() / 100.0));
    if (!s.value("audio_device").toString().isEmpty()) media_.audio().open(s.value("audio_device").toString().toStdString());
    connect(vol, &Stepper::changed, this, [this](double v) {
        media_.audio().setVolume((float)(v / 100.0));
        settings().setValue("volume", v);
    });
    right->addLayout(grid);
    right->addStretch(1);
    // KiwiSDR only: whose receiver this is (its /status page's name)
    kiwiInfo_ = new QLabel;
    kiwiInfo_->setWordWrap(true);
    kiwiInfo_->setAlignment(Qt::AlignCenter);
    kiwiInfo_->setStyleSheet(QString("color: #f0c040; font-size: %1px;").arg(ui.px(13)));
    kiwiInfo_->hide();
    right->addWidget(kiwiInfo_);
    auto* runRow = new QHBoxLayout;
    run_ = new QPushButton("RX");
    run_->setObjectName("run");
    msg_ = new QPushButton(QString::fromUtf8("\u2709"));
    msg_->setObjectName("msg");
    runRow->addWidget(run_, 4);
    runRow->addWidget(msg_, 1);
    right->addLayout(runRow);
    root->addWidget(right_, 2);
    msg_->setToolTip("Text messages received");
    connect(msg_, &QPushButton::clicked, this, &RxPage::showMessages);
    rec_btn_ = new QPushButton(QString::fromUtf8("\u25CF"));
    rec_btn_->setObjectName("msg");
    rec_btn_->setToolTip("Record the received picture and sound (~/recordings)");
    runRow->insertWidget(1, rec_btn_, 1);
    connect(rec_btn_, &QPushButton::clicked, this, &RxPage::toggleRecord);
    media_.audio().tap = [this](const float* p, size_t n) { rec_.sound(p, n); };
    connect(&media_, &MediaDecoder::frame, this, [this](const QImage& img) {
        if (recWant_ && !rec_.recording()) {     // the file opens at the first picture: its size
            QDir().mkpath(QDir::homePath() + "/recordings");
            QString path = QDir::homePath() + "/recordings/avm_" +
                           QDateTime::currentDateTime().toString("yyyyMMdd_HHmmss") + ".mp4";
            QString err;
            if (!rec_.start(path, img.width(), img.height(), &err)) {
                recWant_ = false;
                status_->setText("Recording: " + err);
            } else {
                status_->setText("Recording to " + QDir::toNativeSeparators(path));
            }
            updateRecButton();
        }
        rec_.picture(img);
        if (++pictures_ % 20 == 1) guiLog("RX", QString("picture %1 (%2x%3)").arg(pictures_).arg(img.width()).arg(img.height()));
    });
    updateRecButton();
    connect(&media_, &MediaDecoder::message, this, &RxPage::onMessage);
    updateMsgButton();

    auto save = [this] {
        QSettings& s = settings();
        s.setValue("rx/mode", mode_->value());
        s.setValue("rx/bw", bw_->value());
        s.setValue("rx/mod", mod_->value());
    };
    connect(mode_, &Segmented::changed, this, [this] { enforceModeLimits(mode_, bw_, mod_); });
    for (auto* w : {mode_, bw_, mod_}) connect(w, &Segmented::changed, this, save);
    for (auto* w : {mode_, bw_, mod_}) connect(w, &Segmented::changed, this, &RxPage::updateBand);
    applyModeType();
    updateBand();
    connect(run_, &QPushButton::clicked, this, &RxPage::toggleRun);
    connect(&link_, &Link::line, this, &RxPage::onLine);
    connect(&link_, &Link::data, &media_, &MediaDecoder::feed);
    connect(&media_, &MediaDecoder::frame, video_, &VideoView::setFrame);
    connect(&media_, &MediaDecoder::station, this, [this](const QString& id, bool confirmed) {
        station_->setText(confirmed || id.isEmpty() || id.contains('-') ? id : id + "?");
    });
    connect(&media_, &MediaDecoder::videoConfig, this, [this](int w, int h, double fps, const QString& codec) {
        // (repeated every couple of seconds: only a caption while there's no picture yet)
        if (video_->frame().isNull()) video_->clearFrame(QString("%1 %2x%3 %4 fps").arg(codec).arg(w).arg(h).arg(fps));
    });
    connect(&media_, &MediaDecoder::frame, this, [this] { frameTimes_.push_back(clock_.elapsed()); });
    connect(&link_, &Link::stateChanged, this, [this](bool on) {
        if (!on) {
            media_.stop();
            video_->clearFrame("No signal");
            spectrum_->clear();
            if (rec_.recording()) status_->setText("Saved " + QDir::toNativeSeparators(rec_.path()));
            else status_->setText(QString("Stopped  \u00b7  %1 OK, %2 lost, %3 bad  \u00b7  %4  \u00b7  CFO %5")
                                      .arg(ok_).arg(lost_).arg(bad_).arg(merText(false), cfoText()));
            rec_.stop();
            recWant_ = false;
        }
        updateRecButton();
        if (!on) pending_ = false;
        // duplex: the transmitter once the radio (this receiver's) is up; it stops with it
        if (on && duplexRun_ && peer_) peer_->start();
        if (!on && duplexRun_) {
            duplexRun_ = false;
            if (peer_ && peer_->running()) peer_->stop();
        }
        if (on || !restarting_) {
            run_->setText(on ? "STOP" : runLabel());
            run_->setStyleSheet(on ? QString("background: %1;").arg(STOP) : QString());
        } else run_->setText("STARTING...");
        if (!on) { events_.clear(); updateLamps(); hdrLamp_->setState(Lamp::Idle); frameLamp_->setState(Lamp::Idle); }
        emit runningChanged(on);
    });
    clock_.start();
    connect(&lampTimer_, &QTimer::timeout, this, &RxPage::updateLamps);
    lampTimer_.start(500);
    connect(&demoTimer_, &QTimer::timeout, this, &RxPage::demoTick);
    for (auto* w : {mode_, bw_, mod_}) connect(w, &Segmented::changed, this, &RxPage::markPending);
    connect(freq_, &FreqButton::changed, this, &RxPage::markPending);
    connect(radio_pick_, &RadioPicker::radioChanged, this, &RxPage::markPending);
    connect(port_, &Picker::changed, this, &RxPage::markPending);
}

// a message the C++ player decoded: [4-byte UTC time][text]
void RxPage::onMessage(const QString& text) {
    // (media.cpp passes the text after the time; the time isn't needed to keep them apart here)
    for (const Msg& m : msgs_) if (m.text == text) return;     // each once per session
    guiLog("RX", "message: " + text);
    msgs_.append({QTime::currentTime().toString("HH:mm:ss"), 0, station_->text().remove('?').trimmed(), text});
    ++unread_;
    updateMsgButton();
}

void RxPage::toggleRecord() {
    if (recWant_) {
        recWant_ = false;
        if (rec_.recording()) status_->setText("Saved " + QDir::toNativeSeparators(rec_.path()));
        rec_.stop();
    } else if (link_.running()) {
        recWant_ = true;             // from the next picture
        status_->setText("Recording from the next picture");
    }
    updateRecButton();
}

void RxPage::updateRecButton() {
    rec_btn_->setEnabled(link_.running());
    rec_btn_->setStyleSheet(recWant_ ? QString("background: %1; color: white;").arg(STOP) : QString());
}

void RxPage::updateMsgButton() {
    msg_->setText(unread_ ? QString::fromUtf8("\u2709 %1").arg(unread_) : QString::fromUtf8("\u2709"));
    msg_->setEnabled(!msgs_.isEmpty());
    msg_->setStyleSheet(unread_ ? QString("background: %1; color: white;").arg(RX_ACCENT) : QString());
}

// the messages received, newest first (MessagesDialog)
void RxPage::showMessages() {
    QDialog d(this);
    d.setWindowTitle("Messages received");
    auto* lay = new QVBoxLayout(&d);
    auto* list = new QListWidget;
    list->setWordWrap(true);
    for (int i = msgs_.size() - 1; i >= 0; --i)
        list->addItem(QString("%1  %2  %3").arg(msgs_[i].got, msgs_[i].call.isEmpty() ? "?" : msgs_[i].call, msgs_[i].text));
    lay->addWidget(list, 1);
    auto* close = new QPushButton("Close");
    connect(close, &QPushButton::clicked, &d, &QDialog::accept);
    lay->addWidget(close);
    d.resize(ui.px(520), ui.px(320));
    embedDialog(&d, this);
    unread_ = 0;
    updateMsgButton();
    d.exec();
}

void RxPage::markPending() {
    if (!running()) return;
    pending_ = true;
    run_->setText("RESTART TO APPLY");
    run_->setStyleSheet(QString("background: %1; color: #111;").arg(WARN));
}

// the capture band (libavm's avm_capture_band; touch_rx_page._tuning_geometry):
// the signal's width plus the pull-in each side -- a signal anywhere in it is
// found and decoded
void RxPage::updateBand() {
    double lo = 0, hi = 0;
    if (!captureBand(mode_->value(), bw_->value().toDouble(), mod_->value(), &lo, &hi)) lo = hi = 0;
    spectrum_->setBand(lo, hi);
}

void RxPage::applyModeType() {
    markPending();
    offset_->setStep(offsetStepKhz());
    avm::applyModeType(mode_, bw_, mod_);
    settings().setValue("rx/mode", mode_->value());
    settings().setValue("rx/bw", bw_->value());
    settings().setValue("rx/mod", mod_->value());
}

QString RxPage::modeSummary() const {
    return QString("%1 %2 kHz %3").arg(mode_->value(), bw_->value(), mod_->value().toUpper());
}

QStringList RxPage::modemArgs() const {
    {
        QFile f(rxGainFile());
        if (f.open(QIODevice::WriteOnly | QIODevice::Truncate)) f.write(settings().value("rx_gain", 30).toByteArray());
    }
    writeRxOffset(offset_->value());
    return {"--rx-gain", settings().value("rx_gain", 30).toString(), "--gain-file", rxGainFile(),
            "--freq-offset-file", rxOffsetFile(), "--mode", mode_->value(),
            "--occupancy", bw_->value(), "--modulation", mod_->value(), "--fec", "ldpc",
            "--fragment-size", QString::number(fragmentSize(bw_->value().toDouble())), "--spectrum-stderr",
            // the player buffers the audio itself: pacing the output only delayed it (touch_rx_page)
            "--no-output-pacing",
            // the spectrum 1.5x the signal's width, from the raw samples (noise either side)
            "--spectrum-span-hz", QString::number(1.5 * bw_->value().toDouble() * 1000, 'f', 0)};
}

// The receiver on the page's radio, opened by the receiver itself (SoapySDR,
// HPSDR, a KiwiSDR or the virtual radio: c++/net_radios.h)
// the press: STARTING / STOPPING / RESTARTING at once, and the work on the
// next turn of the event loop -- opening a radio takes a second or two, and a
// change made in the same call wasn't drawn until it was over
void RxPage::toggleRun() {
    run_->setText(pending_ && running() ? "RESTARTING..." : running() ? "STOPPING..." : "STARTING...");
    run_->setStyleSheet(QString("background: %1; color: %2;").arg(BUTTON, TEXT));
    QTimer::singleShot(30, this, [this] {
        doToggleRun();
        if (!running() && !restarting_) { run_->setText(runLabel()); run_->setStyleSheet(QString()); }   // stopped, or didn't start
    });
}

void RxPage::doToggleRun() {
    if (pending_ && running()) {     // restart to apply: stop, then start with the new settings
        pending_ = false;
        restarting_ = true;          // (the button says STARTING... meanwhile, not RX)
        doToggleRun();
        QTimer::singleShot(300, this, [this] {
            restarting_ = false;
            run_->setText("STARTING...");
            if (!running()) doToggleRun();
            if (!running()) { run_->setText(runLabel()); run_->setStyleSheet(QString()); }
        });
        return;
    }
    if (link_.running()) {
        if (duplexRun_ && peer_ && peer_->running()) peer_->stop();   // (its samples go to this radio)
        link_.stop();
        return;
    }
    if (!freq_->inRange()) {     // the driver would quietly open at the nearest frequency it can tune
        status_->setText("Not started: " + freq_->outOfRange());
        Notices::get().add("RX", "Not started: " + freq_->outOfRange() + " -- set a new RX frequency");
        return;
    }
    QSettings& s = settings();
    const double khz = bw_->value().toDouble();
    const QString sdr = s.value("rx/radio", "virtual").toString();
    const QString rate = QString::number(sdrRate(khz));
    ok_ = bad_ = lost_ = 0;
    mer_.clear();
    cfo_.clear();
    status_->setText(media_.ok() ? QString("Searching...") : media_.error() + " -- no video");
    if (!media_.ok()) Notices::get().add("RX", media_.error());
    media_.start(fragmentSize(khz));
    {   // the audio held back: one fragment's air time + 0.25 s, at least 0.5 s (touch_rx_page)
        using Fn = double (*)(const char*, double, const char*, const char*, int, double);
        static QLibrary lib(avmDir() + "/c++/avm");
        auto fn = (Fn)((lib.isLoaded() || lib.load()) ? lib.resolve("avm_link_bitrate") : nullptr);
        double bps = fn ? fn(mode_->value().toUtf8().constData(), khz, mod_->value().toUtf8().constData(), "ldpc",
                             fragmentSize(khz), 0) : 0;
        double period = bps > 0 ? fragmentSize(khz) * 8 / bps : 0.5;
        double target = std::max(0.5, period + 0.25);
        if (!duplexRole().isEmpty()) target = std::max(target, TDD_PLAYER_LATENCY_S);   // bursts a cycle apart
        media_.audio().setLatency(target, std::max(0.2, 0.6 * period + 0.05));
    }
    QStringList args = modemArgs();
    QStringList radio = radioArgs("rx", khz, freq_->hz());
    if (sdr == "kiwi" && khz != 10) {      // a KiwiSDR's ~12 kS/s stream carries 10 kHz only
        status_->setText("Not started: a KiwiSDR carries 10 kHz modes only");
        Notices::get().add("RX", "Not started: a KiwiSDR carries 10 kHz modes only -- set kHz to 10");
        return;
    }
    if (sdr == "kiwi" && settings().value("kiwi_server").toString().trimmed().isEmpty()) {
        status_->setText("Not started: set the KiwiSDR server (Config, Receiver)");
        return;
    }
    args << "--input" << "pluto" << "--sample-rate" << rate;
    args += radio;
    if (s.value("rx/invert", false).toBool()) args << "--invert-spectrum";
    // duplex: this receiver's radio also transmits (the TX page's samples, tdd.h)
    duplexRun_ = !duplexRole().isEmpty();
    if (duplexRun_) {
        QFile::remove(tddFile("avm_tdd_adjust"));      // (a fresh start: no old shift or report)
        QFile::remove(tddFile("avm_tdd_report"));
        args += tddArgs();
        args << "--tdd-tx" << "--tdd-tx-gain" << settings().value("tx_gain", -10).toString();
        if (plutoOnUsb()) {
            int i = args.indexOf("--pluto-uri");
            if (i >= 0 && i + 1 < args.size()) args[i + 1] = "usb:";
            else args << "--pluto-uri" << "usb:";
        }
        guiLog("RX", "Duplex " + duplexRole() + ": TX+RX on one PlutoSDR");
    }
    args << "--drop-if-slow";     // behind live: skip to it (the Python GUI's default too)
    link_.startRx(args, QString());
}

// ---------------------------------------------------------------- constellation
// The received signal's equalised data points (touch_widgets.ConstellationDialog):
// the last two fragments as a density picture, a fixed scale per modulation.
// While open, the receiver sends them ("[constellation] <base64 complex64>"
// lines): its flag file is created here and removed on close.
static const char* CONST_FLAG = "/dev/shm/avm_constellation";

class ConstellationDialog : public QDialog {
public:
    ConstellationDialog(QWidget* parent, const QString& mod) : QDialog(parent, Qt::FramelessWindowHint | Qt::Dialog) {
        const double outer = mod == "qpsk" ? 0.7071 : mod == "16qam" ? 0.9487 : mod == "32qam" ? 1.1180 : 1.0;
        lim_ = 1.4 * outer;
        auto* lay = new QVBoxLayout(this);
        auto* top = new QHBoxLayout;
        title_ = new QLabel("Constellation -- waiting for a decoded fragment...");
        title_->setObjectName("big");
        top->addWidget(title_, 1);
        auto* clear = new QPushButton("Clear");
        auto* close = new QPushButton("Close");
        top->addWidget(clear);
        top->addWidget(close);
        lay->addLayout(top);
        view_ = new QLabel;
        view_->setAlignment(Qt::AlignCenter);
        view_->setStyleSheet("background: #0c0c0c;");
        lay->addWidget(view_, 1);
        connect(clear, &QPushButton::clicked, this, [this] { frags_.clear(); view_->clear(); });
        connect(close, &QPushButton::clicked, this, &QDialog::accept);
        QFile f(CONST_FLAG);
        if (!f.open(QIODevice::WriteOnly)) title_->setText("Constellation -- can't switch the receiver's output on (/dev/shm)");
        QWidget* w = parent->window();
        setGeometry(QRect(w->mapToGlobal(QPoint(0, 0)), w->size()).adjusted(ui.px(40), ui.px(20), -ui.px(40), -ui.px(20)));
    }
    ~ConstellationDialog() override { QFile::remove(CONST_FLAG); }
    void add(const QByteArray& b64) {
        QByteArray raw = QByteArray::fromBase64(b64);
        std::vector<std::complex<float>> pts(raw.size() / sizeof(std::complex<float>));
        std::memcpy(pts.data(), raw.constData(), pts.size() * sizeof(std::complex<float>));
        frags_.push_back(std::move(pts));
        while (frags_.size() > 2) frags_.erase(frags_.begin());
        draw();
    }
private:
    void draw() {
        const int N = 300;
        std::vector<int> h(N * N, 0);
        size_t total = 0;
        for (const auto& f : frags_)
            for (const auto& z : f) {
                int x = (int)((z.real() + lim_) / (2 * lim_) * N), y = (int)((lim_ - z.imag()) / (2 * lim_) * N);
                if (x < 0 || y < 0 || x >= N || y >= N) continue;
                ++h[y * N + x];
                ++total;
            }
        int mx = std::max(1, *std::max_element(h.begin(), h.end()));
        QImage img(N, N, QImage::Format_RGB32);
        const double lm = std::log1p((double)mx);
        for (int y = 0; y < N; ++y) {
            QRgb* row = (QRgb*)img.scanLine(y);
            for (int x = 0; x < N; ++x) {
                int c = h[y * N + x];
                if (!c) { row[x] = (x == N / 2 || y == N / 2) ? qRgb(60, 60, 60) : qRgb(12, 12, 12); continue; }
                // an inferno-like ramp: dark red -> orange -> pale yellow
                double t = std::log1p((double)c) / lm;
                row[x] = qRgb((int)(80 + 175 * std::min(1.0, t * 1.6)), (int)(255 * std::min(1.0, std::max(0.0, t * 1.3 - 0.25))),
                              (int)(255 * std::min(1.0, std::max(0.0, t * 2 - 1.2))));
            }
        }
        int side = std::max(50, std::min(view_->width(), view_->height()));
        view_->setPixmap(QPixmap::fromImage(img).scaled(side, side, Qt::KeepAspectRatio, Qt::FastTransformation));
        title_->setText(QString("Constellation -- last %1 fragment(s), %2 points").arg(frags_.size()).arg(total));
    }
    double lim_;
    QLabel *title_, *view_;
    std::vector<std::vector<std::complex<float>>> frags_;
};

bool RxPage::eventFilter(QObject* o, QEvent* e) {
    if (o == status_ && e->type() == QEvent::MouseButtonRelease) {
        if (!constDlg_) {
            auto* d = new ConstellationDialog(this, mod_->value());
            d->setAttribute(Qt::WA_DeleteOnClose);
            constDlg_ = d;
            d->show();
        }
        return true;
    }
    return QWidget::eventFilter(o, e);
}

// KiwiSDR only: which receiver this is (its public /status page: name=, users=...)
void RxPage::showKiwiInfo() {
    QString server = radio_pick_->driver() == "kiwi" ? settings().value("kiwi_server").toString().trimmed() : QString();
    if (!kiwiInfo_) return;     // (not built yet)
    if (server.isEmpty()) { kiwiInfo_->hide(); return; }
    kiwiInfo_->setText(kiwiInfoCache_.value(server, "KiwiSDR " + server));
    kiwiInfo_->show();
    if (kiwiInfoCache_.contains(server)) return;
    kiwiInfoCache_[server] = "KiwiSDR " + server;     // one fetch per server
    static QNetworkAccessManager* net = new QNetworkAccessManager(qApp);
    QString host = server.contains(':') ? server : server + ":8073";
    QNetworkRequest req(QUrl("http://" + host + "/status"));
    QNetworkReply* reply = net->get(req);
    QTimer::singleShot(6000, reply, &QNetworkReply::abort);
    connect(reply, &QNetworkReply::finished, this, [this, reply, server] {
        reply->deleteLater();
        QString text = "KiwiSDR " + server + "\n(no status page)";
        if (reply->error() == QNetworkReply::NoError) {
            QMap<QString, QString> st;
            for (const QString& l : QString::fromUtf8(reply->read(8192)).split('\n')) {
                int i = l.indexOf('=');
                if (i > 0) st[l.left(i)] = l.mid(i + 1).trimmed();
            }
            QString name = st.value("name").isEmpty() ? server : st.value("name");
            QStringList extra;
            if (!st.value("users").isEmpty() && !st.value("users_max").isEmpty())
                extra << QString("users %1/%2").arg(st["users"], st["users_max"]);
            if (st.value("offline") == "yes") extra << "OFFLINE";
            text = "KiwiSDR: " + name + (extra.isEmpty() ? QString() : "\n" + extra.join(QString::fromUtf8(" · ")));
        }
        kiwiInfoCache_[server] = text;
        showKiwiInfo();
    });
}

void RxPage::setSpectrumAverages(int n) { spectrum_->setAverages(n); }

void RxPage::refreshDuplex() {
    if (!running() && !pending_ && run_->text() != runLabel() &&
        (run_->text() == "RX" || run_->text() == "TX+RX"))
        run_->setText(runLabel());
}

// status lines -> lamps, callsign, status
void RxPage::onLine(const QString& t) {
    if (t.startsWith("[constellation] ")) {     // (big: not logged)
        if (constDlg_) static_cast<ConstellationDialog*>(constDlg_.data())->add(t.mid(16).toLatin1());
        return;
    }
    guiLog("RX", t);
    static const QRegularExpression frag(R"(^Fragment seq=(\d+): (OK|CRC MISMATCH)\s+CFO=(\S+)\s+BER=(\S+)\s+EVM=(\S+))");
    static const QRegularExpression station(R"(callsign[:=]\s*(\S+))", QRegularExpression::CaseInsensitiveOption);
    qint64 now = clock_.elapsed();
    auto m = frag.match(t);
    static const QRegularExpression lostRe(R"(\((\d+) fragment\(s\) lost)");
    if (m.hasMatch()) {
        bool ok = m.captured(2) == "OK";
        events_.push_back({now, 1, ok ? 1 : 0});
        ok ? ++ok_ : ++bad_;
        // MER from the EVM (-20 log10 EVM), and the CFO: the last 8 each (MER_AVERAGE)
        double evm = QString(m.captured(5)).remove('%').toDouble();
        if (evm > 0) {
            if (!mer_.empty() && now - merT_ > 2000) mer_.clear();      // back after a gap
            mer_.push_back(-20 * std::log10(evm / 100));
            if (mer_.size() > 8) mer_.pop_front();
            merT_ = now;
        }
        cfo_.push_back(QString(m.captured(3)).remove("Hz").toDouble());
        if (cfo_.size() > 8) cfo_.pop_front();
    } else if (t.startsWith("Fragment: FAILED")) {
        events_.push_back({now, 0, 0});
    } else if (auto lm = lostRe.match(t); lm.hasMatch()) {
        lost_ += lm.captured(1).toInt();
        for (int i = 0; i < std::min(lm.captured(1).toInt(), LAMP_MAX_LOST); ++i) events_.push_back({now, -1, 0});
    } else if (t.startsWith("[spectrum] ")) {
        QStringList f = t.split(' ');
        if (f.size() == 5) {
            QByteArray raw = QByteArray::fromBase64(f[4].toLatin1());
            QVector<float> db(raw.size() / 4);
            std::memcpy(db.data(), raw.constData(), (size_t)db.size() * 4);
            spectrum_->setSpectrum(f[1].toDouble(), f[2].toDouble(), db);
        }
        return;
    } else if (t.startsWith("ERROR")) {
        status_->setText(t);
    }
    auto sm = station.match(t);
    if (sm.hasMatch()) station_->setText(sm.captured(1));
    updateLamps();
}

// "MER measured/needed dB": needed, where 90% of frames decode in plain noise
// for this mode / width / modulation (mer_thresholds.json)
QString RxPage::merText(bool live) const {
    static QJsonObject table;
    static bool loaded = false;
    if (!loaded) {
        loaded = true;
        QFile f(avmDir() + "/mer_thresholds.json");
        if (f.open(QIODevice::ReadOnly)) table = QJsonDocument::fromJson(f.readAll()).object().value("table").toObject();
    }
    QJsonValue e = table.value(QString("%1/%2/%3").arg(mode_->value()).arg(bw_->value().toInt()).arg(mod_->value()));
    QString need = e.isObject() ? QString::number(e.toObject().value("mer_frame90").toDouble(), 'f', 1) : QString("--");
    if (live && !mer_.empty() && clock_.elapsed() - merT_ > 2000) return QString("0.0/%1 dB").arg(need);
    if (mer_.empty()) return QString("--/%1 dB").arg(need);
    double s = 0;
    for (double v : mer_) s += v;
    return QString("%1/%2 dB").arg(s / mer_.size(), 0, 'f', 1).arg(need);
}

QString RxPage::cfoText() const {
    if (cfo_.empty()) return "-- Hz";
    double s = 0;
    for (double v : cfo_) s += v;
    return QString("%1%2 Hz").arg(s >= 0 ? "+" : "").arg(qRound(s / cfo_.size()));
}

void RxPage::updateLamps() {
    qint64 now = clock_.elapsed();
    static qint64 lastAudioLog = 0;      // the audio buffer, once a second in the --log file
    if (link_.running() && now - lastAudioLog >= 1000) {
        lastAudioLog = now;
        guiLog("AUDIO", QString("buffered %1 s, underruns %2, %3").arg(media_.audio().buffered_s(), 0, 'f', 2)
                            .arg(media_.audio().underruns).arg(media_.audio().callbackStats()));
    }
    while (!frameTimes_.empty() && now - frameTimes_.front() > 4000) frameTimes_.pop_front();
    if (link_.running() && !status_->text().startsWith("ERROR") && !status_->text().startsWith("Recording"))
        status_->setText(QString("MER %1  \u00b7  CFO %2  \u00b7  %3 fps")
                             .arg(merText(true), cfoText()).arg(qRound(frameTimes_.size() / 4.0)));
    // the window: 1 s, or 1.25x the median gap between fragments if longer
    std::vector<qint64> gaps;
    for (size_t i = 1; i < events_.size(); ++i)
        if (events_[i].t > events_[i - 1].t) gaps.push_back(events_[i].t - events_[i - 1].t);
    qint64 window = LOCK_WINDOW_MS;
    if (!gaps.empty()) {
        std::nth_element(gaps.begin(), gaps.begin() + gaps.size() / 2, gaps.end());
        window = std::max(window, (qint64)(1.25 * gaps[gaps.size() / 2]));
    }
    while (!events_.empty() && now - events_.front().t > std::max(STATUS_WINDOW_MS, window)) events_.pop_front();
    if (!link_.running() && !demoTimer_.isActive()) return;
    auto state = [&](bool header) {
        int judged = 0, good = 0;
        for (const auto& e : events_) {
            if (now - e.t > window) continue;
            int v = header ? e.header : e.frame;
            if (v < 0) continue;
            ++judged;
            good += v;
        }
        return judged == 0 ? Lamp::Idle : good == 0 ? Lamp::None : good == judged ? Lamp::Good : Lamp::Some;
    };
    hdrLamp_->setState(state(true));
    frameLamp_->setState(state(false));
}

// --demo: a moving test picture and fake lock events, to judge the layout
void RxPage::setDemo(bool on) {
    if (on) { demoTimer_.start(100); station_->setText("M0DTS"); }
    else demoTimer_.stop();
}

void RxPage::demoTick() {
    QImage img(384, 224, QImage::Format_RGB32);
    QPainter p(&img);
    const QColor bars[] = {Qt::white, Qt::yellow, Qt::cyan, Qt::green, Qt::magenta, Qt::red, Qt::blue};
    for (int i = 0; i < 7; ++i) p.fillRect(i * 384 / 7, 0, 384 / 7 + 1, 160, bars[i]);
    p.fillRect(0, 160, 384, 64, QColor(30, 30, 30));
    int x = (demoPhase_ * 4) % 384;
    p.fillRect(x, 168, 40, 48, QColor(RX_ACCENT));
    p.setPen(Qt::white);
    p.drawText(QRect(0, 160, 384, 64), Qt::AlignCenter, QString("AVM demo  frame %1").arg(demoPhase_));
    p.end();
    video_->setFrame(img);
    if (demoPhase_ % 5 == 0) {
        bool ok = (demoPhase_ / 5) % 13 != 0;
        onLine(QString("Fragment seq=%1: %2  CFO=+12.3Hz  BER=0.0012  EVM=4.1%").arg(demoPhase_ / 5).arg(ok ? "OK" : "CRC MISMATCH"));
    }
    ++demoPhase_;
}

// double-click on the picture: the picture alone, the settings hidden
// the left column no wider than the picture's 16:9 at the height it gets
// (a wide screen gave a ~2:1 box, black bars at the sides); the controls
// on the right get the rest
void RxPage::resizeEvent(QResizeEvent* e) {
    QWidget::resizeEvent(e);
    QMetaObject::invokeMethod(this, [this] {
        if (overlay_) return;
        int w = video_->height() * 16 / 9;
        if (w > 0 && leftW_->maximumWidth() != w) leftW_->setMaximumWidth(w);
    }, Qt::QueuedConnection);
}

void RxPage::toggleBigVideo() {
    if (!overlay_) {
        QWidget* win = window();
        overlay_ = new QWidget(win);
        overlay_->setObjectName("overlay");
        overlay_->setStyleSheet(QString("QWidget#overlay { background: %1; }").arg(BG));
        auto* lay = new QVBoxLayout(overlay_);
        lay->setContentsMargins(ui.px(6), ui.px(4), ui.px(6), ui.px(4));
        lay->setSpacing(ui.px(4));
        lay->addWidget(header_);
        lay->addWidget(video_, 1);
        lay->addWidget(status_);
        overlay_->setGeometry(win->rect());
        overlay_->show();
        overlay_->raise();
    } else {
        // back where they were: above the spectrum, under nothing
        left_->insertWidget(0, header_);
        left_->insertWidget(1, video_, 3);
        left_->insertWidget(2, status_);
        overlay_->hide();
        overlay_->deleteLater();
        overlay_ = nullptr;
    }
}

// ---------------------------------------------------------------- TX page
TxPage::TxPage(QWidget* parent) : QWidget(parent) {
    link_.source = "TX";
    QSettings& s = settings();
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(ui.px(8), ui.px(4), ui.px(8), ui.px(8));
    root->setSpacing(ui.px(8));
    auto* left = new QVBoxLayout;
    preview_ = new VideoView;
    preview_->clearFrame("Camera preview");
    left->addWidget(preview_);
    // the picture's size and rate and the audio: the link's suggestion on each
    // mode / width / modulation change, changeable after (the rates follow)
    res_ = new Picker("Resolution", {"128x80", "192x112", "224x128", "256x144", "288x160", "320x176", "352x192", "384x224"},
                      s.value("tx/resolution", "256x144").toString());
    fps_ = new Picker("Frames a second", {"1", "2", "4", "6", "8", "10", "12", "15"}, s.value("tx/fps", "6").toString());
    audio_ = new Segmented({{"codec2", "Codec2"}, {"opus", "Opus"}}, s.value("tx/audio_codec", "codec2").toString());
    auto* pic = new QWidget;
    pic->setObjectName("seg");
    auto* pl = new QHBoxLayout(pic);
    pl->setContentsMargins(0, 0, 0, 0);
    pl->setSpacing(ui.px(4));
    pl->addWidget(res_, 3);
    pl->addWidget(fps_, 2);
    auto* lg = new QGridLayout;
    lg->setHorizontalSpacing(ui.px(8));
    lg->setVerticalSpacing(ui.px(6));
    lg->addWidget(rowLabel("Picture"), 0, 0);
    lg->addWidget(pic, 0, 1);
    // the C920's focus, between the preview and the picture settings
    focusRow_ = new QWidget;
    focusRow_->setObjectName("seg");
    auto* fl = new QHBoxLayout(focusRow_);
    fl->setContentsMargins(0, 0, 0, 0);
    fl->setSpacing(ui.px(6));
    focusMode_ = new Segmented({{"auto", "Auto"}, {"manual", "Manual"}}, s.value("tx/focus_mode", "auto").toString());
    focus_ = new Stepper(0, 250, 5, s.value("tx/focus", 0).toDouble());
    focus_->setArrowWidth(ui.px(40));   // (a narrow row: the value needs its room)
    fl->addWidget(rowLabel("Focus"));
    fl->addWidget(focusMode_, 1);
    fl->addWidget(focus_, 1);
    left->addWidget(focusRow_);
    focusTimer_.setSingleShot(true);
    connect(&focusTimer_, &QTimer::timeout, this, &TxPage::applyFocus);
    auto focusChanged = [this] {
        settings().setValue("tx/focus_mode", focusMode_->value());
        settings().setValue("tx/focus", focus_->value());
        focus_->setEnabled(focusMode_->value() == "manual");
        focusTimer_.start(100);       // newest wins
    };
    connect(focusMode_, &Segmented::changed, this, focusChanged);
    connect(focus_, &Stepper::changed, this, focusChanged);
    focus_->setEnabled(focusMode_->value() == "manual");
    left->addLayout(lg);
    left->addStretch(1);
    // rates, link, warning, status (touch_tx_page._refresh_info)
    rates_ = new ElideLabel;
    rates_->setObjectName("value");
    linkInfo_ = new ElideLabel;
    linkInfo_->setObjectName("dim");
    warn_ = new ElideLabel;
    warn_->setObjectName("warn");
    status_ = new ElideLabel("Idle");
    status_->setObjectName("dim");
    for (QWidget* w : std::initializer_list<QWidget*>{rates_, linkInfo_, warn_, status_}) left->addWidget(w);
    root->addLayout(left, 1);       // half the page (the preview about the RX picture.s size)
    auto pickSaved = [this] {
        settings().setValue("tx/resolution", res_->value());
        settings().setValue("tx/fps", fps_->value());
        settings().setValue("tx/audio_codec", audio_->value());
        if (!link_.running()) showPlan();
    };
    connect(res_, &Picker::changed, this, pickSaved);
    connect(fps_, &Picker::changed, this, pickSaved);
    connect(audio_, &Segmented::changed, this, pickSaved);

    auto* rightW = new QWidget;
    auto* right = new QVBoxLayout(rightW);
    right->setContentsMargins(0, 0, 0, 0);
    auto* grid = new QGridLayout;
    grid->setHorizontalSpacing(ui.px(8));
    grid->setVerticalSpacing(ui.px(6));
    mode_ = new Segmented(pairs(MODES), s.value("tx/mode", "A").toString());
    bw_ = new Segmented(pairs(BANDWIDTHS), s.value("tx/bw", "80").toString());
    mod_ = new Segmented(MODULATIONS, s.value("tx/mod", "qpsk").toString());
    video_src_ = new Segmented({{"camera", "Camera"}, {"image", "Image"}, {"test", "Test"}, {"off", "Off"}},
                               s.value("tx/video", "test").toString());
    freq_ = new FreqButton("TX frequency", s.value("tx/freq_hz", DEFAULT_FREQ_HZ).toDouble());
    radio_pick_ = new RadioPicker("TX radio (connected now)", false, s.value("tx/radio", "virtual").toString(),
                                  s.value("tx/uri").toString());
    port_ = new Picker("LimeSDR TX port", LIME_TX_PORTS, s.value("tx/port", "Auto").toString());
    gain_ = new Stepper(-89, 0, 1, s.value("tx_gain", -10).toDouble(), " dB");
    int r = 0;
    for (auto p : QList<QPair<QString, QWidget*>>{{"Freq", radioRow(freq_, radio_pick_, port_)}, {"TX gain", gain_},
                                                   {"Audio", audio_}, {"Mode", mode_}, {"kHz", bw_}, {"Mod", mod_}}) {
        grid->addWidget(rowLabel(p.first), r, 0);
        grid->addWidget(p.second, r++, 1);
    }
    lg->addWidget(rowLabel("Video"), 1, 0);   // the source, under the picture (Audio: on the right)
    lg->addWidget(video_src_, 1, 1);
    connect(video_src_, &Segmented::changed, this, [this] { updateFocusRow(); });
    updateFocusRow();
    auto showRadio = [this] {
        port_->setVisible(radio_pick_->driver() == "lime");
        double lo, hi;
        radioRange(radio_pick_->driver(), &lo, &hi);
        freq_->setRange(lo, hi, radioName(radio_pick_->driver()));
    };
    showRadio();
    connect(radio_pick_, &RadioPicker::radioChanged, this, [this, showRadio](const QString& d, const QString& uri) {
        settings().setValue("tx/radio", d);
        settings().setValue("tx/uri", uri);
        showRadio();
        if (!freq_->inRange()) Notices::get().add("TX", freq_->outOfRange() + " -- set a new TX frequency");
    });
    connect(port_, &Picker::changed, this, [](const QString& v) { settings().setValue("tx/port", v); });
    connect(freq_, &FreqButton::changed, this, [](double hz) { settings().setValue("tx/freq_hz", hz); });
    // TX gain: live -- the in-process transmitter directly, the Python one through its file
    connect(gain_, &Stepper::changed, this, [this](double v) {
        link_.setGain(v);
        settings().setValue("tx_gain", v);
        QFile f(QDir::tempPath() + "/avm_gui_tx_gain.txt");
        if (f.open(QIODevice::WriteOnly | QIODevice::Truncate)) f.write(QByteArray::number(v));
    });
    right->addLayout(grid);
    right->addStretch(1);
    auto* runRow = new QHBoxLayout;
    run_ = new QPushButton("TX");
    run_->setObjectName("run");
    msg_ = new QPushButton(QString::fromUtf8("\u2709"));
    msg_->setObjectName("msg");
    runRow->addWidget(run_, 4);
    runRow->addWidget(msg_, 1);
    msg_->setToolTip("The text message sent with the stream");
    connect(msg_, &QPushButton::clicked, this, &TxPage::editMessage);
    updateMsgButton();
    right->addLayout(runRow);
    root->addWidget(rightW, 1);

    auto save = [this] {
        QSettings& s = settings();
        s.setValue("tx/mode", mode_->value());
        s.setValue("tx/bw", bw_->value());
        s.setValue("tx/mod", mod_->value());
        s.setValue("tx/video", video_src_->value());
    };
    for (auto* w : {mode_, bw_, mod_, video_src_}) connect(w, &Segmented::changed, this, save);
    connect(mode_, &Segmented::changed, this, [this] { enforceModeLimits(mode_, bw_, mod_); });
    for (auto* w : {mode_, bw_, mod_}) connect(w, &Segmented::changed, this, &TxPage::linkChanged);
    applyModeType();
    linkChanged();     // the picture size, frame rate and audio for the mode loaded (or the defaults)
    connect(run_, &QPushButton::clicked, this, &TxPage::toggleRun);
    connect(&link_, &Link::line, this, &TxPage::onLine);
    connect(&link_, &Link::stateChanged, this, [this](bool on) {
        if (on && mediaStart_) { auto f = mediaStart_; mediaStart_ = nullptr; f(); }   // the sources and framer
        if (!on) {
            mediaStart_ = nullptr;
            media_.stop();
            preview_->clearFrame("Camera preview");
            if (!status_->text().startsWith("ERROR")) status_->setText(QString("Stopped (%1 fragments sent)").arg(sent_));
        }
    });
    for (auto* w : {mode_, bw_, mod_, video_src_, audio_}) connect(w, &Segmented::changed, this, &TxPage::markPending);
    for (auto* w : {res_, fps_, port_}) connect(w, &Picker::changed, this, &TxPage::markPending);
    connect(freq_, &FreqButton::changed, this, &TxPage::markPending);
    connect(radio_pick_, &RadioPicker::radioChanged, this, &TxPage::markPending);
    // the picture being sent (the C++ chain), passed to the GUI thread
    media_.setPreview([this](const QImage& img) {
        QMetaObject::invokeMethod(this, [this, img] { preview_->setFrame(img); }, Qt::QueuedConnection);
    });
    connect(&link_, &Link::stateChanged, this, [this](bool on) {
        if (!on) pending_ = false;
        if (on || !restarting_) {
            run_->setText(on ? "STOP" : "TX");
            run_->setStyleSheet(on ? QString("background: %1;").arg(STOP) : QString());
        } else run_->setText("STARTING...");
        emit runningChanged(on);
    });
}

// the text message (80 characters): sent until cleared, changed live
void TxPage::editMessage() {
    KeyboardDialog d("Message to send (empty: none)", settings().value("tx/message").toString(), 80, this,
                     KeyboardDialog::MESSAGE_ROWS, true);
    if (!d.exec()) return;
    QString t = d.text().left(80);
    settings().setValue("tx/message", t);
    media_.setMessage(t);
    updateMsgButton();
}

void TxPage::updateMsgButton() {
    bool on = !settings().value("tx/message").toString().isEmpty();
    msg_->setStyleSheet(on ? QString("background: %1; color: white;").arg(TX_ACCENT) : QString());
}

// the link's own payload rate (libavm), and what it carries (link_plan.h)
double TxPage::linkBps() const {
    using Fn = double (*)(const char*, double, const char*, const char*, int, double);
    static Fn fn = nullptr;
    static bool tried = false;
    if (!tried) {
        tried = true;
        static QLibrary lib(avmDir() + "/c++/avm");
        if (lib.isLoaded() || lib.load()) fn = (Fn)lib.resolve("avm_link_bitrate");
    }
    if (!fn) return 0;
    const int frag = fragmentSize(bw_->value().toDouble());
    double bps = fn(mode_->value().toUtf8().constData(), bw_->value().toDouble(), mod_->value().toUtf8().constData(), "ldpc",
                    frag, 0);
    // duplex: one slot a cycle (touch_widgets / media_tx_gui._link_bps)
    if (!duplexRole().isEmpty() && bps > 0) {
        int per_slot = std::max(1, (int)(TDD_SLOT_S / (frag * 8 / bps)));
        bps = frag * 8.0 * per_slot / TDD_CYCLE_S;
    }
    return bps;
}

void TxPage::refreshDuplex() {
    const bool duplex = !duplexRole().isEmpty();
    run_->setVisible(!duplex || running());
    if (duplex && !running()) status_->setText("Duplex: start and stop on the RX page (TX+RX)");
    else if (!duplex && status_->text().startsWith("Duplex:")) status_->setText("");
}

LinkPlan TxPage::plan() const {
    double khz = bw_->value().toDouble();
    return planLink(linkBps(), fragmentSize(khz), khz, !callsign().isEmpty());
}

LinkPlan TxPage::planFor() const {
    double khz = bw_->value().toDouble();
    QStringList wh = res_->value().split('x');
    return planLinkFor(linkBps(), fragmentSize(khz), khz, !callsign().isEmpty(), audio_->value(),
                       wh.value(0).toInt(), wh.value(1).toInt(), fps_->value().toDouble(),
                       settings().value("tx/content", "auto").toString());
}

void TxPage::showPlan() { refreshInfo(); }

void TxPage::refreshInfo() {
    LinkPlan p = planFor();
    double audio = p.audio_codec == "opus" ? p.opus_kbps : 3.2;
    const QString content = settings().value("tx/content", "auto").toString();
    if (!p.audio)
        rates_->setText(QString("Video %1 kbps   NO AUDIO").arg(p.video_kbps, 0, 'f', 1));
    else if (p.video && video_src_->value() != "off")
        rates_->setText(QString("Video %1 kbps   Audio %2 kbps").arg(p.video_kbps, 0, 'f', 1).arg(audio, 0, 'f', 1));
    else
        rates_->setText(QString("%1   Audio %2 kbps").arg(content == "audio" ? "AUDIO ONLY" : "NO VIDEO").arg(audio, 0, 'f', 1));
    linkInfo_->setText(p.link_kbps > 0 ? QString("Link %1 kbps  \u00b7  %2 @ %3 fps")
                                             .arg(p.usable_kbps, 0, 'f', 1).arg(res_->value()).arg(fps_->value())
                                       : QString("Link --"));
    QString w;
    if (p.link_kbps > 0 && !p.video && video_src_->value() != "off" && content != "audio")
        w = "NO VIDEO: link too slow, sending audio only -- use a wider kHz or faster mode";
    warn_->setText(w);
    warn_->setVisible(!w.isEmpty());
}

// a new link: the picture and audio it suits (touch_tx_page._changed)
void TxPage::linkChanged() {
    LinkPlan p = plan();
    if (p.link_kbps > 0) {
        res_->setValue(QString("%1x%2").arg(p.width).arg(p.height));
        fps_->setValue(QString::number(p.fps));
        audio_->setValue(p.audio_codec);
        settings().setValue("tx/resolution", res_->value());
        settings().setValue("tx/fps", fps_->value());
        settings().setValue("tx/audio_codec", audio_->value());
    }
    if (!link_.running()) showPlan();
}

// the camera's v4l2 device, if it's a Logitech C920 and v4l2-ctl is there: "" otherwise
static QString c920Device() {
#ifdef Q_OS_WIN
    return QString();
#else
    QString dev = settings().value("tx/video_device", "/dev/video0").toString();
    if (dev.isEmpty()) dev = "/dev/video0";
    QFile f("/sys/class/video4linux/" + QFileInfo(dev).fileName() + "/name");
    if (!f.open(QIODevice::ReadOnly) || !f.readAll().contains("C920")) return QString();
    if (QStandardPaths::findExecutable("v4l2-ctl").isEmpty()) return QString();
    return dev;
#endif
}

void TxPage::updateFocusRow() {
    focusRow_->setVisible(video_src_->value() == "camera" && !c920Device().isEmpty());
}

// two calls: the C920 refuses focus_absolute while autofocus is on, and a combined one fails whole
void TxPage::applyFocus() {
    QString dev = c920Device();
    if (dev.isEmpty()) return;
    QString cmd = focusMode_->value() == "auto"
                      ? QString("v4l2-ctl -d %1 -c focus_automatic_continuous=1").arg(dev)
                      : QString("v4l2-ctl -d %1 -c focus_automatic_continuous=0; v4l2-ctl -d %1 -c focus_absolute=%2")
                            .arg(dev).arg((int)focus_->value());
    QProcess::startDetached("sh", {"-c", cmd});
}

void TxPage::showEvent(QShowEvent* e) {
    QWidget::showEvent(e);
    updateFocusRow();
}

void TxPage::resizeEvent(QResizeEvent* e) {
    QWidget::resizeEvent(e);
    // the left column's width, less its margins: its 16:9 height
    QMetaObject::invokeMethod(this, [this] { preview_->setFixedHeight(preview_->width() * 9 / 16); }, Qt::QueuedConnection);
}

void TxPage::markPending() {
    if (!running()) return;
    pending_ = true;
    run_->setText("RESTART TO APPLY");
    run_->setStyleSheet(QString("background: %1; color: #111;").arg(WARN));
}

void TxPage::applyModeType() {
    markPending();
    QString before = mode_->value() + bw_->value() + mod_->value();
    avm::applyModeType(mode_, bw_, mod_);
    settings().setValue("tx/mode", mode_->value());
    settings().setValue("tx/bw", bw_->value());
    settings().setValue("tx/mod", mod_->value());
    if (mode_->value() + bw_->value() + mod_->value() != before) linkChanged();
    else if (!link_.running()) showPlan();     // (Config's Content may have changed)
}

QString TxPage::modeSummary() const {
    return QString("%1 %2 kHz %3").arg(mode_->value(), bw_->value(), mod_->value().toUpper());
}

// The transmitter alone for now (warm-up filler, then stdin); the media
// framer and sources in front of it are the next step.
// The media chain (for now the Python framer and sources, as AVM runs them:
// C++ capture with the FFmpeg libraries is the plan) into the transmitter
// -- hf_ofdm_tx.py on any radio, or avm_tx on a SoapySDR one.
static QString quoteArg(const QString& a) { return a.contains(' ') ? '"' + a + '"' : a; }

// the press: STARTING / STOPPING / RESTARTING at once, and the work on the
// next turn of the event loop -- opening a radio takes a second or two, and a
// change made in the same call wasn't drawn until it was over
void TxPage::toggleRun() {
    run_->setText(pending_ && running() ? "RESTARTING..." : running() ? "STOPPING..." : "STARTING...");
    run_->setStyleSheet(QString("background: %1; color: %2;").arg(BUTTON, TEXT));
    QTimer::singleShot(30, this, [this] {
        doToggleRun();
        if (!running() && !restarting_) { run_->setText("TX"); run_->setStyleSheet(QString()); }   // stopped, or didn't start
    });
}

void TxPage::doToggleRun() {
    if (pending_ && running()) {     // restart to apply: stop, then start with the new settings
        pending_ = false;
        restarting_ = true;          // (the button says STARTING... meanwhile, not TX)
        doToggleRun();
        QTimer::singleShot(300, this, [this] {
            restarting_ = false;
            run_->setText("STARTING...");
            if (!running()) doToggleRun();
            if (!running()) { run_->setText("TX"); run_->setStyleSheet(QString()); }
        });
        return;
    }
    if (link_.running() || media_.running()) {
        media_.stop();
        link_.stop();
        return;
    }
    if (!freq_->inRange()) {     // the driver would quietly open at the nearest frequency it can tune
        status_->setText("Not started: " + freq_->outOfRange());
        Notices::get().add("TX", "Not started: " + freq_->outOfRange() + " -- set a new TX frequency");
        return;
    }
    QSettings& s = settings();
    sent_ = 0;
    const double khz = bw_->value().toDouble();
    const QString frag = QString::number(fragmentSize(khz));
    // the rates for the link and the pickers' choices (always worked out, never set by hand)
    LinkPlan lp = planFor();
    if (!lp.ok) { status_->setText("Link: no such mode"); return; }
    s.setValue("tx/opus_kbps", lp.opus_kbps);
    s.setValue("tx/video_kbps", lp.frame_bytes * 8 * lp.fps / 1000);
    refreshInfo();
    status_->setText("Starting...");
    QStringList args = {"--mode", mode_->value(), "--occupancy", bw_->value(), "--modulation", mod_->value(),
                        "--fec", "ldpc", "--stream", "--fragment-size", frag, "--fragment-gap-ms", "0",
                        "--sample-rate", QString::number(sdrRate(khz)), "--max-input-backlog", "8",
                        "--output", "pluto", "--tx-gain", QString::number(gain_->value())};
    // as touch_tx_page: one fragment built ahead of the radio (each queued one is a
    // whole fragment-time of delay -- the default 8 was ~0.9 s at 250 kHz); the
    // the LO offset as the receiver's, in every mode (its leakage away from the
    // signal; the Python tuned OFDM modes' LO on the signal); the single-carrier
    // modes' amplitude
    const bool sc = mode_->value() == "S" || mode_->value() == "SH";
    args << "--tx-queue-depth" << "1" << "--amplitude" << (sc ? "0.3" : "0.2");
    if (!duplexRole().isEmpty()) {
        // duplex: to the RX page's radio (the receiver transmits), in slots
        args[args.indexOf("--output") + 1] = "tdd";
        args += tddArgs();
    } else {
        args += radioArgs("tx", khz, freq_->hz(), true);
    }
    // TX peak reduction (Config, Experimental): clip and filter, receiver-transparent
    if (s.value("experimental", false).toBool() && s.value("tx/papr_db", 0).toDouble() > 0)
        args << "--papr-db" << s.value("tx/papr_db").toString();
    {   // the transmitter and the sources + framer in front of it, in-process
        MediaTxConfig c;
        c.fragment_size = frag.toInt();
        c.station_id = callsign().toStdString();
        c.video = video_src_->value().toStdString();
        if (lp.ok && !lp.video) c.video = "off";
        c.audio = video_src_->value() == "camera" ? "mic" : "test";   // (the camera's microphone even with video off)
        if (lp.ok && !lp.audio) c.audio = "off";                       // video only (Config: Content)
        c.audio_codec = s.value("tx/audio_codec", "codec2").toString().toStdString();
        c.opus_kbps = s.value("tx/opus_kbps", 10).toDouble();
        c.video_device = s.value("tx/video_device").toString().toStdString();
        c.image_path = s.value("tx/image").toString().toStdString();
        if (c.video == "image" && c.image_path.empty()) {
            status_->setText("Config: choose a picture (TX media)");
            return;
        }
        c.audio_device = s.value("tx/audio_device").toString().toStdString();
#ifndef Q_OS_WIN
        // an output's monitor (a loopback) or nothing: the default microphone
        if (c.audio_device.empty() || QString::fromStdString(c.audio_device).endsWith(".monitor")) c.audio_device = "default";
#endif
        QStringList wh = s.value("tx/resolution", "256x144").toString().toLower().split('x');
        c.width = wh.value(0).toInt();
        c.height = wh.value(1).toInt();
        c.fps = s.value("tx/fps", 6).toDouble();
        c.frame_bytes = (int)std::floor(s.value("tx/video_kbps", 20.0).toDouble() * 1000 / 8 / c.fps + 1e-6);
        c.mono = s.value("tx/mono", false).toBool();     // Config, TX media: every bit to brightness
        if (lp.ok) {
            c.frame_bytes = lp.frame_bytes;
            c.fragment_period_s = lp.fragment_s;     // the framer ticks once a fragment
        }
        if (c.audio == "mic" && c.audio_device.empty()) {
            status_->setText("Config: choose a microphone (TX media)");
            return;
        }
        link_.startTx(args);
        if (!link_.running()) return;
        // the sources and framer once the transmitter is up (it's created on a worker thread)
        mediaStart_ = [this, c] {
            media_.setMessage(settings().value("tx/message").toString());
            bool ok = media_.start(c,
                [this](const std::vector<uint8_t>& f) {
                    QByteArray b((const char*)f.data(), (int)f.size());
                    QMetaObject::invokeMethod(this, [this, b] { link_.write(b); }, Qt::QueuedConnection);
                },
                [this](const std::string& t) {
                    QString l = QString::fromStdString(t);
                    QMetaObject::invokeMethod(this, [this, l] {
                        guiLog("MEDIA", l);
                        if (l.startsWith("ERROR")) status_->setText(l);
                        Notices::get().addIfError("TX media", l);
                    }, Qt::QueuedConnection);
                });
            if (!ok) link_.stop();
        };
    }
}

void TxPage::onLine(const QString& t) {
    guiLog("TX", t);
    if (t.contains("Streamed fragment")) {
        ++sent_;
        // "Streamed fragment seq=N (B bytes, D ms)": the count and each one's air time
        static const QRegularExpression re(R"(\(\d+ bytes, ([\d.]+) ms\))");
        auto m = re.match(t);
        // the framer paced by it (C++ media; not the warmup's filler) -- but not in
        // duplex: there the fragments go out in bursts, in this station's slot, and
        // paced by one fragment's air time the framer made twice what the slot
        // carries (the transmitter dropped the rest: half the pictures and audio)
        if (m.hasMatch() && !t.contains("[warmup]") && duplexRole().isEmpty())
            media_.setFragmentPeriod(m.captured(1).toDouble() / 1000);
        status_->setText(QString("%1 fragments sent").arg(sent_));
    } else if (t.contains("[tdd] cycle") && t.contains("--fragment-period-s")) {
        // duplex: the transmitter's slot plan says what the framer's period must be
        // ("... framer period for this: --fragment-period-s 0.2500")
        static const QRegularExpression re(R"(--fragment-period-s ([\d.]+))");
        auto m = re.match(t);
        if (m.hasMatch()) media_.setFragmentPeriod(m.captured(1).toDouble());
    } else if (t.startsWith("ERROR")) {
        status_->setText(t);
    } else if (t.startsWith("Warmup:")) {
        status_->setText("Warming up the transmitter...");
    }
}

QString TxPage::callsign() { return settings().value("callsign").toString().trimmed(); }

void TxPage::callsignChanged() {
    if (link_.running()) markPending();
    else showPlan();
}

// ---------------------------------------------------------------- Config page
ConfigPage::ConfigPage(QWidget* parent) : QWidget(parent) {
    auto* root = new QHBoxLayout(this);
    root->setContentsMargins(ui.px(8), ui.px(4), ui.px(8), ui.px(8));
    list_ = new QListWidget;
    list_->setFixedWidth(ui.px(200));
    stack_ = new QStackedWidget;
    root->addWidget(list_);
    root->addWidget(stack_, 1);
    QSettings& s = settings();

    // Modes: which the TX and RX pages offer
    auto* modes = new QWidget;
    auto* mof = new QFormLayout(modes);
    auto* mtype = new Segmented({{"hf", "HF"}, {"vu", "VHF / UHF"}, {"sat", "Satellite"}},
                                s.value("mode_type", "hf").toString());
    mof->addRow(rowLabel("Mode type"), mtype);
    auto* mnote = new QLabel("HF: modes A-D and SH.  VHF / UHF: mode VU, 80-250 kHz.  Satellite: mode S.");
    mnote->setObjectName("dim");
    mnote->setWordWrap(true);
    mof->addRow(mnote);
    connect(mtype, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("mode_type", v);
        emit modeTypeChanged();
    });

    // Station: the callsign sent with the pictures (shown by the receivers)
    auto* station = new QWidget;
    auto* sf = new QFormLayout(station);
    auto* call = new TextButton("Callsign", s.value("callsign").toString(), 16, "(callsign)");
    sf->addRow(rowLabel("Callsign"), call);
    auto* snote = new QLabel("Sent with each transmission; the receiving station shows it above the picture.");
    snote->setObjectName("dim");
    snote->setWordWrap(true);
    sf->addRow(snote);
    connect(call, &TextButton::changed, this, [this](const QString& t) {
        settings().setValue("callsign", t);
        emit callsignChanged();
    });

    // TX media (the sources)
    auto* media = new QWidget;
    auto* mf = new QFormLayout(media);
    mf->setVerticalSpacing(ui.px(6));
#ifdef Q_OS_WIN
    const char* vfmt = "dshow";
    const char* afmt = "dshow";
#else
    const char* vfmt = "v4l2";
    const char* afmt = "pulse";
#endif
    auto devices = [](const char* fmt, bool video) {
        return [fmt, video] {
            QStringList l;
#ifndef Q_OS_WIN
            if (!video) l << "default";       // the system's default microphone
#endif
            for (const auto& d : Capture::devices(fmt, video)) {
                QString n = QString::fromStdString(d);
                // PulseAudio lists every output's monitor (a loopback of what it plays) as a source
                if (!video && n.endsWith(".monitor")) continue;
                l << n;
            }
            if (l.isEmpty()) l << "(none found)";
            return l;
        };
    };
    auto* content = new Segmented({{"auto", "Auto"}, {"audio", "Audio only"}, {"video", "Video only"}},
                                  s.value("tx/content", "auto").toString());
    mf->addRow(rowLabel("Content"), content);
    connect(content, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("tx/content", v);
        emit modeTypeChanged();       // (the TX page re-plans)
    });
    auto* cam = new Picker("Camera", devices(vfmt, true), s.value("tx/video_device").toString());
    QString savedMic = s.value("tx/audio_device").toString();
#ifndef Q_OS_WIN
    if (savedMic.isEmpty() || savedMic.endsWith(".monitor")) savedMic = "default";   // (a loopback isn't a microphone)
#endif
    auto* mic = new Picker("Microphone", devices(afmt, false), savedMic);
    connect(cam, &Picker::changed, this, [](const QString& v) { settings().setValue("tx/video_device", v); });
    connect(mic, &Picker::changed, this, [](const QString& v) { settings().setValue("tx/audio_device", v); });
    mf->addRow(rowLabel("Camera"), cam);
    mf->addRow(rowLabel("Microphone"), mic);
    auto* mono = new Segmented({{"colour", "Colour"}, {"bw", "Black and white"}},
                               s.value("tx/mono", false).toBool() ? "bw" : "colour");
    mf->addRow(rowLabel("Video"), mono);
    connect(mono, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("tx/mono", v == "bw");
        emit txSettingChanged();
    });
    // the automatic plan's picture on links up to 20 kHz (link_plan.h)
    auto* motion = new Segmented({{"sharp", "Sharp"}, {"smooth", "Smooth"}}, videoSmooth() ? "smooth" : "sharp");
    mf->addRow(rowLabel("Motion"), motion);
    connect(motion, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("tx/video_smooth", v == "smooth");
        setVideoSmooth(v == "smooth");
        emit txSettingChanged();
        emit motionChanged();     // the TX page re-plans its picture size and frame rate
    });
    {   // the picture for the Image source
        auto* img = new ElideLabel(s.value("tx/image").toString().isEmpty() ? QString("(none)") : s.value("tx/image").toString());
        image_ = img;
        auto* browse = new QPushButton("Choose");
        auto* row = new QHBoxLayout;
        row->addWidget(img, 1);
        row->addWidget(browse);
        mf->addRow(rowLabel("Image"), row);
        connect(browse, &QPushButton::clicked, this, [this, img] {
            QString f = QFileDialog::getOpenFileName(this, "Picture to send", settings().value("tx/image").toString(),
                                                     "Pictures (*.png *.jpg *.jpeg *.bmp *.webp)");
            if (f.isEmpty()) return;
            img->setText(f);
            settings().setValue("tx/image", f);
        });
    }
    // Receiver: the spectrum's averaging, its inversion, the sound output, the KiwiSDR to receive from
    auto* receiver = new QWidget;
    auto* rf = new QFormLayout(receiver);
    rf->setVerticalSpacing(ui.px(6));
    QString avg = s.value("rx/spec_avg", 1).toString();
    auto* avgs = new Segmented({{"1", "None"}, {"4", "4"}, {"8", "8"}, {"16", "16"}, {"32", "32"}}, avg);
    rf->addRow(rowLabel("Spectrum avg"), avgs);
    connect(avgs, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("rx/spec_avg", v.toInt());
        emit spectrumAveragesChanged(v.toInt());
    });
    // the receive path mirrors the spectrum (a high-side-LO converter, some radios):
    // the receiver un-mirrors it (--invert-spectrum)
    auto* inv = new Segmented({{"normal", "Normal"}, {"inverted", "Inverted"}},
                              s.value("rx/invert", false).toBool() ? "inverted" : "normal");
    rf->addRow(rowLabel("RX spectrum"), inv);
    auto* inote = new QLabel("Inverted: for a receive path that mirrors the signal (a converter with its LO "
                             "above it). Sync is found but every header fails when this is wrong.");
    inote->setObjectName("dim");
    inote->setWordWrap(true);
    rf->addRow(inote);
    connect(inv, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("rx/invert", v == "inverted");
        emit rxSettingChanged();
    });
    // the sound output
    auto outputs = [] {
        QStringList l = {"Default"};
        for (const auto& n : AudioOut::outputs()) l << QString::fromStdString(n);
        return l;
    };
    QString cur = s.value("audio_device").toString();
    auto* outs = new Picker("Audio output", outputs, cur.isEmpty() ? QString("Default") : cur);
    rf->addRow(rowLabel("Audio out"), outs);
    auto* note = new QLabel("Audio out: takes effect at the next start of the GUI");
    note->setObjectName("dim");
    rf->addRow(note);
    connect(outs, &Picker::changed, this, [](const QString& t) {
        settings().setValue("audio_device", t == "Default" ? QString() : t);
    });
    auto* kiwi = new TextButton("KiwiSDR server (host:port)", s.value("kiwi_server").toString(), 40,
                                "(none -- e.g. KIWI.EXAMPLE.ORG:8073)");
    rf->addRow(rowLabel("KiwiSDR"), kiwi);
    kiwi_ = kiwi;
    auto* knote = new QLabel("Then pick \"KiwiSDR\" as the RX radio. A KiwiSDR carries the 10 kHz modes only.");
    knote->setObjectName("dim");
    knote->setWordWrap(true);
    rf->addRow(knote);
    connect(kiwi, &TextButton::changed, this, [this](const QString& t) {
        settings().setValue("kiwi_server", t.trimmed());
        emit rxSettingChanged();
    });

    // Experimental: can badly distort a transmission if misused -- off until switched on (asks first)
    auto* exp = new QWidget;
    auto* ef = new QFormLayout(exp);
    ef->setVerticalSpacing(ui.px(6));
    const bool expOn = s.value("experimental", false).toBool();
    auto* expSw = new Segmented({{"off", "Off"}, {"on", "On"}}, expOn ? "on" : "off");
    ef->addRow(rowLabel("Experimental"), expSw);
    auto* papr = new Segmented({{"0", "Off"}, {"7", "On"}}, expOn ? s.value("tx/papr_db", "0").toString() : QString("0"));
    papr->setEnabled(expOn);
    ef->addRow(rowLabel("TX peak reduce"), papr);
    auto* enote = new QLabel("TX peak reduction: clip and filter (7 dB), invisible to the receiver -- "
                             "measured 2-4 dB less signal needed in mode A 10 kHz. About 15% of a core at VU 160.");
    enote->setObjectName("dim");
    enote->setWordWrap(true);
    ef->addRow(enote);
    auto* dup = new Segmented({{"off", "Off"}, {"master", "Master"}, {"slave", "Slave"}},
                              expOn ? s.value("duplex", "off").toString() : QString("off"));
    dup->setEnabled(expOn);
    ef->addRow(rowLabel("Duplex"), dup);
    auto* dnote = new QLabel("Duplex: TX and RX on one channel in turns, one PlutoSDR doing both (VU modes, TX and RX "
                             "set alike). One station Master, the other Slave; start with TX+RX on the RX page.");
    dnote->setObjectName("dim");
    dnote->setWordWrap(true);
    ef->addRow(dnote);
    connect(dup, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("duplex", v);
        if (v != "off" && !duplexWhyNot().isEmpty())
        {
            QMessageBox box(QMessageBox::Information, "Duplex", "Duplex " + duplexWhyNot() + ".", QMessageBox::Ok, this);
            embedDialog(&box, this);
            box.exec();
        }
        emit duplexChanged();
    });
    connect(expSw, &Segmented::changed, this, [this, expSw, papr, dup](const QString& v) {
        if (v == "on" && !settings().value("experimental", false).toBool()) {
            QMessageBox box(QMessageBox::Warning, "Enable experimental features?",
                            "These settings can severely distort transmissions if used incorrectly.\n\n"
                            "Don't use them unless you understand how to monitor your transmissions before use on air.",
                            QMessageBox::NoButton, this);
            auto* yes = box.addButton("Enable", QMessageBox::AcceptRole);
            box.addButton("Cancel", QMessageBox::RejectRole);
            embedDialog(&box, this);
            box.exec();
            if (box.clickedButton() != yes) { expSw->setValue("off"); return; }
        }
        const bool on = v == "on";
        settings().setValue("experimental", on);
        papr->setEnabled(on);
        dup->setEnabled(on);
        if (!on) { papr->setValue("0"); settings().setValue("tx/papr_db", 0); }   // switched on again by hand
        if (!on) { dup->setValue("off"); settings().setValue("duplex", "off"); emit duplexChanged(); }
        emit txSettingChanged();
    });
    connect(papr, &Segmented::changed, this, [this](const QString& v) {
        settings().setValue("tx/papr_db", v.toInt());
        emit txSettingChanged();
    });

    // About
    auto* about = new QWidget;
    auto* al = new QVBoxLayout(about);
    auto* at = new QLabel(QString("AVM GUI (C++, Qt %1)\n%2 layout").arg(qVersion(), ui.touch ? "Touch" : "Desktop"));
    at->setObjectName("value");
    al->addWidget(at);
    auto* ab = new QLabel(
        "<p><b>AVM -- Audio Video Modem</b> sends live pictures, sound and short text messages over a radio "
        "link: a camera and microphone at one end, the picture and sound at the other, through an SDR "
        "(PlutoSDR, LimeSDR, HPSDR / Radioberry; KiwiSDR to receive). Wider channels and denser modulation "
        "(QPSK, then 16QAM, 32QAM) carry more, but need a stronger signal.</p>"
        "<p><b>Which mode when</b> (Config, Modes):</p>"
        "<ul>"
        "<li><b>A</b> -- HF ground wave, short steady paths.</li>"
        "<li><b>B</b> -- HF sky wave: the general-purpose HF mode.</li>"
        "<li><b>C</b> -- HF long distance, more fading and Doppler.</li>"
        "<li><b>D</b> -- HF with severe echoes and Doppler (NVIS, poor conditions).</li>"
        "<li><b>SH</b> -- HF single carrier, 10-40 kHz: about 8-10 kbit/s in 10 kHz.</li>"
        "<li><b>VU</b> -- VHF / UHF (144 / 432 MHz), 80-250 kHz: line of sight, mobile and troposcatter.</li>"
        "<li><b>S</b> -- single carrier for fixed and satellite paths, 10-40 kHz.</li>"
        "</ul>"
        "<p>AVM was created mainly by Claude, Anthropic's AI, working with Rob M0DTS.</p>");
    ab->setWordWrap(true);
    ab->setTextFormat(Qt::RichText);
    ab->setAlignment(Qt::AlignTop | Qt::AlignLeft);
    auto* scroll = new QScrollArea;
    scroll->setWidget(ab);
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    QScroller::grabGesture(scroll->viewport(), QScroller::LeftMouseButtonGesture);   // dragged by a finger
    al->addWidget(scroll, 1);

    for (auto p : QList<QPair<QString, QWidget*>>{{"Station", station}, {"Modes", modes}, {"TX media", media}, {"Receiver", receiver}, {"Experimental", exp}, {"About", about}}) {
        list_->addItem(p.first);
        stack_->addWidget(p.second);
    }
    connect(list_, &QListWidget::currentRowChanged, stack_, &QStackedWidget::setCurrentIndex);
    list_->setCurrentRow(0);
}

void ConfigPage::setKiwi(const QString& server) {
    if (kiwi_) kiwi_->setValue(server);
    settings().setValue("kiwi_server", server.trimmed());
    emit rxSettingChanged();
}

void ConfigPage::setImage(const QString& path) {
    if (image_) image_->setText(path);
    settings().setValue("tx/image", path);
    emit txSettingChanged();
}

}  // namespace avm
