"""Interface language: Ukrainian or English.

The EN dictionary key is the Ukrainian phrase from the code. T(“Text”) returns the translation for the current language,
and without a translation it keeps the original, so a missing line does not break the interface.
Placeholders are formatted after translation: T(“Free: {n} MB”, n=5) → “Free: 5 MB”.

The language is stored in the settings: “uk” or “en”. English is the default.
"""
import json
import os

LANGS = ("uk", "en")
DEFAULT = "en"   # English by default; the switch is in Appearance (menu bar) and in the report window's Settings
_current = DEFAULT


def set_language(code):
    global _current
    _current = code if code in LANGS else DEFAULT


def language():
    return _current


def load(prefs_path):
    """Reads the language from the settings file and sets it. Missing or invalid values give English."""
    pref = DEFAULT
    try:
        with open(prefs_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict) and raw.get("language") in LANGS:
            pref = raw["language"]
    except (OSError, ValueError):
        pass
    set_language(pref)
    return _current


def T(text, **kw):
    """Translation of a phrase for the current language. kw values are substituted into the {name} placeholders."""
    out = EN.get(text, text) if _current == "en" else text
    return out.format(**kw) if kw else out


def plural_problem(n):
    """The word “problem” in the plural, as in the report: 1 problem, 2 problems, 5 problems."""
    if _current == "en":
        return "problem" if n == 1 else "problems"
    if n % 10 == 1 and n % 100 != 11:
        return "проблема"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "проблеми"
    return "проблем"


EN = {
    # ---- menu bar panel (menubar.py)
    "Іконка і цифри": "Icon and numbers",
    "Лише іконка": "Icon only",
    "Лише цифри": "Numbers only",
    "Показувати CPU": "Show CPU",
    "Показувати RAM": "Show RAM",
    "Показувати GPU": "Show GPU",
    "проблема": "problem",
    "проблеми": "problems",
    "проблем": "problems",
    "Безпека: звіту ще немає": "Security: no report yet",
    "Безпека: усе гаразд": "Security: all good",
    "Безпека: {n} {word}": "Security: {n} {word}",
    "ЦП": "CPU",
    "ГПУ": "GPU",
    "ОЗП": "RAM",
    "Вигляд": "Appearance",
    "Дії": "Actions",
    "Система": "System",
    "Користувач": "User",
    "Вільно": "Free",
    "Енергоефективні ядра": "Efficiency cores",
    "Продуктивні ядра": "Performance cores",
    "Час роботи": "Uptime",
    "Навантаження 1 · 5 · 15 хв": "Load 1 · 5 · 15 min",
    "{v:.1f} МБ/с": "{v:.1f} MB/s",
    "{v:.0f} КБ/с": "{v:.0f} KB/s",
    "{d} дн. {h} год": "{d} d {h} h",
    "{h} год {m} хв": "{h} h {m} min",
    "Коротко": "Summary",
    "Звіт від {checked}": "Report from {checked}",
    "Звіту ще немає": "No report yet",
    "ЗМІНИ ВІД ПОПЕРЕДНЬОГО ЗВІТУ: {count}": "CHANGES SINCE THE PREVIOUS REPORT: {count}",
    "ЗМІН НЕМАЄ": "NO CHANGES",
    "з'явилась: {to}": "appeared: {to}",
    "зникла": "gone",
    "Очікування даних": "Waiting for data",
    "Ядра: синє — продуктивні (P), бірюзове — економні (E)": "Cores: blue is performance (P), teal is efficiency (E)",
    "історія за 60 секунд": "60-second history",
    "ДИСК": "DISK",
    "МЕРЕЖА": "NETWORK",
    "БАТАРЕЯ": "BATTERY",
    "Звіти": "Reports",
    "Відкрити додаток": "Open app",
    "Невідома мова": "Unknown language",
    "Не вдалося зберегти": "Could not save",
    "Вийти": "Quit",
    "Коротко: безпека і зміни від попереднього звіту": "Summary: security and changes since the previous report",
    "ТОП ПРОЦЕСІВ": "TOP PROCESSES",
    "ПАМʼЯТЬ": "MEMORY",
    "Модель": "Model",
    "Ядра": "Cores",
    "Використання": "Usage",
    "Використовується": "In use",
    "Програми": "Apps",
    "Постійна": "Wired",
    "Стиснена": "Compressed",
    "Вільна": "Free",
    "{used:.1f} ГБ з {total:.0f} ГБ": "{used:.1f} GB of {total:.0f} GB",
    "{v:.1f} ГБ": "{v:.1f} GB",
    "{v:.0f} МБ": "{v:.0f} MB",
    "{free:.0f} ГБ": "{free:.0f} GB",
    "{cores} ядер · загальне навантаження": "{cores} cores · overall load",
    "{cores} ядер": "{cores} cores",
    "Дані недоступні": "Data unavailable",
    "Навантаження графіки": "GPU load",
    "{used:.1f} з {total:.1f} ГБ · swap {swap:.1f} ГБ": "{used:.1f} of {total:.1f} GB · swap {swap:.1f} GB",
    "{used:.1f} з {total:.1f} ГБ, swap {swap:.1f} ГБ": "{used:.1f} of {total:.1f} GB, swap {swap:.1f} GB",
    "н/д": "n/a",
    "звіту ще немає": "no report yet",
    "Останній звіт: {checked}. Змін з попереднього звіту: {changed}. Деталі: «Звіти»": "Last report: {checked}. Changes since the previous report: {changed}. Details: “Reports”",
    "немає батареї": "no battery",
    "від мережі": "on power",
    "від батареї": "on battery",
    "з {total:.0f} ГБ": "of {total:.0f} GB",
    "Показники в барі": "Menu bar metrics",
    "Автоматично": "Automatic",
    "Сповіщення": "Notifications",
    "Щоденна перевірка": "Daily check",
    "Стиль в барі": "Menu bar style",
    "Обидва": "Both",
    "Іконка": "Icon",
    "Цифри": "Numbers",
    "Оновлення даних": "Refresh interval",
    "{s} с": "{s} s",
    "Мова": "Language",
    "Швидке очищення": "Quick clean",
    "Перевірка": "Check",
    "Детальне очищення": "Detailed clean",
    "Звільнити памʼять": "Free memory",
    "Запускати при вході": "Start at login",
    "Так?": "Sure?",
    "Панель недоступна, використовуйте меню: {exc}": "Panel unavailable, use the menu: {exc}",
    "Не вдалося виконати дію: {exc}": "Could not perform the action: {exc}",
    "Очищую: npm, pip, Homebrew, Xcode…": "Cleaning: npm, pip, Homebrew, Xcode…",
    "Рахую розмір безпечних кешів…": "Measuring the size of safe caches…",
    "Безпечних кешів для очищення немає": "No safe caches to clean",
    "Ще раз: очистити {size} старше {days} днів: {names}": "Click again: clean {size} older than {days} days: {names}",
    "Не вдалося очистити кеші": "Could not clean the caches",
    "Звільнено на диску: {freed:.0f} МБ{mem}": "Freed on disk: {freed:.0f} MB{mem}",
    ". RAM: доступно {avail:+d} МБ, swap {swap:+d} МБ": ". RAM: available {avail:+d} MB, swap {swap:+d} MB",
    "Має лишитися увімкненим хоча б один показник": "At least one metric must stay on",
    "Зафіксовано: {name}. Ще раз по назві — зняти вибір": "Pinned: {name}. Click the name again to clear",
    "Вибір знято": "Selection cleared",
    "Натисніть «Так?», щоб завершити {name}": "Click “Sure?” to quit {name}",
    "Немає доступу, щоб завершити {name}": "No permission to quit {name}",
    "Завершено: {name}": "Quit: {name}",
    "{name} вже не працює": "{name} is no longer running",
    "Процеси": "Processes",
    "За CPU (% одного ядра)": "By CPU (% of one core)",
    "За памʼяттю (RSS)": "By memory (RSS)",
    "Перевірити зараз": "Check now",
    "Вийти з MacSafe": "Quit MacSafe",
    "MacSafe: CPU · RAM · GPU, відсотки зайнятості": "MacSafe: CPU · RAM · GPU, percent in use",
    # ---- agent, notifications, cleaning, file search, Trash (agent.py, alerts.py, cleanup.py, engine.py, trash.py)
    ", доступно {avail:+d} МБ": ", available {avail:+d} MB",
    "Вимкнено з наступного входу, launchd не зняв зараз: {err}": "Turned off from the next login, launchd did not unload it now: {err}",
    "Вільна памʼять: {before}% → {after}%{extra}": "Free memory: {before}% → {after}%{extra}",
    "Записано, але launchd не запустив зараз: {err}. Діє з наступного входу.": "Saved, but launchd did not start it now: {err}. Takes effect at the next login.",
    "Збір даних": "Collecting data",
    "Не вдалося очистити памʼять": "Could not free memory",
    "Новий елемент автозапуску": "New login item",
    "Перевірка не вдалась": "Check failed",
    "Перевірка: {msg}": "Check: {msg}",
    "Перевірку завершено, звіт оновлено": "Check finished, report updated",
    "Вільно {free:.1f} ГБ. Спробуйте «Детальне очищення».": "{free:.1f} GB free. Try “Detailed clean”.",
    "Зайнято {ram:.0f}%. Закрийте важкі застосунки або відкрийте «Процеси».": "{ram:.0f}% in use. Close heavy apps or open “Processes”.",
    "Залишилось {pct:.0f}%.": "{pct:.0f}% left.",
    "Нова проблема безпеки": "New security issue",
    "Проблем у звіті: {issues} (було {was}).": "Issues in the report: {issues} (was {was}).",
    "{name}: пропущено, шлях веде за межі": "{name}: skipped, the path leads outside",
    "Категорію не вибрано": "Category not selected",
    "Кеш застосунку. Закрийте його перед очищенням.": "App cache. Quit the app before cleaning.",
    "Запущено. Закрийте і повторіть.": "Running. Quit it and try again.",
    "Не є локальною моделлю": "Not a local model",
    "Звільнено {mb} МБ (файли старші за {days} дн.)": "Freed {mb} MB (files older than {days} days)",
    "Команду не знайдено на цьому Mac": "The command was not found on this Mac",
    "Перевищено час виконання ({s} с)": "Timed out ({s} s)",
    "Невідома дія": "Unknown action",
    "Інша дія ще виконується": "Another action is still running",
    "Не знайдено команду ollama": "The ollama command was not found",
    "Захищено: тут можуть бути дані, які важко відновити.": "Protected: this may hold data that is hard to restore.",
    "Застосунок запущено. Закрийте його і повторіть.": "The app is running. Quit it and try again.",
    "Не вибрано жодного кешу": "No cache selected",
    "Недозволена або відсутня папка": "Folder not allowed or missing",
    "Неправильний ідентифікатор": "Invalid identifier",
    "MacSafe уже виконує перевірку в іншому вікні": "MacSafe is already running a check in another window",
    "macOS покаже запит один раз. Після «Дозволити» запит більше не з'являється.": "macOS shows the request once. After “Allow” it does not appear again.",
    "Готово": "Done",
    "Збір даних": "Collecting data",
    "Керування Finder: очистити кошик": "Finder control: empty the Trash",
    "Не вдалося запустити перевірку: {exc}": "Could not start the check: {exc}",
    "Не вдалося запустити потік: {exc}": "Could not start the thread: {exc}",
    "Не вдалося перегенерувати: {exc}": "Could not regenerate: {exc}",
    "Не вдалося показати у Finder": "Could not show in Finder",
    "Не знайдено {path}": "Not found: {path}",
    "Не є залишком застосунку": "Not a leftover of an app",
    "Недозволений шлях": "Path not allowed",
    "Неправильний ідентифікатор знімка": "Invalid snapshot identifier",
    "Нічого не вибрано": "Nothing selected",
    "Очищення памʼяті (purge)": "Memory cleanup (purge)",
    "Пароль не введено або дію скасовано": "Password not entered or action cancelled",
    "Перевірка вже виконується": "Check is already running",
    "Перевірка завершилась помилкою": "Check failed with an error",
    "Перевірку зупинено": "Check stopped",
    "Помилка регенерації": "Regeneration error",
    "Потрібен пароль адміністратора при кожному запуску. macOS не дає дозволити це наперед.": "An administrator password is needed each time. macOS does not allow granting this in advance.",
    "Потрібне підтвердження": "Confirmation required",
    "Звільнити памʼять…": "Free memory…",
    "Не вдалося перемістити в кошик": "Could not move to the Trash",
    "launchctl не вдався": "launchctl failed",
    "ок": "ok",
    "до відома": "info",
    "увага": "warning",
    "критично": "critical",
    "Немає доступу до системного кошика": "No access to the system Trash",
    "Кеші": "Caches",
    "Кеш пакетів npm. Завантажиться знову за потреби.": "npm package cache. Downloaded again when needed.",
    "Кеш пакетів pip.": "pip package cache.",
    "Кеш завантажень Homebrew.": "Homebrew download cache.",
    "Результати збірок. Xcode створить їх знову.": "Build results. Xcode recreates them.",
    "Логи застосунків. Старіші за 30 днів.": "App logs. Older than 30 days.",
    "Логи": "Logs",
    "Інструменти розробника": "Developer tools",
    "Кеші застосунків": "App caches",
    "Homebrew": "Homebrew",
    "Xcode DerivedData": "Xcode DerivedData",
    "Не вибрано жодного кешу": "No cache selected",
}
