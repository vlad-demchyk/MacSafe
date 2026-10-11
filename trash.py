"""Переміщення в кошик macOS (відновлюване). Нічого не видаляє назавжди.

Шлях має бути існуючим файлом або папкою всередині домашньої папки, не символічним посиланням.
Використовується NSFileManager.trashItemAtURL: без запиту дозволу на керування Finder.
"""
from i18n import T
import os

HOME = os.path.expanduser("~")


def safe_target(path):
    """Реальний шлях для переміщення в кошик або None. Корінь домашньої папки і сам кошик не дозволені."""
    if not isinstance(path, str) or not os.path.isabs(path) or "\0" in path:
        return None
    if os.path.islink(path) or not os.path.exists(path):
        return None
    real = os.path.realpath(path)
    home = os.path.realpath(HOME)
    if real != os.path.abspath(path) or not real.startswith(home + os.sep):
        return None
    if real.startswith(os.path.join(home, ".Trash") + os.sep):
        return None
    return real


def move_to_trash(path):
    """(ok, пояснення). Файл потрапляє в кошик і його можна повернути звідти."""
    real = safe_target(path)
    if real is None:
        return False, T("Недозволений шлях")
    try:
        from Foundation import NSFileManager, NSURL
    except ImportError:
        return False, T("Немає доступу до системного кошика")
    url = NSURL.fileURLWithPath_(real)
    ok, _result, error = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
    if ok:
        return True, ""
    return False, str(error.localizedDescription()) if error is not None else T("Не вдалося перемістити в кошик")
