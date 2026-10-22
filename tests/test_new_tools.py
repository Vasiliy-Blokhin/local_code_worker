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


def test_evidence_hint_points_to_logs_and_server_docs(tmp_path):
    from lcw.agent import INVESTIGATE, evidence_hint
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "site.log").write_text("x\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "vm.md").write_text("intro\n`sudo journalctl -u guard`\n", encoding="utf-8")
    hint = evidence_hint(tmp_path)
    assert "logs/ (1 файлов: site.log)" in hint and "docs/vm.md (строки 2)" in hint
    assert INVESTIGATE.search("найти причину периодического отключения сайта")
    assert not INVESTIGATE.search("добавить кнопку экспорта")


def test_untested_changes(tmp_path):
    from lcw.tools import untested_changes
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "g.py").write_text("X = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_g.py").write_text("from app.g import X\n", encoding="utf-8")
    assert untested_changes(tmp_path, {"app/g.py": "X = 1\n"}) == ["app/g.py (тесты: tests/test_g.py)"]
    assert untested_changes(tmp_path, {"app/g.py": "X = 1\n", "tests/test_g.py": ""}) == []
    assert untested_changes(tmp_path, {"app/g.py": "X = 2\n"}) == []  # не изменился


def test_cause_must_quote_seen_facts():
    from lcw.agent import grounded
    seen = "пики по hour: 2026-10-18 08 ×118\nпо ip: 79.124.58.82 ×1380"
    assert grounded("сайт закрыт 2026-10-18 08: один IP 79.124.58.82 дал 1380 запросов", seen)
    assert not grounded("внутренние запросы 127.0.0.1 и 192.168.0.1 вызывают закрытие", seen)


def test_edit_strips_diff_plus_marks(tmp_path):
    import pytest as _pytest
    from lcw.tools import ToolError, t_edit, t_read
    (tmp_path / "t.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    c = _ctx(tmp_path)
    t_edit(c, "t.py", "def a():\n    return 1\n",
           "def a():\n    return 1\n+\n+\n+def b():\n+    return 2\n")
    assert (tmp_path / "t.py").read_text(encoding="utf-8") == "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    # неудачная правка сбрасывает «уже видел»: повторное чтение снова отдаёт текст
    t_read(c, "t.py")
    with _pytest.raises(ToolError):
        t_edit(c, "t.py", "def zzz():", "def yyy():")
    assert "def b():" in t_read(c, "t.py").output


def test_tool_called_through_shell_is_routed():
    from lcw.agent import tool_from_shell
    assert tool_from_shell("logstats --path=logs/access.log --match=ERROR|WARNING") == (
        "logstats", {"path": "logs/access.log", "match": "ERROR|WARNING"})
    assert tool_from_shell("logstats logs --show 5") == ("logstats", {"path": "logs", "show": 5})
    assert tool_from_shell("refs guard_ip_limit") == ("refs", {"name": "guard_ip_limit"})
    assert tool_from_shell("pytest -q") is None and tool_from_shell("ls logs") is None


def test_stale_log_is_flagged(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "old.log").write_text("2020-01-01 10:00:00 ERROR x\n", encoding="utf-8")
    assert "лог устарел" in t_logstats(_ctx(tmp_path), "logs/old.log").output


def test_server_commands_from_docs(tmp_path):
    from lcw.agent import server_commands
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "vm.md").write_text(
        "`sudo journalctl -u guard -n 50`, `sudo tail -f /var/log/nginx/access.log`, `sudo systemctl restart x`\n",
        encoding="utf-8")
    assert server_commands(tmp_path) == ["sudo journalctl -u guard -n 50"]


def test_fetch_commands_build_ready_ssh(tmp_path):
    from lcw.agent import fetch_commands
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "vm.md").write_text(
        "Войти: `ssh -i ~/.ssh/admin me@10.0.0.5`, лог: `sudo tail /var/log/nginx/access.log`\n", encoding="utf-8")
    assert fetch_commands(tmp_path) == [
        'mkdir -p logs && ssh -o BatchMode=yes -o ConnectTimeout=15 -i ~/.ssh/admin me@10.0.0.5 "zcat -f /var/log/nginx/access.log*"'
        ' > logs/server_access.log']


def test_hour_digest_shows_who_was_there(tmp_path):
    from lcw.agent import hour_digest
    (tmp_path / "logs").mkdir()
    rows = [{"time": "2026-10-18T08:2%d:00+03:00" % (i % 10), "ip": "6.6.6.6" if i < 90 else "1.1.1.1",
             "status": "429"} for i in range(100)]
    (tmp_path / "logs" / "server_access.log").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    out = hour_digest(_ctx(tmp_path), "сайт закрыт 2026-10-18 08:25 из-за ботов")
    assert "6.6.6.6 ×90" in out and "429 ×100" in out


def test_address_counts_refute_guess(tmp_path):
    from lcw.agent import address_counts
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "server_access.log").write_text(
        '{"ip":"6.6.6.6"}\n' * 9 + '{"ip":"192.168.0.23"}\n', encoding="utf-8")
    out = address_counts(_ctx(tmp_path), "много запросов с 127.0.0.1 и 192.168.0.*")
    assert "logs/server_access.log (10 строк): 127.0.0.1: 0, 192.168.0.*: 1" in out


def test_edit_strips_mostly_marked_lines(tmp_path):
    from lcw.tools import t_edit
    (tmp_path / "t.py").write_text("X = 1\n", encoding="utf-8")
    t_edit(_ctx(tmp_path), "t.py", "", "\n# new tests\n+def b():\n+    return 2\n+\n")
    assert "+def" not in (tmp_path / "t.py").read_text(encoding="utf-8")


def test_removed_tests_detected(tmp_path):
    from lcw.tools import removed_tests
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_new():\n    pass\n", encoding="utf-8")
    orig = "def test_old():\n    pass\n\n\ndef test_new():\n    pass\n"
    assert removed_tests(tmp_path, {"tests/test_a.py": orig}) == ["tests/test_a.py: test_old"]


def test_spoofable_exemption_patterns():
    from lcw.agent import EXEMPT, SPOOFABLE
    d = "Add UA-based bot ignore in is_suspicious and pass UA from parse_line"
    assert SPOOFABLE.search(d) and EXEMPT.search(d)
    d2 = "DDoS closes the site only if requests come from at least 20 addresses"
    assert not (SPOOFABLE.search(d2) and EXEMPT.search(d2))


def test_status_codes_are_not_facts():
    from lcw.agent import grounded
    seen = "по status: 404 ×1618, 429 ×858"
    assert not grounded("guard закрывал сайт из-за большого числа 404 на статику", seen)
    assert grounded("1618 ответов 404", seen)


def test_peak_digest_names_the_peak(tmp_path):
    from lcw.agent import peak_digest
    (tmp_path / "logs").mkdir()
    rows = [{"time": "2026-10-18T08:20:00+03:00", "ip": "6.6.6.6", "status": "429"}] * 50 + \
           [{"time": "2026-10-18T10:00:00+03:00", "ip": "1.1.1.1", "status": "200"}] * 3
    (tmp_path / "logs" / "server_access.log").write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert "6.6.6.6 ×50" in peak_digest(_ctx(tmp_path))


def test_spoof_check_single_address(tmp_path):
    from lcw.agent import spoof_check
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "server_access.log").write_text(
        '{"ip":"6.6.6.6","ua":"Googlebot/2.1"}\n' * 5, encoding="utf-8")
    assert "подделка" in spoof_check(_ctx(tmp_path), "ignore Googlebot")


def test_untested_skips_shared_modules(tmp_path):
    from lcw.tools import untested_changes
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "config.py").write_text("X = 2\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    for n in ("a", "b", "c"):
        (tmp_path / "tests" / f"test_{n}.py").write_text("from app.config import X\n", encoding="utf-8")
    assert untested_changes(tmp_path, {"app/config.py": "X = 1\n"}) == []
