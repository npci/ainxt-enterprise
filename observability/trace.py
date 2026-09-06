# SPDX-License-Identifier: MIT
# ============================================================
# Per-turn trace builder — one decision-rich span per stage (pure)
# ============================================================
#
# docs/architecture/19-observability.md §19.3 (O1). Builds the per-turn span tree
# that maps 1:1 to the request lifecycle (§3). Each span records its DECISION
# inputs/outputs, not just timing — because "instrument once, use three ways"
# (debug, evaluate §18, improve §21). Every span is correlated by request_id.
#
# This is a PURE, in-memory model: it assembles the TurnTrace structure and can
# export it as the flat span list an OTEL exporter emits. It does NOT depend on
# core/otel.py, so it stays importable offline and its structure is unit-testable
# independent of the OTLP stack. Fail-safe: instrumentation must never fail a turn,
# so every method swallows errors and the trace degrades to whatever it captured.
# ============================================================

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# canonical lifecycle stages (docs/architecture/19 §19.3, mirrors §3)
STAGES = [
    "ingress", "identity", "cil.analyze", "safety.gates", "context.assemble",
    "retrieval", "routing", "prompt.compile", "generate", "grounding", "persist",
]


@dataclass
class Span:
    name: str
    attrs: Dict[str, Any] = field(default_factory=dict)
    start_ms: float = 0.0
    end_ms: float = 0.0
    parent: Optional[str] = None
    error: str = ""

    @property
    def duration_ms(self) -> float:
        return max(0.0, self.end_ms - self.start_ms) if self.end_ms else 0.0


class TurnTrace:
    """Collects the spans for one turn, all correlated by request_id. Not a
    dataclass because it carries behavior (open/close spans) + a clock."""

    def __init__(self, request_id: str, *, clock=None):
        self.request_id = request_id
        self._clock = clock or (lambda: time.monotonic() * 1000.0)
        self.spans: List[Span] = []
        self._open: Dict[str, Span] = {}

    def start(self, name: str, *, parent: Optional[str] = None, **attrs) -> "TurnTrace":
        """Open a span. Returns self for chaining. Never raises."""
        try:
            sp = Span(name=name, attrs=dict(attrs), start_ms=self._now(), parent=parent)
            self._open[name] = sp
            self.spans.append(sp)
        except Exception:  # noqa: BLE001 — tracing never fails a turn
            pass
        return self

    def set(self, name: str, **attrs) -> "TurnTrace":
        """Add/merge decision attributes onto an (open or closed) span."""
        try:
            sp = self._open.get(name) or self._find(name)
            if sp is not None:
                sp.attrs.update(attrs)
        except Exception:  # noqa: BLE001
            pass
        return self

    def end(self, name: str, *, error: str = "", **attrs) -> "TurnTrace":
        """Close a span, optionally recording final attrs / an error."""
        try:
            sp = self._open.pop(name, None) or self._find(name)
            if sp is not None:
                sp.attrs.update(attrs)
                if error:
                    sp.error = error
                sp.end_ms = self._now()
        except Exception:  # noqa: BLE001
            pass
        return self

    def span(self, name: str, *, parent: Optional[str] = None, **attrs) -> "_SpanCtx":
        """Context manager: `with trace.span("routing", tier="complex"): ...`"""
        return _SpanCtx(self, name, parent, attrs)

    # ── export / query ────────────────────────────────────────────────
    def total_ms(self) -> float:
        try:
            starts = [s.start_ms for s in self.spans if s.start_ms]
            ends = [s.end_ms for s in self.spans if s.end_ms]
            return round(max(ends) - min(starts), 3) if starts and ends else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    def get(self, name: str) -> Optional[Span]:
        return self._find(name)

    def to_spans(self) -> List[Dict[str, Any]]:
        """Flat OTEL-style export: one dict per span, all carrying request_id."""
        out = []
        for s in self.spans:
            out.append({
                "request_id": self.request_id,
                "name": s.name,
                "parent": s.parent,
                "duration_ms": round(s.duration_ms, 3),
                "error": s.error,
                **{f"attr.{k}": v for k, v in s.attrs.items()},
            })
        return out

    def eval_attrs(self) -> Dict[str, Any]:
        """Flatten decision attributes for the eval harness (§18) / evolution
        (§21): {span.attr: value}. This is the "instrument once, use three ways"
        substrate."""
        flat: Dict[str, Any] = {}
        for s in self.spans:
            for k, v in s.attrs.items():
                flat[f"{s.name}.{k}"] = v
        return flat

    # ── internals ─────────────────────────────────────────────────────
    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:  # noqa: BLE001
            return 0.0

    def _find(self, name: str) -> Optional[Span]:
        for s in reversed(self.spans):  # most-recent wins if reused
            if s.name == name:
                return s
        return None


class _SpanCtx:
    def __init__(self, trace: TurnTrace, name: str, parent, attrs):
        self._t, self._n, self._p, self._a = trace, name, parent, attrs

    def __enter__(self) -> TurnTrace:
        self._t.start(self._n, parent=self._p, **self._a)
        return self._t

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._t.end(self._n, error=str(exc) if exc else "")
        return False  # never suppress the real exception
