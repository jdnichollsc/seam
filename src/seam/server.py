"""Seam REST API + SSE + static web app. Binds to localhost by default; the browser and the MCP server
are both clients of this API, so jobs started from Claude show up in the web gallery live."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import ipaddress
import statistics
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import media, receipt
from .config import PACKAGE_DIR, get_settings
from .engine import Engine, TransitionRequest, store_clip, store_upload
from .jobs import JobStore

settings = get_settings()
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_ID = re.compile(r"^[0-9a-f]{16}\.png$")
MAX_CLIP_BYTES = 40 * 1024 * 1024
CLIP_ID = re.compile(r"^[0-9a-f]{16}\.mp4$")
VIDEO_TYPES = ("video/mp4", "video/quicktime", "video/webm")
engine: Engine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    settings.ensure_dirs()
    engine = Engine(settings, JobStore(settings.db_path))
    engine.start()
    yield


app = FastAPI(title="Seam", version="0.1.0", lifespan=lifespan)


def _is_loopback(host: str | None) -> bool:
    host = (host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _host_name(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "").lower()


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Bound to loopback, Seam only answers requests addressed to loopback (blocks DNS rebinding) and only
    accepts writes from loopback pages (blocks other sites in the same browser from queuing renders)."""
    if _is_loopback(settings.host):
        if not _is_loopback(_host_name(request.headers.get("host", ""))):
            return JSONResponse({"detail": "Seam only answers on localhost"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and not _is_loopback(urlsplit(origin).hostname):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


def _engine() -> Engine:
    if engine is None:
        raise HTTPException(503, "engine not started")
    return engine


def _clip_path(clip_id: str | None) -> str | None:
    if not clip_id:
        return None
    if not CLIP_ID.match(clip_id) or not (settings.uploads_dir / clip_id).is_file():
        raise HTTPException(404, f"clip {clip_id} not found")
    return str(settings.uploads_dir / clip_id)


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
    clip_a: str | None = Field(default=None, description="clip id from POST /api/uploads?role=a: plays before the transition")
    clip_b: str | None = Field(default=None, description="clip id from POST /api/uploads?role=b: plays after it")


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
    for k in ("image_a", "image_b", "clip_a", "clip_b"):
        if d["params"].get(k):
            d["params"][k] = Path(d["params"][k]).name  # never expose server paths
    if d.get("extra", {}).get("merged"):
        d["merged_url"] = f"/outputs/{d['extra']['merged']}"
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


# Fictional, SFW test pairs (dev/fixtures) shipped with the app so a first run needs no images of your own.
EXAMPLES = {
    "p1": {"title": "Street to beach", "preset": "smoke"},
    "p2": {"title": "Sneaker to cup", "preset": "whip_pan"},
    "p6": {"title": "Perfume, marble to sea", "preset": "water_bend"},
}
EXAMPLES_DIR = PACKAGE_DIR / "static" / "examples"


@app.get("/api/examples")
def examples() -> list[dict[str, Any]]:
    return [{"id": k, **v, "a_url": f"/examples/{k}_a.jpg", "b_url": f"/examples/{k}_b.jpg"}
            for k, v in EXAMPLES.items() if (EXAMPLES_DIR / f"{k}_a.jpg").is_file()]


@app.post("/api/examples/{example_id}")
def use_example(example_id: str) -> dict[str, Any]:
    """Both shots of an example as uploads, exactly as if the user had dropped them."""
    if example_id not in EXAMPLES or not (EXAMPLES_DIR / f"{example_id}_a.jpg").is_file():
        raise HTTPException(404, "unknown example")
    from .media import image_size

    out: dict[str, Any] = {"preset": EXAMPLES[example_id]["preset"]}
    for side in ("a", "b"):
        stored = store_upload(settings, EXAMPLES_DIR / f"{example_id}_{side}.jpg")
        w, h = image_size(stored)
        out[side] = {"id": stored.name, "url": f"/uploads/{stored.name}", "width": w, "height": h}
    return out


@app.post("/api/uploads")
async def upload(file: UploadFile = File(...), role: str = "a") -> dict[str, Any]:
    """An image, or a short clip (role a: the transition starts from its last frame; role b: lands on its first)."""
    if (file.content_type or "") in VIDEO_TYPES:
        return await _upload_clip(file, role)
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


async def _upload_clip(file: UploadFile, role: str) -> dict[str, Any]:
    if role not in ("a", "b"):
        raise HTTPException(400, "role must be a or b")
    data = await file.read(MAX_CLIP_BYTES + 1)
    if len(data) > MAX_CLIP_BYTES:
        raise HTTPException(413, "clip larger than 40 MB")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"clip{Path(file.filename or 'x.mp4').suffix or '.mp4'}"
        src.write_bytes(data)
        try:
            clip, seconds = await asyncio.to_thread(store_clip, settings, src)
            frame = await asyncio.to_thread(media.clip_frame, clip, Path(tmp) / "frame.png", role == "a")
            stored = store_upload(settings, frame)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"not a readable video: {e}"[:300]) from e
    w, h = media.image_size(stored)
    return {"id": stored.name, "url": f"/uploads/{stored.name}", "width": w, "height": h,
            "clip": clip.name, "clip_url": f"/uploads/{clip.name}", "clip_seconds": round(seconds, 2)}


@app.post("/api/transitions", status_code=201)
def create_transition(body: TransitionIn) -> dict[str, Any]:
    req = TransitionRequest(**{**body.model_dump(), "image_a": str(_upload_path(body.image_a)),
                               "image_b": str(_upload_path(body.image_b)),
                               "clip_a": _clip_path(body.clip_a), "clip_b": _clip_path(body.clip_b)})
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


@app.get("/api/jobs/{job_id}/review-sheet")
async def review_sheet(job_id: str) -> FileResponse:
    """Two-row contact sheet for judging a take (whole clip + its middle), built on first request and cached,
    so takes rendered before this existed get one too."""
    job = _engine().store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != "succeeded" or not job.output:
        raise HTTPException(409, f"job {job_id} is {job.status}, no preview yet")
    dst = settings.outputs_dir / f"{job.id}_sheet2.png"
    if not dst.is_file():
        await asyncio.to_thread(_build_once, settings.outputs_dir / job.output, dst)
    return FileResponse(dst, media_type="image/png")


_sheet_lock = threading.Lock()


def _build_once(video: Path, dst: Path) -> None:
    """Concurrent cold requests build the sheet once; later ones find it on disk."""
    with _sheet_lock:
        if not dst.is_file():
            media.review_sheet(video, dst)


def _finished(job_id: str):
    job = _engine().store.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != "succeeded" or not job.output:
        raise HTTPException(409, f"job {job_id} is {job.status}")
    return job


@app.get("/api/jobs/{job_id}/receipt")
def job_receipt(job_id: str) -> dict[str, Any]:
    """How a take was made: input hashes, effect, model, seed, the prompt Seam sent, graph file and hash."""
    job = _finished(job_id)
    e = _engine()
    return receipt.build_receipt(job, e.spec(job.params.get("model") or e.settings.model), settings.outputs_dir)


@app.get("/api/jobs/{job_id}/comfyui.zip")
def job_comfy_bundle(job_id: str) -> Response:
    """The take as a plain ComfyUI workflow (API format) + its two stills + receipt: open it in ComfyUI."""
    job = _finished(job_id)
    e = _engine()
    data = receipt.comfy_bundle(job, e.spec(job.params.get("model") or e.settings.model), settings.outputs_dir)
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="seam_{job.id}_comfyui.zip"'})


@app.get("/api/estimate")
def estimate() -> dict[str, Any]:
    """Typical render time on this machine: median of the last succeeded takes (None until there is one)."""
    times = [j.wall_s for j in _engine().store.list(limit=50) if j.status == "succeeded" and j.wall_s][:10]
    return {"seconds": round(statistics.median(times)) if times else None, "samples": len(times)}


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
if EXAMPLES_DIR.exists():
    app.mount("/examples", StaticFiles(directory=EXAMPLES_DIR), name="examples")
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
