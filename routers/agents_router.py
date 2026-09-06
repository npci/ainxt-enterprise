# SPDX-License-Identifier: MIT
# ============================================================
# AGENTS CATALOG ROUTER
# Consumer-facing endpoint for browsing agents, starring favorites,
# and managing agent-scoped KB document attachments.
#
# Endpoints:
#   GET  /agents/catalog                 — list visible agents (public + dept) with favorite flag
#   POST /agents/{agent_id}/favorite     — star an agent
#   DELETE /agents/{agent_id}/favorite   — unstar an agent
#   GET  /agents/{agent_id}/kb-docs      — list KB docs attached to this agent
#   POST /agents/{agent_id}/kb-docs      — attach a staged KB doc (creator / admin only)
#   DELETE /agents/{agent_id}/kb-docs/{doc_id} — detach a KB doc
# ============================================================

import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel

from core.logger import logger
from db.database import SessionLocal
from db.models import AgentRecord, UserFavorite, AgentKbDoc, KnowledgeDocument
from auth.dependencies import get_current_user

router = APIRouter(tags=["agents-catalog"])

# ── Statuses visible in the catalog (excludes DRAFT/PENDING/REJECTED) ──
_CATALOG_STATUSES = {"APPROVED", "PRODUCTION"}


# ============================================================
# HELPERS
# ============================================================

def _agent_to_dict(row: AgentRecord, is_favorited: bool = False) -> dict:
    return {
        "id":              str(row.id),
        "name":            row.name,
        "description":     row.description or "",
        "system_prompt":   row.system_prompt or "",
        "tools":           row.tools or [],
        "skills":          row.skills or [],
        "status":          row.status or "PRODUCTION",
        "enabled":         row.enabled,
        "visibility":      row.visibility or "private",
        "department":      row.department or "",
        "created_by":      row.created_by or "",
        "version":         row.version or "1.0.0",
        "kb_namespace":    getattr(row, "kb_namespace", None),
        "preferred_model": getattr(row, "preferred_model", None),
        "created_at":      row.created_at.isoformat() if row.created_at else None,
        "is_favorited":    is_favorited,
    }


# ============================================================
# CATALOG
# ============================================================

@router.get("/agents/catalog")
def agents_catalog(current_user: dict = Depends(get_current_user)):
    """
    Return all agents visible to the requesting user, grouped into:
      - public:     visibility='public', any approved/production status
      - department: visibility='private', department matches user's department
    Each agent includes an `is_favorited` flag based on user_favorites table.
    """
    from sqlalchemy import or_, and_
    from auth.rbac import is_admin

    user_id = current_user.get("sub") or current_user.get("user_id")
    dept    = current_user.get("department", "") or ""

    db = SessionLocal()
    try:
        # ── Fetch all catalog-eligible agents ──
        query = db.query(AgentRecord).filter(
            AgentRecord.status.in_(_CATALOG_STATUSES),
            AgentRecord.enabled == True,
            )
        if not is_admin(current_user):
            query = query.filter(
                or_(
                    AgentRecord.visibility == "public",
                    and_(
                        AgentRecord.visibility == "private",
                        AgentRecord.department == dept,
                        )
                )
            )
        all_agents = query.order_by(AgentRecord.name).all()

        # ── Fetch this user's favorites (agent entity type) ──
        fav_rows = db.query(UserFavorite).filter(
            UserFavorite.user_id == user_id,
            UserFavorite.entity_type == "agent",
            ).all()
        favorited_ids = {f.entity_id for f in fav_rows}

        public_agents = []
        dept_agents   = []
        seen_names    = set()
        for row in all_agents:
            d = _agent_to_dict(row, is_favorited=(row.name in favorited_ids))
            seen_names.add(row.name)
            if row.visibility == "public":
                public_agents.append(d)
            else:
                dept_agents.append(d)

        # ── Resolve favourited agents that fell OUTSIDE the visible set (#30) ──
        # A user can favourite an agent that is later hidden by the visibility
        # filter (e.g. it became private to another department). Those favourites
        # were previously unreachable — the UI only had the id, not the object.
        # Fetch the full records for any favourited names we haven't already
        # included so "Favourites" can always render them.
        missing_fav = [fid for fid in favorited_ids if fid not in seen_names]
        favorite_agents = []
        if missing_fav:
            fav_agent_rows = db.query(AgentRecord).filter(
                AgentRecord.name.in_(missing_fav),
                AgentRecord.enabled == True,  # noqa: E712
            ).all()
            for row in fav_agent_rows:
                favorite_agents.append(_agent_to_dict(row, is_favorited=True))

        return {
            "public":     public_agents,
            "department": dept_agents,
            "favorites":  list(favorited_ids),
            # Full objects for favourites outside the visible set (#30).
            "favorite_agents": favorite_agents,
        }
    except Exception as exc:
        logger.error(f"agents_catalog error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


# ============================================================
# FAVORITES
# ============================================================

@router.post("/agents/{agent_name}/favorite")
def add_favorite(agent_name: str, current_user: dict = Depends(get_current_user)):
    """Star an agent. Idempotent — safe to call multiple times."""
    user_id = current_user.get("sub") or current_user.get("user_id")
    db = SessionLocal()
    try:
        exists = db.query(UserFavorite).filter(
            UserFavorite.user_id    == user_id,
            UserFavorite.entity_type == "agent",
            UserFavorite.entity_id  == agent_name,
            ).first()
        if not exists:
            fav = UserFavorite(
                id          = str(uuid.uuid4()),
                user_id     = user_id,
                entity_type = "agent",
                entity_id   = agent_name,
            )
            db.add(fav)
            db.commit()
        return {"favorited": True, "agent": agent_name}
    except Exception as exc:
        db.rollback()
        logger.error(f"add_favorite error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.delete("/agents/{agent_name}/favorite")
def remove_favorite(agent_name: str, current_user: dict = Depends(get_current_user)):
    """Unstar an agent."""
    user_id = current_user.get("sub") or current_user.get("user_id")
    db = SessionLocal()
    try:
        db.query(UserFavorite).filter(
            UserFavorite.user_id    == user_id,
            UserFavorite.entity_type == "agent",
            UserFavorite.entity_id  == agent_name,
            ).delete()
        db.commit()
        return {"favorited": False, "agent": agent_name}
    except Exception as exc:
        db.rollback()
        logger.error(f"remove_favorite error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


# ============================================================
# AGENT KB DOCS
# ============================================================

class AttachDocRequest(BaseModel):
    doc_id: str    # knowledge_docs.id (UUID string)


@router.get("/agents/{agent_name}/kb-docs")
def list_agent_kb_docs(agent_name: str, current_user: dict = Depends(get_current_user)):
    """List KB documents linked to a specific agent."""
    db = SessionLocal()
    try:
        links = db.query(AgentKbDoc).filter(
            AgentKbDoc.agent_id == agent_name,
            ).all()

        docs = []
        for link in links:
            doc = db.query(KnowledgeDocument).filter(
                KnowledgeDocument.id == str(link.doc_id),
                ).first()
            if doc:
                docs.append({
                    "doc_id":   str(link.doc_id),
                    "filename": doc.filename,
                    "name":     doc.name,
                    "status":   doc.status,
                    "created_at": link.created_at.isoformat() if link.created_at else None,
                })
        return {"agent": agent_name, "kb_docs": docs}
    except Exception as exc:
        logger.error(f"list_agent_kb_docs error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.post("/agents/{agent_name}/kb-docs")
def attach_kb_doc(
        agent_name: str,
        body: AttachDocRequest,
        current_user: dict = Depends(get_current_user),
):
    """
    Link a staged KnowledgeDocument to an agent.
    The doc will be activated (embedded into pgvector with repo='agent_kb:{agent_name}')
    when the agent is approved via governance_router.
    """
    user_id = current_user.get("sub") or current_user.get("user_id")
    db = SessionLocal()
    try:
        # Verify agent exists
        agent = db.query(AgentRecord).filter(AgentRecord.name == agent_name).first()
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

        # Verify doc exists
        doc = db.query(KnowledgeDocument).filter(
            KnowledgeDocument.id == body.doc_id,
            ).first()
        if not doc:
            raise HTTPException(status_code=404, detail=f"Document '{body.doc_id}' not found")

        # Upsert link
        exists = db.query(AgentKbDoc).filter(
            AgentKbDoc.agent_id == agent_name,
            AgentKbDoc.doc_id   == body.doc_id,
            ).first()
        if not exists:
            link = AgentKbDoc(
                id       = str(uuid.uuid4()),
                agent_id = agent_name,
                doc_id   = body.doc_id,
            )
            db.add(link)
            db.commit()

        return {
            "success":  True,
            "agent":    agent_name,
            "doc_id":   body.doc_id,
            "filename": doc.filename,
            "status":   "linked — will activate on agent approval",
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        logger.error(f"attach_kb_doc error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.delete("/agents/{agent_name}/kb-docs/{doc_id}")
def detach_kb_doc(
        agent_name: str,
        doc_id: str,
        current_user: dict = Depends(get_current_user),
):
    """Unlink a KB document from an agent."""
    db = SessionLocal()
    try:
        db.query(AgentKbDoc).filter(
            AgentKbDoc.agent_id == agent_name,
            AgentKbDoc.doc_id   == doc_id,
            ).delete()
        db.commit()
        return {"success": True, "agent": agent_name, "doc_id": doc_id, "unlinked": True}
    except Exception as exc:
        db.rollback()
        logger.error(f"detach_kb_doc error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()


@router.get("/agents/{agent_name}/kb-status")
def agent_kb_status(agent_name: str, current_user: dict = Depends(get_current_user)):
    """
    Diagnostic: show KB indexing status for every doc linked to this agent.
    For each linked doc reports:
      - knowledge_docs.status  (PENDING_APPROVAL / APPROVED / etc.)
      - how many vectors exist in pgvector
      - which repo they are stored under
      - whether the correct agent_kb:{agent_name} repo has vectors
    """
    from db.database import VectorSessionLocal
    from sqlalchemy import text as _sql_text

    expected_repo = f"agent_kb:{agent_name.strip().lower().replace(' ', '_').replace('-', '_')}"
    db  = SessionLocal()
    vdb = VectorSessionLocal()
    try:
        links = db.query(AgentKbDoc).filter(AgentKbDoc.agent_id == agent_name).all()
        if not links:
            return {
                "agent":        agent_name,
                "expected_repo": expected_repo,
                "linked_docs":  [],
                "summary":      "No KB docs linked to this agent.",
            }

        doc_ids = [str(link.doc_id) for link in links]

        # Fetch knowledge_docs metadata
        from db.models import KnowledgeDocument
        kb_docs = {
            str(d.id): d
            for d in db.query(KnowledgeDocument).filter(KnowledgeDocument.id.in_(doc_ids)).all()
        }

        # Fetch vector counts grouped by doc_id + repo
        rows = vdb.execute(
            _sql_text(
                "SELECT metadata->>'doc_id' AS doc_id, repo, COUNT(*) AS chunk_count "
                "FROM document_embeddings "
                "WHERE metadata->>'doc_id' = ANY(:ids) "
                "GROUP BY metadata->>'doc_id', repo "
                "ORDER BY metadata->>'doc_id', repo"
            ),
            {"ids": doc_ids},
        ).fetchall()

        # Group by doc_id
        vector_map: dict = {}
        for row in rows:
            vector_map.setdefault(row.doc_id, []).append({
                "repo":        row.repo,
                "chunk_count": row.chunk_count,
            })

        results = []
        for link in links:
            did   = str(link.doc_id)
            kdoc  = kb_docs.get(did)
            vecs  = vector_map.get(did, [])
            in_correct_repo = any(v["repo"] == expected_repo for v in vecs)
            results.append({
                "doc_id":          did,
                "filename":        kdoc.filename if kdoc else "?",
                "kb_status":       kdoc.status   if kdoc else "NOT_FOUND",
                "chunks_staged":   bool(kdoc.chunks) if kdoc else False,
                "vector_repos":    vecs,
                "in_correct_repo": in_correct_repo,
                "diagnosis": (
                    "OK — indexed in correct repo"           if in_correct_repo else
                    "WRONG REPO — re-approve agent to migrate" if vecs else
                    "NOT INDEXED — approve agent to activate"
                ),
            })

        all_ok = all(r["in_correct_repo"] for r in results)
        return {
            "agent":         agent_name,
            "expected_repo": expected_repo,
            "linked_docs":   results,
            "all_indexed":   all_ok,
            "summary": (
                f"All {len(results)} doc(s) correctly indexed in {expected_repo!r}." if all_ok
                else f"{sum(not r['in_correct_repo'] for r in results)} doc(s) NOT in correct repo. "
                     "Re-submit agent through governance to fix."
            ),
        }
    except Exception as exc:
        logger.error(f"agent_kb_status error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        db.close()
        vdb.close()


# ── P9: Session undo endpoints ────────────────────────────────────────────────

from auth.dependencies import get_current_user as _require_auth_agents


@router.post("/sessions/{session_id}/undo", status_code=200)
async def undo_last_action(
    session_id: str,
    current_user: dict = Depends(_require_auth_agents),
):
    """
    P9: Undo the last reversible write operation in a session.

    Pops the most recent action from the undo stack and executes its inverse
    (e.g. deletes a file created by gitlab_create_or_update_file).

    Returns a description of what was undone, or 404 if the stack is empty.
    """
    user_id = current_user.get("user_id") or current_user.get("sub", "")
    try:
        from agents.recovery_engine import recovery_engine as _re
        # SEC-05: verify session ownership before allowing undo
        # The undo stack key is namespaced as undo_stack:{user_id}:{session_id}
        # so _re.undo_last() will find nothing if user_id doesn't match
        result = _re.undo_last(session_id, user_id=user_id)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=f"No undoable actions in session {session_id} for this user",
            )
        return {"session_id": session_id, "undone": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"undo_last_action failed: {e}")
        raise HTTPException(status_code=500, detail=f"Undo failed: {e}")


@router.get("/sessions/{session_id}/undo-stack", status_code=200)
async def get_undo_stack(
    session_id: str,
    current_user: dict = Depends(_require_auth_agents),
):
    """
    P9: Return the current undo stack for a session (newest last).

    Each entry contains: action_id, tool_name, inputs (sanitized), timestamp.
    """
    user_id = current_user.get("user_id") or current_user.get("sub", "")
    try:
        from agents.recovery_engine import recovery_engine as _re
        # SEC-05: pass user_id so only the owning user's stack is returned
        stack = _re.get_undo_stack(session_id, user_id=user_id)
        # Sanitize inputs (remove sensitive fields)
        safe_stack = []
        for entry in stack:
            safe_entry = {
                "action_id":  entry.get("action_id"),
                "tool_name":  entry.get("tool_name"),
                "timestamp":  entry.get("timestamp"),
                "inputs":     {
                    k: v for k, v in (entry.get("inputs") or {}).items()
                    if k not in ("content", "password", "token", "secret")
                },
            }
            safe_stack.append(safe_entry)
        return {"session_id": session_id, "undo_stack": safe_stack, "count": len(safe_stack)}
    except Exception as e:
        logger.error(f"get_undo_stack failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get undo stack: {e}")
