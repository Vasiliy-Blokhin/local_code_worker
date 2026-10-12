"""Асинхронный клиент AI Manager: стриминг chat/completions с инструментами + управление моделью.

Запросы проходят через очередь задач AI Manager: модель из поля "model" запускается сама,
когда до запроса дойдёт очередь; пока у других проектов есть задачи к другой модели,
переключение запрещено (ручной start → 409 model_busy), а наш запрос ждёт.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx

from . import config


class LLMError(Exception):
    pass


class LLMBusy(LLMError):
    """GPU занята задачами других проектов — модель нельзя переключить прямо сейчас."""


# Ошибки очереди AI Manager, которые не имеет смысла повторять сразу:
# 503 — не дождались очереди (expired), 504 — превышено время выполнения (timeout)
_RETRYABLE = ("HTTP 502",)


@dataclass
class Completion:
    """Собранный итог одного вызова модели."""
    content: str = ""
    reasoning: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)


class LLM:
    def __init__(self, base_url: str = config.AIM_URL, password: str = config.AIM_PASSWORD,
                 model: str = config.MODEL, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url
        self.model = model
        headers = {"X-Project": config.AIM_PROJECT}
        if password:
            headers["X-API-Password"] = password
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(None, connect=10.0),
            headers=headers,
            transport=transport,
        )

    async def aclose(self):
        await self._client.aclose()

    # ---------- управление моделью ----------
    async def status(self) -> dict:
        try:
            r = await self._client.get(f"{self.base_url}/api/status", timeout=10)
            if r.status_code == 401:
                return {"ok": False, "error": "AI Manager: неверный AIM_PASSWORD"}
            r.raise_for_status()
            st = r.json()
            ready = st.get("active") == self.model
            return {"ok": True, "active": st.get("active"), "model": self.model, "ready": ready,
                    # По активной (другой) модели есть задачи: переключение запрещено,
                    # наш запрос встанет в очередь и выполнится после них
                    "busy": bool(st.get("switch_locked")) and not ready,
                    "queue_running": st.get("queue_running", 0),
                    "queue_waiting": st.get("queue_waiting", 0)}
        except Exception as e:
            return {"ok": False, "error": f"AI Manager недоступен ({self.base_url}): {e}"}

    async def start_model(self) -> dict:
        r = await self._client.post(f"{self.base_url}/api/models/{self.model}/start", timeout=None)
        if r.status_code == 409 and '"model_busy"' in r.text:
            raise LLMBusy("GPU занята задачами другого проекта. Запрос к модели встанет в очередь "
                          "и выполнится после них — запускать вручную не нужно.")
        if r.status_code != 200:
            raise LLMError(f"Не удалось запустить модель: HTTP {r.status_code} {_error_text(r.text)}")
        return r.json()

    # ---------- чат ----------
    async def stream(self, messages: list[dict], tools: list[dict] | None = None,
                     **params) -> AsyncIterator[tuple[str, object]]:
        """
        Стрим ответа. Выдаёт события ("reasoning", str), ("text", str), ("tool", name)
        и последним — ("done", Completion). Ошибки соединения до первого токена
        повторяются (до 3 раз) — AI Manager мог перезапускать модель. Ошибки очереди
        (503 — не дождались, 504 — превышено время выполнения) и 4xx не повторяются.
        """
        payload = {"model": self.model, "messages": messages, "stream": True,
                   "stream_options": {"include_usage": True}, "cache_prompt": True, **params}
        if tools:
            payload["tools"] = tools
        for attempt in range(3):
            got_data = False
            try:
                async for ev in self._stream_once(payload):
                    got_data = True
                    yield ev
                return
            except (httpx.TransportError, LLMError) as e:
                retryable = isinstance(e, httpx.TransportError) or str(e).startswith(_RETRYABLE)
                if got_data or attempt == 2 or not retryable:
                    raise LLMError(str(e) or type(e).__name__) from e
                await asyncio.sleep(2 + attempt * 3)

    async def _stream_once(self, payload: dict):
        comp = Completion()
        calls: dict[int, dict] = {}
        async with self._client.stream("POST", f"{self.base_url}/v1/chat/completions",
                                       json=payload) as r:
            if r.status_code != 200:
                body = (await r.aread()).decode("utf-8", "replace")
                raise LLMError(f"HTTP {r.status_code}: {_error_text(body)}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if "error" in chunk:
                    raise LLMError(_error_text(json.dumps(chunk)))
                if chunk.get("usage"):
                    comp.usage = chunk["usage"]
                if chunk.get("timings"):
                    comp.timings = chunk["timings"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        comp.reasoning += delta["reasoning_content"]
                        yield "reasoning", delta["reasoning_content"]
                    if delta.get("content"):
                        comp.content += delta["content"]
                        yield "text", delta["content"]
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)),
                                                {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                            yield "tool", slot["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        comp.finish_reason = choice["finish_reason"]
        comp.tool_calls = [calls[i] for i in sorted(calls)]
        yield "done", comp

    async def complete(self, messages: list[dict], **params) -> Completion:
        """Нестриминговый вызов поверх stream (для сжатия контекста и т.п.)."""
        comp = Completion()
        async for kind, val in self.stream(messages, **params):
            if kind == "done":
                comp = val
        return comp


def _error_text(body: str) -> str:
    try:
        err = json.loads(body).get("error")
        if isinstance(err, dict):
            return err.get("message") or json.dumps(err, ensure_ascii=False)
        if err:
            return str(err)
    except (ValueError, AttributeError):
        pass
    return body[:500]
