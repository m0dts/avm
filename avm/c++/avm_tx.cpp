// avm_tx.cpp -- AVM's transmitter as a program: the library's engine
// (tx_engine.cpp) with stdin as its input and stdout as its output.
//
//   avm_tx --mode A --occupancy 20 --modulation qpsk --fec ldpc --stream
//          --fragment-size 1024 --sample-rate 550000 < data > iq.c64
//
// stdin: the data (each --fragment-size bytes, after an 8-byte fragment
// header, is one frame); stdout: complex64 IQ at --sample-rate (default:
// the mode's own rate); stderr: the Python's status lines ("Streamed
// fragment seq=.. (.. bytes, .. ms)"). Same waveform as hf_ofdm_tx.py to
// float rounding (c++/test_avm_tx.py).
//
// Radio output: --output pluto --sdr pluto|lime --rf-freq --tx-gain
// --pluto-uri --pluto-bandwidth --sdr-antenna --tx-queue-depth --lo-offset-hz
// --gain-file (hf_ofdm_tx.py's options): SoapySDR directly (sdr.h). See
// tx_engine.cpp for the rest.
//
// Build: python build.py (avm_tx[.exe], the engine compiled in).
#include "tx_engine.h"

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <thread>
#include <vector>

#if defined(_WIN32)
#include <fcntl.h>
#include <io.h>
#endif

static std::atomic<bool> g_done{false};

static void on_data(void*, const unsigned char* p, size_t n) {
    for (size_t i = 0; i < n; i += 1 << 16) {
        size_t k = n - i < ((size_t)1 << 16) ? n - i : ((size_t)1 << 16);
        if (std::fwrite(p + i, 1, k, stdout) != k) {
            std::fprintf(stderr, "Output pipe closed. Exiting.\n");
            std::fflush(stderr);
            std::_Exit(0);
        }
    }
    std::fflush(stdout);
}

static void on_done(void*) { g_done = true; }

static void usage() {
    std::fprintf(stderr,
                 "avm_tx: AVM transmitter (hf_ofdm_tx.py --stream in C++)\n"
                 "  --mode A|B|C|D|VU|S|SH  --occupancy KHZ  --modulation M  --fec viterbi|ldpc  --stream\n"
                 "  --fragment-size N  --fragment-gap-ms MS (default 300)  --sample-rate HZ (output rate)\n"
                 "  --amplitude A  --papr-db DB  --tx-warmup-s S (default 2)  --max-input-backlog N\n"
                 "  --tx-normalize per-fragment|fixed  --output pipe|pluto --sdr pluto|lime --rf-freq HZ\n"
                 "  --tx-gain DB --gain-file F --lo-offset-hz HZ\n");
}

int main(int argc, char** argv) {
#if defined(_WIN32)
    _setmode(_fileno(stdin), _O_BINARY);
    _setmode(_fileno(stdout), _O_BINARY);
#endif
    for (int i = 1; i < argc; ++i)
        if (!std::strcmp(argv[i], "--help") || !std::strcmp(argv[i], "-h")) { usage(); return 0; }
    avm_tx_callbacks cb = {on_data, nullptr, on_done, nullptr};    // no log callback: stderr
    void* tx = avm_tx_create(argc - 1, argv + 1, &cb);
    if (!tx) { usage(); return 2; }
    std::thread([tx] {
        std::vector<char> buf(1 << 15);
        while (true) {
            size_t got = std::fread(buf.data(), 1, buf.size(), stdin);
            if (got) avm_tx_push(tx, buf.data(), got);
            if (got < buf.size() && (std::feof(stdin) || std::ferror(stdin))) break;
        }
        avm_tx_end_input(tx);
    }).detach();
    while (!g_done) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    std::fflush(stdout);
    std::fflush(stderr);
    std::_Exit(0);      // the stdin thread may still be in a read: no teardown under it
}
