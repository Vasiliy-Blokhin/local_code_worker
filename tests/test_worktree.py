"""Изолированная работа сессии в git worktree."""
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from lcw import workspace, worktree


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def repo(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return root


def test_create_patch_apply_remove(tmp_path):
    root = repo(tmp_path)
    wt = worktree.create(root, "s1")
    path = Path(wt["path"])
    assert wt["branch"] == "lcw/s1" and (path / "app.py").is_file()
    assert (path / ".env").read_text() == "SECRET=1\n"  # настройки скопированы
    (path / "app.py").write_text("def hello():\n    return 'hey'\n", encoding="utf-8")
    (path / "new.py").write_text("X = 1\n", encoding="utf-8")
    assert "'hi'" in (root / "app.py").read_text()  # рабочая копия проекта не тронута
    assert sorted(worktree.changed_files(wt)) == ["app.py", "new.py"]
    assert sorted(worktree.apply(root, wt)) == ["app.py", "new.py"]
    assert "'hey'" in (root / "app.py").read_text() and (root / "new.py").is_file()
    assert git(root, "log", "--oneline").count("\n") == 1  # без коммита
    worktree.remove(root, wt)
    assert not path.exists() and "lcw/s1" not in git(root, "branch")


def test_apply_keeps_crlf_files_intact(tmp_path):
    root = repo(tmp_path)
    git(root, "config", "core.autocrlf", "false")
    (root / "win.py").write_bytes(b"A = 1\r\nB = 2\r\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "crlf")
    wt = worktree.create(root, "s3")
    try:
        (Path(wt["path"]) / "win.py").write_bytes(b"A = 1\r\nB = 3\r\n")
        assert worktree.apply(root, wt) == ["win.py"]
        assert (root / "win.py").read_bytes() == b"A = 1\r\nB = 3\r\n"
    finally:
        worktree.remove(root, wt)


def test_venv_found_from_worktree(tmp_path):
    root = repo(tmp_path)
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    wt = worktree.create(root, "s2")
    try:
        assert workspace.find_venv(wt["path"]) == (tmp_path / ".venv").resolve()
    finally:
        worktree.remove(root, wt)


def test_api_isolated_session(tmp_path):
    from lcw.server import app
    root = repo(tmp_path)
    pid = workspace.add_project(str(root))["id"]
    c = TestClient(app)
    sid = c.post("/api/sessions", json={"project_id": pid, "settings": {"worktree": True}}).json()["id"]
    meta = c.get(f"/api/sessions/{sid}").json()["meta"]
    assert meta["worktree"]["branch"] == f"lcw/{sid}"
    from lcw.sessions import store
    s = store.get(sid)
    assert s.system.count(s.worktree["path"]) >= 1  # агент видит корнем worktree
    (Path(s.worktree["path"]) / "app.py").write_text("def hello():\n    return 'yo'\n", encoding="utf-8")
    r = c.post(f"/api/sessions/{sid}/worktree/apply")
    assert r.json()["files"] == ["app.py"] and "'yo'" in (root / "app.py").read_text()
    wt_path = Path(s.worktree["path"])
    assert c.delete(f"/api/sessions/{sid}").json()["ok"] and not wt_path.exists()


def test_api_worktree_requires_git(tmp_path):
    from lcw.server import app
    (tmp_path / "plain").mkdir()
    pid = workspace.add_project(str(tmp_path / "plain"))["id"]
    r = TestClient(app).post("/api/sessions", json={"project_id": pid, "settings": {"worktree": True}})
    assert r.status_code == 400 and "git" in r.json()["detail"]


def test_tracked_env_example_is_carried_over(tmp_path):
    root = repo(tmp_path)
    (root / ".env.example").write_text("A=1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "example")
    wt = worktree.create(root, "s4")
    try:
        (Path(wt["path"]) / ".env.example").write_text("A=1\nB=2\n", encoding="utf-8")
        (Path(wt["path"]) / ".env.local").write_text("SECRET=2\n", encoding="utf-8")  # не в git — не переносим
        assert worktree.changed_files(wt) == [".env.example"]
    finally:
        worktree.remove(root, wt)
