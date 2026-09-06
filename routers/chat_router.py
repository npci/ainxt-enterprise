# SPDX-License-Identifier: MIT
# ============================================================
# CHAT ROUTER — multimodal file/image/document upload
#
# POST   /chat/upload        multipart upload (chat_id optional)
# GET    /chat/attachments/{id}  metadata + presigned URL
# DELETE /chat/attachments/{id}  delete file + DB record
#
# Storage backend: MinIO (auto-detected) → local disk fallback
# ============================================================

import uuid
from typing import List, Optional

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException, Depends
from fastapi.concurrency import run_in_threadpool

from core.logger import logger
from auth.dependencies import get_current_user
from core.file_validator import validate_upload
from core.rate_limiter import enforce_rate_limit_with_behaviour, FILE_UPLOAD
from core.storage import UPLOAD_SUBDIR_IMAGE, UPLOAD_SUBDIR_DOCUMENT
from core.security_validation import (
    validate_chat_title,
    validate_chat_scope_fields,
    validate_chat_artifact_request,
    validate_prompt_template_request,
    validate_free_text,
    _flatten_errors,
)
from db.chat_rows import ensure_chat_row

router = APIRouter(tags=["chat"])

_MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB

_ALLOWED_EXTENSIONS = frozenset({
    "pdf", "docx", "pptx", "ppt", "xlsx", "xls", "csv",
    "html", "htm", "rtf", "txt", "json", "md", "xml",
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "log"
})

_CONTENT_TYPES = {
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt":  "application/vnd.ms-powerpoint",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls":  "application/vnd.ms-excel",
    "csv":  "text/csv",
    "html": "text/html", "htm": "text/html",
    "rtf":  "application/rtf",
    "txt":  "text/plain",
    "md":   "text/markdown",
    "json": "application/json",
    "xml":  "application/xml",
    "png":  "image/png",
    "jpg":  "image/jpeg", "jpeg": "image/jpeg",
    "gif":  "image/gif",
    "webp": "image/webp",
    "bmp":  "image/bmp",
}

def _ext(filename: str) -> str:
    from pathlib import Path
    return Path(filename).suffix.lstrip(".").lower()


# ============================================================
# UPLOAD
# ============================================================

@router.post("/chat/upload")
async def upload_chat_files(
        request: Request,
    files:   List[UploadFile] = File(...),
    chat_id: Optional[str]   = Form(None),   # optional — UI generates UUID client-side
    current_user: dict = Depends(get_current_user),
):
    """
    Upload one or more files to a chat session.
    chat_id is optional — if omitted, a server-side UUID is generated.
    Returns parsed text length for each file so the UI can decide whether
    to show a preview tooltip.
    """
    try:

        # ── Rate limit: 30 uploads per 5 minutes per user/IP (behaviour-aware) ───
        user_id = current_user.get("sub") or current_user.get("email") if isinstance(current_user, dict) else None
        try:
            enforce_rate_limit_with_behaviour(request, FILE_UPLOAD, user_id=user_id)

        except Exception:
            raise

        # Generate chat_id if not provided
        effective_chat_id = chat_id or str(uuid.uuid4())

        from core.storage import storage

        uploaded = []

        for idx, upload in enumerate(files):
            file_id  = str(uuid.uuid4())
            filename = upload.filename or f"file_{file_id}"

            # Read content first so we can do magic-byte validation
            try:
                content = await upload.read()

            except Exception:
                raise

            # ── Security: validate extension + magic bytes + size ──
            vr = validate_upload(
                filename=filename,
                content=content,
                allowed_extensions=_ALLOWED_EXTENSIONS,
                max_size_bytes=_MAX_SIZE_BYTES,
                caller="chat_router",
            )

            if not vr.valid:
                uploaded.append({
                    "id": file_id, "file_name": filename, "file_type": vr.extension,
                    "file_size": vr.size_bytes, "blocked": True,
                    "block_reason": vr.error,
                    **({"threat": vr.threat} if vr.threat else {}),
                })
                continue

            ext       = vr.extension
            file_size = vr.size_bytes
            # Use sanitised filename for storage; keep original for display
            safe_name = vr.safe_filename

            # ── Parse ───────────────────────────────────────────

            try:
                from core.document_parser import parse_file_structured
                import tempfile, os
                with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                try:
                    parsed_doc = await run_in_threadpool(
                        parse_file_structured, tmp_path, ext, filename
                    )
                    parsed_text = parsed_doc["content"]

                finally:
                    os.unlink(tmp_path)
            except Exception as e:

                logger.error(f"chat_router: parse_file failed for {filename}: {e}")
                parsed_text = f"[Parse error: {e}]"

            # ── Compliance check ────────────────────────────────
            blocked           = False
            block_reason      = None
            compliance_reasons: list = []
            storage_path      = ""

            # Uploaded-file scanning is gated by COMPLIANCE_SCAN_TOOL_RESULTS — the
            # file-read data-breach guard. Default OFF: uploaded content is not scanned.
            from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
            if COMPLIANCE_SCAN_TOOL_RESULTS:
                try:
                    from agents.compliance_engine import compliance_engine, BLOCKING_TYPES
                    check    = compliance_engine.validate_input(parsed_text or "")
                    findings = check.get("findings", [])

                    if check.get("blocked", False):
                        blocked            = True
                        compliance_reasons = sorted(set(
                            f["type"] for f in findings
                            if f.get("type") in BLOCKING_TYPES
                        ))
                        block_reason = ", ".join(compliance_reasons) if compliance_reasons else "PCI/PII data"
                        parsed_text  = None
                        logger.warning(f"chat_router: {filename} blocked — {compliance_reasons}")
                except Exception as e:

                    logger.warning(f"chat_router: compliance check error: {e}")

            # Uploaded images → UPLOAD_SUBDIR_IMAGE ; everything else → UPLOAD_SUBDIR_DOCUMENT.
            # Keeps uploaded assets in a SEPARATE tree from generated docs/images.
            _IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
            asset_kind  = "image" if ext in _IMAGE_EXTS else "document"
            upload_subdir = UPLOAD_SUBDIR_IMAGE if asset_kind == "image" else UPLOAD_SUBDIR_DOCUMENT

            # Use "sub" first — this is the JWT subject and is what mcp_bridge.py
            # queries by when resolving ChatAttachment rows. Putting "user_id" first
            # caused a mismatch: uploads stored under "user_id" value but looked up
            # under "sub" value → file not found even after upload.
            _owner_id = (
                current_user.get("sub")
                or current_user.get("user_id")
                or current_user.get("email")
                or ""
            )

            # ── Store file (skip if blocked) — use sanitised name ──
            if not blocked:

                try:
                    ct = _CONTENT_TYPES.get(ext, "application/octet-stream")
                    storage_path = await run_in_threadpool(
                        storage.save,
                        content,
                        safe_name,
                        ct,
                        upload_subdir,
                        _owner_id,
                        effective_chat_id,
                    )

                except Exception as e:

                    logger.error(f"chat_router: storage.save failed for {filename}: {e}")
                    storage_path = ""

            # ── Image context (multi-turn image memory) ─────────
            # For image uploads, parse_image() already produced a Gemini Vision
            # description (stored in parsed_text). Persist it explicitly so later
            # turns can replay a compact caption into history without re-sending
            # the image bytes. image_caption = a short summary (first ~2 sentences,
            # ≤600 chars) used for cheap history injection.
            image_description = None
            image_caption     = None
            if asset_kind == "image" and parsed_text and not blocked:
                import re as _re_cap
                image_description = parsed_text
                _cap_src = " ".join(parsed_text.split())      # collapse whitespace
                # First two sentences, else hard char cap.
                _sentences = _re_cap.split(r'(?<=[.!?])\s+', _cap_src)
                _cap = " ".join(_sentences[:2]).strip() or _cap_src
                image_caption = _cap[:600]
                logger.info(
                    f"[DOCTRACE] L1-img caption | file={filename!r} "
                    f"desc_chars={len(image_description)} caption_chars={len(image_caption)} "
                    f"attachment_id={file_id}"
                )

            # ── Persist to Postgres ─────────────────────────────

            try:
                from db.database import SessionLocal
                from db.models import ChatAttachment
                db = SessionLocal()
                try:
                    # Parent chats row must exist first — see ensure_chat_row.
                    ensure_chat_row(db, effective_chat_id, _owner_id)
                    att = ChatAttachment(
                        id=file_id,
                        chat_id=effective_chat_id,
                        user_id=_owner_id,
                        file_name=filename,
                        file_type=ext,
                        file_size=file_size,
                        kind=asset_kind,
                        storage_path=storage_path,
                        parsed_text=parsed_text,
                        image_description=image_description,
                        image_caption=image_caption,
                        created_by=current_user.get("user_id", ""),
                    )
                    db.add(att)
                    db.commit()

                finally:
                    db.close()
                _persisted = True
            except Exception as e:

                # ERROR, not warning: without this row /ask cannot resolve the
                # attachment_id at all, so the file is invisible to the model.
                # Silent-warning here is what hid the FK violation above.
                _persisted = False
                logger.error(
                    f"chat_router: Postgres persist failed for {filename!r} "
                    f"(attachment_id={file_id} chat_id={effective_chat_id}): {e}"
                )

            # ── Build response entry ────────────────────────────
            entry: dict = {
                "id":             file_id,
                "chat_id":        effective_chat_id,
                "file_name":      filename,
                "file_type":      ext,
                "file_size":      file_size,
                "parsed_length":  len(parsed_text or ""),
                "blocked":        blocked,
            }
            # Lets the caller/UI tell "uploaded and resolvable by /ask" apart from
            # "bytes stored but the DB row is missing".
            if not _persisted:
                entry["persist_failed"] = True
            if block_reason:
                entry["block_reason"]      = block_reason
                entry["compliance_reasons"] = compliance_reasons
            # Send first 200 chars of parsed text as preview for the UI tooltip
            if parsed_text and len(parsed_text) > 0:
                entry["parsed_preview"] = parsed_text[:200].strip()
            # Include full parsed text so the frontend can show it without a
            # separate API call (used for Office file types the browser can't render)
            entry["parsed_text"] = parsed_text or ""

            uploaded.append(entry)

            logger.info(
                f"chat_router: processed {filename} ({file_size}B) "
                f"backend={storage.backend} blocked={blocked}"
            )

        return {"uploaded": uploaded, "chat_id": effective_chat_id}
    except HTTPException:
        raise
    except Exception as e:

        try:
            logger.exception(f"chat_router: unhandled error in /chat/upload: {e}")
        except Exception:
            pass
        raise

# ============================================================
# LIST CHATS
# ============================================================

@router.get("/chats")
def list_chats(current_user: dict = Depends(get_current_user)):
    """List the authenticated user's chat sessions, most recent first."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    # Empty string or the anonymous sentinel "default" cannot be cast to UUID.
    # Return an empty list so the UI degrades gracefully instead of logging errors.
    if not user_id or user_id == "default":
        return {"chats": []}
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage
        from sqlalchemy import func
        db = SessionLocal()
        try:
            rows = (
                db.query(Chat)
                .filter(Chat.user_id == user_id)
                .order_by(Chat.updated_at.desc())
                .limit(100)
                .all()
            )
            result = []
            for c in rows:
                count = (
                            db.query(func.count(ChatMessage.id))
                            .filter(ChatMessage.chat_id == c.id)
                            .scalar()
                        ) or 0
                result.append({
                    "id":            str(c.id),
                    "title":         c.title or "New Chat",
                    "project_id":    c.project_id,
                    "is_pinned":     bool(getattr(c, "is_pinned", False)),
                    "rag_mode":      (getattr(c, "rag_mode", "off") or "off"),
                    # Per-chat KB scope (Phase 1 wiring) — UI hydrates the
                    # ScopePicker from these on chat-open. /ask gateway reads
                    # them server-side and injects into _user_ctx['scope_filter']
                    # so retrieval is deterministically scoped.
                    "product_id":    (str(c.product_id) if getattr(c, "product_id", None) else None),
                    "domain":        getattr(c, "domain", None),
                    "spec_version":  getattr(c, "spec_version", None),
                    "kb_doc_id":     (str(c.kb_doc_id) if getattr(c, "kb_doc_id", None) else None),
                    "message_count": count,
                    "created_at":    c.created_at.isoformat() if c.created_at else "",
                    "updated_at":    c.updated_at.isoformat() if c.updated_at else "",
                })
            return {"chats": result}
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"list_chats error: {e}")
        return {"chats": []}

# ============================================================
# GET CHAT MESSAGES
# ============================================================

@router.get("/chats/{chat_id}/messages")
def get_chat_messages(chat_id: str, current_user: dict = Depends(get_current_user)):
    """Load the last 100 messages for a chat session."""
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage, ChatArtifact, ChatAttachment
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id).first()
            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.created_at.asc())
                .limit(100)
                .all()
            )
            # Build a message_id → artifacts lookup so the "Open in Canvas"
            # chip survives page reload (image / code / html artifacts).
            msg_ids = [str(m.id) for m in msgs]
            artifact_rows = (
                db.query(ChatArtifact)
                .filter(ChatArtifact.chat_id == chat_id,
                        ChatArtifact.message_id.in_(msg_ids))
                .all()
            ) if msg_ids else []
            artifacts_by_msg = {}
            for a in artifact_rows:
                mid = str(a.message_id) if a.message_id else None
                if mid:
                    artifacts_by_msg.setdefault(mid, []).append({
                        "id":   str(a.id),
                        "title": a.title,
                        "type":  a.artifact_type,
                    })

            # Build an attachment_id → metadata lookup so the file chip shows
            # the real name + type after page reload (the browser preview cache
            # only holds bytes, not metadata). One batched query for all ids
            # referenced across this chat's messages.
            all_att_ids = []
            for m in msgs:
                for aid in (getattr(m, "attachment_ids", None) or []):
                    if aid:
                        all_att_ids.append(aid)
            att_by_id = {}
            if all_att_ids:
                for att in (
                    db.query(ChatAttachment)
                    .filter(ChatAttachment.id.in_(list(set(all_att_ids))))
                    .all()
                ):
                    att_by_id[str(att.id)] = {
                        "id":        str(att.id),
                        "file_name": att.file_name,
                        "file_type": att.file_type,
                        "file_size": att.file_size,
                        # kind ("image" | "document" | "generated") straight off
                        # the ChatAttachment row. The client previously had to
                        # infer image-vs-doc by regex-testing the message body
                        # for a -image- marker, but that marker is added by the
                        # UI to its LOCAL bubble text only: /ask is sent the
                        # plain `question`, and the gateway persists its own
                        # safe_question, so the marker NEVER reaches the DB.
                        # The test could therefore never pass on reload and
                        # every uploaded image rehydrated as a nameless "doc"
                        # chip instead of a thumbnail. Authoritative value here
                        # removes the guess entirely.
                        "kind":      (att.kind or "document"),
                    }
            return {
                "rag_mode":     (getattr(chat, "rag_mode", "off") or "off") if chat else "off",
                # Per-chat KB scope (Phase 1 wiring) — hydrates ScopePicker.
                "product_id":   (str(chat.product_id) if chat and getattr(chat, "product_id", None) else None),
                "domain":       getattr(chat, "domain", None) if chat else None,
                "spec_version": getattr(chat, "spec_version", None) if chat else None,
                "kb_doc_id":    (str(chat.kb_doc_id) if chat and getattr(chat, "kb_doc_id", None) else None),
                "messages": [
                    {
                        "id":          str(m.id),
                        "role":        m.role,
                        "content":     m.content,
                        "model_used":  m.model_used,
                        "tokens_used": m.tokens_used,
                        "cost_usd":    m.cost_usd,
                        # Split token counts + latency so the frontend can
                        # rebuild ALL FOUR chips (model · in/out tokens ·
                        # latency · cost) after a page refresh — not just the
                        # combined tokens_used total. Persisted per-message on
                        # the ChatMessage row (db/models.py:191-193).
                        "in_tok":      m.in_tok,
                        "out_tok":     m.out_tok,
                        "latency":     m.latency,
                        "language":    m.language,
                        # Phase 3 — coverage badge restoration on reload (§8x).
                        # NULL on user messages and on pre-Phase-1 history.
                        "coverage_trace": getattr(m, "coverage_trace", None),
                        "artifacts":  artifacts_by_msg.get(str(m.id), []),
                        # Attachment ids (docs + images) so the frontend can
                        # rehydrate chips/thumbnails from the browser preview
                        # cache after a page refresh. Image vs doc is inferred
                        # from the 🖼 / 📎 marker in `content`.
                        "attachment_ids": (getattr(m, "attachment_ids", None) or []),
                        # Resolved attachment metadata (name/type/size) so the
                        # chip shows the real filename + the preview modal knows
                        # the file type. Docs resolve from the ChatAttachment
                        # table; images aren't stored server-side (browser cache
                        # only) so they fall back to an id-only entry the client
                        # rehydrates from the 🖼 marker.
                        "attachments": [
                            att_by_id.get(str(aid), {"id": str(aid), "file_name": "", "file_type": ""})
                            for aid in (getattr(m, "attachment_ids", None) or [])
                        ],
                        "created_at":  m.created_at.isoformat() if m.created_at else "",
                    }
                    for m in msgs
                ],
            }
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# DELETE CHAT
# ============================================================

@router.delete("/chats/{chat_id}")
def delete_chat(chat_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a chat and all its messages."""
    try:
        from db.database import SessionLocal
        from db.models import Chat
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            db.delete(chat)
            db.commit()
            return {"deleted": True, "id": chat_id}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# RENAME CHAT
# ============================================================

@router.patch("/chats/{chat_id}/title")
def rename_chat(chat_id: str, body: dict, current_user: dict = Depends(get_current_user)):
    """Update chat title."""
    title = (body.get("title") or "New Chat").strip()[:500]
    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, title = validate_chat_title(title)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"title": _errs}))
    title = title or "New Chat"
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            chat.title      = title
            chat.updated_at = _dt.datetime.utcnow()
            db.commit()
            return {"id": chat_id, "title": title}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# PIN / UNPIN CHAT
# ============================================================

@router.patch("/chats/{chat_id}/pin")
def toggle_pin_chat(chat_id: str, current_user: dict = Depends(get_current_user)):
    """Toggle pinned state for a chat. Returns new is_pinned value."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            chat.is_pinned = not bool(getattr(chat, "is_pinned", False))
            db.commit()
            return {"id": chat_id, "is_pinned": chat.is_pinned}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# UPDATE RAG MODE  (Chat-level Generic | Knowledge Base toggle)
# ============================================================

# ============================================================
# TEXT → IMAGE GENERATION  (Imagen / DALL-E via LLM proxy)
#
# POST /chat/image-generate
#   { prompt, chat_id, message_id?, aspect_ratio?, style? }
#   → returns image/png bytes AND persists into chat_artifacts as
#     an "image" artifact so it shows up in the Canvas drawer.
#
# In production the call goes:
#   gateway → LLM_PROXY_URL/llm/imagen → Imagen-3 / DALL-E
# In dev (no LLM_PROXY_URL) it falls back to direct SDK calls.
# Stock photo APIs (Pexels / Unsplash) are NEVER used.
# ============================================================

# ── Image-gen pricing source-of-truth ─────────────────────────────
# Image generation has exactly ONE backing model on this platform:
# gemini-3.1-flash-image. Anthropic / OpenAI / Local models do NOT
# have an image-gen endpoint here — when the user picks one of those
# the UI routes the prompt through normal /ask chat instead, so the
# model can respond with text (typically a refusal). The mapping in
# ai-ui/src/utils/imageGenerate.js mirrors this.
#
# SINGLE SOURCE OF TRUTH: image-token pricing is read from
# core.model_registry.MODEL_COST_PER_1M[GEMINI_IMAGE_MODEL] — the same table
# the chat/messages path uses. This avoids the drift bug we hit earlier, where
# a duplicated hardcoded rate here stayed stale (text rates) after the registry
# was corrected, so every image cost rounded to <$0.01.
#
# We use the real token counts captured by gateway_gemini.generate_imagen()
# (via Gemini's usage_metadata) so the cost chip matches actual billing.
def _gemini_image_rates_per_1k() -> tuple[float, float]:
    """(input_per_1k, output_per_1k) for the Gemini image model, sourced from
    the central registry (stored per-1M, so divide by 1000). Falls back to the
    known image rate if the registry lookup ever fails."""
    try:
        from core.model_registry import MODEL_COST_PER_1M, GEMINI_IMAGE_MODEL
        in_1m, out_1m = MODEL_COST_PER_1M.get(GEMINI_IMAGE_MODEL, (0.30, 30.00))
        return (in_1m / 1000.0, out_1m / 1000.0)
    except Exception:
        # $0.30/1M input, $30/1M output — image OUTPUT tokens are billed far
        # higher than the old Flash TEXT rate.
        return (0.0003, 0.03)

@router.post("/chat/image-generate")
def chat_generate_image(body: dict, current_user: dict = Depends(get_current_user)):
    """Generate an image inline for a chat turn.

    Persists the image both as a downloadable ChatAttachment (storage
    backend) AND as a ChatArtifact (Canvas-style) so the user can
    open it in the right-pane drawer or copy the inline URL.
    """
    prompt   = (body.get("prompt") or "").strip()
    chat_id  = body.get("chat_id") or ""
    aspect   = body.get("aspect_ratio") or "16:9"
    style    = body.get("style") or ""
    # The user's original question before /ask enrichment (e.g. "improve this
    # image"). Forwarded by the frontend from the {route:"image"} routing
    # response. Used as the stored user message content so chat history shows
    # the original phrasing instead of the long "Reference image description: …"
    # enriched prompt. Falls back to prompt when absent (toolbar-shortcut calls
    # where prompt IS the original question, or older frontend builds).
    original_question = (body.get("original_question") or "").strip() or prompt
    # Original uploaded-image attachment_ids forwarded from the /ask routing
    # response (gateway.py img_intent block). Stored on the user ChatMessage
    # row in Postgres (via Kafka) so the L2-img history-inject block in
    # gateway.py can find the image caption on follow-up turns like
    # "explain the image I attached".
    orig_attachment_ids = [
        aid for aid in (body.get("attachment_ids") or []) if aid
    ]
    # provider is hard-pinned to "gemini" — gemini-3.1-flash-image is the
    # only image-capable model on the platform. The legacy `provider` /
    # `selected_model` body keys are silently ignored; the UI no longer
    # sends them. Kept as a constant local so the downstream
    # generate_imagen() signature (which still accepts provider=) stays
    # untouched.
    provider = "gemini"
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")

    # If a chat_id is supplied and the row already exists, verify ownership.
    # The row may NOT exist yet — chat_messages and the parent Chat record
    # are written by the Kafka consumer (`workers/kafka_consumer.py`)
    # asynchronously after `/ask` completes, and the user can fire image-gen
    # while that write is still in flight. We mustn't 404 in that race; just
    # skip the artifact persistence later if the chat row never lands.
    _chat_row_exists = False
    if chat_id:
        try:
            from db.database import SessionLocal
            from db.models import Chat
            _verdb = SessionLocal()
            try:
                _ch = _verdb.query(Chat).filter(Chat.id == chat_id).first()
                if _ch is not None:
                    # Row exists — enforce ownership (admins of other users'
                    # chats must NOT be able to image-gen into them).
                    if str(_ch.user_id) != str(user_id):
                        raise HTTPException(status_code=403, detail="not your chat")
                    _chat_row_exists = True
                # If _ch is None, the chat may simply not be in DB yet —
                # let the image generate and skip artifact persistence.
            finally:
                _verdb.close()
        except HTTPException:
            raise
        except Exception:
            pass

    # Compliance gate is enforced inside gateway_gemini.generate_imagen.
    # return_meta=True so we learn which provider/model ACTUALLY produced
    # the bytes (post-fallback) — the proxy tries gemini-3.1-flash-image
    # first, falls back to OpenAI's gpt-image-1 / dall-e-3 if gemini is
    # unavailable, and surfaces a 503 if BOTH providers are unavailable.
    # Wall-clock latency around the ACTUAL image generation call. There was
    # previously NO latency captured for image generation — text/doc/video
    # responses all surface a latency chip, so image responses now match.
    # perf_counter() is monotonic (immune to clock adjustments) — the same
    # source used by the latency timing above.
    import time as _time
    _latency_sec = 0.0
    try:
        from gateway_gemini import gemini_gateway as _gg
        _img_t0 = _time.perf_counter()
        img_bytes, _img_meta = _gg.generate_imagen(
            prompt=prompt,
            aspect_ratio=aspect,
            number_of_images=1,
            style_suffix=style,
            provider=provider,
            return_meta=True,
        )
        _latency_sec = _time.perf_counter() - _img_t0
    except Exception as e:
        logger.error(f"chat_router: image-generate failed: {e}")
        raise HTTPException(status_code=502, detail=f"image generation failed: {e}")

    if not img_bytes:
        # The "BOTH providers unavailable" path: proxy returned 503 with
        # "image_model_unavailable". Render a clean 503 with a friendly
        # message that the UI displays as a chat reply rather than a hard
        # error — matches the user's spec: "if both can't generate, say
        # model not available".
        if _img_meta.get("unavailable"):
            raise HTTPException(
                status_code=503,
                detail=(
                    # Deliberately does NOT name the models tried. This is an
                    # end-user message, and the ids were the deployment's own
                    # configured image models -- naming them here hardcoded one
                    # deployment's provider choice into user-visible text. The
                    # specific provider/model that failed is already in the
                    # logger.error above, which is where an operator looks.
                    "Image generation is not available right now. "
                    "No configured image model could be reached — please try again later."
                ),
            )
        # Generic transient failure (proxy 5xx, timeout, network blip).
        _detail = _img_meta.get("error") or (
            "image generation returned empty — check ainxt-gateway and "
            "ainxt-llm-proxy logs. All cloud image calls must route "
            "through LLM_PROXY_URL."
        )
        raise HTTPException(status_code=502, detail=f"image generation failed: {_detail}")

    # ── Persist image to disk + audit row (additive, fire-and-forget) ────
    _image_file_id = None
    try:
        from services.image_store import persist_generated_image
        _image_file_id = persist_generated_image(
            user_id=user_id, chat_id=chat_id or None, provider=provider,
            prompt=prompt, img_bytes=img_bytes, mime_type="image/png",
        )
    except Exception:
        pass  # logged inside persist_generated_image; never block the response

    # ── Persist as ChatAttachment so follow-up turns can reference it ─────
    # The history-image-inject block in gateway.py (L2-img) looks up
    # ChatAttachment rows (kind="image") referenced by ChatMessage.attachment_ids
    # to inject a compact image caption into follow-up turns. Without this,
    # "explain the image" after "improve this image" always fails because the
    # generated image has no ChatAttachment row and no attachment_ids in the
    # ChatMessage — the model has no context about what was generated.
    #
    # image_description = the full prompt (what the image depicts).
    # image_caption     = first 2 sentences of the prompt, capped at 600 chars
    #                     (same formula as uploaded-image captions in chat_router).
    _generated_attachment_id = None
    if _image_file_id and chat_id:
        try:
            import re as _re_gencap
            import uuid as _uuid_genatt
            from db.database import SessionLocal as _GenAttDB
            from db.models import ChatAttachment as _GenAttCA
            _gen_att_id = str(_uuid_genatt.uuid4())
            _gen_desc = prompt  # the generation prompt describes what the image contains
            _cap_src = " ".join(_gen_desc.split())
            _cap_sents = _re_gencap.split(r'(?<=[.!?])\s+', _cap_src)
            _gen_cap = (" ".join(_cap_sents[:2]).strip() or _cap_src)[:600]
            _gen_att_db = _GenAttDB()
            try:
                # Same FK guard as /chat/upload — image generation can be the
                # first turn of a brand-new chat, in which case the chats row
                # has not been written yet. See ensure_chat_row.
                ensure_chat_row(_gen_att_db, chat_id, user_id)
                _gen_att_db.add(_GenAttCA(
                    id=_gen_att_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    file_name=f"{_image_file_id}.png",
                    file_type="png",
                    file_size=len(img_bytes),
                    kind="image",
                    storage_path="",          # bytes already in GeneratedImage table
                    parsed_text=_gen_desc,
                    image_description=_gen_desc,
                    image_caption=_gen_cap,
                    created_by=user_id,
                ))
                _gen_att_db.commit()
                _generated_attachment_id = _gen_att_id
                logger.info(
                    f"chat_router: generated-image ChatAttachment persisted "
                    f"attachment_id={_gen_att_id} image_file_id={_image_file_id} "
                    f"caption_chars={len(_gen_cap)}"
                )
            finally:
                _gen_att_db.close()
        except Exception as _gen_att_err:
            logger.warning(f"chat_router: generated-image ChatAttachment persist failed: {_gen_att_err}")

    # Persist as a Canvas artifact (base64-encoded data URL — kept compact in DB)
    artifact_id = None
    try:
        import base64 as _b64
        from db.database import SessionLocal
        from db.models import ChatArtifact, Chat as _ChatModel
        import uuid as _uuid_img
        b64 = _b64.b64encode(img_bytes).decode("ascii")
        data_url = f"data:image/png;base64,{b64}"
        # The artifact needs a parent Chat row (FK target) so the
        # "Generated image" chip (ai-ui/src/components/Chat.jsx artifact
        # chips) renders live AND survives a page refresh. On the
        # race-with-Kafka path the Chat row is still being written
        # asynchronously by the consumer, so _chat_row_exists is False even
        # though the user is looking at a valid chat. Previously we skipped
        # the artifact in that case, which silently dropped the chip for
        # every image generated in a fresh chat. Instead, upsert the Chat
        # row synchronously here so the FK always resolves.
        if chat_id:
            db = SessionLocal()
            try:
                if not _chat_row_exists:
                    # Create the parent Chat row so the artifact FK resolves.
                    # ownership was already validated above (a mismatched
                    # user_id would have raised 403); a missing row means the
                    # Kafka consumer simply hasn't written it yet. Use a title
                    # hint from the prompt; the consumer's later upsert keeps
                    # its own title logic (this only fills the gap).
                    existing = db.query(_ChatModel).filter(_ChatModel.id == chat_id).first()
                    if existing is None:
                        db.add(_ChatModel(
                            id=chat_id,
                            user_id=user_id,
                            title=(prompt[:200] or "New Chat"),
                        ))
                        db.flush()  # ensure the Chat row lands before the artifact insert
                row = ChatArtifact(
                    id=str(_uuid_img.uuid4()),
                    chat_id=chat_id,
                    message_id=body.get("message_id") or None,
                    # Fixed label so the Canvas chip reads "Generated image"
                    # both live and after a page refresh. Previously this was
                    # set to the prompt text, which made the persisted chip
                    # show the raw prompt (e.g. "AiNxt logo, official corporate
                    # branding…") instead of the friendly "Generated image"
                    # label the UI renders live (see ai-ui/src/utils/
                    # imageGenerate.js IMAGE_ARTIFACT_TITLE).
                    title="generated image",
                    artifact_type="svg" if data_url.startswith("data:image/svg") else "html",
                    language=None,
                    content=f'<img src="{data_url}" alt="" style="max-width:100%;height:auto" />',
                    version=1,
                    created_by=user_id,
                )
                db.add(row)
                db.commit()
                artifact_id = str(row.id)
            finally:
                db.close()
    except Exception as _pers_err:
        logger.warning(f"chat_router: image artifact persist skipped: {_pers_err}")

    # Surface the ACTUAL model id that produced the bytes (post-fallback).
    # If gemini-3.1-flash-image ran → "gemini-3.1-flash-image".
    # If the proxy fell back to OpenAI → "gpt-image-1" or "dall-e-3".
    # The chat router itself never hardcodes a model id; it just relays
    # what the gateway/proxy reported. Fall back to the registry default
    # only on very old proxy builds that didn't populate the meta dict.
    from core.model_registry import GEMINI_IMAGE_MODEL as _GEMINI_IMAGE_MODEL
    _model_label     = _img_meta.get("model")    or _GEMINI_IMAGE_MODEL
    _actual_provider = _img_meta.get("provider") or provider

    # Compute cost based on which model ACTUALLY ran:
    #   • gemini-3.1-flash-image → per-token (input + output) using the
    #     real Gemini usage_metadata when available (proxy doesn't
    #     currently propagate token counts → cost is 0, which we accept
    #     rather than fake with a flat rate).
    #   • gpt-image-1 / dall-e-3 → OpenAI doesn't expose token usage for
    #     image generation; their list price is per-image (gpt-image-1 ≈
    #     $0.04 standard, dall-e-3 ≈ $0.04 standard / $0.08 hd). We
    #     report the published per-image rate here so the cost chip is
    #     never wrong by an order of magnitude.
    # No "openai vs everything else" string match — the rate is keyed on
    # the actual model id the proxy reports.
    _OPENAI_IMAGE_FLAT_USD: dict[str, float] = {
        "gpt-image-1": 0.04,
        "dall-e-3":    0.08,   # hd quality (matches the proxy call)
    }
    from gateway_gemini import gemini_gateway as _gg_usage
    _in_tok  = int(getattr(_gg_usage, "_last_input_tokens",  0) or 0)
    _out_tok = int(getattr(_gg_usage, "_last_output_tokens", 0) or 0)
    if _actual_provider == "gemini":
        _img_in_rate_1k, _img_out_rate_1k = _gemini_image_rates_per_1k()
        _img_cost_usd = (
            (_in_tok  / 1000.0) * _img_in_rate_1k +
            (_out_tok / 1000.0) * _img_out_rate_1k
        )
    else:
        # OpenAI fallback — flat per-image, no token concept.
        _img_cost_usd = _OPENAI_IMAGE_FLAT_USD.get(_model_label, 0.04)
        # OpenAI images have no token accounting; zero the token chips
        # rather than pretending the gemini gateway's leftover counts
        # belong to the OpenAI call.
        _in_tok = 0
        _out_tok = 0
    # ── Budget: DEBIT this image's cost from the user's allocation ──────────
    # Image generation is NOT covered by BudgetMiddleware (its path is not in
    # ENFORCED_PATHS) and previously never called increment_usage — so image
    # cost was shown in the chip but never deducted. Mirror the chat/doc paths:
    # skip in-house models (no external API cost), fire-and-forget, non-fatal.
    if user_id and _img_cost_usd > 0:
        try:
            from store.budget_store import increment_usage
            increment_usage(
                user_id,
                tokens   = _in_tok + _out_tok,
                requests = 1,
                cost_usd = _img_cost_usd,
            )
            logger.info(
                f"chat_router: image budget debited user={user_id} "
                f"model={_model_label} provider={_actual_provider} "
                f"in_tok={_in_tok} out_tok={_out_tok} cost_usd={_img_cost_usd:.6f}"
            )
        except Exception as _bud_err:
            # Non-fatal — image already generated; don't 500 the user over a
            # ledger write. Logged for reconciliation.
            logger.warning(
                f"chat_router: image budget debit FAILED user={user_id} "
                f"cost_usd={_img_cost_usd:.6f}: {_bud_err}"
            )
    # ── Persist ChatMessage rows via Kafka so image survives page refresh ──
    # Same pattern as doc_download_router.py:188. The [IMAGE:{id}:{filename}]
    # marker is stored as the assistant message content; the frontend's
    # parseDocMarkers() detects it on reload and renders an <img> tag
    # pointing to GET /chat/image/{id}.
    if _image_file_id and chat_id:
        try:
            import uuid as _uuid_imgmsg
            _img_filename = f"{_image_file_id}.png"
            _img_marker = f"[IMAGE:{_image_file_id}:{_img_filename}]"
            from core.kafka_producer import produce, TOPIC_CHAT_HISTORY
            produce(TOPIC_CHAT_HISTORY, {
                "chat_id":              chat_id,
                "user_id":              user_id,
                # Use the original user question (e.g. "improve this image") so
                # chat history shows the original phrasing, not the long enriched
                # prompt ("Reference image description: …") that was sent to the
                # image model. original_question falls back to prompt for
                # toolbar-shortcut calls where no enrichment happened.
                "question":             original_question,
                "answer":               _img_marker,
                "assistant_message_id": body.get("message_id") or str(_uuid_imgmsg.uuid4()),
                "request_id":           "",
                "title_hint":           (original_question[:400]),
                "model":                _model_label,
                "in_tok":               _in_tok,
                "out_tok":              _out_tok,
                "cost":                 _img_cost_usd,
                # Persist wall-clock latency so the chip survives a refresh.
                # The chat-history consumer (workers/kafka_consumer.py:320)
                # writes this into ChatMessage.latency; previously it was
                # dropped from the image payload so reloaded images showed
                # no latency chip.
                "latency":              _latency_sec,
                # attachment_ids on the USER message row — BOTH the original
                # uploaded image (forwarded from /ask via the request body) AND
                # the generated image's ChatAttachment. The L2 attachment-context
                # block in gateway.py discovers replayable descriptions purely by
                # reading ChatMessage.attachment_ids, so an id that is not in
                # this list is invisible to every follow-up turn.
                #
                # This used to be `orig_attachment_ids if orig_attachment_ids
                # else [_generated_attachment_id]` — an either/or. Whenever the
                # user had uploaded an image (the common "improve this image"
                # flow), the GENERATED image's id was dropped from the list and
                # only published under "generated_attachment_id", a field no
                # consumer reads (workers/kafka_consumer.py ignores it). So the
                # generated image's ChatAttachment row was orphaned on write and
                # "what did you just generate?" had no context. Union both.
                "attachment_ids":       [
                    _aid for _aid in (
                        list(orig_attachment_ids or [])
                        + ([_generated_attachment_id] if _generated_attachment_id else [])
                    ) if _aid
                ],
            }, key=chat_id)
        except Exception as _kf_err:
            # Non-fatal — image still shows inline; only refresh persistence is lost.
            logger.warning(f"chat_router: image chat_history publish failed: {_kf_err}")

    # ── Write image turn to Redis so follow-up turns see history immediately ──
    # Kafka → Postgres is async (consumer lag can be seconds). The /ask history
    # loader tries Redis first; if the image-gen turn is not there, _messages is
    # empty → _has_history=False → _is_followup=False → cil-clarify fires on
    # "explain the image" even though there IS a prior turn. Writing to Redis
    # here (same pattern as _general_stream / response_stream in gateway.py)
    # makes the turn visible to the very next /ask call with zero lag.
    if chat_id and prompt:
        try:
            from memory.redis_memory import RedisMemory as _RMImg
            _rm_img = _RMImg()
            _img_caption = _gen_cap if '_gen_cap' in dir() else prompt[:200]

            # If the user originally uploaded an image (orig_attachment_ids),
            # fetch its caption and include it in the Redis user-turn content
            # so the L2-img block in gateway.py can inject it on follow-up
            # turns ("explain the image I attached"). Without this, the
            # uploaded image's context is lost after the image-gen turn because
            # /ask returned early (before its normal Redis/Kafka write path).
            _orig_img_ctx = ""
            if orig_attachment_ids:
                try:
                    from db.database import SessionLocal as _OrigRedisDB
                    from db.models import ChatAttachment as _OrigRedisCA
                    _ordb = _OrigRedisDB()
                    try:
                        _orig_att_row = (
                            _ordb.query(_OrigRedisCA)
                            .filter(_OrigRedisCA.id == orig_attachment_ids[0])
                            .first()
                        )
                        if _orig_att_row:
                            _orig_cap = (
                                _orig_att_row.image_caption
                                or (_orig_att_row.image_description or "")[:600]
                                or (_orig_att_row.parsed_text or "")[:400]
                            ).strip()
                            if _orig_cap:
                                _orig_img_ctx = f"\n[Attached image: {_orig_cap[:400]}]"
                    finally:
                        _ordb.close()
                except Exception:
                    pass

            _rm_img.save_message(
                chat_id, "user",
                f"{prompt}{_orig_img_ctx}",
                metadata={"rag_mode": "off", "source": "image_gen",
                          "attachment_ids": orig_attachment_ids},
            )
            _rm_img.save_message(chat_id, "assistant",
                                 f"[Generated image based on: {_img_caption}]",
                                 metadata={"rag_mode": "off", "source": "image_gen",
                                           "model": _model_label})
            logger.info(
                f"chat_router: image turn written to Redis for chat_id={chat_id!r} "
                f"orig_att_ids={orig_attachment_ids}"
            )
        except Exception as _redis_img_err:
            logger.debug(f"chat_router: Redis image turn write skipped: {_redis_img_err}")

    # Return the image bytes inline so the client can drop it into the chat
    # immediately. The artifact_id is returned in a header so the UI can
    # also offer "Open in Canvas".
    from fastapi.responses import Response as _ImgResp
    return _ImgResp(
        content=img_bytes,
        media_type="image/png",
        headers={
            "X-Artifact-Id":   artifact_id or "",
            "X-Aspect":        aspect,
            # Standard meta headers (parsed by ai-ui/src/utils/imageGenerate.js)
            # so MessageMeta can show cost/token/budget chips alongside the
            # image, matching chat/doc response footers.
            "X-Cost-USD":      f"{_img_cost_usd:.6f}",
            "X-Input-Tokens":  str(_in_tok),
            "X-Output-Tokens": str(_out_tok),
            "X-Token-Usage":   str(_in_tok + _out_tok),
            # Wall-clock seconds around generate_imagen() — parsed by
            # ai-ui/src/utils/imageGenerate.js so the latency chip renders
            # live, and persisted via Kafka so it survives a refresh.
            "X-Latency-Sec":   f"{_latency_sec:.3f}",
            # Real model id + provider — single source of truth for the
            # model chip. When the fallback kicked in these will be
            # "gpt-image-1" / "openai" instead of the requested values.
            "X-Model-Label":   _model_label,
            "X-Provider":      _actual_provider,
        },
    )

# ============================================================
# IMAGE SERVING  (authenticated download for persisted images)
#
# GET /chat/image/{image_id}  — serves a generated image from disk.
# Auth required. Mirrors the GET /chat/video/{video_id} pattern.
# The frontend's parseDocMarkers() renders [IMAGE:{id}:{filename}]
# markers as <img src="/chat/image/{id}"> after page refresh.
# ============================================================

@router.get("/chat/image/{image_id}")
def chat_get_image(
        image_id: str,
        current_user: dict = Depends(get_current_user),
):
    """Serve a generated image from IMAGE_STORAGE_DIR.

    Auth required. The image file is NOT served via StaticFiles — every
    fetch goes through this handler so JWT-auth is always enforced.
    """
    from fastapi.responses import Response as _ImgServeResp
    from db.database import SessionLocal
    from db.models import GeneratedImage

    db = SessionLocal()
    try:
        row = db.query(GeneratedImage).filter(GeneratedImage.id == image_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="image not found")

        file_path = row.file_path
        mime_type = row.mime_type or "image/png"
    finally:
        db.close()

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="image file not found on disk")

    with open(file_path, "rb") as fh:
        data = fh.read()

    return _ImgServeResp(
        content=data,
        media_type=mime_type,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{row.filename}"',
        },
    )

# ============================================================
# VIDEO GENERATION  (Veo 3.1 — chat-UI-only)
#
# POST /chat/video-generate   submit prompt, run LRO, persist MP4
# GET  /chat/video/{id}       authenticated stream w/ HTTP Range
#
# Gating layers (defense-in-depth):
#   1. client_source must be "platform" (web Chat UI)
#   2. VEO_ENABLED must be true
#   Per-user access is governed by model governance tables
#   (dept_model_permissions / user_model_permissions) — the same
#   mechanism used for every other model.
#
# Storage: filesystem under MEDIA_DIR/videos/{uuid}.mp4. We do NOT
# mount /media via StaticFiles — every read goes through the
# authenticated GET below.
# ============================================================

# Video duration window — PRODUCT policy (4–8 s), not the provider limit. Veo
# accepts a wider range, but web chat only ever asks for 4–8 s so per-request
# cost (Veo bills per SECOND) and the proxy LRO timeout stay predictable.
# Sourced from cil/intent.py so this clamp, the CIL prompt schema, the gateway
# routing clamp, and the frontend all move together. Falls back to literals if
# the import fails so this module never becomes unimportable over a constant.
try:
    from cil.intent import (
        _VID_MIN_DURATION as _VEO_MIN_DURATION,
        _VID_MAX_DURATION as _VEO_MAX_DURATION,
        _VID_DEFAULT_DURATION as _VEO_DEFAULT_DURATION,
    )
except Exception:  # noqa: BLE001
    _VEO_MIN_DURATION, _VEO_MAX_DURATION, _VEO_DEFAULT_DURATION = 4, 8, 8

import os
import re
from pathlib import Path

def _media_dir() -> str:
    p = Path(os.getenv("MEDIA_DIR", "./media")) / "videos"
    # 0o700 — user-generated media is auth-gated by the route; dir perms harden
    # against accidental world-readable mounts (mirrors broadcast_router pattern).
    p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return str(p)

def _veo_video_path(video_id: str) -> str:
    """Resolve and verify video_id is a safe filename (no path traversal)."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,64}", video_id or ""):
        raise HTTPException(status_code=400, detail="invalid video id")
    return str(Path(_media_dir()) / f"{video_id}.mp4")

@router.post("/chat/video-generate")
def chat_generate_video(
        body: dict,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """Generate a short Veo 3.1 video. See banner above for gating policy."""
    from core.model_registry import (
        VEO_ENABLED, VEO_COST_PER_SECOND, is_veo_allowed_for_user, veo_model as _veo_model,
    )
    from middleware.client_source_middleware import CLIENT_PLATFORM

    # Resolved once up front (env override → an enabled "gemini" registry model
    # tagged "video" → "") and reused for the budget check below, the actual
    # dispatch call, the audit row, and the response payload — previously each
    # of those independently read the raw VEO_MODEL constant, which is blank on
    # a deployment configured purely through the admin "LLM Providers" screen.
    resolved_veo_model = _veo_model()

    # Gate 1: chat-UI-only. CLI/IDE that craft this POST directly get 403.
    if getattr(request.state, "client_source", CLIENT_PLATFORM) != CLIENT_PLATFORM:
        raise HTTPException(status_code=403, detail="video generation is chat-UI-only")

    # Gate 2: global enable + ad_level 0 / admin check. Fails closed.
    if not VEO_ENABLED or not is_veo_allowed_for_user(current_user):
        raise HTTPException(status_code=403, detail="veo is not enabled for this user")

    prompt   = (body.get("prompt") or "").strip()
    chat_id  = body.get("chat_id") or ""
    aspect   = body.get("aspect_ratio") or "16:9"
    # The user's original question before /ask enrichment (e.g. "generate a
    # video from this image"). Forwarded by the frontend from the {route:"video"}
    # routing response. Used as the stored user message content so chat history
    # shows the original phrasing instead of the long enriched prompt.
    # Falls back to prompt when absent (direct calls or older frontend builds).
    original_question = (body.get("original_question") or "").strip() or prompt
    try:
        duration_secs = int(body.get("duration_secs") or _VEO_DEFAULT_DURATION)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration_secs must be an integer")
    # Authoritative clamp to the 4–8 s product window. Deliberately a clamp and
    # not a 400: the CIL and the frontend both already constrain this, so an
    # out-of-range value here means a stale client, not user error — silently
    # correcting it is better UX than failing the (already paid for) request.
    _requested_duration = duration_secs
    duration_secs = max(_VEO_MIN_DURATION, min(_VEO_MAX_DURATION, duration_secs))
    if duration_secs != _requested_duration:
        logger.info(
            f"chat_router: veo duration clamped {_requested_duration}s → "
            f"{duration_secs}s (policy window {_VEO_MIN_DURATION}-{_VEO_MAX_DURATION}s)"
        )

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthenticated")

    # ── Budget gate (cost is per-second, not per-token) ─────────────────────
    # Veo is expensive ($0.40/sec default), so the budget check is AUTHORITATIVE:
    # we project the per-second cost for this request, then verify (current
    # cumulative spend) + (this request's cost) would not exceed the user's
    # allocation. Unlike text/image flows, we do NOT fail-open here — if the
    # budget store is unreachable, deny the request. The per-call $ blast radius
    # is too high to allow silent passthrough.
    # Per-second rate is a flat platform constant, not keyed by which Veo model
    # variant is actually dispatched — resolved_veo_model (used below for the
    # dispatch call, audit row, and response payload) is a separate concern.
    cost_usd = duration_secs * float(VEO_COST_PER_SECOND)
    try:
        from store.budget_store import (
            check_budget, get_budget, get_usage_total,
            DEFAULT_COST_LIMIT_USD,
        )
        b = check_budget(user_id)
        if b.get("allowed") is not True:
            raise HTTPException(
                status_code=429,
                detail=b.get("reason", "Budget exhausted"),
            )
        # Projection check: even if current spend is under the cap, this Veo
        # request's projected cost must also fit. Prevents one expensive video
        # from blowing past the cap by a wide margin.
        _bud_row   = get_budget(user_id) or {}
        _usage_row = get_usage_total(user_id) or {}
        _spent     = float(_usage_row.get("cost_usd_spent", 0.0))
        _max_cost  = float(_bud_row.get("max_cost_usd_total") or DEFAULT_COST_LIMIT_USD)
        if _max_cost and (_spent + cost_usd) > _max_cost:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Veo request would exceed your budget "
                    f"(${_spent:.2f} spent + ${cost_usd:.2f} projected > "
                    f"${_max_cost:.2f} allocated). "
                    f"Reduce duration_secs or contact your admin to top up."
                ),
            )
    except HTTPException:
        raise
    except Exception as _bud_err:
        # FAIL-CLOSED for Veo: financial blast radius is too high to allow
        # silent passthrough on budget-store outage. Other media flows
        # (text/image) fail-open intentionally; Veo does NOT.
        logger.error(f"chat_router: veo budget check failed → denying: {_bud_err}")
        raise HTTPException(
            status_code=503,
            detail="budget service unavailable — video generation temporarily disabled",
        )

    # ── Optional chat ownership check (race with Kafka write OK) ──
    _chat_row_exists = False
    if chat_id:
        try:
            from db.database import SessionLocal
            from db.models import Chat
            _verdb = SessionLocal()
            try:
                _ch = _verdb.query(Chat).filter(Chat.id == chat_id).first()
                if _ch is not None:
                    if str(_ch.user_id) != str(user_id):
                        raise HTTPException(status_code=403, detail="not your chat")
                    _chat_row_exists = True
            finally:
                _verdb.close()
        except HTTPException:
            raise
        except Exception:
            pass

    # ── Generate via Gemini gateway (LRO poll inside) ──────────
    # Wall-clock the entire LRO so the model_usages row carries an accurate
    # latency_ms — Veo LROs take 60-180s and the latency is the most useful
    # signal for cost/perf dashboards.
    import time as _t_veo
    _veo_t0 = _t_veo.monotonic()
    try:
        from gateway_gemini import gemini_gateway
        video_bytes, meta = gemini_gateway.generate_veo_video(
            prompt=prompt,
            aspect_ratio=aspect,
            duration_secs=duration_secs,
            model=resolved_veo_model,
        )
    except Exception as e:
        logger.error(f"chat_router: video-generate failed: {e}")
        raise HTTPException(status_code=502, detail=f"video generation failed: {e}")
    _veo_latency_ms = (_t_veo.monotonic() - _veo_t0) * 1000.0

    if not video_bytes:
        detail = (meta or {}).get("error") or "video generation returned empty"
        raise HTTPException(status_code=502, detail=f"video generation failed: {detail}")

    # Persist MP4 to filesystem
    video_id = uuid.uuid4().hex
    out_path = _veo_video_path(video_id)
    try:
        with open(out_path, "wb") as fh:
            fh.write(video_bytes)
    except Exception as e:
        logger.error(f"chat_router: failed to write video to disk: {e}")
        raise HTTPException(status_code=500, detail="failed to persist video")

    # ── Bill the individual's budget (per-second cost) ─────────────────
    # Veo is billed on output duration, not tokens — we pass tokens=0 so the
    # tokens_used counter is not polluted, and cost_usd carries the full
    # per-second charge. requests=1 (default) increments the request counter.
    # A billing failure here is logged but does NOT 500 the user: the video
    # is already generated and persisted. The accompanying model_usages
    # write below is the durable audit trail; the budget_store ledger is the
    # fast-path. Both are written for defense-in-depth.
    _billed_ok = False
    try:
        from store.budget_store import increment_usage
        increment_usage(user_id, tokens=0, cost_usd=cost_usd)
        _billed_ok = True
    except Exception as bill_err:
        # Surface — do not swallow. SREs need to know if Redis+PG ledger
        # writes are failing because that means budget caps are not being
        # enforced for subsequent requests either.
        logger.error(
            f"chat_router: veo budget deduction FAILED user={user_id} "
            f"cost_usd={cost_usd:.4f} model={resolved_veo_model} → {bill_err}"
        )

    # ── Audit/governance: write model_usages row ──────────────────────
    # Same destination text/image chat-ask uses (PostgresMemory.create_model_usage).
    # endpoint mirrors the route path so admin dashboards can group "Veo" spend
    # by endpoint without inferring it from the model name. tokens=0 because
    # Veo bills per-second, not per-token; duration_secs is reflected via
    # cost_usd (= duration × per-second rate) and latency_ms (wall-clock LRO).
    # Derive surface: desktop app → DESKTOP-CHAT, browser → WEB-CHAT.
    _veo_cs = getattr(request.state, "client_source", "platform")
    _veo_channel = "DESKTOP-CHAT" if _veo_cs == "desktop" else "WEB-CHAT"
    try:
        from memory.postgres_memory import PostgresMemory as _PM
        _PM().create_model_usage(
            model=resolved_veo_model,
            user_id=user_id,
            endpoint="/chat/video-generate",
            source_channel=_veo_channel,
            input_tokens=0,
            output_tokens=0,
            cost_usd=round(float(cost_usd), 6),
            latency_ms=float(_veo_latency_ms),
        )
    except Exception as _mu_err:
        # model_usages is the durable audit trail — log loudly but do NOT
        # 500 the user (video already on disk + budget already deducted).
        logger.error(
            f"chat_router: veo model_usages audit write FAILED user={user_id} "
            f"model={resolved_veo_model} cost_usd={cost_usd:.4f} → {_mu_err}"
        )

    # Persist artifact pointer (so the message survives reload).
    # artifact_type="html" because there's no dedicated "video" enum value yet —
    # the HTML wrapper renders the same in the Canvas drawer.
    artifact_id = None
    try:
        from db.database import SessionLocal
        from db.models import ChatArtifact
        video_url = f"/chat/video/{video_id}"
        if chat_id and _chat_row_exists:
            db = SessionLocal()
            try:
                row = ChatArtifact(
                    id=str(uuid.uuid4()),
                    chat_id=chat_id,
                    message_id=body.get("message_id") or None,
                    title=(prompt[:120] + "…" if len(prompt) > 120 else prompt) or "Generated video",
                    artifact_type="html",
                    language=None,
                    content=(
                        f'<video controls preload="metadata" style="max-width:100%;height:auto">'
                        f'<source src="{video_url}" type="video/mp4"/></video>'
                    ),
                    version=1,
                    created_by=user_id,
                )
                db.add(row)
                db.commit()
                artifact_id = str(row.id)
            finally:
                db.close()
    except Exception as _pers_err:
        logger.warning(f"chat_router: video artifact persist skipped: {_pers_err}")

    # ── Persist chat turn via Kafka so video survives page refresh ────────────
    # Previously missing entirely — video turns were never written to Postgres,
    # so reloading the chat showed an empty history for video generation turns.
    # We store the original user question (not the enriched prompt) so history
    # shows "generate a video from this image" rather than the long
    # "Reference image description: …" text that was sent to the Veo model.
    _veo_latency_sec = _veo_latency_ms / 1000.0
    if chat_id:
        try:
            import uuid as _uuid_vidmsg
            # Store the video URL marker so the frontend can reconstruct the
            # <video> player on reload. Format mirrors the image marker pattern:
            # [VIDEO:{video_id}:{filename}] — parsed by Message.jsx parseDocMarkers.
            _vid_marker = f"[VIDEO:{video_id}:{video_id}.mp4]"
            from core.kafka_producer import produce as _vid_produce, TOPIC_CHAT_HISTORY as _VID_TCH
            _vid_produce(_VID_TCH, {
                "chat_id":              chat_id,
                "user_id":              user_id,
                # original_question is the user's phrasing before enrichment;
                # falls back to prompt for direct/toolbar calls.
                "question":             original_question,
                "answer":               _vid_marker,
                "assistant_message_id": body.get("message_id") or str(_uuid_vidmsg.uuid4()),
                "request_id":           "",
                "title_hint":           original_question[:400],
                "model":                resolved_veo_model,
                "in_tok":               0,   # Veo is per-second billed, not per-token
                "out_tok":              0,
                "cost":                 round(float(cost_usd), 6),
                "latency":              _veo_latency_sec,
                # Link the uploaded reference image to this turn so follow-ups
                # ("what car is it?") can replay its vision description, and set
                # rag_mode explicitly — a NULL rag_mode used to make this turn
                # invisible to Generic history reads in gateway.py.
                "attachment_ids":       list(body.get("attachment_ids") or []),
                "rag_mode":             (body.get("rag_mode") or "off"),
            }, key=chat_id)
            logger.info(
                f"chat_router: video chat_history published "
                f"chat_id={chat_id!r} video_id={video_id!r}"
            )
        except Exception as _vid_kf_err:
            # Non-fatal — video still plays inline; only refresh persistence is lost.
            logger.warning(f"chat_router: video chat_history publish failed: {_vid_kf_err}")

    # Response payload mirrors the text-chat shape so the UI can render the
    # standard MessageMeta footer (model · tokens · cost · latency) under the
    # generated video — identical UX to /ask responses.
    #
    # tokens=0: Veo is billed per-second, not per-token. The duration field
    # carries the per-second analog and is rendered as a separate chip.
    return {
        "video_id":    video_id,
        "url":         f"/chat/video/{video_id}",
        "mime":        "video/mp4",
        "duration":    duration_secs,
        "duration_secs": duration_secs,        # explicit alias for UI footer
        "cost_usd":    round(float(cost_usd), 6),
        "model":       resolved_veo_model,
        "tokens":      0,                       # Veo is per-second billed
        "input_tokens":  0,
        "output_tokens": 0,
        "total_tokens":  0,
        "latency_ms":  round(float(_veo_latency_ms), 1),
        "endpoint":    "/chat/video-generate",
        "billed":      _billed_ok,
        "artifact_id": artifact_id,
    }

@router.get("/chat/video/{video_id}")
def chat_get_video(
        video_id: str,
        request: Request,
        current_user: dict = Depends(get_current_user),
):
    """Stream a generated MP4 with HTTP Range support.

    Auth required. The video file is stored under MEDIA_DIR/videos and is
    NOT served via StaticFiles — every fetch goes through this handler so
    JWT-auth is always enforced.
    """
    from fastapi.responses import Response
    path = _veo_video_path(video_id)
    # Single stat — avoids the exists/getsize TOCTOU + double-stat pattern.
    try:
        file_size = os.path.getsize(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="video not found")

    base_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=3600",
    }
    range_hdr = request.headers.get("range") or request.headers.get("Range")

    # No Range → return whole file (mostly hit by the download button / curl).
    if not range_hdr:
        with open(path, "rb") as fh:
            data = fh.read()
        return Response(
            content=data,
            media_type="video/mp4",
            headers={**base_headers, "Content-Length": str(file_size)},
        )

    # Parse "bytes=START-END"
    try:
        units, _, rng = range_hdr.partition("=")
        if units.strip().lower() != "bytes":
            raise ValueError("only bytes ranges supported")
        start_s, _, end_s = rng.partition("-")
        start = int(start_s) if start_s else 0
        end   = int(end_s) if end_s else file_size - 1
        if start < 0 or end >= file_size or start > end:
            raise ValueError("range out of bounds")
    except Exception:
        raise HTTPException(
            status_code=416,
            detail="invalid Range",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    length = end - start + 1
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read(length)
    return Response(
        status_code=206,
        content=chunk,
        media_type="video/mp4",
        headers={
            **base_headers,
            "Content-Range":  f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(length),
        },
    )

# ============================================================
# ARTIFACTS / CANVAS
#
# Self-contained HTML / SVG / Mermaid / Markdown / code blocks
# generated inside a chat. Rendered in an iframe-sandboxed side
# drawer in the UI. The detection is performed on the frontend
# (we don't want to parse every assistant turn server-side) and
# persisted through these endpoints.
# ============================================================

@router.post("/chats/{chat_id}/artifacts")
def create_artifact(chat_id: str, body: dict, current_user: dict = Depends(get_current_user)):
    """Persist an artifact extracted from an assistant message."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    artifact_type = (body.get("type") or "").strip().lower()
    if artifact_type not in {"html", "react", "svg", "markdown", "mermaid", "code"}:
        raise HTTPException(status_code=400, detail="invalid artifact type")
    content = (body.get("content") or "")[:200_000]
    if not content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    title = (body.get("title") or "Untitled")[:200]

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED) —
    # title is checked as free text; content is scanned for the genuinely
    # dangerous XSS subset only (html/react/svg artifacts legitimately
    # contain markup).
    _is_valid, _field_errors, _sanitized_art = validate_chat_artifact_request(
        {"title": title, "content": content}
    )
    if not _is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(_field_errors))
    title = _sanitized_art["title"][:200]
    content = _sanitized_art["content"][:200_000]

    language = (body.get("language") or None)
    message_id = body.get("message_id") or None
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatArtifact
        import uuid as _uuid_a
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            row = ChatArtifact(
                id=str(_uuid_a.uuid4()),
                chat_id=chat_id,
                message_id=message_id,
                title=title,
                artifact_type=artifact_type,
                language=language,
                content=content,
                version=1,
                created_by=user_id,
            )
            db.add(row)
            db.commit()
            return {
                "id":           str(row.id),
                "chat_id":      chat_id,
                "title":        title,
                "type":         artifact_type,
                "language":     language,
                "version":      1,
                "created_at":   row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/chats/{chat_id}/artifacts")
def list_artifacts(chat_id: str, current_user: dict = Depends(get_current_user)):
    """List artifacts for a chat (newest first)."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatArtifact
        from sqlalchemy import desc
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            rows = (
                db.query(ChatArtifact)
                .filter(ChatArtifact.chat_id == chat_id)
                .order_by(desc(ChatArtifact.created_at))
                .all()
            )
            return {
                "artifacts": [
                    {
                        "id":         str(r.id),
                        "title":      r.title,
                        "type":       r.artifact_type,
                        "language":   r.language,
                        "version":    r.version,
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/chats/{chat_id}/artifacts/{artifact_id}")
def get_artifact(chat_id: str, artifact_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch one artifact in full (content included)."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatArtifact
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            row = db.query(ChatArtifact).filter(
                ChatArtifact.id == artifact_id,
                ChatArtifact.chat_id == chat_id,
                ).first()
            if not row:
                raise HTTPException(status_code=404, detail="Artifact not found")
            return {
                "id":         str(row.id),
                "title":      row.title,
                "type":       row.artifact_type,
                "language":   row.language,
                "content":    row.content,
                "version":    row.version,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# PUBLIC SHARE LINKS
#
# POST /chats/{id}/share              → create read-only token
# GET  /shared/{token}                → public, no auth required
# ============================================================

@router.post("/chats/{chat_id}/share")
def create_chat_share(chat_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """Create a public read-only share link for a chat (snapshot semantics)."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage, ChatShare
        import secrets as _secrets
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.created_at.asc())
                .limit(500)
                .all()
            )
            snapshot = {
                "title":    chat.title or "Shared chat",
                "messages": [
                    {
                        "role":       m.role,
                        "content":    m.content or "",
                        "model_used": m.model_used,
                        "created_at": m.created_at.isoformat() if m.created_at else None,
                    }
                    for m in msgs
                ],
                "created_at": _dt.datetime.utcnow().isoformat(),
            }
            token = _secrets.token_urlsafe(32)
            row = ChatShare(
                token=token,
                chat_id=chat_id,
                owner_id=user_id,
                snapshot=snapshot,
            )
            db.add(row)
            db.commit()
            base = _resolve_share_base_url(request)
            url  = f"{base}/shared/{token}" if base else f"/shared/{token}"
            return {"token": token, "url": url}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _resolve_share_base_url(request: Request) -> str:
    """Derive a public base URL for share links.

    Resolution order:
      1. ``Origin`` header  — set by browsers on POST requests; gives the
         exact scheme + host the user is accessing (works behind proxies).
      2. ``Referer`` header — fallback; strip the path to get the origin.
      3. ``Host`` / ``X-Forwarded-Host`` + ``X-Forwarded-Proto`` headers —
         reconstructed from reverse-proxy headers.
      4. ``PLATFORM_BASE_URL`` env var — explicit override for
         environments where headers are unreliable.

    Returns an empty string only if *none* of the above yield a usable value.
    """
    import os as _os
    from urllib.parse import urlparse as _urlparse

    # 1. Origin header (most reliable for browser-initiated requests)
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if origin and not _is_localhost(origin):
        return origin

    # 2. Referer header (strip path)
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        parsed = _urlparse(referer)
        ref_origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        if ref_origin and parsed.netloc and not _is_localhost(ref_origin):
            return ref_origin

    # 3. Reconstruct from Host / X-Forwarded-* headers
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or ""
    ).strip()
    if host and not _is_localhost(host):
        scheme = (request.headers.get("x-forwarded-proto") or "https").strip()
        return f"{scheme}://{host}".rstrip("/")

    # 4. PLATFORM_BASE_URL env var (explicit fallback)
    env_base = (_os.getenv("PLATFORM_BASE_URL") or "").strip().rstrip("/")
    if env_base and not _is_localhost(env_base):
        return env_base

    # 5. Last resort — use whatever Origin/Host we have, even if localhost,
    #    so the frontend fallback (window.location.origin) can still work.
    return origin or env_base or ""

def _is_localhost(url_or_host: str) -> bool:
    """Return True if the URL or host string points to localhost / 127.0.0.1."""
    lower = url_or_host.lower()
    # Handle full URLs and bare host:port alike
    for marker in ("localhost", "127.0.0.1", "[::1]"):
        if marker in lower:
            return True
    return False

# This endpoint is intentionally registered without the standard auth
# dependency so /shared/{token} is publicly accessible.
_public_share_router = APIRouter(tags=["chat"])

@_public_share_router.get("/shared/{token}")
def get_shared_chat(token: str):
    """Return the read-only snapshot of a shared chat (no auth required)."""
    try:
        from db.database import SessionLocal
        from db.models import ChatShare
        db = SessionLocal()
        try:
            row = db.query(ChatShare).filter(ChatShare.token == token).first()
            if not row:
                raise HTTPException(status_code=404, detail="Shared chat not found or revoked")
            return row.snapshot or {}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# SAVED PROMPT TEMPLATES
# ============================================================

@router.get("/prompt-templates")
def list_prompt_templates(current_user: dict = Depends(get_current_user)):
    """Return the caller's prompt templates (private + org-visible)."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import PromptTemplate
        from sqlalchemy import or_, desc
        db = SessionLocal()
        try:
            rows = (
                db.query(PromptTemplate)
                .filter(or_(PromptTemplate.user_id == user_id,
                            PromptTemplate.scope == "org"))
                .order_by(desc(PromptTemplate.created_at))
                .all()
            )
            return {
                "templates": [
                    {
                        "id":         str(r.id),
                        "name":       r.name,
                        "body":       r.body,
                        "scope":      r.scope,
                        "mine":       (str(r.user_id) == user_id),
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            }
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/prompt-templates")
def create_prompt_template(body: dict, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    name = (body.get("name") or "").strip()[:120]
    body_text = (body.get("body") or "").strip()
    scope = body.get("scope") or "private"
    if scope not in {"private", "org"}:
        scope = "private"
    if not name or not body_text:
        raise HTTPException(status_code=400, detail="name and body are required")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _is_valid, _field_errors, _sanitized_tpl = validate_prompt_template_request(
        {"name": name, "body": body_text}
    )
    if not _is_valid:
        raise HTTPException(status_code=400, detail=_flatten_errors(_field_errors))
    name = _sanitized_tpl["name"][:120]
    body_text = _sanitized_tpl["body"]
    try:
        from db.database import SessionLocal
        from db.models import PromptTemplate
        import uuid as _uuid_pt
        db = SessionLocal()
        try:
            row = PromptTemplate(
                id=str(_uuid_pt.uuid4()),
                user_id=user_id,
                name=name,
                body=body_text,
                scope=scope,
            )
            db.add(row)
            db.commit()
            return {"id": str(row.id), "name": name, "scope": scope}
        finally:
            db.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/prompt-templates/{tpl_id}")
def delete_prompt_template(tpl_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import PromptTemplate
        db = SessionLocal()
        try:
            row = db.query(PromptTemplate).filter(
                PromptTemplate.id == tpl_id,
                PromptTemplate.user_id == user_id,
                ).first()
            if not row:
                raise HTTPException(status_code=404, detail="Template not found")
            db.delete(row)
            db.commit()
            return {"deleted": True}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# EDIT + BRANCH PAST MESSAGES
#
# Editing a user message creates a new version row (message_versions),
# truncates the conversation to that point, and the next /ask call
# will produce a fresh assistant turn — i.e. a new branch.
# Branch traversal: each version row carries parent_id + root_id.
# ============================================================

@router.post("/chats/{chat_id}/messages/{message_id}/edit")
def edit_message(
        chat_id: str,
        message_id: str,
        body: dict,
        current_user: dict = Depends(get_current_user),
):
    """Edit a past user message in-place, archive the old version into
    message_versions, and delete all subsequent messages so the next
    /ask creates a fresh assistant turn on the new branch.
    Returns the new active message id + chat state.
    """
    new_content = (body.get("content") or "").strip()
    if not new_content:
        raise HTTPException(status_code=400, detail="content is required")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, new_content = validate_free_text(new_content)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"content": _errs}))

    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage, MessageVersion
        import uuid as _uuid_mod
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            msg = db.query(ChatMessage).filter(
                ChatMessage.id == message_id,
                ChatMessage.chat_id == chat_id,
                ).first()
            if not msg:
                raise HTTPException(status_code=404, detail="Message not found")
            if msg.role != "user":
                raise HTTPException(status_code=400, detail="Only user messages can be edited")

            # 1. Archive current content as a version row.
            #    Find the parent (latest existing version, if any).
            parent_row = (
                db.query(MessageVersion)
                .filter(MessageVersion.message_id == message_id)
                .order_by(MessageVersion.created_at.desc())
                .first()
            )
            root_id = (parent_row.root_id if parent_row else message_id)
            new_version = ((parent_row.version + 1) if parent_row else 2)

            # Deactivate previous versions
            if parent_row:
                db.query(MessageVersion).filter(
                    MessageVersion.message_id == message_id,
                    ).update({"is_active": False})

            # Archive the pre-edit content as the FIRST version (if not yet present)
            if not parent_row:
                db.add(MessageVersion(
                    id=str(_uuid_mod.uuid4()),
                    message_id=message_id,
                    chat_id=chat_id,
                    parent_id=None,
                    root_id=message_id,
                    role=msg.role,
                    content=msg.content or "",
                    version=1,
                    is_active=False,
                ))

            # Add the new (edited) version row
            db.add(MessageVersion(
                id=str(_uuid_mod.uuid4()),
                message_id=message_id,
                chat_id=chat_id,
                parent_id=(parent_row.id if parent_row else None),
                root_id=root_id,
                role=msg.role,
                content=new_content,
                version=new_version,
                is_active=True,
            ))

            # 2. Update the live ChatMessage row to the new content
            msg.content = new_content

            # 3. Delete all messages that came AFTER this one (any role).
            #    These belong to the old branch and would corrupt context.
            db.query(ChatMessage).filter(
                ChatMessage.chat_id == chat_id,
                ChatMessage.created_at > msg.created_at,
                ).delete(synchronize_session=False)

            chat.updated_at = _dt.datetime.utcnow()
            db.commit()
            return {
                "edited":     True,
                "message_id": message_id,
                "version":    new_version,
                "root_id":    root_id,
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/chats/{chat_id}/messages/{message_id}/versions")
def list_message_versions(
        chat_id: str,
        message_id: str,
        current_user: dict = Depends(get_current_user),
):
    """Return the version history for a single message (oldest first)."""
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, MessageVersion
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            rows = (
                db.query(MessageVersion)
                .filter(MessageVersion.message_id == message_id)
                .order_by(MessageVersion.version.asc())
                .all()
            )
            return {
                "versions": [
                    {
                        "id":         str(r.id),
                        "version":    r.version,
                        "parent_id":  str(r.parent_id) if r.parent_id else None,
                        "content":    r.content,
                        "is_active":  bool(r.is_active),
                        "created_at": r.created_at.isoformat() if r.created_at else None,
                    }
                    for r in rows
                ]
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# AUTO-TITLE (LLM-generated, 4–7 words)
# ============================================================

@router.post("/chats/{chat_id}/auto-title")
def auto_title_chat(chat_id: str, current_user: dict = Depends(get_current_user)):
    """Generate a concise 4–7 word title for a chat using Claude Haiku.

    Pulls the first user question and the first assistant answer from
    chat_messages, asks the LLM for a short title, and persists it to
    chats.title. Returns the new title.
    """
    import json as _json
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat, ChatMessage
        from models.model_router import model_router
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            msgs = (
                db.query(ChatMessage)
                .filter(ChatMessage.chat_id == chat_id, ChatMessage.role.in_(["user", "assistant"]))
                .order_by(ChatMessage.created_at.asc())
                .limit(4)
                .all()
            )
            first_q = next((m.content for m in msgs if m.role == "user" and m.content), "")
            first_a = next((m.content for m in msgs if m.role == "assistant" and m.content), "")
            if not first_q:
                return {"id": chat_id, "title": chat.title or "New Chat"}

            prompt = (
                "Generate a concise 4–7 word title summarising the topic of this conversation. "
                "Return ONLY the title — no quotes, no punctuation at end, no preamble.\n\n"
                f"USER: {first_q[:500]}\n"
                f"ASSISTANT: {first_a[:600]}\n\n"
                "Title:"
            )
            raw = ""
            # When the Rust runtime is enabled, route title generation through it —
            # the runtime talks to the in-house model gateway via /v1/chat/completions (the
            # working path). The Python model_router path calls /llm/generate on the
            # LLM proxy, which is empty in local dev (no proxy service running) and
            # would fail. Use a dedicated title-generation session so it doesn't
            # pollute the user's chat history.
            try:
                from core.runtime_client import ENABLE_RUNTIME as _RT_ON_AT, RUNTIME_PCT as _RT_PCT_AT
                if _RT_ON_AT and _RT_PCT_AT > 0:
                    from core.runtime_client import chat_stream_sync as _rt_title_sync
                    _title_session = f"title-{chat_id}"
                    _title_turn = f"title-{chat_id}-t1"
                    _rt_chunks = []
                    for _tok in _rt_title_sync(
                        session=_title_session,
                        turn=_title_turn,
                        message=prompt,
                        data_class="internal",
                        caps=["chat.send"],
                    ):
                        _rt_chunks.append(_tok)
                    raw = "".join(_rt_chunks)
            except Exception:
                raw = ""
            # Fallback: use the Python model_router (works when LLM_PROXY_URL points
            # at a real proxy service, or when API keys are configured for direct calls).
            if not raw:
                try:
                    raw = model_router.generate(prompt, model_hint="haiku")
                except Exception:
                    raw = ""
            # Guard against error strings leaking as the title — model_router.generate
            # returns "Error: …" on failure instead of raising. Reject any output that
            # looks like an error message and fall back to the first question.
            if raw and isinstance(raw, str) and raw.strip().lower().startswith("error"):
                raw = ""
            title = (raw or "").strip().strip("\"'`").splitlines()[0] if raw else ""
            title = title[:80] if title else (first_q[:50] or "New Chat")
            chat.title      = title
            chat.updated_at = _dt.datetime.utcnow()
            db.commit()
            return {"id": chat_id, "title": title}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/chats")
def create_chat(body: dict, current_user: dict = Depends(get_current_user)):
    """Eagerly create a Chat row with KB scope + rag_mode set atomically.

    Background — why this exists:
        Normal chats are created lazily by the Kafka consumer
        (workers/kafka_consumer.py) when the first user→assistant exchange
        lands. That works fine for the Generic chat surface, but KB chats
        created from KbChatPanel need their rag_mode/product_id/domain/
        spec_version/kb_doc_id columns populated *immediately* — otherwise
        a page refresh before the first prompt loses the chat entirely
        (the row doesn't exist), and even after a prompt the lazy-create
        path doesn't set the scope fields, so isKbChat() returns false
        on hydration and the chat disappears from the KB sidebar.

        This endpoint creates the row up-front with all KB fields set.
        Idempotent on (id, user_id): a retry with the same UUID returns
        the existing row instead of erroring.

    Body shape (all optional except scope validation rules):
        {
          "id":           "<uuid>"          # optional; server generates if absent
          "title":        "New KB Chat",    # optional; defaults to "New Chat"
          "rag_mode":     "on"|"auto"|"off",# optional; defaults to "off"
          "product_id":   "<uuid>"|null,
          "domain":       "Tech"|null,
          "spec_version": "v3"|null,
          "kb_doc_id":    "<uuid>"|null,
          "project_id":   "<id>"|null       # optional; passes through unchanged
        }

    Server-derived product_id enforcement (§7): non-admins can only pick
    a product_id mapped to their department via dept_product_mappings.
    This mirrors the check in PATCH /chats/{chat_id}/scope so a malicious
    client can't bypass the scope guard by going through the create path.
    """
    user_id   = current_user.get("sub") or current_user.get("user_id", "")
    user_role = (current_user.get("role") or current_user.get("user_role") or "").lower()
    user_dept = current_user.get("department") or ""
    is_admin  = user_role == "admin"

    if not user_id or user_id == "default":
        raise HTTPException(status_code=401, detail="authenticated user required")

    # ── Parse + validate inputs ───────────────────────────────────────
    chat_id  = (body.get("id") or "").strip() or str(uuid.uuid4())
    title    = (body.get("title") or "New Chat").strip()[:400] or "New Chat"
    rag_mode = (body.get("rag_mode") or "off").strip().lower()
    if rag_mode not in {"off", "auto", "on"}:
        raise HTTPException(status_code=400, detail="rag_mode must be one of: off, auto, on")

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _ok, _errs, title = validate_chat_title(title)
    if not _ok:
        raise HTTPException(status_code=400, detail=_flatten_errors({"title": _errs}))
    title = title or "New Chat"

    def _norm_str(v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="scope fields must be strings or null")
        v = v.strip()
        return v or None

    _pid = _norm_str(body.get("product_id"))
    _dom = _norm_str(body.get("domain"))
    _ver = _norm_str(body.get("spec_version"))
    _did = _norm_str(body.get("kb_doc_id"))
    _proj = _norm_str(body.get("project_id"))

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _scope_ok, _scope_errs, _scope_san = validate_chat_scope_fields(_dom, _ver)
    if not _scope_ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(_scope_errs))
    _dom = _scope_san["domain"]
    _ver = _scope_san["spec_version"]

    # ── §7 server-derived product_id authorization ────────────────────
    # Mirrors PATCH /chats/{chat_id}/scope (chat_router.py:1178-1218).
    if _pid and not is_admin:
        try:
            import json as _json2
            from core.redis_pool import get_kv as _get_kv
            from core.config import RDB_CACHE as _RDB_CACHE
            _rc = _get_kv(_RDB_CACHE, decode_responses=True)
            _allowed = None
            if user_dept:
                _cached = _rc.get(f"dept:pids:{user_dept}")
                if _cached:
                    try:
                        _allowed = set(_json2.loads(_cached))
                    except Exception:
                        _allowed = None
            if _allowed is None:
                from db.database import SessionLocal as _PgS
                from sqlalchemy import text as _sql
                _s = _PgS()
                try:
                    _rows = _s.execute(
                        _sql("SELECT product_id::text FROM dept_product_mappings WHERE department = :d"),
                        {"d": user_dept},
                    ).fetchall()
                    _allowed = {r[0] for r in _rows}
                finally:
                    _s.close()
            if _pid not in (_allowed or set()):
                raise HTTPException(
                    status_code=403,
                    detail="product_id not in your department's accessible products",
                )
        except HTTPException:
            raise
        except Exception:
            # Fail-closed: same posture as PATCH /scope — drop product_id
            # silently if we cannot verify, so the chat is created without
            # an unverified scope rather than failing the whole request.
            _pid = None

    # ── INSERT (idempotent on existing id+user) ───────────────────────
    try:
        from db.database import SessionLocal
        from db.models import Chat
        import datetime as _dt
        db = SessionLocal()
        try:
            existing = db.query(Chat).filter(Chat.id == chat_id).first()
            if existing:
                if existing.user_id and str(existing.user_id) != str(user_id):
                    raise HTTPException(status_code=403, detail="chat id collision")
                # Idempotent: return the existing row. Don't overwrite scope
                # — that's PATCH /scope's job. This branch is for retry safety.
                return {
                    "id":           str(existing.id),
                    "title":        existing.title or "New Chat",
                    "project_id":   existing.project_id,
                    "is_pinned":    bool(getattr(existing, "is_pinned", False)),
                    "rag_mode":     (getattr(existing, "rag_mode", "off") or "off"),
                    "product_id":   (str(existing.product_id) if getattr(existing, "product_id", None) else None),
                    "domain":       getattr(existing, "domain", None),
                    "spec_version": getattr(existing, "spec_version", None),
                    "kb_doc_id":    (str(existing.kb_doc_id) if getattr(existing, "kb_doc_id", None) else None),
                    "message_count": 0,
                    "created_at":   existing.created_at.isoformat() if existing.created_at else "",
                    "updated_at":   existing.updated_at.isoformat() if existing.updated_at else "",
                }

            now = _dt.datetime.utcnow()
            row = Chat(
                id=chat_id,
                user_id=user_id,
                title=title,
                project_id=_proj,
                rag_mode=rag_mode,
                product_id=_pid,
                domain=_dom,
                spec_version=_ver,
                kb_doc_id=_did,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            db.commit()
            return {
                "id":            chat_id,
                "title":         title,
                "project_id":    _proj,
                "is_pinned":     False,
                "rag_mode":      rag_mode,
                "product_id":    _pid,
                "domain":        _dom,
                "spec_version":  _ver,
                "kb_doc_id":     _did,
                "message_count": 0,
                "created_at":    now.isoformat(),
                "updated_at":    now.isoformat(),
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"create_chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/chats/{chat_id}/rag-mode")
def update_chat_rag_mode(chat_id: str, body: dict, current_user: dict = Depends(get_current_user)):
    """Set the per-chat RAG mode (off | auto | on).

    Drives the Generic / Knowledge Base toggle in the Chat UI. 'off' is
    the default for new chats (matches Claude/ChatGPT defaults).
    """
    mode = (body.get("rag_mode") or "").strip().lower()
    if mode not in {"off", "auto", "on"}:
        raise HTTPException(status_code=400, detail="rag_mode must be one of: off, auto, on")
    user_id = current_user.get("sub") or current_user.get("user_id", "")
    try:
        from db.database import SessionLocal
        from db.models import Chat
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            chat.rag_mode   = mode
            chat.updated_at = _dt.datetime.utcnow()
            db.commit()
            return {"id": chat_id, "rag_mode": mode}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.patch("/chats/{chat_id}/scope")
def update_chat_scope(chat_id: str, body: dict, current_user: dict = Depends(get_current_user)):
    """Set per-chat KB retrieval scope: {product_id, domain, spec_version, kb_doc_id}.

    This is the wire between the Chat UI's ScopePicker and the existing Phase 1–5
    retrieval machinery. The /ask gateway reads these columns on every request
    and injects them into _user_ctx['scope_filter'] + _user_ctx['kb_doc_id'],
    so hybrid_search's product/domain/version WHERE clauses + the coverage tier
    + kb_doc_cache + the entity-graph leak signal all fire deterministically.

    Server-derived enforcement (kn_rewrite.md §7): we validate that the
    selected product_id belongs to the user's department-mapped product set
    (dept_product_mappings) — admins bypass. Any other field can be set to
    null to clear it.

    Body shape (all keys optional; null clears):
        {
          "product_id":   "<uuid>" | null,
          "domain":       "Tech"   | null,
          "spec_version": "v3"     | null,
          "kb_doc_id":    "<uuid>" | null
        }
    """
    user_id   = current_user.get("sub") or current_user.get("user_id", "")
    user_role = (current_user.get("role") or current_user.get("user_role") or "").lower()
    user_dept = current_user.get("department") or ""
    is_admin  = user_role == "admin"

    # Pull fields (preserving explicit nulls as 'clear' signals).
    _pid = body.get("product_id")
    _dom = body.get("domain")
    _ver = body.get("spec_version")
    _did = body.get("kb_doc_id")

    # Light shape validation — strings only; empty → None.
    def _norm_str(v):
        if v is None:
            return None
        if not isinstance(v, str):
            raise HTTPException(status_code=400, detail="scope fields must be strings or null")
        v = v.strip()
        return v or None

    _pid = _norm_str(_pid)
    _dom = _norm_str(_dom)
    _ver = _norm_str(_ver)
    _did = _norm_str(_did)

    # Input validation/sanitization (toggle: INPUT_SANITIZATION_ENABLED)
    _scope_ok, _scope_errs, _scope_san = validate_chat_scope_fields(_dom, _ver)
    if not _scope_ok:
        raise HTTPException(status_code=400, detail=_flatten_errors(_scope_errs))
    _dom = _scope_san["domain"]
    _ver = _scope_san["spec_version"]

    # Server-derived product validation. Non-admins can only pick a product
    # their department is mapped to (dept_product_mappings). This is the
    # §7 "server-derived, never client-spoofable" guarantee — even if the UI
    # sends an arbitrary product_id, we reject it here.
    if _pid and not is_admin:
        try:
            import json as _json2
            # Reuse the same Redis cache + Postgres lookup gateway.py uses to
            # populate _user_ctx['product_ids'] (gateway.py:1584–1610) so a
            # single source of truth governs both the validation here and
            # the runtime injection in /ask.
            from core.redis_pool import get_kv as _get_kv
            from core.config import RDB_CACHE as _RDB_CACHE
            _rc = _get_kv(_RDB_CACHE, decode_responses=True)
            _allowed = None
            if user_dept:
                _cached = _rc.get(f"dept:pids:{user_dept}")
                if _cached:
                    try:
                        _allowed = set(_json2.loads(_cached))
                    except Exception:
                        _allowed = None
            if _allowed is None:
                from db.database import SessionLocal as _PgS
                from sqlalchemy import text as _sql
                _s = _PgS()
                try:
                    _rows = _s.execute(
                        _sql("SELECT product_id::text FROM dept_product_mappings WHERE department = :d"),
                        {"d": user_dept},
                    ).fetchall()
                    _allowed = {r[0] for r in _rows}
                finally:
                    _s.close()
            if _pid not in (_allowed or set()):
                raise HTTPException(
                    status_code=403,
                    detail="product_id not in your department's accessible products",
                )
        except HTTPException:
            raise
        except Exception:
            # Fail-closed: if we can't verify, drop the product_id silently
            # (matches the gateway's fail-closed behavior at /ask time).
            _pid = None

    try:
        from db.database import SessionLocal
        from db.models import Chat
        import datetime as _dt
        db = SessionLocal()
        try:
            chat = db.query(Chat).filter(Chat.id == chat_id, Chat.user_id == user_id).first()
            if not chat:
                raise HTTPException(status_code=404, detail="Chat not found")
            chat.product_id   = _pid
            chat.domain       = _dom
            chat.spec_version = _ver
            chat.kb_doc_id    = _did
            chat.updated_at   = _dt.datetime.utcnow()
            db.commit()
            return {
                "id":           chat_id,
                "product_id":   _pid,
                "domain":       _dom,
                "spec_version": _ver,
                "kb_doc_id":    _did,
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# GET ATTACHMENT METADATA
# ============================================================

@router.get("/chat/attachments/{attachment_id}")
def get_attachment(
        attachment_id: str,
        current_user: dict = Depends(get_current_user),
):
    try:
        from db.database import SessionLocal
        from db.models import ChatAttachment
        from core.storage import storage
        db = SessionLocal()
        try:
            att = db.query(ChatAttachment).filter(
                ChatAttachment.id == attachment_id
            ).first()
            if not att:
                raise HTTPException(status_code=404, detail="Attachment not found")

            # Generate presigned URL if on MinIO
            download_url = None
            if att.storage_path:
                download_url = storage.presigned_url(att.storage_path, expires=3600)

            return {
                "id":            att.id,
                "chat_id":       att.chat_id,
                "file_name":     att.file_name,
                "file_type":     att.file_type,
                "file_size":     att.file_size,
                "storage_path":  att.storage_path,
                "download_url":  download_url,
                "parsed_length": len(att.parsed_text or ""),
                "parsed_preview": (att.parsed_text or "")[:200].strip() or None,
                "created_at":    att.created_at.isoformat() if att.created_at else None,
                "created_by":    att.created_by,
            }
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# GET ATTACHMENT RAW BYTES  (server-side upload persistence)
#
# Serves the stored bytes of an uploaded document/image so previews
# survive re-login, browser restart, and cross-device access — the
# browser Cache API is now only a fast local cache, not the source of truth.
#
# Backend-agnostic: works for local disk AND MinIO (storage.load resolves
# the opaque storage_path). Strict per-user ACL: only the owner may fetch
# (user_id, falling back to created_by for rows created before user_id
# existed). Returns 404 (not 403) on mismatch so file existence isn't leaked.
# ============================================================

@router.get("/chat/attachments/{attachment_id}/raw")
def get_attachment_raw(
        attachment_id: str,
        current_user: dict = Depends(get_current_user),
):
    from fastapi.responses import Response as _RawResp
    from db.database import SessionLocal
    from db.models import ChatAttachment
    from core.storage import storage

    _caller = (
        current_user.get("user_id")
        or current_user.get("sub")
        or current_user.get("email")
        or ""
    )
    _role = current_user.get("role", "")

    db = SessionLocal()
    try:
        att = db.query(ChatAttachment).filter(
            ChatAttachment.id == attachment_id
        ).first()
        if not att:
            raise HTTPException(status_code=404, detail="Attachment not found")

        # Strict owner ACL. Prefer user_id; fall back to created_by for legacy
        # rows. Admins/platform engineers may fetch any (parity with delete).
        _owner = att.user_id or att.created_by or ""
        if _owner and _owner != _caller and _role not in ("admin", "platform_engineer"):
            # 404, not 403 — do not leak existence to non-owners.
            raise HTTPException(status_code=404, detail="Attachment not found")

        storage_path = att.storage_path
        file_name    = att.file_name
        file_type    = att.file_type
    finally:
        db.close()

    if not storage_path:
        raise HTTPException(status_code=410, detail="Attachment has no stored bytes")

    data = storage.load(storage_path)
    if data is None:
        raise HTTPException(status_code=410, detail="Attachment has expired or been deleted")

    mime = _CONTENT_TYPES.get(file_type, "application/octet-stream")
    return _RawResp(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="{file_name}"',
        },
    )

# ============================================================
# DELETE ATTACHMENT
# ============================================================

@router.delete("/chat/attachments/{attachment_id}")
def delete_attachment(
        attachment_id: str,
        current_user: dict = Depends(get_current_user),
):
    """Delete a chat attachment: removes file from object store + DB record."""
    try:
        from db.database import SessionLocal
        from db.models import ChatAttachment
        from core.storage import storage
        db = SessionLocal()
        try:
            att = db.query(ChatAttachment).filter(
                ChatAttachment.id == attachment_id
            ).first()
            if not att:
                raise HTTPException(status_code=404, detail="Attachment not found")

            # Only owner can delete
            user_id = current_user.get("user_id", "")
            role    = current_user.get("role", "")
            if att.created_by and att.created_by != user_id and role not in ("admin", "platform_engineer"):
                raise HTTPException(status_code=403, detail="Cannot delete another user's attachment")

            # Remove from object store
            if att.storage_path:
                deleted = storage.delete(att.storage_path)
                if not deleted:
                    logger.warning(f"chat_router: storage.delete returned False for {att.storage_path}")

            # Remove DB record
            db.delete(att)
            db.commit()

            logger.info(f"chat_router: deleted attachment {attachment_id} ({att.file_name})")
            return {"deleted": True, "id": attachment_id, "file_name": att.file_name}
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# MESSAGE FEEDBACK  (human preference signal)
# ============================================================

from pydantic import BaseModel as _BM

class _FeedbackBody(_BM):
    rating: int  # +1 thumbs-up, -1 thumbs-down
    rag_mode: Optional[str] = None  # sent by FE: "off" (Chat.jsx) | "on"/"auto" (KbChat.jsx)

@router.post("/chat/messages/{message_id}/feedback")
def submit_message_feedback(
        message_id: str,
        body: _FeedbackBody,
        current_user: dict = Depends(get_current_user),
):
    """Record a thumbs-up (+1) or thumbs-down (-1) on an assistant message.

    Stores in message_feedback table and logs to eval_results so human
    preference signals appear alongside automated eval scores.
    """
    if body.rating not in (1, -1):
        raise HTTPException(status_code=422, detail="rating must be +1 or -1")

    user_id = current_user.get("user_id", "anonymous")

    try:
        from db.database import SessionLocal
        from db.models import MessageFeedback
        import uuid as _uuid_mod

        db = SessionLocal()
        try:
            # Upsert: remove previous vote from this user on this message, then insert
            db.query(MessageFeedback).filter(
                MessageFeedback.message_id == message_id,
                MessageFeedback.user_id == user_id,
                ).delete(synchronize_session=False)

            fb = MessageFeedback(
                id=str(_uuid_mod.uuid4()),
                message_id=message_id,
                user_id=user_id,
                rating=body.rating,
            )
            db.add(fb)
            db.commit()
            db.refresh(fb)
            logger.info(f"chat_router: feedback message={message_id} rating={body.rating} user={user_id}")
        finally:
            db.close()

        # ── Wire to evals engine ─────────────────────────────────────────────
        # Map +1 → score 1.0 (accepted), -1 → score 0.0 (rejected)
        try:
            from db.database import SessionLocal as _SL
            from db.models import EvalResult
            import datetime as _dt
            import uuid as _uid

            score = 1.0 if body.rating == 1 else 0.0
            reason = "human_thumbs_up" if body.rating == 1 else "human_thumbs_down"

            # Derive platform from rag_mode sent by the FE component.
            # Chat.jsx always sends "off" → "chat".
            # KbChat.jsx sends its ragMode ("on"/"auto") → "knowledge_base".
            # No DB lookup needed — eliminates the Kafka async race condition
            # where the ChatMessage row may not exist yet when feedback arrives.
            _fb_rag = (body.rag_mode or "").strip().lower()
            _fb_platform = "knowledge_base" if _fb_rag in {"on", "auto"} else "chat"

            _db2 = _SL()
            try:
                er = EvalResult(
                    id=str(_uid.uuid4()),
                    eval_type="human_feedback",
                    score=score,
                    reason=reason,
                    session_id=message_id,
                    run_id=None,
                    question=f"[message:{message_id}]",
                    metadata_={"user_id": user_id, "message_id": message_id, "rating": body.rating},
                    created_at=_dt.datetime.utcnow(),
                    platform=_fb_platform,
                )
                _db2.add(er)
                _db2.commit()
            finally:
                _db2.close()
        except Exception as eval_err:
            logger.warning(f"chat_router: eval log failed (non-fatal): {eval_err}")

        return {"ok": True, "message_id": message_id, "rating": body.rating}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ============================================================
# STOP GENERATION  (cooperative cancellation of active streams)
# ============================================================

class _StopBody(_BM):
    request_id: str   # the request_id echoed back in the SSE stream

@router.post("/chat/stop")
def stop_generation(
        body: _StopBody,
        current_user: dict = Depends(get_current_user),
):
    """
    Signal the active streaming generation identified by *request_id* to stop.

    The frontend should call this endpoint when the user clicks "Stop
    Generating".  The streaming generator in gateway.py polls
    ``generation_registry.should_stop(request_id)`` on every token and
    breaks out of the loop when the flag is set.

    Returns
    -------
    ``{"ok": true,  "stopped": true}``  — flag was set (stream will stop soon)
    ``{"ok": true,  "stopped": false}`` — request_id not found (already done)
    """
    from core.generation_registry import stop as _stop_gen

    user_id = current_user.get("user_id", "anonymous")
    logger.info(
        f"chat_router: stop_generation request_id={body.request_id!r} user={user_id}"
    )

    signalled = _stop_gen(body.request_id)
    return {"ok": True, "stopped": signalled, "request_id": body.request_id}
