#include "capture.h"

#include <chrono>
#include <vector>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavdevice/avdevice.h>
#include <libavfilter/avfilter.h>
#include <libavfilter/buffersink.h>
#include <libavfilter/buffersrc.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/opt.h>
#include <libavutil/time.h>
}

namespace avm {

static std::string av_err(int e) {
    char b[AV_ERROR_MAX_STRING_SIZE] = {0};
    av_strerror(e, b, sizeof b);
    return b;
}

static void register_devices() {
    static bool done = false;
    if (!done) {
        avdevice_register_all();
        av_log_set_level(AV_LOG_ERROR);   // not its per-frame notes on stderr
        done = true;
    }
}

void Capture::start(const CaptureInput& in, FrameFn frame, LogFn log, FrameFn audio) {
    stop();
    stop_ = false;
    running_ = true;
    th_ = std::thread([this, in, frame, log, audio] {
        run(in, frame, log, audio);
        running_ = false;
    });
}

void Capture::stop() {
    stop_ = true;
    if (th_.joinable()) th_.join();
}

namespace {
// one stream taken from the input: its decoder and filter chain
struct Decode {
    int si = -1;
    AVStream* st = nullptr;
    bool video = true, keyed = false;
    std::string chain;
    AVCodecContext* dec = nullptr;
    AVFilterGraph* graph = nullptr;
    AVFilterContext *src = nullptr, *sink = nullptr;
    AVFrame* raw = av_frame_alloc();
    AVFrame* out = av_frame_alloc();
    ~Decode() {
        avfilter_graph_free(&graph);
        avcodec_free_context(&dec);
        av_frame_free(&raw);
        av_frame_free(&out);
    }
};
// what the capture thread owns, freed whichever way it ends
struct Pipeline {
    AVFormatContext* fmt = nullptr;
    AVPacket* pkt = av_packet_alloc();
    Decode main, extra;     // the stream asked for; a network stream's audio
    ~Pipeline() {
        avformat_close_input(&fmt);
        av_packet_free(&pkt);
    }
};
}  // namespace

static int interrupt_cb(void* p) { return ((std::atomic<bool>*)p)->load() ? 1 : 0; }

// the filter chain, built on the first decoded frame (its size and format known then)
static bool build_graph(Decode& D, const AVFrame* f, std::string& err) {
    D.graph = avfilter_graph_alloc();
    char args[512];
    AVRational tb = D.st->time_base;
    if (D.video) {
        AVRational sar = f->sample_aspect_ratio.num ? f->sample_aspect_ratio : AVRational{1, 1};
        snprintf(args, sizeof args, "video_size=%dx%d:pix_fmt=%d:time_base=%d/%d:pixel_aspect=%d/%d", f->width,
                 f->height, f->format, tb.num, tb.den, sar.num, sar.den);
    } else {
        char layout[128] = "mono";
        if (f->ch_layout.nb_channels) av_channel_layout_describe(&f->ch_layout, layout, sizeof layout);
        snprintf(args, sizeof args, "time_base=%d/%d:sample_rate=%d:sample_fmt=%s:channel_layout=%s", tb.num, tb.den,
                 f->sample_rate, av_get_sample_fmt_name((AVSampleFormat)f->format), layout);
    }
    int r = avfilter_graph_create_filter(&D.src, avfilter_get_by_name(D.video ? "buffer" : "abuffer"), "in", args,
                                         nullptr, D.graph);
    if (r < 0) { err = "filter input: " + av_err(r); return false; }
    r = avfilter_graph_create_filter(&D.sink, avfilter_get_by_name(D.video ? "buffersink" : "abuffersink"), "out",
                                     nullptr, nullptr, D.graph);
    if (r < 0) { err = "filter output: " + av_err(r); return false; }
    AVFilterInOut* outputs = avfilter_inout_alloc();
    AVFilterInOut* inputs = avfilter_inout_alloc();
    outputs->name = av_strdup("in");
    outputs->filter_ctx = D.src;
    inputs->name = av_strdup("out");
    inputs->filter_ctx = D.sink;
    r = avfilter_graph_parse_ptr(D.graph, D.chain.empty() ? (D.video ? "null" : "anull") : D.chain.c_str(), &inputs,
                                 &outputs, nullptr);
    avfilter_inout_free(&inputs);
    avfilter_inout_free(&outputs);
    if (r < 0) { err = "filter \"" + D.chain + "\": " + av_err(r); return false; }
    r = avfilter_graph_config(D.graph, nullptr);
    if (r < 0) { err = "filter setup: " + av_err(r); return false; }
    return true;
}

// light: a network stream's picture, shrunk to the mode's size anyway -- H.264's
// deblocking skipped (unseen once scaled down; about a third of the decode) and two
// threads at most, leaving the cores to the modem
static bool open_decoder(Decode& D, AVFormatContext* fmt, std::string& err, bool light = false) {
    D.st = fmt->streams[D.si];
    const AVCodec* codec = avcodec_find_decoder(D.st->codecpar->codec_id);
    if (!codec) { err = "no decoder"; return false; }
    D.dec = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(D.dec, D.st->codecpar);
    D.dec->pkt_timebase = D.st->time_base;
    if (light && D.video) {
        D.dec->skip_loop_filter = AVDISCARD_ALL;
        D.dec->thread_count = 2;
    }
    int r = avcodec_open2(D.dec, codec, nullptr);
    if (r < 0) { err = "decoder: " + av_err(r); return false; }
    return true;
}

void Capture::run(CaptureInput in, FrameFn frame, LogFn log, FrameFn audio) {
    register_devices();
    if (!in.reconnect) { session(in, frame, log, audio, false); return; }
    // a stream: opened again whenever it ends (the sender stopped, the link broke) --
    // a listener waits for the sender to call again
    bool quiet = false;
    while (!stop_) {
        bool got = session(in, frame, log, audio, quiet);
        if (stop_) break;
        if (got) log("Stream ended -- waiting for it again");
        quiet = !got;      // (the same failure, once a second: logged once)
        for (int i = 0; i < (got ? 2 : 10) && !stop_; ++i) av_usleep(100000);   // (back at once after a stream)
    }
}

// one opening of the input, read until it ends; true if it gave any frames
bool Capture::session(const CaptureInput& in, const FrameFn& frame, const LogFn& log, const FrameFn& audio, bool quiet) {
    Pipeline P;
    const AVInputFormat* ifmt = in.format.empty() ? nullptr : av_find_input_format(in.format.c_str());
    if (!in.format.empty() && !ifmt) { log("ERROR: FFmpeg has no \"" + in.format + "\" input"); return false; }
    AVDictionary* opts = nullptr;
    for (const auto& kv : in.options) av_dict_set(&opts, kv.first.c_str(), kv.second.c_str(), 0);
    av_dict_set(&opts, "fflags", "nobuffer", AV_DICT_DONT_OVERWRITE);   // low delay, as the Python sources' ffmpeg
    av_dict_set(&opts, "probesize", "32", AV_DICT_DONT_OVERWRITE);
    P.fmt = avformat_alloc_context();
    P.fmt->interrupt_callback.callback = interrupt_cb;
    P.fmt->interrupt_callback.opaque = &stop_;
    int r = avformat_open_input(&P.fmt, in.url.c_str(), ifmt, &opts);
    av_dict_free(&opts);
    std::string name = in.format.empty() ? in.url : in.format + " \"" + in.url + "\"";
    if (r < 0) {
        P.fmt = nullptr;
        if (!stop_ && !quiet) log(std::string(in.reconnect ? "Stream: " : "ERROR: ") + "could not open " + name + ": " + av_err(r));
        return false;
    }
    avformat_find_stream_info(P.fmt, nullptr);
    std::string err;
    P.main.video = in.video;
    P.main.chain = in.filter;
    P.main.si = av_find_best_stream(P.fmt, in.video ? AVMEDIA_TYPE_VIDEO : AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    if (P.main.si < 0) { log(std::string("ERROR: no ") + (in.video ? "video" : "audio") + " in " + name); return false; }
    if (!open_decoder(P.main, P.fmt, err, in.reconnect)) { log("ERROR: " + name + ": " + err); return false; }
    if (in.with_audio && audio && in.video) {
        P.extra.video = false;
        P.extra.chain = in.audio_filter;
        P.extra.si = av_find_best_stream(P.fmt, AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
        if (P.extra.si < 0) log("Stream: no audio in " + name);
        else if (!open_decoder(P.extra, P.fmt, err)) { log("Stream audio: " + err); P.extra.si = -1; }
    }
    log(std::string("Capturing ") + (in.video ? "video" : "audio") + (P.extra.si >= 0 ? " and audio" : "") + " from " + name);

    bool got = false;
    double t0_wall = -1, t0_pts = 0;
    while (!stop_) {
        r = av_read_frame(P.fmt, P.pkt);
        if (r == AVERROR(EAGAIN)) { av_usleep(5000); continue; }
        if (r < 0) {
            if (!stop_ && r != AVERROR_EOF) log((in.reconnect ? "Stream ended: " : "Capture ended: ") + av_err(r));
            break;
        }
        Decode* D = P.pkt->stream_index == P.main.si ? &P.main : P.pkt->stream_index == P.extra.si ? &P.extra : nullptr;
        if (!D) { av_packet_unref(P.pkt); continue; }
        const FrameFn& out = D == &P.main ? frame : audio;
        // a stream's picture from its first keyframe (joined mid-stream: before it, only smears;
        // a test pattern's or camera's raw frames aren't flagged as keyframes)
        if (in.reconnect && D->video && !D->keyed) {
            if (!(P.pkt->flags & AV_PKT_FLAG_KEY)) { av_packet_unref(P.pkt); continue; }
            D->keyed = true;
        }
        r = avcodec_send_packet(D->dec, P.pkt);
        av_packet_unref(P.pkt);
        if (r < 0 && r != AVERROR(EAGAIN)) continue;
        while (!stop_ && avcodec_receive_frame(D->dec, D->raw) == 0) {
            if (!D->graph) {
                if (!build_graph(*D, D->raw, err)) { log("ERROR: " + err); return got; }
            }
            // -re: no faster than its own timestamps
            if (in.realtime && D == &P.main && D->raw->pts != AV_NOPTS_VALUE) {
                double t = D->raw->pts * av_q2d(D->st->time_base);
                double now = av_gettime_relative() / 1e6;
                if (t0_wall < 0) { t0_wall = now; t0_pts = t; }
                double wait = (t - t0_pts) - (now - t0_wall);
                if (wait > 0) av_usleep((unsigned)(wait * 1e6));
            }
            if (av_buffersrc_add_frame_flags(D->src, D->raw, AV_BUFFERSRC_FLAG_KEEP_REF) < 0) { av_frame_unref(D->raw); continue; }
            av_frame_unref(D->raw);
            while (av_buffersink_get_frame(D->sink, D->out) >= 0) {
                got = true;
                out(D->out);
                av_frame_unref(D->out);
            }
        }
    }
    return got;
}

std::vector<std::string> Capture::devices(const std::string& format, bool video) {
    register_devices();
    std::vector<std::string> names;
    const AVInputFormat* ifmt = av_find_input_format(format.c_str());
    if (!ifmt) return names;
    AVDeviceInfoList* list = nullptr;
    if (avdevice_list_input_sources(ifmt, nullptr, nullptr, &list) >= 0 && list) {
        for (int i = 0; i < list->nb_devices; ++i) {
            AVDeviceInfo* d = list->devices[i];
            bool has_video = false, has_audio = false;
            for (int k = 0; k < d->nb_media_types; ++k) {
                if (d->media_types[k] == AVMEDIA_TYPE_VIDEO) has_video = true;
                if (d->media_types[k] == AVMEDIA_TYPE_AUDIO) has_audio = true;
            }
            if (d->nb_media_types && (video ? !has_video : !has_audio)) continue;
            // dshow: the friendly name (what "video=NAME" takes); others: the device itself
            names.push_back(format == "dshow" && d->device_description ? d->device_description : d->device_name);
        }
        avdevice_free_list_devices(&list);
    }
    return names;
}

}  // namespace avm
