# SPDX-License-Identifier: Apache-2.0
"""
Agent Factory Pipeline — plain Python classes, no agentic frameworks.
All LLM calls route through the project's existing llm_handler (OpenAI-compatible).

The conversational flow is **catalog-only**: blueprint → catalog match →
drop gaps → assemble. ``DynamicToolGenerator`` and ``DynamicSkillGenerator``
are NOT called from the chat path anymore — they are retained because the
``/tools-catalog/generate`` and ``/skills-catalog/generate`` admin endpoints
(invoked by ``CatalogPicker``'s "Generate" button) still use them on
explicit user action. The chat itself never auto-synthesises gaps; missing
tools/skills are simply dropped with a log line and the user can add them
from the picker if needed.

Classes (each independently instantiatable, no global state):
  IntentParser          – detect creation intent, extract raw intent
  ClarificationEngine   – multi-turn Q&A until requirements confirmed
  AgentBlueprintGenerator – produce blueprint JSON from requirements
  ToolSkillMatcher      – score & rank registry candidates
  CapabilityAudit       – flag missing tools/skills
  DynamicToolGenerator  – (admin path only) generate Python tool for gaps
  DynamicSkillGenerator – (admin path only) generate skill markdown for gaps
  AgentAssembler        – combine blueprint + resolved pieces into config
  AgentRegistry         – persist agents (JSON file); save/load/list/delete
  AgentRunner           – load config, build prompt, call LLM, return response
  MonitoringLogger      – append JSONL log: input, output, latency, errors

Session helpers (process-level, in-memory):
  FactorySession        – dataclass tracking multi-turn factory state
  get_or_create_session – look up or create a FactorySession by ID
"""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib.util
import json

import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

from core.logger import logger

# Hoisted to module scope (security review F-04 follow-up) rather than
# imported inside MonitoringLogger._redact on every call — sys.path is
# already primed with the platform root by the time this module loads (see
# app/main.py's _PLATFORM_ROOT insert, which runs before any request
# handler), matching the existing top-level `from core.logger import logger`
# import just above. Fails soft to None if the compliance module is ever
# unavailable (e.g. a stripped-down deployment); _redact treats None the
# same as any other redaction failure — fail open, log the write anyway.
try:
    from agents.compliance_engine import compliance_engine as _compliance_engine
except Exception:  # pragma: no cover - defensive only
    _compliance_engine = None

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent          # repo root
TOOLS_REGISTRY_PATH = PROJECT_ROOT / "tools" / "registry.json"
SKILLS_REGISTRY_PATH = PROJECT_ROOT / "skills" / "registry.json"
GENERATED_TOOLS_DIR = PROJECT_ROOT / "tools" / "generated"
SKILLS_GENERATED_DIR = PROJECT_ROOT / "skills" / "generated"
AGENT_DATA_DIR = Path(__file__).parent.parent / "data"
AGENTS_FILE = AGENT_DATA_DIR / "agents.json"
LOGS_FILE = AGENT_DATA_DIR / "agent_logs.jsonl"

from app.core.factory_utils import (
    FACTORY_MODEL,
    resolve_factory_model as _resolve_factory_model,
    build_factory_llm_config as _build_factory_llm_config,
    call_factory_llm as _call_llm,
    parse_json_response as _parse_json,
    extract_json_block as _extract_json_block,
    raise_if_gateway_rejection,
    SecurityGatewayRejection,
    score_catalog_match,
    semantic_catalog_match,
    MATCH_THRESHOLD,
)

# Default guardrails attached to every assembled agent. `max_turns` and
# `max_tool_rounds` are code-enforced in AgentRunner. The remaining keys are
# prompt-injected — best-effort, not security boundaries.
DEFAULT_GUARDRAILS: dict = {
    "max_turns": 50,
    # Hard cap on tool-call iterations within a single run(). Multi-step
    # tasks (e.g. "fetch a merge-request diff → analyse → write a review",
    # often with retries when a tool returns partial data) routinely need
    # more than a handful of rounds. Set high enough that the cap is a
    # safety net against runaway loops, not a functional ceiling; when it
    # IS hit, run() now forces a final no-tools completion so the user
    # still gets a real answer instead of the model's interim reasoning.
    "max_tool_rounds": 15,
    "off_topic_refusal": False,
    "content_restrictions": [],
}

# Default short-term memory configuration. Currently informational — not
# enforced beyond what's passed in `history` to AgentRunner.run().
DEFAULT_MEMORY_CONFIG: dict = {"type": "sliding_window", "window_size": 20}


def _append_to_registry(registry_path: Path, entry: dict) -> None:
    """
    Append (or replace by name) an entry in a JSON-array registry file.

    The registry file format is a top-level list of dicts with a "name" key.
    Existing entries with the same name are overwritten so re-generation is
    idempotent. Creates parent dirs and the file if missing.
    """
    try:
        if registry_path.exists():
            try:
                data = json.loads(registry_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(f'[AGENT] registry {registry_path} is malformed JSON — rewriting')
                data = []
            if not isinstance(data, list):
                data = []
        else:
            data = []

        name = entry.get("name")
        idx = next(
            (i for i, e in enumerate(data) if isinstance(e, dict) and e.get("name") == name),
            None,
        )
        if idx is not None:
            data[idx] = entry
        else:
            data.append(entry)

        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning(f'[AGENT] _append_to_registry({registry_path}): {exc}')


def _validate_run_signature(code: str) -> bool:
    """
    Return True if ``code`` is parseable Python and contains a top-level
    ``def run(inputs, ...): ...`` (sync or async) where the first positional
    parameter is required (no default). Additional positional parameters
    with defaults are allowed — the dispatcher always calls ``run(inputs)``
    with a single argument, so any extras must be optional.

    Used by DynamicToolGenerator and DynamicSkillGenerator to reject
    generated code that does not conform to the agreed interface
    (``def run(inputs: dict) -> dict``). importlib loading of generated code
    is a known security limitation; this signature check is the minimum
    sanity gate before persisting a file. Not a sandbox.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "run":
            args = node.args
            positional = list(args.posonlyargs) + list(args.args)
            if not positional:
                # No positional params at all — would error on run(inputs).
                continue
            # First positional arg must be required (i.e., no default covers it).
            num_required = len(positional) - len(args.defaults)
            if num_required >= 1:
                return True
    return False


def _coerce_to_text(value: Any, fallback: str = "") -> str:
    """Force ``value`` into a usable string for prompt-shaped fields.

    Smaller local LLMs sometimes ignore "return a string" instructions and
    emit a JSON object or list where the blueprint schema expects prose
    (notably ``system_prompt``). Returning that object unchanged crashes
    downstream string concatenation with ``TypeError``. This helper:

      * returns the trimmed string if already a string,
      * pretty-prints dicts/lists as JSON (readable, valid, no Python repr),
      * casts everything else via ``str(...)``,
      * returns ``fallback`` for ``None`` / empty string.
    """
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip() or fallback
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _strip_code_fences(text: str) -> str:
    """Strip markdown ```python fences and leading/trailing whitespace."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"```(?:python|json)?\s*", "", text).rstrip("` \n").strip()
    return text


# ---------------------------------------------------------------------------
# One-time seed: migrate legacy JSON registries into the postgres catalogs
# ---------------------------------------------------------------------------


_STUB_TOOL_TEMPLATE = '''"""Auto-generated stub for legacy tool '{name}'.

The original implementation file was not found on disk during seed.
Re-run agent creation that needs this tool, or paste real code below
via the catalog admin endpoint.
"""

def run(inputs: dict) -> dict:
    return {{
        "error": "Tool '{name}' has no implementation yet. "
                 "Edit tools_catalog row to add code, or regenerate via the factory.",
        "received_inputs": inputs,
    }}
'''


def _legacy_tool_to_catalog_row(entry: dict, generated_dir: Path) -> Optional[dict]:
    """
    Map a tools/registry.json entry to a tools_catalog row.

    If the entry references a real .py file under tools/generated/, we use
    that file's contents as the code. Otherwise we substitute a stub that
    returns an explicit error so the agent can degrade gracefully.
    """
    name = entry.get("name")
    if not name:
        return None
    description = entry.get("description", "")
    schema = entry.get("input_schema") or {}

    code: Optional[str] = None
    impl_path = entry.get("implementation_path")
    if impl_path:
        candidate = (PROJECT_ROOT / impl_path)
        if candidate.exists():
            try:
                code = candidate.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning(f'[AGENT] seed: failed reading {candidate}: {exc}')

    if code is None and generated_dir.exists():
        # Fall back: try <name>.py in the generated dir
        guess = generated_dir / f"{name}.py"
        if guess.exists():
            try:
                code = guess.read_text(encoding="utf-8")
            except Exception:
                pass

    if code is None or not _validate_run_signature(code):
        logger.info(f"[AGENT] seed: no usable code for tool '{name}' — inserting stub")
        code = _STUB_TOOL_TEMPLATE.format(name=name)

    return {
        "name": name,
        "description": description,
        "input_schema": schema,
        "code": code,
        "generated": bool(entry.get("generated", False)),
    }


def _legacy_skill_to_catalog_row(entry: dict, generated_dir: Path) -> Optional[dict]:
    """
    Map a skills/registry.json entry to a skills_catalog row. Skills are
    stored as markdown content; if the legacy entry has a .py file we
    extract its top-level docstring as the markdown content, otherwise
    we synthesise a minimal markdown skeleton from name + description.
    """
    name = entry.get("name")
    if not name:
        return None
    description = entry.get("description", "")
    category = entry.get("category", "general")

    content: Optional[str] = None
    impl_path = entry.get("implementation_path")
    py_paths: list[Path] = []
    if impl_path:
        py_paths.append(PROJECT_ROOT / impl_path)
    if generated_dir.exists():
        py_paths.append(generated_dir / f"{name}.py")

    for p in py_paths:
        if p.exists():
            try:
                src = p.read_text(encoding="utf-8")
                tree = ast.parse(src)
                docstring = ast.get_docstring(tree)
                if docstring:
                    content = f"# {name.replace('_', ' ').title()}\n\n{docstring.strip()}"
                    break
            except Exception:
                continue

    if not content:
        # Synthesise minimal markdown
        title = name.replace("_", " ").title()
        content = (
            f"# {title}\n\n"
            f"{description or 'Skill: ' + title}.\n\n"
            "## Approach\n\n"
            "1. Carefully read the user's request.\n"
            "2. Apply this skill's expertise to produce a structured response.\n"
            "3. Be concise and accurate.\n"
        )

    return {
        "name": name,
        "description": description,
        "category": category,
        "content": content,
        "generated": bool(entry.get("generated", False)),
    }


async def seed_catalogs_from_legacy() -> dict:
    """
    On first startup, if either catalog table is empty, populate it from
    the legacy JSON registries. Idempotent: returns counts of seeded vs
    existing rows. Safe to call on every startup.
    """
    from app import workflow_repo

    summary = {"tools_seeded": 0, "skills_seeded": 0, "tools_total": 0, "skills_total": 0}

    try:
        tool_count = await workflow_repo.count_tools()
        skill_count = await workflow_repo.count_skills()
    except Exception as exc:
        logger.warning(f'[AGENT] seed_catalogs_from_legacy: catalog count failed: {exc}')
        return summary

    summary["tools_total"] = tool_count
    summary["skills_total"] = skill_count

    if TOOLS_REGISTRY_PATH.exists():
        try:
            data = json.loads(TOOLS_REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f'[AGENT] seed: failed to read {TOOLS_REGISTRY_PATH}: {exc}')
            data = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                if await workflow_repo.get_tool(entry["name"]):
                    continue  # already in catalog — don't clobber edits
                row = _legacy_tool_to_catalog_row(entry, GENERATED_TOOLS_DIR)
                if not row:
                    continue
                try:
                    await workflow_repo.upsert_tool(
                        name=row["name"],
                        code=row["code"],
                        description=row["description"],
                        input_schema=row["input_schema"],
                        generated=row["generated"],
                    )
                    summary["tools_seeded"] += 1
                except Exception as exc:
                    logger.warning(f"[AGENT] seed: failed to upsert tool '{row['name']}': {exc}")

    # Pick up sidecar JSONs and orphan .py files in tools/generated/ even
    # when they aren't referenced from the legacy registry.
    if GENERATED_TOOLS_DIR.exists():
        for sidecar in sorted(GENERATED_TOOLS_DIR.glob("*.json")):
            try:
                entry = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            if await workflow_repo.get_tool(entry["name"]):
                continue
            row = _legacy_tool_to_catalog_row(entry, GENERATED_TOOLS_DIR)
            if not row:
                continue
            try:
                await workflow_repo.upsert_tool(
                    name=row["name"], code=row["code"],
                    description=row["description"], input_schema=row["input_schema"],
                    generated=row["generated"],
                )
                summary["tools_seeded"] += 1
            except Exception as exc:
                logger.warning(f"[AGENT] seed: failed to upsert sidecar tool '{row['name']}': {exc}")

        # Scan loose .py files (no sidecar, not in registry) — these are
        # leftovers from the old file-based DynamicToolGenerator.
        for py in sorted(GENERATED_TOOLS_DIR.glob("*.py")):
            name = py.stem
            if py.name == "__init__.py":
                continue
            if await workflow_repo.get_tool(name):
                continue
            try:
                code = py.read_text(encoding="utf-8")
            except Exception:
                continue
            if not _validate_run_signature(code):
                logger.info(f'[AGENT] seed: skipping orphan {py.name} — no valid run() signature')
                continue
            # Try to extract a description from the module docstring
            description = ""
            try:
                tree = ast.parse(code)
                description = (ast.get_docstring(tree) or "").strip().splitlines()[0][:200]
            except Exception:
                pass
            try:
                await workflow_repo.upsert_tool(
                    name=name, code=code,
                    description=description or f"Imported from legacy file {py.name}",
                    input_schema={}, generated=True,
                )
                summary["tools_seeded"] += 1
                logger.info(f"[AGENT] seed: imported orphan tool '{name}' from {py}")
            except Exception as exc:
                logger.warning(f"[AGENT] seed: failed to upsert orphan '{name}': {exc}")

    if SKILLS_REGISTRY_PATH.exists():
        try:
            data = json.loads(SKILLS_REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f'[AGENT] seed: failed to read {SKILLS_REGISTRY_PATH}: {exc}')
            data = []
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                if await workflow_repo.get_skill(entry["name"]):
                    continue  # already in catalog — don't clobber edits
                row = _legacy_skill_to_catalog_row(entry, SKILLS_GENERATED_DIR)
                if not row:
                    continue
                try:
                    await workflow_repo.upsert_skill(
                        name=row["name"], content=row["content"],
                        description=row["description"], category=row["category"],
                        generated=row["generated"],
                    )
                    summary["skills_seeded"] += 1
                except Exception as exc:
                    logger.warning(f"[AGENT] seed: failed to upsert skill '{row['name']}': {exc}")

    # Sidecar JSONs and orphan .py files in skills/generated/
    if SKILLS_GENERATED_DIR.exists():
        for sidecar in sorted(SKILLS_GENERATED_DIR.glob("*.json")):
            try:
                entry = json.loads(sidecar.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            if await workflow_repo.get_skill(entry["name"]):
                continue
            row = _legacy_skill_to_catalog_row(entry, SKILLS_GENERATED_DIR)
            if not row:
                continue
            try:
                await workflow_repo.upsert_skill(
                    name=row["name"], content=row["content"],
                    description=row["description"], category=row["category"],
                    generated=row["generated"],
                )
                summary["skills_seeded"] += 1
            except Exception as exc:
                logger.warning(f"[AGENT] seed: failed to upsert sidecar skill '{row['name']}': {exc}")

        # Orphan .py files (legacy DynamicSkillGenerator wrote skills as code).
        # Convert their docstring to markdown so the new pipeline can use them.
        for py in sorted(SKILLS_GENERATED_DIR.glob("*.py")):
            name = py.stem
            if py.name == "__init__.py":
                continue
            if await workflow_repo.get_skill(name):
                continue
            row = _legacy_skill_to_catalog_row(
                {"name": name, "description": "", "implementation_path": str(py)},
                SKILLS_GENERATED_DIR,
            )
            if not row:
                continue
            try:
                await workflow_repo.upsert_skill(
                    name=row["name"], content=row["content"],
                    description=row["description"], category=row["category"],
                    generated=True,
                )
                summary["skills_seeded"] += 1
                logger.info(f"[AGENT] seed: imported orphan skill '{name}' from {py}")
            except Exception as exc:
                logger.warning(f"[AGENT] seed: failed to upsert orphan skill '{name}': {exc}")

    if summary["tools_seeded"] or summary["skills_seeded"]:
        logger.info(f"[AGENT] Catalog seed: {summary['tools_seeded']} tool(s) + {summary['skills_seeded']} skill(s) imported from legacy registries")

    # Re-count after the seed so the returned totals are post-seed.
    try:
        summary["tools_total"] = await workflow_repo.count_tools()
        summary["skills_total"] = await workflow_repo.count_skills()
    except Exception:
        pass

    return summary

# ---------------------------------------------------------------------------
# Session state (in-memory, process lifetime)
# ---------------------------------------------------------------------------


@dataclass
class FactorySession:
    session_id: str
    # Stages: "clarifying" → ("suggest_existing") → "confirm" → "done"
    stage: str = "clarifying"
    messages: list[dict] = field(default_factory=list)  # full chat history
    intent: dict = field(default_factory=dict)
    requirements: Optional[dict] = None
    blueprint: Optional[dict] = None
    assembled: Optional[dict] = None
    agent_id: Optional[str] = None
    turn_count: int = 0
    # Existing agents/templates flagged as near-duplicates of the request.
    # Set when stage == "suggest_existing"; cleared on "build new anyway".
    pending_matches: list[dict] = field(default_factory=list)


_sessions: dict[str, FactorySession] = {}
_MAX_AF_SESSIONS = int(os.getenv("AGENT_FACTORY_MAX_SESSIONS", "200"))


def get_or_create_session(session_id: Optional[str]) -> FactorySession:
    """Return existing session or create a new one."""
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    # Evict oldest sessions when the cache exceeds its limit to prevent
    # unbounded memory growth on long-running servers.
    if len(_sessions) >= _MAX_AF_SESSIONS:
        oldest = next(iter(_sessions))
        _sessions.pop(oldest, None)
    sid = session_id or str(uuid.uuid4())
    session = FactorySession(session_id=sid)
    _sessions[sid] = session
    return session


# ---------------------------------------------------------------------------
# Session persistence (Postgres write-through / read-through)
# ---------------------------------------------------------------------------
# The in-memory ``_sessions`` cache is lost on backend restart / LRU eviction.
# These helpers mirror a session to the ``factory_sessions`` table so an
# interrupted build can be resumed. Best-effort: persistence failures never
# break the live chat turn.

FACTORY_TYPE = "agent"


def serialize_session(session: FactorySession) -> dict:
    """Convert a FactorySession into a JSON-serialisable dict for storage."""
    from dataclasses import asdict
    return asdict(session)


def hydrate_session(state: dict) -> FactorySession:
    """Rebuild a FactorySession from a persisted state dict."""
    known = {f for f in FactorySession.__dataclass_fields__}  # type: ignore[attr-defined]
    return FactorySession(**{k: v for k, v in (state or {}).items() if k in known})


async def get_or_restore_session(
    session_id: Optional[str], owner_user_id: str
) -> FactorySession:
    """Return an in-memory session, restoring it from Postgres if evicted.

    Falls back to ``get_or_create_session`` when there is nothing persisted
    or persistence is unavailable, so behaviour is unchanged when the DB is
    down. Scoped by ``owner_user_id``.
    """
    if session_id and session_id in _sessions:
        return _sessions[session_id]
    if session_id:
        try:
            from app.core import workflow_repo
            state = await workflow_repo.load_factory_session(
                session_id, FACTORY_TYPE, owner_user_id
            )
            if state:
                session = hydrate_session(state)
                _sessions[session.session_id] = session
                return session
        except Exception:
            logger.debug('[AGENT] agent_factory: session restore skipped', exc_info=True)
    return get_or_create_session(session_id)


async def persist_session(session: FactorySession, owner_user_id: str) -> None:
    """Write-through the current session state to Postgres (best-effort)."""
    try:
        from app.core import workflow_repo
        await workflow_repo.save_factory_session(
            session.session_id, FACTORY_TYPE, owner_user_id, serialize_session(session)
        )
    except Exception:
        logger.debug('[AGENT] agent_factory: session persist skipped', exc_info=True)


# Platform-utility tools — cross-cutting / last-resort entries the
# assembler must not count as "purpose-built work tools" when deciding
# whether to auto-inject ``code_executor``, and the swarm runtime must
# strip from worker toolsets so subagents can't reach the escape hatch.
# Re-exported as the single source of truth for the assembler gate
# (~L1435), the parent-runner gate (~L2727), and the worker spec
# materialiser (~L1997).
_PLATFORM_UTILITY_TOOLS: frozenset = frozenset({"code_executor", "spawn_swarm"})


# ---------------------------------------------------------------------------
# 1. IntentParser
# ---------------------------------------------------------------------------


class IntentParser:
    """Detects agent-creation intent and extracts raw intent from user message."""

    def __init__(self) -> None:
        pass

    async def parse(self, message: str) -> dict:
        """
        Returns::

            {
                "is_creation_intent": bool,
                "raw_intent": str,
                "summary": str
            }
        """
        system = (
            "You are an intent classifier for an AI agent-creation system.\n"
            "Given the user's first message, extract structured intent to seed the conversation.\n\n"
            "Return JSON only:\n"
            "{\n"
            '  "is_creation_intent": true/false,\n'
            '  "raw_intent": "<what they want the agent to do, in their own words>",\n'
            '  "inferred_domain": "<domain: product / technical / general / content / data / coding / research / operations / other>",\n'
            '  "inferred_persona": "<likely tone: professional / friendly / technical / casual>",\n'
            '  "missing": ["<key info absent — e.g. audience, output_format, scope, tone>"]\n'
            "}\n\n"
            "Output ONLY valid JSON. No markdown."
        )
        try:
            raw = await _call_llm(system, [{"role": "user", "content": message}], max_tokens=256)
            result = _parse_json(raw)
            if not result:
                raise ValueError("empty parse")
            return result
        except SecurityGatewayRejection:
            # Don't swallow gateway blocks behind the fallback — let the user
            # see why their input was rejected.
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] IntentParser.parse: {exc}')
            return {
                "is_creation_intent": True,
                "raw_intent": message,
                "summary": message[:120],
            }


# ---------------------------------------------------------------------------
# 1b. AgentPlanCardGenerator — structured pre-generation questionnaire
# ---------------------------------------------------------------------------


class AgentPlanCardGenerator:
    """Produce a structured Plan Card for the agent factory.

    Returns 4-5 base decisions (plus 5 developer-specific decisions when the
    request looks like a coding/dev agent), each with a fixed set of chip
    ``options`` and an LLM-inferred ``default``. One fast LLM call picks the best
    default per question; the option lists are ALWAYS the static lists below so
    the chips are predictable and never hallucinated.

    Output shape (consumed by the frontend PlanCard component)::

        {"questions": [
            {"id": "audience", "label": "...", "default": "Internal team",
             "options": ["Internal team", "End customers", ...]},
            ...
        ]}
    """

    BASE_QUESTIONS = [
        {"id": "audience", "label": "Who will use this agent?",
         "options": ["Internal team", "End customers", "Other agents", "Developers"]},
        {"id": "refusal_scope", "label": "What should it never do?",
         "options": ["None specified", "Approve transactions", "Access prod data", "Send emails"],
         "allow_freetext": True},
        {"id": "tone", "label": "How formal should responses be?",
         "options": ["Professional", "Conversational", "Structured lists", "Terse"]},
        {"id": "escalation", "label": "What happens when it can't answer?",
         "options": ["Say \"I don't know\"", "Escalate to human", "Log and notify", "Try another approach"]},
        {"id": "detail_level", "label": "How detailed should instructions be?",
         "options": ["Standard", "Minimal (fast)", "Exhaustive (complex tasks)"]},
    ]

    DEVELOPER_QUESTIONS = [
        {"id": "repos", "label": "Which repositories should this agent work with?",
         "options": ["I'll specify in instructions", "All repos in org", "Specific repos"],
         "allow_freetext": True},
        {"id": "branches", "label": "Which branches should it focus on?",
         "options": ["main / master", "feature branches", "release branches", "All branches"]},
        {"id": "languages", "label": "What languages / stacks does it need to understand?",
         "options": ["Auto-detect from repo", "Python", "Java", "JavaScript / TypeScript", "Go"],
         "allow_freetext": True},
        {"id": "code_tasks", "label": "What kind of code tasks should it perform?",
         "options": ["Review only", "Review + suggest fixes", "Generate code", "Write tests", "All of the above"]},
        {"id": "autonomous_approve", "label": "Should it ever approve or merge changes autonomously?",
         "options": ["No — human must approve", "Yes, for minor fixes", "Yes, with audit log"]},
    ]

    _DEVELOPER_KEYWORDS = {
        "code review", "pr", "pull request", "repository", "repo",
        "branch", "sdlc", "pipeline", "ci/cd", "git", "merge request", "diff",
    }

    @classmethod
    def _is_developer_agent(cls, intent: dict, user_message: str) -> bool:
        domain = (intent.get("inferred_domain") or "").lower()
        if domain in ("coding", "technical"):
            return True
        msg_lower = (user_message or "").lower()
        return any(kw in msg_lower for kw in cls._DEVELOPER_KEYWORDS)

    async def generate(self, intent: dict, user_message: str) -> dict:
        questions = [dict(q) for q in self.BASE_QUESTIONS]
        if self._is_developer_agent(intent, user_message):
            questions += [dict(q) for q in self.DEVELOPER_QUESTIONS]

        # Seed defaults with the first option (safe fallback if the LLM fails).
        for q in questions:
            q.setdefault("default", q["options"][0])

        defaults = await _infer_plan_card_defaults(
            questions, user_message, intent,
            context="You are configuring an AI agent.",
        )
        for q in questions:
            picked = defaults.get(q["id"])
            if picked in q["options"]:
                q["default"] = picked
        return {"questions": questions}


async def _infer_plan_card_defaults(
    questions: list[dict], user_message: str, intent: dict, context: str,
) -> dict:
    """One fast LLM call: pick the best default option for each plan-card question.

    Returns ``{question_id: chosen_option}``. Options are constrained to the
    static lists — any hallucinated value is dropped by the caller. Never raises;
    returns ``{}`` on any failure so the caller keeps the seeded first-option
    defaults.
    """
    try:
        q_lines = "\n".join(
            f'- id="{q["id"]}" — {q["label"]} — options: {q["options"]}'
            for q in questions
        )
        system = (
            f"{context}\n"
            "For each question below, choose the SINGLE best default option based on "
            "the user's request. You MUST pick a value verbatim from that question's "
            "options list — never invent a new value.\n\n"
            f"Questions:\n{q_lines}\n\n"
            'Return ONLY a JSON object mapping each id to your chosen option, e.g. '
            '{"audience": "Internal team", "tone": "Professional"}. No markdown.'
        )
        raw = await _call_llm(
            system,
            [{"role": "user", "content": (user_message or "")[:1500]}],
            max_tokens=256,
        )
        parsed = _parse_json(raw)
        return parsed if isinstance(parsed, dict) else {}
    except SecurityGatewayRejection:
        raise
    except Exception as exc:
        logger.warning(f'[AGENT] plan-card default inference failed: {exc}')
        return {}


# ---------------------------------------------------------------------------
# 2. ClarificationEngine
# ---------------------------------------------------------------------------


class ClarificationEngine:
    """Asks follow-up questions until all requirements are confirmed (multi-turn)."""

    MAX_TURNS = 3  # after this many user turns, commit to a blueprint

    def __init__(self) -> None:
        pass

    async def get_next_question_or_requirements(
        self,
        intent: dict,
        messages: list[dict],
    ) -> dict:
        """
        Either asks ONE plain-English follow-up or returns the inferred
        requirements. Designed for non-technical users — never asks about
        "integrations", "triggers", "inputs/outputs" etc. Infers those
        silently from the chat instead.

        Returns::

            {
                "done": bool,
                "question": str,          # present when done=false
                "requirements": {          # present when done=true
                    "purpose", "inputs", "outputs",
                    "integrations", "trigger_type", "persona", "additional_notes"
                }
            }
        """
        user_turns = sum(1 for m in messages if m["role"] == "user")
        force_done = user_turns >= self.MAX_TURNS

        conversation = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages
        )

        system = (
            "You are a warm, helpful coworker helping someone describe an AI agent they want to build.\n"
            f"Their original ask: \"{intent.get('raw_intent', '')}\"\n\n"
            "Your job: gather just enough to build a great agent, then COMMIT. One question max, then generate.\n\n"
            "COMMIT IMMEDIATELY (done=true, no question) when:\n"
            "- They described what it does + who uses it\n"
            "- They gave purpose + desired output style\n"
            "- The ask is specific enough to write a detailed brief\n"
            "- You've already asked one question — build with what you have\n\n"
            "WHEN YOU DO ASK:\n"
            "- ONE question only. Never two in one message.\n"
            "- Plain English only. Banned: integration, trigger, input, output, API, persona,\n"
            "  workflow, pipeline, schema, tool, skill, configure, parameters, deployment\n"
            "- Ask the single most important missing thing\n"
            "- Sound like a curious colleague: 'Got it — who's the main audience for this?'\n\n"
            "SUGGESTIONS: Always provide exactly 4 chips that are DIRECT ANSWERS to your question.\n"
            "Each chip should be a concrete choice the user can click instead of typing.\n"
            "Format: {\"icon\": \"one emoji\", \"label\": \"3-6 word answer\"}\n"
            "Example — if you ask 'What kind of output do you need?', good chips are:\n"
            "  {\"icon\": \"📊\", \"label\": \"Excel report with charts\"}\n"
            "  {\"icon\": \"📧\", \"label\": \"Email summary to team\"}\n"
            "  {\"icon\": \"📋\", \"label\": \"Jira ticket updates\"}\n"
            "  {\"icon\": \"📄\", \"label\": \"PDF document\"}\n"
            "Bad chips: 'Option A', 'Yes', 'Something else' ← too vague, not answers\n\n"
            f"{'IMPORTANT: SET done=true NOW. Enough turns have passed — commit to what you know, no more questions.' if force_done else ''}\n\n"
            "When done=true, infer ALL fields from the conversation. Never ask about them:\n"
            "- purpose: one clear sentence — what does this agent do for the user?\n"
            "- inputs: what the user gives it (default: 'user messages or requests')\n"
            "- outputs: what it returns (default: 'a helpful response in clear text')\n"
            "- integrations: external services mentioned, else 'none'\n"
            "- trigger_type: 'manual' unless explicitly said otherwise\n"
            "- persona: tone inferred from context (default: 'friendly and helpful')\n"
            "- additional_notes: any extra detail shared\n\n"
            "Return ONLY valid JSON, no markdown:\n"
            "{\n"
            '  "done": true | false,\n'
            '  "question": "your question (omit when done=true)",\n'
            '  "suggestions": [{"icon": "emoji", "label": "text"}, ...],\n'
            '  "requirements": { "purpose": "...", "inputs": "...", "outputs": "...", "integrations": "...", "trigger_type": "...", "persona": "...", "additional_notes": "..." }\n'
            "}"
        )

        try:
            raw = await _call_llm(
                system,
                [{"role": "user", "content": conversation}],
                max_tokens=600,
            )
            parsed = _parse_json(raw)
            # Normalize suggestions: LLM may return plain strings; always emit {icon, label}
            if "suggestions" in parsed and isinstance(parsed["suggestions"], list):
                parsed["suggestions"] = [
                    s if isinstance(s, dict) and "icon" in s and "label" in s
                    else {"icon": "✦", "label": str(s)}
                    for s in parsed["suggestions"]
                ]
            if force_done and not parsed.get("done"):
                parsed["done"] = True
            if parsed.get("done") and not parsed.get("requirements"):
                parsed["requirements"] = self._fallback_requirements(intent, messages)
            return parsed
        except SecurityGatewayRejection:
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] ClarificationEngine: {exc}')
            return {
                "done": True,
                "requirements": self._fallback_requirements(intent, messages),
            }

    def _fallback_requirements(self, intent: dict, messages: list[dict]) -> dict:
        user_text = " ".join(m["content"] for m in messages if m["role"] == "user")
        return {
            "purpose": intent.get("summary", user_text[:200]),
            "inputs": "user messages or requests",
            "outputs": "a helpful response in clear text",
            "integrations": "none",
            "trigger_type": "manual",
            "persona": "friendly and helpful",
            "additional_notes": user_text[:400],
        }


# ---------------------------------------------------------------------------
# 3. AgentBlueprintGenerator
# ---------------------------------------------------------------------------

# Domains that MUST answer from grounded documents. Any hit here forces the
# blueprint into ``existing_kb`` mode with a low temperature, regardless of
# what the LLM produced — the "HR Policy Assistant" bug was that the LLM
# occasionally emitted ``knowledge.mode="none"`` for HR agents, so the
# deployed agent hallucinated policy answers.
_KB_DOMAIN_KEYWORDS = (
    # HR / people
    "hr ", "human resource", "employee", "leave", "vacation", "sick leave",
    "benefits", "onboarding", "offboarding", "handbook", "grievance",
    "payroll", "performance review", "appraisal",
    # Legal / compliance / policy
    "policy", "policies", "compliance", "legal", "regulation", "regulatory",
    "gdpr", "hipaa", "iso ", "audit", "governance", "risk management",
    "code of conduct",
    # Product docs / SOPs / KB-shaped
    "documentation", "knowledge base", "kb ", "product doc", "user manual",
    "runbook", "sop", "standard operating", "guideline", "faq", "manual",
    "wiki", "reference doc", "specification",
    # Support-with-grounded-answers
    "support agent", "helpdesk", "customer support", "grounded",
    "citation", "cite the", "source of truth",
)

# Domains that carry irreversible external action risk — default HITL to
# ``before_tool`` so the human approves the actual write before it lands.
_HITL_BEFORE_TOOL_KEYWORDS = (
    "delete", "drop table", "revoke", "terminate", "fire ",
    "wire transfer", "payment", "refund", "chargeback",
    "production deploy", "prod deploy", "rollback prod",
    "merge to main", "merge to master", "force push",
)

# Domains that produce external-facing artefacts — default HITL to
# ``after_response`` so the human reviews the draft before it goes out.
_HITL_AFTER_RESPONSE_KEYWORDS = (
    "customer email", "external email", "press release", "public statement",
    "regulatory filing", "legal opinion", "legal advice",
    "board memo", "shareholder", "investor",
    "marketing copy", "outbound message",
)


def _blueprint_signal_text(blueprint: dict, requirements: dict) -> str:
    """Concatenate the text fields the domain-detector should look at.

    Returns lower-cased so callers can just ``in`` against keyword tuples.
    """
    parts = [
        blueprint.get("name") or "",
        blueprint.get("description") or "",
        blueprint.get("system_prompt") or "",
        blueprint.get("persona") or "",
        requirements.get("purpose") or "",
        requirements.get("additional_notes") or "",
    ]
    return " ".join(str(p) for p in parts).lower()


def _coerce_float(raw, fallback: float, lo: float, hi: float) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return fallback
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _coerce_int(raw, fallback: int, lo: int, hi: int) -> int:
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return fallback
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _apply_domain_defaults(blueprint: dict, requirements: dict) -> None:
    """Fill / correct ``model_params``, ``knowledge``, ``hitl_mode`` in-place.

    Runs AFTER the LLM has produced the blueprint. Steps:
      1. Coerce ``model_params`` to the legal ranges (drop garbage).
      2. Coerce ``knowledge`` to the canonical ``{mode, suggested_topics,
         reason}`` shape.
      3. Coerce ``hitl_mode`` to one of ``off | before_tool | after_response``.
      4. Apply domain heuristics: knowledge-grounded domains force KB on and
         drop temperature; irreversible-action domains force HITL before-tool;
         customer-facing draft domains force HITL after-response.

    The heuristics only STRENGTHEN the config (never weaken it): if the LLM
    already set ``knowledge.mode="existing_kb"`` or picked a low temperature,
    we keep it.
    """
    text = _blueprint_signal_text(blueprint, requirements)

    # --- model_params ---
    raw_params = blueprint.get("model_params") if isinstance(blueprint.get("model_params"), dict) else {}
    temperature = _coerce_float(raw_params.get("temperature"), fallback=0.3, lo=0.0, hi=1.0)
    max_tokens  = _coerce_int(raw_params.get("max_tokens"),  fallback=4096, lo=256, hi=32768)
    top_p       = _coerce_float(raw_params.get("top_p"),      fallback=0.9, lo=0.0, hi=1.0)

    # --- knowledge ---
    raw_kb = blueprint.get("knowledge") if isinstance(blueprint.get("knowledge"), dict) else {}
    kb_mode = str(raw_kb.get("mode") or "none").strip().lower()
    if kb_mode not in ("none", "existing_kb"):
        kb_mode = "none"
    kb_topics = raw_kb.get("suggested_topics")
    if not isinstance(kb_topics, list):
        kb_topics = []
    kb_topics = [str(t).strip() for t in kb_topics if str(t).strip()][:5]
    kb_reason = str(raw_kb.get("reason") or "").strip()

    # --- hitl_mode ---
    hitl_mode = str(blueprint.get("hitl_mode") or "off").strip().lower()
    if hitl_mode not in ("off", "before_tool", "after_response"):
        hitl_mode = "off"

    # --- Domain heuristics ---
    is_kb_domain = any(kw in text for kw in _KB_DOMAIN_KEYWORDS)
    if is_kb_domain:
        if kb_mode == "none":
            kb_mode = "existing_kb"
            if not kb_reason:
                kb_reason = "Answers must be grounded in the relevant knowledge base with citations."
        # Force deterministic behaviour for grounded domains.
        if temperature > 0.35:
            temperature = 0.2
        if top_p > 0.95:
            top_p = 0.9

    is_before_tool_domain = any(kw in text for kw in _HITL_BEFORE_TOOL_KEYWORDS)
    is_after_response_domain = any(kw in text for kw in _HITL_AFTER_RESPONSE_KEYWORDS)
    # Explicit user request wins over heuristics.
    user_asked_hitl = any(kw in text for kw in ("hitl", "human in the loop", "human-in-the-loop", "human approval", "require approval", "manual review"))
    if hitl_mode == "off":
        if is_before_tool_domain:
            hitl_mode = "before_tool"
        elif is_after_response_domain or user_asked_hitl:
            hitl_mode = "after_response"

    blueprint["model_params"] = {
        "temperature": round(temperature, 3),
        "max_tokens": max_tokens,
        "top_p": round(top_p, 3),
    }
    blueprint["knowledge"] = {
        "mode": kb_mode,
        # UI stores namespace ids here; we ship an empty list so the user
        # picks the concrete KB. `suggested_topics` is a hint for the
        # picker's placeholder.
        "namespaces": [],
        "suggested_topics": kb_topics,
        "reason": kb_reason,
    }
    blueprint["hitl_mode"] = hitl_mode


class AgentBlueprintGenerator:
    """Produces a structured agent blueprint from confirmed requirements.

    Uses a 2-step approach (matching the Workflow Factory pattern):
      Step 1 — LLM designs the agent (name, description, system_prompt, etc.)
               with NO tool catalog in the prompt → fast generation.
      Step 2 — A separate LLM call assigns tools/skills from the full catalog,
               grouped by service → focused, reliable matching.
    """

    def __init__(self) -> None:
        pass

    async def generate(
        self,
        requirements: dict,
        available_skills: Optional[list[dict]] = None,
        available_tools: Optional[list[dict]] = None,
    ) -> dict:
        # --- Step 1: Generate blueprint (NO tools in prompt) ---
        blueprint = await self._generate_blueprint(requirements, available_skills)

        # --- Step 2: Assign tools/skills from catalog ---
        if available_tools or available_skills:
            tool_list, skill_list = await self._assign_tools_skills(
                blueprint, available_tools or [], available_skills or [],
            )
            blueprint["tool_list"] = tool_list
            blueprint["skill_list"] = skill_list

        return blueprint

    async def _generate_blueprint(
        self, requirements: dict, available_skills: Optional[list[dict]],
    ) -> dict:
        """LLM call #1 — agent design only. No tool catalog → fast."""
        safe_req = {
            "purpose":      requirements.get("purpose", ""),
            "inputs":       requirements.get("inputs", ""),
            "outputs":      requirements.get("outputs", ""),
            "trigger_type": requirements.get("trigger_type", "manual"),
            "persona":      requirements.get("persona", ""),
        }
        # Plan Card structured decisions — only include when set so the
        # conversational path (which never populates them) is unaffected.
        if requirements.get("tone"):
            safe_req["tone"] = requirements["tone"]
        if requirements.get("escalation_policy"):
            safe_req["escalation_policy"] = requirements["escalation_policy"]
        # ``additional_notes`` carries developer-agent decisions (repos, branches,
        # languages, autonomous-approve rules) and any free-text refusal scope.
        # It was previously dropped here, so plan-card notes never reached the
        # blueprint — thread it through explicitly.
        if requirements.get("additional_notes"):
            safe_req["additional_notes"] = requirements["additional_notes"]
        req_json = json.dumps(safe_req, indent=2)

        # Only skill names (short list) — tools are assigned in step 2
        skill_section = ""
        if available_skills:
            skill_names = [s["name"] for s in available_skills]
            skill_section = f"Skills available: {', '.join(skill_names)}\n"

        system = (
            "You are an AI agent designer. Produce a JSON blueprint.\n\n"
            "Return JSON with these exact fields:\n\n"
            "- name: 3-5 words, role-first (e.g. 'Product Help Bot', 'Document Processor' — never just 'Bot')\n"
            "- description: exactly 2 sentences — what it does, and who uses it\n"
            "- system_prompt: a plain string (NOT an object or list) structured into exactly 4 paragraphs:\n"
            "    Para 1 — Role: 'You are [specific role]. Your purpose is [specific purpose].'\n"
            "    Para 2 — Capabilities: what you can help with, your strengths, what you know\n"
            "    Para 3 — Scope: what falls outside your remit, when to defer to a human\n"
            "    Para 4 — Output style: tone, format, length, and any formatting rules\n"
            "  Each paragraph is 2-3 sentences. system_prompt MUST be a plain string — never an object.\n"
            "  If the requirements include `tone`, Para 4 MUST reflect it verbatim (e.g. 'Conversational' → warm, casual language).\n"
            "  If the requirements include `escalation_policy`, Para 3 MUST state that exact escalation behaviour.\n"
            "  If the requirements include `additional_notes`, treat every line as a HARD constraint — e.g. repo/branch/language scoping and any 'Must not approve/merge autonomously' rule MUST appear explicitly in the system_prompt.\n\n"
            "- tool_list: [] (leave empty — tools are assigned separately)\n"
            "- skill_list: array of strings. Use exact names from the list below. Use [] if none fit.\n"
            "- trigger: one of 'manual' | 'scheduled' | 'event_driven' | 'api_call'\n"
            "- persona: one specific sentence about tone and style\n"
            "- guardrails: {max_turns:50, max_tool_rounds:5, off_topic_refusal:bool, content_restrictions:[str]}\n"
            "- suggested_edits: exactly 4 strings, each 3-6 words, specific to what was built\n"
            "- model_params: {temperature: float 0-1, max_tokens: int 512-8192, top_p: float 0-1} — TUNED to the job. "
            "Low temperature (0.15-0.3) for policy/legal/compliance/HR/finance/SQL/support-KB agents "
            "(deterministic, factual, minimise hallucination). Medium (0.4-0.6) for structured writers "
            "(reports, emails, drafting). Higher (0.7-0.9) only for open-ended creative or ideation agents. "
            "max_tokens: 2048 for short Q&A, 4096 for RAG/policy answers, 8192 for long-form generation.\n"
            "- knowledge: {mode: 'none' | 'existing_kb', suggested_topics: [str], reason: str}. "
            "Set mode to 'existing_kb' when the agent MUST ground answers in a knowledge base — "
            "HR policies, legal, compliance, product docs, SOPs, technical manuals, support KBs, "
            "regulations, standards, internal wikis. `suggested_topics` are 1-5 short natural-language "
            "labels of what should be in the KB (e.g. ['HR Policies', 'Leave Rules', 'Employee Handbook']). "
            "Set mode to 'none' for pure-reasoning agents (chit-chat, translators, formatters, calculators).\n"
            "- hitl_mode: 'off' | 'after_response' | 'before_tool'. Use 'after_response' when the agent's "
            "output is sent to customers / stakeholders and needs review (support drafts, external emails, "
            "legal opinions). Use 'before_tool' when the agent takes IRREVERSIBLE external actions (create/update/"
            "delete in Jira/GitLab/DB/email). Use 'off' for read-only, Q&A, or internal-only agents.\n\n"
            + skill_section
            + "\nEXAMPLE BLUEPRINT (for a 'Code Review Assistant' agent):\n"
            '{\n'
            '  "name": "Code Review Assistant",\n'
            '  "description": "Reviews pull requests for code quality, security, and best practices. Used by engineering teams to catch issues before merge.",\n'
            '  "system_prompt": "You are a Code Review Assistant. Your purpose is to analyze pull request diffs and provide actionable, specific feedback on code quality, security vulnerabilities, and adherence to best practices.\\n\\nYou can help with identifying bugs, suggesting performance improvements, flagging security risks (SQL injection, XSS, hardcoded secrets), checking naming conventions, and verifying error handling. You understand Python, JavaScript, TypeScript, Go, and Java code patterns.\\n\\nYou should not approve or reject PRs autonomously — always defer the final decision to a human reviewer. You also do not write or modify code; you only review and suggest. If a diff is too large to review meaningfully, say so and suggest splitting the PR.\\n\\nBe direct and specific in your feedback. Reference exact line numbers and code snippets. Use severity labels (critical, warning, suggestion). Keep feedback concise — one issue per bullet point. End with a summary of overall code health.",\n'
            '  "tool_list": [],\n'
            '  "skill_list": [],\n'
            '  "trigger": "manual",\n'
            '  "persona": "thorough and direct, like a senior engineer doing a review",\n'
            '  "guardrails": {"max_turns": 50, "max_tool_rounds": 5, "off_topic_refusal": false, "content_restrictions": []},\n'
            '  "suggested_edits": ["Add security-focused checks", "Support more languages", "Enable auto-approval for trivial PRs", "Add PR template enforcement"],\n'
            '  "model_params": {"temperature": 0.3, "max_tokens": 4096, "top_p": 0.9},\n'
            '  "knowledge": {"mode": "none", "suggested_topics": [], "reason": "Code review reasons over the diff in-context; no external KB needed."},\n'
            '  "hitl_mode": "off"\n'
            '}\n\n'
            "SECOND EXAMPLE (an HR Policy Q&A agent — note KB is on, temperature is low):\n"
            '{\n'
            '  "name": "HR Policy Assistant",\n'
            '  "description": "Answers employee questions about HR policies, leave, benefits, and workplace guidelines. Used by employees and managers seeking authoritative policy answers.",\n'
            '  "system_prompt": "You are an HR Policy Assistant. Your purpose is to answer employee questions about company HR policies, leave entitlements, benefits, and workplace guidelines with precise, cited answers grounded in the official policy documents.\\n\\nYou can help employees understand vacation and sick leave rules, parental leave, benefits enrolment, code of conduct, remote-work policy, expense claims, and grievance procedures. You always ground answers in the retrieved policy documents and cite the specific policy section.\\n\\nYou never guess or generalise from external HR practice — if the retrieved documents do not cover the question, say so and route the employee to HR Business Partner. You do not disclose personally identifiable information about other employees. You do not offer legal advice.\\n\\nAnswer in a warm, professional tone. Keep answers concise (2-5 sentences) unless the policy itself is complex. Cite the source (policy name and section). If the policy allows discretion, say so and explain what factors typically apply.",\n'
            '  "tool_list": [],\n'
            '  "skill_list": [],\n'
            '  "trigger": "manual",\n'
            '  "persona": "warm, professional, precise — like a knowledgeable HR partner",\n'
            '  "guardrails": {"max_turns": 30, "max_tool_rounds": 3, "off_topic_refusal": true, "content_restrictions": ["never disclose PII of other employees", "never provide legal advice"]},\n'
            '  "suggested_edits": ["Attach HR policies KB", "Add leave calculator tool", "Enable HITL for edge cases", "Add multilingual support"],\n'
            '  "model_params": {"temperature": 0.2, "max_tokens": 4096, "top_p": 0.9},\n'
            '  "knowledge": {"mode": "existing_kb", "suggested_topics": ["HR Policies", "Employee Handbook", "Leave & Benefits"], "reason": "Answers must be grounded in the official HR policy corpus with citations."},\n'
            '  "hitl_mode": "off"\n'
            '}\n\n'
            "Output ONLY valid JSON. system_prompt MUST be a plain string."
        )

        # No assistant-prefill "{" turn — some gateway models reject it with
        # "This model does not support assistant message prefill. The
        # conversation must end with a user message." (400). Ask for pure JSON
        # in the prompt and let ``_parse_json`` isolate it.
        raw = await _call_llm(
            system,
            [
                {"role": "user", "content": (
                    f"Requirements:\n{req_json}\n\n"
                    "Respond with ONLY the JSON object — start with `{` and end "
                    "with `}`. No markdown, no code fences, no commentary."
                )},
            ],
            max_tokens=2048,
        )
        blueprint = _parse_json(raw)

        # --- Name fallback ---
        if not blueprint.get("name") or blueprint["name"] == "Custom Agent":
            purpose = (requirements.get("purpose") or "").strip()
            if purpose:
                words = purpose.split()[:5]
                blueprint["name"] = " ".join(words).rstrip(".,;:").title() + " Agent"
            else:
                blueprint["name"] = "Custom Agent"

        # --- Coerce shape drift ---
        blueprint["system_prompt"] = _coerce_to_text(
            blueprint.get("system_prompt"),
            fallback=f"You are a helpful AI assistant. {requirements.get('purpose', '')}",
        )
        blueprint["name"] = _coerce_to_text(blueprint.get("name"), fallback="Custom Agent")
        blueprint["description"] = _coerce_to_text(blueprint.get("description"), fallback="")
        blueprint["persona"] = _coerce_to_text(
            blueprint.get("persona"), fallback="helpful and professional"
        )
        blueprint["trigger"] = _coerce_to_text(blueprint.get("trigger"), fallback="manual")

        for key in ("tool_list", "skill_list"):
            raw = blueprint.get(key)
            if isinstance(raw, str):
                blueprint[key] = [raw]
            elif isinstance(raw, list):
                blueprint[key] = [str(x) for x in raw if x]
            else:
                blueprint[key] = []

        # --- Guardrails ---
        provided = blueprint.get("guardrails") or {}
        if not isinstance(provided, dict):
            provided = {}
        merged = dict(DEFAULT_GUARDRAILS)
        merged.update({k: v for k, v in provided.items() if v is not None})
        try:
            merged["max_turns"] = int(merged.get("max_turns", DEFAULT_GUARDRAILS["max_turns"]))
        except (TypeError, ValueError):
            merged["max_turns"] = DEFAULT_GUARDRAILS["max_turns"]
        try:
            merged["max_tool_rounds"] = int(merged.get("max_tool_rounds", DEFAULT_GUARDRAILS["max_tool_rounds"]))
        except (TypeError, ValueError):
            merged["max_tool_rounds"] = DEFAULT_GUARDRAILS["max_tool_rounds"]
        merged["off_topic_refusal"] = bool(merged.get("off_topic_refusal", False))
        restrictions = merged.get("content_restrictions") or []
        if not isinstance(restrictions, list):
            restrictions = [str(restrictions)]
        merged["content_restrictions"] = [str(r) for r in restrictions if r]
        blueprint["guardrails"] = merged

        # --- Model params / Knowledge / HITL — new blueprint fields ---
        # The LLM occasionally omits these; deterministic coercion + a
        # domain-aware fallback guarantee the assembled agent always ships
        # with a sensible model config, a KB pointer when the domain calls
        # for grounding, and an HITL policy that matches the risk level.
        _apply_domain_defaults(blueprint, requirements)

        return blueprint

    async def _assign_tools_skills(
        self,
        blueprint: dict,
        available_tools: list[dict],
        available_skills: list[dict],
    ) -> tuple[list[str], list[str]]:
        """LLM call #2 — tool/skill assignment from full catalog.

        Same pattern as WorkflowToolSkillAssigner: compact prompt with the
        full catalog grouped by service, focused purely on selection.
        """
        agent_name = blueprint.get("name", "Agent")
        agent_desc = blueprint.get("description", "")

        # Build catalog grouped by service
        catalog_lines: list[str] = []
        if available_tools:
            svc_groups: dict[str, list[str]] = {}
            for t in available_tools:
                svc = (t.get("service") or "general").strip() or "general"
                svc_groups.setdefault(svc, []).append(t["name"])
            tool_lines = []
            for svc, names in sorted(svc_groups.items()):
                tool_lines.append(f"  {svc}: {', '.join(names)}")
            catalog_lines.append("TOOLS:\n" + "\n".join(tool_lines))
        if available_skills:
            skill_names = [s["name"] for s in available_skills]
            catalog_lines.append(f"SKILLS: {', '.join(skill_names)}")

        if not catalog_lines:
            return [], blueprint.get("skill_list", [])

        catalog_section = "\n".join(catalog_lines)

        system = (
            "TASK: Pick tools and skills for an AI agent from the catalog below.\n"
            "OUTPUT: exactly one JSON object, nothing else.\n\n"
            'EXACT FORMAT: {"tools":["tool-name-1","tool-name-2"],"skills":["skill-name"]}\n\n'
            "RULES:\n"
            "- tools and skills are plain string arrays with exact catalog names\n"
            "- pick 1-3 tools and 0-2 skills that match the agent's purpose\n"
            "- use [] if nothing fits — do NOT invent names\n"
            "- NO prose, NO markdown, NO code fences\n\n"
            f"{catalog_section}"
        )

        user = (
            f"Agent: {agent_name}\n"
            f"Purpose: {agent_desc}\n\n"
            "Respond with ONLY the JSON object — start with `{` and end with `}`. "
            "No markdown, no code fences, no commentary."
        )

        try:
            # No assistant-prefill "{" turn — some gateway models reject it (400).
            raw = await _call_llm(
                system,
                [{"role": "user", "content": user}],
                max_tokens=512,
            )

            from app.core.factory_utils import extract_json_block
            data = json.loads(extract_json_block(raw))

            # Extract tool/skill names (handle both strings and dicts)
            def _names(items):
                return [
                    (x["name"] if isinstance(x, dict) else str(x))
                    for x in (items or []) if x
                ]

            # Validate against catalog
            valid_tools = {t["name"] for t in available_tools}
            valid_skills = {s["name"] for s in available_skills}

            tools = [t for t in _names(data.get("tools")) if t in valid_tools]
            skills = [s for s in _names(data.get("skills")) if s in valid_skills]

            # Merge with any skills the blueprint step already picked
            for s in blueprint.get("skill_list", []):
                if s in valid_skills and s not in skills:
                    skills.append(s)

            return tools, skills

        except Exception as exc:
            logger.warning(f'[AGENT] AgentBlueprintGenerator._assign_tools_skills failed: {exc}')
            # The blueprint step may have put raw LLM-invented names in tool_list /
            # skill_list (plain strings, not validated against the catalog).  Returning
            # them as-is lets hallucinated tool names bypass the catalog filter and end
            # up on the assembled agent.  Filter them here against the catalog so the
            # error path is as strict as the happy path.
            valid_tool_names  = {t["name"] for t in available_tools}
            valid_skill_names = {s["name"] for s in available_skills}
            safe_tools  = [t for t in blueprint.get("tool_list",  []) if t in valid_tool_names]
            safe_skills = [s for s in blueprint.get("skill_list", []) if s in valid_skill_names]
            if safe_tools or safe_skills:
                logger.info(f'[AGENT] _assign_tools_skills fallback: kept {safe_tools} tools, {safe_skills} skills (catalog-validated)')
            return safe_tools, safe_skills


# ---------------------------------------------------------------------------
# 4. ToolSkillMatcher
# ---------------------------------------------------------------------------


class ToolSkillMatcher:
    """Scores and ranks tool/skill catalog candidates against requested names.

    Delegates scoring to ``score_catalog_match`` in ``factory_utils`` (shared
    with ``WorkflowSkillMatcher``).  Skills get an LLM semantic fallback via
    ``semantic_catalog_match`` for gaps that string matching misses.
    """

    def __init__(self, _legacy_tools_path: str = "", _legacy_skills_path: str = "") -> None:
        pass

    async def _load_catalog(self) -> tuple[list[dict], list[dict]]:
        from skill_factory.pipeline import catalog_cache
        try:
            return await catalog_cache.get()
        except Exception as exc:
            logger.warning(f'[AGENT] ToolSkillMatcher._load_catalog: {exc}')
            return [], []

    async def match_tools(self, requested: list[str]) -> list[dict]:
        """Return tools from the catalog with score >= MATCH_THRESHOLD."""
        _skills, tools = await self._load_catalog()
        return self._rank(requested, tools)

    async def match_skills(self, requested: list[str]) -> list[dict]:
        """Return skills from the catalog with score >= MATCH_THRESHOLD.
        Falls back to LLM semantic matching for gaps string-matching misses.
        """
        skills, _tools = await self._load_catalog()
        string_matches = self._rank(requested, skills)
        matched_requests = {m["requested_name"] for m in string_matches}

        unmatched = [r for r in requested if r not in matched_requests]
        if unmatched and skills:
            semantic = await self._semantic_match(unmatched, skills)
            string_matches.extend(semantic)

        return string_matches

    async def _semantic_match(self, unmatched: list[str], catalog: list[dict]) -> list[dict]:
        """Ask the LLM if any catalog skill semantically covers an unmatched request."""
        raw_matches = await semantic_catalog_match(unmatched, catalog)

        catalog_by_name = {s["name"]: s for s in catalog}
        results = []
        for m in raw_matches:
            cat_name = m.get("catalog_name")
            req_name = m.get("requested")
            if cat_name and cat_name in catalog_by_name:
                item = catalog_by_name[cat_name]
                results.append({
                    "name": item["name"],
                    "description": item.get("description", ""),
                    "category": item.get("category"),
                    "generated": bool(item.get("generated")),
                    "score": 0.75,
                    "requested_name": req_name,
                    "semantic_match": True,
                })
        return results

    def _rank(self, requested: list[str], catalog: list[dict]) -> list[dict]:
        results: list[dict] = []
        for req in requested:
            best_score, best_item = 0.0, None
            for item in catalog:
                s = score_catalog_match(req, item)
                if s > best_score:
                    best_score, best_item = s, item
                    if s == 1.0:
                        break
            if best_item and best_score >= MATCH_THRESHOLD:
                slim = {
                    "name": best_item.get("name"),
                    "description": best_item.get("description", ""),
                    "input_schema": best_item.get("input_schema") or {},
                    "category": best_item.get("category"),
                    "generated": bool(best_item.get("generated")),
                    "score": best_score,
                    "requested_name": req,
                }
                results.append({k: v for k, v in slim.items() if v is not None})
        return sorted(results, key=lambda x: x["score"], reverse=True)


# ---------------------------------------------------------------------------
# 5. CapabilityAudit
# ---------------------------------------------------------------------------


class CapabilityAudit:
    """Checks if required tools/skills exist in the catalog; flags gaps for generation."""

    def __init__(self, matcher: ToolSkillMatcher) -> None:
        self.matcher = matcher

    async def audit(self, blueprint: dict) -> dict:
        """
        Returns::

            {
                "resolved_tools": list[dict],
                "resolved_skills": list[dict],
                "tool_gaps": list[str],
                "skill_gaps": list[str]
            }

        Async because the matcher now hits postgres.
        """
        tool_list: list[str] = blueprint.get("tool_list", [])
        skill_list: list[str] = blueprint.get("skill_list", [])

        resolved_tools, resolved_skills = await asyncio.gather(
            self.matcher.match_tools(tool_list),
            self.matcher.match_skills(skill_list),
        )

        resolved_tool_names = {m["requested_name"] for m in resolved_tools}
        resolved_skill_names = {m["requested_name"] for m in resolved_skills}

        return {
            "resolved_tools": resolved_tools,
            "resolved_skills": resolved_skills,
            "tool_gaps": [t for t in tool_list if t not in resolved_tool_names],
            "skill_gaps": [s for s in skill_list if s not in resolved_skill_names],
        }


# ---------------------------------------------------------------------------
# 6. DynamicToolGenerator
# ---------------------------------------------------------------------------


class DynamicToolGenerator:
    """
    Generates a Python tool implementation for a capability gap and persists
    it as a row in the postgres ``tools_catalog`` table. No filesystem
    artefacts are written — postgres is the single source of truth.

    The generated code must define ``def run(inputs: dict) -> dict`` (sync
    or async). It is validated by AST before insert; if the LLM returns
    invalid Python (e.g. truncated output) the row is skipped and an
    ``error`` is set on the returned metadata.
    """

    def __init__(self, output_dir: str = "") -> None:
        # output_dir kept for backward compatibility — ignored. Tools no
        # longer get .py files written; the catalog row holds the code.
        pass

    async def generate(self, tool_name: str, description: str) -> dict:
        system = (
            "You are a Python developer. Generate a tool function for an AI agent.\n"
            "Requirements:\n"
            "- Exact signature: def run(inputs: dict) -> dict\n"
            "- Include a docstring with Args and Returns sections\n"
            "- Handle missing/invalid inputs gracefully\n"
            "- Return a dict with a 'result' key (and optionally 'error' key)\n"
            "- The function will run in a fresh subprocess sandbox: stdlib is "
            "available, plus httpx, requests, json, datetime, math, re. Do NOT "
            "rely on local files or the parent process state.\n"
            "- No top-level side effects (no print, no I/O at import time)\n\n"
            "Output your response in two clearly separated sections:\n"
            "1. A JSON block wrapped in ```json ... ``` containing the input_schema "
            "(a valid JSON Schema object with 'type', 'properties', and 'required' keys).\n"
            "2. The Python code wrapped in ```python ... ``` containing only the run() function."
        )
        prompt = (
            f"Tool name: {tool_name}\n"
            f"Description: {description}\n\n"
            "Write the input_schema JSON block first, then the run() function."
        )

        meta = {
            "name": tool_name,
            "description": description,
            "input_schema": {},
            "generated": True,
        }

        try:
            raw = await _call_llm(system, [{"role": "user", "content": prompt}], max_tokens=4096)

            # Extract JSON schema block
            input_schema: dict = {}
            json_match = re.search(r"```json\s*(.*?)\s*```", raw, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    if isinstance(parsed, dict) and "properties" in parsed:
                        input_schema = parsed
                except (json.JSONDecodeError, ValueError):
                    pass

            # Extract Python code block; fall back to stripping all fences
            py_match = re.search(r"```python\s*(.*?)\s*```", raw, re.DOTALL)
            code = py_match.group(1).strip() if py_match else _strip_code_fences(raw)

            if not _validate_run_signature(code):
                logger.error(f'[AGENT] DynamicToolGenerator({tool_name}): generated code does not match required `def run(inputs): ...` signature. Skipping write.')
                meta["error"] = "invalid run() signature — code not persisted"
                return meta

            from app import workflow_repo
            row = await workflow_repo.upsert_tool(
                name=tool_name,
                code=code,
                description=description,
                input_schema=input_schema,
                generated=True,
            )
            logger.info(f"[AGENT] DynamicToolGenerator: persisted {tool_name} to tools_catalog ({len(code)} chars, schema fields: {list(input_schema.get('properties', {}).keys())})")
            # Return the matcher-shaped slim dict so the assembler can
            # carry it on the agent without storing the code blob.
            return {
                "name":         row["name"],
                "description":  row["description"],
                "input_schema": row["input_schema"],
                "generated":    True,
            }
        except Exception as exc:
            logger.error(f'[AGENT] DynamicToolGenerator({tool_name}): {exc}')
            meta["error"] = str(exc)
            return meta


# ---------------------------------------------------------------------------
# 6b. DynamicSkillGenerator
# ---------------------------------------------------------------------------


class DynamicSkillGenerator:
    """
    Generates a structured SKILL.md for a capability gap using the full
    SkillFactory pipeline (blueprint → content → validate → fix fences)
    and stores it in the postgres ``skills_catalog`` table.

    The agent factory already knows *what* skill is needed (gap name +
    agent blueprint context), so we skip the conversational clarification
    step and go straight to blueprint generation.
    """

    def __init__(self, output_dir: str = "") -> None:
        # output_dir kept for backward compatibility — ignored.
        pass

    async def generate(
        self,
        skill_name: str,
        description: str,
        agent_blueprint: Optional[dict] = None,
        skip_eval: bool = False,
    ) -> dict:
        """Generate a skill and persist it to the catalog.

        When ``skip_eval`` is True the SkillEvaluator step (several LLM
        calls) is skipped to reduce latency.
        """
        from skill_factory.pipeline import (
            SkillBlueprintGenerator,
            SkillContentGenerator,
            SkillEvaluator,
            _validate_skill_md,
            _fix_code_fences,
            acquire_skill_gen_lock,
            catalog_cache,
        )

        kebab_name = re.sub(r"[^a-z0-9]+", "-", skill_name.lower()).strip("-")[:64]
        meta = {
            "name": kebab_name,
            "description": description,
            "category": "general",
            "generated": True,
        }

        # Dedup: acquire a per-skill lock so concurrent agent creations don't
        # both generate the same skill simultaneously.
        lock = await acquire_skill_gen_lock(kebab_name)
        async with lock:
            # Check if another coroutine already generated this skill while we waited
            from app import workflow_repo
            try:
                existing = await workflow_repo.get_skill(kebab_name)
                if existing:
                    logger.info(f'[AGENT] DynamicSkillGenerator: {kebab_name} already exists — skipping generation')
                    return {
                        "name":        existing["name"],
                        "description": existing["description"],
                        "category":    existing["category"],
                        "generated":   True,
                        "reused":      True,
                    }
            except Exception as check_exc:
                # Only swallow "not found" — propagate real DB connectivity errors
                err_str = str(check_exc).lower()
                if any(w in err_str for w in ("connect", "timeout", "refused", "host", "uri")):
                    logger.error(f'[AGENT] DynamicSkillGenerator: DB unreachable: {check_exc}')
                    meta["error"] = f"Database unavailable: {check_exc}"
                    return meta
                # Otherwise (e.g. row not found, null result) proceed with generation

            agent_context = ""
            if agent_blueprint:
                agent_context = (
                    f" This skill is needed by an agent whose purpose is: "
                    f"{agent_blueprint.get('purpose', agent_blueprint.get('name', ''))}"
                )

            requirements = {
                "name": kebab_name,
                "display_name": skill_name.replace("_", " ").replace("-", " ").title(),
                "purpose": description + agent_context,
                "triggers": f"when {description.lower()} is needed",
                "input_description": "relevant data or text provided by the agent",
                "output_description": "structured result fulfilling the capability",
                "category": _infer_category(skill_name, description),
                "constraints": "none",
            }

            try:
                blueprint = await SkillBlueprintGenerator().generate(requirements)
                content = await SkillContentGenerator().generate(blueprint)
                content = _fix_code_fences(content)

                valid, msg = _validate_skill_md(content)
                if not valid:
                    logger.warning(f'[AGENT] DynamicSkillGenerator({skill_name}): validation warning: {msg}')

                if not content:
                    meta["error"] = "SkillFactory returned empty content — skill not persisted"
                    return meta

                # Evaluate trigger accuracy before persisting (skipped in
                # workflow-factory context where latency matters more).
                eval_result: dict = {"passed": True, "score": -1, "feedback": []}
                if not skip_eval:
                    eval_result = await SkillEvaluator().evaluate(content, kebab_name)
                    if not eval_result.get("passed", True):
                        logger.warning(f"[AGENT] DynamicSkillGenerator({skill_name}): eval score {eval_result.get('score', 0)}% — {'; '.join(eval_result.get('feedback', []))}")

                row = await workflow_repo.upsert_skill(
                    name=blueprint.get("name", kebab_name),
                    content=content,
                    description=blueprint.get("description", description),
                    category=blueprint.get("category", "general"),
                    generated=True,
                    source="ai",
                )
                # Invalidate catalog cache so next blueprint generation sees the new skill
                catalog_cache.invalidate()

                logger.info(f"""[AGENT] DynamicSkillGenerator: persisted {skill_name} via SkillFactory pipeline ({len(content)} chars, eval: {('skipped' if skip_eval else f"{eval_result.get('score', 0)}%")})""")
                return {
                    "name":        row["name"],
                    "description": row["description"],
                    "category":    row["category"],
                    "generated":   True,
                    "eval_score":  eval_result.get("score"),
                    "eval_feedback": eval_result.get("feedback", []),
                }
            except Exception as exc:
                logger.error(f'[AGENT] DynamicSkillGenerator({skill_name}): {exc}')
                meta["error"] = str(exc)
                return meta


def _infer_category(name: str, description: str) -> str:
    """Heuristic category from skill name + description."""
    text = (name + " " + description).lower()
    if any(w in text for w in ("code", "python", "script", "sql", "api", "debug")):
        return "development"
    if any(w in text for w in ("data", "csv", "json", "parse", "extract", "analyz")):
        return "data"
    if any(w in text for w in ("email", "slack", "message", "notify", "communicat")):
        return "communication"
    if any(w in text for w in ("write", "draft", "generat", "creat", "story", "content")):
        return "creative"
    if any(w in text for w in ("search", "research", "find", "lookup", "web")):
        return "research"
    return "productivity"


# ---------------------------------------------------------------------------
# 7. AgentAssembler
# ---------------------------------------------------------------------------


class AgentAssembler:
    """Combines blueprint + resolved tools/skills into a complete agent config."""

    def __init__(self) -> None:
        pass

    def assemble(
        self,
        blueprint: dict,
        resolved_tools: list[dict],
        resolved_skills: list[dict],
        generated_tools: Optional[list[dict]] = None,
        generated_skills: Optional[list[dict]] = None,
    ) -> dict:
        """
        Returns the final agent configuration dict ready for persistence.

        Tools/skills resolved from the registry and any newly generated
        items are concatenated. ``memory_config`` is set to a sliding-window
        default (informational — actual history slicing is the caller's
        responsibility). ``guardrails`` comes from the blueprint when
        present, otherwise falls back to ``DEFAULT_GUARDRAILS``.
        """
        all_tools = list(resolved_tools) + list(generated_tools or [])
        all_skills = list(resolved_skills) + list(generated_skills or [])

        # Unconditional code_executor auto-injection.
        #
        # Every agent gets code_executor so it can always generate files
        # (PPTX, PDF, DOCX, CSV, charts), run local data transforms, do
        # calculations, or format output — regardless of whatever
        # purpose-built tools it also carries. A user can ask any agent
        # to produce a document, and without a code sandbox the agent
        # simply cannot fulfil that request. Guaranteeing the tool here
        # means document generation works out of the box on every agent,
        # not just blank agents or ones that happen to carry a document
        # skill.
        #
        # The tool description below (and the "Tool Priority" directive
        # further down) still steers the LLM to prefer service-prefixed
        # tools (gitlab_*, jira_*, …) for external I/O and to treat
        # code_executor as the last-resort sandbox for genuine
        # computation / file production — so the escape-hatch guidance
        # is preserved via prompt guidance rather than by withholding
        # the tool entirely.
        #
        #     - If the blueprint EXPLICITLY listed code_executor in its
        #       tools array, respect that (skip the duplicate) — the
        #       user / planner already attached it on purpose.
        existing_tool_names = {t.get("name") for t in all_tools if isinstance(t, dict)}
        _explicitly_requested = "code_executor" in existing_tool_names
        if not _explicitly_requested:
            all_tools.append({
                "name": "code_executor",
                "description": (
                    "LAST-RESORT generic Python sandbox. Execute arbitrary Python in a "
                    "subprocess and return any files it generates. "
                    "DO NOT use code_executor for I/O against an external service "
                    "(GitLab, GitHub, Jira, Confluence, PostgreSQL, Slack, Teams, etc.) "
                    "when a purpose-built tool for that service already exists in your "
                    "toolset — those tools handle auth, retries, SSL, and error shaping "
                    "correctly. Always prefer service-prefixed tools like gitlab_*, "
                    "jira_*, postgres_*, etc. for service operations. If you need broad "
                    "multi-step work against a service, prefer spawn_swarm so a planner "
                    "can pick the right service tools. "
                    "ONLY use code_executor when you genuinely need arbitrary computation "
                    "no other tool covers: generating files (PPTX, PDF, DOCX, CSV, "
                    "charts), local data transforms, calculations, or formatting output."
                ),
                "input_schema": {"type": "object", "properties": {"code": {"type": "string"}}},
                "category": "development",
                "generated": False,
            })

        guardrails = blueprint.get("guardrails")
        if not isinstance(guardrails, dict) or not guardrails:
            guardrails = dict(DEFAULT_GUARDRAILS)

        raw_edits = blueprint.get("suggested_edits") or []
        suggested_edits = [str(e) for e in raw_edits if e][:4] if isinstance(raw_edits, list) else []

        # Runtime config carried from the blueprint's domain-aware defaults.
        # ``_apply_domain_defaults`` guarantees these keys exist and are
        # in-range, so the confirm endpoint / UI can rely on them.
        model_params = blueprint.get("model_params") or {}
        knowledge = blueprint.get("knowledge") or {"mode": "none", "namespaces": []}
        hitl_mode = blueprint.get("hitl_mode") or "off"

        return {
            "agent_id": str(uuid.uuid4()),
            "name": blueprint.get("name", "Unnamed Agent"),
            "description": blueprint.get("description", ""),
            "system_prompt": blueprint.get("system_prompt", "You are a helpful assistant."),
            "tools": all_tools,
            "skills": all_skills,
            "trigger": blueprint.get("trigger", "manual"),
            "persona": blueprint.get("persona", ""),
            # Resolve fresh from env so a live FACTORY_MODEL fix (or a local-only
            # constraint) is reflected in the assembled agent's default model.
            "model": _resolve_factory_model(),
            "model_params": {
                "temperature": model_params.get("temperature", 0.3),
                "max_tokens":  model_params.get("max_tokens", 4096),
                "top_p":       model_params.get("top_p", 0.9),
            },
            "knowledge": {
                "mode":              knowledge.get("mode", "none"),
                "namespaces":        knowledge.get("namespaces", []) or [],
                "suggested_topics":  knowledge.get("suggested_topics", []) or [],
                "reason":            knowledge.get("reason", ""),
            },
            "hitl_mode": hitl_mode,
            "created_at": time.time(),
            "memory_config": dict(DEFAULT_MEMORY_CONFIG),
            "guardrails": guardrails,
            "suggested_edits": suggested_edits,
            "blueprint": blueprint,
        }


# ---------------------------------------------------------------------------
# 7b. AgentFieldPatcher — targeted edits on an already-assembled agent
# ---------------------------------------------------------------------------


class AgentFieldPatcher:
    """Applies a targeted patch to an assembled agent based on a chat message.

    Used by the confirm-stage of ``/agent-factory/chat`` so a user request
    like "make the system prompt more aggressive" changes ONLY the system
    prompt — instead of regenerating the whole blueprint and clobbering any
    manual edits the user has already made in the config panel.

    The LLM is asked to return a JSON object mapping a small set of field
    names to their new values. Any field not present in the response is left
    untouched. Field allow-list is enforced defensively after the call so a
    hallucinated key can't leak into the assembled dict.
    """

    ALLOWED_FIELDS: tuple = (
        "name",
        "description",
        "system_prompt",
        "persona",
        "model",
        "model_params",   # {temperature, max_tokens, top_p}
        "knowledge",      # {mode, namespaces, ...}
        "hitl_mode",      # off | before_tool | after_response
    )

    _NUMERIC_PARAM_KEYS = ("temperature", "max_tokens", "top_p")
    _HITL_VALUES = {"off", "before_tool", "after_response"}
    _KB_MODES = {"none", "existing_kb", "add_kb"}

    # ---- Regex fast-path helpers -------------------------------------
    # Simple numeric / enum requests are extracted deterministically here
    # BEFORE we call the LLM so a small local model can't muddle them.
    # The LLM is still called for anything the regex doesn't cover.

    _RE_MAX_TOKENS = re.compile(
        r"\b(?:max[_\s-]*tokens?|output[_\s-]*(?:token|length|limit)|token[_\s-]*limit)\b"
        r"[^0-9]{0,20}([0-9][0-9,\.]*)\s*(k|thousand)?",
        re.IGNORECASE,
    )
    _RE_TEMPERATURE = re.compile(
        r"\b(?:temperature|temp)\b[^0-9]{0,20}([0-9]*\.?[0-9]+)",
        re.IGNORECASE,
    )
    _RE_TOP_P = re.compile(
        r"\btop[_\s-]*p\b[^0-9]{0,20}([0-9]*\.?[0-9]+)",
        re.IGNORECASE,
    )
    _RE_HITL_OFF = re.compile(
        r"\b(?:turn\s*off|disable|no)\s+(?:human|hitl|approvals?)|\bhitl\s*(?:off|=\s*off)\b",
        re.IGNORECASE,
    )
    _RE_HITL_BEFORE = re.compile(
        r"\b(?:before[_\s-]*tool|approve.*tool|human.*approve|review.*tool)",
        re.IGNORECASE,
    )
    _RE_HITL_AFTER = re.compile(
        r"\bafter[_\s-]*response|review\s+(?:the\s+)?(?:draft|response|output)|human\s+review",
        re.IGNORECASE,
    )

    def _regex_patch(self, message: str) -> dict:
        """Extract numeric / enum edits from the raw message.

        This is the deterministic guard rail: any well-known parameter
        request produces a patch here even if the downstream LLM call
        misinterprets the phrasing. Returns {} when nothing matches.
        """
        out: dict = {}
        mp: dict = {}

        m = self._RE_MAX_TOKENS.search(message or "")
        if m:
            raw = m.group(1).replace(",", "").replace(".", "")
            suffix = (m.group(2) or "").lower()
            try:
                val = int(raw)
                if suffix in ("k", "thousand"):
                    val *= 1000
                mp["max_tokens"] = val
            except ValueError:
                pass

        m = self._RE_TEMPERATURE.search(message or "")
        if m:
            try:
                mp["temperature"] = float(m.group(1))
            except ValueError:
                pass

        m = self._RE_TOP_P.search(message or "")
        if m:
            try:
                mp["top_p"] = float(m.group(1))
            except ValueError:
                pass

        if mp:
            out["model_params"] = mp

        msg = message or ""
        if self._RE_HITL_OFF.search(msg):
            out["hitl_mode"] = "off"
        elif self._RE_HITL_BEFORE.search(msg):
            out["hitl_mode"] = "before_tool"
        elif self._RE_HITL_AFTER.search(msg):
            out["hitl_mode"] = "after_response"

        return out

    async def patch(self, assembled: dict, user_message: str) -> dict:
        """Return a dict of {field: new_value} to merge onto ``assembled``.

        Never raises — returns an empty dict on any parse/LLM failure so the
        caller can fall back to "no change" behaviour. The returned dict is
        already sanitised to the allow-list and to the correct value shapes.

        Numeric / enum requests (max_tokens, temperature, top_p, hitl_mode)
        are first extracted by regex so a small local LLM's misclassification
        can't drop the change; the LLM is still called for text-shaped fields
        (name, description, system_prompt) and to catch anything the regex
        missed. Results merge — the LLM cannot overwrite a regex-extracted
        numeric value with something else.
        """
        regex_patch = self._sanitise(self._regex_patch(user_message))

        current_snapshot = {
            "name":          assembled.get("name", ""),
            "description":   assembled.get("description", ""),
            "system_prompt": assembled.get("system_prompt", ""),
            "persona":       assembled.get("persona", ""),
            "model":         assembled.get("model", ""),
            "model_params":  assembled.get("model_params") or {},
            "knowledge":     assembled.get("knowledge") or {"mode": "none", "namespaces": []},
            "hitl_mode":     assembled.get("hitl_mode", "off"),
        }

        system = (
            "You are editing the configuration of an AI agent that is already assembled.\n"
            "The user will describe a change. Identify EXACTLY which fields they want to modify\n"
            "and return ONLY those fields with their new values. Do not touch anything else.\n\n"
            "Allowed fields (return a subset — omit fields the user did not ask to change):\n"
            '  - "name":          string (agent display name)\n'
            '  - "description":   string (short one-line description)\n'
            '  - "system_prompt": string (full instructions / persona / behaviour)\n'
            '  - "persona":       string (optional tone/persona note appended to instructions)\n'
            '  - "model":         string (exact model id, e.g. "claude-sonnet-4-6"). '
            'Only include this field if the user\'s message explicitly names a real '
            'model / provider / family (e.g. "haiku", "opus", "gpt-5", "gemini"). '
            'NEVER echo a vague or empty word back as a model id (e.g. do NOT return '
            '"model", "the model", "new", "default").\n'
            '  - "model_params":  { "temperature": 0-1 float, "max_tokens": int, "top_p": 0-1 float } '
            '(you may include only the keys the user asked to change)\n'
            '  - "knowledge":     { "mode": "none"|"existing_kb"|"add_kb", "namespaces": [str] }\n'
            '  - "hitl_mode":     "off" | "before_tool" | "after_response"\n\n'
            "PHRASING GUIDE — recognise these user phrasings as model_params edits:\n"
            "  * \"change max tokens to 32000\" / \"set max_tokens 8000\" / \"raise the output "
            "limit to 16k\" / \"more tokens\" → model_params.max_tokens\n"
            "  * \"lower temperature\" / \"temp 0.2\" / \"more creative\" (→ higher) / \"more "
            "deterministic\" (→ lower) → model_params.temperature\n"
            "  * \"top_p 0.7\" / \"nucleus sampling 0.9\" → model_params.top_p\n"
            "  * \"switch to claude-haiku\" / \"use gpt-oss\" / \"try opus\" → model\n"
            "  * \"require human approval before tools\" → hitl_mode=\"before_tool\"\n"
            "  * \"review drafts before sending\" → hitl_mode=\"after_response\"\n"
            "  * \"turn off human review\" → hitl_mode=\"off\"\n\n"
            "Rules:\n"
            "  * Return ONLY valid JSON — no markdown, no commentary.\n"
            "  * If the user asked to change ONLY the system prompt, return {\"system_prompt\": \"...\"}\n"
            "    and NOTHING ELSE.\n"
            "  * If the user asked to change the model + temperature, return\n"
            "    {\"model\": \"...\", \"model_params\": {\"temperature\": 0.2}}.\n"
            "  * For max_tokens, extract the raw integer (handle formats like \"32000\", \"32k\",\n"
            "    \"32,000\", \"8000\"). Never round or clamp yourself — return the number the\n"
            "    user asked for.\n"
            "  * Never change fields the user did not mention.\n"
            "  * If the request is genuinely unrelated to any allowed field, return {}.\n"
            "  * When rewriting the system_prompt, produce the FULL new prompt (not a diff).\n\n"
            "Current values:\n"
            f"{json.dumps(current_snapshot, indent=2, ensure_ascii=False)}\n"
        )
        try:
            raw = await _call_llm(
                system,
                [{"role": "user", "content": (user_message or "")[:4000]}],
                max_tokens=2048,
            )
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict):
                parsed = {}
        except SecurityGatewayRejection:
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] AgentFieldPatcher.patch: {exc}')
            parsed = {}

        llm_patch = self._sanitise(parsed)

        # Regex wins for numeric / enum values it recognised — the LLM must
        # NOT overwrite an extracted temperature/max_tokens/top_p/hitl_mode
        # with something else. Text-shaped fields (name, description,
        # system_prompt, persona, model, knowledge) always come from the LLM.
        merged = dict(llm_patch)
        for field_name, value in regex_patch.items():
            if field_name == "model_params":
                base = dict(merged.get("model_params") or {})
                base.update(value)  # regex keys overwrite LLM keys
                merged["model_params"] = base
            else:
                merged[field_name] = value
        return merged

    def _sanitise(self, patch: dict) -> dict:
        """Filter to allow-list and coerce values into their canonical shapes."""
        out: dict = {}
        for field_name, value in patch.items():
            if field_name not in self.ALLOWED_FIELDS:
                continue
            if field_name in ("name", "description", "system_prompt", "persona", "model"):
                if isinstance(value, str) and value.strip():
                    out[field_name] = value.strip() if field_name != "system_prompt" else value
            elif field_name == "model_params":
                if not isinstance(value, dict):
                    continue
                mp: dict = {}
                for k in self._NUMERIC_PARAM_KEYS:
                    if k not in value:
                        continue
                    try:
                        if k == "max_tokens":
                            mp[k] = int(float(value[k]))
                        else:
                            mp[k] = float(value[k])
                    except (TypeError, ValueError):
                        continue
                # Clamp to sane ranges — matches _apply_domain_defaults.
                if "temperature" in mp:
                    mp["temperature"] = max(0.0, min(1.0, mp["temperature"]))
                if "top_p" in mp:
                    mp["top_p"] = max(0.0, min(1.0, mp["top_p"]))
                if "max_tokens" in mp:
                    mp["max_tokens"] = max(1, min(200000, mp["max_tokens"]))
                if mp:
                    out["model_params"] = mp
            elif field_name == "knowledge":
                if not isinstance(value, dict):
                    continue
                mode = str(value.get("mode") or "none").lower()
                if mode not in self._KB_MODES:
                    mode = "none"
                ns = value.get("namespaces") or []
                if not isinstance(ns, list):
                    ns = []
                out["knowledge"] = {
                    "mode": mode,
                    "namespaces": [str(n) for n in ns if n],
                }
            elif field_name == "hitl_mode":
                v = str(value).strip().lower()
                if v in self._HITL_VALUES:
                    out["hitl_mode"] = v
        return out

    @staticmethod
    def apply(assembled: dict, patch: dict) -> dict:
        """Merge ``patch`` onto a copy of ``assembled`` and return it.

        ``model_params`` and ``knowledge`` are merged shallowly so a patch
        that only sets ``{"temperature": 0.2}`` inside ``model_params``
        keeps ``max_tokens`` and ``top_p`` unchanged.
        """
        merged = dict(assembled)
        for k, v in (patch or {}).items():
            if k == "model_params":
                base = dict(merged.get("model_params") or {})
                base.update(v)
                merged["model_params"] = base
            elif k == "knowledge":
                base = dict(merged.get("knowledge") or {})
                base.update(v)
                merged["knowledge"] = base
            else:
                merged[k] = v
        return merged


# ---------------------------------------------------------------------------
# 8. AgentRegistry
# ---------------------------------------------------------------------------


class AgentRegistry:
    """Persists agents to a JSON file. Supports save / load / list / delete."""

    def __init__(self, storage_path: str) -> None:
        self.path = Path(storage_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    def save(self, agent_config: dict) -> str:
        """Persist agent_config and return its agent_id."""
        agents = self._read()
        agent_id: str = agent_config.get("agent_id") or str(uuid.uuid4())
        agent_config["agent_id"] = agent_id
        agents[agent_id] = agent_config
        self._write(agents)
        return agent_id

    def load(self, agent_id: str) -> Optional[dict]:
        """Return agent config or None if not found."""
        return self._read().get(agent_id)

    def list_agents(self) -> list[dict]:
        """Return all persisted agent configs."""
        return list(self._read().values())

    def delete(self, agent_id: str) -> bool:
        """Remove agent. Returns True if it existed."""
        agents = self._read()
        if agent_id not in agents:
            return False
        del agents[agent_id]
        self._write(agents)
        return True


# ---------------------------------------------------------------------------
# 9. AgentRunner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 9a. ToolDispatcher
# ---------------------------------------------------------------------------


# Map "shorthand" Python type names found in registry input_schema dicts
# (e.g. {"query": "string"}) to JSON-Schema type strings.
_SCHEMA_TYPE_MAP = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "dict": "object",
    "object": "object",
}


# ---------------------------------------------------------------------------
# Deny-by-default env for the two credential-free, network-isolated tools
# (security review F-02 follow-up).
#
# ``core.platform_credentials.sanitized_environ()`` only strips the 5
# managed integration-credential keys (GITLAB_TOKEN, JIRA_*, CONFLUENCE_*) —
# it is an ALLOWLIST-BY-OMISSION, and every secret not explicitly added to
# that frozenset (e.g. AZURE_AD_CLIENT_SECRET, which doubles as the M365
# bridge token — see m365_tools.py) still flows through into every tool's
# subprocess, including code_executor / execute_code. That's fine for tools
# that legitimately need those secrets, but code_executor / execute_code
# need essentially NOTHING from the platform's secret surface: their
# documented job is generating/manipulating files in OUTPUT_DIR, and they
# have no code path that calls any platform-integrated service. So for just
# these two tools, strip every environment variable whose NAME looks like a
# credential (by suffix pattern, matching the project's own naming
# convention across .env: *_SECRET, *_TOKEN, *_KEY, *_PASSWORD, *_PWD),
# rather than relying on a hand-maintained "keys we remembered to add"
# denylist. This is deliberately pattern-based instead of exhaustively
# enumerated so a NEW secret introduced later (a fresh integration's API
# key, say) is stripped automatically without anyone having to remember to
# update this file.
_SECRET_ENV_NAME_RE = re.compile(
    r"(SECRET|TOKEN|_KEY|PASSWORD|_PWD)$", re.IGNORECASE
)
# A few non-secret env vars happen to match the pattern above (integer
# config values, not credentials) — keep them so code_executor's documented
# behaviour (budget/timeout awareness, etc.) is unaffected by this filter.
_SECRET_ENV_NAME_ALLOWED = frozenset({"BUDGET_DEFAULT_TOKENS"})


def _strip_secret_shaped_env(env: dict) -> dict:
    """Return ``env`` with every credential-shaped variable name removed.

    Used only for code_executor / execute_code — see module comment above.
    """
    return {
        k: v for k, v in env.items()
        if k in _SECRET_ENV_NAME_ALLOWED or not _SECRET_ENV_NAME_RE.search(k)
    }


def _rehome_generated_files(parsed: dict, user_id: str) -> dict:
    """Move a tool's just-created artifacts into the caller's owner-dir.

    Broken Access Control / IDOR fix. The file-producing tools
    (``code_executor`` / ``execute_code``) run in a sandbox that is
    DELIBERATELY denied the user's identity (``AINXT_USER_ID`` is not injected
    for credential-free tools — see the dispatch env-setup below). So we cannot
    tag files by owner inside the sandbox without regressing that hardening.
    Instead we do it HERE, in the parent, which already knows ``user_id``:
    each artifact the tool reported is relocated from its flat
    ``GENERATED_FILES_DIR/{name}`` location into
    ``GENERATED_FILES_DIR/{owner_tag}/{name}`` and its ``disk_name`` /
    ``download_url`` are rewritten to match. The download endpoint then only
    serves a file to the user whose ``owner_tag`` it lives under.

    No-op (files stay flat) when ``user_id`` is empty — those remain reachable
    as legacy flat files. Best-effort and never raises: a re-home failure
    leaves the entry pointing at the still-valid flat path.
    """
    if not user_id:
        return parsed
    files = parsed.get("generated_files")
    if not isinstance(files, list) or not files:
        return parsed
    try:
        from urllib.parse import quote
        from app.main import rehome_generated_file
    except Exception:
        # main not importable in this context — leave artifacts flat.
        return parsed
    for entry in files:
        if not isinstance(entry, dict):
            continue
        disk_name = entry.get("disk_name")
        if not disk_name:
            continue
        try:
            new_key = rehome_generated_file(disk_name, user_id)
        except Exception:
            continue
        if new_key and new_key != disk_name:
            entry["disk_name"] = new_key
            entry["download_url"] = f"/generated-files/{quote(new_key, safe='/')}"
    return parsed


# ---------------------------------------------------------------------------
# Optional OS-level network isolation for the sandbox subprocess
# (security review F-02, layer 2).
#
# The in-process socket guard in platform_tools.py (_guarded_connect) runs
# inside the SAME interpreter that then exec()s LLM-generated code, so it can
# be undone from within that code — it is a speed bump, not a boundary. The
# process boundary the dispatcher already provides (a fresh `python -I`
# subprocess per call) is real, but today nothing stops that subprocess from
# opening its own sockets.
#
# code_executor / execute_code are the two tools whose documented job
# (generate/manipulate files, run arbitrary code) never requires network
# access, so we can safely deny ALL network egress at the OS level for their
# subprocess via `bwrap --unshare-net`, rather than trying to allow/deny
# specific hosts (which just re-creates the same guard we're trying to
# replace). This is Linux-only and requires the `bubblewrap` package to be
# installed in the runtime image; it's disabled by default and opt-in via
# SANDBOX_ISOLATION=bwrap so it can be validated in UAT before enabling in
# prod, and so Windows dev boxes (which don't have bwrap) keep working
# unchanged. When the flag is unset or the binary is missing, the sandbox
# runs exactly as it did before this change — no regression.
_SANDBOX_ISOLATION = os.getenv("SANDBOX_ISOLATION", "").strip().lower()
_NETWORK_ISOLATED_TOOLS = frozenset({"code_executor", "execute_code"})


def _bwrap_available() -> Optional[str]:
    """Return the resolved path to the ``bwrap`` binary, or None if unusable.

    Cached at module scope — this is checked on every dispatch, and the
    result can't change during the process lifetime.
    """
    if _SANDBOX_ISOLATION != "bwrap":
        return None
    import shutil as _shutil
    path = _shutil.which("bwrap")
    if not path:
        logger.warning(
            "[AGENT] SANDBOX_ISOLATION=bwrap is set but the 'bwrap' binary "
            "was not found on PATH — falling back to the in-process network "
            "guard only. Install the 'bubblewrap' package to enable this."
        )
    return path


_BWRAP_PATH = _bwrap_available()


def _wrap_with_bwrap(argv: list, sandbox_cwd: str) -> list:
    """Prefix ``argv`` with a bubblewrap invocation that denies all network
    egress from the child while preserving filesystem access (read-only root,
    writable sandbox CWD and system temp — code_executor needs to write its
    generated files and pip may need to cache wheels on first use).

    Only ever called when ``_BWRAP_PATH`` is set, i.e. SANDBOX_ISOLATION=bwrap
    AND the binary is present.
    """
    import tempfile
    return [
        _BWRAP_PATH,
        "--unshare-net",        # the actual containment: no sockets, period.
        "--die-with-parent",    # don't leak an orphaned sandbox if we're killed
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", sandbox_cwd, sandbox_cwd,
        "--bind", tempfile.gettempdir(), tempfile.gettempdir(),
        "--chdir", sandbox_cwd,
        "--",
        *argv,
    ]


class ToolDispatcher:
    """
    Fetches tool code from the postgres ``tools_catalog`` and executes it
    in a fresh subprocess sandbox per dispatch call.

    Sandbox model
    -------------
    Each ``dispatch()`` spawns a separate Python interpreter via
    ``asyncio.create_subprocess_exec`` running a small wrapper. The tool
    code (read from postgres) is passed as a base64-encoded argv argument;
    the inputs dict is passed as JSON on stdin; the result is read as
    JSON on stdout. A wall-clock timeout (default 15s) hard-kills the
    subprocess if it runs too long.

    What this protects against
    --------------------------
    * Memory leaks in the tool code can't grow the parent process
    * Infinite loops are killed by the timeout
    * The tool can't directly mutate parent state, the AgentRegistry, or
      the postgres pool

    What it does NOT protect against
    --------------------------------
    * Network egress (the sandbox can still hit the internet)
    * Filesystem reads/writes (no chroot)
    * High CPU during the timeout window
    * Resource exhaustion via huge stdout payloads (capped at 1 MB below)

    For stronger isolation, swap ``_run_in_sandbox`` for a Docker-based
    runner. The interface here is identical.
    """

    # Enterprise-grade sandbox timeout: 300s (5 min). Tools that wrap
    # generate-with-AI calls or chain into attached agents/workflows need
    # headroom beyond the previous 120s so a slow LLM response or a
    # downstream workflow doesn't kill the whole tool dispatch. Overridable
    # at call time via ``dispatch(..., timeout=...)`` and bounded by the
    # outer wall-clock ceiling enforced in ``AGENT_TOOL_TIMEOUT`` env var.
    DEFAULT_TIMEOUT_S = float(os.getenv("AGENT_TOOL_TIMEOUT", "300"))
    MAX_OUTPUT_BYTES = 1_000_000  # 1 MB cap on stdout

    # Retry policy for transient tool failures (subprocess spawn errors,
    # timeouts, network errors from inside the sandbox, HTTP 5xx from
    # downstream services). Deterministic failures — HTTP 4xx, auth
    # rejections, "not found", validation errors — are NOT retried;
    # those would just burn time producing the same answer.
    TOOL_MAX_ATTEMPTS = int(os.getenv("TOOL_MAX_ATTEMPTS", "5"))
    TOOL_RETRY_BASE_DELAY = float(os.getenv("TOOL_RETRY_BASE_DELAY", "1.0"))
    TOOL_RETRY_MAX_DELAY = float(os.getenv("TOOL_RETRY_MAX_DELAY", "8.0"))

    # Wrapper that runs inside the sandbox subprocess. It receives:
    #   sys.argv[1] = base64-encoded tool source code
    #   stdin       = JSON-encoded inputs dict
    # and writes one line of JSON to stdout.
    # The try/except around ``fn(inputs)`` is critical: SystemExit raised by tool
    # code (e.g. a skill script run via runpy.run_path that ends in
    # ``sys.exit(main())``) is a BaseException, so if it escapes this wrapper it
    # kills the sandbox subprocess BEFORE the result is printed. The dispatcher
    # then sees exit-0 with empty stdout ("produced no output") and burns all 5
    # retries. Catching it guarantees we always emit a JSON envelope. A zero/None
    # exit code is a clean exit; only a nonzero code is surfaced as an error.
    _SANDBOX_WRAPPER = (
        "import sys, json, base64, asyncio, inspect, traceback\n"
        "code = base64.b64decode(sys.argv[1]).decode('utf-8')\n"
        "raw = sys.stdin.read() or '{}'\n"
        "inputs = json.loads(raw)\n"
        "ns = {'__name__': '__sandbox__'}\n"
        "exec(compile(code, '<sandbox>', 'exec'), ns)\n"
        "fn = ns.get('run')\n"
        "if not callable(fn):\n"
        "    print(json.dumps({'error': \"tool has no callable run()\"}))\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    result = fn(inputs)\n"
        "    if inspect.iscoroutine(result):\n"
        "        result = asyncio.run(result)\n"
        "except SystemExit as _se:\n"
        "    _c = _se.code\n"
        "    result = {'error': 'tool exited with code %r' % _c} if _c not in (0, None) else {'message': 'tool exited cleanly', 'stdout': ''}\n"
        "except BaseException:\n"
        "    result = {'error': traceback.format_exc()[:2000]}\n"
        "print(json.dumps(result, default=str))\n"
    )

    def __init__(self, *_legacy_args, **_legacy_kwargs) -> None:
        # Legacy positional/kwargs accepted for backward compat — ignored.
        # The catalog now lives in postgres.
        pass

    # ------------------------------------------------------------------
    @staticmethod
    def _input_schema_to_json_schema(schema: Any) -> dict:
        """
        Coerce shorthand ``input_schema`` (e.g.
        ``{"query": "string", "max_results": "integer"}``) into a JSON
        Schema object suitable for LLM function-calling. If the schema is
        already JSON-Schema-shaped (has ``type`` and ``properties``),
        return as-is.
        """
        if not isinstance(schema, dict) or not schema:
            return {"type": "object", "properties": {}}
        if "type" in schema and "properties" in schema:
            return dict(schema)

        properties: dict = {}
        for key, value in schema.items():
            if isinstance(value, dict):
                properties[key] = value
            elif isinstance(value, str):
                json_type = _SCHEMA_TYPE_MAP.get(value.lower(), "string")
                prop = {"type": json_type}
                if json_type == "array":
                    prop["items"] = {"type": "string"}
                properties[key] = prop
            else:
                properties[key] = {"type": "string"}
        return {"type": "object", "properties": properties, "required": []}

    def build_tool_definitions(self, tools: list[dict]) -> list[dict]:
        """
        Convert agent['tools'] slim metadata into the LLM function-calling
        schema. Each entry: {"name", "description", "parameters": <JSON-schema>}.
        """
        defs: list[dict] = []
        for t in tools or []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            params = self._input_schema_to_json_schema(
                t.get("input_schema") or t.get("parameters")
            )
            defs.append({
                "name": t["name"],
                "description": (t.get("description") or "")[:500],
                "parameters": params,
            })
        return defs

    # ------------------------------------------------------------------
    @staticmethod
    def _is_transient_tool_error(result: dict) -> bool:
        """Decide whether a tool result represents a *transient* failure
        worth retrying. We retry on:
          • sandbox timeouts (LLM/external system was too slow)
          • spawn / non-JSON / crash failures (process-level flakiness)
          • network errors surfaced inside the sandbox
          • HTTP 5xx and 429 from downstream services

        We do NOT retry on:
          • HTTP 4xx (except 408/429) — caller bug or auth issue
          • "not found", validation errors, permission errors
          • Success (no "error" key)
        """
        if not isinstance(result, dict):
            return False
        err = (result.get("error") or "").lower()
        if not err:
            return False

        # Deterministic failures — never retry
        deterministic_markers = (
            "not found", "not configured", "required",
            "permission", "forbidden", "unauthorized",
            "invalid", "malformed", "validation",
            "http 400", "http 401", "http 403", "http 404",
            "http 405", "http 409", "http 410", "http 422",
        )
        if any(m in err for m in deterministic_markers):
            return False

        # Transient markers — retry
        transient_markers = (
            "timed out", "timeout",
            "unreachable", "connection", "connect", "refused", "reset",
            "temporarily", "temporary", "retry",
            "could not start sandbox", "crashed", "no output", "non-json",
            "http 408", "http 429",
            "http 500", "http 502", "http 503", "http 504",
        )
        return any(m in err for m in transient_markers)

    @classmethod
    def _tool_backoff(cls, attempt: int) -> float:
        """Exponential backoff for tool retries: 1s, 2s, 4s, 8s, 8s."""
        return min(
            cls.TOOL_RETRY_BASE_DELAY * (2 ** attempt),
            cls.TOOL_RETRY_MAX_DELAY,
        )

    # ------------------------------------------------------------------
    async def dispatch(
        self,
        tool_name: str,
        inputs: dict,
        timeout: Optional[float] = None,
        user_id: str = "",
        email: str = "",
        loop_ctx: Optional[dict] = None,
        workflow_artifact_dir: str = "",
        sample_doc_path: str = "",
        sample_doc_kind: str = "",
    ) -> dict:
        """
        Look up ``tool_name`` in ``tools_catalog``, run its code in a
        sandbox subprocess with the provided inputs, and return the result.

        Transient sandbox / network failures are retried up to
        ``TOOL_MAX_ATTEMPTS`` (default 5) with exponential backoff. After
        the cap is exceeded, the final error is annotated with the attempt
        count so the agent (and the UI surface) can show a clear message.

        Errors are returned as ``{"error": <msg>}`` so the LLM can react.

        ``loop_ctx`` (optional) is set by the Loop runner only. When present
        it surfaces ``loop_run_id`` as ``LOOP_RUN_ID`` in the sandbox env
        for audit. Callers outside the Loop runner pass ``loop_ctx=None``
        (default) and behaviour is unchanged.
        """
        from app import workflow_repo
        try:
            tool = await workflow_repo.get_tool(tool_name)
        except Exception as exc:
            return {"error": f"Failed to fetch tool '{tool_name}' from catalog: {exc}"}

        if not tool:
            return {"error": f"Tool '{tool_name}' not found in tools_catalog"}

        # M365 tools: call connector_registry directly (same as Buddy/Cowork orchestrator).
        # The sandbox subprocess runs python -I so it cannot import connectors.*;
        # running in-process avoids the HTTP round-trip and any proxy interference.
        # asyncio.to_thread keeps the event loop free during blocking Graph API calls.
        if tool.get("service") == "microsoft_365":
            if not user_id:
                return {"error": "No user context; cannot call Microsoft 365."}
            try:
                from connectors.registry import connector_registry
                bare_tool = tool_name.split("__")[-1] if "__" in tool_name else tool_name
                result = await asyncio.to_thread(
                    connector_registry.execute,
                    "microsoft_365", bare_tool, inputs or {}, user_id, "", None,
                )
            except Exception as exc:
                logger.error(f"[AGENT] M365 direct dispatch failed for {tool_name}: {exc}")
                return {"error": f"Microsoft 365 call failed: {exc}"}
            return result.to_dict()

        code = tool.get("code") or ""
        if not code.strip():
            return {"error": f"Tool '{tool_name}' has empty code"}

        effective_timeout = timeout or self.DEFAULT_TIMEOUT_S
        last_result: dict = {}
        for attempt in range(self.TOOL_MAX_ATTEMPTS):
            last_result = await self._run_in_sandbox(
                tool_name, code, inputs, effective_timeout,
                user_id=user_id, email=email,
                loop_ctx=loop_ctx,
                workflow_artifact_dir=workflow_artifact_dir,
                sample_doc_path=sample_doc_path,
                sample_doc_kind=sample_doc_kind,
            )
            # Success → return immediately
            if not (isinstance(last_result, dict) and last_result.get("error")):
                if attempt > 0:
                    logger.info(f"[AGENT] Tool '{tool_name}' succeeded on attempt {attempt + 1}/{self.TOOL_MAX_ATTEMPTS}")
                return last_result

            # Deterministic failure → don't waste attempts retrying
            if not self._is_transient_tool_error(last_result):
                return last_result

            if attempt < self.TOOL_MAX_ATTEMPTS - 1:
                delay = self._tool_backoff(attempt)
                logger.warning(f"[AGENT] Tool '{tool_name}' failed transiently ({str(last_result.get('error'))[:200]}); attempt {attempt + 1}/{self.TOOL_MAX_ATTEMPTS} — retrying in {delay}s")
                await asyncio.sleep(delay)

        # Retry budget exhausted — surface a clear, final error message
        original = str(last_result.get("error") or "unknown error")
        logger.error(f"[AGENT] Tool '{tool_name}' failed after {self.TOOL_MAX_ATTEMPTS} attempts: {original[:300]}")
        final = dict(last_result)
        final["error"] = (
            f"Tool '{tool_name}' failed after {self.TOOL_MAX_ATTEMPTS} "
            f"attempts. Last error: {original}"
        )
        final["attempts"] = self.TOOL_MAX_ATTEMPTS
        final["retry_exhausted"] = True
        return final

    # ------------------------------------------------------------------
    async def _run_in_sandbox(
        self,
        tool_name: str,
        code: str,
        inputs: dict,
        timeout: float,
        user_id: str = "",
        email: str = "",
        loop_ctx: Optional[dict] = None,
        workflow_artifact_dir: str = "",
        sample_doc_path: str = "",
        sample_doc_kind: str = "",
    ) -> dict:
        """Run the sandbox wrapper in a worker thread.

        We deliberately use the *blocking* ``subprocess.run`` (wrapped in
        ``asyncio.to_thread``) instead of ``asyncio.create_subprocess_exec``
        because the latter requires the Proactor event loop on Windows,
        which clashes with the Selector loop psycopg needs for async
        Postgres. The thread-pool approach works under every event-loop
        policy on every platform — at the cost of one short-lived OS
        thread per tool dispatch, which is fine for our use case.
        """
        import subprocess

        # Base env has all platform-level integration secrets (GITLAB_TOKEN,
        # JIRA_API_TOKEN, …) stripped so they can NEVER act as a fallback for a
        # user who has no configured token. Every git/Jira operation must be
        # authorised with the requesting user's OWN token or fail with a clear
        # "not configured" error.
        try:
            from core.platform_credentials import sanitized_environ
            base_env = sanitized_environ()
        except Exception:
            base_env = dict(os.environ)

        # ── Credential-free tools (security review F-02) ────────────────
        # code_executor / execute_code run raw LLM-generated Python/bash in a
        # subprocess whose ONLY containment today is this process boundary
        # (see the in-process socket guard in platform_tools.py, which is
        # bypassable from inside the same interpreter). Because that guard is
        # not a hard boundary, a hostile prompt that manages to escape it must
        # NOT find any per-user credential (GitLab PAT, Atlassian token, org
        # vault secrets, or the M365 bridge identity) sitting in its own
        # environment — that would turn a code-exec bypass directly into
        # credential exfiltration. Neither tool needs a user credential to do
        # its documented job (writing files); any legitimate GitLab/Jira/M365
        # action goes through the dedicated tools (gitlab_write_file,
        # jira_update_issue, outlook_send_mail, …), which keep their
        # credentials as before.
        _NO_CREDENTIAL_TOOLS = frozenset({"code_executor", "execute_code"})
        _skip_credentials = tool_name in _NO_CREDENTIAL_TOOLS

        if _skip_credentials:
            conn_env: dict = {}
            # Also strip every credential-shaped var out of base_env itself —
            # sanitized_environ() only removes the 5 MANAGED_CREDENTIAL_ENV_KEYS
            # (GitLab/Atlassian), so AZURE_AD_CLIENT_SECRET (the M365 bridge
            # token), FERNET_KEY, JWT_SECRET, etc. would otherwise still reach
            # this subprocess untouched. See _strip_secret_shaped_env's module
            # comment for why this is pattern-based rather than enumerated.
            base_env = _strip_secret_shaped_env(base_env)
        else:
            # Fetch connection credentials from the main platform's API Token
            # Vault and merge into subprocess environment.
            try:
                from app import workflow_repo as _wr
                conn_env = await _wr.get_all_connection_env_vars(user_id=user_id, email=email)
            except Exception as exc:
                logger.warning(f'[AGENT] ToolDispatcher: credential injection failed for user={user_id}: {exc}')
                conn_env = {}

        sandbox_env = {**base_env, **conn_env}
        # Caller identity injection. The M365 tool shim
        # (``app/tools/m365_tools.py`` ``_SHIM``) uses ``AINXT_USER_ID`` to
        # authorise every Graph call against THIS user's own OAuth token via
        # the platform's /connectors/execute bridge. ``python -I`` ignores
        # PYTHONPATH/user-site but still passes the process env, so this
        # injection reaches the tool. Never logged (secrets).
        #
        # The bridge URL/token themselves come from ``PLATFORM_BASE_URL`` and
        # ``AZURE_AD_CLIENT_SECRET`` (reused as the X-Bridge-Token — see
        # ``m365_tools.py`` ``_SHIM``). Neither is in
        # ``platform_credentials.MANAGED_CREDENTIAL_ENV_KEYS``, so both flow
        # through the sanitized base env into the sandbox automatically. No
        # explicit injection needed here.
        #
        # Skipped for credential-free tools too: AINXT_USER_ID is itself an
        # authorization delegation credential (it lets the bridge act as this
        # user), so code_executor/execute_code must not receive it either.
        if user_id and not _skip_credentials:
            sandbox_env["AINXT_USER_ID"] = str(user_id)
        if email and not _skip_credentials:
            sandbox_env["AINXT_USER_EMAIL"] = str(email)
        if workflow_artifact_dir:
            sandbox_env["WORKFLOW_ARTIFACT_DIR"] = workflow_artifact_dir
        if os.getenv("RUNTIME_ARTIFACTS_DIR"):
            sandbox_env["RUNTIME_ARTIFACTS_DIR"] = os.getenv("RUNTIME_ARTIFACTS_DIR", "")
        # Per-agent Sample Document — see app/api/agent_sample.py and
        # skill_manifest.sample_doc_directive. The path is exposed to
        # both credential-bearing and credential-free tools: it points at
        # a file the user themselves uploaded, so there's nothing here
        # for a hostile prompt to escalate to. Also add SAMPLE_DOC_DIR
        # so ``document_tools._resolve_allowed_roots`` (which is a
        # read-only path guard, not a secret) can accept a
        # ``read_document({"file_path": SAMPLE_DOC_PATH})`` call.
        if sample_doc_path:
            sandbox_env["SAMPLE_DOC_PATH"] = sample_doc_path
            sandbox_env["SAMPLE_DOC_DIR"]  = os.path.dirname(sample_doc_path)
            if sample_doc_kind:
                sandbox_env["SAMPLE_DOC_KIND"] = sample_doc_kind

        # Document generation is Python-only (see code_executor description and
        # the docx/pptx/pdf skills). We deliberately do NOT probe for npm /
        # inject NODE_PATH: Node.js is not a supported runtime here, the probe
        # spawned an `npm root -g` subprocess on every call (wasted work — and a
        # up-to-5s stall on hosts where npm hangs), and advertising it lured the
        # LLM into writing JavaScript that crashes the sandbox.

        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        stdin_bytes = json.dumps(inputs or {}, default=str).encode("utf-8")

        # Pin sandbox CWD to GENERATED_FILES_DIR so relative-path writes
        # from LLM-generated code (e.g. Presentation().save("deck.pptx"))
        # can't escape ABStudio/tmp onto the drive root that uvicorn was
        # launched from — common on Windows dev boxes. The in-sandbox
        # os.chdir guard in platform_tools._CODE_EXECUTOR_CODE handles the
        # 99% case; this OS-level pin is the belt-and-suspenders layer for
        # atexit handlers, sub-subprocesses, and chdir-restoration races.
        sandbox_cwd = sandbox_env.get("GENERATED_FILES_DIR") or os.getcwd()
        try:
            os.makedirs(sandbox_cwd, exist_ok=True)
        except OSError:
            sandbox_cwd = os.getcwd()

        # Loop-Engineering context propagation. When the Loop runner passes
        # ``loop_ctx``, surface ``LOOP_RUN_ID`` for audit. Everything else
        # stays unchanged for non-Loop callers.
        if loop_ctx:
            sandbox_env["LOOP_RUN_ID"] = str(loop_ctx.get("loop_run_id") or "")

        argv = [sys.executable, "-I", "-c", self._SANDBOX_WRAPPER, encoded]
        # Deny all network egress at the OS level for code_executor/execute_code
        # when SANDBOX_ISOLATION=bwrap is enabled and the binary is present
        # (see the module-level comment above ToolDispatcher for rationale).
        # No-op — argv unchanged — everywhere else (default, Windows dev,
        # or bwrap missing), preserving current behaviour exactly.
        if _BWRAP_PATH and tool_name in _NETWORK_ISOLATED_TOOLS:
            argv = _wrap_with_bwrap(argv, sandbox_cwd)

        def _run_blocking() -> dict:
            try:
                completed = subprocess.run(
                    argv,
                    input=stdin_bytes,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout,
                    check=False,
                    env=sandbox_env,
                    cwd=sandbox_cwd,
                )
            except subprocess.TimeoutExpired:
                return {"_timeout": True}
            except FileNotFoundError as exc:
                return {"_spawn_error": f"python executable not found: {exc}"}
            except Exception as exc:  # noqa: BLE001 — log + report
                logger.exception(f'[AGENT] ToolDispatcher: sandbox spawn failed for {tool_name}')
                return {"_spawn_error": str(exc)}
            return {
                "_returncode": completed.returncode,
                "_stdout": completed.stdout or b"",
                "_stderr": completed.stderr or b"",
            }

        raw = await asyncio.to_thread(_run_blocking)

        if raw.get("_timeout"):
            return {"error": f"Tool '{tool_name}' timed out after {timeout}s"}
        if "_spawn_error" in raw:
            return {"error": f"Could not start sandbox: {raw['_spawn_error']}"}

        returncode = raw["_returncode"]
        stdout = raw["_stdout"]
        stderr = raw["_stderr"]

        if returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace")[:1500]
            return {
                "error": f"Tool '{tool_name}' crashed (exit {returncode})",
                "stderr": err_text,
            }

        if len(stdout) > self.MAX_OUTPUT_BYTES:
            return {"error": f"Tool '{tool_name}' output exceeded {self.MAX_OUTPUT_BYTES} bytes"}

        out_text = stdout.decode("utf-8", errors="replace").strip()
        if not out_text:
            return {"error": f"Tool '{tool_name}' produced no output"}

        # Take the LAST non-empty JSON-looking line (tools sometimes print
        # debug info first; the wrapper always prints one final json line).
        last_line = next(
            (line for line in reversed(out_text.splitlines()) if line.strip()),
            "",
        )
        try:
            parsed = json.loads(last_line)
            if isinstance(parsed, dict):
                # Relocate any artifacts this tool produced into the caller's
                # per-user owner-dir (IDOR fix). Safe no-op for tools that
                # produce no files or when there is no user identity.
                return _rehome_generated_files(parsed, user_id)
            return {"result": parsed}
        except json.JSONDecodeError:
            return {
                "error": f"Tool '{tool_name}' returned non-JSON output",
                "raw": out_text[:1500],
            }


# ---------------------------------------------------------------------------
# 9b. AgentRunner
# ---------------------------------------------------------------------------


def _worker_spec_to_agent_dict(spec) -> dict:
    """Project a swarm ``WorkerSpec`` into the dict shape ``AgentRunner`` expects.

    Synthesised at runtime by the swarm orchestrator. The
    ``role_synth_prompt`` is wrapped in ``WORKER_SKELETON_PROMPT`` so
    every dynamically-born worker inherits the six-block contract
    (``[ROLE] [RULES] [INPUT] [OUTPUT] [TOOLS] [FAILURE]``).

    Worker invariants the rest of the runner relies on:

    * ``id`` is the synthetic ``swarm::<run_id>::<role_id>`` so
      ``MonitoringLogger`` keys it separately from user-created agents
      and ``_load_agent`` can reverse it via the swarm registry.
    * ``hitl_mode="off"`` — workers are short-lived and never pause for
      human input. The HITL gates inside ``AgentRunner``/``NativeEngine``
      are all guarded by ``hitl_mode != "off"``.
    * ``tools`` / ``skills`` are exactly the orchestrator-scoped subset
      (granular permissions — workers don't inherit the parent's
      toolset). Tools pass through as name-only entries; the standard
      ``ToolDispatcher`` / ``build_tool_definitions`` path then hydrates
      each tool from ``tools_catalog`` synchronously at LLM-call time
      using the exact same code path that user-created agents use. This
      matches the working baseline and avoids per-worker DB fan-out at
      bootstrap.
    * ``owner_user_id=""`` so KB ACL defers to the invoker's identity
      forwarded into ``AgentRunner.run``.
    """
    from app.swarm.prompts import WORKER_SKELETON_PROMPT
    instructions = WORKER_SKELETON_PROMPT.format(
        role_synth_prompt=spec.role_synth_prompt,
    )
    # Defence in depth — workers must never reach platform utilities
    # (code_executor escape hatch, nested spawn_swarm). The orchestrator
    # prompt forbids these but the LLM can still slip; strip here so
    # they physically can't be dispatched.
    _safe_tools = [t for t in spec.tools if t not in _PLATFORM_UTILITY_TOOLS]
    return {
        "id":             spec.synthetic_agent_id,
        "name":           spec.role_id,
        "description":    f"Swarm worker {spec.role_id} (run {spec.run_id})",
        "instructions":   instructions,
        "system_prompt":  instructions,
        "provider":       "custom",
        # Worker inherits parent-agent model (set on the WorkerSpec by
        # the SwarmRuntime — see app/swarm/runtime.py). Falling back to
        # "" preserves the legacy behaviour of resolving via FACTORY_MODEL
        # downstream, which itself routes through the LLM_PROXY helpers.
        "model":          getattr(spec, "worker_model", "") or "",
        "model_name":     getattr(spec, "worker_model", "") or "",
        "temperature":    spec.temperature,
        "max_tokens":     spec.max_tokens,
        "tools":          [{"name": t} for t in _safe_tools],
        "skills":         [{"name": s} for s in spec.skills],
        "knowledge":      dict(spec.knowledge),
        "guardrails":     {
            "max_tool_rounds":   spec.max_tool_rounds,
            "off_topic_refusal": False,
            "hitl_mode":         "off",
        },
        "memory_config":  {},
        "owner_user_id":  "",
    }


class _SwarmDisabled(Exception):
    """Internal control-flow signal: the agent opted OUT of subagents.

    Raised inside AgentRunner's swarm-injection try-block when
    ``agent.use_subagents`` is falsy, so the shared cleanup path runs without
    surfacing spawn_swarm. Kept distinct from real errors so the generic
    ``except Exception`` swarm-init warning is not logged for the normal
    opt-out case.
    """


class AgentRunner:
    """Loads a saved agent config and executes it against the LLM with tools."""

    def __init__(
        self,
        registry: AgentRegistry,
        monitor: MonitoringLogger,
        dispatcher: Optional[ToolDispatcher] = None,
    ) -> None:
        self.registry = registry
        self.monitor = monitor
        self._dispatcher = dispatcher or ToolDispatcher(
            str(TOOLS_REGISTRY_PATH), str(GENERATED_TOOLS_DIR)
        )

    # ------------------------------------------------------------------
    async def _run_attached_flows(
        self,
        attached: list,
        seed_input: str,
        *,
        user_id: str = "",
        email: str = "",
        department: str = "",
        is_admin: bool = False,
    ) -> tuple[str, list]:
        """Pipe ``seed_input`` through each attached agent / workflow in order.

        Each entry has the shape:
            {"kind": "agent" | "workflow", "refId": str, "refName": str}

        Returns ``(final_text, generated_files)`` so the caller can splice the
        result back into its own return value.

        Failures inside a single attached step are logged but never aborted —
        the chain continues with the prior step's output so the user always
        gets a reply.
        """
        current_text = seed_input or ""
        collected_files: list = []

        for entry in attached:
            kind  = (entry.get("kind") or "").lower()
            ref_id = entry.get("refId") or ""
            ref_name = entry.get("refName") or ref_id or "(linked)"
            if not ref_id or kind not in ("agent", "workflow"):
                continue

            try:
                if kind == "agent":
                    # Re-enter AgentRunner.run for the sub-agent. History is
                    # intentionally empty — each link in the chain runs as a
                    # one-shot transformation over the upstream output.
                    sub_result = await self.run(
                        ref_id, current_text, history=[],
                        user_id=user_id, email=email,
                        department=department, is_admin=is_admin,
                    )
                    current_text = (sub_result or {}).get("response", "") or current_text
                    for f in (sub_result or {}).get("generated_files") or []:
                        collected_files.append(f)
                    continue

                # kind == "workflow"
                # Drive the workflow through the same NativeEngine the chat
                # surface uses. We collect the engine's complete event so
                # the workflow's final answer becomes the new current_text.
                from app.engine import (
                    ChainDefinition, ChainEdge, ExecutionContext, get_engine,
                )
                from app import workflow_repo as _wf_repo
                wf = await _wf_repo.get_workflow(ref_id, user_id)
                if not wf:
                    logger.warning(f"[AGENT] _run_attached_flows: workflow '{ref_id}' not found for user {user_id}")
                    continue
                graph = wf.get("graphData") or {}
                inner_nodes = graph.get("nodes") or []
                inner_edges = [
                    ChainEdge(
                        source=e.get("source", ""),
                        target=e.get("target", ""),
                        source_handle=e.get("sourceHandle"),
                    )
                    for e in (graph.get("edges") or [])
                    if e.get("source") and e.get("target")
                ]
                chain = ChainDefinition(nodes=inner_nodes, edges=inner_edges)
                ctx = ExecutionContext(
                    thread_id=f"agent-attached:{ref_id}",
                    workflow_id=ref_id,
                    workflow_name=ref_name,
                    user_id=user_id,
                    email=email,
                    department=department,
                    is_admin=is_admin,
                )

                inner_output = ""
                async for raw_event in get_engine().execute(chain, current_text, ctx):
                    if not raw_event.startswith("data:"):
                        continue
                    try:
                        payload = json.loads(raw_event[5:].strip())
                    except Exception:
                        continue
                    etype = payload.get("event") or ""
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    if etype == "complete":
                        inner_output = data.get("output", inner_output) or inner_output
                        for f in data.get("generated_files") or []:
                            collected_files.append(f)
                if inner_output:
                    current_text = inner_output

            except Exception as exc:
                logger.exception(f"[AGENT] _run_attached_flows: {kind} '{ref_name}' failed ({exc}) — continuing with prior output")

        return current_text, collected_files

    async def _load_agent(self, agent_id: str, owner_user_id: str = "") -> Optional[dict]:
        """
        Load an agent config for runtime execution.

        Resolution order:
          1. Swarm worker registry (in-memory, per-run synthetic ids
             ``swarm::<run_id>::<role_id>`` resolved via
             ``app.swarm.registry``). Always first so the swarm namespace
             can never be shadowed by a colliding DB row.
          2. Postgres ``agents`` table — canonical source for
             user-created agents.
          3. Legacy AgentRegistry JSON store — fallback for deployments
             pre-dating the postgres-only architecture.
        """
        try:
            if agent_id and agent_id.startswith("swarm::"):
                from app.swarm.registry import resolve as _resolve_swarm
                parts = agent_id.split("::", 2)
                if len(parts) == 3:
                    _, run_id, role_id = parts
                    spec = _resolve_swarm(run_id, role_id)
                    if spec is not None:
                        agent_dict = _worker_spec_to_agent_dict(spec)
                        # Hydrate worker tools with description + input_schema
                        # from tools_catalog. Without this, build_tool_definitions
                        # emits tool defs with empty description and empty
                        # parameters {"type":"object","properties":{}}, so the
                        # worker LLM has no idea what arguments tools like
                        # gitlab_list_commits require — calls go out with
                        # missing inputs and fail. Parent-attached tools carry
                        # the full schema from postgres ``agents.tools``; this
                        # brings worker tools to parity.
                        try:
                            from app import workflow_repo
                            hydrated = []
                            for entry in agent_dict.get("tools") or []:
                                name = entry.get("name") if isinstance(entry, dict) else None
                                if not name:
                                    continue
                                try:
                                    row = await workflow_repo.get_tool(name)
                                except Exception as _exc:  # noqa: BLE001
                                    logger.warning(f'[AGENT] AgentRunner._load_agent: tool hydration failed for {name}: {_exc}')
                                    row = None
                                if row:
                                    hydrated.append({
                                        "name": name,
                                        "description": row.get("description") or "",
                                        "input_schema": row.get("input_schema") or {},
                                    })
                                else:
                                    # Fall back to bare name so the worker can
                                    # still attempt a call (matches old behaviour).
                                    hydrated.append(entry)
                            agent_dict["tools"] = hydrated
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(f'[AGENT] AgentRunner._load_agent: worker tool hydration skipped: {exc}')
                        return agent_dict
        except Exception as exc:  # noqa: BLE001
            logger.warning(f'[AGENT] AgentRunner._load_agent: swarm registry lookup failed: {exc}')

        try:
            from app import workflow_repo
            row = await workflow_repo.get_agent(agent_id, owner_user_id) if owner_user_id else None
            if row:
                return row
            if not owner_user_id:
                row = await workflow_repo.get_agent_by_id(agent_id)
                if row:
                    return row
        except Exception as exc:
            logger.warning(f'[AGENT] AgentRunner._load_agent: postgres lookup failed: {exc}')

        if not owner_user_id:
            try:
                return self.registry.load(agent_id)
            except Exception:
                return None
        return None

    # ------------------------------------------------------------------
    async def _build_system_prompt(
        self,
        agent: dict,
        user_query: str = "",
        *,
        invoker_email: str = "",
        invoker_dept: str = "",
        invoker_is_admin: bool = False,
    ) -> str:
        """
        Compose the runtime system prompt:
          1. Guardrail directives (off-topic refusal, content restrictions)
          2. The agent's base system prompt
          3. Persona footer
          4. Fetched skill markdown sections from ``skills_catalog``
          5. Retrieved Knowledge Base context (when agent.knowledge.mode is
             "existing_kb" or "add_kb" and ``user_query`` is provided)

        Skill content is fetched lazily by name; missing skills are silently
        skipped (with a warning log) so a deleted skill doesn't break the
        whole agent.

        Content restrictions and off-topic refusal are prompt-injected only
        — best-effort, not security boundaries.
        """
        # AgentRegistry shape uses "system_prompt"; postgres "agents" table uses
        # "instructions". Accept either so this method works for both sources.
        base = agent.get("system_prompt") or agent.get("instructions") or "You are a helpful assistant."
        persona = agent.get("persona", "")
        guardrails = agent.get("guardrails") or {}

        prefix_lines: list[str] = []
        if guardrails.get("off_topic_refusal"):
            prefix_lines.append(
                "If the user asks about something outside the scope of your stated "
                "purpose, politely decline and redirect them to your designed task."
            )
        restrictions = guardrails.get("content_restrictions") or []
        if restrictions:
            prefix_lines.append(
                "Content restrictions you must follow: "
                + "; ".join(str(r) for r in restrictions if r)
                + "."
            )

        sections: list[str] = []
        if prefix_lines:
            sections.append("\n".join(prefix_lines))
        sections.append(base)
        if persona:
            sections.append(f"Persona: {persona}")

        # Fetch skill records from the catalog — SKILL.md body + manifest of
        # bundled files (no content; the LLM pulls files on demand via the
        # read_skill_file tool). Rendering is centralised in skill_manifest
        # so the workflow engine and this path stay in lock-step.
        skills = agent.get("skills") or []
        skill_names = [s.get("name") for s in skills if isinstance(s, dict) and s.get("name")]
        resolved_skills: list[dict] = []
        if skill_names:
            from app import workflow_repo
            for name in skill_names:
                try:
                    row = await workflow_repo.get_skill(name)
                except Exception as exc:
                    logger.warning(f"[AGENT] AgentRunner: failed to fetch skill '{name}': {exc}")
                    continue
                if not row:
                    logger.warning(f"[AGENT] AgentRunner: skill '{name}' missing from skills_catalog")
                    continue
                body = (row.get("content") or "").strip()
                try:
                    files = await workflow_repo.list_skill_files(name)
                except Exception as exc:
                    logger.warning(f"[AGENT] AgentRunner: list_skill_files failed for '{name}': {exc}")
                    files = []
                if body or files:
                    resolved_skills.append({
                        "name":  name,
                        "body":  body,
                        "files": files,
                    })

            if resolved_skills:
                from app.core.skill_manifest import render_skill_section
                section = render_skill_section(resolved_skills)
                if section:
                    sections.append(section)

                # Auto-attach read_skill_file so the manifest's "load on
                # demand via read_skill_file(...)" hint maps to an actually
                # callable tool. Mutates agent["tools"] in place — the caller
                # builds tool_defs from this list right after we return.
                tools_list = agent.get("tools")
                if not isinstance(tools_list, list):
                    tools_list = []
                    agent["tools"] = tools_list
                existing_names = {
                    t.get("name") for t in tools_list if isinstance(t, dict)
                }
                if "read_skill_file" not in existing_names:
                    tools_list.append({
                        "name": "read_skill_file",
                        "description": (
                            "Fetch a specific bundled file from an attached "
                            "skill. Call on demand using paths from the "
                            "## Skills manifest."
                        ),
                        "input_schema": {
                            "type": "object",
                            "properties": {
                                "skill":    {"type": "string"},
                                "rel_path": {"type": "string"},
                            },
                            "required": ["skill", "rel_path"],
                        },
                    })

                # Defensive backstop for document-generation skills
                # (docx/pptx/xlsx/pdf). The assembler now auto-injects
                # code_executor for EVERY agent (see AgentAssembler), so an
                # assembled agent normally already carries it. This guard
                # covers agents that reach the runner via a path that did
                # not run the assembler (e.g. legacy / externally-supplied
                # agent definitions): if such an agent carries a document
                # skill but is missing code_executor, add it so it can
                # actually emit the deliverable. Idempotent — skipped when
                # already present or explicitly attached.
                from app.core.skill_manifest import has_domain_skill
                if (
                    has_domain_skill(s["name"] for s in resolved_skills)
                    and "code_executor" not in existing_names
                ):
                    tools_list.append({
                        "name": "code_executor",
                        "description": (
                            "Execute Python in a sandbox and return any files it "
                            "generates. Use this to render the deliverable for an "
                            "attached document skill (DOCX/PPTX/XLSX/PDF) with the "
                            "skill's Python workflow, writing output to OUTPUT_DIR."
                        ),
                        "input_schema": {
                            "type": "object",
                            "properties": {"code": {"type": "string"}},
                            "required": ["code"],
                        },
                    })

        # Knowledge Base retrieval uses the **invoker's** identity (same
        # rule as the chat "Knowledge" toggle and the workflow engine): the
        # caller sees PUBLIC docs + their own-dept PRIVATE docs; admin
        # bypasses the dept filter. Empty invoker_dept => PUBLIC-only.
        #
        # build_context_section_with_meta() is used instead of
        # build_context_section() so that coverage_trace is captured for
        # full_file KBs (single-doc namespaces). The trace is stored on
        # self._kb_coverage_trace and forwarded to the SSE agent_chat_complete
        # event so the frontend can render the coverage badge — the same badge
        # that kb chat's KbChat.jsx renders via gateway.py's coverage_trace.
        from app.core import kb_retriever
        _kb_meta = await kb_retriever.build_context_section_with_meta(
            query=user_query,
            knowledge=agent.get("knowledge"),
            owner_dept=invoker_dept or None,
            owner_email=invoker_email,
            is_admin=invoker_is_admin,
        )
        kb_section = _kb_meta.get("section", "")
        # Store coverage_trace on the instance so run() can include it in
        # the result dict without changing _build_system_prompt()'s return type.
        self._kb_coverage_trace = _kb_meta.get("coverage_trace") or None
        if kb_section:
            sections.append(kb_section)

        # File-Generation directive: aggressive override by default, but a
        # softer nudge when a domain skill (pptx/docx/xlsx/pdf) is attached
        # so the skill's own workflow can lead instead of being overridden.
        tool_names = {
            t.get("name") for t in (agent.get("tools") or []) if isinstance(t, dict)
        }
        from app.core.skill_manifest import file_generation_directive
        directive = file_generation_directive(
            code_executor_available="code_executor" in tool_names,
            attached_skill_names=[s["name"] for s in resolved_skills],
        )
        if directive:
            # directive starts with a leading "\n\n## File Generation\n\n";
            # the join below already separates sections by "\n\n", so strip
            # the leading whitespace to avoid a triple blank line.
            sections.append(directive.lstrip())

        # ── Sample document (look-and-feel reference) ───────────────
        # When the user attached a sample via
        # ``POST /agent-runner/agents/{id}/sample`` we surface a prompt
        # block instructing the LLM to treat it as loose branding
        # guidance. The corresponding SAMPLE_DOC_* env vars are injected
        # by ``ToolDispatcher._run_in_sandbox`` at dispatch time. Both
        # are no-ops when ``sample_doc`` is missing or empty.
        from app.core.skill_manifest import sample_doc_directive
        _sample_block = sample_doc_directive(agent.get("sample_doc"))
        if _sample_block:
            sections.append(_sample_block.lstrip())

        # ── Tool Priority directive ──────────────────────────────────
        # Every agent gets code_executor and (when delegation is enabled)
        # spawn_swarm auto-injected, plus whatever purpose-built tools
        # the blueprint resolved. Without explicit guidance the parent
        # LLM routinely picks code_executor because it's the most
        # "universal" option — but ad-hoc Python hits auth / SSL / proxy
        # issues that the purpose-built tools already solve. This block
        # establishes a domain-agnostic priority order so the parent
        # always tries a purpose-built tool (or spawn_swarm) before
        # falling back to code_executor. The directive is emitted only
        # when at least one of spawn_swarm or code_executor is present
        # — otherwise there is no ambiguity to resolve.
        has_spawn_swarm  = "spawn_swarm"   in tool_names
        has_code_executor = "code_executor" in tool_names
        if has_spawn_swarm or has_code_executor:
            lines: list[str] = ["## Tool Priority"]
            lines.append(
                "Before each tool call, scan your full toolset and pick the "
                "tool whose name and description most directly match the "
                "user's intent. Read tool descriptions — they exist so you "
                "can verify the match before calling."
            )
            lines.append("**Selection order (highest priority first):**")
            order_n = 1
            lines.append(
                f"  {order_n}. **Any purpose-built tool** whose description "
                "matches the user's intent. Purpose-built tools handle "
                "authentication, transport, retries, and error shaping "
                "correctly — always prefer one of them when its description "
                "covers the requested action."
            )
            order_n += 1
            if has_spawn_swarm:
                lines.append(
                    f"  {order_n}. **`spawn_swarm`** — use when the request "
                    "needs multi-step reasoning, fan-out across many items, "
                    "or a combination of tool calls that no single "
                    "purpose-built tool covers. Pass the user's full goal "
                    "verbatim; the planner will pick the right tools and "
                    "delegate to short-lived sub-workers."
                )
                order_n += 1
                # ── Hybrid execution (mixed coverage) ──────────────────
                # The orchestrator is aware of your attached toolset and
                # will only spawn workers for sub-tasks YOUR tools can't
                # cover. So when a user request has multiple parts and
                # some of them are covered by your attached tools while
                # others aren't, the right move is BOTH: call the
                # purpose-built tool(s) directly for the parts you can
                # cover AND call spawn_swarm for the parts you can't.
                # Do not skip either path — calling only one leaves
                # half the user's request unanswered.
                lines.append(
                    "**Hybrid execution.** When the user's request has "
                    "MULTIPLE parts and your attached toolset covers SOME "
                    "but not ALL of them, you MUST do BOTH in the same "
                    "turn (or across consecutive turns):"
                )
                lines.append(
                    "  - Call your purpose-built tool(s) DIRECTLY for the "
                    "parts your toolset covers."
                )
                lines.append(
                    "  - Call `spawn_swarm` IN PARALLEL for the parts your "
                    "toolset does NOT cover. The orchestrator knows your "
                    "attached tools and will only plan workers for what "
                    "you can't handle yourself."
                )
                lines.append(
                    "  - Then merge both result streams into the final "
                    "reply. Never silently drop the part you can't cover "
                    "yourself just because spawn_swarm exists — and never "
                    "spawn_swarm the entire goal when half of it is a "
                    "one-shot tool call you can make directly."
                )
            if has_code_executor:
                lines.append(
                    f"  {order_n}. **`code_executor`** — ABSOLUTE LAST "
                    "RESORT. Only when NONE of the following can cover "
                    "the request: (a) a purpose-built tool whose "
                    "description matches the intent, (b) an attached "
                    "skill whose SKILL.md covers the workflow, (c) "
                    "`spawn_swarm` to delegate uncovered parts to a "
                    "specialist worker. Never use `code_executor` to "
                    "perform an action that any of (a)/(b)/(c) already "
                    "covers, even if writing a script feels faster. If "
                    "the request is fully covered by your other "
                    "capabilities, do NOT call `code_executor` at all — "
                    "the runtime will reject early `code_executor` "
                    "calls with `tool_order_violation` until you've "
                    "tried a real capability first."
                )

            # ── Failure-reporting rules ─────────────────────────────
            # When the LLM's chosen tool returns an error, it routinely
            # produces a "here's a Python snippet you can run yourself"
            # essay with placeholder credentials and fake network-issue
            # apologies. That output is hallucination disguised as
            # helpfulness — the user has a running platform and expects
            # the agent to DO the work, not to teach them how. These
            # rules forbid that pattern across every agent, regardless
            # of task or toolset.
            lines.append("")
            lines.append("**When tools fail or your toolset cannot cover the request:**")
            lines.append(
                "  - Report the actual failure plainly. Quote the tool's "
                "error message as the user sees it. Do not invent a "
                "network/transient excuse."
            )
            lines.append(
                "  - NEVER instruct the user to run code, curl, or API "
                "calls themselves. Do not emit Python snippets, shell "
                "commands, or step-by-step \"how to do this manually\" "
                "guides as a replacement for tool execution."
            )
            lines.append(
                "  - NEVER reference placeholder values "
                "(`your_personal_access_token`, `gitlab.com`, "
                "`example.com`, `<your_token>`, etc.). If you don't have "
                "a real value, say you don't — never fabricate one."
            )
            lines.append(
                "  - If a needed tool is missing from your toolset, say "
                "so explicitly: \"I don't have a tool for X — please "
                "attach <tool family> to this agent or use a workflow "
                "that includes it.\" Do not pretend you ran the work."
            )
            lines.append(
                "  - If a tool returned an authentication / permission "
                "error, report it as such (\"the GitLab API returned "
                "401\") and stop. Do not try the same call again with "
                "made-up credentials or via `code_executor`."
            )

            sections.append("\n".join(lines))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    async def _build_tool_definitions(self, tools: list[dict]) -> list[dict]:
        defs: list[dict] = []
        if not tools:
            return defs

        from app import workflow_repo

        for tool in tools:
            if not isinstance(tool, dict) or not tool.get("name"):
                continue
            name = tool["name"]
            schema = tool.get("input_schema") or tool.get("parameters")
            description = tool.get("description") or ""
            params = ToolDispatcher._input_schema_to_json_schema(schema)
            if not params.get("properties"):
                try:
                    catalog_tool = await workflow_repo.get_tool(name)
                except Exception as exc:
                    logger.warning(f"[AGENT] AgentRunner: failed to hydrate schema for tool '{name}': {exc}")
                    catalog_tool = None
                if catalog_tool:
                    params = ToolDispatcher._input_schema_to_json_schema(
                        catalog_tool.get("input_schema") or catalog_tool.get("parameters")
                    )
                    description = description or catalog_tool.get("description") or ""
            defs.append({
                "name": name,
                "description": description[:500],
                "parameters": params,
            })
        return defs

    # ------------------------------------------------------------------
    def _record_model_usage(self, *, model: str, usage, latency_ms: float) -> None:
        """Record one model response in ``model_usages`` + the per-user budget.

        Called once per LLM response inside ``_call_llm_with_tools`` (every tool
        round and the final turn), so each model response is audited exactly
        once. Fire-and-forget on a daemon thread — never raises into the run.

        The ``model_usages`` audit row is published onto the ``ainxt.metrics``
        Kafka topic (``core.kafka_producer``) — the same topic + event shape
        gateway.py already produces to for ``/ask`` and ``/v1/chat/completions``
        — rather than written to Postgres directly. workers/kafka_consumer.py's
        ``_handle_metrics`` bulk-inserts these events, and falls back to a
        Redis-backed queue when Kafka is unreachable, so the row survives
        broker downtime instead of depending solely on this thread completing.
        ``governance.increment_budget_usage`` (per-user aggregate; already
        guards enforcement + tags product_id) is still called directly since
        it is not part of the model_usages audit trail.
        """
        try:
            u = usage or {}
            in_tok = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
            out_tok = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        except (TypeError, ValueError):
            in_tok, out_tok = 0, 0

        try:
            from app.core.governance import estimate_model_cost
            cost = estimate_model_cost(model, in_tok, out_tok)
        except Exception:  # noqa: BLE001
            cost = 0.0

        self._run_tokens_in += in_tok
        self._run_tokens_out += out_tok
        self._run_cost += cost
        if model:
            self._run_model = model

        user_id = self._user_id or ""
        agent_id = self._agent_id or ""
        request_id = self._request_id or ""
        endpoint = self._endpoint

        def _bg_write() -> None:
            # Always publish the audit event (visibility is independent of
            # budget enforcement); increment_budget_usage self-guards enforcement.
            try:
                from core.kafka_producer import produce, TOPIC_METRICS
                from core.time_utils import now_ist_iso as _now_ist_iso_as
                _sent_to_kafka = produce(TOPIC_METRICS, {
                    "event":          "llm_cost",
                    "request_id":     request_id or None,
                    "user_id":        user_id or None,
                    "agent_id":       agent_id or None,
                    "endpoint":       endpoint,
                    "source_channel": "AGENT-STUDIO",
                    "model":          model or "",
                    "input_tokens":   in_tok,
                    "output_tokens":  out_tok,
                    "total_tokens":   in_tok + out_tok,
                    "latency_ms":     latency_ms,
                    "cost_usd":       cost,
                    "product_id":     None,
                    "timestamp":      _now_ist_iso_as(),
                }, key=user_id or None)
                logger.info(
                    f"[AGENT] model_usages produced agent={agent_id} model={model} cost=${cost:.6f} "
                    f"via={'kafka' if _sent_to_kafka else 'redis-fallback'}"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[AGENT] model_usages kafka produce FAILED agent={agent_id}: {exc}")

            if user_id:
                try:
                    from app.core.governance import increment_budget_usage
                    increment_budget_usage(
                        user_id, tokens_in=in_tok, tokens_out=out_tok, cost_usd=cost,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[AGENT] budget increment failed agent={agent_id}: {exc}")

        try:
            import threading
            threading.Thread(target=_bg_write, daemon=True, name="abstudio-model-usage").start()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[AGENT] model-usage thread launch failed: {exc}")

    # ------------------------------------------------------------------
    async def _call_llm_with_tools(
        self,
        system_prompt: str,
        messages: list[dict],
        tool_defs: list[dict],
        model: str,
        max_tokens: int = 8192,
    ) -> dict:
        """
        Invoke the LLM with function-calling enabled. Returns::

            {
              "stop_reason": "tool_use" | "end_turn",
              "text": str,
              "tool_calls": [{"id": str, "name": str, "inputs": dict}, ...],
              "truncated": bool,   # output hit the token cap mid-generation
            }

        ``truncated`` is True when the provider reported a length/max_tokens
        finish reason. A truncated turn often means a tool call was cut off
        mid-JSON and silently dropped by the client (leaving only preamble
        text), so the caller must NOT treat such a turn as a finished reply.
        """
        # Lazy imports to avoid circular import at module load
        from app.llm_handler import get_llm_client, Message as LLMMessage, ToolCall

        # ``max_tokens`` is the agent's configured output cap (falling back to
        # 8192). Honoring it matters: code reviews, long summaries, and tool
        # calls carrying large payloads (e.g. gitlab_create_mr_review with a
        # full review body) blow past a small cap — the truncated tool-call
        # JSON then fails to parse and is dropped, collapsing the turn to bare
        # preamble text. A generous floor keeps mis-configured agents usable.
        llm_config = _build_factory_llm_config(
            max_tokens=max(int(max_tokens or 0), 8192), model=model
        )
        client = get_llm_client(llm_config)

        # Translate internal message dicts → llm_handler.Message objects.
        llm_msgs: list = [LLMMessage(role="system", content=system_prompt)]
        for m in messages:
            role = m.get("role", "user")
            if role == "tool":
                llm_msgs.append(LLMMessage(
                    role="tool",
                    content=str(m.get("content", "")),
                    tool_call_id=m.get("tool_use_id") or m.get("tool_call_id") or "",
                    tool_name=m.get("tool_name", ""),
                ))
            elif role == "assistant":
                tcs: list = []
                for tc in m.get("tool_calls") or []:
                    tcs.append(ToolCall(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        args=tc.get("inputs") or tc.get("args") or {},
                    ))
                llm_msgs.append(LLMMessage(
                    role="assistant",
                    content=str(m.get("content", "") or ""),
                    tool_calls=tcs,
                ))
            else:
                llm_msgs.append(LLMMessage(role=role, content=str(m.get("content", ""))))

        _t0 = time.monotonic()
        text = ""
        final_tool_calls: list = []
        finish_reason = ""
        usage = None
        async for chunk in client.stream(llm_msgs, tools=tool_defs or None):
            if chunk.text:
                text += chunk.text
            if chunk.is_final:
                if chunk.tool_calls:
                    final_tool_calls = chunk.tool_calls
                # Last non-empty finish_reason wins (some gateways emit it
                # on an earlier delta than the terminal chunk).
                if getattr(chunk, "finish_reason", ""):
                    finish_reason = chunk.finish_reason
                if getattr(chunk, "usage", None):
                    usage = chunk.usage

        # Record this model response (tool round OR final turn) exactly once.
        self._record_model_usage(
            model=model, usage=usage,
            latency_ms=(time.monotonic() - _t0) * 1000,
        )

        # "length" (OpenAI) / "max_tokens" (Anthropic) both mean the output
        # was cut off at the cap — the model had more to say (and possibly a
        # tool call it never finished emitting).
        truncated = finish_reason.lower() in ("length", "max_tokens")

        if final_tool_calls:
            return {
                "stop_reason": "tool_use",
                "text": text,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "inputs": dict(tc.args or {})}
                    for tc in final_tool_calls
                ],
                "truncated": truncated,
            }
        return {
            "stop_reason": "end_turn",
            "text": text,
            "tool_calls": [],
            "truncated": truncated,
        }

    # ------------------------------------------------------------------
    async def _run_turn_via_cli(
        self,
        *,
        agent: dict,
        agent_id: str,
        user_message: str,
        system_prompt: str,
        model: str,
        user_id: str,
        email: str,
        sse_sink: Optional[Callable[[str], None]],
        delegation_events: List[dict],
        start: float,
    ) -> Optional[dict]:
        """Execute one chat turn in a spawned ``ainxt`` CLI.

        Returns the same response dict ``run()`` returns, so the caller can hand
        it straight back and the ``/agent-runner/chat*`` contract is unchanged.
        Returns ``None`` only to mean "fall back to the native loop", which
        happens exclusively when the operator has explicitly enabled the
        emergency fallback — otherwise a CLI failure is reported as a failure.

        Tool execution happens over MCP against this same process, so per-user
        credentials, the sandbox and the audit trail are the ones already in use;
        nothing about tool semantics changes here.
        """
        from app.cli_runtime.bridge import (
            AgentTurnSpec,
            build_prompt,
            run_agent_turn_via_cli,
        )
        from app.cli_runtime.config import cli_runtime_config

        cfg = cli_runtime_config()

        tool_names = [
            (t.get("name") if isinstance(t, dict) else str(t))
            for t in (agent.get("tools") or [])
        ]
        tool_names = [t for t in tool_names if t]
        skill_names = [
            s.get("name") for s in (agent.get("skills") or [])
            if isinstance(s, dict) and s.get("name")
        ]

        # ``spawn_swarm`` is injected by the native path as a synthetic tool and
        # needs an in-process runtime, so it is never handed to the CLI.
        tool_names = [t for t in tool_names if t != "spawn_swarm"]

        agent_name = agent.get("name") or agent_id
        run_id = f"agentchat-{self._request_id}"

        def _emit(event: str, payload: dict) -> None:
            """Forward one frame to the caller's SSE sink, if any.

            The sink expects the platform's ``data: {json}\\n\\n`` wire format —
            the same shape the swarm runtime emits — so the frontend's existing
            reader needs no changes.
            """
            frame = {"event": event, "data": payload}
            delegation_events.append(frame)
            if sse_sink is None:
                return
            try:
                sse_sink(f"data: {json.dumps(frame)}\n\n")
            except Exception:  # noqa: BLE001
                logger.debug("[AGENT] cli sse_sink raised; ignoring", exc_info=True)

        # Sample document (look-and-feel reference) — read from the same
        # ``agent`` dict AgentRunner.run() loaded from the DB, so both the
        # in-process dispatch path and this CLI path treat the sample the
        # same way. Missing / empty ``sample_doc`` → empty strings →
        # SAMPLE_DOC_* env vars are not injected downstream. Also
        # tolerate a missing file on disk (stale metadata) so we don't
        # tell the CLI a path exists that ``read_document`` will 400 on.
        _sd = agent.get("sample_doc") or {}
        _sd_path = str(_sd.get("path") or "").strip()
        _sd_kind = str(_sd.get("kind") or "").strip().lower()
        if _sd_path and not os.path.isfile(_sd_path):
            logger.warning(
                f"[AGENT] sample_doc path missing on disk for agent={agent_id}: "
                f"{_sd_path!r} — ignoring for this CLI turn"
            )
            _sd_path = ""
            _sd_kind = ""

        spec = AgentTurnSpec(
            prompt=build_prompt(system_prompt, user_message),
            model=model,
            agent_name=agent_name,
            run_id=run_id,
            user_id=user_id,
            email=email,
            tool_names=tool_names,
            skill_names=skill_names,
            # Threaded through to the RunSession so the CLI-side MCP
            # dispatcher can inject SAMPLE_DOC_* into the code_executor
            # sandbox at tool-call time (mirrors what the in-process
            # dispatch path already does). Empty when no sample is
            # attached — no behaviour change for those agents.
            sample_doc_path=_sd_path,
            sample_doc_kind=_sd_kind,
        )

        logger.info(
            "[AGENT] routing chat turn through the ainxt CLI",
            agent_id=agent_id, run_id=run_id, model=model,
            tools=len(tool_names), skills=len(skill_names),
        )

        result = None
        async for event_name, payload in run_agent_turn_via_cli(spec, config=cfg):
            if event_name == "__result__":
                result = payload["result"]
                continue
            _emit(event_name, payload)

        if result is None or not result.ok:
            reason = (result.error if result else "the CLI produced no result")
            if cfg.emergency_native_fallback:
                # Break-glass only. Logged at WARNING because a silent fallback is
                # what made the previous attempt at this feature undebuggable: the
                # feature appeared to work while never actually using the CLI.
                logger.warning(
                    "[AGENT] CLI turn failed — EMERGENCY FALLBACK to the in-process "
                    "engine (ABSTUDIO_CLI_EMERGENCY_FALLBACK=true)",
                    agent_id=agent_id, run_id=run_id, reason=reason,
                )
                return None
            logger.error(
                "[AGENT] CLI turn failed", agent_id=agent_id, run_id=run_id, reason=reason,
            )
            raise RuntimeError(reason)

        # Fold CLI usage into the per-run accounting the monitor and audit log
        # read, so cost tracking behaves identically to the native path.
        usage = result.usage or {}
        self._run_model = model
        self._run_tokens_in = int(usage.get("prompt_tokens") or 0)
        self._run_tokens_out = int(usage.get("completion_tokens") or 0)

        final_text = result.output
        self.monitor.log(agent_id, user_message, final_text, time.monotonic() - start)
        logger.info(
            "[AGENT] CLI chat turn complete",
            agent_id=agent_id, run_id=run_id, model=model,
            in_tok=self._run_tokens_in, out_tok=self._run_tokens_out,
            tool_calls=result.tool_calls, files=len(result.generated_files),
        )

        return {
            "response": final_text,
            "generated_files": result.generated_files,
            "delegation_events": delegation_events,
            "usage": {
                "model": model,
                "tokens_in": self._run_tokens_in,
                "tokens_out": self._run_tokens_out,
                "cost_usd": round(self._run_cost, 6),
                "latency_ms": int((time.monotonic() - start) * 1000),
            },
            "coverage_trace": self._kb_coverage_trace,
        }

    # ------------------------------------------------------------------
    async def run(
        self,
        agent_id: str,
        user_message: str,
        history: Optional[list[dict]] = None,
        user_id: str = "",
        email: str = "",
        *,
        department: str = "",
        is_admin: bool = False,
        sse_sink: Optional[Callable[[str], None]] = None,
        endpoint: str = "abstudio.agent.chat",
        agent_config: Optional[dict] = None,
    ) -> str:
        """
        Build the runtime system prompt, run a tool-dispatch loop until the
        LLM stops requesting tools (or ``max_tool_rounds`` is reached), and
        return the final text reply.

        Args:
            agent_id: ID registered in AgentRegistry
            user_message: the current user turn
            history: previous [{role, content}] turns (without the new message)
            user_id: current user's ID for per-user credential resolution
            email: current user's email for credential resolution fallback

        Termination guards:
            * ``guardrails.max_tool_rounds`` — hard cap on tool-call iterations
              within a single ``run()`` call. Default 5.
            * ``guardrails.max_turns`` — hard cap on accumulated user turns
              (history + new). Default 50. Exceeding this raises ValueError.
        """
        start = time.monotonic()

        # Per-run context + usage accumulator, read by _record_model_usage
        # (one call per LLM response) and the end-of-run summary log.
        self._user_id = user_id
        self._agent_id = agent_id
        self._request_id = uuid.uuid4().hex
        self._endpoint = endpoint
        self._run_tokens_in = 0
        self._run_tokens_out = 0
        self._run_cost = 0.0
        self._run_model = ""
        # Coverage trace from KB full_file retrieval — populated by
        # _build_system_prompt() when full_file_doc_ids are present in the
        # agent's knowledge blob. Forwarded to the SSE agent_chat_complete
        # event so the frontend can render the coverage badge (same badge
        # as kb chat's KbChat.jsx coverage badge).
        self._kb_coverage_trace: Optional[dict] = None

        agent = agent_config if agent_config is not None else await self._load_agent(agent_id, owner_user_id=user_id)
        if not agent:
            raise ValueError(f"Agent '{agent_id}' not found")

        # Per-agent Sample Document (look-and-feel reference). See
        # ``app/api/agent_sample.py``. The path is threaded into every
        # tool dispatch below so ``ToolDispatcher._run_in_sandbox``
        # exposes it as SAMPLE_DOC_PATH inside code_executor; the prompt
        # block is appended by ``_build_system_prompt`` via
        # ``skill_manifest.sample_doc_directive``. Both are no-ops when
        # ``sample_doc`` is missing or empty — agents without a sample
        # incur zero overhead.
        sample_doc = agent.get("sample_doc") or {}
        _sample_doc_path = str(sample_doc.get("path") or "").strip()
        _sample_doc_kind = str(sample_doc.get("kind") or "").strip().lower()
        # Skip stale metadata: if the file has been deleted out from
        # under us (manual cleanup, moved disks, restore-from-backup
        # gap), don't advertise a path the sandbox can't actually open.
        if _sample_doc_path and not os.path.isfile(_sample_doc_path):
            logger.warning(
                f"[AGENT] sample_doc path missing on disk for agent={agent_id}: "
                f"{_sample_doc_path!r} — ignoring for this run"
            )
            _sample_doc_path = ""
            _sample_doc_kind = ""

        guardrails = agent.get("guardrails") or {}
        max_tool_rounds = int(guardrails.get("max_tool_rounds", DEFAULT_GUARDRAILS["max_tool_rounds"]))
        max_turns = int(guardrails.get("max_turns", DEFAULT_GUARDRAILS["max_turns"]))

        history = list(history or [])
        # Count user turns including the new one
        user_turns = sum(1 for m in history if m.get("role") == "user") + 1
        if user_turns > max_turns:
            self.monitor.log(
                agent_id, user_message, "", time.monotonic() - start,
                error=f"max_turns ({max_turns}) exceeded",
            )
            raise ValueError(
                f"Agent has reached its conversation limit of {max_turns} turns."
            )

        system_prompt = await self._build_system_prompt(
            agent,
            user_query=user_message,
            invoker_email=email,
            invoker_dept=department,
            invoker_is_admin=is_admin,
        )
        tool_defs = await self._build_tool_definitions(agent.get("tools") or [])

        # ── Adaptive swarm (``spawn_swarm`` tool) ──────────────────────
        # Surface ONE synthetic tool the parent LLM can call. The swarm
        # orchestrator plans + runs N short-lived workers picking tools
        # / skills / KBs from the live capability manifest. This is the
        # ONLY delegation surface — the previous static ``delegate_to_*``
        # layer has been removed in favour of dynamic adaptive workers.
        from app.tools.spawn_swarm_tool import (
            SpawnSwarmTool as _SpawnSwarmTool,
            SPAWN_SWARM_TOOL_NAME as _SPAWN_SWARM_TOOL_NAME,
        )
        from app.swarm.runtime import SwarmRuntime as _SwarmRuntime, SwarmContext as _SwarmContext
        from app.swarm.prompts import SWARM_POLICY_ADDENDUM as _SWARM_ADDENDUM

        _swarm_tool = None
        # Per-run buffer of raw SSE frames emitted by the SwarmRuntime
        # for THIS chat turn. We populate `delegation_events` from it on
        # return so even the non-streaming /agent-runner/chat endpoint
        # surfaces post-hoc pills. If the caller passed an sse_sink, we
        # also fan out each frame to it in real time (the /chat-stream
        # endpoint uses this to push events down the wire as they arrive).
        delegation_events: List[dict] = []

        def _swarm_sse_capture(frame: str) -> None:
            try:
                # frame is "data: {json}\n\n" — the engine.make_sse format.
                parsed = json.loads(frame[len("data: "):].strip())
                delegation_events.append(parsed)
            except Exception:  # noqa: BLE001
                pass
            if sse_sink is not None:
                try:
                    sse_sink(frame)
                except Exception:  # noqa: BLE001
                    # A flaky sink (closed client connection, etc.) must
                    # never abort the agent run.
                    logger.debug('[AGENT] AgentRunner: sse_sink raised; ignoring', exc_info=True)

        # Per-agent swarm gate. Standalone agents opt IN to subagent
        # delegation via the "Use subagents (swarm)" toggle in Agent
        # Configuration, persisted as ``use_subagents`` on the agent row.
        # Default is FALSE (enterprise-safe): when off we do NOT inject
        # spawn_swarm or the SWARM_POLICY_ADDENDUM, so the LLM never sees the
        # tool and always runs solo. This mirrors the per-node
        # ``enable_subagents`` pin on workflow agent nodes.
        _use_subagents = bool(agent.get("use_subagents", False))
        _swarm_tool = None
        if not _use_subagents:
            logger.info(
                f"[AGENT] AgentRunner: subagents disabled for agent_id={agent_id} "
                f"(use_subagents=False) — spawn_swarm not injected"
            )

        # Inject spawn_swarm + the addendum ONLY when the agent opted in.
        # The orchestrator's system prompt and SWARM_POLICY_ADDENDUM together
        # give the parent LLM and orchestrator LLM enough context to decide
        # per-turn whether to decompose. We intentionally do NOT add a further
        # Python-level pre-gate beyond the opt-in flag — once enabled, the LLM
        # is the only entity that can reliably judge whether a task benefits
        # from multiple workers.
        try:
            if not _use_subagents:
                raise _SwarmDisabled()
            # Fresh runner per worker — keeps dispatcher state local and
            # avoids cross-worker mutation of tools_catalog caches.
            def _worker_runner_factory():
                return AgentRunner(self.registry, self.monitor)

            # ── Parent-context channel for the orchestrator ──────────
            # The orchestrator decomposes the goal AROUND tools the parent
            # already has — for parts the parent covers, it skips
            # spawning a redundant worker; for parts the parent does
            # NOT cover, it spawns a worker that pulls the right tool
            # from the catalog. ``_PLATFORM_UTILITY_TOOLS`` mirrors the
            # assembler / ordering-gate definition so the "what counts
            # as purpose-built" rule stays consistent across the
            # codebase.
            _PARENT_PLATFORM_TOOLS = {
                "code_executor", "spawn_swarm", "ask_human", "read_skill_file",
            }
            _parent_purpose_built = tuple(
                t.get("name")
                for t in (agent.get("tools") or [])
                if isinstance(t, dict)
                and t.get("name")
                and t.get("name") not in _PARENT_PLATFORM_TOOLS
            )

            # ── Nested-swarm model inheritance ───────────────────────
            # When THIS agent (parent of a potential nested swarm) was
            # configured with a specific model, propagate it so any swarm
            # it spawns runs its orchestrator + aggregator + workers on
            # the same model. The agent dict's ``model`` / ``model_name``
            # were populated either by:
            #   * user choice in Agent Configuration (top-level workflow
            #     node) — flows in via ``_extract_llm_config``;
            #   * the parent swarm via ``_worker_spec_to_agent_dict`` —
            #     synthetic workers now carry ``spec.worker_model``.
            # Without this forward-prop, nested swarms silently fell
            # back to FACTORY_MODEL (e.g. Qwen30B), which is exactly the
            # mismatch users see when a Sonnet-picked workflow spawns
            # a subagent that itself decomposes: parent sonnet, nested
            # swarm Qwen. ``None`` is fine — the SwarmOrchestrator's own
            # resolution chain then falls through to env / FACTORY_MODEL
            # as before, preserving backwards compatibility for agents
            # that never set a model.
            _nested_parent_model = (
                agent.get("model")
                or agent.get("model_name")
                or None
            )
            logger.info(f"[AGENT] [SWARM] nested_model_resolution parent_agent_id={agent_id} agent.model={agent.get('model')!r} agent.model_name={agent.get('model_name')!r} forwarded={_nested_parent_model!r}")

            _swarm_runtime = _SwarmRuntime(
                runner_factory=_worker_runner_factory,
                orchestrator_model=_nested_parent_model,
                aggregator_model=_nested_parent_model,
            )
            _swarm_tool = _SpawnSwarmTool(
                _swarm_runtime,
                _SwarmContext(
                    user_id=user_id, email=email,
                    department=department, is_admin=is_admin,
                    parent_agent_id=agent_id,
                    sse_sink=_swarm_sse_capture,
                    parent_attached_tools=_parent_purpose_built,
                ),
            )
            tool_defs.append(_swarm_tool.to_function_spec())
            system_prompt = _SWARM_ADDENDUM + "\n\n---\n\n" + (system_prompt or "")
        except _SwarmDisabled:
            # Expected path when the agent opted OUT of subagents — no tool,
            # no addendum, run solo. Already logged above; nothing to do.
            _swarm_tool = None
        except Exception as _swarm_init_exc:  # noqa: BLE001
            # Don't let a swarm-init bug crash a regular chat turn —
            # degrade to "no spawn_swarm tool surfaced" and log so the
            # error is debuggable.
            logger.warning(f'[AGENT] AgentRunner: swarm tool init skipped: {_swarm_init_exc}')
            _swarm_tool = None
        model = agent.get("model") or agent.get("model_name") or FACTORY_MODEL
        # Agent's configured output-token cap (top-level ``max_tokens`` column
        # on the agents row). Was previously ignored — the run loop hardcoded
        # 2048, silently overriding whatever the user set and truncating long
        # replies / large tool-call payloads. ``_call_llm_with_tools`` applies
        # an 8192 floor so a low/absent value can't re-introduce the truncation.
        try:
            agent_max_tokens = int(agent.get("max_tokens") or 0)
        except (TypeError, ValueError):
            agent_max_tokens = 0

        messages: list[dict] = list(history) + [{"role": "user", "content": user_message}]
        final_text = ""

        # ── code_executor ordering gate ──────────────────────────────
        # Rule: code_executor is the ABSOLUTE LAST RESORT and must run
        # AFTER the agent has actually exercised at least one of its
        # real capabilities in the current turn. "Real capability" is
        # any of:
        #   * a purpose-built tool attached to the agent
        #   * an attached skill (SKILL.md) — exercising a skill in
        #     practice means calling ``read_skill_file`` to pull a
        #     bundled file from it
        #   * ``spawn_swarm`` (when attached) — the planner picks the
        #     right specialist tools from the catalog for the parts
        #     the parent itself can't cover
        # If the agent has NONE of those signals (no purpose-built
        # tools, no skills, no spawn_swarm), the gate doesn't apply —
        # code_executor is the only thing it can do, so we let it run
        # immediately. Otherwise we block code_executor with a
        # structured ``tool_order_violation`` until the LLM tries one
        # of its real capabilities first.
        #
        # ``_PLATFORM_UTILITY_TOOLS`` mirrors the assembler-side set so
        # the definition of "purpose-built" stays consistent across the
        # codebase (assembler decides what to auto-attach; this gate
        # decides when to permit calls). Anything NOT in that set —
        # gitlab_*, jira_*, custom internal tools, generated tools —
        # counts as purpose-built without us having to know its name.
        _agent_tool_names = {
            (t.get("name") if isinstance(t, dict) else str(t))
            for t in (agent.get("tools") or [])
        }
        _attached_skill_names = {
            s.get("name") for s in (agent.get("skills") or [])
            if isinstance(s, dict) and s.get("name")
        }
        _purpose_built_attached = bool(
            (_agent_tool_names - _PLATFORM_UTILITY_TOOLS) or _attached_skill_names
        )
        _spawn_swarm_attached = "spawn_swarm" in _agent_tool_names
        # Flipped to True the moment a real capability is exercised —
        # any purpose-built tool, ``read_skill_file`` against an
        # attached skill, or ``spawn_swarm``.
        _has_run_purpose_built_tool = False
        _has_run_spawn_swarm        = False
        nofiles_retry_used          = False

        # ── CLI execution branch (ABSTUDIO_CLI_MODE) ──────────────────
        # When CLI mode is on, this turn is executed by a spawned headless
        # ``ainxt`` process instead of the in-process LLM + tool loop below.
        # Everything above this point is REUSED unchanged — the system prompt,
        # skills section, tool definitions, swarm gate and guardrails are all
        # already computed, so the CLI runs against exactly the same context the
        # native path would have used.
        #
        # This is one of TWO integration points; the other is
        # ``NativeEngine._run_agent`` for workflow nodes. Both are required:
        # /agent-runner/chat-stream reaches THIS function and never touches
        # NativeEngine, which is why a single-site integration silently appears
        # to do nothing.
        try:
            from app.cli_runtime.config import cli_mode_enabled as _cli_mode_enabled
        except Exception:  # pragma: no cover - cli_runtime is optional
            _cli_mode_enabled = None  # type: ignore[assignment]

        if _cli_mode_enabled is not None and _cli_mode_enabled():
            _cli_outcome = await self._run_turn_via_cli(
                agent=agent,
                agent_id=agent_id,
                user_message=user_message,
                system_prompt=system_prompt,
                model=model,
                user_id=user_id,
                email=email,
                sse_sink=sse_sink,
                delegation_events=delegation_events,
                start=start,
            )
            if _cli_outcome is not None:
                # ── Eval Observatory: fire-and-forget LLM-as-judge (CLI path) ──
                # The native-path eval at line ~4197 is never reached when CLI
                # mode is active because we return here. Mirror it so agent_studio
                # evals appear regardless of which execution backend is used.
                # Skipped for swarm workers (agent_id starts with "swarm::")
                # to avoid noise rows from internal sub-agents.
                _cli_is_swarm_worker = (agent_id or "").startswith("swarm::")
                if not _cli_is_swarm_worker:
                    try:
                        import threading as _cli_eval_thread
                        _cli_eval_q   = user_message
                        _cli_eval_ans = _cli_outcome.get("response", "")
                        _cli_eval_sid = user_id or None
                        _cli_eval_rid = agent_id
                        _cli_eval_mdl = self._run_model or None
                        def _run_cli_agent_eval():
                            try:
                                from core.evals import eval_engine as _ee
                                _ee.eval_answer_quality(
                                    _cli_eval_q, _cli_eval_ans, [],
                                    session_id=_cli_eval_sid,
                                    run_id=_cli_eval_rid,
                                    platform="agent_studio",
                                    model=_cli_eval_mdl,
                                )
                            except Exception as _cli_eval_err:
                                logger.debug(f"[AGENT] CLI eval_answer_quality failed (non-critical): {_cli_eval_err}")
                        _cli_eval_thread.Thread(
                            target=_run_cli_agent_eval, daemon=True, name="eval-agent-studio-cli"
                        ).start()
                    except Exception:
                        pass
                return _cli_outcome

        try:
            for round_idx in range(max_tool_rounds):
                response = await self._call_llm_with_tools(
                    system_prompt, messages, tool_defs, model, agent_max_tokens
                )
                final_text = response.get("text", "") or final_text

                if response.get("stop_reason") == "tool_use" and response.get("tool_calls"):
                    # Record the assistant's tool-calling turn
                    messages.append({
                        "role": "assistant",
                        "content": response.get("text", ""),
                        "tool_calls": response["tool_calls"],
                    })
                    # Pre-compute the attached-skill allowlist once per
                    # round so the per-call scope guard is cheap.
                    allowed_skills = [
                        s.get("name") for s in (agent.get("skills") or [])
                        if isinstance(s, dict) and s.get("name")
                    ]
                    # Dispatch each requested tool and feed the result back.
                    # We trim verbose tracebacks before passing them to the
                    # LLM — otherwise the model parrots a 50-line Python stack
                    # into the user-facing chat reply. Lazy-import the engine
                    # helper so this module stays import-cheap.
                    try:
                        from app.engine.native_engine import (
                            _shorten_tool_payload_for_llm as _shorten,
                        )
                    except Exception:
                        _shorten = None  # type: ignore[assignment]


                    # Pre-compute available tool names for policy checks
                    _available_tool_names = [
                        (t.get("name") if isinstance(t, dict) else str(t))
                        for t in (agent.get("tools") or [])
                        if t
                    ]
                    for tc in response["tool_calls"]:
                        tc_name = tc.get("name", "")
                        tc_inputs = tc.get("inputs") or {}
                        # ── Ordering gate ─────────────────────────────
                        # Block code_executor when the agent has at least
                        # one real capability attached (purpose-built
                        # tool, attached skill, or spawn_swarm) AND none
                        # of those have fired yet this turn. The LLM
                        # gets a structured error back so it can
                        # course-correct and try its real capability
                        # first; the next round will see the gate open.
                        if (tc_name == "code_executor"
                                and (_purpose_built_attached or _spawn_swarm_attached)
                                and not _has_run_purpose_built_tool
                                and not _has_run_spawn_swarm):
                            attached_tools = sorted(
                                _agent_tool_names - _PLATFORM_UTILITY_TOOLS
                            )
                            attached_skills = sorted(
                                s for s in _attached_skill_names if s
                            )
                            signal_lines: list[str] = []
                            if attached_tools:
                                signal_lines.append(
                                    "tools: " + ", ".join(f"`{n}`" for n in attached_tools)
                                )
                            if attached_skills:
                                signal_lines.append(
                                    "skills: " + ", ".join(f"`{n}`" for n in attached_skills)
                                )
                            if _spawn_swarm_attached:
                                signal_lines.append("delegation: `spawn_swarm`")
                            result = {
                                "error": "tool_order_violation",
                                "detail": (
                                    "code_executor is the ABSOLUTE LAST RESORT "
                                    "and is blocked until you have tried one "
                                    "of your real capabilities in this turn. "
                                    "Pick from — "
                                    + "; ".join(signal_lines)
                                    + ". Use the right capability first; "
                                    "code_executor stays available afterwards "
                                    "only if NONE of them can cover the request."
                                ),
                            }
                            try:
                                content = json.dumps(result)
                            except Exception:
                                content = str(result)
                            messages.append({
                                "role": "tool",
                                "tool_use_id": tc.get("id", ""),
                                "tool_name": tc_name,
                                "content": content,
                            })
                            continue
                        # ── Audit tool dispatch ─────────────────────────
                        try:
                            from app.core.governance import audit_event as _gov_audit
                            _gov_audit(
                                user_id=user_id,
                                endpoint="abstudio.factory_agent.tool_execute",
                                action="executed",
                                email=email,
                                department=department,
                                extra={"tool": tc_name, "agent_id": agent_id},
                            )
                        except ImportError:
                            pass  # governance module not yet available — skip silently

                        # Swarm orchestration is handled in-process — the
                        # tool plans + runs N short-lived workers via the
                        # AgentRunner runner_factory above. Intercept
                        # BEFORE the catalog dispatcher so the synthetic
                        # tool name is never confused with a missing
                        # catalog tool.
                        if _swarm_tool is not None and tc_name == _SPAWN_SWARM_TOOL_NAME:
                            raw_result = await _swarm_tool.call(tc_inputs)
                            try:
                                result = json.loads(raw_result)
                            except Exception:
                                result = {"output": raw_result}
                            # spawn_swarm is itself a platform utility, not a
                            # purpose-built tool. Even though it routes into
                            # service tools internally, calling it does NOT
                            # unlock code_executor at the parent level —
                            # the parent still has to call a real attached
                            # tool itself (otherwise the gate is trivial to
                            # bypass via spawn_swarm).
                        elif tc_name == "read_skill_file":
                            # Scope guard for read_skill_file — block calls
                            # against skills not attached to this agent so
                            # the LLM cannot read the rest of the skill
                            # catalog by guessing names.
                            # Fail closed on an empty/missing allowlist —
                            # keep this in lock-step with native_engine.py's
                            # NativeTool.call guard (both pass ``or []`` so the
                            # helper's "empty → block" contract holds on both
                            # dispatch paths).
                            from app.core.skill_manifest import enforce_read_skill_file_scope
                            err = enforce_read_skill_file_scope(tc_inputs, allowed_skills or [])
                            if err:
                                result = {"error": err}
                            else:
                                result = await self._dispatcher.dispatch(
                                    tc_name, tc_inputs,
                                    user_id=user_id, email=email,
                                    sample_doc_path=_sample_doc_path,
                                    sample_doc_kind=_sample_doc_kind,
                                )
                        else:
                            result = await self._dispatcher.dispatch(
                                tc_name, tc_inputs,
                                user_id=user_id, email=email,
                                sample_doc_path=_sample_doc_path,
                                sample_doc_kind=_sample_doc_kind,
                            )
                        # Mark the ordering gate "open" once any real
                        # capability has been dispatched this turn
                        # (success or error — what matters for the gate
                        # is that the LLM TRIED a real capability, not
                        # that it succeeded). The dispatch counts as a
                        # real capability when:
                        #   * tc_name is a purpose-built tool (not a
                        #     platform utility and not read_skill_file),
                        #   * tc_name is ``read_skill_file`` — reading a
                        #     bundled skill file means the LLM has begun
                        #     the skill workflow, which is the
                        #     skill-equivalent of "tried the tool",
                        #   * tc_name is ``spawn_swarm`` — flips its own
                        #     dedicated flag so code_executor stays
                        #     blocked unless spawn_swarm has been tried
                        #     (separate flag because spawn_swarm doesn't
                        #     count as "the parent ran a tool itself").
                        if not tc_name:
                            pass
                        elif tc_name == "spawn_swarm":
                            _has_run_spawn_swarm = True
                        elif tc_name == "read_skill_file":
                            _has_run_purpose_built_tool = True
                        elif tc_name not in _PLATFORM_UTILITY_TOOLS:
                            _has_run_purpose_built_tool = True
                        # ── code_executor "no files generated" auto-retry ──
                        # Mirror of native_engine.py lines 4199–4219.
                        # The model's code ran cleanly but saved nothing to
                        # OUTPUT_DIR (wrong/relative path). Give it exactly
                        # ONE automatic second attempt with a concrete,
                        # imperative instruction before the result is fed
                        # back to the LLM.
                        _llm_override: Optional[str] = None
                        if (tc_name == "code_executor"
                                and not nofiles_retry_used
                                and isinstance(result, dict)
                                and not result.get("error")
                                and not result.get("generated_files")
                                and "no files were generated" in str(
                                    result.get("message", "")).lower()):
                            nofiles_retry_used = True
                            _llm_override = json.dumps({
                                "status": "no_files_written",
                                "required_next_action": (
                                    "Your code ran but wrote NO files. The output "
                                    "directory is injected as the variable OUTPUT_DIR "
                                    "(already in scope — do NOT redefine it). Re-run "
                                    "code_executor and save EVERY artifact with an "
                                    "absolute path built from OUTPUT_DIR, e.g.: "
                                    "import os; doc.save(os.path.join(OUTPUT_DIR, "
                                    "'output.docx')). Do not use bare/relative "
                                    "filenames. Retry now."
                                ),
                            })
                        try:
                            content = _llm_override or json.dumps(result, default=str)
                        except Exception:
                            content = str(result)
                        if _shorten is not None:
                            content = _shorten(content)
                        messages.append({
                            "role": "tool",
                            "tool_use_id": tc.get("id", ""),
                            "tool_name": tc.get("name", ""),
                            "content": content,
                        })
                    continue

                # end_turn with the output truncated at the token cap. The
                # model was cut off mid-generation — often mid tool-call JSON,
                # which the client then drops as unparseable, leaving only
                # preamble text like "Let me post a detailed review:". Treat
                # this as INCOMPLETE, not final: record the partial assistant
                # text, nudge the model to continue, and loop. Without this
                # the user gets the dangling preamble and nothing else.
                if response.get("truncated") and round_idx < max_tool_rounds - 1:
                    logger.warning(
                        f'[AGENT] AgentRunner({agent_id}): response truncated at '
                        f'token cap (round {round_idx + 1}/{max_tool_rounds}) — '
                        f'asking the model to continue'
                    )
                    messages.append({
                        "role": "assistant",
                        "content": response.get("text", ""),
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous message was cut off. Continue from "
                            "exactly where you stopped and finish your response. "
                            "If you intended to call a tool, issue that tool call "
                            "now."
                        ),
                    })
                    continue

                # end_turn (or fallback) — we have the final reply
                break
            else:
                # Loop exited via for-else: max_tool_rounds reached while the
                # model was STILL requesting tools. At this point `final_text`
                # holds the model's interim reasoning (e.g. "Let me post a
                # thoughtful review…") — NOT a finished answer. Returning that
                # verbatim is what surfaced as an "empty"/useless reply in the
                # agent chat UI. Instead, make ONE final LLM call with tools
                # disabled so the model is forced to end_turn and actually
                # write its answer from the tool results already in `messages`.
                logger.warning(
                    f'[AGENT] AgentRunner({agent_id}): max_tool_rounds '
                    f'({max_tool_rounds}) reached while still calling tools — '
                    f'forcing a final no-tools completion'
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "You have reached the tool-use limit for this turn. "
                        "Do NOT request any more tools. Using the information "
                        "you have already gathered, write your complete final "
                        "answer for the user now. If some data is missing, "
                        "state clearly what you could and could not determine."
                    ),
                })
                try:
                    final_response = await self._call_llm_with_tools(
                        system_prompt, messages, [], model, agent_max_tokens
                    )
                    if final_response.get("text"):
                        final_text = final_response["text"]
                except Exception:  # noqa: BLE001
                    # A failure here must not abort the run — fall back to the
                    # last text we have rather than raising.
                    logger.exception(
                        f'[AGENT] AgentRunner({agent_id}): final no-tools '
                        f'completion failed — returning last text'
                    )

            # Collect any file attachments produced by tool calls.
            # Handles two shapes:
            #   - single file: result has top-level "download_url" (pptx_creator, etc.)
            #   - multi file:  result has "generated_files" array (code_executor)
            generated_files = []
            seen_urls: set = set()
            for msg in messages:
                if msg.get("role") == "tool":
                    try:
                        result = json.loads(msg.get("content", "{}"))
                    except Exception:
                        continue
                    if not isinstance(result, dict):
                        continue
                    # Multi-file array (code_executor) + swarm aggregator
                    # artifacts (which surface under ``files``). Preserve
                    # disk_name so markdown links the LLM writes using the
                    # on-disk (run-id-prefixed) name still resolve in the agent
                    # UI's buildAgentMarkdownComponents (which indexes by
                    # disk_name). Merging ``files`` here is what gives
                    # spawn_swarm sub-agent artifacts download chips.
                    for f in (result.get("generated_files") or []) + (result.get("files") or []):
                        if not isinstance(f, dict):
                            continue
                        url = f.get("download_url", "")
                        if url and url not in seen_urls:
                            seen_urls.add(url)
                            entry = {
                                "filename": f.get("filename", ""),
                                "download_url": url,
                                "format": f.get("format", ""),
                            }
                            if f.get("disk_name"):
                                entry["disk_name"] = f["disk_name"]
                            generated_files.append(entry)
                    # Single-file top-level (pptx_creator, pdf_generator, etc.)
                    url = result.get("download_url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        entry = {
                            "filename": result.get("filename", ""),
                            "download_url": url,
                            "format": result.get("format", ""),
                        }
                        if result.get("disk_name"):
                            entry["disk_name"] = result["disk_name"]
                        generated_files.append(entry)

            # Pipe the response through any attached workflows / agents the
            # user has linked to this agent via the "Attached Workflows &
            # Agents" picker. The chain is: main agent → attached[0] → ... →
            # attached[N-1]. Each step receives the previous step's output as
            # its input, and contributes any generated files. The final
            # step's text becomes the response shown in chat. If any step
            # fails, we keep the last successful text and continue so the
            # user still gets a reply.
            attached_flows = agent.get("attached_flows") or []
            if attached_flows:
                final_text, attached_files = await self._run_attached_flows(
                    attached_flows, final_text, user_id=user_id, email=email,
                    department=department, is_admin=is_admin,
                )
                # Merge any files produced by the attached chain.
                seen_urls = {f.get("download_url") for f in generated_files}
                for f in attached_files:
                    if f.get("download_url") and f["download_url"] not in seen_urls:
                        generated_files.append(f)
                        seen_urls.add(f["download_url"])

            self.monitor.log(agent_id, user_message, final_text, time.monotonic() - start)
            logger.info(
                f"[AGENT] budget checked + audited agent={agent_id} user={user_id} "
                f"model={self._run_model} in_tok={self._run_tokens_in} "
                f"out_tok={self._run_tokens_out} cost={self._run_cost:.6f}"
            )

            # ── Eval Observatory: fire-and-forget LLM-as-judge ────────────────
            # Runs after the answer is complete — zero latency impact on the user.
            # Tags as "agent_studio" so Eval Observatory can filter by platform.
            # Skipped for swarm worker agents (agent_id starts with "swarm::")
            # because those are internal sub-agents spawned by the swarm runtime,
            # not user-configured agents. Their evals would appear as extra noise
            # rows in the Observatory alongside the parent agent's own eval row.
            _is_swarm_worker = (agent_id or "").startswith("swarm::")
            if not _is_swarm_worker:
                try:
                    import threading as _abs_eval_thread
                    _abs_q   = user_message
                    _abs_ans = final_text
                    _abs_sid = user_id or None
                    _abs_rid = agent_id
                    _abs_mdl = self._run_model or None
                    def _run_abs_eval():
                        try:
                            from core.evals import eval_engine as _ee
                            _ee.eval_answer_quality(
                                _abs_q, _abs_ans, [],
                                session_id=_abs_sid,
                                run_id=_abs_rid,
                                platform="agent_studio",
                                model=_abs_mdl,
                            )
                        except Exception as _abs_eval_err:
                            logger.debug(f"[AGENT] eval_answer_quality failed (non-critical): {_abs_eval_err}")
                    _abs_eval_thread.Thread(
                        target=_run_abs_eval, daemon=True, name="eval-agent-studio"
                    ).start()
                except Exception:
                    pass

            return {
                "response": final_text,
                "generated_files": generated_files,
                # All swarm SSE frames emitted during this turn, decoded
                # to {event, data} dicts. Empty for non-delegating turns.
                # The /agent-runner/chat endpoint forwards this to the
                # frontend so the delegation-pills strip can render even
                # without SSE.
                "delegation_events": delegation_events,
                # Accumulated usage across every LLM response in this run
                # (all tool rounds + final turn) — surfaced in the chat UI.
                "usage": {
                    "model": self._run_model,
                    "tokens_in": self._run_tokens_in,
                    "tokens_out": self._run_tokens_out,
                    "cost_usd": round(self._run_cost, 6),
                    "latency_ms": int((time.monotonic() - start) * 1000),
                },
                # Coverage trace from KB full_file retrieval — populated when
                # the agent's knowledge blob contains full_file_doc_ids (i.e.
                # single-doc KBs selected in the Existing Knowledge Bases section).
                # Forwarded to the SSE agent_chat_complete event so the frontend
                # renders the same coverage badge as kb chat's KbChat.jsx.
                # None when no full_file KBs were used this turn.
                "coverage_trace": self._kb_coverage_trace,
            }
        except Exception as exc:
            self.monitor.log(
                agent_id, user_message, final_text, time.monotonic() - start, error=str(exc)
            )
            raise


# ---------------------------------------------------------------------------
# 10. MonitoringLogger
# ---------------------------------------------------------------------------


class MonitoringLogger:
    """Appends structured JSONL log entries for every agent run.

    Synchronous file writes from inside async handlers would block the event
    loop for every agent invocation — under 200+ concurrent users that becomes
    a measurable stall. ``log`` returns immediately; the actual append is
    dispatched to the default executor when a running loop is available, and
    falls back to a direct write for sync callers (tests, CLI).
    """

    def __init__(self, log_path: str) -> None:
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # Cap applied to the redacted ``error`` field. input/output already have
    # their own [:500]/[:1000] caps in log() below; error had none, so a
    # large unredacted-looking payload (e.g. a verbose upstream API error
    # body) could still land on disk near-verbatim even after redaction
    # scrubbed the known-sensitive spans. Same order of magnitude as output.
    _ERROR_MAX_CHARS = 1000

    @staticmethod
    def _redact(text: Optional[str]) -> Optional[str]:
        """Scrub PII and secrets before anything reaches disk (security
        review F-04). Reuses the same ``compliance_engine`` the live engine
        already runs on tool input/output (see native_engine._compliance_in/
        _compliance_out) so the redaction rules stay in exactly one place.
        Fails open (returns the original text) on any detector error — a
        redaction bug must never crash a log write, only skip it, same
        fail-open contract as the engine's compliance gates.
        """
        if not text:
            return text
        if _compliance_engine is None:
            return text
        try:
            redacted_text, _redacted_types = _compliance_engine.redact_text(text)
            return redacted_text
        except Exception as exc:
            logger.warning(f'[AGENT] MonitoringLogger redaction failed: {exc}')
            return text

    def _write(self, entry: dict) -> None:
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.warning(f'[AGENT] MonitoringLogger.log: {exc}')

    def log(
        self,
        agent_id: str,
        input_text: str,
        output_text: str,
        latency: float,
        error: Optional[str] = None,
    ) -> None:
        # Redact BEFORE truncation so a secret/PII match that straddles the
        # [:500]/[:1000]/[:1000] cut isn't half-masked, and before the entry
        # dict is built so nothing unredacted is ever constructed in memory
        # longer than necessary. ``error`` is capped too (previously
        # unbounded) — an upstream API error body can be large, and
        # redaction only scrubs known-sensitive spans, not overall size.
        entry = {
            "ts": time.time(),
            "agent_id": agent_id,
            "input": self._redact(input_text)[:500] if input_text else "",
            "output": self._redact(output_text)[:1000] if output_text else "",
            "latency_s": round(latency, 4),
            "error": self._redact(error)[:self._ERROR_MAX_CHARS] if error else None,
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            # Fire-and-forget — the agent run path doesn't await the log write.
            loop.run_in_executor(None, self._write, entry)
        else:
            self._write(entry)
