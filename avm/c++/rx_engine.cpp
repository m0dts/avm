// rx_engine.cpp -- AVM's receiver as a library engine: hf_ofdm_rx.py's main
// loop (run_decode_loop / receive_one_fragment / try_decode, and hf_sc /
// hf_sch's receive_one_fragment for S and SH) on the C++ modem, behind the C
// API in rx_engine.h. avm_rx (the program) and the GUI both run it.
//
// Input: complex64 IQ pushed by the owner (avm_rx_push), or a SoapySDR radio
// opened here (--input pluto --sdr DRIVER ...: sdr.h). Output: the
// fragments' data to the data callback, paced over each fragment's air time
// (the Python's StdoutPacer; --no-output-pacing: at once). Status lines --
// the same as hf_ofdm_rx.py's ("Fragment seq=..: OK  CFO=.. BER=.. ..",
// "[spectrum] ..") -- to the log callback. The receive path is the compiled
// front end (rx_frontend.py: IQ correction, LO shift with AFC, low-pass,
// resampling to the mode's rate), so --sample-rate must differ from the
// mode's rate.
//
// Testing: --test-step BYTES takes all the input first and runs the front
// end synchronously in fixed steps, as c++/e2e_rx.py drives the Python;
// with HF_OFDM_RX_WAIT_EOF=1 (no time budget) the output and status lines
// match the Python's (c++/test_avm_rx.py).
#include "modem_rx.cpp"
#include "hpsdr1.h"
#include "net_radios.h"
#include "tdd.h"
#include "sdr.h"
#include "avm_log.h"
#include "rx_engine.h"
#include <functional>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <string>
#include <thread>

#include <sys/stat.h>

namespace {

#include "avm_app.h"

// ====================================================================== front end (rx_frontend.FrontEnd)
// numpy's pairwise summation (np.sum of a float64 array)
double np_pairwise(const double* a, long long n) {
    if (n < 8) {
        double r = 0;
        for (long long i = 0; i < n; ++i) r += a[i];
        return r;
    }
    if (n <= 128) {
        double r[8];
        for (int j = 0; j < 8; ++j) r[j] = a[j];
        long long i;
        for (i = 8; i < n - (n % 8); i += 8)
            for (int j = 0; j < 8; ++j) r[j] += a[i + j];
        double res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for (; i < n; ++i) res += a[i];
        return res;
    }
    long long n2 = n / 2;
    n2 -= n2 % 8;
    return np_pairwise(a, n2) + np_pairwise(a + n2, n - n2);
}

// hf_ofdm_common.design_lowpass_fir (windowed sinc, Hamming, unity DC gain)
std::vector<double> design_lowpass_fir(double cutoff, double fs, int nt) {
    std::vector<double> h(nt);
    double fc = cutoff / fs;
    for (int i = 0; i < nt; ++i) {
        double n = i - (nt - 1) / 2.0;
        double x = 2 * fc * n;
        double y = fft::PI * (x == 0 ? 1.0e-20 : x);
        h[i] = 2 * fc * (std::sin(y) / y);
        double wn = (double)(1 - nt + 2 * i);
        h[i] *= 0.54 + 0.46 * std::cos(fft::PI * wn / (nt - 1));
    }
    double s = np_pairwise(h.data(), nt);
    for (double& v : h) v /= s;
    return h;
}

TX_API long long fe_process(const cf* x, long long n, cf* hist, const float* taps_rev, int nt, long long n_in0,
                            long long n_out0, double step, const double* iq, int lo_on, const cd* lo_table,
                            long long period, long long lo_phase0, cf* out);

struct FrontEnd {
    double step, in_fs, lo_hz = 0;
    std::vector<float> taps_rev;
    std::vector<cf> hist;
    long long n_in = 0, n_out = 0;
    double iq[5] = {0, 0, 0, 0, 0}, acc[5] = {0, 0, 0, 0, 0};
    long long iq_count = 0, iq_first, iq_step;
    std::vector<cd> table;
    bool have_table = true;

    FrontEnd(double fs_in, double fs_out, const std::vector<double>& taps, double lo)
        : step(fs_in / fs_out), in_fs(fs_in) {
        taps_rev.resize(taps.size());
        for (size_t i = 0; i < taps.size(); ++i) taps_rev[i] = (float)taps[taps.size() - 1 - i];
        hist.assign(taps.size(), cf(0, 0));
        iq_first = std::max(1LL, (long long)std::nearbyint(1.0 * fs_in));
        iq_step = std::max(1LL, (long long)std::nearbyint(0.25 * fs_in));
        set_lo_offset(lo);
    }
    void set_lo_offset(double lo) {
        lo_hz = lo;
        have_table = lo_phasor_table(lo, in_fs, table);
    }
    long long next_iq_boundary() const {
        if (iq_count < iq_first) return iq_first;
        return iq_first + ((iq_count - iq_first) / iq_step + 1) * iq_step;
    }
    void update_iq(const cf* x, long long n) {
        long long boundary = next_iq_boundary();
        for (long long i = 0; i < n; ++i) {
            double I = x[i].real(), Q = x[i].imag();
            acc[0] += I; acc[1] += Q; acc[2] += I * I; acc[3] += Q * Q; acc[4] += I * Q;
        }
        iq_count += n;
        if (iq_count < boundary) return;
        double s[5];
        for (int k = 0; k < 5; ++k) s[k] = acc[k] / (double)iq_count;
        double mI = s[0], mQ = s[1];
        double var_I = s[2] - mI * mI, var_Q = s[3] - mQ * mQ, cross = s[4] - mI * mQ;
        if (var_I <= 0) return;
        double slope = cross / var_I;
        double var_Q1 = var_Q - 2 * slope * cross + slope * slope * var_I;
        if (var_Q1 <= 0) return;
        iq[0] = 1.0; iq[1] = mI; iq[2] = mQ; iq[3] = slope; iq[4] = std::sqrt(var_I / var_Q1);
    }
    void process_chunk(const cf* x, long long n, std::vector<cf>& out) {
        long long n_max = (long long)((double)(n_in + n) / step) - n_out + 2;
        size_t o0 = out.size();
        out.resize(o0 + std::max(n_max, 1LL));
        long long k;
        if (have_table) {
            k = fe_process(x, n, hist.data(), taps_rev.data(), (int)taps_rev.size(), n_in, n_out, step, iq,
                           lo_hz != 0, table.data(), (long long)table.size(), n_in % (long long)table.size(),
                           out.data() + o0);
        } else {
            std::vector<cd> t(n);
            for (long long i = 0; i < n; ++i) {
                double th = (2.0 * fft::PI * lo_hz * (double)(n_in + i)) / in_fs;
                t[i] = cd(std::cos(th), std::sin(th));
            }
            k = fe_process(x, n, hist.data(), taps_rev.data(), (int)taps_rev.size(), n_in, n_out, step, iq,
                           lo_hz != 0, t.data(), n, 0, out.data() + o0);
        }
        n_in += n;
        n_out += k;
        out.resize(o0 + k);
        update_iq(x, n);
    }
    // FrontEnd.process: split at the IQ-update points
    void process(const cf* x, long long n, std::vector<cf>& out) {
        while (n > 0) {
            long long need = next_iq_boundary() - iq_count;
            if (need >= n) break;
            process_chunk(x, need, out);
            x += need;
            n -= need;
        }
        process_chunk(x, n, out);
    }
};

// ====================================================================== input
// The modem-rate sample stream (the FrontEndReader's output), with its
// arrival bookkeeping: total_len in bytes as the Python's, chunks as they
// came. Live: the owner pushes IQ bytes (avm_rx_push), a thread here runs
// the front end on them; or a radio's own read thread. Test (test_step):
// all the input first, then pulled synchronously in fixed steps on
// wait_for_more.
struct Reader {
    std::mutex mu;
    std::condition_variable cv;
    std::vector<std::vector<cf>> chunks;
    long long total_len = 0;     // bytes
    bool eof = false;
    std::atomic<bool> stopping{false};
    FrontEnd* fe = nullptr;
    // --invert-spectrum: I/Q conjugated as it arrives (an inverting receive path:
    // a high-side-LO converter, some radios' IQ order), as hf_ofdm_rx.py's
    bool invert = false;
    std::mutex fe_mu;            // the front end: the input thread and the AFC
    // pushed input, waiting for the front end
    std::mutex in_mu;
    std::condition_variable in_cv;
    std::deque<std::string> in_q;
    bool in_end = false;
    // test mode
    long long test_step = 0;
    std::vector<char> test_data;
    size_t test_pos = 0;
    bool inner_eof = false;
    std::string leftover;
    std::thread th;

    ~Reader() { stop(); }
    // the receive loop sees the end at once; the input thread exits
    void stop() {
        stopping = true;
        {
            std::lock_guard<std::mutex> g(in_mu);
            in_end = true;
            in_cv.notify_all();
        }
        {
            std::lock_guard<std::mutex> g(mu);
            eof = true;
            cv.notify_all();
        }
        if (th.joinable()) th.join();
    }

    // the newest samples, for the spectrum
    std::vector<cf> recent = std::vector<cf>(8192);
    size_t recent_pos = 0;
    // and the newest raw ones, before the front end (--spectrum-span-hz)
    bool want_raw = false;
    std::mutex raw_mu;
    std::vector<cf> raw = std::vector<cf>(65536);
    size_t raw_pos = 0;
    long long raw_total = 0;
    void keep_raw(const cf* x, long long n) {
        if (!want_raw) return;
        std::lock_guard<std::mutex> g(raw_mu);
        for (long long i = 0; i < n; ++i) { raw[raw_pos] = x[i]; raw_pos = (raw_pos + 1) % raw.size(); }
        raw_total += n;
    }
    void push(std::vector<cf>&& y) {
        if (y.empty()) return;
        std::lock_guard<std::mutex> g(mu);
        for (const cf& v : y) { recent[recent_pos] = v; recent_pos = (recent_pos + 1) % recent.size(); }
        total_len += (long long)y.size() * 8;
        chunks.push_back(std::move(y));
        cv.notify_all();
    }
    void mark_eof() {
        std::lock_guard<std::mutex> g(mu);
        eof = true;
        cv.notify_all();
    }
    // samples straight from a radio
    void process_samples(const cf* x, long long n) {
        std::vector<cf> conj;
        if (invert) {
            conj.assign(x, x + n);
            for (cf& v : conj) v = std::conj(v);
            x = conj.data();
        }
        keep_raw(x, n);
        std::vector<cf> y;
        {
            std::lock_guard<std::mutex> g(fe_mu);
            fe->process(x, n, y);
        }
        push(std::move(y));
    }
    // ---- a radio (SoapySDR): read on its own thread
    void start_radio(sdr::RadioIn* src) {
        th = std::thread([this, src, sink = log_sink()] {
            log_sink() = sink;
            std::vector<std::complex<float>> buf;
            while (!stopping) {
                long n = src->read(buf);
                if (n < 0) {
                    if (!stopping) avm_logf("ERROR: the radio stopped sending -- RX stopped\n");
                    break;
                }
                if (n > 0) process_samples(buf.data(), n);
            }
            mark_eof();
        });
    }
    void process_bytes(const std::string& bytes_in) {
        std::string data = leftover + bytes_in;
        size_t usable = data.size() - data.size() % 8;
        leftover = data.substr(usable);
        if (!usable) return;
        std::vector<cf> x(usable / 8);
        std::memcpy(x.data(), data.data(), usable);
        for (cf& v : x) {
            if (!std::isfinite(v.real()) || !std::isfinite(v.imag())) v = cf(0, 0);
            if (invert) v = std::conj(v);
        }
        keep_raw(x.data(), (long long)x.size());
        std::vector<cf> y;
        {
            std::lock_guard<std::mutex> g(fe_mu);
            fe->process(x.data(), (long long)x.size(), y);
        }
        push(std::move(y));
    }
    // ---- pushed input (avm_rx_push / avm_rx_end_input)
    void feed(const void* p, size_t n) {
        std::lock_guard<std::mutex> g(in_mu);
        if (in_end) return;
        in_q.emplace_back((const char*)p, n);
        in_cv.notify_all();
    }
    void end_input() {
        std::lock_guard<std::mutex> g(in_mu);
        in_end = true;
        in_cv.notify_all();
    }
    // the next pushed block; false: the input ended (or stop)
    bool take(std::string& out) {
        std::unique_lock<std::mutex> lk(in_mu);
        in_cv.wait(lk, [&] { return !in_q.empty() || in_end; });
        if (stopping || in_q.empty()) return false;
        out = std::move(in_q.front());
        in_q.pop_front();
        return true;
    }
    void start_live() {
        th = std::thread([this, sink = log_sink()] {
            log_sink() = sink;
            std::string b;
            while (take(b)) process_bytes(b);
            mark_eof();
        });
    }
    // ---- test: the whole input first (called on the receive thread), handed over in steps
    void start_test(long long step) {
        test_step = step;
        std::string b;
        while (take(b)) test_data.insert(test_data.end(), b.begin(), b.end());
        test_add();
        test_pull();
    }
    std::string test_pending;
    void test_add() {
        if (test_pos >= test_data.size()) { inner_eof = true; return; }
        size_t n = std::min((size_t)test_step, test_data.size() - test_pos);
        test_pending.append(test_data.data() + test_pos, n);
        test_pos += n;
        if (test_pos >= test_data.size()) inner_eof = true;
    }
    void test_pull() {
        if (!test_pending.empty()) { std::string b; b.swap(test_pending); process_bytes(b); }
        if (inner_eof && test_pending.empty()) eof = true;
    }
    // blocks until more than since_len bytes have arrived, or EOF
    void wait_for_more(long long since_len) {
        if (test_step) {
            while (!(eof || total_len > since_len)) { test_add(); test_pull(); }
            return;
        }
        std::unique_lock<std::mutex> lk(mu);
        cv.wait(lk, [&] { return eof || total_len > since_len; });
    }
    long long total() { std::lock_guard<std::mutex> g(mu); return total_len; }
    bool at_eof() { std::lock_guard<std::mutex> g(mu); return eof; }
};

// get_rx_raw_incremental: the received samples so far, global indices
// (old ones trimmed away behind the search point)
const long long RAW_BUFFER_TRIM_MARGIN = 500000, RAW_BUFFER_TRIM_INTERVAL = 1000000,
                RAW_BUFFER_HARD_CAP = 50000000;
struct Buffer {
    std::vector<cd> buf;         // samples [origin, origin + buf.size())
    long long origin = 0;
    size_t n_chunks = 0;
    long long last_trim_at = 0;
    long long len() const { return origin + (long long)buf.size(); }
    void trim_before(long long idx) {
        long long cut = std::min(std::max(0LL, idx - origin), (long long)buf.size());
        if (cut <= 0) return;
        buf.erase(buf.begin(), buf.begin() + cut);
        origin += cut;
    }
    void update(Reader& r, long long trim_reference) {
        std::vector<std::vector<cf>> fresh;
        {
            std::lock_guard<std::mutex> g(r.mu);
            for (size_t i = n_chunks; i < r.chunks.size(); ++i) fresh.push_back(std::move(r.chunks[i]));
            for (size_t i = n_chunks; i < r.chunks.size(); ++i) r.chunks[i].clear();
            n_chunks = r.chunks.size();
        }
        for (auto& ch : fresh) {
            if (trim_reference >= 0 && (long long)buf.size() > RAW_BUFFER_HARD_CAP) {
                trim_before(trim_reference - RAW_BUFFER_TRIM_MARGIN);
                last_trim_at = trim_reference;
            }
            for (const cf& v : ch) buf.push_back(cd(v.real(), v.imag()));
        }
        if (trim_reference >= 0 && trim_reference - last_trim_at >= RAW_BUFFER_TRIM_INTERVAL) {
            trim_before(trim_reference - RAW_BUFFER_TRIM_MARGIN);
            last_trim_at = trim_reference;
        }
    }
    // pointer to global index i (must be >= origin)
    const cd* at(long long i) const { return buf.data() + (i - origin); }
};

// ====================================================================== output (StdoutPacer: to the owner's data callback)
struct Pacer {
    struct Item { std::string data; double dur; long long tag; double submit_wall; };
    std::mutex mu;
    std::condition_variable cv, cv_space, cv_done;
    std::deque<Item> q;
    size_t depth;
    bool pace;
    int busy = 0;
    bool stop = false;
    double schedule_t = -1;
    std::thread th;

    static double mono() {
        return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
    }
    static double wall() {
        return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    }
    std::function<void(const char*, size_t)> out;
    Pacer(size_t depth_, bool pace_, std::function<void(const char*, size_t)> out_)
        : depth(depth_ ? depth_ : 1), pace(pace_), out(std::move(out_)) {
        th = std::thread([this, sink = log_sink()] { log_sink() = sink; run(); });
    }
    // stopping: what's queued is dropped
    void abort() {
        std::lock_guard<std::mutex> g(mu);
        q.clear();
        cv_space.notify_all();
        cv_done.notify_all();
    }
    // stop the writer and join it before the condition variables go: glibc's
    // pthread_cond_destroy waits for waiters, so destroying them under a
    // still-waiting thread (detached, as this was) hung avm_rx at exit on Linux
    ~Pacer() {
        {
            std::lock_guard<std::mutex> g(mu);
            stop = true;
            cv.notify_all();
        }
        if (th.joinable()) th.join();
    }
    void submit(const std::string& data, double dur, long long tag) {
        std::unique_lock<std::mutex> lk(mu);
        cv_space.wait(lk, [&] { return q.size() < depth; });
        q.push_back({data, dur, tag, wall()});
        cv.notify_all();
    }
    void drain() {
        std::unique_lock<std::mutex> lk(mu);
        cv_done.wait(lk, [&] { return q.empty() && busy == 0; });
    }
    void write_out(const char* p, size_t n) { if (out) out(p, n); }
    void run() {
        const double CHUNK_INTERVAL_S = 0.02, MAX_SCHEDULE_LAG_S = 1.0, SMALL_LAG_TOLERANCE_S = 0.05;
        while (true) {
            Item it;
            {
                std::unique_lock<std::mutex> lk(mu);
                cv.wait(lk, [&] { return !q.empty() || stop; });
                if (q.empty()) return;          // stopping, nothing left to write
                it = std::move(q.front());
                q.pop_front();
                busy = 1;
                cv_space.notify_all();
            }
            if (!it.data.empty() && !pace) {
                write_out(it.data.data(), it.data.size());
            } else if (!it.data.empty()) {
                double now = mono();
                if (schedule_t < 0 || now - schedule_t > MAX_SCHEDULE_LAG_S) schedule_t = now;
                else if (now - schedule_t > SMALL_LAG_TOLERANCE_S) schedule_t = now - SMALL_LAG_TOLERANCE_S;
                else if (schedule_t - now > SMALL_LAG_TOLERANCE_S) schedule_t = now + SMALL_LAG_TOLERANCE_S;
                double start = schedule_t;
                long long n_chunks = std::max(1LL, (long long)(it.dur / CHUNK_INTERVAL_S));
                size_t len = it.data.size();
                size_t cs = std::max<size_t>(1, (len + n_chunks - 1) / n_chunks);
                double per = it.dur / n_chunks;
                long long ci = 0;
                for (size_t pos = 0; pos < len; pos += cs) {
                    write_out(it.data.data() + pos, std::min(cs, len - pos));
                    ++ci;
                    if (pos + cs < len) {
                        double rem = start + ci * per - mono();
                        if (rem > 0) std::this_thread::sleep_for(std::chrono::duration<double>(rem));
                    }
                }
                schedule_t = start + it.dur;
                double done = wall();
                avm_logf("  [pacer] seq=%lld queued_at=%.3f flushed_at=%.3f (queue_delay=%+.3fs beyond its own %.3fs trickle)\n",
                             it.tag, it.submit_wall, done, (done - it.submit_wall) - it.dur, it.dur);
            }
            std::lock_guard<std::mutex> g(mu);
            busy = 0;
            cv_done.notify_all();
        }
    }
};

// ====================================================================== the receive loop
const double SYNC_MIN = 0.3;      // SYNC_METRIC_MIN
const double CFO_EMA_ALPHA = 0.3, CFO_JUMP_LIMIT_HZ = 100.0;
const int FRAG_HEADER_SIZE = 8;   // >HHI
const int REACQUISITION_TROUBLE_LIMIT = 5;
const double DROP_IF_SLOW_LAG_S = 1.0, SKIP_KEEP_FRAGMENTS = 1.5, SKIP_EXTRA_LAG_S = 0.6;
const double SEQ_MIN_FRAGMENT_S = 0.03;
const int SEQ_GAP_SLACK = 20, MAX_MISMATCH_RETRIES = 8;
const double AFC_ROOM_FRAC = 0.3, AFC_EMA_ALPHA = 0.5;
const int AFC_MIN_FRAGMENTS = 2;

double now_s() { return Pacer::mono(); }

struct Args {
    std::string mode = "B", modulation = "qpsk", fec = "auto";
    double occupancy = 20, sample_rate = 0, fragment_gap_ms = 0, lo_offset_hz = 0;
    int fragment_size = 0, rx_queue_depth = 2;
    bool no_output_pacing = false, drop_if_slow = false, invert_spectrum = false;
    // --input pluto: a radio through SoapySDR (sdr.h), hf_ofdm_rx.py's radio options
    std::string input = "pipe", sdr = "pluto", uri, antenna;
    double rf_freq = 0, rx_gain = 30, bandwidth = 0, ppm = 0, warmup_s = 0.75;
    bool agc = false;
    bool spectrum = false;
    double spectrum_span = 0;     // --spectrum-span-hz: the raw samples, this wide around the signal
    std::string gain_file, freq_offset_file, ppm_file;
    long long test_step = 0;
    bool assume_prior = true;
    int sync_fast_symbols = 6;
    bool wait_eof = false;
    // --tdd-cycle-s: same-channel duplex (hf_ofdm_rx.py _tdd_*)
    double tdd_cycle_s = 0, tdd_slot_s = 0, tdd_offset_s = 0, tdd_tx_gain = -10;
    int tdd_station = -1;            // -1: not given
    std::string tdd_adjust_file, tdd_report_file;
    bool tdd_tx = false;             // --tdd-tx: this receiver's radio also transmits (tdd.h)
};

struct Opt { bool has = false; double v = 0; };

struct FragResult {
    std::string status;              // ok / failed / incomplete / eof
    std::vector<uint8_t> payload;
    bool crc_ok = false;
    double cfo_hz = 0, ber = NaN, evm = NaN;
    std::vector<cd> constellation;
    bool has_next = false;
    long long next = 0;
    bool has_frame_start = false;
    long long frame_start = 0;
    int retries = 0, waits = 0;
    bool has_compute = false;
    double compute = 0;
};

struct SyncCache {
    bool has = false;
    long long start_index = 0;
    double cfo_hz = 0, confidence = 0;
    int extra_k = 0;
    bool assumed_k = false;
    Opt prior_k;
};

struct State {
    long long nss = 0;               // native_search_start (the mode's rate)
    Opt last_cfo;
    int consecutive_failures = 0;
    bool sc_expect_next = false;
    double last_ok_time = -1;
    // AFC
    bool afc_init = false;
    double afc_base = 0, afc_hz = 0, afc_ema = 0;
    long long afc_from = 0;
    int afc_n = 0;
    std::vector<std::pair<long long, double>> afc_hist;
};

// duplex: where the slots are, for the spectrum (only the other station's slot shows)
struct TddView {
    std::mutex mu;
    bool active = false, heard = false, has_phase = false, has_own = false;
    double phase = 0, own_phase = 0, cycle = 0, burst = 0, margin = 0;
    // [start, end) native samples: inside the far end's slot (and not our own)?
    bool far_slot(double start, double end) {
        std::lock_guard<std::mutex> g(mu);
        if (!active) return true;
        if (heard && has_phase) return pyfmod(start - phase, cycle) + (end - start) <= burst;
        if (!has_own) return true;
        double into = pyfmod(start - (own_phase - margin), cycle);
        return into >= burst + 2 * margin && into + (end - start) <= cycle;
    }
};

// hf_ofdm_rx.py's _tdd_* constants
const unsigned TDD_HEADER_FLAG = 0x8000, TDD_STATION_BIT = 0x4000;
const uint32_t TDD_REPORT_FLAG = 0x80000000u;
const double TDD_ADJUST_MIN_S = 50e-6, TDD_UNMUTE_S = 0.005, TDD_PROBE_S = 0.10;
const double TDD_CLOSED_S = 5.0, TDD_CLOSED_HOLD_S = 3.5, TDD_CLOSED_MIN_S = 0.0005;
const double TDD_MARGIN_S = 0.010, TDD_REPORT_EVERY_S = 10.0;
const int TDD_LOST_CYCLES = 3;

struct Tdd {
    double cycle = 0, burst = 0, step = 0, margin = 0, fs = 0, radio_fs = 0, target = 0;
    long long n_frame = 0;
    int per_slot = 0, station = -1;
    Opt phase, last_ok, own_phase;
    bool heard = false;
    double shift_s = 0;
    std::string adjust_file, report_file;
    int rep_ctr = 0;
    Opt rep_burst;
    double closed_t = -1e9, closed_hold_t = -1e9, reported = -1e9;
    int last_ctr = -1;
    tdd::Feeder* feeder = nullptr;      // combined TX/RX: own slot from the TX count
    sdr::RadioIn* source = nullptr;
    long long own_seen = 0;
    int hold = 0;
};

static bool write_atomic(const std::string& path, const std::string& text) {
    std::string tmp = path + ".tmp";
    FILE* f = std::fopen(tmp.c_str(), "wb");
    if (!f) return false;
    std::fwrite(text.data(), 1, text.size(), f);
    std::fclose(f);
#ifdef _WIN32
    std::remove(path.c_str());
#endif
    return std::rename(tmp.c_str(), path.c_str()) == 0;
}

struct Receiver {
    Args a;
    Modem md;
    Reader& reader;
    Buffer buf;
    State st;
    // duplex
    std::unique_ptr<Tdd> tdd_;
    sdr::RadioIn* radio_src = nullptr;
    TddView* tdd_view = nullptr;

    std::function<void(const char*, size_t)> out;
    Receiver(const Args& args, const Mode& m, Reader& r, std::function<void(const char*, size_t)> out_)
        : a(args), md(m), reader(r), out(std::move(out_)) {}

    const Mode& m() const { return md.m; }

    // apply_cfo_correction on global [lo, hi) of the buffer (complex64 input)
    std::vector<cd> rotated(long long lo, long long hi, double cfo) {
        hi = std::min(hi, buf.len());
        lo = std::max(lo, buf.origin);
        std::vector<cd> out(std::max(0LL, hi - lo));
        if (!out.empty()) rotate(buf.at(lo), hi - lo, 2 * fft::PI * cfo / m().fs, out.data(), true);
        return out;
    }

    // ---- try_decode
    void search_integer_cfo(long long start, double cfo, Opt prior_k, std::map<long long, std::pair<int, double>>& memo,
                            int& k, double& conf) {
        int nh = header_symbol_plan(*md.c).back() + 1;
        auto search = [&](long long s, int& kk, double& cc) {
            std::vector<cd> seg = rotated(s, s + m().preamble_len + (long long)nh * m().symbol_len, cfo);
            long long n = std::max(0LL, (long long)seg.size() - m().preamble_len);
            resolve_integer_cfo(*md.c, md.sp, seg.data() + std::min((long long)seg.size(), (long long)m().preamble_len),
                                n, 30, prior_k.has, (int)prior_k.v, kk, cc, true);
        };
        search(start, k, conf);
        // the sync's start is the earliest the frame can begin: FFT windows that
        // early can tilt the carriers' phases past the pilots' interpolation
        // (hf_ofdm_rx._search_integer_cfo). Unsure: half a prefix later too.
        if (conf < 0.95) {
            int k2;
            double c2;
            search(start + m().n_cp / 2, k2, c2);
            if (c2 > conf) { k = k2; conf = c2; }
        }
        memo[start] = {k, conf};
    }

    FragResult try_decode(long long nss, SyncCache& cache, std::map<long long, std::pair<int, double>>& memo,
                          Opt last_cfo) {
        FragResult R;
        const Mode& M = m();
        const Cfg& c = *md.c;
        long long len = buf.len();
        if (len < nss + M.preamble_len + M.symbol_len * 2) { R.status = "incomplete"; return R; }
        long long max_wait;
        if (a.fragment_size) {
            long long expected = a.fragment_size + FRAG_HEADER_SIZE;
            max_wait = std::min((long long)(M.fs * 300), (md.frame_symbol_count(expected * 3) + 2) * M.symbol_len);
        } else {
            max_wait = (long long)(M.fs * 300);
        }
        long long max_needed = max_wait + M.symbol_len + M.preamble_len;
        long long start_index;
        double cfo_hz, confidence;
        int extra_k;
        bool assumed_k;
        Opt prior_k;
        if (cache.has) {
            start_index = cache.start_index; cfo_hz = cache.cfo_hz; extra_k = cache.extra_k;
            confidence = cache.confidence; assumed_k = cache.assumed_k; prior_k = cache.prior_k;
        } else {
            long long floor_;
            if (a.fragment_size) {
                long long one = md.frame_symbol_count(a.fragment_size + FRAG_HEADER_SIZE) * M.symbol_len + M.preamble_len;
                floor_ = 2 * one;
            } else {
                floor_ = (long long)(M.fs * 5);
            }
            long long bound = std::max(max_needed, floor_);
            double metric = -1.0;
            long long fast_len = (long long)a.sync_fast_symbols * M.symbol_len + M.preamble_len - M.symbol_len;
            int L = M.n_fft / 2, W = (M.halves - 1) * (M.n_fft / 2);
            auto sync = [&](long long search_len) {
                long long n_total = len;
                long long max_len = n_total - nss - (L + W);
                long long sl = std::max(1LL, std::min(search_len, max_len));
                long long hi = std::min(n_total, nss + sl + L + W);
                long long navail = std::max(0LL, hi - nss);
                schmidl_cox_sync(c, md.sp, buf.at(nss), nss, navail, n_total, nss, sl, true, start_index, cfo_hz, metric);
            };
            if (fast_len > 0 && fast_len < bound && len - nss >= fast_len + M.n_fft) {
                sync(fast_len);
                if (start_index + M.n_cp - nss > fast_len - 4LL * M.symbol_len) metric = -1.0;
            }
            if (metric < SYNC_MIN) sync(bound);
            if (metric < SYNC_MIN) {
                long long max_search_len = len - nss - 2LL * L;
                if (max_search_len >= bound) {
                    R.status = "failed";
                    R.has_next = true;
                    R.next = nss + bound;
                    return R;
                }
                R.status = "incomplete";
                return R;
            }
            if (last_cfo.has) { prior_k.has = true; prior_k.v = std::nearbyint((cfo_hz - last_cfo.v) / M.spacing); }
            assumed_k = false;
            auto it = memo.find(start_index);
            if (it != memo.end()) {
                extra_k = it->second.first;
                confidence = it->second.second;
            } else if (prior_k.has && a.assume_prior) {
                extra_k = (int)prior_k.v;
                confidence = 1.0;
                assumed_k = true;
            } else {
                search_integer_cfo(start_index, cfo_hz, prior_k, memo, extra_k, confidence);
            }
            double total = cfo_hz - extra_k * M.spacing;
            if (last_cfo.has && confidence < 0.95 && std::fabs(total - last_cfo.v) > CFO_JUMP_LIMIT_HZ) {
                R.status = "failed";
                return R;
            }
            cache.has = true;
            cache.start_index = start_index; cache.cfo_hz = cfo_hz; cache.extra_k = extra_k;
            cache.confidence = confidence; cache.assumed_k = assumed_k; cache.prior_k = prior_k;
        }
        const long long sync_start = start_index;
        double total_cfo = 0;
        auto decode_with = [&](int k, long long& refined, Result& res) {
            double total = cfo_hz - k * M.spacing;
            total_cfo = total;
            long long span = 3LL * M.n_cp, lo = std::max(0LL, sync_start - span);
            refined = refine_frame_start(c, md.sp, buf.at(std::max(lo, buf.origin)), std::max(lo, buf.origin),
                                         std::min(buf.len(), sync_start + M.preamble_len + span), sync_start, total,
                                         5.0, true);
            std::vector<cd> after = rotated(refined, refined + max_needed, total);
            long long off = std::min((long long)after.size(), (long long)M.preamble_len);
            decode_frame(c, after.data() + off, (long long)after.size() - off, max_wait, res);
        };
        Result res;
        decode_with(extra_k, start_index, res);
        if (assumed_k && !res.incomplete && (!res.has_payload || !res.crc_ok)) {
            int real_k;
            search_integer_cfo(sync_start, cfo_hz, prior_k, memo, real_k, confidence);
            cache.extra_k = real_k; cache.confidence = confidence; cache.assumed_k = false;
            if (real_k != extra_k) {
                if (last_cfo.has && confidence < 0.95 &&
                    std::fabs((cfo_hz - real_k * M.spacing) - last_cfo.v) > CFO_JUMP_LIMIT_HZ) {
                    R.status = "failed";
                    return R;
                }
                extra_k = real_k;
                decode_with(extra_k, start_index, res);
            }
        }
        if (!res.has_payload) { R.status = res.incomplete ? "incomplete" : "failed"; return R; }
        if (!res.crc_ok && confidence >= 0.95) {
            int step = std::max(1, M.n_cp / 24);
            std::vector<long long> deltas;
            for (int d = -M.n_cp; d <= M.n_cp; d += step) if (d != 0) deltas.push_back(d);
            long long best_delta;
            double bm;
            bool header_ok;
            int nh = header_symbol_plan(c).back() + 1;
            long long probe_len = M.preamble_len + (long long)nh * M.symbol_len;
            long long lo = std::max(std::max(0LL, start_index + deltas.front()), buf.origin);
            long long hi = std::min(buf.len(), start_index + deltas.back() + probe_len);
            resolve_timing_offset(c, md.sp, buf.at(lo), lo, hi, start_index, total_cfo, deltas.data(),
                                  (int)deltas.size(), best_delta, bm, header_ok, true);
            if (header_ok && best_delta != 0) {
                long long ps = start_index + best_delta;
                std::vector<cd> after = rotated(ps, ps + max_needed, total_cfo);
                long long off = std::min((long long)after.size(), (long long)M.preamble_len);
                Result probe;
                decode_frame(c, after.data() + off, (long long)after.size() - off, max_wait, probe);
                if (probe.has_payload && (probe.crc_ok || std::isnan(res.ber) || (!std::isnan(probe.ber) && probe.ber < res.ber)))
                    res = probe;
            }
        }
        long long n_symbols = md.frame_symbol_count((long long)res.payload.size());
        R.status = "ok";
        R.payload = res.payload;
        R.crc_ok = res.crc_ok;
        R.cfo_hz = total_cfo;
        R.ber = res.ber;
        R.evm = res.evm;
        R.constellation = res.constellation;
        R.has_next = true;
        R.next = start_index + M.preamble_len + n_symbols * M.symbol_len;
        R.has_frame_start = true;
        R.frame_start = start_index;
        return R;
    }

    // ---- receive_one_fragment (OFDM)
    FragResult receive_ofdm() {
        const Mode& M = m();
        int live_L = std::max(4, (int)std::nearbyint(M.n_fft / 2.0));
        std::vector<cd> live_tail;
        size_t live_chunks_scanned = 0;
        bool locked = reader.total() > 0;
        SyncCache cache;
        Opt last_bad;
        Opt last_cfo = st.last_cfo;
        bool have_mismatch = false;
        FragResult mismatch;
        int retries = 0;
        double budget = -1;
        if (a.fragment_size && !a.wait_eof)
            budget = 1.5 * md.frame_symbol_count(a.fragment_size + FRAG_HEADER_SIZE) * M.symbol_len / M.fs;
        long long search_start = st.nss;
        std::map<long long, std::pair<int, double>> memo;
        double compute = 0;
        int waits = 0;
        auto annotate = [&](FragResult r) {
            r.retries = retries;
            r.waits = waits;
            r.has_compute = true;
            r.compute = compute;
            return r;
        };
        while (true) {
            long long snapshot = -1;
            while (locked) {
                snapshot = reader.total();
                buf.update(reader, search_start);
                double t0 = now_s();
                FragResult r = try_decode(search_start, cache, memo, last_cfo);
                compute += now_s() - t0;
                if ((r.status == "ok" || r.status == "failed") && (r.status == "failed" || !r.crc_ok)) {
                    if (r.status == "ok" && !std::isnan(r.ber) && r.ber >= HOPELESS_BER) return annotate(r);
                    ++retries;
                    mismatch = r;
                    have_mismatch = true;
                    Opt bad;
                    if (cache.has) { bad.has = true; bad.v = (double)cache.start_index; }
                    cache = SyncCache();
                    if (bad.has && last_bad.has && bad.v == last_bad.v) {
                        long long step_symbols = 8;
                        if (a.fragment_size)
                            step_symbols = std::min(8LL, std::max(1LL, md.frame_symbol_count(a.fragment_size + FRAG_HEADER_SIZE)));
                        search_start = (long long)bad.v + M.symbol_len * step_symbols;
                    } else if (bad.has) {
                        search_start = (long long)bad.v + M.n_fft / 4;
                    } else {
                        search_start += M.n_fft / 4;
                    }
                    last_bad = bad;
                    bool over = budget >= 0 && r.status == "ok" && compute > budget;
                    if (retries >= MAX_MISMATCH_RETRIES || over) {
                        if (!mismatch.has_next) { mismatch.has_next = true; mismatch.next = search_start; }
                        return annotate(mismatch);
                    }
                    continue;
                } else if (r.status != "incomplete") {
                    return annotate(r);
                }
                break;
            }
            long long prev_len = snapshot >= 0 ? snapshot : reader.total();
            reader.wait_for_more(prev_len);
            ++waits;
            if (!locked) {
                std::vector<cd> seg = live_tail;
                {
                    std::lock_guard<std::mutex> g(reader.mu);
                    for (size_t i = live_chunks_scanned; i < reader.chunks.size(); ++i)
                        for (const cf& v : reader.chunks[i]) seg.push_back(cd(v.real(), v.imag()));
                    live_chunks_scanned = reader.chunks.size();
                }
                long long d;
                double metric;
                long long max_d = (long long)seg.size() - 2LL * live_L;
                metric = 0;
                if (max_d > 0) {
                    std::vector<double> mm, e;
                    std::vector<cd> P;
                    sc_metric(seg.data(), live_L, max_d, live_L, mm, P, e);
                    d = (long long)argmax(mm, 0, mm.size());
                    metric = mm[d];
                }
                if ((long long)seg.size() >= 2LL * live_L) live_tail.assign(seg.end() - 2LL * live_L, seg.end());
                else live_tail = seg;
                if (metric > SYNC_MIN) {
                    locked = true;
                    avm_logf("Signal acquired (metric=%.3f), decoding...\n", metric);
                }
            }
            if (reader.at_eof() && reader.total() <= prev_len) {
                if (have_mismatch) {
                    if (!mismatch.has_next) { mismatch.has_next = true; mismatch.next = search_start; }
                    return annotate(mismatch);
                }
                FragResult e;
                e.status = "eof";
                return annotate(e);
            }
        }
    }

    // ---- hf_sc.receive_one_fragment (S)
    void wait_more() { reader.wait_for_more(reader.total()); }
    void get_samples() { buf.update(reader, st.nss); }

    std::vector<cd> slice(long long lo, long long hi) {
        lo = std::max(lo, buf.origin);
        hi = std::min(hi, buf.len());
        if (hi <= lo) return {};
        return std::vector<cd>(buf.at(lo), buf.at(lo) + (hi - lo));
    }
    static std::vector<cd> c64(std::vector<cd> v) { for (cd& x : v) x = round_c64(x); return v; }

    // --drop-if-slow while searching (S, SH): the search ran slower than real time
    // on a busy Pi (TX, camera and GUI alongside) and fell ever further behind,
    // so a signal that came up was found tens of seconds late. More than
    // SEARCH_SKIP_LAG_S behind live: jump to the last SEARCH_SKIP_KEEP_S.
    bool search_catch_up() {
        if (!a.drop_if_slow) return false;
        const double SEARCH_SKIP_LAG_S = 3.0, SEARCH_SKIP_KEEP_S = 1.5;
        long long live = reader.total() / 8;
        double lag = (live - st.nss) / m().fs;
        if (lag <= SEARCH_SKIP_LAG_S) return false;
        st.nss = std::max(live - (long long)(SEARCH_SKIP_KEEP_S * m().fs), buf.origin);
        avm_logf("  -> SKIPPED %.2fs of backlog while searching (the search fell %.2fs behind live)\n",
                 lag - SEARCH_SKIP_KEEP_S, lag);
        return true;
    }

    FragResult receive_s() {
        const Mode& M = m();
        double t_start = now_s();
        long long chunk = (long long)(2.0 * M.fs);
        long long margin = 2 * S_RRC_DELAY + 8 * SC_SPS;
        const SC& s = *md.sc;
        while (true) {
            get_samples();
            search_catch_up();
            long long start = st.nss, avail = buf.len() - start;
            long long need = (long long)S_PRE_LEN * SC_SPS + margin;
            if (avail < need) {
                if (reader.at_eof()) { FragResult r; r.status = "eof"; return r; }
                wait_more();
                continue;
            }
            bool quick = st.sc_expect_next && avail >= 3LL * S_PRE_LEN * SC_SPS + margin;
            st.sc_expect_next = false;
            long long seg_end = start + (quick ? 3LL * S_PRE_LEN * SC_SPS + margin : std::min(avail, chunk));
            std::vector<cd> seg = slice(start, seg_end);
            std::vector<cd> z = s_mf(s, seg.data(), (long long)seg.size());
            long long i;
            double metric, cfo1;
            bool hit = (long long)z.size() - margin > 0 &&
                       s_detect(s, z.data(), (long long)z.size() - margin, i, metric, cfo1);
            if (!hit && quick) continue;
            if (!hit) {
                // two preambles' overlap kept (see hf_sc.receive_one_fragment)
                long long keep = 2LL * S_PRE_LEN * SC_SPS + margin;
                if (avail < chunk && !reader.at_eof()) {
                    wait_more();
                    st.nss = std::max(start, seg_end - keep);
                    continue;
                }
                st.nss = std::max(start + 1, seg_end - keep);
                if (reader.at_eof() && seg_end >= buf.len()) { FragResult r; r.status = "eof"; return r; }
                continue;
            }
            long long frame_start = start + i - S_RRC_DELAY;
            long long hdr_end = start + i + (long long)(S_PRE_LEN + S_HDR_BITS * S_HDR_REPEAT) * SC_SPS + margin;
            while (true) { get_samples(); if (buf.len() >= hdr_end || reader.at_eof()) break; wait_more(); }
            get_samples();
            long long lo = std::max(std::max(start + i - margin, buf.origin), 0LL);
            long long lead = start + i - lo;
            std::vector<cd> hseg = slice(lo, std::min(buf.len(), hdr_end + margin));
            double t0, cfo;
            std::vector<cd> zz;
            s_sync(s, hseg.data(), (long long)hseg.size(), lead, cfo1, t0, cfo, zz);
            cd ref = s_ref(s, zz, t0);
            int n_bytes = s_header(s, zz, t0, ref);
            if (n_bytes < 0 || (a.fragment_size && n_bytes > 2 * (a.fragment_size + 6))) {
                FragResult r;
                r.status = "failed";
                r.has_next = true;
                r.next = start + i + (long long)S_PRE_LEN * SC_SPS / 2;
                return r;
            }
            long long fs_ = md.s_frame_samples(n_bytes);
            long long end = frame_start + fs_ + margin;
            while (true) { get_samples(); if (buf.len() >= end || reader.at_eof()) break; wait_more(); }
            get_samples();
            if (buf.len() < end - margin - 2 * S_RRC_DELAY) { FragResult r; r.status = "incomplete"; return r; }
            std::vector<cd> dseg = slice(lo, std::min(end, buf.len()));
            if ((long long)dseg.size() < end - lo) dseg.resize(end - lo, cd(0, 0));
            std::vector<cd> dz = s_derotated_mf(s, dseg.data(), (long long)dseg.size(), cfo);
            s_decode(s, dz, t0, n_bytes, md.sres);
            st.sc_expect_next = true;
            FragResult r;
            r.status = "ok";
            r.payload = md.sres.payload;
            r.crc_ok = md.sres.crc_ok;
            r.cfo_hz = cfo * M.symbol_rate;
            r.ber = md.sres.ber;
            r.evm = md.sres.evm;
            r.constellation = c64(md.sres.d);
            r.has_frame_start = true;
            r.frame_start = frame_start;
            r.has_next = true;
            r.next = frame_start + fs_ - 2 * SC_SPS;
            (void)t_start;
            return r;
        }
    }

    // ---- hf_sch.receive_one_fragment (SH)
    FragResult receive_sh() {
        const Mode& M = m();
        const SC& s = *md.sc;
        const SHGeo& g = md.g;
        long long chunk = (long long)(2.0 * M.fs);
        long long keep = (long long)(g.PRE_HF + g.N + 64) * SC_SPS;
        long long margin = 2 * S_RRC_DELAY + 8 * SC_SPS;
        while (true) {
            get_samples();
            search_catch_up();
            long long start = st.nss, avail = buf.len() - start;
            if (avail < keep + margin) {
                if (reader.at_eof()) { FragResult r; r.status = "eof"; return r; }
                wait_more();
                continue;
            }
            long long seg_end = start + std::min(avail, chunk);
            std::vector<cd> seg = c64(slice(start, seg_end));
            double t0, cfo;
            int variant = 0;
            int hit = sh_detect(s, seg.data(), (long long)seg.size(), M.symbol_rate, t0, cfo, variant);
            if (hit != 1) {
                if (hit == 2 && avail < chunk && !reader.at_eof()) { wait_more(); continue; }
                if (avail < chunk && !reader.at_eof()) {
                    wait_more();
                    st.nss = std::max(start, seg_end - keep - margin);
                    continue;
                }
                st.nss = std::max(start + 1, seg_end - keep - margin);
                if (reader.at_eof() && seg_end >= buf.len()) { FragResult r; r.status = "eof"; return r; }
                continue;
            }
            long long lo = std::max(std::max(start + (long long)t0 - margin, buf.origin), 0LL);
            t0 = start + t0 - lo;
            long long hdr_end = lo + (long long)t0 + (long long)(g.PRE_HF + g.N + g.L + 8) * SC_SPS + margin;
            while (true) { get_samples(); if (buf.len() >= hdr_end || reader.at_eof()) break; wait_more(); }
            get_samples();
            std::vector<cd> hseg = c64(slice(lo, std::min(buf.len(), hdr_end)));
            std::vector<cd> uw = to_cd(s.uws[variant]);
            std::vector<cd> r0 = sh_grid(s, g, hseg.data(), (long long)hseg.size(), t0, cfo, g.PRE_HF + g.N);
            int n_bytes = sh_header(s, g, r0, uw).n_bytes;
            if (n_bytes < 0 || (a.fragment_size && n_bytes > 2 * (a.fragment_size + 6))) {
                FragResult r;
                r.status = "failed";
                r.has_next = true;
                r.next = lo + (long long)t0 + (long long)g.PRE_HF * SC_SPS / 2;
                return r;
            }
            double frame_start = lo + t0 - S_RRC_DELAY;
            long long fs_ = md.sh_frame_samples(n_bytes);
            long long end = (long long)(frame_start + fs_ + (double)g.L * SC_SPS + margin);
            while (true) { get_samples(); if (buf.len() >= end || reader.at_eof()) break; wait_more(); }
            get_samples();
            if (buf.len() < end - margin - (long long)g.L * SC_SPS - 2 * S_RRC_DELAY) {
                FragResult r;
                r.status = "incomplete";
                return r;
            }
            std::vector<cd> dseg = c64(slice(lo, std::min(end, buf.len())));
            if ((long long)dseg.size() < end - lo) dseg.resize(end - lo, cd(0, 0));
            long long n_sym = g.PRE_HF + (long long)g.N * (1 + md.sh_n_blocks(n_bytes));
            std::vector<cd> r1 = sh_grid(s, g, dseg.data(), (long long)dseg.size(), t0, cfo, n_sym);
            cd acc(0, 0);
            for (long long b = 0; b + 1 < md.sh_n_blocks(n_bytes) + 2; ++b) {
                long long a0 = g.PRE_HF - g.UW + b * g.N, a1 = a0 + g.N;
                for (int q = 0; q < g.UW; ++q) acc += std::conj(r1[a0 + q]) * r1[a1 + q];
            }
            cfo += std::atan2(acc.imag(), acc.real()) / (2 * fft::PI * g.N);
            r1 = sh_grid(s, g, dseg.data(), (long long)dseg.size(), t0, cfo, n_sym);
            SHHeader hd = sh_header(s, g, r1, uw);
            sh_decode(s, g, r1, n_bytes, hd, uw, md.hres);
            FragResult r;
            r.status = "ok";
            r.payload = md.hres.payload;
            r.crc_ok = md.hres.crc_ok;
            r.cfo_hz = cfo * M.symbol_rate;
            r.ber = md.hres.ber;
            r.evm = 100 * std::pow(10.0, -hd.pre_snr_db / 20);
            r.constellation = c64(md.hres.d);
            r.has_frame_start = true;
            r.frame_start = (long long)frame_start;
            r.has_next = true;
            r.next = (long long)(frame_start + fs_) - 2 * SC_SPS;
            return r;
        }
    }

    FragResult receive_one() {
        if (m().sc == 1) return receive_s();
        if (m().sc == 2) return receive_sh();
        return receive_ofdm();
    }

    // ---- AFC (_afc_update / _afc_applied)
    double afc_applied(const FragResult& r) const {
        double applied = 0;
        for (const auto& h : st.afc_hist)
            if (r.has_frame_start && r.frame_start >= h.first) applied = h.second;
        return applied;
    }
    void afc_update(double cfo_hz, const FragResult& r) {
        FrontEnd* fe = reader.fe;
        if (!fe) return;
        if (!st.afc_init) { st.afc_init = true; st.afc_base = fe->lo_hz; st.afc_hz = 0; st.afc_from = 0; }
        if (!r.has_frame_start || r.frame_start < st.afc_from) return;
        int hb = 0;
        for (int b : m().data_bins) hb = std::max(hb, std::abs(b));
        double hi = (hb + 0.5) * m().spacing;
        double room = std::max(m().fs / 2 - hi, m().spacing);
        int n = st.afc_n;
        if (n && std::fabs(cfo_hz - st.afc_ema) > 0.5 * m().spacing) { st.afc_ema = cfo_hz; st.afc_n = 1; return; }
        double ema = n == 0 ? cfo_hz : AFC_EMA_ALPHA * cfo_hz + (1 - AFC_EMA_ALPHA) * st.afc_ema;
        st.afc_ema = ema;
        st.afc_n = n + 1;
        if (st.afc_n < AFC_MIN_FRAGMENTS || std::fabs(ema) < AFC_ROOM_FRAC * room) return;
        st.afc_hz -= ema;
        {
            std::lock_guard<std::mutex> g(reader.fe_mu);   // the front end runs on the input thread
            st.afc_from = fe->n_out;
            st.afc_hist.push_back({fe->n_out, st.afc_hz});
            fe->set_lo_offset(st.afc_base + st.afc_hz);
        }
        if (st.afc_hist.size() > 8) st.afc_hist.erase(st.afc_hist.begin(), st.afc_hist.end() - 8);
        st.afc_n = 0;
        st.last_cfo.has = false;
        avm_logf("[afc] re-centred by %+.0f Hz (total correction %+.0f Hz)\n", -ema, st.afc_hz);
    }

    void note_trouble() {
        st.consecutive_failures += 1;
        if (st.consecutive_failures >= REACQUISITION_TROUBLE_LIMIT && st.last_cfo.has) {
            avm_logf("  ! %d+ consecutive fragments without a confirmed decode -- clearing the "
                         "CFO-continuity prior in case the transmitter was restarted (a real oscillator re-tune "
                         "can shift CFO enough to either deadlock the jump-limit gate, or bias "
                         "resolve_integer_cfo's own search onto a wrong-but-plausible candidate, against a "
                         "now-stale prior).\n", REACQUISITION_TROUBLE_LIMIT);
            st.last_cfo.has = false;
            st.consecutive_failures = 0;
        }
    }

    static std::string retry_note(const FragResult& r) {
        char b[96];
        std::string s;
        if (r.retries || r.waits) { std::snprintf(b, sizeof b, "  [retries=%d waits=%d]", r.retries, r.waits); s += b; }
        if (r.has_compute) { std::snprintf(b, sizeof b, "  compute=%.2fs", r.compute); s += b; }
        return s;
    }

    // ---- the live constellation for the GUI (_emit_constellation)
    double const_t = -10;
    bool const_on = false;
    void emit_constellation(const std::vector<cd>& pts) {
        double now = now_s();
        if (now - const_t > 1.0) {
            const_t = now;
            struct stat sb;
            const_on = ::stat("/dev/shm/avm_constellation", &sb) == 0;
        }
        if (!const_on || pts.empty()) return;
        const size_t MAXP = 3000;
        std::vector<cf> out;
        if (pts.size() > MAXP) {
            for (size_t i = 0; i < MAXP; ++i) {
                double pos = (double)(pts.size() - 1) * i / (MAXP - 1);
                const cd& p = pts[(size_t)pos];
                out.push_back(cf((float)p.real(), (float)p.imag()));
            }
        } else {
            for (const cd& p : pts) out.push_back(cf((float)p.real(), (float)p.imag()));
        }
        static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        const unsigned char* d = (const unsigned char*)out.data();
        size_t n = out.size() * sizeof(cf);
        std::string s;
        for (size_t i = 0; i < n; i += 3) {
            unsigned v = d[i] << 16 | (i + 1 < n ? d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
            s += B64[(v >> 18) & 63];
            s += B64[(v >> 12) & 63];
            s += i + 1 < n ? B64[(v >> 6) & 63] : '=';
            s += i + 2 < n ? B64[v & 63] : '=';
        }
        avm_logf("[constellation] %s\n", s.c_str());
    }

    // ---- duplex (hf_ofdm_rx.py _tdd_geometry .. _tdd_skip_gap)
    // the far end's TDD pattern in native (mode-rate) samples, worked out the
    // way the transmitter's run_tdd does: whole fragments per slot, each as long
    // on air as it makes it (resample padding included, at the radio's rate)
    void tdd_geometry() {
        auto t = std::make_unique<Tdd>();
        Mode tm;
        std::string err;
        // the far end's frames: with --fec auto, LDPC (what AVM's TX sends)
        build_mode(a.mode, a.occupancy, a.modulation, a.fec == "auto" ? "ldpc" : a.fec, tm, err);
        Modem tmd(tm);
        std::vector<uint8_t> zeros(FRAG_HEADER_SIZE + std::max(0, a.fragment_size), 0);
        if (tm.sc) { tmd.sc->build(zeros.data(), zeros.size()); t->n_frame = (long long)tmd.sc->frame.size(); }
        else { build_frame(*tmd.c, zeros.data(), zeros.size()); t->n_frame = (long long)tmd.c->frame.size(); }
        const double fs = m().fs;
        double radio_fs = reader.fe ? reader.fe->in_fs : (a.sample_rate ? a.sample_rate : fs);
        long long on_air = radio_fs != fs ? std::llround((t->n_frame + resample_pad(t->n_frame, fs, radio_fs)) * radio_fs / fs)
                                          : t->n_frame;
        if (a.fragment_gap_ms > 0) on_air += std::llround(radio_fs * a.fragment_gap_ms / 1000);
        t->per_slot = std::max(1, (int)(std::llround(a.tdd_slot_s * radio_fs) / on_air));
        t->step = on_air * fs / radio_fs;
        t->cycle = a.tdd_cycle_s * fs;
        t->burst = t->per_slot * t->step;
        t->margin = TDD_MARGIN_S * fs;
        t->fs = fs;
        t->radio_fs = radio_fs;
        t->station = a.tdd_station;
        t->adjust_file = a.tdd_adjust_file;
        t->report_file = a.tdd_report_file;
        t->source = a.tdd_tx ? radio_src : nullptr;
        t->feeder = t->source ? t->source->tdd_feeder() : nullptr;
        t->target = (a.tdd_offset_s ? a.tdd_offset_s : a.tdd_cycle_s / 2) * fs;
        tdd_ = std::move(t);
        if (tdd_view) {
            std::lock_guard<std::mutex> g(tdd_view->mu);
            tdd_view->active = true;
            tdd_view->cycle = tdd_->cycle;
            tdd_view->burst = tdd_->burst;
            tdd_view->margin = tdd_->margin;
        }
    }
    void tdd_publish() {
        if (!tdd_view) return;
        Tdd& t = *tdd_;
        std::lock_guard<std::mutex> g(tdd_view->mu);
        tdd_view->heard = t.heard;
        tdd_view->has_phase = t.phase.has;
        tdd_view->phase = t.phase.v;
        tdd_view->has_own = t.own_phase.has;
        tdd_view->own_phase = t.own_phase.v;
    }
    // our own slot, from this process's TX sample count
    void tdd_own_refresh() {
        Tdd& t = *tdd_;
        if (!t.feeder) return;
        long long nb = t.feeder->bursts();
        if (nb > t.own_seen) {
            t.hold = (int)std::max(0LL, t.hold - (nb - t.own_seen));
            t.own_seen = nb;
            t.own_phase.has = true;
            t.own_phase.v = (t.feeder->last_start() - t.source->discarded()) * t.fs / t.radio_fs;
        }
    }
    // a slave, combined TX/RX: transmit only while placed in the master's gap
    void tdd_set_muted(bool muted, const char* why) {
        Tdd& t = *tdd_;
        if (!t.feeder || t.feeder->mute_request == muted) return;
        t.feeder->mute_request = muted;      // (takes effect at the next gap between bursts)
        avm_logf("[tdd] slave TX %s: %s\n", muted ? "silent" : "on air", why);
    }
    static double wrap(double x, double c) { return pyfmod(x + c / 2, c) - c / 2; }   // -c/2 .. c/2
    // where the other station's slot is really heard, relative to ours, every TDD_REPORT_EVERY_S
    void tdd_report_far() {
        Tdd& t = *tdd_;
        tdd_own_refresh();
        double now = now_s();
        if (!t.own_phase.has || now - t.reported < TDD_REPORT_EVERY_S) return;
        t.reported = now;
        double rel = pyfmod(t.phase.v - t.own_phase.v, t.cycle);
        double want = pyfmod(t.adjust_file.empty() ? t.target : -t.target, t.cycle);
        double err = wrap(rel - want, t.cycle);
        avm_logf("[tdd] other station's slot heard %.1f ms after ours (wanted %.1f ms: %+.1f ms off)\n",
                 rel / t.fs * 1e3, want / t.fs * 1e3, err / t.fs * 1e3);
    }
    // master: where the slave's slot is heard, for our modulator to send in its headers
    void tdd_master_report(double burst_start) {
        Tdd& t = *tdd_;
        tdd_own_refresh();
        if (!t.own_phase.has) return;
        if (t.rep_burst.has && std::fabs(burst_start - t.rep_burst.v) < t.cycle / 2) return;   // reported already
        t.rep_burst.has = true;
        t.rep_burst.v = burst_start;
        double err = wrap(pyfmod(burst_start - t.own_phase.v, t.cycle) - t.target, t.cycle);
        t.rep_ctr = (t.rep_ctr + 1) % 256;
        char b[64];
        std::snprintf(b, sizeof b, "%d %lld", t.rep_ctr, std::llround(err / t.fs * 1e4));
        if (!write_atomic(t.report_file, b)) avm_logf("[tdd] couldn't write %s\n", t.report_file.c_str());
    }
    bool write_shift() {
        Tdd& t = *tdd_;
        char b[64];
        std::snprintf(b, sizeof b, "%.9f", t.shift_s);
        if (write_atomic(t.adjust_file, b)) return true;
        avm_logf("[tdd] couldn't write %s\n", t.adjust_file.c_str());
        return false;
    }
    // slave: steer by the master's report of where it hears our slot (the slot as on air)
    void tdd_follow_report(uint32_t field) {
        Tdd& t = *tdd_;
        if (!(field & TDD_REPORT_FLAG)) return;
        int ctr = (int)((field >> 16) & 0xFF);
        int err01 = (int)(field & 0xFFFF);
        if (err01 & 0x8000) err01 -= 0x10000;
        if (ctr == t.last_ctr) return;
        t.last_ctr = ctr;
        double now = now_s();
        t.closed_t = now;
        double err_s = err01 * 1e-4;
        if (now < t.closed_hold_t || std::fabs(err_s) < TDD_CLOSED_MIN_S) return;
        t.shift_s -= err_s;
        t.closed_hold_t = now + TDD_CLOSED_HOLD_S;
        if (!write_shift()) return;
        avm_logf("[tdd] master hears our slot %+.1f ms off: moving it (total %+.1f ms)\n", err_s * 1e3, t.shift_s * 1e3);
    }
    // slave: keep its own slot `target` after the master's (both in this receiver's count)
    void tdd_steer() {
        Tdd& t = *tdd_;
        tdd_own_refresh();
        if (t.adjust_file.empty() || !t.own_phase.has || !t.phase.has || !t.heard) return;
        if (t.hold) return;      // a move is on its way through the modulator
        double err = pyfmod(t.own_phase.v - (t.phase.v + t.target), t.cycle);
        if (err > t.cycle / 2) err -= t.cycle;        // + = own slot late
        if (std::fabs(err) < TDD_UNMUTE_S * t.fs) tdd_set_muted(false, "slot in place");
        if (now_s() - t.closed_t < TDD_CLOSED_S) return;   // the master's reports steer now
        if (std::fabs(err) < TDD_ADJUST_MIN_S * t.fs) return;
        t.shift_s -= err / t.fs;
        t.own_phase.v -= err;
        if (t.source) t.hold = 3;     // own bursts already queued still show the old timing
        if (!write_shift()) return;
        avm_logf("[tdd] own slot %+.3f ms off target: moving it (total %+.3f ms)\n", err / t.fs * 1e3, t.shift_s * 1e3);
    }
    // learn the slot timing from a good fragment; true: our OWN fragment (not for output)
    bool tdd_track(const FragResult& r) {
        Tdd& t = *tdd_;
        if (r.status != "ok" || !r.crc_ok || !r.has_next) {
            if (t.phase.has && st.nss - t.phase.v > TDD_LOST_CYCLES * t.cycle) {
                if (!t.heard) { t.phase.has = false; return false; }   // just a guess: guess again, quietly
                avm_logf("[tdd] no burst for a few cycles -- searching everything again\n");
                t.phase.has = t.last_ok.has = false;
                t.heard = false;
                if (!t.adjust_file.empty()) tdd_set_muted(true, "lost the master");
            }
            return false;
        }
        double start = (double)(r.next - t.n_frame);
        unsigned total = 0;
        uint32_t len_field = 0;
        if ((int)r.payload.size() >= FRAG_HEADER_SIZE) {
            const uint8_t* p = r.payload.data();
            total = (unsigned)p[2] << 8 | p[3];
            len_field = (uint32_t)p[4] << 24 | (uint32_t)p[5] << 16 | (uint32_t)p[6] << 8 | p[7];
        }
        if (total & TDD_HEADER_FLAG) {
            double burst_start = start - ((total >> 8) & 0x3F) * t.step;
            if (t.station >= 0 && ((total & TDD_STATION_BIT) != 0) == (t.station != 0)) {
                if (!t.own_phase.has && !t.source) {     // (combined: from the TX count)
                    t.own_phase.has = true;
                    t.own_phase.v = burst_start;
                    avm_logf("[tdd] own slot found (station %d)\n", t.station);
                    tdd_steer();
                }
                return true;
            }
            t.phase.has = true;
            t.phase.v = burst_start;      // the TX says where in its slot this is: exact
            t.heard = true;
            tdd_report_far();
            if (!t.report_file.empty()) tdd_master_report(burst_start);
            else if (!t.adjust_file.empty()) tdd_follow_report(len_field);
        } else if (!t.last_ok.has || start - t.last_ok.v > 1.5 * t.step) {
            t.phase.has = true;           // (older TX) first fragment after a gap: assume a burst start
            t.phase.v = start;
            t.heard = true;
        } else if (t.phase.has) {
            double k = std::nearbyint((start - t.phase.v) / t.step);
            t.phase.v = start - k * t.step;
        }
        t.last_ok.has = true;
        t.last_ok.v = start;
        tdd_steer();
        return false;
    }
    // past the end of the expected burst: jump to just before the next one
    void tdd_skip_gap() {
        Tdd& t = *tdd_;
        tdd_own_refresh();
        double pos = (double)st.nss;
        if (!t.heard && t.source && t.own_phase.has && t.adjust_file.empty()) {
            // master, slave not heard yet: search everywhere except our own slot
            double into = pyfmod(pos - (t.own_phase.v - t.margin), t.cycle);
            if (into < t.burst + 2 * t.margin) st.nss = (long long)(pos + (t.burst + 2 * t.margin - into));
            return;
        }
        if (!t.phase.has) return;
        if (!t.adjust_file.empty() && !t.own_phase.has && !t.source) return;   // a slave still finding its own slot
        double into = pyfmod(pos - t.phase.v, t.cycle);
        // far end not heard yet: just the start of the slot (searching it all loaded the CPU)
        double window = t.heard ? t.burst - 0.5 * t.step : std::min(t.burst - 0.5 * t.step, TDD_PROBE_S * t.fs);
        if (into > window && into < t.cycle - t.margin) st.nss = (long long)(pos + (t.cycle - into) - t.margin);
    }

    // ---- run_decode_loop
    void run() {
        const Mode& M = m();
        if (a.wait_eof) while (!reader.at_eof()) reader.wait_for_more(reader.total());
        long long fragments_written = 0, fragments_dropped = 0, bytes_written = 0;
        Opt expected_next;
        Pacer pacer(a.rx_queue_depth, !a.no_output_pacing, out);
        double skip_lag = 0, skip_keep = 0;
        {
            double period = 0.6;
            if (a.fragment_size) period = a.fragment_size * 8 / md.effective_bitrate(a.fragment_size, a.fragment_gap_ms);
            skip_keep = SKIP_KEEP_FRAGMENTS * period + 0.1;
            skip_lag = skip_keep + SKIP_EXTRA_LAG_S;
        }
        if (a.drop_if_slow)
            avm_logf("Catch-up: skip to live once %.2fs behind (keeping %.2fs)\n", skip_lag, skip_keep);
        if (a.tdd_cycle_s) {
            tdd_geometry();
            if (!a.tdd_adjust_file.empty()) tdd_set_muted(true, "until it hears the master");
            avm_logf("[tdd] far end sends %d fragment(s) (%.1f ms) every %.0f ms; the rest of each cycle is skipped\n",
                     tdd_->per_slot, tdd_->burst / M.fs * 1000, a.tdd_cycle_s * 1000);
        }
        while (!reader.stopping) {
            long long before = st.nss;
            double t0 = now_s();
            FragResult r = receive_one();
            double elapsed = now_s() - t0;
            if (r.has_next) st.nss = r.next;
            if (r.status == "eof") break;
            bool own = tdd_ ? tdd_track(r) : false;
            if (a.drop_if_slow) {
                double live = reader.total() / 8.0;
                double lag = (live - st.nss) / M.fs;
                if (lag > skip_lag) {
                    st.nss = (long long)(live - skip_keep * M.fs);
                    avm_logf("  -> SKIPPED %.2fs of backlog to catch up with live (decoding fell %.2fs behind)\n",
                                 lag - skip_keep, lag);
                }
            }
            if (tdd_) {
                tdd_skip_gap();
                tdd_publish();
            }
            if (r.status == "failed") {
                double dur = (st.nss - before) / M.fs;
                avm_logf("Fragment: FAILED (garbage header -- bad sync or no signal)  Time=%.2fs (scanned %.2fs)%s\n",
                             elapsed, dur, retry_note(r).c_str());
                note_trouble();
                continue;
            }
            if (r.status == "incomplete") {
                avm_logf("Fragment: INCOMPLETE (stream ended before enough samples arrived)  Time=%.2fs\n", elapsed);
                break;
            }
            if (own) continue;   // our own transmission, heard through the radio's leakage: not for output
            if ((int)r.payload.size() < FRAG_HEADER_SIZE) {
                avm_logf("Fragment: FAILED (payload too short to contain fragment header)\n");
                continue;
            }
            const uint8_t* p = r.payload.data();
            unsigned frag_idx = (unsigned)p[0] << 8 | p[1];
            std::string data((const char*)p + FRAG_HEADER_SIZE, r.payload.size() - FRAG_HEADER_SIZE);
            emit_constellation(r.constellation);
            double fade = 0;
            if (!r.constellation.empty()) {
                long long nf = 0;
                double lim = 3.0 * (1.0 / std::sqrt(2.0));   // deep_fade_fraction's 3 x max |QPSK level|
                for (const cd& q : r.constellation) nf += std::abs(q) > lim;
                fade = (double)nf / r.constellation.size();
            }
            const char* status_word = r.crc_ok ? "OK" : "CRC MISMATCH";
            double search_dur = (st.nss - before) / M.fs;
            double signal_dur = r.has_frame_start ? (st.nss - r.frame_start) / M.fs : search_dur;
            bool slow = elapsed > search_dur + 0.15;
            char gap_note[96] = "";
            if (search_dur > signal_dur + 1.0) std::snprintf(gap_note, sizeof gap_note, "  (spans a %.2fs search gap)", search_dur);
            char time_note[256];
            std::snprintf(time_note, sizeof time_note, "  Time=%.2fs (budget %.2fs)%s%s%s", elapsed, search_dur,
                          slow ? " <-- SLOWER THAN REALTIME" : "", retry_note(r).c_str(), gap_note);
            double cfo_shown = pyfmod(r.cfo_hz + M.fs / 2, M.fs) - M.fs / 2;
            double cfo_full = cfo_shown - afc_applied(r);
            avm_logf("Fragment seq=%u: %s  CFO=%+.1fHz  BER=%.4f  EVM=%.1f%%  DeepFade=%.2f%%%s\n",
                         frag_idx, status_word, cfo_full, r.ber, r.evm, fade * 100, time_note);
            if (!r.crc_ok) {
                afc_update(cfo_shown, r);
                note_trouble();
                continue;
            }
            st.last_cfo.v = st.last_cfo.has ? CFO_EMA_ALPHA * r.cfo_hz + (1 - CFO_EMA_ALPHA) * st.last_cfo.v : r.cfo_hz;
            st.last_cfo.has = true;
            st.consecutive_failures = 0;
            afc_update(cfo_shown, r);
            double now_ok = now_s();
            if (expected_next.has && frag_idx != (unsigned)expected_next.v) {
                long long gap = ((long long)frag_idx - (long long)expected_next.v) % 65536;
                if (gap < 0) gap += 65536;
                double since = now_ok - (st.last_ok_time >= 0 ? st.last_ok_time : now_ok);
                if (gap > since / SEQ_MIN_FRAGMENT_S + SEQ_GAP_SLACK)
                    avm_logf("  ! transmitter restarted: seq %lld -> %u\n", (long long)expected_next.v - 1, frag_idx);
                else
                    avm_logf("  ! sequence gap: expected seq=%lld, got seq=%u (%lld fragment(s) lost -- no return channel to request a resend)\n",
                                 (long long)expected_next.v, frag_idx, gap);
            }
            expected_next.has = true;
            expected_next.v = (frag_idx + 1) % 65536;
            st.last_ok_time = now_ok;
            double lag = reader.total() / 8.0 / M.fs - st.nss / M.fs;
            if (slow && a.drop_if_slow && lag > DROP_IF_SLOW_LAG_S) {
                ++fragments_dropped;
                avm_logf("  -> DROPPED (slower than realtime, %.2fs behind live, seq=%u, %lld dropped so far)\n",
                             lag, frag_idx, fragments_dropped);
                continue;
            }
            if (slow && a.drop_if_slow) avm_logf("  -> kept (only %.2fs behind live)\n", lag);
            pacer.submit(data, signal_dur, frag_idx);
            ++fragments_written;
            bytes_written += (long long)data.size();
            avm_logf("  -> wrote %zu bytes (seq=%u, %lld total so far)\n", data.size(), frag_idx, bytes_written);
        }
        if (reader.stopping) { pacer.abort(); return; }
        pacer.drain();
        if (fragments_dropped)
            avm_logf("Stream ended: wrote %lld bytes across %lld fragment(s), dropped %lld slow fragment(s).\n",
                         bytes_written, fragments_written, fragments_dropped);
        else
            avm_logf("Stream ended: wrote %lld bytes across %lld fragment(s).\n", bytes_written, fragments_written);
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
        else if (k == "--no-output-pacing") a.no_output_pacing = true;
        else if (k == "--invert-spectrum") a.invert_spectrum = true;
        else if (k == "--rx-queue-depth") a.rx_queue_depth = std::atoi(val().c_str());
        else if (k == "--drop-if-slow") a.drop_if_slow = true;
        else if (k == "--test-step") a.test_step = std::atoll(val().c_str());
        else if (k == "--lo-offset-hz") a.lo_offset_hz = std::atof(val().c_str());
        else if (k == "--input") a.input = val();
        else if (k == "--sdr") a.sdr = val();
        else if (k == "--rf-freq") a.rf_freq = std::atof(val().c_str());
        else if (k == "--rx-gain") a.rx_gain = std::atof(val().c_str());
        else if (k == "--rx-agc") a.agc = true;
        else if (k == "--pluto-uri") a.uri = val();
        else if (k == "--pluto-bandwidth") a.bandwidth = std::atof(val().c_str());
        else if (k == "--sdr-antenna") a.antenna = val();
        else if (k == "--sdr-ppm") a.ppm = std::atof(val().c_str());
        else if (k == "--rx-warmup-s") a.warmup_s = std::atof(val().c_str());
        else if (k == "--verbose") {}
        else if (k == "--spectrum-stderr" || k == "--spectrum") a.spectrum = true;
        else if (k == "--spectrum-span-hz") a.spectrum_span = std::atof(val().c_str());
        else if (k == "--gain-file") a.gain_file = val();
        else if (k == "--freq-offset-file") a.freq_offset_file = val();
        else if (k == "--sdr-ppm-file") a.ppm_file = val();
        else if (k == "--tdd-cycle-s") a.tdd_cycle_s = std::atof(val().c_str());
        else if (k == "--tdd-slot-s") a.tdd_slot_s = std::atof(val().c_str());
        else if (k == "--tdd-offset-s") a.tdd_offset_s = std::atof(val().c_str());
        else if (k == "--tdd-station") a.tdd_station = std::atoi(val().c_str());
        else if (k == "--tdd-adjust-file") a.tdd_adjust_file = val();
        else if (k == "--tdd-report-file") a.tdd_report_file = val();
        else if (k == "--tdd-tx") a.tdd_tx = true;
        else if (k == "--tdd-tx-gain") a.tdd_tx_gain = std::atof(val().c_str());
        else { avm_logf("avm_rx: unsupported option %s (the Python receiver has it)\n", k.c_str()); return false; }
        if (missing) { avm_logf("%s needs a value\n", k.c_str()); return false; }
    }
    const char* env = std::getenv("HF_RX_ASSUME_PRIOR_CFO");
    a.assume_prior = !(env && std::string(env) == "0");
    env = std::getenv("HF_RX_SYNC_FAST_SYMBOLS");
    if (env) a.sync_fast_symbols = std::atoi(env);
    env = std::getenv("HF_OFDM_RX_WAIT_EOF");
    a.wait_eof = env && *env;
    return true;
}

// one receiver: its options, front end, input, radio, and threads
struct Engine {
    avm_rx_callbacks cb{};
    LogSink sink;
    Args a;
    Mode m;
    std::unique_ptr<FrontEnd> fe;
    Reader reader;
    std::unique_ptr<sdr::RadioIn> radio;
    TddView tdd_view;
    std::thread rx_th, spectrum_th;
    std::atomic<bool> running{false}, stop_spectrum{false};

    // set up (options, mode, radio); false: the reason has been logged
    bool open(int argc, const char* const* argv) {
        if (!parse_args(argc, argv, a)) return false;
        if (a.tdd_cycle_s && !a.tdd_slot_s) a.tdd_slot_s = a.tdd_cycle_s / 2;
        if (a.tdd_tx && (!a.tdd_cycle_s || a.input == "pipe" || a.sdr != "pluto")) {
            avm_logf("ERROR: --tdd-tx needs --tdd-cycle-s and a PlutoSDR (--input pluto --sdr pluto)\n");
            return false;
        }
        std::string fec = a.fec == "auto" ? "viterbi" : a.fec;
        std::string err;
        if (!build_mode(a.mode, a.occupancy, a.modulation, fec, m, err)) {
            avm_logf("ERROR: %s\n", err.c_str());
            return false;
        }
        avm_logf("Mode %s: %.1f Hz sample rate, %s, %s (C++ receiver)\n", m.mode.c_str(), m.fs,
                 m.modulation.c_str(), m.ldpc ? "LDPC" : "Viterbi");
        if (!a.fragment_size)
            avm_logf("WARNING: --fragment-size not set -- a bad header can still stall for multiple seconds "
                     "on a wrongly-implied huge frame instead of failing fast. Pass the same --fragment-size "
                     "used on the TX side unless you're truly sending one unfragmented message.\n");
        double capture_fs = a.sample_rate ? a.sample_rate : m.fs;
        if (capture_fs == m.fs) {
            avm_logf("ERROR: avm_rx needs --sample-rate different from the mode's rate (%.1f Hz): it runs "
                     "the compiled front end; the Python receiver handles input already at the mode's rate\n", m.fs);
            return false;
        }
        if (a.input != "pipe") {
            if (!a.rf_freq) { avm_logf("ERROR: --input pluto requires --rf-freq.\n"); return false; }
            if (a.sdr == "virtual") {     // the virtual transmitter on this machine (net_radios.h)
                auto* v = new netradio::VirtualRxSource;
                radio.reset(v);
                // the signal where a Pluto tuned lo_offset above would put it (the front end shifts it back)
                if (!v->open(a.rf_freq, capture_fs, -a.lo_offset_hz, a.freq_offset_file)) {
                    avm_logf("ERROR: %s\n", v->error.c_str());
                    radio.reset();
                    return false;
                }
            } else if (a.sdr == "kiwi") {  // a KiwiSDR over the network (--pluto-uri: host:port), its own rate
                auto* k = new netradio::KiwiRxSource;
                radio.reset(k);
                if (!k->open(a.uri, a.rf_freq, a.rx_gain, a.gain_file, a.freq_offset_file)) {
                    avm_logf("ERROR: %s\n", k->error.c_str());
                    radio.reset();
                    return false;
                }
                capture_fs = k->rate;
                a.lo_offset_hz = 0;     // IQ arrives centred on the frequency
            } else if (a.sdr == "hpsdr") {     // protocol 1 over the network (Hermes-Lite 2, Radioberry): hpsdr1.h
                auto* h = new hpsdr1::RxSource;
                radio.reset(h);
                if (!h->open(a.uri, a.rf_freq, 1.3 * a.occupancy * 1e3, a.rx_gain, a.gain_file, a.freq_offset_file)) {
                    avm_logf("ERROR: %s\n", h->error.c_str());
                    radio.reset();
                    return false;
                }
                capture_fs = h->rate;
                a.lo_offset_hz = 0;     // its DDC centres the signal
            } else {
            sdr::RxConfig rc;
            rc.driver = a.sdr; rc.uri = a.uri; rc.antenna = a.antenna; rc.rf_hz = a.rf_freq; rc.rate = capture_fs;
            rc.gain_db = a.rx_gain; rc.agc = a.agc; rc.bandwidth_hz = a.bandwidth; rc.tune_offset_hz = a.lo_offset_hz;
            rc.ppm = a.ppm; rc.warmup_s = a.warmup_s; rc.gain_file = a.gain_file; rc.freq_offset_file = a.freq_offset_file; rc.ppm_file = a.ppm_file;
            rc.tdd_tx = a.tdd_tx; rc.tdd_tx_gain = a.tdd_tx_gain; rc.tdd_start_muted = !a.tdd_adjust_file.empty();   // (a slave)
            auto* r = new sdr::RxSource;
            radio.reset(r);
            if (!r->open(rc)) { avm_logf("ERROR: %s\n", r->error.c_str()); radio.reset(); return false; }
            if (radio->rate != capture_fs) {   // the radio rounded it: the front end works at its real rate
                avm_logf("Radio rate %.3f Hz (asked %.3f)\n", radio->rate, capture_fs);
                capture_fs = radio->rate;
            }
            }
        }
        std::vector<double> taps = design_lowpass_fir(std::min(m.fs / 2, 0.45 * capture_fs), capture_fs, 129);
        // (inverted: the LO offset mirrors with the spectrum, so it's removed the other way)
        const double lo = a.invert_spectrum ? -a.lo_offset_hz : a.lo_offset_hz;
        fe.reset(new FrontEnd(capture_fs, m.fs, taps, lo));   // a radio tuned this far off: shifted back here
        reader.invert = a.invert_spectrum;
        if (a.invert_spectrum) avm_logf("Spectrum inverted (--invert-spectrum)\n");
        avm_logf("Front end: %.1f kS/s -> %.2f kS/s (compiled IQ correction, LO shift %+.1f kHz, 129-tap FIR at output points only)\n",
                 capture_fs / 1e3, m.fs / 1e3, lo / 1e3);
        reader.fe = fe.get();
        return true;
    }

    void start() {
        running = true;
        if (radio) reader.start_radio(radio.get());
        else if (!a.test_step) reader.start_live();
        reader.want_raw = a.spectrum && a.spectrum_span > 0;
        if (a.spectrum) spectrum_th = std::thread([this] { log_sink() = sink; spectrum_loop(); });
        rx_th = std::thread([this] {
            log_sink() = sink;
            if (a.test_step) reader.start_test(a.test_step);
            Receiver rx(a, m, reader, [this](const char* p, size_t n) {
                if (cb.data) cb.data(cb.user, (const unsigned char*)p, n);
            });
            rx.radio_src = radio.get();
            rx.tdd_view = &tdd_view;
            rx.run();
            running = false;
            if (cb.done && !reader.stopping) cb.done(cb.user);
        });
    }

    void stop() {
        reader.stop();                       // the receive loop and the input thread end
        if (rx_th.joinable()) rx_th.join();
        stop_spectrum = true;
        if (spectrum_th.joinable()) spectrum_th.join();
        radio.reset();
    }

    // the spectrum (run_spectrum_stderr's lines): 4 a second, the newest
    // 8 x 512 samples Hann-windowed, |FFT|^2 averaged over them and the last 8
    // updates, as "[spectrum] <freq0_hz> <freq_step_hz> <n> <base64 float32 dB>"
    void spectrum_loop() {
        // the raw samples when the span is wider than the modem's rate -- or when the
        // radio's rate is below it (a KiwiSDR's 12 kS/s under mode SH's 32k: the
        // modem-rate view was mostly empty band), the span then capped to the radio's
        if (a.spectrum_span > 0 && fe && (a.spectrum_span > m.fs || fe->in_fs < m.fs)) {
            raw_span = std::min(a.spectrum_span, 0.92 * fe->in_fs);
            raw_spectrum_loop();
            return;
        }
        const int N = 512, SEGS = 8, AVG = 8;
        std::vector<double> win(N);
        for (int i = 0; i < N; ++i) win[i] = 0.5 - 0.5 * std::cos(2 * fft::PI * i / (N - 1));
        std::deque<std::vector<double>> hist;
        std::vector<cf> latest(N * SEGS);
        std::vector<cd> in(N), out(N);
        while (!stop_spectrum) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            {
                std::lock_guard<std::mutex> g(reader.mu);
                if (reader.total_len < (long long)N * SEGS * 8) continue;
                double end = reader.total_len / 8.0;
                if (!tdd_view.far_slot(end - N * SEGS, end)) continue;   // duplex: the other station's slot only
                size_t p = reader.recent_pos, R = reader.recent.size();
                for (int i = 0; i < N * SEGS; ++i) latest[i] = reader.recent[(p + R - N * SEGS + i) % R];
            }
            std::vector<double> pw(N, 0.0);
            for (int sgm = 0; sgm < SEGS; ++sgm) {
                for (int i = 0; i < N; ++i) in[i] = cd(latest[sgm * N + i].real(), latest[sgm * N + i].imag()) * win[i];
                fft::plan<double>(N).forward(in.data(), out.data());
                for (int k = 0; k < N; ++k) pw[(k + N / 2) % N] += std::norm(out[k]) / SEGS;   // fftshift
            }
            hist.push_back(pw);
            while ((int)hist.size() > AVG) hist.pop_front();
            std::vector<float> db(N);
            for (int k = 0; k < N; ++k) {
                double s = 0;
                for (const auto& h : hist) s += h[k];
                db[k] = (float)(10 * std::log10(s / hist.size() + 1e-12));
            }
            static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
            const unsigned char* d = (const unsigned char*)db.data();
            size_t n = db.size() * 4;
            std::string b64;
            for (size_t i = 0; i < n; i += 3) {
                unsigned v = d[i] << 16 | (i + 1 < n ? d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
                b64 += B64[(v >> 18) & 63];
                b64 += B64[(v >> 12) & 63];
                b64 += i + 1 < n ? B64[(v >> 6) & 63] : '=';
                b64 += i + 2 < n ? B64[v & 63] : '=';
            }
            avm_logf("[spectrum] %.6f %.6f %d %s\n", -N / 2 * m.fs / N, m.fs / N, N, b64.c_str());
        }
    }

    static std::string base64(const std::vector<float>& v) {
        static const char* B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
        const unsigned char* d = (const unsigned char*)v.data();
        size_t n = v.size() * 4;
        std::string s;
        for (size_t i = 0; i < n; i += 3) {
            unsigned x = d[i] << 16 | (i + 1 < n ? d[i + 1] << 8 : 0) | (i + 2 < n ? d[i + 2] : 0);
            s += B64[(x >> 18) & 63];
            s += B64[(x >> 12) & 63];
            s += i + 1 < n ? B64[(x >> 6) & 63] : '=';
            s += i + 2 < n ? B64[x & 63] : '=';
        }
        return s;
    }

    // hf_ofdm_rx._build_spectrum_computer's wide view: the raw samples, the LO
    // offset shifted out (the signal centred), low-passed (windowed sinc, Blackman)
    // and decimated to about the span, 512-point Hann FFTs (up to 8 a picture,
    // from at most 32768 raw samples) averaged over the last 8, the span kept
    double raw_span = 0;
    void raw_spectrum_loop() {
        const int N = 512, AVG = 8;
        const double fs = fe->in_fs, span = raw_span;
        const int decim = std::max(1, (int)(fs / span));
        std::vector<double> h;
        if (decim > 1) {
            int taps = 16 * decim + 1;
            h.resize(taps);
            double cutoff = 1.0 / decim, sum = 0;
            for (int i = 0; i < taps; ++i) {
                double n = i - taps / 2, x = cutoff * n;
                double sinc = x == 0 ? 1.0 : std::sin(fft::PI * x) / (fft::PI * x);
                double bl = 0.42 - 0.5 * std::cos(2 * fft::PI * i / (taps - 1)) + 0.08 * std::cos(4 * fft::PI * i / (taps - 1));
                h[i] = sinc * cutoff * bl;
                sum += h[i];
            }
            for (double& v : h) v /= sum;
        }
        const int segs = std::max(1, std::min(8, 32768 / (N * decim)));
        const long long need = (long long)N * decim * segs + (h.empty() ? 0 : (long long)h.size() - 1);
        const double dfs = fs / decim;
        // the kept bins: |f| <= span / 2 (fftshift(fftfreq(N, 1/dfs)): (j - N/2) dfs/N)
        int j0 = N, j1 = -1;
        for (int j = 0; j < N; ++j)
            if (std::fabs((j - N / 2) * dfs / N) <= span / 2) { j0 = std::min(j0, j); j1 = std::max(j1, j); }
        const double offset_db = a.input != "pipe" && a.sdr == "rtlsdr" ? -50.0 : 0.0;   // SPECTRUM_OFFSET_DB
        std::vector<double> win(N);
        for (int i = 0; i < N; ++i) win[i] = 0.5 - 0.5 * std::cos(2 * fft::PI * i / (N - 1));
        std::deque<std::vector<double>> hist;
        std::vector<cd> s(need), in(N), out(N);
        avm_logf("Spectrum: %.1f kHz around the signal from the raw %.1f kS/s (1/%d)\n", span / 1e3, fs / 1e3, decim);
        while (!stop_spectrum) {
            std::this_thread::sleep_for(std::chrono::milliseconds(250));
            {
                std::lock_guard<std::mutex> g(reader.raw_mu);
                if (reader.raw_total < need) continue;
                double end = reader.raw_total * m.fs / fs;
                if (!tdd_view.far_slot(end - need * m.fs / fs, end)) continue;   // duplex: the other station's slot only
                size_t p = reader.raw_pos, R = reader.raw.size();
                for (long long i = 0; i < need; ++i) {
                    const cf& v = reader.raw[(p + R - need + i) % R];
                    s[i] = cd(v.real(), v.imag());
                }
            }
            {   // the LO offset out, as the front end does (its phasor table)
                std::lock_guard<std::mutex> g(reader.fe_mu);
                if (fe->lo_hz != 0) {
                    if (fe->have_table && !fe->table.empty())
                        for (long long i = 0; i < need; ++i) s[i] *= fe->table[i % (long long)fe->table.size()];
                    else
                        for (long long i = 0; i < need; ++i)
                            s[i] *= std::polar(1.0, 2 * fft::PI * fe->lo_hz * (double)i / fs);
                }
            }
            std::vector<cd> d;
            if (h.empty()) {
                d = s;
            } else {
                for (long long m0 = 0; m0 + (long long)h.size() <= need; m0 += decim) {
                    cd acc(0, 0);
                    for (size_t k = 0; k < h.size(); ++k) acc += h[k] * s[m0 + (long long)k];
                    d.push_back(acc);
                }
            }
            int n = std::min(segs, (int)(d.size() / N));
            if (n < 1) continue;
            std::vector<double> pw(N, 0.0);
            size_t base = d.size() - (size_t)n * N;
            for (int b = 0; b < n; ++b) {
                for (int i = 0; i < N; ++i) in[i] = d[base + (size_t)b * N + i] * win[i];
                fft::plan<double>(N).forward(in.data(), out.data());
                for (int k = 0; k < N; ++k) pw[(k + N / 2) % N] += std::norm(out[k]) / n;
            }
            hist.push_back(pw);
            while ((int)hist.size() > AVG) hist.pop_front();
            std::vector<float> db;
            for (int k = j0; k <= j1; ++k) {
                double t = 0;
                for (const auto& x : hist) t += x[k];
                db.push_back((float)(10 * std::log10(t / hist.size() + 1e-12) + offset_db));
            }
            avm_logf("[spectrum] %.6f %.6f %d %s\n", (j0 - N / 2) * dfs / N, dfs / N, (int)db.size(), base64(db).c_str());
        }
    }
};

// the API's calls log through the engine's sink, whichever thread makes them
struct SinkScope {
    LogSink saved;
    explicit SinkScope(const LogSink& s) : saved(log_sink()) { log_sink() = s; }
    ~SinkScope() { log_sink() = saved; }
};

}  // namespace

AVM_RX_API void* avm_rx_create(int argc, const char* const* argv, const avm_rx_callbacks* cb) {
    Engine* e = new Engine;
    if (cb) e->cb = *cb;
    e->sink.fn = e->cb.log;
    e->sink.user = e->cb.user;
    SinkScope scope(e->sink);
    if (!e->open(argc, argv)) { delete e; return nullptr; }
    e->start();
    return e;
}

AVM_RX_API void avm_rx_push(void* h, const void* iq, size_t nbytes) {
    if (h && nbytes) ((Engine*)h)->reader.feed(iq, nbytes);
}

AVM_RX_API void avm_rx_end_input(void* h) {
    if (h) ((Engine*)h)->reader.end_input();
}

AVM_RX_API void avm_rx_set_gain(void* h, double db) {
    Engine* e = (Engine*)h;
    if (!e || !e->radio) return;
    SinkScope scope(e->sink);
    e->radio->set_gain(db);
}

AVM_RX_API void avm_rx_set_freq_offset(void* h, double hz) {
    Engine* e = (Engine*)h;
    if (!e || !e->radio) return;
    SinkScope scope(e->sink);
    e->radio->set_freq_offset(hz);
}

AVM_RX_API int avm_rx_running(void* h) { return h && ((Engine*)h)->running ? 1 : 0; }

AVM_RX_API void avm_rx_destroy(void* h) {
    Engine* e = (Engine*)h;
    if (!e) return;
    {
        SinkScope scope(e->sink);
        e->stop();
    }
    delete e;
}

// the link's payload rate (hf_ofdm_common.estimate_effective_bitrate): a
// fragment's bytes over its whole on-air time, header symbols, FEC flush
// and gap included; 0 for a mode/width/modulation that doesn't exist. (At
// the mode's own rate: resampling's few samples of rounding aren't counted.)
// where a signal is found and decoded, Hz from the tuned frequency (the RX
// spectrum's capture band; touch_rx_page._tuning_geometry): the signal's own
// edges widened by the pull-in -- single carrier +-half the symbol rate, OFDM
// the integer-CFO search (29 carriers) or a quarter of the width, the less.
// 0 if the combination is invalid.
AVM_RX_API int avm_capture_band(const char* mode, double occupancy, const char* modulation, double* lo_hz,
                                double* hi_hz) {
    Mode m;
    std::string err;
    if (!build_mode(mode, occupancy, modulation, "ldpc", m, err)) return 0;
    const std::vector<int>& bins = m.sc ? m.data_bins : m.fft_bins;
    if (bins.empty()) return 0;
    double lo = (*std::min_element(bins.begin(), bins.end()) - 0.5) * m.spacing;
    double hi = (*std::max_element(bins.begin(), bins.end()) + 0.5) * m.spacing;
    double pull = m.sc ? m.symbol_rate / 2 : std::min(29 * m.spacing, (hi - lo) / 4);
    *lo_hz = lo - pull;
    *hi_hz = hi + pull;
    return 1;
}

AVM_RX_API double avm_link_bitrate(const char* mode, double occupancy, const char* modulation, const char* fec,
                                   int fragment_bytes, double gap_ms) {
    Mode m;
    std::string err;
    if (!build_mode(mode, occupancy, modulation, fec, m, err)) return 0;
    Modem md(m);
    return md.effective_bitrate(fragment_bytes, gap_ms);
}

// the radios connected now (sdr::find_radios), one per line:
// "driver<TAB>label<TAB>uri"; rx: the receive-only ones too. Returns the
// length needed (call again with a bigger buffer if it's more than cap).
AVM_RX_API int avm_sdr_find(int rx, char* out, int cap) {
    std::string s;
    for (const sdr::Found& f : sdr::find_radios(rx != 0)) s += f.driver + "\t" + f.label + "\t" + f.uri + "\n";
    if (out && cap > 0) {
        int n = std::min((int)s.size(), cap - 1);
        std::memcpy(out, s.data(), (size_t)n);
        out[n] = 0;
    }
    return (int)s.size() + 1;
}
