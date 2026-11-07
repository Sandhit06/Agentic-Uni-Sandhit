"""HTTP API over the simulated world. See docs/SIMULATOR.md for the contract."""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from .scenario import load_scenario
from .world import ApiError, World

app = FastAPI(title="Outcome-loop simulator", version="1.0")
_lock = threading.Lock()
_world: World | None = None


def init_world(scenario_path: str, run_dir: str) -> World:
    global _world
    _world = World(load_scenario(scenario_path), run_dir)
    return _world


def W() -> World:
    if _world is None:
        sc, rd = os.environ.get("SIM_SCENARIO"), os.environ.get("SIM_RUN_DIR")
        if not sc or not rd:
            raise HTTPException(500, "simulator not initialised")
        init_world(sc, rd)
    return _world


@app.exception_handler(ApiError)
def _api_error(_, e: ApiError):
    return JSONResponse(status_code=e.status, content={"error": e.code, "detail": e.detail})


def _call(fn, *a):
    with _lock:
        return fn(*a)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/sim/state")
def sim_state():
    return _call(W().state)


@app.post("/sim/advance")
def sim_advance(body: dict = Body(...)):
    return _call(W().advance, body.get("minutes"))


@app.get("/events/chunks")
def list_chunks(kind: str = Query("events", pattern="^(events|history)$"), after: int = -1):
    return [c for c in W().chunks if c["kind"] == kind and c["chunk"] > after]


@app.get("/events/chunks/{n}")
def get_chunk(n: int):
    w = W()
    if not 0 <= n < len(w.chunks):
        raise HTTPException(404, "no such chunk")
    return FileResponse(Path(w.run_dir) / w.chunks[n]["path"], media_type="application/x-ndjson")


@app.get("/workflows")
def workflows():
    w = W()
    return [_call(w.workflow_public, wid) for wid in w.wfs]


@app.get("/workflows/{wid}")
def workflow(wid: str):
    return _call(W().workflow_public, wid)


@app.get("/workflows/{wid}/config")
def get_config(wid: str):
    return _call(W().get_config, wid)


@app.get("/workflows/{wid}/config/history")
def config_history(wid: str):
    return _call(W().config_history, wid)


@app.put("/workflows/{wid}/config")
def auto_apply(wid: str, body: dict = Body(...)):
    return _call(W().auto_apply, wid, body)


@app.post("/approvals")
def request_approval(body: dict = Body(...)):
    return _call(W().request_approval, body)


@app.get("/approvals")
def list_approvals(workflow_id: str | None = None, status: str | None = None):
    return _call(W().list_approvals, workflow_id, status)


@app.get("/approvals/{aid}")
def get_approval(aid: str):
    return _call(W().get_approval, aid)


@app.post("/workflows/{wid}/apply")
def apply_approval(wid: str, body: dict = Body(...)):
    return _call(W().apply_approval, wid, body.get("approval_id"))


@app.post("/llm/suggest-fixes")
def suggest_fixes(body: dict = Body(...)):
    return _call(W().suggest_fixes, body)


@app.post("/validation/runs")
def validation(body: dict = Body(...)):
    return _call(W().run_validation, body)
