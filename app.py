#!/usr/bin/env python3
"""MacSafe: вікно звітів. Запускається агентом (agent.py) за потреби і завершується разом з вікном.

Дані, перевірки і очищення живуть у engine.py. Тут лише вікно pywebview з мостом для сторінки.
Після закриття вікна процес завершується, тож у фоні від нього нічого не лишається.

Використання: app.py --window [--section cleanup]
"""
import os
import pathlib
import sys

import AppKit
import webview
from PyObjCTools import AppHelper

import engine
import i18n


def _section():
    if "--section" in sys.argv:
        i = sys.argv.index("--section")
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def main():
    i18n.load(engine.PREFS_FILE)
    api = engine.Api()
    with open(engine.BAR_JS, encoding="utf-8") as fh:
        bar_js = fh.read()
    base = (pathlib.Path(engine.DASHBOARD).as_uri() if os.path.isfile(engine.DASHBOARD)
            else pathlib.Path(engine.APP_HTML).as_uri())
    section = _section()
    window = webview.create_window(
        "MacSafe",
        url=f"{base}?lang={i18n.language()}" + (f"#{section}" if section else ""),
        js_api=api,
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    api._window = window

    def on_loaded():
        # Панель запуску додаємо після кожного завантаження сторінки
        try:
            window.evaluate_js(bar_js)
        except Exception:
            pass
        # Процес запущено з фонового агента: без цього вікно може лишитися позаду інших застосунків
        AppHelper.callAfter(lambda: AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True))

    def on_closing(*_):
        # Закриття вікна зупиняє перевірку, якщо вона запущена з вікна; None дозволяє закриття
        api.shutdown()
        return None

    window.events.loaded += on_loaded
    window.events.closing += on_closing
    webview.start(debug=False)


if __name__ == "__main__":
    main()
