# SPDX-License-Identifier: Apache-2.0
# ============================================================
# IMPORTS
# ============================================================

import json
import os
import re
import hashlib

import httpx

from core.config import RDB_CACHE
from core.kv import get_kv
from core.logger import logger
from core.model_registry import CLAUDE_HAIKU as _CLAUDE_HAIKU


# ============================================================
# LLM FALLBACK (for ambiguous queries where regex confidence < 0.7)
# Routes through LLM proxy on the LLM proxy server — never calls Anthropic SDK directly.
# ============================================================

_LLM_FALLBACK_SYSTEM = (
    "Classify this query into one of: simple (greeting, trivial), "
    "medium (coding, analysis), complex (deep reasoning, multi-step). "
    "Respond with ONLY the tier name."
)

_VALID_TIERS = {"simple", "medium", "complex"}

# ARCH-F-MISC-010: creating a new httpx.Client on every classify call creates
# and tears down a TCP connection pool on every invocation. A module-level
# singleton reuses the connection pool, reducing latency and resource churn.
_HTTP_CLIENT = httpx.Client(timeout=httpx.Timeout(15.0, connect=3.0))

# Register a shutdown hook so the connection pool is cleanly closed when the
# process exits — matching the explicit aclose() calls used for the async
# httpx clients in services/llm_proxy/main.py's _lifespan() shutdown block.
import atexit as _atexit
_atexit.register(_HTTP_CLIENT.close)


def _llm_classify(question: str) -> str:
    """
    Call Claude Haiku via the LLM proxy to classify a query when regex confidence < 0.7.
    Routes through the LLM proxy server LLM proxy (LLM_PROXY_URL env var) — never calls Anthropic SDK directly.
    Falls back to "medium" on any error or when proxy is not configured.
    """
    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")
    if not proxy_url:
        logger.warning("LLM classifier: LLM_PROXY_URL not set — defaulting to medium")
        return "medium"
    logger.info(f"_llm_classify with URL {proxy_url} ")
    # Prepend system instruction to the prompt so the proxy's single-string
    # format carries the full context (proxy /llm/generate takes one prompt str).
    combined_prompt = f"{_LLM_FALLBACK_SYSTEM}\n\nQuery: {question}"
    try:
        label_tokens: list[str] = []
        from core.proxy_tool_use import llm_proxy_headers as _lph
        with _HTTP_CLIENT.stream(
            "POST",
            f"{proxy_url}/llm/generate",
            json={"provider": "claude", "prompt": combined_prompt, "model": _CLAUDE_HAIKU},
            headers=_lph(),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "t" in obj:
                    label_tokens.append(obj["t"])

        label = "".join(label_tokens).strip().lower()
        # The model may return "simple.", "MEDIUM" etc — take first word
        label = label.split()[0].rstrip(".,") if label else ""
        if label in _VALID_TIERS:
            logger.info(f"LLM classifier fallback (proxy) → {label}")
            return label
        logger.warning(f"LLM classifier (proxy) returned unexpected label '{label}' — defaulting to medium")
    except Exception as e:
        logger.warning(f"LLM classifier fallback (proxy) failed ({e}) — defaulting to medium")
    return "medium"


# ============================================================
# KV CACHE (DB=0)
# Backend selected via REDIS_CLIENT_CONFIG_DB0.
# ============================================================

redis_client = get_kv(RDB_CACHE, decode_responses=True)

CLASSIFIER_CACHE_TTL = 86400 * 7


# ============================================================
# SAFE PATTERNS (ONLY FOR OBVIOUS CASES)
# ============================================================

GREETING_PATTERN = re.compile(
    r"^(?:"
    # Core greetings + optional trailing "there"/"team"/"all" and punctuation.
    r"(?:hi|hii+|hello+|hey+|heya|hiya|yo|hola|namaste|namaskar|howdy|sup|wassup|"
    r"greetings|good\s*(?:morning|afternoon|evening|day)|gm|ge|hey\s*there|hi\s*there|hello\s*there)"
    r"(?:\s+(?:there|team|all|everyone|folks|guys))?"
    r"|"
    # Acknowledgements / thanks / sign-offs that also warrant a light reply.
    r"(?:thanks|thank\s*you|thanks\s*a\s*lot|thankyou|thx|ty|ok|okay|okey|k|kk|"
    r"cool|great|nice|got\s*it|alright|bye|goodbye|see\s*you|cya)"
    r")[\s!.]*$",
    re.IGNORECASE
)

VERY_SIMPLE_PATTERN = re.compile(
    r"^[a-zA-Z0-9\s]{1,30}$"
)

CODE_SIGNAL_PATTERN = re.compile(
    r"\b(class|method|implementation|architecture|repository|api|error|exception)\b",
    re.IGNORECASE
)

# True compound CamelCase — BaseChannel, ISOMsg, TransactionManager.
# Does NOT match plain capitalized words like "Java", "Fibonacci", "Spring".
CLASS_NAME_PATTERN = re.compile(
    r"\b(?:[A-Z]{2,}[a-z][a-zA-Z0-9_]*|[A-Z][a-z]{2,}[A-Z][a-zA-Z0-9_]*)\b"
)

# Questions that ask the AI to WRITE/GENERATE code are always general knowledge,
# regardless of what language or topic is mentioned.
WRITE_CODE_PATTERN = re.compile(
    r"^\s*(write|implement|create|build|make|generate|code|give me|show me|"
    r"can you write|how to write|how to implement|how do i|how to create)\b",
    re.IGNORECASE
)


# ============================================================
# CACHE KEY
# ============================================================

def _cache_key(prefix: str, question: str):

    key = hashlib.sha256(
        question.strip().lower().encode()
    ).hexdigest()

    return f"{prefix}:{key}"


# ============================================================
# COMPLEXITY CLASSIFIER
# ============================================================

def classify_query_complexity(question: str) -> str:
    """
    Production-grade query complexity classification.

    Fast path: regex heuristics with a confidence score.
    Slow path: when regex confidence < 0.7, delegates to Claude Haiku so that
               ambiguous queries are not mis-classified.

    Returns:
        simple
        medium
        complex
    """

    try:

        if not question:
            return "simple"

        cache_key = _cache_key("complexity", question)

        cached = redis_client.get(cache_key)

        if cached:
            return cached

        # Use the confidence-aware path (with LLM fallback) as the single source
        # of truth so that classify_query_complexity and classify_with_confidence_llm
        # are always consistent.
        label, _conf = classify_with_confidence_llm(question)

        redis_client.setex(
            cache_key,
            CLASSIFIER_CACHE_TTL,
            label,
        )

        return label

    except Exception as e:

        logger.error(f"Complexity classification failed: {e}")

        return "medium"


# ============================================================
# DOMAIN CLASSIFIER (SAFE, GENERIC)
# ============================================================

def detect_query_domain(question: str) -> str:
    """
    Generic domain detection.

    Returns:
        general
        code
    """

    try:

        if not question:
            return "general"

        cache_key = _cache_key("domain", question)

        cached = redis_client.get(cache_key)

        if cached:
            return cached

        q = question.strip()

        # "write/implement/create X" → always general (user wants AI to generate,
        # not look it up in the repo). Takes priority over class-name detection.
        if WRITE_CODE_PATTERN.search(q):
            redis_client.setex(cache_key, CLASSIFIER_CACHE_TTL, "general")
            return "general"

        # class name signal → code
        if CLASS_NAME_PATTERN.search(q):

            redis_client.setex(
                cache_key,
                CLASSIFIER_CACHE_TTL,
                "code"
            )

            return "code"


        # code keywords signal
        if CODE_SIGNAL_PATTERN.search(q):

            redis_client.setex(
                cache_key,
                CLASSIFIER_CACHE_TTL,
                "code"
            )

            return "code"


        redis_client.setex(
            cache_key,
            CLASSIFIER_CACHE_TTL,
            "general"
        )

        return "general"


    except Exception as e:

        logger.error(f"Domain classification failed: {e}")

        return "general"


# ============================================================
# CONFIDENCE-AWARE COMPLEXITY CLASSIFIER
# ============================================================

def classify_with_confidence(question: str) -> tuple:
    """
    Returns (label, confidence) where:
        label      — "simple" | "medium" | "complex"
        confidence — float 0.0–1.0 (1.0 = definitive, 0.5 = uncertain)

    Confidence is used by model_router to optionally upgrade the tier
    when the classifier is unsure.
    """
    try:
        if not question:
            return ("simple", 1.0)

        q = question.strip()

        # Definitive simple — greeting
        if GREETING_PATTERN.fullmatch(q):
            return ("simple", 1.0)

        # Very short factual queries — high confidence simple
        if len(q.split()) <= 2 and not CLASS_NAME_PATTERN.search(q):
            return ("simple", 0.9)

        # Definitive complex — class name or code signal present
        if CLASS_NAME_PATTERN.search(q):
            return ("complex", 0.95)

        if CODE_SIGNAL_PATTERN.search(q):
            # Code-related but no class name — high but not maximum confidence
            return ("complex", 0.85)

        # Medium-length queries — moderate confidence medium
        word_count = len(q.split())
        if word_count > 20:
            return ("complex", 0.7)

        if word_count > 8:
            return ("medium", 0.75)

        # Short non-greeting, non-code — uncertain medium
        return ("medium", 0.6)

    except Exception as e:
        logger.error(f"classify_with_confidence failed: {e}")
        return ("medium", 0.5)


def classify_with_confidence_llm(question: str) -> tuple:
    """
    Confidence-aware classifier with LLM fallback.

    Fast path: regex heuristics (same as classify_with_confidence).
    Slow path: when regex confidence < 0.7, delegates to Claude Haiku for a
               more accurate classification.

    Returns (label, confidence) where confidence reflects the final decision
    source (regex confidence if fast path, 0.9 if LLM resolved the ambiguity).
    """
    label, confidence = classify_with_confidence(question)
    if confidence < 0.7:
        llm_label = _llm_classify(question)
        logger.info(
            f"Classifier: regex=({label},{confidence}) → LLM fallback → {llm_label}"
        )
        return (llm_label, 0.9)
    return (label, confidence)