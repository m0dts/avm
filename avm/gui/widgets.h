// widgets.h -- AVM's building blocks (touch_widgets.py): Segmented (a row of
// exclusive buttons), Lamp (a lock lamp), ElideLabel, and VideoView (the
// decoded picture, scaled to fit, aspect kept).
#pragma once
#include <QButtonGroup>
#include <QElapsedTimer>
#include <QImage>
#include <QLabel>
#include <QMap>
#include <QPushButton>
#include <QVector>
#include <QWidget>
#include <algorithm>

namespace avm {

class Segmented : public QWidget {
    Q_OBJECT
public:
    Segmented(const QList<QPair<QString, QString>>& options, const QString& value = {}, QWidget* parent = nullptr);
    QString value() const;
    void setValue(const QString& v);
    QPushButton* button(const QString& v) const { return buttons_.value(v); }
signals:
    void changed(const QString& value);
protected:
    void showEvent(QShowEvent* e) override;
private:
    QButtonGroup group_;
    QMap<QString, QPushButton*> buttons_;
};

// lock lamp: green (all locked over the window), orange (some), red (none), grey (idle)
class Lamp : public QLabel {
    Q_OBJECT
public:
    enum State { Idle, Good, Some, None };
    explicit Lamp(int size, QWidget* parent = nullptr);
    void setState(State s);
private:
    int size_;
};

class ElideLabel : public QLabel {
    Q_OBJECT
public:
    using QLabel::QLabel;
    QSize minimumSizeHint() const override { return {10, QLabel::minimumSizeHint().height()}; }
protected:
    void paintEvent(QPaintEvent* e) override;
};

// The received picture: drawn scaled to the widget, aspect ratio kept,
// black bars round it; a caption when there is none. Double-click: the
// owner toggles a full-window view (tapped).
class VideoView : public QWidget {
    Q_OBJECT
public:
    explicit VideoView(QWidget* parent = nullptr);
    void setFrame(const QImage& img);
    void clearFrame(const QString& caption);
    const QImage& frame() const { return img_; }
    // a fixed shape (e.g. 16.0 / 9): the height follows the width
    void setAspect(double a);
    bool hasHeightForWidth() const override { return aspect_ > 0; }
    int heightForWidth(int w) const override { return aspect_ > 0 ? (int)(w / aspect_) : -1; }
signals:
    void tapped();
protected:
    void paintEvent(QPaintEvent* e) override;
    void mouseReleaseEvent(QMouseEvent* e) override;
private:
    QImage img_;
    QString caption_;
    double aspect_ = 0;
};

// The receiver's spectrum ("[spectrum]" lines: dB over frequency), scaled as
// touch_rx_page._autoscale: the bottom line just under the noise (the
// spectrum's two ends, outside the signal), ten divisions reaching past the
// peak; dB/div only widens until clear() (a new RX start).
class SpectrumView : public QWidget {
    Q_OBJECT
public:
    explicit SpectrumView(QWidget* parent = nullptr);
    void setSpectrum(double f0, double step, const QVector<float>& db);
    void clear();
    void setAverages(int n) { avg_ = std::max(1, n); }
    // Auto: fitted to the noise and peak (autoTop() tells the top); manual: the
    // top at ref_db, MANUAL_DB_PER_DIV per division (touch_rx_page)
    void setScale(bool autoOn, double ref_db);
    // the capture band (Hz from the tuned frequency): where a signal is found and
    // decoded, shaded green behind the trace; lo == hi: none
    void setBand(double lo_hz, double hi_hz) { bandLo_ = lo_hz; bandHi_ = hi_hz; update(); }
signals:
    void autoTop(double top_db);
protected:
    void paintEvent(QPaintEvent* e) override;
private:
    QVector<float> db_;
    QVector<double> pow_;     // the averaged power
    int avg_ = 1;
    QElapsedTimer settle_;    // since the first spectrum after clear()
    bool auto_ = true;
    double bandLo_ = 0, bandHi_ = 0;
    double f0_ = 0, step_ = 0, top_ = -20, bottom_ = -80, div_ = 0, heldDiv_ = 0;
};

QLabel* rowLabel(const QString& text);

}  // namespace avm
