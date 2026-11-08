"""Новые проекты и GUI-приложения: delete, точки входа, окружение проекта, запуск окна."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from lcw import config
from lcw.tools import (ToolContext, execute, gui_entry, project_env, small_project, unused_modules,
                       PIP_INSTALL, WHEEL_BUILD)


def test_delete_tool_and_snapshot(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    snaps = {}
    ctx = ToolContext(root=tmp_path, snapshot=lambda rel, txt: snaps.setdefault(rel, txt))
    res = asyncio.run(execute(ctx, "delete", {"path": "a.py"}))
    assert res.ok and not (tmp_path / "a.py").exists() and snaps == {"a.py": "x = 1\n"}
    res = asyncio.run(execute(ctx, "delete", {"path": "a.py"}))
    assert not res.ok and "нет такого файла" in res.output


def test_entry_script_is_not_unused(tmp_path):
    (tmp_path / "main.py").write_text("import game\nif __name__ == '__main__':\n    game.run()\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("def run():\n    pass\n", encoding="utf-8")
    (tmp_path / "orphan.py").write_text("X = 1\n", encoding="utf-8")
    assert unused_modules(tmp_path, ["main.py", "game.py", "orphan.py"]) == ["orphan.py"]


def test_gui_entry_and_small_project(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "game.py").write_text("import pyglet\nif __name__ == '__main__':\n    pass\n", encoding="utf-8")
    assert gui_entry(tmp_path) == "python src/game.py"
    (tmp_path / "main.py").write_text("from src import game\n\nif __name__ == \"__main__\":\n    game.x()\n",
                                      encoding="utf-8")
    assert gui_entry(tmp_path) == "python main.py"
    assert small_project(tmp_path)
    (tmp_path / "big.py").write_text("x = 1\n" * 2000, encoding="utf-8")
    assert not small_project(tmp_path)
    (tmp_path / "src" / "game.py").write_text("print(1)\n", encoding="utf-8")
    assert gui_entry(tmp_path) is None  # без GUI-библиотек — не GUI


def test_project_env_drops_own_venv(tmp_path, monkeypatch):
    own = str(config.ROOT / "venv" / "Scripts")
    monkeypatch.setenv("PATH", own + os.pathsep + r"C:\Windows")
    env = project_env(tmp_path)
    assert own.lower() not in env["PATH"].lower() and "C:\\Windows" in env["PATH"]


def test_pip_patterns():
    assert PIP_INSTALL.search("pip install pygame") and PIP_INSTALL.search("python -m pip install x")
    assert not PIP_INSTALL.search("pip list")
    assert WHEEL_BUILD.search("error: subprocess-exited-with-error\nGetting requirements to build wheel did not "
                              "run successfully.")


@pytest.mark.skipif(sys.platform != "win32", reason="окна — только Windows")
def test_launch_tkinter_window(tmp_path):
    pytest.importorskip("PIL")
    (tmp_path / "app.py").write_text(
        "import tkinter as tk\nr = tk.Tk()\nr.geometry('300x200')\nc = tk.Canvas(r, bg='black')\n"
        "c.pack(fill='both', expand=1)\nc.create_rectangle(10, 10, 100, 80, fill='red')\n"
        "r.bind('<Key>', lambda e: print('key', e.keysym, flush=True))\nr.mainloop()\n", encoding="utf-8")
    (tmp_path / "crash.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    ctx = ToolContext(root=tmp_path)
    res = asyncio.run(execute(ctx, "launch", {"command": f'"{sys.executable}" app.py', "seconds": 3,
                                              "steps": ["enter", "shot"]}))
    assert res.ok, res.output
    assert "окно открылось" in res.output and "key Return" in res.output
    assert res.shots and not res.shots[0]["blank"] and Path(res.shots[0]["path"]).is_file()
    res = asyncio.run(execute(ctx, "launch", {"command": f'"{sys.executable}" crash.py', "seconds": 3}))
    assert not res.ok and "ОКНО НЕ ОТКРЫЛОСЬ" in res.output and "boom" in res.output


def test_libdoc_reads_installed_package(tmp_path, monkeypatch):
    """libdoc находит пакет в site-packages, показывает __main__-пример и встроенные ресурсы."""
    from lcw import tools
    sp = tmp_path / "sp"
    pkg = sp / "fakeengine"
    (pkg / "textures").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "textures" / "grass.png").write_bytes(b"x")
    (pkg / "camera.py").write_text(
        "class Camera:\n    '''Scene camera.'''\n    def look_at(self, target):\n        pass\n\n"
        "if __name__ == '__main__':\n    cam = Camera()\n    cam.look_at((0, 0, 0))\n", encoding="utf-8")
    (pkg / "texty.py").write_text("x = 'texture texture texture'\n", encoding="utf-8")
    monkeypatch.setattr(tools, "_site_packages", lambda root: [sp])
    r = tools.t_libdoc(tools.ToolContext(root=tmp_path), "fakeengine", "camera look_at")
    assert "textures/: grass" in r.output
    assert "fakeengine/camera.py" in r.output and "cam.look_at" in r.output
    assert "texty.py" not in r.output.split("===", 1)[1].splitlines()[0]
    with pytest.raises(tools.ToolError):
        tools.t_libdoc(tools.ToolContext(root=tmp_path), "nosuchpkg", "x")
