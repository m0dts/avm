// miniaudio.cpp -- miniaudio's implementation (third_party/miniaudio.h, public
// domain / MIT-0), on its own: compiling it takes most of a minute on a Pi,
// and it never changes, so a change to audio.cpp doesn't rebuild it.
// The MA_* options here must match audio.cpp's.
#define MINIAUDIO_IMPLEMENTATION
#define MA_NO_ENCODING
#define MA_NO_DECODING
#define MA_NO_GENERATION
#include "miniaudio.h"
