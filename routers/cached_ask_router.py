# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CACHED ASK ROUTER
#
# POST /ask/cached
#
# Exposes Anthropic's block-level prompt caching to external
# callers via a structured API. Callers split their prompt into
# a stable prefix (reused across calls → cached) and a variable
# tail (per-call → never cached).
#
# Flow:
#   1. JWT auth
#   2. Budget gate (same as /ask)
#   3. Compliance check on assembled input (no output check)
#   4. model_router.generate_structured(blocks)
#   5. Usage recorded with cache_read / cache_write tokens
#   6. JSON response: result + token telemetry
#
# Claude-only: content_blocks caching is an Anthropic feature.
# Allowed model hints: "solution", "complex", "haiku".
# ============================================================

import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from auth.dependencies import get_current_user
from core.logger import logger

router = APIRouter(tags=["cached-ask"])

# Model hints that route to Claude (caching is Anthropic-only).
_CLAUDE_HINTS = {"solution", "complex", "haiku"}

# Anthropic reserves the right to reject fewer than 1024 tokens (~4096 chars).
_CACHE_CHAR_MIN = 4096

# Hard cap: Anthropic allows 4 cache breakpoints total; reserve 1 for tools.
_MAX_STABLE_BLOCKS = 3


# ============================================================
# SCHEMA
# ============================================================

class CachedAskRequest(BaseModel):
    stable_blocks: list[str]
    variable_tail: str
    model: str = "solution"
    agent_id: Optional[str] = None

    @field_validator("stable_blocks")
    @classmethod
    def _validate_blocks(cls, v):
        if not v:
            raise ValueError("stable_blocks must contain at least one string")
        if len(v) > _MAX_STABLE_BLOCKS:
            raise ValueError(
                f"stable_blocks may have at most {_MAX_STABLE_BLOCKS} entries "
                f"(Anthropic 4-breakpoint limit, 1 reserved for tools)"
            )
        return v

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v):
        if v not in _CLAUDE_HINTS:
            raise ValueError(
                f"model must be one of {sorted(_CLAUDE_HINTS)}. "
                f"Prompt caching is Claude-only (Anthropic feature)."
            )
        return v


class CachedAskResponse(BaseModel):
    result: str
    model_hint: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_ms: float
    compliance_redacted: bool


# ============================================================
# ENDPOINT
# ============================================================

@router.post("/ask/cached", response_model=CachedAskResponse)
async def cached_ask(
        req: CachedAskRequest,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """
    Claude call with block-level prompt caching.

    stable_blocks: 1–3 strings that are identical across multiple calls.
                   Each block >= 4096 chars qualifies for caching.
                   Blocks are sent most-stable-first (Anthropic caches prefixes).

    variable_tail: per-call content that changes every invocation (file, query, etc).
                   Never cached.

    On the first call, Anthropic writes the stable_blocks to its cache (5-min TTL,
    charged at 125% of base input cost). Subsequent calls within 5 minutes with the
    same stable_blocks bytes read from cache at 10% of base input cost.
    """
    _t0 = time.time()

    user_id    = current_user.get("sub") or current_user.get("user_id")
    user_email = current_user.get("email", "unknown")

    # ── 1. Budget gate ────────────────────────────────────────────────────────
    try:
        from store.budget_store import check_budget as _chk_budget
        _budget = _chk_budget(user_id)
        if _budget.get("allowed") is not True:
            logger.warning(
                f"[cached-ask] Budget gate BLOCKED: user={user_email} "
                f"reason={_budget.get('reason')}"
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error":  "budget_exceeded",
                    "reason": _budget.get("reason", "Budget allocation exhausted"),
                },
            )
    except HTTPException:
        raise
    except Exception as _be:
        logger.error(f"[cached-ask] Budget check failed (fail-open): {_be}")

    # ── 2. Assemble full prompt for compliance scan ───────────────────────────
    _assembled = "\n\n".join(
        [b for b in req.stable_blocks if b] + [req.variable_tail]
    )

    # ── 3. Input compliance check ─────────────────────────────────────────────
    _redacted    = False
    _safe_prompt = _assembled
    try:
        from agents.compliance_engine import compliance_engine as _ce
        _chk = _ce.validate_input(_assembled)
        if _chk.get("blocked"):
            _blocked_types = [
                f.get("type") for f in _chk.get("findings", []) if f.get("blocked")
            ]
            logger.warning(
                f"[cached-ask] Compliance BLOCKED: user={user_email} "
                f"types={_blocked_types}"
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "error":         "compliance_block",
                    "blocked_types": _blocked_types,
                    "message":       "Input contains restricted content (PCI/PII policy).",
                },
            )
        _safe_prompt = _chk.get("redacted_text") or _assembled
        _redacted    = bool(_chk.get("redacted_types"))
    except HTTPException:
        raise
    except Exception as _ce_err:
        logger.error(f"[cached-ask] Compliance check error (fail-open): {_ce_err}")

    # If redaction changed content, rebuild stable_blocks + variable_tail from the
    # redacted flat string so the blocks we send are also redacted.
    # Simple split: keep the original block structure unless redaction hit something.
    _send_blocks: list[str]
    _send_tail:   str
    if _redacted and _safe_prompt != _assembled:
        # Replace the assembled flat string's content in blocks proportionally.
        # Cheapest correct approach: send redacted content as a single stable block
        # + empty variable tail if the split is no longer reliable.
        _send_blocks = [_safe_prompt]
        _send_tail   = ""
    else:
        _send_blocks = [b for b in req.stable_blocks if b]
        _send_tail   = req.variable_tail

    # ── 4. Warn on blocks that won't be cached (too small) ───────────────────
    for _i, _blk in enumerate(_send_blocks):
        if len(_blk) < _CACHE_CHAR_MIN:
            logger.warning(
                f"[cached-ask] stable_blocks[{_i}] is {len(_blk)} chars "
                f"(< {_CACHE_CHAR_MIN} minimum). Anthropic will not cache it. "
                f"Pad or merge with adjacent blocks to cross the threshold."
            )

    # ── 5. Build content_blocks payload ──────────────────────────────────────
    _blocks = [{"text": b, "cache": True} for b in _send_blocks]
    _blocks.append({"text": _send_tail, "cache": False})

    # ── 6. Model call ─────────────────────────────────────────────────────────
    try:
        from models.model_router import model_router as _mr
        result = _mr.generate_structured(blocks=_blocks, model_hint=req.model)
        if not result or not result.strip():
            raise ValueError("Empty response from model")
    except Exception as _mr_err:
        logger.error(f"[cached-ask] model_router.generate_structured failed: {_mr_err}")
        raise HTTPException(status_code=502, detail=f"Model call failed: {_mr_err}")

    _latency_ms = (time.time() - _t0) * 1000

    # ── 7. Collect token telemetry ────────────────────────────────────────────
    try:
        from models.model_router import model_router as _mr2
        _tokens_in       = _mr2.last_input_tokens
        _tokens_out      = _mr2.last_output_tokens
        _cache_read      = _mr2.last_cache_read_tokens
        _cache_created   = _mr2.last_cache_creation_tokens
    except Exception:
        _tokens_in = _tokens_out = _cache_read = _cache_created = 0

    # Cache-aware cost (Sonnet 4.6 pricing: $0.003/1K in, $0.015/1K out).
    _base_in = 0.003
    _out_rate = 0.015
    _normal_in = max(_tokens_in - _cache_read - _cache_created, 0)
    _cost = (
            (_cache_read    / 1000 * _base_in * 0.10) +
            (_cache_created / 1000 * _base_in * 1.25) +
            (_normal_in     / 1000 * _base_in) +
            (_tokens_out    / 1000 * _out_rate)
    )

    logger.info(
        f"[cached-ask] user={user_email} model={req.model} "
        f"in={_tokens_in} out={_tokens_out} "
        f"cache_read={_cache_read} cache_created={_cache_created} "
        f"cost~${_cost:.4f} latency={_latency_ms:.0f}ms"
    )

    # ── 8. Record usage ───────────────────────────────────────────────────────
    # Derive source_channel: desktop app → DESKTOP-CHAT, browser → WEB-CHAT.
    _cached_cs = getattr(request.state, "client_source", "platform")
    _cached_channel = "DESKTOP-CHAT" if _cached_cs == "desktop" else "WEB-CHAT"
    try:
        from memory.postgres_memory import PostgresMemory as _PM
        _PM().create_model_usage(
            model=req.model,
            user_id=user_id,
            agent_id=req.agent_id or "cached-ask",
            endpoint="/ask/cached",
            source_channel=_cached_channel,
            input_tokens=_tokens_in,
            output_tokens=_tokens_out,
            cost_usd=round(_cost, 6),
            latency_ms=_latency_ms,
            cache_read_tokens=_cache_read,
            cache_write_tokens=_cache_created,
        )
    except Exception as _pm_err:
        logger.warning(f"[cached-ask] create_model_usage failed (non-fatal): {_pm_err}")

    return CachedAskResponse(
        result=result,
        model_hint=req.model,
        input_tokens=_tokens_in,
        output_tokens=_tokens_out,
        cache_read_tokens=_cache_read,
        cache_write_tokens=_cache_created,
        cost_usd=round(_cost, 6),
        latency_ms=round(_latency_ms, 1),
        compliance_redacted=_redacted,
    )