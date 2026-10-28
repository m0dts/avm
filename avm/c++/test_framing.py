"""framing.cpp against framing_ref.py (the production logic, lifted out):
fragments byte for byte on random streams, and deframer events with lost
fragments. Then, with no loss, every audio and video packet must come back.

    python test_framing.py [scenarios]"""
import os
import random
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import framing_cpp as C
import framing_ref as Ref
import media_tx_framer as F


def scenario(seed):
    rnd = random.Random(seed)
    fs = rnd.choice([1024, 1024, 2048, 256, 512])
    sid = rnd.choice([None, "M0DTS", "G4XYZ/P IO94", "A" * 25])
    msg0 = rnd.randrange(256)
    tx_r, tx_c = Ref.RefFramer(fs, sid, msg0), C.Framer(fs, sid, msg0)
    w, h, fps = rnd.choice([(256, 144, 2), (384, 224, 10)]) + ()
    vcfg_payload = struct.pack(">HHH", w, h, fps * 1000) + F.WAVELET_CODEC_TAG
    vcfg = struct.pack(">BH", F.VIDEO_CONFIG_TYPE, len(vcfg_payload)) + vcfg_payload
    period = rnd.choice([0.1731, 0.417, 1.0, 1.5625])
    profile = rnd.choice([0, 1])
    changes = 0
    now = 100.0
    frags = []
    sent_audio, sent_video = [], []
    ok = True
    for t in range(rnd.randrange(60, 220)):
        if t == 3 or (rnd.random() < 0.01):
            new = rnd.choice([0, 1]) if t != 3 else profile
            changes += new != profile
            profile = new
            tx_r.state["audio_profile_id"] = profile
            tx_c.set_audio_profile(profile)
        if t == 2:
            tx_r.state["video_config_record"] = vcfg
            tx_c.set_video_config(vcfg)
        if rnd.random() < 0.05:   # a new message, or none
            if rnd.random() < 0.3:
                data = None
            else:
                text = "".join(rnd.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /-.?!") for _ in range(rnd.randrange(1, 81)))
                data = F.build_message(rnd.randrange(1 << 32), text)
            tx_r.set_message(data)
            tx_c.set_message(data)
        audio = []
        if t >= 3:
            for _ in range(rnd.randrange(0, 6)):
                if profile == 1:
                    audio.append(bytes(rnd.randrange(256) for _ in range(8)))
                else:
                    n = rnd.choice([40, 40, 40, 60, rnd.randrange(1, 300), rnd.randrange(1, 3 * fs)])
                    audio.append(bytes(rnd.randrange(256) for _ in range(n)))
        video = []
        if t >= 2 and rnd.random() < 0.7:
            for _ in range(rnd.randrange(1, 3)):
                n = rnd.choice([rnd.randrange(10, 400), rnd.randrange(10, fs), rnd.randrange(fs, 4 * fs)])
                video.append((bytes(rnd.randrange(256) for _ in range(n)), rnd.random() < 0.1))
        sent_audio += audio
        sent_video += [v for v, _ in video]
        now += period if rnd.random() > 0.05 else period * rnd.choice([0.2, 5])
        a = tx_r.tick(audio, video, now)
        b = tx_c.tick(audio, video, now)
        if a != b:
            print(f"  seed {seed}: fragments differ at tick {t} ({len(a)} vs {len(b)})")
            if os.environ.get("FRAMING_DEBUG"):
                for k, (x, y) in enumerate(zip(a, b)):
                    if x != y:
                        j = next(i for i in range(len(x)) if x[i] != y[i])
                        print(f"    fragment {k}, first different byte {j}: ref {x[max(0, j - 8):j + 12].hex()} / "
                              f"c++ {y[max(0, j - 8):j + 12].hex()}  (lengths {x[0] * 256 + x[1]} / {y[0] * 256 + y[1]})")
                        break
            return False, None
        frags += [(f, now) for f in a]
    # drain: ticks with nothing new until the framer has sent everything it holds
    for t in range(400):
        if not tx_r.video_carry:
            break
        now += period
        a = tx_r.tick([], [], now)
        b = tx_c.tick([], [], now)
        if a != b:
            print(f"  seed {seed}: fragments differ while draining")
            return False, None
        frags += [(f, now) for f in a]
    return ok, (fs, frags, sent_audio, sent_video, changes)


def deframe_compare(seed, fs, frags, drop):
    rnd = random.Random(seed * 7 + 1)
    rx_r, rx_c = Ref.RefDeframer(), C.Deframer(fs)
    ev_r, ev_c = [], []
    t_extra = 0.0
    for f, now in frags:
        if rnd.random() < drop:
            continue
        if rnd.random() < 0.01:
            t_extra += 4.0     # a signal gap: station ID reset
        ev_r += rx_r.push(f, now + t_extra)
        ev_c += rx_c.push(f, now + t_extra)
    return ev_r == ev_c, ev_r


n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
bad = 0
stats = {"frags": 0, "audio": 0, "video": 0, "msgs": 0, "sids": 0}
for seed in range(n):
    ok, res = scenario(seed)
    if not ok:
        bad += 1
        continue
    fs, frags, sent_audio, sent_video, changes = res
    stats["frags"] += len(frags)
    for drop in (0.0, 0.1, 0.3):
        same, ev = deframe_compare(seed, fs, frags, drop)
        if not same:
            print(f"  seed {seed}: deframer events differ (drop {drop})")
            bad += 1
            break
        if drop == 0.0:
            got_v = [d for t, d in ev if t == Ref.EV_VIDEO_PACKET]
            # (a mid-stream audio codec change -- only on a TX restart in AVM,
            # forced here to exercise it -- can cost the record after it)
            if not changes and got_v != sent_video:
                print(f"  seed {seed}: video packets not all recovered ({len(got_v)}/{len(sent_video)})")
                bad += 1
            got_a = [d for t, d in ev if t == Ref.EV_AUDIO_PACKET]
            # (audio packed before the first fragment that carries the audio
            # config can't be decoded: the framer's first block is made before
            # the codec is known -- as in production. Everything after must come back.)
            if not changes and (got_a != sent_audio[len(sent_audio) - len(got_a):]
                                or len(sent_audio) - len(got_a) > 6):
                print(f"  seed {seed}: audio packets not all recovered ({len(got_a)}/{len(sent_audio)})")
                bad += 1
            stats["audio"] += len(got_a)
            stats["video"] += len(got_v)
            stats["msgs"] += sum(t == Ref.EV_MESSAGE for t, _ in ev)
            stats["sids"] += sum(t == Ref.EV_STATION_ID for t, _ in ev)
print(f"{n} random streams: {stats['frags']} fragments; no-loss pass recovered {stats['audio']} audio and "
      f"{stats['video']} video packets, {stats['msgs']} messages, {stats['sids']} callsign confirmations")
print("ALL IDENTICAL" if bad == 0 else f"{bad} MISMATCHES")
