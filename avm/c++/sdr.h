// sdr.h -- radios through SoapySDR's C API, loaded at run time (SoapySDR.dll
// / libSoapySDR.so -- the one AVM's Python uses, with its modules: Pluto,
// LimeSDR, HackRF, RTL-SDR, Airspy, Airspy HF+, SDRplay). No build-time dependency;
// the C API is the same whichever compiler built SoapySDR.
//
// RxSource sets a radio up as pluto_soapy_sink.PlutoRxSource does: the
// driver's device arguments, its rate (the nearest offered for the
// fixed-rate radios), the LO tuned to rf + tune offset, gain by driver
// (AGC, manual, or the radio's own), LimeSDR antenna port and filter,
// RTL-SDR ppm, the bandwidth, and the first warmup_s of samples discarded.
// Not yet: the GUI's live gain / frequency-offset files, duplex TX, the
// Pluto's FDD-mode check (_ensure_fdd_mode).
#pragma once
#include "avm_log.h"
#include "tdd.h"
#include <algorithm>
#include <atomic>
#include <functional>
#include <sys/stat.h>
#include <chrono>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <map>
#include <memory>
#include <thread>
#include <complex>
#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#if defined(_WIN32)
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN   // (no old winsock.h: virtual_radio.h has winsock2)
#endif
#include <windows.h>
#else
#include <arpa/inet.h>
#include <dlfcn.h>
#include <unistd.h>
#include <netdb.h>
#endif

namespace sdr {

const int RX = 1, TX = 0;
const int TIMEOUT = -1, OVERFLOW_ = -4;
// no samples in, or writes failing, this long: the radio has gone (unplugged)
const double RADIO_GONE_S = 3.0;
inline double mono_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

struct Kwargs { size_t size; char** keys; char** vals; };

// the C functions used (SoapySDR 0.8 API)
struct Api {
    void* lib = nullptr;
    void* (*make)(const char*) = nullptr;
    int (*unmake)(void*) = nullptr;
    const char* (*lastError)() = nullptr;
    int (*setSampleRate)(void*, int, size_t, double) = nullptr;
    double (*getSampleRate)(void*, int, size_t) = nullptr;
    double* (*listSampleRates)(void*, int, size_t, size_t*) = nullptr;
    int (*setFrequency)(void*, int, size_t, double, const Kwargs*) = nullptr;
    int (*setFrequencyCorrection)(void*, int, size_t, double) = nullptr;
    int (*setGain)(void*, int, size_t, double) = nullptr;
    int (*setGainMode)(void*, int, size_t, bool) = nullptr;
    int (*setBandwidth)(void*, int, size_t, double) = nullptr;
    double (*getBandwidth)(void*, int, size_t) = nullptr;
    int (*setAntenna)(void*, int, size_t, const char*) = nullptr;
    char** (*listAntennas)(void*, int, size_t, size_t*) = nullptr;
    void (*stringsClear)(char***, size_t) = nullptr;
    void (*freeMem)(void*) = nullptr;
    void* (*setupStream)(void*, int, const char*, const size_t*, size_t, const Kwargs*) = nullptr;
    int (*closeStream)(void*, void*) = nullptr;
    int (*activateStream)(void*, void*, int, long long, size_t) = nullptr;
    int (*deactivateStream)(void*, void*, int, long long) = nullptr;
    int (*readStream)(void*, void*, void* const*, size_t, int*, long long*, long) = nullptr;
    int (*writeStream)(void*, void*, const void* const*, size_t, int*, long long, long) = nullptr;
    const char* (*errToStr)(int) = nullptr;
    struct Range { double minimum, maximum, step; };
    Range (*getGainRange)(void*, int, size_t) = nullptr;
    Kwargs* (*enumerate)(const Kwargs*, size_t*) = nullptr;
    Kwargs* (*enumerateStr)(const char*, size_t*) = nullptr;
    void (*kwargsListClear)(Kwargs*, size_t) = nullptr;
    int (*writeGPIO)(void*, const char*, unsigned) = nullptr;
    unsigned (*readGPIO)(void*, const char*) = nullptr;
    int (*writeGPIODir)(void*, const char*, unsigned) = nullptr;
    unsigned (*readGPIODir)(void*, const char*) = nullptr;

    bool load(std::string& err) {
        if (lib) return true;
#if defined(_WIN32)
        HMODULE h = LoadLibraryA("SoapySDR.dll");
        auto sym = [&](const char* n) { return (void*)GetProcAddress(h, n); };
#else
        void* h = nullptr;
        for (const char* n : {"libSoapySDR.so", "libSoapySDR.so.0.8", "libSoapySDR.so.0.8-3", "libSoapySDR.so.0.7"})
            if ((h = dlopen(n, RTLD_NOW | RTLD_GLOBAL))) break;
        auto sym = [&](const char* n) { return dlsym(h, n); };
#endif
        if (!h) { err = "SoapySDR library not found (install SoapySDR, or put its DLL on the PATH)"; return false; }
        lib = (void*)h;
#define S(f, n) f = (decltype(f))sym(n)
        S(make, "SoapySDRDevice_makeStrArgs");
        S(unmake, "SoapySDRDevice_unmake");
        S(lastError, "SoapySDRDevice_lastError");
        S(setSampleRate, "SoapySDRDevice_setSampleRate");
        S(getSampleRate, "SoapySDRDevice_getSampleRate");
        S(listSampleRates, "SoapySDRDevice_listSampleRates");
        S(setFrequency, "SoapySDRDevice_setFrequency");
        S(setFrequencyCorrection, "SoapySDRDevice_setFrequencyCorrection");
        S(setGain, "SoapySDRDevice_setGain");
        S(setGainMode, "SoapySDRDevice_setGainMode");
        S(setBandwidth, "SoapySDRDevice_setBandwidth");
        S(getBandwidth, "SoapySDRDevice_getBandwidth");
        S(setAntenna, "SoapySDRDevice_setAntenna");
        S(listAntennas, "SoapySDRDevice_listAntennas");
        S(stringsClear, "SoapySDRStrings_clear");
        S(freeMem, "SoapySDR_free");
        S(setupStream, "SoapySDRDevice_setupStream");
        S(closeStream, "SoapySDRDevice_closeStream");
        S(activateStream, "SoapySDRDevice_activateStream");
        S(deactivateStream, "SoapySDRDevice_deactivateStream");
        S(readStream, "SoapySDRDevice_readStream");
        S(writeStream, "SoapySDRDevice_writeStream");
        S(errToStr, "SoapySDR_errToStr");
        S(writeGPIO, "SoapySDRDevice_writeGPIO");
        S(readGPIO, "SoapySDRDevice_readGPIO");
        S(writeGPIODir, "SoapySDRDevice_writeGPIODir");
        S(readGPIODir, "SoapySDRDevice_readGPIODir");
        S(getGainRange, "SoapySDRDevice_getGainRange");
        S(enumerate, "SoapySDRDevice_enumerate");
        S(enumerateStr, "SoapySDRDevice_enumerateStrArgs");
        S(kwargsListClear, "SoapySDRKwargsList_clear");
#undef S
        if (!make || !setSampleRate || !setFrequency || !setupStream || !activateStream || !readStream) {
            err = "SoapySDR library too old (needs the 0.8 API)";
            return false;
        }
        return true;
    }
};

inline Api& api() { static Api a; return a; }

// One SoapySDR device per radio in this process, shared by its receiver and
// transmitter (counted): opened apart -- as the Python AVM's two programs did --
// stopping RX closed it under the running TX (a Pluto: libiio crashed in the
// transmitter's iio_buffer_push). The same args = the same radio.
struct DevicePool {
    struct Entry { std::string args; void* dev; int users; };
    std::mutex mu;
    std::vector<Entry> open;
};
inline DevicePool& device_pool() { static DevicePool p; return p; }

// the args with a network radio's host name as its address -- "uri=ip:pluto.local"
// and "uri=ip:192.168.2.1" are one Pluto (a second one on USB took the name pluto.local):
// opened apart, stopping RX closed the radio under the running TX and libiio crashed
inline std::string device_key(const std::string& args) {
#ifndef _WIN32
    size_t i = args.find("uri=ip:");
    if (i == std::string::npos) return args;
    size_t h = i + 7, e = args.find_first_of(",:", h);
    std::string host = args.substr(h, e == std::string::npos ? std::string::npos : e - h);
    addrinfo hints{}, *res = nullptr;
    hints.ai_family = AF_INET;
    if (host.empty() || getaddrinfo(host.c_str(), nullptr, &hints, &res) != 0 || !res) return args;
    char ip[INET_ADDRSTRLEN] = {0};
    inet_ntop(AF_INET, &((sockaddr_in*)res->ai_addr)->sin_addr, ip, sizeof ip);
    freeaddrinfo(res);
    return args.substr(0, h) + ip + (e == std::string::npos ? std::string() : args.substr(e));
#else
    return args;
#endif
}

inline void* open_device(const std::string& args) {
    Api& A = api();
    DevicePool& P = device_pool();
    const std::string key = device_key(args);
    std::lock_guard<std::mutex> g(P.mu);
    for (auto& e : P.open)
        if (e.args == key) { ++e.users; return e.dev; }
    void* d = A.make ? A.make(args.c_str()) : nullptr;
    if (d) P.open.push_back({key, d, 1});
    return d;
}

// the sample rate, unless the radio already runs at it (within 2 Hz: a Pluto reports
// 549999 for 550000). On a Pluto RX and TX share one clock chain: setting it again
// reloads the AD9361's rates and filters for both, and RX restarting under a running
// TX made the transmission stutter.
inline void set_rate_if_changed(void* dev, int dir, double want) {
    Api& A = api();
    double have = A.getSampleRate ? A.getSampleRate(dev, dir, 0) : 0;
    if (have > 0 && std::fabs(have - want) <= 2.0) return;
    A.setSampleRate(dev, dir, 0, want);
}
// the analog bandwidth likewise (a change re-runs the AD9361's filter calibration)
inline void set_bandwidth_if_changed(void* dev, int dir, double want) {
    Api& A = api();
    if (!A.setBandwidth) return;
    double have = A.getBandwidth ? A.getBandwidth(dev, dir, 0) : 0;
    if (have > 0 && std::fabs(have - want) <= 2.0) return;
    A.setBandwidth(dev, dir, 0, want);
}

inline void close_device(void* dev) {
    if (!dev) return;
    Api& A = api();
    DevicePool& P = device_pool();
    std::lock_guard<std::mutex> g(P.mu);
    for (size_t i = 0; i < P.open.size(); ++i)
        if (P.open[i].dev == dev) {
            if (--P.open[i].users == 0) {
                if (A.unmake) A.unmake(dev);
                P.open.erase(P.open.begin() + (long)i);
            }
            return;
        }
    if (A.unmake) A.unmake(dev);     // (not from the pool)
}

// ---- LimeRFE: the LimeSDR's RF front end, on its expansion header. Driven as
// LimeSuite's limeRFE_cmd.cpp does "via SDR": I2C to the board's controller
// (address 0x51), bit-banged on the LimeSDR's GPIO 6 (SCL) and 7 (SDA) -- here
// through SoapySDR's GPIO calls on the radio already open (LimeSuite's own handle
// would open it a second time). The band from the frequency; its mode following
// this process's receiver and transmitter on that radio.
struct RfeConfig {
    bool on = false;
    int tx_port = 1;        // 1: J3 (TX/RX), 2: J4 (TX only: RX and TX at once possible)
    bool notch = false;     // the HAM 144/430 MHz FM broadcast notch
    int atten_db = 0;       // RX attenuation, 0..14 dB in 2 dB steps
};

// "PORT,NOTCH,ATTEN" (--lime-rfe 2,0,6)
inline RfeConfig parse_rfe(const std::string& s) {
    RfeConfig c;
    int p = 1, n = 0, a = 0;
    if (std::sscanf(s.c_str(), "%d,%d,%d", &p, &n, &a) >= 1) {
        c.on = true;
        c.tx_port = p == 2 ? 2 : 1;
        c.notch = n != 0;
        c.atten_db = std::max(0, std::min(14, a));
    }
    return c;
}

class LimeRfe {
public:
    // a receiver (tx false) or transmitter on dev starting at rf_hz / stopping
    static void attach(void* dev, bool tx, double rf_hz, const RfeConfig& c) {
        if (!c.on || !dev) return;
        std::lock_guard<std::mutex> g(mu());
        State& s = states()[dev];
        s.cfg = c;
        (tx ? s.tx : s.rx) = true;
        (tx ? s.cid_tx : s.cid_rx) = channel(rf_hz);
        update(dev, s);
    }
    static void detach(void* dev, bool tx) {
        if (!dev) return;
        std::lock_guard<std::mutex> g(mu());
        auto it = states().find(dev);
        if (it == states().end()) return;
        (tx ? it->second.tx : it->second.rx) = false;
        update(dev, it->second);
        if (!it->second.rx && !it->second.tx) states().erase(it);
    }

private:
    enum { MODE_RX = 0, MODE_TX = 1, MODE_NONE = 2, MODE_TXRX = 3 };
    static const int SCL = 6, SDA = 7, ADDR = 0x51, CMD_MODE = 0xd1, CMD_CONFIG = 0xd2;
    struct State {
        RfeConfig cfg;
        bool rx = false, tx = false;
        int cid_rx = 0, cid_tx = 0;
        int sent_rx = -1, sent_tx = -1, sent_mode = -1;
        bool failed = false;
    };
    static std::mutex& mu() { static std::mutex m; return m; }
    static std::map<void*, State>& states() { static std::map<void*, State> m; return m; }

    // RFE_CID_*: the HAM bands' filters and amplifiers, else the wideband paths
    static int channel(double hz) {
        const double m = hz / 1e6;
        if (m <= 30) return 3;                  // HAM_0030 (its own port, J5)
        if (m >= 50 && m <= 72) return 4;       // HAM_0070
        if (m >= 144 && m <= 148) return 5;     // HAM_0145
        if (m >= 219 && m <= 225) return 6;     // HAM_0220
        if (m >= 420 && m <= 450) return 7;     // HAM_0435
        if (m >= 902 && m <= 928) return 8;     // HAM_0920
        if (m >= 1240 && m <= 1325) return 9;   // HAM_1280
        if (m >= 2300 && m <= 2450) return 10;  // HAM_2400
        if (m >= 3300 && m <= 3500) return 11;  // HAM_3500
        return m < 1000 ? 1 : 2;                // WB_1000, WB_4000
    }

    static void update(void* dev, State& s) {
        if (s.failed) return;
        const int cid_rx = s.rx ? s.cid_rx : s.cid_tx, cid_tx = s.tx ? s.cid_tx : s.cid_rx;
        const bool hf = cid_rx == 3 || cid_tx == 3;
        const int port_rx = hf ? 3 : 1, port_tx = hf ? 3 : s.cfg.tx_port;
        // both at once: TX+RX only with TX on its own port (J4); on one port, TX wins
        const int mode = s.rx && s.tx ? (port_tx != port_rx ? MODE_TXRX : MODE_TX)
                         : s.tx ? MODE_TX : s.rx ? MODE_RX : MODE_NONE;
        Bus b(dev);
        if (!b.ok) {
            avm_logf("LimeRFE: this radio has no GPIO control through SoapySDR -- LimeRFE not set\n");
            s.failed = true;
            return;
        }
        uint8_t r[16] = {0};
        if (cid_rx != s.sent_rx || cid_tx != s.sent_tx) {
            uint8_t c[16] = {(uint8_t)CMD_CONFIG, (uint8_t)cid_rx, (uint8_t)cid_tx, (uint8_t)port_rx, (uint8_t)port_tx,
                             (uint8_t)mode, (uint8_t)(s.cfg.notch ? 1 : 0), (uint8_t)(s.cfg.atten_db / 2), 0, 0};
            if (!b.command(c, 16, r)) {
                avm_logf("LimeRFE: no answer on the LimeSDR's I2C (GPIO 6/7) -- is it fitted? Not set\n");
                s.failed = true;
                return;
            }
            s.sent_rx = cid_rx;
            s.sent_tx = cid_tx;
            s.sent_mode = mode;
            avm_logf("LimeRFE: band RX %d / TX %d, ports J%d / J%d, mode %s, notch %s, RX atten %d dB (status %d)\n",
                     cid_rx, cid_tx, port_rx == 1 ? 3 : port_rx == 2 ? 4 : 5, port_tx == 1 ? 3 : port_tx == 2 ? 4 : 5,
                     mode_name(mode), s.cfg.notch ? "on" : "off", s.cfg.atten_db, r[1]);
            return;
        }
        if (mode == s.sent_mode) return;
        uint8_t c[2] = {(uint8_t)CMD_MODE, (uint8_t)mode};
        if (!b.command(c, 2, r)) {
            avm_logf("LimeRFE: no answer setting the mode\n");
            return;
        }
        s.sent_mode = mode;
        avm_logf("LimeRFE: mode %s\n", mode_name(mode));
    }
    static const char* mode_name(int m) {
        return m == MODE_RX ? "RX" : m == MODE_TX ? "TX" : m == MODE_TXRX ? "TX+RX" : "off";
    }

    // I2C on two GPIO lines, open-drain style: a 1 released (an input, pulled up), a 0 driven low
    struct Bus {
        void* dev;
        unsigned dir = 0, out = 0;
        bool ok = false;
        explicit Bus(void* d) : dev(d) {
            Api& A = api();
            if (!A.writeGPIO || !A.writeGPIODir || !A.readGPIO || !A.readGPIODir) return;
            dir = A.readGPIODir(dev, "MAIN");
            out = A.readGPIO(dev, "MAIN");
            ok = true;
        }
        void set(int bit, int v) {
            Api& A = api();
            if (v) dir &= ~(1u << bit); else dir |= 1u << bit;
            if (v) out |= 1u << bit; else out &= ~(1u << bit);
            A.writeGPIODir(dev, "MAIN", dir);
            A.writeGPIO(dev, "MAIN", out);
        }
        int get(int bit) { return (api().readGPIO(dev, "MAIN") >> bit) & 1; }
        void start() { set(SDA, 1); set(SCL, 1); set(SDA, 0); set(SCL, 0); }
        void stop() { set(SDA, 0); set(SCL, 1); set(SDA, 1); }
        bool tx(uint8_t d) {        // true: acknowledged
            for (int i = 7; i >= 0; --i) { set(SDA, (d >> i) & 1); set(SCL, 1); set(SCL, 0); }
            set(SDA, 1);
            set(SCL, 1);
            bool ack = get(SDA) == 0;
            set(SCL, 0);
            return ack;
        }
        bool rx(bool ack, uint8_t& d) {
            set(SDA, 1);
            d = 0;
            for (int i = 0; i < 8; ++i) {
                set(SCL, 1);
                int k = 0;
                while (get(SCL) == 0 && ++k < 100) {}     // (the controller stretching the clock)
                if (k >= 100) return false;
                d = (uint8_t)(d << 1 | get(SDA));
                set(SCL, 0);
            }
            set(SDA, ack ? 0 : 1);
            set(SCL, 1);
            set(SCL, 0);
            set(SDA, 1);
            return true;
        }
        // a command written, its reply (the same length) read back
        bool command(const uint8_t* c, int n, uint8_t* r) {
            start();
            bool ok_ = tx((uint8_t)(ADDR << 1));
            for (int i = 0; ok_ && i < n; ++i) tx(c[i]);
            stop();
            if (!ok_) return false;
            start();
            if (!tx((uint8_t)(ADDR << 1 | 1))) { stop(); return false; }
            for (int i = 0; i < n; ++i)
                if (!rx(i < n - 1, r[i])) { stop(); return false; }
            stop();
            return true;
        }
    };
};

// The radios connected now (touch_widgets.detect_radios / detect_rx_radios):
// one per Pluto route (USB, or its address), LimeSDR by model and serial, and
// the receive-only SoapySDR radios. driver: the --sdr name; uri: a Pluto's.
struct Found { std::string driver, label, uri; };

inline std::vector<Found> find_radios(bool rx) {
    std::vector<Found> out;
    std::string err;
    Api& A = api();
    if (!A.load(err) || !A.enumerateStr || !A.kwargsListClear) return out;
    auto list = [&](const char* args, auto fn) {
        size_t n = 0;
        Kwargs* k = A.enumerateStr(args, &n);
        for (size_t i = 0; i < n; ++i) {
            std::map<std::string, std::string> kv;
            for (size_t j = 0; j < k[i].size; ++j) kv[k[i].keys[j]] = k[i].vals[j];
            fn(kv);
        }
        if (k) A.kwargsListClear(k, n);
    };
    auto tail = [](std::string s, size_t n) {
        s.erase(0, std::min(s.find_first_not_of('0'), s.size()));
        return s.size() > n ? s.substr(s.size() - n) : s;
    };
    // each Pluto once, by its address: "ip:pluto.local" (found once over IPv4, again over
    // IPv6) as the address it resolves to; and its USB route left out when its USB network
    // address (192.168.2.x) is listed -- one radio, three entries, and RX and TX chosen by
    // different ones opened it twice (stopping RX crashed TX)
    list("driver=plutosdr", [&](std::map<std::string, std::string>& kv) {
        std::string uri = kv["uri"];
        if (uri.rfind("ip:", 0) == 0) {
            std::string k = device_key("uri=" + uri);
            uri = k.substr(4);
        }
        std::string label = uri.rfind("usb:", 0) == 0 ? "PlutoSDR (USB)"
                            : uri.rfind("ip:", 0) == 0 ? "PlutoSDR (IP " + uri.substr(3) + ")" : "PlutoSDR";
        for (const Found& f : out) if (f.label == label) return;
        out.push_back({"pluto", label, uri});
    });
    {
        bool usb_net = false;
        for (const Found& f : out) usb_net |= f.driver == "pluto" && f.uri.rfind("ip:192.168.2.", 0) == 0;
        if (usb_net)
            out.erase(std::remove_if(out.begin(), out.end(), [](const Found& f) { return f.driver == "pluto" && f.uri.rfind("usb:", 0) == 0; }),
                      out.end());
    }
    list("driver=lime", [&](std::map<std::string, std::string>& kv) {
        std::string name = kv.count("name") ? kv["name"] : kv["label"].substr(0, kv["label"].find(" ["));
        if (name.empty()) name = "LimeSDR";
        std::string label = name.rfind("Lime", 0) == 0 ? name : "LimeSDR " + name;
        std::string s = tail(kv["serial"], 6);
        out.push_back({"lime", s.empty() ? label : label + " " + s, ""});
    });
    list("driver=hackrf", [&](std::map<std::string, std::string>& kv) {
        out.push_back({"hackrf", "HackRF " + tail(kv["serial"], 8), ""});
    });
    if (rx) {
        list("driver=rtlsdr", [&](std::map<std::string, std::string>& kv) {
            out.push_back({"rtlsdr", "RTL-SDR " + tail(kv["serial"], 8), ""});
        });
        list("driver=airspy", [&](std::map<std::string, std::string>& kv) {
            out.push_back({"airspy", "Airspy " + tail(kv["serial"], 8), ""});
        });
        list("driver=airspyhf", [&](std::map<std::string, std::string>& kv) {
            out.push_back({"airspyhf", "Airspy HF+ " + tail(kv["serial"], 8), ""});
        });
        list("driver=sdrplay", [&](std::map<std::string, std::string>& kv) {
            if (kv.count("mode") && kv["mode"] != "ST") return;    // an RSPduo: once, single tuner
            out.push_back({"sdrplay", "SDRplay " + tail(kv["serial"], 6), ""});
        });
    }
    return out;
}

// GainFileWatcher: a number (dB, or Hz) the GUI writes to a file, passed to
// apply() within ~0.2 s of each change
class FileWatcher {
public:
    FileWatcher(const std::string& path, std::function<void(double)> apply) : path_(path), apply_(std::move(apply)) {
        th_ = std::thread([this, sink = log_sink()] {
            log_sink() = sink;
            // (the value read each time: two changes within the file time's second, same size, both seen)
            bool have = false;
            double last = 0;
            while (!stop_) {
                if (FILE* f = std::fopen(path_.c_str(), "r")) {
                    double v;
                    if (std::fscanf(f, "%lf", &v) == 1 && (!have || v != last)) {
                        have = true;
                        last = v;
                        apply_(v);
                    }
                    std::fclose(f);
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(200));
            }
        });
    }
    ~FileWatcher() { stop_ = true; if (th_.joinable()) th_.join(); }
private:
    std::string path_;
    std::function<void(double)> apply_;
    std::atomic<bool> stop_{false};
    std::thread th_;
};

struct RxConfig {
    std::string driver = "pluto", uri, antenna;
    std::string lime_rfe;      // --lime-rfe: a LimeRFE on the LimeSDR (parse_rfe)
    double rf_hz = 0, rate = 0, gain_db = 30, bandwidth_hz = 0, tune_offset_hz = 0, ppm = 0, warmup_s = 0.75;
    bool agc = false;
    std::string gain_file, freq_offset_file, ppm_file;   // (ppm_file: an RTL-SDR's live correction)
    // duplex (TDD): this receiver's radio also transmits tdd::queue() (a Pluto)
    bool tdd_tx = false, tdd_start_muted = false;
    double tdd_tx_gain = -10;
};

// a receiving radio, as the receiver uses it: SoapySDR's (RxSource) or another
// (hpsdr1.h's protocol 1 network radio)
class RadioIn {
public:
    double rate = 0;        // what the radio was set to
    std::string error;
    // the next samples; 0 on a timeout, -1 when the stream has failed
    virtual long read(std::vector<std::complex<float>>& out) = 0;
    virtual void set_gain(double db) = 0;
    virtual void set_freq_offset(double hz) = 0;
    // duplex: the transmitter on this radio (nullptr: none), and the samples
    // read but discarded (the settling warmup) -- own slot = start - discarded
    virtual tdd::Feeder* tdd_feeder() { return nullptr; }
    virtual long long discarded() const { return 0; }
    virtual ~RadioIn() {}
};

class RxSource : public RadioIn {
public:

    bool open(const RxConfig& c) {
        Api& A = api();
        if (!A.load(error)) return false;
        cfg_ = c;
        std::string args;
        const bool fixed = c.driver == "airspy" || c.driver == "airspyhf" || c.driver == "sdrplay";
        if (c.driver == "pluto") args = "driver=plutosdr" + (c.uri.empty() ? std::string() : ",uri=" + c.uri);
        else args = "driver=" + c.driver;
        avm_logf("Opening %s RX (%s)...\n", c.driver.c_str(), args.c_str());
        dev_ = open_device(args);
        if (!dev_ && c.driver == "pluto" && !c.uri.empty()) {
            // the chosen route failed: the other default addresses, then libiio's own search
            for (const char* alt : {"ip:192.168.2.1", "ip:192.168.1.10"})
                if (c.uri != alt && (dev_ = open_device(std::string("driver=plutosdr,uri=") + alt))) {
                    avm_logf("PlutoSDR RX: found at %s\n", alt);
                    break;
                }
            if (!dev_) dev_ = open_device("driver=plutosdr");
        }
        if (!dev_) { error = std::string("could not open the radio: ") + (A.lastError ? A.lastError() : "?"); return false; }
        if (c.driver == "lime" && !c.lime_rfe.empty()) LimeRfe::attach(dev_, false, c.rf_hz, parse_rfe(c.lime_rfe));
        double want = c.rate;
        if (fixed && A.listSampleRates) {
            size_t n = 0;
            double* rates = A.listSampleRates(dev_, RX, 0, &n);
            std::vector<double> rs(rates, rates + n);
            if (rates && A.freeMem) A.freeMem(rates);
            std::sort(rs.begin(), rs.end());
            if (!rs.empty()) {
                double pick = rs.back();
                for (double r : rs) if (r >= want) { pick = r; break; }
                if (pick != want) avm_logf("[rx] %s: %g kS/s not offered, using %g kS/s\n", c.driver.c_str(), want / 1e3, pick / 1e3);
                want = pick;
            }
        }
        // an RTL-SDR can't sample at 300-900 kS/s (it stays at its old rate, and reports the asked
        // one: the signal lands off centre): the next rate up, the front end resamples it
        // a HackRF: 2 MS/s at least (its filters and ADC are made for that), resampled here
        if (c.driver == "hackrf" && want < 2000000) want = 2000000;
        if (c.driver == "rtlsdr" && want > 300000 && want <= 900000) {
            avm_logf("[rx] rtlsdr: %g kS/s not possible, using 960 kS/s\n", want / 1e3);
            want = 960000;
        }
        set_rate_if_changed(dev_, RX, want);
        rate = A.getSampleRate ? A.getSampleRate(dev_, RX, 0) : want;
        if (rate <= 0) rate = want;
        double tune = c.rf_hz + c.tune_offset_hz;
        A.setFrequency(dev_, RX, 0, tune, nullptr);
        if (c.driver == "rtlsdr" && c.ppm && A.setFrequencyCorrection) A.setFrequencyCorrection(dev_, RX, 0, c.ppm);
        ppm_ = c.ppm;
        if (c.driver == "lime") lime_configure(tune);
        if (c.agc) {
            if (A.setGainMode) A.setGainMode(dev_, RX, 0, true);
        } else if (c.driver == "lime") {
            A.setGain(dev_, RX, 0, c.gain_db);
        } else if (c.driver == "airspyhf" || c.driver == "sdrplay") {
            if (A.setGainMode) A.setGainMode(dev_, RX, 0, true);    // their own gain control
        } else {
            if (A.setGainMode) A.setGainMode(dev_, RX, 0, false);
            A.setGain(dev_, RX, 0, c.gain_db);
        }
        if (c.bandwidth_hz && c.driver != "rtlsdr" && c.driver != "hackrf" && !fixed) set_bandwidth_if_changed(dev_, RX, c.bandwidth_hz);
        if (!c.antenna.empty() && c.driver != "lime" && A.setAntenna) A.setAntenna(dev_, RX, 0, c.antenna.c_str());
        size_t ch = 0;
        stream_ = A.setupStream(dev_, RX, "CF32", &ch, 1, nullptr);
        if (!stream_) { error = "setupStream failed"; return false; }
        // duplex: the TX side set up BEFORE RX starts, then both started back
        // to back -- the conditions tdd::TXRX_LATENCY_S was measured in
        if (c.tdd_tx && !tdd_setup()) return false;
        if (A.activateStream(dev_, stream_, 0, 0, 0) != 0) { error = "activateStream failed"; return false; }
        if (c.tdd_tx && !tdd_start()) return false;
        warmup_ = (long long)(rate * c.warmup_s + 0.5);
        tune_ = tune;
        if (!c.gain_file.empty()) gain_watch_.reset(new FileWatcher(c.gain_file, [this](double db) { set_gain(db); }));
        if (!c.freq_offset_file.empty())
            offset_watch_.reset(new FileWatcher(c.freq_offset_file, [this](double hz) { set_freq_offset(hz); }));
        if (!c.ppm_file.empty() && c.driver == "rtlsdr" && A.setFrequencyCorrection)
            ppm_watch_.reset(new FileWatcher(c.ppm_file, [this](double ppm) {
                if (ppm == ppm_) return;
                ppm_ = ppm;
                api().setFrequencyCorrection(dev_, RX, 0, ppm);
                avm_logf("RX frequency correction %+g ppm\n", ppm);
            }));
        avm_logf("%s RX: %.4f MHz (LO %.4f MHz, %+.1f kHz), %.1f kS/s, gain=%s\n", c.driver.c_str(),
                     c.rf_hz / 1e6, tune / 1e6, c.tune_offset_hz / 1e3, rate / 1e3,
                     c.agc ? "AGC" : (std::to_string((int)c.gain_db) + "dB").c_str());
        return true;
    }

    // the next samples (after the warmup discard); 0 on a timeout, -1 when
    // the stream has failed
    long read(std::vector<std::complex<float>>& out) override {
        Api& A = api();
        out.resize(32768);
        void* bufs[1] = {out.data()};
        int flags = 0;
        long long t = 0;
        int r = A.readStream(dev_, stream_, bufs, out.size(), &flags, &t, 1000000);
        const double now = mono_s();
        if (last_data_ < 0) last_data_ = now;
        if (r == TIMEOUT || r == OVERFLOW_ || r == 0) {
            if (r == TIMEOUT && ++timeouts_ % 5 == 0)
                avm_logf("%s RX: no samples for %ds -- the radio is open but not delivering data\n",
                             cfg_.driver.c_str(), timeouts_);
            if (r == OVERFLOW_ && now - overflow_logged_ >= 1.0) {     // (at most once a second)
                avm_logf("WARNING: %s RX overflow -- samples dropped.\n", cfg_.driver.c_str());
                overflow_logged_ = now;
            }
            out.clear();
            // nothing for RADIO_GONE_S however fast the driver answers: unplugged (a Pluto's
            // driver then answers every read at once, and the reader spun, flooding the log)
            if (now - last_data_ > RADIO_GONE_S) {
                avm_logf("ERROR: %s RX: no samples for %.0f s -- the radio has gone (unplugged?)\n",
                         cfg_.driver.c_str(), RADIO_GONE_S);
                return -1;
            }
            if (r != TIMEOUT) std::this_thread::sleep_for(std::chrono::milliseconds(5));
            return 0;
        }
        last_data_ = now;
        if (r < 0) {
            avm_logf("%s RX stream error: %s -- stopping.\n", cfg_.driver.c_str(), A.errToStr ? A.errToStr(r) : "?");
            return -1;
        }
        timeouts_ = 0;
        out.resize(r);
        count_ += r;
        long long w = warmup_.load();
        if (w > 0) {
            long long drop = std::min<long long>(w, r);
            out.erase(out.begin(), out.begin() + drop);
            warmup_ -= drop;
            discarded_ += drop;
        }
        return (long)out.size();
    }

    // live RX gain (PlutoRxSource.set_gain): nothing under AGC or the radios'
    // own; the Pluto's manual mode re-asserted and the settling discard re-armed
    void set_gain(double db) override {
        Api& A = api();
        if (cfg_.agc) { avm_logf("WARNING: gain change ignored -- AGC is active (--rx-agc)\n"); return; }
        if (cfg_.driver == "airspyhf" || cfg_.driver == "sdrplay") return;
        if (cfg_.driver == "lime" || cfg_.driver == "rtlsdr" || cfg_.driver == "airspy" || cfg_.driver == "hackrf") {
            A.setGain(dev_, RX, 0, db);
            return;
        }
        if (feeder_) {      // duplex: no settling discard (it would move the own-slot timing)
            if (A.setGainMode) A.setGainMode(dev_, RX, 0, false);
            A.setGain(dev_, RX, 0, db);
            avm_logf("RX gain %g dB\n", db);
            return;
        }
        if (A.setGainMode) A.setGainMode(dev_, RX, 0, false);
        A.setGain(dev_, RX, 0, db);
        warmup_ = (long long)(rate * cfg_.warmup_s + 0.5);
        avm_logf("RX gain %g dB\n", db);
    }
    // live frequency offset (the GUI's Offset): the LO moves
    void set_freq_offset(double hz) override {
        if (hz == offset_) return;
        offset_ = hz;
        api().setFrequency(dev_, RX, 0, tune_ + hz, nullptr);
        avm_logf("RX frequency offset %+.0f kHz\n", hz / 1e3);
    }

    tdd::Feeder* tdd_feeder() override { return feeder_.get(); }
    long long discarded() const override { return discarded_; }

    ~RxSource() override {
        gain_watch_.reset();
        offset_watch_.reset();
        ppm_watch_.reset();
        Api& A = api();
        if (feeder_) {
            feeder_->stop();
            feeder_.reset();
            if (A.deactivateStream) A.deactivateStream(dev_, tx_stream_, 0, 0);
            if (A.closeStream) A.closeStream(dev_, tx_stream_);
        }
        if (dev_ && stream_) {
            if (A.deactivateStream) A.deactivateStream(dev_, stream_, 0, 0);
            if (A.closeStream) A.closeStream(dev_, stream_);
        }
        if (cfg_.driver == "lime") LimeRfe::detach(dev_, false);
        close_device(dev_);     // (the shared radio: closed once its last user is done)
    }

private:
    // _lime_configure: the antenna port for the frequency (or as asked), the smallest analogue filter
    void lime_configure(double freq) {
        Api& A = api();
        std::vector<std::string> ports;
        if (A.listAntennas) {
            size_t n = 0;
            char** p = A.listAntennas(dev_, RX, 0, &n);
            for (size_t i = 0; i < n; ++i) if (std::string(p[i]) != "NONE") ports.push_back(p[i]);
            if (p && A.stringsClear) A.stringsClear(&p, n);
        }
        auto has = [&](const std::string& a) { return std::find(ports.begin(), ports.end(), a) != ports.end(); };
        std::string ant = cfg_.antenna;
        if (const char* e = std::getenv("AVM_LIME_RX_ANTENNA")) if (ant.empty()) ant = e;
        if (!ant.empty() && !ports.empty() && !has(ant)) ant.clear();
        if (ant.empty()) {
            ant = has("LNAL") && freq < 1.5e9 ? "LNAL" : freq < 2.0e9 ? "LNAW" : "LNAH";
            if (!has(ant) && !ports.empty()) ant = ports[0];
        }
        if (A.setAntenna) A.setAntenna(dev_, RX, 0, ant.c_str());
        if (A.setBandwidth) A.setBandwidth(dev_, RX, 0, 1.5e6);
        avm_logf("LimeSDR RX: antenna port %s\n", ant.c_str());
    }
    // duplex: TX on the same connection (pluto_soapy_sink.TddTxFeeder)
    bool tdd_setup() {
        Api& A = api();
        if (cfg_.driver != "pluto") { error = "duplex needs a PlutoSDR"; return false; }
        if (!A.writeStream) { error = "SoapySDR without writeStream"; return false; }
        A.setSampleRate(dev_, TX, 0, rate);
        A.setFrequency(dev_, TX, 0, cfg_.rf_hz, nullptr);   // (TX: no LO offset)
        double v = cfg_.tdd_tx_gain;
        if (A.getGainRange) {
            Api::Range r = A.getGainRange(dev_, TX, 0);
            if (r.minimum >= 0 && r.maximum >= 89.0 - 1) v = std::min(std::max(89.0 + v, r.minimum), r.maximum);
        }
        A.setGain(dev_, TX, 0, v);
        size_t ch = 0;
        tx_stream_ = A.setupStream(dev_, TX, "CF32", &ch, 1, nullptr);
        if (!tx_stream_) { error = "duplex: TX setupStream failed"; return false; }
        return true;
    }
    bool tdd_start() {
        Api& A = api();
        if (A.activateStream(dev_, tx_stream_, 0, 0, 0) != 0) { error = "duplex: TX activateStream failed"; return false; }
        feeder_.reset(new tdd::Feeder(rate, [this](const std::complex<float>* p, size_t n) {
            Api& A = api();
            size_t pos = 0;
            while (pos < n) {
                const void* bufs[1] = {p + pos};
                int flags = 0;
                int r = A.writeStream(dev_, tx_stream_, bufs, n - pos, &flags, 0, 1000000);
                if (r == TIMEOUT) continue;
                if (r <= 0) return false;      // (0: unplugged -- see TxSink::write_all)
                pos += (size_t)r;
            }
            return true;
        }, [this] { return count_.load(); }, cfg_.tdd_start_muted));
        feeder_->start();
        avm_logf("PlutoSDR duplex TX active on the RX radio: %.4f MHz, %.1f kS/s, gain %g dB%s\n", cfg_.rf_hz / 1e6,
                 rate / 1e3, cfg_.tdd_tx_gain, cfg_.tdd_start_muted ? " (silent until placed)" : "");
        return true;
    }
    RxConfig cfg_;
    void* dev_ = nullptr;
    void* stream_ = nullptr;
    void* tx_stream_ = nullptr;
    std::unique_ptr<tdd::Feeder> feeder_;
    std::atomic<long long> count_{0}, discarded_{0};
    std::atomic<long long> warmup_{0};
    int timeouts_ = 0;
    double last_data_ = -1, overflow_logged_ = -1;
    double tune_ = 0, offset_ = 0, ppm_ = 0;
    std::unique_ptr<FileWatcher> gain_watch_, offset_watch_, ppm_watch_;
};

// ---------------------------------------------------------------- transmit
// TxSink as pluto_soapy_sink.PlutoTxSink: the LO tuned to rf + lo offset
// (the caller pre-shifts the waveform down by it), --tx-gain (dB re full
// power) mapped onto the driver's scale (the LimeSDR's absolute range; the
// Pluto's two conventions, told apart by its range), the bandwidth, the
// LimeSDR's port; samples written by its own thread from a queue, 2 ms of
// silence whenever it runs dry (an activated stream left unfed glitches),
// the LimeSDR paced to at most 0.25 s ahead of real time.
struct TxConfig {
    std::string driver = "pluto", uri, antenna;
    std::string lime_rfe;
    double rf_hz = 0, rate = 0, gain_db = -10, bandwidth_hz = 0, tune_offset_hz = 0;
    size_t queue_depth = 8;
    std::string gain_file;
};

// ---- PTT on a Raspberry Pi: physical pin 40 (BCM GPIO 21), high while RF is out --
// Portsdown 4's "TX LED / PTT drive", so its PTT wiring works as it is. Through
// Raspberry Pi OS's pinctrl (Pi 4 and Pi 5 alike); elsewhere nothing.
namespace ptt {
inline bool available() {
#ifdef __linux__
    static int ok = -1;
    if (ok < 0) {
        ok = 0;
        if (FILE* f = std::fopen("/proc/device-tree/model", "r")) {
            char m[128] = {0};
            size_t n = std::fread(m, 1, sizeof m - 1, f);
            std::fclose(f);
            m[n] = 0;
            ok = std::strstr(m, "Raspberry Pi") && access("/usr/bin/pinctrl", X_OK) == 0;
        }
    }
    return ok == 1;
#else
    return false;
#endif
}
inline void set(bool on) {
    if (!available()) return;
    if (std::system(on ? "/usr/bin/pinctrl set 21 op dh" : "/usr/bin/pinctrl set 21 op dl") != 0)
        avm_logf("PTT: pinctrl failed (GPIO 21)\n");
    else
        avm_logf("PTT %s (GPIO 21, pin 40)\n", on ? "on" : "off");
}

// A Pluto's PTT: its AD9363's GPO0 (as F5OEO's firmware, a G0MJW PTT board on it),
// set by hand through libiio -- register 0x026 bit 4 the GPOs under manual control,
// 0x027 bits 7..4 their levels (bit 4: GPO0). Its own context beside SoapySDR's.
class PlutoGpo {
public:
    explicit PlutoGpo(const std::string& uri) : uri_(uri.empty() ? "ip:192.168.2.1" : uri) {}
    ~PlutoGpo() { if (ctx_) lib().destroy(ctx_); }
    void set(bool on) {
        Lib& L = lib();
        if (!L.ok) { log_once("PTT: libiio not found -- the Pluto's GPO0 not set\n"); return; }
        if (!ctx_) {
            ctx_ = L.create(uri_.c_str());
            if (ctx_) dev_ = L.find(ctx_, "ad9361-phy");
        }
        if (!dev_) { log_once("PTT: could not reach the Pluto's ad9361-phy (" + uri_ + ") -- GPO0 not set\n"); return; }
        uint32_t r26 = 0, r27 = 0;
        if (L.read(dev_, 0x26, &r26) < 0 || L.read(dev_, 0x27, &r27) < 0) { avm_logf("PTT: Pluto register read failed\n"); return; }
        r27 = on ? (r27 | 0x10) : (r27 & ~0x10u);
        if (L.write(dev_, 0x26, r26 | 0x10) < 0 || L.write(dev_, 0x27, r27) < 0) { avm_logf("PTT: Pluto register write failed\n"); return; }
        avm_logf("PTT %s (Pluto GPO0)\n", on ? "on" : "off");
    }

private:
    struct Lib {
        bool ok = false;
        void* (*create)(const char*) = nullptr;
        void (*destroy)(void*) = nullptr;
        void* (*find)(void*, const char*) = nullptr;
        int (*read)(void*, uint32_t, uint32_t*) = nullptr;
        int (*write)(void*, uint32_t, uint32_t) = nullptr;
        Lib() {
#ifdef _WIN32
            HMODULE h = LoadLibraryA("libiio.dll");
            auto sym = [&](const char* n) { return (void*)GetProcAddress(h, n); };
#else
            void* h = nullptr;
            for (const char* n : {"libiio.so.0", "libiio.so"})
                if ((h = dlopen(n, RTLD_NOW))) break;
            auto sym = [&](const char* n) { return dlsym(h, n); };
#endif
            if (!h) return;
            create = (decltype(create))sym("iio_create_context_from_uri");
            destroy = (decltype(destroy))sym("iio_context_destroy");
            find = (decltype(find))sym("iio_context_find_device");
            read = (decltype(read))sym("iio_device_reg_read");
            write = (decltype(write))sym("iio_device_reg_write");
            ok = create && destroy && find && read && write;
        }
    };
    static Lib& lib() { static Lib l; return l; }
    void log_once(const std::string& m) {
        if (!warned_) avm_logf("%s", m.c_str());
        warned_ = true;
    }
    std::string uri_;
    void* ctx_ = nullptr;
    void* dev_ = nullptr;
    bool warned_ = false;
};
}  // namespace ptt

class TxSink {
public:
    double rate = 0;
    std::string error;

    bool open(const TxConfig& c) {
        Api& A = api();
        if (!A.load(error)) return false;
        cfg_ = c;
        if (c.driver != "pluto" && c.driver != "lime" && c.driver != "hackrf") {
            error = "--sdr " + c.driver + " can't transmit";
            return false;
        }
        std::string args = c.driver == "lime" ? "driver=lime" : c.driver == "hackrf" ? "driver=hackrf"
                                              : "driver=plutosdr" + (c.uri.empty() ? std::string() : ",uri=" + c.uri);
        avm_logf("Opening %s TX (%s)...\n", c.driver.c_str(), args.c_str());
        dev_ = open_device(args);
        if (!dev_ && c.driver == "pluto" && !c.uri.empty()) dev_ = open_device("driver=plutosdr");
        if (!dev_) { error = std::string("could not open the radio: ") + (A.lastError ? A.lastError() : "?"); return false; }
        if (c.driver == "lime" && !c.lime_rfe.empty()) LimeRfe::attach(dev_, true, c.rf_hz, parse_rfe(c.lime_rfe));
        // (a HackRF: 2 MS/s at least; the transmitter builds its waveform at the rate it really runs at)
        set_rate_if_changed(dev_, TX, c.driver == "hackrf" ? std::max(c.rate, 2000000.0) : c.rate);
        rate = A.getSampleRate ? A.getSampleRate(dev_, TX, 0) : c.rate;
        if (rate <= 0) rate = c.rate;
        double tune = c.rf_hz + c.tune_offset_hz;
        A.setFrequency(dev_, TX, 0, tune, nullptr);
        if (c.driver == "lime") {
            std::string ant = c.antenna;
            if (const char* e = std::getenv("AVM_LIME_TX_ANTENNA")) if (ant.empty()) ant = e;
            if (ant.empty()) ant = tune < 2.0e9 ? "BAND1" : "BAND2";
            if (A.setAntenna) A.setAntenna(dev_, TX, 0, ant.c_str());
            if (A.setBandwidth) A.setBandwidth(dev_, TX, 0, 5e6);
        } else if (c.bandwidth_hz && c.driver != "hackrf" && A.setBandwidth) {
            set_bandwidth_if_changed(dev_, TX, c.bandwidth_hz);
        }
        set_gain(c.gain_db);
        size_t ch = 0;
        stream_ = A.setupStream(dev_, TX, "CF32", &ch, 1, nullptr);
        if (!stream_) { error = "setupStream failed"; return false; }
        if (A.activateStream(dev_, stream_, 0, 0, 0) != 0) { error = "activateStream failed"; return false; }
        avm_logf("%s TX: %.4f MHz (LO %.4f MHz, %+.1f kHz), %.1f kS/s, gain %g dB\n", c.driver.c_str(),
                     c.rf_hz / 1e6, tune / 1e6, c.tune_offset_hz / 1e3, rate / 1e3, c.gain_db);
        th_ = std::thread([this, sink = log_sink()] { log_sink() = sink; run(); });
        if (!c.gain_file.empty()) gain_watch_.reset(new FileWatcher(c.gain_file, [this](double db) { set_gain(db); }));
        return true;
    }

    // --tx-gain on the driver's scale
    void set_gain(double tx_gain_db) {
        Api& A = api();
        double v = tx_gain_db;
        if (A.getGainRange) {
            Api::Range r = A.getGainRange(dev_, TX, 0);
            // (the LimeSDR's and HackRF's: 0 dB = their maximum, as the Pluto's)
            if (cfg_.driver == "lime" || cfg_.driver == "hackrf") v = std::min(std::max(r.maximum + tx_gain_db, r.minimum), r.maximum);
            else if (r.minimum >= 0 && r.maximum >= 89.0 - 1) v = std::min(std::max(89.0 + tx_gain_db, r.minimum), r.maximum);
        }
        A.setGain(dev_, TX, 0, v);
    }

    // false once the radio has gone (writes failing, or falling behind real time, for RADIO_GONE_S)
    bool ok() const { return !failed_; }

    // stopping: the writer gives up what's queued and nothing waits on it any more
    // (an unplugged Pluto's writes each stall in the network for a while)
    void abort() {
        abort_ = true;
        std::lock_guard<std::mutex> g(mu_);
        stop_ = true;
        q_.clear();
        ready_.notify_all();
        space_.notify_all();
        done_.notify_all();
    }

    // queued for the writer; blocks while queue_depth waveforms are waiting
    void write(std::vector<std::complex<float>>&& w) {
        std::unique_lock<std::mutex> lk(mu_);
        space_.wait(lk, [&] { return q_.size() < cfg_.queue_depth || stop_ || failed_; });
        if (abort_ || failed_) return;
        q_.push_back(std::move(w));
        ready_.notify_all();
    }
    // everything queued sent
    void drain() {
        std::unique_lock<std::mutex> lk(mu_);
        done_.wait(lk, [&] { return (q_.empty() && !busy_) || abort_; });
    }

    ~TxSink() {
        gain_watch_.reset();
        abort_ = true;
        {
            std::lock_guard<std::mutex> g(mu_);
            stop_ = true;
            ready_.notify_all();
            space_.notify_all();
        }
        if (th_.joinable()) th_.join();
        Api& A = api();
        if (dev_ && stream_) {
            if (A.deactivateStream) A.deactivateStream(dev_, stream_, 0, 0);
            if (A.closeStream) A.closeStream(dev_, stream_);
        }
        if (cfg_.driver == "lime") LimeRfe::detach(dev_, true);
        close_device(dev_);     // (the shared radio: closed once its last user is done)
        // PTT dropped once RF has stopped: the stream shut down, the radio's own buffer
        // given time to empty (a relay or amplifier never switched under RF)
        if (ptt_) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            ptt_ = false;
            key(false);
        }
    }

private:
    bool ptt_ = false;
    std::unique_ptr<ptt::PlutoGpo> pluto_gpo_;
    // the Pi's pin 40 and, a Pluto, its GPO0
    void key(bool on) {
        ptt::set(on);
        if (cfg_.driver != "pluto") return;
        if (!pluto_gpo_) pluto_gpo_.reset(new ptt::PlutoGpo(cfg_.uri));
        pluto_gpo_->set(on);
    }
    void run() {
        std::vector<std::complex<float>> silence((size_t)std::max(1.0, rate * 0.002));
        while (true) {
            std::vector<std::complex<float>> w;
            {
                std::unique_lock<std::mutex> lk(mu_);
                ready_.wait_for(lk, std::chrono::milliseconds(2), [&] { return !q_.empty() || stop_; });
                if (stop_ && q_.empty()) return;
                if (!q_.empty()) {
                    w = std::move(q_.front());
                    q_.pop_front();
                    busy_ = true;
                    space_.notify_all();
                }
            }
            write_all(w.empty() ? silence : w);
            // PTT once the first waveform has gone to the radio: after its calibration
            // (a LimeSDR's tones) and the stream's start-up, with RF really coming out
            if (!w.empty() && !ptt_ && !failed_ && !abort_) { ptt_ = true; key(true); }
            std::lock_guard<std::mutex> g(mu_);
            busy_ = false;
            done_.notify_all();
        }
    }
    void write_all(const std::vector<std::complex<float>>& s) {
        Api& A = api();
        if (failed_) return;      // (the radio gone: the queue drains, ok() says so)
        if (cfg_.driver == "lime") pace(s.size());
        size_t pos = 0;
        while (pos < s.size() && !abort_) {      // (closing: given up at once)
            size_t n = std::min<size_t>(1 << 16, s.size() - pos);
            const void* bufs[1] = {s.data() + pos};
            int flags = 0;
            int r = A.writeStream(dev_, stream_, bufs, n, &flags, 0, 1000000);
            // (0 written: a Pluto unplugged answers every write so, at once -- this loop spun
            // on it, and closing TX waited for it for ever)
            if (r == TIMEOUT || r <= 0) {
                // failing for RADIO_GONE_S: unplugged, TX ends (before, it retried for ever)
                const double now = mono_s();
                if (fail_since_ < 0) {
                    fail_since_ = now;
                    if (r != TIMEOUT)
                        avm_logf("SoapySDR writeStream error: %s -- dropping the rest of this chunk.\n",
                                 A.errToStr ? A.errToStr(r) : "?");
                }
                if (now - fail_since_ > RADIO_GONE_S) {
                    avm_logf("ERROR: %s TX: writes failing for %.0f s -- the radio has gone (unplugged?)\n",
                             cfg_.driver.c_str(), RADIO_GONE_S);
                    failed_ = true;
                    return;
                }
                if (r == TIMEOUT) continue;
                std::this_thread::sleep_for(std::chrono::milliseconds(5));
                if (r == 0) continue;
                return;
            }
            fail_since_ = -1;
            pos += (size_t)r;
            // the writer sends all the time (silence between waveforms), so it keeps pace
            // with the radio: RADIO_GONE_S behind real time, the radio has stopped taking
            // samples (an unplugged Pluto's writes "succeed", each after a network stall)
            const double now = mono_s();
            if (rt_t0_ < 0) rt_t0_ = now;
            rt_sent_ += (double)r;
            if ((now - rt_t0_) - rt_sent_ / rate > RADIO_GONE_S) {
                avm_logf("ERROR: %s TX: %.0f s behind real time -- the radio has gone (unplugged?)\n",
                         cfg_.driver.c_str(), RADIO_GONE_S);
                failed_ = true;
                return;
            }
        }
    }
    void pace(size_t n) {
        double now = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
        if (pace_t0_ < 0) { pace_t0_ = now; pace_sent_ = 0; }
        pace_sent_ += (double)n;
        double ahead = pace_sent_ / rate - (now - pace_t0_);
        if (ahead < -1.0) { pace_t0_ = now; pace_sent_ = (double)n; }
        else if (ahead > 0.25) std::this_thread::sleep_for(std::chrono::duration<double>(ahead - 0.25));
    }
    TxConfig cfg_;
    void* dev_ = nullptr;
    void* stream_ = nullptr;
    std::thread th_;
    std::mutex mu_;
    std::condition_variable ready_, space_, done_;
    std::deque<std::vector<std::complex<float>>> q_;
    bool stop_ = false, busy_ = false;
    std::atomic<bool> failed_{false}, abort_{false};
    double fail_since_ = -1, rt_t0_ = -1, rt_sent_ = 0;
    double pace_t0_ = -1, pace_sent_ = 0;
    std::unique_ptr<FileWatcher> gain_watch_;
};

}  // namespace sdr
