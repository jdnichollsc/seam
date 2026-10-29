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

server = MCPServer(
    name="seam",
    title="Seam — AI transitions",
    version="0.1.0",
    instructions=(
        "Seam makes a short video that transitions from image A to image B with an effect preset "
        "(smoke, water bend, whip pan, ...), rendered locally on ComfyUI with open-weights models. "
        "Typical flow: list_presets → create_transition(local image paths, preset) → wait_job → "
        "preview_job to look at the result → refine_job to change preset/prompt/seed. Jobs run one at a "
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
    """Check that the Seam server and its ComfyUI backend are reachable; returns the active model."""
    return await _req("GET", "/api/health")


@server.tool()
async def list_presets() -> list[dict[str, Any]]:
    """List transition presets (id, name, category, camera, seconds = the take lengths each one offers). Use the id in create_transition."""
    return await _req("GET", "/api/presets")


@server.tool()
async def create_transition(image_a: str, image_b: str, preset: str, seconds: int = 5, seed: int | None = None,
                            prompt_extra: str = "", a_desc: str | None = None, b_desc: str | None = None,
                            a_ref: str | None = None, b_ref: str | None = None) -> dict[str, Any]:
    """Queue a transition video from image A (first frame) to image B (last frame).

    image_a / image_b: local file paths to images, or to short clips (1-6 s .mp4/.mov/.webm): with a clip, the
    transition starts from the last frame of clip A / lands on the first frame of clip B, and the job also gets a
    merged video (clip A + transition + clip B, `merged_url`). preset: an id from list_presets. seconds: take length,
    one of the preset's `seconds` from list_presets (default 5). prompt_extra: extra direction appended
    to the preset. Seam reads both shots itself (local model) to describe them in the prompt; pass a_desc / b_desc
    only to correct that reading — a noun phrase like "a young woman with a calm smile in a red jacket on a rainy
    street at night" — and a_ref / b_ref as short labels like "the woman in the red jacket". "" means describe
    nothing. Returns the job (with the descriptions used once it starts); follow with wait_job.
    """
    a, b = await asyncio.gather(_upload(image_a, "a"), _upload(image_b, "b"))
    body = {"image_a": a["id"], "image_b": b["id"], "clip_a": a.get("clip"), "clip_b": b.get("clip"), "preset": preset, "seconds": seconds, "seed": seed, "prompt_extra": prompt_extra,
            "a_desc": a_desc, "b_desc": b_desc, "a_ref": a_ref, "b_ref": b_ref}
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
                     a_ref: str | None = None, b_ref: str | None = None) -> dict[str, Any]:
    """Re-run a previous job with changes (new preset or length, extra direction, descriptions, or a pinned seed).

    Unchanged fields are inherited; without `seed` a fresh take is rolled. The new job links to its parent.
    """
    body = {"preset": preset, "seconds": seconds, "prompt_extra": prompt_extra, "seed": seed, "a_desc": a_desc, "b_desc": b_desc,
            "a_ref": a_ref, "b_ref": b_ref}
    return _brief(await _req("POST", f"/api/jobs/{job_id}/refine", json=body))


@server.tool()
async def cancel_job(job_id: str) -> dict[str, Any]:
    """Cancel a queued or running job."""
    return _brief(await _req("POST", f"/api/jobs/{job_id}/cancel"))


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
