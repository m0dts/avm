"""openHPSDR protocol 1 radio (Hermes-Lite 2, Radioberry, Hermes and other
Metis-era boards) as a receive source over the network: the same
StdinReader-like interface as hpsdr_source.HpsdrRxSource (chunks, total_len,
_lock, eof, wait_for_more, sample_rate_hz), which hf_ofdm_rx.py uses for
--sdr hpsdr when the radio found answers protocol 1 rather than 2.

Receive only: MOX is never set and the TX IQ sent is zeros. Protocol 1 has
one UDP port (1024) both ways. The host sends 1032-byte "EP2" packets --
two 512-byte frames, each with five control bytes (C0-C4) that address one
register -- and the radio sends "EP6" packets of IQ, 24-bit I and Q plus a
16-bit mic sample per point, 126 points a packet with one receiver. The
control frames are sent steadily from a thread (the radio stops when they
stop: its watchdog), cycling through the registers set here.

Gain: a Hermes-Lite 2 / Radioberry has an LNA of -12 to +48 dB, set
directly from the gain (dB). Older boards have a 0-31 dB step attenuator:
gain 31 = no attenuation, as hpsdr_source."""
import select
import socket
import struct
import sys
import threading
import time

import numpy as np

PORT = 1024
SAMPLE_RATES = (48000, 96000, 192000, 384000)
POINTS = 63                   # IQ points per 512-byte frame (one receiver)
SEND_HZ = 100                 # control packets a second (one register pair each)
STALL_S = 5.0                 # no IQ this long: give up (the GUI restarts RX)
BOARDS = {0: "Metis", 1: "Hermes", 2: "Griffin", 4: "Angelia", 5: "Orion", 6: "Hermes Lite", 10: "Orion MkII"}
HL2_BOARD = 6
LNA_DB = (-12, 48)            # Hermes-Lite 2 LNA range
MAX_ATT_DB = 31


def _sockets():
    """One broadcast socket per local address (as hpsdr_source.discover), and an unbound one."""
    from hpsdr_source import _local_ipv4
    socks = []
    for src in [""] + _local_ipv4():
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sk.bind((src, 0))
            socks.append(sk)
        except OSError:
            continue
    return socks


def discover(host=None, timeout=0.6):
    """Protocol 1 radios answering discovery: [(ip, board name, busy, board id)]."""
    pkt = bytes([0xEF, 0xFE, 0x02]) + bytes(60)
    socks = _sockets()
    found = {}
    try:
        for sk in socks:
            for dest in ([host] if host else []) + ["255.255.255.255"]:
                try:
                    sk.sendto(pkt, (dest, PORT))
                except OSError:
                    pass
        t_end = time.monotonic() + timeout
        while socks and time.monotonic() < t_end:
            ready, _, _ = select.select(socks, [], [], max(0.0, t_end - time.monotonic()))
            if not ready:
                break
            for sk in ready:
                try:
                    d, (ip, port) = sk.recvfrom(2048)
                except OSError:
                    continue
                if len(d) >= 11 and d[0] == 0xEF and d[1] == 0xFE and d[2] in (2, 3):
                    found[ip] = (ip, BOARDS.get(d[10], f"board {d[10]}"), d[2] == 3, d[10])
    finally:
        for sk in socks:
            sk.close()
    return list(found.values())


class Hpsdr1RxSource:
    invert = False  # mirror the spectrum (hf_ofdm_rx.py --invert-spectrum)

    def __init__(self, server, freq_hz, sample_rate_hz=None, rx_gain_db=30, gain_file=None,
                 freq_offset_file=None, connect_timeout_s=3.0, radio=None):
        self.freq_hz = float(freq_hz)
        self._freq_offset_hz = 0.0
        self.rx_gain_db = float(rx_gain_db)
        want = float(sample_rate_hz or 96000)
        self.sample_rate_hz = float(next((r for r in SAMPLE_RATES if r >= want), SAMPLE_RATES[-1]))
        self.chunks = []
        self.total_len = 0
        self._lock = threading.Lock()
        self.eof = False
        self._new_data = threading.Event()
        self.lo_offset_hz = 0.0       # IQ arrives centred (the radio's DDC)
        self.radio_count = 0
        self.gaps = 0
        self.overloads = 0
        self._seq = 0
        self._reg = 0

        if radio is None:
            host = str(server or "").replace("ip:", "").split(":")[0].strip() or None
            radios = discover(host)
            if host:
                radios = [r for r in radios if r[0] == host] or radios
            if not radios:
                raise ConnectionError(f"HPSDR: no protocol 1 radio answered on the network"
                                      f"{' at ' + host if host else ''} -- check it's on and connected")
            radio = radios[0]
        self.host, self.board, busy, self.board_id = radio
        if busy:
            raise ConnectionError(f"HPSDR {self.board} {self.host} is in use by another program -- close it first")
        self.hl2 = self.board_id == HL2_BOARD

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind(("", 0))
        for _ in range(4):            # the registers once round before IQ starts
            self._send_control()
        self._start(True)
        threading.Thread(target=self._run, daemon=True, name="hpsdr1-read").start()
        threading.Thread(target=self._keepalive, daemon=True, name="hpsdr1-control").start()
        t_end = time.monotonic() + connect_timeout_s
        while not self.radio_count and not self.eof and time.monotonic() < t_end:
            time.sleep(0.02)
        if not self.radio_count:
            self.close()
            raise ConnectionError(f"HPSDR {self.board} {self.host} sent no IQ within {connect_timeout_s:g} s")
        print(f"HPSDR RX active: {self.board} at {self.host} (protocol 1) freq={self.freq_hz / 1e6:.4f}MHz "
              f"rate={self.sample_rate_hz / 1e3:g} kS/s {self._gain_text()}", file=sys.stderr)
        if gain_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(gain_file, self.set_gain)
        if freq_offset_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(freq_offset_file, self.set_freq_offset)

    # -- packets to the radio ----------------------------------------------
    def _start(self, run):
        try:
            self.sock.sendto(bytes([0xEF, 0xFE, 0x04, 0x01 if run else 0x00]) + bytes(60), (self.host, PORT))
        except OSError:
            pass

    def _lna(self):
        return int(min(LNA_DB[1], max(LNA_DB[0], round(self.rx_gain_db))))

    def _attenuation(self):
        return int(min(MAX_ATT_DB, max(0, round(MAX_ATT_DB - self.rx_gain_db))))

    def _gain_text(self):
        return f"LNA {self._lna():+d} dB" if self.hl2 else f"attenuator {self._attenuation()} dB"

    def _register(self, n):
        """C0-C4 of control register set n (MOX, bit 0 of C0, never set)."""
        f = int(round(self.freq_hz + self._freq_offset_hz)) & 0xFFFFFFFF
        if n == 0:        # general: sample rate; one receiver, duplex (separate TX frequency)
            speed = SAMPLE_RATES.index(int(self.sample_rate_hz))
            return bytes([0x00, speed, 0x00, 0x00, 0x04])
        if n == 1:        # receiver 1 frequency (Hz)
            return bytes([0x04]) + struct.pack(">I", f)
        if n == 2:        # TX frequency: the same (never used)
            return bytes([0x02]) + struct.pack(">I", f)
        # gain: HL2 LNA (C4 bit 6 = gain given, bits 5-0 = dB + 12); others the step attenuator
        if self.hl2:
            return bytes([0x14, 0x00, 0x00, 0x00, 0x40 | (self._lna() - LNA_DB[0])])
        return bytes([0x14, 0x00, 0x00, 0x00, 0x20 | self._attenuation()])

    def _send_control(self):
        """One EP2 packet: two frames, the next two registers in turn, silent TX IQ."""
        pkt = bytearray(1032)
        pkt[0:4] = bytes([0xEF, 0xFE, 0x01, 0x02])
        pkt[4:8] = struct.pack(">I", self._seq & 0xFFFFFFFF)
        self._seq += 1
        for k in range(2):
            o = 8 + 512 * k
            pkt[o:o + 3] = b"\x7f\x7f\x7f"
            pkt[o + 3:o + 8] = self._register(self._reg)
            self._reg = (self._reg + 1) % 4
        try:
            self.sock.sendto(bytes(pkt), (self.host, PORT))
        except OSError:
            pass

    def _keepalive(self):
        while not self.eof:
            self._send_control()
            time.sleep(1.0 / SEND_HZ)

    # -- control (live) ----------------------------------------------------
    def set_gain(self, db):
        self.rx_gain_db = float(db)
        print(f"HPSDR {self._gain_text()}", file=sys.stderr)

    def set_freq_offset(self, hz):
        self._freq_offset_hz = float(hz)

    # -- receiving ---------------------------------------------------------
    def _append(self, iq):
        if self.invert:
            iq = np.conjugate(iq)
        chunk = iq.tobytes()
        with self._lock:
            self.chunks.append(chunk)
            self.total_len += len(chunk)
        self.radio_count += len(iq)
        self._new_data.set()

    def _run(self):
        """Packets collected as they come (little work each: the socket buffer
        holds only ~70 ms at 384 kS/s on a Pi, rmem_max) and decoded in batches
        -- one numpy pass per batch, not per packet."""
        last_seq = None
        last_data = time.monotonic()
        last_overload_msg = 0.0
        # 16-bit full scale, not 24: the 24-bit ADC path puts weak signals near
        # -90 dBFS, under the receiver's sync search (a 10 kHz signal 18 dB over
        # the noise found no sync at all); in float the extra headroom never clips
        scale = np.float32(1.0 / (1 << 15))
        batch = []
        batch_t = time.monotonic()
        self.sock.settimeout(0.2)
        while not self.eof:
            try:
                d, (ip, port) = self.sock.recvfrom(2048)
            except socket.timeout:
                d = None
            except OSError:
                break
            if d is not None and ip == self.host and len(d) == 1032 and d[0:4] == b"\xef\xfe\x01\x06":
                batch.append(d)
            now = time.monotonic()
            if d is None and now - last_data > STALL_S and self.radio_count:
                print(f"HPSDR: no IQ for {STALL_S:g} s -- stopping", file=sys.stderr)
                break
            if not batch or (len(batch) < 64 and now - batch_t < 0.02 and d is not None):
                continue
            pk = np.frombuffer(b"".join(batch), np.uint8).reshape(-1, 1032)
            batch = []
            batch_t = now
            seqs = pk[:, 4:8].astype(np.uint32)
            seqs = seqs[:, 0] << 24 | seqs[:, 1] << 16 | seqs[:, 2] << 8 | seqs[:, 3]
            # frames: (packets, 2, 512); IQ points (packets, 2, 63, 8)
            fr = pk[:, 8:].reshape(-1, 2, 512)
            if np.any((fr[:, :, 3] >> 3 == 0) & (fr[:, :, 4] & 1 == 1)):
                self.overloads += 1
                if now - last_overload_msg > 5.0:
                    last_overload_msg = now
                    print("HPSDR: ADC overload -- lower the RX gain", file=sys.stderr)
            raw = fr[:, :, 8:8 + 8 * POINTS].reshape(-1, 2 * POINTS, 8).astype(np.int32)
            i = (raw[..., 0] << 24 | raw[..., 1] << 16 | raw[..., 2] << 8) >> 8     # sign-extended 24 bit
            q = (raw[..., 3] << 24 | raw[..., 4] << 16 | raw[..., 5] << 8) >> 8
            iq = (i.astype(np.float32) * scale + 1j * (q.astype(np.float32) * scale)).astype(np.complex64)
            # lost packets (sequence gaps): silence of the same length, keeping the timeline
            first = (int(seqs[0]) - 1) & 0xFFFFFFFF if last_seq is None else last_seq
            prev = np.concatenate((np.array([first], np.uint32), seqs[:-1]))
            gap = (seqs - prev - 1).astype(np.uint32)
            last_seq = int(seqs[-1])
            if np.any((gap > 0) & (gap < 1000)):
                parts = []
                for k in range(len(seqs)):
                    g = int(gap[k])
                    if 0 < g < 1000:
                        self.gaps += g
                        parts.append(np.zeros(g * 2 * POINTS, np.complex64))
                    parts.append(iq[k])
                print(f"[hpsdr] {int(gap[(gap > 0) & (gap < 1000)].sum())} packet(s) lost on the network "
                      f"-- filled with silence", file=sys.stderr)
                self._append(np.concatenate(parts))
            else:
                self._append(iq.reshape(-1))
            last_data = now
        self._end()

    def _end(self):
        with self._lock:
            self.eof = True
        self._new_data.set()

    # -- StdinReader interface ---------------------------------------------
    def wait_for_more(self, since_len, timeout=None):
        while True:
            with self._lock:
                if self.eof or self.total_len > since_len:
                    return
            self._new_data.clear()
            self._new_data.wait(timeout=timeout)

    def close(self):
        self.eof = True
        for _ in range(3):
            self._start(False)
            time.sleep(0.02)
        try:
            self.sock.close()
        except OSError:
            pass
