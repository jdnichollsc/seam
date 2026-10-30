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
    mk.add_argument("--seconds", type=int, default=5, help="take length; `seam presets` lists each effect's lengths")
    mk.add_argument("--extra", default="", help="text appended to the preset prompt")
    mk.add_argument("-o", "--out", type=Path, default=Path("seam_transition.mp4"))
    sq = sub.add_parser("sequence", help="one video through several shots (talks to a running `seam serve`)")
    sqs = sq.add_subparsers(dest="action", required=True)
    sm = sqs.add_parser("make", help="render shot 1 -> shot 2 -> ... and join them into one video")
    sm.add_argument("shots", type=Path, nargs="+", help="2-8 images or 1-6 s clips, in order")
    sm.add_argument("--preset", required=True, help="one effect for every hand-off, or one per hand-off: smoke,ink")
    sm.add_argument("--seconds", default="5", help="one length, or one per hand-off: 5,10")
    sm.add_argument("--seed", type=int, help="seed for every segment (default: random per segment)")
    sm.add_argument("--story", default="", help="the idea of the whole film, shared by every transition")
    sm.add_argument("-o", "--out", type=Path, default=Path("seam_sequence.mp4"))
    for name, target, what in (("extend", "id", "add one shot to the end of a finished sequence"),
                               ("continue", "job", "turn a finished two-shot take into a sequence and add a shot")):
        g = sqs.add_parser(name, help=what)
        g.add_argument(target, help="sequence id" if target == "id" else "job id of a finished take")
        g.add_argument("shot", type=Path, help="the next shot: an image or a 1-6 s clip")
        g.add_argument("--preset", required=True, help="the effect into the new shot")
        g.add_argument("--seconds", type=int, default=5)
        g.add_argument("--seed", type=int)
        g.add_argument("--extra", default="", help="text appended to the preset prompt")
        g.add_argument("-o", "--out", type=Path, default=Path("seam_sequence.mp4"))
    se = sqs.add_parser("edit", help="remove / reorder shots and set the story; only new hand-offs render")
    se.add_argument("id")
    se.add_argument("--order", help="current shot numbers in the new order, e.g. 1,3,2,4 (leave one out to remove it)")
    se.add_argument("--transition", action="append", default=[], metavar="N=PRESET[:SECONDS]",
                    help="effect for transition N of the NEW order; needed for every new hand-off (repeatable)")
    se.add_argument("--extra", action="append", default=[], metavar="N=TEXT", help="direction for transition N")
    se.add_argument("--story", help="the idea of the whole film ('' clears it)")
    se.add_argument("-o", "--out", type=Path, default=Path("seam_sequence.mp4"))
    st = sqs.add_parser("status", help="a sequence and its takes")
    st.add_argument("id")
    sr = sqs.add_parser("refine", help="a new take of one segment, then join again")
    sr.add_argument("id")
    sr.add_argument("segment", type=int, help="1-based")
    sr.add_argument("--preset")
    sr.add_argument("--seconds", type=int)
    sr.add_argument("--seed", type=int)
    sr.add_argument("--extra", help="text appended to the preset prompt")
    sr.add_argument("-o", "--out", type=Path, default=Path("seam_sequence.mp4"))
    sub.add_parser("mcp", help="run the MCP server (stdio) — talks to a running `seam serve`")
    bk = sub.add_parser("backend", help="where takes render: this machine or Comfy Cloud")
    bk.add_argument("use", nargs="?", choices=["local", "cloud"], help="switch (omit to show the current one)")
    bk.add_argument("--set-key", action="store_true", help="save a Comfy Cloud API key (asked for, or read from stdin)")
    bk.add_argument("--clear-key", action="store_true", help="forget the saved Comfy Cloud API key")
    sub.add_parser("doctor", help="check ffmpeg, the Comfy API endpoint, models and the web build")
    args = ap.parse_args(argv)

    if args.cmd == "serve":
        from .server import run

        run()
        return 0
    if args.cmd == "backend":
        return _backend(args)
    if args.cmd == "doctor":
        from .doctor import run_doctor

        return run_doctor()
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        mcp_main()
        return 0

    if args.cmd == "sequence":
        return _sequence(args)

    from .config import get_settings
    from .presets import load_presets

    settings = get_settings()
    if args.cmd == "presets":
        from .engine import Engine

        shown = Engine.exposed_statuses()
        for p in load_presets(settings.presets_file).values():
            if p.status not in shown:
                continue
            print(f"{p.id:14} {p.name:16} {p.category:12} {'/'.join(map(str, p.seconds))} s")
        return 0

    from .engine import Engine, TransitionRequest, store_upload
    from .jobs import JobStore

    settings.ensure_dirs()
    from . import backend as backends

    if backends.current(settings)[0] == backends.CLOUD:
        print("rendering on Comfy Cloud: the shots are uploaded and this uses your credits", flush=True)
    engine = Engine(settings, JobStore(settings.db_path))
    engine.start()
    job = engine.submit(TransitionRequest(image_a=str(store_upload(settings, args.image_a)),
                                          image_b=str(store_upload(settings, args.image_b)),
                                          preset=args.preset, seconds=args.seconds, seed=args.seed,
                                          prompt_extra=args.extra))
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


def _backend(args: argparse.Namespace) -> int:
    import getpass

    from . import backend as backends
    from .config import get_settings

    settings = get_settings()
    try:
        if args.clear_key:
            backends.clear_key(settings)
        if args.set_key:  # never an argument: it would stay in the shell history
            key = getpass.getpass("Comfy Cloud API key: ") if sys.stdin.isatty() else sys.stdin.read()
            backends.verify_key(key)
            backends.save_key(settings, key)
        if args.use:
            locked = backends.locked_by_env(settings)
            if locked:
                raise ValueError(f"{locked} decides where Seam renders; change it instead")
            backends.choose(settings, args.use)
    except ValueError as e:
        print(f"seam: {e}", file=sys.stderr)
        return 1
    st = backends.state(settings)
    print(f"rendering on: {'Comfy Cloud (uses your credits)' if st['backend'] == backends.CLOUD else 'this machine'}"
          f" [{st['source']}]")
    print(f"Comfy Cloud key: {st['cloud']['key_source'] or f'not set (create one at {backends.KEYS_URL})'}")
    return 0


def _sequence(args: argparse.Namespace) -> int:
    """`seam sequence ...` is a client of `seam serve`, so its takes share the server's queue and show up in the app."""
    import os

    import httpx

    base = os.environ.get("SEAM_URL", "http://127.0.0.1:7860").rstrip("/")
    http = httpx.Client(base_url=base, timeout=120)

    def call(method: str, path: str, **kw):
        try:
            r = http.request(method, path, **kw)
        except httpx.ConnectError:
            raise SystemExit(f"Seam server not reachable at {base}: start it with `seam serve`.") from None
        if r.status_code >= 400:
            raise SystemExit(f"{r.status_code}: {r.json().get('detail', r.text) if r.content else ''}")
        return r.json()

    def wait(seq: dict) -> int:
        last = ""
        while seq["status"] in ("running", "assembling"):
            line = " | ".join(f"{i + 1}:{t['status'] if t else '?'}" for i, t in enumerate(seq["takes"]))
            if f"{seq['status']} {line}" != last:
                last = f"{seq['status']} {line}"
                print(f"  {last}", flush=True)
            time.sleep(2)
            seq = call("GET", f"/api/sequences/{seq['id']}")
        if seq["status"] != "succeeded":
            print(f"{seq['status']}: {seq.get('error')}", file=sys.stderr)
            return 1
        args.out.write_bytes(http.get(seq["output_url"]).content)
        print(json.dumps({"out": str(args.out), "id": seq["id"], "segments": seq["segments"]}, indent=1))
        return 0

    if args.action == "status":
        print(json.dumps(call("GET", f"/api/sequences/{args.id}"), indent=1))
        return 0
    def upload(path: Path) -> dict:
        kind = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}.get(path.suffix.lower(), "image/png")
        with path.open("rb") as fh:
            up = call("POST", "/api/uploads?role=mid", files={"file": (path.name, fh, kind)})
        return {"clip": up["clip"], "first": up["first"], "last": up["last"]} if up.get("clip") else {"image": up["id"]}

    if args.action in ("extend", "continue"):
        if not args.shot.is_file():
            raise SystemExit(f"file not found: {args.shot}")
        body = {"shot": upload(args.shot), "segment": {"preset": args.preset, "seconds": args.seconds,
                                                       "seed": args.seed, "prompt_extra": args.extra}}
        path = f"/api/sequences/{args.id}/shots" if args.action == "extend" else f"/api/jobs/{args.job}/continue"
        seq = call("POST", path, json=body)
        print(f"sequence {seq['id']}: {len(seq['shots'])} shots, rendering the new transition", flush=True)
        return wait(seq)
    if args.action == "edit":
        seq = call("GET", f"/api/sequences/{args.id}")
        order = [int(x) for x in args.order.split(",")] if args.order else list(range(1, len(seq["shots"]) + 1))
        transitions: dict[str, dict] = {}
        for t in args.transition:
            n, _, spec = t.partition("=")
            preset, _, secs = spec.partition(":")
            transitions[n] = {"preset": preset, **({"seconds": int(secs)} if secs else {})}
        for x in args.extra:
            n, _, text = x.partition("=")
            if n not in transitions:
                raise SystemExit(f"--extra {n}=… needs --transition {n}=PRESET")
            transitions[n]["prompt_extra"] = text
        seq = call("POST", f"/api/sequences/{args.id}/edit", json={
            "order": order, "transitions": transitions, "story": args.story,
            "expected_updated_at": seq["updated_at"]})
        if seq["status"] == "succeeded":
            print(json.dumps({"id": seq["id"], "story": seq["story"], "segments": seq["segments"]}, indent=1))
            return 0
        return wait(seq)
    if args.action == "refine":
        body = {"preset": args.preset, "seconds": args.seconds, "seed": args.seed, "prompt_extra": args.extra}
        return wait(call("POST", f"/api/sequences/{args.id}/segments/{args.segment}/refine", json=body))
    n = len(args.shots) - 1
    presets, lengths = args.preset.split(","), [int(x) for x in args.seconds.split(",")]
    if len(presets) not in (1, n) or len(lengths) not in (1, n):
        raise SystemExit(f"{len(args.shots)} shots: give one --preset/--seconds or {n} comma-separated values")
    missing = [str(p) for p in args.shots if not p.is_file()]
    if missing:
        raise SystemExit(f"file not found: {', '.join(missing)}")
    shots = [upload(path) for path in args.shots]
    segments = [{"preset": presets[i % len(presets)], "seconds": lengths[i % len(lengths)], "seed": args.seed}
                for i in range(n)]
    seq = call("POST", "/api/sequences", json={"shots": shots, "segments": segments, "story": args.story})
    print(f"sequence {seq['id']} queued ({n} segments)", flush=True)
    return wait(seq)


if __name__ == "__main__":
    raise SystemExit(main())
