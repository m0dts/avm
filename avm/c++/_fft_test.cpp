
#include "fft.h"
#if defined(_WIN32)
#define API extern "C" __declspec(dllexport)
#else
#define API extern "C"
#endif
API void fwd_d(const std::complex<double>* in, std::complex<double>* out, int n, int inv) {
    auto& p = fft::plan<double>(n); if (inv) p.inverse(in, out); else p.forward(in, out);
}
API void fwd_f(const std::complex<float>* in, std::complex<float>* out, int n, int inv) {
    auto& p = fft::plan<float>(n); if (inv) p.inverse(in, out); else p.forward(in, out);
}
