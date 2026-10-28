# Third-party code for the Windows build

Linux uses the system's libraries (apt: libopus-dev, libcodec2-dev, the
libav*-dev FFmpeg libraries). The Windows build (build.bat,
package_windows.ps1) needs these here, not kept in git:

- `ffmpeg/` -- BtbN's FFmpeg win64 **LGPL shared** build
  (https://github.com/BtbN/FFmpeg-Builds/releases, `ffmpeg-n*-win64-lgpl-shared-*.zip`),
  unpacked so that `ffmpeg/include`, `ffmpeg/lib` and `ffmpeg/bin` exist
- `opus/` -- the Opus source (https://github.com/xiph/opus), BSD
- `codec2/` -- the Codec2 source (https://github.com/drowe67/codec2), LGPL-2.1

`miniaudio.h` (public domain / MIT-0, https://miniaud.io) is kept in git: every build uses it.
