"""Transition engine: request → comfy-sdk job → MP4 + poster + contact sheet, one job at a time."""

from __future__ import annotations

import hashlib
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
from .jobs import JobRecord, JobStore, SequenceRecord
from .presets import Preset, load_presets, render_prompt
from .workflows import WorkflowSpec, build, load_spec

SUBMIT_UNKNOWN = ("server restarted while sending the job to ComfyUI; it may still have rendered there "
                  "(check ComfyUI's queue/output). Use New take to render it again.")
SECONDS_CHOICES = (5, 8, 10)  # what the graphs render; each launch effect lists the lengths it is qualified at
PUBLIC_MODELS = ("l25", "l23")  # LTX family only: other models were research-only and never ship
SEQUENCE_SHOTS = (2, 8)
SEQ = "seq:"  # worker queue items for sequences (reconcile/join), next to plain job ids


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
    model: str | None = None    # override the preset's model (l25 | l23)
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
        if client is None:
            os.environ.setdefault("COMFY_BASE_URL", settings.comfy_base_url)  # the SDK reads its endpoint from env
            client = Comfy(client_info="seam/0.1")
        self.client = client
        self._specs: dict[str, WorkflowSpec] = {}
        self.presets: dict[str, Preset] = load_presets(settings.presets_file)
        self._queue: queue.Queue[str] = queue.Queue()
        self._handles: dict[str, Any] = {}
        self._seq_lock = threading.Lock()  # sequence pointer swaps vs the worker's reconcile
        self._submit_lock = threading.Lock()  # cancel vs submit: a cancelled take never reaches the GPU
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
        for seq in self.store.list_sequences(limit=10_000):  # e.g. died after the last segment, before the join
            if seq.status in ("running", "assembling"):
                self._queue.put(SEQ + seq.id)
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
        if (req.width is None) != (req.height is None) or any(
                v is not None and not (256 <= v <= 2048 and v % 32 == 0) for v in (req.width, req.height)):
            raise ValueError("width and height go together: multiples of 32 between 256 and 2048")

    def submit(self, req: TransitionRequest, parent_id: str | None = None, extra: dict[str, Any] | None = None,
               enqueue: bool = True) -> JobRecord:
        if req.seed is None:
            req.seed = random.randint(1, 2**31 - 1)
        self.validate(req)
        req.model = self.model_for(req)  # pin the resolved model on the job
        job = self.store.create(req.to_params(), parent_id=parent_id, extra=extra)
        if enqueue:
            self._queue.put(job.id)
        return job

    def refine(self, job_id: str, changes: dict[str, Any], extra: dict[str, Any] | None = None,
               enqueue: bool = True) -> JobRecord:
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
        return self.submit(TransitionRequest(**params), parent_id=job_id, extra=extra, enqueue=enqueue)

    def cancel(self, job_id: str) -> JobRecord:
        job = self.store.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._submit_lock:  # vs submit, handle registration and the final status write in _execute
            handle = self._handles.get(job_id)  # a submitted take can read "queued" again (shared GPU queue)
            if handle is not None:
                handle.cancel()
            return self.store.update(job_id, status="cancelled")

    # -- sequences ------------------------------------------------------------------------------
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
        reqs = []
        for i, seg in enumerate(segments):
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
            jobs = [self.submit(r, enqueue=False) for r in reqs]
            seq = self.store.create_sequence([{"first": s["first"], "last": s["last"], "clip": s.get("clip")}
                                              for s in shots], [j.id for j in jobs], width, height, story.strip())
            for i, j in enumerate(jobs):
                self.store.update(j.id, extra={**j.extra, "sequence_id": seq.id, "segment": i})
        for j in jobs:
            self._queue.put(j.id)
        return seq

    def refine_segment(self, seq_id: str, index: int, changes: dict[str, Any]) -> SequenceRecord:
        """A new take of one segment (same rules as refine); the others are reused and the sequence joins again."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None:
                raise KeyError(seq_id)
            if seq.status in ("running", "assembling"):
                raise SequenceBusy(f"sequence {seq_id} is {seq.status}; re-roll a segment once it settles")
            if not 0 <= index < len(seq.segments):
                raise ValueError(f"segment must be 1-{len(seq.segments)}")
            changes = {**{k: v for k, v in changes.items()
                          if k not in ("width", "height", "clip_a", "clip_b", "story")},
                       "width": seq.width, "height": seq.height, "clip_a": "", "clip_b": "",  # canvas stays, no clips
                       "story": seq.story}  # a new take uses the sequence's story as it is now
            job = self.refine(seq.segments[index], changes, extra={"sequence_id": seq.id, "segment": index},
                              enqueue=False)
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
            if seq is None:
                raise KeyError(seq_id)
            job = self._append_locked(seq, shot, segment)
        self._queue.put(job.id)
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
        self.validate(self._next_request(shots[-1]["last"], shot, segment, info.width, info.height))
        with self._seq_lock:
            seq = self.store.create_sequence(shots, [job.id], info.width, info.height, story.strip())
            seq = self.store.update_sequence(seq.id, status="succeeded")  # one finished take: nothing to wait for
            try:
                new = self._append_locked(seq, shot, segment)
            except Exception:
                self.store.update_sequence(seq.id, status="cancelled", error="could not add the next shot")
                raise
        self._queue.put(new.id)
        return self.store.get_sequence(seq.id)

    def _append_locked(self, seq: SequenceRecord, shot: dict[str, Any], segment: dict[str, Any]) -> JobRecord:
        """Caller holds _seq_lock. Validates everything before anything is written; queues nothing."""
        if seq.status != "succeeded":
            raise SequenceBusy(f"sequence {seq.id} is {seq.status}; add a shot once it has finished")
        if len(seq.shots) >= SEQUENCE_SHOTS[1]:
            raise ValueError(f"a sequence has at most {SEQUENCE_SHOTS[1]} shots")
        for s_ in [*seq.shots, shot]:
            for key in ("first", "last", "clip"):
                if s_.get(key) and not Path(s_[key]).is_file():
                    raise ValueError(f"file not found: {Path(s_[key]).name}")
        jobs = [self.store.get(j) for j in seq.segments]
        if any(j is None or not j.output or not (self.settings.outputs_dir / j.output).is_file() for j in jobs):
            raise ValueError("a take of this sequence is missing on disk; make a new take of it first")
        prev = jobs[-1]
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
                                       shots=[*seq.shots, {"first": shot["first"], "last": shot["last"],
                                                           "clip": shot.get("clip")}])
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
            if seq is None:
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
            plan: list[str | TransitionRequest] = []
            missing: list[str] = []
            for k in range(len(order) - 1):
                pair = (order[k], order[k + 1])
                if k not in transitions and pair in kept:
                    job = self.store.get(kept[pair])
                    if job is None or job.status != "succeeded" or not job.output \
                            or not (self.settings.outputs_dir / job.output).is_file():
                        raise ValueError(f"the transition {pair[0] + 1} → {pair[1] + 1} has no finished take; make a "
                                         "new take of it first, or give it a transition here")
                    plan.append(kept[pair])
                    continue
                if k not in transitions:
                    missing.append(f"{pair[0] + 1} → {pair[1] + 1}")
                    continue
                req = self._next_request(shots[k]["last"], shots[k + 1], {**transitions[k], "story": story},
                                         seq.width, seq.height)
                try:
                    self.validate(req)
                except ValueError as e:
                    raise ValueError(f"transition {k + 1} ({pair[0] + 1} → {pair[1] + 1}): {e}") from e
                plan.append(req)
            if missing:
                raise ValueError(f"pick an effect for the new transition{'s' if len(missing) > 1 else ''}: "
                                 + ", ".join(missing))
            for s_ in shots:
                for key in ("first", "last", "clip"):
                    if s_.get(key) and not Path(s_[key]).is_file():
                        raise ValueError(f"file not found: {Path(s_[key]).name}")
            segments = list(plan) if all(isinstance(x, str) for x in plan) else None
            if segments == seq.segments and order == list(range(n)):
                return self.store.update_sequence(seq_id, story=story) if story != seq.story else seq
            try:
                segments = []
                for k, x in enumerate(plan):
                    if isinstance(x, str):
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
        shot = {"first": seq.shots[i]["first"], "last": seq.shots[i]["last"], "clip": seq.shots[i].get("clip")}
        if shot["clip"] and shot["first"] == shot["last"] and Path(shot["clip"]).is_file():
            shot["first"] = str(store_upload(self.settings, media.clip_frame(
                Path(shot["clip"]), self.settings.uploads_dir / f"{seq.id}_{i}_clip_first.png", last=False)))
        return shot

    def cancel_sequence(self, seq_id: str) -> SequenceRecord:
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None:
                raise KeyError(seq_id)
            if seq.status in ("succeeded", "failed", "cancelled"):
                return seq
            seq = self.store.update_sequence(seq_id, status="cancelled")
            for job_id in seq.segments:
                job = self.store.get(job_id)
                if job and job.status in ("queued", "running"):
                    self.cancel(job_id)
        return seq

    def assemble(self, seq_id: str) -> SequenceRecord:
        """Join again from the segments' existing outputs (after a failed join or a cancel)."""
        with self._seq_lock:
            seq = self.store.get_sequence(seq_id)
            if seq is None:
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
            if seq is None or seq.status == "cancelled":
                return
            jobs = [self.store.get(j) for j in seq.segments]
            status = [j.status if j else "failed" for j in jobs]
            if any(s in ("queued", "running") for s in status):
                return  # still rendering; the next segment to finish reconciles again
            bad = [i for i, s in enumerate(status) if s != "succeeded"]
            if bad:
                i = bad[0]
                why = (jobs[i].error if jobs[i] else None) or status[i]
                self.store.update_sequence(seq_id, status="failed", error=f"segment {i + 1}: {why}"[:600])
                return
            key = hashlib.sha256(",".join(seq.segments).encode()).hexdigest()[:8]
            name = f"{seq.id}_{key}.mp4"  # one file per set of takes: a re-roll gets a new URL
            if seq.output == name and (self.settings.outputs_dir / name).is_file():
                self.store.update_sequence(seq_id, status="succeeded", error=None)
                return
            seq = self.store.update_sequence(seq_id, status="assembling")
        out_dir = self.settings.outputs_dir
        dst = out_dir / name
        try:
            parts: list[Path] = []
            for i, shot in enumerate(seq.shots):  # clip0?, take0, clip1?, take1, …, clipN?
                if shot.get("clip"):
                    parts.append(Path(shot["clip"]))
                if i < len(jobs):
                    parts.append(out_dir / jobs[i].output)
            fps = media.probe(out_dir / jobs[0].output).fps
            media.join(parts, dst, seq.width, seq.height, fps)
            media.poster(dst, out_dir / f"{dst.stem}.jpg")
            media.contact_sheet(dst, out_dir / f"{dst.stem}_sheet.png", n=6 * len(jobs), height=160, cols=6)
        except Exception as e:  # noqa: BLE001 — the takes stay; assemble() joins again
            detail = getattr(e, "stderr", None) or e
            print(f"seam: joining sequence {seq_id} failed: {type(e).__name__}: {detail!s}"[:2000], flush=True)
            with self._seq_lock:
                cur = self.store.get_sequence(seq_id)
                if cur and cur.status == "assembling" and cur.segments == seq.segments:
                    self.store.update_sequence(seq_id, status="failed", error=(
                        f"could not join the takes ({type(e).__name__}); the server log has the details. "
                        "Join again, or make a new take of a segment"))
            return
        with self._seq_lock:
            cur = self.store.get_sequence(seq_id)
            if cur and cur.status == "assembling" and cur.segments == seq.segments:  # not cancelled meanwhile
                self.store.update_sequence(seq_id, status="succeeded", output=name, poster=f"{dst.stem}.jpg",
                                           sheet=f"{dst.stem}_sheet.png", error=None)

    def sequence_receipt(self, seq_id: str) -> dict[str, Any]:
        """How a sequence was made: the ordered join (clips by sha256, each take with its own receipt)."""
        seq = self.store.get_sequence(seq_id)
        if seq is None:
            raise KeyError(seq_id)
        parts: list[dict[str, Any]] = []
        for i, shot in enumerate(seq.shots):
            if shot.get("clip"):
                clip = Path(shot["clip"])
                parts.append({"clip": clip.name, "sha256": hashlib.sha256(clip.read_bytes()).hexdigest()})
            if i < len(seq.segments):
                job = self.store.get(seq.segments[i])
                rec = (receipt.build_receipt(job, self.spec(self.model_for(TransitionRequest(**job.params))),
                                             self.settings.outputs_dir) if job and job.status == "succeeded" else None)
                parts.append({"segment": i + 1, "job_id": seq.segments[i], "receipt": rec})
        return {"seam_sequence": 1, "id": seq.id, "output": seq.output, "canvas": [seq.width, seq.height],
                "join": "hard cuts, every frame kept; parts scaled to cover the canvas and centre-cropped",
                "parts": parts}

    def prompt_for(self, req: TransitionRequest) -> str:
        if req.prompt_override.strip():
            return req.prompt_override.strip()
        return render_prompt(self.model_for(req), self.presets[req.preset],
                             a_ref=req.a_ref or "the first scene", b_ref=req.b_ref or "the second scene",
                             a_desc=req.a_desc or None, b_desc=req.b_desc or None, extra=req.prompt_extra,
                             seconds=req.seconds, story=req.story or "")

    def describe(self, image: Path) -> ShotText | None:
        """Description + label for one shot, cached per upload (uploads are content-addressed)."""
        cache = image.with_name(f"{image.stem}.describe-v{shots.VERSION}.json")
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return ShotText(**data) if data else None
        if "describe" not in self._specs:
            self._specs["describe"] = load_spec(self.settings.workflows_dir, "describe")
        text = shots.run(self.client, self._specs["describe"], image)
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
                text = self.describe(Path(image))
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
            job = self.store.get(job_id)
            if job is None:
                continue
            if job.status not in ("cancelled", "succeeded", "failed"):
                try:
                    self._execute(job)
                except Exception as e:  # noqa: BLE001 — surfaced to the user via the job record
                    with self._submit_lock:
                        if self.store.get(job_id).status != "cancelled":
                            self.store.update(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:800])
                finally:
                    self._handles.pop(job_id, None)
            if job.extra.get("sequence_id"):
                self._reconcile(job.extra["sequence_id"])

    def _execute(self, job: JobRecord) -> None:
        req = TransitionRequest(**job.params)
        t0 = time.monotonic()
        if job.comfy_job_id:
            handle = self.client.jobs.get(job.comfy_job_id)
        else:
            req = self._fill_descriptions(job, req)
            spec = self.spec(self.model_for(req))
            width, height = ((req.width, req.height) if req.width and req.height
                             else media.output_size(*media.image_size(Path(req.image_a))))
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
            with self._submit_lock:
                if self.store.get(job.id).status == "cancelled":  # cancelled while its shots were being read
                    return
                self.store.update(job.id, extra={**self.store.get(job.id).extra, "submitting_at": time.time()})
                handle = self.client.submit(wf)
                self.store.update(job.id, status="running", comfy_job_id=handle.id,
                                  extra={**self.store.get(job.id).extra, "prompt": prompt, "width": width,
                                         "height": height, "submitted_at": time.time()})
        with self._submit_lock:
            self._handles[job.id] = handle
            if self.store.get(job.id).status == "cancelled":  # cancelled between submit and here
                handle.cancel()
                return
        for ev in handle.events():
            if isinstance(ev, Progress) and ev.value is not None:
                self.store.update(job.id, progress=round(float(ev.value), 3))
            elif isinstance(ev, StatusChange):
                if ev.status == "cancelled":
                    break
                if ev.status in ("queued", "running"):  # the GPU queue is shared: reflect where we are
                    with self._submit_lock:
                        if self.store.get(job.id).status == "cancelled":
                            break
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
        with self._submit_lock:
            if self.store.get(job.id).status == "cancelled":  # cancelled while the outputs were saved
                return
            self._finish(job, req, t0, mp4, info, merged, merge_error)
        try:
            self.write_receipt(job.id)
        except Exception:  # noqa: BLE001 — the take is done; the receipt is rebuilt on request
            pass

    def _finish(self, job: JobRecord, req: TransitionRequest, t0: float, mp4: Path, info: Any, merged: str | None,
                merge_error: str | None) -> None:
        self.store.update(job.id, status="succeeded", progress=1.0, output=mp4.name, poster=f"{job.id}.jpg",
                          sheet=f"{job.id}_sheet.png",
                          wall_s=round(time.time() - self.store.get(job.id).extra.get("submitted_at", time.time() - (time.monotonic() - t0)), 1),
                          extra={**self.store.get(job.id).extra, "duration": info.duration, "fps": info.fps,
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
