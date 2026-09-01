# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt COACH — rule evaluator + exponential-decay practice scoring
# ============================================================
#
# The brain of the Coach feature. Given a normalised coach_event dict it:
#
#   • runs every enabled, un-muted predicate in BASELINE_RULES,
#   • persists a coach_rule_hit row (with evidence) for each match,
#   • back-fills coach_event.rule_hits with the rule ids that fired,
#   • exposes compute_scores() — per-category + overall practice scores via
#     an exponential decay of accumulated severity-weighted penalties.
#
# Design constraints (AINXT_COACH_REQUIREMENTS.md):
#   • NO eval/exec — every rule is a plain Python predicate function. The rule
#     "DSL" is just a registry of callables. Deterministic and auditable.
#   • Explainability — every hit stores the field values that triggered it.
#   • Redact-at-write upheld — predicates inspect already-redacted text /
#     flag arrays only; they never receive raw PAN/PII/secret.
#   • Department-scoped, RBAC-gated reads happen in the routers, not here.
#
# Scoring math:
#     penalty(category) = Σ  severity_weight(hit) over hits in window
#     score(category)   = 100 * exp(-penalty / SCORE_DECAY_K)
#   gated by MIN_EVENTS_FOR_SCORE (too few events → score is None/“n/a”).
# ============================================================

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from core.logger import logger

try:
    from core.config import (
        COACH_MIN_EVENTS_FOR_SCORE,
        COACH_SCORE_DECAY_K,
        COACH_EVAL_PENALTY_WEIGHT,
        PLATFORM_BASE_URL,
    )
except Exception:  # pragma: no cover
    import os
    COACH_MIN_EVENTS_FOR_SCORE  = int(os.getenv("COACH_MIN_EVENTS_FOR_SCORE", "5"))
    COACH_SCORE_DECAY_K         = float(os.getenv("COACH_SCORE_DECAY_K", "60.0"))
    COACH_EVAL_PENALTY_WEIGHT   = float(os.getenv("COACH_EVAL_PENALTY_WEIGHT", "3.0"))
    # No localhost default: coach_portal_url() already treats an empty value
    # as "unset" via `(PLATFORM_BASE_URL or "")`.
    PLATFORM_BASE_URL           = os.getenv("PLATFORM_BASE_URL", "")


# ── categories & severity ───────────────────────────────────────────────────

CATEGORY_PROMPT   = "prompt-quality"
CATEGORY_SESSION  = "session-hygiene"
CATEGORY_REVIEW   = "review-discipline"
CATEGORY_TOOL     = "tool-mastery"
CATEGORY_CONTEXT  = "context-management"
CATEGORY_SECURITY = "security"

ALL_CATEGORIES = [
    CATEGORY_PROMPT, CATEGORY_SESSION, CATEGORY_REVIEW,
    CATEGORY_TOOL, CATEGORY_CONTEXT, CATEGORY_SECURITY,
]

_SEVERITY_WEIGHT = {
    "low":      1.0,
    "medium":   1.5,
    "high":     2.5,
    "critical": 4.0,
}


# ── rule definition ─────────────────────────────────────────────────────────

class Rule:
    """A single coaching rule: id + metadata + a predicate.

    predicate(event, ctx) -> Optional[dict]:
        returns None when the rule does NOT fire, or an `evidence` dict (the
        field values that triggered it) when it DOES. The evidence dict is
        persisted on the hit for drill-down explainability.
    """
    __slots__ = ("rule_id", "code", "category", "severity", "title", "advice", "predicate")

    def __init__(self, rule_id: str, code: str, category: str, severity: str,
                 title: str, advice: str,
                 predicate: Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]):
        self.rule_id = rule_id
        self.code = code
        self.category = category
        self.severity = severity
        self.title = title
        self.advice = advice
        self.predicate = predicate

    def to_meta(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "advice": self.advice,
        }


def _rule(rule_id, code, category, severity, title, advice):
    """Decorator factory registering a predicate as a Rule."""
    def _wrap(fn):
        return Rule(rule_id, code, category, severity, title, advice, fn)
    return _wrap


# ── small text helpers (operate on REDACTED text only) ──────────────────────

_VAGUE_TOKENS = {
    "fix", "fix it", "help", "improve", "make it better", "do it", "this",
    "that", "stuff", "thing", "update", "change", "redo", "again",
}
_INFO_QUESTION_PREFIXES = (
    "what is", "what are", "who is", "who are", "how to", "how do", "how does",
    "why ", "explain", "define", "tell me about", "difference between",
)
_PRONOUN_TOKENS = {"it", "this", "that", "these", "those", "them", "they"}
_CONSTRAINT_HINTS = ("must", "should", "constraint", "limit", "within", "only",
                     "exactly", "at most", "at least", "no more than", "ensure")
_ACCEPTANCE_HINTS = ("acceptance", "given ", "when ", "then ", "criteria",
                     "expected", "test ", "verify", "should return", "assert")
_SUCCESS_HINTS = ("success", "done when", "complete when", "definition of done",
                  "pass when", "works when")
_BUILD_VERBS = ("write", "create", "build", "implement", "develop", "make",
                "generate", "code", "add", "fix", "refactor", "design", "validate")


def _ptext(event: Dict[str, Any]) -> str:
    return (event.get("prompt_redacted") or "").strip()


def _words(text: str) -> List[str]:
    return [w for w in text.replace("\n", " ").split(" ") if w]


def _has_hint(text: str, hints) -> bool:
    """True if any hint appears in `text`. Single-word hints are matched on word
    boundaries (so "limit" does NOT match inside "limiter"); multi-word hints
    (containing a space) fall back to plain substring matching."""
    for h in hints:
        if " " in h:
            if h in text:
                return True
        elif re.search(r"\b" + re.escape(h) + r"\b", text):
            return True
    return False


# ── predicate library (~24 rules across 6 categories) ───────────────────────
#
# ctx carries cross-event aggregates the ingestor/consumer pre-computes when
# available: recent_prompt_hashes (list), thread_msg_count (int),
# continue_count (int), seconds_since_thread_start (int),
# recent_acceptance_rate (float), tool_retry_count (int),
# recent_channels (set), kb_hit (bool). All optional — predicates degrade
# gracefully when a key is absent.

# ---- prompt-quality ----

@_rule("prompt.vague", "AINXT-PQ-001", CATEGORY_PROMPT, "medium",
       "Vague prompt", "Add a concrete goal, the target file/function, and what 'done' looks like.")
def _r_vague(event, ctx):
    t = _ptext(event).lower()
    if not t:
        return None
    words = _words(t)
    word_count = len(words)
    if t.rstrip().endswith("?") and t.startswith(_INFO_QUESTION_PREFIXES):
        return None
    if t in _VAGUE_TOKENS:
        return {"prompt_len_words": word_count, "text_sample": t[:80]}
    if word_count <= 2 and any(v == t or f" {v} " in f" {t} " for v in _VAGUE_TOKENS):
        return {"prompt_len_words": word_count, "text_sample": t[:80]}
    return None


@_rule("prompt.missing_acceptance", "AINXT-PQ-002", CATEGORY_PROMPT, "low",
       "No acceptance criteria", "State the expected output or acceptance criteria so the model can self-check.")
def _r_missing_acceptance(event, ctx):
    t = _ptext(event).lower()
    if len(_words(t)) < 8:
        return None  # too short to expect criteria; covered by vague rule
    if not any(h in t for h in _ACCEPTANCE_HINTS):
        return {"text_sample": t[:120]}
    return None


@_rule("prompt.ambiguous_pronoun", "AINXT-PQ-003", CATEGORY_PROMPT, "low",
       "Ambiguous pronouns", "Replace 'it/this/that' with the explicit file, symbol, or value you mean.")
def _r_ambiguous_pronoun(event, ctx):
    words = [w.strip(".,:;!?").lower() for w in _words(_ptext(event))]
    if not words:
        return None
    pron = [w for w in words if w in _PRONOUN_TOKENS]
    # Fires when the prompt is short and pronoun-heavy (no antecedent context).
    if words and len(words) <= 12 and len(pron) >= 2:
        return {"pronoun_count": len(pron), "word_count": len(words)}
    return None


@_rule("prompt.multi_intent", "AINXT-PQ-004", CATEGORY_PROMPT, "low",
       "Multiple intents in one prompt", "Split unrelated asks into separate turns so each gets full attention.")
def _r_multi_intent(event, ctx):
    t = _ptext(event).lower()
    if not t:
        return None
    # crude multi-intent heuristic: many 'and also' / numbered list / multiple '?'
    connectors = t.count(" and also ") + t.count(" then also ") + t.count("; also")
    questions = t.count("?")
    if connectors >= 2 or questions >= 3:
        return {"connectors": connectors, "questions": questions}
    return None


# Language / framework / version keywords that count as a constraint being
# present.  A prompt that mentions any of these is considered constrained.
# All entries are matched on word boundaries (via _has_lang_hint) so short
# tokens like "r" or "go" don't false-positive inside longer words.
_LANG_FRAMEWORK_HINTS = (
    "python", "java", "javascript", "typescript", "golang", "rust",
    "c++", "c#", "kotlin", "swift", "ruby", "php", "scala",
    "react", "vue", "angular", "django", "flask", "fastapi", "spring",
    "nodejs", "express", "rails", "laravel", "dotnet",
    "postgres", "mysql", "mongodb", "redis", "kafka",
    "jdk", "jre", "gradle", "maven", "docker", "kubernetes",
    # Short tokens matched with word boundaries only
    "go", "r", "sql", "pip", "npm", "aws", "gcp",
    # Version markers
    "version", "v1", "v2", "v3", "azure",
)

@_rule("prompt.missing_constraints", "AINXT-PQ-005", CATEGORY_PROMPT, "low",
       "No constraints given", "Specify the language, framework, or version so the model targets the right environment.")
def _r_missing_constraints(event, ctx):
    t = _ptext(event).lower()
    words = _words(t)
    word_count = len(words)
    # Skip truly tiny prompts (≤3 words) — already caught by the vague rule.
    if word_count <= 3:
        return None
    # Skip if the prompt is a question (likely exploratory, not a build task).
    if t.rstrip().endswith("?"):
        return None
    # Skip if the prompt already names a language/framework/version.
    # Use word-boundary matching so "go" doesn't match inside "going",
    # "r" doesn't match inside "for", etc.
    if _has_hint(t, _LANG_FRAMEWORK_HINTS):
        return None
    # Skip if the prompt contains general constraint keywords.
    # Word-boundary match so "limiter" / "rate limiting" don't satisfy "limit".
    if _has_hint(t, _CONSTRAINT_HINTS):
        return None
    # Only fire for prompts that look like build/write/create/implement tasks
    # (imperative verbs) — avoids false positives on conversational prompts.
    if not any(t.startswith(v) or f" {v} " in t for v in _BUILD_VERBS):
        return None
    return {"text_sample": t[:120], "word_count": word_count}


@_rule("prompt.no_success_def", "AINXT-PQ-006", CATEGORY_PROMPT, "low",
       "No definition of done", "Tell the model when the task is complete so it stops at the right point.")
def _r_no_success_def(event, ctx):
    t = _ptext(event).lower()
    words = _words(t)
    word_count = len(words)
    if word_count < 12 or t.rstrip().endswith("?"):
        return None
    if not any(t.startswith(v) or f" {v} " in t for v in _BUILD_VERBS):
        return None
    if any(h in t for h in _SUCCESS_HINTS):
        return None
    return {"text_sample": t[:120], "word_count": word_count}


# ---- session-hygiene ----

@_rule("session.thread_too_long", "AINXT-SH-001", CATEGORY_SESSION, "medium",
       "Thread getting long", "Long threads dilute context. Summarise and start a fresh thread for the next task.")
def _r_thread_too_long(event, ctx):
    n = int(ctx.get("thread_msg_count") or 0)
    if n >= 40:
        return {"thread_msg_count": n}
    return None


@_rule("session.excess_continue", "AINXT-SH-002", CATEGORY_SESSION, "low",
       "Too many 'continue' nudges", "Frequent 'continue' suggests under-specified prompts; give complete instructions.")
def _r_excess_continue(event, ctx):
    c = int(ctx.get("continue_count") or 0)
    t = _ptext(event).lower().strip()
    is_continue = t in {"continue", "go on", "next", "keep going", "more"}
    if is_continue and c >= 4:
        return {"continue_count": c}
    return None


@_rule("session.stale_resume", "AINXT-SH-003", CATEGORY_SESSION, "low",
       "Resuming a stale thread", "Resuming a thread after a long gap loses context — restate the goal briefly.")
def _r_stale_resume(event, ctx):
    gap = int(ctx.get("seconds_since_thread_start") or 0)
    n = int(ctx.get("thread_msg_count") or 0)
    if gap >= 6 * 3600 and n >= 5:
        return {"gap_hours": round(gap / 3600, 1), "thread_msg_count": n}
    return None


# ---- review-discipline ----

@_rule("review.low_acceptance", "AINXT-RD-001", CATEGORY_REVIEW, "medium",
       "Low suggestion-acceptance rate", "Most suggestions are being rejected — refine prompts before generating.")
def _r_low_acceptance(event, ctx):
    rate = ctx.get("recent_acceptance_rate")
    samples = int(ctx.get("recent_acceptance_samples") or 0)
    if rate is None or samples < 5:
        return None
    if float(rate) < 0.25:
        return {"acceptance_rate": round(float(rate), 3), "samples": samples}
    return None


@_rule("review.unreviewed_apply", "AINXT-RD-002", CATEGORY_REVIEW, "low",
       "Applied without review", "Output was applied with near-zero dwell time — review diffs before applying.")
def _r_unreviewed_apply(event, ctx):
    if event.get("accepted") is True:
        dwell = int(ctx.get("review_dwell_ms") or -1)
        if 0 <= dwell < 1500:
            return {"review_dwell_ms": dwell}
    return None


# ---- tool-mastery ----

@_rule("tool.premium_for_trivial", "AINXT-TM-001", CATEGORY_TOOL, "medium",
       "Premium model for a trivial prompt",
       "This prompt is trivial — a faster/cheaper model would do. Reserve premium models for complex work.")
def _r_premium_for_trivial(event, ctx):
    model = (event.get("model") or "")
    if not model:
        return None
    try:
        from core.model_registry import MODEL_COST_PER_1M
        cost = MODEL_COST_PER_1M.get(model)
    except Exception:
        cost = None
    # "premium" = output price >= $10 / 1M tokens.
    is_premium = bool(cost) and cost[1] >= 10.0
    if not is_premium:
        return None
    try:
        from models.classifier import classify_with_confidence
        label, conf = classify_with_confidence(_ptext(event) or "")
    except Exception:
        label, conf = ("medium", 0.5)
    if label == "simple" and conf >= 0.85:
        return {"model": model, "complexity": label, "confidence": round(conf, 2),
                "out_cost_per_1m": cost[1]}
    return None


@_rule("tool.retry_storm", "AINXT-TM-002", CATEGORY_TOOL, "medium",
       "Tool-call retry storm", "Repeated failing tool calls — inspect the error and adjust before retrying.")
def _r_retry_storm(event, ctx):
    retries = int(ctx.get("tool_retry_count") or 0)
    if retries >= 5:
        return {"tool_retry_count": retries}
    return None


@_rule("tool.unused_tools", "AINXT-TM-003", CATEGORY_TOOL, "low",
       "Manual work the tools could do", "A relevant tool/skill exists for this — invoking it is faster and safer.")
def _r_unused_tools(event, ctx):
    if ctx.get("suggested_tool") and not (event.get("tool_calls")):
        return {"suggested_tool": ctx.get("suggested_tool")}
    return None


# ---- context-management ----

@_rule("context.saturated", "AINXT-CM-001", CATEGORY_CONTEXT, "high",
       "Context window saturated", "You're near the context limit — summarise or prune before adding more.")
def _r_context_saturated(event, ctx):
    pct = float(event.get("context_window_pct") or 0.0)
    if pct >= 90.0:
        return {"context_window_pct": pct}
    return None


@_rule("context.cross_channel", "AINXT-CM-002", CATEGORY_CONTEXT, "low",
       "Same task across many channels", "Switching channels mid-task fragments context — keep a task in one place.")
def _r_cross_channel(event, ctx):
    # Fire only when this exact prompt hash was seen on another channel.
    # The old implementation fired whenever the user's recent history had 3+
    # channels, which incorrectly flagged unrelated new prompts like
    # "what is java 8 features" after the user had used Web + IDE + CLI earlier.
    h = event.get("prompt_hash")
    current_channel = event.get("channel")
    if not h or not current_channel:
        return None
    by_hash = ctx.get("recent_channels_by_prompt_hash") or {}
    recent_channels = set(by_hash.get(h) or [])
    other_channels = sorted(c for c in recent_channels if c and c != current_channel)
    if other_channels:
        return {"channels": sorted(set([current_channel] + other_channels)), "prompt_hash": h}
    return None


@_rule("context.kb_miss", "AINXT-CM-003", CATEGORY_CONTEXT, "low",
       "Retrieval returned nothing", "The knowledge base had no match — rephrase or add the doc to the KB.")
def _r_kb_miss(event, ctx):
    if ctx.get("kb_hit") is False and ctx.get("kb_attempted") is True:
        return {"kb_hit": False}
    return None


@_rule("context.duplicate_prompt", "AINXT-CM-004", CATEGORY_CONTEXT, "low",
       "Duplicate prompt", "You've asked this exact prompt before — reuse the prior answer or refine it.")
def _r_duplicate_prompt(event, ctx):
    h = event.get("prompt_hash")
    recent = ctx.get("recent_prompt_hashes") or []
    if h and h in recent:
        return {"prompt_hash": h}
    return None


# ---- security ----

@_rule("security.pii_in_prompt", "AINXT-SEC-001", CATEGORY_SECURITY, "high",
       "PII detected in prompt", "PII was found and redacted — avoid pasting personal data into prompts.")
def _r_pii(event, ctx):
    flags = event.get("pii_flags") or []
    if flags:
        return {"pii_types": list(flags)}
    return None


@_rule("security.secret_in_prompt", "AINXT-SEC-002", CATEGORY_SECURITY, "critical",
       "Secret/credential in prompt", "A secret was detected and redacted — never paste keys/tokens; use the vault.")
def _r_secret(event, ctx):
    flags = event.get("secret_flags") or []
    if flags:
        return {"secret_types": list(flags)}
    return None


@_rule("security.compliance_block", "AINXT-SEC-003", CATEGORY_SECURITY, "critical",
       "Compliance-blocked content", "Content tripped a PCI/compliance block — review handling of sensitive data.")
def _r_compliance(event, ctx):
    flags = event.get("compliance_flags") or []
    if flags:
        return {"compliance_types": list(flags)}
    return None


@_rule("security.governance_flag", "AINXT-SEC-004", CATEGORY_SECURITY, "medium",
       "Governance flag raised", "A governance policy flag was raised on this interaction — review the policy.")
def _r_governance(event, ctx):
    flags = event.get("governance_flags") or []
    if flags:
        return {"governance_types": list(flags)}
    return None


_SENSITIVE_KEYWORDS = ("password", "secret", "private key", "credential",
                       "api key", "token", "ssh key", "bypass auth", "disable auth")


@_rule("security.sensitive_keyword", "AINXT-SEC-005", CATEGORY_SECURITY, "medium",
       "Sensitive keyword in prompt", "Sensitive-topic keywords detected — ensure you're following secure-handling policy.")
def _r_sensitive_keyword(event, ctx):
    t = _ptext(event).lower()
    if not t:
        return None
    hits = [k for k in _SENSITIVE_KEYWORDS if k in t]
    # Only fire when no flag already covered it (avoid double-penalising).
    if hits and not (event.get("secret_flags") or event.get("compliance_flags")):
        return {"keywords": hits}
    return None


# ── registry ────────────────────────────────────────────────────────────────

BASELINE_RULES: List[Rule] = [
    _r_vague, _r_missing_acceptance, _r_ambiguous_pronoun, _r_multi_intent,
    _r_missing_constraints, _r_no_success_def,
    _r_thread_too_long, _r_excess_continue, _r_stale_resume,
    _r_low_acceptance, _r_unreviewed_apply,
    _r_premium_for_trivial, _r_retry_storm, _r_unused_tools,
    _r_context_saturated, _r_cross_channel, _r_kb_miss, _r_duplicate_prompt,
    _r_pii, _r_secret, _r_compliance, _r_governance, _r_sensitive_keyword,
]

RULES_BY_ID: Dict[str, Rule] = {r.rule_id: r for r in BASELINE_RULES}


def rule_catalog() -> List[Dict[str, Any]]:
    """Public metadata for every baseline rule (for the /rules endpoint)."""
    return [r.to_meta() for r in BASELINE_RULES]


# ── mute / disable lookups ──────────────────────────────────────────────────

def _muted_rule_ids(user_id: str, db=None) -> set:
    """Rule ids the user has actively muted (not expired)."""
    own_db = False
    if db is None:
        from db.database import SessionLocal
        db = SessionLocal()
        own_db = True
    muted = set()
    try:
        from db.models import CoachRuleMute
        now = datetime.now(timezone.utc)
        rows = db.query(CoachRuleMute).filter(CoachRuleMute.user_id == user_id).all()
        for r in rows:
            if r.muted_until is None:
                muted.add(r.rule_id)
            else:
                mu = r.muted_until
                if mu.tzinfo is None:
                    mu = mu.replace(tzinfo=timezone.utc)
                if mu > now:
                    muted.add(r.rule_id)
    except Exception as e:
        logger.warning(f"coach.evaluator: mute lookup failed ({e.__class__.__name__})")
    finally:
        if own_db:
            db.close()
    return muted


def _disabled_rule_ids(department: Optional[str], db=None) -> set:
    """Rule ids disabled org-wide (department NULL) or for this department."""
    own_db = False
    if db is None:
        from db.database import SessionLocal
        db = SessionLocal()
        own_db = True
    disabled = set()
    try:
        from db.models import CoachRuleDisabled
        rows = db.query(CoachRuleDisabled).all()
        for r in rows:
            if r.department is None or (department and r.department == department):
                disabled.add(r.rule_id)
    except Exception as e:
        logger.warning(f"coach.evaluator: disabled lookup failed ({e.__class__.__name__})")
    finally:
        if own_db:
            db.close()
    return disabled


# ── evaluation ──────────────────────────────────────────────────────────────

def _run_rules(event: Dict[str, Any], ctx: Dict[str, Any],
               skip: Optional[set] = None) -> List[Dict[str, Any]]:
    """Run every rule not in `skip`; return a list of hit dicts."""
    skip = skip or set()
    hits: List[Dict[str, Any]] = []
    for rule in BASELINE_RULES:
        if rule.rule_id in skip:
            continue
        try:
            evidence = rule.predicate(event, ctx)
        except Exception as e:
            logger.warning(f"coach.evaluator: predicate {rule.rule_id} raised ({e.__class__.__name__})")
            evidence = None
        if evidence:
            hits.append({
                "rule_id": rule.rule_id,
                "category": rule.category,
                "severity": rule.severity,
                "title": rule.title,
                "advice": rule.advice,
                "evidence": evidence,
            })
    return hits


def evaluate_event(event_id: str, event: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Evaluate a persisted event: write coach_rule_hit rows + back-fill
    coach_event.rule_hits. Returns the list of hits. Never raises."""
    ctx = ctx or {}
    user_id = event.get("user_id") or "unknown"
    department = event.get("department")

    from db.database import SessionLocal
    db = SessionLocal()
    try:
        muted_ids = _muted_rule_ids(user_id, db)
        disabled_ids = _disabled_rule_ids(department, db)
        hits = _run_rules(event, ctx, skip=disabled_ids)

        from db.models import CoachRuleHit, CoachEvent
        persisted = []
        for h in hits:
            is_muted = h["rule_id"] in muted_ids
            row = CoachRuleHit(
                event_id=event_id,
                user_id=user_id,
                rule_id=h["rule_id"],
                category=h["category"],
                severity=h["severity"],
                channel=event.get("channel") or "web",
                department=department,
                detail={"title": h["title"], "advice": h["advice"]},
                evidence=h["evidence"],
                muted=is_muted,
            )
            db.add(row)
            persisted.append(h["rule_id"])

        # Back-fill the event's rule_hits summary (composite PK lookup).
        try:
            ts = event.get("ts")
            q = db.query(CoachEvent).filter(CoachEvent.event_id == event_id)
            ev_row = q.first()
            if ev_row is not None:
                ev_row.rule_hits = persisted
        except Exception as e:
            logger.warning(f"coach.evaluator: rule_hits back-fill failed ({e.__class__.__name__})")

        db.commit()
        return hits
    except Exception as e:
        db.rollback()
        logger.error(f"coach.evaluator: evaluate_event failed for {event_id} ({e.__class__.__name__}: {e})")
        return []
    finally:
        db.close()


def evaluate_dry_run(event: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None,
                     rules: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Evaluate without persisting — used for inline /ask hints and the
    POST /rules/test endpoint. `rules` optionally restricts to a subset of
    rule ids."""
    ctx = ctx or {}
    if rules:
        wanted = set(rules)
        skip = {r.rule_id for r in BASELINE_RULES if r.rule_id not in wanted}
    else:
        skip = set()
    return _run_rules(event, ctx, skip=skip)


# ── scoring ─────────────────────────────────────────────────────────────────

def _decay_score(penalty: float) -> float:
    """100 * exp(-penalty / K), clamped to [0, 100]."""
    try:
        val = 100.0 * math.exp(-max(0.0, penalty) / COACH_SCORE_DECAY_K)
    except Exception:
        val = 100.0
    return round(max(0.0, min(100.0, val)), 2)


def compute_scores(user_id: str, days: int = 30, db=None) -> Dict[str, Any]:
    """Compute overall + per-category practice scores for a user over a window.

    Returns:
        {
          "user_id", "days", "event_count", "hit_count",
          "gated": bool,                # True when below MIN_EVENTS_FOR_SCORE
          "overall": float|None,
          "categories": { "prompt-quality": float|None, ... },
          "penalties":  { category: penalty_sum },
        }
    Score is None (n/a) when event_count < MIN_EVENTS_FOR_SCORE.
    """
    own_db = False
    if db is None:
        from db.database import SessionLocal
        db = SessionLocal()
        own_db = True

    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    result: Dict[str, Any] = {
        "user_id": user_id,
        "days": days,
        "event_count": 0,
        "hit_count": 0,
        "gated": True,
        "overall": None,
        "categories": {c: None for c in ALL_CATEGORIES},
        "penalties": {c: 0.0 for c in ALL_CATEGORIES},
    }
    try:
        from db.models import CoachEvent, CoachRuleHit

        event_count = (db.query(CoachEvent)
                       .filter(CoachEvent.user_id == user_id, CoachEvent.ts >= since)
                       .count())
        result["event_count"] = int(event_count)

        hits = (db.query(CoachRuleHit)
                .filter(CoachRuleHit.user_id == user_id,
                        CoachRuleHit.created_at >= since,
                        CoachRuleHit.muted == False)  # noqa: E712
                .all())
        result["hit_count"] = len(hits)

        penalties = {c: 0.0 for c in ALL_CATEGORIES}
        for h in hits:
            w = _SEVERITY_WEIGHT.get(h.severity, 1.0)
            if h.category in penalties:
                penalties[h.category] += w

        # ── EvalEngine contribution to prompt-quality penalty ─────────────
        # For every REJECT event in the window, add (1 - eval_score) *
        # COACH_EVAL_PENALTY_WEIGHT to the prompt-quality penalty.
        # eval_score=0.6 (failed 2/6 criteria) → penalty += 0.4 * 3.0 = 1.2
        # eval_score=0.0 (failed all criteria) → penalty += 1.0 * 3.0 = 3.0
        # NULL eval_score (judge not yet run) → skipped, no penalty.
        try:
            eval_rows = (db.query(CoachEvent.eval_score)
                         .filter(CoachEvent.user_id == user_id,
                                 CoachEvent.ts >= since,
                                 CoachEvent.eval_verdict == "REJECT",
                                 CoachEvent.eval_score.isnot(None))
                         .all())
            for (es,) in eval_rows:
                penalties[CATEGORY_PROMPT] += (1.0 - float(es)) * COACH_EVAL_PENALTY_WEIGHT
        except Exception as _e:
            logger.warning(f"coach.evaluator: eval penalty aggregation failed ({_e.__class__.__name__})")

        result["penalties"] = penalties

        if event_count < COACH_MIN_EVENTS_FOR_SCORE:
            result["gated"] = True
            return result

        result["gated"] = False
        cats: Dict[str, float] = {}
        for c in ALL_CATEGORIES:
            cats[c] = _decay_score(penalties[c])
        result["categories"] = cats
        total_penalty = sum(penalties.values())
        result["overall"] = _decay_score(total_penalty)
        return result
    except Exception as e:
        logger.error(f"coach.evaluator: compute_scores failed for {user_id} ({e.__class__.__name__}: {e})")
        return result
    finally:
        if own_db:
            db.close()


# ── inbox + portal helpers ──────────────────────────────────────────────────

def coach_portal_url() -> str:
    """Deep link to the Coach dashboard in the web UI."""
    base = (PLATFORM_BASE_URL or "").rstrip("/")
    return f"{base}/coach"


def publish_coach_inbox(user_id: str, title: str, body: str,
                        source_id: str = "", metadata: Optional[dict] = None) -> str:
    """Publish a Coach inbox item (digest / nudge / critical hit). Never raises."""
    try:
        from store.inbox_store import publish_inbox_item
        meta = dict(metadata or {})
        meta.setdefault("coach_url", coach_portal_url())
        return publish_inbox_item(
            user_id=user_id,
            type="coach_digest",
            title=title,
            body=body,
            source_id=source_id,
            metadata=meta,
        )
    except Exception as e:
        logger.error(f"coach.evaluator: publish_coach_inbox failed ({e.__class__.__name__}: {e})")
        return ""
