// virtual_radio.h -- the virtual radio's transmit side (virtual_radio.py's
// VirtualTxSink): samples sent over UDP to 127.0.0.1, where the virtual
// receiver (hf_ofdm_rx.py --sdr virtual, c++/radio_iq.py) plays them out as
// a continuous stream. Paced to real time, at most TX_LEAD_S ahead, as a
// radio's buffer would be. Packets: [TX frequency f64][TX rate f64][seq u32]
// (big-endian) + up to 7000 complex64 samples.
#pragma once
#include <chrono>
#include <complex>
#include <cstdint>
#include <cstring>
#include <thread>
#include <vector>

#include "avm_log.h"

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace vradio {

const int PORT = 47731;
const size_t PACKET_SAMPLES = 7000;
const double TX_LEAD_S = 0.2;

class TxSink {
public:
    TxSink(double freq_hz, double rate) : freq_(freq_hz), fs_(rate) {
#if defined(_WIN32)
        WSADATA w;
        WSAStartup(MAKEWORD(2, 2), &w);
#endif
        sock_ = (long long)socket(AF_INET, SOCK_DGRAM, 0);
        std::memset(&to_, 0, sizeof to_);
        to_.sin_family = AF_INET;
        to_.sin_port = htons(PORT);
        to_.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        avm_logf("Virtual radio TX: %.4f MHz, %.0f S/s -> 127.0.0.1:%d\n", freq_ / 1e6, fs_, PORT);
    }
    ~TxSink() {
#if defined(_WIN32)
        closesocket((SOCKET)sock_);
        WSACleanup();
#else
        close((int)sock_);
#endif
    }
    void write(const std::complex<float>* x, size_t n) {
        double now = mono();
        if (t0_ < 0 || now - t0_ > sent_ / fs_) { t0_ = now; sent_ = 0; }   // first, or fell behind: afresh
        std::vector<char> pkt;
        for (size_t i = 0; i < n; i += PACKET_SAMPLES) {
            size_t k = std::min(PACKET_SAMPLES, n - i);
            double ahead = sent_ / fs_ - (mono() - t0_);
            if (ahead > TX_LEAD_S) std::this_thread::sleep_for(std::chrono::duration<double>(ahead - TX_LEAD_S));
            pkt.resize(20 + k * 8);
            put_f64(&pkt[0], freq_);
            put_f64(&pkt[8], fs_);
            for (int b = 0; b < 4; ++b) pkt[16 + b] = (char)(seq_ >> (24 - 8 * b));
            std::memcpy(&pkt[20], x + i, k * 8);
#if defined(_WIN32)
            sendto((SOCKET)sock_, pkt.data(), (int)pkt.size(), 0, (const sockaddr*)&to_, sizeof to_);
#else
            sendto((int)sock_, pkt.data(), pkt.size(), 0, (const sockaddr*)&to_, sizeof to_);
#endif
            ++seq_;
            sent_ += (double)k;
        }
    }

private:
    static double mono() {
        return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    static void put_f64(char* p, double v) {
        uint64_t u;
        std::memcpy(&u, &v, 8);
        for (int b = 0; b < 8; ++b) p[b] = (char)(u >> (56 - 8 * b));
    }
    double freq_, fs_;
    long long sock_;
    sockaddr_in to_;
    uint32_t seq_ = 0;
    double t0_ = -1, sent_ = 0;
};

}  // namespace vradio
