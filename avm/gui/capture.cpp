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

void Capture::start(const CaptureInput& in, FrameFn frame, LogFn log) {
    stop();
    stop_ = false;
    running_ = true;
    th_ = std::thread([this, in, frame, log] {
        run(in, frame, log);
        running_ = false;
    });
}

void Capture::stop() {
    stop_ = true;
    if (th_.joinable()) th_.join();
}

namespace {
// what the capture thread owns, freed whichever way it ends
struct Pipeline {
    AVFormatContext* fmt = nullptr;
    AVCodecContext* dec = nullptr;
    AVFilterGraph* graph = nullptr;
    AVFilterContext *src = nullptr, *sink = nullptr;
    AVPacket* pkt = av_packet_alloc();
    AVFrame* raw = av_frame_alloc();
    AVFrame* out = av_frame_alloc();
    ~Pipeline() {
        avfilter_graph_free(&graph);
        avcodec_free_context(&dec);
        avformat_close_input(&fmt);
        av_packet_free(&pkt);
        av_frame_free(&raw);
        av_frame_free(&out);
    }
};
}  // namespace

static int interrupt_cb(void* p) { return ((std::atomic<bool>*)p)->load() ? 1 : 0; }

// the filter chain, built on the first decoded frame (its size and format known then)
static bool build_graph(Pipeline& P, const AVStream* st, const AVFrame* f, bool video, const std::string& chain,
                        std::string& err) {
    P.graph = avfilter_graph_alloc();
    char args[512];
    AVRational tb = st->time_base;
    if (video) {
        AVRational sar = f->sample_aspect_ratio.num ? f->sample_aspect_ratio : AVRational{1, 1};
        snprintf(args, sizeof args, "video_size=%dx%d:pix_fmt=%d:time_base=%d/%d:pixel_aspect=%d/%d", f->width,
                 f->height, f->format, tb.num, tb.den, sar.num, sar.den);
    } else {
        char layout[128] = "mono";
        if (f->ch_layout.nb_channels) av_channel_layout_describe(&f->ch_layout, layout, sizeof layout);
        snprintf(args, sizeof args, "time_base=%d/%d:sample_rate=%d:sample_fmt=%s:channel_layout=%s", tb.num, tb.den,
                 f->sample_rate, av_get_sample_fmt_name((AVSampleFormat)f->format), layout);
    }
    int r = avfilter_graph_create_filter(&P.src, avfilter_get_by_name(video ? "buffer" : "abuffer"), "in", args,
                                         nullptr, P.graph);
    if (r < 0) { err = "filter input: " + av_err(r); return false; }
    r = avfilter_graph_create_filter(&P.sink, avfilter_get_by_name(video ? "buffersink" : "abuffersink"), "out",
                                     nullptr, nullptr, P.graph);
    if (r < 0) { err = "filter output: " + av_err(r); return false; }
    AVFilterInOut* outputs = avfilter_inout_alloc();
    AVFilterInOut* inputs = avfilter_inout_alloc();
    outputs->name = av_strdup("in");
    outputs->filter_ctx = P.src;
    inputs->name = av_strdup("out");
    inputs->filter_ctx = P.sink;
    r = avfilter_graph_parse_ptr(P.graph, chain.empty() ? (video ? "null" : "anull") : chain.c_str(), &inputs,
                                 &outputs, nullptr);
    avfilter_inout_free(&inputs);
    avfilter_inout_free(&outputs);
    if (r < 0) { err = "filter \"" + chain + "\": " + av_err(r); return false; }
    r = avfilter_graph_config(P.graph, nullptr);
    if (r < 0) { err = "filter setup: " + av_err(r); return false; }
    return true;
}

void Capture::run(CaptureInput in, FrameFn frame, LogFn log) {
    register_devices();
    Pipeline P;
    const AVInputFormat* ifmt = in.format.empty() ? nullptr : av_find_input_format(in.format.c_str());
    if (!in.format.empty() && !ifmt) { log("ERROR: FFmpeg has no \"" + in.format + "\" input"); return; }
    AVDictionary* opts = nullptr;
    for (const auto& kv : in.options) av_dict_set(&opts, kv.first.c_str(), kv.second.c_str(), 0);
    av_dict_set(&opts, "fflags", "nobuffer", 0);       // low delay, as the Python sources' ffmpeg
    av_dict_set(&opts, "probesize", "32", 0);
    P.fmt = avformat_alloc_context();
    P.fmt->interrupt_callback.callback = interrupt_cb;
    P.fmt->interrupt_callback.opaque = &stop_;
    int r = avformat_open_input(&P.fmt, in.url.c_str(), ifmt, &opts);
    av_dict_free(&opts);
    if (r < 0) {
        P.fmt = nullptr;
        if (!stop_) log("ERROR: could not open " + in.format + " \"" + in.url + "\": " + av_err(r));
        return;
    }
    avformat_find_stream_info(P.fmt, nullptr);
    int si = av_find_best_stream(P.fmt, in.video ? AVMEDIA_TYPE_VIDEO : AVMEDIA_TYPE_AUDIO, -1, -1, nullptr, 0);
    if (si < 0) { log(std::string("ERROR: no ") + (in.video ? "video" : "audio") + " in \"" + in.url + "\""); return; }
    AVStream* st = P.fmt->streams[si];
    const AVCodec* codec = avcodec_find_decoder(st->codecpar->codec_id);
    if (!codec) { log("ERROR: no decoder for \"" + in.url + "\""); return; }
    P.dec = avcodec_alloc_context3(codec);
    avcodec_parameters_to_context(P.dec, st->codecpar);
    P.dec->pkt_timebase = st->time_base;
    if ((r = avcodec_open2(P.dec, codec, nullptr)) < 0) { log("ERROR: decoder: " + av_err(r)); return; }
    log(std::string("Capturing ") + (in.video ? "video" : "audio") + " from " + in.format + " \"" + in.url + "\"");

    double t0_wall = -1, t0_pts = 0;
    while (!stop_) {
        r = av_read_frame(P.fmt, P.pkt);
        if (r == AVERROR(EAGAIN)) { av_usleep(5000); continue; }
        if (r < 0) {
            if (!stop_ && r != AVERROR_EOF) log("Capture ended: " + av_err(r));
            break;
        }
        if (P.pkt->stream_index != si) { av_packet_unref(P.pkt); continue; }
        r = avcodec_send_packet(P.dec, P.pkt);
        av_packet_unref(P.pkt);
        if (r < 0 && r != AVERROR(EAGAIN)) continue;
        while (!stop_ && avcodec_receive_frame(P.dec, P.raw) == 0) {
            if (!P.graph) {
                std::string err;
                if (!build_graph(P, st, P.raw, in.video, in.filter, err)) { log("ERROR: " + err); return; }
            }
            // -re: no faster than its own timestamps
            if (in.realtime && P.raw->pts != AV_NOPTS_VALUE) {
                double t = P.raw->pts * av_q2d(st->time_base);
                double now = av_gettime_relative() / 1e6;
                if (t0_wall < 0) { t0_wall = now; t0_pts = t; }
                double wait = (t - t0_pts) - (now - t0_wall);
                if (wait > 0) av_usleep((unsigned)(wait * 1e6));
            }
            if (av_buffersrc_add_frame_flags(P.src, P.raw, AV_BUFFERSRC_FLAG_KEEP_REF) < 0) { av_frame_unref(P.raw); continue; }
            av_frame_unref(P.raw);
            while (av_buffersink_get_frame(P.sink, P.out) >= 0) {
                frame(P.out);
                av_frame_unref(P.out);
            }
        }
    }
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
