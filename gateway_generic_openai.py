# SPDX-License-Identifier: MIT
# ============================================================
# GENERIC OPENAI-COMPATIBLE GATEWAY
#
# Lightweight OpenAI-compatible client for admin-configured "openai_compatible"
# LLM providers (OpenRouter, Together, Groq, or any other custom
# chat-completions-API-shaped provider added via the "LLM Providers" admin
# screen — see core/llm_provider_registry.py).
#
# Unlike gateway_claude.py / gateway_openai.py / gateway_local_llm.py, this is
# NOT a process-wide singleton bound to module-level env-var constants — an
# admin can configure several distinct openai_compatible providers with
# different base_url/api_key pairs, so one instance is built per call from
# core.llm_provider_registry.get_client_for(model_id) (see
# models/model_router.py's TIER_REGISTRY dispatch branch, the only caller).
#
# Deliberately has NO cross-vendor fallback chain, unlike the built-in
# gateways' _try_* methods — an explicitly admin-picked model should surface
# an error if it fails, not silently run a different vendor's model.
# ============================================================

import uuid
from typing import Optional

from openai import OpenAI

from core.logger import logger, get_request_id as _get_request_id


class GenericOpenAIGateway:
    """Matches models/provider_protocol.py's shared generate(prompt, model=None,
    **kwargs) -> Generator[str, None, None] contract."""

    def __init__(self, base_url: str, api_key: Optional[str] = None):
        if not base_url:
            raise ValueError("GenericOpenAIGateway requires a base_url")
        self.base_url = base_url.rstrip("/")
        self._client = OpenAI(base_url=self.base_url, api_key=api_key or "not-needed")
        self._last_input_tokens = 0
        self._last_output_tokens = 0

    def generate(self, prompt, model: Optional[str] = None, **kwargs):
        if not model:
            yield "Error: GenericOpenAIGateway requires an explicit model"
            return

        if isinstance(prompt, list):
            messages_payload = [
                {"role": m["role"], "content": m.get("content") or ""} for m in prompt
            ]
        else:
            messages_payload = [{"role": "user", "content": prompt}]

        self._last_input_tokens = 0
        self._last_output_tokens = 0

        upstream = _get_request_id()
        request_id = upstream if upstream and upstream != "-" else str(uuid.uuid4())
        logger.info(
            f"[LLM DISPATCH] provider=openai_compatible base_url={self.base_url} "
            f"model={model} request_id={request_id}"
        )

        try:
            stream = self._client.chat.completions.create(
                model=model, messages=messages_payload, stream=True,
            )
            response_buf = []
            for chunk in stream:
                if chunk.choices:
                    piece = getattr(chunk.choices[0].delta, "content", None)
                    if piece:
                        response_buf.append(piece)
                        yield piece
                usage = getattr(chunk, "usage", None)
                if usage:
                    self._last_input_tokens = usage.prompt_tokens or 0
                    self._last_output_tokens = usage.completion_tokens or 0

            full_response = "".join(response_buf)
            logger.info(
                f"[OPENAI-COMPAT USAGE] request_id={request_id} base_url={self.base_url} "
                f"model={model} in={self._last_input_tokens} out={self._last_output_tokens} "
                f"response_chars={len(full_response)}"
            )
        except Exception as e:
            logger.error(
                f"[OPENAI-COMPAT ERROR] request_id={request_id} base_url={self.base_url} "
                f"model={model}: {e}"
            )
            yield f"Error: {model} call failed ({e})"


def get_generic_gateway(base_url: str, api_key: Optional[str] = None) -> GenericOpenAIGateway:
    """Factory — intentionally no process-wide caching (unlike
    gateway_local_llm.get_local_gateway()): different registry providers have
    different base_url/api_key pairs, so ModelRouter's TIER_REGISTRY dispatch
    constructs one per call. Cheap — just an SDK client object, no network
    call until .generate() is actually invoked."""
    return GenericOpenAIGateway(base_url=base_url, api_key=api_key)
