// fft.h -- complex FFT of any length, for the modem. Stockham autosort
// (no bit reversal; each pass reads and writes contiguous runs, with a
// per-pass twiddle table), radix 4, 2, 3, 5 butterflies and a generic one
// for other primes up to MAX_DIRECT_PRIME; Bluestein's method beyond.
// Unscaled forward (exp(-i...)) and inverse, like numpy's fft/ifft without
// ifft's 1/n -- callers scale. Plans are cached per length. Not
// thread-safe per plan (scratch buffers). Checked against numpy by
// test_fft.py.
#pragma once
#include <algorithm>
#include <cmath>
#include <complex>
#include <map>
#include <memory>
#include <mutex>
#include <vector>

#include "per_thread.h"

namespace fft {

const int MAX_DIRECT_PRIME = 101;   // beyond this Bluestein (direct is O(p) per point)
const double PI = 3.14159265358979323846;

template <typename T>
struct Plan {
    using C = std::complex<T>;
    struct Pass {
        int r, m, s;            // radix, sub-length/r, stride
        std::vector<C> tw;      // W_(r*m)^(p*u), [p*(r-1) + u-1]
        std::vector<C> root;    // W_r^j (generic radix)
    };
    int n = 0;
    std::vector<Pass> passes;
    std::vector<C> buf, scratch;
    // Bluestein
    bool blue = false;
    std::vector<C> chirp, bfft, work, work2;
    std::shared_ptr<Plan<T>> sub;

    static int largest_prime(int n) {
        int m = 1;
        for (int d = 2; (long long)d * d <= n; ++d)
            while (n % d == 0) { m = d; n /= d; }
        return n > 1 ? (n > m ? n : m) : m;
    }
    static C cis(double a) { return C((T)std::cos(a), (T)std::sin(a)); }
    // plain complex multiply: std::complex's operator* checks for inf/NaN
    // (a library call per product without -ffast-math)
    static inline C mul(C a, C b) {
        return C(a.real() * b.real() - a.imag() * b.imag(), a.real() * b.imag() + a.imag() * b.real());
    }

    explicit Plan(int n_) : n(n_) {
        if (n > 1 && largest_prime(n) > MAX_DIRECT_PRIME) {
            blue = true;
            int m = 1;
            while (m < 2 * n - 1) m <<= 1;
            sub = std::make_shared<Plan<T>>(m);
            chirp.resize(n);
            for (int k = 0; k < n; ++k) {
                long long k2 = ((long long)k * k) % (2LL * n);   // exact phase
                chirp[k] = cis(-PI * (double)k2 / n);
            }
            std::vector<C> b(m, C(0, 0));
            b[0] = std::conj(chirp[0]);
            for (int k = 1; k < n; ++k) b[k] = b[m - k] = std::conj(chirp[k]);
            bfft.resize(m);
            sub->forward(b.data(), bfft.data());
            work.resize(m);
            work2.resize(m);
            return;
        }
        std::vector<int> radices;
        int r = n;
        while (r % 4 == 0) { radices.push_back(4); r /= 4; }
        if (r % 2 == 0) { radices.push_back(2); r /= 2; }
        for (int p = 3; r > 1; p += 2)
            while (r % p == 0) { radices.push_back(p); r /= p; }
        int len = n, s = 1, maxr = 1;
        for (int rad : radices) {
            Pass ps;
            ps.r = rad;
            ps.m = len / rad;
            ps.s = s;
            ps.tw.resize((size_t)ps.m * (rad - 1));
            for (int p = 0; p < ps.m; ++p)
                for (int u = 1; u < rad; ++u)
                    ps.tw[(size_t)p * (rad - 1) + u - 1] = cis(-2.0 * PI * (double)((long long)p * u) / len);
            if (rad > 5) {
                ps.root.resize(rad);
                for (int j = 0; j < rad; ++j) ps.root[j] = cis(-2.0 * PI * j / rad);
            }
            passes.push_back(std::move(ps));
            maxr = std::max(maxr, rad);
            len /= rad;
            s *= rad;
        }
        buf.resize(n);
        scratch.resize(2 * maxr);
    }

    // one pass: x (r*m blocks of s) -> y; y[q + s*(r*p + u)] = (DFT_r a)_u * W^(p*u),
    // a_t = x[q + s*(p + t*m)]
    void pass(const Pass& P, const C* x, C* y) {
        const int r = P.r, m = P.m, s = P.s;
        const size_t sm = (size_t)s * m;
        for (int p = 0; p < m; ++p) {
            const C* w = P.tw.data() + (size_t)p * (r - 1);
            const C* xp = x + (size_t)s * p;
            C* yp = y + (size_t)s * r * p;
            if (r == 4) {
                const C w1 = w[0], w2 = w[1], w3 = w[2];
                for (int q = 0; q < s; ++q) {
                    C a0 = xp[q], a1 = xp[q + sm], a2 = xp[q + 2 * sm], a3 = xp[q + 3 * sm];
                    C t0 = a0 + a2, t1 = a0 - a2, t2 = a1 + a3, d = a1 - a3;
                    C t3(d.imag(), -d.real());      // -i * (a1 - a3)
                    yp[q] = t0 + t2;
                    yp[q + s] = mul(t1 + t3, w1);
                    yp[q + 2 * s] = mul(t0 - t2, w2);
                    yp[q + 3 * s] = mul(t1 - t3, w3);
                }
            } else if (r == 2) {
                const C w1 = w[0];
                for (int q = 0; q < s; ++q) {
                    C a0 = xp[q], a1 = xp[q + sm];
                    yp[q] = a0 + a1;
                    yp[q + s] = mul(a0 - a1, w1);
                }
            } else if (r == 3) {
                const T sn = (T)-0.86602540378443864676;   // sin(-2 pi / 3)
                const C w1 = w[0], w2 = w[1];
                for (int q = 0; q < s; ++q) {
                    C a0 = xp[q], a1 = xp[q + sm], a2 = xp[q + 2 * sm];
                    C t1 = a1 + a2, t2 = a0 - t1 * (T)0.5, d = (a1 - a2) * sn;
                    C x1(t2.real() - d.imag(), t2.imag() + d.real());
                    C x2(t2.real() + d.imag(), t2.imag() - d.real());
                    yp[q] = a0 + t1;
                    yp[q + s] = mul(x1, w1);
                    yp[q + 2 * s] = mul(x2, w2);
                }
            } else if (r == 5) {
                const T c1 = (T)0.30901699437494742410, c2 = (T)-0.80901699437494742410;   // cos 2pi/5, 4pi/5
                const T s1 = (T)-0.95105651629515357212, s2 = (T)-0.58778525229247312917;  // sin -2pi/5, -4pi/5
                for (int q = 0; q < s; ++q) {
                    C a0 = xp[q], a1 = xp[q + sm], a2 = xp[q + 2 * sm], a3 = xp[q + 3 * sm], a4 = xp[q + 4 * sm];
                    C p1 = a1 + a4, m1 = a1 - a4, p2 = a2 + a3, m2 = a2 - a3;
                    C A1 = a0 + p1 * c1 + p2 * c2, A2 = a0 + p1 * c2 + p2 * c1;
                    C B1 = m1 * s1 + m2 * s2, B2 = m1 * s2 - m2 * s1;   // X_u = A_u + i B_u
                    yp[q] = a0 + p1 + p2;
                    yp[q + s] = mul(C(A1.real() - B1.imag(), A1.imag() + B1.real()), w[0]);
                    yp[q + 2 * s] = mul(C(A2.real() - B2.imag(), A2.imag() + B2.real()), w[1]);
                    yp[q + 3 * s] = mul(C(A2.real() + B2.imag(), A2.imag() - B2.real()), w[2]);
                    yp[q + 4 * s] = mul(C(A1.real() + B1.imag(), A1.imag() - B1.real()), w[3]);
                }
            } else {
                const int h = (r - 1) / 2;
                C* a = scratch.data();
                for (int q = 0; q < s; ++q) {
                    C dc(0, 0);
                    for (int t = 0; t < r; ++t) { a[t] = xp[q + t * sm]; dc += a[t]; }
                    yp[q] = dc;
                    for (int u = 1; u <= h; ++u) {
                        T ar = a[0].real(), ai = a[0].imag(), br = 0, bi = 0;
                        int j = 0;
                        for (int t = 1; t <= h; ++t) {
                            j += u; if (j >= r) j -= r;
                            const C wr = P.root[j];
                            C sp = a[t] + a[r - t], sd = a[t] - a[r - t];
                            ar += sp.real() * wr.real(); ai += sp.imag() * wr.real();
                            br += sd.imag() * wr.imag(); bi -= sd.real() * wr.imag();
                        }
                        yp[q + (size_t)u * s] = mul(C(ar - br, ai - bi), w[u - 1]);
                        yp[q + (size_t)(r - u) * s] = mul(C(ar + br, ai + bi), w[r - u - 1]);
                    }
                }
            }
        }
    }

    void forward(const C* in, C* out) {
        if (n <= 1) { if (n == 1) out[0] = in[0]; return; }
        if (blue) {
            int m = sub->n;
            for (int k = 0; k < n; ++k) work[k] = mul(in[k], chirp[k]);
            std::fill(work.begin() + n, work.end(), C(0, 0));
            sub->forward(work.data(), work2.data());
            for (int k = 0; k < m; ++k) work2[k] = std::conj(mul(work2[k], bfft[k]));
            sub->forward(work2.data(), work.data());     // conj(fft(conj(.))) = m * ifft
            T inv = (T)(1.0 / m);
            for (int k = 0; k < n; ++k) out[k] = mul(std::conj(work[k]) * inv, chirp[k]);
            return;
        }
        // ping-pong between out and buf, ending in out
        int np = (int)passes.size();
        C* a = (np % 2) ? buf.data() : out;
        C* b = (np % 2) ? out : buf.data();
        if (in != a) std::copy(in, in + n, a);
        for (int i = 0; i < np; ++i) {
            pass(passes[i], a, b);
            std::swap(a, b);
        }
    }
    // unscaled inverse
    void inverse(const C* in, C* out) {
        if (blue) {
            std::vector<C> tmp(n);
            for (int k = 0; k < n; ++k) tmp[k] = std::conj(in[k]);
            forward(tmp.data(), out);
        } else {
            for (int k = 0; k < n; ++k) out[k] = std::conj(in[k]);
            forward(out, out);
        }
        for (int k = 0; k < n; ++k) out[k] = std::conj(out[k]);
    }
};

// one cache per thread: a Plan works in its own buffers (buf, scratch, work),
// so a transmitter and a receiver in one process (the GUI) sharing one plan of
// the same size corrupted each other's transforms -- the receiver now and then
// missed a fragment's sync, silently
// (per_thread.h: a thread_local map crashed MinGW builds at thread exit)
template <typename T>
Plan<T>& plan(int n) {
    auto& cache = per_thread<std::map<int, std::unique_ptr<Plan<T>>>>();
    auto it = cache.find(n);
    if (it == cache.end()) it = cache.emplace(n, std::make_unique<Plan<T>>(n)).first;
    return *it->second;
}

}  // namespace fft
