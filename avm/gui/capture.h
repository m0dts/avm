// capture.h -- a camera, microphone or test source through the FFmpeg
// libraries (libavdevice / libavformat / libavcodec / libavfilter): the
// input opened, decoded, and run through a filter chain -- the same one the
// Python sources give the ffmpeg program (-vf / -af) -- on its own thread,
// each filtered frame handed to a callback.
#pragma once
#include <atomic>
#include <functional>
#include <map>
#include <string>
#include <thread>
#include <vector>

struct AVFrame;

namespace avm {

struct CaptureInput {
    std::string format;                         // "dshow", "v4l2", "pulse", "alsa", "lavfi"
    std::string url;                            // "video=Integrated Camera", "/dev/video0", "testsrc=..."
    std::map<std::string, std::string> options; // the input's options (-audio_buffer_size 20, ...)
    bool video = true;                          // the stream taken: video, or audio
    std::string filter;                         // the filter chain, e.g. "scale=..,fps=6,format=yuv420p"
    bool realtime = false;                      // -re: paced to its timestamps (the test sources)
    // a network stream (SRT / UDP from OBS): its audio too, through audio_filter to
    // start()'s audio callback; and when it ends or breaks, opened again (waiting for
    // the sender) until stopped
    bool with_audio = false;
    std::string audio_filter;
    bool reconnect = false;
};

class Capture {
public:
    using FrameFn = std::function<void(AVFrame*)>;
    using LogFn = std::function<void(const std::string&)>;
    ~Capture() { stop(); }
    // opens on the capture thread; failures come as log lines, then the thread ends
    void start(const CaptureInput& in, FrameFn frame, LogFn log, FrameFn audio = nullptr);
    void stop();
    bool running() const { return running_; }
    // the devices FFmpeg's input lists (dshow / v4l2 / pulse / alsa): their names
    static std::vector<std::string> devices(const std::string& format, bool video);

private:
    std::thread th_;
    std::atomic<bool> stop_{false}, running_{false};
    void run(CaptureInput in, FrameFn frame, LogFn log, FrameFn audio);
    bool session(const CaptureInput& in, const FrameFn& frame, const LogFn& log, const FrameFn& audio, bool quiet);
};

}  // namespace avm
