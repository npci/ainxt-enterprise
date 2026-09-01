---
name: algorithmic-art
description: Create generative / algorithmic art (flow fields, particle systems, fractals, geometric patterns, noise art) as a single self-contained HTML file using canvas or SVG, saved to the working folder so it renders in the Preview tab.
when_to_use: When the user wants generative, procedural, or algorithmic art — flow fields, particles, fractals, L-systems, noise, geometric/parametric patterns, creative coding — and see it rendered or animated.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
argument-hint: "[style or subject]"
user-invocable: true
---

# Algorithmic Art

Write a **single self-contained `.html` file** that generates art procedurally with `<canvas>` (or SVG) and renders in the Code tab's **Preview** tab. The goal is something genuinely striking, not a textbook demo.

## Inputs
- `$ARGUMENTS`: the style/subject (e.g. "Perlin flow field", "recursive tree", "Voronoi mosaic", "particle constellation").

## Steps

### 1. Choose a technique
Pick a generative approach that fits the request — flow fields, particle/agent systems, noise fields, fractals/recursion, L-systems, Voronoi/Delaunay, packing, reaction-diffusion, parametric curves, etc. Use vanilla canvas/JS, or `p5.js` via CDN if it materially helps. No build step.

### 2. Make it beautiful, not just correct
- **Composition:** density, contrast, and negative space matter as much as the algorithm. Tune parameters until it actually looks good.
- **Color:** a deliberate palette (harmonious or high-contrast on purpose), often seeded from a base hue; avoid pure-random rainbow noise.
- **Motion (optional):** smooth `requestAnimationFrame` animation when it adds life; keep it performant (cap particle counts, avoid layout thrash).
- **Determinism:** support a seed so a result can be reproduced; expose a few key parameters as constants at the top of the script.

### 3. One self-contained file
- Full-viewport canvas that resizes with the window; high-DPI aware (scale by `devicePixelRatio`).
- Everything inline (or CDN). Save to the working folder with a descriptive kebab-case name (e.g. `flow-field.html`). Don't overwrite unrelated files.

### 4. Hand off
Give the filename and tell the user to open the **Preview** tab. Offer a tweak (new seed, palette, density, or an animated version).

**Success criteria:** one `.html` file on disk that renders standalone, generates the art procedurally, and looks intentionally composed.
