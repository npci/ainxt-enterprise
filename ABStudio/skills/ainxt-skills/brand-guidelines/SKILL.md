---
name: brand-guidelines
description: Applies brand colors and typography to any artifact that benefits from consistent visual styling. Use when brand colors, style guidelines, visual formatting, or design standards apply to documents, presentations, or other artifacts.
license: Complete terms in LICENSE.txt
---

# Brand Styling

## Overview

Use this skill to apply consistent brand identity and style to artifacts.
Configure your brand palette and typography via environment variables or
by providing the values directly in the conversation.

**Keywords**: branding, corporate identity, visual identity, post-processing,
styling, brand colors, typography, visual formatting, visual design

## Brand Guidelines

### Colors

Brand colors are deployment-specific. The operator should configure them
via environment variables or provide them in the system prompt. The
following placeholders show the expected structure:

**Main Colors:**

- Dark: `<BRAND_COLOR_DARK>` — Primary text and dark backgrounds
- Light: `<BRAND_COLOR_LIGHT>` — Light backgrounds and text on dark
- Mid Gray: `<BRAND_COLOR_MID_GRAY>` — Secondary elements
- Light Gray: `<BRAND_COLOR_LIGHT_GRAY>` — Subtle backgrounds

**Accent Colors:**

- Primary accent: `<BRAND_ACCENT_PRIMARY>`
- Secondary accent: `<BRAND_ACCENT_SECONDARY>`
- Tertiary accent: `<BRAND_ACCENT_TERTIARY>`

If no brand colors are configured, use neutral defaults:
- Dark: `#1a1a1a`, Light: `#f8f8f8`, Mid Gray: `#aaaaaa`, Light Gray: `#e0e0e0`
- Accents: `#0066cc`, `#00aa66`, `#cc6600`

### Typography

- **Headings**: Configured via `BRAND_FONT_HEADING` env var (fallback: Arial)
- **Body Text**: Configured via `BRAND_FONT_BODY` env var (fallback: Georgia)
- Fonts should be pre-installed in your environment for best results

## Features

### Smart Font Application

- Applies heading font to headings (24pt and larger)
- Applies body font to body text
- Automatically falls back to Arial/Georgia if custom fonts unavailable
- Preserves readability across all systems

### Text Styling

- Headings (24pt+): heading font
- Body text: body font
- Smart color selection based on background
- Preserves text hierarchy and formatting

### Shape and Accent Colors

- Non-text shapes use accent colors
- Cycles through configured accent colors
- Maintains visual interest while staying on-brand

## Technical Details

### Font Management

- Uses system-installed fonts when available
- Provides automatic fallback to Arial (headings) and Georgia (body)
- No font installation required — works with existing system fonts
- For best results, pre-install your configured fonts in your environment

### Color Application

- Uses RGB color values for precise brand matching
- Applied via python-pptx's RGBColor class
- Maintains color fidelity across different systems
