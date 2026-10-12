"""Turns the text a fixed command printed into rows for the page: a status, a title, a value and the things that can be done next.

The commands run with LC_ALL=C, so the format is stable. A parser never raises to the caller: parse() returns None when
the text is not what was expected, and the page then shows the raw text. A row's "ops" carry the argument (a pid, a path);
actions.py keeps them on the server and the page only sends the row index and the operation name.
"""
import os
import re
import subprocess

from i18n import T

GOOD, WARN, BAD, INFO = "good", "warning", "critical", "info"
_ORDER = {GOOD: 0, INFO: 0, WARN: 1, BAD: 2}

PANES = {
    "softwareupdate": "x-apple.systempreferences:com.apple.Software-Update-Settings.extension",
    "timemachine": "x-apple.systempreferences:com.apple.Time-Machine-Settings.extension",
    "network": "x-apple.systempreferences:com.apple.Network-Settings.extension",
    "privacy": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
}


def _row(title, value="", status=INFO, icon="info", ops=None):
    return {"title": title, "value": value, "status": status, "icon": icon, "ops": ops or []}


_mine = set()   # pids of the current user, read once per parse(): the quit button is offered only for these


def _can_quit(pid):
    return pid > 1 and pid != os.getpid() and pid in _mine


def _refresh_mine():
    global _mine
    try:
        out = subprocess.run(["/bin/ps", "-U", str(os.getuid()), "-o", "pid="], capture_output=True, text=True, timeout=5).stdout
        _mine = {int(x) for x in out.split() if x.isdigit()}
    except (OSError, subprocess.TimeoutExpired):
        _mine = set()


def _quit(pid, name):
    return {"op": "quit", "label": T("Завершити"), "arg": {"pid": pid, "name": name}}


def _reveal(path):
    return {"op": "reveal", "label": T("Показати у Finder"), "arg": {"path": path}}


def _pane(key, label=None):
    return {"op": "pane", "label": label or T("Відкрити налаштування"), "arg": {"key": key}}


def _worst(rows):
    return max((r["status"] for r in rows), key=lambda s: _ORDER.get(s, 0), default=INFO)


def _bundle(path):
    """The .app that holds an executable, so Finder shows the app and not a file deep inside it."""
    head, sep, _ = path.partition(".app/")
    return head + ".app" if sep else path


def _name(path):
    return os.path.basename(path.rstrip("/")) or path


# ---------- parsers: text → (summary, rows) ----------
def _sleep_blockers(text):
    rows = []
    for m in re.finditer(r"pid (\d+)\(([^)]*)\):\s*\[[^\]]*\]\s*(\S+)\s+(\w+)\s+named:\s*\"(.*)\"", text):
        pid, name, _held, kind, reason = int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)
        if kind not in ("PreventUserIdleSystemSleep", "PreventSystemSleep", "PreventUserIdleDisplaySleep", "NoIdleSleepAssertion", "NoDisplaySleepAssertion"):
            continue
        ops = [_quit(pid, name)] if _can_quit(pid) else []
        rows.append(_row(name, f"{reason} · {kind}", WARN, "moon", ops))
    if not rows:
        return T("Нічого не блокує сон"), [_row(T("Нічого не блокує сон"), "", GOOD, "check")]
    return T("Блокують сон: {n}", n=len(rows)), rows


def _thermal(text):
    rows = []
    for key, val in re.findall(r"(\w+)\s*=\s*(\d+)", text):
        n = int(val)
        status = WARN if "Limit" in key and n < 100 else GOOD if "Limit" in key else INFO
        rows.append(_row(key.replace("_", " "), str(n), status, "thermometer"))
    if "No thermal" in text or not rows or all(r["status"] != WARN for r in rows):
        rows.insert(0, _row(T("Перегріву немає"), "", GOOD, "check"))
        return T("Перегріву немає"), rows
    return T("Процесор обмежений через нагрів"), rows


_STATES = {"charging": "заряджається", "discharging": "розряджається", "charged": "заряджено", "finishing charge": "дозаряджається",
           "AC attached": "підключено до мережі"}


def _battery(text):
    rows = []
    m = re.search(r"drawing from '([^']+)'", text)
    if m:
        ac = "AC" in m.group(1)
        rows.append(_row(T("Живлення"), T("Від мережі") if ac else T("Від батареї"), GOOD if ac else INFO, "plug"))
    m = re.search(r"(\d+)%;\s*([^;]+);\s*([^;]*?)(?:\s+present|$)", text, re.M)
    if m:
        pct, state, left = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        status = BAD if pct <= 10 and "discharging" in state else WARN if pct <= 20 and "discharging" in state else GOOD
        value = f"{pct}%" + (f" · {T('до кінця: {t}', t=left.split(' ')[0])}" if left and "no estimate" not in left and "(no" not in left else "")
        rows.append(_row(T("Заряд"), value + f" · {T(_STATES[state]) if state in _STATES else state}", status, "battery"))
    if not rows:
        return None
    return rows[-1]["value"], rows


_UNITS = {"B": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def _bytes(text):
    m = re.match(r"([\d.]+)([BKMGT])", text)
    return float(m.group(1)) * _UNITS[m.group(2)] if m else None


def _disk_free(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    parts = lines[1].split()
    if len(parts) < 4:
        return None
    size, avail = parts[1], parts[3]
    total, free = _bytes(size), _bytes(avail)
    if not total or free is None:
        return None
    share = free / total   # df's own capacity column counts only the system volume on APFS, so free space is judged here
    status = BAD if share < 0.05 else WARN if share < 0.15 else GOOD
    rows = [_row(T("Вільно"), f"{avail} · {share:.0%}", status, "disk"), _row(T("Усього"), size, INFO, "disk")]
    return T("Вільно {avail} з {size}", avail=avail, size=size), rows


def _tm_destination(text):
    blocks = [b for b in re.split(r"=+", text) if "Name" in b]
    rows = []
    for b in blocks:
        name = re.search(r"Name\s*:\s*(.+)", b)
        kind = re.search(r"Kind\s*:\s*(.+)", b)
        rows.append(_row(name.group(1).strip() if name else "—", kind.group(1).strip() if kind else "", GOOD, "clock",
                         [_pane("timemachine")]))
    if not rows:
        return T("Місце для копій не вказано"), [_row(T("Місце для копій не вказано"), "", WARN, "clock", [_pane("timemachine", T("Налаштувати"))])]
    return T("Місць для копій: {n}", n=len(rows)), rows


def _mdm(text):
    rows = []
    for key, val in re.findall(r"^(.+?):\s*(Yes|No)\s*$", text, re.M):
        managed = val == "Yes"
        rows.append(_row(key, T("Так") if managed else T("Ні"), WARN if managed else GOOD, "monitor"))
    if not rows:
        return None
    managed = any(r["status"] == WARN for r in rows)
    return (T("Mac керує організація") if managed else T("Mac не керується організацією")), rows


def _top_cpu(text):
    rows = []
    for line in text.splitlines()[1:]:
        m = re.match(r"\s*(\d+)\s+([\d.]+)\s+(\d+)\s+(.+)$", line)
        if not m:
            continue
        pid, cpu, rss, comm = int(m.group(1)), float(m.group(2)), int(m.group(3)), m.group(4).strip()
        ops = [_quit(pid, _name(comm))] if _can_quit(pid) else []
        if comm.startswith("/") and os.path.exists(comm):
            ops.append(_reveal(_bundle(comm)))
        status = WARN if cpu >= 80 else INFO
        rows.append(_row(_name(_bundle(comm)), f"{cpu:.1f}% · {rss // 1024} {T('МБ')} · PID {pid}", status, "cpu", ops))
    if not rows:
        return None
    top = rows[0]
    return f"{top['title']} {top['value'].split(' · ')[0]}", rows


def _updates(text):
    rows = []
    for m in re.finditer(r"\* Label: (.+)\n\s*Title: ([^,]+), Version: ([^,]+), Size: ([^,]+),(.*)", text):
        label, title, version, size, rest = (g.strip() for g in m.groups())
        rows.append(_row(title, f"{version} · {size}" + (f" · {T('потрібне перезавантаження')}" if "restart" in rest else ""),
                         WARN, "download", [_pane("softwareupdate", T("Оновити"))]))
    if not rows:
        if "No new software" in text:
            return T("Оновлень немає"), [_row(T("Оновлень немає"), "", GOOD, "check")]
        return None
    return T("Доступно оновлень: {n}", n=len(rows)), rows


def _single(match_good, match_bad, good, bad, icon, bad_status=BAD, pane="privacy"):
    def parse(text):   # T runs here, not at import, so the language chosen at start is used
        if re.search(match_good, text, re.I):
            return T(good), [_row(T(good), "", GOOD, icon)]
        if re.search(match_bad, text, re.I):
            return T(bad), [_row(T(bad), "", bad_status, icon, [_pane(pane)] if pane else [])]
        return None
    return parse


def _proxy(text):
    rows = []
    for key, label in (("HTTP", "HTTP"), ("HTTPS", "HTTPS"), ("SOCKS", "SOCKS"), ("FTP", "FTP"), ("ProxyAutoConfig", "PAC")):
        if re.search(rf"\b{key}Enable\s*:\s*1", text):
            host = re.search(rf"\b{key}Proxy\s*:\s*(\S+)", text) or re.search(rf"\b{key}URLString\s*:\s*(\S+)", text)
            port = re.search(rf"\b{key}Port\s*:\s*(\d+)", text)
            value = (host.group(1) if host else "") + (f":{port.group(1)}" if port else "")
            rows.append(_row(label, value, WARN, "globe", [_pane("network")]))
    if not rows:
        return T("Проксі не використовується"), [_row(T("Проксі не використовується"), "", GOOD, "globe")]
    return T("Проксі: {n}", n=len(rows)), rows


def _ports(text):
    rows = []
    seen = set()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 9 or not parts[1].isdigit():
            continue
        command, pid, user = parts[0].replace("\\x20", " "), int(parts[1]), parts[2]
        address = " ".join(parts[8:])
        local = re.sub(r"\s*\(LISTEN\)", "", address)
        if (pid, local.rsplit(":", 1)[-1]) in seen:   # the same port on IPv4 and IPv6 is one row
            continue
        seen.add((pid, local.rsplit(":", 1)[-1]))
        public = local.startswith(("*:", "[::]:", "0.0.0.0:"))
        ops = [_quit(pid, command)] if _can_quit(pid) else []
        rows.append(_row(command, f"{local} · PID {pid}", WARN if public else GOOD, "network", ops))
    if not rows:
        return T("Відкритих портів немає"), [_row(T("Відкритих портів немає"), "", GOOD, "check")]
    public = sum(1 for r in rows if r["status"] == WARN)
    return (T("Слухають: {n}, назовні: {p}", n=len(rows), p=public)), rows


def _launchagents(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    folder = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    rows = [_row(lines[0], "", GOOD, "check")] if lines else []
    for name in lines[1:]:
        path = os.path.join(folder, name)
        rows.append(_row(name, "600", GOOD, "lock", [_reveal(path)] if os.path.exists(path) else []))
    return (lines[0] if lines else None), rows or None


PARSERS = {
    "sleep_blockers": _sleep_blockers,
    "thermal": _thermal,
    "battery": _battery,
    "disk_free": _disk_free,
    "tm_destination": _tm_destination,
    "mdm": _mdm,
    "top_cpu": _top_cpu,
    "updates": _updates,
    "sip": _single(r"status: enabled", r"status: disabled", "SIP увімкнено", "SIP вимкнено", "shield", pane=None),
    "filevault": _single(r"FileVault is On", r"FileVault is Off", "Диск зашифровано", "Диск не зашифровано", "lock"),
    "gatekeeper": _single(r"assessments enabled", r"assessments disabled", "Gatekeeper увімкнено", "Gatekeeper вимкнено", "shield"),
    "firewall": _single(r"enabled", r"disabled", "Брандмауер увімкнено", "Брандмауер вимкнено", "flame", WARN, "network"),
    "proxy": _proxy,
    "ports": _ports,
    "fix_launchagent_perms": _launchagents,
}


def parse(action_id, text, ok=True):
    """{"summary", "status", "rows"} for a finished action, or None when the text has no structure to show."""
    parser = PARSERS.get(action_id)
    if parser is None or not ok:
        return None
    _refresh_mine()
    try:
        result = parser(text or "")
    except Exception:   # a parser must never break the result: the raw text is shown instead
        return None
    if not result or not result[1]:
        return None
    summary, rows = result
    return {"summary": summary, "status": _worst(rows), "rows": rows}
