from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from seam.config import Settings
from seam.doctor import model_files, required_workflows
from seam.presets import load_presets
from seam.workflows import load_spec

S = Settings(data_dir=Path("."))


def test_every_loader_instance_is_checked() -> None:
    files = {f for _, _, f in model_files(load_spec(S.workflows_dir, "l25").graph())}
    # two VAELoaders and two CLIPLoaders in one graph: a class → file map used to miss the second of each
    assert {"ltx-2.5-video-vae-bf16.safetensors", "ltx-2.5-audio-vae-bf16.safetensors",
            "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
            "gemma4_e2b_it_int8_convrot.safetensors"} <= files
    assert not any(f.endswith(".png") for f in files)  # image placeholders are not models


def test_required_workflows_follow_the_launch_presets() -> None:
    presets = load_presets(S.presets_file)
    assert required_workflows(presets, "l25", ("launch",)) == {"l25", "describe"}  # fire (l23) is deferred
    presets["fire"] = replace(presets["fire"], status="launch")
    assert required_workflows(presets, "l25", ("launch",)) == {"l23", "l25", "describe"}


def _fake_comfy(installed: set[str]):
    """object_info that lists `installed` for every loader input, in ComfyUI's legacy combo form."""
    fields = ("unet_name", "vae_name", "clip_name", "ckpt_name", "lora_name")

    def get(url: str) -> dict:
        if "/object_info/" not in url:
            return {}  # health endpoints
        node = url.rsplit("/", 1)[1]
        return {node: {"input": {"required": {f: [sorted(installed)] for f in fields}}}}
    return get


def _run(monkeypatch, tmp_path: Path, installed: set[str]) -> int:
    from seam import doctor, media

    (tmp_path / "static" / "web").mkdir(parents=True)
    (tmp_path / "static" / "web" / "index.html").write_text("ok")
    monkeypatch.setattr(doctor, "PACKAGE_DIR", tmp_path)
    monkeypatch.setattr(doctor, "_get", _fake_comfy(installed))
    monkeypatch.setattr(media, "_ensure_ffmpeg", lambda: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda _: "ffprobe")
    monkeypatch.delenv("SEAM_SHOW_CANDIDATES", raising=False)
    return doctor.run_doctor()


def _files(*models: str) -> set[str]:
    return {f for m in models for _, _, f in model_files(load_spec(S.workflows_dir, m).graph())}


def test_doctor_is_ready_without_the_optional_ltx23(monkeypatch, tmp_path: Path, capsys) -> None:
    assert _run(monkeypatch, tmp_path, _files("l25", "describe")) == 0
    assert "l23: optional" in capsys.readouterr().out


def test_doctor_fails_when_a_second_loader_file_is_missing(monkeypatch, tmp_path: Path, capsys) -> None:
    installed = _files("l25", "describe") - {"ltx-2.5-audio-vae-bf16.safetensors"}
    assert _run(monkeypatch, tmp_path, installed) == 1
    assert "FAIL l25: ltx-2.5-audio-vae-bf16.safetensors" in capsys.readouterr().out


def test_choices_reads_both_combo_forms() -> None:
    from seam.doctor import _choices

    legacy = {"VAELoader": {"input": {"required": {"vae_name": [["a.safetensors"], {}]}}}}
    v3 = {"VAELoader": {"input": {"required": {"vae_name": ["COMBO", {"options": ["a.safetensors"]}]}}}}
    assert _choices(legacy, "VAELoader", "vae_name") == _choices(v3, "VAELoader", "vae_name") == ["a.safetensors"]
