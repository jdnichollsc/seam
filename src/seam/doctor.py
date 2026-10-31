"""`seam doctor`: verify a machine is ready to run Seam, with a fix hint for every failed check."""

from __future__ import annotations

import json
import os
import shutil
import urllib.request

from . import backend as backends
from .config import PACKAGE_DIR, get_settings
from .presets import load_presets
from .workflows import load_spec, missing_slots

MODEL_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf")
DOWNLOAD_SET = {"l25": "l25", "describe": "l25", "l23": "l23"}  # workflow → scripts/download_models.py --only <set>


def model_files(graph: dict) -> list[tuple[str, str, str]]:
    """Every model file a workflow loads, per node instance: (class_type, input, filename).

    Same rule ComfyUI applies at submit (value_not_in_list per node input), so two loaders of one class
    (LTX-2.5's video + audio VAE) are both checked."""
    return [(n["class_type"], k, v) for n in graph.values() for k, v in n.get("inputs", {}).items()
            if isinstance(v, str) and v.lower().endswith(MODEL_EXT)]


def required_workflows(presets, default_model: str, statuses: tuple[str, ...]) -> set[str]:
    """Workflows the exposed presets render with, plus the shot reader every job uses."""
    return {p.model or default_model for p in presets.values() if p.status in statuses} | {"describe"}


def _choices(info: dict | None, node: str, field: str) -> list:
    spec = {**info.get(node, {}).get("input", {}).get("required", {}),
            **info.get(node, {}).get("input", {}).get("optional", {})}.get(field) if info else None
    if isinstance(spec, list) and spec and isinstance(spec[0], list):
        return spec[0]
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
        return spec[1].get("options", [])
    return []


def _get(url: str, headers: dict[str, str] | None = None) -> dict | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except Exception:
        return None


def run_doctor() -> int:
    s = get_settings()
    ok = True

    def check(cond: bool, label: str, fix: str = "") -> None:
        nonlocal ok
        ok &= cond
        print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f"\n       fix: {fix}"))

    print("Seam doctor")
    from . import media
    try:
        media._ensure_ffmpeg()
        check(bool(shutil.which("ffprobe")), "ffmpeg + ffprobe on PATH", "install ffmpeg (see install.md)")
    except RuntimeError:
        check(False, "ffmpeg + ffprobe on PATH", "install ffmpeg (see install.md)")

    backend = backends.current(s)[0]
    print(f"  info rendering on {'Comfy Cloud' if backend == backends.CLOUD else 'this machine'}")
    from .engine import Engine

    presets = load_presets(s.presets_file)
    if backend == backends.CLOUD:
        _check_cloud(s, presets, check)
    else:
        check(_get(f"{s.comfy_base_url}/api/v2/health") is not None, f"Comfy API v2 reachable at {s.comfy_base_url}",
              "start ComfyUI, then `comfy-api-proxy start` (or `seam backend cloud` for Comfy Cloud)")
        _check_comfyui(s, presets, check)

    needed = required_workflows(presets, s.model, Engine.exposed_statuses())
    for model in sorted(needed):
        check(not missing_slots(load_spec(s.workflows_dir, model)), f"{model}: workflow slot map matches the graph",
              "reinstall the package")
    launch = [p.id for p in presets.values() if p.status == "launch"]
    check(len(launch) >= 3, f"{len(presets)} presets loaded, {len(launch)} qualified for launch (need 3+)", "reinstall")
    check((PACKAGE_DIR / "static" / "web" / "index.html").exists(), "web app built",
          "cd web && npm install && npm run build")
    print("ready" if ok else "not ready - fix the items above")
    return 0 if ok else 1


def _check_cloud(s, presets, check) -> None:
    """Key, then the graphs' nodes and model files, as far as Comfy Cloud lets an API key look."""
    from .engine import Engine

    key, _ = backends.cloud_key(s)
    check(key is not None, "Comfy Cloud API key set", f"create one at {backends.KEYS_URL}, then `seam backend --set-key`")
    if key is None:
        return
    try:
        backends.verify_key(key)
        check(True, "Comfy Cloud accepts the key")
    except backends.InvalidKey as e:
        check(False, "Comfy Cloud accepts the key", f"{e}: `seam backend --set-key`")
        return
    from .models import object_info

    info = object_info(s, backends.CLOUD, refresh=True)
    if info is None:  # not a pass: we simply could not look
        print("  info could not read Comfy Cloud's node list; nodes and models not checked")
        return
    needed = required_workflows(presets, s.model, Engine.exposed_statuses())
    graphs = [load_spec(s.workflows_dir, m).graph() for m in sorted(needed)]
    for cls in sorted({n["class_type"] for g in graphs for n in g.values()}):
        check(cls in info, f"Comfy Cloud has the node {cls}", "Comfy Cloud does not offer it yet; render locally")
    for g in graphs:
        for node, field, filename in model_files(g):
            check(filename in _choices(info, node, field), f"Comfy Cloud has {filename}",
                  "Comfy Cloud does not offer it yet; render locally")


def _check_comfyui(s, presets, check) -> None:
    from .engine import Engine

    comfyui = os.environ.get("SEAM_COMFYUI_URL", "http://127.0.0.1:8188")
    needed = required_workflows(presets, s.model, Engine.exposed_statuses())
    workflows = sorted(json.loads((s.workflows_dir / "slots.json").read_text(encoding="utf-8")).keys() - {"_doc"})
    graphs = {m: load_spec(s.workflows_dir, m).graph() for m in workflows}
    info: dict[str, dict | None] = {}
    for cls in sorted({n["class_type"] for m in needed for n in graphs[m].values()}):
        info[cls] = _get(f"{comfyui}/object_info/{cls}")
        if info[cls] is None:  # nothing answers: one clear failure instead of a download hint per file
            check(False, f"ComfyUI reachable at {comfyui}", "start ComfyUI (or set SEAM_COMFYUI_URL)")
            break
    else:
        nodes_ok = {cls: cls in (i or {}) for cls, i in info.items()}
        for cls in sorted(c for c, present in nodes_ok.items() if not present):
            check(False, f"ComfyUI has the core node {cls}", "update ComfyUI to >= 0.41.3 (Desktop: update the app)")
        for model in workflows:
            missing = []
            for node, field, filename in model_files(graphs[model]):
                if model in needed and not nodes_ok.get(node, False):
                    continue  # already reported as a missing node; a download would not fix it
                if node not in info:
                    info[node] = _get(f"{comfyui}/object_info/{node}")
                present = filename in _choices(info[node], node, field)
                if model in needed:
                    check(present, f"{model}: {filename}",
                          f"run `python scripts/download_models.py --models-dir <ComfyUI>/models --only "
                          f"{DOWNLOAD_SET.get(model, model)}`")
                elif not present:
                    missing.append(filename)
            if model not in needed:
                print(f"  info {model}: optional - not used by the launch effects"
                      + (f" ({len(missing)} file(s) not installed)" if missing else " (installed)"))
