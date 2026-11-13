"""Transition engine: request → comfy-sdk job → MP4 + poster + contact sheet, one job at a time."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import random
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comfy_sdk import Comfy, Progress, StatusChange

from . import backend as backends
from . import models as model_status
from . import describe as shots
from . import media, receipt, shotgen
from .config import Settings
from .downloads import command, set_for
from .describe import ShotText
from .jobs import JobRecord, JobStore, OrbitRecord, SequenceRecord, ShotRecord
from .presets import NO_EFFECT, Preset, load_presets, render_prompt
from .workflows import WorkflowSpec, build, load_catalog, load_image_models, load_spec

SUBMIT_UNKNOWN = ("server restarted while sending the job to ComfyUI; it may still have rendered there "
                  "(check ComfyUI's queue/output). Use New take to render it again.")
SECONDS_CHOICES = (5, 8, 10)  # what the graphs render; each launch effect lists the lengths it is qualified at
SEQUENCE_SHOTS = (2, 8)
CUT = "cut"  # a hand-off that generates nothing: the two shots meet with a hard cut; its segment is None
DEFAULT_HOLD = 2.0  # seconds an image shows when a cut is next to it (between transitions it shows through them)
HOLD_SECONDS = (0.5, 10.0)
CLOUD_GRACE_S = 30.0
SEQ = "seq:"  # worker queue items for sequences (reconcile/join), next to plain job ids
SHOT = "shot:"  # and for shot generations: one queue, so the GPU is never shared
ORBIT = "orbit:"  # and for 360° orbits of a shot image
ORBIT_FRAMES = 124  # one clockwise turn at 24 fps (ORB360)
# sizes ORB360 was seen to work at, by aspect (width / height at least): landscape, square, portrait
ORBIT_SIZES = ((1.2, (1152, 768)), (0.85, (800, 800)), (0.0, (832, 1024)))


TRASH_DAYS = 7  # how long the trash keeps a take or a sequence before its files go


class TakeInUse(ValueError):
    """A sequence still joins this take: it leaves through the sequence's storyboard."""


class ShotsUnavailable(ValueError):
    """Shots can't generate on this backend or install; the message says why."""


class BackendChanged(ValueError):
    """Takes no longer render where the user was told they would."""


class SequenceBusy(ValueError):
    """The sequence is still rendering or joining: edits wait until it settles."""


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
    model: str | None = None    # a catalog id overriding the effect's qualified model; None or "": that model
    clip_a: str | None = None   # optional clips around the transition: image_a is clip_a's last frame,
    clip_b: str | None = None   # image_b is clip_b's first frame, and the result is clip A + transition + clip B
    width: int | None = None    # pinned canvas (sequences: every segment renders at S0's size); None → from image_a
    height: int | None = None
    story: str = ""             # sequences: the idea of the whole film, snapshotted when the take is made

    def to_params(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class Engine:
    def __init__(self, settings: Settings, store: JobStore, client: Comfy | None = None) -> None:
        self.settings = settings
        self.store = store
        self.client = client  # tests inject one; otherwise client_for builds one per backend
        self._clients: dict[tuple[str, str | None], Comfy] = {}
        self._clients_lock = threading.Lock()
        self._specs: dict[str, WorkflowSpec] = {}
        self.presets: dict[str, Preset] = load_presets(settings.presets_file)
        self._queue: queue.Queue[str] = queue.Queue()
        self._handles: dict[str, Any] = {}
        self._rough_lock = threading.Lock()
        self._seq_lock = threading.Lock()  # sequence pointer swaps vs the worker's reconcile
        self._writing: set[str] = set()  # worker: the take or join whose files are being written, even if cancelled
        self._submit_lock = threading.Lock()  # cancel vs submit: a cancelled take never reaches the GPU
        self._worker = threading.Thread(target=self._run, daemon=True, name="seam-engine")

    # -- lifecycle ------------------------------------------------------------------------------
    def start(self) -> None:
        self.settings.ensure_dirs()
        self.purge_trash()
        self.purge_shots()
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
        for seq in self.store.list_sequences(limit=10_000):  # e.g. died after the last segment, before the join
            if seq.status in ("running", "assembling"):
                self._queue.put(SEQ + seq.id)
        for shot in self.store.list_shots(limit=10_000):
            if shot.status in ("queued", "running"):
                if shot.comfy_job_id:
                    self._queue.put(SHOT + shot.id)
                else:
                    error = SUBMIT_UNKNOWN if shot.submitting_at else "server restarted before submission"
                    self.store.update_shot(shot.id, status="interrupted", error=error)
        for orbit in self.store.list_orbits():
            if orbit.status in ("queued", "running"):
                if orbit.comfy_job_id:
                    self._queue.put(ORBIT + orbit.id)
                else:
                    error = SUBMIT_UNKNOWN if orbit.submitting_at else "server restarted before submission"
                    self.store.update_orbit(orbit.id, status="interrupted", error=error)
        self._worker.start()

    # -- backends -------------------------------------------------------------------------------
    def client_for(self, backend: str) -> Comfy:
        """The client of one backend, rebuilt when the Cloud key changes."""
        if self.client is not None:
            return self.client
        key = (backend, backends.cloud_key(self.settings)[0] if backend == backends.CLOUD else None)
        with self._clients_lock:
            if key not in self._clients:
                self._clients[key] = backends.make_client(self.settings, backend)
            return self._clients[key]

    @staticmethod
    def backend_of(job: JobRecord) -> str:
        return job.extra.get("backend", backends.LOCAL)  # takes from before Comfy Cloud rendered locally

    def active_cloud_jobs(self) -> list[JobRecord]:
        return [j for j in self.store.list(limit=10_000)
                if j.status in ("queued", "running") and self.backend_of(j) == backends.CLOUD]

    # -- models ---------------------------------------------------------------------------------
    def catalog(self) -> dict[str, WorkflowSpec]:
        return load_catalog(self.settings.workflows_dir)

    def lengths(self, preset_id: str, model: str) -> tuple[int, ...]:
        """An effect's lengths are qualified on its own model; on any other model, the model's lengths apply."""
        preset = self.presets[preset_id]
        if preset_id == NO_EFFECT:
            return self.spec(model).seconds
        if preset.status == "launch" and (preset.model or self.settings.model) == model:
            return preset.seconds
        return self.spec(model).seconds

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
        if req.preset == NO_EFFECT and not (req.prompt_extra.strip() or req.prompt_override.strip()):
            raise ValueError("No effect needs a prompt: describe the motion between the two shots")
        model = self.model_for(req)
        catalog = self.catalog()
        if model not in catalog:
            raise ValueError(f"unknown model {model!r}; choose one of {sorted(catalog)}")
        refusal = model_status.known_refusal(self.settings, catalog[model], backends.current(self.settings)[0])
        if refusal:
            raise ValueError(f"{catalog[model].label} cannot render here: {refusal}")
        if req.seconds not in SECONDS_CHOICES:
            raise ValueError(f"seconds must be one of {SECONDS_CHOICES}")
        allowed = self.lengths(req.preset, model)
        if req.seconds not in allowed:
            # each length is qualified on its own: a longer take paces the effect differently
            raise ValueError(f"effect {req.preset!r} on {model} renders at {', '.join(map(str, allowed))} s only")
        for p in (req.image_a, req.image_b, req.clip_a, req.clip_b):
            if p and not Path(p).is_file():
                raise ValueError(f"file not found: {Path(p).name}")
        for p in (req.clip_a, req.clip_b):  # every take passes here: no transition is made next to a long clip
            if p and not media.CLIP_SECONDS[0] <= (sec := media.probe(Path(p)).duration) <= media.CLIP_SECONDS[1] + 0.05:
                lo, hi = media.CLIP_SECONDS
                raise ValueError(f"a clip next to a transition must be {lo:g}-{hi:g} s (this one is {sec:.1f} s)")
        if (req.width is None) != (req.height is None) or any(
                v is not None and not (256 <= v <= 2048 and v % 32 == 0) for v in (req.width, req.height)):
            raise ValueError("width and height go together: multiples of 32 between 256 and 2048")

    def submit(self, req: TransitionRequest, parent_id: str | None = None, extra: dict[str, Any] | None = None,
               enqueue: bool = True, expected_backend: str | None = None) -> JobRecord:
        if req.seed is None:
            req.seed = random.randint(1, 2**31 - 1)
        self.validate(req)
        req.model = self.model_for(req)  # pin the resolved model on the job
        backend = backends.current(self.settings)[0]
        if expected_backend and expected_backend != backend:  # this read is the one the job keeps: no gap for a switch
            raise BackendChanged("The backend changed after that was offered; ask again.")
        self.client_for(backend)  # no Cloud key: refused here, before anything is queued
        # a take renders where it was asked to: a later switch never moves it, and a restart re-attaches to the same place
        job = self.store.create(req.to_params(), parent_id=parent_id, extra={**(extra or {}), "backend": backend})
        if enqueue:
            self._queue.put(job.id)
        return job

    def refine(self, job_id: str, changes: dict[str, Any], extra: dict[str, Any] | None = None,
               enqueue: bool = True, expected_backend: str | None = None) -> JobRecord:
        return self.submit(self._refined(job_id, changes), parent_id=job_id, extra=extra, enqueue=enqueue,
                           expected_backend=expected_backend)

    def preview_refine(self, job_id: str, changes: dict[str, Any]) -> TransitionRequest:
        """What refine would render, validated and with its model resolved; nothing is queued."""
        req = self._refined(job_id, changes)
        self.validate(req)
        req.model = self.model_for(req)
        return req

    def preview_segment(self, seq_id: str, index: int, changes: dict[str, Any]) -> TransitionRequest:
        seq = self.store.get_sequence(seq_id)
        if seq is None or seq.deleted_at:
            raise KeyError(seq_id)
        if seq.status in ("running", "assembling"):
            raise SequenceBusy(f"sequence {seq_id} is {seq.status}; re-roll a segment once it settles")
        self._take_at(seq, index)
        return self.preview_refine(seq.segments[index], self._segment_changes(seq, changes))

    @staticmethod
    def _take_at(seq: SequenceRecord, index: int) -> str:
        if not 0 <= index < len(seq.segments):
            raise ValueError(f"segment must be 1-{len(seq.segments)}")
        if seq.segments[index] is None:
            raise ValueError(f"transition {index + 1} is a cut: give it an effect by editing the sequence")
        return seq.segments[index]

    def _refined(self, job_id: str, changes: dict[str, Any]) -> TransitionRequest:
        base = self.store.get(job_id)
        if base is None:
            raise KeyError(job_id)
        if base.deleted_at:
            raise ValueError(f"take {job_id} is in the trash; restore it first")
        params = {**base.params, **{k: v for k, v in changes.items() if v is not None}}
        old = self.presets.get(base.params.get("preset"))
        picked = old is not None and base.params.get("model") not in (None, "", old.model or self.settings.model)
        if changes.get("preset") and not changes.get("model") and not picked:
            params["model"] = None  # a new preset brings its own preferred model; a model the user picked stays
        new = self.presets.get(params["preset"])
        if changes.get("seconds") is None and new and (params.get("model") or new.model or self.settings.model) in self.catalog():
            allowed = self.lengths(params["preset"], params.get("model") or new.model or self.settings.model)
            if params.get("seconds", 5) not in allowed:
                params["seconds"] = allowed[0]  # not rendered at the old length with this effect and model
        if changes.get("seed") is None:
            params["seed"] = None  # a refinement is a new take unless the caller pins the seed
        for side in ("a", "b"):  # a corrected description drops the old label, which may contradict it
            if changes.get(f"{side}_desc") is not None and changes.get(f"{side}_ref") is None:
                params[f"{side}_ref"] = ""
        return TransitionRequest(**params)

    def cancel(self, job_id: str) -> JobRecord:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._submit_lock:  # vs submit, handle registration and the final status write in _execute
            handle = self._handles.get(job_id)  # a submitted take can read "queued" again (shared GPU queue)
            error = None
            try:
                if handle is None and job.comfy_job_id and job.status in ("queued", "running"):
                    # re-attached after a restart and still waiting for the worker: it runs remotely all the same
                    handle = self.client_for(self.backend_of(job)).jobs.get(job.comfy_job_id)
                if handle is not None:
                    handle.cancel()
            except Exception as e:  # noqa: BLE001 — the user asked to stop: say what we could not do
                error = f"could not stop it on {self._where(job)} ({type(e).__name__}); it may still be running there"
            return self.store.update(job_id, status="cancelled", **({"error": error} if error else {}))

    # -- sequences ------------------------------------------------------------------------------
    def rough_cut(self, shots: list[dict[str, Any]], segments: list[dict[str, Any]]) -> Path:
        """A sequence's order and pacing from ffmpeg alone, before any GPU time: not a take, no job row, not in the gallery.
        Same request, same file. Older files leave after a day."""
        lo, hi = SEQUENCE_SHOTS
        if not lo <= len(shots) <= hi or len(segments) != len(shots) - 1:
            raise ValueError(f"a sequence has {lo}-{hi} shots and one transition per hand-off")
        labels: list[str | None] = []
        for i, seg in enumerate(segments):
            if self._is_cut(seg):
                labels.append(None)
                continue
            preset = self.presets.get(seg.get("preset") or "")
            if seg.get("preset") and preset is None:
                raise ValueError(f"transition {i + 1}: unknown effect {seg['preset']!r}")
            labels.append(f"{preset.name if preset else 'Surprise me'} · {seg.get('seconds', 5)} s")
        cuts = [label is None for label in labels]
        shots = [{**s, "hold": self._hold(s, i, cuts) or s.get("hold")} for i, s in enumerate(shots)]
        folder = self.settings.outputs_dir / "rough"
        folder.mkdir(parents=True, exist_ok=True)
        key = json.dumps([shots, labels], sort_keys=True)
        dst = folder / f"{hashlib.sha256(key.encode()).hexdigest()[:16]}.mp4"
        with self._rough_lock:  # the same request twice would write one temp file
            if dst.is_file():
                return dst
            for old in folder.glob("*.mp4"):
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink(missing_ok=True)
            width, height = media.output_size(*media.image_size(Path(shots[0]["last"])))
            with tempfile.TemporaryDirectory(dir=folder) as work:
                return media.rough_cut(shots, labels, dst, Path(work), width, height)

    def create_sequence(self, shots: list[dict[str, Any]], segments: list[dict[str, Any]],
                        story: str = "") -> SequenceRecord:
        """shots: [{first, last, clip?}] (paths; an image has first == last), segments: TransitionRequest fields
        (preset, seconds, seed, prompt_extra, descriptions…) per hand-off. Every segment is validated before anything
        is queued; segments render without clips and the join adds each clip once."""
        lo, hi = SEQUENCE_SHOTS
        if not lo <= len(shots) <= hi:
            raise ValueError(f"a sequence has {lo}-{hi} shots")
        if len(segments) != len(shots) - 1:
            raise ValueError(f"{len(shots)} shots need {len(shots) - 1} segments (one per hand-off)")
        for shot in shots:
            for key in ("first", "last", "clip"):
                if shot.get(key) and not Path(shot[key]).is_file():
                    raise ValueError(f"file not found: {Path(shot[key]).name}")
        width, height = media.output_size(*media.image_size(Path(shots[0]["last"])))
        cuts = [self._is_cut(seg) for seg in segments]
        self._check_shots(shots, cuts)
        reqs: list[TransitionRequest | None] = []
        for i, seg in enumerate(segments):
            if cuts[i]:
                reqs.append(None)
                continue
            fields = {k: v for k, v in seg.items()
                      if k not in ("image_a", "image_b", "clip_a", "clip_b", "width", "height", "story")}
            req = TransitionRequest(**{**fields, "image_a": shots[i]["last"], "image_b": shots[i + 1]["first"],
                                       "width": width, "height": height, "story": story.strip()})
            try:
                self.validate(req)
            except ValueError as e:
                raise ValueError(f"segment {i + 1}: {e}") from e
            reqs.append(req)
        with self._seq_lock:
            jobs = [self.submit(r, enqueue=False) if r else None for r in reqs]
            seq = self.store.create_sequence([self._shot_record(s) for s in shots],
                                             [j.id if j else None for j in jobs], width, height, story.strip())
            for i, j in enumerate(jobs):
                if j:
                    self.store.update(j.id, extra={**j.extra, "sequence_id": seq.id, "segment": i})
        for j in jobs:
            if j:
                self._queue.put(j.id)
        if not any(jobs):  # only cuts: nothing renders, the video joins at once
            self._queue.put(SEQ + seq.id)
        return seq

    @staticmethod
    def _is_cut(segment: dict[str, Any]) -> bool:
        return (segment.get("preset") or "") == CUT

    @staticmethod
    def _shot_record(shot: dict[str, Any]) -> dict[str, Any]:
        rec = {"first": shot["first"], "last": shot["last"], "clip": shot.get("clip")}
        if shot.get("hold") is not None:
            rec["hold"] = float(shot["hold"])
        return rec

    @staticmethod
    def _hold(shot: dict[str, Any], i: int, cuts: list[bool]) -> float:
        """Seconds image shot i shows on its own: only next to a cut (between transitions it shows through them,
        as before cuts existed). A clip always plays whole."""
        if shot.get("clip") or not ((i > 0 and cuts[i - 1]) or (i < len(cuts) and cuts[i])):
            return 0.0
        return float(shot.get("hold") or DEFAULT_HOLD)

    @staticmethod
    def _check_shots(shots: list[dict[str, Any]], cuts: list[bool]) -> None:
        """Limits that depend on the neighbours: a clip next to a generated transition is short (a product rule: the
        models only ever see its first or last frame); a clip between cuts only plays, so it may be long."""
        for i, shot in enumerate(shots):
            hold = shot.get("hold")
            if hold is not None and not HOLD_SECONDS[0] <= float(hold) <= HOLD_SECONDS[1]:
                raise ValueError(f"shot {i + 1}: an image shows for {HOLD_SECONDS[0]:g}-{HOLD_SECONDS[1]:g} s")
            if not shot.get("clip") or not Path(shot["clip"]).is_file():
                continue
            sides = ([cuts[i - 1]] if i > 0 else []) + ([cuts[i]] if i < len(cuts) else [])
            sec = media.probe(Path(shot["clip"])).duration
            if all(sides):
                lo, hi = media.LONG_CLIP_SECONDS
                if not lo <= sec <= hi + 0.05:
                    raise ValueError(f"shot {i + 1}: a clip between cuts is {lo:g}-{hi:g} s (this one is {sec:.1f} s)")
            elif not media.CLIP_SECONDS[0] <= sec <= media.CLIP_SECONDS[1] + 0.05:
                lo, hi = media.CLIP_SECONDS
                raise ValueError(f"shot {i + 1}: a clip next to a transition is {lo:g}-{hi:g} s (this one is "
                                 f"{sec:.1f} s); make its hand-offs cuts to use it whole")

    @staticmethod
    def _segment_changes(seq: SequenceRecord, changes: dict[str, Any]) -> dict[str, Any]:
        return {**{k: v for k, v in changes.items() if k not in ("width", "height", "clip_a", "clip_b", "story")},
                "width": seq.width, "height": seq.height, "clip_a": "", "clip_b": "",  # canvas stays, no clips
                "story": seq.story}  # a new take uses the sequence's story as it is now

    def refine_segment(self, seq_id: str, index: int, changes: dict[str, Any],
                       expected_updated_at: float | None = None, expected_backend: str | None = None) -> SequenceRecord:
        """A new take of one segment (same rules as refine); the others are reused and the sequence joins again."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at:
                raise KeyError(seq_id)
            if seq.status in ("running", "assembling"):
                raise SequenceBusy(f"sequence {seq_id} is {seq.status}; re-roll a segment once it settles")
            if expected_updated_at is not None and abs(seq.updated_at - expected_updated_at) > 1e-6:
                raise SequenceBusy("this sequence changed after that was offered; ask again")
            job = self.refine(self._take_at(seq, index), self._segment_changes(seq, changes), extra={"sequence_id": seq.id, "segment": index},
                              enqueue=False, expected_backend=expected_backend)
            segments = list(seq.segments)
            segments[index] = job.id
            seq = self.store.update_sequence(seq_id, segments=segments, status="running", error=None)
        self._queue.put(job.id)
        return seq

    def append_shot(self, seq_id: str, shot: dict[str, Any], segment: dict[str, Any]) -> SequenceRecord:
        """Grow a finished sequence by one shot: only the new hand-off renders, the other takes are reused and the
        video joins again. The new take starts from the last shot (its still, or its clip's last frame)."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at:
                raise KeyError(seq_id)
            job = self._append_locked(seq, shot, segment)
        self._queue.put(job.id if job else SEQ + seq_id)  # a cut renders nothing: just join again
        return self.store.get_sequence(seq_id)

    def continue_take(self, job_id: str, shot: dict[str, Any], segment: dict[str, Any],
                      story: str = "") -> SequenceRecord:
        """Turn a finished two-shot take into a sequence whose first segment IS that take (nothing re-renders) and
        add the next shot. The take's clips become shot clips; its canvas is the take's real size."""
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.status != "succeeded" or not job.output or not (self.settings.outputs_dir / job.output).is_file():
            raise ValueError(f"take {job_id} is {job.status}; continue from a finished take")
        if job.deleted_at:
            raise ValueError(f"take {job_id} is in the trash; restore it first")
        p = job.params
        last_b = p["image_b"]
        if p.get("clip_b"):  # the take landed on clip B's first frame; the next one leaves from its last frame
            last_b = str(store_upload(self.settings, media.clip_frame(
                Path(p["clip_b"]), self.settings.uploads_dir / f"{job.id}_clip_b_last.png", last=True)))
        first_a = p["image_a"]
        if p.get("clip_a"):  # image_a is clip A's LAST frame; an edit may move this shot after another one
            first_a = str(store_upload(self.settings, media.clip_frame(
                Path(p["clip_a"]), self.settings.uploads_dir / f"{job.id}_clip_a_first.png", last=False)))
        info = media.probe(self.settings.outputs_dir / job.output)
        shots = [{"first": first_a, "last": p["image_a"], "clip": p.get("clip_a") or None},
                 {"first": p["image_b"], "last": last_b, "clip": p.get("clip_b") or None}]
        for s_ in [*shots, shot]:
            for key in ("first", "last", "clip"):
                if s_.get(key) and not Path(s_[key]).is_file():
                    raise ValueError(f"file not found: {Path(s_[key]).name}")
        # some models size their own output (720p): the canvas of what follows stays on the grid the graphs need
        width, height = info.width - info.width % 32, info.height - info.height % 32
        if not self._is_cut(segment):  # a cut generates nothing: _append_locked checks its shots
            self.validate(self._next_request(shots[-1]["last"], shot, segment, width, height))
        with self._seq_lock:
            if self.store.get(job_id).deleted_at:  # trashed while the files above were being read
                raise ValueError(f"take {job_id} is in the trash; restore it first")
            seq = self.store.create_sequence(shots, [job.id], width, height, story.strip())
            seq = self.store.update_sequence(seq.id, status="succeeded", origin=job.id)  # one finished take: nothing to wait for
            try:
                new = self._append_locked(seq, shot, segment)
            except Exception:
                self.store.update_sequence(seq.id, status="cancelled", error="could not add the next shot")
                raise
        self._queue.put(new.id if new else SEQ + seq.id)
        return self.store.get_sequence(seq.id)

    def _append_locked(self, seq: SequenceRecord, shot: dict[str, Any], segment: dict[str, Any]) -> JobRecord | None:
        """Caller holds _seq_lock. Validates everything before anything is written; queues nothing. A cut returns None."""
        if seq.status != "succeeded":
            raise SequenceBusy(f"sequence {seq.id} is {seq.status}; add a shot once it has finished")
        if len(seq.shots) >= SEQUENCE_SHOTS[1]:
            raise ValueError(f"a sequence has at most {SEQUENCE_SHOTS[1]} shots")
        for s_ in [*seq.shots, shot]:
            for key in ("first", "last", "clip"):
                if s_.get(key) and not Path(s_[key]).is_file():
                    raise ValueError(f"file not found: {Path(s_[key]).name}")
        jobs = [self.store.get(j) for j in seq.segments if j]
        if any(j is None or not j.output or not (self.settings.outputs_dir / j.output).is_file() for j in jobs):
            raise ValueError("a take of this sequence is missing on disk; make a new take of it first")
        shots = [*seq.shots, self._shot_record(shot)]
        cut = self._is_cut(segment)
        self._check_shots(shots, [s is None for s in seq.segments] + [cut])
        if cut:
            self.store.update_sequence(seq.id, status="running", error=None, segments=[*seq.segments, None], shots=shots)
            return None
        prev = self.store.get(seq.segments[-1]) if seq.segments and seq.segments[-1] else None
        fields = dict(segment)
        start = seq.shots[-1]["last"]
        # the shot we leave from is the one the previous take landed on: keep the user's reading of it
        if prev and not seq.shots[-1].get("clip") and Path(prev.params["image_b"]) == Path(start):
            if fields.get("a_desc") is None and prev.params.get("b_desc") is not None:
                fields["a_desc"] = prev.params["b_desc"]
                if fields.get("a_ref") is None:
                    fields["a_ref"] = prev.params.get("b_ref")
        req = self._next_request(start, shot, {**fields, "story": seq.story}, seq.width, seq.height)
        self.validate(req)
        job = self.submit(req, extra={"sequence_id": seq.id, "segment": len(seq.segments)}, enqueue=False)
        try:
            self.store.update_sequence(seq.id, status="running", error=None, segments=[*seq.segments, job.id],
                                       shots=shots)
        except Exception:
            self.store.update(job.id, status="cancelled", error="could not add it to the sequence")
            raise
        return job

    @staticmethod
    def _next_request(start: str, shot: dict[str, Any], segment: dict[str, Any], width: int,
                      height: int) -> TransitionRequest:
        """The transition from the last shot (`start`: its still or its clip's last frame) into `shot`."""
        fields = {k: v for k, v in segment.items()
                  if k not in ("image_a", "image_b", "clip_a", "clip_b", "width", "height")}
        return TransitionRequest(**{**fields, "image_a": start, "image_b": shot["first"], "width": width,
                                    "height": height})

    def edit_sequence(self, seq_id: str, order: list[int], transitions: dict[int, dict[str, Any]] | None = None,
                      story: str | None = None, expected_updated_at: float | None = None) -> SequenceRecord:
        """Remove and reorder shots, and set the story, in one step. `order` lists the current shots (0-based) in their
        new order; a hand-off whose two shots were already adjacent in that direction keeps its take, every other one
        is new and needs a transition spec in `transitions` (keyed by its 0-based position in the new order; a spec on
        a kept hand-off makes a new take of it). Only new transitions render; the canvas never changes. A changed story
        re-renders nothing: later takes use it, older ones keep theirs."""
        transitions = dict(transitions or {})
        new_jobs: list[JobRecord] = []
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at:
                raise KeyError(seq_id)
            if seq.status in ("running", "assembling"):
                raise SequenceBusy(f"sequence {seq_id} is {seq.status}; edit it once it settles")
            if expected_updated_at is not None and abs(seq.updated_at - expected_updated_at) > 1e-6:
                raise SequenceBusy("this sequence changed since you opened it; reload it and edit again")
            n = len(seq.shots)
            lo, hi = SEQUENCE_SHOTS
            if not lo <= len(order) <= hi:
                raise ValueError(f"a sequence keeps {lo}-{hi} shots")
            if len(set(order)) != len(order) or any(not 0 <= i < n for i in order):
                raise ValueError(f"order must list distinct shots between 1 and {n}")
            bad = sorted(k for k in transitions if not 0 <= k < len(order) - 1)
            if bad:
                raise ValueError(f"no transition {', '.join(str(k + 1) for k in bad)} in the new order")
            story = seq.story if story is None else story.strip()
            kept = {(i, i + 1): job_id for i, job_id in enumerate(seq.segments)}
            shots = [self._shot_endpoints(seq, i) for i in order]
            plan: list[str | TransitionRequest | None] = []
            for k in range(len(order) - 1):
                pair = (order[k], order[k + 1])
                if k in transitions and self._is_cut(transitions[k]):
                    plan.append(None)
                    continue
                if k not in transitions and pair in kept and kept[pair] is None:
                    plan.append(None)  # a cut stays a cut
                    continue
                if k not in transitions and pair in kept:
                    job = self.store.get(kept[pair])
                    if job is None or job.status != "succeeded" or not job.output \
                            or not (self.settings.outputs_dir / job.output).is_file():
                        raise ValueError(f"the transition {pair[0] + 1} → {pair[1] + 1} has no finished take; make a "
                                         "new take of it first, or give it a transition here")
                    plan.append(kept[pair])
                    continue
                if k not in transitions:
                    plan.append(None)  # a new hand-off with no effect given is a cut: moving shots never renders by itself
                    continue
                req = self._next_request(shots[k]["last"], shots[k + 1], {**transitions[k], "story": story},
                                         seq.width, seq.height)
                try:
                    self.validate(req)
                except ValueError as e:
                    raise ValueError(f"transition {k + 1} ({pair[0] + 1} → {pair[1] + 1}): {e}") from e
                plan.append(req)
            self._check_shots(shots, [x is None for x in plan])
            for s_ in shots:
                for key in ("first", "last", "clip"):
                    if s_.get(key) and not Path(s_[key]).is_file():
                        raise ValueError(f"file not found: {Path(s_[key]).name}")
            segments = None if any(isinstance(x, TransitionRequest) for x in plan) else list(plan)
            if segments == seq.segments and order == list(range(n)):
                return self.store.update_sequence(seq_id, story=story) if story != seq.story else seq
            try:
                segments = []
                for k, x in enumerate(plan):
                    if not isinstance(x, TransitionRequest):
                        segments.append(x)
                    else:
                        job = self.submit(x, extra={"sequence_id": seq.id, "segment": k}, enqueue=False)
                        new_jobs.append(job)
                        segments.append(job.id)
                seq = self.store.update_sequence(seq_id, shots=shots, segments=segments, story=story,
                                                 status="running", error=None)
            except Exception:
                for job in new_jobs:  # nothing half-written stays queued
                    self.store.update(job.id, status="cancelled", error="the sequence edit did not go through")
                raise
        for job in new_jobs:
            self._queue.put(job.id)
        if not new_jobs:  # only removed an end shot: every take is reused, the video just joins again
            self._queue.put(SEQ + seq_id)
        return seq

    def _shot_endpoints(self, seq: SequenceRecord, i: int) -> dict[str, Any]:
        """Shot i with its real first and last frames. Sequences continued from a pair take before 10/29 stored clip
        A's last frame as both; read its first frame now, before an edit lands another transition on it."""
        shot = self._shot_record(seq.shots[i])
        if shot["clip"] and shot["first"] == shot["last"] and Path(shot["clip"]).is_file():
            shot["first"] = str(store_upload(self.settings, media.clip_frame(
                Path(shot["clip"]), self.settings.uploads_dir / f"{seq.id}_{i}_clip_first.png", last=False)))
        return shot

    def cancel_sequence(self, seq_id: str) -> SequenceRecord:
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at:
                raise KeyError(seq_id)
            if seq.status in ("succeeded", "failed", "cancelled"):
                return seq
            seq = self.store.update_sequence(seq_id, status="cancelled")
            for job_id in seq.segments:
                job = self.store.get(job_id) if job_id else None  # a cut has nothing to stop
                if job and job.status in ("queued", "running"):
                    self.cancel(job_id)
        return seq

    def assemble(self, seq_id: str) -> SequenceRecord:
        """Join again from the segments' existing outputs (after a failed join or a cancel)."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at:
                raise KeyError(seq_id)
            if seq.status in ("running", "assembling"):
                raise SequenceBusy(f"sequence {seq_id} is {seq.status}")
            seq = self.store.update_sequence(seq_id, status="running", error=None)
        self._queue.put(SEQ + seq_id)
        return seq

    def _reconcile(self, seq_id: str) -> None:
        """Worker only: derive the sequence's status from its current segments; join once all of them succeeded."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None or seq.deleted_at or seq.status == "cancelled":
                return
            jobs = [self.store.get(j) if j else None for j in seq.segments]
            status = ["succeeded" if j_id is None else j.status if j else "failed" for j_id, j in zip(seq.segments, jobs)]
            if any(s in ("queued", "running") for s in status):
                return  # still rendering; the next segment to finish reconciles again
            bad = [i for i, s in enumerate(status) if s != "succeeded"]
            if bad:
                i = bad[0]
                why = (jobs[i].error if jobs[i] else None) or status[i]
                self.store.update_sequence(seq_id, status="failed", error=f"segment {i + 1}: {why}"[:600])
                return
            cuts = [j is None for j in seq.segments]
            holds = [self._hold(s, i, cuts) for i, s in enumerate(seq.shots)]
            if any(cuts):  # what is joined, in order: an all-cuts reorder or a new hold gets a new file too
                ident = json.dumps([[s["first"], s.get("clip"), h] for s, h in zip(seq.shots, holds)] + [seq.segments])
            else:
                ident = ",".join(seq.segments)  # sequences without cuts keep the names they already have
            key = hashlib.sha256(ident.encode()).hexdigest()[:8]
            name = f"{seq.id}_{key}.mp4"  # one file per set of takes: a re-roll gets a new URL
            if seq.output == name and (self.settings.outputs_dir / name).is_file():
                self.store.update_sequence(seq_id, status="succeeded", error=None)
                return
            seq = self.store.update_sequence(seq_id, status="assembling")
            self._writing.add(seq_id)
        out_dir = self.settings.outputs_dir
        dst = out_dir / name
        try:
            with tempfile.TemporaryDirectory(dir=out_dir) as work:
                parts: list[Path] = []
                for i, shot in enumerate(seq.shots):  # clip0 or hold0?, take0?, clip1 or hold1?, take1?, …
                    if shot.get("clip"):
                        parts.append(Path(shot["clip"]))
                    elif holds[i]:
                        parts.append(media.still(Path(shot["first"]), Path(work) / f"hold{i}.mp4", holds[i]))
                    if i < len(jobs) and jobs[i]:
                        parts.append(out_dir / jobs[i].output)
                takes = [out_dir / j.output for j in jobs if j]
                clips = [Path(s["clip"]) for s in seq.shots if s.get("clip")]
                # the highest: models differ, no take may lose frames; only cuts: the clips' rate, else 24
                fps = max((media.probe(p).fps for p in takes or clips), default=24.0)
                media.join(parts, dst, seq.width, seq.height, fps)
            media.poster(dst, out_dir / f"{dst.stem}.jpg")
            media.contact_sheet(dst, out_dir / f"{dst.stem}_sheet.png", n=6 * max(1, len(jobs)), height=160, cols=6)
        except Exception as e:  # noqa: BLE001 — the takes stay; assemble() joins again
            detail = getattr(e, "stderr", None) or e
            print(f"seam: joining sequence {seq_id} failed: {type(e).__name__}: {detail!s}"[:2000], flush=True)
            with self._seq_lock:
                cur = self.store.get_sequence(seq_id)
                if cur and cur.status == "assembling" and cur.segments == seq.segments:
                    self.store.update_sequence(seq_id, status="failed", error=(
                        f"could not join the takes ({type(e).__name__}); the server log has the details. "
                        "Join again, or make a new take of a segment"))
                self._writing.discard(seq_id)
            return
        with self._seq_lock:
            self._writing.discard(seq_id)
            cur = self.store.get_sequence(seq_id)
            if cur and cur.status == "assembling" and cur.segments == seq.segments:  # not cancelled meanwhile
                self.store.update_sequence(seq_id, status="succeeded", output=name, poster=f"{dst.stem}.jpg",
                                           sheet=f"{dst.stem}_sheet.png", error=None)

    # -- trash, stars ---------------------------------------------------------------------------
    def trash_job(self, job_id: str) -> JobRecord:
        """Into the trash, nothing leaves the disk yet. A take a sequence joins is removed through that sequence."""
        with self._seq_lock:
            job = self.store.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.deleted_at:
                return job
            if job.status in ("queued", "running"):
                raise SequenceBusy(f"take {job_id} is {job.status}; cancel it first")
            if any(job_id in s.segments for s in self.store.list_sequences(limit=-1)):
                raise TakeInUse(f"a sequence uses take {job_id}; remove it from the storyboard or trash the sequence")
            return self.store.update(job_id, deleted_at=time.time())

    def trash_sequence(self, seq_id: str) -> SequenceRecord:
        """The sequence and its takes, past and present, except starred ones and those another sequence joins."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None:
                raise KeyError(seq_id)
            if seq.deleted_at:
                return seq
            if seq.status in ("running", "assembling"):
                raise SequenceBusy(f"sequence {seq_id} is {seq.status}; cancel it first")
            live = self.store.list(limit=-1)
            own = [j for j in live if j.id in seq.segments or j.id == seq.origin or j.extra.get("sequence_id") == seq_id]
            if any(j.status in ("queued", "running") for j in own):
                raise SequenceBusy(f"sequence {seq_id} still has a take rendering; cancel it first")
            shared = {i for s in self.store.list_sequences(limit=-1) if s.id != seq_id for i in s.segments}
            now = time.time()
            for j in own:
                if not j.starred and j.id not in shared:
                    self.store.update(j.id, deleted_at=now, extra={**j.extra, "trashed_with": seq_id})
            return self.store.update_sequence(seq_id, deleted_at=now)

    def restore_job(self, job_id: str) -> JobRecord:
        with self._seq_lock:
            job = self.store.get(job_id)
            if job is None:
                raise KeyError(job_id)
            return self._restored(job)

    def _restored(self, job: JobRecord) -> JobRecord:
        extra = {k: v for k, v in job.extra.items() if k != "trashed_with"}
        return self.store.update(job.id, deleted_at=None, extra=extra)

    def restore_sequence(self, seq_id: str) -> SequenceRecord:
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None:
                raise KeyError(seq_id)
            for j in self.store.list(limit=-1, trashed=True):
                if j.id in seq.segments or j.extra.get("trashed_with") == seq_id:
                    self._restored(j)
            return self.store.update_sequence(seq_id, deleted_at=None)

    def _remove_files(self, record_id: str) -> bool:
        """Every file named after a take or sequence (ids are ours). False when one is still open somewhere."""
        out = self.settings.outputs_dir
        done = True
        for path in [*out.glob(f"{record_id}.*"), *out.glob(f"{record_id}_*")]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                done = False
        return done

    def purge_trash(self, everything: bool = False) -> dict[str, int]:
        """Remove what the trash has held for TRASH_DAYS (or all of it). Uploads stay: other takes share them.
        What the worker is still writing, or a sequence in the trash still joins, waits for the next purge."""
        cutoff = time.time() - TRASH_DAYS * 86400
        due = lambda r: everything or r.deleted_at < cutoff  # noqa: E731
        removed, kept = 0, 0
        with self._seq_lock:
            gone: set[str] = set()
            for seq in self.store.list_sequences(limit=-1, trashed=True):
                if not due(seq):
                    continue
                if seq.id not in self._writing and self._remove_files(seq.id):
                    self.store.remove(seq.id)
                    gone.add(seq.id)
                    removed += 1
                else:
                    kept += 1
            held = {i for trashed in (False, True) for s in self.store.list_sequences(limit=-1, trashed=trashed)
                    for i in s.segments}
            for job in self.store.list(limit=-1, trashed=True):
                if not due(job):
                    continue
                if job.id not in held and job.id not in self._writing and self._remove_files(job.id):
                    self.store.remove(job.id)
                    removed += 1
                else:
                    kept += 1
            for job in self.store.list(limit=-1):  # starred takes outlive their sequence
                if job.extra.get("sequence_id") in gone:
                    self.store.update(job.id, extra={k: v for k, v in job.extra.items()
                                                     if k not in ("sequence_id", "segment")})
        return {"removed": removed, "kept": kept}

    # -- shots ----------------------------------------------------------------------------------
    def _shot_spec(self, model: str = "shot") -> WorkflowSpec:
        """By id, or by the label a generation keeps."""
        for spec in load_image_models(self.settings.workflows_dir).values():
            if model in (spec.model, spec.label):
                return spec
        raise ValueError(f"unknown image model {model!r}")

    def shot_models(self, backend: str | None = None) -> list[dict[str, Any]]:
        backend = backend or backends.current(self.settings)[0]
        models = load_image_models(self.settings.workflows_dir)
        return [{**m, "max_prompt": models[m["id"]].max_prompt, "max_count": models[m["id"]].max_count}
                for m in model_status.listing(self.settings, models, backend)]

    def shots_unavailable(self, backend: str | None = None, model: str = "shot") -> str | None:
        """Why this model can't generate here, in words; None when it can or the node list could not be read."""
        backend = backend or backends.current(self.settings)[0]
        spec = self._shot_spec(model)
        ok, why = model_status.why_not(spec, backend, model_status.object_info(self.settings, backend))
        if ok is not False:
            return None
        if backend == backends.LOCAL and why == "model files not installed" and (s := set_for(spec.model)):
            why += f". Get them under Models, or run `{command(s.id)}`"
        return f"{spec.label} is not ready: {why}."

    def create_shot(self, prompt: str, aspect: str = "16:9", quality: str = "standard", count: int = 1,
                    seed: int | None = None, parent_id: str | None = None, model: str = "shot") -> ShotRecord:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("write a prompt first")
        spec = self._shot_spec(model)
        if len(prompt) > spec.max_prompt:
            raise ValueError(f"{spec.label} takes up to {spec.max_prompt} characters; this prompt has {len(prompt)}")
        if count > spec.max_count:
            raise ValueError(f"{spec.label} makes {spec.max_count} image{'s' if spec.max_count > 1 else ''} at a time")
        width, height, steps = shotgen.plan(aspect, quality, count, spec.quality)
        backend = backends.current(self.settings)[0]  # read once: what was checked is what the record keeps
        if why := self.shots_unavailable(backend, spec.model):
            raise ShotsUnavailable(why)
        shot = self.store.create_shot(
            prompt=prompt, aspect=aspect, quality=quality, width=width, height=height, steps=steps,
            model=spec.label, seed=random.randint(1, 2**31 - 1) if seed is None else seed, count=count,
            backend=backend, parent_id=parent_id)
        self._queue.put(SHOT + shot.id)
        return shot

    def variation(self, shot_id: str) -> ShotRecord:
        """The same prompt and settings with a new seed."""
        base = self._shot(shot_id)
        return self.create_shot(base.prompt, base.aspect, base.quality, base.count, parent_id=base.id, model=base.model)

    def _shot(self, shot_id: str) -> ShotRecord:
        shot = self.store.get_shot(shot_id)
        if shot is None:
            raise KeyError(shot_id)
        return shot

    def _run_shot(self, shot_id: str) -> None:
        with self._seq_lock:
            shot = self.store.get_shot(shot_id)
            if shot is None or shot.status not in ("queued", "running"):
                return
            self._writing.add(shot_id)
        try:
            self._execute_shot(shot)
        except Exception as e:  # noqa: BLE001 — surfaced to the user via the record
            with self._submit_lock:
                now = self.store.get_shot(shot_id)
                if now is not None and now.status != "cancelled":
                    self.store.update_shot(shot_id, status="failed", error=f"{type(e).__name__}: {e}"[:800])
        finally:
            self._handles.pop(shot_id, None)
            with self._seq_lock:
                self._writing.discard(shot_id)

    def _execute_shot(self, shot: ShotRecord) -> None:
        spec = self._shot_spec(shot.model)
        client = self.client_for(shot.backend)
        if shot.comfy_job_id:
            handle = client.jobs.get(shot.comfy_job_id)
        else:
            wf = client.workflows.from_file(spec.workflow_file)
            for slot, value in {"prompt": shot.prompt, "width": shot.width, "height": shot.height, "count": shot.count,
                                "steps": shot.steps, "seed": shot.seed, "filename_prefix": f"seam/{shot.id}"}.items():
                if slot in spec.slots:  # a model that makes one image has no count
                    wf.set_input(*spec.slots[slot], value)
            with self._submit_lock:
                if self._shot(shot.id).status == "cancelled":
                    return
                self.store.update_shot(shot.id, submitting_at=time.time())
                handle = client.submit(wf)
                self.store.update_shot(shot.id, status="running", comfy_job_id=handle.id)
        with self._submit_lock:
            self._handles[shot.id] = handle
            if self._shot(shot.id).status == "cancelled":
                handle.cancel()
                return
        handle = handle.wait(timeout=self.settings.job_timeout_s)
        if self._shot(shot.id).status == "cancelled":
            return
        if handle.status != "succeeded":
            raise RuntimeError(f"comfy job {handle.id} {handle.status}: {handle.error}")
        folder = self.settings.outputs_dir / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        images = []
        try:
            for i, output in enumerate(handle.get_outputs(spec.output_node)):
                name = f"{shot.id}_{i}.png"
                output.to_file(folder / name)  # bytes as ComfyUI wrote them: the PNG carries its graph, prompt and seed
                images.append({"name": name, "starred": False, "deleted_at": None})
        except Exception:
            self._unlink_shots(shot.id)
            raise
        if not images:
            raise RuntimeError("the graph returned no images")
        with self._submit_lock:
            if self._shot(shot.id).status != "cancelled":
                self.store.update_shot(shot.id, status="succeeded", images=images, error=None)
                return
        self._unlink_shots(shot.id)

    def _unlink_shots(self, shot_id: str) -> None:
        for path in (self.settings.outputs_dir / "shots").glob(f"{shot_id}_*.png"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def cancel_shot(self, shot_id: str) -> ShotRecord:
        shot = self._shot(shot_id)
        if shot.status not in ("queued", "running"):
            return shot
        with self._submit_lock:
            handle = self._handles.get(shot_id)
            error = None
            try:
                if handle is None and shot.comfy_job_id:  # re-attached after a restart, still waiting for the worker
                    handle = self.client_for(shot.backend).jobs.get(shot.comfy_job_id)
                if handle is not None:
                    handle.cancel()
            except Exception as e:  # noqa: BLE001 — the user asked to stop: say what we could not do
                error = f"could not stop it on ComfyUI ({type(e).__name__}); it may still be running there"
            return self.store.update_shot(shot_id, status="cancelled", **({"error": error} if error else {}))

    def dismiss_shot(self, shot_id: str) -> None:
        """A generation that ended without images leaves the collection."""
        with self._seq_lock:
            shot = self._shot(shot_id)
            if shot.status in ("queued", "running") or shot.images or shot_id in self._writing:
                raise ValueError("only a generation that ended without images can be dismissed")
            self.store.remove_shot(shot_id)

    def shot_image(self, shot_id: str, name: str) -> Path:
        """A file of this record, by the name the server gave it: no path from a request reaches the disk."""
        shot = self._shot(shot_id)
        entry = next((i for i in shot.images if i["name"] == name), None)
        if entry is None or entry["deleted_at"]:
            raise KeyError(name)
        return self.settings.outputs_dir / "shots" / entry["name"]

    def edit_image(self, shot_id: str, name: str, **changes: Any) -> ShotRecord:
        with self._seq_lock:
            shot = self._shot(shot_id)
            if not any(i["name"] == name for i in shot.images):
                raise KeyError(name)
            return self.store.update_shot(shot_id, images=[{**i, **changes} if i["name"] == name else i for i in shot.images])

    # -- orbits ---------------------------------------------------------------------------------
    def orbit_unavailable(self, backend: str | None = None) -> str | None:
        """Why a 360° orbit can't render here, in words; None when it can or the node list could not be read."""
        backend = backend or backends.current(self.settings)[0]
        spec = self.spec("orbit")
        ok, why = model_status.why_not(spec, backend, model_status.object_info(self.settings, backend))
        return None if ok is not False else f"{spec.label} is not ready: {why}."

    def create_orbit(self, shot_id: str, name: str) -> OrbitRecord:
        """A 360° orbit of one shot image; one at a time per image, and a new one replaces the old once it succeeds."""
        if why := self.orbit_unavailable():
            raise ShotsUnavailable(why)
        with self._seq_lock:
            path = self.shot_image(shot_id, name)
            if any(o.image == name and o.status in ("queued", "running") for o in self.store.list_orbits(shot_id)):
                raise ValueError("this image's orbit is already rendering")
            ratio = (lambda w, h: w / h)(*media.image_size(path))
            width, height = next(size for least, size in ORBIT_SIZES if ratio >= least)
            orbit = self.store.create_orbit(shot_id=shot_id, image=name, seed=random.randint(1, 2**31 - 1),
                                            width=width, height=height)
        self._queue.put(ORBIT + orbit.id)
        return orbit

    def _orbit(self, orbit_id: str) -> OrbitRecord:
        orbit = self.store.get_orbit(orbit_id)
        if orbit is None:
            raise KeyError(orbit_id)
        return orbit

    def _run_orbit(self, orbit_id: str) -> None:
        with self._seq_lock:
            orbit = self.store.get_orbit(orbit_id)
            if orbit is None or orbit.status not in ("queued", "running"):
                return
            self._writing.add(orbit_id)
        try:
            self._execute_orbit(orbit)
        except Exception as e:  # noqa: BLE001 — surfaced to the user via the record; the still stays as it was
            with self._submit_lock:
                now = self.store.get_orbit(orbit_id)
                if now is not None and now.status != "cancelled":
                    self.store.update_orbit(orbit_id, status="failed", error=f"{type(e).__name__}: {e}"[:800])
        finally:
            self._handles.pop(orbit_id, None)
            with self._seq_lock:
                self._writing.discard(orbit_id)

    def _execute_orbit(self, orbit: OrbitRecord) -> None:
        spec = self.spec("orbit")
        client = self.client_for(backends.LOCAL)
        if orbit.comfy_job_id:
            handle = client.jobs.get(orbit.comfy_job_id)
        else:
            wf = client.workflows.from_file(spec.workflow_file)
            wf.set_input(*spec.slots["image"], client.assets.from_file(self.shot_image(orbit.shot_id, orbit.image)))
            for slot, value in {"width": orbit.width, "height": orbit.height, "seed": orbit.seed,
                                "filename_prefix": f"seam/{orbit.id}"}.items():
                wf.set_input(*spec.slots[slot], value)
            with self._submit_lock:
                if self._orbit(orbit.id).status == "cancelled":
                    return
                self.store.update_orbit(orbit.id, submitting_at=time.time())
                handle = client.submit(wf)
                self.store.update_orbit(orbit.id, status="running", comfy_job_id=handle.id)
        with self._submit_lock:
            self._handles[orbit.id] = handle
            if self._orbit(orbit.id).status == "cancelled":
                handle.cancel()
                return
        try:
            for ev in handle.events():  # the card's progress bar; wait() below reports the outcome
                if isinstance(ev, Progress) and ev.value is not None:
                    self.store.update_orbit(orbit.id, progress=round(float(ev.value), 3))
                elif isinstance(ev, StatusChange) and ev.status == "cancelled":
                    break
        except Exception:  # noqa: BLE001 — a lost event stream only loses the bar
            pass
        handle = handle.wait(timeout=self.settings.job_timeout_s)
        if self._orbit(orbit.id).status == "cancelled":
            return
        if handle.status != "succeeded":
            raise RuntimeError(f"comfy job {handle.id} {handle.status}: {handle.error}")
        folder = self.settings.outputs_dir / "orbits"
        folder.mkdir(parents=True, exist_ok=True)
        dst = folder / f"{orbit.id}.mp4"
        outputs = handle.get_outputs(spec.output_node)
        if not outputs:
            raise RuntimeError("the graph returned no video")
        outputs[0].to_file(dst)
        with self._submit_lock:
            if self._orbit(orbit.id).status == "cancelled":
                dst.unlink(missing_ok=True)
                return
            self.store.update_orbit(orbit.id, status="succeeded", video=dst.name, error=None)
        for old in self.store.list_orbits(orbit.shot_id):  # the new orbit replaces the image's older ones
            if old.image == orbit.image and old.id != orbit.id and old.status not in ("queued", "running"):
                self._remove_orbit(old)

    def _remove_orbit(self, orbit: OrbitRecord) -> None:
        if orbit.video:
            try:
                (self.settings.outputs_dir / "orbits" / orbit.video).unlink(missing_ok=True)
            except OSError:  # locked, on Windows: the row goes, the file waits for the next purge of its image
                pass
        self.store.remove_orbit(orbit.id)

    def cancel_orbit(self, orbit_id: str) -> OrbitRecord:
        orbit = self._orbit(orbit_id)
        if orbit.status not in ("queued", "running"):
            return orbit
        with self._submit_lock:
            handle = self._handles.get(orbit_id)
            error = None
            try:
                if handle is None and orbit.comfy_job_id:
                    handle = self.client_for(backends.LOCAL).jobs.get(orbit.comfy_job_id)
                if handle is not None:
                    handle.cancel()
            except Exception as e:  # noqa: BLE001 — the user asked to stop: say what we could not do
                error = f"could not stop it on ComfyUI ({type(e).__name__}); it may still be running there"
            return self.store.update_orbit(orbit_id, status="cancelled", **({"error": error} if error else {}))

    def orbit_frame(self, shot_id: str, name: str, frame: int) -> Path:
        """Exactly the frame the scrubber showed, from this image's finished orbit, as a PNG in a temp folder."""
        if not 0 <= frame < ORBIT_FRAMES:
            raise ValueError(f"frame must be 0-{ORBIT_FRAMES - 1}")
        self.shot_image(shot_id, name)  # the image itself must still be there (not trashed)
        done = [o for o in self.store.list_orbits(shot_id) if o.image == name and o.status == "succeeded" and o.video]
        if not done:
            raise ValueError("this image has no finished orbit")
        video = self.settings.outputs_dir / "orbits" / done[-1].video
        work = Path(tempfile.mkdtemp(dir=self.settings.outputs_dir))
        return media.frame_n(video, frame, work / f"{done[-1].id}_{frame}.png")

    def purge_shots(self, everything: bool = False) -> int:
        """Images the trash has held for TRASH_DAYS, or all of them. Shots used in a take are copies, so they stay."""
        cutoff = time.time() - TRASH_DAYS * 86400
        removed = 0
        with self._seq_lock:
            for shot in self.store.list_shots(limit=100_000):
                if shot.id in self._writing:
                    continue
                gone = []
                for image in shot.images:
                    if image["deleted_at"] and (everything or image["deleted_at"] < cutoff):
                        try:
                            (self.settings.outputs_dir / "shots" / image["name"]).unlink(missing_ok=True)
                            gone.append(image)
                        except OSError:  # locked, on Windows: it waits for the next purge
                            pass
                if not gone:
                    continue
                removed += len(gone)
                names = {i["name"] for i in gone}
                for orbit in self.store.list_orbits(shot.id):
                    if orbit.image in names and orbit.id not in self._writing:
                        self._remove_orbit(orbit)
                left = [i for i in shot.images if i not in gone]
                if left:
                    self.store.update_shot(shot.id, images=left)
                else:
                    self.store.remove_shot(shot.id)
        return removed

    def sequence_receipt(self, seq_id: str) -> dict[str, Any]:
        """How a sequence was made: the ordered join (clips by sha256, each take with its own receipt)."""
        seq = self.store.get_sequence(seq_id)
        if seq is None:
            raise KeyError(seq_id)
        parts: list[dict[str, Any]] = []
        cuts = [j is None for j in seq.segments]
        for i, shot in enumerate(seq.shots):
            if shot.get("clip"):
                clip = Path(shot["clip"])
                parts.append({"clip": clip.name, "sha256": hashlib.sha256(clip.read_bytes()).hexdigest()})
            elif hold := self._hold(shot, i, cuts):
                image = Path(shot["first"])
                parts.append({"still": image.name, "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                              "seconds": hold})
            if i < len(seq.segments) and seq.segments[i] is None:
                parts.append({"segment": i + 1, "cut": True})
            elif i < len(seq.segments):
                job = self.store.get(seq.segments[i])
                rec = (receipt.build_receipt(job, self.spec(self.model_for(TransitionRequest(**job.params))),
                                             self.settings.outputs_dir) if job and job.status == "succeeded" else None)
                parts.append({"segment": i + 1, "job_id": seq.segments[i], "receipt": rec})
        return {"seam_sequence": 1, "id": seq.id, "output": seq.output, "canvas": [seq.width, seq.height],
                "join": "hard cuts, every frame kept; parts scaled to cover the canvas and centre-cropped; "
                        "an image next to a cut shows for its seconds",
                "parts": parts}

    def prompt_for(self, req: TransitionRequest) -> str:
        if req.prompt_override.strip():
            return req.prompt_override.strip()
        return render_prompt(self.spec(self.model_for(req)).prompt, self.presets[req.preset],
                             a_ref=req.a_ref or "the first scene", b_ref=req.b_ref or "the second scene",
                             a_desc=req.a_desc or None, b_desc=req.b_desc or None, extra=req.prompt_extra,
                             seconds=req.seconds, story=req.story or "")

    @staticmethod
    def _where(job: JobRecord) -> str:
        return "Comfy Cloud" if Engine.backend_of(job) == backends.CLOUD else "ComfyUI"

    @staticmethod
    def _describe_cache(image: Path) -> Path:
        return image.with_name(f"{image.stem}.describe-v{shots.VERSION}.json")

    def cached_description(self, image: Path) -> tuple[bool, ShotText | None]:
        """(known, text): reading a cached shot submits nothing."""
        cache = self._describe_cache(image)
        if not cache.is_file():
            return False, None
        data = json.loads(cache.read_text(encoding="utf-8"))
        return True, ShotText(**data) if data else None

    def describe(self, image: Path, backend: str | None = None) -> ShotText | None:
        """Description + label for one shot, cached per upload (uploads are content-addressed)."""
        known, text = self.cached_description(image)
        if known:
            return text
        cache = self._describe_cache(image)
        if "describe" not in self._specs:
            self._specs["describe"] = load_spec(self.settings.workflows_dir, "describe")
        text = shots.run(self.client_for(backend or backends.current(self.settings)[0]), self._specs["describe"], image)
        tmp = cache.with_name(f"{cache.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(text.__dict__ if text else None), encoding="utf-8")
        tmp.replace(cache)  # a concurrent reader never sees half a file
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
                text = self.describe(Path(image), self.backend_of(job))
            except Exception as e:  # noqa: BLE001 — fall back to the generic prompt, never fail the render
                text = None
                changes.setdefault("extra", {**job.extra})["describe_error"] = f"{type(e).__name__}: {e}"[:300]
            changes[f"{side}_desc"] = text.desc if text else ""
            if getattr(req, f"{side}_ref") is None:
                changes[f"{side}_ref"] = text.ref if text else ""
        extra = changes.pop("extra", None)
        req = TransitionRequest(**{**req.to_params(), **changes})
        with self._submit_lock:  # a cancel made while the shots were read stays (no stale status written back)
            self.store.update(job.id, params=req.to_params(), **({"extra": extra} if extra else {}))
        return req

    # -- worker ---------------------------------------------------------------------------------
    def _run(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id.startswith(SEQ):
                self._reconcile(job_id[len(SEQ):])
                continue
            if job_id.startswith(SHOT):
                self._run_shot(job_id[len(SHOT):])
                continue
            if job_id.startswith(ORBIT):
                self._run_orbit(job_id[len(ORBIT):])
                continue
            with self._seq_lock:  # one step, so a purge never removes a take the worker is about to start
                job = self.store.get(job_id)
                run = job is not None and job.status not in ("cancelled", "succeeded", "failed")
                if run:
                    self._writing.add(job_id)
            if job is None:
                continue
            if run:
                try:
                    self._execute(job)
                except Exception as e:  # noqa: BLE001 — surfaced to the user via the job record
                    with self._submit_lock:
                        if self.store.get(job_id).status != "cancelled":
                            self.store.update(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:800])
                finally:
                    self._handles.pop(job_id, None)
                    with self._seq_lock:
                        self._writing.discard(job_id)
            if job.extra.get("sequence_id"):
                self._reconcile(job.extra["sequence_id"])

    def _execute(self, job: JobRecord) -> None:
        req = TransitionRequest(**job.params)
        t0 = time.monotonic()
        backend = self.backend_of(job)
        client = self.client_for(backend)
        if job.comfy_job_id:
            handle = client.jobs.get(job.comfy_job_id)
        else:
            req = self._fill_descriptions(job, req)
            spec = self.spec(self.model_for(req))
            width, height = ((req.width, req.height) if req.width and req.height
                             else media.output_size(*media.image_size(Path(req.image_a))))
            prompt = self.prompt_for(req)
            params: dict[str, Any] = {"prompt": prompt, "seed": req.seed,
                                      "seconds": req.seconds * spec.fps + 1 if spec.seconds_as_frames else req.seconds}
            if "width" in spec.slots:
                params.update(width=width, height=height)
            if req.preset == NO_EFFECT and "lora_strength" in spec.slots:
                params["lora_strength"] = 0  # an effect LoRA would put its own transition back in
            if "enhance_prompt" in spec.slots:
                # P0: the in-graph enhancer (local Gemma-4 E2B) fixes LTX-2.5 dissolves and end-jumps
                params["enhance_prompt"] = req.enhance
            wf = build(client, spec, Path(req.image_a), Path(req.image_b), params)
            # keep the exact graph this take runs, so its receipt and ComfyUI bundle never follow later template edits
            receipt.archive_graph(job.id, spec, params, self.settings.outputs_dir)
            with self._submit_lock:
                if self.store.get(job.id).status == "cancelled":  # cancelled while its shots were being read
                    return
                self.store.update(job.id, extra={**self.store.get(job.id).extra, "submitting_at": time.time()})
                # a partner node bills through the key; it is passed for this request only and stored nowhere
                handle = client.submit(wf, **({"api_key": backends.cloud_key(self.settings)[0]}
                                              if spec.kind == "partner" else {}))
                self.store.update(job.id, status="running", comfy_job_id=handle.id,
                                  extra={**self.store.get(job.id).extra, "prompt": prompt, "width": width,
                                         "height": height, "submitted_at": time.time()})
        with self._submit_lock:
            self._handles[job.id] = handle
            if self.store.get(job.id).status == "cancelled":  # cancelled between submit and here
                handle.cancel()
                return
        if backend == backends.CLOUD:
            handle = self._follow_cloud(job.id, handle)
        else:
            self._follow(job.id, handle)
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
        with self._submit_lock:
            if self.store.get(job.id).status == "cancelled":  # cancelled while the outputs were saved
                return
            self._finish(job, req, t0, mp4, info, merged, merge_error)
        try:
            self.write_receipt(job.id)
        except Exception:  # noqa: BLE001 — the take is done; the receipt is rebuilt on request
            pass

    def _follow(self, job_id: str, handle: Any) -> None:
        for ev in handle.events():
            if isinstance(ev, Progress) and ev.value is not None:
                self.store.update(job_id, progress=round(float(ev.value), 3))
            elif isinstance(ev, StatusChange):
                if ev.status == "cancelled":
                    break
                if ev.status in ("queued", "running"):  # the GPU queue is shared: reflect where we are
                    with self._submit_lock:
                        if self.store.get(job_id).status == "cancelled":
                            break
                        self.store.update(job_id, status=ev.status,
                                          extra={**self.store.get(job_id).extra, "queue_position": ev.queue_position})

    def _follow_cloud(self, job_id: str, handle: Any) -> Any:
        """Follow a Cloud take with a real deadline: the event stream reconnects forever and has none of its own, and
        a take nobody is watching keeps spending. Past the deadline the take is cancelled, never submitted again."""
        submitted = self.store.get(job_id).extra.get("submitted_at", time.time())
        # a take that finished while the server was down still gets looked at, so its paid output is not thrown away
        remaining = max(CLOUD_GRACE_S, submitted + self.settings.job_timeout_s - time.time())
        failure: list[BaseException] = []

        def follow() -> None:
            try:
                self._follow(job_id, handle)
            except BaseException as e:  # noqa: BLE001 — handed to the worker below
                failure.append(e)

        watcher = threading.Thread(target=follow, daemon=True, name=f"seam-follow-{job_id}")
        watcher.start()
        watcher.join(remaining)
        if watcher.is_alive():
            self._give_up(handle)
        if failure:
            raise failure[0]
        try:
            return handle.wait(timeout=CLOUD_GRACE_S)
        except TimeoutError:
            self._give_up(handle)
            raise

    def _give_up(self, handle: Any) -> None:
        limit = f"not finished after {self.settings.job_timeout_s:.0f} s"
        try:
            handle.cancel()
        except Exception as e:  # noqa: BLE001
            raise TimeoutError(f"{limit}, and cancelling it failed ({type(e).__name__}): it may still be running on "
                               "Comfy Cloud") from None
        raise TimeoutError(f"{limit}: cancelled on Comfy Cloud. Use New take to try again") from None

    def _finish(self, job: JobRecord, req: TransitionRequest, t0: float, mp4: Path, info: Any, merged: str | None,
                merge_error: str | None) -> None:
        self.store.update(job.id, status="succeeded", progress=1.0, output=mp4.name, poster=f"{job.id}.jpg",
                          sheet=f"{job.id}_sheet.png",
                          wall_s=round(time.time() - self.store.get(job.id).extra.get("submitted_at", time.time() - (time.monotonic() - t0)), 1),
                          extra={**self.store.get(job.id).extra, "width": info.width, "height": info.height,
                                 "duration": info.duration, "fps": info.fps,
                                 "has_audio": info.has_audio, **({"merged": merged} if merged else {}),
                                 **({"merge_error": merge_error} if merge_error else {})})

    def write_receipt(self, job_id: str) -> Path:
        """<id>.receipt.json next to the take: inputs (sha256), prompt, seed, model, graph."""
        job = self.store.get(job_id)
        dst = self.settings.outputs_dir / f"{job.id}.receipt.json"
        data = receipt.build_receipt(job, self.spec(self.model_for(TransitionRequest(**job.params))),
                                     self.settings.outputs_dir)
        dst.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return dst


CLIP_FORMAT = b"seam-clip-v1:"  # bump when normalize_clip changes


def store_clip(settings: Settings, src: Path, limits: tuple[float, float] = media.CLIP_SECONDS) -> tuple[Path, float]:
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
            os.replace(tmp, dst)  # a concurrent identical upload never sees a half-written clip
        finally:
            tmp.unlink(missing_ok=True)
    seconds = media.probe(dst).duration
    lo, hi = limits
    if not lo <= seconds <= hi + 0.05:  # checked on reuse too: the same file may have come in with a longer limit
        raise ValueError(f"clips must be {lo:g}-{hi:g} s long here (this one is {seconds:.1f} s)")
    return dst, seconds


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
