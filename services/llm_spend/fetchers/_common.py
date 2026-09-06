# SPDX-License-Identifier: MIT
# ============================================================
# services.llm_spend.fetchers._common
#
# Shared helpers for all fetchers:
#   * FetchResult dataclass
#   * UPSERT helper for llm_spend_daily (PG ON CONFLICT)
#   * fetch_run row writer
# ============================================================

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, List, Optional

import requests
from sqlalchemy import text

from core.logger import logger
from db.database import SessionLocal


# ── token_type vocabulary ───────────────────────────────────────────────────
#
# Canonical values written by the itemising fetchers (anthropic_admin,
# openai_costs). no DB-level CHECK constraint is enforced so a new
# provider token class shows up as a visible new row rather than a silent
# fetch failure, but every fetcher MUST use one of these strings.
TOKEN_TYPE_UNCACHED:       str = "uncached"        # full-rate input (cache miss)
TOKEN_TYPE_CACHE_READ:     str = "cache_read"      # cache hit, ~0.1x uncached
TOKEN_TYPE_CACHE_WRITE_5M: str = "cache_write_5m"  # 5-minute ephemeral write, ~1.25x
TOKEN_TYPE_CACHE_WRITE_1H: str = "cache_write_1h"  # 1-hour ephemeral write, ~2x
TOKEN_TYPE_OUTPUT:         str = "output"          # generated tokens
TOKEN_TYPE_NON_TOKEN:      str = "non_token"       # cost with no token semantics
                                               # (Veo/Imagen/Lyria/Agent
                                               # Platform/grounding). Exclude
                                               # from per-token rate/savings.
TOKEN_TYPE_BLENDED:        str = "blended"         # not itemisable — pre-migration
                                               # rows and CSV-sourced Gemini.
                                               # Exclude from cache-savings
                                               # maths (no sibling 'uncached'
                                               # row to derive a rate from).


@dataclass
class SpendRow:
    usage_date:    date
    provider:      str
    model:         str
    token_type:    str     = TOKEN_TYPE_BLENDED
    cost_usd:      Decimal = Decimal("0")
    input_tokens:  int     = 0
    output_tokens: int     = 0
    request_count: int     = 0


@dataclass
class FetchResult:
    provider:     str
    source:       str
    window_start: date
    window_end:   date
    rows:         List[SpendRow] = field(default_factory=list)
    status:       str = "ok"          # 'ok' | 'partial' | 'failed'
    error_text:   Optional[str] = None


# ── PG upsert ──────────────────────────────────────────────────────────────
#
# Keyed on (usage_date, provider, model, source, token_type) — the rekeyed
# constraint. Itemising
# fetchers (anthropic_admin, openai_costs) now emit multiple rows per
# (day, model): one per token_type. Each upserts independently rather than
# collapsing onto a single blended row.

_UPSERT_SQL = text(
    """
    INSERT INTO ainxt.llm_spend_daily
        (usage_date, provider, model, token_type, cost_usd,
         input_tokens, output_tokens, request_count, source, fetched_at)
    VALUES
        (:usage_date, :provider, :model, :token_type, :cost_usd,
         :input_tokens, :output_tokens, :request_count, :source, NOW())
    ON CONFLICT (usage_date, provider, model, source, token_type) DO UPDATE
       SET cost_usd      = EXCLUDED.cost_usd,
           input_tokens  = EXCLUDED.input_tokens,
           output_tokens = EXCLUDED.output_tokens,
           request_count = EXCLUDED.request_count,
           fetched_at    = NOW()
    """
)


def _row_payload(rows: Iterable[SpendRow], source: str) -> List[dict]:
    return [
        {
            "usage_date":    r.usage_date,
            "provider":      r.provider,
            "model":         r.model,
            "token_type":    r.token_type,
            "cost_usd":      r.cost_usd,
            "input_tokens":  r.input_tokens,
            "output_tokens": r.output_tokens,
            "request_count": r.request_count,
            "source":        source,
        }
        for r in rows
    ]


def upsert_rows_in_session(session, rows: Iterable[SpendRow], source: str) -> int:
    """Upsert SpendRows using a caller-supplied session (no commit here).

    Lets a caller wrap this alongside other statements — e.g. the
    per-provider transactional backfill (services/llm_spend/backfill.py)
    that must DELETE legacy blended rows for a source and INSERT the
    itemised replacement inside a single atomic transaction. The caller owns
    commit/rollback.
    """
    payload = _row_payload(rows, source)
    if not payload:
        return 0
    session.execute(_UPSERT_SQL, payload)
    return len(payload)


def upsert_rows(rows: Iterable[SpendRow], source: str) -> int:
    """Bulk-upsert SpendRows in their own committed transaction.

    Returns count of rows written. For a caller that needs the upsert to
    share a transaction with other statements, use upsert_rows_in_session.
    """
    payload = _row_payload(rows, source)
    if not payload:
        return 0
    with SessionLocal() as session:
        session.execute(_UPSERT_SQL, payload)
        session.commit()
    return len(payload)


# ── fetch_run audit ────────────────────────────────────────────────────────

_RUN_INSERT_SQL = text(
    """
    INSERT INTO ainxt.llm_spend_fetch_runs
        (run_started, run_finished, provider, window_start, window_end,
         status, rows_upserted, error_text)
    VALUES
        (:run_started, NOW(), :provider, :window_start, :window_end,
         :status, :rows_upserted, :error_text)
    """
)


# ── llm_proxy egress (the LLM proxy server) ──────────────────────────────────────────────
#
# the gateway has no outbound internet — every external cost-API call must hop
# through services/llm_proxy on the LLM proxy server. The admin credentials
# (ANTHROPIC_ADMIN_API_KEY, OPENAI_ADMIN_API_KEY, GCP_BILLING_SA_JSON) live
# only on the LLM proxy server; the gateway carries just LLM_PROXY_URL + LLM_PROXY_TOKEN.

_PROXY_HTTP_TIMEOUT = 60
_PROXY_MAX_RETRIES  = 3


def _proxy_post(path: str, body: dict, timeout: Optional[int] = None) -> dict:
    """POST to llm_proxy on the LLM proxy server and return parsed JSON.

    Reads LLM_PROXY_URL + LLM_PROXY_TOKEN from env. Raises RuntimeError if
    LLM_PROXY_URL is unset (misconfiguration must surface immediately rather
    than silently fall back to direct internet — the gateway has none). Raises
    requests.HTTPError on 4xx/5xx so callers see the real upstream status.
    Retries transient 5xx, 429, and connection errors up to _PROXY_MAX_RETRIES.

    `timeout` overrides the default per-request HTTP timeout. The GCP
    BigQuery billing-export path passes a longer value (the BQ scan can
    exceed the 60s default for wide windows); the OpenAI/Anthropic admin
    calls keep the default.
    """
    base = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "LLM_PROXY_URL not set — cannot reach llm_spend egress proxy on the LLM proxy server"
        )
    token = os.getenv("LLM_PROXY_TOKEN", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Internal-Token"] = token

    http_timeout = timeout if timeout is not None else _PROXY_HTTP_TIMEOUT

    last_exc: Optional[Exception] = None
    for attempt in range(_PROXY_MAX_RETRIES):
        try:
            r = requests.post(
                f"{base}{path}",
                json=body,
                headers=headers,
                timeout=http_timeout,
            )
            if (r.status_code == 429 or r.status_code >= 500) and attempt < _PROXY_MAX_RETRIES - 1:
                retry_after = r.headers.get("Retry-After")
                try:
                    sleep_s = int(retry_after) if retry_after is not None else min(2 ** attempt, 10)
                except (TypeError, ValueError):
                    sleep_s = min(2 ** attempt, 10)
                sleep_s = max(0, min(sleep_s, 30))   # clamp so a huge header can't stall the fetch
                logger.warning(
                    f"[llm_spend] proxy {path} → {r.status_code}; "
                    f"retry {attempt + 1}/{_PROXY_MAX_RETRIES} after {sleep_s}s"
                )
                time.sleep(sleep_s)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last_exc = e
            if attempt < _PROXY_MAX_RETRIES - 1:
                logger.warning(
                    f"[llm_spend] proxy {path} transport error: {e}; "
                    f"retry {attempt + 1}/{_PROXY_MAX_RETRIES}"
                )
                time.sleep(min(2 ** attempt, 10))
                continue
            raise
    # Defensive — the loop above either returns or raises.
    if last_exc:
        raise last_exc
    raise RuntimeError(f"[llm_spend] proxy {path} failed without exception")


def record_fetch_run(
    provider:     str,
    window_start: date,
    window_end:   date,
    status:       str,
    rows_upserted: int,
    run_started:  datetime,
    error_text:   Optional[str] = None,
) -> None:
    try:
        with SessionLocal() as session:
            session.execute(
                _RUN_INSERT_SQL,
                {
                    "run_started":   run_started,
                    "provider":      provider,
                    "window_start":  window_start,
                    "window_end":    window_end,
                    "status":        status,
                    "rows_upserted": rows_upserted,
                    "error_text":    error_text,
                },
            )
            session.commit()
    except Exception as e:
        logger.error(f"[llm_spend] failed to record fetch_run for {provider}: {e}")
