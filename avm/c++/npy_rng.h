// npy_rng.h -- an exact copy of numpy's default random generator, as AVM
// uses it: np.random.default_rng(seed) (SeedSequence -> PCG64), then
// .integers(low, high, n) (int64; Lemire's method on 32-bit draws for small
// ranges), .random(n) and .permutation(n). AVM's interleaver, pilots,
// preamble and scramblers are drawn from it, so a C++ modem must draw the
// very same numbers. (numpy/random: bit_generator.pyx SeedSequence,
// pcg64.h, distributions.c random_interval / buffered_bounded_lemire_uint32,
// _generator.pyx shuffle.) Checked against numpy by test_npy_rng.py.
#pragma once
#include <cstdint>
#include <vector>

namespace npy {

// ---- SeedSequence (pool size 4) -> generate_state
inline std::vector<uint32_t> seed_sequence_state(uint64_t entropy, int n_words32) {
    const uint32_t INIT_A = 0x43b0d7e5u, MULT_A = 0x931e8875u, INIT_B = 0x8b51f9ddu, MULT_B = 0x58f38dedu;
    const uint32_t MIX_MULT_L = 0xca01f9ddu, MIX_MULT_R = 0x4973f715u;
    const int XSHIFT = 16, POOL = 4;
    // the entropy as little-endian 32-bit words (at least one)
    std::vector<uint32_t> ent;
    do { ent.push_back((uint32_t)(entropy & 0xFFFFFFFFu)); entropy >>= 32; } while (entropy);
    uint32_t hc = INIT_A;
    auto hashmix = [&](uint32_t v) {
        v ^= hc;
        hc *= MULT_A;
        v *= hc;
        v ^= v >> XSHIFT;
        return v;
    };
    auto mix = [&](uint32_t x, uint32_t y) {
        uint32_t r = MIX_MULT_L * x - MIX_MULT_R * y;
        r ^= r >> XSHIFT;
        return r;
    };
    uint32_t pool[POOL];
    for (int i = 0; i < POOL; ++i) pool[i] = hashmix(i < (int)ent.size() ? ent[i] : 0);
    for (int s = 0; s < POOL; ++s)
        for (int d = 0; d < POOL; ++d)
            if (s != d) pool[d] = mix(pool[d], hashmix(pool[s]));
    for (int s = POOL; s < (int)ent.size(); ++s)
        for (int d = 0; d < POOL; ++d) pool[d] = mix(pool[d], hashmix(ent[s]));
    std::vector<uint32_t> out(n_words32);
    uint32_t hb = INIT_B;
    for (int i = 0; i < n_words32; ++i) {
        uint32_t v = pool[i % POOL];
        v ^= hb;
        hb *= MULT_B;
        v *= hb;
        v ^= v >> XSHIFT;
        out[i] = v;
    }
    return out;
}

// ---- PCG64 (XSL-RR 128/64), numpy's seeding
struct PCG64 {
    unsigned __int128 state = 0, inc = 0;
    bool has32 = false;
    uint32_t buf32 = 0;
    static unsigned __int128 mult() {
        return ((unsigned __int128)2549297995355413924ULL << 64) | 4865540595714422341ULL;
    }
    void step() { state = state * mult() + inc; }
    explicit PCG64(uint64_t seed) {
        std::vector<uint32_t> w = seed_sequence_state(seed, 8);   // 4 uint64 = 8 words
        uint64_t v[4];
        for (int i = 0; i < 4; ++i) v[i] = (uint64_t)w[2 * i] | ((uint64_t)w[2 * i + 1] << 32);
        unsigned __int128 s = ((unsigned __int128)v[0] << 64) | v[1];
        unsigned __int128 q = ((unsigned __int128)v[2] << 64) | v[3];
        state = 0;
        inc = (q << 1) | 1;
        step();
        state += s;
        step();
    }
    uint64_t next64() {
        step();
        uint64_t hi = (uint64_t)(state >> 64), lo = (uint64_t)state;
        unsigned rot = (unsigned)(state >> 122);
        uint64_t x = hi ^ lo;
        return (x >> rot) | (x << ((64 - rot) & 63));
    }
    uint32_t next32() {
        if (has32) { has32 = false; return buf32; }
        uint64_t n = next64();
        has32 = true;
        buf32 = (uint32_t)(n >> 32);
        return (uint32_t)n;
    }
    double next_double() { return (double)(next64() >> 11) * (1.0 / 9007199254740992.0); }
};

struct Generator {
    PCG64 bg;
    explicit Generator(uint64_t seed) : bg(seed) {}
    // integers(low, high, n) for a range under 2^32 (AVM's: 2 or 4 values)
    std::vector<int64_t> integers(int64_t low, int64_t high, size_t n) {
        std::vector<int64_t> out(n);
        uint32_t rng = (uint32_t)(high - low - 1);
        for (size_t i = 0; i < n; ++i) {
            if (rng == 0) { out[i] = low; continue; }
            uint32_t rng_excl = rng + 1;
            uint64_t m = (uint64_t)bg.next32() * rng_excl;
            uint32_t leftover = (uint32_t)m;
            if (leftover < rng_excl) {
                uint32_t threshold = (uint32_t)(0xFFFFFFFFu - rng) % rng_excl;
                while (leftover < threshold) {
                    m = (uint64_t)bg.next32() * rng_excl;
                    leftover = (uint32_t)m;
                }
            }
            out[i] = low + (int64_t)(m >> 32);
        }
        return out;
    }
    std::vector<double> random(size_t n) {
        std::vector<double> out(n);
        for (size_t i = 0; i < n; ++i) out[i] = bg.next_double();
        return out;
    }
    // permutation(n): Fisher-Yates from the end, random_interval draws
    std::vector<int64_t> permutation(int64_t n) {
        std::vector<int64_t> a(n);
        for (int64_t i = 0; i < n; ++i) a[i] = i;
        for (int64_t i = n - 1; i >= 1; --i) {
            uint64_t mx = (uint64_t)i, mask = mx, v;
            mask |= mask >> 1; mask |= mask >> 2; mask |= mask >> 4;
            mask |= mask >> 8; mask |= mask >> 16; mask |= mask >> 32;
            if (mx <= 0xFFFFFFFFu) {
                while ((v = (bg.next32() & mask)) > mx) {}
            } else {
                while ((v = (bg.next64() & mask)) > mx) {}
            }
            int64_t j = (int64_t)v;
            int64_t t = a[i]; a[i] = a[j]; a[j] = t;
        }
        return a;
    }
};

}  // namespace npy
