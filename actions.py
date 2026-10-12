"""Actions that the window runs with a button instead of copying a command to Terminal.

The page sends only an action id. Every command here is a fixed argument list (no shell, nothing from the page ends
up in a command). Levels: read (changes nothing), mild (restarts a part of the system, nothing is lost), destructive
(deletes data: always needs confirmation). One action runs at a time in a background thread; the page polls status().
"""
from i18n import T
import os
import shutil
import subprocess
import threading
import time

OUTPUT_LIMIT = 20_000
EXTRA_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
LEVELS = ("read", "mild", "destructive")


def _a(aid, level, display, argv=None, timeout=60, max_lines=None, fn=None, group="check"):
    return {"id": aid, "level": level, "display": display, "argv": argv, "timeout": timeout, "maxLines": max_lines,
            "fn": fn, "group": group}


def _xcode_derived():
    """DerivedData through the same guarded cleaning as the cleaning page: it skips files that are not old enough and a running Xcode."""
    import cleanup
    reason = cleanup.blocked_reason("xcode")
    if reason:
        return False, reason
    result = cleanup.clean_old(["xcode"])
    item = (result.get("results") or [{}])[0]
    if "error" in item:
        return False, item["error"]
    return True, T("Звільнено {mb} МБ (файли старші за {days} дн.)", mb=result.get("freedMb", 0), days=result.get("days"))


def _fix_launchagent_perms():
    """Own LaunchAgents whose plist mentions a secret and is readable by others: mode 600. Nothing else is touched."""
    import glob
    import re
    folder = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    keyword = re.compile(rb"TOKEN|SECRET|PASSWORD", re.I)
    fixed = []
    for path in sorted(glob.glob(os.path.join(folder, "*.plist"))):
        try:
            st = os.lstat(path)
            if os.path.islink(path) or st.st_uid != os.getuid() or (st.st_mode & 0o077) == 0:
                continue
            with open(path, "rb") as fh:
                if not keyword.search(fh.read(1_000_000)):
                    continue
            os.chmod(path, 0o600)
            fixed.append(os.path.basename(path))
        except OSError:
            continue
    if not fixed:
        return True, T("Нічого виправляти: файлів із секретом і широкими правами немає")
    return True, T("Права 600 встановлено: {n}", n=len(fixed)) + "\n" + "\n".join(fixed)


def _open_pane(url):
    return ["/usr/bin/open", url]


ACTIONS = [
    _a("sleep_blockers", "read", "pmset -g assertions", ["/usr/bin/pmset", "-g", "assertions"]),
    _a("thermal", "read", "pmset -g therm", ["/usr/bin/pmset", "-g", "therm"]),
    _a("battery", "read", "pmset -g batt", ["/usr/bin/pmset", "-g", "batt"]),
    _a("disk_free", "read", "df -h /", ["/bin/df", "-h", "/"]),
    _a("tm_destination", "read", "tmutil destinationinfo", ["/usr/bin/tmutil", "destinationinfo"]),
    _a("mdm", "read", "profiles status -type enrollment", ["/usr/bin/profiles", "status", "-type", "enrollment"]),
    _a("sip", "read", "csrutil status", ["/usr/bin/csrutil", "status"], group="security"),
    _a("filevault", "read", "fdesetup status", ["/usr/bin/fdesetup", "status"], group="security"),
    _a("gatekeeper", "read", "spctl --status", ["/usr/sbin/spctl", "--status"], group="security"),
    _a("firewall", "read", "socketfilterfw --getglobalstate",
       ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], group="security"),
    _a("proxy", "read", "scutil --proxy", ["/usr/sbin/scutil", "--proxy"], group="security"),
    _a("ports", "read", "lsof -nP -iTCP -sTCP:LISTEN", ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], group="security"),
    _a("top_cpu", "read", "ps -Ao pid,%cpu,rss,comm -r | head -20", ["/bin/ps", "-Ao", "pid,%cpu,rss,comm", "-r"], max_lines=20),
    _a("updates", "read", "softwareupdate -l", ["/usr/sbin/softwareupdate", "-l"], timeout=180),
    _a("restart_finder", "mild", "killall Finder", ["/usr/bin/killall", "Finder"], group="fix"),
    _a("restart_dock", "mild", "killall Dock", ["/usr/bin/killall", "Dock"], group="fix"),
    _a("restart_panel", "mild", "killall com.apple.appkit.xpc.openAndSavePanelService",
       ["/usr/bin/killall", "com.apple.appkit.xpc.openAndSavePanelService"], group="fix"),
    _a("backup_now", "mild", "tmutil startbackup", ["/usr/bin/tmutil", "startbackup"], group="fix"),
    _a("npm_cache", "destructive", "npm cache clean --force", ["npm", "cache", "clean", "--force"], timeout=180, group="clean"),
    _a("brew_cleanup", "destructive", "brew cleanup -s", ["brew", "cleanup", "-s"], timeout=300, group="clean"),
    _a("xcode_derived", "destructive", "Xcode DerivedData", fn=_xcode_derived, group="clean"),
    _a("fix_launchagent_perms", "mild", "chmod 600 LaunchAgents with a secret", fn=_fix_launchagent_perms, group="issue"),
    _a("open_softwareupdate", "read", "open System Settings: Software Update",
       _open_pane("x-apple.systempreferences:com.apple.Software-Update-Settings.extension"), group="issue"),
    _a("open_timemachine", "read", "open System Settings: Time Machine",
       _open_pane("x-apple.systempreferences:com.apple.Time-Machine-Settings.extension"), group="issue"),
    _a("open_firewall", "read", "open System Settings: Network",
       _open_pane("x-apple.systempreferences:com.apple.Network-Settings.extension"), group="issue"),
    _a("open_privacy", "read", "open System Settings: Privacy and Security",
       _open_pane("x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension"), group="issue"),
    _a("empty_trash", "destructive", 'osascript -e \'tell application "Finder" to empty the trash\'',
       ["/usr/bin/osascript", "-e", 'tell application "Finder" to empty the trash'], group="clean"),
]
BY_ID = {a["id"]: a for a in ACTIONS}

_lock = threading.Lock()
_state = {"id": None, "state": "idle", "output": "", "code": None, "startedAt": None, "finishedAt": None}


def _resolve(argv):
    """argv with the program found on a PATH that also has Homebrew; None when the tool is not installed."""
    program = argv[0]
    if os.path.isabs(program):
        return argv if os.path.exists(program) else None
    found = shutil.which(program, path=EXTRA_PATH)
    return [found] + argv[1:] if found else None


def listing():
    """What the page may show: id, level, group and whether the tool exists here."""
    out = []
    for a in ACTIONS:
        available = a["fn"] is not None or _resolve(a["argv"]) is not None
        out.append({"id": a["id"], "level": a["level"], "group": a["group"], "display": a["display"], "available": available})
    return out


def status():
    with _lock:
        return dict(_state)


def _tail(text, max_lines):
    lines = text.strip().splitlines()
    if max_lines:
        lines = lines[:max_lines]
    return "\n".join(lines)[-OUTPUT_LIMIT:]


def _execute(action):
    if action["fn"] is not None:
        try:
            ok, text = action["fn"]()
        except Exception as exc:   # an action must report its error, not break the thread silently
            ok, text = False, str(exc)
        return (0 if ok else 1), text
    argv = _resolve(action["argv"])
    if argv is None:
        return 127, T("Команду не знайдено на цьому Mac")
    env = dict(os.environ, PATH=EXTRA_PATH, LC_ALL="C")
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=action["timeout"], env=env,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, T("Перевищено час виконання ({s} с)", s=action["timeout"])
    except OSError as exc:
        return 126, str(exc)
    return r.returncode, _tail((r.stdout or "") + (("\n" + r.stderr) if r.stderr else ""), action["maxLines"])


def start(action_id, confirm):
    """Starts an action in the background. Only ids from ACTIONS; mild and destructive need confirm=True."""
    action = BY_ID.get(action_id) if isinstance(action_id, str) else None
    if action is None:
        return {"ok": False, "error": T("Невідома дія")}
    if action["level"] != "read" and confirm is not True:
        return {"ok": False, "error": T("Потрібне підтвердження")}
    with _lock:
        if _state["state"] == "running":
            return {"ok": False, "error": T("Інша дія ще виконується")}
        _state.update(id=action_id, state="running", output="", code=None, startedAt=time.time(), finishedAt=None)

    def work():
        code, text = _execute(action)
        with _lock:
            _state.update(state="done" if code == 0 else "error", output=text, code=code, finishedAt=time.time())

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}
