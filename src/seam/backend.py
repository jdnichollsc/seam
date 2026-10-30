"""Where graphs render: this machine's ComfyUI (the default) or the user's Comfy Cloud account.

The Seam server, its jobs and its joins stay on this machine either way. The Cloud key lives in <data_dir>/cloud.json
or COMFY_API_KEY, and leaves this module only as the SDK client's credential."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from comfy_sdk import Comfy

from .config import Settings

LOCAL = "local"
CLOUD = "cloud"
BACKENDS = (LOCAL, CLOUD)
CLOUD_URL = "https://cloud.comfy.org"
KEYS_URL = "https://platform.comfy.org/profile/api-keys"
_env_lock = threading.Lock()  # the SDK reads its endpoint from the process environment at construction


class InvalidKey(ValueError):
    """Comfy Cloud refused the key, or the key could not be checked."""


def _file(settings: Settings) -> Path:
    return settings.data_dir / "cloud.json"


def _read(settings: Settings) -> dict[str, Any]:
    try:
        data = json.loads(_file(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(settings: Settings, data: dict[str, Any]) -> None:
    settings.ensure_dirs()
    path = _file(settings)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # Windows: the user-profile ACL protects it
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def is_cloud_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() == urlsplit(CLOUD_URL).hostname


def current(settings: Settings) -> tuple[str, str]:
    """(backend, what decided it). The environment wins over the saved choice."""
    env = os.environ.get("SEAM_BACKEND", "").strip().lower()
    if env:
        if env not in BACKENDS:
            raise ValueError(f"SEAM_BACKEND must be one of {BACKENDS}, got {env!r}")
        return env, "SEAM_BACKEND"
    if is_cloud_url(settings.comfy_base_url):
        return CLOUD, "COMFY_BASE_URL"
    saved = _read(settings).get("backend")
    return (saved, "saved") if saved in BACKENDS else (LOCAL, "default")


def cloud_key(settings: Settings) -> tuple[str | None, str | None]:
    """(key, where it came from). Never put the first item in a response, a log, a job or a receipt."""
    saved = str(_read(settings).get("api_key") or "").strip()
    if saved:
        return saved, "saved"
    env = os.environ.get("COMFY_API_KEY", "").strip()
    return (env, "COMFY_API_KEY") if env else (None, None)


def state(settings: Settings) -> dict[str, Any]:
    backend, source = current(settings)
    _, key_source = cloud_key(settings)
    return {"backend": backend, "source": source, "keys_url": KEYS_URL,
            "cloud": {"key_configured": key_source is not None, "key_source": key_source}}


def locked_by_env(settings: Settings) -> str | None:
    """The variable that decides the backend, when one does: a saved choice would have no effect."""
    _, source = current(settings)
    return source if source in ("SEAM_BACKEND", "COMFY_BASE_URL") else None


def choose(settings: Settings, backend: str) -> None:
    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {BACKENDS}")
    if backend == CLOUD and cloud_key(settings)[0] is None:
        raise ValueError("add a Comfy Cloud API key first")
    _write(settings, {**_read(settings), "backend": backend})


def save_key(settings: Settings, api_key: str) -> None:
    _write(settings, {**_read(settings), "api_key": api_key.strip()})


def clear_key(settings: Settings) -> None:
    """Forget the saved key; without any key left, rendering goes back to this machine."""
    data = {k: v for k, v in _read(settings).items() if k != "api_key"}
    if not os.environ.get("COMFY_API_KEY", "").strip():
        data.pop("backend", None)
    _write(settings, data)


def make_client(settings: Settings, backend: str) -> Comfy:
    """A client for one backend. Cloud raises ValueError without a key; a Cloud key never goes to a local proxy."""
    if backend == CLOUD:
        key, _ = cloud_key(settings)
        if key is None:
            raise ValueError("Comfy Cloud needs an API key: add one in Seam, or run `seam backend --set-key`")
        return _build(CLOUD_URL, key, timeout=120.0)
    host = (urlsplit(settings.comfy_base_url).hostname or "").lower()
    hide_key = host in ("127.0.0.1", "localhost", "::1")
    return _build(settings.comfy_base_url, None, timeout=30.0, hide_key=hide_key)


def _build(url: str, key: str | None, timeout: float, hide_key: bool = False) -> Comfy:
    with _env_lock:
        before = {k: os.environ.get(k) for k in ("COMFY_BASE_URL", "COMFY_API_KEY")}
        os.environ["COMFY_BASE_URL"] = url
        if hide_key:
            os.environ.pop("COMFY_API_KEY", None)
        try:
            return Comfy(api_key=key, timeout=timeout, client_info="seam/0.1")
        finally:
            for k, v in before.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def verify_key(api_key: str) -> None:
    """A free authenticated read (does this asset exist?). Raises InvalidKey; spends nothing, uploads nothing."""
    client = _build(CLOUD_URL, api_key.strip(), timeout=30.0)
    try:
        client._low.head_asset_by_hash("blake3:" + "0" * 64)
    except Exception as e:  # noqa: BLE001 — the message must not depend on the key
        status = getattr(e, "http_status", None) or getattr(e, "status_code", None) or getattr(e, "status", None)
        if status in (401, 403):
            raise InvalidKey("Comfy Cloud refused this API key") from None
        raise InvalidKey(f"could not check the key with Comfy Cloud ({type(e).__name__})") from None
    finally:
        client.close()
