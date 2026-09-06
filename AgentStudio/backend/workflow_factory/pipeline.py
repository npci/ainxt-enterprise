# SPDX-License-Identifier: MIT
"""Workflow Factory Pipeline — conversational workflow generation with skill injection."""
from __future__ import annotations

import asyncio
import json

import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger
_BLUEPRINT_TIMEOUT_S = float(os.getenv("WORKFLOW_FACTORY_BLUEPRINT_TIMEOUT", "300"))

# Keys the model sometimes wraps a blueprint in, and the various names it uses
# for the node array. Shared by the structure generator's shape validation and
# by _repair_blueprint_shape so both agree on what "looks like a blueprint".
_WRAPPER_KEYS = ("graph", "workflow", "blueprint", "data", "result")
_NODE_KEYS = ("nodes", "node", "steps", "tasks")


def _looks_like_blueprint(data) -> bool:
    """Return True if `data` already carries a usable node array.

    Used to decide whether the raw LLM output is a blueprint or something we
    need to repair/retry. A bare node ({id, type, position, data}) or a
    wrapper ({"graph": {...}}) returns False so the caller can correct it.
    """
    if not isinstance(data, dict):
        return False
    if any(isinstance(data.get(nk), list) and data.get(nk) for nk in _NODE_KEYS):
        return True
    for _wk in _WRAPPER_KEYS:
        inner = data.get(_wk)
        if isinstance(inner, dict) and any(
            isinstance(inner.get(nk), list) and inner.get(nk) for nk in _NODE_KEYS
        ):
            return True
    return False


from app.core.factory_utils import (
    call_factory_llm,
    parse_json_response as _parse_json,
    extract_json_block as _extract_json_block,
    raise_if_gateway_rejection,
    SecurityGatewayRejection,
    score_catalog_match,
    semantic_catalog_match,
    MATCH_THRESHOLD,
    SKILL_KEYWORDS,
    keyword_match_tools,
    keyword_match_skills,
    build_service_index,
    agent_needs_tool,
    resolve_services_for_agent,
    _action_families,
)


# ---------------------------------------------------------------------------
# Name sanitisation (mirrors frontend src/utils/validateName.js)
# ---------------------------------------------------------------------------
# The platform rejects names that don't match the frontend regex
# ``^[A-Za-z][A-Za-z0-9 _.\-&/,'():]{0,99}$`` (must start with a letter,
# limited charset, ≤ 100 chars). The generation LLM occasionally emits names
# with emoji, leading digits, or stray punctuation, which then fail validation
# the moment the user hits Apply (``invalid_name`` 400 from agents.py /
# workflows.py). We sanitise every generated agent/workflow name server-side so
# the factory NEVER produces a name the platform will reject.

# Characters permitted AFTER the first letter, matching validateName.js.
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9 _.\-&/,'():]")


def sanitize_entity_name(raw: object, *, fallback: str = "Agent") -> str:
    """Return a name guaranteed to pass ``validateEntityName``.

    Steps mirror the frontend rules:
      - coerce to str + trim;
      - strip characters outside the allowed set;
      - ensure it starts with a letter (prefix ``fallback + ' '`` otherwise,
        which also covers empty / digits-only / symbol-led names);
      - truncate to 100 chars;
      - fall back to ``fallback`` when nothing usable remains.
    """
    name = ("" if raw is None else str(raw)).strip()
    # Drop disallowed characters (emoji, !, ?, etc.) and collapse whitespace.
    name = _NAME_ALLOWED.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip()
    # Must start with a letter — prefix when it doesn't (empty, digit- or
    # punctuation-led). The prefix itself starts with a letter.
    if not name or not re.match(r"^[A-Za-z]", name):
        name = f"{fallback} {name}".strip()
    name = name[:100].strip()
    return name or fallback


def dedupe_names(names: list[str]) -> list[str]:
    """Return ``names`` with case-insensitive duplicates suffixed " 2", " 3"…

    Mirrors ``suggestFreeName`` in validateName.js so two agents the LLM both
    called "Reviewer" don't collide. Order is preserved; the first occurrence
    keeps its name.
    """
    seen: set[str] = set()
    out: list[str] = []
    for original in names:
        base = (original or "").strip()
        candidate = base
        low = candidate.lower()
        if low in seen:
            root = re.sub(r"\s+\d+$", "", base).strip() or base
            n = 2
            while f"{root} {n}".lower() in seen:
                n += 1
            candidate = f"{root} {n}"
            low = candidate.lower()
        seen.add(low)
        out.append(candidate)
    return out


# ---------------------------------------------------------------------------
# Model preference normalisation
# ---------------------------------------------------------------------------
# Users name models loosely in chat ("use opus", "gpt 4o"). Map common aliases
# to the canonical ids from generation.py::_cli_reference_models. Anything not
# recognised falls through to the default (factory_agent_model()).

# Alias table: maps user-friendly short names to canonical model ids.
# No model ids are hardcoded here — the table is intentionally empty so that
# no vendor's models are assumed. Operators who want short-name aliases (e.g.
# "fast" → their preferred fast model) should extend this table via a local
# override or set ABSTUDIO_AGENT_DEFAULT_MODEL in their environment.
# The _TIER_PREFERENCES list below is the multi-provider preference path.
_MODEL_ALIASES: dict = {}

# Canonical ids accepted verbatim (case-insensitive) when a user pastes a
# full model id. Empty by default — the live catalogue from /llm/models is
# the authoritative source of valid ids. No model names are hardcoded.
_KNOWN_MODEL_IDS: set = set()


def normalize_model_pref(raw: object) -> Optional[str]:
    """Map a loose user model string to a canonical id, or None if unknown."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in _KNOWN_MODEL_IDS:
        return text
    if text in _MODEL_ALIASES:
        return _MODEL_ALIASES[text]
    # Substring probe so "please use opus 4" or "the sonnet model" still resolve.
    for alias, model_id in _MODEL_ALIASES.items():
        if alias in text:
            return model_id
    return None


# ---------------------------------------------------------------------------
# Per-agent model selection — LLM (with deterministic heuristic fallback)
# ---------------------------------------------------------------------------
#
# The workflow factory used to stamp every generated agent with the same
# ``ABSTUDIO_AGENT_DEFAULT_MODEL`` env value. That forced the operator to
# choose one blanket SKU regardless of what each agent actually does, and
# any typo in the env put the whole workflow into an "unknown model id"
# failure at run time.
#
# This module lets the factory pick the RIGHT model per agent from the
# user's real catalogue (the same list ``/llm/models`` returns). One
# LLM call classifies every agent in the workflow at once; a deterministic
# heuristic covers the case where the classifier LLM fails or an operator
# runs offline. Both paths choose only from ``available_models`` so the
# runtime CLI never sees an id it can't serve.


# Buckets used by BOTH the heuristic and the LLM classifier so their outputs
# are directly comparable. Tier ordering is [fast, balanced, deep].
_MODEL_TIERS = ("fast", "balanced", "deep")

# Priority-ordered picks for each tier, preferring cloud → in-house so a
# well-provisioned deployment lands on the strongest fit, but a local-only
# deployment still gets a workable choice. First entry per tier that appears
# in ``available_models`` wins; if none are present, we fall back to the
# first available model overall.
_TIER_PREFERENCES: dict = {
    "fast": [
        "claude-haiku-4-5-20251001",
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gpt-5-mini",
        "gpt-oss-120b",
        "qwen-3.6-27B",
        "gemma-4-31B-it",
    ],
    "balanced": [
        "claude-sonnet-4-6",
        "claude-sonnet-5",
        "gpt-5.4",
        "gpt-5.5",
        "gemini-3.5-flash",
        "qwen-3.6-35B-A3B",
    ],
    "deep": [
        "claude-opus-4-8",
        "claude-opus-4-7",
        "claude-opus-5",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "claude-sonnet-5",
        "claude-sonnet-4-6",
    ],
}


# Keywords that push a role toward the "deep" tier — reasoning-heavy,
# multi-step, or safety-critical work where extra capability is worth it.
_DEEP_KEYWORDS = (
    "judge", "evaluate", "review", "critic", "audit", "compliance",
    "legal", "reason", "plan", "architect", "design", "strategy",
    "analyze", "analyse", "diff", "code review", "root cause",
    "risk", "policy interpretation",
)
# Keywords that push a role toward the "fast" tier — short, mechanical,
# or user-facing quick replies that don't justify the extra latency/cost
# of a deep model.
_FAST_KEYWORDS = (
    "classify", "classifier", "route", "router", "triage", "greet",
    "greeting", "fallback", "hello", "welcome", "lookup", "fetch",
    "summarize brief", "one-liner", "short reply", "quick reply",
)


def _tier_for_agent(node: dict) -> str:
    """Deterministic tier heuristic — reads the agent's name + instructions."""
    d = node.get("data") or {}
    haystack = " ".join([
        str(d.get("name") or ""),
        str(d.get("instructions") or ""),
    ]).lower()

    if any(k in haystack for k in _FAST_KEYWORDS):
        return "fast"
    if any(k in haystack for k in _DEEP_KEYWORDS):
        return "deep"
    # Long, multi-section instructions imply meatier work → balanced+.
    if len(haystack) > 1500:
        return "balanced"
    return "balanced"


def _resolve_tier_to_model(tier: str, available_models: list) -> Optional[str]:
    """Return the first preference for ``tier`` that's in ``available_models``.

    Never falls back to a model the runtime can't serve — everything comes
    from ``available_models`` (populated from ``/llm/models``, which is
    what the CLI actually accepts).
    """
    if not available_models:
        return None
    prefs = _TIER_PREFERENCES.get(tier) or _TIER_PREFERENCES["balanced"]
    for candidate in prefs:
        if candidate in available_models:
            return candidate
    # Nothing in the tier preference matched. Pick a sensible neighbour:
    #  - "deep" downgrades toward the strongest available balanced model
    #  - anything else falls back to the first non-pseudo id
    if tier == "deep":
        for candidate in _TIER_PREFERENCES["balanced"]:
            if candidate in available_models:
                return candidate
    for m in available_models:
        if m and m != "ainxt-auto-route":
            return m
    return None


async def _classify_agent_tiers_via_llm(
    agent_nodes: list[dict],
) -> Optional[dict]:
    """Ask the factory LLM to pick a tier per agent.

    Returns ``{node_id: "fast"|"balanced"|"deep"}`` on success, or ``None``
    when the LLM call fails / returns unusable output. The heuristic path
    covers the failure case so a single flaky LLM turn never leaves the
    workflow with an unset model.
    """
    if not agent_nodes:
        return {}

    def _short(txt: str, n: int = 400) -> str:
        return txt if len(txt) <= n else txt[:n] + "…"

    payload = []
    for n in agent_nodes:
        d = n.get("data") or {}
        payload.append({
            "id": n.get("id"),
            "name": d.get("name"),
            "instructions_preview": _short(str(d.get("instructions") or "")),
        })
    system = (
        "You pick a model tier for each agent in a workflow based on how "
        "hard its job is.\n\n"
        "Tiers:\n"
        "  * fast     — short, mechanical, user-facing (greeter, classifier, "
        "router, quick lookup). Latency matters more than depth.\n"
        "  * balanced — most business agents: policy answers, summaries, "
        "structured extraction, single-step reasoning.\n"
        "  * deep     — heavy reasoning: code review, legal / compliance "
        "interpretation, multi-step planning, judged retries, root-cause "
        "analysis. Only pick this when the job genuinely needs it.\n\n"
        "Rules:\n"
        "  * Return ONLY valid JSON, no markdown.\n"
        "  * Shape: {\"tiers\": {\"<node_id>\": \"fast|balanced|deep\", …}}.\n"
        "  * Include EVERY agent id in the input. If unsure, choose "
        '"balanced".'
    )
    try:
        raw = await call_factory_llm(
            system,
            [{"role": "user", "content": json.dumps({"agents": payload}, ensure_ascii=False)}],
            max_tokens=1024,
            temperature=0.1,
        )
        parsed = _parse_json(raw)
        if not isinstance(parsed, dict):
            return None
        tiers = parsed.get("tiers")
        if not isinstance(tiers, dict):
            return None
        out: dict = {}
        for node in agent_nodes:
            nid = node.get("id")
            raw_tier = str(tiers.get(nid) or "").strip().lower()
            if raw_tier not in _MODEL_TIERS:
                raw_tier = _tier_for_agent(node)  # heuristic backup per-node
            out[nid] = raw_tier
        return out
    except SecurityGatewayRejection:
        raise
    except Exception as exc:
        logger.warning(f'[AGENT] _classify_agent_tiers_via_llm: {exc}')
        return None


async def _assign_agent_models(
    agent_nodes: list[dict],
    available_models: Optional[list],
    forced_model: Optional[str] = None,
) -> dict:
    """Return ``{node_id: model_id}`` picking a model for every agent node.

    * ``forced_model`` (set when the user named a preferred model in chat)
      always wins — every agent lands on the same id.
    * Otherwise we try the LLM classifier + heuristic fallback and resolve
      each tier to a real model from ``available_models``.
    * If ``available_models`` is empty (catalogue lookup failed) every
      agent inherits ``forced_model`` if set, else "" (caller falls back
      to the env default).
    """
    if not agent_nodes:
        return {}
    if forced_model:
        return {n.get("id"): forced_model for n in agent_nodes}
    if not available_models:
        return {n.get("id"): "" for n in agent_nodes}

    tiers = await _classify_agent_tiers_via_llm(agent_nodes) or {}
    out: dict = {}
    for node in agent_nodes:
        nid = node.get("id")
        tier = tiers.get(nid) or _tier_for_agent(node)
        model = _resolve_tier_to_model(tier, available_models)
        out[nid] = model or ""
    return out


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

_MAX_WF_SESSIONS = int(os.getenv("WORKFLOW_FACTORY_MAX_SESSIONS", "200"))


@dataclass
class WorkflowFactorySession:
    session_id: str
    # Stages: clarifying → (suggest_existing) → (clarifying_tools) → generating → confirm → done
    stage: str = "clarifying"
    messages: list[dict] = field(default_factory=list)
    requirements: dict = field(default_factory=dict)
    workflow_data: Optional[dict] = None   # {name, graph_data: {nodes, edges}}
    turn_count: int = 0
    # Existing workflows/templates the semantic matcher flagged as
    # near-duplicates of the user's request. Populated when the stage is
    # "suggest_existing"; the user either opens one of these or asks to build
    # new anyway (which clears this and proceeds to generation).
    pending_matches: list[dict] = field(default_factory=list)
    # Answers the user gave to the consolidated tool/skill clarifying question
    # (stage "clarifying_tools"). Folded into requirements so the assigner
    # honours them, e.g. {"__service__:notifier-id": "slack", "__report__": "xlsx"}.
    tool_choices: dict = field(default_factory=dict)
    # External systems the user declared in Plan Card Q6. Used as a HARD
    # inclusion constraint by WorkflowToolSkillAssigner (fixes FM-B) and checked
    # against the live catalog before generation (plan_card_service_warning).
    required_services: list = field(default_factory=list)


_wf_sessions: dict[str, WorkflowFactorySession] = {}


def get_or_create_wf_session(session_id: Optional[str]) -> WorkflowFactorySession:
    if session_id and session_id in _wf_sessions:
        return _wf_sessions[session_id]
    # Evict oldest sessions when the cache exceeds its limit to prevent
    # unbounded memory growth on long-running servers.
    if len(_wf_sessions) >= _MAX_WF_SESSIONS:
        # dict preserves insertion order in Python 3.7+; pop the oldest
        oldest = next(iter(_wf_sessions))
        _wf_sessions.pop(oldest, None)
    sid = session_id or str(uuid.uuid4())
    session = WorkflowFactorySession(session_id=sid)
    _wf_sessions[sid] = session
    return session


# ---------------------------------------------------------------------------
# Session persistence (Postgres write-through / read-through)
# ---------------------------------------------------------------------------
# Mirror an in-memory build session to the ``factory_sessions`` table so an
# interrupted "build me a workflow" conversation survives a backend restart.
# Best-effort — persistence failures never break the live chat turn.

WF_FACTORY_TYPE = "workflow"


def serialize_wf_session(session: WorkflowFactorySession) -> dict:
    from dataclasses import asdict
    return asdict(session)


def hydrate_wf_session(state: dict) -> WorkflowFactorySession:
    known = {f for f in WorkflowFactorySession.__dataclass_fields__}  # type: ignore[attr-defined]
    return WorkflowFactorySession(**{k: v for k, v in (state or {}).items() if k in known})


async def get_or_restore_wf_session(
    session_id: Optional[str], owner_user_id: str
) -> WorkflowFactorySession:
    if session_id and session_id in _wf_sessions:
        return _wf_sessions[session_id]
    if session_id:
        try:
            from app.core import workflow_repo
            state = await workflow_repo.load_factory_session(
                session_id, WF_FACTORY_TYPE, owner_user_id
            )
            if state:
                session = hydrate_wf_session(state)
                _wf_sessions[session.session_id] = session
                return session
        except Exception:
            logger.debug('[AGENT] workflow_factory: session restore skipped', exc_info=True)
    return get_or_create_wf_session(session_id)


async def persist_wf_session(
    session: WorkflowFactorySession, owner_user_id: str
) -> None:
    try:
        from app.core import workflow_repo
        await workflow_repo.save_factory_session(
            session.session_id, WF_FACTORY_TYPE, owner_user_id,
            serialize_wf_session(session),
        )
    except Exception:
        logger.debug('[AGENT] workflow_factory: session persist skipped', exc_info=True)


# ---------------------------------------------------------------------------
# Plan Card Generator — structured pre-generation questionnaire
# ---------------------------------------------------------------------------

class WorkflowPlanCardGenerator:
    """Produce a structured Plan Card for the workflow factory.

    Q1-Q5 are static; Q6 (``external_systems``) is catalog-driven — its options
    are the live service names, so the chips never drift from the real tool
    catalog. Q6 is multi-select and allows free-text for services not yet in the
    catalog. One fast LLM call infers the best default per question (options are
    always the static/derived lists — never hallucinated).

    ``catalog_services`` is passed in by the API layer (it owns ``catalog_cache``)
    so this module needs no cross-module import.
    """

    BASE_QUESTIONS = [
        {"id": "trigger_type", "label": "What triggers this workflow?",
         "options": ["Manual", "Scheduled", "Event/webhook", "API call"]},
        {"id": "failure_policy", "label": "What happens if a step fails?",
         "options": ["Stop and alert", "Retry automatically", "Skip and continue", "Ask a human"]},
        {"id": "approval_gate", "label": "Who approves the final output?",
         "options": ["No approval needed", "One approver", "Team review", "Compliance gate"]},
        {"id": "step_count", "label": "How many steps do you expect?",
         "options": ["Let AI decide", "2–3 steps", "4–6 steps", "7+ steps"]},
        {"id": "share_context", "label": "Should agents share context?",
         "options": ["Yes, pass results forward", "No, each runs independently"]},
    ]

    async def generate(self, intent: dict, user_message: str,
                       catalog_services: Optional[list[str]] = None) -> dict:
        questions = [dict(q) for q in self.BASE_QUESTIONS]

        # Q6 — external systems (catalog-driven, multi-select).
        svc_options = list(catalog_services or [])
        q6 = {
            "id": "external_systems",
            "label": "Which external systems does this workflow need to connect to?",
            "options": ["None — reasoning only", *svc_options],
            "default": "None — reasoning only",
            "multi_select": True,
            "allow_freetext": True,
        }
        questions.append(q6)

        for q in questions:
            q.setdefault("default", q["options"][0])

        # Reuse the shared inference helper (defined in this module below).
        defaults = await _infer_plan_card_defaults(
            [q for q in questions if not q.get("multi_select")],
            user_message, intent,
            context="You are configuring a multi-agent workflow.",
        )
        for q in questions:
            if q.get("multi_select"):
                continue
            picked = defaults.get(q["id"])
            if picked in q["options"]:
                q["default"] = picked
        return {"questions": questions}


async def _infer_plan_card_defaults(
    questions: list[dict], user_message: str, intent: dict, context: str,
) -> dict:
    """One fast LLM call: pick the best default option per question.

    Returns ``{question_id: chosen_option}``; never raises (returns ``{}`` on
    failure so callers keep the seeded first-option defaults). Options are
    constrained to the static lists — hallucinated values are dropped by callers.
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
            'Return ONLY a JSON object mapping each id to your chosen option. No markdown.'
        )
        raw = await call_factory_llm(
            system,
            [{"role": "user", "content": (user_message or "")[:1500]}],
            max_tokens=256,
        )
        parsed = _parse_json(raw)
        return parsed if isinstance(parsed, dict) else {}
    except SecurityGatewayRejection:
        raise
    except Exception as exc:
        logger.warning(f'[AGENT] workflow plan-card default inference failed: {exc}')
        return {}


# ---------------------------------------------------------------------------
# Clarification Engine
# ---------------------------------------------------------------------------

class WorkflowClarificationEngine:
    """Decides whether the user's message is detailed enough to skip
    clarification and go straight to blueprint generation.

    On the first turn, a fast heuristic checks whether the message
    contains enough signal (word count + step/agent indicators).  If it
    does, the single LLM call doubles as *both* requirement extraction
    and clarification — it's told ``done=true`` from the start, which
    eliminates one full round-trip.
    """

    MAX_TURNS = 4

    # Heuristic thresholds for skipping clarification on turn 1.
    # Raised from the original 25/3 so the factory ASKS at least one clarifying
    # question in the common case — users expect a conversation ("what do you
    # want / how do you want it") before a workflow is generated, not an instant
    # one-shot. Only a genuinely spec-complete first message (long + many
    # explicit steps) still skips straight to generation.
    _MIN_WORDS_FOR_AUTO_DONE = 60
    _STEP_INDICATORS = re.compile(
        r"\b(pipeline|process|classify|extract|summarize|review|validate|"
        r"send|generate|analyze|transform|filter|route|notify|check|"
        r"fetch|create|update|delete|parse|draft|schedule)\b",
        re.IGNORECASE,
    )

    def _is_detailed_enough(self, text: str) -> bool:
        """Fast local check: does the first message contain enough detail
        to skip the clarification LLM call entirely?

        Deliberately conservative — we'd rather ask one extra question than
        generate a vague workflow from an underspecified first message.
        """
        words = text.split()
        if len(words) < self._MIN_WORDS_FOR_AUTO_DONE:
            return False
        step_matches = self._STEP_INDICATORS.findall(text)
        return len(step_matches) >= 4

    async def get_next_question_or_requirements(
        self, messages: list[dict]
    ) -> dict:
        user_turns = sum(1 for m in messages if m["role"] == "user")
        force_done = user_turns >= self.MAX_TURNS

        # On the first turn with a detailed message, skip straight to
        # requirement extraction — saves one full LLM round-trip (~5-15s).
        first_msg = messages[0]["content"] if messages else ""
        if user_turns == 1 and self._is_detailed_enough(first_msg):
            force_done = True

        conversation = "\n".join(
            f"{m['role'].upper()}: {m['content']}" for m in messages[-6:]
        )

        prompt = (
            "You are a workflow design assistant. Help the user build a multi-agent pipeline.\n"
            f"Conversation so far:\n{conversation}\n\n"
            "To generate a good workflow you need to know:\n"
            "  1. Main purpose — what problem does this workflow solve?\n"
            "  2. Agent roles — what distinct steps or agents are involved, and what does each one do?\n"
            "  3. Data flow — what goes in at the start, what comes out at the end?\n"
            "  4. Behaviour preferences — does it need human approval/review at any step (HITL), a "
            "knowledge base / reference documents (RAG), repeated refinement until some quality bar "
            "is met (a loop), or branching based on a decision (a condition)? Which LLM model should "
            "the agents use, if the user has a preference?\n\n"
            "RULES:\n"
            "- Have a real conversation. Unless the user's message ALREADY specifies purpose, the "
            "distinct agent roles, AND the data flow, ask at least ONE clarifying question before "
            "finishing — do not rush to done=true on a vague request.\n"
            "- Ask ONE short, specific question at a time. Proactively PROPOSE useful capabilities "
            "when the described flow implies them, e.g. 'This has an approval step — I'll add a "
            "human-approval gate (HITL). Want that?' or 'Should the agents pull from a knowledge "
            "base?'. Let the user confirm or decline.\n"
            "- Be warm and direct, not formal.\n"
            "- Provide exactly 4 suggestion chips that are DIRECT ANSWERS to your question — concrete choices the user can click instead of typing. Example: if asking 'How should data flow?', use chips like '📊 One-way sync' or '🔄 Two-way bidirectional'. Never use vague chips like 'Option 1' or 'Yes'.\n"
            f"{'IMPORTANT: SET done=true NOW — you have enough context. Stop asking and commit.' if force_done else ''}\n\n"
            "When done=true, produce detailed requirements — vague requirements produce vague workflows:\n"
            "- name: a short descriptive name (e.g. 'Document Processing Pipeline', not 'Generated Workflow')\n"
            "- purpose: one precise sentence — what problem does this solve for the user?\n"
            "- agent_roles: list of objects, each with 'role' and 'job' (one sentence describing what that agent does)\n"
            "- data_in: what the workflow receives as input\n"
            "- data_out: what the workflow produces as final output\n"
            "- needs_human_review: true only if the user wants human approval/review/sign-off at some step\n"
            "- needs_knowledge_base: true only if agents should reference a knowledge base / documents (RAG)\n"
            "- needs_iteration: true only if some step should repeat until a quality bar is met (a loop)\n"
            "- needs_branching: true only if the flow branches on a decision (a condition)\n"
            "- preferred_model: the model the user asked for (e.g. 'opus', 'sonnet', 'haiku'), or \"\" if none\n"
            "- additional_notes: anything else relevant\n\n"
            "Return ONLY valid JSON, no markdown:\n"
            "{\n"
            '  "done": true | false,\n'
            '  "question": "your question (omit when done=true)",\n'
            '  "suggestions": [{"icon": "emoji", "label": "3-5 word option"}, ...],\n'
            '  "requirements": {\n'
            '    "name": "Descriptive Workflow Name",\n'
            '    "purpose": "precise one-sentence purpose",\n'
            '    "agent_roles": [{"role": "Role Name", "job": "what this agent does"}],\n'
            '    "data_in": "what enters the workflow",\n'
            '    "data_out": "what exits the workflow",\n'
            '    "needs_human_review": false,\n'
            '    "needs_knowledge_base": false,\n'
            '    "needs_iteration": false,\n'
            '    "needs_branching": false,\n'
            '    "preferred_model": "",\n'
            '    "additional_notes": ""\n'
            '  }\n'
            "}"
        )

        try:
            raw = await call_factory_llm(
                "You are a workflow design assistant.",
                [{"role": "user", "content": prompt}],
                max_tokens=32000,
            )
            parsed = json.loads(_extract_json_block(raw))
            if force_done and not parsed.get("done"):
                parsed["done"] = True
            if parsed.get("done") and not parsed.get("requirements"):
                parsed["requirements"] = self._fallback_requirements(messages)
            # Deterministic enrichment: small local LLMs occasionally return
            # a "done" verdict with empty agent_roles / purpose / branching
            # signals for even a clearly-branching prompt. We reinforce those
            # fields from the raw user message so the downstream structure
            # generator has enough signal to build a multi-branch graph.
            # When the LLM has no ``requirements`` yet (still asking follow-up
            # questions) we still populate a seed with the enriched values so
            # callers that read ``clar.get("requirements") or {}`` and fall
            # back on their own defaults don't lose our extracted signal.
            if parsed.get("done"):
                parsed["requirements"] = _enrich_requirements_from_messages(
                    parsed.get("requirements") or {}, messages,
                )
            else:
                parsed.setdefault("requirements", {})
                parsed["requirements"] = _enrich_requirements_from_messages(
                    parsed["requirements"], messages,
                )
            return parsed
        except Exception as exc:
            logger.warning(f'[AGENT] WorkflowClarificationEngine: {exc}')
            return {"done": True, "requirements": self._fallback_requirements(messages)}

    def _fallback_requirements(self, messages: list[dict]) -> dict:
        text = " ".join(m["content"] for m in messages if m["role"] == "user")
        base = {
            "name": "Generated Workflow",
            "purpose": text[:200],
            "agent_roles": [],
            "data_in": "user-provided input",
            "data_out": "processed result",
            "additional_notes": "",
        }
        return _enrich_requirements_from_messages(base, messages)


# ---------------------------------------------------------------------------
# Deterministic requirements enricher — LLM safety net
# ---------------------------------------------------------------------------
#
# Small local LLMs (e.g. gpt-oss-120b in local-only deployments) occasionally
# return an empty ``agent_roles``/``purpose`` for a clearly-detailed request.
# The structure generator then falls back to a single "General Processor"
# agent even when the user described three explicit branches. This helper
# runs after the LLM step and rescues the requirements deterministically:
#
#   * ``purpose`` is filled from the first user message when blank.
#   * ``needs_branching`` flips to true when the raw prompt contains multiple
#     ``if X`` clauses or explicit ``otherwise``/``else``/``none``/``no match``.
#   * ``agent_roles`` is synthesised from the ``if <intent>, separate agent``
#     pattern so the LLM sees named roles and knows how many branch agents
#     to emit.
#
# The LLM's own extraction wins whenever it was non-empty — we only fill
# missing fields, never overwrite a good value.


_BRANCH_RE = re.compile(
    r"\b(?:if|for|when|whenever)\b[^,.;\n]{2,120}",
    re.IGNORECASE,
)
_BRANCH_LABEL_RE = re.compile(
    r"\b(?:if|for|when|whenever)\b\s+"
    r"(?:the\s+|it\s+is\s+|its\s+|that\s+is\s+|nothing\s+is\s+|any\s+)?"
    r"(?:related\s+to\s+|about\s+|matches\s+|matching\s+)?"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _\-]{1,60}?)"
    r"(?:\s+(?:policy|policies|query|question|questions|request|requests|"
    r"topic|topics|category|categories|type|types|issue|issues|intent|"
    r"related\s+details|related\s+detail))?"
    r"(?:,|\.|;|\n|\s+(?:i\s+want|then|there\s+should|use|route|send|"
    r"a\s+separate|another|different|distinct|dedicated|specialist)\b)",
    re.IGNORECASE,
)
_ELSE_HINT_RE = re.compile(
    r"\b(?:otherwise|else|nothing|no\s+match|neither|none\s+of|"
    r"if\s+not|fallback|default|any\s+other)\b",
    re.IGNORECASE,
)


def _enrich_requirements_from_messages(requirements: dict, messages: list[dict]) -> dict:
    """Fill missing / weak requirement fields deterministically.

    Never overrides a value the LLM already produced. Reads only user
    messages so an assistant echo of the plan-card prompt doesn't sneak
    fake signal into the requirements.
    """
    if not isinstance(requirements, dict):
        return requirements

    user_text = "\n".join(
        m.get("content") or ""
        for m in messages or []
        if m.get("role") == "user" and not str(m.get("content", "")).startswith("__plan_card__:")
    ).strip()
    if not user_text:
        return requirements

    # 1. purpose — first non-empty user message when the LLM left it blank.
    if not str(requirements.get("purpose") or "").strip():
        first_msg = next(
            (m.get("content") or "" for m in messages or []
             if m.get("role") == "user" and not str(m.get("content", "")).startswith("__plan_card__:")),
            "",
        )
        if first_msg:
            requirements["purpose"] = first_msg.strip()[:400]

    # 2. Detect branching intent.
    if_clauses = _BRANCH_RE.findall(user_text)
    has_else = bool(_ELSE_HINT_RE.search(user_text))
    likely_branching = len(if_clauses) >= 2 or (len(if_clauses) >= 1 and has_else)
    if likely_branching and not requirements.get("needs_branching"):
        requirements["needs_branching"] = True
        logger.info(
            f'[AGENT] _enrich_requirements: forced needs_branching=true '
            f'({len(if_clauses)} if-clauses, has_else={has_else})'
        )

    # 3. Synthesise agent_roles when the LLM produced none but branch
    # labels are extractable from the raw prompt.
    current_roles = requirements.get("agent_roles") or []
    if likely_branching and not current_roles:
        synth_roles: list[dict] = []
        seen_labels: set = set()
        # Negation / else-signalling starters — these clauses describe the
        # catch-all branch, not a real specialist. We record the fact via
        # ``has_else`` but do NOT emit a specialist role for them.
        _ELSE_STARTERS = ("nothing", "no one", "none", "not ", "neither",
                          "otherwise", "any other", "no match")

        # First "for X ... a separate agent" is often part of the workflow
        # title ("Create a workflow FOR HR policy Q&A agent"). Track whether
        # we've already dropped a title-like `for` clause so the SECOND `for`
        # clause (a real branch, e.g. "for other policy related details") is
        # not misidentified as another title.
        title_scope_seen = False

        for m in _BRANCH_LABEL_RE.finditer(user_text):
            raw_label = m.group("label").strip()
            trigger = user_text[max(0, m.start()):m.start() + 4].lower()
            starts_with_for = trigger.startswith("for")

            if len(raw_label) < 2 or len(raw_label) > 120:
                continue

            key_raw = raw_label.lower()
            if any(key_raw.startswith(s) for s in _ELSE_STARTERS):
                continue  # else-branch clause, not a real specialist

            # Prune the label deterministically: strip trailing junk like
            # "related details a separate agent" so `other policy related
            # details a seperate agent` collapses to `other policy`.
            pruned = _prune_branch_label(raw_label)
            if not pruned:
                continue
            key = pruned.lower()
            if key in seen_labels:
                continue

            # The first ``for`` clause in a user prompt is very often the
            # workflow title — skip it once. Subsequent ``for`` clauses are
            # real branches.
            if starts_with_for and not title_scope_seen and _looks_like_workflow_title(pruned):
                title_scope_seen = True
                continue

            seen_labels.add(key)
            role_name = _titleize_role(pruned) + " Agent"
            synth_roles.append({
                "role": role_name,
                "job": f"Handles requests related to {pruned.lower()}.",
            })
        # Front-of-flow classifier is almost always needed with branching.
        classifier_role = {
            "role": "Intent Classifier",
            "job": "Classifies the incoming request so the condition node can route it to the right specialist agent.",
        }
        if has_else:
            synth_roles.append({
                "role": "Fallback Handler",
                "job": "Handles requests that don't match any specialist branch (greeting / redirect / graceful decline).",
            })
        if synth_roles:
            requirements["agent_roles"] = [classifier_role] + synth_roles
            logger.info(
                f'[AGENT] _enrich_requirements: synthesised {len(requirements["agent_roles"])} agent_roles '
                f'from branching signal'
            )

    # 4. data_in / data_out — the structure generator uses these to shape
    # the start/end nodes' payload contracts. Fill safe defaults so blank
    # strings don't leak into the prompt.
    if not str(requirements.get("data_in") or "").strip():
        requirements["data_in"] = "The user's question or request as free-form text."
    if not str(requirements.get("data_out") or "").strip():
        requirements["data_out"] = "A single response from the routed agent (text / markdown)."

    # 5. name — synthesise a short, human-readable title. We deliberately
    # rebuild this even when the LLM produced one, because small local
    # models tend to echo the raw user prompt back as the name ("Create A
    # Workflow For Hr Policy Question And Answer Agent"), and existing
    # placeholder titles ("Generated Workflow") aren't useful either.
    current_name = str(requirements.get("name") or "").strip()
    if not current_name or _looks_like_raw_prompt(current_name):
        requirements["name"] = _synthesise_workflow_name(
            requirements, user_text,
        )

    return requirements


# Verb / preamble prefixes users typically start a workflow prompt with. We
# strip these when synthesising the workflow name so the title focuses on
# the SUBJECT ("HR Policy Q&A") rather than the ask ("Create a workflow for
# HR Policy Q&A").
_PROMPT_PREAMBLE_RE = re.compile(
    r"^\s*(?:please\s+)?"
    r"(?:can\s+you\s+)?"
    r"(?:"
    r"(?:i\s+(?:want|need|would\s+like|'d\s+like)\s+(?:to\s+)?"
    r"(?:create|build|make|design|generate|set\s+up|setup|"
    r"put\s+together|assemble)?\s*)"
    r"|"
    r"(?:(?:create|build|make|design|generate|set\s+up|setup|"
    r"put\s+together|assemble)\s+)"
    r")"
    r"(?:a|an|the)?\s*"
    r"(?:new\s+)?"
    r"(?:multi[-\s]?agent\s+)?"
    r"(?:agent\s+)?"
    r"(?:workflow|pipeline|process|flow|automation)"
    r"(?:\s+(?:for|to|that\s+(?:will|can|does|reviews|routes|handles)"
    r"|which|about|around|which\s+will|to\s+handle))?\s*",
    re.IGNORECASE,
)

# Trailing wrapper words we strip from a synthesised workflow name — these
# describe the entity type ("workflow", "agent", "pipeline") rather than the
# workflow's subject.
_TITLE_TRAILING_WRAPPERS = {
    "agent", "agents", "workflow", "workflows", "pipeline", "pipelines",
    "process", "processes", "flow", "flows", "automation", "system",
    "systems", "handler", "handlers", "assistant", "assistants", "bot",
    "bots", "app", "application",
}

# Leading connector / relative-pronoun tokens that add noise when we drop the
# preamble ("Build a workflow **that reviews** ..." → "reviews"). Strip them
# recursively from the head of the extracted subject.
_TITLE_LEADING_STRIPS = {"that", "which", "who", "whose", "to", "for", "of",
                         "the", "a", "an", "and", "or"}

_TITLE_STOPWORDS = {"a", "an", "the", "for", "to", "of", "and", "or", "in",
                    "on", "at", "with", "by", "that", "which"}


def _looks_like_raw_prompt(name: str) -> bool:
    """True when the workflow name reads like the raw user prompt.

    Small local LLMs occasionally echo the prompt back as the ``name``
    field (e.g. ``Create A Workflow For Hr Policy Question And Answer
    Agent``). We treat these as unusable and re-synthesise them.
    """
    lower = name.lower()
    if len(lower.split()) >= 8:
        return True  # 8+ words is almost never a good workflow title
    if _PROMPT_PREAMBLE_RE.match(lower):
        return True  # starts with "create a workflow for …"
    if lower in ("generated workflow", "new workflow", "workflow"):
        return True
    return False


def _synthesise_workflow_name(requirements: dict, user_text: str) -> str:
    """Compose a short, descriptive workflow title.

    Strategy — try each in order, return the first non-empty result:

      1. If the requirements have specialist ``agent_roles`` (a branching
         workflow) → pick the subject from the first user sentence and
         suffix with ``Router`` / ``Q&A`` depending on the flow shape.
      2. Otherwise → strip the ``Create a workflow for …`` preamble from
         the first sentence and title-case what's left.
      3. Fall back to ``Generated Workflow`` when nothing survives.

    Result is always title-cased and capped at ~60 chars.
    """
    first_sentence = (user_text or "").split(".")[0].strip()
    # Strip the "create a workflow for …" preamble so we keep the subject only.
    subject = _PROMPT_PREAMBLE_RE.sub("", first_sentence, count=1).strip(" ,.")
    subject = subject.strip()

    if not subject:
        purpose = str(requirements.get("purpose") or "").strip()
        subject = _PROMPT_PREAMBLE_RE.sub("", purpose, count=1).strip(" ,.")

    if not subject:
        return "Generated Workflow"

    subject = _clip_at_first_clause(subject)
    subject = _strip_leading_connectors(subject)
    subject = _strip_trailing_wrappers(subject)
    if not subject:
        return "Generated Workflow"

    title = _title_case_smart(subject)

    # Suffix decision: routing workflows land on ``Router``, Q&A stays
    # as-is (already reads well), everything else stays plain.
    is_branching = bool(requirements.get("needs_branching"))
    already_qa = any(k in title.lower() for k in ("q&a", "qa", "question", "answer"))
    if is_branching and not already_qa and "router" not in title.lower():
        title = f"{title} Router"

    # Trim to a friendly length.
    if len(title) > 60:
        title = title[:60].rsplit(" ", 1)[0]

    return title or "Generated Workflow"


def _strip_leading_connectors(text: str) -> str:
    """Drop noise words from the head of a synthesised subject."""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    while tokens and tokens[0].lower().strip(".,;:") in _TITLE_LEADING_STRIPS:
        tokens.pop(0)
    return " ".join(tokens)


def _strip_trailing_wrappers(text: str) -> str:
    """Drop entity-type wrapper words from the tail of a synthesised subject."""
    tokens = [t for t in re.split(r"\s+", text.strip()) if t]
    while tokens and tokens[-1].lower().strip(".,;:?!") in _TITLE_TRAILING_WRAPPERS:
        tokens.pop()
    return " ".join(tokens)


def _clip_at_first_clause(text: str) -> str:
    """Cut ``text`` at the first sub-clause boundary so the title stays terse.

    ``HR policy question and answer. Use a condition agent to classify`` →
    ``HR policy question and answer``. Splits on colons, semicolons, and
    conjunction words that typically start a follow-up clause.
    """
    # ``Customer Support Router: Billing goes to Alice, …`` → clip at the
    # colon so the descriptor list doesn't leak into the title.
    if ":" in text:
        idx = text.find(":")
        if idx > 4:
            text = text[:idx].strip()
    for sep in (";", ", use ", ", with ", ", that ", ", where ", ", and use ",
                " if ", " when "):
        idx = text.lower().find(sep.lower())
        if idx > 4:
            text = text[:idx].strip()
            break
    return text.strip(" ,.?!")


def _title_case_smart(text: str) -> str:
    """Title-case ``text`` while preserving common acronyms + small words.

    ``hr policy question and answer`` → ``HR Policy Question and Answer``.
    Keeps ``HR``, ``Q&A``, ``KB``, ``PDF``, ``API`` etc. upper-cased and
    leaves ``a / an / the / and / or / of / for / to`` lowercase (except
    when they open the title).
    """
    acronyms = {"hr", "qa", "q&a", "kb", "pdf", "api", "sql", "csv", "faq",
                "id", "mr", "pr", "ci", "cd", "ui", "ux", "sso", "sla",
                "aws", "gcp", "sap", "erp", "crm", "hrms", "kpi"}
    # Whole-token first so compound tokens like ``q&a`` survive the split.
    words = re.split(r"(\s+|/|\-)", text.strip())
    out: list[str] = []
    for i, tok in enumerate(words):
        if not tok.strip() or tok in ("/", "-", " "):
            out.append(tok)
            continue
        lower = tok.lower()
        if lower in acronyms:
            out.append("Q&A" if lower in ("q&a", "qa") else lower.upper())
        elif i > 0 and lower in _TITLE_STOPWORDS:
            out.append(lower)
        else:
            out.append(tok[:1].upper() + tok[1:].lower() if len(tok) > 1 else tok.upper())
    return "".join(out).strip()


def _prune_branch_label(raw_label: str) -> str:
    """Trim structural / connector tokens off an extracted branch label.

    Real-world prompts contain filler like "a separate agent",
    "related details", "questions" or "policy" tacked onto the branch
    label. The label the user actually meant is the head noun phrase,
    e.g. ``other polilcy related details a seperate agent`` → ``other
    polilcy`` → ``other`` (after stopword strip in ``_titleize_role``).

    Returns an empty string when nothing meaningful remains — the caller
    will skip that match.
    """
    label = raw_label.strip()

    # 1. Cut the label at any structural connector — that phrase is filler,
    # not the branch subject. "medical insurance policy i want a separate"
    # → "medical insurance policy".
    _CONNECTORS = (
        " a separate", " another agent", " a different", " dedicated",
        " specialist agent", " related details", " related detail",
        " i want", " i need", " i'd like", " then", " use ", " route ",
        " send ", " should ",
    )
    lower = label.lower()
    for connector in _CONNECTORS:
        idx = lower.find(connector)
        if idx > 0:
            label = label[:idx].strip()
            lower = label.lower()

    # 2. Peel wrapper words off the tail — they don't add signal.
    _TAIL_WRAPPERS = {
        "policy", "policies", "query", "queries", "question", "questions",
        "request", "requests", "topic", "topics", "category", "categories",
        "type", "types", "issue", "issues", "detail", "details",
        "agent", "handler", "assistant", "bot",
    }
    tokens = [t for t in re.split(r"\s+", label) if t]
    while tokens and tokens[-1].lower().strip(".,;:") in _TAIL_WRAPPERS:
        tokens.pop()

    return " ".join(tokens).strip()


def _looks_like_workflow_title(label: str) -> bool:
    """Heuristic — does this label read like a workflow-scope description?

    Only used to discard the FIRST ``for X`` clause in a user prompt when
    it's clearly the pipeline's own name ("Create a workflow for HR policy
    Q&A") rather than a branch. Later ``for X`` clauses always describe
    real branches so this predicate never runs against them.
    """
    lower = label.lower().strip()
    # Very short labels ("medical", "other") are unambiguously branch
    # subjects, not workflow scopes.
    if len(lower.split()) <= 2:
        return False
    # A workflow-title label typically contains "HR" / "policy" / "Q&A" or
    # simply the phrase "question and answer".
    hints = ("q&a", "question and answer", "questions and answers")
    return any(h in lower for h in hints)


def _titleize_role(label: str) -> str:
    """Turn a raw branch label into a title-cased role name.

    "medical insurance policy" → "Medical Insurance Policy".
    Trims words the branch regex tends to leak in ("policy", "query", …).
    """
    stopwords = {"policy", "policies", "query", "question", "request", "topic",
                 "category", "type", "issue", "intent", "the", "a", "an"}
    words = [w for w in label.strip().split() if w.lower() not in stopwords]
    if not words:
        words = label.strip().split()
    return " ".join(w.capitalize() for w in words).strip() or label.strip().title()


# ---------------------------------------------------------------------------
# Blueprint Generator (catalog-aware)
# ---------------------------------------------------------------------------

# Static structure-generation system prompt. Built once at import — it carries
# no per-request data (all request data flows through the user message), so
# rebuilding it per call would be wasted work.
_STRUCTURE_SYSTEM_PROMPT = (
            "You design workflow graphs. Output ONE JSON object. NO prose, NO markdown, "
            "NO code fences, NO ASCII diagrams. Start with `{` and end with `}`.\n\n"
            'Shape: {"name":str,"nodes":[...],"edges":[...]}\n\n'
            "Nodes:\n"
            '- Exactly 1 start: {"id":"start-1","type":"start","position":{"x":100,"y":300},"data":{"label":"Start"}}\n'
            '- 1-4 agents: {"id":"agent-N","type":"agent","position":{"x":X,"y":Y},'
            '"data":{"name":"Role","instructions":"...","skills":[],"tools":[]}}\n'
            '- Optional condition: {"id":"cond-1","type":"condition","position":{"x":X,"y":Y},"data":{"cases":[{"id":"case-approved","label":"Approved","logic":"AND","conditions":[{"id":"c1","field":"status","operator":"==","value":"approved","type":"string"}]},{"id":"case-rejected","label":"Rejected","logic":"AND","conditions":[{"id":"c2","field":"status","operator":"==","value":"rejected","type":"string"}]}]}}\n'
            '- Optional loop (for_each): {"id":"loop-1","type":"loop","position":{"x":X,"y":Y},"data":{"mode":"for_each","itemsExpression":"input.items","iteratorVar":"item","maxIterations":25}}\n'
            '- Optional loop (count): {"id":"loop-1","type":"loop","position":{"x":X,"y":Y},"data":{"mode":"count","count":3,"maxIterations":5}}\n'
            '- Optional loop (while): {"id":"loop-1","type":"loop","position":{"x":X,"y":Y},"data":{"mode":"while","maxIterations":5,"cases":[{"id":"continue","label":"Keep refining","logic":"AND","conditions":[{"id":"c1","field":"score","operator":"<","value":80,"type":"number"}]}]}}\n'
            '- Optional evaluation gate (judged retries): {"id":"gate-1","type":"evaluation_gate","position":{"x":X,"y":Y},"data":{"criteria":"Response is factually accurate, complete, and cites sources","threshold":0.85,"stop_policy":"pass_or_max","judgeModel":"","maxRetries":3}}\n'
            '- Optional subflow (delegate to an existing agent or workflow): {"id":"sub-1","type":"subflow","position":{"x":X,"y":Y},"data":{"kind":"agent","refId":"","refName":""}} — leave refId empty when the user has not named the delegate; the operator will pick it in the editor.\n'
            '- Exactly 1 end: {"id":"end-1","type":"end","position":{"x":LAST_AGENT_X+300,"y":300},"data":{"label":"End"}} — place it 300px right of the last agent\n\n'
            "AGENT INSTRUCTIONS (most important field): each agent's `data.instructions` MUST be a "
            "detailed, multi-section system prompt written as markdown with THESE exact sections, so the "
            "agent knows precisely what to do and what NOT to do:\n"
            "  ## Role — one line: who this agent is.\n"
            "  ## Objective — what it must accomplish within this workflow.\n"
            "  ## Process — a numbered, step-by-step of exactly how it does its job.\n"
            "  ## Do's — bullet list of expected/required behaviours.\n"
            "  ## Don'ts — bullet list of explicit prohibitions and guardrails.\n"
            "  ## Output — exactly what it should return so the NEXT node can consume it.\n"
            "  ## Escalation — when to defer to a human (HITL) or route to an alternate branch.\n"
            "Write real, specific content for the user's domain — never placeholders. Use \\n for "
            "newlines inside the JSON string. This field is the single biggest driver of quality.\n\n"
            'Edges: {"id":"e1","source":"<id>","target":"<id>","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}}\n'
            'For edges leaving a condition node, add "sourceHandle":"case-yes" or "case-no" (matching the case ids in data.cases).\n\n'
            "ADVANCED PATTERNS — add these when the requirements indicate them (see the `needs_*` flags "
            "in the requirements, or when the described flow clearly implies them). Still prefer the "
            "simplest structure that satisfies the request — do NOT over-engineer a trivial pipeline:\n\n"
            "1. CONDITION (branching): a condition node routes to different agents based on a decision. "
            'Each outgoing edge sets "sourceHandle" to one of the case ids. Every case AND the else '
            "handle MUST lead somewhere (another agent or the end node) — dangling handles crash the "
            "runtime.\n"
            "   CONDITION CASE SCHEMA — every case in data.cases MUST be fully configured or the runtime "
            "silently routes everything to `else`. Required keys per case: `id`, `label`, `logic` "
            '("AND" or "OR"), and `conditions` (a non-empty array). Each row in `conditions` needs '
            "`id`, `field`, `operator`, `value`, `type`. Legal `type` values: \"string\", \"number\", "
            '"boolean". Legal `operator` values by type — string: "==", "!=", "contains", "not_contains"; '
            'number: "==", "!=", "<", "<=", ">", ">="; boolean: "==", "!=". Prefer `field` names from '
            "the upstream agent's payload (common ones: intent, category, sentiment, priority, score, "
            "status, urgency, language, customer_tier). Cases MUST be mutually exclusive; each case `id` "
            "MUST match the `sourceHandle` on the edge leaving that case; the number of cases MUST equal "
            "the number of downstream branches (add an `else` handle for the catch-all, not a case).\n"
            "   ELSE HANDLE IS MANDATORY: for every condition node you MUST emit ONE additional edge "
            'whose `sourceHandle` is exactly "else" — this is the catch-all fallback taken when no '
            "case matches. Point it at the safest destination for the flow: an existing catch-all agent "
            "(e.g. \"General Handler\"), OR the end node when there is no such agent. Never omit this "
            "edge. Never assign `sourceHandle:\"else\"` to a case id — `else` is a separate, always-"
            "present handle rendered by the UI.\n\n"
            "2. PARALLEL (fan-out / fan-in): to run agents concurrently, give ONE node MORE THAN ONE outgoing edge "
            "(each to a different agent), then have all those agents edge into ONE shared downstream node (the fan-in). "
            "The engine runs the branches concurrently and joins at the fan-in node. Lay branches out on separate rows: "
            "y=150, y=300, y=450. Do NOT use a condition node for parallel — parallel is plain edges, no sourceHandle.\n\n"
            "3. LOOP (iterate until done): add a `loop` node. Its data carries `mode` (one of \"count\", \"while\", "
            '"for_each"), `maxIterations` (integer safety ceiling, e.g. 5), and mode-specific fields '
            "described below. Wire it with TWO outgoing edges by sourceHandle:\n"
            '   - "body": edge into the first agent of the loop body (the work that repeats).\n'
            '   - "exit": edge to the node that runs AFTER the loop finishes (usually the end node).\n'
            "   The LAST agent in the loop body MUST edge BACK to the loop node (source=last-body-agent, "
            "target=loop node id, no sourceHandle) so the engine can run the next iteration. Without this back-edge "
            "the loop runs once and stops.\n"
            "   LOOP NODE SCHEMA — pick the mode that fits the request:\n"
            '   - `for_each` — iterate a list. REQUIRED: `itemsExpression` (dotted path against the '
            'upstream payload, e.g. "input.issues" or "input.rows"; must start with "input."), and '
            "`iteratorVar` (default \"item\"). Set `maxIterations` to at least the expected list length "
            "(25 is a safe default cap for unknown-length lists).\n"
            "   - `count` — repeat a fixed number of times. REQUIRED: `count` (positive integer). Set "
            "`maxIterations` to the same value as `count` (or slightly higher) so the safety cap never "
            "clips the intended repeats.\n"
            "   - `while` — repeat until a predicate is false. REQUIRED: `cases` — a SINGLE-element "
            "array whose one case follows the CONDITION CASE SCHEMA above. The predicate should read "
            "from the previous iteration's self-reported payload (typical fields: `score` for quality "
            "loops, `done` boolean, `retries` counter). Use `maxIterations` 3-5 for quality loops — "
            "it is a SAFETY cap, not the target iteration count.\n\n"
            "4. HITL (human-in-the-loop approval): when the requirements set `needs_human_review` or the "
            "flow has a review / approval / sign-off / manual gate, add \"hitlMode\":\"after_response\" to "
            "the relevant agent's data object. This pauses the workflow after that agent responds and waits "
            "for a human to approve or reject before continuing. Use \"before_tool\" instead if the human "
            "must approve the agent's tool actions rather than its final message. Leave hitlMode off for "
            "agents that need no oversight.\n\n"
            "5. KNOWLEDGE BASE (RAG): when the requirements set `needs_knowledge_base` or an agent must "
            "reference documents / a knowledge base to do its job, add "
            '"knowledge":{"mode":"existing_kb","namespaces":[]} to that agent\'s data object. The mode '
            'MUST be exactly "existing_kb". Empty namespaces means the user will pick the specific '
            "knowledge base in the editor. Only add knowledge to agents that actually need reference "
            "material.\n\n"
            "6. EVALUATION GATE (judged retries): when the requirements describe an LLM-as-judge scoring "
            "the previous agent's output and retrying until it meets a quality bar, insert an "
            "`evaluation_gate` node AFTER the agent whose output is being judged. Required data fields: "
            "`criteria` (a plain-English rubric — what \"good\" looks like), `threshold` (a 0-1 float, "
            "0.85 is a good default), `stop_policy` (\"pass_or_max\" is standard — stop when passed OR "
            "when maxRetries is reached), `judgeModel` (leave blank — the platform fills in the "
            "configured default automatically), and `maxRetries` "
            "(2-4). Wire the gate with TWO outgoing edges by `sourceHandle`: \"pass\" → the node that "
            "runs when the judge accepts the output, \"retry\" → back to the agent being judged (so it "
            "tries again with the judge's feedback). Prefer `evaluation_gate` over a full `loop-while` "
            "when the retry decision is pure quality-based judging rather than a numeric predicate.\n\n"
            "7. SUBFLOW (delegation to existing asset): when the user explicitly asks to reuse an "
            "existing agent or workflow by name — e.g. \"call the Compliance Reviewer agent\", \"hand "
            "off to the Vendor Onboarding workflow\" — insert a `subflow` node with "
            '`data.kind:"agent"` or `data.kind:"workflow"` and empty `refId`/`refName` (the operator '
            "will pick the exact target in the editor). Do NOT invent an id. Use this instead of a "
            "regular agent node when reusing a published asset — you'd otherwise duplicate the "
            "instructions verbatim. Wire it like a plain agent: one edge in, one edge out.\n\n"
            "Layout: agents at y=300, x=400/700/1000 incrementing by 300. Parallel branches use y=150/300/450.\n\n"
            "Rules:\n"
            "- Every agent node MUST include a `type:\"agent\"` field and a `position:{x,y}`.\n"
            "- Leave `skills` and `tools` as empty arrays [] — they will be assigned in a later step.\n"
            "- The `instructions` field MUST follow the multi-section template above (Role / Objective / "
            "Process / Do's / Don'ts / Output / Escalation) — never a single vague sentence.\n"
            "- Exactly 1 start, 1 end, 1-4 agents. condition/loop/evaluation_gate/subflow nodes do NOT count toward the agent limit.\n"
            "- Every edge MUST have a unique `id`, plus `source` and `target` pointing to node ids that exist.\n"
            "- Prefer the simplest structure that satisfies the request. Add loops, parallel branches, "
            "conditions, HITL gates, or knowledge only when the `needs_*` flags or the described flow call "
            "for them.\n"
            "- BRANCHING IS MANDATORY when `needs_branching` is true OR the requirements contain multiple "
            "distinct specialist `agent_roles` (e.g. an Intent Classifier plus 2+ handler agents). In that "
            "case you MUST emit:\n"
            "    * ONE classifier agent (or use the first agent role) that sets a routing field like "
            "`intent` / `category` on its output payload,\n"
            "    * ONE condition node right after it with ONE case per specialist branch (case ids "
            'derived from the branch label, e.g. "case-medical", "case-general"), each case reading the '
            "classifier's routing field with an `==` string comparison,\n"
            "    * ONE agent node per branch, wired via the matching case handle,\n"
            "    * ONE `else` edge from the condition to a fallback destination (a fallback agent when "
            "one exists, otherwise the end node),\n"
            "    * Each branch agent edges to the end node so the graph converges.\n"
            "  Never collapse multiple specialist roles into a single generic agent when the "
            "requirements list them separately.\n\n"
            "EXAMPLE 1 (linear — 1 agent):\n"
            '{"name":"Document Review Pipeline","nodes":['
            '{"id":"start-1","type":"start","position":{"x":100,"y":300},"data":{"label":"Start"}},'
            '{"id":"agent-1","type":"agent","position":{"x":400,"y":300},"data":{"name":"Document Analyzer","instructions":"## Role\\nDocument analysis specialist.\\n\\n## Objective\\nExtract structured info from the input.\\n\\n## Process\\n1. Read the document.\\n2. Extract entities and topics.\\n\\n## Do\'s\\n- Ground extractions in the source.\\n\\n## Don\'ts\\n- Do not invent data.\\n\\n## Output\\nA JSON summary for the next node.\\n\\n## Escalation\\nFlag unreadable input.","skills":[],"tools":[]}},'
            '{"id":"end-1","type":"end","position":{"x":700,"y":300},"data":{"label":"End"}}'
            '],"edges":['
            '{"id":"e1","source":"start-1","target":"agent-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e2","source":"agent-1","target":"end-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}}'
            ']}\n\n'
            "EXAMPLE 2 (branching — 1 classifier + 3 specialists + condition; the shape you MUST "
            "emit when the request lists multiple `if X` branches or `needs_branching=true`. Write "
            "full, domain-specific `instructions` on the real request, not this short form):\n"
            '{"name":"HR Policy Q&A","nodes":['
            '{"id":"start-1","type":"start","position":{"x":100,"y":300},"data":{"label":"Start"}},'
            '{"id":"agent-1","type":"agent","position":{"x":400,"y":300},"data":{"name":"Intent Classifier","instructions":"## Role\\nHR intent classifier.\\n\\n## Objective\\nDecide which specialist should answer the user\'s question.\\n\\n## Process\\n1. Read the user question.\\n2. Set `intent` to one of medical, general, or none.\\n\\n## Output\\nJSON with keys `intent` and `question`.","skills":[],"tools":[]}},'
            '{"id":"cond-1","type":"condition","position":{"x":700,"y":300},"data":{"cases":['
            '{"id":"case-medical","label":"Medical","logic":"AND","conditions":[{"id":"c1","field":"intent","operator":"==","value":"medical","type":"string"}]},'
            '{"id":"case-general","label":"General","logic":"AND","conditions":[{"id":"c2","field":"intent","operator":"==","value":"general","type":"string"}]}'
            ']}},'
            '{"id":"agent-medical","type":"agent","position":{"x":1000,"y":150},"data":{"name":"Medical Insurance Agent","instructions":"## Role\\nMedical insurance policy specialist. …","skills":[],"tools":[]}},'
            '{"id":"agent-general","type":"agent","position":{"x":1000,"y":300},"data":{"name":"General Policy Agent","instructions":"## Role\\nGeneral HR policy specialist. …","skills":[],"tools":[]}},'
            '{"id":"agent-greet","type":"agent","position":{"x":1000,"y":450},"data":{"name":"Greeting Agent","instructions":"## Role\\nPolite deflection for off-topic questions. …","skills":[],"tools":[]}},'
            '{"id":"end-1","type":"end","position":{"x":1300,"y":300},"data":{"label":"End"}}'
            '],"edges":['
            '{"id":"e1","source":"start-1","target":"agent-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e2","source":"agent-1","target":"cond-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e3","source":"cond-1","sourceHandle":"case-medical","target":"agent-medical","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e4","source":"cond-1","sourceHandle":"case-general","target":"agent-general","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e5","source":"cond-1","sourceHandle":"else","target":"agent-greet","type":"default","style":{"stroke":"#94a3b8","strokeWidth":2,"strokeDasharray":"4 3"}},'
            '{"id":"e6","source":"agent-medical","target":"end-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e7","source":"agent-general","target":"end-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}},'
            '{"id":"e8","source":"agent-greet","target":"end-1","type":"default","style":{"stroke":"#6366f1","strokeWidth":2}}'
            ']}\n\n'
            "For loop+HITL: wire loop-1 with sourceHandle \"body\" (into the loop\'s first "
            "agent) and \"exit\" (to the node after the loop); the last body agent edges "
            "BACK to loop-1. Add \"hitlMode\":\"after_response\" on any agent needing human "
            "sign-off. Each real agent\'s `instructions` MUST still contain ALL seven "
            "sections (Role/Objective/Process/Do\'s/Don\'ts/Output/Escalation) with full, "
            "domain-specific content.\n\n"
            "Return the JSON object now."
)


class WorkflowStructureGenerator:
    """Agent 1 — designs the graph SHAPE only.

    Does NOT see the tools/skills catalog. Its job is to lay out nodes,
    write per-agent instructions/system prompts, and produce edges in
    the exact React Flow shape. Keeping the prompt small and catalog-free
    dramatically improves JSON-shape compliance.
    """

    async def generate(self, requirements: dict) -> dict:
        system = _STRUCTURE_SYSTEM_PROMPT

        safe_req = {
            "name": requirements.get("name", "Workflow"),
            "purpose": requirements.get("purpose", ""),
            "agent_roles": requirements.get("agent_roles", []),
            "data_in": requirements.get("data_in", ""),
            "data_out": requirements.get("data_out", ""),
            "needs_human_review": bool(requirements.get("needs_human_review")),
            "needs_knowledge_base": bool(requirements.get("needs_knowledge_base")),
            "needs_iteration": bool(requirements.get("needs_iteration")),
            "needs_branching": bool(requirements.get("needs_branching")),
            "additional_notes": requirements.get("additional_notes", ""),
        }
        req_text = json.dumps(safe_req, indent=2)
        user = (
            f"Design the workflow graph for these requirements:\n{req_text}\n\n"
            "Return ONLY the JSON object."
        )

        logger.info(f'[AGENT] WorkflowStructureGenerator: system={len(system)} chars, user={len(user)} chars')

        # NOTE: no assistant-prefill "{" turn. On the AiNxt gateway the prefill
        # trick makes the (non-streaming) endpoint return "Error generating
        # response" for this large prompt; the identical request WITHOUT the
        # prefill returns valid JSON. We ask for pure JSON in the prompt and let
        # ``_extract_json_block`` isolate it from any fences/prose.
        messages = [{"role": "user", "content": user}]

        async def _ask(extra_system: str = "") -> str:
            # max_tokens caps the OUTPUT graph JSON, not the input requirements.
            # Big/many-agent workflows emit a large graph (nodes + positions +
            # per-agent instructions + edges); 4k truncated them, so allow 8k.
            text = await asyncio.wait_for(
                call_factory_llm(
                    system + extra_system,
                    messages,
                    max_tokens=8192,
                    temperature=0.2,
                ),
                timeout=_BLUEPRINT_TIMEOUT_S,
            )
            return text.strip()

        # Extra instruction used when the model returned valid JSON that was
        # NOT blueprint-shaped (e.g. a single bare node, or the graph wrapped
        # under a top-level key). Reinforces the exact envelope we need.
        _shape_hint = (
            "\n\nIMPORTANT: Return a SINGLE JSON object with a top-level "
            '"nodes" array (and an "edges" array). Do NOT return a single '
            "node object, and do NOT wrap the graph under keys like "
            '"graph", "workflow", "blueprint", "data" or "result". '
            'The very first key of your JSON must be "nodes".'
        )

        try:
            raw = await _ask()
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                "The model is taking too long. Try again — it usually works on a second attempt."
            ) from exc

        logger.debug(f'[AGENT] WorkflowStructureGenerator: raw response ({len(raw)} chars):\n{raw[:2000]}')

        raise_if_gateway_rejection(raw, context="WorkflowStructureGenerator")

        data = None
        try:
            data = json.loads(_extract_json_block(raw))
        except json.JSONDecodeError:
            logger.debug('[AGENT] WorkflowStructureGenerator: parse failed first attempt')
            try:
                raw = await _ask()
                raise_if_gateway_rejection(raw, context="WorkflowStructureGenerator retry")
                data = json.loads(_extract_json_block(raw))
            except SecurityGatewayRejection:
                raise
            except Exception as retry_exc:
                logger.error(f'[AGENT] WorkflowStructureGenerator: retry failed: {retry_exc}')
                raise ValueError(
                    "The model couldn't generate valid JSON for this workflow. "
                    "Try rephrasing your request or simplifying it."
                ) from retry_exc

        # Shape check: the JSON parsed, but some models (notably smaller local
        # ones) occasionally emit a single bare node or a wrapped graph instead
        # of the {"nodes": [...], "edges": [...]} envelope. _repair_blueprint_shape
        # can unwrap many of these, but a genuinely single-node payload has no
        # graph to recover, so give the model one corrective retry with an
        # explicit envelope reminder before falling through to repair.
        if not _looks_like_blueprint(data):
            _shape_keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
            logger.info(
                f"[AGENT] WorkflowStructureGenerator: output not blueprint-shaped "
                f"(keys={_shape_keys}); retrying with envelope hint"
            )
            try:
                raw2 = await _ask(extra_system=_shape_hint)
                raise_if_gateway_rejection(raw2, context="WorkflowStructureGenerator shape-retry")
                data2 = json.loads(_extract_json_block(raw2))
                if _looks_like_blueprint(data2):
                    data = data2
                else:
                    logger.info(
                        "[AGENT] WorkflowStructureGenerator: shape-retry still not "
                        "blueprint-shaped; letting _repair_blueprint_shape handle it"
                    )
            except SecurityGatewayRejection:
                raise
            except Exception as shape_exc:
                # Non-fatal: keep the original payload and let repair try to
                # unwrap/wrap it deterministically downstream.
                logger.info(
                    f"[AGENT] WorkflowStructureGenerator: shape-retry failed "
                    f"({shape_exc}); using original payload"
                )

        return data


# ---------------------------------------------------------------------------
# Deterministic shape repair — fixes common LLM shape drift without an
# LLM round-trip. Handles things like missing `type`, missing `position`,
# wrapping in {"graph": ...}, simplified {id, label} nodes, etc.
# ---------------------------------------------------------------------------

# Legal operator/type vocabulary for condition rows. Kept in lock-step with
# ABStudio/frontend/src/constants/operators.js so anything we synthesise will
# render in the ConditionBuilder without a "not configured" pill.
_COND_STRING_OPERATORS = {"==", "!=", "contains", "not_contains"}
_COND_NUMBER_OPERATORS = {"==", "!=", "<", "<=", ">", ">="}
_COND_BOOLEAN_OPERATORS = {"==", "!="}
_COND_LEGAL_TYPES = {"string", "number", "boolean"}

# Common aliases the LLM emits for sourceHandle values that don't match a real
# case id. Mapped by lowercased alias → case index (0-based). "else" is passed
# through untouched by the caller since it's a legal handle.
_COND_HANDLE_ALIASES = {
    "yes": 0, "true": 0, "0": 0, "case-0": 0, "case-yes": 0, "case-true": 0,
    "no": 1, "false": 1, "1": 1, "case-1": 1, "case-no": 1, "case-false": 1,
}


def _slugify_case_id(label: str, fallback_index: int) -> str:
    """Return a case id derived from ``label`` (or ``case-<index>`` fallback)."""
    if not label:
        return f"case-{fallback_index}"
    slug = re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")
    return f"case-{slug}" if slug else f"case-{fallback_index}"


def _synthesize_default_row(label: str, row_index: int = 0) -> dict:
    """Build a routable default condition row keyed to a case label.

    Mirrors the frontend's ``newSimpleConditionRow`` (``intent contains
    <topic>``) so the resulting case renders in Simple mode and evaluates
    cleanly via ``services._build_single_expression``.
    """
    topic = re.sub(r"[^a-z0-9 ]+", " ", str(label or "").lower()).strip()
    topic = re.sub(r"\s+", " ", topic)
    if not topic:
        topic = "match"
    return {
        "id": f"c{row_index + 1}",
        "field": "intent",
        "operator": "contains",
        "value": topic,
        "type": "string",
    }


def _coerce_condition_row(row: dict, row_index: int) -> Optional[dict]:
    """Coerce one condition row to the canonical shape.

    Returns ``None`` when the row has no usable ``field`` — callers drop it so
    the case falls back to a synthesised default rather than shipping a
    row the evaluator turns into ``"False"``.
    """
    if not isinstance(row, dict):
        return None
    field = str(row.get("field") or "").strip()
    if not field:
        return None

    value_type = str(row.get("type") or "").strip().lower()
    if value_type not in _COND_LEGAL_TYPES:
        # Infer from the value when the LLM forgot to declare it.
        raw_value = row.get("value")
        if isinstance(raw_value, bool):
            value_type = "boolean"
        elif isinstance(raw_value, (int, float)):
            value_type = "number"
        else:
            value_type = "string"

    operator = str(row.get("operator") or "").strip()
    if value_type == "string" and operator not in _COND_STRING_OPERATORS:
        operator = "contains" if operator in {"~", "like", "matches"} else "=="
    elif value_type == "number" and operator not in _COND_NUMBER_OPERATORS:
        operator = "=="
    elif value_type == "boolean" and operator not in _COND_BOOLEAN_OPERATORS:
        operator = "=="

    raw_value = row.get("value")
    if value_type == "number":
        try:
            num = float(raw_value)
            coerced_value = int(num) if float(num).is_integer() else num
        except (TypeError, ValueError):
            coerced_value = 0
    elif value_type == "boolean":
        coerced_value = raw_value in (True, "true", "True", 1, "1")
    else:
        coerced_value = "" if raw_value is None else str(raw_value)

    return {
        "id": str(row.get("id") or f"c{row_index + 1}"),
        "field": field,
        "operator": operator,
        "value": coerced_value,
        "type": value_type,
    }


def _repair_condition_case(case: dict, case_index: int) -> dict:
    """Return a fully-configured case (id/label/logic/conditions[]).

    Legacy cases carrying a raw ``expression`` string are preserved — the
    backend evaluator (``services.build_expression_from_case``) still accepts
    that shape. Only the structured path needs synthesis.
    """
    if not isinstance(case, dict):
        case = {}

    label = str(case.get("label") or case.get("name") or f"Case {case_index + 1}").strip()
    case_id = str(case.get("id") or "").strip() or _slugify_case_id(label, case_index)

    logic = str(case.get("logic") or "AND").strip().upper()
    if logic not in {"AND", "OR"}:
        logic = "AND"

    # Legacy string-expression path — nothing to synthesise.
    legacy_expr = str(case.get("expression") or "").strip()

    raw_conditions = case.get("conditions")
    coerced: list[dict] = []
    if isinstance(raw_conditions, list):
        for i, row in enumerate(raw_conditions):
            fixed = _coerce_condition_row(row, i)
            if fixed:
                coerced.append(fixed)

    if not coerced and not legacy_expr:
        coerced = [_synthesize_default_row(label)]

    repaired: dict = {
        "id": case_id,
        "label": label,
        "logic": logic,
        "conditions": coerced,
    }
    if legacy_expr and not coerced:
        repaired["expression"] = legacy_expr
    return repaired


def _requirements_demand_branching(requirements: dict) -> bool:
    """True when the requirements clearly describe a routing workflow.

    Uses the same signals the LLM prompt is built from — ``needs_branching``
    plus a large-enough ``agent_roles`` list — so this predicate never
    contradicts the prompt guidance. False for single-agent or plainly
    linear workflows so we never over-rewrite them.
    """
    if not isinstance(requirements, dict):
        return False
    if not requirements.get("needs_branching"):
        return False
    roles = requirements.get("agent_roles") or []
    if not isinstance(roles, list):
        return False
    return _count_specialist_roles(requirements) >= 2


def _count_specialist_roles(requirements: dict) -> int:
    """Return how many branch-agent roles the requirements list.

    Skips roles whose title clearly identifies the classifier or a
    fallback, since those play distinct roles in the scaffolded graph
    (front-of-flow router + else-branch handler respectively).
    """
    roles = requirements.get("agent_roles") or []
    specialists = 0
    for r in roles:
        if not isinstance(r, dict):
            continue
        title = str(r.get("role") or "").lower()
        if any(k in title for k in ("classifier", "router", "intent", "triage")):
            continue
        if any(k in title for k in ("fallback", "greeting", "default handler", "catch")):
            continue
        specialists += 1
    return specialists


def _pick_classifier_role(requirements: dict) -> dict:
    """Return the role that should sit in front of the condition node."""
    for r in requirements.get("agent_roles") or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("role") or "").lower()
        if any(k in title for k in ("classifier", "router", "intent", "triage")):
            return r
    return {
        "role": "Intent Classifier",
        "job": "Classify the incoming request so the condition node can route it to the right specialist agent.",
    }


def _pick_fallback_role(requirements: dict) -> Optional[dict]:
    """Return the fallback / greeting role when the requirements list one."""
    for r in requirements.get("agent_roles") or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("role") or "").lower()
        if any(k in title for k in ("fallback", "greeting", "default handler", "catch")):
            return r
    return None


def _pick_specialist_roles(requirements: dict) -> list[dict]:
    """Return the specialist branch roles (skips classifier + fallback)."""
    out: list[dict] = []
    for r in requirements.get("agent_roles") or []:
        if not isinstance(r, dict):
            continue
        title = str(r.get("role") or "").lower()
        if any(k in title for k in ("classifier", "router", "intent", "triage")):
            continue
        if any(k in title for k in ("fallback", "greeting", "default handler", "catch")):
            continue
        out.append(r)
    return out


def _slugify_role(role: str) -> str:
    """``Medical Insurance Agent`` → ``medical-insurance``. Used for ids."""
    tail = re.sub(r"\b(agent|handler|assistant|bot)s?\b", "", role, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")
    return slug or "branch"


def _case_value_for_role(role: str) -> str:
    """Return the classifier-payload token the LLM should emit for this role.

    ``Medical Insurance Agent`` → ``medical``. Used both as the classifier's
    output intent and as the condition case's compared value so the graph
    is self-consistent — the case field / value match what the classifier
    is told to produce.

    Prefers the first "interesting" word (skipping articles / suffix words
    like ``agent``, ``handler``). Falls back to the first bare word when
    the role name is nothing but stop words — e.g. ``General HR Policy
    Agent`` yields ``general``, not an empty string.
    """
    tail = re.sub(r"\b(agent|handler|assistant|bot)s?\b", "", role, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[^A-Za-z0-9]+", tail) if w]
    if not words:
        return "match"
    stopwords = {"a", "an", "the", "for", "of", "and", "or"}
    for word in words:
        if word.lower() not in stopwords:
            return word.lower()
    return words[0].lower()


def _build_specialist_instructions(role: dict) -> str:
    """Return a full multi-section instructions block for a scaffolded agent.

    Mirrors the seven-section template the structure-generator prompt
    demands (Role / Objective / Process / Do's / Don'ts / Output /
    Escalation) so scaffolded nodes carry the same quality bar as
    LLM-generated ones.
    """
    role_name = role.get("role", "Agent")
    job = role.get("job", "Handle the assigned request.")
    return (
        f"## Role\n{role_name}.\n\n"
        f"## Objective\n{job}\n\n"
        "## Process\n"
        "1. Read the incoming request from the previous node.\n"
        "2. Apply your domain expertise to answer it accurately.\n"
        "3. Produce a concise, well-structured response.\n\n"
        "## Do's\n"
        "- Stay strictly within your area of responsibility.\n"
        "- Cite the source policy or document when the answer depends on one.\n"
        "- Be polite and precise.\n\n"
        "## Don'ts\n"
        "- Never guess when the policy is silent — say you don't know.\n"
        "- Never speculate about topics outside your role.\n\n"
        "## Output\n"
        "A clear text response for the user or the next node.\n\n"
        "## Escalation\n"
        "If the request is ambiguous or outside HR policy, defer to the fallback branch."
    )


def _scaffold_branching_from_requirements(
    requirements: dict, existing_nodes: list[dict],
) -> Optional[dict]:
    """Build a deterministic branching graph from ``agent_roles``.

    Called when the LLM under-produced. Produces the canonical shape:

        start → classifier → condition ──case-X→ specialistX → end
                                          │       ...
                                          └─ else → fallback → end

    Every case value matches a token the classifier is instructed to
    emit as ``intent`` on its output payload, so wiring stays consistent
    with the runtime engine. Returns ``None`` when the requirements
    don't have enough roles to warrant scaffolding (< 2 specialists).
    """
    specialists = _pick_specialist_roles(requirements)
    fallback = _pick_fallback_role(requirements)
    classifier = _pick_classifier_role(requirements)

    # Refuse to scaffold when there's genuinely nothing to route. The
    # minimum viable branch graph is (specialist + fallback) so the
    # condition has at least two downstream branches. When the requirements
    # give us only a classifier + fallback we bail; the linear graph the
    # LLM produced is fine as-is.
    if len(specialists) + (1 if fallback else 0) < 2:
        return None

    # Deduplicate case value tokens — two specialists with the same first
    # meaningful word would produce colliding case ids otherwise.
    used_values: set = set()
    specialist_meta: list[dict] = []
    for role in specialists:
        base_value = _case_value_for_role(role.get("role", "match"))
        value = base_value
        n = 2
        while value in used_values:
            value = f"{base_value}-{n}"
            n += 1
        used_values.add(value)
        slug = _slugify_role(role.get("role", "branch"))
        specialist_meta.append({
            "role": role,
            "value": value,
            "slug": slug,
            "agent_id": f"agent-{slug}",
            "case_id": f"case-{value}",
        })

    # Layout: classifier at (400, 300), condition at (700, 300),
    # specialists spread on the right, fallback below, end at the far right.
    nodes: list[dict] = [
        {"id": "start-1", "type": "start", "position": {"x": 100, "y": 300},
         "data": {"label": "Start"}},
        {"id": "agent-1", "type": "agent", "position": {"x": 400, "y": 300},
         "data": {
            "name": classifier.get("role") or "Intent Classifier",
            "instructions": _build_classifier_instructions(classifier, specialist_meta, bool(fallback)),
            "skills": [],
            "tools": [],
         }},
        {"id": "cond-1", "type": "condition", "position": {"x": 700, "y": 300},
         "data": {
            "cases": [
                {
                    "id": s["case_id"],
                    "label": s["role"].get("role") or s["value"].title(),
                    "logic": "AND",
                    "conditions": [{
                        "id": f"c-{s['value']}",
                        "field": "intent",
                        "operator": "==",
                        "value": s["value"],
                        "type": "string",
                    }],
                }
                for s in specialist_meta
            ],
         }},
    ]
    edges: list[dict] = [
        {"id": "e-start", "source": "start-1", "target": "agent-1",
         "type": "default", "style": {"stroke": "#6366f1", "strokeWidth": 2}},
        {"id": "e-classifier-cond", "source": "agent-1", "target": "cond-1",
         "type": "default", "style": {"stroke": "#6366f1", "strokeWidth": 2}},
    ]

    # Specialist agents laid out vertically to the right of the condition.
    total_slots = len(specialist_meta) + (1 if fallback else 0)
    row_step = 160
    top_y = 300 - ((total_slots - 1) * row_step) // 2
    for i, s in enumerate(specialist_meta):
        y = top_y + i * row_step
        nodes.append({
            "id": s["agent_id"],
            "type": "agent",
            "position": {"x": 1000, "y": y},
            "data": {
                "name": s["role"].get("role") or "Specialist",
                "instructions": _build_specialist_instructions(s["role"]),
                "skills": [],
                "tools": [],
            },
        })
        edges.append({
            "id": f"e-cond-{s['value']}",
            "source": "cond-1",
            "sourceHandle": s["case_id"],
            "target": s["agent_id"],
            "type": "default",
            "style": {"stroke": "#6366f1", "strokeWidth": 2},
        })

    fallback_target_id: str
    if fallback:
        y = top_y + len(specialist_meta) * row_step
        fb_id = "agent-fallback"
        nodes.append({
            "id": fb_id,
            "type": "agent",
            "position": {"x": 1000, "y": y},
            "data": {
                "name": fallback.get("role") or "Fallback Handler",
                "instructions": _build_specialist_instructions(fallback),
                "skills": [],
                "tools": [],
            },
        })
        edges.append({
            "id": "e-cond-else",
            "source": "cond-1",
            "sourceHandle": "else",
            "target": fb_id,
            "type": "default",
            "style": {"stroke": "#94a3b8", "strokeWidth": 2, "strokeDasharray": "4 3"},
        })
        fallback_target_id = fb_id
    else:
        # No dedicated fallback role — route else straight to end.
        fallback_target_id = "end-1"
        edges.append({
            "id": "e-cond-else",
            "source": "cond-1",
            "sourceHandle": "else",
            "target": "end-1",
            "type": "default",
            "style": {"stroke": "#94a3b8", "strokeWidth": 2, "strokeDasharray": "4 3"},
        })

    # End node — placed to the right of the rightmost column.
    nodes.append({
        "id": "end-1", "type": "end", "position": {"x": 1300, "y": 300},
        "data": {"label": "End"},
    })

    # Every branch agent (specialists + fallback) edges into end.
    branch_agent_ids = [s["agent_id"] for s in specialist_meta]
    if fallback:
        branch_agent_ids.append(fallback_target_id)
    for aid in branch_agent_ids:
        edges.append({
            "id": f"e-{aid}-end",
            "source": aid,
            "target": "end-1",
            "type": "default",
            "style": {"stroke": "#6366f1", "strokeWidth": 2},
        })

    return {
        "name": requirements.get("name") or "Branching Workflow",
        "nodes": nodes,
        "edges": edges,
    }


def _build_classifier_instructions(
    classifier: dict, specialist_meta: list[dict], has_fallback: bool,
) -> str:
    """Compose the classifier agent's instructions.

    The classifier's job in a scaffolded branching graph is to emit an
    ``intent`` field that matches EXACTLY one of the case-values wired
    into the condition node. We list those values verbatim so the runtime
    engine can compare them successfully.
    """
    intent_values = [s["value"] for s in specialist_meta]
    if has_fallback:
        intent_values.append("other")
    intent_list = ", ".join(f"`{v}`" for v in intent_values)
    role_name = classifier.get("role") or "Intent Classifier"
    return (
        f"## Role\n{role_name}.\n\n"
        "## Objective\n"
        "Read the user's question and decide which specialist branch should handle it.\n\n"
        "## Process\n"
        "1. Read the user's question carefully.\n"
        "2. Pick the single most-fitting intent from the allowed values.\n"
        "3. Return a JSON object with the classification.\n\n"
        "## Do's\n"
        f"- Set `intent` to EXACTLY one of: {intent_list}.\n"
        "- Include the original question verbatim in the output payload.\n\n"
        "## Don'ts\n"
        "- Never invent new intent values — pick from the allowed list only.\n"
        "- Never answer the user's question yourself; that is the specialist's job.\n\n"
        "## Output\n"
        '```json\n{ "intent": "<one of the allowed values>", "question": "<user text>" }\n```\n\n'
        "## Escalation\n"
        f"When the question doesn't fit any specialist, set `intent` to "
        f"`{'other' if has_fallback else intent_values[0]}` so the fallback branch handles it."
    )


def _wire_missing_else(node: dict, real_ids: list[str], edges: list[dict], node_id: str) -> None:
    """Ensure the condition node has an outgoing ``else`` edge.

    Called from ``_repair_condition_cases``. Skipped silently if an ``else``
    edge already exists. Otherwise picks a fallback target:

      * The nearest downstream ``end`` node when one is reachable — this is
        the safe choice for a catch-all branch, and matches the layout
        users expect (medical / general / <else → end>).
      * Otherwise the target of one of the existing case-edges — better than
        leaving the branch dangling.

    Does nothing when the node has no outgoing case-edges yet either (that
    means the whole condition is disconnected; ``_repair_blueprint_shape``
    handles that case elsewhere).
    """
    outgoing = [e for e in edges if e.get("source") == node_id]
    if any(e.get("sourceHandle") == "else" for e in outgoing):
        return

    # Look for an ``end``-typed node id in the graph via the edges list — we
    # don't have the full node list handed to us, but a common convention is
    # ``end-<n>``. Falling back to matching by prefix / substring keeps this
    # robust when the LLM changes the id shape.
    all_targets = {e.get("target") for e in edges} | {e.get("source") for e in edges}
    end_target = next(
        (t for t in all_targets if isinstance(t, str) and (t == "end-1" or t.startswith("end-"))),
        None,
    )

    case_edges = [e for e in outgoing if e.get("sourceHandle") in real_ids]
    fallback_target: Optional[str] = end_target
    if not fallback_target and case_edges:
        fallback_target = case_edges[-1].get("target")
    if not fallback_target:
        return  # nothing sensible to wire; leave for a higher-level repair

    edges.append({
        "id": f"e-{node_id}-else-{fallback_target}",
        "source": node_id,
        "sourceHandle": "else",
        "target": fallback_target,
        "type": "default",
        "style": {"stroke": "#94a3b8", "strokeWidth": 2, "strokeDasharray": "4 3"},
    })
    logger.info(
        f"[AGENT] _wire_missing_else: added else edge {node_id} → {fallback_target}"
    )


def _repair_condition_cases(node: dict, edges: list[dict]) -> None:
    """Guarantee every case on a condition node is fully configured.

    Mutates ``node["data"]["cases"]`` and re-maps any outgoing edge whose
    ``sourceHandle`` is an alias (``yes``/``no``/``true``/index) to the real
    case id. ``else`` handles are preserved untouched.
    """
    data = node.setdefault("data", {})
    cases = data.get("cases")

    if not isinstance(cases, list) or not cases:
        cases = [
            {"id": "case-yes", "label": "Yes"},
            {"id": "case-no", "label": "No"},
        ]
        logger.info(f"[AGENT] _repair_condition_cases: seeded default Yes/No cases on node '{node.get('id')}'")

    repaired_cases = [_repair_condition_case(c, i) for i, c in enumerate(cases)]

    # De-duplicate case ids in the (rare) event two labels slugified to the
    # same id — the UI keys cases by id so collisions swallow branches.
    seen_ids: set[str] = set()
    for i, c in enumerate(repaired_cases):
        cid = c["id"]
        if cid in seen_ids:
            cid = f"{cid}-{i}"
            c["id"] = cid
        seen_ids.add(cid)

    data["cases"] = repaired_cases

    # Re-map outgoing edge sourceHandle aliases → real case ids.
    real_ids = [c["id"] for c in repaired_cases]
    node_id = node.get("id")
    for edge in edges:
        if edge.get("source") != node_id:
            continue
        handle = edge.get("sourceHandle")
        if handle in (None, "", "else"):
            continue
        if handle in real_ids:
            continue
        idx = _COND_HANDLE_ALIASES.get(str(handle).lower())
        if idx is not None and idx < len(real_ids):
            logger.info(f"[AGENT] _repair_condition_cases: remapped edge {edge.get('id')} sourceHandle {handle!r} → {real_ids[idx]!r}")
            edge["sourceHandle"] = real_ids[idx]

    # Guarantee the ``else`` handle is wired. The ConditionNode UI always
    # renders an ``else`` output socket; without an outgoing edge that
    # branch dangles in the canvas and the engine has no route for the
    # catch-all path. The caller is expected to pass the full node list via
    # the module-level ``_wire_missing_else`` (invoked below) so we can
    # target an ``end`` node when one exists.
    _wire_missing_else(node, real_ids, edges, node_id)


# Legal loop modes mirror ABStudio/backend/app/engine/interface.py.
_LOOP_LEGAL_MODES = {"for_each", "while", "count"}


def _coerce_positive_int(raw: object, fallback: int, *, hard_max: Optional[int] = None) -> int:
    """Coerce ``raw`` to a positive int; fall back on garbage / ≤ 0.

    ``hard_max`` clamps runaway values (e.g. the LLM emits ``maxIterations:
    9999``) so the engine's own safety cap is never the deciding factor.
    """
    try:
        n = int(float(raw))
    except (TypeError, ValueError):
        n = fallback
    if n <= 0:
        n = fallback
    if hard_max is not None and n > hard_max:
        n = hard_max
    return n


def _repair_loop_node(node: dict, edges: list[dict]) -> None:
    """Normalise mode-specific fields on a loop node.

    Guarantees the runtime contract in ``native_engine._run_loop``: a legal
    ``mode``, a positive ``maxIterations`` safety cap, and the mode-specific
    fields (``itemsExpression`` / ``count`` / ``cases``) the engine reads.
    """
    data = node.setdefault("data", {})

    mode = str(data.get("mode") or "").strip().lower()
    if mode not in _LOOP_LEGAL_MODES:
        # Prefer the frontend store's default ("for_each") when the LLM emits
        # a bogus mode. Log so ops sees prompt drift.
        if mode:
            logger.info(f"[AGENT] _repair_loop_node: unknown mode {mode!r} on node '{node.get('id')}' — coerced to 'for_each'")
        mode = "for_each"
    data["mode"] = mode

    # Safety cap — always required. Clamp obvious runaways to 25 so the
    # engine's own cap doesn't kick in first.
    data["maxIterations"] = _coerce_positive_int(
        data.get("maxIterations"), fallback=5, hard_max=100,
    )

    if mode == "for_each":
        items_expr = str(data.get("itemsExpression") or "").strip()
        if not items_expr:
            items_expr = "input.items"
        elif not items_expr.startswith("input."):
            # The engine's dotted-path resolver accepts bare paths too, but the
            # UI's Loop config panel and the frontend factory both assume the
            # "input." prefix; normalise here so both agree.
            items_expr = f"input.{items_expr.lstrip('.')}"
        data["itemsExpression"] = items_expr
        data.setdefault("iteratorVar", "item")
    elif mode == "count":
        data["count"] = _coerce_positive_int(data.get("count"), fallback=3, hard_max=100)
    elif mode == "while":
        cases = data.get("cases")
        seeded_default = False
        if not isinstance(cases, list) or not cases:
            # No case at all: seed the standard quality-loop predicate directly
            # (score < 80) rather than a bare label, so the synthesised row is a
            # real numeric comparison instead of ``intent contains <label>``.
            cases = [{
                "id": "continue",
                "label": "Continue",
                "logic": "AND",
                "conditions": [{
                    "id": "c1", "field": "score", "operator": "<",
                    "value": 80, "type": "number",
                }],
            }]
            seeded_default = True
            logger.info(f"[AGENT] _repair_loop_node: seeded default 'score < 80' case on loop '{node.get('id')}'")
        # Loop persists exactly one case — trim anything extra to keep the
        # LoopWhileEditor's single-card assumption intact.
        cases = cases[:1]
        repaired = _repair_condition_case(cases[0], 0)
        # If the case still has no usable rule (label carried no signal and no
        # rows were seeded), fall back to the standard quality-loop predicate.
        if not repaired.get("conditions"):
            repaired["conditions"] = [{
                "id": "c1", "field": "score", "operator": "<",
                "value": 80, "type": "number",
            }]
            if not seeded_default:
                logger.info(f"[AGENT] _repair_loop_node: seeded default 'score < 80' rule on loop '{node.get('id')}'")
        data["cases"] = [repaired]

    # Sanity-check outgoing edges — the engine needs both a body and an exit
    # handle. Missing handles are a topology bug we can't fix from here
    # without inventing target nodes, so we log rather than mutate.
    node_id = node.get("id")
    handles = {e.get("sourceHandle") for e in edges if e.get("source") == node_id}
    if "body" not in handles:
        logger.info(f"[AGENT] _repair_loop_node: loop '{node_id}' missing 'body' outgoing edge — subgraph may not run")
    if "exit" not in handles:
        logger.info(f"[AGENT] _repair_loop_node: loop '{node_id}' missing 'exit' outgoing edge — post-loop flow may not run")
    # A back-edge into the loop (source != loop_id, target == loop_id, no
    # sourceHandle) is what re-triggers iteration. Log if absent.
    has_back_edge = any(
        e.get("target") == node_id and e.get("source") != node_id and not e.get("sourceHandle")
        for e in edges
    )
    if not has_back_edge:
        logger.info(f"[AGENT] _repair_loop_node: loop '{node_id}' missing body→loop back-edge — will run at most once per invocation")


def _repair_blueprint_shape(data: dict, requirements: dict) -> dict:
    """Coerce loose LLM output into the canonical {name, nodes, edges} shape.

    The model sometimes wraps everything in {"graph": {...}}, emits nodes as
    {id, label} instead of {id, type, position, data}, or omits required
    fields. This function repairs those without needing another LLM call.
    """
    if not isinstance(data, dict):
        raise ValueError("LLM output was not a JSON object")

    # Unwrap a single-level wrapper the model sometimes adds, e.g.
    # {"graph": {...}}, {"workflow": {...}}, {"blueprint": {...}},
    # {"data": {...}}, or {"result": {...}}. We accept any wrapper key whose
    # value is a dict that itself carries a recognizable node array.
    for _wk in _WRAPPER_KEYS:
        inner = data.get(_wk)
        if isinstance(inner, dict) and any(
            isinstance(inner.get(nk), list) and inner.get(nk) for nk in _NODE_KEYS
        ):
            data = {**inner, "name": data.get("name") or inner.get("name")}
            break

    # Handle the case where the model returned a *single node object* at the
    # top level instead of a blueprint, e.g. {"id", "type", "position", "data"}.
    # A blueprint never carries "position"/"data" at its own top level, so the
    # presence of those alongside an "id" (and no node array) is an unambiguous
    # signal that the payload is one bare node. Wrap it into a nodes list.
    _has_any_node_array = any(
        isinstance(data.get(nk), list) and data.get(nk) for nk in _NODE_KEYS
    )
    if not _has_any_node_array and "id" in data and (
        "position" in data or "type" in data or "data" in data
    ):
        logger.info(
            "[AGENT] _repair_blueprint_shape: payload was a single bare node "
            f"(id={data.get('id')!r}, type={data.get('type')!r}); wrapping into nodes[]"
        )
        _bare = {k: v for k, v in data.items() if k != "name"}
        data = {"name": data.get("name"), "nodes": [_bare], "edges": []}

    # Accept alternate node-array key names by normalizing onto "nodes".
    if not (isinstance(data.get("nodes"), list) and data.get("nodes")):
        for nk in _NODE_KEYS:
            if nk == "nodes":
                continue
            if isinstance(data.get(nk), list) and data.get(nk):
                data["nodes"] = data[nk]
                break

    nodes = data.get("nodes") or []
    edges = data.get("edges") or data.get("connections") or data.get("links") or []
    if not isinstance(nodes, list) or not nodes:
        raise ValueError(
            f"LLM output had no nodes (top-level keys: {sorted(data.keys()) if isinstance(data, dict) else type(data).__name__})"
        )

    # Count agents so we can place the end node past them when positions are missing
    agent_count = sum(1 for n in nodes if isinstance(n, dict) and (n.get("type") == "agent" or (
        not n.get("type") and "start" not in str(n.get("id", "")).lower()
        and "end" not in str(n.get("id", "")).lower()
        and "finish" not in str(n.get("id", "")).lower()
        and "cond" not in str(n.get("id", "")).lower()
        and "branch" not in str(n.get("id", "")).lower()
        and "decision" not in str(n.get("id", "")).lower()
        and "loop" not in str(n.get("id", "")).lower()
    )))
    end_x = max(1000, 400 + agent_count * 300)

    # Infer node types from id when missing (e.g. id "start" → type "start")
    repaired_nodes: list[dict] = []
    agent_index = 0
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or f"node-{i}")
        ntype = n.get("type")
        if not ntype:
            low = nid.lower()
            if "start" in low:
                ntype = "start"
            elif "end" in low or "finish" in low:
                ntype = "end"
            elif "cond" in low or "branch" in low or "decision" in low:
                ntype = "condition"
            elif "loop" in low or "iterate" in low:
                ntype = "loop"
            else:
                ntype = "agent"

        # Promote loose {id, label} → {id, type, data:{label/name}}
        d = n.get("data")
        if not isinstance(d, dict):
            d = {}
        if "label" in n and "label" not in d and "name" not in d:
            d["label" if ntype in ("start", "end") else "name"] = n["label"]
        if ntype == "agent":
            d.setdefault("name", nid.replace("_", " ").replace("-", " ").title())
            d.setdefault("instructions", "")
            d.setdefault("skills", [])
            d.setdefault("tools", [])

        # Fill missing positions deterministically
        pos = n.get("position")
        if not isinstance(pos, dict) or "x" not in pos or "y" not in pos:
            if ntype == "start":
                pos = {"x": 100, "y": 300}
            elif ntype == "end":
                pos = {"x": end_x, "y": 300}
            elif ntype == "agent":
                pos = {"x": 400 + agent_index * 300, "y": 300}
                agent_index += 1
            else:
                pos = {"x": 400 + i * 200, "y": 300}

        repaired_nodes.append({"id": nid, "type": ntype, "position": pos, "data": d})

    # Repair edges — ensure ids, default style, default type
    repaired_edges: list[dict] = []
    for i, e in enumerate(edges):
        if not isinstance(e, dict) or not e.get("source") or not e.get("target"):
            continue
        eid = str(e.get("id") or f"e{i+1}")
        repaired_edges.append({
            "id": eid,
            "source": str(e["source"]),
            "target": str(e["target"]),
            "type": e.get("type") or "default",
            "style": e.get("style") or {"stroke": "#6366f1", "strokeWidth": 2},
            **({"sourceHandle": e["sourceHandle"]} if e.get("sourceHandle") else {}),
        })

    types = {n["type"] for n in repaired_nodes}
    # A missing start node is recoverable in the same deterministic way as a
    # missing end node. The start carries no logic — it only marks the entry
    # point — so rather than failing the whole generation when the LLM omits
    # it, synthesise one and wire it into the graph's natural root (the first
    # node that is never an edge target). This keeps AI generation robust when
    # smaller/local models drop the boilerplate start node.
    if "start" not in types:
        start_id = "start-1"
        _existing_ids = {n["id"] for n in repaired_nodes}
        while start_id in _existing_ids:
            start_id += "-x"
        # Root = a non-end node that never appears as an edge target. Prefer the
        # first such node in document order; fall back to the first node.
        _targets = {e["target"] for e in repaired_edges}
        _roots = [
            n["id"] for n in repaired_nodes
            if n["type"] != "end" and n["id"] not in _targets
        ]
        if not _roots:
            _non_end = [n["id"] for n in repaired_nodes if n["type"] != "end"]
            _roots = _non_end[:1]
        repaired_nodes.insert(0, {
            "id": start_id,
            "type": "start",
            "position": {"x": 100, "y": 300},
            "data": {"label": "Start"},
        })
        for _tgt in _roots[:1]:
            repaired_edges.insert(0, {
                "id": f"e-start-{_tgt}",
                "source": start_id,
                "target": str(_tgt),
                "type": "default",
                "style": {"stroke": "#6366f1", "strokeWidth": 2},
            })
        types.add("start")
        logger.info(f"[AGENT] _repair_blueprint_shape: synthesised missing start node '{start_id}' wired to root {_roots[:1]}")

    # A missing end node is trivially recoverable — unlike a missing start (which
    # means we can't tell where the flow begins), an end node carries no logic.
    # The LLM occasionally omits it despite the prompt; rather than failing the
    # whole generation, synthesise one and wire every terminal node (any node
    # with no outgoing edge) to it. This is deterministic and service-agnostic.
    if "end" not in types:
        end_id = "end-1"
        # Guard against an id collision with an existing (mis-typed) node.
        _existing_ids = {n["id"] for n in repaired_nodes}
        while end_id in _existing_ids:
            end_id += "-x"
        repaired_nodes.append({
            "id": end_id,
            "type": "end",
            "position": {"x": end_x, "y": 300},
            "data": {"label": "End"},
        })
        # Terminal nodes = nodes that never appear as an edge source. Wire each
        # of them into the new end node so the graph actually reaches it. Skip
        # start/end/condition/loop as terminals — only agents should feed the end.
        _sources = {e["source"] for e in repaired_edges}
        _terminals = [
            n["id"] for n in repaired_nodes
            if n["type"] == "agent" and n["id"] not in _sources
        ]
        # If edge analysis found no clear terminal (e.g. the LLM emitted no edges
        # or a fully-connected mess), fall back to the last agent in document order.
        if not _terminals:
            _agents = [n["id"] for n in repaired_nodes if n["type"] == "agent"]
            _terminals = _agents[-1:] if _agents else []
        for _src in _terminals:
            repaired_edges.append({
                "id": f"e-end-{_src}",
                "source": str(_src),
                "target": end_id,
                "type": "default",
                "style": {"stroke": "#6366f1", "strokeWidth": 2},
            })
        logger.info(f"[AGENT] _repair_blueprint_shape: synthesised missing end node '{end_id}' and wired terminals {_terminals}")

    # Fully configure condition + loop nodes so the UI never shows "not
    # configured" and the runtime evaluator has real rules to run against.
    for n in repaired_nodes:
        ntype = n.get("type")
        if ntype == "condition":
            _repair_condition_cases(n, repaired_edges)
        elif ntype == "loop":
            _repair_loop_node(n, repaired_edges)

    # Surface prompt regressions where the requirements asked for branching /
    # iteration but the LLM produced no matching node. We only log — inserting
    # a node here would silently mutate topology.
    _types_present = {n.get("type") for n in repaired_nodes}
    if requirements.get("needs_branching") and "condition" not in _types_present:
        logger.info("[AGENT] _repair_blueprint_shape: requirements.needs_branching=True but no condition node was generated")
    if requirements.get("needs_iteration") and "loop" not in _types_present:
        logger.info("[AGENT] _repair_blueprint_shape: requirements.needs_iteration=True but no loop node was generated")

    # Name precedence: the enricher-synthesised ``requirements["name"]``
    # beats the LLM's own name when the LLM emitted a placeholder or a
    # raw-prompt echo (small local models regularly do the latter). A
    # genuinely descriptive LLM name wins otherwise.
    _llm_name = str(data.get("name") or "").strip()
    _req_name = str(requirements.get("name") or "").strip()
    if _llm_name and not _looks_like_raw_prompt(_llm_name):
        _final_name = _llm_name
    elif _req_name:
        _final_name = _req_name
    else:
        _final_name = "Generated Workflow"
    return {
        "name": _final_name,
        "nodes": repaired_nodes,
        "edges": repaired_edges,
    }


# ---------------------------------------------------------------------------
# Action-family correction — fixes the LLM picking write tools for a read agent
# ---------------------------------------------------------------------------

def _tool_by_name(name: str, index: dict) -> Optional[dict]:
    for entry in index.values():
        for t in entry.get("tools", []):
            if t.get("name") == name:
                return t
    return None


def _correct_action_mismatch(
    agent_name: str,
    agent_instructions: str,
    picked_tools: list[str],
    index: dict,
) -> list[str]:
    """Keep the LLM's tool picks unless they conflict with the agent's action.

    If the agent is clearly read-only (e.g. "Issue Fetcher") but every tool the
    LLM chose is a write tool (add/update/delete), the LLM guessed wrong — the
    screenshot bug where a Fetcher got ``jira_add_comment``. In that case we
    re-derive the tools for the same service(s) using the action-aware
    ``keyword_match_tools`` so it gets the read tool it needs. When the picks
    already include a family-appropriate tool, we leave them untouched.
    """
    if not picked_tools:
        return picked_tools

    agent_fams = _action_families(f"{agent_name} {agent_instructions}")
    if not agent_fams:
        return picked_tools  # no clear intent → trust the LLM

    # Which services did the LLM's tools come from? Re-derive within those only.
    picked_services: set[str] = set()
    any_family_ok = False
    for name in picked_tools:
        t = _tool_by_name(name, index)
        if not t:
            continue
        svc = (t.get("service") or "").strip().lower()
        if svc:
            picked_services.add(svc)
        tool_fams = _action_families(f"{name} {t.get('description') or ''}")
        if not tool_fams or (agent_fams & tool_fams):
            any_family_ok = True

    # At least one pick fits the agent's action → the LLM was reasonable.
    if any_family_ok:
        return picked_tools

    # Every pick conflicts (read agent got only write tools). Re-derive.
    svc_tools: list[dict] = []
    for svc in (picked_services or set()):
        svc_tools.extend(index.get(svc, {}).get("tools", []))
    corrected = keyword_match_tools(
        agent_name, agent_instructions, svc_tools, search_instructions=True,
    )
    if corrected:
        logger.info(f"[AGENT] WorkflowToolSkillAssigner: corrected action mismatch — agent '{agent_name}' had {picked_tools} (wrong family), replaced with {corrected}")
        return corrected
    return picked_tools


# ---------------------------------------------------------------------------
# Agent 2 — Tools & Skills Assigner
# ---------------------------------------------------------------------------

class WorkflowToolSkillAssigner:
    """Agent 2 — given a skeleton + the FULL unrestricted catalog, assigns
    tools and skills to each agent node by exact name.

    Output shape is intentionally tiny — just a mapping from agent id to
    its picks — so the model can focus on selection rather than reproducing
    the full graph.
    """

    async def assign(
        self,
        skeleton: dict,
        requirements: dict,
        available_skills: Optional[list] = None,
        available_tools: Optional[list] = None,
        service_index: Optional[dict] = None,
    ) -> dict:
        agent_nodes = [n for n in skeleton.get("nodes", []) if n.get("type") == "agent"]
        if not agent_nodes:
            return {}

        # Compact agent context — name + instructions only
        agents_brief = [
            {
                "id": n["id"],
                "name": (n.get("data") or {}).get("name", n["id"]),
                "instructions": (n.get("data") or {}).get("instructions", ""),
            }
            for n in agent_nodes
        ]

        index = service_index or build_service_index(available_tools or [])

        # Send the ENTIRE tool catalog — no service pre-filter, no count cap.
        # The old pre-filter + [:60] cap hid tools the model could have picked
        # (an agent whose job didn't obviously name a service got an incomplete
        # catalog → no tool attached). We keep the prompt lean instead by listing
        # NAMES ONLY (grouped by service): the names are descriptive slugs
        # (e.g. jira-get-issue, gitlab-create-mr) so the model has enough signal,
        # while a full 74-tool catalog stays small enough to avoid the gateway's
        # large-prompt slowdown.
        filtered_tools = list(available_tools or [])

        catalog_lines: list[str] = []
        if filtered_tools:
            svc_groups: dict[str, list[dict]] = {}
            for t in filtered_tools:
                svc = (t.get("service") or "general").strip() or "general"
                svc_groups.setdefault(svc, []).append(t)
            tool_lines = []
            for svc, tools in sorted(svc_groups.items()):
                tool_lines.append(f"  {svc}:")
                for t in tools:
                    tool_lines.append(f"    - {t['name']}")
            catalog_lines.append("TOOLS (only assign for external actions):\n" + "\n".join(tool_lines))
        if available_skills:
            skill_lines = []
            for s in available_skills:
                desc = " ".join(str(s.get("description") or "").split())[:80]
                skill_lines.append(f"  - {s['name']}" + (f" — {desc}" if desc else ""))
            catalog_lines.append("SKILLS:\n" + "\n".join(skill_lines))

        if not catalog_lines:
            return {}

        catalog_section = "\n".join(catalog_lines)

        # Build a concrete example using the actual agent IDs
        first_id = agents_brief[0]["id"]
        example = (
            f'{{"assignments":['
            f'{{"agent_id":"{first_id}","tools":["some-tool-name"],"skills":[]}},'
            f"..."
            f"]}}"
        )

        system = (
            "TASK: Pick tools/skills from the catalog below and assign them to agents.\n"
            "OUTPUT: exactly one JSON object, nothing else.\n\n"
            f"EXACT FORMAT:\n{example}\n\n"
            "CRITICAL — tool/skill names:\n"
            "- `tools` and `skills` MUST be arrays of PLAIN STRINGS, e.g. "
            '`"tools":["jira-get-issue"]`. NEVER objects like {\"tool\":...} or {\"name\":...}.\n'
            "- Every string MUST be copied EXACTLY from the catalog below (character-for-character).\n"
            "- You may ONLY use names that appear in the catalog. Do NOT invent, guess, "
            "abbreviate, or generalise names (e.g. no `webhook_listener`, `json_parser`, "
            "`llm_inference`, `logger`, `jira_api_client` — those are NOT in the catalog).\n"
            "- If the exact capability an agent needs is NOT in the catalog, output `[]` for "
            "that agent. An empty list is CORRECT and expected — it is far better than a made-up name.\n\n"
            "RULES:\n"
            "- Only assign tools to an agent whose job requires an EXTERNAL ACTION "
            "(fetch / create / send / query / update a real system). Reasoning-only "
            "agents (summarize, classify, draft, decide, analyze, parse) MUST get `tools:[]`.\n"
            "- 1-3 tools, 0-2 skills per agent\n"
            "- every agent must appear once\n"
            "- NO prose, NO markdown, NO code fences, NO wrapper objects, NO extra keys "
            "(no `reason`, `inputs`, `outputs`, `data_flow`)\n\n"
            f"{catalog_section}"
        )

        # Keep the user message minimal — just agent IDs and names, truncate
        # instructions to avoid the model echoing them back.
        agents_compact = [
            {"id": a["id"], "name": a["name"], "job": a["instructions"][:100]}
            for a in agents_brief
        ]
        user = (
            f"Purpose: {(requirements.get('purpose') or '')[:200]}\n"
            f"Agents: {json.dumps(agents_compact)}\n\n"
            "Return the assignments JSON now."
        )

        logger.info(f'[AGENT] WorkflowToolSkillAssigner: system={len(system)} chars, user={len(user)} chars, catalog={len(catalog_section)} chars, agents={len(agents_brief)}')

        # No assistant-prefill "{" turn — it makes the gateway return "Error
        # generating response" (see WorkflowStructureGenerator). Ask for pure
        # JSON and let ``_extract_json_block`` isolate it.
        messages = [{"role": "user", "content": user}]

        async def _ask() -> str:
            text = await asyncio.wait_for(
                call_factory_llm(
                    system,
                    messages,
                    # Assignment output is a compact per-agent JSON map; 1024 could
                    # truncate on large (15-20 agent) workflows, so give 2k headroom.
                    max_tokens=2048,
                    temperature=0.2,
                ),
                timeout=_BLUEPRINT_TIMEOUT_S,
            )
            return text.strip()

        try:
            raw = await _ask()
        except asyncio.TimeoutError:
            logger.warning('[AGENT] WorkflowToolSkillAssigner: timed out — skipping assignment')
            return {}

        logger.debug(f'[AGENT] WorkflowToolSkillAssigner: raw response ({len(raw)} chars):\n{raw[:3000]}')

        try:
            raise_if_gateway_rejection(raw, context="WorkflowToolSkillAssigner")
            extracted = _extract_json_block(raw)
            logger.debug(f'[AGENT] WorkflowToolSkillAssigner: extracted JSON block ({len(extracted)} chars):\n{extracted[:2000]}')
            data = json.loads(extracted)
        except SecurityGatewayRejection:
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] WorkflowToolSkillAssigner: parse failed ({exc}) — skipping assignment')
            return {}

        logger.debug(f'[AGENT] WorkflowToolSkillAssigner: parsed keys = {list(data.keys())}')

        # Normalize → {agent_id: {"tools": [...], "skills": [...]}}
        # The LLM uses various shapes: {"assignments":[...]}, {"agents":[...]},
        # {"workflow":{"agents":[...]}}, etc.  Dig through all known wrappers.
        raw_list = data.get("assignments") or data.get("agents") or data.get("results") or []

        # Unwrap {"workflow": {"agents": [...]}} or {"workflow": {"assignments": [...]}}
        if not raw_list and isinstance(data.get("workflow"), dict):
            inner = data["workflow"]
            raw_list = inner.get("assignments") or inner.get("agents") or []

        # Unwrap {"data": {"assignments": [...]}} or similar single-key wrappers
        if not raw_list and len(data) == 1:
            only_val = next(iter(data.values()))
            if isinstance(only_val, dict):
                raw_list = only_val.get("assignments") or only_val.get("agents") or []
            elif isinstance(only_val, list):
                raw_list = only_val

        logger.debug(f'[AGENT] WorkflowToolSkillAssigner: raw_list has {len(raw_list)} entries')

        # Build a set of valid catalog names for validation
        valid_tools = {t["name"] for t in (available_tools or [])}
        valid_skills = {s["name"] for s in (available_skills or [])}

        def _extract_names(items: list) -> list[str]:
            """Extract tool/skill names — handle plain strings AND the several
            dict shapes the LLM emits despite instructions:
            ``{"name": "x"}``, ``{"tool": "x"}``, ``{"skill": "x"}``,
            ``{"tool_name": "x"}``. Missing any of these silently drops real
            picks (the "raw_tools=[]" bug: model returned {"tool": ...})."""
            names = []
            for item in items:
                if isinstance(item, str):
                    names.append(item)
                elif isinstance(item, dict):
                    val = (
                        item.get("name")
                        or item.get("tool")
                        or item.get("skill")
                        or item.get("tool_name")
                        or item.get("skill_name")
                    )
                    if val:
                        names.append(str(val))
            return names

        result: dict[str, dict] = {}
        for a in raw_list:
            if not isinstance(a, dict):
                logger.debug(f'[AGENT] WorkflowToolSkillAssigner: skipping non-dict entry: {type(a)}')
                continue
            aid = a.get("agent_id") or a.get("id") or a.get("agent")
            if not aid:
                logger.debug(f'[AGENT] WorkflowToolSkillAssigner: entry has no agent id, keys={list(a.keys())}')
                continue

            raw_tools = _extract_names(a.get("tools") or [])
            raw_skills = _extract_names(a.get("skills") or [])

            logger.debug(f'[AGENT] WorkflowToolSkillAssigner: agent {aid} — raw_tools={raw_tools}, raw_skills={raw_skills}')

            # Only keep names that actually exist in the catalog
            tools = [t for t in raw_tools if t in valid_tools]
            skills = [s for s in raw_skills if s in valid_skills]

            if raw_tools and not tools:
                logger.debug(f'[AGENT] WorkflowToolSkillAssigner: agent {aid} — LLM suggested tools {raw_tools} but NONE matched catalog. Sample valid tools: {list(valid_tools)[:10]}')
            if raw_skills and not skills:
                logger.debug(f'[AGENT] WorkflowToolSkillAssigner: agent {aid} — LLM suggested skills {raw_skills} but NONE matched catalog. Sample valid skills: {list(valid_skills)[:10]}')

            result[str(aid)] = {"tools": tools, "skills": skills}

        # --- Per-agent gap resolution (confidence-gated) ---
        # The old logic only fell back when EVERY agent was empty, so a
        # workflow where one agent got tools but a "fetch the issue" agent got
        # none shipped broken (the screenshot bug). Instead, walk each agent
        # that the necessity heuristic says needs a tool but was left empty:
        #   - exactly ONE catalog service fits → assign it silently
        #   - 2+ services fit (ambiguous) → leave empty, record a question
        #   - 0 services fit (capability missing) → leave empty, record a gap
        # The recorded gaps are surfaced to the caller under "__gaps__" so it
        # can ask one consolidated question / report missing capabilities.
        gaps = self._resolve_gaps(
            agent_nodes, result, available_tools, available_skills, index,
        )

        logger.debug(f'[AGENT] WorkflowToolSkillAssigner: final result = {result} (gaps={gaps})')
        result["__gaps__"] = gaps
        return result

    @staticmethod
    def _resolve_gaps(
        agent_nodes: list[dict],
        result: dict,
        available_tools: Optional[list],
        available_skills: Optional[list],
        index: dict,
    ) -> dict:
        """Fill confident gaps in place; return {"ambiguous": [...], "missing": [...]}.

        ``result`` is mutated: agents whose service resolves unambiguously get
        their tools assigned here (silent). Ambiguous/missing agents are left
        empty and reported so the caller can ask or explain.
        """
        ambiguous: list[dict] = []
        missing: list[dict] = []

        for node in agent_nodes:
            d = node.get("data") or {}
            aid = str(node["id"])
            agent_name = d.get("name") or ""
            agent_instr = d.get("instructions") or ""

            picks = result.get(aid) or {"tools": [], "skills": []}
            # Backfill skills from keywords regardless (report/excel/pdf etc.) —
            # skills are additive and low-risk (name-only match, no service).
            if not picks.get("skills"):
                kw_skills = keyword_match_skills(agent_name, agent_instr, available_skills or [])
                if kw_skills:
                    picks["skills"] = kw_skills

            # If the LLM already picked tools, sanity-check them against the
            # agent's action family. A read-only agent ("Fetcher") that got only
            # write tools (add/update) is the LLM guessing wrong — re-derive from
            # the correct service so it gets the read tool it actually needs.
            if picks.get("tools"):
                picks["tools"] = _correct_action_mismatch(
                    agent_name, agent_instr, picks["tools"], index,
                )
                result[aid] = picks
                continue

            # Resolve which catalog service(s) THIS agent's own role references.
            # Assignment is strictly per-agent role — declared services (Q6) are a
            # WORKFLOW-level signal (the whole workflow uses jira+gitlab), NOT a
            # per-agent instruction. Forcing every declared service onto every
            # agent produced garbage (e.g. a Report Generator getting jira +
            # gitlab tools). So we do NOT union ``required_services`` here.
            services = resolve_services_for_agent(agent_name, agent_instr, index)

            # Skip when the agent needs no external action AND references no
            # catalog service. (The improved _EXTERNAL_ACTION_VERBS now covers
            # read/analysis verbs, and a concrete service reference is a valid
            # second signal — so a "Jira Analyzer: analyse issue" is no longer
            # wrongly skipped.)
            if not agent_needs_tool(agent_name, agent_instr) and not services:
                result[aid] = picks
                continue

            if len(services) == 1:
                svc = services[0]
                # Scope the match to just this service's tools so the picks match
                # the agent's actual role. If keyword matching finds nothing we
                # leave it empty and report a gap — we do NOT attach an arbitrary
                # "first tool", which previously produced wrong picks (e.g.
                # jira_list_issues for an agent that needed jira_get_issue).
                tools = keyword_match_tools(
                    agent_name, agent_instr, index.get(svc, {}).get("tools", []),
                    search_instructions=True,
                )
                if tools:
                    picks["tools"] = tools
                    logger.info(f'[AGENT] WorkflowToolSkillAssigner: gap filled — agent {aid} → {tools} (service={svc})')
                else:
                    missing.append({"agent_id": aid, "agent_name": agent_name})
            elif len(services) >= 2:
                # The agent's OWN role genuinely references 2+ services — this is
                # real ambiguity. Record it so the caller asks one consolidated
                # question rather than guessing.
                ambiguous.append({
                    "agent_id": aid,
                    "agent_name": agent_name,
                    "services": services[:4],
                })
            else:
                missing.append({"agent_id": aid, "agent_name": agent_name})

            result[aid] = picks

        return {"ambiguous": ambiguous, "missing": missing}


# ---------------------------------------------------------------------------
# Public Blueprint Generator — orchestrates Structure → Repair → Assign
# ---------------------------------------------------------------------------

class WorkflowBlueprintGenerator:
    """Public entrypoint. Runs:

    1. ``WorkflowStructureGenerator`` — LLM call #1: shape + instructions.
    2. ``_repair_blueprint_shape`` — deterministic Python fixup.
    3. ``WorkflowToolSkillAssigner`` — LLM call #2: pick from the full catalog.
    4. Server-side fill of runtime config (provider, model, baseUrl, etc.).

    The public ``generate(requirements, available_skills, available_tools)``
    signature is unchanged so ``factories.py`` doesn't need to change.
    """

    async def generate(
        self,
        requirements: dict,
        available_skills: Optional[list] = None,
        available_tools: Optional[list] = None,
        service_index: Optional[dict] = None,
        available_models: Optional[list] = None,
    ) -> dict:
        # --- Step 1: structure ---
        raw_blueprint = await WorkflowStructureGenerator().generate(requirements)

        # Diagnostic: surface the top-level shape so a "no nodes" failure is
        # traceable to the exact key the model used, without dumping the whole
        # (potentially large) payload.
        try:
            if isinstance(raw_blueprint, dict):
                _keys = list(raw_blueprint.keys())
                _node_len = len(raw_blueprint.get("nodes") or []) if isinstance(raw_blueprint.get("nodes"), list) else "n/a"
                logger.info(f"[AGENT] raw_blueprint keys={_keys} top-level nodes={_node_len}")
            else:
                logger.info(f"[AGENT] raw_blueprint non-dict type={type(raw_blueprint).__name__}")
        except Exception:
            pass

        # --- Step 2: deterministic repair ---
        repaired = _repair_blueprint_shape(raw_blueprint, requirements)
        nodes = repaired["nodes"]
        edges = repaired["edges"]
        name = repaired["name"]

        # --- Step 2b: scaffold branching graph from requirements ---
        # Small local LLMs sometimes collapse a clearly-branching request
        # into a single generic agent, ignoring the multi-role
        # ``agent_roles``. When the requirements demand branching AND the
        # LLM produced fewer agents than roles / no condition node, we
        # rebuild the graph deterministically from the roles list so the
        # user's stated intent is honoured. See
        # ``_scaffold_branching_from_requirements`` for the shape.
        if _requirements_demand_branching(requirements):
            agent_count = sum(1 for n in nodes if n.get("type") == "agent")
            has_condition = any(n.get("type") == "condition" for n in nodes)
            expected_specialists = _count_specialist_roles(requirements)
            needs_scaffold = (
                agent_count < expected_specialists + 1  # +1 for the classifier
                or not has_condition
            )
            if needs_scaffold:
                logger.info(
                    "[AGENT] scaffold_branching: LLM under-produced "
                    f"(agents={agent_count}, has_condition={has_condition}, "
                    f"expected_specialists={expected_specialists}) — "
                    "rebuilding graph deterministically from requirements"
                )
                scaffolded = _scaffold_branching_from_requirements(
                    requirements, nodes,
                )
                if scaffolded:
                    nodes = scaffolded["nodes"]
                    edges = scaffolded["edges"]
                    if not name or name in ("Generated Workflow",):
                        name = scaffolded.get("name") or name

        # Gaps the assigner couldn't resolve confidently — surfaced to the
        # caller so it can ask one consolidated question / report missing
        # capabilities. Shape: {"ambiguous": [...], "missing": [...]}.
        gaps: dict = {"ambiguous": [], "missing": []}

        # --- Step 3: tool/skill assignment (best-effort; non-fatal on failure) ---
        if available_tools or available_skills:
            try:
                assignments = await WorkflowToolSkillAssigner().assign(
                    skeleton={"nodes": nodes, "edges": edges},
                    requirements=requirements,
                    available_skills=available_skills,
                    available_tools=available_tools,
                    service_index=service_index,
                )
            except SecurityGatewayRejection:
                raise
            except Exception as exc:
                logger.warning(f'[AGENT] WorkflowToolSkillAssigner failed — continuing with empty assignments: {exc}')
                assignments = {}

            gaps = assignments.pop("__gaps__", None) or gaps
            logger.debug(f'[AGENT] WorkflowBlueprintGenerator: assignments returned = { {k: v for k, v in assignments.items()}}')
            for n in nodes:
                if n.get("type") != "agent":
                    continue
                aid = n.get("id")
                picks = assignments.get(aid) or {}
                d = n.get("data") or {}
                logger.debug(f'[AGENT] WorkflowBlueprintGenerator: applying to node {aid} — picks={picks}')
                if picks.get("tools"):
                    d["tools"] = list(picks["tools"])
                if picks.get("skills"):
                    d["skills"] = list(picks["skills"])
                n["data"] = d

        # --- Step 4: server-side runtime config fill ---
        # Route base_url through the LLM_PROXY-aware helper so SIT (and any
        # env where ``LLM_PROXY_URL`` is set) gets ``${LLM_PROXY_URL}/v1``
        # baked into the workflow blueprint. The previous direct env reads
        # silently produced blank baseUrl in SIT (neither OPENAI_COMPATIBLE_BASE_URL
        # nor LOCAL_LLM_BASE_URL are set there — only LLM_PROXY_URL is),
        # which then fell through to the localhost default at run time and
        # surfaced as "LLM unreachable" during workflow execution.
        from app.core.config import (
            openai_compatible_base_url as _resolved_base_url,
            factory_agent_model,
        )
        # Model precedence:
        #   1. A model the user explicitly named in chat (normalised to a
        #      canonical id) wins and is stamped on every agent — that's what
        #      "use haiku" / "switch to opus" means at the workflow level.
        #   2. Otherwise, an LLM classifier (with a keyword-heuristic backup)
        #      picks a tier per agent based on the agent's name + instructions
        #      and resolves that tier to a real model from ``available_models``
        #      (the exact list ``/llm/models`` exposes and the CLI accepts).
        #   3. Only if the catalogue lookup didn't happen (older callers that
        #      don't pass ``available_models``) do we fall back to the env
        #      default so behaviour stays backward-compatible.
        forced_model = normalize_model_pref(requirements.get("preferred_model"))
        agent_nodes_for_model = [n for n in nodes if n.get("type") == "agent"]
        per_agent_models: dict = {}
        try:
            per_agent_models = await _assign_agent_models(
                agent_nodes_for_model,
                available_models,
                forced_model=forced_model,
            )
        except SecurityGatewayRejection:
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] per-agent model assignment failed: {exc}')

        env_default_model = factory_agent_model()
        base_url = _resolved_base_url()
        for n in nodes:
            if n.get("type") != "agent":
                continue
            d = n.get("data") or {}
            picked = per_agent_models.get(n.get("id"))
            # Precedence for THIS node: any modelName already on the node
            # (from a chat patch or a prior save) wins; then the per-agent
            # pick from step 2 above; then the env default; then a hard-
            # coded fallback so we never emit an empty model.
            if not d.get("modelName"):
                d["modelName"] = picked or forced_model or env_default_model
            d.setdefault("provider", "custom")
            d.setdefault("apiKey", "")
            # Tuned for instruction-following agents: lower temperature/top_p
            # for more deterministic role adherence, larger output ceiling
            # for the richer multi-section responses these agents produce.
            d.setdefault("temperature", 0.3)
            d.setdefault("maxTokens", 4096)
            d.setdefault("topP", 0.9)
            d.setdefault("baseUrl", base_url)
            d.setdefault("skills", [])
            d.setdefault("tools", [])
            n["data"] = d

        # Evaluation-gate judgeModel fill-in — mirrors the agent modelName fill-in
        # just above. The structure-generation prompt deliberately leaves
        # `judgeModel` blank (see _STRUCTURE_SYSTEM_PROMPT) rather than splicing a
        # live model id into a module-level constant string, so this is the one
        # place a blank judgeModel gets a real value — using the same
        # registry-aware factory_agent_model() resolution as everything else here.
        for n in nodes:
            if n.get("type") != "evaluation_gate":
                continue
            d = n.get("data") or {}
            if not d.get("judgeModel"):
                d["judgeModel"] = env_default_model
            n["data"] = d

        # --- Step 5: guarantee valid, unique agent + workflow names ---
        # The generation LLM can emit names the platform rejects (emoji, leading
        # digits, disallowed punctuation) which would 400 on Apply. Sanitise
        # every name to the frontend's validateEntityName rules, then de-dupe
        # agent names so two "Reviewer" agents don't collide.
        agent_nodes = [n for n in nodes if n.get("type") == "agent"]
        sanitized = [
            sanitize_entity_name((n.get("data") or {}).get("name"), fallback="Agent")
            for n in agent_nodes
        ]
        for n, clean in zip(agent_nodes, dedupe_names(sanitized)):
            n["data"]["name"] = clean
        name = sanitize_entity_name(name, fallback="Workflow")

        return {
            "name": name,
            "graph_data": {
                "nodes": nodes,
                "edges": edges,
            },
            # Non-graph metadata the confirm step uses to warn about agents that
            # can't run yet (missing tools) — not persisted with the workflow.
            "tool_gaps": gaps,
        }


# ---------------------------------------------------------------------------
# Skill Matcher — resolves skill names on agent nodes against the catalog
# ---------------------------------------------------------------------------

class WorkflowSkillMatcher:
    """Catalog name-matcher used for both skills and tools.

    Delegates to shared ``score_catalog_match`` and ``semantic_catalog_match``
    from ``factory_utils`` — the same scoring logic used by the agent
    factory's ``ToolSkillMatcher``.
    """

    def match(
        self,
        requested: list[str],
        catalog: list[dict],
    ) -> tuple[list[str], list[str]]:
        """Return (resolved_names, gap_names) using multi-level string scoring."""
        resolved, gaps = [], []
        for req in requested:
            best_score, best_name = 0.0, None
            for item in catalog:
                s = score_catalog_match(req, item)
                if s > best_score:
                    best_score, best_name = s, item["name"]
                    if s == 1.0:
                        break
            if best_name and best_score >= MATCH_THRESHOLD:
                resolved.append(best_name)
            else:
                gaps.append(req)
        return resolved, gaps

    async def semantic_match(
        self,
        unmatched: list[str],
        catalog: list[dict],
    ) -> list[str]:
        """LLM semantic fallback — returns resolved catalog names."""
        matches = await semantic_catalog_match(unmatched, catalog)
        return [m["catalog_name"] for m in matches if m.get("catalog_name")]


# ---------------------------------------------------------------------------
# Skill Injector — generates missing skills and writes them into node data
# ---------------------------------------------------------------------------

async def inject_skills_into_nodes(
    nodes: list[dict],
    catalog_skills: list[dict],
    yield_progress=None,
) -> list[dict]:
    """Resolve each agent node's ``skills[]`` against the catalog.

    Only attaches skills that already exist in the catalog.  Unmatched
    skill names are dropped — the agent's ``instructions`` field carries
    the real behaviour, skills are just optional augmentations.
    """
    matcher = WorkflowSkillMatcher()

    updated: list[dict] = []
    for node in nodes:
        if node.get("type") != "agent":
            updated.append(node)
            continue
        data = dict(node.get("data") or {})
        raw_skills = data.get("skills") or []
        requested = [
            (s["name"] if isinstance(s, dict) else s)
            for s in raw_skills if s
        ]
        if not requested:
            updated.append(node)
            continue

        resolved, gaps = matcher.match(requested, catalog_skills)

        if resolved and yield_progress:
            try:
                for name in resolved:
                    await yield_progress(f"  {data.get('name', 'Agent')} — attached skill: {name}")
            except Exception:
                pass
        if gaps:
            logger.info(f"[AGENT] inject_skills_into_nodes: {data.get('name', 'agent')} requested unknown skills {gaps} — dropped (catalog-only mode)")
            if yield_progress:
                try:
                    await yield_progress(
                        f"  {data.get('name', 'Agent')} — skipped {len(gaps)} unknown skill(s), using instructions instead"
                    )
                except Exception:
                    pass

        data["skills"] = [{"name": s} for s in resolved]
        updated.append({**node, "data": data})

    return updated


# ---------------------------------------------------------------------------
# Tool Injector — catalog-match only; no dynamic generation
# ---------------------------------------------------------------------------

def inject_tools_into_nodes(
    nodes: list[dict],
    catalog_tools: list[dict],
) -> list[dict]:
    """Resolve each agent node's ``tools[]`` against the tools catalog.

    Unlike skills, tools are NEVER auto-generated. Real integrations
    (GitHub, Jira, etc) require credentials, auth flows, and tested
    SDK code that an LLM can't safely synthesize on the fly. Any
    tool name the LLM put on the node that doesn't match an existing
    catalog entry is dropped and logged — the user can wire those
    in manually from the editor.
    """
    matcher = WorkflowSkillMatcher()
    updated: list[dict] = []
    for node in nodes:
        if node.get("type") != "agent":
            updated.append(node)
            continue
        data = dict(node.get("data") or {})
        raw_tools = data.get("tools") or []
        requested = [
            (t["name"] if isinstance(t, dict) else t)
            for t in raw_tools if t
        ]
        if not requested:
            updated.append(node)
            continue
        resolved, gaps = matcher.match(requested, catalog_tools)
        if gaps:
            logger.info(f"[AGENT] inject_tools_into_nodes: {data.get('name', 'agent')} requested unknown tools {gaps} — dropped (no dynamic generation)")
        data["tools"] = [{"name": t} for t in resolved]
        updated.append({**node, "data": data})
    return updated


# ---------------------------------------------------------------------------
# WorkflowFieldPatcher — targeted, non-regenerating edits on an assembled
# workflow. Mirrors ``AgentFieldPatcher`` in agent_factory/pipeline.py but
# extends the surface area to cover graph-shape edits (add / remove / rewire
# nodes) and per-node-type schemas.
# ---------------------------------------------------------------------------

# Node types whose ``data.*`` fields the chat is allowed to edit. Anything
# NOT listed here is silently dropped from the LLM output — a defence against
# the model inventing surface area we haven't validated.
_NODE_TYPE_ALLOWED_FIELDS: dict = {
    "agent": frozenset({
        "name", "instructions", "modelName", "temperature", "maxTokens", "topP",
        "tools", "skills", "enable_subagents", "disable_subagents",
        "hitlMode", "knowledge",
    }),
    "condition": frozenset({"cases"}),
    "loop": frozenset({
        "mode", "itemsExpression", "iteratorVar", "count", "maxIterations",
        "cases", "evaluator_enabled", "judgeModel", "stop_policy", "threshold",
        "criteria",
    }),
    "evaluation_gate": frozenset({
        "criteria", "threshold", "stop_policy", "judgeModel", "maxRetries",
    }),
    "subflow": frozenset({"kind", "refId", "refName"}),
}

_NEW_NODE_TYPES: frozenset = frozenset(_NODE_TYPE_ALLOWED_FIELDS.keys()) | {"end"}
_HITL_VALUES = {"off", "before_tool", "after_response", "both"}
_KB_MODES = {"none", "existing_kb", "add_kb"}
_LOOP_MODES = {"for_each", "count", "while"}
_SUBFLOW_KINDS = {"agent", "workflow"}
_STOP_POLICIES = {"pass_or_max", "pass_only", "max_only"}


# --- Regex fast-path patterns for common numeric / structural edits --------

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
_RE_LOOP_MAX = re.compile(
    r"\bloop\b[^\.]{0,30}\bmax(?:imum)?(?:[_\s-]*iterations?|[_\s-]*iters?)?\b"
    r"[^0-9]{0,15}([0-9]+)",
    re.IGNORECASE,
)
_RE_GATE_THRESHOLD = re.compile(
    r"\b(?:threshold|score|quality)\b[^0-9]{0,20}([0-9]*\.?[0-9]+)\s*(%)?",
    re.IGNORECASE,
)
# "agent 2 ...", "second agent ...", "the reviewer agent ...".
_RE_AGENT_ORDINAL = re.compile(
    r"\b(?:agent[_\s-]*|the[_\s]+)?(?P<ord>1st|2nd|3rd|4th|first|second|third|fourth|last)"
    r"(?:[_\s]+agent)?\b",
    re.IGNORECASE,
)
_RE_AGENT_NUMERIC = re.compile(r"\bagent[_\s-]*([0-9]+)\b", re.IGNORECASE)

_ORDINAL_INDEX = {
    "first": 0, "1st": 0,
    "second": 1, "2nd": 1,
    "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3,
}


def _fresh_node_id(prefix: str, existing_ids: set) -> str:
    """Return the next unused ``<prefix>-<N>`` id.

    Chat-added nodes need a new id that neither collides with the LLM's
    generated ids nor with an id the user just deleted-and-re-added. We
    pick the lowest positive integer suffix that isn't already taken.
    """
    n = 1
    while f"{prefix}-{n}" in existing_ids:
        n += 1
    return f"{prefix}-{n}"


def _find_node(nodes: list[dict], selector: dict) -> Optional[dict]:
    """Look a node up by ``node_id`` / ``node_name`` / ``node_index``.

    All three keys are accepted so the LLM can refer to nodes by whichever
    the user used ("the reviewer agent" → node_name; "agent 2" → node_index;
    a copy-pasted id → node_id). Returns None when nothing matches.
    """
    if not isinstance(selector, dict):
        return None
    node_id = selector.get("node_id") or selector.get("id")
    if node_id:
        for n in nodes:
            if n.get("id") == node_id:
                return n
    name = selector.get("node_name") or selector.get("name")
    if name:
        target = str(name).strip().lower()
        for n in nodes:
            if str((n.get("data") or {}).get("name") or "").strip().lower() == target:
                return n
    idx = selector.get("node_index")
    if isinstance(idx, int):
        agents = [n for n in nodes if n.get("type") == "agent"]
        if 0 <= idx < len(agents):
            return agents[idx]
    return None


class WorkflowFieldPatcher:
    """Apply a targeted patch to an assembled workflow based on a chat message.

    Used by the confirm-stage of ``/workflow-factory/chat`` so a request like
    "make agent 2 use claude-haiku" only touches that node's ``modelName``
    field instead of regenerating the whole graph. Also supports simple
    graph-shape edits: adding, removing, and rewiring nodes.
    """

    async def patch(self, workflow_data: dict, user_message: str) -> dict:
        """Return a patch dict describing every requested edit.

        Shape::

            {
                "name": str,                             # optional workflow rename
                "node_patches": [                        # per-node data edits
                    {"node_id": "...", "data_patch": {...}},
                ],
                "add_nodes": [                            # new nodes to splice in
                    {"type": "condition", "after": "agent-1", "data": {...}},
                ],
                "remove_node_ids": ["gate-2"],
                "rewire_edges": [
                    {"source": "cond-1", "source_handle": "case-reject",
                     "target": "end-1"},
                ],
            }

        Never raises — a broken LLM response yields an empty patch and the
        caller renders a "couldn't tell what to change" nudge.
        """
        regex_patch = self._regex_patch(workflow_data, user_message)

        snapshot = _summarise_workflow_for_prompt(workflow_data)
        system = (
            "You are editing a workflow graph that is already assembled.\n"
            "The user will describe a change. Return ONLY the fields they asked to modify.\n\n"
            "Allowed edits (return a subset — omit anything the user did not ask for):\n"
            '  - "name": string  → rename the workflow.\n'
            '  - "node_patches": [{"node_id"|"node_name"|"node_index": ..., "data_patch": {...}}] '
            '→ per-node field edits. Only include fields the user explicitly changed.\n'
            '  - "add_nodes": [{"type": "agent|condition|loop|evaluation_gate|subflow|end", '
            '"after": "<existing node id or name>", "data": {...}}] → splice a new node after the '
            "named node. Its id is picked server-side.\n"
            '  - "remove_node_ids": ["<id>"] → drop nodes (edges auto-heal).\n'
            '  - "rewire_edges": [{"source": "<id>", "source_handle": "<optional>", '
            '"target": "<id>"}] → replace the outgoing edge from source[+handle] with a new '
            "target.\n\n"
            "Per-node `data_patch` allowed keys, by node type:\n"
            '  agent: name, instructions, modelName, temperature, maxTokens, topP, '
            "tools (array of catalog names), skills (array of catalog names), enable_subagents, "
            "hitlMode ('off'|'before_tool'|'after_response'|'both'), "
            'knowledge ({mode: "none"|"existing_kb"|"add_kb", namespaces: [str]}).\n'
            '  condition: cases (full replacement — array of {id,label,logic:"AND"|"OR",'
            'conditions:[{id,field,operator,value,type}]}).\n'
            '  loop: mode ("for_each"|"count"|"while"), itemsExpression, iteratorVar, count, '
            "maxIterations, cases (while-mode predicate), evaluator_enabled, judgeModel, "
            "stop_policy, threshold, criteria.\n"
            '  evaluation_gate: criteria, threshold (0-1 float), stop_policy '
            '("pass_or_max"|"pass_only"|"max_only"), judgeModel, maxRetries.\n'
            '  subflow: kind ("agent"|"workflow"), refId, refName.\n\n'
            "PHRASING GUIDE — recognise these user phrasings:\n"
            "  * \"agent 2 max tokens 8k\" / \"raise the loop max to 30\" → numeric edits\n"
            "  * \"rename agent 2 to Reviewer\" → node_patches[].data_patch.name\n"
            "  * \"switch the analyst to haiku\" / \"use gpt-5 for agent 1\" → modelName\n"
            "  * \"require human approval before tools\" → hitlMode='before_tool'\n"
            "  * \"add a QA agent after Reviewer\" → add_nodes\n"
            "  * \"remove the compliance agent\" → remove_node_ids\n"
            "  * \"route the reject branch to end\" → rewire_edges\n\n"
            "Rules:\n"
            "  * Return ONLY valid JSON, no markdown, no commentary.\n"
            "  * Never change fields the user did not mention.\n"
            "  * For modelName, only include this field if the user's message explicitly names a "
            'real model / provider / family (e.g. "haiku", "opus", "gpt-5"). NEVER echo a vague '
            'word back as a model id (do NOT return "model", "the model", "new", "default").\n'
            "  * When rewriting instructions, produce the FULL new prompt (not a diff).\n"
            "  * If the request is unrelated to any allowed field, return {}.\n\n"
            "Current workflow snapshot:\n"
            f"{snapshot}\n"
        )
        try:
            raw = await call_factory_llm(
                system,
                [{"role": "user", "content": (user_message or "")[:4000]}],
                max_tokens=4096,
                temperature=0.2,
            )
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict):
                parsed = {}
        except SecurityGatewayRejection:
            raise
        except Exception as exc:
            logger.warning(f'[AGENT] WorkflowFieldPatcher.patch: {exc}')
            parsed = {}

        llm_patch = self._sanitise(parsed, workflow_data)

        # Merge regex-extracted numeric edits INTO the LLM patch. Regex wins for
        # numeric fields (max_tokens / temperature / top_p / maxIterations /
        # threshold) so a small local LLM can't muddle the value the user typed.
        return self._merge_patches(regex_patch, llm_patch)

    # ---- Regex fast-path ------------------------------------------------

    def _regex_patch(self, workflow_data: dict, message: str) -> dict:
        """Extract common numeric / enum edits directly from the message."""
        out: dict = {}
        nodes = ((workflow_data or {}).get("graph_data") or {}).get("nodes") or []
        target_agent = self._pick_target_agent(message, nodes)

        # Agent-scoped numerics.
        mp_patch: dict = {}
        m = _RE_MAX_TOKENS.search(message or "")
        if m:
            try:
                val = int(m.group(1).replace(",", "").replace(".", ""))
                if (m.group(2) or "").lower() in ("k", "thousand"):
                    val *= 1000
                mp_patch["maxTokens"] = val
            except ValueError:
                pass
        m = _RE_TEMPERATURE.search(message or "")
        if m:
            try:
                mp_patch["temperature"] = float(m.group(1))
            except ValueError:
                pass
        m = _RE_TOP_P.search(message or "")
        if m:
            try:
                mp_patch["topP"] = float(m.group(1))
            except ValueError:
                pass
        if mp_patch and target_agent is not None:
            out.setdefault("node_patches", []).append({
                "node_id": target_agent["id"],
                "data_patch": mp_patch,
            })

        # Loop max iterations — routed to the loop node, not the target agent.
        m = _RE_LOOP_MAX.search(message or "")
        if m:
            try:
                val = int(m.group(1))
                loop_node = next((n for n in nodes if n.get("type") == "loop"), None)
                if loop_node is not None and val > 0:
                    out.setdefault("node_patches", []).append({
                        "node_id": loop_node["id"],
                        "data_patch": {"maxIterations": val},
                    })
            except ValueError:
                pass

        # Evaluation-gate threshold. Only routes to the gate node when the
        # message explicitly mentions "threshold" / "quality" / "score" AND a
        # gate exists — otherwise a naked "score 85" could clobber unrelated
        # numeric fields.
        m = _RE_GATE_THRESHOLD.search(message or "")
        if m and re.search(r"\b(?:threshold|quality|score|judge|gate)\b", message or "", re.IGNORECASE):
            try:
                val = float(m.group(1))
                if (m.group(2) or "").strip() == "%" or val > 1.0:
                    val = val / 100.0
                val = max(0.0, min(1.0, val))
                gate = next((n for n in nodes if n.get("type") == "evaluation_gate"), None)
                if gate is not None:
                    out.setdefault("node_patches", []).append({
                        "node_id": gate["id"],
                        "data_patch": {"threshold": val},
                    })
            except ValueError:
                pass

        return out

    def _pick_target_agent(self, message: str, nodes: list[dict]) -> Optional[dict]:
        """Resolve "agent 2" / "the reviewer" / "first agent" to a node dict."""
        agents = [n for n in nodes if n.get("type") == "agent"]
        if not agents:
            return None
        # Numeric ("agent 2") wins if present.
        m = _RE_AGENT_NUMERIC.search(message or "")
        if m:
            try:
                one_based = int(m.group(1))
                if 1 <= one_based <= len(agents):
                    return agents[one_based - 1]
            except ValueError:
                pass
        # Ordinal ("first", "second", "last").
        m = _RE_AGENT_ORDINAL.search(message or "")
        if m:
            key = m.group("ord").lower()
            if key == "last":
                return agents[-1]
            idx = _ORDINAL_INDEX.get(key)
            if idx is not None and 0 <= idx < len(agents):
                return agents[idx]
        # Named — try each agent's data.name.
        lower = (message or "").lower()
        for n in agents:
            name = str((n.get("data") or {}).get("name") or "").strip().lower()
            if name and name in lower:
                return n
        # Only one agent → unambiguous target.
        if len(agents) == 1:
            return agents[0]
        return None

    # ---- Sanitisation ---------------------------------------------------

    def _sanitise(self, patch: dict, workflow_data: dict) -> dict:
        """Filter LLM output to the allow-list and coerce value shapes."""
        out: dict = {}
        if not isinstance(patch, dict):
            return out

        # Workflow rename.
        name = patch.get("name")
        if isinstance(name, str) and name.strip():
            out["name"] = name.strip()

        # Per-node data patches.
        node_patches: list = []
        for np in patch.get("node_patches") or []:
            if not isinstance(np, dict):
                continue
            selector = {k: np.get(k) for k in ("node_id", "node_name", "node_index")}
            node = _find_node(
                ((workflow_data or {}).get("graph_data") or {}).get("nodes") or [],
                selector,
            )
            if node is None:
                continue
            data_patch = self._sanitise_node_data(
                node.get("type"),
                np.get("data_patch") or {},
            )
            if data_patch:
                node_patches.append({"node_id": node["id"], "data_patch": data_patch})
        if node_patches:
            out["node_patches"] = node_patches

        # add_nodes — only allow-listed types with a valid `after` anchor.
        add_nodes: list = []
        existing_nodes = ((workflow_data or {}).get("graph_data") or {}).get("nodes") or []
        for an in patch.get("add_nodes") or []:
            if not isinstance(an, dict):
                continue
            ntype = str(an.get("type") or "").strip().lower()
            if ntype not in _NEW_NODE_TYPES:
                continue
            after_sel = an.get("after")
            after_node = None
            if isinstance(after_sel, str):
                after_node = next((n for n in existing_nodes if n.get("id") == after_sel), None)
                if after_node is None:
                    lower = after_sel.strip().lower()
                    after_node = next(
                        (n for n in existing_nodes if str((n.get("data") or {}).get("name") or "").strip().lower() == lower),
                        None,
                    )
            if after_node is None:
                # No valid anchor — reject: we won't guess where to splice.
                continue
            data = an.get("data") if isinstance(an.get("data"), dict) else {}
            data = self._sanitise_node_data(ntype, data)
            add_nodes.append({
                "type": ntype,
                "after": after_node["id"],
                "data": data,
            })
        if add_nodes:
            out["add_nodes"] = add_nodes

        # remove_node_ids — filter to ids that actually exist and are not start/end.
        remove_ids: list = []
        for rid in patch.get("remove_node_ids") or []:
            if not isinstance(rid, str):
                continue
            node = next((n for n in existing_nodes if n.get("id") == rid), None)
            if node is None:
                continue
            if node.get("type") in ("start", "end"):
                continue  # never remove terminals
            remove_ids.append(rid)
        if remove_ids:
            out["remove_node_ids"] = remove_ids

        # rewire_edges — validate source + target exist.
        rewires: list = []
        for rw in patch.get("rewire_edges") or []:
            if not isinstance(rw, dict):
                continue
            src = rw.get("source")
            tgt = rw.get("target")
            if not (isinstance(src, str) and isinstance(tgt, str)):
                continue
            if not any(n.get("id") == src for n in existing_nodes):
                continue
            if not any(n.get("id") == tgt for n in existing_nodes):
                continue
            entry = {"source": src, "target": tgt}
            handle = rw.get("source_handle")
            if isinstance(handle, str) and handle.strip():
                entry["source_handle"] = handle.strip()
            rewires.append(entry)
        if rewires:
            out["rewire_edges"] = rewires

        return out

    def _sanitise_node_data(self, node_type: Optional[str], data_patch: dict) -> dict:
        """Filter a per-node data_patch to the allow-list for that type."""
        allowed = _NODE_TYPE_ALLOWED_FIELDS.get(node_type or "")
        if not allowed or not isinstance(data_patch, dict):
            return {}
        out: dict = {}
        for k, v in data_patch.items():
            if k not in allowed:
                continue
            # Type-specific coercion.
            if k == "temperature" and isinstance(v, (int, float, str)):
                try:
                    out[k] = max(0.0, min(1.0, float(v)))
                except (TypeError, ValueError):
                    continue
            elif k == "topP" and isinstance(v, (int, float, str)):
                try:
                    out[k] = max(0.0, min(1.0, float(v)))
                except (TypeError, ValueError):
                    continue
            elif k == "maxTokens":
                try:
                    out[k] = max(1, min(200000, int(float(v))))
                except (TypeError, ValueError):
                    continue
            elif k == "maxIterations":
                try:
                    out[k] = max(1, min(100, int(float(v))))
                except (TypeError, ValueError):
                    continue
            elif k == "threshold":
                try:
                    fv = float(v)
                    if fv > 1.0:
                        fv = fv / 100.0
                    out[k] = max(0.0, min(1.0, fv))
                except (TypeError, ValueError):
                    continue
            elif k == "count":
                try:
                    out[k] = max(1, min(1000, int(float(v))))
                except (TypeError, ValueError):
                    continue
            elif k == "maxRetries":
                try:
                    out[k] = max(1, min(10, int(float(v))))
                except (TypeError, ValueError):
                    continue
            elif k == "mode" and node_type == "loop":
                if v in _LOOP_MODES:
                    out[k] = v
            elif k == "kind" and node_type == "subflow":
                if v in _SUBFLOW_KINDS:
                    out[k] = v
            elif k == "hitlMode":
                if v in _HITL_VALUES:
                    out[k] = v
            elif k == "stop_policy":
                if v in _STOP_POLICIES:
                    out[k] = v
            elif k == "enable_subagents":
                out[k] = bool(v)
            elif k == "evaluator_enabled":
                out[k] = bool(v)
            elif k == "knowledge" and isinstance(v, dict):
                mode = str(v.get("mode") or "none").lower()
                if mode not in _KB_MODES:
                    mode = "none"
                ns = v.get("namespaces") or []
                if not isinstance(ns, list):
                    ns = []
                out[k] = {"mode": mode, "namespaces": [str(n) for n in ns if n]}
            elif k in ("tools", "skills"):
                if isinstance(v, list):
                    out[k] = [
                        ({"name": item.get("name")} if isinstance(item, dict) and item.get("name")
                         else ({"name": item} if isinstance(item, str) and item else None))
                        for item in v
                    ]
                    out[k] = [x for x in out[k] if x]
            elif k == "cases" and isinstance(v, list):
                out[k] = v  # deep validation runs via _repair_condition_cases after apply
            elif isinstance(v, str) and v.strip():
                out[k] = v
            elif isinstance(v, (int, float, bool)):
                out[k] = v
        return out

    # ---- Patch merge ----------------------------------------------------

    def _merge_patches(self, regex_patch: dict, llm_patch: dict) -> dict:
        """Combine two patches; regex numeric values win over LLM values."""
        merged: dict = dict(llm_patch or {})

        # Regex only ever produces node_patches. Merge per-node.
        rn = regex_patch.get("node_patches") or []
        if rn:
            existing = {p["node_id"]: p for p in merged.get("node_patches") or []}
            for r in rn:
                nid = r["node_id"]
                if nid in existing:
                    existing[nid]["data_patch"].update(r["data_patch"])
                else:
                    existing[nid] = r
            merged["node_patches"] = list(existing.values())
        return merged

    # ---- Apply ----------------------------------------------------------

    @staticmethod
    def apply(workflow_data: dict, patch: dict) -> dict:
        """Return a new workflow_data with the patch applied.

        Deep-copies the input so callers can hold on to the previous
        version. Runs deterministic shape / condition / loop repair after
        every apply so a bad LLM patch can't leave the graph in a state the
        engine can't execute.
        """
        wf = json.loads(json.dumps(workflow_data or {}))
        graph = wf.setdefault("graph_data", {})
        nodes = graph.setdefault("nodes", [])
        edges = graph.setdefault("edges", [])

        if isinstance(patch.get("name"), str) and patch["name"].strip():
            wf["name"] = patch["name"].strip()

        # 1. Per-node data patches.
        by_id = {n.get("id"): n for n in nodes if isinstance(n, dict)}
        for np in patch.get("node_patches") or []:
            node = by_id.get(np.get("node_id"))
            if not node:
                continue
            data = dict(node.get("data") or {})
            data.update(np.get("data_patch") or {})
            node["data"] = data

        # 2. Remove nodes (auto-heal edges: predecessor → successor).
        for rid in patch.get("remove_node_ids") or []:
            _remove_node_and_heal(nodes, edges, rid)
            by_id.pop(rid, None)

        # 3. Add nodes.
        for an in patch.get("add_nodes") or []:
            _add_node_after(nodes, edges, an)

        # 4. Rewire edges.
        for rw in patch.get("rewire_edges") or []:
            _rewire_edge(edges, rw)

        # 5. Deterministic repair on any touched condition / loop.
        for node in nodes:
            if node.get("type") == "condition":
                _repair_condition_cases(node, edges)
            elif node.get("type") == "loop":
                _repair_loop_node(node, edges)

        graph["nodes"] = nodes
        graph["edges"] = edges
        wf["graph_data"] = graph
        return wf


def _remove_node_and_heal(nodes: list[dict], edges: list[dict], node_id: str) -> None:
    """Delete a node and stitch the graph back together.

    Rewires every edge whose target was ``node_id`` onto the successor(s) of
    ``node_id`` so the graph stays connected. Drops edges that would end up
    pointing at themselves. Terminal nodes (start/end) must never reach here
    — the caller filters them out.
    """
    successors = [e["target"] for e in edges if e.get("source") == node_id]
    predecessors = [e for e in edges if e.get("target") == node_id]

    # Rewire each predecessor onto every successor.
    for pred in predecessors:
        for succ_id in successors or [None]:
            if succ_id is None:
                continue
            if pred.get("source") == succ_id:
                continue  # would create a self-loop
            pred["target"] = succ_id
    # Drop edges whose source / target was the removed node itself.
    edges[:] = [e for e in edges if e.get("source") != node_id and e.get("target") != node_id]
    # Restore the predecessor-rewired edges.
    for pred in predecessors:
        # Only keep the rewired version if its target survived.
        if pred.get("target") and any(n.get("id") == pred["target"] for n in nodes if n.get("id") != node_id):
            if pred not in edges:
                edges.append(pred)
    # Drop the node.
    nodes[:] = [n for n in nodes if n.get("id") != node_id]


def _add_node_after(nodes: list[dict], edges: list[dict], spec: dict) -> None:
    """Splice a new node in after ``spec['after']``.

    Reuses the anchor node's outgoing edge as the new node's outgoing edge
    (so the graph stays connected) and picks a fresh id + a reasonable
    default position 300px to the right of the anchor.
    """
    anchor_id = spec.get("after")
    anchor = next((n for n in nodes if n.get("id") == anchor_id), None)
    if not anchor:
        return
    existing_ids = {n.get("id") for n in nodes if n.get("id")}
    prefix_map = {
        "agent": "agent", "condition": "cond", "loop": "loop",
        "evaluation_gate": "gate", "subflow": "sub", "end": "end",
    }
    new_id = _fresh_node_id(prefix_map.get(spec["type"], "node"), existing_ids)
    anchor_pos = anchor.get("position") or {"x": 400, "y": 300}
    new_pos = {"x": (anchor_pos.get("x") or 400) + 300, "y": anchor_pos.get("y") or 300}
    new_node = {
        "id": new_id,
        "type": spec["type"],
        "position": new_pos,
        "data": spec.get("data") or {},
    }
    nodes.append(new_node)

    # Reroute the anchor's outgoing edge(s) to point at the new node; the new
    # node then edges into the anchor's original successor. Handle only ONE
    # forward edge from the anchor (the common case). Anchors with multiple
    # successors are left alone — the LLM should use rewire_edges for that.
    outgoing = [e for e in edges if e.get("source") == anchor_id and not e.get("source_handle")]
    if outgoing:
        first = outgoing[0]
        original_target = first["target"]
        first["target"] = new_id
        edges.append({
            "id": f"e-{new_id}-{original_target}",
            "source": new_id,
            "target": original_target,
            "type": "default",
            "style": {"stroke": "#6366f1", "strokeWidth": 2},
        })
    else:
        # Anchor had no forward edges — just chain the new node on.
        edges.append({
            "id": f"e-{anchor_id}-{new_id}",
            "source": anchor_id,
            "target": new_id,
            "type": "default",
            "style": {"stroke": "#6366f1", "strokeWidth": 2},
        })


def _rewire_edge(edges: list[dict], spec: dict) -> None:
    """Replace the edge from ``spec['source']`` (optionally by handle) with a new target."""
    src = spec.get("source")
    handle = spec.get("source_handle")
    new_target = spec.get("target")
    if not (src and new_target):
        return
    for e in edges:
        if e.get("source") != src:
            continue
        if handle and e.get("sourceHandle") != handle:
            continue
        e["target"] = new_target
        return
    # No existing edge matched — insert a new one so the intended wiring exists.
    edge_id = f"e-{src}-{handle or 'x'}-{new_target}"
    entry = {
        "id": edge_id, "source": src, "target": new_target,
        "type": "default",
        "style": {"stroke": "#6366f1", "strokeWidth": 2},
    }
    if handle:
        entry["sourceHandle"] = handle
    edges.append(entry)


def _summarise_workflow_for_prompt(workflow_data: dict) -> str:
    """Compact JSON snapshot of the workflow for the patcher LLM prompt.

    Strips heavy fields (long instructions, full tool/skill descriptions)
    down to what's needed for the LLM to identify nodes and decide what to
    change. The full instruction text is only sent when the user's message
    already looks like an instructions edit — a heuristic caller decides.
    """
    graph = (workflow_data or {}).get("graph_data") or {}
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []

    def _short(s: str, n: int = 160) -> str:
        s = s or ""
        return s if len(s) <= n else s[:n] + "…"

    node_lines = []
    for idx, n in enumerate(nodes):
        d = n.get("data") or {}
        row = {
            "id": n.get("id"),
            "type": n.get("type"),
            "index": idx,
            "name": d.get("name") or d.get("label"),
        }
        if n.get("type") == "agent":
            row["model"] = d.get("modelName")
            row["temperature"] = d.get("temperature")
            row["maxTokens"] = d.get("maxTokens")
            row["topP"] = d.get("topP")
            row["hitlMode"] = d.get("hitlMode")
            row["tools"] = [t.get("name") if isinstance(t, dict) else t for t in (d.get("tools") or [])]
            row["skills"] = [s.get("name") if isinstance(s, dict) else s for s in (d.get("skills") or [])]
            row["instructions_preview"] = _short(d.get("instructions") or "")
        elif n.get("type") == "loop":
            row["mode"] = d.get("mode")
            row["maxIterations"] = d.get("maxIterations")
            row["itemsExpression"] = d.get("itemsExpression")
            row["count"] = d.get("count")
        elif n.get("type") == "evaluation_gate":
            row["criteria"] = _short(d.get("criteria") or "")
            row["threshold"] = d.get("threshold")
            row["stop_policy"] = d.get("stop_policy")
        elif n.get("type") == "condition":
            row["case_labels"] = [c.get("label") for c in (d.get("cases") or [])]
        elif n.get("type") == "subflow":
            row["kind"] = d.get("kind")
            row["refName"] = d.get("refName")
        node_lines.append(row)

    edge_lines = [
        {
            "source": e.get("source"),
            "sourceHandle": e.get("sourceHandle"),
            "target": e.get("target"),
        }
        for e in edges
    ]
    payload = {
        "name": (workflow_data or {}).get("name"),
        "nodes": node_lines,
        "edges": edge_lines,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
