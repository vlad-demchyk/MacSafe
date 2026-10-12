"""Storage categories that appear only when something is found: games, virtual machines, developer tools.

Each category lists what takes space and where it lives, so it is clear where memory goes and where to remove it.
Scanning changes nothing. Only games can be removed from here (they go to the Trash, a running launcher blocks it);
virtual machines and developer data are shown with their size and location and removed from their own programs.

Item: {id, name, tool, kind, path, paths, sizeMb, removable, hint, blocked}
"""
from i18n import T
import glob
import os
import plistlib
import re

import apps

HOME = os.path.expanduser("~")
GAME_APP_DIRS = ("/Applications", os.path.join(HOME, "Applications"))
TITLES = {"games": "Ігри", "vms": "Віртуальні машини і контейнери", "dev": "Інструменти розробки"}
RUNNING = {"steam": ("steam_osx", "Steam Helper"), "bottle": ("wineserver", "wine64-preloader", "CrossOver", "Whisky", "wine-preloader"),
           "app": ()}
MIN_SHOW_MB = 100        # data below this is not worth a line (noise); games and apps are always shown
OVERVIEW_SECONDS = 300
STEAM_TOOLS = ("Steamworks Common", "Steam Linux Runtime", "Proton", "Steam Controller")   # not games
_OVERVIEW = {"at": 0.0, "items": []}
_LAST = {}   # category -> items of the last scan: only these paths may be shown in Finder or removed


def _acf(text, key):
    m = re.search(r'"%s"\s+"([^"]*)"' % re.escape(key), text)
    return m.group(1) if m else None


def steam_libraries(home=None):
    """The steamapps folders: Steam's own, those in libraryfolders.vdf, and any steamapps near the top of the home folder."""
    home = home or HOME
    found = []
    own = os.path.join(home, "Library", "Application Support", "Steam", "steamapps")
    candidates = [own] + glob.glob(os.path.join(home, "*", "steamapps")) + glob.glob(os.path.join(home, "*", "*", "steamapps"))
    try:
        with open(os.path.join(own, "libraryfolders.vdf"), encoding="utf-8", errors="replace") as fh:
            candidates += [os.path.join(p, "steamapps") for p in re.findall(r'"path"\s+"([^"]+)"', fh.read())]
    except OSError:
        pass
    for path in candidates:
        real = os.path.realpath(path)
        if os.path.isdir(real) and real not in found and real.startswith(os.path.realpath(home) + os.sep):
            found.append(real)
    return found


def _steam_items(home):
    items = []
    for lib in steam_libraries(home):
        for manifest in sorted(glob.glob(os.path.join(lib, "appmanifest_*.acf"))):
            try:
                with open(manifest, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            appid, name, installdir = _acf(text, "appid"), _acf(text, "name"), _acf(text, "installdir")
            if not appid or not name or not installdir or "/" in installdir or installdir.startswith("."):
                continue
            if name.startswith(STEAM_TOOLS):
                continue
            game = os.path.join(lib, "common", installdir)
            if not os.path.isdir(game):
                continue
            related = [p for p in (manifest, os.path.join(lib, "compatdata", appid), os.path.join(lib, "shadercache", appid)) if os.path.exists(p)]
            items.append({"id": f"steam:{os.path.basename(lib)}:{appid}", "name": name, "tool": "Steam", "kind": "steam", "path": game,
                          "paths": [game] + related, "sizeMb": None, "removable": True, "hint": None, "blocked": None})
    return items


def _game_app_items(app_dirs):
    items = []
    for folder in app_dirs:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".app") or e.is_symlink() or e.name == "Steam.app":
                continue
            try:
                with open(os.path.join(e.path, "Contents", "Info.plist"), "rb") as fh:
                    info = plistlib.load(fh)
            except Exception:
                continue
            bid = str(info.get("CFBundleIdentifier") or "")
            if "games" not in str(info.get("LSApplicationCategoryType") or "").lower() or bid.lower().startswith("com.apple."):
                continue
            items.append({"id": f"app:{e.path}", "name": str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or e.name[:-4]),
                          "tool": T("Застосунок"), "kind": "app", "path": e.path, "paths": [e.path], "sizeMb": None, "removable": True,
                          "hint": None, "blocked": None})
    return items


def _folder_items(home, spec, kind, removable, hint):
    """spec: [(tool name, [path patterns relative to home], 'one' = the folder itself | 'each' = every folder inside)]."""
    items = []
    for tool, patterns, mode in spec:
        for pattern in patterns:
            for path in sorted(glob.glob(os.path.join(home, pattern))):
                if os.path.islink(path) or not os.path.exists(path):
                    continue
                targets = [path]
                if mode == "each":
                    try:
                        targets = [e.path for e in os.scandir(path) if not e.name.startswith(".") and not e.is_symlink()]
                    except OSError:
                        targets = []
                for target in targets:
                    shown = T(tool)
                    name = shown if mode == "one" else f"{shown}: {os.path.basename(target)}"
                    items.append({"id": f"{kind}:{target}", "name": name, "tool": shown, "kind": kind, "path": target, "paths": [target],
                                  "sizeMb": None, "removable": removable, "hint": hint, "blocked": None})
    return items


GAME_FOLDERS = [
    ("Epic Games", ["Library/Application Support/Epic", "../Shared/Epic Games"], "one"),
    ("GOG Galaxy", ["Library/Application Support/GOG.com"], "one"),
    ("Battle.net", ["Library/Application Support/Battle.net", "Library/Application Support/Blizzard"], "one"),
    ("Heroic", ["Library/Application Support/heroic", "Games/Heroic"], "one"),
    ("OpenEmu", ["Library/Application Support/OpenEmu"], "one"),
    ("RetroArch", ["Library/Application Support/RetroArch"], "one"),
    ("Dolphin", ["Library/Application Support/Dolphin"], "one"),
    ("PCSX2", ["Library/Application Support/PCSX2"], "one"),
    ("RPCS3", ["Library/Application Support/rpcs3"], "one"),
    ("Ryujinx", ["Library/Application Support/Ryujinx"], "one"),
    ("DuckStation", ["Library/Application Support/DuckStation"], "one"),
]
BOTTLE_FOLDERS = [
    ("CrossOver", ["Library/Application Support/CrossOver/Bottles"], "each"),
    ("Whisky", ["Library/Containers/com.isaacmarovitz.Whisky/Bottles"], "each"),
    ("Wine", [".wine"], "one"),
    ("PlayCover", ["Library/Containers/io.playcover.PlayCover"], "one"),
    ("Game Porting Toolkit", ["Library/Application Support/GamePortingToolkit"], "one"),
]
VM_FOLDERS = [
    ("UTM", ["Library/Containers/com.utmapp.UTM/Data/Documents/*.utm", "Library/Containers/com.utmapp.UTM/Data/Documents"], "one"),
    ("Parallels", ["Parallels/*.pvm"], "one"),
    ("VMware Fusion", ["Virtual Machines.localized/*", "Virtual Machines/*"], "one"),
    ("VirtualBox", ["VirtualBox VMs/*"], "one"),
    ("Docker", ["Library/Containers/com.docker.docker/Data", ".docker"], "one"),
    ("Multipass", ["Library/Application Support/multipassd"], "one"),
    ("Lima", [".lima"], "one"),
    ("Віртуальні машини", ["VM/*"], "one"),
]
DEV_FOLDERS = [
    ("Симулятори Xcode", ["Library/Developer/CoreSimulator/Devices"], "one"),
    ("Кеш симуляторів", ["Library/Developer/CoreSimulator/Caches"], "one"),
    ("Архіви Xcode", ["Library/Developer/Xcode/Archives"], "one"),
    ("Підтримка пристроїв iOS", ["Library/Developer/Xcode/iOS DeviceSupport"], "one"),
    ("Android SDK", ["Library/Android/sdk"], "one"),
    ("Android AVD", [".android/avd"], "one"),
    ("Gradle", [".gradle"], "one"),
    ("Maven", [".m2/repository"], "one"),
    ("Rust (rustup і cargo)", [".rustup", ".cargo"], "one"),
]


def scan(category, home=None, app_dirs=None, sizes=True):
    """Items of one category, biggest first. sizes=False only detects (cheap), sizes=True counts them with `du`."""
    home = home or HOME
    if category == "games":
        items = (_steam_items(home) + _game_app_items(app_dirs or GAME_APP_DIRS)
                 + _folder_items(home, GAME_FOLDERS, "launcher", False, T("Видаляйте з самої програми: тут і ігри, і її дані"))
                 + _folder_items(home, BOTTLE_FOLDERS, "bottle", True, None))
    elif category == "vms":
        items = _folder_items(home, VM_FOLDERS, "vm", False, T("Видаляйте з самої програми, щоб не пошкодити образ"))
    elif category == "dev":
        items = _folder_items(home, DEV_FOLDERS, "dev", False, T("Дані інструментів розробки: очищайте їх командами самого інструмента"))
    else:
        return []
    seen, unique = set(), []
    for item in items:
        if item["path"] not in seen:
            seen.add(item["path"])
            unique.append(item)
    items = unique
    if sizes:
        measured = apps._du_mb(sorted({p for i in items for p in i["paths"]}))
        for item in items:
            item["sizeMb"] = round(sum(measured.get(p, 0.0) for p in item["paths"]), 1)
        items = [i for i in items if i["kind"] in ("steam", "app") or i["sizeMb"] >= MIN_SHOW_MB]
        items.sort(key=lambda i: -(i["sizeMb"] or 0))
    _LAST[category] = {i["id"]: i for i in items}
    return items


def overview(home=None, app_dirs=None, now=None):
    """[{id, title, count, sizeMb, tools}] for the categories where something worth showing was found.

    Sizes are counted (a few seconds), so the answer is kept for OVERVIEW_SECONDS.
    """
    import time
    now = time.time() if now is None else now
    if home is None and now - _OVERVIEW["at"] < OVERVIEW_SECONDS and _OVERVIEW["items"] is not None:
        return _OVERVIEW["items"]
    found = []
    for cid, title in TITLES.items():
        items = scan(cid, home, app_dirs, sizes=True)
        if items:
            found.append({"id": cid, "title": title, "count": len(items), "sizeMb": round(sum(i["sizeMb"] or 0 for i in items), 1),
                          "tools": sorted({i["tool"] for i in items})})
    if home is None:
        _OVERVIEW.update(at=now, items=found)
    return found


def _running(kind):
    names = RUNNING.get(kind) or ()
    if not names:
        return False
    try:
        import psutil
        return any(p.info["name"] and any(n.lower() in p.info["name"].lower() for n in names) for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def known_path(category, path):
    """True when the path belongs to an item of the last scan of the category (for showing it in Finder)."""
    return any(path in i["paths"] for i in _LAST.get(category, {}).values())


def remove(ids, home=None, app_dirs=None, trasher=None, app_remover=None):
    """Removes games chosen from a fresh scan. Steam games: the folder, the manifest, compatdata and shader cache.
    Apps: the app and its files in Library. Bottles: the whole bottle. Everything goes to the Trash."""
    if not isinstance(ids, list) or not ids or len(ids) > 20:
        return {"ok": False, "error": T("Нічого не вибрано")}
    current = {i["id"]: i for i in scan("games", home, app_dirs, sizes=True)}
    if trasher is None:
        import trash
        trasher = trash.move_to_trash
    app_remover = app_remover or (lambda path: apps.uninstall_app(path, [f["path"] for f in apps.related_files(path) if not f["weak"]]))
    results, freed = [], 0.0
    for gid in dict.fromkeys(ids):
        item = current.get(gid) if isinstance(gid, str) else None
        if item is None:
            results.append({"id": str(gid), "ok": False, "error": T("Не з результатів пошуку")})
            continue
        if not item["removable"]:
            results.append({"id": gid, "ok": False, "error": item["hint"] or T("Недозволено")})
            continue
        if _running(item["kind"]):
            results.append({"id": gid, "ok": False, "error": T("Запущена програма, що використовує це: закрийте її")})
            continue
        if item["kind"] == "app":
            r = app_remover(item["path"])
            ok, error = bool(r.get("ok")), r.get("error", "")
        else:
            ok, error = True, ""
            for path in item["paths"]:
                done, err = trasher(path)
                if not done:
                    ok, error = False, err
                    break
        results.append({"id": gid, "ok": ok, "error": error})
        if ok:
            freed += item["sizeMb"] or 0.0
    return {"ok": True, "results": results, "freedMb": round(freed, 1)}
