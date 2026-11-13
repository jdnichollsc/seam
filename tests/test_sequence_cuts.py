"""Cut hand-offs: your own clips and images in any order, nothing generated between them (ffmpeg stands in for the GPU)."""
from __future__ import annotations

import pytest

from seam import media
from seam.engine import SEQ

from test_sequence import _drain, _image, _render, _upload, _video, seam  # noqa: F401 — the fixture


def _clip(c, tmp, name: str, seconds: float) -> dict:
    return _upload(c, _video(tmp / f"{name}.mp4", seconds, "640x360", rate=30, audio=True), role="mid")


def _shot(u: dict) -> dict:
    return {"clip": u["clip"], "first": u["first"], "last": u["last"]} if u.get("clip") else {"image": u["id"]}


def test_an_all_cuts_montage_joins_at_once_with_long_clips_and_holds(seam) -> None:
    c, e, tmp = seam
    long_clip = _clip(c, tmp, "long", 8.0)  # longer than a transition's neighbour may be
    image = _upload(c, _image(tmp / "still.png", (640, 360), "red"))
    short = _clip(c, tmp, "short", 2.0)
    r = c.post("/api/sequences", json={"shots": [_shot(long_clip), {**_shot(image), "hold": 3.0}, _shot(short)],
                                       "segments": [{"preset": "cut"}, {"preset": "cut"}]})
    assert r.status_code == 201, r.text
    seq = r.json()
    assert seq["segments"] == [None, None] and seq["takes"] == [None, None] and seq["shots"][1]["hold"] == 3.0
    assert _drain(e) == [SEQ + seq["id"]]  # nothing for the GPU
    e._reconcile(seq["id"])
    done = e.store.get_sequence(seq["id"])
    assert done.status == "succeeded", done.error
    info = media.probe(e.settings.outputs_dir / done.output)
    assert info.duration == pytest.approx(8.0 + 3.0 + 2.0, abs=0.15) and info.fps == pytest.approx(30, abs=0.1)
    parts = c.get(f"/api/sequences/{seq['id']}/receipt").json()["parts"]
    assert [p.get("cut") for p in parts if "segment" in p] == [True, True]
    assert any(p.get("seconds") == 3.0 for p in parts)


def test_a_clip_next_to_a_transition_stays_short(seam) -> None:
    c, e, tmp = seam
    long_clip = _clip(c, tmp, "long", 8.0)
    image = _upload(c, _image(tmp / "still.png", (640, 360), "red"))
    r = c.post("/api/sequences", json={"shots": [_shot(long_clip), _shot(image)], "segments": [{"preset": "smoke"}]})
    assert r.status_code == 422 and "make its hand-offs cuts" in r.json()["detail"]
    assert _drain(e) == []
    # a standalone two-shot take never accepts it either: not as an upload, not as a clip already stored
    with (tmp / "long.mp4").open("rb") as f:
        assert c.post("/api/uploads?role=a", files={"file": ("long.mp4", f, "video/mp4")}).status_code == 400
    r = c.post("/api/transitions", json={"image_a": long_clip["last"], "image_b": image["id"],
                                         "clip_a": long_clip["clip"], "preset": "smoke"})
    assert r.status_code == 422 and "next to a transition" in r.json()["detail"]


def test_a_cut_cannot_be_rerolled_and_mixes_with_takes(seam) -> None:
    c, e, tmp = seam
    ids = [_upload(c, _image(tmp / f"s{i}.png", (640, 360), col))["id"] for i, col in enumerate(("red", "green", "blue"))]
    seq = c.post("/api/sequences", json={"shots": [{"image": i} for i in ids],
                                         "segments": [{"preset": "smoke", "seed": 1}, {"preset": "cut"}]}).json()
    take = seq["segments"][0]
    assert seq["segments"][1] is None and _drain(e) == [take]
    _render(e, take)
    e._reconcile(seq["id"])
    done = e.store.get_sequence(seq["id"])
    assert done.status == "succeeded", done.error
    # image 1 shows through the transition only; image 2 lands, then holds 2 s before the cut; image 3 holds 2 s
    assert media.probe(e.settings.outputs_dir / done.output).duration == pytest.approx(1.0 + 2.0 + 2.0, abs=0.15)
    r = c.post(f"/api/sequences/{seq['id']}/segments/2/refine", json={})
    assert r.status_code == 422 and "is a cut" in r.json()["detail"]
    # growing after a cut, with a cut, renders nothing and joins again
    more = _upload(c, _image(tmp / "s3.png", (640, 360), "white"))["id"]
    r = c.post(f"/api/sequences/{seq['id']}/shots", json={"shot": {"image": more}, "segment": {"preset": "cut"}})
    assert r.status_code == 201, r.text
    assert r.json()["segments"] == [take, None, None] and _drain(e) == [SEQ + seq["id"]]


def test_edit_turns_cuts_into_effects_and_new_hand_offs_default_to_cuts(seam) -> None:
    c, e, tmp = seam
    ids = [_upload(c, _image(tmp / f"s{i}.png", (640, 360), col))["id"] for i, col in enumerate(("red", "green", "blue"))]
    seq = c.post("/api/sequences", json={"shots": [{"image": i} for i in ids],
                                         "segments": [{"preset": "cut"}, {"preset": "cut"}]}).json()
    _drain(e)
    e._reconcile(seq["id"])
    first = e.store.get_sequence(seq["id"]).output
    url = f"/api/sequences/{seq['id']}/edit"
    r = c.post(url, json={"order": [1, 3, 2]})  # every new hand-off is a cut: nothing renders, a new file joins
    assert r.status_code == 200, r.text
    assert r.json()["segments"] == [None, None] and _drain(e) == [SEQ + seq["id"]]
    e._reconcile(seq["id"])
    assert e.store.get_sequence(seq["id"]).output != first  # an all-cuts reorder is a new video, never the old one
    seq = e.store.get_sequence(seq["id"])
    r = c.post(url, json={"order": [1, 2, 3], "transitions": {"2": {"preset": "ink"}},
                          "expected_updated_at": seq.updated_at})
    assert r.status_code == 200, r.text
    segs = r.json()["segments"]
    assert segs[0] is None and segs[1] and _drain(e) == [segs[1]]


def test_cancel_and_rough_cut_with_cuts(seam) -> None:
    c, _e, tmp = seam
    ids = [_upload(c, _image(tmp / f"s{i}.png", (640, 360), col))["id"] for i, col in enumerate(("red", "green", "blue"))]
    body = {"shots": [{"image": i} for i in ids], "segments": [{"preset": "smoke"}, {"preset": "cut"}]}
    rough = c.post("/api/rough-cut", json=body)
    assert rough.status_code == 200, rough.text
    # shot 1 holds 1.5 s, the smoke card 0.8 s, shot 2 holds 2 s (a cut follows), no card for the cut, shot 3 2 s
    assert rough.json()["seconds"] == pytest.approx(1.5 + 0.8 + 2.0 + 2.0, abs=0.15)
    seq = c.post("/api/sequences", json=body).json()
    assert c.post(f"/api/sequences/{seq['id']}/cancel").json()["status"] == "cancelled"


def test_a_finished_take_continues_with_a_cut(seam) -> None:
    c, e, tmp = seam
    a = _upload(c, _image(tmp / "a.png", (640, 360), "red"))
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    nxt = _clip(c, tmp, "n", 12)  # longer than 6 s: fine after a cut
    job = c.post("/api/transitions", json={"image_a": a["id"], "image_b": b["id"], "preset": "smoke"}).json()
    _drain(e)
    out = _video(e.settings.outputs_dir / f"{job['id']}.mp4", 1, "640x352", audio=True)
    e.store.update(job["id"], status="succeeded", output=out.name)
    r = c.post(f"/api/jobs/{job['id']}/continue", json={"shot": _shot(nxt), "segment": {"preset": "cut"}})
    assert r.status_code == 201, r.text
    assert r.json()["segments"] == [job["id"], None] and len(r.json()["shots"]) == 3


def test_mcp_progress_counts_a_cut_as_done() -> None:
    from seam.mcp_server import _segment_progress

    assert _segment_progress(None, None) == 1.0  # a cut: nothing to wait for
    assert _segment_progress("t1", None) == 0.0
    assert _segment_progress("t1", {"status": "running", "progress": 0.4}) == 0.4
    assert _segment_progress("t1", {"status": "succeeded", "progress": 0.4}) == 1.0
