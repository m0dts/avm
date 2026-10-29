#include "recorder.h"

#include <chrono>
#include <cstdio>
#include <cstring>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libswscale/swscale.h>
}

namespace avm {

static const int AUDIO_RATE = 48000;
static const AVRational VIDEO_TB = {1, 1000};      // milliseconds: pictures timestamped by arrival

static long long now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

static QString err_str(int e) {
    char b[AV_ERROR_MAX_STRING_SIZE] = {0};
    av_strerror(e, b, sizeof b);
    return b;
}

double Recorder::seconds() const { return (now_ns() - t0_ns_) / 1e9; }

bool Recorder::start(const QString& path, int w, int h, QString* error) {
    stop();
    std::lock_guard<std::mutex> g(mu_);
    QByteArray p = path.toUtf8();
    int r = avformat_alloc_output_context2(&fmt_, nullptr, "mp4", p.constData());
    if (r < 0 || !fmt_) { *error = "MP4 output: " + err_str(r); fmt_ = nullptr; return false; }
    w_ = w & ~1;
    h_ = h & ~1;
    if (!openVideo(w_, h_, error) || !openAudio(error)) {
        avformat_free_context(fmt_);
        fmt_ = nullptr;
        return false;
    }
    if ((r = avio_open(&fmt_->pb, p.constData(), AVIO_FLAG_WRITE)) < 0) {
        *error = "could not create " + path + ": " + err_str(r);
        avformat_free_context(fmt_);
        fmt_ = nullptr;
        return false;
    }
    // fragmented: playable however the recording ends
    AVDictionary* opts = nullptr;
    av_dict_set(&opts, "movflags", "frag_keyframe+empty_moov+default_base_moof", 0);
    r = avformat_write_header(fmt_, &opts);
    av_dict_free(&opts);
    if (r < 0) {
        *error = "MP4 header: " + err_str(r);
        avio_closep(&fmt_->pb);
        avformat_free_context(fmt_);
        fmt_ = nullptr;
        return false;
    }
    pkt_ = av_packet_alloc();
    path_ = path;
    t0_ns_ = now_ns();
    last_vpts_ = -1;
    apts_ = 0;
    abuf_.clear();
    return true;
}

bool Recorder::openVideo(int w, int h, QString* error) {
    // H.264: what every player shows (MPEG-4 part 2 played as sound only in Windows' and phones' players)
    // (the first that opens: libx264 on Linux; Windows' own, Media Foundation, with the LGPL FFmpeg)
    const char* names[] = {"libx264", "libopenh264", "h264_mf", "h264_v4l2m2m", "mpeg4"};
    const AVCodec* c = nullptr;
    QString why;
    AVPixelFormat pix = AV_PIX_FMT_YUV420P;
    for (const char* name : names) {
        c = avcodec_find_encoder_by_name(name);
        if (!c) continue;
        venc_ = avcodec_alloc_context3(c);
        venc_->width = w;
        venc_->height = h;
        pix = AV_PIX_FMT_YUV420P;
        if (c->pix_fmts) {      // (Media Foundation: NV12)
            bool has420 = false;
            for (const AVPixelFormat* p = c->pix_fmts; *p != AV_PIX_FMT_NONE; ++p) has420 |= *p == AV_PIX_FMT_YUV420P;
            if (!has420) pix = c->pix_fmts[0];
        }
        venc_->pix_fmt = pix;
        venc_->time_base = VIDEO_TB;
        venc_->framerate = {25, 1};
        venc_->gop_size = 50;
        venc_->bit_rate = 1500000;
        AVDictionary* o = nullptr;
        if (!std::strcmp(name, "libx264")) {
            av_dict_set(&o, "preset", "veryfast", 0);
            av_dict_set(&o, "crf", "20", 0);       // constant quality: the picture is small
        } else if (!std::strcmp(name, "mpeg4")) {
            venc_->flags |= AV_CODEC_FLAG_QSCALE;
            venc_->global_quality = FF_QP2LAMBDA * 3;
        }
        if (fmt_->oformat->flags & AVFMT_GLOBALHEADER) venc_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
        int r = avcodec_open2(venc_, c, &o);
        av_dict_free(&o);
        if (r >= 0) break;
        why += QString(" %1: %2;").arg(name, err_str(r));
        avcodec_free_context(&venc_);
        c = nullptr;
    }
    if (!c) { *error = "no video encoder opened:" + why; return false; }
    std::fprintf(stderr, "record: video %s\n", c->name);
    vst_ = avformat_new_stream(fmt_, nullptr);
    vst_->time_base = VIDEO_TB;
    avcodec_parameters_from_context(vst_->codecpar, venc_);
    vf_ = av_frame_alloc();
    vf_->format = pix;
    vf_->width = w;
    vf_->height = h;
    av_frame_get_buffer(vf_, 0);
    sws_ = sws_getContext(w, h, AV_PIX_FMT_BGRA, w, h, pix, SWS_BILINEAR, nullptr, nullptr, nullptr);
    return true;
}

bool Recorder::openAudio(QString* error) {
    const AVCodec* c = avcodec_find_encoder(AV_CODEC_ID_AAC);
    if (!c) { *error = "no AAC encoder"; return false; }
    aenc_ = avcodec_alloc_context3(c);
    aenc_->sample_rate = AUDIO_RATE;
    aenc_->sample_fmt = AV_SAMPLE_FMT_FLTP;
    av_channel_layout_default(&aenc_->ch_layout, 1);
    aenc_->bit_rate = 48000;
    aenc_->time_base = {1, AUDIO_RATE};
    if (fmt_->oformat->flags & AVFMT_GLOBALHEADER) aenc_->flags |= AV_CODEC_FLAG_GLOBAL_HEADER;
    int r = avcodec_open2(aenc_, c, nullptr);
    if (r < 0) { *error = "AAC encoder: " + err_str(r); return false; }
    ast_ = avformat_new_stream(fmt_, nullptr);
    ast_->time_base = {1, AUDIO_RATE};
    avcodec_parameters_from_context(ast_->codecpar, aenc_);
    af_ = av_frame_alloc();
    af_->format = AV_SAMPLE_FMT_FLTP;
    af_->sample_rate = AUDIO_RATE;
    av_channel_layout_default(&af_->ch_layout, 1);
    af_->nb_samples = aenc_->frame_size;
    av_frame_get_buffer(af_, 0);
    return true;
}

// one frame (or the flush, f null) through an encoder into the file
void Recorder::write(AVCodecContext* enc, AVStream* st, AVFrame* f) {
    if (avcodec_send_frame(enc, f) < 0) return;
    while (avcodec_receive_packet(enc, pkt_) == 0) {
        av_packet_rescale_ts(pkt_, enc->time_base, st->time_base);
        pkt_->stream_index = st->index;
        av_interleaved_write_frame(fmt_, pkt_);
    }
}

void Recorder::picture(const QImage& img) {
    std::lock_guard<std::mutex> g(mu_);
    if (!fmt_ || img.isNull()) return;
    QImage im = img.convertToFormat(QImage::Format_ARGB32);
    if (im.width() != w_ || im.height() != h_) im = im.scaled(w_, h_, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
    long long pts = (long long)(seconds() * 1000);
    if (pts <= last_vpts_) pts = last_vpts_ + 1;     // strictly rising
    last_vpts_ = pts;
    av_frame_make_writable(vf_);
    const uint8_t* src[1] = {im.constBits()};
    int stride[1] = {(int)im.bytesPerLine()};
    sws_scale(sws_, src, stride, 0, h_, vf_->data, vf_->linesize);
    vf_->pts = pts;
    write(venc_, vst_, vf_);
}

void Recorder::sound(const float* pcm, size_t n) {
    std::lock_guard<std::mutex> g(mu_);
    if (!fmt_) return;
    // the sound's clock: its samples, started level with the pictures' (a late start is silence)
    if (apts_ == 0 && abuf_.empty()) {
        long long lead = (long long)(seconds() * AUDIO_RATE);
        abuf_.assign((size_t)std::max(0LL, lead), 0.0f);
    }
    abuf_.insert(abuf_.end(), pcm, pcm + n);
    const size_t fs = (size_t)aenc_->frame_size;
    size_t used = 0;
    while (abuf_.size() - used >= fs) {
        av_frame_make_writable(af_);
        std::copy(abuf_.begin() + used, abuf_.begin() + used + fs, (float*)af_->data[0]);
        af_->pts = apts_;
        apts_ += (long long)fs;
        write(aenc_, ast_, af_);
        used += fs;
    }
    abuf_.erase(abuf_.begin(), abuf_.begin() + used);
}

void Recorder::stop() {
    std::lock_guard<std::mutex> g(mu_);
    if (!fmt_) return;
    write(venc_, vst_, nullptr);
    write(aenc_, ast_, nullptr);
    av_write_trailer(fmt_);
    avio_closep(&fmt_->pb);
    avformat_free_context(fmt_);
    fmt_ = nullptr;
    avcodec_free_context(&venc_);
    avcodec_free_context(&aenc_);
    av_frame_free(&vf_);
    av_frame_free(&af_);
    av_packet_free(&pkt_);
    sws_freeContext(sws_);
    sws_ = nullptr;
    vst_ = ast_ = nullptr;
}

}  // namespace avm
