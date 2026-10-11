"""Пошук найбільших файлів у домашній папці. Тільки читання: нічого не видаляє і не рухає.

Обхід без символічних посилань, з обмеженням часу. Службові папки (~/Library, кошик, приховані) пропускаються.
Downloads, Documents і Desktop захищені macOS: при першому скануванні система може попросити дозвіл.
"""
import heapq
import os
import time

HOME = os.path.expanduser("~")
SKIP_TOP = {"Library", ".Trash"}      # у корені домашньої папки
TIME_LIMIT = 25.0                      # секунд на обхід
MAX_RESULTS = 30
MIN_MB_CHOICES = (50, 100, 500, 1000)


def find_big(min_mb=100, limit=MAX_RESULTS, root=None, time_limit=TIME_LIMIT):
    """Найбільші файли від min_mb МБ: {files: [{path, sizeMb, days}], truncated, scanned}.

    truncated — True, якщо обхід зупинено за часом. Файли відсортовано за розміром, найбільші першими.
    """
    min_mb = min_mb if min_mb in MIN_MB_CHOICES else 100
    limit = max(1, min(int(limit), MAX_RESULTS))
    root = root or HOME
    threshold = min_mb * 1024 * 1024
    deadline = time.monotonic() + time_limit
    heap = []   # (розмір, шлях, mtime): мінімальна купа на limit елементів
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
    """Шлях, який можна показати у Finder: існуючий файл усередині домашньої папки без посилань. Інакше None."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return None
    if os.path.islink(path) or not os.path.exists(path):
        return None
    real = os.path.realpath(path)
    home = os.path.realpath(HOME)
    if real != os.path.abspath(path) or not real.startswith(home + os.sep):
        return None
    return real
