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
                          "stream=codec_type,width,height,r_frame_rate,nb_frames:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    num, den = v["r_frame_rate"].split("/")
    return VideoInfo(width=v["width"], height=v["height"], fps=float(num) / float(den),
                     frames=int(v.get("nb_frames", 0) or 0), duration=float(d["format"]["duration"]),
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
                          *vf, "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, check=True).stdout
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


def poster(video: Path, dst: Path, height: int = 480) -> Path:
    info = probe(video)
    frame_at(video, info.duration / 2, height).save(dst, quality=88)
    return dst
