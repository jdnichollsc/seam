from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from seam.config import Settings
from seam.engine import Engine, TransitionRequest
from seam.jobs import JobStore


@pytest.fixture()
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("SEAM_SHOW_CANDIDATES", "1")
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work
    # fire (the only LTX-2.3 preset) is deferred after qualification; keep the routing under test as a candidate
    e.presets["fire"] = replace(e.presets["fire"], status="candidate")
    return e


@pytest.fixture()
def images(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (64, 32)).save(a)
    Image.new("RGB", (64, 32)).save(b)
    return str(a), str(b)


def test_preset_model_routing(engine: Engine, images) -> None:
    a, b = images
    assert engine.model_for(TransitionRequest(a, b, "fire")) == "l23"
    assert engine.model_for(TransitionRequest(a, b, "whip_pan")) == "l25"
    assert engine.model_for(TransitionRequest(a, b, "smoke")) == "l25"  # bound in presets.yaml
    assert engine.model_for(TransitionRequest(a, b, "fire", model="l25")) == "l25"  # explicit override


def test_only_launch_presets_are_exposed_by_default(engine: Engine, images, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = images
    monkeypatch.delenv("SEAM_SHOW_CANDIDATES", raising=False)
    exposed = engine.exposed_presets()
    assert len(exposed) >= 3 and all(p.status == "launch" for p in exposed.values())  # PLAN v4 floor
    with pytest.raises(ValueError, match="unknown preset"):
        engine.validate(TransitionRequest(a, b, "fire"))  # not launch: hidden by default
    assert "intermission" not in engine.exposed_presets()  # deferred never ships


def test_unknown_model_is_rejected(engine: Engine, images) -> None:
    a, b = images
    with pytest.raises(ValueError, match="not enabled"):
        engine.validate(TransitionRequest(a, b, "smoke", model="h3"))


def test_submit_pins_model_and_refine_follows_new_preset(engine: Engine, images) -> None:
    a, b = images
    job = engine.submit(TransitionRequest(a, b, "fire"))
    assert job.params["model"] == "l23" and job.params["seed"]
    child = engine.refine(job.id, {"preset": "whip_pan"})
    assert child.parent_id == job.id and child.params["model"] == "l25"
    same = engine.refine(job.id, {"prompt_extra": "slower"})
    assert same.params["model"] == "l23" and same.params["seed"] != job.params["seed"]


def test_launch_presets_are_locked_to_their_qualified_model(engine: Engine, images) -> None:
    a, b = images
    launch = [p for p in engine.presets.values() if p.status == "launch"]
    assert launch and all(p.model for p in launch)  # qualification is bound to a model, not to SEAM_MODEL
    with pytest.raises(ValueError, match="qualified on model 'l25' only"):
        engine.validate(TransitionRequest(a, b, "smoke", model="l23"))
    engine.settings = replace(engine.settings, model="l23")  # a different default must not reroute a launch effect
    assert engine.model_for(TransitionRequest(a, b, "water_bend")) == "l25"
    engine.validate(TransitionRequest(a, b, "whip_pan"))


def test_launch_presets_offer_only_their_qualified_lengths(engine: Engine, images) -> None:
    a, b = images
    engine.presets["smoke"] = replace(engine.presets["smoke"], seconds=(5, 10))
    engine.presets["whip_pan"] = replace(engine.presets["whip_pan"], seconds=(5,))
    engine.validate(TransitionRequest(a, b, "smoke", seconds=10))
    with pytest.raises(ValueError, match="qualified at 5 s only"):
        engine.validate(TransitionRequest(a, b, "whip_pan", seconds=10))
    with pytest.raises(ValueError, match="seconds must be one of"):
        engine.validate(TransitionRequest(a, b, "smoke", seconds=7))
    job = engine.submit(TransitionRequest(a, b, "smoke", seconds=10))
    assert engine.refine(job.id, {"preset": "whip_pan"}).params["seconds"] == 5  # falls back to a qualified length
    assert engine.refine(job.id, {"prompt_extra": "slower"}).params["seconds"] == 10
