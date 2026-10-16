import asyncio
from pathlib import Path

from conftest import MockModel, plan_chunks, text_chunks, tool_call_chunks

from lcw import prompts
from lcw.agent import Agent
from lcw.llm import LLM
from lcw.sessions import store
from lcw.tools import ToolContext, execute


def test_edit_empty_old_appends(project):
    ctx = ToolContext(root=Path(project["path"]))
    r = asyncio.run(execute(ctx, "edit", {"path": "app.py", "old": "", "new": "X = 2"}))
    assert r.ok, r.output
    assert (Path(project["path"]) / "app.py").read_text().endswith("    return 'hi'\nX = 2\n")
    assert "3│X = 2" in r.output


def test_final_answer_blocked_while_syntax_broken(project):
    model = MockModel([
        plan_chunks(status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "return 'hi'", "new": "return ('hi'"}, "c1"),
        text_chunks("Готово"),                       # модель «закончила» со сломанным файлом
        tool_call_chunks("edit", {"path": "app.py", "old": "return ('hi'", "new": "return 'hey'"}, "c2"),
        text_chunks("Исправил"),
    ])
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    s = store.create(project["id"], prompts.build_system(project["path"], ""), {"mode": "auto"})
    asyncio.run(Agent(llm).run_turn(s, "сделай"))
    assert any("Автопроверка" in m["content"] for m in s.messages if m["role"] == "user")
    assert s.messages[-1]["content"] == "Исправил"
    assert "return 'hey'" in (Path(project["path"]) / "app.py").read_text()


def test_patch_style_old_is_unpacked(project):
    """gpt-oss кладёт патч (-/+ и *** End Edit ***) в old, оставляя new пустым."""
    ctx = ToolContext(root=Path(project["path"]))
    patch = "def hello():\n-    return 'hi'\n+    return 'hey'\n+\n+X = 1\n*** End Edit ***\n"
    r = asyncio.run(execute(ctx, "edit", {"path": "app.py", "old": patch, "new": ""}))
    assert r.ok, r.output
    assert (Path(project["path"]) / "app.py").read_text() == "def hello():\n    return 'hey'\n\nX = 1\n"


def test_broken_edit_not_shown_for_approval(project):
    model = MockModel([
        plan_chunks(status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "нет такого", "new": "x"}, "c1"),
        text_chunks("ок"),
    ])
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    s = store.create(project["id"], prompts.build_system(project["path"], ""), {"mode": "ask"})
    asyncio.run(asyncio.wait_for(Agent(llm).run_turn(s, "правь"), 20))
    tool = next(it for it in s.items if it["type"] == "tool")
    assert tool["status"] == "error" and not s.pending
    assert "не найден" in next(m for m in s.messages if m["role"] == "tool" and not m["content"].startswith("План:"))["content"]


def test_read_line_numbers_stripped_from_old(project):
    ctx = ToolContext(root=Path(project["path"]))
    r = asyncio.run(execute(ctx, "edit", {"path": "app.py", "old": "1│def hello():\n2│    return 'hi'",
                                          "new": "def hello():\n    return 'hey'"}))
    assert r.ok, r.output
    assert (Path(project["path"]) / "app.py").read_text() == "def hello():\n    return 'hey'\n"
