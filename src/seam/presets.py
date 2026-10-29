"""Transition presets: data in presets.yaml, rendered into a model-specific prompt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    category: str
    path: str  # what happens between the frames; {a} / {b} are short references to the two images
    camera: str
    sound: str
    model: str | None = None            # preferred model for this effect (None = settings default)
    status: str = "candidate"           # launch | candidate | deferred | trial (see presets.yaml)
    seconds: tuple[int, ...] = (5,)     # take lengths this effect is qualified at (launch presets only offer these)


def load_presets(path: Path) -> dict[str, Preset]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    out = {}
    for p in data["presets"]:
        fields = {k: p[k] for k in Preset.__dataclass_fields__ if k in p}
        if "seconds" in fields:
            fields["seconds"] = tuple(int(s) for s in fields["seconds"])
        out[p["id"]] = Preset(**fields)
    return out


def timing_text(seconds: int) -> str:
    """Takes longer than 5 s hold the effect to a ~3 s window mid-clip; without it the model stretches the effect
    over the whole take (bake-off round 10/28)."""
    if seconds <= 5:
        return ""
    lead = (seconds - 3) / 2
    return (f" For the first {lead:g} seconds the opening scene plays on with gentle natural motion. The transition "
            f"happens between {lead:g} and {lead + 3:g} seconds. For the last {lead:g} seconds the end scene plays on "
            "with gentle natural motion and settles on the final frame.")


def render_prompt(model: str, preset: Preset, a_ref: str = "the first scene", b_ref: str = "the second scene",
                  a_desc: str | None = None, b_desc: str | None = None, extra: str = "", seconds: int = 5) -> str:
    """Model-specific prompt. a_desc/b_desc are optional full descriptions (e.g. written by an agent)."""
    path = preset.path.format(a=a_ref, b=b_ref)
    path = path[0].upper() + path[1:]
    extra = f" {extra.strip()}" if extra.strip() else ""
    if model == "l25":
        opening = (f" The video opens on {a_desc}." if a_desc else "") + timing_text(seconds)
        ending = f" The video ends on {b_desc}." if b_desc else " The video ends exactly on the end image."
        return ("Use the provided start image as the first frame and the provided end image as the final frame "
                f"anchor.{opening} {path} Camera: {preset.camera}. The sound is {preset.sound}.{extra}{ending}")
    if model == "l23":
        opening = f"{a_desc[0].upper() + a_desc[1:]}. " if a_desc else ""
        return f"{opening}{path} Camera: {preset.camera}.{extra} One smooth continuous seamless transition, no cuts. zhuanchang"
    raise ValueError(f"no prompt renderer for model {model!r}")
