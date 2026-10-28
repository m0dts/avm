// Same-channel duplex (TDD) in one process: the transmitter (avm_tx --output
// tdd) hands its samples to the duplex receiver's radio, which transmits them
// on its own connection (pluto_soapy_sink.TddTxFeeder, with the FIFO between
// the two Python processes replaced by tdd::queue()).
//
// TX and RX share the radio's clock, so TX sample n is heard at RX sample
// n + a constant (the radio's buffering): a station knows where its own slot
// is without hearing itself. The feeder records each burst's start as the RX
// sample it lines up with (own_starts), never starves the radio (zeros go out,
// counted, when the modulator is late), stays silent while muted (a slave not
// yet placed in the master's gap) or while RX has stalled.
#pragma once
#include <algorithm>
#include <atomic>
#include <chrono>
#include <complex>
#include <condition_variable>
#include <deque>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>

#include "avm_log.h"

namespace tdd {

using cf = std::complex<float>;

// TX sample -> on air, in RX samples, same radio and process (measured with
// Plutos: 7 ms loopback + 11.35 ms seen between two stations)
const double TXRX_LATENCY_S = 0.0184;
const double BURST_GAP_S = 0.010;        // silence before a burst start, in the TX stream
const double GAP_REPORT_S = 0.02;        // log every TX gap (modulator late) at least this long
const double RX_STALL_MUTE_S = 1.0;      // RX count stopped this long: TX goes silent
const double MAX_AHEAD_S = 0.5;          // samples buffered from the modulator, at most
const double SLOT_IDLE_S = 0.10;         // silence this long = between slots (mute changes only then)

// the modulator's samples on their way to the radio (the Python FIFO)
class Queue {
public:
    // a transmitter attaches for its run (avm_tx --output tdd), and detaches
    void attach(double rate) {
        std::lock_guard<std::mutex> g(mu_);
        q_.clear();
        queued_ = 0;
        head_ = 0;     // (left part-way into the last session's block, a shorter new one underflowed the copy)
        max_ = (size_t)(MAX_AHEAD_S * rate);
        writer_ = true;
        writer_data_ = false;
        closed_ = false;
    }
    void detach() {
        std::lock_guard<std::mutex> g(mu_);
        writer_ = false;
        closed_ = true;
        space_.notify_all();
    }
    // blocks while MAX_AHEAD_S is waiting; false once detached
    bool push(const cf* p, size_t n) {
        std::unique_lock<std::mutex> lk(mu_);
        space_.wait(lk, [&] { return queued_ < max_ || closed_; });
        if (closed_) return false;
        q_.emplace_back(p, p + n);
        queued_ += n;
        return true;
    }
    // up to n samples (fewer only if it runs dry)
    size_t take(cf* out, size_t n) {
        std::lock_guard<std::mutex> g(mu_);
        size_t got = 0;
        while (!q_.empty() && got < n) {
            auto& f = q_.front();
            size_t k = std::min(n - got, f.size() - head_);
            std::copy(f.begin() + head_, f.begin() + head_ + k, out + got);
            got += k;
            head_ += k;
            if (head_ == f.size()) { q_.pop_front(); head_ = 0; }
        }
        queued_ -= got;
        if (got) { writer_data_ = true; space_.notify_all(); }
        return got;
    }
    // a modulator is attached and has sent something (padding then counts as late)
    bool writer_live() {
        std::lock_guard<std::mutex> g(mu_);
        return writer_ && writer_data_;
    }
private:
    std::mutex mu_;
    std::condition_variable space_;
    std::deque<std::vector<cf>> q_;
    size_t head_ = 0, queued_ = 0, max_ = 1 << 20;
    bool writer_ = false, writer_data_ = false, closed_ = true;
};

inline Queue& queue() {
    static Queue q;
    return q;
}

// transmits tdd::queue() on the receiver's radio (write: blocking, the radio's
// own TX stream), in step with its RX count (rx_count: samples read so far)
class Feeder {
public:
    static const size_t CHUNK = 4096;
    std::atomic<bool> mute_request{false};

    Feeder(double rate, std::function<bool(const cf*, size_t)> write, std::function<long long()> rx_count,
           bool start_muted, double latency_s = TXRX_LATENCY_S)
        : fs_(rate), write_(std::move(write)), rx_count_(std::move(rx_count)) {
        latency_ = (long long)std::llround(latency_s * rate);
        gap_ = (long long)std::llround(BURST_GAP_S * rate);
        muted_ = start_muted;
        mute_request = start_muted;
    }
    ~Feeder() { stop(); }

    void start() {
        th_ = std::thread([this, sink = log_sink()] { log_sink() = sink; run(); });
    }
    void stop() {
        stop_ = true;
        if (th_.joinable()) th_.join();
    }
    // own bursts so far, and the RX sample the latest lines up with
    long long bursts() const { return n_bursts_; }
    long long last_start() {
        std::lock_guard<std::mutex> g(mu_);
        return last_start_;
    }

private:
    double fs_;
    std::function<bool(const cf*, size_t)> write_;
    std::function<long long()> rx_count_;
    long long latency_ = 0, gap_ = 0;
    long long n_tx_ = 0, n_written_ = 0, n_padded_ = 0;
    long long last_nz_ = -(1LL << 50);
    std::atomic<long long> n_bursts_{0};
    long long last_start_ = 0;
    std::mutex mu_;
    bool muted_ = false, rx_stalled_ = false;
    long long rc_last_ = -1;
    std::chrono::steady_clock::time_point rc_t_ = std::chrono::steady_clock::now();
    std::atomic<bool> stop_{false};
    std::thread th_;
    // the lead (TX written - RX read): a measure of hardware underruns, reported only
    std::chrono::steady_clock::time_point win_t_;
    bool win_ = false;
    long long win_min_ = 0, slip_ = 0;
    double lead0_ = 0;
    bool have_lead0_ = false;
    std::deque<long long> win_mins_;

    static double secs(std::chrono::steady_clock::duration d) { return std::chrono::duration<double>(d).count(); }

    // each burst start (silence longer than gap_ before it), as the RX sample it lines up with
    void note_bursts(const cf* c, size_t n) {
        for (size_t i = 0; i < n; ++i) {
            if (c[i] == cf(0, 0)) continue;
            long long g = n_tx_ + (long long)i;
            if (g - last_nz_ > gap_) {
                std::lock_guard<std::mutex> lk(mu_);
                last_start_ = g + latency_;
                ++n_bursts_;
            }
            last_nz_ = g;
        }
    }

    void track_lead() {
        const double WINDOW_S = 1.0, STEADY_S = 0.003, REPORT_S = 0.002;
        const size_t SETTLE = 3, MEDIAN_OF = 5;
        long long lead = n_written_ - rx_count_();
        auto now = std::chrono::steady_clock::now();
        if (!win_) { win_ = true; win_t_ = now; win_min_ = lead; return; }
        win_min_ = std::min(win_min_, lead);
        if (secs(now - win_t_) < WINDOW_S) return;
        win_mins_.push_back(win_min_);
        if (win_mins_.size() > 64) win_mins_.pop_front();
        win_t_ = now;
        win_min_ = lead;
        if (win_mins_.size() < SETTLE + MEDIAN_OF) return;
        std::vector<long long> recent(win_mins_.end() - MEDIAN_OF, win_mins_.end());
        std::vector<long long> s = recent;
        std::sort(s.begin(), s.end());
        double current = (double)s[MEDIAN_OF / 2];
        if (!have_lead0_) {
            if (s.back() - s.front() > STEADY_S * fs_) return;   // only once steady
            lead0_ = current;
            have_lead0_ = true;
            avm_logf("[tdd] TX lead over RX: %.1f ms (baseline)\n", current / fs_ * 1e3);
            return;
        }
        long long slip = std::max(0LL, (long long)std::llround(lead0_ - current));
        if (std::llabs(slip - slip_) >= REPORT_S * fs_)
            avm_logf("[tdd] TX lead %.1f ms below its baseline (possible TX underrun; slot timing NOT changed)\n",
                     slip / fs_ * 1e3);
        slip_ = slip;
    }

    void run() {
        std::vector<cf> chunk(CHUNK), zeros(CHUNK, cf(0, 0));
        long long gap_run = 0;
        bool started = false;
        Queue& q = queue();
        while (!stop_) {
            size_t got = q.take(chunk.data(), CHUNK);
            size_t padded = 0;
            if (got) {
                started = true;
                if (got < CHUNK) {          // ran dry: pad the rest (counted)
                    padded = CHUNK - got;
                    std::fill(chunk.begin() + got, chunk.end(), cf(0, 0));
                }
            } else {
                std::fill(chunk.begin(), chunk.end(), cf(0, 0));
                if (started && q.writer_live()) padded = CHUNK;   // (no modulator, or starting: expected)
            }
            if (padded) {
                n_padded_ += padded;
                gap_run += padded;
            } else if (gap_run) {
                if (gap_run >= GAP_REPORT_S * fs_)
                    avm_logf("[tdd] TX gap: %.0f ms of unplanned silence (modulator samples late; %.1f s in all)\n",
                             gap_run / fs_ * 1e3, n_padded_ / fs_);
                gap_run = 0;
            }
            // padding is not the gap between bursts: a late chunk mid-slot mustn't read as a new burst
            if (padded) {
                if (padded < CHUNK) note_bursts(chunk.data(), CHUNK - padded);
                last_nz_ = n_tx_ + (long long)CHUNK - 1;
            } else {
                note_bursts(chunk.data(), CHUNK);
            }
            // RX stalled (its count stopped): our slot timing is unknown -- never transmit blind
            long long rc = rx_count_();
            auto now = std::chrono::steady_clock::now();
            if (rc != rc_last_) {
                rc_last_ = rc;
                rc_t_ = now;
                if (rx_stalled_) { rx_stalled_ = false; avm_logf("[tdd] RX samples flowing again\n"); }
            } else if (secs(now - rc_t_) > RX_STALL_MUTE_S && !rx_stalled_) {
                rx_stalled_ = true;
                avm_logf("[tdd] RX stalled (%g s without samples): TX silent\n", RX_STALL_MUTE_S);
            }
            // a mute/unmute takes effect between slots only: never a partial burst
            bool silent = !padded && std::all_of(chunk.begin(), chunk.end(), [](const cf& v) { return v == cf(0, 0); });
            bool idle = silent && n_tx_ - last_nz_ > SLOT_IDLE_S * fs_;
            if (mute_request != muted_ && idle) muted_ = mute_request;
            const cf* out = muted_ || rx_stalled_ ? zeros.data() : chunk.data();
            if (!write_(out, CHUNK)) avm_logf("[tdd] TX write error\n");
            n_tx_ += CHUNK;
            n_written_ += CHUNK;
            track_lead();
        }
    }
};

}  // namespace tdd
