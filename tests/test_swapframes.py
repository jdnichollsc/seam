"""Swap frames and timing: constant rate, what is sent, putting the result back, and the frame-off check."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest

from seam import media, swap
from seam import swapframes as sf


def _ffmpeg(*args: str) -> None:
    try:
        media._ensure_ffmpeg()
    except RuntimeError:
        pytest.skip("ffmpeg not installed")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


# -- constant rate -------------------------------------------------------------------------------
def test_constant_rate_from_times():
    c = sf.Clock(times=[i / 30 for i in range(60)], tick=1 / 15360, rate=30.0, average=30.0)
    assert sf.constant_rate(c) is True
    jumpy = sf.Clock(times=[i / 30 + (0.01 if i > 20 else 0) for i in range(60)], tick=1 / 15360, rate=30.0, average=30.0)
    assert sf.constant_rate(jumpy) is False
    assert sf.constant_rate(sf.Clock(times=[0.0, None, 2 / 30], tick=1 / 90000, rate=30.0, average=30.0)) is None
    assert sf.constant_rate(sf.Clock(times=[i / 30 for i in range(60)], tick=1e-5, rate=30.0, average=27.5)) is False


def test_a_cfr_file_with_b_frames_is_constant(tmp_path: Path):
    clip = tmp_path / "cfr.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=2:size=160x96:rate=30", "-c:v", "libx264", "-bf", "3",
            "-pix_fmt", "yuv420p", str(clip))
    assert sf.constant_rate(sf.clock(clip)) is True


def test_a_vfr_file_is_not(tmp_path: Path):
    clip = tmp_path / "vfr.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=2:size=160x96:rate=30",
            "-vf", "select='not(between(n,20,25))'", "-fps_mode", "passthrough", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(clip))
    assert sf.constant_rate(sf.clock(clip)) is False


# -- what is sent --------------------------------------------------------------------------------
def test_native_24_is_frame_for_frame():
    p = sf.plan(23.976, np.full(48, 5.0))
    assert p.mode == "native" and p.keep == list(range(48))


def test_24_in_30_sends_only_the_distinct_frames():
    d = np.full(60, 6.0)
    d[3::5] = 0.1  # one in five repeats the one before
    p = sf.plan(30.0, d)
    assert p.mode == "distinct" and len(p.keep) == 48 and 3 not in p.keep and 0 in p.keep


def test_true_30_and_still_shots_are_resampled():
    assert sf.plan(30.0, np.full(60, 6.0)).mode == "resampled"
    still = sf.plan(30.0, np.full(60, 0.3))
    assert still.mode == "resampled" and "could not be measured" in still.note
    assert sf.plan(25.0, np.full(50, 6.0)).mode == "resampled"


def test_24_in_30_is_found_on_a_real_clip(tmp_path: Path):
    clip = tmp_path / "pulldown.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=duration=2:size=160x96:rate=24", "-vf", "fps=30", "-c:v", "libx264",
            "-crf", "10", "-pix_fmt", "yuv420p", str(clip))
    frames = sf.gray(clip, 0, 60)
    p = sf.plan(30.0, sf.frame_diffs(frames))
    assert p.mode == "distinct" and len(p.keep) == 48


# -- putting it back -----------------------------------------------------------------------------
def test_order_puts_each_generated_frame_where_its_source_was():
    p = sf.Plan("distinct", [0, 1, 2, 4, 5, 6, 7, 9])  # frames 3 and 8 repeat
    frames, note = sf.order(p, 10, 8)
    assert frames == [0, 1, 2, 2, 3, 4, 5, 6, 6, 7] and note == ""
    short, note = sf.order(p, 10, 7)
    assert short[-1] == 6 and "one frame short" in note
    with pytest.raises(ValueError, match="run it again"):
        sf.order(p, 10, 6)
    with pytest.raises(ValueError):
        sf.order(p, 10, 9)
    assert sf.order(sf.Plan("resampled", None), 30, 24)[0][-1] == 23


def test_place_keeps_size_rate_frames_and_sound(tmp_path: Path):
    src = tmp_path / "shot.mp4"
    _ffmpeg("-f", "lavfi", "-i", "testsrc=duration=1:size=180x320:rate=30", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src))
    gen = tmp_path / "gen.mp4"
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=96x176:rate=24:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(gen))
    area = swap.seen_area(180, 320, 96, 176)
    frames, _ = sf.order(sf.Plan("resampled", None), 30, 24)
    out = sf.place(gen, src, frames, area, tmp_path / "placed.mp4", tmp_path / "work")
    info = media.probe(out)
    assert (info.width, info.height) == (180, 320) and abs(info.fps - 30) < 0.01 and info.has_audio
    assert len(sf.clock(out).times) == 30


# -- frame-off check -----------------------------------------------------------------------------
def _moving(n: int = 30) -> np.ndarray:
    rng = np.random.default_rng(1)
    return rng.uniform(0, 255, (n, 36, 64)).astype(np.float32)


def test_a_result_in_step_passes_and_one_frame_off_is_caught():
    src = _moving()
    outside = np.ones_like(src, bool)
    outside[:, 10:26, 24:40] = False  # the person
    result = src + np.random.default_rng(2).normal(0, 3, src.shape).astype(np.float32)  # re-encoded, same timing
    assert sf.alignment(src, result, outside) == "ok"
    late = np.concatenate([result[:1], result[:-1]])
    assert sf.alignment(src, late, outside) == "shifted"


def test_a_still_background_gives_no_verdict():
    src = np.repeat(_moving(1), 30, axis=0)
    assert sf.alignment(src, src.copy(), np.ones_like(src, bool)) is None
