---
name: seam
description: Make a short video that transitions from one image to another with an effect (smoke, water bend, whip pan, …), rendered locally on ComfyUI with open-weights LTX models through the Seam MCP server. Use when the user wants a transition, a "before/after" or outfit/location change clip, wants to join two shots with an effect, or wants one longer video through a sequence of shots (a story, a lookbook, a product tour).
---

# Seam — AI transitions between two shots

Seam takes a **first shot** (image A) and a **last shot** (image B) and renders a ~5 s clip that travels from A
to B through an effect. It runs on the user's own GPU (ComfyUI + Comfy SDK, open-weights LTX models). One job
renders at a time; a 5 s clip takes about 40 s on an RTX 5090 (longer on the first run, while models load).

## Before anything

1. Call `seam_status`. If it fails, the Seam server is not running: ask the user to run `seam serve` (and that
   ComfyUI + `comfy-api-proxy` are up — `seam doctor` checks everything). Do not try to start GPU services yourself.
   If `seam_status` reports `backend: cloud`, tell the user before rendering that the shots go to Comfy Cloud and
   the takes spend their credits. Never switch the backend or handle an API key: both are the user's.
   Leave `model` out unless the user names a model: each effect has its reviewed default. If they do, call `list_models`,
   use only an available id, say the effect was not reviewed on that model, and for `kind: partner` say it spends credits.
2. Call `list_presets` and only use ids it returns — they are the qualified effects. Never invent preset ids.

## The loop

1. **Look at both images first** (read them). Seam reads each shot itself with a local model and puts that
   description in the prompt — the job shows it as `a_desc` / `b_desc` once it starts. Pass your own only to
   correct or steer it: a noun phrase (`a_desc: "a young woman with a calm smile in a red leather jacket on a
   rainy neon street at night"`) and optionally a short label (`a_ref: "the woman in the red jacket"`).
2. **Pick the effect with the user**, or suggest one that suits the change:
   - change of place or outfit, same person → smoke, water bend, whip pan, light burst, ink, zoom through
   - object → object, or the same product in a new place → smoke, whip pan, melt, lava
   - a dramatic reveal → beast (the subject turns into a creature first), lava, wind vortex
   - how each one hides the change: **smoke** rolls over the frame and B is revealed as it clears (foreground
     occlusion); **water bend** turns the scene itself into water that reshapes into B; **whip pan** is a fast
     sideways camera move whose motion blur bridges the two places
   - takes are 5 s; `list_presets` shows each effect's `seconds` — smoke, water bend and whip pan also render
     10 s takes, where the scene plays ~3.5 s, the effect sits in the middle, and B settles. Pass `seconds` only
     when the user wants a longer take
   - if the user asks for an effect list_presets does not return (fire, morph, …), say it is not available yet and
     offer the closest listed one
3. `create_transition(image_a, image_b, preset)` with **local file paths** (plus `a_desc` / `b_desc` if you
   are correcting Seam's reading). Either side can be a short clip (1-6 s): the transition starts from clip
   A's last frame / lands on clip B's first, and the job adds `merged_url` (clip A + transition + clip B). Put in `prompt_extra` only what the user asked for, in their terms (keep their
   own constraints, e.g. "keep her face sharp"); never invent direction, and leave it empty when they gave none.
4. `wait_job(job_id)`, then check `status`. Still `queued`/`running` (the wait timed out): say so and wait again.
   `failed`/`cancelled`: report the error. Only `succeeded` has a preview, and it only means a file was rendered,
   not that the transition works — so call `preview_job(job_id)` and **judge the contact sheet** before you go back
   to the job's prompt or descriptions. Row 1 spans the whole clip; row 2 is the middle (30–70%), where the change
   happens — look at it closely, mistakes in the middle are the easiest to miss.
5. Show the user the preview and the video URL. In one or two lines say what you saw (first frame, the moment of
   change, last frame), then whether it matches what was asked. Sampled frames can miss a quick cut or fade between
   them: say what you could not see, and always give the video URL so the user can watch it.
6. To iterate, use `refine_job(job_id, …)`: a different `preset`, extra direction in `prompt_extra`
   ("slower", "keep her face sharp", "warmer light"), or a pinned `seed` to keep the same take. Each refinement
   is a new job linked to its parent, so the user's gallery keeps the history.

## Sequences — one video through several shots

When the user has more than two shots (a lookbook, a trip, a product tour, a short story), make a sequence instead of
separate transitions: shot 1 → shot 2 → … with one transition at each hand-off, joined into one video.

1. Agree on the **order** of the shots with the user (2-8; images or 1-6 s clips — a clip plays in full at its
   place). Every take renders at the first shot's size, and the sequence keeps that canvas even if the shots are
   reordered later, so put the shot with the framing they want first and say when another shot has a very different
   aspect (it gets centre-cropped). If the user has an idea for the whole film (who, where, mood), pass it as
   `story` (≤300 chars); each transition's own direction stays in its `prompt_extra`.
2. Pick an effect **per hand-off** (the same routing as above); varying effects reads better than one repeated, but
   follow the user. `seconds` per hand-off as for a single take.
3. `create_sequence(shots, segments, story?)` — `segments` has one entry per hand-off: `{"preset": …,
   "seconds"?, "seed"?, "prompt_extra"?}`. Then `wait_sequence`.
4. Judge **each segment** with `preview_job(take id)` exactly like a single transition (row 2, the middle), then the
   whole arc with `preview_sequence` (hand-offs: does each take land on the shot the next one leaves from?).
5. Re-roll only the segment that fails with `refine_segment(sequence_id, segment, …)` — the other takes are reused
   and the video is joined again. Give the user the sequence's video URL.
6. **Grow a story shot by shot.** From a finished two-shot transition, `continue_take(job_id, next_shot, preset)`
   makes it the first transition of a sequence; after that, `extend_sequence(sequence_id, next_shot, preset)` adds
   one shot at a time. Only the new transition renders. Show the user each new take (`preview_job`) before adding
   the next shot. Each transition starts from the user's own shot, so the story stays as consistent as the shots the
   user brings: suggest shots of the same subject, look and light, and put a shared direction in `story`.
7. **Edit it like a storyboard.** `edit_sequence(sequence_id, order, transitions?, story?, expected_updated_at)`
   does removes, reorders and the story in one call. `order` lists the current shot numbers (from `get_sequence`)
   in the new order; leave one out to remove it. A transition whose two shots were already adjacent in that
   direction keeps its take; every other one is new, renders, and needs an effect in `transitions` (keyed by its
   number in the NEW order). Before calling, tell the user which transitions will render and which are reused, and
   pass the `updated_at` you read. Removing the first or last shot renders nothing (the video just joins again).
   Never rebuild the whole sequence to change one part: a different effect or direction for one transition is
   `refine_segment`; a new story re-renders nothing — takes made before it show `older_story`, and remaking one
   with the new story is `refine_segment` on it, only when the user asks.

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
- No empty beat: the frame never sits on plain smoke, water or blur with neither scene readable for long.
- B is readable before the effect fully clears, and stays B to the last frame.

Give each check a 1–5 score with what you saw (e.g. "legible effect 4/5: smoke covers the frame by 40%"). The
scores are advice for the user, not a gate — they decide.

When comparing takes, judge each one against the user's **original** request, not against the latest
refinement, and look at the sheets in both orders (A then B, B then A) before you pick; say which you would ship
and why.

If two takes in a row fail the same way, suggest a different preset instead of a third take.

## Rules

- Only use images the user owns or generated; never put a real, identifiable person into a transition without
  their permission. Keep everything SFW.
- You cannot hear the audio track; say "give it a listen" instead of judging sound.
- Jobs are local and free, but the GPU is shared: don't queue more than the user asked for.
- Outputs live in the Seam gallery (`http://127.0.0.1:7860`); share the URL, don't copy files around.
