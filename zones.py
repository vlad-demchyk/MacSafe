"""Protected zones: places where cleaning must not reach, in one list that every cleaner uses.

Two levels:
- hard: never moved to the Trash from MacSafe, whatever the page asks (keys, mail, messages, cloud sync, system
  preferences, Photos and Music libraries, VM images, dotfiles in the home folder). Enforced in trash.py, which every
  removal passes through.
- isolated: the zone is left out of searches and one click removals (projects, vaults, dev environments, sync
  folders, game and model stores, backups, databases). Duplicates there are normal (a project vendors its
  dependencies, a build copies files, a sync folder mirrors a cloud), and removing one copy breaks something.
Each function returns a short reason (Ukrainian source text for T) or None.
"""
from i18n import T
import os

HOME = os.path.expanduser("~")

# A folder holding one of these is a project, a vault or a site: it is isolated as a whole
PROJECT_MARKERS = (".git", "package.json", "Cargo.toml", "pyproject.toml", "setup.py", "requirements.txt", "Package.swift",
                   "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "CMakeLists.txt",
                   "Makefile", "wp-config.php", "Dockerfile", "docker-compose.yml", "pubspec.yaml", ".obsidian", ".brain",
                   "vault.json", "steamapps", "Modelfile")
PROJECT_SUFFIXES = (".xcodeproj", ".xcworkspace", ".sln", ".csproj", ".uproject", ".blend1")
# Folders by name: dependencies, builds, environments, other tools' stores
DEV_DIR_NAMES = {"node_modules", "vendor", "venv", ".venv", "env", "Pods", "Carthage", "build", "dist", "target", "out",
                 "DerivedData", ".gradle", ".tox", "__pycache__", "site-packages", ".next", ".nuxt", ".cache", ".git",
                 "Library", ".Trash", ".npm", ".cargo", ".rustup", ".pyenv", ".nvm", ".ollama", ".lmstudio",
                 ".docker", ".m2", "bower_components"}
SYNC_NAMES = ("dropbox", "onedrive", "google drive", "googledrive", "icloud drive", "box", "nextcloud", "owncloud",
              "creative cloud files", "mega", "pcloud drive", "sync")
STORE_NAMES = {"backups", "steamapps", "llm models", "models", "vm", "virtual machines", "parallels", "utm",
               "virtualbox vms", "vmware fusion", "local sites", "herd", "docker", "applications", "bin", "brain"}
BUNDLE_SUFFIXES = (".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".fcpbundle", ".framework",
                   ".bundle", ".plugin", ".lproj", ".sparsebundle", ".noindex", ".xcarchive", ".utm", ".pvm",
                   ".vmwarevm", ".backupdb", ".lrlibrary", ".lrcat", ".logicx", ".band", ".sketch")
# Files that other programs keep open or depend on: copies of them are not clutter
SKIP_EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".realm", ".mdb", ".ldb", ".wal", ".shm", ".plist", ".keychain",
                   ".keychain-db", ".pem", ".key", ".p12", ".crt", ".kdbx", ".vmdk", ".vdi", ".qcow2", ".vhd", ".vhdx",
                   ".raw", ".img", ".iso", ".dmg", ".gguf", ".safetensors", ".ckpt", ".pkg", ".lock", ".log")

# Hard zones, relative to the home folder: nothing under them is ever trashed from MacSafe
HARD_HOME = (".ssh", ".gnupg", ".aws", ".kube", ".config", ".docker", ".zshrc", ".zprofile", ".bash_profile", ".bashrc",
             ".gitconfig", ".netrc", ".Trash",
             "Library/Keychains", "Library/Mail", "Library/Messages", "Library/Mobile Documents", "Library/CloudStorage",
             "Library/Safari", "Library/Calendars", "Library/Contacts", "Library/Reminders", "Library/Photos",
             "Library/Application Support/AddressBook", "Library/Application Support/CallHistoryDB",
             "Library/Application Support/com.apple.TCC", "Library/Application Support/MobileSync",
             "Library/Application Support/iCloud", "Library/Accounts", "Library/Biome", "Library/Fonts",
             "Library/Keyboard Layouts", "Library/Input Methods", "Library/Internet Plug-Ins", "Library/Extensions")
STANDARD = ("Documents", "Downloads", "Desktop", "Pictures", "Movies", "Music")   # the person's own content


def hard_protected(path, home=None):
    """A reason when the path is in a hard zone, else None. Apple's own data in Library is protected by name."""
    home = os.path.realpath(home or HOME)
    real = os.path.realpath(path)
    if real == home:
        return T("Домашня папка")
    if not real.startswith(home + os.sep):
        return T("Поза домашньою папкою")
    rel = real[len(home) + 1:]
    parts = rel.split("/")
    if len(parts) == 1 and parts[0].startswith("."):
        return T("Службовий файл у домашній папці")
    if len(parts) == 1 and parts[0] in ("Library", "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Public", "Applications"):
        return T("Системна папка")
    for zone in HARD_HOME:
        if rel == zone or rel.startswith(zone + "/"):
            return T("Захищена зона")
    # Apple's own data in Library (preferences, containers, caches of system apps) is never touched; an app's own
    # files named by its bundle id are what leftovers and uninstall may move
    if parts[0] == "Library" and any("com.apple." in p.lower() for p in parts):
        return T("Дані Apple")
    for suffix in (".photoslibrary", ".musiclibrary", ".tvlibrary", ".imovielibrary", ".fcpbundle", ".sparsebundle",
                   ".vmwarevm", ".pvm", ".utm", ".backupdb"):
        if any(p.lower().endswith(suffix) for p in parts):
            return T("Бібліотека або образ")
    return None


def project_marker(folder):
    """The marker that makes this folder a project, vault or site, or None. Looks only at the folder itself."""
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    for marker in PROJECT_MARKERS:
        if marker in names:
            return marker
    for name in names:
        if name.endswith(PROJECT_SUFFIXES):
            return name
    return None


def isolated_dir(name, path, check_marker=True):
    """A reason when a folder is left out of searches and one click removals, else None."""
    low = name.lower()
    if name in DEV_DIR_NAMES or (name.startswith(".") and name != "."):
        return T("Службова або залежності проєкту")
    if low.endswith(BUNDLE_SUFFIXES):
        return T("Пакет або бібліотека")
    if low in SYNC_NAMES or low.startswith(("onedrive", "google drive", "dropbox", "icloud")):
        return T("Хмарна синхронізація: видалення поширюється на інші пристрої")
    if low in STORE_NAMES:
        return T("Сховище іншої програми")
    marker = project_marker(path) if check_marker else None
    if marker:
        return T("Проєкт ({marker}): дублікати в ньому мають сенс", marker=marker)
    return None


def isolated_file(name):
    """A reason when a file is never offered as a duplicate, else None."""
    if name.startswith("."):
        return T("Прихований файл")
    if name.lower().endswith(SKIP_EXTENSIONS):
        return T("Файл, який використовують інші програми")
    return None


def blocked_for_trash(path, home=None):
    """For one click removals of big files, map blocks and duplicates: hard zones plus anything inside a project,
    a vault or another isolated zone, and a project folder itself."""
    reason = hard_protected(path, home)
    if reason:
        return reason
    home = os.path.realpath(home or HOME)
    real = os.path.realpath(path)
    parts = real[len(home) + 1:].split("/")
    probe = home
    for index, part in enumerate(parts):
        probe = os.path.join(probe, part)
        if index < len(parts) - 1 or os.path.isdir(probe):
            found = isolated_dir(part, probe, check_marker=not (index == 0 and part in STANDARD))
            if found:
                return found
    return None
