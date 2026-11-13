"""Which catalog models can render here: read live from the backend's node list, never assumed, never downloaded."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import backend as backends
from .config import Settings
from .doctor import _choices, model_files
from .workflows import WorkflowSpec

CACHE_S = 300.0
_cache: dict[tuple[str, str | None], tuple[float, dict[str, Any] | None]] = {}
_warming: set[tuple[str, str | None]] = set()
_lock = threading.Lock()


def _cache_key(settings: Settings, backend: str) -> tuple[str, str | None]:
    if backend == backends.CLOUD:
        return backend, backends.cloud_key(settings)[0]
    return backend, os.environ.get("SEAM_COMFYUI_URL", "http://127.0.0.1:8188")


def _fetch(settings: Settings, backend: str) -> dict[str, Any] | None:
    if backend == backends.CLOUD:
        key = backends.cloud_key(settings)[0]
        if not key:
            return None
        url, headers = f"{backends.CLOUD_URL}/api/object_info", {"X-API-Key": key, "User-Agent": "seam/0.1"}
    else:
        url, headers = f"{_cache_key(settings, backend)[1]}/object_info", {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
            return json.load(r)
    except Exception:  # noqa: BLE001 — unreadable means "not checked", which the listing says
        return None


def object_info(settings: Settings, backend: str, refresh: bool = False) -> dict[str, Any] | None:
    """The backend's node list, cached: on Cloud it is about 10 MB."""
    key = _cache_key(settings, backend)
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and time.monotonic() - hit[0] < CACHE_S:
        return hit[1]
    return _read(settings, backend)


def _read(settings: Settings, backend: str) -> dict[str, Any] | None:
    info = _fetch(settings, backend)
    with _lock:
        _cache[_cache_key(settings, backend)] = (time.monotonic(), info)
    return info


def info_or_warm(settings: Settings, backend: str) -> tuple[dict[str, Any] | None, bool]:
    """(node list, still being read). Never waits: Comfy Cloud's list can take a minute, so an unknown one is read
    on a thread and the caller asks again."""
    key = _cache_key(settings, backend)

    def warm() -> None:
        try:
            _read(settings, backend)
        except Exception:  # noqa: BLE001 — an unread list is remembered as unknown, so the panel stops asking
            with _lock:
                _cache[key] = (time.monotonic(), None)
        finally:
            with _lock:
                _warming.discard(key)

    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1], False
        if key not in _warming:
            _warming.add(key)
            threading.Thread(target=warm, daemon=True).start()
    return None, True


def comfy_models_dir() -> Path | None:
    """The local ComfyUI's own `models` folder, asked from it: None when it is not running on this machine or does
    not say. Read-only; `SEAM_MODELS_DIR` wins over it."""
    url = os.environ.get("SEAM_COMFYUI_URL", "http://127.0.0.1:8188")
    if (urlsplit(url).hostname or "").lower() not in ("127.0.0.1", "localhost", "::1"):
        return None  # another machine's path means nothing here
    try:
        with urllib.request.urlopen(f"{url}/internal/folder_paths", timeout=3) as r:
            folders = json.load(r)
        for name in ("checkpoints", "diffusion_models", "loras", "vae"):
            for p in folders.get(name) or []:
                root = Path(p).parent
                if root.name == "models" and root.is_dir():
                    return root
    except Exception:  # noqa: BLE001 — not running, or an older ComfyUI: the panel then asks for SEAM_MODELS_DIR
        pass
    return None


def why_not(spec: WorkflowSpec, backend: str, info: dict[str, Any] | None) -> tuple[bool | None, str]:
    """(available, reason). None: the node list could not be read, so nothing is known."""
    if backend not in spec.backends:
        return False, "needs Comfy Cloud" if backends.CLOUD in spec.backends else "not on Comfy Cloud"
    if info is None:
        return None, "not checked"
    graph = spec.graph()
    if any(n["class_type"] not in info for n in graph.values()):
        return False, "ComfyUI does not have its nodes" if backend == backends.LOCAL else "not offered by Comfy Cloud"
    missing = [f for node, field, f in model_files(graph) if f not in _choices(info, node, field)]
    if missing:
        return False, "model files not installed" if backend == backends.LOCAL else "model files not on Comfy Cloud"
    return True, ""


def state(spec: WorkflowSpec, backend: str, info: dict[str, Any] | None) -> tuple[str, str]:
    """(state, reason) for the Models panel: ready | missing (files) | needs_nodes (no download fixes it) |
    not_offered | not_checked."""
    available, reason = why_not(spec, backend, info)
    if available:
        return "ready", ""
    if available is None:
        return "not_checked", reason
    if reason == "model files not installed":
        return "missing", reason
    if reason == "ComfyUI does not have its nodes":
        return "needs_nodes", "update ComfyUI or install the node pack; downloading weights does not fix it"
    return "not_offered", reason


def cached_info(settings: Settings, backend: str) -> dict[str, Any] | None:
    with _lock:
        hit = _cache.get(_cache_key(settings, backend))
    return hit[1] if hit and time.monotonic() - hit[0] < CACHE_S else None


def listing(settings: Settings, catalog: dict[str, WorkflowSpec], backend: str, refresh: bool = False) -> list[dict[str, Any]]:
    info = object_info(settings, backend, refresh)
    other = backends.CLOUD if backend == backends.LOCAL else backends.LOCAL
    other_info = cached_info(settings, other)
    out = []
    for spec in catalog.values():
        available, reason = why_not(spec, backend, info)
        elsewhere = why_not(spec, other, other_info)[0] if available is False else None
        out.append({"id": spec.model, "label": spec.label, "kind": spec.kind, "seconds": list(spec.seconds),
                    "available": available, "reason": reason,
                    "ready_elsewhere": ("Comfy Cloud" if other == backends.CLOUD else "your GPU") if elsewhere else None})
    return out


def known_refusal(settings: Settings, spec: WorkflowSpec, backend: str) -> str | None:
    """Why this model cannot render, from what is already known. Never fetches: validation runs under the sequence
    lock, and the render reports the real error when nothing is known yet."""
    with _lock:
        hit = _cache.get(_cache_key(settings, backend))
    fresh = hit and time.monotonic() - hit[0] < CACHE_S  # an old list may predate what was installed since
    available, reason = why_not(spec, backend, hit[1] if fresh else None)
    return reason if available is False else None
