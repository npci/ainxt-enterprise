# SPDX-License-Identifier: Apache-2.0
# ============================================================
# P18 (E1) — eval harness: probe grammar + judge + gate (pure)
# ============================================================

from evals.harness import (
    ABSTENTION,
    OVERRIDE,
    RECALL,
    Probe,
    gate,
    run_suite,
)


def test_deterministic_pass_and_fail():
    probes = [
        Probe(id="p1", kind=RECALL, inputs="budget", expected="5000"),
        Probe(id="p2", kind=OVERRIDE, inputs="latest", expected="python"),
    ]
    # decide returns correct for p1, wrong for p2
    table = {"budget": "5000", "latest": "java"}
    suite = run_suite("mem", probes, lambda i: table[i])
    assert suite.total == 2
    assert suite.passed == 1
    assert suite.pass_rate() == 0.5


def test_broken_probe_counts_as_fail_not_crash():
    probes = [Probe(id="boom", kind=RECALL, inputs="x", expected="y")]
    suite = run_suite("s", probes, lambda i: 1 / 0)  # raises
    assert suite.total == 1 and suite.passed == 0
    assert suite.results[0].error  # error recorded


def test_by_kind_breakdown():
    probes = [
        Probe(id="a", kind=RECALL, inputs=1, expected=1),
        Probe(id="b", kind=RECALL, inputs=2, expected=99),
        Probe(id="c", kind=ABSTENTION, inputs=3, expected=3),
    ]
    suite = run_suite("s", probes, lambda i: i)
    bk = suite.by_kind()
    assert bk[RECALL] == 0.5
    assert bk[ABSTENTION] == 1.0


def test_custom_judge():
    # judge: got contains expected substring
    probes = [Probe(id="x", kind=RECALL, inputs="q", expected="cat")]
    suite = run_suite("s", probes, lambda i: "the cat sat",
                      judge=lambda got, exp: exp in got)
    assert suite.passed == 1


def test_gate_passes_when_no_worse():
    baseline = {"pass_rate": 0.8, "omission": 0.1}
    current = {"pass_rate": 0.85, "omission": 0.1}  # higher pass, equal omission
    g = gate(current, baseline)
    assert g.passed is True
    assert g.regressions == []


def test_gate_fails_on_regression_ge_metric():
    baseline = {"pass_rate": 0.8}
    current = {"pass_rate": 0.7}   # lower is worse for pass_rate
    g = gate(current, baseline)
    assert g.passed is False
    assert any("pass_rate" in r for r in g.regressions)


def test_gate_fails_on_regression_le_metric():
    baseline = {"omission": 0.0}
    current = {"omission": 0.17}   # the real context-benchmark regression story
    g = gate(current, baseline)
    assert g.passed is False
    assert any("omission" in r for r in g.regressions)


def test_gate_ignores_metrics_absent_from_baseline():
    g = gate({"new_metric": 0.1, "pass_rate": 0.9}, {"pass_rate": 0.9})
    assert g.passed is True


def test_gate_direction_override():
    # cost lower is better via default; verify a custom direction override works
    g = gate({"score": 0.5}, {"score": 0.9}, directions={"score": "le"})
    assert g.passed is True  # 0.5 <= 0.9 ok for a "le" metric
