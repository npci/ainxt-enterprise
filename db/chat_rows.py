# SPDX-License-Identifier: MIT
# ============================================================
# CHAT ROW HELPERS
#
# Shared guards for tables whose rows hang off chats(id) by
# foreign key. Imported by routers/chat_router.py (uploads +
# generated images) and services/doc_context.py (generated docs).
# ============================================================

import uuid as _uuid_mod

from sqlalchemy.exc import IntegrityError

from core.logger import logger


def ensure_chat_row(db, chat_id: str, user_id: str) -> None:
    """Create the parent chats row if it does not exist yet (idempotent).

    Why this is required — chat_attachments.chat_id carries
    fk_chat_attachments_chat_id REFERENCES chats(id) ON DELETE CASCADE
    (db/migrate.py Part G), but chats rows are created LAZILY: either by
    workers/kafka_consumer.py when the first user->assistant exchange lands,
    or by gateway.py::_save_chat_messages after the answer is produced.

    The UI pre-uploads attachments *before* /ask, using a client-generated
    UUID. On the first turn of a new chat there is therefore no chats row,
    the ChatAttachment INSERT fails the FK check, and the row is lost — so
    /ask cannot resolve its attachment_ids and the model never sees the file
    (uploaded images silently stopped working entirely: no parsed_text on
    turn 1, no image_caption on follow-up turns). The generated-doc and
    generated-image mirrors race the same lazy create.

    Caller keeps the transaction: this only flushes, it never commits.

    Title stays "New Chat" on purpose — both lazy-create paths only overwrite
    a title that is in ("New Chat", "", None), so leaving the default lets the
    real title_hint land once the first exchange is persisted.
    """
    from db.models import Chat, User

    if not chat_id:
        return
    if db.query(Chat.id).filter(Chat.id == chat_id).first():
        return

    # chats.user_id is a FK to users.id — passing a JWT "sub" that is not a real
    # users row (or not a UUID at all, e.g. the email fallback used by
    # chat_router) would just trade one FK violation for another. NULL instead.
    _uid = None
    if user_id and str(user_id) != "default":
        try:
            _uuid_mod.UUID(str(user_id))
        except (ValueError, AttributeError, TypeError):
            _uid = None
        else:
            if db.query(User.id).filter(User.id == user_id).first():
                _uid = str(user_id)

    db.add(Chat(id=str(chat_id), user_id=_uid, title="New Chat"))
    try:
        db.flush()
    except IntegrityError:
        # Concurrent writers on the same brand-new chat (the UI fires the image
        # pre-upload and the document upload as separate requests) can both pass
        # the existence check above. Whoever loses the race reuses the row.
        db.rollback()
        return

    logger.info(
        f"[DOCTRACE] ensure_chat_row: eagerly created chats row | "
        f"chat_id={chat_id} user_id={_uid}"
    )
