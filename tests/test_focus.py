"""Работа по пунктам многопунктного запроса, сверка результата и детектор заглушек."""
import asyncio
from pathlib import Path

from conftest import plan_chunks, text_chunks, tool_call_chunks
from test_plan import make

def decide(cid="d1"):
    """Решение по пункту — обязательно перед правками в попунктном режиме."""
    return tool_call_chunks("decide", {"decision": "менять функцию hello в app.py, остальное не трогать",
                                       "uses": ["hello"], "fallback": "прежнее поведение"}, cid)


TASK = "Сделай:\n1. переименуй приветствие hello в hey\n2. добавь функцию bye в app.py\n"


def test_items_one_by_one_then_review(project):
    """Пункты выдаются по одному (со связанным кодом), в конце — одна сверка."""
    model, agent, s = make(project, [
        decide("d1"), plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("Пункт 1 готов"), decide("d2"),
        tool_call_chunks("edit", {"path": "app.py", "old": "", "new": "def bye():\n    return 'bye'\n"}, "c2"),
        text_chunks("Пункт 2 готов"),
        text_chunks("1. сделано — app.py: hello. 2. сделано — app.py: bye."),
    ])
    asyncio.run(agent.run_turn(s, TASK))
    first = s.messages[0]["content"]
    assert "[Работа по пунктам]" in first and "Сейчас пункт 1" in first and "app.py" in first
    users = [m["content"] for m in s.messages if m["role"] == "user"]
    assert any("Сейчас пункт 2: добавь функцию bye" in u for u in users)
    assert sum("КАЖДЫМ пунктом" in u for u in users) == 1
    assert "def bye" in (Path(project["path"]) / "app.py").read_text() and s.focus == 1
    assert s.messages[-1]["content"].startswith("1. сделано")


def test_decide_gate_and_unknown_names(project):
    """Без решения правка пункта не проходит; решение с выдуманными сущностями отклоняется."""
    model, agent, s = make(project, [
        plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        tool_call_chunks("decide", {"decision": "отбирать по метрике sharpe из таблицы метрик",
                                    "uses": ["MetricsTable.sharpe", "helo"], "fallback": "как раньше"}, "d0"),
        decide("d1"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c2"),
        text_chunks("Пункт 1 готов"), text_chunks("Пункт 2 нет"), text_chunks("1. сделано. 2. сделано."),
    ])
    asyncio.run(agent.run_turn(s, TASK))
    res = [m["content"] for m in s.messages if m["role"] == "tool"]
    assert "зафиксируй решение через decide" in res[1]
    assert "MetricsTable.sharpe: нет в проекте" in res[2] and "helo: нет в проекте (похожие: hello)" in res[2]
    assert res[4].startswith("OK:") and s.decisions["0"]["uses"] == ["hello"]
    review = next(m["content"] for m in s.messages if m["role"] == "user" and "КАЖДЫМ пунктом" in m["content"])
    assert "Зафиксированные решения" in review and "менять функцию hello" in review


def test_undone_after_review_is_pushed_once(project):
    model, agent, s = make(project, [
        decide("d1"), plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("Пункт 1 готов"), text_chunks("Пункт 2 сделаю позже"),
        text_chunks("1. сделано. 2. не сделано — bye нет."),
        text_chunks("Нужно решение пользователя: где разместить bye."),
    ])
    asyncio.run(agent.run_turn(s, TASK))
    notices = [it["text"] for it in s.items if it["type"] == "notice"]
    assert sum("несделанные пункты" in n for n in notices) == 1
    assert s.messages[-1]["content"].startswith("Нужно решение")


def test_stub_method_sent_back(project):
    model, agent, s = make(project, [
        plan_chunks("метод", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "",
                                  "new": "def run():\n    return []\n"}, "c1"),
        text_chunks("готово"),
        tool_call_chunks("edit", {"path": "app.py", "old": "    return []", "new": "    return [hello()]"}, "c2"),
        text_chunks("реализовал"),
    ])
    asyncio.run(agent.run_turn(s, "добавь run"))
    assert any("заглушки" in it.get("text", "") for it in s.items if it["type"] == "notice")
    assert "return [hello()]" in (Path(project["path"]) / "app.py").read_text()


def test_tool_call_parse_error_retried_with_hint(project):
    model, agent, s = make(project, [
        [{"error": {"message": "The model produced output that does not match the expected peg-native format"}}],
        text_chunks("со второй попытки"),
    ])
    asyncio.run(agent.run_turn(s, "привет"))
    assert s.messages[-1]["content"] == "со второй попытки"
    assert "не распознан" in model.requests[1]["messages"][-1]["content"]  # подсказка — только в запросе
    assert not any("не распознан" in str(m.get("content")) for m in s.messages)


def test_abandoned_item_gets_minimal_version_push(project):
    model, agent, s = make(project, [
        decide("d1"), plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("Пункт 1 готов"),
        tool_call_chunks("read", {"path": "app.py"}, "r1"),
        text_chunks("Пункт 2 слишком обширный, уточните, пожалуйста, детали."), decide("d2"),
        tool_call_chunks("edit", {"path": "app.py", "old": "", "new": "def bye():\n    return 'bye'\n"}, "c2"),
        text_chunks("Сделал минимальный вариант"),
        text_chunks("1. сделано. 2. сделано — app.py: bye."),
    ])
    asyncio.run(agent.run_turn(s, TASK))
    assert any("брошен без правок" in it.get("text", "") for it in s.items if it["type"] == "notice")
    assert "def bye" in (Path(project["path"]) / "app.py").read_text()


def test_failing_project_tests_block_finish(project):
    root = Path(project["path"])
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent.parent))\n"
        "from app import hello\n\n\ndef test_hello():\n    assert hello() == 'hi'\n", encoding="utf-8")
    model, agent, s = make(project, [
        decide("d1"), plan_chunks("правка", "проверка", status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("готово, всё работает"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hey'", "new": "'hi'"}, "c2"),
        text_chunks("вернул поведение"),
    ])
    asyncio.run(agent.run_turn(s, "поменяй приветствие"))
    feedback = [m["content"] for m in s.messages if m["role"] == "user" and "тесты проекта падают" in m["content"]]
    assert len(feedback) == 1 and "test_hello" in feedback[0]
    assert s.messages[-1]["content"] == "вернул поведение"


def test_merged_functions_show_before_after(tmp_path):
    from lcw.tools import merged_functions
    (tmp_path / "common").mkdir()
    (tmp_path / "common" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "common" / "fmt.py").write_text(
        "def fmt(x):\n    \"\"\"Общий формат.\"\"\"\n    return f'{x:.1f}'\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("from common.fmt import fmt\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("from common.fmt import fmt as _f\n\n\ndef show(x):\n    return _f(x)\n",
                                   encoding="utf-8")
    (tmp_path / "c.py").write_text("from common.fmt import fmt\n", encoding="utf-8")
    out = merged_functions(tmp_path, {
        "a.py": "def fmt(x):\n    return f'{x:.2f}'\n",           # другая точность — сообщить
        "b.py": "def show(x):\n    return f'X={x}'\n",            # тело сведено к вызову — сообщить
        "c.py": "def fmt(x):\n    return f'{x:.1f}'\n",           # точная копия — молчать
    })
    assert len(out) == 2 and out[0].startswith("a.py: fmt заменена на fmt из common/fmt.py")
    assert "x:.2f" in out[0] and "Общий формат" not in out[0]  # докстринг не показываем
    assert out[1].startswith("b.py: show заменена на _f")


def test_registry_with_same_object_twice_reported(tmp_path):
    from lcw.tools import import_errors
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("from pkg.a import A\n\n\nclass B:\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "reg.py").write_text(
        "from pkg.a import A\nfrom pkg.b import A as BAlgo\n\nREG = {'a': A, 'b': BAlgo}\n", encoding="utf-8")
    errs = import_errors(tmp_path, ["pkg/reg.py"])
    assert len(errs) == 1 and "ключи 'a' и 'b' указывают на один объект pkg.a.A" in errs[0]


def test_new_stubs_detected(tmp_path):
    from lcw.tools import new_stubs
    (tmp_path / "w.py").write_text(
        "from abc import abstractmethod\n\n\nclass A:\n    def run(self):\n        return []\n\n"
        "    def old(self):\n        pass\n\n    @abstractmethod\n    def iface(self):\n        ...\n\n"
        "    def name(self):\n        return 'a'\n", encoding="utf-8")
    before = "class A:\n    def old(self):\n        pass\n"
    assert new_stubs(tmp_path, {"w.py": before}) == ["w.py: A.run — заглушка без реальной работы"]


def test_sampling_sent_to_model(project):
    from lcw import config
    model, agent, s = make(project, [text_chunks("ok")])
    asyncio.run(agent.run_turn(s, "привет"))
    assert model.requests[0]["temperature"] == config.SAMPLING["temperature"] == 0.4
