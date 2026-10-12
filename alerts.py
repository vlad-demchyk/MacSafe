"""MacSafe notifications: rules without AppKit, so they can be tested.

Each rule fires when the value stays above the threshold for HOLD seconds, and does not repeat earlier
than after COOLDOWN seconds. A new security rule fires when the number of issues grew relative to
the previous check.
"""
from i18n import T
import time

RAM_PERCENT = 92        # percent of memory in use, used only when the memory pressure is unknown
PRESSURE_WARN = 2       # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical
DISK_FREE_GB = 10       # the least free disk space that is always fine, GB
DISK_MAX_GB = 30        # the threshold never rises above this
BATTERY_LOW = 15        # charge percentage when not on power
HEAT_C = 90             # processor temperature, °C (history.HOT_C)
CPU_BUSY = 90           # system CPU load, percent
LONG_HOLD = 300.0       # heat and load are alerted only when they last this long
HOLD = 60.0             # how many seconds the condition must last
COOLDOWN = 3600.0       # the pause between repeats of the same notification


def disk_threshold(total_gb):
    """Free space (GB) below which to warn: 5% of the disk, at least 10 GB and at most 30 GB."""
    return min(max(DISK_FREE_GB, 0.05 * (total_gb or 0)), DISK_MAX_GB)


class Alerts:
    def __init__(self, clock=time.monotonic, culprit=None):
        self._clock = clock
        self._culprit = culprit   # () -> name of the app using the most CPU, or None; called only when the alert fires
        self._since = {}     # id → since when the condition holds
        self._last = {}      # id → when it was last shown
        self._issues = None  # number of security issues at the previous check

    def _fire(self, key, active, now, out, title, text, hold=HOLD):
        if not active:
            self._since.pop(key, None)
            return
        started = self._since.setdefault(key, now)
        if now - started < hold:
            return
        if now - self._last.get(key, -COOLDOWN) < COOLDOWN:
            return
        self._last[key] = now
        out.append((title, text))

    def check(self, snap, security=None):
        """List of notifications (title, text) to show now."""
        now = self._clock()
        out = []
        ramd = snap.get("ram") or {}
        ram, pressure = ramd.get("percent"), ramd.get("pressure")
        # The share of memory in use is high on a healthy Mac (caches), so the system's own pressure decides;
        # the percentage is only a fallback when macOS does not report it
        tight = pressure >= PRESSURE_WARN if pressure else (ram is not None and ram >= RAM_PERCENT)
        self._fire("ram", tight, now, out,
                   T("Памʼять майже вичерпана"), T("Зайнято {ram:.0f}%. Закрийте важкі застосунки або відкрийте «Процеси».", ram=ram) if ram else "")
        disk = snap.get("disk")
        low = disk is not None and disk["freeGb"] < disk_threshold(disk.get("totalGb"))
        self._fire("disk", low, now, out, T("Мало місця на диску"),
                   T("Вільно {free:.1f} ГБ. Спробуйте «Детальне очищення».", free=disk["freeGb"]) if low else "", hold=0)
        temp = snap.get("temp")
        self._fire("heat", temp is not None and temp >= HEAT_C, now, out, T("Процесор перегрівається"),
                   T("Температура {temp:.0f} °C вже кілька хвилин. Закрийте важкі застосунки й дайте Mac охолонути.", temp=temp) if temp else "",
                   hold=LONG_HOLD)
        cpu = snap.get("cpu")
        busy = cpu is not None and cpu >= CPU_BUSY
        before = len(out)
        self._fire("cpu", busy, now, out, T("Процесор довго навантажений"), T("Навантаження {cpu:.0f}% кілька хвилин.", cpu=cpu) if cpu else "",
                   hold=LONG_HOLD)
        if len(out) > before and self._culprit:
            name = self._culprit()
            if name:
                out[-1] = (out[-1][0], out[-1][1] + " " + T("Найбільше: {name}.", name=name))
        bat = snap.get("battery")
        flat = bat is not None and not bat["plugged"] and bat["percent"] <= BATTERY_LOW
        self._fire("battery", flat, now, out, T("Низький заряд батареї"),
                   T("Залишилось {pct:.0f}%.", pct=bat["percent"]) if flat else "", hold=0)
        if security is not None and security.get("level") != "none":
            issues = int(security.get("issues", 0))
            if self._issues is not None and issues > self._issues:
                out.append((T("Нова проблема безпеки"), T("Проблем у звіті: {issues} (було {was}).", issues=issues, was=self._issues)))
            self._issues = issues
        return out
