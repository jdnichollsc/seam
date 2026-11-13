from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from seam import backend as backends, media, server
from seam.config import Settings
from seam.engine import Engine
from seam.jobs import JobStore


@pytest.fixture
def seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = replace(Settings(), data_dir=tmp_path / "data")
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    monkeypatch.setattr(backends, "current", lambda _s: (backends.LOCAL, "test"))
    images = [tmp_path / f"{i}.png" for i in range(3)]
    for i, path in enumerate(images):
        Image.new("RGB", (640, 360), (i * 80, 40, 90)).save(path)
    return e, [{"first": str(p), "last": str(p), "clip": None} for p in images]


def test_durations_add_up_and_no_job_is_made(seam) -> None:
    e, shots = seam
    out = e.rough_cut(shots, [{"preset": "smoke", "seconds": 5}, {"preset": "", "seconds": 10}])
    assert media.probe(out).duration == pytest.approx(3 * 1.5 + 2 * 0.8, abs=0.1)
    assert e.store.list() == [] and e.store.list_sequences() == []
    assert not list(out.parent.glob("tmp*"))


def test_a_clip_plays_in_full(seam, tmp_path: Path) -> None:
    e, shots = seam
    clip = tmp_path / "clip.mp4"
    media.still(Path(shots[0]["first"]), clip, 2.0)
    shots = [{**shots[0], "clip": str(clip)}, shots[1]]
    out = e.rough_cut(shots, [{"preset": "smoke", "seconds": 5}])
    assert media.probe(out).duration == pytest.approx(2.0 + 0.8 + 1.5, abs=0.1)


def test_the_same_request_is_the_same_file_and_old_ones_leave(seam) -> None:
    import os
    e, shots = seam
    segments = [{"preset": "smoke", "seconds": 5}, {"preset": "whip_pan", "seconds": 5}]
    first = e.rough_cut(shots, segments)
    assert e.rough_cut(shots, segments) == first
    os.utime(first, (1, 1))
    second = e.rough_cut(shots, [{"preset": "smoke", "seconds": 5}, {"preset": "smoke", "seconds": 5}])
    assert second != first and not first.exists()


@pytest.mark.parametrize("n, segments", [
    (1, []),
    (3, [{"preset": "smoke"}]),
    (3, [{"preset": "smoke"}, {"preset": "no_such_effect"}]),
])
def test_a_bad_request_is_refused(seam, n: int, segments: list) -> None:
    e, shots = seam
    with pytest.raises(ValueError):
        e.rough_cut(shots[:n], segments)
