from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import media, server
from seam.config import Settings
from seam.engine import SEQ, Engine
from seam.jobs import JobStore

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _video(path: Path, seconds: float, size: str, rate: int = 24, audio: bool = False) -> Path:
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=d={seconds}", "-shortest"]
    subprocess.run([*args, str(path)], check=True)
    return path


def _image(path: Path, size: tuple[int, int], color: str) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = replace(Settings(), data_dir=tmp_path / "data")
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e, tmp_path


def _upload(c: TestClient, path: Path, role: str = "a") -> dict[str, Any]:
    kind = "video/mp4" if path.suffix == ".mp4" else "image/png"
    with path.open("rb") as f:
        r = c.post(f"/api/uploads?role={role}", files={"file": (path.name, f, kind)})
    assert r.status_code == 200, r.text
    return r.json()


def _render(e: Engine, job_id: str, seconds: float = 1.0) -> None:
    """Stand-in for the GPU: give a queued take an output at the canvas size, as the graph would."""
    job = e.store.get(job_id)
    out = _video(e.settings.outputs_dir / f"{job.id}.mp4", seconds, f"{job.params['width']}x{job.params['height']}",
                 audio=True)
    e.store.update(job.id, status="succeeded", output=out.name)


def _drain(e: Engine) -> list[str]:
    items = []
    while not e._queue.empty():
        items.append(e._queue.get())
    return items


def test_sequence_joins_each_clip_once_at_one_canvas(seam) -> None:
    c, e, tmp = seam
    s0 = _upload(c, _image(tmp / "s0.png", (1280, 720), "red"))
    mid = _upload(c, _video(tmp / "mid.mp4", 2, "640x480", rate=30), role="mid")  # other aspect, silent
    s2 = _upload(c, _image(tmp / "s2.png", (720, 1280), "blue"))
    assert mid["first"] != mid["last"] and mid["clip_seconds"] == pytest.approx(2, abs=0.1)
    r = c.post("/api/sequences", json={
        "shots": [{"image": s0["id"]}, {"clip": mid["clip"], "first": mid["first"], "last": mid["last"]},
                  {"image": s2["id"]}],
        "segments": [{"preset": "smoke"}, {"preset": "whip_pan", "seconds": 10}]})
    assert r.status_code == 201, r.text
    seq = r.json()
    assert seq["status"] == "running" and len(seq["takes"]) == 2
    w, h = media.output_size(1280, 720)
    t0, t1 = (e.store.get(i) for i in seq["segments"])
    # every segment renders at S0's canvas, leaves the middle clip from its last frame, lands on its first
    assert (t0.params["width"], t0.params["height"]) == (w, h) == (t1.params["width"], t1.params["height"])
    assert Path(t0.params["image_b"]).name == mid["first"] and Path(t1.params["image_a"]).name == mid["last"]
    assert not t0.params["clip_a"] and not t0.params["clip_b"] and not t1.params["clip_b"]  # the join adds clips
    assert t0.extra == {"sequence_id": seq["id"], "segment": 0, "backend": "local"} and t1.params["seconds"] == 10
    assert _drain(e) == seq["segments"]

    _render(e, t0.id)
    e._reconcile(seq["id"])
    assert e.store.get_sequence(seq["id"]).status == "running"  # one take still to come
    _render(e, t1.id, seconds=1.5)
    e._reconcile(seq["id"])
    done = c.get(f"/api/sequences/{seq['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    out = media.probe(e.settings.outputs_dir / done["output"])
    assert (out.width, out.height, out.fps) == (w, h, 24)
    assert out.duration == pytest.approx(1 + 2 + 1.5, abs=0.15) and out.has_audio  # take, clip once, take
    assert done["output_url"].startswith(f"/outputs/{seq['id']}_") and done["poster_url"] and done["sheet_url"]
    rec = c.get(f"/api/sequences/{seq['id']}/receipt").json()
    assert [p.get("segment") or p.get("clip") for p in rec["parts"]] == [1, mid["clip"], 2]
    assert rec["parts"][0]["receipt"]["effect"] == "smoke"


def test_one_bad_segment_queues_nothing(seam) -> None:
    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    shots = [{"image": a["id"]}, {"image": b["id"]}, {"image": a["id"]}]
    r = c.post("/api/sequences", json={"shots": shots, "segments": [{"preset": "smoke"}, {"preset": "ink",
                                                                                         "seconds": 10}]})
    assert r.status_code == 422 and "segment 2" in r.json()["detail"]  # ink is qualified at 5 s only
    assert c.post("/api/sequences", json={"shots": shots, "segments": [{"preset": "smoke"}]}).status_code == 422
    assert e.store.list() == [] and e.store.list_sequences() == [] and _drain(e) == []


def test_reroll_one_segment_rejoins_with_a_new_file(seam) -> None:
    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    seq = c.post("/api/sequences", json={"shots": [{"image": a["id"]}, {"image": b["id"]}, {"image": a["id"]}],
                                         "segments": [{"preset": "smoke", "seed": 1}, {"preset": "ink", "seed": 2}]}).json()
    _drain(e)
    assert c.post(f"/api/sequences/{seq['id']}/segments/1/refine", json={}).status_code == 409  # still rendering
    for i in seq["segments"]:
        _render(e, i)
    e._reconcile(seq["id"])
    first = e.store.get_sequence(seq["id"])
    assert first.status == "succeeded"

    r = c.post(f"/api/sequences/{seq['id']}/segments/1/refine", json={"preset": "whip_pan"})
    assert r.status_code == 201, r.text
    again = r.json()
    new = e.store.get(again["segments"][0])
    assert again["status"] == "running" and again["segments"][1] == seq["segments"][1]  # segment 2 is reused
    assert new.parent_id == seq["segments"][0] and new.params["preset"] == "whip_pan" and new.params["seed"] != 1
    assert new.params["width"] == first.width and new.extra == {"sequence_id": seq["id"], "segment": 0, "backend": "local"}
    assert _drain(e) == [new.id]
    _render(e, new.id)
    e._reconcile(seq["id"])
    second = e.store.get_sequence(seq["id"])
    assert second.status == "succeeded" and second.output != first.output  # a new URL, no stale browser cache
    assert c.post(f"/api/sequences/{seq['id']}/segments/3/refine", json={}).status_code == 422


def test_failed_cancelled_and_restarted_sequences(seam) -> None:
    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    body = {"shots": [{"image": a["id"]}, {"image": b["id"]}, {"image": a["id"]}],
            "segments": [{"preset": "smoke"}, {"preset": "smoke"}]}
    seq = c.post("/api/sequences", json=body).json()
    _drain(e)
    _render(e, seq["segments"][0])
    e.store.update(seq["segments"][1], status="failed", error="comfy job x failed: OOM")
    e._reconcile(seq["id"])
    failed = e.store.get_sequence(seq["id"])
    assert failed.status == "failed" and failed.error == "segment 2: comfy job x failed: OOM"
    assert c.post(f"/api/sequences/{seq['id']}/segments/2/refine", json={}).status_code == 201  # re-roll the bad one

    other = c.post("/api/sequences", json=body).json()
    _drain(e)
    cancelled = c.post(f"/api/sequences/{other['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and {t["status"] for t in cancelled["takes"]} == {"cancelled"}
    e._reconcile(other["id"])
    assert e.store.get_sequence(other["id"]).status == "cancelled"  # a late segment never revives it

    # restart: an unfinished sequence is reconciled again by the new worker
    e2 = Engine(e.settings, e.store, client=object())
    e2._worker.start = lambda: None  # type: ignore[method-assign]
    e2.start()
    assert SEQ + seq["id"] in _drain(e2) and SEQ + other["id"] not in _drain(e2)


def test_live_events_carry_sequences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(server, "engine", SimpleNamespace(store=store))

    async def run() -> str:
        body: Any = (await server.events()).body_iterator
        assert (await anext(body)).startswith("retry:")
        seq = store.create_sequence([{"first": str(tmp_path / "a.png"), "last": str(tmp_path / "a.png")}] * 2,
                                    ["j1"], 640, 352)
        store.update_sequence(seq.id, status="succeeded", output=f"{seq.id}_k.mp4")
        chunks = [await anext(body), await anext(body)]
        await body.aclose()
        return chunks[1]

    chunk = asyncio.run(run())
    assert chunk.startswith("event: sequence\n")
    data = json.loads(chunk.split("data: ", 1)[1])
    assert data["output_url"].endswith("_k.mp4") and data["shots"][0]["first"] == "a.png"  # no server paths


def test_a_take_cancelled_while_its_shots_are_read_never_reaches_the_gpu(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    from seam import engine as engine_mod

    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    seq = c.post("/api/sequences", json={"shots": [{"image": a["id"]}, {"image": b["id"]}],
                                         "segments": [{"preset": "smoke"}]}).json()
    _drain(e)
    submitted = []
    e.client = type("C", (), {"submit": lambda self, wf: submitted.append(wf)})()
    monkeypatch.setattr(engine_mod, "build", lambda *a, **k: object())
    monkeypatch.setattr(engine_mod.receipt, "archive_graph", lambda *a, **k: None)

    def reading(job, req):  # the user cancels while Seam reads the shots
        c.post(f"/api/sequences/{seq['id']}/cancel")
        return req

    monkeypatch.setattr(e, "_fill_descriptions", reading)
    e._execute(e.store.get(seq["segments"][0]))
    assert submitted == [] and e.store.get(seq["segments"][0]).status == "cancelled"


def test_grow_a_finished_sequence_one_shot_at_a_time(seam) -> None:
    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    d = _upload(c, _image(tmp / "d.png", (640, 360), "green"))
    seq = c.post("/api/sequences", json={"shots": [{"image": a["id"]}, {"image": b["id"]}],
                                         "segments": [{"preset": "smoke", "b_desc": "a blue door", "b_ref": "the door"}]}).json()
    _drain(e)
    grow = {"shot": {"image": d["id"]}, "segment": {"preset": "ink"}}
    assert c.post(f"/api/sequences/{seq['id']}/shots", json=grow).status_code == 409  # still rendering
    _render(e, seq["segments"][0])
    e._reconcile(seq["id"])
    first = e.store.get_sequence(seq["id"])

    bad = c.post(f"/api/sequences/{seq['id']}/shots", json={**grow, "segment": {"preset": "ink", "seconds": 10}})
    assert bad.status_code == 422 and e.store.get_sequence(seq["id"]).segments == first.segments  # nothing written
    r = c.post(f"/api/sequences/{seq['id']}/shots", json=grow)
    assert r.status_code == 201, r.text
    grown = r.json()
    new = e.store.get(grown["segments"][1])
    assert grown["status"] == "running" and grown["segments"][0] == seq["segments"][0] and len(grown["shots"]) == 3
    assert Path(new.params["image_a"]).name == b["id"] and Path(new.params["image_b"]).name == d["id"]
    assert new.params["a_desc"] == "a blue door" and new.params["a_ref"] == "the door"  # the user's reading carries on
    assert (new.params["width"], new.params["height"]) == (first.width, first.height)
    assert _drain(e) == [new.id]  # only the new transition renders
    _render(e, new.id)
    e._reconcile(seq["id"])
    done = e.store.get_sequence(seq["id"])
    assert done.status == "succeeded" and done.output != first.output
    assert media.probe(e.settings.outputs_dir / done.output).duration == pytest.approx(2, abs=0.15)

    # a reused take that vanished from disk is caught before anything renders
    (e.settings.outputs_dir / e.store.get(done.segments[0]).output).unlink()
    r = c.post(f"/api/sequences/{seq['id']}/shots", json={"shot": {"image": a["id"]}, "segment": {"preset": "smoke"}})
    assert r.status_code == 422 and "missing" in r.json()["detail"] and _drain(e) == []
    assert e.store.get_sequence(seq["id"]).segments == done.segments


def test_continue_a_two_shot_take_with_clips(seam) -> None:
    c, e, tmp = seam
    clip_a = _upload(c, _video(tmp / "ca.mp4", 1.5, "640x360", rate=30), role="a")
    clip_b = _upload(c, _video(tmp / "cb.mp4", 2, "640x360", rate=30), role="b")
    d = _upload(c, _image(tmp / "d.png", (640, 360), "green"))
    job = c.post("/api/transitions", json={"image_a": clip_a["id"], "image_b": clip_b["id"], "preset": "smoke",
                                           "clip_a": clip_a["clip"], "clip_b": clip_b["clip"]}).json()
    _drain(e)
    grow = {"shot": {"image": d["id"]}, "segment": {"preset": "whip_pan"}}
    assert c.post(f"/api/jobs/{job['id']}/continue", json=grow).status_code == 422  # not finished yet
    out = _video(e.settings.outputs_dir / f"{job['id']}.mp4", 1, "640x352", audio=True)  # the take's real canvas
    e.store.update(job["id"], status="succeeded", output=out.name)
    bad = {**grow, "segment": {"preset": "ink", "seconds": 10}}  # ink is qualified at 5 s only
    assert c.post(f"/api/jobs/{job['id']}/continue", json=bad).status_code == 422
    assert e.store.list_sequences() == []  # a rejected continuation leaves nothing behind
    r = c.post(f"/api/jobs/{job['id']}/continue", json=grow)
    assert r.status_code == 201, r.text
    seq = r.json()
    assert seq["segments"][0] == job["id"] and (seq["width"], seq["height"]) == (640, 352)
    assert [s.get("clip") for s in seq["shots"]] == [clip_a["clip"], clip_b["clip"], None]
    assert seq["shots"][1]["first"] == clip_b["id"] and seq["shots"][1]["last"] != clip_b["id"]  # B's last frame
    new = e.store.get(seq["segments"][1])
    assert Path(new.params["image_a"]).name == seq["shots"][1]["last"] and not new.params["clip_a"]
    assert e.store.get(job["id"]).extra == {"backend": "local"}  # the original take is left as it was
    assert _drain(e) == [new.id]
    _render(e, new.id)
    e._reconcile(seq["id"])
    done = e.store.get_sequence(seq["id"])
    assert done.status == "succeeded"
    # clip A, take 1, clip B (once), take 2
    assert media.probe(e.settings.outputs_dir / done.output).duration == pytest.approx(1.5 + 1 + 2 + 1, abs=0.2)

    # a new take of the wrapped transition renders at the sequence canvas, without wrapping the clips again
    r = c.post(f"/api/sequences/{seq['id']}/segments/1/refine", json={})
    assert r.status_code == 201, r.text
    redo = e.store.get(r.json()["segments"][0])
    assert (redo.params["width"], redo.params["height"]) == (640, 352)
    assert not redo.params["clip_a"] and not redo.params["clip_b"]
