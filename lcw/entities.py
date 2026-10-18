"""
Индекс сущностей проекта: модули, классы, функции и методы с сигнатурами, диапазонами
строк и первой строкой докстринга. Нужен для экономии контекста: вместо чтения файлов
целиком модель ориентируется по карте сущностей и читает только нужную функцию.

Python разбирается через ast, JS/TS — регулярками (без диапазона конца — до следующей
сущности). Результат кэшируется по mtime файла, так что повторные запросы дешёвые.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from .workspace import list_files

DOC_CHARS = 110         # первая строка докстринга
SIG_CHARS = 90          # сигнатура
JS_EXT = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx")


@dataclass
class Entity:
    kind: str           # class | def | method
    name: str           # квалифицированное имя: Class.method
    sig: str            # (a, b=…) для функций, (Base) для классов
    line: int           # первая строка (с декораторами)
    end: int            # последняя строка
    doc: str = ""

    def render(self, path: str = "") -> str:
        where = f"{path}:" if path else ""
        doc = f" — {self.doc}" if self.doc else ""
        return f"{where}{self.line}-{self.end} {self.kind} {self.name}{self.sig}{doc}"


# (root, rel) -> (mtime_ns, module_doc, entities)
_CACHE: dict[tuple[str, str], tuple[int, str, list[Entity]]] = {}


def _first_line(doc: str | None) -> str:
    if not doc:
        return ""
    for line in doc.strip().splitlines():
        line = line.strip()
        if line:
            return line[:DOC_CHARS] + ("…" if len(line) > DOC_CHARS else "")
    return ""


def _args(a: ast.arguments, method: bool = False) -> str:
    parts = []
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for k, (arg, d) in enumerate(zip(pos, defaults)):
        if method and k == 0 and arg.arg in ("self", "cls"):
            continue
        parts.append(arg.arg + ("=…" if d is not None else ""))
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    parts += [k.arg + ("=…" if d is not None else "") for k, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    s = "(" + ", ".join(parts) + ")"
    return s if len(s) <= SIG_CHARS else s[:SIG_CHARS - 2] + "…)"


def _start(node) -> int:
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])


def _py_entities(text: str) -> tuple[str, list[Entity]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return "(синтаксическая ошибка — индекс недоступен)", []
    out: list[Entity] = []

    def walk(body, prefix: str = "", in_class: bool = False, depth: int = 0):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "method" if in_class else "def"
                if isinstance(n, ast.AsyncFunctionDef):
                    kind = "async " + kind
                out.append(Entity(kind, prefix + n.name, _args(n.args, in_class), _start(n),
                                  n.end_lineno or n.lineno, _first_line(ast.get_docstring(n))))
            elif isinstance(n, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in n.bases)[:SIG_CHARS]
                out.append(Entity("class", prefix + n.name, f"({bases})" if bases else "", _start(n),
                                  n.end_lineno or n.lineno, _first_line(ast.get_docstring(n))))
                if depth < 2:
                    walk(n.body, prefix + n.name + ".", True, depth + 1)

    walk(tree.body)
    return _first_line(ast.get_docstring(tree)), out


_JS_RX = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:(?P<async>async)\s+)?"
    r"(?:function\s*\*?\s*(?P<fn>[\w$]+)\s*(?P<fa>\([^)]*\))"
    r"|class\s+(?P<cls>[\w$]+)(?:\s+extends\s+(?P<base>[\w$.]+))?"
    r"|(?:const|let|var)\s+(?P<var>[\w$]+)\s*=\s*(?:async\s*)?(?:function\b[^(]*)?(?P<va>\([^)]*\))\s*(?:=>|\{))")
_JS_DOC = re.compile(r"^\s*(?://+|/\*+|\*)\s?(.*?)\s*(?:\*/)?\s*$")


def _js_entities(text: str) -> tuple[str, list[Entity]]:
    lines = text.split("\n")
    out: list[Entity] = []
    tops: list[int] = []  # первая строка комментария над сущностью
    for i, line in enumerate(lines, 1):
        m = _JS_RX.match(line)
        if not m:
            continue
        doc = ""
        j = i - 2
        while j >= 0 and _JS_DOC.match(lines[j]) and lines[j].strip():
            t = _JS_DOC.match(lines[j]).group(1).strip()
            if t and not t.startswith("@"):
                doc = t
            j -= 1
        tops.append(j + 2)
        if m.group("cls"):
            out.append(Entity("class", m.group("cls"), f"({m.group('base')})" if m.group("base") else "",
                              i, i, doc[:DOC_CHARS]))
        else:
            name = m.group("fn") or m.group("var")
            sig = re.sub(r"\s+", " ", m.group("fa") or m.group("va") or "()")[:SIG_CHARS]
            kind = ("async " if m.group("async") else "") + "def"
            out.append(Entity(kind, name, sig, i, i, doc[:DOC_CHARS]))
    for k, e in enumerate(out):  # конец — до следующей сущности (и её комментария)
        end = (tops[k + 1] - 1) if k + 1 < len(out) else len(lines)
        while end > e.line and not lines[end - 1].strip():
            end -= 1
        e.end = end
    return "", out


def supported(rel: str) -> bool:
    return rel.endswith(".py") or rel.endswith(JS_EXT)


def file_entities(root: str | Path, rel: str) -> tuple[str, list[Entity]]:
    """(докстринг модуля, сущности) файла; кэш по mtime."""
    p = Path(root) / rel
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return "", []
    key = (str(root), rel)
    hit = _CACHE.get(key)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", []
    doc, ents = _py_entities(text) if rel.endswith(".py") else _js_entities(text)
    _CACHE[key] = (mtime, doc, ents)
    return doc, ents


def _files(root: str | Path, base: str = "") -> list[str]:
    base = base.strip("/")
    pre = base + "/" if base and base != "." else ""
    return [f for f in list_files(root) if supported(f) and f.startswith(pre)]


def module_map(root: str | Path, max_chars: int = 6000) -> str:
    """
    Карта модулей для системного промпта: одна строка на файл —
    докстринг модуля и верхнеуровневые классы/функции.
    """
    lines, total = [], 0
    files = _files(root)
    for n, rel in enumerate(files):
        doc, ents = file_entities(root, rel)
        top = [e.name for e in ents if "." not in e.name and not e.name.startswith("_")]
        if not doc and not top:
            continue
        names = ", ".join(top[:12]) + (f" …(+{len(top) - 12})" if len(top) > 12 else "")
        line = f"{rel}" + (f" — {doc}" if doc else "") + (f" [{names}]" if names else "")
        if total + len(line) > max_chars:
            lines.append(f"… ещё {len(files) - n} модулей — смотри через symbols")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def outline(root: str | Path, path: str = "", query: str = "", limit: int = 60) -> str:
    """
    Запрос к индексу. path=файл без query — все сущности файла; query — поиск по имени
    и докстрингу (подстрока без учёта регистра; варианты через «|»), по файлу или папке;
    папка без query — карта модулей этой папки.
    """
    root = Path(root)
    rel = path.strip().replace("\\", "/").removeprefix("./").strip("/")
    is_file = bool(rel) and (root / rel).is_file()
    if is_file and not supported(rel):
        return f"{rel}: индекс сущностей есть только для .py/.js/.ts — используй read."
    files = [rel] if is_file else _files(root, rel)
    if not files:
        return "Нет файлов с кодом по этому пути."
    words = [w.strip().lower() for w in query.split("|") if w.strip()]
    if is_file and not words:
        doc, ents = file_entities(root, rel)
        head = f"{rel}" + (f" — {doc}" if doc else "")
        body = "\n".join("  " * e.name.count(".") + e.render() for e in ents)
        return head + ("\n" + body if body else "\n(нет классов/функций)")
    if not words:
        return module_map(root, 8000) if not rel else _dir_map(root, files)
    hits = []
    for f in files:
        doc, ents = file_entities(root, f)
        if any(w in f.lower() or w in doc.lower() for w in words):
            hits.append(f"{f} — модуль" + (f": {doc}" if doc else ""))
        for e in ents:
            if any(w in e.name.lower() or w in e.doc.lower() for w in words):
                hits.append(e.render(f))
    if not hits:
        return "Нет совпадений в именах и докстрингах. Попробуй другие слова или grep."
    more = f"\n…и ещё {len(hits) - limit} — уточни запрос или path" if len(hits) > limit else ""
    return "\n".join(hits[:limit]) + more


_WORD = re.compile(r"[A-Za-zА-Яа-яЁё_]{5,}")
_STOP = {"должны", "должен", "только", "работы", "работу", "использ", "попада", "самые", "через", "чтобы",
         "которы", "всегда", "нужно", "сделай", "сделать", "добавь", "after", "should", "which", "there"}


def related(root: str | Path, text: str, limit: int = 12) -> list[str]:
    """
    Сущности проекта, связанные с текстом требования: совпадение основ слов (первые 6 букв —
    грубо снимает окончания в русском) с именами и докстрингами; больше совпадений — выше.
    Даёт маленькой модели готовую карту «где смотреть» вместо поиска вслепую.
    """
    stems = {w.lower()[:6] for w in _WORD.findall(text)} - _STOP
    if not stems:
        return []
    scored = []
    for f in _files(root):
        doc, ents = file_entities(root, f)
        hay = (f + " " + doc).lower()
        n = sum(st in hay for st in stems)
        if n >= 2:
            scored.append((n + 0.5, f"{f} — модуль" + (f": {doc}" if doc else "")))
        for e in ents:
            hay = (e.name + " " + e.doc).lower()
            n = sum(st in hay for st in stems)
            if n:
                scored.append((n, e.render(f)))
    scored.sort(key=lambda x: -x[0])
    return [line for _, line in scored[:limit]]


_COMMON = {"__init__", "__repr__", "__str__", "__eq__", "__hash__", "main", "run", "setup", "teardown"}
DUP_WORDS = re.compile(r"дубл|однотип|совмест|объедин|общ\w* (модул|интерфейс|структур)|duplicat|unif", re.I)


def _body_key(node: ast.AST) -> str:
    """Отпечаток тела функции без имени, докстринга и позиций — одинаковый у копий."""
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(getattr(node.body[0], "value", None), ast.Constant)) else node.body
    return ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)


def duplicates(root: str | Path, limit: int = 20) -> list[str]:
    """
    Кандидаты на объединение: одноимённые функции/методы в разных модулях и функции с одинаковым
    телом. Даёт модели конкретный список вместо задачи «найди дубли во всём проекте».
    """
    root = Path(root)
    by_name: dict[str, list[str]] = {}
    by_body: dict[str, list[str]] = {}
    for f in _files(root):
        if not f.endswith(".py") or re.search(r"(^|/)tests?/|test_", f):
            continue
        try:
            tree = ast.parse((root / f).read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name not in _COMMON:
                where = f"{f}:{n.lineno}"
                by_name.setdefault(n.name, []).append(where)
                if len(n.body) > 1 or not isinstance(n.body[0], (ast.Pass, ast.Return)):
                    by_body.setdefault(_body_key(n), []).append(f"{where} {n.name}")
    out = []
    for name, places in sorted(by_name.items(), key=lambda x: -len(x[1])):
        files = {p.rsplit(":", 1)[0] for p in places}
        if len(files) >= 2:
            out.append(f"одноимённые {name}(): " + ", ".join(places[:6]))
    for places in by_body.values():
        if len({p.split(":", 1)[0] for p in places}) >= 2:
            out.append("одинаковое тело: " + ", ".join(places[:6]))
    return out[:limit]


def _dir_map(root: Path, files: list[str]) -> str:
    out = []
    for f in files[:150]:
        doc, ents = file_entities(root, f)
        top = [e.name for e in ents if "." not in e.name]
        out.append(f"{f}" + (f" — {doc}" if doc else "") + (f" [{', '.join(top[:15])}]" if top else ""))
    if len(files) > 150:
        out.append(f"…и ещё {len(files) - 150} файлов")
    return "\n".join(out)


def find(root: str | Path, rel: str, symbol: str) -> Entity:
    """Сущность по имени («Class.method» или уникальное короткое имя). Бросает LookupError."""
    _, ents = file_entities(root, rel)
    sym = symbol.strip().removesuffix("()")
    exact = [e for e in ents if e.name == sym]
    if len(exact) == 1:
        return exact[0]
    tail = [e for e in ents if e.name.rsplit(".", 1)[-1] == sym.rsplit(".", 1)[-1]]
    if len(tail) == 1:
        return tail[0]
    cands = ", ".join(e.name for e in (tail or ents)[:20])
    if tail:
        raise LookupError(f"«{symbol}» неоднозначно в {rel}: {cands}. Укажи полное имя.")
    raise LookupError(f"«{symbol}» не найден в {rel}. Есть: {cands or '—'}")
