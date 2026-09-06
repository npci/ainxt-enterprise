# SPDX-License-Identifier: MIT
"""
prose_translate.py — Prose-vs-code segmentation for a CLI translation wrapper.

PURPOSE
-------
Only natural-language chat prose may be translated. ALL code, identifiers,
file paths, URLs, emails, CLI flags and markdown syntax MUST stay English,
byte-for-byte verbatim. The codebase is shared globally; corrupting code is
unacceptable.

PUBLIC API
----------
    translate_prose(markdown_text, target_lang, translate_fn) -> str

    where ``translate_fn`` takes a list of English prose strings and returns
    the translated list (1:1, same length, same order). This is the
    IndicTrans2 call, treated as an opaque black box.

ARCHITECTURE (PROSE-ISOLATION)
------------------------------
The translator NEVER sees a placeholder, a backtick, a path, an identifier,
a URL, a markdown marker, or any structural byte. We achieve this by:

1. Splitting the document into LINES, running a line-level fence state machine
   (which understands blockquote ``>`` prefixes and indentation) so backticks /
   hashes / pipes inside a fenced code block are never misread.

2. For every translatable line we strip the leading blockquote markers and
   indentation FIRST (preserved verbatim), then peel the markdown skeleton
   (heading hashes, list markers, table pipes), then tokenize the inner text
   into an ORDERED SEQUENCE OF SPANS. Every span is tagged either:
       - VERBATIM  (code, inline code, path, URL, email, identifier, flag,
                    markdown syntax, indentation, markers) — kept English; OR
       - PROSE     (pure natural language) — eligible for translation.

3. ALL prose spans across the WHOLE document are collected into one list.
   ``translate_fn`` is called EXACTLY ONCE on that list. Because every element
   is a pure natural-language fragment with no structural bytes, the translator
   can never mangle a placeholder, backtick or path — there are none to mangle.

4. The translated prose fragments are spliced back into their exact original
   positions. Verbatim spans are emitted unchanged. The result preserves the
   exact newline structure and is a byte-for-byte round-trip under identity.

OVER-MASK BIAS (safety invariant)
---------------------------------
When a token is AMBIGUOUS we treat it as VERBATIM (keep English). Under-masking
corrupts shared code (UNACCEPTABLE); over-masking merely leaves a word in
English (acceptable per owner). So proper names, ALL-CAPS English words and
Latin abbreviations may stay English — that is fine. We bias HARD towards never
corrupting code.

CODE-DETECTION HEURISTIC (bare token)
-------------------------------------
A bare token is treated as code/VERBATIM if it contains ANY of:
  - a digit
  - an underscore
  - an interior uppercase-after-lowercase (lowerCamel / PascalCase tail)
  - an uppercase-run-then-lowercase (acronym-prefix, e.g. HTMLParser)
  - a dot between word chars (dotted.call)
  - a slash between word chars (path-like)
Plain English words (including ALL-CAPS such as NOTE/YES and slash-compounds
such as and/or, input/output) translate OR stay English — either is acceptable.

Pure stdlib (``re`` only). Import-safe, zero side effects at import time.
No catastrophic regex backtracking (every pattern is linear / possessive-safe).
"""

from __future__ import annotations

import re
from typing import Callable, List, Tuple

__all__ = ["translate_prose", "Span", "tokenize_line"]


# ===========================================================================
# Span model
# ===========================================================================

# A span is (text, is_prose). is_prose=True means the text is a pure
# natural-language fragment eligible for translation. is_prose=False means the
# text is emitted verbatim (code / structure / ambiguous).
Span = Tuple[str, bool]


# ===========================================================================
# Verbatim-construct regexes (applied to the *inner* text of a line, after the
# leading blockquote / indentation skeleton has already been peeled off).
#
# Ordering inside the combined alternation matters: longer / more specific
# constructs must come first so we never split a path/email into pieces.
# Every sub-pattern is linear-time.
# ===========================================================================

# --- Inline code: a run of N backticks ... matching run of N backticks ------
# Linear: closing token is a fixed backtick run. Matched per-line only.
_INLINE_CODE = r"(?P<bt>`+)(?P<code>.*?)(?P=bt)"

# --- Dangling backtick run: a backtick run with NO closing pair on the line -
# Treated conservatively as verbatim so a code fence never leaks as prose
# (multi-line inline code, fence fragments). Matches one-or-more backticks.
_DANGLING_BT = r"`+"

# --- URLs — scheme-based or bare www. Stops before trailing sentence punct. --
_URL = (
    r"(?:(?:https?|ftp)://|mailto:|www\.)"
    r"[^\s<>()\[\]{}\"'`]+"
    r"(?<![.,;:!?)\]}>\"'])"  # do not swallow trailing sentence punctuation
)

# --- Email address (whole address as ONE verbatim unit) ---------------------
# local-part @ domain. Local part allows the usual RFC-ish atom chars. The
# domain's final TLD label is matched non-greedily of any trailing sentence
# period: we forbid a following word char or '@', but a trailing '.' (sentence
# period) is allowed to remain OUTSIDE the match so it can be prose.
_EMAIL = (
    r"(?<![\w.+@-])"
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}"
    r"(?![\w@])"
)

# --- Inline math:  $$...$$  or  $...$  (no newline inside, non-greedy) -------
# Conservative: must open and close with a matching run of '$' on the same
# line. Kept verbatim so a translator never mangles LaTeX. A lone '$'
# (currency) does not match because it has no closing '$' on the line.
_MATH = r"\$\$[^\n]+?\$\$|\$[^$\n]+?\$"

# --- LaTeX / backslash command:  \alpha \frac \sum  etc. (backslash + word) --
# Also covers escaped sequences that must stay verbatim. A bare backslash with
# no following letter is not matched (left as prose punctuation).
_BACKSLASH_CMD = r"\\[A-Za-z]+"

# --- Windows absolute path, INCLUDING spaces (C:\Program Files\Git\bin) ------
# Drive-letter+colon+backslash, or UNC \\server. After the root we consume
# path segments; a space is only absorbed if the NEXT non-space run is itself
# a path segment continued by a backslash (so we don't eat trailing prose).
# Implemented as: root + first-seg + (optional " seg")* where a continued
# segment must be followed (eventually) by a backslash or end-of-path char.
# To stay linear and avoid swallowing prose, we allow internal single spaces
# between segments only when both flanking chars are path chars.
_WIN_SEG = r"[^\s<>()\[\]{}\"'`,;|\\]+"
# A space is only absorbed when it is INTERNAL to the path, i.e. the space-
# separated segment is itself followed by a backslash (so "Program Files\" is
# part of the path but a trailing " for setup" — no following backslash — is
# NOT swallowed). Backslash-joined segments are always part of the path.
_WIN_PATH = (
    r"(?:[A-Za-z]:\\|\\\\)"            # drive root  C:\  or UNC  \\
    + _WIN_SEG
    + r"(?:"
    + r"\\" + _WIN_SEG                 # \seg  (always part of path)
    + r"|"
    + r" " + _WIN_SEG + r"(?=\\)"      # " seg" only if followed by a backslash
    + r")*"
)

# Combined verbatim-construct scanner. These are the ONLY constructs that may
# legitimately appear glued to prose without an intervening space, or that span
# whitespace (Windows paths). Everything else — bare paths, dotted chains, CLI
# flags, snake_case / camelCase / PascalCase / digit identifiers — is a single
# whitespace-delimited token classified WHOLE by the per-token heuristic, so we
# never fragment a path/identifier into a prose-leaking piece.
#
# Order: inline code first (consumes backticks), then URL, email, Windows path
# (spans internal spaces), then dangling-backtick fallback.
_COMBINED = re.compile(
    "(?P<inline>" + _INLINE_CODE + ")"
    "|(?P<url>" + _URL + ")"
    "|(?P<email>" + _EMAIL + ")"
    "|(?P<math>" + _MATH + ")"
    "|(?P<winpath>" + _WIN_PATH + ")"
    "|(?P<backslash>" + _BACKSLASH_CMD + ")"
    "|(?P<dangling>" + _DANGLING_BT + ")"
)


# ===========================================================================
# Bare-token code heuristic
# ===========================================================================
#
# Applied to each whitespace/structure-delimited bare token in residual prose.
# Over-mask bias: ambiguous -> code (verbatim). The heuristic flags a token as
# code if it contains ANY structural code signal.

# interior lowercase-then-Uppercase  (lowerCamel / PascalCase tail): aB
_RE_LOWER_UPPER = re.compile(r"[a-z][A-Z]")
# uppercase-run-then-lowercase  (acronym prefix): AAb  e.g. HTMLParser, OAuth
_RE_UPPER_RUN_LOWER = re.compile(r"[A-Z]{2,}[a-z]")
# dot between word chars:  a.b
_RE_DOT_WORD = re.compile(r"\w\.\w")
# slash between word chars: a/b
_RE_SLASH_WORD = re.compile(r"\w/\w")


def _looks_like_code(tok: str) -> bool:
    """
    Decide whether a bare token is code/identifier (VERBATIM) vs plain prose.

    Over-mask bias: when ambiguous we return True (keep English). A token is
    code if it contains ANY of: a digit, an underscore, an interior
    lowercase->Uppercase transition, an uppercase-run->lowercase transition, a
    dot between word chars, or a slash between word chars.

    Plain English words (including ALL-CAPS like NOTE/YES, and slash-compounds
    like and/or, input/output) return False here -> they are eligible for
    translation. (Slash-compounds: a "/" between two word chars DOES trigger
    code per the spec heuristic; however the owner says slash-compounds may
    translate OR stay English, either acceptable. We treat a single internal
    slash joining two ALPHA-ONLY english-ish runs as PROSE to favour
    localisation, but anything with a path root, digit, or mixed case stays
    code. See _slash_is_prose.)
    """
    if not tok:
        return False
    # CLI flag: -x, --long, --flag=value. A single '-' or '--' lead followed by
    # a letter. (A bare '-' or '--' is prose punctuation, not a flag.)
    if tok.startswith("-") and len(tok) >= 2 and (tok[1].isalpha() or tok[1] == "-"):
        return True
    # Rooted path: leading '/', './' or '../'  (e.g. /etc/hosts, ./build, ../x)
    if tok.startswith("/") or tok.startswith("./") or tok.startswith("../"):
        return True
    if any(c.isdigit() for c in tok):
        return True
    if "_" in tok:
        return True
    if _RE_LOWER_UPPER.search(tok):
        return True
    if _RE_UPPER_RUN_LOWER.search(tok):
        return True
    if _RE_DOT_WORD.search(tok):
        return True
    if _RE_SLASH_WORD.search(tok):
        # slash between word chars. Per owner, prose slash-compounds
        # (and/or, input/output, read/write) may translate. Treat a pure
        # alpha/alpha (or multi-part alpha) compound with no other code signal
        # as PROSE; otherwise code.
        if _slash_is_prose(tok):
            return False
        return True
    return False


_RE_ALPHA_SLASH_COMPOUND = re.compile(r"^[a-z]+(?:/[a-z]+)+$")


def _slash_is_prose(tok: str) -> bool:
    """
    A slash token is treated as prose (translatable) only if it is a pure
    alpha/alpha[/alpha...] compound (and/or, input/output, read/write, yes/no)
    with NO digit, underscore, dot, mixed-case code signal, or path root. This
    favours localisation of English conjunctions while never touching paths.
    """
    if not _RE_ALPHA_SLASH_COMPOUND.match(tok):
        return False
    # No mixed-case identifier hiding inside (e.g. "io/Reader") and no all-caps
    # acronym that is clearly code: we already require pure alpha; mixed case
    # inside a segment would have tripped _RE_LOWER_UPPER / _RE_UPPER_RUN_LOWER
    # above before reaching here. So a clean alpha compound is prose.
    return True


# Token splitter for residual prose: a "token" is a maximal run of non-space
# characters; we translate the spaces verbatim between tokens but classify each
# token. Trailing/leading ASCII punctuation is split OFF the token so that
# "MCPRegistry." keeps the identifier verbatim and the "." as (possibly) prose
# tail. We keep punctuation attached to the prose run, not the code token.
#
# We split the inner text into alternating (word, gap) where word is a run of
# token chars and gap is the run of separators. To respect the heuristic we
# define a token char as anything that is not whitespace; but we then peel
# leading/trailing punctuation that is NOT a code signal.
_WORD_RE = re.compile(r"\S+")

# Punctuation safe to peel from a token's edges (won't change code meaning).
# We do NOT peel '/', '.', '_', '-', '@', ':' from the *interior*; peeling is
# only of leading/trailing runs of these "sentence" punctuation chars.
_EDGE_PUNCT = set(",;:!?)('\"[]{}<>")
# Note: '.' is intentionally NOT in edge punct for trailing peel when it could
# be part of a path/host, but for a token classified as prose a trailing '.'
# is fine to keep with prose. We handle '.' specially below.


def _peel(token: str) -> Tuple[str, str, str]:
    """
    Split a non-space token into (leading_punct, core, trailing_punct), where
    leading/trailing punct are runs of sentence punctuation safe to treat as
    prose-adjacent. The core is what we classify with _looks_like_code.

    We are conservative: a trailing '.' is peeled ONLY if the core (without it)
    is non-empty and the core does not itself look like a dotted/path construct
    (those are handled before tokenisation by _COMBINED). Since _COMBINED has
    already masked real dotted/path constructs, any '.' reaching here is
    sentence punctuation -> safe to peel.
    """
    i = 0
    n = len(token)
    while i < n and token[i] in _EDGE_PUNCT:
        i += 1
    j = n
    while j > i and token[j - 1] in _EDGE_PUNCT:
        j -= 1
    # Peel a single trailing run of '.' (sentence period / ellipsis) — but only
    # if something remains. e.g. "approach." -> core "approach", trail ".".
    # Also handle trailing ".," combos already covered by _EDGE_PUNCT for ','.
    while j > i and token[j - 1] == ".":
        j -= 1
    lead = token[:i]
    core = token[i:j]
    trail = token[j:]
    return lead, core, trail


# ===========================================================================
# Inner-text tokeniser  -> ordered list of spans
# ===========================================================================


def _tokenize_inner(inner: str) -> List[Span]:
    """
    Tokenise the inner text (skeleton already peeled) into an ordered list of
    spans. Verbatim constructs (inline code, dangling backticks, URLs, emails,
    paths, dotted calls, flags) are emitted first via _COMBINED; the residual
    text between/around them is further split into prose words vs bare code
    tokens via the heuristic. Whitespace is preserved verbatim.
    """
    spans: List[Span] = []
    pos = 0
    for m in _COMBINED.finditer(inner):
        start, end = m.span()
        if start < pos:
            continue  # defensive (finditer is non-overlapping)
        if start > pos:
            _tokenize_residual(inner[pos:start], spans)
        spans.append((m.group(0), False))  # verbatim construct
        pos = end
    if pos < len(inner):
        _tokenize_residual(inner[pos:], spans)
    return spans


def _tokenize_residual(text: str, spans: List[Span]) -> None:
    """
    Split residual text (no inline-code/URL/path/email/flag constructs left)
    into prose runs and verbatim code tokens. Whitespace and the verbatim code
    tokens segment the prose. Consecutive prose words (+ the whitespace and
    sentence punctuation between them) coalesce into a single prose span so the
    translator gets natural multi-word fragments.
    """
    if not text:
        return

    prose_buf: List[str] = []

    def flush_prose() -> None:
        if prose_buf:
            chunk = "".join(prose_buf)
            prose_buf.clear()
            # Only emit as PROSE if there is at least one alphabetic char;
            # otherwise it is pure symbols/whitespace -> verbatim.
            if _has_alpha(chunk):
                spans.append((chunk, True))
            else:
                spans.append((chunk, False))

    pos = 0
    n = len(text)
    for m in _WORD_RE.finditer(text):
        wstart, wend = m.span()
        gap = text[pos:wstart]  # whitespace between previous token and this
        token = m.group(0)
        pos = wend

        lead, core, trail = _peel(token)

        if core and _looks_like_code(core):
            # Emit any buffered prose + the gap as prose (whitespace belongs
            # with the surrounding sentence). Lead/trail punctuation around a
            # code token are emitted verbatim so we never glue prose to code.
            if gap:
                prose_buf.append(gap)
            flush_prose()
            if lead:
                spans.append((lead, False))
            spans.append((core, False))  # verbatim code token
            if trail:
                spans.append((trail, False))
        else:
            # Plain prose word: accumulate gap + whole token into the prose
            # buffer (lead/core/trail are all prose here).
            prose_buf.append(gap)
            prose_buf.append(token)

    # trailing gap after the last token
    trailing = text[pos:]
    if trailing:
        prose_buf.append(trailing)
    flush_prose()


_RE_ALPHA = re.compile(r"[A-Za-zÀ-￿]")


def _has_alpha(s: str) -> bool:
    """True if s contains at least one alphabetic (incl. non-ASCII) char."""
    return bool(_RE_ALPHA.search(s))


# ===========================================================================
# Line-level markdown classification helpers
# ===========================================================================

# Blockquote prefix: leading indent + one-or-more '>' (each optionally followed
# by a single space). Captured so we can strip it BEFORE fence/inline scanning
# and re-emit it verbatim.
_QUOTE_PREFIX_RE = re.compile(r"^(?P<prefix>(?:[ \t]*>[ \t]?)+)(?P<rest>.*)$")

# Fenced code open/close: 3+ backticks or 3+ tildes, optional indent, optional
# info string. Run AFTER stripping blockquote prefix + indentation.
_FENCE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# ATX heading: up to 3 spaces indent, 1-6 #, a space, then text.
_HEADING_RE = re.compile(
    r"^(?P<lead>[ \t]{0,3})(?P<hashes>#{1,6})(?P<sp>[ \t]+)(?P<text>.*)$"
)

# Unordered list item: indent + (-|*|+) + space + text
_ULIST_RE = re.compile(r"^(?P<lead>[ \t]*)(?P<marker>[-*+])(?P<sp>[ \t]+)(?P<text>.*)$")

# Ordered list item: indent + number + (.|)) + space + text
_OLIST_RE = re.compile(
    r"^(?P<lead>[ \t]*)(?P<num>\d+)(?P<dot>[.)])(?P<sp>[ \t]+)(?P<text>.*)$"
)

# Indented code block (4+ leading spaces or a tab then non-space). Only applied
# when the line is not a list/heading/quote and not a list continuation.
_INDENT_CODE_RE = re.compile(r"^(?: {4,}|\t)\S")

_TABLE_PIPE = "|"

# Horizontal rule. IMPORTANT (setext fix): a '---'/'==='/'***' line is only a
# true HR when it is NOT acting as a setext underline for the preceding line.
# We detect setext at the caller (needs previous-line context). Here the regex
# just matches the HR/underline *shape*.
_HRULE_RE = re.compile(r"^[ \t]*([-*_])([ \t]*\1){2,}[ \t]*$")
# Setext underline: a run of '=' (H1) or '-' (H2), optionally surrounded by
# spaces, and nothing else. '===' is unambiguous (never an HR). '---' is shared
# with HR — disambiguated by preceding-line context.
_SETEXT_RE = re.compile(r"^[ \t]*(=+|-+)[ \t]*$")
_TABLE_DELIM_RE = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)*\|?[ \t]*$"
)

# Comparison-operator leader: a line starting with '>=' / '>' followed by a
# space-then-non-quote, used to AVOID misreading '>= 3.8' as a blockquote.
# A real blockquote is '> text'; a comparison is '>=' or '>' immediately
# followed by '=' or by a digit/operator context. We treat a line as a
# blockquote ONLY if, after the leading indent, it starts with '>' that is NOT
# immediately followed by '='. ('>= 3.8' and '>=3' are comparisons, not quotes.)
_GE_OP_RE = re.compile(r"^[ \t]*>=")


def _is_setext_underline(line: str) -> Tuple[bool, str]:
    """
    Return (is_underline, level_char) where level_char is '=' or '-' if the
    line is a setext underline shape, else (False, '').
    """
    m = _SETEXT_RE.match(line)
    if not m:
        return False, ""
    run = m.group(1)
    return True, run[0]


# ===========================================================================
# Table-row splitting (GFM) — pipes verbatim, cells tokenised
# ===========================================================================


def _split_table_row(text: str) -> List[Tuple[str, bool]]:
    """
    Split a GFM table row into alternating (segment, is_cell) parts so the pipe
    skeleton is preserved and only cell *contents* are tokenised. Escaped pipes
    (\\|) inside cells stay within the cell content.
    """
    parts: List[Tuple[str, bool]] = []
    buf: List[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            buf.append(text[i : i + 2])
            i += 2
            continue
        if ch == _TABLE_PIPE:
            parts.append(("".join(buf), True))
            buf = []
            parts.append(("|", False))
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append(("".join(buf), True))
    return parts


def _is_escaped_only_pipe(text: str) -> bool:
    """True if every pipe in text is backslash-escaped (so not a table row)."""
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 1 < n:
            i += 2
            continue
        if text[i] == _TABLE_PIPE:
            return False
        i += 1
    return True


# ===========================================================================
# Per-line processing -> spans
# ===========================================================================


def _process_inner_block(inner: str, spans: List[Span]) -> None:
    """
    Process the body text of a line whose leading markdown skeleton (blockquote
    prefix, indent, heading hashes, list markers) has already been emitted. If
    it looks like a GFM table row, split on pipes and tokenise each cell; else
    tokenise the whole inner text.
    """
    if _TABLE_PIPE in inner and not _is_escaped_only_pipe(inner):
        for seg_text, is_cell in _split_table_row(inner):
            if is_cell:
                if seg_text.strip() == "":
                    spans.append((seg_text, False))
                else:
                    spans.extend(_tokenize_inner(seg_text))
            else:
                spans.append((seg_text, False))  # the '|'
    else:
        spans.extend(_tokenize_inner(inner))


def tokenize_line(line: str) -> List[Span]:
    """
    Tokenise a single line's inner text (no fence/structure context) into an
    ordered list of spans. Exposed for testing / reuse. NOTE: this does NOT do
    fence or blockquote handling — that requires full-document context and
    lives in ``_segment_document``. Use it only on already-peeled inner text.
    """
    spans: List[Span] = []
    _process_inner_block(line, spans)
    return spans


def _segment_document(text: str) -> List[Span]:
    """
    Parse the markdown into a single ordered list of spans for the WHOLE
    document. Verbatim spans carry structure/code; prose spans carry pure
    natural language. Newlines are preserved as verbatim spans so the exact
    line structure round-trips.
    """
    spans: List[Span] = []
    lines = text.split("\n")

    in_fence = False
    fence_char = ""
    fence_len = 0
    fence_quote_prefix = ""  # blockquote prefix that introduced the fence

    for li, line in enumerate(lines):
        nl = "\n" if li < len(lines) - 1 else ""

        # ---- strip a blockquote prefix (and indentation) up front ----------
        # This MUST happen before fence detection so '> ```python' is seen as a
        # fence, and before inline scanning so '> ```' closes correctly.
        qm = _QUOTE_PREFIX_RE.match(line)
        if qm and qm.group("prefix"):
            quote_prefix = qm.group("prefix")
            body = qm.group("rest")
        else:
            quote_prefix = ""
            body = line

        # ---- inside a fenced code block: everything verbatim until close ---
        if in_fence:
            # A close must share the same blockquote container depth context;
            # we compare on the stripped body. Strip THIS line's quote prefix
            # for fence-close detection.
            fm = _FENCE_RE.match(body)
            is_close = (
                fm is not None
                and fm.group("fence")[0] == fence_char
                and len(fm.group("fence")) >= fence_len
                and fm.group("info").strip() == ""
            )
            spans.append((line + nl, False))  # whole line verbatim
            if is_close:
                in_fence = False
                fence_char = ""
                fence_len = 0
                fence_quote_prefix = ""
            continue

        # ---- fence open? ---------------------------------------------------
        fm = _FENCE_RE.match(body)
        if fm:
            in_fence = True
            fence_char = fm.group("fence")[0]
            fence_len = len(fm.group("fence"))
            fence_quote_prefix = quote_prefix
            spans.append((line + nl, False))  # whole line verbatim (incl. '>')
            continue

        # ---- blank line ----------------------------------------------------
        if line.strip() == "":
            spans.append((line + nl, False))
            continue

        # ---- setext underline for the PREVIOUS line? -----------------------
        # If the previous emitted line was plain prose and THIS line is a
        # setext underline ('===' or '---'), keep the underline verbatim (it is
        # markdown structure, not prose, and not an HR).
        is_underline, _lvl = _is_setext_underline(line)
        if is_underline and _prev_line_was_prose(spans):
            spans.append((line + nl, False))
            continue

        # ---- horizontal rule / table delimiter row (no prose) --------------
        # '===' alone never reaches here as HR (handled above as setext). A
        # '---' line that is NOT a setext underline is a genuine HR.
        if _HRULE_RE.match(line) or _TABLE_DELIM_RE.match(line):
            spans.append((line + nl, False))
            continue

        # ---- comparison operator at line start ('>= 3.8 is required.') -----
        # Must be checked BEFORE blockquote so '>=' is not read as a quote.
        if _GE_OP_RE.match(line):
            # Treat the WHOLE line as a normal paragraph; '>=' and the operand
            # token will be classified by the tokeniser (digit -> verbatim).
            _process_inner_block(line, spans)
            spans.append((nl, False))
            continue

        # ---- blockquote --------------------------------------------------
        # If we stripped a blockquote prefix above and the body is NOT a fence
        # (already handled), treat the prefix as verbatim skeleton and process
        # the body, which may itself be a list item, heading, etc.
        if quote_prefix:
            spans.append((quote_prefix, False))
            _process_quote_body(body, spans)
            spans.append((nl, False))
            continue

        # ---- ATX heading ---------------------------------------------------
        hm = _HEADING_RE.match(line)
        if hm:
            spans.append(
                (hm.group("lead") + hm.group("hashes") + hm.group("sp"), False)
            )
            _process_inner_block(hm.group("text"), spans)
            spans.append((nl, False))
            continue

        # ---- ordered list item --------------------------------------------
        om = _OLIST_RE.match(line)
        if om:
            spans.append(
                (
                    om.group("lead") + om.group("num") + om.group("dot") + om.group("sp"),
                    False,
                )
            )
            _process_inner_block(om.group("text"), spans)
            spans.append((nl, False))
            continue

        # ---- unordered list item ------------------------------------------
        um = _ULIST_RE.match(line)
        if um:
            spans.append((um.group("lead") + um.group("marker") + um.group("sp"), False))
            _process_inner_block(um.group("text"), spans)
            spans.append((nl, False))
            continue

        # ---- indented code block (4+ spaces / tab) ------------------------
        if _INDENT_CODE_RE.match(line):
            spans.append((line + nl, False))
            continue

        # ---- plain line (table row or paragraph) --------------------------
        _process_inner_block(line, spans)
        spans.append((nl, False))

    return spans


def _process_quote_body(body: str, spans: List[Span]) -> None:
    """
    Process the body of a blockquote line (prefix already emitted). Re-run the
    structural parsers so a list marker / heading INSIDE a blockquote is kept
    verbatim rather than translated as prose.
    """
    if body.strip() == "":
        if body:
            spans.append((body, False))
        return

    hm = _HEADING_RE.match(body)
    if hm:
        spans.append((hm.group("lead") + hm.group("hashes") + hm.group("sp"), False))
        _process_inner_block(hm.group("text"), spans)
        return

    om = _OLIST_RE.match(body)
    if om:
        spans.append(
            (
                om.group("lead") + om.group("num") + om.group("dot") + om.group("sp"),
                False,
            )
        )
        _process_inner_block(om.group("text"), spans)
        return

    um = _ULIST_RE.match(body)
    if um:
        spans.append((um.group("lead") + um.group("marker") + um.group("sp"), False))
        _process_inner_block(um.group("text"), spans)
        return

    _process_inner_block(body, spans)


def _prev_line_was_prose(spans: List[Span]) -> bool:
    """
    Look back over the spans emitted so far (skipping the trailing newline span)
    to decide whether the immediately preceding line contained translatable
    prose — used for setext-underline disambiguation. We scan back to the
    previous newline boundary and check if any span in that line was prose.
    """
    # spans currently end with a "\n" verbatim span from the previous line.
    i = len(spans) - 1
    # Skip the trailing newline span(s) of the previous line.
    if i >= 0 and spans[i][0] == "\n" and not spans[i][1]:
        i -= 1
    saw_prose = False
    saw_content = False
    # Walk back until we hit the newline that ended the line-before-previous.
    while i >= 0:
        seg_text, is_prose = spans[i]
        if "\n" in seg_text and not is_prose:
            break
        if is_prose:
            saw_prose = True
        if seg_text.strip():
            saw_content = True
        i -= 1
    return saw_prose and saw_content


# ===========================================================================
# Public API
# ===========================================================================


def translate_prose(
    markdown_text: str,
    target_lang: str,
    translate_fn: Callable[[List[str]], List[str]],
) -> str:
    """
    Translate ONLY natural-language prose in ``markdown_text`` into
    ``target_lang``, leaving all code, identifiers, paths, URLs, emails, CLI
    flags and markdown structure verbatim and preserving the exact markdown /
    newline structure.

    The translator (``translate_fn``) is invoked EXACTLY ONCE on a flat list of
    pure natural-language fragments — it NEVER sees a placeholder, a backtick, a
    path, a URL, or any structural byte. This guarantees the translator can
    never corrupt code or markdown structure.

    Parameters
    ----------
    markdown_text:
        Source document (markdown / chat prose). May be empty.
    target_lang:
        Target language code, forwarded opaquely to ``translate_fn`` if it
        accepts a second positional argument (closure-style binding otherwise).
    translate_fn:
        Callable taking ``list[str]`` of English prose fragments and returning
        the translated ``list[str]`` of the SAME length and order.

    Returns
    -------
    str
        Reconstructed document with prose translated and every verbatim span
        intact. Under an identity ``translate_fn`` the output equals the input
        byte-for-byte.
    """
    if not markdown_text:
        return markdown_text

    spans = _segment_document(markdown_text)

    # Collect ALL prose fragments across the WHOLE document, in order. Every
    # element is a pure natural-language fragment — no structural bytes.
    prose: List[str] = [text for (text, is_prose) in spans if is_prose]

    if prose:
        translated = _call_translate(translate_fn, prose, target_lang)
        if (
            not isinstance(translated, list)
            or len(translated) != len(prose)
            or not all(isinstance(x, str) for x in translated)
        ):
            # Defensive: a misbehaving black box must never corrupt structure
            # or drop content (wrong length, non-list, or non-str element) —
            # fall back to originals.
            translated = prose
    else:
        translated = []

    # Splice translated prose back into their exact positions; verbatim spans
    # are emitted unchanged.
    out_parts: List[str] = []
    tpos = 0
    for seg_text, is_prose in spans:
        if is_prose:
            out_parts.append(translated[tpos])
            tpos += 1
        else:
            out_parts.append(seg_text)
    return "".join(out_parts)


def _call_translate(
    translate_fn: Callable[..., List[str]],
    prose: List[str],
    target_lang: str,
) -> List[str]:
    """
    Invoke ``translate_fn``. Prefer the documented single-arg form
    ``translate_fn(list)``. If that raises ``TypeError`` (caller wired a
    two-arg form), retry with ``translate_fn(list, target_lang)``.
    """
    try:
        return translate_fn(prose)
    except TypeError:
        # Caller may have wired a two-arg form translate_fn(list, lang).
        try:
            return translate_fn(prose, target_lang)  # type: ignore[call-arg]
        except Exception:
            return prose
    except Exception:
        # Any failure in the opaque translator must NEVER corrupt the response
        # — fall back to the original English prose.
        return prose


# ===========================================================================
# Self-test (only runs when executed directly; not on import)
# ===========================================================================

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    def _upper_tr(items: List[str]) -> List[str]:
        # Uppercase wrapping simulates "translation" while letting us assert
        # masked/code tokens survive untouched (they are never in `items`).
        return ["<<" + s.upper() + ">>" for s in items]

    sample = (
        "# Setup the gateway\n"
        "\n"
        "Run the `embed_svc` first, then call gateway.start() on the server.\n"
        "See routers/messages_compat_router.py and ./src/engine/repl.ts here.\n"
        "Visit https://example.com/docs?x=1 or email admin@example.com.\n"
        "\n"
        "- Use the `--full` flag with AgentRunner and the API_KEY constant.\n"
        "1. First set POSTGRES_HOST in your env file gateway.py.\n"
        "\n"
        "> Note: the model_router routes simple to gpt-5-mini.\n"
        "> ```python\n"
        "> def authenticate(token: str) -> bool:\n"
        ">     return validate_jwt(token)\n"
        "> ```\n"
        "\n"
        "NOTE: THIS IS AN IMPORTANT WARNING FOR ALL USERS.\n"
        "Use e.g. the approach, i.e. call the function and/or skip it.\n"
        "The MCPRegistry uses HTMLParser, JSONDecoder, OAuth2 and iPhone.\n"
        ">= 3.8 is required.\n"
        "\n"
        "My Heading\n"
        "----------\n"
        "\n"
        "configure C:\\Program Files\\Git\\bin for setup\n"
    )

    out = translate_prose(sample, "hi", _upper_tr)
    sys.stdout.write(out)
    sys.stdout.write("\n----- roundtrip(identity) check -----\n")
    rt = translate_prose(sample, "hi", lambda xs: xs)
    sys.stdout.write("IDENTITY_OK=" + json.dumps(rt == sample) + "\n")