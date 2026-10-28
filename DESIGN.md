---
name: Seam
description: Two shots in, one transition out. A ComfyUI graph you can operate without seeing ComfyUI.
colors:
  comfy-ink: "#211927"
  ink-deep: "#19131e"
  ink-raise: "#2a2132"
  ink-hover: "#33293c"
  stage-black: "#0e0a11"
  comfy-yellow: "#f2ff59"
  yellow-hover: "#f7ff8f"
  yellow-press: "#dbe84a"
  wire-glow: "#fbffc4"
  cream: "#f0efed"
  muted: "#c2bfb9"
  dim: "#9a94a0"
  line: "rgba(255, 255, 255, 0.1)"
  line-strong: "rgba(255, 255, 255, 0.18)"
  sock-image: "#64b5f6"
  sock-effect: "#ffa931"
  sock-text: "#c7a3ff"
  ok: "#8be3a1"
  danger: "#ff8a80"
typography:
  display:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "clamp(28px, 3.4vw, 48px)"
    fontWeight: 800
    lineHeight: 1
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 62"
  headline:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 68"
  title:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 72"
  label:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.01em"
    fontVariation: "'wdth' 72"
  body:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
    fontVariation: "'wdth' 100"
  meta:
    fontFamily: "'Archivo Variable', 'Archivo', system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.4
    fontFeature: "'tnum'"
    fontVariation: "'wdth' 100"
rounded:
  tag: "6px"
  chip: "7px"
  field: "9px"
  media: "10px"
  control: "12px"
  node: "14px"
  pill: "999px"
spacing:
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  section: "44px"
components:
  button-primary:
    backgroundColor: "{colors.comfy-yellow}"
    textColor: "{colors.comfy-ink}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.yellow-hover}"
    textColor: "{colors.comfy-ink}"
  button-primary-active:
    backgroundColor: "{colors.yellow-press}"
    textColor: "{colors.comfy-ink}"
  button-make:
    backgroundColor: "{colors.comfy-yellow}"
    textColor: "{colors.comfy-ink}"
    typography: "{typography.headline}"
    rounded: "{rounded.node}"
    height: "56px"
    width: "100%"
  button-outline:
    backgroundColor: "transparent"
    textColor: "{colors.comfy-yellow}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "40px"
  button-quiet:
    backgroundColor: "rgba(255, 255, 255, 0.05)"
    textColor: "{colors.muted}"
    rounded: "{rounded.control}"
    padding: "0 18px"
    height: "40px"
  node:
    backgroundColor: "{colors.ink-deep}"
    rounded: "{rounded.node}"
  node-chip:
    backgroundColor: "{colors.ink-raise}"
    textColor: "{colors.cream}"
    typography: "{typography.label}"
    rounded: "{rounded.chip}"
    padding: "0 10px 0 9px"
    height: "24px"
  input-field:
    backgroundColor: "{colors.ink-raise}"
    textColor: "{colors.cream}"
    rounded: "{rounded.field}"
    padding: "8px 10px"
  filter-pill:
    backgroundColor: "rgba(255, 255, 255, 0.05)"
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    height: "36px"
    padding: "0 8px 0 16px"
  filter-pill-selected:
    backgroundColor: "rgba(242, 255, 89, 0.06)"
    textColor: "{colors.comfy-yellow}"
  skew-tag:
    backgroundColor: "{colors.comfy-yellow}"
    textColor: "{colors.comfy-ink}"
    rounded: "{rounded.tag}"
    padding: "2px 10px"
---

# Design System: Seam

## Overview

**Creative North Star: "The Graph You Can Operate"**

Seam is a ComfyUI graph with the wiring hidden behind a studio layout. The page is the comfy.org world, pinned by the user: Comfy ink ground, cream text, one hot Comfy yellow, condensed uppercase display type, and a dot-grid canvas on which every block is a node with a typed "• TITLE" chip. A yellow wire runs node to node down the control rail and across into the OUTPUT node, so the one path (shots → effect → make → take) is drawn, not explained.

Density is studio-tight in the rail (400px-ish, 8–14px gaps) and generous on the canvas, where the take owns the largest area. Depth is tonal: nodes sit a step darker than the canvas, heads and fields a step lighter. Motion is reserved for the signature moments: the wire flows while a take renders, and a finished take opens from the centre seam outward.

**Key Characteristics:**
- Dark ink ground with exactly one action colour (Comfy yellow) that also marks live wires and selection.
- Nodes, chips and sockets borrowed from ComfyUI; socket dot colour says what flows through a node.
- Archivo variable: narrow (62–72% width) uppercase for display and controls, normal width for reading.
- Skewed yellow tags (-12deg) as the brand mark and count badges, after comfy.org.
- Flat surfaces; a single soft lift only on hovered gallery cards.

## Colors

One loud yellow on a family of violet-black inks, with ComfyUI's socket hues as small type markers.

### Primary
- **Comfy Yellow** (comfy-yellow): the one primary action (Make transition, Download), live wires and sockets, selected effect ring, selected filter, focus outline, progress fill, skew tags. Pressed state darkens to yellow-press; hover lightens to yellow-hover. Text on it is always Comfy Ink.
- **Wire Glow** (wire-glow): the travelling dash on the wire during a render only.

### Tertiary (socket hues)
- **Image Blue** (sock-image): FIRST/LAST SHOT socket dots, upload icons, drag-over slot border, "reading" placeholder.
- **Effect Orange** (sock-effect): EFFECT node socket.
- **Text Violet** (sock-text): DIRECTION node socket.
- VIDEO sockets reuse Comfy Yellow.

### Neutral
- **Comfy Ink** (comfy-ink): the page and canvas ground; also text on yellow.
- **Node Ink** (ink-deep): node and card bodies, one step below the canvas.
- **Raised Ink** (ink-raise): node chips, slots, effect tiles, fields.
- **Hover Ink** (ink-hover): hovered tiles, selected segment.
- **Stage Black** (stage-black): behind video and the diptych seam.
- **Cream** (cream): primary text. **Muted** (muted): secondary text and quiet buttons. **Dim** (dim): meta, hints, placeholders.
- **Line / Line Strong** (line, line-strong): 1px node borders and dividers; strong for the OUTPUT node and hovered cards.
- **Ok / Danger** (ok, danger): engine health dot and error text only.

### Named Rules
**The One Yellow Rule.** Yellow means "act here" or "live now". It never decorates a surface that is not an action, a live wire, a selection or a brand tag.
**The Socket Rule.** Every node chip carries a socket dot in the colour of what it holds (image blue, effect orange, text violet, video yellow, danger for errors).

## Typography

**Display Font:** Archivo Variable, width axis 62–72% (with Archivo, system-ui)
**Body Font:** Archivo Variable at 100% width
**Label Font:** same family, 72% width, uppercase

**Character:** one variable family doing two voices: a narrow, heavy, uppercase poster voice for headings and controls, and a plain normal-width voice for reading.

### Hierarchy
- **Display** (800, clamp(28px, 3.4vw, 48px) empty-stage title; clamp(30px, 3vw, 44px) gallery heading, line-height 1, width 62%): "Your transition lands here", "Your transitions".
- **Headline** (700, 22px, width 68%): the Make transition button.
- **Title** (800, 16–18px, width 72%): slot titles, effect names.
- **Label** (700, 13–16px, width 72%, uppercase, 0.01em tracking, 0.14em word spacing): node chips, buttons, segments, filters, progress status.
- **Body** (400, 15px, line-height 1.5): tagline, captions (2-line clamp), notes at 13.5px.
- **Meta** (400, 12–13px, tabular figures, dim): seeds, dates, durations, node meta.

### Named Rules
**The Two Widths Rule.** Uppercase is always narrow (62–72%); reading text is always 100% width and sentence case.

## Layout

A two-column studio: a sticky control rail (minmax(360px, 420px), full height under a 64px sticky top bar, its own scroll, the Make button pinned to its foot) and a canvas column with a 22px dot grid. The OUTPUT node fills the first viewport (16:9 stage capped at viewport height minus 230px); the gallery ("Your transitions") follows 44px below as an auto-fill grid of minmax(260px, 1fr) cards with 16px gaps. Canvas gutters are clamp(16px, 2.4vw, 36px).

At 960px and below the rail stacks above the canvas, loses its sticky height, and the tagline hides; the wire re-routes for the stacked layout. At 520px and below shot notes go single-column, button labels collapse to icons, and the Download button stretches.

## Elevation & Depth

Flat and tonal. Depth comes from ink steps (ink-deep body, ink-raise heads and fields, comfy-ink canvas) and 1px translucent lines, not shadows. Overlays on media (progress pill, transport, chips on thumbnails) use 82–90% ink with a 6–10px backdrop blur.

### Shadow Vocabulary
- **Card lift** (`box-shadow: 0 14px 30px -18px rgba(0,0,0,0.8)` with translateY(-3px)): hovered gallery card only.
- **Health halo** (`box-shadow: 0 0 0 3px rgba(139,227,161,0.18)`): engine-ready dot.

### Named Rules
**The Flat Node Rule.** Nodes are flat at rest; only a hovered take card lifts.

## Shapes

Softly rounded rectangles in a nested ladder: nodes and cards 14px, controls 12px, media wells and tiles 10px, fields 9px, chips 7px, tags 5–6px, pills and status dots fully round. Empty drop slots use a dashed 1px border that turns solid once filled. Skewed (-12deg) yellow parallelograms are the one non-rectangular form: brand tag, take tag, gallery count. The diptych's vertical seam (2px yellow line with a rotated yellow label) is the product's mark in shape.

## Components

### Buttons
- **Shape:** gently rounded (12px); Make uses the node radius (14px) at 56px tall.
- **Primary:** yellow fill, ink text, 700 narrow uppercase, 40px tall, 18px side padding. Hover lightens, active darkens and drops 1px. Disabled: #4a4452 fill, #a39ca8 text.
- **Outline:** cream 1px border, yellow text; hover tints 8% yellow and turns the border yellow. Used for Open in ComfyUI.
- **Quiet:** 5% white fill, muted text; hover 10% and cream. New take, Open, Cancel.
- **Icon:** 40px square (34px in the transport, 28px xs), quiet fill, hover turns the glyph yellow. Icons are inline 1.8px-stroke SVG.

### Chips
- **Node chip:** 24px raised-ink chip, label type, leading 7px socket dot in the node's type colour.
- **Filter pill:** 36px pill with a raised count bubble; selected gets a yellow border, yellow text and 6% yellow tint.
- **Segmented control:** 5% white track, 3px inset; selected segment is hover-ink with yellow text.

### Cards / Containers
- **Node:** ink-deep body, 1px line border, 14px radius, head row with chip left and dim meta right. OUTPUT uses line-strong, and a 50% yellow border while queued or running.
- **Take card:** a node with 16:9 media (chip overlaid top-left, badge top-right), meta row, 2-line caption, and an action row (Open + receipt, ComfyUI, download icons). Focused card has a yellow border; failed captions turn danger.

### Inputs / Fields
- **Style:** raised ink, 1px line border, 9px radius, 13.5px text, auto-sizing 2–6 lines.
- **Focus:** border turns yellow, no outline glow. Placeholders are dim; blue while a shot is being read, danger if reading failed.

### Navigation
- **Top bar:** 64px sticky, 92% ink with blur, bottom line. Logo mark plus skewed yellow SEAM tag, dim tagline, engine health pill (dot: dim, ok with halo, or danger) and a GitHub icon button.

### Wire (signature)
A fixed SVG overlay drawn from live layout: 2px yellow line at 55% opacity linking node heads down the rail into OUTPUT, ink-filled sockets with yellow rings. While rendering the line goes to 90% and a wire-glow dash (2/38) flows along it (GSAP, only without reduced motion).

### Output stage (signature)
16:9 well on stage black. Empty: display title plus three numbered step pills. With shots: a first | seam | last diptych, dimmed while rendering under a floating progress pill (status, 6px yellow bar, cancel). A finished take reveals from the centre seam outward (clip-path inset 50% → 0, 1.1s expo.out) and plays in a custom player whose transport (yellow scrub) appears on hover or focus.

### Effect tile (signature)
2-column grid of tiles with a 16:10 looping preview, narrow name and dim model meta. Selected: 2px inset yellow ring and yellow name. "Surprise me" swaps the preview for a yellow dice icon that rotates on hover.

## Do's and Don'ts

### Do:
- **Do** keep Comfy Ink as the ground and give each screen one yellow primary action.
- **Do** put every new block in a node with a "• TITLE" chip whose socket dot names what it carries.
- **Do** set uppercase display and control text in Archivo at 62–72% width; keep reading text at 100% width.
- **Do** connect nodes on the main path with the yellow wire, and animate it only while work is live.
- **Do** let the take own the largest area on the canvas.
- **Do** gate decorative motion behind prefers-reduced-motion: no-preference.

### Don't:
- **Don't** use yellow as a background tint for whole surfaces or for non-actionable decoration.
- **Don't** add purple glows, gradients or light SaaS card stacks; this is the comfy.org ink world.
- **Don't** add drop shadows to nodes at rest.
- **Don't** introduce a second display family or a non-Archivo system face.
