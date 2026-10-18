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

from . import config, entities
from .workspace import WorkspaceError, find_venv, is_text_file, list_files, relpath, resolve, venv_bin

READ_LIMIT = 400        # строк по умолчанию
LINE_CHARS = 400        # обрезка длинных строк при чтении
BIG_READ = 150          # больше строк кода за раз — сначала показываем оглавление сущностей
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

PLAN_STATUSES = ("todo", "doing", "done", "failed")

TOOLS = [
    _fn("plan", "Set the task plan: the FULL list of small steps with statuses (replaces the previous "
                "list). Call it before any change, and again after each step to mark progress.",
        {"steps": {"type": "array", "items": {"type": "object", "properties": {
            "text": S, "status": {"type": "string", "enum": list(PLAN_STATUSES)}},
            "required": ["text", "status"]}}}, ["steps"]),
    _fn("symbols", "Project entity index (classes/functions with line ranges and docstrings). "
                   "path=file: its outline; query: search names+docstrings (words, `|` = or) in path; "
                   "use it to find code before read/grep.",
        {"query": S, "path": {**S, "description": "file or subdir"}}, []),
    _fn("read", "Read a text file. Returns numbered lines. Use offset/limit for big files, "
                "or `symbol` (e.g. Class.method) to read just that entity.",
        {"path": S, "symbol": {**S, "description": "class/function name from symbols"},
         "offset": {**I, "description": "1-based start line"},
         "limit": {**I, "description": f"lines, default {READ_LIMIT}"}}, ["path"]),
    _fn("glob", "Find files by glob pattern (e.g. **/*.py, src/**/test_*.js).",
        {"pattern": S, "path": {**S, "description": "subdir to search in"}}, ["pattern"]),
    _fn("grep", "Search file contents with a regex. Returns path:line: text.",
        {"pattern": S, "path": {**S, "description": "file or subdir"},
         "glob": {**S, "description": "file filter, e.g. *.py"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
    _fn("edit", "Replace an exact unique snippet in a file. `old` must match the file "
                "exactly (copy from read output without line numbers, keep trailing \\ etc.); "
                "include enough lines to be unique. Plain text, NOT patch/diff format. "
                "Empty `old` appends `new` to the end of file. "
                "Prefer several small edits over rewriting.",
        {"path": S, "old": S, "new": S,
         "replace_all": {"type": "boolean", "description": "only for renames: replace every occurrence"}},
        ["path", "old", "new"]),
    _fn("write", "Create a new file or fully overwrite one (prefer edit for changes).",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("run", f"Run a shell command ({SHELL_NAME}) in the project root: tests, linters, git, "
               f"installs. Non-interactive only. Timeout {config.COMMAND_TIMEOUT}s.",
        {"command": S, "timeout": {**I, "description": "seconds"}}, ["command"]),
    _fn("git_commit", "Stage, commit and optionally push changes. ALWAYS needs user approval. Use only "
                      "when the task asks for it, after the work is verified. Never use `run` for git add/commit/push.",
        {"message": {**S, "description": "commit message, first line <= 72 chars"},
         "summary": {**S, "description": "what was changed and why, per file"},
         "tests": {**S, "description": "which checks/tests you ran and their results (or why none)"},
         "files": {"type": "array", "items": S, "description": "paths to commit; default: all changes"},
         "push": {"type": "boolean", "description": "push to remote after commit"}},
        ["message", "summary", "tests"]),
]
TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
MUTATING = {"edit", "write"}
ICONS = {"todo": "[ ]", "doing": "[>]", "done": "[x]", "failed": "[!]"}


def normalize_plan(steps) -> list[dict]:
    """Проверка и нормализация аргумента plan. Бросает ToolError с понятной причиной."""
    if not isinstance(steps, list) or not steps:
        raise ToolError("steps должен быть непустым списком шагов {text, status}.")
    if len(steps) < 2:
        raise ToolError("В плане один шаг. plan принимает ПОЛНЫЙ список шагов со статусами (он заменяет "
                        "прежний): чтобы отметить шаг, пришли весь план с обновлённым статусом. Новый план — "
                        "мелкие шаги (по одному изменению на шаг), последним — общая проверка.")
    if len(steps) > 40:
        raise ToolError("Слишком много шагов (>40): объедини мелочи или вынеси часть в следующий план.")
    out = []
    for st in steps:
        if isinstance(st, str):
            st = {"text": st, "status": "todo"}
        if not isinstance(st, dict) or not str(st.get("text", "")).strip():
            raise ToolError("Каждый шаг — объект {text, status}.")
        status = st.get("status", "todo")
        status = {"pending": "todo", "in_progress": "doing", "completed": "done"}.get(status, status)
        if status not in PLAN_STATUSES:
            raise ToolError(f"status должен быть одним из: {', '.join(PLAN_STATUSES)}.")
        out.append({"text": str(st["text"]).strip()[:300], "status": status})
    if sum(s["status"] == "doing" for s in out) > 1:
        raise ToolError("Одновременно в работе (doing) может быть только один шаг.")
    return out


def render_plan(steps: list[dict]) -> str:
    return "\n".join(f"{ICONS[s['status']]} {n}. {s['text']}" for n, s in enumerate(steps, 1))


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
def t_symbols(ctx: ToolContext, query: str = "", path: str = "") -> ToolResult:
    if path:
        resolve(ctx.root, path)  # проверка выхода за пределы проекта
    return ToolResult(_trunc(entities.outline(ctx.root, path, query)))


def t_read(ctx: ToolContext, path: str, offset: int = 1, limit: int = READ_LIMIT,
           symbol: str = "") -> ToolResult:
    p = resolve(ctx.root, path)
    if symbol and p.is_file():
        try:
            e = entities.find(ctx.root, relpath(ctx.root, p), symbol)
        except LookupError as err:
            raise ToolError(str(err))
        offset, limit = e.line, e.end - e.line + 1
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
    # большой кусок кода без выбора сущности: сначала оглавление (экономия контекста);
    # повторный такой же запрос выполняется — модель не застревает
    span = min(limit, len(lines) - offset + 1)
    if (not symbol and span > BIG_READ and entities.supported(rel)
            and ctx.seen.get(("outline", rel)) != mtime):
        ctx.seen[("outline", rel)] = mtime
        return ToolResult(
            f"{rel}: {len(lines)} строк — вместо всего файла оглавление. Прочитай только нужное: "
            f"read с symbol=<имя> или offset/limit (≤{BIG_READ} строк). Повтори этот же вызов, "
            f"если действительно нужен весь диапазон.\n" + _trunc(entities.outline(ctx.root, rel)),
            path=rel)
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
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error:  # «Rotation(» и т.п. — модель имела в виду буквальную строку
        rx = re.compile(re.escape(pattern), flags)
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


_PATCH_MARK = ("*** ", "@@")
_LINE_NO = re.compile(r"^ *\d+│", re.M)


def _unpatch(old: str, new: str) -> tuple[str, str]:
    """
    gpt-oss обучена на формате apply_patch и иногда кладёт в `old` патч
    (строки с -/+, «*** End Patch», «@@»), оставляя `new` пустым.
    Раскладываем такой патч обратно на old/new; обычные аргументы не трогаем.
    """
    if new.strip():
        return old, new
    lines = [l for l in old.split("\n") if not l.startswith(_PATCH_MARK)]
    while lines and not lines[-1].strip():
        lines.pop()
    marked = [l for l in lines if l[:1] in "+-"]
    if len(marked) < 2 or not any(l.startswith("+") for l in marked):
        return old, new
    # строка контекста перед блоком «-», которая затем повторена первой «+»-строкой блока,
    # на деле тоже заменяется (модель забыла поставить «-») — иначе получится дубль
    for k, l in enumerate(lines):
        if l[:1] in "+-" or k + 1 >= len(lines) or not lines[k + 1].startswith("-"):
            continue
        j = k + 1
        while j < len(lines) and lines[j].startswith("-"):
            j += 1
        if j < len(lines) and lines[j].startswith("+") and lines[j][1:] == l:
            lines[k] = "-" + l
    o, n = [], []
    for l in lines:
        if l.startswith("-"):
            o.append(l[1:])
        elif l.startswith("+"):
            n.append(l[1:])
        else:  # контекст: «␠строка» в unified-формате или строка без префикса
            ctx_line = l[1:] if l.startswith(" ") and not l.startswith("  ") else l
            o.append(ctx_line)
            n.append(ctx_line)
    return "\n".join(o), "\n".join(n)


def _plan_edit(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False):
    """Вычисляет правку без записи. Возвращает (p, rel, text, after, nl, start, new, summary)."""
    p = resolve(ctx.root, path)
    if not p.is_file():
        raise ToolError(f"Файл не найден: {path}. Для нового файла используйте write.")
    rel = relpath(ctx.root, p)
    text, nl = _read_text(p)
    old, new = _unpatch(old.replace("\r\n", "\n"), new.replace("\r\n", "\n"))
    # модель копирует номера строк из вывода read («129│код») — маркер «│» наш, срезаем его
    old, new = _LINE_NO.sub("", old), _LINE_NO.sub("", new)
    if old == new:
        raise ToolError("old и new совпадают.")
    if not old:  # дописать в конец файла
        base = text if not text or text.endswith("\n") else text + "\n"
        new = new if new.endswith("\n") else new + "\n"
        return p, rel, text, base + new, nl, len(base), new, ""
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
            raise ToolError("old не найден в файле (нужно точное совпадение, с отступами; не используй "
                            "формат патча с -/+). Перечитай нужный фрагмент через read." + _closest(text, old))
        start = span[0]
        after = text[:span[0]] + new + text[span[1]:]
    summary = ""
    if replace_all and len(positions) > 1:
        lines = ", ".join(str(_line_at(text, x)) for x in positions[:20])
        summary = f"заменено {len(positions)} вхождений (строки {lines})"
    return p, rel, text, after, nl, start, new, summary


def t_edit(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
    p, rel, text, after, nl, start, new, summary = _plan_edit(ctx, path, old, new, replace_all)
    return _finish_edit(ctx, p, rel, text, after, nl, start, new, summary)


def preview(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    """
    Сухой прогон перед запросом подтверждения: битая правка сразу уходит
    модели с ошибкой (пользователя не спрашиваем), рабочая — даёт настоящий дифф для карточки.
    """
    try:
        if name == "edit":
            _, rel, text, after, *_ = _plan_edit(ctx, **args)
            return ToolResult("", diff=_diff(rel, text, after), path=rel)
        if name == "write":
            p = resolve(ctx.root, args["path"])
            if p.is_dir():
                raise ToolError(f"{args['path']} — это папка.")
            rel = relpath(ctx.root, p)
            before = _read_text(p)[0] if p.is_file() else ""
            return ToolResult("", diff=_diff(rel, before, str(args["content"]).replace("\r\n", "\n")), path=rel)
    except (KeyError, TypeError) as e:
        return ToolResult(f"Неверные аргументы {name}: {e}", ok=False)
    except (ToolError, WorkspaceError) as e:
        return ToolResult(f"Ошибка: {e}", ok=False)
    return ToolResult("")


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


_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$|conftest\.py$)")


def unused_modules(root: Path, rels) -> list[str]:
    """Созданные .py-модули, которые не импортирует ни один рабочий (не тестовый) модуль проекта."""
    new = [r for r in rels if r.endswith(".py") and not _TEST_PATH.search(r)
           and not r.endswith("__main__.py") and (root / r).is_file()]
    if not new:
        return []
    sources = {}
    for f in list_files(root):
        if f.endswith(".py") and not _TEST_PATH.search(f):
            try:
                sources[f] = (root / f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    out = []
    for rel in new:
        p = Path(rel)
        name = p.stem
        if p.name == "__init__.py":  # новый пакет проверяем по имени папки
            pkg = p.parent.as_posix() + "/"
            if any(f.startswith(pkg) and f not in new for f in sources):
                continue  # __init__ в уже существовавшем пакете
            name = p.parent.name
        rx = re.compile(rf"^\s*(from|import)\s[^\n#]*\b{re.escape(name)}\b", re.M)
        if not any(rx.search(text) for f, text in sources.items() if f != rel):
            out.append(rel)
    return out


def _imported_names(tree: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in n.names if a.name != "*")
    return out


def unused_new_imports(root: Path, originals: dict) -> list[str]:
    """
    Импорты, добавленные агентом в существующие файлы и нигде в файле не используемые, —
    частый признак «интеграции для галочки» (импорт есть, а код не изменён).
    """
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if before is None or not rel.endswith(".py") or rel.endswith("__init__.py") or not p.is_file():
            continue
        try:
            old, new = ast.parse(before), ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        added = _imported_names(new) - _imported_names(old)
        if not added:
            continue
        used = {n.id for n in ast.walk(new) if isinstance(n, ast.Name)}
        used |= {c.value for c in ast.walk(new) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
        out += [f"{rel}: импорт {name} добавлен, но не используется" for name in sorted(added - used)]
    return out


def _is_stub(fn: ast.AST) -> bool:
    """Тело функции без реальной работы: pass / ... / return []/None/{} / raise NotImplementedError."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]  # докстринг
    if not body:
        return True
    if len(body) != 1:
        return False
    st = body[0]
    if isinstance(st, ast.Pass) or (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)):
        return True
    if isinstance(st, ast.Return):
        v = st.value
        return v is None or (isinstance(v, ast.Constant) and v.value is None) or (
            isinstance(v, (ast.List, ast.Tuple, ast.Set)) and not v.elts) or (isinstance(v, ast.Dict) and not v.keys)
    if isinstance(st, ast.Raise) and st.exc is not None:
        e = st.exc.func if isinstance(st.exc, ast.Call) else st.exc
        return isinstance(e, ast.Name) and e.id == "NotImplementedError"
    return False


def _stub_names(tree: ast.AST) -> set[str]:
    out = set()

    def walk(body, prefix=""):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                abstract = any(getattr(d, "id", getattr(d, "attr", "")) == "abstractmethod" for d in n.decorator_list)
                if not abstract and _is_stub(n):
                    out.add(prefix + n.name)
            elif isinstance(n, ast.ClassDef):
                walk(n.body, prefix + n.name + ".")

    walk(tree.body)
    return out


def new_stubs(root: Path, originals: dict) -> list[str]:
    """Функции/методы-заглушки, появившиеся в правках агента (не в тестах, не abstractmethod)."""
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if not rel.endswith(".py") or _TEST_PATH.search(rel) or not p.is_file():
            continue
        try:
            new = _stub_names(ast.parse(p.read_text(encoding="utf-8", errors="replace")))
            old = _stub_names(ast.parse(before)) if before else set()
        except SyntaxError:
            continue
        out += [f"{rel}: {name} — заглушка без реальной работы" for name in sorted(new - old)]
    return out


def _defs(tree: ast.AST) -> dict[str, ast.AST]:
    out = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = n
        elif isinstance(n, ast.ClassDef):
            out.update({f"{n.name}.{m.name}": m for m in n.body
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))})
    return out


def _src(text: str, node: ast.AST, cap: int = 16) -> str:
    """Исходник функции без докстринга (он съедает место и не влияет на поведение)."""
    lines = (ast.get_source_segment(text, node) or "").splitlines()
    body = node.body
    if len(body) > 1 and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        lines = lines[:body[0].lineno - node.lineno] + text.splitlines()[body[1].lineno - 1:node.end_lineno]
    return "\n".join(lines[:cap] + (["    …"] if len(lines) > cap else []))


def _delegate(fn: ast.AST) -> str | None:
    """Имя функции, которой теперь целиком делегировано тело (return f(...) / f(...)), иначе None."""
    body = [b for b in fn.body if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant))]
    if len(body) != 1:
        return None
    v = body[0].value if isinstance(body[0], (ast.Return, ast.Expr)) else None
    if isinstance(v, ast.Call):
        f = v.func
        return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
    return None


def merged_functions(root: Path, originals: dict) -> list[str]:
    """
    Функции, которые агент заменил общей реализацией (удалил и импортировал одноимённую из модуля
    проекта или свёл тело к вызову другой функции). Возвращает «было / стало» для сверки поведения:
    маленькая модель объединяет «похожие» функции, не замечая различий в формате, порядке, типах.
    """
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if not before or not rel.endswith(".py") or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            old_t, new_t = ast.parse(before), ast.parse(text)
        except SyntaxError:
            continue
        old_d, new_d = _defs(old_t), _defs(new_t)
        imports = {}
        for n in ast.walk(new_t):
            if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                for a in n.names:
                    imports[a.asname or a.name] = (n.module, a.name)
        for name, old_fn in old_d.items():
            new_fn = new_d.get(name)
            if new_fn is not None and ast.dump(old_fn) == ast.dump(new_fn):
                continue
            target = name if new_fn is None else _delegate(new_fn)
            if not target or (new_fn is None and name not in imports):
                continue
            if target not in imports:  # делегирование функции из этого же файла
                tgt = new_d.get(target)
                where, tgt_text = rel, text
            else:
                mod, real = imports[target]
                f = _module_file(root, mod)
                if f is None or f.is_dir():
                    continue
                tgt_text = f.read_text(encoding="utf-8", errors="replace")
                tgt = _defs(ast.parse(tgt_text)).get(real)
                where = relpath(root, f)
            if tgt is None or _body_key_eq(old_fn, tgt):
                continue
            out.append(f"{rel}: {name} заменена на {target} из {where}.\nБыло:\n{_src(before, old_fn)}\n"
                       f"Стало ({target}):\n{_src(tgt_text, tgt)}"
                       + (f"\nВызов в {rel}:\n{_src(text, new_fn)}" if new_fn is not None else ""))
    return out[:6]


def _body_key_eq(a: ast.AST, b: ast.AST) -> bool:
    strip = lambda f: [x for x in f.body if not (isinstance(x, ast.Expr) and isinstance(x.value, ast.Constant))]
    return [ast.dump(x) for x in strip(a)] == [ast.dump(x) for x in strip(b)]


def project_tests(root: Path, timeout: int = 600) -> tuple[bool, str] | None:
    """
    Прогон тестов проекта (pytest через venv проекта) для автопроверки в конце хода.
    None — тестов нет или pytest недоступен; иначе (прошли ли, хвост вывода).
    """
    has_tests = (root / "tests").is_dir() or any(root.glob("test_*.py")) or (root / "pytest.ini").is_file()
    if not has_tests:
        return None
    venv = find_venv(root)
    py = str(venv_bin(venv) / ("python.exe" if (venv_bin(venv) / "python.exe").is_file() else "python")) \
        if venv else sys.executable
    try:
        p = subprocess.run([py, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=str(root),
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "CI": "1"})
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 5 or "No module named pytest" in out:  # 5 — тесты не найдены
        return None
    return p.returncode == 0, _trunc(out, 3000)


_NO_IMPORT = re.compile(r"(^|/)(main|app|manage|setup|conftest|__main__|run[^/]*)\.py$")


def import_errors(root: Path, rels, timeout: int = 90) -> list[str]:
    """
    Импорт созданных/изменённых модулей интерпретатором проекта: ловит то, что не видит
    ast и не ловят тесты, если модуль никто не загружает (битые относительные импорты, опечатки
    в именах). Точки входа (main.py и т.п.) не импортируются — у них побочные эффекты.
    """
    mods, static = [], []
    for rel in sorted(rels):
        if rel.endswith(".py") and _NO_IMPORT.search(rel) and (root / rel).is_file():
            static += _missing_imports(root, rel)
        if not rel.endswith(".py") or _TEST_PATH.search(rel) or _NO_IMPORT.search(rel):
            continue
        if not (root / rel).is_file():
            continue
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and all(x.isidentifier() for x in parts):
            mods.append(".".join(parts))
    if not mods:
        return static
    venv = find_venv(root)
    py = str(venv_bin(venv) / ("python.exe" if (venv_bin(venv) / "python.exe").is_file() else "python")) \
        if venv else sys.executable
    # кроме ошибок импорта ищем классы, ставшие абстрактными: унаследовали abstractmethod
    # от нового базового класса и не реализовали его — их экземпляр больше не создать
    code = ("import importlib, inspect, sys, traceback\n"
            "for m in sys.argv[1:]:\n"
            "    try:\n        mod = importlib.import_module(m)\n"
            "    except Exception as e:\n"
            "        tb = traceback.extract_tb(e.__traceback__)[-1]\n"
            "        print(f'{m}: {type(e).__name__}: {e} ({tb.filename.rsplit(chr(92), 1)[-1]}:{tb.lineno})')\n"
            "        continue\n"
            "    for name, obj in vars(mod).items():\n"
            "        if not inspect.isclass(obj) or obj.__module__ != m or not inspect.isabstract(obj):\n"
            "            continue\n"
            "        own = {k for k, v in vars(obj).items() if getattr(v, '__isabstractmethod__', False)}\n"
            "        missing = sorted(set(obj.__abstractmethods__) - own)\n"
            "        if missing:\n"
            "            print(f'{m}: класс {name} абстрактный — не реализованы {\", \".join(missing)}; '\n"
            "                  f'{name}() упадёт с TypeError')\n"
            # реестр, где разные ключи указывают на один объект, — обычно ошибка импорта (алиас не того имени)
            "    for name, obj in vars(mod).items():\n"
            "        if isinstance(obj, dict) and len(obj) >= 2 and all(callable(v) for v in obj.values()):\n"
            "            seen = {}\n"
            "            for k, v in obj.items():\n"
            "                if id(v) in seen:\n"
            "                    print(f'{m}: в {name} ключи {seen[id(v)]!r} и {k!r} указывают на один объект '\n"
            "                          f'{getattr(v, \"__module__\", \"?\")}.{getattr(v, \"__qualname__\", v)} — '\n"
            "                          f'проверь, что импортирован нужный класс')\n"
            "                seen.setdefault(id(v), k)\n")
    try:
        p = subprocess.run([py, "-c", code, *mods], cwd=str(root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(root)})
    except (OSError, subprocess.TimeoutExpired):
        return static
    return static + [l for l in p.stdout.splitlines() if l.strip()][:20]


def _module_file(root: Path, mod: str) -> Path | None:
    base = root.joinpath(*mod.split("."))
    for p in (base.with_suffix(".py"), base / "__init__.py"):
        if p.is_file():
            return p
    return base if base.is_dir() else None  # пакет без __init__ (namespace)


def _top_names(path: Path) -> set[str] | None:
    """Имена верхнего уровня модуля; None — если узнать нельзя (import *, __getattr__, ошибка)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return None
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" for a in n.names):
                return None
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
    return None if "__getattr__" in names else names


def _missing_imports(root: Path, rel: str) -> list[str]:
    """
    Статическая проверка импортов точки входа (её нельзя импортировать — побочные эффекты):
    модули проекта и имена из них должны существовать.
    """
    try:
        tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, OSError):
        return []
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            mods, names = [n.module], [a.name for a in n.names]
        elif isinstance(n, ast.Import):
            mods, names = [a.name for a in n.names], []
        else:
            continue
        for mod in mods:
            first = mod.split(".")[0]
            if not ((root / first).is_dir() or (root / f"{first}.py").is_file()):
                continue  # не модуль проекта (stdlib, пакет из venv)
            f = _module_file(root, mod)
            if f is None:
                out.append(f"{rel}:{n.lineno}: модуль {mod} не найден в проекте")
                continue
            if not names or f.is_dir():
                continue
            have = _top_names(f)
            for name in names:
                if have is not None and name != "*" and name not in have \
                        and _module_file(root, f"{mod}.{name}") is None:
                    out.append(f"{rel}:{n.lineno}: в {mod} нет имени {name}")
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
    venv = find_venv(ctx.root)
    if venv:  # python/pytest проекта, а не системные
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = str(venv_bin(venv)) + os.pathsep + env.get("PATH", "")
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


# ---------------- git: add / commit / push (только через подтверждение) ----------------
# git add/commit/push в обход git_commit (через run) запрещены — иначе мимо подтверждения
GIT_WRITE = re.compile(r"\bgit\s+(?:-[cC]\s+\S+\s+)*(add|commit|push|stash|reset|rebase|merge|tag)\b")
SECRET_FILE = re.compile(r"(^|/)(\.env(\..+)?|.*\.(pem|key|p12|pfx)|id_(rsa|ed25519)(\.pub)?|credentials.*)$",
                         re.IGNORECASE)
PROTECTED_BRANCHES = ("main", "master")


def _git(root: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C.UTF-8"}
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def git_preview(ctx: ToolContext, message: str = "", summary: str = "", tests: str = "",
                files: list | None = None, push: bool = False) -> dict:
    """Проверка аргументов и данные для карточки подтверждения. Бросает ToolError."""
    if not message.strip():
        raise ToolError("Пустое сообщение коммита (message).")
    if not summary.strip():
        raise ToolError("Опиши изменения (summary): что и зачем изменено в каждом файле.")
    if not tests.strip():
        raise ToolError("Опиши проведённые проверки (tests): что запускал и с каким результатом; "
                        "если не тестировал — так и напиши с причиной.")
    if not (ctx.root / ".git").exists():
        raise ToolError("Проект не является git-репозиторием.")
    branch = _git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch or branch == "HEAD":
        raise ToolError("Не удалось определить ветку (detached HEAD?).")
    p = _git(ctx.root, "status", "--porcelain", "-z", "--untracked-files=all")
    entries = [e for e in p.stdout.split("\0") if e]
    changed: dict[str, str] = {}
    skip = False
    for e in entries:  # формат -z: «XY путь», у переименований следом идёт исходный путь
        if skip:
            skip = False
            continue
        code, path = e[:2], e[3:]
        changed[path] = code
        skip = code[0] in "RC"
    if files:
        want = [str(f).replace("\\", "/").removeprefix("./") for f in files]
        missing = [f for f in want if f not in changed]
        if missing:
            raise ToolError(f"Нет изменений в файлах: {', '.join(missing)}. Изменены: {', '.join(changed) or '—'}")
        changed = {f: changed[f] for f in want}
    if not changed:
        raise ToolError("Нечего коммитить: рабочая копия чистая.")
    secrets = [f for f in changed if SECRET_FILE.search(f)]
    if secrets:
        raise ToolError(f"Похоже на секреты, коммитить нельзя: {', '.join(secrets)}. Укажи files без них.")
    upstream = _git(ctx.root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if push and not _git(ctx.root, "remote").stdout.strip():
        raise ToolError("Нет remote — push невозможен. Сделай коммит без push.")
    stat = "\n".join(f"{code} {path}" for path, code in changed.items())
    short = _git(ctx.root, "diff", "HEAD", "--shortstat", "--", *changed).stdout.strip()
    return {"message": message.strip(), "summary": summary.strip(), "tests": tests.strip(),
            "branch": branch, "push": bool(push),
            "upstream": upstream.stdout.strip() if upstream.returncode == 0 else None,
            "protected": branch in PROTECTED_BRANCHES, "files": list(changed),
            "stat": stat + (f"\n{short}" if short else "")}


def t_git_commit(ctx: ToolContext, **args) -> ToolResult:
    g = git_preview(ctx, **args)
    body = f"{g['message']}\n\nИзменения:\n{g['summary']}\n\nПроверки:\n{g['tests']}\n"
    steps = []
    for cmd in (["add", "-A", "--", *g["files"]], ["commit", "-q", "-F", "-"]):
        p = subprocess.run(["git", *cmd], cwd=str(ctx.root), input=body if cmd[0] == "commit" else None,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if p.returncode != 0:
            raise ToolError(f"git {cmd[0]}: {(p.stderr or p.stdout).strip()[-800:]}")
    sha = _git(ctx.root, "rev-parse", "--short", "HEAD").stdout.strip()
    steps.append(f"Коммит {sha} в ветке {g['branch']} ({len(g['files'])} файлов).")
    if g["push"]:
        cmd = ["push"] if g["upstream"] else ["push", "-u", "origin", g["branch"]]
        p = _git(ctx.root, *cmd, timeout=180)
        if p.returncode != 0:
            return ToolResult("\n".join(steps) + f"\nPush не удался: {(p.stderr or p.stdout).strip()[-800:]}",
                              ok=False)
        steps.append(f"Push выполнен → {g['upstream'] or 'origin/' + g['branch']}.")
    return ToolResult("OK: " + " ".join(steps))


SYNC = {"git_commit": t_git_commit, "symbols": t_symbols, "read": t_read, "glob": t_glob, "grep": t_grep, "edit": t_edit, "write": t_write}


async def execute(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    if name not in TOOL_NAMES:
        return ToolResult(f"Неизвестный инструмент {name}. Доступны: {', '.join(sorted(TOOL_NAMES))}.", ok=False)
    if name == "plan":  # состояние плана живёт в сессии — обрабатывает агент
        return ToolResult("plan обрабатывается агентом.", ok=False)
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
