# SPDX-License-Identifier: Apache-2.0
"""Loop Engineering subsystem.

Shipped surface (post-consolidation) — three modules only:

* ``models``    — Pydantic shapes mirroring loops_pg / goals / loop_runs /
                  reflections / verification_gate_runs / inbox tables.
* ``repo``      — CRUD helpers (re-uses workflow_repo's pool).
* ``runner``    — outer-loop dispatcher (LoopRunner) + all P2..P5 primitives:
                  BudgetMeter, LLM-judge helper, ProofEvaluator,
                  ComprehensionDigest, VerifierAgent, memory handlers,
                  ReflectionWriter, TriageSkill, and their shared prompt
                  strings.

The primitives were split across a dozen files during development for
review-time diffability; the shipped form co-locates them so the loop
package presents only the surface listed above. Section banners inside
``runner.py`` mark the original file boundaries so operators can still
grep for the historical names.

Keep this docstring up to date if new modules land — it is the index
the rest of the team will read first.
"""
