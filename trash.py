"""Moving to the macOS Trash (restorable). Never deletes permanently.

The path must be an existing file or folder inside the home folder, not a symbolic link.
Uses NSFileManager.trashItemAtURL: no request for permission to control Finder.
"""
from i18n import T
import os

HOME = os.path.expanduser("~")


def safe_target(path):
    """The real path for moving to the Trash, or None. The home folder root and the Trash itself are not allowed."""
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
    """(ok, explanation). The file goes to the Trash and can be restored from there."""
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
