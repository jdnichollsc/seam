# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

People who make short social videos and already use hosted "transitions" apps (Higgsfield, Kling, Runway, Pika). They bring two shots, a first and a last, and want a short clip that travels from one to the other through an effect. They expect a studio layout: controls on one side, the result and their takes on the other, an effect picker with previews, and a single generate button. A second audience is ComfyUI users who want the same result locally and a workflow they can open in ComfyUI.

## Product Purpose

Seam turns two shots (images, or 1–6 s clips) into a ~5 s transition video, rendered on the user's own GPU with open-weights models through ComfyUI. Success: drop two shots, pick an effect (or "Surprise me"), get a usable take in about a minute, make another take, download it, or open the exact take in ComfyUI.

## Positioning

The open, local counterpart to hosted transition apps: no accounts, no credits, and every take is a ComfyUI workflow you can inspect and re-run (receipt + `comfyui.zip`). Built for Comfy's "Open Source Higgsfield" challenge on the Comfy Developer Platform.

## Operating Context

- Runs at `http://127.0.0.1:7860` against a local ComfyUI (via comfy-api-proxy); the same server backs a CLI and an MCP server/agent plugin.
- Renders take ~30–60 s each on an RTX 5090; the ComfyUI queue is shared, so takes can wait in line.
- Only qualified effects are exposed (today: Smoke, Water Bend, Whip Pan, Lava, Beast, Ink, Light Burst, Zoom Through, Melt, Wind Vortex; 10 s takes for the first three), LTX-2.5 only. No model picker.

## Capabilities and Constraints

- Inputs: first shot and last shot, each an image or a short clip (drop, paste, choose). Clips: the transition starts from A's last frame and lands on B's first; the result can be shown "with clips" or "transition only".
- Seam reads each shot and shows an editable one-line description that goes into the prompt.
- Optional: "Describe the moment" text and a seed.
- Takes: live progress, cancel, new take (new seed), download, receipt ("How it was made"), ComfyUI bundle, history with effect filters.
- Engine health indicator; render-time estimate from recent takes.

## Brand Commitments

- Name: Seam. Tagline: "Two shots in, one transition out."
- The web app should feel native to ComfyUI / comfy.org (user direction, 2026-10-27): Comfy ink ground, Comfy yellow, condensed uppercase display, node-and-wire motif.
- SFW content only; no unlicensed IP or likenesses in demo material.

## Evidence on Hand

- Real takes and fixtures: `docs/media/*.webp`, `dev/fixtures/p[1-6]_[ab].png` (fictional test images).
- Measured render times in README. No testimonials, users, or benchmarks beyond those; do not invent any.

## Product Principles

1. The result is the hero: the take, not the chrome, owns the largest area.
2. One obvious path: two shots → effect → make. Everything else is optional and folded.
3. Honest state: queue, progress, failure, and cost-free re-take are always visible and truthful.
4. Open by default: every take can be opened as the ComfyUI workflow that made it.

## Accessibility & Inclusion

Keyboard-operable controls, visible focus, WCAG AA contrast, respects reduced motion.
