# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.fetchers.gcp_billing_bq
#
# Vertex AI Gemini spend from GCP Billing BigQuery export — routed via
# llm_proxy on the LLM proxy server.
#
# Wire protocol:
#   POST ${LLM_PROXY_URL}/spend/gcp/bigquery
#       body: {"window_start": "YYYY-MM-DD", "window_end": "YYYY-MM-DD"}
#       resp: {"rows": [{
#           "usage_date":          "YYYY-MM-DD",
#           "service_description": "Vertex AI" | "Generative Language API" | "Gemini API",
#           "sku_description":     "Generate content input token count gemini 3.5 flash text",
#           "cost_usd":            "1.234567",   # str (preserve Decimal precision)
#           "usage_amount":         1234,
#           "line_count":           5
#       }, ...]}
#
# The SQL template, project, and table all live on the LLM proxy server — the gateway can
# never inject arbitrary SQL via this endpoint. Service-account JSON
# (GCP_BILLING_SA_JSON) is materialised once on the LLM proxy server and never crosses
# the network.
#
# TOKEN_TYPE ITEMISATION + LIVE-QUERY FIX 
#
#   Both the model-id extraction and the token_type classification below are
#   grounded in the real SKU strings observed in the Niveus
#   billing exports (which classify the exact same SKU vocabulary for the CSV-sourced
#   backfill — this module now uses the equivalent live-query logic).
# ============================================================

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Tuple

from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal
from services.llm_spend.approved_models import get_approved_models
from services.llm_spend.fetchers._common import (
    FetchResult, SpendRow, record_fetch_run, upsert_rows, _proxy_post,
    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ, TOKEN_TYPE_OUTPUT,
    TOKEN_TYPE_NON_TOKEN,
)


SOURCE = "gcp_bq_export"
PROVIDER = "gemini"

# GCP BigQuery billing-export scans can exceed the default 60s proxy timeout
# for wide windows. Override here; env-tunable. The overnight Gemini settle
# loop (orchestrator.run_gemini_until_settled) leans on this.
_GCP_HTTP_TIMEOUT = int(os.getenv("LLM_SPEND_GCP_HTTP_TIMEOUT", "120"))


# ── SKU classification ──────────────────────────────────────────────────────
#
# Real sku.description shapes observed across service='Gemini API' and
# service='Vertex AI' in the billing exports:
#
#   Token usage (Gemini API "Generate content ..." family):
#     "Generate content input token count gemini 3.5 flash text"
#     "Generate content cached input token count gemini 3.5 flash text"
#     "Generate content output token count gemini 2.5 flash short output text non-thinking"
#     "Generate_content text input token count for gemini 3 pro short"
#     "Generate_content text cached input token count for gemini 3 pro short"
#     "Generate_content image output token count for Gemini 3 Pro Image"
#     "Generate_content audio input token count for Gemini 3.5 Live Translate"
#
#   Token usage (Vertex AI "... - Predictions" family):
#     "Gemini 3.1 Flash Image Text Input - Predictions"
#     "Gemini 3.1 Flash Image Text Output - Predictions"
#     "Gemini 3.5 Flash Global Text Input - Predictions"
#     "Gemini 3.5 Flash Global Text Output - Predictions"
#
#   Non-token (real GCP spend, no token semantics):
#     "Veo Generation 720p with Audio", "Veo Fast Generation 720p with Audio",
#     "Veo Generation 1080p with Audio", "Veo Generation 4k with Audio",
#     "Number of videos generated", "Imagen 4 fast Generation (output)",
#     "Lyria 3 Audio Ouptut - Predictions", "Agent Platform Compute",
#     "Agent Platform Memory", "Grounding with Google Search on Gemini 3",
#     "Grounding with Google Maps on Gemini 3",
#     "Generate content search query gemini 3 free"

_NON_TOKEN_MARKERS = (
    "veo", "videos generated", "imagen", "lyria", "agent platform",
    "grounding", "search query",
)

# ORDER MATTERS: "cached input" must be tested before plain "input" — this is
# exactly the bug being fixed (_sku_is_input was a bare "input" in s check
# that matched cache-read SKUs too).
_CACHE_READ_RE = re.compile(r"cached\s+input", re.IGNORECASE)
_OUTPUT_RE     = re.compile(r"output\s+token\s+count|output\s*-\s*predictions", re.IGNORECASE)
_UNCACHED_RE   = re.compile(r"input\s+token\s+count|input\s*-\s*predictions", re.IGNORECASE)


def classify_token_type(sku_desc: str) -> str:
    """Classify a Gemini SKU description into a canonical token_type.

    Checked in order: non-token markers first (a "Grounding ... Search"
    or "Veo ..." SKU never reaches the input/output regexes), then
    cache_read BEFORE uncached (cached-input SKUs contain the substring
    "input" too — see module docstring). Falls back to non_token for any
    unrecognised shape rather than guessing, so a new GCP SKU we haven't
    seen yet becomes a visible non_token row instead of silently
    corrupting the uncached/cache_read/output split.
    """
    s = sku_desc or ""
    sl = s.lower()
    if any(marker in sl for marker in _NON_TOKEN_MARKERS):
        return TOKEN_TYPE_NON_TOKEN
    if _CACHE_READ_RE.search(s):
        return TOKEN_TYPE_CACHE_READ
    if _OUTPUT_RE.search(s):
        return TOKEN_TYPE_OUTPUT
    if _UNCACHED_RE.search(s):
        return TOKEN_TYPE_UNCACHED
    return TOKEN_TYPE_NON_TOKEN


# Longest / most specific patterns first — "3.1 Flash Image" must win over
# "3.1 flash lite", and "3 Pro Image" over "3 Pro". Patterns are matched
# against the raw SKU text (case-insensitive), not a normalised model field —
# unlike OpenAI/Anthropic, Gemini SKUs embed the model name in free text with
# no separate structured field.
_MODEL_PATTERNS = (
    (re.compile(r"gemini\s*3\.1\s*flash\s*image", re.IGNORECASE), "gemini-3.1-flash-image"),
    (re.compile(r"gemini\s*3\.1\s*flash\s*lite",  re.IGNORECASE), "gemini-3.1-flash-lite"),
    (re.compile(r"gemini\s*3\.5\s*flash",         re.IGNORECASE), "gemini-3.5-flash"),
    (re.compile(r"gemini\s*3\.5\s*live",          re.IGNORECASE), "gemini-3.5-live"),
    (re.compile(r"gemini\s*2\.5\s*flash",         re.IGNORECASE), "gemini-2.5-flash"),
    (re.compile(r"gemini\s*3\s*pro\s*image",      re.IGNORECASE), "gemini-3-pro-image"),
    (re.compile(r"gemini\s*3\s*pro",              re.IGNORECASE), "gemini-3-pro"),
)

NON_TOKEN_MODEL = "non-token-services"


def classify_sku(sku_desc: str) -> Tuple[str, str]:
    """Map a SKU description to (raw_model, token_type).

    Non-token SKUs (Veo/Imagen/Lyria/Agent Platform/Grounding) are bucketed
    under NON_TOKEN_MODEL rather than any Gemini model name — "Grounding
    with Google Search" isn't spend on a specific model, and lumping it
    under e.g. "gemini-3" would misattribute cost in the per-model
    breakdown.
    """
    s = sku_desc or ""
    token_type = classify_token_type(s)
    if token_type == TOKEN_TYPE_NON_TOKEN:
        return NON_TOKEN_MODEL, TOKEN_TYPE_NON_TOKEN

    for pattern, canon in _MODEL_PATTERNS:
        if pattern.search(s):
            return canon, token_type
    return "unknown-gemini", token_type


# ── llm_proxy call ────────────────────────────────────────────────────────

def _fetch_rows(window_start: date, window_end: date) -> list[dict]:
    """Ask the LLM proxy server to run the canonical billing-export query and return rows."""
    body = {
        "window_start": window_start.isoformat(),
        "window_end":   window_end.isoformat(),
    }
    resp = _proxy_post("/spend/gcp/bigquery", body, timeout=_GCP_HTTP_TIMEOUT)
    return resp.get("rows", []) or []


# ── settle check (overnight retry loop) ────────────────────────────────────

_SETTLED_SQL = text(
    """
    SELECT
        COALESCE(SUM(cost_usd), 0)      AS cost_usd,
        COALESCE(SUM(input_tokens), 0)  AS input_tokens,
        COALESCE(SUM(output_tokens), 0) AS output_tokens
    FROM ainxt.llm_spend_daily
    WHERE provider = 'gemini'
      AND usage_date = :d
      AND source = :source
    """
)


def window_is_settled(target_date: date) -> bool:
    """Heuristic: has Gemini billing for `target_date` actually landed?

    GCP's BigQuery billing export lags 6–24h, so a successful fetch shortly
    after the usage day can return zero/partial rows even though the API call
    itself returned 200 OK. The overnight settle loop
    (orchestrator.run_gemini_until_settled) calls this AFTER each upsert to
    decide whether to keep retrying.

    "Settled" = we have at least some cost AND both input and output token
    counts recorded for the day. This is a pragmatic completeness signal, not
    a guarantee — GCP can still revise totals upward inside the 24h window, in
    which case a later pass simply upserts the corrected number. We only use
    this to stop hammering once the day clearly has real data.

    Returns False (keep retrying) on any DB error so a probe failure doesn't
    prematurely declare the window settled.
    """
    try:
        with SessionLocal() as session:
            row = session.execute(
                _SETTLED_SQL, {"d": target_date, "source": SOURCE}
            ).fetchone()
    except Exception as e:
        logger.warning(f"[gcp_billing_bq] settle probe failed for {target_date}: {e}")
        return False
    if row is None:
        return False
    cost   = Decimal(str(row.cost_usd or 0))
    in_tok = int(row.input_tokens or 0)
    out_tok = int(row.output_tokens or 0)
    return cost > 0 and in_tok > 0 and out_tok > 0


# ── public ─────────────────────────────────────────────────────────────────

def compute_rows(window_start: date, window_end: date) -> list:
    """Fetch + aggregate [window_start, window_end] into SpendRows, no DB write.

    Split out from fetch_window() to match the anthropic_admin / openai_costs
    fetchers, so a future transactional backfill can reuse this without
    duplicating the classification + aggregation logic. Raises on fetch
    failure — the caller decides what that means for its context.
    """
    approved = get_approved_models()
    bq_rows = _fetch_rows(window_start, window_end)

    # Aggregate to (day, canonical_model, token_type). request_count follows
    # the same convention as the other two fetchers: attached only to the
    # 'output' row for each (day, model) so SUM(request_count) isn't
    # inflated across a model's token_type rows. GCP's billing export has no
    # concept of "requests" — line_count (rows folded per SKU per day) is
    # the closest proxy, so we carry it on the output row only.
    agg: Dict[Tuple[date, str, str], SpendRow] = {}
    for r in bq_rows:
        # Wire format: usage_date is an ISO string, cost_usd is a string.
        try:
            day = date.fromisoformat(r["usage_date"])
        except Exception:
            logger.warning(f"[gcp_billing_bq] skipping row with bad usage_date: {r!r}")
            continue
        sku_desc = r.get("sku_description") or ""
        cost     = Decimal(str(r.get("cost_usd") or 0))
        amount   = int(r.get("usage_amount") or 0)
        line_n   = int(r.get("line_count")   or 0)

        raw_model, token_type = classify_sku(sku_desc)
        canon = (
            NON_TOKEN_MODEL if token_type == TOKEN_TYPE_NON_TOKEN
            else approved.bucket(PROVIDER, raw_model)
        )

        key = (day, canon, token_type)
        row = agg.get(key)
        if row is None:
            row = SpendRow(usage_date=day, provider=PROVIDER, model=canon, token_type=token_type)
            agg[key] = row
        row.cost_usd += cost
        if token_type == TOKEN_TYPE_UNCACHED or token_type == TOKEN_TYPE_CACHE_READ:
            row.input_tokens += amount
        elif token_type == TOKEN_TYPE_OUTPUT:
            row.output_tokens += amount
            row.request_count += line_n
        # non_token rows carry cost only — no token count.

    return list(agg.values())


def fetch_window(window_start: date, window_end: date) -> FetchResult:
    run_started = datetime.utcnow()
    result = FetchResult(provider=PROVIDER, source=SOURCE,
                         window_start=window_start, window_end=window_end)

    try:
        rows = compute_rows(window_start, window_end)
    except Exception as e:
        logger.error(f"[gcp_billing_bq] query failed: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    try:
        n = upsert_rows(rows, source=SOURCE)
    except Exception as e:
        logger.error(f"[gcp_billing_bq] upsert failed: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    result.rows = rows
    record_fetch_run(PROVIDER, window_start, window_end, "ok", n, run_started)
    logger.info(f"[gcp_billing_bq] upserted {n} rows for {window_start}..{window_end}")
    return result
