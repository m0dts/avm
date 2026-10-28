// web.h -- a small web page for AVM from a phone or PC on the same network
// (avm_web.py): http://<this machine>:8080/ (AVM_WEB_PORT to change; 0 = off).
//
// The KiwiSDR server the RX uses (Config, Receiver) with the servers used
// before, the picture the TX sends with the Image source, a screenshot of the
// AVM screen, and the screen recordings in ~/recordings (~/record.sh).
// No login -- anyone on the local network can use it -- and it accepts only a
// host or host:port and a picture, so nothing else can be set through it.
//
// Runs in the GUI's own event loop (QTcpServer): a change goes through the
// same code as the Config page.
#pragma once

#include <QByteArray>
#include <QHash>
#include <QObject>
#include <functional>

class QTcpServer;
class QTcpSocket;
class QFile;

namespace avm {

class WebServer : public QObject {
    Q_OBJECT
public:
    struct Hooks {
        std::function<QString()> kiwi;                  // the server in use now
        std::function<void(const QString&)> setKiwi;    // set it (as Config's KiwiSDR row)
        std::function<QString()> image;                 // the TX picture's path now
        std::function<void(const QString&)> setImage;   // use this picture from the next TX start
        std::function<QByteArray()> screenshot;         // the window as PNG
    };
    WebServer(const QString& title, Hooks hooks, QObject* parent = nullptr);
    ~WebServer() override;
    bool listening() const;
    int port() const { return port_; }

private:
    struct Conn {
        QByteArray in;
        int headerEnd = -1;
        qint64 bodyLen = 0;
        QFile* file = nullptr;       // a recording being sent
        qint64 left = 0;
        qint64 discard = 0;          // a too-large upload still arriving: read and dropped,
        bool tooBigImage = false;    // then answered (answered at once, the browser saw a reset)
    };
    void onConnection();
    void onReadyRead(QTcpSocket* s);
    void handle(QTcpSocket* s, const QByteArray& method, const QByteArray& target,
                const QHash<QByteArray, QByteArray>& headers, const QByteArray& body);
    void sendPage(QTcpSocket* s, const QString& msg = QString(), bool ok = true, const QString* value = nullptr,
                  const QString& imageMsg = QString(), bool imageOk = true);
    void send(QTcpSocket* s, int code, const QByteArray& type, const QByteArray& body,
              const QList<QPair<QByteArray, QByteArray>>& extra = {});
    void sendError(QTcpSocket* s, int code, const QByteArray& text);
    void sendRecording(QTcpSocket* s, const QString& name, bool download, const QByteArray& range);
    void pump(QTcpSocket* s);
    void uploadImage(QTcpSocket* s, const QHash<QByteArray, QByteArray>& headers, const QByteArray& body);
    void drop(QTcpSocket* s);

    QString title_;
    Hooks hooks_;
    QTcpServer* server_;
    int port_ = 0;
    QHash<QTcpSocket*, Conn> conns_;
    QByteArray lastShot_;            // the last screenshot taken, for Save
};

}  // namespace avm
