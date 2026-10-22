# Seam

**Two shots in, one transition out.** Seam makes a short video that travels from a first image to a last one
through an effect — smoke, a water bend, a whip pan — rendered on your own GPU with open-weights models.
No accounts, no credits.

It is an open-source take on the popular "transitions" apps, built for the
[Open Source Higgsfield](https://comfy.org/open-source-higgsfield) challenge on the Comfy Developer Platform.

- **Web app**: drop two images, pick an effect, watch it render, download, make another take.
- **CLI**: `seam make first.png last.png --preset smoke`.
- **Agent plugin**: the Seam skill + MCP server so an agent can plan, render, review and refine transitions for you —
  packaged as a Claude Code plugin (`.claude-plugin/`) and as an [Agent Plugins](https://agent-plugins.org/) 1.0 plugin
  (`plugin.json` + `mcp.json` + `skills/`) for other compatible agents.

## How it works

```
browser / CLI / Claude (MCP)
        │  REST + live events
   Seam server (FastAPI) ── presets → model-specific prompt + slot-mapped workflow
        │  Comfy SDK  (comfy-sdk, Comfy API v2)
   comfy-api-proxy  →  ComfyUI (core nodes only)  →  LTX-2.5 / LTX-2.3 on your GPU
```

- Every generation goes through the official **Comfy SDK** against the **Comfy API v2** — served locally by
  `comfy-api-proxy`, or by Comfy Cloud / a deployed endpoint by changing `COMFY_BASE_URL`.
- Each effect is a preset (our own prompt text) routed to the model that renders it best: **LTX-2.5** first/last-frame
  with its in-graph prompt enhancer, or **LTX-2.3** with an open transition LoRA.
- Seam reads both shots first with the same small local model as the enhancer (Gemma-4 E2B), so the prompt says
  what is in them ("a young woman in a red leather jacket on a rainy street at night"). You can correct that line
  in the web app before rendering.
- Workflows are the official ComfyUI templates exported to API format; Seam only touches them through a named
  slot map, with a test that fails if the template drifts.
- Jobs persist in SQLite and survive a server restart (they re-attach to the running ComfyUI job).

## Install

See **[install.md](install.md)** (≈44 GB of models, NVIDIA GPU with 24 GB+ VRAM). Quick check: `uv run seam doctor`.

## Models and licenses

| Model | Role | License |
|---|---|---|
| [LTX-2.5](https://huggingface.co/Lightricks/LTX-2.5) 22B distilled (int8) + VAEs | default video model | LTX-2.x Community License (gated) |
| Gemma-4 12B (LTX-2.5 text encoder) · Gemma-4 E2B (prompt enhancer) | text encoding / prompt enhancement | Gemma terms |
| [LTX-2.3](https://huggingface.co/Lightricks/LTX-2.3-fp8) 22B distilled fp8 + Gemma-3 12B | video model for some effects | LTX-2 Community License |
| [LTX-2.3 Transition LoRA](https://huggingface.co/valiantcat/LTX-2.3-Transition-LORA) | transition style | see model card |
| Z-Image-Turbo | only used to generate the fictional test images in `dev/` | see model card |

Seam's code is MIT ([LICENSE](LICENSE)). The models keep their own licenses; read them before commercial use.
The effect presets are our own text, inspired by common transition concepts — no third-party prompts or media.

## Responsible use

Use images you own or made. Don't put a real, identifiable person into a transition without their permission.
Keep it SFW.

## Credits

Built on [ComfyUI](https://github.com/comfyanonymous/ComfyUI), the [Comfy SDK](https://github.com/Comfy-Org/comfy-python-sdk)
and [comfy-api-proxy](https://github.com/Comfy-Org/comfy-api-proxy), [LTX](https://github.com/Lightricks/LTX-2) by
Lightricks, and the open transition LoRA by valiantcat.
