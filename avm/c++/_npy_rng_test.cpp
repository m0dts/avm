
#include "npy_rng.h"
#include <cstring>
#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif
API void ints(unsigned long long seed, long long lo, long long hi, long long n, long long* out) {
    npy::Generator g(seed); auto v = g.integers(lo, hi, n); std::memcpy(out, v.data(), 8 * n);
}
API void rnd(unsigned long long seed, long long n, double* out) {
    npy::Generator g(seed); auto v = g.random(n); std::memcpy(out, v.data(), 8 * n);
}
API void perm(unsigned long long seed, long long n, long long* out) {
    npy::Generator g(seed); auto v = g.permutation(n); std::memcpy(out, v.data(), 8 * n);
}
