// pages.h -- the three pages (touch_rx_page.py, touch_tx_page.py, the
// config page) and the process wrapper that runs the modem behind them.
#pragma once
#include <QDialog>
#include <QPointer>
#include <atomic>
#include <deque>
#include <thread>
#include <QElapsedTimer>
#include <QLineEdit>
#include <QProcess>
#include <QSettings>
#include <QTimer>
#include <QWidget>

#include "media.h"
#include "media_tx.h"
#include "link_plan.h"
#include "recorder.h"
#include "widgets.h"
#include "touch.h"

class QListWidget;
class QStackedWidget;
class QVBoxLayout;

namespace avm {

// A modem: a C++ engine in-process (startRx / startTx: libavm's avm_rx_* /
// avm_tx_*), its status lines and data passed on; or a helper process, its
// stderr read line by line.
class Link : public QObject {
    Q_OBJECT
public:
    explicit Link(QObject* parent = nullptr);
    ~Link();
    bool running() const { return eng_ || starting_ || proc_.state() != QProcess::NotRunning; }
    bool engineUp() const { return eng_ != nullptr; }     // the in-process engine created (not just starting)
    // stdin_file: fed to the process's stdin (an IQ recording); "|": stdin
    // left open for write(); empty: none
    void start(const QString& program, const QStringList& args, const QString& dir,
               const QString& stdin_file = QString());
    // the C++ receiver in-process, avm_rx's options; iq_file: read into it
    // (an IQ recording); "|": fed by write(); empty: a radio it opens itself
    void startRx(const QStringList& args, const QString& iq_file);
    // the C++ transmitter in-process, avm_tx's options; fed by write()
    void startTx(const QStringList& args);
    void write(const QByteArray& bytes);
    void stop();
    void setGain(double db);    // the in-process engine's radio, live
    QString source;             // its name in the notices ("TX", "RX"...): empty, none
signals:
    void line(const QString& text);
    void data(const QByteArray& bytes);     // its stdout
    void stateChanged(bool running);
private:
    QProcess proc_;
    QByteArray partial_;
    bool stopping_ = false;
    void* eng_ = nullptr;               // the in-process engine
    struct EngineApi* api_ = nullptr;
    // the engine is created on a worker thread (opening a radio takes a second or
    // two, and the GUI stays live meanwhile); input written before it's up waits
    bool starting_ = false, stopWanted_ = false;
    std::thread startTh_;
    QByteArray early_;
    QString iqFile_;
    void engineCreated(void* h, struct EngineApi* api);
    void startFile();
    std::thread file_th_;               // reading an IQ file into it
    std::atomic<bool> file_stop_{false};
    bool startEngine(bool tx, const QStringList& args);
    void engineFinished();
};

// What a link's width sets (touch_widgets.py, so TX and RX always agree):
// the radio's sample rate, the LO offset (the DC spike kept off the signal)
// and the fragment size
int sdrRate(double khz);
double loOffsetHz(double khz);
int fragmentSize(double khz);
// Config's Mode type (touch_widgets.MODE_TYPE_SETS: "hf", "vu", "sat"): the
// modes, widths and modulations a page shows; and each mode's own limits
// (S / SH: 10-40 kHz and their modulations; VU: 80 kHz and up) greyed
void applyModeType(Segmented* mode, Segmented* bw, Segmented* mod);
void enforceModeLimits(Segmented* mode, Segmented* bw, Segmented* mod);
// a radio row: frequency, radio and (with a LimeSDR) its port, side by side
QWidget* radioRow(FreqButton* freq, RadioPicker* radio, Picker* port);

// Shared settings (QSettings: the registry on Windows, ~/.config on Linux;
// an INI file instead after setSettingsFile, before the first use)
QSettings& settings();
void setSettingsFile(const QString& path);
// the session log (--log FILE): every modem status line and GUI event, timestamped
void setLogFile(const QString& path);
void guiLog(const QString& who, const QString& line);
QString avmDir();          // the AVM folder (the one holding c++/ and the library)

// Same-channel duplex (Config, Experimental; touch_widgets.tdd_config): this
// station's role ("master" / "slave"), "" when off or not possible; why not
QString duplexRole();
QString duplexWhyNot();

class TxPage;

class RxPage : public QWidget {
    Q_OBJECT
public:
    explicit RxPage(QWidget* parent = nullptr);
    bool running() const { return link_.running(); }
    // scripted use (--auto): the buttons
    void start() { if (!running()) toggleRun(); }
    void stop() { if (running()) toggleRun(); }
    void record() { if (!recWant_) toggleRecord(); }
    // the web page's Record (the screen has no button for now): 0 RX off, 1 ready, 2 recording
    int recordState() { return recWant_ ? 2 : link_.running() ? 1 : 0; }
    void setRecording(bool on) { if (on != recWant_) toggleRecord(); }
    int pictures() const { return pictures_; }
    int messages() const { return msgs_.size(); }
    QString statusText() const { return status_->text(); }
    void setStatus(const QString& t) { status_->setText(t); }
    QString modeSummary() const;
    QStringList modemArgs() const;
    void setDemo(bool on);
    void updateBand();     // the spectrum's capture band, for the mode / width
    void applyModeType();
    void rxSettingChanged() { markPending(); showKiwiInfo(); }    // (Config's RX spectrum / KiwiSDR: restart to apply)
    void setSpectrumAverages(int n);
    // duplex: this page's button runs TX+RX (the transmitter started once the radio is up)
    void setPeer(TxPage* tx) { peer_ = tx; }
    void refreshDuplex();
protected:
    void resizeEvent(QResizeEvent* e) override;
    bool eventFilter(QObject* o, QEvent* e) override;   // the status line tapped: the constellation
public:
signals:
    void runningChanged(bool running);
private slots:
    void toggleRun();
    void doToggleRun();   // (toggleRun's work, after the button shows it)
    void onLine(const QString& text);
    void updateLamps();
    void demoTick();
    void toggleBigVideo();
    void onMessage(const QString& text);
    void showMessages();
    void toggleRecord();
private:
    // a vital setting changed while running (touch_widgets.RunButton's "pending"):
    // the button says RESTART TO APPLY; pressed, it stops and starts again
    bool pending_ = false;
    bool restarting_ = false;   // stopped for a restart: the button stays STARTING...
    void markPending();
    TxPage* peer_ = nullptr;
    bool duplexRun_ = false;      // running as the duplex receiver (its radio transmits)
    QString runLabel() const { return duplexRole().isEmpty() ? "RX" : "TX+RX"; }
    // recording the received picture and sound (~/recordings, as the web page lists)
    Recorder rec_;
    bool recWant_ = false;
    QPushButton* rec_btn_ = nullptr;
    int pictures_ = 0;
    // the status line (touch_rx_page._status_line): MER measured / needed, CFO, picture rate
    std::deque<double> mer_, cfo_;
    qint64 merT_ = 0;
    int ok_ = 0, bad_ = 0, lost_ = 0;
    std::deque<qint64> frameTimes_;     // pictures in the last second: the fps
    QString merText(bool live) const;
    QString cfoText() const;
    void updateRecButton();
    // text messages received this session (touch_widgets.Messages)
    struct Msg { QString got; qint64 ts; QString call, text; };
    QList<Msg> msgs_;
    int unread_ = 0;
    void updateMsgButton();
    Segmented *mode_, *bw_, *mod_;
    FreqButton* freq_;
    RadioPicker* radio_pick_;
    Picker* port_;            // the LimeSDR's antenna port (shown with a Lime)
    Stepper* gain_;
    Stepper* ppm_;            // RTL-SDR crystal correction (shown with an RTL-SDR)
    Stepper* offset_;         // live receive offset, kHz (its step follows the mode type)
    QLabel* ppmLabel_ = nullptr;
    QLabel* kiwiInfo_ = nullptr;   // KiwiSDR: whose receiver (its /status name)
    QMap<QString, QString> kiwiInfoCache_;
    void showKiwiInfo();
    QPointer<QDialog> constDlg_;   // open while the constellation shows
    Lamp *hdrLamp_, *frameLamp_;
    QLabel* station_;
    ElideLabel* status_;
    VideoView* video_;
    QWidget* right_;
    SpectrumView* spectrum_;
    QPushButton *run_, *msg_;
    Link link_;
    MediaDecoder media_;
    struct Event { qint64 t; int header, frame; };   // 1 good, 0 bad, -1 not judged (touch_rx_page)
    std::deque<Event> events_;
    QElapsedTimer clock_;
    QTimer lampTimer_, demoTimer_;
    int demoPhase_ = 0;
    QWidget* header_ = nullptr;    // lamps + callsign: with the picture full window too
    QVBoxLayout* left_ = nullptr;
    QWidget* leftW_ = nullptr;
    QWidget* overlay_ = nullptr;   // the full-window picture (tap to toggle)
};

class TxPage : public QWidget {
    Q_OBJECT
public:
    explicit TxPage(QWidget* parent = nullptr);
    ~TxPage() override { media_.stop(); }
    bool running() const { return link_.running(); }
    void start() { if (!running()) toggleRun(); }
    // stopped now, not through the button's deferred toggle: in duplex the RX page
    // stops this one, and by the toggle's turn TX had often ended by itself -- the
    // toggle then STARTED it again, with no receiver
    void stop() { if (running() || media_.running()) { media_.stop(); link_.stop(); } }
    QString statusText() const { return status_->text(); }
    void setStatus(const QString& t) { status_->setText(t); }
    QString modeSummary() const;
    void applyModeType();
    void callsignChanged();       // set in Config: re-planned, and restart to apply while sending
    void txSettingChanged() { markPending(); if (!link_.running()) showPlan(); }
    void replan() { linkChanged(); }    // Config's Motion changed: the pickers to the new plan
    void refreshDuplex();         // duplex: no TX button here (RX's TX+RX runs both)
signals:
    void runningChanged(bool running);
protected:
    void resizeEvent(QResizeEvent* e) override;    // the preview kept 16:9
    void showEvent(QShowEvent* e) override;        // (the camera may have changed in Config)
private slots:
    void toggleRun();
    void doToggleRun();   // (toggleRun's work, after the button shows it)
    void onLine(const QString& text);
    void editMessage();
private:
    // a vital setting changed while running (touch_widgets.RunButton's "pending"):
    // the button says RESTART TO APPLY; pressed, it stops and starts again
    bool pending_ = false;
    bool restarting_ = false;   // stopped for a restart: the button stays STARTING...
    void markPending();
    void updateMsgButton();
    double linkBps() const;       // the page's mode / width / modulation's payload rate
    LinkPlan plan() const;        // what the link suggests (sets the pickers on a link change)
    LinkPlan planFor() const;     // the link with the pickers' choices
    void showPlan();
    void linkChanged();           // mode / width / modulation changed: the pickers to the plan's
    Picker *res_, *fps_;
    // a Logitech C920's focus (touch_tx_page's Focus row): shown for that camera
    // only, set live with v4l2-ctl (newest wins: a held stepper never stalls)
    QWidget* focusRow_ = nullptr;
    Segmented* focusMode_ = nullptr;
    Stepper* focus_ = nullptr;
    QTimer focusTimer_;
    void updateFocusRow();
    void applyFocus();
    Segmented* audio_;
    ElideLabel *rates_, *linkInfo_, *warn_;     // touch_tx_page's info lines
    void refreshInfo();
    Segmented *mode_, *bw_, *mod_, *video_src_;
    FreqButton* freq_;
    RadioPicker* radio_pick_;
    Picker* port_;
    Stepper* gain_;
    static QString callsign();    // (set in Config)
    VideoView* preview_;
    ElideLabel* status_;
    QPushButton *run_, *msg_;
    Link link_;
    int sent_ = 0;
    MediaTx media_;        // the sources and framer in-process (capture.h)
    std::function<void()> mediaStart_;   // run when the transmitter is up
};

class ConfigPage : public QWidget {
    Q_OBJECT
public:
    explicit ConfigPage(QWidget* parent = nullptr);
    // from the web page (web.h): as the KiwiSDR row / the Image row's Choose
    void setKiwi(const QString& server);
    void setImage(const QString& path);
signals:
    void modeTypeChanged();
    void callsignChanged();
    void rxSettingChanged();
    void txSettingChanged();      // a TX setting: restart to apply while sending
    void motionChanged();         // Motion (Sharp / Smooth): the TX plan changes
    void duplexChanged();
    void spectrumAveragesChanged(int n);
private:
    QListWidget* list_;
    QStackedWidget* stack_;
    TextButton* kiwi_ = nullptr;
    QLabel* image_ = nullptr;
};

}  // namespace avm
