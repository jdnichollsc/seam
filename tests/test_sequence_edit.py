"""Editing a finished sequence: remove / reorder shots, a story line for the whole film (ffmpeg stands in for the GPU)."""
from __future__ import annotations

from pathlib import Path

import pytest

from seam import media
from seam.engine import SEQ, TransitionRequest

from test_sequence import _drain, _image, _render, _upload, _video, seam  # noqa: F401 — the fixture


def _four(c, e, tmp: Path, story: str = "") -> dict:
    """A finished sequence of four still shots (distinct colours), one take per hand-off."""
    ids = [_upload(c, _image(tmp / f"s{i}.png", (640, 360), col))["id"]
           for i, col in enumerate(("red", "green", "blue", "white"))]
    seq = c.post("/api/sequences", json={"shots": [{"image": i} for i in ids], "story": story,
                                         "segments": [{"preset": "smoke", "seed": k} for k in range(3)]}).json()
    _drain(e)
    for j in seq["segments"]:
        _render(e, j)
    e._reconcile(seq["id"])
    done = c.get(f"/api/sequences/{seq['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    return done


def test_reorder_renders_only_new_hand_offs_and_keeps_the_canvas(seam) -> None:
    c, e, tmp = seam
    seq = _four(c, e, tmp)
    url = f"/api/sequences/{seq['id']}/edit"
    r = c.post(url, json={"order": [1, 3, 2, 4]})  # 1→3, 3→2 and 2→4 are all new
    assert r.status_code == 422 and all(p in r.json()["detail"] for p in ("1 → 3", "3 → 2", "2 → 4"))
    assert _drain(e) == [] and e.store.get_sequence(seq["id"]).segments == seq["segments"]

    r = c.post(url, json={"order": [1, 3, 2, 4], "transitions": {str(k): {"preset": "ink"} for k in (1, 2, 3)}})
    assert r.status_code == 200, r.text
    out = r.json()
    new = [e.store.get(j) for j in out["segments"]]
    assert out["status"] == "running" and not set(out["segments"]) & set(seq["segments"])
    assert _drain(e) == out["segments"]
    shots = seq["shots"]
    assert [s["first_url"] for s in out["shots"]] == [shots[i]["first_url"] for i in (0, 2, 1, 3)]
    assert all((j.params["width"], j.params["height"]) == (seq["width"], seq["height"]) for j in new)
    assert Path(new[1].params["image_a"]).name == shots[2]["first_url"].rsplit("/", 1)[1]  # leaves shot 3 for shot 2


def test_remove_a_middle_shot_reuses_the_first_take(seam) -> None:
    c, e, tmp = seam
    seq = _four(c, e, tmp)
    r = c.post(f"/api/sequences/{seq['id']}/edit", json={"order": [1, 2, 4], "transitions": {"2": {"preset": "melt"}}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["segments"][0] == seq["segments"][0] and out["segments"][1] not in seq["segments"]
    assert e.store.get(out["segments"][1]).params["preset"] == "melt" and _drain(e) == [out["segments"][1]]


@pytest.mark.parametrize("order", [[1, 2, 3], [2, 3, 4]])
def test_remove_an_end_shot_rejoins_without_rendering(seam, order) -> None:
    c, e, tmp = seam
    seq = _four(c, e, tmp)
    out = c.post(f"/api/sequences/{seq['id']}/edit", json={"order": order}).json()
    kept = seq["segments"][:2] if order[0] == 1 else seq["segments"][1:]
    assert out["segments"] == kept and out["status"] == "running"
    assert _drain(e) == [SEQ + seq["id"]]  # no take renders; the join runs again
    e._reconcile(seq["id"])
    done = e.store.get_sequence(seq["id"])
    assert done.status == "succeeded" and done.output != seq["output_url"].rsplit("/", 1)[1]
    assert media.probe(e.settings.outputs_dir / done.output).duration == pytest.approx(2, abs=0.15)


def test_story_is_saved_without_rendering_and_used_by_later_takes(seam) -> None:
    c, e, tmp = seam
    seq = _four(c, e, tmp, story="a summer road trip")
    assert all(e.store.get(j).params["story"] == "a summer road trip" for j in seq["segments"])
    url = f"/api/sequences/{seq['id']}/edit"
    same = c.post(url, json={"order": [1, 2, 3, 4]}).json()  # nothing changes: nothing renders, nothing joins
    assert same["segments"] == seq["segments"] and same["status"] == "succeeded" and _drain(e) == []

    out = c.post(url, json={"order": [1, 2, 3, 4], "story": "  a winter night in the city  "}).json()
    assert out["story"] == "a winter night in the city" and out["status"] == "succeeded" and _drain(e) == []
    assert e.store.get(seq["segments"][0]).params["story"] == "a summer road trip"  # older takes keep theirs

    again = c.post(f"/api/sequences/{seq['id']}/segments/2/refine", json={"prompt_extra": "slow"}).json()
    take = e.store.get(again["segments"][1])
    assert take.params["story"] == "a winter night in the city" and take.params["prompt_extra"] == "slow"
    prompt = e.prompt_for(TransitionRequest(**take.params))
    assert "Story of the whole film: a winter night in the city." in prompt and prompt.index("Story") < prompt.index("slow")
    assert "Story" not in e.prompt_for(TransitionRequest(**{**take.params, "story": ""}))


def test_edit_guards(seam) -> None:
    c, e, tmp = seam
    seq = _four(c, e, tmp)
    url = f"/api/sequences/{seq['id']}/edit"
    for order in ([1], [1, 1, 2], [1, 2, 9], [0, 1]):
        assert c.post(url, json={"order": order}).status_code == 422, order
    r = c.post(url, json={"order": [1, 2, 3], "transitions": {"5": {"preset": "ink"}}})
    assert r.status_code == 422 and "no transition 5" in r.json()["detail"]
    r = c.post(url, json={"order": [1, 3], "transitions": {"1": {"preset": "ink", "seconds": 10}}})
    assert r.status_code == 422 and "transition 1" in r.json()["detail"]  # ink is qualified at 5 s only
    stale = c.post(url, json={"order": [1, 2, 3], "expected_updated_at": seq["updated_at"] - 5})
    assert stale.status_code == 409
    assert e.store.get_sequence(seq["id"]).segments == seq["segments"] and _drain(e) == []

    c.post(url, json={"order": [1, 2, 4], "transitions": {"2": {"preset": "ink"}},
                      "expected_updated_at": seq["updated_at"]})
    assert c.post(url, json={"order": [1, 2]}).status_code == 409  # rendering: edits wait


def test_continued_clip_a_keeps_its_real_first_frame(seam) -> None:
    c, e, tmp = seam
    clip = _upload(c, _video(tmp / "a.mp4", 2, "640x360"), role="a")
    b = _upload(c, _image(tmp / "b.png", (640, 360), "blue"))
    nxt = _upload(c, _image(tmp / "n.png", (640, 360), "green"), role="mid")
    job = c.post("/api/transitions", json={"image_a": clip["id"], "clip_a": clip["clip"], "image_b": b["id"],
                                           "preset": "smoke"}).json()
    _drain(e)
    out = _video(e.settings.outputs_dir / f"{job['id']}.mp4", 1, "640x352", audio=True)
    e.store.update(job["id"], status="succeeded", output=out.name)
    seq = c.post(f"/api/jobs/{job['id']}/continue",
                 json={"shot": {"image": nxt["id"]}, "segment": {"preset": "smoke"}, "story": "one day"}).json()
    s0 = e.store.get_sequence(seq["id"]).shots[0]
    assert s0["clip"] and s0["first"] != s0["last"] and seq["story"] == "one day"
    assert Path(s0["last"]).name == clip["id"]  # the take left from clip A's last frame, as before
    assert e.store.get(seq["segments"][1]).params["story"] == "one day"
