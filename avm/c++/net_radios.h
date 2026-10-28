// net_radios.h -- the two receive "radios" that aren't SoapySDR devices, in
// C++ so the receiver needs no Python:
//   VirtualRxSource -- virtual_radio.py's: the virtual transmitter's UDP
//     packets (virtual_radio.h) played out as a continuous real-time stream,
//     with a noise floor 60 dB down;
//   KiwiRxSource -- kiwi_source.py's: a KiwiSDR's IQ over its WebSocket
//     (its own rate, ~12 kS/s; manual gain; lost packets filled with silence).
// Both are sdr::RadioIn, as hpsdr1::RxSource.
#pragma once
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "avm_log.h"
#include "sdr.h"
#include "virtual_radio.h"

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#endif

namespace netradio {

using cf = std::complex<float>;
const double TWO_PI = 6.283185307179586;

inline double mono() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

#if defined(_WIN32)
using sock_t = SOCKET;
const sock_t BAD_SOCK = INVALID_SOCKET;
inline void sock_close(sock_t s) { closesocket(s); }
struct WsaInit {
    WsaInit() { WSADATA w; WSAStartup(MAKEWORD(2, 2), &w); }
    ~WsaInit() { WSACleanup(); }
};
#else
using sock_t = int;
const sock_t BAD_SOCK = -1;
inline void sock_close(sock_t s) { ::close(s); }
struct WsaInit {};
#endif

// a receive timeout on a socket (0: none)
inline void sock_timeout(sock_t s, double seconds) {
#if defined(_WIN32)
    DWORD ms = (DWORD)(seconds * 1000);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char*)&ms, sizeof ms);
#else
    timeval tv;
    tv.tv_sec = (long)seconds;
    tv.tv_usec = (long)((seconds - (long)seconds) * 1e6);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char*)&tv, sizeof tv);
#endif
}

// samples handed from a radio's threads to read(): the chunks, waited on
class SampleQueue {
public:
    void push(std::vector<cf>&& v) {
        {
            std::lock_guard<std::mutex> g(mu_);
            q_.push_back(std::move(v));
        }
        cv_.notify_all();
    }
    void end() {
        {
            std::lock_guard<std::mutex> g(mu_);
            ended_ = true;
        }
        cv_.notify_all();
    }
    // the next chunk; 0 on a timeout, -1 once ended (and drained)
    long take(std::vector<cf>& out, double timeout_s) {
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait_for(lk, std::chrono::duration<double>(timeout_s), [this] { return !q_.empty() || ended_; });
        if (q_.empty()) return ended_ ? -1 : 0;
        out = std::move(q_.front());
        q_.pop_front();
        return (long)out.size();
    }
private:
    std::mutex mu_;
    std::condition_variable cv_;
    std::deque<std::vector<cf>> q_;
    bool ended_ = false;
};

// ---------------------------------------------------------------- virtual radio
class VirtualRxSource : public sdr::RadioIn {
public:
    static constexpr double JITTER_S = 0.15;   // a burst held this long before it plays out
    static constexpr double BLOCK_S = 0.02;    // the stream made in blocks this long
    static constexpr double NOISE_DB = -60.0;  // the noise floor, relative to the signal's power

    // place_hz: where the TX signal sits in this stream (-LO offset: where a
    // Pluto tuned that far above would put it)
    bool open(double freq_hz, double rate_hz, double place_hz, const std::string& freq_offset_file) {
        freq_ = freq_hz;
        rate = rate_hz;
        place_ = place_hz;
        sock_ = socket(AF_INET, SOCK_DGRAM, 0);
        int big = 16 << 20;      // a burst's start arrives at once: a small buffer dropped most of it
        setsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (const char*)&big, sizeof big);
        sockaddr_in a;
        std::memset(&a, 0, sizeof a);
        a.sin_family = AF_INET;
        a.sin_port = htons(vradio::PORT);
        a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        if (sock_ == BAD_SOCK || bind(sock_, (const sockaddr*)&a, sizeof a) != 0) {
            error = "the virtual radio's receiver is already in use on this machine (another AVM receiving "
                    "from it?) -- stop that one first (UDP port " + std::to_string(vradio::PORT) + ")";
            if (sock_ != BAD_SOCK) sock_close(sock_);
            sock_ = BAD_SOCK;
            return false;
        }
        sock_timeout(sock_, 0.05);
        if (!freq_offset_file.empty())
            offset_watch_.reset(new sdr::FileWatcher(freq_offset_file, [this](double hz) { set_freq_offset(hz); }));
        avm_logf("Virtual radio RX: %.4f MHz, %.0f S/s\n", freq_ / 1e6, rate);
        net_ = std::thread([this, sink = log_sink()] { log_sink() = sink; listen(); });
        gen_ = std::thread([this, sink = log_sink()] { log_sink() = sink; run(); });
        return true;
    }
    ~VirtualRxSource() override { close(); }
    void close() {
        stop_ = true;
        offset_watch_.reset();
        if (net_.joinable()) net_.join();
        if (gen_.joinable()) gen_.join();
        if (sock_ != BAD_SOCK) { sock_close(sock_); sock_ = BAD_SOCK; }
        out_.end();
    }
    long read(std::vector<cf>& out) override { return out_.take(out, 0.5); }
    void set_gain(double) override {}
    // the GUI's RX offset: retunes by hz, so the signal moves by -hz
    void set_freq_offset(double hz) override { tune_ = hz; }

private:
    struct Burst { std::vector<std::complex<double>> x; double df; };

    // the virtual TX's packets -> this rate
    void listen() {
        std::vector<char> pkt(65536);
        bool have_seq = false;
        uint32_t last = 0;
        while (!stop_) {
            int n = (int)recvfrom(sock_, pkt.data(), (int)pkt.size(), 0, nullptr, nullptr);
            if (n <= 20) continue;
            double tx_freq = get_f64(&pkt[0]), tx_fs = get_f64(&pkt[8]);
            uint32_t seq = 0;
            for (int b = 0; b < 4; ++b) seq = seq << 8 | (uint8_t)pkt[16 + b];
            if (have_seq && seq != last + 1) ++lost_;
            have_seq = true;
            last = seq;
            double df = tx_freq - freq_;
            if (std::fabs(df) > std::max(tx_fs, rate) / 2) continue;     // far off this frequency: not heard
            size_t k = (size_t)(n - 20) / 8;
            std::vector<std::complex<double>> x(k);
            const float* f = (const float*)&pkt[20];
            for (size_t i = 0; i < k; ++i) x[i] = {f[2 * i], f[2 * i + 1]};
            if (tx_fs != rate) x = resample(x, tx_fs, rate);
            std::lock_guard<std::mutex> g(mu_);
            queued_ += x.size();
            bursts_.push_back({std::move(x), df});
        }
    }

    // (a TX at another rate: linear interpolation, carried across packets)
    std::vector<std::complex<double>> resample(const std::vector<std::complex<double>>& x, double from, double to) {
        std::vector<std::complex<double>> y;
        double step = from / to;
        for (; rs_pos_ < (double)x.size(); rs_pos_ += step) {
            double p = rs_pos_;
            long i = (long)std::floor(p);
            double fr = p - i;
            std::complex<double> a = i < 0 ? rs_last_ : x[(size_t)i];
            std::complex<double> b = (size_t)(i + 1) < x.size() ? x[(size_t)(i + 1)] : x.back();
            y.push_back(a * (1 - fr) + b * fr);
        }
        rs_pos_ -= (double)x.size();
        rs_last_ = x.back();
        return y;
    }

    // n TX samples to play out now (false: none)
    bool take(size_t n, std::vector<std::complex<double>>& out, double& df) {
        std::lock_guard<std::mutex> g(mu_);
        if (!playing_) {
            if (queued_ < JITTER_S * rate) return false;
            playing_ = true;
        }
        if (!queued_) { playing_ = false; return false; }
        out.assign(n, 0.0);
        df = bursts_.front().df;
        size_t k = 0;
        while (k < n && !bursts_.empty()) {
            Burst& b = bursts_.front();
            df = b.df;
            size_t m = std::min(n - k, b.x.size());
            std::copy(b.x.begin(), b.x.begin() + m, out.begin() + k);
            k += m;
            if (m == b.x.size()) bursts_.pop_front();
            else b.x.erase(b.x.begin(), b.x.begin() + m);
        }
        queued_ -= k;
        if (k < n) playing_ = false;       // ran out: the burst ended
        return true;
    }

    // the stream: real time, in blocks
    void run() {
        std::mt19937_64 rng(std::random_device{}());
        std::normal_distribution<double> nd;
        double t0 = mono(), phase = 0;
        long long n_out = 0;
        std::vector<std::complex<double>> x;
        while (!stop_) {
            long long due = (long long)((mono() - t0) * rate) - n_out;
            if (due < BLOCK_S * rate) {
                std::this_thread::sleep_for(std::chrono::duration<double>(BLOCK_S / 2));
                continue;
            }
            size_t n = (size_t)due;
            std::vector<cf> out(n);
            double df;
            if (take(n, x, df)) {
                double p = 0;
                size_t live = 0;
                for (auto& v : x) if (std::abs(v) > 0) { p += std::norm(v); ++live; }
                if (live) sig_p_ = sig_p_ < 0 ? p / live : 0.9 * sig_p_ + 0.1 * p / live;
                double f = df + place_ - tune_.load(), w = TWO_PI * f / rate;
                for (size_t i = 0; i < n; ++i) {
                    std::complex<double> v = x[i] * std::polar(1.0, phase + w * (double)i);
                    out[i] = cf((float)v.real(), (float)v.imag());
                }
                phase = std::fmod(phase + w * (double)n, TWO_PI);
            }
            double sd = std::sqrt((sig_p_ < 0 ? 0.01 : sig_p_) * std::pow(10.0, NOISE_DB / 10) / 2);
            for (auto& v : out) v += cf((float)(sd * nd(rng)), (float)(sd * nd(rng)));
            n_out += (long long)n;
            out_.push(std::move(out));
        }
    }

    static double get_f64(const char* p) {
        uint64_t u = 0;
        for (int b = 0; b < 8; ++b) u = u << 8 | (uint8_t)p[b];
        double v;
        std::memcpy(&v, &u, 8);
        return v;
    }

    WsaInit wsa_;
    double freq_ = 0, place_ = 0;
    std::atomic<double> tune_{0};
    sock_t sock_ = BAD_SOCK;
    std::atomic<bool> stop_{false};
    std::thread net_, gen_;
    std::mutex mu_;
    std::deque<Burst> bursts_;
    size_t queued_ = 0;
    bool playing_ = false;
    double sig_p_ = -1;
    long long lost_ = 0;
    double rs_pos_ = 0;
    std::complex<double> rs_last_ = 0;
    SampleQueue out_;
    std::unique_ptr<sdr::FileWatcher> offset_watch_;
};

// ---------------------------------------------------------------- KiwiSDR
// kiwi_source.parse_server: 'host', 'host:port', 'kiwi:host:port', 'http://host:port/'
inline void kiwi_parse_server(std::string s, std::string& host, int& port) {
    while (!s.empty() && isspace((unsigned char)s.back())) s.pop_back();
    while (!s.empty() && isspace((unsigned char)s.front())) s.erase(s.begin());
    for (const char* p : {"kiwi:", "http://", "https://", "ws://"})
        if (s.rfind(p, 0) == 0) s = s.substr(std::strlen(p));
    while (!s.empty() && s.back() == '/') s.pop_back();
    size_t c = s.find(':');
    host = s.substr(0, c);
    port = 8073;
    if (c != std::string::npos) {
        std::string ps = s.substr(c + 1);
        if (!ps.empty() && ps.find_first_not_of("0123456789") == std::string::npos) port = std::stoi(ps);
    }
}

class KiwiRxSource : public sdr::RadioIn {
public:
    static constexpr double PASSBAND_FRAC = 0.47;   // IQ passband each side, of the sample rate
    static constexpr double KEEPALIVE_S = 5.0;
    static constexpr double STALL_S = 10.0;         // nothing this long: give up (the GUI restarts RX)

    bool open(const std::string& server, double freq_hz, double gain_db, const std::string& gain_file,
              const std::string& freq_offset_file, double connect_timeout_s = 15.0, const std::string& user = "AVM") {
        kiwi_parse_server(server, host_, port_);
        if (host_.empty()) { error = "no KiwiSDR server given (Config, Receiver: KiwiSDR)"; return false; }
        if (host_.find("..") != std::string::npos || host_.front() == '.' || host_.back() == '.') {
            error = "KiwiSDR server '" + host_ + "' isn't a valid address (Config, Receiver: KiwiSDR)";
            return false;
        }
        freq_ = freq_hz;
        gain_ = gain_db;
        user_ = user;
        if (!connect()) return false;
        // "/ws/kiwi/<ts>/SND": what the Kiwi's own web page uses (some, behind a
        // proxy, accept the old "/<ts>/SND" but then never send a byte)
        std::string path = "/ws/kiwi/" + std::to_string((long long)time(nullptr)) + "/SND";
        if (!handshake(path)) { sock_close(sock_); sock_ = BAD_SOCK; return false; }
        sock_timeout(sock_, STALL_S);
        send("SET auth t=kiwi p=");
        send("SET ident_user=" + user_);    // Kiwis that enforce limits drop an unnamed client
        reader_ = std::thread([this, sink = log_sink()] { log_sink() = sink; run(); });
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_.wait_for(lk, std::chrono::duration<double>(connect_timeout_s), [this] { return rate_ready_; });
            if (!rate_ready_ || !fail_.empty() || rate <= 0) {
                std::string why = !fail_.empty() ? fail_
                    : rate <= 0 && rate_ready_ ? "KiwiSDR " + host_ + ":" + std::to_string(port_) +
                          " accepted the connection but sent no audio -- it may be busy; try again or another KiwiSDR"
                    : "KiwiSDR " + host_ + ":" + std::to_string(port_) + " sent no stream within " +
                          std::to_string((int)connect_timeout_s) + " s";
                lk.unlock();
                close();
                error = why;
                return false;
            }
        }
        avm_logf("KiwiSDR RX active: %s:%d freq=%.4fMHz rate=%.2f S/s gain=%gdB (Kiwi AGC off)\n", host_.c_str(), port_,
                 freq_ / 1e6, rate, gain_);
        if (!gain_file.empty()) gain_watch_.reset(new sdr::FileWatcher(gain_file, [this](double db) { set_gain(db); }));
        if (!freq_offset_file.empty())
            offset_watch_.reset(new sdr::FileWatcher(freq_offset_file, [this](double hz) { set_freq_offset(hz); }));
        keep_ = std::thread([this] {
            while (!stop_) {
                for (int i = 0; i < (int)(KEEPALIVE_S * 10) && !stop_; ++i)
                    std::this_thread::sleep_for(std::chrono::milliseconds(100));
                if (!stop_) send("SET keepalive");
            }
        });
        return true;
    }
    ~KiwiRxSource() override { close(); }
    void close() {
        stop_ = true;
        gain_watch_.reset();
        offset_watch_.reset();
        if (sock_ != BAD_SOCK) {
#if defined(_WIN32)
            shutdown(sock_, SD_BOTH);
#else
            shutdown(sock_, SHUT_RDWR);
#endif
        }
        if (reader_.joinable()) reader_.join();
        if (keep_.joinable()) keep_.join();
        if (sock_ != BAD_SOCK) { sock_close(sock_); sock_ = BAD_SOCK; }
        out_.end();
    }
    long read(std::vector<cf>& out) override { return out_.take(out, 0.5); }
    void set_gain(double db) override {
        gain_ = std::max(0.0, std::min(120.0, db));
        set_agc();
    }
    void set_freq_offset(double hz) override {
        if (hz == offset_) return;
        offset_ = hz;
        tune();
        avm_logf("RX frequency offset %+.3f kHz\n", hz / 1e3);
    }

private:
    bool connect() {
        addrinfo hints, *res = nullptr;
        std::memset(&hints, 0, sizeof hints);
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        if (getaddrinfo(host_.c_str(), std::to_string(port_).c_str(), &hints, &res) != 0 || !res) {
            error = "KiwiSDR " + host_ + ": name not found";
            return false;
        }
        for (addrinfo* p = res; p; p = p->ai_next) {
            sock_ = socket(p->ai_family, p->ai_socktype, p->ai_protocol);
            if (sock_ == BAD_SOCK) continue;
            sock_timeout(sock_, 10.0);
            if (::connect(sock_, p->ai_addr, (int)p->ai_addrlen) == 0) break;
            sock_close(sock_);
            sock_ = BAD_SOCK;
        }
        freeaddrinfo(res);
        if (sock_ == BAD_SOCK) error = "KiwiSDR " + host_ + ":" + std::to_string(port_) + " doesn't answer";
        return sock_ != BAD_SOCK;
    }
    bool handshake(const std::string& path) {
        static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        std::mt19937 rng(std::random_device{}());
        std::string key;
        for (int i = 0; i < 21; ++i) key += B64[rng() % 64];
        key += "Q==";       // 16 random bytes' worth, base64's length
        std::string req = "GET " + path + " HTTP/1.1\r\nHost: " + host_ + ":" + std::to_string(port_) +
                          "\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: " + key +
                          "\r\nSec-WebSocket-Version: 13\r\n\r\n";
        if (!send_all(req.data(), req.size())) { error = "KiwiSDR: the connection failed"; return false; }
        while (buf_.find("\r\n\r\n") == std::string::npos) {
            char t[4096];
            int n = (int)recv(sock_, t, sizeof t, 0);
            if (n <= 0) { error = "KiwiSDR closed the connection during the handshake"; return false; }
            buf_.append(t, (size_t)n);
        }
        size_t e = buf_.find("\r\n\r\n");
        std::string status = buf_.substr(0, buf_.find("\r\n"));
        buf_.erase(0, e + 4);
        if (status.find(" 101 ") == std::string::npos) { error = "KiwiSDR refused the stream: " + status; return false; }
        return true;
    }
    bool send_all(const char* p, size_t n) {
        while (n) {
            int k = (int)::send(sock_, p, (int)n, 0);
            if (k <= 0) return false;
            p += k;
            n -= (size_t)k;
        }
        return true;
    }
    // a masked text frame
    void send(const std::string& text) {
        std::lock_guard<std::mutex> g(send_mu_);
        if (sock_ == BAD_SOCK) return;
        std::string f;
        f += (char)0x81;
        size_t n = text.size();
        if (n < 126) f += (char)(0x80 | n);
        else { f += (char)(0x80 | 126); f += (char)(n >> 8); f += (char)(n & 0xFF); }
        uint8_t m[4];
        for (auto& b : m) b = (uint8_t)(mask_rng_() & 0xFF);
        f.append((const char*)m, 4);
        for (size_t i = 0; i < n; ++i) f += (char)(text[i] ^ m[i % 4]);
        send_all(f.data(), f.size());
    }
    bool exact(size_t n, std::string& out) {
        while (buf_.size() < n) {
            char t[65536];
            int k = (int)recv(sock_, t, sizeof t, 0);
            if (k <= 0) return false;
            buf_.append(t, (size_t)k);
        }
        out = buf_.substr(0, n);
        buf_.erase(0, n);
        return true;
    }
    // (opcode, payload); false: the connection ended or stalled
    bool frame(int& op, std::string& p) {
        std::string h;
        if (!exact(2, h)) return false;
        op = (uint8_t)h[0] & 0x0F;
        const bool masked = (uint8_t)h[1] & 0x80;     // (before h holds an extended length)
        uint64_t n = (uint8_t)h[1] & 0x7F;
        if (n == 126) { if (!exact(2, h)) return false; n = (uint8_t)h[0] << 8 | (uint8_t)h[1]; }
        else if (n == 127) { if (!exact(8, h)) return false; n = 0; for (int i = 0; i < 8; ++i) n = n << 8 | (uint8_t)h[i]; }
        std::string m;
        if (masked && !exact(4, m)) return false;
        if (!exact((size_t)n, p)) return false;
        if (!m.empty()) for (size_t i = 0; i < p.size(); ++i) p[i] ^= m[i % 4];
        return true;
    }
    void tune() {
        int half = (int)(PASSBAND_FRAC * (rate > 0 ? rate : 12000));
        char t[160];
        std::snprintf(t, sizeof t, "SET mod=iq low_cut=%d high_cut=%d freq=%.3f", -half, half, (freq_ + offset_) / 1e3);
        send(t);
    }
    void set_agc() {
        send("SET agc=0 hang=0 thresh=-100 slope=6 decay=1000 manGain=" + std::to_string((int)std::lround(gain_)));
    }
    void configure(const std::string& audio_rate) {
        send("SET AR OK in=" + audio_rate + " out=44100");
        tune();
        set_agc();
        send("SET compression=0");
        send("SET squelch=0 max=0");
        send("SET gen=0 mix=-1");
        send("SERVER DE CLIENT " + user_ + " SND");
    }
    void fail(const std::string& why) {
        avm_logf("KiwiSDR: %s\n", why.c_str());
        std::lock_guard<std::mutex> g(mu_);
        fail_ = why;
        rate_ready_ = true;
        cv_.notify_all();
    }
    void msg(const std::string& text) {
        size_t i = 0;
        while (i < text.size()) {
            size_t j = text.find(' ', i);
            if (j == std::string::npos) j = text.size();
            std::string kv = text.substr(i, j - i);
            i = j + 1;
            size_t eq = kv.find('=');
            std::string key = kv.substr(0, eq), val = eq == std::string::npos ? "" : kv.substr(eq + 1);
            if (key == "sample_rate") rate = std::atof(val.c_str());
            else if (key == "audio_rate") configure(val);
            // (too_busy=0 is only a notice, sent ~10 s into a good stream)
            else if (key == "too_busy" && !val.empty() && val != "0")
                fail("KiwiSDR " + host_ + " is full (all " + val + " channels in use) -- try later or another Kiwi");
            else if (key == "badp" && !val.empty() && val != "0")
                fail("KiwiSDR " + host_ + " needs a password, or its free channels are all in use");
            else if (key == "down") fail("KiwiSDR " + host_ + " is down for maintenance");
            else if (key == "inactivity_timeout" || key == "redirect") avm_logf("[kiwi] %s\n", kv.c_str());
        }
    }
    void run() {
        bool have_seq = false, retuned = false;
        uint32_t last = 0;
        long long count = 0;
        std::string p;
        int op;
        while (!stop_ && fail_.empty()) {
            if (!frame(op, p)) {
                if (!stop_) avm_logf("KiwiSDR: the connection ended or sent nothing for %g s -- stopping\n", STALL_S);
                break;
            }
            if (op == 8) {
                avm_logf("KiwiSDR closed the stream%s\n", p.size() > 2 ? (": " + p.substr(2)).c_str() : "");
                break;
            }
            if ((op != 1 && op != 2) || p.size() < 3) continue;
            if (p.compare(0, 3, "MSG") == 0) { msg(p.size() > 4 ? p.substr(4) : std::string()); continue; }
            if (p.compare(0, 3, "SND") != 0 || p.size() < 20) continue;
            uint32_t seq = (uint8_t)p[4] | (uint8_t)p[5] << 8 | (uint8_t)p[6] << 16 | (uint32_t)(uint8_t)p[7] << 24;
            size_t n = (p.size() - 20) / 4;     // after flags, seq, S-meter and the IQ header (GPS time)
            if (!n) continue;
            if (rate > 0) {
                std::lock_guard<std::mutex> g(mu_);
                if (!rate_ready_) { rate_ready_ = true; cv_.notify_all(); }
            }
            if (have_seq && seq != last + 1) {
                uint32_t missing = seq - last - 1;
                if (missing > 0 && missing < 1000) {
                    // the timeline kept: a lost packet becomes silence of its length
                    avm_logf("[kiwi] %u packet(s) lost on the network -- filled with silence\n", missing);
                    out_.push(std::vector<cf>((size_t)missing * n));
                }
            }
            have_seq = true;
            last = seq;
            std::vector<cf> iq(n);
            const uint8_t* b = (const uint8_t*)p.data() + 20;
            for (size_t i = 0; i < n; ++i) {
                int16_t re = (int16_t)(b[4 * i] << 8 | b[4 * i + 1]), im = (int16_t)(b[4 * i + 2] << 8 | b[4 * i + 3]);
                iq[i] = cf(re / 32768.0f, im / 32768.0f);
            }
            count += (long long)n;
            out_.push(std::move(iq));
            if (!retuned && count > 0.5 * (rate > 0 ? rate : 12000)) {
                // newer firmware ignores the mode / passband / gain set before its
                // audio starts (it streamed an SSB slice with its AGC on): again now
                retuned = true;
                tune();
                set_agc();
            }
        }
        {
            std::lock_guard<std::mutex> g(mu_);
            rate_ready_ = true;
            cv_.notify_all();
        }
        out_.end();
    }

    WsaInit wsa_;
    std::string host_, user_, buf_, fail_;
    int port_ = 8073;
    double freq_ = 0, gain_ = 50;
    std::atomic<double> offset_{0};
    sock_t sock_ = BAD_SOCK;
    std::atomic<bool> stop_{false};
    std::thread reader_, keep_;
    std::mutex mu_, send_mu_;
    std::condition_variable cv_;
    bool rate_ready_ = false;
    std::mt19937 mask_rng_{std::random_device{}()};
    SampleQueue out_;
    std::unique_ptr<sdr::FileWatcher> gain_watch_, offset_watch_;
};

}  // namespace netradio
