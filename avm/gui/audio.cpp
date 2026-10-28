#include "audio.h"

// (its implementation is miniaudio.cpp's, built once; the options as there)
#define MA_NO_ENCODING
#define MA_NO_DECODING
#define MA_NO_GENERATION
#include "miniaudio.h"

#include <opus.h>
#include "codec2.h"

#include <QJsonArray>
#include <algorithm>
#include <cstring>
#include <QJsonDocument>
#include <QJsonObject>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <thread>
#ifndef _WIN32
#include <csignal>
#include <fcntl.h>
#include <unistd.h>
#endif

namespace avm {

const double MAX_BUFFER_S = 4.0;  // more than this: drop the oldest (a backlog is only delay)
const double SYNC_TAU_S = 2.0, SYNC_GAIN = 0.1, SYNC_MAX_ADJ = 0.005;   // media_rx_player's
// a drop (backlog trimmed): a 20 ms equal-power crossfade across the cut; an
// underrun: 10 ms faded out where the sound ran out and in where it resumes
const size_t XFADE = 960, EDGE_FADE = 480;

void AudioOut::dropFront(size_t n) {
    n = std::min(n, buf_.size());
    size_t f = std::min(XFADE, buf_.size() - n);
    for (size_t i = 0; i < f; ++i) {
        double t = (i + 0.5) / f * 1.5707963267948966;
        buf_[n + i] = (float)(buf_[i] * std::cos(t) + buf_[n + i] * std::sin(t));
    }
    buf_.erase(buf_.begin(), buf_.begin() + n);
}

struct AudioOut::Impl {
    ma_context ctx;
    ma_device dev;
    bool ctx_init = false, init = false;
    // Linux with PipeWire: its own player, pw-cat, fed through a pipe by a writer
    // thread (media_rx_player._PwCatStream). Through PipeWire's PulseAudio / ALSA
    // compatibility layers a busy Pi crackled with clean decoded audio, no
    // underruns and no xruns reported; a native PipeWire client stayed clean.
    FILE* pw = nullptr;
    std::thread pwThread;
    std::atomic<bool> pwStop{false};
    // an output PipeWire doesn't offer, played straight to ALSA ("... (ALSA)")
    ma_context alsa;
    bool alsa_init = false;
};

#ifndef _WIN32
// an output listed from ALSA itself: one PipeWire doesn't offer -- e.g. HDMI on a
// screen that sends no EDID (PipeWire then assumes it has no speakers)
static const std::string ALSA_SUFFIX = " (ALSA)";

// PipeWire's outputs (their properties), from pw-dump
static QJsonArray pwSinks() {
    QJsonArray out;
    FILE* p = popen("pw-dump 2>/dev/null", "r");
    if (!p) return out;
    QByteArray json;
    char b[65536];
    size_t n;
    while ((n = fread(b, 1, sizeof b, p)) > 0) json.append(b, (int)n);
    pclose(p);
    for (const QJsonValue& v : QJsonDocument::fromJson(json).array()) {
        QJsonObject props = v.toObject().value("info").toObject().value("props").toObject();
        if (props.value("media.class").toString() == "Audio/Sink") out.append(props);
    }
    return out;
}

// PipeWire's node name for an output listed by its description (miniaudio's
// names through PipeWire's PulseAudio layer: "USB Audio Device Analog Stereo");
// "" if there's none
static std::string pwSinkFor(const std::string& description) {
    for (const QJsonValue& v : pwSinks()) {
        QJsonObject props = v.toObject();
        if (props.value("node.description").toString().toStdString() == description)
            return props.value("node.name").toString().toStdString();
    }
    return "";
}

// the ALSA cards PipeWire plays to (their outputs are listed through it already)
static std::vector<std::string> pwAlsaCards() {
    std::vector<std::string> cards;
    for (const QJsonValue& v : pwSinks()) {
        QString c = v.toObject().value("api.alsa.card.name").toString();
        if (!c.isEmpty()) cards.push_back(c.toStdString());
    }
    return cards;
}

static bool alsaContext(ma_context* ctx) {
    ma_backend b = ma_backend_alsa;
    return ma_context_init(&b, 1, nullptr, ctx) == MA_SUCCESS;
}

// pw-cat there and PipeWire running (AVM_AUDIO_PWCAT=0: miniaudio instead)
static bool usePwCat() {
    const char* off = std::getenv("AVM_AUDIO_PWCAT");
    if (off && std::string(off) == "0") return false;
    const char* rt = std::getenv("XDG_RUNTIME_DIR");
    if (!rt || access((std::string(rt) + "/pipewire-0").c_str(), F_OK) != 0) return false;
    return std::system("command -v pw-cat >/dev/null 2>&1") == 0;
}
#endif

static void ma_callback(ma_device* dev, void* out, const void*, ma_uint32 frames) {
    ((AudioOut*)dev->pUserData)->pull((float*)out, frames);
}

AudioOut::AudioOut() : impl_(new Impl) {
    impl_->ctx_init = ma_context_init(nullptr, 0, nullptr, &impl_->ctx) == MA_SUCCESS;
    open("");
}

void AudioOut::close() {
    if (impl_->init) ma_device_uninit(&impl_->dev);
    impl_->init = false;
    if (impl_->alsa_init) ma_context_uninit(&impl_->alsa);
    impl_->alsa_init = false;
    if (impl_->pw) {
        impl_->pwStop = true;
        if (impl_->pwThread.joinable()) impl_->pwThread.join();
#ifndef _WIN32
        pclose(impl_->pw);
#endif
        impl_->pw = nullptr;
    }
    ok_ = false;
}

AudioOut::~AudioOut() {
    close();
    if (impl_->ctx_init) ma_context_uninit(&impl_->ctx);
}

std::vector<std::string> AudioOut::outputs() {
    std::vector<std::string> names;
    ma_context ctx;
    if (ma_context_init(nullptr, 0, nullptr, &ctx) != MA_SUCCESS) return names;
    ma_device_info* infos;
    ma_uint32 n;
    if (ma_context_get_devices(&ctx, &infos, &n, nullptr, nullptr) == MA_SUCCESS)
        for (ma_uint32 i = 0; i < n; ++i) names.push_back(infos[i].name);
    ma_context_uninit(&ctx);
#ifndef _WIN32
    // with PipeWire: also the ALSA outputs on cards it doesn't play to (played directly)
    if (usePwCat() && alsaContext(&ctx)) {
        std::vector<std::string> pw = pwAlsaCards();
        if (ma_context_get_devices(&ctx, &infos, &n, nullptr, nullptr) == MA_SUCCESS)
            for (ma_uint32 i = 0; i < n; ++i) {
                std::string name = infos[i].name;
                bool used = false;
                for (const auto& c : pw) used = used || name.find(c) != std::string::npos;
                // hardware only ("card, device"): not ALSA's null device or the
                // default / PipeWire plugins, which play through PipeWire anyway
                used = used || name.find(", ") == std::string::npos || name.find("PipeWire") != std::string::npos;
                if (!used && std::find(names.begin(), names.end(), name + ALSA_SUFFIX) == names.end())
                    names.push_back(name + ALSA_SUFFIX);
            }
        ma_context_uninit(&ctx);
    }
#endif
    return names;
}

bool AudioOut::open(const std::string& device) {
    close();
#ifndef _WIN32
    // the default output, or a card PipeWire knows (aimed at with --target)
    std::string target = device.empty() || !usePwCat() ? std::string() : pwSinkFor(device);
    if ((device.empty() || !target.empty()) && usePwCat()) {
        // 20 ms blocks from pull(), written as s16; the pipe's blocking write
        // paces it at the playback rate. A small pipe (~16 KB, 0.17 s): Linux's
        // default 64 KB would add the better part of a second
        std::signal(SIGPIPE, SIG_IGN);
        std::string cmd = "pw-cat --playback --raw --format s16 --rate 48000 --channels 1 "
                          "--latency 100ms --media-category Playback ";
        if (!target.empty()) cmd += "--target '" + target + "' ";
        impl_->pw = popen((cmd + "-").c_str(), "w");
        if (impl_->pw) {
            fcntl(fileno(impl_->pw), 1031 /* F_SETPIPE_SZ */, 16384);
            impl_->pwStop = false;
            impl_->pwThread = std::thread([this] {
                const size_t N = RATE / 50;
                std::vector<float> f(N);
                std::vector<short> s(N);
                while (!impl_->pwStop) {
                    pull(f.data(), N);
                    for (size_t i = 0; i < N; ++i)
                        s[i] = (short)std::lround(std::max(-1.0f, std::min(1.0f, f[i])) * 32767.0f);
                    if (fwrite(s.data(), sizeof(short), N, impl_->pw) != N || fflush(impl_->pw) != 0) break;
                }
            });
            ok_ = true;
            return true;
        }
    }
#endif
    ma_device_config cfg = ma_device_config_init(ma_device_type_playback);
    cfg.playback.format = ma_format_f32;
    cfg.playback.channels = 1;
    cfg.sampleRate = RATE;
    cfg.dataCallback = ma_callback;
    cfg.pUserData = this;
    // a roomy device buffer, as the Python player's latency="high": the default
    // ~10 ms periods click on a busy Pi (TX and RX at once)
    cfg.performanceProfile = ma_performance_profile_conservative;
    cfg.periodSizeInMilliseconds = 40;
    cfg.periods = 3;
    ma_device_id id;
#ifndef _WIN32
    // an output listed from ALSA itself ("... (ALSA)"): opened through ALSA directly
    if (device.size() > ALSA_SUFFIX.size() &&
        device.compare(device.size() - ALSA_SUFFIX.size(), ALSA_SUFFIX.size(), ALSA_SUFFIX) == 0) {
        const std::string name = device.substr(0, device.size() - ALSA_SUFFIX.size());
        if (!(impl_->alsa_init = alsaContext(&impl_->alsa))) return false;
        ma_device_info* infos;
        ma_uint32 n;
        if (ma_context_get_devices(&impl_->alsa, &infos, &n, nullptr, nullptr) == MA_SUCCESS)
            for (ma_uint32 i = 0; i < n; ++i)
                if (name == infos[i].name) { id = infos[i].id; cfg.playback.pDeviceID = &id; break; }
        if (!cfg.playback.pDeviceID) return false;       // (gone: unplugged since)
        // through ALSA's converter (plughw): a Pi's HDMI takes only IEC958 frames on
        // its raw hw device ("Sample format non available")
        if (std::strncmp(id.alsa, "hw:", 3) == 0) {
            std::string p = std::string("plughw:") + (id.alsa + 3);
            std::snprintf(id.alsa, sizeof id.alsa, "%s", p.c_str());
        }
        if (ma_device_init(&impl_->alsa, &cfg, &impl_->dev) != MA_SUCCESS) return false;
        impl_->init = true;
        ok_ = ma_device_start(&impl_->dev) == MA_SUCCESS;
        return ok_;
    }
#endif
    if (!device.empty() && impl_->ctx_init) {
        ma_device_info* infos;
        ma_uint32 n;
        if (ma_context_get_devices(&impl_->ctx, &infos, &n, nullptr, nullptr) == MA_SUCCESS)
            for (ma_uint32 i = 0; i < n; ++i)
                if (device == infos[i].name) { id = infos[i].id; cfg.playback.pDeviceID = &id; break; }
    }
    if (ma_device_init(impl_->ctx_init ? &impl_->ctx : nullptr, &cfg, &impl_->dev) != MA_SUCCESS) return false;
    impl_->init = true;
    ok_ = ma_device_start(&impl_->dev) == MA_SUCCESS;
    return ok_;
}

void AudioOut::push(const float* pcm, size_t n) {
    if (tap) tap(pcm, n);
    std::lock_guard<std::mutex> g(mu_);
    buf_.insert(buf_.end(), pcm, pcm + n);
    pushed_ += n;
    size_t cap = (size_t)(MAX_BUFFER_S * RATE);
    if (buf_.size() > cap) dropFront(buf_.size() - cap);
}

void AudioOut::clear() {
    std::lock_guard<std::mutex> g(mu_);
    buf_.clear();
    playing_ = false;
    backlog_ema_ = -1;
    rate_ = 1.0;
    frac_ = 0;
    played_s_ = 0;
    trimmed_ = false;
    fadeIn_ = 0;
}

double AudioOut::buffered_s() {
    std::lock_guard<std::mutex> g(mu_);
    return (double)buf_.size() / RATE;
}

QString AudioOut::callbackStats() {
    std::lock_guard<std::mutex> g(mu_);
    // the audio that arrived per second of wall time since the last call: under
    // 1.00 for long, the buffer drains (underruns); over, it fills (drops)
    auto now = std::chrono::steady_clock::now();
    double span = statsT_.time_since_epoch().count() ? std::chrono::duration<double>(now - statsT_).count() : 0;
    QString in = span > 0 ? QString("in %1 s/s, ").arg((double)pushed_ / RATE / span, 0, 'f', 3) : QString();
    statsT_ = now;
    pushed_ = 0;
    QString s = in + QString("callbacks %1, longest gap %2 ms, up to %3 frames, backend %4")
                    .arg(callbacks_).arg(maxGapMs_, 0, 'f', 0).arg(maxFrames_)
                    .arg(impl_->pw ? "pw-cat (PipeWire)" : impl_->init ? ma_get_backend_name(impl_->dev.pContext->backend) : "none");
    maxGapMs_ = 0;
    maxFrames_ = 0;
    callbacks_ = 0;
    return s;
}

void AudioOut::pull(float* out, size_t n) {
    std::lock_guard<std::mutex> g(mu_);
    auto now = std::chrono::steady_clock::now();
    if (lastCb_.time_since_epoch().count())
        maxGapMs_ = std::max(maxGapMs_, std::chrono::duration<double, std::milli>(now - lastCb_).count());
    lastCb_ = now;
    maxFrames_ = std::max(maxFrames_, n);
    ++callbacks_;
    if (!playing_ && buf_.size() >= (size_t)(target_s_ * RATE)) {
        playing_ = true;
        fadeIn_ = EDGE_FADE;
    }
    size_t k = 0;
    if (playing_) {
        // the backlog, smoothed (it arrives a fragment at a time), steers the rate
        double backlog = (double)buf_.size() / RATE, dt = (double)n / RATE;
        backlog_ema_ = backlog_ema_ < 0 ? backlog : backlog_ema_ + std::min(1.0, dt / SYNC_TAU_S) * (backlog - backlog_ema_);
        played_s_ += dt;
        double err = backlog_ema_ - hold_s_;
        if (!trimmed_ && played_s_ >= 2.0) {
            // one-off, 2 s in: the start-up excess dropped now (a single small
            // skip, early) rather than played off at 0.5% over a minute
            trimmed_ = true;
            if (err > 0.05) {
                size_t drop = std::min(buf_.size(), (size_t)(err * RATE));
                dropFront(drop);
                backlog_ema_ -= (double)drop / RATE;
                err = backlog_ema_ - hold_s_;
            }
        }
        rate_ = 1.0 + std::max(-SYNC_MAX_ADJ, std::min(SYNC_MAX_ADJ, SYNC_GAIN * err));
        // linear interpolation at that rate
        for (; k < n; ++k) {
            size_t i = (size_t)frac_;
            if (i + 1 >= buf_.size()) break;
            double f = frac_ - (double)i;
            out[k] = (float)((buf_[i] * (1 - f) + buf_[i + 1] * f) * volume_);
            if (fadeIn_) { out[k] *= (float)(EDGE_FADE - fadeIn_) / EDGE_FADE; --fadeIn_; }
            frac_ += rate_;
        }
        size_t used = std::min((size_t)frac_, buf_.size());
        buf_.erase(buf_.begin(), buf_.begin() + used);
        frac_ -= (double)used;
        if (k < n) {        // underrun: build the cushion up again
            size_t fo = std::min(k, EDGE_FADE);      // (the sound so far faded out, not cut)
            for (size_t j = 0; j < fo; ++j) out[k - fo + j] *= (float)(fo - j) / (fo + 1);
            playing_ = false;
            ++underruns;
            frac_ = 0;
        }
    }
    std::fill(out + k, out + n, 0.0f);
}

AudioDecoder::~AudioDecoder() { reset(); }

void AudioDecoder::reset() {
    if (opus_) { opus_decoder_destroy((OpusDecoder*)opus_); opus_ = nullptr; }
    if (c2_) { codec2_destroy((CODEC2*)c2_); c2_ = nullptr; }
    profile_ = -1;
    last_ = 0;
}

void AudioDecoder::setProfile(int id) {
    if (id == profile_) return;
    reset();
    profile_ = id;
    if (id == 0) {
        int err = 0;
        opus_ = opus_decoder_create(AudioOut::RATE, 1, &err);
        if (err != OPUS_OK) opus_ = nullptr;
    } else if (id == 1) {
        c2_ = codec2_create(CODEC2_MODE_3200);
    }
}

void AudioDecoder::decode(const unsigned char* p, size_t n) {
    if (opus_) {
        pcm_.resize(5760);                       // 120 ms at 48 kHz: Opus's longest frame
        int k = opus_decode((OpusDecoder*)opus_, p, (opus_int32)n, pcm_.data(), 5760, 0);
        if (k <= 0) return;
        f_.resize(k);
        for (int i = 0; i < k; ++i) f_[i] = pcm_[i] / 32768.0f;
        out_.push(f_.data(), f_.size());
    } else if (c2_) {
        CODEC2* c = (CODEC2*)c2_;
        int nb = (codec2_bits_per_frame(c) + 7) / 8, ns = codec2_samples_per_frame(c);
        pcm_.resize(ns);
        for (size_t off = 0; off + nb <= n; off += nb) {
            codec2_decode(c, pcm_.data(), p + off);
            // 8 kHz -> 48 kHz, linear
            f_.resize((size_t)ns * 6);
            for (int i = 0; i < ns; ++i) {
                float s = pcm_[i] / 32768.0f;
                for (int j = 0; j < 6; ++j) f_[(size_t)i * 6 + j] = last_ + (s - last_) * (j + 1) / 6.0f;
                last_ = s;
            }
            out_.push(f_.data(), f_.size());
        }
    }
}

}  // namespace avm
