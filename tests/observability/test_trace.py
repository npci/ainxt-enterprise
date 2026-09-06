# SPDX-License-Identifier: MIT
# ============================================================
# P19 (O1) — per-turn trace builder (pure)
# ============================================================

from observability.trace import STAGES, TurnTrace


class FakeClock:
    """Deterministic monotonic clock in ms."""
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        self.t += 10.0
        return self.t


def test_spans_carry_request_id_and_attrs():
    tr = TurnTrace("req-1", clock=FakeClock())
    tr.start("routing", tier="complex", model="sonnet").end("routing", cost_est=0.01)
    spans = tr.to_spans()
    assert len(spans) == 1
    s = spans[0]
    assert s["request_id"] == "req-1"
    assert s["name"] == "routing"
    assert s["attr.tier"] == "complex"
    assert s["attr.cost_est"] == 0.01


def test_duration_measured_from_clock():
    tr = TurnTrace("r", clock=FakeClock())  # each call +10ms
    tr.start("generate")   # start=10
    tr.end("generate")     # end=20
    assert tr.get("generate").duration_ms == 10.0


def test_context_manager_records_and_closes():
    tr = TurnTrace("r", clock=FakeClock())
    with tr.span("cil.analyze", complexity="medium") as t:
        t.set("cil.analyze", domain="finance")
    sp = tr.get("cil.analyze")
    assert sp.attrs["complexity"] == "medium"
    assert sp.attrs["domain"] == "finance"
    assert sp.end_ms > 0


def test_context_manager_records_error_but_does_not_suppress():
    tr = TurnTrace("r", clock=FakeClock())
    raised = False
    try:
        with tr.span("safety.gates"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised is True                      # exception propagated
    assert "boom" in tr.get("safety.gates").error


def test_eval_attrs_flatten_for_harness():
    tr = TurnTrace("r", clock=FakeClock())
    tr.start("retrieval", coverage=0.78, confidence=0.86).end("retrieval")
    tr.start("grounding", unsupported=0).end("grounding")
    flat = tr.eval_attrs()
    assert flat["retrieval.coverage"] == 0.78
    assert flat["grounding.unsupported"] == 0


def test_full_lifecycle_trace_shape():
    tr = TurnTrace("turn-abc", clock=FakeClock())
    for stage in STAGES:
        tr.start(stage).end(stage)
    assert len(tr.to_spans()) == len(STAGES)
    assert tr.total_ms() > 0


def test_instrumentation_never_raises_on_bad_clock():
    def bad_clock():
        raise RuntimeError("clock down")
    tr = TurnTrace("r", clock=bad_clock)
    tr.start("ingress").end("ingress")   # must not raise
    assert tr.get("ingress") is not None
