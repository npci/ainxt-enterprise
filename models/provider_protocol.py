# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt PROVIDER GATEWAY PROTOCOL
# Documentation-as-code for the existing gateway contract.
#
# This module intentionally defines NOTHING but a typing.Protocol. It does
# not change any runtime behaviour, it is not imported by any gateway, and
# no gateway needs to inherit from it — Python's structural typing means a
# class satisfies this Protocol just by having a compatible generate()
# method, with zero code changes required.
#
# WHY A PROTOCOL AND NOT AN ABC:
#   An ABC (abstract base class) would require every gateway to explicitly
#   inherit from it, which is a class-hierarchy change to gateway_claude.py,
#   gateway_openai.py, gateway_gemini.py, and gateway_local_llm.py. A
#   Protocol requires none of that — it is checked structurally (duck
#   typing, enforced by static analysis / isinstance() with
#   @runtime_checkable), so it documents the contract without touching any
#   existing gateway file.
#
# WHY THE FOUR GATEWAYS STAY INDEPENDENT CLASSES, NOT ONE SHARED BASE CLASS:
#   Each provider owns its own SDK, and each SDK evolves on its own
#   schedule — new parameters, renamed methods, deprecated fields — entirely
#   outside our control. If all four gateways were forced under one shared
#   base class, a breaking SDK change from any single provider (e.g. OpenAI
#   deprecating a parameter) would force an edit to shared code that every
#   other gateway depends on, risking a change to Claude/Gemini/Local
#   behaviour triggered by an unrelated OpenAI update. Keeping each gateway
#   as an independent class means a provider-side change stays contained to
#   that one file.
#
# KNOWN SIGNATURE DRIFT (verified against source, 2026-08-28):
#   gateway_claude.generate(prompt, model=CLAUDE_MODEL, temperature=0,
#                            max_tokens=32000, stream=True)
#   gateway_openai.generate(prompt, model=None, precleared=False,
#                            precleared_findings=None)
#   gateway_gemini.generate(prompt, precleared=False,
#                            precleared_findings=None, model=None)
#   gateway_local_llm.generate(prompt, model=None, tier="simple", *,
#                               max_tokens=None, disable_reasoning=False)
#
#   `model` is keyword-only-safe in all four (never required positionally
#   past `prompt`), which is why model_router.py's existing keyword-only
#   call sites already work today without this Protocol. This module makes
#   that common subset explicit and visible to static type checkers and to
#   the next contributor, instead of leaving it implicit in four docstrings.
# ============================================================

from typing import Any, Generator, Optional, Protocol, runtime_checkable


@runtime_checkable
class GatewayProtocol(Protocol):
    """The real, current contract shared by all four LLM provider gateways.

    Every gateway's generate() accepts at least `prompt` and an optional
    `model`, and returns either a streaming Generator[str] (default) or a
    plain str (when stream=False, where supported). Anything beyond that
    (temperature, stream, precleared, precleared_findings, tier,
    disable_reasoning, max_tokens) is provider-specific and passed through
    **kwargs by callers today — this Protocol is not meant to force those
    to be unified, only to document the guaranteed common surface.
    """

    def generate(
        self,
        prompt: str | list[dict],
        model: Optional[str] = None,
        **kwargs: Any,
    ) -> Generator[str, None, None] | str:
        ...


# Optional attributes several gateways expose after a call, read today via
# getattr(gw, "...", default) in model_router.py's _propagate_tokens() —
# listed here for visibility, not enforced (not every gateway sets all of
# these, and that's fine: they're read with a safe default everywhere).
OPTIONAL_POST_CALL_ATTRS = (
    "_last_input_tokens",
    "_last_output_tokens",
    "_last_cache_read_tokens",
    "_last_cache_creation_tokens",
    "_last_thinking_text",
)
