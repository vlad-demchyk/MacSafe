"""Очищення кешів: лише вибрані користувачем папки зі списку, після підтвердження в інтерфейсі.

Видаляється вміст папки, сама папка лишається. Кандидати тільки з дозволених коренів:
~/.npm, ~/Library/Caches, ~/Library/Logs, ~/Library/Developer/Xcode. Символічні посилання і папки поза коренями
пропускаються. Папки com.apple.* не чіпаємо взагалі. Закрийте застосунок, чий кеш очищуєте.
"""
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
# Категорії для вибору в детальному режимі. Усі безпечні: це кеші й логи, які система створить знову.
CATEGORIES = {
    "dev": "Інструменти розробника",
    "logs": "Логи",
    "apps": "Кеші застосунків",
}
FIXED_CATEGORY = {"npm": "dev", "pip": "dev", "brew": "dev", "xcode": "dev", "logs": "logs"}
DAYS_CHOICES = (7, 30, 90)   # допустимі пороги віку файлів
CACHE_PREFIX = "cache:"
SAFE_NAME = re.compile(r"^[^/\\\0]+$")
MAX_IDS = 60
OLD_DAYS = 30   # швидке очищення чіпає лише файли, яких не змінювали стільки днів


def category_of(cid):
    """Категорія id кешу: dev, logs або apps. None, якщо id невідомий."""
    if cid in FIXED:
        return FIXED_CATEGORY[cid]
    return "apps" if isinstance(cid, str) and cid.startswith(CACHE_PREFIX) else None


def memory_snapshot():
    """Знімок памʼяті в МБ: вільна, доступна, неактивна, стиснута, swap. None, якщо psutil недоступний.

    Видалення файлів кешу майже не змінює памʼять: знімок показує, що змінилось насправді, без обіцянок.
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
    """Різниця знімків (після мінус до) для відображення. None, якщо одного з них немає."""
    if not before or not after:
        return None
    return {k: after[k] - before[k] for k in ("freeMb", "availableMb", "inactiveMb", "swapMb")}


def _valid_days(days):
    return days if days in DAYS_CHOICES else OLD_DAYS


def _target(cid):
    """Шлях для id або None, якщо id невідомий чи шлях поза дозволеними коренями."""
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
    if real != os.path.abspath(path):   # шлях не повинен вести через посилання
        return None
    if not any(real == root or real.startswith(root + os.sep) for root in ALLOWED_ROOTS):
        return None
    return path


def _walk_old(root, cutoff):
    """Звичайні файли під root з датою зміни раніше cutoff: [(шлях, розмір)]. Символічні посилання не відвідуються."""
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
    """Прибирає порожні папки всередині root знизу вгору. Сам root і символічні посилання лишаються."""
    for current, dirs, _files in os.walk(root, topdown=False):
        if current == root:
            continue
        try:
            os.rmdir(current)   # непорожня або символічна папка тут не видалиться
        except OSError:
            pass


def _old_mb(path, days=OLD_DAYS):
    """Розмір файлів у path, старших за days днів, у МБ: саме це й видаляє очищення."""
    cutoff = time.time() - days * 86400
    return round(sum(size for _, size in _walk_old(path, cutoff)) / (1024 * 1024), 1)


def scan_fixed(days=OLD_DAYS):
    """Фіксовані безпечні кеші (npm, pip, Homebrew, Xcode, логи) з розміром файлів старших за days днів."""
    days = _valid_days(days)
    items = []
    for cid, (title, path, note) in FIXED.items():
        if os.path.isdir(path) and not os.path.islink(path):
            items.append({"id": cid, "title": title, "note": note, "category": FIXED_CATEGORY[cid],
                          "sizeMb": _old_mb(path, days)})
    return items


def clean_old(ids, days=OLD_DAYS, categories=None):
    """Видаляє з вибраних кешів лише файли, яких не змінювали days днів, і порожні папки всередині.

    Тільки корені з білого списку; символічні посилання не відвідуються і не видаляються.
    categories (необов'язково): id поза цими категоріями пропускаються. У результаті є знімок памʼяті до і після.
    """
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        return {"ok": False, "error": "Не вибрано жодного кешу"}
    days = _valid_days(days)
    allowed = set(categories) & set(CATEGORIES) if isinstance(categories, (list, tuple, set)) else None
    cutoff = time.time() - days * 86400
    before = memory_snapshot()
    results = []
    freed_total = 0.0
    for cid in dict.fromkeys(ids):
        if not isinstance(cid, str):
            results.append({"id": str(cid), "error": "Неправильний ідентифікатор"})
            continue
        if allowed is not None and category_of(cid) not in allowed:
            results.append({"id": cid, "error": "Категорію не вибрано"})
            continue
        root = _target(cid)
        if root is None or os.path.islink(root):
            results.append({"id": cid, "error": "Недозволена або відсутня папка"})
            continue
        real_root = os.path.realpath(root)
        removed = 0
        errors = []
        for path, size in _walk_old(root, cutoff):
            # Перед видаленням батьківська папка не має виходити за корінь (підміна на посилання між скануванням і видаленням)
            parent = os.path.realpath(os.path.dirname(path))
            if parent != real_root and not parent.startswith(real_root + os.sep):
                errors.append(f"{os.path.basename(path)}: пропущено, шлях веде за межі")
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
    """Кандидати з розміром файлів старших за days днів, їхні категорії та знімок памʼяті."""
    days = _valid_days(days)
    items = scan_fixed(days)
    try:
        names = [e.name for e in os.scandir(CACHE_ROOT) if e.is_dir(follow_symlinks=False)]
    except OSError:
        names = []
    skip = {os.path.basename(p) for _, p, _ in FIXED.values()}
    app_items = []
    for name in names:
        if name.startswith(".") or name.startswith("com.apple.") or name in skip:
            continue
        cid = CACHE_PREFIX + name
        if _target(cid) is None:
            continue
        app_items.append({"id": cid, "title": name, "note": "Кеш застосунку. Закрийте його перед очищенням.",
                          "category": "apps", "sizeMb": _old_mb(os.path.join(CACHE_ROOT, name), days)})
    app_items.sort(key=lambda it: -(it["sizeMb"] or 0))
    items += app_items[:40]
    totals = {key: round(sum(i["sizeMb"] or 0 for i in items if i["category"] == key), 1) for key in CATEGORIES}
    return {"items": items, "cacheRoot": CACHE_ROOT, "olderThanDays": days, "daysChoices": list(DAYS_CHOICES),
            "categories": [{"id": k, "title": t, "sizeMb": totals[k]} for k, t in CATEGORIES.items()],
            "memory": memory_snapshot()}


def clean(ids, days=OLD_DAYS, categories=None):
    """Очищує вибрані кеші й логи: лише файли старші за days (за замовчуванням OLD_DAYS)."""
    return clean_old(ids, days, categories)
