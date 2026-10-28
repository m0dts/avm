// ldpc.cpp -- C++ port of hf_ofdm_ldpc.py: AVM's rate-1/2 QC-LDPC code
// (12 x 24 base matrix, Z = 27 / 54 / 81: N = 648 / 1296 / 1944 bits), the
// systematic encoder and the row-layered normalized min-sum decoder with
// float32 messages. Same codewords, and the same decisions and iteration
// counts as the numba decoder (identical float32 operations in the same
// order -- build with -ffp-contract=off).
//
// The tables are built at the first call (~ms here; the Python version
// cached them in a file because they took seconds).
//
//   g++ -O3 -shared -fPIC -std=c++17 -ffp-contract=off -o libldpc.so ldpc.cpp
//   zig c++ -O3 -shared -std=c++17 -ffp-contract=off -o ldpc.dll ldpc.cpp
#include <cmath>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <vector>

#if defined(_WIN32)
#define LD_API extern "C" __declspec(dllexport)
#else
#define LD_API extern "C" __attribute__((visibility("default")))
#endif

namespace {

// ---------------------------------------------------------------- the code (hf_ofdm_ldpc.py _H_BG_81)
const int MB = 12, NB = 24;
const int H_BG_81[MB][NB] = {
    {20, -1, -1, -1, -1, -1, 19, 46, 75, -1, -1, -1,  1,  0, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1},
    {-1, -1, 41, -1, 24, -1, -1, 67, -1,  9, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1, -1, -1},
    {-1, 61, 47, -1, -1, 30, -1, -1, -1, 76, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1, -1},
    {58, -1, 76, -1, -1, -1, 10, -1, 16, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1, -1},
    {80, -1, -1, -1, 54, -1, 73, -1, -1, -1, -1,  0, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1, -1},
    {-1, 74, 18, -1, 24, -1, -1, -1, -1, 72, -1, 66, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1, -1},
    {-1, -1, -1, 52, -1, -1, -1, 30,  8, -1, -1, -1,  0, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1, -1},
    {-1, 19, 10, -1, -1, 17, -1, -1, -1, -1, 25, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1, -1},
    {-1, 65, -1, 30, -1, 68, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1, -1},
    {56, 60, -1, -1, 24, -1, -1, -1, -1, -1, 35, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0, -1},
    {36, -1, -1, 35, -1, -1, 40, -1, -1, -1, -1, 56, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0,  0},
    {24, -1, -1, 69, -1, 72, -1, -1, -1, -1, 58, -1,  1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,  0},
};

struct Table {
    int Z = 0, M = 0, N = 0, K = 0;
    // the base matrix at this Z, per layer (block row): its blocks' column
    // and shift, column ascending (= each check row's edge order)
    std::vector<std::vector<std::pair<int, int>>> layers;
    // edges in np.where(H) order: row-major, column ascending within a row
    std::vector<int32_t> edge_var;         // per edge
    std::vector<int32_t> check_start;      // M + 1: a check row's edges are [start[i], start[i+1])
    // parity generator, packed: for each info bit, the parity bits it flips
    // (G_parity columns), as 64-bit words
    int pw = 0;                            // words per parity vector
    std::vector<uint64_t> gcols;           // K x pw
};
Table g_tables[3];
std::once_flag g_once;

int z_index(int z) { return z == 27 ? 0 : z == 54 ? 1 : 2; }

void build(Table& t, int Z) {
    t.Z = Z;
    t.M = MB * Z;
    t.N = NB * Z;
    t.K = t.N - t.M;
    // base matrix at this Z: floor(P81 * Z / 81) (np.floor of the float product)
    int sh[MB][NB];
    for (int i = 0; i < MB; ++i)
        for (int j = 0; j < NB; ++j)
            sh[i][j] = H_BG_81[i][j] < 0 ? -1 : (int)std::floor(H_BG_81[i][j] * ((double)Z / 81.0));
    // H: row r = i*Z + a has a 1 at column j*Z + ((a + s) % Z) for each block
    // (np.roll(eye, s, axis=1): eye's 1 at column a moves to (a + s) % Z)
    t.layers.assign(MB, {});
    for (int i = 0; i < MB; ++i)
        for (int j = 0; j < NB; ++j)
            if (sh[i][j] >= 0) t.layers[i].push_back({j, sh[i][j] % Z});
    t.check_start.assign(t.M + 1, 0);
    t.edge_var.clear();
    std::vector<std::vector<uint8_t>> H(t.M, std::vector<uint8_t>(t.N, 0));
    for (int i = 0; i < MB; ++i)
        for (int j = 0; j < NB; ++j)
            if (sh[i][j] >= 0) {
                int s = sh[i][j] % Z;
                for (int a = 0; a < Z; ++a) H[i * Z + a][j * Z + (a + s) % Z] = 1;
            }
    for (int r = 0; r < t.M; ++r) {
        t.check_start[r] = (int32_t)t.edge_var.size();
        for (int c = 0; c < t.N; ++c)
            if (H[r][c]) t.edge_var.push_back(c);
    }
    t.check_start[t.M] = (int32_t)t.edge_var.size();
    // G_parity = HB^-1 * HA over GF(2): HB = H[:, K:] (M x M), HA = H[:, :K].
    // Gaussian elimination on [HB | HA] with bit-packed rows gives [I | G_parity].
    const int M = t.M, K = t.K;
    const int W = (M + K + 63) / 64;
    std::vector<uint64_t> aug((size_t)M * W, 0);
    auto setbit = [&](int r, int c) { aug[(size_t)r * W + (c >> 6)] |= (uint64_t)1 << (c & 63); };
    auto getbit = [&](int r, int c) { return (aug[(size_t)r * W + (c >> 6)] >> (c & 63)) & 1; };
    for (int r = 0; r < M; ++r) {
        for (int c = 0; c < M; ++c) if (H[r][K + c]) setbit(r, c);
        for (int c = 0; c < K; ++c) if (H[r][c]) setbit(r, M + c);
    }
    for (int c = 0; c < M; ++c) {
        int p = -1;
        for (int r = c; r < M; ++r) if (getbit(r, c)) { p = r; break; }
        if (p < 0) return;   // singular (can't happen for this code)
        if (p != c)
            for (int w = 0; w < W; ++w) std::swap(aug[(size_t)p * W + w], aug[(size_t)c * W + w]);
        for (int r = 0; r < M; ++r)
            if (r != c && getbit(r, c))
                for (int w = 0; w < W; ++w) aug[(size_t)r * W + w] ^= aug[(size_t)c * W + w];
    }
    // G_parity[p][u] = aug[p][M + u]; store per info bit u the parity column
    t.pw = (M + 63) / 64;
    t.gcols.assign((size_t)K * t.pw, 0);
    for (int p = 0; p < M; ++p)
        for (int u = 0; u < K; ++u)
            if (getbit(p, M + u)) t.gcols[(size_t)u * t.pw + (p >> 6)] |= (uint64_t)1 << (p & 63);
}

void init() {
    std::call_once(g_once, [] {
        build(g_tables[0], 27);
        build(g_tables[1], 54);
        build(g_tables[2], 81);
    });
}

// ldpc_block_params
void block_params(long long msg_bits, int& Z, int& N, int& K, long long& n_blocks) {
    if (msg_bits <= 0) { Z = 27; N = 648; K = 324; n_blocks = 0; return; }
    Z = msg_bits <= 324 ? 27 : msg_bits <= 648 ? 54 : 81;
    K = 12 * Z;
    N = 24 * Z;
    n_blocks = (msg_bits + K - 1) / K;
    if (n_blocks < 1) n_blocks = 1;
}

void encode_block(const Table& t, const uint8_t* u, uint8_t* out) {
    std::memcpy(out, u, t.K);
    std::vector<uint64_t> acc(t.pw, 0);
    for (int i = 0; i < t.K; ++i)
        if (u[i]) {
            const uint64_t* g = t.gcols.data() + (size_t)i * t.pw;
            for (int w = 0; w < t.pw; ++w) acc[w] ^= g[w];
        }
    for (int p = 0; p < t.M; ++p) out[t.K + p] = (uint8_t)((acc[p >> 6] >> (p & 63)) & 1);
}

// _layered_min_sum_numba with float32 work arrays, one LAYER at a time: the
// Z check rows of a base-matrix row share no variables (each block is a
// permutation), so they are updated side by side -- loops over z, which the
// compiler vectorises (NEON 4 floats, AVX 8) -- instead of row by row. Each
// row still does the same float32 operations in the same order (its blocks
// in column order; first minimum wins ties), so the decisions and iteration
// counts are the row-serial decoder's exactly. A block (column j, shift s)
// gives sub-row z the variable j*Z + (z + s) % Z: gathered into a contiguous
// rotated copy, worked on, scattered back.
const int ZMAX = 81, MAXDEG = 16;
struct Decoder {
    std::vector<float> post, c2v;           // c2v: per layer, per block, Z values
    std::vector<uint8_t> dec;
    int decode(const Table& tb, const double* llr, int max_iter, float a, int stall_iter, bool& ok) {
        const int M = tb.M, N = tb.N, Z = tb.Z;
        post.resize(N);
        size_t nblk = 0;
        for (const auto& L : tb.layers) nblk += L.size();
        c2v.assign(nblk * Z, 0.0f);
        dec.assign(N, 0);
        for (int j = 0; j < N; ++j) post[j] = (float)llr[j];
        int best_unsat = M + 1, stall = 0;
        alignas(64) float T[MAXDEG][ZMAX];
        alignas(64) float mn1[ZMAX], mn2[ZMAX];
        alignas(64) int32_t idx[ZMAX], neg[ZMAX];
        alignas(64) uint8_t syn[ZMAX];
        for (int it = 0; it < max_iter; ++it) {
            float* cl = c2v.data();
            for (const auto& L : tb.layers) {
                const int deg = (int)L.size();
                for (int z = 0; z < Z; ++z) { mn1[z] = 1e30f; mn2[z] = 1e30f; idx[z] = -1; neg[z] = 0; }
                for (int b = 0; b < deg; ++b) {
                    const float* p = &post[(size_t)L[b].first * Z];
                    const int s = L[b].second;
                    float* t = T[b];
                    const float* c = cl + (size_t)b * Z;
                    // gather the rotated block: t[z] = post[j Z + (z + s) % Z] - c2v
                    for (int z = 0; z < Z - s; ++z) t[z] = p[z + s] - c[z];
                    for (int z = Z - s; z < Z; ++z) t[z] = p[z + s - Z] - c[z];
                    // branch-free so it vectorises: the second minimum
                    // min(m2, max(m1, av)) is exactly the if/else-if's
                    // result in every case (ties too), the first min(m1, av)
                    for (int z = 0; z < Z; ++z) {
                        float v = t[z], av = std::fabs(v), m1 = mn1[z];
                        int32_t lt1 = av < m1 ? -1 : 0;
                        mn2[z] = std::fmin(mn2[z], std::fmax(m1, av));
                        mn1[z] = std::fmin(m1, av);
                        idx[z] = (idx[z] & ~lt1) | (b & lt1);
                        neg[z] ^= (v < 0) ? 1 : 0;
                    }
                }
                for (int b = 0; b < deg; ++b) {
                    float* __restrict p = &post[(size_t)L[b].first * Z];
                    const int s = L[b].second;
                    const float* __restrict t = T[b];
                    float* __restrict c = cl + (size_t)b * Z;
                    alignas(64) float nv[ZMAX];
                    for (int z = 0; z < Z; ++z) {
                        float v = t[z];
                        float m = (idx[z] == b ? mn2[z] : mn1[z]) * a;
                        int sg = neg[z] ^ ((v < 0) ? 1 : 0);
                        float nw = sg ? -m : m;
                        c[z] = nw;
                        nv[z] = v + nw;
                    }
                    for (int z = 0; z < Z - s; ++z) p[z + s] = nv[z];
                    for (int z = Z - s; z < Z; ++z) p[z + s - Z] = nv[z];
                }
                cl += (size_t)deg * Z;
            }
            for (int j = 0; j < N; ++j) dec[j] = post[j] >= 0 ? 0 : 1;
            int unsat = 0;
            for (const auto& L : tb.layers) {
                for (int z = 0; z < Z; ++z) syn[z] = 0;
                for (const auto& blk : L) {
                    const uint8_t* d = &dec[(size_t)blk.first * Z];
                    const int s = blk.second;
                    for (int z = 0; z < Z - s; ++z) syn[z] ^= d[z + s];
                    for (int z = Z - s; z < Z; ++z) syn[z] ^= d[z + s - Z];
                }
                for (int z = 0; z < Z; ++z) unsat += syn[z];
            }
            if (unsat == 0) { ok = true; return it + 1; }
            if (unsat < best_unsat) { best_unsat = unsat; stall = 0; }
            else if (++stall >= stall_iter) { ok = false; return it + 1; }
        }
        ok = false;
        return max_iter;
    }
};
thread_local Decoder g_dec;

}  // namespace

// ---------------------------------------------------------------- C interface
LD_API long long ldpc_coded_len(long long msg_bits) {
    if (msg_bits <= 0) return 0;
    int Z, N, K;
    long long nb;
    block_params(msg_bits, Z, N, K, nb);
    return nb * N;
}

// bits (0/1, msg_bits of them) -> out (ldpc_coded_len(msg_bits) coded bits)
LD_API void ldpc_encode(const uint8_t* bits, long long msg_bits, uint8_t* out) {
    if (msg_bits <= 0) return;
    init();
    int Z, N, K;
    long long nb;
    block_params(msg_bits, Z, N, K, nb);
    const Table& t = g_tables[z_index(Z)];
    std::vector<uint8_t> u(K);
    for (long long b = 0; b < nb; ++b) {
        for (int i = 0; i < K; ++i) {
            long long p = b * K + i;
            u[i] = p < msg_bits ? (bits[p] & 1) : 0;
        }
        encode_block(t, u.data(), out + b * N);
    }
}

// ldpc_decode_soft(_codeword): llr has >= ldpc_coded_len(msg_bits) values
// (positive = likely 0). out_bits gets msg_bits bits; out_cw (may be null)
// gets the decoded codewords. Returns 1 if every block converged (and the
// padding decoded as zeros: the codeword is then exactly the re-encoding),
// else 0. *iters (may be null): total iterations over the blocks.
LD_API int ldpc_decode(const double* llr, long long n_llr, long long msg_bits, int max_iter, double alpha,
                       int stall_iter, uint8_t* out_bits, uint8_t* out_cw, long long* iters) {
    if (msg_bits <= 0) return 1;
    init();
    int Z, N, K;
    long long nb;
    block_params(msg_bits, Z, N, K, nb);
    if (n_llr < nb * N) return -1;
    const Table& t = g_tables[z_index(Z)];
    std::vector<double> blk(N);
    long long last_valid = msg_bits - (nb - 1) * K;
    bool all_ok = true;
    long long total_it = 0;
    float a = (float)alpha;
    for (long long b = 0; b < nb; ++b) {
        std::memcpy(blk.data(), llr + b * N, sizeof(double) * N);
        if (b == nb - 1 && last_valid < K)
            for (long long i = last_valid; i < K; ++i) blk[i] = 100.0;
        bool ok;
        total_it += g_dec.decode(t, blk.data(), max_iter, a, stall_iter, ok);
        all_ok = all_ok && ok;
        for (int i = 0; i < K; ++i) {
            long long p = b * K + i;
            if (p < msg_bits) out_bits[p] = g_dec.dec[i];
        }
        if (out_cw) std::memcpy(out_cw + b * N, g_dec.dec.data(), N);
        if (b == nb - 1 && all_ok && last_valid < K)
            for (long long i = last_valid; i < K; ++i)
                if (g_dec.dec[i]) { all_ok = false; break; }
    }
    if (iters) *iters = total_it;
    return all_ok ? 1 : 0;
}
