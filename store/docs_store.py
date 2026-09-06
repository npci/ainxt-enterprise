# SPDX-License-Identifier: MIT
# ============================================================
# DOCUMENT KNOWLEDGE BASE STORE
# upload → parse → chunk → embed → store (pgvector only)
# ============================================================

import re
import json
import uuid
import hashlib
import tempfile
import os
from datetime import datetime, timezone
from typing import List, Optional

from core.logger import logger
from core.config import EMBED_SVC_URL as _EMBED_SVC_URL, RDB_CACHE
from core.kv import get_kv


def _docs_kv():
    """KV client for the docs namespace registry (DB=0)."""
    return get_kv(RDB_CACHE, decode_responses=True)


# ---------------------------------------------------------------------------
# Structure-aware document chunker
# ---------------------------------------------------------------------------

_PAGE_NUM_RE = re.compile(
    r'^\s*(?:Page\s+)?\d+(?:\s+of\s+\d+)?\s*$',
    re.IGNORECASE,
)
_SEPARATOR_RE = re.compile(r'^[\-_]{2,}\s*$')

# Matches Docling conversion-error HTML comment placeholders written by
# _convert_smart_group() in core/docling_parser.py when a page batch fails
# (after retry).  These must be stripped before chunking so error messages
# are never embedded or returned to the LLM during RAG retrieval.
# Pattern is intentionally broad — matches any <!-- conversion-error ... -->
# regardless of the exception type or page range in the comment.
_CONVERSION_ERROR_RE = re.compile(
    r'<!--\s*conversion-error\b[^>]*-->', re.IGNORECASE
)


def _clean_text(text: str) -> str:
    """
    Strip parser artifacts before chunking:
    - trailing spaces per line
    - standalone page-number lines ("47", "Page 47", "47 of 200")
    - separator lines made only of - or _
    - Docling conversion-error HTML comment placeholders
    - collapse 3+ consecutive blank lines → one paragraph break
    """
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        # Strip Docling conversion-error placeholders — never embed error messages.
        # Replaced with a blank line so surrounding paragraph spacing is preserved.
        if _CONVERSION_ERROR_RE.search(line):
            lines.append("")
        elif _PAGE_NUM_RE.match(line) or _SEPARATOR_RE.match(line):
            lines.append("")          # replace with blank so paragraph logic still works
        else:
            lines.append(line)

    # collapse runs of 3+ blank lines → 2
    cleaned: List[str] = []
    blank_run = 0
    for line in lines:
        if line == "":
            blank_run += 1
            if blank_run <= 2:
                cleaned.append(line)
        else:
            blank_run = 0
            cleaned.append(line)

    return "\n".join(cleaned).strip()


def _chunk_document(text: str, target: int = 800, overlap: int = 100) -> List[str]:
    """
    Chunk Markdown/prose at structural boundaries instead of fixed character offsets.
    Split order: heading boundary → paragraph (\\n\\n) → sentence → word.
    Tables (consecutive |…| lines) are kept as atomic units.
    Small pieces are merged up to `target` chars before being emitted.
    """
    text = _clean_text(text)
    pieces: List[str] = []
    for section in re.split(r'(?m)(?=^#{1,6}\s)', text):
        if section.strip():
            _section_to_pieces(section, pieces, target)

    chunks = _merge_pieces(pieces, target)

    if overlap and len(chunks) > 1:
        out = [chunks[0]]
        for i in range(1, len(chunks)):
            tail = chunks[i - 1][-overlap:]
            # trim to last word boundary so we don't start mid-word
            space = tail.rfind(' ')
            if space > 0:
                tail = tail[space + 1:]
            out.append(tail + " " + chunks[i])
        return out

    return chunks


def _is_table_line(stripped: str) -> bool:
    """
    Return True when a stripped line is part of a Markdown table.

    Handles all three patterns that appear in Docling output:
      1. Standard GFM row   : "| col | col |"   — starts with |
      2. Separator row       : "|---|---|"        — fullmatch of pipe/dash/colon
      3. Docling artifact    : "1.2 | col | col |" — version prefix before first |

    Rule: any line containing ≥ 2 pipe characters that is NOT a heading
    or blockquote is treated as a table row.  Two pipes is the minimum
    for a valid GFM table cell (opening + closing pipe).
    """
    if not stripped:
        return False
    # Headings and blockquotes are never table rows
    if stripped.startswith('#') or stripped.startswith('>'):
        return False
    return stripped.count('|') >= 2


def _section_to_pieces(text: str, out: List[str], max_piece: int) -> None:
    """
    Within one heading-section split into: table blocks (atomic) and paragraphs.
    Paragraphs larger than max_piece are split further at sentence boundaries.

    Table detection uses _is_table_line() which catches standard GFM rows,
    separator rows, AND Docling artifacts like "1.2 | col | col |" that do
    not start with a pipe character.
    """
    para_buf: List[str] = []
    table_buf: List[str] = []
    in_table = False

    def flush_para() -> None:
        if not para_buf:
            return
        para = "\n".join(para_buf).strip()
        para_buf.clear()
        if not para:
            return
        if len(para) > max_piece:
            out.extend(_sentence_split(para, max_piece))
        else:
            out.append(para)

    def flush_table() -> None:
        if not table_buf:
            return
        tbl = "\n".join(table_buf).strip()
        table_buf.clear()
        if tbl:
            out.append(tbl)

    for line in text.splitlines():
        stripped = line.strip()
        if _is_table_line(stripped):
            if not in_table:
                flush_para()
                in_table = True
            table_buf.append(line)
        else:
            if in_table:
                flush_table()
                in_table = False
            if stripped:
                para_buf.append(line)
            else:
                flush_para()   # blank line = paragraph boundary

    flush_para()
    flush_table()


def _sentence_split(text: str, max_chars: int) -> List[str]:
    """Split text at sentence ends (.!?) so no piece exceeds max_chars."""
    result: List[str] = []
    buf = ""
    for sent in re.split(r'(?<=[.!?])\s+', text):
        candidate = (buf + " " + sent).strip() if buf else sent
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                result.append(buf)
            if len(sent) > max_chars:
                # single sentence over limit — split at word boundaries
                words = sent.split()
                buf = ""
                for w in words:
                    cand = (buf + " " + w).strip() if buf else w
                    if len(cand) <= max_chars:
                        buf = cand
                    else:
                        if buf:
                            result.append(buf)
                        buf = w
            else:
                buf = sent
    if buf:
        result.append(buf)
    return result if result else [text]


def _merge_pieces(pieces: List[str], target: int) -> List[str]:
    """Greedily merge small pieces up to target chars; oversized pieces pass through."""
    chunks: List[str] = []
    buf = ""
    for p in pieces:
        p = p.strip()
        if not p:
            continue
        if _ATOMIC_PLACEHOLDER_RE.match(p):
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(p)
            continue
        if buf and len(buf) + len(p) + 2 > target:
            chunks.append(buf)
            buf = p
        else:
            buf = (buf + "\n\n" + p) if buf else p
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


_ATOMIC_PLACEHOLDER_RE = re.compile(r'^@@AINXT_ATOMIC_BLOCK_(\d+)@@$')
_FENCED_BLOCK_RE = re.compile(
    r'(?ms)^([ \t]*)(`{3,}|~{3,})([^\n`]*)\n(.*?)\n\1\2[ \t]*$'
)
_XML_BLOCK_RE = re.compile(
    r'(?ms)<(?P<tag>[A-Za-z_][\w:.-]*)(?:\s[^>]*)?>.*?</(?P=tag)>'
)
_CODE_KEYWORDS_RE = re.compile(
    r'\b(?:public|private|protected|class|interface|enum|function|def|return|import|SELECT|WHERE|INSERT|UPDATE|DELETE|CREATE|BEGIN|END|throw|throws)\b',
    re.IGNORECASE,
)


def _page_range_for_span(text: str, start: int, end: int) -> tuple[Optional[int], Optional[int]]:
    markers = [(m.start(), int(m.group(1))) for m in _PAGE_MARKER_RE.finditer(text)]
    if not markers:
        return None, None
    page_start = None
    page_end = None
    for pos, page in markers:
        if pos <= start:
            page_start = page
        if pos <= end:
            page_end = page
        else:
            break
    return page_start, page_end or page_start


def _clean_atomic_text(block: str) -> str:
    lines: List[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if _PAGE_MARKER_RE.match(stripped) or _PAGE_NUM_RE.match(stripped) or _SEPARATOR_RE.match(stripped):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _json_span_at(text: str, start: int) -> Optional[int]:
    if start < 0 or start >= len(text):
        return None
    opening = text[start]
    if opening not in "{[":
        return None
    closing = '}' if opening == '{' else ']'
    stack: List[str] = []
    in_string = False
    escape = False

    for i in range(start, len(text)):
        c = text[i]
        if in_string:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            continue
        if c in '{[':
            stack.append(c)
            continue
        if c in '}]':
            if not stack:
                return None
            top = stack[-1]
            if (top == '{' and c != '}') or (top == '[' and c != ']'):
                return None
            stack.pop()
            if not stack:
                candidate = _clean_atomic_text(text[start:i + 1])
                if candidate.startswith(opening) and candidate.endswith(closing):
                    try:
                        parsed = json.loads(candidate)
                    except Exception:
                        return None
                    if len(candidate) < 40:
                        return None
                    if isinstance(parsed, dict) and parsed:
                        return i + 1
                    if isinstance(parsed, list) and parsed:
                        return i + 1
                    return None
    return None


def _infer_fence_language(info: str, body: str) -> str:
    tokens = (info or "").strip().split(None, 1)
    lang = tokens[0].lower() if tokens else ""
    if lang:
        return lang
    stripped = body.strip()
    if stripped.startswith(('{', '[')):
        try:
            json.loads(stripped)
            return "json"
        except Exception:
            pass
    upper = stripped.upper()
    if "SELECT " in upper and " FROM " in upper:
        return "sql"
    if re.search(r'\b(public|private|class|interface)\b', stripped):
        return "java"
    if re.search(r'\bdef\s+\w+\s*\(', stripped):
        return "python"
    if stripped.startswith('<'):
        return "xml"
    return "code"


def _code_line_score(line: str) -> int:
    s = line.strip()
    if not s:
        return 0
    score = 0
    if line.startswith(('    ', '\t')):
        score += 1
    if _CODE_KEYWORDS_RE.search(s):
        score += 2
    if any(tok in s for tok in ('{', '}', ';', '=>', '->', '::', '==', '!=', '<=', '>=')):
        score += 1
    if re.match(r'^["\']?[A-Za-z_][\w_.-]*["\']?\s*:', s):
        score += 1
    if re.match(r'^</?[A-Za-z_][\w:.-]*(?:\s|>|/>)', s):
        score += 2
    return score


def _infer_code_language(block: str) -> str:
    s = block.strip()
    upper = s.upper()
    if upper.count('SELECT ') or re.search(r'\b(CREATE|INSERT|UPDATE|DELETE)\b', upper):
        return "sql"
    if re.search(r'\b(public|private|protected|class|interface|enum)\b', s):
        return "java"
    if re.search(r'\bdef\s+\w+\s*\(', s):
        return "python"
    if re.search(r'\b(function|const|let|var)\b', s):
        return "javascript"
    if s.startswith('<'):
        return "xml"
    return "code"


def _extract_code_like_blocks(text: str) -> List[dict]:
    blocks: List[dict] = []
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)

    start_idx: Optional[int] = None
    buf_scores: List[int] = []
    blank_run = 0

    def flush(end_idx: int) -> None:
        nonlocal start_idx, buf_scores, blank_run
        if start_idx is None:
            return
        start = offsets[start_idx]
        end = offsets[end_idx] if end_idx < len(offsets) else len(text)
        raw = text[start:end]
        cleaned = _clean_atomic_text(raw)
        meaningful = [ln for ln in cleaned.splitlines() if ln.strip()]
        score_sum = sum(buf_scores)
        density = score_sum / max(len(meaningful), 1)
        if len(meaningful) >= 4 and density >= 1.25 and len(cleaned) >= 80:
            page_start, page_end = _page_range_for_span(text, start, end)
            blocks.append({
                "start": start,
                "end": end,
                "text": cleaned,
                "content_type": "code_like",
                "language": _infer_code_language(cleaned),
                "page_start": page_start,
                "page_end": page_end,
            })
        start_idx = None
        buf_scores = []
        blank_run = 0

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if _PAGE_MARKER_RE.match(stripped):
            if start_idx is not None:
                continue
            continue
        score = _code_line_score(line)
        if score:
            if start_idx is None:
                start_idx = idx
            buf_scores.append(score)
            blank_run = 0
        elif start_idx is not None and not stripped and blank_run < 1:
            buf_scores.append(0)
            blank_run += 1
        elif start_idx is not None:
            flush(idx)
    flush(len(lines))
    return blocks


def _extract_atomic_blocks(text: str) -> tuple[str, dict[str, dict]]:
    candidates: List[dict] = []

    for m in _FENCED_BLOCK_RE.finditer(text):
        block = _clean_atomic_text(m.group(0))
        if not block:
            continue
        page_start, page_end = _page_range_for_span(text, m.start(), m.end())
        candidates.append({
            "start": m.start(),
            "end": m.end(),
            "text": block,
            "content_type": "code",
            "language": _infer_fence_language(m.group(3), m.group(4)),
            "page_start": page_start,
            "page_end": page_end,
            "priority": 0,
        })

    i = 0
    while i < len(text):
        if text[i] in '{[':
            end = _json_span_at(text, i)
            if end:
                block = _clean_atomic_text(text[i:end])
                page_start, page_end = _page_range_for_span(text, i, end)
                candidates.append({
                    "start": i,
                    "end": end,
                    "text": block,
                    "content_type": "json",
                    "language": "json",
                    "page_start": page_start,
                    "page_end": page_end,
                    "priority": 1,
                })
                i = end
                continue
        i += 1

    for m in _XML_BLOCK_RE.finditer(text):
        block = _clean_atomic_text(m.group(0))
        if len(block) < 80 or block.count('<') < 3:
            continue
        page_start, page_end = _page_range_for_span(text, m.start(), m.end())
        candidates.append({
            "start": m.start(),
            "end": m.end(),
            "text": block,
            "content_type": "xml",
            "language": "xml",
            "page_start": page_start,
            "page_end": page_end,
            "priority": 2,
        })

    for block in _extract_code_like_blocks(text):
        block["priority"] = 3
        candidates.append(block)

    selected: List[dict] = []
    occupied: List[tuple[int, int]] = []
    for c in sorted(candidates, key=lambda b: (b["priority"], b["start"], -(b["end"] - b["start"]))):
        if any(not (c["end"] <= s or c["start"] >= e) for s, e in occupied):
            continue
        selected.append(c)
        occupied.append((c["start"], c["end"]))

    selected.sort(key=lambda b: b["start"])
    placeholders: dict[str, dict] = {}
    parts: List[str] = []
    cursor = 0
    for idx, block in enumerate(selected):
        placeholder = f"@@AINXT_ATOMIC_BLOCK_{idx}@@"
        parts.append(text[cursor:block["start"]])
        parts.append(f"\n\n{placeholder}\n\n")
        placeholders[placeholder] = block
        cursor = block["end"]
    parts.append(text[cursor:])
    return "".join(parts), placeholders


# ---------------------------------------------------------------------------
# Phase 2 — Structured chunker: emits parent (whole-section) + leaf rows
# with section_path breadcrumbs ("1. Intro > 1.2 Scope").
#
# Returns: list of dicts, each {text, section_path, is_parent, parent_idx}
#   - parent rows  : is_parent=True,  parent_idx=None
#   - leaf rows    : is_parent=False, parent_idx=<index of parent in same list>
# Caller writes parents first to capture their UUIDs, then writes leaves with
# parent_chunk_id pointing at the corresponding parent UUID.
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r'^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$', re.MULTILINE)


_PAGE_MARKER_RE = re.compile(r'<!--\s*page:(\d+)\s*-->')


def _chunk_document_structured(
        text: str,
        target: int = 800,
        overlap: int = 100,
        parent_max_chars: int = 6000,
) -> List[dict]:
    """
    Section-aware chunker for KB docs.

    For every Markdown ATX heading, emits:
      1) A PARENT row containing the entire section text (capped at parent_max_chars).
      2) One or more LEAF rows (the same fine-grained chunks the old chunker produced)
         each carrying parent_idx pointing back to its parent.

    Sections without a heading (text before the first heading, or docs with no
    headings at all) emit leaves with parent_idx=None and section_path="".

    Page number extraction:
      Scans the raw markdown for <!-- page:N --> markers emitted by
      _convert_per_page_smart() in core/docling_parser.py. Each chunk is
      assigned the page number of the most recent marker that appears before
      the chunk's position in the text. Chunks before the first marker (or
      documents with no markers, e.g. legacy docs / DOCX) get page_number=None,
      which is the same as the previous behaviour — fully backward compatible.
    """
    # ── Page marker index ─────────────────────────────────────────────────────
    # Build a sorted list of (char_offset, page_number) from <!-- page:N -->
    # markers in the RAW (pre-clean) text. We use the raw text here because
    # _clean_text() does not strip HTML comments, so offsets are stable.
    import bisect as _bisect

    _page_markers: List[tuple] = [
        (m.start(), int(m.group(1)))
        for m in _PAGE_MARKER_RE.finditer(text)
    ]
    _marker_offsets: List[int] = [pos for pos, _ in _page_markers]

    def _page_at(char_offset: int) -> Optional[int]:
        """
        Return the page number of the most recent <!-- page:N --> marker at or
        before char_offset. Returns None when no marker precedes the offset
        (pre-heading content, or documents with no page markers at all).
        Uses bisect for O(log n) lookup — important for large documents with
        thousands of chunks.
        """
        idx = _bisect.bisect_right(_marker_offsets, char_offset) - 1
        return _page_markers[idx][1] if idx >= 0 else None
    # ─────────────────────────────────────────────────────────────────────────

    cleaned = _clean_text(text)
    cleaned, atomic_blocks = _extract_atomic_blocks(cleaned)
    out: List[dict] = []

    # Split into (heading_line | None, body_text) segments. The existing regex
    # `(?m)(?=^#{1,6}\s)` from _chunk_document is reused — it cuts BEFORE each
    # heading so the heading line stays as the first line of the next segment.
    segments = re.split(r'(?m)(?=^#{1,6}\s)', cleaned)

    # Maintain a stack of (level, heading_text) for section_path breadcrumbs.
    heading_stack: List[tuple] = []

    # Cursor into `cleaned` used by the page-number lookup to avoid matching
    # the wrong occurrence when the same section body text appears twice.
    _seg_search_pos: int = 0

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue

        # Detect heading on the first line of the segment.
        first_line = seg.split('\n', 1)[0]
        m = _HEADING_RE.match(first_line)

        if m:
            level = len(m.group('hashes'))
            htext = m.group('text').strip()
            # Pop any deeper-or-equal levels off the stack, then push this one.
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, htext))
            section_path = " > ".join(h[1] for h in heading_stack)
            section_body = seg  # keep heading line in body so parent text is self-contained
        else:
            # Pre-heading or heading-less doc — no parent, just leaves.
            section_path = ""
            section_body = seg

        # Generate fine-grained leaves for this section using the existing
        # paragraph/table/sentence splitter — preserves table atomicity.
        pieces: List[str] = []
        _section_to_pieces(section_body, pieces, target)
        leaves = _merge_pieces(pieces, target)

        # ── CH2: Filter noise leaf chunks ─────────────────────────────────────
        # Drop leaves that are heading-only (e.g. "## Test Keys for Release 1.2"
        # with no body) or near-empty (< 50 chars).  These arise when a heading
        # piece cannot merge with the next piece because the combined size would
        # exceed `target`.  Storing them wastes a pgvector slot and returns a
        # useless 26-char context to the LLM.
        _heading_only_re = re.compile(r'^#{1,6}\s+.+$', re.DOTALL)
        leaves = [
            lf for lf in leaves
            if len(lf.strip()) >= 50
            or not _heading_only_re.match(lf.strip())
        ]

        # ── CH4: Skip overlap when previous leaf ends with a table row ────────
        # The standard overlap logic appends the last `overlap` chars of the
        # previous leaf to the next leaf.  When the previous leaf ends with a
        # table row (e.g. "| 1051 | Not Prohibited Merchant | UPI |"), the tail
        # is a partial row fragment that corrupts the next chunk's context.
        # Rule: if the last non-empty line of a leaf is a table row, do NOT
        # carry overlap into the following leaf.
        def _leaf_ends_with_table(leaf_text: str) -> bool:
            for ln in reversed(leaf_text.splitlines()):
                if ln.strip():
                    return _is_table_line(ln.strip())
            return False

        if overlap and len(leaves) > 1:
            stitched = [leaves[0]]
            for i in range(1, len(leaves)):
                prev = leaves[i - 1]
                curr = leaves[i]
                if (
                    _leaf_ends_with_table(prev)
                    or _ATOMIC_PLACEHOLDER_RE.match(prev.strip())
                    or _ATOMIC_PLACEHOLDER_RE.match(curr.strip())
                ):
                    # Previous leaf ends with a table row, or either side is an
                    # atomic placeholder — skip overlap to avoid corrupting the
                    # table row / placeholder token.
                    stitched.append(curr)
                else:
                    tail = prev[-overlap:]
                    space = tail.rfind(' ')
                    if space > 0:
                        tail = tail[space + 1:]
                    stitched.append(tail + " " + curr)
            leaves = stitched

        # ── Part U11 (docx §8) — leaf heading name for citation badges ──
        # section_name is the LAST segment of section_path so the UI badge
        # can show just "Mandate Retry" without parsing the full breadcrumb.
        # Empty when the chunk is pre-heading or the doc has no headings.
        section_name = heading_stack[-1][1] if heading_stack else ""

        # ── Page number — resolved from <!-- page:N --> markers ───────────────
        # _convert_per_page_smart() in core/docling_parser.py prepends a
        # <!-- page:N --> marker to each group's markdown output. We find the
        # position of section_body inside the cleaned text and look up the most
        # recent marker before that position.
        #
        # We search from `_seg_search_pos` (updated after each segment) so that
        # repeated identical section bodies don't match the wrong occurrence.
        # For documents with no markers (legacy docs, DOCX, HTML, PPTX) this
        # always returns None — identical to the previous hardcoded behaviour.
        _seg_offset = cleaned.find(section_body, _seg_search_pos)
        if _seg_offset == -1:
            _seg_offset = cleaned.find(section_body)  # safety fallback
        _seg_search_pos = _seg_offset + len(section_body) if _seg_offset >= 0 else _seg_search_pos

        # Map cleaned-text offset back to raw-text offset for marker lookup.
        # Because _clean_text() only strips/replaces lines (never inserts new
        # chars), the raw text is always >= cleaned text in length. We use the
        # section body text itself to find its position in the raw text so the
        # marker lookup is accurate even when _clean_text() changed some lines.
        _raw_offset = text.find(section_body, 0) if _seg_offset >= 0 else -1
        if _raw_offset < 0:
            _raw_offset = _seg_offset if _seg_offset >= 0 else 0
        page_number: Optional[int] = _page_at(_raw_offset)

        def _atomic_chunk_from_placeholder(placeholder: str) -> Optional[dict]:
            block = atomic_blocks.get(placeholder.strip())
            if not block:
                return None
            page_start = block.get("page_start")
            page_end = block.get("page_end")
            return {
                "text":         block.get("text", ""),
                "section_path": section_path,
                "section_name": section_name,
                "page_number":  page_start or page_number,
                "page_start":   page_start,
                "page_end":     page_end,
                "is_parent":    False,
                "parent_idx":   None,
                "atomic":       True,
                "content_type": block.get("content_type"),
                "language":     block.get("language"),
            }

        # ── CH3: Emit PARENT only when it adds value ───────────────────────────
        # Original rule: emit parent when heading exists AND len(leaves) > 1.
        # Problem: a section with one large table produces len(leaves) == 1 after
        # CH2 filtering, but the overlap logic above may have created a duplicate
        # leaf from the same table content.  More importantly, when the single
        # leaf IS the complete table, a parent would duplicate it verbatim.
        # New rule: emit parent only when heading exists AND len(leaves) > 1 AND
        # the single leaf (if only one) is NOT table-dominant — table sections
        # are self-contained and need no parent wrapper.
        _single_table_section = (
            len(leaves) == 1
            and leaves
            and sum(1 for ln in leaves[0].splitlines() if '|' in ln.strip())
               / max(len([ln for ln in leaves[0].splitlines() if ln.strip()]), 1)
               >= 0.40
        )
        parent_idx: Optional[int] = None
        non_atomic_leaves = [lf for lf in leaves if not _ATOMIC_PLACEHOLDER_RE.match(lf.strip())]
        if m and len(non_atomic_leaves) > 1 and not _single_table_section:
            parent_text = "\n\n".join(non_atomic_leaves)[:parent_max_chars]
            out.append({
                "text":         parent_text,
                "section_path": section_path,
                "section_name": section_name,
                "page_number":  page_number,
                "is_parent":    True,
                "parent_idx":   None,
            })
            parent_idx = len(out) - 1

        # Emit LEAVES — each pointing back to the parent (if any).
        for leaf in leaves:
            leaf = leaf.strip()
            if not leaf:
                continue
            atomic_chunk = _atomic_chunk_from_placeholder(leaf)
            if atomic_chunk:
                out.append(atomic_chunk)
                continue
            out.append({
                "text":         leaf,
                "section_path": section_path,
                "section_name": section_name,
                "page_number":  page_number,
                "is_parent":    False,
                "parent_idx":   parent_idx,
            })

    return out


def _notify_approvers_kb(doc_id: str, namespace: str, filename: str, display_name: str, uploader: str,
                          uploaded_at: str = None, visibility: str = "PUBLIC") -> None:
    """Fire-and-forget: push a single kb_approval inbox item to each recipient
    who should approve this upload.

    Routing (mirrors budget-approval routing — auth.rbac.resolve_request_approvers):
      - the uploader's own HOD (users.hod_email), plus any delegatees that HOD
        has nominated (department_hod_mapping.delegated_to).
      - falls back to every active admin/ad_level<=3 user when the uploader
        has no resolvable HOD (e.g. no AD mapping) — a submission must never
        go unrouted.
    Exactly one inbox row is written per recipient (no duplicate entries for
    the same doc/recipient pair).
    """
    try:
        from store.inbox_store import publish_inbox_item
        from db.database import SessionLocal
        from db.models import User
        from sqlalchemy import or_, func
        from auth.rbac import resolve_request_approvers

        approvers = resolve_request_approvers(uploader or "")
        hod_email = approvers.get("hod_email")
        delegatee_emails = approvers.get("delegatee_emails") or []
        recipient_emails = ([hod_email] if hod_email else []) + delegatee_emails

        from core.config import HOD_APPROVAL_ENABLED as _HOD_APPROVAL_ENABLED

        db = SessionLocal()
        try:
            if recipient_emails:
                recipients = db.query(User).filter(
                    func.lower(User.email).in_([e.lower() for e in recipient_emails]),
                    User.is_active == True,
                ).all()
            elif _HOD_APPROVAL_ENABLED:
                # Fallback (HOD mode only): no resolvable HOD — notify
                # configurable approval level
                _approval_level = int(os.getenv("APPROVAL_AD_LEVEL", "3"))
                recipients = db.query(User).filter(
                    or_(User.ad_level <= _approval_level, User.role == "admin"),
                    User.is_active == True,
                ).all()
            else:
                # Flat mode fallback: resolve_request_approvers() already
                # returns the sole active admin. An empty recipient_emails
                # here means there is no active admin to route to, or the
                # uploader IS that admin — in the latter case the doc already
                # auto-approved via the single-admin bootstrap in
                # docs_router.py's upload_doc(), so there is nothing left to
                # notify.
                recipients = []
            for u in recipients:
                publish_inbox_item(
                    user_id  = str(u.id),
                    type     = "kb_approval",
                    title    = f"[KB] New doc pending: {display_name}",
                    body     = "",
                    source_id= doc_id,
                    metadata = {"entity_type": "kb_doc", "entity_id": doc_id,
                                "entity_name": filename, "display_name": display_name,
                                "namespace": namespace,
                                "status": "PENDING_APPROVAL", "action": "submit",
                                "uploaded_by": uploader,
                                "uploaded_at": uploaded_at,
                                "visibility": (visibility or "PUBLIC").upper(),
                                "hod_email": hod_email,
                                "delegatee_emails": delegatee_emails},
                )
        finally:
            db.close()
    except Exception as _e:
        logger.warning(f"_notify_approvers_kb failed: {_e}")


def upload_doc(
        file_bytes: bytes,
        filename: str,
        namespace: str,
        original_filename: str = "",        # user's original filename — used for display name only
        uploaded_by: Optional[str] = None,
        classification: str = "INTERNAL",
        owner_team: Optional[str] = None,
        org_id: Optional[str] = None,
        visibility: str = "PUBLIC",
        department_ids: Optional[List[str]] = None,
        department: Optional[str] = None,   # uploader's dept — used for RAG ACL on document_embeddings
        auto_approve: bool = False,          # True for admins / approvers — skips approval queue
        pre_parsed_text: Optional[str] = None,  # pre-parsed + compliance-redacted text from router; skips re-parse
        # Phase 1 — spec scope metadata
        product_id: Optional[str] = None,   # UUID string — FK to products table
        domain: Optional[str] = None,       # e.g. "Tech", "HR", "Finance"
        spec_version: Optional[str] = None, # e.g. "v3", "2025.1"
        version_date: Optional[str] = None, # ISO date string e.g. "2025-01-15"
        deprecate_prior: bool = False,      # True = deprecate prior versions of same product+domain on activation
        parent_doc_id: Optional[str] = None,  # prior version doc_id (lineage pointer)
        # ── Part U13 (2026-06-08) — docx §8 hierarchy + §2 retain originals ──
        # source_type: BRD / FSD / TPMC_DECISION / RBI_CIRCULAR / ARCHITECTURE /
        # SPEC / OTHER. None = legacy/unknown (citation footer drops the typed
        # badge). Captured at upload via the new dropdown in KnowledgeBase.jsx.
        source_type: Optional[str] = None,
        # True when the router detected a scanned (image-only) PDF with no
        # embedded text. Bypasses the empty-text rejection gate here so the doc
        # can reach the approval queue. PaddleOCR + compliance run in activate_doc().
        is_scanned_pdf: bool = False,
        # True when the router detected a mixed PDF: some pages have selectable
        # text (born-digital) and other pages are image-only (scanned). The upload
        # succeeds with partial text. At activation, PaddleOCR runs on the scanned
        # pages and results are merged with the digital pages' text. Deferred
        # compliance also runs on the merged output.
        has_mixed_scanned_pages: bool = False,
) -> dict:
    """
    Parse, chunk, and stage a document for approval.
    Vectors are NOT written to pgvector here — embedding happens only on approval
    via activate_doc(), ensuring unapproved content is never RAG-searchable.
    Docling/PaddleOCR parsing is also deferred to activate_doc() — only the
    lightweight legacy parser runs here for compliance redaction and chunking.
    """
    # SEC-F-MISC-004: an unvalidated classification could store documents with
    # arbitrary sensitivity labels, breaking downstream access control decisions.
    # Warn-and-default (not hard-fail): no current caller lets an end user pick
    # an arbitrary classification string — it only reaches an unexpected value
    # via a future/API-direct caller — so an upload should not be blocked
    # outright. Instead, fall back to the safe default (INTERNAL) and surface
    # a warning both in the logs and in the response, so the caller/UI can
    # flag it without losing the document.
    _ALLOWED_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"}
    _classification_warning = None
    if classification.upper() not in _ALLOWED_CLASSIFICATIONS:
        _classification_warning = (
            f"Invalid classification {classification!r} — must be one of "
            f"{sorted(_ALLOWED_CLASSIFICATIONS)}. Defaulting to INTERNAL."
        )
        logger.warning(
            f"[UPLOAD_DOC][step=classification][WARN] doc='{original_filename or filename}' "
            f"{_classification_warning}"
        )
        classification = "INTERNAL"
    else:
        classification = classification.upper()

    # SEC-F-MISC-007: an unvalidated namespace could contain path traversal
    # characters or shell metacharacters, enabling injection attacks.
    if not re.match(r'^[a-zA-Z0-9_-]{1,64}$', namespace):
        raise ValueError(f"Invalid namespace {namespace!r} — must match ^[a-zA-Z0-9_-]{{1,64}}$")

    import time as _t
    _upload_start = _t.perf_counter()
    _display = original_filename or filename
    try:

        # ── 1. Parse file ──────────────────────────────────────────────────
        # If the router already parsed + compliance-redacted the text (skip_docling=True
        # path), reuse it directly — avoids double-parsing and ensures PII-redacted
        # content is what gets chunked and stored (not the raw unredacted bytes).
        if pre_parsed_text is not None:
            text = pre_parsed_text
            logger.info(
                f"[UPLOAD_DOC][step=parse] doc='{_display}' "
                f"source=pre_parsed chars={len(text):,}"
            )
        else:
            ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "txt"
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            try:
                from core.document_parser import parse_file_structured
                parsed = parse_file_structured(tmp_path, ext, filename, skip_docling=True)
                text = parsed.get("content", "")
                logger.info(
                    f"[UPLOAD_DOC][step=parse] doc='{_display}' "
                    f"source=legacy chars={len(text):,}"
                )
            finally:
                os.unlink(tmp_path)

        if not text or not text.strip():
            if not is_scanned_pdf:
                logger.error(
                    f"[UPLOAD_DOC][step=parse][ERROR] doc='{_display}' "
                    f"error='No text could be extracted from file'"
                )
                return {"success": False, "error": "No text could be extracted from file"}
            # Scanned (image-only) PDF: no embedded text is expected at upload time.
            # PaddleOCR will run during activate_doc() post-approval via _try_docling().
            # Compliance is also deferred to that step once OCR text is available.
            logger.info(
                f"[UPLOAD_DOC][step=parse] doc='{_display}' "
                f"scanned_pdf=true chars=0 — proceeding without text (OCR+compliance deferred)"
            )
            text = ""

        # ── 0. Deduplication check (after parse — hash on text content, not raw bytes) ──
        # For scanned PDFs text is "" at upload time, so hash the raw file bytes
        # instead to avoid all scanned PDFs colliding on the same empty-string hash.
        if is_scanned_pdf and not text.strip():
            content_hash = hashlib.sha256(file_bytes).hexdigest()
        else:
            content_hash = hashlib.sha256(_clean_text(text).encode()).hexdigest()
        logger.info(
            f"[UPLOAD_DOC][step=dedup] doc='{_display}' "
            f"hash={content_hash[:12]}..."
        )
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        dedup_db = SessionLocal()
        try:
            # Check against all non-terminal statuses — i.e., any doc that is
            # alive in the system (waiting for approval, being indexed, fully
            # active, or deprecated but still present).
            # Previously this checked ["APPROVED", "AUTO_APPROVED"] which are
            # legacy statuses that no longer exist in the current lifecycle
            # (PENDING_APPROVAL → INDEXING → ACTIVE). That meant the dedup
            # check always returned None and every duplicate slipped through.
            existing_doc = (
                dedup_db.query(KnowledgeDocument)
                .filter(
                    KnowledgeDocument.content_hash == content_hash,
                    KnowledgeDocument.status.in_([
                        "PENDING_APPROVAL",  # uploaded, awaiting approver action
                        "INDEXING",          # approved, kb_worker parsing/embedding
                        "ACTIVE",            # fully indexed and RAG-searchable
                        "DEPRECATED",        # superseded but still in DB
                    ]),
                )
                .first()
            )
            if existing_doc:
                logger.warning(
                    f"[UPLOAD_DOC][step=dedup][ERROR] doc='{_display}' "
                    f"duplicate=true existing_id={existing_doc.id} "
                    f"existing_status={existing_doc.status}"
                )
                return {"success": False, "error": "A document with identical content already exists in this knowledge base."}
        finally:
            dedup_db.close()
        logger.info(f"[UPLOAD_DOC][step=dedup] doc='{_display}' duplicate=false")

        # ── 2. Chunk ───────────────────────────────────────────────────────
        # Phase 2: structured chunking emits parent (whole-section) + leaf rows
        # with section_path breadcrumbs. Stored as JSONB list of dicts so
        # activate_doc() can write parents-first then leaves with proper linkage.
        #
        # Pre-chunking pass: SECTION PROMOTER
        # Documents authored in Word using plain bold (instead of "Heading 1/2"
        # styles) arrive here with NO markdown headings, which forces the
        # structured chunker to emit empty section_path on every leaf and
        # produces the cross-section blending seen in retrieval evaluation.
        # When the upload declares a source_type whose vocabulary is registered
        # (e.g. AiNxt release notes), we rewrite vocabulary-matching standalone
        # lines to "## <Section>" so the structured chunker can anchor on them.
        # Unknown source_types pass through untouched — this is a strict
        # opt-in by doc_kind, so legacy uploads keep their existing behavior.
        _promo_stats: dict = {}
        try:
            from core.section_promoter import promote_sections as _promote
            text, _promo_stats = _promote(text, doc_kind=source_type)
            if _promo_stats.get("promoted", 0) > 0:
                logger.info(
                    f"[UPLOAD_DOC][step=section_promote] doc='{_display}' "
                    f"promoted={_promo_stats['promoted']} "
                    f"sections={sorted(set(_promo_stats.get('matched_sections', [])))} "
                    f"source_type={source_type}"
                )
            else:
                logger.info(
                    f"[UPLOAD_DOC][step=section_promote] doc='{_display}' "
                    f"promoted=0 source_type={source_type}"
                )
        except Exception as _spe:
            logger.warning(
                f"[UPLOAD_DOC][step=section_promote][WARN] doc='{_display}' "
                f"error='{_spe}' — continuing with original text"
            )

        structured_chunks = _chunk_document_structured(text)
        if not structured_chunks:
            if not is_scanned_pdf:
                logger.error(
                    f"[UPLOAD_DOC][step=chunk][ERROR] doc='{_display}' "
                    f"error='No text chunks produced'"
                )
                return {"success": False, "error": "No text chunks produced"}
            # Scanned PDF: no chunks yet — that's expected. Chunks will be produced
            # after PaddleOCR runs in activate_doc() and re-chunks the OCR'd text.
            logger.info(
                f"[UPLOAD_DOC][step=chunk] doc='{_display}' "
                f"scanned_pdf=true chunks=0 — chunks deferred to activate_doc()"
            )
            structured_chunks = []

        chunks = [c["text"] for c in structured_chunks if not c.get("is_parent")]
        _parents = len([c for c in structured_chunks if c.get("is_parent")])
        logger.info(
            f"[UPLOAD_DOC][step=chunk] doc='{_display}' "
            f"total={len(structured_chunks)} parents={_parents} leaves={len(chunks)}"
        )

        _structure_score = None
        try:
            from core.structure_scorer import score_chunk_set, log_score
            _structure_score = score_chunk_set(
                text=text,
                structured_chunks=structured_chunks,
                promoter_stats=_promo_stats,
            )
            log_score(_structure_score, filename=(original_filename or filename))
            _verdict = _structure_score.verdict if hasattr(_structure_score, "verdict") else (
                _structure_score.get("verdict") if isinstance(_structure_score, dict) else "unknown"
            )
            logger.info(
                f"[UPLOAD_DOC][step=structure_score] doc='{_display}' "
                f"verdict={_verdict}"
            )
        except Exception as _sse:
            logger.warning(
                f"[UPLOAD_DOC][step=structure_score][WARN] doc='{_display}' "
                f"error='{_sse}'"
            )

        # UUID4 is used (not the filename) for collision-safety, path-traversal
        # safety and rename stability. See docs/KB_MD_STORAGE.md for the full
        # rationale and the debug recipe to reverse-map UUID ↔ filename.
        doc_id = str(uuid.uuid4())

        # ── 3. Persist KnowledgeDocument record (PGS01 main DB) ───────────
        # Chunks stored as JSON — embedding happens in activate_doc() on approval.
        # Nothing is written to pgvector (document_embeddings) until approved.
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        db2 = SessionLocal()
        try:
            display_name = original_filename or filename
            _status = "AUTO_APPROVED" if auto_approve else "PENDING_APPROVAL"
            # Parse version_date string → datetime if provided
            _version_date = None
            if version_date:
                try:
                    _version_date = datetime.fromisoformat(version_date)
                except Exception:
                    logger.warning(f"docs_store: invalid version_date '{version_date}' — ignored")

            # Phase 3: store FULL parsed text in `content` so coverage tier + shared doc
            # cache + map-reduce have the verbatim source available without re-parsing.
            # The object store still receives the same bytes on activation as the SoR copy.
            # Part U13 — pull extension from the upload's original_filename
            # (preferred) or the staging filename. Stored on the doc row so the
            # citation footer can build the "Open original" URL deterministically.
            _src_name = (original_filename or filename) or ""
            _original_ext = (_src_name.rsplit(".", 1)[-1].lower() if "." in _src_name else "") or None
            # Validate source_type against the CHECK enum so a typo in a caller
            # surfaces here rather than as a constraint violation on commit.
            _allowed_source_types = {"BRD", "FSD", "TPMC_DECISION", "RBI_CIRCULAR",
                                     "ARCHITECTURE", "SPEC", "OTHER"}
            _norm_source_type = (source_type or "").strip().upper() or None
            if _norm_source_type and _norm_source_type not in _allowed_source_types:
                logger.warning(
                    f"docs_store: invalid source_type '{source_type}' — coerced to None "
                    f"(allowed: {sorted(_allowed_source_types)})"
                )
                _norm_source_type = None

            doc = KnowledgeDocument(
                id=doc_id,
                name=display_name,
                filename=filename,
                namespace=namespace,
                content=text,
                content_hash=content_hash,  # SHA-256 of raw file bytes — used for dedup
                chunks=structured_chunks,  # Phase 2: list of {text, section_path, is_parent, parent_idx}
                chunk_count=len(chunks),    # leaf-only count for UI display
                file_size=len(file_bytes),
                uploaded_by=uploaded_by,
                uploaded_by_dept=department or None,
                visibility=visibility.upper(),
                department_ids=department_ids or [],
                status=_status,
                approved_by=uploaded_by if auto_approve else None,
                approved_at=datetime.now(timezone.utc) if auto_approve else None,
                created_at=datetime.now(timezone.utc),
                # Phase 1 — spec scope metadata
                product_id=product_id or None,
                domain=domain or None,
                spec_version=spec_version or None,
                version_date=_version_date,
                deprecate_prior=deprecate_prior,
                parent_doc_id=parent_doc_id or None,
                # Part U13 — docx §8 hierarchy + §2 retain originals
                source_type=_norm_source_type,
                original_ext=_original_ext,
                # Scanned PDF flag — True means OCR+compliance are deferred to activate_doc()
                is_scanned_pdf=is_scanned_pdf,
                # Mixed PDF flag — True means some pages are scanned; PaddleOCR runs at activation
                has_mixed_scanned_pages=has_mixed_scanned_pages,
            )
            db2.add(doc)
            db2.commit()
            logger.info(
                f"[UPLOAD_DOC][step=db_save] doc='{_display}' "
                f"doc_id={doc_id} status={_status} namespace={namespace}"
            )

            # ── Part U13 — save original binary for Docling parsing ──────────
            # Written here at upload time so activate_doc() (via kb_worker)
            # can pass the file path to Docling post-approval without needing
            # the raw bytes again. The binary is deleted by kb_worker after
            # successful activation — only the .md file is retained for RAG.
            # For rejected/pending docs the binary is cleaned up by delete_doc().
            if _original_ext and file_bytes:
                _original_saved = False
                try:
                    from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
                    os.makedirs(_KB_FS_ROOT, mode=0o755, exist_ok=True)
                    _orig_abs = os.path.join(_KB_FS_ROOT, f"{doc_id}.{_original_ext}")
                    _orig_tmp = _orig_abs + ".tmp"
                    with open(_orig_tmp, "wb") as _fh:
                        _fh.write(file_bytes)
                    os.replace(_orig_tmp, _orig_abs)
                    _original_saved = True
                    logger.info(
                        f"[UPLOAD_DOC][step=original_save] doc='{_display}' "
                        f"doc_id={doc_id} path={_orig_abs} bytes={len(file_bytes):,}"
                    )
                except Exception as _oe:
                    try:
                        db2.delete(doc)
                        db2.commit()
                    except Exception as _cleanup_exc:
                        logger.warning(
                            f"[UPLOAD_DOC][step=original_save][WARN] doc='{_display}' "
                            f"doc_id={doc_id} cleanup_failed='{_cleanup_exc}'"
                        )
                    logger.error(
                        f"[UPLOAD_DOC][step=original_save][ERROR] doc='{_display}' "
                        f"doc_id={doc_id} error='{_oe}'"
                    )
                    return {"success": False, "error": f"Original file save failed: {_oe}"}
                if _original_saved:
                    try:
                        from store.kb_replication import replicate_file as _replicate_file
                        _replicate_file(doc_id, _original_ext, file_bytes, kind="original")
                    except Exception as _repl_exc:
                        try:
                            if os.path.exists(_orig_abs):
                                os.remove(_orig_abs)
                            db2.delete(doc)
                            db2.commit()
                        except Exception as _cleanup_exc:
                            logger.warning(
                                f"[UPLOAD_DOC][step=original_replicate][WARN] doc='{_display}' "
                                f"doc_id={doc_id} cleanup_failed='{_cleanup_exc}'"
                            )
                        logger.error(
                            f"[UPLOAD_DOC][step=original_replicate][ERROR] doc='{_display}' "
                            f"doc_id={doc_id} error='{_repl_exc}'"
                        )
                        return {"success": False, "error": f"Original file replication failed: {_repl_exc}"}

            if _status == "PENDING_APPROVAL":
                # Notify approvers — doc is staged, not yet searchable
                _notify_approvers_kb(
                    doc_id=str(doc_id), namespace=namespace,
                    filename=filename, display_name=display_name, uploader=uploaded_by,
                    uploaded_at=(doc.created_at.isoformat() + ("+00:00" if doc.created_at.tzinfo is None else "")) if doc.created_at else None,
                    visibility=visibility,
                )
                logger.info(
                    f"[UPLOAD_DOC][step=notify] doc='{_display}' "
                    f"doc_id={doc_id} approvers_notified=true"
                )
            elif _status == "AUTO_APPROVED":
                # Admin/approver uploaded — embed immediately, no review needed
                db2.close()
                db2 = None
                _embed_result = activate_doc(
                    doc_id=doc_id,
                    approved_by=uploaded_by or "auto",
                    classification=classification,
                    owner_team=owner_team,
                    org_id=org_id,
                    department_ids=department_ids,
                )
                if not _embed_result.get("success"):
                    logger.warning(f"docs_store: auto-approve embed failed: {_embed_result}")
                    return {"success": False, "error": _embed_result.get("error") or "Embedding failed"}
        finally:
            if db2:
                db2.close()

        # ── 4. Register namespace in the KV cache ──────────────────────────
        try:
            _docs_kv().sadd("docs:namespaces", namespace)
        except Exception:
            pass

        _elapsed_ms = (_t.perf_counter() - _upload_start) * 1000
        logger.info(
            f"[UPLOAD_DOC][step=complete] doc='{_display}' "
            f"doc_id={doc_id} chunks={len(chunks)} status={_status} "
            f"elapsed={_elapsed_ms:.0f}ms"
        )
        _resp = {
            "success":     True,
            "doc_id":      doc_id,
            "chunk_count": len(chunks),
            "namespace":   namespace,
            "filename":    filename,
            "status":      _status,
        }
        if _classification_warning:
            _resp["warning"] = _classification_warning
        if _structure_score is not None:
            try:
                _resp["structure"] = _structure_score.to_dict()
            except Exception:
                pass
        return _resp

    except Exception as e:
        _elapsed_ms = (_t.perf_counter() - _upload_start) * 1000
        logger.error(
            f"[UPLOAD_DOC][step=complete][ERROR] doc='{_display}' "
            f"elapsed={_elapsed_ms:.0f}ms error='{e}'"
        )
        return {"success": False, "error": str(e)}


def activate_doc(
        doc_id: str,
        approved_by: str,
        classification: str = "INTERNAL",
        owner_team: Optional[str] = None,
        org_id: Optional[str] = None,
        department_ids: Optional[List[str]] = None,
        repo: Optional[str] = None,
) -> dict:
    """
    Called on approval: optionally re-parse with Docling, embed chunks,
    and write vectors into pgvector (document_embeddings).
    This is the ONLY path that makes a document RAG-searchable.

    Docling/PaddleOCR parse runs HERE (post-approval) — not at upload time.
    This ensures wasted parse calls never happen for docs deleted before approval.
    The original binary file (saved at upload time) is read from disk and sent
    to the parse service. If Docling succeeds, content + chunks are upgraded to
    Docling quality before embedding. If Docling fails, the legacy-parsed content
    from upload time is used — approval is never blocked.

    repo: explicit pgvector repo key (e.g. 'agent_kb:my-agent').
          When omitted the default 'docs_kb:{namespace}' is used.
    """
    import time as _t
    _activate_start = _t.perf_counter()

    try:
        from db.database import SessionLocal, VectorSessionLocal
        from db.models import KnowledgeDocument, DocumentEmbedding
        from sqlalchemy import text as _sql_text

        def _cleanup_activation_outputs(stage: str) -> None:
            try:
                _vdb_del = VectorSessionLocal()
                try:
                    _vdb_del.execute(
                        _sql_text("DELETE FROM document_embeddings WHERE metadata->>'doc_id' = :doc_id"),
                        {"doc_id": doc_id},
                    )
                    _vdb_del.commit()
                finally:
                    _vdb_del.close()
            except Exception as _vec_del_exc:
                logger.warning(
                    f"[ACTIVATE][step={stage}][CLEANUP_WARN] doc_id={doc_id} "
                    f"pgvector cleanup failed: {_vec_del_exc}"
                )
            try:
                from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
                import os as _os_cancel
                _md_path = _os_cancel.path.join(_KB_FS_ROOT, f"{doc_id}.md")
                if _os_cancel.path.isfile(_md_path):
                    _os_cancel.unlink(_md_path)
                    logger.info(
                        f"[ACTIVATE][step={stage}][CLEANUP] doc_id={doc_id} "
                        f"removed markdown '{_md_path}'"
                    )
            except Exception as _md_del_exc:
                logger.warning(
                    f"[ACTIVATE][step={stage}][CLEANUP_WARN] doc_id={doc_id} "
                    f"markdown cleanup failed: {_md_del_exc}"
                )
            try:
                from store.kb_replication import delete_file as _delete_replica_file
                _delete_replica_file(doc_id, "md", kind="markdown")
            except Exception as _rep_md_del_exc:
                logger.warning(
                    f"[ACTIVATE][step={stage}][CLEANUP_WARN] doc_id={doc_id} "
                    f"markdown replica cleanup failed: {_rep_md_del_exc}"
                )

        def _cancel_if_deleted(stage: str, cleanup_outputs: bool = False) -> Optional[dict]:
            _db_chk = SessionLocal()
            try:
                _doc_chk = _db_chk.get(KnowledgeDocument, doc_id)
                if _doc_chk and _doc_chk.status not in ("DELETING", "DELETED"):
                    return None
            finally:
                _db_chk.close()
            logger.warning(
                f"[ACTIVATE][step={stage}][CANCELLED] doc_id={doc_id} "
                f"document was deleted while activation was running"
            )
            if cleanup_outputs:
                _cleanup_activation_outputs(stage)
            return {"success": False, "cancelled": True, "error": "Document deleted during activation"}

        # ── [ACTIVATE][step=load_chunks] ───────────────────────────────────
        db = SessionLocal()
        try:
            doc = db.get(KnowledgeDocument, doc_id)
            if not doc:
                logger.error(
                    f"[ACTIVATE][step=load_chunks][ERROR] doc_id={doc_id} "
                    f"error='Document not found'"
                )
                return {"success": False, "error": "Document not found"}

            # Clear any error from a previous failed activation attempt so the
            # UI never shows a stale error message after a successful re-approval.
            if getattr(doc, "parse_error", None):
                doc.parse_error = None
                db.commit()

            raw_chunks       = list(doc.chunks) if doc.chunks else []
            filename         = doc.filename
            namespace        = doc.namespace
            visibility       = (doc.visibility or "PUBLIC").upper()
            dept_ids         = department_ids or doc.department_ids or []
            content_hash     = doc.content_hash
            doc_domain       = doc.domain
            doc_spec_version = doc.spec_version
            doc_product_id   = doc.product_id
            doc_deprecate    = doc.deprecate_prior
            doc_name         = doc.name
            doc_source_type  = doc.source_type
            doc_original_ext = doc.original_ext   # used to locate original binary for Docling
            doc_content      = doc.content or ""  # legacy-parsed text (fallback)
            doc_is_scanned        = bool(getattr(doc, "is_scanned_pdf", False))       # True = OCR+compliance deferred
            doc_has_mixed_scanned = bool(getattr(doc, "has_mixed_scanned_pages", False))  # True = mixed PDF, PaddleOCR on scanned pages
            logger.info(
                f"[ACTIVATE][step=load_chunks] doc_id={doc_id} "
                f"filename='{filename}' chunks={len(raw_chunks)} "
                f"original_ext={doc_original_ext or 'none'}"
                + (" is_scanned_pdf=true" if doc_is_scanned else "")
                + (" has_mixed_scanned_pages=true" if doc_has_mixed_scanned else "")
            )
        finally:
            db.close()

        # ── [ACTIVATE][step=docling_parse] ─────────────────────────────────
        # Run Docling NOW (post-approval) on the original binary file saved at
        # upload time. For Docling-supported formats (pdf/docx/html/htm/pptx),
        # Docling MUST succeed — no fallback to legacy parser.
        # Reason: legacy-parsed embeddings for these formats produce incorrect
        # chunking quality and mislead RAG retrieval. If Docling fails, the
        # entire activation fails so the approver can retry after fixing the
        # issue (e.g. increase PARSE_SVC_TIMEOUT, fix the file, restart embed svc).
        _docling_formats = {"pdf", "docx", "html", "htm", "pptx"}
        _use_docling_content = False
        _docling_text = ""

        if doc_original_ext and doc_original_ext.lower() in _docling_formats:
            from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
            _orig_path = os.path.join(_KB_FS_ROOT, f"{doc_id}.{doc_original_ext}")

            if not os.path.isfile(_orig_path):
                # Original file missing from disk — hard failure, cannot parse
                logger.error(
                    f"[ACTIVATE][step=docling_parse][ERROR] doc_id={doc_id} "
                    f"original file not found at '{_orig_path}' — cannot activate"
                )
                # User-facing: no absolute filesystem path. The logger.error()
                # above records the full path for ops.
                return {
                    "success": False,
                    "error":   (
                        "The uploaded file is no longer available on the server. "
                        "It may have been removed after upload. "
                        "Please re-upload and re-approve the document."
                    ),
                }

            logger.info(
                f"[ACTIVATE][step=docling_parse] doc_id={doc_id} "
                f"file='{_orig_path}' ext={doc_original_ext} — calling Docling parse"
            )
            _dp_start = _t.perf_counter()
            try:
                from core.document_parser import _try_docling
                _docling_text = _try_docling(_orig_path, doc_original_ext)
                _dp_ms = (_t.perf_counter() - _dp_start) * 1000

                if _docling_text and _docling_text.strip():
                    _use_docling_content = True
                    logger.info(
                        f"[ACTIVATE][step=docling_parse] doc_id={doc_id} "
                        f"source=docling chars={len(_docling_text):,} "
                        f"latency={_dp_ms:.0f}ms"
                    )
                    # ── Residual conversion-error guard ───────────────────
                    # _convert_per_page_smart() raises PageConversionError when
                    # any batch fails after retry, which propagates through
                    # _try_docling() as a RuntimeError and is caught below.
                    # This secondary scan is a safety net for any code path
                    # that bypasses the primary raise (e.g. the legacy fallback
                    # or the remote parse service returning placeholders in its
                    # content string).  If placeholders are found here, treat
                    # it as a hard failure — roll back to PENDING_APPROVAL with
                    # the exact failed page ranges stored in parse_error so the
                    # user can see them in the request/status tab.
                    _conv_err_matches = re.findall(
                        r'<!--\s*conversion-error:\s*pages\s+(\d+)-(\d+)[^>]*-->',
                        _docling_text, re.IGNORECASE
                    )
                    if _conv_err_matches:
                        # Exact page count across all failed batches
                        _failed_page_count = sum(
                            int(e) - int(s) + 1 for s, e in _conv_err_matches
                        )
                        # Show 3-4 sample ranges in the user-facing message
                        _sample_ranges = [
                            f"pages {s}-{e}" for s, e in _conv_err_matches[:4]
                        ]
                        _sample_str = ", ".join(_sample_ranges)
                        if len(_conv_err_matches) > 4:
                            _sample_str += f" (and {len(_conv_err_matches) - 4} more batch(es))"
                        _err_msg = (
                            f"PDF conversion failed: {_failed_page_count} page(s) across "
                            f"{len(_conv_err_matches)} batch(es) could not be extracted "
                            f"even after retry — {_sample_str}. "
                            f"Total failed pages: {_failed_page_count}. "
                            f"Please re-upload the document or contact support if the issue persists."
                        )
                        logger.error(
                            f"[ACTIVATE][step=docling_parse][PARTIAL_FAIL] doc_id={doc_id} "
                            f"failed_pages={_failed_page_count} batches={len(_conv_err_matches)} "
                            f"sample={_sample_str} — rolling back to PENDING_APPROVAL"
                        )
                        return {"success": False, "error": _err_msg}
                else:
                    # _try_docling returned None — format not supported by Docling
                    # (e.g. USE_DOCLING_PARSER=0). Use legacy content from upload.
                    _dp_ms = (_t.perf_counter() - _dp_start) * 1000
                    logger.info(
                        f"[ACTIVATE][step=docling_parse] doc_id={doc_id} "
                        f"source=legacy_content latency={_dp_ms:.0f}ms "
                        f"reason='Docling not active for ext={doc_original_ext}'"
                    )

            except RuntimeError as _dpe:
                # Docling raised a hard error (timeout, empty content, service down).
                # Do NOT fall back — return failure so kb_worker rolls back to
                # PENDING_APPROVAL and the approver can retry.
                _dp_ms = (_t.perf_counter() - _dp_start) * 1000
                logger.error(
                    f"[ACTIVATE][step=docling_parse][ERROR] doc_id={doc_id} "
                    f"elapsed={_dp_ms:.0f}ms error='{_dpe}'"
                )
                return {
                    "success": False,
                    "error":   str(_dpe),
                }

        else:
            # Format not in Docling's supported list (e.g. .txt, .csv, .xlsx).
            # Use legacy content from upload time — this is correct behaviour,
            # not a fallback, because Docling never handles these formats.
            _ext_label = doc_original_ext or "none"
            logger.info(
                f"[ACTIVATE][step=docling_parse] doc_id={doc_id} "
                f"source=legacy_content "
                f"reason='ext={_ext_label} not in docling formats — using upload-time content'"
            )

        _cancelled = _cancel_if_deleted("after_docling_parse")
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=deferred_compliance] ──────────────────────────────
        # Gated by COMPLIANCE_SCAN_KB_UPLOAD env flag (default OFF).
        # For scanned/mixed PDFs: compliance was deferred from upload time
        # (no OCR text existed yet). Runs here after Docling/PaddleOCR.
        # When OFF: raw OCR text is stored and indexed as-is.
        # When ON: PII/PCI scan + redaction runs; blocking types reject the doc.
        from core.config import COMPLIANCE_SCAN_KB_UPLOAD as _KB_COMPLIANCE_ON_ACT
        if doc_is_scanned or doc_has_mixed_scanned:
            _ocr_text_for_compliance = _docling_text if _use_docling_content else ""
            if not _ocr_text_for_compliance.strip():
                logger.warning(
                    f"[ACTIVATE][step=ocr_check] doc_id={doc_id} "
                    f"{'scanned_pdf' if doc_is_scanned else 'mixed_scanned'}=true ocr_chars=0 — "
                    f"OCR extracted no text (document will have empty content)"
                )
            elif _KB_COMPLIANCE_ON_ACT:
                _dcomp_start = _t.perf_counter()
                try:
                    from agents.compliance_engine import compliance_engine, BLOCKING_TYPES
                    _dcomp_check    = compliance_engine.validate_input(_ocr_text_for_compliance)
                    _dcomp_findings = _dcomp_check.get("findings", [])
                    _dcomp_ms       = (_t.perf_counter() - _dcomp_start) * 1000
                    if _dcomp_check.get("blocked", False):
                        _dcomp_reasons = sorted(set(
                            f["type"] for f in _dcomp_findings
                            if f.get("type") in BLOCKING_TYPES
                        ))
                        logger.warning(
                            f"[ACTIVATE][step=deferred_compliance][BLOCKED] doc_id={doc_id} "
                            f"reasons={_dcomp_reasons} latency={_dcomp_ms:.0f}ms"
                        )
                        _db_dcomp = SessionLocal()
                        try:
                            _doc_dcomp = _db_dcomp.get(KnowledgeDocument, doc_id)
                            if _doc_dcomp:
                                _doc_dcomp.status           = "REJECTED"
                                _doc_dcomp.rejection_reason = (
                                    f"Compliance block after OCR: "
                                    f"{', '.join(_dcomp_reasons) or 'PCI/PII data'}"
                                )
                                _doc_dcomp.compliance_pass  = False
                                _db_dcomp.commit()
                        finally:
                            _db_dcomp.close()
                        return {
                            "success":            False,
                            "error":              (
                                f"Document blocked by compliance after OCR: "
                                f"{', '.join(_dcomp_reasons) or 'PCI/PII data'}"
                            ),
                            "compliance_reasons": _dcomp_reasons,
                        }
                    _dcomp_redacted = _dcomp_check.get("redacted_text") or _ocr_text_for_compliance
                    if _dcomp_check.get("was_redacted"):
                        logger.info(
                            f"[ACTIVATE][step=deferred_compliance] doc_id={doc_id} "
                            f"redacted=true types={_dcomp_check.get('redacted_types', [])} "
                            f"latency={_dcomp_ms:.0f}ms"
                        )
                        _docling_text = _dcomp_redacted
                    else:
                        logger.info(
                            f"[ACTIVATE][step=deferred_compliance] doc_id={doc_id} "
                            f"redacted=false passed=true latency={_dcomp_ms:.0f}ms"
                        )
                    _db_dcomp2 = SessionLocal()
                    try:
                        _doc_dcomp2 = _db_dcomp2.get(KnowledgeDocument, doc_id)
                        if _doc_dcomp2:
                            _doc_dcomp2.compliance_pass = True
                            _db_dcomp2.commit()
                    finally:
                        _db_dcomp2.close()
                except Exception as _dce:
                    logger.warning(
                        f"[ACTIVATE][step=deferred_compliance][WARN] doc_id={doc_id} "
                        f"error='{_dce}' — proceeding without compliance check"
                    )
            else:
                logger.info(
                    f"[ACTIVATE][step=deferred_compliance] doc_id={doc_id} "
                    f"skipped — COMPLIANCE_SCAN_KB_UPLOAD=false"
                )

        # ── [ACTIVATE][step=rechunk] ───────────────────────────────────────
        # If Docling produced better markdown, re-run section_promoter +
        # structured chunker on it. Otherwise use the chunks from upload time.
        if _use_docling_content:
            _rechunk_start = _t.perf_counter()
            try:
                _rechunk_text = _docling_text
                # Re-run section promoter on Docling output
                try:
                    from core.section_promoter import promote_sections as _promote
                    _rechunk_text, _promo = _promote(_rechunk_text, doc_kind=doc_source_type)
                    if _promo.get("promoted", 0) > 0:
                        logger.info(
                            f"[ACTIVATE][step=rechunk] doc_id={doc_id} "
                            f"section_promoter promoted={_promo['promoted']}"
                        )
                except Exception as _spe:
                    logger.warning(
                        f"[ACTIVATE][step=rechunk][WARN] doc_id={doc_id} "
                        f"section_promoter error='{_spe}' — continuing"
                    )

                _new_structured = _chunk_document_structured(_rechunk_text)
                if _new_structured:
                    raw_chunks = _new_structured
                    # Update doc.content in DB with Docling text for .md write
                    _db_upd = SessionLocal()
                    try:
                        _doc_upd = _db_upd.get(KnowledgeDocument, doc_id)
                        if _doc_upd:
                            _doc_upd.content = _rechunk_text
                            _db_upd.commit()
                    finally:
                        _db_upd.close()
                    doc_content = _rechunk_text
                    _rechunk_ms = (_t.perf_counter() - _rechunk_start) * 1000
                    _new_leaves = len([c for c in _new_structured if not c.get("is_parent")])
                    logger.info(
                        f"[ACTIVATE][step=rechunk] doc_id={doc_id} "
                        f"chunks={len(_new_structured)} leaves={_new_leaves} "
                        f"source=docling latency={_rechunk_ms:.0f}ms"
                    )
                else:
                    # Docling produced text but chunker returned 0 chunks — hard failure.
                    # Do NOT fall back to legacy chunks — they were built from
                    # lower-quality upload-time content and would produce wrong embeddings.
                    _rechunk_ms = (_t.perf_counter() - _rechunk_start) * 1000
                    logger.error(
                        f"[ACTIVATE][step=rechunk][ERROR] doc_id={doc_id} "
                        f"Docling rechunk produced 0 chunks latency={_rechunk_ms:.0f}ms"
                    )
                    # User-facing: no engine name, no internal doc_id.
                    return {
                        "success": False,
                        "error":   (
                            "The document was read successfully but no searchable "
                            "content could be produced from it. The file may be "
                            "empty or contain only images without readable text. "
                            "Please check the file and re-approve."
                        ),
                    }
            except Exception as _rce:
                # Unexpected error during rechunking — hard failure, no fallback.
                _rechunk_ms = (_t.perf_counter() - _rechunk_start) * 1000
                logger.error(
                    f"[ACTIVATE][step=rechunk][ERROR] doc_id={doc_id} "
                    f"elapsed={_rechunk_ms:.0f}ms error='{_rce}'"
                )
                # User-facing: the raw exception text ({_rce}) is logged above
                # but never surfaced — it can contain module paths and internals.
                return {
                    "success": False,
                    "error":   (
                        "The document could not be prepared for search. "
                        "Please re-approve to retry, or contact support if the "
                        "problem continues."
                    ),
                }

        # Normalize chunk shape (structured dicts or legacy flat strings)
        structured: List[dict] = []
        for c in raw_chunks:
            if isinstance(c, dict) and "text" in c:
                _sp = c.get("section_path", "") or ""
                _sn = c.get("section_name")
                if not _sn and _sp:
                    _sn = _sp.rsplit(" > ", 1)[-1].strip() or None
                structured.append({
                    "text":         c.get("text", ""),
                    "section_path": _sp,
                    "section_name": _sn or None,
                    "page_number":  c.get("page_number"),
                    "page_start":   c.get("page_start"),
                    "page_end":     c.get("page_end"),
                    "is_parent":    bool(c.get("is_parent", False)),
                    "parent_idx":   c.get("parent_idx"),
                    "atomic":       bool(c.get("atomic", False)),
                    "content_type": c.get("content_type"),
                    "language":     c.get("language"),
                })
            elif isinstance(c, str):
                structured.append({
                    "text":         c,
                    "section_path": "",
                    "section_name": None,
                    "page_number":  None,
                    "page_start":   None,
                    "page_end":     None,
                    "is_parent":    False,
                    "parent_idx":   None,
                    "atomic":       False,
                    "content_type": None,
                    "language":     None,
                })
        chunks = [c["text"] for c in structured]

        # ── If doc was already activated (chunks cleared), migrate existing
        # vectors to the new repo rather than re-embedding from scratch.
        if not chunks:
            if repo:
                vdb_m = VectorSessionLocal()
                try:
                    result = vdb_m.execute(
                        _sql_text(
                            "UPDATE document_embeddings SET repo = :new_repo "
                            "WHERE metadata->>'doc_id' = :doc_id AND repo != :new_repo"
                        ),
                        {"new_repo": repo, "doc_id": doc_id},
                    )
                    migrated = result.rowcount
                    vdb_m.commit()
                    if migrated:
                        logger.info(
                            f"activate_doc: migrated {migrated} existing vectors → "
                            f"repo={repo!r} for doc {doc_id}"
                        )
                        return {"success": True, "chunk_count": migrated, "migrated": True}
                except Exception as _me:
                    vdb_m.rollback()
                    logger.warning(f"activate_doc: vector migration failed for {doc_id}: {_me}")
                finally:
                    vdb_m.close()
            return {"success": False, "error": "No staged chunks — document may have already been activated"}

        _cancelled = _cancel_if_deleted("before_embed")
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=embed_start] ──────────────────────────────────────
        import httpx as _httpx
        _EMBED_BATCH = 64
        _total_batches = (len(chunks) + _EMBED_BATCH - 1) // _EMBED_BATCH
        logger.info(
            f"[ACTIVATE][step=embed_start] doc_id={doc_id} "
            f"filename='{filename}' chunks={len(chunks)} "
            f"batches={_total_batches} embed_url={_EMBED_SVC_URL}"
        )
        embeddings: List = []
        _embed_start = _t.perf_counter()
        try:
            for _batch_start in range(0, len(chunks), _EMBED_BATCH):
                _batch       = chunks[_batch_start: _batch_start + _EMBED_BATCH]
                _batch_num   = (_batch_start // _EMBED_BATCH) + 1
                _cancelled = _cancel_if_deleted(f"embed_batch_{_batch_num}")
                if _cancelled:
                    return _cancelled
                _b_start     = _t.perf_counter()
                resp = _httpx.post(
                    f"{_EMBED_SVC_URL}/embed",
                    json={"texts": _batch, "provider": "ollama"},
                    timeout=120.0,
                )
                resp.raise_for_status()
                _resp_json = resp.json()
                _batch_embeddings = _resp_json.get("embeddings")
                if not _batch_embeddings:
                    raise ValueError(f"Embed service returned unexpected response (missing 'embeddings' key): {_resp_json}")
                embeddings.extend(_batch_embeddings)
                _b_ms = (_t.perf_counter() - _b_start) * 1000
                logger.info(
                    f"[ACTIVATE][step=embed_batch] doc_id={doc_id} "
                    f"batch={_batch_num}/{_total_batches} texts={len(_batch)} "
                    f"latency={_b_ms:.0f}ms"
                )
        except Exception as e:
            _embed_ms = (_t.perf_counter() - _embed_start) * 1000
            logger.error(
                f"[ACTIVATE][step=embed_start][ERROR] doc_id={doc_id} "
                f"filename='{filename}' elapsed={_embed_ms:.0f}ms error='{e}'"
            )
            # User-facing message. The raw exception, service URL and the
            # WinError/VPN/firewall remediation steps are ops-only detail and
            # are preserved in the logger.error() call above — they must not be
            # rendered in the Request Status tab.
            return {
                "success": False,
                "error":   (
                    "The document could not be indexed for search because the "
                    "indexing service was unavailable. Please re-approve to "
                    "retry once the service is healthy."
                ),
            }

        _embed_ms = (_t.perf_counter() - _embed_start) * 1000
        logger.info(
            f"[ACTIVATE][step=embed_complete] doc_id={doc_id} "
            f"total_chunks={len(embeddings)} latency={_embed_ms:.0f}ms"
        )

        _cancelled = _cancel_if_deleted("after_embed", cleanup_outputs=True)
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=zero_vector_check] ────────────────────────────────
        # The OllamaEmbedder inside the embed service never raises on individual
        # text failures — it silently returns a zero vector [0.0]*N instead.
        # A zero vector has undefined cosine similarity with any real query vector
        # so those chunks are permanently invisible to RAG search even though the
        # document appears ACTIVE in the UI.
        # Guard: count zero vectors BEFORE writing anything to pgvector.
        # If more than 10% of chunks are zero → treat as a hard failure so the
        # document never becomes ACTIVE with silently broken embeddings.
        _zero_count = sum(
            1 for _e in embeddings
            if _e and max(abs(_v) for _v in _e) < 1e-9
        )
        if _zero_count > 0:
            _zero_pct = (_zero_count / len(embeddings)) * 100
            logger.warning(
                f"[ACTIVATE][step=zero_vector_check] doc_id={doc_id} "
                f"zero_vectors={_zero_count}/{len(embeddings)} ({_zero_pct:.1f}%)"
            )
            if _zero_count / len(embeddings) > 0.10:
                # Keep the counts (actionable, shows scale) but drop the model
                # name. Full detail is in the logger.warning() above.
                return {
                    "success": False,
                    "error": (
                        f"Only part of this document could be indexed for search "
                        f"— {_zero_count} of {len(embeddings)} sections "
                        f"({_zero_pct:.0f}%) failed. The indexing service may be "
                        f"overloaded. Please re-approve to retry."
                    ),
                }

        _cancelled = _cancel_if_deleted("before_pgvector_write", cleanup_outputs=True)
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=pgvector_write] ───────────────────────────────────
        repo_key = repo if repo else f"docs_kb:{namespace.lower()}"
        clean_dept_ids = [str(d).strip() for d in (dept_ids or []) if str(d).strip()]
        if visibility == "PRIVATE" and len(clean_dept_ids) == 1:
            _embed_dept = clean_dept_ids[0]
        elif visibility == "PRIVATE" and len(clean_dept_ids) > 1:
            _embed_dept = "__multi_dept__"
        else:
            _embed_dept = None

        if len(embeddings) != len(chunks):
            logger.error(
                f"[ACTIVATE][step=pgvector_write][ERROR] doc_id={doc_id} "
                f"mismatch: expected {len(chunks)} embeddings got {len(embeddings)} — aborting"
            )
            # Internal consistency failure — the expected/actual counts are
            # meaningless to a user and are already logged above.
            return {
                "success": False,
                "error":   (
                    "The document could not be indexed for search because part "
                    "of it was processed incompletely. Please re-approve to retry."
                ),
            }

        # Phase 2: pre-allocate a UUID for every chunk so leaves can reference
        # their parent's UUID at insert time (single-pass commit).
        chunk_ids = [str(uuid.uuid4()) for _ in structured]

        vdb = VectorSessionLocal()
        try:
            # Remove any stale vectors for this doc across ALL repos (idempotent re-approve).
            # Deleting by doc_id handles cases where a previous activation used the wrong repo.
            vdb.execute(
                _sql_text("DELETE FROM document_embeddings WHERE metadata->>'doc_id' = :doc_id"),
                {"doc_id": doc_id},
            )
            for idx, (sc, emb) in enumerate(zip(structured, embeddings)):
                chunk_text   = sc["text"]
                # Dedup is enforced at upload time on the full-document hash; chunk-level
                # hashing here would cause cross-document collisions on shared boilerplate.
                # Resolve parent UUID — leaves point at their parent's pre-allocated id.
                _pid_idx = sc.get("parent_idx")
                parent_chunk_id = chunk_ids[_pid_idx] if _pid_idx is not None and 0 <= _pid_idx < len(chunk_ids) else None
                vdb.add(DocumentEmbedding(
                    id=chunk_ids[idx],
                    repo=repo_key,
                    file_path=filename,
                    chunk_index=idx,
                    content=chunk_text,
                    embedding=emb,
                    metadata_={
                        "doc_id": doc_id,
                        "namespace": namespace,
                        "visibility": visibility,
                        "department_ids": clean_dept_ids,
                        "atomic": bool(sc.get("atomic", False)),
                        "content_type": sc.get("content_type"),
                        "language": sc.get("language"),
                        "page_start": sc.get("page_start"),
                        "page_end": sc.get("page_end"),
                    },
                    content_hash=content_hash,
                    classification=classification,
                    owner_team=owner_team,
                    org_id=org_id,
                    uploaded_by=approved_by,
                    department=_embed_dept,
                    allowed_roles=[],
                    allowed_users=[],
                    # Phase 1 — stamp scope keys so hard filter works at query time
                    product_id=doc_product_id,
                    domain=doc_domain,
                    spec_version=doc_spec_version,
                    # Phase 2 — section-aware chunking + parent linkage
                    parent_chunk_id=parent_chunk_id,
                    section_path=sc.get("section_path") or None,
                    is_section_parent=sc.get("is_parent", False),
                    # ── Part U11 (docx §8) — hierarchy metadata on the chunk row ──
                    # Denormalised so citation rendering + source_type filtering
                    # avoid a cross-DB join. doc_name + source_type are identical
                    # for every chunk of this doc (one row in PGS01.knowledge_docs).
                    # page_number is per-chunk (NULL when the parser didn't
                    # surface page info, e.g. for Markdown/Word/code uploads).
                    # section_name = leaf heading from section_path (e.g.
                    # "Mandate Retry") for compact UI badges.
                    doc_name=doc_name,
                    source_type=doc_source_type,
                    section_name=sc.get("section_name") or None,
                    page_number=sc.get("page_number"),
                    # Phase 1 closure — chunk-level active-version filter.
                    # Stamped 'ACTIVE' here; the deprecate_prior branch below
                    # flips prior versions' rows to 'DEPRECATED' atomically with
                    # the knowledge_docs.status flip.
                    status="ACTIVE",
                    created_at=datetime.now(timezone.utc),
                ))
            vdb.commit()
            _pgv_ms = (_t.perf_counter() - _embed_start) * 1000
            logger.info(
                f"[ACTIVATE][step=pgvector_write] doc_id={doc_id} "
                f"rows={len(structured)} repo={repo_key} latency={_pgv_ms:.0f}ms"
            )
        except Exception as e:
            vdb.rollback()
            logger.error(
                f"[ACTIVATE][step=pgvector_write][ERROR] doc_id={doc_id} "
                f"error='{e}'"
            )
            # Raw DB/driver exception text ({e}) can contain table names, SQL
            # fragments and connection strings — logged above, never surfaced.
            return {
                "success": False,
                "error":   (
                    "The document could not be saved to the search index. "
                    "Please re-approve to retry, or contact support if the "
                    "problem continues."
                ),
            }
        finally:
            vdb.close()

        _cancelled = _cancel_if_deleted("after_pgvector_write", cleanup_outputs=True)
        if _cancelled:
            return _cancelled

        # ── Clear staged chunks from knowledge_docs (free space) ──────────
        try:
            db2 = SessionLocal()
            try:
                doc2 = db2.get(KnowledgeDocument, doc_id)
                if doc2:
                    doc2.chunks = None   # chunks consumed — no longer needed
                    db2.commit()
            finally:
                db2.close()
        except Exception as _ce:
            logger.warning(
                f"[ACTIVATE][step=pgvector_write][WARN] doc_id={doc_id} "
                f"chunk-clear failed (non-fatal — vectors already stored): {_ce}"
            )

        _cancelled = _cancel_if_deleted("before_md_write", cleanup_outputs=True)
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=md_write] ──────────────────────────────────────
        # Write full markdown to filesystem — single SoR for Coverage tier.
        # Uses Docling text if available (upgraded at rechunk step), otherwise
        # the legacy-parsed content from upload time.
        #
        # Must complete BEFORE status is flipped to ACTIVE so that if a user
        # searches immediately after seeing ACTIVE in the UI, kb_doc_cache.warm()
        # finds the .md file on disk and caches the full Docling-processed content
        # in Redis. Without this ordering, warm() falls back to the truncated
        # DB preview and that stale payload is cached for 24 h.
        # Hard timeout of 30 s — if the write stalls (e.g. NFS hang) we log a
        # warning and proceed; the document will still be RAG-searchable via
        # pgvector and Coverage tier will fall back to doc.content from the DB.
        try:
            from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
            import os as _os_kb
            import concurrent.futures as _cf
            _os_kb.makedirs(_KB_FS_ROOT, mode=0o755, exist_ok=True)
            _abs_path = _os_kb.path.join(_KB_FS_ROOT, f"{doc_id}.md")
            _db_content = SessionLocal()
            try:
                _doc_content = _db_content.get(KnowledgeDocument, doc_id)
                _content_text = (_doc_content.content or "") if _doc_content else ""
            finally:
                _db_content.close()
            _md_bytes = _content_text.encode("utf-8")
            _md_source = 'docling' if _use_docling_content else 'legacy'

            logger.info(
                f"[ACTIVATE][step=md_write_start] doc_id={doc_id} "
                f"path={_abs_path} bytes={len(_md_bytes):,} source={_md_source}"
            )

            def _do_md_write(_abs: str, _data: bytes) -> None:
                """Atomic tmp-write + rename — safe for concurrent cache readers."""
                _tmp = _abs + ".tmp"
                with open(_tmp, "wb") as _fh:
                    _fh.write(_data)
                _os_kb.replace(_tmp, _abs)

            _md_write_start = _t.perf_counter()
            with _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="md_write") as _ex:
                _fut = _ex.submit(_do_md_write, _abs_path, _md_bytes)
                try:
                    _fut.result(timeout=30)
                    _md_ms = (_t.perf_counter() - _md_write_start) * 1000
                    logger.info(
                        f"[ACTIVATE][step=md_write_done] doc_id={doc_id} "
                        f"path={_abs_path} bytes={len(_md_bytes):,} "
                        f"source={_md_source} latency={_md_ms:.0f}ms"
                    )
                    try:
                        from store.kb_replication import replicate_file as _replicate_file
                        _replicate_file(doc_id, "md", _md_bytes, kind="markdown")
                    except Exception as _md_repl_exc:
                        logger.warning(
                            f"[ACTIVATE][step=md_replicate][WARN] doc_id={doc_id} "
                            f"error='{_md_repl_exc}' — local .md is written; continuing"
                        )
                except _cf.TimeoutError:
                    _md_ms = (_t.perf_counter() - _md_write_start) * 1000
                    logger.warning(
                        f"[ACTIVATE][step=md_write_timeout] doc_id={doc_id} "
                        f"path={_abs_path} elapsed={_md_ms:.0f}ms — "
                        f"write stalled after 30s, skipping (non-fatal, "
                        f"document is active in pgvector)"
                    )
        except Exception as _se:
            logger.warning(
                f"[ACTIVATE][step=md_write][WARN] doc_id={doc_id} "
                f"error='{_se}' — .md file not written (non-fatal)"
            )

        _cancelled = _cancel_if_deleted("before_set_active", cleanup_outputs=True)
        if _cancelled:
            return _cancelled

        # ── [ACTIVATE][step=set_status_active] ────────────────────────────
        # Flip the document to ACTIVE only after the MD file has been written
        # to disk. This guarantees that when the UI shows ACTIVE and the user
        # immediately searches, kb_doc_cache.warm() will find the full
        # Docling-processed .md file and cache it correctly in Redis.
        # Flipping before the MD write caused warm() to fall back to the
        # truncated DB preview and cache that stale payload for 24 h.
        try:
            _db_active = SessionLocal()
            try:
                _doc_active = _db_active.get(KnowledgeDocument, doc_id)
                if _doc_active:
                    _doc_active.status = "ACTIVE"
                    _db_active.commit()
                    logger.info(
                        f"[ACTIVATE][step=set_status_active] doc_id={doc_id} "
                        f"status=ACTIVE — document is now RAG-searchable"
                    )
            finally:
                _db_active.close()
        except Exception as _sa_exc:
            logger.error(
                f"[ACTIVATE][step=set_status_active][ERROR] doc_id={doc_id} "
                f"error='{_sa_exc}' — status not yet flipped to ACTIVE"
            )

        # ── [ACTIVATE][step=invalidate_new_doc_cache] ─────────────────────
        # Invalidate the cache entry for the newly activated doc immediately
        # after status=ACTIVE so the next warm() call reads the .md file that
        # was just written above, not any previously cached stale payload.
        try:
            from store import kb_doc_cache as _kbc_new
            _kbc_new.invalidate(doc_id)
        except Exception:
            pass

        # ── [ACTIVATE][step=valid_from] ────────────────────────────────────
        try:
            _db_vf = SessionLocal()
            try:
                _doc_vf = _db_vf.get(KnowledgeDocument, doc_id)
                if _doc_vf and _doc_vf.valid_from is None:
                    _doc_vf.valid_from = _doc_vf.version_date or datetime.now(timezone.utc)
                    _db_vf.commit()
                    logger.info(
                        f"[ACTIVATE][step=valid_from] doc_id={doc_id} "
                        f"valid_from={_doc_vf.valid_from.isoformat()}"
                    )
            finally:
                _db_vf.close()
        except Exception as _vfe:
            logger.warning(
                f"[ACTIVATE][step=valid_from][WARN] doc_id={doc_id} "
                f"error='{_vfe}' (non-fatal)"
            )

        # ── Phase 1+4: Deprecate prior versions of same product + domain ──────
        if doc_deprecate and doc_product_id:
            try:
                _db_dep = SessionLocal()
                try:
                    # Fetch the (id, spec_version) pairs of the prior versions BEFORE
                    # updating their status so we can invalidate the shared doc cache
                    # for each one individually.
                    _prior = (
                        _db_dep.query(KnowledgeDocument.id, KnowledgeDocument.spec_version)
                        .filter(
                            KnowledgeDocument.product_id == doc_product_id,
                            KnowledgeDocument.domain == doc_domain,
                            KnowledgeDocument.id != doc_id,
                            KnowledgeDocument.status == "APPROVED",
                        ).all()
                    )
                    # Phase 4: close validity window on the deprecated versions so
                    # the as-of cascade can route old dates back to them.
                    _now_ts = datetime.now(timezone.utc)
                    _db_dep.query(KnowledgeDocument).filter(
                        KnowledgeDocument.product_id == doc_product_id,
                        KnowledgeDocument.domain == doc_domain,
                        KnowledgeDocument.id != doc_id,
                        KnowledgeDocument.status == "APPROVED",
                    ).update(
                        {"status": "DEPRECATED", "valid_to": _now_ts},
                        synchronize_session=False,
                    )
                    _db_dep.commit()
                    logger.info(
                        f"activate_doc: deprecated {len(_prior)} prior version(s) for "
                        f"product_id={doc_product_id} domain={doc_domain}"
                    )
                finally:
                    _db_dep.close()
                # Phase 1 closure — flip every chunk row for the deprecated
                # docs on PGS02 to status='DEPRECATED' so the hybrid_search
                # `AND status='ACTIVE'` predicate excludes them instantly.
                # Paired with the cache invalidation below so DB + cache stay
                # consistent: the moment a new version activates, no further
                # retrieval can return chunks from the prior version.
                if _prior:
                    try:
                        _vdb_dep = VectorSessionLocal()
                        try:
                            _prior_ids = [str(_pid) for _pid, _ in _prior]
                            # metadata->>'doc_id' is stamped at insert (line 676)
                            _vdb_dep.execute(
                                _sql_text(
                                    "UPDATE document_embeddings "
                                    "SET status = 'DEPRECATED' "
                                    "WHERE metadata->>'doc_id' = ANY(:doc_ids)"
                                ),
                                {"doc_ids": _prior_ids},
                            )
                            _vdb_dep.commit()
                            logger.info(
                                f"activate_doc: flipped chunk rows to DEPRECATED for "
                                f"{len(_prior_ids)} prior doc(s) on PGS02"
                            )
                        finally:
                            _vdb_dep.close()
                    except Exception as _cde:
                        logger.warning(
                            f"activate_doc: chunk-level deprecate flip failed "
                            f"(non-fatal — knowledge_docs.status is canonical): {_cde}"
                        )
                # Phase 3f: drop shared doc cache entries for every deprecated
                # version so the next query for this product reloads fresh content.
                try:
                    from store import kb_doc_cache as _kbc
                    for _pid, _pver in _prior:
                        _kbc.invalidate(str(_pid))
                        if _pver:
                            _kbc.invalidate_product_version(str(doc_product_id), _pver)
                except Exception as _ie:
                    logger.warning(f"activate_doc: kb_doc_cache invalidate failed (non-fatal): {_ie}")
            except Exception as _de:
                logger.warning(f"activate_doc: deprecate_prior failed (non-fatal): {_de}")

        # ── Phase 5: enqueue async entity extraction (in-house model only) ────
        # Spec-scoped only — code repos and platform KB docs don't need a graph.
        if doc_product_id:
            try:
                from workers.kb_entity_worker import enqueue as _kb_enq
                _kb_enq(doc_id)
                logger.info(
                    f"[ACTIVATE][step=entity_extract] doc_id={doc_id} "
                    f"enqueued=true product_id={doc_product_id}"
                )
            except Exception as _ee:
                logger.warning(
                    f"[ACTIVATE][step=entity_extract][WARN] doc_id={doc_id} "
                    f"error='{_ee}' (non-fatal)"
                )

        _total_ms = (_t.perf_counter() - _activate_start) * 1000
        logger.info(
            f"[ACTIVATE][step=complete] doc_id={doc_id} "
            f"filename='{filename}' chunks={len(chunks)} "
            f"parse_source={'docling' if _use_docling_content else 'legacy'} "
            f"elapsed={_total_ms:.0f}ms"
        )
        return {"success": True, "chunk_count": len(chunks)}

    except Exception as e:
        _total_ms = (_t.perf_counter() - _activate_start) * 1000
        logger.error(
            f"[ACTIVATE][step=complete][ERROR] doc_id={doc_id} "
            f"elapsed={_total_ms:.0f}ms error='{e}'"
        )
        # Catch-all for any unhandled exception. str(e) here is arbitrary
        # third-party text that may name internal models/services, so scrub it
        # before returning. Sanitization preserves actionable content such as
        # the failed page ranges carried by PageConversionError. The raw text is
        # retained in the logger.error() above.
        try:
            from core.user_error_messages import sanitize_user_error
            _user_err = sanitize_user_error(e)
        except Exception:
            _user_err = (
                "Document processing failed. Please re-approve to retry, or "
                "contact support if the issue persists."
            )
        return {"success": False, "error": _user_err}


def list_docs(
    namespace:    Optional[str] = None,
    status:       Optional[str] = None,
    product_id:   Optional[str] = None,
    domain:       Optional[str] = None,
    spec_version: Optional[str] = None,
) -> List[dict]:
    """List uploaded knowledge documents, optionally filtered by namespace,
    status, and spec scope (product_id / domain / spec_version)."""
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        from sqlalchemy import select

        db = SessionLocal()
        try:
            q = select(KnowledgeDocument).order_by(KnowledgeDocument.created_at.desc())
            if namespace:
                q = q.where(KnowledgeDocument.namespace == namespace)
            if status:
                q = q.where(KnowledgeDocument.status == status)
            if product_id:
                q = q.where(KnowledgeDocument.product_id == product_id)
            if domain:
                q = q.where(KnowledgeDocument.domain == domain)
            if spec_version:
                q = q.where(KnowledgeDocument.spec_version == spec_version)
            rows = db.execute(q).scalars().all()
            return [
                {
                    "id":             r.id,
                    "name":           r.name,
                    "filename":       r.filename,
                    "namespace":      r.namespace,
                    "chunk_count":    r.chunk_count,
                    "file_size":      r.file_size,
                    "uploaded_by":      r.uploaded_by,
                    "uploaded_by_dept": r.uploaded_by_dept or "",
                    "visibility":       r.visibility or "PUBLIC",
                    "department_ids":   r.department_ids or [],
                    "status":           r.status or "PENDING_APPROVAL",
                    "approved_by":      r.approved_by,
                    "rejection_reason": r.rejection_reason or "",
                    "parse_error":      getattr(r, "parse_error", None) or None,
                    "created_at":       r.created_at.isoformat() if r.created_at else None,
                    # Phase 1 — spec scope metadata
                    "product_id":       str(r.product_id) if r.product_id else "",
                    "domain":           r.domain or "",
                    "spec_version":     r.spec_version or "",
                    "version_date":     r.version_date.isoformat() if r.version_date else None,
                    # Full doc body lives at KB_DOC_STORAGE_PATH/<id>.md on
                    # the local filesystem. No storage-URI column — path is
                    # implicit from the row id.
                    "parent_doc_id":    str(r.parent_doc_id) if r.parent_doc_id else "",
                    # Part U13 — docx §8 hierarchy + §2 retain originals
                    "source_type":      r.source_type or "",
                    "original_ext":     r.original_ext or "",
                }
                for r in rows
            ]
        finally:
            db.close()

    except Exception as e:
        logger.error(f"docs_store.list_docs failed: {e}")
        return []


def list_deletion_history() -> List[dict]:
    """Return every row from knowledge_doc_deletions, newest first.

    No filtering by namespace/status here — this table only ever holds
    ACTIVE-at-deletion-time snapshots (see delete_doc()). ACL filtering by
    caller role/department happens at the router layer
    (GET /kb/deleted-history in routers/docs_router.py), since it needs
    current_user context this store function doesn't have.
    """
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocDeletion
        from sqlalchemy import select

        db = SessionLocal()
        try:
            q = select(KnowledgeDocDeletion).order_by(KnowledgeDocDeletion.deleted_at.desc())
            rows = db.execute(q).scalars().all()
            return [
                {
                    "id":               r.id,
                    "doc_id":           r.doc_id,
                    "name":             r.name,
                    "filename":         r.filename,
                    "namespace":        r.namespace,
                    "file_size":        r.file_size,
                    "chunk_count":      r.chunk_count,
                    "visibility":       r.visibility or "PUBLIC",
                    "department_ids":   r.department_ids or [],
                    "product_id":       str(r.product_id) if r.product_id else "",
                    "domain":           r.domain or "",
                    "spec_version":     r.spec_version or "",
                    "source_type":      r.source_type or "",
                    "original_ext":     r.original_ext or "",
                    "status":           r.status or "ACTIVE",
                    "uploaded_by":      r.uploaded_by,
                    "uploaded_by_dept": r.uploaded_by_dept or "",
                    "approved_by":      r.approved_by,
                    "approved_at":      r.approved_at.isoformat() if r.approved_at else None,
                    "doc_created_at":   r.doc_created_at.isoformat() if r.doc_created_at else None,
                    "deleted_by":       r.deleted_by,
                    "deleted_by_dept":  r.deleted_by_dept or "",
                    "deleted_at":       r.deleted_at.isoformat() if r.deleted_at else None,
                }
                for r in rows
            ]
        finally:
            db.close()
    except Exception as e:
        logger.error(f"docs_store.list_deletion_history failed: {e}")
        return []


def delete_doc(doc_id: str, deleted_by: str = None, deleted_by_dept: str = None) -> dict:
    """Delete a document from pgvector and the metadata DB.

    KB Deletion History (2026-08-06): if the doc's status is ACTIVE at the
    moment of deletion (i.e. it was fully indexed and RAG-searchable at some
    point), a snapshot of its fields is written to knowledge_doc_deletions
    in the SAME transaction as the knowledge_docs row delete, so the two can
    never diverge. Docs deleted while PENDING_APPROVAL or REJECTED never went
    live, so no history row is written for them — unchanged behaviour.

    `deleted_by` / `deleted_by_dept` identify who performed the deletion
    (passed from the router's current_user) and are stored on the snapshot
    row for the GET /kb/deleted-history ACL filter.
    """
    # Phase 3f: drop shared doc cache for this doc before touching SQL —
    # avoids a brief race where another request warms the cache from the
    # row we're about to delete.
    try:
        from store import kb_doc_cache as _kbc_del
        _kbc_del.invalidate(doc_id)
    except Exception:
        pass
    # Snapshot the row's fields BEFORE it is mutated/deleted, so the history
    # row (if any) reflects the doc as it was when it was ACTIVE, not the
    # transient "DELETING" status we're about to write.
    _snapshot_fields = None
    try:
        from db.database import SessionLocal as _SL_mark_del
        from db.models import KnowledgeDocument as _KD_mark_del
        _db_mark_del = _SL_mark_del()
        try:
            _doc_mark_del = _db_mark_del.get(_KD_mark_del, doc_id)
            if _doc_mark_del:
                if _doc_mark_del.status == "ACTIVE":
                    _snapshot_fields = {
                        "doc_id":           _doc_mark_del.id,
                        "name":             _doc_mark_del.name,
                        "filename":         _doc_mark_del.filename,
                        "namespace":        _doc_mark_del.namespace,
                        "file_size":        _doc_mark_del.file_size,
                        "chunk_count":      _doc_mark_del.chunk_count,
                        "visibility":       _doc_mark_del.visibility,
                        "department_ids":   _doc_mark_del.department_ids or [],
                        "product_id":       _doc_mark_del.product_id,
                        "domain":           _doc_mark_del.domain,
                        "spec_version":     _doc_mark_del.spec_version,
                        "source_type":      _doc_mark_del.source_type,
                        "original_ext":     _doc_mark_del.original_ext,
                        "status":           _doc_mark_del.status,
                        "uploaded_by":      _doc_mark_del.uploaded_by,
                        "uploaded_by_dept": _doc_mark_del.uploaded_by_dept,
                        "approved_by":      _doc_mark_del.approved_by,
                        "approved_at":      _doc_mark_del.approved_at,
                        "doc_created_at":   _doc_mark_del.created_at,
                    }
                _doc_mark_del.status = "DELETING"
                _db_mark_del.commit()
        finally:
            _db_mark_del.close()
    except Exception as _mark_del_exc:
        logger.warning(f"[DELETE_DOC] doc_id={doc_id} could not mark DELETING: {_mark_del_exc}")
    # Remove all on-disk files for this doc from KB_DOC_STORAGE_PATH:
    #   <doc_id>.md          — canonical markdown body (used by RAG / kb_doc_cache)
    #   <doc_id>.<orig_ext>  — original binary (PDF/DOCX/etc.) if not yet deleted
    #                          by kb_worker post-activation. May already be gone
    #                          for activated docs; best-effort for pending/rejected ones.
    # Best-effort — pgvector rows and the DB record are the canonical state.
    try:
        from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
        import os as _os_del

        # Delete .md file
        _md_path = _os_del.path.join(_KB_FS_ROOT, f"{doc_id}.md")
        if _os_del.path.isfile(_md_path):
            _os_del.unlink(_md_path)
            logger.info(f"[DELETE_DOC] doc_id={doc_id} removed .md file '{_md_path}'")
        try:
            from store.kb_replication import delete_file as _delete_replica_file
            _delete_replica_file(doc_id, "md", kind="markdown")
        except Exception as _rep_md_del_exc:
            logger.warning(
                f"[DELETE_DOC] doc_id={doc_id} "
                f"replica markdown delete failed: {_rep_md_del_exc}"
            )

        # Delete original binary (PDF/DOCX/etc.) — may already be gone for
        # activated docs (kb_worker deletes it post-activation), but still
        # present for PENDING_APPROVAL or REJECTED docs that were never activated.
        _orig_ext = None
        try:
            from db.database import SessionLocal as _SL_del
            from db.models import KnowledgeDocument as _KD_del
            _db_ext = _SL_del()
            try:
                _doc_ext = _db_ext.get(_KD_del, doc_id)
                _orig_ext = _doc_ext.original_ext if _doc_ext else None
            finally:
                _db_ext.close()
        except Exception:
            pass

        if _orig_ext:
            _orig_path = _os_del.path.join(_KB_FS_ROOT, f"{doc_id}.{_orig_ext}")
            if _os_del.path.isfile(_orig_path):
                _os_del.unlink(_orig_path)
                logger.info(
                    f"[DELETE_DOC] doc_id={doc_id} "
                    f"removed original binary '{_orig_path}'"
                )
            try:
                from store.kb_replication import delete_file as _delete_replica_file
                _delete_replica_file(doc_id, _orig_ext, kind="original")
            except Exception as _rep_del_exc:
                logger.warning(
                    f"[DELETE_DOC] doc_id={doc_id} "
                    f"replica original delete failed: {_rep_del_exc}"
                )

    except Exception as _fs_del_e:
        logger.warning(f"[DELETE_DOC] doc_id={doc_id} FS cleanup non-fatal: {_fs_del_e}")
    try:
        # Delete pgvector rows (PGS02)
        try:
            from db.database import VectorSessionLocal
            from sqlalchemy import text as sql_text
            db = VectorSessionLocal()
            try:
                db.execute(
                    sql_text(
                        "DELETE FROM document_embeddings "
                        "WHERE metadata->>'doc_id' = :doc_id"
                    ),
                    {"doc_id": doc_id},
                )
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"docs_store: pgvector delete failed (non-fatal): {e}")

        # Delete DB record (PGS01) — and, if the doc was ACTIVE, write its
        # deletion-history snapshot in the SAME transaction so the two rows
        # can never diverge (either both land, or neither does).
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        db2 = SessionLocal()
        try:
            doc = db2.get(KnowledgeDocument, doc_id)
            if doc:
                _ns = doc.namespace
                if _snapshot_fields is not None:
                    from db.models import KnowledgeDocDeletion
                    db2.add(KnowledgeDocDeletion(
                        **_snapshot_fields,
                        deleted_by=deleted_by or "unknown",
                        deleted_by_dept=deleted_by_dept,
                    ))
                db2.delete(doc)
                db2.commit()
                # Remove namespace from Redis if no more chunks remain for it
                try:
                    from db.database import VectorSessionLocal
                    from sqlalchemy import text as sql_text2
                    vdb2 = VectorSessionLocal()
                    try:
                        remaining = vdb2.execute(
                            sql_text2("SELECT COUNT(*) FROM document_embeddings WHERE LOWER(repo) = LOWER(:repo)"),
                            {"repo": f"docs_kb:{_ns}"},
                        ).scalar()
                        if not remaining:
                            _docs_kv().srem("docs:namespaces", _ns)
                    finally:
                        vdb2.close()
                except Exception:
                    pass
                return {"success": True}
            return {"success": False, "error": "Document not found"}
        finally:
            db2.close()

    except Exception as e:
        logger.error(f"docs_store.delete_doc failed: {e}")
        return {"success": False, "error": str(e)}


def list_namespaces() -> List[str]:
    """Return registered namespaces, falling back to DB when KV is cold."""
    rc = None
    try:
        rc = _docs_kv()
        names = {n for n in (rc.smembers("docs:namespaces") or set()) if n}
        if names:
            return sorted(names)
    except Exception as e:
        logger.warning(f"docs_store: namespace KV unavailable, falling back to DB: {e}")

    names = set()
    try:
        from db.database import SessionLocal
        from db.models import KnowledgeDocument
        db = SessionLocal()
        try:
            rows = (
                db.query(KnowledgeDocument.namespace)
                .filter(KnowledgeDocument.namespace.isnot(None))
                .filter(KnowledgeDocument.status.in_(["PENDING_APPROVAL", "APPROVED", "AUTO_APPROVED"]))
                .distinct()
                .all()
            )
            names.update(r[0] for r in rows if r and r[0])
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"docs_store: namespace DB fallback failed: {e}")

    if not names:
        try:
            from db.database import VectorReadSessionLocal
            from sqlalchemy import text as _sql_text
            vdb = VectorReadSessionLocal()
            try:
                rows = vdb.execute(_sql_text(
                    "SELECT DISTINCT repo FROM document_embeddings "
                    "WHERE repo LIKE 'docs_kb:%'"
                )).fetchall()
                for r in rows:
                    repo = r[0] if r else ""
                    if repo.startswith("docs_kb:"):
                        names.add(repo[len("docs_kb:"):])
            finally:
                vdb.close()
        except Exception as e:
            logger.warning(f"docs_store: namespace vector fallback failed: {e}")

    if names and rc is not None:
        try:
            for name in names:
                rc.sadd("docs:namespaces", name)
        except Exception:
            pass

    return sorted(names)


# ============================================================
# Part U13 (2026-06-08) — original-file accessor for citation "Open original" link
# ============================================================
def get_original_path(doc_id: str, original_ext: Optional[str] = None) -> Optional[str]:
    """
    Return the absolute path to the retained original binary file for `doc_id`,
    or None if no original was retained.

    The original lives at KB_DOC_STORAGE_PATH/<doc_id>.<ext> alongside the
    canonical KB_DOC_STORAGE_PATH/<doc_id>.md. `original_ext` may be passed
    by the caller (faster — skips a SELECT); when omitted we look it up on
    the KnowledgeDocument row.

    Used by routers/kb_doc_router.py:/api/kb/original/<doc_id>.<ext> to serve
    the binary back to the Chat UI's citation footer "Open original" link.
    """
    try:
        from core.config import KB_DOC_STORAGE_PATH as _KB_FS_ROOT
    except Exception as _ce:
        logger.warning(f"get_original_path: KB_DOC_STORAGE_PATH unavailable: {_ce}")
        return None

    _ext = (original_ext or "").strip().lower().lstrip(".") or None
    if not _ext:
        try:
            from db.database import SessionLocal
            from db.models import KnowledgeDocument
            _db = SessionLocal()
            try:
                _doc = _db.get(KnowledgeDocument, doc_id)
                _ext = (_doc.original_ext if _doc else None) or None
            finally:
                _db.close()
        except Exception as _le:
            logger.debug(f"get_original_path: doc lookup failed for {doc_id}: {_le}")
            return None
        if not _ext:
            return None

    _abs = os.path.join(_KB_FS_ROOT, f"{doc_id}.{_ext}")
    return _abs if os.path.exists(_abs) else None
