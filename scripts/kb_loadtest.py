#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""
KB load-test harness — Phase 6 verification gate.

Fires N concurrent /ask requests scoped to a single product+version and prints
a coarse latency histogram. Intended as a manual smoke test; production load
testing should use locust/k6 against the live gateway.

Usage:
    python scripts/kb_loadtest.py \\
        --base http://localhost:8000 \\
        --token "$JWT" \\
        --product-id <UUID> \\
        --spec-version v3 \\
        --question "How does settlement handle reversal?" \\
        --users 2000 \\
        --concurrency 100
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx


def _one(client: httpx.Client, path: str, payload: dict, headers: dict) -> tuple[int, float]:
    start = time.perf_counter()
    try:
        r = client.post(path, json=payload, headers=headers, timeout=120.0)
        return r.status_code, time.perf_counter() - start
    except Exception:
        return 0, time.perf_counter() - start


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base",        required=True)
    parser.add_argument("--token",       required=True)
    parser.add_argument("--product-id",  required=True)
    parser.add_argument("--spec-version", default="")
    parser.add_argument("--question",    required=True)
    parser.add_argument("--users",       type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--path",        default="/ask/submit")
    args = parser.parse_args()

    payload = {
        "question":     args.question,
        "scope":        {
            "product_id":   args.product_id,
            "spec_version": args.spec_version or None,
        },
    }
    headers = {"Authorization": f"Bearer {args.token}"}

    statuses: list[int] = []
    latencies: list[float] = []

    with httpx.Client(base_url=args.base) as client:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futs = [pool.submit(_one, client, args.path, payload, headers)
                    for _ in range(args.users)]
            for f in as_completed(futs):
                status, latency = f.result()
                statuses.append(status)
                latencies.append(latency)

    latencies.sort()
    n = len(latencies)
    def pct(p: float) -> float:
        return latencies[min(n - 1, int(p * n))]

    print(f"users={n} concurrency={args.concurrency}")
    print(f"  2xx={sum(1 for s in statuses if 200 <= s < 300)}")
    print(f"  4xx={sum(1 for s in statuses if 400 <= s < 500)}  (incl 503 back-pressure)")
    print(f"  5xx={sum(1 for s in statuses if 500 <= s < 600)}")
    print(f"  err={sum(1 for s in statuses if s == 0)}")
    print(f"  latency p50={pct(0.50):.2f}s  p95={pct(0.95):.2f}s  p99={pct(0.99):.2f}s")
    print(f"  latency mean={statistics.mean(latencies):.2f}s  max={max(latencies):.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
