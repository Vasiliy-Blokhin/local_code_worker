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
    assert "В плане один шаг" in tool_results(s)[0]


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


def test_gate_rejects_twice_then_creates_plan_itself(project):
    """Без плана — два отказа с примером вызова; дальше harness заводит план сам и пропускает правку."""
    edit = lambda cid: tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, cid)
    model, agent, s = make(project, [edit("c1"), edit("c2"), edit("c3"), text_chunks("ok"), text_chunks("ok")])
    asyncio.run(agent.run_turn(s, "x"))
    res = tool_results(s)
    assert all("сначала составь план" in r for r in res[:2]) and "plan({" in res[0]
    assert res[2].startswith("OK:") and "'hey'" in (Path(project["path"]) / "app.py").read_text()
    assert s.plan[0]["text"] == "правка app.py"


def test_tests_allowed_before_plan_and_file_view_redirected(project):
    model, agent, s = make(project, [
        tool_call_chunks("run", {"command": "python -m py_compile app.py"}, "c1"),
        tool_call_chunks("run", {"command": "sed -n '1,200p' app.py"}, "c2"),
        text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    res = tool_results(s)
    assert res[0].startswith("[exit 0]")
    assert "read" in res[1] and "через run" in res[1]
    assert not any("сначала составь план" in r for r in res)


def test_unused_new_module_nudged_until_integrated(project):
    model, agent, s = make(project, [
        plan_chunks("создать реестр", "подключить в app.py", "проверка", status="done"),
        tool_call_chunks("write", {"path": "pkg/registry.py", "content": "REG = {}\n"}, "c1"),
        text_chunks("готово"),  # заглушка — автопроверка возвращает агента
        tool_call_chunks("edit", {"path": "app.py", "old": "def hello", "new": "from pkg.registry import REG\n\nREG['hello'] = 1\n\n\ndef hello"}, "c2"),
        text_chunks("встроил"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    notices = [it["text"] for it in s.items if it["type"] == "notice"]
    assert any("pkg/registry.py" in n and "не используются" in n for n in notices)
    assert s.messages[-1]["content"] == "встроил"


def test_unused_modules_ignores_tests_and_used(tmp_path):
    from lcw.tools import unused_modules
    (tmp_path / "a.py").write_text("import b\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("Y = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_c.py").write_text("import c\n", encoding="utf-8")
    assert unused_modules(tmp_path, ["b.py", "c.py", "tests/test_c.py"]) == ["c.py"]


def test_new_package_and_import_errors(tmp_path):
    from lcw.tools import import_errors, unused_modules
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "a.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "algos").mkdir()
    (tmp_path / "algos" / "__init__.py").write_text("from .missing import Y\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("raise SystemExit('не импортируется')\n", encoding="utf-8")
    created = ["algos/__init__.py", "core/__init__.py"]
    assert unused_modules(tmp_path, created) == ["algos/__init__.py"]  # core уже был пакетом
    errs = import_errors(tmp_path, ["algos/__init__.py", "core/a.py", "main.py"])
    assert len(errs) == 1 and errs[0].startswith("algos: ModuleNotFoundError")


def test_entry_point_imports_checked_statically(tmp_path):
    from lcw.tools import import_errors
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "w.py").write_text("class Real:\n    pass\n", encoding="utf-8")
    (tmp_path / "main.py").write_text(
        "import os\nfrom pkg.w import Real\nfrom pkg.w import Fake\nfrom pkg.gone import x\n"
        "raise SystemExit('точку входа не исполняем')\n", encoding="utf-8")
    errs = import_errors(tmp_path, ["main.py"])
    assert errs == ["main.py:3: в pkg.w нет имени Fake", "main.py:4: модуль pkg.gone не найден в проекте"]


def test_refusal_without_tools_is_sent_back_once(project):
    model, agent, s = make(project, [
        text_chunks("I'm sorry, but this is far beyond what can be done. Please provide a clear specification."),
        tool_call_chunks("symbols", {"path": "app.py"}, "c1"),
        text_chunks("Изучил: в app.py одна функция hello."),
    ])
    asyncio.run(agent.run_turn(s, "переделай всю архитектуру"))
    assert any("отказался" in it.get("text", "") for it in s.items if it["type"] == "notice")
    assert s.messages[-1]["content"].startswith("Изучил")


def test_plain_answer_not_treated_as_refusal(project):
    model, agent, s = make(project, [text_chunks("Привет! Чем помочь?")])
    asyncio.run(agent.run_turn(s, "привет"))
    assert not any(it["type"] == "notice" for it in s.items) and len(model.requests) == 1


def test_class_made_abstract_is_reported(tmp_path):
    from lcw.tools import import_errors
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "base.py").write_text(
        "from abc import ABC, abstractmethod\n\n\nclass Iface(ABC):\n    @abstractmethod\n"
        "    def run(self):\n        ...\n", encoding="utf-8")
    (tmp_path / "pkg" / "algo.py").write_text(
        "from pkg.base import Iface\n\n\nclass Good(Iface):\n    def run(self):\n        return 1\n\n\n"
        "class Bad(Iface):\n    pass\n", encoding="utf-8")
    errs = import_errors(tmp_path, ["pkg/base.py", "pkg/algo.py"])
    assert errs == ["pkg.algo: класс Bad абстрактный — не реализованы run; Bad() упадёт с TypeError"]


def test_single_step_update_merges_into_plan(project):
    model, agent, s = make(project, [
        plan_chunks("шаг А", "шаг Б", "проверка"),
        tool_call_chunks("plan", {"steps": [{"text": "шаг А", "status": "done"}]}, "p2"),
        tool_call_chunks("plan", {"steps": [{"text": "чужой шаг", "status": "done"}]}, "p3"),
        tool_call_chunks("plan", {"steps": [{"text": "шаг Б", "status": "done"},
                                            ]}, "p4"),
        tool_call_chunks("plan", {"steps": [{"text": "проверка", "status": "done"}]}, "p5"),
        text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    res = tool_results(s)
    assert "[x] 1. шаг А" in res[1] and "[ ] 2. шаг Б" in res[1]
    assert "ПОЛНЫЙ список" in res[2]  # шага нет в плане — понятная ошибка
    assert [x["status"] for x in s.plan] == ["done", "done", "done"]


def test_open_plan_carries_over_to_next_turn(project):
    model, agent, s = make(project, [
        plan_chunks("правка", "проверка"),
        text_chunks("пауза"), text_chunks("пауза"), text_chunks("пауза"),  # ход кончился с открытым планом
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("ok"), text_chunks("ok"), text_chunks("ok"),
    ])
    asyncio.run(agent.run_turn(s, "x"))
    asyncio.run(agent.run_turn(s, "продолжай"))
    assert "'hey'" in (Path(project["path"]) / "app.py").read_text()


def test_unused_new_imports(tmp_path):
    from lcw.tools import unused_new_imports
    (tmp_path / "w.py").write_text("import os\nfrom base import Iface, reg\n\n\n@reg\nclass A:\n    pass\n",
                                   encoding="utf-8")
    assert unused_new_imports(tmp_path, {"w.py": "import os\n\n\nclass A:\n    pass\n"}) == \
        ["w.py: импорт Iface добавлен, но не используется"]


def test_requirements_review_before_finish(project):
    task = "Сделай:\n1. переименуй приветствие в hey\n2. добавь функцию bye в app.py\n"
    model, agent, s = make(project, [
        plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("Готово"),
        text_chunks("1. сделано — app.py: hello. 2. не сделано: bye не добавлена."),
        tool_call_chunks("edit", {"path": "app.py", "old": "", "new": "def bye():\n    return 'bye'\n"}, "c2"),
        text_chunks("1. сделано. 2. сделано — app.py: bye."),
    ])
    asyncio.run(agent.run_turn(s, task))
    review = [m["content"] for m in s.messages if m["role"] == "user" and "КАЖДЫМ пунктом" in m["content"]]
    assert len(review) == 1 and "2. добавь функцию bye в app.py" in review[0]
    assert any("несделанные пункты" in it.get("text", "") for it in s.items if it["type"] == "notice")
    assert "def bye" in (Path(project["path"]) / "app.py").read_text()
    assert s.messages[-1]["content"].startswith("1. сделано. 2. сделано")


def test_requirements_parsing():
    from lcw.agent import requirements
    assert requirements("просто вопрос") == []
    assert requirements("1. первое требование\n2) второе требование\n- третье требование") == [
        "первое требование", "второе требование", "третье требование"]
