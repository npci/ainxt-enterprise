# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.fetchers.anthropic_admin
#
# Anthropic Admin API (routed via llm_proxy on the LLM proxy server):
#
#   POST ${LLM_PROXY_URL}/spend/anthropic/cost_report
#   POST ${LLM_PROXY_URL}/spend/anthropic/usage_report
#       body: {"params": {starting_at, ending_at, bucket_width, group_by, limit, page?}}
#
# Proxy maps these onto:
#   GET https://api.anthropic.com/v1/organizations/cost_report
#   GET https://api.anthropic.com/v1/organizations/usage_report/messages
#
# Auth: handled on the LLM proxy server (ANTHROPIC_ADMIN_API_KEY). The gateway carries no
# Anthropic admin credentials. Rate-limit (~50 req/min) 429 retries also
# happen on the LLM proxy server; this client retries transport / 5xx via _proxy_post.
#
# TOKEN_TYPE ITEMISATION
#   Both endpoints already carry the cache breakdown; this fetcher used to
#   discard it:
#     * /cost_report results carry a structured `token_type` field per line
#       even when grouped by description — e.g.
#       "cache_creation.ephemeral_5m_input_tokens" — plus `context_window`
#       ("0-200k" | "200k-1M"). We fold the two context tiers together (cost is
#       itemised by token_type, not by context tier) and key on
#       (day, model, token_type).
#     * /usage_report returns ONE result object per (day, model) with all
#       token classes as sibling fields: `uncached_input_tokens`,
#       `cache_read_input_tokens`, `cache_creation.{ephemeral_5m,
#       ephemeral_1h}_input_tokens`, `output_tokens`. We fan that single
#       object out into up to 5 (day, model, token_type) rows.
#   request_count has no natural per-token_type home (it's a per-model-per-day
#   figure) — it is written ONLY on the 'output' row so SUM(request_count)
#   stays correct instead of being N x inflated across N token_type rows.
# ============================================================

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Tuple

from core.logger import logger
from services.llm_spend.approved_models import get_approved_models
from services.llm_spend.fetchers._common import (
    FetchResult, SpendRow, record_fetch_run, upsert_rows, _proxy_post,
    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ, TOKEN_TYPE_CACHE_WRITE_5M,
    TOKEN_TYPE_CACHE_WRITE_1H, TOKEN_TYPE_OUTPUT, TOKEN_TYPE_NON_TOKEN,
)


SOURCE = "anthropic_admin"
PROVIDER = "anthropic"

# Maps the Anthropic admin API's `token_type` string (on /cost_report result
# lines) to our canonical vocabulary. Anything unmapped — including a future
# token class Anthropic adds without notice — falls to TOKEN_TYPE_NON_TOKEN
# rather than raising, so a schema surprise costs a visible new row instead
# of a crashed nightly fetch. _COST_TOKEN_TYPE_MAP misses are logged once per
# distinct value per process so the gap is noticed and this map can be
# extended.
_COST_TOKEN_TYPE_MAP = {
    "uncached_input_tokens":                     TOKEN_TYPE_UNCACHED,
    "cache_read_input_tokens":                   TOKEN_TYPE_CACHE_READ,
    "cache_creation.ephemeral_5m_input_tokens":  TOKEN_TYPE_CACHE_WRITE_5M,
    "cache_creation.ephemeral_1h_input_tokens":  TOKEN_TYPE_CACHE_WRITE_1H,
    "output_tokens":                             TOKEN_TYPE_OUTPUT,
}
_warned_unmapped_token_types: set = set()


def _map_cost_token_type(cost_type: str, raw_token_type: str) -> str:
    # cost_type != "tokens" covers non-token line items (e.g. web search
    # tool use) — no token count applies, cost only.
    if cost_type and cost_type != "tokens":
        return TOKEN_TYPE_NON_TOKEN
    mapped = _COST_TOKEN_TYPE_MAP.get(raw_token_type or "")
    if mapped is None:
        if raw_token_type and raw_token_type not in _warned_unmapped_token_types:
            _warned_unmapped_token_types.add(raw_token_type)
            logger.warning(
                f"[anthropic_admin] unmapped cost token_type={raw_token_type!r} "
                f"— bucketing as {TOKEN_TYPE_NON_TOKEN!r}; extend "
                f"_COST_TOKEN_TYPE_MAP if this is a new billable token class"
            )
        return TOKEN_TYPE_NON_TOKEN
    return mapped


def _paginate_proxy(report: str, params: Dict[str, str]):
    """Yield each page's `data` list from a paginated Anthropic admin report.

    `report` is the suffix the proxy understands: 'cost_report' or 'usage_report'.
    The proxy handles upstream auth + 429 backoff; we only thread `next_page`
    between calls so each /spend/anthropic/* hit fetches exactly one page.
    """
    next_page = None
    while True:
        q = dict(params)
        if next_page:
            q["page"] = next_page
        body = _proxy_post(f"/spend/anthropic/{report}", {"params": q})
        yield body.get("data", [])
        next_page = body.get("next_page")
        if not next_page:
            return


def _fetch_costs(
    window_start: date, window_end: date
) -> Dict[Tuple[date, str, str], Decimal]:
    """Returns {(day, raw_model, token_type): cost_usd}.

    Context tiers (0-200k vs 200k-1M) are folded together here — both tiers
    for the same (day, model, token_type) land in the same accumulator cell.
    """
    # Anthropic admin API quirks for /cost_report:
    #   * `limit` capped at 31 — paginate via next_page for longer windows.
    #   * `group_by` for costs accepts only "description" or "workspace_id"
    #     (NOT "model"). This does not lose the model/token_type breakdown:
    #     Anthropic includes structured `model` and `token_type` fields on
    #     every result line regardless of group_by.
    params = {
        "starting_at":  window_start.isoformat(),
        "ending_at":    (window_end + timedelta(days=1)).isoformat(),
        "bucket_width": "1d",
        "group_by":     "description",
        "limit":        "31",
    }
    out: Dict[Tuple[date, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for page in _paginate_proxy("cost_report", params):
        for bucket in page:
            try:
                day = date.fromisoformat(bucket["starting_at"][:10])
            except Exception:
                continue
            for result in bucket.get("results", []):
                # Prefer the structured `model` field. Only fall back to
                # `description` (a human label like "Claude Opus 4.7 Usage -
                # Output Tokens") if `model` is null — which happens for
                # non-model line items like "Web Search Usage". Those land
                # under model="other" via the approved_models bucket.
                model_id = (result.get("model") or "").strip()
                if not model_id:
                    desc = (result.get("description") or "").strip()
                    model_id = desc or "unknown"

                token_type = _map_cost_token_type(
                    result.get("cost_type") or "", result.get("token_type") or ""
                )

                # Anthropic costs come back as a plain string in `amount`
                # ("851.418955"), not a {value, currency} dict like usage_report.
                # Be tolerant of both shapes.
                #
                # UNIT: the Anthropic admin /cost_report endpoint returns
                # amounts in CENTS (USD × 100), not dollars. The admin
                # console confirms: API full-month totals are ~100× the
                # dashboard figure. Divide by 100 to store USD in
                # llm_spend_daily.cost_usd.
                amount_field = result.get("amount", 0) or 0
                if isinstance(amount_field, dict):
                    value = amount_field.get("value", 0) or 0
                else:
                    value = amount_field
                out[(day, model_id, token_type)] += Decimal(str(value)) / Decimal("100")
    return out


def _fetch_usage(
    window_start: date, window_end: date
) -> Dict[Tuple[date, str, str], Dict[str, int]]:
    """Returns {(day, raw_model, token_type): {tokens, request_count}}.

    /usage_report returns ONE result object per (day, model) holding every
    token class as a sibling field; this fans it out into up to 5 rows keyed
    by canonical token_type. request_count is attached ONLY to the 'output'
    row (see module docstring) so summing across token_type rows doesn't
    inflate request totals.
    """
    # Anthropic admin API caps `limit` at 31 — same constraint as cost_report.
    params = {
        "starting_at":  window_start.isoformat(),
        "ending_at":    (window_end + timedelta(days=1)).isoformat(),
        "bucket_width": "1d",
        "group_by":     "model",
        "limit":        "31",
    }
    out: Dict[Tuple[date, str, str], Dict[str, int]] = defaultdict(
        lambda: {"tokens": 0, "request_count": 0}
    )
    for page in _paginate_proxy("usage_report", params):
        for bucket in page:
            try:
                day = date.fromisoformat(bucket["starting_at"][:10])
            except Exception:
                continue
            for result in bucket.get("results", []):
                model_id = (result.get("model") or "unknown").strip()
                cache_creation = result.get("cache_creation") or {}

                token_counts = {
                    TOKEN_TYPE_UNCACHED:       int(result.get("uncached_input_tokens", 0) or 0),
                    TOKEN_TYPE_CACHE_READ:     int(result.get("cache_read_input_tokens", 0) or 0),
                    TOKEN_TYPE_CACHE_WRITE_5M: int(cache_creation.get("ephemeral_5m_input_tokens", 0) or 0),
                    TOKEN_TYPE_CACHE_WRITE_1H: int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0),
                    TOKEN_TYPE_OUTPUT:         int(result.get("output_tokens", 0) or 0),
                }
                req_count = int(
                    result.get("request_count", result.get("num_requests", 0)) or 0
                )

                for token_type, tokens in token_counts.items():
                    cell = out[(day, model_id, token_type)]
                    cell["tokens"] += tokens
                    if token_type == TOKEN_TYPE_OUTPUT:
                        cell["request_count"] += req_count
    return out


# ── public ─────────────────────────────────────────────────────────────────

def compute_rows(window_start: date, window_end: date) -> List[SpendRow]:
    """Fetch + aggregate [window_start, window_end] into SpendRows, no DB write.

    Split out from fetch_window() so the one-off transactional backfill
    (services/llm_spend/backfill.py) can call the SAME fetch+aggregate logic
    inside its own DELETE-then-INSERT transaction, instead of duplicating
    this bucket-then-sum logic. Raises on any fetch failure — the caller
    decides what "failed" means for its context (fetch_window logs+records a
    fetch_run; the backfill script aborts its transaction).
    """
    approved = get_approved_models()

    costs  = _fetch_costs(window_start, window_end)
    usages = _fetch_usage(window_start, window_end)

    # Bucket-then-sum: /cost_report (group_by=description) and /usage_report
    # (group_by=model) emit different raw keys for the same canonical model
    # (description-parsed model id vs structured model id). Re-key both
    # inputs by (canonical model, token_type) BEFORE merging so costs and
    # tokens for the same canon+type land on a single SpendRow even when the
    # raw-key bucketing diverges (one resolves via prefix match, the other
    # falls to "other").
    costs_canon: Dict[Tuple[date, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for (day, raw_model, token_type), cost in costs.items():
        canon = approved.bucket(PROVIDER, raw_model)
        costs_canon[(day, canon, token_type)] += cost

    usages_canon: Dict[Tuple[date, str, str], Dict[str, int]] = defaultdict(
        lambda: {"tokens": 0, "request_count": 0}
    )
    for (day, raw_model, token_type), u in usages.items():
        canon = approved.bucket(PROVIDER, raw_model)
        cell = usages_canon[(day, canon, token_type)]
        cell["tokens"]        += int(u.get("tokens", 0) or 0)
        cell["request_count"] += int(u.get("request_count", 0) or 0)

    agg: Dict[Tuple[date, str, str], SpendRow] = {}
    for (day, canon, token_type) in set(costs_canon.keys()) | set(usages_canon.keys()):
        u = usages_canon.get((day, canon, token_type), {})
        tokens = int(u.get("tokens", 0) or 0)
        agg[(day, canon, token_type)] = SpendRow(
            usage_date=day,
            provider=PROVIDER,
            model=canon,
            token_type=token_type,
            cost_usd=costs_canon.get((day, canon, token_type), Decimal("0")),
            # input_tokens holds this row's own count for uncached/cache_read
            # types; output_tokens holds it for the output type. Every other
            # type (cache_write_*, non_token) carries cost only — zero tokens
            # in either column, since neither is the right home for a write
            # count and it would otherwise double into input_tokens sums.
            # input_tokens: uncached, cache_read, AND cache_write_* are all
            # input tokens — they belong in this column. cache_write tokens
            # are real tokens written into the prompt cache and are correctly
            # included in provider-level input totals and in the per-model
            # rate computation (cost ÷ input_tokens × 1M gives the write rate).
            # output_tokens holds the output row's count. non_token rows carry
            # cost only (no token semantics — e.g. web search tool use).
            input_tokens=(
                tokens if token_type in (
                    TOKEN_TYPE_UNCACHED, TOKEN_TYPE_CACHE_READ,
                    TOKEN_TYPE_CACHE_WRITE_5M, TOKEN_TYPE_CACHE_WRITE_1H,
                ) else 0
            ),
            output_tokens=tokens if token_type == TOKEN_TYPE_OUTPUT else 0,
            request_count=int(u.get("request_count", 0) or 0),
        )

    return list(agg.values())


def fetch_window(window_start: date, window_end: date) -> FetchResult:
    run_started = datetime.utcnow()
    result = FetchResult(provider=PROVIDER, source=SOURCE,
                         window_start=window_start, window_end=window_end)

    try:
        rows = compute_rows(window_start, window_end)
    except Exception as e:
        logger.error(f"[anthropic_admin] fetch failed: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    try:
        n = upsert_rows(rows, source=SOURCE)
    except Exception as e:
        logger.error(f"[anthropic_admin] upsert failed: {e}")
        result.status = "failed"
        result.error_text = str(e)[:500]
        record_fetch_run(PROVIDER, window_start, window_end,
                         "failed", 0, run_started, str(e)[:500])
        return result

    result.rows = rows
    record_fetch_run(PROVIDER, window_start, window_end, "ok", n, run_started)
    logger.info(f"[anthropic_admin] upserted {n} rows for {window_start}..{window_end}")
    return result
