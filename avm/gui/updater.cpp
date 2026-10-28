#include "updater.h"

#include <QApplication>
#include <QDialog>
#include <QDir>
#include <QEventLoop>
#include <QFile>
#include <QLabel>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPlainTextEdit>
#include <QProcess>
#include <QPushButton>
#include <QRegularExpression>
#include <QScrollBar>
#include <QScroller>
#include <QStandardPaths>
#include <QTimer>
#include <QVBoxLayout>

#include "pages.h"
#include "style.h"
#include "touch.h"

namespace avm {

static const int RESTART_AFTER_INSTALL_S = 10;

// "1.0.12" -> (1, 0, 12) as one comparable number; -1 if not a version
static long long versionKey(const QString& v) {
    auto m = QRegularExpression(R"(^\s*v?(\d+)\.(\d+)\.(\d+)\s*$)").match(v);
    if (!m.hasMatch()) return -1;
    return m.captured(1).toLongLong() * 1000000 + m.captured(2).toLongLong() * 1000 + m.captured(3).toLongLong();
}

QString Updater::localVersion() {
    QFile f(avmDir() + "/VERSION");
    if (!f.open(QIODevice::ReadOnly)) return QString();
    return QString::fromUtf8(f.readLine()).trimmed();
}

QString Updater::repo() {
    QString r = qEnvironmentVariable("AVM_REPO");
    return r.isEmpty() ? QString("m0dts/avm") : r;
}

QString Updater::branch() {
    QString b = qEnvironmentVariable("AVM_BRANCH");
    if (!b.isEmpty()) return b;
    QFile f(avmDir() + "/BRANCH");        // a test branch the installer was run with
    if (f.open(QIODevice::ReadOnly)) {
        b = QString::fromUtf8(f.readLine()).trimmed();
        if (!b.isEmpty()) return b;
    }
    return "main";
}

QString Updater::rawUrl(const QString& path) const {
    return QString("https://raw.githubusercontent.com/%1/%2/%3").arg(repo(), branch(), path);
}

// GitHub's VERSION; if newer, CHANGES.md's sections for the newer versions ("## 1.0.x")
void Updater::check() {
    QString here = localVersion();
    if (versionKey(here) < 0) return;      // a development copy without a VERSION: nothing to compare
    auto* nam = new QNetworkAccessManager(this);
    QNetworkRequest rq(QUrl(rawUrl("VERSION")));
    rq.setRawHeader("User-Agent", "avm-update");
    rq.setRawHeader("Cache-Control", "no-cache");
    rq.setTransferTimeout(8000);
    QNetworkReply* r = nam->get(rq);
    connect(r, &QNetworkReply::finished, this, [this, nam, r, here] {
        r->deleteLater();
        QString remote = QString::fromUtf8(r->readAll()).trimmed();
        if (r->error() != QNetworkReply::NoError || versionKey(remote) <= versionKey(here)) {   // (offline: nothing)
            nam->deleteLater();
            return;
        }
        QNetworkRequest cq(QUrl(rawUrl("CHANGES.md")));
        cq.setRawHeader("User-Agent", "avm-update");
        cq.setTransferTimeout(8000);
        QNetworkReply* c = nam->get(cq);
        connect(c, &QNetworkReply::finished, this, [this, nam, c, here, remote] {
            c->deleteLater();
            nam->deleteLater();
            QStringList out;
            bool keep = false;
            for (const QString& line : QString::fromUtf8(c->readAll()).split('\n')) {
                auto m = QRegularExpression(R"(^##\s+v?(\d+\.\d+\.\d+)(.*))").match(line);
                if (m.hasMatch()) keep = versionKey(m.captured(1)) > versionKey(here);
                if (keep && !line.trimmed().isEmpty()) out << (m.hasMatch() ? "v" + m.captured(1) + m.captured(2) : line);
                if (out.size() >= 40) { out << "..."; break; }
            }
            emit found(remote, out.isEmpty() ? QString("(no change notes)") : out.join('\n'));
        });
    });
}

#ifndef Q_OS_WIN
// Linux: the installer with its output in a full-window log (sudo needs no
// password); true if it finished well
static bool runInstallerHere(QWidget* window, const QString& script) {
    QDialog d(window);
    auto* lay = new QVBoxLayout(&d);
    auto* title = new QLabel("Updating AVM: fetching the new version and building it (a few minutes)...");
    title->setObjectName("big");
    title->setWordWrap(true);
    lay->addWidget(title);
    auto* log = new QPlainTextEdit;
    log->setReadOnly(true);
    log->setTextInteractionFlags(Qt::NoTextInteraction);
    log->setMaximumBlockCount(5000);
    QScroller::grabGesture(log->viewport(), QScroller::LeftMouseButtonGesture);
    lay->addWidget(log, 1);
    auto* btn = new QPushButton("Please wait...");
    btn->setEnabled(false);
    lay->addWidget(btn);
    coverWindow(&d, window);
    QProcess p;
    p.setProcessChannelMode(QProcess::MergedChannels);
    p.setWorkingDirectory(avmDir());
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert("AVM_BRANCH", Updater::branch());
    env.insert("AVM_REPO", Updater::repo());
    p.setProcessEnvironment(env);
    static const QRegularExpression ansi("\x1b\\[[0-9;]*[A-Za-z]");
    QObject::connect(&p, &QProcess::readyRead, &d, [&] {
        QString t = QString::fromUtf8(p.readAll()).remove(ansi).remove('\r');
        log->moveCursor(QTextCursor::End);
        log->insertPlainText(t);
        log->verticalScrollBar()->setValue(log->verticalScrollBar()->maximum());
    });
    bool ok = false;
    int left = RESTART_AFTER_INSTALL_S;
    QTimer tick;
    QObject::connect(&tick, &QTimer::timeout, &d, [&] {
        if (--left <= 0) d.accept();
        else btn->setText(QString("Restart AVM (%1)").arg(left));
    });
    QObject::connect(&p, qOverload<int, QProcess::ExitStatus>(&QProcess::finished), &d, [&](int code, QProcess::ExitStatus st) {
        ok = code == 0 && st == QProcess::NormalExit;
        btn->setEnabled(true);
        if (ok) {
            title->setText("Updated: AVM restarts now.");
            btn->setText(QString("Restart AVM (%1)").arg(left));
            QObject::connect(btn, &QPushButton::clicked, &d, &QDialog::accept);
            tick.start(1000);
        } else {
            title->setText("The update didn't finish (the log above says why). AVM carries on as it was.");
            btn->setText("Close");
            QObject::connect(btn, &QPushButton::clicked, &d, &QDialog::reject);
        }
    });
    p.start("bash", {script, "--update", "--yes"});
    d.exec();
    if (p.state() != QProcess::NotRunning) { p.kill(); p.waitForFinished(2000); }
    return ok;
}
#endif

bool Updater::apply(QWidget* window) {
#ifdef Q_OS_WIN
    // the latest installer, in its own console window: it fetches the new zip
    // (closing this AVM first) and starts AVM again
    QNetworkAccessManager nam;
    QNetworkRequest rq(QUrl(rawUrl("avm/install_avm_windows.ps1")));
    rq.setRawHeader("User-Agent", "avm-update");
    rq.setTransferTimeout(20000);
    QNetworkReply* r = nam.get(rq);
    QEventLoop loop;
    QObject::connect(r, &QNetworkReply::finished, &loop, &QEventLoop::quit);
    loop.exec();
    QByteArray ps1 = r->readAll();
    bool got = r->error() == QNetworkReply::NoError && ps1.contains("AVM installer");
    r->deleteLater();
    if (!got) {
        Notices::get().add("Update", "Couldn't download the installer -- check the internet connection");
        return false;
    }
    QString path = QDir::tempPath() + "/install_avm_windows.ps1";
    QFile f(path);
    if (!f.open(QIODevice::WriteOnly) || f.write(ps1) != ps1.size()) return false;
    f.close();
    if (!QProcess::startDetached("cmd", {"/c", "start", "AVM update", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                         "-File", QDir::toNativeSeparators(path), "-Restart"}))
        return false;
    qApp->quit();
    return true;
#else
    QString script = avmDir() + "/install_avm.sh";
    if (!QFile::exists(script)) {
        Notices::get().add("Update", "install_avm.sh isn't in " + avmDir() + " -- update by hand");
        return false;
    }
    const QString exe = QCoreApplication::applicationFilePath();
    const QStringList args = QCoreApplication::arguments().mid(1);
    if (QProcess::execute("sudo", {"-n", "true"}) == 0) {
        // (a Pi, normally): in the window -- a terminal would bring the on-screen keyboard up over it
        if (!runInstallerHere(window, script)) return false;
        QProcess::startDetached(exe, args, avmDir());
        qApp->quit();
        return true;
    }
    // sudo asks for a password: in a terminal, which starts AVM again when done
    QDir().mkpath(avmDir() + "/gui_logs");
    QString sh = avmDir() + "/gui_logs/update_then_restart.sh";
    QFile f(sh);
    if (!f.open(QIODevice::WriteOnly)) return false;
    QString quotedArgs;
    for (const QString& a : args) quotedArgs += " '" + QString(a).replace("'", "'\\''") + "'";
    f.write(QString("#!/bin/bash\n"
                    "export AVM_BRANCH='%1' AVM_REPO='%2'\n"
                    "if bash '%3' --update --yes; then\n"
                    "  echo; echo 'AVM restarts in 10 s...'; sleep 10\n"
                    "  cd '%4' && nohup '%5'%6 >/dev/null 2>&1 &\n"
                    "else\n"
                    "  echo; echo 'The update did not finish (see above). Press Enter to close.'; read -r _\n"
                    "fi\n")
                .arg(branch(), repo(), script, avmDir(), exe, quotedArgs).toUtf8());
    f.close();
    f.setPermissions(f.permissions() | QFileDevice::ExeOwner);
    static const QList<QPair<QString, QStringList>> terms = {
        {"x-terminal-emulator", {"-e"}}, {"lxterminal", {"-e"}}, {"xfce4-terminal", {"-e"}}, {"mate-terminal", {"-e"}},
        {"qterminal", {"-e"}}, {"gnome-terminal", {"--"}}, {"konsole", {"-e"}}, {"xterm", {"-e"}}};
    for (const auto& t : terms) {
        if (QStandardPaths::findExecutable(t.first).isEmpty()) continue;
        if (QProcess::startDetached(t.first, t.second + QStringList{sh})) {
            qApp->quit();
            return true;
        }
    }
    Notices::get().add("Update", "No terminal to run the installer in -- run: bash " + script + " --update");
    return false;
#endif
}

}  // namespace avm
