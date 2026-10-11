"""Автозапуск при вході в систему: LaunchAgent, який відкриває застосунок з --background.

Фонова копія не відкриває вікно, лише іконку в статус-барі. Одна копія вже працює: фоновий запуск
виходить тихо через блокування екземпляра (app.py), тож реєструвати сервіс можна без ризику дубля.

Вимкнення і перереєстрація викликають launchctl bootout, але не тоді, коли застосунок сам запущений
як job launchd: launchd позначає такий процес змінною XPC_SERVICE_NAME, що дорівнює мітці. Bootout
свого job вбив би процес, який виконує дію. У цьому випадку plist змінюється, і зміна діє з наступного входу.
"""
import os
import plistlib
import subprocess

LABEL = "com.user.macsafe"
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
AGENT = os.path.join(AGENT_DIR, f"{LABEL}.plist")
PYTHON = "/usr/bin/python3"       # той самий інтерпретатор, що й у запускачі


def _domain():
    return f"gui/{os.getuid()}"


def _is_self_job():
    """True, якщо цей процес запущений самим launchd як job з нашою міткою."""
    return os.environ.get("XPC_SERVICE_NAME") == LABEL


def is_enabled():
    return os.path.isfile(AGENT)


def _is_loaded():
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    return r.returncode == 0


def _launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if r.returncode == 0:
        return True, ""
    lines = (r.stderr or r.stdout).strip().splitlines()
    return False, lines[-1] if lines else "launchctl не вдався"


def _read_plist():
    try:
        with open(AGENT, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, ValueError):
        return None


def _build(launch_args, workdir, log_path):
    return {
        "Label": LABEL,
        "ProgramArguments": [*launch_args, "--background"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def enable(launch_args, workdir, log_path):
    """Записує plist атомарно і реєструє сервіс у launchd. Повертає (ok, пояснення).

    launch_args: команда запуску без --background (запускач .app або python app.py).
    """
    os.makedirs(AGENT_DIR, exist_ok=True)
    plist = _build(launch_args, workdir, log_path)
    changed = _read_plist() != plist
    tmp = f"{AGENT}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        plistlib.dump(plist, fh)
    os.replace(tmp, AGENT)

    if _is_loaded():
        if not changed or _is_self_job():
            return True, ""
        # Новий plist діє лише після перереєстрації; старий сервіс спершу знімаємо
        ok, err = _launchctl("bootout", f"{_domain()}/{LABEL}")
        if not ok:
            return False, err
    return _launchctl("bootstrap", _domain(), AGENT)


def disable():
    """Прибирає plist і знімає сервіс з launchd, якщо він не є поточним процесом. Повертає (ok, пояснення)."""
    try:
        os.remove(AGENT)
    except FileNotFoundError:
        pass
    if _is_self_job() or not _is_loaded():
        return True, ""
    return _launchctl("bootout", f"{_domain()}/{LABEL}")
