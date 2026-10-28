// media_tx.h -- the transmitted stream made in C++ (media_tx_framer.py with
// media_source_wavelet.py and media_source_audio.py): the camera or test
// pattern (capture.h) -> the wavelet encoder (libavm's wvt_*), the
// microphone or a test tone -> Codec2 3200 or Opus, both into the framer (libavm's
// fr_*), one fragment's worth every fragment period, handed to the
// transmitter (the GUI: Link::write into the in-process avm_tx engine).
#pragma once
#include <QImage>
#include <QLibrary>
#include <QString>
#include <deque>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "capture.h"

namespace avm {

struct MediaTxConfig {
    int fragment_size = 1024;
    double fragment_period_s = 0.24;   // the assembler's cadence (media_tx_framer.py's default)
    std::string station_id;
    // video: "off", "test" (the testsrc pattern), "camera", or "image" (a still, repeated)
    std::string video = "test", video_device, image_path;
    int width = 256, height = 144;
    double fps = 6;
    bool mono = false;     // black and white: the colour planes flat grey (media_source_wavelet.py --mono)
    int frame_bytes = 416;
    // audio: "off", "test" (a 1 kHz tone) or "mic"
    std::string audio = "test", audio_device;
    std::string audio_codec = "codec2";   // "codec2" (3200) or "opus" (48 kHz mono)
    double opus_kbps = 10;
};

class MediaTx {
public:
    using OutFn = std::function<void(const std::vector<uint8_t>&)>;   // one fragment
    using LogFn = std::function<void(const std::string&)>;
    MediaTx();
    ~MediaTx();
    bool ok() const { return ok_; }
    QString error() const { return error_; }
    bool start(const MediaTxConfig& c, OutFn out, LogFn log);
    // each picture as it goes to the encoder (the capture thread): the TX preview
    void setPreview(std::function<void(const QImage&)> fn) { preview_ = std::move(fn); }
    // the text message sent alongside (media_tx_framer.build_message); empty: none
    void setMessage(const QString& text);
    // the transmitter's real air time per fragment (its "Streamed fragment ..
    // ms"): the framer ticks at that, not the plan's estimate -- 1.3% short for
    // SH, the extra fragments queued up in the transmitter until it dropped one
    // (with its audio: an underrun every ~15 s)
    void setFragmentPeriod(double s) { if (s > 0.01 && s < 10) period_ = s; }
    static std::vector<uint8_t> buildMessage(long long ts, const QString& text);
    void stop();
    bool running() const { return running_; }

private:
    void videoFrame(struct AVFrame* f);
    void audioFrame(struct AVFrame* f);
    void assemble();

    QLibrary lib_;
    bool ok_ = false;
    QString error_;
    void* (*frCreate_)(int, const char*, int) = nullptr;
    void (*frFree_)(void*) = nullptr;
    void (*frSetAudioProfile_)(void*, int) = nullptr;
    void (*frSetVideoConfig_)(void*, const uint8_t*, int) = nullptr;
    void (*frSetMessage_)(void*, const uint8_t*, int) = nullptr;
    void (*frAddAudio_)(void*, const uint8_t*, int) = nullptr;
    void (*frAddVideo_)(void*, const uint8_t*, int, int) = nullptr;
    int (*frTick_)(void*, double) = nullptr;
    void (*frFragment_)(void*, int, uint8_t*) = nullptr;
    void* (*wvtCreate_)(long long, long long, long long, double, long long, int, int, int) = nullptr;
    int (*wvtEncode_)(void*, const uint8_t*, const uint8_t*, const uint8_t*, uint8_t*) = nullptr;
    void (*wvtFree_)(void*) = nullptr;

    MediaTxConfig cfg_;
    OutFn out_;
    LogFn log_;
    std::function<void(const QImage&)> preview_;
    void* fr_ = nullptr;
    void* wvt_ = nullptr;
    void* c2_ = nullptr;
    void* opus_ = nullptr;
    std::mutex mu_;                                  // the queues below
    std::deque<std::vector<uint8_t>> video_q_, audio_q_;
    std::vector<short> pcm_;                         // Codec2: samples waiting for a whole frame
    std::vector<uint8_t> y_, u_, v_, pkt_;
    std::vector<uint8_t> message_;
    Capture video_cap_, audio_cap_;
    std::thread asm_th_;
    std::atomic<bool> stop_{false}, running_{false};
    std::atomic<double> period_{0.24};   // the framer's cadence (setFragmentPeriod)
    void countAudioPacket(double expected_per_s);
    long long frames_ = 0;
    long long audio_pkts_ = 0;   // encoded (logged every 500: their rate)
    double audio_t0_ = 0;
    double enc_s_ = 0;
};

}  // namespace avm
