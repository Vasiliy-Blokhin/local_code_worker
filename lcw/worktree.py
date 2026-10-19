"""
Изолированная работа сессии в git worktree.

Сессия с этой опцией правит не рабочую копию проекта, а отдельный worktree на своей ветке
(`lcw/<id сессии>`). Это защищает то, что запущено из рабочей копии (сервисы, демоны), от
незаконченных правок агента. Когда работа проверена, изменения переносятся в рабочую копию
проекта патчем (без коммита — пользователь сам смотрит и коммитит), а worktree удаляется
вместе с сессией.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import config
from .workspace import WorkspaceError, is_git

WORKTREES_DIR = config.DATA_DIR / "worktrees"
BRANCH_PREFIX = "lcw/"
# скопированные из проекта настройки (.env) — не изменения агента, в патч не попадают
_PATHSPEC = (".", ":(exclude).env*")


def _git(cwd: str | Path, *args: str, input: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C.UTF-8"}
    return subprocess.run(["git", *args], cwd=str(cwd), input=input, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def _check(p: subprocess.CompletedProcess, what: str) -> str:
    if p.returncode != 0:
        raise WorkspaceError(f"{what}: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout


def create(project_root: str | Path, sid: str) -> dict:
    """Новый worktree от текущего HEAD проекта. Возвращает {path, branch, base}."""
    root = Path(project_root)
    if not is_git(root):
        raise WorkspaceError("Изолированная работа доступна только для git-репозитория.")
    base = _check(_git(root, "rev-parse", "HEAD"), "git rev-parse").strip()
    path = WORKTREES_DIR / sid
    branch = BRANCH_PREFIX + sid
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    _check(_git(root, "worktree", "add", "-b", branch, str(path), base), "git worktree add")
    # неотслеживаемые настройки (.env) нужны, чтобы проект импортировался и тесты шли как в оригинале
    for f in root.iterdir():
        if f.is_file() and f.name.startswith(".env") and not (path / f.name).exists():
            shutil.copy2(f, path / f.name)
    return {"path": str(path), "branch": branch, "base": base}


def patch(wt: dict) -> bytes:
    """
    Все изменения worktree относительно базового коммита (с новыми файлами) — unified diff.
    Байтами, без текстового режима: на Windows он превращает \n в \r\n и портит патч.
    """
    _stage(wt["path"])
    p = subprocess.run(["git", "diff", "--cached", "--binary", wt["base"], "--", *_PATHSPEC], cwd=wt["path"],
                       capture_output=True, timeout=120, env={**os.environ, "GIT_PAGER": "cat"})
    if p.returncode != 0:
        raise WorkspaceError("git diff: " + p.stderr.decode("utf-8", "replace").strip()[-600:])
    return p.stdout


def _stage(path: str) -> None:
    """В индекс — всё, что изменил агент; скопированные .env (если не в .gitignore) — обратно."""
    _check(_git(path, "add", "-A"), "git add")
    envs = [f.name for f in Path(path).iterdir() if f.is_file() and f.name.startswith(".env")]
    if envs:
        _git(path, "reset", "-q", "--", *envs)


def changed_files(wt: dict) -> list[str]:
    _stage(wt["path"])
    out = _check(_git(wt["path"], "diff", "--cached", "--name-only", wt["base"], "--", *_PATHSPEC), "git diff")
    return [l for l in out.splitlines() if l.strip()]


def apply(project_root: str | Path, wt: dict) -> list[str]:
    """
    Перенос изменений в рабочую копию проекта (без коммита). Сначала проверка `--check`, чтобы
    не оставить проект в полупримененном состоянии; при конфликте — трёхстороннее слияние.
    """
    diff = patch(wt)
    if not diff.strip():
        raise WorkspaceError("В изолированной копии нет изменений.")
    files = changed_files(wt)
    root = Path(project_root)
    with tempfile.NamedTemporaryFile("wb", suffix=".patch", delete=False) as f:
        f.write(diff)
    try:
        if _git(root, "apply", "--check", f.name).returncode == 0:
            _check(_git(root, "apply", f.name), "git apply")
        else:
            p = _git(root, "apply", "--3way", f.name)
            if p.returncode != 0:
                raise WorkspaceError("Изменения не накладываются на текущую рабочую копию проекта "
                                     f"(она ушла вперёд или есть конфликт): {(p.stderr or p.stdout).strip()[-600:]}")
    finally:
        os.unlink(f.name)
    return files


def remove(project_root: str | Path, wt: dict) -> None:
    """Удаление worktree и его ветки (ошибки не критичны: папку могли удалить вручную)."""
    root = Path(project_root)
    _git(root, "worktree", "remove", "--force", wt["path"])
    if Path(wt["path"]).exists():
        shutil.rmtree(wt["path"], ignore_errors=True)
        _git(root, "worktree", "prune")
    _git(root, "branch", "-D", wt["branch"])
