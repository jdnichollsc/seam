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
from . import backend as backends
from . import models as model_status
from .config import PACKAGE_DIR, get_settings
from .engine import Engine, SequenceBusy, TransitionRequest, store_clip, store_upload
from .jobs import JobStore

settings = get_settings()
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
UPLOAD_ID = re.compile(r"^[0-9a-f]{16}\.png$")
MAX_CLIP_BYTES = 40 * 1024 * 1024
CLIP_ID = re.compile(r"^[0-9a-f]{16}\.mp4$")
SEQ_ID = re.compile(r"^s[0-9a-f]{11}$")
VIDEO_TYPES = ("video/mp4", "video/quicktime", "video/webm")
engine: Engine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    settings.ensure_dirs()
    if backends.current(settings)[0] == backends.CLOUD and not _is_loopback(settings.host):
        # no authentication here: anyone who can reach this server could spend the key's credits
        raise RuntimeError(f"Comfy Cloud needs Seam on localhost: SEAM_HOST is {settings.host}. Set SEAM_HOST=127.0.0.1, "
                           "or run `seam backend local`")
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
    model: str | None = Field(default=None, description="id from GET /api/models; omit or '': the effect's own model")


class RefineIn(BaseModel):
    preset: str | None = None
    model: str | None = Field(default=None, description="id from GET /api/models; omit or '': the effect's own model")
    seconds: int | None = None
    seed: int | None = None
    prompt_extra: str | None = Field(default=None, max_length=600)
    prompt_override: str | None = Field(default=None, max_length=4000)
    a_desc: str | None = Field(default=None, max_length=400, description="omit: Seam reads the shot; '': generic")
    b_desc: str | None = Field(default=None, max_length=400)
    a_ref: str | None = Field(default=None, max_length=120)
    b_ref: str | None = Field(default=None, max_length=120)


class ShotIn(BaseModel):
    """One shot of a sequence: an image upload id, or a clip (`first`/`last` from POST /api/uploads?role=mid)."""
    image: str | None = Field(default=None, description="image upload id")
    first: str | None = Field(default=None, description="clip's first-frame upload id (role=mid)")
    last: str | None = Field(default=None, description="clip's last-frame upload id (role=mid)")
    clip: str | None = Field(default=None, description="clip id: plays in full at this point of the sequence")


class SegmentIn(BaseModel):
    """The transition from one shot to the next: same fields as a transition, minus the shots."""
    preset: str
    model: str | None = Field(default=None, description="id from GET /api/models; omit or '': the effect's own model")
    seconds: int = 5
    seed: int | None = None
    prompt_extra: str = Field(default="", max_length=600)
    a_desc: str | None = Field(default=None, max_length=400)
    b_desc: str | None = Field(default=None, max_length=400)
    a_ref: str | None = Field(default=None, max_length=120)
    b_ref: str | None = Field(default=None, max_length=120)


class GrowIn(BaseModel):
    """One more shot and the transition into it."""
    shot: ShotIn
    segment: SegmentIn
    story: str = Field(default="", max_length=300, description="the idea of the whole sequence, shared by every transition")  # continue only: the new sequence's story


class SequenceIn(BaseModel):
    shots: list[ShotIn] = Field(min_length=2, max_length=8)
    segments: list[SegmentIn] = Field(description="one per hand-off: len(shots) - 1")
    story: str = Field(default="", max_length=300, description="the idea of the whole sequence, shared by every transition")


class EditIn(BaseModel):
    """Remove / reorder shots and set the story in one step. Shots and transitions are numbered from 1."""
    order: list[int] = Field(min_length=2, max_length=8, description="current shot numbers in their new order; "
                             "leave one out to remove it")
    transitions: dict[int, SegmentIn] = Field(default_factory=dict, description="transition number in the NEW order "
                                              "→ its effect; required for every hand-off whose shots were not already "
                                              "adjacent in that direction, optional (a new take) for the others")
    story: str | None = Field(default=None, max_length=300, description="omit to keep; '' clears it")
    expected_updated_at: float | None = Field(default=None, description="the sequence's updated_at you edited; a "
                                              "newer sequence answers 409")


def _shot_paths(shot: ShotIn) -> dict[str, str | None]:
    if shot.clip:
        if not (shot.first and shot.last):
            raise HTTPException(422, "a clip shot needs first and last (upload it with role=mid)")
        return {"first": str(_upload_path(shot.first)), "last": str(_upload_path(shot.last)),
                "clip": _clip_path(shot.clip)}
    image = shot.image or shot.first or shot.last
    if not image:
        raise HTTPException(422, "each shot needs an image or a clip")
    path = str(_upload_path(image))
    return {"first": path, "last": path, "clip": None}


def _seq_out(seq: Any, jobs: bool = True) -> dict[str, Any]:
    """One sequence shape for REST and live events: file names and URLs only, plus its current takes."""
    d = seq if isinstance(seq, dict) else seq.public()
    d["shots"] = [{"first": Path(s["first"]).name, "last": Path(s["last"]).name,
                   "first_url": f"/uploads/{Path(s['first']).name}", "last_url": f"/uploads/{Path(s['last']).name}",
                   **({"clip": Path(s["clip"]).name, "clip_url": f"/uploads/{Path(s['clip']).name}"}
                      if s.get("clip") else {})} for s in d["shots"]]
    for k in ("output", "poster", "sheet"):
        if d.get(k):
            d[f"{k}_url"] = f"/outputs/{d[k]}"
    if jobs:
        store = _engine().store
        d["takes"] = [_job_out(j) if (j := store.get(i)) else None for i in d["segments"]]
    return d


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
            "backend": backends.current(settings)[0], "presets": len(e.exposed_presets())}


def _backend_writes_allowed() -> None:
    if not _is_loopback(settings.host):
        raise HTTPException(403, "Comfy Cloud settings are only available when Seam runs on localhost")


class BackendChoice(BaseModel):
    backend: str


class CloudKey(BaseModel):
    api_key: str


@app.get("/api/backend")
def get_backend() -> dict[str, Any]:
    """Where takes render, and whether a Comfy Cloud key is set (never the key)."""
    return backends.state(settings)


@app.put("/api/backend")
def put_backend(body: BackendChoice) -> dict[str, Any]:
    _backend_writes_allowed()
    locked = backends.locked_by_env(settings)
    if locked:
        raise HTTPException(409, f"{locked} decides where Seam renders; change it and restart")
    try:
        backends.choose(settings, body.backend)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return backends.state(settings)


@app.put("/api/backend/key")
def put_cloud_key(body: CloudKey) -> dict[str, Any]:
    """Checked with a free read before it is saved: a refused key writes nothing."""
    _backend_writes_allowed()
    if not body.api_key.strip():
        raise HTTPException(422, "paste a Comfy Cloud API key")
    try:
        backends.verify_key(body.api_key)
    except backends.InvalidKey as e:
        raise HTTPException(422, str(e)) from None
    backends.save_key(settings, body.api_key)
    return backends.state(settings)


@app.delete("/api/backend/key")
def delete_cloud_key() -> dict[str, Any]:
    _backend_writes_allowed()
    active = _engine().active_cloud_jobs()
    if active:
        raise HTTPException(409, f"{len(active)} take(s) are still on Comfy Cloud; wait for them or cancel them first")
    backends.clear_key(settings)
    return backends.state(settings)


@app.get("/api/models")
def list_models(refresh: bool = False) -> dict[str, Any]:
    """The models a transition can pick, and whether each can render on the current backend. Downloads nothing."""
    backend = backends.current(settings)[0]
    return {"backend": backend, "models": model_status.listing(settings, _engine().catalog(), backend, refresh)}


@app.get("/api/presets")
def presets() -> list[dict[str, Any]]:
    previews = PACKAGE_DIR / "static" / "previews"
    out = []
    for p in _engine().exposed_presets().values():
        item = {"id": p.id, "name": p.name, "category": p.category, "camera": p.camera,
                "model": p.model or settings.model, "seconds": list(p.seconds)}
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
    if role not in ("a", "b", "mid"):
        raise HTTPException(400, "role must be a, b or mid")
    data = await file.read(MAX_CLIP_BYTES + 1)
    if len(data) > MAX_CLIP_BYTES:
        raise HTTPException(413, "clip larger than 40 MB")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"clip{Path(file.filename or 'x.mp4').suffix or '.mp4'}"
        src.write_bytes(data)
        try:
            clip, seconds = await asyncio.to_thread(store_clip, settings, src)
            ends = {}
            for end in (("first", "last") if role == "mid" else ("last",) if role == "a" else ("first",)):
                frame = await asyncio.to_thread(media.clip_frame, clip, Path(tmp) / f"{end}.png", end == "last")
                ends[end] = store_upload(settings, frame)
            stored = ends.get("last") or ends["first"]  # a: the frame the transition leaves from; b: lands on
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"not a readable video: {e}"[:300]) from e
    w, h = media.image_size(stored)
    out = {"id": stored.name, "url": f"/uploads/{stored.name}", "width": w, "height": h,
           "clip": clip.name, "clip_url": f"/uploads/{clip.name}", "clip_seconds": round(seconds, 2)}
    if role == "mid":  # a clip in the middle of a sequence: landed on at its first frame, left from its last
        out.update(first=ends["first"].name, first_url=f"/uploads/{ends['first'].name}",
                   last=ends["last"].name, last_url=f"/uploads/{ends['last'].name}")
    return out


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
        path = _upload_path(upload_id)
        if backends.current(settings)[0] == backends.CLOUD:
            # adding a shot never spends credits: the take reads its own shots once the user presses Make
            known, text = _engine().cached_description(path)
            if not known:
                return {"desc": "", "ref": "", "deferred": True}
        else:
            text = _engine().describe(path)
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
def estimate(seconds: int = 5, model: str | None = None) -> dict[str, Any]:
    """Typical render time for a take of this length where Seam renders now: median of the last succeeded ones."""
    e = _engine()
    here = backends.current(settings)[0]
    times = [j.wall_s for j in e.store.list(limit=100) if j.status == "succeeded" and j.wall_s
             and j.params.get("seconds", 5) == seconds and e.backend_of(j) == here
             and (not model or j.params.get("model") == model)][:10]
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


def _sequence(seq_id: str):
    if not SEQ_ID.match(seq_id):
        raise HTTPException(400, "invalid sequence id")
    seq = _engine().store.get_sequence(seq_id)
    if seq is None:
        raise HTTPException(404, "sequence not found")
    return seq


@app.post("/api/sequences", status_code=201)
def create_sequence(body: SequenceIn) -> dict[str, Any]:
    """Shots S0..Sn joined by n transitions, one per hand-off; the result is one video (clips play in full)."""
    shots = [_shot_paths(s) for s in body.shots]
    try:
        return _seq_out(_engine().create_sequence(shots, [s.model_dump() for s in body.segments], body.story))
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/api/sequences")
def list_sequences(limit: int = 20) -> list[dict[str, Any]]:
    return [_seq_out(s) for s in _engine().store.list_sequences(limit=min(limit, 100))]


@app.get("/api/sequences/{seq_id}")
def get_sequence(seq_id: str) -> dict[str, Any]:
    return _seq_out(_sequence(seq_id))


@app.post("/api/sequences/{seq_id}/segments/{index}/refine", status_code=201)
def refine_segment(seq_id: str, index: int, body: RefineIn) -> dict[str, Any]:
    """A new take of segment `index` (1-based); the other takes are reused and the sequence joins again."""
    _sequence(seq_id)
    try:
        return _seq_out(_engine().refine_segment(seq_id, index - 1, body.model_dump()))
    except SequenceBusy as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/sequences/{seq_id}/edit")
def edit_sequence(seq_id: str, body: EditIn) -> dict[str, Any]:
    """Remove and reorder shots and set the story. Hand-offs that stay adjacent keep their takes; only new ones render."""
    _sequence(seq_id)
    try:
        return _seq_out(_engine().edit_sequence(
            seq_id, [i - 1 for i in body.order], {k - 1: v.model_dump() for k, v in body.transitions.items()},
            body.story, body.expected_updated_at))
    except SequenceBusy as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/sequences/{seq_id}/shots", status_code=201)
def append_shot(seq_id: str, body: GrowIn) -> dict[str, Any]:
    """Grow a finished sequence by one shot: only the new transition renders, then the video joins again."""
    _sequence(seq_id)
    shot = _shot_paths(body.shot)
    try:
        return _seq_out(_engine().append_shot(seq_id, shot, body.segment.model_dump()))
    except SequenceBusy as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/jobs/{job_id}/continue", status_code=201)
def continue_take(job_id: str, body: GrowIn) -> dict[str, Any]:
    """Turn a finished two-shot take into a sequence (that take is its first transition) and add the next shot."""
    shot = _shot_paths(body.shot)
    try:
        return _seq_out(_engine().continue_take(job_id, shot, body.segment.model_dump(), body.story))
    except KeyError as e:
        raise HTTPException(404, "job not found") from e
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/sequences/{seq_id}/cancel")
def cancel_sequence(seq_id: str) -> dict[str, Any]:
    _sequence(seq_id)
    return _seq_out(_engine().cancel_sequence(seq_id))


@app.post("/api/sequences/{seq_id}/assemble")
def assemble_sequence(seq_id: str) -> dict[str, Any]:
    """Join again from the current takes (after a failed join)."""
    _sequence(seq_id)
    try:
        return _seq_out(_engine().assemble(seq_id))
    except SequenceBusy as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/sequences/{seq_id}/receipt")
def sequence_receipt(seq_id: str) -> dict[str, Any]:
    """The ordered join: clips by sha256 and each take with its own receipt (each take's graph is in its bundle)."""
    seq = _sequence(seq_id)
    if seq.status != "succeeded":
        raise HTTPException(409, f"sequence {seq_id} is {seq.status}")
    return _engine().sequence_receipt(seq_id)


@app.get("/api/events")
async def events() -> StreamingResponse:
    store = _engine().store
    q = store.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    kind, item = await asyncio.to_thread(q.get, True, 15)
                    item = _seq_out(item, jobs=False) if kind == "sequence" else _job_out(item)
                    yield f"event: {kind}\ndata: {json.dumps(item)}\n\n"
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
