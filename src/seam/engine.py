"""Transition engine: request → comfy-sdk job → MP4 + poster + contact sheet, one job at a time."""

from __future__ import annotations

import json
import os
import queue
import random
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comfy_sdk import Comfy, Progress, StatusChange

from . import describe as shots
from . import media, receipt
from .config import Settings
from .describe import ShotText
from .jobs import JobRecord, JobStore
from .presets import Preset, load_presets, render_prompt
from .workflows import WorkflowSpec, build, load_spec

SUBMIT_UNKNOWN = ("server restarted while sending the job to ComfyUI; it may still have rendered there "
                  "(check ComfyUI's queue/output). Use New take to render it again.")
SECONDS_CHOICES = (5, 8, 10)  # what the graphs render; each launch effect lists the lengths it is qualified at
PUBLIC_MODELS = ("l25", "l23")  # LTX family only: other models were research-only and never ship


@dataclass
class TransitionRequest:
    image_a: str  # paths of normalized uploads inside settings.uploads_dir
    image_b: str
    preset: str
    seconds: int = 5
    seed: int | None = None
    prompt_extra: str = ""      # appended to the preset text
    prompt_override: str = ""   # replaces the rendered prompt entirely (agents / power users)
    a_desc: str | None = None   # shot descriptions: None → Seam reads the shot itself, "" → generic prompt
    b_desc: str | None = None
    a_ref: str | None = None    # short labels used inside the effect text ("the woman in the red jacket")
    b_ref: str | None = None
    enhance: bool = True        # in-graph prompt enhancer (open, local)
    model: str | None = None    # override the preset's model (l25 | l23)
    clip_a: str | None = None   # optional clips around the transition: image_a is clip_a's last frame,
    clip_b: str | None = None   # image_b is clip_b's first frame, and the result is clip A + transition + clip B

    def to_params(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Engine:
    def __init__(self, settings: Settings, store: JobStore, client: Comfy | None = None) -> None:
        self.settings = settings
        self.store = store
        if client is None:
            os.environ.setdefault("COMFY_BASE_URL", settings.comfy_base_url)  # the SDK reads its endpoint from env
            client = Comfy(client_info="seam/0.1")
        self.client = client
        self._specs: dict[str, WorkflowSpec] = {}
        self.presets: dict[str, Preset] = load_presets(settings.presets_file)
        self._queue: queue.Queue[str] = queue.Queue()
        self._handles: dict[str, Any] = {}
        self._worker = threading.Thread(target=self._run, daemon=True, name="seam-engine")

    # -- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        self.settings.ensure_dirs()
        for job in self.store.list(limit=10_000):
            if job.status in ("queued", "running"):
                if job.comfy_job_id:  # re-attach instead of resubmitting
                    self._queue.put(job.id)
                elif job.extra.get("submitting_at"):
                    # died between sending the graph and recording its id: ComfyUI may still render it,
                    # and resubmitting could render it twice, so say so instead of guessing
                    self.store.update(job.id, status="interrupted", error=SUBMIT_UNKNOWN)
                else:
                    self.store.update(job.id, status="interrupted", error="server restarted before submission")
        self._worker.start()

    # -- models ---------------------------------------------------------------------------------
    @staticmethod
    def enabled_models() -> tuple[str, ...]:
        return PUBLIC_MODELS

    @staticmethod
    def exposed_statuses() -> tuple[str, ...]:
        """Only qualified presets ship; candidates are visible on a dev machine with SEAM_SHOW_CANDIDATES=1."""
        return ("launch", "candidate") if os.environ.get("SEAM_SHOW_CANDIDATES") == "1" else ("launch",)

    def exposed_presets(self) -> dict[str, Preset]:
        return {k: p for k, p in self.presets.items() if p.status in self.exposed_statuses()}

    def spec(self, model: str) -> WorkflowSpec:
        if model not in self._specs:
            self._specs[model] = load_spec(self.settings.workflows_dir, model)
        return self._specs[model]

    def model_for(self, req: TransitionRequest) -> str:
        return req.model or self.presets[req.preset].model or self.settings.model

    # -- API ------------------------------------------------------------------------------------
    def validate(self, req: TransitionRequest) -> None:
        if req.preset not in self.exposed_presets():
            raise ValueError(f"unknown preset {req.preset!r}; choose one of {sorted(self.exposed_presets())}")
        if self.model_for(req) not in self.enabled_models():
            raise ValueError(f"model {self.model_for(req)!r} is not enabled; available: {self.enabled_models()}")
        preset = self.presets[req.preset]
        if preset.status == "launch" and preset.model and self.model_for(req) != preset.model:
            # qualification was judged on one model; another model renders an unreviewed effect
            raise ValueError(f"effect {req.preset!r} is qualified on model {preset.model!r} only")
        if req.seconds not in SECONDS_CHOICES:
            raise ValueError(f"seconds must be one of {SECONDS_CHOICES}")
        if preset.status == "launch" and req.seconds not in preset.seconds:
            # each length is qualified on its own: a longer take paces the effect differently
            raise ValueError(f"effect {req.preset!r} is qualified at {', '.join(map(str, preset.seconds))} s only")
        for p in (req.image_a, req.image_b, req.clip_a, req.clip_b):
            if p and not Path(p).is_file():
                raise ValueError(f"file not found: {Path(p).name}")

    def submit(self, req: TransitionRequest, parent_id: str | None = None) -> JobRecord:
        if req.seed is None:
            req.seed = random.randint(1, 2**31 - 1)
        self.validate(req)
        req.model = self.model_for(req)  # pin the resolved model on the job
        job = self.store.create(req.to_params(), parent_id=parent_id)
        self._queue.put(job.id)
        return job

    def refine(self, job_id: str, changes: dict[str, Any]) -> JobRecord:
        base = self.store.get(job_id)
        if base is None:
            raise KeyError(job_id)
        params = {**base.params, **{k: v for k, v in changes.items() if v is not None}}
        if changes.get("preset") and not changes.get("model"):
            params["model"] = None  # a new preset brings its own preferred model
        new = self.presets.get(params["preset"])
        if changes.get("preset") and changes.get("seconds") is None and new and new.status == "launch" \
                and params.get("seconds", 5) not in new.seconds:
            params["seconds"] = new.seconds[0]  # the new effect is not qualified at the old length
        if changes.get("seed") is None:
            params["seed"] = None  # a refinement is a new take unless the caller pins the seed
        for side in ("a", "b"):  # a corrected description drops the old label, which may contradict it
            if changes.get(f"{side}_desc") is not None and changes.get(f"{side}_ref") is None:
                params[f"{side}_ref"] = ""
        return self.submit(TransitionRequest(**params), parent_id=job_id)

    def cancel(self, job_id: str) -> JobRecord:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status == "queued":
            return self.store.update(job_id, status="cancelled")
        handle = self._handles.get(job_id)
        if handle is not None:
            handle.cancel()
        return self.store.update(job_id, status="cancelled")

    def prompt_for(self, req: TransitionRequest) -> str:
        if req.prompt_override.strip():
            return req.prompt_override.strip()
        return render_prompt(self.model_for(req), self.presets[req.preset],
                             a_ref=req.a_ref or "the first scene", b_ref=req.b_ref or "the second scene",
                             a_desc=req.a_desc or None, b_desc=req.b_desc or None, extra=req.prompt_extra,
                             seconds=req.seconds)

    def describe(self, image: Path) -> ShotText | None:
        """Description + label for one shot, cached per upload (uploads are content-addressed)."""
        cache = image.with_name(f"{image.stem}.describe-v{shots.VERSION}.json")
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return ShotText(**data) if data else None
        if "describe" not in self._specs:
            self._specs["describe"] = load_spec(self.settings.workflows_dir, "describe")
        text = shots.run(self.client, self._specs["describe"], image)
        cache.write_text(json.dumps(text.__dict__ if text else None), encoding="utf-8")
        return text

    def _fill_descriptions(self, job: JobRecord, req: TransitionRequest) -> TransitionRequest:
        """Read the shots the caller left undescribed (None), so the prompt has the qualified shape."""
        if req.prompt_override.strip() or (req.a_desc is not None and req.b_desc is not None):
            return req
        changes: dict[str, Any] = {}
        for side, image in (("a", req.image_a), ("b", req.image_b)):
            if getattr(req, f"{side}_desc") is not None:
                continue
            try:
                text = self.describe(Path(image))
            except Exception as e:  # noqa: BLE001 — fall back to the generic prompt, never fail the render
                text = None
                changes.setdefault("extra", {**job.extra})["describe_error"] = f"{type(e).__name__}: {e}"[:300]
            changes[f"{side}_desc"] = text.desc if text else ""
            if getattr(req, f"{side}_ref") is None:
                changes[f"{side}_ref"] = text.ref if text else ""
        extra = changes.pop("extra", None)
        req = TransitionRequest(**{**req.to_params(), **changes})
        self.store.update(job.id, params=req.to_params(), **({"extra": extra} if extra else {}))
        return req

    # -- worker ---------------------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            job = self.store.get(job_id)
            if job is None or job.status in ("cancelled", "succeeded", "failed"):
                continue
            try:
                self._execute(job)
            except Exception as e:  # noqa: BLE001 — surfaced to the user via the job record
                self.store.update(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:800])
            finally:
                self._handles.pop(job_id, None)

    def _execute(self, job: JobRecord) -> None:
        req = TransitionRequest(**job.params)
        t0 = time.monotonic()
        if job.comfy_job_id:
            handle = self.client.jobs.get(job.comfy_job_id)
        else:
            req = self._fill_descriptions(job, req)
            spec = self.spec(self.model_for(req))
            w, h = media.image_size(Path(req.image_a))
            width, height = media.output_size(w, h)
            prompt = self.prompt_for(req)
            params: dict[str, Any] = {"prompt": prompt, "seed": req.seed, "seconds": req.seconds}
            if "width" in spec.slots:
                params.update(width=width, height=height)
            if "enhance_prompt" in spec.slots:
                # P0: the in-graph enhancer (local Gemma-4 E2B) fixes LTX-2.5 dissolves and end-jumps
                params["enhance_prompt"] = req.enhance
            wf = build(self.client, spec, Path(req.image_a), Path(req.image_b), params)
            # keep the exact graph this take runs, so its receipt and ComfyUI bundle never follow later template edits
            receipt.archive_graph(job.id, spec, params, self.settings.outputs_dir)
            self.store.update(job.id, extra={**self.store.get(job.id).extra, "submitting_at": time.time()})
            handle = self.client.submit(wf)
            self.store.update(job.id, status="running", comfy_job_id=handle.id,
                              extra={**self.store.get(job.id).extra, "prompt": prompt, "width": width, "height": height,
                                     "submitted_at": time.time()})
        self._handles[job.id] = handle
        for ev in handle.events():
            if isinstance(ev, Progress) and ev.value is not None:
                self.store.update(job.id, progress=round(float(ev.value), 3))
            elif isinstance(ev, StatusChange):
                if ev.status == "cancelled":
                    break
                if ev.status in ("queued", "running"):  # the GPU queue is shared: reflect where we are
                    self.store.update(job.id, status=ev.status,
                                      extra={**self.store.get(job.id).extra, "queue_position": ev.queue_position})
        handle = handle.wait(timeout=self.settings.job_timeout_s)
        if self.store.get(job.id).status == "cancelled":
            return
        if handle.status != "succeeded":
            raise RuntimeError(f"comfy job {handle.id} {handle.status}: {handle.error}")
        out_dir = self.settings.outputs_dir
        mp4 = out_dir / f"{job.id}.mp4"
        handle.get_outputs(self.spec(self.model_for(req)).output_node)[0].to_file(mp4)
        media.poster(mp4, out_dir / f"{job.id}.jpg")
        media.contact_sheet(mp4, out_dir / f"{job.id}_sheet.png")
        info = media.probe(mp4)
        merged, merge_error = None, None
        if req.clip_a or req.clip_b:  # clip A + transition + clip B, the take the user asked for
            try:
                merged = media.merge_clips(mp4, out_dir / f"{job.id}_full.mp4", Path(req.clip_a) if req.clip_a else None,
                                           Path(req.clip_b) if req.clip_b else None).name
            except Exception as e:  # noqa: BLE001 — the transition rendered; keep it rather than fail the take
                merge_error = f"could not join the clips: {type(e).__name__}: {e}"[:300]
        self.store.update(job.id, status="succeeded", progress=1.0, output=mp4.name, poster=f"{job.id}.jpg",
                          sheet=f"{job.id}_sheet.png",
                          wall_s=round(time.time() - self.store.get(job.id).extra.get("submitted_at", time.time() - (time.monotonic() - t0)), 1),
                          extra={**self.store.get(job.id).extra, "duration": info.duration, "fps": info.fps,
                                 "has_audio": info.has_audio, **({"merged": merged} if merged else {}),
                                 **({"merge_error": merge_error} if merge_error else {})})
        try:
            self.write_receipt(job.id)
        except Exception:  # noqa: BLE001 — the take is done; the receipt is rebuilt on request
            pass

    def write_receipt(self, job_id: str) -> Path:
        """<id>.receipt.json next to the take: inputs (sha256), prompt, seed, model, graph."""
        job = self.store.get(job_id)
        dst = self.settings.outputs_dir / f"{job.id}.receipt.json"
        data = receipt.build_receipt(job, self.spec(self.model_for(TransitionRequest(**job.params))),
                                     self.settings.outputs_dir)
        dst.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return dst


CLIP_FORMAT = b"seam-clip-v1:"  # bump when normalize_clip changes


def store_clip(settings: Settings, src: Path) -> tuple[Path, float]:
    """Keep an uploaded clip (content-addressed by the upload's bytes), normalized to one MP4 clock, after checking
    it is a short video ffmpeg can read."""
    import hashlib

    settings.ensure_dirs()
    # salted with the normalizer version, so clips stored by an older normalizer are never reused
    dst = settings.uploads_dir / f"{hashlib.sha256(CLIP_FORMAT + src.read_bytes()).hexdigest()[:16]}.mp4"
    if not dst.exists():
        tmp = dst.with_name(f"{dst.stem}.{os.getpid()}.{threading.get_ident()}.mp4")
        try:
            media.normalize_clip(src, tmp)
            seconds = media.probe(tmp).duration
            lo, hi = media.CLIP_SECONDS
            if not lo <= seconds <= hi:
                raise ValueError(f"clips must be {lo:g}-{hi:g} s long (this one is {seconds:.1f} s)")
            os.replace(tmp, dst)  # a concurrent identical upload never sees a half-written clip
        finally:
            tmp.unlink(missing_ok=True)
    return dst, media.probe(dst).duration


def store_upload(settings: Settings, src: Path, name_hint: str = "upload") -> Path:
    """Normalize an uploaded image into the uploads dir (content-addressed by size+mtime-free hash)."""
    import hashlib

    settings.ensure_dirs()
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    dst = settings.uploads_dir / f"{digest}.png"
    if not dst.exists():
        tmp = dst.with_suffix(".tmp.png")
        media.normalize_image(src, tmp)
        shutil.move(tmp, dst)
    return dst
