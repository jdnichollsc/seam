"""What a shot generation renders: aspect and the model's quality to pixels and steps."""

from __future__ import annotations

import math

ASPECTS = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4, "21:9": 21 / 9}
MAX_COUNT = 4
MAX_PROMPT = 4000  # the longest any model takes; each has its own, in slots.json


def plan(aspect: str, quality: str, count: int, qualities: tuple[tuple[str, float, int], ...]) -> tuple[int, int, int]:
    """(width, height, steps), multiples of 16 as the latent needs. Raises ValueError on anything the form doesn't offer."""
    if aspect not in ASPECTS:
        raise ValueError(f"aspect must be one of {', '.join(ASPECTS)}")
    sizes = {name: (megapixels, steps) for name, megapixels, steps in qualities}
    if quality not in sizes:
        raise ValueError(f"quality must be one of {', '.join(sizes)}")
    if not 1 <= count <= MAX_COUNT:
        raise ValueError(f"count must be 1 to {MAX_COUNT}")
    megapixels, steps = sizes[quality]
    width = math.sqrt(megapixels * 1e6 * ASPECTS[aspect])
    return round(width / 16) * 16, round(width / ASPECTS[aspect] / 16) * 16, steps
