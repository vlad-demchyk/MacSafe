#!/usr/bin/env python3
"""MacSafe: агент статус-бару. Працює завжди і не має вікна та WebKit.

Показує CPU, RAM, GPU і процеси в статус-барі, перевіряє безпеку у фоні, запускає очищення і пам'ять.
Вікно звітів запускається окремим процесом (app.py --window) тільки коли його просять, і завершується
разом зі своїм вікном. Так у фоні немає ні WebKit, ні зайвого коду.

Один екземпляр: другий запуск просить показати вікно і тихо виходить (фоновий) або показує вікно (звичайний).
"""
import fcntl
import json
import os
import shlex
import subprocess
import sys
import threading
import time

import AppKit
from PyObjCTools import AppHelper

import alerts
import autostart
import persistence
import cleanup
import engine
import i18n
import menubar
from metrics import Monitor
from prefs import Prefs

_lock_handle = None
_window_proc = None


def _acquire_instance():
    """Блокування одного агента. Повертає True, якщо це єдиний екземпляр."""
    global _lock_handle
    os.makedirs(os.path.dirname(engine.APP_LOCK), exist_ok=True)
    fh = open(engine.APP_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_handle = fh   # тримаємо відкритим: блокування діє, доки живе процес
    return True


def _request_show():
    os.makedirs(os.path.dirname(engine.SHOW_REQUEST), exist_ok=True)
    open(engine.SHOW_REQUEST, "w").close()


def open_window(section=None):
    """Запускає вікно звітів окремим процесом. Якщо вікно вже відкрите, нічого не робить."""
    global _window_proc
    if _window_proc is not None and _window_proc.poll() is None:
        return
    cmd = [sys.executable, engine.APP_PY, "--window"]
    if section:
        cmd += ["--section", section]
    _window_proc = subprocess.Popen(cmd, cwd=engine.HERE)


def close_window():
    if _window_proc is not None and _window_proc.poll() is None:
        _window_proc.terminate()


def main():
    background = "--background" in sys.argv
    i18n.load(engine.PREFS_FILE)   # мова до створення будь-яких написів
    if not _acquire_instance():
        if not background:
            _request_show()   # уже працює: звичайний запуск просить показати вікно
        return

    AppKit.NSApplication.sharedApplication().setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    api = engine.Api()
    monitor = Monitor(interval=1.0)
    monitor.start()
    prefs = Prefs(engine.PREFS_FILE)
    bar = {}

    def check():
        r = api.run_check()
        if not r["ok"]:
            bar["ctrl"].set_note(r["error"])
            return
        threading.Thread(target=watch_check, daemon=True).start()

    def watch_check():
        # Прогрес перевірки в рядок статус-бару; після завершення індикатор безпеки оновлюється одразу
        last = ""
        while True:
            s = api.status()
            if s["status"] != "running":
                break
            msg = (s.get("message") or T("Збір даних"))[:90]
            if msg != last:
                last = msg
                AppHelper.callAfter(bar["ctrl"].set_note, T("Перевірка: {msg}", msg=msg))
            time.sleep(1)
        done = s["status"] == "done"
        AppHelper.callAfter(bar["ctrl"].set_note, T("Перевірку завершено, звіт оновлено") if done else T("Перевірка не вдалась"))
        AppHelper.callAfter(bar["ctrl"].refresh_security)

    def purge():
        def work():
            r = api.purge_memory()
            delta = (r.get("memory") or {}).get("delta") if r["ok"] else None
            extra = T(", доступно {avail:+d} МБ", avail=delta["availableMb"]) if delta else ""
            text = (T("Вільна памʼять: {before}% → {after}%{extra}", before=r["freeBefore"], after=r["freeAfter"], extra=extra) if r["ok"]
                    else r.get("error") or T("Не вдалося очистити памʼять"))
            AppHelper.callAfter(bar["ctrl"].set_note, text)
        threading.Thread(target=work, daemon=True).start()

    def toggle_autostart():
        if autostart.is_enabled():
            ok, err = autostart.disable()
            if not ok:
                bar["ctrl"].set_note(T("Вимкнено з наступного входу, launchd не зняв зараз: {err}", err=err))
            return autostart.is_enabled()
        ok, err = autostart.enable(engine.LAUNCH_ARGS, engine.DATA_DIR, engine.AGENT_LOG)
        if not ok:
            bar["ctrl"].set_note(T("Записано, але launchd не запустив зараз: {err}. Діє з наступного входу.", err=err))
        return autostart.is_enabled()

    rules = alerts.Alerts()
    daily = {"at": -3600.0}

    def notify(title, text):
        # Сповіщення macOS через osascript у фоні; помилка (немає дозволу) не ламає агента
        def work():
            script = f'display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}'
            try:
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        threading.Thread(target=work, daemon=True).start()

    def check_alerts(snap, security):
        for title, text in rules.check(snap, security):
            notify(title, text)

    def daily_check():
        # Раз на годину дивимось, чи є звіт за сьогодні; якщо ні і перевірка не йде, запускаємо її
        now = time.monotonic()
        if now - daily["at"] < 3600:
            return
        daily["at"] = now
        stale = engine.dashboard_stale()   # після оновлення застосунку сторінка звітів перебудовується новим шаблоном
        due = prefs.get("dailyCheck") and engine.security_summary().get("checkedAt") != time.strftime("%Y-%m-%d")
        if api.status()["status"] != "running" and (stale or due):
            check()

    def tick():
        # Другий запуск лишає запит на показ вікна; перевіряємо раз на секунду
        if os.path.exists(engine.SHOW_REQUEST):
            try:
                os.remove(engine.SHOW_REQUEST)
            except OSError:
                pass
            open_window()

    def cleanup_window():
        open_window("cleanup")

    def quit_app():
        def finish():
            close_window()
            api.shutdown()
            monitor.stop()
            AppHelper.callAfter(shut_down)
        threading.Thread(target=finish, daemon=True).start()

    def shut_down():
        if "ctrl" in bar:
            bar["ctrl"].close()
        AppHelper.stopEventLoop()

    def cache_scan():
        # Швидке очищення — лише фіксовані безпечні кеші, без кешів окремих застосунків
        return cleanup.scan_fixed()

    def cache_clean(ids):
        # Підтвердження — двоетапний клік у панелі; видаляються лише файли старші за OLD_DAYS
        return cleanup.clean_old(ids)

    def set_language(code):
        # Мову застосовуємо під час запуску: зберігаємо її і перезапускаємо агент. Вікно звітів закривається
        if code not in ("uk", "en"):
            return
        prefs.set("language", code)
        restart_later()

    def restart_later():
        # Новий процес стартує після виходу старого: лок одного екземпляра звільниться
        cmd = " ".join(shlex.quote(part) for part in engine.LAUNCH_ARGS + ["--background"])
        subprocess.Popen(["/bin/sh", "-c", f"sleep 2; exec {cmd}"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        quit_app()

    actions = {
        "set_language": set_language,
        "show": open_window, "reports": open_window, "cleanup": cleanup_window,
        "security": engine.security_summary,
        "check": check, "purge": purge, "autostart": toggle_autostart,
        "quit": quit_app, "tick": tick,
        "cache_scan": cache_scan, "cache_clean": cache_clean,
        "alerts": check_alerts,
    }

    def install():
        bar["ctrl"] = menubar.install(monitor, actions, autostart.is_enabled(), prefs)
        if not background:
            open_window()   # звичайний запуск з Finder одразу показує вікно звітів

    def watch_persistence():
        # Раз на 10 хвилин у фоновому потоці: нові LaunchAgents/LaunchDaemons. Базовий знімок мовчки.
        def work():
            try:
                fresh = persistence.check(engine.PERSIST_FILE)
            except Exception:
                return
            if prefs.get("alerts"):
                for item in [i for i in fresh if i["label"] != autostart.LABEL][:3]:
                    notify(T("Новий елемент автозапуску"), f"{item['label']} ({item['path']})")
        threading.Thread(target=work, daemon=True).start()
        AppHelper.callLater(600, watch_persistence)

    def start_daily():
        # Перша перевірка через хвилину після старту: спершу панель і монітор
        AppHelper.callLater(60, daily_loop)

    def daily_loop():
        daily_check()
        AppHelper.callLater(3600, daily_loop)

    AppHelper.callAfter(install)
    AppHelper.callAfter(start_daily)
    AppHelper.callLater(20, watch_persistence)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
