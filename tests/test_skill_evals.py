from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "skill" / "cases.json"
SKILL = ROOT / "skills" / "seam" / "SKILL.md"
PRESETS = ROOT / "src" / "seam" / "presets" / "presets.yaml"

# Effects the skill must treat as unavailable; cases may mention them on purpose.
INTENTIONALLY_UNLISTED = {"fire"}
ANCHORS = ["seam_status", "list_presets", "prompt_extra", "row 2", "middle", "give it a listen", "different preset", "create_sequence", "refine_segment"]


def _cases() -> list[dict]:
    return json.loads(CASES.read_text(encoding="utf-8"))


def test_cases_structure():
    cases = _cases()
    assert len(cases) >= 7
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for c in cases:
        assert set(c) == {"id", "user_request", "situation", "must", "must_not"}, c["id"]
        for key in ("id", "user_request", "situation"):
            assert isinstance(c[key], str) and c[key].strip(), (c["id"], key)
        assert isinstance(c["must"], list) and c["must"], c["id"]
        assert isinstance(c["must_not"], list), c["id"]
        for rx in c["must"] + c["must_not"]:
            re.compile(rx)


def test_preset_ids_in_situations_exist():
    known = {p["id"] for p in yaml.safe_load(PRESETS.read_text(encoding="utf-8"))["presets"]}
    for c in _cases():
        for pid in re.findall(r"`([a-z_]+)`", c["situation"]):
            assert pid in known or pid in INTENTIONALLY_UNLISTED, (c["id"], pid)


def test_skill_has_anchors():
    text = SKILL.read_text(encoding="utf-8").lower()
    for anchor in ANCHORS:
        assert anchor in text, f"SKILL.md lost anchor: {anchor}"
