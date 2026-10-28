"""The production media_tx_framer.py, run as a process on fake_source.py's
deterministic audio + video, decoded by framing.cpp's Deframer: every
packet it sent must come back, in order. Also decodes the same fragments
with framing_ref.RefDeframer (the production record handling): their
events must match.

    python test_real_framer.py [seconds] [fragment_size] [period_s]"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_source
import framing_cpp as C
import framing_ref as Ref

here = os.path.dirname(os.path.abspath(__file__))
avm = os.path.join(here, "..")
secs = float(sys.argv[1]) if len(sys.argv) > 1 else 15
fs = int(sys.argv[2]) if len(sys.argv) > 2 else 1024
period = sys.argv[3] if len(sys.argv) > 3 else "0.25"
py = sys.executable
vsrc = f'"{py}" "{os.path.join(here, "fake_source.py")}" video 7 6'
asrc = f'"{py}" "{os.path.join(here, "fake_source.py")}" audio 7'
cmd = [py, os.path.join(avm, "media_tx_framer.py"), "--fragment-size", str(fs), "--fragment-period-s", period,
       "--station-id", "M0DTS", "--video-codec", "wavelet", "--video-framerate", "6",
       "--video-resolution", "256x144", "--video-source-cmd", vsrc,
       "--audio-codec", "codec2", "--audio-source-cmd", asrc]
p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=avm)
try:
    data = p.communicate(timeout=secs)[0]
except subprocess.TimeoutExpired:
    p.kill()
    data = p.communicate()[0]
frags = [data[i:i + fs] for i in range(0, len(data) - fs + 1, fs)]
dc, dr = C.Deframer(fs), Ref.RefDeframer()
ev_c, ev_r = [], []
for k, f in enumerate(frags):
    ev_c += dc.push(f, k * float(period))
    ev_r += dr.push(f, k * float(period))
vid = [d for t, d in ev_c if t == Ref.EV_VIDEO_PACKET]
aud = [d for t, d in ev_c if t == Ref.EV_AUDIO_PACKET]
sids = [d for t, d in ev_c if t == Ref.EV_STATION_ID]
want_v = fake_source.packets(7, len(vid))
first_a = int.from_bytes(aud[0][:4], "big") if aud else 0
want_a = fake_source.frames(7, first_a + len(aud))[first_a:]
print(f"{len(frags)} fragments from the production framer ({fs} B, {period} s ticks)")
print(f"  video: {len(vid)} packets back, in order and intact: {vid == want_v}")
print(f"  audio: {len(aud)} frames back (from #{first_a}), contiguous and intact: {aud == want_a}")
print(f"  callsign: {[s.decode().strip() for s in sids]}")
print(f"  C++ deframer events == production record handling: {ev_c == ev_r}")
ok = vid == want_v and aud == want_a and ev_c == ev_r and len(vid) > 0 and len(aud) > 0
print("OK" if ok else "FAILED")
