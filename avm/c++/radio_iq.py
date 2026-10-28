#!/usr/bin/env python3
"""The radio's IQ on stdout, for avm_rx: hf_ofdm_rx.py's own argument
handling and radio opening (make_reader: Pluto, Lime, RTL-SDR, Airspy,
KiwiSDR, HPSDR, the virtual radio... with gain, AGC, ppm, antenna, the
GUI's gain/offset files), but instead of decoding, the raw samples
(complex64, at the radio's rate) are written out as they arrive.

    python radio_iq.py <hf_ofdm_rx.py's arguments> | avm_rx <same modem args> \
        --sample-rate <rate> --lo-offset-hz <offset>

The capture rate actually set (a radio may round it) is printed on stderr
as "[radio] sample_rate=<Hz> lo_offset_hz=<Hz>"; avm_rx needs that rate."""
import os
import sys

AVM = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, AVM)
os.chdir(AVM)
sys.argv[0] = os.path.join(AVM, "hf_ofdm_rx.py")
if "--legacy-frontend" not in sys.argv:
    sys.argv.append("--legacy-frontend")      # the raw radio stream, not the modem-rate front end's
import hf_ofdm_rx as rx  # noqa: E402


def dump(cfg, args, reader):
    rate = getattr(reader, "sample_rate_hz", None) or args.sample_rate or cfg.fs
    print(f"[radio] sample_rate={rate:.6f} lo_offset_hz={args.lo_offset_hz:.3f}", file=sys.stderr, flush=True)
    out = sys.stdout.buffer
    taken = 0
    try:
        while True:
            reader.wait_for_more(reader.total_len if taken >= len(reader.chunks) else -1, timeout=0.5)
            with reader._lock:
                new = reader.chunks[taken:]
                taken = len(reader.chunks)
                discard = getattr(reader.chunks, "discard_before", None)
                if discard:
                    discard(taken)
                eof = reader.eof
            for c in new:
                out.write(c)
            if new:
                out.flush()
            if eof and taken == len(reader.chunks):
                break
    except (BrokenPipeError, OSError):
        pass                                   # avm_rx stopped
    finally:
        try:
            reader.close()
        except Exception:
            pass


rx.run_decode_loop = dump
rx.main()
