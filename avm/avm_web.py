"""A tiny web page for changing AVM settings from a phone or PC on the same
network: http://<this machine>:8080/ (AVM_WEB_PORT to change; 0 = off).

The KiwiSDR server the RX uses (the Config tab's KiwiSDR row), and the
picture the TX sends with the "Image + mic" source (media_source.TX_IMAGE_PATH;
used from the next TX start).
The page has no login -- anyone on the local network can use it -- and
accepts only a host or host:port, so nothing else can be set through it.

Standard library only. The HTTP server runs on its own thread; a change is
handed to the GUI thread through a Qt signal, so it goes through exactly the
same code as tapping the Config page's KiwiSDR button."""
import html
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PyQt5 import QtCore

DEFAULT_PORT = 8080
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,60}(:\d{1,5})?$")
IMAGE_MAX_BYTES = 10 * 1024 * 1024
# first bytes of the picture formats accepted (ffmpeg reads them all)
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"),
               (b"GIF8", "image/gif"), (b"BM", "image/bmp"), (b"RIFF", "image/webp"))


# Screen recordings (~/record.sh on the Pi writes them here): listed on the
# page to play or download. Only plain .mp4 names from this folder are served.
RECORDINGS_DIR = os.path.join(os.path.expanduser("~"), "recordings")
RECORDING_RE = re.compile(r"^[\w.-]{1,120}\.mp4$")
RECORDINGS_SHOWN = 20


def _recordings():
    """[(name, bytes, mtime)] newest first."""
    try:
        names = [n for n in os.listdir(RECORDINGS_DIR) if RECORDING_RE.match(n)]
    except OSError:
        return []
    out = []
    for n in names:
        try:
            st = os.stat(os.path.join(RECORDINGS_DIR, n))
        except OSError:
            continue
        out.append((n, st.st_size, st.st_mtime))
    return sorted(out, key=lambda r: -r[2])


def _image_type(data):
    for magic, ctype in IMAGE_MAGIC:
        if data.startswith(magic) and (ctype != "image/webp" or data[8:12] == b"WEBP"):
            return ctype
    return None


# The TX resolutions (media_tx_gui.WAVELET_RESOLUTIONS) run 1.60-1.83:1 around
# 16:9; a picture centre-cropped to that fills any of them with at most a
# sliver of letterbox. Kept to TX_IMAGE_MAX_W wide (the TX sends <= 384).
TX_IMAGE_ASPECT = 16 / 9
TX_IMAGE_MAX_W = 1280


def _fit_picture(data):
    """(jpeg bytes, note): the upload centre-cropped to TX_IMAGE_ASPECT and
    shrunk to TX_IMAGE_MAX_W. Unchanged (and no note) if it can't be decoded."""
    try:
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return data, ""
        h, w = img.shape[:2]
        if w / h > TX_IMAGE_ASPECT:                    # too wide: trim the sides
            nw = int(round(h * TX_IMAGE_ASPECT))
            x0 = (w - nw) // 2
            img = img[:, x0:x0 + nw]
        else:                                          # too tall: trim top and bottom
            nh = int(round(w / TX_IMAGE_ASPECT))
            y0 = (h - nh) // 2
            img = img[y0:y0 + nh]
        ch, cw = img.shape[:2]
        if cw > TX_IMAGE_MAX_W:
            img = cv2.resize(img, (TX_IMAGE_MAX_W, int(round(TX_IMAGE_MAX_W / TX_IMAGE_ASPECT))),
                             interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return data, ""
        fh, fw = img.shape[:2]
        trimmed = (cw, ch) != (w, h)
        return jpg.tobytes(), (f", trimmed from {w}x{h} to 16:9 ({fw}x{fh})" if trimmed
                               else f" ({fw}x{fh})")
    except Exception:
        return data, ""


# KiwiSDR servers used, most recent first: for quick switching on this page.
# Its own small file (this page's, not AVM's settings, which the GUI thread
# owns); seeded once from the RX log's KiwiSDR starts.
KIWI_HISTORY_PATH = os.path.join(os.path.expanduser("~"), ".config", "hfmodem", "kiwi_history.json")
KIWI_HISTORY_MAX = 30
_history_lock = threading.Lock()


def _seed_history():
    """Kiwi servers from the RX log, most recent first (first run only)."""
    found = []
    try:
        log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_logs", "rx_session.log")
        with open(log, errors="ignore") as f:
            for line in f:
                if "--sdr kiwi" in line:
                    m = re.search(r"--pluto-uri (\S+)", line)
                    if m and HOST_RE.match(m.group(1)):
                        found.append(m.group(1))
    except OSError:
        pass
    out = []
    for s in reversed(found):
        if s.lower() not in (o.lower() for o in out):
            out.append(s)
    return out[:KIWI_HISTORY_MAX]


def _load_history():
    import json
    try:
        with open(KIWI_HISTORY_PATH) as f:
            h = json.load(f)
        return [s for s in h if isinstance(s, str) and HOST_RE.match(s)]
    except FileNotFoundError:
        h = _seed_history()
        _save_history(h)
        return h
    except (OSError, ValueError):
        return []


def _save_history(h):
    import json
    try:
        os.makedirs(os.path.dirname(KIWI_HISTORY_PATH), exist_ok=True)
        tmp = KIWI_HISTORY_PATH + ".part"
        with open(tmp, "w") as f:
            json.dump(h[:KIWI_HISTORY_MAX], f, indent=1)
        os.replace(tmp, KIWI_HISTORY_PATH)
    except OSError:
        pass


def _remember(server, front=True):
    """Add a server to the history (to the front: most recently used)."""
    if not server or not HOST_RE.match(server):
        return
    with _history_lock:
        h = _load_history()
        rest = [s for s in h if s.lower() != server.lower()]
        if not front and len(rest) != len(h):
            return   # already listed: keep its place
        _save_history([server] + rest)


def _forget(server):
    with _history_lock:
        _save_history([s for s in _load_history() if s.lower() != server.lower()])

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AVM settings</title>
<style>
 body {{ font-family: system-ui, sans-serif; background: #25272b; color: #e6e8eb; margin: 0; padding: 16px; }}
 h1 {{ font-size: 1.3em; margin: 0 0 12px; }} .card {{ background: #2f3237; border-radius: 8px; padding: 14px; flex: 1 1 340px; min-width: 0; }}
 .tiles {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }}
 label {{ display: block; color: #9aa0a8; margin-bottom: 6px; }}
 input {{ width: 100%; box-sizing: border-box; font-size: 1.1em; padding: 10px; border-radius: 6px; border: 1px solid #555; background: #1c1d20; color: #fff; }}
 button {{ margin-top: 10px; font-size: 1.05em; padding: 10px 18px; border: 0; border-radius: 6px; background: #2f9e5a; color: #111; font-weight: bold; }}
 .hist {{ width: 100%; border-collapse: collapse; margin-top: 6px; }} .hist td {{ padding: 4px 0; border-top: 1px solid #3c4046; }}
 .hist form {{ margin: 0; display: inline; }} .hist button {{ margin: 0 0 0 6px; padding: 6px 12px; font-size: 0.95em; }}
 .hist .del {{ background: #6b2a24; color: #fff; }} .hist .cur {{ color: #2f9e5a; }} .hist td.b {{ text-align: right; white-space: nowrap; }}
 .msg {{ margin: 10px 0; padding: 8px 10px; border-radius: 6px; background: {msg_bg}; }} .dim {{ color: #9aa0a8; font-size: 0.9em; }}
</style></head><body>
<h1>{title}</h1>
<div class="tiles">
<div class="card">
 <form method="post" action="/kiwi">
  <label for="kiwi">KiwiSDR server (host:port) for RX</label>
  <input id="kiwi" name="kiwi" value="{kiwi}" placeholder="e.g. kiwi.example.org:8073" autocapitalize="off" autocorrect="off" spellcheck="false">
  <button type="submit">Set</button>
 </form>
 {msg}
 <p class="dim">Current: <b>{current}</b>. Used the next time RX starts with the KiwiSDR radio
 (the Config tab shows the same setting). Leave empty to clear.</p>
 {history}
</div>
<div class="card">
 <form method="post" action="/image" enctype="multipart/form-data">
  <label for="img">TX picture for the "Image + mic" source (JPEG, PNG, GIF, BMP or WebP, up to 10 MB)</label>
  <input id="img" type="file" name="image" accept="image/*">
  <button type="submit">Upload</button>
 </form>
 {image_msg}
 {image}
 <p class="dim">Sent from the next TX start with Source "Image + mic" (TX page). Scaled to the TX resolution.</p>
</div>
<div class="card">
 <label>AVM screen</label>
 <p><a href="/" onclick="var i=document.getElementById('shot');i.src='/screenshot.png?t='+Date.now();i.style.display='block';return false"><button type="button">Take screenshot</button></a>
 <a href="/screenshot.png?dl=1"><button type="button">Save screenshot</button></a></p>
 <img id="shot" alt="AVM screen" style="display:none;max-width:100%;border-radius:6px">
 <p class="dim">A picture of the AVM screen as it is now. Save downloads the one shown as a PNG file (a new one if none was taken).</p>
</div>
<div class="card">
 <label>Recordings ({rec_count})</label>
 {recordings}
 <p class="dim">Screen recordings from ~/record.sh, newest first. Tap one to play it here, or Download to save it.</p>
</div>
</div></body></html>"""


class _Bridge(QtCore.QObject):
    kiwi_set = QtCore.pyqtSignal(str)
    shot_req = QtCore.pyqtSignal(object)   # a request dict, answered on the GUI thread


def start(get_kiwi, set_kiwi, title="AVM", grab=None):
    """Serve the page. get_kiwi() -> current server; set_kiwi(server) runs on
    the GUI thread. Returns the server (or None if off / the port is taken)."""
    try:
        port = int(os.environ.get("AVM_WEB_PORT", DEFAULT_PORT))
    except ValueError:
        port = DEFAULT_PORT
    if port <= 0:
        return None
    bridge = _Bridge()
    bridge.kiwi_set.connect(set_kiwi)   # queued: the slot runs on the GUI thread

    def _do_shot(req):   # GUI thread: grab() -> PNG bytes
        try:
            req["png"] = grab() if grab else None
        except Exception as e:
            print(f"[web] screenshot failed: {e}", file=sys.stderr)
        req["done"].set()
    bridge.shot_req.connect(_do_shot)
    last_shot = [None]   # the last screenshot taken, for Save

    from media_source import TX_IMAGE_PATH

    class Handler(BaseHTTPRequestHandler):
        def _page(self, msg="", ok=True, value=None, image_msg="", image_ok=True):
            live = value if (value is not None and ok) else (get_kiwi() or "")   # just set: not applied yet
            current = live or "(none)"
            _remember(live, front=False)   # one chosen on the touchscreen joins the list
            rows = []
            for s in _load_history():
                e = html.escape(s, quote=True)
                is_cur = s.lower() == live.lower()
                use = ('<span class="cur">in use</span>' if is_cur else
                       f'<form method="post" action="/kiwi/use"><input type="hidden" name="kiwi" value="{e}">'
                       f'<button type="submit">Use</button></form>')
                rows.append(f'<tr><td>{e}</td><td class="b">{use}'
                            f'<form method="post" action="/kiwi/delete"><input type="hidden" name="kiwi" value="{e}">'
                            f'<button class="del" type="submit" title="Remove from the list">&#10005;</button></form></td></tr>')
            history = (f'<label>Used before</label><table class="hist">{"".join(rows)}</table>' if rows else "")
            if os.path.isfile(TX_IMAGE_PATH):
                stamp = int(os.path.getmtime(TX_IMAGE_PATH))
                image = (f'<p><img src="/image?t={stamp}" alt="current TX picture" '
                         f'style="max-width:100%;max-height:240px;border-radius:6px"></p>')
            else:
                image = '<p class="dim">No picture uploaded yet (the test pattern is sent instead).</p>'
            recs = _recordings()
            items = []
            for n, size, mtime in recs[:RECORDINGS_SHOWN]:
                e = html.escape(n, quote=True)
                when = time.strftime("%d %b %H:%M", time.localtime(mtime))
                items.append(f'<details><summary>{when} &middot; {size / 1e6:.1f} MB</summary>'
                             f'<video controls preload="none" src="/rec/{e}" style="width:100%;border-radius:6px;margin-top:6px"></video>'
                             f'<p><a href="/rec/{e}?dl=1"><button type="button">Download</button></a></p></details>')
            recordings = "".join(items) or '<p class="dim">No recordings yet.</p>'
            box = lambda m, good: (f'<div class="msg" style="background:{"#24543a" if good else "#6b2a24"}">'
                                   f'{html.escape(m)}</div>') if m else ""
            body = PAGE.format(title=html.escape(title), kiwi=html.escape(value if value is not None else (get_kiwi() or "")),
                               current=html.escape(current), msg_bg="#24543a" if ok else "#6b2a24",
                               msg=box(msg, ok), image_msg=box(image_msg, image_ok), image=image,
                               history=history, recordings=recordings, rec_count=len(recs)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/image":
                try:
                    data = open(TX_IMAGE_PATH, "rb").read()
                except OSError:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", _image_type(data) or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/screenshot.png":
                dl = "dl=1" in self.path
                data = last_shot[0] if dl else None   # Save: the picture taken, not a new one
                if data is None:
                    req = {"done": threading.Event(), "png": None}
                    bridge.shot_req.emit(req)
                    if not req["done"].wait(5) or not req["png"]:
                        self.send_error(503, "Screenshot not available")
                        return
                    data = last_shot[0] = req["png"]
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                if dl:
                    name = time.strftime("avm_%Y%m%d_%H%M%S.png")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                self.wfile.write(data)
                return
            if path.startswith("/rec/"):
                self._send_recording(urllib.parse.unquote(path[5:]), "dl=1" in self.path)
                return
            if path not in ("/", "/index.html"):
                self.send_error(404)
                return
            self._page()

        def _send_recording(self, name, download):
            if not RECORDING_RE.match(name):
                self.send_error(404)
                return
            fn = os.path.join(RECORDINGS_DIR, name)
            try:
                size = os.path.getsize(fn)
                f = open(fn, "rb")
            except OSError:
                self.send_error(404)
                return
            with f:
                start, end = 0, size - 1
                m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                    else:
                        start = max(0, size - int(m.group(2)))
                    if start > end:
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if download:
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.end_headers()
                f.seek(start)
                left = end - start + 1
                try:
                    while left > 0:
                        chunk = f.read(min(left, 1 << 16))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass   # the player stopped reading (seek, close): normal

        def _upload_image(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > IMAGE_MAX_BYTES + 64 * 1024:
                # read and drop the upload first: answering without reading it
                # left the browser with a reset connection, not this message
                left = min(n, 64 * 1024 * 1024)
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    left -= len(chunk)
                self._page(image_msg="No file, or larger than 10 MB -- nothing changed.", image_ok=False)
                return
            body = self.rfile.read(n)
            from email.parser import BytesParser
            from email.policy import HTTP
            msg = BytesParser(policy=HTTP).parsebytes(
                b"Content-Type: " + self.headers.get("Content-Type", "").encode() + b"\r\n\r\n" + body)
            data = None
            for part in msg.iter_parts() if msg.is_multipart() else ():
                if part.get_param("name", header="content-disposition") == "image":
                    data = part.get_payload(decode=True)
            if not data or len(data) > IMAGE_MAX_BYTES:
                self._page(image_msg="No picture in the upload -- nothing changed.", image_ok=False)
                return
            if _image_type(data) is None:
                self._page(image_msg="Not a JPEG, PNG, GIF, BMP or WebP picture -- nothing changed.", image_ok=False)
                return
            data, note = _fit_picture(data)
            os.makedirs(os.path.dirname(TX_IMAGE_PATH), exist_ok=True)
            tmp = TX_IMAGE_PATH + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, TX_IMAGE_PATH)   # a TX start never reads half a file
            self._page(image_msg=f"Picture uploaded{note} ({len(data) // 1024} kB) -- sent from the next TX start.")

        def do_POST(self):
            if self.path == "/image":
                self._upload_image()
                return
            if self.path not in ("/kiwi", "/kiwi/use", "/kiwi/delete"):
                self.send_error(404)
                return
            n = min(int(self.headers.get("Content-Length") or 0), 1024)
            fields = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace"))
            server = (fields.get("kiwi", [""])[0]).strip()
            if server and not HOST_RE.match(server):
                self._page("Not a valid host or host:port -- nothing changed.", ok=False, value=server)
                return
            if self.path == "/kiwi/delete":
                _forget(server)
                self._page(f"{server} removed from the list.")
                return
            bridge.kiwi_set.emit(server)
            _remember(server)
            # the GUI thread applies it shortly: show the new value meanwhile
            self._page(f"KiwiSDR server set to {server}." if server else "KiwiSDR server cleared.", value=server)

        def log_message(self, fmt, *args):
            pass   # quiet: no per-request lines in AVM's output

    try:
        httpd = ThreadingHTTPServer(("", port), Handler)
    except OSError as e:
        print(f"[web] settings page not started on port {port}: {e}", file=sys.stderr)
        return None
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True, name="avm-web").start()
    httpd._bridge = bridge   # keep the bridge (and its connection) alive
    print(f"[web] settings page on port {port}", file=sys.stderr)
    return httpd
