// mainwindow.h -- the window (touch_gui.py's TouchWindow): the top bar
// (CONFIG / TX / RX, what's running, notices, CPU, quit) over the pages.
#pragma once
#include <QElapsedTimer>
#include <QLabel>
#include <QSet>
#include <QStackedWidget>
#include <QTimer>
#include <QWidget>

#include "pages.h"
#include "updater.h"

namespace avm {

class MainWindow : public QWidget {
    Q_OBJECT
public:
    explicit MainWindow(bool demo, QWidget* parent = nullptr);
    RxPage* rx() const { return rx_; }
    TxPage* tx() const { return tx_; }
    void setView(const QString& v) { view_->setValue(v); showView(v); }
protected:
    void closeEvent(QCloseEvent* e) override;
    void resizeEvent(QResizeEvent* e) override;
private slots:
    void showView(const QString& v);
    void updateIndicator();
    void updateCpu();
    void usbTick();
private:
    struct UsbWatch { QSet<QString> watch, resume; bool watching = false; qint64 backT = -1; };
    UsbWatch usb_[2];       // TX, RX
    bool usbSeen_ = false, usbLost_ = false;
    QTimer usbTimer_;
    void startWeb();           // the web page (web.h), but not for screenshots or trial runs
    void offerUpdate();        // the ⬆ button: what's new, then update (updater.h)
    Updater* updater_ = nullptr;
    QPushButton* update_ = nullptr;
    QString updateVersion_, updateNotes_;
    QElapsedTimer usbClock_;
    Segmented* view_;
    QLabel *indicator_, *cpu_;
    QStackedWidget* pages_;
    ConfigPage* config_;
    TxPage* tx_;
    RxPage* rx_;
    QTimer cpuTimer_;
    unsigned long long lastIdle_ = 0, lastTotal_ = 0;
    // the start screen (touch_gui's): the name, a status line, the credit; a
    // moment, or until tapped
    QWidget* splash_ = nullptr;
public:
    void buildSplash();
private:
    bool eventFilter(QObject* o, QEvent* e) override;
};

}  // namespace avm
