import asyncio
from pathlib import Path

from conftest import MockModel, text_chunks, tool_call_chunks

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
