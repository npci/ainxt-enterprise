# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PROXY KEY CLIENT — fetches cloud-LLM API keys from app02
#
# The LLM Proxy (web02) has no HSM connectivity and therefore
# cannot run core.ckms.load_at_boot(). Instead, app02 — which
# has already decrypted the keys via CKMS at boot — serves them
# through a dedicated internal endpoint:
#
#   GET {PROXY_KEY_FETCH_URL}/internal/ckms/proxy-keys
#
# Access is controlled at the nginx layer on web02. nginx only
# accepts calls to this path from app02 and localhost (web02
# itself). Optionally, setting PROXY_KEY_TOKEN on both hosts adds
# an application-layer shared secret on top of the network control.
#
# ProxyKeyCache.load() is called ONCE in _lifespan() before the
# gateway singletons are constructed. The fetched keys are stored
# in process memory only — never written to disk or os.environ.
#
# Fallback: if PROXY_KEY_FETCH_URL is not set (local dev) or the
# fetch fails, get() returns "" and the gateway constructors fall
# back to os.getenv() — reading from services/llm_proxy/.env as
# they do today. Nothing breaks.
#
# Environment variables (web02):
#   PROXY_KEY_FETCH_URL — base URL of app02 via nginx,
#                         e.g. http://localhost:8080
#   PROXY_KEY_TOKEN     — optional shared secret; must match the
#                         value set on app02 when that host sets it.
#
# See: PROXY_LLM_KEY_DELIVERY_REQUIREMENT.html §7.2, §9, §11
# ============================================================

from __future__ import annotations

import os
import threading
from typing import Dict

import httpx

from core.logger import logger

_FETCH_TIMEOUT_SEC = float(os.getenv("PROXY_KEY_FETCH_TIMEOUT_SEC", "5"))

# Alias → real env-var name mapping.
# The router returns keys under short neutral aliases so the wire payload
# does not reveal provider names. This map translates them back to the real
# env-var names that gateway constructors and spend endpoints expect.
_ALIAS_TO_ENV_VAR: dict = {
    "an": "ANTHROPIC_API_KEY",
    "op": "OPENAI_API_KEY",
    "ge": "GEMINI_API_KEY",
    "ga": "GOOGLE_API_KEY",
    "ll": "LITELLM_API_KEY",
    "oa": "OPENAI_ADMIN_API_KEY",
    "aa": "ANTHROPIC_ADMIN_API_KEY",
    "lo": "LOCAL_LLM_API_KEY",
    "no": "NOMIC_EMBED_API_KEY",
    "fo": "FIMI_OPENAI_API_KEY",
    "fa": "FIMI_ANTHROPIC_API_KEY",
}


class ProxyKeyCache:
    """
    Process-memory singleton holding the cloud-LLM API keys
    fetched from app02 at startup.

    Usage::

        # In _lifespan(), before gateway construction:
        ProxyKeyCache.load()

        # In gateway constructors:
        api_key = ProxyKeyCache.instance().get("ANTHROPIC_API_KEY") \\
                  or os.getenv("ANTHROPIC_API_KEY")
    """

    _instance: "ProxyKeyCache | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._keys: Dict[str, str] = {}
        self._loaded: bool = False

    # ── singleton ──────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "ProxyKeyCache":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        """Drop the singleton — tests only."""
        with cls._lock:
            cls._instance = None

    # ── load (called once at startup) ─────────────────────────

    @classmethod
    def load(cls) -> None:
        """Fetch keys from app02 and populate the singleton cache.

        Idempotent — a second call is a no-op if already loaded.
        Non-fatal on failure — gateway constructors fall back to
        os.getenv() so the proxy still starts in degraded mode.
        """
        inst = cls.instance()
        if inst._loaded:
            return
        inst._fetch()

    def _fetch(self) -> None:
        url = os.getenv("PROXY_KEY_FETCH_URL", "").rstrip("/")

        if not url:
            # Local dev — PROXY_KEY_FETCH_URL not set.
            # Gateway constructors will fall back to os.getenv().
            logger.info(
                "ProxyKeyCache: PROXY_KEY_FETCH_URL not set — "
                "gateway constructors will use local env vars (local dev mode)"
            )
            self._loaded = True
            return

        # Optional shared secret. Sent only when PROXY_KEY_TOKEN is configured
        # on this host; app02 enforces it only when the same var is set there,
        # so an unset deployment behaves exactly as before.
        _headers = {}
        _token = (os.getenv("PROXY_KEY_TOKEN") or "").strip()
        if _token:
            _headers["X-Proxy-Key-Token"] = _token

        try:
            resp = httpx.get(
                f"{url}/internal/ckms/proxy-keys",
                timeout=_FETCH_TIMEOUT_SEC,
                headers=_headers,
            )
            resp.raise_for_status()
            data = resp.json()
            aliased = data.get("keys", {})

            # Translate short aliases back to real env-var names.
            # The router sends aliased keys so the wire payload does not
            # reveal provider names. Store only non-null values — null
            # means provider not configured on app02 for this environment.
            self._keys = {
                _ALIAS_TO_ENV_VAR[alias]: value
                for alias, value in aliased.items()
                if alias in _ALIAS_TO_ENV_VAR and value
            }
            self._loaded = True

            # Log counts/booleans only — never the key values.
            logger.info(
                "ProxyKeyCache: keys loaded from app02 — "
                f"an={bool(self._keys.get('ANTHROPIC_API_KEY'))} "
                f"op={bool(self._keys.get('OPENAI_API_KEY'))} "
                f"ge={bool(self._keys.get('GEMINI_API_KEY'))} "
                f"ga={bool(self._keys.get('GOOGLE_API_KEY'))} "
                f"ll={bool(self._keys.get('LITELLM_API_KEY'))} "
                f"oa={bool(self._keys.get('OPENAI_ADMIN_API_KEY'))} "
                f"aa={bool(self._keys.get('ANTHROPIC_ADMIN_API_KEY'))} "
                f"lo={bool(self._keys.get('LOCAL_LLM_API_KEY'))} "
                f"no={bool(self._keys.get('NOMIC_EMBED_API_KEY'))} "
                f"fo={bool(self._keys.get('FIMI_OPENAI_API_KEY'))} "
                f"fa={bool(self._keys.get('FIMI_ANTHROPIC_API_KEY'))} "
                f"as_of={data.get('as_of', 'unknown')}"
            )

        except httpx.HTTPStatusError as exc:
            logger.error(
                f"ProxyKeyCache: fetch failed — HTTP {exc.response.status_code} "
                f"from {url} — gateway constructors will fall back to local env vars."
            )
            self._loaded = True   # mark loaded so we don't retry on every request

        except Exception as exc:
            logger.error(
                f"ProxyKeyCache: fetch failed — {type(exc).__name__}: {exc} — "
                f"gateway constructors will fall back to local env vars."
            )
            self._loaded = True   # same — degrade gracefully, don't crash

    # ── accessor ───────────────────────────────────────────────

    def get(self, key: str) -> str:
        """Return the cached plaintext key, or '' if not available.

        Callers should use the pattern::

            api_key = ProxyKeyCache.instance().get("ANTHROPIC_API_KEY") \\
                      or os.getenv("ANTHROPIC_API_KEY")

        so that local dev (PROXY_KEY_FETCH_URL unset) and any fetch
        failure both fall back to the local env var transparently.
        """
        return self._keys.get(key, "")
