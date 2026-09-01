# SPDX-License-Identifier: Apache-2.0
"""One-time cleanup: remove auto-injected ``code_executor`` from existing agents.

The pre-patch ``AgentAssembler.assemble()`` force-added ``code_executor`` to
every agent's tools array at creation time. The patched assembler now skips
that injection when the agent already has any purpose-built tool, but
agents created before the patch still carry the old entry in the DB.

This script:

  1. Loads every agent.
  2. Skips agents whose ONLY tools are ``code_executor`` / ``spawn_swarm``
     — those are the legitimate "blank" agents the new gate would still
     auto-inject for. Leaving their entry as-is is harmless.
  3. For agents that have BOTH ``code_executor`` AND at least one
     purpose-built tool, removes ``code_executor`` and writes back.
  4. Prints a per-agent report.

Usage::

    cd /d/ainxt-platform/ABStudio/backend
    python scripts/prune_auto_injected_code_executor.py --dry-run   # preview
    python scripts/prune_auto_injected_code_executor.py             # apply

The script is idempotent — running it twice on a clean DB is a no-op.

It opens its own Postgres connection (does NOT go through the FastAPI
backend's connection pool), so it can run while the backend is up or
down without conflicting with it. The ``AGENTCHAIN_POSTGRES_*`` env vars
must be set the same way the backend reads them — the script auto-loads
``../../.env`` (the platform-level .env that ``app/main.py`` also reads).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

def _cfg(key: str, default: str = "") -> str:
    """Read a configuration value from the environment.
    Generic accessor used for all connection parameters including credentials.
    """
    return os.environ.get(key, default)

# Make the standalone backend find the platform packages the same way
# app/main.py does at startup.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PLATFORM_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PLATFORM_ROOT / ".env", override=True)

import psycopg  # noqa: E402

_PLATFORM_UTILITY_TOOLS = {"code_executor", "spawn_swarm"}
_DSN_PASSWORD_RE = re.compile(r"password=\S+", re.IGNORECASE)


def _build_conn_params() -> dict:
    """Construct psycopg connection kwargs from AGENTCHAIN_POSTGRES_* env."""
    host = os.getenv("AGENTCHAIN_POSTGRES_HOST")
    if not host:
        raise SystemExit(
            "AGENTCHAIN_POSTGRES_HOST is not set in the environment.\n"
            "Check that the platform .env file is present at "
            f"{_PLATFORM_ROOT / '.env'} and contains the AGENTCHAIN_POSTGRES_* vars."
        )
    if not _cfg("AGENTCHAIN_POSTGRES_PASSWORD"):
        raise SystemExit(
            "AGENTCHAIN_POSTGRES_PASSWORD is not set in the environment.\n"
            "Check that the platform .env file is present at "
            f"{_PLATFORM_ROOT / '.env'} and contains the AGENTCHAIN_POSTGRES_* vars."
        )
    params = {
        "host":            host,
        "port":            os.getenv("AGENTCHAIN_POSTGRES_PORT", "5432"),
        "dbname":          os.getenv("AGENTCHAIN_POSTGRES_DB", "agent_chain"),
        "user":            os.getenv("AGENTCHAIN_POSTGRES_USER", "agent_chain"),
        "connect_timeout": "5",
    }
    params.update({"password": _cfg("AGENTCHAIN_POSTGRES_PASSWORD")})
    return params


def main(dry_run: bool) -> int:
    conn_params = _build_conn_params()
    print(f"connecting to postgres @ {conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}")
    print(f"mode: {'DRY RUN (no writes)' if dry_run else 'WRITE'}")
    print()

    updated = 0
    skipped_no_change = 0
    skipped_legit = 0

    with psycopg.connect(**conn_params) as conn:
        # Find the agents table — it may live under different schemas
        # in some installs, but the default is public.agents.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT to_regclass('public.agents') IS NOT NULL,
                       to_regclass('agents')        IS NOT NULL
            """)
            has_public, has_default = cur.fetchone()
            if not (has_public or has_default):
                raise SystemExit(
                    "Could not find an 'agents' table in this database. "
                    "Is the backend pointing at the right DB?"
                )

        with conn.cursor() as cur:
            cur.execute("SELECT id, name, owner_user_id, tools FROM agents")
            rows = cur.fetchall()

        print(f"scanned {len(rows)} agent rows")
        print()

        for agent_id, name, owner, tools in rows:
            # ``tools`` column comes back as a JSON-decoded list already
            # when psycopg sees a JSONB column. Be defensive in case the
            # row predates the column type migration.
            if isinstance(tools, str):
                try:
                    tools = json.loads(tools)
                except Exception:
                    continue
            if not isinstance(tools, list):
                continue
            names = [
                t.get("name") if isinstance(t, dict) else str(t)
                for t in tools
            ]
            name_set = set(n for n in names if n)
            if "code_executor" not in name_set:
                skipped_no_change += 1
                continue

            purpose_built = name_set - _PLATFORM_UTILITY_TOOLS
            if not purpose_built:
                # Blank agent — code_executor is its only capability.
                # Matches the new assembler gate's "blank agent" branch:
                # leave it alone.
                skipped_legit += 1
                continue

            new_tools = [
                t for t in tools
                if not (isinstance(t, dict) and t.get("name") == "code_executor")
                and not (isinstance(t, str) and t == "code_executor")
            ]
            print(
                f"  pruning code_executor from agent {name!r} "
                f"(id={agent_id}, owner={owner!r}) — "
                f"had {len(tools)} tools, now {len(new_tools)}"
            )

            if not dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE agents SET tools = %s::jsonb WHERE id = %s",
                        (json.dumps(new_tools), agent_id),
                    )
                conn.commit()
            updated += 1

    print()
    print("summary:")
    print(f"  pruned                          : {updated}")
    print(f"  left alone (no code_executor)   : {skipped_no_change}")
    print(f"  left alone (blank / utility-only): {skipped_legit}")
    if dry_run and updated:
        print()
        print("Re-run WITHOUT --dry-run to apply the changes.")
    return 0


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    raise SystemExit(main(dry))
