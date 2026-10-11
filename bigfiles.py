"""Search for the largest files in the home folder. Read-only: deletes and moves nothing.

A walk without symbolic links, with a time limit. Service folders (~/Library, the Trash, hidden) are skipped.
Downloads, Documents and Desktop are protected by macOS: on the first scan the system may ask for permission.
"""
import heapq
import os
import time

HOME = os.path.expanduser("~")
SKIP_TOP = {"Library", ".Trash"}      # in the root of the home folder
TIME_LIMIT = 25.0                      # seconds per walk
MAX_RESULTS = 30
MIN_MB_CHOICES = (50, 100, 500, 1000)


def find_big(min_mb=100, limit=MAX_RESULTS, root=None, time_limit=TIME_LIMIT):
    """The largest files from min_mb MB: {files: [{path, sizeMb, days}], truncated, scanned}.

    truncated — True if the walk was stopped by time. Files are sorted by size, largest first.
    """
    min_mb = min_mb if min_mb in MIN_MB_CHOICES else 100
    limit = max(1, min(int(limit), MAX_RESULTS))
    root = root or HOME
    threshold = min_mb * 1024 * 1024
    deadline = time.monotonic() + time_limit
    heap = []   # (size, path, mtime): a min-heap of limit items
    scanned = 0
    truncated = False
    stack = [root]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        name = entry.name
                        if entry.is_dir(follow_symlinks=False):
                            if name.startswith(".") or (current == root and name in SKIP_TOP):
                                continue
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            scanned += 1
                            st = entry.stat(follow_symlinks=False)
                            if st.st_size >= threshold:
                                item = (st.st_size, entry.path, st.st_mtime)
                                if len(heap) < limit:
                                    heapq.heappush(heap, item)
                                elif item > heap[0]:
                                    heapq.heapreplace(heap, item)
                    except OSError:
                        continue
        except OSError:
            continue
    now = time.time()
    files = [{"path": path, "sizeMb": round(size / (1024 * 1024), 1), "days": max(0, int((now - mtime) // 86400))}
             for size, path, mtime in sorted(heap, reverse=True)]
    return {"files": files, "truncated": truncated, "scanned": scanned, "minMb": min_mb, "root": root}


def safe_path(path):
    """A path that can be shown in Finder: an existing file inside the home folder with no links. Otherwise None."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return None
    if os.path.islink(path) or not os.path.exists(path):
        return None
    real = os.path.realpath(path)
    home = os.path.realpath(HOME)
    if real != os.path.abspath(path) or not real.startswith(home + os.sep):
        return None
    return real
