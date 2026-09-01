# SPDX-License-Identifier: Apache-2.0
# ============================================================
# MULTILINGUAL TRANSLATION WRAPPER (gateway-side)
#
# Ties the prose-vs-code segmenter (core.prose_translate) to the IndicTrans2
# microservice (services/translate_svc, :8006). ONLY natural-language prose is
# translated; ALL code, identifiers, paths and docs stay English verbatim —
# the segmenter guarantees the translator never even sees a code byte.
#
# GRACEFUL DEGRADATION (non-negotiable): if translate_svc is unreachable or
# returns anything malformed, the ORIGINAL text is returned unchanged.
# Translation must NEVER block, drop, or corrupt a response — same spirit as
# the redact-don't-block compliance rule.
# ============================================================

from __future__ import annotations

import os

import httpx

try:
    from core.logger import logger  # type: ignore
except Exception:  # pragma: no cover - fallback for standalone use
    import logging
    logger = logging.getLogger("translation_wrapper")

from core.prose_translate import translate_prose

# No hardcoded localhost default — unset/unreachable fails the httpx calls
# below, both of which already degrade gracefully (pass-through untranslated /
# health "error").
_TRANSLATE_SVC_URL = os.getenv("TRANSLATE_SVC_URL", "").rstrip("/")
_TIMEOUT = float(os.getenv("TRANSLATE_SVC_TIMEOUT", "20"))

# ISO codes translate_svc supports. Kept as a local copy so this module never
# imports services.translate_svc (which loads ML models at import time).
# MUST stay in sync with services/translate_svc/config.FLORES.
SUPPORTED_LANGS = {
    "as", "bn", "brx", "doi", "kok", "gu", "hi", "kn", "ks_Arab", "ks_Deva",
    "mai", "ml", "mr", "mni_Beng", "mni_Mtei", "ne", "or", "pa", "sa", "sat",
    "sd_Arab", "sd_Deva", "ta", "te", "ur", "en",
}


def is_supported(iso: str) -> bool:
    """True if `iso` is a language the translation stack supports."""
    return iso in SUPPORTED_LANGS


def _svc_translate_batch(texts: list[str], src_iso: str, tgt_iso: str) -> list[str]:
    """
    Call translate_svc /translate_batch on a list of pure-prose fragments.
    On ANY failure (service down, timeout, malformed response) return the
    inputs unchanged so the caller degrades to English rather than failing.
    """
    if not texts:
        return texts
    try:
        resp = httpx.post(
            f"{type(_TRANSLATE_SVC_URL).__name__}/translate_batch",
            json={"texts": texts, "source_lang": src_iso, "target_lang": tgt_iso},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        out = resp.json().get("translations")
        if (
            isinstance(out, list)
            and len(out) == len(texts)
            and all(isinstance(x, str) for x in out)
        ):
            return out
        logger.warning("translation_wrapper: translate_svc returned malformed batch — passing through untranslated")
        return texts
    except Exception:
        logger.warning(f"translation_wrapper: translate_svc unreachable/error — passing through untranslated")
        return texts


def translate_text(text: str, src_iso: str, tgt_iso: str) -> str:
    """
    Translate the natural-language prose in `text` from src_iso to tgt_iso,
    keeping every code construct verbatim. Returns `text` unchanged when:
      - text is empty, or src_iso == tgt_iso,
      - either language is unsupported,
      - the segmenter or translate_svc errors (graceful degradation).
    """
    if not text or src_iso == tgt_iso:
        return text
    if not (is_supported(src_iso) and is_supported(tgt_iso)):
        return text

    def _fn(prose_list: list[str]) -> list[str]:
        return _svc_translate_batch(prose_list, src_iso, tgt_iso)

    try:
        return translate_prose(text, tgt_iso, _fn)
    except Exception:  # pragma: no cover - segmenter is hardened, belt-and-braces
        logger.warning(f"translation_wrapper: segmentation error — passing through untranslated")
        return text


def translate_to_english(text: str, src_iso: str) -> str:
    """User input → English (for the model). Prose only; code stays verbatim."""
    return translate_text(text, src_iso, "en")


def translate_from_english(text: str, tgt_iso: str) -> str:
    """Model's English prose → user's language. Code stays verbatim."""
    return translate_text(text, "en", tgt_iso)


def health() -> dict:
    """Best-effort health probe of translate_svc (for gateway /health)."""
    try:
        r = httpx.get(f"{type(_TRANSLATE_SVC_URL).__name__}/health", timeout=3)
        r.raise_for_status()
        return {"translate_svc": "ok", **(r.json() if r.headers.get("content-type", "").startswith("application/json") else {})}
    except Exception:
        return {"translate_svc": f"error"}
