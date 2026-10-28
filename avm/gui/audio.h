// audio.h -- the received audio (media_rx_player.py's audio side, in C++):
// the stream's Opus (48 kHz mono) or Codec2 3200 packets decoded and played
// through the default output (miniaudio: WASAPI / ALSA / PulseAudio), with
// a jitter buffer -- packets arrive in bursts, one fragment at a time.
#pragma once
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
#include <QString>
#include <chrono>

namespace avm {

class AudioOut {
public:
    static const int RATE = 48000;
    AudioOut();
    ~AudioOut();
    bool ok() const { return ok_; }
    bool open(const std::string& device);       // by name; empty: the default output
    void setVolume(float v) { volume_ = v; }     // 0..1
    // media_rx_player's two: preroll -- waited for before playing (max(0.5 s, a
    // fragment's air time + 0.25 s): its audio arrives at once); hold -- kept
    // queued while playing (max(0.2 s, 0.6 x air time + 0.05 s)), the rest
    // trimmed once after 2 s and steered away by the rate after that
    void setLatency(double preroll, double hold) {
        std::lock_guard<std::mutex> g(mu_);
        target_s_ = preroll;
        hold_s_ = hold;
    }
    static std::vector<std::string> outputs();  // the playback devices' names
    void push(const float* pcm, size_t n);      // mono, RATE
    std::function<void(const float*, size_t)> tap;   // each push's samples too (a recording)
    void clear();
    double buffered_s();
    int underruns = 0;                          // (diagnostics)
    // the device's callbacks since the last call: their longest gap (ms), the
    // frames asked for at most, and the backend in use -- a gap far over the
    // period is the sound system starving the device, not this buffer
    QString callbackStats();
    void pull(float* out, size_t n);            // the device's callback
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
    std::mutex mu_;
    std::deque<float> buf_;
    bool playing_ = false, ok_ = false;
    float volume_ = 1.0f;
    double target_s_ = 0.5, hold_s_ = 0.2;
    double played_s_ = 0;        // since playing started (the start-up trim)
    bool trimmed_ = false;
    size_t fadeIn_ = 0;          // samples left of a fade-in (after an underrun)
    // drop n samples from the front, crossfading what was next into what
    // follows the cut (no click); caller holds mu_
    void dropFront(size_t n);
    // held at the target by playing up to 0.5% fast or slow (media_rx_player's
    // _hold_latency): the transmitter's clock and the sound card's drift apart
    double backlog_ema_ = -1, rate_ = 1.0, frac_ = 0;
    std::chrono::steady_clock::time_point lastCb_{};
    double maxGapMs_ = 0;
    size_t maxFrames_ = 0, callbacks_ = 0;
    size_t pushed_ = 0;          // samples pushed since the last stats (the arrival rate)
    std::chrono::steady_clock::time_point statsT_{};
    void close();
};

// an audio profile's decoder (media_tx_framer.AUDIO_PROFILES): 0 Opus, 1 Codec2 3200
class AudioDecoder {
public:
    explicit AudioDecoder(AudioOut& out) : out_(out) {}
    ~AudioDecoder();
    void setProfile(int id);
    void decode(const unsigned char* p, size_t n);
    void reset();
private:
    AudioOut& out_;
    int profile_ = -1;
    void* opus_ = nullptr;
    void* c2_ = nullptr;
    std::vector<short> pcm_;
    std::vector<float> f_;
    float last_ = 0;     // Codec2 upsampling: the previous sample
};

}  // namespace avm
