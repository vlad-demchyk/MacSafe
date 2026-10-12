"""Live metrics for the menu bar: CPU, RAM, GPU and processes, grouped by app.

No root. Anything unavailable without rights is skipped or marked None (shown as “n/a”).
Quick metrics (CPU, RAM, GPU) update every second and cost tens of microseconds. The process list
(expensive, about 30 ms) we scan only when the menu or panel is open (detail=True): in the closed state
the agent does not touch system processes at all.
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
VERSION_RE = re.compile(r"^\d+(\.\d+)+$")   # binaries named by version, for example claude in …/claude/versions/2.1.296
SELF_NAME = "MacSafe"
SELF_PID = os.getpid()
GB = 1024 ** 3
MB = 1024 ** 2
SAVING_INTERVAL = 5.0   # seconds between samples with the panel closed on battery or in Low Power Mode
GPU_KEYS = ("Device Utilization %", "GPU Activity(%)")   # the main key and the fallback (Stats/eul)


def gpu_percent():
    """GPU load, 0–100. The main path is IOKit directly (gpu_iokit), ioreg only as a fallback.

    None if the key is missing. For several GPUs the maximum is taken.
    """
    if _IOKIT_OK:
        return gpu_percent_iokit()
    return _gpu_percent_ioreg()


def _gpu_percent_ioreg():
    """Fallback path: ioreg (IOAccelerator), XML output. None if the key is missing.

    For several GPUs the maximum is taken, not the sum. The driver may not return the key: then None (“n/a”).
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
    """Parsing the XML output of ioreg: the maximum among GPUs, or None. Separate from the run so it can be tested."""
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
    """Path to the .app if the process belongs to an app; otherwise None. The system app icon is taken from here."""
    m = APP_RE.search(exe or "")
    return exe[: m.end() - 1] if m else None


def _group_name(name, exe):
    """Name for grouping: the .app name if the process belongs to an app; otherwise the process name."""
    if exe:
        m = APP_RE.search(exe)
        if m:
            return m.group(1)
        base = os.path.basename(exe)
        if VERSION_RE.match(base):
            # The binary is named by its version: we take the product folder name (…/claude/versions/2.1.296)
            return os.path.basename(os.path.dirname(os.path.dirname(exe))) or name or "?"
    return name or "?"


def killable(name, pids):
    """Whether the process group can be quit with the “×” button: it has the user's own processes, and it is not MacSafe itself."""
    return bool(pids) and name != SELF_NAME


_GPU_PREV = {"at": None, "ns": None}   # the previous snapshot of accumulated GPU time per PID


def _gpu_percent_by_pid(now):
    """GPU % per PID: the increase of accumulated GPU time over the interval, as a % of real time. None if unknown.

    The first call only stores the baseline. A process value is the sum over its GPU clients, at most 100.
    """
    ns = gpu_time_by_pid()
    prev_at, prev_ns = _GPU_PREV["at"], _GPU_PREV["ns"]
    _GPU_PREV["at"], _GPU_PREV["ns"] = now, ns
    if ns is None or prev_ns is None or now <= prev_at:
        return None
    wall_ns = (now - prev_at) * 1e9
    return {pid: min(100.0, 100.0 * max(0, total - prev_ns.get(pid, total)) / wall_ns)
            for pid, total in ns.items()}


_STATIC = {}   # pid -> (start time, name, exe, owned by the user); the start time guards against pid reuse


def take_processes():
    """Process groups: CPU (% of one core, as in Activity Monitor), RSS and PIDs of their own processes.

    killable — True if the group has at least one process of the current user (it can be quit
    without root). MacSafe itself does not offer to quit itself. Returns (topCpu, topRam, all groups): all groups
    needed so that a pinned row shows values even when the process has dropped out of the top list.
    """
    uid = os.getuid()
    groups = {}
    seen = set()
    for p in psutil.process_iter():
        try:
            with p.oneshot():
                cpu = p.cpu_percent(None)
                rss = p.memory_info().rss
                born = p.create_time()
                cached = _STATIC.get(p.pid)
                if cached is None or cached[0] != born:
                    # name, path and owner do not change while the process lives: read them once
                    name = p.name() or ""
                    try:
                        exe = p.exe()
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        exe = ""
                    try:
                        owned = p.uids().real == uid
                    except (psutil.AccessDenied, psutil.ZombieProcess):
                        owned = False
                    cached = _STATIC[p.pid] = (born, name, exe, owned)
                _, name, exe, owned = cached
        except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
            continue
        seen.add(p.pid)
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
    for pid in set(_STATIC) - seen:
        del _STATIC[pid]
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
    """The type of each logical core from the ioreg output: “P” — performance, “E” — efficiency. An empty list if there is no data.

    Each ioreg node has its own cluster-type and logical-cpu-id, so we take the pair within one node.
    """
    types = {}
    node_type, node_cpu = None, None
    for line in text.splitlines():
        if "+-o " in line:   # the start of a new tree node
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
    """GPU core count from the ioreg output (key gpu-core-count). None if missing."""
    match = _GPU_CORES_RE.search(text)
    return int(match.group(1)) if match else None


def hardware_info():
    """Unchanging hardware data, read once: P/E core types, GPU cores, chip name.

    ioreg is heavy, so we take only the needed values from its output and do not keep the raw text.
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
    """P/E core types. An empty list if macOS did not return the type; then all cores are the same blue."""
    return hardware_info()["coreTypes"]


_LIBC = {"lib": None}


def _sysctl_u64(name):
    """sysctl value as 64-bit (for example, the size of compressed memory). None if missing."""
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
    """Average load of cores of the given type (P or E). None if there are no types."""
    values = [v for v, k in zip(loads, kinds) if k == kind]
    return sum(values) / len(values) if values else None


def gpu_detail():
    """GPU: device, renderer, tiler (0–100 or None). None if there is no data at all."""
    if _IOKIT_OK:
        return gpu_detail_iokit()
    value = _gpu_percent_ioreg()
    return {"device": value, "renderer": None, "tiler": None} if value is not None else None


_NET = {"at": None, "rx": None, "tx": None}


def net_rates(now=None):
    """Network speed, bytes/s: (incoming, outgoing). None until there is a previous snapshot or counters."""
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


_DISK_IO = {"at": None, "read": None, "write": None}


def disk_rates(now=None):
    """Disk speed, bytes/s: (read, write). None until there is a previous snapshot or counters."""
    now = time.monotonic() if now is None else now
    try:
        c = psutil.disk_io_counters()
    except Exception:
        return None
    if c is None:
        return None
    prev_at, prev_r, prev_w = _DISK_IO["at"], _DISK_IO["read"], _DISK_IO["write"]
    _DISK_IO.update(at=now, read=c.read_bytes, write=c.write_bytes)
    if prev_at is None or now <= prev_at or c.read_bytes < prev_r or c.write_bytes < prev_w:
        return None
    dt = now - prev_at
    return ((c.read_bytes - prev_r) / dt, (c.write_bytes - prev_w) / dt)


def disk_info():
    """User disk: free and total, GB, percent in use. None if it failed."""
    try:
        d = psutil.disk_usage(os.path.expanduser("~"))
    except Exception:
        return None
    return {"freeGb": d.free / GB, "totalGb": d.total / GB, "percent": d.percent}


def battery_info():
    """Battery: percentage and whether it is on power. None on a Mac without a battery."""
    try:
        b = psutil.sensors_battery()
    except Exception:
        return None
    if b is None:
        return None
    return {"percent": float(b.percent), "plugged": bool(b.power_plugged)}


def take_quick():
    """Quick metrics for the main panel and zones: CPU, RAM, swap, GPU and details. A few milliseconds plus IOKit."""
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
            "pressure": _sysctl_u64("kern.memorystatus_vm_pressure_level"),   # 1 normal, 2 warning, 4 critical
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
        "diskIo": disk_rates(),
        "battery": battery_info(),
    }


class Monitor:
    """Background thread: quick metrics every second; the process list only when detail=True.

    When detail is switched on, the first pass over processes only sets the CPU baseline (psutil counts % from the previous
    call), its result is discarded. The next cycle already gives real values. When detail is turned off,
    the scan stops, and the old list is cleared.
    Changing detail from the main thread is safe: reads and writes under a lock, waking through an event.
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
        self._wake = threading.Event()   # wakes the cycle at once when the process list is opened
        self._thread = None

    def _temperature(self):
        """The processor temperature in °C from the HID sensors (no root), or None.

        Reading the sensors costs about 30 ms and the value moves slowly, so it is read at most every 3 seconds.
        """
        now = time.time()
        if now - getattr(self, "_temp_at", 0) >= 3:
            self._temp_at = now
            try:
                import thermal_hid
                self._temp_last = (thermal_hid.read() or {}).get("cpu")
            except Exception:
                self._temp_last = None
        return getattr(self, "_temp_last", None)

    def _power(self):
        """{"sysW", "batW"} of the battery sensor: every second through IOKit (cheap), every 5 seconds when it falls back to ioreg."""
        now = time.time()
        try:
            import gpu_iokit
            every = 1 if gpu_iokit._ok else 5
        except Exception:
            every = 5
        if now - getattr(self, "_power_at", 0) >= every:
            self._power_at = now
            try:
                import history
                self._power_last = history.battery_power() or None
            except Exception:
                self._power_last = None
        return getattr(self, "_power_last", None)

    def _netinfo(self, detail):
        """Address and Wi-Fi quality, read every 10 seconds and only while the panel is open."""
        now = time.time()
        if detail and now - getattr(self, "_net_at", 0) >= 10:
            self._net_at = now
            try:
                import netinfo
                self._net_last = netinfo.read()
            except Exception:
                self._net_last = None
        return getattr(self, "_net_last", None)

    def start(self):
        # Priming the system CPU: the first cpu_percent is always 0, so we make a call before the first cycle
        psutil.cpu_percent(interval=None)
        core_types()   # core types are read before the panel is shown, not at the moment of opening
        self._thread = threading.Thread(target=self._loop, name="audit-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    @property
    def interval(self):
        """Sampling interval: open panel — 1 s, closed — as in the settings.

        With the panel closed on battery power or in Low Power Mode nobody watches the numbers, so the pace drops
        to SAVING_INTERVAL (unless the setting is already slower).
        """
        with self._guard:
            detail = self._detail
            latest = self._latest
        if detail:
            return min(self._interval, 1.0)
        return max(self._interval, SAVING_INTERVAL) if self._saving(latest) else self._interval

    def _saving(self, snap):
        """True on battery power or in Low Power Mode. The mode is asked from the system at most every 30 s."""
        battery = (snap or {}).get("battery")
        if battery and not battery.get("plugged"):
            return True
        now = time.time()
        if now - getattr(self, "_lpm_at", 0) >= 30:
            self._lpm_at = now
            try:
                import Foundation
                self._lpm = bool(Foundation.NSProcessInfo.processInfo().isLowPowerModeEnabled())
            except Exception:
                self._lpm = False
        return getattr(self, "_lpm", False)

    def set_interval(self, seconds):
        """Interval between samples, in seconds. Takes effect from the next cycle."""
        if seconds and seconds > 0:
            self._interval = float(seconds)

    def set_detail(self, on):
        # The last list stays on close: after reopening it is visible at once
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
                    # Baseline for CPU and GPU: RAM and the list update at once, CPU and GPU from the next pass
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
                    snap["temp"] = self._temperature()
                    snap["power"] = self._power()
                    snap["netinfo"] = self._netinfo(detail)
                    self._latest = snap
            except Exception:
                pass
            # After priming we wait the full interval, without waking up: otherwise the real measurement would come after milliseconds
            (self._stop if prime else self._wake).wait(min(self._interval, 1.0) if prime else self.interval)
