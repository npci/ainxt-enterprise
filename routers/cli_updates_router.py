# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CLI UPDATE CHANNEL  (mounted at /ainxt/v1/api/updates and /ainxt/v1/api/cli)
#
# Serves the AiNxt CLI's native self-updater (src/shared/utils/nativeInstaller)
# AND records which CLI version each engineer is running (fleet visibility for
# pushing updates). Binaries + manifests live on THIS box (the gateway host)
# under CLI_RELEASES_DIR — no Nexus / no internet needed.
#
#   Update-serving (existing):
#     GET  /updates/{channel}                     → plain-text version (latest|stable)
#     GET  /updates/{version}/manifest.json       → { version, platforms: {<plat>:{checksum}} }
#     GET  /updates/{version}/{platform}/{binary} → the raw binary
#
#   Fleet monitoring (new — Part Z6, 2026-07-08):
#     POST /cli/heartbeat                         → CLI reports its version+env (user-authed)
#     GET  /cli/versions/summary                  → admin: counts per version
#     GET  /cli/versions/users                    → admin: paginated per-user list
#     GET  /cli/versions/stale?older_than_days=7  → admin: installs on non-latest builds
#
# All JWT-authed. Admin endpoints add require_admin. Path components are
# strictly validated to prevent traversal.
# ============================================================

import os
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auth.dependencies import get_current_user, require_admin
from core.logger import logger
from db.database import SessionLocal
from db.models import CliVersionRecord

router = APIRouter(prefix="/updates", tags=["cli_updates"])

# Second router mounted at /cli — same file, distinct prefix so the fleet
# endpoints live under /ainxt/v1/api/cli/... (semantically separate from
# the /updates binary-serving surface).
cli_fleet_router = APIRouter(prefix="/cli", tags=["cli_fleet"])

# Where published CLI releases live on the gateway host.
RELEASES_DIR = os.getenv("CLI_RELEASES_DIR", "/opt/ainxt/cli-releases")

_CHANNELS = {"latest", "stable"}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# ── Heartbeat schema ──────────────────────────────────────────
# Field lengths mirror db.models.CliVersionRecord — enforce at the API edge
# so a malformed client can't cause an INSERT to blow up on VARCHAR overflow.
# All optional except version + install_id — those two are the row key material.

class CliHeartbeatIn(BaseModel):
    version:         str = Field(..., min_length=1, max_length=32)
    install_id:      str = Field(..., min_length=8, max_length=64)
    channel:         Optional[str] = Field(default="latest", max_length=16)
    binary_name:     Optional[str] = Field(default=None, max_length=64)
    os:              Optional[str] = Field(default=None, max_length=32)
    arch:            Optional[str] = Field(default=None, max_length=16)
    os_release:      Optional[str] = Field(default=None, max_length=128)
    runtime:         Optional[str] = Field(default=None, max_length=32)
    runtime_version: Optional[str] = Field(default=None, max_length=32)


class CliHeartbeatOut(BaseModel):
    ok: bool
    latest_version: Optional[str] = None
    update_available: bool = False


def _read_channel_version(channel: str) -> Optional[str]:
    """Read the currently-published version string for a channel (or None)."""
    if channel not in _CHANNELS:
        return None
    fp = os.path.join(RELEASES_DIR, channel)
    if not os.path.isfile(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def _safe(*parts: str) -> None:
    for p in parts:
        if not p or p in (".", "..") or "/" in p or "\\" in p or not _SAFE_COMPONENT.match(p):
            raise HTTPException(status_code=400, detail=f"invalid path component: {p!r}")


def _uid(current_user: dict) -> str:
    # current_user is the JWT payload dict (see auth/dependencies.py:get_current_user)
    return current_user.get("sub") or current_user.get("email") or "unknown"


@router.get("/{channel}")
async def latest_version(channel: str, current_user=Depends(get_current_user)):
    """Return the published version string for a release channel (e.g. '1.0.4')."""
    user_id = _uid(current_user)
    if channel not in _CHANNELS:
        logger.warning(f"[cli-updates] unknown channel '{channel}' user='{user_id}'")
        raise HTTPException(status_code=404, detail="unknown channel")
    fp = os.path.join(RELEASES_DIR, channel)
    if not os.path.isfile(fp):
        logger.warning(f"[cli-updates] channel not published: '{channel}' expected_file='{fp}' user='{user_id}'")
        raise HTTPException(status_code=404, detail="channel not published")
    with open(fp, "r", encoding="utf-8") as f:
        version = f.read().strip()
    logger.info(f"[cli-updates] check channel='{channel}' -> version='{version}' user='{user_id}'")
    return PlainTextResponse(version)


@router.get("/{version}/manifest.json")
async def version_manifest(version: str, current_user=Depends(get_current_user)):
    """Return the per-version manifest (platform → sha256 checksum)."""
    user_id = _uid(current_user)
    _safe(version)
    fp = os.path.join(RELEASES_DIR, version, "manifest.json")
    if not os.path.isfile(fp):
        logger.warning(f"[cli-updates] manifest not found: version='{version}' expected_file='{fp}' user='{user_id}'")
        raise HTTPException(status_code=404, detail="version not found")
    logger.info(f"[cli-updates] serving manifest version='{version}' user='{user_id}'")
    return FileResponse(fp, media_type="application/json")


@router.get("/{version}/{platform}/{binary}")
async def version_binary(
    version: str, platform: str, binary: str, current_user=Depends(get_current_user)
):
    """Stream the platform binary for a version."""
    user_id = _uid(current_user)
    _safe(version, platform, binary)
    fp = os.path.join(RELEASES_DIR, version, platform, binary)
    if not os.path.isfile(fp):
        logger.warning(f"[cli-updates] binary not found: expected_file='{fp}' user='{user_id}'")
        raise HTTPException(status_code=404, detail="binary not found")
    logger.info(f"[cli-updates] serving binary {version}/{platform}/{binary} user='{user_id}'")
    return FileResponse(
        fp, media_type="application/octet-stream", filename=binary
    )


# ============================================================
# FLEET MONITORING — /cli/heartbeat + admin queries
# ============================================================

@cli_fleet_router.post("/heartbeat", response_model=CliHeartbeatOut)
async def cli_heartbeat(
    payload: CliHeartbeatIn,
    current_user=Depends(get_current_user),
):
    """
    Record the calling CLI's version + environment. Called on REPL boot and
    every ~6h for long-running sessions. UPSERT on (user_id, install_id):
    a fresh install adds a row; a returning session bumps last_seen_at +
    session_count and updates version/runtime metadata to the latest values.

    The response includes the currently-published version on the caller's
    channel and an update_available boolean so the CLI can render a soft
    "update available" nudge without a second round trip.

    Payload is telemetry only — no prompts, no file paths, no source
    material. Never blocks the CLI: even a 5xx here is discarded by the
    client so a monitoring hiccup can't degrade the REPL.
    """
    user_id = current_user.get("sub") or current_user.get("id") or "unknown"
    email   = current_user.get("email")

    # Sanitize / clamp fields (Pydantic already enforced max_length; here
    # we just normalise the channel to the small set we recognise).
    channel = (payload.channel or "latest").strip().lower()
    if channel not in _CHANNELS and channel != "dev":
        channel = "latest"

    now = datetime.utcnow()

    stmt = pg_insert(CliVersionRecord.__table__).values(
        user_id         = str(user_id),
        email           = email,
        install_id      = payload.install_id,
        version         = payload.version,
        channel         = channel,
        binary_name     = payload.binary_name,
        os              = payload.os,
        arch            = payload.arch,
        os_release      = payload.os_release,
        runtime         = payload.runtime,
        runtime_version = payload.runtime_version,
        session_count   = 1,
        first_seen_at   = now,
        last_seen_at    = now,
    )
    # ON CONFLICT (user_id, install_id) → bump session_count + refresh
    # everything else. first_seen_at is preserved (excluded so the original
    # first-seen timestamp survives).
    stmt = stmt.on_conflict_do_update(
        constraint="uq_cli_version_user_install",
        set_={
            "email":           stmt.excluded.email,
            "version":         stmt.excluded.version,
            "channel":         stmt.excluded.channel,
            "binary_name":     stmt.excluded.binary_name,
            "os":              stmt.excluded.os,
            "arch":            stmt.excluded.arch,
            "os_release":      stmt.excluded.os_release,
            "runtime":         stmt.excluded.runtime,
            "runtime_version": stmt.excluded.runtime_version,
            "session_count":   CliVersionRecord.session_count + 1,
            "last_seen_at":    stmt.excluded.last_seen_at,
        },
    )

    try:
        with SessionLocal() as db:
            db.execute(stmt)
            db.commit()
    except Exception as exc:
        # Telemetry is best-effort — never fail the CLI over a DB blip.
        logger.warning(
            f"[cli-fleet] heartbeat upsert failed user='{user_id}' "
            f"install='{payload.install_id[:8]}…' err={exc!r}"
        )
        # Still respond OK so the CLI can't retry-storm on a persistent DB
        # outage. Upstream metrics on the router will show the failure.
        return CliHeartbeatOut(ok=True)

    latest = _read_channel_version(channel)
    update_available = bool(latest and latest.strip() and latest != payload.version)

    logger.info(
        f"[cli-fleet] heartbeat user='{user_id}' version='{payload.version}' "
        f"channel='{channel}' os='{payload.os}/{payload.arch}' "
        f"install='{payload.install_id[:8]}…' latest='{latest}' "
        f"update_available={update_available}"
    )
    return CliHeartbeatOut(
        ok=True,
        latest_version=latest,
        update_available=update_available,
    )


@cli_fleet_router.get("/versions/summary")
async def versions_summary(current_user=Depends(require_admin)):
    """
    Admin — aggregate view: how many installs are on each version, when the
    latest heartbeat for that version landed, and how many distinct users
    are on it. Powers the "who needs the update" dashboard.
    """
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(
                    CliVersionRecord.version,
                    CliVersionRecord.channel,
                    func.count(CliVersionRecord.id).label("installs"),
                    func.count(func.distinct(CliVersionRecord.user_id)).label("users"),
                    func.max(CliVersionRecord.last_seen_at).label("last_seen"),
                )
                .group_by(CliVersionRecord.version, CliVersionRecord.channel)
                .order_by(func.max(CliVersionRecord.last_seen_at).desc())
            ).all()
    except Exception as exc:
        logger.error(f"[cli-fleet] versions_summary failed: {exc}")
        raise HTTPException(status_code=500, detail="version summary unavailable")

    published = {c: _read_channel_version(c) for c in _CHANNELS}
    return {
        "published":  published,
        "summary": [
            {
                "version":   r.version,
                "channel":   r.channel,
                "installs":  int(r.installs),
                "users":     int(r.users),
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                "is_latest": (published.get(r.channel) == r.version),
            }
            for r in rows
        ],
    }


@cli_fleet_router.get("/versions/users")
async def versions_users(
    version: Optional[str] = Query(default=None, description="Filter by exact version"),
    channel: Optional[str] = Query(default=None, description="Filter by channel"),
    limit:   int = Query(default=200, ge=1, le=2000),
    offset:  int = Query(default=0,   ge=0),
    current_user=Depends(require_admin),
):
    """
    Admin — paginated per-install list. Filter by version or channel to see
    exactly which engineers are on a stale build so you can DM/mail them
    (or trigger the update-available banner on their next heartbeat).
    """
    try:
        with SessionLocal() as db:
            q = select(
                CliVersionRecord.user_id,
                CliVersionRecord.email,
                CliVersionRecord.install_id,
                CliVersionRecord.version,
                CliVersionRecord.channel,
                CliVersionRecord.os,
                CliVersionRecord.arch,
                CliVersionRecord.runtime,
                CliVersionRecord.runtime_version,
                CliVersionRecord.session_count,
                CliVersionRecord.first_seen_at,
                CliVersionRecord.last_seen_at,
            ).order_by(CliVersionRecord.last_seen_at.desc())

            if version:
                q = q.where(CliVersionRecord.version == version)
            if channel:
                q = q.where(CliVersionRecord.channel == channel)

            q = q.limit(limit).offset(offset)
            rows = db.execute(q).all()

            count_q = select(func.count(CliVersionRecord.id))
            if version:
                count_q = count_q.where(CliVersionRecord.version == version)
            if channel:
                count_q = count_q.where(CliVersionRecord.channel == channel)
            total = int(db.execute(count_q).scalar() or 0)
    except Exception as exc:
        logger.error(f"[cli-fleet] versions_users failed: {exc}")
        raise HTTPException(status_code=500, detail="user list unavailable")

    return {
        "total":  total,
        "limit":  limit,
        "offset": offset,
        "rows": [
            {
                "user_id":         r.user_id,
                "email":           r.email,
                "install_id":      r.install_id,
                "version":         r.version,
                "channel":         r.channel,
                "os":              r.os,
                "arch":            r.arch,
                "runtime":         r.runtime,
                "runtime_version": r.runtime_version,
                "session_count":   int(r.session_count or 0),
                "first_seen_at":   r.first_seen_at.isoformat() if r.first_seen_at else None,
                "last_seen_at":    r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r in rows
        ],
    }


@cli_fleet_router.get("/versions/stale")
async def versions_stale(
    channel: str = Query(default="latest", description="Channel to compare against"),
    older_than_days: int = Query(
        default=0, ge=0, le=365,
        description="Only include installs whose last_seen_at is at least this many days ago (0 = any)",
    ),
    limit:  int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0,   ge=0),
    current_user=Depends(require_admin),
):
    """
    Admin — "who's on a stale build?" — every install whose reported version
    does NOT match the currently-published `latest` (or `stable`) file on
    this host, optionally filtered to installs that have also been seen
    recently enough to be a real target (older_than_days=0 = every install
    on the channel, regardless of recency).
    """
    latest = _read_channel_version(channel)
    if not latest:
        raise HTTPException(
            status_code=404,
            detail=f"channel '{channel}' has no published version file",
        )

    cutoff: Optional[datetime] = None
    if older_than_days > 0:
        cutoff = datetime.utcnow() - timedelta(days=older_than_days)

    try:
        with SessionLocal() as db:
            q = select(
                CliVersionRecord.user_id,
                CliVersionRecord.email,
                CliVersionRecord.install_id,
                CliVersionRecord.version,
                CliVersionRecord.os,
                CliVersionRecord.arch,
                CliVersionRecord.last_seen_at,
            ).where(
                CliVersionRecord.channel == channel,
                CliVersionRecord.version != latest,
            )
            if cutoff:
                q = q.where(CliVersionRecord.last_seen_at >= cutoff)
            q = q.order_by(CliVersionRecord.last_seen_at.desc()).limit(limit).offset(offset)
            rows = db.execute(q).all()
    except Exception as exc:
        logger.error(f"[cli-fleet] versions_stale failed: {exc}")
        raise HTTPException(status_code=500, detail="stale list unavailable")

    return {
        "channel":        channel,
        "latest_version": latest,
        "count":          len(rows),
        "rows": [
            {
                "user_id":      r.user_id,
                "email":        r.email,
                "install_id":   r.install_id,
                "version":      r.version,
                "os":           r.os,
                "arch":         r.arch,
                "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
            }
            for r in rows
        ],
    }
