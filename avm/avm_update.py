"""Is this AVM up to date with GitHub? Compares each file here with the
repository's avm/ folder by git blob hash (what GitHub's tree API lists), so
it works however AVM was installed -- installer, copy or deploy.

    python avm_update.py          # report, for a terminal / the installer

The touch GUI runs check() in the background at start-up and shows an
"Update" button when files differ. No internet: check() raises OSError (the
GUI then shows nothing)."""
import hashlib
import json
import os
import sys
import urllib.request

REPO = os.environ.get("AVM_REPO", "m0dts/avm")
BRANCH = os.environ.get("AVM_BRANCH", "main")
SUBDIR = "avm/"  # the program files' folder in the repository
UPDATE_CMD = "bash ~/avm/install_avm.sh --update"


def _blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_blob_shas(path):
    """The hashes git (and GitHub) could give this file: as it is, and with
    Windows line endings turned to LF (git for Windows may store either)."""
    with open(path, "rb") as f:
        data = f.read()
    shas = {_blob_sha(data)}
    if b"\r\n" in data:
        shas.add(_blob_sha(data.replace(b"\r\n", b"\n")))
    return shas


def remote_files(timeout=8.0):
    """{path inside avm/: blob hash} on GitHub."""
    url = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
    req = urllib.request.Request(url, headers={"User-Agent": "avm-update-check",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        tree = json.load(r)
    return {e["path"][len(SUBDIR):]: e["sha"] for e in tree.get("tree", [])
            if e.get("type") == "blob" and e["path"].startswith(SUBDIR)}


def check(avm_dir=None, timeout=8.0):
    """Files whose GitHub version differs from (or is missing in) avm_dir,
    sorted. Empty: up to date. Raises OSError / ValueError if GitHub can't
    be reached or read."""
    avm_dir = avm_dir or os.path.dirname(os.path.abspath(__file__))
    changed = []
    for rel, sha in remote_files(timeout).items():
        local = os.path.join(avm_dir, *rel.split("/"))
        if not os.path.isfile(local) or sha not in git_blob_shas(local):
            changed.append(rel)
    return sorted(changed)


def apply(avm_dir=None, timeout=60.0):
    """Download GitHub's AVM and replace the files that differ (program
    files only: no system packages, so no sudo). Each file is written to a
    temporary name, then swapped in, so a failed download changes nothing.
    Returns the files updated. Settings and logs are untouched."""
    import io
    import tarfile
    avm_dir = avm_dir or os.path.dirname(os.path.abspath(__file__))
    changed = set(check(avm_dir))
    if not changed:
        return []
    url = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.tar.gz"
    req = urllib.request.Request(url, headers={"User-Agent": "avm-update"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        archive = r.read()
    new = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)  # "<repo>-<branch>/avm/..."
            if m.isfile() and len(parts) == 2 and parts[1].startswith(SUBDIR):
                rel = parts[1][len(SUBDIR):]
                if rel in changed and ".." not in rel.split("/"):
                    new[rel] = tar.extractfile(m).read()
    if set(new) != changed:
        raise ValueError(f"download is missing {len(changed - set(new))} file(s)")
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
    try:
        changed = check()
    except (OSError, ValueError) as e:
        print(f"AVM files: couldn't check GitHub ({e})")
        return 2
    if not changed:
        print(f"AVM files: up to date with github.com/{REPO}")
        return 0
    print(f"AVM files: {len(changed)} differ from github.com/{REPO} -- update with: {UPDATE_CMD}")
    for f in changed:
        print(f"    {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
