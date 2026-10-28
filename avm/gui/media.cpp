#include "media.h"

#include <QRegularExpression>

#include <algorithm>

#include "pages.h"

namespace avm {

// the deframer's event types (framing.cpp)
enum { EV_AUDIO_CONFIG = 1, EV_AUDIO_PACKET = 2, EV_VIDEO_CONFIG = 3, EV_VIDEO_PACKET = 4, EV_STATION_ID = 5, EV_STATION_ID_TENTATIVE = 6, EV_MESSAGE = 7,
       EV_STATION_ID_RESET = 8 };
static const char* WAVELET_TAG = "WVT8";   // media_rx_player.WAVELET_CODEC_TAG

MediaDecoder::MediaDecoder(QObject* parent) : QObject(parent) {
    lib_.setFileName(avmDir() + "/c++/avm");   // avm.dll / libavm.so
    if (!lib_.load()) {
        error_ = "AVM library: " + lib_.errorString();
        return;
    }
    auto r = [this](const char* name) { return lib_.resolve(name); };
    dfCreate_ = (DfCreate)r("df_create");
    dfFree_ = (DfFree)r("df_free");
    dfPush_ = (DfPush)r("df_push");
    dfEvType_ = (DfEvType)r("df_event_type");
    dfEvLen_ = (DfEvLen)r("df_event_len");
    dfEvData_ = (DfEvData)r("df_event_data");
    wvtCreate_ = (WvtCreate)r("wvt_create_auto");
    wvtDecode_ = (WvtDecode)r("wvt_decode");
    wvtFree_ = (WvtFree)r("wvt_free");
    connect(&showTimer_, &QTimer::timeout, this, &MediaDecoder::showNext);
    showTimer_.start(5);
    ok_ = dfCreate_ && dfFree_ && dfPush_ && dfEvType_ && dfEvLen_ && dfEvData_ && wvtCreate_ && wvtDecode_ && wvtFree_;
    if (!ok_) error_ = "AVM library too old -- rebuild AVM (cmake --build)";
    clock_.start();
}

MediaDecoder::~MediaDecoder() { stop(); }

void MediaDecoder::start(int fragment_size) {
    stop();
    if (!ok_) return;
    fragment_ = fragment_size;
    df_ = dfCreate_(fragment_size);
}

void MediaDecoder::stop() {
    if (wvt_) { wvtFree_(wvt_); wvt_ = nullptr; }
    if (df_) { dfFree_(df_); df_ = nullptr; }
    pending_.clear();
    w_ = h_ = 0;
    frames_.clear();
    nextShow_ = -1;
    burst_ = 0;
    burstN_ = 0;
    audioDec_.reset();
    audioOut_.clear();
}

void MediaDecoder::showNext() {
    if (frames_.empty()) return;
    double now = clock_.elapsed() / 1000.0;
    if (frameInterval_ <= 0) {            // no frame rate known: as they come
        emit frame(frames_.front());
        frames_.pop_front();
        return;
    }
    // late for its slot (the gap before the next burst): the schedule restarts from now
    if (nextShow_ < 0 || now > nextShow_ + frameInterval_) nextShow_ = now;
    size_t queued = frames_.size() - 1;
    double burst = std::min(burst_, 1.0 / frameInterval_);
    bool backlog = queued > (size_t)(1 + std::lround(burst));   // VIDEO_CATCHUP_FRAMES = 1
    if (!backlog && now < nextShow_) return;
    double target = backlog ? now : nextShow_;
    emit frame(frames_.front());
    frames_.pop_front();
    nextShow_ = target + frameInterval_ * (frames_.empty() ? 1.0 : 0.97);
}

// each fragment's data (fragment_size bytes, as the modem writes them) -> the deframer
void MediaDecoder::feed(const QByteArray& bytes) {
    if (!df_) return;
    pending_ += bytes;
    int off = 0;
    std::vector<uint8_t> d;
    while (pending_.size() - off >= fragment_) {
        double now = clock_.elapsed() / 1000.0;
        int n = dfPush_(df_, (const uint8_t*)pending_.constData() + off, fragment_, now);
        off += fragment_;
        for (int i = 0; i < n; ++i) {
            d.resize(dfEvLen_(df_, i));
            dfEvData_(df_, i, d.data());
            handleEvent(dfEvType_(df_, i), d);
        }
    }
    pending_.remove(0, off);
}

void MediaDecoder::handleEvent(int type, const std::vector<uint8_t>& d) {
    switch (type) {
    case EV_AUDIO_CONFIG:                    // [profile id][2-byte slot length]
        if (!d.empty()) audioDec_.setProfile(d[0]);
        break;
    case EV_AUDIO_PACKET:
        audioDec_.decode(d.data(), d.size());
        break;
    case EV_VIDEO_CONFIG: {
        if (d.size() < 6) return;
        int w = d[0] << 8 | d[1], h = d[2] << 8 | d[3];
        double fps = (d[4] << 8 | d[5]) / 1000.0;
        QString codec = d.size() >= 10 ? QString::fromLatin1((const char*)d.data() + 6, 4) : QString("?");
        emit videoConfig(w, h, fps, codec);
        frameInterval_ = fps > 0 ? 1.0 / fps : 0;
        bool wavelet = d.size() >= 10 && std::equal(WAVELET_TAG, WAVELET_TAG + 4, d.begin() + 6);
        if (!wavelet) return;                // (H.264: the Python player's)
        if (wvt_ && w == w_ && h == h_) return;
        if (wvt_) wvtFree_(wvt_);
        wvt_ = wvtCreate_(w, h, 64, 0.996, 60, 1, 1, -1);   // as the player's WaveletCodec(w, h, 64)
        if (!wvt_) return;
        w_ = w;
        h_ = h;
        y_.assign((size_t)w * h, 0);
        u_.assign((size_t)w * h / 4, 128);
        v_.assign((size_t)w * h / 4, 128);
        break;
    }
    case EV_VIDEO_PACKET:
    {
        // the arrival bursts (a fragment's pictures come together): their usual size
        double now = clock_.elapsed() / 1000.0;
        if (now - burstT_ > 0.05) {      // BURST_GAP_S: the previous burst is complete
            burst_ = burstN_ > burst_ ? burstN_ : burst_ + 0.25 * (burstN_ - burst_);
            burstN_ = 0;
        }
        ++burstN_;
        burstT_ = now;
        if (wvt_ && wvtDecode_(wvt_, d.data(), (long long)d.size(), y_.data(), u_.data(), v_.data()) == 0) {
            frames_.push_back(toRgb(w_, h_));
            if (frames_.size() > 90) frames_.pop_front();   // VIDEO_QUEUE_DEPTH
        }
    }
        break;
    case EV_STATION_ID:
    case EV_STATION_ID_TENTATIVE: {
        QString id = QString::fromUtf8((const char*)d.data(), (int)d.size());
        if (type == EV_STATION_ID_TENTATIVE) {
            // a character not heard yet (still 0): a dash, until the callsign is known;
            // after the padding's first space, the rest is padding too
            id.replace(QChar('\0'), QChar('-'));
            static const QRegularExpression pad(R"( [ -]*$)");
            id.remove(pad);
        }
        id = id.trimmed();
        id.remove(QChar('\0'));
        emit station(id, type == EV_STATION_ID);
        break;
    }
    case EV_STATION_ID_RESET:
        emit station(QString(), false);
        break;
    case EV_MESSAGE:
        if (d.size() > 4) emit message(QString::fromUtf8((const char*)d.data() + 4, (int)d.size() - 4));
        break;
    default:
        break;
    }
}

QImage MediaDecoder::toRgb(int w, int h) const { return i420ToImage(w, h, y_.data(), u_.data(), v_.data()); }

// I420 -> RGB (BT.601, as OpenCV's cvtColor YUV2BGR_I420 the Python player uses)
QImage i420ToImage(int w, int h, const uint8_t* yp, const uint8_t* up, const uint8_t* vp) {
    QImage img(w, h, QImage::Format_RGB32);
    for (int y = 0; y < h; ++y) {
        QRgb* line = (QRgb*)img.scanLine(y);
        const uint8_t* Y = yp + (size_t)y * w;
        const uint8_t* U = up + (size_t)(y / 2) * (w / 2);
        const uint8_t* V = vp + (size_t)(y / 2) * (w / 2);
        for (int x = 0; x < w; ++x) {
            int yy = Y[x], uu = U[x / 2] - 128, vv = V[x / 2] - 128;
            int r = yy + ((91881 * vv + 32768) >> 16);
            int g = yy - ((22554 * uu + 46802 * vv + 32768) >> 16);
            int b = yy + ((116130 * uu + 32768) >> 16);
            line[x] = qRgb(std::clamp(r, 0, 255), std::clamp(g, 0, 255), std::clamp(b, 0, 255));
        }
    }
    return img;
}

}  // namespace avm
