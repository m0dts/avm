#include "link_plan.h"

#include <algorithm>
#include <cmath>

namespace avm {

// media_tx_framer.py / media_tx_gui.py's constants
static const int RECORD_HEADER_LEN = 3;
static const int AUDIO_PROFILE_CONFIG_BYTES = RECORD_HEADER_LEN + 1 + 2;
static const double AUDIO_PACKET_INTERVAL_S = 0.02;
static const int VIDEO_CONFIG_REPEAT_EVERY = 25;
static const double VIDEO_CONFIG_REPEAT_S = 2.0;
static const int WAVELET_CONFIG_BYTES = RECORD_HEADER_LEN + 6 + 4;
static const int CODEC2_FRAME_BYTES = 8;
static const double LOW_LINK_KBPS = 16;              // touch_tx_page.py: Codec2 below, Opus from here
static const double AUDIO_SHARE = 0.30, AUDIO_MIN_KBPS = 5, AUDIO_MAX_KBPS = 16;

// touch_widgets.VIDEO_PRESETS / VIDEO_PRESETS_10FPS: (from kbps, size, fps)
struct Preset { double kbps; int w, h; double fps; };
// (below 2 kbps: 128x80 -- the slowest links, SH / A at 10 kHz QPSK, get a picture
// at ~0.15 bit a pixel, sharpening over a few frames, rather than none)
static const Preset PRESETS[] = {{150, 384, 224, 15}, {80, 384, 224, 10}, {40, 384, 224, 6}, {20, 384, 224, 4},
                                 {12, 256, 144, 4},   {4, 256, 144, 2},   {2, 192, 112, 1}, {0, 128, 80, 1}};
static const double VIDEO_MIN_KBPS = 1.0;   // below this, audio only (the Python GUI: 2)
static const Preset PRESETS_10FPS[] = {{25, 384, 224, 10}, {12, 256, 144, 10}, {6, 224, 128, 10}, {0, 128, 80, 10}};
// Config, TX media, Video motion "Smooth" (links up to 20 kHz; wider ones run
// at 10 fps anyway): about PRESETS' bits per pixel at roughly twice the frame
// rate -- smaller pictures, smoother movement (estimates, not yet measured)
static const Preset PRESETS_SMOOTH[] = {{40, 288, 160, 10}, {20, 256, 144, 8}, {12, 224, 128, 6},
                                        {6, 192, 112, 5},   {3, 128, 80, 4},   {0, 128, 80, 3}};
static bool g_smooth = false;
void setVideoSmooth(bool on) { g_smooth = on; }
bool videoSmooth() { return g_smooth; }

template <size_t N>
static const Preset& pick(const Preset (&t)[N], double kbps) {
    for (const Preset& p : t)
        if (kbps >= p.kbps) return p;
    return t[N - 1];
}

// _wavelet_video_kbps: one fragment per tick, less its fixed contents, shared by the frames
static double waveletVideoKbps(int frag_bytes, double frag_s, double fps, bool station_id, const QString& codec,
                               double audio_kbps, int* frame_bytes, bool audio = true) {
    const double QUEUE_SLACK = 0.01;
    double budget = frag_bytes - 2;
    if (station_id) budget -= RECORD_HEADER_LEN + 2;
    if (audio) {
        budget -= AUDIO_PROFILE_CONFIG_BYTES;
        double pkt = codec == "codec2" ? CODEC2_FRAME_BYTES : std::ceil(audio_kbps * 1000 * AUDIO_PACKET_INTERVAL_S / 8);
        budget -= frag_s / AUDIO_PACKET_INTERVAL_S * (pkt + 1);
    }
    double frames = fps * frag_s;
    double configs = std::max(frames / VIDEO_CONFIG_REPEAT_EVERY, frag_s / VIDEO_CONFIG_REPEAT_S);
    budget -= configs * WAVELET_CONFIG_BYTES;
    int fb = (int)std::floor(budget * (1 - QUEUE_SLACK) / frames - RECORD_HEADER_LEN);
    *frame_bytes = std::max(0, fb);
    return std::max(0.0, *frame_bytes * 8 * fps / 1000);
}

LinkPlan planLink(double link_bps, int frag_bytes, double width_khz, bool station_id) {
    LinkPlan p;
    if (link_bps <= 0 || frag_bytes <= 0) return p;
    p.link_kbps = link_bps / 1000;
    p.fragment_s = frag_bytes * 8 / link_bps;
    // the framing's overhead (with the frame rate a link like this gets)
    double fps_guess = width_khz > 20 ? 10 : 4;
    double overhead = 2 + AUDIO_PROFILE_CONFIG_BYTES + p.fragment_s / AUDIO_PACKET_INTERVAL_S * 1 +
                      p.fragment_s * fps_guess * RECORD_HEADER_LEN +
                      p.fragment_s * fps_guess / VIDEO_CONFIG_REPEAT_EVERY * 50;
    p.usable_kbps = p.link_kbps * (1 - std::min(overhead / frag_bytes, 0.5));
    // audio: Codec2 on the slow links, Opus at 30% of the link (5-16 kbps),
    // no faster than whole packets fit the fragments
    if (p.usable_kbps < LOW_LINK_KBPS) {
        p.audio_codec = "codec2";
    } else {
        p.audio_codec = "opus";
        double a = std::min(std::max(p.usable_kbps * AUDIO_SHARE, AUDIO_MIN_KBPS), AUDIO_MAX_KBPS);
        auto safe = [&](double kbps) {
            double pkt = kbps * 1000 * AUDIO_PACKET_INTERVAL_S / 8 + 1;
            int per = std::max(1, (int)((frag_bytes - 2 - AUDIO_PROFILE_CONFIG_BYTES) / pkt));
            return (1 / AUDIO_PACKET_INTERVAL_S) / per <= 1 / p.fragment_s;
        };
        if (!safe(a)) {
            double best = AUDIO_MIN_KBPS;
            for (double c = AUDIO_MIN_KBPS; c <= a; c += 0.1)
                if (safe(c)) best = c;
            a = best;
        }
        p.opus_kbps = std::floor(a * 10) / 10;
    }
    // video: the exact rate at a first frame rate, its preset, then the rate at the preset's
    int fb = 0;
    double v = waveletVideoKbps(frag_bytes, p.fragment_s, fps_guess, station_id, p.audio_codec, p.opus_kbps, &fb);
    const Preset& pr = width_khz > 20 ? pick(PRESETS_10FPS, v) : g_smooth ? pick(PRESETS_SMOOTH, v) : pick(PRESETS, v);
    p.width = pr.w;
    p.height = pr.h;
    p.fps = pr.fps;
    p.video_kbps = waveletVideoKbps(frag_bytes, p.fragment_s, p.fps, station_id, p.audio_codec, p.opus_kbps, &p.frame_bytes);
    // too little for a picture: audio only
    p.video = p.video_kbps >= VIDEO_MIN_KBPS;
    p.ok = true;
    return p;
}

LinkPlan planLinkFor(double link_bps, int frag_bytes, double width_khz, bool station_id, const QString& codec,
                     int width, int height, double fps, const QString& content) {
    LinkPlan p = planLink(link_bps, frag_bytes, width_khz, station_id);
    if (link_bps <= 0) return p;
    if (content == "audio") {
        // audio only: the whole link's -- Codec2 as ever; Opus up to 48 kbps, the
        // highest rate whose whole packets still fit the fragments (media_tx_gui)
        p.audio_codec = codec;
        p.video = false;
        p.video_kbps = 0;
        p.frame_bytes = 0;
        if (codec == "opus") {
            double best = AUDIO_MIN_KBPS;
            for (double c = AUDIO_MIN_KBPS; c <= std::min(p.usable_kbps, 48.0) + 1e-9; c += 0.1) {
                double pkt = c * 1000 * AUDIO_PACKET_INTERVAL_S / 8 + 1;
                int per = std::max(1, (int)((frag_bytes - 2 - AUDIO_PROFILE_CONFIG_BYTES) / pkt));
                if ((1 / AUDIO_PACKET_INTERVAL_S) / per <= 1 / p.fragment_s) best = c;
            }
            p.opus_kbps = std::floor(best * 10) / 10;
        }
        p.ok = true;
        return p;
    }
    if (content == "video") {
        // video only: no audio, its share to the picture
        p.audio = false;
        p.opus_kbps = 0;
        p.width = width;
        p.height = height;
        p.fps = fps;
        p.video_kbps = waveletVideoKbps(frag_bytes, p.fragment_s, fps, station_id, codec, 0, &p.frame_bytes, false);
        p.video = p.video_kbps >= VIDEO_MIN_KBPS;
        p.ok = true;
        return p;
    }
    if (codec != p.audio_codec) {
        p.audio_codec = codec;
        // Opus picked on a slow link: its floor (5 kbps), as the plan would give a link that fits it
        p.opus_kbps = codec == "opus" ? std::max(AUDIO_MIN_KBPS, std::min(p.usable_kbps * AUDIO_SHARE, AUDIO_MAX_KBPS)) : 0;
        p.opus_kbps = std::floor(p.opus_kbps * 10) / 10;
    }
    p.width = width;
    p.height = height;
    p.fps = fps;
    p.video_kbps = waveletVideoKbps(frag_bytes, p.fragment_s, fps, station_id, p.audio_codec, p.opus_kbps, &p.frame_bytes);
    p.video = p.video_kbps >= VIDEO_MIN_KBPS;
    p.ok = true;
    return p;
}

QString LinkPlan::summary() const {
    if (link_kbps <= 0) return "Link: no such mode";
    QString a = audio_codec == "opus" ? QString("Opus %1").arg(opus_kbps, 0, 'f', 1) : QString("Codec2 3.2");
    if (!video) return QString("Link %1 kbps: audio %2 only (too little room for video)").arg(link_kbps, 0, 'f', 1).arg(a);
    return QString("Link %1 kbps: audio %2 + video %3 kbps (%4x%5, %6 fps)")
        .arg(link_kbps, 0, 'f', 1).arg(a).arg(video_kbps, 0, 'f', 1).arg(width).arg(height).arg(fps);
}

}  // namespace avm
