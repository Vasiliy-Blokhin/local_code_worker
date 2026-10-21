"""Инструменты refs, test, logstats."""
import json

from lcw.tools import ToolContext, condense_pytest, t_logstats, t_refs, covering_tests


def _ctx(tmp_path):
    return ToolContext(root=tmp_path)


def test_refs_definition_use_and_env(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text("class S:\n    guard_limit: int = 5\n", encoding="utf-8")
    (tmp_path / "app" / "g.py").write_text(
        "from app.config import S\n\n\ndef check(s):\n    return s.guard_limit > 1\n", encoding="utf-8")
    (tmp_path / ".env.example").write_text("GUARD_LIMIT=7\n", encoding="utf-8")
    out = t_refs(_ctx(tmp_path), "settings.guard_limit").output
    assert "[def in S] guard_limit" in out
    assert "[use in check]" in out
    assert "GUARD_LIMIT=7" in out
    assert "в тестах не упоминается" in out


def test_refs_bad_name(tmp_path):
    assert not t_refs(_ctx(tmp_path), "a b").ok


def test_tests_for_finds_importers(tmp_path):
    (tmp_path / "app" / "guard").mkdir(parents=True)
    (tmp_path / "app" / "guard" / "watch.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("from app.guard import watch\n", encoding="utf-8")
    (tmp_path / "tests" / "test_b.py").write_text("from app.guard.watch import X\n", encoding="utf-8")
    (tmp_path / "tests" / "test_c.py").write_text("import os\n", encoding="utf-8")
    assert covering_tests(tmp_path, "app/guard/watch.py") == ["tests/test_a.py", "tests/test_b.py"]


def test_condense_pytest_keeps_failure_essentials():
    out = """F.
=================================== FAILURES ===================================
___________________________________ test_x ____________________________________
tests/test_a.py:5: in test_x
    assert f(1) == 3
E   assert 2 == 3
E    +  where 2 = f(1)
=========================== short test summary info ============================
FAILED tests/test_a.py::test_x - assert 2 == 3
1 failed, 1 passed in 0.05s"""
    short = condense_pytest(out)
    assert "✗ test_x" in short and "tests/test_a.py:5" in short and "E   assert 2 == 3" in short
    assert short.endswith("1 failed, 1 passed in 0.05s")


def test_logstats_json_and_text(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [{"time": f"2026-10-18T08:2{i % 3}:00+03:00", "ip": "1.1.1.1" if i < 8 else "2.2.2.2",
             "status": "429" if i < 6 else "200"} for i in range(10)]
    (logs / "access.log").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (logs / "app.log").write_text("2026-10-18 08:20:01 ERROR db down 12\n2026-10-18 08:21:07 ERROR db down 13\n"
                                  "2026-10-18 08:22:00 INFO ok\n", encoding="utf-8")
    out = t_logstats(_ctx(tmp_path), "logs/access.log", match='"429"', by="ip", bucket="minute").output
    assert out.startswith("6 строк") and "1.1.1.1 ×6" in out and "2026-10-18 08:20 ×2" in out
    out = t_logstats(_ctx(tmp_path), "logs", match="ERROR", by="message").output
    assert "2 строк" in out and "×2" in out  # числа нормализованы — одно сообщение
