"""Disk map: what takes the space, one folder level at a time.

`du` counts each subfolder at the speed of C, several at once, so the page draws a treemap of the folder it opened
while the sizes come in (start/poll) and asks for the next level only when a block is opened. Nothing is changed here. The path must be a real folder
inside the home folder (no links), the same boundary as showing a file in Finder or moving it to the Trash.
"""
from i18n import T
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
MAX_ITEMS = 60          # blocks per level: the biggest ones, the rest is one "other" block
CACHE_SECONDS = 120
WORKERS = 8


def safe_folder(path):
    """The real path of a folder inside the home folder (or the home folder itself), or None."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return None
    if os.path.islink(path) or not os.path.isdir(path):
        return None
    real = os.path.realpath(path)
    home = os.path.realpath(HOME)
    if real != os.path.abspath(path) or not (real == home or real.startswith(home + os.sep)):
        return None
    return real


def parse_du(text):
    """`du -sk path` output → kilobytes of the first line, or None."""
    kb, _, _path = text.partition("\t")
    return int(kb) if kb.strip().isdigit() else None


def _du_size(path, timeout=240):
    try:
        done = subprocess.run(["/usr/bin/du", "-sk", "-x", path], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_du(done.stdout)   # an unreadable subfolder only adds stderr lines; its size is partial


def _entries(real):
    """([(name, path)] of subfolders, [{name, path, sizeMb, dir: False}] of files). Links are skipped."""
    dirs, files = [], []
    try:
        entries = list(os.scandir(real))
    except OSError:
        return dirs, files
    for e in entries:
        try:
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                dirs.append((e.name, e.path))
            elif e.is_file(follow_symlinks=False):
                mb = round(e.stat(follow_symlinks=False).st_blocks * 512 / 1048576, 1)
                files.append({"name": e.name, "path": e.path, "sizeMb": mb, "dir": False})
        except OSError:
            continue
    return dirs, files


def build(real, dir_sizes, files, pending=0):
    """The page's answer for one level: the biggest MAX_ITEMS blocks and one "other" block for the rest."""
    items = list(files)
    for name, path in dir_sizes:
        items.append({"name": name, "path": path, "sizeMb": dir_sizes[(name, path)], "dir": True})
    items = [i for i in items if i["sizeMb"] and i["sizeMb"] > 0]
    items.sort(key=lambda i: -i["sizeMb"])
    rest = items[MAX_ITEMS:]
    items = items[:MAX_ITEMS]
    if rest:
        items.append({"name": T("Інше"), "path": None, "sizeMb": round(sum(i["sizeMb"] for i in rest), 1), "dir": False, "count": len(rest)})
    home = os.path.realpath(HOME)
    return {"ok": True, "path": real, "parent": None if real == home else os.path.dirname(real),
            "totalMb": round(sum(i["sizeMb"] for i in items), 1), "items": items, "pending": pending}


_JOBS = {}
_LOCK = threading.Lock()


def start(path, sizer=None, now=None):
    """Starts counting one folder in the background, or reuses a fresh result. Returns the first poll() answer."""
    real = safe_folder(path)
    if real is None:
        return {"ok": False, "error": T("Недозволений шлях")}
    now = time.time() if now is None else now
    with _LOCK:
        job = _JOBS.get(real)
        if job and (not job["done"] or now - job["at"] < CACHE_SECONDS):
            return poll(real, _locked=True)
        dirs, files = _entries(real)
        job = {"at": now, "done": False, "dirs": dirs, "files": files, "sizes": {}, "total": len(dirs)}
        _JOBS[real] = job
    threading.Thread(target=_work, args=(real, job, sizer or _du_size), daemon=True).start()
    return poll(real)


def _work(real, job, sizer):
    def one(entry):
        kb = sizer(entry[1])
        with _LOCK:
            job["sizes"][entry] = round((kb or 0) / 1024, 1)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(one, job["dirs"]))
    with _LOCK:
        job["done"] = True
        job["at"] = time.time()


def poll(path, _locked=False):
    """{done, progress, result} for a folder started with start(). The result holds what has been counted so far."""
    real = os.path.realpath(path) if isinstance(path, str) else None
    lock = _NullLock() if _locked else _LOCK
    with lock:
        job = _JOBS.get(real)
        if job is None:
            return {"ok": False, "error": T("Не вдалося порахувати")}
        counted = len(job["sizes"])
        result = build(real, dict(job["sizes"]), job["files"], pending=job["total"] - counted)
        return {"ok": True, "done": job["done"], "counted": counted, "total": job["total"], "result": result}


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
