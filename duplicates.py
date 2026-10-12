"""Duplicate files: the same content in several places, found without changing anything.

The search runs in the background (start/poll/cancel). Files are grouped by size, then by a hash of the first 64 KB,
then by a hash of the whole content, so only files that could really be equal are read in full. Hard links to one
file are the same file, not a copy, and are not reported. The page chooses among the folders found directly in the home folder (never a free path). Isolated zones (see
zones.py) are left out, and a group that would free less than MIN_WASTED is not shown: it is noise.
Removal moves the chosen copies to the Trash, and only if at least one copy of every group stays.
"""
from i18n import T
import hashlib
import os
import threading
import time

import zones

HOME = os.path.expanduser("~")
STANDARD = zones.STANDARD   # ticked by default on the page
MIN_WASTED = 5 * 1048576    # a group that frees less than this is not worth showing
HEAD = 64 * 1024
CHUNK = 1024 * 1024
MAX_GROUPS = 100
MAX_PATHS = 200
CACHE_SECONDS = 600
_JOB = {"state": None}
_LOCK = threading.Lock()


def _hash(path, limit=None):
    h = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            if limit is not None:
                h.update(fh.read(limit))
            else:
                while True:
                    block = fh.read(CHUNK)
                    if not block:
                        break
                    h.update(block)
    except OSError:
        return None
    return h.digest()


def walk(roots, min_bytes, cancelled=lambda: False, skipped=None):
    """{size: [(path, mtime, dev, ino)]} for regular files of at least min_bytes.

    Links, hidden files and files other programs depend on are skipped, and so is every isolated zone (projects,
    vaults, sync folders, bundles, dependencies, other tools' stores): duplicates there are normal. The zones
    that were left out are collected into `skipped` as {path, reason}.
    """
    by_size = {}
    stack = list(roots)
    while stack and not cancelled():
        folder = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    # the chosen folder itself may hold a stray marker; only what is inside it is judged
                    reason = zones.isolated_dir(e.name, e.path)
                    if reason:
                        if skipped is not None and len(skipped) < 30 and not e.name.startswith("."):
                            skipped.append({"path": e.path, "reason": reason})
                        continue
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_size >= min_bytes and not zones.isolated_file(e.name):
                        by_size.setdefault(st.st_size, []).append((e.path, st.st_mtime, st.st_dev, st.st_ino))
            except OSError:
                continue
    return by_size


def find_groups(by_size, progress=None, cancelled=lambda: False):
    """Groups of files with identical content: [{size, files: [{path, mtime}]}], the most wasted space first."""
    candidates = {s: f for s, f in by_size.items() if len(f) > 1}
    total, done, groups = sum(len(f) for f in candidates.values()), 0, []
    for size, files in candidates.items():
        if cancelled():
            break
        unique = {}
        for f in sorted(files, key=lambda f: (len(f[0]), f[0])):   # hard links to one file count once, the shortest name stands for it
            unique.setdefault((f[2], f[3]), f)
        files = list(unique.values())
        if len(files) < 2:
            done += len(candidates[size])
            continue
        by_head = {}
        for f in files:
            by_head.setdefault(_hash(f[0], HEAD), []).append(f)
        for same_head in by_head.values():
            if len(same_head) < 2:
                continue
            by_full = {}
            for f in same_head:
                if cancelled():
                    break
                digest = _hash(f[0])
                if digest is not None:
                    by_full.setdefault(digest, []).append(f)
            for same in by_full.values():
                if len(same) > 1:
                    same.sort(key=lambda f: (f[1], len(f[0]), f[0]))   # the oldest first: the default one to keep
                    groups.append({"size": size, "files": [{"path": f[0], "mtime": int(f[1])} for f in same]})
        done += len(candidates[size])
        if progress:
            progress(done, total)
    groups = [g for g in groups if g["size"] * (len(g["files"]) - 1) >= MIN_WASTED]   # small gains are noise
    groups.sort(key=lambda g: -g["size"] * (len(g["files"]) - 1))
    return groups[:MAX_GROUPS]


def _summary(groups):
    wasted = sum(g["size"] * (len(g["files"]) - 1) for g in groups)
    return {"groups": [dict(g, wastedMb=round(g["size"] * (len(g["files"]) - 1) / 1048576, 1), sizeMb=round(g["size"] / 1048576, 1))
                       for g in groups], "wastedMb": round(wasted / 1048576, 1)}


def available_roots(home=None):
    """Folders the page may offer: the visible folders directly in the home folder, without Library. [{id, standard}]"""
    home = home or HOME
    found = []
    try:
        entries = sorted(os.scandir(home), key=lambda e: e.name.lower())
    except OSError:
        return found
    for e in entries:
        if zones.isolated_dir(e.name, e.path, check_marker=False):   # stores, sync folders, Library and the like
            continue
        if e.is_dir(follow_symlinks=False) and not e.is_symlink():
            found.append({"id": e.name, "standard": e.name in STANDARD})
    return found


def resolve_roots(ids, home=None):
    """Real paths of the chosen folders; only names from available_roots(), never a free path."""
    home = home or HOME
    allowed = {r["id"] for r in available_roots(home)}
    return [os.path.join(home, rid) for rid in dict.fromkeys(ids if isinstance(ids, list) else [])
            if isinstance(rid, str) and rid in allowed]


def start(root_ids, min_mb=5, home=None):
    """Starts the search in the background. A search already running is not started twice."""
    roots = resolve_roots(root_ids, home)
    if not roots:
        return {"ok": False, "error": T("Нічого не вибрано")}
    min_bytes = max(1, int(float(min_mb) * 1048576)) if isinstance(min_mb, (int, float)) and min_mb > 0 else 5 * 1048576
    with _LOCK:
        job = _JOB["state"]
        if job and not job["done"]:
            return poll()
        job = {"done": False, "cancel": False, "stage": "walk", "files": 0, "counted": 0, "total": 0, "result": None, "error": None,
               "roots": roots, "skipped": []}
        _JOB["state"] = job
    threading.Thread(target=_work, args=(job, min_bytes), daemon=True).start()
    return poll()


def _work(job, min_bytes):
    cancelled = lambda: job["cancel"]
    try:
        by_size = walk(job["roots"], min_bytes, cancelled, job["skipped"])
        job["files"] = sum(len(f) for f in by_size.values())
        job["stage"] = "hash"

        def progress(done, total):
            job["counted"], job["total"] = done, total
        groups = find_groups(by_size, progress, cancelled)
        job["result"] = _summary(groups)
    except Exception as exc:   # the page shows the reason instead of waiting forever
        job["error"] = str(exc)
    job["done"] = True
    job["at"] = time.time()


def poll():
    job = _JOB["state"]
    if job is None:
        return {"ok": False, "error": T("Пошук не запускали")}
    return {"ok": True, "done": job["done"], "stage": job["stage"], "files": job["files"], "counted": job["counted"],
            "total": job["total"], "result": job["result"], "error": job["error"], "cancelled": job["cancel"],
            "skipped": job["skipped"]}


def cancel():
    job = _JOB["state"]
    if job and not job["done"]:
        job["cancel"] = True
    return poll()


def trash_copies(paths, trasher=None):
    """Moves the chosen copies to the Trash. Every path must belong to a group of the last result, the copy that
    stays must still exist, and no group may lose its last copy. Returns {ok, results, freedMb} or {ok: False, error}."""
    job = _JOB["state"]
    if not job or not job.get("result"):
        return {"ok": False, "error": T("Спершу виконайте пошук")}
    if not isinstance(paths, list) or not paths or len(paths) > MAX_PATHS:
        return {"ok": False, "error": T("Нічого не вибрано")}
    chosen = {p for p in paths if isinstance(p, str)}
    groups = job["result"]["groups"]
    index = {f["path"]: g for g in groups for f in g["files"]}
    if any(p not in index for p in chosen):
        return {"ok": False, "error": T("Не з результатів пошуку")}
    for p in sorted(chosen):   # the zones may have changed since the search (a project was cloned in)
        reason = zones.blocked_for_trash(p, HOME)
        if reason:
            return {"ok": False, "error": reason}
    for g in groups:
        paths_in = [f["path"] for f in g["files"]]
        if not any(p in chosen for p in paths_in):
            continue
        keepers = [p for p in paths_in if p not in chosen and os.path.isfile(p) and not os.path.islink(p)]
        if not keepers or any(os.path.getsize(p) != g["size"] for p in keepers):
            return {"ok": False, "error": T("У групі має лишитися хоча б одна копія")}
        keeper_hash = _hash(keepers[0])
        for p in paths_in:   # a file edited since the search is not a copy any more
            if p in chosen and (not os.path.isfile(p) or _hash(p) != keeper_hash):
                return {"ok": False, "error": T("Файл змінився після пошуку: повторіть пошук")}
    if trasher is None:
        import trash
        trasher = trash.move_to_trash
    results, freed = [], 0.0
    for p in sorted(chosen):
        ok, error = trasher(p)
        results.append({"path": p, "ok": ok, "error": error})
        if ok:
            freed += index[p]["size"] / 1048576
    done = {r["path"] for r in results if r["ok"]}
    for g in groups:   # the shown result stays true: removed copies leave their groups
        g["files"] = [f for f in g["files"] if f["path"] not in done]
    job["result"] = _summary([g for g in groups if len(g["files"]) > 1])
    return {"ok": True, "results": results, "freedMb": round(freed, 1)}
