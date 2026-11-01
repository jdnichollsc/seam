from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from seam import server
from seam.config import Settings
from seam.engine import TRASH_DAYS, Engine
from seam.jobs import JobStore


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = replace(Settings(), data_dir=tmp_path / "data")
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e


def _take(e: Engine, status: str = "succeeded", files: bool = True, **extra: Any):
    job = e.store.create({"preset": "whip_pan", "model": e.settings.model, "seconds": 5, "seed": 7,
                          "prompt_extra": "slow", "image_a": "a.png", "image_b": "b.png"}, extra=extra)
    names = [f"{job.id}.mp4", f"{job.id}.jpg", f"{job.id}_sheet.png", f"{job.id}_sheet2.png", f"{job.id}_full.mp4",
             f"{job.id}.receipt.json", f"{job.id}.graph.json"]
    if files:
        for n in names:
            (e.settings.outputs_dir / n).write_bytes(b"x")
    return e.store.update(job.id, status=status, output=names[0], poster=names[1])


def _sequence(e: Engine, *takes, status: str = "succeeded"):
    seq = e.store.create_sequence([{"first": "a", "last": "a", "clip": None}] * (len(takes) + 1),
                                  [t.id for t in takes], 512, 512)
    for t in takes:
        e.store.update(t.id, extra={**e.store.get(t.id).extra, "sequence_id": seq.id})
    for suffix in ("_abc.mp4", "_abc.jpg", "_abc_sheet.png"):
        (e.settings.outputs_dir / f"{seq.id}{suffix}").write_bytes(b"x")
    return e.store.update_sequence(seq.id, status=status)


def _files(e: Engine, record_id: str) -> list[Path]:
    return [*e.settings.outputs_dir.glob(f"{record_id}.*"), *e.settings.outputs_dir.glob(f"{record_id}_*")]


def test_trash_hides_a_take_keeps_its_files_and_restores(seam):
    c, e = seam
    job = _take(e)
    assert c.post(f"/api/jobs/{job.id}/trash").status_code == 200
    assert job.id not in [j["id"] for j in c.get("/api/jobs").json()]
    assert len(_files(e, job.id)) == 7
    assert [j["id"] for j in c.get("/api/trash").json()["jobs"]] == [job.id]
    assert c.post(f"/api/jobs/{job.id}/restore").json()["deleted_at"] is None
    assert job.id in [j["id"] for j in c.get("/api/jobs").json()]


def test_a_full_jobs_page_is_not_cut_short_by_the_trash(seam):
    c, e = seam
    kept = _take(e, files=False)
    for _ in range(3):
        c.post(f"/api/jobs/{_take(e, files=False).id}/trash")
    assert [j["id"] for j in c.get("/api/jobs?limit=1").json()] == [kept.id]


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_take_in_flight_cannot_be_trashed(seam, status):
    c, e = seam
    assert c.post(f"/api/jobs/{_take(e, status=status).id}/trash").status_code == 409


def test_a_take_a_sequence_joins_is_protected_but_a_replaced_one_is_not(seam):
    c, e = seam
    current, replaced = _take(e), _take(e)
    seq = _sequence(e, current)
    e.store.update(replaced.id, extra={"sequence_id": seq.id})
    assert c.post(f"/api/jobs/{current.id}/trash").status_code == 409
    assert c.post(f"/api/jobs/{replaced.id}/trash").status_code == 200


def test_trashing_a_sequence_takes_its_takes_except_starred_and_shared(seam):
    c, e = seam
    plain, starred, shared, replaced = _take(e), _take(e), _take(e), _take(e)
    c.post(f"/api/jobs/{starred.id}/star", json={"starred": True})
    seq = _sequence(e, plain, starred, shared)
    e.store.update(replaced.id, extra={"sequence_id": seq.id})
    other = _sequence(e, shared)
    assert c.post(f"/api/sequences/{seq.id}/trash").status_code == 200
    assert seq.id not in [s["id"] for s in c.get("/api/sequences").json()]
    live = {j["id"] for j in c.get("/api/jobs").json()}
    assert live == {starred.id, shared.id}
    assert e.store.get_sequence(other.id).deleted_at is None
    c.post(f"/api/sequences/{seq.id}/restore")
    assert {j["id"] for j in c.get("/api/jobs").json()} == {plain.id, starred.id, shared.id, replaced.id}
    assert "trashed_with" not in e.store.get(plain.id).extra


@pytest.mark.parametrize("status", ["running", "assembling"])
def test_a_sequence_in_flight_cannot_be_trashed(seam, status):
    c, e = seam
    assert c.post(f"/api/sequences/{_sequence(e, _take(e), status=status).id}/trash").status_code == 409


def test_a_trashed_sequence_or_take_takes_no_more_changes(seam):
    c, e = seam
    take = _take(e)
    seq = _sequence(e, take)
    c.post(f"/api/sequences/{seq.id}/trash")
    assert c.post(f"/api/sequences/{seq.id}/assemble").status_code == 404
    assert c.post(f"/api/sequences/{seq.id}/segments/1/refine", json={}).status_code == 404
    loose = _take(e)
    c.post(f"/api/jobs/{loose.id}/trash")
    assert c.post(f"/api/jobs/{loose.id}/refine", json={}).status_code in (409, 422)


def test_the_worker_does_not_join_a_trashed_sequence(seam):
    _, e = seam
    seq = _sequence(e, _take(e), status="cancelled")
    e.trash_sequence(seq.id)
    e.store.update_sequence(seq.id, status="running")
    e._reconcile(seq.id)
    assert e.store.get_sequence(seq.id).status == "running"


def test_empty_trash_removes_files_and_records_but_not_uploads(seam):
    c, e = seam
    upload = e.settings.uploads_dir / "shared.png"
    upload.write_bytes(b"x")
    take = _take(e)
    seq = _sequence(e, take)
    c.post(f"/api/sequences/{seq.id}/trash")
    assert c.delete("/api/trash").json() == {"removed": 2, "kept": 0}
    assert not _files(e, take.id) and not _files(e, seq.id)
    assert e.store.get(take.id) is None and e.store.get_sequence(seq.id) is None
    assert upload.is_file()


def test_purge_waits_for_the_retention_and_clears_a_starred_survivor(seam):
    c, e = seam
    starred, plain = _take(e), _take(e)
    c.post(f"/api/jobs/{starred.id}/star", json={"starred": True})
    seq = _sequence(e, starred, plain)
    c.post(f"/api/sequences/{seq.id}/trash")
    assert e.purge_trash() == {"removed": 0, "kept": 0}
    old = time.time() - TRASH_DAYS * 86400 - 1
    e.store.update_sequence(seq.id, deleted_at=old)
    e.store.update(plain.id, deleted_at=old)
    assert e.purge_trash() == {"removed": 2, "kept": 0}
    assert "sequence_id" not in e.store.get(starred.id).extra
    assert len(_files(e, starred.id)) == 7


def test_purge_skips_what_the_worker_is_writing_and_takes_held_by_a_trashed_sequence(seam):
    _, e = seam
    writing, held = _take(e, status="cancelled"), _take(e)
    e.trash_job(writing.id)
    seq = _sequence(e, held)
    e.trash_sequence(seq.id)
    e.store.update_sequence(seq.id, deleted_at=time.time() + 3600)  # not due, still holds its take
    e._writing.add(writing.id)
    e.store.update(held.id, deleted_at=time.time() - TRASH_DAYS * 86400 - 1)
    e.store.update(writing.id, deleted_at=time.time() - TRASH_DAYS * 86400 - 1)
    assert e.purge_trash() == {"removed": 0, "kept": 2}
    e._writing.clear()
    assert e.purge_trash()["removed"] == 1


def test_a_file_still_open_keeps_the_record_for_the_next_purge(seam, monkeypatch):
    c, e = seam
    job = _take(e)
    c.post(f"/api/jobs/{job.id}/trash")
    monkeypatch.setattr(e, "_remove_files", lambda _id: False)
    assert c.delete("/api/trash").json() == {"removed": 0, "kept": 1}
    assert e.store.get(job.id) is not None


def test_a_recipe_keeps_the_settings_and_outlives_its_take(seam):
    c, e = seam
    job = _take(e)
    e.store.update(job.id, params={**job.params, "prompt_override": "never saved"})
    r = c.post("/api/recipes", json={"job_id": job.id, "name": " Slow whip "}).json()
    assert (r["name"], r["preset"], r["seconds"], r["prompt_extra"], r["seed"]) == ("Slow whip", "whip_pan", 5, "slow", 7)
    assert "prompt_override" not in r and r["problem"] is None and r["poster_url"].endswith(".jpg")
    c.post(f"/api/jobs/{job.id}/trash")
    c.delete("/api/trash")
    kept = c.get("/api/recipes").json()
    assert [x["id"] for x in kept] == [r["id"]] and "poster_url" not in kept[0]
    assert c.delete(f"/api/recipes/{r['id']}").status_code == 204
    assert c.delete(f"/api/recipes/{r['id']}").status_code == 404


def test_a_recipe_says_when_it_can_no_longer_render(seam):
    c, e = seam
    job = _take(e)
    rid = c.post("/api/recipes", json={"job_id": job.id, "name": "x"}).json()["id"]
    e.presets.pop("whip_pan")
    row = next(x for x in c.get("/api/recipes").json() if x["id"] == rid)
    assert "no longer" in row["problem"]


def test_only_a_finished_take_makes_a_recipe(seam):
    c, e = seam
    assert c.post("/api/recipes", json={"job_id": _take(e, status="failed").id, "name": "x"}).status_code == 409
    assert c.post("/api/recipes", json={"job_id": "nope", "name": "x"}).status_code == 404


def test_a_take_purged_before_the_worker_reaches_it_does_not_stop_the_worker(seam):
    c, e = seam
    gone = _take(e, status="cancelled")
    c.post(f"/api/jobs/{gone.id}/trash")
    c.delete("/api/trash")
    e._queue.put(gone.id)
    e._worker.start()
    for _ in range(50):
        if e._queue.empty():
            break
        time.sleep(0.02)
    time.sleep(0.05)
    assert e._worker.is_alive()


def test_a_take_left_behind_by_a_purged_sequence_can_still_be_restored(seam):
    c, e = seam
    take = _take(e, status="cancelled")
    seq = _sequence(e, take, status="cancelled")
    c.post(f"/api/sequences/{seq.id}/trash")
    e._writing.add(take.id)
    assert c.delete("/api/trash").json() == {"removed": 1, "kept": 1}
    row = c.get("/api/trash").json()
    assert [j["id"] for j in row["jobs"]] == [take.id] and row["sequences"] == []
    assert c.post(f"/api/jobs/{take.id}/restore").json()["deleted_at"] is None


def test_the_take_a_sequence_was_continued_from_leaves_with_it_once_replaced(seam):
    c, e = seam
    origin, current = _take(e), _take(e)
    seq = e.store.create_sequence([{"first": "a", "last": "a", "clip": None}] * 2, [current.id], 512, 512)
    e.store.update_sequence(seq.id, status="succeeded", origin=origin.id)
    assert c.post(f"/api/sequences/{seq.id}/trash").status_code == 200
    assert e.store.get(origin.id).deleted_at is not None
    c.post(f"/api/sequences/{seq.id}/restore")
    assert e.store.get(origin.id).deleted_at is None
