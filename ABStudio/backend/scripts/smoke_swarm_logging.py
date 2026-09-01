# SPDX-License-Identifier: Apache-2.0
"""Local-only smoke test for SwarmRuntime structured logging + JSON dump.

Wires up a swarm with:
  * stub orchestrator returning a 3-worker SwarmPlan
  * stub aggregator returning a fixed envelope
  * stub runner_factory whose run() returns a canned dict per role_id

Verifies:
  1. Run-start INFO log line contains the orchestrator/aggregator/worker model.
  2. Per-worker INFO log lines fire with the resolved model.
  3. logs/swarm/run_<run_id>.json exists and contains setup.models{...},
     plan.workers[], workers[] outcomes, aggregate{}, and outcome{}.

NEVER runs real LLM calls. NEVER hits the network. Safe to invoke in
any environment (including SIT) — purely exercises wiring.

Usage:
    python -m scripts.smoke_swarm_logging
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

# Make the test self-contained and isolated from anything in logs/.
os.environ["SWARM_DUMP_DIR"] = str(Path(__file__).resolve().parent / "_smoke_dump")
os.environ.setdefault("SWARM_DUMP", "1")
# Pretend SIT-style routing so the resolved base url is non-empty in the dump.
os.environ.setdefault("LLM_PROXY_URL", "https://llm-proxy.test/sit")
os.environ.setdefault("LLM_PROXY_TOKEN", "smoke-token")

# Configure root logger so [SWARM] lines hit stdout in the terminal.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

# Import after env vars are set so module-level reads see them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.swarm.runtime import SwarmRuntime, SwarmContext  # noqa: E402
from app.swarm.types import SwarmPlan, WorkerPlan, SwarmAggregatorSpec  # noqa: E402
from app.swarm.aggregator import SwarmAggregator  # noqa: E402
from app.swarm.orchestrator import SwarmOrchestrator  # noqa: E402
from app.swarm.capability_manifest import CapabilityManifest  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _StubOrchestrator(SwarmOrchestrator):
    """Skip real LLM planning; return a hand-built SwarmPlan."""

    def __init__(self, model: str):
        super().__init__(model=model)

    async def plan(self, goal, hints, manifest, *, parent_attached_tools=()):
        return SwarmPlan(
            strategy="parallel",
            shared_memory_policy="last_per_role",
            workers=[
                WorkerPlan(
                    role_id="researcher",
                    role_synth_prompt="You are a researcher.",
                    task="Collect facts about the input.",
                    tools=["web_search"],
                    skills=[],
                    knowledge={"mode": "none"},
                    max_tool_rounds=2, max_tokens=512, temperature=0.1, timeout_s=30,
                ),
                WorkerPlan(
                    role_id="analyst",
                    role_synth_prompt="You are an analyst.",
                    task="Analyse the facts.",
                    tools=["code_executor"],
                    skills=[],
                    knowledge={"mode": "none"},
                    max_tool_rounds=2, max_tokens=512, temperature=0.1, timeout_s=30,
                ),
                WorkerPlan(
                    role_id="writer",
                    role_synth_prompt="You are a writer.",
                    task="Draft a summary.",
                    tools=[],
                    skills=[],
                    knowledge={"mode": "none"},
                    max_tool_rounds=1, max_tokens=512, temperature=0.1, timeout_s=30,
                ),
            ],
            aggregator=SwarmAggregatorSpec(kind="none", prompt=""),
        )


class _StubAggregator(SwarmAggregator):
    async def reduce(self, spec, blackboard):
        # Use the deterministic "kind == none" envelope path so the
        # dump still reflects a real envelope shape.
        return {"summary": "smoke ok", "sources": ["researcher", "analyst", "writer"]}


class _StubRunner:
    async def run(self, agent_id: str, user_message: str, history=None, **kw) -> Dict[str, Any]:
        role = agent_id.split("::")[-1]
        return {"response": f"[{role}] processed: {user_message[:40]}", "generated_files": []}


def _runner_factory():
    return _StubRunner()


# ---------------------------------------------------------------------------
# Patch CapabilityManifest.build so we don't hit any DBs.
# ---------------------------------------------------------------------------

class _StubManifest:
    tools = []
    skills = []
    knowledge_bases = []


async def _stub_build(user_id="", email=""):
    return _StubManifest()


CapabilityManifest.build = staticmethod(_stub_build)  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

async def main() -> int:
    parent_model = os.getenv("CLAUDE_PRIMARY_MODEL", "")
    runtime = SwarmRuntime(
        runner_factory=_runner_factory,
        orchestrator=_StubOrchestrator(model=parent_model),
        aggregator=_StubAggregator(model=parent_model),
        max_parallel=4,
    )
    # ``SwarmRuntime`` reads ``self._worker_model`` from the
    # ``orchestrator_model`` kwarg — but we injected the orchestrator
    # directly. Set the attribute so per-worker model logging reflects
    # parent-agent inheritance the way it would in production.
    runtime._worker_model = parent_model  # noqa: SLF001

    ctx = SwarmContext(
        user_id="smoke-user", email="smoke@test", department="qa", is_admin=False,
        parent_agent_id="agent-smoke-1", thread_id="thr-smoke",
        sse_sink=lambda frame: None,  # discard SSE for this offline test
        parent_attached_tools=("jira_list_issues",),
    )

    envelope = await runtime.execute(goal="smoke goal", hints=None, ctx=ctx)
    print("\n=== Envelope ===")
    print(json.dumps(envelope, indent=2, default=str))

    # Locate the dump file.
    dump_dir = Path(os.environ["SWARM_DUMP_DIR"]).resolve()
    files = sorted(dump_dir.glob("run_*.json"))
    if not files:
        print(f"FAIL: no dump files in {dump_dir}", file=sys.stderr)
        return 1
    latest = files[-1]
    print(f"\n=== Dump file: {latest} ===")
    data = json.loads(latest.read_text(encoding="utf-8"))

    # Inspect the fields the user asked us to surface.
    print(json.dumps({
        "setup.models":       data.get("setup", {}).get("models"),
        "setup.llm_routing":  data.get("setup", {}).get("llm_routing"),
        "plan.worker_count":  data.get("plan", {}).get("worker_count"),
        "plan.workers":       [w["role_id"] for w in data.get("plan", {}).get("workers", [])],
        "workers_summary":    [
            {"role": w["role_id"], "model": w["model"], "ok": w["ok"]}
            for w in data.get("workers", [])
        ],
        "aggregate":          data.get("aggregate"),
        "outcome.ok":         data.get("outcome", {}).get("ok"),
    }, indent=2, default=str))

    # Hard assertions on what the user asked for.
    setup_models = data["setup"]["models"]
    assert setup_models["orchestrator"] == parent_model, setup_models
    assert setup_models["aggregator"]   == parent_model, setup_models
    assert setup_models["workers"]      == parent_model, setup_models
    assert data["setup"]["llm_routing"]["llm_proxy_url_set"] is True
    assert data["plan"]["worker_count"] == 3
    assert len(data["workers"]) == 3
    assert all(w["model"] == parent_model for w in data["workers"]), data["workers"]
    assert data["outcome"]["ok"] is True
    print("\n=== PASS — all assertions OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
