from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seam import media, server
from seam.config import Settings
from seam.engine import Engine
from seam.jobs import JobStore

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def _clip(path: Path, seconds: float, size: str = "320x180", rate: int = 30, audio: bool = False) -> Path:
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=d={seconds}", "-shortest"]
    subprocess.run([*args, str(path)], check=True)
    return path


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    s = replace(Settings(), data_dir=tmp_path / "data")
    s.ensure_dirs()
    e = Engine(s, JobStore(s.db_path), client=object())  # worker never started: no GPU work
    monkeypatch.setattr(server, "engine", e)
    monkeypatch.setattr(server, "settings", s)
    return TestClient(server.app, base_url="http://127.0.0.1:7860"), e


def test_clip_upload_returns_the_frame_the_transition_needs(client, tmp_path: Path) -> None:
    c, e = client
    clip = _clip(tmp_path / "a.mp4", 2)
    with clip.open("rb") as f:
        a = c.post("/api/uploads?role=a", files={"file": ("a.mp4", f, "video/mp4")}).json()
    with clip.open("rb") as f:
        b = c.post("/api/uploads?role=b", files={"file": ("a.mp4", f, "video/mp4")}).json()
    assert a["clip"] == b["clip"] and a["clip_seconds"] == pytest.approx(2, abs=0.1)
    assert a["id"] != b["id"]  # last frame vs first frame of the same clip
    assert (a["width"], a["height"]) == (320, 180)
    job = c.post("/api/transitions", json={"image_a": a["id"], "image_b": b["id"], "preset": "smoke",
                                            "clip_a": a["clip"], "clip_b": b["clip"]}).json()
    assert job["params"]["clip_a"] == a["clip"]  # file name only, never a server path


def test_clip_length_is_checked(client, tmp_path: Path) -> None:
    c, _ = client
    with _clip(tmp_path / "long.mp4", 7).open("rb") as f:
        r = c.post("/api/uploads?role=a", files={"file": ("long.mp4", f, "video/mp4")})
    assert r.status_code == 400 and "1-6 s" in r.json()["detail"]
    assert c.post("/api/transitions", json={"image_a": "0" * 16 + ".png", "image_b": "0" * 16 + ".png",
                                            "preset": "smoke", "clip_a": "f" * 16 + ".mp4"}).status_code == 404


def test_merge_keeps_transition_size_and_adds_both_clips(tmp_path: Path) -> None:
    t = _clip(tmp_path / "t.mp4", 3, size="448x256", rate=24, audio=True)
    a = _clip(tmp_path / "a.mp4", 1.5, size="640x360", rate=30, audio=True)
    b = _clip(tmp_path / "b.mp4", 1, size="360x640", rate=25)  # portrait, silent
    out = media.probe(media.merge_clips(t, tmp_path / "m.mp4", a, b))
    assert (out.width, out.height, out.fps) == (448, 256, 24)
    assert out.duration == pytest.approx(5.5, abs=0.15) and out.has_audio
    assert not (tmp_path / "m.tmp.mp4").exists()


def test_clip_with_sound_longer_than_picture(tmp_path: Path) -> None:
    """Phone and editor exports often carry an audio tail: frames and joins follow the picture, not the container."""
    clip = tmp_path / "tail.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
                    "-f", "lavfi", "-i", "sine=d=3", str(clip)], check=True)
    assert media.probe(clip).duration == pytest.approx(2, abs=0.1)
    assert media.clip_frame(clip, tmp_path / "last.png", last=True).is_file()
    t = _clip(tmp_path / "t.mp4", 1, size="320x180", rate=24, audio=True)
    out = media.probe(media.merge_clips(t, tmp_path / "m.mp4", clip))
    assert out.duration == pytest.approx(3, abs=0.15)


@pytest.mark.parametrize("ext", ["mp4", "webm"])
def test_uploaded_clips_share_one_clock(tmp_path: Path, ext: str) -> None:
    """WebM often has no video-stream duration; late sound must keep its offset; a sound tail is cut to the picture."""
    from dataclasses import replace

    from seam.engine import store_clip

    src = tmp_path / f"in.{ext}"
    # the sound stream itself starts 0.5 s late (timestamp offset, not silent samples) and runs past the picture
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=2",
                    "-itsoffset", "0.5", "-f", "lavfi", "-i", "sine=f=440:d=2.5", "-map", "0:v", "-map", "1:a",
                    str(src)], check=True)
    s = replace(Settings(), data_dir=tmp_path / "data")
    clip, seconds = store_clip(s, src)
    assert seconds == pytest.approx(2, abs=0.1)
    head = subprocess.run(["ffmpeg", "-v", "info", "-t", "0.4", "-i", str(clip), "-map", "0:a", "-af", "volumedetect",
                           "-f", "null", "-"], capture_output=True, text=True).stderr
    late = subprocess.run(["ffmpeg", "-v", "info", "-ss", "0.8", "-t", "0.4", "-i", str(clip), "-map", "0:a", "-af",
                           "volumedetect", "-f", "null", "-"], capture_output=True, text=True).stderr
    vol = lambda log: float(log.split("mean_volume:")[1].split("dB")[0])  # noqa: E731
    assert vol(head) < -60 < vol(late)  # silence first, then the tone, as in the upload
    assert media.clip_frame(clip, tmp_path / "last.png", last=True).is_file()
    # and the offset survives the join: clip first, so the merged sound is still silent for its first 0.4 s
    merged = media.merge_clips(_clip(tmp_path / "t.mp4", 1, rate=24, audio=True), tmp_path / "m.mp4", clip)
    head = subprocess.run(["ffmpeg", "-v", "info", "-t", "0.4", "-i", str(merged), "-map", "0:a", "-af", "volumedetect",
                           "-f", "null", "-"], capture_output=True, text=True).stderr
    assert vol(head) < -60 and media.probe(merged).duration == pytest.approx(3, abs=0.15)
