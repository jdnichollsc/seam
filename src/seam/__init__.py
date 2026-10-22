"""Seam — open-source AI transitions on ComfyUI (Comfy SDK + open-weights video models)."""

__version__ = "0.1.0"


def main() -> None:
    from .cli import main as cli_main

    raise SystemExit(cli_main())
