# SPDX-License-Identifier: Apache-2.0
# ============================================================
# services.llm_spend.fetchers.openai_costs
#
# Pulls organisation-level cost and usage from OpenAI's admin API,
# routed via llm_proxy on the LLM proxy server:
#
#   POST ${LLM_PROXY_URL}/spend/openai/costs
#   POST ${LLM_PROXY_URL}/spend/openai/usage
#       body: {"params": {start_time, end_time, bucket_width, group_by, limit, page?}}
#
# Proxy maps these onto:
#   GET https://api.openai.com/v1/organization/costs
#   GET https://api.openai.com/v1/organization/usage/completions
#
# Auth: handled on the LLM proxy server (OPENAI_ADMIN_API_KEY); the gateway carries none.
# 429 retries (2s sleep) also live on the LLM proxy server — this client retries
# transport / 5xx via _proxy_post.
#
# TOKEN_TYPE ITEMISATION 
#   /costs returns a `quantity` field alongside `amount` on every result line
#   — the exact token count that line's cost applies to. This was previously
#   read nowhere. Verified against a full day of real org data that it
#   reconciles EXACTLY with /usage's per-model totals, e.g. gpt-5.4:
#       cached: 59,894,528 + 879,616 (long-ctx) == usage input_cached_tokens
#       input : 11,667,929 + 1,063,898           == usage input_uncached_tokens
#       output:  1,294,412 + 23,729              == usage output_tokens
#   So /costs alone gives us (cost, tokens) per (day, model, token_type) —
#   no join to /usage needed for the token breakdown. /usage is now used
#   ONLY for num_model_requests, which /costs does not carry and which has
#   no per-token_type meaning anyway (see request_count handling below).
#
#   `line_item` encodes model + token type + optional context tier, e.g.:
#       "gpt-5.4-2026-03-05, cached input, long context"
#       "gpt-5.4-2026-03-05, input"
#       "gpt-5.6-luna, cache writes"
#       "gpt-image-1 text, input"        — model id itself contains a space
#   We split on the FIRST comma for the model id, classify the remainder
#   (ignoring a "long context" suffix — cost/quantity for that tier folds
#   into the same token_type, matching the flat-cost-report design), and
#   default anything unclassifiable to TOKEN_TYPE_NON_TOKEN rather than
#   guessing, logging once per distinct line_item shape so new OpenAI billing
#   categories are noticed instead of silently mis-bucketed.
#
#   request_count has no natural per-token_type home (it's a per-model-per-day
#   figure) — it is written ONLY on the 'output' row so SUM(request_count)
#   stays correct instead of being N x inflated across N token_type rows.
# ============================================================

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Tuple

from core.logger import logger
from services.llm_spend.approved_models import get_approved_models
from services.llm_spend.fetchers._common import (
    FetchResult, SpendRow, record_fetch_run, upsert_rows, _proxy_post,
    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ, TOKEN_TYPE_CACHE_WRITE_5M,
    TOKEN_TYPE_OUTPUT, TOKEN_TYPE_NON_TOKEN,
)


SOURCE = "openai_costs_api"
PROVIDER = "openai"

# Checked in order — "cached input" must be tested before "input" since it
# contains "input" as a substring. The trailing ", long context" tier suffix
# (if present) has already been dropped by the caller before this runs.
_TOKEN_TYPE_KEYWORDS = (
    ("cache writes",  TOKEN_TYPE_CACHE_WRITE_5M),  # OpenAI has no 1h cache tier
    ("cached input",  TOKEN_TYPE_CACHE_READ),
    ("input",         TOKEN_TYPE_UNCACHED),
    ("output",        TOKEN_TYPE_OUTPUT),
)
_warned_unclassified_line_items: set = set()


def _classify_token_type(rest: str) -> str:
    """Classify the token-type suffix of a /costs `line_item`.

    `rest` is everything after the model id's comma, e.g. "cached input,
    long context" or "output". Falls back to TOKEN_TYPE_NON_TOKEN for shapes
    we don't recognise (e.g. a bare "tts hd" with no comma at all) so an
    unrecognised OpenAI billing category shows up as a visible new row
    rather than silently corrupting input/output/cache totals.
    """
    s = (rest or "").lower()
    for keyword, token_type in _TOKEN_TYPE_KEYWORDS:
        if keyword in s:
            return token_type
    if rest and rest not in _warned_unclassified_line_items:
        _warned_unclassified_line_items.add(rest)
        logger.warning(
            f"[openai_costs] unclassified line_item suffix {rest!r} "
            f"— bucketing as {TOKEN_TYPE_NON_TOKEN!r}; extend "
            f"_TOKEN_TYPE_KEYWORDS if this is a new billing category"
        )
    return TOKEN_TYPE_NON_TOKEN


def _to_unix(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc).timestamp())


def _paginate_proxy(report: str, params: Dict[str, str]):
    """Yield each page's `data` list from a paginated OpenAI organization endpoint.

    `report` is the suffix the proxy understands: 'costs' or 'usage'. The proxy
    handles upstream auth + 429 backoff; we only thread `next_page` between
    calls so each /spend/openai/* hit fetches exactly one page.
    """
    next_page = None
    while True:
        q = dict(params)
        if next_page:
            q["page"] = next_page
        body = _proxy_post(f"/spend/openai/{report}", {"params": q})
        yield body.get("data", [])
        next_page = body.get("next_page")
        if not next_page:
            return


def _fetch_costs(
    window_start: date, window_end: date
) -> Dict[Tuple[date, str, str], Dict[str, object]]:
    """Returns {(day, raw_model, token_type): {"cost": Decimal, "tokens": int}}.

    Both context tiers ("...", "long context" and the base line) fold into
    the same (day, model, token_type) cell — cost and quantity both sum
    across tiers, matching the flat, tier-agnostic token_type design.
    """
    params = {
        "start_time":   str(_to_unix(window_start)),
        "end_time":     str(_to_unix(window_end + timedelta(days=1))),
        "bucket_width": "1d",
        "group_by":     "line_item",
        "limit":        "180",
    }
    out: Dict[Tuple[date, str, str], Dict[str, object]] = defaultdict(
        lambda: {"cost": Decimal("0"), "tokens": 0}
    )
    for page in _paginate_proxy("costs", params):
        for bucket in page:
            start_ts = bucket.get("start_time")
            if start_ts is None:
                continue
            day = datetime.fromtimestamp(int(start_ts), tz=timezone.utc).date()
            for result in bucket.get("results", []):
                amount = result.get("amount", {}) or {}
                value = amount.get("value", 0) or 0
                # `quantity` is the token count this cost line applies to —
                # e.g. 59,894,528 for a "cached input" line. Verified exact
                # against /usage's per-model totals (see module docstring).
                quantity = result.get("quantity", 0) or 0

                line_item = (result.get("line_item") or "").strip()
                if line_item:
                    model_id, _, rest = line_item.partition(",")
                    model_id = model_id.strip()
                else:
                    model_id, rest = "unknown", ""
                token_type = _classify_token_type(rest.strip())

                cell = out[(day, model_id, token_type)]
                cell["cost"] += Decimal(str(value))
                cell["tokens"] += int(round(float(quantity)))
    return out


def _fetch_request_counts(
    window_start: date, window_end: date
) -> Dict[Tuple[date, str], int]:
    """Returns {(day, model): num_model_requests}.

    /usage is retained solely for request counts — /costs has no equivalent
    field, and a request count has no meaningful per-token_type split (a
    single request produces both input and output tokens).
    """
    # OpenAI /v1/organization/usage/completions caps `limit` at 31 for
    # bucket_width=1d (stricter than /costs). Pagination via next_page
    # already covers longer windows.
    params = {
        "start_time":   str(_to_unix(window_start)),
        "end_time":     str(_to_unix(window_end + timedelta(days=1))),
        "bucket_width": "1d",
        "group_by":     "model",
        "limit":        "31",
    }
    out: Dict[Tuple[date, str], int] = defaultdict(int)
    for page in _paginate_proxy("usage", params):
        for bucket in page:
            start_ts = bucket.get("start_time")
            if start_ts is None:
                continue
            day = datetime.fromtimestamp(int(start_ts), tz=timezone.utc).date()
            for result in bucket.get("results", []):
                model_id = (result.get("model") or "unknown").strip()
                out[(day, model_id)] += int(result.get("num_model_requests", 0) or 0)
    return out


# ── public ─────────────────────────────────────────────────────────────────

def compute_rows(window_start: date, window_end: date) -> List[SpendRow]:
    """Fetch + aggregate [window_start, window_end] into SpendRows, no DB write.

    Split out from fetch_window() so the one-off transactional backfill
    (services/llm_spend/backfill.py) can call the SAME fetch+aggregate logic
    inside its own DELETE-then-INSERT transaction, instead of duplicating
    this bucket-then-sum logic. Raises on any fetch failure.
    """
    approved = get_approved_models()

    costs    = _fetch_costs(window_start, window_end)
    requests = _fetch_request_counts(window_start, window_end)

    # Bucket-then-sum: /costs (line_item-derived model id) and /usage
    # (group_by=model) can disagree on the raw model string for the same
    # logical workload (e.g. /costs parses "gpt-image-1 text" from the
    # line_item while /usage's group_by=model returns "gpt-image-1"). Re-key
    # both by canonical id BEFORE merging so tokens/cost and request counts
    # for the same canon land together even when raw-key bucketing diverges.
    costs_canon: Dict[Tuple[date, str, str], Dict[str, object]] = defaultdict(
        lambda: {"cost": Decimal("0"), "tokens": 0}
    )
    for (day, raw_model, token_type), c in costs.items():
        canon = approved.bucket(PROVIDER, raw_model)
        cell = costs_canon[(day, canon, token_type)]
        cell["cost"]   += c["cost"]
        cell["tokens"] += c["tokens"]

    requests_canon: Dict[Tuple[date, str], int] = defaultdict(int)
    for (day, raw_model), n in requests.items():
        canon = approved.bucket(PROVIDER, raw_model)
        requests_canon[(day, canon)] += n

    agg: Dict[Tuple[date, str, str], SpendRow] = {}
    for (day, canon, token_type), c in costs_canon.items():
        tokens = int(c["tokens"])
        agg[(day, canon, token_type)] = SpendRow(
            usage_date=day,
            provider=PROVIDER,
            model=canon,
            token_type=token_type,
            cost_usd=Decimal(str(c["cost"])),
            input_tokens=tokens if token_type in (TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ) else 0,
            output_tokens=tokens if token_type == TOKEN_TYPE_OUTPUT else 0,
            # Attached below, only on the 'output' row.
            request_count=0,
        )

    # Attach request_count to the 'output' row for each (day, canon). If a
    # model has request activity but somehow no 'output' cost line in this
    # window (not observed in practice, but cheap to guard), create a
    # zero-cost carrier row so the request count isn't silently dropped.
    for (day, canon), n in requests_canon.items():
        if n == 0:
            continue
        key = (day, canon, TOKEN_TYPE_OUTPUT)
        if key in agg:
            agg[key].request_count = n
        else:
            agg[key] = SpendRow(
                usage_date=day, provider=PROVIDER, model=canon,
                token_type=TOKEN_TYPE_OUTPUT, request_count=n,
            )

    return list(agg.values())


def fetch_window(window_start: date, window_end: date) -> FetchResult:
    """Fetch [window_start, window_end] inclusive. Idempotent — upserts."""
    run_started = datetime.utcnow()
    result = FetchResult(provider=PROVIDER, source=SOURCE,
                         window_start=window_start, window_end=window_end)

    try:
        rows = compute_rows(window_start, window_end)
    except Exception as e:
        logger.error(f"[openai_costs] fetch failed for {window_start}..{window_end}: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    try:
        n = upsert_rows(rows, source=SOURCE)
    except Exception as e:
        logger.error(f"[openai_costs] upsert failed: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    result.rows = rows
    record_fetch_run(PROVIDER, window_start, window_end, "ok", n, run_started)
    logger.info(f"[openai_costs] upserted {n} rows for {window_start}..{window_end}")
    return result
