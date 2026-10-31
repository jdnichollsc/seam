"""Which catalog models can render here: read live from the backend's node list, never assumed, never downloaded."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

from . import backend as backends
from .config import Settings
from .doctor import _choices, model_files
from .workflows import WorkflowSpec

CACHE_S = 300.0
_cache: dict[tuple[str, str | None], tuple[float, dict[str, Any] | None]] = {}
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
    info = _fetch(settings, backend)
    with _lock:
        _cache[key] = (time.monotonic(), info)
    return info


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


def listing(settings: Settings, catalog: dict[str, WorkflowSpec], backend: str, refresh: bool = False) -> list[dict[str, Any]]:
    info = object_info(settings, backend, refresh)
    out = []
    for spec in catalog.values():
        available, reason = why_not(spec, backend, info)
        out.append({"id": spec.model, "label": spec.label, "kind": spec.kind, "seconds": list(spec.seconds),
                    "available": available, "reason": reason})
    return out


def known_refusal(settings: Settings, spec: WorkflowSpec, backend: str) -> str | None:
    """Why this model cannot render, from what is already known. Never fetches: validation runs under the sequence
    lock, and the render reports the real error when nothing is known yet."""
    with _lock:
        hit = _cache.get(_cache_key(settings, backend))
    fresh = hit and time.monotonic() - hit[0] < CACHE_S  # an old list may predate what was installed since
    available, reason = why_not(spec, backend, hit[1] if fresh else None)
    return reason if available is False else None
