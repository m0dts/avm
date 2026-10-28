// modem_rx.cpp -- the OFDM modes' frame decoder (A, B, C, D, VU), from
// hf_ofdm_common.decode_frame: demodulate, channel estimate + equalise
// (comb, time-first comb, decision-directed, full training), header
// (soft repetition combine, soft Viterbi, checksum), payload (soft demap,
// de-whiten, deinterleave, LDPC or soft Viterbi, CRC), BER and EVM, and
// the retry passes on a CRC failure. Includes modem_tx.cpp (one library:
// the transmitter's tables are shared). Checked against the Python by
// test_modem_rx.py. Part of the AVM library: build.py.
#pragma once   // included by both engines (avm_engines.cpp)
#include "modem_tx.cpp"
#include "evm_tables.h"

#include <functional>
#include <limits>

namespace {

const int TIME_SMOOTHING_HALF_WINDOW = 3;
const int RETRY_HALF_WINDOW = 1;
const double HOPELESS_BER = 0.18;
const double DEEP_FADE_MAG_RATIO = 3.0;
const long long MAX_REASONABLE_PAYLOAD_BYTES = 10000000;
const int LDPC_MAX_ITER = 100, LDPC_STALL_ITER = 30;
const double LDPC_ALPHA = 0.85;
const double NaN = std::numeric_limits<double>::quiet_NaN();

double pyfmod(double a, double b) {
    double r = std::fmod(a, b);
    if (r != 0 && ((b < 0) != (r < 0))) r += b;
    return r;
}

void unwrap(const std::vector<double>& p, std::vector<double>& out) {
    out.resize(p.size());
    if (p.empty()) return;
    out[0] = p[0];
    double corr = 0;
    for (size_t i = 1; i < p.size(); ++i) {
        double dd = p[i] - p[i - 1];
        double ddmod = pyfmod(dd + fft::PI, 2 * fft::PI) - fft::PI;
        if (ddmod == -fft::PI && dd > 0) ddmod = fft::PI;
        if (std::fabs(dd) >= fft::PI) corr += ddmod - dd;
        out[i] = p[i] + corr;
    }
}

void interp_clamped(int n, const std::vector<int>& xp, const std::vector<double>& fp, std::vector<double>& out) {
    out.resize(n);
    int j = 0, m = (int)xp.size();
    for (int i = 0; i < n; ++i) {
        if (i <= xp[0]) out[i] = fp[0];
        else if (i >= xp[m - 1]) out[i] = fp[m - 1];
        else {
            while (xp[j + 1] < i) ++j;
            double t = (double)(i - xp[j]) / (xp[j + 1] - xp[j]);
            out[i] = fp[j] + t * (fp[j + 1] - fp[j]);
        }
    }
}

typedef std::vector<cd> Grid;   // n_sym x n_data, row-major

// _equalize_comb (dd: per-symbol reference rows, or empty)
void eq_comb(const Cfg& c, const Grid& F, int ns, int hw, const std::vector<char>& use_dd, const Grid& dd,
             Grid& eq, Grid& eqs) {
    int n = c.n_data, fi = c.freq_int;
    Grid h(F.size());
    std::vector<int> xp;
    std::vector<double> mag, ang, ph, mi, pi_;
    for (int k = 0; k < ns; ++k) {
        int shift = k % fi;
        const cd* r = &F[(size_t)k * n];
        cd* hr = &h[(size_t)k * n];
        if (!use_dd.empty() && use_dd[k]) {
            for (int j = 0; j < n; ++j) hr[j] = r[j] / (j % fi == shift ? c.pilots[j] : dd[(size_t)k * n + j]);
            continue;
        }
        xp.clear(); mag.clear(); ang.clear();
        for (int j = shift; j < n; j += fi) {
            cd v = r[j] / c.pilots[j];
            xp.push_back(j);
            mag.push_back(std::abs(v));
            ang.push_back(std::atan2(v.imag(), v.real()));
        }
        unwrap(ang, ph);
        interp_clamped(n, xp, mag, mi);
        interp_clamped(n, xp, ph, pi_);
        for (int j = 0; j < n; ++j) hr[j] = mi[j] * std::exp(cd(0, pi_[j]));
    }
    eq.assign(F.size(), cd(NaN, 0));
    eqs.assign(F.size(), cd(NaN, 0));
    for (int k = 0; k < ns; ++k) {
        int lo = std::max(0, k - hw), hi = std::min(ns, k + hw + 1), shift = k % fi;
        for (int j = 0; j < n; ++j) {
            if (j % fi == shift) continue;
            cd s(0, 0);
            for (int q = lo; q < hi; ++q) s += h[(size_t)q * n + j];
            cd hh = s / (double)(hi - lo);
            size_t i = (size_t)k * n + j;
            eq[i] = F[i] / hh;
            eqs[i] = F[i] * std::conj(hh);
        }
    }
}

// _equalize_comb_tf
void eq_comb_tf(const Cfg& c, const Grid& F, int ns, int hw, Grid& eq, Grid& eqs) {
    int n = c.n_data, fi = c.freq_int;
    Grid hp(F.size(), cd(0, 0)), h(F.size(), cd(0, 0));
    std::vector<char> known(F.size(), 0);
    for (int k = 0; k < ns; ++k)
        for (int j = 0; j < n; ++j)
            if (j % fi == k % fi) hp[(size_t)k * n + j] = F[(size_t)k * n + j] / c.pilots[j];
    cd acc(0, 0);
    for (int k = 0; k < ns - fi; ++k)
        for (int j = k % fi; j < n; j += fi) acc += hp[(size_t)(k + fi) * n + j] * std::conj(hp[(size_t)k * n + j]);
    double w = (acc != cd(0, 0)) ? std::atan2(acc.imag(), acc.real()) / fi : 0.0;
    std::vector<cd> rot(ns);
    for (int k = 0; k < ns; ++k) rot[k] = std::exp(cd(0, w * k));
    for (int j = 0; j < n; ++j) {
        int k0 = j % fi;
        if (k0 >= ns) continue;
        for (int k = 0; k < ns; ++k) {
            int kp = k >= k0 ? k - pymod(k - k0, fi) : -1;
            int kn = kp >= 0 ? kp + fi : k0;
            size_t i = (size_t)k * n + j;
            if (kp == k) {
                h[i] = hp[i] / rot[k];
                known[i] = 1;
            } else if (kp >= 0 && kn < ns) {
                double a = (double)(k - kp) / (kn - kp);
                h[i] = (1 - a) * hp[(size_t)kp * n + j] / rot[kp] + a * hp[(size_t)kn * n + j] / rot[kn];
                known[i] = 1;
            } else if (kp >= 0 && k - kp <= fi) {
                h[i] = hp[(size_t)kp * n + j] / rot[kp];
                known[i] = 1;
            } else if (kp < 0 && kn < ns && kn - k <= fi) {
                h[i] = hp[(size_t)kn * n + j] / rot[kn];
                known[i] = 1;
            }
        }
    }
    for (int k = 0; k < ns; ++k) {
        cd* hr = &h[(size_t)k * n];
        const char* kr = &known[(size_t)k * n];
        int last = -1;
        for (int j = 0; j < n; ++j) {
            if (!kr[j]) continue;
            if (last == -1) {
                for (int q = 0; q < j; ++q) hr[q] = hr[j];
            } else if (j - last > 1) {
                for (int q = last + 1; q < j; ++q) {
                    double a = (double)(q - last) / (j - last);
                    hr[q] = (1 - a) * hr[last] + a * hr[j];
                }
            }
            last = j;
        }
        if (last == -1) {
            for (int q = 0; q < n; ++q) hr[q] = hp[(size_t)k * n + q];
        } else {
            for (int q = last + 1; q < n; ++q) hr[q] = hr[last];
        }
    }
    eq.assign(F.size(), cd(NaN, 0));
    eqs.assign(F.size(), cd(NaN, 0));
    for (int k = 0; k < ns; ++k) {
        int lo = std::max(0, k - hw), hi = std::min(ns, k + hw + 1);
        for (int j = 0; j < n; ++j) {
            if (j % fi == k % fi) continue;
            cd s(0, 0);
            for (int q = lo; q < hi; ++q) s += h[(size_t)q * n + j];
            cd hh = s / (double)(hi - lo) * rot[k];
            size_t i = (size_t)k * n + j;
            eq[i] = F[i] / hh;
            eqs[i] = F[i] * std::conj(hh);
        }
    }
}

// full training symbols (mode D): time-interpolate between them
void eq_training(const Cfg& c, const Grid& F, int ns, Grid& eq, Grid& eqs) {
    int n = c.n_data;
    eq.assign(F.size(), cd(NaN, 0));
    eqs.assign(F.size(), cd(NaN, 0));
    std::vector<int> tr;
    for (int k = 0; k < ns; ++k) if (k % c.time_int == 0) tr.push_back(k);
    for (int k = 0; k < ns; ++k) {
        if (k % c.time_int == 0) continue;
        int i0 = -1, i1 = -1;
        for (int t : tr) { if (t <= k) i0 = t; if (t >= k && i1 < 0) i1 = t; }
        if (i0 >= 0 && i1 < 0) i1 = i0;
        if (i0 < 0) i0 = i1;
        for (int j = 0; j < n; ++j) {
            cd h0 = F[(size_t)i0 * n + j] / c.pilots[j], h1 = F[(size_t)i1 * n + j] / c.pilots[j];
            cd h = h0;
            if (i1 != i0) {
                double a = (double)(k - i0) / (i1 - i0);
                h = (1 - a) * h0 + a * h1;
            }
            size_t i = (size_t)k * n + j;
            eq[i] = F[i] / h;
            eqs[i] = F[i] * std::conj(h);
        }
    }
}

// estimate_and_equalize. hw < 0: default; tf < 0: default
void equalize(const Cfg& c, const Grid& F, int ns, Grid& eq, Grid& eqs, int hw = -1, int tf = -1,
              const std::vector<char>& use_dd = {}, const Grid& dd = {}) {
    if (hw < 0) hw = TIME_SMOOTHING_HALF_WINDOW;
    bool time_first = tf < 0 ? (c.time_first && !c.full_training()) : tf != 0;
    bool any_dd = false;
    for (char u : use_dd) any_dd |= u != 0;
    if (c.full_training()) eq_training(c, F, ns, eq, eqs);
    else if (time_first && !any_dd) eq_comb_tf(c, F, ns, hw, eq, eqs);
    else eq_comb(c, F, ns, hw, use_dd, dd, eq, eqs);
}

Grid demodulate(const Cfg& c, const cd* rx, int ns) {
    int n = c.n_fft, L = c.symbol_len();
    Grid out((size_t)ns * c.n_data);
    std::vector<cd> t(n);
    fft::Plan<double>& pl = fft::plan<double>(n);
    double s = 1 / std::sqrt((double)n);
    for (int k = 0; k < ns; ++k) {
        pl.forward(rx + (size_t)k * L + c.n_cp, t.data());
        for (int j = 0; j < c.n_data; ++j) out[(size_t)k * c.n_data + j] = t[c.bin_idx[j]] * s;
    }
    return out;
}

// ---- soft demappers (positive = likely 0)
const double QAM32_GRID_NORM = std::sqrt(20.0);
void soft_demap(int bpc, cd p, std::vector<double>& out) {
    if (bpc == 2) {
        out.push_back(p.real());
        out.push_back(p.imag());
    } else if (bpc == 4) {
        double i = p.real() * QAM16_NORM, q = p.imag() * QAM16_NORM;
        out.push_back(i); out.push_back(2 - std::fabs(i));
        out.push_back(q); out.push_back(2 - std::fabs(q));
    } else {
        cd y = p * QAM32_GRID_NORM;
        double d2[32];
        for (int k = 0; k < 32; ++k) {
            double a = std::abs(y - cd(QAM32_TABLE[k][0], QAM32_TABLE[k][1]));
            d2[k] = a * a;
        }
        for (int b = 0; b < 5; ++b) {
            double m1 = 1e300, m0 = 1e300;
            for (int k = 0; k < 32; ++k) {
                if ((k >> (4 - b)) & 1) m1 = std::min(m1, d2[k]);
                else m0 = std::min(m0, d2[k]);
            }
            out.push_back((m1 - m0) / 4.0);
        }
    }
}

cd data_map(int bpc, const uint8_t* b) { return bpc == 2 ? qpsk(b) : bpc == 4 ? qam16(b) : qam32(b); }

// ---- soft Viterbi (K=7, 171/133, zero-flushed); fec_decode_soft
std::vector<uint8_t> viterbi_soft(const double* soft, long long msg_bits, double* final_metric = nullptr) {
    const int NS = 64;
    long long steps = msg_bits + FEC_N_MEM;
    struct Ideal { double v[NS][2][2]; };
    static const Ideal ideal_ = [] {      // (built once, thread-safely)
        Ideal d;
        for (int ns = 0; ns < NS; ++ns)
            for (int x = 0; x < 2; ++x) {
                int s = ((ns << 1) & 63) | x, b = ns >> 5;
                unsigned reg = ((unsigned)b << 6) | (unsigned)s;
                d.v[ns][x][0] = 1 - 2 * (__builtin_popcount(reg & 0171) & 1);
                d.v[ns][x][1] = 1 - 2 * (__builtin_popcount(reg & 0133) & 1);
            }
        return d;
    }();
    const auto& ideal = ideal_.v;
    std::vector<double> pm(NS, std::numeric_limits<double>::infinity()), nm(NS);
    pm[0] = 0;
    std::vector<uint8_t> choice((size_t)steps * NS);
    for (long long t = 0; t < steps; ++t) {
        double r1 = soft[2 * t], r2 = soft[2 * t + 1];
        for (int ns = 0; ns < NS; ++ns) {
            int s0 = (ns << 1) & 63, s1 = s0 | 1;
            double b0 = (r1 - ideal[ns][0][0]) * (r1 - ideal[ns][0][0]) + (r2 - ideal[ns][0][1]) * (r2 - ideal[ns][0][1]);
            double b1 = (r1 - ideal[ns][1][0]) * (r1 - ideal[ns][1][0]) + (r2 - ideal[ns][1][1]) * (r2 - ideal[ns][1][1]);
            double m0 = pm[s0] + b0, m1 = pm[s1] + b1;
            if (m0 <= m1) { nm[ns] = m0; choice[(size_t)t * NS + ns] = 0; }
            else { nm[ns] = m1; choice[(size_t)t * NS + ns] = 1; }
        }
        pm.swap(nm);
    }
    if (final_metric) *final_metric = pm[0];
    std::vector<uint8_t> bits(steps);
    int st = 0;
    for (long long t = steps - 1; t >= 0; --t) {
        bits[t] = (uint8_t)(st >> 5);
        st = ((st << 1) & 63) | choice[(size_t)t * NS + st];
    }
    bits.resize(msg_bits);
    return bits;
}

// ---- EVM (_evm_percent)
double evm_percent(const std::vector<cd>& pts, int bpc) {
    if (pts.empty()) return NaN;
    std::vector<double> levels;
    const double* meas; const double* tru; int nt;
    if (bpc == 4) { for (int v : {-3, -1, 1, 3}) levels.push_back(v / QAM16_NORM); meas = EVM_MEAS_QAM16; tru = EVM_TRUE_QAM16; nt = EVM_N_QAM16; }
    else if (bpc == 5) { for (int v : {-5, -3, -1, 1, 3, 5}) levels.push_back(v / QAM32_NORM); meas = EVM_MEAS_QAM32; tru = EVM_TRUE_QAM32; nt = EVM_N_QAM32; }
    else { levels = {-1 / SQRT2, 1 / SQRT2}; meas = EVM_MEAS_QPSK; tru = EVM_TRUE_QPSK; nt = EVM_N_QPSK; }
    double amax = levels.back(), lim = DEEP_FADE_MAG_RATIO * DEEP_FADE_MAG_RATIO * amax * amax;
    double sum = 0;
    long long cnt = 0;
    for (const cd& p : pts) {
        double re = p.real(), im = p.imag();
        if (!(re * re + im * im <= lim)) continue;
        double e;
        if (bpc == 5) {
            e = 1e300;
            for (int k = 0; k < 32; ++k) {
                double a = std::abs(p - cd(QAM32_TABLE[k][0], QAM32_TABLE[k][1]) / QAM32_NORM);
                e = std::min(e, a * a);
            }
        } else {
            double br = 1e300, bi = 1e300;
            for (double lv : levels) {
                br = std::min(br, (re - lv) * (re - lv));
                bi = std::min(bi, (im - lv) * (im - lv));
            }
            e = br + bi;
        }
        sum += e;
        ++cnt;
    }
    if (!cnt) return NaN;
    double m = sum / cnt, t;
    if (m >= meas[nt - 1]) t = tru[nt - 1] * m / meas[nt - 1];
    else if (m <= meas[0]) t = tru[0];
    else {
        int j = 0;
        while (meas[j + 1] < m) ++j;
        t = tru[j] + (m - meas[j]) / (meas[j + 1] - meas[j]) * (tru[j + 1] - tru[j]);
    }
    return 100 * std::sqrt(t);
}

int symbols_until(const Cfg& c, int k0, long long needed) {
    int period = c.period();
    long long per = 0;
    for (int i = 0; i < period; ++i) per += (long long)c.n_data_carriers(k0 + i) * c.bpc;
    int k = k0;
    long long got = 0;
    if (per > 0) {
        long long full = std::max(0LL, needed / per - 1);
        k = (int)(k0 + full * period);
        got = full * per;
    }
    while (got < needed) {
        got += (long long)c.n_data_carriers(k) * c.bpc;
        ++k;
    }
    return k;
}

struct Result {
    int has_payload = 0, crc_ok = 0, incomplete = 0;
    double ber = NaN, evm = NaN;
    std::vector<uint8_t> payload;
    std::vector<cd> constellation;
};

// one decode pass over equalised symbols: payload soft bits -> decoded
struct Pass {
    std::vector<double> soft;           // de-whitened, truncated to needed (pre-deinterleave)
    std::vector<cd> constel;
    std::vector<uint8_t> bits, re_encoded;   // re_encoded: interleaved, unwhitened
    std::vector<uint8_t> bytes;
    bool complete = false;
};

void decode_pass(const Cfg& c, const Grid& eq, const Grid& eqs, int first, int ns, long long needed,
                 bool is_ldpc, long long payload_len, Pass& P) {
    const Grid& src = (c.bpc == 2) ? eqs : eq;
    int n = c.n_data;
    std::vector<double> soft;
    for (int k = first; k < ns; ++k)
        for (int j = 0; j < n; ++j) {
            if (c.is_pilot(k, j)) continue;
            size_t i = (size_t)k * n + j;
            P.constel.push_back(eq[i]);
            soft_demap(c.bpc, src[i], soft);
        }
    const std::vector<uint8_t>& prbs = prbs_table();
    for (size_t i = 0; i < soft.size(); ++i) soft[i] *= 1.0 - 2.0 * prbs[i % prbs.size()];
    if ((long long)soft.size() < needed) return;
    soft.resize(needed);
    P.complete = true;
    P.soft = soft;
    const std::vector<int32_t>& perm = interleave_perm((int)needed);
    std::vector<double> de(needed);
    for (long long i = 0; i < needed; ++i) de[perm[i]] = soft[i];
    long long nb = payload_len * 8;
    std::vector<uint8_t> cw;
    if (is_ldpc) {
        double e = 0;
        for (double v : de) e += v * v;
        double rms = std::sqrt(e / std::max<long long>(needed, 1));
        if (rms > 0) for (double& v : de) v /= rms;
        P.bits.resize(nb);
        cw.resize(ldpc_coded_len(nb));
        int ok = ldpc_decode(de.data(), (long long)de.size(), nb, LDPC_MAX_ITER, LDPC_ALPHA, LDPC_STALL_ITER,
                             P.bits.data(), cw.data(), nullptr);
        if (ok != 1) ldpc_encode(P.bits.data(), nb, cw.data());
    } else {
        P.bits = viterbi_soft(de.data(), nb);
        cw = fec_encode(P.bits.data(), P.bits.size());
    }
    const std::vector<int32_t>& p2 = interleave_perm((int)cw.size());
    P.re_encoded.resize(cw.size());
    for (size_t i = 0; i < cw.size(); ++i) P.re_encoded[i] = cw[p2[i]];
    P.bytes.assign(nb / 8, 0);
    for (long long i = 0; i < (nb / 8) * 8; ++i) P.bytes[i / 8] |= P.bits[i] << (7 - i % 8);
}

double pass_ber(const Pass& P) {
    long long d = 0, n = (long long)P.soft.size();
    for (long long i = 0; i < n; ++i) d += P.re_encoded[i] != (uint8_t)(P.soft[i] < 0);
    return n ? (double)d / n : NaN;
}

void decode_frame(const Cfg& c, const cd* rx, long long n_rx, long long max_wait, Result& R) {
    R = Result();
    int L = c.symbol_len(), n = c.n_data;
    std::vector<int> hidx = header_symbol_plan(c);
    int nh = hidx.back() + 1;
    if (n_rx < (long long)nh * L) { R.incomplete = 1; return; }
    Grid hf = demodulate(c, rx, nh), heq, heqs;
    equalize(c, hf, nh, heq, heqs);
    std::vector<double> hs;
    for (int k : hidx)
        for (int j = 0; j < n; ++j)
            if (!c.is_pilot(k, j)) soft_demap(2, heqs[(size_t)k * n + j], hs);
    const std::vector<uint8_t>& pn = header_pn((int)hs.size());
    for (size_t i = 0; i < hs.size(); ++i) if (pn[i]) hs[i] = -hs[i];
    const int clen = 2 * (HEADER_BITS + FEC_N_MEM);
    std::vector<double> comb(clen, 0.0), cnt(clen, 0.0);
    {
        size_t nfull = hs.size() / clen;
        for (size_t r = 0; r < nfull; ++r)
            for (int b = 0; b < clen; ++b) comb[b] += hs[r * clen + b];
        for (int b = 0; b < clen; ++b) cnt[b] = (double)std::max<size_t>(nfull, 1);
        for (size_t b = 0; b < hs.size() - nfull * clen; ++b) { comb[b] += hs[nfull * clen + b]; cnt[b] += 1; }
        for (int b = 0; b < clen; ++b) comb[b] /= cnt[b];
    }
    std::vector<uint8_t> hb = viterbi_soft(comb.data(), HEADER_BITS);
    auto field = [&](int off, int len) {
        uint32_t v = 0;
        for (int i = 0; i < len; ++i) v = (v << 1) | hb[off + i];
        return v;
    };
    uint32_t raw_len = field(0, 32), expected_crc = field(32, 32), chk = field(64, 8);
    uint8_t hbytes[8];
    for (int i = 0; i < 8; ++i) hbytes[i] = (uint8_t)field(8 * i, 8);
    if (chk != (crc32(hbytes, 8) & 0xFF)) return;
    bool is_ldpc = (raw_len & 0x80000000u) || c.ldpc;
    long long payload_len = raw_len & 0x7FFFFFFF;
    if (payload_len > MAX_REASONABLE_PAYLOAD_BYTES) return;
    long long needed = is_ldpc ? ldpc_coded_len(payload_len * 8) : 2 * (payload_len * 8 + FEC_N_MEM);
    int ns = symbols_until(c, nh, needed);
    long long need_samples = (long long)ns * L;
    if (max_wait >= 0 && need_samples > max_wait) return;
    if (n_rx < need_samples) { R.incomplete = 1; return; }

    Grid F = demodulate(c, rx, ns), eq, eqs;
    equalize(c, F, ns, eq, eqs);
    Pass P;
    decode_pass(c, eq, eqs, nh, ns, needed, is_ldpc, payload_len, P);
    R.evm = evm_percent(P.constel, c.bpc);
    R.constellation = P.constel;
    if (!P.complete) { R.incomplete = 1; return; }
    R.has_payload = 1;
    R.payload = P.bytes;
    R.ber = pass_ber(P);
    R.crc_ok = crc32(P.bytes.data(), P.bytes.size()) == expected_crc;
    if (R.crc_ok || R.ber >= HOPELESS_BER || c.full_training()) return;

    // retries: the other equaliser (time-first <-> per-symbol), shorter smoothing
    std::vector<std::pair<int, int>> variants;
    if (c.time_first) variants.push_back({-1, 0});
    if (RETRY_HALF_WINDOW != TIME_SMOOTHING_HALF_WINDOW) variants.push_back({RETRY_HALF_WINDOW, -1});
    for (auto& v : variants) {
        Grid e2, s2;
        equalize(c, F, ns, e2, s2, v.first, v.second);
        Pass Q;
        decode_pass(c, e2, s2, nh, ns, needed, is_ldpc, payload_len, Q);
        if (!Q.complete) continue;
        if (crc32(Q.bytes.data(), Q.bytes.size()) == expected_crc) {
            R.payload = Q.bytes; R.crc_ok = 1; R.ber = pass_ber(Q);
            R.evm = evm_percent(Q.constel, c.bpc); R.constellation = Q.constel;
            return;
        }
    }
    // decision-directed: the first pass's re-encoding as references
    std::vector<char> use_dd(ns, 0);
    Grid dd((size_t)ns * n, cd(NaN, 0));
    {
        std::vector<uint8_t> re = P.re_encoded;
        const std::vector<uint8_t>& prbs = prbs_table();
        for (size_t i = 0; i < re.size(); ++i) re[i] ^= prbs[i % prbs.size()];
        size_t pos = 0;
        for (int k = nh; k < ns; ++k) {
            int nd = c.n_data_carriers(k);
            if (nd == 0) continue;
            size_t nbits = (size_t)nd * c.bpc;
            if (pos + nbits > re.size()) break;
            const uint8_t* b = re.data() + pos;
            pos += nbits;
            for (int j = 0; j < n; ++j) {
                if (c.is_pilot(k, j)) continue;
                dd[(size_t)k * n + j] = data_map(c.bpc, b);
                b += c.bpc;
            }
            use_dd[k] = 1;
        }
    }
    Grid e3, s3;
    equalize(c, F, ns, e3, s3, -1, -1, use_dd, dd);
    Pass Q;
    decode_pass(c, e3, s3, nh, ns, needed, is_ldpc, payload_len, Q);
    if (Q.complete && crc32(Q.bytes.data(), Q.bytes.size()) == expected_crc) {
        R.payload = Q.bytes; R.crc_ok = 1; R.ber = pass_ber(Q);
        R.evm = evm_percent(Q.constel, c.bpc); R.constellation = Q.constel;
    }
}

// ======================================================================
// Sync: Schmidl & Cox, CFO rotation, preamble matched filter, integer CFO
// and timing searches (hf_ofdm_common.py). Samples come in as complex128;
// c64 says the Python caller's array was complex64, where its rotation
// rounds to single precision -- done here too, so the two see the same
// samples.

const double SYNC_METRIC_MIN = 0.3;
const int TIMING_REFINE_EARLY_MARGIN = 16;
const double CFO_PRIOR_PENALTY_PER_CARRIER = 2.0;

struct SyncParams { double fs = 0, spacing = 0; };

// _sc_metric: exact running sums, re-summed every 4096 offsets
void sc_metric(const cd* w, int L, long long n_out, int W, std::vector<double>& metric, std::vector<cd>& P,
               std::vector<double>& E12) {
    if (W < 0) W = L;
    metric.resize(n_out); P.resize(n_out); E12.resize(n_out);
    cd p(0, 0);
    double e1 = 0, e2 = 0;
    for (long long d = 0; d < n_out; ++d) {
        if (d % 4096 == 0) {
            p = cd(0, 0); e1 = 0; e2 = 0;
            for (int i = 0; i < W; ++i) {
                cd a = w[d + i], b = w[d + i + L];
                p += std::conj(a) * b;
                e1 += a.real() * a.real() + a.imag() * a.imag();
                e2 += b.real() * b.real() + b.imag() * b.imag();
            }
        } else {
            cd a0 = w[d - 1], aL = w[d - 1 + L], aW = w[d - 1 + W], b = w[d - 1 + W + L];
            p += std::conj(aW) * b - std::conj(a0) * aL;
            double pa0 = a0.real() * a0.real() + a0.imag() * a0.imag();
            double paL = aL.real() * aL.real() + aL.imag() * aL.imag();
            double paW = aW.real() * aW.real() + aW.imag() * aW.imag();
            e1 += paW - pa0;
            e2 += (b.real() * b.real() + b.imag() * b.imag()) - paL;
        }
        P[d] = p;
        E12[d] = e1 * e2;
        metric[d] = (p.real() * p.real() + p.imag() * p.imag()) / (e1 * e2 + 1e-12);
    }
}

double median(std::vector<double> v) {
    size_t n = v.size(), h = n / 2;
    std::nth_element(v.begin(), v.begin() + h, v.end());
    double hi = v[h];
    if (n % 2) return hi;
    double lo = *std::max_element(v.begin(), v.begin() + h);
    return (lo + hi) / 2;
}

size_t argmax(const std::vector<double>& v, size_t lo, size_t hi) {
    size_t k = lo;
    for (size_t i = lo + 1; i < hi; ++i) if (v[i] > v[k]) k = i;
    return k;
}

// _rotate: x[n] * exp(-i step n), phasor recomputed every 1024 samples
void rotate(const cd* x, long long n, double step, cd* out, bool c64) {
    cd w(std::cos(step), -std::sin(step));
    for (long long b0 = 0; b0 < n; b0 += 1024) {
        double ph = step * b0;
        cd z(std::cos(ph), -std::sin(ph));
        long long e = std::min(b0 + 1024, n);
        for (long long i = b0; i < e; ++i) {
            cd v = x[i] * z;
            if (c64) v = cd((float)v.real(), (float)v.imag());
            out[i] = v;
            z = z * w;
        }
    }
}

// rx: global samples [base, base + n_avail) of a stream n_rx long (the
// receiver's buffer drops old samples; indices stay global)
void schmidl_cox_sync(const Cfg& c, const SyncParams& sp, const cd* rx, long long base, long long n_avail,
                      long long n_rx, long long search_start, long long search_len, bool prefer_earliest,
                      long long& start, double& cfo, double& best) {
    int L = c.n_fft / 2, W = (c.preamble_halves - 1) * (c.n_fft / 2);
    long long max_len = n_rx - search_start - (L + W);
    if (search_len < 0) search_len = max_len;
    search_len = std::max(1LL, std::min(search_len, max_len));
    std::vector<double> metric, E12;
    std::vector<cd> P;
    // the search reads search_len + L + W samples from search_start; a buffer
    // that ends sooner (search_len clamped to 1 above) is zero-padded (the
    // numba kernel read past its window instead)
    long long need = search_len + L + W;
    long long have = std::min(base + n_avail, n_rx) - search_start;      // from search_start on
    if (need > have) {
        std::vector<cd> w(need, cd(0, 0));
        if (have > 0) std::copy(rx + (search_start - base), rx + (search_start - base) + have, w.begin());
        sc_metric(w.data(), L, search_len, W, metric, P, E12);
    } else {
        sc_metric(rx + (search_start - base), L, search_len, W, metric, P, E12);
    }
    size_t d_rel = 0;
    bool done = false;
    if (prefer_earliest) {
        std::vector<double> sub;
        for (size_t i = 0; i < E12.size(); i += 64) sub.push_back(E12[i]);
        // not a near-silent stretch either (a transmission fading into a gap
        // correlates at the preamble's lag): over 60 dB below the window's
        // strongest is ignored (hf_ofdm_common.schmidl_cox_sync)
        double mx = 0;
        for (double e : E12) mx = std::max(mx, e);
        double floor_ = std::max(1e-9 * std::max(median(sub), 1e-30), 1e-6 * mx);
        long long d0 = -1;
        for (size_t i = 0; i < metric.size(); ++i)
            if (metric[i] > SYNC_METRIC_MIN && E12[i] > floor_) { d0 = (long long)i; break; }
        if (d0 >= 0) {
            long long hard_cap = std::min((long long)metric.size(),
                                          d0 + std::max((long long)c.symbol_len() * 4, (long long)W + 2 * L));
            long long wend = hard_cap;
            for (long long i = d0; i < hard_cap; ++i)
                if (metric[i] <= SYNC_METRIC_MIN) { wend = i; break; }
            wend = std::max(wend, d0 + 1);
            d_rel = argmax(metric, d0, wend);
            done = true;
        }
    }
    if (!done) d_rel = argmax(metric, 0, metric.size());
    long long best_d = search_start + (long long)d_rel;
    best = metric[d_rel];
    double eps = std::atan2(P[d_rel].imag(), P[d_rel].real()) / fft::PI;
    cfo = eps * sp.spacing;
    start = std::max(0LL, best_d - c.n_cp);
}

int fast_fft_len(int n) {
    int m = n;
    while (max_prime_factor(m) > 7) ++m;
    return m;
}

long long refine_frame_start(const Cfg& c, const SyncParams& sp, const cd* rx, long long base, long long n_rx,
                             long long start, double cfo, double min_ratio, bool c64) {
    // template: the preamble at single precision, as the Python's
    std::vector<cd> tm(c.preamble.size());
    for (size_t i = 0; i < tm.size(); ++i) tm[i] = cd((float)c.preamble[i].real(), (float)c.preamble[i].imag());
    long long m = (long long)tm.size(), span = 3LL * c.n_cp;
    long long lo = std::max(0LL, start - span), hi = std::min(n_rx, start + m + span);
    long long n = hi - lo;
    if (n < m + 1) return start;
    std::vector<cd> seg(n);
    rotate(rx + (lo - base), n, 2 * fft::PI * cfo / sp.fs, seg.data(), c64);
    int nf = fast_fft_len((int)n);
    std::vector<cd> A(nf, cd(0, 0)), V(nf, cd(0, 0)), FA(nf), FV(nf), r(nf);
    std::copy(seg.begin(), seg.end(), A.begin());
    std::copy(tm.begin(), tm.end(), V.begin());
    fft::Plan<double>& pl = fft::plan<double>(nf);
    pl.forward(A.data(), FA.data());
    pl.forward(V.data(), FV.data());
    for (int i = 0; i < nf; ++i) FA[i] *= std::conj(FV[i]);
    pl.inverse(FA.data(), r.data());
    std::vector<double> corr(n - m + 1);
    for (long long i = 0; i <= n - m; ++i) corr[i] = std::abs(r[i] / (double)nf);
    size_t k = argmax(corr, 0, corr.size());
    double floor_ = median(corr) + 1e-12;
    if (corr[k] / floor_ < min_ratio) return start;
    return std::max(0LL, lo + (long long)k - std::min(TIMING_REFINE_EARLY_MARGIN, c.n_cp / 2));
}

// the header from equalised header symbols: soft-combine, Viterbi; metric
// and whether it parses (checksum, length)
struct HeaderScore { double metric; bool ok; };
HeaderScore score_header(const Cfg& c, const std::vector<int>& hidx, const Grid& eqs) {
    int n = c.n_data;
    std::vector<double> hs;
    for (int k : hidx)
        for (int j = 0; j < n; ++j)
            if (!c.is_pilot(k, j)) soft_demap(2, eqs[(size_t)k * n + j], hs);
    const std::vector<uint8_t>& pn = header_pn((int)hs.size());
    for (size_t i = 0; i < hs.size(); ++i) if (pn[i]) hs[i] = -hs[i];
    const int clen = 2 * (HEADER_BITS + FEC_N_MEM);
    std::vector<double> comb(clen, 0.0), cnt(clen);
    size_t nfull = hs.size() / clen;
    for (size_t r = 0; r < nfull; ++r)
        for (int b = 0; b < clen; ++b) comb[b] += hs[r * clen + b];
    for (int b = 0; b < clen; ++b) cnt[b] = (double)std::max<size_t>(nfull, 1);
    for (size_t b = 0; b < hs.size() - nfull * clen; ++b) { comb[b] += hs[nfull * clen + b]; cnt[b] += 1; }
    for (int b = 0; b < clen; ++b) comb[b] /= cnt[b];
    HeaderScore s;
    std::vector<uint8_t> hb = viterbi_soft(comb.data(), HEADER_BITS, &s.metric);
    uint8_t by[9] = {0};
    for (int i = 0; i < HEADER_BITS; ++i) by[i / 8] |= hb[i] << (7 - i % 8);
    uint32_t raw = ((uint32_t)by[0] << 24) | ((uint32_t)by[1] << 16) | ((uint32_t)by[2] << 8) | by[3];
    s.ok = (raw & 0x7FFFFFFF) <= MAX_REASONABLE_PAYLOAD_BYTES && by[8] == (crc32(by, 8) & 0xFF);
    return s;
}

// _repetition_confidence on hard bits from eq
double repetition_confidence(const Cfg& c, const std::vector<int>& hidx, const Grid& eq) {
    int n = c.n_data;
    std::vector<uint8_t> hard;
    for (int k : hidx)
        for (int j = 0; j < n; ++j)
            if (!c.is_pilot(k, j)) {
                cd v = eq[(size_t)k * n + j];
                hard.push_back(v.real() < 0);
                hard.push_back(v.imag() < 0);
            }
    const std::vector<uint8_t>& pn = header_pn((int)hard.size());
    for (size_t i = 0; i < hard.size(); ++i) hard[i] ^= pn[i];
    const size_t clen = 2 * (HEADER_BITS + FEC_N_MEM);
    size_t nr = hard.size() / clen;
    if (!nr) return 0.0;
    long long agree = 0;
    for (size_t b = 0; b < clen; ++b) {
        long long ones = 0;
        for (size_t r = 0; r < nr; ++r) ones += hard[r * clen + b];
        int maj = (double)ones / nr >= 0.5;
        for (size_t r = 0; r < nr; ++r) agree += hard[r * clen + b] == maj;
    }
    return (double)agree / (nr * clen);
}

void resolve_integer_cfo(const Cfg& c, const SyncParams& sp, const cd* rx, long long n_rx, int search_range,
                         bool has_prior, int prior_k, int& out_k, double& out_conf, bool c64) {
    std::vector<int> hidx = header_symbol_plan(c);
    int nh = hidx.back() + 1, n = c.n_data, N = c.n_fft, L = c.symbol_len();
    long long needed = (long long)nh * L;
    out_k = 0; out_conf = 0.0;
    if (n_rx < needed) return;
    bool exact = std::fabs(sp.spacing * N - sp.fs) <= 1e-9 * sp.fs;
    std::vector<cd> full;   // nh x N header FFTs
    if (exact) {
        full.resize((size_t)nh * N);
        std::vector<cd> t(N);
        fft::Plan<double>& pl = fft::plan<double>(N);
        double s = 1 / std::sqrt((double)N);
        for (int k = 0; k < nh; ++k) {
            pl.forward(rx + (size_t)k * L + c.n_cp, t.data());
            for (int i = 0; i < N; ++i) {
                cd v = t[i] * s;
                if (c64) v = cd((float)v.real(), (float)v.imag());
                full[(size_t)k * N + i] = v;
            }
        }
    }
    // the compiled (batched) path of the Python: per-symbol comb equaliser
    // for comb-pilot modes, estimate_and_equalize (training) for D
    auto score = [&](const std::vector<int>& cands, int& bk, double& bconf, bool& sane, int& sk, double& sconf) {
        double bm = std::numeric_limits<double>::infinity(), sm = bm;
        bk = 0; bconf = -1; sane = false; sk = 0; sconf = -1;
        Grid hf((size_t)nh * n), eq, eqs;
        std::vector<cd> shifted;
        for (int cand : cands) {
            if (exact) {
                for (int k = 0; k < nh; ++k) {
                    double sidx = (double)k * L + c.n_cp;
                    cd ph = std::exp(cd(0, 2 * fft::PI * cand * sidx / N));
                    for (int j = 0; j < n; ++j) hf[(size_t)k * n + j] = full[(size_t)k * N + pymod(c.fft_bins[j] - cand, N)] * ph;
                }
            } else {
                shifted.resize(needed);
                rotate(rx, needed, 2 * fft::PI * (-cand * sp.spacing) / sp.fs, shifted.data(), c64);
                hf = demodulate(c, shifted.data(), nh);
            }
            if (!exact || c.full_training()) equalize(c, hf, nh, eq, eqs);
            else eq_comb(c, hf, nh, TIME_SMOOTHING_HALF_WINDOW, {}, {}, eq, eqs);
            HeaderScore hs = score_header(c, hidx, eqs);
            double conf = repetition_confidence(c, hidx, eq);
            double m = hs.metric;
            if (has_prior) m += std::abs(cand - prior_k) * CFO_PRIOR_PENALTY_PER_CARRIER;
            if (m < bm) { bm = m; bk = cand; bconf = conf; }
            if (hs.ok && m < sm) { sm = m; sk = cand; sconf = conf; sane = true; }
        }
    };
    int bk, sk; double bc, sc; bool sane;
    if (has_prior) {
        std::vector<int> local;
        for (int k = prior_k - 1; k <= prior_k + 1; ++k) if (k >= -search_range && k <= search_range) local.push_back(k);
        if (!local.empty()) {
            score(local, bk, bc, sane, sk, sc);
            if (sane) { out_k = sk; out_conf = sc; return; }
        }
    }
    std::vector<int> all;
    for (int k = -search_range; k <= search_range; ++k) all.push_back(k);
    score(all, bk, bc, sane, sk, sc);
    if (sane) { out_k = sk; out_conf = sc; }
    else { out_k = bk; out_conf = bc; }
}

void resolve_timing_offset(const Cfg& c, const SyncParams& sp, const cd* rx, long long base, long long n_rx,
                           long long start,
                           double cfo, const long long* deltas, int nd, long long& out_delta, double& out_metric,
                           bool& out_ok, bool c64) {
    std::vector<int> hidx = header_symbol_plan(c);
    int nh = hidx.back() + 1, L = c.symbol_len();
    long long needed = (long long)nh * L, probe_len = c.preamble_len() + needed;
    double bm = std::numeric_limits<double>::infinity(), sm = bm;
    long long bd = 0, sd = 0;
    bool sane = false;
    std::vector<cd> pr(probe_len);
    for (int i = 0; i < nd; ++i) {
        long long ps = start + deltas[i];
        if (ps < 0 || ps < base || ps + probe_len > n_rx) continue;
        rotate(rx + (ps - base), probe_len, 2 * fft::PI * cfo / sp.fs, pr.data(), c64);
        Grid hf = demodulate(c, pr.data() + c.preamble_len(), nh), eq, eqs;
        equalize(c, hf, nh, eq, eqs);
        HeaderScore hs = score_header(c, hidx, eqs);
        if (hs.metric < bm) { bm = hs.metric; bd = deltas[i]; }
        if (hs.ok && hs.metric < sm) { sm = hs.metric; sd = deltas[i]; sane = true; }
    }
    if (sane) { out_delta = sd; out_metric = sm; out_ok = true; }
    else { out_delta = bd; out_metric = bm; out_ok = false; }
}

struct RxHandle {
    Cfg* cfg;
    SyncParams sp;
    Result res;
};

}  // namespace

TX_API void* rx_new(void* tx_cfg, int time_first, double fs, double carrier_spacing) {
    RxHandle* h = new RxHandle();
    h->cfg = (Cfg*)tx_cfg;
    h->cfg->time_first = time_first != 0;
    h->sp.fs = fs;
    h->sp.spacing = carrier_spacing;
    return h;
}

// search_len < 0: to the end
// rx: global samples [base, base + n_avail) of a buffer n_total long
TX_API void rx_sc_sync(void* hh, const cd* rx, long long base, long long n_avail, long long n_total,
                       long long search_start, long long search_len, int prefer_earliest, long long* start,
                       double* cfo, double* metric) {
    RxHandle* h = (RxHandle*)hh;
    schmidl_cox_sync(*h->cfg, h->sp, rx, base, n_avail, n_total, search_start, search_len, prefer_earliest != 0,
                     *start, *cfo, *metric);
}

TX_API void rx_quick_sc_scan(const cd* x, long long n, int L, long long* d, double* metric) {
    long long max_d = n - 2LL * L;
    *d = 0; *metric = 0.0;
    if (max_d <= 0) return;
    std::vector<double> m, e;
    std::vector<cd> P;
    sc_metric(x, L, max_d, L, m, P, e);
    size_t k = argmax(m, 0, m.size());
    *d = (long long)k;
    *metric = m[k];
}

TX_API void rx_rotate(const cd* x, long long n, double step, cd* out, int c64) { rotate(x, n, step, out, c64 != 0); }

TX_API long long rx_refine_frame_start(void* hh, const cd* rx, long long base, long long n_total, long long start,
                                       double cfo, double min_ratio, int c64) {
    RxHandle* h = (RxHandle*)hh;
    return refine_frame_start(*h->cfg, h->sp, rx, base, n_total, start, cfo, min_ratio, c64 != 0);
}

TX_API void rx_resolve_integer_cfo(void* hh, const cd* rx, long long n, int search_range, int has_prior,
                                   int prior_k, int* k, double* conf, int c64) {
    RxHandle* h = (RxHandle*)hh;
    resolve_integer_cfo(*h->cfg, h->sp, rx, n, search_range, has_prior != 0, prior_k, *k, *conf, c64 != 0);
}

TX_API void rx_resolve_timing_offset(void* hh, const cd* rx, long long base, long long n_total, long long start,
                                     double cfo, const long long* deltas, int nd, long long* delta, double* metric,
                                     int* ok, int c64) {
    RxHandle* h = (RxHandle*)hh;
    bool o;
    resolve_timing_offset(*h->cfg, h->sp, rx, base, n_total, start, cfo, deltas, nd, *delta, *metric, o, c64 != 0);
    *ok = o;
}
TX_API void rx_free(void* h) { delete (RxHandle*)h; }

// returns flags: bit0 payload present, bit1 crc ok, bit2 incomplete;
// ber/evm NaN when absent; lengths of payload and constellation
TX_API int rx_decode_frame(void* hh, const cd* rx, long long n, long long max_wait, double* ber, double* evm,
                           long long* payload_len, long long* n_points) {
    RxHandle* h = (RxHandle*)hh;
    decode_frame(*h->cfg, rx, n, max_wait, h->res);
    *ber = h->res.ber;
    *evm = h->res.evm;
    *payload_len = (long long)h->res.payload.size();
    *n_points = (long long)h->res.constellation.size();
    return h->res.has_payload | (h->res.crc_ok << 1) | (h->res.incomplete << 2);
}
TX_API void rx_copy(void* hh, uint8_t* payload, cd* points) {
    RxHandle* h = (RxHandle*)hh;
    if (payload) std::memcpy(payload, h->res.payload.data(), h->res.payload.size());
    if (points) std::memcpy(points, h->res.constellation.data(), h->res.constellation.size() * sizeof(cd));
}

// ======================================================================
// Front end kernels (hf_ofdm_common.py): the IQ-imbalance transform
// (_iq_apply), causal FIR filtering (np.convolve "valid" with the
// low-pass taps) and linear interpolation (np.interp, as linear_resample
// uses it). The growable-buffer caches around them stay in Python for now
// (frontend_cpp.py).

// out = (I0 + mI) + j((Q0 - slope I0) scale + mQ), I0 = re - mI, Q0 = im - mQ
TX_API void fe_iq_apply(const cd* x, long long n, double mI, double mQ, double slope, double scale, cd* out) {
    for (long long i = 0; i < n; ++i) {
        double I0 = x[i].real() - mI, Q0 = x[i].imag() - mQ;
        double Q1 = (Q0 - slope * I0) * scale;
        out[i] = cd(I0 + mI, Q1 + mQ);
    }
}

// y[i] = sum_k x[i + k] * taps[nt - 1 - k], i = 0 .. n - nt (np.convolve valid)
TX_API void fe_fir_valid(const cd* x, long long n, const double* taps, int nt, cd* out) {
    std::vector<double> rt(taps, taps + nt);
    std::reverse(rt.begin(), rt.end());
    long long n_out = n - nt + 1;
    if (n_out <= 0) return;
    // split re/im, then BLK outputs at a time: each output still sums its
    // taps in order k = 0.., but the BLK independent sums vectorise
    std::vector<double> xr(n), xi(n);
    for (long long i = 0; i < n; ++i) { xr[i] = x[i].real(); xi[i] = x[i].imag(); }
    const int BLK = 16;
    long long i = 0;
    for (; i + BLK <= n_out; i += BLK) {
        double ar[BLK] = {0}, ai[BLK] = {0};
        for (int k = 0; k < nt; ++k) {
            const double t = rt[k];
            const double* pr = &xr[i + k];
            const double* pi = &xi[i + k];
            for (int j = 0; j < BLK; ++j) { ar[j] += pr[j] * t; ai[j] += pi[j] * t; }
        }
        for (int j = 0; j < BLK; ++j) out[i + j] = cd(ar[j], ai[j]);
    }
    for (; i < n_out; ++i) {
        double re = 0, im = 0;
        for (int k = 0; k < nt; ++k) { re += xr[i + k] * rt[k]; im += xi[i + k] * rt[k]; }
        out[i] = cd(re, im);
    }
}

// np.interp(arange(o0, o1) / fs_out, arange(i0, i0 + n) / fs_in, x) for the
// real and imaginary parts (clamped ends, numpy's slope form)
TX_API void fe_interp(const cd* x, long long n, long long i0, double fs_in, double fs_out, long long o0,
                      long long o1, cd* out) {
    if (n <= 0) return;
    auto xp = [&](long long j) { return (double)(i0 + j) / fs_in; };
    long long j = 0;
    double first = xp(0), last = xp(n - 1);
    for (long long o = o0; o < o1; ++o) {
        double t = (double)o / fs_out;
        cd v;
        if (t < first) v = x[0];
        else if (t > last) v = x[n - 1];
        else {
            while (j + 1 < n && xp(j + 1) <= t) ++j;
            if (j >= n - 1) v = x[n - 1];
            else {
                double x0 = xp(j), dx = xp(j + 1) - x0;
                double sr = (x[j + 1].real() - x[j].real()) / dx, si = (x[j + 1].imag() - x[j].imag()) / dx;
                double re = sr * (t - x0) + x[j].real(), im = si * (t - x0) + x[j].imag();
                if (std::isnan(re)) re = sr * (t - xp(j + 1)) + x[j + 1].real();
                if (std::isnan(im)) im = si * (t - xp(j + 1)) + x[j + 1].imag();
                v = cd(re, im);
            }
        }
        out[o - o0] = v;
    }
}

// ======================================================================
// Mode S receiver kernels (hf_sc.py): matched filter, preamble detection,
// timing + frequency sync, header, and the data demodulator (pilot timing
// line, smoothed pilot + soft decision-directed phase, short least-squares
// equaliser, exact LLRs) with the LDPC decode. Python's receive_one_fragment
// (waiting for samples, choosing segments) calls these. The Python works in
// single precision, this in double: same decisions, metrics to rounding.
namespace {

const int S_PRE_LEN = 256, S_SPS = 4, S_RRC_DELAY = 32, S_HDR_BITS = 24, S_HDR_REPEAT = 8, S_DATA_BLOCK = 128,
          S_PILOT_LEN = 8, S_PHASE_SMOOTH = 5, S_DD_WINDOW = 32, S_DD_SMOOTH = 5, S_EQ_PRE = 3, S_EQ_POST = 8;
const double S_DETECT_THRESHOLD = 0.32, S_LLR_CLIP = 40.0, S_EQ_RIDGE = 0.01, S_EQ_MIN_GAIN_DB = 0.5;
const int S_FINE_LAGS[3] = {16, 64, 128};

inline cd round_c64(cd v) { return cd((float)v.real(), (float)v.imag()); }

// _mf: RRC matched filter, delay removed, rounded to complex64
TX_API void fe_fir_valid(const cd* x, long long n, const double* taps, int nt, cd* out);

std::vector<cd> s_mf(const SC& s, const cd* x, long long n) {
    // the full convolution's [DELAY, DELAY + n): a "valid" FIR over x with
    // DELAY zeros either side (the vectorised kernel)
    int L = (int)s.rrc.size();
    std::vector<cd> p(n + 2 * S_RRC_DELAY, cd(0, 0)), y(n);
    std::copy(x, x + n, p.begin() + S_RRC_DELAY);
    std::vector<double> taps(s.rrc.begin(), s.rrc.end());
    fe_fir_valid(p.data(), (long long)p.size(), taps.data(), L, y.data());
    for (cd& v : y) v = round_c64(v);
    return y;
}

std::vector<cd> s_derotated_mf(const SC& s, const cd* x, long long n, double cfo) {
    std::vector<cd> r(n);
    for (long long k = 0; k < n; ++k) r[k] = round_c64(x[k] * std::exp(cd(0, -2 * fft::PI * cfo * (double)k / S_SPS)));
    return s_mf(s, r.data(), n);
}

// _cubic: 4-point Lagrange at fractional position t
inline cd s_cubic(const std::vector<cd>& z, double t) {
    long long i = (long long)std::floor(t);
    float mu = (float)(t - (double)i);
    long long n = (long long)z.size();
    i = std::min(std::max(i, 1LL), n - 3);
    float c0 = -mu * (mu - 1) * (mu - 2) / 6, c1 = (mu + 1) * (mu - 1) * (mu - 2) / 2;
    float c2 = -(mu + 1) * mu * (mu - 2) / 2, c3 = (mu + 1) * mu * (mu - 1) / 6;
    return z[i - 1] * (double)c0 + z[i] * (double)c1 + z[i + 1] * (double)c2 + z[i + 2] * (double)c3;
}

// _fine_timing: correlation at -2..+2 samples, parabola through the peak
double s_fine_timing(const std::vector<cd>& z, double t_first, const cd* known, int n_known) {
    double v[5];
    for (int o = -2; o <= 2; ++o) {
        cd acc(0, 0);
        for (int k = 0; k < n_known; ++k) acc += std::conj(known[k]) * s_cubic(z, t_first + (double)k * S_SPS + o);
        v[o + 2] = std::abs(acc);
    }
    int k = 0;
    for (int j = 1; j < 5; ++j) if (v[j] > v[k]) k = j;
    if (k > 0 && k < 4) {
        double den = v[k - 1] - 2 * v[k] + v[k + 1];
        return (k - 2) + (den != 0 ? 0.5 * (v[k - 1] - v[k + 1]) / den : 0.0);
    }
    return (double)(k - 2);
}

std::vector<cd> s_preamble_c(const SC& s) {
    std::vector<cd> p(s.preamble.size());
    for (size_t i = 0; i < p.size(); ++i) p[i] = cd(s.preamble[i].real(), s.preamble[i].imag());
    return p;
}

// detect: (index of symbol 0's peak, metric, coarse cfo cycles/symbol)
bool s_detect(const SC& s, const cd* z, long long n, long long& out_i, double& out_m, double& out_cfo) {
    if (n < (long long)S_PRE_LEN * S_SPS + S_SPS) return false;
    long long nd = n - S_SPS;
    std::vector<cd> d(nd);
    for (long long k = 0; k < nd; ++k) d[k] = round_c64(z[k + S_SPS] * std::conj(z[k]));
    const int npd = S_PRE_LEN - 1, span = (npd - 1) * S_SPS + 1;
    if (nd < span) return false;
    long long nc = nd - span + 1;
    // c[i] = sum_j pd1[j] d[i + SPS j], by FFT (power of two, as the Python)
    long long nf = 1;
    while (nf < nd + span) nf <<= 1;
    std::vector<cd> A(nf, cd(0, 0)), T(nf, cd(0, 0)), FA(nf), FT(nf), r(nf);
    std::copy(d.begin(), d.end(), A.begin());
    for (int j = 0; j < npd; ++j)
        T[(size_t)j * S_SPS] = cd(s.preamble[j + 1].real() * s.preamble[j].real(), 0);
    fft::Plan<double>& pl = fft::plan<double>((int)nf);
    pl.forward(A.data(), FA.data());
    pl.forward(T.data(), FT.data());
    for (long long i = 0; i < nf; ++i) FA[i] *= std::conj(FT[i]);
    pl.inverse(FA.data(), r.data());
    // energy under the template's taps exactly: running sums along each phase
    std::vector<double> run(nd), m(nc);
    for (long long k = 0; k < nd; ++k) run[k] = std::abs(d[k]) + (k >= S_SPS ? run[k - S_SPS] : 0.0);
    long long i0 = -1;
    for (long long i = 0; i < nc; ++i) {
        double e = run[i + span - 1] - (i >= S_SPS ? run[i - S_SPS] : 0.0);
        m[i] = std::abs(r[i] / (double)nf) / std::max(e, 1e-12);
        if (i0 < 0 && m[i] > S_DETECT_THRESHOLD) i0 = i;
    }
    if (i0 < 0) return false;
    // a crossing within a span of the end may be a burst only just arriving:
    // wait for its full alignment (see hf_sc.detect)
    if (i0 + span > nc) return false;
    long long hi = std::min(nc, i0 + span + (long long)S_PRE_LEN * S_SPS / 2), best = i0;
    for (long long i = i0 + 1; i < hi; ++i) if (m[i] > m[best]) best = i;
    cd cb = r[best] / (double)nf;
    out_i = best;
    out_m = m[best];
    out_cfo = std::atan2(cb.imag(), cb.real()) / (2 * fft::PI);
    return true;
}

// _sync: (t0, cfo, matched-filtered x on that frequency)
void s_sync(const SC& s, const cd* x, long long n, long long i, double cfo1, double& t0, double& cfo,
            std::vector<cd>& zz) {
    std::vector<cd> pre = s_preamble_c(s);
    zz = s_derotated_mf(s, x, n, cfo1);
    t0 = i + s_fine_timing(zz, (double)i, pre.data(), S_PRE_LEN);
    cfo = cfo1;
    std::vector<cd> sv(S_PRE_LEN);
    for (int lag : S_FINE_LAGS) {
        for (int k = 0; k < S_PRE_LEN; ++k) sv[k] = s_cubic(zz, t0 + (double)k * S_SPS) * pre[k];
        cd acc(0, 0);
        for (int k = 0; k + lag < S_PRE_LEN; ++k) acc += std::conj(sv[k]) * sv[k + lag];
        cfo += std::atan2(acc.imag(), acc.real()) / (2 * fft::PI * lag);
        zz = s_derotated_mf(s, x, n, cfo);
        t0 = i + s_fine_timing(zz, (double)i, pre.data(), S_PRE_LEN);
    }
}

cd s_ref(const SC& s, const std::vector<cd>& zz, double t0) {
    cd acc(0, 0);
    for (int k = 0; k < S_PRE_LEN; ++k) acc += (double)s.preamble[k].real() * s_cubic(zz, t0 + (double)k * S_SPS);
    acc /= (double)S_PRE_LEN;
    return acc / std::max(std::abs(acc), 1e-12);
}

int s_header(const SC& s, const std::vector<cd>& zz, double t0, cd ref) {
    uint8_t bits[S_HDR_BITS];
    for (int b = 0; b < S_HDR_BITS; ++b) {
        double soft = 0;
        for (int r = 0; r < S_HDR_REPEAT; ++r) {
            int k = b * S_HDR_REPEAT + r;
            double y = (s_cubic(zz, t0 + (double)(S_PRE_LEN + k) * S_SPS) * std::conj(ref)).real();
            soft += y * (1 - 2.0 * s.hdr_scramble[k]);
        }
        bits[b] = soft < 0;
    }
    uint8_t c = 0;
    for (int i = 0; i < 8; ++i) c = (uint8_t)((c << 1) | bits[16 + i]);
    if (crc8_bits(bits, 16) != c) return -1;
    int n = 0;
    for (int i = 0; i < 16; ++i) n = (n << 1) | bits[i];
    return n;
}

void unwrap_v(std::vector<double>& p) {
    std::vector<double> o;
    unwrap(p, o);
    p = o;
}

// _smooth (fixed form: centred full convolution, len(v) out)
std::vector<double> s_smooth(const std::vector<double>& v, int n) {
    if (n <= 1 || v.size() < 2) return v;
    int L = (int)v.size(), lo = (n - 1) / 2;
    std::vector<double> out(L);
    for (int i = 0; i < L; ++i) {
        double sum = 0;
        int cnt = 0;
        for (int j = i + lo - (n - 1); j <= i + lo; ++j)
            if (j >= 0 && j < L) { sum += v[j]; ++cnt; }
        out[i] = sum / cnt;
    }
    return out;
}

// np.interp(arange(n), xp, fp) for increasing xp
std::vector<double> interp_at(int n, const std::vector<double>& xp, const std::vector<double>& fp) {
    std::vector<double> out(n);
    size_t j = 0, m = xp.size();
    for (int i = 0; i < n; ++i) {
        double x = i;
        if (x <= xp[0]) { out[i] = fp[0]; continue; }
        if (x >= xp[m - 1]) { out[i] = fp[m - 1]; continue; }
        while (xp[j + 1] <= x) ++j;
        double sl = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
        out[i] = sl * (x - xp[j]) + fp[j];
    }
    return out;
}

cd s_slice(const std::vector<cf>& cst, cd y) {
    int b = 0;
    double bd = 1e300;
    for (size_t k = 0; k < cst.size(); ++k) {
        double d = std::abs(y - cd(cst[k].real(), cst[k].imag()));
        if (d < bd) { bd = d; b = (int)k; }
    }
    return cd(cst[b].real(), cst[b].imag());
}

// complex linear solve (partial pivoting)
std::vector<cd> csolve(std::vector<cd> A, std::vector<cd> b, int n) {
    for (int c = 0; c < n; ++c) {
        int p = c;
        for (int r = c + 1; r < n; ++r) if (std::abs(A[r * n + c]) > std::abs(A[p * n + c])) p = r;
        if (p != c) {
            for (int k = 0; k < n; ++k) std::swap(A[c * n + k], A[p * n + k]);
            std::swap(b[c], b[p]);
        }
        for (int r = c + 1; r < n; ++r) {
            cd f = A[r * n + c] / A[c * n + c];
            for (int k = c; k < n; ++k) A[r * n + k] -= f * A[c * n + k];
            b[r] -= f * b[c];
        }
    }
    std::vector<cd> x(n);
    for (int r = n - 1; r >= 0; --r) {
        cd s = b[r];
        for (int k = r + 1; k < n; ++k) s -= A[r * n + k] * x[k];
        x[r] = s / A[r * n + r];
    }
    return x;
}

// _equalise
std::vector<cd> s_equalise(const std::vector<cd>& y, const std::vector<char>& known, const std::vector<cd>& pref,
                           const std::vector<cf>& cst) {
    const int nt = S_EQ_PRE + S_EQ_POST + 1;
    long long n = (long long)y.size();
    auto Yat = [&](long long k, int j) {      // column j: offset o = j - EQ_PRE, row k sees y[k - o]
        long long q = k - (j - S_EQ_PRE);
        return (q >= 0 && q < n) ? y[q] : cd(0, 0);
    };
    std::vector<cd> out = y;
    for (int pass = 0; pass < 2; ++pass) {
        std::vector<cd> M((size_t)nt * nt, cd(0, 0)), rhs(nt, cd(0, 0));
        long long cnt = 0;
        std::vector<cd> t(n);
        for (long long k = 0; k < n; ++k) {
            if (pass == 0 && !known[k]) continue;
            t[k] = known[k] ? pref[k] : s_slice(cst, out[k]);
            ++cnt;
            for (int a = 0; a < nt; ++a) {
                cd ya = std::conj(Yat(k, a));
                rhs[a] += ya * t[k];
                for (int b = 0; b < nt; ++b) M[a * nt + b] += ya * Yat(k, b);
            }
        }
        for (int a = 0; a < nt; ++a) M[a * nt + a] += S_EQ_RIDGE * (double)cnt;
        std::vector<cd> w = csolve(M, rhs, nt);
        std::vector<cd> o(n);
        for (long long k = 0; k < n; ++k) {
            cd acc(0, 0);
            for (int j = 0; j < nt; ++j) acc += Yat(k, j) * w[j];
            o[k] = round_c64(acc);
        }
        if (pass == 0) {
            auto fit_err = [&](const std::vector<cd>& v) {
                cd num(0, 0);
                double den = 0;
                for (long long k = 0; k < n; ++k) if (known[k]) { num += std::conj(v[k]) * t[k]; den += std::norm(v[k]); }
                cd g = num / std::max(den, 1e-12);
                double e = 0;
                for (long long k = 0; k < n; ++k) if (known[k]) e += std::norm(g * v[k] - t[k]);
                return e / cnt;
            };
            if (fit_err(o) > fit_err(y) * std::pow(10.0, -S_EQ_MIN_GAIN_DB / 10)) return y;
        }
        out = o;
    }
    return out;
}

double logaddexp(double a, double b) {
    if (a == b) return a + std::log(2.0);
    double t = a - b;
    return t > 0 ? a + std::log1p(std::exp(-t)) : b + std::log1p(std::exp(t));
}

struct SDecoded {
    std::vector<uint8_t> payload;
    bool crc_ok = false;
    double ber = 0, evm = 0;
    std::vector<cd> d;
};

void s_decode(const SC& s, const std::vector<cd>& zz, double t0, int n_bytes, SDecoded& R) {
    const int bps = s.bps;
    std::vector<cd> cst(s.cst.size());
    for (size_t k = 0; k < cst.size(); ++k) cst[k] = cd(s.cst[k].real(), s.cst[k].imag());
    long long n_coded = ldpc_coded_len((long long)(n_bytes + 4) * 8);
    long long n_data = (n_coded + bps - 1) / bps, n_blocks = (n_data + S_DATA_BLOCK - 1) / S_DATA_BLOCK,
              n_pb = n_blocks + 1;
    std::vector<long long> pilot_starts, data_pos;
    long long pos = 0;
    for (long long b = 0; b < n_pb; ++b) {
        pilot_starts.push_back(pos);
        pos += S_PILOT_LEN;
        if (b < n_pb - 1) {
            long long k = std::min((long long)S_DATA_BLOCK, n_data - b * S_DATA_BLOCK);
            for (long long j = 0; j < k; ++j) data_pos.push_back(pos + j);
            pos += k;
        }
    }
    const long long base = S_PRE_LEN + S_HDR_BITS * S_HDR_REPEAT, n_body = pos;
    std::vector<cd> pil(s.pilots.size());
    for (size_t k = 0; k < pil.size(); ++k) pil[k] = cd(s.pilots[k].real(), s.pilots[k].imag());
    // timing: a (robust) line through each pilot block's own offset
    std::vector<double> cen, off;
    for (long long b = 0; b < n_pb; ++b) {
        double tn = t0 + (double)(base + pilot_starts[b]) * S_SPS;
        off.push_back(s_fine_timing(zz, tn, &pil[b * S_PILOT_LEN], S_PILOT_LEN));
        cen.push_back((double)(base + pilot_starts[b]) + S_PILOT_LEN / 2.0);
    }
    auto linefit = [&](const std::vector<char>& use, double& a, double& b0) {
        double sx = 0, sy = 0;
        int c = 0;
        for (size_t i = 0; i < cen.size(); ++i) if (use[i]) { sx += cen[i]; sy += off[i]; ++c; }
        double mx = sx / c, my = sy / c, sxx = 0, sxy = 0;
        for (size_t i = 0; i < cen.size(); ++i)
            if (use[i]) { sxx += (cen[i] - mx) * (cen[i] - mx); sxy += (cen[i] - mx) * (off[i] - my); }
        a = sxx > 0 ? sxy / sxx : 0.0;
        b0 = my - a * mx;
    };
    double pa = 0, pb = 0;
    if (cen.size() >= 3) {
        std::vector<char> all(cen.size(), 1);
        linefit(all, pa, pb);
        std::vector<double> r(cen.size());
        for (size_t i = 0; i < cen.size(); ++i) r[i] = std::fabs(off[i] - (pa * cen[i] + pb));
        std::vector<double> sr = r;
        std::sort(sr.begin(), sr.end());
        double q = 0.8 * (sr.size() - 1);
        size_t qi = (size_t)std::floor(q);
        double p80 = qi + 1 < sr.size() ? sr[qi] + (q - qi) * (sr[qi + 1] - sr[qi]) : sr[qi];
        std::vector<char> keep(cen.size());
        for (size_t i = 0; i < cen.size(); ++i) keep[i] = r[i] <= std::max(0.25, p80);
        linefit(keep, pa, pb);
    } else {
        double m = 0;
        for (double o : off) m += o;
        pa = 0; pb = m / off.size();
    }
    std::vector<cd> y(n_body);
    for (long long k = 0; k < n_body; ++k) {
        double si = (double)(base + k);
        y[k] = round_c64(s_cubic(zz, t0 + si * S_SPS + (pa * si + pb)));
    }
    // phase: smoothed pilot phases, then soft decision-directed refinement
    std::vector<double> ph, pc;
    for (long long b = 0; b < n_pb; ++b) {
        cd g(0, 0);
        for (int k = 0; k < S_PILOT_LEN; ++k) g += std::conj(pil[b * S_PILOT_LEN + k]) * y[pilot_starts[b] + k];
        g /= (double)S_PILOT_LEN;
        ph.push_back(std::atan2(g.imag(), g.real()));
        pc.push_back((double)pilot_starts[b] + S_PILOT_LEN / 2.0);
    }
    unwrap_v(ph);
    ph = s_smooth(ph, S_PHASE_SMOOTH);
    std::vector<double> pha = interp_at((int)n_body, pc, ph);
    for (long long k = 0; k < n_body; ++k) y[k] = round_c64(y[k] * std::exp(cd(0, -pha[k])));
    std::vector<char> known(n_body, 0);
    std::vector<cd> pref(n_body, cd(0, 0));
    for (long long b = 0; b < n_pb; ++b)
        for (int k = 0; k < S_PILOT_LEN; ++k) {
            known[pilot_starts[b] + k] = 1;
            pref[pilot_starts[b] + k] = pil[b * S_PILOT_LEN + k];
        }
    long long nk = n_pb * S_PILOT_LEN;
    double amp0 = 0;
    for (long long k = 0; k < n_body; ++k) if (known[k]) amp0 += (std::conj(pref[k]) * y[k]).real();
    amp0 = std::max(amp0 / nk, 1e-9);
    double nv0 = 0;
    for (long long k = 0; k < n_body; ++k) if (known[k]) nv0 += std::norm(y[k] / amp0 - pref[k]);
    nv0 = std::max(nv0 / nk, 1e-3);
    for (int it = 0; it < 2; ++it) {
        long long n_win = (n_body + S_DD_WINDOW - 1) / S_DD_WINDOW;
        std::vector<cd> e(n_win, cd(0, 0));
        for (long long k = 0; k < n_body; ++k) {
            cd dec;
            if (known[k]) dec = pref[k];
            else {
                cd yn = y[k] / amp0, num(0, 0);
                double den = 0;
                for (const cd& c : cst) {
                    double w = std::exp(-(std::norm(yn - c) - 0.0) / nv0);
                    num += w * c;
                    den += w;
                }
                dec = num / std::max(den, 1e-30);
            }
            e[k / S_DD_WINDOW] += round_c64(y[k] * std::conj(dec));
        }
        std::vector<double> dph(n_win), cw(n_win);
        for (long long w = 0; w < n_win; ++w) {
            dph[w] = std::atan2(e[w].imag(), e[w].real());
            cw[w] = (double)w * S_DD_WINDOW + S_DD_WINDOW / 2.0;
        }
        unwrap_v(dph);
        dph = s_smooth(dph, S_DD_SMOOTH);
        std::vector<double> a = interp_at((int)n_body, cw, dph);
        for (long long k = 0; k < n_body; ++k) y[k] = round_c64(y[k] * std::exp(cd(0, -a[k])));
    }
    double amp = 0;
    for (long long k = 0; k < n_body; ++k) if (known[k]) amp += (std::conj(pref[k]) * y[k]).real();
    amp /= nk;
    for (cd& v : y) v = round_c64(v / std::max(amp, 1e-9));
    y = s_equalise(y, known, pref, s.cst);
    // noise, MER, LLRs
    R.d.resize(data_pos.size());
    double sp = 0, sd = 0;
    for (long long b = 0; b < n_pb; ++b)
        for (int k = 0; k < S_PILOT_LEN; ++k) sp += std::norm(y[pilot_starts[b] + k] - pil[b * S_PILOT_LEN + k]);
    for (size_t i = 0; i < data_pos.size(); ++i) {
        R.d[i] = y[data_pos[i]];
        sd += std::norm(R.d[i] - s_slice(s.cst, R.d[i]));
    }
    double nv = std::max((sp + sd) / (nk + (double)data_pos.size()), 1e-4);
    double mer = -10 * std::log10(std::max(sp / nk, 1e-4));
    int npts = (int)cst.size();
    std::vector<double> llr(data_pos.size() * bps), m(npts);
    for (size_t i = 0; i < data_pos.size(); ++i) {
        for (int k = 0; k < npts; ++k) m[k] = -std::norm(R.d[i] - cst[k]) / nv;
        for (int b = 0; b < bps; ++b) {
            double l0 = 0, l1 = 0;
            bool f0 = true, f1 = true;
            for (int k = 0; k < npts; ++k) {
                if ((k >> (bps - 1 - b)) & 1) { l1 = f1 ? m[k] : logaddexp(l1, m[k]); f1 = false; }
                else { l0 = f0 ? m[k] : logaddexp(l0, m[k]); f0 = false; }
            }
            double v = (float)(l0 - l1);
            llr[i * bps + b] = std::min(S_LLR_CLIP, std::max(-S_LLR_CLIP, v));
        }
    }
    // de-whiten, deinterleave, LDPC
    const std::vector<uint8_t>& prbs = prbs_table();
    const std::vector<int32_t>& perm = interleave_perm((int)n_coded);
    std::vector<double> de(n_coded);
    for (long long i = 0; i < n_coded; ++i) de[perm[i]] = llr[i] * (1.0 - 2.0 * prbs[i % prbs.size()]);
    long long n_bits = (long long)(n_bytes + 4) * 8;
    std::vector<uint8_t> bits(n_bits), cw(ldpc_coded_len(n_bits));
    int ok = ldpc_decode(de.data(), n_coded, n_bits, LDPC_MAX_ITER, LDPC_ALPHA, LDPC_STALL_ITER, bits.data(),
                         cw.data(), nullptr);
    if (ok != 1) ldpc_encode(bits.data(), n_bits, cw.data());
    std::vector<uint8_t> data(n_bytes + 4, 0);
    for (long long i = 0; i < n_bits; ++i) data[i / 8] |= bits[i] << (7 - i % 8);
    R.payload.assign(data.begin(), data.begin() + n_bytes);
    uint32_t crc = crc32(data.data(), n_bytes);
    R.crc_ok = data[n_bytes] == (uint8_t)(crc >> 24) && data[n_bytes + 1] == (uint8_t)(crc >> 16)
               && data[n_bytes + 2] == (uint8_t)(crc >> 8) && data[n_bytes + 3] == (uint8_t)crc;
    long long diff = 0;
    for (long long i = 0; i < n_coded; ++i) diff += (de[i] < 0) != (cw[i] != 0);
    R.ber = (double)diff / n_coded;
    R.evm = 100 * std::pow(10.0, -mer / 20);
}

struct SRx {
    SC* sc;
    std::vector<cd> zz;
    double t0 = 0, cfo = 0;
    cd ref;
    SDecoded dec;
};

}  // namespace

TX_API void* srx_new(void* sc_tx_handle) {
    SRx* r = new SRx();
    r->sc = (SC*)sc_tx_handle;
    return r;
}
TX_API void srx_free(void* h) { delete (SRx*)h; }

TX_API void srx_mf(void* h, const cd* x, long long n, cd* out) {
    std::vector<cd> y = s_mf(*((SRx*)h)->sc, x, n);
    std::memcpy(out, y.data(), n * sizeof(cd));
}

TX_API void srx_derotated_mf(void* h, const cd* x, long long n, double cfo, cd* out) {
    std::vector<cd> y = s_derotated_mf(*((SRx*)h)->sc, x, n, cfo);
    std::memcpy(out, y.data(), n * sizeof(cd));
}

TX_API int srx_detect(void* h, const cd* z, long long n, long long* i, double* metric, double* cfo) {
    return s_detect(*((SRx*)h)->sc, z, n, *i, *metric, *cfo) ? 1 : 0;
}

// sync on raw x (preamble near sample i): t0, cfo, ref; zz kept for the header/decode
TX_API void srx_sync(void* hh, const cd* x, long long n, long long i, double cfo1, double* t0, double* cfo,
                     double* ref_re, double* ref_im) {
    SRx* h = (SRx*)hh;
    s_sync(*h->sc, x, n, i, cfo1, h->t0, h->cfo, h->zz);
    h->ref = s_ref(*h->sc, h->zz, h->t0);
    *t0 = h->t0; *cfo = h->cfo; *ref_re = h->ref.real(); *ref_im = h->ref.imag();
}

// header from the kept zz: length, or -1 (checksum)
TX_API int srx_header(void* hh) {
    SRx* h = (SRx*)hh;
    return s_header(*h->sc, h->zz, h->t0, h->ref);
}

// decode from raw x with the sync's t0/cfo; returns the number of data symbols
TX_API long long srx_decode(void* hh, const cd* x, long long n, double t0, double cfo, int n_bytes, int* crc_ok,
                            double* ber, double* evm) {
    SRx* h = (SRx*)hh;
    std::vector<cd> zz = s_derotated_mf(*h->sc, x, n, cfo);
    s_decode(*h->sc, zz, t0, n_bytes, h->dec);
    *crc_ok = h->dec.crc_ok;
    *ber = h->dec.ber;
    *evm = h->dec.evm;
    return (long long)h->dec.d.size();
}

TX_API void srx_decode_copy(void* hh, uint8_t* payload, cd* points) {
    SRx* h = (SRx*)hh;
    std::memcpy(payload, h->dec.payload.data(), h->dec.payload.size());
    std::memcpy(points, h->dec.d.data(), h->dec.d.size() * sizeof(cd));
}

// ======================================================================
// Mode SH receiver kernels (hf_sch.py): detection (periodic-preamble
// metric, run end, UW check over the frequency steps and the 8 variants,
// fine frequency), the symbol grid, the header (first channel from the
// preamble's UWs, MMSE, the header block's own estimate) and the decode
// (per-block least-squares channels tracked, smoothed and denoised, MMSE,
// exact LLRs, LDPC, turbo re-estimation from the decoder's output).
namespace {

const int SH_SHORT_LEN = SH_PERIOD * SH_SHORT_REPEATS;
const double SH_DETECT_THRESHOLD = 0.35, SH_UW_MATCH = 0.25, SH_RUN_END_FRACTION = 0.75, SH_TAP_THRESHOLD = 9.0,
             SH_TRACK = 0.7, SH_MAX_CFO_HZ = 4000.0;
const int SH_TURBO = 2;
const double SH_SMOOTH_W[5] = {1, 2, 3, 2, 1};

struct SHGeo {
    int k, N, UW, D, L, PRE, REP, PRE_HF;
};
SHGeo sh_geo(const SC& s) {
    SHGeo g;
    g.k = s.k; g.N = s.N; g.UW = s.UW_LEN; g.D = s.D_LEN; g.L = s.UW_LEN; g.PRE = 24 * s.k; g.REP = s.HDR_REPEAT;
    g.PRE_HF = SH_SHORT_LEN + SH_PRE_UWS * s.UW_LEN;
    return g;
}

std::vector<cd> to_cd(const std::vector<cf>& v) {
    std::vector<cd> o(v.size());
    for (size_t i = 0; i < v.size(); ++i) o[i] = cd(v[i].real(), v[i].imag());
    return o;
}

std::vector<cd> fft_fwd(const std::vector<cd>& x) {
    std::vector<cd> o(x.size());
    fft::plan<double>((int)x.size()).forward(x.data(), o.data());
    return o;
}
std::vector<cd> fft_inv(const std::vector<cd>& X) {      // numpy ifft (scaled)
    std::vector<cd> o(X.size());
    fft::plan<double>((int)X.size()).inverse(X.data(), o.data());
    double s = 1.0 / X.size();
    for (cd& v : o) v *= s;
    return o;
}

// |sum_j conj(seq[j]) z[n + SPS j]| for n = 0 .. n_out-1
std::vector<double> strided_xcorr_abs(const std::vector<cd>& z, long long z0, const std::vector<cd>& seq,
                                      long long n_out) {
    std::vector<double> c(n_out);
    std::vector<cd> cs(seq.size());
    for (size_t j = 0; j < seq.size(); ++j) cs[j] = std::conj(seq[j]);
    for (long long n = 0; n < n_out; ++n) {
        double re = 0, im = 0;
        const cd* p = z.data() + z0 + n;
        for (size_t j = 0; j < cs.size(); ++j) {
            cd a = p[j * S_SPS], b = cs[j];
            re += a.real() * b.real() - a.imag() * b.imag();
            im += a.real() * b.imag() + a.imag() * b.real();
        }
        c[n] = std::sqrt(re * re + im * im);
    }
    return c;
}

std::vector<cd> tile3(const std::vector<cd>& u) {
    std::vector<cd> s;
    for (int r = 0; r < SH_PRE_UWS; ++r) s.insert(s.end(), u.begin(), u.end());
    return s;
}

// _xc: start (fractional) of seq in z within lo..hi, earliest peak >= 0.6 max
double sh_xc(const std::vector<cd>& z, const std::vector<cd>& seq, long long lo, long long hi) {
    std::vector<double> c = strided_xcorr_abs(z, lo, seq, hi - lo + 1);
    double mx = *std::max_element(c.begin(), c.end());
    long long k0 = 0;
    while (c[k0] < 0.6 * mx) ++k0;
    long long e = std::min((long long)c.size(), k0 + 2 * S_SPS + 1), k = k0;
    for (long long i = k0 + 1; i < e; ++i) if (c[i] > c[k]) k = i;
    double den = (k > 0 && k < (long long)c.size() - 1) ? c[k - 1] - 2 * c[k] + c[k + 1] : 0.0;
    return lo + k + (den != 0 ? 0.5 * (c[k - 1] - c[k + 1]) / den : 0.0);
}

// _check: 0 none, 1 found (t0, cfo, variant), 2 short
int sh_check(const SC& s, const SHGeo& g, const cd* x, long long nx, cd Pi, long long i, double rate, double& t0,
             double& cfo, int& variant) {
    const int S = S_SPS;
    long long ex = i + SH_SHORT_LEN * S;
    long long lo = std::max(0LL, ex - 4LL * g.PRE * S), hi = ex + 2LL * g.PRE * S;
    if (hi + (long long)(SH_PRE_UWS * g.UW + 64) * S >= nx) return 2;
    double c16 = -std::atan2(Pi.imag(), Pi.real()) / (2 * fft::PI * SH_PERIOD);
    long long s0 = std::max(0LL, lo - 64), s1 = std::min(nx, hi + (long long)SH_PRE_UWS * g.UW * S + 64);
    long long off = lo - s0;
    int reach = SH_PERIOD / 2;
    if (rate > 0) reach = std::min(reach, (int)std::ceil(SH_MAX_CFO_HZ * SH_PERIOD / rate));
    double best = -1, bcand = 0;
    int bv = 0;
    std::vector<std::vector<cd>> seqs;
    for (int v = 0; v < SH_VARIANTS; ++v) seqs.push_back(tile3(to_cd(s.uws[v])));
    long long tlen = (long long)(seqs[0].size() - 1) * S + 1;
    // correlations by FFT: the strided templates' spectra once, then per
    // frequency step one forward FFT of the segment and one inverse per variant
    long long seg_len = (hi - lo) + tlen;
    int nf = 1;
    while (nf < seg_len + tlen) nf <<= 1;
    fft::Plan<double>& pl = fft::plan<double>(nf);
    std::vector<std::vector<cd>> TF(SH_VARIANTS);
    std::vector<cd> buf(nf), Z(nf), Rr(nf);
    for (int v = 0; v < SH_VARIANTS; ++v) {
        std::fill(buf.begin(), buf.end(), cd(0, 0));
        for (size_t j = 0; j < seqs[v].size(); ++j) buf[j * S] = seqs[v][j];
        TF[v].resize(nf);
        pl.forward(buf.data(), TF[v].data());
    }
    for (int step = -reach + (reach == SH_PERIOD / 2 ? 1 : 0); step <= reach; ++step) {
        double cand = c16 + (double)step / SH_PERIOD;
        std::vector<cd> zc = s_derotated_mf(s, x + s0, s1 - s0, cand);
        long long avail = (long long)zc.size() - off;
        long long len = std::min(seg_len, avail), n_out = len - tlen + 1;
        if (n_out <= 0) continue;
        std::fill(buf.begin(), buf.end(), cd(0, 0));
        std::copy(zc.begin() + off, zc.begin() + off + len, buf.begin());
        pl.forward(buf.data(), Z.data());
        for (int v = 0; v < SH_VARIANTS; ++v) {
            for (int i = 0; i < nf; ++i) Rr[i] = Z[i] * std::conj(TF[v][i]);
            pl.inverse(Rr.data(), buf.data());
            double mx = 0;
            for (long long i = 0; i < n_out; ++i) mx = std::max(mx, std::abs(buf[i]) / nf);
            if (mx > best) { best = mx; bcand = cand; bv = v; }
        }
    }
    cfo = bcand;
    variant = bv;
    std::vector<cd> uw = to_cd(s.uws[bv]);
    long long zn = std::min(nx, hi + (long long)(SH_PRE_UWS * g.UW + 64) * S);
    std::vector<cd> zz = s_derotated_mf(s, x, zn, cfo);
    t0 = sh_xc(zz, uw, lo, hi) - SH_SHORT_LEN * S;
    cd acc(0, 0);
    std::vector<std::vector<cd>> u(SH_PRE_UWS, std::vector<cd>(g.UW));
    for (int j = 0; j < SH_PRE_UWS; ++j)
        for (int q = 0; q < g.UW; ++q) u[j][q] = s_cubic(zz, t0 + (double)(SH_SHORT_LEN + j * g.UW + q) * S);
    for (int j = 0; j + 1 < SH_PRE_UWS; ++j)
        for (int q = 0; q < g.UW; ++q) acc += std::conj(u[j][q]) * u[j + 1][q];
    double fine = std::atan2(acc.imag(), acc.real()) / (2 * fft::PI * g.UW);
    std::vector<cd> seq = tile3(uw), rx(seq.size());
    for (size_t q = 0; q < seq.size(); ++q) rx[q] = s_cubic(zz, t0 + (double)(SH_SHORT_LEN + q) * S);
    auto score = [&](int j) {
        cd a(0, 0);
        for (size_t q = 0; q < seq.size(); ++q)
            a += std::conj(seq[q]) * rx[q] * std::exp(cd(0, -2 * fft::PI * (fine + (double)j / g.UW) * (double)q));
        return std::abs(a);
    };
    int bj = -2;
    double bs = score(-2);
    for (int j = -1; j <= 2; ++j) { double sc_ = score(j); if (sc_ > bs) { bs = sc_; bj = j; } }
    double ns = 0, nr = 0;
    for (size_t q = 0; q < seq.size(); ++q) { ns += std::norm(seq[q]); nr += std::norm(rx[q]); }
    if (bs < SH_UW_MATCH * std::sqrt(ns) * std::sqrt(nr)) return 0;
    cfo += fine + (double)bj / g.UW;
    return 1;
}

int sh_detect(const SC& s, const cd* x, long long nx, double rate, double& t0, double& cfo, int& variant) {
    const int S = S_SPS, lag = SH_PERIOD * S, win = (SH_SHORT_REPEATS - 1) * lag;
    std::vector<cd> z = s_mf(s, x, nx);
    long long nz = (long long)z.size();
    if (nz < win + lag + 10) return 0;
    // running sums in single precision, as numpy's complex64 / float32 cumsum
    long long npd = nz - lag;
    std::vector<cd> cs(npd + 1);
    std::vector<double> ce(nz + 1);
    std::complex<float> accp(0, 0);
    float acce = 0;
    cs[0] = cd(0, 0);
    for (long long k = 0; k < npd; ++k) {
        std::complex<float> a((float)z[k].real(), (float)z[k].imag()), b((float)z[k + lag].real(), (float)z[k + lag].imag());
        std::complex<float> pr(a.real() * b.real() + a.imag() * b.imag(), a.imag() * b.real() - a.real() * b.imag());
        accp += pr;
        cs[k + 1] = cd(accp.real(), accp.imag());
    }
    ce[0] = 0;
    for (long long k = 0; k < nz; ++k) {
        float ab = std::hypot((float)z[k].real(), (float)z[k].imag());
        acce += ab * ab;
        ce[k + 1] = acce;
    }
    long long n = npd - win;
    if (n <= 0) return 0;
    std::vector<cd> P(n);
    std::vector<double> m(n);
    for (long long i = 0; i < n; ++i) {
        P[i] = cs[win + i] - cs[i];
        double E1 = ce[win + i] - ce[i], E2 = ce[lag + win + i] - ce[lag + i];
        m[i] = std::abs(P[i]) / std::sqrt(std::max(E1 * E2, 1e-30));
    }
    long long pos = 0;
    while (true) {
        long long i0 = -1;
        for (long long q = pos; q < n; ++q) if (m[q] > SH_DETECT_THRESHOLD) { i0 = q; break; }
        if (i0 < 0) return 0;
        double peak = 0;
        for (long long q = i0; q < std::min(n, i0 + 2LL * SH_SHORT_LEN * S); ++q) peak = std::max(peak, m[q]);
        double thr = std::max(SH_DETECT_THRESHOLD, SH_RUN_END_FRACTION * peak);
        long long j0 = -1;
        for (long long q = i0; q < n; ++q) if (m[q] > thr) { j0 = q; break; }
        if (j0 < 0) j0 = i0;      // (argmax of an all-False array is 0)
        long long run_end = -1;
        for (long long q = j0; q < n; ++q) if (!(m[q] > thr)) { run_end = q; break; }
        if (run_end < 0) return 2;
        long long i = std::max(i0, run_end - lag);
        int got = sh_check(s, sh_geo(s), x, nx, P[i], i, rate, t0, cfo, variant);
        if (got) return got;
        pos = run_end + lag;
        if (pos >= n) return 0;
    }
}

// _grid: symbol-rate samples at t0 + k SPS, k = 0 .. n_sym + L - 1
std::vector<cd> sh_grid(const SC& s, const SHGeo& g, const cd* x, long long nx, double t0, double cfo, long long n_sym) {
    std::vector<cd> zz = s_derotated_mf(s, x, nx, cfo);
    std::vector<cd> r(n_sym + g.L);
    for (long long k = 0; k < n_sym + g.L; ++k) r[k] = round_c64(s_cubic(zz, t0 + (double)k * S_SPS));
    return r;
}

std::vector<cd> taps_to_H(const SHGeo& g, const std::vector<cd>& h, int pre) {
    std::vector<cd> v(g.N, cd(0, 0));
    for (size_t i = pre; i < h.size(); ++i) v[i - pre] = h[i];
    for (int i = 0; i < pre; ++i) v[g.N - pre + i] = h[i];
    return fft_fwd(v);
}

std::vector<cd> denoise(const std::vector<cd>& H, double var) {
    std::vector<cd> gt = fft_inv(H);
    size_t best = 0;
    std::vector<double> p(gt.size());
    for (size_t i = 0; i < gt.size(); ++i) { p[i] = std::norm(gt[i]); if (p[i] > p[best]) best = i; }
    for (size_t i = 0; i < gt.size(); ++i) if (!(p[i] > SH_TAP_THRESHOLD * var) && i != best) gt[i] = cd(0, 0);
    return fft_fwd(gt);
}

// Levinson: T x = y, T[i][j] = t(i - j) (any sign), O(n^2)
std::vector<cd> levinson(const std::function<cd(int)>& t, const std::vector<cd>& y) {
    int n = (int)y.size();
    std::vector<cd> f(1, 1.0 / t(0)), b(1, 1.0 / t(0)), x(1, y[0] / t(0));
    for (int m = 1; m < n; ++m) {
        cd ef(0, 0), eb(0, 0), ex(0, 0);
        for (int i = 0; i < m; ++i) {
            ef += t(m - i) * f[i];
            eb += t(-(i + 1)) * b[i];
            ex += t(m - i) * x[i];
        }
        cd den = 1.0 - ef * eb;
        std::vector<cd> fn(m + 1), bn(m + 1);
        for (int i = 0; i <= m; ++i) {
            cd fi = i < m ? f[i] : cd(0, 0), bi = i > 0 ? b[i - 1] : cd(0, 0);
            fn[i] = (fi - ef * bi) / den;
            bn[i] = (bi - eb * fi) / den;
        }
        f.swap(fn);
        b.swap(bn);
        x.push_back(cd(0, 0));
        for (int i = 0; i <= m; ++i) x[i] += (y[m] - ex) * b[i];
    }
    return x;
}

// _block_channel: least-squares L taps from a block's cyclic window
std::vector<cd> block_channel(const SHGeo& g, const std::vector<cd>& yw, const std::vector<cd>& block) {
    int N = g.N;
    std::vector<cd> c(N);
    for (int i = 0; i < N; ++i) c[i] = block[pymod(i - g.PRE, N)];
    std::vector<cd> Sf = fft_fwd(c), Y = fft_fwd(yw), A(N), B(N);
    for (int i = 0; i < N; ++i) { A[i] = std::conj(Sf[i]) * Sf[i]; B[i] = std::conj(Sf[i]) * Y[i]; }
    std::vector<cd> a = fft_inv(A), bc = fft_inv(B), rhs(g.L);
    for (int i = 0; i < g.L; ++i) rhs[i] = bc[pymod(i - g.PRE, N)];
    std::vector<cd> hb = levinson([&](int k) { return a[pymod(k, N)]; }, rhs);
    return taps_to_H(g, hb, g.PRE);
}

std::vector<cd> mmse(const SC& s, const SHGeo& g, const std::vector<cd>& Y, const std::vector<cd>& H, double nv,
                     const std::vector<cd>& uw) {
    int N = g.N;
    std::vector<cd> V(N);
    double mu = 0;
    for (int i = 0; i < N; ++i) {
        double h2 = std::norm(H[i]);
        V[i] = Y[i] * (std::conj(H[i]) / (h2 + nv));
        mu += h2 / (h2 + nv);
    }
    mu = std::max(mu / N, 1e-6);
    std::vector<cd> v = fft_inv(V), xh(N);
    for (int i = 0; i < N; ++i) xh[i] = v[(i + g.PRE) % N] / mu;
    cd a(0, 0);
    for (int i = 0; i < g.UW; ++i) a += std::conj(uw[i]) * xh[g.D + i];
    cd rot = std::exp(cd(0, -std::atan2(a.imag(), a.real())));
    for (cd& q : xh) q *= rot;
    return xh;
}

std::vector<cd> sh_window(const SHGeo& g, const std::vector<cd>& r, int b) {
    long long st = g.PRE_HF + (long long)b * g.N - g.PRE;
    return std::vector<cd>(r.begin() + st, r.begin() + st + g.N);
}

struct SHHeader { int n_bytes = -1; std::vector<cd> H; double nv = 0, pre_snr_db = 0; };

void sh_first_channel(const SHGeo& g, const std::vector<cd>& r, const std::vector<cd>& uw, std::vector<cd>& H,
                      double& nv, double& snr_db) {
    int U = g.UW;
    long long a0 = SH_SHORT_LEN + 1LL * U - g.PRE, a1 = SH_SHORT_LEN + 2LL * U - g.PRE;
    std::vector<cd> avg(U), ru(U);
    double p = 0, dn = 0, sg = 0;
    for (int i = 0; i < U; ++i) {
        cd y0 = r[a0 + i], y1 = r[a1 + i];
        avg[i] = (y0 + y1) / 2.0;
        ru[i] = uw[pymod(i - g.PRE, U)];
        p += std::norm(y0) + std::norm(y1);
        dn += std::norm(y0 - y1);
        sg += std::norm(avg[i]);
    }
    p = p / U / 2 + 1e-30;
    std::vector<cd> A = fft_fwd(avg), R = fft_fwd(ru);
    for (int i = 0; i < U; ++i) A[i] /= R[i];
    std::vector<cd> gt = fft_inv(A), h(U);
    for (int i = 0; i < U; ++i) h[i] = gt[pymod(i - g.PRE, U)];
    nv = std::max(dn / U / 2, 1e-4 * p);
    double sig = std::max(sg / U - nv / 2, 1e-5 * p);
    snr_db = 10 * std::log10(sig / nv);
    std::vector<cd> hl(h.begin(), h.begin() + g.L);
    H = denoise(taps_to_H(g, hl, g.PRE), nv / (2.0 * U));
}

std::vector<cd> sh_header_symbols(const SC& s, const SHGeo& g, int n) {
    uint8_t hb[SC_HDR_BITS];
    sc_header_bits(n, hb);
    std::vector<cd> o(SC_HDR_BITS * g.REP);
    for (int i = 0; i < SC_HDR_BITS * g.REP; ++i) o[i] = cd(1.0 - 2.0 * (hb[i / g.REP] ^ s.hdr_scramble[i]), 0);
    return o;
}

SHHeader sh_header(const SC& s, const SHGeo& g, const std::vector<cd>& r, const std::vector<cd>& uw) {
    SHHeader R;
    std::vector<cd> H;
    double nv, snr;
    sh_first_channel(g, r, uw, H, nv, snr);
    R.H = H; R.nv = nv; R.pre_snr_db = snr;
    std::vector<cd> yw = sh_window(g, r, 0);
    std::vector<cd> xh = mmse(s, g, fft_fwd(yw), H, nv, uw);
    uint8_t bits[SC_HDR_BITS];
    for (int b = 0; b < SC_HDR_BITS; ++b) {
        double soft = 0;
        for (int q = 0; q < g.REP; ++q) soft += xh[b * g.REP + q].real() * (1 - 2.0 * s.hdr_scramble[b * g.REP + q]);
        bits[b] = soft < 0;
    }
    uint8_t c = 0;
    for (int i = 0; i < 8; ++i) c = (uint8_t)((c << 1) | bits[16 + i]);
    if (crc8_bits(bits, 16) != c) return R;
    int n = 0;
    for (int i = 0; i < 16; ++i) n = (n << 1) | bits[i];
    R.n_bytes = n;
    std::vector<cd> blk = sh_header_symbols(s, g, n);
    blk.insert(blk.end(), uw.begin(), uw.end());
    std::vector<cd> Hh = block_channel(g, yw, blk);
    for (int i = 0; i < g.N; ++i) R.H[i] = (1 - SH_TRACK) * H[i] + SH_TRACK * Hh[i];
    return R;
}

struct SHDecoded {
    std::vector<uint8_t> payload;
    bool crc_ok = false;
    double ber = 0, cfo = 0, pre_snr_db = 0;
    std::vector<cd> d;
};

void sh_llrs(const SC& s, const SHGeo& g, const std::vector<cd>& xh, double nve, const std::vector<cd>& cst,
             std::vector<double>& out) {
    int bps = s.bps, np_ = (int)cst.size();
    std::vector<double> m(np_);
    for (int q = 0; q < g.D; ++q) {
        for (int k = 0; k < np_; ++k) m[k] = -std::norm(xh[q] - cst[k]) / nve;
        for (int b = 0; b < bps; ++b) {
            double l0 = 0, l1 = 0;
            bool f0 = true, f1 = true;
            for (int k = 0; k < np_; ++k) {
                if ((k >> (bps - 1 - b)) & 1) { l1 = f1 ? m[k] : logaddexp(l1, m[k]); f1 = false; }
                else { l0 = f0 ? m[k] : logaddexp(l0, m[k]); f0 = false; }
            }
            out.push_back((float)(l0 - l1));
        }
    }
}

void sh_decode(const SC& s, const SHGeo& g, const std::vector<cd>& r, int n_bytes, const SHHeader& hd,
               const std::vector<cd>& uw, SHDecoded& R) {
    int bps = s.bps;
    std::vector<cd> cst = to_cd(s.cst);
    long long n_coded = ldpc_coded_len((long long)(n_bytes + 4) * 8);
    long long per = (long long)bps * g.D, n_blocks = (n_coded + per - 1) / per;
    std::vector<std::vector<cd>> win, Yw;
    for (long long b = 0; b < n_blocks; ++b) { win.push_back(sh_window(g, r, (int)(1 + b))); Yw.push_back(fft_fwd(win.back())); }
    double nv = hd.nv;
    std::vector<std::vector<cd>> known;
    std::vector<double> de;
    std::vector<uint8_t> cw;
    for (int turbo = 0; turbo <= SH_TURBO; ++turbo) {
        std::vector<std::vector<cd>> Hs;
        if (known.empty()) {
            std::vector<cd> Hc = hd.H;
            for (long long b = 0; b < n_blocks; ++b) {
                std::vector<cd> xh = mmse(s, g, Yw[b], Hc, nv, uw), dec(g.N);
                for (int q = 0; q < g.D; ++q) dec[q] = s_slice(s.cst, round_c64(xh[q]));
                for (int q = 0; q < g.UW; ++q) dec[g.D + q] = uw[q];
                std::vector<cd> Hb = block_channel(g, win[b], dec), Hd = denoise(Hb, nv / g.N);
                for (int i = 0; i < g.N; ++i) Hc[i] = (1 - SH_TRACK) * Hc[i] + SH_TRACK * Hd[i];
                Hs.push_back(Hb);
            }
        } else {
            for (long long b = 0; b < n_blocks; ++b) Hs.push_back(block_channel(g, win[b], known[b]));
        }
        // _smooth_blocks, then denoise
        std::vector<std::vector<cd>> Sm;
        double w2 = 0, w1 = 0;
        for (double w : SH_SMOOTH_W) { w2 += w * w; w1 += w; }
        for (long long b = 0; b < n_blocks; ++b) {
            std::vector<cd> acc(g.N, cd(0, 0));
            double ws = 0;
            for (int j = 0; j < 5; ++j) {
                long long bb = b + j - 2;
                if (bb < 0 || bb >= n_blocks) continue;
                for (int i = 0; i < g.N; ++i) acc[i] += SH_SMOOTH_W[j] * Hs[bb][i];
                ws += SH_SMOOTH_W[j];
            }
            for (cd& v : acc) v /= ws;
            Sm.push_back(denoise(acc, nv / g.N * w2 / (w1 * w1)));
        }
        std::vector<double> llr;
        R.d.clear();
        for (long long b = 0; b < n_blocks; ++b) {
            std::vector<cd> xh = mmse(s, g, Yw[b], Sm[b], nv, uw);
            double e = 0;
            for (int q = 0; q < g.UW; ++q) e += std::norm(xh[g.D + q] - uw[q]);
            double nve = std::max(e / g.UW, 1e-4);
            sh_llrs(s, g, xh, nve, cst, llr);
            for (int q = 0; q < g.D; ++q) R.d.push_back(round_c64(xh[q]));
        }
        const std::vector<uint8_t>& prbs = prbs_table();
        const std::vector<int32_t>& perm = interleave_perm((int)n_coded);
        de.assign(n_coded, 0);
        for (long long i = 0; i < n_coded; ++i) {
            double v = std::min(40.0, std::max(-40.0, llr[i]));
            de[perm[i]] = v * (1.0 - 2.0 * prbs[i % prbs.size()]);
        }
        long long n_bits = (long long)(n_bytes + 4) * 8;
        std::vector<uint8_t> bits(n_bits);
        cw.assign(ldpc_coded_len(n_bits), 0);
        int ok = ldpc_decode(de.data(), n_coded, n_bits, LDPC_MAX_ITER, LDPC_ALPHA, LDPC_STALL_ITER, bits.data(),
                             cw.data(), nullptr);
        if (ok != 1) ldpc_encode(bits.data(), n_bits, cw.data());
        std::vector<uint8_t> data(n_bytes + 4, 0);
        for (long long i = 0; i < n_bits; ++i) data[i / 8] |= bits[i] << (7 - i % 8);
        R.payload.assign(data.begin(), data.begin() + n_bytes);
        uint32_t crc = crc32(data.data(), n_bytes);
        R.crc_ok = data[n_bytes] == (uint8_t)(crc >> 24) && data[n_bytes + 1] == (uint8_t)(crc >> 16)
                   && data[n_bytes + 2] == (uint8_t)(crc >> 8) && data[n_bytes + 3] == (uint8_t)crc;
        if (R.crc_ok || turbo == SH_TURBO) break;
        // the decoder's output as the next pass's known data
        const std::vector<int32_t>& p2 = interleave_perm((int)cw.size());
        std::vector<uint8_t> coded(cw.size());
        for (size_t i = 0; i < cw.size(); ++i) coded[i] = cw[p2[i]] ^ prbs[i % prbs.size()];
        std::vector<cf> dsf = const_cast<SC&>(s).data_symbols(coded, (size_t)n_blocks * g.D);
        known.clear();
        for (long long b = 0; b < n_blocks; ++b) {
            std::vector<cd> blk(g.N);
            for (int q = 0; q < g.D; ++q) blk[q] = cd(dsf[b * g.D + q].real(), dsf[b * g.D + q].imag());
            for (int q = 0; q < g.UW; ++q) blk[g.D + q] = uw[q];
            known.push_back(blk);
        }
    }
    long long diff = 0;
    for (long long i = 0; i < n_coded; ++i) diff += (de[i] < 0) != (cw[i] != 0);
    R.ber = (double)diff / n_coded;
}

struct SHRx {
    SC* sc;
    SHDecoded dec;
};

}  // namespace

TX_API void* shrx_new(void* sc_tx_handle) {
    SHRx* r = new SHRx();
    r->sc = (SC*)sc_tx_handle;
    return r;
}
TX_API void shrx_free(void* h) { delete (SHRx*)h; }

// 0 none, 1 found, 2 short (may run past the end of x)
TX_API int shrx_detect(void* hh, const cd* x, long long n, double symbol_rate, double* t0, double* cfo, int* variant) {
    SHRx* h = (SHRx*)hh;
    return sh_detect(*h->sc, x, n, symbol_rate, *t0, *cfo, *variant);
}

// the header on x from t0 (cfo, variant from detect): length or -1
TX_API int shrx_header(void* hh, const cd* x, long long n, double t0, double cfo, int variant) {
    SHRx* h = (SHRx*)hh;
    SHGeo g = sh_geo(*h->sc);
    std::vector<cd> r = sh_grid(*h->sc, g, x, n, t0, cfo, g.PRE_HF + g.N);
    return sh_header(*h->sc, g, r, to_cd(h->sc->uws[variant])).n_bytes;
}

// the whole frame: fine frequency from the UWs, header, decode. Returns the
// number of data symbols; cfo_out the refined frequency, pre_snr_db the readout
TX_API long long shrx_decode(void* hh, const cd* x, long long n, double t0, double cfo, int variant, int n_bytes,
                             int* crc_ok, double* ber, double* cfo_out, double* pre_snr_db) {
    SHRx* h = (SHRx*)hh;
    const SC& s = *h->sc;
    SHGeo g = sh_geo(s);
    std::vector<cd> uw = to_cd(s.uws[variant]);
    long long per = (long long)s.bps * g.D;
    long long n_blocks = (ldpc_coded_len((long long)(n_bytes + 4) * 8) + per - 1) / per;
    long long n_sym = g.PRE_HF + (long long)g.N * (1 + n_blocks);
    std::vector<cd> r = sh_grid(s, g, x, n, t0, cfo, n_sym);
    cd acc(0, 0);
    for (long long b = 0; b + 1 < n_blocks + 2; ++b) {
        long long a0 = g.PRE_HF - g.UW + b * g.N, a1 = a0 + g.N;
        for (int q = 0; q < g.UW; ++q) acc += std::conj(r[a0 + q]) * r[a1 + q];
    }
    cfo += std::atan2(acc.imag(), acc.real()) / (2 * fft::PI * g.N);
    r = sh_grid(s, g, x, n, t0, cfo, n_sym);
    SHHeader hd = sh_header(s, g, r, uw);
    sh_decode(s, g, r, n_bytes, hd, uw, h->dec);
    *crc_ok = h->dec.crc_ok;
    *ber = h->dec.ber;
    *cfo_out = cfo;
    *pre_snr_db = hd.pre_snr_db;
    return (long long)h->dec.d.size();
}

TX_API void shrx_decode_copy(void* hh, uint8_t* payload, cd* points) {
    SHRx* h = (SHRx*)hh;
    std::memcpy(payload, h->dec.payload.data(), h->dec.payload.size());
    std::memcpy(points, h->dec.d.data(), h->dec.d.size() * sizeof(cd));
}

// ---- entry points matching the Python helpers' own signatures, so hf_sc /
// hf_sch's receive_one_fragment can run unchanged on these (avm_cpp.py)

// S: the matched-filtered samples the last srx_sync settled on (len n)
TX_API void srx_zz_copy(void* hh, cd* out) {
    SRx* h = (SRx*)hh;
    std::memcpy(out, h->zz.data(), h->zz.size() * sizeof(cd));
}

// S: hf_sc.decode(zz, t0, ref, n_bytes) on already derotated + filtered zz
TX_API long long srx_decode_zz(void* hh, const cd* zz, long long n, double t0, int n_bytes, int* crc_ok, double* ber,
                               double* evm) {
    SRx* h = (SRx*)hh;
    std::vector<cd> z(zz, zz + n);
    s_decode(*h->sc, z, t0, n_bytes, h->dec);
    *crc_ok = h->dec.crc_ok;
    *ber = h->dec.ber;
    *evm = h->dec.evm;
    return (long long)h->dec.d.size();
}

// SH: hf_sch._grid (n_sym + L_TAPS values)
TX_API void shrx_grid(void* hh, const cd* x, long long n, double t0, double cfo, long long n_sym, cd* out) {
    SHRx* h = (SHRx*)hh;
    std::vector<cd> r = sh_grid(*h->sc, sh_geo(*h->sc), x, n, t0, cfo, n_sym);
    std::memcpy(out, r.data(), r.size() * sizeof(cd));
}

// SH: hf_sch._header(r) -> length or -1; *pre_snr_db its preamble SNR
TX_API int shrx_header_r(void* hh, const cd* r, long long n, int variant, double* pre_snr_db) {
    SHRx* h = (SHRx*)hh;
    std::vector<cd> rv(r, r + n);
    SHHeader hd = sh_header(*h->sc, sh_geo(*h->sc), rv, to_cd(h->sc->uws[variant]));
    *pre_snr_db = hd.pre_snr_db;
    return hd.n_bytes;
}

// SH: hf_sch._header + _decode on grid r: returns the number of data symbols
TX_API long long shrx_decode_r(void* hh, const cd* r, long long n, int variant, int n_bytes, int* crc_ok, double* ber,
                               double* pre_snr_db) {
    SHRx* h = (SHRx*)hh;
    const SC& s = *h->sc;
    SHGeo g = sh_geo(s);
    std::vector<cd> rv(r, r + n), uw = to_cd(s.uws[variant]);
    SHHeader hd = sh_header(s, g, rv, uw);
    sh_decode(s, g, rv, n_bytes, hd, uw, h->dec);
    *crc_ok = h->dec.crc_ok;
    *ber = h->dec.ber;
    *pre_snr_db = hd.pre_snr_db;
    return (long long)h->dec.d.size();
}

// ======================================================================
// rx_frontend._process: one chunk of native-rate samples -> modem-rate
// samples (IQ correction and LO shift per sample in double, then the
// causal FIR evaluated only at the two native samples each output
// interpolates between, single precision, as the numba kernel). hist: the
// previous nt processed samples (in), replaced by the last nt (out).
// iq: [apply, mean_I, mean_Q, slope, scale]. Returns the outputs written.
TX_API long long fe_process(const cf* x, long long n, cf* hist, const float* taps_rev, int nt, long long n_in0,
                            long long n_out0, double step, const double* iq, int lo_on, const cd* lo_table,
                            long long period, long long lo_phase0, cf* out) {
    const int nh = nt;
    std::vector<float> wr(nh + n), wi(nh + n);
    for (int i = 0; i < nh; ++i) { wr[i] = hist[i].real(); wi[i] = hist[i].imag(); }
    long long ph = lo_phase0;
    const bool apply_iq = iq[0] != 0.0;
    for (long long i = 0; i < n; ++i) {
        double I = x[i].real(), Q = x[i].imag();
        if (apply_iq) {
            double I0 = I - iq[1];
            double Q1 = ((Q - iq[2]) - iq[3] * I0) * iq[4];
            I = I0 + iq[1];
            Q = Q1 + iq[2];
        }
        if (lo_on) {
            cd t = lo_table[ph];
            double re = I * t.real() - Q * t.imag(), im = I * t.imag() + Q * t.real();
            I = re; Q = im;
            if (++ph == period) ph = 0;
        }
        wr[nh + i] = (float)I;
        wi[nh + i] = (float)Q;
    }
    const long long base = n_in0 - nh, last = n_in0 + n - 1;
    long long k = 0;
    for (long long m = n_out0;; ++m) {
        double pos = (double)m * step;
        long long j = (long long)std::floor(pos);
        if (j + 1 > last) break;
        const float* pr = &wr[j - base - nt + 1];
        const float* pi = &wi[j - base - nt + 1];
        float yr0 = 0, yi0 = 0, yr1 = 0, yi1 = 0;
        for (int q = 0; q < nt; ++q) {
            float t = taps_rev[q];
            yr0 += t * pr[q];
            yi0 += t * pi[q];
            yr1 += t * pr[q + 1];
            yi1 += t * pi[q + 1];
        }
        float frac = (float)(pos - (double)j);
        out[k++] = cf(yr0 + frac * (yr1 - yr0), yi0 + frac * (yi1 - yi0));
    }
    for (int i = 0; i < nh; ++i) hist[i] = cf(wr[n + i], wi[n + i]);
    return k;
}
