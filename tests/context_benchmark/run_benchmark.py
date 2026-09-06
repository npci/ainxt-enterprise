# SPDX-License-Identifier: MIT
"""Runner: execute the benchmark and print a side-by-side report.

Usage (from repo root):
    python -m tests.context_benchmark.run_benchmark
    python -m tests.context_benchmark.run_benchmark --model gpt-5 --json out.json

Deterministic and offline — safe to run in CI. Exit code is 0 unless
--gate is passed and a strategy regresses vs. the baseline ('current').
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import Dict, List

from .eval_set import build_cases
from .scorer import StrategyReport, run_strategy
from .strategies import STRATEGIES


def _fmt_report(rep: StrategyReport) -> str:
    return (
        f"  strategy={rep.strategy:<8}  probes={rep.n_probes:<3}  "
        f"omission={rep.omission_rate:<7} staleness={rep.staleness_rate:<7} "
        f"halluc_risk={rep.hallucination_risk:<7} avg_tokens={rep.avg_tokens}"
    )


def _gate(baseline: StrategyReport, candidate: StrategyReport) -> List[str]:
    """Return list of regression messages (empty == pass). Ship gate per §6:
    staleness <= baseline AND omission <= baseline AND hallucination <= baseline.
    Cost/latency reported but NOT gating (completeness-first)."""
    fails = []
    if candidate.staleness_rate > baseline.staleness_rate:
        fails.append(f"staleness {candidate.staleness_rate} > baseline {baseline.staleness_rate}")
    if candidate.omission_rate > baseline.omission_rate:
        fails.append(f"omission {candidate.omission_rate} > baseline {baseline.omission_rate}")
    if candidate.hallucination_risk > baseline.hallucination_risk:
        fails.append(f"hallucination {candidate.hallucination_risk} > baseline {baseline.hallucination_risk}")
    return fails


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Chat-context benchmark (C0/C1).")
    ap.add_argument("--model", default="claude",
                    help="model hint for window sizing (claude|gpt-5|gemini|local...)")
    ap.add_argument("--strategies", default=",".join(STRATEGIES),
                    help="comma-separated strategy names to run")
    ap.add_argument("--json", default="", help="write full results to this JSON path")
    ap.add_argument("--gate", action="store_true",
                    help="exit non-zero if any candidate regresses vs 'current'")
    args = ap.parse_args(argv)

    cases = build_cases()
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]
    reports: Dict[str, StrategyReport] = {}

    print(f"\nContext benchmark — model_hint={args.model}, "
          f"{len(cases)} cases, "
          f"{sum(len(c.probes) for c in cases)} probes\n")
    for name in names:
        if name not in STRATEGIES:
            print(f"  [skip] unknown strategy '{name}'")
            continue
        rep = run_strategy(name, cases, model_hint=args.model)
        reports[name] = rep
        print(_fmt_report(rep))

    # Ship-gate comparison against baseline 'current'.
    exit_code = 0
    if "current" in reports:
        base = reports["current"]
        for name, rep in reports.items():
            if name == "current":
                continue
            fails = _gate(base, rep)
            if fails:
                print(f"\n  GATE FAIL [{name}] vs current: " + "; ".join(fails))
                if args.gate:
                    exit_code = 1
            else:
                print(f"\n  GATE PASS [{name}] vs current "
                      f"(staleness/omission/hallucination all <= baseline)")

    if args.json:
        payload = {name: asdict(rep) for name, rep in reports.items()}
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\n  wrote {args.json}")

    print()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
