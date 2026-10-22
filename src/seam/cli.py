"""`seam` command line: run the server, list presets, or make one transition without the UI."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="seam", description="Open-source AI transitions on ComfyUI.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web app + REST API")
    sub.add_parser("presets", help="list transition presets")
    mk = sub.add_parser("make", help="make one transition (no server needed)")
    mk.add_argument("image_a", type=Path)
    mk.add_argument("image_b", type=Path)
    mk.add_argument("--preset", required=True, help="a preset id from `seam presets`")
    mk.add_argument("--seed", type=int)
    mk.add_argument("--extra", default="", help="text appended to the preset prompt")
    mk.add_argument("-o", "--out", type=Path, default=Path("seam_transition.mp4"))
    sub.add_parser("mcp", help="run the MCP server (stdio) — talks to a running `seam serve`")
    sub.add_parser("doctor", help="check ffmpeg, the Comfy API endpoint, models and the web build")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import run

        run()
        return 0
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor()
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    from .config import get_settings
    from .presets import load_presets

    settings = get_settings()
    if args.cmd == "presets":
        from .engine import Engine

        shown = Engine.exposed_statuses()
        for p in load_presets(settings.presets_file).values():
            if p.status not in shown:
                continue
            print(f"{p.id:14} {p.name:16} {p.category}")
        return 0

    from .engine import Engine, TransitionRequest, store_upload
    from .jobs import JobStore

    settings.ensure_dirs()
    engine = Engine(settings, JobStore(settings.db_path))
    engine.start()
    job = engine.submit(TransitionRequest(image_a=str(store_upload(settings, args.image_a)),
                                          image_b=str(store_upload(settings, args.image_b)),
                                          preset=args.preset, seed=args.seed, prompt_extra=args.extra))
    print(f"job {job.id} queued (preset={args.preset})", flush=True)
    last = -1.0
    while True:
        j = engine.store.get(job.id)
        if j.progress != last:
            print(f"  {j.status} {j.progress:.0%}", flush=True)
            last = j.progress
        if j.status in ("succeeded", "failed", "cancelled", "interrupted"):
            break
        time.sleep(1)
    if j.status != "succeeded":
        print(f"failed: {j.error}", file=sys.stderr)
        return 1
    shutil.copy(settings.outputs_dir / j.output, args.out)
    print(json.dumps({"out": str(args.out), "wall_s": j.wall_s, **j.extra}, default=str, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
