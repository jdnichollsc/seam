"""The model sets Seam can fetch into a ComfyUI models folder: one table for the CLI, the doctor and the app.

Only URLs from this table, only into the configured models folder, and the Hugging Face token only goes to
huggingface.co for gated files (never along a redirect to another host). Licenses are shown and accepted before a
download starts; the acceptance is recorded in <data_dir>/licenses.json.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

HF = "https://huggingface.co"
HF_HOST = "huggingface.co"
CHUNK = 8 * 1024 * 1024
SPARE = 1 << 30  # free space kept beyond the files themselves


@dataclass(frozen=True)
class File:
    folder: str  # ComfyUI models subfolder
    url: str
    size: int    # bytes; a file is only moved into place at exactly this size
    gated: bool = False

    @property
    def name(self) -> str:
        return self.url.rsplit("/", 1)[1]


@dataclass(frozen=True)
class License:
    name: str
    url: str
    note: str = ""


@dataclass(frozen=True)
class ModelSet:
    id: str
    label: str
    enables: tuple[str, ...]  # workflow ids (slots.json) these files make runnable
    files: tuple[File, ...]
    licenses: tuple[License, ...]
    default: bool = False     # the CLI fetches it when no --only is given

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def gated(self) -> bool:
        return any(f.gated for f in self.files)


LTX = License("LTX-2 Community License", f"{HF}/Lightricks/LTX-2.5", "gated: accept it on Hugging Face first")
GEMMA = License("Gemma Terms of Use", "https://ai.google.dev/gemma/terms")
APACHE = License("Apache 2.0", "https://www.apache.org/licenses/LICENSE-2.0")
MINIMAX = License("MiniMax H3 Community License", f"{HF}/Comfy-Org/MiniMax-H3", "read its terms before commercial use")

SETS: dict[str, ModelSet] = {s.id: s for s in (
    ModelSet("l25", "LTX-2.5 (the default transition model, the shot reader and the chat)", ("l25", "chat", "describe"), (
        File("diffusion_models", f"{HF}/Lightricks/LTX-2.5/resolve/main/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors", 21504034224, True),
        File("vae", f"{HF}/Lightricks/LTX-2.5/resolve/main/vae/ltx-2.5-video-vae-bf16.safetensors", 1472223346, True),
        File("vae", f"{HF}/Lightricks/LTX-2.5/resolve/main/vae/ltx-2.5-audio-vae-bf16.safetensors", 364866540, True),
        File("text_encoders", f"{HF}/Lightricks/LTX-2.5/resolve/main/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors", 15372969374, True),
        File("text_encoders", f"{HF}/Comfy-Org/gemma-4/resolve/main/text_encoders/gemma4_e2b_it_int8_convrot.safetensors", 5199997904),
    ), (LTX, GEMMA), default=True),
    ModelSet("shot", "Z-Image-Turbo (Generate shot)", ("shot",), (
        File("diffusion_models", f"{HF}/Comfy-Org/z_image_turbo/resolve/main/split_files/diffusion_models/z_image_turbo_bf16.safetensors", 12309866400),
        File("text_encoders", f"{HF}/Comfy-Org/z_image_turbo/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors", 8044982048),
        File("vae", f"{HF}/Comfy-Org/z_image_turbo/resolve/main/split_files/vae/ae.safetensors", 335304388),
    ), (APACHE,), default=True),
    ModelSet("l23", "LTX-2.3 + transition LoRA", ("l23",), (
        File("checkpoints", f"{HF}/Lightricks/LTX-2.3-fp8/resolve/main/ltx-2.3-22b-distilled-fp8.safetensors", 29531884062),
        File("text_encoders", f"{HF}/Comfy-Org/ltx-2/resolve/main/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors", 9447702218),
        File("loras", f"{HF}/valiantcat/LTX-2.3-Transition-LORA/resolve/main/ltx2.3-transition.safetensors", 390229424),
    ), (License("LTX-2 Community License", f"{HF}/Lightricks/LTX-2.3-fp8"), GEMMA,
        License("LTX-2.3 Transition LoRA terms", f"{HF}/valiantcat/LTX-2.3-Transition-LORA", "see its model card"))),
    ModelSet("wan22", "Wan 2.2 14B first/last frame", ("wan22",), (
        File("diffusion_models", f"{HF}/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors", 14294742832),
        File("diffusion_models", f"{HF}/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors", 14294742832),
        File("text_encoders", f"{HF}/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors", 6735906897),
        File("vae", f"{HF}/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors", 253815318),
    ), (APACHE,)),
    ModelSet("shot_klein", "FLUX.2 Klein 4B (Generate shot)", ("shot_klein",), (
        File("diffusion_models", f"{HF}/Comfy-Org/flux2-klein/resolve/main/split_files/diffusion_models/flux-2-klein-base-4b.safetensors", 7751105712),
        File("text_encoders", f"{HF}/Comfy-Org/flux2-klein/resolve/main/split_files/text_encoders/qwen_3_4b.safetensors", 8044982048),
        File("vae", f"{HF}/Comfy-Org/flux2-klein/resolve/main/split_files/vae/flux2-vae.safetensors", 336211292),
    ), (APACHE,)),
    ModelSet("shot_juggernaut", "Juggernaut XL v9 (Generate shot)", ("shot_juggernaut",), (
        File("checkpoints", f"{HF}/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors", 7105348188),
    ), (License("CreativeML OpenRAIL-M", f"{HF}/RunDiffusion/Juggernaut-XL-v9", "use restrictions apply"),)),
    ModelSet("shot_h3", "MiniMax H3 Image (Generate shot)", ("shot_h3",), (
        File("diffusion_models", f"{HF}/Comfy-Org/MiniMax-H3-Image/resolve/main/diffusion_models/minimax_h3_image_pruned_int8_convrot.safetensors", 20979314802),
        File("text_encoders", f"{HF}/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", 15687142551),
        File("vae", f"{HF}/Comfy-Org/MiniMax-H3-Image/resolve/main/vae/minimax_h3_image_vae_fp16.safetensors", 1168505312),
    ), (License("MiniMax H3 Image Community License", f"{HF}/MiniMaxAI/MiniMax-H3-Image", "read its terms before commercial use"),)),
    ModelSet("orbit", "MiniMax-H3 Ref2VA + ORB360 (360° orbit)", ("orbit",), (
        File("diffusion_models", f"{HF}/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors", 20970379616),
        File("text_encoders", f"{HF}/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", 15687142551),
        File("vae", f"{HF}/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors", 2811065184),
        File("vae", f"{HF}/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors", 605254808),
        File("loras", f"{HF}/MATLOWAI/MiniMax-H3-ORB360-CardSpin/resolve/main/minimax_h3_orb360_step1500.safetensors", 596450168),
    ), (MINIMAX, License("MiniMax H3 Community License (ORB360 LoRA)", f"{HF}/MATLOWAI/MiniMax-H3-ORB360-CardSpin"))),
)}


def set_for(workflow: str) -> ModelSet | None:
    return next((s for s in SETS.values() if workflow in s.enables), None)


def command(set_id: str) -> str:
    return f"python scripts/download_models.py --models-dir <ComfyUI>/models --only {set_id}"


def token() -> str | None:
    """The machine's Hugging Face login, read on the server: Seam never asks for it in the browser."""
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"].strip()
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    p = home / "token"
    return p.read_text(encoding="utf-8").strip() if p.is_file() else None


class DownloadError(Exception):
    """A failure in words a user can act on; never carries the token or a signed URL."""


class Cancelled(Exception):
    pass


def target(root: Path, f: File) -> Path:
    """Where a file goes; refused if it would land outside the models folder (a link or junction included)."""
    base = root.resolve()
    dst = (root / f.folder / f.name).resolve()
    if not dst.is_relative_to(base):
        raise DownloadError(f"{f.folder}/{f.name} resolves outside the models folder")
    return dst


def present(root: Path, f: File) -> bool:
    try:
        dst = target(root, f)
    except DownloadError:
        return False
    return dst.is_file() and dst.stat().st_size == f.size


class _Redirects(urllib.request.HTTPRedirectHandler):
    """HTTPS only, a few hops, and the Authorization header stays with huggingface.co (its storage hosts get signed
    URLs). urllib's own handler copies every header to any host it is sent to."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 — urllib's signature
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.HTTPError(req.full_url, code, "refused a redirect off HTTPS", headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urlsplit(newurl).hostname != HF_HOST:
            new.remove_header("Authorization")
        return new


OPENER = urllib.request.build_opener(_Redirects)


def _start_of(content_range: str | None) -> int | None:
    m = re.match(r"bytes (\d+)-", content_range or "")
    return int(m.group(1)) if m else None


def fetch(f: File, dst: Path, tok: str | None, on_progress: Callable[[int], None] | None = None,
          cancelled: Callable[[], bool] | None = None, opener: urllib.request.OpenerDirector | None = None) -> None:
    """Resumable: a .part file grows across attempts and is moved into place at exactly the expected size."""
    parts = urlsplit(f.url)
    if parts.scheme != "https" or parts.hostname != HF_HOST:
        raise DownloadError(f"{f.name}: only Hugging Face URLs are fetched")
    part = dst.with_name(dst.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > f.size:
        part.unlink()
        have = 0
    headers = {"User-Agent": "seam-downloader"}
    if f.gated and tok:
        headers["Authorization"] = f"Bearer {tok}"
    if have:
        headers["Range"] = f"bytes={have}-"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        r = (opener or OPENER).open(urllib.request.Request(f.url, headers=headers), timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have == f.size:  # the part is already whole
            r = None
        else:
            hint = (": accept the license on Hugging Face, then run `hf auth login` on this machine"
                    if e.code in (401, 403) and f.gated else "")
            raise DownloadError(f"{f.name}: HTTP {e.code} from {urlsplit(e.filename or f.url).hostname}{hint}") from None
    except (urllib.error.URLError, OSError) as e:
        raise DownloadError(f"{f.name}: could not reach {parts.hostname} ({type(e).__name__})") from None
    if r is not None:
        with r:
            if have and r.status == 206:
                if _start_of(r.headers.get("Content-Range")) != have:
                    part.unlink()
                    raise DownloadError(f"{f.name}: the server resumed at the wrong place; try again")
                mode = "ab"
            else:  # 200: the whole file again
                have, mode = 0, "wb"
            done = have
            try:
                with part.open(mode) as fh:
                    while chunk := r.read(CHUNK):
                        if cancelled and cancelled():
                            raise Cancelled
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress:
                            on_progress(done)
            except OSError as e:
                raise DownloadError(f"{f.name}: the download broke off ({type(e).__name__}); try again to resume") from None
    got = part.stat().st_size
    if got != f.size:
        raise DownloadError(f"{f.name}: got {got} bytes, expected {f.size}; try again to resume")
    part.replace(dst)


class Downloads:
    """One set at a time, on a thread; state goes out as "download" events."""

    def __init__(self, models_dir: Path | None, data_dir: Path, publish: Callable[[str, dict[str, Any]], None],
                 on_done: Callable[[], None] | None = None, opener: urllib.request.OpenerDirector | None = None) -> None:
        self.root = models_dir
        self.licenses_file = data_dir / "licenses.json"
        self._publish = publish
        self._on_done = on_done
        self._opener = opener
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._cancel = threading.Event()
        self.thread: threading.Thread | None = None

    def _accepted(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.licenses_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def listing(self) -> list[dict[str, Any]]:
        accepted = {a.get("set") for a in self._accepted()}
        with self._lock:
            state = dict(self._state) if self._state else None
        out = []
        for s in SETS.values():
            files = [{"folder": f.folder, "name": f.name, "size": f.size, "gated": f.gated,
                      "present": bool(self.root) and present(self.root, f)} for f in s.files]
            remaining = sum(f.size - (self._part_size(f) if self.root else 0) for f in s.files if not (self.root and present(self.root, f)))
            out.append({"id": s.id, "label": s.label, "enables": list(s.enables), "default": s.default,
                        "size": s.size, "missing": sum(f["size"] for f in files if not f["present"]),
                        "remaining": remaining,
                        "gated": s.gated, "files": files, "command": command(s.id),
                        "licenses": [{"name": lic.name, "url": lic.url, "note": lic.note} for lic in s.licenses],
                        "accepted": s.id in accepted,
                        "state": state if state and state["set"] == s.id else None})
        return out

    def free(self) -> int | None:
        return shutil.disk_usage(self.root).free if self.root and self.root.is_dir() else None

    def start(self, set_id: str, accept: bool) -> None:
        s = SETS.get(set_id)
        if s is None:
            raise KeyError(set_id)
        if not accept:
            raise ValueError("accept the licenses first")
        if not self.root or not self.root.is_dir():
            raise ValueError(f"set SEAM_MODELS_DIR to your ComfyUI models folder, or run: {command(set_id)}")
        files = [f for f in s.files if not present(self.root, f)]
        for f in files:
            target(self.root, f)  # refuse before anything is written
        tok = token()
        if any(f.gated for f in files) and not tok:
            raise ValueError("these files are gated: accept the license on Hugging Face, then run `hf auth login` "
                             "on this machine and try again")
        need = sum(f.size - self._part_size(f) for f in files)
        free = self.free() or 0
        if files and free < need + SPARE:
            raise ValueError(f"not enough disk space: {need / 1e9:.1f} GB needed (+1 GB spare), {free / 1e9:.1f} GB free")
        with self._lock:
            if self._state and self._state["status"] == "running":
                raise RuntimeError(f"{SETS[self._state['set']].label} is downloading; wait for it or cancel it")
            self._cancel.clear()
            self._state = {"set": s.id, "status": "running", "file": None, "done": 0,
                           "total": sum(f.size for f in files), "error": None}
        self._record(s)
        self._emit()
        self.thread = threading.Thread(target=self._run, args=(files, tok), daemon=True)
        self.thread.start()

    def _part_size(self, f: File) -> int:
        try:
            part = target(self.root, f).with_name(f.name + ".part")  # type: ignore[arg-type]
        except DownloadError:
            return 0
        return min(part.stat().st_size, f.size) if part.is_file() else 0

    def _record(self, s: ModelSet) -> None:
        records = self._accepted()
        records.append({"set": s.id, "licenses": [{"name": lic.name, "url": lic.url} for lic in s.licenses],
                        "files": [f.url for f in s.files], "accepted_at": time.time()})
        self.licenses_file.parent.mkdir(parents=True, exist_ok=True)
        self.licenses_file.write_text(json.dumps(records, indent=1), encoding="utf-8")

    def cancel(self, set_id: str) -> bool:
        with self._lock:
            running = bool(self._state and self._state["set"] == set_id and self._state["status"] == "running")
        if running:
            self._cancel.set()
        return running

    def _emit(self) -> None:
        with self._lock:
            state = dict(self._state) if self._state else None
        if state:
            self._publish("download", state)

    def _update(self, **kw: Any) -> None:
        with self._lock:
            if self._state:
                self._state.update(kw)

    def _run(self, files: list[File], tok: str | None) -> None:
        base = 0
        last = 0.0
        try:
            for f in files:
                self._update(file=f.name)
                self._emit()

                def progress(done: int, base: int = base) -> None:
                    nonlocal last
                    self._update(done=base + done)
                    if time.monotonic() - last > 0.5:
                        last = time.monotonic()
                        self._emit()

                fetch(f, target(self.root, f), tok, progress, self._cancel.is_set, self._opener)  # type: ignore[arg-type]
                base += f.size
                self._update(done=base)
            self._update(status="done", file=None)
            if self._on_done:
                try:
                    self._on_done()
                except Exception:  # noqa: BLE001 — the files are in place; a stale list refreshes in minutes
                    pass
        except Cancelled:
            self._update(status="cancelled")
        except DownloadError as e:
            self._update(status="failed", error=str(e))
        except Exception as e:  # noqa: BLE001 — never let the thread die silently
            self._update(status="failed", error=f"{type(e).__name__}")
        self._emit()
