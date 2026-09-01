# SPDX-License-Identifier: Apache-2.0
# ============================================================
# DOCUMENT CONTEXT MANAGEMENT SERVICE
#
# Turns the stateless doc-gen pipeline into a stateful DOCUMENT SYSTEM:
# every conversation has a recallable document memory so follow-ups like
# "make the intro shorter", "add a section on X", "convert that to PDF" resolve
# to the RIGHT previously generated document and revise it — in both Chat and
# Buddy (they share this service).
#
# Backed by db.models.GeneratedDocument:
#   - artifact_id : durable handle for a logical document (versions share it)
#   - version     : rises on each revision
#   - chat_id     : links a doc to a conversation
#   - content_md  : editable source (enables revise-without-regenerate)
#
# Every function is fail-open: on any error it returns an empty/None result and
# logs — document generation must never break because recall failed.
# ============================================================

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger

# Which local model disambiguates fuzzy references ("that doc"). Fast, in-house.
_REF_MODEL_HINT = (os.getenv("DOC_INTENT_MODEL", "") or "local").strip() or "local"


@dataclass
class DocRef:
    """A recalled document (latest version of one artifact) in a conversation."""
    artifact_id: str
    doc_id: str            # file_id of the latest version row
    title: str
    format: str
    version: int
    filename: str = ""
    content_md: str = ""
    created_at: Optional[str] = None


@dataclass
class DocMemory:
    """The document memory for a single conversation, newest first."""
    docs: list[DocRef] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.docs

    def latest(self) -> Optional[DocRef]:
        return self.docs[0] if self.docs else None

    def summary_for_llm(self, limit: int = 8) -> str:
        """Compact listing fed to the intent classifier so it can tell
        'generate new' from 'revise an existing one'."""
        lines = []
        for i, d in enumerate(self.docs[:limit], start=1):
            lines.append(f"{i}. artifact_id={d.artifact_id} | \"{d.title}\" ({d.format}, v{d.version})")
        return "\n".join(lines)


def list_docs_for_chat(chat_id: str, user_id: str, limit: int = 20,
                       include_source: bool = False) -> DocMemory:
    """Recall the latest version of every document generated in a conversation,
    newest first. `include_source=True` also loads content_md (heavier)."""
    if not chat_id:
        return DocMemory()
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            rows = (
                db.query(GeneratedDocument)
                .filter(GeneratedDocument.chat_id == str(chat_id),
                        GeneratedDocument.user_id == str(user_id))
                .order_by(GeneratedDocument.created_at.desc())
                .limit(500)
                .all()
            )
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_context] list_docs_for_chat failed: {exc}")
        return DocMemory()

    # Collapse to the latest version per artifact_id (rows already newest-first).
    seen: dict[str, DocRef] = {}
    for r in rows:
        art = r.artifact_id or r.id
        if art in seen:
            continue
        seen[art] = DocRef(
            artifact_id=art,
            doc_id=r.id,
            title=r.title or "Document",
            format=(r.format or "").lower(),
            version=int(r.version or 1),
            filename=r.filename or "",
            content_md=(r.content_md or "") if include_source else "",
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        if len(seen) >= limit:
            break
    return DocMemory(docs=list(seen.values()))


def load_latest_source(artifact_id: str, user_id: str) -> Optional[DocRef]:
    """Load the newest version (with content_md) for an artifact — the source
    a revision/convert edits from.

    IMPORTANT: one-shot docs are saved with artifact_id = NULL (their `id` IS the
    logical handle). list_docs_for_chat surfaces such a doc with artifact_id = its
    row id (via `r.artifact_id or r.id`), so callers pass that id here. We must
    therefore match EITHER the shared artifact_id column OR the row id — otherwise
    a convert/revise of the FIRST (one-shot) doc silently finds nothing and the
    worker re-authors from scratch (wrong-topic output). Mirrors the
    /docs/{artifact_id}/versions endpoint."""
    if not artifact_id:
        return None
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            r = (db.query(GeneratedDocument)
                 .filter(
                     ((GeneratedDocument.artifact_id == str(artifact_id)) |
                      (GeneratedDocument.id == str(artifact_id))),
                     GeneratedDocument.user_id == str(user_id))
                 .order_by(GeneratedDocument.version.desc())
                 .first())
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_context] load_latest_source failed: {exc}")
        return None
    if not r:
        return None
    return DocRef(
        artifact_id=r.artifact_id or r.id,
        doc_id=r.id,
        title=r.title or "Document",
        format=(r.format or "").lower(),
        version=int(r.version or 1),
        filename=r.filename or "",
        content_md=r.content_md or "",
        created_at=r.created_at.isoformat() if r.created_at else None,
    )


def ensure_artifact_id(doc_id: str, user_id: str) -> Optional[str]:
    """Ensure a doc row has a non-NULL artifact_id, backfilling it to its own id
    when it was a one-shot (artifact_id NULL). Returns the effective artifact_id.

    Called before versioning a one-shot doc (convert/revise) so the original v1
    and the new v2 share the SAME artifact_id and form a coherent version chain
    (otherwise list_docs_for_chat would show them as two separate documents).
    Fail-open: returns the input id on any error."""
    if not doc_id:
        return doc_id
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            r = (db.query(GeneratedDocument)
                 .filter(GeneratedDocument.id == str(doc_id),
                         GeneratedDocument.user_id == str(user_id))
                 .first())
            if r and not r.artifact_id:
                r.artifact_id = r.id
                db.commit()
                return r.id
            return (r.artifact_id if r else doc_id) or doc_id
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_context] ensure_artifact_id failed: {exc}")
        return doc_id


def resolve_reference(chat_id: str, user_id: str, text: str,
                      memory: Optional[DocMemory] = None,
                      strict: bool = False) -> Optional[DocRef]:
    """Map a fuzzy user reference to a concrete document — NO REGEX.

    Strategy (cheap → costly):
      1. If there's only one doc in the conversation → it.
      2. Exact title-substring match (plain string compare).
      3. Otherwise ask the fast local model to pick an index (or none).

    `strict` changes the AMBIGUOUS-tail behaviour:
      - strict=False (default, legacy callers): fall back to the newest doc so
        the caller always gets *something* (safe UX for /docs/revise).
      - strict=True (doc_router): return None when nothing confidently matches,
        so the caller can ask the user a clarifying question instead of
        silently editing the wrong document.
    Returns None when nothing plausible matches (caller may then ask the user)."""
    mem = memory or list_docs_for_chat(chat_id, user_id)
    if mem.is_empty():
        return None
    if len(mem.docs) == 1:
        return mem.docs[0]

    t = (text or "").strip()
    tl = t.lower()

    # 2. Title match (longest title first to prefer specific matches).
    for d in sorted(mem.docs, key=lambda x: len(x.title or ""), reverse=True):
        title = (d.title or "").strip().lower()
        if title and len(title) >= 4 and title in tl:
            return d

    # 3. Ask the local model to disambiguate (the ONLY intelligence here — the
    #    model decides "that doc"/"the last one"/"the first one"/new, not regex).
    try:
        from models.model_router import model_router
        listing = mem.summary_for_llm()
        prompt = (
            "The user is referring to one of their previously generated documents. "
            "Pick the single best match and reply with ONLY its number, or 0 if "
            "the user clearly wants a NEW document.\n\n"
            f"Documents:\n{listing}\n\nUser message: {t}\n\nNumber:"
        )
        raw = (model_router.generate(prompt, model_hint=_REF_MODEL_HINT,
                                     return_meta=False) or "").strip()
        # Extract the first integer from the reply without regex.
        digits = "".join(ch for ch in raw if ch.isdigit())
        n = int(digits) if digits else 0
        if 1 <= n <= len(mem.docs):
            return mem.docs[n - 1]
        # n == 0 → the model judged this a NEW-doc request or couldn't pick;
        # fall through to the ambiguous tail below.
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_context] LLM reference resolution failed: {exc}")

    # Ambiguous — strict callers get None (ask the user); legacy callers get the
    # newest doc as a safe default.
    return None if strict else mem.latest()


def mirror_generated_doc_as_attachment(
    *, doc_id: str, chat_id: Optional[str], user_id: str,
    title: str, fmt: str, file_path: str, content_md: str,
) -> None:
    """Make a generated document RE-INGESTABLE as an input attachment so the user
    can later say "combine last week's report with this new PDF". We insert a
    ChatAttachment row carrying the document's editable source as `parsed_text`
    (that's what the attachment_ids input path reads). kind="generated" lets
    cleanup/UX distinguish these from real uploads.

    Idempotent-ish: uses the generated doc's own id so re-runs don't duplicate.
    Fail-open — never blocks generation."""
    if not chat_id or not content_md.strip():
        return
    try:
        from db.database import SessionLocal
        from db.models import ChatAttachment
        db = SessionLocal()
        try:
            existing = db.query(ChatAttachment).filter(ChatAttachment.id == doc_id).first()
            if existing:
                return
            db.add(ChatAttachment(
                id=doc_id,
                chat_id=str(chat_id),
                user_id=str(user_id),
                file_name=(title or "Document")[:500] + f".{fmt or 'md'}",
                file_type=(fmt or "md").lower(),
                file_size=len(content_md.encode("utf-8", "ignore")),
                kind="generated",
                storage_path=file_path or "",
                parsed_text=content_md,
                created_by=str(user_id),
            ))
            db.commit()
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[doc_context] mirror_generated_doc_as_attachment failed: {exc}")
