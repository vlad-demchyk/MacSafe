"""Живі метрики для статус-бару: CPU, RAM, GPU і процеси, згруповані за застосунком.

Без root. Усе, що недоступно без прав, пропускається або позначається як None (показується «н/д»).
Швидкі показники (CPU, RAM, GPU) оновлюються щосекунди і коштують десятки мікросекунд. Список процесів
(дорогий, близько 30 мс) скануємо лише коли меню або панель відкриті (detail=True): у закритому стані
агент не торкається процесів системи взагалі.
"""
import ctypes
import os
import plistlib
import re
import subprocess
import threading
import time

import psutil

from gpu_iokit import _ok as _IOKIT_OK, gpu_detail_iokit, gpu_percent_iokit, gpu_time_by_pid

APP_RE = re.compile(r"/([^/]+)\.app/")
VERSION_RE = re.compile(r"^\d+(\.\d+)+$")   # бінарники з назвою-версією, наприклад claude у …/claude/versions/2.1.296
SELF_NAME = "MacSafe"
SELF_PID = os.getpid()
GB = 1024 ** 3
MB = 1024 ** 2
GPU_KEYS = ("Device Utilization %", "GPU Activity(%)")   # основний ключ і фолбек (Stats/eul)


def gpu_percent():
    """Завантаження GPU, 0–100. Основний шлях — IOKit напряму (gpu_iokit), ioreg лише як запасний.

    None, якщо ключа немає. Для кількох GPU береться максимум.
    """
    if _IOKIT_OK:
        return gpu_percent_iokit()
    return _gpu_percent_ioreg()


def _gpu_percent_ioreg():
    """Запасний шлях: ioreg (IOAccelerator), XML-вивід. None, якщо ключа немає.

    Для кількох GPU береться максимум, а не сума. Драйвер може не віддавати ключ: тоді None («н/д»).
    """
    try:
        raw = subprocess.run(
            ["/usr/sbin/ioreg", "-a", "-r", "-d", "1", "-c", "IOAccelerator"],
            capture_output=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_gpu(raw)


def parse_gpu(raw):
    """Розбір XML-виводу ioreg: максимум серед GPU, або None. Окремо від запуску, щоб тестувати."""
    try:
        devices = plistlib.loads(raw) if raw else []
    except (plistlib.InvalidFileException, ValueError, TypeError):
        return None
    vals = []
    for dev in devices if isinstance(devices, list) else []:
        stats = dev.get("PerformanceStatistics") if isinstance(dev, dict) else None
        if not isinstance(stats, dict):
            continue
        for key in GPU_KEYS:
            v = stats.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(min(max(float(v), 0.0), 100.0))
                break
    return max(vals) if vals else None


def bundle_path(exe):
    """Шлях до .app, якщо процес із застосунку; інакше None. Звідси береться системна іконка застосунку."""
    m = APP_RE.search(exe or "")
    return exe[: m.end() - 1] if m else None


def _group_name(name, exe):
    """Ім'я для групування: назва .app, якщо процес із застосунку; інакше назва процесу."""
    if exe:
        m = APP_RE.search(exe)
        if m:
            return m.group(1)
        base = os.path.basename(exe)
        if VERSION_RE.match(base):
            # Бінарник названий версією: беремо назву папки продукту (…/claude/versions/2.1.296)
            return os.path.basename(os.path.dirname(os.path.dirname(exe))) or name or "?"
    return name or "?"


def killable(name, pids):
    """Чи можна завершити групу процесів кнопкою «×»: є власні процеси користувача, і це не сам MacSafe."""
    return bool(pids) and name != SELF_NAME


_GPU_PREV = {"at": None, "ns": None}   # попередній знімок накопиченого часу GPU по PID


def _gpu_percent_by_pid(now):
    """GPU % по PID: приріст накопиченого часу GPU за інтервал, у % від реального часу. None, якщо невідомо.

    Перший виклик лише запам'ятовує базу. Значення процесу — сума по його клієнтах GPU, не більше 100.
    """
    ns = gpu_time_by_pid()
    prev_at, prev_ns = _GPU_PREV["at"], _GPU_PREV["ns"]
    _GPU_PREV["at"], _GPU_PREV["ns"] = now, ns
    if ns is None or prev_ns is None or now <= prev_at:
        return None
    wall_ns = (now - prev_at) * 1e9
    return {pid: min(100.0, 100.0 * max(0, total - prev_ns.get(pid, total)) / wall_ns)
            for pid, total in ns.items()}


def take_processes():
    """Групи процесів: CPU (% одного ядра, як у Activity Monitor), RSS і PID власних процесів.

    killable — True, якщо в групі є хоча б один процес поточного користувача (його можна завершити
    без root). Сам MacSafe не пропонує завершити себе. Повертає (topCpu, topRam, усі групи): усі групи
    потрібні, щоб зафіксований рядок показував значення, навіть коли процес випав з топ-списку.
    """
    uid = os.getuid()
    groups = {}
    for p in psutil.process_iter(["name"]):
        try:
            with p.oneshot():
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss
                name = p.info.get("name") or ""
                try:
                    exe = p.exe()
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    exe = ""
                try:
                    owned = p.uids().real == uid
                except (psutil.AccessDenied, psutil.ZombieProcess):
                    owned = False
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
        key = SELF_NAME if p.pid == SELF_PID else _group_name(name, exe)
        g = groups.setdefault(key, {"name": key, "cpu": 0.0, "rss": 0, "count": 0, "pids": [], "all": [],
                                    "bundle": None})
        g["all"].append(p.pid)
        if g["bundle"] is None and exe:
            g["bundle"] = bundle_path(exe)
        g["cpu"] += cpu
        g["rss"] += rss
        g["count"] += 1
        if owned and p.pid != SELF_PID:
            g["pids"].append(p.pid)
    rows = list(groups.values())
    gpu_pct = _gpu_percent_by_pid(time.monotonic())
    for g in rows:
        g["killable"] = killable(g["name"], g["pids"])
        g["gpu"] = None if gpu_pct is None else min(100.0, sum(gpu_pct.get(pid, 0.0) for pid in g["all"]))
    top_cpu = sorted(rows, key=lambda g: (-g["cpu"], -g["rss"]))[:12]
    top_ram = sorted(rows, key=lambda g: -g["rss"])[:5]
    top_gpu = sorted((g for g in rows if g["gpu"]), key=lambda g: -g["gpu"])[:5]
    return top_cpu, top_ram, top_gpu, rows


_CLUSTER_RE = re.compile(r'"cluster-type" = <"([PE])">')
_LOGICAL_RE = re.compile(r'"logical-cpu-id" = (\d+)')
_GPU_CORES_RE = re.compile(r'"gpu-core-count" = (\d+)')


def parse_core_types(text):
    """Тип кожного логічного ядра з виводу ioreg: «P» — продуктивне, «E» — економне. Порожній список, якщо даних немає.

    Кожен вузол ioreg має свій cluster-type і logical-cpu-id, тому пара беремо в межах одного вузла.
    """
    types = {}
    node_type, node_cpu = None, None
    for line in text.splitlines():
        if "+-o " in line:   # початок нового вузла дерева
            if node_cpu is not None and node_type is not None:
                types[node_cpu] = node_type
            node_type, node_cpu = None, None
        cluster = _CLUSTER_RE.search(line)
        if cluster:
            node_type = cluster.group(1)
        logical = _LOGICAL_RE.search(line)
        if logical:
            node_cpu = int(logical.group(1))
    if node_cpu is not None and node_type is not None:
        types[node_cpu] = node_type
    return [types[i] for i in sorted(types)] if types else []


_HW = {}


def parse_gpu_cores(text):
    """Кількість ядер GPU з виводу ioreg (ключ gpu-core-count). None, якщо немає."""
    match = _GPU_CORES_RE.search(text)
    return int(match.group(1)) if match else None


def hardware_info():
    """Незмінні дані про залізо, читаються раз: типи ядер P/E, ядра GPU, назва чіпа.

    ioreg важкий, тому з його виводу беремо лише потрібні значення, а сирий текст не зберігаємо.
    """
    if not _HW:
        types, gpu_cores, chip = [], None, ""
        try:
            raw = subprocess.run(["/usr/sbin/ioreg", "-l", "-w0"], capture_output=True, timeout=10).stdout
            text = raw.decode("utf-8", "replace")
            types = parse_core_types(text)
            gpu_cores = parse_gpu_cores(text)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            chip = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"],
                                  capture_output=True, timeout=5).stdout.decode("utf-8", "replace").strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        _HW.update({"coreTypes": types, "gpuCores": gpu_cores, "chip": chip})
    return _HW


def core_types():
    """Типи ядер P/E. Порожній список, якщо macOS не віддала тип; тоді всі ядра однаково блакитні."""
    return hardware_info()["coreTypes"]


_LIBC = {"lib": None}


def _sysctl_u64(name):
    """Значення sysctl у 64 біти (наприклад, розмір стиснутої пам'яті). None, якщо немає."""
    try:
        if _LIBC["lib"] is None:
            _LIBC["lib"] = ctypes.CDLL(None)
        value = ctypes.c_uint64(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        if _LIBC["lib"].sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
            return None
        return value.value
    except (OSError, AttributeError):
        return None


def _kind_avg(loads, kinds, kind):
    """Середнє навантаження ядер заданого типу (P або E). None, якщо типів немає."""
    values = [v for v, k in zip(loads, kinds) if k == kind]
    return sum(values) / len(values) if values else None


def gpu_detail():
    """GPU: device, renderer, tiler (0–100 або None). None, якщо немає даних узагалі."""
    if _IOKIT_OK:
        return gpu_detail_iokit()
    value = _gpu_percent_ioreg()
    return {"device": value, "renderer": None, "tiler": None} if value is not None else None


_NET = {"at": None, "rx": None, "tx": None}


def net_rates(now=None):
    """Швидкість мережі, байт/с: (вхідна, вихідна). None, поки немає попереднього знімка або лічильників."""
    now = time.monotonic() if now is None else now
    try:
        c = psutil.net_io_counters()
    except Exception:
        return None
    prev_at, prev_rx, prev_tx = _NET["at"], _NET["rx"], _NET["tx"]
    _NET.update(at=now, rx=c.bytes_recv, tx=c.bytes_sent)
    if prev_at is None or now <= prev_at or c.bytes_recv < prev_rx or c.bytes_sent < prev_tx:
        return None
    dt = now - prev_at
    return ((c.bytes_recv - prev_rx) / dt, (c.bytes_sent - prev_tx) / dt)


def disk_info():
    """Диск користувача: вільно і всього, ГБ, відсоток зайнятого. None, якщо не вдалося."""
    try:
        d = psutil.disk_usage(os.path.expanduser("~"))
    except Exception:
        return None
    return {"freeGb": d.free / GB, "totalGb": d.total / GB, "percent": d.percent}


def battery_info():
    """Батарея: відсоток і чи живиться від мережі. None на Mac без батареї."""
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b is None:
        return None
    return {"percent": float(b.percent), "plugged": bool(b.power_plugged)}


def take_quick():
    """Швидкі показники для головної панелі і зон: CPU, RAM, swap, GPU і деталі. Кілька мілісекунд плюс IOKit."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    times = psutil.cpu_times_percent(interval=None)
    per_core = psutil.cpu_percent(interval=None, percpu=True)
    kinds = core_types()
    compressed = _sysctl_u64("vm.compressor_bytes_used") or 0
    gpu = gpu_detail()
    return {
        "time": time.time(),
        "cpu": psutil.cpu_percent(interval=None),
        "perCore": per_core,
        "cores": psutil.cpu_count(),
        "cpuTimes": {"user": times.user, "system": times.system, "idle": times.idle},
        "pAvg": _kind_avg(per_core, kinds, "P"),
        "eAvg": _kind_avg(per_core, kinds, "E"),
        "uptime": time.time() - psutil.boot_time(),
        "loadavg": os.getloadavg(),
        "ram": {
            "percent": vm.percent,
            "usedGb": round((vm.total - vm.available) / GB, 1),
            "totalGb": round(vm.total / GB, 1),
            "swapGb": round(sw.used / GB, 1),
        },
        "memory": {
            "totalGb": vm.total / GB,
            "freeGb": vm.free / GB,
            "wiredGb": vm.wired / GB,
            "compressedGb": compressed / GB,
            "appGb": max(0, vm.total - vm.free - vm.wired - compressed) / GB,
            "swapGb": sw.used / GB,
        },
        "gpu": gpu["device"] if gpu else None,
        "gpuDetail": gpu,
        "disk": disk_info(),
        "net": net_rates(),
        "battery": battery_info(),
    }


class Monitor:
    """Фоновий потік: швидкі показники щосекунди; список процесів лише коли detail=True.

    Коли detail вмикається, перший прохід по процесах лише задає базу для CPU (psutil рахує % від попереднього
    виклику), його результат відкидаємо. Наступний цикл уже дає справжні значення. Коли detail вимкнено,
    сканування зупиняється, а старий список очищується.
    Зміна detail з головного потоку безпечна: читання і запис під замком, пробудження через подію.
    """

    def __init__(self, interval=1.0):
        self._interval = interval
        self._guard = threading.Lock()
        self._latest = None
        self._detail = False
        self._primed = False
        self._top_cpu = []
        self._top_ram = []
        self._top_gpu = []
        self._groups = []
        self._stop = threading.Event()
        self._wake = threading.Event()   # будить цикл одразу, коли відкрили список процесів
        self._thread = None

    def start(self):
        # Прайм системного CPU: перший cpu_percent завжди 0, тому робимо виклик до першого циклу
        psutil.cpu_percent(interval=None)
        core_types()   # типи ядер читаємо до показу панелі, а не в момент відкриття
        self._thread = threading.Thread(target=self._loop, name="audit-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def interval(self):
        """Інтервал вибірок: відкрита панель — 1 с, закрита — як у налаштуваннях."""
        with self._guard:
            detail = self._detail
        return min(self._interval, 1.0) if detail else self._interval

    def set_interval(self, seconds):
        """Інтервал між вибірками, секунд. Діє з наступного циклу."""
        if seconds and seconds > 0:
            self._interval = float(seconds)

    def set_detail(self, on):
        # Останній список лишається при закритті: після повторного відкриття він одразу видно
        with self._guard:
            self._detail = bool(on)
            self._primed = False
        if on:
            self._wake.set()

    def latest(self):
        with self._guard:
            return self._latest

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            prime = False
            ready = True
            try:
                snap = take_quick()
                with self._guard:
                    detail, primed = self._detail, self._primed
                if detail and not primed:
                    # База для CPU і GPU: RAM і список оновлюються одразу, CPU і GPU — з наступного проходу
                    _, top_ram, _, groups = take_processes()
                    prime = True
                    ready = False
                    with self._guard:
                        self._primed = True
                        self._top_ram, self._groups = top_ram, groups
                elif detail:
                    top_cpu, top_ram, top_gpu, groups = take_processes()
                    with self._guard:
                        self._top_cpu, self._top_ram, self._top_gpu, self._groups = top_cpu, top_ram, top_gpu, groups
                with self._guard:
                    snap["topCpu"] = self._top_cpu
                    snap["topRam"] = self._top_ram
                    snap["topGpu"] = self._top_gpu
                    snap["groups"] = self._groups
                    snap["procReady"] = ready
                    self._latest = snap
            except Exception:
                pass
            # Після праймінгу чекаємо повний інтервал, без пробудження: інакше справжній замір піде через мілісекунди
            (self._stop if prime else self._wake).wait(min(self._interval, 1.0) if prime else self.interval)
