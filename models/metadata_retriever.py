# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CLASS-AWARE METADATA RETRIEVER
# ChromaDB removed — class/symbol lookup now handled by
# symbol_search() in models/hybrid_search.py (code_symbols table).
# This file is kept for import compatibility; metadata_search returns [].
# ============================================================

import re
from core.logger import logger

CLASS_PATTERN = re.compile(r"\b[A-Z][a-zA-Z0-9_]{2,}\b")


def extract_class_names(question: str):
    if not question:
        return []
    matches = CLASS_PATTERN.findall(question)
    seen = set()
    result = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def metadata_search(repo_filter, question, chroma_client=None):
    """ChromaDB removed. Symbol lookup now via hybrid_search.symbol_search()."""
    return []
