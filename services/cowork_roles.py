#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# COWORK ROLE SPECIALISTS — "role packs"
#
# A Cowork "role" bundles, in one named unit:
#   - a system prompt (the persona / operating instructions)
#   - a set of allowed connectors (connector slugs, e.g. "microsoft_365",
#     or fully-qualified connector tool names, e.g. "microsoft_365__outlook_search_emails")
#   - a set of Skills (by name, resolved against skills_pg / SkillRecord)
#   - a sub-agent allowlist (which agentTypes this role may spawn)
#   - department + visibility for ABAC scoping
#
# This mirrors Claude Cowork's "bundle Skills + connectors + sub-agents into a
# role" concept. A role is persisted in the `cowork_roles` Postgres table and
# can be *materialized* into a Cowork session directory as:
#   (a) an agent .md file with YAML frontmatter that the ainxt-cli agent loader
#       (ainxt-cli/src/tools/agent/loadAgentsDir.ts) understands, and
#   (b) a scoped managed-mcp.json that restricts the connector MCP to only the
#       connector tools this role is allowed to touch.
#
# AiNxt GUARDRAILS (non-negotiable):
#   - Reads are REDACTED, never blocked (the connector MCP bridge already
#     redacts outbound text via connectors/mcp_bridge._redact_output).
#   - Connector / doc WRITES never auto-execute from a materialized role — they
#     must go through the existing confirm + compliance-gated path
#     (POST /connectors/action, workers/doc_worker.py). This module only ever
#     materializes *configuration*; it never sends anything.
#   - Never log secrets/tokens. The managed-mcp.json carries NO bearer token —
#     auth flows from the user's JWT at connection time, exactly like
#     routers/cowork_mcp_router.py.
# ============================================================

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from core.config import PLATFORM_BASE_URL as _CONFIG_PLATFORM_BASE_URL
from core.logger import logger


# ============================================================
# DB HELPERS  (mirrors routers/profile_router.py _db() pattern)
# ============================================================

def _db():
    """Return (engine, text) — lazy import so this module is import-clean even
    when the DB layer is not yet initialised (e.g. CLI tooling)."""
    from db.database import engine
    from sqlalchemy import text
    return engine, text


# ============================================================
# MODEL
# ============================================================

@dataclass
class CoworkRole:
    """A Cowork role specialist pack.

    allowed_connectors entries may be either:
      - a bare connector slug ("microsoft_365") → all of that connector's tools, OR
      - a fully-qualified connector tool name ("microsoft_365__outlook_search_emails")
        using the double-underscore convention from connectors/mcp_bridge.py.
    """
    name: str
    system_prompt: str
    id: Optional[str] = None
    description: str = ""
    allowed_connectors: List[str] = field(default_factory=list)
    skill_names: List[str] = field(default_factory=list)
    subagent_allowlist: List[str] = field(default_factory=list)
    department: str = ""
    visibility: str = "private"  # "public" | "private"
    created_by: str = ""
    status: str = "DRAFT"        # "DRAFT" | "PUBLISHED" — marketplace governance
    published_at: Optional[str] = None
    published_by: str = ""

    # ── serialization ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "system_prompt": self.system_prompt or "",
            "allowed_connectors": list(self.allowed_connectors or []),
            "skill_names": list(self.skill_names or []),
            "subagent_allowlist": list(self.subagent_allowlist or []),
            "department": self.department or "",
            "visibility": self.visibility or "private",
            "created_by": self.created_by or "",
            "status": self.status or "DRAFT",
            "published": (self.status or "DRAFT") == "APPROVED" and (self.visibility or "") == "public",
            "published_at": self.published_at,
            "published_by": self.published_by or "",
        }

    @staticmethod
    def from_row(row) -> "CoworkRole":
        """Build from a SQLAlchemy Row. Column order matches _SELECT_COLS."""
        return CoworkRole(
            id=str(row[0]),
            name=row[1],
            description=row[2] or "",
            system_prompt=row[3] or "",
            allowed_connectors=_as_list(row[4]),
            skill_names=_as_list(row[5]),
            subagent_allowlist=_as_list(row[6]),
            department=row[7] or "",
            visibility=row[8] or "private",
            created_by=row[9] or "",
            status=(row[10] if len(row) > 10 else "DRAFT") or "DRAFT",
            published_at=(str(row[11]) if len(row) > 11 and row[11] else None),
            published_by=(row[12] if len(row) > 12 else "") or "",
        )


def _as_list(v) -> List[str]:
    """JSONB columns come back as Python lists already; tolerate str/None."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


_SELECT_COLS = (
    "id, name, description, system_prompt, allowed_connectors, "
    "skill_names, subagent_allowlist, department, visibility, created_by, "
    "status, published_at, published_by"
)


# ============================================================
# DDL  (returned in the structured result for the orchestrator to wire in)
# ============================================================

COWORK_ROLES_DDL = """
CREATE TABLE IF NOT EXISTS cowork_roles (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(255) NOT NULL,
    description         TEXT,
    system_prompt       TEXT NOT NULL,
    allowed_connectors  JSONB NOT NULL DEFAULT '[]'::jsonb,
    skill_names         JSONB NOT NULL DEFAULT '[]'::jsonb,
    subagent_allowlist  JSONB NOT NULL DEFAULT '[]'::jsonb,
    department          VARCHAR(255),
    visibility          VARCHAR(10) NOT NULL DEFAULT 'private',
    created_by          VARCHAR(255),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_cowork_roles_name_dept UNIQUE (name, department)
);
CREATE INDEX IF NOT EXISTS ix_cowork_roles_dept ON cowork_roles (department);
CREATE INDEX IF NOT EXISTS ix_cowork_roles_created_by ON cowork_roles (created_by);
""".strip()


# ============================================================
# CRUD
# ============================================================

def create_role(role: CoworkRole) -> CoworkRole:
    """Insert a new role. Returns the role with its generated id populated."""
    engine, text = _db()
    new_id = role.id or str(uuid.uuid4())
    params = {
        "id": new_id,
        "name": role.name,
        "description": role.description or None,
        "system_prompt": role.system_prompt or "",
        "allowed_connectors": json.dumps(list(role.allowed_connectors or [])),
        "skill_names": json.dumps(list(role.skill_names or [])),
        "subagent_allowlist": json.dumps(list(role.subagent_allowlist or [])),
        "department": role.department or None,
        "visibility": role.visibility or "private",
        "created_by": role.created_by or None,
    }
    with engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO cowork_roles
                    (id, name, description, system_prompt, allowed_connectors,
                     skill_names, subagent_allowlist, department, visibility, created_by)
                VALUES
                    (:id, :name, :description, :system_prompt,
                     CAST(:allowed_connectors AS jsonb),
                     CAST(:skill_names AS jsonb),
                     CAST(:subagent_allowlist AS jsonb),
                     :department, :visibility, :created_by)
            """),
            params,
        )
        conn.commit()
    role.id = new_id
    logger.info(f"cowork_roles: created role '{role.name}' (id={new_id})")
    return role


def get_role(role_id: str) -> Optional[CoworkRole]:
    engine, text = _db()
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles WHERE id = :id"),
            {"id": role_id},
        ).fetchone()
    return CoworkRole.from_row(row) if row else None


def get_role_by_name(name: str, department: str = "") -> Optional[CoworkRole]:
    engine, text = _db()
    with engine.connect() as conn:
        row = conn.execute(
            text(f"""
                SELECT {_SELECT_COLS} FROM cowork_roles
                WHERE name = :name AND COALESCE(department, '') = :dept
                LIMIT 1
            """),
            {"name": name, "dept": department or ""},
        ).fetchone()
    return CoworkRole.from_row(row) if row else None


def list_roles(department: str = "", visibility: Optional[str] = None,
               include_public: bool = True) -> List[CoworkRole]:
    """List roles visible to a department. Public roles are included by default."""
    engine, text = _db()
    clauses, params = [], {}
    if visibility:
        clauses.append("visibility = :visibility")
        params["visibility"] = visibility
    if department:
        if include_public:
            clauses.append("(COALESCE(department, '') = :dept OR visibility = 'public')")
        else:
            clauses.append("COALESCE(department, '') = :dept")
        params["dept"] = department
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles{where} ORDER BY name"),
            params,
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def list_owned(user_id: str) -> List["CoworkRole"]:
    """Roles created by this user — their PRIVATE specialists (any status). Used
    for the self-service management list + the picker's 'my roles' portion."""
    if not user_id:
        return []
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles WHERE created_by = :uid ORDER BY name"),
            {"uid": str(user_id)},
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def list_published_roles() -> List["CoworkRole"]:
    """All PUBLISHED (org-wide) roles — the admin-vetted marketplace. These are the
    only roles a user who isn't the owner may see/select."""
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles WHERE status = 'PUBLISHED' ORDER BY name"),
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def list_for_picker(user_id: str, department: str = "") -> List["CoworkRole"]:
    """Roles a user may SELECT in the Cowork picker — the 3-tier visibility model.
    NOTE: 'private' = DEPARTMENT-scoped (matches the platform-wide skills/agents
    convention). The extra 'personal' tier (just the owner) is Cowork-specific —
    you build your own assistant — and is covered by the created_by clause.
      - PUBLIC (org-wide): status='PUBLISHED' (admin-published) — everyone.
      - PRIVATE (department): visibility='private' AND same department — self-serve.
      - PERSONAL / your own: created_by == the caller — any tier/status.
    """
    # private/public are gated by governance (status='APPROVED'); personal/own are
    # always visible to the owner regardless of status (created_by).
    engine, text = _db()
    params = {"uid": str(user_id or ""), "dept": department or ""}
    where = (
        "(visibility = 'public' AND status = 'APPROVED') "
        "OR (visibility = 'private' AND status = 'APPROVED' AND COALESCE(department, '') = :dept) "
        "OR created_by = :uid"
    )
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles WHERE {where} ORDER BY name"),
            params,
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def list_pending() -> List["CoworkRole"]:
    """Roles awaiting approval (status='PENDING_APPROVAL') — the approver queue."""
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles WHERE status = 'PENDING_APPROVAL' ORDER BY name"),
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def set_role_status(role_id: str, status: str, approver_email: str = "") -> Optional["CoworkRole"]:
    """Set a role's governance status (DRAFT|PENDING_APPROVAL|APPROVED|REJECTED).
    On APPROVED, stamp published_at/by for audit. Single source of truth for the
    Cowork role approval lifecycle (mirrors KB doc approval)."""
    engine, text = _db()
    with engine.begin() as conn:
        if status == "APPROVED":
            conn.execute(
                text("UPDATE cowork_roles SET status='APPROVED', published_at=NOW(), "
                     "published_by=:by, updated_at=NOW() WHERE id=:id"),
                {"id": role_id, "by": approver_email or None},
            )
        else:
            conn.execute(
                text("UPDATE cowork_roles SET status=:st, updated_at=NOW() WHERE id=:id"),
                {"id": role_id, "st": status},
            )
    return get_role(role_id)


def list_all_roles() -> List["CoworkRole"]:
    """Every role (admin management view)."""
    engine, text = _db()
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT {_SELECT_COLS} FROM cowork_roles ORDER BY name"),
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def update_role(role_id: str, **fields) -> Optional[CoworkRole]:
    """Patch mutable fields. JSONB list fields are re-serialized automatically."""
    if not fields:
        return get_role(role_id)
    _ALLOWED = {
        "name", "description", "system_prompt", "allowed_connectors",
        "skill_names", "subagent_allowlist", "department", "visibility",
    }
    _JSON_COLS = {"allowed_connectors", "skill_names", "subagent_allowlist"}
    sets, params = [], {"id": role_id}
    for key, val in fields.items():
        if key not in _ALLOWED:
            continue
        if key in _JSON_COLS:
            sets.append(f"{key} = CAST(:{key} AS jsonb)")
            params[key] = json.dumps(list(val or []))
        else:
            sets.append(f"{key} = :{key}")
            params[key] = val
    if not sets:
        return get_role(role_id)
    sets.append("updated_at = NOW()")
    engine, text = _db()
    with engine.connect() as conn:
        conn.execute(
            text(f"UPDATE cowork_roles SET {', '.join(sets)} WHERE id = :id"),
            params,
        )
        conn.commit()
    logger.info(f"cowork_roles: updated role id={role_id} fields={list(fields)}")
    return get_role(role_id)


def publish_role(role_id: str, published_by: str = "") -> Optional[CoworkRole]:
    """Publish a role/plugin to the org marketplace: status→PUBLISHED, visibility→
    public, stamp published_at/by. This is the governance gate — only published
    roles appear in the shared marketplace; drafts stay private to the author/dept."""
    engine, text = _db()
    with engine.connect() as conn:
        conn.execute(
            text("""
                UPDATE cowork_roles
                   SET status = 'APPROVED', visibility = 'public',
                       published_at = NOW(), published_by = :by, updated_at = NOW()
                 WHERE id = :id
            """),
            {"id": role_id, "by": published_by or None},
        )
        conn.commit()
    logger.info(f"cowork_roles: PUBLISHED role id={role_id} by {published_by}")
    return get_role(role_id)


def unpublish_role(role_id: str) -> Optional[CoworkRole]:
    """Withdraw a role from the marketplace: status→DRAFT, visibility→private.
    Existing sessions keep their materialized copy; new discovery is removed."""
    engine, text = _db()
    with engine.connect() as conn:
        conn.execute(
            text("""
                UPDATE cowork_roles
                   SET status = 'DRAFT', visibility = 'private',
                       published_at = NULL, published_by = NULL, updated_at = NOW()
                 WHERE id = :id
            """),
            {"id": role_id},
        )
        conn.commit()
    logger.info(f"cowork_roles: UNPUBLISHED role id={role_id}")
    return get_role(role_id)


def list_marketplace(department: str = "") -> List[CoworkRole]:
    """Roles available in the marketplace: anything PUBLISHED (org-wide) plus the
    caller's own department drafts (so a dept sees its in-progress packs too)."""
    engine, text = _db()
    params = {"dept": department or ""}
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT {_SELECT_COLS} FROM cowork_roles
                 WHERE status = 'PUBLISHED'
                    OR COALESCE(department, '') = :dept
                 ORDER BY (status = 'PUBLISHED') DESC, name
            """),
            params,
        ).fetchall()
    return [CoworkRole.from_row(r) for r in rows]


def delete_role(role_id: str) -> bool:
    engine, text = _db()
    with engine.connect() as conn:
        res = conn.execute(
            text("DELETE FROM cowork_roles WHERE id = :id"),
            {"id": role_id},
        )
        conn.commit()
    deleted = (res.rowcount or 0) > 0
    if deleted:
        logger.info(f"cowork_roles: deleted role id={role_id}")
    return deleted


# ============================================================
# SKILL RESOLUTION  (skills are stored in skills_pg / SkillRecord)
# ============================================================

def resolve_skill_names(skill_names: List[str], department: str = "") -> List[str]:
    """Return the subset of skill_names that exist as usable (APPROVED/PRODUCTION)
    SkillRecords. Unknown or non-production skills are dropped (and logged) so a
    materialized role never references a Skill the runtime can't load.

    Reuses db.database.SessionLocal + db.models.SkillRecord, exactly like
    routers/skills_router.py."""
    if not skill_names:
        return []
    from db.database import SessionLocal
    from db.models import SkillRecord

    wanted = list(dict.fromkeys(s for s in skill_names if s))  # dedupe, keep order
    db = SessionLocal()
    try:
        rows = (
            db.query(SkillRecord)
            .filter(SkillRecord.name.in_(wanted))
            .filter(SkillRecord.status.in_(["APPROVED", "PRODUCTION"]))
            .all()
        )
        found = {r.name for r in rows}
    finally:
        db.close()

    missing = [s for s in wanted if s not in found]
    if missing:
        logger.warning(f"cowork_roles: dropping unknown/non-production skills {missing}")
    # Preserve the caller's ordering.
    return [s for s in wanted if s in found]


def _resolve_skill_records(skill_names: List[str]):
    """Return the usable (APPROVED/PRODUCTION) SkillRecord rows for these names,
    in the caller's order. Used to render a role's bundled skills into its
    operating context."""
    if not skill_names:
        return []
    from db.database import SessionLocal
    from db.models import SkillRecord

    wanted = list(dict.fromkeys(s for s in skill_names if s))
    db = SessionLocal()
    try:
        rows = (
            db.query(SkillRecord)
            .filter(SkillRecord.name.in_(wanted))
            .filter(SkillRecord.status.in_(["APPROVED", "PRODUCTION"]))
            .all()
        )
    finally:
        db.close()
    by_name = {r.name: r for r in rows}
    return [by_name[n] for n in wanted if n in by_name]


def build_role_context(role_id: str, department: str = "") -> str:
    """Render a role specialist's full operating context for injection into a
    Cowork session: the specialist system prompt PLUS its bundled Skills.

    - BEHAVIORAL skills (plain-text SOPs, stored in `code`) are injected verbatim —
      the office agent has no code sandbox, so these are pure instructions it must
      follow. This is the office-relevant skill type.
    - EXECUTION skills (Python `run()`) are only *named* with their description —
      the Cowork office surface has no Bash/code tools, so we never claim it can run
      them; we just tell the agent the capability exists (so it can ask the platform
      or route to Code). Most office roles carry no execution skills.

    Returns "" if the role is unknown. The result REPLACES the bare role.system_prompt
    in the session's [ROLE] block. DB is the source of truth — never hardcode role
    prompts/skills in the client.
    """
    role = get_role(role_id)
    if not role:
        return ""
    lines: List[str] = []
    sp = (role.system_prompt or "").strip()
    if sp:
        lines.append(sp)

    records = _resolve_skill_records(list(role.skill_names or []))
    behavioral = [r for r in records if (getattr(r, "skill_type", "execution") or "execution") == "behavioral"]
    execution = [r for r in records if (getattr(r, "skill_type", "execution") or "execution") != "behavioral"]

    if behavioral:
        lines.append("")
        lines.append("## Role Skills — standard operating procedures you MUST follow")
        for r in behavioral:
            text = (r.code or "").strip() or (r.description or "").strip()
            if not text:
                continue
            lines.append(f"\n### Skill: {r.name}")
            if r.description:
                lines.append(f"_{r.description.strip()}_")
            lines.append(text)

    if execution:
        names = ", ".join(r.name for r in execution)
        lines.append("")
        lines.append(
            f"## Available specialist capabilities (executable skills): {names}. "
            "These run code and are NOT directly runnable from this office surface — "
            "if the user needs one, explain it and route via the platform; never claim "
            "you executed it here."
        )

    return "\n".join(lines).strip()


# ============================================================
# CONNECTOR ALLOWLIST NORMALIZATION
# ============================================================

def _split_connectors(allowed_connectors: List[str]) -> tuple[set, set]:
    """Split allowed_connectors into:
      - connector slugs allowed wholesale (e.g. {"microsoft_365"})
      - explicit connector tool names (e.g. {"microsoft_365__outlook_search_emails"})
    Tool names use the double-underscore convention from connectors/mcp_bridge.py.
    """
    slugs, tool_names = set(), set()
    for entry in allowed_connectors or []:
        e = (entry or "").strip()
        if not e:
            continue
        if "__" in e:
            tool_names.add(e)
            slugs.add(e.split("__", 1)[0])  # parent slug is implicitly allowed
        else:
            slugs.add(e)
    return slugs, tool_names


def _expand_allowed_tool_names(role: CoworkRole) -> List[str]:
    """Resolve the role's allowed_connectors into a concrete list of
    `connector__tool` names by consulting the connector registry. A bare slug
    expands to all of that connector's tool names; an explicit tool name passes
    through. Falls back to the raw entries if the registry is unavailable."""
    slugs, explicit = _split_connectors(role.allowed_connectors)
    resolved: List[str] = list(explicit)
    try:
        from connectors.registry import connector_registry
        # get_available() → [{"name": slug, "tools": [{"name": ...}, ...]}, ...]
        by_slug = {d["name"]: d for d in (connector_registry.get_available() or [])}
        for slug in slugs:
            # Only expand a slug wholesale when no explicit tool subset was given
            # for it; otherwise the explicit subset already scopes it.
            if any(t.startswith(f"{slug}__") for t in explicit):
                continue
            defn = by_slug.get(slug)
            for t in ((defn or {}).get("tools") or []):
                tname = t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
                if tname:
                    resolved.append(f"{slug}__{tname}")
    except Exception as e:
        logger.debug(f"cowork_roles: connector registry expand skipped → {e}")
        # Fall back to whatever was explicitly listed (slugs alone can't be
        # turned into tool names without the registry).
    # Dedupe, preserve order.
    return list(dict.fromkeys(resolved))


# ============================================================
# MATERIALIZATION
# ============================================================

# These are the ainxt-cli connector-MCP server defaults. The materialized
# managed-mcp.json points the agent at the gateway's per-user Buddy MCP SSE
# endpoint (routers/cowork_mcp_router.py → /buddy/mcp/sse). Auth is the user's
# JWT supplied by the CLI at connect time — NO token is written into the file.
_COWORK_MCP_SERVER_NAME = "ainxt-connectors"
# No localhost default: reuses the canonical (also no-default) core.config value
# so this module never disagrees with the rest of the platform about "unset".
_DEFAULT_GATEWAY = os.getenv("PLATFORM_BASE_URL", _CONFIG_PLATFORM_BASE_URL).rstrip("/")
_COWORK_MCP_SSE_PATH = "/buddy/mcp/sse"


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "role").strip().lower())
    return re.sub(r"-+", "-", s).strip("-") or "role"


def _yaml_escape(value: str) -> str:
    """Single-line YAML scalar: quote and escape newlines so the CLI's
    frontmatter parser (which unescapes \\n in `description`) round-trips it."""
    v = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    v = v.replace("\n", "\\n").replace("\r", "")
    return f'"{v}"'


def _yaml_list(items: List[str]) -> str:
    """Inline YAML flow sequence, e.g. [a, b, c]. Quotes each item."""
    safe = [f'"{str(i).strip()}"' for i in (items or []) if str(i).strip()]
    return "[" + ", ".join(safe) + "]"


def build_agent_markdown(role: CoworkRole, allowed_tool_names: List[str],
                         resolved_skills: List[str]) -> str:
    """Render the agent .md (YAML frontmatter + system prompt body).

    Frontmatter keys match what ainxt-cli/src/tools/agent/loadAgentsDir.ts reads:
      - name        → parsed into `agentType`
      - description → parsed into `whenToUse`
      - tools       → parseAgentToolsFromFrontmatter (the connector__tool allowlist
                      + sub-agent Task tools — this is the CLIENT-SIDE scope gate)
      - skills      → parseSlashCommandToolsFromFrontmatter (comma-separated)
      - mcpServers  → array of server-name references (we reference the scoped
                      managed-mcp.json server by name)
    """
    agent_type = _slugify(role.name)
    when_to_use = (
        role.description
        or f"Cowork role specialist: {role.name}. Spawn for tasks matching this role's remit."
    )

    # The agent's tool allowlist = the connector tools it may call + the Task
    # tool gated to the sub-agent allowlist. We list the connector tool names so
    # the CLI enforces the connector scope even before the MCP server filters.
    tools: List[str] = list(allowed_tool_names)
    for sub in (role.subagent_allowlist or []):
        sub = str(sub).strip()
        if sub:
            # Task(agentType) scoping convention used by the CLI agent loader.
            tools.append(f"Task({sub})")

    lines: List[str] = ["---"]
    lines.append(f"name: {agent_type}")
    lines.append(f"description: {_yaml_escape(when_to_use)}")
    if tools:
        lines.append(f"tools: {_yaml_list(tools)}")
    if resolved_skills:
        # CLI parses skills as comma-separated string OR flow list; use flow list.
        lines.append(f"skills: {_yaml_list(resolved_skills)}")
    lines.append(f"mcpServers: {_yaml_list([_COWORK_MCP_SERVER_NAME])}")
    if role.department:
        lines.append(f"# department: {role.department}")
    if role.visibility:
        lines.append(f"# visibility: {role.visibility}")
    lines.append("---")
    lines.append("")
    lines.append(role.system_prompt.strip())
    lines.append("")
    return "\n".join(lines)


def build_managed_mcp(allowed_tool_names: List[str],
                      gateway_base: Optional[str] = None) -> dict:
    """Build a scoped managed-mcp.json structure.

    Shape matches the ainxt-cli McpJsonConfig: {"mcpServers": {name: config}}.
    The single server is the per-user cowork connector bridge over HTTP/SSE. It
    carries NO bearer token (the CLI attaches the user JWT). The allowlist is
    passed as a header so the gateway-side bridge can further restrict tools to
    exactly this role's connector scope (defence in depth alongside the agent
    `tools` allowlist). Writes still never auto-execute — the bridge proposes
    write actions for confirmation via POST /connectors/action.
    """
    base = (gateway_base or _DEFAULT_GATEWAY).rstrip("/")
    return {
        "mcpServers": {
            _COWORK_MCP_SERVER_NAME: {
                "type": "sse",
                "url": f"{base}{_COWORK_MCP_SSE_PATH}",
                "headers": {
                    # Gateway-side scope hint. The cowork MCP router/bridge may
                    # intersect its per-user tool list with this allowlist.
                    # Empty list ⇒ no connector tools (KB search only).
                    "x-cowork-allowed-tools": ",".join(allowed_tool_names),
                },
            }
        }
    }


def materialize_role(role: CoworkRole, session_dir: str,
                     gateway_base: Optional[str] = None) -> dict:
    """Write a role's agent .md + scoped managed-mcp.json into a Cowork session
    directory.

    Layout (matches ainxt-cli expectations):
      {session_dir}/agents/{agent_type}.md
      {session_dir}/.ainxt/managed-mcp.json

    Returns the paths written + the resolved scope (for audit / response). Never
    writes any token/secret. Never executes any connector or doc action — this
    is configuration only; writes flow through the confirm + compliance-gated
    path at runtime.
    """
    agent_type = _slugify(role.name)
    allowed_tool_names = _expand_allowed_tool_names(role)
    resolved_skills = resolve_skill_names(role.skill_names, role.department)

    agents_dir = os.path.join(session_dir, "agents")
    ainxt_dir = os.path.join(session_dir, ".ainxt")
    os.makedirs(agents_dir, exist_ok=True)
    os.makedirs(ainxt_dir, exist_ok=True)

    agent_path = os.path.join(agents_dir, f"{agent_type}.md")
    mcp_path = os.path.join(ainxt_dir, "managed-mcp.json")

    agent_md = build_agent_markdown(role, allowed_tool_names, resolved_skills)
    managed_mcp = build_managed_mcp(allowed_tool_names, gateway_base)

    with open(agent_path, "w", encoding="utf-8") as f:
        f.write(agent_md)
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(managed_mcp, f, indent=2)
        f.write("\n")

    logger.info(
        f"cowork_roles: materialized role '{role.name}' → {agent_path} "
        f"({len(allowed_tool_names)} connector tools, {len(resolved_skills)} skills, "
        f"{len(role.subagent_allowlist or [])} subagents)"
    )
    return {
        "agent_type": agent_type,
        "agent_md_path": agent_path,
        "managed_mcp_path": mcp_path,
        "allowed_tool_names": allowed_tool_names,
        "resolved_skills": resolved_skills,
        "subagent_allowlist": list(role.subagent_allowlist or []),
    }
