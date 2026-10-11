"""Login start: a LaunchAgent that opens the app with --background.

A background copy does not open a window, only the menu bar icon. If one copy is already running: a background launch
exits quietly through the instance lock (app.py), so registering the service carries no risk of a duplicate.

Disabling and re-registering call launchctl bootout, but not when the app is running itself
as a launchd job: launchd marks such a process with the XPC_SERVICE_NAME variable equal to the label. Bootout
its job would kill the process that is performing the action. In this case the plist changes, and the change takes effect at the next login.
"""
from i18n import T
import os
import plistlib
import subprocess

LABEL = "com.user.macsafe"
AGENT_DIR = os.path.expanduser("~/Library/LaunchAgents")
AGENT = os.path.join(AGENT_DIR, f"{LABEL}.plist")
PYTHON = "/usr/bin/python3"       # the same interpreter as in the launcher


def _domain():
    return f"gui/{os.getuid()}"


def _is_self_job():
    """True if this process was started by launchd itself as a job with our label."""
    return os.environ.get("XPC_SERVICE_NAME") == LABEL


def is_enabled():
    return os.path.isfile(AGENT)


def _is_loaded():
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    return r.returncode == 0


def _launchctl(*args):
    r = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if r.returncode == 0:
        return True, ""
    lines = (r.stderr or r.stdout).strip().splitlines()
    return False, lines[-1] if lines else T("launchctl не вдався")


def _read_plist():
    try:
        with open(AGENT, "rb") as fh:
            return plistlib.load(fh)
    except (OSError, ValueError):
        return None


def _build(launch_args, workdir, log_path):
    return {
        "Label": LABEL,
        "ProgramArguments": [*launch_args, "--background"],
        "WorkingDirectory": workdir,
        "RunAtLoad": True,
        "ProcessType": "Interactive",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
    }


def enable(launch_args, workdir, log_path):
    """Writes the plist atomically and registers the service with launchd. Returns (ok, explanation).

    launch_args: the launch command without --background (the .app launcher or python app.py).
    """
    os.makedirs(AGENT_DIR, exist_ok=True)
    plist = _build(launch_args, workdir, log_path)
    changed = _read_plist() != plist
    tmp = f"{AGENT}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        plistlib.dump(plist, fh)
    os.replace(tmp, AGENT)

    if _is_loaded():
        if not changed or _is_self_job():
            return True, ""
        # A new plist takes effect only after re-registration; the old service is removed first
        ok, err = _launchctl("bootout", f"{_domain()}/{LABEL}")
        if not ok:
            return False, err
    return _launchctl("bootstrap", _domain(), AGENT)


def disable():
    """Removes the plist and unloads the service from launchd if it is not the current process. Returns (ok, explanation)."""
    try:
        os.remove(AGENT)
    except FileNotFoundError:
        pass
    if _is_self_job() or not _is_loaded():
        return True, ""
    return _launchctl("bootout", f"{_domain()}/{LABEL}")
