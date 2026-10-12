"""Programs: installed apps with everything they leave behind, and command line packages (Homebrew, npm, cargo, pipx).

Listing changes nothing. Removal is only for ids that the page just got from a fresh listing:
- an app goes to the Trash (restorable) together with the files in ~/Library that belong to its bundle id;
- a command line package is removed with the manager's own uninstall command. Every command is a fixed argument
  list (no shell); the package name is checked against the manager's own listing and a strict pattern first.
"""
from i18n import T
import glob
import json
import os
import plistlib
import re
import shutil
import subprocess
import time

HOME = os.path.expanduser("~")
LIB = os.path.join(HOME, "Library")
APP_DIRS = ("/Applications", os.path.join(HOME, "Applications"))
SELF_IDS = ("com.user.macsafe", "com.macsafe.app")
NAME_OK = re.compile(r"^[A-Za-z0-9@+._/-]{1,120}$")   # package names; "/" for npm scopes and brew taps
MAX_IDS = 20


# ---------- Applications ----------

def _du_mb(paths):
    """{path: size in MB}: one `du` for all of them, faster than walking the trees in Python."""
    sizes = {}
    paths = [p for p in paths if os.path.exists(p)]
    for start in range(0, len(paths), 40):
        chunk = paths[start:start + 40]
        try:
            out = subprocess.run(["/usr/bin/du", "-sk", *chunk], capture_output=True, text=True, timeout=120).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        for line in out.splitlines():
            kb, _, path = line.partition("\t")
            if kb.isdigit():
                sizes[path] = round(int(kb) / 1024, 1)
    return sizes


def _last_used_days(paths):
    """{path: days since the app was last opened} from Spotlight; missing when Spotlight has no date."""
    if not paths:
        return {}
    try:
        out = subprocess.run(["/usr/bin/mdls", "-name", "kMDItemLastUsedDate", "-raw", *paths],
                             capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    days = {}
    for path, value in zip(paths, out.split("\0")):
        try:
            stamp = time.mktime(time.strptime(value.strip()[:19], "%Y-%m-%d %H:%M:%S")) - time.timezone
        except ValueError:
            continue
        days[path] = max(0, int((time.time() - stamp) // 86400))
    return days


def _bundle_info(app):
    try:
        with open(os.path.join(app, "Contents", "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
    except Exception:   # a broken plist must not break the listing
        return None
    bid = info.get("CFBundleIdentifier")
    if not isinstance(bid, str) or not bid:
        return None
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or os.path.basename(app)[:-4]
    return {"bundleId": bid, "name": str(name), "version": str(info.get("CFBundleShortVersionString") or "")}


def running_paths():
    """Paths of the .app bundles that have a running process."""
    try:
        import psutil
    except ImportError:
        return set()
    found = set()
    for p in psutil.process_iter():
        try:
            exe = p.exe()
        except (psutil.Error, OSError):
            continue
        marker = exe.find(".app/")
        if marker != -1:
            found.add(exe[:marker + 4])
    return found


def installed_apps(app_dirs=APP_DIRS, with_sizes=True):
    """[{path, name, bundleId, version, sizeMb, days, running, blocked}] largest first. Apple's own apps are left out."""
    apps = []
    for folder in app_dirs:
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not e.name.endswith(".app") or e.is_symlink() or not e.is_dir(follow_symlinks=False):
                continue
            info = _bundle_info(e.path)
            if info is None or info["bundleId"].lower().startswith("com.apple."):
                continue
            apps.append(dict(info, path=e.path, sizeMb=None, days=None, running=False, blocked=None))
    if with_sizes:
        sizes = _du_mb([a["path"] for a in apps])
        used = _last_used_days([a["path"] for a in apps])
        running = running_paths()
        for a in apps:
            a["sizeMb"] = sizes.get(a["path"])
            a["days"] = used.get(a["path"])
            a["running"] = a["path"] in running
            if a["bundleId"].lower() in SELF_IDS or a["name"] == "MacSafe":
                a["blocked"] = T("Це MacSafe")
            elif a["running"]:
                a["blocked"] = T("Застосунок запущений: закрийте його")
    apps.sort(key=lambda a: -(a["sizeMb"] or 0))
    return apps


def related_files(app, lib=LIB):
    """Files in ~/Library that belong to the app: [{path, sizeMb, weak}].

    Matches by the bundle id are exact. `weak` marks matches by the app's display name only (Application Support,
    Caches, Logs): another program may use such a folder, so the page leaves them unchecked.
    """
    info = _bundle_info(app["path"] if isinstance(app, dict) else app)
    if info is None or info["bundleId"].lower().startswith("com.apple."):
        return []
    bid, name = info["bundleId"], info["name"]
    strong = [os.path.join(lib, "Application Support", bid), os.path.join(lib, "Caches", bid),
              os.path.join(lib, "Preferences", bid + ".plist"), os.path.join(lib, "Containers", bid),
              os.path.join(lib, "Saved Application State", bid + ".savedState"),
              os.path.join(lib, "HTTPStorages", bid), os.path.join(lib, "HTTPStorages", bid + ".binarycookies"),
              os.path.join(lib, "WebKit", bid), os.path.join(lib, "Logs", bid),
              os.path.join(lib, "Cookies", bid + ".binarycookies")]
    for pattern in (os.path.join(lib, "Preferences", "ByHost", glob.escape(bid) + ".*.plist"),
                    os.path.join(lib, "LaunchAgents", glob.escape(bid) + "*.plist"),
                    os.path.join(lib, "Group Containers", "*." + glob.escape(bid)),
                    os.path.join(lib, "Group Containers", "group." + glob.escape(bid))):
        strong.extend(glob.glob(pattern))
    weak = []
    if len(name) >= 4 and "/" not in name:
        weak = [os.path.join(lib, "Application Support", name), os.path.join(lib, "Caches", name),
                os.path.join(lib, "Logs", name)]
    seen, found = set(), []
    for weak_flag, group in ((False, strong), (True, weak)):
        for path in group:
            if path in seen or not os.path.lexists(path) or os.path.islink(path):
                continue
            seen.add(path)
            found.append({"path": path, "weak": weak_flag})
    sizes = _du_mb([f["path"] for f in found])
    for f in found:
        f["sizeMb"] = sizes.get(f["path"], 0.0)
    return found


def safe_app_path(path, app_dirs=APP_DIRS):
    """The real path of an .app that sits directly in an applications folder, or None."""
    if not isinstance(path, str) or not path.endswith(".app") or "\0" in path or os.path.islink(path):
        return None
    if not os.path.isdir(path):
        return None
    folder = os.path.dirname(os.path.abspath(path))
    if os.path.abspath(path) != path or folder not in [os.path.abspath(d) for d in app_dirs]:
        return None
    return path


def uninstall_app(path, related, app_dirs=APP_DIRS, lib=LIB, app_trasher=None, file_trasher=None):
    """Moves the app and the chosen related files to the Trash. Returns {ok, results, freedMb} or {ok: False, error}.

    `related` is a list of paths; only those that related_files() finds for this app are accepted. The list is read
    before the app moves, while its Info.plist is still there.
    """
    real = safe_app_path(path, app_dirs)
    app = next((a for a in installed_apps(app_dirs) if a["path"] == real), None) if real else None
    if app is None:
        return {"ok": False, "error": T("Не знайдено такого застосунку")}
    if app["blocked"]:
        return {"ok": False, "error": app["blocked"]}
    allowed = {f["path"]: f for f in related_files(real, lib)}
    if app_trasher is None or file_trasher is None:
        import trash
        app_trasher = app_trasher or trash.move_app_to_trash
        file_trasher = file_trasher or trash.move_to_trash
    ok, error = app_trasher(real)
    if not ok:
        return {"ok": False, "error": error or T("Не вдалося перемістити в кошик")}
    results = [{"path": real, "ok": True, "error": ""}]
    freed = app["sizeMb"] or 0.0
    for item in dict.fromkeys(related if isinstance(related, list) else []):
        f = allowed.get(item) if isinstance(item, str) else None
        if f is None:
            results.append({"path": str(item), "ok": False, "error": T("Не належить цьому застосунку")})
            continue
        ok2, err2 = file_trasher(item)
        results.append({"path": item, "ok": ok2, "error": err2})
        if ok2:
            freed += f["sizeMb"]
    return {"ok": True, "results": results, "freedMb": round(freed, 1)}


# ---------- Command line packages ----------

def tool_env():
    """PATH for a program started from the Dock: Homebrew, cargo, pipx and the newest nvm node are not in it."""
    extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", os.path.join(HOME, ".cargo", "bin"),
             os.path.join(HOME, ".local", "bin")]
    nvm = sorted(glob.glob(os.path.join(HOME, ".nvm", "versions", "node", "*", "bin")))
    if nvm:
        extra.append(nvm[-1])
    env = dict(os.environ)
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    env["HOMEBREW_NO_ANALYTICS"] = "1"
    return env


def _run(argv, timeout=60):
    """(returncode, stdout, stderr). No shell, no input: a command that asks for a password fails instead of hanging."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=tool_env(),
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _which(name):
    return shutil.which(name, path=tool_env()["PATH"])


def parse_brew_versions(text, kind, leaves=None):
    """`brew list --versions` lines → items. `leaf`: nothing installed depends on it, so it can go on its own."""
    items = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            name = parts[0]
            items.append({"id": f"brew:{kind}:{name}", "manager": "brew", "kind": kind, "name": name,
                          "version": parts[-1], "sizeMb": None,
                          "leaf": True if leaves is None or kind == "cask" else name in leaves})
    return items


def parse_npm(text):
    try:
        deps = (json.loads(text).get("dependencies") or {})
    except (ValueError, AttributeError):
        return []
    return [{"id": f"npm:{name}", "manager": "npm", "kind": "global", "name": name,
             "version": str((meta or {}).get("version") or ""), "sizeMb": None, "leaf": True}
            for name, meta in deps.items() if name not in ("npm", "corepack")]


def parse_cargo(text):
    items = []
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+) v([^\s:]+)", line)
        if m and not line.startswith(" "):
            items.append({"id": f"cargo:{m.group(1)}", "manager": "cargo", "kind": "binary", "name": m.group(1),
                          "version": m.group(2), "sizeMb": None, "leaf": True})
    return items


def parse_pipx(text):
    try:
        venvs = json.loads(text).get("venvs") or {}
    except (ValueError, AttributeError):
        return []
    return [{"id": f"pipx:{name}", "manager": "pipx", "kind": "app", "name": name,
             "version": str(((meta.get("metadata") or {}).get("main_package") or {}).get("package_version") or ""),
             "sizeMb": None, "leaf": True} for name, meta in venvs.items()]


def _brew_items():
    if not _which("brew"):
        return []
    _, prefix_out, _ = _run(["brew", "--prefix"], 20)
    prefix = prefix_out.strip() or "/opt/homebrew"
    _, leaves_out, _ = _run(["brew", "leaves"], 60)
    leaves = set(leaves_out.split())
    _, formulae, _ = _run(["brew", "list", "--formula", "--versions"], 60)
    _, casks, _ = _run(["brew", "list", "--cask", "--versions"], 60)
    items = parse_brew_versions(formulae, "formula", leaves) + parse_brew_versions(casks, "cask")
    sizes = _du_mb([os.path.join(prefix, "Cellar", i["name"]) for i in items if i["kind"] == "formula"]
                   + [os.path.join(prefix, "Caskroom", i["name"]) for i in items if i["kind"] == "cask"])
    for i in items:
        folder = "Cellar" if i["kind"] == "formula" else "Caskroom"
        i["sizeMb"] = sizes.get(os.path.join(prefix, folder, i["name"]))
    return items


def _npm_items():
    if not _which("npm"):
        return []
    _, root, _ = _run(["npm", "root", "-g"], 30)
    _, out, _ = _run(["npm", "ls", "-g", "--depth=0", "--json"], 60)
    items = parse_npm(out)
    base = root.strip()
    sizes = _du_mb([os.path.join(base, i["name"]) for i in items]) if base else {}
    for i in items:
        i["sizeMb"] = sizes.get(os.path.join(base, i["name"]))
    return items


def _cargo_items():
    if not _which("cargo"):
        return []
    _, out, _ = _run(["cargo", "install", "--list"], 30)
    items = parse_cargo(out)
    sizes = _du_mb([os.path.join(HOME, ".cargo", "bin", i["name"]) for i in items])
    for i in items:
        i["sizeMb"] = sizes.get(os.path.join(HOME, ".cargo", "bin", i["name"]))
    return items


def _pipx_items():
    if not _which("pipx"):
        return []
    _, out, _ = _run(["pipx", "list", "--json"], 30)
    items = parse_pipx(out)
    base = os.path.join(HOME, ".local", "pipx", "venvs")
    sizes = _du_mb([os.path.join(base, i["name"]) for i in items])
    for i in items:
        i["sizeMb"] = sizes.get(os.path.join(base, i["name"]))
    return items


MANAGERS = {"brew": ("Homebrew", _brew_items), "npm": ("npm", _npm_items), "cargo": ("cargo", _cargo_items),
            "pipx": ("pipx", _pipx_items)}


def packages():
    """{"managers": [{id, title, count}], "items": [...]} for the managers found on this Mac, largest first."""
    managers, items = [], []
    for mid, (title, lister) in MANAGERS.items():
        try:
            found = lister()
        except Exception:   # one broken manager must not hide the others
            found = []
        if found or _which(mid):
            managers.append({"id": mid, "title": title, "count": len(found)})
        items.extend(found)
    items.sort(key=lambda i: -(i["sizeMb"] or 0))
    return {"managers": managers, "items": items}


def uninstall_argv(item):
    """The fixed command that removes a listed package, or None."""
    name = item["name"]
    if not NAME_OK.match(name) or name.startswith("-"):
        return None
    manager = item["manager"]
    if manager == "brew":
        return ["brew", "uninstall", "--cask", name] if item["kind"] == "cask" else ["brew", "uninstall", name]
    if manager == "npm":
        return ["npm", "uninstall", "-g", name]
    if manager == "cargo":
        return ["cargo", "uninstall", name]
    if manager == "pipx":
        return ["pipx", "uninstall", name]
    return None


def remove_packages(ids, listing=None, runner=None):
    """Removes the packages with these ids. Only ids present in a fresh listing; a package others depend on is refused."""
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        return {"ok": False, "error": T("Нічого не вибрано")}
    current = {i["id"]: i for i in (listing if listing is not None else packages()["items"])}
    runner = runner or _run
    results, freed = [], 0.0
    for pid in dict.fromkeys(ids):
        item = current.get(pid) if isinstance(pid, str) else None
        if item is None:
            results.append({"id": str(pid), "ok": False, "error": T("Пакета немає у списку")})
            continue
        if not item["leaf"]:
            results.append({"id": pid, "ok": False, "error": T("Від нього залежать інші пакети")})
            continue
        argv = uninstall_argv(item)
        if argv is None:
            results.append({"id": pid, "ok": False, "error": T("Недозволена назва")})
            continue
        code, _out, err = runner(argv, 300)
        message = "" if code == 0 else (err.strip().splitlines() or [T("Команда не вдалась")])[-1][:200]
        results.append({"id": pid, "ok": code == 0, "error": message, "command": " ".join(argv)})
        if code == 0:
            freed += item["sizeMb"] or 0.0
    return {"ok": True, "results": results, "freedMb": round(freed, 1)}
