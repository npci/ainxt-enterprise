# SPDX-License-Identifier: Apache-2.0
"""
Context Distillation Layer — Phase 1 (deterministic, no ML dependencies).

Converts noisy machine-generated outputs (build logs, test results, retry
history, tool outputs, IDE file reads) into compact symbolic state that
preserves all actionable information while cutting 60-98% of token volume.

NEVER compress: user prompts, generated code, final answers, architectural reasoning.
COMPRESS:        build logs, stack traces, retry history, tool outputs, RAG chunks,
                 IDE file-read tool results.
"""
from __future__ import annotations

import json
import re
import hashlib
import logging
from typing import Any

# Use the platform structlog logger so compression events appear in agent.log
# at whatever LOG_LEVEL is configured (default INFO).
try:
    from core.logger import logger
except Exception:
    logger = logging.getLogger(__name__)

# ── Error line extraction ─────────────────────────────────────────────────────

# Matches file:LINE or file:[LINE,COL] error patterns.
# Handles javac, maven, gradle, pytest, go test, cargo, tsc, eslint formats.
# Maven format: path/File.java:[LINE,COL] error: message
# javac format: path/File.java:LINE: error: message
# pytest format: path/file.py:LINE: AssertionError
#
# After the filename we match:
#   [:\[]+   — colon (javac) or open-bracket (maven)
#   (\d+)    — line number (captured)
#   [^\]]*   — optional col spec like ",5" or ":10" inside Maven bracket
#   \]?      — optional closing bracket
#   [:\s]+   — separator before message
#   (.*)     — the error message (captured)
_ERR_LINE_RE = re.compile(
    r"([\w./\-]+\.(?:java|kt|kts|scala|groovy|py|js|ts|tsx|jsx|go|rs|rb|php|cs|cpp|cc|cxx|c|h|hpp|swift|m|sh|bash))"
    r"[:\[]+(\d+)(?:[,:\d]*\]?)"   # optional col spec like ,5 or :10 then optional ]
    r"[:\s]+"
    r"(?:error|Error|ERROR|exception|Exception|EXCEPTION)?[:\s]*(.*)",
    re.IGNORECASE,
)

# Maven/Gradle [ERROR] lines
_MVN_ERR_RE = re.compile(r"\[ERROR\]\s*(.*)")
_MVN_WARN_RE = re.compile(r"\[WARNING\]\s*(.*)")

# Language detection from file extension
_EXT_LANG = {
    "java": "JAVA", "kt": "KOTLIN", "kts": "KOTLIN", "scala": "SCALA",
    "py": "PY", "go": "GO", "ts": "TS", "tsx": "TS", "js": "JS", "jsx": "JS",
    "rs": "RUST", "cpp": "CPP", "cc": "CPP", "cxx": "CPP", "c": "C",
    "cs": "CS", "rb": "RB", "php": "PHP", "swift": "SWIFT",
}


def _extract_error_lines(errors: list[str], raw: str) -> list[tuple[str, str, str, str]]:
    """
    Returns list of (lang, filepath, lineno, message) tuples from build output.
    Handles Maven, Gradle, javac, pytest, go test, npm/tsc, cargo.
    """
    results: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    blob = "\n".join(errors) + "\n" + (raw or "")

    # Strip Maven/Gradle prefix so main regex can match the file:line part
    # "[ERROR] path/File.java:[442,5] error: ..." → "path/File.java:[442,5] error: ..."
    clean_blob = re.sub(r"^\s*\[(?:ERROR|WARNING|INFO)\]\s*", "", blob, flags=re.MULTILINE)

    for m in _ERR_LINE_RE.finditer(clean_blob):
        fp, lineno, msg = m.group(1), m.group(2), m.group(3).strip()
        # Skip obviously wrong matches (very short paths, test framework internals)
        if len(fp) < 4 or "junit" in fp.lower() or "TestRunner" in fp:
            continue
        ext = fp.rsplit(".", 1)[-1].lower()
        lang = _EXT_LANG.get(ext, "?")
        msg = msg[:120]
        key = f"{fp}:{lineno}:{msg[:40]}"
        if key not in seen:
            seen.add(key)
            results.append((lang, fp.lstrip("/").replace("workspace/", "", 1), lineno, msg))

    # Maven [ERROR] lines not matched by the file-pattern (e.g. build lifecycle errors)
    if not results:
        for m in _MVN_ERR_RE.finditer(blob):
            line = m.group(1).strip()[:150]
            if line and line not in seen:
                seen.add(line)
                results.append(("MVN", "", "", line))
            if len(results) >= 10:
                break

    return results[:20]  # cap at 20 distinct errors


# ── Public compression functions ──────────────────────────────────────────────

def compress_build_errors(errors: list[str], raw_output: str) -> str:
    """
    Convert Maven/Gradle/javac/pytest build output into compact symbolic state.
    Safe compression target: 90-98%.

    Input : ["[ERROR] PaymentService.java:[442,5] error: cannot find symbol", ...]
    Output: ERR[JAVA][PaymentService.java:442][cannot find symbol]
            ERR[JAVA][AuditCtx.java:91][incompatible types: int → String]
    """
    lines = _extract_error_lines(errors, raw_output)
    if not lines:
        # Fallback: grab first meaningful line from raw output
        first = next(
            (l.strip() for l in (raw_output or "").splitlines() if len(l.strip()) > 10),
            "unknown build error"
        )
        return f"ERR[BUILD][{first[:200]}]"

    parts = []
    for lang, fp, lineno, msg in lines:
        loc = f"{fp}:{lineno}" if fp and lineno else (fp or "?")
        parts.append(f"ERR[{lang}][{loc}][{msg}]")
    return "\n".join(parts)


def compress_test_results(result: dict) -> str:
    """
    Convert test runner output dict into compact test state.
    Safe compression target: 80-95%.

    Input : {passed:4, failed:2, output:"...", failed_tests:[...]}
    Output: TEST[pass=4 fail=2]
            FAIL:MerchantTest.testInvalidFlow:expected 401 got 500
            FAIL:RedisCacheTest.testExpiry:timeout(5s)
    """
    passed = result.get("passed", 0) or 0
    failed = result.get("failed", 0) or 0
    zero   = result.get("zero_tests", False)

    header = f"TEST[pass={passed} fail={failed}{'  zero_tests=true' if zero else ''}]"
    parts  = [header]

    # Structured failed_tests list (preferred)
    for ft in (result.get("failed_tests") or [])[:10]:
        if isinstance(ft, dict):
            name   = ft.get("name", "?")
            reason = ft.get("reason", ft.get("message", ""))[:120]
            parts.append(f"FAIL:{name}:{reason}" if reason else f"FAIL:{name}")
        elif isinstance(ft, str):
            parts.append(f"FAIL:{ft[:160]}")

    # Fallback: extract FAIL lines from raw output if no structured data
    if failed > 0 and len(parts) == 1:
        raw = (result.get("output", "") or "") + "\n" + (result.get("stderr", "") or "")
        fail_re = re.compile(r"(?:FAILED|FAIL|AssertionError|assert|Error)[:\s]+(.+)", re.IGNORECASE)
        seen_fails: set[str] = set()
        for m in fail_re.finditer(raw):
            snippet = m.group(1).strip()[:120]
            if snippet and snippet not in seen_fails:
                seen_fails.add(snippet)
                parts.append(f"FAIL:{snippet}")
            if len(parts) > 8:
                break

    return "\n".join(parts)


def compress_fix_history(attempts: list[dict]) -> str:
    """
    Convert retry history into compact symbolic state.
    Safe compression target: 95%.

    Input : [{attempt:1, path:"X.java", action:"import_fix", result:"build_fail"}, ...]
    Output: FIX_HIST[2 attempts]
            A1:X.java→import_fix→FAIL(build)
            A2:X.java→null_guard→FAIL(test:assert)
    """
    if not attempts:
        return ""
    parts = [f"FIX_HIST[{len(attempts)} attempt{'s' if len(attempts) != 1 else ''}]"]
    for a in attempts[-6:]:  # last 6 max
        n    = a.get("attempt", "?")
        path = (a.get("path", "") or "").split("/")[-1]  # basename only
        act  = (a.get("action", "?") or "?")[:60]
        res  = (a.get("result", "pending") or "pending")[:40]
        parts.append(f"A{n}:{path}→{act}→{res}")
    return "\n".join(parts)


def compress_tool_output(tool_name: str, raw_output: str, max_chars: int = 400) -> str:
    """
    Generic tool output compression: head+tail strategy for large outputs.
    For JSON: keep keys, truncate long string values.
    Safe compression target: 70-90%.
    """
    if not raw_output:
        return ""
    if len(raw_output) <= max_chars:
        return raw_output

    half = max_chars // 2
    head = raw_output[:half]
    tail = raw_output[-half:]
    omitted = len(raw_output) - max_chars
    return f"{head}\n[...{omitted:,} chars omitted — {tool_name} output compressed...]\n{tail}"


def _truncate_middle(text: str, max_chars: int) -> str:
    """
    Smart truncation: keep first 70% + last 30% of the allowed budget.
    Inserts a clear marker between the preserved segments.

    Rationale: system instructions live at the top; the specific task/error
    being fixed lives at the bottom. The middle is usually repeated logs,
    import lists, or verbose boilerplate.
    """
    if len(text) <= max_chars:
        return text
    head_chars = int(max_chars * 0.70)
    tail_chars = max_chars - head_chars
    omitted = len(text) - max_chars
    marker = f"\n[...{omitted:,} chars omitted — middle bulk removed to fit token budget...]\n"
    return text[:head_chars] + marker + text[-tail_chars:]


# ── RAG chunk helpers ─────────────────────────────────────────────────────────

_FENCED_CODE_RE = re.compile(r"^\s*(?:```|~~~)[\s\S]*?(?:```|~~~)\s*$")
_XML_DOC_RE = re.compile(r"^\s*<([A-Za-z_][\w:.-]*)(?:\s[^>]*)?>[\s\S]*</\1>\s*$")
_CODE_HINT_RE = re.compile(
    r"\b(?:public|private|protected|class|interface|enum|function|def|return|import|SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|TABLE)\b|[{};]|=>|->|::",
    re.IGNORECASE,
)


def _strip_source_header(chunk: str) -> str:
    m = re.match(r"\[Source:[^\]]+\]\n?(.*)", chunk, re.DOTALL)
    return (m.group(1) if m else chunk).strip()


def _is_atomic_rag_content(content: str) -> bool:
    s = content.strip()
    if not s:
        return False
    if _FENCED_CODE_RE.match(s) or _XML_DOC_RE.match(s):
        return True
    if s.startswith(("{", "[")):
        try:
            json.loads(s)
            return True
        except Exception:
            pass
    lines = [ln for ln in s.splitlines() if ln.strip()]
    if len(lines) >= 4:
        hits = sum(1 for ln in lines if _CODE_HINT_RE.search(ln.strip()) or ln.startswith(("    ", "\t")))
        if hits / len(lines) >= 0.50:
            return True
    return False


def _source_key(chunk: str) -> str:
    """Extract the [Source: path] key from a RAG chunk, or fingerprint first 80 chars."""
    m = re.match(r"\[Source:\s*([^\]]+)\]\n?(.*)", chunk, re.DOTALL)
    if m:
        source = m.group(1).strip()
        body = (m.group(2) or "").strip()
        if _is_atomic_rag_content(body):
            return f"{source}:{hashlib.sha256(body.encode()).hexdigest()[:12]}"
        return source
    return hashlib.sha256(chunk[:80].encode()).hexdigest()[:12]


def dedup_rag_chunks(chunks: list[str]) -> list[str]:
    """
    Remove duplicate RAG chunks by source file path.
    When multiple chunks share the same source, keep only the first occurrence
    (highest-scored, since reranker outputs are ordered best-first).
    """
    _input_total = sum(len(c) for c in chunks)

    seen: set[str] = set()
    result: list[str] = []
    for chunk in chunks:
        key = _source_key(chunk)
        if key not in seen:
            seen.add(key)
            result.append(chunk)

    _dropped = len(chunks) - len(result)
    _output_total = sum(len(c) for c in result)
    return result


def _is_table_dominant(content: str, threshold: float = 0.40) -> bool:
    """
    Return True when the content is predominantly a Markdown table.

    A chunk is table-dominant when ≥ threshold fraction of its non-blank
    lines contain a pipe character.  This covers:
      • Standard GFM tables  (| col | col |)
      • Separator rows        (|------|------|)
      • Docling artifacts     (1.2 | col | col |)

    Table chunks must NEVER be trimmed — a table with rows cut out is
    worse than no table at all (the LLM will hallucinate the missing rows).
    """
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return False
    pipe_lines = sum(1 for l in lines if '|' in l)
    return (pipe_lines / len(lines)) >= threshold


def trim_rag_chunk(chunk: str, max_chars: int = 800) -> str:
    """
    Trim a single RAG chunk from 1500 → 800 chars while preserving the
    [Source: path] header and keeping a head+tail view of the content.
    Tries to split at line boundaries.

    TABLE CHUNKS ARE NEVER TRIMMED — returning a partial table is worse
    than returning the full table because the LLM will hallucinate the
    missing rows.  If the chunk is table-dominant the full chunk is
    returned regardless of max_chars.
    """
    _input_chars = len(chunk)

    if _is_atomic_rag_content(_strip_source_header(chunk)):
        return chunk

    # ── Table-dominant guard ──────────────────────────────────────────────────
    # Separate header first so we test only the content portion for table-ness.
    # If the content is a table, return the full chunk — never truncate tables.
    _hdr_m = re.match(r"(\[Source:[^\]]+\]\n?)(.*)", chunk, re.DOTALL)
    _content_for_check = _hdr_m.group(2) if _hdr_m else chunk
    if _is_table_dominant(_content_for_check):
        logger.debug(
            f"trim_rag_chunk: table-dominant chunk ({_input_chars} chars) — "
            f"skipping trim to preserve all rows"
        )
        return chunk

    # Separate the source header from the content
    header = ""
    content = chunk
    m = re.match(r"(\[Source:[^\]]+\]\n?)(.*)", chunk, re.DOTALL)
    if m:
        header = m.group(1)
        content = m.group(2)
        available = max_chars - len(header) - 60  # reserve space for marker
    else:
        available = max_chars - 60

    if available <= 0:
        result = chunk[:max_chars]
        return result

    head_chars = int(available * 0.65)
    tail_chars = available - head_chars

    # Snap to nearest line boundary
    head_end = content.rfind("\n", 0, head_chars)
    if head_end < 0:
        head_end = head_chars
    tail_start = content.find("\n", len(content) - tail_chars)
    if tail_start < 0:
        tail_start = len(content) - tail_chars

    omitted = tail_start - head_end
    if omitted <= 0:
        result = chunk[:max_chars]
        return result

    marker = f"\n[...{omitted} chars omitted...]\n"
    result = header + content[:head_end] + marker + content[tail_start:]
    _output_chars = len(result)
    _saved = _input_chars - _output_chars
    return result


# ── IDE-specific compression ──────────────────────────────────────────────────

# Tags that Kilo Code / Continue inject around file contents in tool results
_IDE_FILE_TAGS_RE = re.compile(
    r"<(?:file_content|read_file|file)[^>]*>(.*?)</(?:file_content|read_file|file)>",
    re.DOTALL | re.IGNORECASE,
)

# Patterns for Kilo Code's <environment_details> / workspace listings
_ENV_DETAILS_RE = re.compile(r"<environment_details>.*?</environment_details>", re.DOTALL)
_REPO_MAP_RE    = re.compile(r"<repo_map>.*?</repo_map>", re.DOTALL)


def compress_ide_tool_result(content: str | Any, max_chars: int = 6000) -> str:
    """
    Compress a Kilo Code / Continue IDE tool result message.

    The dominant use-case is file-read results: the model reads a 1000-line file
    and the entire content goes into a `role=tool` message.  With 10-lakh-line
    codebases this can be 50K tokens per message.

    Strategy:
    - Strip environment_details / repo_map boilerplate entirely (we have our own index)
    - For file contents > max_chars: keep first 60% + last 40% with a line-count marker
    - Preserves the most important parts: imports/class header (top) and the specific
      function being edited (bottom)
    """
    if not isinstance(content, str):
        try:
            content = json.dumps(content) if not isinstance(content, str) else content
        except Exception:
            content = str(content)

    _input_chars = len(content)
    logger.debug(
        f"[IDE] [compress_ide_tool_result] ENTER  input_chars={_input_chars:,}  "
        f"max_chars={max_chars}  preview={content[:80]!r}"
    )

    # Strip environment / workspace boilerplate
    content = _ENV_DETAILS_RE.sub("", content)
    content = _REPO_MAP_RE.sub("[repo map omitted — use platform codebase index]", content)
    _after_strip_chars = len(content)
    if _after_strip_chars != _input_chars:
        logger.debug(
            f"[IDE[ [compress_ide_tool_result] STRIPPED boilerplate  "
            f"{_input_chars:,}→{_after_strip_chars:,}  "
            f"saved={_input_chars - _after_strip_chars:,}"
        )

    if len(content) <= max_chars:
        logger.debug(
            f"[IDE] [compress_ide_tool_result] SKIP   content within limit after strip "
            f"({_after_strip_chars:,} <= {max_chars}) — NO FURTHER COMPRESSION"
        )
        return content.strip()

    # Count lines to give the model useful context about what was omitted
    total_lines = content.count("\n")
    # Reserve ~200 chars for the marker so total stays within max_chars
    _marker_budget = 200
    available = max_chars - _marker_budget
    head_chars = int(available * 0.60)
    tail_chars = available - head_chars

    # Snap to line boundaries
    head_end   = content.rfind("\n", 0, head_chars)
    if head_end < 0:
        head_end = head_chars
    tail_start = content.find("\n", len(content) - tail_chars)
    if tail_start < 0:
        tail_start = len(content) - tail_chars

    head_lines = content[:head_end].count("\n")
    tail_lines = content[tail_start:].count("\n")
    omit_lines = total_lines - head_lines - tail_lines

    marker = (
        f"\n[...{omit_lines:,} lines omitted ({total_lines:,} total). "
        f"Use platform codebase search for specific functions.]\n"
    )
    result = (content[:head_end] + marker + content[tail_start:]).strip()
    _output_chars = len(result)
    _saved = _after_strip_chars - _output_chars
    logger.info(
        f"[compress_ide_tool_result] COMPRESSED  "
        f"input={_input_chars:,}  after_strip={_after_strip_chars:,}  "
        f"output={_output_chars:,}  saved={_saved:,} "
        f"({100*_saved/max(_after_strip_chars,1):.1f}%)  "
        f"total_lines={total_lines:,}  omitted_lines={omit_lines:,}  "
        f"preview_before={content[:80]!r}  preview_after={result[:80]!r}"
    )
    return result


def compress_ide_messages(messages: list[dict], keep_recent_rounds: int = 4) -> list[dict]:
    """
    Compress a Kilo Code / Continue messages array before forwarding to LLM.

    The problem: every file read by the IDE agent appends a full role=tool message
    with thousands of lines.  A 20-turn session on a large codebase can exceed
    100K tokens just from accumulated tool results.

    Strategy:
    1. Always keep system messages (tool definitions) untouched.
    2. Keep the last `keep_recent_rounds` complete tool-call rounds verbatim
       (assistant + tool messages), with tool content compressed to 6K chars.
    3. For older rounds: keep the assistant turn (shows what action was taken)
       but REPLACE the tool result with a compact summary.
    4. Always keep user and final assistant turns verbatim.

    A "round" = one assistant message (possibly with tool_calls) + its tool responses.
    """
    if not messages:
        return messages

    def _msg_chars(m: dict) -> int:
        c = m.get("content", "") or ""
        return len(c) if isinstance(c, str) else len(json.dumps(c))

    # ── Entry snapshot ────────────────────────────────────────────────────────
    _input_msg_count  = len(messages)
    _input_total_chars = sum(_msg_chars(m) for m in messages)
    _input_tool_msgs  = [m for m in messages if m.get("role") == "tool"]
    _input_tool_chars = sum(_msg_chars(m) for m in _input_tool_msgs)
    logger.info(
        f"[compress_ide_messages] ENTER  "
        f"messages={_input_msg_count}  total_chars={_input_total_chars:,}  "
        f"tool_messages={len(_input_tool_msgs)}  tool_chars={_input_tool_chars:,}  "
        f"keep_recent_rounds={keep_recent_rounds}"
    )

    # Partition messages into rounds
    # Round = [assistant_with_tool_calls, tool_result1, tool_result2, ...]
    system_msgs  = [m for m in messages if m.get("role") == "system"]
    non_system   = [m for m in messages if m.get("role") != "system"]

    # Find round boundaries: a round starts at each assistant message that has tool_calls
    rounds: list[list[dict]] = []
    current_round: list[dict] = []
    orphans_before: list[dict] = []

    in_round = False
    for m in non_system:
        role = m.get("role", "")
        if role == "assistant" and m.get("tool_calls"):
            if current_round:
                rounds.append(current_round)
            current_round = [m]
            in_round = True
        elif role == "tool" and in_round:
            current_round.append(m)
        else:
            if in_round and current_round:
                rounds.append(current_round)
                current_round = []
                in_round = False
            if not in_round:
                orphans_before.append(m)
            else:
                current_round.append(m)

    if current_round:
        rounds.append(current_round)

    # Rebuild: system + orphans + (compressed old rounds) + (recent rounds verbatim)
    old_rounds    = rounds[:-keep_recent_rounds] if len(rounds) > keep_recent_rounds else []
    recent_rounds = rounds[-keep_recent_rounds:] if rounds else []

    logger.debug(
        f"[compress_ide_messages] ROUNDS  "
        f"total_rounds={len(rounds)}  old_rounds={len(old_rounds)}  "
        f"recent_rounds={len(recent_rounds)}  orphans={len(orphans_before)}"
    )

    result: list[dict] = list(system_msgs)
    result.extend(orphans_before)

    # Compress old rounds: keep assistant turn, replace tool results with summary
    _old_tool_squashed_count = 0
    _old_tool_squashed_chars = 0
    for rnd in old_rounds:
        for m in rnd:
            if m.get("role") == "assistant":
                result.append(m)  # keep action record
            elif m.get("role") == "tool":
                raw = m.get("content", "") or ""
                raw_str = raw if isinstance(raw, str) else json.dumps(raw)
                total_chars = len(raw_str)
                _old_tool_squashed_count += 1
                _old_tool_squashed_chars += total_chars
                compressed = {
                    "role": "tool",
                    "content": f"[tool result compressed — {total_chars:,} chars → summary only]",
                }
                if m.get("tool_call_id"):
                    compressed["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    compressed["name"] = m["name"]
                logger.debug(
                    f"[compress_ide_messages] OLD-ROUND SQUASH  "
                    f"tool_call_id={m.get('tool_call_id', 'n/a')!r}  "
                    f"name={m.get('name', 'n/a')!r}  "
                    f"original_chars={total_chars:,}  "
                    f"preview={raw_str[:80]!r}"
                )
                result.append(compressed)

    if _old_tool_squashed_count:
        logger.info(
            f"[compress_ide_messages] OLD-ROUND SQUASH SUMMARY  "
            f"squashed={_old_tool_squashed_count} tool message(s)  "
            f"chars_removed={_old_tool_squashed_chars:,}  "
            f"(replaced with stub summaries)"
        )

    # Recent rounds: keep verbatim but compress large file contents
    _recent_tool_trimmed_count = 0
    _recent_tool_trimmed_saved = 0
    for rnd in recent_rounds:
        for m in rnd:
            if m.get("role") == "tool":
                raw = m.get("content", "") or ""
                raw_str = raw if isinstance(raw, str) else json.dumps(raw)
                if len(raw_str) > 6000:
                    compressed_content = compress_ide_tool_result(raw_str, max_chars=6000)
                    _saved = len(raw_str) - len(compressed_content)
                    _recent_tool_trimmed_count += 1
                    _recent_tool_trimmed_saved += _saved
                    logger.debug(
                        f"[compress_ide_messages] RECENT-ROUND TRIM  "
                        f"tool_call_id={m.get('tool_call_id', 'n/a')!r}  "
                        f"name={m.get('name', 'n/a')!r}  "
                        f"before={len(raw_str):,}  after={len(compressed_content):,}  "
                        f"saved={_saved:,}  "
                        f"preview_before={raw_str[:80]!r}  "
                        f"preview_after={compressed_content[:80]!r}"
                    )
                    nm = dict(m)
                    nm["content"] = compressed_content
                    result.append(nm)
                else:
                    logger.debug(
                        f"[compress_ide_messages] RECENT-ROUND SKIP  "
                        f"tool_call_id={m.get('tool_call_id', 'n/a')!r}  "
                        f"chars={len(raw_str):,} <= 6000 — NO CHANGE"
                    )
                    result.append(m)
            else:
                result.append(m)

    if _recent_tool_trimmed_count:
        logger.info(
            f"[compress_ide_messages] RECENT-ROUND TRIM SUMMARY  "
            f"trimmed={_recent_tool_trimmed_count} tool message(s)  "
            f"chars_saved={_recent_tool_trimmed_saved:,}"
        )

    # Final token budget guard: if total is still massive, compress all tool results harder
    total_size = sum(
        len(m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content", "")))
        for m in result
    )
    _HARD_LIMIT = 80_000  # chars ≈ 20K tokens — absolute ceiling before we get cut off
    if total_size > _HARD_LIMIT:
        logger.warning(
            f"[compress_ide_messages] EMERGENCY PASS  "
            f"total={total_size:,} chars exceeds hard limit {_HARD_LIMIT:,} — "
            f"applying emergency tool-result compression (max_chars=2000)"
        )
        _emergency_count = 0
        _emergency_saved = 0
        for i, m in enumerate(result):
            if m.get("role") == "tool":
                raw = m.get("content", "") or ""
                raw_str = raw if isinstance(raw, str) else ""
                if len(raw_str) > 2000:
                    compressed_emergency = compress_ide_tool_result(raw_str, max_chars=2000)
                    _saved = len(raw_str) - len(compressed_emergency)
                    _emergency_count += 1
                    _emergency_saved += _saved
                    logger.debug(
                        f"[compress_ide_messages] EMERGENCY TRIM  "
                        f"msg_index={i}  "
                        f"tool_call_id={m.get('tool_call_id', 'n/a')!r}  "
                        f"before={len(raw_str):,}  after={len(compressed_emergency):,}  "
                        f"saved={_saved:,}  "
                        f"preview_before={raw_str[:80]!r}  "
                        f"preview_after={compressed_emergency[:80]!r}"
                    )
                    nm = dict(m)
                    nm["content"] = compressed_emergency
                    result[i] = nm
        logger.warning(
            f"[compress_ide_messages] EMERGENCY PASS DONE  "
            f"emergency_trimmed={_emergency_count} message(s)  "
            f"chars_saved={_emergency_saved:,}"
        )
    else:
        logger.debug(
            f"[compress_ide_messages] EMERGENCY PASS SKIPPED  "
            f"total={total_size:,} <= hard_limit={_HARD_LIMIT:,}"
        )

    # ── Exit summary ──────────────────────────────────────────────────────────
    _output_total_chars = sum(_msg_chars(m) for m in result)
    _total_saved = _input_total_chars - _output_total_chars
    _reduction_pct = 100 * _total_saved / max(_input_total_chars, 1)
    logger.info(
        f"[compress_ide_messages] EXIT  "
        f"messages: {_input_msg_count}→{len(result)}  "
        f"chars: {_input_total_chars:,}→{_output_total_chars:,}  "
        f"saved={_total_saved:,} ({_reduction_pct:.1f}%)  "
        f"{'COMPRESSED ✓' if _total_saved > 0 else 'NO CHANGE — all messages within limits'}"
    )

    return result
