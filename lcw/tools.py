"""
Инструменты агента. Описания (схемы) намеренно короткие: они входят в каждый
запрос, и каждый лишний токен — время префилла. Результаты для модели тоже
компактные; подробности (диффы) уходят только в UI.
"""
from __future__ import annotations

import ast
import asyncio
import base64
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

from . import config, documents, entities
from .workspace import WorkspaceError, find_venv, is_text_file, list_files, main_checkout, pytest_env, relpath, resolve, venv_bin

READ_LIMIT = 400        # строк по умолчанию
LINE_CHARS = 400        # обрезка длинных строк при чтении
BIG_READ = 150          # больше строк кода за раз — сначала показываем оглавление сущностей
GREP_MAX = 80           # совпадений

def _shell() -> tuple[list[str], str]:
    """Определяет команду оболочки для выполнения: bash на Linux/WSL, Git Bash на Windows или PowerShell."""
    if sys.platform != "win32":
        return ["bash", "-lc"], "bash"
    for p in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        # bash из WSL (System32) не видит пути Windows — берём только Git Bash
        if p and Path(p).is_file() and "system32" not in p.lower():
            return [p, "-c"], "bash (Git Bash on Windows)"
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command"], "PowerShell"


SHELL_CMD, SHELL_NAME = _shell()


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    """Создаёт словарь описания функции для LLM, включая имя, описание и параметры."""
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
    _fn("decide", "Record the design decision for the current request item BEFORE editing it: what exactly "
                  "changes, which EXISTING project entities it is built on, what happens when data is missing.",
        {"decision": {**S, "description": "concrete rule: criterion/metric/threshold or structure, where it plugs in"},
         "uses": {"type": "array", "items": S,
                  "description": "existing functions/classes/settings it relies on (real names from the code)"},
         "fallback": {**S, "description": "behaviour when data is missing or the check cannot run"},
         "cause": {**S, "description": "the problem this solves and its cause, with evidence you found (log "
                                       "times, counts, file:line); '-' only if the item is not about a problem"},
         "effect": {**S, "description": "what the new rule does in the found case (same times/numbers) and in the "
                                        "same case from another address or twice as strong"}},
        ["decision", "uses", "fallback", "cause"]),
    _fn("symbols", "Project entity index (classes/functions with line ranges and docstrings). "
                   "path=file: its outline; query: search names+docstrings (words, `|` = or) in path; "
                   "use it to find code before read/grep.",
        {"query": S, "path": {**S, "description": "file or subdir"}}, []),
    _fn("read", "Read a text file (also .pdf/.docx: their text, PDF pages marked `=== стр. N ===`). "
                "Returns numbered lines. Use offset/limit for big files, "
                "or `symbol` (e.g. Class.method) to read just that entity.",
        {"path": S, "symbol": {**S, "description": "class/function name from symbols"},
         "offset": {**I, "description": "1-based start line"},
         "limit": {**I, "description": f"lines, default {READ_LIMIT}"}}, ["path"]),
    _fn("glob", "Find files by glob pattern (e.g. **/*.py, src/**/test_*.js).",
        {"pattern": S, "path": {**S, "description": "subdir to search in"}}, ["pattern"]),
    _fn("grep", "Search file contents with a regex (also inside .pdf/.docx text). Returns path:line: text.",
        {"pattern": S, "path": {**S, "description": "file or subdir"},
         "glob": {**S, "description": "file filter, e.g. *.py"},
         "ignore_case": {"type": "boolean"}}, ["pattern"]),
    _fn("refs", "All references of a name (whole word): definition, imports, uses, each with the enclosing "
                "function; for snake_case settings also NAME_UPPER (.env). Use before renaming or changing "
                "a function/setting to find every place to update.",
        {"name": {**S, "description": "identifier, e.g. guard_ip_limit or Guard.analyze"},
         "path": {**S, "description": "file or subdir"}}, ["name"]),
    _fn("test", "Run the project's pytest (project venv) and get a SHORT report: each failure's test, "
                "location and `E` lines. path = test file/dir, or a source module to run the tests that "
                "import it; keyword = -k filter. Use it to verify each step.",
        {"path": S, "keyword": S, "timeout": {**I, "description": "seconds"}}, []),
    _fn("logstats", "Summarize a log file or folder instead of reading it: line count, time range, top values, "
                    "peaks over time. Works with JSON lines (nginx) and text logs. Use it to find evidence "
                    "of a problem (errors, attacks, outages) before changing code.",
        {"path": {**S, "description": "log file or folder (e.g. logs)"},
         "match": {**S, "description": "regex filter of lines, e.g. ERROR|503"},
         "by": {**S, "description": "JSON field (status, ip, uri…) or level | message: top values"},
         "bucket": {"type": "string", "enum": ["minute", "hour", "day"], "description": "histogram over time"},
         "show": {**I, "description": "also show the last N matching lines (<=30)"}}, ["path"]),
    _fn("edit", "Replace an exact unique snippet in a file. `old` must match the file "
                "exactly (copy from read output without line numbers, keep trailing \\ etc.); "
                "include enough lines to be unique. Plain text, NOT patch/diff format. "
                "Empty `old` appends `new` to the end of file. "
                "Prefer several small edits over rewriting.",
        {"path": S, "old": S, "new": S,
         "replace_all": {"type": "boolean", "description": "only for renames: replace every occurrence"}},
        ["path", "old", "new"]),
    _fn("write", "Create a new file or fully overwrite one (prefer edit for changes). For a .docx path "
                 "give Markdown (# headings, - lists, **bold**; formulas in plain Unicode: ∩ ∪ Σ C(n,k) 2ⁿ): "
                 "it becomes a Word document. Big document: write the first section, then append each next "
                 "one with edit (empty old).",
        {"path": S, "content": S}, ["path", "content"]),
    _fn("delete", "Delete a file of the project (it can be restored by rewind).", {"path": S}, ["path"]),
    _fn("libdoc", "Real usage of an INSTALLED third-party library, read from its own files: example programs "
                  "(the library's `__main__` demos), definitions with docstrings matching the query, and its "
                  "built-in assets (textures, models, fonts). Use it before writing code with a library you are "
                  "not sure about, and when the app runs but behaves or looks wrong.",
        {"package": {**S, "description": "import name, e.g. ursina, pygame, arcade"},
         "query": {**S, "description": "API words, e.g. 'camera look_at update held_keys'"}}, ["package"]),
    _fn("launch", "Start a GUI/desktop app or game (e.g. `python main.py`), let it run, optionally press keys or "
                  "click, take screenshots of its window, then stop it. Reports whether a window opened, crashes "
                  "(traceback) and what is on screen. Use it to verify apps that `run` cannot (they never exit).",
        {"command": S, "seconds": {**I, "description": "how long to run before stopping, default 6"},
         "steps": {"type": "array", "items": S, "description": "after the window opens, in order: key names "
                   "(enter, esc, space, up, down, left, right, a..z, 1..9, f1..f12), 'wait:1.5', 'click:X,Y' "
                   "(window coords), 'shot' (screenshot now)"},
         "look": {**S, "description": "question about the screenshots for a vision model, e.g. 'is a menu with "
                  "buttons visible?'"}}, ["command"]),
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
MUTATING = {"edit", "write", "delete"}
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
    """Исключение для ошибок инструментов."""
    pass


@dataclass
class ToolResult:
    """Результат работы инструмента: вывод, статус, diff и путь."""
    output: str                       # для модели (компактно)
    ok: bool = True
    diff: str = ""                    # для UI
    path: str = ""
    shots: list = field(default_factory=list)  # скриншоты launch: [{path, label, size, blank}]


@dataclass
class ToolContext:
    """Контекст инструмента: корень проекта, кеш чтений и снимок для отката."""
    root: Path
    # path -> (mtime_ns, offset, limit): что модель уже видела (дедупликация чтений)
    seen: dict = field(default_factory=dict)
    # вызывается перед изменением файла: сохранить оригинал для отката
    snapshot: callable = None


def _trunc(text: str, limit: int = config.TOOL_OUTPUT_CHARS) -> str:
    """Обрезает длинный текст до заданного лимита, вставляя информацию об обрезке."""
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
    """Записывает текст в файл, создавая директорию при необходимости, с поддержкой пользовательского
    разделителя строк.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace("\n", nl) if nl != "\n" else text
    p.write_bytes(data.encode("utf-8"))


def _diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        f"a/{path}", f"b/{path}", n=2))


def _diff_stat(diff: str) -> str:
    """Подсчитывает добавленные и удалённые строки в diff‑строке."""
    add = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    rem = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    return f"+{add} -{rem}"


# ---------------- реализация ----------------
def t_symbols(ctx: ToolContext, query: str = "", path: str = "") -> ToolResult:
    """Возвращает список символов (функций, классов) в файле или проекте."""
    if path:
        resolve(ctx.root, path)  # проверка выхода за пределы проекта
    return ToolResult(_trunc(entities.outline(ctx.root, path, query)))


def t_read(ctx: ToolContext, path: str, offset: int = 1, limit: int = READ_LIMIT,
           symbol: str = "") -> ToolResult:
    """Читает часть файла, учитывая символ, offset, limit, и кеширует результаты."""
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
        alt = _in_main(ctx, path)
        if alt is not None:  # worktree: файл из .gitignore (логи, данные) есть только в основной копии
            if alt.is_dir() or LOG_FILE.search(alt.name):
                return ToolResult(f"{path} есть только в основной копии проекта ({alt}). Логи смотри через "
                                  f"logstats path={path} (сводка, match=, by=, bucket=, show=).")
            raise ToolError(f"Файл не найден: {path} (есть только в основной копии проекта, не в этой ветке)")
        raise ToolError(f"Файл не найден: {path}")
    doc = documents.is_document(p)
    if not doc and not is_text_file(p):
        raise ToolError(f"Бинарный файл: {path}")
    rel = relpath(ctx.root, p)
    offset = max(1, int(offset or 1))
    limit = max(1, min(int(limit or READ_LIMIT), 2000))
    mtime = p.stat().st_mtime_ns
    key = (rel, offset, limit)
    if ctx.seen.get(key) == mtime:
        return ToolResult(f"{rel}: не изменился с прошлого чтения (содержимое выше в диалоге).", path=rel)
    if doc:
        try:
            # .docx, собранный write: показываем его Markdown — по нему работает edit
            text = (p.suffix.lower() == ".docx" and documents.docx_source(p)) or documents.document_text(p)
        except Exception as e:  # битый/зашифрованный документ
            raise ToolError(f"Не удалось извлечь текст из {path}: {e}")
    else:
        text, _ = _read_text(p)
    lines = text.split("\n") if text else []
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


LOG_FILE = re.compile(r"\.(log|out|err)(\.\d+)?(\.gz)?$|^access|^error", re.IGNORECASE)


def _in_main(ctx: ToolContext, path: str) -> Path | None:
    """Путь, которого нет в worktree, но есть в основной копии проекта (папки из .gitignore: logs/, data/)."""
    main = main_checkout(ctx.root)
    if not main or not path or resolve(ctx.root, path).exists():
        return None
    alt = resolve(main, path)
    return alt if alt.exists() else None


def t_glob(ctx: ToolContext, pattern: str, path: str = "") -> ToolResult:
    """Ищет файлы по шаблону в проекте, с учётом worktree‑папки."""
    alt = _in_main(ctx, path)
    if alt is not None and alt.is_dir():
        names = sorted(q.relative_to(alt).as_posix() for q in alt.rglob("*") if q.is_file())[:200]
        return ToolResult(f"{path} есть только в основной копии проекта ({alt}); файлы:\n"
                          + "\n".join(f"{path.rstrip('/')}/{n}" for n in names)
                          + "\nЛоги смотри через logstats (сводка без чтения целиком).")
    base = relpath(ctx.root, resolve(ctx.root, path or "."))
    base = "" if base == "." else base.rstrip("/") + "/"
    pat = pattern.strip().replace("\\", "/").removeprefix("./") or "*"  # пустой шаблон — всё в path
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
    """Поиск по тексту файлов с поддержкой регулярных выражений и ограничением количества совпадений."""
    flags = re.IGNORECASE if ignore_case else 0
    # Обрабатываем ошибку компиляции шаблона, чтобы не падать
    try:
        rx = re.compile(pattern, flags)
    except re.error:  # «Rotation(» и т.п. — модель имела в виду буквальную строку
        rx = re.compile(re.escape(pattern), flags)
    root, note = ctx.root, ""
    alt = _in_main(ctx, path)
    if alt is not None:  # worktree: логи и данные из .gitignore есть только в основной копии
        root = main_checkout(ctx.root)
        note = f"(основная копия проекта {root})\n"
        files = ([relpath(root, alt)] if alt.is_file() else
                 [relpath(root, q) for q in sorted(alt.rglob("*")) if q.is_file()])
    else:
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
        p = root / f
        try:
            if documents.is_document(p):  # .docx от write — по Markdown, как его показывает read
                text = (p.suffix.lower() == ".docx" and documents.docx_source(p)) or documents.document_text(p)
            elif not is_text_file(p) or p.stat().st_size > 2_000_000:
                continue
            else:
                text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                out.append(f"{f}:{i}: {line.strip()[:200]}")
                n += 1
                if n >= GREP_MAX:
                    out.append(f"…лимит {GREP_MAX} совпадений, уточните запрос")
                    return ToolResult(note + "\n".join(out))
    return ToolResult(note + ("\n".join(out) if out else "Нет совпадений."))



REFS_MAX = 80
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _enclosing(root: Path, rel: str, line: int) -> str:
    """Самая вложенная сущность (функция/метод/класс), в которую попадает строка; '' — уровень модуля."""
    if not entities.supported(rel):
        return ""
    best = None
    for e in entities.file_entities(root, rel)[1]:
        if e.line <= line <= e.end and (best is None or e.end - e.line < best.end - best.line):
            best = e
    return best.name if best else ""


def _scope_files(root: Path, path: str) -> list[str]:
    """Возвращает список файлов, соответствующих пути относительно корня; если путь указывает на файл,
    возвращает его относительный путь, иначе – все файлы, начинающиеся с указанного префикса.
    """
    target = resolve(root, path or ".")
    if target.is_file():
        return [relpath(root, target)]
    base = relpath(root, target)
    base = "" if base == "." else base + "/"
    return [f for f in list_files(root) if f.startswith(base)]


def t_refs(ctx: ToolContext, name: str, path: str = "") -> ToolResult:
    """
    Все упоминания имени (целым словом) с охватывающей функцией: где определено, где импортируется,
    где используется. Для snake_case-настроек ищется и ИМЯ_В_ВЕРХНЕМ_РЕГИСТРЕ (.env, документация).
    """
    name = name.strip().rsplit(".", 1)[-1]  # settings.guard_ip_limit -> guard_ip_limit
    if not _IDENT.match(name):
        return ToolResult("Ошибка: name — один идентификатор (функция, класс, переменная, настройка).", ok=False)
    variants = {name}
    if name.lower() == name and "_" in name:
        variants.add(name.upper())
    # Создаём регэксп, чтобы найти точные совпадения имени
    rx = re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, sorted(variants))) + r")(?![A-Za-z0-9_])")
    defn = re.compile(r"^\s*(?:async\s+def|def|class)\s+" + re.escape(name) + r"\b|^\s*" + re.escape(name)
                      + r"\s*(?::[^=]*)?=(?!=)")
    groups: dict[str, list[str]] = {}
    n = 0
    for f in _scope_files(ctx.root, path):
        p = ctx.root / f
        if not is_text_file(p) or p.stat().st_size > 2_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if not rx.search(line):
                continue
            # Определяем тип упоминания: определение, импорт или обычное использование
            kind = ("def" if defn.match(line) else
                    "import" if re.match(r"\s*(from\s+\S+\s+)?import\b", line) else "use")
            where = _enclosing(ctx.root, f, i)
            groups.setdefault(f, []).append(f"  {i} [{kind}{' in ' + where if where else ''}] {line.strip()[:160]}")
            n += 1
            # Останавливаем поиск, если достигнут лимит упоминаний
            if n >= REFS_MAX:
                break
        if n >= REFS_MAX:
            break
    if not groups:
        return ToolResult(f"«{name}» нигде не упоминается" + (f" в {path}" if path else "") + ".")
    tests = [f for f in groups if re.search(r"(^|/)(tests?/|test_)", f)]
    head = (f"{n} упоминаний «{'/'.join(sorted(variants))}» в {len(groups)} файлах"
            + (f" (лимит {REFS_MAX}, уточните path)" if n >= REFS_MAX else "")
            + (f"; тесты: {', '.join(tests)}" if tests else "; в тестах не упоминается") + ":")
    return ToolResult(head + "\n" + "\n".join(f"{f}\n" + "\n".join(lines) for f, lines in groups.items()))


def _pytest_python(root: Path) -> str:
    """Определяет исполняемый файл Python для проекта, используя виртуальное окружение, если оно найдено,
    иначе – системный интерпретатор.
    """
    venv = find_venv(root)
    if not venv:
        return sys.executable
    exe = venv_bin(venv) / ("python.exe" if (venv_bin(venv) / "python.exe").is_file() else "python")
    return str(exe)


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return rel.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def covering_tests(root: Path, rel: str) -> list[str]:
    """Тестовые файлы, которые импортируют модуль rel (app/guard/watch.py -> app.guard.watch)."""
    if not rel.endswith(".py"):
        return []
    mod = rel[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    last = mod.rsplit(".", 1)[-1]
    parent = mod.rsplit(".", 1)[0] if "." in mod else ""
    pats = [re.compile(r"\b(from|import)\s+" + re.escape(mod) + r"\b")]
    if parent:
        pats.append(re.compile(r"\bfrom\s+" + re.escape(parent) + r"\s+import\s+[^\n]*\b" + re.escape(last) + r"\b"))
    out = []
    for f in list_files(root):
        if not _is_test_file(f):
            continue
        try:
            text = (root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(p.search(text) for p in pats):
            out.append(f)
    return out


def _norm(text: str) -> str:
    return text.replace(chr(13) + chr(10), chr(10))


def revert_whitespace_only(root: Path, originals: dict) -> list[str]:
    """
    Файлы, отличающиеся от исходных только пробелами и пустыми строками, — вернуть как были.
    Модель удаляет свои неудачные вставки и оставляет хвосты пустых строк в чужих файлах.
    """
    out = []
    for rel, orig in originals.items():
        p = root / rel
        if orig is None or not p.is_file():
            continue
        try:
            now = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _norm(now) != _norm(orig) and re.sub(r"\s+", "", now) == re.sub(r"\s+", "", orig):
            p.write_bytes(orig.encode("utf-8"))
            out.append(rel)
    return out


def _test_names(text: str) -> set[str]:
    """Извлекает имена функций, начинающихся с «test», из исходного текста, игнорируя синтаксические ошибки.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return set()
    return {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test")}


def tests_pass_on_old_code(root: Path, originals: dict, timeout: int = 300) -> list[str] | None:
    """
    Новые тесты сессии, которые проходят и на коде ДО правок, — изменение они не проверяют.
    Рабочие файлы на время прогона возвращаются к исходным и затем восстанавливаются.
    None — проверить нельзя (нет новых тестов, pytest недоступен); [] — все новые тесты ловят изменение.
    """
    new_ids = []
    for rel, orig in originals.items():
        if _is_test_file(rel) and (root / rel).is_file():
            now = (root / rel).read_text(encoding="utf-8", errors="replace")
            new_ids += [f"{rel}::{n}" for n in sorted(_test_names(now) - _test_names(orig or ""))]
    sources = {rel: orig for rel, orig in originals.items() if not _is_test_file(rel) and rel.endswith(".py")}
    if not new_ids or not sources:
        return None
    current = {}
    try:
        for rel, orig in sources.items():
            p = root / rel
            current[rel] = p.read_bytes() if p.is_file() else None
            if orig is None:
                p.unlink(missing_ok=True)  # файл создан в сессии — до правок его не было
            else:
                p.write_bytes(orig.encode("utf-8"))
        res = subprocess.run([_pytest_python(root), "-m", "pytest", "-q", "-rA", "--tb=no", "-p", "no:cacheprovider",
                              *new_ids], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout,
                             env=pytest_env({**os.environ, "PYTHONIOENCODING": "utf-8", "CI": "1"}))
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        for rel, data in current.items():
            p = root / rel
            if data is None:
                p.unlink(missing_ok=True)
            else:
                p.write_bytes(data)
    passed = set(re.findall(r"^PASSED (\S+)", res.stdout, re.M))
    failed = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", res.stdout, re.M))
    # параметризованные тесты: test_x[a], test_x[b] — тест «не ловит изменение», если ни один вариант не упал
    old_ok = [t for t in new_ids if any(p == t or p.startswith(t + "[") for p in passed)
              and not any(f == t or f.startswith(t + "[") for f in failed)]
    # тест «прежнее поведение сохранилось» и должен проходить на старом коде; плохо, когда так ведут себя ВСЕ новые
    return old_ok if len(old_ok) == len(new_ids) else []


def removed_tests(root: Path, originals: dict) -> list[str]:
    """Тестовые функции, которые были в тестовых файлах до правок сессии и исчезли (write целиком и т.п.)."""
    out = []
    for rel, orig in originals.items():
        if orig is None or not _is_test_file(rel):
            continue
        try:
            before = {n.name for n in ast.walk(ast.parse(orig))
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}
            now = (root / rel).read_text(encoding="utf-8", errors="replace") if (root / rel).is_file() else ""
            after = {n.name for n in ast.walk(ast.parse(now))
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}
        except (SyntaxError, ValueError, OSError):
            continue  # синтаксис проверяется отдельно
        out += [f"{rel}: {name}" for name in sorted(before - after)]
    return out


def untested_changes(root: Path, originals: dict) -> list[str]:
    """Изменённые рабочие модули с тестами, когда ни один тестовый файл в сессии не менялся."""
    if any(_is_test_file(rel) for rel in originals):
        return []
    out = []
    for rel, orig in originals.items():
        if not rel.endswith(".py") or orig is None or not (root / rel).is_file():
            continue
        try:
            if _norm((root / rel).read_text(encoding="utf-8", errors="replace")) == _norm(orig):
                continue
        except OSError:
            continue
        tests = covering_tests(root, rel)
        if len(tests) > 2:
            # общий модуль (настройки, утилиты) импортируют многие тесты — это не его тесты;
            # иначе модель дописывает бессмысленные проверки в чужие тестовые файлы
            stem = rel.rsplit("/", 1)[-1][:-3]
            tests = [t for t in tests if stem in t.rsplit("/", 1)[-1]]
        if tests:
            out.append(f"{rel} (тесты: {', '.join(tests[:3])})")
    return out


_FAIL_HEAD = re.compile(r"^_{3,} (.+?) _{3,}$")
_TB_PLACE = re.compile(r"^[\w./\\:-]+\.py:\d+:")


def condense_pytest(out: str, max_fail: int = 6) -> str:
    """
    Вывод pytest -> коротко: по каждому падению — имя теста, место (файл:строка) и строки `E`,
    затем итоговая строка. Полные трейсбеки маленькой модели только мешают.
    """
    lines = out.splitlines()
    summary = next((l.strip("= ").strip() for l in reversed(lines)
                    if re.search(r"\b(passed|failed|errors?|no tests ran)\b", l)), "")
    fails, cur = [], None
    for l in lines:
        st = l.strip()
        m = _FAIL_HEAD.match(st)
        if m:
            cur = {"name": m.group(1), "where": "", "e": []}
            fails.append(cur)
            continue
        if l.startswith("="):
            cur = None  # секция кончилась (short test summary, warnings…)
            continue
        if cur is None:
            continue
        if _TB_PLACE.match(st):
            cur["where"] = st[:200]  # последнее место в трейсбеке — ближе всего к причине
        elif l.startswith("E ") and len(cur["e"]) < 10:
            cur["e"].append(l[:220])
    if not fails:
        errs = [l for l in lines if l.startswith(("E ", "ERROR", "ImportError", "ModuleNotFoundError"))][:15]
        return "\n".join(errs + [summary or "\n".join(lines[-15:])])
    parts = [f"✗ {f['name']}" + (f"\n  at {f['where']}" if f["where"] else "")
             + ("\n" + "\n".join(f["e"]) if f["e"] else "") for f in fails[:max_fail]]
    if len(fails) > max_fail:
        parts.append(f"…ещё {len(fails) - max_fail} падений")
    return "\n".join(parts + [summary])


def t_test(ctx: ToolContext, path: str = "", keyword: str = "", timeout: int | None = None) -> ToolResult:
    """
    pytest проекта (через его venv) с кратким отчётом. path — тестовый файл/папка или исходный модуль:
    для модуля запускаются тесты, которые его импортируют. keyword — фильтр -k.
    """
    root = ctx.root
    args: list[str] = []
    note = ""
    if path:
        target = resolve(root, path)
        rel = relpath(root, target)
        if target.is_file() and rel.endswith(".py") and not _is_test_file(rel):
            found = covering_tests(root, rel)
            if not found:
                return ToolResult(f"Ни один тест не импортирует {rel}. Напиши тест (tests/test_<модуль>.py) "
                                  "или запусти test без path — все тесты.", ok=False)
            args += found
            note = f"Тесты, импортирующие {rel}: {', '.join(found)}\n"
        else:
            args.append(rel)
    if keyword:
        args += ["-k", keyword]
    timeout = max(10, min(int(timeout or config.COMMAND_TIMEOUT), 1800))
    cmd = [_pytest_python(root), "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *args]
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout,
                           env=pytest_env({**os.environ, "PYTHONIOENCODING": "utf-8", "CI": "1"}))
    except subprocess.TimeoutExpired:
        return ToolResult(f"Тесты не уложились в {timeout} с.", ok=False)
    except OSError as e:
        return ToolResult(f"Не удалось запустить pytest: {e}", ok=False)
    out = (p.stdout + p.stderr).strip()
    if p.returncode == 5:
        return ToolResult(note + "Тесты не найдены (" + (" ".join(args) or "весь проект") + ").", ok=False)
    if "No module named pytest" in out:
        return ToolResult("pytest не установлен в окружении проекта.", ok=False)
    return ToolResult(note + _trunc(condense_pytest(out), 4000), ok=p.returncode == 0)


_TS = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})")
_LEVEL = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN|INFO|DEBUG)\b")
_NUMS = re.compile(r"\d+(?:[.:]\d+)*")


STALE_DAYS = 2


def log_age_days(stamp: str) -> int | None:
    """Сколько полных дней прошло с отметки «YYYY-MM-DD HH:MM» (локальное время)."""
    import datetime as _dt
    try:
        t = _dt.datetime.strptime(stamp[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return (_dt.datetime.now() - t).days


def _log_lines(p: Path, limit: int = 50 * 1024 * 1024) -> list[str]:
    """Строки лога (и .gz); не больше limit байт с конца файла."""
    import gzip
    if p.suffix == ".gz":
        with gzip.open(p, "rb") as f:
            data = f.read(limit * 4)
    else:
        with p.open("rb") as f:
            size = p.stat().st_size
            f.seek(max(0, size - limit))
            data = f.read()
    return data.decode("utf-8", "replace").splitlines()


def t_logstats(ctx: ToolContext, path: str, match: str = "", by: str = "", bucket: str = "",
               show: int = 0) -> ToolResult:
    """
    Сводка по логу вместо чтения его целиком: строки JSON (nginx log_format json и т.п.) или текст.
    match — regex-фильтр строк; by — поле JSON (status, ip, uri…) или `level`/`message` для текста:
    топ значений; bucket — minute|hour|day: гистограмма по времени; show — показать N последних строк.
    """
    base, note = ctx.root, ""
    target = resolve(base, path)
    main = main_checkout(ctx.root)
    if not target.exists() and main and resolve(main, path).exists():
        # изолированный worktree: логи (.gitignore) есть только в основной копии проекта
        base, target = main, resolve(main, path)
        # Переходим к основной копии, если лог в изолированном worktree
        note = f"(логи основной копии проекта {main})\n"
    every = ([relpath(base, q) for q in sorted(target.rglob("*")) if q.is_file()]
             if target.is_dir() else [])  # логи обычно в .gitignore — list_files их не видит
    files = [f for f in every
             if re.search(r"\.(log|jsonl|json|txt|out|err)(\.\d+)?(\.gz)?$", f) or "log" in f.rsplit("/", 1)[-1]]
    if target.is_file():
        files = [relpath(base, target)]
    if not files:
        return ToolResult(f"В {path} нет файлов логов.", ok=False)
    try:
        rx = re.compile(match, re.IGNORECASE) if match else None
    except re.error:
        rx = re.compile(re.escape(match), re.IGNORECASE)
    rows: list[tuple[str, dict | None]] = []
    for f in files[:30]:
        try:
            lines = _log_lines(base / f)
        except OSError:
            continue
        for line in lines:
            if not line.strip() or (rx and not rx.search(line)):
                continue
            # Определяем, является ли строка JSON, иначе None
            d = None
            if line.lstrip().startswith("{"):
                try:
                    d = json.loads(line)
                except ValueError:
                    d = None
            rows.append((line, d if isinstance(d, dict) else None))
    out = [f"{len(rows)} строк" + (f" по «{match}»" if match else "") + f" в {len(files)} файлах"
           + (f" ({', '.join(files[:5])}{'…' if len(files) > 5 else ''})" if len(files) > 1 else "")]
    if not rows:
        return ToolResult(out[0] + ".")
    times = []
    # Извлекаем временную метку из JSON или строки
    for line, d in rows:
        m = _TS.search(str(d.get("time") or d.get("ts") or d.get("timestamp") or "") if d else line) or _TS.search(line)
        times.append(m)
    found = [m for m in times if m]
    if found:
        stamps = sorted(f"{m.group(1)} {m.group(2)}:{m.group(3)}" for m in found)
        out.append(f"время: {stamps[0]} … {stamps[-1]}")
        age = log_age_days(stamps[-1])
        if age is not None and age >= STALE_DAYS:
            out.append(f"ВНИМАНИЕ: лог устарел — последняя запись {age} дн. назад. Если сервис сейчас работает "
                       "в другом месте (сервер, ВМ — см. документацию о развёртывании), актуальные логи там.")
    keys = {}
    for _, d in rows:
        if d:
            for k in d:
                keys[k] = keys.get(k, 0) + 1
    if keys:
        out.append("поля JSON: " + ", ".join(sorted(keys, key=lambda k: -keys[k])[:20]))

    def top(values, n=12):
        """Подсчитывает частоту появления элементов в последовательности и возвращает строку с топ‑n наиболее
        частыми значениями.
        """
        c: dict[str, int] = {}
        for v in values:
            c[v] = c.get(v, 0) + 1
        items = sorted(c.items(), key=lambda kv: -kv[1])
        return f"{len(c)} разных: " + ", ".join(f"{k[:80]} ×{v}" for k, v in items[:n])

    if by:
        if by == "level":
            vals = [(m.group(1) if (m := _LEVEL.search(line)) else "-") for line, _ in rows]
        elif by == "message":  # текст без чисел: одинаковые сообщения с разными временем/адресами склеиваются
            vals = [_NUMS.sub("#", _TS.sub("", line))[:120].strip() for line, _ in rows]
        else:
            vals = [str(d.get(by, "-")) if d else "-" for _, d in rows]
            if all(v == "-" for v in vals):
                out.append(f"поля «{by}» нет ни в одной строке")
                vals = []
        if vals:
            out.append(f"по {by}: " + top(vals))
    elif keys:  # без by — самые полезные поля для разбора
        for k in [k for k in ("status", "ip", "method", "level") if k in keys]:
            out.append(f"по {k}: " + top([str(d.get(k, "-")) if d else "-" for _, d in rows], 8))
    else:
        out.append("по level: " + top([(m.group(1) if (m := _LEVEL.search(line)) else "-") for line, _ in rows], 6))
    # Группируем по времени для гистограммы bucket
    if bucket in ("minute", "hour", "day"):
        cut = {"minute": 16, "hour": 13, "day": 10}[bucket]
        c: dict[str, int] = {}
        for m in found:
            k = f"{m.group(1)} {m.group(2)}:{m.group(3)}"[:cut]
            c[k] = c.get(k, 0) + 1
        peak = sorted(c.items(), key=lambda kv: -kv[1])[:15]
        out.append(f"пики по {bucket}: " + ", ".join(f"{k} ×{v}" for k, v in sorted(peak)))
    if show:
        out.append("последние строки:\n" + "\n".join(line[:300] for line, _ in rows[-min(int(show), 30):]))
    return ToolResult(note + _trunc("\n".join(out), 5000))


def _find_block(text: str, old: str, indent: bool = True) -> tuple[int, int] | None:
    """
    Совпадение блока строк без учёта пробелов в конце строк (частая ошибка моделей); indent=False —
    и без учёта отступов (в тексте документа они ничего не значат).
    """
    norm = (lambda l: l.rstrip()) if indent else (lambda l: l.strip())
    t_lines = text.split("\n")
    o_lines = old.strip("\n").split("\n")
    o_norm = [norm(l) for l in o_lines]
    hits = []
    for i in range(len(t_lines) - len(o_lines) + 1):
        if [norm(l) for l in t_lines[i:i + len(o_lines)]] == o_norm:
            hits.append(i)
    if len(hits) != 1:
        return None
    start = sum(len(l) + 1 for l in t_lines[:hits[0]])
    end = start + len("\n".join(t_lines[hits[0]:hits[0] + len(o_lines)]))
    return start, end


def _closest(text: str, old: str) -> str:
    """Находит в тексте фрагмент, наиболее похожий на исходный, и возвращает его с номерами строк, если
    схожесть выше порога 0.5.
    """
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


def _strip_added_marks(old: str, new: str) -> str:
    """
    Модель пишет в `new` добавленные строки с «+» в начале (как в диффе), а `old` — без них:
    в файл попадает «+def test_…» и ломает синтаксис. Если ВСЕ новые строки начинаются с «+»
    (а в old таких строк нет), это разметка, а не код, — снимаем её.
    """
    old_lines = set(old.split("\n"))
    if any(l.startswith("+") for l in old_lines):
        return new
    added = [l for l in new.split("\n") if l.strip() and l not in old_lines]
    marked = sum(1 for l in added if l.startswith("+"))
    # «большинство с +»: модель часто пишет первую строку (комментарий) без метки, остальные — с ней
    if marked < 2 or marked * 3 < len(added) * 2:
        return new
    return "\n".join(l[1:] if l.startswith("+") and l not in old_lines else l for l in new.split("\n"))


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
    # Обрабатываем добавление новых строк, если файл не .diff/.patch/.md
    if not rel.endswith((".diff", ".patch", ".md")):
        new = _strip_added_marks(old, new)
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
    # Проверяем, что старый фрагмент встречается только один раз, иначе ошибка
    if len(positions) > 1 and not replace_all:
        lines = ", ".join(str(_line_at(text, x)) for x in positions[:10])
        raise ToolError(f"old встречается {len(positions)} раз (строки {lines}). Правка не применена. "
                        f"Включи в old соседние уникальные строки (например, строку def/class выше), "
                        f"чтобы фрагмент встречался ровно один раз.")
    if positions:
        start = positions[0]
        after = text.replace(old, new) if replace_all else text[:start] + new + text[start + len(old):]
    else:
        # Если точного совпадения нет, ищем блок по отступам
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
    """Редактирует файл, выбирая подходящий механизм (docx или обычный текст) и обрабатывая ошибки, связанные
    с чтением/записью.
    """
    if path.lower().endswith(".docx"):
        return _edit_docx(ctx, path, old, new, replace_all)
    try:
        p, rel, text, after, nl, start, new, summary = _plan_edit(ctx, path, old, new, replace_all)
    except ToolError:
        # после неудачной правки модель перечитывает место, чтобы скопировать точный текст, —
        # заглушка «не изменился с прошлого чтения» тут только зацикливает её
        try:
            _forget(ctx, relpath(ctx.root, resolve(ctx.root, path)))
        except (WorkspaceError, ValueError):
            pass
        raise
    return _finish_edit(ctx, p, rel, text, after, nl, start, new, summary)


def preview(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    """
    Сухой прогон перед запросом подтверждения: битая правка сразу уходит
    модели с ошибкой (пользователя не спрашиваем), рабочая — даёт настоящий дифф для карточки.
    """
    try:
        if str(args.get("path", "")).lower().endswith(".docx"):
            return ToolResult("", path=args.get("path", ""))  # Word: дифф по тексту даст сам инструмент
        if name == "edit":
            _, rel, text, after, *_ = _plan_edit(ctx, **args)
            return ToolResult("", diff=_diff(rel, text, after), path=rel)
        if name == "delete":
            p = resolve(ctx.root, args["path"])
            if not p.is_file():
                raise ToolError(f"{args['path']}: нет такого файла.")
            rel = relpath(ctx.root, p)
            return ToolResult("", diff=_diff(rel, _read_text(p)[0] if is_text_file(p) else "", ""), path=rel)
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
    """Завершает редактирование, сохраняет изменения, формирует diff и сообщение об успехе, включая подсказку
    по синтаксису.
    """
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


_MAIN_GUARD = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]\s*:""", re.M)


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
        if _MAIN_GUARD.search(sources.get(rel, "")):
            continue  # скрипт-точка входа: его запускают, а не импортируют
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
    """Собирает имена, импортированные в модуле, исключая wildcard‑импорты."""
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            out.update((a.asname or a.name).split(".")[0] for a in n.names if a.name != "*")
    return out


def _unbound_names(tree: ast.AST) -> dict[str, int]:
    """
    Имена, которые читаются, но нигде в модуле не связываются (импорт, def/class, присваивание,
    параметр, for/with/except, global) и не встроенные. Грубо по всему модулю, без областей
    видимости, — зато без ложных срабатываний на обычном коде. {имя: первая строка}.
    """
    import builtins
    bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__package__", "__builtins__"}
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            if any(a.name == "*" for a in n.names):
                return {}  # import * — не узнать, что определено
            bound.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, ast.arg):
            bound.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            bound.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            bound.add(n.rest)
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound:
            out.setdefault(n.id, n.lineno)
    return out


def undefined_names(root: Path, originals: dict) -> list[str]:
    """
    Имена, ставшие неопределёнными после правок: агент убрал импорт/определение, а использование
    осталось — NameError при выполнении (часто в точках входа, которые тесты не импортируют).
    """
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if not rel.endswith(".py") or not p.is_file():
            continue
        try:
            new = _unbound_names(ast.parse(p.read_text(encoding="utf-8", errors="replace")))
            old = _unbound_names(ast.parse(before)) if before else {}
        except SyntaxError:
            continue
        out += [f"{rel}:{line}: имя {name} не определено (импорт или определение удалены?) — NameError"
                for name, line in sorted(new.items(), key=lambda x: x[1]) if name not in old]
    return out[:20]


_SQL = re.compile(r"^\s*(SELECT|UPDATE|INSERT|DELETE|WITH)\b", re.IGNORECASE)
_SQL_WORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null", "as", "on", "join", "left", "right",
    "inner", "outer", "group", "by", "order", "asc", "desc", "limit", "offset", "distinct", "count", "sum",
    "avg", "min", "max", "having", "update", "set", "insert", "into", "values", "delete", "case", "when",
    "then", "else", "end", "like", "between", "exists", "union", "all", "with", "coalesce", "ifnull",
    "cast", "date", "datetime", "abs", "round", "true", "false", "lower", "upper", "length", "over",
    "partition", "row_number", "replace", "conflict", "do", "nothing", "excluded", "strftime", "julianday"}


def _schema(root: Path) -> dict[str, set[str]]:
    """Таблицы из моделей SQLAlchemy проекта: {__tablename__: {колонки}}."""
    out = {}
    for f in list_files(root):
        if not f.endswith(".py") or _TEST_PATH.search(f):
            continue
        try:
            text = (root / f).read_text(encoding="utf-8", errors="replace")
            if "__tablename__" not in text:
                continue
            tree = ast.parse(text)
        except (SyntaxError, OSError):
            continue
        for c in ast.walk(tree):
            if not isinstance(c, ast.ClassDef):
                continue
            name, cols = None, set()
            for st in c.body:
                if isinstance(st, ast.Assign) and any(getattr(t, "id", "") == "__tablename__" for t in st.targets) \
                        and isinstance(st.value, ast.Constant):
                    name = str(st.value.value)
                elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                    cols.add(st.target.id)
                elif isinstance(st, ast.Assign):
                    cols.update(t.id for t in st.targets if isinstance(t, ast.Name))
            if name:
                out[name] = cols
    return out


def sql_column_errors(root: Path, originals: dict) -> list[str]:
    """
    Новые SQL-строки, которые ссылаются на колонки, которых нет у таблиц из моделей проекта
    (SQLAlchemy: __tablename__ + атрибуты). Такие ошибки всплывают только при выполнении запроса,
    а тесты на пустой БД их обычно не доходят.
    """
    schema = None
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if not rel.endswith(".py") or _TEST_PATH.search(rel) or not p.is_file():
            continue
        try:
            new_tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            old_tree = ast.parse(before) if before else None
        except SyntaxError:
            continue
        old = {n.value for n in ast.walk(old_tree) if isinstance(n, ast.Constant)} if old_tree else set()
        for n in ast.walk(new_tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str) and _SQL.match(n.value)) \
                    or n.value in old:
                continue
            if schema is None:
                schema = _schema(root)
            if not schema:
                return []
            sql = re.sub(r"'[^']*'", "", n.value)  # строковые литералы
            # Извлекаем имена таблиц из ключевых слов FROM/JOIN/UPDATE/INTO
            tables = re.findall(r"\b(?:FROM|JOIN|UPDATE|INTO)\s+[\"`]?(\w+)", sql, re.IGNORECASE)
            known = [t for t in tables if t in schema]
            if not known:
                continue
            cols = set().union(*(schema[t] for t in known))
            aliases = set(re.findall(r"\bAS\s+[\"`]?(\w+)", sql, re.IGNORECASE)) | set(tables)
            # Находим алиасы после FROM/JOIN, игнорируя ключевые слова
            aliases |= set(re.findall(r"\b(?:FROM|JOIN)\s+\w+\s+(?!WHERE|JOIN|ON|ORDER|GROUP|LIMIT|LEFT|INNER)(\w+)",
                                      sql, re.IGNORECASE))
            for word in dict.fromkeys(re.findall(r"(?<![:@$.\w])[A-Za-z_]\w*", sql)):
                if word.lower() in _SQL_WORDS or word in aliases or word in cols or word.isdigit():
                    continue
                out.append(f"{rel}:{n.lineno}: в SQL колонки {word} нет у таблиц {', '.join(known)} "
                           f"(есть: {', '.join(sorted(cols))[:200]})")
    return out[:10]


def dropped_dependencies(root: Path, originals: dict) -> list[str]:
    """
    Импортированные имена, которые использовались до правки, а после — нет (импорт остался или
    удалён вместе с использованием). Часто это пропавшее поведение: настройка/флаг больше не
    проверяется (REVERSE_ENABLED), вызов функции выпал. Тесты такое обычно не замечают.
    """
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        if not before or not rel.endswith(".py") or rel.endswith("__init__.py") or not p.is_file():
            continue
        try:
            old, new = ast.parse(before), ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        # использования внутри функций, которые из файла удалены (перенесены в общий модуль), не в счёт
        old_defs, new_defs = _defs(old), _defs(new)
        moved = {id(fn) for name, fn in old_defs.items() if name not in new_defs}

        def used(tree, skip=()):
            """Определяет набор идентификаторов, загруженных в дереве AST, пропуская узлы, указанные в skip.
            """
            out, stack = set(), [tree]
            while stack:
                n = stack.pop()
                if id(n) in skip:
                    continue
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    out.add(n.id)
                stack.extend(ast.iter_child_nodes(n))
            return out

        old_used, new_used = used(old, moved), used(new)
        for name in sorted(_imported_names(old)):
            if name in old_used and name not in new_used:
                out.append(f"{rel}: {name} использовался до правки, а теперь нигде не используется")
    return out[:15]


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
    """Находит имена функций‑заглушек в дереве AST, включая вложенные классы, и возвращает их с префиксами."""
    out = set()

    def walk(body, prefix=""):
        """Вспомогательная функция для _stub_names, рекурсивно обходит тело, собирая имена заглушек."""
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
    """Создаёт словарь, сопоставляющий имена функций и методов классов с их AST‑узлами."""
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
    return [m["text"] for m in find_merges(root, originals)]


def find_merges(root: Path, originals: dict) -> list[dict]:
    """
    Функции, которые агент заменил общей реализацией (удалил и импортировал одноимённую из модуля
    проекта или свёл тело к вызову другой функции), причём тела отличаются. Для каждой — «было /
    стало» (text) и данные для дифференциальной проверки (equivalence.check_merges): маленькая
    модель объединяет «похожие» функции, не замечая различий в формате, порядке, типах.
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
        own_mod = rel[:-3].replace("/", ".").removesuffix(".__init__")
        for name, old_fn in old_d.items():
            new_fn = new_d.get(name)
            if new_fn is not None and ast.dump(old_fn) == ast.dump(new_fn):
                continue
            target = name if new_fn is None else _delegate(new_fn)
            if not target or (new_fn is None and name not in imports):
                continue
            if target not in imports:  # делегирование функции из этого же файла
                tgt = new_d.get(target)
                where, tgt_text, tgt_mod, tgt_name = rel, text, own_mod, target
            else:
                tgt_mod, tgt_name = imports[target]
                f = _module_file(root, tgt_mod)
                if f is None or f.is_dir():
                    continue
                tgt_text = f.read_text(encoding="utf-8", errors="replace")
                tgt = _defs(ast.parse(tgt_text)).get(tgt_name)
                where = relpath(root, f)
            if tgt is None or _body_key_eq(old_fn, tgt):
                continue
            static = any(getattr(d, "id", "") == "staticmethod" for d in old_fn.decorator_list)
            out.append({
                "text": f"{rel}: {name} заменена на {target} из {where}.\nБыло:\n{_src(before, old_fn)}\n"
                        f"Стало ({target}):\n{_src(tgt_text, tgt)}"
                        + (f"\nВызов в {rel}:\n{_src(text, new_fn)}" if new_fn is not None else ""),
                "name": name, "old_src": ast.get_source_segment(before, old_fn) or "",
                # импорты старого файла: старой версии нужны её имена (ZoneInfo, константы настроек)
                "old_imports": [ast.unparse(n) for n in old_t.body
                                if isinstance(n, (ast.Import, ast.ImportFrom)) and not getattr(n, "level", 0)],
                # старую версию исполняем в пространстве имён модуля, где функция теперь «живёт»
                "ns_module": tgt_mod if new_fn is None else own_mod,
                # новая версия: обёртка в своём модуле или сама общая функция
                "new_module": own_mod if new_fn is not None else tgt_mod,
                "new_name": name if new_fn is not None else tgt_name,
                # вызовы перехватываем там, где их делает код (у точек входа — нет: побочные эффекты)
                "patch_module": None if _NO_IMPORT.search(rel) else own_mod,
                "plain": "." not in name or static,
            })
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
                           env=pytest_env({**os.environ, "PYTHONIOENCODING": "utf-8", "CI": "1"}))
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
            "        print(f'@@LCW {m}: {type(e).__name__}: {e} ({tb.filename.rsplit(chr(92), 1)[-1]}:{tb.lineno})')\n"
            "        continue\n"
            "    for name, obj in vars(mod).items():\n"
            "        if not inspect.isclass(obj) or obj.__module__ != m or not inspect.isabstract(obj):\n"
            "            continue\n"
            "        own = {k for k, v in vars(obj).items() if getattr(v, '__isabstractmethod__', False)}\n"
            "        missing = sorted(set(obj.__abstractmethods__) - own)\n"
            "        if missing:\n"
            "            print(f'@@LCW {m}: класс {name} абстрактный — не реализованы {\", \".join(missing)}; '\n"
            "                  f'{name}() упадёт с TypeError')\n"
            # реестр, где разные ключи указывают на один объект, — обычно ошибка импорта (алиас не того имени)
            "    for name, obj in vars(mod).items():\n"
            "        if isinstance(obj, dict) and len(obj) >= 2 and all(callable(v) for v in obj.values()):\n"
            "            seen = {}\n"
            "            for k, v in obj.items():\n"
            "                if id(v) in seen:\n"
            "                    print(f'@@LCW {m}: в {name} ключи {seen[id(v)]!r} и {k!r} указывают на один объект '\n"
            "                          f'{getattr(v, \"__module__\", \"?\")}.{getattr(v, \"__qualname__\", v)} — '\n"
            "                          f'проверь, что импортирован нужный класс')\n"
            "                seen.setdefault(id(v), k)\n")
    try:
        p = subprocess.run([py, "-c", code, *mods], cwd=str(root), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(root)})
    except (OSError, subprocess.TimeoutExpired):
        return static
    # только строки проверки: библиотеки печатают при импорте свои баннеры (pygame: «pygame-ce 2.5 (SDL…)»)
    return static + [l[6:] for l in p.stdout.splitlines() if l.startswith("@@LCW ")][:20]


def _module_file(root: Path, mod: str) -> Path | None:
    """Определяет путь к файлу модуля, учитывая как .py, так и __init__.py, либо возвращает None, если файл не
    найден.
    """
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
    """Записывает содержимое в файл, создавая его при необходимости, и возвращает diff и сообщение об успехе,
    учитывая форматирование строк.
    """
    p = resolve(ctx.root, path)
    if p.is_dir():
        raise ToolError(f"{path} — это папка.")
    rel = relpath(ctx.root, p)
    if p.suffix.lower() == ".docx":
        return _write_docx(ctx, p, rel, content)
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


def _edit_docx(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
    """
    Правка .docx по его Markdown-источнику; пустой old — дописать раздел в конец. Так документ
    собирается по частям: один огромный write маленькая модель обрывает на лимите ответа (битый JSON).
    """
    p = resolve(ctx.root, path)
    rel = relpath(ctx.root, p)
    old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
    old, new = _LINE_NO.sub("", old), _LINE_NO.sub("", new)
    src = ""
    if p.is_file() and p.stat().st_size > 0:
        src = documents.docx_source(p) or documents.document_text(p)
    if not old:  # раздел с уже существующим заголовком заменяет прежний — без дублей
        after = documents.upsert_section(src, new)
    else:
        count = src.count(old)
        if count > 1 and not replace_all:
            raise ToolError(f"old встречается {count} раз — добавь соседние строки, чтобы он был уникален. "
                            "Целый раздел проще заменить: edit с пустым old и new, начинающимся с того же "
                            "заголовка (## …), — он заменит прежний раздел.")
        if count:
            after = src.replace(old, new) if replace_all else src.replace(old, new, 1)
        else:
            span = _find_block(src, old, indent=False)  # без учёта пробелов в начале/конце строк
            if not span:
                raise ToolError("old не найден в документе. Перечитай его через read (там Markdown, по которому "
                                "работает edit) или замени раздел целиком: edit с пустым old и new с тем же "
                                "заголовком (## …)." + _closest(src, old))
            after = src[:span[0]] + new + src[span[1]:]
    return _write_docx(ctx, p, rel, documents.dedupe_sections(after), shown=new)


def _write_docx(ctx: ToolContext, p: Path, rel: str, content: str, shown: str | None = None) -> ToolResult:
    """Markdown → Word; в ответе — текст получившегося файла, чтобы модель видела результат."""
    existed = p.is_file() and p.stat().st_size > 0
    before = documents.document_text(p) if existed else ""
    if (shown is None and len(before) > 1500 and len(content) < len(before) * 0.5
            and ctx.seen.get(("shrink", rel)) != content):
        # «оформить итог» маленькая модель понимает как пересказ: подробные решения теряются
        ctx.seen[("shrink", rel)] = content
        raise ToolError(f"write сократил бы {rel} с {len(before)} до {len(content)} символов — содержимое будет "
                        "потеряно. Меняй документ по частям: edit с old из read (там Markdown) и new. Если "
                        "сокращение действительно нужно, повтори этот же write.")
    if ctx.snapshot:
        ctx.snapshot(rel, documents.BINARY_MARK + base64.b64encode(p.read_bytes()).decode() if existed else None)
    try:
        n = documents.write_docx(p, content)
    except PermissionError:
        raise ToolError(f"{rel} открыт в другой программе (Word?) — закрой его и повтори write.")
    after = documents.document_text(p)
    _forget(ctx, rel)
    if shown is not None:  # edit: только изменённый кусок — в том виде, как он попал в Word
        return ToolResult(f"OK: {rel} обновлён (Word, {n} абзацев, {len(after)} символов). Новый текст "
                          f"в документе:\n{_trunc(documents.latex_to_unicode(shown), 3000)}",
                          diff=_diff(rel, before, after), path=rel)
    preview = after if len(after) <= 1500 else after[:1500] + f"\n…(ещё {len(after) - 1500} символов)"
    return ToolResult(f"OK: {rel} {'перезаписан' if existed else 'создан'} (Word, {n} абзацев). "
                      f"Текст документа:\n{preview}", diff=_diff(rel, before, after), path=rel)


def _forget(ctx: ToolContext, rel: str) -> None:
    for k in [k for k in ctx.seen if k[0] == rel]:
        del ctx.seen[k]


async def t_run(ctx: ToolContext, command: str, timeout: int | None = None) -> ToolResult:
    """Запускает внешнюю команду в окружении проекта, обрабатывая таймауты, виртуальное окружение и вывод,
    возвращая результат в виде ToolResult.
    """
    timeout = max(1, min(int(timeout or config.COMMAND_TIMEOUT), 1800))
    note = ""
    if PIP_INSTALL.search(command) and not find_venv(ctx.root):
        note = ensure_venv(ctx.root)
    env = project_env(ctx.root)
    if "pytest" in command:
        env = pytest_env(env)
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

    # Функция wait оборачивает Popen.communicate
    def wait() -> tuple[bytes, bool]:
        """Ожидает завершения процесса с таймаутом, завершая его при истечении времени и возвращая вывод и
        флаг таймаута.
        """
        try:
            return proc.communicate(timeout=timeout)[0], False
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            return proc.communicate()[0], True

    try:
        out, timed_out = await asyncio.to_thread(wait)
    except asyncio.CancelledError:
        # При отмене убиваем все дочерние процессы
        _kill_tree(proc.pid)  # кнопка «Стоп»: убиваем дерево процессов
        raise
    code = proc.returncode
    text = out.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    for _ in range(1):  # Windows-консоль иногда отдаёт cp866
        if "\ufffd" in text and sys.platform == "win32":
            text = out.decode("cp866", errors="replace").replace("\r\n", "\n").strip()
    head = f"exit {code}" if not timed_out else f"ТАЙМАУТ {timeout}s, процесс остановлен"
    hint = ""
    if PIP_INSTALL.search(command) and code and WHEEL_BUILD.search(text):
        hint = (f"\n[Подсказка: у пакета нет готовой сборки (wheel) для Python {project_python_version(ctx.root)}, "
                "pip пытается собрать его из исходников и падает. Не повторяй установку: возьми другую версию "
                "или аналог с готовыми сборками (проверка: pip install --only-binary :all: <пакет>; часто у "
                "популярной библиотеки есть поддерживаемый форк, например с суффиксом -ce).]")
    if timed_out and not text and re.search(r"\b(python|py)\b", command):
        hint += ("\n[Подсказка: программа не завершилась сама — если это GUI-приложение или игра, проверяй её "
                 "инструментом launch: он покажет, открылось ли окно, ошибки и скриншот.]")
    body = f"[{head}]\n{_trunc(text, 8000)}" if text else f"[{head}] (нет вывода)"
    return ToolResult(note + body + hint, ok=(code == 0))


PIP_INSTALL = re.compile(r"\b(pip3?|python[\d.]*\s+-m\s+pip|uv\s+pip)\s+install\b")
WHEEL_BUILD = re.compile(r"Failed (to )?build|subprocess-exited-with-error|Getting requirements to build wheel "
                         r"did not run successfully|error: metadata-generation-failed", re.I)


def _base_python() -> str:
    """Системный Python (тот, на котором создан venv самого LCW), а не python из venv LCW."""
    return getattr(sys, "_base_executable", "") or sys.executable


def project_python_version(root: Path) -> str:
    """Версия Python проекта (из pyvenv.cfg его venv) — для подсказок об отсутствующих сборках пакетов."""
    venv = find_venv(root)
    cfg = venv / "pyvenv.cfg" if venv else None
    if cfg and cfg.is_file():
        m = re.search(r"^version\w*\s*=\s*(\d+\.\d+)", cfg.read_text(encoding="utf-8", errors="replace"), re.M)
        if m:
            return m.group(1)
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def ensure_venv(root: Path) -> str:
    """
    У проекта нет venv, а модель ставит пакеты: создаём .venv в проекте. Иначе pip из PATH
    сервера ставит зависимости проекта в venv самого LCW (а запуск проекта их потом не видит).
    """
    target = root / ".venv"
    r = subprocess.run([_base_python(), "-m", "venv", str(target)], capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        return f"[не удалось создать .venv проекта: {(r.stderr or r.stdout)[-300:]}]\n"
    return "[создано виртуальное окружение проекта .venv — пакеты ставятся в него, python/pip в run — из него]\n"


def project_env(root: Path) -> dict:
    """Окружение команд проекта: utf-8, без пейджеров, venv проекта первым в PATH, без venv самого LCW."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "GIT_PAGER": "cat",
           "PAGER": "cat", "GIT_TERMINAL_PROMPT": "0", "CI": "1"}
    own = str(config.ROOT.resolve()).lower()
    if not str(Path(root).resolve()).lower().startswith(own):
        # сервер запущен из своего venv: его Scripts в PATH подменяют python/pip проекта
        env["PATH"] = os.pathsep.join(x for x in env.get("PATH", "").split(os.pathsep)
                                      if not x.lower().startswith(own))
        if str(env.get("VIRTUAL_ENV", "")).lower().startswith(own):
            env.pop("VIRTUAL_ENV", None)
    venv = find_venv(root)
    if venv:  # python/pytest проекта, а не системные
        env["VIRTUAL_ENV"] = str(venv)
        env["PATH"] = str(venv_bin(venv)) + os.pathsep + env.get("PATH", "")
    return env


def t_delete(ctx: ToolContext, path: str) -> ToolResult:
    """Удаляет файл проекта (снимок для отката сохраняется)."""
    p = resolve(ctx.root, path)
    if p.is_dir():
        raise ToolError(f"{path} — папка; удаляются только файлы.")
    if not p.is_file():
        raise ToolError(f"{path}: нет такого файла.")
    rel = relpath(ctx.root, p)
    before = ""
    if is_text_file(p):
        before, nl = _read_text(p)
        if ctx.snapshot:
            ctx.snapshot(rel, before if nl == "\n" else before.replace("\n", nl))
    p.unlink()
    _forget(ctx, rel)
    return ToolResult(f"OK: {rel} удалён.", diff=_diff(rel, before, ""), path=rel)


def _site_packages(root: Path) -> list[Path]:
    """Папки site-packages окружения проекта (или интерпретатора, на котором запускается проект)."""
    venv = find_venv(root)
    if venv:
        found = [venv / "Lib" / "site-packages"] + sorted(venv.glob("lib/python*/site-packages"))
        return [d for d in found if d.is_dir()]
    return [Path(p) for p in sys.path if p.endswith("site-packages") and Path(p).is_dir()]


def _demo_block(text: str) -> str:
    """Пример использования из модуля библиотеки: блок `if __name__ == '__main__':`."""
    m = re.search(r"^if\s+__name__\s*==\s*['\"]__main__['\"]\s*:\s*$", text, re.M)
    return text[m.end():].strip("\n") if m else ""


def t_libdoc(ctx: ToolContext, package: str, query: str = "") -> ToolResult:
    """Справка по установленной библиотеке из её собственных файлов: примеры (`__main__`-блоки),
    определения и докстринги по словам запроса, встроенные ресурсы (текстуры, модели, шрифты)."""
    name = package.strip().split(".")[0]
    pkg = next((sp / name for sp in _site_packages(ctx.root) if (sp / name).is_dir()), None)
    if pkg is None:
        mod = next((sp / f"{name}.py" for sp in _site_packages(ctx.root) if (sp / f"{name}.py").is_file()), None)
        if mod is None:
            raise ToolError(f"Пакет {name} не найден в окружении проекта — сначала run pip install {name}.")
        files = [mod]
        pkg = mod.parent
    else:
        files = [f for f in sorted(pkg.rglob("*.py")) if "__pycache__" not in f.parts and "test" not in f.name]
    words = [w.lower() for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)]
    if "shader" not in query.lower():  # GLSL-тексты шейдеров забивают выдачу словами texture/position
        files = [f for f in files if not any("shader" in p.lower() for p in f.relative_to(pkg).parts[:-1])]
    out = []
    if pkg.is_dir() and (pkg / "__init__.py").exists():
        assets = {}
        for f in pkg.rglob("*"):
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".obj", ".gltf", ".glb", ".bam",
                                                     ".ursinamesh", ".ttf", ".otf", ".wav", ".ogg", ".mp3"):
                assets.setdefault(f.parent.relative_to(pkg).as_posix(), []).append(f.stem)
        if assets:
            out.append("Встроенные ресурсы пакета (имя без расширения):\n" + "\n".join(
                f"  {d}/: {', '.join(sorted(set(v))[:40])}" for d, v in sorted(assets.items())[:12]))
    scored = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        demo = _demo_block(low)
        # плотность совпадений (большие файлы не побеждают одним объёмом) + слова, покрытые демо-примером
        hits = {w: len(re.findall(rf"\b{w}\b", low)) for w in words}  # целые слова: text ≠ texture
        score = (sum(min(n, 20) for n in hits.values()) / (1 + low.count("\n")) ** 0.5
                 + sum(1.0 for w in words if re.search(rf"\b{w}\b", demo))
                 + sum(2.0 for w in words if w == f.stem.lower()))
        if score or not words:
            scored.append((score, f, text))
    scored.sort(key=lambda x: -x[0])
    budget = 9000
    for score, f, text in scored[:4]:
        rel = f.relative_to(pkg.parent).as_posix()
        parts = []
        lines = text.splitlines()
        for i, line in enumerate(lines):  # определения, где встречаются слова запроса, с докстрингом
            s = line.strip()
            if s.startswith(("def ", "class ")) and any(w in s.lower() for w in words):
                parts.append("\n".join(lines[i:i + 8]))
            if len(parts) >= 4:
                break
        demo = _demo_block(text)
        if demo:
            parts.append("Пример из библиотеки (__main__):\n" + "\n".join(demo.splitlines()[:45]))
        if not parts:
            hits = [i for i, line in enumerate(lines) if any(w in line.lower() for w in words)][:3]
            parts += ["\n".join(lines[max(0, i - 3):i + 6]) for i in hits]
        chunk = f"=== {rel}\n" + "\n...\n".join(parts)
        out.append(chunk[:budget // 3])
    if not scored:
        out.append(f"По словам «{query}» в пакете ничего не найдено. Файлы: "
                   + ", ".join(f.stem for f in files[:60]))
    return ToolResult("\n\n".join(out)[:budget] or f"{name}: пусто.")


GUI_LIBS = re.compile(r"^\s*(?:from|import)\s+(pygame|pyglet|arcade|tkinter|PyQt\d|PySide\d|kivy|ursina|panda3d|"
                      r"moderngl_window|glfw|wx|dearpygui|customtkinter|raylib|pyray)\b", re.M)
ENTRY_NAMES = ("main.py", "app.py", "game.py", "run.py", "__main__.py")


def _code_files(root: Path) -> list[str]:
    return [f for f in list_files(root) if f.endswith(".py") and not _TEST_PATH.search(f)]


def small_project(root: Path, limit: int = 1500) -> bool:
    """Новый/маленький проект (меньше limit строк кода): опоры на «существующие сущности» ещё нет."""
    total = 0
    for f in list_files(root):
        if Path(f).suffix.lower() in (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".cs", ".cpp", ".c"):
            try:
                total += (root / f).read_text(encoding="utf-8", errors="replace").count("\n")
            except OSError:
                pass
            if total >= limit:
                return False
    return True


def gui_entry(root: Path) -> str | None:
    """
    Команда запуска GUI-приложения проекта (`python main.py`), если в коде есть GUI/игровая библиотека
    и найдена точка входа: файл с `if __name__ == "__main__"` — сначала в корне с типовым именем.
    """
    files = _code_files(root)
    texts = {}
    for f in files:
        try:
            texts[f] = (root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    if not any(GUI_LIBS.search(x) for x in texts.values()):
        return None
    mains = [f for f, x in texts.items() if _MAIN_GUARD.search(x)]
    if not mains:
        return None
    mains.sort(key=lambda f: (f.count("/"), ENTRY_NAMES.index(Path(f).name) if Path(f).name in ENTRY_NAMES else 9, f))
    best = mains[0]
    if best.endswith("__main__.py"):
        return f"python -m {Path(best).parent.as_posix().replace('/', '.')}"
    return f"python {best}"


SHOTS_DIR = config.DATA_DIR / "shots"
# sitecustomize для запускаемой программы: перед остановкой сохранить стек всех потоков —
# по нему видно, где программа висит без окна (инициализация звука, input(), бесконечный цикл)
HANG_HOOK = """import os
_f = os.environ.get("LCW_HANG_FILE")
if _f:
    import faulthandler
    _h = open(_f, "w", encoding="utf-8")
    faulthandler.dump_traceback_later(float(os.environ.get("LCW_HANG_DUMP", "5")), exit=False, file=_h)
"""


def _hang_stack(text: str, limit: int = 12) -> str:
    """Стек главного потока из дампа faulthandler, без строк runpy/importlib."""
    blocks = re.split(r"\n(?=(?:Current thread|Thread) 0x)", text)
    main = next((b for b in blocks if "most recent call first" in b and "<module>" in b), blocks[0] if blocks else "")
    rows = [l.strip() for l in main.splitlines() if l.strip().startswith("File ")
            and "<frozen" not in l and "runpy" not in l]
    out = []
    for row in reversed(rows[:limit]):
        m = re.match(r'File "(.+)", line (\d+)', row)
        code = ""
        if m:
            try:
                code = Path(m.group(1)).read_text(encoding="utf-8", errors="replace").splitlines()[int(m.group(2)) - 1]
            except (OSError, IndexError):
                pass
        out.append(row + (f":  {code.strip()}" if code.strip() else ""))
    return "\n".join(out)


TRACEBACK = re.compile(r"Traceback \(most recent call last\)|^\w*(Error|Exception): ", re.M)


def t_launch(ctx: ToolContext, command: str, seconds: int = 6, steps: list | None = None,
             look: str = "") -> ToolResult:
    """
    Запуск GUI-приложения: ждём окно, выполняем шаги (клавиши, клики, паузы, скриншоты), снимаем
    итоговый скриншот и останавливаем процесс. Вывод и traceback — из stdout/stderr.
    """
    import threading
    import time
    import uuid as _uuid
    from . import gui
    seconds = max(2, min(int(seconds or 6), 120))
    steps = [str(x) for x in (steps or [])][:60]
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = _uuid.uuid4().hex[:8]
    hook = SHOTS_DIR / "hook"
    hook.mkdir(exist_ok=True)
    (hook / "sitecustomize.py").write_text(HANG_HOOK, encoding="utf-8")
    hang_file = SHOTS_DIR / f"{tag}_stack.txt"
    env = {**project_env(ctx.root), "PYTHONUNBUFFERED": "1", "LCW_HANG_FILE": str(hang_file),
           "LCW_HANG_DUMP": str(max(1.5, seconds - 1.0))}
    env["PYTHONPATH"] = str(hook) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else \
        {"start_new_session": True}
    proc = subprocess.Popen([*SHELL_CMD, command], cwd=str(ctx.root), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    chunks: list[bytes] = []

    def pump() -> None:
        for block in iter(lambda: proc.stdout.read1(4096), b""):
            chunks.append(block)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    shots, notes = [], []
    start = time.monotonic()
    hwnd, opened_at = None, None

    def alive() -> bool:
        return proc.poll() is None

    def window():
        try:
            wins = gui.find_windows(gui.child_pids(proc.pid))
        except Exception:
            return None
        return wins[0] if wins else None

    def shot(label: str) -> None:
        nonlocal hwnd
        hwnd = window() or hwnd
        if not hwnd or len(shots) >= 6:
            return
        path = SHOTS_DIR / f"{tag}_{len(shots) + 1}.png"
        try:
            w, h = gui.screenshot(hwnd, str(path))
            shots.append({"path": str(path), "label": label, "size": f"{w}x{h}", "blank": gui.is_blank(str(path))})
        except Exception as e:
            notes.append(f"скриншот не снят ({label}): {e}")

    try:
        while alive() and time.monotonic() - start < seconds:
            hwnd = window()
            if hwnd:
                opened_at = time.monotonic() - start
                break
            time.sleep(0.25)
        if hwnd:
            time.sleep(1.0)  # первый кадр
            shot("после запуска")
            for n, step in enumerate(steps, 1):
                if not alive():
                    notes.append(f"процесс завершился до шага {n} «{step}»")
                    break
                low = step.strip().lower()
                try:
                    if low.startswith("wait:"):
                        time.sleep(min(float(low[5:]), 20))
                    elif low.startswith("click:"):
                        x, y = (int(float(v)) for v in low[6:].split(","))
                        gui.click(hwnd, x, y)
                        time.sleep(0.4)
                    elif low in ("shot", "screenshot"):
                        shot(f"после шага {n - 1}")
                    else:
                        gui.send_key(hwnd, low)
                        time.sleep(0.35)
                except ValueError as e:
                    notes.append(f"шаг {n} «{step}» пропущен: {e}")
            rest = seconds - (time.monotonic() - start)
            while alive() and rest > 0:
                time.sleep(min(0.25, rest))
                rest = seconds - (time.monotonic() - start)
            if alive():
                shot("в конце")
    finally:
        ran = time.monotonic() - start
        still = alive()
        if still:
            _kill_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=3)
    text = b"".join(chunks).decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    crashed = bool(TRACEBACK.search(text))
    lines = [f"окно открылось через {opened_at:.1f} с" if hwnd and opened_at is not None else "ОКНО НЕ ОТКРЫЛОСЬ"]
    if still:
        lines.append(f"приложение работало {ran:.0f} с, остановлено"
                     + (" (норма для игры/GUI)" if hwnd else " — всё это время БЕЗ ОКНА"))
    else:
        lines.append(f"процесс ЗАВЕРШИЛСЯ сам через {ran:.1f} с, код {proc.returncode}"
                     + (" — окно не создаётся или главный цикл не запущен" if not hwnd else ""))
    for sh in shots:
        lines.append(f"скриншот {sh['label']}: {sh['size']}"
                     + (" — ОДНОТОННЫЙ КАДР: ничего не нарисовано" if sh["blank"] else ""))
    lines += notes
    if crashed:
        lines.append("В ВЫВОДЕ ОШИБКА (traceback) — исправь её")
    stack = ""
    if hang_file.is_file():
        if not hwnd and still:  # окна нет, а процесс жив — где он висит
            stack = _hang_stack(hang_file.read_text(encoding="utf-8", errors="replace"))
            if stack:
                lines.append("программа ЗАВИСЛА до создания окна; стек Python в момент остановки "
                             "(последняя строка — где висит):\n" + stack)
                if re.search(r"\binit\w*\(", stack.splitlines()[-1]):
                    lines.append("[Подсказка: висит общая инициализация библиотеки — обычно одно её устройство "
                                 "(звук, джойстик, сеть) недоступно на этой машине. Инициализируй только нужные "
                                 "подсистемы по отдельности (графика, шрифты), а необязательные — в try/except "
                                 "или не трогай вовсе; повторный запуск того же кода не поможет.]")
        try:
            hang_file.unlink()
        except OSError:
            pass
    out = "\n".join(lines) + (f"\nвывод:\n{_trunc(text, 4000)}" if text else "\nвывода нет")
    ok = bool(hwnd) and not crashed and (still or proc.returncode == 0)
    return ToolResult(out, ok=ok, shots=shots)


def _kill_tree(pid: int) -> None:
    """Уничтожает процесс и все его дочерние процессы, используя taskkill на Windows или killpg на Unix,
    игнорируя ошибки.
    """
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
    """Выполняет команду git в указанной директории, возвращает CompletedProcess."""
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
    # Ищем файлы, содержащие потенциальные секреты, чтобы не коммитить их.
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
    """Создаёт коммит из данных preview, добавляет файлы, коммитит, возможно пушит, возвращает результат
    операции.
    """
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


SYNC = {"git_commit": t_git_commit, "symbols": t_symbols, "refs": t_refs, "test": t_test,
        "logstats": t_logstats, "read": t_read, "glob": t_glob, "grep": t_grep, "edit": t_edit, "write": t_write,
        "delete": t_delete, "libdoc": t_libdoc}


async def execute(ctx: ToolContext, name: str, args: dict) -> ToolResult:
    """Вызывает нужный инструмент по имени, обрабатывает ошибки и возвращает ToolResult."""
    if name not in TOOL_NAMES:
        return ToolResult(f"Неизвестный инструмент {name}. Доступны: {', '.join(sorted(TOOL_NAMES))}.", ok=False)
    if name in ("plan", "decide"):  # состояние живёт в сессии — обрабатывает агент
        return ToolResult(f"{name} обрабатывается агентом.", ok=False)
    try:
        if name == "run":
            return await t_run(ctx, **args)
        if name == "launch":
            return await asyncio.to_thread(t_launch, ctx, **args)
        return await asyncio.to_thread(SYNC[name], ctx, **args)
    except TypeError as e:
        return ToolResult(f"Неверные аргументы {name}: {e}", ok=False)
    except (ToolError, WorkspaceError) as e:
        return ToolResult(f"Ошибка: {e}", ok=False)
    except OSError as e:
        return ToolResult(f"Ошибка ввода-вывода: {e}", ok=False)
