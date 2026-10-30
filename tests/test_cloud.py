"""Comfy Cloud as a backend: where each take renders, the key's handling, and what can spend credits. No network."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import backend as backends
from seam import server
from seam.config import Settings
from seam.engine import Engine, TransitionRequest
from seam.jobs import JobStore

KEY = "comfyui-test-secret-key"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("SEAM_BACKEND", "COMFY_API_KEY", "COMFY_BASE_URL"):
        monkeypatch.delenv(k, raising=False)


def settings(tmp_path: Path, **kw) -> Settings:
    s = Settings(data_dir=tmp_path, comfy_base_url="http://127.0.0.1:8189", **kw)
    s.ensure_dirs()
    return s


def engine(tmp_path: Path, **kw) -> Engine:
    s = settings(tmp_path, **kw)
    return Engine(s, JobStore(s.db_path), client=object())


def request(tmp_path: Path) -> TransitionRequest:
    img = tmp_path / "a.png"
    Image.new("RGB", (64, 64)).save(img)
    e = Engine(settings(tmp_path), JobStore(tmp_path / "x.db"), client=object())
    preset = next(iter(e.exposed_presets()))
    return TransitionRequest(image_a=str(img), image_b=str(img), preset=preset, a_desc="", b_desc="")


def test_local_is_the_default_and_the_environment_wins(tmp_path, monkeypatch):
    s = settings(tmp_path)
    assert backends.current(s) == ("local", "default")
    backends.save_key(s, KEY)
    backends.choose(s, "cloud")
    assert backends.current(s) == ("cloud", "saved")
    monkeypatch.setenv("SEAM_BACKEND", "local")
    assert backends.current(s) == ("local", "SEAM_BACKEND")
    monkeypatch.setenv("SEAM_BACKEND", "mars")
    with pytest.raises(ValueError):
        backends.current(s)


def test_a_cloud_url_in_the_environment_reads_as_cloud(tmp_path):
    s = Settings(data_dir=tmp_path, comfy_base_url="https://cloud.comfy.org")
    assert backends.current(s) == ("cloud", "COMFY_BASE_URL")


def test_cloud_cannot_be_chosen_without_a_key(tmp_path):
    with pytest.raises(ValueError):
        backends.choose(settings(tmp_path), "cloud")


def test_clients_get_the_right_target_and_restore_the_environment(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setenv("COMFY_BASE_URL", "http://keep.me:1")
    monkeypatch.setenv("COMFY_API_KEY", KEY)
    cloud = backends.make_client(s, "cloud")
    local = backends.make_client(s, "local")
    assert "cloud.comfy.org" in cloud._low.safe_base_url and cloud._low.authenticated
    assert "127.0.0.1:8189" in local._low.safe_base_url
    assert not local._low.authenticated  # a Cloud key is never sent to the local proxy
    assert os.environ["COMFY_BASE_URL"] == "http://keep.me:1" and os.environ["COMFY_API_KEY"] == KEY
    assert KEY not in repr(cloud)


def test_a_cloud_client_without_a_key_is_a_readable_refusal(tmp_path):
    with pytest.raises(ValueError, match="API key"):
        backends.make_client(settings(tmp_path), "cloud")


def test_a_take_stays_where_it_was_asked_to_render(tmp_path):
    e = engine(tmp_path)
    req = request(tmp_path)
    first = e.submit(req, enqueue=False)
    assert first.extra["backend"] == "local"
    backends.save_key(e.settings, KEY)
    backends.choose(e.settings, "cloud")
    second = e.submit(request(tmp_path), enqueue=False)
    assert second.extra["backend"] == "cloud"
    assert e.backend_of(e.store.get(first.id)) == "local"
    backends.choose(e.settings, "local")
    assert e.backend_of(e.store.get(second.id)) == "cloud"
    assert [j.id for j in e.active_cloud_jobs()] == [second.id]


def test_a_take_from_before_cloud_reads_as_local(tmp_path):
    e = engine(tmp_path)
    job = e.store.create(request(tmp_path).to_params())
    assert e.backend_of(job) == "local"


def test_no_key_refuses_before_anything_is_queued(tmp_path, monkeypatch):
    s = settings(tmp_path)
    e = Engine(s, JobStore(s.db_path))
    monkeypatch.setenv("SEAM_BACKEND", "cloud")
    with pytest.raises(ValueError, match="API key"):
        e.submit(request(tmp_path), enqueue=False)
    assert e.store.list() == []


class Handle:
    def __init__(self, cancel_error: Exception | None = None) -> None:
        self.id = "remote"
        self.cancels = 0
        self.cancel_error = cancel_error
        self.release = threading.Event()

    def cancel(self):
        self.cancels += 1
        if self.cancel_error:
            raise self.cancel_error
        self.release.set()
        return self

    def events(self):
        self.release.wait(10)  # like the SDK's stream: yields nothing and never gives up by itself
        return iter(())

    def wait(self, timeout=None):
        return self


def test_cancelling_a_reattached_take_reaches_the_remote_job(tmp_path):
    e = engine(tmp_path)
    handle = Handle()
    seen = []
    e.client = SimpleNamespace(jobs=SimpleNamespace(get=lambda i: seen.append(i) or handle))
    job = e.submit(request(tmp_path), enqueue=False)
    e.store.update(job.id, status="running", comfy_job_id="remote-1")
    assert e.cancel(job.id).status == "cancelled"
    assert seen == ["remote-1"] and handle.cancels == 1


def test_a_failed_cancel_says_it_may_still_be_running(tmp_path):
    e = engine(tmp_path)
    handle = Handle(cancel_error=RuntimeError("down"))
    e.client = SimpleNamespace(jobs=SimpleNamespace(get=lambda i: handle))
    job = e.submit(request(tmp_path), enqueue=False)
    e.store.update(job.id, status="running", comfy_job_id="remote-1")
    done = e.cancel(job.id)
    assert done.status == "cancelled" and "may still be running" in done.error


def test_cloud_deadline_cancels_a_silent_take(tmp_path, monkeypatch):
    monkeypatch.setattr("seam.engine.CLOUD_GRACE_S", 0.05)
    e = engine(tmp_path, job_timeout_s=0.05)
    job = e.submit(request(tmp_path), enqueue=False)
    handle = Handle()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="cancelled on Comfy Cloud"):
        e._follow_cloud(job.id, handle)
    assert handle.cancels == 1 and time.monotonic() - t0 < 5


def test_cloud_deadline_says_so_when_the_cancel_fails(tmp_path, monkeypatch):
    monkeypatch.setattr("seam.engine.CLOUD_GRACE_S", 0.05)
    e = engine(tmp_path, job_timeout_s=0.05)
    job = e.submit(request(tmp_path), enqueue=False)
    handle = Handle(cancel_error=RuntimeError("down"))
    with pytest.raises(TimeoutError, match="may still be running"):
        e._follow_cloud(job.id, handle)
    handle.release.set()


def test_the_receipt_says_where_it_rendered(tmp_path):
    from seam import receipt

    e = engine(tmp_path)
    backends.save_key(e.settings, KEY)
    backends.choose(e.settings, "cloud")
    job = e.submit(request(tmp_path), enqueue=False)
    data = receipt.build_receipt(job, e.spec("l25"), e.settings.outputs_dir)
    assert data["rendered_on"] == "cloud"
    assert KEY not in json.dumps(data, default=str)
    assert KEY not in json.dumps(e.store.get(job.id).extra, default=str)


@pytest.fixture
def api(tmp_path, monkeypatch):
    s = settings(tmp_path)
    monkeypatch.setattr(server, "settings", s)
    monkeypatch.setattr(server, "engine", Engine(s, JobStore(s.db_path), client=object()))
    monkeypatch.setattr(backends, "verify_key", lambda key: None)
    return TestClient(server.app, base_url="http://127.0.0.1")


def test_the_api_never_returns_the_key(api):
    r = api.put("/api/backend/key", json={"api_key": KEY})
    assert r.status_code == 200 and KEY not in r.text
    assert r.json()["cloud"] == {"key_configured": True, "key_source": "saved"}
    assert KEY not in api.get("/api/backend").text and KEY not in api.get("/api/health").text


def test_a_refused_key_writes_nothing(api, monkeypatch):
    def refuse(key):
        raise backends.InvalidKey("Comfy Cloud refused this API key")

    monkeypatch.setattr(backends, "verify_key", refuse)
    assert api.put("/api/backend/key", json={"api_key": KEY}).status_code == 422
    assert api.get("/api/backend").json()["cloud"]["key_configured"] is False
    assert api.put("/api/backend", json={"backend": "cloud"}).status_code == 422


def test_the_environment_locks_the_switch(api, monkeypatch):
    monkeypatch.setenv("SEAM_BACKEND", "local")
    r = api.put("/api/backend", json={"backend": "cloud"})
    assert r.status_code == 409 and "SEAM_BACKEND" in r.text


def test_settings_writes_are_refused_off_localhost(api, monkeypatch):
    monkeypatch.setattr(server, "settings", Settings(data_dir=server.settings.data_dir, host="0.0.0.0"))
    assert api.put("/api/backend", json={"backend": "local"}).status_code == 403
    assert api.put("/api/backend/key", json={"api_key": KEY}).status_code == 403
    assert api.delete("/api/backend/key").status_code == 403


def test_the_server_will_not_start_on_cloud_off_localhost(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, host="0.0.0.0", comfy_base_url="http://127.0.0.1:8189")
    monkeypatch.setattr(server, "settings", s)
    monkeypatch.setenv("SEAM_BACKEND", "cloud")
    with pytest.raises(RuntimeError, match="localhost"):
        with TestClient(server.app):
            pass


def test_the_key_stays_while_cloud_takes_are_active(api, tmp_path):
    api.put("/api/backend/key", json={"api_key": KEY})
    api.put("/api/backend", json={"backend": "cloud"})
    job = server.engine.submit(request(tmp_path), enqueue=False)
    assert api.delete("/api/backend/key").status_code == 409
    server.engine.store.update(job.id, status="cancelled")
    assert api.delete("/api/backend/key").status_code == 200
    assert api.get("/api/backend").json()["backend"] == "local"  # no key left: back to this machine


def test_adding_a_shot_on_cloud_spends_nothing(api, tmp_path, monkeypatch):
    api.put("/api/backend/key", json={"api_key": KEY})
    api.put("/api/backend", json={"backend": "cloud"})
    name = "0123456789abcdef.png"
    Image.new("RGB", (64, 64)).save(server.settings.uploads_dir / name)
    monkeypatch.setattr(Engine, "describe", lambda *a, **k: pytest.fail("submitted a read on upload"))
    r = api.post(f"/api/describe/{name}")
    assert r.status_code == 200 and r.json()["deferred"] is True


def test_estimates_only_count_this_backend(api, tmp_path):
    job = server.engine.submit(request(tmp_path), enqueue=False)
    server.engine.store.update(job.id, status="succeeded", wall_s=40.0)
    assert api.get("/api/estimate?seconds=5").json()["seconds"] == 40
    api.put("/api/backend/key", json={"api_key": KEY})
    api.put("/api/backend", json={"backend": "cloud"})
    assert api.get("/api/estimate?seconds=5").json()["samples"] == 0
