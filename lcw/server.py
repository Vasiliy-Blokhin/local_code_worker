"""FastAPI: REST + SSE для веб-интерфейса агента."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, entities, prompts, workspace
from .agent import Agent, rewind, session_changes
from .llm import LLM, LLMBusy, LLMError
from .sessions import store
from .workspace import WorkspaceError

STATIC = config.ROOT / "static"
llm = LLM()
agent = Agent(llm)
TOKEN = hmac.new(b"lcw", config.PASSWORD.encode(), hashlib.sha256).hexdigest() if config.PASSWORD else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await llm.aclose()


app = FastAPI(title="local_code_worker", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def auth(request: Request, call_next):
    path = request.url.path
    if TOKEN and path.startswith("/api/") and path != "/api/login":
        if not hmac.compare_digest(request.cookies.get("lcw_token", ""), TOKEN):
            return JSONResponse({"detail": "auth"}, status_code=401)
    return await call_next(request)


@app.exception_handler(WorkspaceError)
async def ws_error(request: Request, exc: WorkspaceError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


def _session(sid: str):
    try:
        return store.get(sid)
    except KeyError:
        raise HTTPException(404, "Сессия не найдена")


# ---------------------------------------------------------------- общее
@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/login")
async def login(password: str = Body(..., embed=True)):
    if not TOKEN or hmac.compare_digest(password, config.PASSWORD):
        r = JSONResponse({"ok": True})
        if TOKEN:
            r.set_cookie("lcw_token", TOKEN, httponly=True, samesite="strict", max_age=90 * 86400)
        return r
    raise HTTPException(401, "Неверный пароль")


@app.get("/api/status")
async def status():
    st = await llm.status()
    return {**st, "context": config.CONTEXT, "auth": bool(TOKEN)}


@app.post("/api/model/start")
async def model_start():
    try:
        return await llm.start_model()
    except LLMBusy as e:
        raise HTTPException(409, str(e))
    except (LLMError, Exception) as e:
        raise HTTPException(502, str(e))


# -------------------------------------------------------------- проекты
@app.get("/api/projects")
async def projects():
    return workspace.list_projects()


@app.post("/api/projects")
async def project_add(path: str = Body("", embed=True), git_url: str = Body("", embed=True),
                      branch: str = Body("", embed=True)):
    return await asyncio.to_thread(workspace.add_project, path.strip(), git_url.strip(), branch.strip())


@app.delete("/api/projects/{pid}")
async def project_remove(pid: str):
    workspace.remove_project(pid)
    return {"ok": True}


@app.get("/api/projects/{pid}/files")
async def project_files(pid: str):
    p = workspace.get_project(pid)
    return await asyncio.to_thread(workspace.list_files, p["path"])


@app.get("/api/projects/{pid}/file")
async def project_file(pid: str, path: str):
    p = workspace.get_project(pid)
    f = workspace.resolve(p["path"], path)
    if not f.is_file():
        raise HTTPException(404, "Файл не найден")
    if not workspace.is_text_file(f):
        return {"path": path, "binary": True}
    data = f.read_bytes()[:1_000_000]
    return {"path": path, "content": data.decode("utf-8", "replace")}


@app.get("/api/projects/{pid}/git")
async def project_git(pid: str):
    p = workspace.get_project(pid)
    return await asyncio.to_thread(workspace.git_status, p["path"])


@app.get("/api/projects/{pid}/zip")
async def project_zip(pid: str):
    p = workspace.get_project(pid)
    data = await asyncio.to_thread(workspace.export_zip, p["path"])
    name = Path(p["path"]).name
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@app.get("/api/projects/{pid}/sessions")
async def project_sessions(pid: str):
    return store.list(pid)


# --------------------------------------------------------------- сессии
@app.post("/api/sessions")
async def session_create(project_id: str = Body(..., embed=True), settings: dict = Body(None, embed=True)):
    p = workspace.get_project(project_id)
    tree = await asyncio.to_thread(workspace.tree_summary, p["path"])
    modules = await asyncio.to_thread(entities.module_map, p["path"])
    name, text = workspace.read_instructions(p["path"])
    system = prompts.build_system(p["path"], tree, text, name, modules)
    s = store.create(project_id, system, settings)
    return {"id": s.id}


@app.get("/api/sessions/{sid}")
async def session_get(sid: str):
    s = _session(sid)
    return {"id": s.id, "project_id": s.project_id, "items": s.items, "seq": s.seq,
            "meta": s.meta(), "rewind_floor": s.rewind_floor}


@app.patch("/api/sessions/{sid}")
async def session_patch(sid: str, settings: dict = Body(None, embed=True), title: str = Body(None, embed=True)):
    s = _session(sid)
    if settings:
        allowed = {"mode": {"ask", "edits", "auto"}, "effort": {"low", "medium", "high"}}
        for k, v in settings.items():
            if k in allowed and v in allowed[k]:
                s.settings[k] = v
                if k == "mode":
                    s.settings["always"] = []
    if title:
        s.title = title[:100]
    s.save()
    s.push_meta()
    return s.meta()


@app.delete("/api/sessions/{sid}")
async def session_delete(sid: str):
    store.delete(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/message")
async def session_message(sid: str, text: str = Body(..., embed=True)):
    s = _session(sid)
    if s.running:
        raise HTTPException(409, "Агент уже работает в этой сессии")
    if not text.strip():
        raise HTTPException(400, "Пустое сообщение")
    s.running = True  # сразу, чтобы второй запрос не проскочил
    s.task = asyncio.create_task(agent.run_turn(s, text))
    return {"ok": True}


@app.post("/api/sessions/{sid}/stop")
async def session_stop(sid: str):
    s = _session(sid)
    if s.task and not s.task.done():
        s.task.cancel()
    return {"ok": True}


@app.post("/api/sessions/{sid}/approve")
async def session_approve(sid: str, id: str = Body(..., embed=True), decision: str = Body(..., embed=True),
                          note: str = Body("", embed=True)):
    s = _session(sid)
    fut = s.pending.get(id)
    if not fut or fut.done():
        raise HTTPException(404, "Нет ожидающего подтверждения")
    if decision not in ("allow", "deny", "always"):
        raise HTTPException(400, "decision: allow | deny | always")
    fut.set_result((decision, note))
    return {"ok": True}


@app.post("/api/sessions/{sid}/rewind")
async def session_rewind(sid: str, turn: int = Body(..., embed=True)):
    s = _session(sid)
    try:
        files = rewind(s, turn)  # в цикле событий: rewind публикует операции для UI
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"restored": files}


@app.get("/api/sessions/{sid}/changes")
async def session_changes_api(sid: str):
    s = _session(sid)
    return await asyncio.to_thread(session_changes, s)


@app.get("/api/sessions/{sid}/events")
async def session_events(sid: str, request: Request, after: int = 0):
    s = _session(sid)

    async def gen():
        seq = after
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            ops = await s.ops_after(seq)
            if not ops:
                yield ": ping\n\n"
                continue
            for op in ops:
                seq = op["seq"]
                yield f"data: {json.dumps(op, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
