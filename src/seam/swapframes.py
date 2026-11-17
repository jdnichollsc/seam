"""Frames and timing for a swap: one generated frame per source frame, in place.

The swap model works at 24 fps. A source has to be constant-rate (frame numbers and times must agree); 24 fps
material carried at 30 fps is sent as its distinct frames only, native 24 frame for frame, anything else as it is
(the model resamples it). The result is put back at the source's size, rate and frame count, and, where frames map
one for one, checked for coming back a frame off.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import media
from .swap import FPS as MODEL_FPS

SMALL = (64, 36)  # the size frames are compared at, as remake's cut finder does (portrait sources: swapped)
REPEAT = 0.6  # mean gray change (0-255) under which a frame repeats the one before
MOVING = 2.0  # mean change above which a frame is moving
MOTION = 1.5  # outside the person: the change a frame needs, both ways, to give timing evidence
MIN_INFORMATIVE = 12
SHIFT_SHARE = 0.7  # of informative frames on which a neighbour wins ...
SHIFT_MARGIN = 0.2  # ... by at least this much, for a result to count as shifted


# -- constant rate -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Clock:
    times: list[float | None]  # presentation time of each decoded frame, in display order
    tick: float  # one unit of the stream's time base
    rate: float  # r_frame_rate
    average: float  # avg_frame_rate


def _rate(s: str) -> float:
    num, _, den = s.partition("/")
    return float(num) / float(den or 1) if float(den or 1) else 0.0


def clock(video: Path) -> Clock:
    """The video stream's frame times as decoded (display order: packet times come in decode order with B-frames)."""
    media._ensure_ffmpeg()
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=time_base,r_frame_rate,avg_frame_rate:frame=best_effort_timestamp_time",
                          "-of", "json", str(video)], capture_output=True, text=True, check=True, timeout=300).stdout
    d = json.loads(out)
    s = d["streams"][0]
    times = [float(f["best_effort_timestamp_time"]) if f.get("best_effort_timestamp_time") not in (None, "N/A")
             else None for f in d.get("frames", [])]
    return Clock(times=times, tick=_rate(s["time_base"]), rate=_rate(s["r_frame_rate"]), average=_rate(s["avg_frame_rate"]))


def constant_rate(c: Clock) -> bool | None:
    """True when every step between frames is one frame at the stated rate (within a tick of the time base, or 1 ms
    if that is coarser), False when one is not or the average rate differs, None when frames have no times."""
    if not c.rate or len(c.times) < 2:
        return None
    if c.average and abs(c.average - c.rate) > 0.01 * c.rate:
        return False
    if any(t is None for t in c.times):
        return None
    step, slack = 1 / c.rate, max(c.tick, 0.001)
    return all(abs((b - a) - step) <= slack for a, b in zip(c.times, c.times[1:]))  # type: ignore[operator]


VFR_HINT = ("this video has a variable frame rate; convert it to a constant rate first, for example: "
            "ffmpeg -i in.mp4 -fps_mode cfr -r 30 -c:a copy out.mp4")


# -- what is sent --------------------------------------------------------------------------------
def gray(video: Path, first: int, count: int, size: tuple[int, int] = SMALL, vf: str = "") -> np.ndarray:
    """Frames `first` .. `first + count - 1` of the video (by number), small and gray, as `(count, h, w)` floats."""
    w, h = size
    chain = f"trim=start_frame={first}:end_frame={first + count},{vf + ',' if vf else ''}scale={w}:{h},format=gray"
    run = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-map", "0:v:0", "-vf", chain,
                          "-f", "rawvideo", "-"], capture_output=True, check=True, timeout=600)
    n = len(run.stdout) // (w * h)
    return np.frombuffer(run.stdout[: n * w * h], np.uint8).reshape(n, h, w).astype(np.float32)


@dataclass(frozen=True)
class Plan:
    mode: str  # "native" (24 fps, frame for frame), "distinct" (24-in-30, repeats dropped), "resampled"
    keep: list[int] | None  # source frames sent, in order (None: all, resampled by the model)
    note: str = ""


def plan(fps: float, diffs: np.ndarray) -> Plan:
    """What to send for a shot. `diffs[i]` is the mean change of frame i from frame i-1 (diffs[0] unused). 24-in-30
    material repeats one frame in five, in a steady position; when the shot barely moves that cannot be told."""
    n = len(diffs)
    if abs(fps - MODEL_FPS) < 0.05:
        return Plan("native", list(range(n)))
    if abs(fps - 30) < 0.1 and n >= 10:
        repeat = diffs < REPEAT
        repeat[0] = False
        moving = int((diffs[1:] > MOVING).sum())
        if moving < 0.5 * (n - 1):
            return Plan("resampled", None, "the shot barely moves, so its frame rhythm could not be measured")
        phase = np.bincount(np.flatnonzero(repeat) % 5, minlength=5)
        if phase.max() >= 0.8 * repeat.sum() and abs(repeat.sum() - n / 5) <= max(2, 0.1 * n / 5):
            return Plan("distinct", [i for i in range(n) if not repeat[i]])
    return Plan("resampled", None)


def frame_diffs(frames: np.ndarray) -> np.ndarray:
    d = np.zeros(len(frames), np.float32)
    d[1:] = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    return d


# -- putting it back -----------------------------------------------------------------------------
def order(p: Plan, source_frames: int, generated: int) -> tuple[list[int], str]:
    """For each source frame, the generated frame shown there. Frame for frame: a run one short holds its last
    frame (said in the note); any other count is refused. Resampled: spread evenly."""
    if p.keep is None:
        if generated < 1:
            raise ValueError("the swap came back with no frames")
        return [min(generated - 1, round(j * generated / source_frames)) for j in range(source_frames)], ""
    sent = len(p.keep)
    if generated not in (sent, sent - 1):
        raise ValueError(f"the swap came back with {generated} frames for the {sent} sent; run it again")
    note = "the swap came back one frame short; its last frame is held" if generated == sent - 1 else ""
    out, k = [], -1
    kept = set(p.keep)
    for j in range(source_frames):
        if j in kept:
            k += 1
        out.append(min(max(k, 0), generated - 1))
    return out, note


def place(generated: Path, source_clip: Path, frames: list[int], area: tuple[int, int, int, int], dst: Path,
          work: Path) -> Path:
    """The swapped shot at the source's size and rate: the source clip underneath (it shows where the centre crop cut
    the model's view), generated frame `frames[j]` scaled into `area` on top of source frame j, and the source's
    audio. Exactly len(frames) frames."""
    info = media.probe(source_clip)
    work.mkdir(parents=True, exist_ok=True)
    gen_dir, seq = work / "generated", work / "ordered"
    for d in (gen_dir, seq):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(generated), "-map", "0:v:0", "-fps_mode", "passthrough",
                    str(gen_dir / "%05d.png")], check=True, capture_output=True, timeout=600)
    pngs = sorted(gen_dir.glob("*.png"))
    for j, i in enumerate(frames):
        shutil.copyfile(pngs[i], seq / f"{j:05d}.png")
    x, y, w, h = area
    audio = ["-map", "0:a:0", "-c:a", "aac"] if info.has_audio else ["-an"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source_clip), "-framerate", f"{info.fps:.6f}",
                    "-i", str(seq / "%05d.png"), "-filter_complex",
                    f"[1:v]scale={w}:{h}:flags=lanczos[g];[0:v][g]overlay={x}:{y}:eof_action=pass,setsar=1[v]",
                    "-map", "[v]", *audio, "-frames:v", str(len(frames)), "-c:v", "libx264", "-crf", "16",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", str(dst)], check=True, capture_output=True, timeout=900)
    shutil.rmtree(gen_dir, ignore_errors=True)
    shutil.rmtree(seq, ignore_errors=True)
    return dst


# -- did it come back a frame off? ---------------------------------------------------------------
def alignment(source: np.ndarray, result: np.ndarray, outside: np.ndarray) -> str | None:
    """"ok", "shifted", or None (no verdict). `source[k]` is the source frame generated frame k was made from, in the
    model's view (same crop, same small size); `outside[k]` is True away from the person. Only frames where the
    picture outside the person changes both ways give evidence; a static background gives none."""
    n = min(len(source), len(result), len(outside))
    informative = shifted = 0
    for k in range(1, n - 1):
        m = outside[k]
        if m.sum() < 0.05 * m.size:
            continue
        diff = lambda a, b, m=m: float(np.abs(a[m] - b[m]).mean())
        if diff(source[k], source[k - 1]) < MOTION or diff(source[k], source[k + 1]) < MOTION:
            continue
        informative += 1
        here = diff(result[k], source[k])
        if min(diff(result[k], source[k - 1]), diff(result[k], source[k + 1])) < (1 - SHIFT_MARGIN) * here:
            shifted += 1
    if informative < MIN_INFORMATIVE:
        return None
    return "shifted" if shifted >= SHIFT_SHARE * informative else "ok"
