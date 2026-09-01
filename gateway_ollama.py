# SPDX-License-Identifier: Apache-2.0
# ============================================================
# OLLAMA GATEWAY — DEPRECATED
# All LLM inference routes through gateway_local_llm (in-house proxy).
# Ollama is used ONLY by the embed service for nomic-embed-text.
# This shim exists so old import paths don't break.
# ============================================================

from core.logger import logger, get_request_id as _get_request_id


def generate(
    prompt: str,
    system_prompt: str = "",
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> str:
    """
    Redirects to in-house local LLM proxy (gateway_local_llm).
    Ollama /api/chat is NOT used for LLM inference in prod.
    """
    # request_id is propagated automatically via thread-local context;
    # gateway_local_llm.generate() reads it via _get_request_id() and
    # forwards it as X-Request-ID to the in-house LLM service.
    from gateway_local_llm import get_local_gateway
    gw = get_local_gateway()
    if system_prompt:
        full_prompt = f"{system_prompt}\n\n{prompt}"
    else:
        full_prompt = prompt
    return "".join(gw.generate(full_prompt, tier="simple"))


def count_tokens(text: str) -> int:
    """Rough token estimate (4 chars ≈ 1 token)."""
    return max(1, len(text) // 4)
