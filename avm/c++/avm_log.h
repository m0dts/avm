// avm_log.h -- where the engines' status lines go. Each thread an engine
// starts carries its owner's sink (thread_local), so a receiver and a
// transmitter in one process (the GUI) keep their lines apart; no sink:
// stderr, as the programs always printed them.
#pragma once
#include <cstdarg>
#include <cstdio>
#include <string>

struct LogSink {
    void (*fn)(void* user, const char* line) = nullptr;   // one line, no trailing newline
    void* user = nullptr;
};

inline LogSink& log_sink() {
    static thread_local LogSink s;
    return s;
}

// printf-style; the text may hold several lines (each passed on its own)
inline void avm_logf(const char* fmt, ...) {
    char small[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = std::vsnprintf(small, sizeof small, fmt, ap);
    va_end(ap);
    std::string s;
    if (n >= (int)sizeof small) {
        s.resize(n + 1);
        va_start(ap, fmt);
        std::vsnprintf(&s[0], n + 1, fmt, ap);
        va_end(ap);
        s.resize(n);
    } else if (n > 0) {
        s.assign(small, n);
    }
    LogSink& k = log_sink();
    if (!k.fn) {
        std::fputs(s.c_str(), stderr);
        std::fflush(stderr);
        return;
    }
    size_t a = 0;
    while (a < s.size()) {
        size_t b = s.find('\n', a);
        if (b == std::string::npos) b = s.size();
        k.fn(k.user, s.substr(a, b - a).c_str());
        a = b + 1;
    }
}
