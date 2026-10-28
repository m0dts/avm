// tx_engine.cpp -- AVM's transmitter as a library engine: hf_ofdm_tx.py
// --stream (run_stream / transmit_fragment) on the C++ modem, behind the C
// API in tx_engine.h. avm_tx (the program) and the GUI both run it.
//
// Input: the data, pushed by the owner (avm_tx_push; each --fragment-size
// bytes, after an 8-byte fragment header, is one frame). Output: complex64
// IQ at --sample-rate (default: the mode's own rate) to the data callback,
// or a SoapySDR radio opened here (--output pluto --sdr pluto|lime ...:
// sdr.h, the waveform shifted down by the LO offset). Status lines (the
// Python's: "Streamed fragment seq=.. (.. bytes, .. ms)") to the log
// callback. Same waveform as hf_ofdm_tx.py to float rounding
// (c++/test_avm_tx.py).
//
// As the Python: --tx-warmup-s of inert filler fragments first,
// --max-input-backlog (newest N input fragments kept; standing backlog
// trimmed), --fragment-gap-ms of silence after each fragment -- or, with no
// gap and a resampled OFDM output, fragments joined by a raised-cosine
// crossfade over a cyclic prefix -- --papr-db peak reduction, RMS
// (per-fragment) or --tx-normalize fixed peak normalisation, --amplitude.
// Not here yet (the Python keeps them): the virtual radio, duplex (--tdd-*),
// --dpd-file, --amplitude-sweep, the non-stream (whole message) mode.
#include "modem_rx.cpp"      // (#pragma once: the library has both engines in one unit)
#include "sdr.h"
#include "virtual_radio.h"
#include "hpsdr1.h"
#include "tdd.h"
#include "avm_log.h"
#include "tx_engine.h"
#include <atomic>
#include <functional>

#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <random>
#include <string>
#include <thread>


namespace {

#include "avm_app.h"

namespace txe {     // apart from the receiver's names (rx_engine.cpp) in the library

const int FRAG_HEADER_SIZE = 8;
const double TX_CUT_SPACINGS = 2.0, TX_TAPER_SPACINGS = 1.5;

double mono() { return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count(); }

struct Args {
    std::string mode = "B", modulation = "qpsk", fec = "viterbi", normalize = "per-fragment";
    double occupancy = 20, sample_rate = 0, amplitude = 0.1, papr_db = 0, fragment_gap_ms = 300, warmup_s = 2.0;
    int fragment_size = 0, max_input_backlog = 0;
    // --output pluto: a radio through SoapySDR (sdr.h), hf_ofdm_tx.py's radio options
    std::string output = "pipe", sdr = "pluto", uri, antenna;
    double rf_freq = 0, tx_gain = -10, bandwidth = 0, lo_offset_hz = 0;
    int tx_queue_depth = 8;
    std::string gain_file;
    bool stream = false;
    // --tdd-cycle-s: same-channel duplex (hf_ofdm_tx.py run_tdd)
    double tdd_cycle_s = 0, tdd_slot_s = 0, tdd_offset_s = 0;
    int tdd_station = 0;
    std::string tdd_report_file, tdd_adjust_file;
};

// the data in (avm_tx_push), read as a stream: read() blocks until n bytes
// or the end, as fread on a pipe
struct ByteInput {
    std::mutex mu;
    std::condition_variable cv;
    std::deque<char> q;
    bool end = false;
    void push(const void* p, size_t n) {
        std::lock_guard<std::mutex> g(mu);
        if (end) return;
        q.insert(q.end(), (const char*)p, (const char*)p + n);
        cv.notify_all();
    }
    void close() {
        std::lock_guard<std::mutex> g(mu);
        end = true;
        cv.notify_all();
    }
    size_t read(char* out, size_t n) {
        std::unique_lock<std::mutex> lk(mu);
        cv.wait(lk, [&] { return q.size() >= n || end; });
        size_t k = std::min(n, q.size());
        std::copy(q.begin(), q.begin() + k, out);
        q.erase(q.begin(), q.begin() + k);
        return k;
    }
};

// --max-input-backlog (_LiveInput): the input read on its own thread, only
// the newest `cap` fragments kept
struct LiveInput {
    size_t frag, cap;
    std::deque<std::string> buf;
    std::mutex mu;
    std::condition_variable cv;
    bool eof = false;
    long long dropped = 0;
    ByteInput& in;
    std::thread th;
    LiveInput(size_t f, size_t c, ByteInput& in_) : frag(f), cap(c), in(in_) {
        th = std::thread([this, sink = log_sink()] { log_sink() = sink; reader(); });
    }
    ~LiveInput() { in.close(); if (th.joinable()) th.join(); }
    void reader() {
        while (true) {
            std::string d(frag, '\0');
            size_t got = in.read(&d[0], frag);
            d.resize(got);
            std::lock_guard<std::mutex> g(mu);
            if (!got) { eof = true; cv.notify_all(); return; }
            buf.push_back(std::move(d));
            while (buf.size() > cap) {
                buf.pop_front();
                ++dropped;
                if (dropped == 1 || dropped % 20 == 0)
                    avm_logf("[latency cap] dropped a stale input fragment (%lld so far)\n", dropped);
            }
            cv.notify_all();
        }
    }
    void end_warmup(size_t c, size_t keep = 0) {
        {
            std::lock_guard<std::mutex> g(mu);
            while (buf.size() > keep) { buf.pop_front(); ++dropped; }
            cap = c;
        }
        avm_logf("[latency cap] discarded the warmup backlog; %lld stale fragment(s) dropped so far\n", dropped);
    }
    // the oldest waiting fragment, or false if there's none (TDD: an empty
    // slot is filled with filler, never waited for)
    bool take_nowait(std::string& out) {
        std::lock_guard<std::mutex> g(mu);
        if (buf.empty()) return false;
        out = std::move(buf.front());
        buf.pop_front();
        return true;
    }
    bool ended() {
        std::lock_guard<std::mutex> g(mu);
        return eof && buf.empty();
    }
    // drain(): the next fragment, false at the end
    const size_t STANDING_MIN = 2;
    const double STANDING_S = 3.0, REPORT_S = 10.0;
    double standing_since = -1, last_report = mono();
    size_t peak = 0;
    bool next(std::string& out) {
        std::unique_lock<std::mutex> lk(mu);
        cv.wait(lk, [&] { return !buf.empty() || eof; });
        if (buf.empty()) return false;
        double now = mono();
        size_t waiting = buf.size();
        peak = std::max(peak, waiting);
        if (waiting >= STANDING_MIN) {
            if (standing_since < 0) standing_since = now;
            else if (now - standing_since >= STANDING_S) {
                while (buf.size() > STANDING_MIN - 1) { buf.pop_front(); ++dropped; }
                standing_since = -1;
                avm_logf("[latency cap] %zu fragments had been waiting %g+ s: dropped %zu (%lld dropped so far)\n",
                             waiting, STANDING_S, waiting - buf.size(), dropped);
            }
        } else {
            standing_since = -1;
        }
        if (now - last_report >= REPORT_S) {
            avm_logf("[latency] input backlog %zu now, peak %zu in the last %g s\n", waiting, peak, REPORT_S);
            last_report = now;
            peak = 0;
        }
        out = std::move(buf.front());
        buf.pop_front();
        return true;
    }
};

struct Transmitter {
    Args a;
    Modem md;
    std::vector<cf> carry;
    bool has_carry = false;
    std::vector<float> ramp;
    long long frag_idx = 0;
    bool overlap_tail;
    double fixed_peak = 0;          // --tx-normalize fixed: the calibrated peak (0: per-fragment)
    long long clipped = 0;

    ByteInput& in;
    std::atomic<bool>& stopping;
    std::function<void(const cf*, size_t)> out;
    Transmitter(const Args& args, const Mode& m, ByteInput& in_, std::atomic<bool>& stop_,
                std::function<void(const cf*, size_t)> out_)
        : a(args), md(m), in(in_), stopping(stop_), out(std::move(out_)) {
        overlap_tail = a.fragment_gap_ms == 0 && !m.sc && !a.tdd_cycle_s;   // (duplex keeps the gap: its slot timing counts it)
    }
    const Mode& m() const { return md.m; }

    // hf_ofdm_common.build_frame -> complex128 (OFDM) / complex64 (S, SH)
    std::vector<cd> build(const std::string& payload) {
        const uint8_t* p = (const uint8_t*)payload.data();
        if (m().sc) {
            md.sc->build(p, payload.size());
            std::vector<cd> w(md.sc->frame.size());
            for (size_t i = 0; i < w.size(); ++i) w[i] = cd(md.sc->frame[i].real(), md.sc->frame[i].imag());
            return w;
        }
        build_frame(*md.c, p, payload.size());
        return md.c->frame;
    }
    // tx_spectrum_cut
    void spectrum_cut(double& cut, double& taper) const {
        if (m().sc) {
            double edge = m().symbol_rate * (1 + SC_ALPHA) / 2;
            cut = edge * 1.04;
            taper = m().symbol_rate * 0.04;
            return;
        }
        int hb = 0;
        for (int b : m().data_bins) hb = std::max(hb, std::abs(b));
        double last = (double)hb * m().spacing;
        cut = last + TX_CUT_SPACINGS * m().spacing;
        taper = TX_TAPER_SPACINGS * m().spacing;
    }

    // calibrate_fixed_peak: the worst peak of random-payload frames, +5%
    void calibrate(int trials = 200) {
        std::mt19937_64 rng(std::random_device{}());
        double worst = 0;
        for (int t = 0; t < trials; ++t) {
            std::string p(FRAG_HEADER_SIZE + a.fragment_size, '\0');
            for (size_t i = FRAG_HEADER_SIZE; i < p.size(); ++i) p[i] = (char)(rng() & 0xFF);
            for (const cd& v : build(p)) worst = std::max(worst, std::abs(v));
        }
        fixed_peak = worst * 1.05;
        avm_logf("Fixed peak reference: %.4f\n", fixed_peak);
    }

    // the radio (--output pluto), and the LO pre-shift's table and position
    sdr::TxSink* sink = nullptr;
    vradio::TxSink* vsink = nullptr;     // --sdr virtual
    hpsdr1::TxSink* hsink = nullptr;     // --sdr hpsdr (protocol 1: Hermes-Lite 2, Radioberry)
    bool tdd_out = false;                // --output tdd
    bool radio_lost = false;
    std::vector<cd> lo_table;
    bool lo_exact = true;
    long long lo_n = 0;
    double out_rate = 0;
    // to the radio: shifted down by the LO offset (the LO is tuned up by it),
    // the phase continuing across fragments, as PlutoTxSink._lo_phasor
    void emit(const std::vector<cf>& w) {
        if (tdd_out) {      // duplex: to the receiver's radio (tdd.h)
            if (!tdd::queue().push(w.data(), w.size())) stopping = true;
            return;
        }
        if (vsink) { vsink->write(w.data(), w.size()); return; }
        if (hsink) {
            std::vector<std::complex<float>> o(w.begin(), w.end());
            hsink->write(o.data(), o.size());
            if (!hsink->ok() && !radio_lost) {     // the radio stopped: TX ends (see start())
                radio_lost = true;
                avm_logf("ERROR: the radio stopped responding -- TX stopped\n");
                stopping = true;
            }
            return;
        }
        if (!sink) { write_out(w); return; }
        std::vector<std::complex<float>> o(w.begin(), w.end());
        if (a.lo_offset_hz) {
            for (size_t i = 0; i < o.size(); ++i, ++lo_n) {
                cd ph;
                if (lo_exact) ph = lo_table[lo_n % (long long)lo_table.size()];
                else { double th = -2 * fft::PI * a.lo_offset_hz * (double)lo_n / out_rate; ph = cd(std::cos(th), std::sin(th)); }
                o[i] = std::complex<float>((float)(o[i].real() * ph.real() - o[i].imag() * ph.imag()),
                                           (float)(o[i].real() * ph.imag() + o[i].imag() * ph.real()));
            }
        }
        sink->write(std::move(o));
    }
    void write_out(const std::vector<cf>& w) { if (out) out(w.data(), w.size()); }

    // transmit_fragment: its length on air. total_field / len_field: FRAG_HEADER's
    // total_frags / total_len, unused (0) in stream mode -- TDD puts the slot
    // position there, and the master its report of where it hears the slave
    size_t transmit(const std::string& data, const char* tag, unsigned total_field = 0, uint32_t len_field = 0) {
        std::string payload(FRAG_HEADER_SIZE, '\0');
        unsigned idx = (unsigned)(frag_idx % 65536);
        payload[0] = (char)(idx >> 8);
        payload[1] = (char)(idx & 0xFF);
        payload[2] = (char)((total_field >> 8) & 0xFF);
        payload[3] = (char)(total_field & 0xFF);
        for (int i = 0; i < 4; ++i) payload[4 + i] = (char)((len_field >> (24 - 8 * i)) & 0xFF);
        payload += data;
        std::vector<cd> w = build(payload);
        if (a.papr_db && !m().sc) {
            md.c->frame = w;
            reduce_papr(*md.c, a.papr_db, 3);
            w = md.c->frame;
        }
        double out_fs = m().fs;
        long long n_native = (long long)w.size();
        bool resample = a.sample_rate && a.sample_rate != m().fs;
        bool joined = overlap_tail && resample;
        if (joined)   // the last symbol continued for one cyclic prefix
            for (int i = 0; i < m().n_cp; ++i) w.push_back(w[n_native - m().n_fft + i]);
        std::vector<cf> o;
        bool single = m().sc != 0;   // the waveform is complex64 already (S/SH), or after resampling
        if (resample) {
            std::vector<cf> x(w.size());
            for (size_t i = 0; i < w.size(); ++i) x[i] = cf((float)w[i].real(), (float)w[i].imag());
            double cut, taper;
            spectrum_cut(cut, taper);
            o.resize(tx_resample_len((long long)x.size(), m().fs, a.sample_rate));
            tx_resample(x.data(), (long long)x.size(), m().fs, a.sample_rate, cut, taper, o.data());
            out_fs = a.sample_rate;
            single = true;
        }
        // normalisation: RMS to --amplitude (or a fixed peak reference)
        std::vector<cd> wd;
        if (!resample) wd = w;
        auto n_samples = [&]() { return resample ? o.size() : wd.size(); };
        if (fixed_peak > 0) {
            double peak = 0;
            if (resample) { for (cf& v : o) { v /= (float)fixed_peak; peak = std::max(peak, (double)std::abs(v)); } }
            else { for (cd& v : wd) { v /= fixed_peak; peak = std::max(peak, std::abs(v)); } }
            if (peak > 1.0) {
                ++clipped;
                auto clip = [](double x) { return std::min(1.0, std::max(-1.0, x)); };
                if (resample) for (cf& v : o) v = cf((float)clip(v.real()), (float)clip(v.imag()));
                else for (cd& v : wd) v = cd(clip(v.real()), clip(v.imag()));
                avm_logf("WARNING: fragment seq=%u exceeded the fixed peak reference (%.3fx) -- hard-clipped (%lld total this session).\n",
                             idx, peak, clipped);
            }
            if (a.amplitude != 1.0) {
                if (resample) for (cf& v : o) v *= (float)a.amplitude;
                else for (cd& v : wd) v *= a.amplitude;
            }
        } else {
            double e = 0;
            if (resample) for (const cf& v : o) e += (double)v.real() * v.real() + (double)v.imag() * v.imag();
            else for (const cd& v : wd) e += v.real() * v.real() + v.imag() * v.imag();
            double rms = std::sqrt(e / (double)std::max<size_t>(n_samples(), 1));
            if (single) {
                // single precision, as numpy: rms and the scale are float32
                float rf = (float)rms;
                float sc = rf > 0 ? (float)(a.amplitude / rf) : (float)a.amplitude;
                if (sc != 1.0f) {
                    if (resample) for (cf& v : o) v *= sc;
                    else for (cd& v : wd) v = cd((float)((float)v.real() * sc), (float)((float)v.imag() * sc));
                }
            } else {
                double sc = rms > 0 ? a.amplitude / rms : a.amplitude;
                if (sc != 1.0) for (cd& v : wd) v *= sc;
            }
        }
        if (!resample) {
            o.resize(wd.size());
            for (size_t i = 0; i < wd.size(); ++i) o[i] = cf((float)wd[i].real(), (float)wd[i].imag());
        }
        if (joined) {
            long long n_keep = (long long)std::nearbyint(n_native * out_fs / m().fs);
            long long n_fade = (long long)std::nearbyint(m().n_cp * out_fs / m().fs);
            if ((long long)ramp.size() != n_fade) {
                ramp.resize(n_fade);
                for (long long i = 0; i < n_fade; ++i)
                    ramp[i] = (float)(0.5 - 0.5 * std::cos(fft::PI * (i + 0.5) / n_fade));
            }
            std::vector<cf> tail(n_fade);
            for (long long i = 0; i < n_fade; ++i) tail[i] = o[n_keep + i] * ramp[n_fade - 1 - i];
            o.resize(n_keep);
            for (long long i = 0; i < n_fade; ++i) o[i] *= ramp[i];
            if (has_carry) for (long long i = 0; i < n_fade; ++i) o[i] += carry[i];
            carry = tail;
            has_carry = true;
        }
        if (a.fragment_gap_ms > 0) {
            long long gap = (long long)std::nearbyint(out_fs * a.fragment_gap_ms / 1000);
            if (gap > 0) o.resize(o.size() + gap, cf(0, 0));
        }
        emit(o);
        avm_logf("%sStreamed fragment seq=%u (%zu bytes, %.1f ms)\n", tag, idx, data.size(),
                     o.size() / out_fs * 1000);
        ++frag_idx;
        return o.size();
    }

    // ---- TDD (hf_ofdm_tx.py run_tdd): every cycle EXACTLY round(cycle_s * fs)
    // samples on air -- as many whole fragments as fit in the slot (real input
    // if waiting, else filler: the radio's timeline must never stall), then
    // silence. Timing is the sample count itself (TX and RX share the radio's
    // clock). --tdd-offset-s delays the first slot; --tdd-adjust-file (the
    // slave): the receiver's total wanted shift, applied as longer/shorter silence.
    static const unsigned TDD_HEADER_FLAG = 0x8000, TDD_STATION_BIT = 0x4000;
    static const uint32_t TDD_REPORT_FLAG = 0x80000000u;
    static unsigned tdd_header_field(int pos, int per_slot, int station) {
        return TDD_HEADER_FLAG | (station ? TDD_STATION_BIT : 0) | (unsigned)std::min(pos, 0x3F) << 8 |
               (unsigned)std::min(per_slot, 0xFF);
    }
    // the master receiver's latest report as the header field, 0 if none
    static uint32_t read_tdd_report(const std::string& path) {
        FILE* f = std::fopen(path.c_str(), "rb");
        if (!f) return 0;
        long ctr = 0, err = 0;
        int n = std::fscanf(f, "%ld %ld", &ctr, &err);
        std::fclose(f);
        if (n != 2) return 0;
        err = std::max(-32768L, std::min(32767L, err));
        return TDD_REPORT_FLAG | (uint32_t)(ctr & 0xFF) << 16 | ((uint32_t)err & 0xFFFF);
    }
    // the total slot shift (seconds) asked for so far; false if not there (yet) or mid-write
    static bool read_tdd_adjust(const std::string& path, double& v) {
        FILE* f = std::fopen(path.c_str(), "rb");
        if (!f) return false;
        int n = std::fscanf(f, "%lf", &v);
        std::fclose(f);
        return n == 1;
    }
    void run_tdd(LiveInput* backlog) {
        double out_fs = a.sample_rate ? a.sample_rate : m().fs;
        long long cycle = std::llround(a.tdd_cycle_s * out_fs), slot = std::llround(a.tdd_slot_s * out_fs);
        std::string filler(a.fragment_size, '\0');
        // one fragment on air, as transmit() builds it (resample padding and any gap included)
        long long n_frag = (long long)build(std::string(FRAG_HEADER_SIZE, '\0') + filler).size();
        if (out_fs != m().fs) n_frag = tx_resample_len(n_frag, m().fs, out_fs);
        if (a.fragment_gap_ms > 0) n_frag += std::llround(out_fs * a.fragment_gap_ms / 1000);
        int per_slot = (int)(slot / n_frag);
        if (per_slot < 1) {
            avm_logf("ERROR: --tdd-slot-s %g is shorter than one fragment (%.1f ms): use a longer slot or smaller fragments\n",
                     a.tdd_slot_s, n_frag / out_fs * 1000);
            return;
        }
        if (per_slot * n_frag > cycle) { avm_logf("ERROR: --tdd-slot-s must not be longer than --tdd-cycle-s\n"); return; }
        avm_logf("[tdd] cycle %.1f ms: %d fragment(s) (%.1f ms) then %.1f ms silent; framer period for this: "
                 "--fragment-period-s %.4f\n", cycle / out_fs * 1000, per_slot, per_slot * n_frag / out_fs * 1000,
                 (cycle - per_slot * n_frag) / out_fs * 1000, a.tdd_cycle_s / per_slot);
        std::vector<cf> zeros;
        auto silence = [&](long long n) {
            if (n <= 0) return;
            zeros.assign((size_t)n, cf(0, 0));
            emit(zeros);
        };
        if (a.tdd_offset_s) silence(std::llround(a.tdd_offset_s * out_fs) % cycle);
        long long applied = 0, n_cycles = 0, n_real = 0, n_filler = 0;
        long long warmup_cycles = a.warmup_s > 0 ? (long long)std::ceil(a.warmup_s / a.tdd_cycle_s) : 0;
        if (warmup_cycles) avm_logf("[tdd] warmup: %lld cycle(s) of slotted filler before real input\n", warmup_cycles);
        std::string data;
        while (!stopping) {
            if (n_cycles == warmup_cycles && backlog) backlog->end_warmup(a.max_input_backlog);
            long long written = 0;
            uint32_t report = a.tdd_report_file.empty() ? 0 : read_tdd_report(a.tdd_report_file);
            for (int pos = 0; pos < per_slot && !stopping; ++pos) {
                bool real = n_cycles >= warmup_cycles && backlog && backlog->take_nowait(data);
                if (!real) {
                    if (backlog && backlog->ended()) {
                        avm_logf("[tdd] input ended after %lld cycles\n", n_cycles);
                        return;
                    }
                    ++n_filler;
                } else {
                    ++n_real;
                }
                written += (long long)transmit(real ? data : filler, real ? "" : "[filler] ",
                                               tdd_header_field(pos, per_slot, a.tdd_station), report);
            }
            long long quiet = cycle - written;
            if (!a.tdd_adjust_file.empty()) {
                double want;
                if (read_tdd_adjust(a.tdd_adjust_file, want)) {
                    // longer silence delays the next slot, shorter brings it forward
                    long long delta = std::llround(want * out_fs) - applied;
                    delta = std::max(-quiet, std::min(cycle, delta));
                    if (delta) {
                        quiet += delta;
                        applied += delta;
                        avm_logf("[tdd] slot moved %+.2f ms (total %+.2f ms)\n", delta / out_fs * 1e3, applied / out_fs * 1e3);
                    }
                }
            }
            if (quiet < 0) avm_logf("[tdd] WARNING: slot overran the cycle by %lld samples\n", -quiet);
            else silence(quiet);
            ++n_cycles;
            if (n_cycles % std::max(1LL, (long long)(10 / a.tdd_cycle_s)) == 0)
                avm_logf("[tdd] %lld cycles: %lld real, %lld filler fragment(s)\n", n_cycles, n_real, n_filler);
        }
    }

    void run_stream() {
        std::unique_ptr<LiveInput> backlog;
        if (a.max_input_backlog) backlog.reset(new LiveInput(a.fragment_size, 1, in));
        if (a.tdd_cycle_s) {     // (the warmup is slotted too, so the far end locks onto the slot timing)
            run_tdd(backlog.get());
            return;
        }
        if (a.warmup_s > 0) {
            std::string filler(a.fragment_size, '\0');
            double deadline = mono() + a.warmup_s;
            long long n = 0;
            avm_logf("Warmup: transmitting ~%.1fs of on-air filler before consuming real input (see --tx-warmup-s) -- covers the transmit chain's own settling time.\n",
                         a.warmup_s);
            while (mono() < deadline && !stopping) { transmit(filler, "[warmup] "); ++n; }
            avm_logf("Warmup done (%lld filler fragment(s)) -- now sending real input.\n", n);
        }
        std::string data;
        if (!backlog) {
            while (!stopping) {
                data.assign(a.fragment_size, '\0');
                size_t got = in.read(&data[0], a.fragment_size);
                if (!got) break;
                data.resize(got);
                transmit(data, "");
            }
        } else {
            backlog->end_warmup(a.max_input_backlog);
            while (!stopping && backlog->next(data)) transmit(data, "");
            if (backlog->dropped)
                avm_logf("Dropped %lld stale input fragment(s) in total (--max-input-backlog).\n", backlog->dropped);
        }
        if (stopping) return;
        if (has_carry) {
            std::vector<cf> end = carry;
            end.resize(carry.size() * 5, cf(0, 0));
            emit(end);
        }
        avm_logf("Stream ended: sent %lld fragment(s).\n", frag_idx);
    }
};

bool parse_args(int argc, const char* const* argv, Args& a) {
    for (int i = 0; i < argc; ++i) {
        std::string k = argv[i];
        bool missing = false;
        auto val = [&]() -> std::string {
            if (i + 1 >= argc) { missing = true; return ""; }
            return argv[++i];
        };
        if (k == "--mode") a.mode = val();
        else if (k == "--occupancy") a.occupancy = std::atof(val().c_str());
        else if (k == "--modulation") a.modulation = val();
        else if (k == "--fec") a.fec = val();
        else if (k == "--sample-rate") a.sample_rate = std::atof(val().c_str());
        else if (k == "--fragment-size") a.fragment_size = std::atoi(val().c_str());
        else if (k == "--fragment-gap-ms") a.fragment_gap_ms = std::atof(val().c_str());
        else if (k == "--amplitude") a.amplitude = std::atof(val().c_str());
        else if (k == "--papr-db") a.papr_db = std::atof(val().c_str());
        else if (k == "--tx-warmup-s") a.warmup_s = std::atof(val().c_str());
        else if (k == "--max-input-backlog") a.max_input_backlog = std::atoi(val().c_str());
        else if (k == "--tx-normalize") a.normalize = val();
        else if (k == "--stream") a.stream = true;
        else if (k == "--output") a.output = val();
        else if (k == "--sdr") a.sdr = val();
        else if (k == "--rf-freq") a.rf_freq = std::atof(val().c_str());
        else if (k == "--tx-gain") a.tx_gain = std::atof(val().c_str());
        else if (k == "--pluto-uri") a.uri = val();
        else if (k == "--pluto-bandwidth") a.bandwidth = std::atof(val().c_str());
        else if (k == "--sdr-antenna") a.antenna = val();
        else if (k == "--tx-queue-depth") a.tx_queue_depth = std::atoi(val().c_str());
        else if (k == "--gain-file") a.gain_file = val();
        else if (k == "--lo-offset-hz") a.lo_offset_hz = std::atof(val().c_str());
        else if (k == "--tdd-cycle-s") a.tdd_cycle_s = std::atof(val().c_str());
        else if (k == "--tdd-slot-s") a.tdd_slot_s = std::atof(val().c_str());
        else if (k == "--tdd-offset-s") a.tdd_offset_s = std::atof(val().c_str());
        else if (k == "--tdd-station") a.tdd_station = std::atoi(val().c_str());
        else if (k == "--tdd-report-file") a.tdd_report_file = val();
        else if (k == "--tdd-adjust-file") a.tdd_adjust_file = val();
        else { avm_logf("avm_tx: unsupported option %s (the Python transmitter has it)\n", k.c_str()); return false; }
        if (missing) { avm_logf("%s needs a value\n", k.c_str()); return false; }
    }
    if (!a.stream || !a.fragment_size) {
        avm_logf("avm_tx: needs --stream and --fragment-size (the Python has the whole-message mode)\n");
        return false;
    }
    if (a.tdd_cycle_s) {
        if (!a.max_input_backlog) { avm_logf("--tdd-cycle-s needs --stream and --max-input-backlog\n"); return false; }
        if (!a.tdd_slot_s) a.tdd_slot_s = a.tdd_cycle_s / 2;
    }
    if (a.output == "tdd" && !a.tdd_cycle_s) { avm_logf("--output tdd needs --tdd-cycle-s\n"); return false; }
    return true;
}

// one transmitter: its options, input, radio, and thread
struct Engine {
    avm_tx_callbacks cb{};
    LogSink sink;
    Args a;
    Mode m;
    ByteInput in;
    std::unique_ptr<sdr::TxSink> radio;
    std::unique_ptr<vradio::TxSink> vradio_;
    std::unique_ptr<hpsdr1::TxSink> hradio_;
    std::thread th;
    std::atomic<bool> stopping{false}, running{false};

    bool open(int argc, const char* const* argv) {
        if (!parse_args(argc, argv, a)) return false;
        std::string err;
        if (!build_mode(a.mode, a.occupancy, a.modulation, a.fec, m, err)) {
            avm_logf("ERROR: %s\n", err.c_str());
            return false;
        }
        avm_logf("Mode %s: %.1f Hz sample rate, %s, %s (C++ transmitter)\n", m.mode.c_str(), m.fs,
                 m.modulation.c_str(), m.ldpc ? "LDPC" : "Viterbi");
        if (a.output == "tdd") {      // duplex: the receiver's radio transmits (tdd.h)
            if (!a.sample_rate) { avm_logf("ERROR: --output tdd needs --sample-rate (the radio's)\n"); return false; }
            tdd::queue().attach(a.sample_rate);
            avm_logf("Duplex TX: %d %s, to the receiver's radio\n", a.tdd_station, a.tdd_station ? "(slave)" : "(master)");
            return true;
        }
        if (a.output != "pipe") {
            if (!a.rf_freq) { avm_logf("ERROR: --output pluto requires --rf-freq.\n"); return false; }
            if (a.sdr == "virtual") {   // virtual_radio.py's: UDP to the virtual receiver on this machine
                vradio_.reset(new vradio::TxSink(a.rf_freq, a.sample_rate ? a.sample_rate : m.fs));
                return true;
            }
            if (a.sdr == "hpsdr") {     // protocol 1 over the network: its TX IQ is always 48 kS/s
                hradio_.reset(new hpsdr1::TxSink);
                if (!hradio_->open(a.uri, a.rf_freq, a.tx_gain, a.gain_file)) {
                    avm_logf("ERROR: %s\n", hradio_->error.c_str());
                    hradio_.reset();
                    return false;
                }
                a.sample_rate = hradio_->rate;
                a.lo_offset_hz = 0;
                return true;
            }
            sdr::TxConfig tc;
            tc.driver = a.sdr; tc.uri = a.uri; tc.antenna = a.antenna; tc.rf_hz = a.rf_freq;
            tc.rate = a.sample_rate ? a.sample_rate : m.fs; tc.gain_db = a.tx_gain; tc.bandwidth_hz = a.bandwidth;
            tc.tune_offset_hz = a.lo_offset_hz; tc.queue_depth = (size_t)std::max(1, a.tx_queue_depth);
            tc.gain_file = a.gain_file;
            radio.reset(new sdr::TxSink);
            if (!radio->open(tc)) { avm_logf("ERROR: %s\n", radio->error.c_str()); radio.reset(); return false; }
            if (radio->rate != tc.rate) a.sample_rate = radio->rate;   // build the waveform at the rate it really runs at
        }
        return true;
    }

    void start() {
        running = true;
        th = std::thread([this] {
            log_sink() = sink;
            Transmitter tx(a, m, in, stopping, [this](const cf* p, size_t n) {
                if (cb.data) cb.data(cb.user, (const unsigned char*)p, n * sizeof(cf));
            });
            tx.vsink = vradio_.get();
            tx.hsink = hradio_.get();
            tx.tdd_out = a.output == "tdd";
            if (radio) {
                tx.sink = radio.get();
                tx.out_rate = a.sample_rate ? a.sample_rate : m.fs;
                tx.lo_exact = lo_phasor_table(-a.lo_offset_hz, tx.out_rate, tx.lo_table);
            }
            if (a.normalize == "fixed") {
                avm_logf("Calibrating a fixed peak reference (--tx-normalize fixed)...\n");
                tx.calibrate();
            }
            tx.run_stream();
            if (radio && !stopping) radio->drain();
            if (hradio_ && !stopping) hradio_->drain();
            running = false;
            if (cb.done && (!stopping || tx.radio_lost)) cb.done(cb.user);
        });
    }

    void stop() {
        stopping = true;
        if (a.output == "tdd") tdd::queue().detach();     // (a push waiting for room returns)
        in.close();
        if (th.joinable()) th.join();
        radio.reset();
        vradio_.reset();
        hradio_.reset();
    }
};

struct SinkScope {
    LogSink saved;
    explicit SinkScope(const LogSink& s) : saved(log_sink()) { log_sink() = s; }
    ~SinkScope() { log_sink() = saved; }
};

}  // namespace txe
}  // namespace

AVM_TX_API void* avm_tx_create(int argc, const char* const* argv, const avm_tx_callbacks* cb) {
    txe::Engine* e = new txe::Engine;
    if (cb) e->cb = *cb;
    e->sink.fn = e->cb.log;
    e->sink.user = e->cb.user;
    txe::SinkScope scope(e->sink);
    if (!e->open(argc, argv)) { delete e; return nullptr; }
    e->start();
    return e;
}

AVM_TX_API void avm_tx_push(void* h, const void* data, size_t n) {
    if (h && n) ((txe::Engine*)h)->in.push(data, n);
}

AVM_TX_API void avm_tx_end_input(void* h) {
    if (h) ((txe::Engine*)h)->in.close();
}

AVM_TX_API void avm_tx_set_gain(void* h, double db) {
    txe::Engine* e = (txe::Engine*)h;
    if (!e || !e->radio) return;
    txe::SinkScope scope(e->sink);
    e->radio->set_gain(db);
}

AVM_TX_API int avm_tx_running(void* h) { return h && ((txe::Engine*)h)->running ? 1 : 0; }

AVM_TX_API void avm_tx_destroy(void* h) {
    txe::Engine* e = (txe::Engine*)h;
    if (!e) return;
    {
        txe::SinkScope scope(e->sink);
        e->stop();
    }
    delete e;
}
