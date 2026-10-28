# Seam

**Two shots in, one transition out.** Seam makes a short video that travels from a first shot to a last one
through an effect — smoke, a water bend, a whip pan — rendered on your own GPU with open-weights models.
No accounts, no credits.

It is an open-source take on the popular "transitions" apps, built for the
[Open Source Higgsfield](https://comfy.org/open-source-higgsfield) challenge on the Comfy Developer Platform.

- **Web app**: each shot can be an image, a 1–6 s clip or a **3D model (.glb)**. Pick an effect (or "Surprise me"),
  watch it render, download, make another take, or open the take in ComfyUI. No images at hand? **Try an example**.
- **CLI**: `seam make first.png last.png --preset smoke`.
- **Agent plugin**: the Seam skill + MCP server so an agent can plan, render, review and refine transitions for you —
  packaged as a Claude Code plugin (`.claude-plugin/`) and as an [Agent Plugins](https://agent-plugins.org/) 1.0 plugin
  (`plugin.json` + `mcp.json` + `skills/`) for other compatible agents.

**Site:** https://jdnichollsc.github.io/seam/ · **Demo video (2 min):** _link added at submission_

## Hackathon submission

| | |
|---|---|
| Category fit | **Open Weights Only**: every render is LTX-2.5 (+ Gemma-4 E2B to read the shots) on your own GPU through core ComfyUI nodes; no partner nodes, no closed APIs. **VFX Workflow built with LTX**: LTX-2.5 first/last-frame is the core model of every effect. |
| Comfy Developer Platform | Every render goes through the official **Comfy SDK** to the **Comfy API v2** (`comfy-api-proxy` locally). |
| Workflow files | [`src/seam/workflows/`](src/seam/workflows) (API format, slot-mapped), and any take exports its exact graph via **Open in ComfyUI**. |
| Run it locally | [install.md](install.md), checked end to end by `uv run seam doctor`. |
| Try it | The app ships three example pairs: open the studio and pick one. No hosted endpoint: Seam runs on your GPU. |

**3D models in.** Drop a `.glb` on either shot: Seam opens it in a small 3D studio in the browser (three.js), you
frame it and record a 3 s camera orbit. The orbit becomes a clip, so the transition leaves from where the first
orbit ends and lands where the second begins. Or keep one angle as a still. The model never leaves your machine.

## The three effects

First shot → Seam → last shot. Fictional test images; real takes from the qualification run (seed 1).

| First shot | Smoke | Water bend | Whip pan | Last shot |
|---|---|---|---|---|
| <img src="docs/media/p1_a.webp" width="120"> | <img src="docs/media/smoke.webp" width="120"> | <img src="docs/media/water_bend.webp" width="120"> | <img src="docs/media/whip_pan.webp" width="120"> | <img src="docs/media/p1_b.webp" width="120"> |

Only effects that passed the same check ship: played and approved on two image pairs (a person, a product), two
seeds, with Seam's own shot reading, then one take on each of four unseen pairs spread across the three effects
([dev/PRESET_REVIEW.md](dev/PRESET_REVIEW.md)).
More effects ship only if they pass it too.

**Measured** (RTX 5090 32 GB, Windows 11, ComfyUI Desktop 0.41.3; 5 s clips at 1280×704 or 704×1280):

| Effect | Takes | Render time, median (min–max) |
|---|---|---|
| Smoke | 10 | 41 s (30–46 s) |
| Water bend | 9 | 40 s (30–41 s) |
| Whip pan | 9 | 41 s (30–56 s) |

Render time runs from submit to finished video, including LTX-2.5's prompt enhancer and any model loading (the
slowest takes were the first after a load). Reading the two shots (~1 s each, cached) comes on top. Peak VRAM was ~31 GB on a 32 GB
card; 24 GB cards should work through ComfyUI's offloading (64 GB RAM) but are untested.

**Status:** tested on Windows 11 with one RTX 5090. Linux, macOS and smaller GPUs are untested. The web app, CLI
and MCP server run against a local ComfyUI; Comfy Cloud should work by changing `COMFY_BASE_URL` but is untested.

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
- **Clips in, clips out:** either side can be a 1–6 s clip. The transition starts from clip A's last frame and lands
  on clip B's first, through the same qualified graph, and Seam joins clip A + transition + clip B with ffmpeg.
- **Every take is reproducible:** a receipt (input hashes, effect, model, seed, final prompt, graph hash) and a
  one-click **Open in ComfyUI** bundle: the exact API workflow plus both stills, runnable in plain ComfyUI.

## Why generative, not a 2D blend

Code-based tools (FFmpeg `xfade`, Remotion, HyperFrames) blend two flat frames: a wipe or a dissolve, exact and
fast, but they cannot invent what is between the shots. Seam asks a video model for the in-between: smoke that
really occludes the subject, water that reshapes into the next scene, a pan with real parallax and motion blur.
Those tools are a good assembly layer around Seam's clips, not a replacement for them.

## Install

See **[install.md](install.md)** (≈44 GB of models, NVIDIA GPU with 32 GB VRAM tested). Quick check: `uv run seam doctor`.

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
