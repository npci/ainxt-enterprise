---
name: canvas-design
description: Design a polished visual composition (poster, social card, banner, hero graphic, illustration, or infographic) as a single self-contained HTML or SVG file, saved to the working folder so it renders in the Preview tab.
when_to_use: When the user wants to design a graphic, poster, social media card, banner, badge, illustration, diagram, or any standalone visual composition — and see it rendered.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
argument-hint: "[what to design]"
user-invocable: true
---

# Canvas Design

Create a **single self-contained visual** — either an `.html` file (HTML/CSS, or `<canvas>` + JS) or a standalone `.svg` — that renders in the Code tab's **Preview** tab. Treat it as a finished design piece, not a wireframe.

## Inputs
- `$ARGUMENTS`: what to design and (if given) the target size/format (e.g. "Instagram post 1080×1080", "conference poster A2", "podcast cover").

## Steps

### 1. Lock the format
Pick exact dimensions for the medium (e.g. 1080×1080 social, 1200×630 OG image, 1080×1920 story, poster ratio). Set them explicitly on the artboard so the export is pixel-correct.

### 2. Compose with intent
- **Focal point + hierarchy:** one clear hero element; size/contrast/position guide the eye.
- **Grid & alignment:** align to an underlying grid; balanced margins; avoid accidental centering.
- **Color & type:** a tight, deliberate palette (CSS variables / SVG defs); expressive type pairing via Google Fonts; real headline copy, not placeholder.
- **Texture & depth:** gradients, layered shapes, soft shadows, geometric or organic motifs — avoid flat default boxes.
- **Finish:** consistent corner radii, optical spacing, crisp edges.

### 3. Produce one self-contained file
- **SVG** for crisp vector posters/cards/illustrations; **HTML/CSS or canvas** when you need effects, layout, or interactivity. External fonts/icons via CDN only.
- Fixed artboard sized to the chosen dimensions, centered on a neutral backdrop so the composition reads clearly in Preview.
- Save to the working folder with a descriptive kebab-case name (e.g. `launch-poster.svg`). Don't overwrite unrelated files.

### 4. Hand off
Give the filename and tell the user to open the **Preview** tab. Offer a quick variant (different palette, alternate size/orientation).

**Success criteria:** one `.svg` or `.html` file on disk, correctly sized, that renders standalone as a finished, intentional composition.
