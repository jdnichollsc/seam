"""A reference video in, a sequence draft out: where its cuts are, one keyframe per shot, and the shots and hand-offs
that re-create its structure in Seam. ffmpeg only: nothing renders and nothing leaves this machine."""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import Any

from . import media

MAX_SECONDS = 180.0
MAX_BYTES = 200 * 1024 * 1024
MAX_BEATS = 8  # a sequence holds 8 shots
HOLD = (0.5, 10.0)  # an image next to a cut (engine.HOLD_SECONDS)
CLIP = media.LONG_CLIP_SECONDS  # a clip between cuts
SCENE = 0.3  # ffmpeg's scene score a cut exceeds
SHORTEST = 0.2  # seconds: nothing shorter counts as a shot
_PTS = re.compile(r"pts_time:([0-9.]+)")


def cut_times(video: Path, threshold: float = SCENE) -> list[float]:
    """The time each new shot starts at (the first excluded): frames whose picture differs sharply from the one
    before. A dissolve or a fast pan may be missed or over-split; the draft is for the user to correct."""
    media._ensure_ffmpeg()
    run = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-map", "0:v:0", "-an",
                          "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if run.returncode:
        raise ValueError("not a readable video")
    return sorted({float(t) for t in _PTS.findall(run.stderr) if float(t) > 0})


def analyze(video: Path) -> dict[str, Any]:
    """`{source, shots}`: the video's size and length, and its shots in order as `{start, end}` seconds."""
    info = media.probe(video)
    if info.duration > MAX_SECONDS + 0.05:
        raise ValueError(f"a reference video is up to {MAX_SECONDS / 60:g} minutes (this one is {info.duration:.0f} s)")
    edges = [0.0]
    for t in cut_times(video):  # a "shot" of a frame or two is a flash or a detection echo: it stays with the next
        if t - edges[-1] >= SHORTEST and info.duration - t >= SHORTEST:
            edges.append(t)
    edges.append(info.duration)
    return {"source": {"duration": round(info.duration, 3), "fps": round(info.fps, 3), "width": info.width,
                       "height": info.height, "has_audio": info.has_audio},
            "shots": [{"start": round(a, 3), "end": round(b, 3)} for a, b in zip(edges, edges[1:])]}


def beats(shots: list[dict[str, float]], most: int = MAX_BEATS, longest: float | None = None) -> list[dict[str, float]]:
    """The shots as at most `most` beats, the whole video still covered: a shot over `longest` is split evenly, then
    the two neighbours that are shortest together are joined until they fit."""
    out: list[dict[str, float]] = []
    for s in shots:
        n = max(1, math.ceil((s["end"] - s["start"]) / longest - 1e-9)) if longest else 1
        step = (s["end"] - s["start"]) / n
        out += [{"start": s["start"] + i * step, "end": s["start"] + (i + 1) * step} for i in range(n)]
    while len(out) > most:
        spans = [(out[i + 1]["end"] - out[i]["start"], i) for i in range(len(out) - 1)]
        fits = [x for x in spans if longest is None or x[0] <= longest + 1e-6]
        if not fits:
            raise ValueError(f"this video does not fit in {most} shots of up to {longest:g} s")
        _, i = min(fits)
        out[i:i + 2] = [{"start": out[i]["start"], "end": out[i + 1]["end"]}]
    return [{"start": round(b["start"], 3), "end": round(b["end"], 3)} for b in out]


def _frame(video: Path, t: float, dst: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-map", "0:v:0",
                    "-frames:v", "1", str(dst)], check=True, capture_output=True, timeout=60)
    return dst


def _cut(video: Path, start: float, end: float, dst: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-i", str(video), "-t", f"{end - start:.3f}",
                    "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(dst)], check=True, capture_output=True, timeout=300)
    return dst


def draft(video: Path, shots: list[dict[str, float]], work: Path, footage: bool = False,
          most: int = MAX_BEATS) -> dict[str, Any]:
    """The sequence Seam proposes, as files in `work`: one shot per beat and a cut at every hand-off (the only
    hand-off this analysis can tell). Re-create (default): each beat is its first frame, shown for the beat's
    length; `footage`: each beat is that part of the video itself. `notes` says where the engine's limits changed
    something."""
    if not 2 <= most <= MAX_BEATS:
        raise ValueError(f"a sequence has 2-{MAX_BEATS} shots")
    found = beats(shots, most, CLIP[1] if footage else None)
    if len(found) < 2:
        raise ValueError("no cut was found in this video: a sequence needs at least two shots")
    work.mkdir(parents=True, exist_ok=True)
    out, notes = [], []
    if len(shots) > len(found):
        notes.append(f"{len(shots)} shots were found; neighbours were joined into {len(found)} beats")
    for i, b in enumerate(found):
        seconds = b["end"] - b["start"]
        beat = {"start": b["start"], "end": b["end"]}
        if footage and seconds >= CLIP[0]:
            out.append({**beat, "clip": str(_cut(video, b["start"], b["end"], work / f"beat{i + 1}.mp4"))})
            continue
        hold = min(max(seconds, HOLD[0]), HOLD[1])
        if footage:
            notes.append(f"shot {i + 1} is under {CLIP[0]:g} s: it is an image, shown for {hold:g} s")
        elif abs(hold - seconds) > 0.01:
            notes.append(f"shot {i + 1} lasts {seconds:.1f} s in the video; an image shows for {hold:g} s")
        out.append({**beat, "image": str(_frame(video, b["start"], work / f"beat{i + 1}.png")), "hold": round(hold, 2)})
    return {"shots": out, "segments": [{"preset": "cut"} for _ in out[1:]], "story": "", "notes": notes}
