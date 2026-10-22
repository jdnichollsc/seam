# Preset review — user observations → seldon (Codex) recommendation → decision

Context for the reviewer: PLAN.md (v3 + product decisions), REQUIREMENTS_v2, deadline Fri 10/30 9am PT,
MVP launch set target 6 (floor 3), "don't build more than the first app needs".
Clips: dev/bakeoff/compare/new/<preset>.mp4 (left = LTX-2.3 + transition LoRA, right = LTX-2.5 + enhancer),
pair p1 (woman, neon street at night → white dress, beach at sunset). Earlier picks: fire → L23, whip_pan → L25,
morph → L25 (+ H3 opt-in local).

Evaluation questions: (1) starts on A / ends on B, no end jump; (2) effect legible; (3) the scene change
happens *inside* the effect, not as a cut after it; (4) identity stable, no artifacts; (5) would you post it.

| Preset | User observation | Seldon recommendation | Decision |
|---|---|---|---|
| raven | LEFT: ravens look like pigeons, then penguins, then abstract paint. RIGHT: keeps bird shape but wings flap too fast like bats/hummingbirds — unnatural. Neither convinces. | ALT-MODEL (Wan 2.2 / H3 local), auto-DEFER on fail | |
| air | LEFT (LTX-2.3): the air starts at her chest — not realistic; she does spin, but the effect origin looks wrong. RIGHT (LTX-2.5): wind wraps *around* her in 3D and makes her spin — more natural and realistic. In both, the change to the beach is more of a fade-in/fade-out than happening inside the vortex; still prefers RIGHT. | RETUNE on LTX-2.5 + enhancer (0.86): vortex must fully occlude the frame while A→B changes; one re-render, else DEFER. Text: dev/retunes/candidates.yaml `air_v2`; review: _research/reviews/preset-air-codex.md | pending user (re-render dev/bakeoff/retune/l25/p1/air_v2_s1.mp4) |
| water_bend | RIGHT more realistic. Prefers RIGHT. | INCLUDE L25 | |
| lava | RIGHT more realistic (she carbonizes). LEFT looks like a flash from the chest, then paint instead of lava. Prefers RIGHT. | INCLUDE L25 | |
| fpv | Likes NEITHER. LEFT: she starts moving but seems to sink to the floor. RIGHT worse: several 180° spins, face distorts, reaches the beach then goes back to scene A and passes to B again — dizzying. | ALT-MODEL (Wan 2.2 / H3 local), auto-DEFER on fail | |
| pillars | Not understandable in either. RIGHT: after a pillar (a tree trunk on a night street?) passes, she fades before scene B with no reaction. LEFT: pillars move both directions at once and one seems to carry her away. Neither makes sense. | ALT-MODEL (Wan 2.2 / H3 local), auto-DEFER on fail | |
| spin_wipe | Prefers RIGHT. | INCLUDE L25 | |
| cameraman | RIGHT: more realistic photographer/camera operator; she turns to look at the camera (good), but the operator never pans to keep the camera on her. Prefers RIGHT. | RETUNE L25 (cameraman_v2: operator pans to keep her framed + full occlusion during change); one re-render else DEFER | re-rendered: full occlusion by the operator, beach established behind him, no dissolve (dev/bakeoff/compare/retune/cameraman.mp4) — pending user |
| jump | LEFT: jump too small but realistic. RIGHT: super-jump with dust, but face/arms deform during the fast motion. Prefers LEFT. | INCLUDE L23 | |
| seamless | LEFT: transition carries from one scene to the other better. RIGHT: she moves and fades out before scene B appears, no connection. Prefers LEFT. | INCLUDE L23 | |
| truck | Likes NEITHER much. LEFT: very slow and the truck should run her over being so close. RIGHT: truck comes from behind and she fades like a ghost. | ALT-MODEL (Wan 2.2 / H3 local), auto-DEFER on fail | |
| beast | RIGHT: more realistic monster transformation. | INCLUDE L25 | |
| intermission | No readable "we will be right back" card. RIGHT card appears/disappears better but has no text; LEFT text is unreadable AI-garbage and the card looks unrealistic. | DEFER — models cannot render legible card text | |
| passerby | LEFT: someone appears behind, she steps aside left while the person walks forward and exits right — makes sense. RIGHT: a person stands up in front of her, another appears behind, both move backwards — weird. Prefers LEFT. | INCLUDE L23 | |
| earth_wave | Likes NEITHER. LEFT: she looks at the rock and reacts/moves. RIGHT: rock more realistic but she seems to melt into rock — makes no sense. | ALT-MODEL (Wan 2.2 / H3 local), auto-DEFER on fail | |


User request (2026-10-17 23:1x): test other open models (latest MiniMax / Seedance) on the presets where neither side convinces. Seedance = partner API only (not open) → not testable as open weights. Running: MiniMax H3 (local, already downloaded) on earth_wave/fpv/intermission/pillars/raven/truck; downloading Wan 2.2 14B FLF2V (Apache-2.0) for the same set.

Codex batch review (_research/reviews/preset-batch-codex.md, 0.84): launch priority 6 = fire L23, whip_pan L25, morph L25, smoke L25, water_bend L25, spin_wipe L25; then (only if G1-passing) ink, light_burst, zoom_through, lava L25; seamless, jump, passerby L23; beast L25. Alt-model cap: 12 renders / 2 GPU-h / Sun 10/18 evening, no tuning, H3 private. Cross-cutting enhancer rule: effect starts → full opaque coverage → B established behind coverage → effect clears; no transparent A/B overlap, late cuts, duplication, unintended anatomy changes (except morph/beast/seamless).

## Launch qualification — user verdicts on played clips (2026-10-20, `compare/qual/`, hand-written shot texts)
Grid per clip: P1 (woman, portrait) / P2 (sneaker → cup, landscape) × seed 1 / seed 2. Auto cut flag (6× median) in brackets.

| Preset | User observation (qual) | Seldon recommendation | Decision |
|---|---|---|---|
| fire (L23) | All four look good. [no flags] | | |
| morph (L25) | P1 s2 looks odd: her chest compresses inward like liquid while mist surrounds her; s1 is better. P2 s1+s2: the sneakers turn black before a white tea cup appears; the sneaker transformation looks fine but has no relation to the cup at the end. [no flags] | | |
| smoke (L25) | P1 s2: the smoke looks good, but before she is fully covered her chest compresses inward again, which makes no sense. [P1 s2 flagged 9.06×] | | |
| spin_wipe (L25) | P1 s2: the circle animation starts in an odd place on screen. [P1 s1 26.75×, P1 s2 27.38×, P2 s2 6.32×] | | |
| water_bend (L25) | P1 s2: the water looks good, but when it covers her the face fades unrealistically (it moves down and grows). [no flags] | | |
| whip_pan (L25) | P1 s2: near the end of the pan the taxi seen earlier reappears briefly, which makes no sense (she would be looking the other way; cars should be seen from behind or not at all). [P1 s1 flagged 10.04×] | | |

Pattern (verified in results.jsonl): every reported defect is P1 seed 2, in all 5 L25 presets; L23 fire has none.
Same seed + same image ⇒ correlated noise across presets, so these are probably one unlucky draw, not 5 independent failures.
Production draws a random seed per job (engine.py:109-110). `autodesc/` verdicts still pending.

### autodesc verdicts (2026-10-21, `compare/autodesc/`, seed 1, Seam's own shot reading vs hand-written)
| Preset | User observation (autodesc) |
|---|---|
| fire | Looks good. |
| morph | P1 Seam reads looks odd: she moves her arms back and disappears into some substance; does not look good. |
| smoke | Look good. |
| spin_wipe | P2 Seam reads: odd circle; it leaves, but when the scene changes to the cup in the forest it jumps back to the previous scene and repeats the animation — incongruent. |
| water_bend | Look perfect. |
| whip_pan | Look good. |

Seldon (Codex, verified; water_bend corrected to match smoke): fire/smoke/whip_pan/water_bend = LAUNCH-IF the autodesc seed-2
repeat passes on both pairs (no recurrence ⇒ launch; recurrence ⇒ defer, no seed hunting); morph, spin_wipe = DEFER.
Renders: 8 (4 presets × p1/p2 × seed 2, autodesc) + 4 unseen pairs = 12. Brief-path renders postponed.

### autodesc seed-2 repeat verdicts (2026-10-21, `compare/autodesc_s2/`; hand-written = qual s2, Seam reads = new autodesc s2)
| Preset | User observation |
|---|---|
| fire | P2 hand-written looks better: when it catches fire it shrinks a little as if burning; P2 Seam reads also ignites but then changes scene with a simple fade-out. |
| smoke | Looks good. |
| water_bend | All good. |
| whip_pan | All good. |
| (morph, spin_wipe) | Re-stated earlier seed-1 autodesc defects (already DEFER): morph P1 arms move back then disappear into a viscous substance; spin_wipe P2 circle repeats twice, cup moves then appears mid-frame in the forest. |

### G1 endpoint tolerance (calibrated 2026-10-21 from the user-rated clips, LTX-2.5)
Approved launch clips (smoke, water_bend, whip_pan; qual + autodesc, 24 clips): ssim_first 0.873–0.918, ssim_last 0.880–0.949.
Rejected clips (fire p2 autodesc s2, morph p2 qual s1/s2, morph p1 autodesc s1, spin_wipe p2 autodesc s1): 0.872–0.924 —
inside the approved range. So endpoint SSIM does NOT separate good from bad motion; it only proves the clip starts on A and ends
on B (frames 0/-1 are guided at 0.7). **Tolerance: ssim_first and ssim_last ≥ 0.85** (≈0.02 below the lowest approved value).
Motion quality (dissolve, late cut, deformation) is judged only by playing the clip.

### Held-out check (2026-10-21, unseen fictional pairs p3–p6, autodesc, seed 1, one take each; `compare/heldout/heldout.mp4`)
| Pair · effect | User observation | Facts checked |
|---|---|---|
| p3 man (snow → market) · smoke | Before the smoke he stands frozen, no arm movement, while the frame zooms in until the smoke covers him; scene B moves naturally. | The zoom is the preset's own camera: "slow push-in through the smoke" (presets.yaml:28). The prompt asks for no subject action. |
| p6 perfume (marble → volcanic rock) · smoke | The smoke looks good, but the perfume bottle is different between the two scenes. | The input fixtures already show two different bottles (dev/fixtures/heldout_pairs.png: p6_a tall bottle, p6_b smaller bottle with taller cap) — a fixture flaw, not a render defect. |
| p4 bicycle → scooter · whip_pan | (no comment) | ssim_first 0.8175 < 0.85 tolerance; frame 0 visually matches p4_a (compare/heldout/p4_start.png) — likely fine brick texture. |
| p5 woman (park → rooftop) · water_bend | (no comment) | — |
User verdict on p4 and p5 (2026-10-21): both look good.

## DECISION (2026-10-21, user-confirmed after Codex seldon + verifier)
- **launch:** smoke, water_bend, whip_pan (all LTX-2.5 + enhancer). G1: user-approved on both pairs, both seeds, hand-written
  and Seam-read shot texts; endpoint SSIM ≥ 0.85. Held-out: 4/4 pairs without transition defects. Recorded exception: p4 ssim_first
  0.8175 < 0.85, frame 0 visually matches A.
- **deferred:** fire (fade-out on p2 autodesc s2), morph (p2 no connected transformation; p1 autodesc disappearance),
  spin_wipe (repeated animation / returns to A).
- Known limitations for docs/demo: smoke's push-in can leave a still subject frozen before coverage (p3; the prompt asks for no
  subject action); Seam keeps whatever is in the two photos — if the object differs between A and B, so will the clip (p6).
