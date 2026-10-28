// link_plan.h -- what to send on a link (media_tx_gui.py's
// _recalculate_bitrates / _wavelet_video_kbps and touch_widgets.video_preset):
// from the link's payload rate (libavm's avm_link_bitrate), the audio codec
// and rate, the picture size and frame rate, and the wavelet video's exact
// bytes a frame, so that one fragment per framer tick carries it all.
#pragma once
#include <QString>

namespace avm {

struct LinkPlan {
    bool ok = false;
    double link_kbps = 0;        // the payload rate
    double usable_kbps = 0;      // after the framing's overhead
    double fragment_s = 0;       // one fragment's air time (the framer's tick)
    QString audio_codec;         // "codec2" / "opus"
    double opus_kbps = 0;
    int width = 0, height = 0;
    double fps = 0;
    double video_kbps = 0;
    int frame_bytes = 0;
    bool video = true;           // false: too little room (under 2 kbps), or audio only chosen
    bool audio = true;           // false: video only chosen
    QString summary() const;
};

// link_bps: avm_link_bitrate's; width_khz: the occupancy (picks the 10 fps
// presets over 20 kHz); station_id: whether one is sent
LinkPlan planLink(double link_bps, int fragment_bytes, double width_khz, bool station_id);
// the picture size / frame rate choice on links up to 20 kHz: smooth (higher
// frame rate, smaller picture) or sharp (the measured best quality, the default)
void setVideoSmooth(bool on);
bool videoSmooth();
// the same link with the picture size, frame rate and audio codec chosen
// (the TX page's pickers): the Opus rate and the video's exact frame size for them
// content (Config's Content, touch_tx_page.set_content): "auto", "audio" (audio
// only: Opus up to 48 kbps, as packing allows) or "video" (video only: all of it)
LinkPlan planLinkFor(double link_bps, int fragment_bytes, double width_khz, bool station_id, const QString& codec,
                     int width, int height, double fps, const QString& content = "auto");

}  // namespace avm
