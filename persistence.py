"""Login item monitoring: the list of LaunchAgents and LaunchDaemons, the difference from the previous snapshot.

Only plist files in three system folders are read, without root and Full Disk Access. The first snapshot
is saved silently as the baseline. Changes nothing in the system.
"""
import json
import os
import plistlib

HOME = os.path.expanduser("~")
DIRS = (
    os.path.join(HOME, "Library", "LaunchAgents"),
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
)


def scan(dirs=DIRS):
    """{plist path: {label, program}}. Damaged files take the file name as the label."""
    found = {}
    for folder in dirs:
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".plist"))
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            label, program = name[:-6], ""
            try:
                with open(path, "rb") as fh:
                    data = plistlib.load(fh)
                if isinstance(data, dict):
                    label = str(data.get("Label") or label)
                    args = data.get("ProgramArguments")
                    program = str(data.get("Program") or (args[0] if isinstance(args, list) and args else ""))
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
            found[path] = {"label": label, "program": program}
    return found


def added(old, new):
    """New items: [{path, label, program}] in new that were not in old."""
    return [dict(path=p, **v) for p, v in new.items() if p not in old]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def check(state_path, dirs=DIRS):
    """Compares with the baseline snapshot and updates it. Returns the list of new items (empty on the first run)."""
    current = scan(dirs)
    previous = load(state_path)
    save(state_path, current)
    return [] if previous is None else added(previous, current)
