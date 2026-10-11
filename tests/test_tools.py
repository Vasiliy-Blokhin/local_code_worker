import asyncio
from pathlib import Path

from lcw.tools import ToolContext, execute


def run(ctx, name, **args):
    return asyncio.run(execute(ctx, name, args))


def ctx_for(project):
    return ToolContext(root=Path(project["path"]))


def test_read_numbered_and_dedup(project):
    ctx = ctx_for(project)
    r = run(ctx, "read", path="app.py")
    assert "1│def hello():" in r.output
    again = run(ctx, "read", path="app.py")
    assert "не изменился" in again.output


def test_read_range_and_missing(project):
    ctx = ctx_for(project)
    r = run(ctx, "read", path="app.py", offset=2, limit=1)
    assert r.output.startswith("2│")
    assert not run(ctx, "read", path="nope.py").ok


def test_path_escape_blocked(project):
    r = run(ctx_for(project), "read", path="../../etc/passwd")
    assert not r.ok and "вне проекта" in r.output


def test_glob_and_grep(project):
    ctx = ctx_for(project)
    assert "pkg/util.py" in run(ctx, "glob", pattern="**/*.py").output
    assert "app.py" in run(ctx, "glob", pattern="*.py").output
    g = run(ctx, "grep", pattern=r"return\s+'hi'")
    assert g.output.startswith("app.py:2:")
    assert run(ctx, "grep", pattern="Y = ", glob="*.py").output.startswith("pkg/util.py:2")


def test_edit_exact_and_crlf_preserved(project):
    ctx = ctx_for(project)
    snaps = {}
    ctx.snapshot = lambda rel, text: snaps.setdefault(rel, text)
    r = run(ctx, "edit", path="pkg/util.py", old="Y = 2", new="Y = 3")
    assert r.ok, r.output
    raw = (Path(project["path"]) / "pkg/util.py").read_bytes()
    assert raw == b"X = 1\r\nY = 3\r\n"
    assert snaps["pkg/util.py"] == "X = 1\r\nY = 2\r\n"
    assert "+Y = 3" in r.diff


def test_edit_trailing_whitespace_tolerant(project):
    ctx = ctx_for(project)
    r = run(ctx, "edit", path="app.py", old="def hello():   \n    return 'hi'", new="def hello():\n    return 'hello'")
    assert r.ok, r.output
    assert "return 'hello'" in (Path(project["path"]) / "app.py").read_text()


def test_edit_not_found_gives_hint_and_ambiguous(project):
    ctx = ctx_for(project)
    r = run(ctx, "edit", path="app.py", old="def helo():\n    return 'hi'", new="x")
    assert not r.ok and "Самый похожий" in r.output
    (Path(project["path"]) / "d.py").write_text("a\na\n")
    r = run(ctx, "edit", path="d.py", old="a", new="b")
    assert not r.ok and "2 раз" in r.output
    assert run(ctx, "edit", path="d.py", old="a", new="b", replace_all=True).ok


def test_write_new_file(project):
    ctx = ctx_for(project)
    snaps = {}
    ctx.snapshot = lambda rel, text: snaps.setdefault(rel, text)
    r = run(ctx, "write", path="new/mod.py", content="print(1)\n")
    assert r.ok and snaps == {"new/mod.py": None}
    assert (Path(project["path"]) / "new/mod.py").read_text() == "print(1)\n"


def test_run_command(project):
    r = run(ctx_for(project), "run", command="echo привет")
    assert r.ok and "привет" in r.output and "[exit 0]" in r.output


def test_run_timeout(project):
    r = run(ctx_for(project), "run", command="sleep 5", timeout=1)
    assert not r.ok and "ТАЙМАУТ" in r.output


def test_bad_args_and_unknown_tool(project):
    ctx = ctx_for(project)
    assert not run(ctx, "read", file="x").ok
    assert not run(ctx, "delete_all").ok


def test_edit_shows_snippet_and_syntax_warning(project):
    ctx = ctx_for(project)
    r = run(ctx, "edit", path="app.py", old="    return 'hi'", new="    return 'hi'\n\nX = (")
    assert r.ok and "Теперь:" in r.output and "2│    return 'hi'" in r.output
    assert "синтаксическая ошибка" in r.output


def test_edit_ambiguous_lists_lines(project):
    ctx = ctx_for(project)
    (Path(project["path"]) / "m.py").write_text("def a():\n    pass\n\ndef b():\n    pass\n")
    r = run(ctx, "edit", path="m.py", old="    pass", new="    return 1")
    assert not r.ok and "строки 2, 5" in r.output and "replace_all" not in r.output
    r = run(ctx, "edit", path="m.py", old="def b():\n    pass", new="def b():\n    return 1")
    assert r.ok and "def b():" in r.output
