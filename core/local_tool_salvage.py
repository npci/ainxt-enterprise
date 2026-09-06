# SPDX-License-Identifier: MIT
"""
Salvage tool calls that a local model emitted as RAW TEXT.

Why this exists
---------------
A correctly configured OpenAI-compatible runtime returns tool calls as
structured `delta.tool_calls`. That requires a model-specific tool-call parser
to be enabled on the serving side — on vLLM, for example:

    --enable-auto-tool-choice --tool-call-parser kimi_k2

When that flag is missing (or the parser does not match the model), the runtime
streams the model's tool-call syntax through as ordinary assistant TEXT. The
gateway then forwards well-formed prose to the CLI, the CLI sees `end_turn` with
no `tool_use` blocks, and the agent loop stops. The user gets a confident
"I've created the file and updated the config." for work that never happened.

That failure mode is indistinguishable from success at every layer above the
runtime, which is why it survives repeated debugging: nothing errors, tokens are
billed, and the transcript looks plausible.

`extract_raw_tool_calls` recognises the dialects our in-house models actually
emit and converts them back into structured calls. It is a COMPATIBILITY SHIM,
not a substitute for correct runtime configuration: every salvage is logged as a
warning naming the flag to set.

Design constraints
------------------
* Never raise. A parser bug must not take down a turn; on doubt, return nothing
  and let the caller treat the turn as tool-less.
* Never invent calls. A name is only accepted if the caller advertised that tool
  (`known_tools`), which keeps prose that merely *mentions* a tool from being
  executed.
* Report what was consumed, so the caller can strip the tool syntax out of the
  visible text instead of showing raw control tokens to the user.
"""

from __future__ import annotations

import json
import re
from typing import Iterable, NamedTuple, Optional


class SalvagedCall(NamedTuple):
    """A tool call recovered from assistant text."""

    name: str
    arguments: str      # JSON object string, always parseable
    span: tuple[int, int]   # (start, end) offsets consumed from the source text


# ─────────────────────────────────────────────────────────────────────────────
# Dialects
#
# Kimi K2 native:
#   <|tool_calls_section_begin|>
#     <|tool_call_begin|>functions.read_file:0<|tool_call_argument_begin|>
#     {"target_file":"a"}<|tool_call_end|>
#   <|tool_calls_section_end|>
#
# Hermes / Qwen / many finetunes:
#   <tool_call>{"name": "read_file", "arguments": {...}}</tool_call>
#
# GLM / ChatGLM:
#   <|assistant|>read_file\n{"target_file": "a"}
#   or  read_file\n```json\n{...}\n```
#
# DeepSeek:
#   <｜tool▁call▁begin｜>function<｜tool▁sep｜>read_file\n```json\n{...}\n```
#
# Generic fenced JSON (last resort, requires an exact tool-name match):
#   ```json\n{"name": "read_file", "arguments": {...}}\n```
# ─────────────────────────────────────────────────────────────────────────────

# Kimi K2. The id carries the name as "functions.<name>:<index>"; some builds
# emit a bare name instead, so accept both.
_KIMI_CALL = re.compile(
    r"<\|tool_call_begin\|>\s*(?P<id>[^<]*?)\s*"
    r"<\|tool_call_argument_begin\|>\s*(?P<args>.*?)\s*<\|tool_call_end\|>",
    re.DOTALL,
)
_KIMI_SECTION = re.compile(
    r"<\|tool_calls_section_begin\|>.*?(?:<\|tool_calls_section_end\|>|\Z)",
    re.DOTALL,
)
_KIMI_NAME_FROM_ID = re.compile(r"^(?:functions?\.)?(?P<name>[A-Za-z0-9_.-]+?)(?::\d+)?$")

# Hermes / Qwen style.
_HERMES_CALL = re.compile(
    r"<tool_call>\s*(?P<body>\{.*?\})\s*(?:</tool_call>|\Z)",
    re.DOTALL,
)

# DeepSeek style — the separators use full-width bars.
_DEEPSEEK_CALL = re.compile(
    r"<[｜|]tool[▁_]call[▁_]begin[｜|]>\s*(?:function)?\s*"
    r"<[｜|]tool[▁_]sep[｜|]>\s*(?P<name>[A-Za-z0-9_.-]+)\s*"
    r"(?P<args>\{.*?\}|```(?:json)?\s*\{.*?\}\s*```)",
    re.DOTALL,
)

# A fenced JSON object that self-describes a call. Bare (unfenced) objects are
# located by brace matching in `_iter_bare_json_objects` instead of a regex,
# because a regex cannot balance the nested braces of an `arguments` payload.
_JSON_CALL_CANDIDATE = re.compile(
    r"```(?:json|tool_code)?\s*(?P<body>\{.*?\})\s*```",
    re.DOTALL,
)

# Cheap pre-filter: only brace-scan when a call-shaped key is present.
_JSON_CALL_HINT = re.compile(r"\"(?:name|tool|function)\"\s*:")


def _iter_bare_json_objects(text: str):
    """
    Yield `(start, end, obj)` for top-level `{...}` JSON objects in `text`.

    Brace-counting (string- and escape-aware) rather than a regex, so nested
    objects such as `{"function": {"name": ..., "arguments": {...}}}` are
    captured whole instead of being truncated at the first inner `}`.
    """
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    chunk = text[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        obj = None
                    if isinstance(obj, dict):
                        yield start, i + 1, obj
                    start = -1

# GLM: a lone tool name on its own line followed by a JSON object / fence.
_GLM_CALL = re.compile(
    r"(?:<\|assistant\|>\s*)?^[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*\r?\n+"
    r"[ \t]*(?:```(?:json)?\s*)?(?P<args>\{.*?\})\s*(?:```)?",
    re.DOTALL | re.MULTILINE,
)

# Any residual control token, stripped from user-visible text.
_CONTROL_TOKENS = re.compile(
    r"<\|tool_calls_section_(?:begin|end)\|>"
    r"|<\|tool_call_(?:begin|end|argument_begin)\|>"
    r"|</?tool_call>"
    r"|<[｜|]tool[▁_](?:call[▁_](?:begin|end)|sep|outputs?[▁_](?:begin|end))[｜|]>"
    r"|<\|(?:assistant|user|system|observation)\|>"
)


def _coerce_args(raw: object) -> Optional[str]:
    """Normalise an arguments payload to a JSON *object* string, or None."""
    if raw is None:
        return "{}"
    if isinstance(raw, dict):
        try:
            return json.dumps(raw)
        except (TypeError, ValueError):
            return None
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return "{}"
        # Strip a code fence if the model wrapped the args in one.
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*", "", s)
            s = re.sub(r"\s*```$", "", s).strip()
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict):
            return json.dumps(parsed)
        return None
    return None


def _name_ok(name: str, known: Optional[frozenset[str]]) -> bool:
    """A salvaged name must be a tool the caller actually advertised."""
    if not name:
        return False
    if known is None:
        return True
    return name in known


def extract_raw_tool_calls(
        text: str,
        known_tools: Optional[Iterable[str]] = None,
) -> tuple[list[SalvagedCall], str]:
    """
    Recover tool calls embedded in assistant `text`.

    Returns `(calls, cleaned_text)` where `cleaned_text` has the consumed tool
    syntax and any stray control tokens removed, so it is safe to show a user.
    When nothing is salvaged, `calls` is empty and the text is returned with only
    control-token cleanup applied.

    `known_tools` restricts which names may be salvaged. Pass the tool names sent
    upstream; `None` disables the check (useful only for tests).
    """
    if not text:
        return [], text

    known = frozenset(known_tools) if known_tools is not None else None
    calls: list[SalvagedCall] = []
    consumed: list[tuple[int, int]] = []

    try:
        # ── 1. Kimi K2 native ────────────────────────────────────────────────
        for m in _KIMI_CALL.finditer(text):
            nm = _KIMI_NAME_FROM_ID.match((m.group("id") or "").strip())
            if not nm:
                continue
            name = nm.group("name")
            args = _coerce_args(m.group("args"))
            if args is None or not _name_ok(name, known):
                continue
            calls.append(SalvagedCall(name, args, m.span()))
        if calls:
            # Consume the whole section wrapper so its tokens never reach the UI.
            spans = [s.span for s in calls]
            for sm in _KIMI_SECTION.finditer(text):
                spans.append(sm.span())
            return calls, _strip_spans(text, spans)

        # ── 2. Hermes / Qwen ─────────────────────────────────────────────────
        for m in _HERMES_CALL.finditer(text):
            try:
                body = json.loads(m.group("body"))
            except json.JSONDecodeError:
                continue
            if not isinstance(body, dict):
                continue
            name = str(body.get("name") or body.get("tool") or body.get("function") or "")
            args = _coerce_args(body.get("arguments", body.get("parameters")))
            if args is None or not _name_ok(name, known):
                continue
            calls.append(SalvagedCall(name, args, m.span()))
        if calls:
            return calls, _strip_spans(text, [c.span for c in calls])

        # ── 3. DeepSeek ──────────────────────────────────────────────────────
        for m in _DEEPSEEK_CALL.finditer(text):
            name = (m.group("name") or "").strip()
            args = _coerce_args(m.group("args"))
            if args is None or not _name_ok(name, known):
                continue
            calls.append(SalvagedCall(name, args, m.span()))
        if calls:
            return calls, _strip_spans(text, [c.span for c in calls])

        # ── 4. Self-describing JSON (fenced or bare) ─────────────────────────
        # Candidates from both sources, each as (start, end, parsed_object).
        json_candidates: list[tuple[int, int, dict]] = []
        for m in _JSON_CALL_CANDIDATE.finditer(text):
            try:
                obj = json.loads(m.group("body"))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                json_candidates.append((m.start(), m.end(), obj))
        if _JSON_CALL_HINT.search(text):
            covered = [(s, e) for s, e, _ in json_candidates]
            for s, e, obj in _iter_bare_json_objects(text):
                if any(s >= cs and e <= ce for cs, ce in covered):
                    continue  # already captured via its fence
                json_candidates.append((s, e, obj))

        for start, end, body in sorted(json_candidates):
            fn_obj = body.get("function") if isinstance(body.get("function"), dict) else None
            name = str(
                body.get("name")
                or body.get("tool")
                or (fn_obj or {}).get("name")
                or (body.get("function") if isinstance(body.get("function"), str) else "")
                or ""
            )
            raw_args = body.get("arguments", body.get("parameters", body.get("input")))
            if raw_args is None and fn_obj is not None:
                raw_args = fn_obj.get("arguments", fn_obj.get("parameters"))
            args = _coerce_args(raw_args)
            # Require an exact known-tool match here: this pattern is broad and
            # would otherwise capture ordinary JSON the model was discussing.
            if args is None or not name or known is None or name not in known:
                continue
            calls.append(SalvagedCall(name, args, (start, end)))
        if calls:
            return calls, _strip_spans(text, [c.span for c in calls])

        # ── 5. GLM: bare "<name>\n{json}" ────────────────────────────────────
        # Narrowest and most ambiguous — only with an exact known-tool match.
        if known:
            for m in _GLM_CALL.finditer(text):
                name = (m.group("name") or "").strip()
                if name not in known:
                    continue
                args = _coerce_args(m.group("args"))
                if args is None:
                    continue
                calls.append(SalvagedCall(name, args, m.span()))
            if calls:
                return calls, _strip_spans(text, [c.span for c in calls])
    except Exception:
        # A salvage bug must never break a turn.
        return [], _CONTROL_TOKENS.sub("", text)

    return [], _CONTROL_TOKENS.sub("", text)


# ─────────────────────────────────────────────────────────────────────────────
# Streaming support
# ─────────────────────────────────────────────────────────────────────────────

# Literal openers that begin a raw tool-call payload. Once one of these appears,
# nothing after it may be shown to the user until the turn ends and we know
# whether it parsed as a call.
_OPENERS: tuple[str, ...] = (
    "<|tool_calls_section_begin|>",
    "<|tool_call_begin|>",
    "<tool_call>",
    "<｜tool▁call▁begin｜>",
    "<|tool▁call▁begin|>",
    "<｜tool_call_begin｜>",
)


def safe_emit_boundary(text: str) -> int:
    """
    Longest prefix of `text` that is safe to stream to the user.

    Text before any tool-call opener is ordinary prose and streams immediately,
    which keeps local models responsive. From the first opener onward — including
    a PARTIAL opener still arriving at the tail, e.g. a chunk ending in
    ``"<|tool_call"`` — output is held back so raw control tokens never reach the
    UI and the payload stays intact for salvage.
    """
    if not text:
        return 0
    cut = len(text)
    for op in _OPENERS:
        i = text.find(op)
        if i != -1:
            cut = min(cut, i)
    # A partial opener split across chunk boundaries: hold back the tail.
    for op in _OPENERS:
        for n in range(min(len(op) - 1, len(text)), 0, -1):
            if text.endswith(op[:n]):
                cut = min(cut, len(text) - n)
                break
    return max(cut, 0)


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Remove `spans` from `text`, then clean up control tokens and whitespace."""
    if not spans:
        return _CONTROL_TOKENS.sub("", text)
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out, prev = [], 0
    for start, end in merged:
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    cleaned = _CONTROL_TOKENS.sub("", "".join(out))
    # Collapse the blank runs left behind by excision.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


# ─────────────────────────────────────────────────────────────────────────────
# False-completion detection
# ─────────────────────────────────────────────────────────────────────────────

# Past-tense claims of file/command work. Deliberately narrow: it must be a
# claim of COMPLETED action, not a plan ("I will create…") or a question.
_CLAIMS_WORK = re.compile(
    r"\b("
    r"i(?:'ve| have)\s+(?:now\s+)?(?:created|added|updated|modified|edited|written|wrote|"
    r"fixed|implemented|removed|deleted|renamed|moved|refactored|installed|configured|"
    r"applied|committed|patched|generated)"
    r"|i\s+(?:created|added|updated|modified|edited|wrote|fixed|implemented|removed|"
    r"deleted|renamed|moved|refactored|installed|configured|applied|committed|patched|generated)"
    r"|(?:has|have)\s+been\s+(?:created|added|updated|modified|written|fixed|implemented|"
    r"removed|deleted|renamed|moved|applied|committed)"
    r"|successfully\s+(?:created|added|updated|modified|written|fixed|implemented|removed|"
    r"deleted|renamed|moved|applied|committed|ran|executed)"
    r"|(?:the\s+)?(?:file|files|changes|code|config|configuration)\s+(?:is|are|has been|have been)\s+"
    r"(?:now\s+)?(?:created|added|updated|modified|written|fixed|in place|ready)"
    r")\b",
    re.IGNORECASE,
)


def claims_completed_work(text: str) -> bool:
    """
    True when assistant text asserts it performed file or command work.

    Used only when tools WERE offered and NO tool call came back: in that state
    such a claim is necessarily false, because the model has no other way to act.
    Surfacing an error there is far better than letting the CLI record a
    confident lie as a finished turn.
    """
    if not text:
        return False
    return bool(_CLAIMS_WORK.search(text))
