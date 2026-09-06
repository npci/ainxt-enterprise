# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MODEL REGISTRY (STRICT ENTERPRISE CONFIG)
# ============================================================
#
# Approved routing:
#   simple tasks              → OPENAI_SIMPLE_MODEL   (gpt-5-mini)
#   coding/reasoning/agents   → OPENAI_CODING_MODEL   (gpt-5.4)
#   latest OpenAI (explicit)  → OPENAI_LATEST_MODEL   (gpt-5.5)
#   complex reasoning         → CLAUDE_PRIMARY_MODEL  (claude-sonnet-4-6)
#   solution tier (explicit)  → CLAUDE_OPUS_MODEL     (claude-opus-4-7)
#   opus legacy (explicit)    → CLAUDE_OPUS_46_MODEL  (claude-opus-4-6)
#   vision only               → GEMINI_VISION_MODEL
#
# All model identifiers are env-var-backed — override at deploy
# time without code changes.  Defaults are AiNxt-approved models.
#
# BLOCKED: claude-opus-4-5 and older, gpt-5.2-pro, gpt-5.2
# ============================================================

import os

# ---------------- OPENAI (APPROVED) ----------------

OPENAI_SIMPLE_MODEL  = os.getenv("OPENAI_SIMPLE_MODEL",  "")   # set via OPENAI_SIMPLE_MODEL in .env
OPENAI_CODING_MODEL  = os.getenv("OPENAI_CODING_MODEL",  "")   # set via OPENAI_CODING_MODEL in .env
OPENAI_PRIMARY_MODEL = OPENAI_CODING_MODEL   # alias


# ---------------- CLAUDE (APPROVED) ----------------

CLAUDE_PRIMARY_MODEL = os.getenv("CLAUDE_PRIMARY_MODEL", "")   # set via CLAUDE_PRIMARY_MODEL in .env
CLAUDE_HAIKU         = os.getenv("CLAUDE_HAIKU",         "")   # set via CLAUDE_HAIKU in .env

# Opus — user-selectable; ENABLE_OPUS defaults to true
CLAUDE_OPUS_MODEL    = os.getenv("CLAUDE_OPUS_MODEL",    "")   # set via CLAUDE_OPUS_MODEL in .env
CLAUDE_OPUS_46_MODEL = os.getenv("CLAUDE_OPUS_46_MODEL", "")   # RETIRED — always blocked; set via env if needed
ENABLE_OPUS          = os.getenv("ENABLE_OPUS", "true").lower() in ("true", "1", "yes")
SOLUTION_MODEL       = CLAUDE_OPUS_MODEL if ENABLE_OPUS else CLAUDE_PRIMARY_MODEL


# ---------------- GEMINI ----------------
# See core/model_registry.py at the repo root for the full split rationale.

GEMINI_TEXT_MODEL        = os.getenv("GEMINI_TEXT_MODEL",        "")   # set via GEMINI_TEXT_MODEL in .env
GEMINI_CODING_LITE_MODEL = os.getenv("GEMINI_CODING_LITE_MODEL", "")   # set via GEMINI_CODING_LITE_MODEL in .env
GEMINI_IMAGE_MODEL       = os.getenv("GEMINI_IMAGE_MODEL",       "")   # set via GEMINI_IMAGE_MODEL in .env
GEMINI_VISION_MODEL      = os.getenv("GEMINI_VISION_MODEL",      GEMINI_TEXT_MODEL)


# ---------------- VISION ROUTING ----------------
PRIMARY_VISION_PROVIDER  = os.getenv("PRIMARY_VISION_PROVIDER",  "")   # set via PRIMARY_VISION_PROVIDER in .env
FALLBACK_VISION_PROVIDER = os.getenv("FALLBACK_VISION_PROVIDER", "")   # set via FALLBACK_VISION_PROVIDER in .env
LOCAL_VISION_MODELS: list[str] = [
    m.strip()
    for m in os.getenv("LOCAL_VISION_MODELS", "").split(",")
    if m.strip()
]


# ---------------- LOCAL LLM (in-house GPU, TIER_SIMPLE) ----------------

LOCAL_LLM_MODEL_NAME = os.getenv("LOCAL_LLM_MODEL_NAME", "local-llm")   # overridden by LOCAL_LLM_MODEL_NAME env var in config.py


# ---------------- ADDITIONAL MODEL CONSTANTS (needed for cost table) ----------------
# These mirror the root core/model_registry.py — kept in sync manually.

OPENAI_LATEST_MODEL       = os.getenv("OPENAI_LATEST_MODEL",       "")   # set via OPENAI_LATEST_MODEL in .env
OPENAI_TERA_MODEL         = os.getenv("OPENAI_TERA_MODEL",         "")   # set via OPENAI_TERA_MODEL in .env
OPENAI_LUNA_MODEL         = os.getenv("OPENAI_LUNA_MODEL",         "")   # set via OPENAI_LUNA_MODEL in .env
OPENAI_OSS_MODEL          = os.getenv("OPENAI_OSS_MODEL",          "")   # set via OPENAI_OSS_MODEL in .env
OPENAI_DEEP_RESEARCH_MINI = os.getenv("OPENAI_DEEP_RESEARCH_MINI", "")   # set via OPENAI_DEEP_RESEARCH_MINI in .env
OPENAI_DEEP_RESEARCH      = os.getenv("OPENAI_DEEP_RESEARCH",      "")   # set via OPENAI_DEEP_RESEARCH in .env
OPENAI_IMAGE_MODEL        = os.getenv("OPENAI_IMAGE_MODEL",        "")   # set via OPENAI_IMAGE_MODEL in .env

CLAUDE_SONNET_5_MODEL = os.getenv("CLAUDE_SONNET_5_MODEL", "")   # set via CLAUDE_SONNET_5_MODEL in .env
CLAUDE_OPUS_48_MODEL  = os.getenv("CLAUDE_OPUS_48_MODEL",  "")   # set via CLAUDE_OPUS_48_MODEL in .env
CLAUDE_OPUS_5_MODEL   = os.getenv("CLAUDE_OPUS_5_MODEL",   "")   # set via CLAUDE_OPUS_5_MODEL in .env

VEO_MODEL = os.getenv("VEO_MODEL", "")   # set via VEO_MODEL in .env


# ---------------- SHARED COST TABLE (one source of truth) ----------------
#
# Cost per 1 million tokens (input_usd, output_usd).
# Keys use the constant values so env-var overrides propagate automatically.
# In-house local models are always free — callers check for "local" in model name.
# Mirrors root core/model_registry.py — keep in sync when pricing changes.

MODEL_COST_PER_1M: dict[str, tuple[float, float]] = {
    OPENAI_SIMPLE_MODEL:        (0.15,    0.60),
    OPENAI_CODING_MODEL:        (2.50,   15.00),
    OPENAI_LATEST_MODEL:        (5.00,   30.00),
    OPENAI_TERA_MODEL:          (2.00,   12.00),   # matches root core/model_registry.py
    OPENAI_LUNA_MODEL:          (0.20,    1.20),   # matches root core/model_registry.py
    OPENAI_OSS_MODEL:           (0.0,     0.0),   # in-house hosted — no cloud cost
    OPENAI_DEEP_RESEARCH_MINI:  (2.00,   10.00),
    OPENAI_DEEP_RESEARCH:       (15.00,  60.00),
    CLAUDE_PRIMARY_MODEL:       (3.00,   15.00),
    CLAUDE_HAIKU:               (0.80,    4.00),
    CLAUDE_OPUS_MODEL:          (15.00,  75.00),
    CLAUDE_OPUS_48_MODEL:       (15.00,  75.00),
    CLAUDE_OPUS_5_MODEL:        (15.00,  75.00),
    CLAUDE_SONNET_5_MODEL:      (3.00,   15.00),
    GEMINI_TEXT_MODEL:          (0.30,    1.20),
    GEMINI_CODING_LITE_MODEL:   (0.10,    0.40),
    GEMINI_IMAGE_MODEL:         (0.30,   30.00),
    LOCAL_LLM_MODEL_NAME:       (0.0,     0.0),
}


# ---------------- BLOCKED MODELS ----------------

BLOCKED_MODELS = {

    # Claude — retired/old models always blocked (mirrors root core/model_registry.py)
    "claude-opus-4-6",   # retired — superseded by Opus 4.7/4.8
    "claude-opus-4-5",
    "claude-opus-4",
    "claude-opus-3",
    "claude-sonnet-4-5", # retired — superseded by Sonnet 4.6

    # OpenAI — blocked variants
    "gpt-5.2-pro",
    "gpt-5.2",   # retired — replaced by gpt-5.4

}

# Opus 4-7 and 4-6 blocked only when ENABLE_OPUS=false (defaults to true)
if not ENABLE_OPUS:
    BLOCKED_MODELS.add(CLAUDE_OPUS_MODEL)
    BLOCKED_MODELS.add(CLAUDE_OPUS_46_MODEL)

# Operator extension — block additional models without code changes.
# Format: BLOCKED_MODELS_EXTRA=model-a,model-b
# Mirrors root core/model_registry.py — keep in sync.
_BLOCKED_MODELS_EXTRA: set[str] = {
    m.strip() for m in os.getenv("BLOCKED_MODELS_EXTRA", "").split(",") if m.strip()
}
if _BLOCKED_MODELS_EXTRA:
    BLOCKED_MODELS.update(_BLOCKED_MODELS_EXTRA)
