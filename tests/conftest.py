"""Общие фикстуры: изолированная папка данных и мок OpenAI-совместимого сервера."""
import json
import os
import sys
import tempfile
from pathlib import Path

_DATA = tempfile.mkdtemp(prefix="lcw_test_")
os.environ["LCW_DATA_DIR"] = _DATA
os.environ["LCW_PASSWORD"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import pytest  # noqa: E402


def sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def tool_call_chunks(name, args, call_id="c1", reasoning="думаю"):
    a = json.dumps(args)
    return [
        {"choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": call_id, "type": "function",
                                                             "function": {"name": name, "arguments": a[:5]}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": a[5:]}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
         "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
         "timings": {"prompt_per_second": 900, "predicted_per_second": 30, "cache_n": 800}},
    ]


def text_chunks(text):
    return [
        {"choices": [{"index": 0, "delta": {"content": text[:3]}}]},
        {"choices": [{"index": 0, "delta": {"content": text[3:]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 1200, "completion_tokens": 20}},
    ]


class MockModel:
    """Отдаёт заранее заданные ответы по очереди и запоминает запросы."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/status":
            return httpx.Response(200, json={"active": "gpt-oss-20b"})
        body = json.loads(request.content)
        self.requests.append(body)
        chunks = self.script.pop(0) if self.script else text_chunks("готово")
        return httpx.Response(200, text=sse(chunks), headers={"content-type": "text/event-stream"})

    def transport(self):
        return httpx.MockTransport(self.handler)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "util.py").write_text("X = 1\r\nY = 2\r\n", encoding="utf-8", newline="")
    from lcw import workspace
    return workspace.add_project(str(tmp_path))


def plan_chunks(*texts, status="todo", call_id="p1"):
    """Вызов plan — агент требует план перед изменениями."""
    steps = [{"text": t, "status": status} for t in (texts or ("шаг", "проверка"))]
    return tool_call_chunks("plan", {"steps": steps}, call_id, reasoning="план")
