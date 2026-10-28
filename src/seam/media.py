"""ffmpeg/ffprobe helpers: probing, output sizing, frame extraction, contact sheets."""

from __future__ import annotations

import io
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

_WINGET_FFMPEG = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"


def _ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    for p in _WINGET_FFMPEG.glob("Gyan.FFmpeg*/ffmpeg-*/bin"):
        os.environ["PATH"] += os.pathsep + str(p)
        if shutil.which("ffmpeg"):
            return
    raise RuntimeError("ffmpeg not found on PATH — install it (see install.md)")


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    fps: float
    frames: int
    duration: float
    has_audio: bool


def probe(path: Path) -> VideoInfo:
    _ensure_ffmpeg()
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,width,height,r_frame_rate,nb_frames,duration:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=60).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    # the picture's own length: a container can run longer than its video (audio tail), and Seam cuts on pictures
    duration = float(v.get("duration") or d["format"]["duration"])
    return VideoInfo(width=v["width"], height=v["height"], fps=float(num) / float(den),
                     frames=int(v.get("nb_frames", 0) or 0), duration=duration,
                     has_audio=any(s["codec_type"] == "audio" for s in d["streams"]))


def output_size(width: int, height: int, megapixels: float = 0.9, multiple: int = 32) -> tuple[int, int]:
    """Size with image A's aspect ratio, ~megapixels total, snapped to the model's multiple."""
    ar = width / height
    w = math.sqrt(megapixels * 1_000_000 * ar)
    h = w / ar
    return max(multiple, round(w / multiple) * multiple), max(multiple, round(h / multiple) * multiple)


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.width, im.height


def normalize_image(src: Path, dst: Path) -> Path:
    """Re-encode an upload as RGB PNG with EXIF orientation applied (no metadata carried over)."""
    with Image.open(src) as im:
        ImageOps.exif_transpose(im).convert("RGB").save(dst, "PNG")
    return dst


def frame_at(video: Path, t: float, height: int | None = None) -> Image.Image:
    _ensure_ffmpeg()
    vf = ["-vf", f"scale=-2:{height}"] if height else []
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", str(video), "-frames:v", "1",
                          *vf, "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True, timeout=60).stdout
    return Image.open(io.BytesIO(raw)).convert("RGB")


def contact_sheet(video: Path, dst: Path, n: int = 8, height: int = 220) -> Path:
    info = probe(video)
    frames = [frame_at(video, min(info.duration * i / (n - 1), info.duration - 0.05), height) for i in range(n)]
    sheet = Image.new("RGB", (sum(f.width for f in frames), height))
    x = 0
    for f in frames:
        sheet.paste(f, (x, 0))
        x += f.width
    sheet.save(dst)
    return dst


def review_sheet(video: Path, dst: Path, n: int = 8, height: int = 220, middle: tuple[float, float] = (0.3, 0.7)) -> Path:
    """Two rows: row 1 spans the whole clip, row 2 samples 30-70% of it, where the effect does its work."""
    info = probe(video)
    last = info.duration - 0.05
    lo, hi = middle
    times = [min(info.duration * i / (n - 1), last) for i in range(n)]
    times += [min(info.duration * (lo + (hi - lo) * i / (n - 1)), last) for i in range(n)]
    frames = [frame_at(video, t, height) for t in times]
    width = sum(f.width for f in frames[:n])
    sheet = Image.new("RGB", (width, 2 * height))
    for row in range(2):
        x = 0
        for f in frames[row * n:(row + 1) * n]:
            sheet.paste(f, (x, row * height))
            x += f.width
    tmp = dst.with_name(dst.stem + ".tmp.png")
    sheet.save(tmp)
    tmp.replace(dst)  # concurrent callers never read a half-written file
    return dst


CLIP_SECONDS = (1.0, 6.0)  # clip inputs, as Higgsfield's Transitions: short shots around the transition


def normalize_clip(src: Path, dst: Path) -> Path:
    """Re-encode an uploaded clip (mp4/mov/webm) as H.264/AAC MP4 on one clock: picture and sound start at 0 with
    their offset kept (late sound gets leading silence), the sound is cut to the picture, and the file carries an
    exact video duration. Everything after this (frames, joins) can trust probe()."""
    _ensure_ffmpeg()
    has_audio = probe(src).has_audio
    audio = ["-map", "0:a:0", "-af", "aresample=async=1:first_pts=0", "-c:a", "aac", "-ar", "48000", "-ac", "2",
             "-shortest"] if has_audio else ["-an"]
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-c:v", "libx264", "-crf", "16",
                    "-preset", "veryfast", "-pix_fmt", "yuv420p", *audio, "-avoid_negative_ts", "make_zero",
                    "-movflags", "+faststart", str(tmp)], check=True, capture_output=True, timeout=180)
    tmp.replace(dst)
    return dst


def clip_frame(video: Path, dst: Path, last: bool) -> Path:
    """First or last frame of a clip as a PNG: the still the transition starts from (A's last) or lands on (B's first)."""
    _ensure_ffmpeg()
    seek = ["-ss", f"{max(probe(video).duration - 0.5, 0):.3f}"] if last else []
    # -update 1 keeps overwriting one file, so after seeking near the end it holds the final decoded video frame
    subprocess.run(["ffmpeg", "-v", "error", "-y", *seek, "-i", str(video), "-map", "0:v:0",
                    *([] if last else ["-frames:v", "1"]), "-update", "1", str(dst)], check=True, capture_output=True,
                   timeout=60)
    if not dst.is_file():
        raise ValueError("could not read a frame from this clip")
    return dst


def merge_clips(transition: Path, dst: Path, clip_a: Path | None = None, clip_b: Path | None = None) -> Path:
    """clip A + transition + clip B as one video at the transition's size and frame rate. Clips are scaled to
    cover and centre-cropped (aspect follows the transition, which follows A); a clip without sound gets silence."""
    _ensure_ffmpeg()
    t = probe(transition)
    parts = [p for p in (clip_a, transition, clip_b) if p is not None]
    inputs: list[str] = []
    chains: list[str] = []
    for i, p in enumerate(parts):
        inputs += ["-i", str(p)]
        info = probe(p)
        # every segment is cut to its picture's length and restarts at t=0, so sound and picture stay together
        chains.append(f"[{i}:v]trim=duration={info.duration:.3f},setpts=PTS-STARTPTS,scale={t.width}:{t.height}:"
                      f"force_original_aspect_ratio=increase,crop={t.width}:{t.height},setsar=1,fps={t.fps:g},"
                      f"format=yuv420p[v{i}]")
        src = (f"[{i}:a]asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad" if info.has_audio
               else "anullsrc=r=48000:cl=stereo")
        chains.append(f"{src},atrim=duration={info.duration:.3f}[a{i}]")
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(parts)))
    graph = ";".join(chains) + f";{joined}concat=n={len(parts)}:v=1:a=1[v][a]"
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac",
                    "-movflags", "+faststart", str(tmp)], check=True, capture_output=True, timeout=300)
    tmp.replace(dst)
    return dst


def poster(video: Path, dst: Path, height: int = 480) -> Path:
    info = probe(video)
    frame_at(video, info.duration / 2, height).save(dst, quality=88)
    return dst
