"""API-format workflow + slot map → a parameterised comfy-sdk Workflow.

Slots are traced from the graph once and stored in workflows/slots.json;
runtime code only ever touches the graph through those named slots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from comfy_sdk import Comfy
from comfy_sdk.workflows import Workflow


@dataclass(frozen=True)
class WorkflowSpec:
    model: str
    workflow_file: Path
    output_node: str
    fps: int
    slots: dict[str, tuple[str, str]]

    def graph(self) -> dict[str, Any]:
        return json.loads(self.workflow_file.read_text(encoding="utf-8"))


def load_spec(workflows_dir: Path, model: str) -> WorkflowSpec:
    data = json.loads((workflows_dir / "slots.json").read_text(encoding="utf-8"))[model]
    return WorkflowSpec(model=model, workflow_file=workflows_dir / data["workflow"], output_node=data["output_node"],
                        fps=data.get("fps", 0), slots={k: (v[0], v[1]) for k, v in data["slots"].items()})


def missing_slots(spec: WorkflowSpec) -> list[str]:
    """Slots whose node/input no longer exists in the graph (template drift)."""
    graph = spec.graph()
    return [name for name, (node, field) in spec.slots.items()
            if node not in graph or field not in graph[node].get("inputs", {})]


def export_graph(spec: WorkflowSpec, image_a: str, image_b: str, params: dict[str, Any]) -> dict[str, Any]:
    """The API graph a job ran, filled through the same slots, with the two stills referenced by file name
    (ComfyUI's input folder) instead of SDK uploads: opens and re-runs in plain ComfyUI."""
    graph = spec.graph()
    for name, value in {"image_a": image_a, "image_b": image_b, **params}.items():
        node, field = spec.slots[name]
        graph[node]["inputs"][field] = value
    return graph


def build(client: Comfy, spec: WorkflowSpec, image_a: Path, image_b: Path, params: dict[str, Any]) -> Workflow:
    unknown = set(params) - set(spec.slots)
    if unknown:
        raise KeyError(f"unknown slots for {spec.model}: {sorted(unknown)}")
    wf = client.workflows.from_file(spec.workflow_file)
    wf.set_input(*spec.slots["image_a"], client.assets.from_file(image_a))
    wf.set_input(*spec.slots["image_b"], client.assets.from_file(image_b))
    for name, value in params.items():
        wf.set_input(*spec.slots[name], value)
    return wf
