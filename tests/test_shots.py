from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import backend as backends, engine as engine_module, media, models as model_status, server, shotgen
from seam.config import Settings
from seam.engine import SHOT, TRASH_DAYS, Engine
from seam.jobs import JobStore
from seam.workflows import load_catalog, load_image_models, missing_slots, load_spec

QUALITIES = (("draft", 0.6, 4), ("standard", 1.0, 8), ("high", 2.1, 8))


class Output:
    def __init__(self, i: int) -> None:
        self.i = i

    def to_file(self, path: Path) -> None:
        Image.new("RGB", (64, 64), (self.i * 40, 0, 0)).save(path)


class Handle:
    id = "comfy-1"
    error = None

    def __init__(self, status: str = "succeeded", count: int = 2) -> None:
        self.status, self.count, self.cancelled = status, count, False

    def wait(self, timeout: float = 0) -> "Handle":
        return self

    def cancel(self) -> None:
        self.cancelled = True

    def get_outputs(self, node: str) -> list[Output]:
        assert node == "9"
        return [Output(i) for i in range(self.count)]


class Workflow:
    def __init__(self) -> None:
        self.inputs: dict = {}

    def set_input(self, node: str, field: str, value: object) -> None:
        self.inputs[node, field] = value


class Client:
    def __init__(self) -> None:
        self.handle, self.submitted = Handle(), []
        self.workflows = type("W", (), {"from_file": staticmethod(lambda _p: Workflow())})
        self.jobs = type("J", (), {"get": staticmethod(lambda _id: self.handle)})

    def submit(self, wf: Workflow) -> Handle:
        self.submitted.append(wf)
        return self.handle


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = replace(Settings(), data_dir=tmp_path / "data")
    s.ensure_dirs()
    client = Client()
    e = Engine(s, JobStore(s.db_path), client=client)
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    monkeypatch.setattr(backends, "current", lambda _s: (backends.LOCAL, "test"))
    monkeypatch.setattr(model_status, "object_info", lambda _s, _b, refresh=False: None)  # not checked: allowed
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e, client


def _run_next(e: Engine) -> None:
    e._run_shot(e._queue.get_nowait()[len(SHOT):])


@pytest.mark.parametrize("aspect", shotgen.ASPECTS)
@pytest.mark.parametrize("quality", ("draft", "standard", "high"))
def test_every_size_is_on_the_latent_grid_and_keeps_its_shape(aspect: str, quality: str) -> None:
    w, h, steps = shotgen.plan(aspect, quality, 1, QUALITIES)
    assert w % 16 == 0 and h % 16 == 0 and steps in (4, 8)
    assert w / h == pytest.approx(shotgen.ASPECTS[aspect], rel=0.03)
    assert w * h == pytest.approx(next(q[1] for q in QUALITIES if q[0] == quality) * 1e6, rel=0.05)


@pytest.mark.parametrize("args", [("5:4", "standard", 1), ("1:1", "ultra", 1), ("1:1", "high", 0), ("1:1", "high", 5)])
def test_what_the_form_does_not_offer_is_refused(args: tuple) -> None:
    with pytest.raises(ValueError):
        shotgen.plan(*args, QUALITIES)


def test_the_graph_is_not_a_transition_model_and_its_slots_exist(seam) -> None:
    _, e, _ = seam
    assert "shot" not in load_catalog(e.settings.workflows_dir)
    assert missing_slots(load_spec(e.settings.workflows_dir, "shot")) == []


def test_a_batch_lands_as_one_generation_with_a_card_per_image(seam) -> None:
    c, e, client = seam
    made = c.post("/api/shots", json={"prompt": "a lighthouse", "aspect": "21:9", "quality": "draft", "count": 2, "seed": 7}).json()
    assert made["status"] == "queued" and made["seed"] == 7
    _run_next(e)
    wf = client.submitted[0].inputs
    assert wf["57:27", "text"] == "a lighthouse" and wf["57:13", "batch_size"] == 2 and wf["57:3", "seed"] == 7 and wf["57:3", "steps"] == 4
    got = c.get(f"/api/shots/{made['id']}").json()
    assert got["status"] == "succeeded" and [i["name"] for i in got["images"]] == [f"{made['id']}_0.png", f"{made['id']}_1.png"]
    assert got["images"][1]["url"] == f"/outputs/shots/{made['id']}_1.png"
    assert (e.settings.outputs_dir / "shots" / f"{made['id']}_1.png").is_file()
    assert e.store.list() == []  # not a take


def test_no_seed_picks_one_and_stores_it_and_a_variation_keeps_the_rest(seam) -> None:
    c, e, _ = seam
    a = c.post("/api/shots", json={"prompt": "x", "aspect": "1:1", "quality": "high", "count": 3}).json()
    b = c.post(f"/api/shots/{a['id']}/variations").json()
    assert a["seed"] > 0 and b["parent_id"] == a["id"]
    assert (b["prompt"], b["aspect"], b["quality"], b["count"]) == ("x", "1:1", "high", 3)
    assert b["seed"] != a["seed"]


def test_star_and_trash_touch_one_image_not_its_siblings(seam) -> None:
    c, e, _ = seam
    s = c.post("/api/shots", json={"prompt": "x", "count": 2}).json()
    _run_next(e)
    first, second = f"{s['id']}_0.png", f"{s['id']}_1.png"
    c.patch(f"/api/shots/{s['id']}/images/{first}", json={"starred": True})
    got = c.patch(f"/api/shots/{s['id']}/images/{second}", json={"trashed": True}).json()
    assert got["images"][0]["starred"] and not got["images"][0]["deleted_at"]
    assert got["images"][1]["deleted_at"] and not got["images"][1]["starred"]
    got = c.patch(f"/api/shots/{s['id']}/images/{second}", json={"trashed": False}).json()
    assert not got["images"][1]["deleted_at"]


def test_a_used_image_is_a_normal_upload_and_survives_the_trash(seam) -> None:
    c, e, client = seam
    client.handle = Handle(count=1)
    s = c.post("/api/shots", json={"prompt": "x", "count": 1}).json()
    _run_next(e)
    name = f"{s['id']}_0.png"
    up = c.post(f"/api/shots/{s['id']}/images/{name}/use").json()
    assert set(up) == {"id", "url", "width", "height"} and up["width"] == 64
    c.patch(f"/api/shots/{s['id']}/images/{name}", json={"trashed": True})
    assert c.post(f"/api/shots/{s['id']}/images/{name}/use").status_code in (404, 405)  # 405: an encoded slash matches no route
    assert e.purge_shots(everything=True) == 1
    assert (e.settings.uploads_dir / up["id"]).is_file()
    assert c.get(f"/api/shots/{s['id']}").status_code == 404  # its last image left


@pytest.mark.parametrize("name", ["..%2F..%2Fseam.db", "x.png", "..%5Cseam.db"])
def test_only_a_name_the_record_holds_reaches_the_disk(seam, name: str) -> None:
    c, e, _ = seam
    s = c.post("/api/shots", json={"prompt": "x", "count": 1}).json()
    _run_next(e)
    assert c.post(f"/api/shots/{s['id']}/images/{name}/use").status_code in (404, 405)  # 405: an encoded slash matches no route


def test_the_trash_waits_its_days_and_skips_what_is_being_written(seam) -> None:
    c, e, _ = seam
    s = c.post("/api/shots", json={"prompt": "x", "count": 2}).json()
    _run_next(e)
    first = f"{s['id']}_0.png"
    e.edit_image(s["id"], first, deleted_at=time.time())
    assert e.purge_shots() == 0
    e.edit_image(s["id"], first, deleted_at=time.time() - TRASH_DAYS * 86400 - 1)
    e._writing.add(s["id"])
    assert e.purge_shots() == 0
    e._writing.discard(s["id"])
    assert e.purge_shots() == 1
    assert not (e.settings.outputs_dir / "shots" / first).exists()
    assert (e.settings.outputs_dir / "shots" / f"{s['id']}_1.png").exists()


def test_cancel_before_the_worker_never_submits(seam) -> None:
    c, e, client = seam
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    assert c.post(f"/api/shots/{s['id']}/cancel").json()["status"] == "cancelled"
    _run_next(e)
    assert client.submitted == []
    assert c.delete(f"/api/shots/{s['id']}").status_code == 204


def test_cancel_after_a_restart_stops_it_on_comfyui(seam) -> None:
    c, e, client = seam
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    e.store.update_shot(s["id"], status="running", comfy_job_id="comfy-1")
    c.post(f"/api/shots/{s['id']}/cancel")
    assert client.handle.cancelled


def test_a_failed_render_says_why_and_the_worker_lives(seam) -> None:
    c, e, client = seam
    client.handle = Handle("failed")
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    _run_next(e)
    got = c.get(f"/api/shots/{s['id']}").json()
    assert got["status"] == "failed" and "failed" in got["error"]
    assert s["id"] not in e._writing and s["id"] not in e._handles


def test_recovery_reattaches_or_says_it_was_interrupted(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, client = seam
    attached = e.store.create_shot(prompt="a", aspect="1:1", quality="draft", width=1, height=1, steps=4, model="Z-Image-Turbo", seed=1, count=2, backend="local")
    e.store.update_shot(attached.id, status="running", comfy_job_id="comfy-1")
    unknown = e.store.create_shot(prompt="b", aspect="1:1", quality="draft", width=1, height=1, steps=4, model="Z-Image-Turbo", seed=1, count=1, backend="local")
    e.store.update_shot(unknown.id, submitting_at=1.0)
    before = e.store.create_shot(prompt="c", aspect="1:1", quality="draft", width=1, height=1, steps=4, model="Z-Image-Turbo", seed=1, count=1, backend="local")
    monkeypatch.setattr(e._worker, "start", lambda: None)
    e.start()
    assert e.store.get_shot(unknown.id).status == "interrupted" and "may still" in e.store.get_shot(unknown.id).error
    assert e.store.get_shot(before.id).status == "interrupted"
    assert e._queue.get_nowait() == SHOT + attached.id
    e._run_shot(attached.id)
    assert client.submitted == [] and e.store.get_shot(attached.id).status == "succeeded"


def test_shots_and_takes_share_one_queue(seam) -> None:
    c, e, _ = seam
    c.post("/api/shots", json={"prompt": "x"})
    assert e._queue.get_nowait().startswith(SHOT)


def test_cloud_without_the_model_is_refused_in_words_before_anything_queues(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, _ = seam
    monkeypatch.setattr(backends, "current", lambda _s: (backends.CLOUD, "test"))
    monkeypatch.setattr(model_status, "object_info", lambda *_a, **_k: {})
    r = c.post("/api/shots", json={"prompt": "x"})
    assert r.status_code == 409 and "not offered by Comfy Cloud" in r.json()["detail"]
    assert e.store.list_shots() == [] and e._queue.empty()
    assert "not offered by Comfy Cloud" in c.get("/api/shots").json()["unavailable"]


def test_missing_models_are_refused_with_the_download_line(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, _ = seam
    monkeypatch.setattr(model_status, "why_not", lambda *_: (False, "model files not installed"))
    r = c.post("/api/shots", json={"prompt": "x"})
    assert r.status_code == 409 and "--only shot" in r.json()["detail"]


def test_a_live_event_has_the_same_shape_as_rest(seam) -> None:
    c, e, _ = seam
    q = e.store.subscribe()
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    kind, item = q.get_nowait()
    assert kind == "shot" and server._shot_out(item) == s
    json.dumps(s)


def test_dismiss_waits_for_the_worker_and_the_worker_survives_a_vanished_record(seam) -> None:
    c, e, _ = seam
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    c.post(f"/api/shots/{s['id']}/cancel")
    e._writing.add(s["id"])
    assert c.delete(f"/api/shots/{s['id']}").status_code == 422
    e._writing.discard(s["id"])
    e.store.remove_shot(s["id"])
    e._run_shot(s["id"])  # gone before it ran
    other = e.store.create_shot(prompt="y", aspect="1:1", quality="draft", width=1, height=1, steps=4, model="Z-Image-Turbo", seed=1, count=1, backend="local")
    e._execute_shot = lambda _s: e.store.remove_shot(other.id) or 1 / 0  # gone while it ran
    e._run_shot(other.id)


def test_the_backend_checked_is_the_backend_kept(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    _, e, _ = seam
    answers = iter([backends.LOCAL, backends.CLOUD, backends.CLOUD])
    monkeypatch.setattr(backends, "current", lambda _s: (next(answers), "test"))
    assert e.create_shot("x").backend == backends.LOCAL


def test_a_batch_failing_halfway_leaves_no_files(seam) -> None:
    c, e, client = seam
    class Broken(Output):
        def to_file(self, path: Path) -> None:
            raise OSError("disk full")
    client.handle.get_outputs = lambda _node: [Output(0), Broken(1)]
    s = c.post("/api/shots", json={"prompt": "x", "count": 2}).json()
    _run_next(e)
    assert c.get(f"/api/shots/{s['id']}").json()["status"] == "failed"
    assert list((e.settings.outputs_dir / "shots").glob(f"{s['id']}_*")) == []


def test_a_locked_file_waits_for_the_next_purge(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, client = seam
    client.handle = Handle(count=1)
    s = c.post("/api/shots", json={"prompt": "x"}).json()
    _run_next(e)
    name = f"{s['id']}_0.png"
    e.edit_image(s["id"], name, deleted_at=1.0)
    real = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("locked")))
    assert e.purge_shots() == 0 and e.store.get_shot(s["id"]) is not None
    monkeypatch.setattr(Path, "unlink", real)
    assert e.purge_shots() == 1


def test_each_model_has_its_own_limit_steps_and_graph(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, client = seam
    monkeypatch.setattr(e, "shots_unavailable", lambda *_a, **_k: None)
    too_long = c.post("/api/shots", json={"prompt": "x" * 601, "model": "shot_juggernaut"})
    assert too_long.status_code == 422 and "600" in too_long.text
    made = c.post("/api/shots", json={"prompt": "x" * 600, "model": "shot_juggernaut"}).json()
    assert made["model"] == "Juggernaut XL" and made["steps"] == 30
    assert c.post("/api/shots", json={"prompt": "x" * 601}).status_code == 201
    assert c.post("/api/shots", json={"prompt": "x", "model": "nope"}).status_code == 422
    _run_next(e)
    assert client.submitted[0].inputs["6", "text"] == "x" * 600
    assert e.variation(made["id"]).model == "Juggernaut XL"
    for spec in load_image_models(e.settings.workflows_dir).values():
        assert missing_slots(spec) == [] and spec.model not in load_catalog(e.settings.workflows_dir)


def test_a_model_that_makes_one_image_refuses_more(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    c, e, client = seam
    monkeypatch.setattr(e, "shots_unavailable", lambda *_a, **_k: None)
    assert next(m for m in c.get("/api/shots").json()["models"] if m["id"] == "shot_h3")["max_count"] == 1
    two = c.post("/api/shots", json={"prompt": "x", "model": "shot_h3", "count": 2})
    assert two.status_code == 422 and "1 image at a time" in two.text
    made = c.post("/api/shots", json={"prompt": "a pier", "model": "shot_h3", "aspect": "16:9"}).json()
    assert (made["model"], made["steps"], made["width"], made["height"]) == ("MiniMax H3 Image", 6, 1936, 1088)
    client.handle = Handle(count=1)
    _run_next(e)
    assert client.submitted[0].inputs["23:24", "value"] == "a pier" and client.submitted[0].inputs["23:5", "width"] == 1936
