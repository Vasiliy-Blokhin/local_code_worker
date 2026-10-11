"""
Сессии: история для модели (messages), элементы для UI (items) и поток операций.

UI получает снимок items + номер seq, затем по SSE — операции после seq:
  {"op": "add", "item": {...}}                 — новый элемент
  {"op": "append", "i": n, "key": k, "text": s} — дописать строку (стриминг)
  {"op": "set", "i": n, "data": {...}}          — обновить поля элемента
  {"op": "meta", "data": {...}}                 — состояние сессии (running, usage…)
Так переподключение (перезагрузка страницы) не теряет ни одного события.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from . import config

OPS_KEEP = 5000  # операций в памяти для догоняющих подписчиков


class Session:
    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.project_id: str = data["project_id"]
        self.title: str = data.get("title", "Новая сессия")
        self.created: float = data.get("created", time.time())
        self.updated: float = data.get("updated", self.created)
        self.system: str = data.get("system", "")           # фиксируется при создании (кэш префикса)
        self.messages: list[dict] = data.get("messages", [])
        self.items: list[dict] = data.get("items", [])
        self.checkpoints: list[dict] = data.get("checkpoints", [])  # [{turn, files:{path: text|None}}]
        self.turn: int = data.get("turn", 0)
        self.rewind_floor: int = data.get("rewind_floor", 0)        # ходы до сжатия не откатить
        self.settings: dict = data.get("settings", {"mode": "edits", "effort": config.REASONING})
        self.usage: dict = data.get("usage", {})
        self.seen: dict = {}            # дедупликация чтений (не сохраняется)
        # runtime
        self.ops: list[dict] = []
        self.seq: int = 0
        self.running: bool = False
        self.task: asyncio.Task | None = None
        self._event = asyncio.Event()
        self.pending: dict[str, asyncio.Future] = {}   # подтверждения инструментов

    # ---------- сохранение ----------
    @property
    def path(self) -> Path:
        return config.SESSIONS_DIR / f"{self.id}.json"

    def to_json(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "title": self.title,
                "created": self.created, "updated": self.updated, "system": self.system,
                "messages": self.messages, "items": self.items, "checkpoints": self.checkpoints,
                "turn": self.turn, "rewind_floor": self.rewind_floor,
                "settings": self.settings, "usage": self.usage}

    def save(self) -> None:
        self.updated = time.time()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def summary(self) -> dict:
        return {"id": self.id, "project_id": self.project_id, "title": self.title,
                "updated": self.updated, "running": self.running}

    def meta(self) -> dict:
        return {"running": self.running, "usage": self.usage, "settings": self.settings,
                "title": self.title, "context": config.CONTEXT}

    # ---------- события ----------
    def _push(self, op: dict) -> None:
        self.seq += 1
        op["seq"] = self.seq
        self.ops.append(op)
        if len(self.ops) > OPS_KEEP:
            del self.ops[: len(self.ops) - OPS_KEEP]
        # будим всех ждущих: они держат старый Event, новый — для следующих
        self._event.set()
        self._event = asyncio.Event()

    def add(self, item: dict) -> int:
        self.items.append(item)
        self._push({"op": "add", "item": item})
        return len(self.items) - 1

    def append(self, i: int, key: str, text: str) -> None:
        self.items[i][key] = self.items[i].get(key, "") + text
        self._push({"op": "append", "i": i, "key": key, "text": text})

    def set(self, i: int, **data) -> None:
        self.items[i].update(data)
        self._push({"op": "set", "i": i, "data": data})

    def push_meta(self) -> None:
        self._push({"op": "meta", "data": self.meta()})

    def reset_items(self) -> None:
        self._push({"op": "reset"})

    async def ops_after(self, seq: int, timeout: float = 15.0) -> list[dict]:
        """Операции после seq; ждёт новых до timeout (пустой список — keep-alive)."""
        if self.seq <= seq:
            try:
                await asyncio.wait_for(self._event.wait(), timeout)
            except asyncio.TimeoutError:
                return []
        if self.ops and self.ops[0]["seq"] > seq + 1:
            return [{"op": "resync", "seq": self.seq}]  # отстал сильнее буфера — перезагрузить снимок
        return [op for op in self.ops if op["seq"] > seq]


class Store:
    def __init__(self):
        self._cache: dict[str, Session] = {}

    def create(self, project_id: str, system: str, settings: dict | None = None) -> Session:
        s = Session({"id": uuid.uuid4().hex[:12], "project_id": project_id, "system": system})
        if settings:
            s.settings.update(settings)
        self._cache[s.id] = s
        s.save()
        return s

    def get(self, sid: str) -> Session:
        if sid not in self._cache:
            p = config.SESSIONS_DIR / f"{sid}.json"
            if not p.is_file():
                raise KeyError(sid)
            self._cache[sid] = Session(json.loads(p.read_text(encoding="utf-8")))
        return self._cache[sid]

    def list(self, project_id: str | None = None) -> list[dict]:
        out = []
        for p in config.SESSIONS_DIR.glob("*.json"):
            sid = p.stem
            if sid in self._cache:
                s = self._cache[sid].summary()
            else:
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                s = {"id": d["id"], "project_id": d["project_id"], "title": d.get("title", ""),
                     "updated": d.get("updated", 0), "running": False}
            if project_id is None or s["project_id"] == project_id:
                out.append(s)
        return sorted(out, key=lambda x: -x["updated"])

    def delete(self, sid: str) -> None:
        s = self._cache.pop(sid, None)
        if s and s.task:
            s.task.cancel()
        (config.SESSIONS_DIR / f"{sid}.json").unlink(missing_ok=True)


store = Store()
