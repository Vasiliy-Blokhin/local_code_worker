"""Работа с очередью задач AI Manager: X-Project, статус «GPU занята», 409 model_busy, политика повторов."""
import asyncio
import json

import httpx
import pytest
from conftest import sse, text_chunks
from fastapi.testclient import TestClient

from lcw import llm as llm_mod
from lcw import server
from lcw.llm import LLM, LLMBusy, LLMError


def make_llm(handler):
    return LLM(base_url="http://mock", password="pw", transport=httpx.MockTransport(handler))


def run(coro):
    return asyncio.run(coro)


async def collect(llm):
    return [ev async for ev in llm.stream([{"role": "user", "content": "привет"}])]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def fast(_):
        return None
    monkeypatch.setattr(llm_mod.asyncio, "sleep", fast)


def test_project_header_and_model_in_requests():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text=sse(text_chunks("готово")), headers={"content-type": "text/event-stream"})

    events = run(collect(make_llm(handler)))
    assert events[-1][0] == "done"
    req = seen[0]
    assert req.headers["X-Project"] == "local_code_worker"
    assert req.headers["X-API-Password"] == "pw"
    assert json.loads(req.content)["model"] == "gpt-oss-20b"


def test_status_reports_busy_gpu():
    def handler(request):
        return httpx.Response(200, json={"active": "qwen36-35b-a3b", "switch_locked": True,
                                         "queue_running": 1, "queue_waiting": 2})

    st = run(make_llm(handler).status())
    assert st["ok"] and not st["ready"] and st["busy"]
    assert (st["queue_running"], st["queue_waiting"]) == (1, 2)


def test_status_not_busy_when_our_model_active():
    def handler(request):
        return httpx.Response(200, json={"active": "gpt-oss-20b", "switch_locked": True})

    st = run(make_llm(handler).status())
    assert st["ready"] and not st["busy"]


def test_start_model_busy_maps_to_409(monkeypatch):
    def handler(request):
        return httpx.Response(409, json={"error": {"code": "model_busy", "message": "занята"}})

    llm = make_llm(handler)
    with pytest.raises(LLMBusy):
        run(llm.start_model())
    monkeypatch.setattr(server, "llm", llm)
    with TestClient(server.app) as c:
        r = c.post("/api/model/start")
    assert r.status_code == 409 and "очередь" in r.json()["detail"]


@pytest.mark.parametrize("status,code", [(503, "expired"), (504, "timeout"), (409, "no_active_model")])
def test_queue_errors_not_retried(status, code):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"error": {"code": code, "message": code}})

    with pytest.raises(LLMError):
        run(collect(make_llm(handler)))
    assert len(calls) == 1


def test_backend_502_retried():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(502, json={"error": {"code": "start_failed", "message": "упал"}})
        return httpx.Response(200, text=sse(text_chunks("ок")), headers={"content-type": "text/event-stream"})

    events = run(collect(make_llm(handler)))
    assert events[-1][0] == "done" and len(calls) == 2


def test_timeout_inside_stream_is_error():
    body = "data: " + json.dumps({"error": {"code": "timeout", "message": "Превышено время выполнения"}}) \
           + "\n\ndata: [DONE]\n\n"

    def handler(request):
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    with pytest.raises(LLMError, match="Превышено"):
        run(collect(make_llm(handler)))
