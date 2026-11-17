"""Swap the person in one shot: the bundled character-swap graph, filled in for one run.

The graph (workflows/character_swap.api.json, see character_swap.NOTICE.md) is never edited on disk; `fill` returns a copy with
this run's inputs. Its nodes come from the h3_character_swap pack the user installs in their own ComfyUI; the sizes
and limits below follow that pack at the commit named in PACK_COMMIT.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GRAPH = Path(__file__).parent / "workflows" / "character_swap.api.json"
GRAPH_SHA256 = "4db7670a58574e0c2def6f48dad3e00f7a5dda8ba37501bb92d5361ab47552b0"
PACK_REPO = "https://github.com/akatz-labs/h3-character-swap-tool"
PACK_COMMIT = "0670e4fdf95adf2262caa4b81afed2a9c4ae4b7b"

FPS = 24  # the pack prepares every input at 24 fps
MAX_SECONDS = 10.0  # Seam's guard, not the pack's limit (3592 frames): the longest run known to fit in ~32 GB
MIN_SECONDS = 1.0  # the shortest clip a sequence takes (media.LONG_CLIP_SECONDS)
TIERS = {"480": 500_000, "768": 1_000_000}  # pixels the pack works with at each resolution
DEFAULT_TIER = "768"
DEFAULT_SEED = 904239
DEFAULT_EXPAND = 64

# node id -> class the filler writes to; anything else means this is not the bundled graph
_NODES = {"30": "LoadVideo", "9": "LoadImage", "trim": "Video Slice", "31": "H3CharacterSwapPrepareVideo",
          "58": "SelectCLIPDevice", "101": "CLIPTextEncode", "103": "SAM3_Detect",
          "104": "H3CharacterSwapSelectSubject", "105": "SAM3_VideoTrack", "106": "SAM3_TrackToMask",
          "107": "H3CharacterSwapRecoverMasks", "110": "H3CharacterSwapExpandMask", "801": "H3CharacterSwapSettings",
          "803": "H3CharacterSwapShot", "807": "H3CharacterSwapAssemble", "809": "SaveVideo"}
MASK_IMAGE, MASK_SAVE = "seam_mask_image", "seam_mask_save"  # added by fill: the grown mask, saved for the timing check


def load_graph() -> dict[str, Any]:
    raw = GRAPH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != GRAPH_SHA256:
        raise RuntimeError(f"{GRAPH.name} is not the bundled swap graph (its SHA-256 changed)")
    return json.loads(raw)


def canvas(tier: str, width: int, height: int) -> tuple[int, int]:
    """The size the pack prepares a `width`x`height` input at: kept when it fits the tier's pixels on a 32-pixel
    grid, otherwise the 32-pixel size within the budget whose sides are closest, in ratio, to the ideal ones."""
    budget = TIERS[tier]
    if width <= 0 or height <= 0:
        raise ValueError("the frame has no size")
    if width * height <= budget and width % 32 == 0 and height % 32 == 0:
        return width, height
    ratio = width / height
    ideal_w, ideal_h = math.sqrt(budget * ratio), math.sqrt(budget / ratio)
    candidates = []
    for w in range(32, min(16384, budget // 32) + 1, 32):
        top = min(16384, (budget // w) // 32 * 32)
        for h in {max(32, min(top, math.floor(ideal_h / 32) * 32)), max(32, min(top, math.ceil(ideal_h / 32) * 32))}:
            candidates.append((math.log(w / ideal_w) ** 2 + math.log(h / ideal_h) ** 2, -(w * h), w, h))
    _, _, w, h = min(candidates)
    return w, h


def seen_area(width: int, height: int, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
    """`(x, y, w, h)`: the part of a `width`x`height` frame the model sees. The pack scales the frame to cover its
    canvas and crops the centre, so a frame of another shape loses its sides or its top and bottom."""
    even = lambda n: max(2, round(n / 2) * 2)
    src, dst = width / height, canvas_w / canvas_h
    if abs(src - dst) < 1e-4:
        return 0, 0, width, height
    if dst < src:
        w = min(width, even(height * dst))
        return round((width - w) / 2), 0, w, height
    h = min(height, even(width / dst))
    return 0, round((height - h) / 2), width, h


def prepared_point(x: float, y: float, width: int, height: int, tier: str) -> tuple[int, int]:
    """A point on the source frame, in the pixels of the frame the detector gets. Raises when it is in the part the
    centre crop cuts away."""
    cw, ch = canvas(tier, width, height)
    ax, ay, aw, ah = seen_area(width, height, cw, ch)
    px, py = round((x - ax) / aw * cw), round((y - ay) / ah * ch)
    if not (0 <= px < cw and 0 <= py < ch):
        raise ValueError("that point is outside the part of the frame the model sees")
    return px, py


def run_seconds(frames: int) -> float:
    """The length to ask for `frames` frames at 24 fps. The pack takes floor(seconds * 24) frames, so exactly
    frames / 24, written in decimals, can come out one short; half a frame of margin keeps the last one."""
    return round((frames + 0.5) / FPS, 5)


@dataclass(frozen=True)
class Options:
    video: str  # names in ComfyUI's input folder
    identity: str
    frames: int  # frames at 24 fps the run covers (after any 24-in-30 thinning)
    prefix: str  # output file names
    tier: str = DEFAULT_TIER
    seed: int = DEFAULT_SEED
    prompt: str | None = None
    point: tuple[int, int] | None = None  # in prepared-frame pixels (prepared_point)
    recover: bool = False  # re-detect where tracking lost the person: only safe with one person in the shot
    expand: int = DEFAULT_EXPAND
    start: float = 0.0


def fill(graph: dict[str, Any], o: Options) -> dict[str, Any]:
    """A copy of the bundled graph with this run's inputs. The graph is not touched."""
    w = copy.deepcopy(graph)
    for node, cls in _NODES.items():
        if w.get(node, {}).get("class_type") != cls:
            raise ValueError(f"node {node} is not {cls}: this is not the bundled swap graph")
    if o.tier not in TIERS:
        raise ValueError(f"tier is one of {', '.join(TIERS)}")
    if o.frames < 1 or o.frames > round(MAX_SECONDS * FPS):
        raise ValueError(f"a swap covers 1 to {round(MAX_SECONDS * FPS)} frames ({MAX_SECONDS:g} s); got {o.frames}")
    if o.start < 0:
        raise ValueError("start must not be negative")
    seconds = run_seconds(o.frames)
    w["30"]["inputs"]["file"] = o.video
    w["9"]["inputs"]["image"] = o.identity
    w["trim"]["inputs"].update(start_time=o.start, duration=seconds, strict_duration=False)
    w["31"]["inputs"].update(resolution=o.tier, duration_seconds=seconds)  # the pack takes the resolution as a string
    w["101"]["inputs"]["text"] = "person"
    w["104"]["inputs"].update(selection="largest", index=0)
    w["107"]["inputs"]["recover_empty_frames"] = bool(o.recover)
    if o.point is not None:
        x, y = o.point
        if not (isinstance(x, int) and isinstance(y, int) and x >= 0 and y >= 0):
            raise ValueError("point is whole, non-negative pixels")
        # one explicit target, with no text detection competing with it
        w["103"]["inputs"]["positive_coords"] = json.dumps([{"x": x, "y": y}])
        w["103"]["inputs"].pop("conditioning", None)
        w["104"]["inputs"].update(selection="index", index=0)
        w["105"]["inputs"].pop("conditioning", None)
        w["106"]["inputs"]["object_indices"] = "0"
    w["110"]["inputs"]["expansion_px"] = int(o.expand)
    w["801"]["inputs"]["seed"] = int(o.seed)
    if o.prompt:
        w["801"]["inputs"]["prompt"] = o.prompt
    # no anchor still in v1
    w.pop("anchor_image", None)
    w.pop("anchor_fit", None)
    w["803"]["inputs"].pop("anchor", None)
    w["803"]["inputs"]["use_anchor"] = False
    # a single-GPU ComfyUI offers "default" and "cpu" for the text encoder; the graph asks for "gpu:0"
    for node in w.values():
        if node["class_type"] == "SelectCLIPDevice" and node["inputs"].get("device") not in ("default", "cpu"):
            node["inputs"]["device"] = "default"
    w["803"]["inputs"]["shot_id"] = o.prefix
    w["807"]["inputs"]["filename_prefix"] = o.prefix
    w["809"]["inputs"]["filename_prefix"] = f"video/{o.prefix}"
    # the grown mask (110 output 0, a MASK) as images, so Seam can compare the picture outside the person
    w[MASK_IMAGE] = {"class_type": "MaskToImage", "inputs": {"mask": ["110", 0]}}
    w[MASK_SAVE] = {"class_type": "SaveImage", "inputs": {"images": [MASK_IMAGE, 0], "filename_prefix": f"{o.prefix}_mask"}}
    return w
