// recorder.h -- the received picture and sound saved as they play: an MP4
// (MPEG-4 Part 2 video + AAC audio, FFmpeg's own LGPL encoders) written
// fragmented, so a recording cut off by a crash or power loss still plays.
// Pictures come at the stream's own irregular pace, timestamped by arrival;
// the sound at 48 kHz mono, timestamped by its samples.
#pragma once
#include <QImage>
#include <QString>
#include <mutex>
#include <vector>

struct AVFormatContext;
struct AVCodecContext;
struct AVStream;
struct AVFrame;
struct AVPacket;
struct SwsContext;

namespace avm {

class Recorder {
public:
    ~Recorder() { stop(); }
    // path: the file to write; w x h: the picture size (fixed for the file)
    bool start(const QString& path, int w, int h, QString* error);
    void stop();
    bool recording() const { return fmt_ != nullptr; }
    QString path() const { return path_; }
    void picture(const QImage& img);              // a decoded picture
    void sound(const float* pcm, size_t n);       // 48 kHz mono, as played

private:
    bool openVideo(int w, int h, QString* error);
    bool openAudio(QString* error);
    void write(AVCodecContext* enc, AVStream* st, AVFrame* f);
    double seconds() const;

    std::mutex mu_;
    QString path_;
    AVFormatContext* fmt_ = nullptr;
    AVCodecContext *venc_ = nullptr, *aenc_ = nullptr;
    AVStream *vst_ = nullptr, *ast_ = nullptr;
    AVFrame *vf_ = nullptr, *af_ = nullptr;
    AVPacket* pkt_ = nullptr;
    SwsContext* sws_ = nullptr;
    int w_ = 0, h_ = 0;
    long long t0_ns_ = 0, last_vpts_ = -1, apts_ = 0;
    std::vector<float> abuf_;
};

}  // namespace avm
