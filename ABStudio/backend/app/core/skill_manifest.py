# SPDX-License-Identifier: Apache-2.0
"""Skill manifest rendering and override-guard constants.

Progressive disclosure: instead of pasting every bundled file from a skill
folder into the system prompt, we paste only SKILL.md's body plus a manifest
listing the bundled reference docs and scripts. The LLM pulls specific
files on demand via the ``read_skill_file`` tool, and invokes bundled
scripts directly through ``code_executor`` by absolute path.

This module is the single source of truth for:

* What counts as a "domain skill" (file-emitting workflows like pptx/docx).
* How the ``## Skills`` section of the system prompt is formatted.
* The generic File-Generation override block that gets appended when no
  domain skill is attached — and the softer nudge that replaces it when one
  is.

Both the workflow engine (``app/engine/native_engine.py``) and the chat /
single-agent path (``agent_factory/pipeline.py``) call into this module so
they can't drift apart.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Optional


# Skill names that ship a complete file-emission workflow inside SKILL.md.
# When any of these is attached, the generic File-Generation override is
# replaced with a softer nudge so the skill's own steps lead.
DOMAIN_SKILL_NAMES = {"pptx", "docx", "xlsx", "pdf", "txt"}


# Maps a domain skill name to the ONLY output format/extension it is allowed
# to emit. When a domain skill is attached, the deliverable MUST use this
# format — the LLM may not silently substitute another format (e.g. attach
# `pptx` but produce a PDF). Keyed by skill name for direct lookup.
DOMAIN_SKILL_FORMAT = {
    "pptx": {"ext": ".pptx", "label": "PowerPoint presentation (.pptx)"},
    "docx": {"ext": ".docx", "label": "Word document (.docx)"},
    "xlsx": {"ext": ".xlsx", "label": "Excel spreadsheet (.xlsx)"},
    "pdf": {"ext": ".pdf", "label": "PDF document (.pdf)"},
    "txt": {"ext": ".txt", "label": "plain text file (.txt)"},
}


# One-week safety valve: set SKILL_DISCLOSURE=eager to fall back to the
# legacy whole-blob injection while we validate progressive disclosure.
def _disclosure_mode() -> str:
    return (os.getenv("SKILL_DISCLOSURE") or "progressive").lower()


# ---------------------------------------------------------------------------
# Prompt fragments — appended to (or replacing) the generic override block.
# ---------------------------------------------------------------------------

# Single source of truth for the "stay inside OUTPUT_DIR" guidance. Reused
# wherever a system prompt or tool description tells the LLM how to emit
# files; drift between sites was the original reason `outputs/` subfolders
# kept leaking onto disk.
NO_SUBDIRS_CLAUSE = (
    "no subdirectories — do NOT create 'outputs/' or 'generated/' folders "
    "inside OUTPUT_DIR"
)

GENERIC_FILE_GENERATION_OVERRIDE = (
    "\n\n## File Generation\n\n"
    "Only call `code_executor` when the user EXPLICITLY asks for a file "
    "(PDF, PPTX, DOCX, Excel, CSV, chart, image, etc.) by name or clearly "
    "describes a file-output deliverable. Do NOT call `code_executor` for "
    "general questions, conversations, summaries, explanations, or other "
    "text-only answers.\n\n"
    "A short confirmatory reply ('yes', 'go ahead', 'proceed', 'do it') is "
    "NOT by itself a request for a file. Only treat it as confirmation when "
    "YOU just proposed generating a specific file in the immediately "
    "preceding turn. If the user's intent is unclear, ASK a clarifying "
    "question first — do not assume a file is wanted.\n\n"
    "When you do generate a file:\n"
    "  - Use PYTHON ONLY. Node.js / npm / require() and any JavaScript "
    "approach are NOT supported in the sandbox and will crash the run. Use "
    "pure-Python libraries: python-docx, python-pptx, reportlab, pypdf, "
    "openpyxl, pandas, matplotlib.\n"
    f"  - Write output files DIRECTLY to OUTPUT_DIR ({NO_SUBDIRS_CLAUSE}).\n"
    "  - You may install a missing Python package on the fly:\n"
    "      subprocess.run([sys.executable, '-m', 'pip', 'install', 'pkg'], check=True)\n"
    "  - If the tool errors, fix and retry.\n"
    "  - After success, share the download_url from the 'generated_files' array."
)

DOMAIN_SKILL_NUDGE = (
    "\n\n## File Generation\n\n"
    "A domain skill is attached above. Only act on it when the user "
    "EXPLICITLY asks for a file in that skill's domain — do not generate a "
    "file for general questions or short confirmatory replies unless you "
    "just proposed a specific file in the immediately preceding turn. "
    "If intent is unclear, ASK first.\n\n"
    "When the user does ask for a file, **follow the skill's workflow as "
    "written** — pick a palette, design layout, then render. Do NOT rush "
    "to `code_executor` with arbitrary code; first read the relevant "
    "bundled reference file via `read_skill_file(skill, rel_path)` if you "
    "need deeper API patterns, then invoke bundled scripts via "
    "`code_executor` using the absolute paths listed in the manifest.\n\n"
    "**Sandbox constraints (override any conflicting skill instructions):** "
    "the `code_executor` sandbox is PYTHON ONLY. Use pure-Python libraries "
    "(python-docx, python-pptx, openpyxl, reportlab, pypdf, markitdown, "
    "pandas, matplotlib). Do NOT use Node.js / npm / require(), pandoc, "
    "soffice / LibreOffice, pdftoppm, or any other shell CLI — they are not "
    "available and will crash the run. If the skill's examples use those, "
    "translate the same intent into equivalent Python.\n\n"
    f"Write output files DIRECTLY to OUTPUT_DIR ({NO_SUBDIRS_CLAUSE}) using "
    "os.path.join(OUTPUT_DIR, 'name.ext') — never a bare/relative filename or "
    "hardcoded path. After success, share the download_url from the "
    "'generated_files' array."
)


# ---------------------------------------------------------------------------
# Manifest renderer
# ---------------------------------------------------------------------------

def _normalize_path(p: str) -> str:
    """Forward-slash paths for prompt readability and copy-paste safety.

    Python on Windows accepts forward slashes in ``subprocess.run`` so this
    is safe for both dev (Windows) and prod (Linux Docker).
    """
    return (p or "").replace("\\", "/")


def render_skill_section(resolved: List[dict]) -> str:
    """Render the ``## Skills`` block of the system prompt.

    Each entry in ``resolved`` is::

        {
          "name": "pptx",
          "body": "<SKILL.md body with frontmatter stripped>",
          "files": [
            {
              "rel_path":    "pythonpptx.md",
              "description": "python-pptx API patterns and palette recipes",
              "kind":        "reference",  # or "script"
              "abs_path":    "/app/skills/ainxt-skills/pptx/pythonpptx.md",
              "size_bytes":  12345,
            },
            ...
          ],
        }

    Empty ``files`` (e.g. SkillFactory-generated skills) renders only the
    body — no empty headers.

    When ``SKILL_DISCLOSURE=eager`` the manifest collapses into a single
    blob equivalent to the legacy behaviour — ``body`` is expected to be
    the full concatenated content in that mode, but in practice we keep
    using the progressive layout because the new seeder no longer stores
    the full blob in ``content``. The flag is a safety valve, not a
    parallel codepath.
    """
    if not resolved:
        return ""

    if _disclosure_mode() == "eager":
        # Eager fallback: just emit each body separated, no manifest.
        # ``body`` is SKILL.md only — the legacy blob isn't stored anymore,
        # so this is essentially the same as progressive without the file
        # listings. Acceptable since the flag exists only to disable the
        # manifest, not to resurrect the deleted whole-blob path.
        blocks = []
        for s in resolved:
            body = (s.get("body") or "").strip()
            if not body:
                continue
            blocks.append(f"### {s['name']}\n\n{body}")
        return "## Skills\n\n" + "\n\n---\n\n".join(blocks) if blocks else ""

    attached_names = ", ".join(f"`{s.get('name','')}`" for s in resolved if s.get("name"))
    parts: List[str] = [
        "## Skills",
        (
            f"\nAttached skills: {attached_names}. You may only call "
            f"`read_skill_file` with one of these skill names — other skills "
            f"are not accessible from this agent."
        ),
    ]
    for s in resolved:
        name = s.get("name") or ""
        body = (s.get("body") or "").strip()
        files = s.get("files") or []

        parts.append(f"\n### {name}")
        if body:
            parts.append(f"\n{body}")

        refs = [f for f in files if f.get("kind") == "reference"]
        scripts = [f for f in files if f.get("kind") == "script"]

        if refs:
            lines = [
                f"\n**Bundled reference files** (load on demand via "
                f"`read_skill_file(\"{name}\", \"<rel_path>\")`):"
            ]
            for f in refs:
                desc = (f.get("description") or "").strip()
                suffix = f" — {desc}" if desc else ""
                lines.append(f"- `{f['rel_path']}`{suffix}")
            parts.append("\n".join(lines))

        if scripts:
            lines = [
                "\n**Bundled scripts** (invoke via `code_executor` using the "
                "absolute path; do NOT paste the source):"
            ]
            for f in scripts:
                desc = (f.get("description") or "").strip()
                suffix = f" — {desc}" if desc else ""
                abs_p = _normalize_path(f.get("abs_path") or "")
                lines.append(f"- `{abs_p}`{suffix}")
            parts.append("\n".join(lines))

    return "\n".join(parts).strip()


def has_domain_skill(skill_names: Iterable[str]) -> bool:
    """True if any of ``skill_names`` is a recognised file-emission domain skill."""
    return bool(set(skill_names) & DOMAIN_SKILL_NAMES)


def format_lock_clause(skill_names: Iterable[str]) -> str:
    """Build a HARD output-format constraint from attached domain skills.

    Each recognised domain skill dictates exactly one allowed output format
    (pptx→.pptx, docx→.docx, xlsx→.xlsx, pdf→.pdf, txt→.txt). When such a
    skill is attached, the deliverable MUST use that format — the model may
    NOT substitute another format (the original bug: `pptx` attached but a
    PDF produced). Returns '' when no domain skill is attached.
    """
    attached = [n for n in skill_names if n in DOMAIN_SKILL_FORMAT]
    if not attached:
        return ""

    # Preserve order but de-duplicate.
    seen: List[str] = []
    for n in attached:
        if n not in seen:
            seen.append(n)

    allowed = [DOMAIN_SKILL_FORMAT[n] for n in seen]
    allowed_ext_set = {a["ext"] for a in allowed}
    exts = ", ".join(f"`{a['ext']}`" for a in allowed)
    labels = " and ".join(a["label"] for a in allowed)
    # Every OTHER recognised format the model must not emit when a specific
    # one is locked (exclude the allowed extension itself).
    other_exts = ", ".join(
        sorted(
            v["ext"] for v in DOMAIN_SKILL_FORMAT.values()
            if v["ext"] not in allowed_ext_set
        )
    )

    return (
        "\n\n### Output format is LOCKED (mandatory)\n\n"
        f"An output-format skill is attached, so the deliverable MUST be a "
        f"{labels}. The ONLY acceptable output extension is {exts}. "
        f"You MUST NOT produce any other format ({other_exts}) — in "
        f"particular, do NOT fall back to PDF or plain text when the attached "
        f"skill specifies a different format.\n\n"
        f"Save the file to OUTPUT_DIR with the {exts} extension. If your first "
        f"instinct is a different format, discard it and use the locked format. "
        f"This constraint OVERRIDES any conflicting guidance, the "
        f"`code_executor` tool description, and your own preference — the "
        f"attached skill decides the format, not the phrasing of the request."
    )


def enforce_read_skill_file_scope(
    arguments: dict, allowed_skills: Iterable[str],
) -> str:
    """Return an error string if a ``read_skill_file`` call escapes scope, else ''.

    The LLM may pass any skill name to ``read_skill_file`` — without scoping,
    attaching ``pptx`` would give the model read access to every other skill
    in the catalog (pdf, docx, etc.). This guard enforces "attached = only
    accessible" at dispatch time, so prompt injection or sloppy reasoning
    can't widen the surface.

    Empty / missing ``allowed_skills`` means "no skills attached" → block any
    call. The error string is returned to the LLM as the tool result so it
    can recover (call again with a correct skill name).
    """
    allowed = {str(s).strip() for s in (allowed_skills or []) if str(s).strip()}
    requested = str((arguments or {}).get("skill") or "").strip()
    if not requested:
        return "ERROR: read_skill_file requires a 'skill' argument."
    if requested not in allowed:
        allowed_list = ", ".join(sorted(allowed)) if allowed else "(none)"
        return (
            f"ERROR: skill '{requested}' is not attached to this agent. "
            f"You may only call read_skill_file with skill in: {allowed_list}."
        )
    return ""


def file_generation_directive(
    code_executor_available: bool,
    attached_skill_names: Iterable[str],
) -> str:
    """Pick the right File-Generation prompt fragment.

    * No code_executor → empty string (no directive makes sense).
    * code_executor + domain skill attached → soft nudge that defers to the
      skill's workflow.
    * code_executor + only non-domain skills (or none) → the original
      aggressive override.
    """
    if not code_executor_available:
        return ""
    names = list(attached_skill_names or [])
    if has_domain_skill(names):
        # Append the hard format-lock so the attached domain skill strictly
        # dictates the output format (pptx→.pptx, docx→.docx, etc.).
        return DOMAIN_SKILL_NUDGE + format_lock_clause(names)
    return GENERIC_FILE_GENERATION_OVERRIDE


# ---------------------------------------------------------------------------
# Sample document (look-and-feel reference) directive
# ---------------------------------------------------------------------------
# When the user attaches a sample document to a generation agent
# (Agent Studio → Sample document), the engine surfaces its path via
# SAMPLE_DOC_PATH inside the code_executor sandbox and appends the block
# below to the system prompt. The block is deliberately **guidance, not
# a constraint**: the sample supplies branding cues (logos, fonts,
# heading order, master slides, header/footer), and the agent stays
# free to add, drop, or reorder sections as the task demands.
#
# Kept out of the generic / domain-skill nudges above so a future rewrite
# of either directive can't accidentally strip this behaviour, and so
# agents WITHOUT a sample see zero extra tokens.

def sample_doc_directive(
    sample_doc: Optional[dict],
) -> str:
    """Return the "sample document available" prompt block, or ``''``.

    Renders only when ``sample_doc`` is a non-empty dict with both a
    ``path`` and a ``kind``. Optional ``notes`` (user guidance captured
    from the editor) is appended verbatim so instructions like "keep
    the cover page but rewrite everything else" reach the model.

    The block references SAMPLE_DOC_PATH / SAMPLE_DOC_KIND /
    SAMPLE_DOC_DIR — the same names injected by
    ``agent_factory/pipeline.py::ToolDispatcher._run_in_sandbox`` — so
    a copy-pasted snippet from the prompt works verbatim inside
    ``code_executor``.
    """
    if not isinstance(sample_doc, dict):
        return ""
    path = (sample_doc.get("path") or "").strip()
    kind = (sample_doc.get("kind") or "").strip().lower()
    if not path or not kind:
        return ""
    display_name = (sample_doc.get("name") or "").strip()
    notes = (sample_doc.get("notes") or "").strip()

    header = (
        "\n\n## Sample document (guidance, not a constraint)\n\n"
        f"The user attached a **sample** at `SAMPLE_DOC_PATH` "
        f"(kind: `{kind}`"
        + (f", filename: `{display_name}`" if display_name else "")
        + "). Treat it as a **look-and-feel reference**: mirror its "
          "branding (logos, colours, fonts, header/footer, slide "
          "masters, heading order, list/table styles) and use its "
          "layouts as a pattern library. You are FREE to adapt "
          "structure and content — add sections/slides the task needs, "
          "drop ones that don't apply, reorder freely."
    )

    recipes = (
        "\n\n**Recommended pattern** — do NOT build a blank document from scratch:\n\n"
        "- **docx** — `doc = Document(os.environ[\"SAMPLE_DOC_PATH\"])`; "
        "keep its styles / headers / footers / images; write new content "
        "via `doc.add_heading(text, level=N)` and `doc.add_paragraph(text, "
        "style=\"List Bullet\")` (styles are inherited automatically); "
        "delete sample paragraphs that don't fit the task; save into "
        "`OUTPUT_DIR`.\n"
        "- **pptx** — `prs = Presentation(os.environ[\"SAMPLE_DOC_PATH\"])`; "
        "either edit text on existing slides that fit, or add new slides "
        "using `prs.slide_layouts[i]` (the slide master — logo, theme, "
        "colours — is inherited automatically). Pick the slide count the "
        "task actually needs; the sample is a pattern library, not a "
        "fixed sequence.\n"
        "- **xlsx** — `wb = load_workbook(os.environ[\"SAMPLE_DOC_PATH\"])`; "
        "reuse header styling and any embedded brand images; add or "
        "remove sheets as needed.\n"
        "- **pdf** — visual reference only. Call `read_document` on "
        "`SAMPLE_DOC_PATH` to study its content and layout, then generate "
        "the requested output document while echoing the branding cues."
    )

    study_only = (
        "\n\n**When opening-as-base is the wrong fit** (sample is a BRD "
        "but the task is a one-page memo, sample is a deck but the task "
        "is a spreadsheet, sample is a PDF but the output is a docx): "
        "call `read_document({\"file_path\": os.environ[\"SAMPLE_DOC_PATH\"]})` "
        "to study the sample, note its fonts / colours / heading order / "
        "section conventions, and generate the new document from scratch "
        "while echoing those cues."
    )

    rules = (
        "\n\n**Rules of thumb:**\n"
        "- Prefer opening-as-base when the task is the *same kind* of "
        "document as the sample (BRD → BRD, quarterly deck → quarterly "
        "deck).\n"
        "- Prefer study-only when the task is a *different kind* of "
        "document.\n"
        "- Never invent branding that contradicts the sample. Keep the "
        "sample's own palette; do not substitute a different colour.\n"
        "- Never treat the sample's TEXT as required content — its "
        "content is a pattern, not a source of truth.\n"
        "- The sample is optional guidance. If it genuinely doesn't help "
        "the task, ignore it and generate as usual."
    )

    block = header + recipes + study_only + rules
    if notes:
        block += (
            "\n\n**User's guidance on the sample** (follow this if it "
            f"conflicts with the rules of thumb above):\n{notes}"
        )
    return block
