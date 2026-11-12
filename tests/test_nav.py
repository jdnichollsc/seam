"""Pages that survive a reload, and a finished take reused as a clip of a sequence."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam.server import mount_web

from test_sequence import _drain, _image, _upload, _video, seam  # noqa: F401 — the fixture


def test_the_app_is_served_at_its_own_paths_only(tmp_path) -> None:
    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<title>Seam</title>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("1", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    mount_web(app, web)
    c = TestClient(app)
    for path in ("/", "/sequence", "/generate", "/sequences/abc", "/takes/abc"):
        r = c.get(path)
        assert r.status_code == 200 and "<title>Seam</title>" in r.text, path
    assert c.get("/sequences/abc").headers["cache-control"] == "no-cache"
    assert c.get("/assets/app.js").text == "1"
    assert c.get("/api/ping").json() == {"ok": True}
    for path in ("/api/nope", "/assets/missing.js", "/nope", "/sequences/a/b", "/takes/..%2f..%2fsecret.txt",
                 "/%2e%2e/secret.txt"):
        assert c.get(path).status_code == 404, path


def _take(c, e, tmp, seconds: float, merged: float | None = None) -> dict:
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    job = c.post("/api/transitions", json={"image_a": a["id"], "image_b": b["id"], "preset": "smoke"}).json()
    _drain(e)
    out = _video(e.settings.outputs_dir / f"{job['id']}.mp4", seconds, "640x352", audio=True)
    extra = {}
    if merged:
        extra["merged"] = _video(e.settings.outputs_dir / f"{job['id']}_full.mp4", merged, "640x352", audio=True).name
    e.store.update(job["id"], status="succeeded", output=out.name, extra=extra)
    return job


def _cuts(c, tmp) -> dict:
    ids = [_upload(c, _image(tmp / f"s{i}.png", (640, 360), col))["id"] for i, col in enumerate(("red", "green"))]
    return c.post("/api/sequences", json={"shots": [{"image": i} for i in ids], "segments": [{"preset": "cut"}]}).json()


def test_a_finished_take_becomes_a_clip_of_a_sequence(seam) -> None:
    c, e, tmp = seam
    unfinished = c.post("/api/transitions", json={
        "image_a": _upload(c, _image(tmp / "u1.png", (640, 360), "red"))["id"],
        "image_b": _upload(c, _image(tmp / "u2.png", (640, 360), "blue"))["id"], "preset": "smoke"}).json()
    assert c.post(f"/api/jobs/{unfinished['id']}/use").status_code == 409
    assert c.post("/api/jobs/nope/use").status_code == 404
    _drain(e)
    gone = _take(c, e, tmp, 5)
    assert c.post(f"/api/jobs/{gone['id']}/trash").status_code == 200
    assert c.post(f"/api/jobs/{gone['id']}/use").status_code == 404  # a trashed take is not offered

    long_take = _take(c, e, tmp, 10)
    up = c.post(f"/api/jobs/{long_take['id']}/use")
    assert up.status_code == 200, up.text
    up = up.json()
    assert up["clip"] and up["first"] and up["last"] and up["clip_seconds"] == pytest.approx(10, abs=0.2)
    shot = {"clip": up["clip"], "first": up["first"], "last": up["last"]}
    seq = _cuts(c, tmp)
    _drain(e)
    e._reconcile(seq["id"])
    url = f"/api/sequences/{seq['id']}/shots"
    r = c.post(url, json={"shot": shot, "segment": {"preset": "smoke"}})
    assert r.status_code == 422, r.text  # 10 s next to a generated transition
    r = c.post(url, json={"shot": shot, "segment": {"preset": "cut"}})
    assert r.status_code == 201, r.text
    assert len(r.json()["shots"]) == 3 and r.json()["segments"][-1] is None


def test_the_full_version_or_the_transition_alone(seam) -> None:
    c, e, tmp = seam
    job = _take(c, e, tmp, 5, merged=9)
    assert c.post(f"/api/jobs/{job['id']}/use").json()["clip_seconds"] == pytest.approx(9, abs=0.2)
    alone = c.post(f"/api/jobs/{job['id']}/use?full=false").json()
    assert alone["clip_seconds"] == pytest.approx(5, abs=0.2)
    seq = _cuts(c, tmp)
    _drain(e)
    e._reconcile(seq["id"])
    r = c.post(f"/api/sequences/{seq['id']}/shots", json={
        "shot": {"clip": alone["clip"], "first": alone["first"], "last": alone["last"]}, "segment": {"preset": "smoke"}})
    assert r.status_code == 201, r.text  # a 5 s take fits next to a generated transition


def test_a_take_longer_than_a_minute_is_refused(seam) -> None:
    c, e, tmp = seam
    job = _take(c, e, tmp, 5, merged=62)
    r = c.post(f"/api/jobs/{job['id']}/use")
    assert r.status_code == 400 and "1-60 s" in r.json()["detail"]
