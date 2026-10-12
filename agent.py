#!/usr/bin/env python3
"""MacSafe: menu bar agent. Runs all the time and has no window or WebKit.

Shows CPU, RAM, GPU and processes in the menu bar, checks security in the background, starts cleaning and memory actions.
The report window runs as a separate process (app.py --window) only when asked, and exits
together with its window. So there is neither WebKit nor extra code in the background.

One instance: a second launch asks to show the window and quietly exits (background) or shows the window (normal).
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
import history
import i18n
import menubar
import metrics
from i18n import T
from metrics import Monitor
from prefs import Prefs

_lock_handle = None
_window_proc = None


def _acquire_instance():
    """Lock for a single agent. Returns True if this is the only instance."""
    global _lock_handle
    os.makedirs(os.path.dirname(engine.APP_LOCK), exist_ok=True)
    fh = open(engine.APP_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    _lock_handle = fh   # we keep it open: the lock holds as long as the process lives
    return True


def _request_show():
    os.makedirs(os.path.dirname(engine.SHOW_REQUEST), exist_ok=True)
    open(engine.SHOW_REQUEST, "w").close()


def open_window(section=None):
    """Starts the report window as a separate process. If the window is already open, does nothing."""
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
    i18n.load(engine.PREFS_FILE)   # the language before any labels are created
    if not _acquire_instance():
        if not background:
            _request_show()   # already running: a normal launch asks to show the window
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
        # Check progress in the menu bar line; after it finishes the security indicator updates at once
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

    def busiest_app():
        # Two scans a second apart give real CPU values; the closed agent does not scan processes otherwise
        try:
            metrics.take_processes()
            time.sleep(1)
            top = metrics.take_processes()[0]
            return top[0]["name"] if top and top[0]["cpu"] >= 30 else None
        except Exception:
            return None

    rules = alerts.Alerts(culprit=busiest_app)
    daily = {"at": -3600.0}

    def notify(title, text):
        # macOS notifications through osascript in the background; an error (no permission) does not break the agent
        def work():
            script = f'display notification {json.dumps(text, ensure_ascii=False)} with title {json.dumps(title, ensure_ascii=False)}'
            try:
                subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        threading.Thread(target=work, daemon=True).start()

    def check_alerts(snap, security):
        off = tuple(name for key, name in (
            ("alertMemoryDisk", "memory_disk"), ("alertBattery", "battery"), ("alertLoad", "load")) if not prefs.get(key))
        for title, text in rules.check(snap, security, off):
            notify(title, text)

    def daily_check():
        # Once an hour we check whether there is a report for today; if not and no check is running, we start one
        now = time.monotonic()
        if now - daily["at"] < 3600:
            return
        daily["at"] = now
        stale = engine.dashboard_stale()   # after an app update the report page is rebuilt with the new template
        due = prefs.get("dailyCheck") and engine.security_summary().get("checkedAt") != time.strftime("%Y-%m-%d")
        if api.status()["status"] != "running" and (stale or due):
            check()

    def tick():
        # A second launch leaves a request to show the window; we check once a second
        if os.path.exists(engine.SHOW_REQUEST):
            try:
                os.remove(engine.SHOW_REQUEST)
            except OSError:
                pass
            open_window()
        # The report window's Settings leave the chosen language in a request file: apply it here
        if os.path.exists(engine.LANG_REQUEST):
            try:
                with open(engine.LANG_REQUEST, encoding="utf-8") as fh:
                    code = fh.read().strip()
                os.remove(engine.LANG_REQUEST)
            except OSError:
                code = ""
            set_language(code)

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
        # Quick clean — only the fixed safe caches, without the caches of individual apps
        return cleanup.scan_fixed()

    def cache_clean(ids):
        # Confirmation is a two-step click in the panel; only files older than OLD_DAYS are deleted
        return cleanup.clean_old(ids)

    def set_language(code):
        # We apply the language at launch: we save it and restart the agent. The report window is closed
        if code not in ("uk", "en") or code == i18n.language():
            return
        prefs.set("language", code)
        restart_later()

    def restart_later():
        # The new process starts after the old one exits: the single-instance lock will be released
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
            open_window()   # a normal launch from Finder shows the report window at once

    def watch_persistence():
        # Every 10 minutes in a background thread: new LaunchAgents/LaunchDaemons. The baseline snapshot is recorded silently.
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

    def watch_history():
        # A sample of temperatures and power every minute in a background thread: the history for graphs and the score
        def work():
            try:
                history.append(history.take_sample())
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()
        AppHelper.callLater(history.SAMPLE_EVERY, watch_history)

    def start_daily():
        # The first check one minute after start: the panel and the monitor come first
        AppHelper.callLater(60, daily_loop)

    def daily_loop():
        daily_check()
        AppHelper.callLater(3600, daily_loop)

    AppHelper.callAfter(install)
    AppHelper.callAfter(start_daily)
    AppHelper.callLater(20, watch_persistence)
    AppHelper.callLater(15, watch_history)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
