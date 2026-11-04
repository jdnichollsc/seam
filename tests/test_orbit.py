from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import backend as backends, media, models as model_status, server
from seam.config import Settings
from seam.engine import ORBIT, SHOT, Engine
from comfy_sdk import Progress

from seam.jobs import JobStore
from seam.workflows import load_catalog, load_spec, missing_slots

WF = Path(__file__).resolve().parents[1] / "src" / "seam" / "workflows"


class Output:
    def __init__(self, src: Path | None) -> None:
        self.src = src

    def to_file(self, path: Path) -> None:
        if self.src is None:
            Image.new("RGB", (64, 64)).save(path)
        else:
            path.write_bytes(self.src.read_bytes())


class Handle:
    id = "comfy-1"
    error = None

    def __init__(self, video: Path | None = None) -> None:
        self.status, self.video, self.cancelled = "succeeded", video, False

    def wait(self, timeout: float = 0) -> "Handle":
        return self

    def cancel(self) -> None:
        self.cancelled = True

    def events(self) -> list[object]:
        return [Progress(value=0.5)]

    def get_outputs(self, node: str) -> list[Output]:
        return [Output(self.video if node == "92" else None)]


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
        self.assets = type("A", (), {"from_file": staticmethod(lambda p: Path(p).name)})

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
    monkeypatch.setattr(model_status, "object_info", lambda _s, _b, refresh=False: None)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e, client


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    """A 124-frame stand-in orbit whose frame n is n pixels of red."""
    try:
        media._ensure_ffmpeg()
    except RuntimeError:
        pytest.skip("ffmpeg not installed")
    dst = tmp_path / "orbit.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x96:rate=24", "-frames:v", "124",
                    "-pix_fmt", "yuv420p", str(dst)], check=True)
    return dst


def _shot(client: TestClient, e: Engine) -> tuple[str, str]:
    shot = client.post("/api/shots", json={"prompt": "two cats dancing"}).json()
    e._run_shot(e._queue.get_nowait()[len(SHOT):])
    return shot["id"], e.store.get_shot(shot["id"]).images[0]["name"]


def _run_orbit(e: Engine) -> None:
    e._run_orbit(e._queue.get_nowait()[len(ORBIT):])


def test_the_orbit_graph_is_its_own_kind_and_local_only() -> None:
    spec = load_spec(WF, "orbit")
    assert not missing_slots(spec) and spec.backends == ("local",)
    assert "orbit" not in load_catalog(WF)  # never a transition model
    graph = spec.graph()
    assert graph["150"]["inputs"]["lora_name"] == "minimax_h3_orb360_step1500.safetensors"
    assert graph["136"]["inputs"]["length"] == 124 and graph["124"]["inputs"]["steps"] == 20


def test_an_orbit_renders_and_the_frame_used_is_the_frame_asked(seam, clip: Path) -> None:
    client, e, comfy = seam
    shot_id, name = _shot(client, e)
    comfy.handle = Handle(clip)
    card = client.post(f"/api/shots/{shot_id}/images/{name}/orbit").json()
    assert card["images"][0]["orbit"]["status"] == "queued"
    assert client.post(f"/api/shots/{shot_id}/images/{name}/orbit").status_code == 422  # one at a time per image
    _run_orbit(e)
    orbit = client.get(f"/api/shots/{shot_id}").json()["images"][0]["orbit"]
    assert orbit["status"] == "succeeded" and orbit["url"].endswith(".mp4") and orbit["frames"] == 124
    wf = comfy.submitted[-1]
    assert wf.inputs["137", "image"] == name and wf.inputs["92", "filename_prefix"].startswith("seam/o")
    a = client.post(f"/api/shots/{shot_id}/images/{name}/use?frame=0").json()
    b = client.post(f"/api/shots/{shot_id}/images/{name}/use?frame=62").json()
    assert a["id"] != b["id"]  # different frames, different uploads
    for bad in (-1, 124):
        assert client.post(f"/api/shots/{shot_id}/images/{name}/use?frame={bad}").status_code == 422
    still = client.post(f"/api/shots/{shot_id}/images/{name}/use").json()
    assert still["id"] not in (a["id"], b["id"])


def test_no_finished_orbit_no_frame_and_a_new_orbit_replaces_the_old(seam, clip: Path) -> None:
    client, e, comfy = seam
    shot_id, name = _shot(client, e)
    assert client.post(f"/api/shots/{shot_id}/images/{name}/use?frame=3").status_code == 422
    comfy.handle = Handle(clip)
    for _ in range(2):
        client.post(f"/api/shots/{shot_id}/images/{name}/orbit")
        _run_orbit(e)
    left = e.store.list_orbits(shot_id)
    assert len(left) == 1 and len(list((e.settings.outputs_dir / "orbits").glob("*.mp4"))) == 1


def test_a_failed_orbit_keeps_the_still_and_cancel_reaches_comfyui(seam) -> None:
    client, e, comfy = seam
    shot_id, name = _shot(client, e)
    comfy.handle.status = "failed"
    client.post(f"/api/shots/{shot_id}/images/{name}/orbit")
    _run_orbit(e)
    image = client.get(f"/api/shots/{shot_id}").json()["images"][0]
    assert image["orbit"]["status"] == "failed" and image["url"]
    comfy.handle.status = "succeeded"
    orbit = e.create_orbit(shot_id, name)
    e.store.update_orbit(orbit.id, status="running", comfy_job_id="comfy-1")
    assert client.post(f"/api/orbits/{orbit.id}/cancel").json()["images"][0]["orbit"]["status"] == "cancelled"
    assert comfy.handle.cancelled


def test_restart_reattaches_or_says_the_submit_is_unknown(seam) -> None:
    client, e, _ = seam
    shot_id, name = _shot(client, e)
    a = e.store.create_orbit(shot_id=shot_id, image=name, seed=1, width=800, height=800)
    e.store.update_orbit(a.id, status="running", comfy_job_id="comfy-1")
    b = e.store.create_orbit(shot_id=shot_id, image=name, seed=2, width=800, height=800)
    e.store.update_orbit(b.id, submitting_at=1.0)
    e._worker = type("T", (), {"start": lambda self: None})()
    e.start()
    assert e._queue.get_nowait() == ORBIT + a.id
    assert e.store.get_orbit(b.id).status == "interrupted"


def test_purging_an_image_removes_its_orbit(seam, clip: Path) -> None:
    client, e, comfy = seam
    shot_id, name = _shot(client, e)
    comfy.handle = Handle(clip)
    client.post(f"/api/shots/{shot_id}/images/{name}/orbit")
    _run_orbit(e)
    client.patch(f"/api/shots/{shot_id}/images/{name}", json={"trashed": True})
    assert client.post(f"/api/shots/{shot_id}/images/{name}/use?frame=3").status_code == 404
    e.purge_shots(everything=True)
    assert not e.store.list_orbits(shot_id) and not list((e.settings.outputs_dir / "orbits").glob("*.mp4"))


def test_on_cloud_the_orbit_is_refused_in_words(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    client, e, _ = seam
    shot_id, name = _shot(client, e)
    monkeypatch.setattr(backends, "current", lambda _s: (backends.CLOUD, "test"))
    r = client.post(f"/api/shots/{shot_id}/images/{name}/orbit")
    assert r.status_code == 409 and "not on Comfy Cloud" in r.json()["detail"]
