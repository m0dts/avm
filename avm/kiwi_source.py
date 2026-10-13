"""KiwiSDR as a receive source: IQ over the Kiwi's WebSocket, duck-typing
hf_ofdm_rx.py's StdinReader like pluto_soapy_sink.PlutoRxSource (chunks,
total_len, _lock, eof, wait_for_more, sample_rate_hz).

Standard library only (a small WebSocket client of its own). The Kiwi
decides the sample rate -- ~12 kS/s normally (e.g. 11998.86, GPS-corrected),
~20.25 kS/s on Kiwis set up for wideband -- and reports it; hf_ofdm_rx.py's
front end then resamples to the mode's rate, as for the Airspy/SDRplay's
fixed rates. The Kiwi tunes IQ mode to the centre frequency itself, so there
is no LO offset to remove.

Its AGC is turned off (manual gain): AGC pumping wrecks OFDM equalisation.
Packets carry sequence numbers; a lost one is filled with zeros of the same
length, so the sample timeline the decoder sees stays continuous."""
import base64
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

KIWI_DEFAULT_PORT = 8073
PACKET_SAMPLES = 512          # IQ samples per SND packet (fills gaps of unknown size)
KEEPALIVE_S = 5.0
PASSBAND_FRAC = 0.47          # IQ passband each side, as a fraction of the sample rate
STALL_S = 10.0                # nothing from the Kiwi this long: give up (the GUI restarts RX)


def parse_server(server):
    """'host', 'host:port', 'kiwi:host:port' or 'http://host:port/' -> (host, port)."""
    s = (server or "").strip()
    for prefix in ("kiwi:", "http://", "https://", "ws://"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.strip("/")
    host, _, port = s.partition(":")
    return host, int(port) if port.isdigit() else KIWI_DEFAULT_PORT


def server_answers(server, timeout=2.0):
    """Is a Kiwi listening there? (TCP connect only.)"""
    host, port = parse_server(server)
    if not host:
        return False
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except (OSError, UnicodeError, ValueError):  # UnicodeError: a malformed name, e.g. "a..b"
        return False


class _WebSocket:
    """Just enough RFC 6455 for the Kiwi: masked text frames out, text and
    binary frames in."""

    def __init__(self, host, port, path, timeout=10.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            d = self.sock.recv(4096)
            if not d:
                raise ConnectionError("KiwiSDR closed the connection during the handshake")
            self.buf += d
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in status:
            raise ConnectionError(f"KiwiSDR refused the stream: {status}")
        self._send_lock = threading.Lock()

    def send(self, text):
        p = text.encode()
        m = os.urandom(4)
        n = len(p)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n))
        masked = bytes(b ^ m[i % 4] for i, b in enumerate(p))
        with self._send_lock:
            self.sock.sendall(hdr + m + masked)

    def _exact(self, n):
        while len(self.buf) < n:
            d = self.sock.recv(65536)
            if not d:
                raise EOFError
            self.buf += d
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        """(opcode, payload)."""
        b0, b1 = self._exact(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        mask = self._exact(4) if b1 & 0x80 else None
        p = self._exact(n)
        if mask:
            p = bytes(b ^ mask[i % 4] for i, b in enumerate(p))
        return b0 & 0x0F, p

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class KiwiRxSource:
    invert = False  # mirror the spectrum (hf_ofdm_rx.py --invert-spectrum)

    def __init__(self, server, freq_hz, rx_gain_db=50.0, gain_file=None, freq_offset_file=None,
                 user="AVM", connect_timeout_s=15.0):
        self.host, self.port = parse_server(server)
        if not self.host:
            raise ValueError("no KiwiSDR server given (Config tab: KiwiSDR server)")
        if ".." in self.host or self.host.startswith(".") or self.host.endswith("."):
            raise ValueError(f"KiwiSDR server '{self.host}' isn't a valid address (Config tab: KiwiSDR)")
        self.freq_hz = float(freq_hz)
        self.rx_gain_db = float(rx_gain_db)
        self._freq_offset_hz = 0.0
        self.chunks = []
        self.total_len = 0
        self._lock = threading.Lock()
        self.eof = False
        self._new_data = threading.Event()
        self.lo_offset_hz = 0.0       # hf_ofdm_rx.py may set this; IQ arrives centred already
        self.radio_count = 0
        self.discarded_total = 0
        self.gaps = 0
        self.sample_rate_hz = None
        self._rate_ready = threading.Event()
        self._error = None
        self._user = user

        self.ws = _WebSocket(self.host, self.port, f"/{int(time.time())}/SND")
        self.ws.sock.settimeout(STALL_S)
        self.ws.send("SET auth t=kiwi p=")
        threading.Thread(target=self._run, daemon=True, name="kiwi-read").start()
        if not self._rate_ready.wait(connect_timeout_s):
            self.close()
            raise ConnectionError(self._error or f"KiwiSDR {self.host}:{self.port} sent no stream "
                                                 f"within {connect_timeout_s:g} s")
        if self._error:
            raise ConnectionError(self._error)
        print(f"KiwiSDR RX active: {self.host}:{self.port} freq={self.freq_hz / 1e6:.4f}MHz "
              f"rate={self.sample_rate_hz:.2f} S/s gain={self.rx_gain_db:g}dB (Kiwi AGC off)",
              file=sys.stderr)
        if gain_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(gain_file, self.set_gain)
        if freq_offset_file:
            from pluto_soapy_sink import GainFileWatcher
            GainFileWatcher(freq_offset_file, self.set_freq_offset)
        threading.Thread(target=self._keepalive, daemon=True, name="kiwi-keepalive").start()

    # -- control -----------------------------------------------------------
    def _tune(self):
        half = int(PASSBAND_FRAC * (self.sample_rate_hz or 12000))
        f_khz = (self.freq_hz + self._freq_offset_hz) / 1e3
        self.ws.send(f"SET mod=iq low_cut={-half} high_cut={half} freq={f_khz:.3f}")

    def _set_agc(self):
        self.ws.send(f"SET agc=0 hang=0 thresh=-100 slope=6 decay=1000 manGain={int(round(self.rx_gain_db))}")

    def set_gain(self, db):
        self.rx_gain_db = max(0.0, min(120.0, float(db)))
        try:
            self._set_agc()
        except OSError:
            pass

    def set_freq_offset(self, hz):
        if hz == self._freq_offset_hz:
            return
        self._freq_offset_hz = hz
        try:
            self._tune()
        except OSError:
            return
        print(f"RX frequency offset {hz / 1e3:+.3f} kHz", file=sys.stderr)

    def _keepalive(self):
        while not self.eof:
            time.sleep(KEEPALIVE_S)
            try:
                self.ws.send("SET keepalive")
            except OSError:
                return

    # -- stream ------------------------------------------------------------
    def _configure(self, audio_rate):
        ws = self.ws
        ws.send(f"SET AR OK in={audio_rate} out=44100")
        self._tune()
        self._set_agc()
        ws.send("SET compression=0")
        ws.send("SET squelch=0 max=0")
        ws.send("SET gen=0 mix=-1")
        ws.send(f"SERVER DE CLIENT {self._user} SND")

    def _msg(self, text):
        for kv in text.split():
            key, _, val = kv.partition("=")
            if key == "sample_rate":
                self.sample_rate_hz = float(val)
            elif key == "audio_rate":
                self._configure(val)
            elif key == "too_busy":
                self._fail(f"KiwiSDR {self.host} is full (all {val} channels in use) -- try later or another Kiwi")
            elif key == "badp" and val not in ("0", ""):
                # (also sent when the channels open without one are all in use)
                self._fail(f"KiwiSDR {self.host} needs a password, or its free channels are all in use")
            elif key == "down":
                self._fail(f"KiwiSDR {self.host} is down for maintenance")
            elif key in ("inactivity_timeout", "redirect"):
                print(f"[kiwi] {kv}", file=sys.stderr)

    def _fail(self, why):
        self._error = why
        print(f"KiwiSDR: {why}", file=sys.stderr)
        self._end()

    def _end(self):
        with self._lock:
            self.eof = True
        self._new_data.set()
        self._rate_ready.set()

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
        retuned = False
        try:
            while not self.eof:
                op, p = self.ws.recv()
                if op == 8:
                    why = p[2:].decode(errors="replace") if len(p) > 2 else ""
                    print(f"KiwiSDR closed the stream{': ' + why if why else ''}", file=sys.stderr)
                    break
                if op not in (1, 2) or len(p) < 3:
                    continue
                tag = p[:3]
                if tag == b"MSG":
                    self._msg(p[4:].decode(errors="replace"))
                    continue
                if tag != b"SND" or len(p) < 20:
                    continue
                seq = struct.unpack("<I", p[4:8])[0]
                body = p[20:]   # flags, seq, S-meter, then the IQ header (GPS time)
                n = len(body) // 4
                if not n:
                    continue
                if self.sample_rate_hz and not self._rate_ready.is_set():
                    self._rate_ready.set()
                if last_seq is not None and seq != last_seq + 1:
                    missing = (seq - last_seq - 1) & 0xFFFFFFFF
                    if 0 < missing < 1000:
                        # keep the timeline: a lost packet becomes silence of its length
                        self.gaps += missing
                        print(f"[kiwi] {missing} packet(s) lost on the network -- filled with silence",
                              file=sys.stderr)
                        self._append(np.zeros(missing * n, np.complex64))
                last_seq = seq
                v = np.frombuffer(body[:4 * n], ">i2").astype(np.float32) * (1.0 / 32768.0)
                self._append((v[0::2] + 1j * v[1::2]).astype(np.complex64))
                if not retuned and self.radio_count > 0.5 * (self.sample_rate_hz or 12000):
                    # Newer Kiwi firmware (seen: 1.840) ignores the mode/passband
                    # and gain set before its audio starts: it streamed a USB
                    # (SSB-wide) slice with its AGC on. Sent again once streaming.
                    retuned = True
                    self._tune()
                    self._set_agc()
        except socket.timeout:
            print(f"KiwiSDR: no data for {STALL_S:g} s -- stopping", file=sys.stderr)
        except (OSError, EOFError) as e:
            if not self.eof:
                print(f"KiwiSDR connection lost: {e}", file=sys.stderr)
        self._end()

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
        self.ws.close()
