"""Is there a newer AVM on GitHub? Compares version numbers: this AVM's
(avm_version.VERSION, 1.0.x) with the VERSION file at the top of the
repository -- one small plain download, no GitHub API (so no rate limit), and
only a HIGHER number counts: a development copy that's ahead of GitHub, or
one with local edits, is never offered an "update" backwards. CHANGES.md
there says what each version brought.

    python avm_update.py           # this version vs GitHub's, and what's new
    python avm_update.py --files   # also: which files here differ from GitHub's

The touch GUI runs check() in the background at start-up and shows an
"Update" button with the new version when there is one; apply() fetches it.
No internet: check() raises OSError (the GUI then shows nothing)."""
import io
import os
import re
import sys
import tarfile
import urllib.request

REPO = os.environ.get("AVM_REPO", "m0dts/avm")
BRANCH = os.environ.get("AVM_BRANCH", "main")
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
SUBDIR = "avm/"  # the program files' folder in the repository
UPDATE_CMD = "bash ~/avm/install_avm.sh --update"
# files whose update means the installer itself should run again (it may
# have new steps: packages, ffmpeg, drivers...)
INSTALLER_FILES = ("install_avm.sh", "install_avm_windows.ps1", "install_avm.bat")


def run_installer_then_restart(avm_dir=None):
    """After a GUI update that changed an installer: start that installer in
    a window of its own, which restarts AVM when done. Returns False if it
    can't be shown (Linux with no terminal program): the caller then just
    restarts AVM and asks for the installer to be run by hand."""
    import shutil
    import subprocess
    avm_dir = avm_dir or os.path.dirname(os.path.abspath(__file__))
    if sys.platform == "win32":
        bat = os.path.join(avm_dir, "install_avm.bat")
        if not os.path.exists(bat):
            return False
        # its own console window: "start" opens one whatever this process has
        subprocess.Popen(["cmd", "/c", "start", "AVM update", bat, "-Restart"], cwd=avm_dir)
        return True
    # Linux: in a terminal, so sudo can ask for the password
    script = os.path.join(avm_dir, "gui_logs", "update_then_restart.sh")
    os.makedirs(os.path.dirname(script), exist_ok=True)
    restart = (f'cd "{avm_dir}" && HF_RX_GUI_LOG="{avm_dir}/gui_logs/rx_session.log" '
               f'nohup "{sys.executable}" touch_gui.py >/dev/null 2>&1 &')
    with open(script, "w") as f:
        f.write("\n".join([
            "#!/bin/bash",
            f'bash "{avm_dir}/install_avm.sh" --yes',
            'echo; read -r -p "Press Enter to restart AVM " _',
            restart,
            ""]))
    os.chmod(script, 0o755)
    for term, args in (("x-terminal-emulator", ["-e", script]), ("lxterminal", ["-e", script]),
                       ("xfce4-terminal", ["-e", script]), ("mate-terminal", ["-e", script]),
                       ("qterminal", ["-e", script]), ("gnome-terminal", ["--", script]),
                       ("konsole", ["-e", script]), ("xterm", ["-e", script])):
        if shutil.which(term):
            subprocess.Popen([term] + args, start_new_session=True)
            return True
    return False


def _version_tuple(text):
    """'1.0.12' -> (1, 0, 12); anything else -> None."""
    m = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", text or "")
    return tuple(int(x) for x in m.groups()) if m else None


def local_version():
    import avm_version
    return avm_version.VERSION


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "avm-update",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def remote_version(timeout=8.0):
    """GitHub's VERSION file, e.g. '1.0.4'. Raises OSError / ValueError."""
    text = _fetch(f"{RAW}/VERSION", timeout).decode("utf-8", "replace").strip()
    if not _version_tuple(text):
        raise ValueError(f"VERSION on GitHub isn't a version number: {text[:30]!r}")
    return text


def check(timeout=8.0):
    """GitHub's version if it's NEWER than this one, else None. Raises
    OSError / ValueError if GitHub can't be reached or read."""
    remote = remote_version(timeout)
    return remote if _version_tuple(remote) > _version_tuple(local_version()) else None


def changes_since(version, timeout=8.0, limit=12):
    """What CHANGES.md on GitHub lists for versions newer than `version`, as
    text ('' if none or it can't be read). Sections start '## 1.0.x'."""
    try:
        text = _fetch(f"{RAW}/CHANGES.md", timeout).decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    have = _version_tuple(version)
    out, keep = [], False
    for line in text.splitlines():
        m = re.match(r"^##\s+v?(\d+\.\d+\.\d+)", line)
        if m:
            keep = _version_tuple(m.group(1)) > have
        if keep and line.strip():
            out.append(line.lstrip("#").strip() if m else line)
    return "\n".join(out[:limit]) + ("\n..." if len(out) > limit else "")


def _same(local_path, data):
    """Does this file already hold data? (CRLF vs LF doesn't count: git for
    Windows may store either.)"""
    try:
        with open(local_path, "rb") as f:
            have = f.read()
    except OSError:
        return False
    return have == data or (have.replace(b"\r\n", b"\n")
                            == data.replace(b"\r\n", b"\n"))


def _download(timeout):
    """{path inside avm/: contents} from GitHub's archive of the branch."""
    archive = _fetch(f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.tar.gz", timeout)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)  # "<repo>-<branch>/avm/..."
            if m.isfile() and len(parts) == 2 and parts[1].startswith(SUBDIR):
                rel = parts[1][len(SUBDIR):]
                if rel and ".." not in rel.split("/"):
                    files[rel] = tar.extractfile(m).read()
    if "touch_gui.py" not in files:
        raise ValueError("the download has no AVM in it")
    return files


def differing_files(avm_dir=None, timeout=60.0):
    """Files here that differ from GitHub's (or are missing), sorted."""
    avm_dir = avm_dir or os.path.dirname(os.path.abspath(__file__))
    return sorted(rel for rel, data in _download(timeout).items()
                  if not _same(os.path.join(avm_dir, *rel.split("/")), data))


def apply(avm_dir=None, timeout=60.0):
    """Download GitHub's AVM and replace the files that differ (program
    files only: no system packages, so no sudo). Each file is written to a
    temporary name, then swapped in, so a failed download changes nothing.
    Returns the files updated. Settings and logs are untouched."""
    avm_dir = avm_dir or os.path.dirname(os.path.abspath(__file__))
    new = {rel: data for rel, data in _download(timeout).items()
           if not _same(os.path.join(avm_dir, *rel.split("/")), data)}
    tmp = []
    try:
        for rel, data in new.items():
            dst = os.path.join(avm_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst + ".avm-new", "wb") as f:
                f.write(data)
            tmp.append(dst)
        for dst in tmp:
            os.replace(dst + ".avm-new", dst)
    finally:
        for dst in tmp:
            if os.path.exists(dst + ".avm-new"):
                os.remove(dst + ".avm-new")
    return sorted(new)


def main():
    here = local_version()
    try:
        remote = remote_version()
    except (OSError, ValueError) as e:
        print(f"AVM v{here}: couldn't check GitHub ({e})")
        return 2
    newer = _version_tuple(remote) > _version_tuple(here)
    if newer:
        print(f"AVM v{here}: v{remote} is available -- update with: {UPDATE_CMD}")
        notes = changes_since(here)
        if notes:
            print("    " + notes.replace("\n", "\n    "))
    elif _version_tuple(remote) == _version_tuple(here):
        print(f"AVM v{here}: up to date with github.com/{REPO}")
    else:
        print(f"AVM v{here}: newer than github.com/{REPO} (v{remote})")
    if "--files" in sys.argv:
        diff = differing_files()
        print(f"files differing from GitHub's: {len(diff)}" + "".join(f"\n    {f}" for f in diff))
    return 1 if newer else 0


if __name__ == "__main__":
    sys.exit(main())
