"""History of temperatures and power: one sample a minute, kept for a week, summarised for the report.

A sample is one line of JSON in history.jsonl (next to the other app data):
    {"t": epoch, "cpu": °C, "ssd": °C, "bat": battery °C, "pct": battery %, "sysW": system power in W, "batW": battery power in W
     (negative while discharging)}
Only the fields that could be read are present. Nothing here needs root.

    python3 history.py summary [hours]     prints key=value lines for the report script
    python3 history.py series [hours]      prints a JSON array of 5-minute points for the charts
"""
import json
import os
import re
import subprocess
import sys
import time

DATA_DIR = os.environ.get("AUDIT_DATA_DIR") or os.path.join(os.path.expanduser("~"), "Library", "Application Support", "MacSafe")
FILE = os.path.join(DATA_DIR, "history.jsonl")
KEEP_SECONDS = 7 * 86400
SAMPLE_EVERY = 60
HOT_C = 90            # a die temperature that counts as hot
EXTREME_C = 100       # close to the point where Apple Silicon throttles
HOT_MINUTES_WARN = 45   # hot for this long within the window: a warning
EXTREME_MINUTES_CRIT = 5


def _signed64(value):
    return value - (1 << 64) if value >= (1 << 63) else value


def battery_power():
    """{"sysW", "batW"} from AppleSmartBattery (SystemLoad in mW, voltage * amperage), or {}."""
    try:
        import gpu_iokit
        raw = gpu_iokit.battery_iokit()
    except Exception:
        raw = None
    if raw:
        result = {}
        if "load" in raw:
            result["sysW"] = round(raw["load"] / 1000, 2)
        if "volt" in raw and "amp" in raw:
            result["batW"] = round(raw["volt"] / 1000 * raw["amp"] / 1000, 2)
        if "pct" in raw:
            result["pct"] = raw["pct"]
        return result
    try:
        out = subprocess.run(["/usr/sbin/ioreg", "-rn", "AppleSmartBattery"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result = {}
    load = re.search(r'"SystemLoad"=(\d+)', out)
    if load:
        result["sysW"] = round(int(load.group(1)) / 1000, 2)
    volt = re.search(r'^\s*\|\s*"Voltage" = (\d+)', out, re.M)
    amp = re.search(r'^\s*\|\s*"Amperage" = (\d+)', out, re.M)
    if volt and amp:
        result["batW"] = round(int(volt.group(1)) / 1000 * _signed64(int(amp.group(1))) / 1000, 2)
    pct = re.search(r'^\s*\|\s*"CurrentCapacity" = (\d+)', out, re.M)
    if pct:
        result["pct"] = int(pct.group(1))
    return result


def take_sample(now=None):
    """One sample from the sensors and the battery. Missing readings are left out."""
    sample = {"t": int(now if now is not None else time.time())}
    try:
        import thermal_hid
        temps = thermal_hid.read() or {}
    except Exception:
        temps = {}
    for src, dst in (("cpu", "cpu"), ("ssd", "ssd"), ("battery", "bat")):
        if src in temps:
            sample[dst] = temps[src]
    sample.update(battery_power())
    return sample


def append(sample, path=FILE, now=None):
    """Adds the sample and drops what is older than a week (only when the file grows past a day's worth of margin)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sample, separators=(",", ":")) + "\n")
    cutoff = (now if now is not None else time.time()) - KEEP_SECONDS
    try:
        if os.path.getsize(path) > 1_500_000:   # ~10 000 samples; trimmed rarely, not on every write
            rows = [r for r in load(0, path) if r.get("t", 0) >= cutoff]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            os.replace(tmp, path)
    except OSError:
        pass


def load(hours=24, path=FILE, now=None):
    """The samples of the last `hours` (0 = all), oldest first. A broken line is skipped."""
    since = 0 if not hours else (now if now is not None else time.time()) - hours * 3600
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("t"), (int, float)) and row["t"] >= since:
                    rows.append(row)
    except OSError:
        pass
    return rows


def summarize(rows, step=SAMPLE_EVERY):
    """Peaks and how long the processor was hot: minutes at or above HOT_C and EXTREME_C, from the samples that had a reading."""
    cpu = [r["cpu"] for r in rows if isinstance(r.get("cpu"), (int, float))]
    if not cpu:
        return {}
    minutes = lambda limit: round(sum(1 for c in cpu if c >= limit) * step / 60)
    out = {"samples": len(cpu), "maxCpu": round(max(cpu), 1), "avgCpu": round(sum(cpu) / len(cpu), 1),
           "hotMinutes": minutes(HOT_C), "extremeMinutes": minutes(EXTREME_C)}
    ssd = [r["ssd"] for r in rows if isinstance(r.get("ssd"), (int, float))]
    if ssd:
        out["maxSsd"] = round(max(ssd), 1)
    watts = [r["sysW"] for r in rows if isinstance(r.get("sysW"), (int, float))]
    if watts:
        out["maxW"] = round(max(watts), 1)
        out["avgW"] = round(sum(watts) / len(watts), 1)
    return out


def verdict(summary):
    """'critical', 'warning', 'info' or '' by how high and how long the processor was hot. A single peak is only 'info'."""
    if not summary:
        return ""
    if summary.get("extremeMinutes", 0) >= EXTREME_MINUTES_CRIT:
        return "critical"
    if summary.get("hotMinutes", 0) >= HOT_MINUTES_WARN:
        return "warning"
    if summary.get("maxCpu", 0) >= HOT_C:
        return "info"
    return ""


def series(rows, bucket=300):
    """Points for a chart: the average of each `bucket` seconds, as {t, cpu, ssd, bat, w, batW, pct}."""
    buckets = {}
    for r in rows:
        buckets.setdefault(int(r["t"] // bucket), []).append(r)
    points = []
    for key in sorted(buckets):
        group = buckets[key]
        point = {"t": key * bucket}
        for src, dst in (("cpu", "cpu"), ("ssd", "ssd"), ("bat", "bat"), ("sysW", "w"), ("batW", "batW"), ("pct", "pct")):
            values = [g[src] for g in group if isinstance(g.get(src), (int, float))]
            if values:
                point[dst] = round(sum(values) / len(values), 1)
        points.append(point)
    return points


def _main(argv):
    command = argv[1] if len(argv) > 1 else "summary"
    hours = float(argv[2]) if len(argv) > 2 else 24
    rows = load(hours)
    if command == "series":
        print(json.dumps(series(rows), separators=(",", ":")))
        return
    s = summarize(rows)
    for key, value in s.items():
        print(f"{key}={value}")
    print(f"verdict={verdict(s)}")


if __name__ == "__main__":
    _main(sys.argv)
