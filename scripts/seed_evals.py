#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
seed_evals.py — Seed 20 demo eval queries against the AiNxt AI platform.

Usage:
    PLATFORM_BASE_URL=http://localhost:8000 SEED_ADMIN_PASSWORD=... \
        python scripts/seed_evals.py

Steps:
    1. POST /auth/login  → obtain JWT
    2. POST /ask         → send 20 realistic AiNxt engineering queries
    3. GET  /evals/summary → print eval results

Env vars:
    PLATFORM_BASE_URL   Gateway base URL (required, no default) — same var
                        used across the platform (core/config.py, auth/sso.py,
                        etc.) for the public-facing gateway URL.
    SEED_ADMIN_EMAIL    Admin login email (default: admin@ainxt.local)
    SEED_ADMIN_PASSWORD Admin login password (required, no default)
"""

import sys
import os
import json
import time
import urllib.request
import urllib.error

# Allow running from the repo root without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# No fallback literal: an unset PLATFORM_BASE_URL fails fast below rather than silently posting
# real-looking traffic at a hardcoded localhost port that may not even be the
# instance the caller meant to seed.
BASE_URL = os.environ.get("PLATFORM_BASE_URL", "")
LOGIN_EMAIL    = os.environ.get("SEED_ADMIN_EMAIL",    "admin@ainxt.local")
# No fallback literal: the admin password is either set in .env or generated at
# first boot, so there is nothing safe to guess here. Fail with a clear message
# rather than silently attempting a login that cannot succeed.
LOGIN_PASSWORD = os.environ.get("SEED_ADMIN_PASSWORD", "")
DELAY_BETWEEN_REQUESTS = 1.0   # seconds — avoid overwhelming the dev server

# ─────────────────────────────────────────────────────────────────────────────
# 20 realistic AiNxt engineering queries covering a wide range of categories
# ─────────────────────────────────────────────────────────────────────────────
QUERIES = [
    # UPI / Payments domain
    "What is the UPI transaction flow from initiation to settlement?",
    "Explain the difference between UPI pull and push transactions.",
    "What is NACH mandate processing and how does it work?",
    "How does IMPS differ from NEFT and RTGS in terms of settlement time?",
    "What are the PCI-DSS requirements for storing card data in a payment system?",

    # Engineering / Code review
    (
        "Review this Java code for security issues: "
        "public void processPayment(String cardNum) { "
        "System.out.println(\"Card: \" + cardNum); }"
    ),
    "How do I implement idempotency in a payment API to prevent duplicate charges?",
    "What design pattern should I use for a payment state machine?",
    "How do I write a Java unit test for a UPI transaction service using JUnit 5?",
    "What are the best practices for handling database transactions in a payment microservice?",

    # Jira / Project management
    "How do I create a Jira issue for a payment gateway timeout bug?",
    "What fields are required when creating a Critical priority Jira bug for a prod incident?",
    "How should I structure a Jira epic for the NACH auto-debit feature rollout?",

    # GitLab / SCM
    "What is the GitLab merge request review process for AiNxt payment services?",
    "How do I create a GitLab branch for a hotfix and raise an MR targeting main?",

    # Architecture / Platform
    "What is the role of the orchestrator in the AiNxt AI platform?",
    "How does the hybrid retriever combine pgvector and BM25 search results?",
    "Explain the RQ worker queue architecture and how chat responses are streamed to users.",
    "What compliance checks run on every LLM input and output in the platform?",

    # Incident / SRE
    "What steps should I follow to diagnose a high latency spike in the settlements API?",
]


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

def _post(path: str, payload: dict, token: str = None) -> dict:
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        import contextlib
        with contextlib.closing(urllib.request.urlopen(req, timeout=60)) as _conn:
            body = _conn.read().decode(errors="replace")
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {"_raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [HTTP {e.code}] {path} → {body[:200]}")
        return {"error": e.code, "_raw": body}
    except Exception:  # noqa: BLE001
        print(f"  [ERROR] {path} → request failed")
        return {"error": "request failed"}


def _get(path: str, token: str = None) -> dict:
    url = f"{BASE_URL}{path}"
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        import contextlib
        with contextlib.closing(urllib.request.urlopen(req, timeout=30)) as _conn:
            body = _conn.read().decode(errors="replace")
        try:
            return json.loads(body)
        except Exception:  # noqa: BLE001
            return {"_raw": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"  [HTTP {e.code}] {path} → {body[:200]}")
        return {"error": e.code, "_raw": body}
    except Exception:  # noqa: BLE001
        print(f"  [ERROR] {path} → request failed")
        return {"error": "request failed"}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("AiNxt AI Platform — Eval Seed Script")
    print("=" * 60)

    # ── Step 0: Validate config ────────────────────────────────
    if not BASE_URL:
        print(
            "  ERROR: PLATFORM_BASE_URL is not set.\n"
            "  Set it to the gateway URL for the deployment you want to seed,\n"
            "  e.g. PLATFORM_BASE_URL=http://localhost:8000"
        )
        sys.exit(1)

    # ── Step 1: Login ──────────────────────────────────────────
    if not LOGIN_PASSWORD:
        print(
            "  ERROR: SEED_ADMIN_PASSWORD is not set.\n"
            "  Set it to the admin password for this deployment — either the value\n"
            "  from your .env, or the one printed once when the admin was seeded."
        )
        sys.exit(1)

    print(f"\n[1/3] Logging in as {LOGIN_EMAIL} ...")
    login_resp = _post("/auth/login", {"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD})
    token = login_resp.get("access_token") or login_resp.get("token")
    if not token:
        print(f"  ERROR: Could not obtain token. Response: {login_resp}")
        sys.exit(1)
    print(f"  OK — token acquired (first 20 chars): {token[:20]}...")

    # ── Step 2: Send 20 queries ────────────────────────────────
    print(f"\n[2/3] Sending {len(QUERIES)} eval queries to /ask ...")
    success_count = 0
    fail_count = 0

    for i, query in enumerate(QUERIES, start=1):
        display = query if len(query) <= 80 else query[:77] + "..."
        print(f"  [{i:02d}/{len(QUERIES)}] {display}")
        resp = _post(
            "/ask",
            {
                "message": query,
                "session_id": f"seed-evals-session-{i}",
                "stream": False,
            },
            token=token,
        )
        if resp.get("error"):
            fail_count += 1
            print(f"         FAIL → {resp.get('error')}")
        else:
            success_count += 1
            answer_snippet = str(resp.get("answer") or resp.get("response") or "")[:80]
            print(f"         OK   → {answer_snippet or '(no answer field)'}")

        if i < len(QUERIES):
            time.sleep(DELAY_BETWEEN_REQUESTS)

    print(f"\n  Sent: {len(QUERIES)}  |  Success: {success_count}  |  Failed: {fail_count}")

    # ── Step 3: Fetch eval summary ─────────────────────────────
    print("\n[3/3] Fetching eval summary from /evals/summary ...")
    summary = _get("/evals/summary", token=token)

    if summary.get("error"):
        print(f"  WARNING: Could not fetch eval summary → {summary.get('error')}")
        print("  (Eval engine may not be running or /evals/summary not yet implemented)")
    else:
        print("\n  ── Eval Summary ─────────────────────────────────────")
        _pretty_print(summary)

    print("\n" + "=" * 60)
    print("Seed complete.")
    print("=" * 60)


def _pretty_print(obj, indent: int = 2):
    """Recursively print a dict/list in a readable format."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                print(f"{'  ' * indent}{k}:")
                _pretty_print(v, indent + 1)
            else:
                print(f"{'  ' * indent}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                _pretty_print(item, indent)
            else:
                print(f"{'  ' * indent}- {item}")
    else:
        print(f"{'  ' * indent}{obj}")


if __name__ == "__main__":
    main()
