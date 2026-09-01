# SPDX-License-Identifier: Apache-2.0
"""Chat history endpoints.

Security review F-06/F-10: every route below now passes ``current_user.id``
into the engine/store so a thread's recorded owner is enforced end to end
(mirrors the pattern already used by app/api/agent_chat.py for the separate
per-agent chat history). Threads created before this migration (or by any
remaining internal call site that doesn't yet pass an owner) have no
recorded owner and are treated as accessible rather than denied — see
CheckpointStore / postgres_store.py migration comments for the rationale.
"""
from fastapi import APIRouter, Depends, HTTPException
from app.models import AuthenticatedUser
from app.engine import get_engine
from app.api.deps import require_access

router = APIRouter()


@router.get("/chat-threads/{workflow_id}")
async def get_workflow_chat_threads(
    workflow_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        threads = await get_engine().list_threads(workflow_id, current_user.id)
        return {"workflow_id": workflow_id, "threads": threads}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/chat-history/{thread_id}")
async def get_thread_chat_history(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        return await get_engine().get_history(thread_id, current_user.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/chat-threads/{thread_id}", status_code=204)
async def delete_chat_thread(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        deleted = await get_engine().delete_thread(thread_id, current_user.id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")


@router.get("/chat-pending/{thread_id}")
async def get_thread_pending_interrupt(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Return the pending HITL interrupt for a thread, or null if none.

    The frontend polls this on thread open so a paused run re-renders its
    HITL card even after the live SSE stream has been lost (tab close,
    reload, network blip).
    """
    try:
        snap = await get_engine().get_pending_interrupt(thread_id, current_user.id)
        return {"thread_id": thread_id, "pending": snap}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/chat-pending/{thread_id}")
async def abort_thread_pending_interrupt(
    thread_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Discard the pending interrupt / paused-run snapshot for a thread.

    Used by the "Abort" affordance on the failure / user-cancelled banner
    so the reviewer can decide "I don't want to resume this — throw away
    the checkpoint and start a fresh conversation from the next message".
    The chat history itself is preserved; only the pending snapshot row
    is deleted. Returns ``{aborted: bool}`` so the client can tell whether
    anything was actually removed.
    """
    try:
        aborted = await get_engine().clear_pending_interrupt(thread_id, current_user.id)
        return {"thread_id": thread_id, "aborted": bool(aborted)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/node-last-output/{thread_id}/{node_id}")
async def get_node_last_output(
    thread_id: str,
    node_id: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """Return the most recent output produced by a specific node in a thread.

    Powers the Loop node's connection-aware list picker — when the user wires
    Loop ← UpstreamAgent and opens the Loop config, the frontend fetches the
    upstream agent's last output and surfaces any lists inside it as
    click-to-pick options instead of demanding a typed dotted path.

    Returns {"output": null} if the node hasn't run in this thread yet.
    """
    try:
        record = await get_engine().get_node_last_output(thread_id, node_id, current_user.id)
        return {
            "thread_id": thread_id,
            "node_id":   node_id,
            "output":    (record or {}).get("output"),
            "agent":     (record or {}).get("agent"),
            "updated_at": (record or {}).get("updated_at"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
