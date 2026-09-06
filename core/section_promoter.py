# SPDX-License-Identifier: MIT
# ============================================================
# SECTION PROMOTER — vocabulary-driven heading recovery
#
# Problem this solves:
#   Many enterprise documents (AiNxt release notes, BRDs, FSDs) are authored
#   in Word using *bold or plain text* for section titles instead of the
#   "Heading 1/2" styles. core/document_parser.parse_docx() correctly only
#   emits "##" when an explicit heading style is set, so those documents
#   arrive at store.docs_store._chunk_document_structured() with no headings
#   at all. The structured chunker then degrades to "no parent + empty
#   section_path", which is the root cause of cross-section blending and
#   fabricated citations observed in the retrieval evaluation.
#
# How this works:
#   We maintain a small, domain-curated vocabulary of expected section names
#   (e.g. "Release Summary", "Prerequisites", "Installation Procedure",
#   "Checklist"). After parsing but BEFORE chunking, we scan the Markdown
#   line-by-line. When a stand-alone line matches a vocabulary entry, we
#   rewrite it to "## <Section Name>" so the downstream chunker treats it
#   as a section boundary.
#
# Safety:
#   - Idempotent: lines that already start with "#" are left alone.
#   - Conservative: we only promote stand-alone short lines that match the
#     vocabulary exactly (case-insensitive). We never rewrite inline prose.
#   - Disabled-by-default for unknown doc kinds: callers pass `doc_kind`,
#     and we look up that kind's vocabulary. If no vocabulary is registered
#     for the kind, we return the text unchanged.
#   - Never alters table rows, list items, or lines with trailing punctuation
#     that looks like a sentence.
# ============================================================

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from core.logger import logger


# ----------------------------------------------------------------------
# Domain vocabularies
#
# Each entry is a tuple: (canonical_title, heading_level, synonyms)
#   - canonical_title is what we emit in the promoted heading (preserves
#     the exact casing/spacing the retrieval prompt will quote back).
#   - heading_level is the markdown depth (1 = "#", 2 = "##", 3 = "###").
#     We default to "##" because release-note authors typically use the
#     document title as the level-1 heading.
#   - synonyms is the list of strings we accept as matches (case-insensitive,
#     whitespace-collapsed). Always include the canonical title itself.
#
# Add new doc kinds here as they appear in production. Keep the lists short
# and exact — broad fuzzy matching causes false positives (e.g. matching
# "Prerequisites" inside a paragraph that just mentions the word).
# ----------------------------------------------------------------------

# Aliases / common misspellings seen in AiNxt release notes.
_AiNxt_RELEASE_NOTE_SECTIONS: List[Tuple[str, int, List[str]]] = [
    ("Release Summary",        2, ["Release Summary"]),
    ("Release Details",        2, ["Release Details"]),
    ("Change Description",     2, ["Change Description"]),
    ("Change Type",            2, ["Change Type"]),
    ("Special Instructions",   2, ["Special Instructions"]),
    ("Database Changes",       3, ["Database Changes", "DB Changes", "3.1 Database Changes"]),
    ("Configuration Changes",  3, ["Configuration Changes", "3.2 Configuration changes"]),
    ("Script Changes",         3, ["Script Changes", "3.3 Script Changes"]),
    ("GUI WAR Files",          3, ["GUI WAR Files", "3.4 GUI WAR Files"]),
    ("Installation Guide",     3, ["Installation Guide", "3.5 Installation Guide"]),
    ("Build URL",              3, ["Build URL", "3.6 Build URL"]),
    ("Installation Procedure", 2, ["Installation Procedure"]),
    ("Impact Analysis",        2, ["Impact Analysis"]),
    ("Monitoring the changes", 3, ["Monitoring the changes"]),
    ("Release Rollback",       2, ["Release Rollback"]),
    ("Test Cases",             2, ["Test Cases"]),
    ("Prerequisites",          2, ["Prerequisites", "Pre-requisites", "Pre Requisites"]),
    ("Checklist",              2, ["Checklist", "Check List"]),
]


# HR / People policy documents authored in Word that arrive as plain bold
# section titles or colon-terminated labels ("Purpose:", "Scope:"). Many of
# these labels are also semantically distinct from the same words used inline
# in prose, which is why we still only promote stand-alone short lines.
_AiNxt_HR_POLICY_SECTIONS: List[Tuple[str, int, List[str]]] = [
    ("Document History",            2, ["Document History"]),
    ("Table of Contents",           2, ["Table of Contents"]),
    ("Purpose",                     2, ["Purpose", "Purpose:"]),
    ("Coverage",                    2, ["Coverage", "Coverage:"]),
    ("Scope",                       2, ["Scope", "Scope:"]),
    ("Procedures & Guidelines",     2, ["Procedures & Guidelines", "Procedures & Guidelines:",
                                        "Procedures and Guidelines"]),
    ("Payout Guidelines & Matrix",  2, ["Payout Guidelines & Matrix",
                                        "Payout Guidelines & Matrix:",
                                        "Payout Guidelines and Matrix"]),
    ("Referral Payout Amount",      3, ["Referral Payout Amount", "Referral Payout Amount:"]),
    ("Separation cases",            3, ["Separation cases", "Separation Cases"]),
    ("Review",                      2, ["Review", "Review:"]),
    ("Claim Form",                  2, ["Claim Form", "Employee Referral Incentive Claim Form"]),
    ("Declaration",                 3, ["Declaration", "Declaration:"]),
    ("HR Approval",                 3, ["HR Approval", "HR Approval:"]),
    ("Eligibility",                 2, ["Eligibility", "Eligibility:"]),
    ("Definitions",                 2, ["Definitions", "Definitions:"]),
    ("Roles & Responsibilities",    2, ["Roles & Responsibilities",
                                        "Roles and Responsibilities"]),
    ("Compliance",                  2, ["Compliance", "Compliance:"]),
    ("Effective Date",              3, ["Effective Date", "Effective Date:"]),
    ("Annexure",                    2, ["Annexure", "Annexures"]),
]


# Map: doc_kind → vocabulary
# Caller passes one of these kinds (see _VOCAB_BY_KIND.keys()). Anything else
# returns the original text unchanged, so unknown / legacy documents never
# get accidentally rewritten.
_VOCAB_BY_KIND = {
    "RELEASE_NOTE":  _AiNxt_RELEASE_NOTE_SECTIONS,
    "SETTLENXT":     _AiNxt_RELEASE_NOTE_SECTIONS,   # common alias used by uploads
    "RUPAY":         _AiNxt_RELEASE_NOTE_SECTIONS,
    # HR / People policies, referral guidelines, code of conduct, etc.
    "HR_POLICY":     _AiNxt_HR_POLICY_SECTIONS,
    "POLICY":        _AiNxt_HR_POLICY_SECTIONS,
    "GUIDELINE":     _AiNxt_HR_POLICY_SECTIONS,
}


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------

# Lines we never promote — these are clearly content, not section titles.
_LINE_REJECT_RE = re.compile(
    r"^("
    r"\|"                          # table row
    r"|[-*+]\s"                    # bullet list
    r"|\d+[.)]\s"                  # ordered list ("1. ", "1) ")
    r"|\s*$"                       # blank line
    r"|#"                          # already a heading
    r")"
)

# Aggressively trim a candidate before matching:
#  - strip surrounding markdown bold/italic markers (**Title**, *Title*, _Title_)
#  - collapse internal whitespace
#  - strip trailing punctuation that authors sometimes add (": ", ".")
_STRIP_DECOR_RE = re.compile(r"^[*_\s]+|[*_\s:.\u00a0]+$")
_WS_RE = re.compile(r"\s+")


def _normalize_candidate(line: str) -> str:
    """Reduce a line to a comparable key: strip decoration, collapse spaces, lowercase."""
    s = _STRIP_DECOR_RE.sub("", line)
    s = _WS_RE.sub(" ", s).strip()
    return s.lower()


def _build_lookup(vocab: Iterable[Tuple[str, int, List[str]]]) -> dict:
    """Flatten (canonical, level, synonyms) into {normalized_synonym: (canonical, level)}."""
    lookup: dict = {}
    for canonical, level, synonyms in vocab:
        for syn in synonyms:
            key = _WS_RE.sub(" ", syn).strip().lower()
            # Last write wins — synonyms registered later override earlier ones,
            # which lets a doc kind override a generic vocabulary if needed.
            lookup[key] = (canonical, level)
    return lookup


# Cache per doc_kind so we don't rebuild the lookup on every chunk call.
_LOOKUP_CACHE: dict = {}


def _lookup_for_kind(doc_kind: Optional[str]) -> Optional[dict]:
    if not doc_kind:
        return None
    key = doc_kind.upper().strip()
    if key not in _VOCAB_BY_KIND:
        return None
    cached = _LOOKUP_CACHE.get(key)
    if cached is not None:
        return cached
    lookup = _build_lookup(_VOCAB_BY_KIND[key])
    _LOOKUP_CACHE[key] = lookup
    return lookup


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

# Length window for plausible section-title lines. Tuned empirically against
# AiNxt release notes — titles are short. Very long lines are almost always
# prose that happens to contain a vocabulary word.
_MIN_TITLE_LEN = 3
_MAX_TITLE_LEN = 80



# Words that indicate a line is a sentence/instruction, not a heading.
# Headings are noun phrases — they don't contain action verbs or start
# with articles/prepositions.
_HEADING_REJECT_FIRST_WORDS: frozenset = frozenset({
    # Articles / demonstratives
    "the", "a", "an", "this", "these", "those", "all", "any", "each",
    # Prepositions that start sentences
    "for", "in", "on", "by", "with", "to", "of", "at", "from",
    "if", "when", "where", "how", "what", "which", "who",
    # Instruction / callout words
    "note", "important", "warning", "caution", "please",
    "see", "refer", "step", "steps",
})

_HEADING_REJECT_VERBS: frozenset = frozenset({
    "is", "are", "was", "were", "must", "should", "shall", "will", "would",
    "can", "could", "may", "might", "do", "does", "did", "have", "has", "had",
    "see", "refer", "note", "initiate", "submit", "send", "file", "provide",
    "ensure", "check", "use", "apply", "follow", "contact", "review",
})


def _is_bold_heading(text: str) -> bool:
    """
    Return True when a bold-only line is a section heading, not a bold paragraph.

    A line is treated as a heading when ALL of the following hold:
      1. Does not end with sentence-ending punctuation (. ! ? , : ;)
         — body sentences end with punctuation; headings typically don't
      2. Does not contain a comma — comma-separated lists are not headings
         (e.g. "**Visa, Mastercard, Amex**" is a list, not a heading)
      3. Does not start with an article, preposition, or instruction word
         (e.g. "**The card issuer...**", "**For other Networks...**",
          "**Note: ...**", "**See Section 6.3**")
      4. Does not contain a verb word anywhere in the text
         (e.g. "**See Section 6.3 for details**" contains 'see')
    """
    if not text:
        return False
    # Rule 1: trailing punctuation → sentence, not heading
    if text[-1] in ".!?,:;":
        return False
    # Rule 2: comma → list or clause, not heading
    if "," in text:
        return False
    # Rule 3: starts with article / preposition / instruction word
    first_word = text.split()[0].lower().rstrip(":") if text.split() else ""
    if first_word in _HEADING_REJECT_FIRST_WORDS:
        return False
    # Rule 4: contains a verb word
    words = {w.lower().strip(".,;:()[]") for w in text.split()}
    if words & _HEADING_REJECT_VERBS:
        return False
    return True


def _promote_bold_lines(text: str) -> Tuple[str, int]:
    """
    Universal pass: promote **bold-only lines** to ## headings.

    This handles the extremely common case where document authors use bold
    Normal-style paragraphs as section titles instead of Word Heading styles.
    Docling's Word backend emits these as **text** in its markdown output —
    they are structurally headings but syntactically just bold text.

    Without this promotion:
      - _chunk_document_structured() sees no # headings → one flat section
      - Every chunk gets section_path="" → no section metadata
      - _make_section_map() returns [] → Coverage tier returns only first 8,000 chars
      - Deep sections (e.g. "Arbitration Time Limits") never reach the LLM

    Rules (conservative — avoids false positives on bold paragraphs):
      1. The ENTIRE line content is wrapped in **...** (bold markdown)
      2. Text is between 3 and 120 chars
      3. Passes _is_bold_heading() — rejects sentences, lists, instructions
      4. Only applied when the document has < 3 existing # headings
         — if the document already has proper headings, trust them

    Heading level:
      ≤ 60 chars → ## (major section)
      61–120 chars → ### (sub-section)

    Returns (promoted_text, count_promoted).
    """
    existing = len(re.findall(r"^#{1,6}\s", text, re.MULTILINE))
    if existing >= 3:
        return text, 0

    bold_re = re.compile(r"^\*{2}(?P<text>[^*\n]{3,120}?)\*{2}\s*$")
    out: List[str] = []
    promoted = 0
    for line in text.splitlines():
        m = bold_re.match(line)
        if m:
            txt = m.group("text").strip()
            if _is_bold_heading(txt):
                prefix = "##" if len(txt) <= 60 else "###"
                out.append(f"{prefix} {txt}")
                promoted += 1
                continue
        out.append(line)
    return "\n".join(out), promoted


def promote_sections(
        text: str,
        doc_kind: Optional[str] = None,
) -> Tuple[str, dict]:
    """
    Scan Markdown text and promote stand-alone section-title lines to ATX
    headings so the downstream structured chunker can attach section_path
    metadata.

    Two promotion passes run in sequence:

    Pass 1 — Universal bold-line promotion (always runs):
      Promotes **bold-only lines** to ## / ### headings. Handles documents
      where section titles use bold Normal-style paragraphs instead of Word
      Heading styles — a pattern Docling's Word backend cannot detect.
      Only fires when the document has < 3 existing # headings (guard).

    Pass 2 — Vocabulary-driven promotion (runs when doc_kind is registered):
      Promotes stand-alone lines that match a curated vocabulary of known
      section names for the given doc_kind (e.g. "RELEASE_NOTE", "HR_POLICY").
      Conservative: exact match only, no fuzzy matching.

    Args:
        text:     Markdown produced by core.document_parser.parse_file.
        doc_kind: Domain key used to pick the vocabulary
                  (e.g. "RELEASE_NOTE" / "SETTLENXT"). When None or unknown
                  only Pass 1 runs.

    Returns:
        (promoted_text, stats)
          promoted_text — input with matching lines rewritten to "## Title"
          stats         — {
              "doc_kind":              <echoed>,
              "promoted":              <count of lines rewritten>,
              "matched_sections":      [<canonical titles in document order>],
              "vocabulary_size":       <int>,
              "skipped":               False (Pass 1 always runs),
              "bold_promoted":         <count from Pass 1>,
          }

    Idempotency:
        Running promote_sections twice on the same text yields identical
        output. Lines that are already headings are skipped explicitly.
    """
    if not text:
        return text, {"doc_kind": doc_kind, "promoted": 0,
                      "matched_sections": [], "vocabulary_size": 0,
                      "skipped": True, "bold_promoted": 0}

    # ── Pass 1: Universal bold-line promotion ─────────────────────────────────
    text, _bold_count = _promote_bold_lines(text)
    if _bold_count:
        logger.info(
            f"section_promoter: universal bold promotion — "
            f"promoted {_bold_count} **bold** lines → ## headings "
            f"(doc_kind={doc_kind})"
        )

    lookup = _lookup_for_kind(doc_kind)
    if lookup is None:
        return text, {"doc_kind": doc_kind, "promoted": _bold_count,
                      "matched_sections": [], "vocabulary_size": 0,
                      "skipped": True, "bold_promoted": _bold_count}

    out_lines: List[str] = []
    matched: List[str] = []
    promoted = 0

    for raw in text.splitlines():
        # Fast reject: anything that begins like a table / list / heading /
        # blank line cannot be a promotable section title.
        if _LINE_REJECT_RE.match(raw):
            out_lines.append(raw)
            continue

        # Length gate — section titles are short.
        stripped = raw.strip()
        if len(stripped) < _MIN_TITLE_LEN or len(stripped) > _MAX_TITLE_LEN:
            out_lines.append(raw)
            continue

        key = _normalize_candidate(raw)
        hit = lookup.get(key)
        if hit is None:
            out_lines.append(raw)
            continue

        canonical, level = hit
        prefix = "#" * max(1, min(6, level))
        out_lines.append(f"{prefix} {canonical}")
        promoted += 1
        matched.append(canonical)

    stats = {
        "doc_kind":         doc_kind,
        "promoted":         promoted + _bold_count,
        "matched_sections": matched,
        "vocabulary_size":  len(lookup),
        "skipped":          False,
        "bold_promoted":    _bold_count,
    }

    if promoted:
        logger.info(
            f"section_promoter: doc_kind={doc_kind} promoted {promoted} lines "
            f"({len(set(matched))} distinct sections)"
        )

    return "\n".join(out_lines), stats


def known_doc_kinds() -> List[str]:
    """Return registered doc_kind keys — for admin diagnostics / UI dropdowns."""
    return sorted(_VOCAB_BY_KIND.keys())


def vocabulary_for(doc_kind: str) -> List[str]:
    """Return canonical section titles for the given doc_kind (diagnostics)."""
    vocab = _VOCAB_BY_KIND.get((doc_kind or "").upper().strip())
    if not vocab:
        return []
    return [canonical for canonical, _lvl, _syn in vocab]
