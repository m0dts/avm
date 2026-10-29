#include "web.h"

#include <QBuffer>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QRegularExpression>
#include <QSaveFile>
#include <QTcpServer>
#include <QTcpSocket>
#include <QUrl>
#include <QUrlQuery>
#include <cstdio>

#include "pages.h"

namespace avm {

static const int DEFAULT_PORT = 8080;
static const qint64 IMAGE_MAX_BYTES = 10 * 1024 * 1024;
static const qint64 REQUEST_MAX_BYTES = IMAGE_MAX_BYTES + 64 * 1024;
static const int RECORDINGS_SHOWN = 20, KIWI_HISTORY_MAX = 30;
// the TX resolutions run 1.60-1.83:1 around 16:9: a picture centre-cropped to
// that fills any of them with at most a sliver of letterbox; kept to 1280 wide
static const double TX_IMAGE_ASPECT = 16.0 / 9;
static const int TX_IMAGE_MAX_W = 1280;

static const QRegularExpression& hostRe() {
    static const QRegularExpression re(R"(^[A-Za-z0-9.-]{1,60}(:\d{1,5})?$)");
    return re;
}
static const QRegularExpression& recordingRe() {
    static const QRegularExpression re(R"(^[\w.-]{1,120}\.mp4$)");
    return re;
}
static QString configDir() { return QDir::homePath() + "/.config/hfmodem"; }
static QString recordingsDir() { return QDir::homePath() + "/recordings"; }
// the uploaded picture (media_source.TX_IMAGE_PATH: the Python GUI's too)
static QString uploadPath() { return configDir() + "/tx_image"; }

static QString esc(const QString& s) { return s.toHtmlEscaped(); }

static const char* imageType(const QByteArray& d) {
    if (d.startsWith("\xff\xd8\xff")) return "image/jpeg";
    if (d.startsWith("\x89PNG\r\n\x1a\n")) return "image/png";
    if (d.startsWith("GIF8")) return "image/gif";
    if (d.startsWith("BM")) return "image/bmp";
    if (d.startsWith("RIFF") && d.mid(8, 4) == "WEBP") return "image/webp";
    return nullptr;
}

// ---- KiwiSDR servers used, most recent first (avm_web.py's own file:
// ~/.config/hfmodem/kiwi_history.json), seeded once from the RX logs
static QStringList seedHistory() {
    QStringList found;
    static const QRegularExpression uri(R"(--pluto-uri (\S+))");
    for (const char* name : {"rx_session.log", "cpp_session.log"}) {
        QFile f(avmDir() + "/gui_logs/" + name);
        if (!f.open(QIODevice::ReadOnly)) continue;
        while (!f.atEnd()) {
            QString line = QString::fromUtf8(f.readLine());
            if (!line.contains("--sdr kiwi")) continue;
            auto m = uri.match(line);
            if (m.hasMatch() && hostRe().match(m.captured(1)).hasMatch()) found << m.captured(1);
        }
    }
    QStringList out;
    for (int i = found.size() - 1; i >= 0 && out.size() < KIWI_HISTORY_MAX; --i)
        if (!out.contains(found[i], Qt::CaseInsensitive)) out << found[i];
    return out;
}

static void saveHistory(const QStringList& h) {
    QDir().mkpath(configDir());
    QSaveFile f(configDir() + "/kiwi_history.json");
    if (!f.open(QIODevice::WriteOnly)) return;
    f.write(QJsonDocument(QJsonArray::fromStringList(h.mid(0, KIWI_HISTORY_MAX))).toJson());
    f.commit();
}

static QStringList loadHistory() {
    QFile f(configDir() + "/kiwi_history.json");
    if (!f.exists()) {
        QStringList h = seedHistory();
        saveHistory(h);
        return h;
    }
    if (!f.open(QIODevice::ReadOnly)) return {};
    QStringList out;
    for (const auto& v : QJsonDocument::fromJson(f.readAll()).array())
        if (v.isString() && hostRe().match(v.toString()).hasMatch()) out << v.toString();
    return out;
}

// add a server (to the front: most recently used; front false: only if new)
static void remember(const QString& server, bool front = true) {
    if (server.isEmpty() || !hostRe().match(server).hasMatch()) return;
    QStringList h = loadHistory(), rest;
    for (const QString& s : h) if (s.compare(server, Qt::CaseInsensitive) != 0) rest << s;
    if (!front && rest.size() != h.size()) return;   // already listed: keep its place
    saveHistory(QStringList{server} + rest);
}

static void forget(const QString& server) {
    QStringList h;
    for (const QString& s : loadHistory()) if (s.compare(server, Qt::CaseInsensitive) != 0) h << s;
    saveHistory(h);
}

// the upload centre-cropped to 16:9 and shrunk to TX_IMAGE_MAX_W, as JPEG;
// unchanged (no note) if it can't be decoded
static QByteArray fitPicture(const QByteArray& data, QString* note) {
    QImage img;
    if (!img.loadFromData(data)) return data;
    int w = img.width(), h = img.height();
    QImage c = img;
    if ((double)w / h > TX_IMAGE_ASPECT) {           // too wide: trim the sides
        int nw = (int)std::lround(h * TX_IMAGE_ASPECT);
        c = img.copy((w - nw) / 2, 0, nw, h);
    } else {                                         // too tall: trim top and bottom
        int nh = (int)std::lround(w / TX_IMAGE_ASPECT);
        c = img.copy(0, (h - nh) / 2, w, nh);
    }
    bool trimmed = c.size() != img.size();
    if (c.width() > TX_IMAGE_MAX_W)
        c = c.scaled(TX_IMAGE_MAX_W, (int)std::lround(TX_IMAGE_MAX_W / TX_IMAGE_ASPECT), Qt::IgnoreAspectRatio,
                     Qt::SmoothTransformation);
    QByteArray out;
    QBuffer b(&out);
    b.open(QIODevice::WriteOnly);
    if (!c.save(&b, "JPEG", 92)) return data;
    *note = trimmed ? QString(", trimmed from %1x%2 to 16:9 (%3x%4)").arg(w).arg(h).arg(c.width()).arg(c.height())
                    : QString(" (%1x%2)").arg(c.width()).arg(c.height());
    return out;
}

static const char* PAGE = R"(<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AVM settings</title>
<style>
 body { font-family: system-ui, sans-serif; background: #25272b; color: #e6e8eb; margin: 0; padding: 16px; }
 h1 { font-size: 1.3em; margin: 0 0 12px; } .card { background: #2f3237; border-radius: 8px; padding: 14px; flex: 1 1 340px; min-width: 0; }
 .tiles { display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-start; }
 label { display: block; color: #9aa0a8; margin-bottom: 6px; }
 input { width: 100%; box-sizing: border-box; font-size: 1.1em; padding: 10px; border-radius: 6px; border: 1px solid #555; background: #1c1d20; color: #fff; }
 button { margin-top: 10px; font-size: 1.05em; padding: 10px 18px; border: 0; border-radius: 6px; background: #2f9e5a; color: #111; font-weight: bold; }
 .hist { width: 100%; border-collapse: collapse; margin-top: 6px; } .hist td { padding: 4px 0; border-top: 1px solid #3c4046; }
 .hist form { margin: 0; display: inline; } .hist button { margin: 0 0 0 6px; padding: 6px 12px; font-size: 0.95em; }
 .hist .del { background: #6b2a24; color: #fff; } .hist .cur { color: #2f9e5a; } .hist td.b { text-align: right; white-space: nowrap; }
 .msg { margin: 10px 0; padding: 8px 10px; border-radius: 6px; } .dim { color: #9aa0a8; font-size: 0.9em; }
</style></head><body>
<h1>%TITLE%</h1>
<div class="tiles">
<div class="card">
 <form method="post" action="/kiwi">
  <label for="kiwi">KiwiSDR server (host:port) for RX</label>
  <input id="kiwi" name="kiwi" value="%KIWI%" placeholder="e.g. kiwi.example.org:8073" autocapitalize="off" autocorrect="off" spellcheck="false">
  <button type="submit">Set</button>
 </form>
 %MSG%
 <p class="dim">Current: <b>%CURRENT%</b>. Used the next time RX starts with the KiwiSDR radio
 (Config, Receiver shows the same setting). Leave empty to clear.</p>
 %HISTORY%
</div>
<div class="card">
 <form method="post" action="/image" enctype="multipart/form-data">
  <label for="img">TX picture for the Image source (JPEG, PNG, GIF, BMP or WebP, up to 10 MB)</label>
  <input id="img" type="file" name="image" accept="image/*">
  <button type="submit">Upload</button>
 </form>
 %IMAGE_MSG%
 %IMAGE%
 <p class="dim">Sent from the next TX start with Video "Image" (TX page). Scaled to the TX resolution.</p>
</div>
<div class="card">
 <label>AVM screen</label>
 <p><a href="/" onclick="var i=document.getElementById('shot');i.src='/screenshot.png?t='+Date.now();i.style.display='block';return false"><button type="button">Take screenshot</button></a>
 <a href="/screenshot.png?dl=1"><button type="button">Save screenshot</button></a></p>
 <img id="shot" alt="AVM screen" style="display:none;max-width:100%;border-radius:6px">
 <p class="dim">A picture of the AVM screen as it is now. Save downloads the one shown as a PNG file (a new one if none was taken).</p>
</div>
<div class="card">
 <label>Recordings (%REC_COUNT%)</label>
 %REC_MSG%
 %RECORD%
 %RECORDINGS%
 <p class="dim">The received picture and sound (Record), and screen recordings from ~/record.sh, newest first. Tap one to play it here, or Download to save it.</p>
</div>
</div></body></html>)";

WebServer::WebServer(const QString& title, Hooks hooks, QObject* parent)
    : QObject(parent), title_(title), hooks_(std::move(hooks)), server_(new QTcpServer(this)) {
    bool ok = false;
    port_ = qEnvironmentVariableIntValue("AVM_WEB_PORT", &ok);
    if (!ok) port_ = DEFAULT_PORT;
    if (port_ <= 0) return;
    connect(server_, &QTcpServer::newConnection, this, &WebServer::onConnection);
    if (server_->listen(QHostAddress::Any, (quint16)port_))
        guiLog("WEB", QString("settings page on port %1").arg(port_));
    else
        guiLog("WEB", QString("settings page not started on port %1: %2").arg(port_).arg(server_->errorString()));
}

WebServer::~WebServer() {
    for (auto it = conns_.begin(); it != conns_.end(); ++it) delete it->file;
}

bool WebServer::listening() const { return server_->isListening(); }

void WebServer::onConnection() {
    while (QTcpSocket* s = server_->nextPendingConnection()) {
        conns_.insert(s, Conn());
        connect(s, &QTcpSocket::readyRead, this, [this, s] { onReadyRead(s); });
        connect(s, &QTcpSocket::bytesWritten, this, [this, s] { pump(s); });
        connect(s, &QTcpSocket::disconnected, this, [this, s] { drop(s); });
    }
}

void WebServer::drop(QTcpSocket* s) {
    auto it = conns_.find(s);
    if (it != conns_.end()) {
        delete it->file;
        conns_.erase(it);
    }
    s->deleteLater();
}

void WebServer::onReadyRead(QTcpSocket* s) {
    auto it = conns_.find(s);
    if (it == conns_.end()) return;
    Conn& c = *it;
    if (c.file) { s->readAll(); return; }       // (already answering)
    if (c.discard > 0) {
        c.discard -= s->readAll().size();
        if (c.discard <= 0) {
            if (c.tooBigImage) sendPage(s, QString(), true, nullptr, "No file, or larger than 10 MB -- nothing changed.", false);
            else sendError(s, 413, "Too large");
        }
        return;
    }
    c.in += s->readAll();
    if (c.headerEnd < 0) {
        c.headerEnd = c.in.indexOf("\r\n\r\n");
        if (c.headerEnd < 0) {
            if (c.in.size() > 64 * 1024) sendError(s, 431, "Request header too large");
            return;
        }
        static const QRegularExpression len(R"(\r\ncontent-length:\s*(\d+))", QRegularExpression::CaseInsensitiveOption);
        auto m = len.match(QString::fromLatin1(c.in.left(c.headerEnd + 2)));
        c.bodyLen = m.hasMatch() ? m.captured(1).toLongLong() : 0;
        if (c.bodyLen > REQUEST_MAX_BYTES) {
            c.tooBigImage = c.in.startsWith("POST /image");
            if (c.bodyLen > 64LL * 1024 * 1024) return sendError(s, 413, "Too large");
            c.discard = c.headerEnd + 4 + c.bodyLen - c.in.size();
            c.in.clear();
            if (c.discard <= 0) {      // (all here already)
                if (c.tooBigImage) sendPage(s, QString(), true, nullptr, "No file, or larger than 10 MB -- nothing changed.", false);
                else sendError(s, 413, "Too large");
            }
            return;
        }
    }
    if (c.in.size() < c.headerEnd + 4 + c.bodyLen) return;
    QList<QByteArray> lines = c.in.left(c.headerEnd).split('\n');
    QList<QByteArray> req = lines.value(0).trimmed().split(' ');
    QHash<QByteArray, QByteArray> headers;
    for (int i = 1; i < lines.size(); ++i) {
        int colon = lines[i].indexOf(':');
        if (colon > 0) headers.insert(lines[i].left(colon).trimmed().toLower(), lines[i].mid(colon + 1).trimmed());
    }
    QByteArray body = c.in.mid(c.headerEnd + 4, c.bodyLen);
    c.in.clear();
    handle(s, req.value(0), req.value(1), headers, body);
}

void WebServer::send(QTcpSocket* s, int code, const QByteArray& type, const QByteArray& body,
                     const QList<QPair<QByteArray, QByteArray>>& extra) {
    static const QHash<int, QByteArray> reason = {{200, "OK"}, {206, "Partial Content"}, {404, "Not Found"},
                                                  {413, "Payload Too Large"}, {416, "Range Not Satisfiable"},
                                                  {431, "Request Header Fields Too Large"}, {503, "Service Unavailable"}};
    QByteArray h = "HTTP/1.1 " + QByteArray::number(code) + " " + reason.value(code, "Error") + "\r\n";
    h += "Content-Type: " + type + "\r\nContent-Length: " + QByteArray::number(body.size()) + "\r\n";
    for (const auto& kv : extra) h += kv.first + ": " + kv.second + "\r\n";
    h += "Connection: close\r\n\r\n";
    s->write(h + body);
    s->disconnectFromHost();      // (after the data is written)
}

void WebServer::sendError(QTcpSocket* s, int code, const QByteArray& text) {
    send(s, code, "text/plain; charset=utf-8", text + "\n");
}

void WebServer::sendPage(QTcpSocket* s, const QString& msg, bool ok, const QString* value, const QString& imageMsg,
                         bool imageOk, const QString& recMsg, bool recOk) {
    QString now = hooks_.kiwi ? hooks_.kiwi() : QString();
    QString live = value && ok ? *value : now;          // just set: may not be applied yet
    remember(live, false);                               // one chosen on the touchscreen joins the list
    QString rows;
    for (const QString& srv : loadHistory()) {
        QString e = esc(srv);
        QString use = srv.compare(live, Qt::CaseInsensitive) == 0
            ? QString("<span class=\"cur\">in use</span>")
            : QString("<form method=\"post\" action=\"/kiwi/use\"><input type=\"hidden\" name=\"kiwi\" value=\"%1\">"
                      "<button type=\"submit\">Use</button></form>").arg(e);
        rows += QString("<tr><td>%1</td><td class=\"b\">%2<form method=\"post\" action=\"/kiwi/delete\">"
                        "<input type=\"hidden\" name=\"kiwi\" value=\"%1\"><button class=\"del\" type=\"submit\" "
                        "title=\"Remove from the list\">&#10005;</button></form></td></tr>").arg(e, use);
    }
    QString history = rows.isEmpty() ? QString() : "<label>Used before</label><table class=\"hist\">" + rows + "</table>";
    QString imgPath = hooks_.image ? hooks_.image() : QString();
    QString image = !imgPath.isEmpty() && QFileInfo(imgPath).isFile()
        ? QString("<p><img src=\"/image?t=%1\" alt=\"current TX picture\" style=\"max-width:100%;max-height:240px;"
                  "border-radius:6px\"></p>").arg(QFileInfo(imgPath).lastModified().toSecsSinceEpoch())
        : QString("<p class=\"dim\">No picture chosen yet (the test pattern is sent instead).</p>");
    QFileInfoList recs = QDir(recordingsDir()).entryInfoList({"*.mp4"}, QDir::Files, QDir::Time);
    QString items;
    int shown = 0, count = 0;
    for (const QFileInfo& fi : recs) {
        if (!recordingRe().match(fi.fileName()).hasMatch()) continue;
        ++count;
        if (shown++ >= RECORDINGS_SHOWN) continue;
        QString u = QString::fromLatin1(QUrl::toPercentEncoding(fi.fileName()));
        items += QString("<details><summary>%1 &middot; %2 MB</summary><video controls preload=\"none\" src=\"/rec/%3\" "
                         "style=\"width:100%;border-radius:6px;margin-top:6px\"></video><p><a href=\"/rec/%3?dl=1\">"
                         "<button type=\"button\">Download</button></a></p></details>")
                     .arg(fi.lastModified().toString("dd MMM HH:mm"))
                     .arg(fi.size() / 1e6, 0, 'f', 1)
                     .arg(u);
    }
    if (items.isEmpty()) items = "<p class=\"dim\">No recordings yet.</p>";
    auto box = [](const QString& m, bool good) {
        return m.isEmpty() ? QString()
                           : QString("<div class=\"msg\" style=\"background:%1\">%2</div>").arg(good ? "#24543a" : "#6b2a24", esc(m));
    };
    QString page = QString::fromUtf8(PAGE);
    page.replace("%TITLE%", esc(title_));
    page.replace("%KIWI%", esc(value ? *value : now));
    page.replace("%CURRENT%", esc(live.isEmpty() ? QString("(none)") : live));
    page.replace("%MSG%", box(msg, ok));
    page.replace("%HISTORY%", history);
    page.replace("%IMAGE_MSG%", box(imageMsg, imageOk));
    page.replace("%IMAGE%", image);
    // Record: the received picture and sound, into ~/recordings
    const int rs = hooks_.recordState ? hooks_.recordState() : 0;
    QString rec = rs == 2 ? "<form method=\"post\" action=\"/record\"><input type=\"hidden\" name=\"on\" value=\"0\">"
                            "<button type=\"submit\">&#9632; Stop recording</button> <span class=\"dim\">recording...</span></form>"
                : rs == 1 ? "<form method=\"post\" action=\"/record\"><input type=\"hidden\" name=\"on\" value=\"1\">"
                            "<button type=\"submit\">&#9679; Record</button></form>"
                          : "<p class=\"dim\">Record: start RX first.</p>";
    page.replace("%RECORD%", rec);
    page.replace("%REC_MSG%", box(recMsg, recOk));
    page.replace("%REC_COUNT%", QString::number(count));
    page.replace("%RECORDINGS%", items);
    send(s, 200, "text/html; charset=utf-8", page.toUtf8());
}

void WebServer::handle(QTcpSocket* s, const QByteArray& method, const QByteArray& target,
                       const QHash<QByteArray, QByteArray>& headers, const QByteArray& body) {
    QByteArray path = target.split('?').value(0);
    bool dl = target.contains("dl=1");
    if (method == "GET") {
        if (path == "/image") {
            QFile f(hooks_.image ? hooks_.image() : QString());
            if (!f.open(QIODevice::ReadOnly)) return sendError(s, 404, "No picture");
            QByteArray d = f.readAll();
            const char* t = imageType(d);
            return send(s, 200, t ? t : "application/octet-stream", d, {{"Cache-Control", "no-store"}});
        }
        if (path == "/screenshot.png") {
            QByteArray d = dl ? lastShot_ : QByteArray();     // Save: the picture taken, not a new one
            if (d.isEmpty()) d = lastShot_ = hooks_.screenshot ? hooks_.screenshot() : QByteArray();
            if (d.isEmpty()) return sendError(s, 503, "Screenshot not available");
            QList<QPair<QByteArray, QByteArray>> extra = {{"Cache-Control", "no-store"}};
            if (dl)
                extra.append({"Content-Disposition",
                              "attachment; filename=\"" + QDateTime::currentDateTime().toString("'avm_'yyyyMMdd_HHmmss'.png'").toLatin1() + "\""});
            return send(s, 200, "image/png", d, extra);
        }
        if (path.startsWith("/rec/"))
            return sendRecording(s, QUrl::fromPercentEncoding(path.mid(5)), dl, headers.value("range"));
        if (path != "/" && path != "/index.html") return sendError(s, 404, "Not found");
        return sendPage(s);
    }
    if (method == "POST") {
        if (path == "/image") return uploadImage(s, headers, body);
        if (path == "/record") {
            const bool on = QUrlQuery(QString::fromUtf8(body.left(64))).queryItemValue("on") == "1";
            if (hooks_.setRecording) hooks_.setRecording(on);
            const bool rec = hooks_.recordState && hooks_.recordState() == 2;
            return sendPage(s, QString(), true, nullptr, QString(), true,
                            on ? (rec ? "Recording from the next picture." : "Not recording: start RX first.")
                               : "Recording stopped and saved.", !on || rec);
        }
        if (path != "/kiwi" && path != "/kiwi/use" && path != "/kiwi/delete") return sendError(s, 404, "Not found");
        QUrlQuery q(QString::fromUtf8(body.left(1024)).replace('+', ' '));
        QString server = q.queryItemValue("kiwi", QUrl::FullyDecoded).trimmed();
        if (!server.isEmpty() && !hostRe().match(server).hasMatch())
            return sendPage(s, "Not a valid host or host:port -- nothing changed.", false, &server);
        if (path == "/kiwi/delete") {
            forget(server);
            return sendPage(s, server + " removed from the list.");
        }
        if (hooks_.setKiwi) hooks_.setKiwi(server);
        remember(server);
        return sendPage(s, server.isEmpty() ? QString("KiwiSDR server cleared.") : "KiwiSDR server set to " + server + ".",
                        true, &server);
    }
    sendError(s, 404, "Not found");
}

void WebServer::uploadImage(QTcpSocket* s, const QHash<QByteArray, QByteArray>& headers, const QByteArray& body) {
    // multipart/form-data: the part named "image"
    QByteArray type = headers.value("content-type");
    int b = type.indexOf("boundary=");
    QByteArray data;
    if (b >= 0) {
        QByteArray boundary = "--" + type.mid(b + 9).split(';').value(0).trimmed();
        if (boundary.size() > 2 && boundary.startsWith("--\"")) boundary = "--" + boundary.mid(3, boundary.size() - 4);
        int at = 0;
        while ((at = body.indexOf(boundary, at)) >= 0) {
            int hs = at + boundary.size() + 2, he = body.indexOf("\r\n\r\n", hs);
            if (he < 0) break;
            QByteArray ph = body.mid(hs, he - hs);
            int next = body.indexOf("\r\n" + boundary, he + 4);
            if (next < 0) break;
            if (ph.contains("name=\"image\"")) { data = body.mid(he + 4, next - he - 4); break; }
            at = next + 2;
        }
    }
    if (data.isEmpty() || data.size() > IMAGE_MAX_BYTES)
        return sendPage(s, QString(), true, nullptr, "No picture in the upload -- nothing changed.", false);
    if (!imageType(data))
        return sendPage(s, QString(), true, nullptr, "Not a JPEG, PNG, GIF, BMP or WebP picture -- nothing changed.", false);
    QString note;
    data = fitPicture(data, &note);
    QDir().mkpath(configDir());
    QSaveFile f(uploadPath());                  // a TX start never reads half a file
    if (!f.open(QIODevice::WriteOnly) || f.write(data) != data.size() || !f.commit())
        return sendPage(s, QString(), true, nullptr, "Could not save the picture -- nothing changed.", false);
    if (hooks_.setImage) hooks_.setImage(uploadPath());
    sendPage(s, QString(), true, nullptr,
             QString("Picture uploaded%1 (%2 kB) -- sent from the next TX start.").arg(note).arg(data.size() / 1024));
}

void WebServer::sendRecording(QTcpSocket* s, const QString& name, bool download, const QByteArray& range) {
    if (!recordingRe().match(name).hasMatch()) return sendError(s, 404, "Not found");
    auto* f = new QFile(recordingsDir() + "/" + name);
    if (!f->open(QIODevice::ReadOnly)) { delete f; return sendError(s, 404, "Not found"); }
    qint64 size = f->size(), start = 0, end = size - 1;
    bool partial = false;
    static const QRegularExpression re(R"(^bytes=(\d*)-(\d*)$)");
    auto m = re.match(QString::fromLatin1(range));
    if (m.hasMatch() && (!m.captured(1).isEmpty() || !m.captured(2).isEmpty())) {
        if (!m.captured(1).isEmpty()) {
            start = m.captured(1).toLongLong();
            end = m.captured(2).isEmpty() ? size - 1 : std::min(m.captured(2).toLongLong(), size - 1);
        } else {
            start = std::max(0LL, size - m.captured(2).toLongLong());
        }
        if (start > end) { delete f; return sendError(s, 416, "Range not satisfiable"); }
        partial = true;
    }
    QByteArray h = partial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n";
    if (partial) h += "Content-Range: bytes " + QByteArray::number(start) + "-" + QByteArray::number(end) + "/" + QByteArray::number(size) + "\r\n";
    h += "Content-Type: video/mp4\r\nAccept-Ranges: bytes\r\nContent-Length: " + QByteArray::number(end - start + 1) + "\r\n";
    if (download) h += "Content-Disposition: attachment; filename=\"" + name.toUtf8() + "\"\r\n";
    h += "Connection: close\r\n\r\n";
    f->seek(start);
    Conn& c = conns_[s];
    c.file = f;
    c.left = end - start + 1;
    s->write(h);
    pump(s);
}

// a recording, 64 kB at a time as the socket drains
void WebServer::pump(QTcpSocket* s) {
    auto it = conns_.find(s);
    if (it == conns_.end() || !it->file) return;
    Conn& c = *it;
    while (c.left > 0 && s->bytesToWrite() < 256 * 1024) {
        QByteArray chunk = c.file->read(std::min<qint64>(c.left, 1 << 16));
        if (chunk.isEmpty()) { c.left = 0; break; }
        s->write(chunk);
        c.left -= chunk.size();
    }
    if (c.left <= 0) {
        delete c.file;
        c.file = nullptr;
        s->disconnectFromHost();
    }
}

}  // namespace avm
