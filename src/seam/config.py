"""Runtime settings. Everything is overridable by environment variables; nothing secret is stored here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    # Comfy API v2 endpoint used by comfy-sdk: the local comfy-api-proxy by default,
    # or Comfy Cloud / a deployed endpoint (then COMFY_API_KEY is read by the SDK itself).
    comfy_base_url: str = field(default_factory=lambda: os.environ.get("COMFY_BASE_URL", "http://127.0.0.1:8189"))
    model: str = field(default_factory=lambda: os.environ.get("SEAM_MODEL", "l25"))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("SEAM_DATA_DIR", Path.home() / ".seam")))
    host: str = field(default_factory=lambda: os.environ.get("SEAM_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("SEAM_PORT", "7860")))
    job_timeout_s: float = field(default_factory=lambda: float(os.environ.get("SEAM_JOB_TIMEOUT", "900")))
    presets_file: Path = PACKAGE_DIR / "presets" / "presets.yaml"
    workflows_dir: Path = PACKAGE_DIR / "workflows"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "seam.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.outputs_dir, self.uploads_dir):
            d.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
