import time

from conftest import MockModel, plan_chunks, text_chunks, tool_call_chunks
from fastapi.testclient import TestClient

from lcw import server
from lcw.llm import LLM


def test_api_flow(project, monkeypatch):
    model = MockModel([
        plan_chunks(status="done"),
        tool_call_chunks("edit", {"path": "app.py", "old": "'hi'", "new": "'hey'"}, "c1"),
        text_chunks("Сделано"),
    ])
    llm = LLM(base_url="http://mock", password="", transport=model.transport())
    monkeypatch.setattr(server.agent, "llm", llm)
    monkeypatch.setattr(server, "llm", llm)

    with TestClient(server.app) as c:
        assert c.get("/").status_code == 200
        st = c.get("/api/status").json()
        assert st["ok"] and st["ready"]
        assert any(p["id"] == project["id"] for p in c.get("/api/projects").json())
        assert "app.py" in c.get(f"/api/projects/{project['id']}/files").json()

        sid = c.post("/api/sessions", json={"project_id": project["id"], "settings": {"mode": "auto"}}).json()["id"]
        snap = c.get(f"/api/sessions/{sid}").json()
        assert snap["items"] == [] and snap["meta"]["settings"]["mode"] == "auto"

        assert c.post(f"/api/sessions/{sid}/message", json={"text": "замени"}).status_code == 200
        for _ in range(100):
            if not c.get(f"/api/sessions/{sid}").json()["meta"]["running"]:
                break
            time.sleep(0.05)
        snap = c.get(f"/api/sessions/{sid}").json()
        assert [it["type"] for it in snap["items"]] == ["user", "reasoning", "plan", "reasoning", "tool", "text"]
        assert snap["items"][-1]["text"] == "Сделано"

        # буфер операций для SSE (сам SSE TestClient не умеет — он ждёт конца ответа)
        ops = server.store.get(sid).ops
        assert ops[0]["op"] == "meta" and ops[1]["op"] == "add"
        assert [o["seq"] for o in ops] == list(range(1, len(ops) + 1))

        ch = c.get(f"/api/sessions/{sid}/changes").json()
        assert ch[0]["path"] == "app.py"
        assert c.get(f"/api/projects/{project['id']}/zip").headers["content-type"] == "application/zip"

        assert c.post(f"/api/sessions/{sid}/rewind", json={"turn": 1}).json()["restored"] == ["app.py"]
        assert c.patch(f"/api/sessions/{sid}", json={"settings": {"effort": "high"}}).json()["settings"]["effort"] == "high"
        assert any(s["id"] == sid for s in c.get(f"/api/projects/{project['id']}/sessions").json())
        assert c.delete(f"/api/sessions/{sid}").json()["ok"]
