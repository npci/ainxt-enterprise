# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TRANSLATE SERVICE — configuration
# ============================================================
import os

TRANSLATE_SVC_PORT  = int(os.getenv("TRANSLATE_SVC_PORT", "8006"))

# No localhost default: main.py wraps TranslateCache.connect() in try/except
# and disables caching gracefully if Redis is unreachable/unset.
REDIS_HOST          = os.getenv("REDIS_HOST", "")
REDIS_PORT          = int(os.getenv("REDIS_PORT", "6379"))
REDIS_TRANSLATE_DB  = 9          # dedicated DB for translation cache
TRANSLATE_CACHE_TTL = 86400      # 24h — translations are deterministic

# ── Model IDs ─────────────────────────────────────────────────────────────────
# CPU dev:  200M distilled variants (loaded below)
# Prod GPU: swap to the 1B variants and use torch_dtype=float16 + device="cuda"
#   INDIC_EN_MODEL=ai4bharat/indictrans2-indic-en-1B
#   EN_INDIC_MODEL=ai4bharat/indictrans2-en-indic-1B
INDIC_EN_MODEL = os.getenv(
    "INDIC_EN_MODEL",
    "ai4bharat/indictrans2-indic-en-dist-200M",
)
EN_INDIC_MODEL = os.getenv(
    "EN_INDIC_MODEL",
    "ai4bharat/indictrans2-en-indic-dist-200M",
)

TRANSLATE_DEVICE = os.getenv("TRANSLATE_DEVICE", "cpu")

MAX_BATCH = 32   # max sentences per IndicTrans2 generate() call

# ── FLORES-200 language code map ──────────────────────────────────────────────
# ISO code → FLORES-200 code used by IndicTrans2 / IndicProcessor.
# 22 scheduled Indian languages produce 26 codes due to dual-script coverage:
#   Kashmiri  (ks_Arab / ks_Deva), Manipuri (mni_Beng / mni_Mtei),
#   Sindhi    (sd_Arab / sd_Deva), Konkani uses gom_Deva (NOT kok_Deva).
# English is included so en↔Indic round-trips work with a single map lookup.
FLORES: dict[str, str] = {
    "as":       "asm_Beng",   # Assamese
    "bn":       "ben_Beng",   # Bengali
    "brx":      "brx_Deva",   # Bodo
    "doi":      "doi_Deva",   # Dogri
    "kok":      "gom_Deva",   # Konkani (Goan Konkani — FLORES-200 code is gom_Deva)
    "gu":       "guj_Gujr",   # Gujarati
    "hi":       "hin_Deva",   # Hindi
    "kn":       "kan_Knda",   # Kannada
    "ks_Arab":  "kas_Arab",   # Kashmiri (Arabic script — standard)
    "ks_Deva":  "kas_Deva",   # Kashmiri (Devanagari script)
    "mai":      "mai_Deva",   # Maithili
    "ml":       "mal_Mlym",   # Malayalam
    "mr":       "mar_Deva",   # Marathi
    "mni_Beng": "mni_Beng",   # Manipuri (Bengali script)
    "mni_Mtei": "mni_Mtei",   # Manipuri (Meitei/Meetei Mayek script)
    "ne":       "npi_Deva",   # Nepali
    "or":       "ory_Orya",   # Odia
    "pa":       "pan_Guru",   # Punjabi
    "sa":       "san_Deva",   # Sanskrit
    "sat":      "sat_Olck",   # Santali
    "sd_Arab":  "snd_Arab",   # Sindhi (Arabic script — standard)
    "sd_Deva":  "snd_Deva",   # Sindhi (Devanagari script)
    "ta":       "tam_Taml",   # Tamil
    "te":       "tel_Telu",   # Telugu
    "ur":       "urd_Arab",   # Urdu
    "en":       "eng_Latn",   # English
}


def to_flores(iso: str) -> str:
    """Map an ISO language code to its FLORES-200 code.

    Raises ValueError with a clear message for unsupported languages.
    """
    code = FLORES.get(iso)
    if code is None:
        supported = ", ".join(sorted(FLORES.keys()))
        raise ValueError(
            f"Unsupported language code '{iso}'. "
            f"Supported codes: {supported}"
        )
    return code


def is_supported(iso: str) -> bool:
    """Return True if the ISO code is in the FLORES map."""
    return iso in FLORES
