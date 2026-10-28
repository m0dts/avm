// modem_tx.cpp -- the OFDM modes' transmitter (A, B, C, D, VU), from
// hf_ofdm_common.py: build_frame (header: CRC, Viterbi code, repetition,
// scramble; payload: Viterbi or LDPC, interleave, whiten; QPSK/16QAM/32QAM
// mapping, pilots, Schmidl & Cox preamble, OFDM modulation), reduce_papr
// and fft_resample_rate (with tx_spectrum_cut's spectrum cut). The tables
// drawn from numpy's random generator come from npy_rng.h, so the
// waveform is Python's to rounding (test_modem_tx.py). The configuration
// (carriers, sizes) is passed in from the Python OFDMConfig.
//
// Part of the AVM library (build.py); compiled through modem_rx.cpp.
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstring>
#include <map>
#include <mutex>
#include <vector>

#include "fft.h"
#include "npy_rng.h"
#include "ldpc.h"

#if defined(_WIN32)
#define TX_API extern "C" __declspec(dllexport)
#else
#define TX_API extern "C" __attribute__((visibility("default")))
#endif

namespace {

using cd = std::complex<double>;
using cf = std::complex<float>;

const uint64_t PILOT_SEQ_SEED = 12345, PREAMBLE_SEED = 999, HEADER_SCRAMBLE_SEED = 4242,
               INTERLEAVER_SEED = 424242;
const int HEADER_BITS = 32 + 32 + 8;
const int FEC_N_MEM = 6;
const int HEADER_MIN_REPEATS = 3, HEADER_MAX_SYMBOLS = 4;
const int PAPR_OVERSAMPLE = 2;

// ---- zlib's CRC-32
uint32_t crc32(const uint8_t* p, size_t n) {
    static uint32_t table[256];
    static bool init = false;
    if (!init) {
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
        init = true;
    }
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) c = table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

// ---- rate-1/2 K=7 convolutional code (171/133), zero-flushed
std::vector<uint8_t> fec_encode(const uint8_t* bits, size_t n) {
    std::vector<uint8_t> out(2 * (n + FEC_N_MEM));
    unsigned reg = 0;   // bit 6 = newest input, bit 0 = oldest (reg[] order of the Python)
    for (size_t t = 0; t < n + FEC_N_MEM; ++t) {
        unsigned b = t < n ? (bits[t] & 1) : 0;
        reg = (reg >> 1) | (b << 6);
        out[2 * t] = (uint8_t)(__builtin_popcount(reg & 0171) & 1);
        out[2 * t + 1] = (uint8_t)(__builtin_popcount(reg & 0133) & 1);
    }
    return out;
}

// ---- 15-bit LFSR whitening table (x^15 + x^14 + 1)
const std::vector<uint8_t>& prbs_table() {
    static const std::vector<uint8_t> t = [] {     // (built once, thread-safely)
        std::vector<uint8_t> v(32767);
        unsigned s = 0x7FFF;
        for (int i = 0; i < 32767; ++i) {
            unsigned bit = ((s >> 14) ^ (s >> 13)) & 1;
            v[i] = s & 1;
            s = ((s << 1) | bit) & 0x7FFF;
        }
        return v;
    }();
    return t;
}

std::mutex cache_mu;

const std::vector<int32_t>& interleave_perm(int n) {
    static std::map<int, std::vector<int32_t>> cache;
    std::lock_guard<std::mutex> g(cache_mu);
    auto it = cache.find(n);
    if (it == cache.end()) {
        npy::Generator gen(INTERLEAVER_SEED);
        std::vector<int64_t> p = gen.permutation(n);
        it = cache.emplace(n, std::vector<int32_t>(p.begin(), p.end())).first;
    }
    return it->second;
}

const std::vector<uint8_t>& header_pn(int n) {
    static std::map<int, std::vector<uint8_t>> cache;
    std::lock_guard<std::mutex> g(cache_mu);
    auto it = cache.find(n);
    if (it == cache.end()) {
        npy::Generator gen(HEADER_SCRAMBLE_SEED);
        std::vector<int64_t> v = gen.integers(0, 2, n);
        it = cache.emplace(n, std::vector<uint8_t>(v.begin(), v.end())).first;
    }
    return it->second;
}

inline int pymod(long long a, long long m) { long long r = a % m; return (int)(r < 0 ? r + m : r); }

const double QAM16_NORM = std::sqrt(10.0), QAM32_NORM = std::sqrt(20.0), SQRT2 = std::sqrt(2.0);
const int QAM32_TABLE[32][2] = {
    {-5, 3}, {-5, -3}, {-3, -5}, {-3, -3}, {3, 3}, {3, 5}, {1, 3}, {1, 5},
    {5, 3}, {5, -3}, {-1, -5}, {-1, -3}, {3, -5}, {3, -3}, {1, -5}, {1, -3},
    {-5, 1}, {-5, -1}, {-3, 1}, {-3, -1}, {-3, 3}, {-3, 5}, {-1, 3}, {-1, 5},
    {5, 1}, {5, -1}, {-1, 1}, {-1, -1}, {3, 1}, {3, -1}, {1, 1}, {1, -1}};

inline cd qpsk(const uint8_t* b) { return cd(1 - 2 * b[0], 1 - 2 * b[1]) / SQRT2; }
inline cd qam16(const uint8_t* b) {
    return cd((1 - 2 * b[0]) * (1 + 2 * b[1]), (1 - 2 * b[2]) * (1 + 2 * b[3])) / QAM16_NORM;
}
inline cd qam32(const uint8_t* b) {
    int i = (b[0] << 4) | (b[1] << 3) | (b[2] << 2) | (b[3] << 1) | b[4];
    return cd(QAM32_TABLE[i][0], QAM32_TABLE[i][1]) / QAM32_NORM;
}

struct Cfg {
    int n_fft, n_cp, n_data, freq_int, time_int, preamble_halves, bpc;   // bpc: payload bits per carrier
    bool ldpc;
    bool time_first = false;            // RX: time-first comb equaliser by default (modes A, B, C)
    std::vector<int> fft_bins;          // may be negative (numpy index)
    std::vector<int> bin_idx;           // fft_bins % n_fft
    std::vector<cd> pilots;             // pilot_sequence
    std::vector<cd> preamble;
    std::vector<cd> frame;              // last build_frame / reduce_papr result
    bool full_training() const { return freq_int <= 1; }
    int period() const { return full_training() ? time_int : freq_int; }
    int symbol_len() const { return n_fft + n_cp; }
    int preamble_len() const { return n_cp + preamble_halves * (n_fft / 2); }
    bool is_pilot(int k, int j) const {
        if (full_training()) return k % time_int == 0;
        return j % freq_int == k % freq_int;
    }
    int n_data_carriers(int k) const {
        int c = 0;
        for (int j = 0; j < n_data; ++j) c += !is_pilot(k, j);
        return c;
    }
};

void build_tables(Cfg& c) {
    c.bin_idx.resize(c.n_data);
    for (int j = 0; j < c.n_data; ++j) c.bin_idx[j] = pymod(c.fft_bins[j], c.n_fft);
    {
        npy::Generator g(PILOT_SEQ_SEED);
        std::vector<int64_t> b = g.integers(0, 2, (size_t)c.n_data * 2);
        c.pilots.resize(c.n_data);
        for (int j = 0; j < c.n_data; ++j) c.pilots[j] = cd(1 - 2 * b[2 * j], 1 - 2 * b[2 * j + 1]) / SQRT2;
    }
    // Schmidl & Cox preamble: every other carrier
    int n = c.n_fft;
    std::vector<int> periodic;
    for (int j = 0; j < c.n_data; ++j) if (pymod(c.fft_bins[j], 2) == 0) periodic.push_back(c.bin_idx[j]);
    npy::Generator g(PREAMBLE_SEED);
    std::vector<int64_t> b = g.integers(0, 2, periodic.size() * 2);
    std::vector<cd> freq(n, cd(0, 0)), t(n);
    for (size_t i = 0; i < periodic.size(); ++i)
        freq[periodic[i]] = cd(1 - 2 * b[2 * i], 1 - 2 * b[2 * i + 1]) / SQRT2 * SQRT2;
    fft::plan<double>(n).inverse(freq.data(), t.data());
    double s = std::sqrt((double)n) / n;
    for (auto& v : t) v *= s;
    std::vector<cd> sig;
    if (c.preamble_halves != 2) {
        for (int h = 0; h < c.preamble_halves; ++h) sig.insert(sig.end(), t.begin(), t.begin() + n / 2);
    } else {
        sig = t;
    }
    c.preamble.assign(sig.end() - c.n_cp, sig.end());
    c.preamble.insert(c.preamble.end(), sig.begin(), sig.end());
}

std::vector<int> header_symbol_plan(const Cfg& c) {
    int target = HEADER_MIN_REPEATS * 2 * (HEADER_BITS + FEC_N_MEM);
    std::vector<int> idx;
    int total = 0;
    for (int k = 0; total < target && (int)idx.size() < HEADER_MAX_SYMBOLS; ++k) {
        int cap = c.n_data_carriers(k) * 2;
        if (cap > 0) {
            idx.push_back(k);
            total += cap;
        } else if (!idx.empty()) {
            break;
        }
    }
    return idx;
}

void build_frame(Cfg& c, const uint8_t* payload, size_t len) {
    std::vector<uint8_t> pbits(len * 8);
    for (size_t i = 0; i < len; ++i)
        for (int k = 0; k < 8; ++k) pbits[8 * i + k] = (payload[i] >> (7 - k)) & 1;
    uint32_t crc = crc32(payload, len);
    uint32_t raw_len = (uint32_t)len | (c.ldpc ? 0x80000000u : 0);
    uint8_t hb[8] = {(uint8_t)(raw_len >> 24), (uint8_t)(raw_len >> 16), (uint8_t)(raw_len >> 8), (uint8_t)raw_len,
                     (uint8_t)(crc >> 24), (uint8_t)(crc >> 16), (uint8_t)(crc >> 8), (uint8_t)crc};
    uint8_t chk = (uint8_t)(crc32(hb, 8) & 0xFF);
    uint8_t header[HEADER_BITS];
    for (int i = 0; i < 64; ++i) header[i] = (hb[i / 8] >> (7 - i % 8)) & 1;
    for (int i = 0; i < 8; ++i) header[64 + i] = (chk >> (7 - i)) & 1;

    std::vector<uint8_t> coded;
    if (c.ldpc) {
        coded.resize(ldpc_coded_len((long long)pbits.size()));
        if (!pbits.empty()) ldpc_encode(pbits.data(), (long long)pbits.size(), coded.data());
    } else {
        coded = fec_encode(pbits.data(), pbits.size());
    }
    {
        const std::vector<int32_t>& perm = interleave_perm((int)coded.size());
        std::vector<uint8_t> il(coded.size());
        for (size_t i = 0; i < coded.size(); ++i) il[i] = coded[perm[i]];
        coded.swap(il);
    }

    // per symbol: bits and bits-per-carrier (0 = no data)
    std::vector<std::vector<uint8_t>> sym_bits;
    std::vector<int> sym_bpc;
    int k = 0;
    while (c.n_data_carriers(k) == 0) {
        sym_bits.emplace_back();
        sym_bpc.push_back(2);
        ++k;
    }
    std::vector<int> hidx = header_symbol_plan(c);
    std::vector<int> hcaps;
    int htotal = 0;
    for (int i : hidx) { hcaps.push_back(c.n_data_carriers(i) * 2); htotal += hcaps.back(); }
    std::vector<uint8_t> hcoded = fec_encode(header, HEADER_BITS);
    std::vector<uint8_t> hall(htotal);
    const std::vector<uint8_t>& pn = header_pn(htotal);
    for (int i = 0; i < htotal; ++i) hall[i] = hcoded[i % hcoded.size()] ^ pn[i];
    int off = 0;
    for (int cap : hcaps) {
        sym_bits.emplace_back(hall.begin() + off, hall.begin() + off + cap);
        sym_bpc.push_back(2);
        off += cap;
    }
    k = hidx.back() + 1;
    std::vector<int> caps;
    size_t got = 0;
    while (got < coded.size()) {
        int cap = c.n_data_carriers(k) * c.bpc;
        caps.push_back(cap);
        got += cap;
        ++k;
    }
    std::vector<uint8_t> padded(got, 0);
    std::copy(coded.begin(), coded.end(), padded.begin());
    const std::vector<uint8_t>& prbs = prbs_table();
    for (size_t i = 0; i < got; ++i) padded[i] ^= prbs[i % prbs.size()];
    size_t pos = 0;
    for (int cap : caps) {
        sym_bits.emplace_back(padded.begin() + pos, padded.begin() + pos + cap);
        sym_bpc.push_back(c.bpc);
        pos += cap;
    }

    // modulate
    int n = c.n_fft, L = c.symbol_len(), nsym = (int)sym_bits.size();
    c.frame.assign(c.preamble.begin(), c.preamble.end());
    c.frame.resize(c.preamble.size() + (size_t)nsym * L);
    fft::Plan<double>& pl = fft::plan<double>(n);
    std::vector<cd> freq(n), t(n);
    double sc = std::sqrt((double)n) / n;
    for (int s = 0; s < nsym; ++s) {
        std::fill(freq.begin(), freq.end(), cd(0, 0));
        const uint8_t* b = sym_bits[s].data();
        int bpc = sym_bpc[s];
        for (int j = 0; j < c.n_data; ++j) {
            cd v;
            if (c.is_pilot(s, j)) {
                v = c.pilots[j];
            } else {
                v = bpc == 2 ? qpsk(b) : bpc == 4 ? qam16(b) : qam32(b);
                b += bpc;
            }
            freq[c.bin_idx[j]] = v;
        }
        pl.inverse(freq.data(), t.data());
        cd* o = c.frame.data() + c.preamble.size() + (size_t)s * L;
        for (int i = 0; i < c.n_cp; ++i) o[i] = t[n - c.n_cp + i] * sc;
        for (int i = 0; i < n; ++i) o[c.n_cp + i] = t[i] * sc;
    }
}

void reduce_papr(Cfg& c, double target_db, int iterations) {
    int n = c.n_fft, L = c.symbol_len(), cp = c.n_cp, pre = c.preamble_len();
    int nsym = ((int)c.frame.size() - pre) / L;
    if (nsym <= 0 || target_db == 0) return;
    int on = PAPR_OVERSAMPLE * n;
    std::vector<int> idx_os(c.n_data);
    for (int j = 0; j < c.n_data; ++j) idx_os[j] = pymod(c.fft_bins[j], on);
    fft::Plan<double>& pn = fft::plan<double>(n);
    fft::Plan<double>& po = fft::plan<double>(on);
    std::vector<cd> X((size_t)nsym * on, cd(0, 0)), x((size_t)nsym * on), tmp(on), body(n), F(n);
    double inv_sqrt_n = 1 / std::sqrt((double)n);
    double sum = 0;
    for (int s = 0; s < nsym; ++s) {
        pn.forward(c.frame.data() + pre + (size_t)s * L + cp, body.data());
        cd* Xs = X.data() + (size_t)s * on;
        for (int j = 0; j < c.n_data; ++j) Xs[idx_os[j]] = body[c.bin_idx[j]] * inv_sqrt_n;
        cd* xs = x.data() + (size_t)s * on;
        po.inverse(Xs, xs);
        for (int i = 0; i < on; ++i) {
            xs[i] /= (double)on;
            sum += xs[i].real() * xs[i].real() + xs[i].imag() * xs[i].imag();
        }
    }
    double thr2 = sum / ((double)nsym * on) * std::pow(10.0, target_db / 10);
    for (int it = 0; it < iterations; ++it) {
        for (int s = 0; s < nsym; ++s) {
            cd* xs = x.data() + (size_t)s * on;
            for (int i = 0; i < on; ++i) {
                double pw = xs[i].real() * xs[i].real() + xs[i].imag() * xs[i].imag();
                if (pw > thr2) xs[i] *= std::sqrt(thr2 / pw);
            }
            po.forward(xs, tmp.data());
            cd* Xs = X.data() + (size_t)s * on;
            for (int j = 0; j < c.n_data; ++j)
                if (!c.is_pilot(s, j)) Xs[idx_os[j]] = tmp[idx_os[j]];
            po.inverse(Xs, xs);
            for (int i = 0; i < on; ++i) xs[i] /= (double)on;
        }
    }
    double sq = std::sqrt((double)n) / n;
    for (int s = 0; s < nsym; ++s) {
        std::fill(F.begin(), F.end(), cd(0, 0));
        cd* Xs = X.data() + (size_t)s * on;
        for (int j = 0; j < c.n_data; ++j) F[c.bin_idx[j]] = Xs[idx_os[j]];
        pn.inverse(F.data(), body.data());
        cd* o = c.frame.data() + pre + (size_t)s * L;
        for (int i = 0; i < cp; ++i) o[i] = body[n - cp + i] * sq;
        for (int i = 0; i < n; ++i) o[cp + i] = body[i] * sq;
    }
}

int max_prime_factor(long long n) {
    long long m = 1, d = 2;
    while (d * d <= n) {
        while (n % d == 0) { m = d; n /= d; }
        ++d;
    }
    return (int)(n > 1 ? std::max(m, n) : m);
}

long long resample_pad(long long n_in, double fs_in, double fs_out) {
    static std::map<std::tuple<long long, double, double>, long long> cache;
    std::lock_guard<std::mutex> g(cache_mu);
    auto key = std::make_tuple(n_in, fs_in, fs_out);
    auto it = cache.find(key);
    if (it != cache.end()) return it->second;
    long long pad = -1;
    for (int pass = 0; pass < 2 && pad < 0; ++pass) {
        for (long long p = 0; p < 4096; ++p) {
            long long n_out = (long long)std::nearbyint((double)(n_in + p) * fs_out / fs_in);
            if (max_prime_factor(n_out) <= 11 && (pass == 1 || max_prime_factor(n_in + p) <= 100)) {
                pad = p;
                break;
            }
        }
    }
    if (pad < 0) pad = 0;
    cache[key] = pad;
    return pad;
}

}  // namespace

// ---- C API
TX_API void* tx_cfg_new(int n_fft, int n_cp, int n_data, const long long* fft_bins, int freq_int, int time_int,
                        int preamble_halves, int bits_per_carrier, int ldpc) {
    Cfg* c = new Cfg();
    c->n_fft = n_fft; c->n_cp = n_cp; c->n_data = n_data;
    c->freq_int = freq_int; c->time_int = time_int; c->preamble_halves = preamble_halves;
    c->bpc = bits_per_carrier; c->ldpc = ldpc != 0;
    c->fft_bins.assign(fft_bins, fft_bins + n_data);
    build_tables(*c);
    return c;
}

TX_API void tx_cfg_free(void* h) { delete (Cfg*)h; }

// builds a frame; returns its length in samples (fetch with tx_frame_copy)
TX_API long long tx_build_frame(void* h, const uint8_t* payload, long long len) {
    Cfg* c = (Cfg*)h;
    build_frame(*c, payload, (size_t)len);
    return (long long)c->frame.size();
}

TX_API void tx_reduce_papr(void* h, double target_db, int iterations) { reduce_papr(*(Cfg*)h, target_db, iterations); }

TX_API long long tx_frame_len(void* h) { return (long long)((Cfg*)h)->frame.size(); }

TX_API void tx_frame_copy(void* h, cd* out) {
    Cfg* c = (Cfg*)h;
    std::memcpy(out, c->frame.data(), c->frame.size() * sizeof(cd));
}

// pass a frame in (to reduce_papr one built elsewhere)
TX_API void tx_frame_set(void* h, const cd* in, long long n) {
    Cfg* c = (Cfg*)h;
    c->frame.assign(in, in + n);
}

TX_API long long tx_resample_len(long long n_in, double fs_in, double fs_out) {
    long long n = n_in + resample_pad(n_in, fs_in, fs_out);
    return (long long)std::nearbyint((double)n * fs_out / fs_in);
}

// fft_resample_rate in single precision; cut_hz < 0: no cut
TX_API void tx_resample(const cf* x, long long n_in0, double fs_in, double fs_out, double cut_hz, double taper_hz,
                        cf* out) {
    long long n_in = n_in0 + resample_pad(n_in0, fs_in, fs_out);
    long long n_out = (long long)std::nearbyint((double)n_in * fs_out / fs_in);
    std::vector<cf> xin(n_in, cf(0, 0)), X(n_in);
    std::copy(x, x + n_in0, xin.begin());
    fft::plan<float>((int)n_in).forward(xin.data(), X.data());
    if (cut_hz >= 0 && cut_hz < fs_in / 2) {
        double val = 1.0 / ((double)n_in * (1.0 / fs_in));
        double tp = std::max(taper_hz, 1e-9);
        long long half = (n_in - 1) / 2 + 1;   // fftfreq: 0..half-1, then negative
        for (long long k = 0; k < n_in; ++k) {
            long long kk = k < half ? k : k - n_in;
            double f = std::fabs((double)kk * val);
            double u = std::min(1.0, std::max(0.0, (f - cut_hz) / tp));
            X[k] *= (float)(0.5 + 0.5 * std::cos(fft::PI * u));
        }
    }
    std::vector<cf> Y(n_out, cf(0, 0));
    if (n_out > n_in) {
        long long left = (n_in + 1) / 2;
        std::copy(X.begin(), X.begin() + left, Y.begin());
        std::copy(X.begin() + left, X.end(), Y.begin() + left + (n_out - n_in));
    } else {
        long long left = (n_out + 1) / 2, right = n_out - left;
        std::copy(X.begin(), X.begin() + left, Y.begin());
        std::copy(X.end() - right, X.end(), Y.begin() + left);
    }
    fft::plan<float>((int)n_out).inverse(Y.data(), out);
    float s = (float)(1.0 / n_out * ((double)n_out / n_in));
    for (long long i = 0; i < n_out; ++i) out[i] *= s;
}

// ======================================================================
// Single-carrier modes S (hf_sc.py) and SH (hf_sch.py): build_frame.
// S: preamble (BPSK m-sequence), repeated scrambled BPSK header, then
// pilot blocks of 8 QPSK symbols between data blocks of 128. SH: per
// fragment one of 8 variants (crc32(payload) % 8) of short-sequence +
// unique-word preamble, header block, UW, then blocks [data, UW]. Both:
// RRC-shaped at 4 samples per symbol, unit RMS, complex64.
namespace {

const int SC_SPS = 4, SC_RRC_SPAN = 8, SC_HDR_BITS = 24, SC_HDR_REPEAT = 8, SC_DATA_BLOCK = 128,
          SC_PILOT_LEN = 8;
const double SC_ALPHA = 0.25;
const int SH_PERIOD = 16, SH_SHORT_REPEATS = 8, SH_PRE_UWS = 3, SH_VARIANTS = 8;

std::vector<cf> gray_psk(int m) {
    std::vector<cf> pts(m);
    for (int k = 0; k < m; ++k) {
        double a = 2 * fft::PI * k / m + (m == 4 ? fft::PI / 4 : 0.0);
        pts[k ^ (k >> 1)] = cf((float)std::cos(a), (float)std::sin(a));
    }
    return pts;
}

std::vector<float> rrc_taps() {
    const double a = SC_ALPHA, pi = fft::PI;
    int n = 2 * SC_RRC_SPAN * SC_SPS + 1;
    std::vector<double> h(n);
    double e = 0;
    for (int i = 0; i < n; ++i) {
        double x = (double)(i - SC_RRC_SPAN * SC_SPS) / SC_SPS;
        if (std::fabs(x) < 1e-9)
            h[i] = 1 - a + 4 * a / pi;
        else if (std::fabs(std::fabs(x) - 1 / (4 * a)) < 1e-9)
            h[i] = a / std::sqrt(2.0) * ((1 + 2 / pi) * std::sin(pi / (4 * a)) + (1 - 2 / pi) * std::cos(pi / (4 * a)));
        else
            h[i] = (std::sin(pi * x * (1 - a)) + 4 * a * x * std::cos(pi * x * (1 + a)))
                   / (pi * x * (1 - (4 * a * x) * (4 * a * x)));
        e += h[i] * h[i];
    }
    std::vector<float> out(n);
    for (int i = 0; i < n; ++i) out[i] = (float)(h[i] / std::sqrt(e));
    return out;
}

uint8_t crc8_bits(const uint8_t* bits, int n) {
    unsigned c = 0;
    for (int i = 0; i < n; ++i) {
        c ^= (unsigned)bits[i] << 7;
        c = (c & 0x80) ? (((c << 1) ^ 0x07) & 0xFF) : ((c << 1) & 0xFF);
    }
    return (uint8_t)c;
}

// 16-bit length + its CRC-8
void sc_header_bits(int length, uint8_t* out) {
    for (int i = 0; i < 16; ++i) out[i] = (length >> (15 - i)) & 1;
    uint8_t c = crc8_bits(out, 16);
    for (int i = 0; i < 8; ++i) out[16 + i] = (c >> (7 - i)) & 1;
}

// payload + CRC-32 -> LDPC -> interleave -> whiten
std::vector<uint8_t> sc_coded(const uint8_t* payload, size_t n) {
    std::vector<uint8_t> data(payload, payload + n);
    uint32_t crc = crc32(payload, n);
    for (int i = 3; i >= 0; --i) data.push_back((uint8_t)(crc >> (8 * i)));
    std::vector<uint8_t> bits(data.size() * 8);
    for (size_t i = 0; i < data.size(); ++i)
        for (int k = 0; k < 8; ++k) bits[8 * i + k] = (data[i] >> (7 - k)) & 1;
    std::vector<uint8_t> cw(ldpc_coded_len((long long)bits.size()));
    ldpc_encode(bits.data(), (long long)bits.size(), cw.data());
    const std::vector<int32_t>& perm = interleave_perm((int)cw.size());
    const std::vector<uint8_t>& prbs = prbs_table();
    std::vector<uint8_t> out(cw.size());
    for (size_t i = 0; i < cw.size(); ++i) out[i] = cw[perm[i]] ^ prbs[i % prbs.size()];
    return out;
}

struct SC {
    bool sh;
    int k, bps;
    std::vector<cf> cst;            // data constellation
    std::vector<float> rrc;
    // S
    std::vector<cf> preamble, pilots;
    std::vector<uint8_t> hdr_scramble;
    // SH
    int N = 0, UW_LEN = 0, D_LEN = 0, HDR_REPEAT = 0;
    std::vector<std::vector<cf>> uws, shorts;
    std::vector<cf> frame;

    SC(bool sh_, int k_, int bps_) : sh(sh_), k(k_), bps(bps_) {
        cst = gray_psk(1 << bps);
        rrc = rrc_taps();
        if (!sh) {
            unsigned st = 1;
            for (int i = 0; i < 255; ++i) {
                unsigned bit = ((st >> 7) ^ (st >> 5) ^ (st >> 4) ^ (st >> 3)) & 1;
                preamble.push_back(cf(1.0f - 2.0f * (st & 1), 0));
                st = ((st << 1) | bit) & 0xFF;
            }
            preamble.push_back(cf(1, 0));
            npy::Generator g(20261019);
            const float r = (float)(1 / std::sqrt(2.0));
            const cf q[4] = {cf(r, r), cf(-r, r), cf(-r, -r), cf(r, -r)};
            for (int64_t v : g.integers(0, 4, 4096)) pilots.push_back(q[v]);
            for (int64_t v : g.integers(0, 2, SC_HDR_BITS * SC_HDR_REPEAT)) hdr_scramble.push_back((uint8_t)v);
            return;
        }
        N = 256 * k;
        UW_LEN = 64 * k;
        D_LEN = N - UW_LEN;
        HDR_REPEAT = D_LEN / SC_HDR_BITS;
        {
            npy::Generator g(777 + k);
            for (int64_t v : g.integers(0, 2, (size_t)SC_HDR_BITS * HDR_REPEAT)) hdr_scramble.push_back((uint8_t)v);
        }
        npy::Generator g(424242 + k);
        fft::Plan<double>& pl = fft::plan<double>(UW_LEN);
        std::vector<cd> u(UW_LEN), F(UW_LEN);
        for (int vnt = 0; vnt < SH_VARIANTS; ++vnt) {
            std::vector<double> ph = g.random(UW_LEN);
            for (int i = 0; i < UW_LEN; ++i) u[i] = std::exp(cd(0, 2 * fft::PI * ph[i]));
            for (int it = 0; it < 300; ++it) {
                pl.forward(u.data(), F.data());
                for (auto& f : F) f /= std::max(std::abs(f), 1e-12);
                pl.inverse(F.data(), u.data());
                for (auto& x : u) x /= (double)UW_LEN;
                for (auto& x : u) x /= std::max(std::abs(x), 1e-12);
            }
            std::vector<cf> uw(UW_LEN);
            for (int i = 0; i < UW_LEN; ++i) uw[i] = cf((float)u[i].real(), (float)u[i].imag());
            uws.push_back(uw);
        }
        std::vector<cf> q4 = gray_psk(4);
        for (int vnt = 0; vnt < SH_VARIANTS; ++vnt) {
            std::vector<cf> s;
            for (int64_t v : g.integers(0, 4, SH_PERIOD)) s.push_back(q4[v]);
            shorts.push_back(s);
        }
    }

    std::vector<cf> data_symbols(const std::vector<uint8_t>& coded, size_t n_sym) {
        std::vector<cf> out(n_sym);
        for (size_t i = 0; i < n_sym; ++i) {
            int v = 0;
            for (int b = 0; b < bps; ++b) {
                size_t j = i * bps + b;
                v = (v << 1) | (j < coded.size() ? coded[j] : 0);
            }
            out[i] = cst[v];
        }
        return out;
    }

    void build(const uint8_t* payload, size_t n) {
        std::vector<uint8_t> coded = sc_coded(payload, n);
        uint8_t hb[SC_HDR_BITS];
        sc_header_bits((int)n, hb);
        std::vector<cf> syms;
        int rep = sh ? HDR_REPEAT : SC_HDR_REPEAT;
        auto add_header = [&]() {
            for (int i = 0; i < SC_HDR_BITS * rep; ++i)
                syms.push_back(cf(1.0f - 2.0f * (hb[i / rep] ^ hdr_scramble[i]), 0));
        };
        if (!sh) {
            size_t n_data = (coded.size() + bps - 1) / bps;
            size_t n_blocks = (n_data + SC_DATA_BLOCK - 1) / SC_DATA_BLOCK, n_pb = n_blocks + 1;
            std::vector<cf> ds = data_symbols(coded, n_data);
            syms = preamble;
            add_header();
            size_t di = 0;
            for (size_t blk = 0; blk < n_pb; ++blk) {
                syms.insert(syms.end(), pilots.begin() + blk * SC_PILOT_LEN, pilots.begin() + (blk + 1) * SC_PILOT_LEN);
                if (blk < n_pb - 1) {
                    size_t kk = std::min((size_t)SC_DATA_BLOCK, n_data - blk * SC_DATA_BLOCK);
                    syms.insert(syms.end(), ds.begin() + di, ds.begin() + di + kk);
                    di += kk;
                }
            }
        } else {
            int v = (int)(crc32(payload, n) % SH_VARIANTS);
            const std::vector<cf>& uw = uws[v];
            for (int r = 0; r < SH_SHORT_REPEATS; ++r) syms.insert(syms.end(), shorts[v].begin(), shorts[v].end());
            for (int r = 0; r < SH_PRE_UWS; ++r) syms.insert(syms.end(), uw.begin(), uw.end());
            add_header();
            syms.insert(syms.end(), uw.begin(), uw.end());
            size_t per = (size_t)bps * D_LEN;
            size_t n_blocks = ((size_t)ldpc_coded_len((long long)(n + 4) * 8) + per - 1) / per;
            std::vector<cf> ds = data_symbols(coded, n_blocks * D_LEN);
            for (size_t b = 0; b < n_blocks; ++b) {
                syms.insert(syms.end(), ds.begin() + b * D_LEN, ds.begin() + (b + 1) * D_LEN);
                syms.insert(syms.end(), uw.begin(), uw.end());
            }
        }
        // RRC: w[i] = sum_j syms[j] * rrc[i - SPS*j]
        int L = (int)rrc.size(), delay = L / 2;
        size_t n_up = syms.size() * SC_SPS, n_out = n_up + 2 * delay - (SC_SPS - 1);
        frame.assign(n_out, cf(0, 0));
        for (size_t j = 0; j < syms.size(); ++j) {
            cf s = syms[j];
            cf* o = frame.data() + j * SC_SPS;
            size_t lim = std::min((size_t)L, n_out - j * SC_SPS);
            for (size_t t = 0; t < lim; ++t) o[t] += cf(s.real() * rrc[t], s.imag() * rrc[t]);
        }
        double e = 0;
        for (const cf& x : frame) e += (double)x.real() * x.real() + (double)x.imag() * x.imag();
        float g = (float)(1 / std::sqrt(e / n_out));
        for (cf& x : frame) x *= g;
    }
};

}  // namespace

// sh: 0 = mode S, 1 = SH; k10: width / 10 kHz; bps: data bits per symbol
TX_API void* tx_sc_new(int sh, int k10, int bps) { return new SC(sh != 0, k10, bps); }
TX_API void tx_sc_free(void* h) { delete (SC*)h; }
TX_API long long tx_sc_build(void* h, const uint8_t* payload, long long len) {
    SC* s = (SC*)h;
    s->build(payload, (size_t)len);
    return (long long)s->frame.size();
}
TX_API void tx_sc_copy(void* h, cf* out) {
    SC* s = (SC*)h;
    std::memcpy(out, s->frame.data(), s->frame.size() * sizeof(cf));
}
