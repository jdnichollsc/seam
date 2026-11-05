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
    starred: bool = False
    deleted_at: float | None = None  # in the trash since then; its files stay until the trash is purged

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
    deleted_at: float | None = None
    origin: str | None = None    # the finished take it was continued from; a new take may replace it in `segments`

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShotRecord:
    """One generation in the Shots collection. Its images share a prompt and settings, and each has its own star and trash:
    images = [{"name": file in outputs/shots, "starred": bool, "deleted_at": float | None}]."""
    id: str
    status: str
    prompt: str
    aspect: str
    quality: str
    width: int
    height: int
    steps: int
    model: str
    seed: int
    count: int
    backend: str
    created_at: float
    updated_at: float
    images: list[dict[str, Any]] = field(default_factory=list)
    comfy_job_id: str | None = None
    parent_id: str | None = None
    error: str | None = None
    submitting_at: float | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OrbitRecord:
    """A 360° orbit of one shot image (ORB360): its own render, so cancel and restart recovery reach it after the shot
    succeeded. video = file in outputs/orbits once it succeeds."""
    id: str
    shot_id: str
    image: str
    status: str
    seed: int
    width: int
    height: int
    created_at: float
    updated_at: float
    comfy_job_id: str | None = None
    submitting_at: float | None = None
    error: str | None = None
    video: str | None = None
    progress: float = 0.0

    def public(self) -> dict[str, Any]:
        return asdict(self)


def _where(trashed: bool) -> str:
    return f"WHERE json_extract(data, '$.deleted_at') IS {'NOT ' if trashed else ''}NULL"


class JobStore:
    def __init__(self, db_path: Path) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()  # re-entrant: update() holds it across its get + save
        self._subs: list[queue.Queue] = []
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS sequences (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS shots (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS orbits (id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL)")
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

    def list(self, limit: int = 50, trashed: bool = False) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute(f"SELECT data FROM jobs {_where(trashed)} ORDER BY created DESC LIMIT ?",
                                    (limit,)).fetchall()
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

    def list_sequences(self, limit: int = 50, trashed: bool = False) -> list[SequenceRecord]:
        with self._lock:
            rows = self._db.execute(f"SELECT data FROM sequences {_where(trashed)} ORDER BY created DESC LIMIT ?",
                                    (limit,)).fetchall()
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

    def remove(self, record_id: str) -> None:
        """Forget a take or a sequence for good (the trash, once its files are gone)."""
        with self._lock:
            for table in ("jobs", "sequences"):
                self._db.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            self._db.commit()

    # -- shots ---------------------------------------------------------------------------------
    def _save_shot(self, shot: ShotRecord) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO shots (id, data, created) VALUES (?, ?, ?)",
                             (shot.id, json.dumps(shot.public()), shot.created_at))
            self._db.commit()

    def create_shot(self, **fields: Any) -> ShotRecord:
        now = time.time()
        shot = ShotRecord(id="s" + uuid.uuid4().hex[:11], status="queued", created_at=now, updated_at=now, **fields)
        self._save_shot(shot)
        self._publish(shot)
        return shot

    def get_shot(self, shot_id: str) -> ShotRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM shots WHERE id = ?", (shot_id,)).fetchone()
        return ShotRecord(**json.loads(row[0])) if row else None

    def list_shots(self, limit: int = 200) -> list[ShotRecord]:
        with self._lock:
            rows = self._db.execute("SELECT data FROM shots ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [ShotRecord(**json.loads(r[0])) for r in rows]

    def update_shot(self, shot_id: str, **changes: Any) -> ShotRecord:
        with self._lock:
            shot = self.get_shot(shot_id)
            if shot is None:
                raise KeyError(shot_id)
            for k, v in changes.items():
                setattr(shot, k, v)
            shot.updated_at = time.time()
            self._save_shot(shot)
        self._publish(shot)
        return shot

    def remove_shot(self, shot_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM shots WHERE id = ?", (shot_id,))
            self._db.commit()

    # -- orbits --------------------------------------------------------------------------------
    def _save_orbit(self, orbit: OrbitRecord) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO orbits (id, data, created) VALUES (?, ?, ?)",
                             (orbit.id, json.dumps(orbit.public()), orbit.created_at))
            self._db.commit()

    def create_orbit(self, **fields: Any) -> OrbitRecord:
        now = time.time()
        orbit = OrbitRecord(id="o" + uuid.uuid4().hex[:11], status="queued", created_at=now, updated_at=now, **fields)
        self._save_orbit(orbit)
        self._publish(orbit)
        return orbit

    def get_orbit(self, orbit_id: str) -> OrbitRecord | None:
        with self._lock:
            row = self._db.execute("SELECT data FROM orbits WHERE id = ?", (orbit_id,)).fetchone()
        return OrbitRecord(**json.loads(row[0])) if row else None

    def list_orbits(self, shot_id: str | None = None) -> list[OrbitRecord]:
        """Oldest first, so the last one per image is the newest."""
        with self._lock:
            if shot_id is None:
                rows = self._db.execute("SELECT data FROM orbits ORDER BY created").fetchall()
            else:
                rows = self._db.execute("SELECT data FROM orbits WHERE json_extract(data, '$.shot_id') = ? ORDER BY created",
                                        (shot_id,)).fetchall()
        return [OrbitRecord(**json.loads(r[0])) for r in rows]

    def update_orbit(self, orbit_id: str, **changes: Any) -> OrbitRecord:
        with self._lock:
            orbit = self.get_orbit(orbit_id)
            if orbit is None:
                raise KeyError(orbit_id)
            for k, v in changes.items():
                setattr(orbit, k, v)
            orbit.updated_at = time.time()
            self._save_orbit(orbit)
        self._publish(orbit)
        return orbit

    def remove_orbit(self, orbit_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM orbits WHERE id = ?", (orbit_id,))
            self._db.commit()

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

    def _publish(self, record: JobRecord | SequenceRecord | ShotRecord | OrbitRecord) -> None:
        """Subscribers get (kind, record) with kind "job", "sequence", "shot" or "orbit"."""
        kind = ("sequence" if isinstance(record, SequenceRecord) else "shot" if isinstance(record, ShotRecord)
                else "orbit" if isinstance(record, OrbitRecord) else "job")
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait((kind, record.public()))
            except queue.Full:
                pass
