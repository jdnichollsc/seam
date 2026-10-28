from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from seam import media
from seam.config import PACKAGE_DIR, Settings
from seam.jobs import JobStore
from seam.presets import load_presets, render_prompt
from seam.workflows import load_spec, missing_slots

WF = PACKAGE_DIR / "workflows"
PRESETS = PACKAGE_DIR / "presets" / "presets.yaml"


@pytest.mark.parametrize("model", ["l25", "l23", "describe"])
def test_slot_maps_resolve_against_graph(model: str) -> None:
    """Template drift guard: every named slot must exist in the exported API graph."""
    spec = load_spec(WF, model)
    assert spec.workflow_file.exists()
    assert missing_slots(spec) == []
    assert spec.output_node in spec.graph()


def test_core_model_has_required_slots() -> None:
    spec = load_spec(WF, "l25")
    assert {"image_a", "image_b", "prompt", "seed", "seconds", "width", "height"} <= set(spec.slots)


@pytest.mark.parametrize("w,h", [(720, 1280), (1280, 720), (1024, 1024), (4032, 3024)])
def test_output_size_keeps_aspect_and_multiple(w: int, h: int) -> None:
    ow, oh = media.output_size(w, h)
    assert ow % 32 == 0 and oh % 32 == 0
    assert abs(ow / oh - w / h) < 0.06
    assert 0.75e6 < ow * oh < 1.05e6


def test_presets_load_and_render_for_core_model() -> None:
    presets = load_presets(PRESETS)
    assert len(presets) >= 3
    for p in presets.values():
        prompt = render_prompt("l25", p, a_desc="a red door", b_desc="a blue sea", extra="slow")
        assert prompt.startswith("Use the provided start image as the first frame")
        assert "a red door" in prompt and "a blue sea" in prompt and "{" not in prompt


def test_job_store_lifecycle_and_lineage(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "t.db")
    q = store.subscribe()
    a = store.create({"preset": "morph"})
    b = store.create({"preset": "fire"}, parent_id=a.id)
    store.update(a.id, status="running", comfy_job_id="abc")
    assert store.get(b.id).parent_id == a.id
    assert store.get(a.id).comfy_job_id == "abc"
    assert [j.id for j in store.list()] [:2] == [b.id, a.id]
    assert q.qsize() == 3
    assert store.mark_interrupted() and store.get(b.id).status == "interrupted"


def test_normalize_image_applies_rgb_png(tmp_path: Path) -> None:
    src = tmp_path / "in.jpg"
    Image.new("RGBA", (40, 20), (255, 0, 0, 128)).convert("RGB").save(src)
    dst = media.normalize_image(src, tmp_path / "out.png")
    with Image.open(dst) as im:
        assert im.mode == "RGB" and im.size == (40, 20)


class _FakeAsset:
    def __init__(self, path: Path) -> None:
        self.path = path


class _FakeWorkflow:
    def __init__(self) -> None:
        self.inputs: dict[tuple[str, str], object] = {}

    def set_input(self, node: str, field: str, value: object) -> None:
        self.inputs[(node, field)] = value


class _FakeClient:
    class workflows:  # noqa: N801
        @staticmethod
        def from_file(path):
            return _FakeWorkflow()

    class assets:  # noqa: N801
        @staticmethod
        def from_file(path):
            return _FakeAsset(Path(path))


def test_build_injects_only_through_slots(tmp_path: Path) -> None:
    from seam.workflows import build

    spec = load_spec(WF, "l25")
    wf = build(_FakeClient(), spec, tmp_path / "a.png", tmp_path / "b.png",
               {"prompt": "x", "seed": 7, "width": 1280, "height": 704, "seconds": 5})
    assert wf.inputs[tuple(spec.slots["prompt"])] == "x"
    assert wf.inputs[tuple(spec.slots["seed"])] == 7
    with pytest.raises(KeyError):
        build(_FakeClient(), spec, tmp_path / "a.png", tmp_path / "b.png", {"not_a_slot": 1})


def test_settings_defaults_are_local_only() -> None:
    s = Settings()
    assert s.host == "127.0.0.1"
    assert s.comfy_base_url.startswith("http://127.0.0.1")


def test_research_models_are_not_shipped() -> None:
    import json
    shipped = json.loads((WF / "slots.json").read_text(encoding="utf-8"))
    assert {k for k in shipped if not k.startswith("_")} == {"l25", "l23", "describe"}
    assert not any(f.name.startswith(("h3", "wan")) for f in WF.glob("*.api.json") if f.name != "wan22_flf2v.api.json")


def test_review_sheet_has_whole_clip_and_middle_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        media._ensure_ffmpeg()
    except RuntimeError:
        pytest.skip("ffmpeg not installed")
    clip = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=4:size=160x96:rate=24",
                    "-pix_fmt", "yuv420p", str(clip)], check=True)
    seen: list[float] = []
    real = media.frame_at
    monkeypatch.setattr(media, "frame_at", lambda v, t, h=None: (seen.append(t), real(v, t, h))[1])
    out = media.review_sheet(clip, tmp_path / "sheet2.png", n=8, height=60)
    with Image.open(out) as im:
        assert im.height == 120 and im.width == 8 * 100  # 160x96 scaled to height 60 -> 100 wide
    assert seen[0] == 0 and seen[7] == pytest.approx(3.95)  # row 1: whole clip
    assert seen[8] == pytest.approx(1.2) and seen[15] == pytest.approx(2.8)  # row 2: 30%..70%
    assert not (tmp_path / "sheet2.tmp.png").exists()


def test_review_sheet_is_built_once_under_concurrent_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    from seam import server

    calls: list[int] = []

    def slow_build(video: Path, dst: Path) -> Path:
        calls.append(1)
        time.sleep(0.2)
        dst.write_bytes(b"png")
        return dst

    monkeypatch.setattr(server.media, "review_sheet", slow_build)
    dst = tmp_path / "x_sheet2.png"
    threads = [threading.Thread(target=server._build_once, args=(tmp_path / "x.mp4", dst)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1] and dst.read_bytes() == b"png"
