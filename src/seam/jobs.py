"""SQLite job store + in-process event fan-out (used by the REST API's SSE stream)."""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "interrupted")
SEQUENCE_STATUSES = ("running", "assembling", "succeeded", "failed", "cancelled")


@dataclass
class JobRecord:
    id: str
    status: str
    params: dict[str, Any]
    created_at: float
    updated_at: float
    parent_id: str | None = None  # refine_job lineage
    comfy_job_id: str | None = None
    progress: float = 0.0
    output: str | None = None
    poster: str | None = None
    sheet: str | None = None
    error: str | None = None
    wall_s: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SequenceRecord:
    """Shots S0..Sn joined by n transition jobs (segment i: last frame of Si → first frame of Si+1)."""
    id: str
    status: str
    shots: list[dict[str, Any]]  # {first, last, clip}: an image has first == last and no clip
    segments: list[str]          # current job id per segment; a re-roll swaps the pointer
    width: int                   # one canvas for every segment, from S0
    height: int
    created_at: float
    updated_at: float
    output: str | None = None
    poster: str | None = None
    sheet: str | None = None
    error: str | None = None
    story: str = ""              # the idea of the whole sequence; each take keeps the story it was made with

    def public(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()  # re-entrant: update() holds it across its get + save
        self._subs: list[queue.Queue] = []
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS sequences (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            self._db.commit()

    # -- persistence ---------------------------------------------------------------------------
    def _save(self, job: JobRecord) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO jobs (id, data, created) VALUES (?, ?, ?)",
                             (job.id, json.dumps(job.public()), job.created_at))
            self._db.commit()

    def create(self, params: dict[str, Any], parent_id: str | None = None,
               extra: dict[str, Any] | None = None) -> JobRecord:
        now = time.time()
        job = JobRecord(id=uuid.uuid4().hex[:12], status="queued", params=params, created_at=now, updated_at=now,
                        parent_id=parent_id, extra=dict(extra or {}))
        self._save(job)
        self._publish(job)
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRecord(**json.loads(row[0])) if row else None

    def list(self, limit: int = 50) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [JobRecord(**json.loads(r[0])) for r in rows]

    def update(self, job_id: str, **changes: Any) -> JobRecord:
        with self._lock:  # one read-modify-write: a concurrent update (e.g. a cancel) is never overwritten
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            for k, v in changes.items():
                setattr(job, k, v)
            job.updated_at = time.time()
            self._save(job)
        self._publish(job)
        return job

    # -- sequences -----------------------------------------------------------------------------
    def _save_sequence(self, seq: SequenceRecord) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO sequences (id, data, created) VALUES (?, ?, ?)",
                             (seq.id, json.dumps(seq.public()), seq.created_at))
            self._db.commit()

    def create_sequence(self, shots: list[dict[str, Any]], segments: list[str], width: int,
                        height: int, story: str = "") -> SequenceRecord:
        now = time.time()
        seq = SequenceRecord(id="s" + uuid.uuid4().hex[:11], status="running", shots=shots, segments=segments,
                             width=width, height=height, created_at=now, updated_at=now, story=story)
        self._save_sequence(seq)
        self._publish(seq)
        return seq

    def get_sequence(self, seq_id: str) -> SequenceRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM sequences WHERE id = ?", (seq_id,)).fetchone()
        return SequenceRecord(**json.loads(row[0])) if row else None

    def list_sequences(self, limit: int = 50) -> list[SequenceRecord]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM sequences ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [SequenceRecord(**json.loads(r[0])) for r in rows]

    def update_sequence(self, seq_id: str, **changes: Any) -> SequenceRecord:
        with self._lock:
            seq = self.get_sequence(seq_id)
            if seq is None:
                raise KeyError(seq_id)
            for k, v in changes.items():
                setattr(seq, k, v)
            seq.updated_at = time.time()
            self._save_sequence(seq)
        self._publish(seq)
        return seq

    def mark_interrupted(self) -> list[str]:
        """On startup: anything left queued/running by a previous process becomes 'interrupted'."""
        ids = [j.id for j in self.list(limit=10_000) if j.status in ("queued", "running")]
        for i in ids:
            self.update(i, status="interrupted", error="server restarted before the job finished")
        return ids

    # -- events --------------------------------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _publish(self, record: JobRecord | SequenceRecord) -> None:
        """Subscribers get (kind, record) with kind "job" or "sequence"."""
        kind = "sequence" if isinstance(record, SequenceRecord) else "job"
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((kind, record.public()))
            except queue.Full:
                pass
