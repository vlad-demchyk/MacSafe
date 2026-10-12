"""Cache cleaning: only folders selected by the user from the list, after confirmation in the interface.

A cache of a running app or of a protected app (offline media, sync state, VM images) is shown but skipped.
The folder's contents are deleted, the folder itself stays. Candidates only from allowed roots:
~/.npm, ~/Library/Caches, ~/Library/Logs, ~/Library/Developer/Xcode. Symbolic links and folders outside the roots
are skipped. com.apple.* folders are never touched. Close the app whose cache you clean.
"""
from i18n import T
import os
import re
import time

HOME = os.path.expanduser("~")
CACHE_ROOT = os.path.join(HOME, "Library", "Caches")
ALLOWED_ROOTS = (
    os.path.join(HOME, ".npm"),
    CACHE_ROOT,
    os.path.join(HOME, "Library", "Developer", "Xcode"),
    os.path.join(HOME, "Library", "Logs"),
)
FIXED = {
    "npm": ("npm", os.path.join(HOME, ".npm", "_cacache"), "Кеш пакетів npm. Завантажиться знову за потреби."),
    "pip": ("pip", os.path.join(CACHE_ROOT, "pip"), "Кеш пакетів pip."),
    "brew": ("Homebrew", os.path.join(CACHE_ROOT, "Homebrew"), "Кеш завантажень Homebrew."),
    "xcode": ("Xcode DerivedData", os.path.join(HOME, "Library", "Developer", "Xcode", "DerivedData"),
              "Результати збірок. Xcode створить їх знову."),
    "logs": ("Логи", os.path.join(HOME, "Library", "Logs"), "Логи застосунків. Старіші за 30 днів."),
}
# Categories to choose from in detailed mode. All are safe: these are caches and logs that the system creates again.
CATEGORIES = {
    "dev": "Інструменти розробника",
    "logs": "Логи",
    "apps": "Кеші застосунків",
}
FIXED_CATEGORY = {"npm": "dev", "pip": "dev", "brew": "dev", "xcode": "dev", "logs": "logs"}
DAYS_CHOICES = (7, 30, 90)   # allowed file age thresholds
CACHE_PREFIX = "cache:"
SAFE_NAME = re.compile(r"^[^/\\\0]+$")
MAX_IDS = 60
OLD_DAYS = 30   # quick clean touches only files not changed for this many days


# Caches that hold data an app cannot quickly rebuild (offline media, sync state, VM images, messages). Matched by
# a part of the folder name, case-insensitive. Such folders are shown but never selectable.
PROTECTED_WORDS = (
    "spotify", "dropbox", "googledrive", "google drive", "drivefs", "onedrive", "pcloud", "syncthing", "icloud",
    "cloudkit", "1password", "bitwarden", "keepass", "signal", "telegram", "whatsapp", "docker", "parallels",
    "vmware", "virtualbox", "utm", "mail", "photos", "music", "podcasts", "tv", "backup", "timemachine",
)
# Fixed caches that belong to a tool: the clean is skipped while the tool runs (process name or bundle id).
FIXED_BUSY = {
    "npm": {"names": {"npm"}, "bundles": set()},
    "pip": {"names": {"pip", "pip3"}, "bundles": set()},
    "brew": {"names": {"brew"}, "bundles": set()},
    "xcode": {"names": {"xcode", "xcodebuild"}, "bundles": {"com.apple.dt.xcode"}},
}


def running_apps():
    """(bundle ids, names) of running apps and processes, lower-case. Empty sets when they cannot be read."""
    bundles, names = set(), set()
    try:
        from AppKit import NSWorkspace
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.bundleIdentifier():
                bundles.add(str(app.bundleIdentifier()).lower())
            if app.localizedName():
                names.add(str(app.localizedName()).lower())
    except Exception:
        pass
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            if proc.info.get("name"):
                names.add(proc.info["name"].lower())
    except Exception:
        pass
    return bundles, names


def blocked_reason(cid, running=None):
    """Why this cache must not be cleaned right now: protected data or a running app. None when it is safe."""
    bundles, names = running if running is not None else running_apps()
    if cid in FIXED_BUSY:
        spec = FIXED_BUSY[cid]
        if (spec["names"] & names) or (spec["bundles"] & bundles):
            return T("Запущено. Закрийте і повторіть.")
        return None
    if not isinstance(cid, str) or not cid.startswith(CACHE_PREFIX):
        return None
    low = cid[len(CACHE_PREFIX):].lower()
    if any(word in low for word in PROTECTED_WORDS):
        return T("Захищено: тут можуть бути дані, які важко відновити.")
    for bid in bundles:
        if low == bid or bid.startswith(low + ".") or low.startswith(bid + "."):
            return T("Застосунок запущено. Закрийте його і повторіть.")
    tail = low.rsplit(".", 1)[-1]   # com.openai.codex -> codex
    for name in names:
        squeezed = name.replace(" ", "")
        if len(low) >= 4 and (low == name or low == squeezed or name.startswith(low + " ")):   # "google" for "google chrome"
            return T("Застосунок запущено. Закрийте його і повторіть.")
        if "." in low and len(tail) >= 4 and tail in (name, squeezed):
            return T("Застосунок запущено. Закрийте його і повторіть.")
    return None


def category_of(cid):
    """Category of a cache id: dev, logs or apps. None if the id is unknown."""
    if cid in FIXED:
        return FIXED_CATEGORY[cid]
    return "apps" if isinstance(cid, str) and cid.startswith(CACHE_PREFIX) else None


def memory_snapshot():
    """Memory snapshot in MB: free, available, inactive, compressed, swap. None if psutil is unavailable.

    Deleting cache files barely changes memory: the snapshot shows what actually changed, without promises.
    """
    try:
        import psutil
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()
    except Exception:
        return None
    mb = lambda v: round(v / (1024 * 1024))
    return {"freeMb": mb(vm.free), "availableMb": mb(vm.available),
            "inactiveMb": mb(getattr(vm, "inactive", 0)), "wiredMb": mb(getattr(vm, "wired", 0)),
            "usedPercent": round(vm.percent, 1), "swapMb": mb(sw.used)}


def memory_delta(before, after):
    """Difference of snapshots (after minus before) for display. None if one of them is missing."""
    if not before or not after:
        return None
    return {k: after[k] - before[k] for k in ("freeMb", "availableMb", "inactiveMb", "swapMb")}


def _valid_days(days):
    return days if days in DAYS_CHOICES else OLD_DAYS


def _target(cid):
    """Path for an id, or None if the id is unknown or the path is outside the allowed roots."""
    if not isinstance(cid, str):
        return None
    if cid in FIXED:
        path = FIXED[cid][1]
    elif cid.startswith(CACHE_PREFIX):
        name = cid[len(CACHE_PREFIX):]
        if not SAFE_NAME.match(name) or name.startswith(".") or name.startswith("com.apple."):
            return None
        path = os.path.join(CACHE_ROOT, name)
    else:
        return None
    if os.path.islink(path) or not os.path.isdir(path):
        return None
    real = os.path.realpath(path)
    if real != os.path.abspath(path):   # the path must not lead through a link
        return None
    if not any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS):
        return None
    return path


def _walk_old(root, cutoff):
    """Regular files under root with a modification date earlier than cutoff: [(path, size)]. Symbolic links are not visited."""
    found = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            if st.st_mtime < cutoff:
                                found.append((entry.path, st.st_size))
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _drop_empty_dirs(root):
    """Removes empty folders inside root from the bottom up. The root itself and symbolic links stay."""
    for current, dirs, _files in os.walk(root, topdown=False):
        if current == root:
            continue
        try:
            os.rmdir(current)   # a non-empty or symbolic folder will not be deleted here
        except OSError:
            pass


def _old_stats(path, days=OLD_DAYS):
    """(size in MB, file count) of files in path older than days days: this is exactly what cleaning deletes."""
    cutoff = time.time() - days * 86400
    found = _walk_old(path, cutoff)
    return round(sum(size for _, size in found) / (1024 * 1024), 1), len(found)


def _old_mb(path, days=OLD_DAYS):
    return _old_stats(path, days)[0]


def scan_fixed(days=OLD_DAYS):
    """Fixed safe caches (npm, pip, Homebrew, Xcode, logs) with the size of files older than days days."""
    days = _valid_days(days)
    items = []
    running = running_apps()
    for cid, (title, path, note) in FIXED.items():
        if os.path.isdir(path) and not os.path.islink(path):
            size, count = _old_stats(path, days)
            items.append({"id": cid, "title": T(title), "note": T(note), "category": FIXED_CATEGORY[cid],
                          "sizeMb": size, "files": count, "blocked": blocked_reason(cid, running)})
    return items


def clean_old(ids, days=OLD_DAYS, categories=None):
    """Deletes from the selected caches only files not changed for days days, and empty folders inside.

    Only roots from the allowlist; symbolic links are not visited or deleted.
    categories (optional): ids outside these categories are skipped. The result includes a memory snapshot before and after.
    """
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        return {"ok": False, "error": T("Не вибрано жодного кешу")}
    days = _valid_days(days)
    allowed = set(categories) & set(CATEGORIES) if isinstance(categories, (list, tuple, set)) else None
    cutoff = time.time() - days * 86400
    before = memory_snapshot()
    results = []
    freed_total = 0.0
    for cid in dict.fromkeys(ids):
        if not isinstance(cid, str):
            results.append({"id": str(cid), "error": T("Неправильний ідентифікатор")})
            continue
        if allowed is not None and category_of(cid) not in allowed:
            results.append({"id": cid, "error": T("Категорію не вибрано")})
            continue
        root = _target(cid)
        if root is None or os.path.islink(root):
            results.append({"id": cid, "error": T("Недозволена або відсутня папка")})
            continue
        reason = blocked_reason(cid)
        if reason:
            results.append({"id": cid, "error": reason, "skipped": True})
            continue
        real_root = os.path.realpath(root)
        removed = 0
        errors = []
        for path, size in _walk_old(root, cutoff):
            # Before deletion the parent folder must not leave the root (a link substitution between scanning and deleting)
            parent = os.path.realpath(os.path.dirname(path))
            if parent != real_root and not parent.startswith(real_root + os.sep):
                errors.append(T("{name}: пропущено, шлях веде за межі", name=os.path.basename(path)))
                continue
            try:
                os.remove(path)
                removed += size
            except OSError as exc:
                errors.append(f"{os.path.basename(path)}: {exc.strerror}")
        _drop_empty_dirs(root)
        freed_mb = round(removed / (1024 * 1024), 1)
        freed_total += freed_mb
        results.append({"id": cid, "freedMb": freed_mb, "errors": errors[:5], "errorCount": len(errors)})
    after = memory_snapshot()
    return {"ok": True, "freedMb": round(freed_total, 1), "results": results, "days": days,
            "memory": {"before": before, "after": after, "delta": memory_delta(before, after)}}


def scan(days=OLD_DAYS):
    """Candidates with the size of files older than days days, their categories and a memory snapshot."""
    days = _valid_days(days)
    items = scan_fixed(days)
    try:
        names = [e.name for e in os.scandir(CACHE_ROOT) if e.is_dir(follow_symlinks=False)]
    except OSError:
        names = []
    skip = {os.path.basename(p) for _, p, _ in FIXED.values()}
    app_items = []
    running = running_apps()
    for name in names:
        if name.startswith(".") or name.startswith("com.apple.") or name in skip:
            continue
        cid = CACHE_PREFIX + name
        if _target(cid) is None:
            continue
        size, count = _old_stats(os.path.join(CACHE_ROOT, name), days)
        app_items.append({"id": cid, "title": name, "note": T("Кеш застосунку. Закрийте його перед очищенням."),
                          "category": "apps", "sizeMb": size, "files": count,
                          "blocked": blocked_reason(cid, running)})
    app_items.sort(key=lambda it: -(it["sizeMb"] or 0))
    items += app_items[:40]
    totals = {key: round(sum(i["sizeMb"] or 0 for i in items if i["category"] == key), 1) for key in CATEGORIES}
    return {"items": items, "cacheRoot": CACHE_ROOT, "olderThanDays": days, "daysChoices": list(DAYS_CHOICES),
            "categories": [{"id": k, "title": T(t), "sizeMb": totals[k]} for k, t in CATEGORIES.items()],
            "memory": memory_snapshot()}


def clean(ids, days=OLD_DAYS, categories=None):
    """Cleans the selected caches and logs: only files older than days (OLD_DAYS by default)."""
    return clean_old(ids, days, categories)
