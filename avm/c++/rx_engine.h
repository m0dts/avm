/* rx_engine.h -- AVM's receiver, in-process (libavm). The options are
 * avm_rx's own command line (argv without the program name: "--mode", "A",
 * "--sample-rate", "550000", ...); the engine runs on its own threads.
 *
 *   avm_rx_callbacks cb = {on_data, on_log, on_done, me};
 *   void* rx = avm_rx_create(argc, argv, &cb);    // NULL: bad options or no radio (logged)
 *   avm_rx_push(rx, iq, n);                        // complex64 IQ (not with --input pluto)
 *   avm_rx_set_gain(rx, 40);                       // a radio's, live
 *   avm_rx_destroy(rx);                            // stops and joins
 *
 * The callbacks come on the engine's threads: data, the received stream
 * (paced unless --no-output-pacing); log, one status line at a time, no
 * newline (avm_rx's stderr lines, "[spectrum] ..." with --spectrum); done,
 * once when the receive loop ends by itself (the input or the radio ended).
 * avm_rx_destroy must not be called from a callback. */
#pragma once
#include <stddef.h>

#if defined(_WIN32)
#define AVM_RX_API extern "C" __declspec(dllexport)
#else
#define AVM_RX_API extern "C" __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif
typedef struct {
    void (*data)(void* user, const unsigned char* p, size_t n);
    void (*log)(void* user, const char* line);
    void (*done)(void* user);
    void* user;
} avm_rx_callbacks;
#ifdef __cplusplus
}
#endif

AVM_RX_API void* avm_rx_create(int argc, const char* const* argv, const avm_rx_callbacks* cb);
AVM_RX_API void avm_rx_push(void* rx, const void* iq, size_t nbytes);
AVM_RX_API void avm_rx_end_input(void* rx);
AVM_RX_API void avm_rx_set_gain(void* rx, double db);
AVM_RX_API void avm_rx_set_freq_offset(void* rx, double hz);
AVM_RX_API int avm_rx_running(void* rx);
AVM_RX_API void avm_rx_destroy(void* rx);
AVM_RX_API double avm_link_bitrate(const char* mode, double occupancy, const char* modulation, const char* fec, int fragment_bytes, double gap_ms);
AVM_RX_API int avm_sdr_find(int rx, char* out, int cap);
