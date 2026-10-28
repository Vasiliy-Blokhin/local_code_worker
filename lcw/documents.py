"""
Документы для инструментов: текст PDF и DOCX (чтение и grep как обычного текста) и запись
.docx из Markdown. Не все проекты — код: задания, конспекты и ответы лежат в PDF/DOCX, а
маленькая модель, увидев «бинарный файл», бросает задачу или пишет заглушку-парсер.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

DOC_EXT = {".pdf", ".docx"}
BINARY_MARK = "\0b64:"  # снимок бинарного файла для отката: метка + base64

_cache: dict[tuple[str, int], str] = {}


def is_document(p: Path) -> bool:
    return p.suffix.lower() in DOC_EXT


def docs_project(root) -> bool:
    """В проекте нет исходного кода (только документы, материалы, данные): гейты для кода не к месту."""
    from .entities import supported
    from .workspace import list_files
    return not any(supported(f) for f in list_files(root))


def _pdftotext() -> str | None:
    """Находит путь к исполняемому файлу pdftotext, проверяя системный PATH и стандартные каталоги Git."""
    found = shutil.which("pdftotext")
    if found:
        return found
    for base in (r"C:\Program Files\Git\mingw64\bin", r"C:\Program Files (x86)\Git\mingw64\bin"):
        cand = Path(base) / "pdftotext.exe"
        if cand.is_file():
            return str(cand)
    return None


# в кириллических TeX-шрифтах эти коды — «ё», «Ё» и кавычки, а не «ј», «Ј», «ѕ», «ї» из cp1251
TEX_CYR = {"¼": "ё", "£": "Ё", "¾": "«", "¿": "»", "\x15": "—"}


def fix_mojibake(text: str) -> str:
    """
    Кириллица из PDF со шрифтами без ToUnicode приходит как cp1251, прочитанная как latin-1
    («Çàäà÷à» вместо «Задача»). Если таких букв много, а настоящей кириллицы почти нет — перекодируем.
    """
    latin = sum(1 for c in text if "\u00c0" <= c <= "\u00ff")
    cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
    if latin < 20 or cyr > latin:
        return text
    out = []
    for c in text:
        if c in TEX_CYR:
            c = TEX_CYR[c]
        elif "\u00a0" <= c <= "\u00ff":
            try:
                c = c.encode("latin-1").decode("cp1251")
            except UnicodeDecodeError:
                pass
        out.append(c)
    return "".join(out)


def _page_with_scripts(page) -> str:
    """
    Текст страницы с индексами: обычный extract_text склеивает «C^k_{2n+1}» в «Ck2n+1», и модель
    читает формулу неверно. Здесь кусок мелкого шрифта выше базовой линии строки становится ^{…},
    ниже — _{…}. Страницы без формул (нет мелкого шрифта со сдвигом) — как обычно.
    """
    chunks = []

    def visit(text, cm, tm, font, size):
        """Собирает фрагменты текста PDF в список с координатами и размером шрифта, если текст не пустой."""
        if text.strip():
            # первый элемент — порядок в потоке: x у коротких фрагментов pypdf часто не обновляет
            chunks.append((len(chunks), tm[5] * cm[3] + cm[5], abs(size * tm[3] * cm[3]) or size, text))
    # Сохраняем текст, если он не пустой
    plain = page.extract_text(visitor_text=visit) or ""
    if not chunks:
        return plain
    weight: dict[float, int] = {}
    for *_, size, text in chunks:
        weight[round(size, 1)] = weight.get(round(size, 1), 0) + len(text)
    body = max(weight, key=weight.get)
    symbol = lambda c: len(c[3].strip()) <= 2 and not c[3].strip().isalnum()  # ∑, большие скобки
    big = [c for c in chunks if c[2] >= body * 0.9 and not symbol(c)]
    # Разделяем большие и остальные фрагменты
    rest = [c for c in chunks if c[2] < body * 0.85 or (c[2] >= body * 0.9 and symbol(c))]
    if not big or not any(c[2] < body * 0.85 for c in rest):
        return plain
    # строки — по базовым линиям обычного текста
    lines: list[list] = []
    for c in sorted(big, key=lambda c: -c[1]):
        if lines and abs(lines[-1][0] - c[1]) < body * 0.3:
            lines[-1][1].append((c[0], "", c[3]))
        else:
            lines.append([c[1], [(c[0], "", c[3])]])
    shifted = 0
    for x, y, size, text in rest:
        # Находим ближайшую строку по вертикали
        base = min(lines, key=lambda l: abs(l[0] - y))
        if abs(base[0] - y) > body * 1.2:  # сноска или подпись — отдельная строка
            lines.append([y, [(x, "", text)]])
            continue
        mode = ""
        # Определяем, над или под базовой линией
        if size < body * 0.85:
            mode = "^" if y > base[0] + body * 0.12 else "_" if y < base[0] - body * 0.08 else ""
        shifted += bool(mode)
        base[1].append((x, mode, text))
    if not shifted:
        return plain
    out = []
    for _, parts in sorted(lines, key=lambda l: -l[0]):
        parts.sort(key=lambda t: t[0])
        line, sup, sub = "", "", ""

        def flush():
            """Формирует строку с надписями над и под символом, учитывая специальные правила для сумм и
            подстрочных обозначений.
            """
            nonlocal line, sup, sub
            sup, sub = sup.strip(), sub.strip()
            if sup and sub.startswith("=") and len(sup) >= 2 and sup[-1].isalpha():
                sup, sub = sup[:-1], sup[-1] + sub  # ∑: «nk» сверху и «=0» снизу — это «n» и «k=0»
            for mark, s in (("^", sup), ("_", sub)):
                if s:
                    line += f"{mark}{s}" if len(s) == 1 else f"{mark}{{{s}}}"
            sup = sub = ""
        for _, mode, text in parts:
            text = text.replace("\n", "")
            if mode == "^":
                sup += text
            elif mode == "_":
                sub += text
            else:
                flush()
                line += text
        flush()
        out.append(line.rstrip())
    return "\n".join(out)


def pdf_pages(p: Path) -> list[str]:
    """pypdf сохраняет формулы (∑, ∆, индексы), pdftotext их теряет — он только запасной путь."""
    try:
        import pypdf
        pages = [_page_with_scripts(page) for page in pypdf.PdfReader(str(p)).pages]
        if any(t.strip() for t in pages):
            return pages
    except Exception:
        pass
    exe = _pdftotext()
    if not exe:
        raise RuntimeError("нет ни pypdf, ни pdftotext")
    r = subprocess.run([exe, "-enc", "UTF-8", str(p), "-"], capture_output=True, timeout=300)
    return r.stdout.decode("utf-8", errors="replace").split("\f")


def docx_text(p: Path) -> str:
    """Извлекает текст из DOCX, преобразуя таблицы и разметку в обычный текст с сохранением переносов и
    табуляции.
    """
    with zipfile.ZipFile(p) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="replace")
    def para_text(para: str) -> str:
        parts = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:(br|tab)/>", para)
        return "".join(t if t else ("\n" if tag == "br" else "\t") for t, tag in parts)
    paras = []
    for block in re.findall(r"<w:tr[ >].*?</w:tr>|<w:p[ >].*?</w:p>", xml, re.S):
        if block.startswith("<w:tr"):  # строка таблицы — одной строкой «ячейка | ячейка»
            cells = re.findall(r"<w:tc[ >].*?</w:tc>", block, re.S)
            paras.append(" | ".join(" ".join(para_text(q) for q in re.findall(r"<w:p[ >].*?</w:p>", c, re.S))
                                    for c in cells))
        else:
            paras.append(para_text(block))
    text = "\n".join(paras)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")


def document_text(p: Path) -> str:
    """Текст документа (кэш по mtime): страницы PDF отмечены строками «=== стр. N ===»."""
    key = (str(p), p.stat().st_mtime_ns)
    if key in _cache:
        return _cache[key]
    if p.stat().st_size == 0:
        text = ""
    elif p.suffix.lower() == ".pdf":
        pages = pdf_pages(p)
        if pages and not pages[-1].strip():
            pages.pop()
        text = "\n".join(f"=== стр. {i} ===\n{fix_mojibake(t).strip()}" for i, t in enumerate(pages, 1))
    else:
        text = docx_text(p)
    text = re.sub(r"[ \t]+\n", "\n", text)
    _cache[key] = text
    return text


PROBLEM = re.compile(r"^\s*(?:Задач[аa]|Задание|Упражнение|Problem|Exercise|Task)\s*№?\s*(\d+)\s*[.:)]?",
                     re.IGNORECASE | re.MULTILINE)
SOLVE = re.compile(r"реш(и|ить|ите|ение)|выполн\w* (задан|задач|упражн)|solve|answer the", re.IGNORECASE)
DOC_REF = re.compile(r"[\w./\\-]+\.(?:pdf|docx|txt|md)\b", re.IGNORECASE)


def split_problems(text: str) -> list[tuple[str, str]]:
    """«Задача 1 … Задача 2 …» → [(номер, текст задачи)]; без отметок страниц и номеров страниц."""
    marks = list(PROBLEM.finditer(text))
    out = []
    for i, m in enumerate(marks):
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        body = re.sub(r"^=== стр\. \d+ ===$|^\s*\d{1,3}\s*$", "", body, flags=re.M)
        out.append((m.group(1), re.sub(r"\n{2,}", "\n", body).strip()))
    return out if len(out) >= 2 else []


def problem_items(root, item: str) -> list[str]:
    """
    Пункт «реши задачи из task.pdf» → по подпункту на задачу (с её текстом): все задачи сразу
    маленькая модель не решает — обрывается на лимите ответа или решает наспех.
    """
    if not SOLVE.search(item):
        return []
    for ref in DOC_REF.findall(item):
        p = Path(root) / ref
        if not p.is_file():
            continue
        try:
            text = document_text(p) if is_document(p) else p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        probs = split_problems(text)
        if probs:
            return [f"Задача {n} из {ref} (в рамках пункта «{item[:120]}»):\n{body[:1500]}" for n, body in probs]
    return []


_HEAD = re.compile(r"^(#{1,3}) +(.+?)\s*$", re.M)


def _head_key(head: str) -> str:
    """Заголовок для сравнения: «Задача 3» с неразрывным пробелом, «задача 3.» и «Задача 3» — один раздел."""
    return re.sub(r"\s+", " ", head.replace("*", "")).strip(" .:").casefold()


def _sections(md: str, level: int | None = None) -> list[tuple[str, str]]:
    """
    Markdown → [(заголовок или '' для вступления, текст раздела)]. Делим по самому крупному уровню,
    который встречается не раз («## Задача N»), — подразделы «### a)» остаются внутри разделов.
    """
    heads = list(_HEAD.finditer(md))
    if level is None:
        counts = [sum(1 for h in heads if len(h.group(1)) == n) for n in (1, 2, 3)]
        level = next((n for n in (1, 2, 3) if counts[n - 1] >= 2), 1)
    marks = [h for h in heads if len(h.group(1)) <= level]
    out = [("", md[:marks[0].start()] if marks else md)]
    for i, m in enumerate(marks):
        out.append((_head_key(m.group(2)), md[m.start(): marks[i + 1].start() if i + 1 < len(marks) else len(md)]))
    return out


def dedupe_sections(md: str) -> str:
    """Раздел с тем же заголовком, записанный ещё раз, заменяет прежний (на его месте), а не дублирует его."""
    secs = _sections(md)
    last = {h: i for i, (h, _) in enumerate(secs) if h}
    seen, out = set(), []
    for i, (h, body) in enumerate(secs):
        if not h:
            out.append(body)
        elif h not in seen:
            seen.add(h)
            out.append(secs[last[h]][1].rstrip("\n") + "\n\n")
    return "".join(out).rstrip("\n") + "\n"


def upsert_section(src: str, new: str) -> str:
    """Дописать раздел; если раздел с таким заголовком уже есть — заменить его."""
    m = _HEAD.match(new.lstrip("\n"))
    if m and any(h == _head_key(m.group(2)) for h, _ in _sections(src, len(m.group(1)))):
        return dedupe_sections(src.rstrip("\n") + "\n\n" + new.strip("\n") + "\n")
    return (src.rstrip("\n") + "\n\n" if src.strip() else "") + new.strip("\n") + "\n"


def doc_issues(p: Path) -> list[str]:
    """Проблемы собранного документа, которые модель сама не замечает: раздел записан дважды."""
    if not p.is_file() or p.stat().st_size == 0:
        return []
    src = docx_source(p) if p.suffix.lower() == ".docx" else None
    text = src or (document_text(p) if is_document(p) else p.read_text(encoding="utf-8", errors="replace"))
    heads = [h.strip() for h in re.findall(r"^#{1,3} (.+)$", text, re.M)]
    dups = sorted({h for h in heads if heads.count(h) > 1})
    return [f"раздел «{h}» встречается {heads.count(h)} раза — оставь один (исправленный), лишний удали "
            f"через edit" for h in dups]


def output_file(reqs: list[str]) -> str:
    """Файл результата из пункта вида «ответ сохранить в result.docx» ('' — не указан)."""
    for r in reqs:
        if re.search(r"сохран|запи[шс]|save|write", r, re.IGNORECASE):
            refs = [x for x in DOC_REF.findall(r) if not x.lower().endswith(".pdf")]
            if refs:
                return refs[-1]
    return ""


_STEM = re.compile(r"[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё-]{3,}|[σΣΩ∆Δ∪∩]")


def _stems(text: str) -> set[str]:
    return {w.lower()[:6] for w in _STEM.findall(text)}


def related_excerpts(root, query: str, skip: set[str] = frozenset(), n: int = 2, width: int = 14) -> list[str]:
    """
    Места материалов (документы и тексты проекта), где больше всего редких слов задачи, — как
    «связанный код» для проекта с кодом: модель сразу видит определения и приёмы из лекций.
    """
    from .workspace import list_files
    q = _stems(query) - {"задача", "найдит", "докажи", "покажи", "которы", "являет"}
    if not q:
        return []
    windows = []
    # Обрабатываем каждый найденный файл
    for rel in list_files(root):
        p = Path(root) / rel
        if rel in skip or p.suffix.lower() not in (".pdf", ".docx", ".txt", ".md") or p.stat().st_size > 5_000_000:
            continue
        try:
            # Получаем текст, учитывая тип документа
            text = document_text(p) if is_document(p) else p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        lines = text.split("\n")
        for start in range(0, max(1, len(lines) - width // 2), width // 2):
            chunk = lines[start:start + width]
            # в показ — и продолжение: в лекции приём объясняют уже после слов задачи («стандартный подход…»)
            windows.append((rel, start + 1, lines[start:start + width + 16], _stems("\n".join(chunk)) & q))
    if not windows:
        return []
    df: dict[str, int] = {}
    for *_, found in windows:
        for w in found:
            df[w] = df.get(w, 0) + 1
    import math
    score = lambda found: sum(math.log(1 + len(windows) / df[w]) for w in found)
    # Функция расчёта балла по найденным словам
    best, used = [], set()
    for rel, line, chunk, found in sorted(windows, key=lambda w: -score(w[3])):
        if len(best) >= n or score(found) <= 0:
            break
        if (rel, line // width) in used:
            continue
        used.update({(rel, line // width), (rel, line // width + 1), (rel, line // width - 1)})
        body = "\n".join(l[:220] for l in chunk)
        best.append(f"{rel}, строки {line}-{line + len(chunk) - 1}:\n{body}")
    return best


INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*|(?<!\w)_[^_\s][^_]*_(?!\w))")
BLOCK = re.compile(r"^(#{1,3} |[-*] |\d+[.)] |```|\$\$|\s*\|)")


def _add_inline(paragraph, s: str) -> None:
    """Добавляет в параграф части строки с форматированием: жирный, моноширинный, курсив, или обычный текст,
    разделяя по шаблону INLINE.
    """
    for part in INLINE.split(s):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            paragraph.add_run(part[1:-1]).font.name = "Consolas"
        elif len(part) > 2 and part[0] == part[-1] and part[0] in "*_":
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def _math(doc, line: str) -> None:
    doc.add_paragraph().add_run(line).font.name = "Cambria Math"


LATEX = [  # (шаблон, замена): маленькая модель пишет формулы в LaTeX, а в Word нужен читаемый текст
    (r"\\[()\[\]]", ""),  # \( … \) и \[ … \]
    (r"\\(?:left|right|[bB]igg?[lr]?|displaystyle|limits|quad|qquad)(?![a-zA-Z])|\\[,;:!]", " "),  # «\ » не трогаем: «A \ B» — разность
    (r"\\(?:d|t)?binom\{([^{}]*)\}\{([^{}]*)\}", r"C(\1, \2)"),
    (r"\\(?:d|t)?frac\{(\w+)\}\{(\w+)\}", r"\1/\2"),
    (r"\\(?:d|t)?frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)"),
    (r"\\mathbb\{([RNZQC])\}", lambda m: "ℝℕℤℚℂ"["RNZQC".index(m.group(1))]),
    (r"\\mathfrak\s*\{?c\}?", "𝔠"),
    (r"\\(?:d|t)?frac(\d)(\d)", r"\1/\2"),  # \frac12
    (r"\\(?:dot|bar|hat|tilde|overline|widetilde)\{([^{}]*)\}", r"\1"),
    (r"\\(?:mathbb|mathbf|mathrm|text|textbf|operatorname|mathcal)\{([^{}]*)\}", r"\1"),
    (r"\\sqrt\{([^{}]*)\}", r"√(\1)"),
]
SYMBOLS = {
    "bigcap": "∩", "bigcup": "∪", "cap": "∩", "cup": "∪", "setminus": "\\", "backslash": "\\",
    "triangle": "Δ", "bigtriangleup": "Δ", "triangleup": "Δ", "Delta": "Δ", "vartriangle": "Δ", "sum": "Σ",
    "prod": "Π", "emptyset": "∅", "varnothing": "∅", "infty": "∞", "in": "∈", "notin": "∉", "subseteq": "⊆",
    "subset": "⊂", "supseteq": "⊇", "supset": "⊃", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠",
    "ne": "≠", "cdot": "·", "times": "×", "to": "→", "rightarrow": "→", "Rightarrow": "⇒", "Leftrightarrow": "⇔",
    "iff": "⇔", "mapsto": "↦", "approx": "≈", "sim": "~", "pm": "±", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨", "ldots": "…", "dots": "…", "cdots": "…",
    "Omega": "Ω", "omega": "ω", "sigma": "σ", "Sigma": "Σ", "alpha": "α", "beta": "β", "gamma": "γ",
    "lambda": "λ", "mu": "μ", "pi": "π", "varepsilon": "ε", "epsilon": "ε", "delta": "δ", "mathbb": "",
    "aleph": "ℵ", "circ": "∘", "equiv": "≡", "{": "{", "}": "}", "|": "‖", "mid": "|",
    "varphi": "φ", "phi": "φ", "psi": "ψ", "chi": "χ", "eta": "η", "theta": "θ", "tau": "τ", "upsilon": "υ",
    "rho": "ρ", "xi": "ξ", "zeta": "ζ", "kappa": "κ", "nu": "ν", "Phi": "Φ", "Psi": "Ψ", "Lambda": "Λ",
    "arctan": "arctg ", "arcsin": "arcsin ", "tan": "tg ", "sin": "sin ", "cos": "cos ", "ln": "ln ",
    "log": "log ", "exp": "exp ", "lim": "lim ", "max": "max ", "min": "min ", "sup": "sup ", "inf": "inf ",
    "sim": "∼", "cong": "≅", "hookrightarrow": "↪", "twoheadrightarrow": "↠", "dotsc": "…", "colon": ":",
}
SCRIPT_CHARS = "0123456789+-−=()nkmi"
SUP = str.maketrans(SCRIPT_CHARS, "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁻⁼⁽⁾ⁿᵏᵐⁱ")
SUB = str.maketrans(SCRIPT_CHARS, "₀₁₂₃₄₅₆₇₈₉₊₋₋₌₍₎ₙₖₘᵢ")


def latex_to_unicode(text: str) -> str:
    """$…$, \\cap, \\binom{n}{k}, x^{2n} → ∩, C(n, k), x²ⁿ (вне блоков кода)."""
    if not re.search(r"[\\$^]|_\{", text):
        return text
    t = text
    for _ in range(3):  # вложенные \frac{\binom{..}{..}}{..}
        for rx, rep in LATEX:
            t = re.sub(rx, rep, t)
    def symbol(m):  # «\subsetF» — команда, приклеенная к букве: берём самое длинное известное начало
        """Преобразует LaTeX‑символы, заменяя их на Unicode‑эквиваленты, выбирая самое длинное совпадение из
        словаря SYMBOLS.
        """
        name = m.group(1)
        for n in range(len(name), 0, -1):
            if name[:n] in SYMBOLS:
                return SYMBOLS[name[:n]] + name[n:]
        return m.group(0)
    t = re.sub(r"\\([a-zA-Z]+|[{}|])", symbol, t)

    def script(m):
        """Обрабатывает надстрочные и подстрочные скрипты, переводя их в Unicode‑символы, если содержимое
        состоит из допустимых символов, иначе оставляет как обычный текст.
        """
        mark, body = m.group(1), m.group(2) if m.group(2) is not None else m.group(3)
        if body and all(c in SCRIPT_CHARS for c in body):
            return body.translate(SUP if mark == "^" else SUB)
        return f"{mark}({body})" if len(body) > 1 else mark + body
    t = re.sub(r"([\^_])(?:\{([^{}]*)\}|([0-9a-zA-Z])(?![a-zA-Z]))", script, t)  # file_name не трогаем
    t = re.sub(r"(?<!\$)\$\$?([^$\n]+?)\$\$?(?!\$)", r"\1", t)  # инлайн-формулы $…$ без знаков доллара
    t = re.sub(r"(?<=\S)\\\\?[ \t]*$", "", t, flags=re.M)  # «\» / «\\» в конце строки — перенос из LaTeX
    t = t.replace("{~}", "∼")
    return re.sub(r"[ \t]{2,}", " ", t)


def _source_file(p: Path) -> Path:
    """Генерирует путь к JSON‑файлу исходного кода, используя SHA‑1 хеш от абсолютного пути файла."""
    from . import config
    import hashlib
    return config.DATA_DIR / "docsrc" / (hashlib.sha1(str(Path(p).resolve()).lower().encode()).hexdigest()[:16] + ".json")


def docx_source(p: Path) -> str | None:
    """Markdown, из которого harness собрал этот .docx (если файл с тех пор не меняли снаружи)."""
    import json
    f = _source_file(p)
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d["md"] if d.get("mtime") == p.stat().st_mtime_ns else None
    except (OSError, ValueError, KeyError):
        return None


def write_docx(path, text: str) -> int:
    """Markdown → .docx: заголовки #..###, списки, **жирный**, *курсив*, `код`, блоки ``` и $$. Возвращает число абзацев."""
    import json
    n = _build_docx(path, text)
    f = _source_file(Path(path))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"mtime": Path(path).stat().st_mtime_ns, "md": text}, ensure_ascii=False),
                 encoding="utf-8")
    return n


def _build_docx(path, text: str) -> int:
    """Преобразует Markdown‑текст в Word‑документ, поддерживая заголовки, списки, таблицы, формулы и
    специальные символы, возвращая количество добавленных элементов.
    """
    import docx
    from docx.shared import Pt

    # Обрабатываем экранированные  , чтобы получить реальные переводы строк
    text = re.sub(r"\\n(?=\s|$)", "\n", latex_to_unicode(text))  # буквальный «\n» от пере-экранирования
    doc = docx.Document()
    normal = doc.styles["Normal"].font
    normal.name, normal.size = "Times New Roman", Pt(12)
    lines = text.replace("\r\n", "\n").split("\n")
    count, i = 0, 0
    while i < len(lines):
        line = lines[i]
        # Определяем тип блока: код или формула
        fence = "```" if line.startswith("```") else "$$" if line.startswith("$$") else ""
        if fence:
            rest = line[2:].strip().rstrip("$").strip() if fence == "$$" else ""
            if fence == "$$" and rest:  # $$ формула $$ в одну строку
                _math(doc, rest)
                count, i = count + 1, i + 1
                continue
            i += 1
            while i < len(lines) and not lines[i].startswith(fence):
                _math(doc, lines[i])
                # Добавляем каждую строку формулы в документ
                count, i = count + 1, i + 1
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        if line.lstrip().startswith("|"):  # таблица Markdown → таблица Word
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    rows.append(cells)
                i += 1
            if rows:
                cols = max(len(r) for r in rows)
                table = doc.add_table(rows=len(rows), cols=cols)
                table.style = "Table Grid"
                for r, cells in enumerate(rows):
                    for c, text in enumerate(cells):
                        cell = table.cell(r, c).paragraphs[0]
                        _add_inline(cell, text)
                        if r == 0:
                            for run in cell.runs:
                                run.bold = True
                count += 1
            continue
        m = re.match(r"^(#{1,3}) (.*)", line)
        if m:
            doc.add_heading(m.group(2).strip(), level=len(m.group(1)))
        elif re.match(r"^[-*] ", line):
            _add_inline(doc.add_paragraph(style="List Bullet"), line[2:].strip())
        elif re.match(r"^\d+[.)] ", line):
            _add_inline(doc.add_paragraph(style="List Number"), line.split(" ", 1)[1])
        else:
            para = doc.add_paragraph()
            _add_inline(para, line)
            # Объединяем непрерывные строки в один абзац
            while i + 1 < len(lines) and lines[i + 1].strip() and not BLOCK.match(lines[i + 1]):
                i += 1
                para.add_run().add_break()
                _add_inline(para, lines[i])
        count, i = count + 1, i + 1
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return count
