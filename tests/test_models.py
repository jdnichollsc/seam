"""Picking a model per transition: catalog, availability, requests, partner key. No network."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import backend as backends
from seam import models, server
from seam.config import Settings
from seam.doctor import model_files
from seam.engine import Engine, TransitionRequest
from seam.jobs import JobStore
from seam.presets import load_presets, render_prompt
from seam.workflows import build, load_catalog, missing_slots

KEY = "comfyui-test-secret-key"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("COMFY_API_KEY", "COMFY_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SEAM_BACKEND", "local")
    models._cache.clear()


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    s = Settings(data_dir=tmp_path, comfy_base_url="http://127.0.0.1:8189")
    s.ensure_dirs()
    return Engine(s, JobStore(s.db_path), client=object())


@pytest.fixture
def images(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (64, 64)).save(a)
    Image.new("RGB", (64, 64)).save(b)
    return str(a), str(b)


def info_for(*specs, drop_class=None, drop_file=None) -> dict:
    """A node list that offers exactly what these graphs need."""
    out: dict = {}
    for spec in specs:
        graph = spec.graph()
        for n in graph.values():
            out.setdefault(n["class_type"], {"input": {"required": {}}})
        for node, field, f in model_files(graph):
            if f != drop_file:
                out[node]["input"]["required"].setdefault(field, [[]])[0].append(f)
    out.pop(drop_class, None)
    return out


def test_every_catalog_model_is_wired(engine: Engine) -> None:
    for mid, spec in engine.catalog().items():
        assert not missing_slots(spec), mid
        assert {"image_a", "image_b", "prompt", "seed", "seconds"} <= set(spec.slots), mid
        assert spec.label and spec.seconds and spec.output_node in spec.graph(), mid
        if spec.kind == "partner":
            assert spec.backends == ("cloud",), mid
    assert "describe" not in engine.catalog()


def test_availability_reads_the_backend(engine: Engine) -> None:
    catalog = engine.catalog()
    wan = catalog["wan22"]
    assert models.why_not(wan, "local", info_for(wan)) == (True, "")
    assert models.why_not(wan, "local", None) == (None, "not checked")
    cls = next(iter(wan.graph().values()))["class_type"]
    assert models.why_not(wan, "local", info_for(wan, drop_class=cls))[0] is False
    file = model_files(wan.graph())[0][2]
    assert models.why_not(wan, "local", info_for(wan, drop_file=file)) == (False, "model files not installed")
    assert models.why_not(catalog["kling_v3"], "local", {}) == (False, "needs Comfy Cloud")


def test_validation_never_fetches_and_refuses_what_is_known(engine: Engine, images, monkeypatch) -> None:
    a, b = images
    monkeypatch.setattr(models, "_fetch", lambda *x: pytest.fail("validation fetched the node list"))
    engine.validate(TransitionRequest(a, b, "smoke", model="wan22"))  # nothing known yet: the render will say
    with pytest.raises(ValueError, match="needs Comfy Cloud"):
        engine.validate(TransitionRequest(a, b, "smoke", model="kling_v3"))


def test_the_default_is_the_effects_model_and_an_explicit_pick_is_kept(engine: Engine, images) -> None:
    a, b = images
    assert engine.submit(TransitionRequest(a, b, "smoke"), enqueue=False).params["model"] == "l25"
    job = engine.submit(TransitionRequest(a, b, "smoke", model="wan22"), enqueue=False)
    assert job.params["model"] == "wan22"
    next_ = engine.refine(job.id, {"preset": "whip_pan"}, enqueue=False)
    assert next_.params["model"] == "wan22"  # survives a change of effect, and again on the next take
    assert engine.refine(next_.id, {"preset": "smoke"}, enqueue=False).params["model"] == "wan22"
    assert engine.refine(next_.id, {"model": ""}, enqueue=False).params["model"] == "l25"


def test_lengths_follow_the_model(engine: Engine, images) -> None:
    a, b = images
    engine.validate(TransitionRequest(a, b, "smoke", seconds=10))
    with pytest.raises(ValueError, match="renders at 5 s only"):
        engine.validate(TransitionRequest(a, b, "smoke", seconds=10, model="wan22"))
    job = engine.submit(TransitionRequest(a, b, "smoke", seconds=10), enqueue=False)
    assert engine.refine(job.id, {"model": "wan22"}, enqueue=False).params["seconds"] == 5


def test_each_part_of_a_sequence_keeps_its_model(engine: Engine, images) -> None:
    a, b = images
    shots = [{"first": p, "last": p} for p in (a, b, a)]
    seq = engine.create_sequence(shots, [{"preset": "smoke", "model": "wan22"}, {"preset": "whip_pan"}])
    first, second = (engine.store.get(j) for j in seq.segments)
    assert (first.params["model"], second.params["model"]) == ("wan22", "l25")


def test_wan_counts_frames_and_plain_prompts_carry_no_ltx_text(engine: Engine) -> None:
    wan = engine.catalog()["wan22"]
    assert wan.seconds_as_frames and wan.fps == 16
    prompt = render_prompt("plain", engine.presets["smoke"], a_desc="a cat", b_desc="a dog", extra="slowly", story="A trip")
    assert "zhuanchang" not in prompt and "first frame anchor" not in prompt
    assert all(t in prompt for t in ("A trip", "A cat", "a dog", "slowly"))
    assert engine.prompt_for(TransitionRequest("a", "b", "smoke", model="kling_v3", a_desc="a cat")).startswith("A cat")


class Stub:
    """Captures what is submitted; stops the take right after."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None
        self.workflows = SimpleNamespace(from_file=lambda f: SimpleNamespace(set_input=lambda *a: None))
        self.assets = SimpleNamespace(from_file=lambda p: "asset")

    def submit(self, wf, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here")


@pytest.mark.parametrize("model,expect_key", [("kling_v3", True), ("wan22", False)])
def test_only_partner_models_send_the_key_and_it_is_stored_nowhere(engine, images, monkeypatch, model, expect_key) -> None:
    a, b = images
    monkeypatch.setenv("SEAM_BACKEND", "cloud")
    backends.save_key(engine.settings, KEY)
    stub = Stub()
    engine.client = stub
    job = engine.submit(TransitionRequest(a, b, "smoke", model=model, a_desc="", b_desc=""), enqueue=False)
    with pytest.raises(RuntimeError, match="stop here"):
        engine._execute(job)
    assert stub.kwargs == ({"api_key": KEY} if expect_key else {})
    assert KEY not in json.dumps(engine.store.get(job.id).params) + json.dumps(engine.store.get(job.id).extra)


def test_continuing_stays_on_the_grid(engine: Engine, images, monkeypatch) -> None:
    a, b = images
    job = engine.submit(TransitionRequest(a, b, "smoke"), enqueue=False)
    (engine.settings.outputs_dir / "x.mp4").write_bytes(b"")
    engine.store.update(job.id, status="succeeded", output="x.mp4")
    monkeypatch.setattr("seam.engine.media.probe", lambda p: SimpleNamespace(width=1280, height=720))
    seq = engine.continue_take(job.id, {"first": b, "last": b}, {"preset": "smoke"})
    assert (seq.width, seq.height) == (1280, 704)


def test_the_api_lists_models_and_takes_a_model(engine: Engine, images, monkeypatch) -> None:
    monkeypatch.setattr(server, "settings", engine.settings)
    monkeypatch.setattr(server, "engine", engine)
    monkeypatch.setattr(models, "_fetch", lambda *x: None)
    c = TestClient(server.app, base_url="http://127.0.0.1")
    rows = {m["id"]: m for m in c.get("/api/models").json()["models"]}
    assert rows["l25"]["available"] is None and rows["kling_v3"]["available"] is False
    assert rows["kling_v3"]["kind"] == "partner" and rows["wan22"]["seconds"] == [5]
    assert "model" in server.TransitionIn.model_fields and "model" in server.RefineIn.model_fields
    assert "model" in server.SegmentIn.model_fields


def test_a_refine_with_an_unknown_model_says_so(engine: Engine, images) -> None:
    a, b = images
    job = engine.submit(TransitionRequest(a, b, "smoke"), enqueue=False)
    with pytest.raises(ValueError, match="unknown model"):
        engine.refine(job.id, {"model": "nope"}, enqueue=False)


def test_an_old_node_list_refuses_nothing(engine: Engine, monkeypatch) -> None:
    spec = engine.catalog()["wan22"]
    key = models._cache_key(engine.settings, "local")
    models._cache[key] = (0.0, {})
    monkeypatch.setattr(models.time, "monotonic", lambda: models.CACHE_S + 1)
    assert models.known_refusal(engine.settings, spec, "local") is None
    models._cache[key] = (models.CACHE_S, {})
    assert models.known_refusal(engine.settings, spec, "local")


def test_low_frame_rate_clips_get_a_sheet_and_joins_keep_every_frame(tmp_path: Path) -> None:
    import subprocess

    from seam import media

    try:
        media._ensure_ffmpeg()
    except RuntimeError:
        pytest.skip("no ffmpeg")

    def clip(name, fps):
        p = tmp_path / name
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x192:rate={fps}:duration=2",
                        "-pix_fmt", "yuv420p", str(p)], check=True)
        return p

    slow, fast = clip("slow.mp4", 16), clip("fast.mp4", 24)
    assert media.contact_sheet(slow, tmp_path / "s.png").is_file()
    media.join([slow, fast], tmp_path / "j.mp4", 320, 192, max(media.probe(p).fps for p in (slow, fast)))
    assert media.probe(tmp_path / "j.mp4").fps == 24
