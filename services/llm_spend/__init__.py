# SPDX-License-Identifier: Apache-2.0
# ============================================================
# services.llm_spend — enterprise LLM spend tracking.
#
# Public surface (imported by routers/llm_spend_report_router.py and by
# gateway.py for scheduled jobs):
#
#   from services.llm_spend.orchestrator import (
#       run_daily_fetch,
#       send_daily_digest,
#       send_weekly_digest,
#       send_monthly_digest,
#       send_quarterly_digest,
#       backfill_if_empty,
#   )
#
# All other modules in this package are implementation detail.
# ============================================================
