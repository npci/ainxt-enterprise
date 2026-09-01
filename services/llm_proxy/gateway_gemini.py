# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt / RBI PRODUCTION GEMINI GATEWAY
# ============================================================

import os
import threading
import uuid
from typing import Generator

from google import genai

from core.logger import logger, get_request_id as _get_request_id
# NOTE: Compliance (PCI/PII detection + redaction) lives EXCLUSIVELY in the
# backend gateway layer (Tier 1). This proxy forwards already-validated,
# already-redacted text verbatim. Do NOT reintroduce a compliance engine here.
from core.model_registry import GEMINI_VISION_MODEL, GEMINI_IMAGE_MODEL, VEO_MODEL

# Thread-local storage so concurrent requests don't overwrite each other's token counts
_tl = threading.local()


# Default model for generate() — aliases to GEMINI_IMAGE_MODEL via the registry.
MODEL = GEMINI_VISION_MODEL

# Gemini context caching: cached tokens are billed at 25% of the model's normal input rate.
# Context cache storage is billed separately per hour; not modelled here.
_GEMINI_CACHE_READ_RATIO = 0.25   # 25% of full input price


def _log_cache_effectiveness(
    *,
    request_id: str,
    model: str,
    cache_read: int,
    prompt_total: int,
    context: str = "",          # e.g. "stream", "imagen", "vision"
) -> None:
    """Emit a structured [CACHE EFFECTIVENESS] log line for Gemini calls.

    Derives the per-token cost from MODEL_COST_PER_1M (the single source of truth)
    so savings estimates stay accurate when model pricing changes in the registry.
    Gemini context caching (cached_content_token_count in usage_metadata) is
    explicit -- callers must create a CachedContent object. Always emitted so
    zero-cache calls are visible and cache effectiveness can be tracked over time.
    """
    try:
        from core.model_registry import MODEL_COST_PER_1M
        input_rate_per_1m, _ = MODEL_COST_PER_1M.get(model, (0.0, 0.0))
        hit_rate = (cache_read / prompt_total * 100) if prompt_total > 0 else 0.0
        # Savings: cache_read tokens billed at 25% instead of 100% of input rate
        savings_usd = cache_read * input_rate_per_1m * (1.0 - _GEMINI_CACHE_READ_RATIO) / 1_000_000
        ctx_tag = f" context={context}" if context else ""
        logger.info(
            f"[CACHE EFFECTIVENESS] provider=gemini request_id={request_id} model={model}{ctx_tag} "
            f"cache_read={cache_read} prompt_total={prompt_total} "
            f"hit_rate={hit_rate:.1f}% savings_tokens={cache_read} savings_est_usd={savings_usd:.6f}"
        )
    except Exception:
        pass



class GeminiGateway:

    def __init__(self, api_key: str = None):
        """Initialise the Gemini gateway.

        Args:
            api_key: Plaintext Gemini API key.  When provided (Option A
                     key-delivery path via ProxyKeyCache), this key is used
                     directly.  When ``None`` (local dev / fallback), the key
                     is read from the ``GEMINI_API_KEY`` environment variable.
        """
        api_key = api_key or os.getenv("GEMINI_API_KEY")

        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        # Retained on the instance (not just os.environ) because on web02 the
        # key is delivered at runtime via ProxyKeyCache and is DELIBERATELY
        # never written to os.environ (see core/proxy_key_client.py). Any
        # fallback path that needs the key directly — e.g. _fetch_video_uri's
        # manual URI fetch for Veo — must read it from here, not os.getenv().
        self._api_key = api_key

        # SSL_CERT_FILE is a standard POSIX env var consumed by Python's
        # ssl.create_default_context(), which httpx uses for its default SSL context.
        # All three SDKs (google-genai, openai, anthropic) go through httpx, so
        # setting SSL_CERT_FILE on the LLM proxy server is sufficient — no custom client needed.
        self.client = genai.Client(api_key=api_key)

        _ssl_cert = os.getenv("SSL_CERT_FILE") or os.getenv("REQUESTS_CA_BUNDLE")
        logger.info(
            "Gemini Gateway initialized"
            + (f" (SSL_CERT_FILE={_ssl_cert})" if _ssl_cert else "")
        )


    @property
    def _last_input_tokens(self):
        return getattr(_tl, "gemini_in", 0)

    @_last_input_tokens.setter
    def _last_input_tokens(self, v):
        _tl.gemini_in = v

    @property
    def _last_output_tokens(self):
        return getattr(_tl, "gemini_out", 0)

    @_last_output_tokens.setter
    def _last_output_tokens(self, v):
        _tl.gemini_out = v

    @property
    def _last_imagen_model(self):
        # Thread-local so concurrent image requests don't clobber each other's
        # reported model id (matches the token-count properties above).
        return getattr(_tl, "gemini_imagen_model", GEMINI_IMAGE_MODEL)

    @_last_imagen_model.setter
    def _last_imagen_model(self, v):
        _tl.gemini_imagen_model = v


    def generate(
        self,
        prompt,                              # str | list[dict] (OpenAI multi-turn format)
        model: str | None = None,
    ) -> Generator[str, None, None]:
        """Stream tokens from Gemini.

        Compliance (PCI/PII detection + redaction) is handled by the backend
        gateway layer (Tier 1) BEFORE the request reaches this proxy. The text
        received here is already validated and redacted, so it is forwarded to
        the provider verbatim — this proxy performs NO compliance itself.

        prompt accepts a string (single turn) or an OpenAI-format messages
        array (list of {"role": "user"|"assistant", "content": str}). Lists
        become google-genai Content list with "assistant" → "model".

        model: optional explicit Gemini model ID. When None, falls back to
        the module-level MODEL constant (GEMINI_VISION_MODEL)."""

        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        # Reset real token counts for this call
        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        try:
            from core.retry import retry_llm
            from core.circuit_breaker import get_breaker
            from google.genai import types as _gtypes

            # Build the contents arg in the shape google-genai expects:
            #   - str  → plain string (single turn)
            #   - list → list[Content(role=..., parts=[Part(text=...)])]
            # OpenAI "assistant" role maps to Gemini "model".
            # Content is forwarded unchanged (already redacted upstream).
            if isinstance(prompt, list):
                contents_arg = []
                for m in prompt:
                    _role = "model" if m.get("role") == "assistant" else "user"
                    contents_arg.append(
                        _gtypes.Content(role=_role, parts=[_gtypes.Part(text=m.get("content") or "")])
                    )
            else:
                contents_arg = prompt

            _effective_model = model or MODEL

            def _call():
                # Streaming generation: returns an iterator of partial chunks
                # so we can forward text to the client token-by-token instead of
                # buffering the whole response.
                return self.client.models.generate_content_stream(
                    model=_effective_model,
                    contents=contents_arg,
                )

            breaker  = get_breaker("gemini")
            response = breaker.call(retry_llm, _call)

            # Stream each chunk's text as it arrives. usage_metadata is captured
            # from whichever chunk carries it (Gemini attaches it to the final
            # chunk). Input is already redacted upstream, so no output redaction.
            _final_um = None
            for chunk in response:
                if getattr(chunk, "usage_metadata", None):
                    _final_um = chunk.usage_metadata
                token = getattr(chunk, "text", None)
                if not token:
                    continue
                yield token

            # Capture real token counts from the final usage_metadata + dump raw JSON
            try:
                if _final_um is not None:
                    _um = _final_um
                    self._last_input_tokens  = getattr(_um, "prompt_token_count",    0) or 0
                    self._last_output_tokens = getattr(_um, "candidates_token_count", 0) or 0
                    # Print raw usage_metadata so full token breakdown is visible in logs
                    try:
                        import json as _json
                        _um_dict = {
                            "prompt_token_count":         getattr(_um, "prompt_token_count",         0),
                            "candidates_token_count":     getattr(_um, "candidates_token_count",     0),
                            "total_token_count":          getattr(_um, "total_token_count",          0),
                            "cached_content_token_count": getattr(_um, "cached_content_token_count", 0),
                            "thoughts_token_count":       getattr(_um, "thoughts_token_count",       0),
                        }
                        logger.info(f"[GEMINI RAW usage_metadata] {_json.dumps(_um_dict)}")
                    except Exception:
                        logger.info(f"[GEMINI RAW usage_metadata] prompt={self._last_input_tokens} candidates={self._last_output_tokens}")
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_effective_model,
                        cache_read=getattr(_um, "cached_content_token_count", 0) or 0,
                        prompt_total=self._last_input_tokens,
                        context="stream",
                    )
            except Exception:
                pass


        except Exception as e:

            logger.exception(
                f"{request_id} → Gemini failed → {repr(e)[:1500]}"
            )

            yield "\nError generating response"

    async def async_generate(
        self,
        prompt,                              # str | list[dict] (OpenAI multi-turn format)
        model: str | None = None,
    ):
        """Async streaming generator — yields str tokens.

        Mirrors generate() but uses genai.Client.aio.models so the entire call
        runs on the uvicorn event loop without blocking a thread-pool worker.
        Called from the /llm/generate endpoint's _stream() coroutine so all
        three providers (Claude, OpenAI, Gemini) share the same native-async
        token delivery path.

        c.aio.models.generate_content_stream(...) is a coroutine that returns
        an AsyncIterator[GenerateContentResponse] — use `async for chunk in
        await c.aio.models.generate_content_stream(...)`.

        model: optional explicit Gemini model ID. When None, falls back to
        the module-level MODEL constant (GEMINI_VISION_MODEL).
        """
        _upstream = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        self._last_input_tokens  = 0
        self._last_output_tokens = 0

        _effective_model = model or MODEL

        try:
            from core.circuit_breaker import get_breaker
            from google.genai import types as _gtypes

            logger.info(f"[LLM DISPATCH async] provider=gemini model={_effective_model} request_id={request_id}")

            # Build contents in the shape google-genai expects (same as sync path).
            if isinstance(prompt, list):
                contents_arg = []
                for m in prompt:
                    _role = "model" if m.get("role") == "assistant" else "user"
                    contents_arg.append(
                        _gtypes.Content(role=_role, parts=[_gtypes.Part(text=m.get("content") or "")])
                    )
            else:
                contents_arg = prompt

            breaker = get_breaker("gemini")
            # c.aio.models.generate_content_stream is a coroutine → await it to
            # get the AsyncIterator, then iterate with async for.
            _final_um = None
            async_iter = await breaker.async_call(
                lambda: self.client.aio.models.generate_content_stream(
                    model=_effective_model,
                    contents=contents_arg,
                )
            )
            async for chunk in async_iter:
                if getattr(chunk, "usage_metadata", None):
                    _final_um = chunk.usage_metadata
                token = getattr(chunk, "text", None)
                if not token:
                    continue
                yield token

            # Capture token counts from the final usage_metadata chunk.
            try:
                if _final_um is not None:
                    self._last_input_tokens  = getattr(_final_um, "prompt_token_count",    0) or 0
                    self._last_output_tokens = getattr(_final_um, "candidates_token_count", 0) or 0
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_effective_model,
                        cache_read=getattr(_final_um, "cached_content_token_count", 0) or 0,
                        prompt_total=self._last_input_tokens,
                        context="stream",
                    )
            except Exception:
                pass

        except Exception as e:
            logger.exception(
                f"{request_id} → Gemini async failed → {repr(e)[:1500]}"
            )
            yield "\nError generating response"

    def generate_imagen(self, prompt: str) -> bytes | None:
        """
        Generate an image via Gemini (text → image bytes).
        Uses gemini-3.1-flash-image with generate_content + response_modalities=["IMAGE"].
        Returns raw image bytes or None on failure.
        Compliance is enforced upstream in the backend gateway layer (Tier 1);
        the prompt received here is already validated/redacted.
        Called by /llm/generate-ppt-image proxy endpoint.
        """
        _upstream  = _get_request_id()
        request_id = _upstream if _upstream and _upstream != "-" else str(uuid.uuid4())

        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        full_prompt = (
            f"{prompt}. "
            "Cinematic professional photography, ultra high resolution, "
            "16:9 landscape, no text, no watermarks, photorealistic."
        )

        # Reset token/model metadata for this call so stale counts from a
        # previous call don't leak through (mirrors generate()).
        self._last_input_tokens  = 0
        self._last_output_tokens = 0
        self._last_imagen_model  = GEMINI_IMAGE_MODEL

        try:
            from google.genai import types as _gtypes

            # Image-generation model — sourced from the registry so the env
            # override (GEMINI_IMAGE_MODEL) is respected without code changes.
            _GEMINI_MULTIMODAL = GEMINI_IMAGE_MODEL

            def _call():
                return self.client.models.generate_content(
                    model=_GEMINI_MULTIMODAL,
                    contents=full_prompt,
                    config=_gtypes.GenerateContentConfig(
                        response_modalities=["IMAGE"],
                        image_config=_gtypes.ImageConfig(
                            aspect_ratio="16:9",
                        ),
                    ),
                )

            breaker  = get_breaker("gemini")
            response = breaker.call(retry_llm, _call)

            # Capture real token counts from usage_metadata + dump raw JSON.
            # Mirrors generate() so image generation surfaces the same
            # (model, in_token, out_token, cost, latency) metadata chat/doc do.
            self._last_imagen_model = _GEMINI_MULTIMODAL
            try:
                if getattr(response, "usage_metadata", None):
                    _um = response.usage_metadata
                    self._last_input_tokens  = getattr(_um, "prompt_token_count",     0) or 0
                    self._last_output_tokens = getattr(_um, "candidates_token_count", 0) or 0
                    try:
                        import json as _json
                        _um_dict = {
                            "prompt_token_count":         getattr(_um, "prompt_token_count",         0),
                            "candidates_token_count":     getattr(_um, "candidates_token_count",     0),
                            "total_token_count":          getattr(_um, "total_token_count",          0),
                            "cached_content_token_count": getattr(_um, "cached_content_token_count", 0),
                            "thoughts_token_count":       getattr(_um, "thoughts_token_count",       0),
                        }
                        logger.info(f"[GEMINI RAW usage_metadata] imagen model={_GEMINI_MULTIMODAL} {_json.dumps(_um_dict)}")
                    except Exception:
                        logger.info(f"[GEMINI RAW usage_metadata] imagen prompt={self._last_input_tokens} candidates={self._last_output_tokens}")
                    _log_cache_effectiveness(
                        request_id=request_id,
                        model=_GEMINI_MULTIMODAL,
                        cache_read=getattr(_um, "cached_content_token_count", 0) or 0,
                        prompt_total=self._last_input_tokens,
                        context="imagen",
                    )
            except Exception:
                pass

            # Extract inline image data from response candidates
            if not response.candidates:
                logger.warning("generate_imagen: no candidates in response")
                return None

            parts = (response.candidates[0].content.parts or []) if response.candidates[0].content else []
            for part in parts:
                if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                    logger.info(
                        f"generate_imagen: OK model={_GEMINI_MULTIMODAL} "
                        f"mime={getattr(part.inline_data, 'mime_type', 'unknown')} "
                        f"bytes={len(part.inline_data.data)}"
                    )
                    return part.inline_data.data

            logger.warning(f"generate_imagen: response has no inline image data (parts={len(parts)})")
        except Exception as exc:
            logger.error(f"llm_proxy: Gemini generate_imagen failed: {exc}")
        return None

    # ============================================================
    # VIDEO URI FETCH (used as a fallback when files.download misbehaves)
    # ============================================================
    def _fetch_video_uri(self, video_uri: str) -> bytes | None:
        """
        Download a Veo video from its signed URI, bypassing the SDK's
        `files.download` path.

        Why this exists: on some deployments (notably behind a TLS-inspecting
        corporate proxy on CPython 3.13), the stdlib `http.client` parser
        mis-reads chunked-transfer bodies and raises
        `BadStatusLine('B35\\r\\n')` / `illegal status line: bytearray(b'B35')`.
        `requests` sits on `http.client` too and fails identically. We prefer
        `httpx` here because it uses `h11` for HTTP/1.1 framing, which handles
        the chunked stream correctly on the same wire bytes.

        When both parsers fail we drop to a raw-socket capture so we can log
        exactly what the corporate proxy is putting on the wire — that's the
        only way to prove whether McAfee is re-framing/re-chunking the body.
        """
        # Append the API key for generativelanguage.googleapis.com URIs, which
        # the SDK would have signed via its own auth path.
        #
        # IMPORTANT: read from self._api_key, not os.getenv("GEMINI_API_KEY").
        # On web02 the key is delivered at runtime via ProxyKeyCache and is
        # deliberately NOT written to os.environ (see core/proxy_key_client.py),
        # so os.getenv() here silently returns "" and every fallback fetch below
        # goes out with no key — producing the persistent
        # "video bytes unavailable after download" / 502 failure.
        fetch_url = video_uri
        if "generativelanguage.googleapis.com" in fetch_url and "key=" not in fetch_url:
            api_key = getattr(self, "_api_key", "") or os.getenv("GEMINI_API_KEY", "")
            sep = "&" if "?" in fetch_url else "?"
            fetch_url = f"{fetch_url}{sep}key={api_key}"

        # Corporate-CA bundle: httpx picks up SSL_CERT_FILE automatically;
        # requests only honours REQUESTS_CA_BUNDLE/CURL_CA_BUNDLE. Resolve
        # once and pass explicitly so both libraries agree.
        ca_bundle = (
                os.getenv("REQUESTS_CA_BUNDLE")
                or os.getenv("CURL_CA_BUNDLE")
                or os.getenv("SSL_CERT_FILE")
        )

        # 1) Preferred path: httpx (h11 parser — immune to the chunked bug).
        try:
            import httpx
            verify_arg = ca_bundle if ca_bundle else True
            with httpx.Client(verify=verify_arg, timeout=120.0, follow_redirects=True) as hc:
                resp = hc.get(fetch_url)
                if resp.status_code == 200 and resp.content:
                    logger.info(
                        f"generate_veo_video: recovered via httpx URI fetch bytes={len(resp.content)}"
                    )
                    return resp.content
                logger.error(
                    f"generate_veo_video: httpx URI fetch returned {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as httpx_exc:
            logger.warning(f"generate_veo_video: httpx URI fetch failed: {httpx_exc}")

        # 2) requests fallback (uses http.client — same parser as the SDK).
        try:
            import requests
            r = requests.get(
                fetch_url,
                timeout=120,
                stream=True,
                verify=ca_bundle if ca_bundle else True,
            )
            if r.status_code == 200:
                content = r.content
                logger.info(
                    f"generate_veo_video: recovered via requests URI fetch bytes={len(content)}"
                )
                return content
            logger.error(
                f"generate_veo_video: requests URI fetch returned {r.status_code}: {r.text[:200]}"
            )
        except Exception as req_exc:
            logger.error(f"generate_veo_video: requests URI fetch failed: {req_exc}")

        # 3) Diagnostic: raw-socket capture so the network team has evidence.
        #    Also attempts to parse framing itself and, if successful, returns
        #    the decoded body so we still get the video through.
        try:
            body = self._raw_socket_capture(fetch_url, ca_bundle)
            if body:
                logger.info(
                    f"generate_veo_video: recovered via raw-socket capture bytes={len(body)}"
                )
                return body
        except Exception as raw_exc:
            logger.error(f"generate_veo_video: raw-socket capture failed: {raw_exc}")

        return None

    def _raw_socket_capture(
            self,
            fetch_url: str,
            ca_bundle: str | None,
            _max_redirects: int = 5,
    ) -> bytes | None:
        """
        Open a plain TLS socket, send a minimal HTTP/1.1 GET, and log the raw
        response bytes.

        This started as pure diagnostics, but the raw capture also lets us
        drive HTTP by hand — which is exactly what we need because the SDK-
        supplied Veo URI (`/v1beta/files/<id>:download`) responds with a
        `302 Found` pointing at `/download/v1beta/files/<id>:download`. Every
        stdlib-based HTTP parser (http.client, requests, and — via keep-alive
        reuse — even httpx in this environment) trips over the follow-up
        connection state. Driving the socket manually with `Connection: close`
        and following the redirect ourselves is what actually works.

        Follows up to `_max_redirects` hops and only returns a body for HTTP
        200 responses. Non-2xx responses are logged and produce None so we
        never write a JSON error page into a `.mp4` file.
        """
        current_url = fetch_url
        for hop in range(_max_redirects + 1):
            status, headers_text, body_blob = self._raw_http_get_once(current_url, ca_bundle, hop)
            if status is None:
                return None

            header_lower = headers_text.lower()

            # Redirect handling
            if status in (301, 302, 303, 307, 308):
                location = None
                for line in headers_text.split("\r\n"):
                    if line.lower().startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                        break
                if not location:
                    logger.error(
                        f"raw-capture: hop={hop} status={status} with no Location header — giving up"
                    )
                    return None
                # Resolve relative locations against the current URL.
                if location.startswith("/"):
                    from urllib.parse import urlsplit as _split
                    p = _split(current_url)
                    location = f"{p.scheme}://{p.netloc}{location}"
                logger.info(f"raw-capture: hop={hop} {status} redirect → {location[:200]}")
                current_url = location
                continue

            if status != 200:
                # Emit the body so the operator can see the actual error.
                try:
                    body_preview = body_blob[:512].decode("utf-8", errors="replace")
                except Exception:
                    body_preview = repr(body_blob[:256])
                logger.error(
                    f"raw-capture: hop={hop} non-OK status={status}; body preview:\n{body_preview}"
                )
                return None

            # 200 OK — decode body using advertised framing.
            try:
                if "transfer-encoding: chunked" in header_lower:
                    return self._decode_chunked(body_blob)
                for line in headers_text.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        n = int(line.split(":", 1)[1].strip())
                        return body_blob[:n] if n > 0 else None
                # No framing advertised → treat everything as body (Connection: close).
                return body_blob or None
            except Exception as decode_exc:
                logger.error(f"raw-capture: manual body decode failed: {decode_exc}")
                return None

        logger.error(f"raw-capture: exceeded max redirects ({_max_redirects})")
        return None

    def _raw_http_get_once(
            self,
            fetch_url: str,
            ca_bundle: str | None,
            hop: int,
    ) -> tuple[int | None, str, bytes]:
        """
        One HTTP GET over a fresh TLS socket. Returns (status, header_text, body_bytes).

        Logs the response headers and a hexdump of the first 512 bytes of the
        body at ERROR level so failing responses leave an audit trail. On the
        happy path (200 with a big MP4) we don't want to flood the logs, so we
        only hexdump when the body is small (<= 1 KiB) or when the status is
        non-200.
        """
        import socket
        import ssl
        from urllib.parse import urlsplit

        parts = urlsplit(fetch_url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = parts.path + ("?" + parts.query if parts.query else "")

        if ca_bundle and os.path.isfile(ca_bundle):
            ctx = ssl.create_default_context(cafile=ca_bundle)
        else:
            ctx = ssl.create_default_context()

        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: ainxt-llm-proxy/raw-capture\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        ).encode("ascii")

        raw = bytearray()
        try:
            with socket.create_connection((host, port), timeout=120) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    tls.sendall(req)
                    while True:
                        try:
                            chunk = tls.recv(65536)
                        except socket.timeout:
                            logger.error(f"raw-capture: hop={hop} socket timed out waiting for EOF")
                            break
                        if not chunk:
                            break
                        raw.extend(chunk)
        except Exception as sock_exc:
            logger.error(f"raw-capture: hop={hop} socket failure: {sock_exc}")
            return None, "", b""

        raw_bytes = bytes(raw)
        header_sep = raw_bytes.find(b"\r\n\r\n")
        if header_sep < 0:
            logger.error(
                f"raw-capture: hop={hop} no header terminator in {len(raw_bytes)} bytes; "
                f"first 256 bytes (repr)={raw_bytes[:256]!r}"
            )
            return None, "", b""

        header_blob = raw_bytes[:header_sep]
        body_blob = raw_bytes[header_sep + 4:]

        try:
            header_text = header_blob.decode("iso-8859-1")
        except Exception:
            header_text = repr(header_blob)

        # Parse status line: e.g. "HTTP/1.1 302 Found".
        first_line = header_text.split("\r\n", 1)[0]
        status: int | None = None
        try:
            status = int(first_line.split(" ", 2)[1])
        except Exception:
            logger.error(f"raw-capture: hop={hop} bad status line: {first_line!r}")
            return None, header_text, body_blob

        # Only dump full headers when something's off, to avoid spamming logs
        # on the happy path.
        if status != 200:
            logger.error(
                f"raw-capture: hop={hop} response headers\n"
                "----- BEGIN HEADERS -----\n"
                f"{header_text}\n"
                "----- END HEADERS -----"
            )
            head = body_blob[:512]
            hex_pairs = " ".join(f"{b:02x}" for b in head)
            printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)
            logger.error(
                f"raw-capture: hop={hop} body head ({len(head)}/{len(body_blob)} bytes)\n"
                f"HEX  : {hex_pairs}\n"
                f"ASCII: {printable}"
            )
        else:
            logger.info(
                f"raw-capture: hop={hop} 200 OK, body_bytes={len(body_blob)}"
            )

        return status, header_text, body_blob

    @staticmethod
    def _decode_chunked(buf: bytes) -> bytes | None:
        """
        Decode an HTTP/1.1 chunked-transfer body by hand. Tolerant of the
        exact quirks that trip http.client (extra blank lines between
        chunks, chunk-ext syntax, missing final `0\\r\\n\\r\\n`).
        """
        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            # Skip stray CRLFs / blank lines that some proxies inject.
            while i < n and buf[i:i+2] == b"\r\n":
                i += 2
            eol = buf.find(b"\r\n", i)
            if eol < 0:
                logger.error(f"decode_chunked: no CRLF after chunk-size at offset {i}")
                return bytes(out) or None
            size_line = buf[i:eol].split(b";", 1)[0].strip()
            try:
                size = int(size_line, 16)
            except ValueError:
                logger.error(
                    f"decode_chunked: bad chunk-size {size_line!r} at offset {i}; "
                    f"aborting — returning {len(out)} bytes decoded so far"
                )
                return bytes(out) or None
            i = eol + 2
            if size == 0:
                break  # last-chunk
            if i + size > n:
                logger.error(
                    f"decode_chunked: chunk of {size} bytes at offset {i} exceeds "
                    f"buffer len {n} — proxy truncated the stream"
                )
                out.extend(buf[i:])
                return bytes(out) or None
            out.extend(buf[i:i + size])
            i += size
            # Chunks are followed by CRLF; skip it if present.
            if buf[i:i+2] == b"\r\n":
                i += 2
        return bytes(out) or None

    # ============================================================
    # TEXT → VIDEO  (Veo 3.1 — long-running operation, returns MP4)
    # ============================================================
    def generate_veo_video(
            self,
            prompt: str,
            aspect_ratio: str = "16:9",
            duration_secs: int = 8,
            poll_interval_secs: int = 5,
            max_wait_secs: int = 300,
    ) -> tuple[bytes | None, str | None]:
        """
        Generate a short video via Google Veo 3.1 (preview).

        Returns (mp4_bytes, None) on success or (None, error_str) on failure.

        Veo is a Long-Running Operation:
          1. Submit `models.generate_videos(...)` → returns an Operation handle.
          2. Poll `operations.get(op)` until `op.done`.
          3. Download bytes from the returned File handle.

        Compliance is enforced upstream in the backend gateway layer (Tier 1);
        the prompt received here is already validated/redacted. Total wall-clock
        capped by `max_wait_secs` to bound LRO polling on errant operations.
        """
        import time
        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        safe_prompt = prompt

        try:
            from google.genai import types as gtypes

            def _start():
                return self.client.models.generate_videos(
                    model=VEO_MODEL,
                    prompt=safe_prompt,
                    config=gtypes.GenerateVideosConfig(
                        aspect_ratio=aspect_ratio,
                        duration_seconds=duration_secs,
                    ),
                )

            breaker = get_breaker("gemini")
            operation = breaker.call(retry_llm, _start)

            t0 = time.time()
            while not getattr(operation, "done", False):
                if (time.time() - t0) > max_wait_secs:
                    err = f"timeout after {max_wait_secs}s"
                    logger.error(f"generate_veo_video: {err}")
                    return None, err
                time.sleep(poll_interval_secs)
                try:
                    operation = self.client.operations.get(operation)
                except Exception as poll_exc:
                    err = f"poll failed: {poll_exc}"
                    logger.error(f"generate_veo_video poll error: {poll_exc}")
                    return None, err

            response = getattr(operation, "response", None)
            gen_videos = getattr(response, "generated_videos", None) if response else None
            if not gen_videos:
                err = "no generated_videos in response"
                logger.warning(f"generate_veo_video: {err}")
                return None, err

            video_handle = gen_videos[0].video
            # Newer google-genai revisions return bytes from files.download();
            # older ones mutate the handle in-place. Capture both.
            downloaded = None
            try:
                downloaded = self.client.files.download(file=video_handle)
            except Exception as dl_exc:
                # Known failure: some google-genai builds route this through
                # http.client and mis-parse chunked-transfer bodies, raising
                # "illegal status line: bytearray(b'...')". Fall back to a
                # direct HTTPS fetch below (urllib3 handles chunking correctly).
                logger.warning(f"generate_veo_video: files.download fallthrough: {dl_exc}")

            # Attribute name varies across google-genai revisions.
            video_bytes = (
                    (downloaded if isinstance(downloaded, (bytes, bytearray)) else None)
                    or getattr(video_handle, "video_bytes", None)
                    or getattr(video_handle, "data", None)
                    or getattr(video_handle, "bytes", None)
            )

            # URI fallback: fetch the signed video URL directly, side-stepping
            # the SDK's broken chunked-download path.
            if not video_bytes:
                video_uri = (
                        getattr(video_handle, "uri", None)
                        or getattr(video_handle, "file_uri", None)
                        or getattr(video_handle, "download_uri", None)
                )
                if video_uri:
                    video_bytes = self._fetch_video_uri(video_uri)

            if not video_bytes:
                err = "video bytes unavailable after download"
                logger.error(f"generate_veo_video: {err}")
                return None, err

            # Sanity floor: a real Veo MP4 is millions of bytes. Anything
            # smaller is almost certainly a JSON error page we mistakenly
            # accepted from a 3xx/4xx response. Reject rather than serve.
            MIN_VIDEO_BYTES = 4096
            if len(video_bytes) < MIN_VIDEO_BYTES:
                try:
                    preview = video_bytes[:256].decode("utf-8", errors="replace")
                except Exception:
                    preview = repr(video_bytes[:128])
                err = (
                    f"downloaded body too small to be a video "
                    f"(bytes={len(video_bytes)}, min={MIN_VIDEO_BYTES}); preview={preview!r}"
                )
                logger.error(f"generate_veo_video: {err}")
                return None, err

            logger.info(
                f"generate_veo_video: OK model={VEO_MODEL} "
                f"duration={duration_secs}s bytes={len(video_bytes)}"
            )
            return video_bytes, None

        except Exception as exc:
            logger.error(f"llm_proxy: Gemini generate_veo_video failed: {exc}")
            return None, str(exc)


# LAZY singleton — must NOT be constructed at import time.
# On web02 the API key is delivered at runtime by ProxyKeyCache and is
# deliberately NOT in os.environ, so eagerly calling GeminiGateway() here
# would raise RuntimeError("GEMINI_API_KEY not set") during
# `from gateway_gemini import GeminiGateway` in _lifespan() and take down
# every Gemini endpoint. The gateway that actually serves traffic is
# `_gemini_gw` in main.py, built with the ProxyKeyCache-sourced key.
_gemini_gateway_singleton = None


def _get_gemini_gateway() -> "GeminiGateway":
    """Build (once) and return the module-level fallback singleton."""
    global _gemini_gateway_singleton
    if _gemini_gateway_singleton is None:
        _gemini_gateway_singleton = GeminiGateway()
    return _gemini_gateway_singleton


def __getattr__(name):
    """PEP 562 — resolve `gemini_gateway` lazily on first access."""
    if name == "gemini_gateway":
        return _get_gemini_gateway()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def generate_with_image(
    prompt: str,
    image_b64: str,
    mime_type: str = "image/jpeg",
    system_prompt: str = "",
    _gateway: "GeminiGateway | None" = None,
    images_b64: "list[str] | None" = None,
    mime_types: "list[str] | None" = None,
) -> str:
    """
    Send a prompt + inline image(s) to Gemini vision.
    Returns the full response text (not streamed).

    _gateway: pass the proxy's already-initialised GeminiGateway instance so
    token counts are written back to the object that main.py reads them from.

    Multi-image (optional, backward-compatible): pass `images_b64` (list of
    base64 strings) + matching `mime_types` to analyse multiple images in a
    single call. When omitted, falls back to the single `image_b64`/
    `mime_type` pair (original behaviour, unchanged for every existing
    caller — including callers on an older client version that only ever
    sends the single-image fields).
    """
    import base64 as _b64

    # _gateway is the proxy's already-initialised instance (has the
    # ProxyKeyCache key). Only fall back to the lazy module singleton
    # when no gateway was passed in.
    gw = _gateway or _get_gemini_gateway()

    # Compliance is enforced upstream (Tier 1); prompt is already validated/redacted.
    safe_prompt = prompt

    # Reset so stale counts from a previous call don't leak through
    gw._last_input_tokens  = 0
    gw._last_output_tokens = 0

    # Normalise to a list — single-image callers keep working unchanged.
    _imgs  = images_b64 if images_b64 else ([image_b64] if image_b64 else [])
    _mimes = mime_types  if mime_types  else [mime_type] * len(_imgs)
    if len(_mimes) < len(_imgs):
        _mimes = _mimes + [mime_type] * (len(_imgs) - len(_mimes))

    try:
        from google.genai import types as _gtypes

        # Build a multi-part content with text + one or more inline images
        parts = []
        if system_prompt:
            parts.append(_gtypes.Part(text=system_prompt + "\n\n"))
        parts.append(_gtypes.Part(text=safe_prompt))
        for _img, _mt in zip(_imgs, _mimes):
            parts.append(
                _gtypes.Part(
                    inline_data=_gtypes.Blob(
                        mime_type=_mt,
                        data=_b64.b64decode(_img),
                    )
                )
            )

        from core.retry import retry_llm
        from core.circuit_breaker import get_breaker

        def _call():
            return gw.client.models.generate_content(
                model=MODEL,
                contents=_gtypes.Content(parts=parts, role="user"),
            )

        breaker  = get_breaker("gemini")
        response = breaker.call(retry_llm, _call)

        # Capture token counts and write them onto gw so main.py can read them
        try:
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                _um = response.usage_metadata
                _p  = getattr(_um, "prompt_token_count",      0) or 0
                _c  = getattr(_um, "candidates_token_count",   0) or 0
                _ci = getattr(_um, "cached_content_token_count", 0) or 0
                gw._last_input_tokens  = _p
                gw._last_output_tokens = _c
                logger.info(
                    f"[GEMINI USAGE] vision model={MODEL} "
                    f"prompt={_p} candidates={_c} cached_in={_ci} "
                    f"billed_in={_p - _ci} total={getattr(_um, 'total_token_count', 0) or 0}"
                )
                _log_cache_effectiveness(
                    request_id="gemini-vision",
                    model=MODEL,
                    cache_read=_ci,
                    prompt_total=_p,
                    context="vision",
                )
        except Exception:
            pass

        output = response.text or ""
        # Output redaction is handled by the backend gateway layer (Tier 1).
        return output

    except Exception as exc:
        logger.error(f"generate_with_image failed: {exc}")
        return "Error generating response from image"