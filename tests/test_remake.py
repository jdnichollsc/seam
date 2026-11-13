"""Remake from a video: the cuts ffmpeg finds, the beats they become, and the sequence draft."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from seam import media, remake
from seam.engine import Engine

COLORS = ("black", "white", "navy", "yellow", "darkred", "cyan", "darkgreen", "pink", "purple", "orange", "maroon", "lightgray")  # dark, bright, dark, ...
FPS = 24


def _video(dst: Path, lengths: list[float]) -> Path:
    """Flat colours, one per shot, cut hard at the given lengths."""
    try:
        media._ensure_ffmpeg()
    except RuntimeError:
        pytest.skip("ffmpeg not installed")
    inputs: list[str] = []
    for i, sec in enumerate(lengths):
        inputs += ["-f", "lavfi", "-i", f"color=c={COLORS[i % len(COLORS)]}:s=320x180:r={FPS}:d={sec}"]
    graph = "".join(f"[{i}:v]" for i in range(len(lengths))) + f"concat=n={len(lengths)}:v=1:a=0"
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-pix_fmt", "yuv420p", str(dst)], check=True)
    return dst


def test_five_cuts_give_six_shots_within_a_frame(tmp_path: Path) -> None:
    lengths = [2.0, 1.5, 3.0, 0.75, 2.5, 1.25]
    found = remake.analyze(_video(tmp_path / "ref.mp4", lengths))
    assert found["source"]["width"] == 320 and abs(found["source"]["duration"] - sum(lengths)) < 0.05
    assert len(found["shots"]) == 6
    at = 0.0
    for shot, sec in zip(found["shots"], lengths):
        assert abs(shot["start"] - at) <= 1 / FPS + 1e-3
        at += sec
    assert abs(found["shots"][-1]["end"] - sum(lengths)) < 0.05


def test_a_flash_of_one_frame_is_not_a_shot(tmp_path: Path) -> None:
    found = remake.analyze(_video(tmp_path / "ref.mp4", [2.0, 1 / FPS, 2.0, 1 / FPS]))
    assert [(s["start"], round(s["end"], 1)) for s in found["shots"]] == [(0.0, 2.0), (2.0, 4.1)]


def test_draft_is_a_sequence_the_engine_accepts(tmp_path: Path) -> None:
    video = _video(tmp_path / "ref.mp4", [2.0, 12.0, 0.25, 3.0])
    shots = remake.analyze(video)["shots"]
    d = remake.draft(video, shots, tmp_path / "work")
    assert [s["hold"] for s in d["shots"]] == [2.0, 10.0, 0.5, 3.0] and all(Path(s["image"]).is_file() for s in d["shots"])
    assert d["segments"] == [{"preset": "cut"}] * 3 and d["story"] == ""
    assert any("shot 2 lasts 12.0 s" in n for n in d["notes"]) and any("shot 3" in n for n in d["notes"])
    Engine._check_shots(d["shots"], [True] * 3)

    f = remake.draft(video, shots, tmp_path / "footage", footage=True)
    assert ["clip" in s for s in f["shots"]] == [True, True, False, True]  # the 0.25 s shot can only be an image
    assert abs(media.probe(Path(f["shots"][1]["clip"])).duration - 12.0) < 0.1 and f["shots"][2]["hold"] == 0.5
    Engine._check_shots(f["shots"], [True] * 3)


def test_more_shots_than_a_sequence_holds_become_eight_beats(tmp_path: Path) -> None:
    lengths = [1.0, 0.5, 2.0, 0.5, 0.5, 3.0, 1.0, 1.0, 2.0, 0.75, 1.5]
    video = _video(tmp_path / "ref.mp4", lengths)
    shots = remake.analyze(video)["shots"]
    assert len(shots) == 11
    d = remake.draft(video, shots, tmp_path / "work")
    assert len(d["shots"]) == 8 and "11 shots were found" in d["notes"][0]
    assert d["shots"][0]["start"] == 0 and abs(d["shots"][-1]["end"] - sum(lengths)) < 0.05  # the whole video is covered
    assert all(abs(a["end"] - b["start"]) < 1e-6 for a, b in zip(d["shots"], d["shots"][1:]))
    assert len(remake.draft(video, shots, tmp_path / "three", most=3)["shots"]) == 3


def test_beats_split_what_is_too_long_and_join_the_shortest_neighbours() -> None:
    assert remake.beats([{"start": 0, "end": 150}], longest=60) == [
        {"start": 0, "end": 50}, {"start": 50, "end": 100}, {"start": 100, "end": 150}]
    joined = remake.beats([{"start": 0, "end": 5}, {"start": 5, "end": 6}, {"start": 6, "end": 7}, {"start": 7, "end": 12}], most=3)
    assert joined == [{"start": 0, "end": 5}, {"start": 5, "end": 7}, {"start": 7, "end": 12}]
    with pytest.raises(ValueError, match="does not fit"):
        remake.beats([{"start": i * 50.0, "end": (i + 1) * 50.0} for i in range(4)], most=3, longest=60)


def test_a_video_without_a_cut_or_too_long_is_refused(tmp_path: Path) -> None:
    one = _video(tmp_path / "one.mp4", [3.0])
    shots = remake.analyze(one)["shots"]
    assert len(shots) == 1
    with pytest.raises(ValueError, match="no cut was found"):
        remake.draft(one, shots, tmp_path / "work")
    with pytest.raises(ValueError, match="up to 3 minutes"):
        remake.analyze(_video(tmp_path / "long.mp4", [181.0]))
    (tmp_path / "junk.mp4").write_bytes(b"not a video")
    with pytest.raises(Exception):  # noqa: B017, PT011 — ffprobe's own error
        remake.analyze(tmp_path / "junk.mp4")


from test_sequence import _drain, seam  # noqa: E402, F401 — the fixture


def _post(c, video: Path, **params):
    with video.open("rb") as f:
        return c.post("/api/remakes", params=params, files={"file": (video.name, f, "video/mp4")})


def test_api_draft_makes_a_sequence_and_queues_nothing(seam) -> None:  # noqa: F811
    c, e, tmp = seam
    video = _video(tmp / "ref.mp4", [2.0, 1.5, 3.0])
    r = _post(c, video)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["found"] == 3 and [b["start"] for b in out["beats"]] == [0, 2.0, 3.5] and not out["footage"]
    assert [s["hold"] for s in out["draft"]["shots"]] == [2.0, 1.5, 3.0] and all(Path(p).is_file() for p in out["files"])
    assert _drain(e) == [] and e.store.list(limit=-1) == []  # analysing renders nothing
    folder = e.settings.data_dir / "remakes" / out["id"]
    assert [p.name for p in folder.iterdir()] == ["remake.json"]  # the reference itself is not kept
    assert c.get(f"/api/remakes/{out['id']}").json() == out
    assert c.get("/api/remakes/r00000000000").status_code == 404 and c.get("/api/remakes/nope").status_code == 404
    made = c.post("/api/sequences", json=out["draft"])
    assert made.status_code == 201, made.text
    assert made.json()["segments"] == [None, None] and len(made.json()["shots"]) == 3  # all cuts: no take

    clips = _post(c, video, footage=True, max_beats=2).json()
    assert len(clips["draft"]["shots"]) == 2 and all("clip" in s for s in clips["draft"]["shots"]) and clips["notes"]
    assert c.post("/api/sequences", json=clips["draft"]).status_code == 201


def test_api_refuses_what_is_not_a_usable_reference(seam) -> None:  # noqa: F811
    c, e, tmp = seam
    no_cut = _post(c, _video(tmp / "one.mp4", [2.0]))
    assert no_cut.status_code == 400 and "no cut was found" in no_cut.text
    assert _post(c, _video(tmp / "two.mp4", [1.0, 1.0]), max_beats=9).status_code == 400
    (tmp / "junk.mp4").write_bytes(b"not a video")
    assert _post(c, tmp / "junk.mp4").status_code == 400
    with (tmp / "two.mp4").open("rb") as f:
        assert c.post("/api/remakes", files={"file": ("x.png", f, "image/png")}).status_code == 400
    remakes = e.settings.data_dir / "remakes"
    assert not remakes.exists() or not any(remakes.iterdir())  # a refused reference leaves nothing behind


def test_mcp_create_sequence_passes_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from seam import mcp_server

    sent = {}

    async def upload(path, role):
        return {"id": f"{path}.png"} if path != "c" else {"id": "c.png", "clip": "c.mp4", "first": "f.png", "last": "l.png"}

    async def req(method, path, **kw):
        sent.update(kw["json"])
        return {}

    monkeypatch.setattr(mcp_server, "_upload", upload)
    monkeypatch.setattr(mcp_server, "_req", req)
    monkeypatch.setattr(mcp_server, "_seq_brief", lambda seq: seq)
    cuts = [{"preset": "cut"}] * 2
    asyncio.run(mcp_server.create_sequence(["a", "b", "c"], cuts, holds=[3.5, None, None]))
    assert sent["shots"] == [{"image": "a.png", "hold": 3.5}, {"image": "b.png"}, {"clip": "c.mp4", "first": "f.png", "last": "l.png"}]
    with pytest.raises(ValueError, match="one entry per shot"):
        asyncio.run(mcp_server.create_sequence(["a", "b"], cuts[:1], holds=[1.0]))
