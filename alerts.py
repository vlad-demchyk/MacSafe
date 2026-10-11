"""Сповіщення MacSafe: правила без AppKit, щоб їх можна було тестувати.

Кожне правило спрацьовує, коли значення тримається вище порогу HOLD секунд, і не повторюється раніше
ніж через COOLDOWN секунд. Нове правило безпеки спрацьовує, коли кількість проблем зросла відносно
попереднього перегляду.
"""
from i18n import T
import time

RAM_PERCENT = 92        # відсоток зайнятої памʼяті
DISK_FREE_GB = 10       # вільне місце на диску, ГБ
BATTERY_LOW = 15        # відсоток заряду без живлення від мережі
HOLD = 60.0             # скільки секунд умова має тривати
COOLDOWN = 3600.0       # пауза між повторами одного сповіщення


class Alerts:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._since = {}     # id → з якого моменту умова триває
        self._last = {}      # id → коли востаннє показували
        self._issues = None  # кількість проблем безпеки при попередньому перегляді

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
        """Список сповіщень (заголовок, текст), які треба показати зараз."""
        now = self._clock()
        out = []
        ram = (snap.get("ram") or {}).get("percent")
        self._fire("ram", ram is not None and ram >= RAM_PERCENT, now, out,
                   "Памʼять майже вичерпана", T("Зайнято {ram:.0f}%. Закрийте важкі застосунки або відкрийте «Процеси».", ram=ram) if ram else "")
        disk = snap.get("disk")
        low = disk is not None and disk["freeGb"] < DISK_FREE_GB
        self._fire("disk", low, now, out, "Мало місця на диску",
                   T("Вільно {free:.1f} ГБ. Спробуйте «Детальне очищення».", free=disk["freeGb"]) if low else "", hold=0)
        bat = snap.get("battery")
        flat = bat is not None and not bat["plugged"] and bat["percent"] <= BATTERY_LOW
        self._fire("battery", flat, now, out, "Низький заряд батареї",
                   T("Залишилось {pct:.0f}%.", pct=bat["percent"]) if flat else "", hold=0)
        if security is not None and security.get("level") != "none":
            issues = int(security.get("issues", 0))
            if self._issues is not None and issues > self._issues:
                out.append((T("Нова проблема безпеки"), T("Проблем у звіті: {issues} (було {was}).", issues=issues, was=self._issues)))
            self._issues = issues
        return out
