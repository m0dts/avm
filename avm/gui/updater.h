// updater.h -- is there a newer AVM? (avm_update.py's): GitHub's VERSION
// file against this AVM's (avmDir()/VERSION), checked in the background at
// start-up -- one small download, so no rate limit; only a HIGHER number
// counts. CHANGES.md says what's new. Updating runs the installer:
//   Linux: install_avm.sh --update (fetches the new source and rebuilds),
//     its output shown in the window when sudo needs no password (a Pi),
//     else in a terminal; then AVM restarts;
//   Windows: the latest install_avm_windows.ps1 -Restart in its own window
//     (it fetches the new release zip and starts AVM again).
// AVM_REPO / AVM_BRANCH (or a BRANCH file in the AVM folder, written by the
// installer for a test branch): where to look.
#pragma once
#include <QObject>
#include <QString>

class QWidget;

namespace avm {

class Updater : public QObject {
    Q_OBJECT
public:
    explicit Updater(QObject* parent = nullptr) : QObject(parent) {}
    static QString localVersion();      // "1.0.21" ("" if unknown)
    static QString repo();
    static QString branch();
    void check();                       // in the background: found() if newer
    // stop nothing itself: the caller stops TX / RX first. Returns false if it
    // couldn't start (then nothing changed)
    bool apply(QWidget* window);
signals:
    void found(const QString& version, const QString& notes);
private:
    QString rawUrl(const QString& path) const;
};

}  // namespace avm
