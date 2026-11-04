from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from seam import receipt
from seam.config import Settings
from seam.engine import Engine, TransitionRequest
from seam.jobs import JobStore
from seam.presets import NO_EFFECT, render_prompt


@pytest.fixture()
def engine(tmp_path: Path) -> Engine:
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    return Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work


@pytest.fixture()
def images(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "a.png", tmp_path / "b.png"
    Image.new("RGB", (64, 32)).save(a)
    Image.new("RGB", (64, 32)).save(b)
    return str(a), str(b)


def test_no_effect_needs_a_prompt_and_takes_the_models_lengths(engine: Engine, images) -> None:
    a, b = images
    with pytest.raises(ValueError, match="needs a prompt"):
        engine.validate(TransitionRequest(a, b, NO_EFFECT))
    engine.validate(TransitionRequest(a, b, NO_EFFECT, prompt_extra="the camera orbits her", seconds=8))
    assert engine.lengths(NO_EFFECT, "l25") == engine.spec("l25").seconds


@pytest.mark.parametrize("model", ["l25", "l23", "plain"])
def test_the_prompt_is_the_whole_direction(engine: Engine, model: str) -> None:
    prompt = render_prompt(model, engine.presets[NO_EFFECT], extra="The camera orbits her slowly", b_desc="a beach")
    assert "The camera orbits her slowly" in prompt
    assert "Camera:" not in prompt and "The sound is" not in prompt and "zhuanchang" not in prompt


def test_the_effect_lora_is_off_and_the_receipt_says_so(engine: Engine, images) -> None:
    a, b = images
    job = engine.submit(TransitionRequest(a, b, NO_EFFECT, prompt_extra="slow dolly in", model="l23"), enqueue=False)
    assert receipt.render_params(job, engine.spec("l23"))["lora_strength"] == 0
    other = engine.submit(TransitionRequest(a, b, "whip_pan", model="l23"), enqueue=False)
    assert "lora_strength" not in receipt.render_params(other, engine.spec("l23"))


def test_it_is_offered_as_no_effect(engine: Engine) -> None:
    assert engine.presets[NO_EFFECT].name == "No effect" and NO_EFFECT in engine.exposed_presets()
