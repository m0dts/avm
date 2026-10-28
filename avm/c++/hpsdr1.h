// hpsdr1.h -- an openHPSDR protocol 1 radio (Hermes-Lite 2, Radioberry) over
// the network: avm_rx / avm_tx --sdr hpsdr (hpsdr1_source.py is the Python
// receiver's).
//
// Protocol 1 has one UDP port (1024). The host sends 1032-byte "EP2" packets:
// two 512-byte frames, each 7F 7F 7F, five control bytes (C0-C4, one register
// each, C0 bit 0 = MOX) and 63 points of L R I Q (16 bit, big-endian), the TX
// IQ always at 48 kS/s. The radio sends "EP6" packets of receive IQ: two
// frames, each 7F 7F 7F, five status bytes and 63 points of I Q (24 bit) and
// a 16-bit mic sample, with one receiver.
//
// The radio talks to one host port only, so a process has one Radio (the
// socket, one thread) that the receiver and the transmitter share: in the GUI
// both run at once on one radio (separate avm_rx / avm_tx processes can't).
// Its thread reads the EP6s and sends one EP2 per 48 kS/s worth of them --
// every 1, 2, 4 or 8 EP6 at 48-384 kS/s -- so the radio's own clock paces the
// TX (its buffer never drifts empty or full) and the control registers go out
// ~380 times a second (the radio stops if they stop: its watchdog). The
// registers cycle: rate (and duplex), TX frequency, RX frequency, RX gain,
// TX drive. MOX is set only while there's waveform to send.
//
// RxSource: the receiver at 48-384 kS/s, lost packets filled with silence of
// the same length (the sequence numbers say how many). Gain: a Hermes-Lite 2's
// LNA, -12..+48 dB, set from the gain directly; other boards a 0-31 dB step
// attenuator (gain 31 = none). Samples are scaled to 16-bit full scale, not
// 24: a weak signal sits near -90 dBFS of 24 bits, under the sync search's
// reach (floats: the headroom never clips).
//
// TxSink: the waveform at 48 kS/s, queued at most TX_LEAD_S ahead; unkeyed on
// close. Drive: --tx-gain (dB, 0 = full) sets the drive level (0-255, linear
// in amplitude; a Hermes-Lite 2 uses its top 4 bits). The Hermes-Lite 2's PA
// is left off: its small-signal output only (a Radioberry has no PA).
#pragma once
#include <atomic>
#include <chrono>
#include <cmath>
#include <complex>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#if defined(__linux__)
#include <pthread.h>
#endif
#include <vector>

#include "avm_log.h"
#include "sdr.h"

#if defined(_WIN32)
#include <winsock2.h>
#include <ws2tcpip.h>
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <unistd.h>
#endif

namespace hpsdr1 {

const int PORT = 1024;
const double TX_RATE = 48000.0;
const int POINTS = 63;                 // per frame
const double TX_LEAD_S = 0.2;          // waveform queued at most this far ahead
const int HL2_BOARD = 6;

// ---------------------------------------------------------------- the radio
class Radio {
public:
    using IqFn = std::function<void(const std::complex<float>* p, size_t n, bool fresh)>;
    std::string error, ip, board;
    int board_id = -1;

    // the process's radio at uri, opened on first use (shared while anyone holds it)
    static std::shared_ptr<Radio> get(const std::string& uri, std::string* err) {
        static std::mutex reg_mu;
        static std::weak_ptr<Radio> current;
        std::lock_guard<std::mutex> g(reg_mu);
        if (auto r = current.lock()) {
            std::string host = strip(uri);
            if (host.empty() || host == r->ip) return r;
            *err = "HPSDR " + r->ip + " is already open here -- one HPSDR radio at a time";
            return nullptr;
        }
        auto r = std::shared_ptr<Radio>(new Radio);
        if (!r->open(uri)) { *err = r->error; return nullptr; }
        current = r;
        return r;
    }
    ~Radio() { close(); }

    bool hl2() const { return board_id == HL2_BOARD; }
    // per packet received -- (callers' counters; the transmitter waits for the first)
    long long packets() const { return packets_; }

    // the receiver: its rate (speed 0-3: 48-384 kS/s), frequency, gain, IQ callback
    void attach_rx(int speed, double freq, IqFn fn) {
        std::lock_guard<std::mutex> g(mu_);
        speed_ = speed;
        rx_freq_ = freq;
        rx_fn_ = std::move(fn);
        rx_on_ = true;
    }
    void detach_rx() {
        std::lock_guard<std::mutex> g(mu_);
        rx_on_ = false;
        rx_fn_ = nullptr;
        if (!tx_on_) speed_ = 0;
    }
    void set_rx_freq(double hz) { std::lock_guard<std::mutex> g(mu_); rx_freq_ = hz; }
    void set_rx_gain(double db) { rx_gain_ = db; }
    int lna() const { return (int)std::lround(std::max(-12.0, std::min(48.0, rx_gain_.load()))); }
    int attenuation() const { return (int)std::lround(std::max(0.0, std::min(31.0, 31.0 - rx_gain_.load()))); }

    // the transmitter
    void attach_tx(double freq) {
        std::lock_guard<std::mutex> g(mu_);
        tx_freq_ = freq;
        tx_on_ = true;
        tx_q_.clear();
    }
    void detach_tx() {
        {
            std::lock_guard<std::mutex> g(mu_);
            tx_on_ = false;
            tx_q_.clear();
        }
        // unkeyed before going: a few packets' time for the thread to send MOX off
        std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }
    void set_drive(int d) { drive_ = d; }
    int drive() const { return drive_; }
    // queued; false when full (TX_LEAD_S waiting)
    size_t tx_push(const std::complex<float>* x, size_t n) {
        std::lock_guard<std::mutex> g(mu_);
        size_t cap = (size_t)(TX_LEAD_S * TX_RATE);
        size_t k = std::min(n, cap > tx_q_.size() ? cap - tx_q_.size() : 0);
        tx_q_.insert(tx_q_.end(), x, x + k);
        return k;
    }
    size_t tx_queued() { std::lock_guard<std::mutex> g(mu_); return tx_q_.size(); }
    long long underruns() const { return underruns_; }
    bool running() const { return !stop_ && !failed_; }

private:
#if defined(_WIN32)
    SOCKET S() const { return (SOCKET)sock_; }
#else
    int S() const { return (int)sock_; }
#endif
    long long sock_ = -1;
    sockaddr_in to_{};
    uint32_t seq_ = 0;
    std::thread th_;
    std::atomic<bool> stop_{false}, failed_{false};
    std::mutex mu_;
    // (under mu_)
    bool rx_on_ = false, tx_on_ = false;
    int speed_ = 0;
    double rx_freq_ = 0, tx_freq_ = 0;
    IqFn rx_fn_;
    std::deque<std::complex<float>> tx_q_;
    // (the thread's)
    int reg_ = 0;
    bool keyed_ = false;
    std::atomic<double> rx_gain_{30};
    std::atomic<int> drive_{0};
    std::atomic<long long> packets_{0}, underruns_{0};

    static std::string strip(const std::string& uri) { return uri.rfind("ip:", 0) == 0 ? uri.substr(3) : uri; }

    // a UDP socket, and the radio at uri ("", "A.B.C.D" or "ip:A.B.C.D"; empty:
    // the first that answers discovery) found and free; then started
    bool open(const std::string& uri) {
#if defined(_WIN32)
        WSADATA w;
        WSAStartup(MAKEWORD(2, 2), &w);
#endif
        sock_ = (long long)socket(AF_INET, SOCK_DGRAM, 0);
        int one = 1;
        setsockopt(S(), SOL_SOCKET, SO_BROADCAST, (const char*)&one, sizeof one);
        int big = 4 << 20;
        setsockopt(S(), SOL_SOCKET, SO_RCVBUF, (const char*)&big, sizeof big);
        sockaddr_in any{};
        any.sin_family = AF_INET;
        if (bind(S(), (const sockaddr*)&any, sizeof any) != 0) { error = "HPSDR: no UDP socket"; close_socket(); return false; }
        bool busy = false;
        if (!discover(strip(uri), &busy)) { close_socket(); return false; }
        if (busy) { error = "HPSDR " + ip + " is in use by another program -- close it first"; close_socket(); return false; }
        for (int i = 0; i < 5; ++i) send_packet();      // the registers once round
        start(true);
        th_ = std::thread([this, sink = log_sink()] {
            log_sink() = sink;
#if defined(__linux__)
            pthread_setname_np(pthread_self(), "hpsdr1");
#endif
            run();
        });
        return true;
    }

    void close() {
        if (sock_ == -1) return;
        stop_ = true;
        if (th_.joinable()) th_.join();
        {
            std::lock_guard<std::mutex> g(mu_);
            tx_on_ = rx_on_ = false;
            tx_q_.clear();
        }
        for (int i = 0; i < 3; ++i) { send_packet(); std::this_thread::sleep_for(std::chrono::milliseconds(5)); }
        for (int i = 0; i < 3; ++i) start(false);
        close_socket();
    }

    void close_socket() {
        if (sock_ == -1) return;
#if defined(_WIN32)
        closesocket((SOCKET)sock_);
        WSACleanup();
#else
        ::close((int)sock_);
#endif
        sock_ = -1;
    }

    bool discover(const std::string& host, bool* busy) {
        unsigned char pkt[63] = {0xEF, 0xFE, 0x02};
        sockaddr_in d{};
        d.sin_family = AF_INET;
        d.sin_port = htons(PORT);
        d.sin_addr.s_addr = host.empty() ? htonl(INADDR_BROADCAST) : inet_addr(host.c_str());
        sendto(S(), (const char*)pkt, sizeof pkt, 0, (const sockaddr*)&d, sizeof d);
        auto t_end = std::chrono::steady_clock::now() + std::chrono::milliseconds(800);
        while (std::chrono::steady_clock::now() < t_end) {
            if (!readable(50)) continue;
            unsigned char b[1500];
            sockaddr_in from{};
            socklen_t fl = sizeof from;
            int n = (int)recvfrom(S(), (char*)b, sizeof b, 0, (sockaddr*)&from, &fl);
            if (n >= 11 && b[0] == 0xEF && b[1] == 0xFE && (b[2] == 2 || b[2] == 3)) {
                to_ = from;
                to_.sin_port = htons(PORT);
                ip = inet_ntoa(from.sin_addr);
                *busy = b[2] == 3;
                board_id = b[10];
                board = board_id == HL2_BOARD ? "Hermes Lite" : board_id == 1 ? "Hermes" : "board " + std::to_string(board_id);
                return true;
            }
        }
        error = "HPSDR: no protocol 1 radio answered on the network" + (host.empty() ? std::string() : " at " + host) +
                " -- check it's on and connected";
        return false;
    }

    bool readable(int ms) {
        fd_set fs;
        FD_ZERO(&fs);
        FD_SET(S(), &fs);
        timeval tv{ms / 1000, (ms % 1000) * 1000};
        return select((int)sock_ + 1, &fs, nullptr, nullptr, &tv) > 0;
    }

    void start(bool run) {
        unsigned char p[64] = {0xEF, 0xFE, 0x04, (unsigned char)(run ? 1 : 0)};
        sendto(S(), (const char*)p, sizeof p, 0, (const sockaddr*)&to_, sizeof to_);
    }

    static void freq_bytes(unsigned char* c, double hz) {
        uint32_t f = (uint32_t)std::llround(hz);
        c[1] = f >> 24; c[2] = f >> 16; c[3] = f >> 8; c[4] = f;
    }

    // one EP2: the next two registers in turn, and the TX queue's next 126 points
    // (MOX while there's waveform)
    void send_packet() {
        unsigned char p[1032] = {};
        p[0] = 0xEF; p[1] = 0xFE; p[2] = 0x01; p[3] = 0x02;
        p[4] = seq_ >> 24; p[5] = seq_ >> 16; p[6] = seq_ >> 8; p[7] = seq_;
        ++seq_;
        std::complex<float> pts[2 * POINTS] = {};
        size_t got = 0;
        int speed;
        double rxf, txf;
        bool tx;
        {
            std::lock_guard<std::mutex> g(mu_);
            speed = speed_;
            tx = tx_on_;
            // an unused side's frequency follows the other's
            rxf = rx_on_ || !tx_on_ ? rx_freq_ : tx_freq_;
            txf = tx_on_ ? tx_freq_ : rx_freq_;
            if (tx) {
                got = std::min(tx_q_.size(), (size_t)(2 * POINTS));
                for (size_t i = 0; i < got; ++i) pts[i] = tx_q_[i];
                tx_q_.erase(tx_q_.begin(), tx_q_.begin() + got);
            }
        }
        bool mox = tx && got > 0;
        if (tx && keyed_ && got < 2 * POINTS) ++underruns_;
        keyed_ = mox;
        for (int k = 0; k < 2; ++k) {
            unsigned char* o = p + 8 + 512 * k;
            std::memset(o, 0x7F, 3);
            unsigned char* c = o + 3;
            switch (reg_) {
            case 0: c[0] = 0x00; c[1] = (unsigned char)speed; c[4] = 0x04; break;     // rate; one receiver, duplex
            case 1: c[0] = 0x02; freq_bytes(c, txf); break;                           // TX frequency
            case 2: c[0] = 0x04; freq_bytes(c, rxf); break;                           // receiver 1 frequency
            case 3:                                                                   // RX gain
                c[0] = 0x14;
                c[4] = hl2() ? (unsigned char)(0x40 | (lna() + 12)) : (unsigned char)(0x20 | attenuation());
                break;
            default: c[0] = 0x12; c[1] = (unsigned char)drive_.load(); break;          // TX drive; PA off
            }
            reg_ = (reg_ + 1) % 5;
            if (mox) c[0] |= 0x01;
            for (int i = 0; i < POINTS; ++i) {
                std::complex<float> v = pts[k * POINTS + i];
                int iv = (int)std::lround(std::max(-1.0f, std::min(1.0f, v.real())) * 32767.0f);
                int qv = (int)std::lround(std::max(-1.0f, std::min(1.0f, v.imag())) * 32767.0f);
                unsigned char* s = o + 8 + 8 * i;     // L R (audio: unused) I Q
                s[4] = (unsigned char)(iv >> 8); s[5] = (unsigned char)iv;
                s[6] = (unsigned char)(qv >> 8); s[7] = (unsigned char)qv;
            }
        }
        sendto(S(), (const char*)p, sizeof p, 0, (const sockaddr*)&to_, sizeof to_);
    }

    void run() {
        const float scale = 1.0f / 32768.0f;      // 16-bit full scale (see the top)
        auto now = std::chrono::steady_clock::now();
        auto last_send = now, last_data = now, last_push = now, last_overload = now - std::chrono::seconds(10);
        bool have_seq = false, fresh = true, rx_was = false;
        uint32_t last_seq = 0;
        int ep6 = 0;              // EP6s since the last EP2
        long long gaps = 0;
        std::vector<std::complex<float>> batch;
        batch.reserve(8192);
        unsigned char b[1500];
        while (!stop_) {
            now = std::chrono::steady_clock::now();
            if (!readable(5)) {
                // nothing from the radio (starting up): keep its watchdog fed
                if (now - last_send > std::chrono::milliseconds(50)) {
                    send_packet();
                    last_send = now;
                }
                // (it sends ~380 packets a second at 48 kS/s: 2 s of nothing is a fault)
                if (packets_ && now - last_data > std::chrono::seconds(2)) {
                    avm_logf("ERROR: HPSDR %s %s stopped responding (nothing for 2 s) -- TX/RX stopped\n",
                             board.c_str(), ip.c_str());
                    break;
                }
                continue;
            }
            int speed;
            bool rx;
            {
                std::lock_guard<std::mutex> g(mu_);
                speed = speed_;
                rx = rx_on_;
            }
            if (rx && !rx_was) { fresh = true; have_seq = false; batch.clear(); }
            rx_was = rx;
            // every packet waiting, then one hand-over
            while (readable(0)) {
                int n = (int)recv(S(), (char*)b, sizeof b, 0);
                if (n != 1032 || b[0] != 0xEF || b[1] != 0xFE || b[2] != 0x01 || b[3] != 0x06) continue;
                ++packets_;
                last_data = now;
                // the TX pace: one EP2 per 126 points at 48 kS/s
                if (++ep6 >= (1 << speed)) {
                    ep6 = 0;
                    send_packet();
                    last_send = now;
                }
                if (!rx) continue;
                uint32_t seq = (uint32_t)b[4] << 24 | (uint32_t)b[5] << 16 | (uint32_t)b[6] << 8 | b[7];
                if (have_seq && seq != last_seq + 1) {
                    uint32_t missing = seq - last_seq - 1;
                    if (missing > 0 && missing < 1000) {
                        gaps += missing;
                        avm_logf("[hpsdr] %u packet(s) lost on the network -- filled with silence\n", missing);
                        batch.insert(batch.end(), (size_t)missing * 2 * POINTS, std::complex<float>(0, 0));
                    }
                }
                have_seq = true;
                last_seq = seq;
                for (int k = 0; k < 2; ++k) {
                    const unsigned char* o = b + 8 + 512 * k;
                    if (o[0] != 0x7F || o[1] != 0x7F || o[2] != 0x7F) continue;
                    if ((o[3] >> 3) == 0 && (o[4] & 1) && now - last_overload > std::chrono::seconds(5)) {
                        last_overload = now;
                        avm_logf("HPSDR: ADC overload -- lower the RX gain\n");
                    }
                    for (int i = 0; i < POINTS; ++i) {
                        const unsigned char* s = o + 8 + 8 * i;
                        int32_t iv = (int32_t)((uint32_t)s[0] << 24 | (uint32_t)s[1] << 16 | (uint32_t)s[2] << 8) >> 8;
                        int32_t qv = (int32_t)((uint32_t)s[3] << 24 | (uint32_t)s[4] << 16 | (uint32_t)s[5] << 8) >> 8;
                        batch.emplace_back(iv * scale, qv * scale);
                    }
                }
                if (batch.size() >= 16384) break;
            }
            // handed over every ~20 ms, not per packet: each hand-over wakes the
            // receiver for a fresh decode attempt (per packet, ~150 a fragment,
            // cost it most of a Pi 4 core)
            const double rate = TX_RATE * (1 << speed);
            if (!batch.empty() && (batch.size() >= (size_t)(rate / 50) || now - last_push > std::chrono::milliseconds(20))) {
                last_push = now;
                std::lock_guard<std::mutex> g(mu_);
                if (rx_on_ && rx_fn_) rx_fn_(batch.data(), batch.size(), fresh);
                fresh = false;
                batch.clear();
            }
        }
        if (gaps) avm_logf("HPSDR: %lld packet(s) lost on the network in all\n", gaps);
        failed_ = true;
        std::lock_guard<std::mutex> g(mu_);
        if (rx_on_ && rx_fn_) rx_fn_(nullptr, 0, false);    // (the receiver: the radio stopped)
    }
};

// ---------------------------------------------------------------- receiving
class RxSource : public sdr::RadioIn {
public:
    // want_rate: the lowest rate wanted (the smallest of 48/96/192/384 kS/s at or above is used)
    bool open(const std::string& uri, double freq_hz, double want_rate, double gain_db, const std::string& gain_file,
              const std::string& freq_offset_file) {
        freq_ = freq_hz;
        static const double rates[] = {48000, 96000, 192000, 384000};
        int speed = 3;
        for (int i = 0; i < 4; ++i)
            if (rates[i] >= want_rate) { speed = i; break; }
        rate = rates[speed];
        radio_ = Radio::get(uri, &error);
        if (!radio_) return false;
        radio_->set_rx_gain(gain_db);
        radio_->attach_rx(speed, freq_, [this](const std::complex<float>* p, size_t n, bool) {
            // (the radio's thread, under its lock)
            std::lock_guard<std::mutex> g(mu_);
            if (!p) failed_ = true;
            else { q_.insert(q_.end(), p, p + n); got_ = true; }
            cv_.notify_all();
        });
        attached_ = true;
        {
            std::unique_lock<std::mutex> lk(mu_);
            cv_.wait_for(lk, std::chrono::seconds(3), [this] { return got_ || failed_; });
        }
        if (!got_) {
            error = "HPSDR " + radio_->board + " " + radio_->ip + " sent no IQ within 3 s";
            close();
            return false;
        }
        avm_logf("HPSDR RX active: %s at %s (protocol 1) freq=%.4fMHz rate=%g kS/s %s\n", radio_->board.c_str(),
                 radio_->ip.c_str(), freq_ / 1e6, rate / 1e3, gain_text().c_str());
        if (!gain_file.empty()) gain_watch_.reset(new sdr::FileWatcher(gain_file, [this](double db) { set_gain(db); }));
        if (!freq_offset_file.empty())
            offset_watch_.reset(new sdr::FileWatcher(freq_offset_file, [this](double hz) { set_freq_offset(hz); }));
        return true;
    }

    long read(std::vector<std::complex<float>>& out) override {
        std::unique_lock<std::mutex> lk(mu_);
        cv_.wait_for(lk, std::chrono::milliseconds(500), [this] { return !q_.empty() || failed_ || stop_; });
        if (q_.empty()) {
            out.clear();
            return failed_ || stop_ ? -1 : 0;
        }
        out.swap(q_);
        q_.clear();
        return (long)out.size();
    }

    void set_gain(double db) override {
        if (!radio_) return;
        radio_->set_rx_gain(db);
        avm_logf("HPSDR %s\n", gain_text().c_str());
    }
    void set_freq_offset(double hz) override {
        if (hz == offset_ || !radio_) return;
        offset_ = hz;
        radio_->set_rx_freq(freq_ + hz);
        avm_logf("RX frequency offset %+.0f kHz\n", hz / 1e3);
    }

    void close() {
        if (closed_) return;
        closed_ = true;
        gain_watch_.reset();
        offset_watch_.reset();
        stop_ = true;
        cv_.notify_all();
        if (radio_ && attached_) radio_->detach_rx();
        radio_.reset();         // (the last user closes the radio)
    }
    ~RxSource() override { close(); }

private:
    std::shared_ptr<Radio> radio_;
    double freq_ = 0, offset_ = 0;
    std::mutex mu_;
    std::condition_variable cv_;
    std::vector<std::complex<float>> q_;
    std::atomic<bool> stop_{false};
    bool got_ = false, failed_ = false, closed_ = false, attached_ = false;
    std::unique_ptr<sdr::FileWatcher> gain_watch_, offset_watch_;

    std::string gain_text() const {
        char b[64];
        if (radio_->hl2()) std::snprintf(b, sizeof b, "LNA %+d dB", radio_->lna());
        else std::snprintf(b, sizeof b, "attenuator %d dB", radio_->attenuation());
        return b;
    }
};

// ---------------------------------------------------------------- transmitting
class TxSink {
public:
    double rate = TX_RATE;
    std::string error;

    bool open(const std::string& uri, double freq_hz, double gain_db, const std::string& gain_file) {
        set_gain(gain_db);
        radio_ = Radio::get(uri, &error);
        if (!radio_) return false;
        radio_->set_drive(drive_);
        radio_->attach_tx(freq_hz);
        attached_ = true;
        long long p0 = radio_->packets();
        for (int i = 0; i < 150 && radio_->packets() == p0; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(20));
        if (radio_->packets() == p0) { error = "HPSDR " + radio_->ip + " sent nothing back -- is it on?"; close(); return false; }
        u0_ = radio_->underruns();
        avm_logf("HPSDR TX: %s at %s (protocol 1), %.4f MHz, 48 kS/s, drive %d/255\n", radio_->board.c_str(),
                 radio_->ip.c_str(), freq_hz / 1e6, drive_);
        if (!gain_file.empty()) watch_.reset(new sdr::FileWatcher(gain_file, [this](double g) {
            set_gain(g);
            avm_logf("HPSDR TX drive %d/255\n", drive_);
        }));
        return true;
    }

    void set_gain(double db) {
        double a = std::pow(10.0, std::min(0.0, db) / 20.0);
        drive_ = (int)std::lround(std::max(0.0, std::min(255.0, 255.0 * a)));
        if (radio_) radio_->set_drive(drive_);
    }

    // queued for the radio; blocks while more than TX_LEAD_S is waiting
    void write(const std::complex<float>* x, size_t n) {
        for (size_t i = 0; i < n && !closed_ && radio_->running();) {
            size_t k = radio_->tx_push(x + i, n - i);
            i += k;
            if (!k) std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }

    // false once the radio has stopped (nothing back from it)
    bool ok() const { return radio_ && radio_->running(); }

    // everything queued sent
    void drain() {
        for (int i = 0; i < 400 && radio_ && radio_->running() && radio_->tx_queued(); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    void close() {
        if (closed_) return;
        closed_ = true;
        watch_.reset();
        if (radio_ && attached_) {
            radio_->detach_tx();
            avm_logf("HPSDR TX: unkeyed, %lld underrun packet(s)\n", radio_->underruns() - u0_);
        }
        radio_.reset();         // (the last user closes the radio)
    }
    ~TxSink() { close(); }

private:
    std::shared_ptr<Radio> radio_;
    int drive_ = 0;
    bool closed_ = false, attached_ = false;
    long long u0_ = 0;
    std::unique_ptr<sdr::FileWatcher> watch_;
};

}  // namespace hpsdr1
