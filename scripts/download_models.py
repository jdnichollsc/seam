"""Download the open-weights models Seam needs into a ComfyUI models folder.

With no --only, the default sets: l25 (LTX-2.5, the shot reader and the chat, ~44 GB) and shot (Z-Image-Turbo,
~21 GB). Every other set is optional and needs --only: l23, wan22, shot_klein, shot_juggernaut, orbit. Resumable;
every file is checked against its expected byte size before it is moved into place. Read each set's licenses first
(listed below and in the app's Models panel).

  python scripts/download_models.py --models-dir ~/ComfyUI/models                     # l25 + shot
  python scripts/download_models.py --models-dir "C:/path/to/ComfyUI/models" --only orbit

LTX-2.5 is gated on Hugging Face: accept the license at https://huggingface.co/Lightricks/LTX-2.5 first and
provide a token via HF_TOKEN or `hf auth login` (stored in ~/.cache/huggingface/token).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from seam.downloads import SETS, DownloadError, fetch, present, target, token
except ImportError:  # run from a checkout without `pip install -e .`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from seam.downloads import SETS, DownloadError, fetch, present, target, token


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models-dir", required=True, type=Path, help="ComfyUI models folder (contains checkpoints/, vae/, ...)")
    ap.add_argument("--only", choices=sorted(SETS), action="append", help="limit to one model set (repeatable)")
    args = ap.parse_args()
    root = args.models_dir.expanduser()
    if not root.is_dir():
        print(f"models dir not found: {root}", file=sys.stderr)
        return 2
    tok = token()
    for key in args.only or [k for k, s in SETS.items() if s.default]:
        s = SETS[key]
        print(f"{s.label}: " + "; ".join(f"{lic.name} ({lic.url})" for lic in s.licenses))
        for f in s.files:
            if present(root, f):
                print(f"  ok      {f.folder}/{f.name}")
                continue
            if f.gated and not tok:
                print(f"  needs a Hugging Face token (gated): {f.url}", file=sys.stderr)
                return 3
            print(f"  fetch   {f.folder}/{f.name}")
            try:
                fetch(f, target(root, f), tok,
                      lambda done, f=f: print(f"\r  {f.name}: {done / 1e9:6.2f} / {f.size / 1e9:.2f} GB", end="", flush=True))
                print()
            except DownloadError as e:
                print(f"\n  {e}", file=sys.stderr)
                return 4
    print("all models present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
