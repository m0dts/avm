// touch.h -- AVM's touch controls (touch_widgets.py): no typing and no
// drop-downs, everything big enough for a finger. A Picker is a button
// showing its choice; tapping it opens a full-window list. A Stepper is
// [-] value [+] (the arrows wide, held to repeat). FreqButton opens a
// keypad, TextButton an on-screen keyboard. RadioPicker lists the radios
// connected now (libavm's avm_sdr_find, searched in the background).
#pragma once
#include <QDialog>
#include <QLabel>
#include <QListWidget>
#include <QPushButton>
#include <QStringList>
#include <QWidget>
#include <functional>

namespace avm {

// the window a dialog covers (the whole top-level window, as the Python's dialog_geometry)
void coverWindow(QDialog* d, QWidget* parent);
// a smaller dialog (a message box, the messages list): on the touchscreen drawn
// inside the main window, centred over a dimmed backdrop that takes the taps
// (a window of its own got its first press where the pointer last was); call
// before exec(). Elsewhere: nothing changes.
void embedDialog(QDialog* d, QWidget* parent);

// Full-window choice: a mosaic of buttons (as many columns as the names allow), tap one to pick it. refresh: a (possibly slow, Qt-free)
// search for the items -- a Refresh button, run on a worker thread.
class TouchListDialog : public QDialog {
    Q_OBJECT
public:
    TouchListDialog(const QString& title, const QStringList& items, const QString& current, QWidget* parent,
                    std::function<QStringList()> refresh = nullptr);
    QString result() const { return result_; }
private slots:
    void searchDone(const QStringList& items, int n);
private:
    void fill(const QStringList& items, bool enabled = true);
    void doRefresh();
    QWidget* grid_;
    QPushButton* refreshBtn_ = nullptr;
    std::function<QStringList()> refresh_;
    QString current_, result_;
    int search_ = 0;
};

class Picker : public QPushButton {
    Q_OBJECT
public:
    // items: fixed, or (itemsFn) searched each time it's opened, in the background
    Picker(const QString& title, const QStringList& items, const QString& current, QWidget* parent = nullptr);
    Picker(const QString& title, std::function<QStringList()> itemsFn, const QString& current, QWidget* parent = nullptr);
    QString value() const { return value_; }
    void setValue(const QString& v);
    void setItems(const QStringList& items) { items_ = items; }
    void setItemsFn(std::function<QStringList()> fn) { itemsFn_ = std::move(fn); }
signals:
    void changed(const QString& value);
protected:
    void resizeEvent(QResizeEvent* e) override;
    virtual QString shown(const QString& v) const { return v; }    // the text for a value
    void showText();
private:
    void pick();
    QString title_, value_;
    QStringList items_;
    std::function<QStringList()> itemsFn_;
};

// [-] value [+], auto-repeating when held; the arrows wide for a finger
class Stepper : public QWidget {
    Q_OBJECT
public:
    Stepper(double lo, double hi, double step, double value, const QString& suffix = QString(), QWidget* parent = nullptr);
    double value() const { return value_; }
    void setValue(double v, bool emitChange = false);
    bool sign = false;        // "+3" for positive values
    void setStep(double s) { step_ = s; }
    void setArrowWidth(int px);   // the - / + buttons' minimum width (default: wide, 72 px at scale 1)
signals:
    void changed(double value);
private:
    double lo_, hi_, step_, value_ = 1e300;
    QString suffix_;
    QLabel* label_;
    QPushButton *minus_ = nullptr, *plus_ = nullptr;
};

class KeypadDialog : public QDialog {
    Q_OBJECT
public:
    KeypadDialog(const QString& title, const QString& text, QWidget* parent);
    bool value(double* v) const;
private:
    void key(const QString& k);
    QLabel* display_;
    bool fresh_ = true;
};

// a frequency (Hz) shown in MHz; tapping opens the keypad
class FreqButton : public QPushButton {
    Q_OBJECT
public:
    FreqButton(const QString& title, double hz, QWidget* parent = nullptr);
    double hz() const { return hz_; }
    void setHz(double hz);
    void setRange(double lo_mhz, double hi_mhz, const QString& radio) { lo_ = lo_mhz; hi_ = hi_mhz; radio_ = radio; }
    bool inRange() const { return hz_ / 1e6 >= lo_ && hz_ / 1e6 <= hi_; }
    // "29 MHz is outside the PlutoSDR's range (70-6000 MHz)"
    QString outOfRange() const {
        return QString("%1 MHz is outside the %2's range (%3-%4 MHz)").arg(hz_ / 1e6).arg(radio_).arg(lo_).arg(hi_);
    }
signals:
    void changed(double hz);
private:
    void edit();
    QString title_, radio_ = "radio";
    double hz_ = 0, lo_ = 0.001, hi_ = 6000;
};

class KeyboardDialog : public QDialog {
    Q_OBJECT
public:
    static const QStringList ROWS, MESSAGE_ROWS;
    KeyboardDialog(const QString& title, const QString& text, int maxLen, QWidget* parent,
                   const QStringList& rows = ROWS, bool spaceBar = false);
    QString text() const { return text_.trimmed(); }
private:
    void set(const QString& t);
    QLabel* display_;
    QString text_;
    int max_;
};

// short uppercase text (a callsign); tapping opens the keyboard
class TextButton : public QPushButton {
    Q_OBJECT
public:
    TextButton(const QString& title, const QString& text, int maxLen, const QString& placeholder = "(none)",
               QWidget* parent = nullptr);
    QString value() const { return value_; }
    void setValue(const QString& t);
signals:
    void changed(const QString& value);
private:
    QString title_, value_, placeholder_;
    int max_;
};

// The radio one direction uses: tapping lists those connected now. value():
// the label picked; driver(), uri() what it means ("pluto" + "usb:1.8.5").
class RadioPicker : public Picker {
    Q_OBJECT
public:
    RadioPicker(const QString& title, bool rx, const QString& driver, const QString& uri, QWidget* parent = nullptr);
    QString driver() const { return driver_; }
    QString uri() const { return uri_; }
    static const char* VIRTUAL_LABEL;
signals:
    void radioChanged(const QString& driver, const QString& uri);
protected:
    QString shown(const QString& v) const override;
private:
    QString driver_, uri_;
};

// Errors and warnings seen this session (touch_widgets.Notices), for the top
// bar's warning button. A repeat of the newest entry from the same source just
// bumps its count, so a looping error can't flood the list.
class Notices : public QObject {
    Q_OBJECT
public:
    struct Item { QString time, source, text; int count; };
    static Notices& get();
    void add(const QString& source, const QString& text);
    // a process / engine log line that reports a real problem: added, if it is one
    void addIfError(const QString& source, const QString& line);
    void clear();
    int count() const;
    const QList<Item>& items() const { return items_; }
signals:
    void changed();
private:
    QList<Item> items_;
};

// full-window list of the session's notices, newest first, with Clear errors
class NoticesDialog : public QDialog {
    Q_OBJECT
public:
    explicit NoticesDialog(QWidget* parent);
private:
    void fill();
    QListWidget* list_;
};

// the radios' tuning range (touch_widgets.FREQ_RANGE_MHZ) and names
void radioRange(const QString& driver, double* lo_mhz, double* hi_mhz);
QString radioName(const QString& driver);

}  // namespace avm
