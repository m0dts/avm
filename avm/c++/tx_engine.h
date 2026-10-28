/* tx_engine.h -- AVM's transmitter, in-process (libavm). The options are
 * avm_tx's own command line (argv without the program name: "--stream",
 * "--fragment-size", "1024", ...); the engine runs on its own thread.
 *
 *   avm_tx_callbacks cb = {on_iq, on_log, on_done, me};
 *   void* tx = avm_tx_create(argc, argv, &cb);    // NULL: bad options or no radio (logged)
 *   avm_tx_push(tx, data, n);                      // the framed stream
 *   avm_tx_set_gain(tx, -10);                      // a radio's, live
 *   avm_tx_destroy(tx);                            // stops and joins
 *
 * The callbacks come on the engine's thread: data, complex64 IQ (unless a
 * radio is open: --output pluto); log, one status line, no newline; done,
 * once when the stream ends by itself (avm_tx_end_input). avm_tx_destroy
 * must not be called from a callback. */
#pragma once
#include <stddef.h>

#if defined(_WIN32)
#define AVM_TX_API extern "C" __declspec(dllexport)
#else
#define AVM_TX_API extern "C" __attribute__((visibility("default")))
#endif

#ifdef __cplusplus
extern "C" {
#endif
typedef struct {
    void (*data)(void* user, const unsigned char* p, size_t n);
    void (*log)(void* user, const char* line);
    void (*done)(void* user);
    void* user;
} avm_tx_callbacks;
#ifdef __cplusplus
}
#endif

AVM_TX_API void* avm_tx_create(int argc, const char* const* argv, const avm_tx_callbacks* cb);
AVM_TX_API void avm_tx_push(void* tx, const void* data, size_t n);
AVM_TX_API void avm_tx_end_input(void* tx);
AVM_TX_API void avm_tx_set_gain(void* tx, double db);
AVM_TX_API int avm_tx_running(void* tx);
AVM_TX_API void avm_tx_destroy(void* tx);
