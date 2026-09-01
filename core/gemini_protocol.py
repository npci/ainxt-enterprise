# SPDX-License-Identifier: Apache-2.0
"""
Shared wire-format constants for the Gemini tool-use protocol carried across
the gateway (routers/messages_compat_router.py) ↔ LLM proxy
(services/llm_proxy/main.py) HTTP boundary.

Owned in core/ because it's a contract between two separate processes — neither
side should import from the other, but both must agree on the key name.
"""

# Gemini 2.5+/3.x require the original thought_signature to be replayed verbatim
# on every assistant function_call across turns; missing it returns 400
# INVALID_ARGUMENT. The proxy primarily caches signatures server-side by
# tool_call_id and additionally embeds a base64 copy on the OAI `function` object
# (and the Anthropic `tool_use` content_block) under this key, so the signature
# survives proxy restart / multi-replica cold-cache as well.
GEMINI_THOUGHT_SIG_KEY = "_gemini_thought_signature"
