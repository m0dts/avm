#include "media_tx.h"

#include <chrono>
#include <cmath>
#include <cstring>
#include <ctime>
#include <set>
#include <utility>

#ifndef _WIN32
#include <fcntl.h>
#include <linux/videodev2.h>
#include <sys/ioctl.h>
#include <unistd.h>
#endif

#include "codec2.h"
#include <opus.h>
#include "media.h"
#include "pages.h"

extern "C" {
#include <libavutil/frame.h>
}

namespace avm {

static const char* WAVELET_CODEC_TAG = "WVT8";
static const int VIDEO_CONFIG_TYPE = 0x03;
static const int AUDIO_PROFILE_OPUS_48K = 0x00, AUDIO_PROFILE_CODEC2_3200 = 0x01;   // media_tx_framer.AUDIO_PROFILES
static const int OPUS_SAMPLES = 960;                 // 20 ms at 48 kHz (--audio-frame-duration 20)
static const double MOTION_PIXEL_RATE = 1.1e6;      // media_source_wavelet.py: --motion auto's limit
static const int CODEC2_SAMPLES = 160, CODEC2_BYTES = 8;

#ifndef _WIN32
// media_source.py's _v4l2_capture_size: the smallest YUYV size the camera lists
// that is at least WxH, preferring the same shape (within 3%) -- e.g. 424x240
// for 384x224 on a C920, whose default 640x480 is 4:3 and came out with black
// side bars in the 16:9 frame. Empty if none (the device default then).
static std::string v4l2_capture_size(const std::string& dev, int tw, int th) {
    int fd = ::open(dev.c_str(), O_RDWR | O_NONBLOCK);
    if (fd < 0) return "";
    std::set<std::pair<int, int>> sizes;
    v4l2_frmsizeenum fs;
    memset(&fs, 0, sizeof fs);
    fs.pixel_format = V4L2_PIX_FMT_YUYV;
    for (fs.index = 0; ioctl(fd, VIDIOC_ENUM_FRAMESIZES, &fs) == 0; ++fs.index)
        if (fs.type == V4L2_FRMSIZE_TYPE_DISCRETE) sizes.insert({(int)fs.discrete.width, (int)fs.discrete.height});
    ::close(fd);
    double target = (double)tw / th;
    std::pair<int, int> best{0, 0};
    std::pair<bool, long> bestKey{true, -1};
    for (auto& z : sizes) {
        if (z.first < tw || z.second < th) continue;
        std::pair<bool, long> key{std::fabs((double)z.first / z.second - target) / target > 0.03, (long)z.first * z.second};
        if (bestKey.second < 0 || key < bestKey) { bestKey = key; best = z; }
    }
    if (bestKey.second < 0) return "";
    return std::to_string(best.first) + "x" + std::to_string(best.second);
}
#endif

static double mono() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
static const double AUDIO_LOST_S = 4.0;
static const double STREAM_GAP_S = 0.6;     // no stream frames this long: the picture held, silence sent

MediaTx::MediaTx() {
    lib_.setFileName(avmDir() + "/c++/avm");
    if (!lib_.load()) { error_ = "AVM library: " + lib_.errorString(); return; }
    auto r = [this](const char* n) { return lib_.resolve(n); };
    frCreate_ = (decltype(frCreate_))r("fr_create");
    frFree_ = (decltype(frFree_))r("fr_free");
    frSetAudioProfile_ = (decltype(frSetAudioProfile_))r("fr_set_audio_profile");
    frSetVideoConfig_ = (decltype(frSetVideoConfig_))r("fr_set_video_config");
    frSetMessage_ = (decltype(frSetMessage_))r("fr_set_message");
    frAddAudio_ = (decltype(frAddAudio_))r("fr_add_audio");
    frAddVideo_ = (decltype(frAddVideo_))r("fr_add_video");
    frTick_ = (decltype(frTick_))r("fr_tick");
    frFragment_ = (decltype(frFragment_))r("fr_fragment");
    wvtCreate_ = (decltype(wvtCreate_))r("wvt_create_auto");
    wvtEncode_ = (decltype(wvtEncode_))r("wvt_encode");
    wvtFree_ = (decltype(wvtFree_))r("wvt_free");
    ok_ = frCreate_ && frFree_ && frSetMessage_ && frSetAudioProfile_ && frSetVideoConfig_ && frAddAudio_ && frAddVideo_ && frTick_ &&
          frFragment_ && wvtCreate_ && wvtEncode_ && wvtFree_;
    if (!ok_) error_ = "AVM library too old -- rebuild AVM (cmake --build)";
}

MediaTx::~MediaTx() { stop(); }

bool MediaTx::start(const MediaTxConfig& c, OutFn out, LogFn log) {
    stop();
    if (!ok_) { log("ERROR: " + error_.toStdString()); return false; }
    if (c.video == "off" && c.audio == "off") { log("ERROR: no video and no audio to send"); return false; }
    cfg_ = c;
    out_ = std::move(out);
    log_ = std::move(log);
    fr_ = frCreate_(c.fragment_size, c.station_id.empty() ? nullptr : c.station_id.c_str(), (int)(time(nullptr) & 0xFF));
    if (!message_.empty()) frSetMessage_(fr_, message_.data(), (int)message_.size());
    frames_ = 0;
    enc_s_ = 0;
    audio_pkts_ = 0;
    audio_watch_ = c.audio == "mic";
    stream_ = c.video == "stream" || c.audio == "stream";
    stream_gap_ = stream_lost_ = have_pic_ = false;
    fill_audio_ = fill_video_ = 0;
    stream_vlast_ = stream_alast_ = mono();
    audio_lost_ = false;
    audio_last_ = mono();
    {
        std::lock_guard<std::mutex> g(mu_);
        video_q_.clear();
        audio_q_.clear();
        pcm_.clear();
    }
    const int W = c.width, H = c.height;
    CaptureInput stream_in;     // SRT / UDP: one input, its picture and its sound
    if (c.video != "off") {
        // the wavelet encoder as media_source_wavelet.py makes it: leak 0.996,
        // a whole-picture refresh every 5 s (at least 20 frames), motion while it's cheap
        long long refresh = std::max<long long>(std::llround(5.0 * c.fps), 20);
        int motion = W * H * c.fps <= MOTION_PIXEL_RATE;
        wvt_ = wvtCreate_(W, H, c.frame_bytes, 0.996, refresh, 1, motion, -1);
        if (!wvt_) { log_("ERROR: video size " + std::to_string(W) + "x" + std::to_string(H) + " (needs multiples of 32x16)"); stop(); return false; }
        y_.resize((size_t)W * H);
        u_.resize((size_t)(W / 2) * (H / 2));
        v_.resize(u_.size());
        pkt_.resize((size_t)c.frame_bytes + 4096);
        // VIDEO_CONFIG: [W][H][fps x 1000] + the codec tag
        int fps_x1000 = (int)std::min(65535L, std::max(0L, std::lround(c.fps * 1000)));
        std::vector<uint8_t> rec = {(uint8_t)VIDEO_CONFIG_TYPE, 0, 10, (uint8_t)(W >> 8), (uint8_t)W, (uint8_t)(H >> 8),
                                    (uint8_t)H, (uint8_t)(fps_x1000 >> 8), (uint8_t)fps_x1000};
        rec.insert(rec.end(), WAVELET_CODEC_TAG, WAVELET_CODEC_TAG + 4);
        frSetVideoConfig_(fr_, rec.data(), (int)rec.size());
        log_("[video] wavelet " + std::to_string(W) + "x" + std::to_string(H) + ", " + std::to_string(c.frame_bytes) +
             " B/frame = " + std::to_string(c.frame_bytes * 8 * c.fps / 1000).substr(0, 5) + " kbps" +
             (motion ? ", motion" : ""));
        char fps[32];
        snprintf(fps, sizeof fps, "%g", c.fps);
        CaptureInput in;
        in.video = true;
        std::string w = std::to_string(W), h = std::to_string(H);
        // (a stream: started below, once the audio encoder is there)
        std::string fit = "scale=" + w + ":" + h + ":force_original_aspect_ratio=decrease,pad=" + w + ":" + h +
                          ":(ow-iw)/2:(oh-ih)/2,fps=" + fps + ",format=yuv420p";
        if (c.video == "test") {
            in.format = "lavfi";
            in.url = "testsrc=size=" + std::to_string(W) + "x" + std::to_string(H) + ":rate=" + fps;
            in.realtime = true;
            in.filter = "format=yuv420p";
        } else if (c.video == "stream") {
            in.filter = fit;
        } else if (c.video == "image") {
            // media_source.py's --source image: -re -loop 1 -framerate FPS -i PATH
            in.format = "image2";
            in.url = c.image_path;
            in.options["loop"] = "1";
            in.options["framerate"] = fps;
            in.realtime = true;
            in.filter = fit;
        } else {
#ifdef _WIN32
            in.format = "dshow";
            in.url = "video=" + (c.video_device.empty() ? std::string("Integrated Camera") : c.video_device);
#else
            in.format = "v4l2";
            in.url = c.video_device.empty() ? std::string("/dev/video0") : c.video_device;
            std::string size = v4l2_capture_size(in.url, W, H);
            if (!size.empty()) {
                in.options["input_format"] = "yuyv422";
                in.options["video_size"] = size;
                log_("[video] camera at " + size);
            }
#endif
            in.filter = fit;
        }
        if (c.video != "stream") video_cap_.start(in, [this](AVFrame* f) { videoFrame(f); }, log_);
        else stream_in.filter = in.filter;
    }
    if (c.audio != "off") {
        bool opus = c.audio_codec == "opus";
        if (opus) {
            // CBR, complexity 10, 20 ms frames. The test tone from 10 kbit/s in the
            // low-delay mode (the music coder alone: the default's switching between
            // coders around 12-16 kbit/s broke up a steady tone).
            // Speech (Config, TX media): the VoIP tuning and the signal flagged as voice --
            // what holds speech together at the slow links' 4-6 kbit/s
            bool lowdelay = c.audio == "test" && c.opus_kbps >= 10;
            bool speech = c.opus_speech && c.audio != "test";
            int err = 0;
            opus_ = opus_encoder_create(48000, 1, lowdelay ? OPUS_APPLICATION_RESTRICTED_LOWDELAY
                                                  : speech ? OPUS_APPLICATION_VOIP : OPUS_APPLICATION_AUDIO, &err);
            if (err != OPUS_OK || !opus_) { log_("ERROR: Opus encoder: " + std::string(opus_strerror(err))); stop(); return false; }
            opus_encoder_ctl((OpusEncoder*)opus_, OPUS_SET_BITRATE((opus_int32)std::lround(c.opus_kbps * 1000)));
            opus_encoder_ctl((OpusEncoder*)opus_, OPUS_SET_VBR(0));
            opus_encoder_ctl((OpusEncoder*)opus_, OPUS_SET_COMPLEXITY(10));     // (one mono voice: cheap even on a Pi)
            if (speech) opus_encoder_ctl((OpusEncoder*)opus_, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
            frSetAudioProfile_(fr_, AUDIO_PROFILE_OPUS_48K);
            log_("[audio] Opus " + std::to_string(c.opus_kbps).substr(0, 4) + " kbps, 20 ms frames" + (speech ? ", speech" : ""));
        } else {
            c2_ = codec2_create(CODEC2_MODE_3200);
            frSetAudioProfile_(fr_, AUDIO_PROFILE_CODEC2_3200);
            log_("[audio] Codec2 3200");
        }
        CaptureInput in;
        in.video = false;
        std::string rate = opus ? "48000" : "8000";
        std::string to8k = "aformat=sample_fmts=s16:sample_rates=" + rate + ":channel_layouts=mono";
        if (c.audio == "stream") {
            stream_in.audio_filter = "aresample=async=1000," + to8k;
        } else if (c.audio == "test") {     // 400 Hz: clean through Opus and Codec2 alike (1 kHz warbled in Opus)
            in.format = "lavfi";
            in.url = "sine=f=400:r=" + rate;
            in.realtime = true;
            in.filter = to8k;
        } else {
            // a real device locked to the wall clock (media_source_audio.py:
            // arrival timestamps, aresample stretching to match)
            in.options["use_wallclock_as_timestamps"] = "1";
#ifdef _WIN32
            in.format = "dshow";
            in.url = "audio=" + c.audio_device;
            in.options["audio_buffer_size"] = "20";
#else
            in.format = "pulse";
            in.url = c.audio_device.empty() ? std::string("default") : c.audio_device;
#endif
            in.filter = "aresample=async=1000," + to8k;
        }
        if (c.audio != "stream") audio_cap_.start(in, [this](AVFrame* f) { audioFrame(f); }, log_);
    }
    if (stream_) {
        if (c.stream_url.empty()) { log_("ERROR: no stream URL (Config, TX media)"); stop(); return false; }
        stream_in.url = c.stream_url;
        // OBS's SRT / UDP output: MPEG-TS; probed far enough to find its picture size
        stream_in.format = "mpegts";
        stream_in.options["probesize"] = "500000";
        stream_in.options["analyzeduration"] = "1000000";
        stream_in.options["fflags"] = "discardcorrupt";   // (not nobuffer: it threw away the probed packets, the first keyframe with them)
        stream_in.reconnect = true;
        auto audio = [this](AVFrame* f) { audioFrame(f); };
        if (c.video == "stream") {
            stream_in.video = true;
            stream_in.with_audio = c.audio == "stream";
            stream_cap_.start(stream_in, [this](AVFrame* f) { videoFrame(f); }, log_, audio);
        } else {    // its sound alone
            stream_in.video = false;
            stream_in.filter = stream_in.audio_filter;
            stream_cap_.start(stream_in, audio, log_);
        }
        log_("[stream] waiting for " + c.stream_url);
    }
    stop_ = false;
    running_ = true;
    asm_th_ = std::thread([this] { assemble(); });
    return true;
}

void MediaTx::stop() {
    stop_ = true;
    video_cap_.stop();
    audio_cap_.stop();
    stream_cap_.stop();
    if (asm_th_.joinable()) asm_th_.join();
    running_ = false;
    if (wvt_) { wvtFree_(wvt_); wvt_ = nullptr; }
    if (c2_) { codec2_destroy((CODEC2*)c2_); c2_ = nullptr; }
    if (opus_) { opus_encoder_destroy((OpusEncoder*)opus_); opus_ = nullptr; }
    if (fr_) { frFree_(fr_); fr_ = nullptr; }
}

static uint32_t crc32(const uint8_t* p, size_t n) {
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) {
        c ^= p[i];
        for (int k = 0; k < 8; ++k) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1)));
    }
    return ~c;
}

// [4-byte UTC time][UTF-8 text, 80 characters at most][CRC32 of both]
std::vector<uint8_t> MediaTx::buildMessage(long long ts, const QString& text) {
    QString t = text.trimmed().left(80);
    if (t.isEmpty()) return {};
    QByteArray u = t.toUtf8();
    std::vector<uint8_t> m = {(uint8_t)(ts >> 24), (uint8_t)(ts >> 16), (uint8_t)(ts >> 8), (uint8_t)ts};
    m.insert(m.end(), u.begin(), u.end());
    uint32_t c = crc32(m.data(), m.size());
    for (int s = 24; s >= 0; s -= 8) m.push_back((uint8_t)(c >> s));
    return m;
}

void MediaTx::setMessage(const QString& text) {
    std::lock_guard<std::mutex> g(mu_);
    message_ = buildMessage((long long)time(nullptr), text);
    if (fr_) frSetMessage_(fr_, message_.data(), (int)message_.size());
}

// a picture: encoded to exactly frame_bytes, queued for the framer
void MediaTx::videoFrame(AVFrame* f) {
    const int W = cfg_.width, H = cfg_.height;
    if (f->width != W || f->height != H) return;
    stream_vlast_ = mono();
    std::lock_guard<std::mutex> e(enc_mu_);
    for (int r = 0; r < H; ++r) std::memcpy(&y_[(size_t)r * W], f->data[0] + (size_t)r * f->linesize[0], W);
    if (cfg_.mono) {
        std::fill(u_.begin(), u_.end(), 128);
        std::fill(v_.begin(), v_.end(), 128);
    } else {
        for (int r = 0; r < H / 2; ++r) {
            std::memcpy(&u_[(size_t)r * (W / 2)], f->data[1] + (size_t)r * f->linesize[1], W / 2);
            std::memcpy(&v_[(size_t)r * (W / 2)], f->data[2] + (size_t)r * f->linesize[2], W / 2);
        }
    }
    have_pic_ = true;
    if (preview_) preview_(i420ToImage(W, H, y_.data(), u_.data(), v_.data()));
    encodePicture();
}

void MediaTx::encodePicture() {
    double t0 = mono();
    int n = wvtEncode_(wvt_, y_.data(), u_.data(), v_.data(), pkt_.data());
    enc_s_ += mono() - t0;
    if (n <= 0) return;
    if (++frames_ % 100 == 0)
        log_("[video] " + std::to_string(frames_) + " frames, avg encode " +
             std::to_string(enc_s_ / frames_ * 1000).substr(0, 4) + " ms");
    std::lock_guard<std::mutex> g(mu_);
    video_q_.emplace_back(pkt_.begin(), pkt_.begin() + n);
}

// every 500 audio packets, their rate (each 20 ms: 50 a second when real time)
void MediaTx::countAudioPacket(double expected_per_s) {
    double now = mono();
    if (audio_pkts_++ == 0) audio_t0_ = now;
    else if (audio_pkts_ % 500 == 0 && now > audio_t0_)
        log_("[audio] " + std::to_string(audio_pkts_) + " packets, " +
             std::to_string((audio_pkts_ - 1) / (now - audio_t0_) / expected_per_s).substr(0, 5) + "x real time");
}

// mono s16 -> 20 ms frames: Codec2 3200 (8 kHz, 160 samples) or Opus (48 kHz, 960)
void MediaTx::audioFrame(AVFrame* f) {
    audio_last_ = mono();
    stream_alast_ = audio_last_.load();
    std::lock_guard<std::mutex> g(mu_);
    addPcm((const short*)f->data[0], f->nb_samples);
}

void MediaTx::addPcm(const short* s, int count) {
    pcm_.insert(pcm_.end(), s, s + count);
    size_t used = 0;
    if (opus_) {
        uint8_t pkt[1500];
        while (pcm_.size() - used >= (size_t)OPUS_SAMPLES) {
            int n = opus_encode((OpusEncoder*)opus_, &pcm_[used], OPUS_SAMPLES, pkt, sizeof pkt);
            if (n > 0) audio_q_.emplace_back(pkt, pkt + n);
            used += OPUS_SAMPLES;
            countAudioPacket(50);
        }
        pcm_.erase(pcm_.begin(), pcm_.begin() + used);
        return;
    }
    uint8_t bits[CODEC2_BYTES];
    while (pcm_.size() - used >= (size_t)CODEC2_SAMPLES) {
        codec2_encode((CODEC2*)c2_, bits, &pcm_[used]);
        audio_q_.emplace_back(bits, bits + CODEC2_BYTES);
        used += CODEC2_SAMPLES;
        countAudioPacket(50);
    }
    pcm_.erase(pcm_.begin(), pcm_.begin() + used);
}

// once a fragment period: the stream away -- its last picture again at the frame rate
// and silence in place of its sound, so the far end stays locked and new receivers
// still get a picture; gone stream_lost_s: TX stopped (pages.cpp)
void MediaTx::fillStreamGap() {
    const double now = mono();
    const bool v = cfg_.video == "stream", a = cfg_.audio == "stream";
    // the picture's own pace counts: at 1 fps a second between frames isn't a break
    // (taken as one, held copies filled the gaps and the real pictures queued behind them)
    const double vlimit = std::max(STREAM_GAP_S, 2.5 / std::max(cfg_.fps, 0.1));
    const double vgap = (now - stream_vlast_) * STREAM_GAP_S / vlimit, agap = now - stream_alast_;
    double gap = v && a ? std::max(vgap, agap) : v ? vgap : agap;     // (either missing: a break)
    if (gap < STREAM_GAP_S) {
        if (stream_gap_) log_("[stream] back");
        stream_gap_ = false;
        fill_audio_ = fill_video_ = 0;
        return;
    }
    if (!stream_gap_) {
        stream_gap_ = true;
        log_(have_pic_ ? "[stream] lost -- holding the picture, sending silence" : "[stream] nothing yet -- sending silence");
    }
    if (!stream_lost_ && cfg_.stream_lost_s > 0 && std::min(v ? now - stream_vlast_ : 1e9, a ? agap : 1e9) > cfg_.stream_lost_s) {
        stream_lost_ = true;
        char b[160];
        snprintf(b, sizeof b, "ERROR: the stream stopped (nothing for %.0f s) -- TX stopped", cfg_.stream_lost_s);
        log_(b);
    }
    double p = period_.load();
    if (a && agap >= STREAM_GAP_S && (opus_ || c2_)) {
        fill_audio_ += p / 0.020;      // 20 ms frames
        int n = (int)fill_audio_;
        fill_audio_ -= n;
        std::vector<short> zero((size_t)n * (opus_ ? OPUS_SAMPLES : CODEC2_SAMPLES), 0);
        std::lock_guard<std::mutex> g(mu_);
        addPcm(zero.data(), (int)zero.size());
    }
    if (v && vgap >= STREAM_GAP_S && wvt_ && have_pic_) {
        fill_video_ += p * cfg_.fps;
        std::lock_guard<std::mutex> e(enc_mu_);
        for (; fill_video_ >= 1; fill_video_ -= 1) encodePicture();
    }
}

// media_tx_framer.py's assembler: every fragment period, whatever has
// arrived packed into fragments (drift-corrected; resynced when late)
void MediaTx::assemble() {
    period_ = cfg_.fragment_period_s;
    double next = mono();
    std::vector<uint8_t> frag(cfg_.fragment_size);
    while (!stop_) {
        if (stream_) fillStreamGap();
        if (audio_watch_ && !audio_lost_ && mono() - audio_last_ > AUDIO_LOST_S) {
            audio_lost_ = true;
            log_("ERROR: the audio input stopped (unplugged?) -- TX stopped");
        }
        {
            std::lock_guard<std::mutex> g(mu_);
            for (auto& a : audio_q_) frAddAudio_(fr_, a.data(), (int)a.size());
            for (auto& v : video_q_) frAddVideo_(fr_, v.data(), (int)v.size(), 0);
            audio_q_.clear();
            video_q_.clear();
        }
        std::unique_lock<std::mutex> lk(mu_);       // (setMessage)
        int n = frTick_(fr_, mono());
        lk.unlock();
        for (int i = 0; i < n; ++i) {
            frFragment_(fr_, i, frag.data());
            out_(frag);
        }
        next += period_.load();
        double wait = next - mono();
        if (wait > 0) {
            auto until = std::chrono::steady_clock::now() + std::chrono::duration<double>(wait);
            while (!stop_ && std::chrono::steady_clock::now() < until)
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
        } else {
            next = mono();
        }
    }
}

}  // namespace avm
