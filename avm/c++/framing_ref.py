"""Reference for framing.cpp: media_tx_framer.py's packing (main()'s
_fresh_block / fill_spare / flush_block / append_audio_packet /
assembler_tick) and media_rx_player.py's record handling (run_player_loop
and VideoWorker), lifted line for line into two plain classes with no
threads, pipes or decoders -- so they can be driven tick by tick and
compared with the C++ byte for byte. The constants and helpers come from
the production modules themselves."""
import os
import struct
import sys
import zlib
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import media_tx_framer as F
import media_rx_player as R

EV_AUDIO_CONFIG, EV_AUDIO_PACKET, EV_VIDEO_CONFIG, EV_VIDEO_PACKET = 1, 2, 3, 4
EV_STATION_ID, EV_STATION_ID_TENTATIVE, EV_MESSAGE, EV_STATION_ID_RESET = 5, 6, 7, 8
EV_AUDIO_PROFILE_CHANGED = 9


class RefFramer:
    def __init__(self, fragment_size, station_id=None, msg_id0=0):
        self.fragment_size = fragment_size
        self.budget = fragment_size - 2
        self.state = {"audio_profile_id": None, "video_config_record": None, "last_audio_packet_len": None,
                      "station_id_payload": F._build_station_id_payload(station_id) if station_id else None,
                      "station_id_seq": 0}
        self.video_packets_since_config = 0
        self.video_carry = []
        self.msg_data, self.msg_id, self.msg_pos = None, msg_id0 & 0xFF, 0
        self.block = self._fresh_block()
        self.out = []

    def set_message(self, data):   # MessageFile.poll, given the message bytes (None = none)
        if data != self.msg_data:
            self.msg_data, self.msg_pos = data, 0
            self.msg_id = (self.msg_id + 1) & 0xFF

    def _piece(self, room):        # MessageFile.piece
        n = min(room - F.RECORD_HEADER_LEN - F.MESSAGE_PIECE_HEADER, len(self.msg_data or b"") - self.msg_pos)
        if self.msg_data is None or n < min(F.MESSAGE_MIN_PIECE, len(self.msg_data) - self.msg_pos):
            return None
        payload = bytes([self.msg_id, len(self.msg_data), self.msg_pos]) + self.msg_data[self.msg_pos:self.msg_pos + n]
        self.msg_pos = (self.msg_pos + n) % len(self.msg_data)
        return struct.pack(">BH", F.MESSAGE_TYPE, len(payload)) + payload

    def _fresh_block(self):
        state = self.state
        out = bytearray()
        id_payload = state["station_id_payload"]
        if id_payload is not None:
            idx = state["station_id_seq"] % len(id_payload)
            state["station_id_seq"] += 1
            rec_payload = bytes([idx, id_payload[idx]])
            out.extend(struct.pack(">BH", F.STATION_ID_TYPE, len(rec_payload)) + rec_payload)
        if state["audio_profile_id"] is None:
            return out
        slot_len = state["last_audio_packet_len"] or 0
        payload = bytes([state["audio_profile_id"]]) + struct.pack(">H", slot_len)
        out.extend(struct.pack(">BH", F.AUDIO_CONFIG_TYPE, len(payload)) + payload)
        return out

    def _fill_spare(self):
        room = self.budget - len(self.block)
        rec = self._piece(room)
        if rec is not None:
            self.block.extend(rec)
            return
        id_payload = self.state["station_id_payload"]
        if id_payload is not None and room >= F.RECORD_HEADER_LEN + len(id_payload):
            self.block.extend(struct.pack(">BH", F.STATION_ID_FULL_TYPE, len(id_payload)) + id_payload)

    def _flush_block(self):
        if not self.block:
            return
        self._fill_spare()
        out = bytearray(struct.pack(">H", len(self.block)))
        out.extend(self.block)
        out.extend(b"\x00" * (self.fragment_size - len(out)))
        self.out.append(bytes(out))
        self.block = self._fresh_block()

    def _append_audio_packet(self, raw):
        state, budget = self.state, self.budget
        if len(raw) == state["last_audio_packet_len"]:
            rec_whole = struct.pack(">B", F.AUDIO_PACKET_FIXED_TYPE) + raw
        else:
            rec_whole = struct.pack(">BH", F.AUDIO_PACKET_TYPE, len(raw)) + raw
        if len(self.block) + len(rec_whole) <= budget:
            self.block.extend(rec_whole)
            state["last_audio_packet_len"] = len(raw)
            return
        if len(rec_whole) > budget:
            self._flush_block()
        header_len = F.RECORD_HEADER_LEN + 4
        remaining_in_block = budget - len(self.block)
        if remaining_in_block <= header_len:
            self._flush_block()
            remaining_in_block = budget - len(self.block)
            if len(self.block) + len(rec_whole) <= budget:   # (the fix in media_tx_framer.py)
                self.block.extend(rec_whole)
                state["last_audio_packet_len"] = len(raw)
                return
        first_chunk_len = remaining_in_block - header_len
        first, rest = raw[:first_chunk_len], raw[first_chunk_len:]
        payload = struct.pack(">I", len(raw)) + first
        self.block.extend(struct.pack(">BH", F.AUDIO_PACKET_START_TYPE, len(payload)) + payload)
        self._flush_block()
        while rest:
            max_cont_chunk = budget - len(self.block) - F.RECORD_HEADER_LEN
            chunk, rest = rest[:max_cont_chunk], rest[max_cont_chunk:]
            self.block.extend(struct.pack(">BH", F.AUDIO_PACKET_CONT_TYPE, len(chunk)) + chunk)
            if rest:
                self._flush_block()
        state["last_audio_packet_len"] = len(raw)

    def tick(self, audio_items, video_items_new, now):
        """assembler_tick: video_items_new [(bytes, is_keyframe)]; returns the fragments."""
        state, budget = self.state, self.budget
        self.out = []
        video_items = self.video_carry[:]
        self.video_carry.clear()
        video_items += list(video_items_new)
        for raw in audio_items:
            self._append_audio_packet(raw)
        MIN_START_CHUNK = 16
        for i, (au, is_keyframe) in enumerate(video_items):
            if is_keyframe == "cont":
                room = budget - len(self.block) - F.RECORD_HEADER_LEN
                if room <= 0:   # (the fix in media_tx_framer.py)
                    self.video_carry.extend(video_items[i:])
                    break
                chunk, rest = au[:room], au[room:]
                self.block.extend(struct.pack(">BH", F.VIDEO_PACKET_CONT_TYPE, len(chunk)) + chunk)
                if rest:
                    self.video_carry.append((rest, "cont"))
                    self.video_carry.extend(video_items[i + 1:])
                    break
                continue
            if (is_keyframe or self.video_packets_since_config == 0
                    or now - state.get("video_config_t", 0.0) >= F.VIDEO_CONFIG_REPEAT_S):
                cfg = state["video_config_record"]
                if len(self.block) + len(cfg) > budget:
                    self.video_carry.extend(video_items[i:])
                    break
                self.block.extend(cfg)
                state["video_config_t"] = now
            room = budget - len(self.block)
            if F.RECORD_HEADER_LEN + len(au) <= room:
                self.block.extend(struct.pack(">BH", F.VIDEO_PACKET_TYPE, len(au)) + au)
            elif room - F.RECORD_HEADER_LEN - 4 >= MIN_START_CHUNK:
                n = room - F.RECORD_HEADER_LEN - 4
                payload = struct.pack(">I", len(au)) + au[:n]
                self.block.extend(struct.pack(">BH", F.VIDEO_PACKET_START_TYPE, len(payload)) + payload)
                self.video_carry.append((au[n:], "cont"))
                self.video_carry.extend(video_items[i + 1:])
                self.video_packets_since_config = (self.video_packets_since_config + 1) % F.VIDEO_CONFIG_REPEAT_EVERY
                break
            else:
                self.video_carry.extend(video_items[i:])
                break
            self.video_packets_since_config = (self.video_packets_since_config + 1) % F.VIDEO_CONFIG_REPEAT_EVERY
        self._flush_block()
        return self.out


class RefDeframer:
    """run_player_loop's per-block record handling + VideoWorker's video
    reassembly, reporting events instead of decoding."""

    def __init__(self):
        self.audio_ready = False
        self.audio_profile_id = None
        self.audio_reassembly = None
        self.fixed = {"len": None}
        self.video_reassembly = None
        self.sid_buf = bytearray(R.STATION_ID_LEN + 4)
        self.sid_hist = [deque(maxlen=R.STATION_ID_VOTE_CYCLES) for _ in range(R.STATION_ID_LEN + 4)]
        self.sid_shown = None
        self.tentative_shown = None
        self.last_block_t = None
        self.message_parts = {}
        self.messages_seen = set()

    def push(self, block, now):
        ev = []
        if self.last_block_t is not None and now - self.last_block_t > R.STATION_ID_RESET_GAP_S:
            had_state = self.sid_shown is not None or any(self.sid_hist)
            self.sid_buf[:] = bytes(len(self.sid_buf))
            for h in self.sid_hist:
                h.clear()
            self.sid_shown = None
            self.tentative_shown = None
            if had_state:
                ev.append((EV_STATION_ID_RESET, b""))
        self.last_block_t = now
        if len(block) < 2:
            return ev
        real_len = struct.unpack_from(">H", block, 0)[0]
        payload = block[2:2 + real_len]
        reassembly_before = self.audio_reassembly
        buf_len_before = len(self.audio_reassembly["buf"]) if self.audio_reassembly else None
        for rtype, rpayload in R.parse_records(payload, self.fixed):
            if rtype == R.AUDIO_CONFIG_TYPE and len(rpayload) >= 1:
                profile = R.AUDIO_PROFILES.get(rpayload[0])
                if profile is None:
                    continue
                if len(rpayload) >= 3:
                    slot_len = struct.unpack_from(">H", rpayload, 1)[0]
                    if slot_len > 0:
                        self.fixed["len"] = slot_len
                if self.audio_profile_id is not None and rpayload[0] != self.audio_profile_id:
                    ev.append((EV_AUDIO_PROFILE_CHANGED, bytes([self.audio_profile_id, rpayload[0]])))
                    self.audio_ready = False
                    self.audio_reassembly = None
                self.audio_profile_id = rpayload[0]
                if not self.audio_ready:
                    self.audio_ready = True
                    ev.append((EV_AUDIO_CONFIG, bytes(rpayload)))
            elif rtype in (R.AUDIO_PACKET_TYPE, R.AUDIO_PACKET_FIXED_TYPE) and self.audio_ready:
                self.audio_reassembly = None
                ev.append((EV_AUDIO_PACKET, bytes(rpayload)))
            elif rtype == R.AUDIO_PACKET_START_TYPE and len(rpayload) >= 4 and self.audio_ready:
                total_len = struct.unpack_from(">I", rpayload, 0)[0]
                self.audio_reassembly = {"total_len": total_len, "buf": bytearray(rpayload[4:])}
                if len(self.audio_reassembly["buf"]) >= total_len:   # (the fix in media_rx_player.py)
                    complete = bytes(self.audio_reassembly["buf"][:total_len])
                    self.audio_reassembly = None
                    self.fixed["len"] = len(complete)
                    ev.append((EV_AUDIO_PACKET, complete))
            elif rtype == R.AUDIO_PACKET_CONT_TYPE and self.audio_ready:
                if self.audio_reassembly is None:
                    continue
                self.audio_reassembly["buf"].extend(rpayload)
                if len(self.audio_reassembly["buf"]) >= self.audio_reassembly["total_len"]:
                    complete = bytes(self.audio_reassembly["buf"][:self.audio_reassembly["total_len"]])
                    self.audio_reassembly = None
                    self.fixed["len"] = len(complete)
                    ev.append((EV_AUDIO_PACKET, complete))
            elif rtype == R.STATION_ID_TYPE and len(rpayload) >= 2:
                idx, data_byte = rpayload[0], rpayload[1]
                if idx < len(self.sid_buf):
                    self.sid_hist[idx].append(data_byte)
                    self.sid_buf[idx] = R._majority_vote(self.sid_hist[idx])
                    crc_expected = struct.unpack_from(">I", self.sid_buf, R.STATION_ID_LEN)[0]
                    crc_actual = zlib.crc32(bytes(self.sid_buf[:R.STATION_ID_LEN])) & 0xFFFFFFFF
                    if crc_actual == crc_expected:
                        sid = bytes(self.sid_buf[:R.STATION_ID_LEN])
                        if sid != self.sid_shown:
                            self.sid_shown = sid
                            self.tentative_shown = None
                            ev.append((EV_STATION_ID, sid))
                    elif self.sid_shown is None:
                        t = bytes(self.sid_buf[:R.STATION_ID_LEN])
                        if t != self.tentative_shown:
                            self.tentative_shown = t
                            ev.append((EV_STATION_ID_TENTATIVE, t))
            elif rtype == R.STATION_ID_FULL_TYPE and len(rpayload) == R.STATION_ID_LEN + 4:
                raw = bytes(rpayload[:R.STATION_ID_LEN])
                if zlib.crc32(raw) & 0xFFFFFFFF == struct.unpack_from(">I", rpayload, R.STATION_ID_LEN)[0]:
                    ok = bytes(rpayload)
                    self.sid_buf[:] = ok
                    for k, byte in enumerate(ok):
                        self.sid_hist[k].clear()
                        self.sid_hist[k].append(byte)
                    if raw != self.sid_shown:
                        self.sid_shown = raw
                        self.tentative_shown = None
                        ev.append((EV_STATION_ID, raw))
            elif rtype == R.MESSAGE_TYPE and len(rpayload) > 3:
                msg_id, total, offset = rpayload[0], rpayload[1], rpayload[2]
                piece = bytes(rpayload[3:])
                if 8 < total and offset + len(piece) <= total:
                    key = (msg_id, total)
                    if key not in self.message_parts:
                        if len(self.message_parts) >= 4:
                            self.message_parts.pop(next(iter(self.message_parts)))
                        self.message_parts[key] = (bytearray(total), bytearray(total))
                    buf, have = self.message_parts[key]
                    buf[offset:offset + len(piece)] = piece
                    have[offset:offset + len(piece)] = b"\x01" * len(piece)
                    if all(have):
                        body = bytes(buf[:-4])
                        if zlib.crc32(body) & 0xFFFFFFFF == struct.unpack_from(">I", buf, total - 4)[0]:
                            del self.message_parts[key]
                            ts = struct.unpack_from(">I", body, 0)[0]
                            text = body[4:]
                            if (ts, text) not in self.messages_seen:
                                self.messages_seen.add((ts, text))
                                ev.append((EV_MESSAGE, body))
                        else:
                            have[:] = bytes(total)
            elif rtype == R.VIDEO_CONFIG_TYPE and len(rpayload) >= 6:
                ev.append((EV_VIDEO_CONFIG, bytes(rpayload)))
            elif rtype == R.VIDEO_PACKET_TYPE:
                self.video_reassembly = None
                ev.append((EV_VIDEO_PACKET, bytes(rpayload)))
            elif rtype == R.VIDEO_PACKET_START_TYPE and len(rpayload) >= 4:
                total_len = struct.unpack_from(">I", rpayload, 0)[0]
                self.video_reassembly = {"total_len": total_len, "buf": bytearray(rpayload[4:])}
            elif rtype == R.VIDEO_PACKET_CONT_TYPE:
                if self.video_reassembly is None:
                    continue
                self.video_reassembly["buf"].extend(rpayload)
                if len(self.video_reassembly["buf"]) >= self.video_reassembly["total_len"]:
                    complete = bytes(self.video_reassembly["buf"][:self.video_reassembly["total_len"]])
                    self.video_reassembly = None
                    ev.append((EV_VIDEO_PACKET, complete))
        if (reassembly_before is not None and self.audio_reassembly is reassembly_before
                and len(self.audio_reassembly["buf"]) == buf_len_before):
            self.audio_reassembly = None
        return ev
