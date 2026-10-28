// avm_app.h -- what the AVM programs (avm_rx, avm_tx) share on top of the
// modem: a mode's configuration (hf_ofdm_common.build_config, hf_sc /
// hf_sch configs) and per-mode frame sizes (frame_symbol_count,
// estimate_effective_bitrate). Include after modem_rx.cpp, inside an
// anonymous namespace.
#pragma once
// ====================================================================== config
// hf_ofdm_common.build_config (OFDM modes, DRM_MODES + Dream's carrier
// tables) and hf_sc.SCConfig / hf_sch.SHConfig.
struct ModeSpec { const char* name; double spacing, guard; int freq_int, time_int, halves; double min_occ; };
const ModeSpec MODE_SPECS[] = {
    {"A", 125.0 / 3, 1.0 / 9, 4, 5, 2, 0},
    {"B", 375.0 / 8, 1.0 / 4, 2, 3, 2, 0},
    {"C", 750.0 / 11, 4.0 / 11, 2, 2, 2, 0},
    {"D", 750.0 / 7, 11.0 / 14, 1, 3, 2, 0},
    {"VU", 2500.0, 1.0 / 12, 4, 4, 32, 80},
};
struct KTab { const char* mode; double occ; int kmin, kmax; };
const KTab K_TABLE[] = {
    {"A", 4.5, 2, 102}, {"A", 5, 2, 114}, {"A", 9, -102, 102}, {"A", 10, -114, 114}, {"A", 18, -98, 314},
    {"A", 20, -110, 350},
    {"B", 4.5, 1, 91}, {"B", 5, 1, 103}, {"B", 9, -91, 91}, {"B", 10, -103, 103}, {"B", 18, -87, 279},
    {"B", 20, -99, 311},
    {"C", 10, -69, 69}, {"C", 20, -67, 213},
    {"D", 10, -44, 44}, {"D", 20, -43, 135},
};
const double EDGE_GUARD = 0.03;

long long floordiv(long long a, long long b) { long long q = a / b; return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q; }

struct Mode {
    std::string mode, modulation;
    int sc = 0;                   // 0 OFDM, 1 S, 2 SH
    double fs = 0, spacing = 0;
    int n_fft = 0, n_cp = 0, symbol_len = 0, preamble_len = 0;
    std::vector<int> data_bins, fft_bins;
    int freq_int = 0, time_int = 0, halves = 2, bpc = 2;
    bool ldpc = false, time_first = false;
    // S / SH
    double occupancy_hz = 0, symbol_rate = 0;
    int k10 = 0;
};

bool build_mode(const std::string& mode, double occ, const std::string& mod, const std::string& fec, Mode& m,
                std::string& err) {
    m.mode = mode;
    m.modulation = mod;
    m.ldpc = fec == "ldpc";
    if (mode == "S" || mode == "SH") {
        int w = (int)std::nearbyint(occ);
        if (w != 10 && w != 20 && w != 40) { err = "mode " + mode + " is 10, 20, 40 kHz wide"; return false; }
        m.sc = mode == "S" ? 1 : 2;
        if (m.sc == 1 && mod != "qpsk" && mod != "8psk") { err = "mode S has qpsk, 8psk"; return false; }
        if (m.sc == 2 && mod != "qpsk" && mod != "8psk" && mod != "16psk") { err = "mode SH has qpsk, 8psk, 16psk"; return false; }
        m.bpc = mod == "qpsk" ? 2 : mod == "8psk" ? 3 : 4;
        m.ldpc = true;
        m.occupancy_hz = occ * 1000;
        m.symbol_rate = m.occupancy_hz / (1 + SC_ALPHA);
        m.fs = SC_SPS * m.symbol_rate;
        m.symbol_len = SC_SPS;
        m.n_cp = SC_SPS;
        m.n_fft = 128;
        m.spacing = m.symbol_rate / 62;
        m.k10 = (int)std::nearbyint(occ / 10);
        m.preamble_len = m.sc == 1 ? 256 * SC_SPS : (SH_SHORT_LEN + SH_PRE_UWS * 64 * m.k10) * SC_SPS;
        int k = (int)std::nearbyint(m.occupancy_hz / 2 / m.spacing - 0.5);
        for (int b = -k; b <= k; ++b) m.data_bins.push_back(b);
        return true;
    }
    const ModeSpec* sp = nullptr;
    for (const auto& s : MODE_SPECS) if (mode == s.name) sp = &s;
    if (!sp) { err = "unknown mode " + mode; return false; }
    if (sp->min_occ && occ < sp->min_occ) { err = "mode " + mode + " needs at least 80 kHz occupancy"; return false; }
    m.bpc = mod == "16qam" ? 4 : mod == "32qam" ? 5 : 2;
    const KTab* kt = nullptr;
    for (const auto& t : K_TABLE) if (mode == t.mode && occ == t.occ) kt = &t;
    if (kt) {
        for (int k = kt->kmin; k < 0; ++k) m.data_bins.push_back(k);
        for (int k = 1; k <= kt->kmax; ++k) m.data_bins.push_back(k);
    } else {
        int nc = (int)std::nearbyint(occ * 1000 * (1 - 2 * EDGE_GUARD) / sp->spacing);
        int half = nc / 2;
        if (mode == "VU") {
            for (int k = -half; k < nc - half + 1; ++k) m.data_bins.push_back(k);
        } else {
            for (int k = -half; k < 0; ++k) m.data_bins.push_back(k);
            for (int k = 1; k < nc - half + 1; ++k) m.data_bins.push_back(k);
        }
    }
    int lo = *std::min_element(m.data_bins.begin(), m.data_bins.end());
    int hi = *std::max_element(m.data_bins.begin(), m.data_bins.end());
    long long mid = floordiv((long long)lo + hi, 2);
    for (int b : m.data_bins) m.fft_bins.push_back((int)(b - mid));
    int span = hi - lo;
    m.n_fft = (int)std::ceil(span / (1 - 2 * EDGE_GUARD));
    if (m.n_fft % 2) m.n_fft += 1;
    m.spacing = sp->spacing;
    m.fs = sp->spacing * m.n_fft;
    m.n_cp = (int)std::nearbyint(m.n_fft * sp->guard);
    m.freq_int = sp->freq_int;
    m.time_int = sp->time_int;
    m.halves = sp->halves;
    m.symbol_len = m.n_fft + m.n_cp;
    m.preamble_len = m.n_cp + m.halves * (m.n_fft / 2);
    m.time_first = (mode == "A" || mode == "B" || mode == "C") && m.freq_int > 1;
    return true;
}

// ====================================================================== the modem for a mode
struct Modem {
    Mode m;
    Cfg* c = nullptr;          // OFDM
    SyncParams sp;
    SC* sc = nullptr;          // S / SH
    SHGeo g{};
    Result res;
    SDecoded sres;
    SHDecoded hres;

    explicit Modem(const Mode& mode) : m(mode) {
        if (m.sc) {
            sc = new SC(m.sc == 2, m.k10, m.bpc);
            if (m.sc == 2) g = sh_geo(*sc);
            return;
        }
        c = new Cfg();
        c->n_fft = m.n_fft; c->n_cp = m.n_cp; c->n_data = (int)m.fft_bins.size();
        c->freq_int = m.freq_int; c->time_int = m.time_int; c->preamble_halves = m.halves;
        c->bpc = m.bpc; c->ldpc = m.ldpc; c->time_first = m.time_first;
        c->fft_bins = m.fft_bins;
        build_tables(*c);
        sp.fs = m.fs;
        sp.spacing = m.spacing;
    }

    // hf_ofdm_common.frame_symbol_count
    long long s_frame_samples(int n_bytes) const {
        long long coded = ldpc_coded_len((long long)(n_bytes + 4) * 8);
        long long n_data = (coded + m.bpc - 1) / m.bpc, n_blocks = (n_data + SC_DATA_BLOCK - 1) / SC_DATA_BLOCK;
        long long total = S_PRE_LEN + S_HDR_BITS * S_HDR_REPEAT + n_data + (n_blocks + 1) * SC_PILOT_LEN;
        return total * SC_SPS + 2 * S_RRC_DELAY - (SC_SPS - 1);
    }
    long long sh_n_blocks(int n_bytes) const {
        long long per = (long long)m.bpc * g.D;
        return (ldpc_coded_len((long long)(n_bytes + 4) * 8) + per - 1) / per;
    }
    long long sh_frame_samples(int n_bytes) const {
        long long n_sym = g.PRE_HF + (long long)g.N * (1 + sh_n_blocks(n_bytes));
        return n_sym * SC_SPS + 2 * S_RRC_DELAY - (SC_SPS - 1);
    }
    long long frame_symbol_count(long long n_bytes) const {
        if (m.sc) {
            long long fsamp = m.sc == 1 ? s_frame_samples((int)n_bytes) : sh_frame_samples((int)n_bytes);
            long long a = fsamp - m.preamble_len;
            return (a + SC_SPS - 1) / SC_SPS;
        }
        std::vector<int> hidx = header_symbol_plan(*c);
        long long needed = m.ldpc ? ldpc_coded_len(n_bytes * 8) : 2 * (n_bytes * 8 + FEC_N_MEM);
        return symbols_until(*c, hidx.back() + 1, needed);
    }
    // estimate_effective_bitrate(cfg, bytes, gap) (no out_fs)
    double effective_bitrate(int bytes, double gap_ms) const {
        long long n_frame = frame_symbol_count(bytes) * m.symbol_len + m.preamble_len;
        return bytes * 8.0 / (n_frame / m.fs + gap_ms / 1000.0);
    }
};


// ---- hf_ofdm_common.lo_phasor_table (the receiver's front end and the
// transmitter's LO pre-shift)
// Fraction(x).limit_denominator(maxd) for a finite double, as Python's
struct Frac { __int128 n, d; };
__int128 i128abs(__int128 v) { return v < 0 ? -v : v; }
__int128 gcd128(__int128 a, __int128 b) { a = i128abs(a); b = i128abs(b); while (b) { __int128 t = a % b; a = b; b = t; } return a; }
Frac frac_exact(double x) {
    int e;
    double mant = std::frexp(x, &e);                   // x = mant * 2^e, 0.5 <= |mant| < 1
    __int128 n = (__int128)std::ldexp(mant, 53);       // exact
    int p = e - 53;
    __int128 d = 1;
    if (p >= 0) n <<= p; else d <<= -p;
    __int128 g = gcd128(n, d);
    return {n / g, d / g};
}
__int128 floordiv128(__int128 a, __int128 b) { __int128 q = a / b; return (a % b != 0 && ((a < 0) != (b < 0))) ? q - 1 : q; }
Frac limit_denominator(Frac f, long long maxd) {
    if (f.d <= maxd) return f;
    __int128 p0 = 0, q0 = 1, p1 = 1, q1 = 0, n = f.n, d = f.d;
    while (true) {
        __int128 a = floordiv128(n, d);
        __int128 q2 = q0 + a * q1;
        if (q2 > maxd) break;
        __int128 np0 = p1, nq0 = q1, np1 = p0 + a * p1;
        p0 = np0; q0 = nq0; p1 = np1; q1 = q2;
        __int128 nn = d, nd = n - a * d;
        n = nn; d = nd;
    }
    __int128 k = (maxd - q0) / q1;
    // Python 3.12+: compare |bound2 - self| <= |bound1 - self| as fractions
    Frac b1{p0 + k * p1, q0 + k * q1}, b2{p1, q1};
    // |b2 - f| <= |b1 - f|  <=>  |b2.n f.d - f.n b2.d| * b1.d <= |b1.n f.d - f.n b1.d| * b2.d
    __int128 l = i128abs(b2.n * f.d - f.n * b2.d) * b1.d, r = i128abs(b1.n * f.d - f.n * b1.d) * b2.d;
    return l <= r ? b2 : b1;
}

// hf_ofdm_common.lo_phasor_table: false when there is no short exact period
bool lo_phasor_table(double lo, double fs, std::vector<cd>& table) {
    table.clear();
    if (lo == 0) { table.push_back(cd(1, 0)); return true; }
    Frac a = limit_denominator(frac_exact(lo), 1000000), b = limit_denominator(frac_exact(fs), 1000000);
    if (b.n == 0) return false;
    __int128 n = a.n * b.d, d = a.d * b.n;
    if (d < 0) { n = -n; d = -d; }
    __int128 gg = gcd128(n, d);
    n /= gg; d /= gg;
    if (std::fabs((double)n / (double)d - lo / fs) > 1e-12 || d > 65536) return false;
    long long nn = (long long)n, dd = (long long)d;
    table.resize(dd);
    for (long long i = 0; i < dd; ++i) {
        long long mm = (long long)(((__int128)nn * i) % dd);
        if (mm < 0) mm += dd;
        double th = (2.0 * fft::PI * (double)mm) / (double)dd;   // (2j * pi * m) / d, as numpy
        table[i] = cd(std::cos(th), std::sin(th));
    }
    return true;
}

