"""Залишки видалених застосунків: папки з іменем bundle id в ~/Library, для яких застосунка вже немає.

Тільки пошук. Кандидати показуються з розміром і датою, переміщення в кошик (відновлюване) робить trash.py
після підтвердження. Не чіпає com.apple.*, застосунки з будь-якого кошика і папки, змінені менш ніж 30 днів тому.
"""
import os
import plistlib
import re
import time

HOME = os.path.expanduser("~")
APP_DIRS = ("/Applications", "/Applications/Utilities", "/System/Applications", os.path.join(HOME, "Applications"))
SCAN_DIRS = (
    os.path.join(HOME, "Library", "Application Support"),
    os.path.join(HOME, "Library", "Containers"),
    os.path.join(HOME, "Library", "Saved Application State"),
)
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9_-]+){2,}$")   # com.vendor.app: не менше трьох частин
MIN_AGE_DAYS = 30
MAX_ITEMS = 40


def installed_ids(app_dirs=APP_DIRS):
    """Множина bundle id усіх знайдених застосунків (на глибині до двох рівнів у папках застосунків)."""
    ids = set()
    stack = [(d, 0) for d in app_dirs]
    while stack:
        folder, depth = stack.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if e.name.endswith(".app"):
                try:
                    with open(os.path.join(e.path, "Contents", "Info.plist"), "rb") as fh:
                        bid = plistlib.load(fh).get("CFBundleIdentifier")
                    if isinstance(bid, str):
                        ids.add(bid.lower())
                except Exception:   # битий Info.plist (ExpatError тощо) не повинен ламати скан
                    pass
            elif depth < 1 and e.is_dir(follow_symlinks=False):
                stack.append((e.path, depth + 1))
    return ids


def _size_mb(path):
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return round(total / (1024 * 1024), 1)


def scan(scan_dirs=SCAN_DIRS, app_dirs=APP_DIRS, min_age_days=MIN_AGE_DAYS):
    """Кандидати: [{path, id, sizeMb, days}] за спаданням розміру."""
    known = installed_ids(app_dirs)
    cutoff = time.time() - min_age_days * 86400
    out = []
    for folder in scan_dirs:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            name = e.name.removesuffix(".savedState") if e.name.endswith(".savedState") else e.name
            low = name.lower()
            if not BUNDLE_ID.match(name) or low.startswith("com.apple.") or e.is_symlink():
                continue
            if low in known or any(k.startswith(low) or low.startswith(k) for k in known):
                continue
            try:
                mtime = e.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            out.append({"path": e.path, "id": name, "sizeMb": _size_mb(e.path) if e.is_dir(follow_symlinks=False)
                        else round(e.stat().st_size / (1024 * 1024), 1), "days": int((time.time() - mtime) // 86400)})
    out.sort(key=lambda i: -i["sizeMb"])
    return out[:MAX_ITEMS]
