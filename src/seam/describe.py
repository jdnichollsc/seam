"""Read a shot: the local Gemma-4 E2B (the same model as LTX-2.5's prompt enhancer) writes the description and
short label that the prompt templates take, so every prompt has the shape the presets were qualified with.
Core ComfyUI nodes, through the Comfy SDK; about a second per shot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from comfy_sdk import Comfy

from .workflows import WorkflowSpec

VERSION = "1"  # part of the cache key: bump when ASK or describe.api.json changes

ASK = ("Describe the main subject of this image for a video editor. Answer with exactly two lines and nothing else.\n"
       "Line 1: a noun phrase of at most 30 words that starts with 'a' or 'an' and names the subject first, then "
       "(for a person) their expression, hair and clothes, then the place and the light. Do not use verbs like "
       "'stands', 'wears' or 'rests'.\n"
       "Line 2: a short name for the subject, 3 to 7 words, starting with 'the'.\n"
       "Example for a different image:\n"
       "an old man with a calm smile, a grey beard and a yellow raincoat on a foggy harbor pier at dawn\n"
       "the old man in the yellow raincoat\n"
       "Example for another image:\n"
       "a red ceramic teapot on a wooden kitchen table in soft morning window light\n"
       "the red teapot")


@dataclass(frozen=True)
class ShotText:
    desc: str  # "a young woman with … on a … at night" → "The video opens on {desc}."
    ref: str   # "the woman in the red jacket" → inside the effect text; "" when the model gave no usable label


def _clean(line: str) -> str:
    line = re.sub(r"^\s*(?:line\s*\d|description|label)\s*[:.)-]\s*", "", line, flags=re.I)
    return line.strip().strip("\"'`*").rstrip(".").strip()


def parse(text: str) -> ShotText | None:
    """Two lines → ShotText. None when the description is not the noun phrase the templates need."""
    lines = [c for c in (_clean(x) for x in text.splitlines()) if c]
    if not lines:
        return None
    desc = lines[0][0].lower() + lines[0][1:]
    if not re.match(r"^an? ", desc) or not 3 <= len(desc.split()) <= 40 or re.search(r"[.!?]\s", desc):
        return None  # one phrase only: "The video opens on {desc}."
    ref = lines[1][0].lower() + lines[1][1:] if len(lines) > 1 else ""
    if not re.match(r"^the ", ref) or not 2 <= len(ref.split()) <= 10:
        ref = ""
    return ShotText(desc=desc, ref=ref)


def run(client: Comfy, spec: WorkflowSpec, image: Path, timeout_s: float = 180) -> ShotText | None:
    wf = client.workflows.from_file(spec.workflow_file)
    wf.set_input(*spec.slots["image"], client.assets.from_file(image))
    wf.set_input(*spec.slots["prompt"], ASK)
    job = client.submit(wf).wait(timeout=timeout_s)
    if job.status != "succeeded":
        raise RuntimeError(f"describe job {job.id} {job.status}: {job.error}")
    outputs = job.get_outputs(spec.output_node)
    return parse(outputs[0].to_bytes().decode("utf-8", "replace")) if outputs else None
