from __future__ import annotations

import io
import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import server
from seam.config import Settings
from seam.engine import SUBMIT_UNKNOWN, Engine
from seam.jobs import JobStore


def _engine(tmp_path: Path) -> Engine:
    s = replace(Settings(), data_dir=tmp_path)
    s.ensure_dirs()
    return Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work


def _done_job(e: Engine, tmp_path: Path):
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (64, 48), "red").save(a)
    Image.new("RGB", (64, 48), "blue").save(b)
    job = e.store.create({"image_a": str(a), "image_b": str(b), "preset": "smoke", "seed": 7, "seconds": 5,
                          "model": "l25", "enhance": True, "prompt_extra": ""})
    (e.settings.outputs_dir / f"{job.id}.mp4").write_bytes(b"mp4")
    return e.store.update(job.id, status="succeeded", output=f"{job.id}.mp4", wall_s=41.0,
                          extra={"prompt": "smoke rolls over", "width": 1248, "height": 704})


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    e = _engine(tmp_path)
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", e.settings)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e


def test_receipt_and_comfy_bundle_rebuild_the_take(client, tmp_path: Path) -> None:
    c, e = client
    job = _done_job(e, tmp_path)
    r = c.get(f"/api/jobs/{job.id}/receipt").json()
    assert (r["effect"], r["model"], r["seed"], r["prompt_sent"]) == ("smoke", "l25", 7, "smoke rolls over")
    assert len(r["inputs"]["image_a"]["sha256"]) == 64 and r["workflow"]["file"] == "l25_flf2v.api.json"

    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/jobs/{job.id}/comfyui.zip").content))
    assert {"workflow_api.json", "receipt.json", "README.txt", f"input/seam_{job.id}_a.png"} <= set(z.namelist())
    graph = json.loads(z.read("workflow_api.json"))
    spec = e.spec("l25")
    for slot, value in {"image_a": f"seam_{job.id}_a.png", "seed": 7, "prompt": "smoke rolls over",
                        "width": 1248, "enhance_prompt": True}.items():
        node, field = spec.slots[slot]
        assert graph[node]["inputs"][field] == value


def test_archived_graph_wins_over_later_template_edits(client, tmp_path: Path) -> None:
    from seam import receipt

    c, e = client
    job = _done_job(e, tmp_path)
    spec = e.spec("l25")
    receipt.archive_graph(job.id, spec, receipt.render_params(job, spec), e.settings.outputs_dir)
    saved = json.loads(receipt.graph_file(job.id, e.settings.outputs_dir).read_text(encoding="utf-8"))
    saved["graph"]["marker"] = {"inputs": {}, "class_type": "Note"}  # stands in for "the template changed since"
    saved["workflow_sha256"] = "0" * 64
    receipt.graph_file(job.id, e.settings.outputs_dir).write_text(json.dumps(saved), encoding="utf-8")
    r = c.get(f"/api/jobs/{job.id}/receipt").json()
    assert r["workflow"] == {"file": "l25_flf2v.api.json", "sha256": "0" * 64, "archived_at_submit": True}
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/jobs/{job.id}/comfyui.zip").content))
    assert "marker" in json.loads(z.read("workflow_api.json"))


def test_receipt_needs_a_finished_take(client) -> None:
    c, e = client
    job = e.store.create({"image_a": "a", "image_b": "b", "preset": "smoke"})
    assert c.get(f"/api/jobs/{job.id}/receipt").status_code == 409
    assert c.get("/api/jobs/nope/comfyui.zip").status_code == 404


def test_estimate_is_median_of_recent_takes(client) -> None:
    c, e = client
    assert c.get("/api/estimate").json() == {"seconds": None, "samples": 0}
    for wall in (40.0, 44.0, 90.0):
        j = e.store.create({"preset": "smoke"})
        e.store.update(j.id, status="succeeded", wall_s=wall)
    assert c.get("/api/estimate").json() == {"seconds": 44, "samples": 3}


def test_only_localhost_and_same_site_writes(client) -> None:
    c, _ = client
    assert c.get("/api/estimate").status_code == 200
    assert c.get("/api/estimate", headers={"host": "evil.example:7860"}).status_code == 403  # DNS rebinding
    r = c.post("/api/jobs/x/cancel", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.post("/api/jobs/x/cancel", headers={"origin": "http://localhost:5173"}).status_code == 404  # dev UI ok
    assert c.post("/api/jobs/x/cancel").status_code == 404  # MCP/CLI send no Origin
    assert server._is_loopback("127.0.0.2") and server._is_loopback("[::1]") and server._is_loopback("localhost")
    assert not server._is_loopback("0.0.0.0") and not server._is_loopback("evil.example")


def test_restart_mid_submit_is_reported_as_unknown_not_resubmitted(tmp_path: Path) -> None:
    e = _engine(tmp_path)
    sending = e.store.create({"preset": "smoke"})
    e.store.update(sending.id, status="running", extra={"submitting_at": 1.0})
    never = e.store.create({"preset": "smoke"})
    e._worker.start = lambda: None  # no GPU work
    e.start()
    assert e.store.get(sending.id).error == SUBMIT_UNKNOWN
    assert e.store.get(never.id).error == "server restarted before submission"
    assert e._queue.empty()


def test_examples_load_as_ordinary_uploads(client) -> None:
    c, e = client
    listed = c.get("/api/examples").json()
    assert {x["id"] for x in listed} == {"p1", "p2", "p6"}
    assert c.get(listed[0]["a_url"]).status_code == 200
    got = c.post("/api/examples/p2").json()
    assert got["preset"] == "whip_pan" and got["a"]["id"] != got["b"]["id"]
    assert (e.settings.uploads_dir / got["a"]["id"]).is_file()
    assert c.post("/api/examples/nope").status_code == 404
