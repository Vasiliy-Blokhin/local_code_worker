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

from . import config, prompts
from .llm import LLM, Completion, LLMError
from .sessions import Session
from .tools import (MUTATING, TOOLS, ToolContext, ToolError, ToolResult, execute, normalize_plan,
                    preview, render_plan, syntax_errors)
from .workspace import WorkspaceError, get_project, is_text_file, relpath, resolve

UI_OUTPUT = 6000           # сколько вывода инструмента показывать в UI
CHARS_PER_TOKEN = 3.2      # грубая оценка для бюджета контекста
SAFE_CMD = re.compile(r"^\s*(git (status|diff|log|show|branch|remote -v)|ls|dir|pwd|cat|head|tail|wc|"
                      r"tree|echo|python -V|python --version|node -v|which|where)\b[^;&|<>`$]*$")


def _approval_needed(s: Session, name: str, args: dict) -> bool:
    mode = s.settings.get("mode", "edits")
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
            proj = get_project(s.project_id)
            root = Path(proj["path"])
            if text.strip() == "/compact":
                await self.compact(s, manual=True)
                return
            if text.strip() == "/init":
                text = prompts.INIT
            s.turn += 1
            checkpoint = {"turn": s.turn, "files": {}}
            s.checkpoints.append(checkpoint)
            s.add({"type": "user", "text": text, "turn": s.turn, "msg_index": len(s.messages)})
            s.messages.append({"role": "user", "content": expand_mentions(root, text, s)})
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
        # состояние хода: составлен ли план, было ли уже напоминание, элемент плана в UI
        st = {"planned": False, "plan_rejects": 0, "plan_item": None, "since_plan": 0}
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
                # автопроверка: не даём закончить ход со сломанным синтаксисом в изменённых файлах
                changed = s.checkpoints[-1]["files"].keys() if s.checkpoints else ()
                errors = await asyncio.to_thread(syntax_errors, ctx.root, changed)
                if errors and fixes < 3:
                    fixes += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": "Автопроверка: синтаксические ошибки — агент исправляет:\n" + "\n".join(errors)})
                    s.messages.append({"role": "user", "content": (
                        "Автопроверка: в изменённых файлах синтаксические ошибки:\n" + "\n".join(errors)
                        + "\nПеречитай эти места через read и исправь, затем дай итоговый ответ.")})
                    continue
                # не даём закончить, пока в плане есть незакрытые шаги
                open_steps = [x for x in s.plan if x["status"] in ("todo", "doing")]
                if st["planned"] and open_steps and plan_nudges < 2:
                    plan_nudges += 1
                    s.add({"type": "notice", "level": "warn",
                           "text": f"В плане незакрыто шагов: {len(open_steps)} — агент продолжает."})
                    s.messages.append({"role": "user", "content": (
                        "В плане остались незакрытые шаги:\n" + render_plan(s.plan)
                        + "\nВыполни их по одному с проверкой каждого (или отметь failed с причиной "
                          "через plan), затем проверь всё вместе и дай итоговый ответ.")})
                    continue
                return
            for n, tc in enumerate(comp.tool_calls):
                call_id = tc["id"] or f"call_{uuid.uuid4().hex[:8]}"
                msg = {"role": "assistant", "content": comp.content if n == 0 else "",
                       "tool_calls": [{"id": call_id, "type": "function",
                                       "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}]}
                if n == 0 and comp.reasoning:
                    msg["reasoning_content"] = comp.reasoning
                s.messages.append(msg)
                sig = (tc["name"], tc["arguments"])
                result = await self._tool(s, ctx, call_id, tc["name"], tc["arguments"], st)
                out = result.output
                if sig == last_call:
                    out = "Ты уже делал ровно этот вызов, результат тот же. Смени подход.\n" + out
                last_call = sig
                s.messages.append({"role": "tool", "tool_call_id": call_id, "content": out})
            s.save()
        s.add({"type": "notice", "level": "warn",
               "text": f"Достигнут лимит шагов ({config.MAX_STEPS}). Напишите «продолжай», чтобы продолжить."})

    # ------------------------------------------------------------ вызов модели
    async def _call(self, s: Session, retry_overflow: bool = True) -> Completion:
        est = _estimate_tokens(s)
        max_tokens = max(512, min(config.MAX_TOKENS, config.CONTEXT - est - 256))
        r_i = t_i = None
        comp = Completion()
        try:
            async for kind, val in self.llm.stream(
                    build_request(s), TOOLS, max_tokens=max_tokens,
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
        if not st["planned"] and st["plan_rejects"] < 3 and (
                name in MUTATING or (name == "run" and not SAFE_CMD.match(args.get("command", "")))):
            # правило работы маленькой модели: сначала план из мелких шагов, потом изменения
            st["plan_rejects"] += 1
            prev = f"\nПредыдущий план:\n{render_plan(s.plan)}" if s.plan else ""
            s.add({"type": "tool", "id": call_id, "name": name, "args": _ui_args(name, args),
                   "status": "error", "output": "Сначала нужен план (plan)."})
            return ToolResult("Не выполнено: сначала составь план через plan — мелкие проверяемые шаги "
                              "(одно изменение в одном месте на шаг), в конце — общая проверка. "
                              "Затем выполняй по одному шагу." + prev, ok=False)
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
            if decision == "always":
                s.settings.setdefault("always", []).append(name)
            s.set(i, status="running")
        result = await execute(ctx, name, args)
        if st["planned"] and (name in MUTATING or name == "run"):
            st["since_plan"] = st.get("since_plan", 0) + 1
            if st["since_plan"] >= 2:  # модель увлеклась и забыла про план — напоминаем
                result.output += ("\n[Напоминание: обнови plan — отметь выполненный шаг done и "
                                  "следующий doing; делай по одному шагу с проверкой.]")
        out = result.output
        ui_out = out if len(out) <= UI_OUTPUT else out[:UI_OUTPUT] + "\n…"
        s.set(i, status="ok" if result.ok else "error", output=ui_out, diff=result.diff, path=result.path)
        return result

    def _plan(self, s: Session, args: dict, st: dict) -> ToolResult:
        try:
            steps = normalize_plan(args.get("steps"))
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
    proj = get_project(s.project_id)
    root = Path(proj["path"])
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
    proj = get_project(s.project_id)
    root = Path(proj["path"])
    originals: dict[str, str | None] = {}
    for cp in sorted(s.checkpoints, key=lambda c: c["turn"]):
        for rel, text in cp["files"].items():
            originals.setdefault(rel, text)
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
