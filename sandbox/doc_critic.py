# SPDX-License-Identifier: Apache-2.0
"""
Visual QA for generated documents.

`sandbox/doc_executor.py` already rasterises every build to page JPEGs
(soffice → pdftoppm). Nothing used to look at them: they went to the in-app
preview for the human and no further. This module feeds them to a vision model,
scored against the configured brand contract, so a build can be critiqued and repaired
before the user ever sees it.

Design constraints this module respects:
  - The LLM proxy's /llm/generate-image endpoint accepts ONE image per call, so
    pages are critiqued individually and the page count is hard-capped.
  - Fails OPEN. No vision model, a proxy error, unparseable output — all return
    verdict="unavailable" so the caller ships the document. A visual nicety must
    never block a deliverable (platform redact-don't-block / audit-and-proceed).
  - Every model call goes through the gateway/proxy. Never a direct vendor call.
"""
from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass, field

from core.config import PLATFORM_NAME
from core.logger import logger

# Feature flag — off means the build path behaves exactly as it did before.
ENABLE_VISION_QA = (os.getenv("ENABLE_DOC_VISION_QA", "true").lower() == "true")

# Cost/latency guard: one vision call per page reviewed.
MAX_PAGES_REVIEWED = int(os.getenv("DOC_VISION_QA_MAX_PAGES", "4"))

_SKILLS_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "ainxt_doc_craft",
)
# DOC_BRAND_FILE — relative path inside ainxt_doc_craft/ to the brand contract.
# Default: brand/BRAND.md (generic OSS brand — no org-specific colours or fonts).
# Internal/enterprise: set DOC_BRAND_FILE=brand/INTERNAL_BRAND.md in your .env.
_BRAND_PATH = os.path.join(_SKILLS_ROOT, os.getenv("DOC_BRAND_FILE", "brand/BRAND.md"))

_VERDICT_PASS: str = "pass"
_VERDICT_REVISE: str = "revise"
_VERDICT_UNAVAILABLE: str = "unavailable"


@dataclass
class PageIssue:
    page: int
    problem: str
    fix_hint: str = ""


@dataclass
class CritiqueResult:
    verdict: str = _VERDICT_UNAVAILABLE      # pass | revise | unavailable
    issues: list[PageIssue] = field(default_factory=list)
    pages_reviewed: int = 0
    model: str = ""
    note: str = ""                            # why it was unavailable, if it was

    @property
    def needs_revision(self) -> bool:
        return self.verdict == _VERDICT_REVISE and bool(self.issues)


def _load_rubric() -> str:
    """The brand contract IS the rubric — one source of truth for generation and
    verification. Falls back to a compact inline summary if the file is missing."""
    try:
        with open(_BRAND_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        # Generic OSS fallback — no org-specific branding enforced.
        return (
            "Brand contract: use consistent heading colours, a single sans-serif "
            "font (e.g. Arial or Calibri), body text on white, alternating table "
            "row shading, no vertical table borders, no centred body text, no "
            "clip-art or emoji. Include a footer with a page number."
        )


def _pages_to_review(total: int) -> list[int]:
    """Pick which pages to spend vision calls on: always the first (cover/title
    carries the most brand signal) and the last, then fill from the front."""
    if total <= MAX_PAGES_REVIEWED:
        return list(range(total))
    picks = {0, total - 1}
    i = 1
    while len(picks) < MAX_PAGES_REVIEWED and i < total - 1:
        picks.add(i)
        i += 1
    return sorted(picks)


_INSTRUCTION = """You are a document design reviewer for {org}. You are shown ONE \
rendered page of a generated {fmt} file.

Judge ONLY what is visible. Do not guess at content you cannot see. Report a problem \
only if you can point at it on this page.

Report a problem when you see any of:
- text that overflows its box, is clipped, or collides with another element
- body text that is centred, or justified
- text too low-contrast to read comfortably, or coloured status text at small size
- a table with vertical borders, an outer box, or no header fill
- leftover placeholder text (REPLACE, TODO, Lorem, Sample, xxx)
- clip-art, emoji, stock photography, a drop shadow, or a 3-D chart effect
- a heading and body at the same visual weight, so hierarchy is unreadable
- a page that is visually empty or nearly empty for no clear reason
- an obviously broken image, or a placeholder box where art should be
- crowding: no usable margin, or elements touching the page edge

Do NOT report: wording, spelling, factual accuracy, or subjective taste about the \
content. Design and legibility only.

Brand contract this page must satisfy:
{rubric}

Answer with JSON only, no prose and no code fence:
{{"verdict":"pass","issues":[]}}
or
{{"verdict":"revise","issues":[{{"problem":"<what is wrong, specifically>",\
"fix_hint":"<the concrete change to make>"}}]}}

Report at most 3 issues, most severe first. If the page is acceptable, return \
verdict "pass" with an empty issues list."""


def _parse_verdict(text: str) -> tuple[str, list[dict]]:
    """Pull the JSON verdict out of a model reply. Tolerates code fences and
    surrounding prose; returns ("", []) if nothing usable is present."""
    if not text:
        return "", []
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        candidates.append(cleaned[start:end + 1])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        verdict = str(obj.get("verdict") or "").strip().lower()
        raw_issues = obj.get("issues")
        issues = [i for i in raw_issues if isinstance(i, dict)] if isinstance(raw_issues, list) else []
        if verdict in (_VERDICT_PASS, _VERDICT_REVISE):
            return verdict, issues
    return "", []


def critique(page_images: list[bytes], fmt: str) -> CritiqueResult:
    """Review rendered pages against the brand contract.

    Never raises. Returns verdict="unavailable" whenever a judgement could not be
    formed, which the caller must treat as "ship it".
    """
    if not ENABLE_VISION_QA:
        return CritiqueResult(note="vision QA disabled by flag")
    if not page_images:
        return CritiqueResult(note="no rendered pages to review")

    try:
        from models.model_router import model_router
        vision = model_router._get_gemini()
    except Exception as exc:
        logger.warning(f"doc_critic: model router unavailable → {exc}")
        return CritiqueResult(note=f"router unavailable: {exc}")

    if vision is None or not hasattr(vision, "generate_image"):
        return CritiqueResult(note="no vision-capable gateway configured")

    rubric = _load_rubric()
    instruction = _INSTRUCTION.format(fmt=fmt.upper(), rubric=rubric, org=PLATFORM_NAME)

    issues: list[PageIssue] = []
    reviewed = 0
    model_used = ""
    saw_any_verdict = False

    for idx in _pages_to_review(len(page_images)):
        page_no = idx + 1
        try:
            b64 = base64.b64encode(page_images[idx]).decode("ascii")
            text, _in_tok, _out_tok, actual = vision.generate_image(
                prompt=f"This is page {page_no} of {len(page_images)}. Review it.",
                image_b64=b64,
                mime_type="image/jpeg",
                system_prompt=instruction,
            )
            model_used = actual or model_used
            reviewed += 1
        except Exception as exc:
            logger.warning(f"doc_critic: vision call failed on page {page_no} → {exc}")
            continue

        verdict, raw = _parse_verdict(text or "")
        if not verdict:
            logger.info(f"doc_critic: unparseable verdict for page {page_no}")
            continue
        saw_any_verdict = True
        if verdict == _VERDICT_REVISE:
            for item in raw[:3]:
                problem = str(item.get("problem") or "").strip()
                if problem:
                    issues.append(PageIssue(
                        page=page_no,
                        problem=problem[:400],
                        fix_hint=str(item.get("fix_hint") or "").strip()[:400],
                    ))

    if not saw_any_verdict:
        return CritiqueResult(
            pages_reviewed=reviewed, model=model_used,
            note="no usable verdict returned for any page",
        )

    return CritiqueResult(
        verdict=_VERDICT_REVISE if issues else _VERDICT_PASS,
        issues=issues, pages_reviewed=reviewed, model=model_used,
    )


def build_repair_prompt(fmt: str, code: str, issues: list[PageIssue]) -> str:
    """Instruction for the text model: repair the build script against the visual
    findings. Deliberately narrow — a rewrite loses content the user asked for."""
    listed = "\n".join(
        f"- page {i.page}: {i.problem}" + (f" → {i.fix_hint}" if i.fix_hint else "")
        for i in issues
    )
    return (
        f"A rendered review of the {fmt.upper()} document your script produced found "
        f"these visual defects:\n\n{listed}\n\n"
        "Return the COMPLETE corrected build script and nothing else — no commentary, "
        "no markdown fence. Fix only the defects listed. Keep every heading, table, "
        "figure and sentence of the existing content intact; do not shorten, "
        "re-order, or reword the document. Keep the same output path. Preserve the "
        "brand colours, fonts, and the existing page or slide geometry.\n\n"
        f"Current script:\n{code}"
    )


def strip_code_fence(text: str) -> str:
    """Extract code from a model reply.

    Small local models routinely ignore "return only code" and answer with a
    sentence of preamble, a fenced block, then a closing remark. Stripping only an
    outer fence leaves the prose attached and the build dies on a syntax error at
    line 1, so pull out the largest fenced block wherever it sits. Falls back to
    the whole reply when there is no fence at all.
    """
    if not text:
        return ""
    stripped = text.strip()
    blocks = re.findall(r"```(?:[a-zA-Z0-9_+-]+)?[ \t]*\r?\n(.*?)```", stripped, flags=re.DOTALL)
    if blocks:
        return max(blocks, key=len).strip()
    return stripped
