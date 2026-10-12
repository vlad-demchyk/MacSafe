"""Local LLM models: search and manual removal.

Search only reads folders of known tools: Ollama, Hugging Face (also MLX), LM Studio, GPT4All, Jan, llama.cpp.
Models are a manual category: they are never part of the automatic cleaning. Removal is only for models chosen one by
one and only from the list just found. Folders and files go to the Trash (restorable); an Ollama model is removed with
`ollama rm`, because its files are shared by hash and must not be deleted one by one.
"""
from i18n import T
import json
import os
import shutil
import subprocess
import time

HOME = os.path.expanduser("~")
OLLAMA_MANIFESTS = os.path.join(HOME, ".ollama", "models", "manifests")
HF_HUB = os.path.join(HOME, ".cache", "huggingface", "hub")
LMSTUDIO_ROOTS = (os.path.join(HOME, ".lmstudio", "models"), os.path.join(HOME, ".cache", "lm-studio", "models"))
GPT4ALL_DIR = os.path.join(HOME, "Library", "Application Support", "nomic.ai", "GPT4All")
JAN_ROOTS = (os.path.join(HOME, "jan", "models"), os.path.join(HOME, "Library", "Application Support", "Jan", "data", "models"))
LLAMACPP_DIR = os.path.join(HOME, "Library", "Caches", "llama.cpp")
MODEL_EXT = (".gguf", ".safetensors", ".bin", ".mlx")
MAX_IDS = 40
TOOL_APPS = {"lmstudio": ("lm studio",), "gpt4all": ("gpt4all",), "jan": ("jan",)}   # running names that block removal
TOOL_TITLES = {"ollama": "Ollama", "huggingface": "Hugging Face", "lmstudio": "LM Studio", "gpt4all": "GPT4All",
               "jan": "Jan", "llamacpp": "llama.cpp"}


def _dir_mb(path):
    """Size in MB of the regular files under path. Symbolic links are not followed or counted twice."""
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if not os.path.islink(os.path.join(root, name)):
                total += st.st_size
    return round(total / (1024 * 1024), 1)


def _days_since(path):
    try:
        return max(0, int((time.time() - os.stat(path).st_mtime) // 86400))
    except OSError:
        return None


def _item(tool, name, size_mb, path, days, kind):
    return {"id": f"{tool}:{name}", "tool": tool, "toolTitle": TOOL_TITLES[tool], "name": name, "sizeMb": size_mb,
            "path": path, "days": days, "kind": kind}


def _ollama():
    items = []
    for dirpath, _dirs, files in os.walk(OLLAMA_MANIFESTS, followlinks=False):
        for tag in files:
            path = os.path.join(dirpath, tag)
            parts = os.path.relpath(dirpath, OLLAMA_MANIFESTS).split(os.sep)   # host/namespace/model
            if len(parts) < 3:
                continue
            if parts[0] == "registry.ollama.ai":
                parts = parts[1:]
                if parts[0] == "library":
                    parts = parts[1:]
            model = "/".join(parts)
            name = f"{model}:{tag}"
            try:
                with open(path, encoding="utf-8") as fh:
                    manifest = json.load(fh)
                layers = [manifest.get("config") or {}] + list(manifest.get("layers") or [])
                size = sum(int(layer.get("size") or 0) for layer in layers if isinstance(layer, dict))
            except (OSError, ValueError, TypeError):
                continue
            items.append(_item("ollama", name, round(size / (1024 * 1024), 1), path, _days_since(path), "ollama"))
    return items


def _huggingface():
    items = []
    try:
        entries = list(os.scandir(HF_HUB))
    except OSError:
        return items
    for e in entries:
        if not e.name.startswith("models--") or e.is_symlink() or not e.is_dir(follow_symlinks=False):
            continue
        name = e.name[len("models--"):].replace("--", "/")
        items.append(_item("huggingface", name, _dir_mb(e.path), e.path, _days_since(os.path.join(e.path, "snapshots")), "dir"))
    return items


def _model_dirs(tool, roots, depth):
    """Folders `depth` levels below the roots (LM Studio: publisher/model; Jan: model) that hold model files."""
    items = []
    for root in roots:
        level = [root]
        for _ in range(depth):
            nxt = []
            for folder in level:
                try:
                    nxt += [e.path for e in os.scandir(folder) if e.is_dir(follow_symlinks=False) and not e.name.startswith(".")]
                except OSError:
                    pass
            level = nxt
        for folder in level:
            if os.path.islink(folder):
                continue
            has_model = any(f.lower().endswith(MODEL_EXT) for _r, _d, fs in os.walk(folder) for f in fs)
            if has_model:
                items.append(_item(tool, os.path.relpath(folder, root), _dir_mb(folder), folder, _days_since(folder), "dir"))
    return items


def _model_files(tool, folder):
    items = []
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return items
    for e in entries:
        if e.is_file(follow_symlinks=False) and e.name.lower().endswith(MODEL_EXT):
            try:
                size = round(e.stat(follow_symlinks=False).st_size / (1024 * 1024), 1)
            except OSError:
                continue
            items.append(_item(tool, e.name, size, e.path, _days_since(e.path), "file"))
    return items


def scan():
    """All local models found: [{id, tool, name, sizeMb, path, days, kind, blocked}] in descending size order."""
    items = _ollama() + _huggingface()
    items += _model_dirs("lmstudio", LMSTUDIO_ROOTS, 2)
    items += _model_dirs("jan", JAN_ROOTS, 1)
    items += _model_files("gpt4all", GPT4ALL_DIR) + _model_files("llamacpp", LLAMACPP_DIR)
    names = _running_names()
    for item in items:
        item["blocked"] = _blocked(item["tool"], names)
    items.sort(key=lambda i: -i["sizeMb"])
    return items


def _running_names():
    try:
        import psutil
        return {(p.info.get("name") or "").lower() for p in psutil.process_iter(["name"])}
    except Exception:
        return set()


def _blocked(tool, names):
    for word in TOOL_APPS.get(tool, ()):
        if any(word in n for n in names):
            return T("Застосунок запущено. Закрийте його і повторіть.")
    return None


def _ollama_bin():
    for path in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/Applications/Ollama.app/Contents/Resources/ollama"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return shutil.which("ollama")


def remove(ids):
    """Removes the chosen models. Only ids from a fresh search; every result says what happened."""
    if not isinstance(ids, list) or not ids or len(ids) > MAX_IDS:
        return {"ok": False, "error": T("Нічого не вибрано")}
    import trash
    found = {item["id"]: item for item in scan()}
    results, freed = [], 0.0
    for mid in dict.fromkeys(ids):
        item = found.get(mid) if isinstance(mid, str) else None
        if item is None:
            results.append({"id": str(mid), "ok": False, "error": T("Не є локальною моделлю")})
            continue
        if item["blocked"]:
            results.append({"id": mid, "ok": False, "skipped": True, "error": item["blocked"]})
            continue
        if item["kind"] == "ollama":
            binary = _ollama_bin()
            if binary is None:
                results.append({"id": mid, "ok": False, "error": T("Не знайдено команду ollama")})
                continue
            try:
                r = subprocess.run([binary, "rm", item["name"]], capture_output=True, text=True, timeout=120)
                ok, error = r.returncode == 0, (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
                error = "" if ok else error[0]
            except (OSError, subprocess.SubprocessError) as exc:
                ok, error = False, str(exc)
        else:
            ok, error = trash.move_to_trash(item["path"])
        results.append({"id": mid, "ok": ok, "error": error})
        if ok:
            freed += item["sizeMb"]
    return {"ok": True, "results": results, "freedMb": round(freed, 1)}
