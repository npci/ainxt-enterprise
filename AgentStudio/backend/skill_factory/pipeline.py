# SPDX-License-Identifier: MIT
"""
Skill Factory Pipeline — conversational skill creation following the
AiNxt skill-authoring methodology.
"""
from __future__ import annotations
import asyncio
import io
import json

import os
import re
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import logger
from app.core.factory_utils import (
    build_factory_llm_config as _build_llm_config,
    call_factory_llm as _call_llm,
    parse_json_response as _parse_json,
    SecurityGatewayRejection,
)


# ---------------------------------------------------------------------------
# Skill-authoring guidance loader
# ---------------------------------------------------------------------------
# The "Create with AI" pipeline follows the AiNxt skill-authoring methodology.
# Prompts were hand-copied and can drift out of sync with the canonical spec
# supplied via AINXT_SKILL_GUIDANCE_MD. Rather than duplicate that
# authoring guidance inline (where nobody remembers to update it), we read the
# real file from disk at runtime and inject the relevant sections into the
# generator prompts. Editing the .md now changes how skills are generated with
# no code change — the file becomes the single source of truth for *how to
# write a skill*, while the pipeline still owns *the machinery* (staging,
# validation, quality loop).

# Optional authoring guidance. This repo ships no built-in guidance file, so by
# default the loader degrades to the inline prompts — which is the behaviour that
# has actually been in effect. Point AINXT_SKILL_GUIDANCE_MD at a Markdown file
# to supply your own authoring guidance without changing code.
_SKILL_GUIDANCE_ENV = os.getenv("AINXT_SKILL_GUIDANCE_MD")
_SKILL_CREATOR_MD = Path(_SKILL_GUIDANCE_ENV) if _SKILL_GUIDANCE_ENV else None


class _SkillCreatorGuidance:
    """Reads the guidance file from disk and caches it, re-reading only
    when the file's mtime changes.

    We extract just the authoring-relevant sections (the ones that describe how
    to *write* a good SKILL.md) rather than injecting the whole file — most of
    it covers the eval/benchmark/iteration loop that this pipeline implements
    its own way (SkillQualityLoop, SkillEvaluator), and injecting the whole file
    into every prompt would bloat token cost for no benefit.
    """

    # Section headings we want as authoring guidance. Matched against the text
    # after a Markdown heading marker (## or ###), case-insensitive.
    _WANTED_SECTIONS = (
        "Write the SKILL.md",
        "Skill Writing Guide",
        "How skill triggering works",
    )

    def __init__(self, path: Optional[Path]):
        self._path = path
        self._mtime: float = -1.0
        self._authoring: str = ""

    def _clear(self) -> None:
        self._mtime = -1.0
        self._authoring = ""

    def _refresh(self) -> None:
        # No external guidance configured — stay on the inline prompts.
        if self._path is None:
            self._clear()
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            # File missing (e.g. skills/ not deployed) — degrade gracefully to
            # empty guidance so the pipeline still runs on the inline prompts.
            if self._mtime != -1.0:
                logger.warning(f'[AGENT] skill-authoring guidance unavailable at {self._path}: {exc}')
            self._clear()
            return

        if mtime == self._mtime:
            return

        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f'[AGENT] skill-authoring guidance read failed at {self._path}: {exc}')
            self._clear()
            return

        self._mtime = mtime
        self._authoring = self._extract_authoring(raw)
        logger.info(f'[AGENT] Loaded skill-authoring guidance from {self._path} ({len(self._authoring)} chars authoring guidance)')

    @classmethod
    def _extract_authoring(cls, raw: str) -> str:
        """Pull the authoring-relevant sections out of the full SKILL.md.

        Splits on Markdown headings and keeps sections whose title matches one
        of ``_WANTED_SECTIONS`` (plus everything nested under them until the
        next same-or-higher-level heading). Lines inside fenced code blocks are
        never treated as headings — the spec's examples contain ``## ``-prefixed
        sample text that would otherwise start/stop capture spuriously.
        """
        wanted_lower = tuple(s.lower() for s in cls._WANTED_SECTIONS)
        out: list[str] = []
        capturing = False
        capture_level = 0
        in_fence = False

        for line in raw.splitlines():
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
            elif not in_fence:
                m = re.match(r"^(#{2,6})\s+(.*)$", line)
                if m:
                    level = len(m.group(1))
                    title = m.group(2).strip().lower()
                    if any(w in title for w in wanted_lower):
                        capturing = True
                        capture_level = level
                        out.append(line)
                        continue
                    if capturing and level <= capture_level:
                        capturing = False
            if capturing:
                out.append(line)

        return "\n".join(out).strip()

    @property
    def authoring(self) -> str:
        """Authoring guidance sections, or '' when the file is unavailable."""
        self._refresh()
        return self._authoring


_skill_creator_guidance = _SkillCreatorGuidance(_SKILL_CREATOR_MD)


def _guidance_block(context_hint: str) -> str:
    """Return an injectable prompt block sourced from the guidance file.

    Returns an empty string when the file is unavailable, so callers can safely
    concatenate it onto their inline SYSTEM prompt without conditionals. The
    inline prompts stay as a self-sufficient fallback — the on-disk guidance
    *augments* them with the canonical methodology.
    """
    authoring = _skill_creator_guidance.authoring
    if not authoring:
        logger.debug(f'[AGENT] No external skill-authoring guidance configured (set AINXT_SKILL_GUIDANCE_MD to supply one); generation uses the inline prompts. Context: {context_hint}')
        return ""
    return (
        "\n\n---\n"
        "CANONICAL SKILL-AUTHORING GUIDANCE (from the AiNxt skill-authoring "
        f"specification — follow it when {context_hint}). Where it conflicts "
        "with anything above, the guidance below wins:\n\n"
        f"{authoring}\n"
        "---\n"
    )


# ---------------------------------------------------------------------------
# Skill validation — quick structural validation
# ---------------------------------------------------------------------------

_ALLOWED_FRONTMATTER_KEYS = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}


def parse_frontmatter(content: str) -> dict:
    """Parse a SKILL.md YAML frontmatter block into a flat key→value dict.

    Deliberately PyYAML-free — handles the simple ``key: value`` lines skills
    use (quoted or unquoted) and returns ``{}`` when no frontmatter is present.
    Shared by validation, description extraction, and the upload importer so the
    parsing rules stay in one place.
    """
    m = re.match(r"^---\n(.*?)\n---", content.strip(), re.DOTALL)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).splitlines():
        km = re.match(r'^(\w[\w-]*):\s*(.*)', line)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip('"').strip("'")
    return fm


def _validate_skill_md(content: str) -> tuple[bool, str]:
    """Validate a SKILL.md string (frontmatter only) without PyYAML."""
    content = content.strip()
    if not content.startswith("---"):
        return False, "No YAML frontmatter found"

    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return False, "Invalid frontmatter format — missing closing ---"

    fm = parse_frontmatter(content)

    unexpected = set(fm.keys()) - _ALLOWED_FRONTMATTER_KEYS
    if unexpected:
        return False, f"Unexpected frontmatter key(s): {', '.join(sorted(unexpected))}. Allowed: {', '.join(sorted(_ALLOWED_FRONTMATTER_KEYS))}"

    if "name" not in fm:
        return False, "Missing 'name' in frontmatter"
    if "description" not in fm:
        return False, "Missing 'description' in frontmatter"

    name = fm["name"].strip()
    if name:
        if not re.match(r"^[a-z0-9-]+$", name):
            return False, f"Name '{name}' must be kebab-case (lowercase letters, digits, hyphens only)"
        if name.startswith("-") or name.endswith("-") or "--" in name:
            return False, f"Name '{name}' cannot start/end with hyphen or have consecutive hyphens"
        if len(name) > 64:
            return False, f"Name too long ({len(name)} chars, max 64)"

    description = fm["description"].strip()
    if description:
        if "<" in description or ">" in description:
            return False, "Description cannot contain angle brackets"
        if len(description) > 1024:
            return False, f"Description too long ({len(description)} chars, max 1024)"

    return True, "Skill is valid"


def _lint_skill_md(content: str) -> list[str]:
    """Style/quality lint that runs *after* validation passes.

    Returns a list of human-readable issues. Empty list = clean. These mirror
    the qualitative checks from the skill-authoring methodology — they're not
    blockers (the skill still saves), but they feed into the quality loop's
    critique so the regeneration pass can target them.
    """
    issues: list[str] = []
    stripped = content.strip()

    # Pull frontmatter description out for the description-shape checks.
    m = re.match(r"^---\n(.*?)\n---", stripped, re.DOTALL)
    description = ""
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip().strip('"').strip("'")
                break

    if description:
        if len(description) < 30:
            issues.append(
                "description is too short — agents will under-trigger this skill. "
                "Aim for 1-2 sentences with concrete trigger phrases."
            )
        if not description.lower().startswith("use when") and "use when" not in description.lower()[:20]:
            issues.append(
                "description does not start with 'Use when' — the skill-authoring spec "
                "requires this so triggering is predictable."
            )
        # Pushiness check: the skill-authoring spec explicitly says descriptions should
        # be a little "pushy" — i.e. include language that nudges the model to
        # use the skill even when the user doesn't name it. Single-sentence
        # descriptions almost never have that.
        sentence_count = len([s for s in re.split(r"(?<=[.!?])\s+", description) if s.strip()])
        if sentence_count < 2:
            issues.append(
                "description has only one sentence — add a second sentence listing "
                "trigger phrases / file types / contexts so the agent loads this "
                "skill even when the user doesn't name it explicitly."
            )

    body = stripped
    # Strip frontmatter for body checks
    if m:
        body = stripped[m.end():]

    if "do not use when" not in body.lower():
        issues.append(
            "body is missing a 'Do not use when' section — this is what prevents "
            "false-positive triggers on adjacent tasks."
        )

    # Imperative-step check — flag the classic anti-patterns the spec warns about.
    bad_phrases = [
        ("the skill will", "approach steps must be imperative directives, not narration"),
        ("it scans", "approach steps must be imperative directives, not narration"),
        ("this skill helps", "filler phrase that doesn't earn its tokens — be specific"),
    ]
    body_lower = body.lower()
    for phrase, why in bad_phrases:
        if phrase in body_lower:
            issues.append(f"body contains '{phrase}' — {why}.")

    # Placeholder-example check
    placeholder_markers = ["sample input here", "your input here", "<example>", "placeholder"]
    if any(p in body_lower for p in placeholder_markers):
        issues.append(
            "example section contains placeholder text — replace with a realistic, "
            "specific example that matches the documented schema."
        )

    return issues


# ---------------------------------------------------------------------------
# Skill packaging — skill packaging
# ---------------------------------------------------------------------------

def _package_skill(
    name: str,
    content: str,
    bundle_files: Optional[list[dict]] = None,
) -> bytes:
    """Return a .skill (zip) file as bytes containing SKILL.md plus any
    bundled scripts/references.

    ``bundle_files`` is the same shape we persist via ``upsert_skill_files``:
    a list of ``{rel_path, content, ...}`` dicts. ``rel_path`` is relative to
    the skill folder (e.g. ``scripts/extract.py``, ``references/spec.md``).
    """
    buf = io.BytesIO()
    skill_name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") or "skill"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{skill_name}/SKILL.md", content)
        for f in (bundle_files or []):
            rel = f.get("rel_path", "").lstrip("/").replace("\\", "/")
            if not rel or ".." in rel.split("/"):
                continue  # never let a path escape the skill folder
            body = f.get("content", "")
            zf.writestr(f"{skill_name}/{rel}", body)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Catalog cache — avoids hitting postgres on every blueprint generation call
# ---------------------------------------------------------------------------

_CATALOG_TTL = 60  # seconds

class _CatalogCache:
    def __init__(self):
        self._skills: Optional[list] = None
        self._tools: Optional[list] = None
        # Derived once per refresh from ``_tools`` so the workflow factory's
        # per-agent tool resolution doesn't rebuild it on every request. Shares
        # the 60s catalog TTL — rebuilt only when the tool list refreshes.
        self._service_index: Optional[dict] = None
        self._ts: float = 0.0
        self._lock: Optional[asyncio.Lock] = None  # lazy — created inside event loop

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _expired(self) -> bool:
        return (time.monotonic() - self._ts) > _CATALOG_TTL

    async def get(self) -> tuple[list, list]:
        async with self._get_lock():
            if self._expired():
                try:
                    from app import workflow_repo
                    all_skills, all_tools = await asyncio.gather(
                        workflow_repo.list_skills(),
                        workflow_repo.list_tools(),
                    )
                    # Exclude platform/M365 tools — mirrors /tools-catalog filter.
                    _EXCLUDED_SERVICES = {"platform", "microsoft_365"}
                    self._tools = [t for t in all_tools if (t.get("service") or "").lower() not in _EXCLUDED_SERVICES]
                    self._skills = all_skills
                    self._service_index = None
                    self._ts = time.monotonic()
                    logger.debug(f'[AGENT] CatalogCache: refreshed ({len(self._tools)}/{len(all_tools)} tools, {len(self._skills)} skills)')
                except Exception as exc:
                    logger.warning(f'[AGENT] CatalogCache: refresh failed: {exc}')
                    self._skills = self._skills or []
                    self._tools = self._tools or []
            return self._skills or [], self._tools or []

    async def get_service_index(self) -> dict:
        """Return the derived service index for the current tool catalog.

        Built lazily from the cached tools and memoised until the next catalog
        refresh, so callers get catalog-accurate tool matching for ~free.
        """
        from app.core.factory_utils import build_service_index
        await self.get()  # ensure tools are loaded / fresh
        if self._service_index is None:
            self._service_index = build_service_index(self._tools or [])
        return self._service_index

    def invalidate(self):
        self._ts = 0.0


catalog_cache = _CatalogCache()


# ---------------------------------------------------------------------------
# Template candidate cache — avoids a DB round-trip on every semantic match
# call. Templates change rarely (admin-only), so a 5-minute TTL is safe and
# means the match task starts its LLM call almost immediately instead of
# waiting on postgres first.
# ---------------------------------------------------------------------------

_TEMPLATE_CANDIDATE_TTL = int(os.getenv("FACTORY_TEMPLATE_CACHE_TTL_S", "300"))


class _TemplateCandidateCache:
    """Cache workflow/agent/skill candidates for semantic matching."""

    def __init__(self):
        self._workflows: Optional[list] = None
        self._agents: Optional[list] = None
        self._skills: Optional[list] = None
        self._ts: float = 0.0
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _expired(self) -> bool:
        return (time.monotonic() - self._ts) > _TEMPLATE_CANDIDATE_TTL

    async def get(self, kind: str) -> list[dict]:
        async with self._get_lock():
            if self._expired():
                await self._refresh()
        if kind == "workflow":
            return self._workflows or []
        if kind == "agent":
            return self._agents or []
        if kind == "skill":
            return self._skills or []
        return []

    async def _refresh(self):
        try:
            from app import workflow_repo
            wf_templates, agent_templates, skills = await asyncio.gather(
                workflow_repo.get_all_templates(),
                workflow_repo.get_all_agent_templates(),
                workflow_repo.list_skills(),
                return_exceptions=True,
            )
            self._workflows = [
                {"id": t.get("id"), "name": t.get("name"),
                 "description": t.get("description") or "", "kind": "workflow_template"}
                for t in (wf_templates if isinstance(wf_templates, list) else [])
                if t.get("id") and t.get("name")
            ]
            self._agents = [
                {"id": t.get("id"), "name": t.get("name"),
                 "description": t.get("description") or "", "kind": "agent_template"}
                for t in (agent_templates if isinstance(agent_templates, list) else [])
                if t.get("id") and t.get("name")
            ]
            self._skills = [
                {"id": s.get("id") or s.get("name"), "name": s.get("name"),
                 "description": s.get("description") or "", "kind": "skill"}
                for s in (skills if isinstance(skills, list) else [])
                if s.get("name")
            ]
            self._ts = time.monotonic()
            logger.debug(f'[AGENT] TemplateCandidateCache: refreshed — workflows={len(self._workflows)} agents={len(self._agents)} skills={len(self._skills)}')
        except Exception as exc:
            logger.warning(f'[AGENT] TemplateCandidateCache: refresh failed: {exc}')
            self._workflows = self._workflows or []
            self._agents = self._agents or []
            self._skills = self._skills or []

    def invalidate(self):
        self._ts = 0.0


template_candidate_cache = _TemplateCandidateCache()


# ---------------------------------------------------------------------------
# Skill generation dedup lock — prevents two concurrent agent creations from
# generating the same skill simultaneously
# ---------------------------------------------------------------------------

_skill_gen_locks: dict[str, asyncio.Lock] = {}
_skill_gen_locks_mu: Optional[asyncio.Lock] = None  # lazy


def _get_skill_gen_mu() -> asyncio.Lock:
    global _skill_gen_locks_mu
    if _skill_gen_locks_mu is None:
        _skill_gen_locks_mu = asyncio.Lock()
    return _skill_gen_locks_mu


async def acquire_skill_gen_lock(name: str) -> asyncio.Lock:
    """Return (and create if needed) a per-skill-name asyncio.Lock."""
    async with _get_skill_gen_mu():
        if name not in _skill_gen_locks:
            _skill_gen_locks[name] = asyncio.Lock()
        return _skill_gen_locks[name]


# ---------------------------------------------------------------------------
# Skill evaluator — LLM-based replacement for run_eval.py
# Tests whether the skill's description correctly triggers in realistic scenarios
# ---------------------------------------------------------------------------

class SkillEvaluator:
    """
    Evaluates a generated SKILL.md by simulating the trigger-accuracy test
    without needing an external CLI.

    Generates positive (should trigger) and negative (should NOT trigger)
    scenarios, then asks the LLM whether it would load the skill for each.
    Returns a score and actionable feedback.
    """

    SCENARIO_SYSTEM = """You are generating test scenarios for a skill trigger evaluation.
Given a skill description, generate realistic user messages to test if the skill would be loaded.

Return JSON only:
{
  "positive": ["<msg that SHOULD trigger>", "<msg>", "<msg>", "<msg>", "<msg>"],
  "negative": ["<msg that should NOT trigger>", "<msg>", "<msg>"]
}

For positives: vary phrasing across the 5 messages — don't just rephrase the description. Use realistic user language from different angles.
For negatives: choose messages from adjacent domains that could be confused with this skill but shouldn't trigger it."""

    JUDGE_SYSTEM = """You are a precise skill-trigger evaluator.
A skill is loaded when the user's need clearly falls within the skill's "Use when" scope.

Rules:
- load=true only if the user's need is a direct match for the skill's described purpose
- load=false if the message is adjacent, partial, or ambiguous — be strict
- False positives waste resources; when in doubt, return false

Answer with JSON only: {"load": true/false, "reason": "<one sentence explaining the decision>"}"""

    async def evaluate(self, content: str, name: str) -> dict:
        description = self._extract_description(content)
        if not description:
            return {"score": 0, "max_score": 0, "error": "Could not extract description from frontmatter"}

        # Step 1: generate test scenarios
        try:
            scenario_prompt = f'Skill description: "{description}"\n\nGenerate 3 positive and 2 negative test messages.'
            raw = await _call_llm(self.SCENARIO_SYSTEM, [{"role": "user", "content": scenario_prompt}], max_tokens=512)
            scenarios = _parse_json(raw)
        except Exception as exc:
            return {"score": 0, "max_score": 0, "error": f"Scenario generation failed: {exc}"}

        positives = scenarios.get("positive", [])[:5]
        negatives = scenarios.get("negative", [])[:3]
        if not positives:
            return {"score": 0, "max_score": 0, "error": "No test scenarios generated"}

        # Step 2: judge each scenario — all calls are independent, run in parallel
        async def _judge_one(msg: str, expected: bool) -> dict:
            verdict = await self._judge(description, msg)
            default_got = False if expected else True
            return {
                "message": msg,
                "expected": expected,
                "got": verdict.get("load", default_got),
                "reason": verdict.get("reason", ""),
            }

        judge_tasks = [_judge_one(msg, True) for msg in positives]
        judge_tasks += [_judge_one(msg, False) for msg in negatives]
        results = await asyncio.gather(*judge_tasks)

        correct = sum(1 for r in results if r["expected"] == r["got"])
        total = len(results)
        score = round(correct / total * 100) if total else 0

        # Step 3: build feedback
        failures = [r for r in results if r["expected"] != r["got"]]
        feedback = []
        if failures:
            false_positives = [r for r in failures if not r["expected"] and r["got"]]
            false_negatives = [r for r in failures if r["expected"] and not r["got"]]
            if false_negatives:
                feedback.append(f"Description too narrow — missed {len(false_negatives)} expected trigger(s). Consider broadening 'Use when' scope.")
            if false_positives:
                feedback.append(f"Description too broad — triggered on {len(false_positives)} unrelated message(s). Add 'Do not use when' constraints.")

        return {
            "score": score,
            "max_score": 100,
            "correct": correct,
            "total": total,
            "results": results,
            "feedback": feedback,
            "passed": score >= 80,
        }

    async def _judge(self, description: str, message: str) -> dict:
        prompt = f'Skill description: "{description}"\n\nUser message: "{message}"\n\nShould this skill be loaded?'
        try:
            raw = await _call_llm(self.JUDGE_SYSTEM, [{"role": "user", "content": prompt}], max_tokens=128)
            parsed = _parse_json(raw)
            if not parsed:
                # LLM returned non-JSON — try a simple yes/no parse
                lower = raw.lower()
                load = "true" in lower or lower.strip().startswith("yes")
                return {"load": load, "reason": raw[:120]}
            return parsed
        except Exception as exc:
            logger.warning(f'[AGENT] SkillEvaluator._judge: {exc}')
            return {"load": False, "reason": f"judge error: {exc}", "error": True}

    def _extract_description(self, content: str) -> str:
        return parse_frontmatter(content).get("description", "")


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class SkillFactorySession:
    session_id: str
    stage: str = "clarifying"  # clarifying → generating → confirm → done
    messages: list[dict] = field(default_factory=list)
    intent: dict = field(default_factory=dict)
    requirements: Optional[dict] = None
    blueprint: Optional[dict] = None
    content: Optional[str] = None
    assembled: Optional[dict] = None
    turn_count: int = 0
    # Kept separate from `assembled` because `assembled["bundle_files"]` is
    # a UI-only manifest without the actual file content — confirm/download
    # endpoints need the full payload.
    bundle_files: list[dict] = field(default_factory=list)
    # Existing skills flagged as near-duplicates of the request. Set when
    # stage == "suggest_existing"; cleared on "build new anyway".
    pending_matches: list[dict] = field(default_factory=list)

SKILL_FACTORY_SESSIONS: dict[str, SkillFactorySession] = {}

def get_or_create_skill_session(session_id: Optional[str]) -> SkillFactorySession:
    if session_id and session_id in SKILL_FACTORY_SESSIONS:
        return SKILL_FACTORY_SESSIONS[session_id]
    sid = session_id or str(uuid.uuid4())
    session = SkillFactorySession(session_id=sid)
    SKILL_FACTORY_SESSIONS[sid] = session
    return session


# ---------------------------------------------------------------------------
# Session persistence (Postgres write-through / read-through)
# ---------------------------------------------------------------------------
# Mirror an in-memory build session to the ``factory_sessions`` table so an
# interrupted "build me a skill" conversation survives a backend restart.
# Best-effort — persistence failures never break the live chat turn.

SKILL_FACTORY_TYPE = "skill"


def serialize_skill_session(session: SkillFactorySession) -> dict:
    from dataclasses import asdict
    return asdict(session)


def hydrate_skill_session(state: dict) -> SkillFactorySession:
    known = {f for f in SkillFactorySession.__dataclass_fields__}  # type: ignore[attr-defined]
    return SkillFactorySession(**{k: v for k, v in (state or {}).items() if k in known})


async def get_or_restore_skill_session(
    session_id: Optional[str], owner_user_id: str
) -> SkillFactorySession:
    if session_id and session_id in SKILL_FACTORY_SESSIONS:
        return SKILL_FACTORY_SESSIONS[session_id]
    if session_id:
        try:
            from app.core import workflow_repo
            state = await workflow_repo.load_factory_session(
                session_id, SKILL_FACTORY_TYPE, owner_user_id
            )
            if state:
                session = hydrate_skill_session(state)
                SKILL_FACTORY_SESSIONS[session.session_id] = session
                return session
        except Exception:
            logger.debug('[AGENT] skill_factory: session restore skipped', exc_info=True)
    return get_or_create_skill_session(session_id)


async def persist_skill_session(
    session: SkillFactorySession, owner_user_id: str
) -> None:
    try:
        from app.core import workflow_repo
        await workflow_repo.save_factory_session(
            session.session_id, SKILL_FACTORY_TYPE, owner_user_id,
            serialize_skill_session(session),
        )
    except Exception:
        logger.debug('[AGENT] skill_factory: session persist skipped', exc_info=True)


# ---------------------------------------------------------------------------
# SkillPlanCardGenerator — structured pre-generation questionnaire
# ---------------------------------------------------------------------------


class SkillPlanCardGenerator:
    """Produce a structured Plan Card for the skill factory (4 static questions).

    One fast LLM call infers the best default per question; option lists are
    always the static lists below (never hallucinated).
    """

    QUESTIONS = [
        {"id": "output_format", "label": "What format should the output be?",
         "options": ["Plain text", "Structured JSON", "Markdown report", "Email-ready"]},
        {"id": "avoid_when", "label": "When should this skill NOT be used?",
         "options": ["None specified", "Sensitive data", "Large files", "Real-time needs"],
         "allow_freetext": True},
        {"id": "detail_level", "label": "How specific should the instructions be?",
         "options": ["Standard", "Step-by-step", "High-level guidance only"]},
        {"id": "include_examples", "label": "Should this skill include example inputs/outputs?",
         "options": ["Yes", "No"]},
    ]

    async def generate(self, intent: dict, user_message: str) -> dict:
        questions = [dict(q) for q in self.QUESTIONS]
        for q in questions:
            q.setdefault("default", q["options"][0])
        defaults = await _infer_plan_card_defaults(
            questions, user_message, intent,
            context="You are configuring a reusable AI skill.",
        )
        for q in questions:
            picked = defaults.get(q["id"])
            if picked in q["options"]:
                q["default"] = picked
        return {"questions": questions}


async def _infer_plan_card_defaults(
    questions: list[dict], user_message: str, intent: dict, context: str,
) -> dict:
    """One fast LLM call: pick the best default option per question.

    Returns ``{question_id: chosen_option}``; never raises (returns ``{}`` on
    failure). Options are constrained to the static lists.
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
        raw = await _call_llm(
            system,
            [{"role": "user", "content": (user_message or "")[:1500]}],
            max_tokens=200,
        )
        parsed = _parse_json(raw)
        return parsed if isinstance(parsed, dict) else {}
    except SecurityGatewayRejection:
        # A compliance/security-gateway rejection MUST propagate — never
        # downgrade a blocked prompt to "use defaults" (mirrors the agent and
        # workflow factory paths).
        raise
    except Exception as exc:
        logger.warning(f'[AGENT] skill plan-card default inference failed: {exc}')
        return {}


# ---------------------------------------------------------------------------
# SkillIntentParser
# ---------------------------------------------------------------------------

class SkillIntentParser:
    SYSTEM = """You are a skill-design intake assistant. Parse the user's request and extract structured intent.
Return JSON only:
{
  "skill_purpose": "<one sentence — what this skill enables an AI agent to do>",
  "domain": "<productivity | development | data | communication | creative | research>",
  "raw_intent": "<verbatim user request>",
  "inferred_trigger": "<when would an agent use this? one specific sentence>",
  "inferred_output": "<what format/type of output: text summary / JSON / report / code / list / etc.>"
}
Output ONLY valid JSON. No markdown."""

    async def parse(self, message: str) -> dict:
        result = await _call_llm(self.SYSTEM, [{"role": "user", "content": message}], max_tokens=512)
        parsed = _parse_json(result)
        if not parsed.get("skill_purpose"):
            parsed["skill_purpose"] = message[:200]
            parsed["domain"] = "general"
            parsed["raw_intent"] = message
        return parsed


# ---------------------------------------------------------------------------
# SkillClarificationEngine
# ---------------------------------------------------------------------------

class SkillClarificationEngine:
    """Multi-turn Q&A following the skill-authoring methodology."""

    SYSTEM = """You are a skill designer. Gather exactly what you need to build a great skill — then BUILD IT.

A skill is a reusable capability an AI agent calls. To generate it you need:
1. Purpose — what does it do?
2. Trigger — when should an agent use it? (the "Use when" condition)
3. Input — what data goes in, and in what format?
4. Output — what does it return, and in what format?
5. Category: productivity / development / data / communication / creative / research

RULES:
- Ask ONE question at a time
- Provide 2-4 suggestion chips per question — specific to the domain, not generic
- Chip format: {"icon": "one emoji", "label": "3-6 words"}
- If purpose + trigger + input are all clear → declare done immediately, infer the rest
- After 3 exchanges → declare done, build with what you have
- NEVER ask more than 4 questions total
- Be decisive — skills can always be refined later

When done=true respond ONLY with:
{"done": true, "requirements": {
  "name": "<kebab-case-name>",
  "display_name": "<Human Readable Name>",
  "purpose": "<precise one sentence — what it does>",
  "triggers": "<specific condition, e.g. 'when user asks to summarize meeting notes' not 'when needed'>",
  "input_description": "<format + what fields/content it expects>",
  "output_description": "<exact format + key fields returned>",
  "category": "<category>",
  "constraints": "<hard limits or 'none'>",
  "wants_scripts": <true if the user explicitly asked for helper scripts / Python code / a runnable tool, false otherwise — do NOT infer true unless they said so>
}}

When not done respond ONLY with:
{"done": false, "question": "<single focused question>", "suggestions": [{"icon": "emoji", "label": "text"}, {"icon": "emoji", "label": "text"}, {"icon": "emoji", "label": "text"}]}

Valid JSON only. No extra text."""

    # Each clarifying turn is a full user-facing round-trip, so keep the Q&A
    # short: force generation after this many user turns. Lower = faster time
    # to a drafted skill at the cost of the model inferring more from less.
    MAX_CLARIFYING_TURNS = int(os.environ.get("SKILL_CLARIFY_MAX_TURNS", "2"))

    async def get_next_question_or_requirements(self, intent: dict, messages: list[dict], turn_count: int = 0) -> dict:
        # Force done once the user has answered enough — enough context to generate
        if turn_count >= self.MAX_CLARIFYING_TURNS:
            force_prompt = (
                f"The user has answered enough questions. Based on this conversation, "
                f"produce the requirements JSON with done=true. "
                f"Infer any missing fields from context. Intent: {json.dumps(intent)}"
            )
            llm_messages = [{"role": "user", "content": force_prompt}] + messages
        else:
            context = (
                f"User wants to create a skill. Initial intent: {json.dumps(intent)}. "
                f"Turns so far: {turn_count}. "
                f"Ask at most ONE more essential question, then declare done — be decisive "
                f"and infer the rest from context."
            )
            llm_messages = [{"role": "user", "content": context}] + messages

        result = await _call_llm(self.SYSTEM, llm_messages, max_tokens=1024)
        parsed = _parse_json(result)
        if not parsed:
            # Fallback: if JSON parse fails and we have enough turns, force done with what we know
            if turn_count >= self.MAX_CLARIFYING_TURNS:
                return {
                    "done": True,
                    "requirements": {
                        "name": intent.get("skill_purpose", "custom_skill").lower().replace(" ", "_")[:30],
                        "display_name": intent.get("skill_purpose", "Custom Skill")[:50],
                        "purpose": intent.get("skill_purpose", "Custom skill"),
                        "triggers": intent.get("domain", "when this capability is needed"),
                        "input_description": "user-provided data",
                        "output_description": "structured result",
                        "category": "general",
                        "constraints": "none",
                    },
                }
            return {"done": False, "question": "What should this skill return as output?", "suggestions": ["Plain text summary", "Structured JSON", "Formatted report"]}
        return parsed


# ---------------------------------------------------------------------------
# SkillBlueprintGenerator
# ---------------------------------------------------------------------------

class SkillBlueprintGenerator:
    SYSTEM = """You are a skill architect. Given requirements, produce a detailed, implementation-ready blueprint.

Return JSON only:
{
  "name": "<kebab-case, max 64 chars, e.g. meeting-notes-summarizer>",
  "display_name": "<Title Case Human Name>",
  "description": "<EXACTLY 2 sentences. Sentence 1 starts with 'Use when' and states the specific trigger. Sentence 2 lists concrete phrases / file types / contexts that should trigger this skill EVEN WHEN the user does not name it explicitly. No angle brackets, no filler like 'this skill helps'.>",
  "category": "<productivity|development|data|communication|creative|research>",
  "overview": "<2-3 sentences: what it does, what problem it solves, what makes it useful>",
  "input_format": "<prose: format (plain text / JSON / CSV), required fields, typical size — e.g. 'Plain text meeting transcript, 200-2000 words, with speaker labels optional'>",
  "triggers": ["<specific phrase>", "<specific phrase>", "<specific phrase>"],
  "do_not_use_when": ["<specific anti-pattern — adjacent task that this skill should NOT handle>", "<another>"],
  "approach": [
    "<imperative step: verb-first, e.g. 'Extract all action items from the transcript'>",
    "<imperative step, e.g. 'Identify the owner and deadline for each action item if mentioned'>",
    "<imperative step, e.g. 'Group items by owner into a structured list'>",
    "<imperative step, e.g. 'Return a markdown list with owner, task, and deadline columns'>"
  ],
  "output_schema": {"type": "object", "properties": {}},
  "example_input": "<realistic, specific example — not placeholder text like 'sample input here'>",
  "example_output": "<realistic, specific example matching the schema>",
  "needs_bundle": <true|false>
}

NEEDS_BUNDLE RULE — this gates whether we spend an extra generation step producing bundled scripts/references, so decide honestly:
- false when the skill is purely LLM-native text work: summarizing, rephrasing, tone/style change, classification, extraction from prose, drafting. These need NO bundled files — the model does them from the SKILL.md instructions alone. This is the common case — prefer false.
- true ONLY when the skill genuinely needs runnable code or long reference material: parsing structured formats (CSV/JSON/XML/ICS), formatted-output generation with one correct algorithm, calling a specific library where copy-pasteable code beats prose, or lookups against a fixed table/schema too large for SKILL.md.

DESCRIPTION RULES — agents under-trigger skills by default, so be a little pushy:
Bad: "Summarizes meeting notes."
Bad: "Use when the user needs help summarizing notes."
Good: "Use when the user wants to turn raw meeting notes or a transcript into structured action items. Use this skill whenever the user mentions meeting minutes, a standup recap, action items, owners, due dates, .txt or .docx files of notes, or says things like 'who's doing what' or 'pull out the to-dos' — even if they don't say 'summarize'."

TRIGGER RULES — be specific:
Bad: "when the user needs help with data"
Good: "when the user asks to extract action items from meeting notes or transcripts"

APPROACH RULES — every step is a directive to the model:
Bad: "The skill will scan the document for relevant information"
Good: "Scan each sentence for verbs in future tense or modal form (will, should, must, need to) — these signal action items"

DO-NOT-USE-WHEN RULES — list adjacent tasks this skill should refuse, so false-positive triggers stay rare:
Good (for a meeting-notes skill): ["the user wants a sentiment analysis of a conversation", "the user pastes code and wants it reviewed", "the user wants to schedule a meeting (not summarize one)"]

EXAMPLE BLUEPRINT (for a "commit-message-generator" skill):
{
  "name": "commit-message-generator",
  "display_name": "Commit Message Generator",
  "description": "Use when the user wants to generate a conventional commit message from staged changes or a diff. Use this skill whenever the user mentions commits, staging, git add, pull requests, merge requests, or says things like 'what should my commit message be' or 'help me write a commit' — even if they don't say 'generate'.",
  "category": "development",
  "overview": "Analyzes a git diff and produces a conventional commit message following the Angular/Conventional Commits spec. Solves the problem of engineers writing vague or inconsistent commit messages.",
  "input_format": "Git diff output (unified diff format), typically 10-500 lines. Can be from 'git diff --staged' or 'git diff HEAD'.",
  "triggers": ["generate commit message", "write a commit", "what should my commit say", "conventional commit"],
  "do_not_use_when": ["the user wants to review code quality", "the user wants to revert a commit", "the user is asking about git branching strategy"],
  "approach": [
    "Parse the diff to identify changed files and the nature of each change (added, modified, deleted)",
    "Classify the overall change type: feat, fix, docs, style, refactor, test, chore, or perf",
    "Extract the primary scope from the most significant file path or module affected",
    "Draft a concise subject line under 72 characters using the format: type(scope): description",
    "Add a body paragraph when the change is non-trivial, explaining the why behind the change",
    "Return the formatted commit message as plain text"
  ],
  "output_schema": {"type": "object", "properties": {"subject": {"type": "string"}, "body": {"type": "string"}, "type": {"type": "string"}, "scope": {"type": "string"}}},
  "example_input": "diff --git a/auth/login.py b/auth/login.py\\nindex 1a2b3c4..5d6e7f8 100644\\n--- a/auth/login.py\\n+++ b/auth/login.py\\n@@ -15,6 +15,8 @@ def login(username, password):\\n+    if not validate_input(username):\\n+        raise ValueError('Invalid username')",
  "example_output": "feat(auth): add input validation to login endpoint\\n\\nPrevents malformed usernames from reaching the database query layer.\\nValidates against a regex pattern before processing.",
  "needs_bundle": false
}"""

    async def generate(self, requirements: dict) -> dict:
        prompt = f"Requirements:\n{json.dumps(requirements, indent=2)}"
        # Plan Card structured decisions are hard constraints on the blueprint.
        directives = []
        if requirements.get("output_format"):
            directives.append(
                f"The skill's output MUST be formatted as: {requirements['output_format']}. "
                "Reflect this in `output_schema`, `example_output`, and the final `approach` step."
            )
        if requirements.get("include_examples") == "No":
            directives.append("Keep `example_input`/`example_output` minimal — the user opted out of detailed examples.")
        if directives:
            prompt += "\n\nHARD CONSTRAINTS:\n" + "\n".join(f"- {d}" for d in directives)
        system = self.SYSTEM + _guidance_block("designing the blueprint and writing the description")
        result = await _call_llm(system, [{"role": "user", "content": prompt}], max_tokens=2048)
        blueprint = _parse_json(result)
        if not blueprint.get("name"):
            blueprint["name"] = requirements.get("name", "custom_skill")
        if not blueprint.get("category"):
            blueprint["category"] = requirements.get("category", "general")
        if not blueprint.get("description"):
            blueprint["description"] = f"Use when {requirements.get('triggers', 'this capability is needed')}."
        # Default to no bundle — most skills are LLM-native text work. Only an
        # explicit true from the model triggers the extra bundle-generation
        # call. Coerce carefully: the model sometimes emits the string "false",
        # which is truthy under bool(), so treat known falsey strings as False.
        nb = blueprint.get("needs_bundle", False)
        if isinstance(nb, str):
            nb = nb.strip().lower() in ("true", "yes", "1")
        blueprint["needs_bundle"] = bool(nb)
        # User opt-in overrides the model's (deliberately conservative) bias:
        # when the intake captured an explicit "yes, generate helper scripts"
        # we force the bundle-decider to run so Python tools are produced even
        # if the model would have said needs_bundle=false.
        ws = requirements.get("wants_scripts")
        if isinstance(ws, str):
            ws = ws.strip().lower() in ("true", "yes", "1")
        if ws:
            blueprint["needs_bundle"] = True
        return blueprint


# ---------------------------------------------------------------------------
# SkillContentGenerator
# ---------------------------------------------------------------------------

_CODE_FENCE_LANGS = {"json", "python", "text", "yaml", "bash", "sh", "javascript", "ts", "typescript", "markdown", "xml", "csv"}
# Lines that look like the start of a code block (not prose)
_CODE_START_RE = re.compile(r'^[\{\[\"\'\d\s#<]|^\w+\s*[=:({\[]')


def _fix_code_fences(content: str) -> str:
    """
    Repair bare language identifiers left when the LLM drops the opening fence.
    Only triggers when the bare word is immediately followed by a line that
    looks like code (not prose) — prevents false-positives on markdown text.
    """
    lines = content.split("\n")
    out = []
    i = 0
    inside_fence = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Track whether we're already inside a fenced block
        if stripped.startswith("```"):
            inside_fence = not inside_fence
            out.append(line)
            i += 1
            continue

        if (
            not inside_fence
            and stripped.lower() in _CODE_FENCE_LANGS
            and not stripped.startswith("```")
            and (i == 0 or lines[i - 1].strip() == "" or lines[i - 1].strip().startswith("#"))
            # Key guard: next line must look like code, not prose
            and i + 1 < len(lines)
            and lines[i + 1].strip() != ""
            and _CODE_START_RE.match(lines[i + 1].strip())
        ):
            out.append(f"```{stripped.lower()}")
            i += 1
            while i < len(lines) and lines[i].strip() != "":
                out.append(lines[i])
                i += 1
            out.append("```")
            continue

        out.append(line)
        i += 1
    return "\n".join(out)


class SkillContentGenerator:
    SYSTEM = """You are writing a SKILL.md file following the AiNxt skill-authoring spec. Generate complete, production-ready content.

REQUIRED STRUCTURE (exactly these sections, in this order):

---
name: kebab-case-name
description: "Use when [specific trigger condition]. Use this skill whenever the user mentions [concrete phrases / file types / synonyms / adjacent contexts] — even if they don't explicitly say [skill-name keyword]."
---

### Overview
2-3 sentences. What it does, what input it needs, what it returns.

### Input Format
Exactly what the skill receives: data type, format (plain text / JSON / CSV), required fields, size guidance.

### When to Use
**Use when:**
- [specific condition — not 'when the user needs help']
- [specific condition]

**Do not use when:**
- [common misuse to prevent — be specific, adjacent task this skill should refuse]
- [another anti-pattern]

### Approach
1. [Imperative directive — Extract X from Y]
2. [Imperative directive — Identify Z based on W criteria]
3. [Imperative directive — Format results as V]
4. [Imperative directive — Return U with summary line at top]

### Output Format
One sentence describing the structure, then a fenced block:
```json
{ "field_name": "type and what it contains" }
```

### Example
```text
[realistic input — not placeholder text]
```
```text
[realistic output matching the schema above]
```

ABSOLUTE RULES:
- Every code block MUST use triple backticks with a language tag: ```json ```text ```python
- NEVER write a bare language name (json, text, python) on its own line without the opening backticks
- description frontmatter MUST be EXACTLY 2 sentences. Sentence 1 starts with "Use when". Sentence 2 lists concrete trigger phrases / file types / contexts ("Use this skill whenever the user mentions ... — even if they don't ..."). Single-sentence descriptions are REJECTED.
- name frontmatter MUST be kebab-case only (lowercase, hyphens, no spaces or underscores)
- The body MUST contain a literal "**Do not use when:**" subsection with at least two specific anti-patterns. This prevents false-positive triggering on adjacent tasks.
- Approach steps MUST be imperative directives — never "The skill will..." or "It scans..." Explain WHY a step matters when it's non-obvious; rote MUSTs without reasoning are weak.
- Example sections MUST contain realistic input/output, NEVER placeholder text like "sample input here" or "<example>".
- Return raw markdown only — no JSON wrapper, no outer code fence

EXAMPLE SKILL.md (for reference — match this quality level):
---
name: commit-message-generator
description: "Use when the user wants to generate a conventional commit message from staged changes or a diff. Use this skill whenever the user mentions commits, staging, git add, pull requests, merge requests, or says things like 'what should my commit message be' or 'help me write a commit' — even if they don't say 'generate'."
---

### Overview
Analyzes a git diff and produces a conventional commit message following the Conventional Commits specification. Takes unified diff output and returns a properly formatted commit message with type, scope, subject, and optional body.

### Input Format
Git diff output in unified diff format, typically 10-500 lines. Can be from `git diff --staged`, `git diff HEAD`, or pasted directly. No preprocessing needed — raw diff text is fine.

### When to Use
**Use when:**
- The user has staged changes and wants a commit message
- The user pastes a diff and asks for a commit message
- The user mentions "conventional commits" or "semantic commits"

**Do not use when:**
- The user wants to review code quality or get feedback on changes
- The user wants to revert or amend an existing commit
- The user is asking about git branching strategy or merge conflicts

### Approach
1. Parse the diff to identify changed files and the nature of each change (added, modified, deleted) — the `+` and `-` line prefixes tell you what was added vs removed
2. Classify the overall change type using the Conventional Commits spec: `feat` (new feature), `fix` (bug fix), `docs` (documentation), `style` (formatting), `refactor` (code restructuring), `test` (tests), `chore` (build/tooling), `perf` (performance)
3. Extract the primary scope from the most significant file path or module affected — e.g. `auth/login.py` suggests scope `auth`
4. Draft a concise subject line under 72 characters using the format: `type(scope): description` — the subject should be in imperative mood ("add" not "added")
5. Add a body paragraph when the change is non-trivial, explaining the motivation behind the change — the body wraps at 72 characters
6. Return the formatted commit message as plain text, ready to paste into `git commit -m`

### Output Format
Plain text commit message, optionally with a subject and body separated by a blank line:
```text
feat(auth): add input validation to login endpoint

Prevents malformed usernames from reaching the database query layer.
Validates against a regex pattern before processing.
```

### Example
```text
diff --git a/auth/login.py b/auth/login.py
index 1a2b3c4..5d6e7f8 100644
--- a/auth/login.py
+++ b/auth/login.py
@@ -15,6 +15,8 @@ def login(username, password):
     session = create_session(username)
+    if not validate_input(username):
+        raise ValueError('Invalid username')
     return session
```
```text
feat(auth): add input validation to login endpoint

Prevents malformed usernames from reaching the database query layer.
Validates against a regex pattern before processing.
```"""

    BUNDLE_HINT_TEMPLATE = """

When generating the body, the skill will ship with these bundled files in its folder:
{bundle_list}

In the Approach section, reference each bundled file by relative path with a one-line "when to read this" hint, exactly like the pdf skill does: e.g. "For [task], read `references/spec.md` and follow its instructions." or "Run `scripts/extract.py` to perform [deterministic step]." Do NOT inline the contents of bundled files into SKILL.md — that defeats progressive disclosure. The agent will load them on demand via `read_skill_file`."""

    # SKILL.md bodies are compact by design (progressive disclosure keeps bulk
    # content in bundled files, not inline), so the previous 8000-token cap was
    # far larger than any real draft needs. A tighter cap ends the completion
    # sooner and shaves latency; raise SKILL_CONTENT_MAX_TOKENS if a legitimate
    # skill ever gets truncated (the validation auto-fix path still catches it).
    MAX_CONTENT_TOKENS = int(os.environ.get("SKILL_CONTENT_MAX_TOKENS", "4000"))

    async def generate(
        self,
        blueprint: dict,
        bundle_files: Optional[list[dict]] = None,
        critique: Optional[list[str]] = None,
    ) -> str:
        """Generate SKILL.md content.

        ``bundle_files`` — when present, the body should reference them by path
        with a "when to read this" hint instead of inlining their content.

        ``critique`` — when present (regeneration pass from the quality loop),
        these issues are appended so the next draft fixes them.
        """
        system = self.SYSTEM + _guidance_block("writing the SKILL.md body")
        if bundle_files:
            listing = "\n".join(
                f"- `{f.get('rel_path', '')}` — {f.get('description', '').strip() or 'bundled resource'}"
                for f in bundle_files
            )
            system = system + self.BUNDLE_HINT_TEMPLATE.format(bundle_list=listing)

        user_content = f"Blueprint:\n{json.dumps(blueprint, indent=2)}"
        if critique:
            user_content += (
                "\n\nThe previous draft had these issues. Fix ALL of them in this draft:\n"
                + "\n".join(f"- {issue}" for issue in critique)
            )

        result = await _call_llm(system, [{"role": "user", "content": user_content}], max_tokens=self.MAX_CONTENT_TOKENS)
        result = result.strip()
        if result.startswith("```"):
            result = re.sub(r"```(?:markdown)?\s*", "", result).rstrip("` \n").strip()
        if not result.startswith("---"):
            name = blueprint.get("name", "custom_skill")
            description = blueprint.get("description", "Use when this skill is needed.")
            result = f'---\nname: {name}\ndescription: "{description}"\n---\n\n' + result

        # Fix any bare language identifiers the LLM dropped fences on
        result = _fix_code_fences(result)

        # Validate against skill-authoring spec; attempt one auto-fix if needed
        valid, msg = _validate_skill_md(result)
        if not valid:
            logger.warning(f'[AGENT] SkillContentGenerator: validation failed ({msg}) — attempting fix')
            fix_prompt = (
                f"The following SKILL.md has a validation error: {msg}\n\n"
                f"Fix the frontmatter so it complies with these rules:\n"
                f"- name: kebab-case, max 64 chars\n"
                f"- description: no angle brackets, max 1024 chars, starts with 'Use when'\n"
                f"- allowed frontmatter keys only: name, description, license, allowed-tools, metadata, compatibility\n\n"
                f"Return the complete corrected SKILL.md only.\n\n{result}"
            )
            fixed = await _call_llm(self.SYSTEM, [{"role": "user", "content": fix_prompt}], max_tokens=self.MAX_CONTENT_TOKENS)
            fixed = fixed.strip()
            if fixed.startswith("```"):
                fixed = re.sub(r"```(?:markdown)?\s*", "", fixed).rstrip("` \n").strip()
            fixed = _fix_code_fences(fixed)
            valid2, _ = _validate_skill_md(fixed)
            if valid2:
                result = fixed

        return result


# ---------------------------------------------------------------------------
# SkillBundleDecider — decides which scripts/references the skill should ship
# ---------------------------------------------------------------------------

# Cap how much bundled content one skill can emit so a runaway LLM call can't
# write a thousand-line script. The skill-authoring spec says SKILL.md should
# stay under ~500 lines; bundled files can be larger but still bounded.
_MAX_BUNDLE_FILES = 4
_MAX_BUNDLE_FILE_BYTES = 12_000   # ~250 lines of code/markdown per file
_ALLOWED_SCRIPT_EXTS = {".py", ".sh", ".js"}
_ALLOWED_REFERENCE_EXTS = {".md"}


def _safe_rel_path(rel_path: str, kind: str) -> Optional[str]:
    """Normalise & sanity-check a bundled-file relative path.

    Returns the cleaned path or ``None`` if it's unsafe / wrong extension /
    outside the allowed ``scripts/`` or ``references/`` prefix.
    """
    if not rel_path or not isinstance(rel_path, str):
        return None
    cleaned = rel_path.strip().lstrip("/").replace("\\", "/")
    if ".." in cleaned.split("/") or cleaned.startswith("/"):
        return None
    # Force one of the two known prefixes so we don't write SKILL.md, LICENSE,
    # etc. by accident.
    if kind == "script" and not cleaned.startswith("scripts/"):
        cleaned = f"scripts/{cleaned.split('/')[-1]}"
    if kind == "reference" and not cleaned.startswith("references/"):
        cleaned = f"references/{cleaned.split('/')[-1]}"
    ext = "." + cleaned.rsplit(".", 1)[-1].lower() if "." in cleaned else ""
    if kind == "script" and ext not in _ALLOWED_SCRIPT_EXTS:
        return None
    if kind == "reference" and ext not in _ALLOWED_REFERENCE_EXTS:
        return None
    return cleaned


class SkillBundleDecider:
    """Decides whether the skill needs bundled scripts or references, and
    generates them.

    The skill-authoring methodology says bundled resources exist because (a)
    deterministic work belongs in scripts the agent can execute without
    re-deriving it, and (b) long reference material bloats SKILL.md and
    breaks progressive disclosure. Most simple skills (e.g. a writing-style
    transformer) need zero bundled files — that's fine, we just return [].
    """

    SYSTEM = """You decide whether a skill needs bundled scripts/references, then write them.

A skill ships as a folder. SKILL.md is the entry point the agent always reads. Optional bundled files (loaded on demand via `read_skill_file`) belong in:
- `scripts/` — Python or shell scripts the agent can execute deterministically (parsing, transformation, formatting, library calls). Use when the task has a repeatable computation that's better as code than as prose instructions.
- `references/` — Markdown documentation the agent reads when it needs deeper detail (schemas, glossaries, edge-case rules, longer examples). Use when SKILL.md would otherwise grow past ~400 lines.

DO NOT bundle anything if the skill is purely about how to phrase or transform text in a way the LLM does natively (e.g. summarization, tone change, classification). Returning zero files is the right answer for those.

DO bundle when the skill involves: parsing structured formats (CSV, JSON, XML, ICS), formatted output generation that has a single correct algorithm, calling a specific library where copy-pasteable code beats English instructions, lookups against a fixed table/schema.

Return JSON only (no markdown, no commentary):
{
  "files": [
    {
      "rel_path": "scripts/<short-name>.py",
      "kind": "script",
      "description": "one-line purpose — what the agent gets by running/reading this",
      "content": "<the full file contents — runnable code with a docstring, NO placeholder TODOs>"
    }
  ]
}

Rules:
- 0 to 3 files maximum
- Scripts must be self-contained — runnable as `python scripts/foo.py <args>` with arguments documented in a docstring at the top
- References must be markdown with a clear H1 title and table of contents if >150 lines
- Never produce placeholder content ("# TODO: implement", "# Your code here") — if you can't write the real thing, omit the file
- Keep each file under 10KB"""

    async def decide(self, blueprint: dict) -> list[dict]:
        prompt = (
            "Skill blueprint:\n"
            f"{json.dumps(blueprint, indent=2)}\n\n"
            "Decide which bundled files (if any) this skill needs and emit them."
        )
        try:
            raw = await _call_llm(self.SYSTEM, [{"role": "user", "content": prompt}], max_tokens=4096)
        except Exception as exc:
            logger.warning(f'[AGENT] SkillBundleDecider: LLM call failed ({exc}) — skipping bundle')
            return []
        parsed = _parse_json(raw) or {}
        raw_files = parsed.get("files", []) or []

        out: list[dict] = []
        for f in raw_files[:_MAX_BUNDLE_FILES]:
            if not isinstance(f, dict):
                continue
            kind = (f.get("kind") or "").strip().lower()
            if kind not in ("script", "reference"):
                # Infer from path if the LLM omitted kind
                rp = (f.get("rel_path") or "").lower()
                if rp.startswith("scripts/"):
                    kind = "script"
                elif rp.startswith("references/"):
                    kind = "reference"
                else:
                    continue
            safe = _safe_rel_path(f.get("rel_path", ""), kind)
            if not safe:
                continue
            content = f.get("content") or ""
            if not content.strip():
                continue
            # Reject obvious placeholder-only files. The size guard applies to
            # all markers — a 200-byte file with any of these is almost
            # certainly a stub the LLM gave up on, while a real script that
            # happens to mention "placeholder" in a docstring is fine.
            low = content.lower()
            has_marker = any(m in low for m in ("todo: implement", "your code here", "placeholder"))
            if has_marker and len(content) < 400:
                logger.info(f'[AGENT] SkillBundleDecider: dropping placeholder file {safe}')
                continue
            if len(content.encode("utf-8")) > _MAX_BUNDLE_FILE_BYTES:
                content = content[:_MAX_BUNDLE_FILE_BYTES]
            out.append({
                "rel_path": safe,
                "kind": kind,
                "description": (f.get("description") or "").strip()[:200],
                "content": content,
                "size_bytes": len(content.encode("utf-8")),
                "abs_path": "",  # populated only when sourced from disk; AI-generated stays empty
            })
        return out


# ---------------------------------------------------------------------------
# SkillCritiqueAgent — structural/qualitative review of a generated SKILL.md
# ---------------------------------------------------------------------------

class SkillCritiqueAgent:
    """Reviews a SKILL.md against the skill-authoring quality axes.

    Distinct from ``SkillEvaluator``, which only tests trigger accuracy.
    This one checks structural and qualitative properties: imperative
    approach steps, realistic examples, "Do not use when" coverage, output
    format clarity. Returns a 0-100 score + actionable issue list.
    """

    SYSTEM = """You are a strict reviewer of skill documentation (SKILL.md files) written for the AiNxt skill-authoring spec.

Score the SKILL.md on five axes, each 0-20:

1. **Trigger specificity (0-20)** — Is the description concrete enough that an agent knows when to load this skill? Vague descriptions ("when needed", "for data tasks") score low. Specific triggers with named contexts/file types score high.

2. **Imperative approach (0-20)** — Are the steps written as directives to the model ("Extract X", "Identify Y by Z rule") or as narration ("The skill will scan...", "It then determines...")? Narration is bad. Also: do the steps explain WHY when non-obvious, or are they just rote MUSTs?

3. **Realistic example (0-20)** — Does the Example section contain a real, specific, detailed example, or placeholder text ("sample input", "<example>", "your data here")?

4. **Output format clarity (0-20)** — Is the output structure documented unambiguously (schema, field names, types, fenced code block)? Or is it vague ("returns a summary")?

5. **False-positive guard (0-20)** — Is there a "Do not use when:" section listing adjacent tasks this skill should refuse? Without it, the skill will trigger on unrelated requests.

Return JSON only:
{
  "score": <sum of axes, 0-100>,
  "axes": {"trigger": <0-20>, "imperative": <0-20>, "example": <0-20>, "output": <0-20>, "guard": <0-20>},
  "issues": ["<concrete actionable issue 1>", "<concrete actionable issue 2>"],
  "passed": <true if score >= 80 AND no axis is below 12>
}

Be strict — false negatives (passing weak skills) waste user time more than false positives."""

    async def critique(self, content: str) -> dict:
        try:
            raw = await _call_llm(
                self.SYSTEM,
                [{"role": "user", "content": f"SKILL.md to review:\n\n{content}"}],
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning(f'[AGENT] SkillCritiqueAgent: {exc}')
            return {"score": 0, "issues": [f"Critique LLM call failed: {exc}"], "passed": False, "error": True}
        parsed = _parse_json(raw) or {}
        score = int(parsed.get("score") or 0)
        return {
            "score": score,
            "axes": parsed.get("axes") or {},
            "issues": parsed.get("issues") or [],
            "passed": bool(parsed.get("passed", score >= 80)),
        }


# ---------------------------------------------------------------------------
# SkillQualityLoop — orchestrates evaluate → critique → regenerate
# ---------------------------------------------------------------------------

# Cost-aware loop tuning. Worst case used to be ~30 LLM calls (eval is ~9 per
# draft, critique is 1, plus up to 2 regens). We short-circuit aggressively
# below: cheap lint runs first and gates the LLM critique; the critique gates
# the expensive evaluator. Typical case for a well-formed first draft is
# ~10 LLM calls; degenerate prompts hit ~18.
_SKILL_LOOP_MAX_ITERATIONS = int(os.environ.get("SKILL_QUALITY_MAX_ITERS", "2"))


class SkillQualityLoop:
    """Iterate-and-improve loop. Composes lint + critique + evaluator and
    regenerates the draft against accumulated issues until thresholds clear
    or the regeneration budget is exhausted.

    Tier ordering matters for cost:
      1. ``_lint_skill_md`` (free, deterministic) — covers the obvious stuff
         like missing "Do not use when" and single-sentence descriptions.
      2. ``SkillCritiqueAgent`` (1 LLM call) — structural quality axes.
      3. ``SkillEvaluator`` (~9 LLM calls) — only when the first two pass,
         because trigger accuracy is meaningless on a malformed skill.
    """

    TRIGGER_THRESHOLD = 80
    CRITIQUE_THRESHOLD = 80

    def __init__(self, max_iterations: Optional[int] = None):
        # 0 = score initial draft only, never regenerate.
        # 1 = up to one regeneration. 2 = up to two. etc.
        self.max_iterations = (
            _SKILL_LOOP_MAX_ITERATIONS if max_iterations is None else max_iterations
        )
        self.evaluator = SkillEvaluator()
        self.critic = SkillCritiqueAgent()
        self.generator = SkillContentGenerator()

    async def _score(self, content: str, name: str) -> dict:
        """Score one draft. Returns
        ``{trigger, critique, lint_issues, issues, passed}``.

        Skips the expensive evaluator when lint or critique already flag
        problems — there's no point spending 9 LLM calls measuring trigger
        accuracy on a draft we already know we're going to regenerate.
        """
        lint_issues = _lint_skill_md(content)
        critique_result = await self.critic.critique(content)
        if isinstance(critique_result, dict) and critique_result.get("error"):
            critique_score = 0
            critique_issues = critique_result.get("issues") or []
        else:
            critique_score = int((critique_result or {}).get("score") or 0)
            critique_issues = (critique_result or {}).get("issues") or []

        cheap_problems = bool(lint_issues) or critique_score < self.CRITIQUE_THRESHOLD
        if cheap_problems:
            return {
                "trigger": None,  # not measured this round
                "critique": critique_score,
                "lint_issues": len(lint_issues),
                "issues": _dedupe(lint_issues + critique_issues),
                "passed": False,
            }

        eval_result = await self.evaluator.evaluate(content, name)
        if isinstance(eval_result, dict) and eval_result.get("error"):
            trigger_score = 0
            trigger_issues = [eval_result["error"]]
        else:
            trigger_score = int((eval_result or {}).get("score") or 0)
            trigger_issues = (eval_result or {}).get("feedback") or []

        return {
            "trigger": trigger_score,
            "critique": critique_score,
            "lint_issues": 0,
            "issues": _dedupe(trigger_issues + critique_issues),
            "passed": (
                trigger_score >= self.TRIGGER_THRESHOLD
                and critique_score >= self.CRITIQUE_THRESHOLD
            ),
        }

    async def run(
        self,
        blueprint: dict,
        initial_content: str,
        bundle_files: Optional[list[dict]] = None,
        progress_cb=None,
    ) -> tuple[str, dict]:
        """Returns ``(best_content, summary)``.

        ``summary`` exposes the best draft's scores plus a per-iteration log
        so the UI can show the trajectory.
        """
        content = initial_content
        name = blueprint.get("name", "custom_skill")
        log: list[dict] = []
        best_content = content
        best_combined = -1.0
        best_summary: dict = {}

        async def _emit(msg: str):
            if progress_cb is not None:
                try:
                    await progress_cb(msg)
                except Exception:
                    pass  # progress reporting must never break the loop

        # Iterate (max_iterations + 1) times: initial draft + up to N regens.
        for i in range(self.max_iterations + 1):
            iter_label = i + 1
            await _emit(f"Iteration {iter_label}: scoring draft…")

            scored = await self._score(content, name)
            trigger = scored["trigger"]
            critique = scored["critique"]

            log.append({
                "iter": iter_label,
                "trigger": trigger,
                "critique": critique,
                "lint_issues": scored["lint_issues"],
                "issues": scored["issues"][:8],  # cap SSE payload
            })

            # Combined ranking treats an un-evaluated draft as a 50-point
            # placeholder so a draft that *did* pass through evaluation
            # always wins ties — we don't want to return an unmeasured one.
            combined = (trigger if trigger is not None else 50) + critique
            if combined > best_combined:
                best_combined = combined
                best_content = content
                best_summary = {
                    "trigger_score": trigger if trigger is not None else 0,
                    "critique_score": critique,
                    "lint_issues": scored["lint_issues"],
                }

            score_line = (
                f"trigger {trigger}/100 · structure {critique}/100"
                if trigger is not None
                else f"structure {critique}/100 (trigger eval skipped — structural issues found)"
            )
            await _emit(f"Iteration {iter_label}: {score_line}")

            if scored["passed"] or not scored["issues"]:
                break
            if i == self.max_iterations:
                break  # budget exhausted; the final draft we have is the best we'll get

            await _emit(
                f"Iteration {iter_label + 1}: regenerating with {len(scored['issues'])} issue(s) to fix…"
            )
            try:
                content = await self.generator.generate(
                    blueprint, bundle_files=bundle_files, critique=scored["issues"]
                )
            except Exception as exc:
                logger.warning(f'[AGENT] SkillQualityLoop: regenerate failed ({exc}) — keeping best draft')
                break

        summary = {
            "iterations": len(log),
            "passed": (
                best_summary.get("trigger_score", 0) >= self.TRIGGER_THRESHOLD
                and best_summary.get("critique_score", 0) >= self.CRITIQUE_THRESHOLD
            ),
            "log": log,
            **best_summary,
        }
        return best_content, summary


def _dedupe(items: list[str]) -> list[str]:
    """Order-preserving dedupe — kept as a helper so call sites don't reinvent
    it with the side-effect-set trick that's easy to misread.
    """
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# SkillAssembler
# ---------------------------------------------------------------------------

# Per-file cap for inlining bundled-file content into the assembled manifest
# that streams to the creation UI. Bundled files are already capped at
# _MAX_BUNDLE_FILE_BYTES (12KB) by the decider, so this only trims the rare
# hand-uploaded/edited outlier; the full content always persists on confirm.
_MANIFEST_CONTENT_MAX_BYTES = 16_000


class SkillAssembler:
    def assemble(
        self,
        blueprint: dict,
        content: str,
        bundle_files: Optional[list[dict]] = None,
        quality: Optional[dict] = None,
    ) -> dict:
        # Manifest carries the file content too so the creation UI can preview
        # (and let the user edit) generated scripts/references before saving.
        # Guarded by _MANIFEST_CONTENT_MAX_BYTES so a large reference doc can't
        # bloat the SSE frame — oversized files fall back to content="" and the
        # UI shows the chip only (full content is still saved from
        # session.bundle_files on confirm).
        file_manifest = []
        for f in (bundle_files or []):
            content_str = f.get("content") or ""
            included = content_str if len(content_str.encode("utf-8")) <= _MANIFEST_CONTENT_MAX_BYTES else ""
            file_manifest.append({
                "rel_path": f.get("rel_path", ""),
                "kind": f.get("kind", "reference"),
                "size_bytes": int(f.get("size_bytes", len(content_str))),
                "description": f.get("description", ""),
                "content": included,
            })
        return {
            "name": blueprint.get("name", "custom_skill"),
            "display_name": blueprint.get("display_name", blueprint.get("name", "Custom Skill")),
            "description": blueprint.get("description", ""),
            "category": blueprint.get("category", "general"),
            "content": content,
            "generated": True,
            "tags": blueprint.get("triggers", [])[:5],
            "bundle_files": file_manifest,        # for the UI
            "quality": quality or {},             # {trigger_score, critique_score, iterations, log}
        }
