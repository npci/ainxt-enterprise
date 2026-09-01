#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CONNECTOR TOKEN REFRESHER — keeps OAuth connections alive
#
# WHY THIS EXISTS
# ---------------
# Connector access tokens are short-lived (Microsoft Entra ≈ 1 hour). Until now
# the ONLY refresh trigger was lazy: connectors/engine._get_token_row refreshed a
# token when a tool call happened to find it near expiry. That has two
# consequences users actually felt:
#
#   1. A scheduled Cowork task that fires at 21:00, hours after the user last
#      touched the platform, was the FIRST thing to discover a stale token. Any
#      hiccup in that refresh surfaced as "please connect the M365 connector"
#      and the task silently failed.
#   2. Refresh breakage (wrong authority, rotated client secret, relay/egress
#      down) stayed invisible until a user or a scheduled job tripped over it.
#
# This process refreshes tokens BEFORE anything needs them, so a scheduled run
# always finds a valid access token, and refresh problems show up in the logs
# within minutes instead of at 21:00.
#
# It deliberately reuses connector_engine._refresh_token, so all the careful
# error discrimination added alongside this worker applies unchanged:
#   * ConnectorReauthRequired  → the grant is genuinely gone; the engine
#                                deactivates the token and the user is notified.
#   * ConnectorTransientError  → server/network/config problem; the token is
#                                LEFT ACTIVE and retried on the next sweep.
#
# Idempotent and safe to run alongside the lazy path: refreshing twice is
# harmless because providers accept a valid refresh token repeatedly, and
# _update_token uses COALESCE so a rotated refresh token is never lost.
#
# Run (PM2-managed in prod; never systemd):
#   python workers/connector_token_refresher.py
#
# Required env:
#   POSTGRES_* (platform DB), FERNET_KEY (must MATCH the gateway's),
#   AZURE_AD_TENANT_ID (single-tenant M365 apps), LLM_PROXY_URL (if this host
#   has no direct internet egress).
#
# Optional env:
#   CONNECTOR_REFRESH_INTERVAL_S   default 1800 (30 min) — sweep cadence
#   CONNECTOR_REFRESH_LOOKAHEAD_S  default 5400 (90 min) — refresh tokens
#                                  expiring within this window
#   CONNECTOR_REFRESH_BATCH        default 200 — max rows per sweep
#   CONNECTOR_REFRESH_ONCE         set to 1 to run a single sweep and exit
#                                  (useful for cron-style deploys and testing)
# ============================================================

from __future__ import annotations

import os
import signal
import sys
import time

# Add project root to path so imports work when launched directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before any project imports so os.getenv() sees correct values.
try:
    from dotenv import load_dotenv
    load_dotenv(
        dotenv_path=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
        ),
        override=True,
    )
except ImportError:
    pass

from core.logger import logger

_INTERVAL_S   = int(os.getenv("CONNECTOR_REFRESH_INTERVAL_S", "1800"))
_LOOKAHEAD_S  = int(os.getenv("CONNECTOR_REFRESH_LOOKAHEAD_S", "5400"))
_BATCH        = int(os.getenv("CONNECTOR_REFRESH_BATCH", "200"))

# NOTE: auth models with nothing to refresh (pat / api_key / dpi_consent) are
# excluded in SQL inside _due_rows() — PATs and API keys don't expire, and DPI
# consent artifacts are re-granted rather than refreshed.


def _due_rows() -> list[tuple[str, str]]:
    """Return [(user_id, connector_name)] for active OAuth tokens expiring within
    the lookahead window.

    Only rows that HAVE a refresh_token are considered — without one, refreshing
    is impossible and the user must reconnect (the lazy path already reports
    that clearly, so there's nothing useful to do here).
    """
    from db.database import SessionLocal
    import sqlalchemy as sa

    db = SessionLocal()
    try:
        rows = db.execute(
            sa.text(
                """
                SELECT t.user_id, t.connector_name
                  FROM ainxt.user_oauth_tokens t
                  LEFT JOIN ainxt.connector_definitions d
                         ON d.name = t.connector_name
                 WHERE t.is_active = TRUE
                   AND t.refresh_token IS NOT NULL
                   AND t.expires_at IS NOT NULL
                   AND t.expires_at < (NOW() + make_interval(secs => :lookahead))
                   AND COALESCE(d.auth_type, 'oauth2') NOT IN (
                       'pat', 'api_key', 'dpi_consent'
                   )
                 ORDER BY t.expires_at ASC
                 LIMIT :lim
                """
            ),
            {"lookahead": _LOOKAHEAD_S, "lim": _BATCH},
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        db.close()


def refresh_due_tokens() -> dict:
    """Run ONE sweep. Returns {checked, refreshed, reauth, transient}.

    Never raises: this runs unattended, and one bad row must not stop the sweep.
    """
    from connectors.base import ConnectorReauthRequired, ConnectorTransientError
    from connectors.engine import connector_engine

    stats = {"checked": 0, "refreshed": 0, "reauth": 0, "transient": 0}

    try:
        rows = _due_rows()
    except Exception as exc:
        logger.error(f"connector_token_refresher: could not query due tokens: {exc}")
        return stats

    if not rows:
        logger.debug("connector_token_refresher: no tokens due for refresh")
        return stats

    logger.info(f"connector_token_refresher: {len(rows)} token(s) due for refresh")

    for user_id, connector_name in rows:
        stats["checked"] += 1
        try:
            enc_refresh = _read_encrypted_refresh_token(user_id, connector_name)
            if not enc_refresh:
                continue

            # Reuses the engine's refresh + persistence + error classification.
            connector_engine._refresh_token(user_id, connector_name, enc_refresh)
            stats["refreshed"] += 1
            # Never log tokens; user_id is an opaque subject id.
            logger.info(
                f"connector_token_refresher: refreshed {connector_name} "
                f"for user={user_id}"
            )

        except ConnectorReauthRequired:
            # The grant is genuinely gone. The engine has already deactivated the
            # token; tell the user so they can reconnect BEFORE their next
            # scheduled run fails, instead of discovering it afterwards.
            stats["reauth"] += 1
            logger.warning(
                f"connector_token_refresher: {connector_name} for user={user_id} "
                f"needs re-authorisation — notifying the user"
            )
            _notify_reauth_needed(user_id, connector_name)

        except ConnectorTransientError as exc:
            # Server/network/config problem. The token was left ACTIVE on purpose;
            # the next sweep retries. Logged at ERROR so ops see it immediately
            # rather than at 21:00 via a failed scheduled task.
            stats["transient"] += 1
            logger.error(
                f"connector_token_refresher: transient failure refreshing "
                f"{connector_name} for user={user_id} (token left active): {exc}"
            )

        except Exception as exc:
            stats["transient"] += 1
            logger.error(
                f"connector_token_refresher: unexpected error refreshing "
                f"{connector_name} for user={user_id}: "
                f"{type(exc).__name__}: {exc}"
            )

    logger.info(
        f"connector_token_refresher: sweep done — checked={stats['checked']} "
        f"refreshed={stats['refreshed']} reauth={stats['reauth']} "
        f"transient={stats['transient']}"
    )
    return stats


def _read_encrypted_refresh_token(user_id: str, connector_name: str) -> str | None:
    """Fetch the STILL-ENCRYPTED refresh token. Decryption happens inside
    engine._refresh_token, which already reports a FERNET_KEY mismatch precisely,
    so we deliberately don't decrypt here."""
    from db.database import SessionLocal
    import sqlalchemy as sa

    db = SessionLocal()
    try:
        row = db.execute(
            sa.text(
                "SELECT refresh_token FROM ainxt.user_oauth_tokens "
                "WHERE user_id = :uid AND connector_name = :cn AND is_active = TRUE"
            ),
            {"uid": user_id, "cn": connector_name},
        ).fetchone()
        return row[0] if row and row[0] else None
    except Exception as exc:
        logger.warning(
            f"connector_token_refresher: could not read refresh token for "
            f"{connector_name}/user={user_id}: {type(exc).__name__}"
        )
        return None
    finally:
        db.close()


def _notify_reauth_needed(user_id: str, connector_name: str) -> None:
    """Put an inbox item in front of the user so they reconnect BEFORE their next
    scheduled task fails. Best-effort — never breaks the sweep."""
    try:
        from store.inbox_store import publish_inbox_item

        publish_inbox_item(
            str(user_id),
            "connector_reauth",
            f"Reconnect required: {connector_name}",
            (
                f"Your {connector_name} connection has expired and could not be "
                f"renewed automatically, so scheduled tasks that use it will fail "
                f"until it is reconnected.\n\n"
                f"Go to Settings → Connectors and connect {connector_name} again."
            ),
            source_id=f"reauth:{connector_name}",
            metadata={"kind": "connector_reauth", "connector": connector_name},
        )
    except Exception as exc:
        logger.warning(
            f"connector_token_refresher: could not notify user={user_id} "
            f"about {connector_name}: {type(exc).__name__}"
        )


# ── Main loop ─────────────────────────────────────────────────

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    _stop = True
    logger.info(f"connector_token_refresher: signal {signum} received — stopping")


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # Fail loudly if the vault key is missing: without it this process cannot
    # decrypt any stored token, so every sweep would be a no-op that silently
    # logs failures forever.
    if not (os.getenv("FERNET_KEY") or os.getenv("VAULT_ENCRYPTION_KEY") or "").strip():
        logger.error(
            "connector_token_refresher: no FERNET_KEY/VAULT_ENCRYPTION_KEY set — "
            "this process cannot decrypt stored tokens and every refresh will "
            "fail. Set the SAME FERNET_KEY as the gateway."
        )

    once = (os.getenv("CONNECTOR_REFRESH_ONCE", "").strip().lower()
            in ("1", "true", "yes", "on"))

    if once:
        refresh_due_tokens()
        return 0

    logger.info(
        f"connector_token_refresher: starting — interval={_INTERVAL_S}s "
        f"lookahead={_LOOKAHEAD_S}s batch={_BATCH}"
    )

    # Sweep immediately on boot: after a deploy or restart there may already be
    # tokens near expiry, and waiting a full interval risks a scheduled task
    # firing first.
    while not _stop:
        try:
            refresh_due_tokens()
        except Exception as exc:
            logger.error(f"connector_token_refresher: sweep crashed: {exc}")

        # Sleep in short slices so SIGTERM is honoured promptly.
        slept = 0
        while slept < _INTERVAL_S and not _stop:
            time.sleep(min(5, _INTERVAL_S - slept))
            slept += 5

    logger.info("connector_token_refresher: stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
