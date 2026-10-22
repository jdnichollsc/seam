---
name: seam
description: Make a short video that transitions from one image to another with an effect (smoke, water bend, whip pan, …), rendered locally on ComfyUI with open-weights LTX models through the Seam MCP server. Use when the user wants a transition, a "before/after" or outfit/location change clip, or wants to join two shots with an effect.
---

# Seam — AI transitions between two shots

Seam takes a **first shot** (image A) and a **last shot** (image B) and renders a ~5 s clip that travels from A
to B through an effect. It runs on the user's own GPU (ComfyUI + Comfy SDK, open-weights LTX models). One job
renders at a time; a clip takes about a minute on an RTX 5090.

## Before anything

1. Call `seam_status`. If it fails, the Seam server is not running: ask the user to run `seam serve` (and that
   ComfyUI + `comfy-api-proxy` are up — `seam doctor` checks everything). Do not try to start GPU services yourself.
2. Call `list_presets` and only use ids it returns — they are the qualified effects. Never invent preset ids.

## The loop

1. **Look at both images first** (read them). Seam reads each shot itself with a local model and puts that
   description in the prompt — the job shows it as `a_desc` / `b_desc` once it starts. Pass your own only to
   correct or steer it: a noun phrase (`a_desc: "a young woman with a calm smile in a red leather jacket on a
   rainy neon street at night"`) and optionally a short label (`a_ref: "the woman in the red jacket"`).
2. **Pick the effect with the user**, or suggest one that suits the change:
   - change of place or outfit, same person → smoke, water bend, whip pan
   - object → object, or the same product in a new place → smoke, whip pan
   - if the user asks for an effect list_presets does not return (fire, morph, …), say it is not available yet and
     offer the closest listed one
3. `create_transition(image_a, image_b, preset)` with **local file paths** (plus `a_desc` / `b_desc` if you
   are correcting Seam's reading).
4. `wait_job(job_id)`, then check `status`. Still `queued`/`running` (the wait timed out): say so and wait again.
   `failed`/`cancelled`: report the error. Only `succeeded` has a preview, and it only means a file was rendered,
   not that the transition works — so call `preview_job(job_id)` and **judge the contact sheet** before you go back
   to the job's prompt or descriptions.
5. Show the user the preview and the video URL. In one or two lines say what you saw (first frame, the moment of
   change, last frame), then whether it matches what was asked. The 8 frames can miss a quick cut or fade between
   them: say what you could not see, and always give the video URL so the user can watch it.
6. To iterate, use `refine_job(job_id, …)`: a different `preset`, extra direction in `prompt_extra`
   ("slower", "keep her face sharp", "warmer light"), or a pinned `seed` to keep the same take. Each refinement
   is a new job linked to its parent, so the user's gallery keeps the history.

## Judge a result like an editor

Judge from the frames: describe what you see before comparing it with the request. The job's text (prompt,
descriptions, status) tells you what was asked and that it ran — never that a requirement was met. When unsure,
say so; the user watching the clip is the final check.

A transition passes only if all of these hold — check the contact sheet:
- It **starts on image A and ends on image B** (no jump in the last frames).
- The effect is **legible** (you would name it without being told).
- The scene change happens **inside** the effect — behind the smoke, under the water, mid-pan while the frame is covered.
  An effect that plays and *then* cuts or fades to B is a defect: refine (new seed or a clearer `prompt_extra`).
- The subject keeps its identity; no warped faces or extra limbs.

If two takes in a row fail the same way, suggest a different preset instead of a third take.

## Rules

- Only use images the user owns or generated; never put a real, identifiable person into a transition without
  their permission. Keep everything SFW.
- You cannot hear the audio track; say "give it a listen" instead of judging sound.
- Jobs are local and free, but the GPU is shared: don't queue more than the user asked for.
- Outputs live in the Seam gallery (`http://127.0.0.1:7860`); share the URL, don't copy files around.
