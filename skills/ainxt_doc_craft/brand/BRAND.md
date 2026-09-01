---
name: ainxt-brand
description: Generic brand contract for AiNxt document artifacts (docx, pptx, xlsx, pdf).
license: Apache-2.0
---

# AiNxt Document Identity — Default Brand Contract

This is the OSS default brand guide. Replace with your organisation's brand file
by setting `DOC_BRAND_FILE=brand/<YourOrg>_BRAND.md` in your `.env`.

## Typography
- **Heading font**: Arial (or Calibri as fallback)
- **Body font**: Arial, 11pt, colour `#222222` on white background
- **No decorative or script fonts**

## Colour Palette
- **Primary heading colour**: `#1F3864` (deep navy) — headings, title bands, header rows
- **Accent colour**: `#00A551` (green) — use sparingly, above 18pt or as a shape fill only
- **Body text**: `#222222` on white (`#FFFFFF`)
- **Alternating table rows**: `#F2F5FA` (light blue-grey) on odd rows

## Layout Rules
- No vertical table borders — horizontal rules only
- No centred body text — left-align all paragraphs
- No clip-art, stock photos, or emoji in formal documents
- Tables: header row in primary colour with white bold text

## Footer
- Left: `[Organisation] — Confidential`
- Right: `Page N of M`
- Font: 9pt Arial, colour `#555555`

## Slide Layouts (PPTX)
- Title slide: full-bleed primary colour background, white title text
- Content slides: white background, primary colour title bar at top (height ~1.2cm)
- No busy backgrounds or gradients on content slides

## Document Metadata
- Set document author to the platform name, not a personal name
- Set document title to the report/document title

---
*Override this file with your organisation's brand guide via `DOC_BRAND_FILE` env var.*
