# Modes and sections

MacSafe has two places to look: the **menu bar popover** (live numbers and quick actions) and the **report window** (the full report, organised in six groups). Both use the same data.

## Menu bar popover

Click the MacSafe icon in the menu bar.

| Part | What it shows |
|---|---|
| Header | Security summary from the last report. |
| CPU, RAM, GPU | The current value, a 60-second graph and details. CPU shows per-core load, uptime and the load average. RAM shows memory use, swap and the breakdown. GPU shows the load (if macOS reports it). |
| Disk, Network, Battery | Free space, network speed, battery charge and power source. |
| Process lists | The top processes by CPU, GPU and memory, grouped by app. Click a name to pin the row; click × and then **Sure?** to quit a process. Only your own processes can be quit, never MacSafe. |
| Appearance | Which values show in the menu bar, the menu bar style, the refresh interval, notifications and the daily check, **Language** (English or Ukrainian). |
| Actions | **Quick clean** (two clicks: the first measures, the second cleans), **Check** (runs the security report), **Detailed clean** (opens the window's cleaning section), **Free memory** (asks for the administrator password), and **Start at login**. |
| Footer | **Reports** opens the window, the sidebar button shows a short summary, **Quit** closes MacSafe. |

## Report window

The sidebar groups sections into six areas. Each section shows its issues count when it has any.

### Overview

- **State**: the score (0–100), the most important issues first, the key numbers, the snapshot time, and the context (macOS version, mode).
- **Issues**: every finding, filtered by urgency (urgent, warning, info). Each issue expands to show the evidence, why it matters, the risk of acting, and the steps.
- **Action plan**: the steps grouped by time: now, this week, this month, regularly. Check marks are saved in the browser.

### System

- **Mac**: model, chip, cores, storage, battery health and cycles, load, and the macOS version.
- **Memory**: memory pressure, what is compressed or in swap, swap activity, and which apps use the most memory.
- **Processes**: the top processes by CPU and memory. "Lifetime" shows the average CPU over the whole run of a process.
- **Temperatures**: component temperatures, fans and thermal state. Without root, CPU and GPU temperatures are usually unavailable. The section says so instead of guessing.

### Security

- **Checks**: local checks without root or Full Disk Access (FileVault, firewall, stealth mode, SIP, Gatekeeper, TCP listeners, system extensions, login items, XProtect). An unknown state is not the same as a disabled protection.
- **Login items**: LaunchAgents and LaunchDaemons, broken paths, and secrets stored in files with open permissions.
- **Crashes**: crash reports from the last week and kernel panics from the last 30 days.
- **XBrain daemon**: the health of a local daemon, judged by spikes, restart loops, errors in its log and the integrity of its database. It shows nothing useful when the daemon is not installed.

### Maintenance

- **Cleaning**: the cleaning panel (see [Safety](safety.md)), large files, and leftovers of removed apps. Nothing is deleted without confirmation, and removals go to the Trash.
- **Disk map** (a tile on the Cleaning page): a treemap of what takes space in your home folder, one folder level at a time. Click selects a block, double click opens a folder, and a selected block can be shown in Finder or moved to the Trash after confirmation. Folders are counted in parallel and the map fills in while it works; the biggest folders can take a minute or two the first time, and a result is kept for two minutes.
- **Duplicates** (a tile on the Cleaning page): files with identical content in the folders you tick (the standard ones are ticked by default). The search compares sizes, then the first 64 KB, then the whole content, in the background and can be stopped. Hard links to one file are not counted as copies. In every group the oldest copy stays unmarked; you can change the marks, but a group cannot lose its last copy.
- **Programs** (a tile on the Cleaning page): installed apps and command line packages (Homebrew formulae and casks, npm global, cargo, pipx). Removing an app moves it to the Trash together with the Library files you tick; files matched by the app name only are unticked by default. A running app cannot be removed. A command line package is removed by its own manager; a Homebrew package that others depend on is refused.
- **Action buttons**: commands for Terminal, grouped by purpose. The page runs nothing; you copy a command and run it yourself. Destructive commands are marked in red.

### Help

- **Tips**: short guidance by topic. The search at the top filters this list too.
- **Agents**: an explanation of an issue written by a model. You copy the command into Terminal yourself. The issue text is sent to the provider you choose, so check it before running anything.

### Archive

- **History**: the score trend across runs, with links to earlier HTML reports.
- **Other Macs**: a place for snapshots from other Macs. Drop a JSON snapshot onto the area, or put it in the `systems` folder and regenerate the report.
- **Notes**: free text: what is done, what is checked, what is waiting. Saved in the browser.

### App

- **Settings**: the interface language (English or Ukrainian). The same switch is in the menu bar popover under Appearance. The menu bar restarts itself to apply the choice.

## Keyboard

- `1`–`9` switch between sections of the current group.
- `Esc` returns to the overview.
- The search field filters the issues, the advice and the cleaning items.
