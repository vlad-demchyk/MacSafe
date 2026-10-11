"""Leftovers of removed apps: folders named by bundle id in ~/Library whose app no longer exists.

Search only. Candidates are shown with size and date; moving to the Trash (restorable) is done by trash.py
after confirmation. Does not touch com.apple.*, apps from any Trash, or folders changed less than 30 days ago.
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
BUNDLE_ID = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9_-]+){2,}$")   # com.vendor.app: at least three parts
MIN_AGE_DAYS = 30
MAX_ITEMS = 40


def installed_ids(app_dirs=APP_DIRS):
    """The set of bundle ids of all found apps (up to two levels deep in the app folders)."""
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
                except Exception:   # a broken Info.plist (ExpatError and similar) must not break the scan
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
    """Candidates: [{path, id, sizeMb, days}] in descending size order."""
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
