from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from seam import server
from seam.jobs import JobStore


def test_live_events_carry_the_same_urls_as_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The web app swaps its job for the SSE copy, so a finished job must arrive with its video URL."""
    store = JobStore(tmp_path / "jobs.db")
    monkeypatch.setattr(server, "engine", SimpleNamespace(store=store))

    async def run() -> list[dict]:
        body: Any = (await server.events()).body_iterator
        assert (await anext(body)).startswith("retry:")
        job = store.create({"image_a": str(tmp_path / "a.png"), "image_b": str(tmp_path / "b.png"), "preset": "smoke"})
        store.update(job.id, status="succeeded", progress=1.0, output=f"{job.id}.mp4", poster=f"{job.id}.jpg",
                     sheet=f"{job.id}_sheet.png")
        chunks = [await anext(body), await anext(body)]
        await body.aclose()
        return [json.loads(c.split("data: ", 1)[1]) for c in chunks]

    created, done = asyncio.run(run())
    assert created["params"]["image_a"] == "a.png"  # never expose server paths
    assert done == server._job_out(store.get(done["id"]))
    assert done["output_url"] == f"/outputs/{done['id']}.mp4"
    assert done["poster_url"] == f"/outputs/{done['id']}.jpg"
