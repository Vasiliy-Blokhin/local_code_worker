import asyncio
from pathlib import Path

from conftest import MockModel, plan_chunks, text_chunks, tool_call_chunks

from lcw import prompts
from lcw.agent import Agent
from lcw.llm import LLM
from lcw.sessions import store


def make(project, script, mode="auto"):
    model = MockModel(script)
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    s = store.create(project["id"], prompts.build_system(project["path"], ""), {"mode": mode})
    return model, Agent(llm), s


def tool_results(s):
    return [m["content"] for m in s.messages if m["role"] == "tool"]


def test_edit_without_plan_is_rejected_once(project):
    model, agent, s = make(project, [
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        plan_chunks("заменить строку", "проверить", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c2"),
        text_chunks("готово"),
    ])
    asyncio.run(agent.run_turn(s, "замени"))
    res = tool_results(s)
    assert "сначала составь план" in res[0]  # отклоняется, пока нет корректного плана
    assert "План:" in res[1] and res[2].startswith("OK")
    assert "'hey'" in (Path(project["path"]) / "app.py").read_text()


def test_plan_item_updated_in_place_and_progress_hint(project):
    model, agent, s = make(project, [
        plan_chunks("шаг А", "шаг Б"),
        tool_call_chunks("plan", {"steps": [{"text": "шаг А", "status": "done"},
                                            {"text": "шаг Б", "status": "doing"}]}, "p2"),
        tool_call_chunks("plan", {"steps": [{"text": "шаг А", "status": "done"},
                                            {"text": "шаг Б", "status": "done"}]}, "p3"),
        text_chunks("итог"),
    ])
    asyncio.run(agent.run_turn(s, "задача"))
    plans = [it for it in s.items if it["type"] == "plan"]
    assert len(plans) == 1 and [x["status"] for x in plans[0]["steps"]] == ["done", "done"]
    res = tool_results(s)
    assert "Сейчас шаг 1" in res[0] and "Сейчас шаг 2" in res[1] and "Все шаги закрыты" in res[2]
    assert s.plan[1]["status"] == "done"


def test_unfinished_plan_blocks_final_answer(project):
    model, agent, s = make(project, [
        plan_chunks("шаг А", "шаг Б"),
        text_chunks("всё сделал"),                  # врёт: шаги не закрыты
        tool_call_chunks("plan", {"steps": [{"text": "шаг А", "status": "done"},
                                            {"text": "шаг Б", "status": "failed"}]}, "p2"),
        text_chunks("итог"),
    ])
    asyncio.run(agent.run_turn(s, "задача"))
    assert any("незакрытые шаги" in m["content"] for m in s.messages if m["role"] == "user")
    assert s.messages[-1]["content"] == "итог"


def test_invalid_plan_rejected(project):
    model, agent, s = make(project, [
        tool_call_chunks("plan", {"steps": [{"text": "a", "status": "doing"},
                                            {"text": "b", "status": "doing"}]}, "p1"),
        text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    assert "только один шаг" in tool_results(s)[0] and s.plan == []


def test_plan_survives_compaction(project, monkeypatch):
    from lcw import config
    monkeypatch.setattr(config, "CONTEXT", 2000)
    model, agent, s = make(project, [text_chunks("СВОДКА"), text_chunks("ok")])
    s.plan = [{"text": "шаг А", "status": "done"}, {"text": "шаг Б", "status": "todo"}]
    s.messages = [{"role": "user", "content": "старое"}, {"role": "assistant", "content": "ответ"}]
    s.usage = {"ctx_used": 1900, "msg_count": 2}
    asyncio.run(agent.run_turn(s, "продолжай"))
    assert "[Текущий план]" in s.messages[0]["content"] and "[ ] 2. шаг Б" in s.messages[0]["content"]


def test_single_step_plan_rejected(project):
    model, agent, s = make(project, [
        tool_call_chunks("plan", {"steps": [{"text": "сделать всё", "status": "doing"}]}, "p1"),
        text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    assert "Слишком крупный план" in tool_results(s)[0]


def test_reminder_after_two_changes_without_plan_update(project):
    model, agent, s = make(project, [
        plan_chunks("правка 1", "правка 2", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'a'"}, "c1"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'a'", "new": "'b'"}, "c2"),
        text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    res = tool_results(s)
    assert "Напоминание" not in res[1] and "Напоминание: обнови plan" in res[2]


def test_gate_persists_until_valid_plan_then_gives_up(project):
    edit = lambda cid: tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, cid)
    model, agent, s = make(project, [edit("c1"), edit("c2"), edit("c3"), edit("c4"), text_chunks("ok")])
    asyncio.run(agent.run_turn(s, "x"))
    res = tool_results(s)
    assert all("сначала составь план" in r for r in res[:3]) and "OK:" in res[3]
