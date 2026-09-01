#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# LIVE DOC-GEN BENCHMARK vs Claude / ChatGPT
#
# Opt-in (needs live model access). For each fixture it:
#   1. Runs OUR pipeline (intent classify → structure → refine).
#   2. Asks a strong judge model to score OUR output AND reference output
#      from Claude and GPT on the same rubric.
#   3. Prints a scorecard so we can track the gap to 100%.
#
# Usage:
#   python -m tests.docgen_benchmark.run_benchmark
#   DOCGEN_BENCH_JUDGE=complex python -m tests.docgen_benchmark.run_benchmark
#
# Rubric (0-10 each): structure, faithfulness, completeness, formatting, tone.
# ============================================================

import json
import os
import sys

from tests.docgen_benchmark.fixtures import CASES

_JUDGE_HINT = os.getenv("DOCGEN_BENCH_JUDGE", "complex")


def _gen_ours(prompt: str) -> str:
    """Run our structuring (+refine) and flatten to text for judging."""
    from workers.doc_worker import _llm_structure
    result = _llm_structure(job_id="bench", fmt="docx", question=prompt)
    if not result:
        return ""
    sections, _meta, title = result
    out = [f"# {title}"]
    for s in sections or []:
        if not isinstance(s, dict):
            continue
        if s.get("heading"):
            out.append(f"## {s['heading']}")
        if s.get("content"):
            out.append(s["content"])
        for b in (s.get("bullets") or []):
            out.append(f"- {b}")
    return "\n\n".join(out)


def _gen_reference(prompt: str, model_hint: str) -> str:
    from models.model_router import model_router
    return (model_router.generate(
        f"Write a well-structured document for this request:\n{prompt}",
        model_hint=model_hint, return_meta=False) or "")


def _judge(prompt: str, doc: str) -> dict:
    from models.model_router import model_router
    raw = (model_router.generate(
        "Score this DOCUMENT (0-10 each) for a user request. Return ONLY JSON "
        '{"structure":n,"faithfulness":n,"completeness":n,"formatting":n,"tone":n}.\n\n'
        f"REQUEST:\n{prompt}\n\nDOCUMENT:\n{doc[:8000]}\n\nJSON:",
        model_hint=_JUDGE_HINT, return_meta=False) or "")
    try:
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
        return {k: float(d.get(k, 0)) for k in
                ("structure", "faithfulness", "completeness", "formatting", "tone")}
    except Exception:
        return {}


def main() -> int:
    rows = []
    for case in CASES:
        if case["expect_intent"] == "none":
            continue
        prompt = case["prompt"]
        print(f"\n=== {case['id']} ===\n{prompt}")
        ours = _gen_ours(prompt)
        s_ours = _judge(prompt, ours)
        print("  ours:", s_ours)
        # References (best-effort — skip if a provider isn't reachable).
        refs = {}
        for name, hint in (("claude", "complex"), ("gpt", "openai-deep")):
            try:
                refs[name] = _judge(prompt, _gen_reference(prompt, hint))
            except Exception as e:  # noqa: BLE001
                print(f"  {name}: skipped ({e})")
        rows.append({"id": case["id"], "ours": s_ours, **refs})

    print("\n\n================ SCORECARD ================")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
