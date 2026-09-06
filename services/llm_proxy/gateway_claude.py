# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt / RBI PRODUCTION CLAUDE GATEWAY
# CLASS BASED • PCI SAFE • ENTERPRISE READY
# ============================================================

import os
import uuid
import contextvars
from typing import AsyncGenerator, Optional

from anthropic import AsyncAnthropic

from core.logger import logger, get_request_id as _get_request_id
# NOTE: Compliance (PCI/PII detection + redaction) lives EXCLUSIVELY in the
# backend gateway layer (Tier 1). This proxy forwards already-validated,
# already-redacted text verbatim. Do NOT reintroduce a compliance engine here.

from core.model_registry import CLAUDE_PRIMARY_MODEL

from dotenv import load_dotenv
load_dotenv()

CLAUDE_MODEL = CLAUDE_PRIMARY_MODEL


# Env flag: set DISABLE_ANTHROPIC_API=true in .env to block all outbound Anthropic calls.
_ANTHROPIC_DISABLED = os.getenv("DISABLE_ANTHROPIC_API", "").lower() in ("1", "true", "yes")

def _no_temperature_model(model: str) -> bool:
    """Claude Opus 4+/5+ and Sonnet 5+ deprecated the temperature parameter — omit it entirely."""
    return "opus-4" in model or "opus-5" in model or "sonnet-5" in model


# ContextVar storage so concurrent asyncio tasks each have isolated token counts
_cv_input_tokens: contextvars.ContextVar[int] = contextvars.ContextVar(
    "claude_in_tokens", default=0
)
_cv_output_tokens: contextvars.ContextVar[int] = contextvars.ContextVar(
    "claude_out_tokens", default=0
)
_cv_cache_read: contextvars.ContextVar[int] = contextvars.ContextVar(
    "claude_cache_read", default=0
)
_cv_cache_created: contextvars.ContextVar[int] = contextvars.ContextVar(
    "claude_cache_created", default=0
)



def _safe_json_dump(obj) -> str:
    """Best-effort JSON serialization for logging request/response payloads.
    Falls back to str() for anything not natively serialisable (SDK objects,
    content-block instances, etc.) so a log call can never crash the request.
    """
    import json as _j
    def _default(o):
        try:
            if hasattr(o, "model_dump"):
                return o.model_dump()
        except Exception:
            pass
        try:
            return str(o)
        except Exception:
            return f"<unserializable {type(o).__name__}>"
    try:
        return _j.dumps(obj, default=_default, ensure_ascii=False)
    except Exception as _je:
        return f"<json-dump-error {type(_je).__name__}: {_je}>"


def _dump_raw_response(raw) -> str:
    """Best-effort dump of the raw HTTP response body before SDK parsing.
    Works with the wrapper returned by with_raw_response.create().
    Never raises — logging must not break the request path.
    """
    for _attr in ("text", ):
        try:
            _t = getattr(raw, _attr, None)
            if isinstance(_t, str) and _t:
                return _t
        except Exception:
            pass
    try:
        _hr = getattr(raw, "http_response", None)
        if _hr is not None:
            _t = getattr(_hr, "text", None)
            if isinstance(_t, str) and _t:
                return _t
    except Exception:
        pass
    try:
        _c = getattr(raw, "content", None)
        if isinstance(_c, (bytes, bytearray)):
            return _c.decode("utf-8", errors="replace")
    except Exception:
        pass
    return _safe_json_dump(raw)


def _dump_raw_headers(raw) -> str:
    """Best-effort dump of raw HTTP response headers from Anthropic.
    Surfaces useful diagnostic headers (x-request-id, ratelimit, etc.)
    that only exist on the raw HTTP response.
    """
    try:
        _h = getattr(raw, "headers", None)
        if _h is None:
            _hr = getattr(raw, "http_response", None)
            if _hr is not None:
                _h = getattr(_hr, "headers", None)
        if _h is None:
            return "{}"
        return _safe_json_dump({str(k): str(v) for k, v in dict(_h).items()})
    except Exception:
        return "{}"


def _log_claude_request(request_id: str, label: str, kwargs: dict) -> None:
    """Log the outgoing Anthropic API request payload (system, messages, tools).
    Called just before every messages.create / messages.stream call. NOTE: this
    logs the payload BEFORE the egress transport adds cache_control — see the
    [CLAUDE CACHE EGRESS] line for the actual body sent on the wire.
    """
    try:
        logger.info(
            f"[CLAUDE REQUEST] {request_id} {label} → "
            f"{_safe_json_dump(kwargs)}"
        )
    except Exception:
        pass


def _log_claude_response(request_id: str, label: str, raw) -> None:
    """Log the raw HTTP response headers and body from Anthropic before SDK
    parsing.  Works with the wrapper returned by with_raw_response.create().
    Called immediately after every non-streaming create() call.
    """
    try:
        logger.info(
            f"[CLAUDE RAW RESPONSE] {request_id} {label} ← "
            f"headers={_dump_raw_headers(raw)} "
            f"body={_dump_raw_response(raw)}"
        )
    except Exception:
        pass


def _log_claude_cache(request_id: str, label: str, cache_read: int, cache_creation: int, input_tokens: int) -> None:
    """Log the cache token breakdown returned by Anthropic in usage fields."""
    try:
        logger.info(
            f"[CLAUDE CACHE] {request_id} {label} "
            f"cache_read={cache_read} "
            f"cache_creation={cache_creation} "
            f"input_tokens={input_tokens}"
        )
    except Exception:
        pass


# Anthropic prompt-cache billing ratios (stable policy; dollar amount derived
# from MODEL_COST_PER_1M so it stays accurate when pricing changes in the registry).
_CACHE_READ_RATIO  = 0.10   # 10%  of full input price
_CACHE_WRITE_RATIO = 1.25   # 125% of full input price


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    cache_created: int,
    prompt_total: int,
    context: str = "",          # e.g. "stream", "non-stream", "tool-use"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for Anthropic/Claude calls.

    Derives the per-token cost from MODEL_COST_PER_1M (the single source of truth)
    so savings estimates stay accurate when model pricing changes in the registry.
    Local/in-house models have (0.0, 0.0) rates → savings_est_usd is always 0.
    Always emitted (even when all values are 0) so the absence of caching is explicit.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M
        input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
        # `prompt_total` (Anthropic's `input_tokens`) excludes cache_read/cache_created —
        # they are disjoint token buckets, not overlapping subsets. The true total
        # prompt size processed by the model is the sum of all three, so the hit
        # rate must be computed against that sum, not against `prompt_total` alone
        # (which is often near-zero on a cache hit, blowing the ratio past 100%).
        _full_prompt = prompt_total + cache_read + cache_created
        hit_rate = (cache_read / _full_prompt * 100) if _full_prompt > 0 else 0.0
        # Savings: cache_read tokens billed at 10% instead of 100% of input rate
        savings_usd = cache_read * input_rate_per_1m * (1.0 - _CACHE_READ_RATIO) / 1_000_000
        # Write surcharge: cache_created tokens billed at 125% instead of 100%
        write_surcharge_usd = cache_created * input_rate_per_1m * (_CACHE_WRITE_RATIO - 1.0) / 1_000_000
        ctx_tag = f" context={context}" if context else ""
        logger.info(
            f"[CACHE EFFECTIVENESS] provider=claude request_id={request_id} model={model}{ctx_tag} "
            f"cache_read={cache_read} cache_created={cache_created} prompt_total={prompt_total} "
            f"full_prompt={_full_prompt} hit_rate={hit_rate:.1f}% savings_tokens={cache_read} "
            f"savings_est_usd={savings_usd:.6f} write_surcharge_est_usd={write_surcharge_usd:.6f}"
        )
    except Exception:
        pass


def _build_content(prompt=None, content_blocks=None):
    """
    Build the Anthropic messages content for a single user turn.

    content_blocks path: each block dict has {"text": str}; empty blocks are dropped.
    prompt path (legacy flat-string): returned as-is.
    """
    if content_blocks is not None:
        result = []
        for blk in content_blocks:
            text = blk.get("text") or ""
            if not text:
                continue  # Anthropic rejects empty text blocks with 400
            result.append({"type": "text", "text": text})
        return result
    return prompt or ""


def _normalize_anthropic_parts(parts, request_id, msg_index):
    """Coerce a content-parts list into Anthropic's required shape.

    Anthropic rejects any part without a 'type' field with:
      messages.<i>.content.<j>.type: Field required
    Upstream callers may send OpenAI-style parts (image_url, text-without-type,
    or bare {"text": "..."}). Normalize the common cases and drop unrecoverable
    parts with a diagnostic log so the request still succeeds.
    """
    if not isinstance(parts, list):
        return parts

    normalized = []
    for j, p in enumerate(parts):
        if not isinstance(p, dict):
            logger.error(
                f"[CLAUDE HOP-4] {request_id} dropping message[{msg_index}].content[{j}]: "
                f"not a dict (type={type(p).__name__})"
            )
            continue
        if "type" in p:
            # OpenAI vision shape → Anthropic image block
            if p["type"] == "image_url" and isinstance(p.get("image_url"), dict):
                url = p["image_url"].get("url", "")
                if url.startswith("data:"):
                    try:
                        header, b64 = url.split(",", 1)
                        media_type = header.split(";")[0].removeprefix("data:") or "image/png"
                        normalized.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        })
                        continue
                    except ValueError:
                        pass
                normalized.append({"type": "image", "source": {"type": "url", "url": url}})
                continue
            normalized.append(p)
            continue
        # Missing 'type': infer from keys we recognize
        if isinstance(p.get("text"), str):
            normalized.append({"type": "text", "text": p["text"]})
            continue
        logger.error(
            f"[CLAUDE HOP-4] {request_id} dropping message[{msg_index}].content[{j}]: "
            f"missing 'type' field, keys={list(p.keys())}"
        )
    return normalized


def _oai_tool_calls_to_anthropic(tool_calls, request_id, msg_index):
    """Convert an OpenAI assistant `tool_calls` list into Anthropic tool_use blocks.

    OpenAI shape: [{"id": "...", "type": "function",
                    "function": {"name": "...", "arguments": "<json string>"}}]
    Anthropic shape: [{"type": "tool_use", "id": "...", "name": "...", "input": {...}}]

    `arguments` is a JSON-encoded string in OpenAI but a dict in Anthropic.
    Malformed JSON falls back to an empty dict so the request still succeeds.
    """
    import json as _json
    blocks = []
    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments", "")
        try:
            args = _json.loads(raw_args) if raw_args else {}
        except (ValueError, TypeError):
            logger.warning(
                f"[CLAUDE HOP-4] {request_id} message[{msg_index}] tool_call "
                f"id={tc.get('id')!r} arguments not valid JSON → using {{}}"
            )
            args = {}
        blocks.append({
            "type":  "tool_use",
            "id":    tc.get("id") or f"call_{msg_index}",
            "name":  fn.get("name") or "",
            "input": args,
        })
    return blocks


def _has_oai_format(messages):
    """Detect whether `messages` contains OpenAI-specific fields that require
    conversion to Anthropic format.

    Returns True when any message has `role: "tool"` (OpenAI tool results) or
    an assistant message with a top-level `tool_calls` key (OpenAI tool
    invocations). Returns False for Anthropic-native messages that already
    carry `tool_use` / `tool_result` content blocks inside the `content` list.
    """
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get("role") == "tool":
            return True
        if m.get("tool_calls"):
            return True
    return False


def _convert_oai_messages_to_anthropic(messages, request_id):
    """Convert an OpenAI-format messages list into Anthropic-native messages.

    Anthropic only accepts `user` and `assistant` roles. OpenAI's `tool` role
    (tool results) and `assistant` `tool_calls` (tool invocations) must be
    rewritten:

      • {"role": "tool", "content": "...", "tool_call_id": "X"}
            → {"role": "user",
               "content": [{"type": "tool_result", "tool_use_id": "X", "content": "..."}]}
      • {"role": "assistant", "tool_calls": [...], "content": "..."}
            → {"role": "assistant",
               "content": [{"type": "text", "text": "..."}?,
                           {"type": "tool_use", ...}, ...]}

    Consecutive tool results are merged into a single user turn (Anthropic
    requires one user message per round of tool_results). `system` messages are
    lifted out and returned separately so the caller can pass them as the
    top-level `system=` parameter.

    Returns (anthropic_messages, system_texts).
    """
    import json as _json
    out = []
    sys_texts = []
    # Buffer of pending tool_result blocks; flushed as one user turn when a
    # non-tool message arrives or at the end. Anthropic requires tool_results
    # to live inside a user message, and a single user turn may carry several.
    pending_tool_results = []

    def _flush_tool_results():
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            continue
        role = m.get("role") or "user"
        content = m.get("content")

        if role == "system":
            sys_texts.append(str(content) if content is not None else "")
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block. Accumulate
            # consecutive tool results; they are flushed as a single user turn
            # when the next non-tool message arrives (or at the end). Anthropic
            # requires tool_results to live inside a user message, and one user
            # turn may carry several tool_result blocks.
            tool_use_id = m.get("tool_call_id") or m.get("tool_use_id") or ""
            # Anthropic requires content to be a string or a content-blocks list.
            if isinstance(content, list):
                # Already structured (rare for OpenAI tool role) — pass through.
                result_content = content
            else:
                result_content = str(content) if content is not None else ""
            pending_tool_results.append({
                "type":        "tool_result",
                "tool_use_id": tool_use_id,
                "content":     result_content,
            })
            continue

        # user / assistant: any buffered tool_results must be flushed first so
        # they land in their own user turn before the next role appears.
        _flush_tool_results()

        if role == "assistant":
            tool_calls = m.get("tool_calls")
            if tool_calls:
                blocks = []
                if isinstance(content, str) and content:
                    blocks.append({"type": "text", "text": content})
                blocks.extend(_oai_tool_calls_to_anthropic(tool_calls, request_id, i))
                out.append({"role": "assistant", "content": blocks})
            else:
                # Plain assistant text. Content may be None when only tool_calls
                # were present but no text — coerce to empty string to satisfy
                # Anthropic's non-null content requirement.
                out.append({"role": "assistant",
                            "content": content if isinstance(content, str) else (content or "")})
            continue

        # role == "user" (or unknown → treat as user to avoid a 400)
        if isinstance(content, list):
            out.append({"role": "user",
                        "content": _normalize_anthropic_parts(content, request_id, i)})
        else:
            out.append({"role": "user",
                        "content": str(content) if content is not None else ""})

    _flush_tool_results()
    return out, sys_texts


# ============================================================
# CLAUDE GATEWAY CLASS
# ============================================================

class ClaudeGateway:
    from core.model_registry import BLOCKED_MODELS

    if CLAUDE_MODEL in BLOCKED_MODELS:
        raise Exception("Blocked Claude model attempted")

    def __init__(self, api_key: str = None):
        """Initialise the Claude gateway.

        Args:
            api_key: Plaintext Anthropic API key.  When provided (Option A
                     key-delivery path via ProxyKeyCache), this key is used
                     directly.  When ``None`` (local dev / fallback), the key
                     is read from the ``ANTHROPIC_API_KEY`` environment variable.
        """
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")

        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set")

        # the LLM proxy server has direct outbound internet access — no proxy needed.
        _t = float(os.getenv("LLM_TIMEOUT_SEC", "600"))

        # httpx event hook — logs outgoing request URL, headers (API key redacted)
        # and the full serialised JSON body for every call through self.client
        # (used by generate_with_tools).
        async def _log_outgoing_request(req):
            try:
                await req.aread()   # ensure body bytes are buffered before reading
                _hdrs = {
                    k: ("***REDACTED***" if k.lower() in ("x-api-key", "authorization") else v)
                    for k, v in req.headers.items()
                }
                _body = req.content.decode("utf-8", errors="replace") if req.content else ""
                logger.info(
                    f"[CLAUDE RAW REQUEST] → "
                    f"url={req.url} "
                    #f"headers={_safe_json_dump(_hdrs)} "
                    #f"body={_body}"
                )
            except Exception:
                pass

        # The cache-control marker is added by this client's transport at the moment
        # the request leaves for api.anthropic.com — see core/claude_cache_egress.py.
        # Do NOT add cache breakpoints to payloads built here or in main.py.
        from core.claude_cache_egress import build_cached_async_client
        self.client = AsyncAnthropic(
            api_key=api_key,
            timeout=None if _t <= 0 else _t,
            http_client=build_cached_async_client(
                event_hooks={"request": [_log_outgoing_request]},
            ),
        )
        # Store the resolved key so the per-call AsyncAnthropic client
        # inside generate() uses the same key rather than re-reading
        # os.environ (which may not have the key when ProxyKeyCache is used).
        self._api_key = api_key

        logger.info("Claude Gateway initialized")

    @property
    def _last_input_tokens(self):
        return _cv_input_tokens.get()

    @_last_input_tokens.setter
    def _last_input_tokens(self, v):
        _cv_input_tokens.set(v)

    @property
    def _last_output_tokens(self):
        return _cv_output_tokens.get()

    @_last_output_tokens.setter
    def _last_output_tokens(self, v):
        _cv_output_tokens.set(v)

    @property
    def _last_cache_read_tokens(self):
        return _cv_cache_read.get()

    @_last_cache_read_tokens.setter
    def _last_cache_read_tokens(self, v):
        _cv_cache_read.set(v)

    @property
    def _last_cache_creation_tokens(self):
        return _cv_cache_created.get()

    @_last_cache_creation_tokens.setter
    def _last_cache_creation_tokens(self, v):
        _cv_cache_created.set(v)


    # ========================================================
    # GENERATE WITH TOOLS METHOD
    # ========================================================

    async def generate_with_tools(
            self,
            system_prompt: str,
            user_message: str,
            context: str,
            tools: list,
            tool_executor,
            model: str = CLAUDE_MODEL,
            max_tokens: int = 32000,
            max_tool_rounds: int = 5,
    ) -> str:
        """Non-streaming async call with Claude tool-use API.

        Drives a multi-round loop:
          1. Claude receives message + tool schemas
          2. If Claude emits tool_use blocks → execute each via tool_executor
          3. Send tool_result messages back → Claude continues
          4. Repeat until Claude emits end_turn (no tool calls) or max rounds

        Returns the final text answer as a plain string.
        tool_executor: callable(tool_name: str, inputs: dict) -> str
        """

        if _ANTHROPIC_DISABLED:
            logger.info("generate_with_tools: Anthropic API disabled (DISABLE_ANTHROPIC_API=true)")
            return "[Anthropic API disabled by configuration]"

        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())
        tool_names = [t["name"] for t in tools]
        logger.info(f"{request_id} → CLAUDE TOOL-USE START tools={tool_names}")

        # Reset and accumulate real token counts across multi-turn loop
        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0

        user_content = f"{context}\n\n## User Request\n{user_message}" if context else user_message

        # No cache breakpoints here — the egress transport adds one top-level
        # cache_control to the outbound request (see core/claude_cache_egress.py).
        system_blocks = [{"type": "text", "text": system_prompt}]

        first_msg_content = [{"type": "text", "text": user_content}]

        messages = [{"role": "user", "content": first_msg_content}]

        for round_num in range(max_tool_rounds + 1):
            try:
                # On the last round, strip tools so Claude is forced to answer
                call_tools = tools if round_num < max_tool_rounds else []

                _tool_kw = dict(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_blocks,
                    messages=messages,
                    tools=call_tools if call_tools else None,
                )
                #_log_claude_request(request_id, f"tool-use round={round_num}", _tool_kw)
                _raw = await self.client.messages.with_raw_response.create(**_tool_kw)
                #_log_claude_response(request_id, f"tool-use round={round_num}", _raw)
                response = _raw.parse()
                # Accumulate real token counts across all tool-use rounds
                if hasattr(response, "usage") and response.usage:
                    self._last_input_tokens  += response.usage.input_tokens or 0
                    self._last_output_tokens += response.usage.output_tokens or 0
                    _cr = getattr(response.usage, "cache_read_input_tokens",     0) or 0
                    _cc = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                    self._last_cache_read_tokens     += _cr
                    self._last_cache_creation_tokens += _cc
                    if _cr or _cc:
                        _log_claude_cache(request_id, f"tool-use round={round_num}", _cr, _cc, response.usage.input_tokens or 0)
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=model,
                        cache_read=_cr,
                        cache_created=_cc,
                        prompt_total=response.usage.input_tokens or 0,
                        context=f"tool-use round={round_num}",
                    )
            except Exception as e:
                logger.error(f"{request_id} → tool-use round {round_num} failed: {e}")
                return f"[ERROR generating response: {e}]"

            # Collect tool_use blocks from response
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

            # If no tool calls or Claude is done, extract text and return
            if not tool_use_blocks or response.stop_reason == "end_turn":
                text = " ".join(
                    b.text for b in response.content if hasattr(b, "text") and b.text
                ).strip()
                logger.info(f"{request_id} → CLAUDE TOOL-USE DONE after {round_num} round(s)")
                return text

            # Append assistant turn
            messages.append({"role": "assistant", "content": response.content})

            # Execute each tool call and collect results
            # tool_executor calls are sync (local tool execution) — no await needed
            logger.info(f"{request_id} → round {round_num}: {len(tool_use_blocks)} tool call(s)")
            tool_results = []
            for block in tool_use_blocks:
                try:
                    logger.info(f"{request_id} → executing {block.name}")
                    result_text = tool_executor(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     str(result_text),
                    })
                    logger.info(f"{request_id} → {block.name} → OK: {str(result_text)[:120]}")
                except Exception as e:
                    logger.error(f"{request_id} → {block.name} failed: {e}")
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     f"Error executing {block.name}: {e}",
                        "is_error":    True,
                    })

            # Append tool results as next user turn
            messages.append({"role": "user", "content": tool_results})

        return "[ERROR: max tool-use rounds exceeded]"


    # ========================================================
    # GENERATE METHOD
    # ========================================================

    async def generate(
            self,
            prompt=None,                              # str | list[dict] (OpenAI multi-turn format) | None
            model: str = CLAUDE_MODEL,
            temperature: float = 0,
            max_tokens: int = 32000,
            stream: bool = True,
            content_blocks: Optional[list] = None,
    ) -> AsyncGenerator[str, None]:
        """prompt accepts:
          - str           → single user turn, wrapped via _build_content for caching
          - list[dict]    → OpenAI-format multi-turn history; each turn is forwarded
                            as a separate Anthropic message (roles map 1:1; the last
                            user turn becomes cache-eligible if large enough)
          - None          → must supply content_blocks instead (existing path)
        """

        if _ANTHROPIC_DISABLED:
            logger.info("generate: Anthropic API disabled (DISABLE_ANTHROPIC_API=true)")
            yield "[Anthropic API disabled by configuration]"
            return

        from core.logger import get_request_id as _get_req_id
        request_id = _get_req_id() or str(uuid.uuid4())

        # Detect multi-turn shape early so logging and downstream branching agree.
        _is_messages_list = isinstance(prompt, list)

        logger.info(f"{request_id} → CLAUDE GATEWAY START")
        # Decisive pre-call log so every failure can be attributed to the exact call shape.
        if content_blocks is not None:
            _call_mode = "content_blocks"
        elif _is_messages_list:
            _call_mode = "messages"
        else:
            _call_mode = "prompt"
        if content_blocks is not None:
            _call_chars = sum(len(b.get("text", "")) for b in content_blocks)
        elif _is_messages_list:
            _call_chars = sum(len(str(m.get("content", ""))) for m in prompt)
        else:
            _call_chars = len(prompt or "")
        logger.info(
            f"[CLAUDE HOP-4] {request_id} START "
            f"model={model} stream={stream} mode={_call_mode} "
            + (f"blocks={len(content_blocks)} chars={_call_chars}"
               if content_blocks is not None
               else (f"turns={len(prompt)} chars={_call_chars}"
                     if _is_messages_list
                     else f"prompt_chars={_call_chars}"))
        )

        # Reset real token counts for this call
        self._last_input_tokens          = 0
        self._last_output_tokens         = 0
        self._last_cache_read_tokens     = 0
        self._last_cache_creation_tokens = 0

        # CLI mode = developer tool with full freedom; platform = full PCI/DSS gates.
        # For multi-turn we detect on the joined text so a CLI session that spans
        # multiple turns still bypasses compliance correctly.
        if _is_messages_list:
            _prompt_text = "\n".join(
                str(m.get("content") or "") for m in prompt
            )
        else:
            _prompt_text = prompt or ""
        _is_cli = isinstance(_prompt_text, str) and "---\n\nTask:" in _prompt_text

        # NOTE: The proxy drains generate() via asyncio.run() once PER REQUEST, which
        # opens then closes a fresh event loop each time. An AsyncAnthropic/httpx client
        # binds its connection pool to the loop that first used it, so reusing the cached
        # self.client across loops raises "RuntimeError: Event loop is closed". Per the
        # httpx maintainers, create the async client INSIDE the coroutine driven by
        # asyncio.run() so it is loop-agnostic (mirrors App01's _make_async_client
        # pattern). self.client is left intact for the always-async tools-stream/chat
        # endpoints (those run on the long-lived uvicorn loop, never via asyncio.run()).
        _call_timeout = float(os.getenv("LLM_TIMEOUT_SEC", "300"))

        # httpx event hook — fires just before each outgoing HTTP request so we
        # can log the exact URL, headers, and serialised body Anthropic receives
        # (API key redacted). This fires BEFORE the egress transport adds
        # cache_control — see the [CLAUDE CACHE EGRESS] line for the final body.
        async def _log_request(req):
            try:
                await req.aread()   # ensure body bytes are buffered before reading
                _hdrs = {
                    k: ("***REDACTED***" if k.lower() in ("x-api-key", "authorization") else v)
                    for k, v in req.headers.items()
                }
                _body = req.content.decode("utf-8", errors="replace") if req.content else ""
                logger.info(
                    f"[CLAUDE RAW REQUEST] {request_id} → "
                    f"url={req.url} "
                    #f"headers={_safe_json_dump(_hdrs)} "
                    #f"body={_body}"
                )
            except Exception:
                pass

        # Per-call client, same cache-control egress transport as self.client so
        # this path gets an identical marker without building one itself.
        # Use self._api_key (set in __init__) so ProxyKeyCache-sourced keys
        # are used here too rather than falling back to os.getenv().
        from core.claude_cache_egress import build_cached_async_client
        # Attach x-api-key as a default header on the per-call httpx client so the
        # auth header is present on the wire even if the Anthropic SDK skips its
        # header-merge step for user-supplied http_client instances (observed in
        # anthropic>=0.40 when the client is built inside a fresh asyncio.run()
        # loop). api_key= is still passed so the SDK's internal state matches.
        _client = AsyncAnthropic(
            api_key=self._api_key,
            timeout=None if _call_timeout <= 0 else _call_timeout,
            http_client=build_cached_async_client(
                event_hooks={"request": [_log_request]},
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": "2023-06-01",
                },
            ),
        )

        try:

            # Input compliance is handled at the orchestrator layer
            # (before reaching the gateway) so the user question is
            # already PCI-clean.  Running it again on the full assembled
            # CODE_PROMPT (which includes jPOS source code chunks) causes
            # false positives on developer emails, test IFSC codes, etc.
            # Output compliance (per token below) still guards generated text.

            from core.retry import retry_llm_async
            from core.circuit_breaker import get_breaker

            breaker = get_breaker("claude")

            # Build Anthropic messages — three input shapes:
            #   1. content_blocks → structured multi-block single user turn
            #   2. messages list  → forward each turn as a separate Anthropic message
            #   3. flat prompt    → single user turn via _build_content (legacy)
            #
            # No cache breakpoints are added here. The egress transport adds one
            # top-level cache_control as the request leaves for api.anthropic.com
            # (see core/claude_cache_egress.py).
            _system_blocks_for_generate = None  # populated from system-role messages below

            if _is_messages_list:
                # Only convert when the messages actually contain OpenAI-specific
                # fields (`role: "tool"` or `assistant.tool_calls`). The claude-
                # tools-stream path sends messages already in Anthropic-native
                # format (with tool_use / tool_result content blocks). Running
                # the converter on those double-nests tool_result blocks and
                # silently drops tool_use blocks → HTTP 400 from Anthropic.
                if _has_oai_format(prompt):
                    _msgs, _sys_texts = _convert_oai_messages_to_anthropic(prompt, request_id)
                else:
                    # Already in Anthropic format — normalise non-string content
                    # blocks (same logic as the pre-conversion inline loop).
                    _msgs, _sys_texts = [], []
                    for i, m in enumerate(prompt):
                        role = m.get("role") or "user"
                        raw = m.get("content")
                        if role == "system":
                            _sys_texts.append(str(raw) if raw is not None else "")
                            continue
                        if not isinstance(raw, str):
                            _msgs.append({"role": role,
                                          "content": _normalize_anthropic_parts(raw, request_id, i)})
                        else:
                            _msgs.append({"role": role, "content": raw})
                messages_payload = _msgs

                if _sys_texts:
                    _system_blocks_for_generate = [
                        {"type": "text", "text": t} for t in _sys_texts
                    ]

                _content_blocks_built = len(messages_payload)
            else:
                _content = _build_content(prompt=prompt, content_blocks=content_blocks)
                if isinstance(_content, str) and _content:
                    _content = [{"type": "text", "text": _content}]
                messages_payload = [{"role": "user", "content": _content}]
                _content_blocks_built = len(_content) if isinstance(_content, list) else 1
            logger.info(
                f"[CLAUDE HOP-4] {request_id} payload built → "
                f"{len(messages_payload)} message(s), "
                f"{_content_blocks_built} content block(s)"
            )

            if stream:
                if _is_messages_list:
                    logger.info(
                        f"{request_id} → CLAUDE PROMPT (messages) "
                    )
                elif prompt:
                    logger.info(f"{request_id} → CLAUDE PROMPT (text)")
                elif content_blocks:
                    logger.info(f"{request_id} → CLAUDE PROMPT (content_blocks)")

                _stream_kw = dict(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages_payload,
                )
                if _system_blocks_for_generate:
                    _stream_kw["system"] = _system_blocks_for_generate
                if not _no_temperature_model(model):
                    _stream_kw["temperature"] = temperature

                logger.info(
                    f"[CLAUDE HOP-4] {request_id} → Anthropic API stream open "
                    f"model={model} max_tokens={max_tokens}"
                )
                logger.info(
                    f"[CLAUDE REQUEST] {request_id} stream → "
                    f"{_safe_json_dump(_stream_kw)}"
                )
                async with _client.messages.stream(**_stream_kw) as stream_ctx:

                    async for event in stream_ctx:

                        # Capture real input + cache token counts from message_start
                        if event.type == "message_start":
                            try:
                                _u = event.message.usage
                                self._last_input_tokens          = _u.input_tokens or 0
                                self._last_cache_read_tokens     = getattr(_u, "cache_read_input_tokens",     0) or 0
                                self._last_cache_creation_tokens = getattr(_u, "cache_creation_input_tokens", 0) or 0
                                logger.info(f"[CLAUDE RAW message_start] {event.model_dump_json()}")
                                logger.info(
                                    f"[CLAUDE CACHE] {request_id} stream "
                                    f"cache_read={self._last_cache_read_tokens} "
                                    f"cache_creation={self._last_cache_creation_tokens} "
                                    f"input_tokens={self._last_input_tokens}"
                                )
                                _log_cache_effectiveness(
                                    request_id=request_id,
                                    model=model,
                                    cache_read=self._last_cache_read_tokens,
                                    cache_created=self._last_cache_creation_tokens,
                                    prompt_total=self._last_input_tokens,
                                    context="stream",
                                )
                            except Exception as _le:
                                logger.info(f"[CLAUDE RAW message_start] parse error: {_le}")
                            continue

                        # Capture real output token count from message_delta
                        if event.type == "message_delta":
                            try:
                                if hasattr(event, "usage") and event.usage:
                                    self._last_output_tokens = event.usage.output_tokens or 0
                                    logger.info(f"[CLAUDE RAW message_delta] {event.model_dump_json()}")
                            except Exception:
                                pass
                            continue

                        if event.type != "content_block_delta":
                            continue

                        if event.delta.type != "text_delta":
                            continue

                        token = event.delta.text

                        if not token:
                            continue

                        # Stream each token as it arrives so the client renders
                        # the answer incrementally (matches OpenAI/Gemini gateways).
                        # Token/usage counts are captured from message_start /
                        # message_delta events above, independent of these yields.
                        yield token

            else:

                async def _call():
                    _kw = dict(
                        model=model,
                        max_tokens=max_tokens,
                        messages=messages_payload,
                        stream=False,
                    )
                    if _system_blocks_for_generate:
                        _kw["system"] = _system_blocks_for_generate
                    if not _no_temperature_model(model):
                        _kw["temperature"] = temperature
                    logger.info(
                        f"[CLAUDE REQUEST] {request_id} non-stream → "
                        f"{_safe_json_dump(_kw)}"
                    )
                    # with_raw_response lets us log exact HTTP response headers
                    # and body before SDK parsing.
                    _raw = await _client.messages.with_raw_response.create(**_kw)
                    logger.info(
                        f"[CLAUDE RAW RESPONSE] {request_id} non-stream ← "
                        f"headers={_dump_raw_headers(_raw)} "
                        #f"body={_dump_raw_response(_raw)}"
                    )
                    return _raw.parse()

                response = await breaker.async_call(retry_llm_async, _call)

                output = response.content[0].text

                # Capture real token counts from non-streaming response
                if hasattr(response, "usage") and response.usage:
                    self._last_input_tokens          = response.usage.input_tokens or 0
                    self._last_output_tokens         = response.usage.output_tokens or 0
                    self._last_cache_read_tokens     = getattr(response.usage, "cache_read_input_tokens",     0) or 0
                    self._last_cache_creation_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
                    logger.info(
                        f"[CLAUDE CACHE] {request_id} non-stream "
                        f"cache_read={self._last_cache_read_tokens} "
                        f"cache_creation={self._last_cache_creation_tokens} "
                        f"input_tokens={self._last_input_tokens}"
                    )
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=model,
                        cache_read=self._last_cache_read_tokens,
                        cache_created=self._last_cache_creation_tokens,
                        prompt_total=self._last_input_tokens,
                        context="non-stream",
                    )

                yield output


        except Exception as e:
            import traceback as _tb
            logger.error(
                f"[CLAUDE HOP-4] {request_id} Claude generation failed "
                f"[{type(e).__name__}]: {e}\n{_tb.format_exc()}"
            )
            raise
        finally:
            # Close the per-call client on the SAME loop that created it (this
            # coroutine's loop, driven by asyncio.run()). Never reuse it across loops.
            try:
                await _client.close()
            except Exception:
                pass

    # Alias so /llm/generate's unified _stream() can call gw.async_generate()
    # on all three providers (Claude / OpenAI / Gemini) without branching.
    # generate() is already async — no wrapper needed.
    async_generate = generate


# ============================================================
# SINGLETON INSTANCE (IMPORTANT)
# ============================================================
#
# LAZY: this module-level singleton must NOT be constructed at import
# time. On web02 the API keys are delivered at runtime by ProxyKeyCache
# (see services/llm_proxy/core/proxy_key_client.py) and are deliberately
# NOT present in os.environ. Eagerly calling ClaudeGateway() here would
# raise ValueError("ANTHROPIC_API_KEY not set") during
# `from gateway_claude import ClaudeGateway` in _lifespan(), which would
# take down every Claude endpoint on the proxy.
#
# The real gateway used to serve traffic is `_claude_gw` in main.py,
# built in _lifespan() with the ProxyKeyCache-sourced key. This module
# attribute exists only for backward compatibility with any caller that
# imports `claude_gateway` directly.

_claude_gateway_singleton = None


def __getattr__(name):
    """PEP 562 module-level __getattr__ — builds the singleton on first
    attribute access rather than at import time."""
    if name == "claude_gateway":
        global _claude_gateway_singleton
        if _claude_gateway_singleton is None:
            _claude_gateway_singleton = ClaudeGateway()
        return _claude_gateway_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
