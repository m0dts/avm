#include "widgets.h"

#include <QFontMetrics>
#include <QHBoxLayout>
#include <QPainter>
#include <algorithm>
#include <cmath>
#include <vector>

#include "style.h"

namespace avm {

Segmented::Segmented(const QList<QPair<QString, QString>>& options, const QString& value, QWidget* parent)
    : QWidget(parent), group_(this) {
    setObjectName("seg");
    auto* lay = new QHBoxLayout(this);
    lay->setContentsMargins(0, 0, 0, 0);
    lay->setSpacing(ui.px(4));
    group_.setExclusive(true);
    for (const auto& o : options) {
        auto* b = new QPushButton(o.second);
        b->setCheckable(true);
        b->setFocusPolicy(Qt::NoFocus);
        lay->addWidget(b);
        group_.addButton(b);
        buttons_[o.first] = b;
        const QString v = o.first;
        connect(b, &QPushButton::clicked, this, [this, v] { emit changed(v); });
    }
    if (!value.isEmpty()) setValue(value);
}

QString Segmented::value() const {
    for (auto it = buttons_.begin(); it != buttons_.end(); ++it)
        if (it.value()->isChecked()) return it.key();
    return {};
}

void Segmented::setValue(const QString& v) {
    if (auto* b = buttons_.value(v)) b->setChecked(true);
}

// the selected label is bold: room for that on every button, so a short one
// isn't clipped when picked
void Segmented::showEvent(QShowEvent* e) {
    QWidget::showEvent(e);
    for (auto* b : buttons_) {
        b->ensurePolished();
        QFont f(b->font());
        f.setBold(true);
        b->setMinimumWidth(QFontMetrics(f).horizontalAdvance(b->text()) + ui.px(20));
    }
}

Lamp::Lamp(int size, QWidget* parent) : QLabel(parent), size_(size) {
    setFixedSize(size, size);
    setState(Idle);
}

void Lamp::setState(State s) {
    const char* c = s == Good ? GO : s == Some ? TX_ACCENT : s == None ? STOP : BUTTON;
    setStyleSheet(QString("background: %1; border-radius: %2px;").arg(c).arg(size_ / 2));
}

void ElideLabel::paintEvent(QPaintEvent*) {
    QPainter p(this);
    QFontMetrics fm(font());
    QString t = fm.elidedText(text(), Qt::ElideRight, width());
    p.setPen(palette().color(QPalette::WindowText));
    p.drawText(rect(), alignment() | Qt::AlignVCenter, t);
}

VideoView::VideoView(QWidget* parent) : QWidget(parent) {
    setMinimumSize(160, 90);
    setAttribute(Qt::WA_OpaquePaintEvent);
}

void VideoView::setAspect(double a) {
    aspect_ = a;
    QSizePolicy p = sizePolicy();
    p.setHeightForWidth(a > 0);
    p.setVerticalPolicy(a > 0 ? QSizePolicy::Fixed : QSizePolicy::Preferred);
    setSizePolicy(p);
    updateGeometry();
}

void VideoView::setFrame(const QImage& img) {
    img_ = img;
    update();
}

void VideoView::clearFrame(const QString& caption) {
    img_ = QImage();
    caption_ = caption;
    update();
}

void VideoView::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.fillRect(rect(), Qt::black);
    if (img_.isNull()) {
        p.setPen(QColor(TEXT_DIM));
        p.drawText(rect(), Qt::AlignCenter, caption_);
        return;
    }
    QSize s = img_.size().scaled(size(), Qt::KeepAspectRatio);
    QRect r(QPoint((width() - s.width()) / 2, (height() - s.height()) / 2), s);
    p.setRenderHint(QPainter::SmoothPixmapTransform);
    p.drawImage(r, img_);
}

void VideoView::mouseReleaseEvent(QMouseEvent*) { emit tapped(); }

SpectrumView::SpectrumView(QWidget* parent) : QWidget(parent) {
    setMinimumSize(160, 60);
}

void SpectrumView::setSpectrum(double f0, double step, const QVector<float>& db) {
    f0_ = f0;
    step_ = step;
    // the first SETTLE_MS after a start: neither held nor averaged (a KiwiSDR's
    // level starts high and drops as its gain settles: the scale stuck wide)
    const qint64 SETTLE_MS = 1500;
    if (!settle_.isValid()) settle_.start();
    const bool settling = settle_.elapsed() < SETTLE_MS;
    // averaging (Config: Spectrum avg): a running power mean over about avg_ spectra
    if (avg_ > 1 && pow_.size() == db.size() && !settling) {
        const double a = 1.0 / avg_;
        for (int i = 0; i < db.size(); ++i) pow_[i] += a * (std::pow(10.0, db[i] / 10.0) - pow_[i]);
    } else {
        pow_.resize(db.size());
        for (int i = 0; i < db.size(); ++i) pow_[i] = std::pow(10.0, db[i] / 10.0);
    }
    db_ = db;
    if (avg_ > 1)
        for (int i = 0; i < db.size(); ++i) db_[i] = (float)(10.0 * std::log10(std::max(pow_[i], 1e-30)));
    if (db.size() >= 16 && auto_) {
        auto powerMean = [](const std::vector<double>& v) {
            double s = 0;
            for (double x : v) s += std::pow(10.0, x / 10);
            return 10 * std::log10(s / v.size());
        };
        // the noise: the outer 4% each side (band noise, power-averaged)...
        int n = db.size(), k = std::max(2, n * 4 / 100);
        std::vector<double> edges;
        for (int i = 0; i < k; ++i) { edges.push_back(db[i]); edges.push_back(db[n - 1 - i]); }
        double noise = powerMean(edges);
        // ...unless the quietest tenth of all bins is lower (skirts at the edges)
        QVector<float> s = db;
        std::sort(s.begin(), s.end());
        std::vector<double> quiet(s.begin(), s.begin() + std::max(1, n / 10));
        noise = std::min(noise, powerMean(quiet));
        double peak = s.back();
        double bottom = std::round((noise - 1.0) / 0.5) * 0.5;      // the noise 1 dB above the bottom line
        double span = peak + 1.0 - bottom;
        static const double DIVS[] = {1, 2, 4, 5, 10, 20};
        double div = 20;
        for (double d : DIVS) if (10 * d >= span) { div = d; break; }
        div = std::max(div, heldDiv_);    // only widens while RX runs (a pumping scale between bursts)
        heldDiv_ = settling ? 0 : div;
        div_ = div;
        bottom_ = bottom;
        if (bottom + 10 * div != top_) emit autoTop(bottom + 10 * div);
        top_ = bottom + 10 * div;
    }
    update();
}

void SpectrumView::setScale(bool autoOn, double ref_db) {
    const double MANUAL_DB_PER_DIV = 2.5;
    auto_ = autoOn;
    heldDiv_ = 0;
    if (!autoOn) {
        div_ = MANUAL_DB_PER_DIV;
        top_ = ref_db;
        bottom_ = ref_db - 10 * MANUAL_DB_PER_DIV;
    }
    update();
}

void SpectrumView::clear() {
    db_.clear();
    pow_.clear();
    heldDiv_ = 0;
    settle_.invalidate();
    update();
}

void SpectrumView::paintEvent(QPaintEvent*) {
    QPainter p(this);
    p.fillRect(rect(), QColor(PANEL));
    if (db_.size() < 2) {
        p.setPen(QColor(TEXT_DIM));
        p.drawText(rect(), Qt::AlignCenter, "Spectrum");
        return;
    }
    QRectF r = QRectF(rect()).adjusted(4, 4, -4, -4);
    double span = top_ - bottom_;
    p.setPen(QColor(BUTTON));
    for (int i = 0; i <= 10; ++i) {          // ten divisions
        double y = r.top() + i * r.height() / 10;
        p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y));
    }
    p.drawLine(QPointF(r.center().x(), r.top()), QPointF(r.center().x(), r.bottom()));   // the centre frequency
    if (bandHi_ > bandLo_ && step_ > 0) {     // the capture band
        double w = step_ * (db_.size() - 1);
        double x0 = r.left() + (bandLo_ - f0_) / w * r.width(), x1 = r.left() + (bandHi_ - f0_) / w * r.width();
        x0 = std::max(x0, r.left());
        x1 = std::min(x1, r.right());
        if (x1 > x0) p.fillRect(QRectF(x0, r.top(), x1 - x0, r.height()), QColor(102, 187, 106, 60));
    }
    QPolygonF poly;
    for (int i = 0; i < db_.size(); ++i) {
        double x = r.left() + (double)i / (db_.size() - 1) * r.width();
        double y = r.top() + (top_ - db_[i]) / span * r.height();
        poly << QPointF(x, std::min(std::max(y, r.top()), r.bottom()));
    }
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(QPen(QColor(RX_ACCENT), 1.5));
    p.drawPolyline(poly);
    p.setPen(QColor(TEXT_DIM));
    p.drawText(r.adjusted(2, 0, 0, 0), Qt::AlignLeft | Qt::AlignTop, QString("%1 dB/div").arg(div_));
    p.drawText(r.adjusted(0, 0, -2, 0), Qt::AlignRight | Qt::AlignTop,
               QString("span %1 kHz").arg(step_ * (db_.size() - 1) / 1e3, 0, 'f', 1));   // the whole width shown
}

QLabel* rowLabel(const QString& text) {
    auto* l = new QLabel(text);
    l->setObjectName("rowlabel");
    return l;
}

}  // namespace avm
