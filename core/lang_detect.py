# SPDX-License-Identifier: MIT
# ============================================================
# LANGUAGE DETECTION — extended to the 22 scheduled Indian languages
#
# Two-tier:
#   1. langdetect — reliably distinguishes the languages it knows (incl. several
#      Indian languages that share no script with others).
#   2. Unicode script-block fallback — covers script-only languages langdetect
#      misses, and disambiguates by writing system.
#
# Returns a supported ISO code (see _SUPPORTED), "mixed", or "unknown".
# Reproducible (langdetect seeded). Detection is a CONVENIENCE — the user's
# explicit /language preference is the authoritative source for translate-in.
# ============================================================

from core.logger import logger

# ISO codes the translation stack supports. MUST align with
# core.translation_wrapper.SUPPORTED_LANGS and services/translate_svc FLORES map.
_SUPPORTED = {
    "as", "bn", "brx", "doi", "kok", "gu", "hi", "kn", "ks_Arab", "ks_Deva",
    "mai", "ml", "mr", "mni_Beng", "mni_Mtei", "ne", "or", "pa", "sa", "sat",
    "sd_Arab", "sd_Deva", "ta", "te", "ur", "en",
}

# langdetect ISO-639-1 codes we accept directly (it distinguishes these well).
_LANGDETECT_DIRECT = {
    "en", "hi", "bn", "gu", "kn", "ml", "mr", "ne", "pa", "ta", "te", "ur",
}

# Unicode script block → representative supported language (coarse fallback).
# Languages sharing Devanagari (hi/mr/ne/sa/mai/doi/brx/kok) default to hi
# unless langdetect disambiguated above. Order: most-specific first.
_SCRIPT_RANGES = [
    ("஀", "௿", "ta"),        # Tamil
    ("ఀ", "౿", "te"),        # Telugu
    ("ಀ", "೿", "kn"),        # Kannada
    ("ഀ", "ൿ", "ml"),        # Malayalam
    ("઀", "૿", "gu"),        # Gujarati
    ("਀", "੿", "pa"),        # Gurmukhi (Punjabi)
    ("଀", "୿", "or"),        # Odia
    ("ঀ", "৿", "bn"),        # Bengali / Assamese
    ("᱐", "᱿", "sat"),       # Ol Chiki (Santali)
    ("ꯀ", "꯿", "mni_Mtei"),  # Meetei Mayek (Manipuri)
    ("ऀ", "ॿ", "hi"),        # Devanagari (hi/mr/ne/sa/mai/doi/brx/kok)
    ("؀", "ۿ", "ur"),        # Arabic (Urdu / Kashmiri-Arab / Sindhi-Arab)
]


def detect_language(text: str) -> str:
    """
    Detect the primary language of *text*.

    Returns one of the supported ISO codes (see _SUPPORTED), "mixed", or
    "unknown". Falls back gracefully to a Unicode-script heuristic when
    langdetect is unavailable or uncertain.
    """
    if not text or len(text.strip()) < 3:
        return "unknown"

    sample = text[:2000]

    # 1) langdetect for languages it reliably distinguishes
    try:
        from langdetect import detect_langs, DetectorFactory, LangDetectException
        DetectorFactory.seed = 0  # reproducible
        try:
            langs = detect_langs(sample)
        except LangDetectException:
            langs = []
        if langs:
            top = langs[0]
            if top.lang in _LANGDETECT_DIRECT and top.prob >= 0.80:
                return top.lang
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"lang_detect: detection error — {e}")

    # 2) Unicode-script fallback (covers script-only langs + low-confidence cases)
    return _script_detect(sample)


def _script_detect(text: str) -> str:
    """Coarse detection by Unicode script block. Disambiguates by writing system."""
    counts: dict[str, int] = {}
    ascii_alpha = 0
    for c in text:
        if c.isascii() and c.isalpha():
            ascii_alpha += 1
            continue
        for lo, hi, lang in _SCRIPT_RANGES:
            if lo <= c <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break

    indic = sum(counts.values())
    total = max(ascii_alpha + indic, 1)

    if not counts:
        return "en" if ascii_alpha else "unknown"
    if indic / total < 0.15:
        return "en"
    if ascii_alpha / total > 0.30 and indic / total >= 0.15:
        return "mixed"
    return max(counts, key=counts.__getitem__)


def is_supported(iso: str) -> bool:
    """True if the ISO code is one the translation stack handles."""
    return iso in _SUPPORTED
