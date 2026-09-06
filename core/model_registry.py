# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MODEL REGISTRY  (STRICT ENTERPRISE CONFIG)
# ============================================================
#
# Routing table:
#   simple   → Local (in-house Local LLM proxy)   — private, free, low-latency
#   medium   → GPT-5.4                           — coding, reasoning, agents
#   complex  → Claude Sonnet 4.6                 — deep reasoning, SDLC
#   deep     → GPT-5-5                           — latest OpenAI, explicit selection only
#   solution → Claude Opus 4.7                   — final synthesis (CLI/IDE only)
#   opus-4-8 → Claude Opus 4.8                   — CLI/IDE opt-in
#   opus-5   → Claude Opus 5                     — CLI/IDE opt-in (ENABLE_CLI_OPUS_5)
#   vision   → Gemini 3.1 Flash Image            — image generation (auto-detected)
#   gemini   → Gemini 3.5 Flash                  — explicit Gemini text routing (coding)
#
# All model identifiers are env-var-backed so they can be updated
# at deploy time without code changes.  The default values shown
# below are the AiNxt-approved production models — do not change
# without a formal model approval workflow.
#
# BLOCKED: claude-opus-4-6 and older Claude, claude-sonnet-4-5, gpt-5.2-pro
# ============================================================

import os

from core.logger import logger

# ============================================================
# PROVIDER POSTURE  —  the single switch an adopter needs
# ============================================================
#
#   LLM_PROVIDER=cloud   (default)  Use the cloud models named below. Requires
#                                   credentials for whichever of Anthropic /
#                                   OpenAI / Google you actually route to.
#   LLM_PROVIDER=local              Resolve EVERY routing tier to
#                                   LOCAL_LLM_MODEL_NAME, served by your own
#                                   OpenAI-compatible endpoint (Ollama, vLLM,
#                                   LiteLLM, ...). No cloud provider account is
#                                   needed and no prompt leaves your network.
#
# The default is `cloud` so existing deployments are unaffected. An adopter who
# does not use Anthropic (or any cloud provider) sets ONE variable rather than
# overriding a dozen model ids individually. See docs/PROVIDERS.md.
#
# An unrecognised value is a hard error rather than a silent fallback: quietly
# routing to a different provider than the operator asked for would send prompts
# somewhere they did not intend.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "cloud").strip().lower()
_VALID_LLM_PROVIDERS = ("cloud", "local")
if LLM_PROVIDER not in _VALID_LLM_PROVIDERS:
    raise ValueError(
        "LLM_PROVIDER=%r is not valid (expected one of: %s). Refusing to start "
        "rather than silently routing to a provider you did not choose."
        % (LLM_PROVIDER, ", ".join(_VALID_LLM_PROVIDERS))
    )


def is_local_only() -> bool:
    """True when the deployment must not call a cloud model provider.

    Read via this helper rather than comparing the constant, so a future posture
    (e.g. a specific single cloud provider) only changes one place.
    """
    return LLM_PROVIDER == "local"


# ---------------- OPENAI (APPROVED) ----------------

OPENAI_SIMPLE_MODEL  = os.getenv("OPENAI_SIMPLE_MODEL",  "")    # set via OPENAI_SIMPLE_MODEL in .env
OPENAI_CODING_MODEL  = os.getenv("OPENAI_CODING_MODEL",  "")    # set via OPENAI_CODING_MODEL in .env
OPENAI_PRIMARY_MODEL = OPENAI_CODING_MODEL   # alias

OPENAI_LATEST_MODEL       = os.getenv("OPENAI_LATEST_MODEL",       "")   # set via OPENAI_LATEST_MODEL in .env
OPENAI_TERA_MODEL         = os.getenv("OPENAI_TERA_MODEL",         "")   # set via OPENAI_TERA_MODEL in .env
OPENAI_LUNA_MODEL         = os.getenv("OPENAI_LUNA_MODEL",         "")   # set via OPENAI_LUNA_MODEL in .env
OPENAI_OSS_MODEL          = os.getenv("OPENAI_OSS_MODEL",          "")   # set via OPENAI_OSS_MODEL in .env — your self-hosted model ID
OPENAI_DEEP_RESEARCH_MINI = os.getenv("OPENAI_DEEP_RESEARCH_MINI", "")   # set via OPENAI_DEEP_RESEARCH_MINI in .env
OPENAI_DEEP_RESEARCH      = os.getenv("OPENAI_DEEP_RESEARCH",      "")   # set via OPENAI_DEEP_RESEARCH in .env
OPENAI_IMAGE_MODEL        = os.getenv("OPENAI_IMAGE_MODEL",        "")   # set via OPENAI_IMAGE_MODEL in .env


# ---------------- CLAUDE (APPROVED) ----------------

CLAUDE_PRIMARY_MODEL = os.getenv("CLAUDE_PRIMARY_MODEL", "")   # set via CLAUDE_PRIMARY_MODEL in .env
CLAUDE_HAIKU         = os.getenv("CLAUDE_HAIKU",         "")   # set via CLAUDE_HAIKU in .env

# Sonnet 5 — explicit user selection, available on ALL channels (web Chat picker,
# CLI `/model sonnet-5`, IDE `/v1/models`, OpenAI-compat clients). NOT gated by
# ENABLE_OPUS — it is a Sonnet-tier model, not an Opus-tier one. Falls back to
# CLAUDE_PRIMARY_MODEL when the upstream call fails.
CLAUDE_SONNET_5_MODEL = os.getenv("CLAUDE_SONNET_5_MODEL", "")   # set via CLAUDE_SONNET_5_MODEL in .env

# Opus — solution-tier model for final synthesis in Threads @AiNxt + SDLC.
# CLI/IDE only; ENABLE_CHAT_OPUS=false keeps Opus out of the web Chat picker.
CLAUDE_OPUS_MODEL    = os.getenv("CLAUDE_OPUS_MODEL",   "")   # set via CLAUDE_OPUS_MODEL in .env
# Opus 4.6 is retired — kept as a constant so existing env-var references
# resolve cleanly, but it is always in BLOCKED_MODELS (see below).
CLAUDE_OPUS_46_MODEL = os.getenv("CLAUDE_OPUS_46_MODEL", "")  # RETIRED — always blocked; set via env if needed
# Opus 4.8 — CLI/IDE opt-in. Available via CLI (`/model opus-4-8`) and IDE
# plugins (/v1/models). NOT shown in the web Chat picker (/v1/all-models) and
# NOT used by the SDLC pipeline (which stays on Opus 4.7 via the solution tier).
CLAUDE_OPUS_48_MODEL = os.getenv("CLAUDE_OPUS_48_MODEL", "")   # set via CLAUDE_OPUS_48_MODEL in .env
# Opus 5 — CLI/IDE opt-in. Gated by ENABLE_CLI_OPUS_5 (default false).
# NOT shown in the web Chat picker. NOT used by the SDLC pipeline.
CLAUDE_OPUS_5_MODEL  = os.getenv("CLAUDE_OPUS_5_MODEL",  "")   # set via CLAUDE_OPUS_5_MODEL in .env
ENABLE_OPUS          = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
# Sonnet 5 is enabled on all channels by default. Kept env-var-backed so ops can
# hard-disable it (set to "false") without a code change if a rollback is needed.
ENABLE_SONNET_5      = os.getenv("ENABLE_SONNET_5", "true").lower() in ("true", "1", "yes")
ENABLE_CHAT_OPUS     = os.getenv("ENABLE_CHAT_OPUS", "false").lower() in ("true", "1", "yes")
ENABLE_CLI_OPUS_48   = os.getenv("ENABLE_CLI_OPUS_48", "true").lower() in ("true", "1", "yes")
# Opus 5 is a new model — opt-in only. Set ENABLE_CLI_OPUS_5=true to expose it
# on CLI and IDE channels. Web Chat never shows it regardless of this flag.
ENABLE_CLI_OPUS_5    = os.getenv("ENABLE_CLI_OPUS_5", "false").lower() in ("true", "1", "yes")
# Set ENABLE_RAW_OPENAI_API=true to re-enable direct access.
ENABLE_RAW_OPENAI_API = os.getenv("ENABLE_RAW_OPENAI_API", "false").lower() in ("true", "1", "yes")
# GPT-5.6 Tera and Luna — enabled on both Chat and CLI by default.
# Set to "false" to hide a variant without a code change.
ENABLE_GPT56_TERA    = os.getenv("ENABLE_GPT56_TERA", "true").lower() in ("true", "1", "yes")
ENABLE_GPT56_LUNA    = os.getenv("ENABLE_GPT56_LUNA", "true").lower() in ("true", "1", "yes")
SOLUTION_MODEL       = CLAUDE_OPUS_MODEL if ENABLE_OPUS else CLAUDE_PRIMARY_MODEL


# ---------------- GEMINI ----------------
# Four explicit models (gemini-2.5-flash deprecated):
#   GEMINI_TEXT_MODEL        — text/coding (multimodal: also handles vision analysis)
#   GEMINI_CODING_LITE_MODEL — lightweight coding
#   GEMINI_IMAGE_MODEL       — image generation (text → image, e.g. gemini-3.1-flash-image)
#   GEMINI_VISION_MODEL      — vision analysis (image → text description/analysis)
#
# IMPORTANT: GEMINI_VISION_MODEL must be a text-output model (e.g. gemini-3.5-flash),
# NOT the image-generation model. gemini-3.1-flash-image is designed for text→image
# generation; when used for image analysis it returns empty text (response.text = "").
# GEMINI_IMAGE_MODEL is used exclusively for /chat/image-generate (image generation).
# GEMINI_VISION_MODEL is used for /ask/image vision analysis and parse_image() in
# document_parser.py — both need a model that returns text, not image bytes.

GEMINI_TEXT_MODEL        = os.getenv("GEMINI_TEXT_MODEL",        "")   # set via GEMINI_TEXT_MODEL in .env
GEMINI_CODING_LITE_MODEL = os.getenv("GEMINI_CODING_LITE_MODEL", "")   # set via GEMINI_CODING_LITE_MODEL in .env
GEMINI_IMAGE_MODEL       = os.getenv("GEMINI_IMAGE_MODEL",       "")   # set via GEMINI_IMAGE_MODEL in .env
# Default to GEMINI_TEXT_MODEL (gemini-3.5-flash) — a multimodal model that can
# analyse images and return text. Previously aliased to GEMINI_IMAGE_MODEL which
# caused empty responses when /ask/image was called with non-generation prompts
# (e.g. "improve the UI") because the image-generation model returns image bytes,
# not text, leaving response.text = "".
GEMINI_VISION_MODEL      = os.getenv("GEMINI_VISION_MODEL",      GEMINI_TEXT_MODEL)

# Veo 3.1 video preview — Gemini provider, long-running operation, returns MP4.
# Chat-UI-only. Per-user access is governed by model governance tables
# (dept_model_permissions / user_model_permissions) — same as every other model.
# NOT shown in CLI (/v1/models), IDE (/ide/models), or OpenAI-compat (/v1/models).
VEO_MODEL          = os.getenv("VEO_MODEL",   "")   # set via VEO_MODEL in .env
VEO_DISPLAY        = os.getenv("VEO_DISPLAY", "Veo 3.1 (video preview)")
VEO_ENABLED        = os.getenv("VEO_ENABLED", "false").lower() in ("true", "1", "yes")
# Per-second cost (Veo is billed by output video duration, not tokens).
# Placeholder — update with official Vertex AI Veo 3.1 preview pricing.
VEO_COST_PER_SECOND = float(os.getenv("VEO_COST_PER_SECOND", "0.40"))


# ---------------- VISION ROUTING ----------------
# Primary vision provider at runtime: gemini | openai | local_llm
# Change via env vars — no code changes needed.
PRIMARY_VISION_PROVIDER  = os.getenv("PRIMARY_VISION_PROVIDER",  "")   # set via PRIMARY_VISION_PROVIDER in .env
# Fallback if primary fails. Set to "none" to disable.
FALLBACK_VISION_PROVIDER = os.getenv("FALLBACK_VISION_PROVIDER", "")   # set via FALLBACK_VISION_PROVIDER in .env
# Comma-separated IDs of local hosted models that accept image/vision input.
# Set LOCAL_VISION_MODELS to the vision-capable model IDs served by your local
# LLM endpoint (e.g. LOCAL_VISION_MODELS=llava:13b,bakllava:7b for Ollama).
# Defaults to empty — vision falls back to the cloud provider (Gemini/OpenAI).
LOCAL_VISION_MODELS: list[str] = [
    m.strip()
    for m in os.getenv("LOCAL_VISION_MODELS", "").split(",")
    if m.strip()
]


# ---------------- LOCAL LLM (in-house GPU) ----------------

LOCAL_LLM_MODEL_NAME = os.getenv("LOCAL_LLM_MODEL_NAME", "local-llm")

# ---------------- EVERYDAY-CHAT FALLBACK CHAIN ----------------
# Ordered fallback for the default everyday-chat mini tier when the primary
# (GPT-5-mini) is unavailable / its circuit breaker is open. Comma-separated;
# each entry is either a hint understood by the router ("haiku") or a pinned
# local model ("local:<id>"). Admins retune via the CHAT_FALLBACK_CHAIN env var
# without a code change.
# Default: fall back to Claude Haiku only. Add local model IDs for your
# deployment, e.g. CHAT_FALLBACK_CHAIN=haiku,local:llama3.1:8b
CHAT_FALLBACK_CHAIN: list[str] = [
    m.strip() for m in os.getenv(
        "CHAT_FALLBACK_CHAIN", ""
    ).split(",") if m.strip()
]
# No code default — set CHAT_FALLBACK_CHAIN in .env.
# OSS: leave blank (no fallback assumed).
# Internal: set to your preferred chain, e.g. local:kimi-k2.7-code,local:glm-5.2,haiku


# ---------------- SHARED COST TABLE (one source of truth) ----------------
#
# Cost per 1 million tokens (input_usd, output_usd).
# Keys use the constant values so env-var overrides propagate automatically.
# In-house local models are always free — callers check for "local" in model name.

MODEL_COST_PER_1M: dict[str, tuple[float, float]] = {
    OPENAI_SIMPLE_MODEL:        (0.15,    0.60),
    OPENAI_CODING_MODEL:        (2.50,    15.00),
    OPENAI_LATEST_MODEL:        (5.00,   30.00),
    OPENAI_TERA_MODEL:          (2.00,   12.00),  # placeholder — update when official pricing confirmed
    OPENAI_LUNA_MODEL:          (0.20,   1.20),  # placeholder — update when official pricing confirmed
    OPENAI_OSS_MODEL:           (0.0,     0.0),   # in-house hosted — no cloud cost
    OPENAI_DEEP_RESEARCH_MINI:  (2.00,   10.00),
    OPENAI_DEEP_RESEARCH:       (15.00,  60.00),
    CLAUDE_PRIMARY_MODEL:       (3.00,   15.00),
    CLAUDE_HAIKU:               (0.80,    4.00),
    CLAUDE_OPUS_MODEL:          (15.00,  75.00),
    CLAUDE_OPUS_48_MODEL:       (15.00,  75.00),  # placeholder — update when official pricing announced
    CLAUDE_OPUS_5_MODEL:        (15.00,  75.00),  # placeholder — update when official pricing announced
    CLAUDE_SONNET_5_MODEL:      (3.00,   15.00),  # placeholder — mirrors Sonnet 4.6 until official pricing
    GEMINI_TEXT_MODEL:          (0.30,    1.20),  # placeholder — confirm official pricing
    GEMINI_CODING_LITE_MODEL:   (0.10,    0.40),  # placeholder — confirm official pricing
    GEMINI_IMAGE_MODEL:         (0.30,   30.00),  # image OUTPUT tokens billed ~$30/1M (~$0.039/image); input keeps text rate
    LOCAL_LLM_MODEL_NAME:       (0.0,     0.0),
}


# ---------------- MAX OUTPUT TOKENS (per model, HARD ceiling) ----------------
#
# Anthropic (and some other providers) reject `max_tokens` values above a
# per-model ceiling that is INDEPENDENT of the model's context window — e.g.
# Claude Haiku 4.5 has a 256K context window but only a 64K output ceiling.
# The ainxt-cli defaults `max_tokens` by clamping to the model's
# `context_window` only (see ainxt-sampler/src/client.rs
# `default_messages_max_tokens`), so a model whose context window is larger
# than its real output ceiling needs an explicit entry here or the CLI will
# send an oversized `max_tokens` that the provider hard-rejects with a 400 on
# every single request (and, with the current stream-error propagation, the
# CLI retries that same doomed request until it exhausts its retry budget —
# see gaps.md "Haiku max_tokens 400" incident).
#
# Only models with an output ceiling BELOW their context window need an entry.
# Consulted by CLI-facing catalog endpoints (`/ainxt/v1/api/models`,
# `/v1/models`, `/v1/all-models`) so `max_completion_tokens` is served to the
# CLI/IDE and the client never has to guess.
MODEL_MAX_OUTPUT_TOKENS: dict[str, int] = {
    CLAUDE_HAIKU: 64_000,   # hard ceiling — see https://docs.anthropic.com model card
}


def max_output_tokens_for(model_id: str) -> int | None:
    """Return the hard output-token ceiling for `model_id`, or None when the
    model has no ceiling narrower than its context window (the common case)."""
    return MODEL_MAX_OUTPUT_TOKENS.get(model_id)


# ---------------- PER-SECOND COST TABLE (video models) ----------------
#
# Video-generation models (Veo) are billed per output second, not per token.
# Kept as a separate map so per-token math elsewhere is unaffected.
MODEL_COST_PER_SECOND: dict[str, float] = {
    VEO_MODEL: VEO_COST_PER_SECOND,
}


# ---------------- VEO ACCESS GATE (ad_level 0 or admin) ----------------
#
# Access is granted to:
#   - Users with ad_level == 0 (most senior execs), OR
#   - Users with role == "admin"
# Both fields are read from the JWT-decoded `current_user` dict.
# Fail-closed: missing claims → no access.
def is_veo_allowed_for_user(current_user: dict | None) -> bool:
    """Return True when VEO is globally enabled AND the user is ad_level 0 or admin."""
    if not VEO_ENABLED:
        return False
    if not current_user:
        return False
    if current_user.get("role") == "admin":
        return True
    try:
        return int(current_user.get("ad_level", 6)) == 0
    except (TypeError, ValueError):
        return False


# ---------------- DISPLAY NAMES (for UI labels and logs) ----------------
#
# Human-readable names shown in dropdowns, logs, and audit trails.
# Override via env vars if your internal branding differs from model IDs.
# These are intentionally separate from model IDs so a model can be
# upgraded (e.g. claude-sonnet-4-6 → claude-sonnet-4-7) with a single
# env-var change and the display name updates automatically.

CLAUDE_PRIMARY_DISPLAY  = os.getenv("CLAUDE_PRIMARY_DISPLAY",  "Claude Sonnet")
CLAUDE_HAIKU_DISPLAY    = os.getenv("CLAUDE_HAIKU_DISPLAY",    "Claude Haiku")
CLAUDE_OPUS_DISPLAY     = os.getenv("CLAUDE_OPUS_DISPLAY",     "Claude Opus 4.7")
CLAUDE_OPUS_48_DISPLAY  = os.getenv("CLAUDE_OPUS_48_DISPLAY",  "Claude Opus 4.8")
CLAUDE_OPUS_5_DISPLAY   = os.getenv("CLAUDE_OPUS_5_DISPLAY",   "Claude Opus 5")
CLAUDE_SONNET_5_DISPLAY = os.getenv("CLAUDE_SONNET_5_DISPLAY", "Claude Sonnet 5")
OPENAI_CODING_DISPLAY   = os.getenv("OPENAI_CODING_DISPLAY",   "GPT-5.4 (Coding)")
OPENAI_SIMPLE_DISPLAY   = os.getenv("OPENAI_SIMPLE_DISPLAY",   "GPT-5-mini (Fast)")
OPENAI_LATEST_DISPLAY   = os.getenv("OPENAI_LATEST_DISPLAY",   "GPT-5-5 (Latest)")
OPENAI_TERA_DISPLAY     = os.getenv("OPENAI_TERA_DISPLAY",     "GPT-5.6 Terra")
OPENAI_LUNA_DISPLAY     = os.getenv("OPENAI_LUNA_DISPLAY",     "GPT-5.6 Luna")
OPENAI_OSS_DISPLAY      = os.getenv("OPENAI_OSS_DISPLAY",      "GPT-OSS 120B (In-house)")
GEMINI_DISPLAY          = os.getenv("GEMINI_DISPLAY",          "Gemini")
GEMINI_TEXT_DISPLAY        = os.getenv("GEMINI_TEXT_DISPLAY",        "Gemini 3.5 Flash (Coding)")
GEMINI_CODING_LITE_DISPLAY = os.getenv("GEMINI_CODING_LITE_DISPLAY", "Gemini 3.1 Flash-Lite (Coding)")
GEMINI_IMAGE_DISPLAY       = os.getenv("GEMINI_IMAGE_DISPLAY",       "Gemini 3.1 Flash Image")
LOCAL_LLM_DISPLAY       = os.getenv("LOCAL_LLM_DISPLAY",       "Local (In-house)")


# ---------------- BLOCKED MODELS ----------------

BLOCKED_MODELS: set[str] = {

    # Claude — retired/old models always blocked
    "claude-opus-4-6",   # retired — superseded by Opus 4.7/4.8
    "claude-opus-4-5",
    "claude-opus-4",
    "claude-opus-3",
    "claude-sonnet-4-5", # retired — superseded by Sonnet 4.6

    # OpenAI — blocked pro variant
    "gpt-5.2-pro",
    # gpt-5.2 is retired — replaced by gpt-5.4
    "gpt-5.2",

}
# Opus 4.7 blocked only when ENABLE_OPUS=false
if not ENABLE_OPUS:
    BLOCKED_MODELS.add(CLAUDE_OPUS_MODEL)
    BLOCKED_MODELS.add(CLAUDE_OPUS_48_MODEL)


# ---------------- MODELS THAT REJECT `temperature` ----------------
#
# Anthropic has progressively stopped accepting `temperature` on newer Claude
# generations — they 400 outright instead of clamping/ignoring it. Both
# direct-dispatch paths that call the Anthropic SDK need to agree on this:
# gateway_claude.py (the main platform gateway) and
# AgentStudio/backend/app/core/llm_handler.py's ClaudeDirectClient (Agent
# Studio). This used to be a bare env var (MODELS_WITHOUT_TEMPERATURE) with
# no built-in defaults at all — meaning a fresh install with the env var
# unset sent `temperature` to opus-5/sonnet-5/opus-4-7/opus-4-8 unconditionally
# and got a 400 on every single call to any of them, including whichever one
# ends up the admin-configured default. The env var still works, purely
# additive, for any future/self-hosted model this list doesn't yet cover.
_MODELS_WITHOUT_TEMPERATURE_DEFAULTS = (
    "claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7",
)


def models_without_temperature() -> tuple[str, ...]:
    """Prefix list of model ids that must NOT receive a `temperature` param."""
    extra = tuple(
        p.strip() for p in os.getenv("MODELS_WITHOUT_TEMPERATURE", "").split(",") if p.strip()
    )
    return _MODELS_WITHOUT_TEMPERATURE_DEFAULTS + extra


# ---------------- SDLC PER-STAGE MODEL TIERS ----------------
#
# Each SDLC stage maps to a router hint ("haiku" | "medium" | "complex" | "solution").
# Defaults below encode the recommended split: cheap models for mechanical work,
# Opus only where deep reasoning matters. Override ANY stage at deploy time with
#   SDLC_MODEL_<STAGE>=<hint>     e.g.  SDLC_MODEL_CODER=solution
# without touching code. ENABLE_OPUS=false transparently downgrades solution→complex.
#
# Stage guide (CLI-loop rework 2026-06-27 — Sonnet workhorse + 2 Opus gates):
#   classify / locate      → haiku   (JSON classify, region picking — trivial)
#   coder / fixer / noncode→ complex (Sonnet — surgical edits against visible code)
#   analyze / design /
#   synthesis              → complex (Sonnet WORKHORSE — pull-first loop emits JSON)
#   solution_review        → solution (Opus PRE-code design-review GATE)
#   code_review            → solution (Opus POST-code code-review GATE)
#   cross_model_review     → medium  (GPT — deliberately a different model family)

_SDLC_STAGE_HINTS_ALLOWED = {"haiku", "medium", "complex", "solution", "deep"}

SDLC_STAGE_MODEL_DEFAULTS: dict = {
    "classify":           "haiku",
    "locate":             "haiku",
    "coder":              "complex",
    "fixer":              "complex",
    "noncode":            "complex",
    "exploration":        "complex",
    # CLI-loop rework (2026-06-27): analyze/design/synthesis are now the Sonnet
    # WORKHORSE — a single pull-first explore loop gathers context and emits the
    # JSON itself (no separate always-Opus synthesizer). Opus is reserved for the
    # TWO review GATES only: solution_review (pre-code design review) and
    # code_review (post-code review). This is the cost-cascade pattern (cheap
    # workhorse + expensive eval gates). ENABLE_OPUS=false transparently
    # downgrades the two solution gates to complex (see sdlc_stage_hint).
    "analyze":            "complex",
    "design":             "complex",
    "plan":               "complex",
    "solution_review":    "solution",   # PRE-code Opus design-review gate
    "code_review":        "solution",   # POST-code Opus code-review gate
    "synthesis":          "complex",
    "cross_model_review": "medium",
    "normalize":          "haiku",
    "diagnose":           "complex",
    "manifest_validate":  "deep",
    "pre_coding_build":   "",        # no LLM — falls back to default hint when hint is needed
}


def sdlc_stage_hint(stage: str, default: str = "complex") -> str:
    """
    Resolve the router hint for an SDLC stage.

    Precedence: env SDLC_MODEL_<STAGE>  →  SDLC_STAGE_MODEL_DEFAULTS[stage]  →  default.
    solution → complex when ENABLE_OPUS is off, so callers never need to
    special-case Opus availability.

    ``SDLC_MODEL_<STAGE>`` accepts EITHER a router tier name
    (haiku|medium|complex|solution|deep) OR a concrete model id of any provider.
    A concrete id is returned verbatim: the model router resolves it via the
    admin-configured provider registry (``models/model_router.py`` route() step
    1a), so a harness with no Anthropic provider can pin any stage to its own
    model. The id is used only as a model-id string (registry lookup / API model
    param); it is not interpolated into any shell command.
    """
    base = SDLC_STAGE_MODEL_DEFAULTS.get(stage, default)
    env  = (os.getenv(f"SDLC_MODEL_{stage.upper()}") or "").strip()
    # A non-tier env value is a concrete model id — hand it straight to the router.
    if env and env.lower() not in _SDLC_STAGE_HINTS_ALLOWED:
        return env
    hint = (env or base or default).strip().lower()
    if hint not in _SDLC_STAGE_HINTS_ALLOWED:
        hint = base
    # Read ENABLE_OPUS at call time so deploy-time changes apply without re-import.
    if hint == "solution" and os.getenv("ENABLE_OPUS", "true").lower() in ("false", "0", "no"):
        hint = "complex"
    return hint

# Opus 4.8 is CLI/IDE-only; blocked when either the global Opus switch is off
# OR the CLI-specific opt-in is off. This guarantees the chat-picker (which does
# not list Opus 4.8) and SDLC (which routes via `solution` → Opus 4.7) are never
# accidentally upgraded to 4.8.
if not ENABLE_OPUS or not ENABLE_CLI_OPUS_48:
    BLOCKED_MODELS.add(CLAUDE_OPUS_48_MODEL)

# Opus 5 is CLI/IDE-only and opt-in. Blocked unless ENABLE_CLI_OPUS_5=true.
if not ENABLE_CLI_OPUS_5:
    BLOCKED_MODELS.add(CLAUDE_OPUS_5_MODEL)

# Sonnet 5 kill-switch — no channel gating, only a global on/off.
if not ENABLE_SONNET_5:
    BLOCKED_MODELS.add(CLAUDE_SONNET_5_MODEL)

# Operator extension — block additional models without code changes.
# Format: BLOCKED_MODELS_EXTRA=model-a,model-b
# The base set above (retired models) is always enforced regardless of this var.
_BLOCKED_MODELS_EXTRA: set[str] = {
    m.strip() for m in os.getenv("BLOCKED_MODELS_EXTRA", "").split(",") if m.strip()
}
if _BLOCKED_MODELS_EXTRA:
    BLOCKED_MODELS.update(_BLOCKED_MODELS_EXTRA)

def _role_model(env_value: str, family: str, tag: str) -> str:
    """Fall back to a registry-configured model when a role-specific env
    constant (CLAUDE_PRIMARY_MODEL, OPENAI_CODING_MODEL, etc.) is blank.

    Mirrors ``models/model_router.py``'s ``_resolve_tier_model()`` exactly —
    this module can't import that one (model_router.py imports
    core.model_registry at module level, so importing the reverse would be
    circular) — so the same env-override → registry-lookup-by-family/tag →
    "" chain is duplicated here as the single fallback every CLI/tier
    resolver below goes through. `tag` uses the same vocabulary as
    db/migrate.py's `_AC1_MODEL_ROLE_TAGS` backfill.

    No-op when env_value is already set — zero behavior change for any
    deployment that has its role env vars configured.
    """
    if env_value:
        return env_value
    try:
        from core.llm_provider_registry import get_enabled_models
        candidates = [m for m in get_enabled_models() if m["family"] == family]
        tagged = [m for m in candidates if tag in (m["capabilities"].get("tier_tags") or [])]
        pick = tagged[0] if tagged else (candidates[0] if candidates else None)
        return pick["model_id"] if pick else ""
    except Exception as exc:
        logger.warning(f"[model_registry] registry fallback failed family={family} tag={tag}: {exc}")
        return ""


def _tier_env_override(tier: str) -> str:
    """Operator override for a router tier's concrete model, provider-agnostic.

    ``SDLC_TIER_<TIER>_MODEL`` takes precedence over the family-specific constant
    (CLAUDE_*/OPENAI_*), letting a harness with NO Anthropic provider map every
    tier to its own models in ONE place instead of per-stage. Returns "" when
    unset, so the caller falls through to the historical constant → registry chain
    (see ``_role_model``). The value is only ever used as a model-id string passed
    to the router (registry lookup) or to the ``ainxt`` CLI ``--model`` argv
    element (spawned without a shell) — never interpolated into a shell command —
    and it is still gated by BLOCKED_MODELS downstream."""
    return (os.getenv(f"SDLC_TIER_{tier.upper()}_MODEL") or "").strip()


def cli_model_for(stage: str) -> str:
    """
    Resolve a CONCRETE model id for a CLI phase.

    For CLI usage (--model flag), resolves the tier via sdlc_stage_hint(stage),
    maps to the concrete model id, and guards against BLOCKED_MODELS.

    If the resolved concrete id is in BLOCKED_MODELS, falls back to the Sonnet
    workhorse (CLAUDE_PRIMARY_MODEL) instead.

    Args:
        stage: SDLC stage name (typically "plan" or "coder" for CLI)

    Returns:
        Concrete model id string (e.g., "claude-sonnet-4-6")
    """
    # Resolve tier via the standard SDLC mechanism
    # Resolve tier via the standard SDLC mechanism, then map + guard.
    return cli_model_for_tier(sdlc_stage_hint(stage))


def _cli_model_from_env(env_var: str, default_tier: str) -> str:
    """Shared resolver for per-phase CLI model env vars (CLASSIFY / PLAN / IMPLEMENT).

    Accepts, in order of precedence:
      - a direct concrete model id (e.g. ``claude-sonnet-5``, ``claude-opus-4-8``) —
        returned as-is unless it is in BLOCKED_MODELS.
      - a router tier name (haiku|complex|medium|solution|deep) — resolved via
        ``cli_model_for_tier`` (BLOCKED_MODELS-guarded).
      - "local" — mapped to CLAUDE_HAIKU (the ainxt CLI has no Ollama bridge).
      - unset / invalid — falls back to ``default_tier``.
    """
    _tiers = {"haiku", "complex", "medium", "solution", "deep"}
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return cli_model_for_tier(default_tier)
    low = raw.lower()
    if low == "local":
        return _role_model(CLAUDE_HAIKU, "anthropic", "haiku")
    if low in _tiers:
        return cli_model_for_tier(low)
    # Treat as a direct model id. BLOCKED_MODELS is the only gate — an operator
    # who names a model explicitly has opted in to it.
    blocked = set(BLOCKED_MODELS)
    if raw in blocked:
        return _role_model(CLAUDE_PRIMARY_MODEL, "anthropic", "complex")
    return raw


def cli_classify_model() -> str:
    """Resolve the concrete CLI model for the CLASSIFY phase.

    Controlled by ``SDLC_CLI_CLASSIFY_MODEL`` (tier name, direct model id, or
    "local"). Defaults to the haiku tier when unset."""
    return _cli_model_from_env("SDLC_CLI_CLASSIFY_MODEL", "haiku")


def cli_plan_model() -> str:
    """Resolve the concrete CLI model for the PLAN phase.

    Controlled by ``SDLC_CLI_PLAN_MODEL`` (tier name, direct model id, or
    "local"). Defaults to the complex (Sonnet) tier when unset.

    Examples::

        SDLC_CLI_PLAN_MODEL=complex          # Sonnet (default)
        SDLC_CLI_PLAN_MODEL=claude-sonnet-5  # pin Sonnet 5 directly
        SDLC_CLI_PLAN_MODEL=solution         # Opus (requires ENABLE_OPUS=true)
        SDLC_CLI_PLAN_MODEL=claude-opus-4-7  # Opus by direct id (explicit opt-in)
    """
    return _cli_model_from_env("SDLC_CLI_PLAN_MODEL", "complex")


def cli_implement_model() -> str:
    """Resolve the concrete CLI model for the IMPLEMENT phase.

    Controlled by ``SDLC_CLI_IMPLEMENT_MODEL`` (tier name, direct model id, or
    "local"). Defaults to the complex (Sonnet) tier when unset.

    Examples::

        SDLC_CLI_IMPLEMENT_MODEL=complex          # Sonnet (default)
        SDLC_CLI_IMPLEMENT_MODEL=claude-sonnet-5  # pin Sonnet 5 directly
        SDLC_CLI_IMPLEMENT_MODEL=solution         # Opus (requires ENABLE_OPUS=true)
        SDLC_CLI_IMPLEMENT_MODEL=claude-opus-4-7  # Opus by direct id (explicit opt-in)
    """
    return _cli_model_from_env("SDLC_CLI_IMPLEMENT_MODEL", "complex")


def cli_model_for_tier(hint: str) -> str:
    """Shared tier→concrete-CLI-model resolution (BLOCKED_MODELS-guarded), used by
    both cli_model_for(stage) and cli_classify_model(). ENABLE_OPUS is re-read at
    call time so the kill-switch applies without a restart; when Opus is enabled
    the "solution" tier resolves to Opus for CLI phases too."""
    # Local-only posture: every tier collapses to the locally served model. Done
    # before the tier table so no cloud model id can escape to a gateway.
    if is_local_only():
        return LOCAL_LLM_MODEL_NAME

    enable_opus = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
    blocked = set(BLOCKED_MODELS)
    if not enable_opus:
        blocked.add(CLAUDE_OPUS_MODEL)
        blocked.add(CLAUDE_OPUS_48_MODEL)

    # SDLC_TIER_<TIER>_MODEL lets an operator remap any tier to any provider's
    # model in one place (family-agnostic). Wins over the CLAUDE_*/OPENAI_*
    # constants below via _role_model's "truthy env_value returned as-is" rule.
    _tier_to_role = {
        "solution":  (_tier_env_override("solution") or CLAUDE_OPUS_MODEL,    "anthropic", "opus"),
        "complex":   (_tier_env_override("complex")  or CLAUDE_PRIMARY_MODEL, "anthropic", "complex"),
        # "simple" is the provider-neutral operator name for the cheap/fast tier
        # (internally keyed "haiku"): SDLC_TIER_SIMPLE_MODEL overrides it.
        "haiku":     (_tier_env_override("simple")   or CLAUDE_HAIKU,         "anthropic", "haiku"),
        "medium":    (_tier_env_override("medium")   or OPENAI_CODING_MODEL,  "openai",    "medium"),
        "deep":      (_tier_env_override("deep")     or OPENAI_LATEST_MODEL,  "openai",    "deep"),
    }
    _key = (hint or "").strip().lower()
    if _key not in _tier_to_role:
        # Not a known tier: treat a non-empty hint as a concrete model id and
        # return it verbatim (the router resolves it via the provider registry).
        # BLOCKED_MODELS is the only gate — an operator who names a model has
        # opted in to it. Mirrors _cli_model_from_env's direct-id branch. The
        # value is used solely as a model-id string / argv element, never in a
        # shell, so widening this path introduces no injection surface.
        if hint and hint.strip() and hint.strip() not in blocked:
            return hint.strip()
        return _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")

    env_value, family, tag = _tier_to_role[_key]
    model_id = _role_model(env_value, family, tag)

    if model_id in blocked:
        return _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")
    return model_id

def openai_model_for_tier(hint: str) -> tuple[str, bool]:
    """Resolve a router tier to a CONCRETE OpenAI model id, for gateways that only
    speak the OpenAI API (e.g. the SDLC manifest cross-validator, which calls
    ``model_router._get_openai()`` directly).

        deep         → OPENAI_LATEST_MODEL   (gpt-5.5; env-upgradable to gpt-5.6)
        medium       → OPENAI_CODING_MODEL   (gpt-5.4)
        mini/simple  → OPENAI_SIMPLE_MODEL   (gpt-5-mini)

    A Claude tier (complex/solution/haiku) or an unknown hint has NO OpenAI
    equivalent — it falls back to OPENAI_LATEST_MODEL and returns fell_back=True so
    the caller can log a WARNING. Sending a Claude id (or None) to the OpenAI gateway
    400s, so this fallback is a correctness guard, not a nicety.

    Returns (model_id, fell_back).
    """
    if is_local_only():
        # A local OpenAI-compatible server serves one model name; that is not a
        # fallback, so fell_back=False.
        return LOCAL_LLM_MODEL_NAME, False

    h = (hint or "").strip().lower()
    # SDLC_TIER_<TIER>_MODEL overrides the OPENAI_* constant per tier (see
    # _tier_env_override / cli_model_for_tier). Kept consistent so the manifest
    # cross-validator honors the same operator remap.
    _openai_tiers = {
        "deep":   (_tier_env_override("deep")   or OPENAI_LATEST_MODEL, "deep"),
        "medium": (_tier_env_override("medium") or OPENAI_CODING_MODEL, "medium"),
        "mini":   (_tier_env_override("mini")   or OPENAI_SIMPLE_MODEL, "simple"),
        "simple": (_tier_env_override("mini")   or OPENAI_SIMPLE_MODEL, "simple"),
    }
    if h in _openai_tiers:
        env_value, tag = _openai_tiers[h]
        return _role_model(env_value, "openai", tag), False
    return _role_model(OPENAI_LATEST_MODEL, "openai", "deep"), True


def veo_model() -> str:
    """Resolve the concrete Veo (video-gen) model id.

    Resolution order: explicit ``VEO_MODEL`` env override → an enabled
    registry model of family "gemini" tagged "video" → "" (unchanged from
    the historical blank-constant behavior). Single source of truth for
    both the actual dispatch model (``gateway_gemini.py::generate_veo_video()``)
    and the cost/audit lookups in ``routers/chat_router.py`` — previously
    those two consulted the same blank env constant independently, so a
    blank ``VEO_MODEL`` was both a billing/audit-integrity bug (Veo became
    free and the audit trail recorded no model) and a genuine dispatch
    risk on the direct-SDK path.
    """
    return _role_model(VEO_MODEL, "gemini", "video")


def tier_cost_per_1m(hint: str) -> tuple[float, float]:
    """(input_usd, output_usd) per 1M tokens for a router hint/tier.

    Single source of truth for SDLC cost accounting (RFD R3) — resolves the
    hint to its concrete model id (env override → registry lookup, via
    ``_role_model()``) and reads the canonical MODEL_COST_PER_1M, falling
    through to the registry's own billing_tier metadata and finally a
    conservative non-zero default when the resolved id isn't in either
    table. Reads ENABLE_OPUS at call time for the solution tier.
    Local/simple → (0, 0). Unknown hints fall back to the Sonnet (complex)
    rate/model, never $0, so an unrecognised tier over-bills rather than
    silently under-bills.
    """
    h = (hint or "").strip().lower()
    if h in ("local", "simple"):
        return (0.0, 0.0)
    _solution_env = (
        (_tier_env_override("solution") or CLAUDE_OPUS_MODEL)
        if os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
        else (_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL)
    )
    _map = {
        "solution": (_solution_env,                                            "anthropic", "opus"),
        "complex":  (_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL,    "anthropic", "complex"),
        "haiku":    (_tier_env_override("simple")  or CLAUDE_HAIKU,            "anthropic", "haiku"),  # SDLC_TIER_SIMPLE_MODEL
        "medium":   (_tier_env_override("medium")  or OPENAI_CODING_MODEL,     "openai",    "medium"),
        "deep":     (_tier_env_override("deep")    or OPENAI_LATEST_MODEL,     "openai",    "deep"),
        "mini":     (_tier_env_override("mini")    or OPENAI_SIMPLE_MODEL,     "openai",    "simple"),
    }
    if h in _map:
        env_value, family, tag = _map[h]
        model_id = _role_model(env_value, family, tag)
    elif h:
        # Concrete model id (SDLC_MODEL_<STAGE> can now be a raw id) — price it
        # directly rather than mislabeling it as the Sonnet (complex) tier.
        model_id = h
    else:
        model_id = _role_model(_tier_env_override("complex") or CLAUDE_PRIMARY_MODEL, "anthropic", "complex")

    rates = MODEL_COST_PER_1M.get(model_id)
    if rates is not None:
        return rates

    try:
        from core.llm_provider_registry import get_model as _get_registry_model
        reg = _get_registry_model(model_id)
        if reg and (reg["family"] == "ollama" or reg["capabilities"].get("billing_tier") == "free"):
            return (0.0, 0.0)
    except Exception as exc:
        logger.warning(f"[model_registry] tier_cost_per_1m registry lookup failed for {model_id!r}: {exc}")

    # Conservative non-zero default (matches gateway.py::_estimate_cost()'s
    # fallback for unknown models) — never $0, so an id this table and the
    # registry both know nothing about over-bills rather than under-bills.
    return (2.00, 8.00)


# ============================================================
# SDLC GROUNDING / MINIMALISM CHARTER
# ------------------------------------------------------------
# A single authoritative block injected at the top of the scope-defining SDLC
# prompts (analyst, designer / bug-solutioning, coder). Counters the well-known
# LLM bias toward gold-plating — speculative abstractions, unrequested features,
# drive-by refactors, invented dependencies — without tipping into under-build:
# compliance, validation, error handling, and tests for the changed behavior are
# explicitly kept in-scope. "Ask" is calibrated for an autonomous pipeline: raise
# an open question ONLY for high-impact ambiguity, else take the smallest sane
# interpretation and state the assumption.
#
# Rule 5 (clarifying questions) is gated per stage: only stages that actually feed
# the AWAITING_USER_INPUT gate — the feature analyst and the bug solutioning/fix
# designer — get the "raise an open question" variant. Every other stage (feature
# designer, coder, revisions) cannot pause for an answer, so it gets the no-ask
# variant: resolve ambiguity yourself with the minimal interpretation. Rules 1-4
# (grounding/minimalism) apply everywhere.
#
# Kill-switch: set SDLC_GROUNDING_CHARTER=false to disable injection everywhere
# (read at call time — no restart). Default on.
# ============================================================
_GROUNDING_RULES_CORE = (
    "=== ENGINEERING CHARTER — GROUNDING & MINIMALISM (MANDATORY) ===\n"
    "Apply these to everything you produce below:\n"
    "1. MINIMAL SCOPE. Make the SMALLEST change that FULLY satisfies the ticket. Match the\n"
    "   complexity, structure, and idioms of the surrounding code. Do NOT add speculative\n"
    "   abstractions, layers, patterns, configuration, or features the ticket did not ask\n"
    "   for — \"for future flexibility\" / \"just in case\" is NOT a reason.\n"
    "2. NOT A LICENSE TO UNDER-BUILD. Required input validation, error handling, security /\n"
    "   PCI-DSS / PII compliance, and tests for the changed behavior ARE part of \"what is\n"
    "   needed\" — never drop them to look minimal.\n"
    "3. STAY IN BOUNDS. Touch only the files and symbols this ticket requires. No drive-by\n"
    "   refactors, renames, reformatting, or cleanup of code you were not asked to change.\n"
    "4. REAL DEPENDENCIES ONLY. Use the standard library and dependencies already present in\n"
    "   the repo manifest. Never import a package that does not exist. Add a new dependency\n"
    "   only if strictly necessary, only one that really exists, and declare it in the manifest.\n"
)

# Asking variant — ONLY for stages whose open_questions feed the user-input gate.
_GROUNDING_RULE_ASK = (
    "5. SMALLEST REASONABLE INTERPRETATION. If a requirement is ambiguous, take the simplest\n"
    "   interpretation and STATE the assumption. Raise it as an open question ONLY when the\n"
    "   ambiguity is high-impact (materially changes scope or behavior, or a wrong guess\n"
    "   forces rework). Do not pause for low-impact ambiguity.\n"
)

# No-ask variant — for stages that run autonomously and cannot pause for an answer.
_GROUNDING_RULE_NOASK = (
    "5. SMALLEST REASONABLE INTERPRETATION. If a requirement is ambiguous, take the simplest\n"
    "   reasonable interpretation and STATE the assumption explicitly, then proceed. This\n"
    "   stage runs autonomously and CANNOT pause for a user answer — do not block, defer, or\n"
    "   wait on clarifying questions; resolve the ambiguity yourself with the minimal interpretation.\n"
)

_GROUNDING_CHARTER_END = "=== END CHARTER ===\n\n"

# Back-compat full charter (asking variant) for any direct reference.
SDLC_GROUNDING_CHARTER = _GROUNDING_RULES_CORE + _GROUNDING_RULE_ASK + _GROUNDING_CHARTER_END


def grounding_charter(allow_questions: bool = False) -> str:
    """Return the SDLC grounding/minimalism charter block, or '' when disabled via
    SDLC_GROUNDING_CHARTER=false (read at call time so the toggle needs no restart).

    allow_questions=True  → rule 5 permits raising an open question (use ONLY at stages
                            that feed the AWAITING_USER_INPUT gate: feature analyst,
                            bug solutioning/fix designer).
    allow_questions=False → rule 5 tells the stage to resolve ambiguity itself (designer,
                            coder, revisions — they cannot pause for input)."""
    if os.getenv("SDLC_GROUNDING_CHARTER", "true").strip().lower() in ("false", "0", "no"):
        return ""
    rule5 = _GROUNDING_RULE_ASK if allow_questions else _GROUNDING_RULE_NOASK
    return _GROUNDING_RULES_CORE + rule5 + _GROUNDING_CHARTER_END
