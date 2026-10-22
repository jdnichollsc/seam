from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from seam import describe as shots
from seam.config import Settings
from seam.describe import ShotText, parse
from seam.engine import Engine, TransitionRequest
from seam.jobs import JobStore

# The whip_pan prompt of the qualification render (pair p1, seed 1, hand-written shots; see dev/PRESET_REVIEW.md).
QUALIFIED_WHIP_PAN = (
    "Use the provided start image as the first frame and the provided end image as the final frame anchor. "
    "The video opens on a young woman with short black hair in a red leather jacket on a rainy neon city street at "
    "night. The camera whips sideways away from the woman in the red jacket in a fast blurred pan and lands precisely "
    "on the woman in the white dress on the beach, which comes into sharp focus. Camera: fast horizontal whip pan "
    "with motion blur, settling at the end. The sound is a sharp swish. The video ends on the young woman in a "
    "flowing white summer dress on a sandy beach at golden-hour sunset.")
P1: dict[str, Any] = dict(a_desc="a young woman with short black hair in a red leather jacket on a rainy neon city street at night",
          b_desc="the young woman in a flowing white summer dress on a sandy beach at golden-hour sunset",
          a_ref="the woman in the red jacket", b_ref="the woman in the white dress on the beach")


@pytest.fixture()
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.setenv("SEAM_SHOW_CANDIDATES", "1")
    s = Settings(data_dir=tmp_path)
    s.ensure_dirs()
    return Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work


@pytest.fixture()
def images(tmp_path: Path) -> tuple[str, str]:
    a, b = tmp_path / "uploads" / "a.png", tmp_path / "uploads" / "b.png"
    Image.new("RGB", (64, 32)).save(a)
    Image.new("RGB", (64, 32)).save(b)
    return str(a), str(b)


@pytest.mark.parametrize("text, desc, ref", [
    ("a young woman with a serious expression on a wet street at night\nthe young woman in the leather jacket",
     "a young woman with a serious expression on a wet street at night", "the young woman in the leather jacket"),
    ("Line 1: A white sneaker on dark wet pavement.\nLine 2: The white sneaker.", "a white sneaker on dark wet pavement",
     "the white sneaker"),
    ('"an old car in the rain"\n\n', "an old car in the rain", ""),
    ("a red mug\nred mug on a table", "a red mug", ""),
])
def test_parse_keeps_the_template_shapes(text: str, desc: str, ref: str) -> None:
    assert parse(text) == ShotText(desc=desc, ref=ref)


@pytest.mark.parametrize("text", ["", "An intense gaze. Rainy street.", "The woman stands on a beach",
                                  "Here is the description of the image you sent:"])
def test_parse_rejects_what_the_template_cannot_take(text: str) -> None:
    assert parse(text) is None


def test_prompt_with_shot_text_matches_the_qualified_prompt(engine: Engine, images) -> None:
    a, b = images
    assert engine.prompt_for(TransitionRequest(a, b, "whip_pan", **P1)) == QUALIFIED_WHIP_PAN


def test_empty_shot_text_means_the_generic_prompt(engine: Engine, images) -> None:
    a, b = images
    p = engine.prompt_for(TransitionRequest(a, b, "whip_pan", a_desc="", b_desc="", a_ref="", b_ref=""))
    assert "away from the first scene" in p and "lands precisely on the second scene" in p
    assert "opens on" not in p and p.endswith("The video ends exactly on the end image.")


def test_worker_reads_only_the_shots_left_undescribed(engine: Engine, images, monkeypatch) -> None:
    a, b = images
    read: list[str] = []
    monkeypatch.setattr(engine, "describe", lambda p: read.append(Path(p).name) or ShotText("a blue car", "the car"))
    job = engine.submit(TransitionRequest(a, b, "whip_pan", b_desc=""))  # B explicitly cleared by the user
    req = engine._fill_descriptions(job, TransitionRequest(**job.params))
    assert read == ["a.png"]
    assert (req.a_desc, req.a_ref, req.b_desc, req.b_ref) == ("a blue car", "the car", "", None)
    assert engine.store.get(job.id).params["a_desc"] == "a blue car"  # a new take reuses the same reading


def test_refine_with_a_corrected_description_drops_the_old_label(engine: Engine, images) -> None:
    a, b = images
    job = engine.submit(TransitionRequest(a, b, "smoke", **P1))
    child = engine.refine(job.id, {"a_desc": "an angry young woman in a red leather jacket on a rainy street"})
    assert child.params["a_ref"] == "" and child.params["b_ref"] == P1["b_ref"]
    assert engine.refine(job.id, {"preset": "whip_pan"}).params["a_ref"] == P1["a_ref"]  # untouched otherwise


def test_a_failed_reading_falls_back_to_the_generic_prompt(engine: Engine, images, monkeypatch) -> None:
    a, b = images

    def boom(_: Path) -> ShotText:
        raise RuntimeError("gpu busy")

    monkeypatch.setattr(engine, "describe", boom)
    job = engine.submit(TransitionRequest(a, b, "smoke"))
    req = engine._fill_descriptions(job, TransitionRequest(**job.params))
    assert (req.a_desc, req.b_desc) == ("", "")
    assert "gpu busy" in engine.store.get(job.id).extra["describe_error"]


def test_describe_is_cached_per_upload(engine: Engine, images, monkeypatch) -> None:
    a, _ = images
    calls: list[Path] = []
    monkeypatch.setattr(shots, "run", lambda client, spec, image: calls.append(image) or ShotText("a cat", "the cat"))
    assert engine.describe(Path(a)) == ShotText("a cat", "the cat")
    assert engine.describe(Path(a)) == ShotText("a cat", "the cat")
    assert len(calls) == 1
