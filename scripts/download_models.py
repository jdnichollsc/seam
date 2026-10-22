"""Download the open-weights models Seam needs into a ComfyUI models folder.

Only the release models (LTX family) — about 83 GB in total. Resumable; every file is checked against its
expected byte size before it is moved into place.

  python scripts/download_models.py --models-dir ~/ComfyUI/models --only l25             # launch effects (~44 GB)
  python scripts/download_models.py --models-dir "C:/path/to/ComfyUI/models"            # + optional LTX-2.3 (~83 GB)

LTX-2.5 is gated on Hugging Face: accept the license at https://huggingface.co/Lightricks/LTX-2.5 first and
provide a token via HF_TOKEN or `hf auth login` (stored in ~/.cache/huggingface/token).
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

HF = "https://huggingface.co"
MODELS = {
    "l25": [  # LTX-2.5 FLF2V — the default model (license: LTX-2.x Community License, gated)
        ("diffusion_models", f"{HF}/Lightricks/LTX-2.5/resolve/main/diffusion_models/ltx-2.5-22b-distilled-transformer-comfy-int8-convrot.safetensors", 21504034224, True),
        ("vae", f"{HF}/Lightricks/LTX-2.5/resolve/main/vae/ltx-2.5-video-vae-bf16.safetensors", 1472223346, True),
        ("vae", f"{HF}/Lightricks/LTX-2.5/resolve/main/vae/ltx-2.5-audio-vae-bf16.safetensors", 364866540, True),
        ("text_encoders", f"{HF}/Lightricks/LTX-2.5/resolve/main/text_encoders/gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors", 15372969374, True),
        ("text_encoders", f"{HF}/Comfy-Org/gemma-4/resolve/main/text_encoders/gemma4_e2b_it_int8_convrot.safetensors", 5199997904, False),
    ],
    "l23": [  # LTX-2.3 + transition LoRA — optional: only deferred/candidate presets (e.g. fire) use it
        ("checkpoints", f"{HF}/Lightricks/LTX-2.3-fp8/resolve/main/ltx-2.3-22b-distilled-fp8.safetensors", 29531884062, False),
        ("text_encoders", f"{HF}/Comfy-Org/ltx-2/resolve/main/split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors", 9447702218, False),
        ("loras", f"{HF}/valiantcat/LTX-2.3-Transition-LORA/resolve/main/ltx2.3-transition.safetensors", 390229424, False),
    ],
}


def _token() -> str | None:
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"]
    p = Path.home() / ".cache" / "huggingface" / "token"
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


def _download(url: str, dst: Path, size: int, token: str | None) -> None:
    part = dst.with_name(dst.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    headers = {"User-Agent": "seam-downloader"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, part.open("ab" if have else "wb") as fh:
        done = have
        while chunk := r.read(8 * 1024 * 1024):
            fh.write(chunk)
            done += len(chunk)
            print(f"\r  {dst.name}: {done / 1e9:6.2f} / {size / 1e9:.2f} GB", end="", flush=True)
    print()
    if part.stat().st_size != size:
        raise RuntimeError(f"{dst.name}: size {part.stat().st_size} != expected {size}; re-run to resume")
    part.replace(dst)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", required=True, type=Path, help="ComfyUI models folder (contains checkpoints/, vae/, ...)")
    ap.add_argument("--only", choices=sorted(MODELS), action="append", help="limit to one model set (repeatable)")
    args = ap.parse_args()
    root = args.models_dir.expanduser()
    if not root.is_dir():
        print(f"models dir not found: {root}", file=sys.stderr)
        return 2
    token = _token()
    sets = args.only or list(MODELS)
    for key in sets:
        for folder, url, size, gated in MODELS[key]:
            dst = root / folder / url.rsplit("/", 1)[1]
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and dst.stat().st_size == size:
                print(f"  ok      {folder}/{dst.name}")
                continue
            if gated and not token:
                print(f"  needs a Hugging Face token (gated): {url}", file=sys.stderr)
                return 3
            print(f"  fetch   {folder}/{dst.name}")
            try:
                _download(url, dst, size, token if gated or token else None)
            except urllib.error.HTTPError as e:
                hint = " — accept the license on Hugging Face first" if e.code in (401, 403) else ""
                print(f"  HTTP {e.code} for {url}{hint}", file=sys.stderr)
                return 4
    print("all models present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
