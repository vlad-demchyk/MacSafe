"""Menu bar settings: which numbers to show and in which style.

Stored as JSON in the app data folder. Writes are atomic (a temporary file + replace).
An invalid or damaged file does not break the launch: default values are used.
"""
import json
import os

DEFAULTS = {"cpu": True, "ram": True, "gpu": True, "style": "both", "interval": 2,
            "alerts": True, "dailyCheck": True, "language": "en", "graphTemp": True, "graphPower": False}
STYLES = ("both", "icon", "text")   # icon and numbers / icon only / numbers only
INTERVALS = (1, 2, 5)              # seconds between metric samples
LANGUAGES = ("uk", "en")   # English is the default


class Prefs:
    def __init__(self, path):
        self._path = path
        self._data = dict(DEFAULTS)
        self._load()

    def _load(self):
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for key in ("cpu", "ram", "gpu", "alerts", "dailyCheck", "graphTemp", "graphPower"):
            if isinstance(raw.get(key), bool):
                self._data[key] = raw[key]
        if raw.get("style") in STYLES:
            self._data["style"] = raw["style"]
        if raw.get("interval") in INTERVALS:
            self._data["interval"] = raw["interval"]
        if raw.get("language") in LANGUAGES:
            self._data["language"] = raw["language"]

    def get(self, key):
        return self._data[key]

    def set(self, key, value):
        if key in ("cpu", "ram", "gpu", "alerts", "dailyCheck", "graphTemp", "graphPower"):
            value = bool(value)
        elif key == "style":
            if value not in STYLES:
                raise ValueError(f"Unknown style: {value}")
        elif key == "interval":
            if value not in INTERVALS:
                raise ValueError(f"Invalid interval: {value}")
        elif key == "language":
            if value not in LANGUAGES:
                raise ValueError(f"Unknown language: {value}")
        else:
            raise KeyError(key)
        self._data[key] = value
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = f"{self._path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
