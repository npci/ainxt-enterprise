# SPDX-License-Identifier: MIT
# ============================================================
# Endpoint Management Router
# Admin CRUD for named OpenAI-compatible proxy endpoints.
#
# Design:
#   - Each endpoint gets a platform-generated API key stored in user_api_keys
#     (same table as CLI keys), linked via api_key_id FK.
#   - Callers authenticate with that key: Authorization: Bearer <key>
#   - use_env_key toggle controls which key is forwarded to LiteLLM:
#       True  → os.getenv(env_key_name)  (team-specific LiteLLM virtual key)
#       False → global LOCAL_LLM_API_KEY
#   - env_key_name is only required when use_env_key=True
#
# CLOUD MODELS — THIS ROUTER IS THE ONLY WAY TO ENABLE THEM
#   model_ids may contain local (LiteLLM) AND cloud (GPT/Claude/Gemini) models.
#   Every route here requires Depends(require_admin), so cloud enablement is
#   structurally admin-only; the proxy router only ever READS an allowlist and
#   refuses any cloud model that is not in it.
#
#   Because cloud inference costs money, one invariant is enforced on write:
#     Cloud models require a `hod_email` — that HOD's monthly cap funds the
#     spend (ainxt.endpoint_hod_mapping → ainxt.hod_allocation_caps). 422
#     otherwise, so an endpoint can never be saved in a state where every
#     cloud request would fail at runtime with 503.
#
#   Cloud enablement is DERIVED from model_ids ∩ cloud catalog — there is no
#   separate boolean that could drift out of sync with the allowlist.
#
#   The fallback model served for an unrecognised model name is COMPUTED at
#   request time from model_ids (local-first, else cheapest cloud — see
#   endpoint_proxy_router._resolve_model /
#   services.endpoint_model_catalog.cheapest_cloud_model), never admin-set.
#   managed_endpoints.fallback_model is a legacy column: no longer written by
#   this router (any value in a request body is silently ignored by Pydantic),
#   and _ep_out reports the COMPUTED value for display, not the stored one.
#
# Proxy URLs (handled by endpoint_proxy_router.py):
#   POST /ainxt/v1/api/{slug}/v1/chat/completions
#   GET  /ainxt/v1/api/{slug}/v1/models
# ============================================================

import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth.dependencies import require_admin
from core.config import HOD_APPROVAL_ENABLED
from core.logger import logger

from db.database import SessionLocal
from db.models import ManagedEndpoint, UserAPIKey, User

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_LITELLM_BASE_URL = (
    os.getenv("LOCAL_LLM_BASE_URL") or os.getenv("LITELLM_BASE_URL", "")
).rstrip("/")
_LOCAL_LLM_API_KEY = (
    os.getenv("LOCAL_LLM_API_KEY") or os.getenv("LITELLM_API_KEY", "sk-local")
)

# Slug validation: lowercase alphanumeric + hyphens, 3–50 chars
_SLUG_RE    = re.compile(r"^[a-z0-9][a-z0-9\-]{2,49}$")
# Env key name: uppercase letters, digits, underscores
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,99}$")

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/endpoint-mgmt", tags=["endpoint-management"])

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
# Key generation helpers — mirrors routers/api_keys_router.py pattern
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _generate_endpoint_key(slug: str, admin_user_id: str, db: Session):
    """
    Generate a platform API key for an endpoint and store it in user_api_keys.
    Follows the exact same pattern as CLI key generation in api_keys_router.py:
      - raw key format: ainxt-{slug8}-{uuid4_hex}
      - only SHA-256 hash stored; raw key returned once and never stored
      - label = "endpoint:{slug}" distinguishes from user CLI keys

    Returns (raw_key, key_row) where key_row is the flushed UserAPIKey ORM object.
    """
    slug8  = re.sub(r"[^a-z0-9]", "", slug.lower())[:8]
    raw    = f"ainxt-{slug8}-{uuid.uuid4().hex}"
    prefix = raw[:20]
    khash  = _sha256(raw)

    key_row = UserAPIKey(
        user_id    = admin_user_id,
        key_prefix = prefix,
        key_hash   = khash,
        label      = f"endpoint:{slug}",
        is_active  = True,
    )
    db.add(key_row)
    db.flush()   # populate key_row.id before the endpoint row references it
    return raw, key_row

def _create_system_user(slug: str, name: str, org_id: str, db: Session) -> User:
    """
    Create a system user to own an endpoint's usage in billing/audit.
    Email is deterministic so re-creation is idempotent-safe.
    """
    email = f"endpoint-{slug}@system.ainxt"
    user = User(
        email=email,
        name=f"{name}",
        role="user",
        org_id=org_id,
        is_active=True,
        account_status="active",
        email_verified=True,
        department="system",
        ad_level=6,
    )
    db.add(user)
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Cache invalidation
# ---------------------------------------------------------------------------


def _invalidate_slug_cache(slug: str, endpoint_id: Optional[str] = None):
    """
    Drop every cached entry for an endpoint so config changes take effect at once.

    Without the endpoint→HOD invalidation, a newly assigned budget owner would not
    be picked up by the proxy for up to 60s, and cloud requests would keep failing
    with "no budget owner configured".
    """
    try:
        from core.kv import get_kv
        kv = get_kv(0)
        kv.delete(f"ep:slug:{slug}")
        kv.delete(f"ep:models:{slug}")
    except Exception:
        pass

    if endpoint_id:
        try:
            from services.endpoint_budget_governor import invalidate_endpoint_hod_cache
            invalidate_endpoint_hod_cache(str(endpoint_id))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# LiteLLM model fetch helper
# ---------------------------------------------------------------------------


def _resolve_litellm_key(ep: ManagedEndpoint) -> str:
    """
    Return the LiteLLM API key to use for this endpoint based on use_env_key.
    Raises 503 if use_env_key=True but the env var is not set.
    """
    if ep.use_env_key:
        if not ep.env_key_name:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="use_env_key is ON but env_key_name is not set on this endpoint.",
            )
        key = os.getenv(ep.env_key_name)
        if not key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"use_env_key is ON but env var '{ep.env_key_name}' is not set.",
            )
        return key
    return _LOCAL_LLM_API_KEY


def _fetch_models_for_key(api_key: str) -> List[str]:
    """Call LiteLLM /v1/models with the given key. Raises 502 on failure."""
    if not _LITELLM_BASE_URL:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="LITELLM_BASE_URL / LOCAL_LLM_BASE_URL is not configured.",
        )
    try:
        resp = httpx.get(
            f"{_LITELLM_BASE_URL}/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LiteLLM returned {exc.response.status_code} when fetching models.",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach LiteLLM: {exc}",
        )


# ---------------------------------------------------------------------------
# Local-model validation
# ---------------------------------------------------------------------------


def _validate_models(model_ids: Optional[List[str]]) -> dict:
    """
    Validate an endpoint's model allowlist. Accepts BOTH local and cloud models.

    THIS IS THE ONLY PLACE CLOUD MODELS CAN BE ENABLED. Every route on this
    router requires Depends(require_admin), so cloud enablement is structurally
    admin-only — the proxy path never widens an allowlist, it only reads one.

    Returns {"cloud": [...], "local": [...]} so callers can apply the
    "cloud requires a funding HOD" rule.

    Raises 422 for unknown or blocked models, 503 only when the local catalog is
    genuinely needed but unreachable.
    """
    out = {"cloud": [], "local": []}
    if not model_ids:
        return out

    from services.endpoint_model_catalog import get_cloud_models, get_local_models

    cloud_catalog = set(get_cloud_models())

    # Only reach for the local catalog if something isn't a known cloud model —
    # a purely cloud allowlist must not 503 just because LiteLLM is down.
    non_cloud = [m for m in model_ids if m not in cloud_catalog]
    local_catalog: set = set()
    local_unavailable = False
    if non_cloud:
        try:
            local_catalog = set(get_local_models())
        except Exception as exc:
            logger.warning("[endpoint-mgmt] local model catalog unreachable: %s", exc)
            local_catalog = set()
        local_unavailable = not local_catalog

    invalid: List[str] = []
    for m in model_ids:
        if m in cloud_catalog:
            out["cloud"].append(m)
        elif m in local_catalog:
            out["local"].append(m)
        else:
            invalid.append(m)

    if invalid:
        # Distinguish "we cannot check" (503, retry later) from "this model does
        # not exist" (422, fix your input). Reporting 503 for a typo'd model name
        # would send an admin chasing an infrastructure problem that isn't there.
        if local_unavailable:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"These models are not in the cloud catalog and the local "
                    f"LiteLLM catalog is unreachable, so they cannot be verified: "
                    f"{invalid}. Available cloud models: {sorted(cloud_catalog)}."
                ),
            )
        # A blocked model (e.g. a retired Opus, or gpt-5.5) lands here too, since
        # get_cloud_models() filters BLOCKED_MODELS.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown, retired, or blocked models: {invalid}. "
                f"Available cloud models: {sorted(cloud_catalog)}. "
                f"Available local models: {sorted(local_catalog)}."
            ),
        )
    return out


def _validate_hod_email(hod_email: Optional[str]) -> Optional[str]:
    """
    Validate a HOD email against the DBA-owned ainxt.department_hod_mapping.

    Matched case-insensitively (consistent with services/hod_budget_governor.py)
    and returned lowercased so the mapping row and every later lookup agree.
    Returns None for a blank input. Raises 422 if the email is not a known HOD.
    """
    email = (hod_email or "").strip().lower()
    if not email:
        return None

    try:
        db = SessionLocal()
        try:
            row = db.execute(
                text(
                    'SELECT 1 FROM ainxt.department_hod_mapping '
                    'WHERE lower("hod_email") = :e LIMIT 1'
                ),
                {"e": email},
            ).first()
        finally:
            db.close()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not verify the HOD email: {exc}",
        )

    if not row:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"'{email}' is not a known HOD. The email must appear in the "
                f"department-to-HOD mapping."
            ),
        )
    return email


def _enforce_cloud_requires_hod(
        model_ids: Optional[List[str]],
        hod_email: Optional[str],
        split: Optional[dict] = None,
) -> None:
    """
    Cloud models may not be enabled without a funding HOD.

    Without this, a cloud-enabled endpoint would have no cap to draw from and
    every cloud request would be refused at runtime with 503 — far more confusing
    than refusing to save the configuration.
    """
    if split is None:
        split = _validate_models(model_ids)
    if split["cloud"] and not hod_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "A HOD owner is required when cloud models are enabled — their "
                "monthly budget funds this endpoint's cloud usage. "
                f"Cloud models selected: {split['cloud']}."
            ),
        )


# ---------------------------------------------------------------------------
# HOD mapping persistence  (ainxt.endpoint_hod_mapping)
# ---------------------------------------------------------------------------


def _get_endpoint_hod(endpoint_id: str, db: Session) -> Optional[str]:
    """Current funding HOD for an endpoint, or None."""
    row = db.execute(
        text(
            'SELECT "hod_email" FROM ainxt.endpoint_hod_mapping '
            'WHERE "endpoint_id" = :eid AND "is_active" = TRUE'
        ),
        {"eid": str(endpoint_id)},
    ).first()
    return str(row[0]) if row and row[0] else None


def _set_endpoint_hod(endpoint_id: str, hod_email: Optional[str],
                      actor: str, db: Session) -> None:
    """
    Upsert (or clear) an endpoint's funding HOD.

    uq_ehm_endpoint guarantees one row per endpoint, so this is a true upsert
    rather than a delete-then-insert that could briefly leave an endpoint
    unfunded. Clearing sets is_active=FALSE, preserving who paid historically.
    """
    if hod_email:
        db.execute(
            text(
                'INSERT INTO ainxt.endpoint_hod_mapping '
                ' ("id", "endpoint_id", "hod_email", "is_active", "created_by",'
                '  "created_at", "updated_at") '
                'VALUES (gen_random_uuid(), :eid, :e, TRUE, :by, NOW(), NOW()) '
                'ON CONFLICT ("endpoint_id") DO UPDATE SET '
                '  "hod_email" = EXCLUDED."hod_email", '
                '  "is_active" = TRUE, '
                '  "updated_at" = NOW()'
            ),
            {"eid": str(endpoint_id), "e": hod_email, "by": actor},
        )
    else:
        db.execute(
            text(
                'UPDATE ainxt.endpoint_hod_mapping '
                'SET "is_active" = FALSE, "updated_at" = NOW() '
                'WHERE "endpoint_id" = :eid'
            ),
            {"eid": str(endpoint_id)},
        )


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _ep_out(ep: ManagedEndpoint, db: Session) -> dict:
    """
    Serialize a ManagedEndpoint for API responses.
    Fetches key_prefix from user_api_keys for display.
    Never exposes key_hash, api_key_id, or any raw key value.

    Also reports the cloud-budget state the admin UI needs: which of the allowed
    models are cloud, the funding HOD, and that HOD's live cap/remaining figures.
    """
    key_prefix = None
    key_active = False
    if ep.api_key_id:
        key_row = db.query(UserAPIKey).filter_by(id=ep.api_key_id).first()
        if key_row:
            key_prefix = key_row.key_prefix
            key_active = bool(key_row.is_active)

    env_key_configured = False
    if ep.use_env_key and ep.env_key_name:
        env_key_configured = bool(os.getenv(ep.env_key_name))

    model_ids = ep.model_ids or []

    # Cloud enablement is DERIVED from the allowlist, never stored separately —
    # so it can never drift out of sync with the models actually permitted.
    cloud_models: List[str] = []
    try:
        from services.endpoint_model_catalog import get_cloud_models
        cloud_catalog = set(get_cloud_models())
        cloud_models  = [m for m in model_ids if m in cloud_catalog]
    except Exception:
        pass

    # Fallback for an unrecognised model — COMPUTED for display, exactly what
    # endpoint_proxy_router._resolve_model would actually pick at request time
    # (local-first, else cheapest cloud). Never read from ep.fallback_model —
    # that column is a legacy write target this router no longer uses.
    computed_fallback = None
    try:
        from services.endpoint_model_catalog import cheapest_cloud_model, first_local_model
        computed_fallback = first_local_model(model_ids) or cheapest_cloud_model(model_ids)
    except Exception:
        pass

    hod_email = None
    try:
        hod_email = _get_endpoint_hod(ep.id, db)
    except Exception:
        pass   # a missing mapping table must not break the Endpoints screen

    # Live budget for the UI banner. Fails soft (zeros) by design.
    hod_budget = None
    if hod_email:
        try:
            from services.endpoint_budget_governor import get_endpoint_budget_status
            hod_budget = get_endpoint_budget_status(hod_email)
        except Exception:
            hod_budget = None

    return {
        "id":                 ep.id,
        "name":               ep.name,
        "slug":               ep.slug,
        "org_id":             ep.org_id,
        "description":        ep.description,
        "use_env_key":        ep.use_env_key,
        "env_key_name":       ep.env_key_name,
        "env_key_configured": env_key_configured,   # True only when use_env_key=True and var is set
        "key_prefix":         key_prefix,            # display hint e.g. "ainxt-lxpendp-f47ac1"
        "key_active":         key_active,
        "enabled":            ep.enabled,
        "tool_calls_enabled": ep.tool_calls_enabled,
        "model_ids":          model_ids,             # allowed models (local AND cloud); [] = no restriction
        "cloud_models":       cloud_models,          # subset of model_ids that costs money
        "cloud_enabled":      bool(cloud_models),
        "fallback_model":     computed_fallback,     # COMPUTED, not admin-set — see note above
        "hod_email":          hod_email,             # funds this endpoint's cloud spend
        "hod_budget":         hod_budget,            # {cap_usd, consumed_usd, remaining_usd, ...}
        "created_by":         ep.created_by,
        "created_at":         ep.created_at.isoformat() if ep.created_at else None,
        "updated_at":         ep.updated_at.isoformat() if ep.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class EndpointCreate(BaseModel):
    name:         str
    slug:         str
    description:  Optional[str] = None
    use_env_key:  bool = False
    env_key_name: Optional[str] = None   # required only when use_env_key=True
    tool_calls_enabled: bool = True
    model_ids:    Optional[List[str]] = None  # allowed models, local AND cloud; None = no restriction
    # Required when model_ids contains any cloud model — funds the cloud spend.
    hod_email:      Optional[str] = None
    # NOTE: no fallback_model field — the fallback for an unrecognised model is
    # COMPUTED from model_ids at request time (local-first, else cheapest
    # cloud), never admin-set. See endpoint_proxy_router._resolve_model.

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not _SLUG_RE.match(v):
            raise ValueError(
                "Slug must be 3–50 lowercase alphanumeric characters or hyphens "
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

    @field_validator("env_key_name")
    @classmethod
    def validate_env_key_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().upper()
        if not _ENV_KEY_RE.match(v):
            raise ValueError(
                "Env key name must be uppercase letters, digits, and underscores "
                "(e.g. TEAM_LITELLM_API_KEY)."
            )
        return v


class EndpointUpdate(BaseModel):
    name:         Optional[str]  = None
    description:  Optional[str]  = None
    use_env_key:  Optional[bool] = None
    env_key_name: Optional[str]  = None
    tool_calls_enabled: Optional[bool] = None
    model_ids:    Optional[List[str]] = None  # None = leave unchanged; [] = clear all allowed models
    # None = leave unchanged; "" = clear (only permitted when no cloud models remain)
    hod_email:      Optional[str] = None
    # NOTE: no fallback_model field — see EndpointCreate.

    @field_validator("env_key_name")
    @classmethod
    def validate_env_key_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().upper()
        if not _ENV_KEY_RE.match(v):
            raise ValueError(
                "Env key name must be uppercase letters, digits, and underscores "
                "(e.g. TEAM_LITELLM_API_KEY)."
            )
        return v


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/", summary="List all managed endpoints")
def list_endpoints(
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """Returns all managed endpoints. key_hash and api_key_id are never included."""
    eps = db.query(ManagedEndpoint).order_by(ManagedEndpoint.created_at.desc()).all()
    return {"endpoints": [_ep_out(ep, db) for ep in eps], "count": len(eps)}


@router.post("/", status_code=status.HTTP_201_CREATED, summary="Create a managed endpoint")
def create_endpoint(
    body: EndpointCreate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Creates a new managed endpoint and generates its platform API key.
    The raw key is returned ONCE in this response — share it securely with the team.
    The key is stored in user_api_keys (same as CLI keys) as a SHA-256 hash only.
    """
    # Validate use_env_key + env_key_name consistency
    if body.use_env_key and not body.env_key_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="env_key_name is required when use_env_key is True.",
        )

    # Validate model_ids (local AND cloud) and enforce the cloud→HOD rule.
    # Flat mode (HOD_APPROVAL_ENABLED=False, the default): skip the HOD-picker
    # requirement entirely for cloud-enabled endpoints — no endpoint_hod_mapping
    # row is created, hod_email stays None. Cloud spend is instead gated
    # against the org-wide cap at request time (see endpoint_proxy_router.py).
    split = _validate_models(body.model_ids)
    if HOD_APPROVAL_ENABLED:
        hod_email = _validate_hod_email(body.hod_email)
        _enforce_cloud_requires_hod(body.model_ids, hod_email, split)
    else:
        hod_email = None

    # Check slug uniqueness
    if db.query(ManagedEndpoint).filter_by(slug=body.slug).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An endpoint with slug '{body.slug}' already exists.",
        )

    # Warn if env var not yet set
    if body.use_env_key and body.env_key_name:
        if not os.getenv(body.env_key_name):
            logger.warning(
                f"[endpoint-mgmt] Creating endpoint slug='{body.slug}' with use_env_key=True "
                f"but env var '{body.env_key_name}' is not set — LiteLLM calls will return 503 "
                f"until the key is added to .env and the gateway is restarted."
            )

    # Generate platform API key → stored in user_api_keys
    admin_user_id = admin.get("sub") or admin.get("user_id")
    raw_key, key_row = _generate_endpoint_key(body.slug, admin_user_id, db)

    # Create system user to own this endpoint's usage
    system_user = _create_system_user(body.slug, body.name, "default", db)

    ep = ManagedEndpoint(
        name         = body.name,
        slug         = body.slug,
        description  = body.description,
        api_key_id   = key_row.id,
        use_env_key  = body.use_env_key,
        env_key_name = body.env_key_name,
        tool_calls_enabled = body.tool_calls_enabled,
        model_ids    = body.model_ids or None,
        system_user_id = system_user.id,
        enabled      = True,
        created_by   = admin.get("email") or admin.get("sub"),
    )
    db.add(ep)
    db.flush()   # need ep.id before the HOD mapping row can reference it

    if hod_email:
        _set_endpoint_hod(ep.id, hod_email, admin.get("email") or "admin", db)

    db.commit()
    db.refresh(ep)
    _invalidate_slug_cache(ep.slug, ep.id)

    logger.info(
        f"[endpoint-mgmt] Created endpoint slug='{ep.slug}' use_env_key={ep.use_env_key} "
        f"cloud_models={split['cloud']} hod={hod_email or '-'} "
        f"by {admin.get('email', 'unknown')}"
    )

    return {
        "endpoint":  _ep_out(ep, db),
        "key":       raw_key,
        "key_note":  "This key will not be shown again. Share it securely with your team.",
    }


# ── IMPORTANT: /available-models and /hods MUST be registered BEFORE /{endpoint_id} ──
# FastAPI matches routes in registration order, so if /{endpoint_id} came first a
# request to /available-models would bind endpoint_id="available-models" and 404.
# (Same convention as model_governance_router's /my-models comment.)


@router.get("/available-models", summary="Models selectable for an endpoint allowlist")
def available_models(admin: dict = Depends(require_admin)):
    """
    Every model an admin may add to an endpoint, grouped cloud vs local, in ONE
    call so the UI needs no second request.

    Cloud entries carry a price hint so the cost implication of enabling a model
    is visible at selection time. Cloud models are feature-flag filtered and
    exclude BLOCKED_MODELS. `local_available=False` means the LiteLLM proxy is
    unreachable — cloud models can still be selected.
    """
    from services.endpoint_model_catalog import (
        get_cloud_models, get_local_models, price_hint,
    )

    cloud = get_cloud_models()
    local = get_local_models()

    return {
        "cloud": [
            {"id": m, "is_cloud": True, "pricing": price_hint(m)}
            for m in cloud
        ],
        "local": [
            {"id": m, "is_cloud": False, "pricing": None}
            for m in local
        ],
        "cloud_count":     len(cloud),
        "local_count":     len(local),
        "local_available": bool(local),
        "note": (
            "Cloud models cost money and require a HOD budget owner on the "
            "endpoint. Local models are in-house and free."
        ),
    }


@router.get("/hods", summary="HODs available as endpoint budget owners")
def list_hods(admin: dict = Depends(require_admin)):
    """
    HODs that may fund an endpoint, with their current cap and remaining budget.

    Sourced from the DBA-owned ainxt.department_hod_mapping (the same source as
    /budget/admin/hods) so only real HODs can be assigned. Budget figures come
    from endpoint_budget_governor, so the admin sees the SAME numbers the runtime
    gate will use — including spend already consumed by other endpoints.
    """
    try:
        db = SessionLocal()
        try:
            rows = db.execute(text(
                'SELECT lower("hod_email") AS email, '
                '       MAX("hod_name")    AS hod_name, '
                '       array_agg(DISTINCT "department_name") AS departments '
                'FROM ainxt.department_hod_mapping '
                'WHERE "hod_email" IS NOT NULL AND "hod_email" <> \'\' '
                'GROUP BY lower("hod_email") '
                'ORDER BY lower("hod_email")'
            )).fetchall()
        finally:
            db.close()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Could not load the HOD list: {exc}",
        )

    from services.endpoint_budget_governor import get_endpoint_budget_status

    hods = []
    for r in rows:
        email = r[0]
        item  = {
            "hod_email":   email,
            "hod_name":    r[1] or email,
            "departments": [d for d in (r[2] or []) if d],
        }
        try:
            item["budget"] = get_endpoint_budget_status(email)
        except Exception:
            item["budget"] = None
        hods.append(item)

    return {"hods": hods, "count": len(hods)}


@router.get("/{endpoint_id}", summary="Get a single managed endpoint")
def get_endpoint(
    endpoint_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")
    return {"endpoint": _ep_out(ep, db)}


@router.put("/{endpoint_id}", summary="Update a managed endpoint")
def update_endpoint(
    endpoint_id: str,
    body: EndpointUpdate,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Update name, description, use_env_key, env_key_name, model_ids, or
    hod_email. Slug is immutable. API key unchanged — use regenerate-key.
    (fallback_model is computed at request time, not settable here — see
    endpoint_proxy_router._resolve_model.)

    Cloud models and the funding HOD are validated TOGETHER against the resulting
    state, not the submitted fields alone: removing a HOD while cloud models
    remain, or adding cloud models to an endpoint that has no HOD, are both
    rejected with 422. Otherwise the endpoint would be saved in a state where
    every cloud request fails at runtime.
    """
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    if body.name is not None:
        ep.name = body.name.strip()
    if body.description is not None:
        ep.description = body.description
    if body.use_env_key is not None:
        ep.use_env_key = body.use_env_key
    if body.env_key_name is not None:
        ep.env_key_name = body.env_key_name
    if body.tool_calls_enabled is not None:
        ep.tool_calls_enabled = body.tool_calls_enabled

    # ── Resolve the POST-UPDATE state before validating ──────────────────────
    # Flat mode (HOD_APPROVAL_ENABLED=False): skip the HOD-picker requirement
    # entirely — cloud endpoints need no funding HOD; the org-wide cap governs
    # cloud spend instead. hod_email inputs are ignored rather than validated.
    final_models = body.model_ids if body.model_ids is not None else (ep.model_ids or [])
    current_hod  = _get_endpoint_hod(ep.id, db)

    if HOD_APPROVAL_ENABLED:
        hod_changed = body.hod_email is not None
        if hod_changed:
            # "" explicitly clears the owner; a value is validated against the mapping.
            final_hod = _validate_hod_email(body.hod_email) if body.hod_email.strip() else None
        else:
            final_hod = current_hod

        split = _validate_models(final_models)
        _enforce_cloud_requires_hod(final_models, final_hod, split)
    else:
        hod_changed = False
        final_hod = current_hod
        _validate_models(final_models)

    if body.model_ids is not None:
        ep.model_ids = body.model_ids or None      # empty list → clear the allowlist

    # No fallback_model handling here — it is computed at request time from
    # the (possibly just-updated) model_ids, never stored. See _ep_out for the
    # equivalent computation used to display it.

    # Validate consistency after update
    if ep.use_env_key and not ep.env_key_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="env_key_name is required when use_env_key is True.",
        )

    if hod_changed and final_hod != current_hod:
        _set_endpoint_hod(ep.id, final_hod, admin.get("email") or "admin", db)

    db.commit()
    db.refresh(ep)
    _invalidate_slug_cache(ep.slug, ep.id)

    logger.info(
        f"[endpoint-mgmt] Updated endpoint slug='{ep.slug}' "
        f"cloud_models={split['cloud']} hod={final_hod or '-'} "
        f"by {admin.get('email', 'unknown')}"
    )
    return {"endpoint": _ep_out(ep, db)}


@router.delete(
    "/{endpoint_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a managed endpoint",
)
def delete_endpoint(
    endpoint_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    slug = ep.slug
    ep_id = ep.id
    old_key_id = ep.api_key_id
    system_user_id = ep.system_user_id

    # The endpoint_hod_mapping row is removed by ON DELETE CASCADE.
    db.delete(ep)

    # Revoke the associated API key (SET NULL FK means key row still exists — revoke it)
    if old_key_id:
        key_row = db.query(UserAPIKey).filter_by(id=old_key_id).first()
        if key_row:
            key_row.is_active  = False
            key_row.revoked_at = datetime.now(timezone.utc)
    
    # Soft-disable the system user
    if system_user_id:
        sys_user = db.query(User).filter_by(id=system_user_id).first()
        if sys_user:
            sys_user.is_active = False
            sys_user.account_status = "suspended"


    db.commit()
    _invalidate_slug_cache(slug, ep_id)

    logger.info(
        f"[endpoint-mgmt] Deleted endpoint slug='{slug}' "
        f"by {admin.get('email', 'unknown')}"
    )
    # 204 — no body


@router.patch("/{endpoint_id}/toggle", summary="Enable or disable a managed endpoint")
def toggle_endpoint(
    endpoint_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    ep.enabled = not ep.enabled
    db.commit()
    db.refresh(ep)
    _invalidate_slug_cache(ep.slug)

    logger.info(
        f"[endpoint-mgmt] Toggled endpoint slug='{ep.slug}' enabled={ep.enabled} "
        f"by {admin.get('email', 'unknown')}"
    )
    return {"endpoint": _ep_out(ep, db)}


@router.post(
    "/{endpoint_id}/regenerate-key",
    summary="Regenerate the platform API key for an endpoint",
)
def regenerate_key(
    endpoint_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Generates a new platform API key for the endpoint.
    The old key is immediately revoked (is_active=False in user_api_keys).
    The new raw key is returned ONCE — the old key stops working immediately.
    """
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    # Revoke old key
    if ep.api_key_id:
        old_key = db.query(UserAPIKey).filter_by(id=ep.api_key_id).first()
        if old_key:
            old_key.is_active  = False
            old_key.revoked_at = datetime.now(timezone.utc)

    # Generate new key
    admin_user_id = admin.get("sub") or admin.get("user_id")
    raw_key, new_key_row = _generate_endpoint_key(ep.slug, admin_user_id, db)
    ep.api_key_id = new_key_row.id

    db.commit()
    db.refresh(ep)
    _invalidate_slug_cache(ep.slug)

    logger.info(
        f"[endpoint-mgmt] Regenerated key for slug='{ep.slug}' "
        f"by {admin.get('email', 'unknown')}"
    )

    return {
        "key":      raw_key,
        "key_prefix": new_key_row.key_prefix,
        "key_note": "This key will not be shown again. The old key is now invalid.",
    }


@router.get(
    "/{endpoint_id}/preview-models",
    summary="Preview models accessible via this endpoint's LiteLLM key",
)
def preview_models(
    endpoint_id: str,
    admin: dict = Depends(require_admin),
    db: Session = Depends(_get_db),
):
    """
    Fetches the list of models from LiteLLM using the key selected by use_env_key:
      use_env_key=True  → uses os.getenv(env_key_name)
      use_env_key=False → uses global LOCAL_LLM_API_KEY

    Live call — not cached — so admins can verify the key is working.
    Returns 503 if use_env_key=True and the env var is not set.
    Returns 502 if LiteLLM is unreachable.
    """
    ep = db.query(ManagedEndpoint).filter_by(id=endpoint_id).first()
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found.")

    litellm_token = _resolve_litellm_key(ep)
    models  = _fetch_models_for_key(litellm_token)

    return {
        "endpoint_id":  endpoint_id,
        "slug":         ep.slug,
        "use_env_key":  ep.use_env_key,
        "env_key_name": ep.env_key_name if ep.use_env_key else None,
        "models":       models,
        "count":        len(models),
    }
