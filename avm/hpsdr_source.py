"""openHPSDR protocol 2 radio (ANAN-10/100, Hermes and similar) as a receive
source over the network: duck-types hf_ofdm_rx.py's StdinReader like
kiwi_source.KiwiRxSource (chunks, total_len, _lock, eof, wait_for_more,
sample_rate_hz). Standard library + numpy only, no SoapySDR module.

Receive only: the PTT bit, the PA enable and the TX drive are never set and
no TX IQ is sent. The radio's hardware watchdog is on, so it stops by itself
if this process dies without saying so.

One UDP socket for everything, as piHPSDR: the radio answers the socket the
commands came from, and its packets are told apart by their SOURCE port
(1035 DDC0 IQ, 1025 high-priority status). The setup packets need short
pauses between them (the FPGA "digests" each). The radio found by discovery
(or at --pluto-uri's address) is used; it picks the sample rate from
SAMPLE_RATES and the DDC delivers IQ centred on the frequency, so there is
no LO offset to remove. Lost packets are filled with zeros of the same
length (the sequence numbers say how many), keeping the timeline continuous.

The filter board ("Alex") word follows piHPSDR's thresholds for Hermes-class
radios (ANAN-10/100): high-pass by the receive frequency, low-pass likewise
(no transmitting), the antenna (ANT1-3) via the TX-antenna bits. The gain
setting drives the 0-31 dB step attenuator: gain 31 = 0 dB attenuation."""
import select
import socket
import struct
import sys
import threading
import time

import numpy as np

DISCOVERY_PORT = 1024
GENERAL_PORT, RX_SPECIFIC_PORT, HIGH_PRIORITY_PORT = 1024, 1025, 1027
DDC0_FROM_RADIO, STATUS_FROM_RADIO = 1035, 1025
CLOCK_HZ = 122.88e6
SAMPLE_RATES = (48000, 96000, 192000, 384000, 768000, 1536000)
PACKET_SAMPLES = 238          # IQ samples per DDC packet (24-bit I + Q)
KEEPALIVE_S = 0.1             # high-priority packet interval (the watchdog needs them)
STALL_S = 5.0                 # no IQ this long: give up (the GUI restarts RX)
MAX_GAIN_DB = 31              # step attenuator range
BOARDS = {0: "Atlas", 1: "Hermes", 2: "Hermes II", 3: "Angelia", 4: "Orion", 5: "Orion MkII",
          6: "Hermes Lite", 10: "Saturn"}


def _local_ipv4():
    """This machine's IPv4 addresses (not loopback)."""
    addrs = []
    try:
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=2).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                addrs.append(parts[parts.index("inet") + 1].split("/")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        try:
            addrs = socket.gethostbyname_ex(socket.gethostname())[2]
        except OSError:
            pass
    return [a for a in dict.fromkeys(addrs) if not a.startswith("127.")]


def discover(host=None, timeout=0.6, sock=None):
    """Radios answering discovery in either protocol: [(ip, board name, busy)],
    protocol 2 first (a radio answering both is used in protocol 2)."""
    import hpsdr1_source
    found = {r[0]: r for r in discover_p2(host, timeout)}
    for ip, board, busy, _ in hpsdr1_source.discover(host, timeout):
        found.setdefault(ip, (ip, board, busy))
    return list(found.values())


def open_rx(server, freq_hz, sample_rate_hz=None, rx_gain_db=MAX_GAIN_DB, antenna=None,
            gain_file=None, freq_offset_file=None):
    """The receive source for the radio found: protocol 2 (HpsdrRxSource) if one
    answers, else protocol 1 (Hermes-Lite 2, Radioberry: hpsdr1_source)."""
    import hpsdr1_source
    host = str(server or "").replace("ip:", "").split(":")[0].strip() or None
    p2 = discover_p2(host)
    if host:
        p2 = [r for r in p2 if r[0] == host]
    if not p2:
        p1 = hpsdr1_source.discover(host)
        if host:
            p1 = [r for r in p1 if r[0] == host] or p1
        if p1:
            return hpsdr1_source.Hpsdr1RxSource(server, freq_hz, sample_rate_hz=sample_rate_hz,
                                                rx_gain_db=rx_gain_db, gain_file=gain_file,
                                                freq_offset_file=freq_offset_file, radio=p1[0])
    return HpsdrRxSource(server, freq_hz, sample_rate_hz=sample_rate_hz, rx_gain_db=rx_gain_db,
                         antenna=antenna, gain_file=gain_file, freq_offset_file=freq_offset_file)


def discover_p2(host=None, timeout=0.6, sock=None):
    """Protocol 2 radios answering discovery: [(ip, board name, busy)].
    host: ask that address too. The broadcast goes out of EVERY interface,
    from each one's own address: an unbound broadcast only leaves by the
    default route, so a radio on a second network adapter (a radio-only
    link, 169.254.x.x when nothing hands out addresses) was never asked.
    (sock: ignored, kept for callers.)"""
    pkt = bytes([0, 0, 0, 0, 0x02]) + bytes(55)
    socks = []
    for src in [""] + _local_ipv4():
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sk.bind((src, 0))
            socks.append(sk)
        except OSError:
            continue
    found = {}
    try:
        for sk in socks:
            for dest in ([host] if host else []) + ["255.255.255.255"]:
                try:
                    sk.sendto(pkt, (dest, DISCOVERY_PORT))
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
                if port == DISCOVERY_PORT and len(d) >= 12 and d[4] in (2, 3) and d[:4] == b"\0\0\0\0":
                    found[ip] = (ip, BOARDS.get(d[11], f"board {d[11]}"), d[4] == 3)
    finally:
        for sk in socks:
            sk.close()
    return list(found.values())


def alex_rx_word(f_hz, antenna):
    """Filter board word for receiving at f_hz on ANT1..3 (Hermes / ANAN-10/100)."""
    hpf = (0x1000 if f_hz < 1.8e6 else 0x40 if f_hz < 6.5e6 else 0x20 if f_hz < 9.5e6 else
           0x10 if f_hz < 13e6 else 0x02 if f_hz < 20e6 else 0x04 if f_hz < 50e6 else 0x08)
    lpf = (0x20000000 if f_hz > 35.6e6 else 0x40000000 if f_hz > 24e6 else 0x80000000 if f_hz > 16.5e6
           else 0x00100000 if f_hz > 8e6 else 0x00200000 if f_hz > 5e6 else 0x00400000 if f_hz > 2.5e6
           else 0x00800000)
    return hpf | lpf | (0x01000000 << (antenna - 1))


def parse_antenna(name):
    """'ANT2' / '2' -> 2; anything else -> 1."""
    s = str(name or "").upper().replace("ANT", "").strip()
    return int(s) if s in ("1", "2", "3") else 1


class HpsdrRxSource:
    invert = False  # mirror the spectrum (hf_ofdm_rx.py --invert-spectrum)

    def __init__(self, server, freq_hz, sample_rate_hz=None, rx_gain_db=MAX_GAIN_DB, antenna=None,
                 gain_file=None, freq_offset_file=None, connect_timeout_s=3.0):
        self.freq_hz = float(freq_hz)
        self._freq_offset_hz = 0.0
        self.rx_gain_db = float(rx_gain_db)
        self.antenna = parse_antenna(antenna)
        want = float(sample_rate_hz or 192000)
        self.sample_rate_hz = float(next((r for r in SAMPLE_RATES if r >= want), SAMPLE_RATES[-1]))
        self.chunks = []
        self.total_len = 0
        self._lock = threading.Lock()
        self.eof = False
        self._new_data = threading.Event()
        self.lo_offset_hz = 0.0       # hf_ofdm_rx.py may set this; IQ arrives centred already
        self.radio_count = 0
        self.discarded_total = 0
        self.gaps = 0
        self.overloads = 0
        self._seq = {"gen": 0, "rx": 0, "hp": 0}
        self._send_lock = threading.Lock()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        self.sock.bind(("", 0))
        host = str(server or "").replace("ip:", "").split(":")[0].strip() or None
        radios = discover_p2(host)
        if host:
            radios = [r for r in radios if r[0] == host] or radios
        if not radios:
            self.sock.close()
            raise ConnectionError(f"ANAN/HPSDR: no protocol 2 radio answered on the network"
                                  f"{' at ' + host if host else ''} -- check it's on and connected")
        self.host, self.board, busy = radios[0]
        if busy:
            self.sock.close()
            raise ConnectionError(f"ANAN/HPSDR {self.host} is in use by another program -- close it first")

        self._general()
        time.sleep(0.1)       # piHPSDR: "let FPGA digest the port numbers"
        self._rx_specific()
        time.sleep(0.05)
        self._high_priority(True)
        threading.Thread(target=self._run, daemon=True, name="hpsdr-read").start()
        threading.Thread(target=self._keepalive, daemon=True, name="hpsdr-keepalive").start()
        t_end = time.monotonic() + connect_timeout_s
        while not self.radio_count and not self.eof and time.monotonic() < t_end:
            time.sleep(0.02)
        if not self.radio_count:
            self.close()
            raise ConnectionError(f"ANAN/HPSDR {self.host} sent no IQ within {connect_timeout_s:g} s")
        print(f"ANAN/HPSDR RX active: {self.board} at {self.host} freq={self.freq_hz / 1e6:.4f}MHz "
              f"rate={self.sample_rate_hz / 1e3:g} kS/s ANT{self.antenna} "
              f"attenuator={self._attenuation()} dB", file=sys.stderr)
        if gain_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(gain_file, self.set_gain)
        if freq_offset_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(freq_offset_file, self.set_freq_offset)

    # -- packets to the radio ----------------------------------------------
    def _send(self, kind, buf, port):
        with self._send_lock:
            buf[0:4] = struct.pack(">I", self._seq[kind] & 0xFFFFFFFF)
            self._seq[kind] += 1
            try:
                self.sock.sendto(bytes(buf), (self.host, port))
            except OSError:
                pass

    def _general(self):
        b = bytearray(60)
        b[4] = 0x00            # general packet (default ports)
        b[37] = 0x08           # DDC frequencies as phase words
        b[38] = 0x01           # hardware watchdog on: stops if our packets stop
        b[58] = 0x00           # PA off
        b[59] = 0x01           # filter board (Alex 0) present
        self._send("gen", b, GENERAL_PORT)

    def _rx_specific(self):
        b = bytearray(1444)
        b[4] = 1               # one ADC
        b[7] = 0x01            # DDC0 only
        b[17] = 0              # DDC0 <- ADC0
        b[18:20] = struct.pack(">H", int(self.sample_rate_hz) // 1000)
        b[22] = 24             # bits per sample
        self._send("rx", b, RX_SPECIFIC_PORT)

    def _attenuation(self):
        return int(min(MAX_GAIN_DB, max(0, round(MAX_GAIN_DB - self.rx_gain_db))))

    def _high_priority(self, run):
        f = self.freq_hz + self._freq_offset_hz
        b = bytearray(1444)
        b[4] = 0x01 if run else 0x00           # bit 0 run -- bit 1 (PTT) is never set
        b[9:13] = struct.pack(">I", int(round(f * 2 ** 32 / CLOCK_HZ)) & 0xFFFFFFFF)   # DDC0 phase word
        b[345] = 0                             # TX drive 0
        b[1432:1436] = struct.pack(">I", alex_rx_word(f, self.antenna))
        b[1443] = self._attenuation()          # ADC0 step attenuator
        self._send("hp", b, HIGH_PRIORITY_PORT)

    def _keepalive(self):
        n = 0
        while not self.eof:
            self._high_priority(True)
            n += 1
            if n % 10 == 0:
                self._rx_specific()
            time.sleep(KEEPALIVE_S)

    # -- control (live) ----------------------------------------------------
    def set_gain(self, db):
        self.rx_gain_db = float(db)
        print(f"ANAN/HPSDR attenuator now {self._attenuation()} dB", file=sys.stderr)

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
        last_seq = None
        last_data = time.monotonic()
        last_overload_msg = 0.0
        while not self.eof:
            r, _, _ = select.select([self.sock], [], [], 0.2)
            if not r:
                if time.monotonic() - last_data > STALL_S and self.radio_count:
                    print(f"ANAN/HPSDR: no IQ for {STALL_S:g} s -- stopping", file=sys.stderr)
                    break
                continue
            try:
                d, (ip, port) = self.sock.recvfrom(2048)
            except OSError:
                break
            if ip != self.host:
                continue
            if port == DDC0_FROM_RADIO and len(d) >= 16:
                seq = struct.unpack(">I", d[0:4])[0]
                n = struct.unpack(">H", d[14:16])[0]
                if n == 0 or len(d) < 16 + 6 * n:
                    continue
                if last_seq is not None and seq != (last_seq + 1) & 0xFFFFFFFF:
                    missing = (seq - last_seq - 1) & 0xFFFFFFFF
                    if 0 < missing < 1000:
                        self.gaps += missing
                        print(f"[hpsdr] {missing} packet(s) lost on the network -- filled with silence",
                              file=sys.stderr)
                        self._append(np.zeros(missing * n, np.complex64))
                last_seq = seq
                raw = np.frombuffer(d, np.uint8, 6 * n, 16).reshape(n, 6).astype(np.int32)
                i = (raw[:, 0] << 24 | raw[:, 1] << 16 | raw[:, 2] << 8) >> 8     # sign-extended 24 bit
                q = (raw[:, 3] << 24 | raw[:, 4] << 16 | raw[:, 5] << 8) >> 8
                scale = np.float32(1.0 / (1 << 23))
                self._append((i.astype(np.float32) * scale + 1j * (q.astype(np.float32) * scale))
                             .astype(np.complex64))
                last_data = time.monotonic()
            elif port == STATUS_FROM_RADIO and len(d) > 5 and d[5] & 0x01:
                self.overloads += 1
                if time.monotonic() - last_overload_msg > 5.0:
                    last_overload_msg = time.monotonic()
                    print("ANAN/HPSDR: ADC overload -- lower the RX gain (more attenuation)",
                          file=sys.stderr)
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
        for _ in range(3):        # stop it now (the watchdog would anyway)
            self._high_priority(False)
            time.sleep(0.02)
        try:
            self.sock.close()
        except OSError:
            pass
