"""Seam MCP server (stdio). A thin client of a running `seam serve`, so everything an agent makes also
appears in the web gallery. Install snippets for Claude Code / Claude Desktop / Codex are in install.md."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import Context, Image, MCPServer

BASE = os.environ.get("SEAM_URL", "http://127.0.0.1:7860").rstrip("/")
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
SEQ_TERMINAL = {"succeeded", "failed", "cancelled"}

server = MCPServer(
    name="seam",
    title="Seam — AI transitions",
    version="0.1.0",
    instructions=(
        "Seam makes a short video that transitions from image A to image B with an effect preset "
        "(smoke, water bend, whip pan, ...), rendered locally on ComfyUI with open-weights models. "
        "Typical flow: list_presets → create_transition(local image paths, preset) → wait_job → "
        "preview_job to look at the result → refine_job to change preset/prompt/seed. For a longer video through "
        "several shots: create_sequence(shot paths in order, one effect per hand-off) → wait_sequence → preview_job "
        "on each take → refine_segment for a bad one → preview_sequence. To grow a story shot by shot: "
        "continue_take(a finished two-shot job, next shot) or extend_sequence(sequence, next shot). To edit a "
        "sequence like a storyboard: edit_sequence(order without a shot = remove it, a new order = reorder; only "
        "hand-offs whose shots become adjacent render) and a `story` (the idea of the whole film, shared by every "
        "transition made after it is set). Jobs run one at a "
        "time; a 5 s take renders in about 40 s on an RTX 5090, a 10 s one in about 80 s (longer on the first run). Always show the user the preview."
    ),
)


async def _req(method: str, path: str, **kw: Any) -> Any:
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        try:
            r = await c.request(method, path, **kw)
        except httpx.ConnectError as e:
            raise RuntimeError(f"Seam server not reachable at {BASE} — start it with `seam serve`.") from e
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:400]}")
        return r.json()


VIDEO_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


async def _upload(path: str, role: str) -> dict[str, Any]:
    """Upload an image, or a 1-6 s clip (role a: the transition starts from its last frame; b: lands on its first)."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValueError(f"file not found: {p}")
    kind = VIDEO_TYPES.get(p.suffix.lower(), "application/octet-stream")
    with p.open("rb") as fh:
        return await _req("POST", f"/api/uploads?role={role}", files={"file": (p.name, fh, kind)})


def _brief(job: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "status", "progress", "parent_id", "error", "wall_s", "output_url", "sheet_url")
    out = {k: job.get(k) for k in keys if job.get(k) is not None}
    out["preset"] = job.get("params", {}).get("preset")
    out["seed"] = job.get("params", {}).get("seed")
    out["seconds"] = job.get("params", {}).get("seconds", 5)
    for k in ("a_desc", "b_desc"):  # what the prompt said about each shot (Seam's own reading unless you passed one)
        if job.get("params", {}).get(k):
            out[k] = job["params"][k]
    if job.get("output"):
        out["video_url"] = f"{BASE}/outputs/{job['output']}"
    if job.get("merged_url"):
        out["merged_url"] = f"{BASE}{job['merged_url']}"
    return out


@server.tool()
async def seam_status() -> dict[str, Any]:
    """Check that the Seam server is reachable; returns the active model and `backend`: "local" renders on this
    machine, "cloud" uploads the shots to Comfy Cloud and spends the user's credits (tell them before rendering).
    Only the user switches it, in the Seam app or with `seam backend`."""
    return await _req("GET", "/api/health")


@server.tool()
async def list_presets() -> list[dict[str, Any]]:
    """List transition presets (id, name, category, camera, seconds = the take lengths each one offers). Use the id in create_transition."""
    return await _req("GET", "/api/presets")


@server.tool()
async def list_models() -> dict[str, Any]:
    """The models a transition can pick and whether each can render on the current backend (`available`: true, false
    with a `reason`, or null when not checked). `kind: partner` models spend the user's credits on a paid provider.
    Leave `model` out to use the effect's own model: pick another only when the user names one."""
    return await _req("GET", "/api/models")


@server.tool()
async def create_transition(image_a: str, image_b: str, preset: str, seconds: int = 5, seed: int | None = None,
                            prompt_extra: str = "", a_desc: str | None = None, b_desc: str | None = None,
                            a_ref: str | None = None, b_ref: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Queue a transition video from image A (first frame) to image B (last frame).

    image_a / image_b: local file paths to images, or to short clips (1-6 s .mp4/.mov/.webm): with a clip, the
    transition starts from the last frame of clip A / lands on the first frame of clip B, and the job also gets a
    merged video (clip A + transition + clip B, `merged_url`). preset: an id from list_presets. seconds: take length,
    one of the preset's `seconds` from list_presets (default 5). prompt_extra: extra direction appended
    to the preset. Seam reads both shots itself (local model) to describe them in the prompt; pass a_desc / b_desc
    only to correct that reading — a noun phrase like "a young woman with a calm smile in a red jacket on a rainy
    street at night" — and a_ref / b_ref as short labels like "the woman in the red jacket". "" means describe
    nothing. model: an id from list_models, only when the user asks for one; the effect has only been reviewed on its
    own model. Returns the job (with the descriptions used once it starts); follow with wait_job.
    """
    a, b = await asyncio.gather(_upload(image_a, "a"), _upload(image_b, "b"))
    body = {"image_a": a["id"], "image_b": b["id"], "clip_a": a.get("clip"), "clip_b": b.get("clip"), "preset": preset, "seconds": seconds, "seed": seed, "prompt_extra": prompt_extra,
            "a_desc": a_desc, "b_desc": b_desc, "a_ref": a_ref, "b_ref": b_ref, "model": model}
    return _brief(await _req("POST", "/api/transitions", json=body))


@server.tool()
async def get_job(job_id: str) -> dict[str, Any]:
    """Current status of a job (queued/running/succeeded/failed/cancelled/interrupted)."""
    return _brief(await _req("GET", f"/api/jobs/{job_id}"))


@server.tool()
async def wait_job(job_id: str, ctx: Context, timeout_s: int = 900) -> dict[str, Any]:
    """Block until the job finishes (or timeout), reporting progress. Returns the final job."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        job = await _req("GET", f"/api/jobs/{job_id}")
        await ctx.report_progress(float(job.get("progress") or 0.0), 1.0)
        if job["status"] in TERMINAL or loop.time() > deadline:
            return _brief(job)
        await asyncio.sleep(2)


@server.tool()
async def list_jobs(limit: int = 10) -> list[dict[str, Any]]:
    """Most recent jobs, newest first."""
    return [_brief(j) for j in await _req("GET", "/api/jobs", params={"limit": limit})]


@server.tool()
async def preview_job(job_id: str) -> Image:
    """Two-row contact sheet of a succeeded job: row 1 = 8 frames across the whole clip, row 2 = 8 frames from 30% to
    70% of it, where the effect does its work. Judge these frames before re-reading the job's prompt or
    descriptions: the job's text says what was asked, not what the clip shows."""
    job = await _req("GET", f"/api/jobs/{job_id}")
    if job["status"] != "succeeded":
        raise RuntimeError(f"job {job_id} is {job['status']}, no preview yet")
    async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
        r = await c.get(f"/api/jobs/{job_id}/review-sheet")
        r.raise_for_status()
    return Image(data=r.content, format="png")


@server.tool()
async def refine_job(job_id: str, preset: str | None = None, seconds: int | None = None, prompt_extra: str | None = None,
                     seed: int | None = None, a_desc: str | None = None, b_desc: str | None = None,
                     a_ref: str | None = None, b_ref: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Re-run a previous job with changes (new preset or length, extra direction, descriptions, or a pinned seed).

    Unchanged fields are inherited; without `seed` a fresh take is rolled. The new job links to its parent. model: an
    id from list_models; "" returns to the effect's own model.
    """
    body = {"preset": preset, "seconds": seconds, "prompt_extra": prompt_extra, "seed": seed, "a_desc": a_desc, "b_desc": b_desc,
            "a_ref": a_ref, "b_ref": b_ref, "model": model}
    return _brief(await _req("POST", f"/api/jobs/{job_id}/refine", json=body))


@server.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job."""
    return _brief(await _req("POST", f"/api/jobs/{job_id}/cancel"))


def _seq_brief(seq: dict[str, Any]) -> dict[str, Any]:
    """Shots and transitions numbered from 1 in their CURRENT order; each take says whether it was made with the
    sequence's current story. `updated_at` is the revision to pass to edit_sequence."""
    out = {k: seq.get(k) for k in ("id", "status", "error", "story", "updated_at") if seq.get(k) is not None}
    if seq.get("output_url"):
        out["video_url"] = f"{BASE}{seq['output_url']}"
    out["canvas"] = [seq.get("width"), seq.get("height")]
    out["shots"] = [{"shot": i + 1, "file": s.get("first"), **({"clip": s["clip"]} if s.get("clip") else {})}
                    for i, s in enumerate(seq.get("shots") or [])]
    story = seq.get("story") or ""
    out["segments"] = [{"segment": i + 1, "from_shot": i + 1, "to_shot": i + 2,
                        **(_brief(t) if t else {"status": "missing"}),
                        **({"prompt_extra": t["params"].get("prompt_extra") or "",
                            "older_story": (t["params"].get("story") or "") != story} if t else {})}
                       for i, t in enumerate(seq.get("takes") or [])]
    return out


@server.tool()
async def create_sequence(shots: list[str], segments: list[dict[str, Any]], story: str = "") -> dict[str, Any]:
    """Queue one longer video through several shots: shot 1 → shot 2 → … with a transition at each hand-off.

    shots: 2-8 local file paths in order, images or short clips (1-6 s). A clip plays in full at its place; the
    transition before it lands on its first frame and the one after it leaves from its last frame. segments: one
    dict per hand-off (len(shots) - 1), each {"preset": id from list_presets, "model"?: id from list_models, "seconds"?: one of its lengths,
    "seed"?, "prompt_extra"?, "a_desc"?, "b_desc"?}. Every segment is checked before anything renders; each one is
    an ordinary job (judge it with preview_job). Every take renders at the first shot's size (the sequence keeps
    that canvas through later edits). story: optional, ≤300 chars, the idea of the whole film (who, where, mood),
    shared context for every transition; per-transition direction goes in each segment's prompt_extra. Follow with
    wait_sequence.
    """
    ups = await asyncio.gather(*[_upload(p, "mid") for p in shots])
    body = {"shots": [{"clip": u["clip"], "first": u["first"], "last": u["last"]} if u.get("clip")
                      else {"image": u["id"]} for u in ups], "segments": segments, "story": story}
    return _seq_brief(await _req("POST", "/api/sequences", json=body))


async def _grow_body(shot: str, preset: str, seconds: int, seed: int | None, prompt_extra: str,
                     model: str | None = None) -> dict[str, Any]:
    u = await _upload(shot, "mid")
    return {"shot": {"clip": u["clip"], "first": u["first"], "last": u["last"]} if u.get("clip") else {"image": u["id"]},
            "segment": {"preset": preset, "seconds": seconds, "seed": seed, "prompt_extra": prompt_extra, "model": model}}


@server.tool()
async def extend_sequence(sequence_id: str, shot: str, preset: str, seconds: int = 5, seed: int | None = None,
                          prompt_extra: str = "", model: str | None = None) -> dict[str, Any]:
    """Add one more shot to the end of a finished sequence: only the transition into it renders (from the last
    shot to this one), the other takes are reused and the video is joined again. shot: a local image or 1-6 s clip
    path. Follow with wait_sequence, then preview_job on the new take before adding the next shot."""
    body = await _grow_body(shot, preset, seconds, seed, prompt_extra, model)
    return _seq_brief(await _req("POST", f"/api/sequences/{sequence_id}/shots", json=body))


@server.tool()
async def continue_take(job_id: str, shot: str, preset: str, seconds: int = 5, seed: int | None = None,
                        prompt_extra: str = "", story: str = "", model: str | None = None) -> dict[str, Any]:
    """Start a story from a finished two-shot transition: it becomes the first transition of a new sequence
    (nothing re-renders) and `shot` is added after it. Use extend_sequence for the shots after that. story: the
    idea of the whole film, used by the new transition and every later one."""
    body = {**await _grow_body(shot, preset, seconds, seed, prompt_extra, model), "story": story}
    return _seq_brief(await _req("POST", f"/api/jobs/{job_id}/continue", json=body))


@server.tool()
async def edit_sequence(sequence_id: str, order: list[int], transitions: dict[str, dict[str, Any]] | None = None,
                        story: str | None = None, expected_updated_at: float | None = None) -> dict[str, Any]:
    """Edit a settled sequence like a storyboard, in one step. order: the CURRENT shot numbers (1-based, from
    get_sequence) in their new order — leave a number out to remove that shot, permute to reorder (2-8 shots).
    A transition whose two shots were already adjacent in that direction keeps its take; every other one is new and
    needs an entry in `transitions`, keyed by its number in the NEW order ("1" = new shot 1 → new shot 2), each
    {"preset", "seconds"?, "prompt_extra"?, "seed"?}; an entry for a kept transition makes a new take of it. Only new
    transitions render; removing the first or last shot renders nothing and just joins again. story: omit to keep,
    "" to clear; changing it re-renders nothing (takes made before keep theirs: older_story in the result; use
    refine_segment on one to remake it with the new story). The canvas never changes. Pass the updated_at you read
    as expected_updated_at so an edit never lands on a sequence that changed meanwhile. Tell the user how many
    transitions will render before calling this. Follow with wait_sequence when anything renders."""
    body = {"order": order, "transitions": transitions or {}, "story": story,
            "expected_updated_at": expected_updated_at}
    return _seq_brief(await _req("POST", f"/api/sequences/{sequence_id}/edit", json=body))


@server.tool()
async def get_sequence(sequence_id: str) -> dict[str, Any]:
    """Status of a sequence (running/assembling/succeeded/failed/cancelled) and of each segment's take."""
    return _seq_brief(await _req("GET", f"/api/sequences/{sequence_id}"))


@server.tool()
async def wait_sequence(sequence_id: str, ctx: Context, timeout_s: int = 1800) -> dict[str, Any]:
    """Block until every segment has rendered and the video is joined (or one failed, or timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        seq = await _req("GET", f"/api/sequences/{sequence_id}")
        takes = [t for t in seq.get("takes") or [] if t]
        done = sum(1.0 if t["status"] == "succeeded" else float(t.get("progress") or 0) for t in takes)
        await ctx.report_progress(done, float(len(seq["segments"])))
        if seq["status"] in SEQ_TERMINAL or loop.time() > deadline:
            return _seq_brief(seq)
        await asyncio.sleep(3)


@server.tool()
async def preview_sequence(sequence_id: str) -> Image:
    """Contact sheet of a joined sequence: 6 frames per segment, in rows. Use preview_job on a segment's take to
    judge its transition closely; this sheet is for the whole arc and the hand-offs."""
    seq = await _req("GET", f"/api/sequences/{sequence_id}")
    if seq["status"] != "succeeded" or not seq.get("sheet_url"):
        raise RuntimeError(f"sequence {sequence_id} is {seq['status']}, no preview yet")
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        r = await c.get(seq["sheet_url"])
        r.raise_for_status()
    return Image(data=r.content, format="png")


@server.tool()
async def refine_segment(sequence_id: str, segment: int, preset: str | None = None, seconds: int | None = None,
                         prompt_extra: str | None = None, seed: int | None = None, a_desc: str | None = None,
                         b_desc: str | None = None, model: str | None = None) -> dict[str, Any]:
    """A new take of one segment (1-based) once the sequence has settled; the other takes are reused and the video
    is joined again. Same rules as refine_job: unchanged fields are inherited, a fresh seed unless pinned. The new
    take uses the sequence's current story."""
    body = {"preset": preset, "seconds": seconds, "prompt_extra": prompt_extra, "seed": seed, "a_desc": a_desc,
            "b_desc": b_desc, "model": model}
    return _seq_brief(await _req("POST", f"/api/sequences/{sequence_id}/segments/{segment}/refine", json=body))


@server.tool()
async def cancel_sequence(sequence_id: str) -> dict[str, Any]:
    """Cancel a sequence and its queued or running takes (finished takes are kept)."""
    return _seq_brief(await _req("POST", f"/api/sequences/{sequence_id}/cancel"))


@server.tool()
async def star_take(job_id: str, starred: bool = True) -> dict[str, Any]:
    """Star a take the user liked, or unstar it. A starred take stays when its sequence goes to the trash."""
    return _brief(await _req("POST", f"/api/jobs/{job_id}/star", json={"starred": starred}))


@server.tool()
async def list_recipes() -> list[dict[str, Any]]:
    """Saved settings to use again: pass preset, model, seconds and prompt_extra to create_transition or a segment.
    A recipe with a `problem` can no longer render. `seed` only repeats a take on the same shots, so leave it out
    on new ones."""
    return await _req("GET", "/api/recipes")


@server.tool()
async def save_recipe(job_id: str, name: str) -> dict[str, Any]:
    """Save a finished take's settings under a name, when the user asks to keep how it was made."""
    return await _req("POST", "/api/recipes", json={"job_id": job_id, "name": name})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
