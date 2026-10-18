"""Проекты (рабочие папки): реестр, индекс файлов, git, экспорт в ZIP."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import uuid
import zipfile
from pathlib import Path

from . import config

IGNORED_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", ".idea", ".vscode",
                "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next",
                "target", "coverage", ".cache", "site-packages"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".pdf", ".zip", ".gz", ".7z",
              ".rar", ".exe", ".dll", ".so", ".pyd", ".pyc", ".whl", ".gguf", ".bin", ".pt", ".onnx",
              ".mp3", ".mp4", ".wav", ".woff", ".woff2", ".ttf", ".eot", ".db", ".sqlite", ".lock"}
MAX_INDEX = 20000


class WorkspaceError(Exception):
    pass


# ---------------- реестр проектов ----------------
def _load() -> list[dict]:
    try:
        return json.loads(config.PROJECTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list[dict]) -> None:
    config.PROJECTS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")


def list_projects() -> list[dict]:
    return [p for p in _load() if Path(p["path"]).is_dir()]


def get_project(pid: str) -> dict:
    for p in _load():
        if p["id"] == pid:
            if not Path(p["path"]).is_dir():
                raise WorkspaceError(f"Папка проекта не найдена: {p['path']}")
            return p
    raise WorkspaceError("Проект не найден")


def add_project(path: str = "", git_url: str = "", branch: str = "") -> dict:
    if git_url:
        name = re.sub(r"\.git$", "", git_url.rstrip("/").rsplit("/", 1)[-1]) or "repo"
        target = config.WORKSPACES_DIR / name
        n = 2
        while target.exists():
            target = config.WORKSPACES_DIR / f"{name}-{n}"
            n += 1
        cmd = ["git", "clone", "--quiet", git_url, str(target)]
        if branch:
            cmd[2:2] = ["-b", branch]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise WorkspaceError(f"git clone: {(p.stderr or p.stdout).strip()[-500:]}")
        path = str(target)
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceError(f"Папка не существует: {root}")
    items = _load()
    for p in items:
        if Path(p["path"]).resolve() == root:
            return p
    proj = {"id": uuid.uuid4().hex[:8], "name": root.name, "path": str(root)}
    items.insert(0, proj)
    _save(items)
    return proj


def remove_project(pid: str) -> None:
    """Убирает проект из списка (файлы на диске не трогаются)."""
    _save([p for p in _load() if p["id"] != pid])


# ---------------- файлы ----------------
def resolve(root: str | Path, rel: str) -> Path:
    """Путь внутри проекта; выход за пределы корня запрещён."""
    root_p = Path(root).resolve()
    rel = (rel or ".").strip().replace("\\", "/")
    p = Path(rel)
    full = (p if p.is_absolute() else root_p / p).resolve()
    if full != root_p and root_p not in full.parents:
        raise WorkspaceError(f"Путь вне проекта: {rel}")
    return full


def relpath(root: str | Path, p: Path) -> str:
    return p.resolve().relative_to(Path(root).resolve()).as_posix()


def find_venv(root: str | Path) -> Path | None:
    """Виртуальное окружение проекта: в самом проекте или в родительской папке (монорепо)."""
    root = Path(root).resolve()
    for base in (root, root.parent):
        for name in (".venv", "venv", "env"):
            d = base / name
            if (d / "Scripts" / "python.exe").is_file() or (d / "bin" / "python").is_file():
                return d
    return None


def venv_bin(venv: Path) -> Path:
    return venv / "Scripts" if (venv / "Scripts").is_dir() else venv / "bin"


def is_git(root: str | Path) -> bool:
    return (Path(root) / ".git").exists()


def list_files(root: str | Path) -> list[str]:
    """Файлы проекта (относительные пути). В git-репозитории — с учётом .gitignore."""
    root = Path(root)
    if is_git(root):
        try:
            p = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root,
                               capture_output=True, timeout=30)
            if p.returncode == 0:
                files = [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f]
                # нет .gitignore — всё равно отсекаем служебные папки и байткод
                files = [f for f in files if not IGNORED_DIRS.intersection(f.split("/")[:-1])
                         and not f.endswith((".pyc", ".pyo"))]
                return sorted(f for f in files if (root / f).is_file())[:MAX_INDEX]
        except (OSError, subprocess.TimeoutExpired):
            pass
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS and not d.startswith("."))
        for f in sorted(filenames):
            out.append(Path(dirpath, f).relative_to(root).as_posix())
            if len(out) >= MAX_INDEX:
                return out
    return out


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in BINARY_EXT:
        return False
    try:
        with open(path, "rb") as f:
            return b"\0" not in f.read(4096)
    except OSError:
        return False


def tree_summary(root: str | Path, max_chars: int = 2500) -> str:
    """
    Компактный снимок структуры для системного промпта: одна строка на папку
    («dir/: a.py b.py»). Обрезается по длине — модель доищет через glob.
    """
    files = list_files(root)
    by_dir: dict[str, list[str]] = {}
    for f in files:
        d, _, name = f.rpartition("/")
        by_dir.setdefault(d, []).append(name)
    lines, total = [], 0
    for d in sorted(by_dir, key=lambda x: (x.count("/"), x)):
        names = by_dir[d]
        shown = " ".join(names[:25]) + (f" …(+{len(names) - 25})" if len(names) > 25 else "")
        line = f"{d or '.'}/: {shown}"
        if total + len(line) > max_chars:
            lines.append(f"… ещё {len(by_dir) - len(lines)} папок, всего файлов: {len(files)}")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines) if lines else "(пустой проект)"


def read_instructions(root: str | Path, max_chars: int = 4000) -> tuple[str, str]:
    """Инструкции проекта для агента (AGENTS.md / CLAUDE.md): (имя файла, текст) или ("", "")."""
    for name in ("AGENTS.md", "CLAUDE.md", ".lcw.md"):
        p = Path(root) / name
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            return name, text[:max_chars] + ("\n…(обрезано)" if len(text) > max_chars else "")
    return "", ""


def git_status(root: str | Path) -> dict:
    if not is_git(root):
        return {"git": False}
    def g(*args):
        p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return p.stdout.strip()
    try:
        return {"git": True, "branch": g("rev-parse", "--abbrev-ref", "HEAD"),
                "changes": g("status", "--porcelain").splitlines()[:200]}
    except (OSError, subprocess.TimeoutExpired):
        return {"git": True}


def export_zip(root: str | Path) -> bytes:
    buf = io.BytesIO()
    root = Path(root)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in list_files(root):
            z.write(root / f, f"{root.name}/{f}")
    return buf.getvalue()
