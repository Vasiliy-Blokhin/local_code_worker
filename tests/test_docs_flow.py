"""Проект-документы: задачи по одной, независимые решения, проверка, запись в result.docx по частям."""
import asyncio

from conftest import text_chunks, tool_call_chunks
from test_plan import make
from lcw import documents, workspace

TASK = "Задача\n1. Используя материалы решить задачи из task.txt\n2. ответ сохранить в result.docx\n"


def test_problems_one_by_one_with_independent_check(tmp_path):
    (tmp_path / "task.txt").write_text("Задача 1.\nНайдите A ∩ A.\nЗадача 2.\nНайдите A ∪ ∅.\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("Пересечение A ∩ A равно A.\n", encoding="utf-8")
    project = workspace.add_project(str(tmp_path))
    model, agent, s = make(project, [
        tool_call_chunks("write", {"path": "result.docx", "content": "# всё сразу"}, "w0"),  # рано: отклоняется
        text_chunks("Задача 1: A ∩ A = A"),
        text_chunks("независимо: A"), text_chunks("независимо: A"),
        tool_call_chunks("write", {"path": "draft.txt", "content": "черновик"}, "w1"),  # посторонний файл
        tool_call_chunks("edit", {"path": "result.docx", "old": "", "new": "## Задача 1\nA ∩ A = A"}, "e1"),
        text_chunks("Задача 1 проверена"),
        text_chunks("Задача 2: A"),
        text_chunks("независимо: A"), text_chunks("независимо: A"),
        tool_call_chunks("edit", {"path": "result.docx", "old": "", "new": "## Задача 2\nA ∪ ∅ = A"}, "e2"),
        tool_call_chunks("edit", {"path": "result.docx", "old": "", "new": "## Задача 2\nA ∪ ∅ = A (проверено)"}, "e3"),
        text_chunks("Задача 2 проверена"),
        text_chunks("Сохранено"),
    ])
    asyncio.run(agent.run_turn(s, TASK))
    first = s.messages[0]["content"]
    assert "Сейчас пункт 1 из 3: Задача 1 из task.txt" in first and "notes.md" in first
    users = [m["content"] for m in s.messages if m["role"] == "user"]
    assert sum("Независимые решения" in u for u in users) == 2
    tools = [m["content"] for m in s.messages if m["role"] == "tool"]
    assert "сначала закончи решение задачи 1" in tools[0] and "только файл результата" in tools[1]
    assert not (tmp_path / "draft.txt").exists()
    md = documents.docx_source(tmp_path / "result.docx")
    assert md.count("## Задача 2") == 1 and "(проверено)" in md and md.index("Задача 1") < md.index("Задача 2")
    # независимые решения — отдельный короткий контекст без истории сессии и без инструментов
    side = [r for r in model.requests if r["messages"][0]["content"].startswith("Ты решаешь задачу")]
    assert len(side) == 4 and all(len(r["messages"]) == 2 and not r.get("tools") for r in side)
