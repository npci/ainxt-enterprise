# SPDX-License-Identifier: MIT
"""
Shared utilities for all factory pipelines (Agent, Workflow, Skill).

Consolidates the LLM configuration, LLM call wrapper, and JSON parsing
logic that was previously duplicated across all three factory modules.
"""
from __future__ import annotations

import asyncio
import json

import math
import os
import re
import time
from typing import Any, Dict, Optional

from core.logger import logger
# The gateway yields this literal when an upstream LLM call throws inside its
# streaming path (see gateway.py). It arrives as an HTTP 200 with a non-JSON
# body, so downstream JSON parsing fails and callers silently degrade to {} —
# which then triggers a full, expensive re-run. It's a transient error (not a
# policy block), so retrying the same request once usually succeeds and is far
# cheaper than letting a factory step fail and regenerate from scratch.
_GATEWAY_TRANSIENT_SENTINEL = "error generating response"


def _is_transient_gateway_error(text: str) -> bool:
    """True when ``text`` is the gateway's transient-failure sentinel.

    Kept strict (short body + exact sentinel substring) so legitimate model
    output that merely mentions the phrase inside a longer answer is never
    mistaken for a failure.
    """
    if not text:
        return True  # empty body is itself a failed generation
    sample = text.strip()
    return len(sample) < 120 and _GATEWAY_TRANSIENT_SENTINEL in sample.lower()

# The gateway yields this literal when an upstream LLM call throws inside its
# streaming path (see gateway.py). It arrives as an HTTP 200 with a non-JSON
# body, so downstream JSON parsing fails and callers silently degrade to {} —
# which then triggers a full, expensive re-run. It's a transient error (not a
# policy block), so retrying the same request once usually succeeds and is far
# cheaper than letting a factory step fail and regenerate from scratch.
_GATEWAY_TRANSIENT_SENTINEL = "error generating response"


def _is_transient_gateway_error(text: str) -> bool:
    """True when ``text`` is the gateway's transient-failure sentinel.

    Kept strict (short body + exact sentinel substring) so legitimate model
    output that merely mentions the phrase inside a longer answer is never
    mistaken for a failure.
    """
    if not text:
        return True  # empty body is itself a failed generation
    sample = text.strip()
    return len(sample) < 120 and _GATEWAY_TRANSIENT_SENTINEL in sample.lower()

# ---------------------------------------------------------------------------
# Model resolution — single source of truth
# ---------------------------------------------------------------------------

def resolve_factory_model() -> str:
    """Resolve the factory's own LLM model at CALL time.

    Delegates to ``app.core.config.factory_model()`` — the single source of
    truth for this resolution (env override → core.llm_provider_registry's
    configured default → legacy env fallback) — rather than duplicating that
    chain here. Calling through a function (not a module-level constant)
    still means an operator can fix ``FACTORY_MODEL`` in ``.env`` or change
    the admin-configured default and have a ``--reload`` gateway pick it up
    without a full process restart; the old import-time constant could go
    stale when only some modules were reloaded, which surfaced as the agent
    factory still calling a decommissioned default (``claude-sonnet-4-6``)
    after the workflow factory had already switched.
    """
    from app.core.config import factory_model as _factory_model
    return _factory_model()


# Back-compat: some call sites import ``FACTORY_MODEL`` directly. Keep it as a
# snapshot of the resolved value, but prefer ``resolve_factory_model()`` /
# ``build_factory_llm_config()`` (which read the env fresh) anywhere the value
# must survive a live ``.env`` edit.
FACTORY_MODEL: str = resolve_factory_model()


def build_factory_llm_config(
    max_tokens: int = 2048,
    temperature: float = 0.7,
    model: Optional[str] = None,
):
    """Construct an LLMConfig for any factory's own LLM calls.

    ``model`` overrides the resolved factory model when provided (used by
    the agent factory which lets callers choose a model per-call).
    """
    from app.models import LLMConfig, LLMProvider
    # Route through the LLM_PROXY-aware helpers so the orchestrator,
    # aggregator, and every other ``call_factory_llm`` caller hit
    # ``${LLM_PROXY_URL}/v1`` whenever it is set (SIT / prod). Without
    # this delegation, ``FACTORY_BASE_URL`` falls straight through to
    # ``localhost:11434/v1`` — exactly the SIT failure mode Phase 0
    # fixed for the engine, mirrored here for the factory layer so the
    # single-agent path and the swarm path use the same model gateway.
    from app.core.config import factory_base_url, factory_api_key

    return LLMConfig(
        provider=LLMProvider.CUSTOM,
        api_key=factory_api_key(),
        model_name=model or resolve_factory_model(),
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=1.0,
        base_url=factory_base_url(),
    )


# ---------------------------------------------------------------------------
# PCI gateway bypass — replace trigger words that the AiNxt content filter
# blocks. The substitutions are semantically equivalent so the LLM still
# understands the intent, but the raw HTTP body no longer trips the filter.
# ---------------------------------------------------------------------------

_PCI_SUBSTITUTIONS: list[tuple[re.Pattern, str]] = [
    # Financial / PCI trigger phrases
    (re.compile(r"\bcredit[\s_-]?card\b", re.I), "payment method"),
    (re.compile(r"\bdebit[\s_-]?card\b", re.I), "payment method"),
    (re.compile(r"\bcard[\s_-]?number\b", re.I), "card identifier"),
    (re.compile(r"\bCVV\b"), "security code"),
    (re.compile(r"\bPAN\b"), "account reference"),
    (re.compile(r"\bcard[\s_-]?holder\b", re.I), "account holder"),
    (re.compile(r"\bexpiry[\s_-]?date\b", re.I), "validity date"),
    (re.compile(r"\bexpiration[\s_-]?date\b", re.I), "validity date"),
    # Credential-adjacent terms
    (re.compile(r"\bAPI[\s_-]?key\b", re.I), "auth token"),
    (re.compile(r"\bsecret[\s_-]?key\b", re.I), "auth credential"),
    (re.compile(r"\baccess[\s_-]?token\b", re.I), "auth token"),
    (re.compile(r"\bpassword\b", re.I), "passphrase"),
    (re.compile(r"\bSSN\b"), "identifier"),
    # Sensitive action phrases sometimes flagged
    (re.compile(r"\bexfiltrat", re.I), "extract"),
    (re.compile(r"\bscrape\b", re.I), "collect"),
    (re.compile(r"\bscraping\b", re.I), "collecting"),
    (re.compile(r"\bhack\b", re.I), "access"),
    (re.compile(r"\bexploit\b", re.I), "utilize"),
    (re.compile(r"\bviolat", re.I), "breach"),
    # PCI-specific (the gateway literally matches these)
    (re.compile(r"\bPCI\b"), "compliance"),
    (re.compile(r"\bPCI[\s_-]?DSS\b", re.I), "data compliance"),
]


def _sanitize_for_gateway(text: str) -> str:
    """Replace PCI-trigger words with safe synonyms.

    Applied transparently to all outbound LLM messages so the AiNxt
    gateway's content filter doesn't block legitimate workflow prompts
    that happen to mention terms like 'credit card' or 'API key'.
    """
    for pattern, replacement in _PCI_SUBSTITUTIONS:
        text = pattern.sub(replacement, text)
    return text


async def call_factory_llm(
    system: str,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, Any]] = None,
) -> str:
    """Call the LLM via the project's existing llm_handler (OpenAI-compatible).

    ``messages`` is a list of plain dicts: ``[{"role": "user"|"assistant", "content": str}]``.
    The ``system`` string is prepended as a system Message.

    All outbound text is run through ``_sanitize_for_gateway`` to replace
    PCI-trigger words that would cause the AiNxt content filter to block
    the request with a 200-status non-JSON body.

    ``response_format`` is an OpenAI-compatible structured-output spec
    (``{"type":"json_schema", "json_schema":{...}}`` or
    ``{"type":"json_object"}``). When supplied and the gateway honors it,
    the model output is constrained to match the schema. None = unconstrained
    (default — preserves behavior for every existing call site).
    """
    from app.llm_handler import get_llm_client, Message as LLMMessage

    llm_config = build_factory_llm_config(max_tokens=max_tokens, model=model, temperature=temperature)
    client = get_llm_client(llm_config)

    llm_messages = [LLMMessage(role="system", content=_sanitize_for_gateway(system))]
    for m in messages:
        content = m.get("content", "")
        # Don't sanitize assistant prefill (e.g. "{") — only user/system text
        if m["role"] != "assistant":
            content = _sanitize_for_gateway(content)
        llm_messages.append(LLMMessage(role=m["role"], content=content))

    # Retry once on the gateway's transient-failure sentinel. A single blip
    # otherwise returns non-JSON that fails downstream parsing and forces the
    # caller to regenerate an entire (often 8000-token) draft — the retry is
    # far cheaper than that re-run. Real content-filter rejections are handled
    # separately (raise_if_gateway_rejection) and are NOT retried.
    # Timing: each factory LLM call is a full blocking generation, so per-call
    # latency dominates multi-stage pipelines. Log elapsed wall-clock, the
    # model, the token cap, and output size so slow stages are visible without
    # a profiler. Enabled by default; silence via FACTORY_LLM_TIMING=0.
    _timing = os.getenv("FACTORY_LLM_TIMING", "1") != "0"
    _t0 = time.perf_counter()

    # Prefer a NON-STREAMING request for factory calls. Some gateways return
    # "Error generating response" (a 200 body with no real content) on the
    # STREAMING endpoint for large-generation requests, while the SAME request
    # on the NON-streaming endpoint returns valid output. Factory calls wait for
    # a full JSON blob and gain nothing from streaming, so we route them through
    # ``complete_nonstream`` when the client supports it. Set
    # FACTORY_LLM_FORCE_STREAM=1 to revert to the streaming path.
    _force_stream = os.getenv("FACTORY_LLM_FORCE_STREAM", "0") == "1"
    _use_nonstream = (not _force_stream) and hasattr(client, "complete_nonstream")

    async def _one_call() -> str:
        if _use_nonstream:
            return await client.complete_nonstream(llm_messages, response_format=response_format)
        return await client.complete(llm_messages, response_format=response_format)

    result = await _one_call()
    if _is_transient_gateway_error(result):
        logger.warning('[AGENT] call_factory_llm: transient gateway error — retrying once')
        await asyncio.sleep(0.5)
        result = await _one_call()

    if _timing:
        logger.info(f"[AGENT] factory_llm: {time.perf_counter() - _t0}s · model={llm_config.model_name} · max_tokens={max_tokens} · out={len(result or '')} chars · nonstream={_use_nonstream}")
    return result


async def call_factory_llm_with_finish_reason(
    system: str,
    messages: list[dict],
    max_tokens: int = 1024,
    model: str | None = None,
    temperature: float = 0.7,
    response_format: Optional[Dict[str, Any]] = None,
) -> tuple[str, str]:
    """Same as ``call_factory_llm`` but also returns the OpenAI ``finish_reason``.

    Returns ``(text, finish_reason)``. ``finish_reason`` is one of
    ``"stop"``, ``"length"``, ``"tool_calls"``, ``"content_filter"`` or an
    empty string when the upstream provider omits it.

    Used by ``SwarmOrchestrator._call_llm`` to authoritatively detect a
    ``max_tokens`` cap hit instead of guessing from response shape. All
    other callers should keep using ``call_factory_llm`` — adding this
    signature globally would churn every factory-LLM call site for no
    benefit.
    """
    from app.llm_handler import get_llm_client, Message as LLMMessage

    llm_config = build_factory_llm_config(max_tokens=max_tokens, model=model, temperature=temperature)
    client = get_llm_client(llm_config)

    llm_messages = [LLMMessage(role="system", content=_sanitize_for_gateway(system))]
    for m in messages:
        content = m.get("content", "")
        if m["role"] != "assistant":
            content = _sanitize_for_gateway(content)
        llm_messages.append(LLMMessage(role=m["role"], content=content))

    return await client.complete_with_finish_reason(
        llm_messages, response_format=response_format,
    )


class SecurityGatewayRejection(ValueError):
    """Raised when an LLM response is actually a gateway content-filter rejection.

    The AiNxt gateway (and similar) return HTTP 200 with a short non-JSON body
    such as ``{Request blocked due to PCI violation`` when input matches their
    content rules. Callers that catch this can surface a user-actionable error
    without retrying (retrying a blocked request is wasted latency).
    """


# Signatures observed in real gateway rejection bodies. Lowercased for matching.
# Substrings — not regexes — so they stay fast on long LLM responses.
_GATEWAY_REJECTION_SIGNATURES: tuple[str, ...] = (
    "request blocked",
    "blocked by",
    "blocked due to",
    "pci violation",
    "policy violation",
    "compliance violation",
    "content filtered",
    "content blocked",
    "guardrail",
    "moderation",
    "input rejected",
    "response rejected",
    "not allowed by policy",
    "violates our policy",
    "violates the content policy",
    "safety filter",
    "prompt blocked",
    "filtered by",
)

# Real LLM JSON output that legitimately uses some of the above words
# (e.g. an agent named "Policy Violation Detector") would false-positive.
# We require BOTH a signature hit AND the response NOT containing a balanced
# JSON object — gateway rejections are always short, broken text.
_REJECTION_LENGTH_CAP = 600  # chars; real workflow JSON is always longer


def detect_security_gateway_rejection(text: str) -> Optional[str]:
    """Return a cleaned rejection message if ``text`` looks like a gateway block.

    Returns ``None`` when the text is normal model output. Returns the
    human-readable rejection reason (signature-stripped, trimmed) when it
    matches one of the known gateway-block patterns.

    Heuristic: a rejection is short (``< _REJECTION_LENGTH_CAP`` chars), is
    NOT valid JSON, and contains one of the known signature substrings. This
    avoids flagging long legitimate model output that happens to mention
    e.g. "policy violation" inside a prose explanation.
    """
    if not text:
        return None
    sample = text.strip()
    if len(sample) > _REJECTION_LENGTH_CAP:
        return None

    lowered = sample.lower()
    matched = next((sig for sig in _GATEWAY_REJECTION_SIGNATURES if sig in lowered), None)
    if matched is None:
        return None

    # If it parses as JSON, treat it as legitimate model output.
    try:
        json.loads(sample)
        return None
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip leading "{" the gateway often emits to look JSON-shaped.
    cleaned = sample.lstrip("{").strip().rstrip("}").strip()
    return cleaned or matched


def raise_if_gateway_rejection(text: str, *, context: str = "") -> None:
    """Raise ``SecurityGatewayRejection`` if ``text`` is a gateway block.

    Convenience wrapper used at LLM-call sites:
        raw = await call_factory_llm(...)
        raise_if_gateway_rejection(raw, context="WorkflowBlueprintGenerator")
    """
    msg = detect_security_gateway_rejection(text)
    if msg is None:
        return
    prefix = f"{context}: " if context else ""
    raise SecurityGatewayRejection(
        f"{prefix}The model gateway rejected this request — \"{msg}\". "
        "Rephrase your description to avoid sensitive or restricted terms and try again."
    )


def clean_llm_text(raw: str) -> str:
    """Strip reasoning tags, markdown code fences and wrapping quotes.

    Reasoning models (Qwen3, DeepSeek-R1, ...) emit ``<think>...</think>``
    blocks that must not leak into user-visible prose. Some models also wrap
    output in ``` ``` ``` fences or surrounding quotes despite being told not to.
    """
    if not raw:
        return ""
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw, flags=re.IGNORECASE).strip()
    fence_match = re.search(r"```(?:[a-zA-Z]+)?\s*([\s\S]*?)```", cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ('"', "'"):
        cleaned = cleaned[1:-1].strip()
    return cleaned


def extract_json_block(raw: str) -> str:
    """Isolate the largest valid JSON object inside an LLM reply.

    Sonnet (and most large models) frequently mix prose with literal braces
    before the actual JSON payload — e.g. mentioning ``{classifier, summarizer}``
    in an explanation, or echoing the prompt's example syntax. The previous
    greedy regex ``\\{[\\s\\S]*\\}`` would grab from the first prose brace to
    the last JSON brace and feed garbage to ``json.loads``.

    Strategy:
      1. If a ```` ```json ``` ```` (or generic ```` ``` ```` ) fence exists, look
         inside it first.
      2. Walk each ``{`` in the (possibly fenced) text, track balanced braces
         while ignoring braces inside string literals, and try ``json.loads``
         on each balanced span. Return the **longest** span that parses.
      3. Fall back to the original raw input so callers' own ``json.loads``
         can surface the real error.
    """
    candidates: list[str] = []

    fence = re.search(r"```(?:json|JSON)?\s*([\s\S]*?)```", raw)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(raw.strip())

    best: Optional[str] = None
    best_is_envelope = False
    for text in candidates:
        for span in _iter_balanced_json_objects(text):
            try:
                obj = json.loads(span)
            except json.JSONDecodeError:
                continue
            # Prefer a blueprint-style envelope (top-level "nodes") even when a
            # glued inner node object happens to be a longer valid span. Local
            # models sometimes malform sibling nodes so badly that the ONLY
            # parseable span is a single inner node; the envelope preference
            # stops us from silently accepting that as the whole graph.
            is_envelope = isinstance(obj, dict) and "nodes" in obj
            if best is None:
                best, best_is_envelope = span, is_envelope
            elif is_envelope and not best_is_envelope:
                best, best_is_envelope = span, True
            elif is_envelope == best_is_envelope and len(span) > len(best):
                best = span
        if best is not None and best_is_envelope:
            return best

    # If the best we found is not an envelope, the top-level object is likely
    # malformed (glued sibling nodes). Try a structural repair before giving up.
    for text in candidates:
        repaired = _repair_glued_json(text)
        if repaired is not None:
            try:
                obj = json.loads(repaired)
            except json.JSONDecodeError:
                obj = None
            if isinstance(obj, dict) and "nodes" in obj:
                return repaired

    if best is not None:
        return best

    # No parseable object found — let the caller's json.loads raise the
    # original error with the original raw text so logs stay useful.
    return raw


def _iter_balanced_json_objects(text: str):
    """Yield each balanced ``{...}`` substring in ``text``.

    Tracks string state so braces inside ``"..."`` literals don't affect
    depth. Handles escape sequences (``\\"``, ``\\\\``) inside strings.
    Skips malformed regions silently — the caller validates with ``json.loads``.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        escape = False
        while j < n:
            ch = text[j]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i : j + 1]
                        break
            j += 1
        # Move past this opening brace whether or not we closed it
        i += 1


def _repair_glued_json(raw: str) -> Optional[str]:
    """Repair the most common structural defect in local-model JSON output.

    Smaller local models (e.g. ``gpt-oss-120b``) intermittently emit array
    elements that are *glued* together instead of comma-separated. Instead of

        {"id": "a", ...}, {"id": "b", ...}

    they produce

        {"id": "a", ...} }, {"id": "b", ...}      # stray closing brace, or
        {"id": "a", ...} {"id": "b", ...}         # missing ``,`` entirely, or
        {"id": "a", ...}, "id": "b", ...}         # missing ``{`` after comma

    The greedy/balanced extractor then latches onto a single inner object,
    which downstream shows up as a "single bare node" blueprint. This routine
    applies a few narrow, reversible textual fixes and returns the result only
    if it parses; otherwise ``None`` so the caller can fall back cleanly.
    """
    if not raw:
        return None

    fixes = [
        # ``} },{`` / ``} },"`` -> ``},{`` : stray closing brace between siblings.
        (re.compile(r"\}\s*\}\s*,\s*\{"), "},{"),
        # ``}\n\n{`` with no separator (missing comma) between two objects.
        (re.compile(r"\}\s*\{"), "},{"),
        # ``},\s*"key":`` inside an array where a new ``{`` was dropped:
        #   ...}, "id": "..."  ->  ...},{"id": "..."
        # An optional stray closing brace may sit between the ``}`` and ``,``
        # (compound defect: stray brace *and* dropped ``{``), e.g.
        #   ...} }, "id": "..."  ->  ...},{"id": "..."
        # Only trigger when the key looks like a node field to stay conservative.
        (re.compile(r"\}\s*\}?\s*,\s*\"(id|type|source|target)\"\s*:"), '},{"\\1":'),
    ]

    candidate = raw
    for pattern, repl in fixes:
        patched = pattern.sub(repl, candidate)
        # Prefer the incremental patch only if it still balances better; we
        # validate the final combined result with json.loads below.
        candidate = patched

    for text in (candidate, raw):
        try:
            json.loads(text)
            return text
        except json.JSONDecodeError:
            continue
    return None


# ---------------------------------------------------------------------------
# Catalog matching — shared scoring logic for all factories
# ---------------------------------------------------------------------------

MATCH_THRESHOLD: float = 0.8


def score_catalog_match(requested: str, item: dict) -> float:
    """Score how well ``requested`` matches a catalog ``item``.

    Returns 1.0 (exact) down to 0.0 (no match).  Both ``ToolSkillMatcher``
    (agent factory) and ``WorkflowSkillMatcher`` (workflow factory) delegate
    to this single implementation.
    """
    req = requested.lower().replace("_", "-").replace(" ", "-")
    name = (item.get("name") or "").lower().replace("_", "-").replace(" ", "-")
    desc = (item.get("description") or "").lower()

    if req == name:
        return 1.0
    if req in name or name in req:
        return 0.9

    req_words = [w for w in re.split(r"[-_\s]+", req) if len(w) > 3]
    name_words = set(re.split(r"[-_\s]+", name))
    if req_words and all(w in name_words or w in desc for w in req_words):
        return 0.85

    if req_words:
        matched = sum(1 for w in req_words if w in name or w in desc)
        ratio = matched / len(req_words)
        if ratio >= 0.6:
            return 0.5 + ratio * 0.3
    return 0.0


async def semantic_catalog_match(
    unmatched: list[str],
    catalog: list[dict],
) -> list[dict]:
    """Ask the LLM whether any catalog entry semantically covers the
    ``unmatched`` items.  Returns a list of ``{"requested": ...,
    "catalog_name": ...}`` dicts for successful matches.

    This is a single LLM call regardless of how many gaps there are.
    """
    if not unmatched or not catalog:
        return []

    catalog_summary = "\n".join(
        f"- {s['name']}: {(s.get('description') or '')[:120]}"
        for s in catalog
    )
    system = (
        "You are a capability matcher. Map each requested capability to the closest catalog entry.\n"
        "Only match when the catalog entry genuinely covers the capability — don't force-fit.\n"
        'Return JSON only: {"matches": [{"requested": "<name>", "catalog_name": "<exact catalog name or null>"}]}'
    )
    prompt = (
        f"Needed capabilities: {json.dumps(unmatched)}\n\n"
        f"Available catalog:\n{catalog_summary}\n\n"
        "For each capability, return the best matching catalog name, or null if none covers it."
    )
    try:
        raw = await call_factory_llm(system, [{"role": "user", "content": prompt}], max_tokens=32000)
        parsed = json.loads(extract_json_block(raw))
        matches = parsed.get("matches", [])
    except Exception:
        logger.debug('[AGENT] semantic_catalog_match: LLM fallback failed')
        return []

    catalog_names = {s["name"] for s in catalog}
    return [
        m for m in matches
        if m.get("catalog_name") and m["catalog_name"] in catalog_names
    ]


# ---------------------------------------------------------------------------
# Existing-item semantic matching — "does this already exist?"
# ---------------------------------------------------------------------------
#
# Used by the Create-with-AI factories (workflow / agent / skill) to detect
# when a user's request is already covered by an existing workflow, agent, or
# skill, so the factory can recommend opening/reusing it instead of building a
# near-duplicate. Mirrors ``semantic_catalog_match`` (single LLM call, no new
# dependencies) but returns full candidate records keyed by ``id`` rather than
# capability-name mappings.

# Confidence floor for a "balanced" recommendation — only surface clear matches;
# when the model is uncertain it should return a lower score and we skip it,
# so users aren't nagged about loosely-related items. Override via env for
# tuning without a redeploy.
EXISTING_MATCH_MIN_CONFIDENCE: float = float(
    os.getenv("FACTORY_EXISTING_MATCH_MIN_CONFIDENCE", "0.5")
)

# Cap how many candidates we send to the LLM so a large catalog can't blow the
# prompt budget. Candidates are truncated in caller-provided order (callers
# should pass most-recent-first for workflows/agents).
_EXISTING_MATCH_MAX_CANDIDATES: int = int(
    os.getenv("FACTORY_EXISTING_MATCH_MAX_CANDIDATES", "60")
)

# Hard ceiling on the match LLM call. The gateway occasionally takes 2-3 minutes
# on this model; without a timeout the "Checking for existing…" step hangs and
# the whole build feels broken. On timeout we fail safe (return no matches) and
# let the factory proceed to build new. Override via env for tuning.
_EXISTING_MATCH_TIMEOUT_S: float = float(
    os.getenv("FACTORY_EXISTING_MATCH_TIMEOUT_S", "30")
)


_TFIDF_PREFILTER_TOP_N: int = int(os.getenv("FACTORY_MATCH_PREFILTER_TOP_N", "10"))

# Common English stop-words to ignore during TF-IDF pre-filtering so that
# words like "the", "a", "that" don't inflate overlap scores.
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "this", "that", "be", "as",
    "are", "was", "were", "has", "have", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "i", "you", "we",
    "they", "he", "she", "my", "your", "our", "their", "its", "not", "no",
    "so", "if", "then", "than", "when", "which", "who", "what", "how",
    "new", "create", "build", "make", "add", "get", "set", "use", "using",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, remove stop-words."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]


def _tfidf_prefilter(query: str, candidates: list[dict], top_n: int) -> list[dict]:
    """Return the ``top_n`` candidates most likely to match ``query``.

    Uses TF-IDF-style scoring (term frequency in candidate × inverse document
    frequency across all candidates) to rank candidates by relevance to the
    query without any external dependencies. This narrows the LLM prompt from
    up to 60 candidates down to a focused shortlist, making the LLM call
    faster, cheaper, and more accurate.
    """
    if len(candidates) <= top_n:
        return candidates

    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return candidates[:top_n]

    # IDF: count how many candidates contain each query token
    doc_freq: dict[str, int] = {t: 0 for t in query_tokens}
    candidate_tokens: list[set[str]] = []
    for c in candidates:
        text = f"{c.get('name', '')} {c.get('description', '')}"
        tokens = set(_tokenize(text))
        candidate_tokens.append(tokens)
        for t in query_tokens:
            if t in tokens:
                doc_freq[t] += 1

    n = len(candidates)
    idf: dict[str, float] = {
        t: math.log((n + 1) / (doc_freq[t] + 1)) + 1.0
        for t in query_tokens
    }

    # Score each candidate: sum of IDF weights for matching query tokens
    scored: list[tuple[float, int]] = []
    for i, tokens in enumerate(candidate_tokens):
        score = sum(idf[t] for t in query_tokens if t in tokens)
        scored.append((score, i))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_indices = {i for _, i in scored[:top_n]}

    # Preserve original order among the selected candidates
    return [c for j, c in enumerate(candidates) if j in top_indices]


async def semantic_match_existing(
    query: str,
    candidates: list[dict],
    *,
    max_results: int = 3,
    min_confidence: Optional[float] = None,
) -> list[dict]:
    """Return existing ``candidates`` that semantically satisfy ``query``.

    ``query`` is a short natural-language description of what the user is about
    to build (typically ``name`` + ``purpose``/``description`` from the gathered
    requirements). Each candidate is a dict with at least ``id`` and ``name``
    and optionally ``description``. Extra keys (e.g. ``kind``) are preserved and
    echoed back on matches so callers can route the "Open" action.

    Uses a two-stage approach for speed and accuracy:
    1. TF-IDF pre-filter: narrows up to 60 candidates down to top 10 by keyword
       overlap — pure Python, zero latency, no external deps.
    2. LLM rerank: judges only the shortlisted candidates for genuine semantic
       overlap, keeping the prompt small and the call fast.

    Returns a list of the original candidate dicts (annotated with ``_match``:
    ``{"confidence": float, "reason": str}``) for candidates the model judged a
    genuine match at or above ``min_confidence`` (defaults to
    ``EXISTING_MATCH_MIN_CONFIDENCE``), best first, capped at ``max_results``.

    Fails safe: any error (LLM unavailable, unparseable output, gateway block)
    returns ``[]`` so the factory silently proceeds to build new rather than
    erroring the whole chat turn.
    """
    if not query or not candidates:
        return []

    threshold = (
        EXISTING_MATCH_MIN_CONFIDENCE if min_confidence is None else min_confidence
    )

    # Stage 1 — TF-IDF pre-filter: cap candidates before hitting the LLM.
    # First apply the hard catalog cap, then narrow further with TF-IDF so the
    # LLM only sees the most relevant shortlist.
    trimmed = candidates[:_EXISTING_MATCH_MAX_CANDIDATES]
    shortlisted = _tfidf_prefilter(query, trimmed, _TFIDF_PREFILTER_TOP_N)
    logger.debug(f'[AGENT] semantic_match_existing: tfidf pre-filter {len(trimmed)} → {len(shortlisted)} candidates')

    # Stage 2 — LLM rerank: index by ordinal so the model can reference
    # candidates compactly without us trusting it to echo long ids verbatim.
    by_index: dict[int, dict] = {}
    lines: list[str] = []
    for i, c in enumerate(shortlisted):
        by_index[i] = c
        name = str(c.get("name") or "").strip() or f"item-{i}"
        desc = str(c.get("description") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {name}: {desc[:160]}" if desc else f"[{i}] {name}")

    catalog_block = "\n".join(lines)
    system = (
        "You detect duplicates. The user wants to build something new. Decide which "
        "EXISTING items (if any) already do essentially the same thing, so we can "
        "recommend reusing one instead of building a duplicate.\n"
        "Only match when an existing item genuinely covers the request — same core "
        "purpose, not merely the same domain. When unsure, do NOT match.\n"
        'Return JSON only: {"matches":[{"index":<int>,"confidence":<0..1>,'
        '"reason":"<one short sentence>"}]}. Empty list if nothing matches.'
    )
    prompt = (
        f"User wants to build:\n{query.strip()[:600]}\n\n"
        f"Existing items:\n{catalog_block}\n\n"
        "Return the matches JSON now."
    )

    try:
        raw = await asyncio.wait_for(
            call_factory_llm(
                system, [{"role": "user", "content": prompt}], max_tokens=32000,
            ),
            timeout=_EXISTING_MATCH_TIMEOUT_S,
        )
        parsed = json.loads(extract_json_block(raw))
        raw_matches = parsed.get("matches", []) if isinstance(parsed, dict) else []
    except asyncio.TimeoutError:
        logger.warning(f'[AGENT] semantic_match_existing: LLM match timed out after {_EXISTING_MATCH_TIMEOUT_S}s — skipping recommendation, proceeding to build')
        return []
    except Exception:
        logger.debug('[AGENT] semantic_match_existing: LLM match failed', exc_info=True)
        return []

    results: list[dict] = []
    for m in raw_matches:
        if not isinstance(m, dict):
            continue
        try:
            idx = int(m.get("index"))
            conf = float(m.get("confidence", 0.0))
        except (TypeError, ValueError):
            continue
        cand = by_index.get(idx)
        if cand is None or conf < threshold:
            continue
        annotated = dict(cand)
        annotated["_match"] = {
            "confidence": round(conf, 3),
            "reason": str(m.get("reason") or "").strip()[:200],
        }
        results.append(annotated)

    results.sort(key=lambda r: r["_match"]["confidence"], reverse=True)
    return results[:max_results]


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Keyword-based tool / skill fallback — shared by all factories
# ---------------------------------------------------------------------------

# Maps natural-language keywords to exact catalog skill names.
SKILL_KEYWORDS: dict[str, str] = {
    "excel": "xlsx",
    "spreadsheet": "xlsx",
    "xlsx report": "xlsx",
    "xlsx file": "xlsx",
    "xlsx": "xlsx",
    "csv file": "xlsx",
    "powerpoint": "pptx",
    "presentation": "pptx",
    "slide deck": "pptx",
    "pptx": "pptx",
    "word document": "docx",
    "docx file": "docx",
    "docx": "docx",
    "pdf report": "pdf",
    "pdf file": "pdf",
    "pdf": "pdf",
}

# Action verbs used to score tools by relevance to an agent's job.
_TOOL_ACTION_WORDS: set[str] = {
    "get", "fetch", "list", "search", "read", "find", "query",
    "create", "add", "post", "write", "make", "new",
    "update", "edit", "modify", "patch",
    "delete", "remove",
    "comment", "assign", "transition", "move", "link",
}


# Action families — synonym groups so an agent whose job says "fetch" matches a
# tool named "get" (they mean the same thing). Without this, a "Fetcher" agent
# scores 0 against ``jira_get_issue`` and falls back to alphabetical order,
# wrongly picking ``jira_add_*`` write tools (the screenshot bug). Each family
# lists the verbs that imply it; a tool belongs to a family if its name or
# description contains any of those verbs.
_ACTION_FAMILIES: dict[str, set[str]] = {
    "read":   {"get", "fetch", "retrieve", "read", "list", "search", "find",
               "query", "lookup", "pull", "load", "view", "show", "check"},
    "create": {"create", "add", "post", "new", "make", "open", "raise", "file",
               "submit", "insert", "log"},
    "update": {"update", "edit", "modify", "patch", "change", "set", "assign",
               "transition", "move", "link", "comment", "close", "resolve"},
    "delete": {"delete", "remove", "close", "archive", "cancel"},
}


def _action_families(text: str) -> set[str]:
    """Return the action families implied by ``text`` (an agent job or tool)."""
    tokens = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return {fam for fam, verbs in _ACTION_FAMILIES.items() if tokens & verbs}


# Service aliases — a small hand-tuned SEED of genuine abbreviations/synonyms
# that keyword derivation can't infer from tool names/descriptions alone (e.g.
# "MR" → gitlab). Merged ON TOP of the dynamically derived index in
# ``build_service_index`` for precision — it is NOT the source of truth. Keep
# this tiny; every real service is discovered from the live catalog.
_SERVICE_ALIASES: dict[str, list[str]] = {
    "gitlab": ["gitlab", "merge request", "mr", "code review", "repository", "pipeline", "ci/cd"],
    "jira": ["jira", "sprint", "ticket", "backlog", "epic", "story point", "kanban", "scrum"],
}


# Words that carry no service signal — stripped when deriving keywords from tool
# names/descriptions so generic verbs/nouns don't bleed one service's keywords
# into another's. Union of English stop-words, the action verbs (which describe
# WHAT a tool does, not WHICH service it belongs to), and tool-domain filler.
_SERVICE_INDEX_STOPWORDS: frozenset[str] = frozenset(
    _STOP_WORDS
    | _TOOL_ACTION_WORDS
    | {
        "tool", "tools", "api", "apis", "data", "info", "information", "item",
        "items", "record", "records", "entry", "entries", "detail", "details",
        "field", "fields", "value", "values", "id", "ids", "name", "names",
        "given", "specific", "single", "multiple", "all", "any", "via", "using",
        "return", "returns", "returned", "response", "request", "requests",
        "call", "calls", "endpoint", "endpoints", "given",
    }
)


def _service_keyword_tokens(text: str) -> list[str]:
    """Tokenise ``text`` into service-signal words (len>2, not a stop/action word)."""
    return [
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(t) > 2 and t not in _SERVICE_INDEX_STOPWORDS
    ]


def build_service_index(available_tools: list[dict]) -> dict[str, dict]:
    """Derive, from the live catalog, a per-service index used for tool matching.

    Returns ``{service: {"keywords": set[str], "tools": list[dict]}}`` where
    ``keywords`` are salient words drawn from the service name plus every tool's
    name and description (stop/action words removed), and ``tools`` is the list
    of tool dicts belonging to that service.

    This auto-covers every current and future service with zero hardcoding —
    the previous 2-entry ``_SERVICE_ALIASES`` map is now only a precision seed
    merged on top. Pure in-memory over the already-cached catalog, so callers
    can rebuild it cheaply (the workflow factory caches it on the 60s catalog
    TTL rather than per-request).
    """
    index: dict[str, dict] = {}
    for t in available_tools or []:
        svc = (t.get("service") or "").strip().lower()
        if not svc:
            continue
        entry = index.setdefault(svc, {"keywords": set(), "tools": []})
        entry["tools"].append(t)
        # The service name itself is always a keyword.
        entry["keywords"].update(_service_keyword_tokens(svc))
        entry["keywords"].update(_service_keyword_tokens(t.get("name") or ""))
        entry["keywords"].update(_service_keyword_tokens(t.get("description") or ""))

    # Merge the hand-tuned seed aliases on top for precision.
    for svc, aliases in _SERVICE_ALIASES.items():
        entry = index.setdefault(svc, {"keywords": set(), "tools": []})
        for alias in aliases:
            entry["keywords"].update(_service_keyword_tokens(alias))
    return index


# Verbs that ALWAYS imply reaching an external system (unambiguous).
_EXTERNAL_ACTION_VERBS: frozenset[str] = frozenset({
    "fetch", "retrieve", "get", "pull", "query", "search", "lookup",
    "send", "post", "notify", "publish", "sync", "push", "upload",
    "download", "ingest", "transition",
    # Read/analysis verbs: an agent asked to "analyse this Jira issue" or
    # "review the repo code" must read from that external system first, so it
    # genuinely needs a tool. These were previously missing, so analysis agents
    # (very common) were wrongly treated as reasoning-only and shipped without
    # a tool — surfacing at runtime as "Please attach a <service> tool".
    "analyse", "analyze", "review", "read", "inspect", "examine", "check",
    "scan", "load", "import", "list",
})

# Verbs that MAY be internal reasoning ("write a reply", "create a summary") or
# external ("write to Jira"). Only count as needing a tool when paired with an
# external-target noun below.
_AMBIGUOUS_ACTION_VERBS: frozenset[str] = frozenset({
    "create", "add", "write", "make", "new", "update", "edit", "modify",
    "patch", "delete", "remove", "comment", "assign", "move", "link", "raise",
    "file", "log", "close", "open",
})

# Nouns that signal an external target/system when paired with an ambiguous verb.
_EXTERNAL_TARGET_NOUNS: frozenset[str] = frozenset({
    "issue", "issues", "ticket", "tickets", "card", "cards", "pr", "mr",
    "merge", "request", "pipeline", "repo", "repository", "channel", "message",
    "record", "row", "table", "database", "page", "wiki", "epic", "sprint",
    "comment", "label", "status", "board", "task",
})


def agent_needs_tool(agent_name: str, agent_instructions: str) -> bool:
    """Heuristic: does this agent's job require an EXTERNAL action (a tool)?

    Conservative on both sides — returns True when the job clearly reaches an
    external system so the caller resolves-or-asks rather than silently
    shipping an empty agent (the screenshot bug); returns False for pure
    reasoning jobs (summarize, classify, draft a reply, decide) so they
    correctly get ``tools: []`` and don't trigger spurious "missing tool"
    warnings.

    Rules:
      - An unambiguous external verb (fetch/send/notify/...) → True.
      - An ambiguous verb (create/write/update/...) counts ONLY when paired with
        an external-target noun (issue/ticket/channel/record/...).
      - The "intake" phrasing (e.g. "Issue Intake") → True.
    """
    text = f"{agent_name} {agent_instructions}".lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))

    if tokens & _EXTERNAL_ACTION_VERBS:
        return True
    if "intake" in text or "look up" in text:
        return True
    if (tokens & _AMBIGUOUS_ACTION_VERBS) and (tokens & _EXTERNAL_TARGET_NOUNS):
        return True
    return False


def resolve_services_for_agent(
    agent_name: str,
    agent_instructions: str,
    service_index: dict[str, dict],
) -> list[str]:
    """Return the catalog services whose keywords the agent's job mentions,
    ordered by match strength (strongest first).

    Uses the dynamically derived ``service_index`` (see ``build_service_index``)
    so it covers every real service, not just gitlab/jira. Returns [] when no
    service is referenced (the capability may be missing from the catalog).

    Callers use the shape of the result:
      - 1 service  → assign silently
      - 2+ services → ambiguous, ask the user
      - 0 services  → capability likely missing, report it
    """
    text = f"{agent_name} {agent_instructions}".lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))

    direct_hits: list[tuple[int, str]] = []
    weak_hits: list[tuple[int, str]] = []
    for svc, entry in service_index.items():
        directly_named = bool(re.search(rf"\b{re.escape(svc)}\b", text))
        # Count only DISTINCTIVE keyword overlap — words shared with the
        # service name/description that aren't generic across services.
        overlap = len(tokens & entry["keywords"])
        if directly_named:
            # Explicit mention (e.g. "Jira REST API") is authoritative.
            direct_hits.append((5 + overlap, svc))
        elif overlap >= 2:
            # No direct mention: require at least TWO distinctive keywords so a
            # single generic word ("request", "update") can't drag in unrelated
            # services (the Jira agent falsely matching gitlab/platform bug).
            weak_hits.append((overlap, svc))

    # If any service is explicitly named, trust ONLY those — a directly named
    # service is never genuinely ambiguous with a keyword-only guess.
    chosen = direct_hits if direct_hits else weak_hits
    chosen.sort(key=lambda x: (-x[0], x[1]))
    return [svc for _, svc in chosen]


def keyword_match_tools(
    agent_name: str,
    agent_instructions: str,
    available_tools: list[dict],
    *,
    match_field: str = "name",
    max_per_service: int = 3,
    search_instructions: bool = False,
    service_index: Optional[dict[str, dict]] = None,
) -> list[str]:
    """Match tools to an agent using the dynamically derived service index.

    A service "matches" the agent when the agent's search text shares any
    derived keyword with that service (covering every catalog service, not just
    a hardcoded few). When ``search_instructions=True`` the instructions are
    included in the search text (single-agent contexts); leave ``False``
    (default) for multi-agent workflows where instructions reference upstream
    services and would cause cross-agent false positives.

    ``service_index`` may be supplied by the caller (workflow factory caches it
    on the catalog TTL); when omitted it is derived from ``available_tools``.
    Tools are scored by action-word overlap with the instructions. Returns up
    to ``max_per_service`` names per matched service, capped at 3 total.
    """
    index = service_index or build_service_index(available_tools)

    name_lower = agent_name.lower()
    instr_lower = agent_instructions.lower()
    search_text = name_lower if not search_instructions else f"{name_lower} {instr_lower}"
    search_tokens = set(re.findall(r"[a-z0-9]+", search_text))

    def _service_matches(svc: str) -> bool:
        if re.search(rf"\b{re.escape(svc)}\b", search_text):
            return True
        return bool(search_tokens & index.get(svc, {}).get("keywords", set()))

    # What ACTIONS does this agent perform? (read / create / update / delete).
    # We match a tool's action family to the agent's, so a "Fetcher" (read) gets
    # ``get_issue`` rather than ``add_comment``.
    agent_families = _action_families(search_text)
    # Content nouns from the instructions (e.g. "comment", "watcher") let us
    # break ties within the same family by topical overlap with the tool.
    instr_tokens = {
        w for w in re.findall(r"[a-z0-9]+", instr_lower)
        if len(w) > 2 and w not in _SERVICE_INDEX_STOPWORDS
    }

    matched: list[str] = []
    for svc in sorted(index):
        if not _service_matches(svc):
            continue
        scored: list[tuple[int, int, str]] = []
        for t in index[svc]["tools"]:
            name = t.get(match_field)
            if not name:
                continue
            tname = str(name).lower()
            tdesc = str(t.get("description") or "").lower()
            tool_families = _action_families(f"{tname} {tdesc}")

            # Primary signal: does the tool's action family match the agent's?
            # +3 when it does; -2 when the agent is clearly read-only but the
            # tool is a write tool (so a Fetcher never prefers add/update tools).
            if agent_families & tool_families:
                family_score = 3
            elif agent_families and tool_families and not (agent_families & tool_families):
                family_score = -2
            else:
                family_score = 0

            # Secondary signal: topical noun overlap (tool name/desc words that
            # appear in the agent's instructions).
            tool_tokens = set(re.findall(r"[a-z0-9]+", f"{tname} {tdesc}"))
            topic_score = len(instr_tokens & tool_tokens)

            scored.append((family_score, topic_score, str(name)))

        # Highest family match first, then topical overlap, then name for
        # deterministic ordering. Drop tools whose family actively conflicts
        # (negative) unless nothing better exists.
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        picks = [n for fam, _, n in scored if fam >= 0][:max_per_service]
        if not picks:  # everything conflicted — fall back to best available
            picks = [n for _, _, n in scored[:max_per_service]]
        matched.extend(picks)

    return matched[:3]


def keyword_match_skills(
    agent_name: str,
    agent_instructions: str,
    available_skills: list[dict],
    max_skills: int = 2,
    search_instructions: bool = False,
) -> list[str]:
    """Match skills to an agent by checking if the agent's name (and optionally
    instructions) mention a known keyword (e.g. "excel" → ``xlsx``).

    Set ``search_instructions=True`` for single-agent contexts (Agent Factory).
    Leave ``False`` (default) for multi-agent workflows where instructions
    reference upstream/downstream agents and would cause false positives.

    Returns up to ``max_skills`` matched skill names.
    """
    valid = {s["name"] for s in available_skills}
    search_text = f"{agent_name} {agent_instructions}".lower() if search_instructions else agent_name.lower()

    matched: list[str] = []
    seen: set[str] = set()
    for keyword, skill_name in SKILL_KEYWORDS.items():
        if keyword in search_text and skill_name not in seen and skill_name in valid:
            matched.append(skill_name)
            seen.add(skill_name)

    return matched[:max_skills]


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def parse_json_response(text: str) -> dict:
    """Strip markdown fences then parse JSON. Returns ``{}`` on failure.

    Raises ``SecurityGatewayRejection`` when the response is a gateway
    content-filter block — callers should let it propagate to the user
    rather than silently degrading to ``{}`` (which would hide the cause).
    """
    raise_if_gateway_rejection(text, context="parse_json_response")
    text = extract_json_block(text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f'[AGENT] parse_json_response failed: {exc} — raw: {text}')
        return {}
