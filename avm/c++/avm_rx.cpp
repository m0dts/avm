// avm_rx.cpp -- AVM's receiver as a program: the library's engine
// (rx_engine.cpp) with stdin as its input and stdout as its output.
//
//   avm_rx --mode A --occupancy 20 --modulation qpsk --fec ldpc
//          --sample-rate 550000 --fragment-size 1024 < iq.c64 > payload.bin
//
// stdin: complex64 IQ at --sample-rate (default: the mode's own rate);
// stdout: the fragments' data, paced over each fragment's air time (as the
// Python's StdoutPacer; --no-output-pacing: at once); stderr: the same
// status lines as hf_ofdm_rx.py ("Fragment seq=..: OK  CFO=.. BER=.. ..").
//
// Radio input: --input pluto --sdr DRIVER ... (hf_ofdm_rx.py's radio options):
// SoapySDR directly (sdr.h), with live --gain-file / --freq-offset-file;
// the radios that aren't SoapySDR ones (virtual, kiwi, hpsdr):
// c++/radio_iq.py (the receiver's own radio code) piped in, with its
// --lo-offset-hz. Not here yet (the Python keeps them): duplex (--tdd-*),
// --verbose diagnostics, --dump-constellation.
//
// Testing: --test-step BYTES (all of stdin first, then fixed steps); see
// rx_engine.cpp.
//
// Build: python build.py (avm_rx[.exe], the engine compiled in).
#include "rx_engine.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#endif

static std::atomic<bool> g_done{false};

static void on_data(void*, const unsigned char* p, size_t n) {
    if (std::fwrite(p, 1, n, stdout) != n || std::fflush(stdout) != 0) {
        std::fprintf(stderr, "Output pipe closed -- downstream reader exited. Exiting.\n");
        std::fflush(stderr);
        std::_Exit(0);
    }
}

static void on_done(void*) { g_done = true; }

static void usage() {
    std::fprintf(stderr,
                 "avm_rx: AVM receiver (hf_ofdm_rx.py's decode loop in C++)\n"
                 "  --mode A|B|C|D|VU|S|SH  --occupancy KHZ  --modulation M  --fec auto|viterbi|ldpc\n"
                 "  --sample-rate HZ (input rate; must differ from the mode's)  --fragment-size N\n"
                 "  --fragment-gap-ms MS  --no-output-pacing  --invert-spectrum  --rx-queue-depth N  --drop-if-slow\n"
                 "  --lo-offset-hz HZ (the radio tuned this far from the signal: c++/radio_iq.py prints it)\n"
                 "  --input pluto --sdr DRIVER --rf-freq HZ --rx-gain DB [--rx-agc] --gain-file F --freq-offset-file F\n"
                 "  --spectrum-stderr  --test-step BYTES (repeatable input steps, for testing)\n");
}

int main(int argc, char** argv) {
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    bool radio = false;
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--help") || !std::strcmp(argv[i], "-h")) { usage(); return 0; }
        if (!std::strcmp(argv[i], "--input") && i + 1 < argc && std::strcmp(argv[i + 1], "pipe")) radio = true;
    }
    avm_rx_callbacks cb = {on_data, nullptr, on_done, nullptr};    // no log callback: stderr
    void* rx = avm_rx_create(argc - 1, argv + 1, &cb);
    if (!rx) { usage(); return 2; }
    if (!radio) {
        // stdin in, as it comes (a reader thread: the main thread watches for the end)
        std::thread([rx] {
            std::vector<char> buf(1 << 15);
            while (true) {
                size_t got = std::fread(buf.data(), 1, buf.size(), stdin);
                if (got) avm_rx_push(rx, buf.data(), got);
                if (got < buf.size() && (std::feof(stdin) || std::ferror(stdin))) break;
            }
            avm_rx_end_input(rx);
        }).detach();
    }
    while (!g_done) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    std::fflush(stdout);
    std::fflush(stderr);
    std::_Exit(0);      // the stdin thread may still be in a read: no teardown under it
}
