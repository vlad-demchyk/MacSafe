"""MacSafe: data, reports, cleaning and checks without an interface.

Shared code for the menu bar agent (agent.py) and the report window (app.py). There is no AppKit or WebKit import here,
so the agent runs without a heavy window. All paths and data formats are here, in one place.
"""
from i18n import T
import json
import os
import shutil
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque

import autostart
import bigfiles
import cleanup
import diskmap
import duplicates
import actions
import apps
import leftovers
import llm
import storage
import trash
import zones
from diff import security_changes

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(HERE, "app.py")      # the report window (started on demand)
AGENT_PY = os.path.join(HERE, "agent.py")  # the menu bar and the background: what always runs
# App data: ~/Library/Application Support/MacSafe. The old “report” folder on the Desktop no longer exists.
DATA_DIR = os.path.expanduser("~/Library/Application Support/MacSafe")
REPORT_DIR = DATA_DIR                                      # the generator's working folder
GENERATOR = os.path.join(HERE, "core", "generate-report.sh")
DATA_REPORTS = DATA_DIR                                    # reports, snapshots, history
MARKER = os.path.join(DATA_DIR, "дані", ".first-run-done")
GEN_ENV = dict(os.environ, AUDIT_DATA_DIR=DATA_DIR, MACSAFE_PYTHON=sys.executable, MACSAFE_HELPERS=HERE)
DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")   # one report per day: the ID is the date
DASHBOARD = os.path.join(DATA_DIR, "dashboard.html")
LOCK_DIR = os.path.join(DATA_DIR, "дані", ".lock")
# How to launch itself from login items: inside .app it is its launcher, otherwise the system Python with app.py
LAUNCH_ARGS = [os.environ["MACSAFE_LAUNCHER"]] if os.environ.get("MACSAFE_LAUNCHER") else [autostart.PYTHON, AGENT_PY]
APP_LOCK = os.path.join(DATA_REPORTS, "дані", ".app.lock")          # one instance of the app
SHOW_REQUEST = os.path.join(DATA_REPORTS, "дані", ".show-request")  # a second launch's request: show the window
WINDOW_REQUEST = os.path.join(DATA_REPORTS, "дані", ".window-request")  # the agent asks the open window to come back
LANG_REQUEST = os.path.join(DATA_REPORTS, "дані", ".lang-request")   # the window's language choice, applied by the agent
AGENT_LOG = os.path.join(DATA_REPORTS, "дані", "автозапуск.log")
PERSIST_FILE = os.path.join(DATA_REPORTS, "дані", "автозапуск-знімок.json")   # the baseline list of system login items
PREFS_FILE = os.path.join(DATA_REPORTS, "дані", "налаштування.json")   # menu bar switches
APP_HTML = os.path.join(HERE, "app.html")   # shown while there is no report yet
BAR_JS = os.path.join(HERE, "bar.js")       # the run bar, added to the report page
LOG_LINES = 300


def _free_pct():
    """Free memory by memory_pressure, in percent. None if it could not be read."""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"System-wide memory free percentage: (\d+)%", out)
    return int(m.group(1)) if m else None


def security_summary():
    """Security summary from the latest report: how many checks are in the “warning” or “critical” state.

    Reads only the JSON of the latest report, runs nothing. If there is no report yet, the level is “none”.
    """
    empty = {"level": "none", "issues": 0, "critical": 0, "checkedAt": None, "changes": [], "changeCount": 0}
    folder = os.path.join(DATA_DIR, "дані")
    try:
        names = sorted(n for n in os.listdir(folder) if re.match(r"^report-\d{4}-\d{2}-\d{2}\.json$", n))
    except OSError:
        return empty
    if not names:
        return empty
    def _security_items(name):
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        items = data.get("security") if isinstance(data, dict) else None
        return items if isinstance(items, list) else None

    last = names[-1]
    items = _security_items(last)
    if items is None:
        return empty
    kinds = [i.get("kind") for i in items if isinstance(i, dict)]
    crit = kinds.count("critical")
    warn = kinds.count("warning")
    m = re.match(r"^report-(\d{4}-\d{2}-\d{2})\.json$", last)
    checked = m.group(1) if m else None
    level = "critical" if crit else ("warning" if warn else "ok")
    # Changes relative to the previous report: what appeared, disappeared or changed
    changes = []
    if len(names) > 1:
        previous = _security_items(names[-2])
        if previous is not None:
            changes = security_changes(previous, items)
    return {"level": level, "issues": crit + warn, "critical": crit, "checkedAt": checked,
            "changes": changes[:6], "changeCount": len(changes)}


def dashboard_stale():
    """True if the central page is missing or older than the app template (after an app update)."""
    template = os.path.join(HERE, "core", "template.html")
    try:
        return os.path.getmtime(DASHBOARD) < os.path.getmtime(template)
    except OSError:
        return os.path.isfile(template)


def _lock_holder_alive():
    """Whether the process holding the check lock is alive. A lock without a PID or with a dead PID is stale."""
    try:
        with open(os.path.join(LOCK_DIR, "pid"), encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():   # its own lock without an active check is also stale
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Api:
    """Methods for the page through pywebview.api. No parameter becomes a command.

    Attributes with an underscore (for example _window) are not exposed to the page by pywebview.
    """

    def __init__(self):
        self._guard = threading.Lock()   # protects the state, the log and _proc
        self._proc = None
        self._stop_requested = False
        self._log = deque(maxlen=LOG_LINES)
        self._window = None              # set in main()
        self._keep_in_menubar = False    # True when the menu bar icon is installed
        self._quitting = False           # True after “Quit”: closing the window no longer hides it
        self._state = {"status": "idle", "started": None, "finished": None, "exit": None, "message": ""}

    def dashboard_url(self):
        return pathlib.Path(DASHBOARD).as_uri()

    # ---------- Permissions ----------
    # What can be done without manual setup and what cannot. Full Disk Access is not needed:
    # no function reads protected folders such as ~/Library/Mail. So there is no such step.
    def _finder_allowed(self):
        try:
            r = subprocess.run(
                ["osascript", "-e", 'tell application "Finder" to get name of startup disk'],
                capture_output=True, text=True, timeout=90,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def permissions(self):
        """Permission state. No requests are made here: read-only."""
        return {
            "firstRunDone": os.path.exists(MARKER),
            "items": [
                {"id": "finder", "title": T("Керування Finder: очистити кошик"), "action": "request_finder",
                 "note": T("macOS покаже запит один раз. Після «Дозволити» запит більше не з'являється.")},
                {"id": "admin", "title": T("Очищення памʼяті (purge)"), "action": "purge_memory",
                 "note": T("Потрібен пароль адміністратора при кожному запуску. macOS не дає дозволити це наперед.")},
            ],
        }

    def request_finder(self):
        """Triggers the macOS request to control Finder. Blocks until the user answers."""
        return {"ok": self._finder_allowed()}

    def finish_setup(self):
        """Marks the first run as done so the settings panel no longer appears."""
        try:
            os.makedirs(os.path.dirname(MARKER), exist_ok=True)
            open(MARKER, "w", encoding="utf-8").close()
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- Calendar: regenerating a day from a saved snapshot ----------
    def regen_day(self, day_id):
        if not isinstance(day_id, str) or not DAY_RE.match(day_id):
            return {"ok": False, "error": T("Неправильний ідентифікатор знімка")}
        try:
            r = subprocess.run(
                ["/bin/bash", GENERATOR, "--regen", day_id],
                cwd=REPORT_DIR, env=GEN_ENV, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": T("Не вдалося перегенерувати: {exc}", exc=exc)}
        if r.returncode != 0:
            last = (r.stderr or r.stdout or "").strip().splitlines()
            return {"ok": False, "error": last[-1] if last else T("Помилка регенерації")}
        return {"ok": True, "file": os.path.join(DATA_REPORTS, f"report-{day_id}.html")}

    # ---------- Memory ----------
    def purge_memory(self):
        """Frees inactive memory. macOS shows a window asking for the administrator password on each call."""
        before = _free_pct()
        mem_before = cleanup.memory_snapshot()
        try:
            r = subprocess.run(
                ["osascript", "-e", 'do shell script "/usr/sbin/purge" with administrator privileges'],
                capture_output=True, text=True, timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        if r.returncode != 0:
            return {"ok": False, "error": T("Пароль не введено або дію скасовано")}
        mem_after = cleanup.memory_snapshot()
        return {"ok": True, "freeBefore": before, "freeAfter": _free_pct(),
                "memory": {"before": mem_before, "after": mem_after,
                           "delta": cleanup.memory_delta(mem_before, mem_after)}}

    # ---------- Caches ----------
    def cache_scan(self, days=cleanup.OLD_DAYS):
        """Cleaning candidates with size, categories and a memory snapshot. Deletes nothing."""
        return cleanup.scan(days)

    def cache_clean(self, ids, confirm, days=cleanup.OLD_DAYS, categories=None):
        """Cleans the selected caches. Without confirm=True from the interface nothing is deleted.

        days — file age threshold (7, 30 or 90), categories — allowed categories; the rest is rejected.
        """
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return cleanup.clean(ids, days, categories)

    # ---------- Large files ----------
    def big_files(self, min_mb=100):
        """The largest files in the home folder. Read-only."""
        return bigfiles.find_big(min_mb)

    def reveal_file(self, path):
        """Shows a file in Finder. The path must be an existing file inside the home folder."""
        real = bigfiles.safe_path(path)
        if real is None:
            return {"ok": False, "error": T("Недозволений шлях")}
        try:
            subprocess.run(["/usr/bin/open", "-R", real], check=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error": T("Не вдалося показати у Finder")}
        return {"ok": True}

    def trash_big(self, path, confirm):
        """Moves a large file to the Trash (restorable). Only with confirmation and for a path inside the home folder."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        reason = zones.blocked_for_trash(path) if isinstance(path, str) and os.path.exists(path) else None
        if reason:   # projects, vaults, sync folders and other isolated zones are handled by hand in Finder
            return {"ok": False, "error": reason}
        ok, error = trash.move_to_trash(path)
        return {"ok": ok, "error": error}

    # ---------- Leftovers of removed apps ----------
    def leftovers_scan(self):
        """Folders in ~/Library from apps that no longer exist. Changes nothing."""
        return {"items": leftovers.scan(), "olderThanDays": leftovers.MIN_AGE_DAYS}

    def leftovers_trash(self, paths, confirm):
        """Moves the selected leftovers to the Trash. Only paths just found by the search are allowed."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        if not isinstance(paths, list) or not paths or len(paths) > leftovers.MAX_ITEMS:
            return {"ok": False, "error": T("Нічого не вибрано")}
        allowed = {item["path"]: item for item in leftovers.scan()}
        results, freed = [], 0.0
        for path in dict.fromkeys(paths):
            item = allowed.get(path) if isinstance(path, str) else None
            if item is None:
                results.append({"path": str(path), "ok": False, "error": T("Не є залишком застосунку")})
                continue
            ok, error = trash.move_to_trash(path)
            results.append({"path": path, "ok": ok, "error": error})
            if ok:
                freed += item["sizeMb"]
        return {"ok": True, "results": results, "freedMb": round(freed, 1)}

    # ---------- Disk map ----------
    def diskmap_start(self, path=None):
        """Starts counting one folder of the home folder (the home folder by default). Changes nothing."""
        return diskmap.start(path or os.path.expanduser("~"))

    def diskmap_poll(self, path):
        """What has been counted so far for a folder started with diskmap_start."""
        return diskmap.poll(path)

    def reveal_folder(self, path):
        """Shows a folder or file of the disk map in Finder."""
        return self.reveal_file(path)

    # ---------- Storage categories: games, virtual machines, developer tools ----------
    def storage_overview(self):
        """Which categories exist on this Mac (only the found ones), without counting sizes."""
        return {"items": storage.overview()}

    def storage_scan(self, category):
        """The items of one category with sizes. Changes nothing."""
        return {"items": storage.scan(category) if category in storage.TITLES else []}

    def storage_reveal(self, category, path):
        """Shows a path of the last scan in Finder."""
        if not isinstance(path, str) or not storage.known_path(category, path):
            return {"ok": False, "error": T("Недозволений шлях")}
        try:
            subprocess.run(["/usr/bin/open", "-R", path], check=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error": T("Не вдалося показати у Finder")}
        return {"ok": True}

    def storage_remove(self, ids, confirm):
        """Removes games to the Trash (Steam: the folder with its manifest and data). Only with confirmation."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return storage.remove(ids)

    # ---------- Duplicate files ----------
    def dupes_roots(self):
        """Folders of the home folder the search may look in; the standard ones come ticked."""
        return {"items": duplicates.available_roots()}

    def dupes_start(self, roots, min_mb=1):
        """Starts a background search for duplicates in the chosen standard folders. Changes nothing."""
        return duplicates.start(roots, min_mb)

    def dupes_poll(self):
        """Progress and, when done, the groups found."""
        return duplicates.poll()

    def dupes_cancel(self):
        """Stops a running search."""
        return duplicates.cancel()

    def dupes_trash(self, paths, confirm):
        """Moves the chosen copies to the Trash, keeping at least one copy of every group. Only with confirmation."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return duplicates.trash_copies(paths)

    # ---------- Programs: apps and command line packages ----------
    def apps_list(self):
        """Installed apps with size, version and last use. Changes nothing."""
        return {"items": apps.installed_apps()}

    def app_related(self, path):
        """Files in ~/Library that belong to one app, for the removal dialog. Changes nothing."""
        real = apps.safe_app_path(path)
        return {"items": apps.related_files(real) if real else []}

    def app_uninstall(self, path, related, confirm):
        """Moves an app and the chosen related files to the Trash. Only with confirmation, never a running app."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return apps.uninstall_app(path, related)

    def packages_list(self):
        """Homebrew, npm, cargo and pipx packages found on this Mac. Changes nothing."""
        return apps.packages()

    def packages_remove(self, ids, confirm):
        """Removes command line packages with the manager's own command, only ids from a fresh listing."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return apps.remove_packages(ids)

    # ---------- Action buttons: fixed commands run from the window, not from Terminal ----------
    def actions_list(self):
        """The actions the window may run, with their level and availability. The page never sends a command."""
        return {"items": actions.listing()}

    def action_run(self, action_id, confirm):
        """Starts one action by id in the background. mild and destructive ones need confirm=True."""
        return actions.start(action_id, confirm)

    def action_row(self, action_id, index, op, confirm):
        """One operation of one result row (quit a process, show a file, open a Settings pane): the page sends only indexes and names."""
        return actions.row_action(action_id, index, op, confirm)

    def action_status(self):
        """State and output of the running or last action."""
        return actions.status()

    # ---------- Local LLM models (a manual category, never part of the automatic cleaning) ----------
    def llm_scan(self):
        """Local models of known tools with size and last use. Changes nothing."""
        return {"items": llm.scan()}

    def llm_remove(self, ids, confirm):
        """Removes the chosen models: files go to the Trash, an Ollama model through `ollama rm`. Only with confirmation."""
        if confirm is not True:
            return {"ok": False, "error": T("Потрібне підтвердження")}
        return llm.remove(ids)

    def set_language(self, code):
        """Language choice from the report window's Settings. The agent saves it and restarts: this only leaves a request."""
        if code not in ("uk", "en"):
            return {"ok": False, "error": T("Невідома мова")}
        try:
            os.makedirs(os.path.dirname(LANG_REQUEST), exist_ok=True)
            with open(LANG_REQUEST, "w", encoding="utf-8") as fh:
                fh.write(code)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def save_snapshot(self, name, text):
        """Saves the JSON of the selected snapshot to Downloads. Accepts only a file name, not a path; an existing file is not overwritten."""
        if not isinstance(name, str) or not isinstance(text, str) or len(text) > 20_000_000:
            return {"ok": False, "error": T("Не вдалося зберегти")}
        base = re.sub(r"[^\w.\- ]", "_", os.path.basename(name.replace("\\", "/"))) or "snapshot.json"
        if not base.lower().endswith(".json"):
            base += ".json"
        folder = os.path.expanduser("~/Downloads")
        stem, ext = os.path.splitext(base)
        path, n = os.path.join(folder, base), 1
        try:
            os.makedirs(folder, exist_ok=True)
            while os.path.exists(path):
                path = os.path.join(folder, f"{stem} {n}{ext}")
                n += 1
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "name": os.path.basename(path), "path": path}

    def status(self):
        with self._guard:
            s = dict(self._state)
            s["log"] = list(self._log)
        s["now"] = time.time()
        return s

    def run_check(self):
        with self._guard:
            if self._state["status"] == "running":
                return {"ok": False, "error": T("Перевірка вже виконується")}
            if not os.path.isfile(GENERATOR):
                return {"ok": False, "error": T("Не знайдено {path}", path=GENERATOR)}
            try:
                os.makedirs(os.path.dirname(LOCK_DIR), exist_ok=True)
                os.mkdir(LOCK_DIR)
            except FileExistsError:
                if _lock_holder_alive():
                    return {"ok": False, "error": T("MacSafe уже виконує перевірку в іншому вікні")}
                # The lock was left by a process that stopped during a check: we remove it and start
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                os.mkdir(LOCK_DIR)
            with open(os.path.join(LOCK_DIR, "pid"), "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self._log.clear()
            self._stop_requested = False
            self._state.update(status="running", started=time.time(), finished=None, exit=None, message=T("Збір даних"))
        try:
            threading.Thread(target=self._run, daemon=True).start()
        except RuntimeError as exc:
            # The thread did not start: we release the lock, otherwise the next checks will not run
            try:
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
            except OSError:
                pass
            self._finish(-1, T("Не вдалося запустити потік: {exc}", exc=exc))
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _run(self):
        code, note = -1, ""
        try:
            proc = subprocess.Popen(
                ["/bin/bash", GENERATOR],
                cwd=REPORT_DIR,
                env=GEN_ENV,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,   # a separate group: stopping also affects child processes
            )
            with self._guard:
                self._proc = proc
                stop_now = self._stop_requested
            if stop_now:
                self._kill_group(proc)
            for line in proc.stdout:
                line = line.rstrip("\n")
                with self._guard:
                    self._log.append(line)
                    if line.strip():
                        self._state["message"] = line.strip()[:160]
            code = proc.wait()
        except OSError as exc:
            note = T("Не вдалося запустити перевірку: {exc}", exc=exc)
        finally:
            with self._guard:
                self._proc = None
            try:
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
            except OSError:
                pass
            self._finish(code, note)

    def _finish(self, code, note=""):
        with self._guard:
            if note:
                self._log.append(note)
            if self._stop_requested:
                msg, status = T("Перевірку зупинено"), "error"
            else:
                status = "done" if code == 0 else "error"
                msg = T("Готово") if code == 0 else T("Перевірка завершилась помилкою")
            self._state.update(status=status, finished=time.time(), exit=code, message=msg)
            window = self._window
        # After a successful check we show the fresh report; the run bar will add the load handler
        if window is not None and code == 0 and not self._stop_requested:
            try:
                window.load_url(self.dashboard_url())
            except Exception:
                pass

    @staticmethod
    def _kill_group(proc):
        """Stops the whole process group: first politely, then forcibly.

        A child process may ignore SIGTERM or outlive bash, so SIGKILL is sent
        the group, regardless of whether the main process is still alive.
        """
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + 3
        while time.time() < deadline and proc.poll() is None:
            time.sleep(0.1)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()

    def shutdown(self):
        """Stops the check together with its child processes. Called when the app quits."""
        with self._guard:
            self._stop_requested = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill_group(proc)

    def _on_close(self, *_):
        """Window closing. Not a public method: the page does not see it.

        If the menu bar icon is active, the window only hides. Returning False cancels the closing
        (this is how pywebview works). Otherwise closing ends the check.
        """
        if self._keep_in_menubar and not self._quitting:
            self._window.hide()
            return False
        self.shutdown()
        return None


