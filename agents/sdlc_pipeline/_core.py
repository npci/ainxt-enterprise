# SPDX-License-Identifier: MIT
# ============================================================
# SDLC PIPELINE
#
# Replicates CRED's AI SDLC pipeline:
#   Feature flow: Classifier → Analyst → Solution Agent →
#                 [Solution Review Loop] → [HITL Approval] →
#                 Coding State Machine → PR Reviewer →
#                 [HITL PR Approval]
#
#   Bug flow:     Classifier (triage) → Inbox Manager →
#                 Troubleshooter → Solutioning Agent →
#                 [HITL Approval]
#
# Each stage is run by a dedicated purpose-built agent with
# a laser-scoped system prompt and minimal tool set.
# Self-review loop runs before every HITL gate.
# ============================================================

import json
import os
import threading
from contextvars import ContextVar
from typing import Optional

from core.logger import logger, bind_context, clear_bound_context, set_request_id, mask_email
from core.config import REDIS_HOST as _REDIS_HOST, REDIS_PORT as _REDIS_PORT
from core.config import sdlc_gate_deadline
import time


# ── Step-level timeout decorator ──────────────────────────────────────────
#
# SDLC pipelines are long-running (5–30 min total) but individual steps
# should never hang indefinitely (e.g. stalled Jira API, hung LLM call).
# wrap_step_timeout() runs the step in a thread and kills it after STEP_TIMEOUT.
# On timeout the pipeline raises StepTimeoutError and the rq job fails → DLQ.

_STEP_TIMEOUT    = 900    # 15 minutes per step (most steps finish in <3 min)
# Soft warning threshold for prompt size. Above this we log loudly so unusually
# large prompts are visible in the worker log, but we do NOT truncate — the
# whole prompt is sent to the LLM and the model decides (Claude 4.x accepts
# ~200K tokens ≈ 800K chars). Silent middle-truncation used to drop critical
# context (file contents, code samples, the trailing `open_questions` field of
# self-reviewed JSON, etc.) without anyone noticing.
_PROMPT_WARN_CHARS = 200_000   # ~50K tokens — large but not necessarily fatal
# Opt-in emergency truncation cap. Unset by default → never truncate. Set via
# env var when a deployment hits a hard model limit and wants the safety net
# even at the cost of context loss. When set AND exceeded, we head+tail-window
# (drop the middle) and log a WARNING so the truncation is auditable.
_PROMPT_HARD_CAP_CHARS = int(os.getenv("SDLC_LLM_PROMPT_HARD_CAP", "0") or "0")

# ── W-F (C2): non-editable deny-list ────────────────────────────────────────
# Path prefix / glob patterns that must NEVER appear in `files_to_change`
# (the EDIT list). Backups, archived dirs, old-script copies, and HISTORICAL
# migrations are read-only artifacts — editing them in place is exactly the C2
# bug we are fixing (the coder anchored on a bkp_ copy and edited committed SQL
# instead of authoring a new dated migration).
#
# IMPORTANT: this filter applies ONLY to files_to_change (EDIT). It is
# deliberately NOT applied to `new_files_needed` (CREATE) — authoring a NEW
# dated `prod_catchup_*.sql` is the DESIRED behavior, so a new file matching
# `prod_catchup_*` must pass through untouched.
#
# Patterns are matched as fnmatch globs against the full normalized path AND
# every path segment (so `bkp_*` matches whether it is a dir name or a leaf).
# Tunable; override at deploy time via SDLC_DENYLIST_EXTRA (comma-separated
# globs) and disable wholesale via SDLC_DENYLIST_NONEDITABLE=false.
_NONEDITABLE_DENYLIST: tuple = (
    "old_scripts/*", "*/old_scripts/*",
    "bkp_*", "*/bkp_*",
    "backup/*", "*/backup/*", "*/backups/*", "backups/*",
    "archive/*", "*/archive/*", "archived/*", "*/archived/*",
    "*/prod_catchup_*",        # historical catch-up migrations (EDIT only)
    "prod_catchup_*",
)


def _denylist_enabled() -> bool:
    """W-F: deny-list filter is on by default; SDLC_DENYLIST_NONEDITABLE=false disables it."""
    return os.getenv("SDLC_DENYLIST_NONEDITABLE", "true").lower() in ("1", "true", "yes")


def _denylist_patterns() -> tuple:
    """Effective deny-list = built-in patterns + any SDLC_DENYLIST_EXTRA globs."""
    extra = os.getenv("SDLC_DENYLIST_EXTRA", "")
    if not extra.strip():
        return _NONEDITABLE_DENYLIST
    extra_globs = tuple(g.strip() for g in extra.split(",") if g.strip())
    return _NONEDITABLE_DENYLIST + extra_globs


def _is_noneditable_path(path: str) -> bool:
    """True when `path` matches any deny-list glob (full path or any segment).

    Backups / archived / historical-migration paths are read-only. Matching is
    case-insensitive on the normalized (forward-slash) path.
    """
    if not path or not isinstance(path, str):
        return False
    import fnmatch as _fn
    norm = path.replace("\\", "/").strip().lower()
    segments = [s for s in norm.split("/") if s]
    for pat in _denylist_patterns():
        p = pat.lower()
        if _fn.fnmatch(norm, p):
            return True
        # Also test bare-name globs (no slash) against each path segment so
        # e.g. "bkp_*" matches "db/old_scripts/bkp_rbac/x.sql".
        if "/" not in p and any(_fn.fnmatch(seg, p) for seg in segments):
            return True
    return False


def _filter_noneditable_files(paths: list) -> tuple:
    """Split a files_to_change list into (kept, dropped) by the deny-list.

    Returns (kept_paths, dropped_paths). When the deny-list is disabled via env,
    returns (paths, []) unchanged. Always _s()-wraps items before testing so a
    dict/nested list returned by the LLM is normalized to a string first.
    """
    if not _denylist_enabled():
        return ([_s(p) for p in (paths or [])], [])
    kept: list = []
    dropped: list = []
    for p in (paths or []):
        sp = _s(p)
        if sp and _is_noneditable_path(sp):
            dropped.append(sp)
        elif sp:
            kept.append(sp)
    return (kept, dropped)

# Per-file cap inside _build_code_block. 15 files × 30K = 450K worst case for
# code, leaves ~50K for analysis JSON + tree + instructions under the global cap.
_MAX_FILE_CHARS   = 30_000

# W-C — hard ceiling on what the STRUCTURED (cached) path may receive. The
# structured streaming path in model_router collects the whole response body and
# was timing out "after 0 lines" when handed ~500KB analyst prompts. We cap the
# combined structured payload well below that danger zone; over-cap blobs are
# head+tail windowed (middle dropped) so trailing JSON schema instructions survive.
# Override at deploy time with SDLC_STRUCTURED_MAX_CHARS; <=0 disables the cap.
_STRUCTURED_MAX_CHARS = int(os.getenv("SDLC_STRUCTURED_MAX_CHARS", "350000") or "350000")


def _env_flag(name: str, default: bool) -> bool:
    """
    Parse a boolean env flag. Accepts 1/true/yes/on (case-insensitive) as True
    and 0/false/no/off as False; any other value (or unset) → *default*. Used by
    the W-H / W-D-inject feature gates so they can be rolled back at deploy time
    with no redeploy.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


# W-D-inject — inject a structured graph dependency slice (from
# models.graph_resolver.build_dependency_slice) into the analyst + designer
# prompts. This is a structural call/import graph, not embedding RAG.
# Default OFF (SDLC_GRAPH_CONTEXT=false); set to true only if agentic
# grep/read against the checkout proves insufficient on large repos.
_GRAPH_CONTEXT = _env_flag("SDLC_GRAPH_CONTEXT", False)


# Per-run user credentials — set via contextvars at pipeline entry points so
# every helper function (_jira_comment, _publish_confluence, etc.) automatically
# uses the triggering user's Atlassian token without parameter threading.
_cv_user_id:    ContextVar[str] = ContextVar("sdlc_user_id",    default="")
_cv_user_email: ContextVar[str] = ContextVar("sdlc_user_email", default="")


def _get_run_user(run_id: str = "") -> tuple[str, str]:
    """Return (user_id, user_email) for the current pipeline run.

    Primary source is the per-run contextvars set at each pipeline entry point.
    FALLBACK (2026-07-07): when those are empty — e.g. a resume/worker job (like
    the pre-SM resume that runs PLAN→pregate) that did not re-bind them — resolve
    the triggering user from the run row instead. Downstream Atlassian credential
    resolution (`get_atlassian_creds`) only needs the user_id (it looks the email
    up from the users table), and preflight already proved a token exists for that
    user_id — so this stops Confluence/Jira publish from failing with "no token"
    purely because the contextvar was not bound in this particular job.
    """
    uid, email = _cv_user_id.get(), _cv_user_email.get()
    if uid:
        return uid, email
    rid = run_id or _cv_run_id.get()
    if rid:
        try:
            run = get_run(rid) or {}
            ctx = run.get("context") or {}
            uid = (run.get("triggered_by") or ctx.get("user_id")
                   or ctx.get("triggered_by_user_id") or "")
            email = (email or ctx.get("triggered_by_email")
                     or ctx.get("user_email") or "")
        except Exception as _e:
            logger.debug(f"[SDLC] _get_run_user run-row fallback failed for {rid}: {_e}")
    return uid, email


# ── W-I-emit: per-run context for the model-fallback run event ──────────────
# `_llm` is a module-level helper that does NOT receive run_id/stage as args,
# so we expose them via contextvars set at each pipeline entry point (right
# beside the existing bind_context(correlation_id=run_id) call). When a fallback
# is detected after model_router.generate(), `_llm` reads these to attribute the
# emitted run event to the correct run + stage. Empty default → emit is skipped
# (non-fatal) for any call path that hasn't bound a run (e.g. ad-hoc helpers).
_cv_run_id:    ContextVar[str] = ContextVar("sdlc_llm_run_id",    default="")
_cv_stage:     ContextVar[str] = ContextVar("sdlc_llm_stage",     default="")


def _bind_llm_run_context(run_id: str = "", stage: str = "") -> None:
    """Set the run_id/stage used to attribute model-fallback run events from _llm.

    Mirrors the existing bind_context() pattern — call once at each pipeline
    entry point. Empty values are ignored so a later stage update never clears
    a previously-set run_id.
    """
    if run_id:
        _cv_run_id.set(run_id)
        # Also set the thread-local request_id = run_id so every SDLC LLM call
        # (which routes through model_router → _ProxyGateway and reads
        # get_request_id()) carries the run_id into the LLM proxy. This is the
        # single SDLC entry point hit by all pipeline stages, so the pre-state-
        # machine stages (classify/analyze/design) are covered here; the state
        # machine sets it in its constructor and re-binds it into pool threads
        # via _submit_bound(). SDLC-scoped — no impact on the rest of the system.
        set_request_id(run_id)
    if stage:
        _cv_stage.set(stage)


def _emit_fallback_event_if_any() -> None:
    """W-I-emit (G3): after a model_router.generate() call, read last_decision and
    emit a single `fallback` run event when a primary→fallback swap occurred.

    Consumes the L5 contract (models.model_router.FallbackInfo / last_decision).
    NON-FATAL by contract: any failure here is swallowed so it can never break an
    LLM call. Gated behind SDLC_EMIT_FALLBACK_EVENTS (default true). Emits at most
    one event per actual fallback (last_decision is reset per generate() call, so
    no duplicate spam across calls).

    NOTE: last_decision is populated by model_router.generate() only — NOT by
    stream(). The SDLC _llm path uses generate(), so this is reliable here.
    """
    if os.getenv("SDLC_EMIT_FALLBACK_EVENTS", "true").lower() not in ("1", "true", "yes"):
        return
    try:
        from models.model_router import model_router
        fi = getattr(model_router, "last_decision", None)
        if fi is None or not getattr(fi, "fallback_occurred", False):
            return
        run_id = _cv_run_id.get()
        if not run_id:
            # No bound run (e.g. ad-hoc helper) — nothing to attribute the event
            # to. Skip silently; this is best-effort observability, not control flow.
            return
        stage = _cv_stage.get() or ""
        add_run_event(
            run_id,
            from_state=stage,
            to_state=stage,
            stage=stage,
            actor="model-router",
            output=f"fallback: {fi.from_tier}→{fi.to_tier}",
            data={
                "from": fi.from_tier,
                "to": fi.to_tier,
                "reason": fi.reason,
                "from_label": fi.from_label,
                "to_label": fi.to_label,
            },
        )
    except Exception as _fb_e:  # pragma: no cover — defensive, must never raise
        try:
            logger.warning(f"[SDLC] fallback-event emit failed (non-fatal): {_fb_e}")
        except Exception:
            pass


class StepTimeoutError(RuntimeError):
    """Raised when an SDLC pipeline step exceeds _STEP_TIMEOUT seconds."""


def _run_with_timeout(fn, *args, timeout: int = _STEP_TIMEOUT, step_name: str = "step", **kwargs):
    """
    Run fn(*args, **kwargs) in a daemon thread.
    Raises StepTimeoutError if it does not complete within `timeout` seconds.
    Thread-safe: the calling thread blocks until done or timeout.
    """
    result_holder: list = []
    exc_holder:    list = []

    def _target():
        try:
            result_holder.append(fn(*args, **kwargs))
        except Exception as _e:
            exc_holder.append(_e)

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout=timeout)

    if t.is_alive():
        # Thread is still running — we cannot kill it, but we stop waiting.
        # The daemon thread will be cleaned up when the rq worker process exits.
        logger.error(
            f"sdlc step_timeout: {step_name!r} exceeded {timeout}s limit — aborting pipeline"
        )
        raise StepTimeoutError(
            f"SDLC step {step_name!r} timed out after {timeout}s. "
            "Check Jira/GitLab API health and LLM availability."
        )

    if exc_holder:
        raise exc_holder[0]

    return result_holder[0] if result_holder else None
from store.sdlc_store import (
    create_run, get_run, update_run_state, add_run_event,
    patch_run_context, SDLCCancelled, SDLCUserTokenMissing,
)


# ── Model router shortcut ─────────────────────────────────────

def _llm(prompt: str, hint: str = "solution", agent_id: str = None) -> str:
    """
    SDLC model policy: Claude Opus 4.7 primary, GPT-5.4 fallback.
    NO local models (Ollama) ever — always routes to solution tier
    (Opus 4.7 when ENABLE_OPUS=true) and falls back to GPT-5.4 (medium tier).
    Circuit breaker wraps both providers to prevent cascade failures.

    Prompt size policy (changed from earlier silent-truncation):
      • No truncation by default. The full prompt is sent to the model. If
        the model rejects it as too long, the existing Claude→GPT fallback
        and the circuit breaker take over — failure is loud, not silent.
      • A soft WARNING is logged when the prompt exceeds _PROMPT_WARN_CHARS
        (200K) so unusual sizes are visible in the worker log.
      • Optional emergency truncation only when SDLC_LLM_PROMPT_HARD_CAP is
        set in the environment AND the prompt exceeds that cap. Head+tail
        windowing (drop the middle) so trailing fields like `open_questions`
        survive — the issue that prompted this rewrite.
    """
    _prompt_len = len(prompt)
    if _prompt_len > _PROMPT_WARN_CHARS:
        logger.warning(
            f"[SDLC] _llm: prompt is {_prompt_len:,} chars "
            f"(>{_PROMPT_WARN_CHARS:,} warning threshold). "
            f"Sending in full — model decides. Set SDLC_LLM_PROMPT_HARD_CAP to enable opt-in truncation."
        )
    if _PROMPT_HARD_CAP_CHARS and _prompt_len > _PROMPT_HARD_CAP_CHARS:
        logger.warning(
            f"[SDLC] _llm: prompt {_prompt_len:,} chars exceeds opt-in hard cap "
            f"{_PROMPT_HARD_CAP_CHARS:,} — compacting prompt. "
            f"Some context may be condensed; expect minor downstream quality change."
        )
        from core.context_compressor import compact_prompt
        prompt = compact_prompt(prompt, _PROMPT_HARD_CAP_CHARS)

    # F4 — safety-net boundary guard: if no explicit hard cap was applied above,
    # still compact any prompt that exceeds the context budget ceiling to prevent
    # silent model-level rejection. Uses _STRUCTURED_MAX_CHARS as the ceiling
    # (~350K chars). No-op when already under budget (compact_prompt is a no-op
    # for under-budget input). Guard is skipped when _PROMPT_HARD_CAP_CHARS
    # already handled compaction above.
    _F4_CEILING = _STRUCTURED_MAX_CHARS if _STRUCTURED_MAX_CHARS > 0 else 350_000
    if not _PROMPT_HARD_CAP_CHARS and len(prompt) > _F4_CEILING:
        logger.warning(
            f"[SDLC] _llm: prompt {len(prompt):,} chars exceeds F4 ceiling "
            f"{_F4_CEILING:,} — applying safety-net compaction before send."
        )
        from core.context_compressor import compact_prompt
        prompt = compact_prompt(prompt, _F4_CEILING)

    from models.model_router import model_router
    from core.circuit_breaker import get_breaker
    # Primary tier is the caller's hint (default "solution"). This lets each stage
    # pick its model — pass a stage-resolved hint, e.g. _sdlc_model("noncode").
    # GPT-5.4 (medium) remains the cross-provider fallback.
    _model_used = hint or "solution"
    try:
        result = get_breaker("claude").call(
            lambda: model_router.generate(prompt, model_hint=_model_used)
        )  # primary tier = caller hint (solution/Opus by default)
        if not result or not result.strip():
            raise ValueError("empty response from Claude")
    except Exception as _ce:
        logger.warning(f"[SDLC] Claude unavailable ({_ce}) — falling back to GPT-5.4 (medium)")
        try:
            result = get_breaker("openai").call(
                lambda: model_router.generate(prompt, model_hint="medium")
            )  # GPT-5.4
            if not result or not result.strip():
                raise ValueError("GPT-5.4 returned empty response")
        except Exception as _gpt_e:
            logger.error(f"[SDLC] _llm: both Claude and GPT-5.4 failed ({_gpt_e}) — returning error sentinel")
            result = '{"error": "Both LLM providers failed or returned empty"}'
        _model_used = "medium"
    # W-I-emit (G3): surface any model-router fallback that just happened.
    # model_router.last_decision reflects the LAST generate() call above (the
    # router's own internal primary→fallback swap, e.g. Opus unavailable →
    # routed to GPT). Non-fatal + gated by SDLC_EMIT_FALLBACK_EVENTS.
    _emit_fallback_event_if_any()
    # Token + cost estimation (4 chars ≈ 1 token — rough but consistent).
    # Rate from the single-source-of-truth helper (R3) — replaces the prior
    # hand-rolled table that over-billed Opus 3× and billed Sonnet/Haiku at
    # GPT-mini rates. tier_cost_per_1m reads ENABLE_OPUS at call time.
    from core.model_registry import tier_cost_per_1m
    _tokens_in  = len(prompt) // 4
    _tokens_out = len(result) // 4 if result else 0
    _rate_in, _rate_out = tier_cost_per_1m(_model_used)
    _cost = (_tokens_in / 1_000_000 * _rate_in) + (_tokens_out / 1_000_000 * _rate_out)
    logger.info(
        f"[SDLC] _llm model={_model_used} tokens_in~{_tokens_in} tokens_out~{_tokens_out} "
        f"cost~${_cost:.4f}"
    )
    try:
        from memory.postgres_memory import PostgresMemory as _PM
        _pm = _PM()
        _pm.create_model_usage(
            user_id="sdlc",
            agent_id=agent_id or "sdlc-pipeline",
            endpoint="/sdlc/pipeline",
            model=_model_used,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=round(_cost, 6),
        )
    except Exception:
        pass
    try:
        from services.sdlc_budget_tracker import record_llm_cost as _rec_cost
        _rec_cost(_tokens_in, _tokens_out, round(_cost, 6), run_id=_cv_run_id.get())
    except Exception:
        pass
    return result


def _sdlc_model(tier: str) -> str:
    """
    Returns the model router hint for a task tier/stage. Delegates to the canonical
    per-stage resolver (core.model_registry.sdlc_stage_hint): defaults live in code,
    each stage is overridable via SDLC_MODEL_<STAGE>, and solution→complex when
    ENABLE_OPUS=false. Known tiers: synthesis | exploration | noncode | classify.
    """
    from core.model_registry import sdlc_stage_hint
    return sdlc_stage_hint(tier, default="complex")


_SDLC_AGENT_TIMEOUT = 600  # 10 minutes — SDLC steps run multi-file LLM loops

def _run_sdlc_agent(agent_name: str, task: str) -> str:
    """
    Run a named SDLC agent via AgentRunner — drives Claude's real tool-use loop.

    Claude autonomously calls jira_get_issue, gitlab_read_file, retrieve_tool,
    jira_add_comment, confluence_create_page, etc. with structured parameters.
    This is the real agentic path vs _llm() which is a plain text completion.

    Falls back to _llm(task) if the agent record isn't found or AgentRunner fails.
    """
    from db.database import SessionLocal as _SL
    from db.models import AgentRecord as _AR
    from agents.agent_builder import AgentBuilder, AgentRunner
    db = _SL()
    try:
        rec = db.query(_AR).filter(_AR.name == agent_name).first()
        if not rec:
            logger.warning(f"_run_sdlc_agent: {agent_name!r} not found in DB — falling back to _llm()")
            return _llm(task)
        builder = AgentBuilder()
        runner = AgentRunner(builder)
        result = runner.run(agent_name, task, timeout_secs=_SDLC_AGENT_TIMEOUT)
        if not result.success:
            raise RuntimeError(result.error or "Agent run failed with no output")
        return result.answer
    except Exception as e:
        logger.warning(f"_run_sdlc_agent({agent_name!r}) failed: {e} — falling back to _llm()")
        return _llm(task)
    finally:
        db.close()


# ── Dynamic Multi-Stack Language Detector ─────────────────────
#
# Scans EVERY file in the tree at EVERY depth.
# Handles monorepos (React frontend + Java backend + Python scripts, etc.).
# No hardcoded language ordering — files are counted and scored dynamically.
# Framework is identified from config FILE CONTENTS, not path heuristics.

# Source file extension → (language, default test framework)
_EXT_LANG: dict[str, tuple[str, str]] = {
    # JVM family
    "java":   ("java",        "JUnit 5"),
    "kt":     ("kotlin",      "JUnit 5"),
    "kts":    ("kotlin",      "JUnit 5"),
    "scala":  ("scala",       "ScalaTest"),
    "groovy": ("groovy",      "Spock"),
    "clj":    ("clojure",     "clojure.test"),
    "cljs":   ("clojure",     "clojure.test"),
    # Web / Node
    "ts":     ("typescript",  "Jest"),
    "tsx":    ("typescript",  "Jest"),
    "mts":    ("typescript",  "Jest"),
    "js":     ("javascript",  "Jest"),
    "jsx":    ("javascript",  "Jest"),
    "mjs":    ("javascript",  "Jest"),
    "cjs":    ("javascript",  "Jest"),
    "vue":    ("javascript",  "Jest + Vue Test Utils"),
    "svelte": ("javascript",  "Vitest"),
    # Python
    "py":     ("python",      "pytest"),
    "pyw":    ("python",      "pytest"),
    "pyx":    ("python",      "pytest"),
    # Go
    "go":     ("go",          "go test"),
    # Rust
    "rs":     ("rust",        "cargo test"),
    # .NET
    "cs":     ("csharp",      "xUnit"),
    "fs":     ("fsharp",      "xUnit"),
    "vb":     ("vbnet",       "xUnit"),
    # Ruby
    "rb":     ("ruby",        "RSpec"),
    "rake":   ("ruby",        "RSpec"),
    # PHP
    "php":    ("php",         "PHPUnit"),
    # Swift / ObjC
    "swift":  ("swift",       "XCTest"),
    "m":      ("objc",        "XCTest"),
    # Dart / Flutter
    "dart":   ("dart",        "flutter test"),
    # Elixir / Erlang
    "ex":     ("elixir",      "ExUnit"),
    "exs":    ("elixir",      "ExUnit"),
    "erl":    ("erlang",      "EUnit"),
    "hrl":    ("erlang",      "EUnit"),
    # C / C++
    "cpp":    ("cpp",         "Google Test"),
    "cc":     ("cpp",         "Google Test"),
    "cxx":    ("cpp",         "Google Test"),
    "hxx":    ("cpp",         "Google Test"),
    "hpp":    ("cpp",         "Google Test"),
    "c":      ("c",           "Unity"),
    # Shell
    "sh":     ("shell",       "bats"),
    "bash":   ("shell",       "bats"),
    # Data / ML
    "r":      ("r",           "testthat"),
    "jl":     ("julia",       "Test"),
    "hs":     ("haskell",     "HUnit"),
    "lua":    ("lua",         "busted"),
}

# Config filenames that identify a language (searched at ANY depth)
# value: (language, framework_hint_if_no_content)
_CONFIG_LANG: dict[str, tuple[str, str]] = {
    "pom.xml":             ("java",       "Maven"),
    "build.gradle":        ("java",       "Gradle"),
    "build.gradle.kts":    ("kotlin",     "Gradle"),
    "build.sbt":           ("scala",      "sbt"),
    "go.mod":              ("go",         "Go Modules"),
    "Cargo.toml":          ("rust",       "Cargo"),
    "pyproject.toml":      ("python",     "Python"),
    "setup.py":            ("python",     "Python"),
    "setup.cfg":           ("python",     "Python"),
    "requirements.txt":    ("python",     "Python"),
    "Pipfile":             ("python",     "Python"),
    "poetry.lock":         ("python",     "Poetry"),
    "package.json":        ("javascript", "Node.js"),
    "tsconfig.json":       ("typescript", "TypeScript"),
    "tsconfig.base.json":  ("typescript", "TypeScript"),
    "deno.json":           ("typescript", "Deno"),
    "deno.jsonc":          ("typescript", "Deno"),
    "composer.json":       ("php",        "PHP"),
    "Gemfile":             ("ruby",       "Ruby"),
    "mix.exs":             ("elixir",     "Elixir"),
    "pubspec.yaml":        ("dart",       "Dart/Flutter"),
    "Package.swift":       ("swift",      "Swift PM"),
    "build.xml":           ("java",       "Ant"),
    "CMakeLists.txt":      ("cpp",        "CMake"),
    "Makefile":            ("c",          "Make"),
    "cabal.project":       ("haskell",    "Cabal"),
    "stack.yaml":          ("haskell",    "Stack"),
    "project.clj":         ("clojure",    "Leiningen"),
    "deps.edn":            ("clojure",    "deps.edn"),
    "rebar.config":        ("erlang",     "rebar3"),
}

# Directory names to skip entirely (vendor / generated / artifacts)
_SKIP_DIRS: frozenset[str] = frozenset({
    "node_modules", ".git", ".svn", "dist", "build", "__pycache__",
    ".gradle", "target", "vendor", ".cache", "coverage",
    ".next", ".nuxt", ".svelte-kit", "out", "tmp", "temp",
    "venv", ".venv", "env", "site-packages", ".tox",
    "bazel-out", ".dart_tool", ".pub-cache",
    "generated", "gen", "proto_gen", "grpc_gen",
    ".terraform", "terraform.tfstate.d",
})

# Extensions of non-source files (lock files, assets, compiled output)
_SKIP_EXTS: frozenset[str] = frozenset({
    "lock", "sum", "ico", "png", "jpg", "jpeg", "gif", "svg", "webp",
    "woff", "woff2", "ttf", "eot", "otf", "map",
    "pdf", "doc", "docx", "xls", "xlsx", "pptx",
    "zip", "tar", "gz", "bz2", "7z",
    "jar", "war", "ear", "aar", "class",
    "pyc", "pyo", "pyd",
    "o", "obj", "so", "dll", "dylib", "exe", "bin",
    "img", "iso", "log", "bak", "tmp",
    "min",  # .min.js / .min.css counted by base ext anyway
})

# Directory names that indicate test code (lower weight when scoring)
_TEST_DIRS: frozenset[str] = frozenset({
    "test", "tests", "spec", "specs", "__tests__",
    "e2e", "integration_tests", "unit_tests", "test_data",
    "__mocks__", "mocks", "fixtures", "testdata", "testing",
    "it", "acceptance",
})

# Files worth reading for context injection into prompts
_CONTEXT_FILES = [
    "package.json", "go.mod", "pom.xml", "build.gradle",
    "build.gradle.kts", "requirements.txt", "pyproject.toml",
    "Cargo.toml", "composer.json", "Gemfile", "README.md",
]

# Finding any of these files definitively identifies the primary language.
# Once one is read successfully the config scan loop can stop — no need to
# attempt gradle/go.mod/Cargo.toml API calls that will all 404.
_DEFINITIVE_BUILD_FILES = frozenset({
    "pom.xml", "build.gradle", "build.gradle.kts",
    "go.mod", "Cargo.toml",
})


def _enhance_framework_from_content(entry: dict, config_filename: str, content: str) -> None:
    """
    Improve framework and test-framework detection by parsing config file content.
    Called when we actually have the file text (from GitLab or indexed store).
    Mutates `entry` in place.
    """
    c = content.lower()

    if config_filename == "pom.xml":
        if "spring-boot" in c or "spring.boot" in c:
            entry["framework"] = "Spring Boot"
            entry["test_framework"] = "JUnit 5 + MockMvc"
        elif "quarkus" in c:
            entry["framework"] = "Quarkus"
        elif "micronaut" in c:
            entry["framework"] = "Micronaut"
        elif "vertx" in c:
            entry["framework"] = "Vert.x"
        elif "jakarta" in c:
            entry["framework"] = "Jakarta EE"
        else:
            entry["framework"] = "Maven"

    elif config_filename in ("build.gradle", "build.gradle.kts"):
        if "spring" in c:
            entry["framework"] = "Spring Boot"
            entry["test_framework"] = "JUnit 5 + MockMvc"
        elif "quarkus" in c:
            entry["framework"] = "Quarkus"
        elif "micronaut" in c:
            entry["framework"] = "Micronaut"
        else:
            entry["framework"] = "Gradle"

    elif config_filename == "package.json":
        import json as _jj
        try:
            pkg = _jj.loads(content)
            all_deps = {
                **pkg.get("dependencies", {}),
                **pkg.get("devDependencies", {}),
                **pkg.get("peerDependencies", {}),
            }
            deps = {k.lower(): v for k, v in all_deps.items()}
            has_ts = "typescript" in deps or any(k.startswith("@types/") for k in deps)

            if has_ts:
                entry["language"] = "typescript"

            if "react" in deps or "react-dom" in deps:
                entry["framework"] = "Next.js" if "next" in deps else "React"
                entry["test_framework"] = (
                    "Vitest" if "vitest" in deps else "Jest + React Testing Library"
                )
            elif "@angular/core" in deps or "angular" in deps:
                entry["framework"] = "Angular"
                entry["test_framework"] = "Jasmine + Karma"
            elif "vue" in deps or "vue3" in deps:
                entry["framework"] = "Nuxt.js" if "nuxt" in deps else "Vue.js"
                entry["test_framework"] = "Vitest" if "vitest" in deps else "Jest + Vue Test Utils"
            elif "@sveltejs/kit" in deps or "svelte" in deps:
                entry["framework"] = "SvelteKit" if "@sveltejs/kit" in deps else "Svelte"
                entry["test_framework"] = "Vitest"
            elif "@nestjs/core" in deps:
                entry["framework"] = "NestJS"
                entry["test_framework"] = "Jest"
            elif "express" in deps:
                entry["framework"] = "Express"
                entry["test_framework"] = "Jest + Supertest"
            elif "fastify" in deps:
                entry["framework"] = "Fastify"
                entry["test_framework"] = "Jest"
            elif "koa" in deps:
                entry["framework"] = "Koa"
                entry["test_framework"] = "Jest"
            elif "hapi" in deps or "@hapi/hapi" in deps:
                entry["framework"] = "Hapi"
            elif "electron" in deps:
                entry["framework"] = "Electron"
            else:
                entry["framework"] = "Node.js"
        except Exception:
            pass

    elif config_filename in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile"):
        if "fastapi" in c:
            entry["framework"] = "FastAPI"
            entry["test_framework"] = "pytest + httpx"
        elif "django" in c:
            entry["framework"] = "Django"
            entry["test_framework"] = "pytest-django"
        elif "flask" in c:
            entry["framework"] = "Flask"
            entry["test_framework"] = "pytest"
        elif "tornado" in c:
            entry["framework"] = "Tornado"
        elif "aiohttp" in c:
            entry["framework"] = "aiohttp"
        elif "starlette" in c:
            entry["framework"] = "Starlette"
        elif "celery" in c:
            entry["framework"] = "Celery"
        elif "pydantic" in c:
            entry["framework"] = "Pydantic"

    elif config_filename == "go.mod":
        if "github.com/gin-gonic/gin" in c:
            entry["framework"] = "Gin"
        elif "github.com/labstack/echo" in c:
            entry["framework"] = "Echo"
        elif "github.com/gofiber/fiber" in c:
            entry["framework"] = "Fiber"
        elif "github.com/go-chi/chi" in c:
            entry["framework"] = "Chi"
        elif "google.golang.org/grpc" in c:
            entry["framework"] = "gRPC"
        elif "github.com/gorilla/mux" in c:
            entry["framework"] = "Gorilla Mux"
        else:
            entry["framework"] = "net/http"

    elif config_filename == "Cargo.toml":
        if "axum" in c:
            entry["framework"] = "Axum"
        elif "actix-web" in c or "actix_web" in c:
            entry["framework"] = "Actix"
        elif "warp" in c:
            entry["framework"] = "Warp"
        elif "rocket" in c:
            entry["framework"] = "Rocket"
        elif "tonic" in c:
            entry["framework"] = "Tonic (gRPC)"
        else:
            entry["framework"] = "Rust"

    elif config_filename in ("Gemfile",):
        if "rails" in c:
            entry["framework"] = "Rails"
            entry["test_framework"] = "RSpec"
        elif "sinatra" in c:
            entry["framework"] = "Sinatra"
        elif "grape" in c:
            entry["framework"] = "Grape"

    elif config_filename == "composer.json":
        if "laravel" in c:
            entry["framework"] = "Laravel"
        elif "symfony" in c:
            entry["framework"] = "Symfony"
        elif "lumen" in c:
            entry["framework"] = "Lumen"
        elif "slim" in c:
            entry["framework"] = "Slim"

    elif config_filename == "mix.exs":
        if "phoenix" in c:
            entry["framework"] = "Phoenix"
        else:
            entry["framework"] = "Elixir"

    elif config_filename == "pubspec.yaml":
        if "flutter" in c:
            entry["framework"] = "Flutter"
            entry["test_framework"] = "flutter test"
        else:
            entry["framework"] = "Dart"

    elif config_filename == "build.sbt":
        if "play" in c:
            entry["framework"] = "Play"
        elif "akka" in c:
            entry["framework"] = "Akka"
        elif "http4s" in c:
            entry["framework"] = "http4s"
        else:
            entry["framework"] = "sbt"


def _path_based_framework(lang: str, path_str: str) -> str:
    """
    Heuristic framework detection purely from the set of file paths
    when no config file content is available.
    """
    s = path_str.lower()
    if lang == "java":
        if "spring" in s: return "Spring Boot"
        if "quarkus" in s: return "Quarkus"
        if "micronaut" in s: return "Micronaut"
        if "vertx" in s: return "Vert.x"
        return "Java"
    if lang == "kotlin":
        if "spring" in s: return "Spring Boot"
        if "ktor" in s: return "Ktor"
        return "Kotlin"
    if lang in ("javascript", "typescript"):
        if ".jsx" in s or ".tsx" in s or "/react" in s: return "React"
        if "/angular" in s or "ng-" in s: return "Angular"
        if ".vue" in s or "/vue" in s: return "Vue.js"
        if ".svelte" in s: return "Svelte"
        if "/next" in s or "next.config" in s: return "Next.js"
        if "/nuxt" in s: return "Nuxt.js"
        if "/express" in s: return "Express"
        if "/nest" in s: return "NestJS"
        if "/fastify" in s: return "Fastify"
        if "/electron" in s: return "Electron"
        return "Node.js"
    if lang == "python":
        if "fastapi" in s: return "FastAPI"
        if "django" in s: return "Django"
        if "flask" in s: return "Flask"
        if "aiohttp" in s: return "aiohttp"
        if "celery" in s: return "Celery"
        return "Python"
    if lang == "go":
        if "gin" in s: return "Gin"
        if "echo" in s: return "Echo"
        if "fiber" in s: return "Fiber"
        if "chi" in s: return "Chi"
        if "grpc" in s: return "gRPC"
        return "Go"
    if lang == "rust":
        if "axum" in s: return "Axum"
        if "actix" in s: return "Actix"
        if "rocket" in s: return "Rocket"
        return "Rust"
    if lang == "csharp":
        if "blazor" in s: return "Blazor"
        return "ASP.NET Core"
    if lang == "ruby":
        if "rails" in s: return "Rails"
        if "sinatra" in s: return "Sinatra"
        return "Ruby"
    if lang == "scala":
        if "play" in s: return "Play"
        if "akka" in s: return "Akka"
        return "Scala"
    if lang == "dart": return "Flutter" if "flutter" in s else "Dart"
    if lang == "php":
        if "laravel" in s: return "Laravel"
        if "symfony" in s: return "Symfony"
        return "PHP"
    return lang.capitalize()


def _detect_all_stacks(paths: list, config_contents: dict | None = None) -> list[dict]:
    """
    Dynamically detect ALL language stacks present in the file tree.

    Scans every file at every depth. Groups source files by language.
    Uses config file contents (when available) for precise framework detection.
    Handles monorepos: Java backend + React frontend + Python scripts all detected.

    Returns list of stack dicts sorted by source weight (primary first):
        {language, framework, test_framework, src_count, test_count,
         root_dirs, config_found, config_path}
    Returns [] if nothing detected (caller must treat as failure).
    """
    config_contents = config_contents or {}
    lang_data: dict[str, dict] = {}
    path_str = " ".join(paths)

    def _entry(lang: str, test_fw: str) -> dict:
        if lang not in lang_data:
            lang_data[lang] = {
                "language": lang,
                "framework": "",
                "test_framework": test_fw,
                "src_count": 0,
                "test_count": 0,
                "root_dirs": set(),
                "config_found": False,
                "config_path": "",
            }
        return lang_data[lang]

    for path in paths:
        parts = path.replace("\\", "/").split("/")
        filename = parts[-1]
        dir_parts = parts[:-1]

        # Skip vendor / generated / artifact directories
        if any(seg in _SKIP_DIRS for seg in parts):
            continue

        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

        # ── Config file detection (at ANY depth) ──────────────────
        if filename in _CONFIG_LANG:
            lang, fw_hint = _CONFIG_LANG[filename]
            test_fw = _EXT_LANG.get({"java": "java", "kotlin": "kt", "python": "py",
                                      "typescript": "ts", "javascript": "js", "go": "go",
                                      "rust": "rs", "ruby": "rb", "php": "php",
                                      "scala": "scala", "dart": "dart", "elixir": "ex",
                                      "cpp": "cpp", "c": "c", "csharp": "cs",
                                      "groovy": "groovy", "clojure": "clj",
                                      "erlang": "erl", "haskell": "hs"}.get(lang, ""), ("", ""))[1]
            e = _entry(lang, test_fw or "")
            e["config_found"] = True
            e["config_path"] = path
            if not e["framework"]:
                e["framework"] = fw_hint
            # Use content for precise framework detection
            if path in config_contents and config_contents[path]:
                _enhance_framework_from_content(e, filename, config_contents[path])
            continue

        # Glob-pattern config files (*.csproj, *.fsproj, *.sln, *.xcodeproj)
        if filename.endswith(".csproj") or filename.endswith(".fsproj") or filename.endswith(".sln"):
            e = _entry("csharp", "xUnit")
            e["config_found"] = True
            if not e["config_path"]:
                e["config_path"] = path
            if not e["framework"]:
                e["framework"] = "ASP.NET Core"
            continue
        if filename.endswith(".xcodeproj") or filename.endswith(".xcworkspace"):
            e = _entry("swift", "XCTest")
            e["config_found"] = True
            if not e["config_path"]:
                e["config_path"] = path
            continue

        # ── Source file counting ───────────────────────────────────
        if ext in _SKIP_EXTS or not ext:
            continue
        if ext not in _EXT_LANG:
            continue

        lang, test_fw = _EXT_LANG[ext]
        e = _entry(lang, test_fw)

        # Is this file in a test directory?
        is_test = (
            any(td in dir_parts for td in _TEST_DIRS)
            or filename.startswith("test_")
            or filename.startswith("Test")
            or filename.endswith((
                "_test.py", "_test.go", "Test.java", "Spec.java",
                ".test.ts", ".test.js", ".spec.ts", ".spec.js",
                "_spec.rb", "Spec.scala", "_test.kt",
            ))
        )

        root_dir = dir_parts[0] if dir_parts else "."
        e["root_dirs"].add(root_dir)

        if is_test:
            e["test_count"] += 1
        else:
            e["src_count"] += 1

    if not lang_data:
        return []

    # Fill framework from path heuristics for any stack that has no framework yet
    for lang, e in lang_data.items():
        if not e["framework"]:
            e["framework"] = _path_based_framework(lang, path_str)

    # Sort: config-confirmed stacks first, then by weighted file count
    results = sorted(
        lang_data.values(),
        key=lambda e: (
            not e["config_found"],
            -(e["src_count"] + e["test_count"] * 0.3),
        ),
    )

    for r in results:
        r["root_dirs"] = sorted(r["root_dirs"])

    return results


# Authoritative stack manifest — checked before all heuristics
_SDLC_MANIFEST = ".sdlc.yml"


def _read_manifest_from_scm(repo: str, branch: str) -> dict | None:
    """
    Read .sdlc.yml from the repo root and return parsed fields, or None if absent/invalid.
    Expected keys: language, framework, test_framework (all strings).
    Dispatches to GitHub or GitLab based on SCM_PROVIDER.
    """
    try:
        import yaml as _yaml
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_read_file as _scm_read_file
        else:
            from tools.gitlab_tools import gitlab_read_file as _scm_read_file
        raw = _scm_read_file(repo, _SDLC_MANIFEST, branch)
        if raw.startswith("[Error"):
            return None
        data = _yaml.safe_load(raw)
        if not isinstance(data, dict):
            return None
        lang = str(data.get("language", "")).strip().lower()
        fw   = str(data.get("framework", "")).strip()
        tests = str(data.get("test_framework", "")).strip()
        if not lang:
            return None
        logger.info(f"_read_manifest_from_scm: repo={repo} → {lang}/{fw}/{tests} (from .sdlc.yml)")
        return {"language": lang, "framework": fw, "test_framework": tests}
    except Exception as _e:
        logger.warning(f"_read_manifest_from_scm: could not parse .sdlc.yml for {repo}: {_e}")
        return None


def _read_manifest_from_indexed(repo_name: str) -> dict | None:
    """
    Read .sdlc.yml content stored in document_embeddings (indexed via CodebaseManager).
    Used as authoritative override when GitLab is unreachable.
    Returns parsed dict or None.
    """
    logger.info(f"entered _read_manifest_from_indexed - {repo_name}")
    try:
        import yaml as _yaml
        from db.database import VectorSessionLocal as _SL
        from sqlalchemy import text as _sql
        from agents.sdlc_context import normalize_repo_index_key as _nrik
        db = _SL()
        try:
            _repo_key = _nrik(repo_name) if repo_name else ""
            row = db.execute(_sql(
                "SELECT content FROM document_embeddings "
                "WHERE repo = :r AND file_path = :f LIMIT 1"
            ), {"r": _repo_key, "f": _SDLC_MANIFEST}).fetchone()
            if not row or not row[0]:
                return None
            data = _yaml.safe_load(row[0])
            if not isinstance(data, dict):
                return None
            lang  = str(data.get("language", "")).strip().lower()
            fw    = str(data.get("framework", "")).strip()
            tests = str(data.get("test_framework", "")).strip()
            if not lang:
                return None
            logger.info(
                f"_read_manifest_from_indexed: repo={repo_name} → {lang}/{fw}/{tests} (from indexed .sdlc.yml)"
            )
            return {"language": lang, "framework": fw, "test_framework": tests}
        finally:
            db.close()
    except Exception as _e:
        logger.warning(f"_read_manifest_from_indexed: DB query failed for {repo_name}: {_e}")
        return None


def _detect_lang_from_indexed(repo_name: str) -> tuple:
    """
    Language detector: scan file_path extensions in document_embeddings for this repo.
    Returns (language, framework, test_framework) or (None, None, None) if undetectable.
    Never defaults to Python.
    """
    logger.info(f"entered _detect_lang_from_indexed - {repo_name}")
    try:
        from db.database import VectorSessionLocal as _SL
        from sqlalchemy import text as _sql
        from agents.sdlc_context import normalize_repo_index_key as _nrik
        db = _SL()
        try:
            _repo_key = _nrik(repo_name) if repo_name else ""
            rows = db.execute(_sql(
                "SELECT file_path FROM document_embeddings "
                "WHERE repo = :r LIMIT 500"
            ), {"r": _repo_key}).fetchall()
            paths = [r[0] for r in rows if r[0]]
            if paths:
                stacks = _detect_all_stacks(paths)
                if stacks:
                    p = stacks[0]
                    logger.info(
                        f"_detect_lang_from_indexed: repo={repo_name} "
                        f"→ all_stacks={[s['language'] for s in stacks]} "
                        f"primary={p['language']}/{p['framework']} (from {len(paths)} indexed files)"
                    )
                    return p["language"], p["framework"], p["test_framework"]
                logger.error(
                    f"_detect_lang_from_indexed: {len(paths)} indexed files for repo={repo_name} "
                    "but no recognized language detected (GitLab-live detection is the primary "
                    "path; add a .sdlc.yml or pass language_override)."
                )
                return None, None, None
            logger.error(
                f"_detect_lang_from_indexed: no indexed files found for repo={repo_name!r} "
                "and GitLab was unreachable — add a .sdlc.yml or pass language_override."
            )
        finally:
            db.close()
    except Exception as _e:
        logger.error(f"_detect_lang_from_indexed: DB query failed for {repo_name}: {_e}")
    return None, None, None


def _fetch_single_repo_context(repo: str, branch: str = None, user_id: str = "", user_email: str = "") -> dict:
    """
    Pull real context for a single repo from GitLab.

    Returns a flat dict with keys:
      language, framework, test_framework, file_tree, config_summary,
      tech_stack, confidence, detection_source, (optionally) all_stacks,
      all_paths, branch, error.

    Language detection order (most authoritative first):
      1. .sdlc.yml manifest in repo root (confidence=1.0, beats all heuristics)
      2. GitLab file tree + config files (requires GITLAB_TOKEN + repo in namespace/project form)
      3. .sdlc.yml stored in document_embeddings (indexed via CodebaseManager)
      4. document_embeddings file paths heuristic (partial signal, confidence=0.6)
      5. Returns confidence=0.0 — pipeline MUST block, never silently default to Python
    """
    # ── Phase 4: Language detection cache ────────────────────────────────
    # Skip the 5-layer detection waterfall when we already ran it for this
    # repo in the last hour.  Cache key includes a re-index discriminator
    # (repo_index_status.completed_at) so the cache auto-invalidates when
    # the repo is re-indexed. Falls back to "unknown" when DB is unavailable
    # — the cache still de-dupes within the same 1-hour window.
    import json as _jlc
    import redis as _rlc
    _rlc_cli = _rlc.Redis(
        host=_REDIS_HOST, port=_REDIS_PORT, db=0,
        decode_responses=True, socket_connect_timeout=1,
    )
    _lang_epoch = "unknown"
    try:
        from db.database import VectorSessionLocal as _SL_lc
        from sqlalchemy import text as _sql_lc
        with _SL_lc() as _sess_lc:
            _row_lc = _sess_lc.execute(
                _sql_lc("SELECT completed_at FROM repo_index_status WHERE repo_name=:r"),
                {"r": repo},
            ).fetchone()
        if _row_lc and _row_lc[0]:
            _lang_epoch = str(_row_lc[0])[:19]  # truncate to seconds; avoids timezone noise
    except Exception:
        pass  # DB unavailable — fall back to "unknown"; caching still works per-run
    _lang_ck = f"sdlc:lang_detect:{repo}:{_lang_epoch}"
    try:
        _cached_lc = _rlc_cli.get(_lang_ck)
        if _cached_lc:
            logger.debug(f"[lang_detect] cache HIT repo={repo!r} epoch={_lang_epoch!r}")
            return _jlc.loads(_cached_lc)
    except Exception:
        pass  # Redis unavailable — proceed normally

    def _write_lang_cache(result: dict) -> dict:
        """Cache result if confidence > 0 (never memoize hard failures)."""
        if result.get("confidence", 0.0) > 0.0:
            try:
                _rlc_cli.setex(_lang_ck, 3600, _jlc.dumps(result))
                logger.debug(f"[lang_detect] cache SET repo={repo!r} epoch={_lang_epoch!r}")
            except Exception:
                pass
        return result

    if not repo or "/" not in repo:
        # No GitLab path available — try manifest from indexed content first
        manifest = _read_manifest_from_indexed(repo or "")
        if manifest:
            lang, fw, tests = manifest["language"], manifest["framework"], manifest["test_framework"]
            tech_stack = f"{lang.capitalize()} / {fw} / {tests}" if fw else f"{lang.capitalize()} / {tests}"
            return _write_lang_cache({
                "language": lang, "framework": fw, "test_framework": tests,
                "file_tree": "", "config_summary": "", "tech_stack": tech_stack,
                "confidence": 1.0, "detection_source": "manifest_indexed",
            })
        lang, fw, tests = _detect_lang_from_indexed(repo or "")
        if not lang:
            reason = (
                f"No repo path given and no indexed files found for repo={repo!r}. "
                "Fix: provide repo in 'namespace/project' format AND ensure GITLAB_TOKEN is set, "
                "OR index the codebase via CodebaseManager first."
            )
            logger.critical(f"SDLC: LANGUAGE UNDETECTABLE — {reason}")
            return {
                "language": None, "framework": None, "test_framework": None,
                "file_tree": "", "config_summary": "", "tech_stack": "unknown",
                "confidence": 0.0, "detection_source": "none",
                "error": reason,
            }
        tech_stack = f"{lang.capitalize()} / {fw} / {tests}" if fw else f"{lang.capitalize()}"
        return _write_lang_cache({
            "language": lang, "framework": fw, "test_framework": tests,
            "file_tree": "", "config_summary": "", "tech_stack": tech_stack,
            "confidence": 0.6, "detection_source": "indexed",
        })

    try:
        import os as _os_rc
        # Resolve per-user SCM token: user_tokens[user_id] → env var fallback
        # user_id is the JWT sub = UUID primary key — direct lookup, no email join needed
        from core.config import SCM_PROVIDER as _SCM_RC
        _gl_token = ""
        if user_id:
            try:
                from core.platform_credentials import get_scm_token as _get_scm_tok
                _gl_token = _get_scm_tok(user_id=user_id)
                logger.debug(f"[_fetch_repo_context] using SCM token ({_SCM_RC}) for user_id={user_id!r} ({mask_email(user_email)})")
            except PermissionError:
                _provider_label_rc = "GitHub" if _SCM_RC == "github" else "GitLab"
                logger.warning(
                    f"[_fetch_repo_context] no SCM token in user_tokens for user_id={user_id!r} ({mask_email(user_email)}) — "
                    f"user must add their {_provider_label_rc} PAT under Profile → {_provider_label_rc} Token"
                )
        if not _gl_token:
            _env_key_rc = "GITHUB_TOKEN" if _SCM_RC == "github" else "GITLAB_TOKEN"
            _gl_token = _os_rc.getenv(_env_key_rc, "")
        logger.debug(
            f"[_fetch_repo_context] repo={repo!r} SCM_PROVIDER={_SCM_RC!r} "
            f"token={'set('+str(len(_gl_token))+'chars)' if _gl_token else 'MISSING — SCM calls will 401'}"
        )
        # Set token in SCM tools thread-local — no process-wide env mutation
        if _gl_token:
            if _SCM_RC == "github":
                from tools.github_tools import set_token as _scm_set_token_rc
                from tools.github_tools import github_read_file as gitlab_read_file
                from tools.github_tools import _detect_default_branch, _get_file_tree
            else:
                from tools.gitlab_tools import set_token as _scm_set_token_rc
                from tools.gitlab_tools import gitlab_read_file, _detect_default_branch, _get_file_tree
            _scm_set_token_rc(_gl_token)
        else:
            if _SCM_RC == "github":
                from tools.github_tools import github_read_file as gitlab_read_file
                from tools.github_tools import _detect_default_branch, _get_file_tree
            else:
                from tools.gitlab_tools import gitlab_read_file, _detect_default_branch, _get_file_tree
        if not branch:
            branch = _detect_default_branch(repo)

        # ── 0. Authoritative manifest (.sdlc.yml) — skip heuristics if present ──
        manifest = _read_manifest_from_scm(repo, branch)

        # ── 1. Fetch FULL file tree (all depths) ────────────────
        all_paths = _get_file_tree(repo, branch)
        # Pass through _SKIP_DIRS filtering — _detect_all_stacks handles this internally,
        # but we also filter here for file_tree display (agent prompt context)
        filtered = [
            p for p in all_paths
            if not any(seg in p.split("/") for seg in _SKIP_DIRS)
            and not p.endswith((".lock", ".sum", ".ico", ".png", ".jpg",
                                ".jpeg", ".gif", ".svg", ".woff", ".ttf",
                                ".woff2", ".map", ".min.js", ".min.css"))
        ]
        file_tree = "\n".join(filtered)  # full filtered tree — _llm now sends in full; model decides

        # ── 2. Read config file contents at ANY depth ─────────────
        # Used for precise framework detection (package.json deps, pom.xml artifacts, etc.)
        # Once a definitive build file (pom.xml, build.gradle, go.mod, Cargo.toml) is
        # successfully read we break the outer loop — no point firing 404 API calls for
        # the remaining file types.
        config_contents: dict[str, str] = {}
        config_display_parts = []
        _definitive_found = False
        for cfg_file in _CONTEXT_FILES:
            if _definitive_found:
                break
            candidates = [p for p in filtered if p == cfg_file or p.endswith("/" + cfg_file)]
            candidates.sort(key=lambda x: (x.count("/"), x))
            for candidate in candidates:
                content = gitlab_read_file(repo, candidate, branch)
                if content and not content.startswith("[Error"):
                    config_contents[candidate] = content
                    # Show up to 10K chars so deep dependency/plugin blocks are visible
                    config_display_parts.append(f"=== {candidate} ===\n{content[:10_000]}")
                    if cfg_file in _DEFINITIVE_BUILD_FILES:
                        _definitive_found = True
                    break  # prefer shallowest; continue to next _CONTEXT_FILES entry
        config_summary = "\n\n".join(config_display_parts)

        # ── 3. Detect ALL stacks with actual config content ───────
        if manifest:
            # .sdlc.yml is authoritative — create a single-stack result
            stacks = [{
                "language":      manifest["language"],
                "framework":     manifest["framework"],
                "test_framework": manifest["test_framework"],
                "src_count": 1, "test_count": 0,
                "root_dirs": ["."], "config_found": True, "config_path": ".sdlc.yml",
            }]
        else:
            stacks = _detect_all_stacks(filtered, config_contents)

        if not stacks:
            # GitLab returned 0 files (repo unreachable / circuit open) — fall back to
            # indexed document_embeddings before giving up entirely.
            _manifest_idx = _read_manifest_from_indexed(repo)
            if _manifest_idx:
                _lang, _fw, _tests = _manifest_idx["language"], _manifest_idx["framework"], _manifest_idx["test_framework"]
                logger.warning(
                    f"_fetch_repo_context: GitLab returned 0 files for {repo!r} — "
                    f"using indexed .sdlc.yml manifest (lang={_lang})"
                )
                return _write_lang_cache({
                    "language": _lang, "framework": _fw, "test_framework": _tests,
                    "all_stacks": [], "file_tree": file_tree, "config_summary": config_summary,
                    "tech_stack": f"{_lang.capitalize()} / {_fw}" if _fw else _lang.capitalize(),
                    "confidence": 1.0, "detection_source": "manifest_indexed",
                })
            _lang, _fw, _tests = _detect_lang_from_indexed(repo)
            if _lang:
                logger.warning(
                    f"_fetch_repo_context: GitLab returned 0 files for {repo!r} — "
                    f"using indexed file path heuristic (lang={_lang}, confidence=0.6)"
                )
                return _write_lang_cache({
                    "language": _lang, "framework": _fw, "test_framework": _tests,
                    "all_stacks": [], "file_tree": file_tree, "config_summary": config_summary,
                    "tech_stack": f"{_lang.capitalize()} / {_fw}" if _fw else _lang.capitalize(),
                    "confidence": 0.6, "detection_source": "indexed_fallback",
                })
            reason = (
                f"GitLab returned {len(all_paths)} files for repo={repo!r} "
                f"but no recognised source language was found. "
                "Add source files or a .sdlc.yml manifest to the repo root."
            )
            logger.critical(f"SDLC: LANGUAGE UNDETECTABLE — {reason}")
            return {
                "language": None, "framework": None, "test_framework": None,
                "all_stacks": [], "file_tree": file_tree,
                "config_summary": config_summary, "tech_stack": "unknown",
                "confidence": 0.0, "detection_source": "none", "error": reason,
            }

        primary = stacks[0]
        language      = primary["language"]
        framework     = primary["framework"]
        test_framework = primary["test_framework"]

        # Human-readable tech stack (includes all languages for monorepos)
        if len(stacks) > 1:
            stack_parts = [
                f"{s['language'].capitalize()}/{s['framework']}" for s in stacks[:4]
            ]
            tech_stack = " + ".join(stack_parts)
        else:
            tech_stack = (
                f"{language.capitalize()} / {framework} / {test_framework}"
                if framework else f"{language.capitalize()} / {test_framework}"
            )

        logger.info(
            f"_fetch_repo_context: repo={repo} stacks={[s['language'] for s in stacks]} "
            f"primary={language}/{framework} files={len(all_paths)} confidence=1.0"
        )
        return _write_lang_cache({
            "language":       language,
            "framework":      framework,
            "test_framework": test_framework,
            "all_stacks":     stacks,
            "file_tree":      file_tree,    # 500-line truncated view (backward-compat)
            "all_paths":      filtered,     # full filtered path list for relevant-tree building
            "config_summary": config_summary,
            "tech_stack":     tech_stack,
            "branch":         branch,
            "confidence":     1.0,
            "detection_source": "gitlab",
        })

    except Exception as e:
        logger.error(f"SDLC: GitLab fetch FAILED for repo={repo!r}: {e}")
        # Try manifest from indexed content (authoritative .sdlc.yml)
        manifest = _read_manifest_from_indexed(repo)
        if manifest:
            lang, fw, tests = manifest["language"], manifest["framework"], manifest["test_framework"]
            tech_stack = f"{lang.capitalize()} / {fw} / {tests}" if fw else f"{lang.capitalize()}"
            logger.warning(
                f"_fetch_repo_context: GitLab unreachable for repo={repo!r} — "
                f"using indexed .sdlc.yml manifest (lang={lang})"
            )
            return _write_lang_cache({
                "language": lang, "framework": fw, "test_framework": tests,
                "file_tree": "", "config_summary": "", "tech_stack": tech_stack,
                "confidence": 1.0, "detection_source": "manifest_indexed",
            })
        lang, fw, tests = _detect_lang_from_indexed(repo)
        if not lang:
            reason = (
                f"GitLab unreachable for repo={repo!r} ({e}) AND no indexed files found. "
                "Ensure GITLAB_TOKEN is set and repo is in 'namespace/project' format, "
                "OR index the codebase via CodebaseManager first."
            )
            logger.critical(f"SDLC: LANGUAGE UNDETECTABLE — {reason}")
            return {
                "language": None, "framework": None, "test_framework": None,
                "file_tree": "", "config_summary": "", "tech_stack": "unknown",
                "confidence": 0.0, "detection_source": "none",
                "error": reason,
            }
        tech_stack = f"{lang.capitalize()} / {fw} / {tests}" if fw else f"{lang.capitalize()}"
        logger.warning(
            f"_fetch_repo_context: GitLab unreachable for repo={repo!r} — "
            f"using indexed file path heuristic (lang={lang}, confidence=0.6)"
        )
        return _write_lang_cache({
            "language": lang, "framework": fw, "test_framework": tests,
            "file_tree": "", "config_summary": "", "tech_stack": tech_stack,
            "confidence": 0.6, "detection_source": "indexed_fallback",
        })


def _fetch_repo_context(repo: str, branch: str = None, user_id: str = "", user_email: str = "",
                        kind: str = "editable") -> dict:
    """
    Multi-repo-aware wrapper around _fetch_single_repo_context.

    Returns: {repo: inner_ctx_dict}

    The inner dict shape is unchanged from the single-repo era so callers that
    already read repo_ctx["language"] are updated to repo_ctx[repo]["language"]
    via repo_ctx_for().

    kind parameter:
      "editable"     (default) — confidence=0.0 is a hard block; the pipeline
                                 must not proceed when language is unknown for a
                                 repo the coder will modify.
      "compile-only" — confidence=0.0 is tolerated (soft-warn only); the repo is
                                 used only for its build artifacts, so language
                                 detection failure does not block the run.
    """
    inner = _fetch_single_repo_context(repo, branch=branch, user_id=user_id, user_email=user_email)
    if kind == "compile-only" and inner.get("confidence", 1.0) == 0.0:
        inner = dict(inner)
        inner["confidence"] = 0.0
        inner["compile_only_soft_warn"] = True
    return {repo: inner}


def repo_ctx_for(run: dict, repo: str) -> dict:
    """
    Return the per-repo context dict for `repo` in a given run.

    Lookup order:
      1. sdlc_run_repos.repo_ctx for (run_id, repo) via store.sdlc_store.list_run_repos
      2. run["context"]["repo_ctx"][repo] — new multi-repo shape
      3. run["context"]["repo_ctx"]        — legacy single-repo flat dict (backward compat)

    Returns an empty dict if no context is found.
    """
    run_id = run.get("id", "") if isinstance(run, dict) else ""

    if run_id:
        try:
            from store import sdlc_store as _ss
            rows = _ss.list_run_repos(run_id)
            for row in rows:
                if row.get("repo") == repo:
                    ctx = row.get("repo_ctx") or {}
                    if ctx and isinstance(ctx, dict) and ctx.get("language"):
                        return ctx
        except Exception as _e:
            logger.debug(f"repo_ctx_for: sdlc_run_repos lookup failed ({_e})")

    ctx_block = (run.get("context") or {}).get("repo_ctx", {}) if isinstance(run, dict) else {}
    if isinstance(ctx_block, dict):
        if repo in ctx_block and isinstance(ctx_block[repo], dict):
            return ctx_block[repo]
        # Legacy single-repo flat dict: has "language" at top level, not a nested key
        if "language" in ctx_block:
            return ctx_block
    return {}


# ── GitLab repo resolver ───────────────────────────────────────

def _resolve_gitlab_repo(repo_name: str) -> str:
    """
    Convert an indexed repo name (e.g. 'payment-service') to a GitLab
    'namespace/project' string (e.g. 'ainxt/payment-service').

    Resolution order:
      1. Empty repo_name → return "" (caller must validate before calling)
      2. If repo_name already contains '/' → already a valid namespace/project.
      3. Look up the git URL stored in Redis by index_data.py
         (key: 'index:repo:{name}:url') and extract namespace/project from it.
      4. Return repo_name as-is (pipeline will fail clearly with a
         meaningful GitLab API error rather than silently using a wrong repo).

    NOTE: GITLAB_REPO env-var fallback has been intentionally removed.
    Tickets MUST include 'repo: namespace/project-name' in the description.
    """
    if not repo_name:
        return ""  # caller (webhook) validates this before enqueuing
    if "/" in repo_name:
        return repo_name  # already in owner/repo format
    # Try Redis lookup (db=3, where index_data.py stores git URLs)
    try:
        import redis as _redis
        _rc = _redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=3,
                           decode_responses=True, socket_connect_timeout=2)
        git_url = _rc.get(f"index:repo:{repo_name}:url") or ""
        if git_url:
            url = git_url.rstrip("/")
            if url.endswith(".git"):
                url = url[:-4]
            parts = url.split("/")
            if len(parts) >= 2:
                owner_repo = f"{parts[-2]}/{parts[-1]}"
                logger.info(f"_resolve_gitlab_repo: '{repo_name}' → '{owner_repo}' (from Redis)")
                return owner_repo
    except Exception:
        pass
    # Return as-is — the pipeline will get a clear GitLab 404 if it's wrong
    logger.warning(f"_resolve_gitlab_repo: could not resolve '{repo_name}' to namespace/project — using as-is")
    return repo_name


# ── Pre-fetch helpers — guarantee real code reaches every LLM call ──────────
#
# The audit showed that telling agents "please call gitlab_read_file" is fake
# tool use — agents hallucinate tool results when they don't have the schemas
# or when the AgentRunner falls back to plain _llm().
#
# The fix: Python code reads GitLab files DIRECTLY before every LLM call and
# injects the full content into the prompt.  The LLM receives ACTUAL code.
# No tool-use theater, no hallucinated file contents, no 2KB truncation.

# File extensions where content is structurally sequential (every line matters).
# For these we snap truncation to line / statement boundaries instead of raw
# char offsets so the LLM never sees a half-written CREATE TABLE or YAML key.
_STRUCTURED_EXTS = frozenset({
    ".sql", ".yaml", ".yml", ".json", ".xml", ".toml", ".hcl", ".tf",
    ".properties", ".ini", ".conf",
})


def _read_jira_full(key: str, user_id: str = "", user_email: str = "") -> dict:
    """Fetch the complete Jira ticket — description, acceptance criteria, comments."""
    if not key:
        return {}
    if not user_id and not user_email:
        user_id, user_email = _get_run_user()
    try:
        from tools.jira_tools import jira_get_issue_dict
        result = jira_get_issue_dict(key, user_id=user_id, user_email=user_email)
        if result:
            return result
        logger.error(
            f"_read_jira_full({key!r}): JIRA returned empty — ticket does not exist or credentials missing. "
            "Set JIRA_EMAIL + JIRA_API_TOKEN in .env and verify the ticket key is correct."
        )
        return {}
    except Exception as e:
        logger.error(f"_read_jira_full({key!r}) FAILED: {e}")
        raise


def _jira_description(ticket, fallback: str = "") -> str:
    """Extract description string from a Jira ticket dict (jira_get_issue_dict format)."""
    if not isinstance(ticket, dict):
        return fallback
    return (
        ticket.get("description", "")
        or ticket.get("fields", {}).get("description", "")
        or fallback
    )


# ── Jira helpers ──────────────────────────────────────────────

def _jira_comment(issue_key: str, comment: str, user_id: str = "", user_email: str = ""):
    if not issue_key:
        return
    if not user_id and not user_email:
        user_id, user_email = _get_run_user()
    try:
        from tools.jira_tools import jira_add_comment
        jira_add_comment(issue_key, comment, user_id=user_id, user_email=user_email)
    except Exception as e:
        logger.warning(f"SDLC: jira_comment failed → {e}")


# ── Inbox publish ─────────────────────────────────────────────

def _inbox_notify(run_id: str, stage: str, summary: str, data: dict = None):
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id="platform",
            type="sdlc_approval_required",
            title=f"[SDLC/{stage.upper()}] {summary[:120]}",
            body=summary,
            source_id=run_id,
            metadata={
                "run_id": run_id,
                "stage":  stage,
                "data":   data or {},
            },
        )
    except Exception as e:
        logger.warning(f"SDLC: inbox_notify failed → {e}")


def _update_origin_inbox(run_id: str, confluence_url: str):
    """
    Update the original 'pipeline started' inbox item (created by threads_router)
    with the real Confluence URL once the design page is available.
    """
    if not confluence_url:
        return
    try:
        from store.inbox_store import update_inbox_item
        _item_id = (get_run(run_id) or {}).get("context", {}).get("inbox_item_id", "")
        if not _item_id:
            return
        # Fetch the item body and replace the placeholder with the real link
        # Search for the item directly
        from db.database import SessionLocal as _SL2
        from db.models import InboxItem as _II
        _db = _SL2()
        try:
            _item = _db.query(_II).filter(_II.id == _item_id).first()
            if not _item:
                return
            _new_body = _item.body.replace(
                "_A Confluence design doc will be linked here once solution design is complete._",
                f"[📄 View Confluence Design Doc]({confluence_url})",
            )
            # Also append if placeholder wasn't found (Teams-triggered runs)
            if _new_body == _item.body and confluence_url not in _item.body:
                _new_body = _item.body + f"\n\n[📄 View Confluence Design Doc]({confluence_url})"
        finally:
            _db.close()
        update_inbox_item(_item_id, body=_new_body, metadata={"confluence_url": confluence_url})
        logger.info(f"SDLC {run_id}: inbox item {_item_id} updated with Confluence URL")
    except Exception as e:
        logger.warning(f"SDLC {run_id}: _update_origin_inbox failed → {e}")


def _teams_notify(run_id: str, message: str = "", *,
                  hitl: bool = False, stage: str = "", summary: str = ""):
    """
    Push a notification to the Teams conversation where this run was triggered.
    Only fires when triggered_by starts with 'teams:' — silently skips otherwise.
    For HITL gates use hitl=True + stage to send an Adaptive Card with Approve/Reject.
    """
    try:
        run = get_run(run_id)
        if not run:
            return
        if not run.get("triggered_by", "").startswith("teams:"):
            return
        ctx         = run.get("context", {})
        conv_id     = ctx.get("teams_conv_id", "")
        service_url = ctx.get("teams_service_url", "")
        if not conv_id or not service_url:
            return
        from integrations.teams_client import send_message, send_adaptive_card, build_hitl_card
        if hitl and stage:
            card = build_hitl_card(run_id, stage, summary or message)
            send_adaptive_card(service_url, conv_id, card)
        else:
            send_message(service_url, conv_id, message)
    except Exception as e:
        logger.warning(f"SDLC: teams_notify failed → {e}")


# ============================================================
# WS-5 — agentic pull for analyze/design
# Pull is the ONLY path for ANALYZING and DESIGNING. The explore loop reads ≤N
# files itself against the real checkout (workspace_root), with the single Opus
# synthesis producing the analysis/design JSON. A pull-loop failure SUSPENDS the
# run (HITL) — there is no silent fallback to a stuffed-context path.
# ============================================================

# Phase 5 (ctx-migration): cap on the stuffed exploration block fed into the
# classify/triage prompt. The old block stuffed full entry files (~342k), which
# made the classify call ~438k and tripped MessageSizeTooLargeError. Hardcoded —
# no runtime tuning needed (Invariant #2).
_EXPLORATION_BLOCK_MAX_CHARS = 40_000


def _resolve_required_against_workspace(expected, workspace_root: str = "",
                                        new_files=None) -> tuple:
    """Step 2: filter the required-read set to REALITY against the materialized
    workspace checkout (NOT GitLab).

    Drops paths absent from disk (foreign/hallucinated paths like ABStudio/… can
    never be read, so requiring them yields FALSE incompleteness) and excludes any
    path in `new_files` (a file to be CREATED can't be read). Returns
    (kept: list[str], dropped_nonexistent: list[str], excluded_new: list[str]).

    Conservative: when `workspace_root` is empty/missing we cannot verify existence
    on disk, so we keep every non-new path (prior behavior) rather than over-filter
    and re-introduce an ungrounded analysis. Pure / Windows-safe."""
    import os as _os
    _new = {(_s(p) or "").replace("\\", "/").lstrip("/")
            for p in (new_files or []) if isinstance(p, str) and p.strip()}
    kept: list = []
    dropped_nonexistent: list = []
    excluded_new: list = []
    for p in (expected or []):
        if not isinstance(p, str) or not p.strip():
            continue
        rel = p.replace("\\", "/").lstrip("/")
        if rel in _new:
            excluded_new.append(p)
            continue
        if workspace_root:
            full = _os.path.join(workspace_root, rel)
            if not _os.path.isfile(full):
                dropped_nonexistent.append(p)
                continue
        kept.append(p)
    return kept, dropped_nonexistent, excluded_new


# ============================================================
# New pipeline stage functions (Part 3)
# ============================================================

def _phase_normalize_ticket(run_id: str, jira_key: str, issue: dict,
                             repo_ctx: dict, workspace_root: str = "") -> tuple:
    """TICKET_NORMALIZATION stage — convert raw Jira ticket to structured WorkItem.

    Returns (work_item, open_questions). If open_questions is non-empty,
    the pipeline transitions to AWAITING_USER_INPUT; the caller persists
    the work_item + open_questions and pauses.

    If jira_get_issue_full fails, falls back to jira_dict built from issue.
    """
    from agents.sdlc_normalizer import NormalizationAgent
    _transition(run_id, "TICKET_NORMALIZATION", "normalizer-agent")
    user_id = (
        issue.get("triggered_by_user_id", "")
        or issue.get("user_id", "")
    )
    user_email = (
        issue.get("triggered_by_email", "")
        or issue.get("user_email", "")
    )
    try:
        from tools.jira_tools import jira_get_issue_full
        jira_dict = jira_get_issue_full(jira_key, user_id=user_id, user_email=user_email)
    except Exception as _je:
        logger.warning(f"[NORM {run_id}] jira_get_issue_full failed ({_je}) — using issue dict")
        jira_dict = {}
    if not jira_dict:
        # jira_get_issue_full returned empty (Jira not reachable or creds missing);
        # try _read_jira_full which has _get_run_user() fallback built in.
        try:
            jira_dict = _read_jira_full(jira_key, user_id=user_id, user_email=user_email)
        except Exception as _rjf_err:
            logger.warning(f"[NORM {run_id}] _read_jira_full also failed ({_rjf_err}) — using raw issue dict")
            jira_dict = {}
    if not jira_dict:
        jira_dict = {
            "key": jira_key,
            "summary": issue.get("summary", ""),
            "description": issue.get("description", ""),
            "comments": [],
            "acceptance_criteria": "",
            "labels": [],
            "epic_summary": "",
            "raw_fields": {},
        }
    agent = NormalizationAgent(run_id=run_id)
    work_item, open_questions = agent.normalize(jira_dict, repo_ctx, workspace_root)
    return work_item, open_questions


def _phase_validate_manifest(run_id: str, jira_key: str, work_item_dict: dict,
                              design: dict, analysis: dict,
                              workspace_root: str = "") -> tuple:
    """MANIFEST_VALIDATION stage — structural + OpenAI cross-check of the change manifest.

    Returns (passed: bool, issues: list[str]).
    Structural check is deterministic (no LLM).
    OpenAI cross-check is gated by SDLC_MANIFEST_VALIDATION_ENABLED (default true).
    """
    _transition(run_id, "MANIFEST_VALIDATION", "manifest-validator")
    issues = []
    file_changes = (design.get("file_changes") or design.get("files_to_change") or
                    analysis.get("files_to_change") or [])
    new_files = design.get("new_files_needed") or analysis.get("new_files_needed") or []
    affected = analysis.get("affected_components") or []
    # The PLAN dict (passed as both `design` and `analysis`) carries the reasoning the
    # cross-check needs to avoid false positives — it was previously discarded:
    #   • ruled_out: files the plan DELIBERATELY excluded, with a reason. A scope-listed
    #     file that appears here is NOT "missing" — it was a reasoned decision.
    #   • solution_approach / code_structure: WHY each manifest file is touched, so
    #     mandatory companion files (e.g. db/migrate.py for a schema change) are not
    #     read as out-of-scope just because the scope bullets didn't enumerate them.
    ruled_out = design.get("ruled_out") or analysis.get("ruled_out") or []
    solution_approach = _s(design.get("solution_approach") or analysis.get("solution_approach") or "")
    code_structure = _s(design.get("code_structure") or analysis.get("code_structure") or "")

    # WS-3/WS-4 (gate-reorder, 2026-07-02): promote MANIFEST_VALIDATION to a
    # first-class stage that STORES an artifact (P5 — the verdict was previously
    # never persisted/shown), matching the ManifestValidationPanel.jsx contract:
    # struct_pass, openai_pass, struct_failures, hallucinated_paths,
    # missing_components, oos_violations, openai_issues. `_finish` wraps every
    # return point so the return-shape contract (passed, issues) stays unchanged
    # for the one existing caller (_run_plan_phase).
    def _finish(passed: bool, out_issues: list, **details) -> tuple:
        try:
            from store.sdlc_artifacts import _store_artifact as _mv_sa, compute_input_hash as _mv_cih
            _mv_sa(
                run_id=run_id, stage="MANIFEST_VALIDATION",
                payload={"passed": passed, "issues": out_issues, **details},
                producer="ai:manifest-validator",
                input_hash=_mv_cih(run_id, "MANIFEST_VALIDATION"),
                created_by="system",
            )
        except Exception as _mvae:
            logger.warning(f"[SDLC {run_id}] MANIFEST_VALIDATION artifact store failed (non-fatal): {_mvae}")
        return passed, out_issues

    # WS-3: for complexity=="simple" runs, skip the OpenAI cross-check (Step 2) —
    # the structural check alone is enough signal for a small, low-blast-radius
    # change. Read complexity off the run's stored classification.
    _complexity = str(
        (get_run(run_id) or {}).get("context", {}).get("classification", {}).get("complexity") or ""
    ).strip().lower()

    # ── FULL INPUT DUMP (no stripping) — so a structural failure (which returns
    #    before the LLM prompt is built) is just as diagnosable as an LLM reject.
    try:
        _input_dump = json.dumps({
            "work_item": work_item_dict,
            "file_changes": file_changes,
            "new_files_needed": new_files,
            "affected_components": affected,
            "workspace_root": workspace_root,
        }, indent=2, default=str)
    except Exception as _de:
        _input_dump = f"<unserializable inputs: {_de}>"
    logger.info(
        f"[MANIFEST-INPUT {run_id}] ===== FULL VALIDATION INPUT BEGIN =====\n"
        f"{_input_dump}\n"
        f"[MANIFEST-INPUT {run_id}] ===== FULL VALIDATION INPUT END ====="
    )

    # ── Step 1: Structural validation (deterministic, no LLM) ──
    # Path existence is decided HERE, against real disk — this is the authoritative,
    # deterministic hallucination check. The Step-2 LLM cross-check no longer judges
    # path existence (it saw a pruned/head-capped tree and false-flagged real
    # root-level files like gateway.py); struct_missing_paths is what feeds the
    # artifact's hallucinated_paths field.
    covered_affected = set()
    struct_missing_paths: list = []
    for fc in file_changes:
        path = _s(fc.get("path") or fc) if isinstance(fc, dict) else _s(fc)
        if not path:
            continue
        # Check file exists in workspace
        if workspace_root:
            import os as _os
            full = _os.path.join(workspace_root, path.lstrip("/"))
            exists = _os.path.isfile(full)
        else:
            exists = True  # no workspace: cannot verify, assume OK
        logger.info(f"[MANIFEST-STRUCT {run_id}] path_check: {path!r} → exists={exists}")
        if not exists and path not in (new_files or []):
            issues.append(f"path not found in workspace: {path}")
            struct_missing_paths.append(path)
        # Check affected component coverage
        for af in affected:
            af_s = _s(af)
            if af_s and (af_s in path or path.endswith(af_s)):
                covered_affected.add(af_s)
        # Check non-empty change_description
        if isinstance(fc, dict):
            if not (fc.get("change_description") or fc.get("description") or fc.get("reason")):
                issues.append(f"missing change_description for: {path}")

    n_covered = len(covered_affected)
    n_total = len(affected)
    logger.info(f"[MANIFEST-STRUCT {run_id}] coverage: {n_covered}/{n_total} affected_components covered")
    struct_pass = len(issues) == 0
    logger.info(f"[MANIFEST-STRUCT {run_id}] result: pass={struct_pass} failures={len(issues)}")

    if not struct_pass:
        return _finish(False, issues, struct_pass=False, openai_pass=None,
                       struct_failures=issues, hallucinated_paths=struct_missing_paths)

    # ── Step 2: OpenAI cross-validation (gated; skipped for simple complexity) ──
    enabled = os.getenv("SDLC_MANIFEST_VALIDATION_ENABLED", "true").lower() not in ("false", "0", "no")
    if not enabled:
        logger.info(f"[MANIFEST-OPENAI {run_id}] SDLC_MANIFEST_VALIDATION_ENABLED=false — skipping")
        return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[])
    if _complexity == "simple":
        logger.info(f"[MANIFEST-OPENAI {run_id}] complexity=simple — skipping OpenAI cross-check (WS-3)")
        return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[], skipped_reason="simple_complexity")

    try:
        manifest_summary = json.dumps({
            "file_changes": [_s(fc) if not isinstance(fc, dict) else fc for fc in file_changes[:20]],
            "new_files_needed": [_s(f) for f in new_files[:10]],
        }, indent=2)
        wi_problem = work_item_dict.get("problem_statement", "")
        wi_scope = work_item_dict.get("scope", [])
        wi_oos = work_item_dict.get("out_of_scope", [])
        # Path existence is NOT judged here. Step 1 already verified every manifest
        # path against real disk (the authoritative, deterministic check). This LLM
        # cross-check used to receive a repo file tree and flag "hallucinated" paths
        # against it — but the tree was pruned/head-capped (≤40K) and routinely dropped
        # real root-level files (e.g. gateway.py), false-rejecting a valid manifest.
        # The tree and the hallucinated_paths judgement are gone; the cross-check now
        # judges ONLY scope adherence and affected-component coverage.
        # ── Build the two reasoning blocks that stop the historical false positives ──
        # (1) DELIBERATELY EXCLUDED: a scope-listed file that the plan ruled out (with a
        #     reason) is NOT missing — surfacing this stops the "auth/rbac.py missing"
        #     class of false reject, where the plan verified an already-correct file.
        if ruled_out:
            _ro_lines = []
            for _r in ruled_out:
                if isinstance(_r, dict):
                    _rp = _s(_r.get("path") or _r.get("file") or "")
                    _rr = _s(_r.get("reason") or _r.get("rationale") or "")
                    if _rp:
                        _ro_lines.append(f"  - {_rp}: {_rr}" if _rr else f"  - {_rp}")
                elif _s(_r):
                    _ro_lines.append(f"  - {_s(_r)}")
            ruled_out_block = (
                "Files the plan DELIBERATELY EXCLUDED (do NOT report these as missing — "
                "each was a reasoned decision, e.g. the file was inspected and needs no change):\n"
                + "\n".join(_ro_lines) + "\n\n"
            ) if _ro_lines else ""
        else:
            ruled_out_block = ""
        # (2) RATIONALE: why each manifest file is touched. Lets the reviewer see that a
        #     file not literally enumerated in the scope bullets (e.g. db/migrate.py, a
        #     SQL catch-up script, a test file) is a mandatory companion of an in-scope
        #     change — not an out-of-scope excursion.
        rationale_block = ""
        if solution_approach:
            rationale_block += f"Solution approach:\n{solution_approach[:1500]}\n\n"
        if code_structure:
            rationale_block += f"Per-file change rationale:\n{code_structure[:2500]}\n\n"
        cross_prompt = (
            f"You are a senior engineer reviewing a change manifest. Every file path has "
            f"ALREADY been verified to exist — do NOT judge or flag path existence.\n\n"
            f"Work Item Problem: {wi_problem}\n"
            f"In Scope: {wi_scope}\n"
            f"Out of Scope: {wi_oos}\n\n"
            f"{ruled_out_block}"
            f"{rationale_block}"
            f"Manifest (files to change):\n{manifest_summary}\n\n"
            f"Judge ONLY: (1) does any change fall MATERIALLY OUTSIDE the stated scope, and "
            f"(2) is any in-scope component MISSING from the manifest.\n"
            f"Judging rules — read carefully, these prevent false positives:\n"
            f"- The 'In Scope' list is HIGH-LEVEL GUIDANCE, not an exhaustive whitelist. "
            f"Mandatory companion files that directly serve an in-scope change are IN scope "
            f"even if not individually named — e.g. the DB migration runner (db/migrate.py) "
            f"and SQL catch-up scripts always accompany a schema change; test files always "
            f"accompany code changes. Do NOT flag these as out-of-scope.\n"
            f"- A file listed under 'DELIBERATELY EXCLUDED' above is NOT missing. Never "
            f"report a ruled-out file as a missing component.\n"
            f"- Only report an out-of-scope violation for a change that is UNRELATED to the "
            f"work item or explicitly named under 'Out of Scope'. When in doubt, do not flag.\n"
            f"Answer ONLY in JSON: "
            f'{{ "valid": true/false, "missing_components": [], '
            f'"out_of_scope_violations": [], "issues": [] }}'
        )
        # Step 1: resolve the judge model through config. _sdlc_model returns the
        # TIER (default "deep" → gpt-5.5, env-overridable via SDLC_MODEL_MANIFEST_VALIDATE);
        # openai_model_for_tier maps it to a CONCRETE OpenAI model id. This validator
        # calls the OpenAI gateway directly, so a Claude tier must fall back to the
        # latest OpenAI model — sending a Claude id (or None) 400s.
        from core.model_registry import openai_model_for_tier as _omft
        _mv_hint = _sdlc_model("manifest_validate")
        _mv_model, _mv_fellback = _omft(_mv_hint)
        _mv_source = (
            "fallback" if _mv_fellback
            else ("env" if os.getenv("SDLC_MODEL_MANIFEST_VALIDATE") else "default")
        )
        if _mv_fellback:
            logger.warning(
                "[MANIFEST-OPENAI] judge tier has no OpenAI model — using latest",
                run_id=run_id, requested_hint=_mv_hint, model=_mv_model,
            )
        logger.info(
            "[MANIFEST-OPENAI] judge model resolved",
            run_id=run_id, model=_mv_model, source=_mv_source,
        )
        prompt_chars = len(cross_prompt)
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] sending manifest model={_mv_model} prompt_chars={prompt_chars}"
        )
        # ── FULL PROMPT DUMP (no stripping / no truncation) — for diagnosing why the
        #    manifest cross-check rejects. Delimited so it is easy to extract from logs.
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION PROMPT BEGIN "
            f"(chars={prompt_chars}) =====\n{cross_prompt}\n"
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION PROMPT END ====="
        )
        # 2026-07-07 (scoped Fix 2) / 2026-07-13 (Step 1): call the resolved judge model
        # (_mv_model — default gpt-5.5) with its model id EXPLICITLY set for THIS
        # cross-check only. The shared medium router path (_try_openai_coding) sends
        # model=None to the proxy → 400 on every medium call; rather than change that
        # universal path, this best-effort validation calls the OpenAI gateway directly
        # with the model. On any call failure `raw` stays empty and the cross-check
        # SKIPs gracefully below (non-blocking gate).
        from models.model_router import model_router as _mr
        _gw = _mr._get_openai()

        def _judge_call(_p: str) -> str:
            """Call the OpenAI judge once with the resolved model; account cost;
            return raw text ('' on any failure — non-blocking)."""
            if _gw is None:
                logger.warning(f"[MANIFEST-OPENAI {run_id}] no OpenAI gateway available — cross-check skipped")
                return ""
            try:
                _r = _mr._collect(_gw.generate(_p, model=_mv_model)) or ""
            except Exception as _ce:
                logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-check call failed (non-fatal): {_ce}")
                return ""
            # Best-effort cost/budget accounting (mirrors _llm's estimate) since this
            # bypasses _llm. Cost tier tracks the ACTUAL model used — deep rate when the
            # deep/fallback model runs (Step 1). Never fatal.
            try:
                from core.model_registry import tier_cost_per_1m as _tc
                _ti, _to = len(_p) // 4, (len(_r) // 4 if _r else 0)
                _ri, _ro = _tc("deep" if _mv_fellback else _mv_hint)
                _mv_cost = (_ti / 1_000_000 * _ri) + (_to / 1_000_000 * _ro)
                from services.sdlc_budget_tracker import record_llm_cost as _rec_cost
                _rec_cost(_ti, _to, round(_mv_cost, 6), run_id=run_id)
            except Exception:
                pass
            return _r

        # NOTE: the OpenAI gateway generate() exposes NO response_format / JSON-mode
        # kwarg (confirmed against _ProxyGateway.generate — the production path via
        # the LLM proxy), so Step 2 relies on the strict-shape RE-ASK below rather than a
        # structured-output request.
        raw = _judge_call(cross_prompt)
        # ── FULL RESPONSE DUMP (no stripping / no truncation) ──
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION RESPONSE BEGIN "
            f"(chars={len(raw or '')}) =====\n{raw}\n"
            f"[MANIFEST-OPENAI {run_id}] ===== FULL CROSS-VALIDATION RESPONSE END ====="
        )
        # Step 7: do NOT fail open. A compliance-blocked or unparseable cross-check
        # yields NO verdict — skip it (non-blocking, best-effort), never silently
        # treat the absence of a verdict as a PASS.
        from agents.compliance_engine import is_compliance_block
        if is_compliance_block(raw):
            logger.warning(
                "[SDLC] manifest cross-check SKIPPED", run_id=run_id, reason="compliance_block"
            )
            logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-check compliance-blocked — SKIPPED (no verdict)")
            return _finish(True, [], struct_pass=True, openai_pass=None, struct_failures=[], skipped_reason="compliance_block")
        result = _parse_json(raw)
        if not isinstance(result, dict) or "valid" not in result:
            # Step 2: ONE re-ask before giving up — the judge may have wrapped the JSON
            # in prose. Re-call appending a strict shape instruction (no response_format
            # kwarg exists on the gateway, so a re-ask is the only lever).
            logger.warning(f"[MANIFEST-OPENAI {run_id}] first verdict unparseable — re-asking once")
            _reask_prompt = (
                cross_prompt
                + "\n\nReturn ONLY a JSON object of the exact shape "
                  '{"valid": true/false, "missing_components": [], '
                  '"out_of_scope_violations": [], "issues": []} — no prose, no code fences.'
            )
            raw2 = _judge_call(_reask_prompt)
            logger.info(
                f"[MANIFEST-OPENAI {run_id}] ===== RE-ASK RESPONSE BEGIN "
                f"(chars={len(raw2 or '')}) =====\n{raw2}\n"
                f"[MANIFEST-OPENAI {run_id}] ===== RE-ASK RESPONSE END ====="
            )
            # A compliance-blocked re-ask is still NO verdict — never parse it as one.
            if is_compliance_block(raw2):
                logger.warning(
                    "[SDLC] manifest cross-check SKIPPED", run_id=run_id,
                    reason="compliance_block_on_retry",
                )
                return _finish(True, [], struct_pass=True, openai_pass=None,
                               struct_failures=[], skipped_reason="compliance_block")
            result = _parse_json(raw2)
            if not isinstance(result, dict) or "valid" not in result:
                # Still unparseable → keep the non-blocking SKIP (a flaky judge must not
                # dead-end the pipeline) BUT make it LOUD + ATTRIBUTED so the operator
                # sees the gate was effectively bypassed. This is "no silent skip".
                logger.warning(
                    "[MANIFEST-OPENAI] judge_unparseable_after_retry",
                    run_id=run_id, raw_len=len(raw or ""),
                    skipped_reason="unparseable_after_retry",
                )
                return _finish(True, [], struct_pass=True, openai_pass=None,
                               struct_failures=[], skipped_reason="unparseable_after_retry")
        # Only a real dict carrying an explicit `valid` key produces a verdict —
        # no silent `, True` default.
        valid = result.get("valid")
        missing = result.get("missing_components") or []
        violations = result.get("out_of_scope_violations") or []
        logger.info(
            f"[MANIFEST-OPENAI {run_id}] result: valid={valid} "
            f"missing={len(missing)} violations={len(violations)}"
        )
        verdict = "PASS" if valid else "REJECT"
        logger.info(f"[MANIFEST-OPENAI {run_id}] verdict: {verdict}")
        if not valid:
            oi = [_s(x) for x in (missing + violations + (result.get("issues") or []))]
            return _finish(
                False, oi, struct_pass=True, openai_pass=False, struct_failures=[],
                hallucinated_paths=[],  # existence is Step 1's job — never from the LLM
                missing_components=[_s(x) for x in missing],
                oos_violations=[_s(x) for x in violations],
                openai_issues=[_s(x) for x in (result.get("issues") or [])],
            )
    except Exception as _ve:
        logger.warning(f"[MANIFEST-OPENAI {run_id}] cross-validation failed (non-fatal): {_ve}")

    return _finish(True, [], struct_pass=True, openai_pass=True, struct_failures=[],
                    hallucinated_paths=[], missing_components=[], oos_violations=[], openai_issues=[])


# ── Pre-gate completeness verifier (shift-left: "decide before the gate") ─────
# Required JSON keys per explore stage. A pull-loop result missing any of these
# (or that never actually READ the files it claims to change) is "thin" and must
# SUSPEND to HITL rather than be force-synthesized into the codegen stage.
# CLI three-phase-engine: the PLAN phase (CLI-driven, read-only) emits a single
# implementation plan covering BOTH the analyst and designer concerns plus the file
# list. This is now the SOLE required-keys contract — the legacy per-stage tuples
# (_ANALYST_REQUIRED_KEYS / _DESIGNER_REQUIRED_KEYS / _DIAGNOSE_REQUIRED_KEYS /
# _PLANNER_REQUIRED_KEYS) and the SDLC_PLANNER_MODE split/merged lever
# (_PLANNER_MODE / _planner_merged_enabled) were removed in the three-phase hard
# cutover, along with the _run_analyst_pull/_run_designer_pull/_run_planner_pull/
# _phase_diagnose functions that consumed them.
_PLAN_REQUIRED_KEYS = (
    "files_to_change", "sub_tasks", "implementation_spec", "solution_approach",
    "implementation_plan", "code_structure", "testing_strategy", "rollback_strategy",
)


def _canon_path(p: str) -> str:
    """Pure, Windows-safe path canonicalizer for coverage matching. Unifies
    separators (``\\`` → ``/``), strips a leading ``/``, and resolves ``./`` +
    redundant segments at the STRING level (``posixpath.normpath`` — no filesystem
    access). Returns ``""`` for falsy / non-str input.

    Note: it does NOT strip a repo-slug head prefix — this helper has no repo-slug
    context (see the canonical repo-slug form in memory
    ``project_sdlc_threefix_2026_06_10``). Path-form drift where one side carries an
    extra leading repo-slug segment is absorbed by ``_path_covered``'s basename-suffix
    rule instead."""
    if not p or not isinstance(p, str):
        return ""
    s = p.replace("\\", "/").strip().lstrip("/")
    if not s:
        return ""
    import posixpath
    norm = posixpath.normpath(s)
    if norm == ".":
        return ""
    return norm.lstrip("/")


def _path_covered(expected: str, read_paths: set) -> bool:
    """True if `expected` is covered by any path in `read_paths`. Tolerant of
    absolute-vs-relative, leading-slash, separator, and ``./`` drift (all normalized
    via ``_canon_path``). After canonicalization, coverage holds when:
      * a read path equals `expected`, or
      * one is a basename-suffix of the other (``a/b/c.py`` covered by ``c.py`` or
        ``b/c.py`` and vice-versa — absorbs repo-slug-prefix drift), or
      * `expected` names a DIRECTORY (trailing ``/`` or no ``.`` in its final segment)
        and some read path lies beneath it (``startswith expected.rstrip('/') + '/'``).
    The directory rule only ADDS coverage — it never removes an equal/suffix match, so
    no currently-passing plan starts failing. Pure / Windows-safe; ``not expected``
    short-circuits True (nothing to cover)."""
    if not expected:
        return True
    e = _canon_path(expected)
    if not e:
        return True
    # Directory detection off the ORIGINAL expected (trailing slash) or the canonical
    # final segment carrying no dot (e.g. a package/dir-level classifier guess).
    _last = e.rsplit("/", 1)[-1]
    is_dir = expected.replace("\\", "/").rstrip().endswith("/") or ("." not in _last)
    e_prefix = e.rstrip("/") + "/"
    for rp in read_paths:
        if not isinstance(rp, str):
            continue
        r = _canon_path(rp)
        if not r:
            continue
        if r == e or r.endswith("/" + e) or e.endswith("/" + r):
            return True
        if is_dir and r.startswith(e_prefix):
            return True
    return False


def _explore_convergence_verdict(stage: str, parsed, ctx,
                                 expected_files=None, required_keys=(),
                                 affected_components=None, final_text: str = ""):
    """Deterministic convergence verdict — the CORE predicate shared by the in-loop
    `propose_plan` stop signal (artifact-planning-loop Step 4) and the post-loop
    `_verify_explore_output` completeness gate.

    `parsed` is the plan as a dict: mid-loop pass `artifact.to_combined_json()`
    (the live PlanningArtifact); post-loop pass `_parse_json(final_text)`. The verdict
    splits failures into:
      * coverage_gaps  — a required key empty, or an affected_component neither in
        files_to_change/new_files_needed nor read (the thing to keep exploring).
      * grounding_gaps — a cited EXISTING file not in ctx._reads (read-set): the
        anti-hallucination defense. New files are exempt (they don't exist yet).

    Returns {ok, coverage_gaps, grounding_gaps, recoverable}. Pure (no LLM/network).
    Stop = (no coverage_gaps) ∧ (no grounding_gaps). NEVER a confidence score
    (Research Q1/Q2): `assumptions[].confidence` is audit-only and is not read here.
    """
    from agents.sdlc_agent_loop import _looks_truncated_json as _trunc_json

    coverage_gaps: list = []
    grounding_gaps: list = []

    def _is_recoverable() -> bool:
        raw_text = final_text or ""
        if raw_text and _trunc_json(raw_text):
            return True
        if isinstance(parsed, dict):
            _raw = parsed.get("raw")
            if isinstance(_raw, str) and len(_raw) > 200 and _trunc_json(_raw):
                return True
        return False

    if not isinstance(parsed, dict) or not parsed:
        return {"ok": False,
                "coverage_gaps": [f"{stage}: plan is not a valid non-empty object"],
                "grounding_gaps": [], "recoverable": _is_recoverable()}

    # (b) required keys present and non-empty → COVERAGE
    for k in required_keys:
        v = parsed.get(k)
        if v is None or (isinstance(v, (str, list, dict, tuple)) and len(v) == 0):
            coverage_gaps.append(f"required field empty: {k}")

    # read-set: tool-read paths + files whose REAL content reached the model via the
    # seed_full channel (ctx._reads["contents"]). Mirrors the original verifier (c).
    _reads = getattr(ctx, "_reads", {}) or {}
    read_paths = set(_reads.get("paths") or [])
    read_paths |= set((_reads.get("contents") or {}).keys())

    # files the plan declares NEW are exempt from the read requirement (don't exist yet)
    _new_declared = set()
    for f in (parsed.get("new_files_needed") or []):
        if isinstance(f, str) and f.strip():
            _new_declared.add(f.strip())
        elif isinstance(f, dict):
            _p = f.get("path") or f.get("file") or f.get("name")
            if _p:
                _new_declared.add(_p)

    # (c) every cited EXISTING file must be grounded (read) → GROUNDING
    expected = [p for p in (expected_files or [])
                if isinstance(p, str) and p.strip() and p not in _new_declared]
    for p in expected:
        if not _path_covered(p, read_paths):
            grounding_gaps.append(f"cited but unread: {p}")

    # (d) affected-component coverage — read OR named in the plan OR explicitly
    #     ruled_out (with a concrete reason) → COVERAGE. The ruled_out escape hatch
    #     (2026-07-07) removes the false "plan must be a SUPERSET of the classifier's
    #     guess" requirement: coverage is now measured off the PLAN's own decisions
    #     (files_to_change / new_files_needed / ruled_out), not the raw classifier
    #     list. Grounding (clause c above) is UNCHANGED and stays HARD.
    out_files = set(_new_declared)
    for f in (parsed.get("files_to_change") or []):
        if isinstance(f, str):
            out_files.add(f)
        elif isinstance(f, dict):
            _p = f.get("path") or f.get("file") or f.get("name")
            if _p:
                out_files.add(_p)
    # ruled_out discharge set: a classifier candidate the planner explicitly decided
    # is irrelevant WITH a non-empty reason. An entry whose reason is blank/missing
    # does NOT discharge — it must carry a justification.
    ruled_out_paths = set()
    for entry in (parsed.get("ruled_out") or []):
        if isinstance(entry, dict):
            _rp = entry.get("path")
            _reason = entry.get("reason")
            if isinstance(_rp, str) and _rp.strip() \
                    and isinstance(_reason, str) and _reason.strip():
                ruled_out_paths.add(_rp.strip())
    for c in (affected_components or []):
        if isinstance(c, str) and c.strip() \
                and not _path_covered(c, read_paths) \
                and not _path_covered(c, out_files) \
                and not _path_covered(c, ruled_out_paths):
            coverage_gaps.append(f"affected component not covered: {c}")

    ok = not coverage_gaps and not grounding_gaps
    recoverable = (not ok) and _is_recoverable()
    return {"ok": ok, "coverage_gaps": coverage_gaps,
            "grounding_gaps": grounding_gaps, "recoverable": recoverable}


def _verify_explore_output(stage: str, final_text: str, ctx,
                           expected_files=None, required_keys=(),
                           affected_components=None):
    """Deterministic completeness gate for a pre-gate explore stage.

    Asserts that (a) the output parses as a non-empty JSON object, (b) every key
    in `required_keys` is present AND non-empty, (c) every path in `expected_files`
    actually appears in `ctx._reads.paths` (the loop READ it, did not guess it),
    and (d) every affected component is covered (read OR declared in the output's
    files_to_change). Pure and unit-testable — no network, no LLM.

    Returns (ok: bool, reasons: list[str], recoverable: bool).

    `recoverable` is True when a FAILED verdict looks like JSON that was truncated
    at the output ceiling (starts like a JSON object/array but is unbalanced, or
    _parse_json returned a {"raw": …} wrapper over large truncated-looking text) —
    as opposed to genuinely thin/empty/prose output. The caller repairs a
    recoverable result (re-ask / _self_review) before suspending (Step 2). A
    thin/empty/prose failure is NOT recoverable → suspend as before.
    """
    parsed = _parse_json(final_text or "")
    verdict = _explore_convergence_verdict(
        stage, parsed, ctx,
        expected_files=expected_files, required_keys=required_keys,
        affected_components=affected_components, final_text=final_text or "",
    )
    coverage_gaps = verdict.get("coverage_gaps") or []
    grounding_gaps = verdict.get("grounding_gaps") or []
    # Flatten the split verdict back into the legacy flat reasons list so existing
    # callers (analyst/designer/diagnose post-loop) are unchanged.
    reasons: list = list(coverage_gaps) + list(grounding_gaps)
    ok = verdict.get("ok", not reasons)
    recoverable = verdict.get("recoverable", False)
    logger.info(
        f"[VERIFY-EXPLORE] {stage} verdict ok={ok}",
        run_id=getattr(ctx, "run_id", ""),
        stage=stage,
        ok=ok,
        recoverable=recoverable,
        coverage_gaps=coverage_gaps,
        grounding_gaps=grounding_gaps,
    )
    return ok, reasons, recoverable


def _repair_explore_json(run_id: str, stage: str, final_text: str,
                         required_keys: tuple) -> str:
    """Repair a truncated/malformed explore-stage JSON answer (Step 2) using the
    direct path's _self_review repair loop. Returns the repaired text (or the
    original if repair fails). Best-effort — never raises."""
    try:
        criteria = (
            "Valid JSON object with ALL of these non-empty top-level keys: "
            + ", ".join(required_keys)
            + ". Output ONLY the JSON — no prose, no markdown fences."
        )
        repaired = _self_review(final_text or "", criteria, max_iter=1)
        logger.info(
            f"[SDLC {run_id}] {stage}: JSON repair attempted",
            run_id=run_id, stage=stage,
            before_len=len(final_text or ""), after_len=len(repaired or ""),
        )
        return repaired or final_text
    except Exception as _e:
        logger.warning(f"[SDLC {run_id}] {stage}: JSON repair failed ({_e}) — using original",
                       run_id=run_id, stage=stage)
        return final_text


def _phase_pre_coding_build(run_id: str, machine) -> bool:
    """PRE_CODING_BUILD stage — build workspace before CODING starts.

    Returns True if build passes (or gate disabled), False on failure (run is SUSPENDED).
    `machine` is the CodingStateMachine instance (already created, workspace not yet set up).
    """
    _transition(run_id, "PRE_CODING_BUILD", "pre-coding-build")
    gate_enabled = os.getenv("SDLC_ENABLE_BASELINE_GATE", "false").lower() in ("1", "true", "yes")
    if not gate_enabled:
        logger.info(
            f"[PRE-CODE-BUILD {run_id}] gate disabled (SDLC_ENABLE_BASELINE_GATE=false) "
            f"— skipping build, proceeding anyway"
        )
        return True
    try:
        import time as _time
        machine._ensure_run_workspace(machine.repo)
        branch = machine.base_branch or "main"
        logger.info(f"[PRE-CODE-BUILD {run_id}] workspace_sync: re-synced to {branch!r} sha=HEAD")
        t0 = _time.monotonic()
        result = machine._build_oracle()
        duration = round(_time.monotonic() - t0, 1)
        success = bool(result.get("success"))
        logger.info(f"[PRE-CODE-BUILD {run_id}] build: success={success} duration={duration}s")
        if success:
            logger.info(f"[PRE-CODE-BUILD {run_id}] PASS — baseline clean, proceeding to CODING")
            return True
        errors = result.get("errors") or []
        err_preview = "; ".join(_s(e) for e in errors[:3])
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] FAIL — workspace does not build: {err_preview}",
            run_id=run_id,
            errors=errors[:3],
        )
        update_run_state(
            run_id, "SUSPENDED",
            context_patch={"suspended_at_stage": "PRE_CODING_BUILD"},
            suspended_at_stage="PRE_CODING_BUILD",
            error="Workspace does not build cleanly before coding. Fix manually and re-trigger."
        )
        return False
    except (RuntimeError, OSError, ImportError) as _infra:
        # Infrastructure failure (clone missing, network, missing dependency) — workspace
        # is unusable but this is not a code-quality build failure.  Proceed with a warning
        # so CODING can still attempt the run; a genuine build failure is caught above.
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] infra error setting up build gate ({_infra!r}) "
            f"— proceeding anyway",
            run_id=run_id,
        )
        return True
    except Exception as _be:
        # Unexpected exception (e.g. assertion in _build_oracle internals) — proceed with
        # warning; do not silently treat this as a passing build.
        logger.error(
            f"[PRE-CODE-BUILD {run_id}] unexpected error in build gate ({_be!r}) "
            f"— proceeding anyway",
            run_id=run_id,
        )
        return True


# ── Shift-left pre-gate codegen helpers ("decide before the gate") ───────────

def _bug_analysis_from_fix(run_id: str, jira_key: str, fix: dict, triage: dict,
                           jira_summary: str = "") -> dict:
    """Build the state-machine `analysis` dict from an approved bug `fix` + triage.
    Single source of truth shared by the bug pre-gate codegen and the bug post-gate
    resume so both build the identical edit list / requirements."""
    fix = fix or {}
    triage = triage or {}
    _fix_files = [c.get("file", "") for c in fix.get("code_changes", [])
                  if isinstance(c, dict) and c.get("file")]
    _bug_ftc = _fix_files or triage.get("affected_components", [])
    _bug_ftc_kept, _bug_ftc_dropped = _filter_noneditable_files(_bug_ftc)
    if _bug_ftc_dropped:
        logger.warning(
            f"[SDLC {run_id}] Deny-list (bug): dropped {len(_bug_ftc_dropped)} "
            f"non-editable path(s) from files_to_change: {_bug_ftc_dropped}"
        )
    return {
        "files_to_change":   _bug_ftc_kept,
        "new_files_needed":  [],
        "requirements":      fix.get("fix_description", "") or jira_summary,
        "problem_statement": (
            f"Bug {jira_key}: {jira_summary}\n"
            f"Fix: {fix.get('fix_description', '')}\n"
            f"Root cause: {fix.get('root_cause_analysis', '')}"
        ).strip(),
        "root_cause":        fix.get("root_cause_analysis", ""),
    }


def _capture_base_sha_unconditional(run_id: str, machine) -> None:
    """Pin base_sha even when SDLC_REUSE_RUN_WORKSPACE is off, so every approved
    diff has a base to rebase from at APPLYING (recommended adjustment 2).
    First-writer-wins; best-effort (never fatal)."""
    try:
        if machine._get_run_base_sha():
            return
        # Prefer the GitLab API branch head (cheap) so we do NOT force a clone on
        # every pre-gate run just to read HEAD. Fall back to a workspace clone only
        # if the API read returns nothing (e.g. transient API failure).
        sha = machine._current_branch_head(machine.base_branch or "main")
        if not sha:
            try:
                ws = machine._ensure_run_workspace(machine.repo)
                if ws:
                    from workers.workspace_sync_worker import _git_head as _gh
                    sha = _gh(ws) or ""
            except Exception as _ws_e:
                logger.debug(f"[SDLC {run_id}] base_sha workspace head read failed: {_ws_e}")
        if sha:
            machine._set_run_base_sha(sha)
            logger.info(f"[SDLC {run_id}] base_sha pinned unconditionally: {sha[:8]}")
    except Exception as _e:
        logger.warning(f"[SDLC {run_id}] unconditional base_sha capture failed (non-fatal): {_e}")


def _pregate_codegen(run_id: str, jira_key: str, repo_resolved: str, language: str,
                     design: dict, analysis: dict, base_branch: str = "",
                     working_branch: str = "", ctx: dict = None,
                     run_type: str = "feature") -> bool:
    """Run the FULL CODING → REVIEW_GATE → TESTING machinery in PRE-GATE mode so
    the human approves a real, compiled, test-green diff (the VERIFIED_DIFF) — not
    a JSON plan. Captures base_sha unconditionally and runs PRE_CODING_BUILD first.

    Returns True iff a VERIFIED_DIFF was produced and the run did not FAIL/SUSPEND;
    on False the caller must NOT advance to the approval gate (the run is already
    SUSPENDED/FAILED with the partial state the engineer needs)."""
    from agents.sdlc_state_machine import CodingStateMachine
    from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
    import os as _os
    ctx = ctx or {}
    repo_key = _nrik(repo_resolved) if repo_resolved else ""
    _skip_tests_env = _os.getenv("SDLC_SKIP_TESTS", "").lower() in ("1", "true", "yes")
    _st_raw = ctx.get("skip_tests", _skip_tests_env)
    _ss_raw = ctx.get("skip_slt", False)
    _resolved_skip_tests = _st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")
    _resolved_skip_slt = _ss_raw if isinstance(_ss_raw, bool) else str(_ss_raw).lower() in ("1", "true", "yes")
    try:
        machine = CodingStateMachine(
            run_id=run_id, jira_key=jira_key, repo=repo_key, language=language,
            design=design, analysis=analysis,
            base_branch=base_branch or ctx.get("base_branch", ""),
            working_branch=working_branch or ctx.get("working_branch", ""),
            gitlab_repo=repo_resolved,
            skip_tests=_resolved_skip_tests, skip_slt=_resolved_skip_slt,
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""),
            mode="pregate",
        )
    except Exception as _ce:
        logger.error(f"[SDLC {run_id}] PREGATE_CODEGEN construction failed: {_ce}")
        # Terminalize: without this the run is left at its prior (non-terminal) state
        # with no SUSPEND/FAIL — the caller just returns and the run is orphaned/stuck.
        _suspend_plan(
            run_id, jira_key, "IMPLEMENT",
            f"pre-gate codegen could not start (state-machine construction failed): {_ce}",
        )
        return False

    logger.info(
        f"[SDLC {run_id}] PREGATE_CODEGEN start",
        run_id=run_id, stage="PREGATE_CODEGEN",
        files=(analysis.get("files_to_change") if isinstance(analysis, dict) else None),
    )
    _capture_base_sha_unconditional(run_id, machine)
    # PRE_CODING_BUILD runs here now (moved off the resume path) so the workspace
    # is proven to build BEFORE pre-gate patching begins.
    if not _phase_pre_coding_build(run_id, machine):
        return False
    try:
        machine.run()
    except SDLCCancelled:
        logger.info(f"SDLC {run_id}: pregate codegen cancelled mid-run")
        return False
    except Exception as _re:
        logger.error(f"[SDLC {run_id}] PREGATE_CODEGEN failed: {_re}")
        update_run_state(run_id, "FAILED", error=str(_re))
        return False

    _state = (get_run(run_id) or {}).get("state", "")
    if _state in ("FAILED", "SUSPENDED", "CANCELLED", "MERGE_CONFLICT"):
        logger.warning(
            f"[SDLC {run_id}] PREGATE_CODEGEN did not reach the gate (state={_state}) — not advancing"
        )
        return False
    from store.sdlc_artifacts import _load_latest_artifact
    _vd = _load_latest_artifact(run_id, "VERIFIED_DIFF")
    _payload = (_vd or {}).get("payload") or {}
    if not _payload.get("edits"):
        logger.warning(f"[SDLC {run_id}] PREGATE_CODEGEN produced no VERIFIED_DIFF edits — not advancing")
        # Terminalize: the machine left the run at REVIEW (a non-terminal state) but
        # produced no applicable diff. Returning False here without a SUSPEND leaves the
        # run silently stranded at REVIEW (Case 3 orphan). Suspend at IMPLEMENT so it is
        # visible/actionable and a resume re-runs implementation to produce a real diff.
        _suspend_plan(
            run_id, jira_key, "IMPLEMENT",
            "pre-gate codegen completed but produced no VERIFIED_DIFF edits to review/apply "
            "— re-run implementation (resume) to generate a diff",
        )
        return False
    logger.info(
        f"[SDLC {run_id}] PREGATE_CODEGEN finish",
        run_id=run_id, stage="PREGATE_CODEGEN",
        base_sha=_payload.get("base_sha"),
        files=_payload.get("files"),
        compile_passed=(_payload.get("compile") or {}).get("passed"),
        tests_passed=(_payload.get("tests") or {}).get("passed"),
    )
    return True


# ── CLI three-phase engine: PLAN + REVIEW phases (additive; Step 3 + Step 6) ──
# These are NET-NEW functions for the CLI three-phase engine. They do NOT rewire
# run_feature_pipeline/run_bug_pipeline yet (a later step does the cutover). PLAN
# drives the read-only CLI to produce the implementation plan; REVIEW is the
# platform-controlled Opus diff-only gate. Both REUSE existing helpers verbatim.

# JSON Schema for the PLAN phase structured output. Kept module-level (next to the
# required-keys constant) so it is defined once and shared. `required` == the
# _PLAN_REQUIRED_KEYS tuple so the CLI's structured-output contract matches the
# post-loop completeness gate exactly.
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "files_to_change":    {"type": "array", "items": {"type": "string"}},
        "new_files_needed":   {"type": "array"},
        "sub_tasks":          {"type": "array"},
        "implementation_spec": {"type": "string"},
        "solution_approach":  {"type": "string"},
        # implementation_plan is intentionally permissive: models emit either an
        # ordered list of steps or a single prose block. Both are acceptable.
        # Expressed via anyOf rather than a union `type: [array, string]` — the CLI
        # binary compiles this schema with Ajv in strict mode, which REJECTS the union
        # `type` array form ("strictTypes: use allowUnionTypes …") at schema-compile
        # time. That broke the StructuredOutput tool for the whole PLAN session (see
        # logs/root_cause_analysis_plan_stall.md). anyOf is strict-clean and preserves
        # both acceptable shapes.
        "implementation_plan": {"anyOf": [{"type": "array"}, {"type": "string"}]},
        "code_structure":     {"type": "string"},
        "testing_strategy":   {"type": "string"},
        "rollback_strategy":  {"type": "string"},
        "affected_components": {"type": "array"},
        # Escape hatch (2026-07-07): classifier-flagged paths the planner decided are
        # NOT relevant to this change. Each entry {path, reason} with a NON-EMPTY
        # reason discharges that path from the affected-component coverage check
        # (see _explore_convergence_verdict). Optional (NOT in _PLAN_REQUIRED_KEYS) —
        # missing ⇒ treated as empty.
        "ruled_out": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        # Planner's estimate of how many CLI tool-call turns IMPLEMENT will need.
        # Optional (not in _PLAN_REQUIRED_KEYS) — a missing/invalid value falls back
        # to the coder's default budget (see sdlc_cli_budget.resolve_implement_turns).
        "implement_max_turns": {"type": "integer"},
        # Per-file UNIQUE VERBATIM anchor strings the coder matches on instead of line
        # numbers. Line hints drift against the live tree and force expensive re-reads
        # (the dominant IMPLEMENT-timeout cause); an exact anchor lets the coder Grep to
        # the edit site in one pass. Optional (NOT in _PLAN_REQUIRED_KEYS) — a missing
        # value just means the coder falls back to reading. Projected into the IMPLEMENT
        # prompt via sdlc_implement_prompt._SPEC_KEYS. Object items with plain string
        # properties only — Ajv strict mode (see implementation_plan note above) rejects
        # union `type` arrays, so this shape mirrors ruled_out/open_questions exactly.
        "edit_anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file":    {"type": "string"},
                    "anchors": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question":    {"type": "string"},
                    "options":     {"type": "array"},
                    "recommended": {"type": "integer"},
                    "rationale":   {"type": "string"},
                },
            },
        },
    },
    "required": list(_PLAN_REQUIRED_KEYS),
}


def _suspend_plan(run_id: str, jira_key: str, stage: str, reason: str) -> None:
    """Suspend a run from the PLAN (or any CLI) phase. Mirrors the SUSPEND shape used
    elsewhere in this module (event + transition + state patch + best-effort Jira
    comment). Never raises — a suspend must never itself crash the pipeline."""
    try:
        logger.warning(
            f"[SDLC {run_id}] {stage} suspended: {reason}",
            run_id=run_id, stage=stage, reason=reason,
        )
    except Exception:
        pass
    try:
        _event(run_id, stage, f"{stage.lower()}-suspend", reason, {"stage": stage})
    except Exception:
        pass
    try:
        _transition(run_id, "SUSPENDED", f"{stage.lower()}-suspend")
    except Exception:
        pass
    try:
        update_run_state(
            run_id, "SUSPENDED",
            context_patch={"suspended_at_stage": stage, "suspend_reason": reason},
            suspended_at_stage=stage, error=reason,
        )
    except Exception:
        pass
    try:
        _jira_comment(jira_key, f"[AiNxt AI] {stage} suspended: {reason}")
    except Exception:
        pass


def _ground_plan_reads(run_id: str, plan: dict, workspace_root: str):
    """Build a lightweight ctx stub whose ``_reads`` reflects the plan's cited EXISTING
    files, read off the pinned checkout (the CLI's own reads happen in a subprocess the
    platform cannot observe). Returns ``(ctx_v, kept)`` where ``kept`` is the set of
    existing ``files_to_change`` that resolve on disk (new files excluded; cited-but-
    absent paths are dropped from ``kept`` by ``_resolve_required_against_workspace``, so
    they are NOT passed as ``expected_files`` and therefore do NOT themselves raise a
    grounding gap — a *resolved* file that then fails to READ is what leaves an expected
    path unseeded → grounding gap → suspend). Filesystem READ only — never mutates. Shared
    by the initial grounding gate and the fix-round re-grounding so both judge identically."""
    expected_raw = [p for p in (plan.get("files_to_change") or [])
                    if isinstance(p, str) and p.strip()]
    _new_files = [p for p in (plan.get("new_files_needed") or [])
                  if isinstance(p, str) and p.strip()]
    kept, _dropped_nonexistent, _excluded_new = _resolve_required_against_workspace(
        expected_raw, workspace_root, new_files=_new_files,
    )

    class _PlanCtx:
        pass
    ctx_v = _PlanCtx()
    ctx_v.run_id = run_id
    ctx_v._reads = {"paths": set(), "contents": {}}
    for _p in kept:
        try:
            _rel = _p.replace("\\", "/").lstrip("/")
            _full = os.path.join(workspace_root, _rel)
            with open(_full, "r", encoding="utf-8", errors="replace") as _fh:
                _content = _fh.read()
            ctx_v._reads["paths"].add(_p)
            ctx_v._reads["contents"][_p] = _content
        except Exception as _re:
            logger.debug(f"[SDLC {run_id}] PLAN ground-read miss {_p!r}: {_re}")
    return ctx_v, kept


def _plan_gate_action(verdict: dict) -> tuple:
    """Pure decision over a split convergence verdict (Research Q2 split). Returns
    ``(action, hard_gaps, candidate_gaps)`` where action ∈ {"suspend","fix_round",
    "proceed"}:
      * grounding gaps (cited-but-unread EXISTING file = anti-hallucination) OR a
        ``required field empty:`` coverage gap (structurally-thin plan) ⇒ ``suspend``
        (HARD — never softened);
      * any OTHER coverage gap (a classifier candidate neither changed nor ruled_out)
        ⇒ ``fix_round``;
      * neither ⇒ ``proceed``.
    Pure — no LLM/network/IO. This is the accept/suspend boundary guarding IMPLEMENT."""
    grounding = [g for g in (verdict.get("grounding_gaps") or [])]
    coverage = [g for g in (verdict.get("coverage_gaps") or [])]

    def _is_hard_coverage(g) -> bool:
        # A structurally-thin plan (required key empty) or a non-dict/empty plan
        # sentinel is HARD (not a mere classifier-guess mismatch to fix-round).
        s = str(g)
        return s.startswith("required field empty:") or "not a valid non-empty object" in s

    empty_field = [g for g in coverage if _is_hard_coverage(g)]
    candidates = [g for g in coverage if not _is_hard_coverage(g)]
    hard = grounding + empty_field
    if hard:
        return ("suspend", hard, candidates)
    if candidates:
        return ("fix_round", [], candidates)
    return ("proceed", [], [])


def _run_plan_fix_round(run_id: str, workspace_root: str, repo_resolved: str,
                        language: str, plan: dict, unaddressed_paths: list,
                        session_id: str, issue: dict = None,
                        manifest_feedback: dict = None):
    """One bounded PLAN fix round (mirrors REVIEW's single fix round). Re-invokes the
    read-only CLI with targeted feedback naming the EXACT unaddressed classifier paths,
    warm-started PATH-ONLY (prior plan file lists + prior narrative fields + never file
    contents — item 9). When ``SDLC_CLI_RESUME_ENABLED`` is on and a ``session_id`` is
    available the CLI resumes that session; otherwise it is a fresh session — which is
    the shipped default, so the prompt carries enough ticket + repo + approach context
    to regenerate a COMPLETE plan (not just the delta). Returns the fix-round plan dict,
    or None when the CLI produced nothing usable. Never raises.

    ``manifest_feedback`` (2026-07-13): when the caller is the manifest auto-correction
    round rather than the classifier coverage round, this dict carries the cross-model
    judge's structured reject reasons (``missing_components`` / ``out_of_scope_violations``).
    When present an extra directive block is appended telling the planner to add each
    missing component (or ruled_out it) and drop/justify each out-of-scope file."""
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from agents.sdlc_cli_budget import remaining_budget, resolve_plan_turns, record_cli_usage
    from core.model_registry import cli_model_for
    try:
        cfg = CliEngineConfig.from_env()
        issue = issue or {}
        _prior_change = [p for p in (plan.get("files_to_change") or [])
                         if isinstance(p, str) and p.strip()]
        _prior_new = [p for p in (plan.get("new_files_needed") or [])
                      if isinstance(p, str) and p.strip()]
        _cls = (get_run(run_id) or {}).get("context", {}).get("classification", {}) or {}
        max_turns = resolve_plan_turns(_cls.get("complexity"), remaining_budget(run_id, "PLAN"))
        # Ticket + prior narrative context so a FRESH (resume-off, the default) session
        # can regenerate a complete plan rather than run blind. Text/paths only — never
        # source file contents (item 9 warm-start contract).
        _summary = issue.get("summary", "") or ""
        _desc = issue.get("description", "") or issue.get("jira_description", "") or ""
        _approach = plan.get("solution_approach", "") or ""
        from agents.sdlc_implement_prompt import workspace_boundary_clause as _wbc
        # Classifier-coverage block — only when there are unaddressed classifier paths
        # (the original coverage fix round). Empty for a pure manifest auto-correction.
        _unaddressed_block = ""
        if unaddressed_paths:
            _unaddressed_block = (
                "Your previous implementation PLAN left these classifier-flagged paths "
                "UNADDRESSED (they were neither in files_to_change / new_files_needed nor "
                "in ruled_out):\n"
                f"  {unaddressed_paths}\n\n"
                "For EACH such path you MUST either (a) add it to files_to_change (or "
                "new_files_needed if it does not exist), OR (b) add it to ruled_out as "
                "{\"path\": ..., \"reason\": ...} with a concrete reason it is irrelevant "
                "to this change. Keep the rest of the plan intact.\n\n"
            )
        # Manifest cross-validation reject block (2026-07-13 auto-correction round).
        _manifest_block = ""
        if manifest_feedback:
            _mf_missing = manifest_feedback.get("missing_components") or []
            _mf_oos = manifest_feedback.get("out_of_scope_violations") or []
            _manifest_block = (
                "Your previous plan was REJECTED by manifest cross-validation.\n"
                f"  Missing in-scope components: {_mf_missing}\n"
                f"  Out-of-scope violations: {_mf_oos}\n"
                "Revise the manifest to (a) ADD each missing component to "
                "files_to_change / new_files_needed, OR explicitly add it to ruled_out "
                "as {\"path\": ..., \"reason\": ...} with a concrete reason; and (b) DROP "
                "each out-of-scope file OR justify it as a mandatory companion of an "
                "in-scope change (e.g. db/migrate.py + SQL for a schema change). Keep the "
                "rest of the plan intact.\n\n"
            )
        fix_prompt = (
            "You are refining an implementation PLAN for a code change in repository "
            f"{repo_resolved} (language: {language or 'unknown'})."
            f"{_wbc(workspace_root)}\n\n"
            f"TICKET:\n  Summary: {_summary}\n  Description: {_desc}\n\n"
            f"Intended solution approach (from your prior plan):\n{_approach}\n\n"
            f"{_unaddressed_block}"
            f"{_manifest_block}"
            "Your prior plan's file lists (paths only — re-read the ACTUAL source before "
            "deciding, do NOT guess):\n"
            f"  files_to_change: {_prior_change}\n"
            f"  new_files_needed: {_prior_new}\n\n"
            "Output ONLY the full required JSON with all top-level keys: "
            f"{', '.join(_PLAN_REQUIRED_KEYS)}, plus new_files_needed, "
            "affected_components, ruled_out, and implement_max_turns."
        )
        # PART 1 governance awareness into the PLAN fix round (fail-safe → no-op).
        # Inlined SKILL.md content in the prompt text — no CLI plugin-loading
        # mechanism exists (confirmed 2026-07-20; see resolve_awareness docstring).
        from agents.sdlc_governance import engine as _gov_engine
        from agents.sdlc_implement_prompt import governance_pointer_clause as _gov_pointer
        _run_ctx = (get_run(run_id) or {}).get("context", {}) or {}
        _gov_block = _gov_engine.resolve_awareness(
            _run_ctx.get("governance_skills"), phase="plan", workspace_root=workspace_root
        )
        if _gov_block:
            fix_prompt = fix_prompt + _gov_pointer(_gov_block)
        result = run_cli(
            config=cfg, workspace_root=workspace_root, prompt=fix_prompt,
            profile="plan", model=cli_model_for("plan"), output_schema=PLAN_SCHEMA,
            max_turns=max_turns, run_id=run_id, resume_session_id=session_id or "",
            transient_retries=2,   # read-only PLAN-fix: safe to re-spawn on a proxy 502
        )
        try:
            record_cli_usage(run_id, result.usage or {}, result.total_cost_usd or 0.0)
        except Exception as _bue:
            logger.warning(f"[SDLC {run_id}] PLAN fix-round budget accounting failed: {_bue}")
        if result.status == "suspended":
            logger.warning(
                f"[SDLC {run_id}] PLAN fix round CLI suspended: {result.reason}",
                run_id=run_id, stage="PLAN",
            )
            return None
        fixp = result.structured_output
        if not isinstance(fixp, dict) or not fixp:
            fixp = _parse_json(result.result_text or "")
        if not isinstance(fixp, dict) or not fixp:
            return None
        return fixp
    except Exception as _fre:
        logger.warning(f"[SDLC {run_id}] PLAN fix round errored (non-fatal): {_fre}",
                       run_id=run_id, stage="PLAN")
        return None


def _run_plan_phase(run_id: str, jira_key: str, repo_resolved: str, language: str,
                    issue: dict, ctx: dict, run_type: str = "feature"):
    """CLI three-phase engine — PLAN phase (Step 3).

    Drives the read-only CLI (profile="plan", Sonnet workhorse) to produce an
    implementation PLAN dict, verifies it is grounded+complete (REUSING
    _verify_explore_output), optionally clarifies-in-plan (suspends to
    AWAITING_USER_INPUT), runs the manifest-validation sub-check, stores the plan
    as the PLAN artifact, and mirrors it into run context as analysis/design so the
    existing _pregate_codegen(design=plan, analysis=plan) contract holds unchanged.

    Returns the plan dict on success, or None when the run has ALREADY been
    suspended/paused (the caller must then `return run_id`)."""
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from agents.sdlc_cli_budget import (
        record_cli_usage, remaining_budget, derive_max_turns, is_exhausted,
        resolve_plan_turns,
    )
    from agents.sdlc_agent_loop import _looks_truncated_json
    from core.model_registry import cli_model_for
    from store.sdlc_artifacts import _store_artifact, compute_input_hash

    issue = issue or {}
    ctx = ctx or {}
    _cls = (get_run(run_id) or {}).get("context", {}).get("classification", {}) or {}

    # Live state → PLAN so the UI/manifest highlights the PLAN node during planning.
    try:
        _transition(run_id, "PLAN", "cli-planner")
    except Exception:
        pass

    # ── 3. Materialize + pin the workspace early (PLAN needs the pinned tree so the
    #        grounding predicate can confirm cited files exist on disk).
    workspace_root = _materialize_early_workspace(
        run_id, repo_resolved,
        issue.get("working_branch", "") or "",
        issue.get("base_branch", "") or "",
        user_id=issue.get("triggered_by_user_id", "") or "",
        user_email=issue.get("triggered_by_email", "") or "",
    )
    if not workspace_root:
        logger.warning(
            f"[SDLC {run_id}] PLAN workspace materialization returned empty path",
            run_id=run_id, stage="PLAN",
        )
        _suspend_plan(run_id, jira_key, "PLAN", "workspace materialization failed")
        return None
    patch_run_context(run_id, {"workspace_root": workspace_root})

    # ── 3b. Multi-repo: stage dep-repo checkouts inside the primary workspace
    #        (no-op for single-repo runs). See _setup_multi_repo_workspace_for_plan
    #        docstring for why PLAN never suspends on a staging failure.
    _setup_multi_repo_workspace_for_plan(run_id, workspace_root)

    # ── 4. Budget check + derive the CLI turn cap. PLAN turns scale to classifier
    #        complexity (simple/medium/complex → 8/20/40) so a small ticket cannot burn
    #        the flat default budget; the HOD budget can only REDUCE that cap, never
    #        inflate it (resolve_plan_turns → derive_max_turns).
    if is_exhausted(run_id, "PLAN"):
        _suspend_plan(run_id, jira_key, "PLAN", "per-run budget exhausted")
        return None
    _plan_remaining = remaining_budget(run_id, "PLAN")
    max_turns = resolve_plan_turns(_cls.get("complexity"), _plan_remaining)
    logger.info(
        "[SDLC PLAN] turn budget resolved", run_id=run_id,
        complexity=_cls.get("complexity"), plan_turns=max_turns,
        budget_ceiling=derive_max_turns(_plan_remaining), resolved=max_turns,
    )

    # ── 5. Build the PLAN prompt (ticket + conventions + seeded components + prior
    #        clarify answers + approved scope). Do NOT instruct it to write code —
    #        PLAN is read-only.
    _summary = issue.get("summary", "") or ""
    _desc = issue.get("description", "") or issue.get("jira_description", "") or ""
    _seed_components = _cls.get("affected_components") or []
    _prior_answers = ctx.get("user_answers") or []
    _answers_block = ""
    if isinstance(_prior_answers, list) and _prior_answers:
        _lines = ["AUTHORITATIVE USER ANSWERS (already provided — honor these, do NOT re-ask):"]
        for _qa in _prior_answers:
            if isinstance(_qa, dict):
                _lines.append(f"  Q: {_qa.get('question', '')}\n  A: {_qa.get('answer', '')}")
        _answers_block = "\n".join(_lines) + "\n\n"
    # WS-5: inject the GATE-1-approved WorkItem scope so the planner stays inside
    # the human-confirmed boundary (prevents the scope divergence that used to
    # trip an unconfirmed out_of_scope_violations suspend at MANIFEST_VALIDATION).
    _wi = ctx.get("work_item") or {}
    _scope_block = ""
    if isinstance(_wi, dict) and (_wi.get("scope") or _wi.get("out_of_scope")):
        _scope_block = (
            "APPROVED SCOPE (human-confirmed at the WorkItem gate — stay inside it):\n"
            f"  In scope: {_wi.get('scope') or []}\n"
            f"  Out of scope (do NOT touch): {_wi.get('out_of_scope') or []}\n\n"
        )
    # Step 4.2 (2026-07-13): a COLD manual retry (resume_pre_sm_pipeline("PLAN") →
    # _drive_pre_sm → here) re-enters with the run context that carries a prior
    # manifest_feedback (persisted at the Step-13 suspend tail). Inject it so the cold
    # retry re-plans WITH the reject reasons instead of blind — closing the
    # "feedback lost on retry" gap. The warm auto-round reads the SAME contract via
    # _run_plan_fix_round(manifest_feedback=...); this is its cold-path twin.
    _manifest_fb = ctx.get("manifest_feedback") or {}
    _manifest_fb_block = ""
    if isinstance(_manifest_fb, dict) and (
        _manifest_fb.get("missing_components") or _manifest_fb.get("out_of_scope_violations")
    ):
        _manifest_fb_block = (
            "PRIOR MANIFEST REJECTION (address before re-planning):\n"
            f"  Missing components: {_manifest_fb.get('missing_components') or []}\n"
            f"  Out-of-scope: {_manifest_fb.get('out_of_scope_violations') or []}\n"
            "Add each missing component to files_to_change / new_files_needed or to "
            "ruled_out (with a concrete reason); drop or justify each out-of-scope file "
            "as a mandatory companion of an in-scope change.\n\n"
        )
        logger.info(
            "[PLAN] injecting prior manifest feedback into cold re-plan",
            run_id=run_id, has_manifest_feedback=True,
        )
    _keys_list = ", ".join(_PLAN_REQUIRED_KEYS)
    from agents.sdlc_implement_prompt import workspace_boundary_clause as _wbc

    # ── 5a. Multi-repo: dependent-repo awareness (Step 3, multi-repo CLI visibility).
    #        Hoisted ahead of the boundary clause below (Fix A) so the workspace-boundary
    #        clause's deps_dirname= carve-out can be conditioned on whether this run
    #        actually has dep rows — IMPLEMENT/continue/fix-round already pass
    #        deps_dirname=".sdlc_deps"; PLAN was missed, which silently negated the whole
    #        point of staging deps inside the workspace (workspace_boundary_clause says
    #        workspace_root is "the ONLY tree you may read", so without the carve-out the
    #        model treats .sdlc_deps/ as out of scope). Sourced from list_run_repos (same
    #        rows _setup_multi_repo_workspace_for_plan used to stage the checkouts at 3b)
    #        rather than the MultiRepoWorkspace return value, since the rows already carry
    #        the kind/ref info the clause needs and this avoids depending on
    #        prepare_and_install_deps' return shape.
    #        "" for single-repo runs — prompt stays byte-identical to today.
    try:
        from agents.sdlc_implement_prompt import dependent_repos_clause as _dep_clause
        from store.sdlc_store import list_run_repos as _list_run_repos_for_dep
        _dep_rows = _list_run_repos_for_dep(run_id) or []
        _dep_block = _dep_clause(_dep_rows)
    except Exception as _dep_e:
        logger.debug(f"[SDLC {run_id}] PLAN dep_block build failed (non-fatal): {_dep_e}")
        _dep_rows, _dep_block = [], ""

    prompt = (
        "You are a senior engineer producing an implementation PLAN for a code change.\n"
        f"Repository: {repo_resolved} (language: {language or 'unknown'})."
        f"{_wbc(workspace_root, deps_dirname='.sdlc_deps' if _dep_block else '')}\n\n"
        f"TICKET {jira_key}:\n  Summary: {_summary}\n  Description: {_desc}\n\n"
        + (f"Classifier-flagged affected components: {_seed_components}\n"
           "For EACH classifier-flagged path you MUST either (a) include it in "
           "files_to_change (or new_files_needed if it does not exist yet), OR "
           "(b) list it in ruled_out as {\"path\": ..., \"reason\": ...} with a "
           "concrete reason it is NOT relevant to this change. A classifier-flagged "
           "path that is neither changed nor ruled_out will FAIL validation. Verify "
           "each against the real code before deciding.\n\n" if _seed_components else "")
        + _scope_block
        + _answers_block
        + _manifest_fb_block
        + "Read the ACTUAL source via your read-only tools before planning — do NOT guess "
        "file paths or APIs. Every file you list in files_to_change MUST be a real, existing "
        "file you have read (new files go in new_files_needed instead).\n\n"
        f"Output ONLY the required JSON with these top-level keys: {_keys_list}, plus "
        "new_files_needed, affected_components, ruled_out, implement_max_turns, and "
        "edit_anchors. All clarifying questions were already resolved before planning "
        "started — do NOT ask questions here; make the best grounded decision and proceed.\n\n"
        "Also emit edit_anchors: for EACH file in files_to_change, a list of SHORT, UNIQUE, "
        "VERBATIM strings copied from the CURRENT source at (or immediately adjacent to) every "
        "edit site — a function/method signature, a decorator, a distinctive comment, or the "
        "exact line your change attaches to. Copy each string exactly as it appears (including "
        "whitespace) and make it unique enough to match exactly ONE place in its file. These let "
        "the coder locate each edit by string match in a single pass instead of trusting line "
        "numbers, which drift against the live tree and force the expensive re-reads that are the "
        "main cause of IMPLEMENT timeouts. Do NOT use line numbers as anchors. Shape: "
        "[{\"file\": \"<path>\", \"anchors\": [\"<verbatim snippet>\", ...]}]. Inside "
        "implementation_spec, reference these anchor strings rather than \"~line NNN\".\n\n"
        "Also emit implement_max_turns: your realistic estimate of how many CLI tool-call "
        "turns a coding agent will need to IMPLEMENT this plan end-to-end (read each target "
        "file, write every file in files_to_change + new_files_needed, author the tests the "
        "plan calls for, and get the build compiling). Size it from the ACTUAL file count in "
        "your plan: budget ~10–12 turns per file that is edited or created (read → edit → "
        "re-read/verify), PLUS 15–25 turns of overhead for build/compile/test iteration and "
        "fixing the errors those surface. Concrete anchors: a focused 1–2 file change ~25, a "
        "typical 3–5 file multi-file change ~60, a large change spanning a DB migration + UI + "
        "backend (8+ files) ~100–140. Count your files and scale accordingly. When uncertain, "
        "round UP — under-estimating forces the coder to abort mid-implementation when it hits "
        "the turn cap (which fails the whole run), whereas the per-run budget ceiling and the "
        "coder's own STOP contract already stop it from over-spending an over-estimate. Do not "
        "low-ball this number."
    )

    # ── 5a2. Read-only exploration + symbol-selection disciplines (P1 + P2).
    #         search_discipline_clause: stop the same failed exact-name search from looping
    #         (SecureNxt vs SecureNext). identifier_fidelity_clause: bind the ticket's domain
    #         qualifier (issuer vs acquirer) to the RIGHT symbol when look-alikes coexist in
    #         the target scope, and record the choice in implementation_spec/solution_approach
    #         so REVIEW can verify it. Appended (established pattern — gov/dep blocks below
    #         also append after the "Output ONLY the required JSON" instruction).
    from agents.sdlc_implement_prompt import (
        search_discipline_clause as _search_discipline,
        identifier_fidelity_clause as _identifier_fidelity,
    )
    prompt = prompt + _search_discipline() + _identifier_fidelity()

    # ── 5b. PART 1 governance awareness (always-on, fail-safe): inline the governance
    #        skills' SKILL.md content directly into the PLAN prompt + append the short
    #        pointer clause so the plan is conditioned on the standards. There is no CLI
    #        plugin-loading mechanism (confirmed 2026-07-20 — neither a --plugin/--skill
    #        flag nor a /plugin//skill slash command loads anything headlessly on the
    #        deployed binary), so prompt text is the only channel that reaches the CLI.
    #        "" when no bundle resolves / awareness disabled → prompt unchanged.
    from agents.sdlc_governance import engine as _gov_engine
    from agents.sdlc_implement_prompt import governance_pointer_clause as _gov_pointer
    _gov_block = _gov_engine.resolve_awareness(
        ctx.get("governance_skills"), phase="plan", workspace_root=workspace_root
    )
    if _gov_block:
        prompt = prompt + _gov_pointer(_gov_block)
        logger.info(
            "[SDLC-GOV] Governance awareness added to PLAN prompt (staged read-only in workspace)",
            run_id=run_id,
        )

    # ── 5c. Multi-repo: dependent-repo awareness — append the block already computed
    #        at 5a above (do NOT re-query list_run_repos here).
    if _dep_block:
        prompt = prompt + _dep_block
        logger.info(
            "[SDLC-CLI] Dep block added to PLAN prompt", run_id=run_id,
            dep_count=sum(1 for r in _dep_rows if r.get("kind") != "primary"),
        )

    # ── 6. Drive the read-only CLI (profile=plan pins Sonnet + no Edit tool).
    result = run_cli(
        config=CliEngineConfig.from_env(),
        workspace_root=workspace_root,
        prompt=prompt,
        profile="plan",
        model=cli_model_for("plan"),
        output_schema=PLAN_SCHEMA,
        max_turns=max_turns,
        run_id=run_id,
        transient_retries=2,   # read-only PLAN: safe to re-spawn on a proxy 502
    )

    # ── 7. Record CLI usage (per-run budget accounting).
    try:
        record_cli_usage(run_id, result.usage or {}, result.total_cost_usd or 0.0)
    except Exception as _bue:
        logger.warning(f"[SDLC {run_id}] PLAN budget accounting failed (non-fatal): {_bue}")

    # ── 8. Engine-level suspend (max turns / error subtype / harness abort).
    if result.status == "suspended":
        _suspend_plan(run_id, jira_key, "PLAN", result.reason or "cli suspended")
        return None

    # ── 9. Extract the plan; treat truncated / retry-exhausted / empty as thin.
    plan = result.structured_output
    if plan is None:
        plan = _parse_json(result.result_text or "")
    if (result.subtype == "error_max_structured_output_retries"
            or _looks_truncated_json(result.result_text or "")
            or not isinstance(plan, dict) or not plan):
        _suspend_plan(run_id, jira_key, "PLAN", "plan incomplete")
        return None

    # ── 10. Gate reorder (2026-07-02): PLAN no longer raises a question gate — the
    #         SINGLE question gate (GATE 2) now lives in CLASSIFY, which always runs
    #         before PLAN starts. A PLAN that still can't proceed falls through to
    #         the grounding/manifest suspends below (go-back, not a question gate).

    # ── 11. RETAINED grounding gate. The CLI read files read-only in its subprocess,
    #         which the platform cannot observe, so we GROUND by confirming each cited
    #         EXISTING file exists on the pinned checkout and seeding its content into
    #         a lightweight ctx._reads so the RETAINED predicate can verify it. A cited
    #         path that is NOT on disk is dropped from `kept` → remains a grounding gap
    #         → suspend, which correctly catches hallucinated paths.
    ctx_v, kept = _ground_plan_reads(run_id, plan, workspace_root)
    _affected = (_cls.get("affected_components") or kept)

    ok, reasons, recoverable = _verify_explore_output(
        "PLAN", json.dumps(plan), ctx_v,
        expected_files=kept, required_keys=_PLAN_REQUIRED_KEYS,
        affected_components=_affected,
    )

    # One repair attempt on a recoverable (truncated-looking) verdict — ORDERED FIRST,
    # before the coverage fix round: a truncated plan must be de-truncated before its
    # coverage/grounding is judged.
    if (not ok) and recoverable:
        _repaired = _repair_explore_json(run_id, "PLAN", json.dumps(plan), _PLAN_REQUIRED_KEYS)
        _rplan = _parse_json(_repaired or "")
        if isinstance(_rplan, dict) and _rplan:
            _rok, _rreasons, _rrecoverable = _verify_explore_output(
                "PLAN", json.dumps(_rplan), ctx_v,
                expected_files=kept, required_keys=_PLAN_REQUIRED_KEYS,
                affected_components=_affected,
            )
            if _rok:
                plan, ok, _reasons, recoverable = _rplan, _rok, _rreasons, _rrecoverable
                ctx_v, kept = _ground_plan_reads(run_id, plan, workspace_root)
                _affected = (_cls.get("affected_components") or kept)

    # ── Verdict SPLIT (2026-07-07 coverage-gate fix). Grounding gaps (cited-but-unread
    #     EXISTING file = anti-hallucination) and a structurally-thin plan (required
    #     field empty) are HARD suspends. A residual classifier-candidate coverage gap
    #     (the planner neither changed nor ruled_out a flagged path) gets exactly ONE
    #     bounded fix round, then WARN+PROCEED — never a silent discard of an otherwise
    #     grounded plan (Research Q1/Q2). See _plan_gate_action / _run_plan_fix_round.
    def _split_verdict(_pd):
        return _explore_convergence_verdict(
            "PLAN", _pd, ctx_v, expected_files=kept,
            required_keys=_PLAN_REQUIRED_KEYS, affected_components=_affected,
            final_text=json.dumps(_pd),
        )

    verdict = _split_verdict(plan)
    action, hard_gaps, candidate_gaps = _plan_gate_action(verdict)
    logger.info(
        "[PLAN] gate decision", run_id=run_id,
        grounding_gaps=verdict.get("grounding_gaps") or [],
        coverage_gaps=verdict.get("coverage_gaps") or [],
        action=action,
    )

    # Warm-start persist (4b): stash the best plan + gap list BEFORE any fix round OR
    # suspend so a later resume can warm-start from plan_partial/plan_gaps instead of
    # exploring cold. (patch_run_context merges into context JSON, so the subsequent
    # suspend's own context_patch does not clobber these.)
    try:
        patch_run_context(run_id, {
            "plan_partial": plan,
            "plan_gaps": list(verdict.get("grounding_gaps") or [])
                         + list(verdict.get("coverage_gaps") or []),
        })
    except Exception as _ppe:
        logger.warning(f"[SDLC {run_id}] PLAN warm-start persist failed (non-fatal): {_ppe}")

    if action == "suspend":
        _suspend_plan(run_id, jira_key, "PLAN", f"plan not grounded/complete: {hard_gaps}")
        return None

    if action == "fix_round":
        _resume_enabled = False
        try:
            from agents.sdlc_cli_engine import CliEngineConfig as _CEC
            _resume_enabled = bool(_CEC.from_env().resume_enabled)
        except Exception:
            pass
        _warm_paths = len([p for p in (plan.get("files_to_change") or [])
                           if isinstance(p, str) and p.strip()]) \
            + len([p for p in (plan.get("new_files_needed") or [])
                   if isinstance(p, str) and p.strip()])
        logger.info(
            "[PLAN] fix round start", run_id=run_id, unaddressed_paths=candidate_gaps,
            resume_used=(_resume_enabled and bool(result.session_id)),
            warm_start_paths=_warm_paths,
        )
        _fixed = _run_plan_fix_round(
            run_id, workspace_root, repo_resolved, language,
            plan, candidate_gaps, result.session_id, issue=issue,
        )
        # Reaching the fix_round branch GUARANTEES the ORIGINAL `plan` is already
        # grounding-clean AND has every required field (else _plan_gate_action would
        # have returned "suspend"). So the original is fully shippable modulo one soft
        # coverage candidate — we must NEVER discard it just because the bounded fix
        # round returned something worse. Only a GENUINE hallucination in the fix-round
        # output (a cited EXISTING file that isn't on disk = grounding gap) is allowed
        # to hard-suspend (plan Step 6). Any other regression (a structurally-thin
        # _fixed) falls back to warn+proceed on the grounded original.
        if isinstance(_fixed, dict) and _fixed:
            # Re-ground the fix-round plan (its files_to_change may have changed) and
            # re-judge with a fresh verdict against the re-grounded read-set.
            ctx_v, kept = _ground_plan_reads(run_id, _fixed, workspace_root)
            _affected = (_cls.get("affected_components") or kept)
            _fverdict = _split_verdict(_fixed)
            _fground = _fverdict.get("grounding_gaps") or []
            if _fground:
                # Genuine hallucination in the fix-round output → hard suspend; never
                # ship an ungrounded plan to IMPLEMENT.
                plan = _fixed
                _suspend_plan(run_id, jira_key, "PLAN",
                              f"plan not grounded/complete: {_fground}")
                return None
            _faction, _fhard, _fcand = _plan_gate_action(_fverdict)
            if _faction == "proceed":
                # Fix round fully resolved the coverage gap → adopt the better plan.
                plan = _fixed
            elif _faction == "fix_round":
                # Residual coverage gap AFTER the one allowed round → adopt + warn.
                plan = _fixed
                plan["coverage_warnings"] = _fcand
                logger.warning(
                    "[PLAN] residual coverage gap after fix round — proceeding",
                    run_id=run_id, coverage_warnings=_fcand,
                )
            else:
                # _faction == "suspend" WITHOUT a grounding gap ⇒ _fixed regressed to a
                # structurally-thin plan (required field empty). Keep the grounded
                # ORIGINAL rather than discard a shippable plan over a malformed fix
                # output — warn + proceed on the original's residual candidates.
                plan["coverage_warnings"] = candidate_gaps
                logger.warning(
                    "[PLAN] fix round produced a thin plan — keeping original + proceeding",
                    run_id=run_id, coverage_warnings=candidate_gaps, fix_round_gaps=_fhard,
                )
        else:
            # Fix round produced nothing usable → keep the grounded original plan,
            # attach the residual coverage gaps as a warning, and PROCEED (never
            # discard an otherwise-grounded plan for a mere classifier-guess mismatch).
            plan["coverage_warnings"] = candidate_gaps
            logger.warning(
                "[PLAN] fix round yielded no usable plan — proceeding with residual",
                run_id=run_id, coverage_warnings=candidate_gaps,
            )
    else:
        logger.info(
            "[PLAN] verify", run_id=run_id, ok=True, recoverable=recoverable,
            open_questions=len(plan.get("open_questions") or []), keys_missing=[],
        )

    # ── 12. Persist the PLAN artifact + mirror into run context as analysis/design
    #         FIRST — BEFORE the (non-blocking, best-effort) manifest gate. The plan
    #         must be durable regardless of the gate verdict: if the gate suspends and
    #         a human WAIVES it, resume re-enters IMPLEMENT and reads
    #         run["context"]["analysis"]/["design"]. Persisting AFTER the gate (the old
    #         order) left those empty on a suspend, so a waive ran IMPLEMENT with no
    #         plan and failed. The existing _pregate_codegen(design=plan, analysis=plan)
    #         + resume paths read these keys unchanged.
    try:
        _store_artifact(
            run_id, "PLAN", plan, producer="cli-planner",
            input_hash=compute_input_hash(run_id, "PLAN"),
            created_by="sdlc", reason="cli plan phase",
        )
    except Exception as _sae:
        logger.warning(f"[SDLC {run_id}] PLAN artifact store failed (non-fatal): {_sae}")
    patch_run_context(run_id, {"plan": plan, "analysis": plan, "design": plan})

    # ── 13. MANIFEST_VALIDATION gate with ONE bounded auto-correction round
    #         (2026-07-13). The plan is already durable above, so any suspend here is
    #         safely waivable. Flow: validate → PASS proceeds; on REJECT, if under the
    #         correction budget, re-plan ONCE with the judge's structured reject
    #         reasons, re-persist the revised plan, and re-validate ONCE. Only a
    #         still-failing round suspends to HITL (with the feedback persisted so a
    #         later COLD manual retry re-plans WITH it — Step 4). Research: Reflexion /
    #         self-refine — exactly ONE critique→revise→recommit round then escalate;
    #         the external verifier is Step-1's deterministic disk check + the CLI
    #         re-reading real source, never the judge grading its own prose.
    def _run_manifest_gate(_plan_arg):
        _wi = (get_run(run_id) or {}).get("context", {}).get("work_item") or {}
        try:
            return _phase_validate_manifest(
                run_id, jira_key, _wi, _plan_arg, _plan_arg, workspace_root,
            )
        except Exception as _mve:
            # Best-effort: a crashing cross-check must not silently pass — fail toward
            # the gate rather than certify an unvalidated plan.
            logger.warning(f"[SDLC {run_id}] PLAN manifest validation errored (non-fatal): {_mve}")
            return False, [f"manifest validation error: {_mve}"]

    def _read_manifest_reasons() -> dict:
        """Structured reject reasons from the just-written MANIFEST_VALIDATION artifact.
        `_finish` stores missing_components / oos_violations / openai_issues on every
        REJECT return — this reads them back as the single feedback contract."""
        try:
            from store.sdlc_artifacts import _load_latest_artifact as _lla
            _art = _lla(run_id, "MANIFEST_VALIDATION") or {}
            _pl = _art.get("payload") or {}
            return {
                "missing_components": list(_pl.get("missing_components") or []),
                "out_of_scope_violations": list(_pl.get("oos_violations") or []),
                "issues": list(_pl.get("openai_issues") or []),
            }
        except Exception as _rae:
            logger.warning(f"[SDLC {run_id}] manifest reason read failed (non-fatal): {_rae}")
            return {"missing_components": [], "out_of_scope_violations": [], "issues": []}

    mv_pass, mv_issues = _run_manifest_gate(plan)

    if not mv_pass:
        _ctx_now = (get_run(run_id) or {}).get("context", {}) or {}
        _attempts = int(_ctx_now.get("manifest_correction_attempts") or 0)
        try:
            _max_corr = int(os.getenv("SDLC_MANIFEST_MAX_CORRECTIONS", "1"))
        except (TypeError, ValueError):
            logger.warning(
                "[PLAN] invalid SDLC_MANIFEST_MAX_CORRECTIONS — using default 1",
                run_id=run_id, raw=os.getenv("SDLC_MANIFEST_MAX_CORRECTIONS"),
            )
            _max_corr = 1
        if _attempts < _max_corr:
            _reasons = _read_manifest_reasons()
            logger.info(
                "[PLAN] manifest auto-correction round start", run_id=run_id,
                attempt=_attempts + 1,
                missing_components=_reasons.get("missing_components"),
                oos_violations=_reasons.get("out_of_scope_violations"),
            )
            _fixed = _run_plan_fix_round(
                run_id, workspace_root, repo_resolved, language,
                plan, _reasons.get("missing_components") or [],
                result.session_id, issue=issue, manifest_feedback=_reasons,
            )
            if isinstance(_fixed, dict) and _fixed:
                plan = _fixed
                # Re-persist the REVISED plan BEFORE re-validation so a later waive/
                # resume reads the corrected plan, not the stale one (the bug fixed in
                # project_sdlc_manifest_plan_persist_2026_07_09).
                try:
                    _store_artifact(
                        run_id, "PLAN", plan, producer="cli-planner",
                        input_hash=compute_input_hash(run_id, "PLAN"),
                        created_by="sdlc", reason="cli plan phase (manifest auto-correction)",
                    )
                except Exception as _sae2:
                    logger.warning(f"[SDLC {run_id}] revised PLAN artifact store failed (non-fatal): {_sae2}")
                patch_run_context(run_id, {"plan": plan, "analysis": plan, "design": plan})
            else:
                logger.warning(
                    "[PLAN] manifest auto-correction produced no usable plan — re-validating original",
                    run_id=run_id, attempt=_attempts + 1,
                )
            # Increment the ctx counter REGARDLESS of fix-round outcome so the bound
            # holds even if the fix round returned nothing usable.
            patch_run_context(run_id, {"manifest_correction_attempts": _attempts + 1})
            mv_pass, mv_issues = _run_manifest_gate(plan)
            logger.info(
                "[PLAN] manifest auto-correction result", run_id=run_id,
                attempt=_attempts + 1, revalidate_pass=bool(mv_pass),
            )

    if not mv_pass:
        # Round exhausted (or disabled via SDLC_MANIFEST_MAX_CORRECTIONS=0) → persist
        # the structured feedback so a later COLD manual retry re-plans WITH it (Step 4),
        # then suspend to HITL. patch_run_context merges into the context JSON, so the
        # subsequent _suspend_plan context_patch will NOT clobber this key.
        _reasons = _read_manifest_reasons()
        _fb = {
            "missing_components": _reasons.get("missing_components") or [],
            "out_of_scope_violations": _reasons.get("out_of_scope_violations") or [],
            "issues": _reasons.get("issues") or [],
            "attempts": int((get_run(run_id) or {}).get("context", {}).get("manifest_correction_attempts") or 0),
        }
        try:
            patch_run_context(run_id, {"manifest_feedback": _fb})
        except Exception as _fbe:
            logger.warning(f"[SDLC {run_id}] manifest_feedback persist failed (non-fatal): {_fbe}")
        logger.warning(
            "[PLAN] manifest gate exhausted — suspending with feedback", run_id=run_id,
            attempts=_fb["attempts"],
            feedback_keys=[k for k, v in _fb.items() if v and k != "attempts"],
        )
        _suspend_plan(run_id, jira_key, "PLAN", f"manifest validation failed: {mv_issues[:3]}")
        return None

    # Manifest PASSED → clear any prior feedback from ctx so stale reject reasons never
    # bias an unrelated future re-plan (Step 4.3).
    try:
        _ctx_after = (get_run(run_id) or {}).get("context", {}) or {}
        if _ctx_after.get("manifest_feedback"):
            patch_run_context(run_id, {"manifest_feedback": None})
            logger.info("[PLAN] cleared manifest_feedback after PASS", run_id=run_id)
    except Exception:
        pass

    # ── 14. Success.
    return plan


def _run_governance_review_phase(run_id: str, workspace: str, diff_text: str,
                                 changed_files: list, product_id, repo: str,
                                 subset=None, db=None) -> dict:
    """DEPRECATED shim (scan-unify 2026-07-28). Historically ran ONE diff-only
    governance CLI session; now delegates to the unified per-skill parallel scan core
    ``run_governance_scan_snapshot`` so every governance trigger uses the SAME engine
    (per-skill ``scan_all_skills``, not the retired single-session ``run_review``).

    Retained only for the dead SM ``_run_governance_review`` caller — the live callers
    (the in-pipeline end-gate and the standalone worker job) now call the core directly.
    Returns the core's rich dict (a superset of the old shape). Never raises."""
    return run_governance_scan_snapshot(
        run_id, workspace=workspace, diff_text=diff_text, changed_files=changed_files,
        product_id=product_id, repo=repo, subset=subset, db=db,
    )


def run_governance_scan_snapshot(run_id: str, *, workspace: str, diff_text: str,
                                 changed_files: list, product_id, repo: str,
                                 base_sha: str = "HEAD", subset=None, db=None,
                                 trigger: str = "initial",
                                 created_by: str = None) -> dict:
    """THE single governance scan+persist core (scan-unify 2026-07-28).

    The ONE primitive shared by every governance trigger:
      - the standalone ``run_governance_pipeline`` (Send-to-Governance / API),
      - the in-pipeline END-GATE (``sdlc_state_machine._run_governance_endgate``), and
      - the standalone worker job (``workers.sdlc_worker.run_governance_review_job``).

    Runs one agentic CLI scan session PER SKILL in PARALLEL via ``scan_all_skills``
    (this is the change that made every trigger spawn N sessions instead of one), applies
    per-(product,repo) suppressions, DUAL-WRITES findings (legacy
    ``sdlc_governance_findings`` upsert) + an immutable scan snapshot (+ observations),
    renders the report, and returns the rich dict every caller reads. Never raises —
    fails CLOSED (blocking) on an unexpected internal error.

    ``base_sha`` labels the per-skill scan prompt's ``base_sha...HEAD`` reference; the
    diff itself is precomputed by the caller. Callers that clone-and-diff against the MR
    base branch should pass the merge-base SHA so the prompt label is accurate.

    Returns: ``{report, blocking, open_findings, suppressed, skills, skipped,
    scan_error(+scan_error_detail), diff_too_large, snapshot_id, domain_by_skill}``.
    - ``blocking``      = any non-suppressed finding at/above ``block_severity()``.
    - ``skipped``       = True when no bundle/skills resolve (governance simply not run).
    - ``scan_error``    = availability error (diff too large, or CLI could not complete)
                          — the caller SUSPENDS for a human retry, it is NOT a violation.
    """
    from agents.sdlc_governance import config as gov_config, engine as gov_engine
    from agents.sdlc_governance.engine import scan_all_skills
    from agents.sdlc_governance.schema import parse_findings, is_blocking
    from agents.sdlc_cli_engine import AinxtCliEngine

    logger.info(
        "[SDLC-GOV] unified scan start", run_id=run_id, trigger=trigger,
        changed_files=len(changed_files or []), diff_chars=len(diff_text or ""),
        base_sha=base_sha,
    )
    try:
        # 1. Diff-size cap (fail-closed toward a human): a diff above the file/byte cap
        #    is too large for one meaningful automated pass and would overflow the CLI
        #    --print argv token. Return the scan_error sentinel (with diff_too_large) so
        #    every caller SUSPENDS for manual review rather than crashing or passing.
        _too_large = gov_config.diff_cap_exceeded(changed_files, diff_text)
        if _too_large:
            logger.warning(
                "[SDLC-GOV] diff exceeds governance cap — routing to manual review",
                run_id=run_id, repo=repo, detail=_too_large,
                changed_files=len(changed_files or []), diff_chars=len(diff_text or ""),
            )
            return {
                "report": {"overall_verdict": "FAIL", "ref": "", "skills": [],
                           "report_md": f"Governance review not run — {_too_large}"},
                "blocking": False, "open_findings": [], "suppressed": [],
                "skills": [], "skipped": False,
                "scan_error": True, "scan_error_detail": _too_large, "diff_too_large": True,
                "snapshot_id": None, "domain_by_skill": {},
            }

        # 2. Resolve the governance bundle + selected skills.
        bundle, skills = gov_engine.select_skills(subset, phase="review")
        if not bundle or not skills:
            logger.warning(
                "[SDLC-GOV] no governance skills resolved — skipping governance scan",
                run_id=run_id, repo=repo,
            )
            return {"report": None, "blocking": False, "open_findings": [],
                    "suppressed": [], "skills": [], "skipped": True,
                    "scan_error": False, "diff_too_large": False,
                    "snapshot_id": None, "domain_by_skill": {}}

        # 3. Scan — ONE agentic session PER SKILL, in parallel (ThreadPoolExecutor).
        structured, domain_by_skill = scan_all_skills(
            engine=AinxtCliEngine(), bundle=bundle, skills=skills,
            workspace_root=workspace, diff_text=diff_text, changed_files=changed_files,
            base_sha=base_sha or "HEAD", model=gov_config.review_model(), run_id=run_id,
        )

        # 4. Scan-engine failure (a skill session hit max_turns / crashed / timed out)
        #    → scan_error sentinel. That is an availability error, NOT a real finding;
        #    surfacing it as a blocking violation would route it into the approval gate.
        if structured.get("_scan_error"):
            _err_detail = "; ".join(
                (f.get("detail") or "")
                for sk in (structured.get("skills") or [])
                for f in (sk.get("findings") or [])
                if isinstance(f, dict) and (f.get("detail") or "")
            )
            if len(_err_detail) > 300:
                _err_detail = _err_detail[:300] + "…"
            logger.warning(
                "[SDLC-GOV] unified scan could not complete — returning scan_error",
                run_id=run_id, detail=_err_detail,
            )
            return {
                "report": None, "blocking": False, "open_findings": [],
                "suppressed": [], "skills": [s.slug for s in skills],
                "skipped": False, "scan_error": True,
                "scan_error_detail": _err_detail, "diff_too_large": False,
                "snapshot_id": None, "domain_by_skill": domain_by_skill,
            }

        # 5. Suppress (per-(product,repo) active suppressions; fail toward surfacing).
        findings = parse_findings(structured)
        open_f, suppressed_f = gov_engine.apply_suppressions(findings, db, product_id, repo)
        _all = open_f + suppressed_f

        # 6. DUAL-WRITE: legacy findings upsert + immutable scan snapshot (both fail-safe;
        #    in repo/MR-mode there is no sdlc_runs row and both simply no-op).
        from store.sdlc_governance_findings import persist_findings, persist_snapshot
        persist_findings(run_id, _all, domain_by_skill)
        snapshot_id = None
        try:
            import hashlib as _hl
            from agents.sdlc_governance.bundle import (
                governance_bundle_version as _gbv, skill_versions as _skv,
            )
            snapshot_id = persist_snapshot(
                run_id, _all,
                diff_hash=_hl.sha256((diff_text or "").encode("utf-8", "ignore")).hexdigest(),
                bundle_version=_gbv(bundle), skill_versions=_skv(bundle, skills),
                trigger=trigger, created_by=created_by, domain_by_skill=domain_by_skill,
            )
        except Exception as _se:
            logger.warning("[SDLC-GOV] scan snapshot write skipped — non-fatal",
                           run_id=run_id, error=str(_se))

        # 7. Report + 8. blocking verdict.
        report = gov_engine.render_report(
            structured=structured, findings=_all, ref=bundle.ref, skills=skills,
            domain_by_skill=domain_by_skill,
        )
        threshold = gov_config.block_severity()
        blocking = any(is_blocking(f, threshold) for f in open_f)

        logger.info(
            "[SDLC-GOV] unified scan verdict", run_id=run_id,
            overall=(report or {}).get("overall_verdict"),
            open=len(open_f), suppressed=len(suppressed_f), snapshot_id=snapshot_id,
        )
        return {
            "report": report, "blocking": blocking, "open_findings": open_f,
            "suppressed": suppressed_f, "skills": [s.slug for s in skills],
            "skipped": False, "scan_error": False, "diff_too_large": False,
            "snapshot_id": snapshot_id, "domain_by_skill": domain_by_skill,
        }
    except Exception as _ge:
        logger.error("[SDLC-GOV] unified scan errored — fail-closed blocking",
                     run_id=run_id, error=str(_ge))
        return {
            "report": {"overall_verdict": "FAIL", "ref": "", "skills": [],
                       "report_md": f"Governance scan errored: {_ge}"},
            "blocking": True, "open_findings": [], "suppressed": [],
            "skills": [], "skipped": False, "scan_error": False,
            "diff_too_large": False, "snapshot_id": None, "domain_by_skill": {},
        }


def _post_governance_mr_note_if_present(run_id: str, repo: str) -> None:
    """
    STEP 11 (2026-07-17) — best-effort governance MR note delivery.

    Called AFTER the post-gate CodingStateMachine.run() returns (the point at
    which COMMITTING/MR_CREATION has already happened inside the SM — the MR
    does not exist yet at GOVERNANCE_REVIEW time, hence posting here rather
    than from the governance phase itself). Re-reads the run row for pr_number
    (set by the SM's MR creation) and, when both an MR and a GOVERNANCE_REPORT
    artifact exist, posts/updates the governance note on the MR. Never raises —
    a note-post failure must never fail the pipeline or mask the MR.
    """
    try:
        run = get_run(run_id)
        pr_number = (run or {}).get("pr_number")
        if not pr_number:
            return  # no MR was created on this pass (suspended / commit failed)
        from store.sdlc_artifacts import _load_latest_artifact
        artifact = _load_latest_artifact(run_id, "GOVERNANCE_REPORT")
        if not artifact:
            return  # governance review didn't run for this run — nothing to post
        report_md = (artifact.get("payload") or {}).get("report_md") or ""
        if not report_md.strip():
            return
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_post_governance_note as _post_gov_note
        else:
            from tools.gitlab_tools import gitlab_post_governance_note as _post_gov_note
        result = _post_gov_note(repo, int(pr_number), report_md)
        logger.info(f"[SDLC-GOV] MR note posted for run={run_id} MR=!{pr_number}: {result}")
    except Exception as e:
        logger.warning(f"[SDLC-GOV] _post_governance_mr_note_if_present failed for run={run_id} (best-effort): {e}")

# ============================================================
# AGENT PROMPTS  — each agent has a single responsibility
# ============================================================


def _detect_language(files: list) -> str:
    """Detect primary language from a list of file paths. Raises if undetectable."""
    exts = [str(f).rsplit(".", 1)[-1].lower() for f in files if "." in str(f)]
    for ext in exts:
        if ext == "java":
            return "java"
        if ext in ("js", "jsx", "ts", "tsx", "mjs", "cjs"):
            return "javascript"
        if ext == "go":
            return "go"
        if ext in ("cs", "csx"):
            return "csharp"
        if ext == "rb":
            return "ruby"
        if ext in ("py", "pyw"):
            return "python"
        if ext in ("kt", "kts"):
            return "kotlin"
        if ext == "scala":
            return "scala"
        if ext == "rs":
            return "rust"
        if ext == "swift":
            return "swift"
        if ext in ("cpp", "cc", "cxx", "hpp"):
            return "cpp"
        if ext == "c":
            return "c"
    raise RuntimeError(
        f"Language undetectable: no recognized source extension in {exts[:10]}. "
        "Ensure GITLAB_TOKEN is configured and repo is in namespace/project format, "
        "or index the codebase via CodebaseManager."
    )


_LANG_HINT_CODER = {
    "java":       "Java (use JUnit 5 for tests)",
    "javascript": "JavaScript/TypeScript (use Jest + supertest for tests)",
    "go":         "Go (use table-driven tests with testing package)",
    "csharp":     "C# (use xUnit for tests)",
    "ruby":       "Ruby (use RSpec for tests)",
    "python":     "Python (use pytest for tests)",
}

_LANG_HINT_SLT = {
    "java":       "Java (JUnit 5 + RestAssured for HTTP tests)",
    "javascript": "JavaScript/TypeScript (Jest + supertest for HTTP tests)",
    "go":         "Go (table-driven tests + httptest for HTTP tests)",
    "csharp":     "C# (xUnit + HttpClient for HTTP tests)",
    "ruby":       "Ruby (RSpec + Faraday for HTTP tests)",
    "python":     "Python (pytest + requests/httpx for HTTP tests)",
}


def _prompt_coder(solution: str, language: str, jira_key: str, repo_ctx: dict = None) -> str:
    lang_hint  = _LANG_HINT_CODER.get(language, _LANG_HINT_CODER["python"])
    ctx        = repo_ctx or {}
    file_tree  = ctx.get("file_tree", "")
    tech       = ctx.get("tech_stack", "Unknown")
    framework  = ctx.get("framework", "")

    return f"""You are an expert {lang_hint.split("(")[0].strip()} developer implementing a Jira ticket.

=== CONTEXT ===
Jira: {jira_key}
Tech Stack: {tech} | Framework: {framework}

=== SOLUTION DESIGN ===
{solution}

=== ACTUAL REPO STRUCTURE ===
{file_tree}

=== CRITICAL RULES ===
1. ONLY write code for files listed in the solution design's implementation_plan.
2. Match the EXACT coding style, naming conventions, and patterns visible in the file tree.
   - If the repo uses camelCase functions → use camelCase.
   - If imports use relative paths → use relative paths.
   - If the project uses DI containers → follow the same pattern.
3. Do NOT add new dependencies not already in the project (package.json/pom.xml/go.mod).
4. Write COMPLETE, production-ready code — no TODOs, no placeholders, no stub functions.
5. For any file that MODIFIES an existing file: write the complete file content as it should
   look after the change (not a diff, not just the changed lines — the full file).
6. Unit tests: use {lang_hint.split("use")[-1].strip().rstrip(")")} conventions matching the
   existing test directory pattern in the file tree.
7. Do NOT include secrets, credentials, or card/PAN data.

Output ONLY this JSON (no markdown fences, no prose):
{{
  "files": [
    {{
      "path": "exact/path/matching/convention.ext",
      "content": "complete file content here...",
      "is_test": false
    }}
  ],
  "summary": "What was implemented and why each file was changed"
}}"""


def _prompt_slt_creator(solution: str, language: str, repo_ctx: dict = None) -> str:
    if not language:
        raise RuntimeError("SDLC _prompt_slt_creator: language is required — detect from GitLab first.")
    lang_hint  = _LANG_HINT_SLT.get(language, f"{language} (use the appropriate test framework)")
    ctx        = repo_ctx or {}
    file_tree  = ctx.get("file_tree", "")
    tech       = ctx.get("tech_stack", "Unknown")
    test_fw    = ctx.get("test_framework") or language

    # Derive test path from the actual repo file tree — never hardcode a fake package.
    # For Java: find existing test files and use their package root + /slt/
    # For other languages: fall back to sensible lang-specific defaults.
    import re as _re_slt
    _test_path_defaults = {
        "javascript": "src/__tests__/slt/",
        "typescript": "src/__tests__/slt/",
        "go":         "tests/slt/",
        "python":     "tests/slt/",
        "csharp":     "tests/Slt/",
        "ruby":       "spec/slt/",
    }
    if language == "java":
        _java_test_lines = [
            ln.strip() for ln in (ctx.get("file_tree", "") or "").splitlines()
            if "src/test/java/" in ln
        ]
        if _java_test_lines:
            # e.g. "src/test/java/com/ainxt/payment/service/PaymentServiceTest.java"
            # → extract "src/test/java/com/ainxt/payment/" and append "slt/"
            _m = _re_slt.match(r'(src/test/java/(?:\w+/)+)', _java_test_lines[0])
            test_path = (_m.group(1) + "slt/") if _m else "src/test/java/slt/"
        else:
            test_path = "src/test/java/slt/"
    else:
        test_path = _test_path_defaults.get(language, "tests/slt/")

    return f"""You are a senior QA engineer writing Service Level Tests (SLTs) — API-level integration tests.

=== CONTEXT ===
Tech Stack: {tech}
Test Framework: {lang_hint}
SLT location convention: {test_path}

=== SOLUTION TO TEST ===
{solution}

=== ACTUAL REPO STRUCTURE ===
{file_tree}

=== RULES ===
1. Write SLTs in {language} using {test_fw} — the project's ACTUAL test framework.
2. Place test files in the {test_path} directory, following the naming convention in the file tree.
3. SLTs test the API contract end-to-end: real HTTP calls (or in-process service calls), not unit tests.
4. Cover: happy path, validation errors, auth failures, boundary conditions, idempotency where relevant.
5. Use the same import/module patterns as existing test files visible in the file tree.
6. Do NOT mock everything — SLTs use real dependencies (test DB, real service instances).
7. Include proper setup/teardown (before/after hooks) for test isolation.
8. Assertions must be SPECIFIC — check exact status codes, response fields, DB state.

Output ONLY this JSON (no markdown fences, no prose):
{{
  "slt_files": [
    {{
      "path": "{test_path}feature_name_slt.ext",
      "content": "complete SLT file content..."
    }}
  ],
  "test_scenarios": [
    "Scenario 1: [method] [endpoint] with [condition] → expects [exact result]",
    "..."
  ]
}}"""


def _prompt_code_reviewer(code_output: str, language: str = "python", repo_ctx: dict = None) -> str:
    tech = (repo_ctx or {}).get("tech_stack", "")
    return f"""You are a principal engineer reviewing AI-generated code before it's committed.
Tech Stack: {tech} | Language: {language}

Code to Review:
{code_output}

Review against these criteria — be SPECIFIC, name exact files/functions/lines:
1. Correctness: Does each file implement ONLY what the solution design asked for?
   Flag any code that goes beyond the ticket scope.
2. Minimal changes: Is the implementation lean? Flag unnecessary abstractions, over-engineering,
   or files that shouldn't need changing.
3. Code quality: Naming conventions, error handling, no dead code, no commented-out blocks.
4. Security: SQL injection, XSS, hardcoded credentials, insecure random, unsafe deserialization.
5. PCI/DSS: No card numbers, CVVs, or PANs in code, logs, or comments.
6. Test quality: Are tests actually testing the new code paths (not just import-and-pass)?
7. Score 1-10. Score < 8 blocks the pipeline. Be strict.

Output ONLY valid JSON:
{{
  "score": <int 1-10>,
  "approved": <true|false>,
  "critical_issues": ["[filename.ext]: specific issue — why it's a blocker"],
  "improvements": ["[filename.ext]: specific suggestion"],
  "security_issues": ["[filename.ext]: exact vulnerability"],
  "pci_issues": ["[filename.ext]: exact PCI concern"]
}}"""


def _prompt_slt_reviewer(slt_output: str, language: str = "python", repo_ctx: dict = None) -> str:
    tech    = (repo_ctx or {}).get("tech_stack", "")
    test_fw = (repo_ctx or {}).get("test_framework", "")
    return f"""You are a senior QA engineer reviewing AI-generated service-level tests.
Tech Stack: {tech} | Test Framework: {test_fw}

SLTs to Review:
{slt_output}

Review criteria:
1. Language correctness: Are SLTs written in the RIGHT language ({language}) with the right framework ({test_fw})?
   If tests are in the wrong language, score = 1, approved = false, and flag it.
2. Coverage: Do tests cover the actual API endpoints/functions changed? Are happy path, error path,
   and edge cases all present?
3. Assertions: Are assertions SPECIFIC (exact status codes, response field names, values)?
   "assert response.ok" is NOT sufficient — flag it.
4. Isolation: Are there setup/teardown hooks? Can tests run in any order without side effects?
5. Reliability: Any hardcoded timestamps, random values, or sleep() calls that cause flakiness?

Output ONLY valid JSON:
{{
  "score": <int 1-10>,
  "approved": <true|false>,
  "wrong_language": <true|false>,
  "gaps": ["Missing test for [specific scenario]"],
  "improvements": ["[filename]: specific suggestion"]
}}"""


def _prompt_pr_reviewer(pr_diff: str, jira_key: str, repo_ctx: dict = None) -> str:
    tech = (repo_ctx or {}).get("tech_stack", "")
    return f"""You are a principal engineer doing a thorough code review before merge.

Jira ticket: {jira_key}
Tech stack: {tech}

PR diff (unified format — line numbers are the RIGHT-side line numbers after the change):
{pr_diff}

Review against these criteria:
1. SCOPE — Does the PR touch ONLY what the ticket requires? Flag unrelated changes.
2. CORRECTNESS — Does the logic correctly implement the requirement?
3. TESTS — Are tests added/updated? Do they cover the new code paths?
4. SECURITY — SQL injection, command injection, exposed secrets, insecure deserialization.
5. PCI/DSS — No card data, credentials, or PII in code, comments, logs, or test fixtures.
6. MERGE RISK — Breaking API changes, missing migrations, dependency conflicts.

IMPORTANT — format for inline GitLab MR comments:
- For each issue/suggestion, use "[filepath:line_number]: message" format when you know the exact line.
- Use "[filepath]: message" when the issue is file-level (no specific line).
- Line numbers must match the diff shown above (right-side / new file line numbers).

Output ONLY valid JSON (no prose outside the JSON):
{{
  "approved": <true|false>,
  "score": <int 1-10>,
  "blocking_issues": ["[src/payment.py:42]: hardcoded secret in plain text"],
  "suggestions":     ["[src/utils.py:17]: extract this into a shared helper"],
  "security_flags":  ["[config/settings.py:8]: API key should come from env var"],
  "summary": "One concise paragraph — what is good, what is blocked, overall recommendation"
}}"""


# ============================================================
# STRUCTURED OUTPUT FORMATTERS
# Convert raw JSON dicts from each agent stage into
# human-readable Markdown blocks for Jira / Inbox / UI.
# ============================================================

def _code_path(val) -> str:
    """Normalize code_path (a function call chain) to a display string."""
    if isinstance(val, list):
        return " → ".join(str(x) for x in val if x)
    return str(val) if val else ""


def _s(item) -> str:
    """Coerce any LLM output item (str, dict, list, or other) to a plain string."""
    if isinstance(item, str):
        return item
    if isinstance(item, list):
        # Flatten nested list → join non-empty string parts
        return "; ".join(_s(x) for x in item if x is not None and x != "")
    if isinstance(item, dict):
        return (item.get("name") or item.get("component") or item.get("file")
                or item.get("path") or item.get("step") or item.get("description")
                or item.get("text") or item.get("value") or item.get("content")
                or str(item))
    return str(item)


def _fmt_classifier(c: dict) -> str:
    """Format classifier output as structured markdown."""
    components = c.get("affected_components") or []
    comp_str   = ", ".join(_s(x) for x in components) if components else "—"
    deps       = c.get("dependencies") or []
    dep_str    = ", ".join(_s(x) for x in deps) if deps else "None"
    risks      = c.get("risks") or []
    risk_str   = "; ".join(_s(x) for x in risks) if risks else "None"
    return (
        f"**Summary:** {c.get('core_intent', '—')}\n"
        f"**Scope:** {dep_str}\n"
        f"**Complexity:** {c.get('complexity', '—')}  |  Effort: {c.get('effort_estimate', '—')}\n"
        f"**Impacted Services:** {comp_str}\n"
        f"**Risks:** {risk_str}"
    )


# Keyword sets MUST mirror CodingStateMachine._derive_file_plan's prune so the
# HITL approval gate shows exactly the file set the coder will actually produce.
_TEST_PATH_KWS = ("test", "spec")
_SLT_PATH_KWS  = ("slt",)


def _path_is_test(path: str) -> bool:
    pl = (path or "").lower()
    return any(k in pl for k in _TEST_PATH_KWS) or any(k in pl for k in _SLT_PATH_KWS)


def _path_is_slt(path: str) -> bool:
    return any(k in (path or "").lower() for k in _SLT_PATH_KWS)


def _resolve_skip_flags(run_id: str) -> tuple[bool, bool]:
    """Read (skip_tests, skip_slt) from the run context, honouring SDLC_SKIP_TESTS
    env for skip_tests. Same parsing as the resume-after-approval path."""
    ctx = (get_run(run_id) or {}).get("context", {}) or {}
    _st_env = os.getenv("SDLC_SKIP_TESTS", "").lower() in ("1", "true", "yes")
    _st_raw = ctx.get("skip_tests", _st_env)
    _ss_raw = ctx.get("skip_slt", False)
    skip_tests = _st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")
    skip_slt   = _ss_raw if isinstance(_ss_raw, bool) else str(_ss_raw).lower() in ("1", "true", "yes")
    return skip_tests, skip_slt


def _prune_test_files_for_skip(run_id: str, analysis: dict, design: dict) -> None:
    """When skip_slt / skip_tests is set on the run, strip test/SLT entries from
    the analysis + design dicts IN PLACE *before* the HITL approval gate.

    Previously this prune only happened in CodingStateMachine._derive_file_plan
    (CODING, after approval), so the formatted solution, Confluence doc, GitLab
    issue and repos[] payload all still listed test/SLT files the approver had
    asked to skip. Doing it here means the gate shows the real scope.

    Semantics mirror _derive_file_plan exactly:
      * skip_tests → drop any path containing 'test'/'spec'/'slt'
      * skip_slt   → drop only SLT paths (unit tests from the coder still allowed)
    No-op when neither flag is set.
    """
    skip_tests, skip_slt = _resolve_skip_flags(run_id)
    if not (skip_tests or skip_slt):
        return

    def _path_of(x) -> str:
        if isinstance(x, str):
            return x
        if isinstance(x, dict):
            return x.get("path") or x.get("file") or x.get("name") or ""
        return _s(x)

    def _drop(path: str) -> bool:
        if skip_tests and _path_is_test(path):
            return True
        if skip_slt and _path_is_slt(path):
            return True
        return False

    removed: list = []

    if isinstance(analysis, dict):
        for key in ("files_to_change", "new_files_needed"):
            v = analysis.get(key)
            if isinstance(v, list):
                analysis[key] = [x for x in v if not _drop(_path_of(x))]
                removed += [_path_of(x) for x in v if _drop(_path_of(x))]

    if isinstance(design, dict):
        cc = design.get("code_changes")
        if isinstance(cc, list):
            design["code_changes"] = [c for c in cc if not _drop(_path_of(c))]
            removed += [_path_of(c) for c in cc if _drop(_path_of(c))]

        # Explicit test descriptors ("tests/path: test_x asserts ..."). Drop all
        # under skip_tests; under skip_slt drop only the SLT ones.
        for key in ("tests_to_add", "test_files"):
            v = design.get(key)
            if isinstance(v, list):
                kept = []
                for t in v:
                    p = _path_of(t) or _s(t)
                    if skip_tests or (skip_slt and _path_is_slt(p)):
                        removed.append(p)
                    else:
                        kept.append(t)
                design[key] = kept

        # implementation_plan steps that name a dropped file (pattern "[path.ext]").
        ip = design.get("implementation_plan")
        if isinstance(ip, list):
            import re as _re_ip
            kept = []
            for step in ip:
                m = _re_ip.search(r'\[([^\]]+\.[a-zA-Z0-9]{1,6})\]', _s(step))
                if m and _drop(m.group(1)):
                    removed.append(m.group(1))
                    continue
                kept.append(step)
            design["implementation_plan"] = kept

    if removed:
        logger.info(
            f"[SDLC {run_id}] HITL prune: removed {len(removed)} test/SLT entry(ies) "
            f"from design before approval (skip_tests={skip_tests}, skip_slt={skip_slt}): "
            f"{removed[:8]}"
        )


def _fmt_solution(d: dict, analysis: dict = None) -> str:
    """
    Format solution designer output as structured markdown.
    Skips sections that have no real content — never shows bare dashes.
    Degrades gracefully when JSON parsing failed (d has 'raw' key).
    """
    import re as _re
    analysis = analysis or {}
    parts    = []

    # ── Architecture / approach ─────────────────────────────────────────────
    arch = (
        d.get("solution_approach")
        or "\n".join(_s(x) for x in (d.get("implementation_plan") or []))
        or None
    )
    # If JSON parse failed, fall back to raw LLM text (first 600 chars)
    if not arch and d.get("raw"):
        raw_snippet = d["raw"].strip()[:600]
        arch = raw_snippet if len(raw_snippet) > 20 else None
    if arch:
        parts.append(f"**Architecture:**\n{arch}")

    # ── Files Affected ───────────────────────────────────────────────────────
    # W-E (C1): render the FULL labeled scope at the HITL gate — every path,
    # each labeled NEW vs EDIT, with a count line. Approval at this gate then
    # equals approval of the complete scope (no silent truncation downstream).
    files_aff = [_s(f) for f in (analysis.get("files_to_change") or [])]   # EDIT
    new_files = [_s(f) for f in (analysis.get("new_files_needed") or [])]  # NEW
    # Preserve order, de-dupe; an entry appearing in BOTH lists is treated as
    # EDIT (a pre-existing file the analyst also tagged "new" → real file wins).
    _new_set = {p for p in new_files if p}
    _edit_set = {p for p in files_aff if p}
    _labeled: list = []   # (path, label) pairs, order-stable, de-duped
    _seen_paths: set = set()
    for _fp in files_aff + new_files:
        if not _fp or _fp in _seen_paths:
            continue
        _seen_paths.add(_fp)
        _label = "NEW" if (_fp in _new_set and _fp not in _edit_set) else "EDIT"
        _labeled.append((_fp, _label))

    # If analysis gave no files, mine them from implementation_plan (pattern:
    # "[path.ext]"). Mined paths reference existing files → label EDIT.
    if not _labeled:
        plan = d.get("implementation_plan") or []
        for step in plan:
            m = _re.search(r'\[([^\]]+\.[a-zA-Z0-9]{1,6})\]', str(step))
            if m:
                fp = _s(m.group(1))
                if fp and fp not in _seen_paths:
                    _seen_paths.add(fp)
                    _labeled.append((fp, "EDIT"))
    # Also check code_changes (bug pipeline) — these MODIFY existing files.
    if not _labeled:
        for chg in (d.get("code_changes") or []):
            if not isinstance(chg, dict):
                continue
            fp = _s(chg.get("file", ""))
            if fp and fp not in _seen_paths:
                _seen_paths.add(fp)
                _labeled.append((fp, "EDIT"))

    if _labeled:
        _edits = sum(1 for _, lbl in _labeled if lbl == "EDIT")
        _news  = sum(1 for _, lbl in _labeled if lbl == "NEW")
        _scope_line = (
            f"Scope: {len(_labeled)} file{'s' if len(_labeled) != 1 else ''} "
            f"— {_edits} edit{'s' if _edits != 1 else ''}, {_news} new"
        )
        # _s() on every path before join; no truncation — render EVERY file.
        file_str = "\n".join(f"  • [{lbl}] `{_s(fp)}`" for fp, lbl in _labeled)
        parts.append(f"**Files Affected ({_scope_line}):**\n{file_str}")

    # ── DB / API changes — only show when non-trivial ───────────────────────
    db_chg = str(d.get("data_model_changes") or "").strip()
    if db_chg and db_chg.lower() not in ("none", "n/a", "—", "-", "null", "{}"):
        parts.append(f"**Database Changes:** {db_chg}")

    api_chg = str(d.get("api_changes") or "").strip()
    if api_chg and api_chg.lower() not in ("none", "n/a", "—", "-", "null", "{}"):
        parts.append(f"**API Changes:** {api_chg}")

    # ── Testing strategy ────────────────────────────────────────────────────
    testing = str(d.get("testing_strategy") or "").strip()
    if testing and testing.lower() not in ("none", "n/a", "—", "-", "null", "{}"):
        parts.append(f"**Testing:** {testing}")

    if not parts:
        return "Design details pending. Engineer review required."
    return "\n\n".join(parts)


def _fmt_bug_solution(fix: dict) -> str:
    """Format bug solutioning output as thorough structured markdown."""
    rca_text = fix.get("root_cause_analysis") or fix.get("fix_description") or "—"
    approach = fix.get("fix_approach") or ""
    impact   = fix.get("impact_analysis") or ""

    changes   = fix.get("code_changes") or []
    files_str = "\n".join(
        f"  • `{c.get('file', '')}` — {c.get('change', '')}"
        for c in changes if isinstance(c, dict) and c.get("file")
    ) or "  —"

    risk     = fix.get("regression_risk") or "—"
    risk_exp = fix.get("regression_explanation") or ""

    tests     = fix.get("tests_to_add") or []
    tests_str = "\n".join(f"  {i+1}. {_s(t)}" for i, t in enumerate(tests)) or "  —"

    verify     = fix.get("verification_steps") or []
    verify_str = "\n".join(f"  {i+1}. {_s(v)}" for i, v in enumerate(verify)) or "  —"

    parts = [f"**Root Cause Analysis:**\n{rca_text}"]
    if approach:
        parts.append(f"**Fix Approach:**\n{approach}")
    if impact:
        parts.append(f"**Impact Analysis:**\n{impact}")
    parts.append(f"**Files to Change:**\n{files_str}")
    risk_line = f"**Regression Risk:** {risk}"
    if risk_exp:
        risk_line += f"\n{risk_exp}"
    parts.append(risk_line)
    parts.append(f"**Tests to Add:**\n{tests_str}")
    parts.append(f"**Verification Steps:**\n{verify_str}")

    return "\n\n".join(parts)


def _fmt_coding(files: list, pr_url: str = "") -> str:
    """Format coding agent output as structured markdown."""
    created  = [f["path"] for f in files if isinstance(f, dict) and not f.get("is_test") and f.get("path")]
    modified = []   # state machine does file-level git ops; new files are "created"
    tests    = [f["path"] for f in files if isinstance(f, dict) and f.get("is_test") and f.get("path")]
    created_str  = "\n".join(f"  • {p}" for p in created)  or "  —"
    modified_str = "\n".join(f"  • {p}" for p in modified) or "  —"
    tests_str    = "\n".join(f"  • {p}" for p in tests)    or "  —"
    pr_str = pr_url if pr_url else "Pending"
    return (
        f"**Files Created:**\n{created_str}\n\n"
        f"**Files Modified:**\n{modified_str}\n\n"
        f"**Tests Created:**\n{tests_str}\n\n"
        f"**PR Link:** {pr_str}"
    )


def _fmt_bug_triage(t: dict) -> str:
    """Format bug classifier/triage output as structured markdown."""
    components = t.get("affected_components") or []
    comp_str   = ", ".join(_s(x) for x in components) if components else "—"
    steps      = t.get("triage_steps") or []
    steps_str  = "\n".join(f"  {i+1}. {_s(s)}" for i, s in enumerate(steps)) or "  —"
    return (
        f"**Summary:** Bug triage complete\n"
        f"**Scope:** {t.get('category', '—')}\n"
        f"**Complexity:** {t.get('severity', '—')} severity  |  Repro: {t.get('reproduction', '—')}\n"
        f"**Impacted Services:** {comp_str}\n\n"
        f"**Triage Steps:**\n{steps_str}"
    )


# ============================================================
# SELF-REVIEW LOOP
# Agents critique their own output before returning.
# Max 2 iterations per stage.
# ============================================================

def _self_review(output: str, criteria: str, max_iter: int = 2) -> str:
    # The reviewer sees the FULL output by default. Capping at 2000 (or any
    # small number) silently drops trailing JSON fields when the reviewer
    # rewrites — that's what caused open_questions to vanish for every run.
    # Modern context windows handle 200K+ tokens; an analyst JSON of 5-20 KB
    # is well within budget.
    #
    # The safety cap below only triggers on pathological cases (LLM goes
    # off-script and emits >50K chars). In that case we middle-truncate so
    # both head and tail survive — never head-only, which is what bit us.
    _MAX_REVIEW_INPUT = 50_000  # chars; ~12.5K tokens — generous for any sane JSON

    def _safe_view(s: str) -> str:
        if len(s) <= _MAX_REVIEW_INPUT:
            return s
        half = _MAX_REVIEW_INPUT // 2
        logger.warning(
            f"[SDLC] _self_review: output is {len(s)} chars (>{_MAX_REVIEW_INPUT}) — "
            f"middle-truncating before review. This is unusual; check the upstream LLM call."
        )
        return (
            s[:half]
            + f"\n\n... [middle truncated; {len(s) - 2*half} chars omitted to fit review budget] ...\n\n"
            + s[-half:]
        )

    # Strip bare fence artifacts before any evaluation — catches the "json\n{...}"
    # pattern that the synthesis step emits when the model ignores the JSON-only constraint.
    output = _strip_llm_json_fences(output)

    # Fast path: if the output is already parseable JSON, skip the review loop.
    # Criteria is structural (field presence, valid JSON) — a valid JSON object
    # satisfies the structural bar; the reviewer cannot improve it further and
    # should say APPROVED anyway. Avoids unnecessary Opus calls and the risk of
    # the reviewer replacing valid JSON with explanatory prose.
    try:
        _pre_check = json.loads(output.strip())
        if isinstance(_pre_check, dict):
            logger.info(
                f"[SDLC.review] pre-check: output is valid JSON ({len(output)} chars) — skipping review loop"
            )
            return output
    except Exception:
        pass

    logger.info(
        f"[SDLC.review] pre-check: output is NOT valid JSON ({len(output)} chars) — "
        f"starting review loop (max_iter={max_iter}). head={output[:200]!r}"
    )

    _best_json   = None   # best valid-JSON string seen across iterations
    _repair_mode = False  # flip to True when approved-but-unrescuable: next pass repairs, not reviews

    for iteration in range(max_iter):
        if _repair_mode:
            # The reviewer approved the content but the JSON is malformed/truncated.
            # Re-sending "review" produces another "APPROVED" with no fix.
            # Switch to an explicit repair prompt so the model outputs valid JSON.
            review_prompt = (
                f"The following JSON is malformed or truncated. It was approved on content "
                f"but is NOT valid JSON. Output ONLY the complete, repaired JSON — "
                f"do NOT output 'APPROVED', no markdown fences, no prose.\n\n"
                f"Use this as the schema guide:\n{criteria}\n\n"
                f"Malformed JSON to repair:\n{_safe_view(output)}"
            )
            _repair_mode = False
        else:
            review_prompt = f"""Review this AI output against the following criteria.
If the output meets ALL criteria, respond with exactly: APPROVED
If not, respond with the corrected output ONLY — no preamble, no explanation, no markdown fences.
CRITICAL: If the expected output format is JSON, your corrected response MUST be raw JSON only.
Never wrap in prose, never add an explanation header, never use markdown code fences.

When you emit a corrected version, you MUST preserve EVERY field present in the
original output — do not drop fields just because they are not listed in the
criteria. Criteria below is the minimum bar, not an exhaustive schema.

Criteria:
{criteria}

Output to review:
{_safe_view(output)}"""
        # Use Sonnet (complex tier) for format correction — this is JSON extraction,
        # not reasoning. Opus is not needed here and iter 2 is almost always trivial.
        result = _llm(review_prompt, hint="complex")
        # Strip fences from the reviewer's response before any evaluation
        result = _strip_llm_json_fences(result) if result else result
        _is_approved = bool(result and result.strip().upper().startswith("APPROVED"))
        _result_is_json = False
        if not _is_approved and result:
            try:
                if result.strip().startswith("{"):
                    json.loads(result)
                    _result_is_json = True
            except Exception:
                # Starts with { but not valid JSON — try _parse_json rescue
                _rescued_dict = _parse_json(result)
                if not _is_raw_fallback(_rescued_dict):
                    result = json.dumps(_rescued_dict, ensure_ascii=False)
                    _result_is_json = True
        logger.info(
            f"[SDLC.review] iter={iteration + 1}/{max_iter} approved={_is_approved} "
            f"result_is_json={_result_is_json} result_len={len(result) if result else 0} "
            f"head={(result[:200] if result else '')!r}"
        )
        if _is_approved:
            # Trust but verify: the reviewer can APPROVE malformed output.
            # Validate before returning so a wrong APPROVED doesn't propagate.
            try:
                json.loads(output)
                return output  # output is valid JSON — trust the APPROVED
            except Exception:
                pass
            # output doesn't parse cleanly — try fence-strip + _parse_json rescue
            _approved_rescued = _parse_json(_strip_llm_json_fences(output))
            if not _is_raw_fallback(_approved_rescued):
                logger.info(
                    f"[SDLC.review] iter={iteration + 1}/{max_iter} APPROVED but output "
                    f"was malformed — rescued via _parse_json"
                )
                return json.dumps(_approved_rescued, ensure_ascii=False)
            # Rescue failed. Return best JSON seen so far, or fall through.
            if _best_json:
                logger.warning(
                    f"[SDLC.review] iter={iteration + 1}/{max_iter} APPROVED but output "
                    f"malformed + unrescuable — returning best JSON from prior iteration"
                )
                return _best_json
            logger.warning(
                f"[SDLC.review] iter={iteration + 1}/{max_iter} APPROVED but output "
                f"malformed, no prior good JSON — switching to repair mode"
            )
            # Switch the next iteration to an explicit JSON-repair prompt.
            # Re-sending the same review prompt gets the same APPROVED response.
            _repair_mode = True
        else:
            if _result_is_json:
                _best_json = result
                # Fast-path: this iter corrected the output to valid JSON.
                # The next iteration would just receive this JSON and say "APPROVED" —
                # that is the entire pattern costing one extra Opus call per analyst/designer
                # invocation. Skip it: return the valid JSON now.
                logger.info(
                    f"[SDLC.review] iter={iteration + 1}/{max_iter}: corrected to valid JSON "
                    f"— fast-path exit (skipping redundant verify iteration)"
                )
                return result
            output = result

    # Post-loop: nothing cleanly approved. Try _parse_json one last time on whatever we have.
    _final_candidate = _strip_llm_json_fences(output)
    _final_dict = _parse_json(_final_candidate)
    if not _is_raw_fallback(_final_dict):
        logger.warning(
            f"[SDLC] _self_review: loop ended without APPROVED — "
            f"rescued final output via _parse_json ({len(_final_candidate)} chars)"
        )
        return json.dumps(_final_dict, ensure_ascii=False)

    logger.warning(
        f"[SDLC] _self_review: could not APPROVE after {max_iter} iteration(s) — "
        f"returning {'best JSON from iteration' if _best_json else 'last iteration result'}"
    )
    return _best_json if _best_json else output


# ============================================================
# PRE-FLIGHT CHECK — runs before every pipeline, fails fast
# ============================================================

def _preflight_check(issue: dict, run_id: str) -> bool:
    """
    Validate all external credentials and connectivity before any pipeline work.
    Checks (in order):
      1. GitLab token exists in user_tokens for the triggering user
      2. GitLab token can actually reach the target repo (GET /projects/{repo})
      3. JIRA token exists in user_tokens for the triggering user
      4. JIRA token can actually read the target ticket (GET /issue/{key})

    Logs a clear table of pass/fail for every check.
    On any HARD failure → updates run to FAILED immediately and returns False.
    JIRA failure is SOFT (warns but doesn't block — JIRA writes are nice-to-have).
    """
    import os as _os
    import urllib.request, urllib.error
    from core.config import SCM_PROVIDER as _SCM_PF

    user_id    = issue.get("triggered_by_user_id", "")  # JWT sub — direct key to user_tokens
    user_email = issue.get("triggered_by_email", "")    # display label only
    repo       = issue.get("repo", "").strip()
    jira_key   = issue.get("key", "").strip()
    gl_url     = _os.getenv("GITLAB_URL", "https://<YOUR_GITLAB_URL>").rstrip("/")
    jira_url   = _os.getenv("JIRA_URL",   "").rstrip("/")
    who        = user_email or user_id or "anonymous"
    _provider_label = "GitHub" if _SCM_PF == "github" else "GitLab"
    _token_env_key  = "GITHUB_TOKEN" if _SCM_PF == "github" else "GITLAB_TOKEN"

    results: list[dict] = []

    def _add(check: str, status: str, detail: str):
        icon = "✅" if status == "PASS" else ("⚠️" if status == "WARN" else "❌")
        results.append({"check": check, "status": status, "detail": detail, "icon": icon})
        logger.info(f"[PREFLIGHT] {icon} {check}: {detail}")

    # ── 1. SCM token lookup ────────────────────────────────────
    # Resolution: user_tokens[user_id] → env var fallback
    # user_id is the JWT sub claim = UUID primary key in users table = FK in user_tokens
    gl_token = ""
    if not user_id:
        _add(f"{_provider_label} token lookup", "WARN",
             "No triggered_by_user_id in issue_dict — pipeline triggered without user context "
             f"(JIRA webhook / Threads). Falling back to {_token_env_key} env var.")
        gl_token = _os.getenv(_token_env_key, "")
        if not gl_token:
            _add(f"{_provider_label} token (env fallback)", "FAIL",
                 f"{_token_env_key} env var is also empty. "
                 "Trigger via UI so user_id is available, or set the token env var in .env.")
            update_run_state(run_id, "FAILED",
                error=f"Pre-flight FAILED: no {_provider_label} token available. "
                      f"Trigger via UI (user must have {_provider_label} PAT in Profile → {_provider_label} Token) "
                      f"or set {_token_env_key} env var for webhook-triggered runs.")
            _emit_preflight_event(run_id, results)
            return False
        else:
            _add(f"{_provider_label} token (env fallback)", "WARN",
                 f"Using {_token_env_key} env var (len={len(gl_token)}). "
                 f"Per-user token preferred — user should add PAT in Profile → {_provider_label} Token.")
    else:
        try:
            from core.platform_credentials import get_scm_token as _get_scm_pf
            gl_token = _get_scm_pf(user_id=user_id)   # direct lookup: user_tokens WHERE user_id=?
            _add(f"{_provider_label} token lookup", "PASS",
                 f"Found {_provider_label} PAT for user_id={user_id!r} ({user_email}) "
                 f"in user_tokens (len={len(gl_token)})")
        except PermissionError:
            # User-triggered run with NO per-user token stored. We deliberately do
            # NOT fall back to the GITLAB_TOKEN env (service) token here, even when
            # it is set: the later indexed-repo clone goes through
            # build_run_clone_url → get_gitlab_token, which is per-user-ONLY and has
            # no env fallback. Borrowing the service token now would let PREFLIGHT,
            # BASELINE, and branch-creation pass on the service token, then the run
            # would die at the CLASSIFYING clone with an opaque error. Fail fast here
            # instead, at the earliest gate, with the same actionable message the
            # clone path uses — so the asymmetry can never surface downstream.
            _add("GitLab token lookup", "FAIL",
                 f"No token in user_tokens for user_id={user_id!r} ({user_email}). "
                 "A user-triggered run clones the target (and any dependency) repos "
                 "with YOUR OWN GitLab PAT — the service token is not used for user "
                 "runs. Add your GitLab PAT under Profile → GitLab Token "
                 "(needs read_repository scope).")
            update_run_state(run_id, "FAILED",
                error=f"Pre-flight FAILED: no GitLab token for user {who}. "
                      "Add your PAT under Profile → GitLab Token "
                      "(needs read_repository scope on the target repo and any "
                      "dependency repos).")
            _emit_preflight_event(run_id, results)
            return False

    # Tokens may be stored as "username:glpat-xxxx" — extract only the token part
    if gl_token and ":" in gl_token:
        gl_token = gl_token.split(":", 1)[-1]

    # Set token in SCM tools thread-local — safe for concurrent workers
    if gl_token:
        if _SCM_PF == "github":
            from tools.github_tools import set_token as _scm_set_token_pf
        else:
            from tools.gitlab_tools import set_token as _scm_set_token_pf
        _scm_set_token_pf(gl_token)

    # ── 2. SCM repo connectivity ───────────────────────────────
    if repo and "/" in repo:
        try:
            import json as _json
            from urllib.parse import quote as _q
            if _SCM_PF == "github":
                _api_url = f"https://api.github.com/repos/{repo}"
                _req = urllib.request.Request(
                    _api_url,
                    headers={"Authorization": f"Bearer {gl_token}",
                             "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28"},
                )
                logger.info(f"TS- sdlc_pipeline:_preflight_check GitHub api url - {_api_url}")
                with urllib.request.urlopen(_req, timeout=8) as _r:
                    _data = _json.loads(_r.read().decode())
                _name = _data.get("full_name", repo)
                _vis  = "private" if _data.get("private") else "public"
                _def  = _data.get("default_branch", "main")
                _add("GitHub repo access", "PASS",
                     f"Reached {repo!r} ({_name}, {_vis}, default_branch={_def!r})")
            else:
                _proj_encoded = _q(repo, safe="")
                logger.info(f"Repo - {repo} and Project encoded - {_proj_encoded}")
                _api_url = f"{gl_url}/api/v4/projects/{_proj_encoded}"
                logger.info(f"TS- sdlc_pipeline:_preflight_check git api url - {_api_url}")
                _req = urllib.request.Request(
                    _api_url,
                    headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(_req, timeout=8) as _r:
                    _data = _json.loads(_r.read().decode())
                _name = _data.get("name_with_namespace", repo)
                _vis  = _data.get("visibility", "?")
                _def  = _data.get("default_branch", "main")
                _add("GitLab repo access", "PASS",
                     f"Reached {repo!r} ({_name}, {_vis}, default_branch={_def!r})")
        except urllib.error.HTTPError as _he:
            if _he.code == 404:
                _skip = _os.getenv("SDLC_SKIP_REPO_CHECK", "0").strip() == "1"
                _add(f"{_provider_label} repo access", "WARN" if _skip else "FAIL",
                     f"GET repo/{repo} → 404. Either the repo doesn't exist "
                     f"OR the token has no read scope for it. "
                     f"Token owner: {user_email or 'env var'}. "
                     f"Check repo path is exact (case-sensitive)."
                     + (" [SDLC_SKIP_REPO_CHECK=1 — continuing]" if _skip else ""))
                if not _skip:
                    update_run_state(run_id, "FAILED",
                        error=f"Pre-flight FAILED: {_provider_label} 404 on repo {repo!r}. "
                              "Check repo path (namespace/project) and token scope.")
                    _emit_preflight_event(run_id, results)
                    return False
            else:
                _add(f"{_provider_label} repo access", "WARN",
                     f"GET repo/{repo} → HTTP {_he.code} {_he.reason} (non-fatal, continuing)")
        except Exception as _ge:
            _add(f"{_provider_label} repo access", "WARN",
                 f"Could not verify repo {repo!r}: {_ge} (non-fatal, continuing)")
    else:
        _add(f"{_provider_label} repo access", "WARN",
             f"repo={repo!r} has no namespace/ — skipping live check. "
             "Will rely on indexed codebase for language detection.")

    # ── 3. JIRA token lookup ──────────────────────────────────
    # Resolution: user_tokens[user_id, atlassian] → env var service account fallback
    jira_email, jira_token = "", ""
    if not user_id:
        jira_email = _os.getenv("JIRA_EMAIL", "")
        jira_token = _os.getenv("JIRA_API_TOKEN", "")
        if jira_email and jira_token:
            _add("JIRA token lookup", "WARN",
                 f"No user context — using JIRA_EMAIL env var ({jira_email!r}). "
                 "Per-user token preferred.")
        else:
            _add("JIRA token lookup", "WARN",
                 "No user context and no JIRA_EMAIL/JIRA_API_TOKEN env vars. "
                 "JIRA comments/updates will be skipped.")
    else:
        try:
            from core.platform_credentials import get_atlassian_creds as _get_at
            jira_email, jira_token = _get_at(user_id=user_id)  # direct lookup by user_id
            _add("JIRA token lookup", "PASS",
                 f"Found Atlassian token for user_id={user_id!r} ({user_email}) in user_tokens")
        except PermissionError:
            jira_email = _os.getenv("JIRA_EMAIL", "")
            jira_token = _os.getenv("JIRA_API_TOKEN", "")
            if jira_email and jira_token:
                _add("JIRA token lookup", "WARN",
                     f"No Atlassian token in user_tokens for user_id={user_id!r} ({user_email}) — "
                     "using JIRA service account from env vars. "
                     "User should add their Atlassian token under Profile → Atlassian Token.")
            else:
                _add("JIRA token lookup", "WARN",
                     f"No JIRA token for user_id={user_id!r} and no env var fallback. "
                     "JIRA comments/updates will be skipped (pipeline continues).")

    # ── 4. JIRA ticket connectivity ───────────────────────────
    if jira_key and jira_email and jira_token:
        try:
            _proxy_url = _os.getenv("LLM_PROXY_URL", "").rstrip("/")
            if _proxy_url:
                # Production: route through LLM proxy server (same pattern as jira_tools.py)
                import httpx as _httpx
                _resp = _httpx.post(
                    f"{_proxy_url}/atlassian/proxy",
                    json={
                        "service": "jira",
                        "method":  "GET",
                        "path":    f"/rest/api/3/issue/{jira_key}",
                        "email":   jira_email,
                        "token":   jira_token,
                    },
                    timeout=10.0,
                )
                _resp.raise_for_status()
                _data = _resp.json()
            else:
                # Local dev: call Jira directly (only when LLM_PROXY_URL not set)
                import base64 as _b64
                _creds   = _b64.b64encode(f"{jira_email}:{jira_token}".encode()).decode()
                _jira_api = f"{jira_url}/rest/api/3/issue/{jira_key}"
                _req = urllib.request.Request(
                    _jira_api,
                    headers={"Authorization": f"Basic {_creds}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(_req, timeout=8) as _r:
                    _data = _json.loads(_r.read().decode())
            _summary = (_data.get("fields") or {}).get("summary", "?")[:60]
            _status  = ((_data.get("fields") or {}).get("status") or {}).get("name", "?")
            _add("JIRA ticket access", "PASS",
                 f"Read {jira_key!r}: status={_status!r} summary={_summary!r}")
        except urllib.error.HTTPError as _he:
            _add("JIRA ticket access", "WARN",
                 f"GET /issue/{jira_key} → HTTP {_he.code}. "
                 "JIRA writes will be skipped. Check token scope (read:jira-work).")
        except Exception as _je:
            _add("JIRA ticket access", "WARN",
                 f"Could not verify JIRA ticket {jira_key!r}: {_je}")
    else:
        _add("JIRA ticket access", "WARN",
             "Skipped — missing JIRA_URL, token, or jira_key. "
             "Set JIRA_URL in .env and add Atlassian token in Profile.")

    # ── 5. HOD budget check ───────────────────────────────────────────────────
    # Hard failure when enforcement is on and the HOD's monthly cap is exhausted,
    # or when department / HOD mapping is missing (every AiNxt dept must have an HOD).
    # On pass: hod_email is written to sdlc_runs.hod_email for run-end deduction.
    try:
        from services.sdlc_budget_tracker import check_hod_budget as _chk_hod
        _hod_ok, _hod_err = _chk_hod(
            user_id=user_id, run_id=run_id, user_email=user_email
        )
        if not _hod_ok:
            _add("HOD budget check", "FAIL", _hod_err)
            update_run_state(run_id, "FAILED", error=_hod_err)
            _emit_preflight_event(run_id, results)
            return False
        _add("HOD budget check", "PASS", "HOD budget available for this department")
    except Exception as _hod_exc:
        _add("HOD budget check", "WARN",
             f"HOD budget check error (non-blocking): {_hod_exc}")

    # ── 6. Multi-repo dependency resolution (added 2026-05-19) ──
    # No-op unless ENABLE_MULTI_REPO_SDLC=true. When enabled, resolves dep
    # repos via dep_resolver (user > manifest > build-file), validates each
    # against GitLab, gates on indexing for editables, and writes one row per
    # repo (primary + deps) into sdlc_run_repos.
    primary_ref = issue.get("base_branch") or issue.get("branch") or "main"
    if not _preflight_multi_repo(
        issue, run_id, repo, primary_ref, gl_url, gl_token, _add,
    ):
        _emit_preflight_event(run_id, results)
        return False

    # ── 7. Baseline build gate (WS-2, behind SDLC_ENABLE_BASELINE_GATE) ───────
    # Build HEAD as-is BEFORE CLASSIFYING. A repo broken at HEAD suspends here at
    # BASELINE_BUILD (not FAILED) with a user/agent-fix choice; a green/cached
    # baseline advances. No-op when the flag is off.
    if not _baseline_gate_preflight(issue, run_id, repo, primary_ref, gl_url, gl_token, _add):
        _emit_preflight_event(run_id, results)
        return False

    # ── Summary log ───────────────────────────────────────────
    passed = sum(1 for r in results if r["status"] == "PASS")
    warned = sum(1 for r in results if r["status"] == "WARN")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    logger.info(
        f"[PREFLIGHT] Summary for run={run_id} jira={jira_key} repo={repo!r} "
        f"user={who} (user_id={user_id!r}): "
        f"PASS={passed} WARN={warned} FAIL={failed}"
    )
    _emit_preflight_event(run_id, results)
    return failed == 0


def _emit_preflight_event(run_id: str, results: list) -> None:
    """Write preflight results to the run event log."""
    try:
        from store.sdlc_store import add_run_event
        run_id_entry = {"check": "SDLC Run ID", "status": "INFO", "detail": run_id, "icon": "✅"}
        all_results = [run_id_entry] + results
        summary_lines = [f"{r['icon']} {r['check']}: {r['detail']}" for r in all_results]
        add_run_event(
            run_id,
            from_state="PREFLIGHT",
            to_state="PREFLIGHT",
            stage="preflight-checker",
            actor="preflight-checker",
            output="\n".join(summary_lines),
            data={"checks": all_results},
        )
    except Exception:
        pass


# ── WS-2: baseline build gate helpers (behind SDLC_ENABLE_BASELINE_GATE) ──────
# See docs/planning/SDLC_AGENTIC_LOOP_RFD.md §4 WS-2 / §5.3 and agents/sdlc_baseline_gate.py.
# Orchestration is in agents/sdlc_baseline_gate.run_baseline_gate (pure, unit-tested);
# the real build closure below only runs server-side (Docker/Maven/Nexus) when the
# flag is ON. With the flag OFF the gate returns "skipped" before build_fn is ever called.

def _resolve_head_sha(repo: str, ref: str, gl_url: str, gl_token: str) -> str:
    """Resolve a branch/ref to its HEAD commit SHA via the GitLab commits API.
    Returns '' on any failure (the gate then builds without a cache key)."""
    if not (repo and "/" in repo and gl_token):
        return ""
    try:
        import json as _json
        import urllib.request
        from urllib.parse import quote as _q
        _proj = _q(repo, safe="")
        _req = urllib.request.Request(
            f"{gl_url}/api/v4/projects/{_proj}/repository/commits/{_q(ref, safe='')}",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(_req, timeout=8) as _r:
            commit = _json.loads(_r.read().decode())
        return str(commit.get("id", "")).strip()
    except Exception as _e:
        logger.warning(f"[baseline-gate] HEAD SHA resolve failed for {repo}@{ref}: {_e}")
        return ""


def _run_baseline_build(run_id: str, repo: str, ref: str, gl_url: str, gl_token: str) -> dict:
    """Build the repo at HEAD as-is (server-side only). Returns the gate's
    build_fn contract: {success, transient, errors, output}.

    Reuses the existing build machinery: BuildManifestResolver → prepare_run_workspace
    (token-embedded clone) → WorkspaceBuilder.compile. Any wiring/infra failure is
    reported as a TRANSIENT failure so a flake retries rather than hard-suspending.
    NOTE: this path is exercised on Ubuntu only — see the §11.4 hand-off list.
    """
    try:
        from core.build_manifest_resolver import BuildManifestResolver, _canonical_slug
        from sandbox.workspace_builder import WorkspaceBuilder
        from workers.workspace_sync_worker import prepare_run_workspace

        # Clone FIRST, then resolve the build manifest from the fresh checkout.
        # Reading the real build files off the clone needs NO prior indexing, so a
        # never-indexed repo establishes a baseline just fine.
        # Token-embedded clone URL (standard GitLab CI pattern). gl_url is e.g.
        # https://<YOUR_GITLAB_URL> → https://oauth2:<token>@<YOUR_GITLAB_URL>/<repo>.git
        from urllib.parse import urlsplit, urlunsplit
        _sp = urlsplit(gl_url)
        _netloc = f"oauth2:{gl_token}@{_sp.netloc}" if gl_token else _sp.netloc
        clone_url = urlunsplit((_sp.scheme or "https", _netloc, f"/{repo}.git", "", ""))

        workspace = prepare_run_workspace(run_id, _canonical_slug(repo), clone_url, ref or "main")

        _pid = ""
        try:
            _pid = ((get_run(run_id) or {}).get("context") or {}).get("product_id", "") or ""
        except Exception:
            _pid = ""
        manifest = BuildManifestResolver().resolve(
            repo, gitlab_path=repo, workspace_path=workspace, product_id=_pid,
        )
        if not manifest or getattr(manifest, "status", "") == "UNKNOWN_BUILD_PATTERN":
            # No detectable build → cannot establish a baseline; treat as transient
            # so it surfaces as a clear suspend after retries rather than a crash.
            return {"success": False, "transient": True,
                    "errors": ["baseline build: no build manifest could be resolved"],
                    "output": ""}

        # ── Multi-repo: build internal deps BEFORE compiling the primary ──────
        # Mirror what PLAN (_setup_multi_repo_workspace_for_plan, this module) and
        # IMPLEMENT (CodingStateMachine._setup_multi_repo_workspace,
        # sdlc_state_machine.py) each do: clone the dependent repos resolved at
        # preflight step 6, mvn-install the compile-only ones into a per-run
        # _m2_cache, and point the primary's compile at that cache. Without this
        # the baseline build can't see internal org.ainxt.* jars that aren't
        # published to Nexus and suspends a repo that actually builds fine. The
        # jar cache makes the later PLAN/IMPLEMENT rebuild a cache hit, so this
        # prep is paid once per (dep, sha).
        _m2_override = None
        try:
            from store.sdlc_store import list_run_repos
            _dep_rows = list_run_repos(run_id) or []
        except Exception:
            _dep_rows = []
        if any(r.get("kind") != "primary" for r in _dep_rows):
            from agents.multi_repo_workspace import prepare_and_install_deps
            from urllib.parse import urlsplit as _us, urlunsplit as _uus

            def _dep_clone_url(gitlab_path: str) -> str:
                _p = _us(gl_url)
                _nl = f"oauth2:{gl_token}@{_p.netloc}" if gl_token else _p.netloc
                return _uus((_p.scheme or "https", _nl, f"/{gitlab_path}.git", "", ""))

            try:
                _mr_ws = prepare_and_install_deps(run_id, workspace, _dep_rows, _dep_clone_url)
                if _mr_ws and getattr(_mr_ws, "m2_cache", None) and os.path.isdir(_mr_ws.m2_cache):
                    _m2_override = _mr_ws.m2_cache
                    logger.info(
                        f"[baseline-gate {run_id}] multi-repo deps built; "
                        f"compiling primary against {_m2_override!r}"
                    )
            except Exception as _mr_e:
                # A genuine dep-build failure IS a baseline breakage — surface it
                # (non-transient) so the gate suspends with an actionable reason
                # rather than burning retries on a build that won't go green.
                logger.error(f"[baseline-gate {run_id}] multi-repo dep prep failed: {_mr_e}")
                return {"success": False, "transient": False,
                        "errors": [f"baseline multi-repo dep build failed: {_mr_e}"],
                        "output": str(_mr_e)}

        result = WorkspaceBuilder().compile(
            manifest, sdlc_run_id=run_id, workspace_path=workspace,
            m2_cache_override=_m2_override,
        )
        status = getattr(result, "status", "UNKNOWN_ERROR")
        success = status == "BUILD_SUCCESS"
        transient = status in ("INFRA_FAILURE", "BUILD_TIMEOUT", "UNKNOWN_ERROR")

        # Merge the public deps this build downloaded back into the shared m2
        # cache (green only; internal artifacts excluded by the merge). The
        # baseline gate is the FIRST build of a run, so it is where the largest
        # set of newly resolved third-party artifacts appears.
        if success and _m2_override:
            try:
                from agents.multi_repo_workspace import merge_m2_cache_to_shared
                merge_m2_cache_to_shared(_m2_override, label=run_id)
            except Exception as _wb:
                logger.warning(f"[baseline-gate {run_id}] shared m2 write-back failed: {_wb}")

        return {
            "success": success,
            "transient": transient,
            "errors": list(getattr(result, "error_lines", []) or [])[:10],
            "output": getattr(result, "output_tail", "") or "",
            "_build_status": status,
        }
    except Exception as e:
        logger.error(f"[baseline-gate {run_id}] build machinery error: {e}", exc_info=True)
        return {"success": False, "transient": True, "errors": [str(e)], "output": str(e)}


def _baseline_gate_preflight(issue: dict, run_id: str, repo: str, ref: str,
                             gl_url: str, gl_token: str, _add) -> bool:
    """Run the WS-2 baseline build gate as the final preflight step. Returns True
    to proceed (green / cache-hit / flag-off), False to STOP (suspended).

    On suspend the run state is set to SUSPENDED at BASELINE_BUILD (NOT FAILED) so
    the user/agent-fix HITL choice (UI §6) can resume it."""
    from agents.sdlc_baseline_gate import run_baseline_gate, baseline_gate_enabled

    # Compilation skipped for this run (operator chose "Skip compilation & continue"
    # at a prior BASELINE_BUILD suspend). Don't build HEAD — just proceed; every
    # downstream compile point honours compile_skipped too.
    _bctx = (get_run(run_id) or {}).get("context", {}) or {}
    if _bctx.get("compile_skipped"):
        _add("Baseline build", "WARN",
             "Compilation skipped on user request — baseline build bypassed")
        try:
            add_run_event(
                run_id, from_state="PREFLIGHT", to_state="PREFLIGHT",
                stage="BASELINE_BUILD", actor="baseline-gate",
                output="Baseline build SKIPPED — compile_skipped set on run",
                data={"compile_skipped": True},
            )
        except Exception:
            pass
        return True

    if not baseline_gate_enabled():
        return True  # flag off → no-op, behave exactly as before WS-2

    head_sha = _resolve_head_sha(repo, ref, gl_url, gl_token)

    # Redis cache on db=0 (same db the pipeline already uses for its caches).
    _redis = None
    try:
        import redis as _redis_lib
        _redis = _redis_lib.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=0,
                                  decode_responses=True, socket_connect_timeout=2)
    except Exception as _re:
        logger.warning(f"[baseline-gate {run_id}] redis unavailable (cache disabled): {_re}")

    def _suspend(reason: str) -> None:
        update_run_state(
            run_id, "SUSPENDED",
            current_stage="BASELINE_BUILD",
            suspended_at_stage="BASELINE_BUILD",
            context_patch={"suspended_at_stage": "BASELINE_BUILD",
                           "suspend_reason": reason,
                           "baseline_build": {"status": "broken", "sha": head_sha}},
        )

    def _event(actor: str, msg: str, data: dict) -> None:
        try:
            run = get_run(run_id)
            add_run_event(
                run_id,
                from_state=(run["state"] if run else "PREFLIGHT"),
                to_state="BASELINE_BUILD",
                stage="BASELINE_BUILD",
                actor=str(actor),
                output=str(msg),
                data=data or {},
            )
        except Exception:
            pass

    def _patch_ctx(patch: dict) -> None:
        try:
            patch_run_context(run_id, patch)
        except Exception:
            pass

    result = run_baseline_gate(
        run_id=run_id,
        repo=repo,
        head_sha=head_sha,
        build_fn=lambda: _run_baseline_build(run_id, repo, ref, gl_url, gl_token),
        redis_client=_redis,
        suspend_fn=_suspend,
        event_fn=_event,
        context_patch_fn=_patch_ctx,
    )

    status = result.get("status")
    if status == "green":
        _add("Baseline build", "PASS",
             f"Repo builds at HEAD ({'cache hit' if result.get('from_cache') else 'compiled'}) "
             f"sha={(result.get('sha') or '')[:12]}")
        return True
    if status == "suspended":
        _add("Baseline build", "FAIL", f"SUSPENDED at BASELINE_BUILD — {result.get('reason')}")
        return False
    return True  # "skipped" (flag flipped off mid-call) → proceed


# ── Multi-repo preflight helpers (added 2026-05-19, behind ENABLE_MULTI_REPO_SDLC) ──

def _is_multi_repo_enabled() -> bool:
    """
    Kill switch for multi-repo SDLC behavior. Default off until all phases ship.

    Read at call time (not module load) so the flag can be toggled in prod
    without restarting workers.
    """
    import os as _os
    return _os.getenv("ENABLE_MULTI_REPO_SDLC", "").strip().lower() in ("1", "true", "yes", "on")


def _setup_multi_repo_workspace_for_plan(run_id: str, workspace_root: str) -> object | None:
    """
    PLAN-side twin of `CodingStateMachine._setup_multi_repo_workspace`
    (agents/sdlc_state_machine.py). PLAN runs as a module-level function before
    any CodingStateMachine instance exists, so it cannot call that method
    directly — this stages the same dependency-repo checkouts into the primary
    workspace using the same underlying `prepare_and_install_deps` call, so PLAN
    can read the deps for grounding and the later IMPLEMENT staging pass is a
    cheap SHA-match no-op instead of a fresh clone.

    No-op (returns None immediately) when multi-repo is disabled, the run has
    no non-primary `sdlc_run_repos` rows, or `workspace_root` is empty.

    PLAN is read-only: a staging failure here is logged as an ERROR and this
    returns None so PLAN continues without deps rather than hard-failing the
    plan phase. NOTE: IMPLEMENT will hit the same failure and SUSPEND there
    (CodingStateMachine._setup_multi_repo_workspace) — this function never
    suspends the run itself.
    """
    if not _is_multi_repo_enabled():
        return None

    from store.sdlc_store import list_run_repos
    rows = list_run_repos(run_id) or []
    if not rows or all(r.get("kind") == "primary" for r in rows):
        return None

    if not workspace_root:
        return None

    try:
        from urllib.parse import urlsplit, urlunsplit
        from agents.multi_repo_workspace import prepare_and_install_deps

        run = get_run(run_id) or {}
        ctx = run.get("context") or {}
        user_id = ctx.get("user_id") or run.get("triggered_by", "")
        user_email = ctx.get("user_email") or ""

        gl_url = os.getenv("GITLAB_URL", "https://gitlab.com")
        # Same per-user-token-then-env-fallback resolution as everywhere else in
        # this module (e.g. _preflight_check); dep repos don't necessarily have a
        # repo_index_status row, so we build the clone URL directly rather than
        # via core.platform_credentials.build_run_clone_url (see the parallel
        # NOTE in CodingStateMachine._setup_multi_repo_workspace).
        # Resolve user_id → email → GITLAB_TOKEN, exactly like build_run_clone_url
        # does for the primary clone. Trying user_id only (the old behaviour) left a
        # user whose token is keyed by email authenticated for the primary but with
        # NO token for the dep clone → "unauthorized" on the dep repo.
        gl_token = ""
        if user_id or user_email:
            try:
                from core.platform_credentials import get_gitlab_token as _get_gl
                gl_token = _get_gl(user_id=user_id or "", email=user_email or "")
            except PermissionError:
                gl_token = ""
        if not gl_token:
            gl_token = os.getenv("GITLAB_TOKEN", "")

        def _dep_clone_url(gitlab_path: str) -> str:
            _sp = urlsplit(gl_url)
            _netloc = f"oauth2:{gl_token}@{_sp.netloc}" if gl_token else _sp.netloc
            return urlunsplit((_sp.scheme or "https", _netloc, f"/{gitlab_path}.git", "", ""))

        # When the operator chose "Skip compilation & continue" at a BASELINE_BUILD
        # suspend (compile_skipped set by retrigger_pipeline), clone every dep so the
        # CLI can still SEE them under .sdlc_deps/, but DO NOT re-run the compile-only
        # `mvn install` — that install is exactly the dependent-repo build the user
        # opted out of. Without this, PLAN re-attempts the failing dep build, the
        # exception below drops ALL deps, and PLAN silently proceeds against only the
        # primary repo (the ".sdlc_deps vanished" symptom).
        _compile_skipped = bool(ctx.get("compile_skipped", False))
        ws = prepare_and_install_deps(
            run_id, workspace_root, rows, _dep_clone_url,
            skip_install=_compile_skipped,
        )
        _dep_count = sum(1 for r in rows if r.get("kind") != "primary")
        logger.info(
            "[SDLC PLAN] multi-repo dep staging done", run_id=run_id, dep_count=_dep_count,
        )
        return ws
    except Exception as e:
        logger.error(
            f"[SDLC {run_id}] PLAN multi-repo dep staging failed (non-fatal — "
            f"PLAN continues without deps; IMPLEMENT will suspend on the same failure): {e}"
        )
        return None


def _validate_repo_connectivity(repo: str, ref: str, gl_url: str, gl_token: str) -> tuple[bool, str, str]:
    """
    Validate a single GitLab repo and resolve `ref` to a commit SHA.

    Returns (ok, error_message, commit_sha). On success error_message is empty.
    On failure commit_sha is empty. Used by _preflight_check for both the
    primary repo (refactored from the inline check) and each dep repo.

    Two API calls per repo:
      1. GET /projects/{repo}             — confirms existence + token access
      2. GET /repository/commits/{ref}    — pins the ref to a commit SHA so
                                             later steps don't see branch drift
    """
    import json as _json
    import urllib.request, urllib.error
    from urllib.parse import quote as _q

    if not repo or "/" not in repo:
        return False, f"repo {repo!r} has no namespace/ — must be 'group/project'", ""

    proj = _q(repo, safe="")
    try:
        _req = urllib.request.Request(
            f"{gl_url}/api/v4/projects/{proj}",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(_req, timeout=8) as _r:
            _ = _json.loads(_r.read().decode())
    except urllib.error.HTTPError as he:
        if he.code == 404:
            return False, (
                f"GET /projects/{repo} → 404. Either the repo doesn't exist at "
                f"{gl_url!r} or the token has no read_repository scope for it."
            ), ""
        return False, f"GET /projects/{repo} → HTTP {he.code} {he.reason}", ""
    except Exception as exc:
        return False, f"GET /projects/{repo}: {exc}", ""

    # Resolve ref → commit SHA. We pin both branches and tags so subsequent
    # steps (workspace clone, build) see a stable revision even if main moves.
    if not ref:
        ref = "main"
    try:
        _req = urllib.request.Request(
            f"{gl_url}/api/v4/projects/{proj}/repository/commits/{_q(ref, safe='')}",
            headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(_req, timeout=8) as _r:
            commit = _json.loads(_r.read().decode())
        sha = str(commit.get("id", "")).strip()
        if not sha:
            return False, f"GET /repository/commits/{ref} returned no id", ""
        return True, "", sha
    except urllib.error.HTTPError as he:
        if he.code == 404:
            return False, (
                f"GET /repository/commits/{ref} on {repo} → 404. "
                f"Branch or tag {ref!r} does not exist."
            ), ""
        return False, f"GET /repository/commits/{ref} on {repo} → HTTP {he.code} {he.reason}", ""
    except Exception as exc:
        return False, f"GET /repository/commits/{ref} on {repo}: {exc}", ""


def _preflight_multi_repo(
    issue: dict,
    run_id: str,
    primary_repo: str,
    primary_ref: str,
    gl_url: str,
    gl_token: str,
    add_result,
) -> bool:
    """
    Step 5 of preflight (added for multi-repo). No-op when flag is off.

    Resolves dependent repos via dep_resolver (user > manifest > build-file),
    validates each against GitLab, and writes one row per repo (primary + deps)
    into `sdlc_run_repos`. (Editable deps are no longer required to be indexed —
    the pipeline greps the live checkout, not the embeddings index.)

    Returns False on hard failure (caller marks the run FAILED). Returns True
    when the flag is off, when no deps are declared, OR when all checks pass.
    """
    if not _is_multi_repo_enabled():
        return True

    try:
        from agents.dep_resolver import resolve_dependencies
        from store.sdlc_store import upsert_run_repo
    except Exception as exc:
        # If imports fail, the multi-repo path is unusable but we must not
        # break single-repo runs — surface a warning and return True.
        add_result(
            "Multi-repo init",
            "WARN",
            f"Multi-repo enabled but imports failed ({exc}); falling back to single-repo behavior.",
        )
        return True

    user_overrides = issue.get("dependencies") or []
    if not isinstance(user_overrides, list):
        add_result(
            "Multi-repo input",
            "FAIL",
            f"issue['dependencies'] must be a list, got {type(user_overrides).__name__}",
        )
        update_run_state(
            run_id, "FAILED",
            error="Pre-flight FAILED: issue.dependencies is not a list.",
        )
        return False

    try:
        dep_specs = resolve_dependencies(primary_repo, primary_ref, user_overrides)
    except Exception as exc:
        add_result("Multi-repo resolve", "FAIL", f"dep_resolver raised: {exc}")
        update_run_state(
            run_id, "FAILED",
            error=f"Pre-flight FAILED: dep_resolver error: {exc}",
        )
        return False

    if not dep_specs and not user_overrides:
        # Common case: no deps declared. Still record the primary as a
        # sdlc_run_repos row so downstream code has a uniform view.
        _, _, primary_sha = _validate_repo_connectivity(primary_repo, primary_ref, gl_url, gl_token)
        upsert_run_repo(
            run_id=run_id,
            repo=primary_repo,
            ref=primary_ref or "main",
            kind="primary",
            source="primary",
            ref_sha=primary_sha or None,
            state="READY",
        )
        add_result(
            "Multi-repo deps",
            "PASS",
            f"No dependent repos declared for {primary_repo!r}; recorded primary only.",
        )
        return True

    add_result(
        "Multi-repo deps",
        "PASS",
        f"Resolved {len(dep_specs)} dependent repo(s) for {primary_repo!r}: "
        + ", ".join(f"{s.repo}@{s.ref}({s.kind},{s.source})" for s in dep_specs),
    )

    # ── 5a. Validate each dep against GitLab, pin commit SHAs ──
    dep_shas: dict[str, str] = {}
    for spec in dep_specs:
        ok, err, sha = _validate_repo_connectivity(spec.repo, spec.ref, gl_url, gl_token)
        if not ok:
            add_result(f"Dep repo {spec.repo}", "FAIL", err)
            update_run_state(
                run_id, "FAILED",
                error=f"Pre-flight FAILED on dependent repo {spec.repo!r}: {err}",
            )
            return False
        dep_shas[spec.repo] = sha
        add_result(
            f"Dep repo {spec.repo}",
            "PASS",
            f"ref={spec.ref!r} → sha={sha[:10]} ({spec.kind}, source={spec.source})",
        )

    # ── 5b. (removed) Editable-dep indexing gate ──
    # Previously editable deps were required to have `document_embeddings` rows
    # or the run was FAILED here. That gate is gone: the CLI IMPLEMENT phase works
    # against the freshly-cloned workspace and does NOT consult the pgvector/
    # document_embeddings index (it is a different branch and routinely stale).
    # Indexing was therefore never actually used as retrieval context during
    # PLAN/IMPLEMENT/REVIEW, so requiring it only broke editable deps that a
    # compile-only run accepts fine.

    # ── 5c. Pin primary SHA too, then write sdlc_run_repos rows ──
    _, _, primary_sha = _validate_repo_connectivity(primary_repo, primary_ref, gl_url, gl_token)
    upsert_run_repo(
        run_id=run_id,
        repo=primary_repo,
        ref=primary_ref or "main",
        kind="primary",
        source="primary",
        ref_sha=primary_sha or None,
        state="READY",
    )
    for spec in dep_specs:
        upsert_run_repo(
            run_id=run_id,
            repo=spec.repo,
            ref=spec.ref,
            kind=spec.kind,
            source=spec.source,
            ref_sha=dep_shas.get(spec.repo) or None,
            build_order=spec.build_order,
            state="READY",
        )
    add_result(
        "Multi-repo write",
        "PASS",
        f"Wrote 1 primary + {len(dep_specs)} dep row(s) to sdlc_run_repos.",
    )
    return True


def _create_working_branch(issue: dict, run_id: str) -> bool:
    """
    Create the working branch in GitLab early in the pipeline (right after preflight).
    Uses issue["working_branch"] (from_branch=issue["base_branch"]).
    Returns True on success, False on failure (run already marked FAILED).
    No-op when working_branch or base_branch are not in issue dict.
    """
    working_branch = issue.get("working_branch", "")
    base_branch    = issue.get("base_branch", "")
    repo           = issue.get("repo", "")
    if not working_branch or not base_branch or not repo:
        return True  # nothing to do — COMMITTING phase creates branch with old logic
    gitlab_repo = _resolve_gitlab_repo(repo)
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_create_branch as _create_branch
        else:
            from tools.gitlab_tools import gitlab_create_branch as _create_branch
        result = _create_branch(gitlab_repo, working_branch, from_branch=base_branch)
        if result.startswith("[Error"):
            logger.error(
                f"[SDLC] {run_id}: working branch creation FAILED: {result}"
            )
            update_run_state(run_id, "FAILED",
                error=f"Could not create working branch '{working_branch}' "
                      f"from '{base_branch}' in {repo}: {result}")
            return False
        logger.info(f"[SDLC] {run_id}: working branch ready: {repo}/{working_branch} (from {base_branch})")
        return True
    except Exception as e:
        logger.error(f"[SDLC] {run_id}: working branch creation exception: {e}")
        update_run_state(run_id, "FAILED",
            error=f"Working branch creation error for '{working_branch}': {e}")
        return False


# ============================================================
# EARLY WORKSPACE MATERIALIZATION
# ============================================================

def _materialize_early_workspace(
    run_id: str,
    repo_slug: str,
    working_branch: str,
    base_branch: str,
    user_id: str = "",
    user_email: str = "",
) -> str:
    """
    Materialize a local checkout of the repo BEFORE analysis stages begin.

    Mirrors the state machine's _ensure_run_workspace() logic but runs as a
    standalone pipeline-level helper (no SDLCStateMachine instance required).
    Called right after repo_ctx/language detection so ANALYZING, DESIGNING,
    DIAGNOSING, and MANIFEST_VALIDATION all have a real checkout to read from.

    Returns the absolute workspace path on success, or "" on any failure
    (GitLab API fallback preserved — never hard-fails analysis on a clone miss).

    Honors SDLC_REUSE_RUN_WORKSPACE + base_sha for byte-identical reuse across
    HITL-gate resumes (same guarantee as the state machine's clone path).
    """
    if not repo_slug:
        logger.warning(
            f"[SDLC {run_id}] early-checkout: repo_slug empty, skipping clone",
            run_id=run_id,
        )
        return ""

    try:
        from db.database import engine as _eng
        from sqlalchemy import text as _txt
        from workers.workspace_sync_worker import prepare_run_workspace as _prw
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik

        # Resolve repo_index_status the same way the state machine does:
        # try the normalized slug first, then the raw value for backward-compat.
        _canon_slug = _nrik(repo_slug)
        _row = None
        for _slug in (_canon_slug, repo_slug):
            if not _slug:
                continue
            with _eng.connect() as _c:
                _cand = _c.execute(
                    _txt("SELECT git_url, branch FROM repo_index_status WHERE repo_name=:slug"),
                    {"slug": _slug},
                ).fetchone()
            if _cand and _cand.git_url:
                _row = _cand
                break

        # Indexing is NOT required. Prefer the repo_index_status row when present
        # (honors the dev GitLab mock's file:// URL and the registered origin);
        # otherwise build the clone URL from GITLAB_URL below.
        branch = working_branch or base_branch or (_row.branch if _row else None) or "main"

        # SDLC_REUSE_RUN_WORKSPACE: pin this run to one base commit so a reused
        # checkout and a fresh clone on another host are byte-identical.
        _reuse = os.getenv("SDLC_REUSE_RUN_WORKSPACE", "false").strip().lower() in (
            "1", "true", "yes", "on"
        )
        _pin = ""
        if _reuse:
            try:
                with _eng.connect() as _c:
                    _sha_row = _c.execute(
                        _txt("SELECT base_sha FROM sdlc_runs WHERE id=:id"),
                        {"id": run_id},
                    ).fetchone()
                _pin = (_sha_row.base_sha or "") if _sha_row else ""
            except Exception as _sha_err:
                logger.debug(f"[SDLC {run_id}] early-checkout: base_sha read failed: {_sha_err}")

        # 2026-07-07 workspace-path consistency: build the run workspace under the
        # CANONICAL slug (the same normalizer the state machine's _ensure_run_workspace
        # uses) so PLAN/analysis and IMPLEMENT land on the SAME directory. Previously
        # this passed the RAW repo_slug (e.g. "ainxt/ainxt-platform", keeping the "/"
        # as a subdir) while IMPLEMENT used "ainxt_platform" — two different clones for
        # one run. _canon_slug is already resolved above for the repo_index_status lookup.
        # Clone as the user who TRIGGERED this run — never with the indexer's token
        # baked into repo_index_status.git_url. Strip any embedded credentials and
        # re-inject this user's own PAT (mirrors the state machine's clone path).
        from core.platform_credentials import build_run_clone_url as _build_clone_url
        if _row and _row.git_url:
            _clone_url = _build_clone_url(_row.git_url, user_id=user_id, email=user_email)
        else:
            # Repo never indexed → build the clone URL from GITLAB_URL directly.
            _gl_url = os.getenv("GITLAB_URL", "https://gitlab.example.com")
            _gl_token = ""
            if user_id or user_email:
                try:
                    from core.platform_credentials import get_gitlab_token as _get_gl
                    _gl_token = _get_gl(user_id=user_id or "", email=user_email or "")
                except PermissionError:
                    _gl_token = ""
            if not _gl_token:
                _gl_token = os.getenv("GITLAB_TOKEN", "")
            _clone_url = _authenticated_clone_url(repo_slug, _gl_url, _gl_token)
            logger.info(
                f"[SDLC {run_id}] early-checkout: '{repo_slug}' not in repo_index_status "
                f"— cloning from GITLAB_URL (indexing not required)",
                run_id=run_id, repo=repo_slug,
            )
        workspace_path = _prw(
            run_id, _canon_slug or repo_slug, _clone_url, branch,
            pin_sha=_pin, reuse=bool(_reuse and _pin),
        )

        if _reuse and not _pin and workspace_path:
            # First materialization: capture and persist the exact SHA cloned
            # so every later stage/instance restores byte-identical code.
            try:
                from workers.workspace_sync_worker import _git_head as _gh
                _captured = _gh(workspace_path)
                if _captured:
                    with _eng.connect() as _c:
                        _c.execute(
                            _txt(
                                "UPDATE sdlc_runs SET base_sha=:s "
                                "WHERE id=:id AND (base_sha IS NULL OR base_sha='')"
                            ),
                            {"s": _captured, "id": run_id},
                        )
                        _c.commit()
                    logger.info(
                        f"[SDLC {run_id}] early-checkout: pinned base_sha={_captured[:8]}"
                    )
            except Exception as _pin_err:
                logger.debug(
                    f"[SDLC {run_id}] early-checkout: base_sha capture failed (non-fatal): {_pin_err}"
                )

        reused = bool(_reuse and _pin)
        logger.info(
            f"[SDLC {run_id}] early-checkout: workspace materialized for analysis",
            run_id=run_id,
            workspace_root=workspace_path,
            reused=reused,
            base_sha=_pin[:8] if _pin else "",
        )
        patch_run_context(run_id, {"workspace_root": workspace_path})
        return workspace_path

    except Exception as _e:
        # Distinguish a genuinely-missing user token from a transient clone miss.
        # The indexed-repo path clones via build_run_clone_url → get_gitlab_token,
        # which raises PermissionError with NO env fallback when the triggering
        # user has no stored PAT (per-user-only is intentional for user runs).
        # Silently returning "" here is exactly what let CLASSIFYING spawn the CLI
        # with cwd='' and die on "[Errno 2] No such file or directory: ''".
        # Fail fast instead: suspend with a clear, actionable message.
        _msg = str(_e)
        _is_missing_token = isinstance(_e, PermissionError) or (
            "No GitLab personal access token found" in _msg
        )
        if _is_missing_token and (user_id or user_email):
            _who = user_email or user_id
            reason = (
                "No GitLab token for user — add your PAT under Profile → GitLab Token "
                "(needs read_repository scope on the target repo and any dependency repos)."
            )
            logger.error(
                f"[SDLC {run_id}] early-checkout: SUSPENDING — {reason} "
                f"(user={_who}, repo={repo_slug})",
                run_id=run_id,
                repo=repo_slug,
                error=_msg,
            )
            update_run_state(
                run_id, "SUSPENDED",
                current_stage="CLASSIFYING",
                suspended_at_stage="CLASSIFYING",
                context_patch={
                    "suspended_at_stage": "CLASSIFYING",
                    "suspend_reason": reason,
                },
                error=reason,
            )
            # Raise a SDLCCancelled subclass: every existing pipeline
            # `except SDLCCancelled` stops cleanly WITHOUT overwriting the
            # SUSPENDED state/message we just set.
            raise SDLCUserTokenMissing(run_id, reason)

        # Workspace-CLEANUP failure — NOT a clone miss and NOT a token problem.
        # _force_remove_dir raises RuntimeError when it cannot remove root-owned
        # build residue (e.g. left by a timed-out builder container, or when the
        # privileged cleanup container's image is missing on an air-gapped host).
        # Returning "" here would let CLASSIFYING run with an empty workspace and
        # then blame a "missing GitLab token", masking the true cause. Suspend
        # with the real error so the residue/image issue is what gets surfaced.
        _is_cleanup_failure = isinstance(_e, RuntimeError) and (
            "could not clean leftover workspace" in _msg
        )
        if _is_cleanup_failure:
            reason = (
                "Workspace could not be prepared — leftover root-owned build artifacts "
                "could not be removed (likely from a timed-out builder container, or a "
                "missing/uncached cleanup image on an air-gapped host). Clean the run "
                "workspace manually, ensure WORKSPACE_CLEANUP_IMAGE points at a cached "
                "image, and retry."
            )
            logger.error(
                f"[SDLC {run_id}] early-checkout: SUSPENDING — {reason} ({_e})",
                run_id=run_id,
                repo=repo_slug,
                error=_msg,
            )
            update_run_state(
                run_id, "SUSPENDED",
                current_stage="CLASSIFYING",
                suspended_at_stage="CLASSIFYING",
                context_patch={
                    "suspended_at_stage": "CLASSIFYING",
                    "suspend_reason": reason,
                },
                error=reason,
            )
            # Run is already SUSPENDED with an actionable message above; raise the
            # SDLCCancelled sentinel so every `except SDLCCancelled` handler stops
            # the pipeline cleanly WITHOUT flipping the run to FAILED / clobbering
            # that message (same contract as the missing-token path above).
            raise SDLCCancelled(run_id)

        # Transient / non-auth clone miss: preserve the documented GitLab-API
        # fallback (never hard-fail analysis on a flaky clone).
        logger.warning(
            f"[SDLC {run_id}] early-checkout: clone failed, continuing with GitLab fallback — {_e}",
            run_id=run_id,
            repo=repo_slug,
            error=_msg,
        )
        return ""


# ============================================================
# WS-0 / WS-1 / WS-2 / WS-5 — RE-ENTRANT PRE-SM DRIVER (2026-07-02)
#
# Gate reorder: GATE 1 (WorkItem review/approve) ALWAYS fires right after
# NORMALIZE; GATE 2 (open questions) fires ONLY from CLASSIFY (now a read-only
# CLI phase — WS-1) and is the pipeline's single question gate. Each phase
# below is individually resumable: a phase is skipped (its durable result
# reused) when its context flag / stored artifact already exists and has not
# been invalidated via _invalidate_from (the only path that re-runs a
# completed phase — used by go-back). Both run_feature_pipeline and
# run_bug_pipeline drive this SAME sequence via _drive_pre_sm; only the
# post-PLAN tail (Confluence/GitLab issue/HITL gate shape) differs by type.
# ============================================================

RESUMABLE_PRE_SM = ["PREFLIGHT", "BASELINE", "NORMALIZE", "CLASSIFYING", "PLAN"]

_CLASSIFY_REQUIRED_KEYS = ("complexity", "affected_components")

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "core_intent":         {"type": "string"},
        "affected_components": {"type": "array", "items": {"type": "string"}},
        "affected_stack":      {"type": "array"},
        "complexity":          {"type": "string"},
        "assignee_role":       {"type": "string"},
        "severity":            {"type": "string"},
        "category":            {"type": "string"},
        "reproduction":        {"type": "string"},
        "triage_steps":        {"type": "array"},
        "assets_to_check":     {"type": "array"},
        "open_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question":    {"type": "string"},
                    "options":     {"type": "array"},
                    "recommended": {"type": "integer"},
                    "rationale":   {"type": "string"},
                },
            },
        },
    },
    "required": list(_CLASSIFY_REQUIRED_KEYS),
}


def _phase_preflight(run_id: str, issue: dict) -> bool:
    """PREFLIGHT (token/connectivity checks + baseline-build gate, both inside
    _preflight_check) + working-branch creation. Skips re-checking when
    context.preflight_ok is set. Returns False when the run has already been
    marked FAILED/SUSPENDED — caller must return run_id."""
    ctx = (get_run(run_id) or {}).get("context") or {}
    if ctx.get("preflight_ok"):
        logger.info(f"SDLC {run_id}: PREFLIGHT reused (preflight_ok) — skipping re-check")
        return True
    if not _preflight_check(issue, run_id):
        return False
    if not _create_working_branch(issue, run_id):
        return False
    jira_key = issue.get("key", "")
    try:
        from tools.jira_tools import _jira_base
        jira_url = f"{_jira_base()}/browse/{jira_key}" if jira_key else ""
        if jira_url:
            patch_run_context(run_id, {"jira_url": jira_url})
    except Exception:
        pass
    user_id = issue.get("triggered_by_user_id", "")
    user_email = issue.get("triggered_by_email", "")
    if user_id or user_email:
        patch_run_context(run_id, {"user_id": user_id, "user_email": user_email})
    patch_run_context(run_id, {"preflight_ok": True})
    return True


def _phase_baseline(run_id: str, issue: dict, repo: str) -> Optional[dict]:
    """Repo resolution + repo-context fetch + language-confidence gate + early
    workspace checkout. Skips recompute when context.baseline_ok is set (the
    workspace cache already makes re-checkout cheap; this flag stops a
    duplicate repo-context fetch + run event on resume). Returns
    {repo_resolved, repo_ctx, detected_language, workspace_root}, or None when
    the run has already been marked FAILED (language-confidence gate)."""
    ctx = (get_run(run_id) or {}).get("context") or {}
    if ctx.get("baseline_ok") and ctx.get("repo_ctx"):
        repo_resolved = ctx.get("baseline_repo_resolved", "")
        _repo_ctx_map = ctx.get("repo_ctx") or {}
        logger.info(f"SDLC {run_id}: BASELINE reused (baseline_ok) — skipping repo/workspace re-fetch")
        return {
            "repo_resolved":     repo_resolved,
            "repo_ctx":          _repo_ctx_map.get(repo_resolved) or {},
            "detected_language": ctx.get("language", ""),
            "workspace_root":    ctx.get("workspace_root", ""),
        }

    user_id = issue.get("triggered_by_user_id", "")
    user_email = issue.get("triggered_by_email", "")
    repo_resolved = _run_with_timeout(_resolve_gitlab_repo, repo, step_name="resolve_gitlab_repo")

    # base_branch is MANDATORY — the whole run (workspace clone, language +
    # build-version detection, MR target) operates on it. We NEVER fall back to
    # GitLab's default branch: all detection/resolution must happen on the base
    # branch given at trigger. A missing base_branch is a hard failure, not a
    # silent default-branch substitution.
    _base_branch = (issue.get("base_branch") or "").strip()
    if not _base_branch:
        err_msg = (
            f"No base branch resolved for repo '{repo_resolved or repo or 'unknown'}'. "
            "base_branch is mandatory — configure the product's repo branch "
            "(product_repos.branch) or pass an explicit branch at trigger. "
            "The pipeline will not fall back to GitLab's default branch."
        )
        logger.critical(f"SDLC {run_id}: BLOCKED — {err_msg}")
        update_run_state(run_id, "FAILED", error=err_msg)
        return None

    _repo_ctx_map = _run_with_timeout(
        _fetch_repo_context, repo_resolved, _base_branch, user_id, user_email,
        step_name="fetch_repo_context",
    )
    repo_ctx = _repo_ctx_map.get(repo_resolved) or {}

    language_override = issue.get("language_override", "").strip().lower()
    if language_override:
        repo_ctx["language"] = language_override
        repo_ctx["confidence"] = 1.0
        repo_ctx["detection_source"] = "manual_override"
        _repo_ctx_map[repo_resolved] = repo_ctx
        logger.info(f"SDLC {run_id}: language_override={language_override!r} applied")

    lang_confidence = repo_ctx.get("confidence", 0.0)
    if lang_confidence < 0.8:
        err_msg = (
            f"Language detection failed for repo '{repo_resolved or 'unknown'}' "
            f"(confidence={lang_confidence:.2f}, source={repo_ctx.get('detection_source', 'none')}). "
            "Cannot proceed — generating code in the wrong language would corrupt your codebase. "
            "Fix: (1) ensure GitLab token is configured and repo is set in 'org/project' format, "
            "OR (2) index the codebase via CodebaseManager first, "
            "OR (3) set a manual language override when triggering the pipeline."
        )
        logger.critical(f"SDLC {run_id}: BLOCKED — {err_msg}")
        update_run_state(run_id, "FAILED", error=err_msg)
        try:
            import redis as _redis
            _rc = _redis.Redis(host=_REDIS_HOST, port=_REDIS_PORT, db=6,
                               decode_responses=True, socket_connect_timeout=2)
            _rc.xadd(f"chat:stream:{run_id}", {"type": "run_failed", "error": err_msg[:500]}, maxlen=100)
        except Exception as _re:
            logger.debug(f"SDLC {run_id}: could not push failure event to stream → {_re}")
        return None

    detected_language = repo_ctx.get("language") or ""
    patch_run_context(run_id, {"repo_ctx": _repo_ctx_map, "language": detected_language})
    logger.info(
        f"SDLC {run_id}: repo_ctx fetched — stack={repo_ctx.get('tech_stack')} "
        f"lang={detected_language} confidence={lang_confidence}"
    )

    workspace_root = _materialize_early_workspace(
        run_id, repo_resolved,
        issue.get("working_branch", "") or "",
        issue.get("base_branch", "") or "",
        user_id=issue.get("triggered_by_user_id", "") or "",
        user_email=issue.get("triggered_by_email", "") or "",
    )

    _relevant_tree = repo_ctx.get("file_tree", "")
    if _relevant_tree:
        repo_ctx = {**repo_ctx, "relevant_tree": _relevant_tree}
        _repo_ctx_map[repo_resolved] = repo_ctx
        patch_run_context(run_id, {"repo_ctx": _repo_ctx_map})

    # ── Build-metadata reconciliation (Issue 1) — runs off the BASE-BRANCH
    # checkout just materialized (never a GitLab default branch). Detects the
    # language version from the real build files and reconciles it against the
    # stored (product_id, repo) metadata: inserts it for a new/un-indexed repo,
    # proceeds on a match, or raises the AWAITING_BUILD_METADATA_APPROVAL gate on
    # a mismatch. On gate we return None WITHOUT setting baseline_ok, so a
    # post-confirmation resume re-enters here and skips the gate via the
    # build_metadata_confirmed flag.
    if _reconcile_build_metadata(run_id, issue, repo_resolved, workspace_root) == "gate":
        return None

    patch_run_context(run_id, {
        "baseline_ok":            True,
        "baseline_repo_resolved": repo_resolved,
        "workspace_root":         workspace_root,
    })
    return {
        "repo_resolved":     repo_resolved,
        "repo_ctx":          repo_ctx,
        "detected_language": detected_language,
        "workspace_root":    workspace_root,
    }


def _versions_match(a: str, b: str) -> bool:
    """Language-version equality for the build-metadata gate. Compares on the
    significant components only: an integer major (Java/Node, e.g. "17" == "17.0.9")
    and major.minor for dotted runtimes (Python "3.11" != "3.10"). Empty/one-sided
    values never spuriously match."""
    import re as _re

    def _norm(v: str) -> str:
        v = (v or "").strip().lower()
        m = _re.match(r"^(\d+)(?:\.(\d+))?", v)
        if not m:
            return v
        return f"{m.group(1)}.{m.group(2)}" if m.group(2) is not None else m.group(1)

    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # A bare major ("17") matches a dotted same-major ("17.0.x") for Java/Node,
    # but only when at least one side is a bare major (so "3.10" vs "3.11" stays a
    # mismatch).
    if ("." not in na or "." not in nb) and na.split(".")[0] == nb.split(".")[0]:
        return True
    return False


def _reconcile_build_metadata(run_id: str, issue: dict, repo_resolved: str,
                              workspace_root: str) -> str:
    """
    Reconcile the build version declared on the BASE-BRANCH checkout against the
    stored (product_id, repo) build metadata (Issue 1). Runs entirely off the
    already-materialized base-branch workspace — never queries GitLab or a
    default branch.

    Returns:
      "ok"   — proceed (matched, or new/unknown metadata was detected + stored)
      "gate" — a language-version mismatch was found; the run has been
               transitioned to AWAITING_BUILD_METADATA_APPROVAL and the caller
               must stop (return run_id).
    """
    ctx = (get_run(run_id) or {}).get("context") or {}
    # Already resolved once for this run (a post-confirmation resume) — never re-gate.
    if ctx.get("build_metadata_confirmed"):
        return "ok"
    if not workspace_root:
        # Nothing checked out — the language-confidence gate above already guards
        # the unknown-language case; the resolver detects at build time.
        return "ok"

    product_id = (issue.get("product_id") or ctx.get("product_id") or "").strip()
    try:
        from core.build_metadata_extractor import BuildMetadataExtractor
        ext = BuildMetadataExtractor()
        detected = ext.detect_from_workspace(repo_resolved, workspace_root, product_id=product_id)
    except Exception as exc:
        logger.warning(f"SDLC {run_id}: build-metadata detect failed (non-blocking): {exc}")
        return "ok"

    if not detected or not str(detected.get("language_version") or "").strip():
        # No recognizable build files / no version declared — not a reconcilable
        # condition; let the resolver handle it at build time.
        return "ok"

    det_ver = str(detected.get("language_version")).strip()
    try:
        stored = ext.read_stored(repo_resolved, product_id=product_id)
    except Exception:
        stored = None

    if not stored or not str(stored.get("language_version") or "").strip():
        # New / never-seen repo (or repo-only row absent) — persist the
        # base-branch-detected version under (product, repo) and proceed.
        try:
            ext.extract_and_store(repo_resolved, local_path=workspace_root, product_id=product_id)
            logger.info(
                f"SDLC {run_id}: build metadata inserted (new) "
                f"product={product_id or '(none)'} repo={repo_resolved} ver={det_ver}"
            )
        except Exception as exc:
            logger.warning(f"SDLC {run_id}: build-metadata insert failed (non-blocking): {exc}")
        return "ok"

    stored_ver = str(stored.get("language_version")).strip()
    if _versions_match(det_ver, stored_ver):
        # Match. If the hit came from the repo-only ('') fallback row, upsert a
        # product-scoped copy so subsequent reads are correctly scoped.
        if str(stored.get("product_id") or "") != product_id:
            try:
                ext.extract_and_store(repo_resolved, local_path=workspace_root, product_id=product_id)
            except Exception:
                pass
        logger.info(
            f"SDLC {run_id}: build metadata matches "
            f"product={product_id or '(none)'} repo={repo_resolved} ver={det_ver}"
        )
        return "ok"

    # ── Mismatch → HITL gate ──────────────────────────────────────────────────
    gate_payload = {
        "repo":             repo_resolved,
        "product_id":       product_id,
        "detected_version": det_ver,
        "stored_version":   stored_ver,
        "build_tool":       detected.get("build_tool") or stored.get("build_tool") or "",
        "language":         detected.get("language") or stored.get("language") or "",
        "workspace_root":   workspace_root,
    }
    _hitl_deadline = sdlc_gate_deadline("build_metadata")
    logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="build_metadata",
                hitl_deadline=_hitl_deadline)
    _transition(run_id, "AWAITING_BUILD_METADATA_APPROVAL", "build-metadata-gate")
    update_run_state(
        run_id, "AWAITING_BUILD_METADATA_APPROVAL",
        context_patch={
            "build_metadata_gate": gate_payload,
            "hitl_deadline":       _hitl_deadline,
            "gate_kind":           "build_metadata",
            "resume_stage":        "BASELINE",
            "original_issue":      dict(issue),
        },
    )
    _event(
        run_id, "AWAITING_BUILD_METADATA_APPROVAL", "build-metadata-gate",
        f"Build version mismatch on base branch for {repo_resolved}: "
        f"detected={det_ver} vs stored={stored_ver} — confirm which to use.",
        gate_payload,
    )
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id="platform",
            type="sdlc_gate",
            title=f"[SDLC] Confirm build version — {issue.get('key') or repo_resolved}",
            body=(f"Base-branch build version for **{repo_resolved}** is **{det_ver}**, "
                  f"but stored metadata says **{stored_ver}**. Confirm which to use."),
            source_id=run_id,
            metadata={"run_id": run_id, "stage": "AWAITING_BUILD_METADATA_APPROVAL"},
        )
    except Exception:
        pass
    logger.info(
        f"SDLC {run_id}: AWAITING_BUILD_METADATA_APPROVAL — detected={det_ver} "
        f"stored={stored_ver} product={product_id or '(none)'} repo={repo_resolved}"
    )
    return "gate"


def _phase_normalize(run_id: str, issue: dict, jira_key: str, repo_ctx: dict,
                      run_type: str = "feature") -> Optional[dict]:
    """TICKET_NORMALIZATION — GATE 1. ALWAYS suspends to AWAITING_USER_INPUT for
    WorkItem review/edit/approve, even when the normalizer raised no
    open_questions (WS-2 decision: the human confirms scope before any CLI
    spend). Returns the locked WorkItem dict once GATE 1 has been passed
    (context.normalization_confirmed_at set by resume_after_normalization_confirmed),
    or None when this call just raised the gate — caller must return run_id."""
    from agents.sdlc_pipeline._phases import _phase_normalize_ticket  # lazy: _core->_phases
    ctx = (get_run(run_id) or {}).get("context") or {}
    _stored_wi = ctx.get("work_item")
    if ctx.get("normalization_confirmed_at") and isinstance(_stored_wi, dict) and _stored_wi.get("locked"):
        logger.info(f"SDLC {run_id}: NORMALIZE reused (normalization_confirmed_at) — GATE 1 already passed")
        return _stored_wi

    work_item, open_questions = _phase_normalize_ticket(run_id, jira_key, issue, repo_ctx or {})
    _hitl_deadline = sdlc_gate_deadline("normalization")
    logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="normalization", hitl_deadline=_hitl_deadline)
    _transition(run_id, "AWAITING_USER_INPUT", "normalizer-agent")
    update_run_state(
        run_id, "AWAITING_USER_INPUT",
        context_patch={
            "work_item":         work_item.to_dict(),
            "open_questions":    open_questions,
            "pending_questions": open_questions,
            "hitl_deadline":     _hitl_deadline,
            "original_issue":    dict(issue),
            "run_type":          run_type,
            "gate_kind":         "normalization",
            "resume_stage":      "CLASSIFYING",
        },
    )
    logger.info(
        f"SDLC {run_id}: AWAITING_USER_INPUT (GATE 1 / WorkItem review) — "
        f"{len(open_questions)} normalizer question(s); paused before CLASSIFYING"
    )
    return None


def _phase_classify(run_id: str, issue: dict, jira_key: str, repo_resolved: str,
                     repo_ctx: dict, workspace_root: str, work_item: dict,
                     run_type: str = "feature") -> Optional[dict]:
    """CLASSIFY via CLI (WS-1) — read-only, small/haiku/local model
    (SDLC_CLI_CLASSIFY_MODEL), emits open_questions AT THE END. This is the
    pipeline's SINGLE question gate (GATE 2). Reuses the stored CLASSIFYING
    artifact (when not STALE) instead of re-invoking the CLI. Returns the
    classification dict, or None when the run has suspended (gate or hard
    failure) — caller must return run_id."""
    from agents.sdlc_pipeline._phases import _suspend_plan  # lazy: _core->_phases
    from agents.sdlc_cli_engine import run_cli, CliEngineConfig
    from agents.sdlc_cli_budget import (
        record_cli_usage, remaining_budget, derive_max_turns, is_exhausted,
    )
    from core.model_registry import cli_classify_model
    from store.sdlc_artifacts import (
        _load_latest_artifact, _store_artifact, compute_input_hash, compute_risk_score,
    )

    _existing = _load_latest_artifact(run_id, "CLASSIFYING")
    if (_existing and _existing.get("status") == "PRODUCED"
            and isinstance(_existing.get("payload"), dict) and _existing["payload"].get("complexity")):
        logger.info(f"SDLC {run_id}: CLASSIFY reused (artifact present) — skipping CLI classify")
        return _existing["payload"]

    _transition(run_id, "CLASSIFYING", "cli-classifier")

    # GATE 1 fires BEFORE this phase ever runs, so on the common path this call
    # happens on the resume worker, not the original trigger worker — the
    # `workspace_root` handed in from context may point at a checkout that
    # only exists on a different host. Re-materialize (reuse-or-clone; cheap
    # when the pinned checkout is already local) rather than trusting the
    # passed-in path — a workspace miss must never be a hard dependency here,
    # same rule _run_plan_phase follows for PLAN.
    workspace_root = _materialize_early_workspace(
        run_id, repo_resolved,
        issue.get("working_branch", "") or "",
        issue.get("base_branch", "") or "",
        user_id=issue.get("triggered_by_user_id", "") or "",
        user_email=issue.get("triggered_by_email", "") or "",
    ) or workspace_root

    try:
        full_jira = _read_jira_full(jira_key)
    except Exception as _jira_err:
        _fallback_desc = (issue.get("description", "") or issue.get("summary", "") or "").strip()
        logger.warning(f"SDLC {run_id}: JIRA fetch failed for {jira_key!r} ({_jira_err}) — using fallback description")
        full_jira = {"description": _fallback_desc, "summary": issue.get("summary", "")}
    full_jira_desc = _jira_description(
        full_jira, fallback=(issue.get("description", "") or issue.get("summary", "")).strip(),
    )
    if not full_jira_desc.strip():
        err = f"JIRA ticket {jira_key!r} has no description and no fallback was provided."
        logger.critical(f"SDLC {run_id}: BLOCKED — {err}")
        update_run_state(run_id, "FAILED", error=err)
        return None
    patch_run_context(run_id, {"full_jira": full_jira})

    if is_exhausted(run_id, "CLASSIFYING"):
        update_run_state(run_id, "FAILED", error="CLASSIFYING: per-run CLI budget exhausted")
        return None
    max_turns = derive_max_turns(remaining_budget(run_id, "CLASSIFYING"))

    wi_summary = json.dumps({
        "problem_statement": (work_item or {}).get("problem_statement", ""),
        "scope":             (work_item or {}).get("scope", []),
        "out_of_scope":      (work_item or {}).get("out_of_scope", []),
    }, default=str)

    _type_ask = (
        "- severity: Critical|High|Medium|Low\n"
        "- category: frontend|backend|api|database|auth|config|...\n"
        "- reproduction: Always|Sometimes|Rare\n"
        "- triage_steps: ordered investigation steps with specific file/function targets\n"
        "- assets_to_check: related configs, logs, environment variables to examine\n"
        "- assignee_role: Backend|Frontend|DevOps|Security\n"
        if run_type == "bug" else
        "- core_intent: what exactly needs to be built/changed (specific, not generic)\n"
        "- affected_stack: which detected stack(s) (this may be a monorepo) are involved\n"
        "- assignee_role: Backend|Frontend|DevOps|Security\n"
    )
    from agents.sdlc_implement_prompt import (
        workspace_boundary_clause as _wbc,
        search_discipline_clause as _search_discipline,
    )
    prompt = (
        f"You are a tech lead triaging a {'bug report' if run_type == 'bug' else 'feature ticket'} "
        "before any planning happens. Read the repo with your read-only tools as needed — do NOT "
        "guess file paths."
        f"{_wbc(workspace_root)}\n\n"
        f"TICKET {jira_key}: {issue.get('summary', '')}\n{full_jira_desc}\n\n"
        f"Normalized WorkItem (already approved): {wi_summary}\n\n"
        f"Repository: {repo_resolved} — language {repo_ctx.get('language', 'unknown')}/"
        f"{repo_ctx.get('framework', 'unknown')}\n\n"
        "Classify:\n"
        "- complexity: simple|medium|complex\n"
        "- affected_components: exact existing file paths (verify against the real tree; new files "
        "go unlisted here, PLAN will declare them)\n"
        f"{_type_ask}\n"
        "If genuinely ambiguous, ask at the END via open_questions (each "
        "{question, options:[2-4 strings], recommended:int index, rationale:string}); "
        "otherwise set open_questions: []. Output ONLY the JSON. Do not write or edit any files."
        # Search discipline (P1): stop the classify phase from re-running the SAME failed
        # exact-name search (the SecureNxt vs SecureNext 181x loop) — vary the query, cap
        # attempts per target, then proceed / raise an open_question instead of looping.
        f"{_search_discipline()}"
    )
    result = run_cli(
        config=CliEngineConfig.from_env(),
        workspace_root=workspace_root,
        prompt=prompt,
        profile="plan",
        model=cli_classify_model(),
        output_schema=CLASSIFY_SCHEMA,
        max_turns=max_turns,
        run_id=run_id,
        transient_retries=2,   # read-only CLASSIFY: safe to re-spawn on a proxy 502
    )
    try:
        record_cli_usage(run_id, result.usage or {}, result.total_cost_usd or 0.0)
    except Exception as _bue:
        logger.warning(f"SDLC {run_id}: CLASSIFY budget accounting failed (non-fatal): {_bue}")

    if result.status == "suspended":
        _suspend_plan(run_id, jira_key, "CLASSIFYING", result.reason or "cli suspended")
        return None

    classification = result.structured_output
    if classification is None:
        classification = _parse_json(result.result_text or "")
    if not isinstance(classification, dict) or not classification.get("complexity"):
        _suspend_plan(run_id, jira_key, "CLASSIFYING", "classify incomplete")
        return None

    open_qs = classification.get("open_questions") or []
    _fmt_cls = _fmt_bug_triage(classification) if run_type == "bug" else _fmt_classifier(classification)
    _event(run_id, "CLASSIFYING", "cli-classifier", json.dumps(classification, default=str),
           {"stage": "classification", "structured": _fmt_cls})
    _jira_comment(jira_key, f"[AI Classifier]\n{_fmt_cls}")

    try:
        _complexity_raw = str(classification.get("complexity") or "medium").lower().strip()
        _risk_score = compute_risk_score(
            complexity=_complexity_raw,
            files_to_change=len(classification.get("affected_components") or []),
            file_paths=classification.get("affected_components") or [],
        )
        _store_artifact(
            run_id, "CLASSIFYING",
            {**classification, "type": run_type, "complexity": _complexity_raw, "risk_score": _risk_score},
            producer="cli-classifier", input_hash=compute_input_hash(run_id, "CLASSIFYING"),
            created_by=issue.get("user_id") or issue.get("triggered_by") or "system",
        )
        logger.info(f"SDLC {run_id}: CLASSIFYING artifact stored — complexity={_complexity_raw} risk={_risk_score:.2f}")
    except Exception as _art_ex:
        logger.warning(f"SDLC {run_id}: CLASSIFYING artifact store failed (non-fatal): {_art_ex}")

    if open_qs:
        _hitl_deadline = sdlc_gate_deadline("questions")
        logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="questions", hitl_deadline=_hitl_deadline)
        _transition(run_id, "AWAITING_USER_INPUT", "cli-classifier")
        update_run_state(
            run_id, "AWAITING_USER_INPUT",
            context_patch={
                "classification":    classification,
                "pending_questions": open_qs,
                "hitl_deadline":     _hitl_deadline,
                "gate_kind":         "questions",
                "resume_stage":      "PLAN",
            },
        )
        logger.info(
            f"SDLC {run_id}: AWAITING_USER_INPUT (GATE 2 / classify questions) — "
            f"{len(open_qs)} question(s); paused before PLAN"
        )
        return None

    patch_run_context(run_id, {"classification": classification})

    # ── Gap-fix: eval_sdlc_classification (fire-and-forget) ──────────────────
    # Grade the classification output for hallucinated file paths and scope
    # accuracy in a background thread — never blocks the pipeline.
    try:
        import threading as _cls_threading
        import json as _cls_json
        _cls_desc   = issue.get("summary", "") or issue.get("description", "")
        _cls_output = _cls_json.dumps(classification, default=str)
        _cls_run_id = run_id
        _cls_ctx    = dict(repo_ctx)  # shallow copy — safe for background thread
        def _run_cls_eval():
            try:
                from core.evals import eval_engine as _ee
                _ee.eval_sdlc_classification(
                    ticket_description=_cls_desc[:600],
                    classification=_cls_output[:1000],
                    repo_ctx=_cls_ctx,
                    run_id=_cls_run_id,
                )
            except Exception as _ce:
                logger.debug(f"SDLC {_cls_run_id}: eval_sdlc_classification failed (non-critical): {_ce}")
        _cls_threading.Thread(
            target=_run_cls_eval, daemon=True, name="eval-sdlc-classification"
        ).start()
    except Exception:
        pass

    return classification


def _drive_pre_sm(run_id: str, issue: dict, jira_key: str, repo: str,
                   run_type: str, start_at: str = None) -> Optional[dict]:
    """WS-0 re-entrant pre-state-machine driver, shared by run_feature_pipeline
    and run_bug_pipeline (both share CLASSIFYING → PLAN post-cutover). Runs
    PREFLIGHT → BASELINE → NORMALIZE → CLASSIFY → PLAN starting at `start_at`
    (default: from the top); each _phase_* call above is itself responsible for
    skipping when its result is already durable. Returns
    {repo_resolved, detected_language, repo_ctx, work_item, classification, plan}
    on success, or None when a phase raised a gate or hard-failed (the run
    state is already set — caller must `return run_id`)."""
    from agents.sdlc_pipeline._phases import _run_plan_phase  # lazy: _core->_phases
    start_idx = RESUMABLE_PRE_SM.index(start_at.upper()) if start_at else 0

    if start_idx <= RESUMABLE_PRE_SM.index("PREFLIGHT"):
        if not _phase_preflight(run_id, issue):
            return None

    if start_idx <= RESUMABLE_PRE_SM.index("BASELINE"):
        _baseline = _phase_baseline(run_id, issue, repo)
        if _baseline is None:
            return None
    else:
        _ctx = (get_run(run_id) or {}).get("context") or {}
        _repo_ctx_map = _ctx.get("repo_ctx") or {}
        _repo_resolved = _ctx.get("baseline_repo_resolved", "")
        _baseline = {
            "repo_resolved":     _repo_resolved,
            "repo_ctx":          _repo_ctx_map.get(_repo_resolved) or {},
            "detected_language": _ctx.get("language", ""),
            "workspace_root":    _ctx.get("workspace_root", ""),
        }

    if start_idx <= RESUMABLE_PRE_SM.index("NORMALIZE"):
        work_item = _phase_normalize(run_id, issue, jira_key, _baseline["repo_ctx"], run_type)
        if work_item is None:
            return None
    else:
        work_item = (get_run(run_id) or {}).get("context", {}).get("work_item") or {}

    if start_idx <= RESUMABLE_PRE_SM.index("CLASSIFYING"):
        classification = _phase_classify(
            run_id, issue, jira_key, _baseline["repo_resolved"], _baseline["repo_ctx"],
            _baseline["workspace_root"], work_item, run_type,
        )
        if classification is None:
            return None
    else:
        classification = (get_run(run_id) or {}).get("context", {}).get("classification") or {}

    plan = _run_plan_phase(
        run_id, jira_key, _baseline["repo_resolved"], _baseline["detected_language"],
        issue, (get_run(run_id) or {}).get("context") or {}, run_type=run_type,
    )
    if plan is None:
        return None

    return {
        "repo_resolved":     _baseline["repo_resolved"],
        "detected_language": _baseline["detected_language"],
        "repo_ctx":          _baseline["repo_ctx"],
        "work_item":         work_item,
        "classification":    classification,
        "plan":              plan,
    }


def _invalidate_from(run_id: str, stage: str) -> None:
    """Mark `stage` and everything downstream of it (RESUMABLE_PRE_SM order)
    invalidated: clears the lightweight context flags for PREFLIGHT/BASELINE/
    NORMALIZE (which _drive_pre_sm's phase functions gate on directly) and
    marks the CLASSIFYING/PLAN artifacts STALE (audit trail — _phase_classify
    additionally checks status=='PRODUCED' so a STALE artifact is NOT reused).
    This is the ONLY path that re-runs an already-completed pre-SM stage."""
    from store.sdlc_artifacts import _mark_stale
    stage = (stage or "").upper()
    if stage not in RESUMABLE_PRE_SM:
        logger.warning(f"SDLC {run_id}: _invalidate_from unknown stage {stage!r} — no-op")
        return
    downstream = RESUMABLE_PRE_SM[RESUMABLE_PRE_SM.index(stage):]
    clear_patch = {}
    if "PREFLIGHT" in downstream:
        clear_patch["preflight_ok"] = False
    if "BASELINE" in downstream:
        clear_patch["baseline_ok"] = False
    if "NORMALIZE" in downstream:
        clear_patch["normalization_confirmed_at"] = False
    if "CLASSIFYING" in downstream:
        try:
            _mark_stale(run_id, "CLASSIFYING")
        except Exception as _e:
            logger.warning(f"SDLC {run_id}: _invalidate_from CLASSIFYING mark_stale failed: {_e}")
    if "PLAN" in downstream:
        try:
            _mark_stale(run_id, "PLAN")
        except Exception as _e:
            logger.warning(f"SDLC {run_id}: _invalidate_from PLAN mark_stale failed: {_e}")
    if clear_patch:
        patch_run_context(run_id, clear_patch)
    logger.info(f"SDLC {run_id}: invalidated pre-SM stages from {stage} onward: {downstream}")


def _rebuild_issue_from_context(run_id: str, run: dict, ctx: dict) -> dict:
    """Reconstruct the worker `issue` payload from run.context.original_issue
    (preferred) or the run's top-level columns (fallback for old runs). Shared
    by every pre-SM resume path so the reconstruction logic lives in one place."""
    issue = ctx.get("original_issue")
    if not isinstance(issue, dict) or not issue:
        issue = {
            "key":                  run.get("jira_key", ""),
            "summary":              run.get("jira_summary", ""),
            "description":          ctx.get("jira_description", ""),
            "repo":                 run.get("repo", ""),
            "triggered_by_user_id": ctx.get("user_id") or run.get("triggered_by", ""),
            "triggered_by_email":   ctx.get("user_email", ""),
            "base_branch":          ctx.get("base_branch", ""),
            "working_branch":       ctx.get("working_branch", ""),
            "language_override":    ctx.get("language_override", ""),
            "product_id":           ctx.get("product_id", ""),
        }
    issue = dict(issue)
    issue["_run_id"] = run_id
    # Always surface product_id from context even when original_issue was stored
    # (older runs may have an original_issue dict without it).
    if not issue.get("product_id"):
        issue["product_id"] = ctx.get("product_id", "")
    return issue


def resume_pre_sm_pipeline(run_id: str, start_at: str) -> str:
    """WS-0 resume entrypoint (replaces re-enqueuing the whole
    run_feature_pipeline_job/run_bug_pipeline_job — see run_pre_sm_resume_job).
    Re-enters _drive_pre_sm at `start_at` and, on success, runs the same
    post-PLAN tail (_finish_feature_pipeline / _finish_bug_pipeline) the
    original trigger would have run. Never restarts PREFLIGHT/BASELINE/
    NORMALIZE/CLASSIFY when they are already durable."""
    run = get_run(run_id)
    if not run:
        logger.error(f"resume_pre_sm_pipeline: run {run_id} not found")
        return run_id
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
    run_type = (ctx.get("run_type") or run.get("type") or "feature").lower()
    issue = _rebuild_issue_from_context(run_id, run, ctx)
    jira_key = run.get("jira_key", "")
    repo = run.get("repo", "")
    summary = run.get("jira_summary", "") or issue.get("summary", "")

    bind_context(correlation_id=run_id, pipeline_stage=f"sdlc_pre_sm_resume_{run_type}")
    _bind_llm_run_context(run_id, f"sdlc_pre_sm_resume_{run_type}")

    # ── author GitLab token (thread-local) ────────────────────────────────────
    # On a fresh resume job _drive_pre_sm skips PREFLIGHT/BASELINE (already durable),
    # so _preflight_check — the ONLY place set_token() fires on the initial trigger —
    # never runs. Without this, the GitLab tracking-issue created downstream in
    # _finish_feature_pipeline / _finish_bug_pipeline (gitlab_create_issue) falls back
    # to the SCM token env default and is filed under the platform's credentials
    # rather than the author's (branch/push already use the author PAT via the state
    # machine + clone URL, which is why only issue-creation showed the wrong owner).
    # Re-resolve the per-user PAT and set it on this thread. Never touch env token.
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import set_token as _gl_set_token
        else:
            from tools.gitlab_tools import set_token as _gl_set_token
        _author_uid = issue.get("triggered_by_user_id", "") or ctx.get("user_id", "")
        _author_tok = _gov_resolve_gitlab_token(_author_uid)
        if _author_tok:
            _gl_set_token(_author_tok)
    except Exception as _te:
        logger.warning("resume_pre_sm_pipeline: could not set author GitLab token — "
                       "GitLab issue may use env default", run_id=run_id, error=str(_te))

    try:
        driven = _drive_pre_sm(run_id, issue, jira_key, repo, run_type, start_at=start_at)
        if driven is None:
            return run_id
        if run_type == "bug":
            return _finish_bug_pipeline(run_id, issue, jira_key, repo, summary, driven)
        return _finish_feature_pipeline(run_id, issue, jira_key, repo, summary, driven)
    except SDLCCancelled:
        logger.info(f"resume_pre_sm_pipeline {run_id}: stopped — run cancelled mid-resume")
        return run_id
    except Exception as e:
        import traceback as _tb
        logger.error(f"resume_pre_sm_pipeline failed: run={run_id} → {e}\n{_tb.format_exc()}")
        update_run_state(run_id, "FAILED", error=str(e))
        return run_id
    finally:
        clear_bound_context()


# ============================================================
# FEATURE PIPELINE
# ============================================================

def run_feature_pipeline(issue: dict, run_id: Optional[str] = None) -> str:
    """
    Full CRED-equivalent feature pipeline.
    Runs asynchronously (called in BackgroundTasks).
    Returns run_id.
    """
    logger.info(f"TS- feature issue dictionary received - {issue}")
    summary     = issue.get("summary", issue.get("fields", {}).get("summary", "Unknown"))
    jira_key    = issue.get("key", "")
    repo        = issue.get("repo", "") or issue.get("fields", {}).get("customfield_repo", "") or ""
    user_id     = issue.get("triggered_by_user_id", "")
    user_email  = issue.get("triggered_by_email", "")

    # Use provided run_id if already created by the caller; else create one
    if run_id:
        run = get_run(run_id) or create_run(
            run_type="feature", jira_key=jira_key, jira_summary=summary,
            repo=repo, triggered_by="jira_webhook",
            created_by=user_id or "",
        )
        run_id = run["id"]
    else:
        run = create_run(
            run_type="feature", jira_key=jira_key, jira_summary=summary,
            repo=repo, triggered_by="jira_webhook",
            created_by=user_id or "",
        )
        run_id = run["id"]

    # Entry bail-check: a run cancelled while the job sat in the queue (e.g.
    # cancelled straight from CREATED) must not start. The cancel endpoint has
    # already freed the dedup slot; the worker's `finally` still releases the
    # per-reporter counter. Leave the state CANCELLED — do not run / do not FAIL.
    if run and run.get("state") in {"CANCELLED", "COMPLETE", "MERGED", "FAILED"}:
        logger.info(
            f"SDLC feature {run_id}: run already in terminal state "
            f"{run.get('state')!r} at worker pickup — skipping pipeline"
        )
        return run_id

    logger.info(f"SDLC feature pipeline started: run={run_id} jira={jira_key}")
    _pipeline_start = time.time()
    bind_context(pipeline_stage="sdlc_feature", task_id=jira_key, correlation_id=run_id)
    _bind_llm_run_context(run_id, "sdlc_feature")  # W-I-emit: attribute fallback events
    # Set user credentials in the execution context so all helpers in this
    # call chain automatically pick them up via _get_run_user().
    _cv_user_id.set(user_id)
    _cv_user_email.set(user_email)

    # Save the trigger payload to context on first entry so pre-SM resume paths
    # (resume_pre_sm_pipeline, called from the AWAITING_USER_INPUT endpoints) can
    # rebuild the worker payload exactly. Skipped on resume re-entry.
    _resume_ctx = (run.get("context") if isinstance(run, dict) else {}) or {}
    if not _resume_ctx.get("original_issue"):
        try:
            patch_run_context(run_id, {"original_issue": dict(issue), "run_type": "feature"})
        except Exception as _ois_exc:
            logger.warning(f"SDLC {run_id}: could not save original_issue to context: {_ois_exc}")

    try:
        # WS-0: PREFLIGHT → BASELINE → NORMALIZE (GATE 1) → CLASSIFY (CLI, GATE 2)
        # → PLAN (CLI), each individually resumable — see _drive_pre_sm. Returns
        # None when a phase raised a gate or hard-failed (state already set).
        driven = _drive_pre_sm(run_id, issue, jira_key, repo, "feature")
        if driven is None:
            return run_id
        return _finish_feature_pipeline(run_id, issue, jira_key, repo, summary, driven)

    except SDLCCancelled:
        logger.info(f"SDLC feature {run_id}: stopped — run cancelled mid-pipeline")
    except Exception as e:
        import traceback as _tb
        logger.error(f"SDLC feature pipeline failed: run={run_id} → {e}\n{_tb.format_exc()}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Pipeline error at feature stage: {e}")
        _teams_notify(run_id, f"❌ **Feature Pipeline Failed** — `{jira_key}`\nError: {str(e)[:300]}")
    finally:
        clear_bound_context()
        logger.info(
            "sdlc_pipeline_complete",
            run_id=run_id,
            run_type="feature",
            jira_key=jira_key,
            total_duration_s=round(time.time() - _pipeline_start, 1),
        )
    return run_id


def _finish_feature_pipeline(run_id: str, issue: dict, jira_key: str, repo: str,
                              summary: str, driven: dict) -> str:
    """Post-PLAN tail for the feature pipeline: Confluence publish, GitLab
    tracking issue, pre-gate codegen, AWAITING_CODE_APPROVAL. Shared by the
    initial trigger (run_feature_pipeline) and the WS-0 resume entrypoint
    (resume_pre_sm_pipeline) — unchanged from pre-gate-reorder behavior."""
    from agents.sdlc_pipeline._phases import _pregate_codegen  # lazy: _core->_phases
    repo_resolved     = driven["repo_resolved"]
    detected_language = driven["detected_language"]
    classification    = driven["classification"]
    analysis = design = driven["plan"]
    _fmt_sol = _fmt_solution(design, analysis)

    # ── Publish Confluence doc (retained; reads analysis/design/classification) ─
    confluence_url = _publish_confluence(
        title=f"[{jira_key}] {summary} — Solution Design",
        body=_make_confluence_md_feature(summary, classification, analysis, design),
        repo_name=repo,
    )
    patch_run_context(run_id, {"confluence_url": confluence_url})
    _jira_comment(jira_key,
                  f"[AI Solution Agent] Design complete.\n\n{_fmt_sol}\n\nConfluence: {confluence_url}")
    _update_origin_inbox(run_id, confluence_url)

    # ── Create SCM tracking issue (with full design context) — retained ──
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_create_issue as _gci
        else:
            from tools.gitlab_tools import gitlab_create_issue as _gci
        import re as _re_gi
        if repo_resolved:
            _jira_gi_url = (get_run(run_id) or {}).get("context", {}).get("jira_url", "")
            _complexity  = str(classification.get("complexity") or "").strip()
            _services    = ", ".join(
                _s(x) for x in (classification.get("impacted_services") or classification.get("affected_components") or [])
            ) or "—"
            _all_files   = [_s(f) for f in (list(analysis.get("files_to_change") or []) + list(analysis.get("new_files_needed") or []))]
            _approach    = str(design.get("solution_approach") or "").strip() or "\n".join(_s(x) for x in (design.get("implementation_plan") or []))
            _db_chg      = str(design.get("data_model_changes") or "").strip()
            _api_chg     = str(design.get("api_changes") or "").strip()
            _skip        = ("none", "n/a", "—", "-", "null", "{}")
            _parts = [
                "## Linked Jira Ticket",
                f"[{jira_key}]({_jira_gi_url})" if _jira_gi_url else jira_key,
                "",
                "## Summary",
                summary,
                "",
            ]
            if _complexity:
                _parts.append(f"**Complexity:** {_complexity}")
            if _services != "—":
                _parts += [f"**Impacted Services:** {_services}", ""]
            if _all_files:
                _parts += ["## Files Affected", ""]
                for _f in _all_files[:12]:
                    _parts.append(f"- `{_f}`")
                _parts.append("")
            if _approach:
                _parts += ["## Solution Approach", "", _approach, ""]
            if _db_chg and _db_chg.lower() not in _skip:
                _parts += ["## Database Changes", "", _db_chg, ""]
            if _api_chg and _api_chg.lower() not in _skip:
                _parts += ["## API Changes", "", _api_chg, ""]
            if confluence_url:
                _parts += ["## Design Document", "", f"[View on Confluence]({confluence_url})", ""]
            _parts += [
                "---",
                "_Auto-created by AiNxt AI — implementation in progress._",
                "_A PR will be linked to this issue upon completion._",
            ]
            _gl_str = _gci(repo=repo_resolved, title=f"[{jira_key}] {summary}", body="\n".join(_parts))
            logger.info(f"SDLC {run_id}: GitLab issue → {_gl_str}")
            _gi_m = _re_gi.search(r"(https://\S+/-/issues/\d+)", _gl_str)
            if _gi_m:
                patch_run_context(run_id, {"gitlab_issue_url": _gi_m.group(1)})
    except Exception as _gh_ex:
        logger.warning(f"SDLC {run_id}: GitLab issue creation failed → {_gh_ex}")

    logger.info(f"[SDLC {run_id}] PLAN complete — proceeding to pre-gate codegen")
    # ── Shift-left: pre-gate codegen (the human approves the real diff) ──
    logger.info(
        f"[SDLC {run_id}] calling _pregate_codegen",
        run_id=run_id, stage="PRE_CODING_BUILD",
        design_keys=list(design.keys()) if isinstance(design, dict) else [],
        analysis_keys=list(analysis.keys()) if isinstance(analysis, dict) else [],
        design_is_none=design is None,
        analysis_is_none=analysis is None,
    )
    if not _pregate_codegen(
        run_id, jira_key, repo_resolved, detected_language,
        design, analysis,
        issue.get("base_branch", ""), issue.get("working_branch", ""),
        (get_run(run_id) or {}).get("context") or {}, run_type="feature",
    ):
        logger.warning(f"[SDLC {run_id}] pre-gate codegen did not produce an approvable diff — not advancing to gate")
        return run_id

    # ── AWAITING CODE APPROVAL (HITL) ────────
    # (renamed 2026-07-29 from AWAITING_DESIGN_APPROVAL — expand/contract; readers dual-read)
    _hitl_deadline = sdlc_gate_deadline("code")
    logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="code", hitl_deadline=_hitl_deadline)
    _transition(run_id, "AWAITING_CODE_APPROVAL", "hitl-gate")
    update_run_state(run_id, "AWAITING_CODE_APPROVAL",
                     context_patch={
                         "classification": classification,
                         "analysis":       analysis,
                         "design":         design,
                         "confluence_url": confluence_url,
                         "hitl_deadline":  _hitl_deadline,
                         "base_branch":    issue.get("base_branch", ""),
                         "working_branch": issue.get("working_branch", ""),
                         # Multi-repo HITL payload — empty list for single-repo runs.
                         "repos":          _build_repos_payload(run_id, design),
                     })
    # NOTE: inbox `type` stays "design_approval" — it's a machine key the UI
    # Inbox maps to icon/label/approval-filter; only the run state was renamed.
    _inbox_notify(run_id, "design_approval",
                  f"[{jira_key}] Solution design ready for approval.\n\n{_fmt_sol}\n\nConfluence: {confluence_url}",
                  {"jira_key": jira_key, "confluence_url": confluence_url, "summary": summary})
    _teams_notify(run_id, hitl=True, stage="AWAITING_CODE_APPROVAL",
                  summary=f"[{jira_key}] Solution design ready for approval.\n\n{_fmt_sol[:600]}")
    logger.info(f"SDLC run {run_id}: AWAITING_CODE_APPROVAL — pipeline paused for engineer")
    return run_id  # Pipeline pauses here until /approve or /reject


def resume_after_user_answers(run_id: str, answers: list) -> None:
    """
    Resume a pipeline paused at AWAITING_USER_INPUT for GATE 2 (2026-07-02 gate
    reorder). GATE 2 is raised ONLY by CLASSIFY (WS-1's CLI classify phase) now
    — PLAN no longer emits questions. Pairs each answer with its pending
    question, stores the result in run.context.user_answers, and re-enters the
    pipeline directly at PLAN via the WS-0 pre-SM resume job — CLASSIFY is NOT
    re-run; PLAN's prompt injects user_answers as authoritative context.

    `answers` is a list aligned 1-1 with `run.context.pending_questions`, each
    entry shaped like:
        {"selected_option": int | None, "answer": str}
    where `answer` is either one of the option strings or the user's free-text
    override.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_question_resume")
    _bind_llm_run_context(run_id, "sdlc_question_resume")  # W-I-emit
    from store.sdlc_store import get_run as _get_run
    run = _get_run(run_id)
    if not run:
        raise ValueError(f"resume_after_user_answers: no run {run_id!r}")
    if run.get("state") != "AWAITING_USER_INPUT":
        raise ValueError(
            f"resume_after_user_answers: run {run_id!r} is in state {run.get('state')!r}, "
            f"not AWAITING_USER_INPUT"
        )
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
    pending_qs = ctx.get("pending_questions") or []
    if not pending_qs:
        logger.warning(f"resume_after_user_answers: run {run_id!r} had no pending_questions; resuming as no-op")

    # Pair each answer with its question text.
    paired: list = []
    answers = answers or []
    for idx, q in enumerate(pending_qs):
        ans_entry = answers[idx] if idx < len(answers) else {}
        if not isinstance(ans_entry, dict):
            ans_entry = {"answer": str(ans_entry)}
        text = (ans_entry.get("answer") or "").strip()
        sel  = ans_entry.get("selected_option")
        if not text and isinstance(sel, int):
            opts = q.get("options") or []
            if 0 <= sel < len(opts):
                text = opts[sel]
        if text:
            paired.append({
                "question":        q.get("question", ""),
                # Snapshot the options + recommended index + rationale alongside the
                # answer so the UI can show the full Q&A history (recommended option
                # vs what the user picked) even after pending_questions is cleared.
                "options":         q.get("options") or [],
                "recommended":     q.get("recommended"),
                "rationale":       q.get("rationale", ""),
                "answer":          text,
                "selected_option": sel if isinstance(sel, int) else None,
            })

    update_run_state(
        run_id, "RUNNING",
        context_patch={
            "user_answers":      paired,
            "pending_questions": [],
            "gate_kind":         "",
        },
    )
    _event(run_id, "AWAITING_USER_INPUT", "question-gate-resume",
           f"User submitted {len(paired)} answer(s); resuming at PLAN.",
           {"answers": paired})

    try:
        from core.job_queue import enqueue_hitl_resume_job
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.run_pre_sm_resume_job", run_id, extra={"start_at": "PLAN"},
        )
        logger.info(f"SDLC {run_id}: re-enqueued after GATE 2 answers → run_pre_sm_resume_job(start_at=PLAN) job_id={job_id}")
    except Exception as exc:
        logger.error(f"SDLC {run_id}: re-enqueue after user answers FAILED — {exc}")
        update_run_state(run_id, "FAILED", error=f"Could not resume pipeline after user answers: {exc}")
        raise


def resume_after_normalization_confirmed(run_id: str, answers: list, work_item: dict = None) -> None:
    """
    Resume a pipeline paused at AWAITING_USER_INPUT for GATE 1 (WorkItem
    review/approve — 2026-07-02 gate reorder). GATE 1 now ALWAYS fires after
    NORMALIZE, even with zero normalizer-raised questions, so the human can
    review/edit scope before any CLI spend. This function:
      1. Rebuilds the WorkItem from run.context.work_item
      2. Merges normalizer-question `answers` (if any) via
         NormalizationAgent.apply_user_answers
      3. Merges an optional human-edited `work_item` dict (WS-5 — scope/
         out_of_scope/acceptance_criteria edits from the approval UI) on top
      4. Locks + persists the WorkItem, then re-enters the pipeline directly at
         CLASSIFY via the WS-0 pre-SM resume job

    `answers` is a list of {field: str, answer: str} dicts aligned with the
    open_questions the normalizer raised (may be empty — GATE 1 fires even
    with none). `work_item` is an optional dict of human edits merged on top
    of the stored WorkItem (e.g. {"scope": [...], "out_of_scope": [...]}).
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_normalization_resume")
    _bind_llm_run_context(run_id, "sdlc_normalization_resume")
    from store.sdlc_store import get_run as _get_run
    run = _get_run(run_id)
    if not run:
        raise ValueError(f"resume_after_normalization_confirmed: no run {run_id!r}")
    if run.get("state") != "AWAITING_USER_INPUT":
        raise ValueError(
            f"resume_after_normalization_confirmed: run {run_id!r} is in state "
            f"{run.get('state')!r}, not AWAITING_USER_INPUT"
        )
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}

    # Rebuild WorkItem from stored dict
    from agents.sdlc_normalizer import WorkItem, NormalizationAgent
    _wi_dict = ctx.get("work_item") or {}
    work_item_obj = WorkItem.from_dict(_wi_dict)

    # Recover the target WorkItem `field` for each answer positionally. The UI
    # (OpenQuestionsForm) submits answers in the same order as pending_questions
    # but only carries {selected_option, answer} — it drops the `field` key that
    # apply_user_answers needs to know which WorkItem field each answer fills.
    # Without this re-alignment every answer is silently discarded (field empty
    # → `continue`), the WorkItem locks with no answers merged, and the user's
    # input is lost. Align by index against the questions that were raised.
    _pending = ctx.get("pending_questions") or ctx.get("open_questions") or []
    _aligned_answers = []
    for _i, _ans in enumerate(answers or []):
        if not isinstance(_ans, dict):
            continue
        _field = str(_ans.get("field") or "").strip()
        if not _field and _i < len(_pending) and isinstance(_pending[_i], dict):
            _field = str(_pending[_i].get("field") or "").strip()
        _aligned_answers.append({"field": _field, "answer": _ans.get("answer", "")})

    # Merge normalizer-question answers (no-op when empty — GATE 1 fires either way)
    _agent = NormalizationAgent(run_id=run_id)
    work_item_obj = _agent.apply_user_answers(work_item_obj, _aligned_answers)

    # WS-5: merge human-edited scope fields on top (e.g. from the WorkItemPanel
    # approve-with-edits flow). Only whitelisted fields — never let an arbitrary
    # payload clobber normalizer-derived fields it didn't intend to touch.
    if isinstance(work_item, dict) and work_item:
        _editable = ("problem_statement", "scope", "out_of_scope", "acceptance_criteria")
        for _k in _editable:
            if _k in work_item:
                setattr(work_item_obj, _k, work_item[_k])
        logger.info(f"[NORM {run_id}] GATE 1 approve-with-edits: merged {[k for k in _editable if k in work_item]}")

    work_item_obj.locked = True
    locked_dict = work_item_obj.to_dict()

    # Persist the locked WorkItem to the DB
    try:
        from store.sdlc_store import update_run_work_item
        update_run_work_item(run_id, locked_dict)
    except Exception as _persist_err:
        logger.warning(
            f"[NORM {run_id}] normalization resume: DB persist failed (non-fatal): {_persist_err}"
        )

    # Advance state and record the event
    update_run_state(
        run_id, "RUNNING",
        context_patch={
            "work_item": locked_dict,
            "normalization_confirmed_at": True,
            "pending_questions": [],
            "gate_kind": "",
        },
    )
    _event(
        run_id, "AWAITING_USER_INPUT", "normalization-gate-resume",
        f"GATE 1 approved — WorkItem locked ({len(answers or [])} normalizer answer(s)"
        f"{', with human edits' if work_item else ''}); resuming at CLASSIFY.",
        {"n_answers": len(answers or []), "edited": bool(work_item)},
    )
    logger.info(
        f"[NORM {run_id}] GATE 1 passed — WorkItem locked n_answers={len(answers or [])} edited={bool(work_item)}",
        run_id=run_id, gate_kind="normalization",
    )

    try:
        from core.job_queue import enqueue_hitl_resume_job
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.run_pre_sm_resume_job", run_id, extra={"start_at": "CLASSIFYING"},
        )
        logger.info(f"SDLC {run_id}: re-enqueued after GATE 1 approval → run_pre_sm_resume_job(start_at=CLASSIFYING) job_id={job_id}")
    except Exception as exc:
        logger.error(f"SDLC {run_id}: re-enqueue after normalization answers FAILED — {exc}")
        update_run_state(run_id, "FAILED", error=f"Could not resume pipeline after normalization answers: {exc}")
        raise


def resume_build_metadata_gate(run_id: str, choice: str, chosen_version: str = "") -> str:
    """
    Resume a run paused at AWAITING_BUILD_METADATA_APPROVAL (Issue 1).

    `choice` ∈ {"detected", "stored", "custom"}:
      • detected → adopt the base-branch-detected version
      • stored   → keep the previously-stored version
      • custom   → use the operator-supplied `chosen_version`

    Persists the confirmed language_version to repo_build_metadata (product, repo),
    invalidates the cached resolved manifest so the newly-selected builder image is
    picked up, marks the run build_metadata_confirmed, and re-enters the pre-SM
    pipeline at BASELINE (which now skips the gate via that flag).
    """
    import os as _os
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_build_metadata_resume")
    from store.sdlc_store import get_run as _get_run
    run = _get_run(run_id)
    if not run:
        raise ValueError(f"resume_build_metadata_gate: no run {run_id!r}")
    if run.get("state") != "AWAITING_BUILD_METADATA_APPROVAL":
        raise ValueError(
            f"resume_build_metadata_gate: run {run_id!r} is in state "
            f"{run.get('state')!r}, not AWAITING_BUILD_METADATA_APPROVAL"
        )
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
    gate = ctx.get("build_metadata_gate") or {}
    repo = gate.get("repo") or run.get("repo") or ""
    product_id = (gate.get("product_id") or ctx.get("product_id") or "").strip()
    workspace_root = gate.get("workspace_root") or ctx.get("workspace_root") or ""

    _choice = (choice or "").strip().lower()
    if _choice == "detected":
        version = str(gate.get("detected_version") or "").strip()
    elif _choice == "stored":
        version = str(gate.get("stored_version") or "").strip()
    elif _choice == "custom":
        version = str(chosen_version or "").strip()
    else:
        raise ValueError(f"resume_build_metadata_gate: invalid choice {choice!r}")
    if not version:
        raise ValueError("resume_build_metadata_gate: resolved version is empty")

    # Re-materialize the base-branch checkout when the gate ran on a different
    # host (workspace jails to a per-run dir the resume worker may not have).
    # Cheap when the pinned checkout is already local (reuse-or-clone).
    if not workspace_root or not _os.path.isdir(workspace_root):
        try:
            workspace_root = _materialize_early_workspace(
                run_id, repo,
                ctx.get("working_branch", "") or "",
                ctx.get("base_branch", "") or "",
                user_id=ctx.get("user_id", "") or "",
                user_email=ctx.get("user_email", "") or "",
            )
        except Exception as exc:
            logger.warning(f"SDLC {run_id}: build-metadata resume workspace re-materialize failed: {exc}")

    # Persist the confirmed version + refresh the resolved-manifest cache so the
    # newly-selected versioned builder image is picked up on the next resolve().
    try:
        from core.build_metadata_extractor import BuildMetadataExtractor
        BuildMetadataExtractor().store_confirmed_version(
            repo, workspace_root, product_id, version,
        )
    except Exception as exc:
        logger.warning(f"SDLC {run_id}: store confirmed build version failed (non-blocking): {exc}")
    try:
        from core.build_manifest_resolver import BuildManifestResolver
        BuildManifestResolver().invalidate_cache(repo)
    except Exception as exc:
        logger.warning(f"SDLC {run_id}: invalidate manifest cache failed (non-blocking): {exc}")

    # build_metadata_confirmed is set unconditionally so a re-run of _phase_baseline
    # never re-raises the gate, even if the persist above degraded.
    update_run_state(
        run_id, "RUNNING",
        context_patch={
            "build_metadata_confirmed": True,
            "build_metadata_choice":    _choice,
            "build_metadata_version":   version,
            "gate_kind":                "",
        },
    )
    _event(
        run_id, "AWAITING_BUILD_METADATA_APPROVAL", "build-metadata-gate-resume",
        f"Build version confirmed ({_choice}) = {version} for {repo}; resuming at BASELINE.",
        {"choice": _choice, "version": version, "repo": repo, "product_id": product_id},
    )
    try:
        from core.job_queue import enqueue_hitl_resume_job
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.run_pre_sm_resume_job", run_id, extra={"start_at": "BASELINE"},
        )
        logger.info(
            f"SDLC {run_id}: re-enqueued after build-metadata gate → "
            f"run_pre_sm_resume_job(start_at=BASELINE) job_id={job_id}"
        )
    except Exception as exc:
        logger.error(f"SDLC {run_id}: re-enqueue after build-metadata gate FAILED — {exc}")
        update_run_state(run_id, "FAILED",
                         error=f"Could not resume after build-metadata confirmation: {exc}")
        raise
    return run_id


def _can_waive(actor_user_id: str, run: dict, jwt_claims: dict) -> bool:
    """Return True if actor may waive a gate: run owner or can_approve privilege."""
    created_by   = run.get("created_by") or ""
    triggered_by = run.get("triggered_by") or ""
    return (
        actor_user_id == created_by
        or actor_user_id == triggered_by
        or bool(jwt_claims.get("can_approve"))
    )


def cascade_preview(run_id: str, target_stage: str) -> dict:
    """
    Pure read-only walk from target_stage through the run-type's stage sequence.
    Returns {preserved, will_re_run, stale_now} — no side effects.

    Optional stages (e.g. GOVERNANCE_SCAN) are excluded from will_re_run and
    stale_now when the run is already at or past AWAITING_PR_APPROVAL AND no
    artifact was produced for that stage (governance-disabled path). This
    prevents callers from treating an absent optional-stage artifact as an
    incomplete / stuck run.
    """
    from store.sdlc_store import get_run
    from store.sdlc_artifacts import (
        stage_sequence_for, is_optional_stage, _load_latest_artifact,
    )
    _run = get_run(run_id) or {}
    all_stages = stage_sequence_for(_run.get("type") or _run.get("run_type") or "feature")
    target_idx = all_stages.index(target_stage) if target_stage in all_stages else -1

    # States that indicate the run has already moved past any governance gate.
    # When a run is in one of these states AND an optional stage has no artifact,
    # that stage was intentionally skipped (governance disabled / waived) and
    # must not be counted as pending or stale.
    _POST_GOV_STATES = frozenset({
        "AWAITING_PR_APPROVAL", "COMPLETED", "COMPLETE",
    })
    _run_state = _run.get("state", "")

    def _optional_and_satisfied(stage: str) -> bool:
        """True when *stage* is optional, was never run, and the run is past it."""
        if not is_optional_stage(stage):
            return False
        if _run_state not in _POST_GOV_STATES:
            return False
        artifact = _load_latest_artifact(run_id, stage)
        if artifact is not None:
            # Artifact exists — governance did run; don't skip in the preview.
            return False
        logger.debug(
            "[SDLC] optional stage skipped in progress calc",
            run_id=run_id,
            stage=stage,
        )
        return True

    preserved   = [s for s in all_stages[:target_idx]  if not _optional_and_satisfied(s)] if target_idx > 0  else []
    will_re_run = [s for s in all_stages[target_idx:]  if not _optional_and_satisfied(s)] if target_idx >= 0 else []
    stale_now   = list(will_re_run)  # all downstream stages become STALE

    return {
        "preserved":   preserved,
        "will_re_run": will_re_run,
        "stale_now":   stale_now,
    }


def retrigger_pipeline(
    run_id: str,
    *,
    skip_compile: bool = False,
    skip_tests: bool = None,
    actor: str = "baseline-resume",
    jwt_claims: dict = None,
) -> dict:
    """
    Re-enter the FULL pipeline for a run SUSPENDED at BASELINE_BUILD (RFD §4.1).

    This is deliberately NOT resume_from_stage. The baseline build gate runs inside
    _preflight_check — *before* any artifact-backed stage and before CLASSIFYING — so
    BASELINE_BUILD is absent from stage_sequence_for() and the stage-resume validator
    structurally cannot resume it. The only correct re-entry is to re-run the whole
    pipeline (preflight → baseline gate → CLASSIFYING → …), exactly as the original
    trigger did, keyed to the SAME run_id.

    The operator pushed a repo fix ("I'll fix the repo"); the gate simply rebuilds HEAD
    and (if green) proceeds. Zero autonomous edits.

    The issue dict is reconstructed from the saved ``context.original_issue`` (preferred)
    or the scalar run columns (fallback) — the same pattern resume_after_user_answers
    uses.

    Re-enqueues via enqueue_sdlc_job (full re-admission): the per-Jira dedup slot and the
    per-reporter counter were released by the worker's ``finally`` when the run suspended,
    so this cleanly re-acquires them — the same lifecycle resume_after_user_answers relies
    on. Returns {"job_id": ...}.
    """
    run = get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    state = run.get("state", "")
    ctx = run.get("context") if isinstance(run.get("context"), dict) else {}
    suspended_stage = (ctx or {}).get("suspended_at_stage")
    if state != "SUSPENDED" or suspended_stage != "BASELINE_BUILD":
        raise ValueError(
            "retrigger_pipeline requires a run SUSPENDED at BASELINE_BUILD; "
            f"got state={state!r} suspended_at_stage={suspended_stage!r}"
        )

    # Reconstruct the worker payload from the saved original_issue (preferred) or
    # from the run's scalar columns (fallback for runs created before original_issue
    # was persisted) — mirror resume_after_user_answers.
    issue = ctx.get("original_issue")
    if not isinstance(issue, dict) or not issue:
        issue = {
            "key":                  run.get("jira_key", ""),
            "summary":              run.get("jira_summary", ""),
            "description":          ctx.get("jira_description", ""),
            "repo":                 run.get("repo", ""),
            "triggered_by_user_id": ctx.get("user_id", "") or run.get("triggered_by", ""),
            "triggered_by_email":   ctx.get("user_email", ""),
            "base_branch":          ctx.get("base_branch", ""),
            "working_branch":       ctx.get("working_branch", ""),
            "language_override":    ctx.get("language_override", ""),
        }
    issue = dict(issue)
    issue["_run_id"] = run_id

    run_type = (ctx.get("run_type") or run.get("type") or run.get("run_type") or "feature").lower()
    worker_fn = (
        "workers.sdlc_worker.run_bug_pipeline_job" if run_type == "bug"
        else "workers.sdlc_worker.run_feature_pipeline_job"
    )

    # Reset to a running precursor state so the worker entry bail-check (which skips
    # terminal states) lets the pipeline run again, and stamp the resume actor so the
    # gate / UI can read it. CREATED is the clean pre-CLASSIFYING running state.
    _ctx_patch = {
        "baseline_resume_actor": str(actor),
    }
    if skip_compile:
        # Mark the run so the baseline gate AND every downstream compile point
        # (build-check, multi-repo dep install, test loop) skip compilation. Add
        # a waiver banner so the MR / UI surfaces that the build was not verified.
        _ctx_patch["compile_skipped"] = True
        _banners = list(ctx.get("waiver_banners") or [])
        _banners.append(
            f"⚠ Compilation SKIPPED at BASELINE_BUILD by {actor} — "
            f"code was generated and committed WITHOUT a successful build."
        )
        _ctx_patch["waiver_banners"] = _banners
    if skip_tests is not None:
        # Explicit build-gate-failure escape: engineer opts to skip tests+SLT on resume.
        # Never automatic — must be a deliberate action at the SUSPENDED panel.
        _ctx_patch["skip_tests"] = bool(skip_tests)
        if skip_tests:
            logger.info(
                f"retrigger_pipeline: {run_id} — skip_tests=True set explicitly by {actor} "
                f"at build-gate-failure resume; TESTING+SLT will be bypassed."
            )

    update_run_state(
        run_id, "CREATED",
        current_stage="CREATED",
        context_patch=_ctx_patch,
    )
    if skip_compile:
        _resume_label = "compile-skipped"
    else:
        _resume_label = "operator-fixed"
    add_run_event(
        run_id,
        from_state=state,
        to_state="CREATED",
        stage="BASELINE_BUILD",
        actor=str(actor),
        output=(
            f"Baseline resume ({_resume_label}) — "
            f"re-entering the full pipeline ({run_type})"
        ),
        data={"skip_compile": bool(skip_compile),
              "skip_tests": skip_tests, "run_type": run_type},
    )

    from core.job_queue import enqueue_sdlc_job
    try:
        job_id = enqueue_sdlc_job(worker_fn, issue)
    except Exception as exc:
        logger.error(f"retrigger_pipeline {run_id}: re-enqueue FAILED — {exc}")
        # Keep the run resumable (suspend-not-fail): restore the SUSPENDED marker so
        # the operator can retry rather than landing in a dead CREATED state.
        try:
            update_run_state(
                run_id, "SUSPENDED",
                current_stage="BASELINE_BUILD",
                suspended_at_stage="BASELINE_BUILD",
                context_patch={"suspended_at_stage": "BASELINE_BUILD"},
            )
        except Exception:
            pass
        raise
    logger.info(
        f"retrigger_pipeline: {run_id} re-entered pipeline "
        f"(run_type={run_type}) → {worker_fn} job_id={job_id}"
    )
    return {"job_id": job_id}


def resume_from_stage(
    run_id:           str,
    target_stage:     str,
    mode:             str,        # 'retry' | 'go_back' | 'override' | 'waive'
    *,
    feedback:         str  = None,
    override_payload: dict = None,
    actor:            str,
    reason:           str  = None,
    jwt_claims:       dict = None,
) -> dict:
    """
    Generic resume entry point for the flexible pipeline.

    Modes:
      retry    — Re-run target stage with NL feedback. No special auth.
      go_back  — Same as retry but targeted at an upstream stage.
      override — Human replaces payload directly. Requires can_approve.
      waive    — Accept a failing gate and proceed. Run owner OR can_approve.

    Returns {job_id, cascade_preview}.
    Does NOT call resume_feature_after_design_approval or resume_bug_after_solution_approval.
    """
    from store.sdlc_store import get_run, update_run_state, add_run_event
    from store.sdlc_artifacts import (
        MANDATORY_STAGES, _mark_stale, _store_artifact, compute_input_hash,
        stage_sequence_for, pre_sm_revision_stages,
    )
    from core.job_queue import enqueue_hitl_resume_job

    jwt_claims = jwt_claims or {}

    # 1. Load run
    run = get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found")
    state = run.get("state", "")
    if state not in ("SUSPENDED", "COMPLETED", "FAILED", "COMPLETE", "AWAITING_PR_APPROVAL"):
        raise ValueError(
            f"resume_from_stage requires run in SUSPENDED/COMPLETED/FAILED state; got {state!r}"
        )

    # Resumable stages are run-type-native: feature exposes ANALYZING/DESIGNING,
    # bug exposes TROUBLESHOOTING/SOLUTIONING; both share the CODING→COMMITTING
    # state-machine tail. See store/sdlc_artifacts.stage_sequence_for.
    run_type = (run.get("type") or run.get("run_type") or "feature").lower()
    seq      = stage_sequence_for(run_type)
    if target_stage not in seq:
        raise ValueError(
            f"Stage {target_stage!r} is not resumable for a {run_type} run. "
            f"Valid stages: {seq}"
        )

    # Gate-reorder (2026-07-02): NORMALIZE/CLASSIFYING/PLAN are all individually
    # go-back-able pre-SM phases now (WS-0/WS-1) — CLASSIFYING is no longer a
    # trigger-time-only mandatory root. See pre_sm_revision_stages.
    _EARLY_REVISION_STAGES = pre_sm_revision_stages(run_type)

    # 2. Authorization
    if mode == "override":
        if not jwt_claims.get("can_approve"):
            raise PermissionError("mode=override requires can_approve privilege")
        if not reason:
            raise ValueError("reason is required for mode=override")
    elif mode == "waive":
        if target_stage in MANDATORY_STAGES:
            raise PermissionError(f"Stage {target_stage!r} is mandatory and cannot be waived")
        if not reason:
            raise ValueError("reason is required for mode=waive")
        if not _can_waive(actor, run, jwt_claims):
            raise PermissionError(
                "mode=waive requires run ownership or can_approve privilege"
            )

    # 3. Compute cascade preview (pure, no side effects)
    preview = cascade_preview(run_id, target_stage)

    # 4. Mark downstream stages STALE (walk the run-type sequence from target on)
    target_idx      = seq.index(target_stage)
    stages_to_stale = seq[target_idx:]
    for s in stages_to_stale:
        try:
            _mark_stale(run_id, s)
        except Exception as _me:
            logger.warning(f"resume_from_stage: _mark_stale({s}) failed: {_me}")

    # 5. Mode-specific logic

    # Pre-SM stages (NORMALIZE/CLASSIFYING/PLAN) go back through the WS-0
    # re-entrant driver: _invalidate_from marks the target + everything
    # downstream stale/cleared, then _drive_pre_sm re-enters at that exact
    # stage — NOT the old ANALYZING/DESIGNING revision path (dead post-cutover).
    # Only retry/go_back route here; waive/override fall through below.
    if target_stage in _EARLY_REVISION_STAGES and mode in ("retry", "go_back"):
        _invalidate_from(run_id, target_stage)
        add_run_event(
            run_id,
            from_state=state,
            to_state="RUNNING",
            stage=target_stage,
            actor=str(actor),
            output=f"{mode.upper()}→{target_stage}: {feedback or ''}",
            data={"mode": mode, "stage": target_stage, "feedback": feedback},
        )
        update_run_state(
            run_id, "RUNNING",
            context_patch={
                "resume_feedback": feedback or "",
                "resume_stage":    target_stage,
            },
        )
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.run_pre_sm_resume_job", run_id,
            feedback=feedback or "", extra={"start_at": target_stage},
        )
        logger.info(
            f"resume_from_stage: {run_id} go-back to {target_stage} "
            f"(run_type={run_type}) → run_pre_sm_resume_job"
        )
        return {"job_id": job_id, "cascade_preview": preview}

    if mode == "waive":
        # Write WAIVED artifact
        try:
            _store_artifact(
                run_id=run_id,
                stage=target_stage,
                payload={"waived": True, "reason": reason, "actor": actor},
                producer=f"human:{actor}",
                input_hash=compute_input_hash(run_id, target_stage),
                created_by=actor,
                reason=reason,
            )
        except Exception as _we:
            logger.warning(f"resume_from_stage: waive artifact store failed: {_we}")

        # Signed audit event
        add_run_event(
            run_id,
            from_state=state,
            to_state="RUNNING",
            stage=target_stage,
            actor=str(actor),
            output=f"WAIVED: {reason}",
            data={"mode": "waive", "stage": target_stage, "reason": reason, "actor": actor},
        )

        # Waiver banner — stored in context for MR description
        banner = (
            f"⚠ [{target_stage}] gate WAIVED by {actor} "
            f"— Reason: {reason} — Run ID: {run_id}"
        )
        ctx = run.get("context") or {}
        existing_banners = ctx.get("waiver_banners") or []
        existing_banners.append(banner)
        ctx_patch: dict = {"waiver_banners": existing_banners}
        if target_stage == "TEST_VERIFY":
            # Waiving tests: tell the resumed post-gate machine to skip TEST_VERIFY
            # so it chains APPLYING → (skip) → SLT_RUNNING → COMMITTING → MR.
            ctx_patch["skip_tests"] = True
        update_run_state(
            run_id, "RUNNING",
            context_patch=ctx_patch,
        )

        # Advance to the next stage after the waived one.
        #  • REVIEW waive = "accept the review outcome; let the human approve the DIFF".
        #    It must land on the HITL diff-approval gate (AWAITING_CODE_APPROVAL /
        #    AWAITING_SOLUTION_APPROVAL) — the SAME place a passing REVIEW goes — NOT
        #    APPLYING. Routing REVIEW→APPLYING skipped the human diff gate and pushed a
        #    PR straight to AWAITING_PR_APPROVAL (the reported bug). The post-gate
        #    APPLYING→COMMIT chain runs only AFTER the human approves at that gate
        #    (resume_feature_job / resume_bug_job).
        #  • TEST_VERIFY waive stays post-gate (diff already human-approved earlier):
        #    route through APPLYING with skip_tests=True so it chains straight to
        #    SLT_RUNNING → COMMITTING → MR. Jumping to COMMITTING would apply nothing.
        next_stages = seq[target_idx + 1:]
        next_stage  = next_stages[0] if next_stages else None
        if target_stage == "REVIEW":
            next_stage = "AWAITING_APPROVAL"   # sentinel — worker resolves bug vs feature gate
        elif target_stage == "TEST_VERIFY":
            next_stage = "APPLYING"
        # Resume continuation — use the HITL-resume enqueue, NOT enqueue_sdlc_job:
        # the latter applies new-run admission guards (per-reporter counter +
        # Jira dedup) that would credit this to reporter "unknown" and leak the
        # counter until resumes are rate-limited. See enqueue_hitl_resume_job.
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.resume_from_stage_job",
            run_id,
            feedback="",
            extra={
                "target_stage": next_stage or target_stage,
                "mode":         "waive",
                "actor":        actor,
                "reason":       reason,
            },
        )
        return {"job_id": job_id, "cascade_preview": preview}

    elif mode == "override":
        # Write override artifact with human payload
        try:
            _store_artifact(
                run_id=run_id,
                stage=target_stage,
                payload=override_payload or {},
                producer=f"human:{actor}",
                input_hash=compute_input_hash(run_id, target_stage),
                created_by=actor,
                reason=reason,
            )
        except Exception as _oe:
            logger.warning(f"resume_from_stage: override artifact store failed: {_oe}")

        add_run_event(
            run_id,
            from_state=state,
            to_state="RUNNING",
            stage=target_stage,
            actor=str(actor),
            output=f"OVERRIDE: {reason}",
            data={"mode": "override", "stage": target_stage, "reason": reason},
        )
        update_run_state(run_id, "RUNNING")

        # Continue from downstream of the overridden stage
        next_stages = seq[target_idx + 1:]
        next_stage  = next_stages[0] if next_stages else target_stage
        # Resume continuation — HITL-resume enqueue (no new-run admission guards).
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.resume_from_stage_job",
            run_id,
            feedback="",
            extra={
                "target_stage":     next_stage,
                "mode":             "override",
                "override_payload": override_payload,
                "actor":            actor,
                "reason":           reason,
            },
        )
        return {"job_id": job_id, "cascade_preview": preview}

    else:
        # retry or go_back — re-run from target_stage with optional feedback
        add_run_event(
            run_id,
            from_state=state,
            to_state="RUNNING",
            stage=target_stage,
            actor=str(actor),
            output=f"{mode.upper()}: {feedback or ''}",
            data={"mode": mode, "stage": target_stage, "feedback": feedback},
        )
        update_run_state(run_id, "RUNNING",
                         context_patch={"resume_feedback": feedback, "resume_stage": target_stage})
        # Resume continuation — HITL-resume enqueue (no new-run admission guards).
        job_id = enqueue_hitl_resume_job(
            "workers.sdlc_worker.resume_from_stage_job",
            run_id,
            feedback=feedback or "",
            extra={
                "target_stage": target_stage,
                "mode":         mode,
                "actor":        actor,
            },
        )
        return {"job_id": job_id, "cascade_preview": preview}


def resume_feature_after_design_approval(run_id: str, feedback: str = "",
                                          skip_tests_override: bool = None) -> None:
    """Called after engineer approves the design. Continues to coding.

    ``skip_tests_override`` — when explicitly True/False, overrides the value stored in
    the run context, letting the engineer decide at resume time whether tests+SLT are
    skipped.  None = honour the context value (backward-compatible default).
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_feature_resume")
    _bind_llm_run_context(run_id, "sdlc_feature_resume")  # W-I-emit
    from agents.sdlc_pipeline._phases import _post_governance_mr_note_if_present  # lazy: _core->_phases
    import os
    from store.sdlc_store import get_run_for_hitl
    run = get_run_for_hitl(run_id, required_keys=["design", "analysis"])
    if not run:
        return

    jira_key    = run["jira_key"]
    repo        = _resolve_gitlab_repo(run["repo"])
    ctx         = run["context"] if isinstance(run["context"], dict) else {}

    # Enforce HITL deadline — if 48h window expired, auto-fail rather than running stale
    import time as _time
    _hitl_deadline = ctx.get("hitl_deadline")
    if _hitl_deadline and int(_time.time()) > int(_hitl_deadline):
        logger.error(f"SDLC {run_id}: HITL approval deadline expired — marking FAILED")
        update_run_state(run_id, "FAILED", error="HITL design approval deadline expired (48h window passed without action)")
        _jira_comment(run["jira_key"], "[AiNxt AI] ❌ Design approval window expired (48h). Please re-trigger the pipeline.")
        return

    design      = ctx.get("design", {})
    analysis    = ctx.get("analysis", {})

    # Language resolution order:
    # 1. repo_ctx stored in run context — may be the new multi-repo map {repo: inner} or
    #    legacy flat dict; repo_ctx_for() handles both shapes.
    # 2. top-level "language" key (also persisted in context_patch)
    # 3. re-fetch from GitLab (cross-process resume: RQ worker → gateway, _runs cache miss)
    # 4. heuristic from file extensions (last resort)
    repo_ctx = repo_ctx_for(run, repo)
    language = repo_ctx.get("language") or ctx.get("language")
    if not language and repo:
        try:
            _fresh_map = _fetch_repo_context(repo)
            _fresh = _fresh_map.get(repo) or {}
            if _fresh.get("language"):
                repo_ctx = _fresh
                language = _fresh["language"]
                logger.info(f"SDLC {run_id}: re-fetched repo_ctx after cross-process HITL resume — lang={language}")
        except Exception as _fe:
            logger.warning(f"SDLC {run_id}: repo_ctx re-fetch failed: {_fe}")
    if not language:
        try:
            language = _detect_language(
                analysis.get("files_to_change", []) + analysis.get("new_files_needed", [])
            )
        except RuntimeError as _le:
            logger.error(f"SDLC {run_id}: language detection failed at design-approval resume: {_le}")
            update_run_state(run_id, "FAILED", error=f"Language detection failed: {_le}. Check repo file tree and re-trigger.")
            return

    try:
        # ── Start Coding State Machine ───────────────────────
        from agents.sdlc_state_machine import CodingStateMachine
        import os as _os
        _skip_tests_env = _os.getenv("SDLC_SKIP_TESTS", "").lower() in ("1", "true", "yes")
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
        repo_key = _nrik(repo) if repo else ""
        logger.info(f"Git Lab Repo - {repo} & Repo key - {repo_key}")
        _st_raw = ctx.get("skip_tests", _skip_tests_env)
        _ss_raw = ctx.get("skip_slt", False)
        # Resume-time override: engineer may explicitly set skip_tests when approving.
        # skip_tests_override=True bypasses TESTING+SLT; False re-enables them;
        # None (default) honours the stored context value.
        if skip_tests_override is not None:
            _resolved_skip_tests = bool(skip_tests_override)
            if _resolved_skip_tests != (_st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")):
                logger.info(
                    f"SDLC {run_id}: skip_tests overridden at design-approval resume "
                    f"(context={_st_raw!r} → override={_resolved_skip_tests})"
                )
                update_run_state(run_id, run.get("state", "APPROVED"),
                                 context_patch={"skip_tests": _resolved_skip_tests})
        else:
            _resolved_skip_tests = _st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")
        # POST-GATE: the human approved the real VERIFIED_DIFF produced pre-gate.
        # This run only APPLIES it deterministically (APPLYING → TEST_VERIFY →
        # SLT_RUNNING → COMMITTING → MR) — no LLM codegen happens after the gate.
        # PRE_CODING_BUILD already ran pre-gate, so it is NOT repeated here.
        machine = CodingStateMachine(
            run_id=run_id, jira_key=jira_key, repo=repo_key,
            language=language, design=design, analysis=analysis,
            base_branch=ctx.get("base_branch", ""),
            working_branch=ctx.get("working_branch", ""), gitlab_repo=repo,
            skip_tests=_resolved_skip_tests,
            skip_slt=(_ss_raw if isinstance(_ss_raw, bool) else str(_ss_raw).lower() in ("1", "true", "yes")),
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""),
            mode="postgate",
        )
        # Engineer's optional note left when APPROVING the design. On the post-gate
        # path it routes to the (recovery-only) coder if a red oracle forces recovery.
        _approval_fb = (feedback or ctx.get("design_feedback", "") or "").strip()
        if _approval_fb:
            machine._resume_feedback = _approval_fb
            logger.info(f"SDLC {run_id}: applying design-approval feedback to coder ({len(_approval_fb)} chars)")

        machine.run()  # APPLYING → … → COMMIT / re-gate / FAILED
        _post_governance_mr_note_if_present(run_id, repo)

    except SDLCCancelled:
        logger.info(f"SDLC {run_id}: coding phase stopped — run cancelled mid-pipeline")
    except Exception as e:
        logger.error(f"SDLC coding phase failed: run={run_id} → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Coding phase error: {e}")


# ============================================================
# BUG PIPELINE
# ============================================================

def run_bug_pipeline(issue: dict, run_id: Optional[str] = None) -> str:
    """
    Full CRED-equivalent bug triage + RCA + solution pipeline.
    Runs asynchronously.
    Returns run_id.
    """
    logger.info(f"TS- issue dictionary received - {issue}")
    summary    = issue.get("summary", issue.get("fields", {}).get("summary", "Unknown bug"))
    jira_key   = issue.get("key", "")
    repo       = issue.get("repo", "") or issue.get("fields", {}).get("customfield_repo", "") or ""
    user_id    = issue.get("triggered_by_user_id", "")
    user_email = issue.get("triggered_by_email", "")

    if run_id:
        run = get_run(run_id) or create_run(
            run_type="bug", jira_key=jira_key, jira_summary=summary,
            repo=repo, triggered_by="jira_webhook",
            created_by=user_id or "",
        )
        run_id = run["id"]
    else:
        run = create_run(
            run_type="bug", jira_key=jira_key, jira_summary=summary,
            repo=repo, triggered_by="jira_webhook",
            created_by=user_id or "",
        )
        run_id = run["id"]

    # Entry bail-check: a run cancelled while the job sat in the queue (e.g.
    # cancelled straight from CREATED) must not start. The cancel endpoint has
    # already freed the dedup slot; the worker's `finally` still releases the
    # per-reporter counter. Leave the state CANCELLED — do not run / do not FAIL.
    if run and run.get("state") in {"CANCELLED", "COMPLETE", "MERGED", "FAILED"}:
        logger.info(
            f"SDLC bug {run_id}: run already in terminal state "
            f"{run.get('state')!r} at worker pickup — skipping pipeline"
        )
        return run_id

    logger.info(f"SDLC bug pipeline started: run={run_id} jira={jira_key}")
    _pipeline_start = time.time()
    bind_context(pipeline_stage="sdlc_bug", task_id=jira_key, correlation_id=run_id)
    _bind_llm_run_context(run_id, "sdlc_bug")  # W-I-emit
    # Set user credentials in the execution context so all helpers in this
    # call chain automatically pick them up via _get_run_user().
    _cv_user_id.set(user_id)
    _cv_user_email.set(user_email)

    # Save the trigger payload to context on first entry so pre-SM resume paths
    # (resume_pre_sm_pipeline) can rebuild the worker payload exactly. Skipped
    # on resume re-entry.
    _resume_ctx = (run.get("context") if isinstance(run, dict) else {}) or {}
    if not _resume_ctx.get("original_issue"):
        try:
            patch_run_context(run_id, {"original_issue": dict(issue), "run_type": "bug"})
        except Exception as _ois_exc:
            logger.warning(f"SDLC bug {run_id}: could not save original_issue: {_ois_exc}")

    try:
        # WS-0: PREFLIGHT → BASELINE → NORMALIZE (GATE 1) → CLASSIFY (CLI, GATE 2)
        # → PLAN (CLI) — the SAME sequence as run_feature_pipeline (both share
        # CLASSIFYING → PLAN post-cutover). See _drive_pre_sm.
        driven = _drive_pre_sm(run_id, issue, jira_key, repo, "bug")
        if driven is None:
            return run_id
        return _finish_bug_pipeline(run_id, issue, jira_key, repo, summary, driven)

    except SDLCCancelled:
        logger.info(f"SDLC bug {run_id}: stopped — run cancelled mid-pipeline")
    except Exception as e:
        import traceback as _tb
        logger.error(f"SDLC bug pipeline failed: run={run_id} → {e}\n{_tb.format_exc()}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Bug pipeline error: {e}")
        _teams_notify(run_id, f"❌ **Bug Pipeline Failed** — `{jira_key}`\nError: {str(e)[:300]}")
    finally:
        clear_bound_context()
        logger.info(
            "sdlc_pipeline_complete",
            run_id=run_id,
            run_type="bug",
            jira_key=jira_key,
            total_duration_s=round(time.time() - _pipeline_start, 1),
        )
    return run_id


def _finish_bug_pipeline(run_id: str, issue: dict, jira_key: str, repo: str,
                          summary: str, driven: dict) -> str:
    """Post-PLAN tail for the bug pipeline: builds the bug-shaped `fix`/`rca`
    dicts from the unified PLAN, publishes Confluence + GitLab issue, runs
    pre-gate codegen, raises AWAITING_SOLUTION_APPROVAL. Shared by the initial
    trigger (run_bug_pipeline) and the WS-0 resume entrypoint
    (resume_pre_sm_pipeline) — unchanged from pre-gate-reorder behavior.
    `driven["classification"]` doubles as `triage` — the unified CLASSIFY CLI
    phase (WS-1) emits severity/category/affected_components for bug runs."""
    from agents.sdlc_pipeline._phases import _bug_analysis_from_fix, _pregate_codegen  # lazy: _core->_phases
    repo_resolved     = driven["repo_resolved"]
    detected_language = driven["detected_language"]
    triage            = driven["classification"]
    _plan = driven["plan"]

    # Build a bug-shaped fix dict from the unified plan so the ONE retained
    # AWAITING_SOLUTION_APPROVAL gate below (and the deterministic post-gate
    # resume resume_bug_after_solution_approval, which reconstructs its edit
    # list from fix["code_changes"]) stay byte-compatible with what the
    # pre-gate codegen used. Conservative choice: we carry files_to_change
    # into code_changes so the post-gate re-apply targets the SAME files the
    # pre-gate VERIFIED_DIFF was built from (a plain plan has no code_changes).
    _plan_ftc = [_s(p) for p in (_plan.get("files_to_change") or []) if _s(p).strip()]
    # Deny-list filter (retained): strip backup/archived/historical paths from
    # the EDIT list before it reaches the gate / state machine.
    _plan_ftc_kept, _plan_ftc_dropped = _filter_noneditable_files(_plan_ftc)
    if _plan_ftc_dropped:
        logger.warning(
            f"[SDLC {run_id}] Deny-list (bug): dropped {len(_plan_ftc_dropped)} "
            f"non-editable path(s) from files_to_change: {_plan_ftc_dropped}"
        )
    _plan_impl = _plan.get("implementation_spec") or _plan.get("solution_approach") or ""
    fix = {
        "fix_description":     _s(_plan.get("solution_approach", "")) or summary,
        "root_cause_analysis": _s(_plan_impl),
        "fix_approach":        _s(_plan.get("solution_approach", "")),
        "impact_analysis":     "",
        "code_changes":        [{"file": _f, "change": "MODIFY per plan"} for _f in _plan_ftc_kept],
        "new_files_needed":    [_s(pp) for pp in (_plan.get("new_files_needed") or [])],
        "regression_risk":     "Medium",
        "regression_explanation": "",
        "tests_to_add":        [],
        "verification_steps":  [],
        "followup_tasks":      [],
        "open_questions":      [],
        # Preserve the full plan so downstream code that expects feature-shape
        # keys (files_to_change / implementation_spec) still finds them.
        "files_to_change":     _plan_ftc_kept,
        "implementation_spec": _s(_plan_impl),
        "solution_approach":   _s(_plan.get("solution_approach", "")),
        "implementation_plan": _plan.get("implementation_plan") or [],
        "testing_strategy":    _s(_plan.get("testing_strategy", "")),
        "rollback_strategy":   _s(_plan.get("rollback_strategy", "")),
    }
    # RCA is now folded into the unified plan — keep a minimal dict so the gate
    # context / Confluence render without KeyErrors.
    rca = {
        "root_cause":     _s(_plan_impl),
        "code_path":      "",
        "affected_files": _plan_ftc_kept,
        "hypotheses":     [],
        "missing_test":   "",
    }
    # Persist plan-derived fix/rca so the post-gate resume path sees them.
    patch_run_context(run_id, {"fix": fix, "rca": rca, "triage": triage})
    _prune_test_files_for_skip(run_id, {}, fix)
    _fmt_fix = _fmt_bug_solution(fix)
    _event(run_id, "SOLUTIONING", "ai-solutioning-agent", json.dumps(_plan, default=str),
           {"stage": "solution", "structured": _fmt_fix, "unified_plan": True})

    # ── Gap-fix: eval_sdlc_solution (fire-and-forget) ────────────────────────
    # Grade the solution design for hallucinated file paths and security regressions
    # in a background thread — never blocks the pipeline.
    try:
        import threading as _sol_threading
        import json as _sol_json
        _sol_desc    = issue.get("summary", "") or issue.get("description", "")
        _sol_output  = _sol_json.dumps(_plan, default=str)
        _sol_run_id  = run_id
        _sol_ctx     = dict(repo_ctx)
        def _run_sol_eval():
            try:
                from core.evals import eval_engine as _ee
                _ee.eval_sdlc_solution(
                    ticket_description=_sol_desc[:600],
                    solution=_sol_output[:1500],
                    repo_ctx=_sol_ctx,
                    run_id=_sol_run_id,
                )
            except Exception as _se:
                logger.debug(f"SDLC {_sol_run_id}: eval_sdlc_solution failed (non-critical): {_se}")
        _sol_threading.Thread(
            target=_run_sol_eval, daemon=True, name="eval-sdlc-solution"
        ).start()
    except Exception:
        pass

    # ── Publish Confluence fix design (retained) ──
    confluence_url = _publish_confluence(
        title=f"[{jira_key}] {summary} — Bug Fix Design",
        body=_make_confluence_md_bug(summary, triage, rca, fix),
        repo_name=repo,
    )
    _jira_comment(jira_key,
                  f"[AI Solutioning Agent] Fix designed.\n\n{_fmt_fix}\n\nConfluence: {confluence_url}")
    _update_origin_inbox(run_id, confluence_url)

    # ── Create GitLab tracking issue (with full fix context) — retained ──
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_create_issue as _gci_b
        else:
            from tools.gitlab_tools import gitlab_create_issue as _gci_b
        import re as _re_gi_b
        if repo_resolved:
            _jira_gi_url_b = (get_run(run_id) or {}).get("context", {}).get("jira_url", "")
            _severity      = str(triage.get("severity") or "").strip()
            _components    = ", ".join(_s(x) for x in (triage.get("affected_components") or [])) or "—"
            _root_cause    = str(fix.get("root_cause_analysis") or rca.get("root_cause") or "").strip()
            _fix_approach  = str(fix.get("fix_approach") or fix.get("fix_description") or "").strip()
            _fix_files     = [c.get("file", "") for c in (fix.get("code_changes") or []) if isinstance(c, dict) and c.get("file")]
            _regression    = str(fix.get("regression_risk") or "").strip()
            _skip_b        = ("none", "n/a", "—", "-", "null", "{}")
            _parts_b = [
                "## Linked Jira Ticket",
                f"[{jira_key}]({_jira_gi_url_b})" if _jira_gi_url_b else jira_key,
                "",
                "## Bug Summary",
                summary,
                "",
            ]
            if _severity:
                _parts_b.append(f"**Severity:** {_severity}")
            if _components != "—":
                _parts_b += [f"**Affected Components:** {_components}", ""]
            if _fix_files:
                _parts_b += ["## Files to Change", ""]
                for _f in _fix_files[:12]:
                    _parts_b.append(f"- `{_f}`")
                _parts_b.append("")
            if _root_cause:
                _parts_b += ["## Root Cause Analysis", "", _root_cause, ""]
            if _fix_approach and _fix_approach.lower() not in _skip_b:
                _parts_b += ["## Fix Approach", "", _fix_approach, ""]
            if _regression and _regression.lower() not in _skip_b:
                _parts_b.append(f"**Regression Risk:** {_regression}")
            if confluence_url:
                _parts_b += ["", "## Fix Design Document", "", f"[View on Confluence]({confluence_url})", ""]
            _parts_b += [
                "---",
                "_Auto-created by AiNxt AI — fix in progress._",
                "_A PR will be linked to this issue upon completion._",
            ]
            _gl_str_b = _gci_b(repo=repo_resolved, title=f"[{jira_key}] {summary}", body="\n".join(_parts_b))
            logger.info(f"SDLC bug {run_id}: GitLab issue → {_gl_str_b}")
            _gi_m_b = _re_gi_b.search(r"(https://\S+/-/issues/\d+)", _gl_str_b)
            if _gi_m_b:
                update_run_state(run_id, "SOLUTIONING",
                                 context_patch={"gitlab_issue_url": _gi_m_b.group(1)})
    except Exception as _gh_ex_b:
        logger.warning(f"SDLC bug {run_id}: GitLab issue creation failed → {_gh_ex_b}")

    logger.info(f"[SDLC {run_id}] PLAN complete (bug) — proceeding to pre-gate codegen")

    # ── Shift-left: pre-gate codegen (the human approves the real diff) ──
    if not _pregate_codegen(
        run_id, jira_key, repo_resolved, detected_language,
        fix, _bug_analysis_from_fix(run_id, jira_key, fix, triage, summary),
        issue.get("base_branch", ""), issue.get("working_branch", ""),
        (get_run(run_id) or {}).get("context") or {}, run_type="bug",
    ):
        logger.warning(f"[SDLC {run_id}] pre-gate bug codegen did not produce an approvable diff — not advancing to gate")
        return run_id

    # ── HITL gate: engineer approves fix approach ────────
    _hitl_deadline = sdlc_gate_deadline("solution")
    logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="solution", hitl_deadline=_hitl_deadline)
    _transition(run_id, "AWAITING_SOLUTION_APPROVAL", "hitl-gate")
    update_run_state(run_id, "AWAITING_SOLUTION_APPROVAL",
                     context_patch={
                         "triage":         triage,
                         "rca":            rca,
                         "fix":            fix,
                         "confluence_url": confluence_url,
                         "hitl_deadline":  _hitl_deadline,
                         "base_branch":    issue.get("base_branch", ""),
                         "working_branch": issue.get("working_branch", ""),
                         # Multi-repo HITL payload — empty list for single-repo bug runs.
                         "repos":          _build_repos_payload(run_id, fix),
                     },
                     confluence_url=confluence_url)
    _inbox_notify(run_id, "solution_approval",
                  f"[{jira_key}] Bug fix design ready. Severity: {triage.get('severity','?')}.\n\n{_fmt_fix}\n\nConfluence: {confluence_url}",
                  {"jira_key": jira_key, "confluence_url": confluence_url, "severity": triage.get("severity","?")})
    _teams_notify(run_id, hitl=True, stage="AWAITING_SOLUTION_APPROVAL",
                  summary=f"[{jira_key}] Bug fix design ready. Severity: {triage.get('severity','?')}.\n\n{_fmt_fix[:600]}")

    logger.info(f"SDLC bug pipeline {run_id}: AWAITING_SOLUTION_APPROVAL")
    return run_id


def run_feature_revision(run_id: str, feedback: str) -> None:
    """
    Re-run the feature design phase with human revision feedback injected.
    Called after engineer clicks "Request Changes" at AWAITING_CODE_APPROVAL,
    and on a go-back/resume to ANALYZING/DESIGNING (resume_redo_analysis in ctx
    triggers an analyst re-run first).
    Returns run to AWAITING_CODE_APPROVAL after the revised design is ready.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_feature_revision")
    _bind_llm_run_context(run_id, "sdlc_feature_revision")  # W-I-emit
    from agents.sdlc_pipeline._phases import _pregate_codegen, _run_plan_phase  # lazy: _core->_phases
    run = get_run(run_id)
    if not run:
        return

    jira_key       = run["jira_key"]
    ctx            = run["context"] if isinstance(run["context"], dict) else {}
    analysis       = ctx.get("analysis", {})
    repo_ctx       = repo_ctx_for(run, run.get("repo", ""))
    revision_count = ctx.get("revision_count", 1)
    # Set by a go-back to ANALYZING; empty for a normal "request changes" at the
    # gate. Consume-once so a stale value from a prior go-back can't make a later
    # normal revision unexpectedly re-run the analyst.
    _resume_stage  = ctx.get("resume_stage", "")
    if _resume_stage:
        patch_run_context(run_id, {"resume_stage": ""})

    try:
        # HARD CUTOVER: revision re-runs the unified PLAN phase with the
        # engineer's corrective feedback injected as an AUTHORITATIVE user answer
        # (the PLAN prompt surfaces prior user_answers). The deleted analyst/
        # designer/solution-review re-run path is gone; _run_plan_phase does all
        # grounding + manifest validation internally and mirrors the plan into
        # run context as analysis/design.
        _rev_repo = _resolve_gitlab_repo(run.get("repo", ""))
        _rev_full_jira = ctx.get("full_jira") or _read_jira_full(jira_key)
        _rev_jira_desc = _jira_description(_rev_full_jira, fallback=ctx.get("description", ""))
        _rev_language = repo_ctx.get("language") or ctx.get("language") or ""
        _rev_issue = {
            "summary":        run.get("jira_summary", ""),
            "description":    _rev_jira_desc,
            "key":            jira_key,
            "repo":           run.get("repo", ""),
            "base_branch":    ctx.get("base_branch", ""),
            "working_branch": ctx.get("working_branch", ""),
        }
        # Stash the feedback as an authoritative answer BEFORE the PLAN call so the
        # planner honours it (append to any existing answers rather than clobber).
        if (feedback or "").strip():
            _existing_ans = list(ctx.get("user_answers") or [])
            _existing_ans.append({"question": "Engineer requested changes", "answer": feedback})
            patch_run_context(run_id, {"user_answers": _existing_ans})
        _rev_ctx = (get_run(run_id) or {}).get("context") or {}
        _plan = _run_plan_phase(
            run_id, jira_key, _rev_repo, _rev_language,
            _rev_issue, _rev_ctx, run_type="feature",
        )
        if _plan is None:
            return  # PLAN already suspended / awaiting input inside _run_plan_phase
        analysis = _plan
        design   = _plan

        _prune_test_files_for_skip(run_id, analysis, design)
        _fmt_sol = _fmt_solution(design, analysis)
        _event(run_id, "DESIGNING", "ai-solution-agent", json.dumps(_plan, default=str),
               {"stage": "design", "structured": _fmt_sol, "revision": revision_count})

        # Update Confluence page (update existing rather than creating new)
        confluence_url = ctx.get("confluence_url", "")
        if confluence_url:
            try:
                page_id = confluence_url.split("/")[-1]
                from tools.confluence_tools import confluence_update_page
                _uid, _uemail = _get_run_user()
                confluence_update_page(
                    page_id=page_id,
                    title=f"[{jira_key}] {run.get('jira_summary', '')} — Solution Design (Rev {revision_count})",
                    body=_make_confluence_md_feature(run.get("jira_summary", ""), ctx.get("classification", {}), analysis, design),
                    user_id=_uid, user_email=_uemail,
                )
            except Exception as _cf_err:
                logger.warning(f"run_feature_revision: confluence update failed — {_cf_err}")
                # Re-publish if update failed
                confluence_url = _publish_confluence(
                    title=f"[{jira_key}] {run.get('jira_summary', '')} — Solution Design (Rev {revision_count})",
                    body=_make_confluence_md_feature(run.get("jira_summary", ""), ctx.get("classification", {}), analysis, design),
                    repo_name=run.get("repo", ""),
                )
        else:
            confluence_url = _publish_confluence(
                title=f"[{jira_key}] {run.get('jira_summary', '')} — Solution Design (Rev {revision_count})",
                body=_make_confluence_md_feature(run.get("jira_summary", ""), ctx.get("classification", {}), analysis, design),
                repo_name=run.get("repo", ""),
            )

        _jira_comment(jira_key,
                      f"[AiNxt] Revision #{revision_count} design ready.\n\n{_fmt_sol}\n\nConfluence: {confluence_url}")


        # ── Shift-left: pre-gate codegen on the revised design ──
        if not _pregate_codegen(
            run_id, jira_key, _resolve_gitlab_repo(run.get("repo", "")),
            repo_ctx.get("language") or ctx.get("language"),
            design, analysis,
            ctx.get("base_branch", ""), ctx.get("working_branch", ""),
            (get_run(run_id) or {}).get("context") or {}, run_type="feature",
        ):
            logger.warning(f"[SDLC {run_id}] pre-gate revision codegen did not produce an approvable diff — not advancing to gate")
            return

        _revgate_deadline = sdlc_gate_deadline("code")
        logger.info("[SDLC] gate entered", run_id=run_id, gate_kind="code", hitl_deadline=_revgate_deadline)
        _transition(run_id, "AWAITING_CODE_APPROVAL", "hitl-gate")
        update_run_state(run_id, "AWAITING_CODE_APPROVAL",
                         context_patch={
                             "classification": ctx.get("classification", {}),
                             "analysis":       analysis,
                             "design":         design,
                             "confluence_url": confluence_url,
                             "hitl_deadline":  _revgate_deadline,
                             "repos":          _build_repos_payload(run_id, design),
                         },
                         confluence_url=confluence_url)

        _inbox_notify(run_id, "design_approval",
                      f"[{jira_key}] Revised design (#{revision_count}) ready for approval.\n\n{_fmt_sol}",
                      {"jira_key": jira_key, "confluence_url": confluence_url, "revision": revision_count})
        _teams_notify(run_id, hitl=True, stage="AWAITING_CODE_APPROVAL",
                      summary=f"[{jira_key}] Revised design (Rev #{revision_count}) ready for approval.\n\n{_fmt_sol[:600]}")

        logger.info(f"SDLC run {run_id}: revision #{revision_count} done → AWAITING_CODE_APPROVAL")

    except Exception as e:
        logger.error(f"run_feature_revision: run={run_id} → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Revision #{revision_count} failed: {e}")


def run_bug_revision(run_id: str, feedback: str) -> None:
    """
    Re-run the bug solution phase with human revision feedback injected.
    Called after engineer clicks "Request Changes" at AWAITING_SOLUTION_APPROVAL.
    Returns run to AWAITING_SOLUTION_APPROVAL after the revised fix is ready.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_bug_revision")
    _bind_llm_run_context(run_id, "sdlc_bug_revision")  # W-I-emit
    from agents.sdlc_pipeline._phases import _bug_analysis_from_fix, _pregate_codegen, _run_plan_phase  # lazy: _core->_phases
    run = get_run(run_id)
    if not run:
        return

    jira_key       = run["jira_key"]
    ctx            = run["context"] if isinstance(run["context"], dict) else {}
    rca            = ctx.get("rca", {})
    triage         = ctx.get("triage", {})
    repo_ctx       = repo_ctx_for(run, run.get("repo", ""))
    revision_count = ctx.get("revision_count", 1)
    # Set by a go-back to TROUBLESHOOTING; empty for a normal "request changes"
    # at the gate. Consume-once so a stale value can't make a later normal
    # revision unexpectedly re-run the RCA.
    _resume_stage  = ctx.get("resume_stage", "")
    if _resume_stage:
        patch_run_context(run_id, {"resume_stage": ""})

    try:
        # HARD CUTOVER: bug revision re-runs the unified PLAN phase with the
        # engineer's corrective feedback injected as an AUTHORITATIVE user answer.
        # The deleted RCA/solutioning _llm_react + _revise_rca re-run path is gone;
        # _run_plan_phase does all grounding + manifest validation internally and
        # mirrors the plan into run context as analysis/design.
        _bugrev_repo = _resolve_gitlab_repo(run.get("repo", ""))
        _bugrev_full_jira = ctx.get("full_jira") or _read_jira_full(jira_key)
        _bugrev_jira_desc = _jira_description(_bugrev_full_jira, fallback=ctx.get("description", ""))
        _bugrev_language = repo_ctx.get("language") or ctx.get("language") or ""
        _bugrev_issue = {
            "summary":        run.get("jira_summary", ""),
            "description":    _bugrev_jira_desc,
            "key":            jira_key,
            "repo":           run.get("repo", ""),
            "base_branch":    ctx.get("base_branch", ""),
            "working_branch": ctx.get("working_branch", ""),
        }
        if (feedback or "").strip():
            _existing_ans = list(ctx.get("user_answers") or [])
            _existing_ans.append({"question": "Engineer requested changes", "answer": feedback})
            patch_run_context(run_id, {"user_answers": _existing_ans})
        _bugrev_ctx = (get_run(run_id) or {}).get("context") or {}
        _plan = _run_plan_phase(
            run_id, jira_key, _bugrev_repo, _bugrev_language,
            _bugrev_issue, _bugrev_ctx, run_type="bug",
        )
        if _plan is None:
            return  # PLAN already suspended / awaiting input inside _run_plan_phase
        # Build a bug-shaped fix dict from the unified plan (mirrors run_bug_pipeline)
        # so the retained gate + post-gate resume stay byte-compatible.
        _plan_ftc = [_s(pp) for pp in (_plan.get("files_to_change") or []) if _s(pp).strip()]
        _plan_ftc_kept, _plan_ftc_dropped = _filter_noneditable_files(_plan_ftc)
        if _plan_ftc_dropped:
            logger.warning(
                f"[SDLC {run_id}] Deny-list (bug revision): dropped {len(_plan_ftc_dropped)} "
                f"non-editable path(s): {_plan_ftc_dropped}"
            )
        _plan_impl = _plan.get("implementation_spec") or _plan.get("solution_approach") or ""
        fix = {
            "fix_description":     _s(_plan.get("solution_approach", "")) or run.get("jira_summary", ""),
            "root_cause_analysis": _s(_plan_impl),
            "fix_approach":        _s(_plan.get("solution_approach", "")),
            "impact_analysis":     "",
            "code_changes":        [{"file": _f, "change": "MODIFY per plan"} for _f in _plan_ftc_kept],
            "new_files_needed":    [_s(pp) for pp in (_plan.get("new_files_needed") or [])],
            "regression_risk":     "Medium",
            "regression_explanation": "",
            "tests_to_add":        [],
            "verification_steps":  [],
            "followup_tasks":      [],
            "open_questions":      [],
            "files_to_change":     _plan_ftc_kept,
            "implementation_spec": _s(_plan_impl),
            "solution_approach":   _s(_plan.get("solution_approach", "")),
            "implementation_plan": _plan.get("implementation_plan") or [],
            "testing_strategy":    _s(_plan.get("testing_strategy", "")),
            "rollback_strategy":   _s(_plan.get("rollback_strategy", "")),
        }
        rca = {
            "root_cause":     _s(_plan_impl),
            "code_path":      "",
            "affected_files": _plan_ftc_kept,
            "hypotheses":     [],
            "missing_test":   "",
        }
        patch_run_context(run_id, {"fix": fix, "rca": rca})
        _prune_test_files_for_skip(run_id, {}, fix)
        _fmt_fix = _fmt_bug_solution(fix)
        _event(run_id, "SOLUTIONING", "ai-solutioning-agent", json.dumps(_plan, default=str),
               {"stage": "solution", "structured": _fmt_fix, "revision": revision_count, "unified_plan": True})

        # ── Gap-fix: eval_sdlc_solution on revision path (fire-and-forget) ───
        try:
            import threading as _solr_threading
            import json as _solr_json
            _solr_desc   = issue.get("summary", "") or issue.get("description", "")
            _solr_output = _solr_json.dumps(_plan, default=str)
            _solr_run_id = run_id
            _solr_ctx    = dict(repo_ctx)
            def _run_solr_eval():
                try:
                    from core.evals import eval_engine as _ee
                    _ee.eval_sdlc_solution(
                        ticket_description=_solr_desc[:600],
                        solution=_solr_output[:1500],
                        repo_ctx=_solr_ctx,
                        run_id=_solr_run_id,
                    )
                except Exception as _se:
                    logger.debug(f"SDLC {_solr_run_id}: eval_sdlc_solution (revision) failed (non-critical): {_se}")
            _solr_threading.Thread(
                target=_run_solr_eval, daemon=True, name="eval-sdlc-solution-rev"
            ).start()
        except Exception:
            pass

        confluence_url = ctx.get("confluence_url", "")
        if confluence_url:
            try:
                page_id = confluence_url.split("/")[-1]
                from tools.confluence_tools import confluence_update_page
                _uid, _uemail = _get_run_user()
                confluence_update_page(
                    page_id=page_id,
                    title=f"[{jira_key}] {run.get('jira_summary', '')} — Bug Fix Design (Rev {revision_count})",
                    body=_make_confluence_md_bug(run.get("jira_summary", ""), triage, rca, fix),
                    user_id=_uid, user_email=_uemail,
                )
            except Exception as _cf_err:
                logger.warning(f"run_bug_revision: confluence update failed — {_cf_err}")
                confluence_url = _publish_confluence(
                    title=f"[{jira_key}] {run.get('jira_summary', '')} — Bug Fix Design (Rev {revision_count})",
                    body=_make_confluence_md_bug(run.get("jira_summary", ""), triage, rca, fix),
                    repo_name=run.get("repo", ""),
                )
        else:
            confluence_url = _publish_confluence(
                title=f"[{jira_key}] {run.get('jira_summary', '')} — Bug Fix Design (Rev {revision_count})",
                body=_make_confluence_md_bug(run.get("jira_summary", ""), triage, rca, fix),
                repo_name=run.get("repo", ""),
            )

        _jira_comment(jira_key,
                      f"[AiNxt] Revision #{revision_count} fix design ready.\n\n{_fmt_fix}\n\nConfluence: {confluence_url}")

        # ── Shift-left: pre-gate codegen on the revised bug fix ──
        if not _pregate_codegen(
            run_id, jira_key, _resolve_gitlab_repo(run.get("repo", "")),
            repo_ctx.get("language") or ctx.get("language"),
            fix, _bug_analysis_from_fix(run_id, jira_key, fix, triage, run.get("jira_summary", "")),
            ctx.get("base_branch", ""), ctx.get("working_branch", ""),
            (get_run(run_id) or {}).get("context") or {}, run_type="bug",
        ):
            logger.warning(f"[SDLC {run_id}] pre-gate bug-revision codegen did not produce an approvable diff — not advancing to gate")
            return

        _transition(run_id, "AWAITING_SOLUTION_APPROVAL", "hitl-gate")
        update_run_state(run_id, "AWAITING_SOLUTION_APPROVAL",
                         context_patch={
                             "triage":         triage,
                             "rca":            rca,
                             "fix":            fix,
                             "confluence_url": confluence_url,
                             "repos":          _build_repos_payload(run_id, fix),
                         },
                         confluence_url=confluence_url)

        _inbox_notify(run_id, "solution_approval",
                      f"[{jira_key}] Revised fix design (#{revision_count}) ready for approval.\n\n{_fmt_fix}",
                      {"jira_key": jira_key, "confluence_url": confluence_url,
                       "severity": triage.get("severity", "?"), "revision": revision_count})
        _teams_notify(run_id, hitl=True, stage="AWAITING_SOLUTION_APPROVAL",
                      summary=f"[{jira_key}] Revised fix design (Rev #{revision_count}) ready for approval.\n\n{_fmt_fix[:600]}")

        logger.info(f"SDLC bug run {run_id}: revision #{revision_count} done → AWAITING_SOLUTION_APPROVAL")

    except Exception as e:
        logger.error(f"run_bug_revision: run={run_id} → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Bug revision #{revision_count} failed: {e}")


def resume_bug_after_solution_approval(run_id: str, feedback: str = "",
                                        skip_tests_override: bool = None) -> None:
    """After engineer approves the fix, kick off coding state machine.

    ``skip_tests_override`` — when explicitly True/False, overrides the value stored in
    the run context, letting the engineer decide at resume time whether tests+SLT are
    skipped.  None = honour the context value (backward-compatible default).
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_bug_resume")
    _bind_llm_run_context(run_id, "sdlc_bug_resume")  # W-I-emit
    from agents.sdlc_pipeline._phases import _post_governance_mr_note_if_present  # lazy: _core->_phases
    import os
    from store.sdlc_store import get_run_for_hitl
    run = get_run_for_hitl(run_id, required_keys=["fix", "triage"])
    if not run:
        return
    jira_key = run["jira_key"]
    repo     = _resolve_gitlab_repo(run["repo"])
    ctx      = run["context"] if isinstance(run["context"], dict) else {}

    # Enforce HITL deadline
    import time as _time
    _hitl_deadline = ctx.get("hitl_deadline")
    if _hitl_deadline and int(_time.time()) > int(_hitl_deadline):
        logger.error(f"SDLC {run_id}: HITL solution-approval deadline expired — marking FAILED")
        update_run_state(run_id, "FAILED", error="HITL solution approval deadline expired (48h window passed without action)")
        _jira_comment(run["jira_key"], "[AiNxt AI] ❌ Solution approval window expired (48h). Please re-trigger the pipeline.")
        return

    fix      = ctx.get("fix", {})
    repo_ctx = repo_ctx_for(run, repo)
    triage   = ctx.get("triage", {})
    language = repo_ctx.get("language") or ctx.get("language")
    if not language and repo:
        try:
            _fresh_map = _fetch_repo_context(repo)
            _fresh = _fresh_map.get(repo) or {}
            if _fresh.get("language"):
                repo_ctx = _fresh
                language = _fresh["language"]
                logger.info(f"SDLC {run_id}: re-fetched repo_ctx after cross-process HITL resume — lang={language}")
        except Exception as _fe:
            logger.warning(f"SDLC {run_id}: repo_ctx re-fetch failed: {_fe}")
    if not language:
        try:
            language = _detect_language(
                [c.get("file", "") for c in fix.get("code_changes", []) if isinstance(c, dict)]
            )
        except RuntimeError as _le:
            logger.error(f"SDLC {run_id}: language detection failed at solution-approval resume: {_le}")
            update_run_state(run_id, "FAILED", error=f"Language detection failed: {_le}. Check repo file tree and re-trigger.")
            return

    # Primary: use the engineer-approved code_changes from the fix.
    # Fallback to triage affected_components only if the fix has no code_changes
    # (shouldn't happen, but guards against empty LLM output).
    _fix_files = [c.get("file", "") for c in fix.get("code_changes", [])
                  if isinstance(c, dict) and c.get("file")]
    _jira_summary = run.get("jira_summary", "")
    # W-F (C2): deny-list filter on the bug-pipeline EDIT list before it reaches
    # the state machine. new_files_needed stays empty here so it is unaffected.
    _bug_ftc = _fix_files or triage.get("affected_components", [])
    _bug_ftc_kept, _bug_ftc_dropped = _filter_noneditable_files(_bug_ftc)
    if _bug_ftc_dropped:
        logger.warning(
            f"[SDLC {run_id}] Deny-list (bug): dropped {len(_bug_ftc_dropped)} "
            f"non-editable path(s) from files_to_change: {_bug_ftc_dropped}"
        )
    analysis = {
        "files_to_change":   _bug_ftc_kept,
        "new_files_needed":  [],
        "requirements":      fix.get("fix_description", "") or _jira_summary,
        "problem_statement": (
            f"Bug {jira_key}: {_jira_summary}\n"
            f"Fix: {fix.get('fix_description', '')}\n"
            f"Root cause: {fix.get('root_cause_analysis', '')}"
        ).strip(),
        "root_cause":        fix.get("root_cause_analysis", ""),
    }

    try:
        from agents.sdlc_state_machine import CodingStateMachine
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
        import os as _os
        _skip_tests_env = _os.getenv("SDLC_SKIP_TESTS", "").lower() in ("1", "true", "yes")
        logger.info(f"Skip test variable - {_skip_tests_env}")
        repo_name = _nrik(repo) if repo else ""
        logger.info(f"Git Lab Repo - {repo} & Repo key - {repo_name}")
        _st_raw = ctx.get("skip_tests", _skip_tests_env)
        _ss_raw = ctx.get("skip_slt", False)
        # Resume-time override: engineer may explicitly set skip_tests when approving.
        # skip_tests_override=True bypasses TESTING+SLT; False re-enables them;
        # None (default) honours the stored context value.
        if skip_tests_override is not None:
            _resolved_skip_tests = bool(skip_tests_override)
            if _resolved_skip_tests != (_st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")):
                logger.info(
                    f"SDLC {run_id}: skip_tests overridden at solution-approval resume "
                    f"(context={_st_raw!r} → override={_resolved_skip_tests})"
                )
                update_run_state(run_id, run.get("state", "APPROVED"),
                                 context_patch={"skip_tests": _resolved_skip_tests})
        else:
            _resolved_skip_tests = _st_raw if isinstance(_st_raw, bool) else str(_st_raw).lower() in ("1", "true", "yes")
        # POST-GATE: the human approved the real VERIFIED_DIFF produced pre-gate.
        # This run only APPLIES it deterministically — no LLM codegen after the gate.
        # PRE_CODING_BUILD already ran pre-gate, so it is NOT repeated here.
        machine = CodingStateMachine(
            run_id=run_id, jira_key=jira_key, repo=repo_name,
            language=language, design=fix, analysis=analysis,
            base_branch=ctx.get("base_branch", ""),
            working_branch=ctx.get("working_branch", ""), gitlab_repo=repo,
            skip_tests=_resolved_skip_tests,
            skip_slt=(_ss_raw if isinstance(_ss_raw, bool) else str(_ss_raw).lower() in ("1", "true", "yes")),
            compile_skipped=bool(ctx.get("compile_skipped", False)),
            user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""),
            mode="postgate",
        )
        # Engineer's optional note left when APPROVING the fix — routes to the
        # (recovery-only) coder if a red oracle forces post-gate recovery.
        _approval_fb = (feedback or ctx.get("solution_feedback", "") or "").strip()
        if _approval_fb:
            machine._resume_feedback = _approval_fb
            logger.info(f"SDLC {run_id}: applying solution-approval feedback to coder ({len(_approval_fb)} chars)")

        machine.run()  # APPLYING → … → COMMIT / re-gate / FAILED
        _post_governance_mr_note_if_present(run_id, repo)
    except SDLCCancelled:
        logger.info(f"SDLC {run_id}: bug coding phase stopped — run cancelled mid-pipeline")
    except Exception as e:
        logger.error(f"SDLC bug coding phase failed: run={run_id} → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
        _jira_comment(jira_key, f"[AiNxt AI] Bug coding phase error: {e}")


# ── PR Review Pipeline ────────────────────────────────────────

def run_pr_review_pipeline(pr: dict, run_id: Optional[str] = None) -> str:
    """
    Triggered by GitLab MR opened webhook.
    Runs AI MR Reviewer, posts review comments.
    """
    repo      = pr.get("repo", "") or pr.get("base", {}).get("repo", {}).get("full_name", "")
    pr_number = pr.get("number", 0)
    jira_key  = _extract_jira_from_pr(pr)

    if run_id:
        run = get_run(run_id) or create_run(
            run_type="pr_review", jira_key=jira_key, jira_summary=pr.get("title", ""),
            repo=repo, triggered_by="gitlab_webhook",
        )
        run_id = run["id"]
    else:
        run = create_run(
            run_type="pr_review", jira_key=jira_key, jira_summary=pr.get("title", ""),
            repo=repo, triggered_by="gitlab_webhook",
        )
        run_id = run["id"]

    bind_context(correlation_id=run_id, pipeline_stage="sdlc_pr_review")
    _bind_llm_run_context(run_id, "sdlc_pr_review")  # W-I-emit
    try:
        _transition(run_id, "PR_REVIEWING", "ai-pr-reviewer")

        # ── Merge conflict detection ───────────────────────────────────────────
        _pr_branch = pr.get("head", {}).get("ref", "") or pr.get("branch", "")
        _pr_base   = pr.get("base", {}).get("ref", "main") or "main"
        _gl_url    = os.getenv("GITLAB_URL", "https://gitlab.com").rstrip("/")
        _repo_url  = pr.get("url", "") or f"{_gl_url}/{repo}"
        if _detect_merge_conflict(_repo_url, _pr_branch, _pr_base):
            _conflict_ctx = (
                f"Repository: {repo}\n"
                f"Branch: {_pr_branch} → {_pr_base}\n"
                f"PR #{pr_number}: {pr.get('title', '')}\n"
            )
            _resolution = _generate_conflict_resolution(_conflict_ctx)
            _transition(run_id, "MERGE_CONFLICT", "conflict-detector")
            update_run_state(run_id, "MERGE_CONFLICT",
                             pr_number=pr_number,
                             pr_url=pr.get("html_url", ""),
                             conflict_resolution=_resolution)
            _inbox_notify(run_id, "merge_conflict",
                          f"**Merge Conflict Detected** — PR #{pr_number} in `{repo}`\n\n"
                          f"Branch `{_pr_branch}` cannot be merged into `{_pr_base}`.\n\n"
                          f"**AI Resolution Proposal**\n{_resolution[:800]}",
                          {"repo": repo, "pr_number": pr_number, "branch": _pr_branch})
            _teams_notify(run_id, hitl=True, stage="MERGE_CONFLICT",
                          summary=f"⚠️ Merge conflict on PR #{pr_number} in `{repo}`.")
            return run_id

        # Fetch PR diff
        pr_diff = _get_pr_diff(repo, pr_number)

        # Fetch repo_ctx for tech stack context
        try:
            _pr_repo_ctx_map = _fetch_repo_context(repo)
            _pr_repo_ctx = _pr_repo_ctx_map.get(repo) or {}
        except Exception:
            _pr_repo_ctx = {}

        # Review loop (up to 2 iterations)
        for iteration in range(1, 3):
            review_raw = _run_sdlc_agent("sdlc-pr-reviewer", (
                f"You are a senior engineer reviewing PR #{pr_number} in {repo!r}.\n"
                f"Jira: {jira_key!r}. Tech stack: {_pr_repo_ctx.get('tech_stack', 'unknown')}.\n\n"
                f"MR diff (full file content available via gitlab_read_file if needed):\n"
                f"{pr_diff[:4000]}\n\n"
                f"Review each changed file carefully. For every file, list specific issues and suggestions.\n\n"
                f"Output ONLY valid JSON (no markdown fences):\n"
                f'{{\n'
                f'  "approved": true or false,\n'
                f'  "score": 0-10,\n'
                f'  "summary": "one paragraph overall verdict",\n'
                f'  "blocking_issues": ["concise blocking issue (not file-specific)"],\n'
                f'  "security_flags": ["security concern"],\n'
                f'  "suggestions": ["general improvement"],\n'
                f'  "file_reviews": [\n'
                f'    {{\n'
                f'      "file": "exact/path/to/file.ext",\n'
                f'      "issues": ["specific bug or error in this file"],\n'
                f'      "suggestions": ["improvement for this file"]\n'
                f'    }}\n'
                f'  ]\n'
                f'}}'
            ))
            review     = _parse_json(review_raw)
            _event(run_id, "PR_REVIEWING", "ai-pr-reviewer", review_raw,
                   {"iteration": iteration})

            # Post review comment to GitLab MR
            _post_pr_review_comment(repo, pr_number, review, iteration)

            if review.get("approved"):
                break

            logger.info(f"SDLC PR review {run_id}: iteration {iteration} not approved, waiting for push")
            # In production: wait for push event; here we exit loop for simplicity

        # HITL gate
        _transition(run_id, "AWAITING_PR_APPROVAL", "hitl-gate")
        update_run_state(run_id, "AWAITING_PR_APPROVAL",
                         pr_number=pr_number,
                         pr_url=pr.get("html_url", ""))
        # Build rich PR review body for inbox
        _score          = review.get("score", "?")
        _approved_lbl   = "✅ Approved" if review.get("approved") else "⚠️ Needs Changes"
        _summary        = review.get("summary", "")
        _blocking       = review.get("blocking_issues") or []
        _suggestions    = review.get("suggestions") or []
        _sec_flags      = review.get("security_flags") or []

        _blocking_md    = "\n".join(f"  • {i}" for i in _blocking[:5]) if _blocking else "  None"
        _suggest_md     = "\n".join(f"  • {s}" for s in _suggestions[:5]) if _suggestions else "  None"
        _sec_md         = "\n".join(f"  • {f}" for f in _sec_flags[:3]) if _sec_flags else "  None"

        _pr_body = (
            f"**PR #{pr_number}** in `{repo}` | Jira: {jira_key}\n"
            f"Status: {_approved_lbl} | Score: {_score}/10\n\n"
            f"**Summary**\n{_summary}\n\n"
            f"**Blocking Issues**\n{_blocking_md}\n\n"
            f"**Suggestions**\n{_suggest_md}\n\n"
            f"**Security Flags**\n{_sec_md}\n\n"
            f"_Review complete — awaiting engineer sign-off._"
        )
        _inbox_notify(run_id, "pr_approval", _pr_body,
                      {"repo": repo, "pr_number": pr_number, "jira_key": jira_key,
                       "score": _score, "approved": review.get("approved", False)})
        _teams_notify(run_id, hitl=True, stage="AWAITING_PR_APPROVAL",
                      summary=_pr_body[:600])

        return run_id

    except SDLCCancelled:
        logger.info(f"SDLC PR review {run_id}: stopped — run cancelled mid-pipeline")
        return run_id
    except Exception as e:
        logger.error(f"SDLC PR review failed: run={run_id} → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
        _teams_notify(run_id, f"❌ **PR Review Failed** — `{repo}`\nError: {str(e)[:300]}")
        return run_id


def resume_after_pr_approval(run_id: str) -> None:
    """Mark run as COMPLETE after engineer approves the PR."""
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_pr_approval_resume")
    _bind_llm_run_context(run_id, "sdlc_pr_approval_resume")  # W-I-emit
    update_run_state(run_id, "COMPLETE", current_stage="merged")
    run = get_run(run_id)
    if run:
        jira_key = run.get("jira_key", "")
        ctx = run.get("context") or {}
        _jira_comment(jira_key, "[AiNxt AI] PR approved. Pipeline complete.",
                      user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""))
        _teams_notify(run_id,
                      f"✅ **Pipeline Complete** — `{jira_key}`\n"
                      f"Run `{run_id[:8]}` finished successfully. PR approved and merged.")


def address_pr_review_comments(run_id: str) -> str:
    """
    Called by the job queue when a reviewer requests changes on a AiNxt PR.

    Restores the CodingStateMachine context from the SDLC run and delegates
    to _phase_address_comments(), which:
      1. Fetches all open review comments from GitLab MR
      2. Asks AI Coding Agent to generate fixes
      3. Commits the fixes to the same branch
      4. Posts a reply comment on the PR
      5. Transitions run → AWAITING_RE_REVIEW
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_pr_comments")
    _bind_llm_run_context(run_id, "sdlc_pr_comments")  # W-I-emit
    run = get_run(run_id)
    if not run:
        logger.error(f"address_pr_review_comments: run {run_id} not found")
        return run_id

    ctx      = run.get("context", {}) or {}
    jira_key = run.get("jira_key", "UNKNOWN")
    repo     = _resolve_gitlab_repo(run.get("repo", ""))
    design   = ctx.get("design", {}) or {}
    analysis = ctx.get("analysis", {}) or {}
    # Use real repo language if available, fallback to file extension heuristic
    _repo_ctx = repo_ctx_for(run, repo)
    _files    = analysis.get("files_to_change", []) + analysis.get("new_files_needed", [])
    language  = _repo_ctx.get("language") or _detect_language(_files)

    # Restore state machine in headless mode (no re-run of full pipeline)
    from agents.sdlc_state_machine import CodingStateMachine
    from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
    repo_key = _nrik(repo) if repo else ""
    logger.info(f"Git Lab Repo - {repo} & Repo key - {repo_key}")
    sm = CodingStateMachine(
        run_id=run_id,
        jira_key=jira_key,
        repo=repo_key,
        language=language,
        design=design,
        analysis=analysis,
        base_branch=ctx.get("base_branch", ""),
        working_branch=ctx.get("working_branch", ""),
        gitlab_repo=repo,
        skip_tests=bool(ctx.get("skip_tests", False)),
        skip_slt=bool(ctx.get("skip_slt", False)),
        compile_skipped=bool(ctx.get("compile_skipped", False)),
        user_id=ctx.get("user_id", ""), user_email=ctx.get("user_email", ""),
    )
    # code_output / slt_output may not be persisted; gracefully default to empty
    sm.code_output = ctx.get("code_output", {}) or {}
    sm.slt_output  = ctx.get("slt_output", {}) or {}

    try:
        sm._phase_address_comments()
    except SDLCCancelled:
        logger.info(f"address_pr_review_comments {run_id}: stopped — run cancelled")
    except Exception as e:
        logger.error(f"address_pr_review_comments: phase failed → {e}")
        update_run_state(run_id, "FAILED", error=str(e))
    finally:
        # Cleanup per-run workspace if _phase_address_comments materialized one.
        try:
            sm._cleanup_run_workspace()
        except Exception:
            pass

    return run_id


# ============================================================
# SOLUTION REVIEW LOOP  (CRED-style: reviewer critiques designer)
# ============================================================

def _transition(run_id: str, to_state: str, actor: str):
    run        = get_run(run_id)
    from_state = run["state"] if run else "UNKNOWN"
    # In-flight cancellation: if an operator cancelled this run mid-pipeline, the
    # cancel endpoint already persisted state=CANCELLED. Stop here instead of
    # overwriting it and marching on toward an MR. Raised before the state update
    # so CANCELLED is preserved; pipeline functions catch SDLCCancelled.
    if from_state == "CANCELLED":
        from store.sdlc_store import SDLCCancelled
        raise SDLCCancelled(run_id)
    update_run_state(run_id, to_state, current_stage=to_state)
    add_run_event(run_id, from_state, to_state, stage=to_state, actor=actor)


def _event(run_id: str, stage: str, actor: str, output: str, data: dict = None):
    run = get_run(run_id)
    add_run_event(run_id, run["state"] if run else stage, stage,
                  stage=stage, actor=actor, output=output, data=data or {})


def _build_repos_payload(run_id: str, design: dict) -> list:
    """
    Build the per-repo `repos[]` payload included in HITL approval context.

    Read by `MultiRepoApprovalView` in the UI to render one card per editable
    repo + a compile-only footer. Returns an empty list when multi-repo is off
    or `sdlc_run_repos` has at most one row (the primary) — single-repo runs
    keep the legacy approval shape unchanged and the UI's empty-state branch
    fires (no card section rendered).

    Per-repo plan slicing is intentionally NOT done by the LLM today — the
    design phase produces one unified design covering all repos in scope. We
    surface the same plan text on the primary + editable cards so the
    approver sees what's planned; compile-only cards carry no plan since they
    are not being modified.
    """
    try:
        from store.sdlc_store import list_run_repos
    except Exception:
        return []
    rows = list_run_repos(run_id) or []
    if len(rows) <= 1:
        return []

    plan_text = _extract_plan_for_payload(design)
    files     = _extract_files_list(design)

    out: list = []
    for row in rows:
        kind = row.get("kind", "")
        entry = {
            "repo":                   row.get("repo"),
            "ref":                    row.get("ref"),
            "kind":                   kind,
            "per_repo_plan":          None,
            "files_likely_to_change": [],
        }
        if kind in ("primary", "editable"):
            entry["per_repo_plan"]          = plan_text or None
            entry["files_likely_to_change"] = list(files)
        out.append(entry)
    return out


def _extract_plan_for_payload(design: dict) -> str:
    """Derive a short plan summary from the design for the HITL approval view."""
    if not isinstance(design, dict):
        return ""
    for k in ("solution_approach", "fix_description", "implementation_plan", "approach"):
        v = design.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v:
            return "\n".join(_s(x) for x in v if x)
    return ""


def _extract_files_list(design: dict) -> list:
    """Pull a path list from common design keys; returns plain strings."""
    if not isinstance(design, dict):
        return []
    for k in ("files_to_change", "new_files_needed", "code_changes"):
        v = design.get(k)
        if isinstance(v, list) and v:
            out = []
            for x in v:
                if isinstance(x, str):
                    out.append(x.strip())
                elif isinstance(x, dict):
                    out.append(_s(x.get("path") or x.get("file") or x.get("name") or x).strip())
            return [p for p in out if p]
    return []


def _is_raw_fallback(d: dict) -> bool:
    """Returns True when _parse_json() fell back to {"raw": text} — caller got no structured data."""
    return isinstance(d, dict) and set(d.keys()) <= {"raw", "error"}


# Phrases that indicate the LLM refused to analyze because it couldn't see the code.
_SENTINEL_PHRASES = (
    "unable to produce", "without access", "unable to access",
    "cannot produce", "do not have access", "don't have access",
    "access to the repository", "no access to", "no source code",
)


def _strip_llm_json_fences(text: str) -> str:
    """
    Strip markdown code-fence artifacts that LLMs add around JSON output.

    Handles three variants:
      - ```json\\n{...}\\n```    full backtick fence with language tag
      - ```\\n{...}\\n```        full backtick fence without language tag
      - json\\n{...}             bare language tag without backticks (streaming artifact
                                 seen when synthesis prompt says "same JSON format" and the
                                 model emits the language identifier but skips the backticks)

    Returns the inner content; the original string if no pattern matches.
    """
    if not text:
        return text
    import re as _re
    s = text.strip()
    # Case 1: full backtick fence — capture everything between the fences
    m = _re.match(r'^```(?:json|JSON)?\s*\n?([\s\S]+?)\n?```\s*$', s)
    if m:
        return m.group(1).strip()
    # Case 2: opening fence only (LLM was cut off before closing ```)
    m = _re.match(r'^```(?:json|JSON)?\s*\n([\s\S]+)', s)
    if m:
        return m.group(1).rstrip('`').strip()
    # Case 3: bare "json\n" or "JSON\n" prefix without backticks
    if _re.match(r'^(?:json|JSON)\s*\n', s):
        return s.split('\n', 1)[1].strip()
    return text


def _parse_json(text: str) -> dict:
    """
    Robust JSON extractor — handles plain JSON, markdown-fenced JSON, and raw {} scans.
    Falls back to {"raw": text} so callers can still access the LLM output.
    """
    import re

    text = text.strip()

    # 1 — plain JSON parse
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # 2 — JSON inside ```json...``` or ``` ... ``` fence
    for fence_re in [r"```json\s*(\{[\s\S]+?\})\s*```", r"```\w*\s*(\{[\s\S]+?\})\s*```"]:
        m = re.search(fence_re, text)
        if m:
            try:
                result = json.loads(m.group(1))
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

    # 3 — scan for all {...} blocks, try largest first
    candidates = re.findall(r"\{[\s\S]+\}", text)
    for candidate in sorted(candidates, key=len, reverse=True)[:3]:
        try:
            result = json.loads(candidate)
            if isinstance(result, dict):
                return result
        except Exception:
            pass

    logger.warning(
        f"[SDLC] _parse_json: could not extract JSON from LLM output (len={len(text)}) — raw fallback. "
        f"head={text[:500]!r}"
    )
    return {"raw": text}


def _make_confluence_md_feature(title: str, classification: dict, analysis: dict, design: dict) -> str:
    """
    Generate a properly-formatted Markdown document for a feature solution design.
    Used instead of relying on the LLM's confluence_doc field (which is often raw JSON).
    """
    cls = classification or {}
    ana = analysis or {}
    des = design or {}

    lines = [f"# Solution Design: {title}", ""]

    # Overview
    lines += ["## Overview", ""]
    lines.append(f"**Core Intent:** {cls.get('core_intent', '—')}")
    lines.append(f"**Complexity:** {cls.get('complexity', '—')}  |  **Effort Estimate:** {cls.get('effort_estimate', '—')}")
    lines.append("")

    # Impacted Components
    comps = cls.get("affected_components") or []
    if comps:
        lines += ["## Impacted Components", ""]
        for c in comps:
            lines.append(f"- {_s(c)}")
        lines.append("")

    # Architecture
    arch = _s(des.get("solution_approach") or "")
    if arch:
        lines += ["## Architecture / Solution Approach", "", arch, ""]

    # Implementation Plan
    plan = des.get("implementation_plan") or []
    if plan:
        lines += ["## Implementation Plan", ""]
        for i, step in enumerate(plan, 1):
            lines.append(f"{i}. {_s(step)}")
        lines.append("")

    # Files
    ftc = ana.get("files_to_change") or []
    nf  = ana.get("new_files_needed") or []
    if ftc or nf:
        lines += ["## Files Affected", ""]
        if ftc:
            lines.append("**Modified:**")
            for f in ftc:
                lines.append(f"- `{_s(f)}`")
        if nf:
            lines.append("**New:**")
            for f in nf:
                lines.append(f"- `{_s(f)}`")
        lines.append("")

    # DB Changes
    db_ch = _s(des.get("data_model_changes") or ana.get("model_changes") or "")
    if db_ch and db_ch.strip().lower() not in ("none", "null", ""):
        lines += ["## Database Changes", "", db_ch, ""]

    # API Changes
    api_ch = _s(des.get("api_changes") or "")
    if api_ch and api_ch.strip().lower() not in ("none", "null", ""):
        lines += ["## API Changes", "", api_ch, ""]

    # Testing Strategy
    test_str = _s(des.get("testing_strategy") or "")
    if test_str:
        lines += ["## Testing Strategy", "", test_str, ""]

    # Rollback
    rb = _s(des.get("rollback_strategy") or "")
    if rb:
        lines += ["## Rollback Strategy", "", rb, ""]

    # Risks
    risks = cls.get("risks") or []
    if risks:
        lines += ["## Risks & Dependencies", ""]
        for r in risks:
            lines.append(f"- {_s(r)}")
        deps = cls.get("dependencies") or []
        for d in deps:
            lines.append(f"- {_s(d)}")
        lines.append("")

    # Open Questions
    oq = des.get("open_questions") or []
    if oq:
        lines += ["## Open Questions", ""]
        for q in oq:
            lines.append(f"- {_s(q)}")
        lines.append("")

    return "\n".join(lines)


def _make_confluence_md_bug(title: str, triage: dict, rca: dict, fix: dict) -> str:
    """
    Generate a properly-formatted Markdown document for a bug fix design.
    """
    t = triage or {}
    r = rca    or {}
    f = fix    or {}

    lines = [f"# Bug Fix Design: {title}", ""]

    # Bug Summary
    lines += ["## Bug Summary", ""]
    lines.append(f"**Severity:** {t.get('severity', '—')}  |  **Category:** {t.get('category', '—')}")
    lines.append(f"**Reproduction:** {t.get('reproduction', '—')}  |  **Assignee Role:** {t.get('assignee_role', '—')}")
    lines.append("")

    # Impacted Components
    comps = t.get("affected_components") or []
    if comps:
        lines += ["## Impacted Components", ""]
        for c in comps:
            lines.append(f"- {_s(c)}")
        lines.append("")

    # Root Cause
    rc = _s(r.get("root_cause") or "")
    if rc:
        lines += ["## Root Cause Analysis", "", rc, ""]

    # Hypotheses
    hyps = r.get("hypotheses") or []
    if hyps:
        lines += ["## Hypotheses", ""]
        for h in hyps:
            if isinstance(h, dict):
                lines.append(f"- **{h.get('likelihood','?')}** — {h.get('hypothesis','')} _{h.get('evidence','')}_")
            else:
                lines.append(f"- {_s(h)}")
        lines.append("")

    # Code Path
    cp = _code_path(r.get("code_path"))
    if cp:
        lines += ["## Affected Code Path", "", f"```\n{cp}\n```", ""]

    # Fix Description
    fd = _s(f.get("fix_description") or "")
    if fd:
        lines += ["## Fix Description", "", fd, ""]

    # Code Changes
    cc = f.get("code_changes") or []
    if cc:
        lines += ["## Code Changes", ""]
        for c in cc:
            if isinstance(c, dict):
                lines.append(f"- **`{c.get('file', '?')}`** — {c.get('change', '')}")
            else:
                lines.append(f"- {_s(c)}")
        lines.append("")

    # Tests
    tests = f.get("tests_to_add") or []
    if tests:
        lines += ["## Tests to Add", ""]
        for item in tests:
            lines.append(f"- {_s(item)}")
        lines.append("")

    # Verification
    vs = f.get("verification_steps") or []
    if vs:
        lines += ["## Verification Steps", ""]
        for i, s in enumerate(vs, 1):
            lines.append(f"{i}. {_s(s)}")
        lines.append("")

    # Regression Risk
    rr = f.get("regression_risk") or ""
    if rr:
        lines += ["## Regression Risk", "", f"**{rr}**", ""]

    return "\n".join(lines)


def _publish_confluence(title: str, body: str, repo_name: str = "", space_key: str = "",
                        user_id: str = "", user_email: str = "") -> str:
    """Publish a Confluence page.  Space resolved from product → env var."""
    if not user_id and not user_email:
        user_id, user_email = _get_run_user()
    try:
        # Resolve space key from product if not given
        if not space_key and repo_name:
            try:
                from core.platform_credentials import get_product_for_repo
                ctx = get_product_for_repo(repo_name)
                space_key = ctx.get("confluence_space", "")
            except Exception:
                pass
        from tools.confluence_tools import confluence_create_page
        result = json.loads(confluence_create_page(title=title, body=body, space_key=space_key,
                                                   user_id=user_id, user_email=user_email))
        return result.get("url", "")
    except Exception as e:
        logger.warning(f"SDLC: Confluence publish failed → {e}")
        return ""


def _get_pr_diff(repo: str, pr_number: int) -> str:
    """
    Fetch actual code diff for an MR — file list with unified patches.
    Falls back to MR metadata if the files API fails.
    Truncated per-file to 3000 chars to stay within LLM context.
    """
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_get_pr_files as _get_mr_files
            from tools.github_tools import github_get_pr as _get_mr
        else:
            from tools.gitlab_tools import gitlab_get_mr_files as _get_mr_files
            from tools.gitlab_tools import gitlab_get_mr as _get_mr
        files = _get_mr_files(repo, pr_number, max_files=20)
        if not files:
            return _get_mr(repo, pr_number)
        parts = []
        for f in files:
            header = (
                f"### {f['filename']}  [{f['status']}]  "
                f"+{f['additions']} -{f['deletions']}\n"
            )
            patch  = f.get("patch", "") or "(binary or no diff)"
            # Truncate large patches so total stays within LLM window
            if len(patch) > 3000:
                patch = patch[:3000] + "\n... (truncated)"
            parts.append(header + "```diff\n" + patch + "\n```")
        return f"PR #{pr_number} — {len(files)} file(s) changed:\n\n" + "\n\n".join(parts)
    except Exception as e:
        logger.warning(f"SDLC: get_pr_diff failed → {e}")
        return f"PR #{pr_number} in {repo}"


def _post_pr_review_comment(repo: str, pr_number: int, review: dict, iteration: int):
    """
    Post a GitLab MR review note (APPROVE / REQUEST_CHANGES),
    then fall back to a plain MR note if the API returns an error.
    """
    from core.config import SCM_PROVIDER as _SCM
    if _SCM == "github":
        from tools.github_tools import github_create_pr_review as gitlab_create_mr_review
        from tools.github_tools import github_comment_on_pr as gitlab_comment_on_mr
    else:
        from tools.gitlab_tools import gitlab_create_mr_review, gitlab_comment_on_mr

    approved = review.get("approved", False)
    score    = review.get("score", "?")
    summary  = review.get("summary", "")
    blocking = review.get("blocking_issues") or []
    suggest  = review.get("suggestions") or []
    sec      = review.get("security_flags") or []
    files    = review.get("file_reviews") or []   # list of {file, issues, suggestions}

    # ── Build overall review body ──────────────────────────────
    verdict = "✅ APPROVED" if approved else "⚠️ CHANGES REQUESTED"
    body_lines = [
        f"## AiNxt AI Code Review — Iteration {iteration}",
        f"**Verdict:** {verdict} &nbsp;|&nbsp; **Score:** {score}/10",
        "",
        "### Summary",
        summary or "_No summary provided._",
    ]
    if blocking:
        body_lines += ["", "### ❌ Blocking Issues"]
        body_lines += [f"- {i}" for i in blocking]
    if sec:
        body_lines += ["", "### 🔒 Security Flags"]
        body_lines += [f"- {f}" for f in sec]
    if suggest:
        body_lines += ["", "### 💡 Suggestions"]
        body_lines += [f"- {s}" for s in suggest]
    if files:
        body_lines += ["", "### 📂 File-by-File Review"]
        for fr in files:
            fname = fr.get("file", "")
            body_lines.append(f"\n**`{fname}`**")
            for iss in (fr.get("issues") or []):
                body_lines.append(f"  - ❌ {iss}")
            for sug in (fr.get("suggestions") or []):
                body_lines.append(f"  - 💡 {sug}")
    body_lines.append("\n---\n_Review generated by AiNxt Autonomous SDLC Pipeline_")

    event        = "APPROVE" if approved else "REQUEST_CHANGES"
    review_body  = "\n".join(body_lines)

    # Post MR review note — falls back to plain note on error
    result = gitlab_create_mr_review(
        repo, pr_number,
        body=review_body,
        event=event,
        comments=None,
    )
    if result.startswith("[Error"):
        logger.warning(f"SDLC: MR review API failed ({result}) — posting plain comment")
        fallback = gitlab_comment_on_mr(repo, pr_number, review_body)
        logger.info(f"SDLC: plain MR comment posted: {fallback}")
    else:
        logger.info(f"SDLC MR review posted: {result}")


def _extract_jira_from_pr(pr: dict) -> str:
    import re
    title = pr.get("title", "")
    match = re.search(r"([A-Z]+-\d+)", title)
    return match.group(1) if match else ""


# ── Merge Conflict Detection + Resolution ─────────────────────────────────────

def _detect_merge_conflict(repo_url: str, branch: str, base_branch: str = "main") -> bool:
    """
    Check if a branch has merge conflicts with base branch.
    Uses GitLab API to check mergeable status on any open MR for this branch.
    Returns True if conflict detected.
    """
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import _find_existing_pr as _find_existing_mr
        else:
            from tools.gitlab_tools import _find_existing_mr
        # Extract namespace/project from URL
        url = (repo_url or "").rstrip("/")
        if url.endswith(".git"):
            url = url[:-4]
        parts = url.split("/")
        if len(parts) < 2:
            return False
        repo = f"{parts[-2]}/{parts[-1]}"
        # Check open MRs/PRs for this source branch
        mr = _find_existing_mr(repo, branch)
        if mr:
            if _SCM == "github":
                # GitHub: mergeable=False means conflict
                if mr.get("mergeable") is False:
                    return True
            else:
                # GitLab has_conflicts field
                if mr.get("has_conflicts"):
                    return True
        return False
    except Exception:
        return False


def _generate_conflict_resolution(conflict_context: str) -> str:
    """Use Claude to propose merge conflict resolution steps."""
    from models.model_router import model_router
    from core.circuit_breaker import get_breaker
    prompt = (
        "You are a senior engineer. Propose step-by-step merge conflict resolution for:\n\n"
        f"{conflict_context}\n\n"
        "Provide:\n"
        "1. Root cause of the conflict\n"
        "2. Recommended resolution approach\n"
        "3. Specific files/lines to change\n"
        "4. Command sequence to resolve"
    )
    system = "You are an expert at resolving Git merge conflicts."
    try:
        result = get_breaker("claude").call(
            lambda: model_router.generate(prompt, model_hint="solution", system_prompt=system)
        )
        if result and result.strip():
            return result
        raise ValueError("empty response from Claude")
    except Exception as _ce:
        logger.warning(f"[SDLC] Claude conflict resolution unavailable ({_ce}) — falling back to GPT-5.2")
        return model_router.generate(prompt, model_hint="medium", system_prompt=system)



def _authenticated_clone_url(repo: str, gl_url: str, gl_token: str) -> str:
    """Token-embedded HTTPS clone URL.

    For GitLab: standard ``oauth2:<token>@<host>/<repo>.git`` form.
    For GitHub:  ``https://<token>@github.com/<repo>.git`` form.

    Generic (not governance-specific): also the fallback used by the primary
    feature/bug clone path when a repo has no ``repo_index_status`` row, i.e.
    was never indexed. Indexing is not a prerequisite for running SDLC.
    """
    from urllib.parse import urlsplit, urlunsplit
    from core.config import SCM_PROVIDER as _SCM
    if _SCM == "github":
        # GitHub HTTPS clone: https://<token>@github.com/<repo>.git
        _gh_host = os.getenv("GITHUB_URL", "https://github.com").rstrip("/")
        _sp = urlsplit(_gh_host)
        _netloc = f"{gl_token}@{_sp.netloc}" if gl_token else _sp.netloc
        return urlunsplit((_sp.scheme or "https", _netloc, f"/{repo}.git", "", ""))
    else:
        # GitLab: oauth2:<token>@<host>/<repo>.git
        _sp = urlsplit(gl_url)
        _netloc = f"oauth2:{gl_token}@{_sp.netloc}" if gl_token else _sp.netloc
        return urlunsplit((_sp.scheme or "https", _netloc, f"/{repo}.git", "", ""))


def _gov_resolve_clone_url(repo: str, gl_url: str, gl_token: str,
                           user_id: str = "", user_email: str = "") -> str:
    """Resolve the clone URL for a governance workspace.

    Prefers ``repo_index_status.git_url`` — the SAME source the feature/bug
    pipeline clones from (``sdlc_state_machine._ensure_run_workspace``). This is
    what makes governance honor the local GitLab mock (a ``file://`` git_url is
    seeded there) AND, in production, clone the exact registered origin with the
    triggering user's own PAT re-injected. Falls back to the ``GITLAB_URL``-derived
    ``oauth2:<token>`` URL only when the repo is not registered in the index.
    """
    try:
        from db.database import engine as _eng
        from sqlalchemy import text as _txt
        from agents.sdlc_context import normalize_repo_index_key_without_prefix as _nrik
        _canon = _nrik(repo)
        for _slug in (_canon, repo):
            if not _slug:
                continue
            with _eng.connect() as _c:
                _row = _c.execute(
                    _txt("SELECT git_url FROM repo_index_status WHERE repo_name=:s"),
                    {"s": _slug},
                ).fetchone()
            if _row and _row.git_url:
                from core.platform_credentials import build_run_clone_url as _burl
                url = _burl(_row.git_url, user_id=user_id or "", email=user_email or "")
                logger.info("[SDLC-GOV] clone url from repo_index_status",
                            repo=repo, slug=_slug)
                return url
    except Exception as e:
        logger.warning("[SDLC-GOV] repo_index_status lookup failed (falling back to GITLAB_URL)",
                       repo=repo, error=str(e))
    return _authenticated_clone_url(repo, gl_url, gl_token)


def _gov_clone_workspace(run_id: str, repo: str, head_branch: str,
                         gl_url: str, gl_token: str,
                         user_id: str = "", user_email: str = "") -> str:
    """Fresh-clone ``head_branch`` of ``repo`` into the governance workspace.
    Returns the workspace path on success, "" on failure. Never raises."""
    import shutil
    import subprocess
    ws = _gov_workspace_dir(run_id)
    try:
        if os.path.isdir(ws):
            shutil.rmtree(ws, ignore_errors=True)
        os.makedirs(ws, exist_ok=True)
        clone_url = _gov_resolve_clone_url(repo, gl_url, gl_token, user_id, user_email)
        cmd = ["git", "clone"]
        if head_branch:
            cmd += ["--branch", head_branch]
        cmd += [clone_url, ws]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            logger.error("[SDLC-GOV] clone failed", run_id=run_id, repo=repo,
                         head_branch=head_branch, stderr=(r.stderr or "")[-500:])
            return ""
        return ws
    except Exception as e:
        logger.error("[SDLC-GOV] clone errored", run_id=run_id, repo=repo, error=str(e))
        return ""


def _gov_git_diff(workspace: str, base_ref: str) -> tuple:
    """Return (changed_files, diff_text) for ``{base_ref}...HEAD`` in ``workspace``.
    Falls back to ``origin/{base_ref}`` when the bare ref is unknown locally.
    Never raises — returns ([], "") on any failure."""
    import subprocess

    def _try(ref: str):
        try:
            names = subprocess.run(
                ["git", "diff", f"{ref}...HEAD", "--name-only"],
                cwd=workspace, capture_output=True, text=True, timeout=60,
            )
            if names.returncode != 0:
                return None
            body = subprocess.run(
                ["git", "diff", f"{ref}...HEAD"],
                cwd=workspace, capture_output=True, text=True, timeout=120,
            )
            files = [ln.strip() for ln in (names.stdout or "").splitlines() if ln.strip()]
            return files, (body.stdout or "")
        except Exception:
            return None

    for ref in [r for r in (base_ref, f"origin/{base_ref}") if r]:
        out = _try(ref)
        if out is not None:
            return out
    return [], ""


def _gov_commit_and_push(workspace: str, push_branch: str, *, run_id: str,
                         message: str, author_name: str = "",
                         author_email: str = "") -> tuple:
    """Stage + commit the governance fixer's working-tree changes and push to
    ``origin HEAD:{push_branch}`` so the fix reaches origin and the subsequent
    re-scan diff (``git diff base...HEAD``) actually SEES it. ``push_branch`` is the
    governance FIX branch (not the developer's scanned branch); the post-approval MR
    is opened fix_branch → scanned_branch.

    The commit ALWAYS passes ``-c user.name`` / ``-c user.email``: the fresh
    governance clone (``_gov_clone_workspace``) never configures a git identity, so a
    bare ``git commit`` fails with "Author identity unknown" on any host without a
    global identity — and that failure was silently swallowed as ``(False, False)``,
    which is exactly the "changes staged but never committed/pushed" symptom. Prefer
    the triggering user's identity; fall back to a platform bot.

    Returns ``(committed: bool, pushed: bool)``. Idempotent: a clean tree
    ("nothing to commit") returns ``(False, False)`` so the caller STOPS the loop
    rather than looping forever or pushing an empty change. The origin URL was
    cloned with the triggering user's embedded PAT (``_gov_ensure_workspace``), so
    the push authenticates without mutating the process-wide GITLAB_TOKEN. Never
    raises. NOTE: ``.governance_skills/`` / ``.governance_diff/`` are git-excluded,
    so ``git add -A`` never stages the read-only staged skills."""
    import subprocess

    _name  = (author_name or "").strip()  or "AiNxt AI"
    _email = (author_email or "").strip() or "ainxt-bot@example.com"

    def _run(args, timeout=120):
        try:
            return subprocess.run(["git"] + args, cwd=workspace,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as _e:
            logger.warning("[SDLC-GOV] git op failed", run_id=run_id, args=args, error=str(_e))
            return None

    _run(["add", "-A"])
    # `git diff --cached --quiet` exits 0 when the index is clean, 1 when staged
    # changes exist — the reliable "is there anything to commit?" check.
    _st = _run(["diff", "--cached", "--quiet"])
    if _st is not None and _st.returncode == 0:
        return (False, False)   # nothing staged → nothing to commit

    # `-c user.name/-c user.email` MUST precede the `commit` subcommand. Without an
    # identity the commit exits non-zero ("Please tell me who you are") and never lands.
    _c = _run(["-c", f"user.name={_name}", "-c", f"user.email={_email}",
               "commit", "-m", message])
    if _c is None or _c.returncode != 0:
        _out = ((_c.stdout if _c else "") + (_c.stderr if _c else "")).lower()
        if "nothing to commit" in _out:
            return (False, False)
        logger.warning("[SDLC-GOV] git commit failed (treating as no-change)", run_id=run_id,
                       out=(_c.stdout if _c else ""), err=(_c.stderr if _c else ""))
        return (False, False)

    pushed = False
    if push_branch:
        _p = _run(["push", "origin", f"HEAD:{push_branch}"], timeout=180)
        pushed = bool(_p is not None and _p.returncode == 0)
        if not pushed:
            logger.warning("[SDLC-GOV] git push failed (fix committed locally, MR may lag)",
                           run_id=run_id, branch=push_branch, err=(_p.stderr if _p else ""))
    else:
        logger.warning("[SDLC-GOV] no push branch resolved for governance fix",
                       run_id=run_id)
    return (True, pushed)


def _gov_prepare_fix_branch(workspace: str, scanned_branch: str, fix_branch: str,
                            *, run_id: str) -> bool:
    """Check out the governance FIX branch in ``workspace`` so every fixer commit
    lands on it, NEVER on the developer's scanned branch. If origin already has the
    fix branch (a prior 'Run fixes' round on this run), fetch + check it out so
    earlier fix commits are preserved; otherwise create it off the current HEAD (the
    scanned branch tip). Idempotent — a no-op when already on the fix branch. Returns
    True on success. Never raises."""
    import subprocess

    def _run(args, timeout=120):
        try:
            return subprocess.run(["git"] + args, cwd=workspace,
                                  capture_output=True, text=True, timeout=timeout)
        except Exception as _e:
            logger.warning("[SDLC-GOV] git op failed", run_id=run_id, args=args, error=str(_e))
            return None

    # Already on the fix branch (same batch job, later iteration) → nothing to do.
    _cur = _run(["rev-parse", "--abbrev-ref", "HEAD"])
    if _cur is not None and _cur.returncode == 0 and (_cur.stdout or "").strip() == fix_branch:
        return True

    # Reuse an existing remote fix branch (a prior round) to keep earlier fix commits.
    _fetch = _run(["fetch", "origin", fix_branch], timeout=120)
    if _fetch is not None and _fetch.returncode == 0:
        _co = _run(["checkout", "-B", fix_branch, "FETCH_HEAD"])
        if _co is not None and _co.returncode == 0:
            return True

    # First round → create the fix branch off the current (scanned) HEAD.
    _co = _run(["checkout", "-B", fix_branch])
    ok = bool(_co is not None and _co.returncode == 0)
    if not ok:
        logger.error("[SDLC-GOV] could not create governance fix branch",
                     run_id=run_id, fix_branch=fix_branch, scanned=scanned_branch,
                     err=(_co.stderr if _co else ""))
    return ok


def _gov_resolve_gitlab_token(user_id: str) -> str:
    """Resolve a GitLab PAT for a governance run: per-user token (user_tokens) →
    GITLAB_TOKEN env fallback. Strips any ``user:token`` prefix. Returns "" when
    nothing resolves (caller decides whether that is a hard failure)."""
    gl_token = ""
    if user_id:
        try:
            from core.platform_credentials import get_gitlab_token as _get_gl
            gl_token = _get_gl(user_id=user_id) or ""
        except PermissionError:
            gl_token = ""
        except Exception as e:
            logger.warning("[SDLC-GOV] per-user GitLab token lookup failed", error=str(e))
            gl_token = ""
    if not gl_token:
        gl_token = os.getenv("GITLAB_TOKEN", "")
    if gl_token and ":" in gl_token:
        gl_token = gl_token.split(":", 1)[-1]
    return gl_token


def _governance_preflight(run_id: str, product_id, repo: str, base_branch: str,
                          base_commit: str, head_branch: str) -> bool:
    """Validate GitLab credentials + connectivity for a standalone governance run.

    Relies on the caller having already set the resolved GitLab token in
    ``gitlab_tools`` thread-local via ``set_token()`` (run_governance_pipeline
    does this). Checks, in order:
      1. GitLab token present (thread-local → GITLAB_TOKEN env)      — HARD
      2. GitLab repo reachable (GET /projects/{repo})                — HARD on 404
      3. base_commit resolves (GET /repository/commits/{sha})        — SOFT
      4. head_branch exists (GET /repository/branches/{branch})      — HARD on 404

    Hard failure → marks the run FAILED and returns False. Soft issues warn and
    continue. Non-404 transport errors are treated as soft (transient network),
    mirroring _preflight_check.
    """
    import json as _json
    import urllib.request
    import urllib.error
    from urllib.parse import quote as _q

    gl_url = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")

    # ── 1. token (already set in thread-local by the caller) ──────────────────
    from core.config import SCM_PROVIDER as _SCM_GOV
    gl_token = ""
    try:
        if _SCM_GOV == "github":
            from tools.github_tools import _resolve_token as _gl_resolve
        else:
            from tools.gitlab_tools import _resolve_token as _gl_resolve
        gl_token = _gl_resolve() or ""
    except Exception:
        gl_token = ""
    if not gl_token:
        _gov_env_key = "GITHUB_TOKEN" if _SCM_GOV == "github" else "GITLAB_TOKEN"
        gl_token = os.getenv(_gov_env_key, "")
    if not gl_token:
        reason = "no_scm_token"
        _gov_provider = "GitHub" if _SCM_GOV == "github" else "GitLab"
        logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                     repo=repo, head_branch=head_branch)
        update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                         error=f"Governance pre-flight FAILED: no {_gov_provider} token available "
                               f"(add a {_gov_provider} PAT under Profile → {_gov_provider} Token, or set "
                               f"{_gov_env_key} for service-triggered runs).")
        return False

    _proj = _q(repo, safe="") if repo else ""

    def _gl_get(path: str, timeout: int = 8):
        """SCM-agnostic GET helper for governance preflight checks."""
        if _SCM_GOV == "github":
            # GitHub API paths: /repos/{repo}/branches/{branch}, /repos/{repo}/commits/{sha}
            # path is like "/repository/branches/main" → convert to GitHub equivalent
            if path == "":
                _gh_path = f"/repos/{repo}"
            elif path.startswith("/repository/branches/"):
                _branch = path.split("/repository/branches/", 1)[1]
                _gh_path = f"/repos/{repo}/branches/{_branch}"
            elif path.startswith("/repository/commits/"):
                _sha = path.split("/repository/commits/", 1)[1]
                _gh_path = f"/repos/{repo}/commits/{_sha}"
            else:
                _gh_path = f"/repos/{repo}{path}"
            _gh_url = f"https://api.github.com{_gh_path}"
            req = urllib.request.Request(
                _gh_url,
                headers={"Authorization": f"Bearer {gl_token}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
            )
        else:
            req = urllib.request.Request(
                f"{gl_url}/api/v4/projects/{_proj}{path}",
                headers={"PRIVATE-TOKEN": gl_token, "Content-Type": "application/json"},
            )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read().decode())

    # ── 2. repo connectivity (HARD on 404) ───────────────────────────────────
    if not (repo and "/" in repo):
        reason = "repo_not_namespaced"
        logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason, repo=repo)
        update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                         error=f"Governance pre-flight FAILED: repo {repo!r} is not in "
                               "namespace/project form.")
        return False
    try:
        _data = _gl_get("")
        _name = _data.get("full_name") or _data.get("name_with_namespace", repo)
        logger.info("[SDLC-GOV] preflight repo access OK", run_id=run_id, repo=repo,
                    name=_name,
                    default_branch=_data.get("default_branch", ""))
    except urllib.error.HTTPError as he:
        if he.code == 404:
            reason = "repo_404"
            _gov_provider = "GitHub" if _SCM_GOV == "github" else "GitLab"
            logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                         repo=repo)
            update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                             error=f"Governance pre-flight FAILED: {_gov_provider} 404 on repo {repo!r}. "
                                   "Check the repo path and token scope.")
            return False
        logger.warning("[SDLC-GOV] preflight repo check non-fatal", run_id=run_id, repo=repo,
                       http=he.code)
    except Exception as e:
        logger.warning("[SDLC-GOV] preflight repo check non-fatal", run_id=run_id, repo=repo,
                       error=str(e))

    # ── 3. base_commit resolvable (SOFT) ──────────────────────────────────────
    if base_commit:
        try:
            _gl_get(f"/repository/commits/{_q(base_commit, safe='')}")
            logger.info("[SDLC-GOV] preflight base_commit OK", run_id=run_id,
                        base_commit=base_commit)
        except Exception as e:
            logger.warning("[SDLC-GOV] preflight base_commit not verifiable (soft)",
                           run_id=run_id, base_commit=base_commit, error=str(e))

    # ── 4. head_branch exists (HARD on 404) ───────────────────────────────────
    if head_branch:
        try:
            _gl_get(f"/repository/branches/{_q(head_branch, safe='')}")
            logger.info("[SDLC-GOV] preflight head_branch OK", run_id=run_id,
                        head_branch=head_branch)
        except urllib.error.HTTPError as he:
            if he.code == 404:
                reason = "head_branch_not_found"
                logger.error("[SDLC-GOV] preflight hard failure", run_id=run_id, reason=reason,
                             repo=repo, head_branch=head_branch)
                update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                                 error=f"Governance pre-flight FAILED: branch {head_branch!r} "
                                       f"not found in {repo!r}.")
                return False
            logger.warning("[SDLC-GOV] preflight head_branch check non-fatal", run_id=run_id,
                           head_branch=head_branch, http=he.code)
        except Exception as e:
            logger.warning("[SDLC-GOV] preflight head_branch check non-fatal", run_id=run_id,
                           head_branch=head_branch, error=str(e))

    return True


def run_governance_pipeline(issue: dict, run_id=None) -> str:
    """Standalone governance pipeline: scan → per-domain HITL gate → fix → MR.

    issue keys: product_id, repo, base_branch, base_commit, head_branch,
                governance_skills (optional subset), triggered_by_user_id,
                triggered_by_email
    """
    repo         = (issue.get("repo") or "").strip()
    base_branch  = issue.get("base_branch") or "main"
    base_commit  = issue.get("base_commit") or ""
    head_branch  = issue.get("head_branch") or ""
    subset       = issue.get("governance_skills")
    user_id      = issue.get("triggered_by_user_id", "")
    user_email   = issue.get("triggered_by_email", "")

    # ── 1. create / resolve run ───────────────────────────────────────────────
    if run_id:
        run = get_run(run_id) or create_run(
            run_type="governance", repo=repo, jira_key=head_branch,
            jira_summary=f"Governance scan: {repo}", triggered_by="api",
            created_by=user_id,
        )
        run_id = run["id"]
    else:
        run = create_run(
            run_type="governance", repo=repo, jira_key=head_branch,
            jira_summary=f"Governance scan: {repo}", triggered_by="api",
            created_by=user_id,
        )
        run_id = run["id"]

    # ── 2. context ────────────────────────────────────────────────────────────
    bind_context(correlation_id=run_id, pipeline_stage="governance_pipeline")

    try:
        # Persist the trigger fields into the run context so the HITL-resume path
        # (a separate process) can rebuild the workspace + branch state.
        update_run_state(run_id, run.get("state", "CREATED"), context_patch={
            "repo":         repo,
            "base_branch":  base_branch,
            "base_commit":  base_commit,
            "head_branch":  head_branch,
            "product_id":   issue.get("product_id") or "",
            "user_id":      user_id,
            "user_email":   user_email,
            "governance_skills": subset or [],
        })

        # ── 3. resolve + set SCM token (thread-local for concurrent workers) ──
        gl_url   = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")
        gl_token = _gov_resolve_gitlab_token(user_id)
        if gl_token:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import set_token as _gl_set_token
            else:
                from tools.gitlab_tools import set_token as _gl_set_token
            _gl_set_token(gl_token)

        # ── 4. preflight ────────────────────────────────────────────────────────
        if not _governance_preflight(run_id, issue.get("product_id"), repo,
                                      base_branch, base_commit, head_branch):
            return run_id

        # ── 4b. HOD budget preflight ─────────────────────────────────────────────
        # Governance runs consume LLM tokens (one agentic scan session per skill),
        # so they participate in HOD budget governance exactly like feature/bug runs.
        # This writes sdlc_runs.hod_email so finalize_run_budget() can attribute the
        # cost at run-end (instead of "finalize skip — no hod_email"). Hard failure
        # only when enforcement is on and the HOD cap is exhausted.
        try:
            from services.sdlc_budget_tracker import check_hod_budget as _chk_hod
            _hod_ok, _hod_err = _chk_hod(user_id=user_id, run_id=run_id, user_email=user_email)
            if not _hod_ok:
                logger.error("[SDLC-GOV] HOD budget preflight blocked run", run_id=run_id,
                             reason=_hod_err)
                update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                                 error=_hod_err)
                return run_id
        except Exception as _hod_exc:
            logger.warning("[SDLC-GOV] HOD budget preflight error (non-blocking)",
                           run_id=run_id, error=str(_hod_exc))

        # ── 5. GOVERNANCE_SCAN transition ────────────────────────────────────────
        _transition(run_id, "GOVERNANCE_SCAN", "governance-scanner")

        # ── 6. clone workspace (head_branch) ─────────────────────────────────────
        workspace = _gov_clone_workspace(run_id, repo, head_branch, gl_url, gl_token,
                                         user_id=user_id, user_email=user_email)
        if not workspace:
            update_run_state(run_id, "FAILED", current_stage="GOVERNANCE_SCAN",
                             error=f"Governance scan: failed to clone {repo!r}@{head_branch!r}.")
            return run_id
        update_run_state(run_id, "GOVERNANCE_SCAN", context_patch={"workspace": workspace})

        # ── 7. compute diff ──────────────────────────────────────────────────────
        base_ref = base_commit or base_branch
        changed_files, diff_text = _gov_git_diff(workspace, base_ref)
        logger.info("[SDLC-GOV] diff captured", run_id=run_id, repo=repo,
                    base_commit=base_commit, head_branch=head_branch,
                    changed_files=len(changed_files))

        # EMPTY-DIFF GUARD (2026-07-30): the scan runs on a FRESH CLONE of
        # origin/<head_branch>. An empty diff (no changes over the base) means the
        # branch's commits never reached origin (unpushed local changes) or a
        # base/branch misresolution. Scanning it writes empty .patch files and would
        # FALSE-GREEN the gate — SUSPEND with an actionable message instead.
        if not changed_files or not (diff_text or "").strip():
            _empty_msg = (
                f"Governance scan found no changes on '{head_branch}' over "
                f"'{base_ref}'. The branch has no diff versus its base — usually the "
                "commits were not pushed to origin. Ensure the changes are committed "
                f"and pushed to origin/{head_branch}, then retry governance."
            )
            logger.error("[SDLC-GOV] standalone diff is EMPTY — suspending",
                         run_id=run_id, repo=repo, head_branch=head_branch,
                         base_ref=base_ref)
            update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                             error=_empty_msg)
            add_run_event(run_id, "GOVERNANCE_SCAN", "SUSPENDED",
                          stage="GOVERNANCE_SCAN", actor="governance-scanner")
            return run_id

        # ── 8-10. unified scan core (cap → select → per-skill scan → suppress →
        #    persist findings + snapshot → report). SAME primitive the end-gate and
        #    the standalone worker use — this is what makes every trigger spawn one
        #    parallel session per skill (scan-unify 2026-07-28). ──────────────────
        from agents.sdlc_governance import engine as gov_engine, config as gov_config
        from store.sdlc_governance_findings import domain_open_counts
        from store.sdlc_governance_approvers import seed_domain_approvals
        from store.sdlc_artifacts import _store_artifact

        try:
            from db.database import SessionLocal
            db = SessionLocal()
        except Exception:
            db = None
        product_id = issue.get("product_id") or gov_engine.resolve_product_id(db, repo)

        res = run_governance_scan_snapshot(
            run_id, workspace=workspace, diff_text=diff_text, changed_files=changed_files,
            product_id=product_id, repo=repo, base_sha=base_commit or "HEAD",
            subset=subset, db=db, trigger="initial", created_by=user_email,
        )
        if db:
            db.close()

        # Diff too large OR scan CLI could not complete → SUSPEND for manual retry.
        if res.get("scan_error"):
            _detail = res.get("scan_error_detail") or ""
            _suspend_msg = (
                f"Governance scan not run — {_detail}"
                if res.get("diff_too_large")
                else (f"Governance scan could not complete ({_detail or 'CLI error'}). "
                      "Increase SDLC_GOVERNANCE_SCAN_TURNS and retry.")
            )
            logger.warning("[SDLC-GOV] scan engine failure → SUSPEND",
                           run_id=run_id, reason=_detail,
                           diff_too_large=bool(res.get("diff_too_large")))
            update_run_state(run_id, "SUSPENDED", current_stage="GOVERNANCE_SCAN",
                             error=_suspend_msg)
            add_run_event(run_id, "GOVERNANCE_SCAN", "SUSPENDED",
                          stage="GOVERNANCE_SCAN", actor="governance-scanner")
            return run_id

        # No bundle/skills resolved → nothing to scan.
        if res.get("skipped"):
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE",
                             error="No governance skills resolved — nothing to scan")
            add_run_event(run_id, "GOVERNANCE_SCAN", "COMPLETE", stage="COMPLETE",
                          actor="governance-scanner")
            return run_id

        open_f = res.get("open_findings") or []
        suppressed_f = res.get("suppressed") or []
        report = res.get("report")
        _store_artifact(run_id, "GOVERNANCE_REPORT", report, "governance-scanner", "", "system")

        # ── 11. per-domain team sign-off gate (clean-PASS acknowledge, 2026-07-30) ─
        # EVERY scanned domain now requires explicit team acknowledgement — including a
        # clean PASS (zero findings). Seed a 'pending' row per scanned domain (count 0
        # for clean ones). Only when NO domain was classified at all is there nothing to
        # acknowledge → COMPLETE (so the run never stalls with an empty gate).
        counts = domain_open_counts(run_id)
        scanned_domains = {
            (d or "").strip().upper()
            for d in (res.get("domain_by_skill") or {}).values()
            if (d or "").strip()
        }
        all_domains = scanned_domains | set(counts.keys())

        if not open_f and not all_domains:
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(run_id, "GOVERNANCE_SCAN", "COMPLETE", stage="COMPLETE",
                          actor="governance-scanner")
            return run_id

        # ── 12. seed per-domain approvals (all scanned domains) ──────────────────
        seed_domain_approvals(run_id, counts, all_domains=all_domains)
        logger.info("[SDLC-GOV] suspend to approval gate", run_id=run_id,
                    domains=sorted(all_domains), open=len(open_f),
                    clean_pass=not open_f, suppressed=len(suppressed_f))

        # ── 13. suspend to AWAITING_GOVERNANCE_APPROVAL ──────────────────────────
        update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                         current_stage="GOVERNANCE_APPROVAL",
                         context_patch={"awaiting_domain_approvals": sorted(all_domains)})
        add_run_event(run_id, "GOVERNANCE_SCAN", "AWAITING_GOVERNANCE_APPROVAL",
                      stage="GOVERNANCE_APPROVAL", actor="governance-scanner")
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] governance pipeline stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] governance pipeline failed", run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=f"Governance pipeline error: {e}")
        return run_id


def _gov_ensure_workspace(run_id: str, ctx: dict) -> str:
    """Return a usable governance workspace for a resume/trigger step.

    Workspace-identity fix (2026-07-30): the author-fix / re-scan MUST operate on the
    SAME tree that produced the findings, not a fresh clone of the wrong branch. The
    standalone governance pipeline persists `ctx["workspace"]`; the IN-PIPELINE
    feature/bug end-gate persists `ctx["workspace_root"]` (= runs/{run_id}_{slug}, the
    tree the end-gate actually scanned) and `working_branch` — it never sets
    `workspace`/`head_branch`. Previously this read only `ctx["workspace"]`/`head_branch`,
    so a feature/bug run fell through to a fresh clone of `runs/{run_id}_gov` at an EMPTY
    branch (→ default/base branch → HEAD==base → empty diff → the SAME findings on
    re-scan). Now: reuse the first existing checkout (workspace → workspace_root → _gov),
    and if none is on disk, re-clone using the WORKING branch (head_branch →
    working_branch → run.branch), NEVER an empty value. Returns "" on failure."""
    for _cand in (ctx.get("workspace"), ctx.get("workspace_root"),
                  _gov_workspace_dir(run_id)):
        if _cand and os.path.isdir(os.path.join(_cand, ".git")):
            return _cand
    repo        = ctx.get("repo", "")
    head_branch = (ctx.get("head_branch") or ctx.get("working_branch")
                   or (get_run(run_id) or {}).get("branch") or "")
    if not head_branch:
        logger.error("[SDLC-GOV] _gov_ensure_workspace: no working branch resolved — "
                     "refusing to clone (would fetch the base branch → empty diff)",
                     run_id=run_id)
        return ""
    gl_url      = os.getenv("GITLAB_URL", "https://gitlab.example.com").rstrip("/")
    gl_token    = _gov_resolve_gitlab_token(ctx.get("user_id", ""))
    if gl_token:
        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import set_token as _gl_set_token
            else:
                from tools.gitlab_tools import set_token as _gl_set_token
            _gl_set_token(gl_token)
        except Exception:
            pass
    ws = _gov_clone_workspace(run_id, repo, head_branch, gl_url, gl_token,
                              user_id=ctx.get("user_id", ""),
                              user_email=ctx.get("user_email", ""))
    if ws:
        update_run_state(run_id, get_run(run_id).get("state", ""),
                         context_patch={"workspace": ws})
    return ws


def resume_governance_fix(run_id: str, actor: str = "user") -> str:
    """Resume after all domains are approved. Called only after
    all_finding_domains_approved. Runs the auto-fixer over the remaining OPEN
    findings, commits onto head_branch, and opens an MR head_branch → base_branch."""
    import re
    import subprocess

    bind_context(correlation_id=run_id, pipeline_stage="governance_fix")

    from store.sdlc_governance_approvers import (
        all_finding_domains_approved, list_domain_approvals,
    )
    # ── 1. fail-closed guard: every seeded domain must be approved ────────────
    if not all_finding_domains_approved(run_id):
        pending = [d["domain"] for d in list_domain_approvals(run_id)
                   if d.get("status") != "approved"]
        logger.warning("[SDLC-GOV] resume_governance_fix: not all domains approved — no-op",
                       run_id=run_id, pending_domains=pending)
        return run_id

    try:
        # ── 2. load run context ───────────────────────────────────────────────
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}
        repo        = run.get("repo") or ctx.get("repo", "")
        head_branch = ctx.get("head_branch", "")     # the scanned branch = MR TARGET
        base_branch = ctx.get("base_branch", "main")
        base_commit = ctx.get("base_commit", "")
        # The governance FIX branch (set by run_governance_batch_fix when it committed
        # any fix). Empty → nothing was ever fixed → no MR is needed.
        fix_branch  = ctx.get("governance_fix_branch", "")

        # ── author SCM token (thread-local) ───────────────────────────────────
        # This is a FRESH rq resume job: the SCM tools thread-local is empty, so
        # create_mr/create_pr below would otherwise fall back to the env token
        # default and raise the MR under the platform's credentials instead of the
        # author's. Re-resolve the per-user PAT (user_tokens → env fallback) and set
        # it before any SCM call — mirrors run_governance_pipeline / the end-gate
        # resume job. Never mutate the process-wide token env var.
        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import set_token as _gl_set_token
            else:
                from tools.gitlab_tools import set_token as _gl_set_token
            _gov_tok = _gov_resolve_gitlab_token(ctx.get("user_id", ""))
            if _gov_tok:
                _gl_set_token(_gov_tok)
        except Exception as _te:
            logger.warning("[SDLC-GOV] resume_governance_fix: could not set author GitLab token — "
                           "MR may use env default", run_id=run_id, error=str(_te))

        # ── 3. NO post-approval fixer (2026-07-31) ────────────────────────────
        # Once every domain is approved the findings have already been resolved,
        # accepted, or marked false-positive DURING triage — there is nothing left
        # to fix. Re-running the CLI fixer here is exactly the "governance fix kicked
        # off again" defect. Instead just publish the outcome.
        #
        # TOPOLOGY (2026-08-03): governance fixes were committed onto a SEPARATE
        # fix_branch (run_governance_batch_fix), NEVER the developer's scanned branch.
        # So the MR is opened fix_branch → head_branch (scanned branch). If no
        # fix_branch was ever created — clean pass, or every finding was accepted /
        # marked false-positive without a code change — there is nothing to merge →
        # COMPLETE with no MR.
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            from tools.github_tools import github_branch_has_changes as gitlab_branch_has_changes
            from tools.github_tools import github_create_pr as gitlab_create_mr
        else:
            from tools.gitlab_tools import gitlab_branch_has_changes, gitlab_create_mr

        if not fix_branch:
            logger.info("[SDLC-GOV] resume_governance_fix: no fix branch (no code fix) — "
                        "COMPLETE, no MR", run_id=run_id, scanned=head_branch)
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(
                run_id, "GOVERNANCE_SCAN", "COMPLETE", actor=actor,
                output="All domains approved; no governance code fix was needed — "
                       "no MR created, run complete.",
            )
            return run_id

        # Does the fix branch actually differ from the scanned branch? (defensive —
        # the batch fixer only sets fix_branch after a real commit, but a compare is
        # cheap insurance against an empty MR.)
        has_changes = gitlab_branch_has_changes(repo, head_branch, fix_branch)
        if has_changes is False:
            logger.info("[SDLC-GOV] resume_governance_fix: fix branch has no diff over "
                        "scanned branch — COMPLETE, no MR", run_id=run_id,
                        fix_branch=fix_branch, scanned=head_branch)
            update_run_state(run_id, "COMPLETE", current_stage="COMPLETE")
            add_run_event(
                run_id, "GOVERNANCE_SCAN", "COMPLETE", actor=actor,
                output=f"All domains approved; '{fix_branch}' has no changes over "
                       f"'{head_branch}' — no MR created, run complete.",
            )
            return run_id

        # A real change exists (or the compare was indeterminate → fail-open and
        # still open the MR so a real fix is never dropped). Open the MR
        # fix_branch → head_branch (409-idempotent — returns any existing MR).
        _transition(run_id, "MR_CREATION", "governance-approved")
        report_md = ""
        try:
            from store.sdlc_artifacts import _load_latest_artifact
            _art = _load_latest_artifact(run_id, "GOVERNANCE_REPORT")
            report_md = ((_art or {}).get("payload") or {}).get("report_md") or ""
        except Exception:
            report_md = ""
        mr_url = ""
        try:
            mr_result = gitlab_create_mr(
                repo=repo,
                title=f"Governance fixes (run {run_id[:8]})",
                body=report_md or "Governance remediation — all domains approved.",
                head=fix_branch,
                base=head_branch,
            )
            _m = re.search(r"https?://\S+", mr_result or "")
            mr_url = _m.group(0) if _m else (mr_result or "")
            logger.info("[SDLC-GOV] MR created (governance approved)", run_id=run_id,
                        mr_url=mr_url, head=fix_branch, base=head_branch)
        except Exception as _me:
            logger.error("[SDLC-GOV] MR creation errored", run_id=run_id, error=str(_me))

        # ── 4. COMPLETE ───────────────────────────────────────────────────────
        update_run_state(run_id, "COMPLETE", current_stage="COMPLETE", pr_url=mr_url)
        add_run_event(
            run_id, "MR_CREATION", "COMPLETE", actor=actor,
            output=f"All domains approved — MR: {mr_url or '(creation failed)'}",
        )
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] resume_governance_fix stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] resume_governance_fix failed", run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=f"Governance fix error: {e}")
        return run_id


def trigger_domain_fix(run_id: str, domain: str, actor: str,
                       fix_instructions: str = "") -> str:
    """Run the auto-fixer for a SINGLE domain's open findings after its approver
    requested changes, then reset that domain to pending for re-approval. Does NOT
    commit / open an MR — the run stays at the approval gate until every domain is
    approved (then resume_governance_fix commits + opens the MR).

    LEGACY (2026-07-23, B2.6): the per-DOMAIN fixer predates the end-gate model. The
    author remediation loop ``run_governance_author_fix`` (per-FINDING fix + auto
    re-scan + snapshot-scoped carry-forward) is the current end-gate path. This
    function is retained because it is still wired via
    routers.sdlc_router → workers.sdlc_worker.trigger_domain_fix_job; it operates on
    the still-dual-written legacy findings table, marks fixes there, and never
    resumes into APPLYING, so it is safe under the new tail. Prefer the author loop
    for end-gate remediation."""
    bind_context(correlation_id=run_id, pipeline_stage="governance_domain_fix")

    try:
        dom = (domain or "").upper()

        # ── 1. load open findings for this domain only ─────────────────────────
        from store.sdlc_governance_findings import (
            list_findings, mark_fixed, domain_open_counts,
        )
        rows = list_findings(run_id, status="open", domain=dom)
        # ── 2. nothing to fix → no-op ──────────────────────────────────────────
        if not rows:
            logger.info("[SDLC-GOV] trigger_domain_fix: no open findings", run_id=run_id,
                        domain=dom)
            return run_id

        # rebuild Finding objects for the fixer prompt + fingerprints
        from agents.sdlc_governance.schema import Finding, fingerprint as fp_fn
        findings_to_fix = []
        for r in rows:
            try:
                findings_to_fix.append(Finding(
                    skill=r.get("skill") or "", severity=r.get("severity") or "low",
                    file=r.get("file") or "", rule=r.get("rule") or "",
                    title=r.get("title") or "", detail=r.get("detail") or "",
                    fix_hint=r.get("fix_hint") or "", snippet=r.get("snippet") or "",
                    line=r.get("line"), status="open",
                ))
            except Exception:
                continue
        if not findings_to_fix:
            return run_id

        # ── 3. workspace ───────────────────────────────────────────────────────
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}
        workspace = _gov_ensure_workspace(run_id, ctx)
        if not workspace:
            logger.error("[SDLC-GOV] trigger_domain_fix: workspace unavailable",
                         run_id=run_id, domain=dom)
            return run_id

        # ── 4. build fixer prompt (prepend approver instructions as context) ────
        from agents.sdlc_governance.engine import build_fix_prompt
        fix_prompt = build_fix_prompt(findings_to_fix, workspace)
        if fix_instructions.strip():
            fix_prompt = (
                f"APPROVER REQUESTED CHANGES ({dom}): {fix_instructions.strip()}\n\n"
                + fix_prompt
            )

        # ── 5. run CLI fixer (profile="code") ──────────────────────────────────
        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from core.model_registry import cli_model_for
        fix_result = run_cli(
            config=CliEngineConfig.from_env(), workspace_root=workspace,
            prompt=fix_prompt, profile="code", model=cli_model_for("coder"),
            max_turns=60, run_id=run_id,
        )
        if fix_result.status == "suspended":
            logger.warning("[SDLC-GOV] trigger_domain_fix: fixer suspended", run_id=run_id,
                           domain=dom, reason=fix_result.reason)
            return run_id

        # ── 6. mark this domain's findings fixed ───────────────────────────────
        mark_fixed(run_id, [fp_fn(f) for f in findings_to_fix])

        # ── 7. reset the domain back to pending for re-approval ────────────────
        from store.sdlc_governance_approvers import (
            reset_domain_to_pending, seed_domain_approvals,
        )
        reset_domain_to_pending(run_id, dom)

        # ── 8. re-seed open counts (idempotent; refreshes remaining domains) ───
        counts = domain_open_counts(run_id)
        seed_domain_approvals(run_id, counts)
        logger.info("[SDLC-GOV] trigger_domain_fix complete", run_id=run_id, domain=dom,
                    fixed=len(findings_to_fix), remaining_domains=list(counts.keys()))
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] trigger_domain_fix stopped — run cancelled", run_id=run_id)
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] trigger_domain_fix failed", run_id=run_id,
                     domain=domain, error=str(e))


# ============================================================
# AUTHOR REMEDIATION LOOP (2026-07-23, B2.2)
#
# Runs the bounded auto-fix + auto re-scan loop for ONE finding the author
# asked to fix, while the run is SUSPENDED at AWAITING_GOVERNANCE_APPROVAL.
# Enqueued by routers.sdlc_router.author_request_fix via the sdlc_worker
# governance_author_fix_job (never run synchronously in the request handler).
# The run keeps its AWAITING_GOVERNANCE_APPROVAL state throughout — the rq job
# holds a worker slot only while it is actually fixing; when it returns no slot
# is held during subsequent human think-time.
# ============================================================

def run_governance_batch_fix(run_id: str, fingerprints: list, actor: str = "user") -> str:
    """Bounded CLI fixer + re-scan + convergence loop for a BATCH of requested findings.

    ONE fixer CLI session per iteration handles ALL target findings together (the fix
    prompt lists them all), then ONE re-scan verifies — for N findings that is 1 session
    + 1 re-scan per iteration, not N. Each iteration (capped by ``config.max_iters()``):
    fix → commit/push → re-diff → NEW scan snapshot (trigger="rescan") → mark findings
    that disappeared vs the prior snapshot ``fix_confirmed`` → check convergence.

    Stops on ANY of: all target findings resolved; ``max_iters()`` reached; the open-set
    hash repeats; the open count fails to strictly decrease for
    ``convergence_stall_limit()`` iterations; the HOD per-run budget is exhausted; the
    fixer suspends; or a re-scan that could not complete (fail-closed). On stop-without-
    full-resolution, every UNCONFIRMED target is reset ``fix_requested → open`` (so it
    stays actionable + team-visible, never stranded) and ``governance_not_converging`` is
    set. ALWAYS clears ``governance_rescanning`` and re-suspends to
    AWAITING_GOVERNANCE_APPROVAL. NEVER loops unbounded. Never raises (except
    SDLCCancelled)."""
    import hashlib

    bind_context(correlation_id=run_id, pipeline_stage="governance_batch_fix")

    from agents.sdlc_governance import config as gov_config
    from store.sdlc_governance_findings import (
        list_findings, set_disposition, open_fingerprint_set, domain_open_counts,
    )
    from agents.sdlc_governance.schema import Finding, fingerprint as fp_fn

    # De-dup + drop blanks; the batch acts on this ordered set of target fingerprints.
    target_fps = []
    for fp in (fingerprints or []):
        if fp and fp not in target_fps:
            target_fps.append(fp)

    def _observed_from_res(res: dict) -> set:
        """All fingerprints a scan result recorded (open + suppressed)."""
        out = set()
        for f in (res.get("open_findings") or []) + (res.get("suppressed") or []):
            try:
                out.add(fp_fn(f))
            except Exception:
                pass
        return out

    def _open_hash(snapshot_id):
        fps = sorted(open_fingerprint_set(run_id, snapshot_id))
        return (hashlib.sha256("\n".join(fps).encode("utf-8", "ignore")).hexdigest(),
                len(fps))

    def _resuspend(not_converging: bool, reason: str, open_count) -> None:
        """Re-affirm the AWAITING_GOVERNANCE_APPROVAL gate and ALWAYS clear the
        ``governance_rescanning`` flag — the batch job is no longer running, so the UI
        spinner/Send-gate must release (same-state context patch, no rq slot held)."""
        try:
            update_run_state(
                run_id, "AWAITING_GOVERNANCE_APPROVAL", current_stage="GOVERNANCE_SCAN",
                context_patch={
                    "governance_rescanning": False,
                    "governance_not_converging": bool(not_converging),
                    "governance_not_converging_reason": (reason if not_converging else ""),
                },
            )
        except Exception as _re:
            logger.warning("[SDLC-GOV] batch fix re-suspend failed — non-fatal",
                           run_id=run_id, error=str(_re))

    if not target_fps:
        logger.warning("[SDLC-GOV] batch fix: no target fingerprints — no-op", run_id=run_id)
        _resuspend(False, "", 0)
        return run_id

    try:
        run = get_run(run_id) or {}
        ctx = run.get("context") or {}

        # ── locate the requested findings (for the fixer prompt) ───────────────
        by_fp = {}
        for r in (list_findings(run_id) or []):
            fp = r.get("fingerprint")
            if fp in target_fps and fp not in by_fp:
                by_fp[fp] = r
        target_findings = []
        for fp in target_fps:
            r = by_fp.get(fp)
            if not r:
                continue
            try:
                target_findings.append(Finding(
                    skill=r.get("skill") or "", severity=r.get("severity") or "low",
                    file=r.get("file") or "", rule=r.get("rule") or "",
                    title=r.get("title") or "", detail=r.get("detail") or "",
                    fix_hint=r.get("fix_hint") or "", snippet=r.get("snippet") or "",
                    line=r.get("line"), status="open",
                ))
            except Exception:
                pass
        if not target_findings:
            logger.warning("[SDLC-GOV] batch fix: none of the requested findings exist — no-op",
                           run_id=run_id, requested=len(target_fps))
            _resuspend(False, "", 0)
            return run_id

        # Domains this fix batch actually targets — used to scope the post-rescan
        # carry-forward so an already-approved domain the author did NOT touch (e.g.
        # EA/DPDP) keeps its sign-off instead of being spuriously reverted to pending
        # by the whole-diff re-scan. Derived from the targeted findings' stamped
        # domain. If none resolve, pass None (legacy: re-evaluate every domain).
        targeted_domains = {
            (by_fp[fp].get("domain") or "").upper()
            for fp in target_fps
            if fp in by_fp and (by_fp[fp].get("domain") or "").strip()
        }
        if not targeted_domains:
            targeted_domains = None
        logger.info("[SDLC-GOV] batch fix targeted domains", run_id=run_id,
                    targeted_domains=(sorted(targeted_domains) if targeted_domains else None))

        # ── workspace + base ref (re-clone if the persisted path is gone) ──────
        workspace = _gov_ensure_workspace(run_id, ctx)
        if not workspace:
            logger.error("[SDLC-GOV] batch fix: workspace unavailable — cannot fix",
                         run_id=run_id, targets=len(target_fps))
            _resuspend(True, "workspace unavailable", 0)
            return run_id

        repo = run.get("repo") or ctx.get("repo", "")
        base_ref = ctx.get("base_commit") or ctx.get("base_branch") or "main"

        # ── scanned branch = the developer's branch governance ran against ─────
        # Governance NEVER commits onto this branch; it is the MR *target*. Resolve
        # from context (standalone → ctx["head_branch"]; end-gate → working_branch).
        # If both are empty (run triggered without an explicit head branch), fall
        # back to whatever branch the clone actually checked out — an empty value
        # was the root cause of `branch: ""` → push skipped → fix never reached
        # origin → the same findings re-reported round after round.
        scanned_branch = (ctx.get("head_branch") or ctx.get("working_branch")
                          or run.get("branch") or "")
        if not scanned_branch:
            try:
                import subprocess as _sp
                _hb = _sp.run(["git", "-C", workspace, "rev-parse", "--abbrev-ref", "HEAD"],
                              capture_output=True, text=True, timeout=30)
                _name = (_hb.stdout or "").strip() if _hb.returncode == 0 else ""
                if _name and _name != "HEAD":
                    scanned_branch = _name
            except Exception:
                pass
        if not scanned_branch:
            logger.error("[SDLC-GOV] batch fix: could not resolve the scanned branch — "
                         "cannot open a fix branch/MR", run_id=run_id)
            _resuspend(True, "scanned branch could not be resolved", 0)
            return run_id

        # Commit author identity — a fresh clone has none, so a bare commit fails and
        # gets swallowed as "no changes". Prefer the triggering user; bot fallback.
        author_email = ctx.get("user_email") or ""
        author_name  = (author_email.split("@", 1)[0] if author_email else "") or "AiNxt AI"

        # ── fix branch — topology depends on the run type ─────────────────────
        # STANDALONE governance (run.type == "governance"): the fixer's commits go
        # onto a NEW branch off the scanned branch, and the post-approval MR is opened
        # fix_branch → scanned_branch (resume_governance_fix). Keeps the developer's
        # branch untouched and the remediation independently reviewable.
        #
        # FEATURE/BUG END-GATE: the governance fix MUST stay on the working branch so
        # the downstream APPLYING/COMMITTING tail (resume_in_pipeline_governance_job)
        # carries it into the existing working_branch → base MR. Commit directly onto
        # the working (scanned) branch, as before — do NOT branch off.
        is_standalone_gov = (run.get("type") == "governance")
        if is_standalone_gov:
            fix_branch = (ctx.get("governance_fix_branch")
                          or f"governance-fix/{scanned_branch}-{run_id[:8]}")
            # Check the fix branch out BEFORE any fixer edits so every commit lands on it.
            if not _gov_prepare_fix_branch(workspace, scanned_branch, fix_branch, run_id=run_id):
                _resuspend(True, "could not create governance fix branch", 0)
                return run_id
            # Persist branch resolution so the post-approval MR step (a separate
            # process) opens fix_branch → scanned_branch without re-deriving.
            try:
                update_run_state(run_id, run.get("state") or "AWAITING_GOVERNANCE_APPROVAL",
                                 context_patch={"governance_fix_branch": fix_branch,
                                                "head_branch": scanned_branch})
            except Exception:
                pass
        else:
            fix_branch = scanned_branch

        subset = ctx.get("governance_skills")

        # Re-scan base_sha (2026-07-30): resolve the merge-base against the MR base
        # branch so the re-scan labels its range the SAME way the initial end-gate scan
        # did (base_sha...HEAD). Passing nothing defaults to "HEAD" → HEAD...HEAD +
        # `--base HEAD`, diverging from how the finding was originally produced. A
        # concrete SHA always resolves (unlike a bare/origin branch name in a shallow
        # clone). `_gov_git_diff` already uses three-dot base_ref...HEAD for the diff
        # body, so only the scan's base_sha LABEL needed fixing.
        _base_branch_name = ctx.get("base_branch") or "main"

        def _gov_merge_base(ref: str) -> str:
            import subprocess as _sp
            try:
                _r = _sp.run(["git", "-C", workspace, "merge-base", ref, "HEAD"],
                             capture_output=True, text=True, timeout=30)
                return (_r.stdout or "").strip() if _r.returncode == 0 else ""
            except Exception:
                return ""

        rescan_base_sha = (_gov_merge_base(f"origin/{_base_branch_name}")
                           or _gov_merge_base(_base_branch_name)
                           or base_ref or "HEAD")
        logger.info("[SDLC-GOV] batch fix workspace + base resolved", run_id=run_id,
                    workspace=workspace, scanned_branch=scanned_branch,
                    fix_branch=fix_branch, base_ref=base_ref, rescan_base_sha=rescan_base_sha)

        # product_id (best-effort; scan primitive tolerates None).
        product_id = None
        try:
            from db.database import SessionLocal as _SL
            from agents.sdlc_governance import engine as _gov_engine
            _db = _SL()
            try:
                product_id = _gov_engine.resolve_product_id(_db, repo)
            finally:
                _db.close()
        except Exception:
            product_id = None

        add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_STARTED", actor=actor,
                      output=f"Batch fix requested for {len(target_findings)} finding(s)")
        # Mark the run as actively re-scanning so the UI shows "fixing…" and gates
        # "Send to governance teams". Cleared by _resuspend / the except handlers.
        try:
            update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                             current_stage="GOVERNANCE_SCAN",
                             context_patch={"governance_rescanning": True})
        except Exception:
            pass

        from agents.sdlc_cli_engine import run_cli, CliEngineConfig
        from agents.sdlc_cli_budget import record_cli_usage, is_exhausted
        from agents.sdlc_governance.config import fix_model

        max_iters = gov_config.max_iters()
        stall_limit = gov_config.convergence_stall_limit()

        # Baseline observed set = the current (initial) scan's findings. The legacy
        # findings table has not been mutated by a re-scan yet, so it reflects the
        # end-gate snapshot's detections.
        prev_observed = {r.get("fingerprint") for r in (list_findings(run_id) or [])
                         if r.get("fingerprint")}
        prev_hash = None
        prev_open_count = None
        stall = 0
        resolved = False
        stop_reason = ""
        iteration = 0
        last_open_count = len(prev_observed)
        confirmed_targets = set()   # target fps proven gone (marked fix_confirmed)

        for iteration in range(1, max_iters + 1):
            if is_exhausted(run_id):
                stop_reason = "HOD per-run budget exhausted"
                break

            fix_res = run_cli(
                config=CliEngineConfig.from_env(), workspace_root=workspace,
                prompt=_gov_engine_build_fix_prompt(target_findings, workspace),
                profile="code", model=fix_model(),
                max_turns=gov_config.review_turns(), run_id=run_id,
            )
            try:
                record_cli_usage(run_id, fix_res.usage or {}, fix_res.total_cost_usd or 0.0)
            except Exception:
                pass
            if getattr(fix_res, "status", "") == "suspended":
                stop_reason = f"fixer suspended: {getattr(fix_res, 'reason', '')}"
                break

            # Commit + push the fixer's working-tree changes BEFORE re-diffing so the
            # committed-only re-scan diff sees them and the fix reaches origin/fix_branch.
            # "nothing to commit" → the fixer changed nothing → STOP (no empty MR,
            # no infinite loop). Commits land on the governance fix_branch (never the
            # developer's scanned branch) with an explicit author identity.
            committed, pushed = _gov_commit_and_push(
                workspace, fix_branch, run_id=run_id,
                message=f"[AiNxt AI] governance auto-fix (iter {iteration}) — {len(target_findings)} finding(s)",
                author_name=author_name, author_email=author_email,
            )
            logger.info("[SDLC-GOV] batch fix commit/push", run_id=run_id,
                        iteration=iteration, committed=bool(committed),
                        pushed=bool(pushed), branch=fix_branch)
            if not committed:
                stop_reason = "fixer produced no changes"
                logger.warning("[SDLC-GOV] batch fix stop — fixer produced no changes",
                               run_id=run_id, iteration=iteration, stop_reason=stop_reason)
                break

            # FAIL-CLOSED: the fixer committed but the push to origin did NOT land
            # (an auth/transport failure). The in-session re-scan below runs on the
            # LOCAL commit and could look green, but any later re-clone
            # (_gov_ensure_workspace) pulls origin WITHOUT the fix → the finding
            # silently returns "for the second time" and the MR carries nothing. Stop
            # now with an actionable reason instead of trusting an unverifiable re-scan.
            if not pushed:
                stop_reason = (
                    f"fix committed but not pushed to origin/'{fix_branch}' "
                    "(re-scan would not see the fix after re-clone)"
                )
                logger.error("[SDLC-GOV] batch fix stop — commit not pushed to origin",
                              run_id=run_id, iteration=iteration, branch=fix_branch,
                              stop_reason=stop_reason)
                break

            # Re-diff the workspace (fixer's real changes, now committed) + NEW scan snapshot.
            changed_files, diff_text = _gov_git_diff(workspace, base_ref)
            res = run_governance_scan_snapshot(
                run_id, workspace=workspace, diff_text=diff_text,
                changed_files=changed_files, product_id=product_id, repo=repo,
                base_sha=rescan_base_sha, subset=subset, trigger="rescan",
                created_by=actor,
            )
            snapshot_id = res.get("snapshot_id")

            # FAIL-CLOSED guard (review finding #2): a re-scan that could NOT complete
            # (CLI error/timeout) or resolved no bundle returns scan_error/skipped with
            # open_findings=[] and snapshot_id=None. Treating that as the state below
            # would compute disappeared = prev_observed − ∅ = ALL findings and mark
            # every open finding fix_confirmed — hiding real, unresolved findings from
            # governance. Instead STOP the loop and re-suspend for a human retry;
            # findings stay visible (nothing is marked fix_confirmed on an errored scan).
            if res.get("scan_error") or res.get("skipped") or not snapshot_id:
                stop_reason = (
                    "re-scan could not complete (CLI error/timeout)"
                    if res.get("scan_error") else
                    "re-scan resolved no governance bundle/skills"
                    if res.get("skipped") else
                    "re-scan produced no snapshot"
                )
                break

            # B2.5 — per-domain, fingerprint-granular approval carry-forward on the
            # NEW snapshot: approved domains with no new/changed findings stay
            # approved (their accepts copied forward); a domain that gained a
            # new/changed finding reverts to 'pending' so only that finding blocks
            # the B2.4 gate. Strictly per-domain — never invalidate-all. Fail-safe.
            if snapshot_id:
                try:
                    from store.sdlc_governance_approvers import evaluate_carry_forward
                    evaluate_carry_forward(run_id, snapshot_id,
                                           targeted_domains=targeted_domains)
                except Exception as _cf:
                    logger.warning("[SDLC-GOV] carry-forward eval failed — non-fatal",
                                   run_id=run_id, error=str(_cf))

            # Findings that disappeared since the prior snapshot → fix_confirmed.
            cur_observed = _observed_from_res(res)
            disappeared = prev_observed - cur_observed
            if disappeared:
                set_disposition(run_id, list(disappeared), "fix_confirmed", actor)
            prev_observed = cur_observed

            # Track which TARGETS are now proven gone (marked fix_confirmed above).
            for fp in target_fps:
                if fp not in cur_observed:
                    confirmed_targets.add(fp)

            open_hash, open_count = _open_hash(snapshot_id)
            last_open_count = open_count
            logger.info("[SDLC-GOV] batch fix iteration", run_id=run_id,
                        iteration=iteration, open_count=open_count, open_set_hash=open_hash,
                        targets_confirmed=len(confirmed_targets), targets_total=len(target_fps))

            # All requested findings resolved (none still detected) → done.
            if all(fp not in cur_observed for fp in target_fps):
                resolved = True
                stop_reason = "all requested findings resolved"
                break

            # Convergence guards.
            if prev_hash is not None and open_hash == prev_hash:
                stop_reason = "open-set hash repeated (no progress)"
                break
            if prev_open_count is not None and open_count >= prev_open_count:
                stall += 1
                if stall >= stall_limit:
                    stop_reason = "open count not strictly decreasing"
                    break
            else:
                stall = 0
            prev_hash = open_hash
            prev_open_count = open_count
        else:
            stop_reason = "max iterations reached"

        # Re-seed per-domain approvals (idempotent) so any newly-cleared / newly-
        # surfaced domains are reflected before the author keeps triaging.
        try:
            from store.sdlc_governance_approvers import seed_domain_approvals
            seed_domain_approvals(run_id, domain_open_counts(run_id))
        except Exception:
            pass

        if resolved:
            logger.info("[SDLC-GOV] batch fix resolved all targets", run_id=run_id,
                        targets=len(target_fps), open_count=last_open_count)
            _resuspend(False, "", last_open_count)
            add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_DONE", actor=actor,
                          output=f"Fix confirmed for {len(target_fps)} finding(s) — awaiting approval")
        else:
            # Auto-fix could not confirm every requested finding. Any target still
            # parked at `fix_requested` would strand the gate (no author actions,
            # excluded from the team-visible set, perpetual "re-scanning…" inference).
            # Reset every UNCONFIRMED target to `open` so the author can retry, mark it
            # a false positive, or send it on for manual review. Confirmed targets keep
            # fix_confirmed. The attempt is preserved via the AUTHOR_FIX_STOPPED event.
            still_open = [fp for fp in target_fps if fp not in confirmed_targets]
            if still_open:
                try:
                    set_disposition(run_id, still_open, "open", actor)
                except Exception as _rd:
                    logger.warning("[SDLC-GOV] batch fix: could not reset unconfirmed targets — non-fatal",
                                   run_id=run_id, error=str(_rd))
            logger.warning("[SDLC-GOV] batch fix stopped — not fully converged", run_id=run_id,
                           iteration=iteration, reason=stop_reason, open_count=last_open_count,
                           confirmed=len(confirmed_targets), reset_to_open=len(still_open))
            _resuspend(True, stop_reason, last_open_count)
            add_run_event(run_id, "GOVERNANCE_SCAN", "AUTHOR_FIX_STOPPED", actor=actor,
                          output=f"Auto-fix stopped ({stop_reason}) — {len(confirmed_targets)}/{len(target_fps)} resolved")
        return run_id

    except SDLCCancelled:
        logger.info("[SDLC-GOV] run_governance_batch_fix stopped — run cancelled",
                    run_id=run_id)
        # Clear the rescanning flag so a cancelled batch doesn't leave the UI spinning.
        try:
            update_run_state(run_id, "AWAITING_GOVERNANCE_APPROVAL",
                             current_stage="GOVERNANCE_SCAN",
                             context_patch={"governance_rescanning": False})
        except Exception:
            pass
        return run_id
    except Exception as e:
        logger.error("[SDLC-GOV] run_governance_batch_fix failed", run_id=run_id,
                     targets=len(target_fps), error=str(e))
        # Anti-strand reset: a crashed fixer must not leave targets parked at
        # `fix_requested` (perpetual spinner + no author actions). Reopen them and
        # clear the rescanning flag so the author retains a forward path.
        try:
            from store.sdlc_governance_findings import set_disposition as _reset_disp
            _reset_disp(run_id, target_fps, "open", actor)
        except Exception:
            pass
        try:
            update_run_state(
                run_id, "AWAITING_GOVERNANCE_APPROVAL", current_stage="GOVERNANCE_SCAN",
                context_patch={"governance_rescanning": False,
                               "governance_not_converging": True,
                               "governance_not_converging_reason": f"batch fix error: {e}"},
            )
        except Exception:
            pass
        return run_id


def run_governance_author_fix(run_id: str, fingerprint: str, actor: str = "user") -> str:
    """Back-compat single-finding wrapper → delegates to run_governance_batch_fix."""
    return run_governance_batch_fix(run_id, [fingerprint], actor)


def _gov_engine_build_fix_prompt(findings, workspace: str) -> str:
    """Build the governance fixer prompt for one OR MORE findings. Reuses
    agents.sdlc_governance.engine.build_fix_prompt (which expects a list); accepts a
    single Finding or a list/tuple for back-compat."""
    from agents.sdlc_governance.engine import build_fix_prompt
    fl = list(findings) if isinstance(findings, (list, tuple)) else [findings]
    return build_fix_prompt(fl, workspace)


# ============================================================
# IN-PIPELINE GOVERNANCE RESUME (2026-07-21; reworked 2026-07-23 B2.6)
#
# Resume a feature/bug run suspended at AWAITING_GOVERNANCE_APPROVAL.
# Distinct from resume_governance_fix (standalone governance pipeline
# path that reads head_branch from context).
#
# END-GATE OVERHAUL (2026-07-23): governance now runs AFTER COMMITTING +
# a DRAFT MR. On all-domains-approved there is NOTHING left to build or
# apply — the change is already committed. This resume therefore just
# UN-DRAFTS the MR (makes it mergeable) and composes into the EXISTING
# AWAITING_PR_APPROVAL gate. It NO LONGER re-enters the state machine to
# re-run the pre-apply fixer / APPLYING (that was the OLD mid-pipeline
# gate); end-gate fixes happen in the author remediation loop
# (run_governance_author_fix) BEFORE approval.
# ============================================================

def resume_in_pipeline_governance_approval(run_id: str, actor: str = "user") -> str:
    """Resume a feature/bug run suspended at AWAITING_GOVERNANCE_APPROVAL.

    End-gate model (B2.6): when every seeded domain is approved, flip the committed
    DRAFT MR to mergeable and transition the run to the EXISTING
    ``AWAITING_PR_APPROVAL`` gate — governance approval PRECEDES PR approval, two
    terminal gates composed in that order. Does NOT re-run IMPLEMENT / APPLYING /
    TEST_VERIFY (the code is already committed on the branch).

    Fail-closed: re-verifies ``all_finding_domains_approved`` before advancing (stays
    suspended otherwise). MR un-draft failures are non-fatal (logged) — a GitLab
    hiccup must not strand the run. Returns run_id in all paths.
    """
    bind_context(correlation_id=run_id, pipeline_stage="sdlc_governance_in_pipeline_resume")

    from store.sdlc_governance_approvers import all_finding_domains_approved, list_domain_approvals

    run = get_run(run_id)
    if not run:
        logger.error("[SDLC-GOV] resume_in_pipeline_governance_approval: run not found",
                     run_id=run_id)
        return run_id

    # Fail-closed guard — every seeded domain must be 'approved' before we unblock.
    if not all_finding_domains_approved(run_id):
        pending = [d["domain"] for d in (list_domain_approvals(run_id) or [])
                   if d.get("status") != "approved"]
        logger.warning("[SDLC-GOV] resume_in_pipeline_governance_approval: not all domains approved",
                       run_id=run_id, pending=pending)
        return run_id

    try:
        ctx  = run.get("context") or {}
        repo = run.get("repo") or ctx.get("repo", "")

        # Resolve the MR iid (pr_number IS the iid) + branch + pr_url from the run
        # row, falling back to the COMMITTING artifact {branch, mr_url, pr_number}.
        pr_number = run.get("pr_number")
        branch    = run.get("branch") or ctx.get("working_branch") or ""
        pr_url    = run.get("pr_url") or ""
        if not pr_number or not branch or not pr_url:
            try:
                from store.sdlc_artifacts import _load_latest_artifact
                _art = (_load_latest_artifact(run_id, "COMMITTING") or {}).get("payload") or {}
                pr_number = pr_number or _art.get("pr_number")
                branch    = branch or _art.get("branch") or ""
                pr_url    = pr_url or _art.get("mr_url") or ""
            except Exception:
                pass

        # Latest scan snapshot id — for the audit log line only (best-effort).
        snapshot_id = None
        try:
            from store.sdlc_governance_findings import latest_snapshot
            snapshot_id = (latest_snapshot(run_id) or {}).get("id")
        except Exception:
            snapshot_id = None

        # Flip the DRAFT MR to mergeable — best-effort, non-fatal on any GitLab hiccup.
        gitlab_repo = _resolve_gitlab_repo(repo) if repo else repo
        if pr_number:
            try:
                from core.config import SCM_PROVIDER as _SCM
                if _SCM == "github":
                    from tools.github_tools import github_set_pr_draft as _set_draft
                else:
                    from tools.gitlab_tools import gitlab_set_mr_draft as _set_draft
                _set_draft(gitlab_repo, pr_number, draft=False)
            except Exception as _ue:
                logger.warning("[SDLC-GOV] resume: MR undraft failed (non-fatal)",
                               run_id=run_id, mr_iid=pr_number, error=str(_ue))
        else:
            logger.warning("[SDLC-GOV] resume: no MR iid on run — cannot un-draft",
                           run_id=run_id)

        # Compose into the EXISTING PR-approval gate (governance precedes PR approval).
        update_run_state(run_id, "AWAITING_PR_APPROVAL",
                         branch=(branch or None), pr_number=pr_number,
                         pr_url=(pr_url or None))
        add_run_event(
            run_id, "GOVERNANCE_SCAN", "AWAITING_PR_APPROVAL", actor=actor,
            output="All governance domains approved — MR unblocked, awaiting PR approval",
        )
        logger.info("[SDLC-GOV] MR unblocked after all domains approved",
                    run_id=run_id, mr_iid=pr_number, snapshot_id=snapshot_id)
        return run_id

    except SDLCCancelled:
        raise
    except Exception as e:
        logger.error("[SDLC-GOV] resume_in_pipeline_governance_approval failed",
                     run_id=run_id, error=str(e))
        update_run_state(run_id, "FAILED", error=str(e))
        return run_id


def cleanup_abandoned_governance_mr(run_id: str, actor: str = "system",
                                    delete_branch: bool = False) -> None:
    """Best-effort cleanup for a run CANCELLED/abandoned while a governance end-gate
    DRAFT MR + committed branch exist (B2.6).

    Closes the draft MR and (optionally) deletes the abandoned source branch so a
    cancelled run does not leave an un-mergeable draft MR dangling. There is no
    public GitLab close-MR helper, so the MR is closed with an inline
    ``state_event=close`` PUT reusing ``tools.gitlab_tools``' existing request
    pattern (no new API surface added). Branch deletion is OFF by default
    (conservative — abandoned branches are cheap and the committer may want to
    recover the work); pass ``delete_branch=True`` to reap it via the existing
    ``gitlab_delete_branch`` helper.

    Idempotent + fail-safe: no-op when the run has no MR; a GitLab hiccup is logged,
    never re-raised (must not re-enter / strand the cancel path). The per-Jira dedup
    slot is released by the existing cancel machinery (routers.cancel_run /
    worker _release_slot) — NOT re-done here.
    """
    try:
        run = get_run(run_id) or {}
    except Exception:
        return
    ctx  = run.get("context") or {}
    repo = run.get("repo") or ctx.get("repo", "")

    pr_number = run.get("pr_number")
    branch    = run.get("branch") or ctx.get("working_branch") or ""
    if not pr_number or not branch:
        try:
            from store.sdlc_artifacts import _load_latest_artifact
            _art = (_load_latest_artifact(run_id, "COMMITTING") or {}).get("payload") or {}
            pr_number = pr_number or _art.get("pr_number")
            branch    = branch or _art.get("branch") or ""
        except Exception:
            pass

    if not pr_number:
        # Nothing committed / no draft MR for this run → nothing to clean.
        return

    gitlab_repo = _resolve_gitlab_repo(repo) if repo else repo

    # 1. Close the draft MR/PR (best-effort, non-fatal).
    try:
        from core.config import SCM_PROVIDER as _SCM
        if _SCM == "github":
            # GitHub: PATCH /repos/{repo}/pulls/{number} with state=closed
            from tools.github_tools import _patch as _gh_patch
            _res = _gh_patch(f"/repos/{gitlab_repo}/pulls/{pr_number}", {"state": "closed"})
        else:
            from tools.gitlab_tools import _put as _gl_put, _proj as _gl_proj
            _res = _gl_put(
                f"/projects/{_gl_proj(gitlab_repo)}/merge_requests/{pr_number}",
                {"state_event": "close"},
            )
        if isinstance(_res, dict) and _res.get("error"):
            logger.warning("[SDLC-GOV] abandoned draft MR close returned error (non-fatal)",
                           run_id=run_id, mr_iid=pr_number, error=str(_res.get("error")))
    except Exception as _ce:
        logger.warning("[SDLC-GOV] abandoned draft MR close failed (non-fatal) — orphan MR",
                       run_id=run_id, mr_iid=pr_number, branch=branch, error=str(_ce))

    # 2. Optionally reap the abandoned source branch (existing helper, best-effort).
    if delete_branch and branch:
        try:
            from core.config import SCM_PROVIDER as _SCM
            if _SCM == "github":
                from tools.github_tools import github_delete_branch as _del_branch
            else:
                from tools.gitlab_tools import gitlab_delete_branch as _del_branch
            _del_branch(gitlab_repo, branch)
        except Exception as _be:
            logger.warning("[SDLC-GOV] abandoned branch delete failed (non-fatal)",
                           run_id=run_id, branch=branch, error=str(_be))

    logger.info("[SDLC-GOV] abandoned draft MR cleaned on cancel",
                run_id=run_id, mr_iid=pr_number, branch=branch)
