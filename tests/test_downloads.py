from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seam import backend as backends, downloads, server
from seam.config import Settings
from seam.doctor import model_files
from seam.downloads import Downloads, DownloadError, File, ModelSet, fetch
from seam.engine import Engine
from seam.jobs import JobStore

WF = Path(__file__).resolve().parents[1] / "src" / "seam" / "workflows"
URL = "https://huggingface.co/org/repo/resolve/main/a.safetensors"


class Response(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class Opener:
    """Answers with what the test queued and records every request it was sent."""

    def __init__(self, *answers: object) -> None:
        self.answers, self.requests = list(answers), []

    def open(self, req: urllib.request.Request, timeout: float = 0) -> Response:
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer  # type: ignore[return-value]


def test_every_graph_file_has_a_set() -> None:
    """The table and the graphs never drift: every model file an open-weights graph loads is in its set."""
    spec = json.loads((WF / "slots.json").read_text(encoding="utf-8"))
    for model, entry in spec.items():
        if not isinstance(entry, dict) or entry.get("kind") == "partner":
            continue
        needed = {f for _node, _field, f in model_files(json.loads((WF / entry["workflow"]).read_text(encoding="utf-8")))}
        if not needed:
            continue
        s = downloads.set_for(model)
        assert s is not None, model
        assert needed <= {f.name for f in s.files}, (model, needed - {f.name for f in s.files})


def test_only_hugging_face_urls_and_defaults() -> None:
    assert all(f.url.startswith("https://huggingface.co/") for s in downloads.SETS.values() for f in s.files)
    assert [k for k, s in downloads.SETS.items() if s.default] == ["l25", "shot"]  # orbit, wan22, … need --only
    assert all(s.licenses for s in downloads.SETS.values())


def test_token_goes_only_with_gated_files(tmp_path: Path) -> None:
    ungated, gated = File("loras", URL, 3), File("loras", URL, 3, gated=True)
    op = Opener(Response(b"abc"), Response(b"abc"))
    fetch(ungated, tmp_path / "a.safetensors", "tok", opener=op)
    (tmp_path / "a.safetensors").unlink()
    fetch(gated, tmp_path / "a.safetensors", "tok", opener=op)
    assert op.requests[0].get_header("Authorization") is None
    assert op.requests[1].get_header("Authorization") == "Bearer tok"


def test_redirects_drop_the_token_off_hugging_face_and_refuse_http() -> None:
    h = downloads._Redirects()
    req = urllib.request.Request(URL, headers={"Authorization": "Bearer tok"})
    same = h.redirect_request(req, None, 302, "Found", {}, "https://huggingface.co/other")
    assert same.get_header("Authorization") == "Bearer tok"
    away = h.redirect_request(req, None, 302, "Found", {}, "https://cas-bridge.xethub.hf.co/signed")
    assert away.get_header("Authorization") is None and not away.unredirected_hdrs.get("Authorization")
    with pytest.raises(urllib.error.HTTPError):
        h.redirect_request(req, None, 302, "Found", {}, "http://huggingface.co/plain")
    assert h.max_redirections == 5


def test_errors_never_carry_the_token(tmp_path: Path) -> None:
    err = urllib.error.HTTPError(URL, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]
    with pytest.raises(DownloadError) as e:
        fetch(File("loras", URL, 3, gated=True), tmp_path / "a.safetensors", "secret-token", opener=Opener(err))
    assert "secret-token" not in str(e.value) and "hf auth login" in str(e.value) and "401" in str(e.value)


def test_resume_appends_on_206_restarts_on_200_and_promotes_on_416(tmp_path: Path) -> None:
    f, dst = File("loras", URL, 6), tmp_path / "a.safetensors"
    part = tmp_path / "a.safetensors.part"
    part.write_bytes(b"abc")
    op = Opener(Response(b"def", 206, {"Content-Range": "bytes 3-5/6"}))
    fetch(f, dst, None, opener=op)
    assert dst.read_bytes() == b"abcdef" and op.requests[0].get_header("Range") == "bytes=3-"

    dst.unlink()
    part.write_bytes(b"xyz")
    fetch(f, dst, None, opener=Opener(Response(b"abcdef", 200)))  # the server ignored the range
    assert dst.read_bytes() == b"abcdef"

    dst.unlink()
    part.write_bytes(b"abcdef")
    fetch(f, dst, None, opener=Opener(urllib.error.HTTPError(URL, 416, "Range", {}, None)))  # type: ignore[arg-type]
    assert dst.read_bytes() == b"abcdef"

    dst.unlink()
    part.write_bytes(b"abc")
    with pytest.raises(DownloadError):
        fetch(f, dst, None, opener=Opener(Response(b"def", 206, {"Content-Range": "bytes 0-2/6"})))
    assert not part.exists() and not dst.exists()


def test_only_hugging_face_and_only_inside_the_models_folder(tmp_path: Path) -> None:
    with pytest.raises(DownloadError):
        fetch(File("loras", "https://example.com/a.safetensors", 3), tmp_path / "a", None, opener=Opener())
    with pytest.raises(DownloadError):
        downloads.target(tmp_path / "models", File("../outside", URL, 3))
    (tmp_path / "models").mkdir()
    (tmp_path / "elsewhere").mkdir()
    try:  # a link (a junction on Windows behaves the same for resolve) out of the models folder
        (tmp_path / "models" / "loras").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    except OSError:  # Windows without the symlink right: a junction, which needs none
        import subprocess
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "models" / "loras"), str(tmp_path / "elsewhere")],
                              capture_output=True).returncode == 0
        if not made:
            pytest.skip("this machine cannot create links")
    with pytest.raises(DownloadError):
        downloads.target(tmp_path / "models", File("loras", URL, 3))


TINY = ModelSet("tiny", "Tiny", ("tiny",), (File("loras", URL, 3),), (downloads.APACHE,))


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(downloads.SETS, "tiny", TINY)
    (tmp_path / "models").mkdir()
    events: list[dict] = []
    done: list[bool] = []
    op = Opener(Response(b"abc"))
    m = Downloads(tmp_path / "models", tmp_path / "data", lambda kind, item: events.append(dict(item)),
                  on_done=lambda: done.append(True), opener=op)
    return m, events, done


def test_download_needs_acceptance_and_records_it(manager, tmp_path: Path) -> None:
    m, events, done = manager
    with pytest.raises(ValueError, match="accept"):
        m.start("tiny", accept=False)
    m.start("tiny", accept=True)
    m.thread.join(5)
    assert (tmp_path / "models" / "loras" / "a.safetensors").read_bytes() == b"abc"
    assert events[-1]["status"] == "done" and events[-1]["done"] == 3 and done == [True]
    record = json.loads((tmp_path / "data" / "licenses.json").read_text(encoding="utf-8"))
    assert record[0]["set"] == "tiny" and record[0]["licenses"][0]["name"] == "Apache 2.0"
    row = next(x for x in m.listing() if x["id"] == "tiny")
    assert row["accepted"] and row["missing"] == 0 and row["files"][0]["present"]


def test_download_refusals(manager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    m, _events, _done = manager
    with pytest.raises(KeyError):
        m.start("nope", accept=True)
    nowhere = Downloads(None, tmp_path / "data", lambda *_: None)
    with pytest.raises(ValueError, match="SEAM_MODELS_DIR"):
        nowhere.start("tiny", accept=True)
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(10, 10, 10))  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="disk space"):
        m.start("tiny", accept=True)
    monkeypatch.undo()
    monkeypatch.setitem(downloads.SETS, "tiny", TINY)
    gated = ModelSet("gated", "Gated", ("gated",), (File("loras", URL, 3, gated=True),), (downloads.LTX,))
    monkeypatch.setitem(downloads.SETS, "gated", gated)
    monkeypatch.setattr(downloads, "token", lambda: None)
    with pytest.raises(ValueError, match="hf auth login"):
        m.start("gated", accept=True)


def test_one_at_a_time_and_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(downloads.SETS, "tiny", TINY)
    (tmp_path / "models").mkdir()
    events: list[dict] = []

    class Slow(Opener):  # one byte per read, so the test can cancel in between
        def open(self, req, timeout=0):
            return Response(b"abc")

    m = Downloads(tmp_path / "models", tmp_path / "data", lambda k, i: events.append(dict(i)), opener=Slow())
    monkeypatch.setattr(downloads, "CHUNK", 1)
    m._cancel.set()
    m._state = {"set": "tiny", "status": "running"}
    with pytest.raises(RuntimeError, match="downloading"):
        m.start("tiny", accept=True)
    m._state = None
    m.start("tiny", accept=True)
    assert m.cancel("tiny") or events[-1]["status"] == "done"
    m.thread.join(5)
    assert events[-1]["status"] in ("cancelled", "done")
    assert not m.cancel("tiny")


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "models").mkdir()
    s = replace(Settings(), data_dir=tmp_path / "data", models_dir=tmp_path / "models")
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    monkeypatch.setattr(server, "downloads", Downloads(s.models_dir, s.data_dir, e.store.publish))
    monkeypatch.setattr(backends, "current", lambda _s: (backends.LOCAL, "test"))
    monkeypatch.setattr(server.model_status, "object_info", lambda *_a, **_k: None)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), s


def test_panel_lists_every_feature_and_both_backends(seam) -> None:
    client, _s = seam
    body = client.get("/api/downloads").json()
    assert body["blocker"] is None
    features = {m["feature"] for m in body["models"]}
    assert features == {"transitions", "shots", "orbit"}
    orbit = next(m for m in body["models"] if m["id"] == "orbit")
    assert orbit["set"] == "orbit" and orbit["cloud"]["state"] == "not_offered"
    assert orbit["local"]["state"] == "not_checked"
    assert {x["id"] for x in body["sets"]} >= {"l25", "shot", "orbit", "wan22"}


def test_server_refuses_without_acceptance_on_cloud_and_off_loopback(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    client, s = seam
    assert client.post("/api/downloads/orbit", json={"accept": False}).status_code == 422
    assert client.post("/api/downloads/nope", json={"accept": True}).status_code == 404
    monkeypatch.setattr(backends, "current", lambda _s: (backends.CLOUD, "test"))
    assert client.post("/api/downloads/orbit", json={"accept": True}).status_code == 409
    monkeypatch.setattr(backends, "current", lambda _s: (backends.LOCAL, "test"))
    monkeypatch.setattr(server, "settings", replace(s, host="0.0.0.0"))
    r = client.post("/api/downloads/orbit", json={"accept": True})
    assert r.status_code == 403 and "localhost" in r.json()["detail"]
    assert client.post("/api/downloads/orbit/cancel").status_code == 403


def test_models_folder_comes_from_the_local_comfyui_when_not_set(seam, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, s = seam
    monkeypatch.setattr(server, "downloads", Downloads(None, s.data_dir, lambda *_a: None))
    monkeypatch.setattr(server.model_status, "comfy_models_dir", lambda: None)
    body = client.get("/api/downloads").json()
    assert "Start your local ComfyUI" in body["blocker"] and body["models_dir"] is None
    assert client.post("/api/downloads/orbit", json={"accept": True}).status_code == 409
    asked = []
    monkeypatch.setattr(server.model_status, "comfy_models_dir", lambda: asked.append(1) or s.models_dir)
    assert client.get("/api/downloads").json()["models_dir"] is None and not asked  # just said no: not asked again yet
    monkeypatch.setattr(server, "_root_asked", 0.0)
    body = client.get("/api/downloads").json()
    assert body["blocker"] is None and body["models_dir"] == str(s.models_dir) and body["free"]
    monkeypatch.setattr(server.model_status, "comfy_models_dir", lambda: None)  # ComfyUI stopped later
    assert client.get("/api/downloads").json()["models_dir"] == str(s.models_dir) and len(asked) == 1


def test_a_cloud_list_that_fails_to_read_is_not_asked_for_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from seam import models
    from seam.config import Settings

    def broken(_settings, _backend):
        raise RuntimeError("no network")

    monkeypatch.setattr(models, "_fetch", broken)
    monkeypatch.setattr(models, "_cache", {})
    settings = Settings()
    assert models.info_or_warm(settings, "cloud") == (None, True)
    for _ in range(100):
        if not models._warming:
            break
        threading.Event().wait(0.05)
    assert models.info_or_warm(settings, "cloud") == (None, False)


def test_comfyui_models_folder_is_read_from_its_folder_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import json as _json

    from seam import models

    root = tmp_path / "ComfyUI" / "models"
    (root / "checkpoints").mkdir(parents=True)
    asked = []

    def urlopen(url, timeout=0):
        asked.append(url)
        return io.BytesIO(_json.dumps({"checkpoints": [str(root / "checkpoints"), str(tmp_path / "output" / "checkpoints")]}).encode())

    monkeypatch.setattr(models.urllib.request, "urlopen", urlopen)
    monkeypatch.delenv("SEAM_COMFYUI_URL", raising=False)
    assert models.comfy_models_dir() == root and asked == ["http://127.0.0.1:8188/internal/folder_paths"]
    monkeypatch.setenv("SEAM_COMFYUI_URL", "http://192.168.1.20:8188")  # another machine: its path is not ours
    assert models.comfy_models_dir() is None and len(asked) == 1


def test_the_panel_does_not_wait_for_comfy_cloud(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from seam import models

    client, _s = seam
    release, started = threading.Event(), threading.Event()

    def slow(_settings, backend):
        started.set()
        release.wait(10)
        return {}

    monkeypatch.setattr(backends, "cloud_key", lambda _s: ("k", "test"))
    monkeypatch.setattr(models, "_fetch", slow)
    monkeypatch.setattr(models, "_cache", {})
    body = client.get("/api/downloads").json()  # returns while Comfy Cloud is still being read
    assert body["checking"] is True and started.wait(5)
    l25 = next(m for m in body["models"] if m["cloud"]["state"] == "not_checked")
    assert "checking" in l25["cloud"]["reason"]
    release.set()
    for _ in range(100):
        if not models._warming:
            break
        threading.Event().wait(0.05)
    assert client.get("/api/downloads").json()["checking"] is False


def _info(graph: dict, with_files: bool) -> dict:
    """A fake /object_info: every node class of the graph, its model inputs listing the graph's files or nothing."""
    info: dict = {}
    for node in graph.values():
        info.setdefault(node["class_type"], {"input": {"required": {}}})
    if with_files:
        for cls, field, f in model_files(graph):
            info[cls]["input"]["required"].setdefault(field, [[]])[0].append(f)
    return info


def test_panel_states_ready_missing_needs_nodes(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    from seam.workflows import load_spec

    client, s = seam
    spec = load_spec(s.workflows_dir, "orbit")
    states = {}
    for name, info in (("ready", _info(spec.graph(), True)), ("missing", _info(spec.graph(), False)), ("needs_nodes", {})):
        monkeypatch.setattr(server.model_status, "object_info", lambda _s, b, *_a, info=info, **_k: info if b == backends.LOCAL else None)
        orbit = next(m for m in client.get("/api/downloads").json()["models"] if m["id"] == "orbit")
        states[name] = orbit["local"]["state"]
    assert states == {"ready": "ready", "missing": "missing", "needs_nodes": "needs_nodes"}


def test_cloud_blocker_per_set_and_remaining_counts_parts(seam, monkeypatch: pytest.MonkeyPatch) -> None:
    client, s = seam
    monkeypatch.setattr(backends, "current", lambda _s: (backends.CLOUD, "test"))
    sets = {x["id"]: x for x in client.get("/api/downloads").json()["sets"]}
    assert "switch to Your GPU" in sets["orbit"]["blocker"]  # Cloud does not offer the orbit
    f = downloads.SETS["orbit"].files[-1]
    (s.models_dir / f.folder).mkdir()
    (s.models_dir / f.folder / (f.name + ".part")).write_bytes(b"x" * 1000)
    row = next(x for x in server.downloads.listing() if x["id"] == "orbit")
    assert row["remaining"] == row["missing"] - 1000


def test_cli_default_sets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("dl_cli", Path(__file__).resolve().parents[1] / "scripts" / "download_models.py")
    cli = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(cli)  # type: ignore[union-attr]
    fetched: list[str] = []
    monkeypatch.setattr(cli, "present", lambda _r, _f: False)
    monkeypatch.setattr(cli, "token", lambda: "tok")
    monkeypatch.setattr(cli, "fetch", lambda f, *_a, **_k: fetched.append(f.url))
    monkeypatch.setattr(sys, "argv", ["download_models.py", "--models-dir", str(tmp_path)])
    assert cli.main() == 0
    assert set(fetched) == {f.url for k in ("l25", "shot") for f in downloads.SETS[k].files}


def test_remaining_without_models_dir_is_the_full_size(tmp_path: Path) -> None:
    d = downloads.Downloads(None, tmp_path, lambda *_a: None)
    row = next(x for x in d.listing() if x["id"] == "shot_klein")
    assert row["remaining"] == row["missing"] == downloads.SETS["shot_klein"].size > 0
