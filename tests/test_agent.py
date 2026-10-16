import asyncio
from pathlib import Path

from conftest import MockModel, plan_chunks, text_chunks, tool_call_chunks

from lcw import config, prompts
from lcw.agent import Agent, build_request, rewind, session_changes
from lcw.llm import LLM
from lcw.sessions import store


def make(project, script, mode="auto"):
    model = MockModel(script)
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    s = store.create(project["id"], prompts.build_system(project["path"], "app.py"), {"mode": mode})
    return model, Agent(llm), s


def test_full_turn_edit_and_rewind(project):
    model, agent, s = make(project, [
        plan_chunks(status="done"),
        tool_call_chunks("grep", {"pattern": "hello"}, "c1"),
        tool_call_chunks("edit", {"path": "app.py", "old": "return 'hi'", "new": "return 'hello'"}, "c2"),
        text_chunks("Готово: изменил app.py"),
    ])
    asyncio.run(agent.run_turn(s, "поменяй приветствие"))

    root = Path(project["path"])
    assert "return 'hello'" in (root / "app.py").read_text()
    roles = [m["role"] for m in s.messages]
    assert roles == ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant"]
    assert s.messages[3]["tool_calls"][0]["function"]["name"] == "grep"
    assert s.messages[-1]["content"] == "Готово: изменил app.py"
    types = [it["type"] for it in s.items]
    assert types.count("tool") == 2 and types[-1] == "text"
    assert all(it["status"] == "ok" for it in s.items if it["type"] == "tool")
    assert s.usage["prompt"] == 1200 and not s.running

    # третий запрос: system + tools неизменны (кэш префикса), reasoning текущего хода передаётся
    req = model.requests[3]
    assert req["messages"][0]["role"] == "system" and req["tools"] == model.requests[0]["tools"]
    assert req["messages"][2]["reasoning_content"] == "план"
    assert req["model"] == config.MODEL and req["stream"] is True

    ch = session_changes(s)
    assert ch[0]["path"] == "app.py" and "+    return 'hello'" in ch[0]["diff"]

    restored = rewind(s, 1)
    assert restored == ["app.py"]
    assert "return 'hi'" in (root / "app.py").read_text()
    assert s.messages == [] and s.items == []


def test_reasoning_dropped_for_previous_turns(project):
    model, agent, s = make(project, [
        tool_call_chunks("read", {"path": "app.py"}, "c1"), text_chunks("ok"),
        text_chunks("второй ответ"),
    ])
    asyncio.run(agent.run_turn(s, "первый"))
    asyncio.run(agent.run_turn(s, "второй"))
    req = model.requests[-1]
    assert not any("reasoning_content" in m for m in req["messages"])
    assert req["messages"][-1] == {"role": "user", "content": "второй"}


def test_approval_deny(project):
    model, agent, s = make(project, [
        plan_chunks(status="done"),
        tool_call_chunks("run", {"command": "rm -rf /"}, "c1"),
        text_chunks("ладно"),
    ], mode="edits")

    async def scenario():
        task = asyncio.create_task(agent.run_turn(s, "удали всё"))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if "c1" in s.pending:
                break
        assert s.items[-1]["status"] == "approve"
        s.pending["c1"].set_result(("deny", "нельзя"))
        await task

    asyncio.run(scenario())
    tool_msg = next(m for m in s.messages if m["role"] == "tool" and not m["content"].startswith("План:"))
    assert "отклонил" in tool_msg["content"] and "нельзя" in tool_msg["content"]
    assert any(it.get("status") == "denied" for it in s.items)


def test_safe_command_auto_allowed_in_edits_mode(project):
    model, agent, s = make(project, [tool_call_chunks("run", {"command": "git status"}, "c1"),
                                     text_chunks("ok")], mode="edits")
    asyncio.run(asyncio.wait_for(agent.run_turn(s, "статус"), 30))
    assert not s.pending and s.messages[-1]["content"] == "ok"


def test_invalid_json_args_reported(project):
    bad = tool_call_chunks("read", {"path": "app.py"}, "c1")
    bad[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = "{{{"
    model, agent, s = make(project, [bad, text_chunks("ok")])
    asyncio.run(agent.run_turn(s, "x"))
    tool_msg = next(m for m in s.messages if m["role"] == "tool" and not m["content"].startswith("План:"))
    assert "JSON" in tool_msg["content"]


def test_auto_compaction(project, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT", 2000)
    model, agent, s = make(project, [
        text_chunks("СВОДКА: делали X"),                       # ответ на запрос сжатия
        tool_call_chunks("read", {"path": "app.py"}, "c1"),
        text_chunks("готово"),
    ])
    s.messages = [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "старый ответ"}]
    s.usage = {"ctx_used": 1900, "msg_count": 2}
    asyncio.run(agent.run_turn(s, "задача"))
    assert s.messages[0]["content"].startswith("[Сводка предыдущей части сессии]")
    assert "задача" in s.messages[0]["content"]
    assert any(it.get("summary") for it in s.items)


def test_mentions_attach_file(project):
    model, agent, s = make(project, [text_chunks("ok")])
    asyncio.run(agent.run_turn(s, "посмотри @app.py"))
    assert '<file path="app.py">' in model.requests[0]["messages"][-1]["content"]
    assert build_request(s)[0]["role"] == "system"


def test_cancel_marks_stopped(project):
    model, agent, s = make(project, [plan_chunks(status="done"),
                                     tool_call_chunks("run", {"command": "sleep 30"}, "c1")])

    async def scenario():
        task = asyncio.create_task(agent.run_turn(s, "долго"))
        for _ in range(300):
            await asyncio.sleep(0.02)
            if s.items and s.items[-1].get("status") == "running":
                break
        task.cancel()
        await task

    asyncio.run(asyncio.wait_for(scenario(), 20))
    assert any(it.get("text", "").startswith("Остановлено") for it in s.items) and not s.running
    # история закрыта: у вызова инструмента есть результат, карточка не висит «выполняется»
    assert s.messages[-1]["role"] == "tool" and s.messages[-1]["content"] == "Прервано пользователем."
    assert not any(it.get("status") == "running" for it in s.items)
