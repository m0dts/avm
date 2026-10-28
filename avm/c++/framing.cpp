// framing.cpp -- C++ port of AVM's fragment framing: the packing core of
// media_tx_framer.py (Framer) and the record handling of media_rx_player.py's
// run_player_loop and VideoWorker (Deframer). Same bytes on air, same
// behaviour on lost fragments.
//
// A fragment is [2-byte big-endian length][TLV records][zero padding] to
// exactly fragment_size bytes. A record is [type][2-byte length][payload],
// except AUDIO_PACKET_FIXED ([type][payload], length = the last audio
// packet's).
//
// Plain C interface for ctypes, as wavelet_core.cpp:
//   g++ -O2 -shared -fPIC -std=c++17 -o libframing.so framing.cpp
//   zig c++ -O2 -shared -std=c++17 -o framing.dll framing.cpp
#include <cstdint>
#include <cstring>
#include <deque>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#if defined(_WIN32)
#define FR_API extern "C" __declspec(dllexport)
#else
#define FR_API extern "C" __attribute__((visibility("default")))
#endif

namespace {

using Bytes = std::vector<uint8_t>;

// ---------------------------------------------------------------- the format (media_tx_framer.py)
const int AUDIO_CONFIG_TYPE = 0x01;
const int AUDIO_PACKET_TYPE = 0x02;
const int VIDEO_CONFIG_TYPE = 0x03;
const int VIDEO_PACKET_TYPE = 0x04;
const int VIDEO_PACKET_START_TYPE = 0x05;
const int VIDEO_PACKET_CONT_TYPE = 0x06;
const int AUDIO_PACKET_START_TYPE = 0x07;
const int AUDIO_PACKET_CONT_TYPE = 0x08;
const int AUDIO_PACKET_FIXED_TYPE = 0x0A;
const int STATION_ID_TYPE = 0x0B;
const int MESSAGE_TYPE = 0x0C;
const int STATION_ID_FULL_TYPE = 0x0D;
const int RECORD_HEADER_LEN = 3;
const int STATION_ID_LEN = 20;
const int VIDEO_CONFIG_REPEAT_EVERY = 25;
const double VIDEO_CONFIG_REPEAT_S = 2.0;
const int MESSAGE_PIECE_HEADER = 3;
const int MESSAGE_MIN_PIECE = 4;
const int MIN_START_CHUNK = 16;
// receiver (media_rx_player.py)
const int STATION_ID_VOTE_CYCLES = 4;
const double STATION_ID_RESET_GAP_S = 3.0;
const int KNOWN_AUDIO_PROFILES[] = {0x00, 0x01};   // AUDIO_PROFILES keys

uint32_t crc32(const uint8_t* p, size_t n) {   // zlib.crc32
    struct Table { uint32_t t[256]; };
    static const Table table_ = [] {      // (built once, thread-safely)
        Table tb;
        for (uint32_t i = 0; i < 256; ++i) {
            uint32_t c = i;
            for (int k = 0; k < 8; ++k) c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
            tb.t[i] = c;
        }
        return tb;
    }();
    const uint32_t* table = table_.t;
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; ++i) c = table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}
inline void put_u16(Bytes& b, uint32_t v) { b.push_back((uint8_t)(v >> 8)); b.push_back((uint8_t)v); }
inline void put_u32(Bytes& b, uint32_t v) {
    b.push_back((uint8_t)(v >> 24)); b.push_back((uint8_t)(v >> 16)); b.push_back((uint8_t)(v >> 8)); b.push_back((uint8_t)v);
}
inline uint32_t get_u16(const uint8_t* p) { return ((uint32_t)p[0] << 8) | p[1]; }
inline uint32_t get_u32(const uint8_t* p) { return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3]; }
inline void put_record_header(Bytes& b, int type, size_t len) { b.push_back((uint8_t)type); put_u16(b, (uint32_t)len); }

// ---------------------------------------------------------------- Framer (media_tx_framer.py main())
struct VideoItem {
    Bytes data;
    int kind;   // 0 = not a keyframe, 1 = keyframe, 2 = "cont" (remainder of a packet split last tick)
};

struct Framer {
    int fragment_size, budget;
    Bytes station_id_payload;           // empty = none
    long long station_id_seq = 0;
    int audio_profile_id = -1;          // -1 = not known yet
    long long last_audio_packet_len = -1;   // -1 = None
    Bytes video_config_record;          // the whole record; empty = not known yet
    double video_config_t = 0.0;
    int video_packets_since_config = 0;
    Bytes block;
    std::deque<VideoItem> video_carry;
    // the current text message (MessageFile)
    Bytes msg_data;                     // empty = none
    int msg_id = 0;
    size_t msg_pos = 0;
    // this tick's input and output
    std::vector<Bytes> audio_in;
    std::vector<VideoItem> video_in;
    std::vector<Bytes> out;

    Bytes fresh_block() {
        Bytes o;
        if (!station_id_payload.empty()) {
            size_t idx = (size_t)(station_id_seq % (long long)station_id_payload.size());
            station_id_seq += 1;
            put_record_header(o, STATION_ID_TYPE, 2);
            o.push_back((uint8_t)idx);
            o.push_back(station_id_payload[idx]);
        }
        if (audio_profile_id < 0) return o;
        long long slot_len = last_audio_packet_len > 0 ? last_audio_packet_len : 0;   // "or 0"
        put_record_header(o, AUDIO_CONFIG_TYPE, 3);
        o.push_back((uint8_t)audio_profile_id);
        put_u16(o, (uint32_t)slot_len);
        return o;
    }

    bool message_piece(long long room, Bytes& rec) {
        long long avail = (long long)msg_data.size() - (long long)msg_pos;
        long long n = room - RECORD_HEADER_LEN - MESSAGE_PIECE_HEADER;
        if (avail < n) n = avail;
        if (msg_data.empty()) return false;
        long long need = MESSAGE_MIN_PIECE < avail ? MESSAGE_MIN_PIECE : avail;
        if (n < need) return false;
        rec.clear();
        put_record_header(rec, MESSAGE_TYPE, (size_t)(MESSAGE_PIECE_HEADER + n));
        rec.push_back((uint8_t)msg_id);
        rec.push_back((uint8_t)msg_data.size());
        rec.push_back((uint8_t)msg_pos);
        rec.insert(rec.end(), msg_data.begin() + msg_pos, msg_data.begin() + msg_pos + n);
        msg_pos = (msg_pos + (size_t)n) % msg_data.size();
        return true;
    }

    void fill_spare() {
        long long room = budget - (long long)block.size();
        Bytes rec;
        if (message_piece(room, rec)) {
            block.insert(block.end(), rec.begin(), rec.end());
            return;
        }
        if (!station_id_payload.empty() && room >= RECORD_HEADER_LEN + (long long)station_id_payload.size()) {
            put_record_header(block, STATION_ID_FULL_TYPE, station_id_payload.size());
            block.insert(block.end(), station_id_payload.begin(), station_id_payload.end());
        }
    }

    void flush_block() {
        if (block.empty()) return;
        fill_spare();
        Bytes f;
        f.reserve(fragment_size);
        put_u16(f, (uint32_t)block.size());
        f.insert(f.end(), block.begin(), block.end());
        f.resize(fragment_size, 0);
        out.push_back(std::move(f));
        block = fresh_block();
    }

    void append_audio_packet(const Bytes& raw) {
        Bytes rec_whole;
        if ((long long)raw.size() == last_audio_packet_len) {
            rec_whole.push_back((uint8_t)AUDIO_PACKET_FIXED_TYPE);
        } else {
            put_record_header(rec_whole, AUDIO_PACKET_TYPE, raw.size());
        }
        rec_whole.insert(rec_whole.end(), raw.begin(), raw.end());
        if ((long long)(block.size() + rec_whole.size()) <= budget) {
            block.insert(block.end(), rec_whole.begin(), rec_whole.end());
            last_audio_packet_len = (long long)raw.size();
            return;
        }
        if ((long long)rec_whole.size() > budget) flush_block();
        long long header_len = RECORD_HEADER_LEN + 4;
        long long remaining = budget - (long long)block.size();
        if (remaining <= header_len) {
            flush_block();
            remaining = budget - (long long)block.size();
            if ((long long)(block.size() + rec_whole.size()) <= budget) {
                // it fits whole in the fresh fragment (a START holding the whole
                // packet, with no CONT, was never completed by a receiver)
                block.insert(block.end(), rec_whole.begin(), rec_whole.end());
                last_audio_packet_len = (long long)raw.size();
                return;
            }
        }
        size_t first_len = (size_t)(remaining - header_len);
        if (first_len > raw.size()) first_len = raw.size();
        put_record_header(block, AUDIO_PACKET_START_TYPE, 4 + first_len);
        put_u32(block, (uint32_t)raw.size());
        block.insert(block.end(), raw.begin(), raw.begin() + first_len);
        flush_block();
        size_t pos = first_len;
        while (pos < raw.size()) {
            size_t max_cont = (size_t)(budget - (long long)block.size() - RECORD_HEADER_LEN);
            size_t n = raw.size() - pos < max_cont ? raw.size() - pos : max_cont;
            put_record_header(block, AUDIO_PACKET_CONT_TYPE, n);
            block.insert(block.end(), raw.begin() + pos, raw.begin() + pos + n);
            pos += n;
            if (pos < raw.size()) flush_block();
        }
        last_audio_packet_len = (long long)raw.size();
    }

    // assembler_tick
    void tick(double now) {
        out.clear();
        std::vector<VideoItem> items(video_carry.begin(), video_carry.end());
        video_carry.clear();
        for (auto& v : video_in) items.push_back(std::move(v));
        video_in.clear();
        for (const Bytes& raw : audio_in) append_audio_packet(raw);
        audio_in.clear();
        for (size_t i = 0; i < items.size(); ++i) {
            VideoItem& it = items[i];
            if (it.kind == 2) {   // the remainder of a packet split last tick
                long long room_ll = budget - (long long)block.size() - RECORD_HEADER_LEN;
                if (room_ll <= 0) {
                    // this tick's audio filled the fragment: it waits for the next tick
                    for (size_t k = i; k < items.size(); ++k) video_carry.push_back(std::move(items[k]));
                    break;
                }
                size_t room = (size_t)room_ll;
                size_t n = it.data.size() < room ? it.data.size() : room;
                put_record_header(block, VIDEO_PACKET_CONT_TYPE, n);
                block.insert(block.end(), it.data.begin(), it.data.begin() + n);
                if (n < it.data.size()) {
                    video_carry.push_back({Bytes(it.data.begin() + n, it.data.end()), 2});
                    for (size_t k = i + 1; k < items.size(); ++k) video_carry.push_back(std::move(items[k]));
                    break;
                }
                continue;
            }
            if (it.kind == 1 || video_packets_since_config == 0 || now - video_config_t >= VIDEO_CONFIG_REPEAT_S) {
                const Bytes& cfg = video_config_record;
                if ((long long)(block.size() + cfg.size()) > budget) {
                    for (size_t k = i; k < items.size(); ++k) video_carry.push_back(std::move(items[k]));
                    break;
                }
                block.insert(block.end(), cfg.begin(), cfg.end());
                video_config_t = now;
            }
            const Bytes& au = it.data;
            long long room = budget - (long long)block.size();
            if (RECORD_HEADER_LEN + (long long)au.size() <= room) {
                put_record_header(block, VIDEO_PACKET_TYPE, au.size());
                block.insert(block.end(), au.begin(), au.end());
            } else if (room - RECORD_HEADER_LEN - 4 >= MIN_START_CHUNK) {
                size_t n = (size_t)(room - RECORD_HEADER_LEN - 4);
                put_record_header(block, VIDEO_PACKET_START_TYPE, 4 + n);
                put_u32(block, (uint32_t)au.size());
                block.insert(block.end(), au.begin(), au.begin() + n);
                video_carry.push_back({Bytes(au.begin() + n, au.end()), 2});
                for (size_t k = i + 1; k < items.size(); ++k) video_carry.push_back(std::move(items[k]));
                video_packets_since_config = (video_packets_since_config + 1) % VIDEO_CONFIG_REPEAT_EVERY;
                break;
            } else {
                for (size_t k = i; k < items.size(); ++k) video_carry.push_back(std::move(items[k]));
                break;
            }
            video_packets_since_config = (video_packets_since_config + 1) % VIDEO_CONFIG_REPEAT_EVERY;
        }
        flush_block();
    }
};

// ---------------------------------------------------------------- Deframer (media_rx_player.py)
// event types reported to the caller
enum {
    EV_AUDIO_CONFIG = 1,     // [profile id][2-byte slot length]
    EV_AUDIO_PACKET = 2,     // a complete audio packet
    EV_VIDEO_CONFIG = 3,     // the VIDEO_CONFIG payload: [w][h][fps*1000] (2 bytes each) + extradata
    EV_VIDEO_PACKET = 4,     // a complete video packet
    EV_STATION_ID = 5,       // a confirmed station ID (20 bytes, as sent)
    EV_STATION_ID_TENTATIVE = 6,   // the best guess before the CRC holds (first one only)
    EV_MESSAGE = 7,          // [4-byte timestamp][UTF-8 text], once per message
    EV_STATION_ID_RESET = 8, // signal gap: the station ID was forgotten
    EV_AUDIO_PROFILE_CHANGED = 9,  // [old][new]
};

struct Event {
    int type;
    Bytes data;
};

struct Deframer {
    int fragment_size;
    // audio
    int audio_profile_id = -1;
    bool audio_ready = false;         // a decoder exists (a known profile was configured)
    long long fixed_len = -1;         // audio_fixed_len_state["len"]
    bool reasm_active = false;
    uint32_t reasm_total = 0;
    Bytes reasm_buf;
    long long reasm_id = 0;           // identity, for the end-of-block stale check
    // video (VideoWorker)
    bool vre_active = false;
    uint32_t vre_total = 0;
    Bytes vre_buf;
    // station ID
    Bytes sid_buf = Bytes(STATION_ID_LEN + 4, 0);
    std::vector<std::deque<uint8_t>> sid_hist = std::vector<std::deque<uint8_t>>(STATION_ID_LEN + 4);
    bool sid_shown_set = false;
    Bytes sid_shown;
    bool tentative_set = false;
    Bytes tentative_shown;
    bool have_last_t = false;
    double last_block_t = 0;
    // messages
    std::vector<std::pair<std::pair<int, int>, std::pair<Bytes, Bytes>>> message_parts;   // insertion ordered
    std::set<std::pair<uint32_t, Bytes>> messages_seen;
    std::vector<Event> events;

    static uint8_t majority_vote(const std::deque<uint8_t>& h) {
        int counts[256] = {0};
        int best = 0;
        for (uint8_t v : h) { counts[v] += 1; if (counts[v] > best) best = counts[v]; }
        for (auto it = h.rbegin(); it != h.rend(); ++it)
            if (counts[*it] == best) return *it;
        return 0;
    }
    void emit(int type, const Bytes& d) { events.push_back({type, d}); }

    void process_audio_packet(const Bytes& raw) { emit(EV_AUDIO_PACKET, raw); }

    void push(const uint8_t* block, size_t blen, double now) {
        if (have_last_t && now - last_block_t > STATION_ID_RESET_GAP_S) {
            bool had_state = sid_shown_set;
            for (auto& h : sid_hist) { if (!h.empty()) had_state = true; h.clear(); }
            std::fill(sid_buf.begin(), sid_buf.end(), 0);
            sid_shown_set = false;
            tentative_set = false;
            if (had_state) emit(EV_STATION_ID_RESET, Bytes());
        }
        have_last_t = true;
        last_block_t = now;
        if (blen < 2) return;
        size_t real_len = get_u16(block);
        const uint8_t* payload = block + 2;
        size_t plen = blen - 2 < real_len ? blen - 2 : real_len;

        bool before_active = reasm_active;
        long long before_id = reasm_id;
        size_t before_len = reasm_buf.size();

        size_t pos = 0;
        while (pos < plen) {
            int rtype = payload[pos];
            const uint8_t* rp;
            size_t rlen;
            if (rtype == AUDIO_PACKET_FIXED_TYPE) {
                if (fixed_len < 0 || pos + 1 + (size_t)fixed_len > plen) break;
                rp = payload + pos + 1;
                rlen = (size_t)fixed_len;
                pos += 1 + rlen;
            } else {
                if (pos + RECORD_HEADER_LEN > plen) break;
                rlen = get_u16(payload + pos + 1);
                pos += RECORD_HEADER_LEN;
                if (pos + rlen > plen) break;
                rp = payload + pos;
                pos += rlen;
                if (rtype == AUDIO_PACKET_TYPE) fixed_len = (long long)rlen;
            }
            record(rtype, rp, rlen);
        }
        // a split pending before this block that didn't grow in it: its
        // continuation was lost -- drop it (media_rx_player.py)
        if (before_active && reasm_active && reasm_id == before_id && reasm_buf.size() == before_len)
            reasm_active = false;
    }

    void record(int rtype, const uint8_t* rp, size_t rlen) {
        if (rtype == AUDIO_CONFIG_TYPE && rlen >= 1) {
            int prof = rp[0];
            bool known = false;
            for (int k : KNOWN_AUDIO_PROFILES) if (k == prof) known = true;
            if (!known) return;
            if (rlen >= 3) {
                uint32_t slot = get_u16(rp + 1);
                if (slot > 0) fixed_len = slot;
            }
            if (audio_profile_id >= 0 && prof != audio_profile_id) {
                emit(EV_AUDIO_PROFILE_CHANGED, Bytes{(uint8_t)audio_profile_id, (uint8_t)prof});
                audio_ready = false;
                reasm_active = false;
            }
            audio_profile_id = prof;
            if (!audio_ready) {
                audio_ready = true;
                emit(EV_AUDIO_CONFIG, Bytes(rp, rp + rlen));
            }
        } else if ((rtype == AUDIO_PACKET_TYPE || rtype == AUDIO_PACKET_FIXED_TYPE) && audio_ready) {
            reasm_active = false;
            process_audio_packet(Bytes(rp, rp + rlen));
        } else if (rtype == AUDIO_PACKET_START_TYPE && rlen >= 4 && audio_ready) {
            reasm_active = true;
            reasm_id += 1;
            reasm_total = get_u32(rp);
            reasm_buf.assign(rp + 4, rp + rlen);
            if (reasm_buf.size() >= reasm_total) {   // the whole packet in the START (older transmitters)
                Bytes complete(reasm_buf.begin(), reasm_buf.begin() + reasm_total);
                reasm_active = false;
                fixed_len = (long long)complete.size();
                process_audio_packet(complete);
            }
        } else if (rtype == AUDIO_PACKET_CONT_TYPE && audio_ready) {
            if (!reasm_active) return;
            reasm_buf.insert(reasm_buf.end(), rp, rp + rlen);
            if (reasm_buf.size() >= reasm_total) {
                Bytes complete(reasm_buf.begin(), reasm_buf.begin() + reasm_total);
                reasm_active = false;
                fixed_len = (long long)complete.size();
                process_audio_packet(complete);
            }
        } else if (rtype == STATION_ID_TYPE && rlen >= 2) {
            int idx = rp[0];
            if (idx < (int)sid_buf.size()) {
                auto& h = sid_hist[idx];
                h.push_back(rp[1]);
                if ((int)h.size() > STATION_ID_VOTE_CYCLES) h.pop_front();
                sid_buf[idx] = majority_vote(h);
                if (crc32(sid_buf.data(), STATION_ID_LEN) == get_u32(sid_buf.data() + STATION_ID_LEN)) {
                    Bytes sid(sid_buf.begin(), sid_buf.begin() + STATION_ID_LEN);
                    if (!sid_shown_set || sid != sid_shown) {
                        sid_shown = sid;
                        sid_shown_set = true;
                        tentative_set = false;
                        emit(EV_STATION_ID, sid);
                    }
                } else if (!sid_shown_set) {
                    Bytes t(sid_buf.begin(), sid_buf.begin() + STATION_ID_LEN);
                    if (!tentative_set || t != tentative_shown) {
                        tentative_shown = t;
                        tentative_set = true;
                        emit(EV_STATION_ID_TENTATIVE, t);
                    }
                }
            }
        } else if (rtype == STATION_ID_FULL_TYPE && rlen == STATION_ID_LEN + 4) {
            if (crc32(rp, STATION_ID_LEN) == get_u32(rp + STATION_ID_LEN)) {
                sid_buf.assign(rp, rp + rlen);
                for (size_t k = 0; k < sid_buf.size(); ++k) { sid_hist[k].clear(); sid_hist[k].push_back(sid_buf[k]); }
                Bytes sid(rp, rp + STATION_ID_LEN);
                if (!sid_shown_set || sid != sid_shown) {
                    sid_shown = sid;
                    sid_shown_set = true;
                    tentative_set = false;
                    emit(EV_STATION_ID, sid);
                }
            }
        } else if (rtype == MESSAGE_TYPE && rlen > 3) {
            int mid = rp[0], total = rp[1], offset = rp[2];
            size_t n = rlen - 3;
            if (8 < total && offset + n <= (size_t)total) {
                std::pair<int, int> key{mid, total};
                size_t ki = message_parts.size();
                for (size_t k = 0; k < message_parts.size(); ++k) if (message_parts[k].first == key) ki = k;
                if (ki == message_parts.size()) {
                    if (message_parts.size() >= 4) message_parts.erase(message_parts.begin());
                    message_parts.push_back({key, {Bytes(total, 0), Bytes(total, 0)}});
                    ki = message_parts.size() - 1;
                }
                Bytes& buf = message_parts[ki].second.first;
                Bytes& have = message_parts[ki].second.second;
                std::memcpy(buf.data() + offset, rp + 3, n);
                std::memset(have.data() + offset, 1, n);
                bool all = true;
                for (uint8_t h : have) if (!h) { all = false; break; }
                if (all) {
                    if (crc32(buf.data(), total - 4) == get_u32(buf.data() + total - 4)) {
                        Bytes body(buf.begin(), buf.end() - 4);
                        message_parts.erase(message_parts.begin() + ki);
                        uint32_t ts = get_u32(body.data());
                        Bytes text(body.begin() + 4, body.end());
                        if (messages_seen.insert({ts, text}).second) emit(EV_MESSAGE, body);
                    } else {
                        std::fill(have.begin(), have.end(), 0);
                    }
                }
            }
        } else if (rtype == VIDEO_CONFIG_TYPE && rlen >= 6) {
            emit(EV_VIDEO_CONFIG, Bytes(rp, rp + rlen));
        } else if (rtype == VIDEO_PACKET_TYPE) {
            vre_active = false;
            emit(EV_VIDEO_PACKET, Bytes(rp, rp + rlen));
        } else if (rtype == VIDEO_PACKET_START_TYPE && rlen >= 4) {
            vre_active = true;
            vre_total = get_u32(rp);
            vre_buf.assign(rp + 4, rp + rlen);
        } else if (rtype == VIDEO_PACKET_CONT_TYPE) {
            if (!vre_active) return;
            vre_buf.insert(vre_buf.end(), rp, rp + rlen);
            if (vre_buf.size() >= vre_total) {
                Bytes complete(vre_buf.begin(), vre_buf.begin() + vre_total);
                vre_active = false;
                emit(EV_VIDEO_PACKET, complete);
            }
        }
    }
};

}  // namespace

// ---------------------------------------------------------------- C interface: Framer
FR_API void* fr_create(int fragment_size, const char* station_id, int msg_id0) {
    Framer* f = new Framer();
    f->fragment_size = fragment_size;
    f->budget = fragment_size - 2;
    f->msg_id = msg_id0 & 0xFF;
    if (station_id && *station_id) {
        Bytes raw(station_id, station_id + std::strlen(station_id));
        if (raw.size() > (size_t)STATION_ID_LEN) raw.resize(STATION_ID_LEN);
        raw.resize(STATION_ID_LEN, ' ');
        uint32_t c = crc32(raw.data(), raw.size());
        put_u32(raw, c);
        f->station_id_payload = raw;
    }
    f->block = f->fresh_block();
    return f;
}
FR_API void fr_free(void* h) { delete (Framer*)h; }
FR_API void fr_set_audio_profile(void* h, int id) { ((Framer*)h)->audio_profile_id = id; }
// the whole VIDEO_CONFIG record ([type][len][payload]), as media_tx_framer.py keeps it
FR_API void fr_set_video_config(void* h, const uint8_t* rec, int len) {
    ((Framer*)h)->video_config_record.assign(rec, rec + len);
}
// the message bytes (build_message), or len 0 for none; a change restarts it under a new id
FR_API void fr_set_message(void* h, const uint8_t* data, int len) {
    Framer* f = (Framer*)h;
    Bytes d(data, data + len);
    if (d != f->msg_data) {
        f->msg_data = d;
        f->msg_pos = 0;
        f->msg_id = (f->msg_id + 1) & 0xFF;
    }
}
FR_API void fr_add_audio(void* h, const uint8_t* p, int len) { ((Framer*)h)->audio_in.emplace_back(p, p + len); }
FR_API void fr_add_video(void* h, const uint8_t* p, int len, int keyframe) {
    ((Framer*)h)->video_in.push_back({Bytes(p, p + len), keyframe ? 1 : 0});
}
// one assembler tick at time `now` (seconds): returns the number of fragments made
FR_API int fr_tick(void* h, double now) {
    Framer* f = (Framer*)h;
    f->tick(now);
    return (int)f->out.size();
}
FR_API void fr_fragment(void* h, int i, uint8_t* out) {
    Framer* f = (Framer*)h;
    std::memcpy(out, f->out[i].data(), f->out[i].size());
}

// ---------------------------------------------------------------- C interface: Deframer
FR_API void* df_create(int fragment_size) {
    Deframer* d = new Deframer();
    d->fragment_size = fragment_size;
    return d;
}
FR_API void df_free(void* h) { delete (Deframer*)h; }
// one received fragment at time `now`: returns the number of events it gave
FR_API int df_push(void* h, const uint8_t* block, int len, double now) {
    Deframer* d = (Deframer*)h;
    d->events.clear();
    d->push(block, (size_t)len, now);
    return (int)d->events.size();
}
FR_API int df_event_type(void* h, int i) { return ((Deframer*)h)->events[i].type; }
FR_API int df_event_len(void* h, int i) { return (int)((Deframer*)h)->events[i].data.size(); }
FR_API void df_event_data(void* h, int i, uint8_t* out) {
    const Bytes& b = ((Deframer*)h)->events[i].data;
    if (!b.empty()) std::memcpy(out, b.data(), b.size());
}
