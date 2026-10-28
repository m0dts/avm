// wavelet_core.cpp -- C++ port of wavelet_codec.py's per-frame work: the
// integer wavelet transforms, motion search and compensation, prediction,
// the bit-plane range coder, and the reference updates. Bit-exact with the
// Python codec (same integer arithmetic, same float operations in the same
// order on the encoder side), so packets from either decode on the other.
//
// The one-time layout (subband order and weights, the refresh-tile order)
// stays in Python (wavelet_codec_cpp.py) and is handed over at creation:
// that guarantees both use identical tables.
//
// Plain C interface for ctypes: build as a shared library, e.g.
//   g++ -O3 -shared -fPIC -std=c++17 -o libwavelet_core.so wavelet_core.cpp
//   zig c++ -O3 -shared -std=c++17 -o wavelet_core.dll wavelet_core.cpp
#include <cmath>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <chrono>
#include <map>
#include <vector>

#if defined(_WIN32)
#define WVT_API extern "C" __declspec(dllexport)
#else
#define WVT_API extern "C" __attribute__((visibility("default")))
#endif

using i64 = int64_t;
using i32 = int32_t;
using u8 = uint8_t;

namespace {

// ---------------------------------------------------------------- profiling (wvt_profile)
// seconds per stage, accumulated: encoder 0 motion search, 1 move reference,
// 2 analysis DWT, 3 class weights, 4 predict+quantise, 5 coder, 6 update+round trip;
// decoder 8 move reference, 9 predict, 10 coder, 11 update+round trip, 12 picture
double g_prof[16];
struct Lap {
    std::chrono::steady_clock::time_point t = std::chrono::steady_clock::now();
    void operator()(int slot) {
        auto n = std::chrono::steady_clock::now();
        g_prof[slot] += std::chrono::duration<double>(n - t).count();
        t = n;
    }
};

// ---------------------------------------------------------------- constants (wavelet_codec.py)
const double REC_BIAS = 0.4;
const int PIX_FRAC = 8;
const int LS = 14;
const i64 LH = i64(1) << (LS - 1);
const i64 A_FX = -25987, B_FX = -868, G_FX = 14466, D_FX = 7266, K_FX = 20155, KI_FX = 13318;
const int REC_FRAC = 8;
const i64 REC_BIAS_FX = 102;          // round(REC_BIAS * 256)
const int INVW_FRAC = 16;
const int WEIGHT_BITS = 3;
const int WEIGHT_LEVELS = 7;
const i64 A_DEN = 256 * WEIGHT_LEVELS;
const int N_CLS = 9;
const int CTX_BLK = 0;
const int CTX_SIG = CTX_BLK + N_CLS * 4;
const int CTX_SIGN = CTX_SIG + N_CLS * 3 * 2 * 2;
const int CTX_REF = CTX_SIGN + 1;
const int CTX_SB = CTX_REF + N_CLS * 2;
const int CTX_GRP = CTX_SB + N_CLS;
const int N_CTX = CTX_GRP + N_CLS * 2;
const int GROUP = 4;
const int TOP_BITS = 5;
const int HEADER_BYTES = 7;
const int MOTION_BYTES = 2;
const int MOTION_FLAG = 0x80;
const double SNAP_BITS_PER_PIXEL = 0.08;
const int BLK = 16;
const int BLK_RANGE = 7;
const int BLOCK_FLAG = 0x80;
const double BLK_MAX_SHARE = 0.25;
const double BLK_MIN_GAIN = 0.15;
const double BLK_MIN_ABS = 1.5;
const int MC_RANGE = 8;
const double MC_GAIN = 0.92;
const int PASS_ORDER = 1;
const i64 ADAPT0 = 4, ADAPT1 = 6, ADAPT2 = 16;

const i64 INIT_PROBS[] = {
    51629, 51293, 32768, 32768, 52228, 49312, 32768, 32768, 62586, 56577, 52629, 45733,
    62577, 51981, 52177, 46194, 62013, 49366, 48967, 40074, 52726, 47345, 32768, 32768,
    51950, 48506, 32768, 32768, 61569, 56537, 51760, 45896, 61744, 54310, 52766, 41401,
    62101, 32768, 61096, 32768, 60545, 32768, 51260, 32768, 51027, 32768, 36641, 32768,
    60824, 32768, 60106, 32768, 58759, 32768, 51083, 32768, 48771, 32768, 40119, 32768,
    62219, 52084, 60523, 54160, 56289, 47546, 56732, 48036, 45272, 41240, 44197, 37998,
    60903, 55452, 58543, 55862, 52890, 46812, 52520, 48976, 45876, 40792, 41356, 36370,
    61007, 56189, 59765, 57251, 49880, 44819, 54740, 50118, 42879, 36817, 34756, 34679,
    60060, 32768, 58804, 32768, 55159, 32768, 48398, 32768, 41440, 32768, 35420, 32768,
    59481, 32768, 60012, 32768, 54659, 32768, 50950, 32768, 41460, 32768, 37967, 32768,
    61332, 51072, 62303, 58982, 53964, 44166, 58701, 55555, 41354, 41545, 40317, 34920,
    61535, 51687, 63779, 62973, 49987, 36255, 61308, 56765, 35747, 34280, 34257, 25486,
    32595, 40771, 35995, 44475, 38462, 45885, 40057, 47362, 40979, 49956, 43352, 45523,
    41897, 50358, 48173, 57415, 48545, 58027, 57344, 2800, 46893, 49740, 52579, 56806,
    57048, 61517, 63398, 64512, 1024, 50000, 1024, 50000, 32982, 36142, 60302, 43050,
    62794, 50298, 1024, 50000, 1024, 50000, 31467, 33056, 57403, 41552,
};
static_assert(sizeof(INIT_PROBS) / sizeof(INIT_PROBS[0]) == N_CTX, "INIT_PROBS size");

// Python's floor division (C++ '/' truncates towards zero)
inline i64 floordiv(i64 a, i64 b) {
    i64 q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) --q;
    return q;
}
inline i64 imin(i64 a, i64 b) { return a < b ? a : b; }
inline i64 imax(i64 a, i64 b) { return a > b ? a : b; }
inline i64 clampi(i64 v, i64 lo, i64 hi) { return v < lo ? lo : (v > hi ? hi : v); }

// ---------------------------------------------------------------- integer DWT
// (wavelet_codec.py _lift_d / _lift_s / _fwd_1d_int / _inv_1d_int /
// _fwd_axis0_int / _inv_axis0_int: products in 64 bits, planes int32).
// Each lifting step's neighbour sum is formed in 32 bits, then widened for
// the product: the coefficients stay within ~+-2^22, so the sum is the same
// as numba's 64-bit one -- and int32 x const -> int64 is one vector
// instruction on ARM NEON (a 64 x 64-bit product isn't).
inline void lift_d(i32* t, i64 hh, i64 k, i64 sign) {
    for (i64 i = 0; i < hh - 1; ++i)
        t[hh + i] += (i32)(sign * ((k * ((i64)(t[i] + t[i + 1])) + LH) >> LS));
    t[2 * hh - 1] += (i32)(sign * ((k * ((i64)(t[hh - 1] + t[hh - 1])) + LH) >> LS));
}
inline void lift_s(i32* t, i64 hh, i64 k, i64 sign) {
    t[0] += (i32)(sign * ((k * ((i64)(t[hh] + t[hh])) + LH) >> LS));
    for (i64 i = 1; i < hh; ++i)
        t[i] += (i32)(sign * ((k * ((i64)(t[hh + i - 1] + t[hh + i])) + LH) >> LS));
}
void fwd_1d(i32* x, i32* t, i64 n) {
    i64 hh = n / 2;
    for (i64 i = 0; i < hh; ++i) { t[i] = x[2 * i]; t[hh + i] = x[2 * i + 1]; }
    lift_d(t, hh, A_FX, 1);
    lift_s(t, hh, B_FX, 1);
    lift_d(t, hh, G_FX, 1);
    lift_s(t, hh, D_FX, 1);
    for (i64 i = 0; i < hh; ++i) {
        x[i] = (i32)(((i64)t[i] * KI_FX + LH) >> LS);
        x[hh + i] = (i32)(((i64)t[hh + i] * K_FX + LH) >> LS);
    }
}
void inv_1d(i32* x, i32* t, i64 n) {
    i64 hh = n / 2;
    for (i64 i = 0; i < hh; ++i) {
        t[i] = (i32)(((i64)x[i] * K_FX + LH) >> LS);
        t[hh + i] = (i32)(((i64)x[hh + i] * KI_FX + LH) >> LS);
    }
    lift_s(t, hh, D_FX, -1);
    lift_d(t, hh, G_FX, -1);
    lift_s(t, hh, B_FX, -1);
    lift_d(t, hh, A_FX, -1);
    for (i64 i = 0; i < hh; ++i) { x[2 * i] = t[i]; x[2 * i + 1] = t[hh + i]; }
}
// c: plane with row stride W (the full plane width), working on the top-left h x w
void fwd_axis0(i32* c, i64 W, i64 h, i64 w, i32* T) {
    i64 hh = h / 2;
    for (i64 i = 0; i < hh; ++i)
        for (i64 j = 0; j < w; ++j) { T[i * W + j] = c[(2 * i) * W + j]; T[(hh + i) * W + j] = c[(2 * i + 1) * W + j]; }
    for (i64 k = 0; k < hh + 2; ++k) {
        i64 i = k;
        if (i < hh) {
            i64 ip = imin(i + 1, hh - 1);
            for (i64 j = 0; j < w; ++j)
                T[(hh + i) * W + j] += (i32)((A_FX * ((i64)(T[i * W + j] + T[ip * W + j])) + LH) >> LS);
            i64 im = imax(i - 1, 0);
            for (i64 j = 0; j < w; ++j)
                T[i * W + j] += (i32)((B_FX * ((i64)(T[(hh + im) * W + j] + T[(hh + i) * W + j])) + LH) >> LS);
        }
        i = k - 1;
        if (0 <= i && i < hh) {
            i64 ip = imin(i + 1, hh - 1);
            for (i64 j = 0; j < w; ++j)
                T[(hh + i) * W + j] += (i32)((G_FX * ((i64)(T[i * W + j] + T[ip * W + j])) + LH) >> LS);
        }
        i = k - 2;
        if (0 <= i && i < hh) {
            i64 im = imax(i - 1, 0);
            for (i64 j = 0; j < w; ++j) {
                T[i * W + j] += (i32)((D_FX * ((i64)(T[(hh + im) * W + j] + T[(hh + i) * W + j])) + LH) >> LS);
                c[i * W + j] = (i32)(((i64)T[i * W + j] * KI_FX + LH) >> LS);
                c[(hh + i) * W + j] = (i32)(((i64)T[(hh + i) * W + j] * K_FX + LH) >> LS);
            }
        }
    }
}
void inv_axis0(i32* c, i64 W, i64 h, i64 w, i32* T) {
    i64 hh = h / 2;
    for (i64 i = 0; i < hh; ++i)
        for (i64 j = 0; j < w; ++j) {
            T[i * W + j] = (i32)(((i64)c[i * W + j] * K_FX + LH) >> LS);
            T[(hh + i) * W + j] = (i32)(((i64)c[(hh + i) * W + j] * KI_FX + LH) >> LS);
        }
    for (i64 k = 0; k < hh + 3; ++k) {
        i64 i = k;
        if (i < hh) {
            i64 im = imax(i - 1, 0);
            for (i64 j = 0; j < w; ++j)
                T[i * W + j] -= (i32)((D_FX * ((i64)(T[(hh + im) * W + j] + T[(hh + i) * W + j])) + LH) >> LS);
        }
        i = k - 1;
        if (0 <= i && i < hh) {
            i64 ip = imin(i + 1, hh - 1);
            for (i64 j = 0; j < w; ++j)
                T[(hh + i) * W + j] -= (i32)((G_FX * ((i64)(T[i * W + j] + T[ip * W + j])) + LH) >> LS);
        }
        i = k - 2;
        if (0 <= i && i < hh) {
            i64 im = imax(i - 1, 0);
            for (i64 j = 0; j < w; ++j)
                T[i * W + j] -= (i32)((B_FX * ((i64)(T[(hh + im) * W + j] + T[(hh + i) * W + j])) + LH) >> LS);
        }
        i = k - 3;
        if (0 <= i && i < hh) {
            i64 ip = imin(i + 1, hh - 1);
            for (i64 j = 0; j < w; ++j) {
                T[(hh + i) * W + j] -= (i32)((A_FX * ((i64)(T[i * W + j] + T[ip * W + j])) + LH) >> LS);
                c[(2 * i) * W + j] = T[i * W + j];
                c[(2 * i + 1) * W + j] = T[(hh + i) * W + j];
            }
        }
    }
}
void dwt2_fwd(i32* c, i64 H, i64 W, i64 levels, std::vector<i32>& T, std::vector<i32>& t) {
    T.resize(H * W);
    t.resize(W);
    i64 h = H, w = W;
    for (i64 l = 0; l < levels; ++l) {
        fwd_axis0(c, W, h, w, T.data());
        for (i64 i = 0; i < h; ++i) fwd_1d(c + i * W, t.data(), w);
        h /= 2;
        w /= 2;
    }
}
void dwt2_inv(i32* c, i64 H, i64 W, i64 levels, std::vector<i32>& T, std::vector<i32>& t) {
    T.resize(H * W);
    t.resize(W);
    for (i64 l = levels - 1; l >= 0; --l) {
        i64 h = H >> l, w = W >> l;
        for (i64 i = 0; i < h; ++i) inv_1d(c + i * W, t.data(), w);
        inv_axis0(c, W, h, w, T.data());
    }
}
void to_pixels(i32* c, i64 n, u8* out) {
    const i64 half = i64(1) << (PIX_FRAC - 1);
    for (i64 i = 0; i < n; ++i) {
        i64 p = (((i64)c[i] + half) >> PIX_FRAC) + 128;
        p = clampi(p, 0, 255);
        out[i] = (u8)p;
        c[i] = (i32)((p - 128) << PIX_FRAC);
    }
}

// ---------------------------------------------------------------- motion (encoder search; both ends warp)
// planes: row-major uint8, H x W
inline i32 px(const u8* a, i64 W, i64 i, i64 j) { return (i32)a[i * W + j]; }

void shift_plane(const u8* src, i64 H, i64 W, i64 qx, i64 qy, u8* out) {
    for (i64 i = 0; i < H; ++i) {
        i64 py = 4 * i - qy, y0 = py >> 2, fy = py & 3;
        i64 ya = clampi(y0, 0, H - 1), yb = clampi(y0 + 1, 0, H - 1);
        for (i64 j = 0; j < W; ++j) {
            i64 pxx = 4 * j - qx, x0 = pxx >> 2, fx = pxx & 3;
            i64 xa = clampi(x0, 0, W - 1), xb = clampi(x0 + 1, 0, W - 1);
            i64 top = (4 - fx) * px(src, W, ya, xa) + fx * px(src, W, ya, xb);
            i64 bot = (4 - fx) * px(src, W, yb, xa) + fx * px(src, W, yb, xb);
            out[i * W + j] = (u8)(((4 - fy) * top + fy * bot + 8) >> 4);
        }
    }
}
double sad(const u8* cur, const u8* prev, i64 H, i64 W, i64 dx, i64 dy, i64 step) {
    i64 acc = 0, n = 0;
    for (i64 i = imax(0, dy); i < imin(H, H + dy); i += step)
        for (i64 j = imax(0, dx); j < imin(W, W + dx); j += step) {
            i64 d = (i64)px(cur, W, i, j) - px(prev, W, i - dy, j - dx);
            acc += d < 0 ? -d : d;
            ++n;
        }
    return (double)acc / (double)imax(n, 1);
}
std::vector<u8> down2(const u8* a, i64 H0, i64 W0, i64& H, i64& W) {
    H = H0 / 2; W = W0 / 2;
    std::vector<u8> out(H * W);
    for (i64 i = 0; i < H; ++i)
        for (i64 j = 0; j < W; ++j)
            out[i * W + j] = (u8)(((i64)a[(2 * i) * W0 + 2 * j] + a[(2 * i) * W0 + 2 * j + 1] +
                                   a[(2 * i + 1) * W0 + 2 * j] + a[(2 * i + 1) * W0 + 2 * j + 1] + 2) >> 2);
    return out;
}
double sad_q(const u8* cur, const u8* prev, i64 H, i64 W, i64 qx, i64 qy, i64 step) {
    i64 m = 2 + ((qx < 0 ? -qx : qx) >> 2);
    i64 mv = 2 + ((qy < 0 ? -qy : qy) >> 2);
    i64 acc = 0, n = 0;
    for (i64 i = mv; i < H - mv; i += step) {
        i64 py = 4 * i - qy, y0 = py >> 2, fy = py & 3;
        for (i64 j = m; j < W - m; j += step) {
            i64 pxx = 4 * j - qx, x0 = pxx >> 2, fx = pxx & 3;
            i64 top = (4 - fx) * px(prev, W, y0, x0) + fx * px(prev, W, y0, x0 + 1);
            i64 bot = (4 - fx) * px(prev, W, y0 + 1, x0) + fx * px(prev, W, y0 + 1, x0 + 1);
            i64 d = (i64)px(cur, W, i, j) - (((4 - fy) * top + fy * bot + 8) >> 4);
            acc += d < 0 ? -d : d;
            ++n;
        }
    }
    return (double)acc / (double)imax(n, 1);
}
// (hx, hy) half pixels, cost, cost at (0, 0) -- wavelet_codec._global_motion_fast
void global_motion_fast(const u8* cur, const u8* prev, i64 H, i64 W, i64 rng,
                        i64& hx_out, i64& hy_out, double& cost, double& cost0) {
    i64 H1, W1, H2, W2, Ht, Wt;
    std::vector<u8> c1 = down2(cur, H, W, H1, W1), p1 = down2(prev, H, W, Ht, Wt);
    std::vector<u8> c2 = down2(c1.data(), H1, W1, H2, W2), p2 = down2(p1.data(), H1, W1, Ht, Wt);
    i64 r2 = (rng + 3) / 4;
    double best = 1e30;
    i64 bx = 0, by = 0;
    for (i64 dy = -r2; dy <= r2; ++dy)
        for (i64 dx = -r2; dx <= r2; ++dx) {
            double c = sad(c2.data(), p2.data(), H2, W2, dx, dy, 1);
            if (c < best) { best = c; bx = dx; by = dy; }
        }
    for (int lev = 0; lev < 2; ++lev) {
        const u8* cc = lev == 0 ? c1.data() : cur;
        const u8* pp = lev == 0 ? p1.data() : prev;
        i64 hh = lev == 0 ? H1 : H, ww = lev == 0 ? W1 : W;
        i64 step = lev == 0 ? 1 : 2;
        i64 cx = 2 * bx, cy = 2 * by;
        best = 1e30;
        for (i64 dy = cy - 1; dy < cy + 2; ++dy)
            for (i64 dx = cx - 1; dx < cx + 2; ++dx)
                if ((dx < 0 ? -dx : dx) <= rng && (dy < 0 ? -dy : dy) <= rng) {
                    double c = sad(cc, pp, hh, ww, dx, dy, step);
                    if (c < best) { best = c; bx = dx; by = dy; }
                }
    }
    i64 hx = 2 * bx, hy = 2 * by;
    best = 1e30;
    i64 cx = hx, cy = hy;
    for (i64 ty = cy - 1; ty < cy + 2; ++ty)
        for (i64 tx = cx - 1; tx < cx + 2; ++tx)
            if ((tx < 0 ? -tx : tx) <= 2 * rng && (ty < 0 ? -ty : ty) <= 2 * rng) {
                double c = sad_q(cur, prev, H, W, 2 * tx, 2 * ty, 2);
                if (c < best) { best = c; hx = tx; hy = ty; }
            }
    hx_out = hx;
    hy_out = hy;
    cost = best;
    cost0 = sad_q(cur, prev, H, W, 0, 0, 2);
}
double block_sad(const u8* cur, const u8* prev, i64 H, i64 W, i64 by, i64 bx, i64 blk, i64 qx, i64 qy, i64 step) {
    i64 acc = 0, n = 0;
    i64 i0 = by * blk, i1 = imin(H, (by + 1) * blk);
    i64 j0 = bx * blk, j1 = imin(W, (bx + 1) * blk);
    if ((qx & 3) == 0 && (qy & 3) == 0) {
        i64 sy = qy >> 2, sx = qx >> 2;
        for (i64 i = i0; i < i1; i += step) {
            i64 ya = clampi(i - sy, 0, H - 1);
            for (i64 j = j0; j < j1; j += step) {
                i64 xa = clampi(j - sx, 0, W - 1);
                i64 d = (i64)px(cur, W, i, j) - px(prev, W, ya, xa);
                acc += d < 0 ? -d : d;
                ++n;
            }
        }
        return (double)acc / (double)imax(n, 1);
    }
    // a block well inside the picture: no sample needs clamping to the edge
    // (most of them) -- the same sums without the per-pixel clamps
    bool inside = i1 > i0 && j1 > j0 &&
                  ((4 * i0 - qy) >> 2) >= 0 && ((4 * (i1 - 1) - qy) >> 2) + 1 <= H - 1 &&
                  ((4 * j0 - qx) >> 2) >= 0 && ((4 * (j1 - 1) - qx) >> 2) + 1 <= W - 1;
    if (inside) {
        for (i64 i = i0; i < i1; i += step) {
            i64 py = 4 * i - qy, y0 = py >> 2, fy = py & 3;
            const u8* ra = prev + y0 * W;
            const u8* rb = ra + W;
            const u8* rc = cur + i * W;
            for (i64 j = j0; j < j1; j += step) {
                i64 pxx = 4 * j - qx, x0 = pxx >> 2, fx = pxx & 3;
                i64 top = (4 - fx) * (i64)ra[x0] + fx * (i64)ra[x0 + 1];
                i64 bot = (4 - fx) * (i64)rb[x0] + fx * (i64)rb[x0 + 1];
                i64 v = ((4 - fy) * top + fy * bot + 8) >> 4;
                i64 d = (i64)rc[j] - v;
                acc += d < 0 ? -d : d;
                ++n;
            }
        }
        return (double)acc / (double)imax(n, 1);
    }
    for (i64 i = i0; i < i1; i += step) {
        i64 py = 4 * i - qy, y0 = py >> 2, fy = py & 3;
        i64 ya = clampi(y0, 0, H - 1), yb = clampi(y0 + 1, 0, H - 1);
        for (i64 j = j0; j < j1; j += step) {
            i64 pxx = 4 * j - qx, x0 = pxx >> 2, fx = pxx & 3;
            i64 xa = clampi(x0, 0, W - 1), xb = clampi(x0 + 1, 0, W - 1);
            i64 top = (4 - fx) * px(prev, W, ya, xa) + fx * px(prev, W, ya, xb);
            i64 bot = (4 - fx) * px(prev, W, yb, xa) + fx * px(prev, W, yb, xb);
            i64 v = ((4 - fy) * top + fy * bot + 8) >> 4;
            i64 d = (i64)px(cur, W, i, j) - v;
            acc += d < 0 ? -d : d;
            ++n;
        }
    }
    return (double)acc / (double)imax(n, 1);
}
void block_search_fast(const u8* cur, const u8* prev, i64 H, i64 W, i64 gx, i64 gy, i64 blk, i64 rng,
                       double min_gain, double min_abs, i64 nby, i64 nbx, std::vector<i64>& vx, std::vector<i64>& vy) {
    i64 cand[4][2];
    for (i64 by = 0; by < nby; ++by)
        for (i64 bx = 0; bx < nbx; ++bx) {
            double base = block_sad(cur, prev, H, W, by, bx, blk, 2 * gx, 2 * gy, 2);
            if (base < min_abs) { vx[by * nbx + bx] = 0; vy[by * nbx + bx] = 0; continue; }
            double best = base;
            i64 bdx = 0, bdy = 0;
            int nc = 0;
            if ((gx < 0 ? -gx : gx) <= rng && (gy < 0 ? -gy : gy) <= rng) { cand[nc][0] = -gx; cand[nc][1] = -gy; ++nc; }
            if (bx > 0) { cand[nc][0] = vx[by * nbx + bx - 1]; cand[nc][1] = vy[by * nbx + bx - 1]; ++nc; }
            if (by > 0) { cand[nc][0] = vx[(by - 1) * nbx + bx]; cand[nc][1] = vy[(by - 1) * nbx + bx]; ++nc; }
            for (int c = 0; c < nc; ++c) {
                i64 dx = cand[c][0], dy = cand[c][1];
                if (dx == 0 && dy == 0) continue;
                double v = block_sad(cur, prev, H, W, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy), 2);
                if (v < best) { best = v; bdx = dx; bdy = dy; }
            }
            for (i64 stp : {2, 1}) {
                i64 cx = bdx, cy = bdy;
                for (i64 dy = cy - stp; dy < cy + stp + 1; dy += stp)
                    for (i64 dx = cx - stp; dx < cx + stp + 1; dx += stp)
                        if ((dx != cx || dy != cy) && (dx < 0 ? -dx : dx) <= rng && (dy < 0 ? -dy : dy) <= rng) {
                            double v = block_sad(cur, prev, H, W, by, bx, blk, 2 * (gx + dx), 2 * (gy + dy), 2);
                            if (v < best) { best = v; bdx = dx; bdy = dy; }
                        }
            }
            if ((bdx != 0 || bdy != 0) && best < (1 - min_gain) * base && base - best >= min_abs) {
                vx[by * nbx + bx] = bdx;
                vy[by * nbx + bx] = bdy;
            } else {
                vx[by * nbx + bx] = 0;
                vy[by * nbx + bx] = 0;
            }
        }
}
void warp_plane(const u8* src, i64 H, i64 W, const i64* fx, const i64* fy, i64 nby, i64 nbx, i64 blk, i64 sub, u8* out) {
    i64 bs = blk / sub;
    for (i64 i = 0; i < H; ++i) {
        i64 cy = i - bs / 2;
        i64 b0 = cy >= 0 ? cy / bs : -1;
        i64 wy1 = cy >= 0 ? cy - b0 * bs : 0;
        i64 y0 = clampi(b0, 0, nby - 1), y1 = clampi(b0 + 1, 0, nby - 1);
        if (cy < 0) wy1 = 0;
        for (i64 j = 0; j < W; ++j) {
            i64 cx = j - bs / 2;
            i64 a0 = cx >= 0 ? cx / bs : -1;
            i64 wx1 = cx >= 0 ? cx - a0 * bs : 0;
            i64 x0 = clampi(a0, 0, nbx - 1), x1 = clampi(a0 + 1, 0, nbx - 1);
            i64 wx0 = bs - wx1, wy0 = bs - wy1;
            i64 mx = wy0 * (wx0 * fx[y0 * nbx + x0] + wx1 * fx[y0 * nbx + x1]) + wy1 * (wx0 * fx[y1 * nbx + x0] + wx1 * fx[y1 * nbx + x1]);
            i64 my = wy0 * (wx0 * fy[y0 * nbx + x0] + wx1 * fy[y0 * nbx + x1]) + wy1 * (wx0 * fy[y1 * nbx + x0] + wx1 * fy[y1 * nbx + x1]);
            i64 den = bs * bs;
            i64 qx = mx >= 0 ? (mx * 8 / sub + (den >> 1)) / den : -(((-mx) * 8 / sub + (den >> 1)) / den);
            i64 qy = my >= 0 ? (my * 8 / sub + (den >> 1)) / den : -(((-my) * 8 / sub + (den >> 1)) / den);
            i64 pxx = 16 * j - qx, pyy = 16 * i - qy;
            i64 xa0 = pxx >> 4, fxx = pxx & 15, ya0 = pyy >> 4, fyy = pyy & 15;
            i64 xa = clampi(xa0, 0, W - 1), xb = clampi(xa0 + 1, 0, W - 1);
            i64 ya = clampi(ya0, 0, H - 1), yb = clampi(ya0 + 1, 0, H - 1);
            i64 top = (16 - fxx) * px(src, W, ya, xa) + fxx * px(src, W, ya, xb);
            i64 bot = (16 - fxx) * px(src, W, yb, xa) + fxx * px(src, W, yb, xb);
            out[i * W + j] = (u8)(((16 - fyy) * top + fyy * bot + 128) >> 8);
        }
    }
}

// ---------------------------------------------------------------- range coder (LZMA-style)
struct RC {
    i64 low = 0, range = 0xFFFFFFFFLL, cache = 0, cache_size = 1, pos = -1, code = 0, nsym = 0;
};
inline void shift_low(RC& st, u8* buf, i64 blen) {
    i64 low = st.low;
    if (low < 0xFF000000LL || low >= 0x100000000LL) {
        i64 carry = low >> 32;
        i64 temp = st.cache;
        while (true) {
            if (0 <= st.pos && st.pos < blen) buf[st.pos] = (u8)((temp + carry) & 0xFF);
            st.pos += 1;
            temp = 0xFF;
            st.cache_size -= 1;
            if (st.cache_size == 0) break;
        }
        st.cache = (low >> 24) & 0xFF;
    }
    st.cache_size += 1;
    st.low = (low & 0x00FFFFFF) << 8;
}
inline void enc(RC& st, i64 p, i64 bit, u8* buf, i64 blen) {
    i64 bound = (st.range >> 16) * p;
    if (bit == 0) st.range = bound;
    else { st.low += bound; st.range -= bound; }
    while (st.range < 0x1000000) { st.range <<= 8; shift_low(st, buf, blen); }
}
void flush(RC& st, u8* buf, i64 blen) {
    st.low = (st.low + 0xFFFFFF) & ~(i64)0xFFFFFF;
    shift_low(st, buf, blen);
    shift_low(st, buf, blen);
}

struct Coder {
    // layout (subband tables)
    i64 nsb = 0;
    const i64 *sb_off, *sb_h, *sb_w, *sb_cls, *sb_par, *sb_boff, *sb_goff;
    i64 n_blocks = 0, n_groups = 0;
    // state
    int mode = 0;   // 0 encode, 1 decode
    RC st, sh;
    RC* enc_st;
    u8* buf;
    i64 blen;       // buf length (limit)
    i64 limit;
    i64 probs[N_CTX];
    i64 uses[N_CTX];
    uint32_t* mag;
    u8* neg;
    int8_t *sigp, *lastp;
    std::vector<int8_t> vis;
    std::vector<i64> sb_nsig, sb_nblk, blkmax, grpmax, sbmax;
    std::vector<u8> blks, grps;

    inline i64 sym(i64 ctx, i64 bit) {
        i64 p = probs[ctx];
        if (mode == 0) {
            enc(st, p, bit, buf, blen);
        } else {
            i64 bound = (st.range >> 16) * p;
            if (st.code < bound) { st.range = bound; bit = 0; }
            else { st.code -= bound; st.range -= bound; bit = 1; }
            while (st.range < 0x1000000) {
                st.range <<= 8;
                i64 b = st.pos < blen ? buf[st.pos] : 0;
                st.code = ((st.code << 8) | b) & 0xFFFFFFFFLL;
                st.pos += 1;
            }
            enc(sh, p, bit, nullptr, 0);
        }
        i64 u = uses[ctx];
        i64 s = imin(ADAPT0 + u / ADAPT2, ADAPT1);
        uses[ctx] = u + 1;
        if (bit == 0) probs[ctx] = p + ((65536 - p) >> s);
        else probs[ctx] = p - (p >> s);
        st.nsym += 1;
        return bit;
    }
    inline bool room(i64 k) const { return enc_st->pos + enc_st->cache_size + 1 + 2 * k <= limit; }

    bool code_sig(i64 j, i64 yy, i64 xx, i64 h, i64 w, i64 cls, i64 par, i64 p, i64 s) {
        if (!room(2)) return false;
        i64 hv = 0;
        if (yy > 0 && sigp[j - w] >= 0) hv += 1;
        if (yy < h - 1 && sigp[j + w] >= 0) hv += 1;
        if (xx > 0 && sigp[j - 1] >= 0) hv += 1;
        if (xx < w - 1 && sigp[j + 1] >= 0) hv += 1;
        i64 dg = 0;
        if (yy > 0 && xx > 0 && sigp[j - w - 1] >= 0) dg = 1;
        else if (yy > 0 && xx < w - 1 && sigp[j - w + 1] >= 0) dg = 1;
        else if (yy < h - 1 && xx > 0 && sigp[j + w - 1] >= 0) dg = 1;
        else if (yy < h - 1 && xx < w - 1 && sigp[j + w + 1] >= 0) dg = 1;
        i64 pf = 0;
        if (par >= 0 && sigp[sb_off[par] + (yy >> 1) * sb_w[par] + (xx >> 1)] >= 0) pf = 1;
        i64 ctx = CTX_SIG + ((cls * 3 + imin(hv, 2)) * 2 + dg) * 2 + pf;
        i64 bit = mode == 0 ? (mag[j] >> p) & 1 : 0;
        bit = sym(ctx, bit);
        if (bit) {
            i64 sg = sym(CTX_SIGN, mode == 0 ? neg[j] : 0);
            if (mode == 1) { neg[j] = (u8)sg; mag[j] |= (uint32_t)1 << p; }
            sigp[j] = (int8_t)p;
            lastp[j] = (int8_t)p;
            sb_nsig[s] += 1;
        }
        return true;
    }

    void maxima() {
        for (i64 s = 0; s < nsb; ++s) {
            i64 off = sb_off[s], h = sb_h[s], w = sb_w[s];
            i64 nbw = (w + 3) >> 2;
            i64 ngw = (nbw + GROUP - 1) / GROUP;
            for (i64 yy = 0; yy < h; ++yy)
                for (i64 xx = 0; xx < w; ++xx) {
                    i64 m = (i64)mag[off + yy * w + xx];
                    i64 b = sb_boff[s] + (yy >> 2) * nbw + (xx >> 2);
                    if (m > blkmax[b]) blkmax[b] = m;
                    i64 g = sb_goff[s] + ((yy >> 2) / GROUP) * ngw + (xx >> 2) / GROUP;
                    if (m > grpmax[g]) grpmax[g] = m;
                    if (m > sbmax[s]) sbmax[s] = m;
                }
        }
    }

    // returns true when out of room
    bool pass_clean(i64 p) {
        for (i64 s = 0; s < nsb; ++s) {
            i64 off = sb_off[s], h = sb_h[s], w = sb_w[s], cls = sb_cls[s], par = sb_par[s];
            i64 nbh = (h + 3) >> 2, nbw = (w + 3) >> 2;
            i64 ngh = (nbh + GROUP - 1) / GROUP, ngw = (nbw + GROUP - 1) / GROUP;
            if (sb_nblk[s] == 0) {
                if (!room(1)) return true;
                i64 bit = (mode == 0 && (sbmax[s] >> p) != 0) ? 1 : 0;
                bit = sym(CTX_SB + cls, bit);
                if (bit == 0) continue;
            }
            for (i64 gy = 0; gy < ngh; ++gy)
                for (i64 gx = 0; gx < ngw; ++gx) {
                    i64 gidx = sb_goff[s] + gy * ngw + gx;
                    if (grps[gidx] == 0) {
                        if (!room(1)) return true;
                        i64 gpf = 0;
                        if (par >= 0) {
                            i64 pngw = (((sb_w[par] + 3) >> 2) + GROUP - 1) / GROUP;
                            gpf = grps[sb_goff[par] + (gy >> 1) * pngw + (gx >> 1)];
                        }
                        i64 bit = (mode == 0 && (grpmax[gidx] >> p) != 0) ? 1 : 0;
                        bit = sym(CTX_GRP + cls * 2 + gpf, bit);
                        if (bit == 0) continue;
                        grps[gidx] = 1;
                    }
                    for (i64 by = gy * GROUP; by < imin(gy * GROUP + GROUP, nbh); ++by)
                        for (i64 bx = gx * GROUP; bx < imin(gx * GROUP + GROUP, nbw); ++bx) {
                            i64 bidx = sb_boff[s] + by * nbw + bx;
                            i64 y1 = imin(by * 4 + 4, h), x1 = imin(bx * 4 + 4, w);
                            if (blks[bidx] == 0) {
                                if (!room(1)) return true;
                                i64 pf = 0;
                                if (par >= 0) {
                                    i64 pnbw = (sb_w[par] + 3) >> 2;
                                    pf = blks[sb_boff[par] + (by >> 1) * pnbw + (bx >> 1)];
                                }
                                i64 nf = 0;
                                if (bx > 0 && blks[bidx - 1]) nf = 1;
                                if (by > 0 && blks[bidx - nbw]) nf = 1;
                                i64 bit = (mode == 0 && (blkmax[bidx] >> p) != 0) ? 1 : 0;
                                bit = sym(CTX_BLK + cls * 4 + pf * 2 + nf, bit);
                                if (bit == 0) continue;
                                blks[bidx] = 1;
                                sb_nblk[s] += 1;
                            }
                            for (i64 yy = by * 4; yy < y1; ++yy)
                                for (i64 xx = bx * 4; xx < x1; ++xx) {
                                    i64 j = off + yy * w + xx;
                                    if (sigp[j] >= 0 || vis[j] == p) continue;
                                    if (!code_sig(j, yy, xx, h, w, cls, par, p, s)) return true;
                                }
                        }
                }
        }
        return false;
    }

    bool pass_prop(i64 p) {
        for (i64 s = 0; s < nsb; ++s) {
            if (sb_nblk[s] == 0) continue;
            i64 off = sb_off[s], h = sb_h[s], w = sb_w[s], cls = sb_cls[s], par = sb_par[s];
            i64 nbw = (w + 3) >> 2;
            for (i64 yy = 0; yy < h; ++yy) {
                i64 brow = sb_boff[s] + (yy >> 2) * nbw;
                for (i64 bx = 0; bx < nbw; ++bx) {
                    if (blks[brow + bx] == 0) continue;
                    for (i64 xx = bx * 4; xx < imin(bx * 4 + 4, w); ++xx) {
                        i64 j = off + yy * w + xx;
                        if (sigp[j] >= 0) continue;
                        bool nb = false;
                        if (yy > 0 && sigp[j - w] >= 0) nb = true;
                        else if (yy < h - 1 && sigp[j + w] >= 0) nb = true;
                        else if (xx > 0 && sigp[j - 1] >= 0) nb = true;
                        else if (xx < w - 1 && sigp[j + 1] >= 0) nb = true;
                        else if (yy > 0 && xx > 0 && sigp[j - w - 1] >= 0) nb = true;
                        else if (yy > 0 && xx < w - 1 && sigp[j - w + 1] >= 0) nb = true;
                        else if (yy < h - 1 && xx > 0 && sigp[j + w - 1] >= 0) nb = true;
                        else if (yy < h - 1 && xx < w - 1 && sigp[j + w + 1] >= 0) nb = true;
                        else if (par >= 0 && sigp[sb_off[par] + (yy >> 1) * sb_w[par] + (xx >> 1)] >= 0) nb = true;
                        if (!nb) continue;
                        vis[j] = (int8_t)p;
                        if (!code_sig(j, yy, xx, h, w, cls, par, p, s)) return true;
                    }
                }
            }
        }
        return false;
    }

    bool pass_ref(i64 p) {
        for (i64 s = 0; s < nsb; ++s) {
            if (sb_nsig[s] == 0) continue;
            i64 off = sb_off[s], cls = sb_cls[s], h = sb_h[s], w = sb_w[s];
            i64 nbw = (w + 3) >> 2;
            for (i64 yy = 0; yy < h; ++yy) {
                i64 brow = sb_boff[s] + (yy >> 2) * nbw;
                for (i64 bx = 0; bx < nbw; ++bx) {
                    if (blks[brow + bx] == 0) continue;
                    for (i64 xx = bx * 4; xx < imin(bx * 4 + 4, w); ++xx) {
                        i64 j = off + yy * w + xx;
                        if (sigp[j] > p) {
                            if (!room(1)) return true;
                            i64 ctx = CTX_REF + cls * 2 + (sigp[j] == p + 1 ? 0 : 1);
                            i64 bit = mode == 0 ? (mag[j] >> p) & 1 : 0;
                            bit = sym(ctx, bit);
                            if (mode == 1) mag[j] |= (uint32_t)bit << p;
                            lastp[j] = (int8_t)p;
                        }
                    }
                }
            }
        }
        return false;
    }

    // wavelet_codec._code: returns bytes used (encoder) / position (decoder)
    i64 run(i64 top, i64 n_coef) {
        st = RC();
        sh = RC();
        if (mode == 1) {
            for (int i = 0; i < 4; ++i) {
                i64 b = i < blen ? buf[i] : 0;
                st.code = ((st.code << 8) | b) & 0xFFFFFFFFLL;
            }
            st.pos = 4;
        }
        enc_st = mode == 0 ? &st : &sh;
        for (int c = 0; c < N_CTX; ++c) { probs[c] = INIT_PROBS[c]; uses[c] = 0; }
        blks.assign(n_blocks, 0);
        grps.assign(n_groups, 0);
        blkmax.assign(n_blocks, 0);
        grpmax.assign(n_groups, 0);
        sbmax.assign(nsb, 0);
        if (mode == 0) maxima();
        vis.assign(n_coef, -1);
        sb_nsig.assign(nsb, 0);
        sb_nblk.assign(nsb, 0);
        for (i64 p = top; p >= 0; --p) {
            if (PASS_ORDER == 1) {
                if (pass_prop(p)) break;
                if (pass_ref(p)) break;
                if (pass_clean(p)) break;
            } else if (PASS_ORDER == 2) {
                if (pass_ref(p)) break;
                if (pass_clean(p)) break;
            } else {
                if (pass_clean(p)) break;
                if (pass_ref(p)) break;
            }
        }
        if (mode == 0) flush(st, buf, blen);
        return st.pos;
    }
};

// ---------------------------------------------------------------- the codec
struct Codec {
    i64 W, H, frame_bytes, leak_q, n_bands;
    bool adaptive, motion, block_motion;
    i64 seq = 0;
    i64 n_coef, n_blocks, n_groups, nsb;
    std::vector<i64> sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff, sb_goff;
    std::vector<i32> gidx, invw;
    std::vector<u8> coef_cls;
    std::vector<i64> coef_tile;
    std::vector<double> wflat, ana_scale;
    i64 geo[3][4];
    i64 plane_off[4];
    i64 nby, nbx;
    // reference: coefficients (ref) and/or snapped pixels (ref_pixels)
    std::vector<i32> ref;
    bool ref_valid = true;
    std::vector<u8> ref_pixels;
    std::vector<u8> last_picture;   // the three planes concatenated
    bool last_valid = true;
    std::vector<u8> prev_y;
    bool has_prev = false;
    std::map<i64, std::vector<u8>> phases;
    // scratch, kept between frames (fresh ~1 MB arrays each frame cost
    // allocation, zeroing and page faults -- a large share on a Pi)
    std::vector<i32> T, t, buf32;
    std::vector<double> X_;
    std::vector<i32> P_, newref_;
    std::vector<uint32_t> mag_;
    std::vector<u8> neg_;
    std::vector<int8_t> sigp_, lastp_;
    std::vector<u8> pics_, body_;
    Coder coder_;
    bool coder_ready = false;

    const std::vector<u8>& phase(i64 nb) {
        auto it = phases.find(nb);
        if (it != phases.end()) return it->second;
        std::vector<u8> ph(n_coef);
        i64 m = imax(1, nb);
        for (i64 i = 0; i < n_coef; ++i) ph[i] = (u8)(coef_tile[i] % m);
        return phases[nb] = ph;
    }

    void reset() {
        ref.assign(n_coef, 0);
        ref_valid = true;
        ref_pixels.clear();
        last_picture.assign(plane_off[3], 128);
        last_valid = true;
    }

    // whole pixels (3 planes) -> reference coefficients (wavelet_codec._pixels_to_ref)
    void pixels_to_ref(const std::vector<u8>& pics, std::vector<i32>& out) {
        buf32.resize(plane_off[3]);
        for (i64 i = 0; i < plane_off[3]; ++i) buf32[i] = ((i32)pics[i] - 128) << PIX_FRAC;
        for (int ci = 0; ci < 3; ++ci)
            dwt2_fwd(buf32.data() + geo[ci][3], geo[ci][0], geo[ci][1], geo[ci][2], T, t);
        out.resize(n_coef);
        for (i64 i = 0; i < n_coef; ++i) out[i] = buf32[gidx[i]];
    }
    // reference coefficients -> whole pixels (wavelet_codec._picture_kernel)
    void picture_kernel(const std::vector<i32>& r, std::vector<u8>& pics) {
        buf32.resize(plane_off[3]);
        for (i64 i = 0; i < n_coef; ++i) buf32[gidx[i]] = r[i];
        pics.resize(plane_off[3]);
        for (int ci = 0; ci < 3; ++ci) {
            i32* c = buf32.data() + geo[ci][3];
            dwt2_inv(c, geo[ci][0], geo[ci][1], geo[ci][2], T, t);
            to_pixels(c, geo[ci][0] * geo[ci][1], pics.data() + geo[ci][3]);
        }
    }
    const std::vector<i32>& get_ref() {
        if (!ref_valid) {
            pixels_to_ref(ref_pixels, ref);
            ref_valid = true;
            ref_pixels.clear();
        }
        return ref;
    }
    const std::vector<u8>& picture() {
        if (!last_valid) {
            picture_kernel(get_ref(), last_picture);
            last_valid = true;
        }
        return last_picture;
    }
    void round_trip(std::vector<i32>& new_ref, i64 nbytes) {
        i64 HH = geo[0][0], WW = geo[0][1];
        if ((double)(nbytes * 8) < SNAP_BITS_PER_PIXEL * (double)(HH * WW)) {
            picture_kernel(new_ref, last_picture);
            ref_pixels = last_picture;
            ref_valid = false;
            last_valid = true;
            return;
        }
        ref.swap(new_ref);
        ref_valid = true;
        ref_pixels.clear();
        last_valid = false;
    }
    void shift_ref(i64 hx, i64 hy) {
        std::vector<u8> pics = picture();
        std::vector<u8> out(plane_off[3]);
        for (int ci = 0; ci < 3; ++ci) {
            i64 sx = ci == 0 ? 2 * hx : hx, sy = ci == 0 ? 2 * hy : hy;
            shift_plane(pics.data() + plane_off[ci], geo[ci][0], geo[ci][1], sx, sy, out.data() + plane_off[ci]);
        }
        pixels_to_ref(out, ref);
        ref_valid = true;
        ref_pixels.clear();
        last_valid = false;
    }
    void warp_ref(const std::vector<i64>& fx, const std::vector<i64>& fy) {
        std::vector<u8> pics = picture();
        std::vector<u8> out(plane_off[3]);
        for (int ci = 0; ci < 3; ++ci)
            warp_plane(pics.data() + plane_off[ci], geo[ci][0], geo[ci][1], fx.data(), fy.data(), nby, nbx,
                       BLK, ci == 0 ? 1 : 2, out.data() + plane_off[ci]);
        pixels_to_ref(out, ref);
        ref_valid = true;
        ref_pixels.clear();
        last_valid = false;
    }

    void prediction(i64 lq, const i64* k, i64 band, i64 nb, std::vector<i32>& P) {
        const std::vector<i32>& r = get_ref();
        const std::vector<u8>& ph = phase(nb);
        i64 a_num[N_CLS];
        for (int c = 0; c < N_CLS; ++c) a_num[c] = lq * k[c];
        const i64 half = A_DEN / 2;
        P.resize(n_coef);
        for (i64 i = 0; i < n_coef; ++i) {
            if (nb > 0 && ph[i] == band) P[i] = 0;
            else {
                // Python's floor division by the constant A_DEN, one division
                // (floordiv's second one, for the remainder, was most of this loop)
                i64 x = (i64)r[i] * a_num[coef_cls[i]] + half;
                // floor division without a branch on the (random) sign:
                // x < 0 -> (x - (A_DEN - 1)) / A_DEN, truncating = floor
                P[i] = (i32)((x - ((x >> 63) & (A_DEN - 1))) / A_DEN);
            }
        }
    }
    void update_ref(const std::vector<i32>& P, const std::vector<uint32_t>& mag, const std::vector<u8>& neg,
                    const std::vector<int8_t>& sigp, const std::vector<int8_t>& lastp, std::vector<i32>& out) {
        const int sh = REC_FRAC + INVW_FRAC - PIX_FRAC;
        const i64 half = i64(1) << (sh - 1);
        out.resize(n_coef);
        for (i64 i = 0; i < n_coef; ++i) {
            if (sigp[i] >= 0) {
                i64 lp = lastp[i];
                i64 v = ((((i64)mag[i] >> lp) << lp) << REC_FRAC) + REC_BIAS_FX * ((i64(1) << lp) - 1);
                v = (v * (i64)invw[i] + half) >> sh;
                out[i] = (i32)(neg[i] ? (i64)P[i] - v : (i64)P[i] + v);
            } else {
                out[i] = P[i];
            }
        }
    }
    Coder& make_coder() {
        Coder& c = coder_;
        if (coder_ready) return c;
        coder_ready = true;
        c.nsb = nsb;
        c.sb_off = sb_off.data(); c.sb_h = sb_h.data(); c.sb_w = sb_w.data(); c.sb_cls = sb_cls.data();
        c.sb_par = sb_par.data(); c.sb_boff = sb_boff.data(); c.sb_goff = sb_goff.data();
        c.n_blocks = n_blocks; c.n_groups = n_groups;
        return c;
    }

    // ---- encoder
    int encode(const u8* y, const u8* u, const u8* v, u8* out) {
        Lap lap;
        i64 band = n_bands ? seq % n_bands : 0;
        // global motion
        i64 mdx = 0, mdy = 0;
        if (motion && has_prev) {
            i64 dx, dy;
            double cost, cost0;
            global_motion_fast(y, prev_y.data(), H, W, MC_RANGE, dx, dy, cost, cost0);
            if ((dx != 0 || dy != 0) && cost < MC_GAIN * cost0) { mdx = dx; mdy = dy; }
        }
        bool moved = mdx != 0 || mdy != 0;
        // block motion field
        std::vector<i64> vx, vy;
        bool field = false;
        if (block_motion && has_prev) {
            vx.assign(nby * nbx, 0);
            vy.assign(nby * nbx, 0);
            block_search_fast(y, prev_y.data(), H, W, mdx, mdy, BLK, BLK_RANGE, BLK_MIN_GAIN, BLK_MIN_ABS,
                              nby, nbx, vx, vy);
            for (i64 i = 0; i < nby * nbx; ++i)
                if (vx[i] != 0 || vy[i] != 0) { field = true; break; }
        }
        lap(0);
        std::vector<u8> fpk;
        if (field) {
            i64 nb = nby * nbx;
            std::vector<u8> data((nb + 7) / 8, 0);
            for (i64 i = 0; i < nb; ++i)
                if (vx[i] != 0 || vy[i] != 0) data[i >> 3] |= (u8)(0x80 >> (i & 7));
            for (i64 i = 0; i < nb; ++i)
                if (vx[i] != 0 || vy[i] != 0) data.push_back((u8)(((vx[i] & 0xF) << 4) | (vy[i] & 0xF)));
            fpk.push_back((u8)((data.size() >> 8) & 0xFF));
            fpk.push_back((u8)(data.size() & 0xFF));
            fpk.insert(fpk.end(), data.begin(), data.end());
            if ((double)fpk.size() > BLK_MAX_SHARE * (double)frame_bytes) { field = false; fpk.clear(); }
        }
        if (field) {
            std::vector<i64> fx(nby * nbx), fy(nby * nbx);
            for (i64 i = 0; i < nby * nbx; ++i) { fx[i] = vx[i] + mdx; fy[i] = vy[i] + mdy; }
            warp_ref(fx, fy);
        } else if (moved) {
            shift_ref(mdx, mdy);
        }
        lap(1);
        i64 hlen = HEADER_BYTES + (moved ? MOTION_BYTES : 0) + (i64)fpk.size();
        // analysis (weighted wavelet coefficients)
        buf32.resize(plane_off[3]);
        const u8* planes[3] = {y, u, v};
        for (int ci = 0; ci < 3; ++ci) {
            i64 n = geo[ci][0] * geo[ci][1];
            i32* c = buf32.data() + geo[ci][3];
            for (i64 i = 0; i < n; ++i) c[i] = ((i32)planes[ci][i] - 128) << PIX_FRAC;
            dwt2_fwd(c, geo[ci][0], geo[ci][1], geo[ci][2], T, t);
        }
        std::vector<double>& X = X_;
        X.resize(n_coef);
        for (i64 i = 0; i < n_coef; ++i) X[i] = (double)buf32[gidx[i]] * ana_scale[i];
        lap(2);
        // per-class prediction weights (least squares)
        i64 k[N_CLS];
        for (int c = 0; c < N_CLS; ++c) k[c] = WEIGHT_LEVELS;
        double cap = (double)leak_q / 256.0;
        if (adaptive && cap != 0) {
            double xr[N_CLS] = {0}, rr[N_CLS] = {0};
            const std::vector<i32>& r = get_ref();
            const std::vector<u8>& ph = phase(n_bands);
            const double inv = 1.0 / (1 << PIX_FRAC);
            for (i64 i = 0; i < n_coef; ++i) {
                if (n_bands > 0 && ph[i] == band) continue;
                double rv = (double)r[i] * inv * wflat[i];
                i64 c = coef_cls[i];
                xr[c] += X[i] * rv;
                rr[c] += rv * rv;
            }
            for (int c = 0; c < N_CLS; ++c) {
                double a = rr[c] > 0 ? xr[c] / rr[c] : 0.0;
                double q = std::rint(a / cap * WEIGHT_LEVELS);
                k[c] = (i64)(q < 0 ? 0 : (q > WEIGHT_LEVELS ? WEIGHT_LEVELS : q));
            }
        }
        lap(3);
        // prediction, residual, coding
        std::vector<i32>& P = P_;
        prediction(leak_q, k, band, n_bands, P);
        std::vector<uint32_t>& mag = mag_;
        std::vector<u8>& neg = neg_;
        mag.resize(n_coef);
        neg.resize(n_coef);
        const double inv = 1.0 / (1 << PIX_FRAC);
        i64 mx = 0;
        for (i64 i = 0; i < n_coef; ++i) {
            double q = std::rint(X[i] - (double)P[i] * inv * wflat[i]);
            // (no branch on the sign: it's random, so a branch mispredicted half the time)
            neg[i] = q < 0;
            i64 m = (i64)std::fabs(q);
            mag[i] = (uint32_t)m;
            if (m > mx) mx = m;
        }
        i64 top = 0;
        while ((mx >> (top + 1)) > 0) ++top;
        if (top >= (1 << TOP_BITS)) return -2;
        lap(4);
        i64 blen = frame_bytes - hlen;
        if (blen <= 0) return -3;
        std::memset(out, 0, (size_t)frame_bytes);
        u8* body = out + hlen;
        std::vector<int8_t>& sigp = sigp_;
        std::vector<int8_t>& lastp = lastp_;
        sigp.assign(n_coef, -1);
        lastp.assign(n_coef, -1);
        Coder& cd = make_coder();
        cd.mode = 0;
        cd.buf = body; cd.blen = blen; cd.limit = blen;
        cd.mag = mag.data(); cd.neg = neg.data(); cd.sigp = sigp.data(); cd.lastp = lastp.data();
        i64 nbytes = cd.run(top, n_coef);
        if (nbytes > blen) return -4;
        lap(5);
        update_ref(P, mag, neg, sigp, lastp, newref_);
        round_trip(newref_, frame_bytes);
        lap(6);
        // header
        uint32_t packed = (uint32_t)top;
        for (int c = 0; c < N_CLS; ++c) packed = (packed << WEIGHT_BITS) | (uint32_t)k[c];
        out[0] = (u8)(packed >> 24); out[1] = (u8)(packed >> 16); out[2] = (u8)(packed >> 8); out[3] = (u8)packed;
        out[4] = (u8)leak_q;
        out[5] = (u8)(band | (fpk.empty() ? 0 : BLOCK_FLAG));
        out[6] = (u8)(n_bands | (moved ? MOTION_FLAG : 0));
        i64 pos = 7;
        if (moved) { out[pos++] = (u8)(mdx & 0xFF); out[pos++] = (u8)(mdy & 0xFF); }
        for (u8 b : fpk) out[pos++] = b;
        if (motion || block_motion) {
            prev_y.assign(y, y + H * W);
            has_prev = true;
        }
        seq += 1;
        return (int)frame_bytes;
    }

    // ---- decoder: 0 ok, -1 malformed packet
    int decode(const u8* pkt, i64 len) {
        Lap lap;
        if (len < HEADER_BYTES) return -1;
        uint32_t packed = ((uint32_t)pkt[0] << 24) | ((uint32_t)pkt[1] << 16) | ((uint32_t)pkt[2] << 8) | pkt[3];
        i64 top = packed >> (N_CLS * WEIGHT_BITS);
        i64 k[N_CLS];
        for (int c = 0; c < N_CLS; ++c) k[c] = (packed >> (WEIGHT_BITS * (N_CLS - 1 - c))) & WEIGHT_LEVELS;
        i64 lq = pkt[4], band = pkt[5] & 0x7F, nb = pkt[6] & 0x7F;
        bool has_field = (pkt[5] & BLOCK_FLAG) != 0;
        if (nb && band >= nb) return -1;
        i64 hlen = HEADER_BYTES;
        i64 mdx = 0, mdy = 0;
        if (pkt[6] & MOTION_FLAG) {
            if (len < HEADER_BYTES + MOTION_BYTES) return -1;
            mdx = pkt[7] > 127 ? (i64)pkt[7] - 256 : pkt[7];
            mdy = pkt[8] > 127 ? (i64)pkt[8] - 256 : pkt[8];
            hlen += MOTION_BYTES;
            if ((mdx < 0 ? -mdx : mdx) > 2 * MC_RANGE || (mdy < 0 ? -mdy : mdy) > 2 * MC_RANGE || (mdx == 0 && mdy == 0))
                return -1;
        }
        bool field = false;
        std::vector<i64> fx, fy;
        if (has_field) {
            // [2-byte n][bitmap][one byte per flagged block]
            if (hlen + 2 > len) return -1;
            i64 n = ((i64)pkt[hlen] << 8) | pkt[hlen + 1];
            i64 nbk = nby * nbx;
            i64 nbm = (nbk + 7) / 8;
            if (n < nbm || hlen + 2 + n > len) return -1;
            const u8* bm = pkt + hlen + 2;
            const u8* vals = bm + nbm;
            i64 nvals = n - nbm, cnt = 0;
            for (i64 i = 0; i < nbk; ++i) if (bm[i >> 3] & (0x80 >> (i & 7))) ++cnt;
            if (cnt != nvals) return -1;
            fx.assign(nbk, 0);
            fy.assign(nbk, 0);
            i64 vi = 0;
            for (i64 i = 0; i < nbk; ++i)
                if (bm[i >> 3] & (0x80 >> (i & 7))) {
                    i64 a = vals[vi] >> 4, c = vals[vi] & 0xF;
                    ++vi;
                    fx[i] = (a > 7 ? a - 16 : a) + mdx;
                    fy[i] = (c > 7 ? c - 16 : c) + mdy;
                }
                else { fx[i] = mdx; fy[i] = mdy; }
            hlen = hlen + 2 + n;
            field = true;
        }
        if (field) warp_ref(fx, fy);
        else if (mdx != 0 || mdy != 0) shift_ref(mdx, mdy);
        lap(8);
        std::vector<u8>& body = body_;
        body.assign(pkt + hlen, pkt + len);
        std::vector<i32>& P = P_;
        prediction(lq, k, band, nb, P);
        lap(9);
        std::vector<uint32_t>& mag = mag_;
        std::vector<u8>& neg = neg_;
        std::vector<int8_t>& sigp = sigp_;
        std::vector<int8_t>& lastp = lastp_;
        mag.assign(n_coef, 0);
        neg.assign(n_coef, 0);
        sigp.assign(n_coef, -1);
        lastp.assign(n_coef, -1);
        Coder& cd = make_coder();
        cd.mode = 1;
        cd.buf = body.data(); cd.blen = (i64)body.size(); cd.limit = (i64)body.size();
        cd.mag = mag.data(); cd.neg = neg.data(); cd.sigp = sigp.data(); cd.lastp = lastp.data();
        cd.run(top, n_coef);
        lap(10);
        update_ref(P, mag, neg, sigp, lastp, newref_);
        round_trip(newref_, len);
        lap(11);
        return 0;
    }
};

}  // namespace

// ---------------------------------------------------------------- C interface
WVT_API void* wvt_create(i64 W, i64 H, i64 frame_bytes, i64 leak_q, i64 n_bands, int adaptive, int motion,
                         int block_motion, i64 n_coef, i64 n_blocks, i64 n_groups, i64 nsb,
                         const i64* sb_off, const i64* sb_h, const i64* sb_w, const i64* sb_cls, const i64* sb_par,
                         const i64* sb_boff, const i64* sb_goff, const i64* gidx, const double* wflat,
                         const i64* invw, const double* ana_scale, const i64* coef_cls, const i64* coef_tile,
                         const i64* geo) {
    Codec* c = new Codec();
    c->W = W; c->H = H; c->frame_bytes = frame_bytes; c->leak_q = leak_q; c->n_bands = n_bands;
    c->adaptive = adaptive != 0; c->motion = motion != 0; c->block_motion = block_motion != 0;
    c->n_coef = n_coef; c->n_blocks = n_blocks; c->n_groups = n_groups; c->nsb = nsb;
    c->sb_off.assign(sb_off, sb_off + nsb); c->sb_h.assign(sb_h, sb_h + nsb); c->sb_w.assign(sb_w, sb_w + nsb);
    c->sb_cls.assign(sb_cls, sb_cls + nsb); c->sb_par.assign(sb_par, sb_par + nsb);
    c->sb_boff.assign(sb_boff, sb_boff + nsb); c->sb_goff.assign(sb_goff, sb_goff + nsb);
    c->gidx.assign(gidx, gidx + n_coef); c->wflat.assign(wflat, wflat + n_coef); c->invw.assign(invw, invw + n_coef);   // (narrowed: all small)
    c->ana_scale.assign(ana_scale, ana_scale + n_coef);
    c->coef_cls.resize(n_coef);
    for (i64 i = 0; i < n_coef; ++i) c->coef_cls[i] = (u8)coef_cls[i];
    c->coef_tile.assign(coef_tile, coef_tile + n_coef);
    for (int i = 0; i < 3; ++i) for (int j = 0; j < 4; ++j) c->geo[i][j] = geo[i * 4 + j];
    c->plane_off[0] = 0;
    for (int i = 0; i < 3; ++i) c->plane_off[i + 1] = c->plane_off[i] + c->geo[i][0] * c->geo[i][1];
    c->nby = (H + BLK - 1) / BLK;
    c->nbx = (W + BLK - 1) / BLK;
    c->reset();
    return c;
}
WVT_API void wvt_free(void* h) { delete (Codec*)h; }
WVT_API void wvt_reset(void* h) { ((Codec*)h)->reset(); }
WVT_API void wvt_set_seq(void* h, i64 seq) { ((Codec*)h)->seq = seq; }
WVT_API i64 wvt_get_seq(void* h) { return ((Codec*)h)->seq; }
// returns frame_bytes, or < 0 on an internal error
WVT_API int wvt_encode(void* h, const u8* y, const u8* u, const u8* v, u8* out) {
    return ((Codec*)h)->encode(y, u, v, out);
}
// 0 ok (the picture is then in y, u, v), -1 malformed packet
WVT_API int wvt_decode(void* h, const u8* pkt, i64 len, u8* y, u8* u, u8* v) {
    Codec* c = (Codec*)h;
    int r = c->decode(pkt, len);
    if (r != 0) return r;
    Lap lap;
    const std::vector<u8>& p = c->picture();
    lap(12);
    std::memcpy(y, p.data() + c->plane_off[0], (size_t)(c->plane_off[1] - c->plane_off[0]));
    std::memcpy(u, p.data() + c->plane_off[1], (size_t)(c->plane_off[2] - c->plane_off[1]));
    std::memcpy(v, p.data() + c->plane_off[2], (size_t)(c->plane_off[3] - c->plane_off[2]));
    return 0;
}
WVT_API void wvt_picture(void* h, u8* y, u8* u, u8* v) {
    Codec* c = (Codec*)h;
    const std::vector<u8>& p = c->picture();
    std::memcpy(y, p.data() + c->plane_off[0], (size_t)(c->plane_off[1] - c->plane_off[0]));
    std::memcpy(u, p.data() + c->plane_off[1], (size_t)(c->plane_off[2] - c->plane_off[1]));
    std::memcpy(v, p.data() + c->plane_off[2], (size_t)(c->plane_off[3] - c->plane_off[2]));
}

// per-stage seconds since the last call (see g_prof), then cleared
WVT_API void wvt_profile(double* out) {
    for (int i = 0; i < 16; ++i) { out[i] = g_prof[i]; g_prof[i] = 0; }
}

// ======================================================================
// wvt_create_auto: the codec with its layout made here instead of handed in
// by wavelet_codec.py (_Layout, WaveletCodec.__init__) -- for programs with
// no Python (the C++ GUI's video). Same tables bit for bit: the subband
// weights from the float inverse DWT of an impulse, summed as numpy does
// (pairwise), and the refresh-tile order from numpy's legacy RandomState
// (MT19937) permutation. Checked by test_wvt_auto.py (Python-encoded frames
// decode to identical pictures).
namespace {

const double FA = -1.586134342059924, FB = -0.052980118572961, FG = 0.882911075530934,
             FD = 0.443506852043971, FK = 1.230174104914001;
const double CHROMA_WEIGHT = 0.6, Q_SCALE = 2.0;
const int REFRESH_TILE = 16;
const uint32_t REFRESH_SEED = 0x5754;

// _inv_axis0 along a strided line of 2n values
void inv_line(double* x, int n2, int stride) {
    int n = n2 / 2;
    std::vector<double> s(n), d(n), sn(n), dp(n);
    const double ik = 1.0 / FK;
    for (int i = 0; i < n; ++i) { s[i] = x[(size_t)i * stride] * FK; d[i] = x[(size_t)(n + i) * stride] * ik; }
    for (int i = 0; i < n; ++i) dp[i] = d[i > 0 ? i - 1 : 0];
    for (int i = 0; i < n; ++i) s[i] -= FD * (dp[i] + d[i]);
    for (int i = 0; i < n; ++i) sn[i] = s[i + 1 < n ? i + 1 : n - 1];
    for (int i = 0; i < n; ++i) d[i] -= FG * (s[i] + sn[i]);
    for (int i = 0; i < n; ++i) dp[i] = d[i > 0 ? i - 1 : 0];
    for (int i = 0; i < n; ++i) s[i] -= FB * (dp[i] + d[i]);
    for (int i = 0; i < n; ++i) sn[i] = s[i + 1 < n ? i + 1 : n - 1];
    for (int i = 0; i < n; ++i) d[i] -= FA * (s[i] + sn[i]);
    for (int i = 0; i < n; ++i) { x[(size_t)(2 * i) * stride] = s[i]; x[(size_t)(2 * i + 1) * stride] = d[i]; }
}

// idwt2 in place on an H x W row-major image
void idwt2_f(std::vector<double>& c, int H, int W, int levels) {
    for (int l = levels - 1; l >= 0; --l) {
        int h = H >> l, w = W >> l;
        for (int y = 0; y < h; ++y) inv_line(&c[(size_t)y * W], w, 1);          // along x (the .T pass)
        for (int x = 0; x < w; ++x) inv_line(&c[x], h, W);                       // along y
    }
}

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

struct Sub { int y0, x0, h, w, lev; };
std::vector<Sub> component_subbands(int H, int W, int levels) {
    std::vector<Sub> out{{0, 0, H >> levels, W >> levels, 0}};
    for (int l = levels; l > 0; --l) {
        int h = H >> l, w = W >> l;
        out.push_back({0, w, h, w, l});
        out.push_back({h, 0, h, w, l});
        out.push_back({h, w, h, w, l});
    }
    return out;
}

// numpy RandomState(seed).permutation(n): MT19937 (init_genrand), legacy shuffle
std::vector<i64> legacy_permutation(uint32_t seed, i64 n) {
    uint32_t mt[624];
    mt[0] = seed;
    for (int i = 1; i < 624; ++i) mt[i] = 1812433253u * (mt[i - 1] ^ (mt[i - 1] >> 30)) + (uint32_t)i;
    int pos = 624;
    auto next32 = [&]() {
        if (pos >= 624) {
            for (int k = 0; k < 624; ++k) {
                uint32_t y = (mt[k] & 0x80000000u) | (mt[(k + 1) % 624] & 0x7fffffffu);
                mt[k] = mt[(k + 397) % 624] ^ (y >> 1) ^ ((y & 1u) ? 0x9908b0dfu : 0u);
            }
            pos = 0;
        }
        uint32_t y = mt[pos++];
        y ^= y >> 11;
        y ^= (y << 7) & 0x9d2c5680u;
        y ^= (y << 15) & 0xefc60000u;
        y ^= y >> 18;
        return y;
    };
    std::vector<i64> a(n);
    for (i64 i = 0; i < n; ++i) a[i] = i;
    for (i64 i = n - 1; i >= 1; --i) {
        uint64_t mx = (uint64_t)i, mask = mx, v;
        mask |= mask >> 1; mask |= mask >> 2; mask |= mask >> 4; mask |= mask >> 8; mask |= mask >> 16;
        while ((v = (next32() & mask)) > mx) {}
        std::swap(a[i], a[(i64)v]);
    }
    return a;
}

}  // namespace

// The tables wavelet_codec.WaveletCodec(width, height, frame_bytes, leak,
// refresh_frames, adaptive, motion, block_motion) makes. block_motion < 0:
// as motion. Null if the size isn't a multiple of 32x16.
WVT_API void* wvt_create_auto(i64 W0, i64 H0, i64 frame_bytes, double leak, i64 refresh_frames, int adaptive,
                              int motion, int block_motion) {
    if (W0 % 32 || H0 % 16 || W0 <= 0 || H0 <= 0) return nullptr;
    struct Comp { int H, W, L; double cw; int cls0; };
    Comp comps[3] = {{(int)H0, (int)W0, 4, 1.0, 0}, {(int)H0 / 2, (int)W0 / 2, 3, CHROMA_WEIGHT, 5},
                     {(int)H0 / 2, (int)W0 / 2, 3, CHROMA_WEIGHT, 5}};
    struct Band { int ci, y0, x0, h, w, lev, L, orient, cls; double wt; i64 size, off = 0, boff = 0, goff = 0; int par = -1; };
    std::vector<Band> bands;
    for (int ci = 0; ci < 3; ++ci) {
        const Comp& cp = comps[ci];
        std::vector<Sub> subs = component_subbands(cp.H, cp.W, cp.L);
        for (size_t bi = 0; bi < subs.size(); ++bi) {
            const Sub& sb = subs[bi];
            // _subband_weights: L2 norm of the synthesis of an impulse at the band's centre
            std::vector<double> c((size_t)cp.H * cp.W, 0.0);
            c[(size_t)(sb.y0 + sb.h / 2) * cp.W + sb.x0 + sb.w / 2] = 1.0;
            idwt2_f(c, cp.H, cp.W, cp.L);
            for (double& v : c) v = v * v;
            double wt = std::sqrt(np_pairwise(c.data(), (long long)c.size()));
            Band b;
            b.ci = ci; b.y0 = sb.y0; b.x0 = sb.x0; b.h = sb.h; b.w = sb.w; b.lev = sb.lev; b.L = cp.L;
            b.orient = sb.lev == 0 ? 0 : (int)((bi - 1) % 3);
            b.cls = sb.lev == 0 ? cp.cls0 : cp.cls0 + 1 + (cp.L - sb.lev);
            b.wt = wt * cp.cw;
            b.size = (i64)sb.h * sb.w;
            bands.push_back(b);
        }
    }
    std::stable_sort(bands.begin(), bands.end(), [](const Band& a, const Band& b) {
        int la = -(a.lev == 0), lb = -(b.lev == 0);
        if (la != lb) return la < lb;
        if (a.size != b.size) return a.size < b.size;
        if (a.ci != b.ci) return a.ci < b.ci;
        return a.orient < b.orient;
    });
    i64 off = 0, boff = 0, goff = 0;
    for (Band& b : bands) {
        b.off = off; b.boff = boff; b.goff = goff;
        off += b.size;
        i64 nbh = (b.h + 3) >> 2, nbw = (b.w + 3) >> 2;
        boff += nbh * nbw;
        goff += ((nbh + GROUP - 1) / GROUP) * ((nbw + GROUP - 1) / GROUP);
    }
    for (Band& b : bands)
        if (b.lev && b.lev < b.L)
            for (size_t pi = 0; pi < bands.size(); ++pi)
                if (bands[pi].ci == b.ci && bands[pi].lev == b.lev + 1 && bands[pi].orient == b.orient) b.par = (int)pi;
    const i64 n_coef = off, nsb = (i64)bands.size();
    std::vector<i64> sb_off, sb_h, sb_w, sb_cls, sb_par, sb_boff, sb_goff;
    for (const Band& b : bands) {
        sb_off.push_back(b.off); sb_h.push_back(b.h); sb_w.push_back(b.w); sb_cls.push_back(b.cls);
        sb_par.push_back(b.par); sb_boff.push_back(b.boff); sb_goff.push_back(b.goff);
    }
    // refresh tiles
    int ty = (int)H0 / REFRESH_TILE, tx = (int)W0 / REFRESH_TILE;
    std::vector<i64> rank = legacy_permutation(REFRESH_SEED, (i64)ty * tx);
    std::vector<i64> coef_cls(n_coef), coef_tile(n_coef), gidx(n_coef), invw(n_coef);
    std::vector<double> wflat(n_coef), ana(n_coef);
    i64 plane_off[4] = {0, 0, 0, 0};
    for (int ci = 0; ci < 3; ++ci) plane_off[ci + 1] = plane_off[ci] + (i64)comps[ci].H * comps[ci].W;
    for (const Band& b : bands) {
        int s = (b.lev == 0 ? b.L : b.lev) + (b.ci ? 1 : 0);
        int CW = comps[b.ci].W;
        for (int yy = 0; yy < b.h; ++yy)
            for (int xx = 0; xx < b.w; ++xx) {
                i64 i = b.off + (i64)yy * b.w + xx;
                coef_cls[i] = b.cls;
                coef_tile[i] = rank[(i64)(((i64)yy << s) / REFRESH_TILE) * tx + (((i64)xx << s) / REFRESH_TILE)];
                gidx[i] = plane_off[b.ci] + (i64)(b.y0 + yy) * CW + (b.x0 + xx);
                wflat[i] = b.wt * Q_SCALE;
                invw[i] = (i64)std::nearbyint((double)(1 << INVW_FRAC) / wflat[i]);
                ana[i] = (1.0 / (1 << PIX_FRAC)) * wflat[i];
            }
    }
    i64 geo[12];
    for (int ci = 0; ci < 3; ++ci) {
        geo[ci * 4] = comps[ci].H; geo[ci * 4 + 1] = comps[ci].W; geo[ci * 4 + 2] = comps[ci].L; geo[ci * 4 + 3] = plane_off[ci];
    }
    i64 leak_q = (i64)std::min(255.0, std::max(0.0, std::nearbyint(leak * 256)));
    i64 n_bands = refresh_frames ? std::max<i64>(1, std::min<i64>(refresh_frames, 127)) : 0;
    int bm = block_motion < 0 ? motion : block_motion;
    return wvt_create(W0, H0, frame_bytes, leak_q, n_bands, adaptive, motion, bm, n_coef, boff, goff, nsb,
                      sb_off.data(), sb_h.data(), sb_w.data(), sb_cls.data(), sb_par.data(), sb_boff.data(),
                      sb_goff.data(), gidx.data(), wflat.data(), invw.data(), ana.data(), coef_cls.data(),
                      coef_tile.data(), geo);
}

// a codec's picture size
WVT_API void wvt_size(void* h, i64* w, i64* hh) {
    Codec* c = (Codec*)h;
    *w = c->W;
    *hh = c->H;
}
