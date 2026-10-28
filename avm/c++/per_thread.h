// per_thread.h -- one object of a type per thread, without a thread_local that
// has a destructor: with MinGW on Windows, such a thread_local in a DLL was
// destroyed in the thread's exit (a TLS callback) and crashed there -- the
// camera's capture thread, which ran the wavelet encoder's FFTs, ending when TX
// stopped. The thread_local is a plain pointer instead; the objects are owned
// by a list that lives until the program ends, so a thread's object is simply
// kept when the thread ends (a few kB of plans or LDPC buffers per thread).
#pragma once
#include <memory>
#include <mutex>
#include <vector>

template <typename T>
T& per_thread() {
    static thread_local T* mine = nullptr;      // (trivially destructible: no clean-up at thread exit)
    if (!mine) {
        static std::mutex mu;
        static std::vector<std::unique_ptr<T>>* all = new std::vector<std::unique_ptr<T>>;   // (never destroyed)
        std::lock_guard<std::mutex> g(mu);
        all->push_back(std::make_unique<T>());
        mine = all->back().get();
    }
    return *mine;
}
