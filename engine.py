"""MacSafe: дані, звіти, очищення і перевірки без інтерфейсу.

Спільний код для агента статус-бару (agent.py) і вікна звітів (app.py). Імпорту AppKit і WebKit тут немає,
тож агент працює без важкого вікна. Усі шляхи і формати даних тут, у одному місці.
"""
import json
import os
import shutil
import pathlib
import re
import signal
import subprocess
import threading
import time
from collections import deque

import autostart
import bigfiles
import cleanup
import leftovers
import trash
from diff import security_changes

HERE = os.path.dirname(os.path.abspath(__file__))
APP_PY = os.path.join(HERE, "app.py")      # вікно звітів (запускається за потреби)
AGENT_PY = os.path.join(HERE, "agent.py")  # статус-бар і фон: те, що працює завжди
# Дані застосунку: ~/Library/Application Support/MacSafe. Старої папки «звіт» на Desktop більше немає.
DATA_DIR = os.path.expanduser("~/Library/Application Support/MacSafe")
REPORT_DIR = DATA_DIR                                      # робоча папка генератора
GENERATOR = os.path.join(HERE, "core", "generate-report.sh")
DATA_REPORTS = DATA_DIR                                    # звіти, знімки, історія
MARKER = os.path.join(DATA_DIR, "дані", ".first-run-done")
GEN_ENV = dict(os.environ, AUDIT_DATA_DIR=DATA_DIR)
DAY_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")   # один звіт на день: ідентифікатор — дата
DASHBOARD = os.path.join(DATA_DIR, "dashboard.html")
LOCK_DIR = os.path.join(DATA_DIR, "дані", ".lock")
# Як запускати себе з автозапуску: у .app це його запускач, інакше системний Python з app.py
LAUNCH_ARGS = [os.environ["MACSAFE_LAUNCHER"]] if os.environ.get("MACSAFE_LAUNCHER") else [autostart.PYTHON, AGENT_PY]
APP_LOCK = os.path.join(DATA_REPORTS, "дані", ".app.lock")          # один екземпляр застосунку
SHOW_REQUEST = os.path.join(DATA_REPORTS, "дані", ".show-request")  # запит другого запуску: показати вікно
AGENT_LOG = os.path.join(DATA_REPORTS, "дані", "автозапуск.log")
PERSIST_FILE = os.path.join(DATA_REPORTS, "дані", "автозапуск-знімок.json")   # базовий список автозапуску системи
PREFS_FILE = os.path.join(DATA_REPORTS, "дані", "налаштування.json")   # перемикачі статус-бару
APP_HTML = os.path.join(HERE, "app.html")   # показується, поки звіту ще немає
BAR_JS = os.path.join(HERE, "bar.js")       # панель запуску, додається на сторінку звітів
LOG_LINES = 300


def _free_pct():
    """Вільна памʼять за memory_pressure, у відсотках. None, якщо не вдалося прочитати."""
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"System-wide memory free percentage: (\d+)%", out)
    return int(m.group(1)) if m else None


def security_summary():
    """Підсумок безпеки з останнього звіту: скільки перевірок у стані «увага» або «критично».

    Читає лише JSON останнього звіту, нічого не запускає. Якщо звіту ще немає, рівень «none».
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
    # Зміни відносно попереднього звіту: що з'явилось, зникло або стало інакшим
    changes = []
    if len(names) > 1:
        previous = _security_items(names[-2])
        if previous is not None:
            changes = security_changes(previous, items)
    return {"level": level, "issues": crit + warn, "critical": crit, "checkedAt": checked,
            "changes": changes[:6], "changeCount": len(changes)}


def dashboard_stale():
    """True, якщо центральної сторінки немає або вона старіша за шаблон застосунку (після оновлення програми)."""
    template = os.path.join(HERE, "core", "template.html")
    try:
        return os.path.getmtime(DASHBOARD) < os.path.getmtime(template)
    except OSError:
        return os.path.isfile(template)


def _lock_holder_alive():
    """Чи живий процес, що тримає замок перевірки. Замок без PID або з мертвим PID — застарілий."""
    try:
        with open(os.path.join(LOCK_DIR, "pid"), encoding="utf-8") as fh:
            pid = int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return False
    if pid <= 0 or pid == os.getpid():   # свій замок без активної перевірки теж застарілий
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Api:
    """Методи для сторінки через pywebview.api. Жоден параметр не стає командою.

    Атрибути з підкресленням (наприклад _window) pywebview не відкриває сторінці.
    """

    def __init__(self):
        self._guard = threading.Lock()   # захищає стан, журнал і _proc
        self._proc = None
        self._stop_requested = False
        self._log = deque(maxlen=LOG_LINES)
        self._window = None              # задається в main()
        self._keep_in_menubar = False    # True, коли іконка в статус-барі встановлена
        self._quitting = False           # True після «Вийти»: закриття вікна більше не ховає його
        self._state = {"status": "idle", "started": None, "finished": None, "exit": None, "message": ""}

    def dashboard_url(self):
        return pathlib.Path(DASHBOARD).as_uri()

    # ---------- Дозволи ----------
    # Що можна зробити без ручного налаштування, а що ні. Повний доступ до диска не потрібен:
    # жодна функція не читає захищені папки на кшталт ~/Library/Mail. Тому такого кроку немає.
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
        """Стан дозволів. Ніяких запитів тут не робиться: лише читання."""
        return {
            "firstRunDone": os.path.exists(MARKER),
            "items": [
                {"id": "finder", "title": "Керування Finder: очистити кошик", "action": "request_finder",
                 "note": "macOS покаже запит один раз. Після «Дозволити» запит більше не з'являється."},
                {"id": "admin", "title": "Очищення памʼяті (purge)", "action": "purge_memory",
                 "note": "Потрібен пароль адміністратора при кожному запуску. macOS не дає дозволити це наперед."},
            ],
        }

    def request_finder(self):
        """Викликає запит macOS на керування Finder. Блокує, поки користувач не відповість."""
        return {"ok": self._finder_allowed()}

    def finish_setup(self):
        """Позначає перший запуск завершеним, щоб панель налаштувань більше не з'являлась."""
        try:
            os.makedirs(os.path.dirname(MARKER), exist_ok=True)
            open(MARKER, "w", encoding="utf-8").close()
            return {"ok": True}
        except OSError as exc:
            return {"ok": False, "error": str(exc)}

    # ---------- Календар: регенерація дня з збереженого знімка ----------
    def regen_day(self, day_id):
        if not isinstance(day_id, str) or not DAY_RE.match(day_id):
            return {"ok": False, "error": "Неправильний ідентифікатор знімка"}
        try:
            r = subprocess.run(
                ["/bin/bash", GENERATOR, "--regen", day_id],
                cwd=REPORT_DIR, env=GEN_ENV, capture_output=True, text=True, timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"Не вдалося перегенерувати: {exc}"}
        if r.returncode != 0:
            last = (r.stderr or r.stdout or "").strip().splitlines()
            return {"ok": False, "error": last[-1] if last else "Помилка регенерації"}
        return {"ok": True, "file": os.path.join(DATA_REPORTS, f"report-{day_id}.html")}

    # ---------- Памʼять ----------
    def purge_memory(self):
        """Очищає неактивну памʼять. macOS показує вікно з паролем адміністратора при кожному виклику."""
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
            return {"ok": False, "error": "Пароль не введено або дію скасовано"}
        mem_after = cleanup.memory_snapshot()
        return {"ok": True, "freeBefore": before, "freeAfter": _free_pct(),
                "memory": {"before": mem_before, "after": mem_after,
                           "delta": cleanup.memory_delta(mem_before, mem_after)}}

    # ---------- Кеші ----------
    def cache_scan(self, days=cleanup.OLD_DAYS):
        """Кандидати на очищення з розміром, категоріями і знімком памʼяті. Нічого не видаляє."""
        return cleanup.scan(days)

    def cache_clean(self, ids, confirm, days=cleanup.OLD_DAYS, categories=None):
        """Очищує вибрані кеші. Без confirm=True з інтерфейсу нічого не видаляється.

        days — поріг віку файлів (7, 30 або 90), categories — дозволені категорії; решта відхиляється.
        """
        if confirm is not True:
            return {"ok": False, "error": "Потрібне підтвердження"}
        return cleanup.clean(ids, days, categories)

    # ---------- Великі файли ----------
    def big_files(self, min_mb=100):
        """Найбільші файли в домашній папці. Тільки читання."""
        return bigfiles.find_big(min_mb)

    def reveal_file(self, path):
        """Показує файл у Finder. Шлях має бути існуючим файлом усередині домашньої папки."""
        real = bigfiles.safe_path(path)
        if real is None:
            return {"ok": False, "error": "Недозволений шлях"}
        try:
            subprocess.run(["/usr/bin/open", "-R", real], check=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return {"ok": False, "error": "Не вдалося показати у Finder"}
        return {"ok": True}

    def trash_big(self, path, confirm):
        """Переміщує великий файл у кошик (відновлюється). Лише з підтвердженням і для шляху всередині домашньої папки."""
        if confirm is not True:
            return {"ok": False, "error": "Потрібне підтвердження"}
        ok, error = trash.move_to_trash(path)
        return {"ok": ok, "error": error}

    # ---------- Залишки видалених застосунків ----------
    def leftovers_scan(self):
        """Папки в ~/Library від застосунків, яких уже немає. Нічого не змінює."""
        return {"items": leftovers.scan(), "olderThanDays": leftovers.MIN_AGE_DAYS}

    def leftovers_trash(self, paths, confirm):
        """Переміщує вибрані залишки в кошик. Дозволено лише шляхи, які щойно знайшов пошук."""
        if confirm is not True:
            return {"ok": False, "error": "Потрібне підтвердження"}
        if not isinstance(paths, list) or not paths or len(paths) > leftovers.MAX_ITEMS:
            return {"ok": False, "error": "Нічого не вибрано"}
        allowed = {item["path"]: item for item in leftovers.scan()}
        results, freed = [], 0.0
        for path in dict.fromkeys(paths):
            item = allowed.get(path) if isinstance(path, str) else None
            if item is None:
                results.append({"path": str(path), "ok": False, "error": "Не є залишком застосунку"})
                continue
            ok, error = trash.move_to_trash(path)
            results.append({"path": path, "ok": ok, "error": error})
            if ok:
                freed += item["sizeMb"]
        return {"ok": True, "results": results, "freedMb": round(freed, 1)}

    def status(self):
        with self._guard:
            s = dict(self._state)
            s["log"] = list(self._log)
        s["now"] = time.time()
        return s

    def run_check(self):
        with self._guard:
            if self._state["status"] == "running":
                return {"ok": False, "error": "Перевірка вже виконується"}
            if not os.path.isfile(GENERATOR):
                return {"ok": False, "error": f"Не знайдено {GENERATOR}"}
            try:
                os.makedirs(os.path.dirname(LOCK_DIR), exist_ok=True)
                os.mkdir(LOCK_DIR)
            except FileExistsError:
                if _lock_holder_alive():
                    return {"ok": False, "error": "MacSafe уже виконує перевірку в іншому вікні"}
                # Замок лишився від процесу, який зупинився під час перевірки: прибираємо і запускаємо
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
                os.mkdir(LOCK_DIR)
            with open(os.path.join(LOCK_DIR, "pid"), "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self._log.clear()
            self._stop_requested = False
            self._state.update(status="running", started=time.time(), finished=None, exit=None, message="Збір даних")
        try:
            threading.Thread(target=self._run, daemon=True).start()
        except RuntimeError as exc:
            # Потік не стартував: знімаємо блокування, інакше наступні перевірки не запустяться
            try:
                shutil.rmtree(LOCK_DIR, ignore_errors=True)
            except OSError:
                pass
            self._finish(-1, f"Не вдалося запустити потік: {exc}")
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
                start_new_session=True,   # окрема група: зупинка зачіпає і дочірні процеси
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
            note = f"Не вдалося запустити перевірку: {exc}"
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
                msg, status = "Перевірку зупинено", "error"
            else:
                status = "done" if code == 0 else "error"
                msg = "Готово" if code == 0 else "Перевірка завершилась помилкою"
            self._state.update(status=status, finished=time.time(), exit=code, message=msg)
            window = self._window
        # Після успішної перевірки показуємо свіжий звіт; панель запуску додасть обробник завантаження
        if window is not None and code == 0 and not self._stop_requested:
            try:
                window.load_url(self.dashboard_url())
            except Exception:
                pass

    @staticmethod
    def _kill_group(proc):
        """Зупиняє всю групу процесів: спершу ввічливо, потім примусово.

        Дочірній процес може ігнорувати SIGTERM або пережити bash, тому SIGKILL надсилається
        групі незалежно від того, чи ще жив головний процес.
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
        """Зупиняє перевірку разом з дочірніми процесами. Викликається при виході з застосунку."""
        with self._guard:
            self._stop_requested = True
            proc = self._proc
        if proc is not None and proc.poll() is None:
            self._kill_group(proc)

    def _on_close(self, *_):
        """Закриття вікна. Не публічний метод: сторінка його не бачить.

        Якщо іконка в статус-барі активна, вікно лише ховається. Повернення False скасовує закриття
        (так працює pywebview). Інакше закриття завершує перевірку.
        """
        if self._keep_in_menubar and not self._quitting:
            self._window.hide()
            return False
        self.shutdown()
        return None


