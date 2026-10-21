import asyncio
import subprocess
from pathlib import Path

import pytest
from conftest import MockModel, plan_chunks, text_chunks, tool_call_chunks

from lcw import prompts
from lcw.agent import Agent
from lcw.llm import LLM
from lcw.sessions import store
from lcw.tools import ToolContext, ToolError, execute, git_preview

GIT_ARGS = {"message": "Change greeting", "summary": "app.py: hi -> hey", "tests": "py_compile app.py: ok"}


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          check=True).stdout.strip()


@pytest.fixture
def repo(project, tmp_path_factory):
    root = Path(project["path"])
    git(root, "init", "-q", "-b", "feature")
    git(root, "config", "user.email", "t@t"), git(root, "config", "user.name", "t")
    git(root, "add", "-A"), git(root, "commit", "-qm", "init")
    remote = tmp_path_factory.mktemp("remote")
    git(remote, "init", "-q", "--bare")
    git(root, "remote", "add", "origin", str(remote))
    (root / "app.py").write_text("def hello():\n    return 'hey'\n")
    return project, root, remote


def test_preview_requires_description_and_tests(repo):
    _, root, _ = repo
    ctx = ToolContext(root=root)
    with pytest.raises(ToolError, match="tests"):
        git_preview(ctx, message="m", summary="s", tests=" ")
    with pytest.raises(ToolError, match="summary"):
        git_preview(ctx, message="m", summary="", tests="t")
    g = git_preview(ctx, **GIT_ARGS, push=True)
    assert g["files"] == ["app.py"] and g["branch"] == "feature" and g["upstream"] is None


def test_secrets_refused(repo):
    _, root, _ = repo
    (root / ".env").write_text("TOKEN=1")
    with pytest.raises(ToolError, match="секреты"):
        git_preview(ToolContext(root=root), **GIT_ARGS)
    assert git_preview(ToolContext(root=root), **GIT_ARGS, files=["app.py"])["files"] == ["app.py"]


def test_commit_and_push(repo):
    _, root, remote = repo
    r = asyncio.run(execute(ToolContext(root=root), "git_commit", {**GIT_ARGS, "push": True}))
    assert r.ok, r.output
    log = git(root, "log", "-1", "--format=%B")
    assert log.startswith("Change greeting") and "Проверки:\npy_compile app.py: ok" in log
    assert git(remote, "log", "-1", "--format=%s", "feature") == "Change greeting"
    assert "Push выполнен" in r.output


def _session(project, script, mode):
    model = MockModel(script)
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    return Agent(llm), store.create(project["id"], prompts.build_system(project["path"], ""), {"mode": mode})


def test_git_via_run_blocked(repo):
    project, root, _ = repo
    agent, s = _session(project, [plan_chunks(status="done"),
                                  tool_call_chunks("run", {"command": "git commit -am x"}, "c1"),
                                  text_chunks("ok")], "auto")
    asyncio.run(agent.run_turn(s, "закоммить"))
    res = [m["content"] for m in s.messages if m["role"] == "tool"]
    assert "используй git_commit" in res[-1]
    assert git(root, "log", "-1", "--format=%s") == "init"


def test_git_commit_always_asks_even_in_auto_and_always_is_one_time(repo):
    project, root, _ = repo
    agent, s = _session(project, [tool_call_chunks("git_commit", GIT_ARGS, "g1"), text_chunks("готово")], "auto")

    async def scenario():
        task = asyncio.create_task(agent.run_turn(s, "закоммить"))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if "g1" in s.pending:
                break
        card = next(it for it in s.items if it.get("id") == "g1")
        assert card["status"] == "approve" and card["git"]["files"] == ["app.py"]
        s.pending["g1"].set_result(("always", ""))
        await task

    asyncio.run(asyncio.wait_for(scenario(), 30))
    assert git(root, "log", "-1", "--format=%s") == "Change greeting"
    assert "git_commit" not in s.settings.get("always", [])


def test_git_commit_denied(repo):
    project, root, _ = repo
    agent, s = _session(project, [tool_call_chunks("git_commit", GIT_ARGS, "g1"), text_chunks("ок")], "auto")

    async def scenario():
        task = asyncio.create_task(agent.run_turn(s, "закоммить"))
        for _ in range(300):
            await asyncio.sleep(0.01)
            if "g1" in s.pending:
                break
        s.pending["g1"].set_result(("deny", "не сейчас"))
        await task

    asyncio.run(asyncio.wait_for(scenario(), 30))
    assert git(root, "log", "-1", "--format=%s") == "init"
