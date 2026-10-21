"""Virtual radio: AVM's transmitter and receiver joined on one machine,
for testing everything (GUI, video, audio, messages...) without radio
hardware. A clean link: no channel, and a noise floor ~60 dB down (pure
silence between transmissions would upset the receiver's detection).

    TX (hf_ofdm_tx.py --sdr virtual)  -->  UDP on 127.0.0.1  -->
    RX (hf_ofdm_rx.py --sdr virtual): a continuous real-time stream

The TX side paces itself to real time (a radio's buffer would) and sends
its samples with its frequency and rate. The RX side always delivers a
continuous stream at its own rate: the noise floor, plus the TX signal
when one is on its frequency. A frequency difference between the two
shows up as an offset, as on air; one outside the RX bandwidth isn't heard.
"""
import os
import socket
import struct
import sys
import threading
import time
from fractions import Fraction

import numpy as np

PORT = 47731                     # UDP on 127.0.0.1
PACKET_HEADER = struct.Struct(">ddI")   # TX frequency, TX sample rate, sequence number
PACKET_SAMPLES = 7000                   # complex64: 56 kB, under UDP's 64 kB
TX_LEAD_S = 0.2                  # the TX may run this far ahead of real time (a radio's buffer)
JITTER_S = 0.15                  # RX holds this much of a burst before playing it out
RX_BLOCK_S = 0.02                # RX makes its stream in blocks this long
NOISE_DB = -60.0                 # noise floor, relative to the signal's power


class VirtualTxSink:
    """hf_ofdm_tx.py's radio for --sdr virtual: samples to the virtual RX,
    paced to real time."""

    def __init__(self, freq_hz, sample_rate_hz, **_):
        self.freq, self.fs = float(freq_hz), float(sample_rate_hz)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self.t0 = None
        self.sent = 0
        print(f"Virtual radio TX: {self.freq / 1e6:.4f} MHz, {self.fs:.0f} S/s -> 127.0.0.1:{PORT}",
              file=sys.stderr)

    def set_gain(self, *_):
        pass                                   # a clean link: the level doesn't matter

    def write(self, waveform):
        x = np.asarray(waveform, np.complex64)
        now = time.monotonic()
        if self.t0 is None or now - self.t0 > self.sent / self.fs:
            self.t0, self.sent = now, 0        # first write, or the TX fell behind: start afresh
        for i in range(0, len(x), PACKET_SAMPLES):
            chunk = x[i:i + PACKET_SAMPLES]
            ahead = (self.sent / self.fs) - (time.monotonic() - self.t0)
            if ahead > TX_LEAD_S:
                time.sleep(ahead - TX_LEAD_S)
            try:
                self.sock.sendto(PACKET_HEADER.pack(self.freq, self.fs, self.seq) + chunk.tobytes(),
                                 ("127.0.0.1", PORT))
            except OSError:
                pass                           # no receiver: like transmitting to nobody
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            self.sent += len(chunk)

    def close(self):
        self.sock.close()


class VirtualRxSource:
    """hf_ofdm_rx.py's radio for --sdr virtual: a continuous real-time
    stream at sample_rate_hz -- the noise floor, plus the virtual TX when
    it's on this frequency. The reader interface of StdinReader /
    KiwiRxSource (chunks of complex64 bytes, total_len, eof, wait_for_more)."""
    invert = False

    def __init__(self, freq_hz, sample_rate_hz, tune_offset_hz=0.0, lo_offset_hz=0.0):
        import hf_ofdm_common as ofdm
        self.freq_hz, self.fs = float(freq_hz), float(sample_rate_hz)
        # where the TX signal sits in this stream: at -tune_offset (the LO is
        # tuned that far above the frequency, as PlutoRxSource does), less
        # any shift this source removes itself
        self.place_hz = float(lo_offset_hz) - float(tune_offset_hz)
        self.lo_offset_hz = 0.0
        self._tune_hz = 0.0
        # (ChunkList: the consumer discards what it has used -- a plain list
        # kept the whole session, ~4.4 MB/s at 550 kS/s)
        self.chunks, self.total_len, self.eof = ofdm.ChunkList(), 0, False
        self._lock = threading.Lock()
        self._new_data = threading.Event()
        self.radio_count = self.discarded_total = self.gaps = 0
        self.sample_rate_hz = self.fs
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # a burst's start arrives at once (the TX's TX_LEAD_S of samples):
        # Windows' default 64 kB receive buffer dropped most of it
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        try:
            self.sock.bind(("127.0.0.1", PORT))
        except OSError as e:
            raise ConnectionError(f"the virtual radio's receiver is already in use on this machine "
                                  f"(another AVM receiving from it?) -- stop that one first "
                                  f"(UDP port {PORT}: {e.strerror or e})") from None
        self.sock.settimeout(0.05)
        self._last_seq = None
        self._rx = []                          # TX samples waiting to be played out (at this rate)
        self._rx_n = 0
        self._rx_lock = threading.Lock()
        self._playing = False
        self._sig_p = None                     # running TX signal power (sets the noise floor)
        print(f"Virtual radio RX: {self.freq_hz / 1e6:.4f} MHz, {self.fs:.0f} S/s", file=sys.stderr)
        threading.Thread(target=self._listen, daemon=True, name="virtual-rx-net").start()
        threading.Thread(target=self._run, daemon=True, name="virtual-rx").start()

    # -- the virtual TX's packets -> this rate
    def _listen(self):
        debug = os.environ.get("AVM_VIRTUAL_DEBUG")
        while not self.eof:
            try:
                pkt, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(pkt) <= PACKET_HEADER.size:
                continue
            tx_freq, tx_fs, seq = PACKET_HEADER.unpack_from(pkt)
            if self._last_seq is not None and seq != (self._last_seq + 1) & 0xFFFFFFFF:
                self.discarded_total += 1
                if debug:
                    print(f"[virtual] packet(s) lost before {seq}", file=sys.stderr)
            self._last_seq = seq
            x = np.frombuffer(pkt, np.complex64, offset=PACKET_HEADER.size).astype(complex)
            df = tx_freq - self.freq_hz            # the TX's offset from this receiver's frequency
            if abs(df) > max(tx_fs, self.fs) / 2:
                continue                           # far off this frequency: not heard
            if tx_fs != self.fs:
                from scipy.signal import resample_poly
                r = Fraction(self.fs / tx_fs).limit_denominator(1000)
                x = resample_poly(x, r.numerator, r.denominator)
            with self._rx_lock:
                self._rx.append((x, df))
                self._rx_n += len(x)

    def _take(self, n):
        """n TX samples (with their offset) to play out now, or None."""
        with self._rx_lock:
            if not self._playing:
                if self._rx_n < JITTER_S * self.fs:
                    return None
                self._playing = True
            if self._rx_n == 0:
                self._playing = False
                return None
            out, df = np.zeros(n, complex), self._rx[0][1]
            k = 0
            while k < n and self._rx:
                x, df = self._rx[0]
                m = min(n - k, len(x))
                out[k:k + m] = x[:m]
                k += m
                if m == len(x):
                    self._rx.pop(0)
                else:
                    self._rx[0] = (x[m:], df)
            self._rx_n -= k
            if k < n:
                self._playing = False             # ran out: the burst ended
                if os.environ.get("AVM_VIRTUAL_DEBUG"):
                    print(f"[virtual] TX ran out: {k}/{n} samples", file=sys.stderr)
            return out, df

    # -- the stream
    def _run(self):
        rng = np.random.default_rng()
        n_out = 0
        phase = 0.0
        t_start = time.monotonic()
        while not self.eof:
            due = int((time.monotonic() - t_start) * self.fs) - n_out
            if due < RX_BLOCK_S * self.fs:
                time.sleep(RX_BLOCK_S / 2)
                continue
            n = due
            out = np.zeros(n, complex)
            got = self._take(n)
            if got is not None:
                x, df = got
                live = x[np.abs(x) > 0]
                if len(live):
                    p = float(np.mean(np.abs(live) ** 2))
                    self._sig_p = p if self._sig_p is None else 0.9 * self._sig_p + 0.1 * p
                f = df + self.place_hz - self._tune_hz
                ph = phase + 2 * np.pi * f * np.arange(n) / self.fs
                phase = float((ph[-1] + 2 * np.pi * f / self.fs) % (2 * np.pi))
                out = x * np.exp(1j * ph)
            var = (self._sig_p or 0.01) * 10 ** (NOISE_DB / 10)
            out = out + np.sqrt(var / 2) * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
            out = out.astype(np.complex64)
            if self.invert:
                out = np.conj(out)
            chunk = out.tobytes()
            with self._lock:
                self.chunks.append(chunk)
                self.total_len += len(chunk)
            self.radio_count += n
            n_out += n
            self._new_data.set()

    # -- reader interface
    def wait_for_more(self, since_len, timeout=None):
        while True:
            with self._lock:
                if self.eof or self.total_len > since_len:
                    return
            self._new_data.clear()
            self._new_data.wait(timeout=timeout)

    def set_gain(self, *_):
        pass

    def set_freq_offset(self, hz):
        """The GUI's RX offset: retunes by hz, so the signal moves by -hz."""
        self._tune_hz = float(hz)

    def close(self):
        self.eof = True
        self._new_data.set()
        try:
            self.sock.close()
        except OSError:
            pass
