"""
Агентный цикл: модель ↔ инструменты, подтверждения, чекпоинты, сжатие контекста.

Оптимизация скорости (главное — не пересчитывать промпт):
- system + tools неизменны в течение сессии, история только дописывается —
  llama.cpp берёт префикс из KV-кэша и считает лишь новые токены;
- рассуждения прошлых ходов в запрос не отправляются (их и так отбрасывает
  шаблон gpt-oss) — только в рамках текущего хода, где они нужны модели;
- результаты инструментов компактные и обрезанные, повторные чтения — заглушка;
- сжатие истории запускается заранее (COMPACT_AT), а не по ошибке переполнения.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path

from . import config, entities, equivalence, prompts
from .llm import LLM, Completion, LLMError
from .sessions import Session
from .tools import (MUTATING, TOOLS, ToolContext, ToolError, ToolResult, execute, normalize_plan,
                    GIT_WRITE, git_preview, preview, render_plan, syntax_errors, unused_modules, import_errors,
                    unused_new_imports, new_stubs, project_tests, untested_changes, find_merges, undefined_names,
                    sql_column_errors, dropped_dependencies)
from .workspace import WorkspaceError, get_project, is_text_file, main_checkout, relpath, resolve

UI_OUTPUT = 6000           # сколько вывода инструмента показывать в UI
CHARS_PER_TOKEN = 3.2      # грубая оценка для бюджета контекста
SAFE_CMD = re.compile(r"^\s*(git (status|diff|log|show|branch|remote -v)|ls|dir|pwd|cat|head|tail|wc|"
                      r"tree|echo|python -V|python --version|node -v|which|where)\b[^;&|<>`$]*$")
# отказ/«слишком сложно» вместо работы (en/ru)
REFUSAL = re.compile(r"I'?m sorry|I can(no|')t|cannot (help|do|complete)|beyond what|too (complex|large|big)|"
                     r"not (feasible|possible) (to|in)|provide (a |more )?(clear )?spec|"
                     r"извин|не могу|не (получится|удастся)|слишком (сложн|больш|объ[её]мн)|"
                     r"выходит за рамки|предостав\w* (специфика|подробн|пример)", re.IGNORECASE)
# сбой разбора вызова инструмента в llama.cpp и подсказка для повтора
PARSE_ERROR = re.compile(r"does not match the expected|Failed to parse|unparsed", re.IGNORECASE)
TOOL_FORMAT_HINT = ("[Система] Предыдущий вызов инструмента не распознан: неверный формат заголовка. Вызывай "
                    "ровно один инструмент обычным способом (канал commentary, получатель functions.<имя>, "
                    "аргументы — JSON-объект) — или ответь текстом. Продолжай с того же места.")
# модель просит уточнений вместо работы
ASKS = re.compile(r"уточни|уточнит|нужн\w* (уточнен|решени|информац)|clarif|could you|please (provide|specify)",
                  re.IGNORECASE)
# имя-настройка в сообщении о пропавших зависимостях («…: REVERSE_ENABLED использовался…»)
SETTING = re.compile(r": [A-Z][A-Z0-9]*_[A-Z0-9_]+ использовался")
# запасной вариант решения, выключающий функциональность при отсутствии данных
DISABLING = re.compile(r"не генерир|не созда|не (будут|будет) (генерир|создав|работ|участв)|ничего не|"
                       r"пуст\w* (список|множеств|результат)|отключ|сигнал\w* нет|не выдава|"
                       r"no signals|nothing|disabled?|empty (set|list)", re.IGNORECASE)
# пункт-расследование: причину надо найти по фактам, а не угадать
INVESTIGATE = re.compile(r"причин|проблем|почему|периодическ|отключ|пада[ею]т|лома[ею]т|сбо[ий]|не работа|"
                         r"изуч|исследу|проанализ|investigat|root cause|\bwhy\b|outage|crash|analy[sz]",
                         re.IGNORECASE)
# вызовы, которые собирают факты: логи, конфиги, документы о развёртывании
EVIDENCE_CMD = re.compile(r"\b(ssh|scp|journalctl|tail|zcat|Get-Content|Select-String|grep|wc)\b|/var/log",
                          re.IGNORECASE)
EVIDENCE_PATH = re.compile(r"(^|/)(logs?|deploy|nginx|docs|etc)/|\.(log|conf|inc|service|md|ya?ml)$",
                           re.IGNORECASE)
# в ответе на сверку есть несделанные пункты
UNDONE = re.compile(r"не\s+сделан|не\s+выполнен|частично|not\s+done|partial", re.IGNORECASE)
# до плана разрешены проверки, которые ничего не меняют в коде: тесты, компиляция, импорт
PRE_PLAN_CMD = re.compile(r"^\s*(\S*python(\.exe)?\s+-m\s+(pytest|py_compile|compileall)|\S*pytest(\.exe)?|"
                          r"npm (test|run test)|go test|cargo test)\b")
# просмотр файлов через shell — для этого есть read/symbols/grep
FILE_VIEW = re.compile(r"^\s*(cat|head|tail|less|more|type|sed\s+-n|awk)\s+[^|;&]*\.(py|js|ts|tsx|jsx|json|"
                       r"md|txt|toml|ya?ml|cfg|ini|html|css)\b[^|;&]*$")


def _approval_needed(s: Session, name: str, args: dict) -> bool:
    mode = s.settings.get("mode", "edits")
    if name == "git_commit":  # коммит/пуш — всегда с подтверждением, в любом режиме
        return True
    if name in s.settings.get("always", []):
        return False
    if name == "run":
        return mode != "auto" and not SAFE_CMD.match(args.get("command", ""))
    if name in MUTATING:
        return mode == "ask"
    return False


def _ui_args(name: str, args: dict) -> dict:
    a = dict(args)
    if name == "write" and isinstance(a.get("content"), str):
        a["content"] = f"<{a['content'].count(chr(10)) + 1} строк>"
    for k in ("old", "new"):
        if isinstance(a.get(k), str) and len(a[k]) > 1500:
            a[k] = a[k][:1500] + "…"
    return a


def build_request(s: Session) -> list[dict]:
    """Запрос к модели: system + история; reasoning — только текущего хода."""
    last_user = max((i for i, m in enumerate(s.messages) if m["role"] == "user"), default=0)
    out = [{"role": "system", "content": s.system}]
    for i, m in enumerate(s.messages):
        if "reasoning_content" in m and i < last_user:
            m = {k: v for k, v in m.items() if k != "reasoning_content"}
        out.append(m)
    return out


def _estimate_tokens(s: Session) -> int:
    """Оценка размера промпта: точный счёт из прошлого ответа + прирост по символам."""
    base = s.usage.get("ctx_used", 0)
    tail = s.messages[s.usage.get("msg_count", 0):] if base else s.messages
    chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in tail)
    if not base:
        chars += len(s.system) + len(json.dumps(TOOLS))
    return base + int(chars / CHARS_PER_TOKEN)


_REQ = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.{8,})$", re.M)


def requirements(text: str) -> list[str]:
    """Нумерованные/маркированные пункты запроса пользователя (для сверки результата)."""
    if not isinstance(text, str) or text.startswith("[Сводка"):
        return []
    return [m.group(1).strip()[:300] for m in _REQ.finditer(text.split("\n\n<file ")[0])][:12]


def evidence_hint(root: Path) -> str:
    """Где искать факты: логи в проекте и документы, где сказано, как добраться до логов на сервере."""
    root = Path(root)
    logs = []
    main = main_checkout(root)
    for d in ("logs", "log"):
        where = root / d if (root / d).is_dir() else main / d if main and (main / d).is_dir() else None
        if where:
            files = [q for q in where.rglob("*") if q.is_file()]
            if files:
                logs.append(f"{d}/ ({len(files)} файлов: " + ", ".join(q.name for q in files[:6])
                            + ("…" if len(files) > 6 else "") + ")")
    docs, commands = [], []
    for q in sorted(list(root.glob("*.md")) + list(root.glob("docs/**/*.md")) + list(root.glob("deploy/**/*"))):
        if not q.is_file() or q.stat().st_size > 500_000:
            continue
        try:
            text = q.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = []
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"journalctl|/var/log|access\.log|ssh -i", line):
                hits.append(n)
                # готовые команды из документации: ssh-подключение и чтение логов на сервере
                for cmd in re.findall(r"`([^`]*(?:ssh -i|journalctl|tail [^`]*log)[^`]*)`", line):
                    if cmd not in commands and not re.search(r"\b(rm|nano|vi|restart|stop|start|reboot)\b|tail -f", cmd):
                        commands.append(cmd)
        if hits:
            docs.append(f"{q.relative_to(root).as_posix()} (строки {', '.join(map(str, hits[:5]))})")
    out = ("\nЭто расследование: сначала найди причину по фактам, потом правь. `logstats` даёт сводку лога "
           "(пики по времени, топ адресов/статусов/сообщений); сравни время сбоев с тем, что делает код. "
           "В decide укажи cause — причину с фактами.")
    if logs:
        out += "\nЛоги в проекте: " + "; ".join(logs)
    if docs:
        out += "\nГде описаны логи/сервер (команды — только чтение): " + "; ".join(docs[:6])
    if commands:
        out += ("\nКоманды из документации (сервер — только читать, ничего не менять): "
                + "; ".join(f"`{c}`" for c in commands[:6])
                + "\nЛог с сервера разбирай так: run `ssh … \"cat /путь/к/логу\" > logs/server_<имя>.log` "
                  "(через ssh из этих команд, без sudo, если лог читается и так), затем logstats path=logs/server_<имя>.log.")
    return out


def focus_message(root: Path, reqs: list[str], k: int, first: bool = False) -> str:
    """
    Задание на один пункт многопунктного запроса + связанный код из индекса сущностей.
    Маленькая модель не удерживает несколько крупных требований сразу и не знает, где их
    код, — поэтому harness выдаёт их по одному и сразу показывает, куда смотреть.
    """
    rel = entities.related(root, reqs[k], 12)
    dups = entities.duplicates(root, 12) if entities.DUP_WORDS.search(reqs[k]) else []
    head =(f"[Работа по пунктам] В запросе {len(reqs)} пунктов — выполняй их по очереди, по одному. "
            if first else "[Следующий пункт] ")
    return (head + f"Сейчас пункт {k + 1}: {reqs[k]}\n"
            "Порядок: изучи связанный код (read с symbol=) и как он используется (grep) → реши, что "
            "конкретно меняешь в СУЩЕСТВУЮЩЕМ коде → plan с файлами и функциями → правки по шагам с "
            "проверкой. Не подменяй работу пустыми методами, заглушками и новыми неиспользуемыми файлами."
            + ("\nКандидаты на объединение (найдены автоматически; сравни реализации через read, объединяй "
               "только действительно одинаковое, вызовы обнови через grep):\n" + "\n".join(dups) if dups else "")
            + (evidence_hint(root) if INVESTIGATE.search(reqs[k]) else "")
            + ("\nСвязанный код (имена и докстринги из индекса):\n" + "\n".join(rel) if rel else ""))


def _session_originals(s: Session) -> dict:
    """Файл → его содержимое до первой правки в сессии (None — файл создан агентом)."""
    out: dict = {}
    for cp in sorted(s.checkpoints, key=lambda c: c["turn"]):
        for rel, text in cp["files"].items():
            out.setdefault(rel, text)
    return out


def session_root(s: Session) -> Path:
    """Папка, в которой работает сессия: изолированный worktree или сам проект."""
    if s.worktree:
        return Path(s.worktree["path"])
    return Path(get_project(s.project_id)["path"])


def expand_mentions(root: Path, text: str, s: Session) -> str:
    """@путь/к/файлу в сообщении → содержимое файла прикладывается сразу (экономит шаг read)."""
    files = []
    for m in re.finditer(r"(?<![\w@])@([\w./\\-]+)", text):
        try:
            p = resolve(root, m.group(1))
        except WorkspaceError:
            continue
        if p.is_file() and is_text_file(p) and p.stat().st_size < 200_000:
            rel = relpath(root, p)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            body = "\n".join(f"{i}│{l}" for i, l in enumerate(lines[:400], 1))
            if len(lines) > 400:
                body += f"\n…(ещё {len(lines) - 400} строк, читай через read с offset=401)"
            files.append(f'<file path="{rel}">\n{body}\n</file>')
            s.seen[(rel, 1, 400)] = p.stat().st_mtime_ns
    return text + ("\n\n" + "\n".join(files) if files else "")


class Agent:
    def __init__(self, llm: LLM):
        self.llm = llm

    # ------------------------------------------------------------------ ход
    async def run_turn(self, s: Session, text: str) -> None:
        s.running = True
        s.push_meta()
        try:
            root = session_root(s)
            if text.strip() == "/compact":
                await self.compact(s, manual=True)
                return
            if text.strip() == "/init":
                text = prompts.INIT
            s.turn += 1
            checkpoint = {"turn": s.turn, "files": {}}
            s.checkpoints.append(checkpoint)
            s.add({"type": "user", "text": text, "turn": s.turn, "msg_index": len(s.messages)})
            content = expand_mentions(root, text, s)
            reqs = requirements(text)
            if len(reqs) >= 2:  # новое многопунктное задание: ведём модель по пунктам по очереди
                s.focus, s.decisions = 0, {}
                content += "\n\n" + await asyncio.to_thread(focus_message, root, reqs, 0, True)
            s.messages.append({"role": "user", "content": content})
            if s.title == "Новая сессия":
                s.title = text.strip().splitlines()[0][:70] or s.title
            ctx = ToolContext(root=root, seen=s.seen,
                              snapshot=lambda rel, txt: checkpoint["files"].setdefault(rel, txt))
            await self._loop(s, ctx)
        except asyncio.CancelledError:
            s.add({"type": "notice", "level": "warn", "text": "Остановлено пользователем."})
        except (LLMError, WorkspaceError) as e:
            s.add({"type": "notice", "level": "error", "text": str(e)})
        except Exception as e:  # не роняем сервер из-за одной сессии
            s.add({"type": "notice", "level": "error", "text": f"Внутренняя ошибка: {e!r}"})
        finally:
            for f in s.pending.values():
                f.cancel()
            s.pending.clear()
            # вызов инструмента без результата (прервали посреди шага) ломает историю — закрываем его
            last = s.messages[-1] if s.messages else {}
            if last.get("tool_calls"):
                s.messages.append({"role": "tool", "tool_call_id": last["tool_calls"][0]["id"],
                                   "content": "Прервано пользователем."})
            for i, it in enumerate(s.items):
                if it.get("status") in ("running", "approve"):
                    s.set(i, status="denied", output="прервано")
                elif it.get("type") == "reasoning" and not it.get("done"):
                    s.set(i, done=True)
            s.running = False
            s.task = None
            s.save()
            s.push_meta()

    async def _loop(self, s: Session, ctx: ToolContext) -> None:
        last_call = None
        nudged = False
        fixes = 0
        plan_nudges = 0
        orphan_nudges = import_nudges = stub_nudges = test_runs = dep_nudges = untested_nudges = 0
        dropped_seen: set[str] = set()
        tested_sig = None
        merge_sig, merge_nudges = None, 0  # дифференциальная проверка объединений: по чему и сколько раз
        refused = reviewed = pushed = False
        reviewed_at = 0
        # исходная задача хода («продолжай» — значит задача из предыдущих сообщений пользователя)
        # (берём из элементов UI: там только реальные сообщения пользователя, без служебных)
        task = next((it["text"] for it in reversed(s.items) if it.get("type") == "user"
                     and len(requirements(it.get("text", ""))) >= 2), "")
        # состояние хода: составлен ли план, было ли уже напоминание, элемент плана в UI
        # незакрытый план прошлого хода («продолжай») остаётся в силе — заново не требуем
        carried = any(x["status"] in ("todo", "doing") for x in s.plan)
        st = {"planned": carried, "plan_rejects": 0, "plan_item": None, "since_plan": 0, "tools_used": 0,
              "edits": 0, "reqs": requirements(task), "decide_rejects": 0}
        focus_edits, item_pushed = 0, False  # правок к началу текущего пункта; было ли дожатие пункта
        for _ in range(config.MAX_STEPS):
            if _estimate_tokens(s) > config.COMPACT_AT * config.CONTEXT:
                await self.compact(s)
            comp = await self._call(s)
            if not comp.tool_calls:
                if not comp.content.strip():
                    if comp.finish_reason == "length":
                        s.add({"type": "notice", "level": "warn",
                               "text": "Ответ оборван по лимиту токенов."})
                    if not nudged:  # пустой ответ: один раз подталкиваем модель
                        nudged = True
                        s.messages.append({"role": "user",
                                           "content": "Продолжи: выполни задачу или дай итоговый ответ."})
                        continue
                if not st["tools_used"] and not refused and REFUSAL.search(comp.content):
                    # маленькая модель пугается большой задачи и отказывается, не открыв ни одного файла
                    refused = True
                    s.add({"type": "notice", "level": "warn",
                           "text": "Агент отказался, не изучив код, — возвращаю к работе по частям."})
                    s.messages.append({"role": "user", "content": (
                        "Не отказывайся и не проси спецификацию: задача выполнима по частям, а ответы на "
                        "вопросы — в коде. Изучи проект (symbols по модулям, read нужных сущностей, тесты), "
                        "сам определи общий интерфейс по тому, что уже есть, составь plan первой законченной "
                        "части (с именами файлов и функций) и выполни её. Остальное перечисли в итоговом ответе.")})
                    continue
                # автопроверка: не даём закончить ход со сломанным синтаксисом в изменённых файлах
                originals = _session_originals(s)  # все правки сессии, не только этого хода
                changed = originals.keys()
                errors = await asyncio.to_thread(syntax_errors, ctx.root, changed)
                if not errors:  # синтаксис цел — проверяем, что изменённые модули реально импортируются
                    errors = await asyncio.to_thread(import_errors, ctx.root, changed)
                    # и что не осталось имён без импорта/определения (точки входа тесты не импортируют)
                    errors += await asyncio.to_thread(undefined_names, ctx.root, originals)
                    errors += await asyncio.to_thread(sql_column_errors, ctx.root, originals)
                if errors and fixes < 3:
                    fixes += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: ошибки синтаксиса/импорта — агент исправляет:\n" + "\n".join(errors)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: изменённые модули не компилируются или не импортируются:\n"
                        + "\n".join(errors)
                        + "\nПеречитай эти места через read и исправь, затем дай итоговый ответ.")})
                    continue
                # новый модуль, который нигде не импортируется, — признак «заглушки» вместо работы
                created = [rel for rel, orig in originals.items() if orig is None]
                unused = await asyncio.to_thread(unused_modules, ctx.root, created)
                if unused and orphan_nudges < 2:
                    orphan_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: новые модули нигде не используются:\n" + "\n".join(unused)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: созданные модули не импортируются ни одним рабочим (не тестовым) "
                        "модулем проекта:\n" + "\n".join(unused) + "\nНовый файл сам по себе задачу не решает. "
                        "Встрой его: измени существующий код, чтобы он использовал новый модуль (добавь эти "
                        "шаги в plan и выполни по одному с проверкой), или удали файл, если он не нужен.")})
                    continue
                # импорт добавлен, а код его не использует — «интеграция для галочки»
                idle = await asyncio.to_thread(unused_new_imports, ctx.root, originals)
                if idle and import_nudges < 2:
                    import_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: добавленные импорты не используются:\n" + "\n".join(idle)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: добавленные импорты нигде в файле не используются:\n" + "\n".join(idle)
                        + "\nИмпорт сам по себе ничего не меняет. Доделай изменение кода, ради которого он "
                          "добавлен (шаги — в plan), или убери импорт. Не пиши, что сделал то, чего нет в коде.")})
                    continue
                # пропавшие зависимости: настройка/флаг/функция больше не используется — поведение пропало?
                all_dropped = await asyncio.to_thread(dropped_dependencies, ctx.root, originals)
                # настройки (UPPER_CASE) напоминаем, пока не вернутся: отключить настройку пользователя
                # модель не вправе; прочие имена — один раз, с возможностью объяснить
                settings_lost = [d for d in all_dropped if SETTING.search(d)]
                dropped = settings_lost + [d for d in all_dropped if d not in dropped_seen and d not in settings_lost]
                if dropped and dep_nudges < 3:
                    dep_nudges += 1
                    dropped_seen.update(dropped)
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: перестали использоваться:\n" + "\n".join(dropped)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: после правок перестали использоваться имена, от которых раньше "
                        "зависело поведение:\n" + "\n".join(dropped) + "\nПроверь, не пропало ли поведение "
                        "(флаг больше не учитывается, вызов выпал). Если пропало — верни его: новая логика должна "
                        "учитывать и прежнее условие. "
                        + ("Имена в ВЕРХНЕМ РЕГИСТРЕ — настройки пользователя (.env/settings): их НЕЛЬЗЯ просто "
                           "убрать, они должны продолжать действовать (например, `FLAG and новое_условие`). "
                           if settings_lost else "")
                        + "Прочие имена, если действительно больше не нужны, — убери импорт и объясни в итоге.")})
                    continue
                # пустые методы «для галочки» (return [] / pass) вместо реализации
                stubs = await asyncio.to_thread(new_stubs, ctx.root, originals)
                if stubs and stub_nudges < 2:
                    stub_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: добавлены заглушки:\n" + "\n".join(stubs)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: в правках появились функции без реальной работы:\n" + "\n".join(stubs)
                        + "\nЗаглушка не выполняет требование. Реализуй их на основе существующего кода "
                          "класса/модуля (прочитай его через read с symbol=) — например, вызови уже имеющиеся "
                          "методы, — или убери, если они не нужны. Если для реализации нужно решение "
                          "пользователя — так и напиши в итоге.")})
                    continue
                # объединение функций: показываем «было / стало» — модель склеивает похожее, не замечая различий
                # и проверяем дифференциально: старая и новая версия на реальных аргументах из тестов
                files_sig = tuple(sorted((r, (ctx.root / r).stat().st_mtime_ns if (ctx.root / r).exists() else 0)
                                         for r in originals))
                merges = await asyncio.to_thread(find_merges, ctx.root, originals)
                if merges and files_sig != merge_sig and merge_nudges < 3:
                    merge_sig = files_sig
                    results = await asyncio.to_thread(equivalence.check_merges, ctx.root, merges)
                    bad, good = equivalence.describe(merges, results) if results else (
                        [m["text"] for m in merges], [])
                    if good:
                        s.add({"type": "notice", "level": "info", "text": "Автопроверка: " + "; ".join(good)})
                    if bad:
                        merge_nudges += 1
                        s.add({"type": "notice", "level": "warn",
                               "text": "Автопроверка: объединённые функции ведут себя иначе:\n"
                                       + "\n".join(b.split("\n", 1)[0] for b in bad)})
                        s.messages.append({"role": "user", "content": (
                            "Автопроверка поведения: ты заменил функции общей реализацией. Старая и новая "
                            "версии вызваны с одними и теми же аргументами (взяты из реальных вызовов в тестах):"
                            "\n\n" + "\n\n".join(bad) + "\n\nРезультат должен совпадать ТОЧНО (текст, порядок "
                            "полей, типы, часовой пояс). Исправь общую функцию (например, параметром, который "
                            "сохраняет прежний вариант для этого места) или верни прежнюю реализацию здесь.")})
                        continue
                # тесты проекта сам прогоняет harness: модель любит отчитаться, не заметив падений
                sig = tuple(sorted((r, (ctx.root / r).stat().st_mtime_ns if (ctx.root / r).exists() else 0)
                                   for r in originals))
                if originals and test_runs < 2 and sig != tested_sig:
                    tested_sig = sig
                    res = await asyncio.to_thread(project_tests, ctx.root)
                    if res and not res[0]:
                        test_runs += 1
                        s.add({"type": "notice", "level": "warn",
                               "text": "Автопроверка: тесты проекта падают — агент исправляет."})
                        s.messages.append({"role": "user", "content": (
                            "Автопроверка: после твоих правок тесты проекта падают:\n" + res[1]
                            + "\nРазберись, чем правка изменила поведение (часто — нет данных, и новая логика "
                              "отсекает всё; сохрани прежнее поведение как запасной вариант), исправь код, а "
                              "не тесты, и прогони тесты снова.")})
                        continue
                # поведение изменено, а тесты модуля не тронуты — новое поведение ничем не закреплено
                untested = await asyncio.to_thread(untested_changes, ctx.root, originals)
                if untested and untested_nudges < 1:
                    untested_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: изменения без тестов:\n" + "\n".join(untested)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: ты изменил поведение модулей, у которых есть тесты, но ни одного теста "
                        "не добавил и не изменил:\n" + "\n".join(untested) + "\nДобавь в эти тестовые файлы "
                        "тест на каждое новое правило (случай, который раньше работал иначе, и граничный случай), "
                        "запусти test path=<модуль> и исправь, если упадёт. Затем дай итоговый ответ.")})
                    continue
                # не даём закончить, пока в плане есть незакрытые шаги
                open_steps = [x for x in s.plan if x["status"] in ("todo", "doing")]
                if st["planned"] and open_steps and plan_nudges >= 1:
                    # модель уже отчиталась, но plan не обновляет — закрываем шаги сами, без лишних ходов;
                    # реальную проверку сделанного дают автопроверки и сверка с пунктами
                    s.plan = [dict(x, status="done") if x["status"] in ("todo", "doing") else x for x in s.plan]
                    if st["plan_item"] is not None:
                        s.set(st["plan_item"], steps=s.plan)
                    s.add({"type": "notice", "level": "info", "text": "Шаги плана закрыты автоматически."})
                    open_steps = []
                if st["planned"] and open_steps and plan_nudges < 1:
                    plan_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": f"В плане незакрыто шагов: {len(open_steps)} — агент продолжает."})
                    s.messages.append({"role": "user", "content": (
                        "В плане остались незакрытые шаги:\n" + render_plan(s.plan)
                        + "\nВыполни их по одному с проверкой каждого (или отметь failed с причиной "
                          "через plan), затем проверь всё вместе и дай итоговый ответ.")})
                    continue
                # сверка итога с пунктами запроса: маленькая модель отчитывается о части как о целом
                reqs = requirements(task)
                if len(reqs) >= 2 and st["edits"] == focus_edits and not item_pushed and not reviewed:
                    # пункт закрыт без единой правки («слишком обширно», «не требует изменений, но можно
                    # было бы…») — один раз просим минимальный рабочий вариант; если модель и после этого
                    # обоснует, что правки не нужны, — принимаем
                    item_pushed = True
                    s.add({"type": "notice", "level": "warn",
                           "text": f"Пункт {s.focus + 1} брошен без правок — агент делает минимальный вариант."})
                    s.messages.append({"role": "user", "content": (
                        f"Не откладывай пункт {s.focus + 1} и не проси уточнений: сделай минимальный рабочий "
                        "вариант, который опирается на уже существующий код и не меняет поведение (например, "
                        "общий словарь/функцию поверх имеющихся классов и методов, и подключи его там, где они "
                        "сейчас вызываются). Составь plan из 3-6 шагов с конкретными файлами и выполни. "
                        "Что останется за рамками — перечисли в итоге.")})
                    continue
                if len(reqs) >= 2 and s.focus < len(reqs) - 1:
                    # пункт отработан — выдаём следующий со связанным кодом
                    s.focus += 1
                    focus_edits, item_pushed = st["edits"], False
                    s.add({"type": "notice", "level": "info",
                           "text": f"Переход к пункту {s.focus + 1} из {len(reqs)}."})
                    s.messages.append({"role": "user", "content": await asyncio.to_thread(
                        focus_message, ctx.root, reqs, s.focus)})
                    continue
                if (reviewed and not pushed and st["edits"] == reviewed_at
                        and UNDONE.search(comp.content)):
                    # сверка показала несделанное, а модель просто отчиталась — один раз отправляем доделывать
                    pushed = True
                    s.add({"type": "notice", "level": "warn",
                           "text": "По сверке есть несделанные пункты — агент продолжает."})
                    # карта «где смотреть» по каждому пункту — из индекса сущностей (имена + докстринги)
                    hints = []
                    for n, r in enumerate(reqs, 1):
                        rel = await asyncio.to_thread(entities.related, ctx.root, r, 10)
                        if rel:
                            hints.append(f"Пункт {n} — связанный код:\n" + "\n".join(rel))
                    s.messages.append({"role": "user", "content": (
                        "Ты сам отметил несделанное. Начни с первого несделанного пункта: прочитай связанный "
                        "код (read с symbol=), составь plan этой части с конкретными файлами и функциями и "
                        "выполни его по шагам с проверкой. Если пункт правда невыполним без решения "
                        "пользователя — объясни, какое решение нужно, и закончи."
                        + ("\n\n" + "\n\n".join(hints) if hints else ""))})
                    continue
                if reqs and originals and not reviewed:
                    reviewed, reviewed_at = True, st["edits"]
                    s.add({"type": "notice", "level": "info", "text": "Сверка результата с пунктами запроса."})
                    s.messages.append({"role": "user", "content": (
                        "Перед завершением сверь сделанное с КАЖДЫМ пунктом запроса:\n"
                        + "\n".join(f"{n}. {r}" for n, r in enumerate(reqs, 1))
                        + ("\nЗафиксированные решения (перечисли их и в итоговом ответе — пользователь должен "
                           "их проверить):\n" + "\n".join(
                               f"{int(k) + 1}. {v['decision']} (если данных нет: {v['fallback']})"
                               for k, v in sorted(s.decisions.items())) if s.decisions else "")
                        + "\nПо каждому пункту ОДНОЙ строкой, без таблиц: «сделано — файл: функция, что изменено» "
                          "или «не сделано — что осталось». Опирайся только на реальные правки (смотри diff через run git diff "
                          "при необходимости), не на намерения. Если что-то не сделано и выполнимо — составь "
                          "plan и доделай; иначе честно перечисли несделанное в итоговом ответе.")})
                    continue
                if s.decisions and reviewed:
                    # проектные решения — на проверку человеку, даже если модель забыла их перечислить
                    s.add({"type": "notice", "level": "warn", "text": "Проектные решения агента — проверьте:\n"
                           + "\n".join(f"Пункт {int(k) + 1}: {v['decision']}\n  опирается на: {', '.join(v['uses'])}"
                                       f"\n  если данных нет: {v['fallback']}" for k, v in sorted(s.decisions.items()))})
                return
            for n, tc in enumerate(comp.tool_calls):
                call_id = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
                msg = {"role": "assistant", "content": comp.content if n == 0 else "",
                       "tool_calls": [{"id": call_id, "type": "function",
                                       "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}]}
                if n == 0 and comp.reasoning:
                    msg["reasoning_content"] = comp.reasoning
                s.messages.append(msg)
                st["tools_used"] += 1
                result = await self._tool(s, ctx, call_id, tc["name"], tc["arguments"], st)
                out = result.output
                sig = (tc["name"], tc["arguments"], out)
                if sig == last_call:  # тот же вызов с тем же результатом — модель зациклилась
                    out = "Ты уже делал ровно этот вызов, результат тот же. Смени подход.\n" + out
                last_call = sig
                s.messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
            s.save()
        s.add({"type": "notice", "level": "warn",
               "text": f"Достигнут лимит шагов ({config.MAX_STEPS}). Напишите «продолжай», чтобы продолжить."})

    # ------------------------------------------------------------ вызов модели
    async def _wait_model(self) -> None:
        """Ожидание, пока AI Manager снова сможет принять запрос (не дольше BUSY_WAIT)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + config.BUSY_WAIT
        await asyncio.sleep(config.BUSY_POLL)
        while loop.time() < deadline:
            st = await self.llm.status()
            if st.get("ok") and (st.get("ready") or not st.get("busy")):
                return
            await asyncio.sleep(config.BUSY_POLL)

    async def _call(self, s: Session, retry_overflow: bool = True, recover: int = 0,
                    hint: str = "") -> Completion:
        est = _estimate_tokens(s)
        max_tokens = max(512, min(config.MAX_TOKENS, config.CONTEXT - est - 256))
        r_i = t_i = None
        comp = Completion()
        try:
            async for kind, val in self.llm.stream(
                    build_request(s) + ([{"role": "user", "content": hint}] if hint else []),
                    TOOLS, max_tokens=max_tokens, **config.SAMPLING,
                    reasoning_effort=s.settings.get("effort", config.REASONING),
                    chat_template_kwargs={"reasoning_effort": s.settings.get("effort", config.REASONING)}):
                if kind == "reasoning":
                    if r_i is None:
                        r_i = s.add({"type": "reasoning", "text": ""})
                    s.append(r_i, "text", val)
                elif kind == "text":
                    if t_i is None:
                        t_i = s.add({"type": "text", "text": ""})
                    s.append(t_i, "text", val)
                elif kind == "done":
                    comp = val
        except asyncio.CancelledError:
            partial = s.items[t_i]["text"] if t_i is not None else ""
            if partial.strip():
                s.messages.append({"role": "assistant", "content": partial + "\n[прервано пользователем]"})
            raise
        except LLMError as e:
            msg = str(e).lower()
            if retry_overflow and ("context" in msg and ("exceed" in msg or "size" in msg)):
                await self.compact(s)
                return await self._call(s, retry_overflow=False)
            if PARSE_ERROR.search(str(e)) and recover < config.RECOVER_TRIES:
                # llama.cpp не разобрал вызов инструмента (gpt-oss путает заголовок harmony, чаще на
                # длинном контексте). Тот же запрос даёт тот же сбой — повторяем сразу, с подсказкой
                # о формате: она меняет контекст, но в историю сессии не пишется
                s.add({"type": "notice", "level": "warn",
                       "text": "Вызов инструмента в неверном формате — повторяю шаг с подсказкой."})
                return await self._call(s, retry_overflow, recover + 1, hint=TOOL_FORMAT_HINT)
            if e.transient and recover < config.RECOVER_TRIES:
                # модель занята/переключается/упала: ждём освобождения GPU и повторяем шаг
                s.add({"type": "notice", "level": "warn",
                       "text": f"Модель недоступна ({str(e)[:200]}) — жду и повторю шаг."})
                await self._wait_model()
                return await self._call(s, retry_overflow, recover + 1)
            raise
        if r_i is not None:
            s.set(r_i, done=True)
        u, t = comp.usage, comp.timings
        if u:
            s.usage = {"ctx_used": u.get("prompt_tokens", 0) + u.get("completion_tokens", 0),
                       "msg_count": len(s.messages) + 1,  # + ответ модели, который сейчас допишется
                       "prompt": u.get("prompt_tokens", 0), "completion": u.get("completion_tokens", 0),
                       "cached": t.get("cache_n", 0), "prompt_tps": round(t.get("prompt_per_second", 0), 1),
                       "gen_tps": round(t.get("predicted_per_second", 0), 1)}
            s.push_meta()
        if not comp.tool_calls:
            s.messages.append({"role": "assistant", "content": comp.content})
        return comp

    # ------------------------------------------------------------- инструмент
    async def _tool(self, s: Session, ctx: ToolContext, call_id: str, name: str, raw: str,
                    st: dict | None = None) -> ToolResult:
        st = st if st is not None else {"planned": True, "plan_rejects": 99, "plan_item": None}
        try:
            args = json.loads(raw or "{}")
            if not isinstance(args, dict):
                raise ValueError("arguments must be an object")
        except ValueError as e:
            i = s.add({"type": "tool", "id": call_id, "name": name, "args": {"raw": raw[:500]},
                       "status": "error", "output": f"Неверный JSON аргументов: {e}"})
            return ToolResult(f"Ошибка: аргументы не являются корректным JSON ({e}). Повтори вызов.", ok=False)
        if name == "plan":
            return self._plan(s, args, st)
        if name == "decide":
            return await asyncio.to_thread(self._decide, s, ctx, args, st)
        if (name in MUTATING and len(st.get("reqs", [])) >= 2 and str(s.focus) not in s.decisions
                and st.get("decide_rejects", 0) < 2):
            # проектные пункты маленькая модель решает наугад: сначала — решение, привязанное к коду
            st["decide_rejects"] = st.get("decide_rejects", 0) + 1
            s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "error", "output": "Сначала решение по пункту (decide)."})
            return ToolResult(
                f"Не выполнено: перед правками пункта {s.focus + 1} зафиксируй решение через decide: "
                "decision — что конкретно меняешь (критерий/метрика/порог или структура и где подключается), "
                "uses — существующие функции/классы/настройки проекта, на которых оно строится (если в проекте "
                "уже есть механизм — метрики, отбор, флаги, — используй его, а не придумывай новый), fallback — "
                "что происходит, когда данных нет (обычно — прежнее поведение).", ok=False)
        if name == "run" and GIT_WRITE.search(args.get("command", "")):
            s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "error", "output": "git add/commit/push — только через git_commit."})
            return ToolResult("Не выполнено: изменять историю git через run нельзя. Для add/commit/push "
                              "используй git_commit (с описанием изменений и проведённых тестов) — "
                              "пользователь подтвердит его.", ok=False)
        if name == "run" and FILE_VIEW.match(args.get("command", "")):
            # чтение файлов через shell обходит read (номера строк, оглавление, дедупликацию)
            s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "error", "output": "Для чтения файлов — read/symbols/grep."})
            return ToolResult("Не выполнено: файлы читай инструментами, а не через run: symbols (оглавление "
                              "и поиск по сущностям), read (с symbol= или offset/limit), grep. Они доступны "
                              "и до составления плана.", ok=False)
        command = args.get("command", "")
        needs_plan = not st["planned"] and (name in MUTATING or (
            name == "run" and not SAFE_CMD.match(command) and not PRE_PLAN_CMD.match(command)))
        if needs_plan and st["plan_rejects"] >= 2:
            # модель упорно не вызывает plan (после закрытого плана прошлого хода — часто): заводим
            # минимальный план сами и пропускаем действие — от заглушек защищают автопроверки хода
            what = f"правка {args.get('path', '')}".strip() if name in MUTATING else f"команда: {command[:80]}"
            self._plan(s, {"steps": [{"text": what, "status": "doing"},
                                     {"text": "проверить изменения (тесты)", "status": "todo"}]}, st)
            s.add({"type": "notice", "level": "warn", "text": "Агент не составил план — заведён автоматически."})
            needs_plan = False
        if needs_plan:
            # правило работы маленькой модели: сначала план из мелких шагов, потом изменения
            st["plan_rejects"] += 1
            prev = f"\nПредыдущий план:\n{render_plan(s.plan)}" if s.plan else ""
            s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "error", "output": "Сначала нужен план (plan)."})
            return ToolResult("Не выполнено: сначала составь план через plan — мелкие проверяемые шаги "
                              "(одно изменение в одном месте на шаг, с именами файлов/функций), в конце — общая "
                              "проверка. До плана можно и нужно изучать код: symbols, read, grep, glob, запуск "
                              "тестов. Если код ещё не изучен — первые шаги плана сделай исследовательскими. "
                              "Пример: plan({\"steps\": [{\"text\": \"изучить X через symbols\", \"status\": "
                              "\"doing\"}, {\"text\": \"…\", \"status\": \"todo\"}, {\"text\": \"прогнать все "
                              "тесты\", \"status\": \"todo\"}]})" + prev, ok=False)
        i = s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "running"})
        if _approval_needed(s, name, args):
            if name in MUTATING:
                # сначала проверяем правку всухую: битую не показываем пользователю, а сразу
                # возвращаем модели ошибку; рабочую — показываем настоящим диффом
                pre = await asyncio.to_thread(preview, ctx, name, args)
                if not pre.ok:
                    s.set(i, status="error", output=pre.output)
                    return pre
                s.set(i, diff=pre.diff)
            if name == "git_commit":
                # карточка: сообщение, описание изменений, тесты, файлы, куда push
                try:
                    s.set(i, git=await asyncio.to_thread(git_preview, ctx, **args))
                except (ToolError, TypeError) as e:
                    s.set(i, status="error", output=str(e))
                    return ToolResult(f"Ошибка: {e}", ok=False)
            fut = asyncio.get_running_loop().create_future()
            s.pending[call_id] = fut
            s.set(i, status="approve")
            try:
                decision, note = await fut
            finally:
                s.pending.pop(call_id, None)
            if decision == "deny":
                s.set(i, status="denied", output=note or "")
                return ToolResult("Пользователь отклонил это действие." +
                                  (f" Комментарий: {note}" if note else " Спроси, как поступить, или выбери другой путь."),
                                  ok=False)
            if decision == "always" and name != "git_commit":  # git — только разовое разрешение
                s.settings.setdefault("always", []).append(name)
            s.set(i, status="running")
        result = await execute(ctx, name, args)
        if result.ok and (name == "logstats"
                          or name == "run" and EVIDENCE_CMD.search(args.get("command", ""))
                          or name in ("read", "grep") and EVIDENCE_PATH.search(str(args.get("path", "")))):
            st["evidence"] = st.get("evidence", 0) + 1
            if name != "read" and name != "grep" or re.search(r"(^|/)logs?/|\.log$", str(args.get("path", ""))):
                st["log_evidence"] = st.get("log_evidence", 0) + 1
        if name in MUTATING and result.ok:
            st["edits"] = st.get("edits", 0) + 1
        if st["planned"] and (name in MUTATING or name == "run"):
            st["since_plan"] = st.get("since_plan", 0) + 1
            if st["since_plan"] >= 2:  # модель увлеклась и забыла про план — напоминаем
                result.output += ("\n[Напоминание: обнови plan — отметь выполненный шаг done и "
                                  "следующий doing; делай по одному шагу с проверкой.]")
        out = result.output
        ui_out = out if len(out) <= UI_OUTPUT else out[:UI_OUTPUT] + "\n…"
        s.set(i, status="ok" if result.ok else "error", output=ui_out, diff=result.diff, path=result.path)
        return result

    def _decide(self, s: Session, ctx: ToolContext, args: dict, st: dict) -> ToolResult:
        """Решение по текущему пункту: конкретное правило, на чём строится, что без данных."""
        decision = str(args.get("decision", "")).strip()
        uses = [str(x) for x in (args.get("uses") or []) if str(x).strip()]
        fallback = str(args.get("fallback", "")).strip()
        if len(decision) < 20 or not uses or not fallback:
            return ToolResult("Ошибка: нужны decision (конкретное правило, не общие слова), uses (непустой "
                              "список существующих сущностей) и fallback.", ok=False)
        reqs = st.get("reqs") or []
        item = reqs[s.focus] if 0 <= s.focus < len(reqs) else ""
        cause = str(args.get("cause", "")).strip()
        if INVESTIGATE.search(item) and st.get("cause_rejects", 0) < 3:
            why = ""
            main = main_checkout(ctx.root)
            has_logs = any((r / d).is_dir() for r in (ctx.root, main) if r for d in ("logs", "log"))
            if has_logs and not st.get("log_evidence"):
                why = ("в проекте есть логи (logs/), а ты их не смотрел: logstats path=logs (match=ERROR|WARNING, "
                       "by=message; для JSON-логов by=status|ip, bucket=hour), а логи сервера — командами из "
                       "документов о развёртывании")
            elif not st.get("evidence"):
                why = ("ты ещё не смотрел факты: логи (logstats), конфиги, документы о развёртывании (где логи "
                       "сервера и как их прочитать)")
            elif len(cause) < 30 or not re.search(r"\d", cause):
                why = ("cause должна называть причину с фактами: время сбоев, числа из логов, файл:строка "
                       "кода, который к этому приводит")
            if why:
                st["cause_rejects"] = st.get("cause_rejects", 0) + 1
                return ToolResult(f"Ошибка: пункт {s.focus + 1} — расследование, решение без найденной причины "
                                  f"не принимается: {why}. Затем снова decide, ОБЯЗАТЕЛЬНО с полем cause: "
                                  '{"decision": "...", "uses": [...], "fallback": "...", "cause": "<что видно в логах: '
                                  'время, числа, адреса> -> <какой код (файл:строка) к этому приводит>"}', ok=False)
        missing = entities.check_names(ctx.root, uses)
        if missing:
            lines = [f"- {n}: нет в проекте" + (f" (похожие: {', '.join(sim)})" if sim else "")
                     for n, sim in missing.items()]
            return ToolResult("Ошибка: решение опирается на несуществующие сущности:\n" + "\n".join(lines)
                              + "\nНайди реальные через symbols/grep и пришли decide заново.", ok=False)
        if DISABLING.search(fallback) and not st.get("fallback_warned"):
            # «нет данных — ничего не делаем» молча выключает работающую функцию (сигналы, отчёты)
            st["fallback_warned"] = True
            return ToolResult(
                f"Внимание: fallback «{fallback}» выключает работающую функциональность, когда данных нет "
                "(например, пустая таблица на свежей установке или в тестах). Обычно нужно сохранить прежнее "
                "поведение. Пришли decide снова: либо с fallback «прежнее поведение», либо с этим же fallback, "
                "если такое изменение прямо требуется задачей.", ok=False)
        s.decisions[str(s.focus)] = {"decision": decision, "uses": uses, "fallback": fallback, "cause": cause}
        s.add({"type": "notice", "level": "info",
               "text": (f"Причина: {cause}\n" if cause else "")
                       + f"Решение по пункту {s.focus + 1}: {decision}\nОпирается на: {', '.join(uses)}\n"
                       f"Если данных нет: {fallback}"})
        return ToolResult(f"Решение по пункту {s.focus + 1} принято. Теперь plan и правки по нему.")

    def _plan(self, s: Session, args: dict, st: dict) -> ToolResult:
        raw = args.get("steps")
        if isinstance(raw, list) and len(raw) == 1 and s.plan:
            # модель часто шлёт только обновлённый шаг, а не весь список: обновляем его статус в плане
            one = raw[0] if isinstance(raw[0], dict) else {"text": str(raw[0])}
            key = str(one.get("text", "")).strip().casefold()
            hit = next((x for x in s.plan if x["text"].casefold() == key), None)
            if hit:
                raw = [dict(x, status=one.get("status", x["status"])) if x is hit else x for x in s.plan]
        try:
            steps = normalize_plan(raw)
        except ToolError as e:
            s.add({"type": "notice", "level": "warn", "text": f"План отклонён: {e}"})
            return ToolResult(f"Ошибка: {e}", ok=False)
        s.plan = steps
        st["planned"] = True
        st["since_plan"] = 0
        if st["plan_item"] is None:
            st["plan_item"] = s.add({"type": "plan", "steps": steps})
        else:  # тот же чек-лист обновляется на месте, а не дублируется
            s.set(st["plan_item"], steps=steps)
        cur = next((n for n, x in enumerate(steps, 1) if x["status"] in ("doing", "todo")), None)
        hint = (f"Сейчас шаг {cur}: сделай только его, проверь результат, затем отметь в plan."
                if cur else "Все шаги закрыты: проверь всё вместе (тесты/запуск) и дай итоговый ответ.")
        return ToolResult(f"План:\n{render_plan(steps)}\n{hint}")

    # ----------------------------------------------------------------- сжатие
    async def compact(self, s: Session, manual: bool = False) -> None:
        if len(s.messages) < 2:
            if manual:
                s.add({"type": "notice", "level": "info", "text": "Нечего сжимать."})
            return
        i = s.add({"type": "notice", "level": "info", "text": "Сжатие контекста…"})
        last_user = next((m["content"] for m in reversed(s.messages) if m["role"] == "user"), "")
        # те же system+tools → префикс берётся из кэша, считается только сама просьба
        req = build_request(s) + [{"role": "user", "content": prompts.COMPACT}]
        summary = ""
        try:
            comp = await self.llm.complete(req, tools=TOOLS, max_tokens=2500, reasoning_effort="low",
                                           chat_template_kwargs={"reasoning_effort": "low"})
            summary = comp.content.strip()
        except LLMError:
            pass
        if not summary:  # запасной вариант без модели: оставляем только последние сообщения
            summary = "(автосводка недоступна) Последний запрос пользователя:\n" + last_user[:4000]
        resume = f"[Сводка предыдущей части сессии]\n{summary}"
        if s.plan:  # план переживает сжатие дословно — модель продолжит с нужного шага
            resume += f"\n\n[Текущий план]\n{render_plan(s.plan)}"
        if not manual:
            resume += f"\n\n[Текущий запрос пользователя]\n{last_user[:6000]}\n\nПродолжай работу."
        s.messages = [{"role": "user", "content": resume}]
        if manual:
            s.messages.append({"role": "assistant", "content": "Контекст сжат. Готов продолжать."})
        s.seen.clear()
        s.rewind_floor = s.turn
        s.usage = {}
        s.set(i, text="Контекст сжат.", summary=summary)
        s.push_meta()
        s.save()


# ------------------------------------------------------------------ откат
def rewind(s: Session, turn: int) -> list[str]:
    """Откат к состоянию ДО хода turn: файлы восстанавливаются, история обрезается."""
    if s.running:
        raise RuntimeError("Сессия выполняется — сначала остановите.")
    if turn <= s.rewind_floor:
        raise RuntimeError("Этот ход был до сжатия контекста — откат недоступен.")
    root = session_root(s)
    restored = []
    for cp in sorted((c for c in s.checkpoints if c["turn"] >= turn), key=lambda c: -c["turn"]):
        for rel, text in cp["files"].items():
            p = resolve(root, rel)
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(text.encode("utf-8"))
            restored.append(rel)
    s.checkpoints = [c for c in s.checkpoints if c["turn"] < turn]
    idx = next((n for n, it in enumerate(s.items) if it.get("type") == "user" and it.get("turn") == turn), None)
    if idx is not None:
        s.messages = s.messages[: s.items[idx]["msg_index"]]
        s.items = s.items[:idx]
    s.turn = turn - 1
    s.usage = {}
    s.seen.clear()
    s.save()
    s.reset_items()
    return sorted(set(restored))


def session_changes(s: Session) -> list[dict]:
    """Итоговые диффы всех файлов, изменённых агентом в сессии (оригинал → сейчас)."""
    import difflib
    root = session_root(s)
    originals = _session_originals(s)
    out = []
    for rel, before in sorted(originals.items()):
        p = root / rel
        after = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
        a = (before or "").replace("\r\n", "\n")
        b = (after or "").replace("\r\n", "\n")
        if a == b and (before is None) == (after is None):
            continue
        d = "".join(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
        status = "added" if before is None else ("deleted" if after is None else "modified")
        out.append({"path": rel, "status": status, "diff": d})
    return out
