# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TRANSLATE SERVICE — IndicTrans2 model wrapper
#
# Both models (indic-en and en-indic) are loaded at MODULE IMPORT TIME,
# device=cpu (project rule: never lazy-load ML models, never use MPS).
# Prod GPU: set TRANSLATE_DEVICE=cuda and swap to 1B model IDs via env vars.
# ============================================================

import logging
import math

import torch
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from IndicTransToolkit.processor import IndicProcessor

from services.translate_svc.config import (
    INDIC_EN_MODEL,
    EN_INDIC_MODEL,
    TRANSLATE_DEVICE,
    MAX_BATCH,
    to_flores,
)

try:
    from core.logger import logger  # type: ignore
except Exception:
    logger = logging.getLogger("translate_svc")

# ---------------------------------------------------------------------------
# IndicTrans2 — loaded at module import time (project rule)
# CPU dev:  indic-en-dist-200M + en-indic-dist-200M  (float32)
# Prod GPU: swap to 1B variants via env vars, float16, device=cuda
# ---------------------------------------------------------------------------

# GPU (cuda) → float16 (half the VRAM, faster); CPU → float32 (required —
# float16 on CPU-only torch causes precision errors / crashes).
_DTYPE = torch.float16 if str(TRANSLATE_DEVICE).startswith("cuda") else torch.float32

logger.info(f"TranslateSvc: loading indic-en model '{INDIC_EN_MODEL}' on device='{TRANSLATE_DEVICE}' (dtype={_DTYPE}) …")

_indic_en_tokenizer = AutoTokenizer.from_pretrained(
    INDIC_EN_MODEL,
    trust_remote_code=True,
)
_indic_en_model = AutoModelForSeq2SeqLM.from_pretrained(
    INDIC_EN_MODEL,
    trust_remote_code=True,
    torch_dtype=_DTYPE,
    low_cpu_mem_usage=True,
).to(TRANSLATE_DEVICE)
_indic_en_model.eval()

logger.info(f"TranslateSvc: loading en-indic model '{EN_INDIC_MODEL}' on device='{TRANSLATE_DEVICE}' …")

_en_indic_tokenizer = AutoTokenizer.from_pretrained(
    EN_INDIC_MODEL,
    trust_remote_code=True,
)
_en_indic_model = AutoModelForSeq2SeqLM.from_pretrained(
    EN_INDIC_MODEL,
    trust_remote_code=True,
    torch_dtype=_DTYPE,
    low_cpu_mem_usage=True,
).to(TRANSLATE_DEVICE)
_en_indic_model.eval()

_ip = IndicProcessor(inference=True)

logger.info("TranslateSvc: IndicTrans2 models ready")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def translate_batch(
    sentences: list[str],
    src_flores: str,
    tgt_flores: str,
) -> list[str]:
    """Translate a list of sentences using IndicTrans2.

    src_flores / tgt_flores must be FLORES-200 codes, e.g.:
      "eng_Latn" -> "hin_Deva"   (English to Hindi — uses en-indic model)
      "hin_Deva" -> "eng_Latn"   (Hindi to English — uses indic-en model)

    Inputs are automatically chunked to MAX_BATCH to avoid OOM on long lists.
    Returns translations in the same order as the input.
    """
    if not sentences:
        return []

    # Select model pair based on source language
    if src_flores == "eng_Latn":
        tokenizer = _en_indic_tokenizer
        model     = _en_indic_model
    else:
        tokenizer = _indic_en_tokenizer
        model     = _indic_en_model

    results: list[str] = []
    n_chunks = math.ceil(len(sentences) / MAX_BATCH)

    for chunk_idx in range(n_chunks):
        chunk = sentences[chunk_idx * MAX_BATCH : (chunk_idx + 1) * MAX_BATCH]

        # Step 1: IndicProcessor preprocess
        batch = _ip.preprocess_batch(
            chunk,
            src_lang=src_flores,
            tgt_lang=tgt_flores,
        )

        # Step 2: Tokenize
        inputs = tokenizer(
            batch,
            truncation=True,
            padding="longest",
            return_tensors="pt",
            return_attention_mask=True,
        ).to(TRANSLATE_DEVICE)

        # Step 3: Generate
        with torch.inference_mode():
            generated_tokens = model.generate(
                **inputs,
                use_cache=True,
                min_length=0,
                max_length=256,
                num_beams=5,
                num_return_sequences=1,
            )

        # Step 4: Decode
        decoded = tokenizer.batch_decode(
            generated_tokens,
            skip_special_tokens=True,
            clean_up_tokenization_spaces=True,
        )

        # Step 5: IndicProcessor postprocess
        translations = _ip.postprocess_batch(decoded, lang=tgt_flores)
        results.extend(translations)

    return results


def translate(text: str, src_iso: str, tgt_iso: str) -> str:
    """Translate a single text string from src_iso to tgt_iso.

    ISO codes are mapped to FLORES-200 via config.to_flores().
    If src_iso == tgt_iso the input is returned unchanged with no model call.
    Raises ValueError for unsupported language codes (propagated from to_flores).
    """
    if src_iso == tgt_iso:
        return text

    src_flores = to_flores(src_iso)
    tgt_flores = to_flores(tgt_iso)

    # Translate the whole string as a single batch item.
    # Sentence-level splitting would improve quality for long multi-sentence
    # texts but is deferred — the batch endpoint handles multiple sentences
    # explicitly, and for the single-text path callers can pre-split.
    translations = translate_batch([text], src_flores, tgt_flores)
    return translations[0] if translations else text
