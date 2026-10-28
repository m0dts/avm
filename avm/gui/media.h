// media.h -- the received stream turned into pictures (media_rx_player.py's
// video side, in C++): the modem's output bytes -> the deframer
// (framing.cpp) -> wavelet video packets -> the decoder (wavelet_core.cpp,
// its layout made by wvt_create_auto) -> QImage; audio packets -> Opus /
// Codec2 -> the default output (audio.h). Both come from the AVM
// library (avm.dll / libavm.so), loaded at run time, so the GUI builds with
// any compiler and Qt version and needs nothing else.
#pragma once
#include <QByteArray>
#include <QElapsedTimer>
#include <QImage>
#include <QLibrary>
#include <QObject>
#include <QTimer>
#include <deque>
#include <vector>

#include "audio.h"

namespace avm {

// I420 planes -> an RGB picture (BT.601, as the Python player)
QImage i420ToImage(int w, int h, const uint8_t* y, const uint8_t* u, const uint8_t* v);

class MediaDecoder : public QObject {
    Q_OBJECT
public:
    explicit MediaDecoder(QObject* parent = nullptr);
    ~MediaDecoder() override;
    bool ok() const { return ok_; }
    QString error() const { return error_; }
    void start(int fragment_size);       // a new stream
    void stop();
    void feed(const QByteArray& bytes);  // the modem's stdout
signals:
    void frame(const QImage& img);
    void station(const QString& id, bool confirmed);
    void message(const QString& text);
    void videoConfig(int width, int height, double fps, const QString& codec);
private:
    void handleEvent(int type, const std::vector<uint8_t>& d);
public:
    AudioOut& audio() { return audioOut_; }
private:
    QImage toRgb(int w, int h) const;
    // display pacing (media_rx_player.VideoWorker): a fragment's burst of
    // pictures spread at the frame rate; a backlog beyond a burst shown at once;
    // 3% fast while any wait, so a faster TX clock can't build up delay
    void showNext();
    std::deque<QImage> frames_;
    QTimer showTimer_;
    double frameInterval_ = 0, nextShow_ = -1;
    double burst_ = 0, burstT_ = -1;
    int burstN_ = 0;
    // the library's C functions
    using DfCreate = void* (*)(int);
    using DfFree = void (*)(void*);
    using DfPush = int (*)(void*, const uint8_t*, int, double);
    using DfEvType = int (*)(void*, int);
    using DfEvLen = int (*)(void*, int);
    using DfEvData = void (*)(void*, int, uint8_t*);
    using WvtCreate = void* (*)(long long, long long, long long, double, long long, int, int, int);
    using WvtDecode = int (*)(void*, const uint8_t*, long long, uint8_t*, uint8_t*, uint8_t*);
    using WvtFree = void (*)(void*);
    QLibrary lib_;
    bool ok_ = false;
    QString error_;
    DfCreate dfCreate_ = nullptr;
    DfFree dfFree_ = nullptr;
    DfPush dfPush_ = nullptr;
    DfEvType dfEvType_ = nullptr;
    DfEvLen dfEvLen_ = nullptr;
    DfEvData dfEvData_ = nullptr;
    WvtCreate wvtCreate_ = nullptr;
    WvtDecode wvtDecode_ = nullptr;
    WvtFree wvtFree_ = nullptr;
    void* df_ = nullptr;
    void* wvt_ = nullptr;
    int fragment_ = 1024, w_ = 0, h_ = 0;
    QByteArray pending_;
    std::vector<uint8_t> y_, u_, v_;
    QElapsedTimer clock_;
    AudioOut audioOut_;
    AudioDecoder audioDec_{audioOut_};
};

}  // namespace avm
