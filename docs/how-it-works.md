# How it works

MacSafe is a small Python app built on PyObjC (AppKit), psutil and pywebview. It has two processes and one shell-script generator.

## Components

| File | Role |
|---|---|
| `agent.py` | The background agent. It owns the menu bar item, samples the metrics, sends alerts, runs the daily check, and watches for new login items. It starts at login. |
| `menubar.py` | The status item, the popover, the secondary "summary" panel and the menus. |
| `metrics.py`, `gpu_iokit.py`, `thermal_hid.py` | Sampling: CPU and per-core load (psutil, `sysctl`), memory (`vm_stat`, `memory_pressure`), disk and network; GPU load, battery power and temperatures straight from IOKit (no subprocess per sample). |
| `tools/bench.py` | Cost check: milliseconds per sampling call and the CPU and memory of a running process. |
| `app.py` | The report window, a separate process started on demand. It loads the report page with pywebview and closes with the window. |
| `engine.py` | Shared logic: report paths, the check runner, permission checks and the methods the window page calls. |
| `cleanup.py`, `bigfiles.py`, `leftovers.py`, `trash.py` | Cleaning and file search. See [Safety](safety.md). |
| `alerts.py`, `persistence.py` | Alert rules with a cooldown, and the login-item baseline. |
| `prefs.py`, `i18n.py` | Settings (JSON file) and translation. |
| `autostart.py` | Writes and removes the LaunchAgent `com.user.macsafe` and asks launchd to load it. |
| `core/generate-report.sh` | The security and health generator. Collects facts with macOS commands and writes the report. |
| `core/template.html` | The report page: one HTML file with the report data embedded as JSON. |
| `bar.js`, `app.html` | The run-check bar in the window and the placeholder page shown before the first report. |

## Two processes

- The **agent** stays running for as long as you are logged in. It has no window and no WebKit, so it stays light.
- The **window** is a separate process. Closing the window ends it. A second launch of MacSafe asks the running agent to show the window instead of starting another copy (a file lock enforces a single agent).

## Data flow

1. Every 1–5 seconds (your choice) the agent samples the metrics on a background thread and updates the popover and the menu bar.
2. The security and health report is produced by `core/generate-report.sh`. It runs the macOS commands, writes `report-YYYY-MM-DD.json` with the facts, and renders `report-YYYY-MM-DD.html` from the template.
3. The same script also writes `dashboard.html`: one page with every stored snapshot, so the calendar can switch between days.
4. The agent reads the summary of the latest report (score, issues, changes) for the panel and the alerts. It runs a check once a day when no report exists for today, and rebuilds the dashboard after an update.
5. The window loads `dashboard.html` (or the placeholder page before the first report) with the language in the URL: `?lang=en` or `?lang=uk`.

## The report

- **Facts first.** The generator records raw values (commands and their output) before any judgement. The page shows the evidence next to each issue.
- **Score.** Warnings and critical findings reduce a score from 100. The number of penalties appears in the summary.
- **Changes.** Each report is compared with the previous one. New problems, resolved ones and changed values appear in "Changes since the previous report".
- **History.** Up to 60 HTML reports and 60 JSON snapshots are kept. The history keeps one year of weekly points.

## Alerts

Alerts fire when a condition holds for at least 60 seconds (or immediately for the battery and disk checks) and repeat at most once an hour:

- memory use is 92% or more,
- free disk space is below 10 GB,
- the battery is at 15% or less and the Mac is not on power,
- the security report has more issues than before.

New LaunchAgents and LaunchDaemons are checked every 10 minutes against a saved baseline. The first run only records the baseline.

## Preferences

Settings live in `~/Library/Application Support/MacSafe/дані/налаштування.json`:

- which values show in the menu bar (CPU, RAM, GPU) and the style,
- the refresh interval (1, 2 or 5 seconds),
- notifications and the daily check,
- the language: `auto`, `uk` or `en`.

## Tests

`tests/test_macsafe.py` runs without touching your data: paths are replaced with temporary folders, and `launchctl` and `ioreg` are not called. Run it with `python3 -m unittest discover -s tests`.

## Cost of sampling

Measured with `tools/bench.py` on Apple Silicon. Idle agent: about 1% CPU, 106 MB, 5 threads (Stats, for comparison: 2.7% CPU, 123 MB, 14 threads).

| Call | Before | Now |
|---|---|---|
| Temperature sensors | 51 ms every cycle | 22 ms every 3 s |
| Process scan (panel open) | 47 ms | 9 ms (name, path and owner cached per process) |
| Battery power | 15 ms (fork of `ioreg`) | 0.04 ms (IOKit) |

Disk speed (read and write) comes from `psutil.disk_io_counters` and is almost free. It replaces the size line of the disk card while the disk moves more than 100 KB/s. Per-process network speed was measured and left out: `nettop` costs about 1.3 s of CPU per 5 s, around a quarter of a core.
