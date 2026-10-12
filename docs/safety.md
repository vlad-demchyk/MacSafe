# Safety

MacSafe is built to be safe to run on a daily Mac. It does not need root, does not change system settings, and removes data in only one controlled way.

## What it can change

| Action | Where | Limits |
|---|---|---|
| Delete cache and log files | `~/.npm/_cacache`, `~/Library/Caches`, `~/Library/Logs`, `~/Library/Developer/Xcode/DerivedData` | Only the contents of allowed folders. Only files older than the chosen age (7, 30 or 90 days; 30 by default). Empty subfolders are removed. The folder itself stays. |
| Move to the Trash | Large files you choose, leftovers you choose | Only paths inside your home folder. Not your home folder itself, not the Trash. You can restore the item from the Trash. |
| Quit a process | Your own processes (the menu bar process list) | Two clicks: the first asks **Sure?**, the second sends a normal quit signal (SIGTERM). MacSafe never quits itself. |
| Free memory | `purge` | Needs your administrator password for each use, which macOS asks for. |
| Start at login | A LaunchAgent in `~/Library/LaunchAgents` | Created and removed by you from the menu bar. |

Nothing else is changed. MacSafe does not edit system files, firewall or FileVault settings, or other apps' data.

## How cleaning is restricted

- **Allowed roots only.** Cleaning works only inside a fixed list: npm, the user cache folder, Xcode's developer folder, and the user log folder. Anything else is refused, including paths that point elsewhere.
- **No symbolic links.** Links are skipped when scanning and when deleting. A folder whose real path leaves the allowed root is refused.
- **Apple's own caches are skipped.** Folders named `com.apple.*` are never touched.
- **Checked again when cleaning.** The server walks the folder again at the moment of cleaning, applies the age limit then, and skips any file whose parent folder now points outside the allowed root.
- **Explicit confirmation.** The cleaning panel requires you to tick "I have closed the apps whose caches I am cleaning" before the button works.
- **Categories you choose.** Developer tools, logs and app caches are separate. Unticked categories are skipped even if they were scanned.

## Trash, not delete

Large files and leftovers go to the macOS Trash through the system API, so you can restore them. MacSafe does not permanently delete them, and it does not empty the Trash for you. Empty it in Finder when you are sure.

## Leftovers

Leftovers are folders named after an app's bundle identifier in `~/Library` (Application Support, Containers, Saved Application State). MacSafe moves a folder to the Trash only when:

- no installed app matches the identifier,
- the folder is at least 30 days old,
- the folder is not a system identifier (`com.apple.*`).

A folder for an app that is still installed is never listed as a leftover. You still choose each item yourself.

## Permissions

MacSafe asks macOS for permission only when it needs it:

- **Finder control** (Automation) to empty or reveal items. Requested once, the first time you use it.
- **Full Disk Access is not required.** Some protected folders (Downloads, Documents, Desktop) may trigger a macOS prompt while large-file search reads them.
- **Notifications** for alerts, if you allow them in System Settings.

Security checks read only what macOS exposes without root. Values that need root are shown as unknown, not as "off".

## Your data

- Reports, history and settings are stored only in `~/Library/Application Support/MacSafe`.
- MacSafe makes no network requests. The Agents section copies commands for Terminal, and you run them yourself.
- Secret values in files are never printed in reports. Only the path and permissions are shown.

## Programs

- An app is removed only if it sits directly in `/Applications` or `~/Applications`, is not an Apple app, is not running and is not MacSafe. It goes to the Trash with the related Library files you chose; the page can only choose from the list MacSafe just found for that app.
- A command line package is removed with the manager's own command, a fixed argument list with no shell and no input, so a command that asks for a password fails instead of hanging. The name must match a strict pattern and appear in a fresh listing of that manager.

## Disk map

The map only reads sizes (`du` and a directory listing) and never leaves the home folder: a path outside it, a link or a file is refused. Symbolic links are not followed, so a link cannot make a folder look bigger or lead out of the home folder. Moving a block to the Trash uses the same rules as large files.

## Duplicates

The search chooses only among folders found directly in your home folder (not Library, not hidden ones), skips links, app bundles, `node_modules` and `.git`, and changes nothing. Before moving a copy to the Trash MacSafe checks again that it belongs to the last result, that at least one other copy of its group still exists with the same size, and that the file still has the same content as that copy (a file edited since the search is refused). Copies go to the Trash and can be restored.
