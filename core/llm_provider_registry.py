# SPDX-License-Identifier: MIT
# ============================================================
# LLM PROVIDER REGISTRY  (DB-backed read path)
#
# Single source of truth for "which LLM providers/models are configured and
# enabled", read from the llm_providers/llm_models tables (db/models.py) that
# the admin "LLM Providers" screen (routers/llm_provider_admin_router.py)
# manages. Callers that used to read hardcoded model-id literals or
# core.model_registry env vars should resolve through here instead, so a
# model an admin adds becomes usable everywhere without a code change.
#
# Cached in the shared Redis-backed KV (core.kv) — the same mechanism
# routers/endpoint_mgmt_router.py already uses to invalidate cross-worker
# caches on admin writes (see _invalidate_slug_cache there). This keeps every
# gateway worker consistent immediately after an admin edit, not just after
# a TTL expires.
# ============================================================

from __future__ import annotations

import json
from typing import Optional

from core.logger import logger

_KV_DB = 0
_CACHE_KEY = "llm_registry:enabled_models"
_CACHE_TTL_SECONDS = 300   # safety net only — invalidate_cache() clears this immediately on writes


def credential_name_for_slug(slug: str) -> str:
    """Deterministic credential_vault name for a provider slug.

    Every LLMProvider's API key lives in credential_vault under this name —
    avoids needing an id-based lookup helper in store/credential_vault.py,
    which is keyed by name everywhere else. Shared by
    routers/llm_provider_admin_router.py (writes) and db/migrate.py's
    backfill (writes) so all three call sites agree on the same name.
    """
    return f"llm_provider_{slug}"


def _load_from_db() -> list[dict]:
    from db.database import SessionLocal
    from db.models import LLMProvider, LLMModel

    db = SessionLocal()
    try:
        rows = (
            db.query(LLMModel, LLMProvider)
            .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
            .filter(LLMModel.enabled.is_(True), LLMProvider.enabled.is_(True))
            .order_by(LLMModel.sort_order, LLMModel.created_at)
            .all()
        )
        out = []
        for model, provider in rows:
            out.append({
                "id": model.id,
                "model_id": model.model_id,
                "display_name": model.display_name,
                "capabilities": model.capabilities or {},
                "is_default": model.is_default,
                "sort_order": model.sort_order,
                "provider_id": provider.id,
                "provider_slug": provider.slug,
                "provider_name": provider.name,
                "family": provider.family,
                "base_url": provider.base_url,
            })
        return out
    finally:
        db.close()


def _read_cache() -> Optional[list[dict]]:
    try:
        from core.kv import get_kv
        kv = get_kv(_KV_DB)
        raw = kv.get(_CACHE_KEY)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[llm_provider_registry] cache read failed, falling back to DB: {exc}")
    return None


def _write_cache(models: list[dict]) -> None:
    try:
        from core.kv import get_kv
        kv = get_kv(_KV_DB)
        kv.set(_CACHE_KEY, json.dumps(models), ex=_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning(f"[llm_provider_registry] cache write failed: {exc}")


def invalidate_cache() -> None:
    """Call at the end of every admin write (create/update/delete/toggle) on
    llm_providers or llm_models so every gateway worker sees the change on its
    next request rather than waiting out the TTL."""
    try:
        from core.kv import get_kv
        kv = get_kv(_KV_DB)
        kv.delete(_CACHE_KEY)
    except Exception as exc:
        logger.warning(f"[llm_provider_registry] cache invalidation failed: {exc}")


def get_enabled_models(channel: Optional[str] = None) -> list[dict]:
    """All enabled models across all enabled providers.

    `channel` (e.g. "web", "cli", "ide-vscode", "api") filters against each
    model's `capabilities.channels` list; a model with no `channels` entry is
    visible on every channel (the common case — most models aren't
    channel-restricted).
    """
    models = _read_cache()
    if models is None:
        models = _load_from_db()
        _write_cache(models)

    if channel:
        models = [
            m for m in models
            if not m["capabilities"].get("channels")
            or channel in m["capabilities"]["channels"]
        ]
    return models


def get_model(model_id: str) -> Optional[dict]:
    """Look up an enabled model by its exact API model_id.

    model_id is unique per-provider, not globally, so if two enabled
    providers expose the same model_id string this returns whichever sorts
    first (lowest sort_order, then earliest created_at) — the same
    "first enabled provider wins" convention used by resolve_credential's
    family lookup.
    """
    for m in get_enabled_models():
        if m["model_id"] == model_id:
            return m
    return None


def get_provider(provider_id: str) -> Optional[dict]:
    for m in get_enabled_models():
        if m["provider_id"] == provider_id:
            return {
                "id": m["provider_id"],
                "slug": m["provider_slug"],
                "name": m["provider_name"],
                "family": m["family"],
                "base_url": m["base_url"],
            }
    return None


def resolve_credential(provider: dict) -> Optional[str]:
    """Decrypted API key for a provider dict (as returned by get_provider()),
    or None for providers that need no key (e.g. ollama)."""
    from store.credential_vault import get_credential_value
    return get_credential_value(credential_name_for_slug(provider["slug"]))


def resolve_credential_for_family(family: str) -> Optional[str]:
    """Decrypted API key for the first enabled provider of `family` (lowest
    sort_order, then earliest created_at — same "first enabled provider wins"
    convention as get_model()).

    Used ONLY as a fallback inside gateway_claude.py/gateway_openai.py/
    gateway_gemini.py's client construction — `os.getenv(VAR) or
    resolve_credential_for_family(family)` — so an admin who configures a
    provider purely through the "LLM Providers" screen (no .env key at all)
    still works, without changing behavior for the common case where the env
    var is already set. Does not support multiple distinct providers of the
    same family with different keys — see the LLM provider config design
    doc's Phase 9 notes."""
    for m in get_enabled_models():
        if m["family"] == family:
            return resolve_credential({"slug": m["provider_slug"]})
    return None


# Maps LLMProvider.family (anthropic|openai|gemini|openai_compatible|ollama)
# onto the provider bucket strings the CLI/Agent-Studio/IDE pickers have
# always used ("google" not "gemini", "inhouse" not "ollama") — several
# call sites branch on this exact string (e.g. `provider == "inhouse"` for
# context-window/pricing defaults), so the bucket names can't just be `family`.
_FAMILY_TO_CLI_PROVIDER = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openai_compatible": "openai",
    "gemini": "google",
    "ollama": "inhouse",
}


def get_cli_style_models(channel: Optional[str] = None) -> list[dict]:
    """Flat catalogue shape shared by the CLI (``routers/messages_compat_router.py
    ::list_models_compat``), Agent Studio (``AgentStudio/backend/app/api/generation.py
    ::_cli_reference_models``), and IDE plugins (``routers/ide_router.py
    ::ide_get_models``).

    Unlike ``GET /v1/all-models`` (gateway.py), which was switched to read
    this registry exclusively, those three pickers still built their cloud
    (Claude/OpenAI/Gemini) catalogue from core.model_registry env-var
    constants — so a provider an admin configured purely through the
    "LLM Providers" screen (DB row, no matching env var) showed up on the web
    Chat picker but not on the CLI, Agent Studio, or any IDE plugin.

    Returns ``[{"id", "hint", "provider", "label", "tag", "billing_tier"}, ...]``.
    In-house (ollama) ids are prefixed ``local:`` to match the prefix
    convention those three callers already use for locally-discovered models,
    so registry-sourced and live-discovered local entries dedupe cleanly.
    """
    out = []
    for m in get_enabled_models(channel=channel):
        caps = m["capabilities"] or {}
        bucket = _FAMILY_TO_CLI_PROVIDER.get(m["family"], m["family"])
        default_tier = "free" if m["family"] == "ollama" else "paid"
        billing_tier = caps.get("billing_tier", default_tier)
        raw_id = m["model_id"]
        mid = f"local:{raw_id}" if bucket == "inhouse" and not raw_id.startswith("local:") else raw_id
        out.append({
            "id": mid,
            "hint": mid,
            "provider": bucket,
            "label": f"{m['display_name']} ({raw_id})",
            "tag": f"{m['provider_name']} · {billing_tier}",
            "billing_tier": billing_tier,
        })
    return out


def get_default_model_id(channel: Optional[str] = None, prefer_free: bool = False) -> Optional[str]:
    """The model id a caller should use when nothing else picks one explicitly.

    Several callers across the platform (ABStudio's ``factory_model()`` /
    ``factory_agent_model()``, Buddy's forced-model lock, etc.) used to answer
    this with a hardcoded literal or an env var that assumed the admin's
    "LLM Providers" configuration would always include one specific model
    (e.g. ``claude-sonnet-4-6``). Since that screen is now the actual source of
    truth for what's configured, a hardcoded default can silently point at a
    model the admin never enabled — this resolves against what's ACTUALLY
    enabled instead:

      1. the model an admin explicitly marked ``is_default`` in the admin
         screen, if any (an explicit admin choice always wins);
      2. else, when ``prefer_free`` is set (cheap/frequent orchestration
         calls, not a specific user-facing generation), the first enabled
         free-tier/self-hosted model;
      3. else the first enabled model by sort_order.

    Returns ``None`` when no providers/models are configured/enabled at all —
    callers must handle that (there is nothing to route to).
    """
    models = get_enabled_models(channel=channel)
    if not models:
        return None
    for m in models:
        if m.get("is_default"):
            return m["model_id"]
    if prefer_free:
        for m in models:
            caps = m["capabilities"] or {}
            if m["family"] == "ollama" or caps.get("billing_tier") == "free":
                return m["model_id"]
    return models[0]["model_id"]


def ensure_default_model(db) -> bool:
    """Guarantee at least one enabled model is flagged ``is_default`` so the
    platform always has a real default the moment any model becomes usable —
    instead of relying on ``get_default_model_id()``'s silent sort_order
    fallback (which works for routing but leaves the admin screen showing no
    default at all until someone clicks the star manually).

    No-op if a default already exists among currently enabled models of
    enabled providers. Otherwise picks the same candidate
    ``get_default_model_id()`` would have fallen back to (first enabled
    model by sort_order, then created_at) and flags it explicitly, so the
    choice becomes visible/durable instead of implicit.

    Callers (provider create + auto-sync, sync-models, add-model, ollama
    pull, enabling a model/provider, install.sh's bootstrap script) must
    still commit() — this only mutates the session, on the assumption it's
    running inside an existing transaction alongside other writes. Returns
    True if it changed anything.
    """
    from db.models import LLMModel, LLMProvider

    # db/database.py's SessionLocal is autoflush=False, so a pending
    # add()/delete() from earlier in the same request wouldn't otherwise be
    # visible to the queries below — flush explicitly rather than relying on
    # every call site to remember to (several didn't, which silently defeated
    # this function on the exact "first provider ever created" case it
    # exists for).
    db.flush()

    has_default = (
        db.query(LLMModel)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .filter(
            LLMModel.is_default.is_(True),
            LLMModel.enabled.is_(True),
            LLMProvider.enabled.is_(True),
        )
        .first()
    )
    if has_default:
        return False

    candidate = (
        db.query(LLMModel)
        .join(LLMProvider, LLMModel.provider_id == LLMProvider.id)
        .filter(LLMModel.enabled.is_(True), LLMProvider.enabled.is_(True))
        .order_by(LLMModel.sort_order, LLMModel.created_at)
        .first()
    )
    if not candidate:
        return False

    # A row can be left with a stale is_default=True after the provider or
    # model it belongs to gets disabled/deleted (excluded from the
    # eligibility filters above, but the flag itself isn't cleared there) —
    # clear any such leftovers so re-enabling it later can't resurrect a
    # second concurrent "default", matching update_model's single-default
    # invariant.
    db.query(LLMModel).filter(
        LLMModel.id != candidate.id, LLMModel.is_default.is_(True)
    ).update({"is_default": False}, synchronize_session=False)
    candidate.is_default = True
    return True


def get_client_for(model_id: str) -> Optional[dict]:
    """Resolve everything a gateway_*.py call module needs to build an SDK/HTTP
    client for `model_id`: {family, base_url, api_key}. Returns None if the
    model is not enabled/known — callers should fall back to their existing
    env-var-based client construction in that case (see Phase 9 in the LLM
    provider config design doc)."""
    model = get_model(model_id)
    if not model:
        return None
    provider = {
        "slug": model["provider_slug"],
        "family": model["family"],
        "base_url": model["base_url"],
    }
    return {
        "family": provider["family"],
        "base_url": provider["base_url"],
        "api_key": resolve_credential(provider),
    }
