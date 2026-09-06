# SPDX-License-Identifier: MIT
# ============================================================
# LLM Provider Configuration — Admin CRUD
#
# Admin-managed inventory of LLM provider accounts (Anthropic, OpenAI,
# Gemini, OpenRouter/other OpenAI-compatible services, Ollama/local) and the
# concrete models exposed under each. This is the write side of
# core/llm_provider_registry.py, which is the read side every other
# subsystem (chat model dropdown, model governance, managed endpoints)
# should eventually resolve models through instead of hardcoded literals.
#
# Every route requires Depends(require_admin) (same pattern as
# routers/endpoint_mgmt_router.py) — provider/model configuration is
# structurally admin-only.
#
# API keys are never stored on llm_providers directly — they live in
# credential_vault under the deterministic name "llm_provider_{slug}"
# (see core.llm_provider_registry._credential_name_for_slug) and are never
# echoed back in any response.
#
# Discovery / test-connection / model-pull endpoints live in this same
# router but are added in a later phase (see the LLM provider config design
# doc) — this file starts with CRUD only.
# ============================================================

import json
import os
import re
import threading
import uuid as _uuid_mod
from datetime import datetime
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from auth.dependencies import require_admin
from core.llm_provider_registry import ensure_default_model, invalidate_cache
from core.logger import logger
from db.database import SessionLocal
from db.models import LLMProvider, LLMModel

router = APIRouter(prefix="/llm-providers", tags=["llm-provider-config"])

_HTTP_TIMEOUT = 10.0
_ANTHROPIC_API_VERSION = "2023-06-01"   # long-stable header value; SDK gateway_claude.py negotiates its own
# Prefixes of OpenAI's own catalog that are never chat-completion models —
# filtered out of "openai" family sync so the chat picker never fills with
# ids it can't call. Deliberately narrow (data, not a chat-model allowlist):
# anything not matching one of these prefixes is assumed to be a chat model.
_OPENAI_NON_CHAT_PREFIXES = (
    "text-embedding-", "whisper-", "tts-", "dall-e", "omni-moderation",
    "text-moderation", "davinci-", "babbage-",
)
_OLLAMA_SUGGESTIONS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "ollama_model_suggestions.json",
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,99}$")

FAMILIES = [
    {"family": "anthropic", "label": "Anthropic (Claude)",
     "requires_api_key": True, "requires_base_url": False, "discovery_supported": True},
    {"family": "openai", "label": "OpenAI (GPT)",
     "requires_api_key": True, "requires_base_url": False, "discovery_supported": True},
    {"family": "gemini", "label": "Google Gemini",
     "requires_api_key": True, "requires_base_url": False, "discovery_supported": True},
    {"family": "openai_compatible", "label": "OpenRouter / Other (OpenAI-compatible)",
     "requires_api_key": True, "requires_base_url": True, "discovery_supported": True},
    {"family": "ollama", "label": "Ollama (local)",
     "requires_api_key": False, "requires_base_url": True, "discovery_supported": "tags-only"},
]
_VALID_FAMILIES = {f["family"] for f in FAMILIES}
_FAMILY_BY_NAME = {f["family"]: f for f in FAMILIES}


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------

def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------

def _credential_name(slug: str) -> str:
    from core.llm_provider_registry import credential_name_for_slug
    return credential_name_for_slug(slug)


def _store_credential(slug: str, api_key: str) -> str:
    """Create-or-rotate the credential for a provider slug. Returns credential id."""
    from store.credential_vault import create_credential, get_credential, rotate_credential

    name = _credential_name(slug)
    existing = get_credential(name)
    if existing:
        rotate_credential(name, api_key)
        return existing["id"]
    created = create_credential(
        name=name, value=api_key, category="api_key",
        description=f"LLM provider API key for '{slug}' (managed via LLM Providers admin screen)",
    )
    return created["id"]


def _delete_credential(slug: str) -> None:
    from store.credential_vault import delete_credential
    try:
        delete_credential(_credential_name(slug))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Model discovery — one function per family, all normalized to
# [{"model_id", "display_name", "capabilities"}]. Used by both
# /test-connection (discards the list) and /sync-models (upserts it).
# ---------------------------------------------------------------------------

_DEFAULT_BASE_URL = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}


def _provider_api_key(provider: LLMProvider) -> Optional[str]:
    if not provider.credential_id:
        return None
    from store.credential_vault import get_credential_value
    return get_credential_value(_credential_name(provider.slug))


def _discover_models(provider: LLMProvider) -> List[dict]:
    """Call the provider's list-models API. Raises HTTPException(502) on failure."""
    base_url = (provider.base_url or _DEFAULT_BASE_URL.get(provider.family, "")).rstrip("/")
    api_key = _provider_api_key(provider)

    try:
        if provider.family == "anthropic":
            resp = httpx.get(
                f"{base_url}/v1/models",
                headers={"x-api-key": api_key or "", "anthropic-version": _ANTHROPIC_API_VERSION},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            return [
                {"model_id": m["id"], "display_name": m.get("display_name", m["id"]), "capabilities": {}}
                for m in resp.json().get("data", []) if m.get("id")
            ]

        if provider.family in ("openai", "openai_compatible"):
            resp = httpx.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key or ''}"},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            out = []
            for m in resp.json().get("data", []):
                mid = m.get("id")
                if not mid:
                    continue
                # OpenAI's own catalog is a single, well-known API — filter out
                # non-chat model families so "sync" doesn't import embeddings/
                # audio/image models the chat picker can never use. NOT applied
                # to openai_compatible: OpenRouter and other aggregators mix
                # arbitrary vendors with no shared naming convention to filter
                # by, so those rely on the type-ahead "add by name" flow instead
                # of bulk sync (see LLMProviderConfig.jsx's TypeaheadAddModelForm).
                if provider.family == "openai" and mid.startswith(_OPENAI_NON_CHAT_PREFIXES):
                    continue
                caps = {}
                if m.get("context_length"):   # OpenRouter-style extra field
                    caps["context_window"] = m["context_length"]
                out.append({"model_id": mid, "display_name": mid, "capabilities": caps})
            return out

        if provider.family == "gemini":
            resp = httpx.get(f"{base_url}/models", params={"key": api_key or ""}, timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            out = []
            for m in resp.json().get("models", []):
                name = m.get("name", "")   # "models/gemini-3.5-flash"
                mid = name.rsplit("/", 1)[-1] if name else None
                if not mid:
                    continue
                caps = {}
                if m.get("inputTokenLimit"):
                    caps["context_window"] = m["inputTokenLimit"]
                if m.get("outputTokenLimit"):
                    caps["reserved_output"] = m["outputTokenLimit"]
                out.append({"model_id": mid, "display_name": m.get("displayName", mid), "capabilities": caps})
            return out

        if provider.family == "ollama":
            resp = httpx.get(f"{base_url}/api/tags", timeout=_HTTP_TIMEOUT)
            resp.raise_for_status()
            # billing_tier="free" — Ollama models are self-hosted; without this,
            # capabilities defaults to {} and every reader that checks
            # capabilities.billing_tier (e.g. GET /all-models' price tier badge)
            # falls back to "paid", showing a free local model as billable.
            return [
                {"model_id": m["name"], "display_name": m["name"], "capabilities": {"billing_tier": "free"}}
                for m in resp.json().get("models", []) if m.get("name")
            ]

        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown family '{provider.family}'.")

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"{provider.family} returned HTTP {exc.response.status_code} when listing models.",
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not reach {provider.family}: {exc}")


# ---------------------------------------------------------------------------
# Ollama pull jobs — tracked in the shared KV so status survives across
# gateway workers (same core.kv used by core/llm_provider_registry.py).
# ---------------------------------------------------------------------------

def _pull_job_key(job_id: str) -> str:
    return f"llm_pull_job:{job_id}"


def _set_pull_status(job_id: str, status_: str, **fields) -> None:
    try:
        from core.kv import get_kv
        kv = get_kv(0)
        kv.set(_pull_job_key(job_id), json.dumps({"status": status_, **fields}), ex=3600)
    except Exception as exc:
        logger.warning(f"[llm-provider-admin] pull-status write failed: {exc}")


def _run_ollama_pull(provider_id: str, base_url: str, model_id: str, job_id: str, created_by: Optional[str]) -> None:
    """Runs in a background thread — a model download can take minutes, far too
    long to hold a request open or block the event loop."""
    _set_pull_status(job_id, "pulling", percent=0)
    try:
        with httpx.stream(
            "POST", f"{base_url.rstrip('/')}/api/pull",
            json={"name": model_id, "stream": True}, timeout=None,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                total = evt.get("total") or 0
                completed = evt.get("completed") or 0
                percent = int(completed / total * 100) if total else None
                _set_pull_status(job_id, "pulling", detail=evt.get("status", ""), percent=percent)

        db = SessionLocal()
        try:
            if not db.query(LLMModel).filter_by(provider_id=provider_id, model_id=model_id).first():
                db.add(LLMModel(
                    provider_id=provider_id, model_id=model_id, display_name=model_id,
                    capabilities={"billing_tier": "free"}, enabled=True, source="manual", created_by=created_by,
                ))
                ensure_default_model(db)
                db.commit()
        finally:
            db.close()
        invalidate_cache()
        _set_pull_status(job_id, "done", percent=100)
    except Exception as exc:
        logger.warning(f"[llm-provider-admin] ollama pull failed for '{model_id}': {exc}")
        _set_pull_status(job_id, "error", detail=str(exc))


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class ProviderCreate(BaseModel):
    name: str
    slug: str
    family: str
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    extra_config: Optional[dict] = None

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG_RE.match(v):
            raise ValueError(
                "Slug must be 3-100 lowercase alphanumeric characters or hyphens "
                "and must start with a letter or digit."
            )
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty.")
        return v

    @field_validator("family")
    @classmethod
    def validate_family(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in _VALID_FAMILIES:
            raise ValueError(f"family must be one of: {sorted(_VALID_FAMILIES)}")
        return v


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    enabled: Optional[bool] = None
    extra_config: Optional[dict] = None
    api_key: Optional[str] = None   # rotate when present


class ModelCreate(BaseModel):
    model_id: str
    display_name: str
    capabilities: Optional[dict] = None

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model_id cannot be empty.")
        return v

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display_name cannot be empty.")
        return v


class ModelUpdate(BaseModel):
    display_name: Optional[str] = None
    capabilities: Optional[dict] = None
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None


class PullModelRequest(BaseModel):
    model_id: str

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model_id cannot be empty.")
        return v


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _provider_out(p: LLMProvider, db: Session) -> dict:
    model_count = db.query(LLMModel).filter_by(provider_id=p.id).count()
    return {
        "id": p.id,
        "name": p.name,
        "slug": p.slug,
        "family": p.family,
        "base_url": p.base_url,
        "org_id": p.org_id,
        "enabled": p.enabled,
        "extra_config": p.extra_config or {},
        "credential_configured": p.credential_id is not None,
        "model_count": model_count,
        "last_verified_at": p.last_verified_at.isoformat() if p.last_verified_at else None,
        "last_verify_status": p.last_verify_status,
        "last_verify_error": p.last_verify_error,
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def _model_out(m: LLMModel) -> dict:
    return {
        "id": m.id,
        "provider_id": m.provider_id,
        "model_id": m.model_id,
        "display_name": m.display_name,
        "capabilities": m.capabilities or {},
        "enabled": m.enabled,
        "is_default": m.is_default,
        "sort_order": m.sort_order,
        "source": m.source,
        "created_by": m.created_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Routes — static paths registered before "/{provider_id}" (FastAPI matches
# in registration order; see the identical comment in endpoint_mgmt_router.py)
# ---------------------------------------------------------------------------

@router.get("/", summary="List all configured LLM providers")
def list_providers(admin: dict = Depends(require_admin), db: Session = Depends(_get_db)):
    providers = db.query(LLMProvider).order_by(LLMProvider.created_at.desc()).all()
    return {"providers": [_provider_out(p, db) for p in providers], "count": len(providers)}


@router.post("/", status_code=status.HTTP_201_CREATED, summary="Add an LLM provider")
def create_provider(
    body: ProviderCreate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    family_meta = _FAMILY_BY_NAME[body.family]
    if family_meta["requires_base_url"] and not (body.base_url or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"base_url is required for family '{body.family}'.",
        )
    if family_meta["requires_api_key"] and not (body.api_key or "").strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"api_key is required for family '{body.family}'.",
        )

    if db.query(LLMProvider).filter_by(slug=body.slug).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A provider with slug '{body.slug}' already exists.",
        )

    credential_id = None
    if body.api_key:
        credential_id = _store_credential(body.slug, body.api_key)

    provider = LLMProvider(
        name=body.name,
        slug=body.slug,
        family=body.family,
        base_url=(body.base_url or "").strip() or None,
        credential_id=credential_id,
        extra_config=body.extra_config or {},
        enabled=True,
        created_by=admin.get("email") or admin.get("sub"),
    )
    db.add(provider)
    db.flush()

    # Auto-discover and load this provider's model catalog right away, and
    # make sure a platform default exists — an admin adding a provider
    # through this screen should end up with a fully connected, usable
    # provider in one step, not "create provider" -> "sync models" -> "set
    # default" as three separate manual actions. Same discovery call
    # /sync-models uses; best-effort only (e.g. a not-yet-valid key or a
    # transient network error shouldn't block creating the provider record
    # itself — the admin can still hit "Sync models" manually afterward).
    # Skipped for openai_compatible, same reason bulk sync is disabled for it
    # elsewhere (hundreds of unrelated models per catalog).
    sync_result = None
    if provider.family != "openai_compatible":
        try:
            discovered = _discover_models(provider)
            sync_result = _upsert_discovered_models(provider, discovered, db, provider.created_by)
        except Exception as exc:
            logger.warning(f"[llm-provider-admin] auto-sync on create failed for slug='{provider.slug}': {exc}")

    ensure_default_model(db)
    db.commit()
    db.refresh(provider)
    invalidate_cache()

    logger.info(
        f"[llm-provider-admin] created provider slug='{provider.slug}' family='{provider.family}' "
        f"auto_synced={len(sync_result['added']) if sync_result else 0} by {admin.get('email', 'unknown')}"
    )
    return {"provider": _provider_out(provider, db)}


@router.get("/families", summary="Supported provider families (for the 'add provider' form)")
def list_families(admin: dict = Depends(require_admin)):
    return {"families": FAMILIES}


@router.get("/ollama-suggestions", summary="Autocomplete suggestions for the Ollama 'pull a new model' action")
def ollama_suggestions(admin: dict = Depends(require_admin)):
    try:
        with open(_OLLAMA_SUGGESTIONS_PATH) as f:
            data = json.load(f)
        return {"suggestions": data.get("suggestions", [])}
    except Exception as exc:
        logger.warning(f"[llm-provider-admin] could not read ollama suggestions file: {exc}")
        return {"suggestions": []}


@router.get("/{provider_id}", summary="Get a single LLM provider")
def get_provider(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")
    return {"provider": _provider_out(provider, db)}


@router.put("/{provider_id}", summary="Update an LLM provider")
def update_provider(
    provider_id: str,
    body: ProviderUpdate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    if body.name is not None:
        provider.name = body.name.strip() or provider.name
    if body.base_url is not None:
        provider.base_url = body.base_url.strip() or None
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.extra_config is not None:
        provider.extra_config = body.extra_config
    if body.api_key:
        provider.credential_id = _store_credential(provider.slug, body.api_key)

    if body.enabled is not None:
        # This can flip enabled state the same as PATCH /toggle — re-settle
        # the default the same way that endpoint does.
        ensure_default_model(db)

    db.commit()
    db.refresh(provider)
    invalidate_cache()

    logger.info(f"[llm-provider-admin] updated provider slug='{provider.slug}' by {admin.get('email', 'unknown')}")
    return {"provider": _provider_out(provider, db)}


@router.delete("/{provider_id}", summary="Delete an LLM provider")
def delete_provider(
    provider_id: str,
    cascade: bool = Query(False),
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    enabled_models = db.query(LLMModel).filter_by(provider_id=provider.id, enabled=True).count()
    if enabled_models and not cascade:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Provider '{provider.slug}' has {enabled_models} enabled model(s). "
                f"Disable or delete them first, or retry with ?cascade=true."
            ),
        )

    slug = provider.slug
    db.delete(provider)   # ON DELETE CASCADE removes llm_models rows
    ensure_default_model(db)   # re-pick if the default lived under this provider
    db.commit()
    _delete_credential(slug)
    invalidate_cache()

    logger.info(f"[llm-provider-admin] deleted provider slug='{slug}' by {admin.get('email', 'unknown')}")
    return {"deleted": True, "slug": slug}


@router.patch("/{provider_id}/toggle", summary="Enable or disable an LLM provider")
def toggle_provider(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    provider.enabled = not provider.enabled
    # Re-enabling makes this provider's models eligible again (may need a
    # default if nothing else has one); disabling may have just taken the
    # current default out of rotation (get_default_model_id() requires the
    # provider to be enabled too) — either way, re-settle onto a valid
    # candidate immediately rather than leaving the platform without one
    # until the next unrelated write happens to trigger it.
    ensure_default_model(db)
    db.commit()
    db.refresh(provider)
    invalidate_cache()

    logger.info(f"[llm-provider-admin] toggled provider slug='{provider.slug}' enabled={provider.enabled} by {admin.get('email', 'unknown')}")
    return {"provider": _provider_out(provider, db)}


@router.post("/{provider_id}/test-connection", summary="Verify credentials/reachability for a provider")
def test_connection(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    try:
        models = _discover_models(provider)
        provider.last_verify_status = "ok"
        provider.last_verify_error = None
        detail = f"Reachable — {len(models)} model(s) visible."
    except HTTPException as exc:
        provider.last_verify_status = "error"
        provider.last_verify_error = str(exc.detail)
        detail = str(exc.detail)

    provider.last_verified_at = datetime.utcnow()
    db.commit()
    logger.info(f"[llm-provider-admin] test-connection slug='{provider.slug}' status={provider.last_verify_status} by {admin.get('email', 'unknown')}")
    return {"status": provider.last_verify_status, "detail": detail}


@router.get("/{provider_id}/discover-models", summary="Preview the provider's model catalog (read-only, does not write to the DB)")
def discover_models(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Read-only counterpart to /sync-models — returns the raw candidate list so
    the UI can offer it as search/autocomplete suggestions without writing
    anything. This is the ONLY discovery path for `openai_compatible`
    providers (OpenRouter etc.): their catalogs can run into the hundreds of
    models from many unrelated vendors, so bulk-importing all of them (what
    /sync-models does) would flood the admin's model list. The frontend's
    TypeaheadAddModelForm uses this to suggest names, then adds exactly the
    one the admin picks via POST /{provider_id}/models.
    """
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")
    return {"models": _discover_models(provider)}


def _upsert_discovered_models(provider: LLMProvider, discovered: List[dict], db: Session, created_by: Optional[str]) -> dict:
    """Shared upsert loop behind /sync-models and the auto-sync that fires
    right after a provider is created — a provider an admin just configured
    should be immediately usable in chat without a separate manual
    'Sync models' click, matching what install.sh's bootstrap script already
    does for providers seeded from .env."""
    existing = {m.model_id: m for m in db.query(LLMModel).filter_by(provider_id=provider.id).all()}

    added, updated, unchanged = [], [], []
    for d in discovered:
        mid = d["model_id"]
        if mid in existing:
            m = existing[mid]
            # Merge rather than overwrite — never clobber capability fields an
            # admin already hand-edited that discovery doesn't report.
            merged = {**(m.capabilities or {}), **{k: v for k, v in d["capabilities"].items() if v is not None}}
            if merged != (m.capabilities or {}):
                m.capabilities = merged
                updated.append(mid)
            else:
                unchanged.append(mid)
        else:
            db.add(LLMModel(
                provider_id=provider.id, model_id=mid, display_name=d["display_name"],
                capabilities=d["capabilities"], enabled=True, source="discovered",
                created_by=created_by,
            ))
            added.append(mid)

    return {"added": added, "updated": updated, "unchanged": unchanged}


@router.post("/{provider_id}/sync-models", summary="Discover and upsert models from the provider API")
def sync_models(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")
    if provider.family == "openai_compatible":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Bulk sync is disabled for openai_compatible providers (their catalogs can run into the "
                   "hundreds of unrelated models). Use GET /discover-models for suggestions and add models "
                   "one at a time via POST /{provider_id}/models.",
        )

    discovered = _discover_models(provider)   # raises 502 on failure
    result = _upsert_discovered_models(provider, discovered, db, admin.get("email") or admin.get("sub"))

    ensure_default_model(db)
    db.commit()
    invalidate_cache()
    logger.info(f"[llm-provider-admin] sync-models slug='{provider.slug}' added={len(result['added'])} updated={len(result['updated'])} by {admin.get('email', 'unknown')}")
    return result


@router.post("/{provider_id}/pull-model", summary="Ollama only: pull a new model by name")
def pull_model(
    provider_id: str,
    body: PullModelRequest,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")
    if provider.family != "ollama":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="pull-model is only supported for the 'ollama' family — other providers' models "
                   "are added via 'Sync models' or 'Add model manually'.",
        )
    if not provider.base_url:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This provider has no base_url configured.")

    job_id = str(_uuid_mod.uuid4())
    thread = threading.Thread(
        target=_run_ollama_pull,
        args=(provider.id, provider.base_url, body.model_id, job_id, admin.get("email") or admin.get("sub")),
        daemon=True,
    )
    thread.start()

    logger.info(f"[llm-provider-admin] started ollama pull '{body.model_id}' job={job_id} slug='{provider.slug}' by {admin.get('email', 'unknown')}")
    return {"job_id": job_id}


@router.get("/{provider_id}/pull-status/{job_id}", summary="Poll an Ollama pull job")
def pull_status(
    provider_id: str,
    job_id: str,
    admin: dict = Depends(require_admin),
):
    from core.kv import get_kv
    kv = get_kv(0)
    raw = kv.get(_pull_job_key(job_id))
    if not raw:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or expired job id.")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Model routes (nested under a provider for list/create; flat for update/delete
# since a model row doesn't need its parent provider id repeated in the path)
# ---------------------------------------------------------------------------

@router.get("/{provider_id}/models", summary="List models for a provider")
def list_models(
    provider_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")
    models = (
        db.query(LLMModel)
        .filter_by(provider_id=provider_id)
        .order_by(LLMModel.sort_order, LLMModel.created_at)
        .all()
    )
    return {"models": [_model_out(m) for m in models], "count": len(models)}


@router.delete("/{provider_id}/models", summary="Bulk-delete a provider's models, optionally filtered by source")
def bulk_delete_models(
    provider_id: str,
    source: Optional[str] = Query(None, description="Only delete models with this `source` value (discovered|manual|seed). Omit to delete all."),
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Convenience for undoing an accidental bulk import — e.g. an openai_compatible
    provider synced before bulk sync was disabled for that family, leaving
    hundreds of `source='discovered'` rows. `?source=discovered` clears only
    those; omitting `source` clears every model under the provider.
    """
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    q = db.query(LLMModel).filter_by(provider_id=provider_id)
    if source:
        q = q.filter_by(source=source)
    deleted = q.delete(synchronize_session=False)
    ensure_default_model(db)   # re-pick if the default was among the deleted rows
    db.commit()
    invalidate_cache()

    logger.info(f"[llm-provider-admin] bulk-deleted {deleted} model(s) (source={source or 'any'}) from provider slug='{provider.slug}' by {admin.get('email', 'unknown')}")
    return {"deleted": deleted}


@router.post("/{provider_id}/models", status_code=status.HTTP_201_CREATED, summary="Manually add a model")
def create_model(
    provider_id: str,
    body: ModelCreate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    provider = db.query(LLMProvider).filter_by(id=provider_id).first()
    if not provider:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provider not found.")

    if db.query(LLMModel).filter_by(provider_id=provider_id, model_id=body.model_id).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Model '{body.model_id}' already exists under this provider.",
        )

    capabilities = dict(body.capabilities or {})
    if provider.family == "ollama" and "billing_tier" not in capabilities:
        capabilities["billing_tier"] = "free"   # self-hosted — never billable

    model = LLMModel(
        provider_id=provider_id,
        model_id=body.model_id,
        display_name=body.display_name,
        capabilities=capabilities,
        enabled=True,
        source="manual",
        created_by=admin.get("email") or admin.get("sub"),
    )
    db.add(model)
    ensure_default_model(db)
    db.commit()
    db.refresh(model)
    invalidate_cache()

    logger.info(f"[llm-provider-admin] added model '{model.model_id}' to provider slug='{provider.slug}' by {admin.get('email', 'unknown')}")
    return {"model": _model_out(model)}


@router.put("/models/{model_id}", summary="Update a model")
def update_model(
    model_id: str,
    body: ModelUpdate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    model = db.query(LLMModel).filter_by(id=model_id).first()
    if not model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found.")

    if body.display_name is not None:
        model.display_name = body.display_name.strip() or model.display_name
    if body.capabilities is not None:
        model.capabilities = body.capabilities
    if body.enabled is not None:
        model.enabled = body.enabled
    if body.is_default is not None:
        model.is_default = body.is_default
        if body.is_default:
            # core.llm_provider_registry.get_default_model_id() returns the
            # FIRST model it finds flagged is_default — so more than one
            # default row would make "the" default ambiguous and silently
            # depend on sort_order/created_at instead of the admin's most
            # recent choice. Enforce single-default globally (across every
            # provider, not just this one) by clearing any other row.
            db.query(LLMModel).filter(
                LLMModel.id != model.id, LLMModel.is_default.is_(True)
            ).update({"is_default": False}, synchronize_session=False)
    if body.sort_order is not None:
        model.sort_order = body.sort_order

    # Covers both directions: enabling the first/only eligible model should
    # give it the default automatically, and disabling the current default
    # should hand it off to another enabled model immediately rather than
    # leaving the platform without one until some unrelated write notices.
    if body.enabled is not None:
        ensure_default_model(db)

    db.commit()
    db.refresh(model)
    invalidate_cache()

    logger.info(f"[llm-provider-admin] updated model '{model.model_id}' by {admin.get('email', 'unknown')}")
    return {"model": _model_out(model)}


@router.delete("/models/{model_id}", summary="Delete a model")
def delete_model(
    model_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    model = db.query(LLMModel).filter_by(id=model_id).first()
    if not model:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found.")

    # Guard against silently breaking a managed endpoint or governance rule that
    # names this exact model_id — mirrors endpoint_mgmt_router's "don't let a
    # write leave something else broken" convention.
    referenced_by = []
    try:
        from db.models import ManagedEndpoint
        for ep in db.query(ManagedEndpoint).filter(ManagedEndpoint.enabled.is_(True)).all():
            if ep.model_ids and model.model_id in ep.model_ids:
                referenced_by.append(f"endpoint:{ep.slug}")
    except Exception:
        pass

    if referenced_by:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Model '{model.model_id}' is referenced by: {referenced_by}. "
                f"Remove it from those allowlists first, or disable it instead of deleting."
            ),
        )

    mid = model.model_id
    db.delete(model)
    ensure_default_model(db)   # re-pick if this was the default, so one always stays set
    db.commit()
    invalidate_cache()

    logger.info(f"[llm-provider-admin] deleted model '{mid}' by {admin.get('email', 'unknown')}")
    return {"deleted": True, "model_id": mid}
