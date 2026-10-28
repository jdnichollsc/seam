"""What a take was made from: a receipt (inputs, prompt, seed, model, graph) and a ComfyUI bundle that re-runs it."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

from . import __version__
from .jobs import JobRecord
from .workflows import WorkflowSpec, export_graph

BUNDLE_README = """Seam take {id} as a ComfyUI workflow

1. Copy the two images from input/ into your ComfyUI input folder.
2. Drag workflow_api.json onto the ComfyUI canvas (or POST it to /prompt).
3. Queue it: same graph, prompt, seed and size as the take ({model}, seed {seed}).

The models it needs are listed in Seam's install.md. receipt.json says how the take was made.
"""


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def render_params(job: JobRecord, spec: WorkflowSpec) -> dict[str, Any]:
    """The slot values the engine sent for this job (only slots this graph has)."""
    p, x = job.params, job.extra
    values = {"prompt": x.get("prompt"), "seed": p.get("seed"), "seconds": p.get("seconds", 5),
              "width": x.get("width"), "height": x.get("height"), "enhance_prompt": p.get("enhance", True)}
    return {k: v for k, v in values.items() if k in spec.slots and v is not None}


def graph_file(job_id: str, outputs_dir: Path) -> Path:
    return outputs_dir / f"{job_id}.graph.json"


def archive_graph(job_id: str, spec: WorkflowSpec, params: dict[str, Any], outputs_dir: Path) -> Path:
    """The API graph exactly as submitted (stills by bundle file name), saved before the take runs."""
    graph = export_graph(spec, f"seam_{job_id}_a.png", f"seam_{job_id}_b.png", params)
    dst = graph_file(job_id, outputs_dir)
    dst.write_text(json.dumps({"workflow": spec.workflow_file.name, "workflow_sha256": sha256(spec.workflow_file),
                               "graph": graph}, indent=2), encoding="utf-8")
    return dst


def _submitted(job: JobRecord, spec: WorkflowSpec, outputs_dir: Path) -> dict[str, Any]:
    """The archived graph of a take; takes rendered before archiving existed are rebuilt from the current template."""
    f = graph_file(job.id, outputs_dir)
    if f.is_file():
        return {**json.loads(f.read_text(encoding="utf-8")), "archived": True}
    return {"workflow": spec.workflow_file.name, "workflow_sha256": sha256(spec.workflow_file), "archived": False,
            "graph": export_graph(spec, f"seam_{job.id}_a.png", f"seam_{job.id}_b.png", render_params(job, spec))}


def build_receipt(job: JobRecord, spec: WorkflowSpec, outputs_dir: Path) -> dict[str, Any]:
    p, x = job.params, job.extra
    a, b = Path(p["image_a"]), Path(p["image_b"])
    sub = _submitted(job, spec, outputs_dir)
    return {
        "seam_version": __version__,
        "job_id": job.id,
        "parent_id": job.parent_id,
        "status": job.status,
        "created_at": job.created_at,
        "finished_at": job.updated_at,
        "render_seconds": job.wall_s,
        "effect": p.get("preset"),
        "model": spec.model,
        "seed": p.get("seed"),
        "seconds": p.get("seconds"),
        "size": [x.get("width"), x.get("height")],
        "inputs": {"image_a": {"file": a.name, "sha256": sha256(a)}, "image_b": {"file": b.name, "sha256": sha256(b)}},
        "shots": {"a_desc": p.get("a_desc"), "b_desc": p.get("b_desc"), "a_ref": p.get("a_ref"), "b_ref": p.get("b_ref")},
        "prompt_extra": p.get("prompt_extra") or "",
        "prompt_sent": x.get("prompt"),  # what Seam sent; with the enhancer on, the graph rewrites it before encoding
        "prompt_enhancer": bool(p.get("enhance", True)) and "enhance_prompt" in spec.slots,
        "workflow": {"file": sub["workflow"], "sha256": sub["workflow_sha256"],
                     "archived_at_submit": sub["archived"]},
        "comfy_job_id": job.comfy_job_id,
        "output": {"file": job.output, "sha256": sha256(outputs_dir / job.output) if job.output else None,
                   "duration": x.get("duration"), "fps": x.get("fps"), "has_audio": x.get("has_audio")},
    }


def comfy_bundle(job: JobRecord, spec: WorkflowSpec, outputs_dir: Path) -> bytes:
    """Zip: workflow_api.json + input/<stills> + receipt.json + README.txt."""
    a, b = Path(job.params["image_a"]), Path(job.params["image_b"])
    a_name, b_name = f"seam_{job.id}_a.png", f"seam_{job.id}_b.png"
    graph = _submitted(job, spec, outputs_dir)["graph"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("workflow_api.json", json.dumps(graph, indent=2))
        z.write(a, f"input/{a_name}")
        z.write(b, f"input/{b_name}")
        z.writestr("receipt.json", json.dumps(build_receipt(job, spec, outputs_dir), indent=2))
        z.writestr("README.txt", BUNDLE_README.format(id=job.id, model=spec.model, seed=job.params.get("seed")))
    return buf.getvalue()
