"""
Инструменты агента. Описания (схемы) намеренно короткие: они входят в каждый
запрос, и каждый лишний токен — время префилла. Результаты для модели тоже
компактные; подробности (диффы) уходят только в UI.
"""
from __future__ import annotations

import ast
import asyncio
import difflib
import json
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .workspace import WorkspaceError, is_text_file, list_files, relpath, resolve

READ_LIMIT = 400        # строк по умолчанию
LINE_CHARS = 400        # обрезка длинных строк при чтении
GREP_MAX = 80           # совпадений

def _shell() -> tuple[list[str], str]:
    if sys.platform != "win32":
        return ["bash", "-lc"], "bash"
    for p in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        # bash из WSL (System32) не видит пути Windows — берём только Git Bash
        if p and Path(p).is_file() and "system32" not in p.lower():
            return [p, "-c"], "bash (Git Bash on Windows)"
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command"], "PowerShell"


SHELL_CMD, SHELL_NAME = _shell()


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


S = {"type": "string"}
I = {"type": "integer"}

TOOLS = [
    _fn("read", "Read a text file. Returns numbered lines. Use offset/limit for big files.",
        {"path": S, "offset": {**I, "description": "1-based start line"},
         "limit": {**I, "description": f"lines, default {READ_LIMIT}"}}, ["path"]),
    _fn("glob", "Find files by glob pattern (e.g. **/*.py, src/**/test_*.js).",
        {"pattern": S, "path": {**S, "description": "subdir to search in"}}, ["pattern"]),
    _fn("grep", "Search file contents with a regex. Returns path:line: text.",
        {"pattern": S, "path": {**S, "description": "file or subdir"},
         "glob": {**S, "description": "file filter, e.g. *.py"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
    _fn("edit", "Replace an exact unique snippet in a file. `old` must match the file "
                "exactly (copy from read output without line numbers, keep trailing \\ etc.); "
                "include enough lines to be unique. Empty `old` appends `new` to the end of file. "
                "Prefer several small edits over rewriting.",
        {"path": S, "old": S, "new": S,
         "replace_all": {"type": "boolean", "description": "only for renames: replace every occurrence"}},
        ["path", "old", "new"]),
    _fn("write", "Create a new file or fully overwrite one (prefer edit for changes).",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("run", f"Run a shell command ({SHELL_NAME}) in the project root: tests, linters, git, "
               f"installs. Non-interactive only. Timeout {config.COMMAND_TIMEOUT}s.",
        {"command": S, "timeout": {**I, "description": "seconds"}}, ["command"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
MUTATING = {"edit", "write"}


class ToolError(Exception):
    pass


@dataclass
class ToolResult:
    output: str                       # для модели (компактно)
    ok: bool = True
    diff: str = ""                    # для UI
    path: str = ""


@dataclass
class ToolContext:
    root: Path
    # path -> (mtime_ns, offset, limit): что модель уже видела (дедупликация чтений)
    seen: dict = field(default_factory=dict)
    # вызывается перед изменением файла: сохранить оригинал для отката
    snapshot: callable = None


def _trunc(text: str, limit: int = config.TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-int(limit * 0.3):]
    return f"{head}\n…[обрезано {len(text) - len(head) - len(tail)} символов]…\n{tail}"


def _read_text(p: Path) -> tuple[str, str]:
    """Текст файла с \\n и исходный стиль перевода строк."""
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), nl


def _write_text(p: Path, text: str, nl: str = "\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", nl) if nl != "\n" else text
    p.write_bytes(data.encode("utf-8"))


def _diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{path}", f"b/{path}", n=2))


def _diff_stat(diff: str) -> str:
    add = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return f"+{add} -{rem}"


# ---------------- реализация ----------------
def t_read(ctx: ToolContext, path: str, offset: int = 1, limit: int = READ_LIMIT) -> ToolResult:
    p = resolve(ctx.root, path)
    if p.is_dir():
        return t_glob(ctx, "*", path)
    if not p.is_file():
        raise ToolError(f"Файл не найден: {path}")
    if not is_text_file(p):
        raise ToolError(f"Бинарный файл: {path}")
    rel = relpath(ctx.root, p)
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or READ_LIMIT), 2000))
    mtime = p.stat().st_mtime_ns
    key = (rel, offset, limit)
    if ctx.seen.get(key) == mtime:
        return ToolResult(f"{rel}: не изменился с прошлого чтения (содержимое выше в диалоге).", path=rel)
    text, _ = _read_text(p)
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    chunk = lines[offset - 1: offset - 1 + limit]
    width = len(str(offset + len(chunk)))
    out = []
    for i, line in enumerate(chunk, offset):
        if len(line) > LINE_CHARS:
            line = line[:LINE_CHARS] + "…"
        out.append(f"{i:>{width}}│{line}")
    ctx.seen[key] = mtime
    tail = ""
    if offset - 1 + limit < len(lines):
        tail = f"\n…(строки {offset + len(chunk)}-{len(lines)} не показаны; offset={offset + len(chunk)})"
    if not lines:
        return ToolResult(f"{rel}: пустой файл", path=rel)
    return ToolResult(_trunc("\n".join(out)) + tail, path=rel)


def t_glob(ctx: ToolContext, pattern: str, path: str = "") -> ToolResult:
    base = relpath(ctx.root, resolve(ctx.root, path or "."))
    base = "" if base == "." else base.rstrip("/") + "/"
    pat = pattern.strip().replace("\\", "/").removeprefix("./")
    files = [f for f in list_files(ctx.root) if f.startswith(base)]
    hits = []
    for f in files:
        sub = f[len(base):]
        if (fnmatch.fnmatch(sub, pat) or fnmatch.fnmatch(sub, pat.replace("**/", ""))
                or ("/" not in pat and fnmatch.fnmatch(sub.rsplit("/", 1)[-1], pat))):
            hits.append(f)
    if not hits:
        return ToolResult("Нет совпадений.")
    shown = hits[:200]
    more = f"\n…и ещё {len(hits) - 200}" if len(hits) > 200 else ""
    return ToolResult("\n".join(shown) + more)


def t_grep(ctx: ToolContext, pattern: str, path: str = "", glob: str = "",
           ignore_case: bool = False) -> ToolResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ToolError(f"Неверный regex: {e}")
    target = resolve(ctx.root, path or ".")
    if target.is_file():
        files = [relpath(ctx.root, target)]
    else:
        base = relpath(ctx.root, target)
        base = "" if base == "." else base + "/"
        files = [f for f in list_files(ctx.root) if f.startswith(base)]
    if glob:
        files = [f for f in files if fnmatch.fnmatch(f.rsplit("/", 1)[-1], glob) or fnmatch.fnmatch(f, glob)]
    out, n = [], 0
    for f in files:
        p = ctx.root / f
        if not is_text_file(p) or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{f}:{i}: {line.strip()[:200]}")
                n += 1
                if n >= GREP_MAX:
                    out.append(f"…лимит {GREP_MAX} совпадений, уточните запрос")
                    return ToolResult("\n".join(out))
    return ToolResult("\n".join(out) if out else "Нет совпадений.")


def _find_block(text: str, old: str) -> tuple[int, int] | None:
    """Совпадение блока строк без учёта пробелов в конце строк (частая ошибка моделей)."""
    t_lines = text.split("\n")
    o_lines = old.strip("\n").split("\n")
    o_norm = [l.rstrip() for l in o_lines]
    hits = []
    for i in range(len(t_lines) - len(o_lines) + 1):
        if [l.rstrip() for l in t_lines[i:i + len(o_lines)]] == o_norm:
            hits.append(i)
    if len(hits) != 1:
        return None
    start = sum(len(l) + 1 for l in t_lines[:hits[0]])
    end = start + len("\n".join(t_lines[hits[0]:hits[0] + len(o_lines)]))
    return start, end


def _closest(text: str, old: str) -> str:
    lines = text.split("\n")
    o_lines = old.strip("\n").split("\n")
    n = len(o_lines)
    best, best_i = 0.0, 0
    target = "\n".join(l.strip() for l in o_lines)
    for i in range(max(1, len(lines) - n + 1)):
        cand = "\n".join(l.strip() for l in lines[i:i + n])
        r = difflib.SequenceMatcher(None, cand, target).quick_ratio()
        if r > best:
            best, best_i = r, i
    if best < 0.5:
        return ""
    snippet = "\n".join(f"{j + 1}│{lines[j]}" for j in range(best_i, min(len(lines), best_i + n)))
    return f"\nСамый похожий фрагмент (строка {best_i + 1}):\n{snippet}"


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _snippet(text: str, first: int, last: int, pad: int = 2, cap: int = 16) -> str:
    """Фрагмент с номерами строк вокруг правки — модель видит, что получилось."""
    lines = text.split("\n")
    a, b = max(1, first - pad), min(len(lines), last + pad)
    if b - a + 1 > cap:
        b = a + cap - 1
    return "\n".join(f"{i}│{lines[i - 1]}" for i in range(a, b + 1))


def _syntax_warning(rel: str, text: str) -> str:
    """Быстрая проверка синтаксиса после правки (без запуска кода)."""
    try:
        if rel.endswith(".py"):
            ast.parse(text)
        elif rel.endswith(".json"):
            json.loads(text)
    except SyntaxError as e:
        return f"\nВНИМАНИЕ: синтаксическая ошибка в строке {e.lineno}: {e.msg}. Исправь."
    except ValueError as e:
        return f"\nВНИМАНИЕ: невалидный JSON: {e}. Исправь."
    return ""


def t_edit(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
    p = resolve(ctx.root, path)
    if not p.is_file():
        raise ToolError(f"Файл не найден: {path}. Для нового файла используйте write.")
    rel = relpath(ctx.root, p)
    text, nl = _read_text(p)
    old = old.replace("\r\n", "\n")
    new = new.replace("\r\n", "\n")
    if old == new:
        raise ToolError("old и new совпадают.")
    if not old:  # дописать в конец файла
        base = text if not text or text.endswith("\n") else text + "\n"
        new = new if new.endswith("\n") else new + "\n"
        return _finish_edit(ctx, p, rel, text, base + new, nl, len(base), new)
    positions, i = [], text.find(old)
    while i != -1:
        positions.append(i)
        i = text.find(old, i + len(old))
    if len(positions) > 1 and not replace_all:
        lines = ", ".join(str(_line_at(text, x)) for x in positions[:10])
        raise ToolError(f"old встречается {len(positions)} раз (строки {lines}). Правка не применена. "
                        f"Включи в old соседние уникальные строки (например, строку def/class выше), "
                        f"чтобы фрагмент встречался ровно один раз.")
    if positions:
        start = positions[0]
        after = text.replace(old, new) if replace_all else text[:start] + new + text[start + len(old):]
    else:
        span = _find_block(text, old)
        if not span:
            raise ToolError("old не найден в файле (нужно точное совпадение, с отступами). "
                            "Перечитайте нужный фрагмент через read." + _closest(text, old))
        start = span[0]
        after = text[:span[0]] + new + text[span[1]:]
    if replace_all and len(positions) > 1:
        lines = ", ".join(str(_line_at(text, x)) for x in positions[:20])
        return _finish_edit(ctx, p, rel, text, after, nl, start, new,
                            f"заменено {len(positions)} вхождений (строки {lines})")
    return _finish_edit(ctx, p, rel, text, after, nl, start, new)


def _finish_edit(ctx: ToolContext, p: Path, rel: str, text: str, after: str, nl: str,
                 start: int, new: str, summary: str = "") -> ToolResult:
    if ctx.snapshot:
        ctx.snapshot(rel, text if nl == "\n" else text.replace("\n", nl))
    _write_text(p, after, nl)
    d = _diff(rel, text, after)
    _forget(ctx, rel)
    if summary:
        msg = f"OK: {rel}: {summary}."
    else:
        first = _line_at(after, start)
        msg = (f"OK: {rel} изменён ({_diff_stat(d)}). Теперь:\n"
               + _snippet(after, first, first + new.rstrip("\n").count("\n")))
    return ToolResult(msg + _syntax_warning(rel, after), diff=d, path=rel)


def syntax_errors(root: Path, rels) -> list[str]:
    """Синтаксические ошибки в изменённых файлах (для автопроверки перед завершением хода)."""
    out = []
    for rel in sorted(rels):
        p = root / rel
        if p.is_file() and rel.endswith((".py", ".json")):
            w = _syntax_warning(rel, _read_text(p)[0])
            if w:
                out.append(f"{rel}:{w.split(':', 1)[1].rstrip()}")
    return out


def t_write(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = resolve(ctx.root, path)
    if p.is_dir():
        raise ToolError(f"{path} — это папка.")
    rel = relpath(ctx.root, p)
    before, nl = ("", "\n")
    if p.is_file():
        before, nl = _read_text(p)
        if ctx.snapshot:
            ctx.snapshot(rel, before if nl == "\n" else before.replace("\n", nl))
    elif ctx.snapshot:
        ctx.snapshot(rel, None)
    content = content.replace("\r\n", "\n")
    _write_text(p, content, nl)
    d = _diff(rel, before, content)
    _forget(ctx, rel)
    verb = "перезаписан" if before else "создан"
    return ToolResult(f"OK: {rel} {verb} ({content.count(chr(10)) + 1} строк)."
                      + _syntax_warning(rel, content), diff=d, path=rel)


def _forget(ctx: ToolContext, rel: str) -> None:
    for k in [k for k in ctx.seen if k[0] == rel]:
        del ctx.seen[k]


async def t_run(ctx: ToolContext, command: str, timeout: int | None = None) -> ToolResult:
    timeout = max(1, min(int(timeout or config.COMMAND_TIMEOUT), 1800))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "GIT_PAGER": "cat",
           "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "CI": "1"}
    cmd = command
    if SHELL_NAME == "PowerShell":
        cmd = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    # Popen + поток, а не asyncio-subprocess: тот не работает на SelectorEventLoop (Windows/uvicorn)
    proc = subprocess.Popen([*SHELL_CMD, cmd], cwd=str(ctx.root), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)

    def wait() -> tuple[bytes, bool]:
        try:
            return proc.communicate(timeout=timeout)[0], False
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            return proc.communicate()[0], True

    try:
        out, timed_out = await asyncio.to_thread(wait)
    except asyncio.CancelledError:
        _kill_tree(proc.pid)  # кнопка «Стоп»: убиваем дерево процессов
        raise
    code = proc.returncode
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    for _ in range(1):  # Windows-консоль иногда отдаёт cp866
        if "\ufffd" in text and sys.platform == "win32":
            text = out.decode("cp866", errors="replace").replace("\r\n", "\n").strip()
    head = f"exit {code}" if not timed_out else f"ТАЙМАУТ {timeout}s, процесс остановлен"
    return ToolResult(f"[{head}]\n{_trunc(text, 8000)}" if text else f"[{head}] (нет вывода)",
                      ok=(code == 0))


def _kill_tree(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15)
        else:
            os.killpg(pid, 9)
    except Exception:
        pass


SYNC = {"read": t_read, "glob": t_glob, "grep": t_grep, "edit": t_edit, "write": t_write}


async def execute(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    if name not in TOOL_NAMES:
        return ToolResult(f"Неизвестный инструмент {name}. Доступны: {', '.join(sorted(TOOL_NAMES))}.", ok=False)
    try:
        if name == "run":
            return await t_run(ctx, **args)
        return await asyncio.to_thread(SYNC[name], ctx, **args)
    except TypeError as e:
        return ToolResult(f"Неверные аргументы {name}: {e}", ok=False)
    except (ToolError, WorkspaceError) as e:
        return ToolResult(f"Ошибка: {e}", ok=False)
    except OSError as e:
        return ToolResult(f"Ошибка ввода-вывода: {e}", ok=False)
