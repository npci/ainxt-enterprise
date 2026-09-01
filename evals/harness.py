# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Eval harness — probe grammar + deterministic judge + baseline gate (pure)
# ============================================================
#
# docs/architecture/18-evaluation.md §18.4-18.7 (E1). Generalizes the context
# benchmark's probe methodology into ONE reusable harness any subsystem suite can
# use, so gates are consistent and mostly deterministic (L1/L2 of the pyramid).
#
# Three pieces:
#   1. Probe grammar — the six probe types (recall/override/distractor/boundary/
#      adversarial/abstention). A probe passes on a DECISION, not on prose.       (§18.4)
#   2. run_suite() — executes probes through a caller-supplied decision fn,
#      aggregates per-type pass rates. Deterministic-first judging.               (§18.5)
#   3. gate() — compares a run to a recorded baseline with per-metric direction
#      (≥ or ≤), returning pass/fail + regressions. This is the CI gate.          (§18.7)
#
# Pure stdlib. Evaluation only GATES; it never changes runtime behavior (§18.8),
# so this cannot break production. Fail-safe: a probe that raises counts as a
# FAIL (never crashes the run).
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# probe types (docs/architecture/18 §18.4)
RECALL = "recall"
OVERRIDE = "override"
DISTRACTOR = "distractor"
BOUNDARY = "boundary"
ADVERSARIAL = "adversarial"
ABSTENTION = "abstention"

PROBE_TYPES = [RECALL, OVERRIDE, DISTRACTOR, BOUNDARY, ADVERSARIAL, ABSTENTION]


@dataclass
class Probe:
    """One test case. `inputs` is passed to the decision fn; `expected` is the
    decision the layer under test should make (present fact, chosen tier, etc.)."""

    id: str
    kind: str                       # one of PROBE_TYPES
    inputs: Any = None
    expected: Any = None


@dataclass
class ProbeResult:
    id: str
    kind: str
    passed: bool
    got: Any = None
    error: str = ""


@dataclass
class SuiteResult:
    name: str
    results: List[ProbeResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    def by_kind(self) -> Dict[str, float]:
        """Per-probe-type pass rate."""
        buckets: Dict[str, List[bool]] = {}
        for r in self.results:
            buckets.setdefault(r.kind, []).append(r.passed)
        return {k: (sum(v) / len(v) if v else 0.0) for k, v in buckets.items()}

    def metrics(self) -> Dict[str, float]:
        """Flat metric dict for gate() comparison against a baseline."""
        m = {"pass_rate": round(self.pass_rate(), 4)}
        for k, v in self.by_kind().items():
            m[f"pass_rate.{k}"] = round(v, 4)
        return m


def run_suite(
    name: str,
    probes: List[Probe],
    decide: Callable[[Any], Any],
    *,
    judge: Optional[Callable[[Any, Any], bool]] = None,
) -> SuiteResult:
    """Run each probe's inputs through `decide`, judge got-vs-expected. Default
    judge is equality (deterministic, §18.5). A probe that raises counts as a
    FAIL, never crashing the run."""
    _judge = judge or (lambda got, exp: got == exp)
    suite = SuiteResult(name=name)
    for p in probes or []:
        try:
            got = decide(p.inputs)
            ok = bool(_judge(got, p.expected))
            suite.results.append(ProbeResult(id=p.id, kind=p.kind, passed=ok, got=got))
        except Exception as e:  # noqa: BLE001 — a broken probe is a fail, not a crash
            suite.results.append(ProbeResult(id=p.id, kind=p.kind, passed=False,
                                             error=str(e)))
    return suite


@dataclass
class GateResult:
    passed: bool
    regressions: List[str] = field(default_factory=list)
    deltas: Dict[str, float] = field(default_factory=dict)


# metric name → comparison direction. "ge" = higher is better (must be ≥ baseline);
# "le" = lower is better (must be ≤ baseline). Unknown metrics default to "ge".
_DEFAULT_DIRECTIONS = {
    "pass_rate": "ge",
    "omission": "le",
    "staleness": "le",
    "hallucination": "le",
    "false_block": "le",
    "cost_per_turn": "le",
    "privacy_violations": "le",
}


def _direction(metric: str, directions: Dict[str, str]) -> str:
    if metric in directions:
        return directions[metric]
    base = metric.split(".", 1)[0]
    return directions.get(base, "ge")


def gate(
    current: Dict[str, float],
    baseline: Dict[str, float],
    *,
    directions: Optional[Dict[str, str]] = None,
    tolerance: float = 1e-9,
) -> GateResult:
    """Compare current metrics to a recorded baseline (§18.7). A change passes
    only if it is provably no worse on every shared metric, per that metric's
    direction. Metrics absent from baseline are ignored (new, no baseline yet)."""
    dirs = {**_DEFAULT_DIRECTIONS, **(directions or {})}
    res = GateResult(passed=True)
    for metric, base_val in (baseline or {}).items():
        if metric not in (current or {}):
            continue
        cur_val = current[metric]
        res.deltas[metric] = round(cur_val - base_val, 6)
        d = _direction(metric, dirs)
        worse = (cur_val < base_val - tolerance) if d == "ge" else (cur_val > base_val + tolerance)
        if worse:
            res.passed = False
            arrow = "≥" if d == "ge" else "≤"
            res.regressions.append(f"{metric}: {cur_val} not {arrow} baseline {base_val}")
    return res
