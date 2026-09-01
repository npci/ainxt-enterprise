---
name: frontend-design
description: Build a polished, production-quality front-end UI (landing page, dashboard, component, or full page) as a single self-contained HTML file with modern CSS and JS, then save it to the working folder so it renders in the Preview tab.
when_to_use: When the user wants to design or build front-end UI — a landing page, hero section, pricing page, dashboard, form, component, or any web interface — and see it rendered live.
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
argument-hint: "[what to build]"
user-invocable: true
---

# Frontend Design

Produce a **single self-contained `.html` file** (inline `<style>` and `<script>`; external libraries only via CDN) that the user can open in the Code tab's **Preview** tab. Aim for work that looks like a senior product designer made it — not a generic template.

## Inputs
- `$ARGUMENTS`: what to build (e.g. "SaaS landing page hero", "analytics dashboard", "pricing table").

## Steps

### 1. Understand the intent
If the request is specific, proceed. If it's vague, make one strong, opinionated choice (audience, tone, brand feel) and state it in one line — do not stall asking questions.

### 2. Design with craft
- **Layout:** clear visual hierarchy, generous whitespace, a sensible max-width, real responsive behaviour (mobile + desktop) via fl/grid + media queries.
- **Type:** a deliberate type scale; pair fonts via Google Fonts CDN. Never leave everything at 16px.
- **Color:** a small, intentional palette (CSS variables). Accessible contrast. Subtle gradients/shadows over flat gray.
- **Detail:** hover/focus states, smooth transitions, rounded radii, iconography (inline SVG or a CDN icon set). No lorem-only blocks — write plausible, specific copy.
- **Motion:** tasteful, performant CSS animation where it helps; never gratuitous.

### 3. Write one self-contained file
- Everything inline. If you need a framework, use a CDN (e.g. Tailwind Play CDN, Alpine, or vanilla). No build step, no local `node_modules`.
- Semantic HTML, `lang`, viewport meta, sensible `<title>`.
- Save to the working folder with a descriptive kebab-case name, e.g. `landing-hero.html`. If a file with that name exists, pick a new name rather than clobbering unrelated work.

### 4. Hand off
Tell the user the filename and that they can open it in the **Preview** tab (or the file appears in the Files explorer). Offer one or two concrete next tweaks (e.g. "want a dark variant / a second section?").

**Success criteria:** a single `.html` file on disk that renders standalone, looks intentionally designed, and is responsive.
