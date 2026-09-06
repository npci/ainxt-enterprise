# SPDX-License-Identifier: MIT
"""Centralised environment / LLM config helpers (extracted from app/main.py)."""
import os
from typing import Optional

from app.models import LLMConfig, LLMProvider


_TRUTHY_ENV = {"1", "true", "yes", "on"}


def env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean feature-flag env var (1/true/yes/on, case-insensitive)."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in _TRUTHY_ENV


def llm_proxy_root() -> str:
    """Return the platform llm_proxy ROOT URL (no ``/v1`` suffix), or "".

    Operators occasionally paste the proxy URL with a trailing ``/v1``
    (matching the OpenAI-compatible surface they see in the docs). If we
    then re-append ``/v1`` (or call ``GET {base}/v1/models``) the request
    lands on ``…/v1/v1/…``, which the proxy doesn't expose, surfacing as
    ``NotFoundError: 404 {'detail': 'Not Found'}`` after 5 retries.
    Normalising once here makes every caller idempotent without breaking
    the documented convention (``LLM_PROXY_URL=http://your-llm-proxy:8003``).
    """
    base = os.getenv("LLM_PROXY_URL", "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-len("/v1")].rstrip("/")
    return base


def _llm_proxy_openai_base() -> str:
    """Return ``{llm_proxy_root}/v1`` when configured, else "".

    The platform llm_proxy service exposes an OpenAI-compatible surface at
    ``/v1`` (``/v1/chat/completions``, ``/v1/models``, …). When configured,
    it should be the single source of truth for ALL agent LLM calls — both
    model discovery (``generation.py::list_llm_models``) and runtime
    inference (``native_engine.py::_run_agent`` via the OpenAI SDK).
    Local-only fallbacks (``LOCAL_LLM_BASE_URL`` / ``OPENAI_COMPATIBLE_BASE_URL``)
    are kept so standalone ABStudio dev still works against Ollama, but in
    any environment where ``LLM_PROXY_URL`` is set it must win — otherwise
    the Agent Configuration picker shows proxy-backed models the orchestrator
    has no way to invoke (SIT failure mode: orchestrator LLM unreachable
    after retries against the localhost fallback).
    """
    root = llm_proxy_root()
    return f"{root}/v1" if root else ""


def _llm_proxy_token() -> str:
    return os.getenv("LLM_PROXY_TOKEN", "").strip()


def openai_compatible_base_url() -> str:
    # _llm_proxy_openai_base() already appends /v1 explicitly. The two env
    # fallbacks are commonly set to a bare host (e.g. LOCAL_LLM_BASE_URL=
    # http://host.docker.internal:11434), so normalise those the same way
    # app.core.llm_handler._local_llm_base_url() does — an un-normalised bare
    # host here made every call using it 404 on /chat/completions (missing
    # the required /v1 prefix) instead of /v1/chat/completions.
    resolved = _llm_proxy_openai_base()
    if resolved:
        return resolved
    raw = (os.getenv("OPENAI_COMPATIBLE_BASE_URL") or os.getenv("LOCAL_LLM_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return "http://localhost:11434/v1"
    from urllib.parse import urlsplit
    path_segments = urlsplit(raw).path.strip("/").split("/")
    return raw if "v1" in path_segments else f"{raw}/v1"


def openai_compatible_api_key() -> str:
    # When routing through llm_proxy the OpenAI SDK still needs a non-empty
    # ``api_key`` argument (the real auth is the ``X-Internal-Token`` header
    # injected by ``llm_handler.OpenAIClient``). Prefer the proxy token here
    # so logs / telemetry don't show ``"not-needed"`` when the orchestrator
    # is actually calling the platform proxy.
    return (
        os.getenv("OPENAI_COMPATIBLE_API_KEY")
        or os.getenv("LOCAL_LLM_API_KEY")
        or _llm_proxy_token()
        or "not-needed"
    )


def factory_base_url() -> str:
    return (
        os.getenv("FACTORY_BASE_URL")
        or _llm_proxy_openai_base()
        or os.getenv("LOCAL_LLM_BASE_URL")
        or "http://localhost:11434/v1"
    )


def factory_api_key() -> str:
    return (
        os.getenv("FACTORY_API_KEY")
        or os.getenv("LOCAL_LLM_API_KEY")
        or _llm_proxy_token()
        or "not-needed"
    )


def factory_model() -> str:
    """Model that runs the factory itself (clarification / structure
    generation) and, via ``verifier_model()``/``triage_model()``/the many
    ``factory_model()`` call sites across the engine, the platform-wide
    fallback whenever nothing more specific picks a model.

    Resolution order: explicit env override → the admin's configured default
    in core.llm_provider_registry (preferring a free/self-hosted model, since
    this is a cheap, frequent orchestration call, not a specific user-facing
    generation) → "". Previously this fell through to ``LOCAL_LLM_MODEL``,
    an env var nothing in this deployment sets (the documented one is
    ``LOCAL_LLM_MODEL_NAME``) — so on any install configured purely through
    the "LLM Providers" screen this returned "", which is what made
    "Create with AI" 400 (an empty ``model`` field sent to Ollama).
    """
    explicit = os.getenv("FACTORY_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from core.llm_provider_registry import get_default_model_id
        default_id = get_default_model_id(prefer_free=True)
        if default_id:
            return default_id
    except Exception as exc:
        from core.logger import logger
        logger.warning(f"[FACTORY] llm_provider_registry unavailable, falling back "
                        f"to env-var model resolution: {exc}")
    return os.getenv("LOCAL_LLM_MODEL", "").strip() or os.getenv("CLAUDE_PRIMARY_MODEL", "").strip()


def factory_agent_model() -> str:
    """Default model baked into agent nodes GENERATED by the workflow factory.

    This is deliberately distinct from ``factory_model()``: ``factory_model()``
    is the *meta* model that runs the factory itself (clarification / structure
    generation, typically an in-house SKU like qwen), whereas this is the model
    the *generated agents* will run on at execution time — should prefer a
    strong instruction-following model, not necessarily the cheapest one.

    Resolution order: ``ABSTUDIO_AGENT_DEFAULT_MODEL`` override → the admin's
    configured default in core.llm_provider_registry → ``factory_model()`` as
    a last resort (so this is never blank as long as at least one model is
    configured). A user who names a model in the factory chat overrides this
    per-run (see ``workflow_factory/pipeline.py`` ``preferred_model`` handling).
    """
    explicit = os.getenv("ABSTUDIO_AGENT_DEFAULT_MODEL", "").strip()
    if explicit:
        return explicit
    try:
        from core.llm_provider_registry import get_default_model_id
        default_id = get_default_model_id(prefer_free=False)
        if default_id:
            return default_id
    except Exception as exc:
        from core.logger import logger
        logger.warning(f"[FACTORY] llm_provider_registry unavailable, falling back "
                        f"to factory_model(): {exc}")
    return factory_model()


# ---------------------------------------------------------------------------
# Uploaded-document (attachment) handling in the workflow engine
# ---------------------------------------------------------------------------
# Controls the size-aware injection of an uploaded document into agent
# prompts. No RAG/KB: the already-extracted parsed_text is injected verbatim.
#   - Small doc (char_count <= threshold): injected into the FIRST agent only.
#   - Big doc   (char_count >  threshold): injected into EVERY agent, so the
#     document reaches every node through the end of the workflow rather than
#     degrading into the previous agent's paraphrase.

def doc_inline_threshold_chars() -> int:
    """char_count boundary between "small" and "big" uploaded documents.

    Default = 40_000 chars. At/below → first agent only; above → every agent.
    Override via ``ABSTUDIO_DOC_INLINE_THRESHOLD_CHARS``.
    """
    try:
        return int(os.getenv("ABSTUDIO_DOC_INLINE_THRESHOLD_CHARS", "40000"))
    except ValueError:
        return 40000


def doc_agent_budget_chars() -> int:
    """Hard clip on the document section injected into a single agent prompt.

    Guards the model context window even when a big document is injected into
    every agent. The upstream extraction pipeline already caps text at 60k
    chars; this trims the per-agent injected section (across all docs) and
    appends a truncation note. Default = 48_000 chars. Override via
    ``ABSTUDIO_DOC_AGENT_BUDGET_CHARS``.
    """
    try:
        return int(os.getenv("ABSTUDIO_DOC_AGENT_BUDGET_CHARS", "48000"))
    except ValueError:
        return 48000


def fill_blank_llm_fields(
    cfg: dict, *, base_url: str, api_key: str, model_name: str,
) -> dict:
    """Populate ``base_url`` / ``api_key`` / ``model_name`` on ``cfg`` only
    when the current value is missing or whitespace. Mutates and returns
    ``cfg`` for chaining. Empty-string treated as unset (the engine's LLM
    dict commonly carries ``""`` for absent fields, not ``None``).
    """
    if not (cfg.get("base_url") or "").strip():
        cfg["base_url"] = base_url
    if not (cfg.get("api_key") or "").strip():
        cfg["api_key"] = api_key
    if not (cfg.get("model_name") or "").strip():
        cfg["model_name"] = model_name
    return cfg


def postgres_enabled() -> bool:
    """Whether ABStudio persistence (Postgres) is available.

    ABStudio now shares the platform's single pool (``db.database.engine``), so
    it is backed by Postgres whenever the platform is — i.e. whenever
    ``POSTGRES_HOST`` is set. When unset, ABStudio degrades to its in-memory /
    file stores. This is the single gate every "Postgres-vs-file" call site
    checks; the old per-namespace ``AGENTCHAIN_POSTGRES_*`` vars are gone.
    """
    return bool(os.getenv("POSTGRES_HOST", "").strip())


def agentchain_postgres_uri() -> str:
    """Deprecated compatibility shim — prefer ``postgres_enabled()``.

    Historically built an ``AGENTCHAIN_POSTGRES_*`` URI that drove ABStudio's
    own pool. ABStudio no longer opens a pool (it borrows from the shared
    platform engine), so no connection string is needed. Callers only ever
    tested this for truthiness to pick Postgres vs file stores; it now returns a
    non-empty sentinel iff ``postgres_enabled()``. Retained so any lingering
    ``if agentchain_postgres_uri()`` keeps working; slated for removal.
    """
    return "postgres" if postgres_enabled() else ""


def build_meta_llm_config(max_tokens: int = 1024, temperature: float = 0.7) -> LLMConfig:
    return LLMConfig(
        provider=LLMProvider.CUSTOM,
        api_key=factory_api_key(),
        model_name=factory_model(),
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=1.0,
        base_url=factory_base_url(),
    )


# ---------------------------------------------------------------------------
# Loop Engineering (P1) — env helpers
# ---------------------------------------------------------------------------
# Added in Phase 1 per docs/loop-engineering/PHASE_1_FOUNDATIONS.md §10.
# Defined here so the schema/api surface is stable; most are *consumed* only
# from P2 onward (BudgetMeter, VerifierAgent). Keeping them in one block
# matches the rest of the file's "one helper per env var" convention so
# nothing else has to learn a new pattern.

def budget_defaults() -> dict:
    """Default outer-loop budget caps. Honoured from P2.

    Returns a dict with ``tokens`` / ``wall_clock_s`` / ``max_iterations``.
    Used only on ad-hoc ``/run-stream`` goal-mode runs where the caller did
    not supply explicit budget — saved Loops always carry their own
    ``stopping_condition`` (validated at create time per FR-1.7).
    """
    def _int(env: str, default: int) -> int:
        try:
            return int(os.getenv(env, str(default)))
        except ValueError:
            return default
    return {
        "tokens":         _int("BUDGET_DEFAULT_TOKENS", 200_000),
        "wall_clock_s":   _int("BUDGET_DEFAULT_WALL_CLOCK_S", 3600),
        "max_iterations": _int("BUDGET_DEFAULT_MAX_ITERATIONS", 10),
    }


def verifier_model() -> str:
    """LLM model name for the P4 ``VerifierAgent``. Falls through to
    ``factory_model()`` when unset so an operator who hasn't configured a
    dedicated verifier model still gets a working independent pre-ship
    check on the same SKU the maker uses."""
    return os.getenv("VERIFIER_MODEL", "").strip() or factory_model()


def verifier_temperature() -> float:
    """Low default temperature so the verifier verdict is reproducible."""
    try:
        return float(os.getenv("VERIFIER_TEMPERATURE", "0.2"))
    except ValueError:
        return 0.2


def verifier_max_tokens() -> int:
    """Output cap on the VerifierAgent completion (P4).

    Default = 4096 — large enough to fit the JSON verdict block (verdict,
    risk_class, reasons[], confidence, evidence[]) with comfortable
    headroom for the reason strings, but small enough to keep the
    pre-ship gate cheap. Override via ``VERIFIER_MAX_TOKENS``.
    """
    try:
        return int(os.getenv("VERIFIER_MAX_TOKENS", "4096"))
    except ValueError:
        return 4096


def verifier_timeout_s() -> int:
    """Wall-clock cap for one VerifierAgent call (P4).

    Default = 90 seconds. The runner wraps the verifier call in
    ``asyncio.wait_for(..., timeout=verifier_timeout_s())`` and treats a
    timeout as ``INCONCLUSIVE`` (which the runner in turn treats as FAIL
    per PHASE_4_VERIFIER.md §6.3). Override via ``VERIFIER_TIMEOUT_S``.
    """
    try:
        return int(os.getenv("VERIFIER_TIMEOUT_S", "90"))
    except ValueError:
        return 90


def verifier_debug() -> bool:
    """When True, surface the verifier's raw_response on the verdict API.

    Off by default so the verifier's chain-of-thought never leaks into
    operator dashboards or audit exports. PHASE_4_VERIFIER.md §6.5.
    """
    return os.getenv("VERIFIER_DEBUG", "false").strip().lower() != "false"


def loop_triage_enabled() -> bool:
    """Master switch for ``TriageSkill`` cron rows (P5)."""
    return os.getenv("LOOP_TRIAGE_ENABLED", "true").strip().lower() != "false"


def loop_reflection_inject_top_k() -> int:
    """Top-K reflections injected into next run's prompt (P5). Default = 3."""
    try:
        return int(os.getenv("LOOP_REFLECTION_INJECT_TOP_K", "3"))
    except ValueError:
        return 3


def loop_reflection_max_chars() -> int:
    """Cap on the injected reflection prompt section (P5). Default = 1500."""
    try:
        return int(os.getenv("LOOP_REFLECTION_MAX_CHARS", "1500"))
    except ValueError:
        return 1500


def loop_degradation_inbox_enabled() -> bool:
    """Route non-shipped outcomes to inbox (P5). Default = True."""
    return os.getenv("LOOP_DEGRADATION_INBOX_ENABLED", "true").strip().lower() != "false"


# ---------------------------------------------------------------------------
# P5 — Triage + Reflection + Memory tunables
# ---------------------------------------------------------------------------
# These were added when PHASE_5_TRIAGE_REFLEXION.md was implemented; the
# pre-existing ``loop_triage_enabled`` / ``loop_reflection_*`` helpers above
# are intentionally kept (older callers consume them) — the helpers below
# are the names called for by the phase spec and the surfaces newly
# introduced in P5 (TriageSkill / ReflectionWriter / MemoryReadHandler).
# Naming follows the rest of the file: ``<env-var-lowercased>()``.


def triage_interval_cron() -> str:
    """Cron expression APScheduler uses to fire each loop's TriageSkill.

    Default = "*/30 * * * *" (every 30 minutes, IST). Override per
    deployment with ``TRIAGE_INTERVAL_CRON``. The string is passed
    verbatim to ``CronTrigger.from_crontab`` so any 5-field cron string
    APScheduler accepts is valid here.
    """
    return os.getenv("TRIAGE_INTERVAL_CRON", "*/30 * * * *").strip() or "*/30 * * * *"


def triage_max_inbox_items() -> int:
    """Hard cap on the number of inbox items one TriageSkill run will
    consider. Default = 50; SRS-mandated absolute ceiling is 200, enforced
    inside the skill regardless of this value.
    """
    try:
        return int(os.getenv("TRIAGE_MAX_INBOX_ITEMS", "50"))
    except ValueError:
        return 50


def triage_model() -> Optional[str]:
    """LLM model name the TriageSkill summariser should use.

    Returns ``None`` when unset — TriageSkill falls through to
    ``factory_model()`` in that case so a fresh install gets a working
    triage prompt without bespoke configuration.
    """
    raw = os.getenv("TRIAGE_MODEL", "").strip()
    return raw or None


def triage_include_log_alerts() -> bool:
    """Whether to scan platform log alerts as part of the triage inbox.

    Off by default in v1 — there's no canonical log-alert source in
    ABStudio yet, so the helper exists only so a future operator can
    flip the source on without code change.
    """
    return os.getenv("TRIAGE_INCLUDE_LOG_ALERTS", "false").strip().lower() != "false"


def reflection_top_n() -> int:
    """Top-N most recent reflections injected into the maker's prompt by
    the MemoryReadHandler. Default = 5. The handler still enforces an
    overall character cap via ``memory_inject_max_tokens`` even if N is
    large, so this is purely the *fetch* size.
    """
    try:
        return int(os.getenv("REFLECTION_TOP_N", "5"))
    except ValueError:
        return 5


def reflection_max_tokens() -> int:
    """Cap on the LLM's reflection-derivation completion. Default = 256.

    The full DB column is capped at 2000 chars (validated by Pydantic);
    this is the *generation* cap so each reflection LLM call stays cheap.
    """
    try:
        return int(os.getenv("REFLECTION_MAX_TOKENS", "256"))
    except ValueError:
        return 256


def memory_inject_max_tokens() -> int:
    """Approximate character budget (≈ 4 chars / token) for the lesson +
    digest payload injected into the maker prompt by MemoryReadHandler.
    Default = 1200 tokens. The handler drops the digest before
    truncating lessons so the most recent lesson always survives.
    """
    try:
        return int(os.getenv("MEMORY_INJECT_MAX_TOKENS", "1200"))
    except ValueError:
        return 1200
