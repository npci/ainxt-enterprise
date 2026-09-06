# SPDX-License-Identifier: MIT
# Backwards-compat shim — import from app.core.llm_handler directly.
from app.core.llm_handler import *  # noqa: F401,F403
from app.core.llm_handler import (  # noqa: F401
    get_llm_client,
    Message,
    is_permanent_llm_error,
    PERMANENT_LLM_ERRORS,
    FallbackLLMClient,
    FALLBACK_LLM_MODEL,
)
