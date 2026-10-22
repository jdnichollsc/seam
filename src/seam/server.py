"""Seam REST API + SSE + static web app. Binds to localhost by default; the browser and the MCP server
are both clients of this API, so jobs started from Claude show up in the web gallery live."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import PACKAGE_DIR, get_settings
from .engine import Engine, TransitionRequest, store_upload
from .jobs import JobStore

settings = get_settings()
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_ID = re.compile(r"^[0-9a-f]{16}\.png$")
engine: Engine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    settings.ensure_dirs()
    engine = Engine(settings, JobStore(settings.db_path))
    engine.start()
    yield


app = FastAPI(title="Seam", version="0.1.0", lifespan=lifespan)


def _engine() -> Engine:
    if engine is None:
        raise HTTPException(503, "engine not started")
    return engine


def _upload_path(upload_id: str) -> Path:
    if not UPLOAD_ID.match(upload_id):
        raise HTTPException(400, "invalid upload id")
    p = settings.uploads_dir / upload_id
    if not p.is_file():
        raise HTTPException(404, f"upload {upload_id} not found")
    return p


class TransitionIn(BaseModel):
    image_a: str = Field(description="upload id returned by POST /api/uploads")
    image_b: str
    preset: str
    seconds: int = 5
    seed: int | None = None
    prompt_extra: str = Field(default="", max_length=600)
    prompt_override: str = Field(default="", max_length=4000)
    a_desc: str | None = Field(default=None, max_length=400, description="omit: Seam reads the shot; '': generic")
    b_desc: str | None = Field(default=None, max_length=400)
    a_ref: str | None = Field(default=None, max_length=120)
    b_ref: str | None = Field(default=None, max_length=120)


class RefineIn(BaseModel):
    preset: str | None = None
    seed: int | None = None
    prompt_extra: str | None = Field(default=None, max_length=600)
    prompt_override: str | None = Field(default=None, max_length=4000)
    a_desc: str | None = Field(default=None, max_length=400, description="omit: Seam reads the shot; '': generic")
    b_desc: str | None = Field(default=None, max_length=400)
    a_ref: str | None = Field(default=None, max_length=120)
    b_ref: str | None = Field(default=None, max_length=120)


def _job_out(job: Any) -> dict[str, Any]:
    """One job shape for REST and live events (the web app replaces its copy with whichever arrives)."""
    d = job if isinstance(job, dict) else job.public()
    for k in ("image_a", "image_b"):
        d["params"][k] = Path(d["params"][k]).name  # never expose server paths
    for k in ("output", "poster", "sheet"):
        if d.get(k):
            d[f"{k}_url"] = f"/outputs/{d[k]}"
    return d


@app.get("/api/health")
def health() -> dict[str, Any]:
    e = _engine()
    return {"ok": True, "model": settings.model, "comfy_base_url": settings.comfy_base_url,
            "presets": len(e.exposed_presets())}


@app.get("/api/presets")
def presets() -> list[dict[str, Any]]:
    previews = PACKAGE_DIR / "static" / "previews"
    out = []
    for p in _engine().exposed_presets().values():
        item = {"id": p.id, "name": p.name, "category": p.category, "camera": p.camera,
                "model": p.model or settings.model}
        if (previews / f"{p.id}.mp4").exists():
            item["preview_url"] = f"/previews/{p.id}.mp4"
            item["poster_url"] = f"/previews/{p.id}.jpg"
        out.append(item)
    return out


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image larger than 25 MB")
    with tempfile.NamedTemporaryFile(delete=False, suffix=Path(file.filename or "x").suffix) as tmp:
        tmp.write(data)
    try:
        stored = store_upload(settings, Path(tmp.name))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"not a readable image: {e}") from e
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    from .media import image_size
    w, h = image_size(stored)
    return {"id": stored.name, "url": f"/uploads/{stored.name}", "width": w, "height": h}


@app.post("/api/transitions", status_code=201)
def create_transition(body: TransitionIn) -> dict[str, Any]:
    req = TransitionRequest(**{**body.model_dump(), "image_a": str(_upload_path(body.image_a)),
                               "image_b": str(_upload_path(body.image_b))})
    try:
        return _job_out(_engine().submit(req))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/describe/{upload_id}")
def describe_shot(upload_id: str) -> dict[str, Any]:
    """What Seam sees in one shot (local Gemma-4 E2B via the Comfy SDK); waits behind a render on the shared GPU."""
    try:
        text = _engine().describe(_upload_path(upload_id))
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"could not read the shot: {type(e).__name__}: {e}"[:300]) from e
    return {"desc": text.desc if text else "", "ref": text.ref if text else ""}


@app.get("/api/jobs")
def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    return [_job_out(j) for j in _engine().store.list(limit=min(limit, 200))]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _engine().store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return _job_out(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    try:
        return _job_out(_engine().cancel(job_id))
    except KeyError as e:
        raise HTTPException(404, "job not found") from e


@app.post("/api/jobs/{job_id}/refine", status_code=201)
def refine_job(job_id: str, body: RefineIn) -> dict[str, Any]:
    try:
        return _job_out(_engine().refine(job_id, body.model_dump()))
    except KeyError as e:
        raise HTTPException(404, "job not found") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/events")
async def events() -> StreamingResponse:
    store = _engine().store
    q = store.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    item = _job_out(await asyncio.to_thread(q.get, True, 15))
                    yield f"event: job\ndata: {json.dumps(item)}\n\n"
                except Exception:  # queue.Empty → keep-alive
                    yield ": ping\n\n"
        finally:
            store.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


app.mount("/outputs", StaticFiles(directory=settings.outputs_dir, check_dir=False), name="outputs")
app.mount("/uploads", StaticFiles(directory=settings.uploads_dir, check_dir=False), name="uploads")
_previews = PACKAGE_DIR / "static" / "previews"
if _previews.exists():
    app.mount("/previews", StaticFiles(directory=_previews), name="previews")
_web = PACKAGE_DIR / "static" / "web"
if _web.exists():
    app.mount("/", StaticFiles(directory=_web, html=True), name="web")
else:
    @app.get("/")
    def index() -> dict[str, str]:
        return {"message": "Seam API is running. Build the web app (see install.md) to get the UI."}


def run() -> None:
    import uvicorn

    settings.ensure_dirs()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
