# SPDX-License-Identifier: Apache-2.0
"""
Persist generated images to disk + audit DB row.

Called from routers/chat_router.py after image generation succeeds.
Binary files live in core.config.IMAGE_STORAGE_DIR (separate from documents).
Purged by workers/image_purge.py after IMAGE_RETAIN_DAYS (default 2).
"""

import os
import uuid

from core.logger import logger


def persist_generated_image(
    *,
    user_id: str,
    chat_id: str | None,
    provider: str,
    prompt: str | None,
    img_bytes: bytes,
    mime_type: str = "image/png",
) -> str | None:
    """Write image bytes to IMAGE_STORAGE_DIR and insert a GeneratedImage row.

    Returns the file_id (UUID) on success, None on failure.
    Fire-and-forget: errors are logged as warnings, never raised.
    """
    try:
        from core.config import user_image_dir
        from db.database import SessionLocal
        from db.models import GeneratedImage

        file_id  = str(uuid.uuid4())
        ext      = "png" if "png" in mime_type else "jpg"
        filename = f"{file_id}.{ext}"
        dir_path = user_image_dir(user_id, chat_id)
        file_path = os.path.join(dir_path, filename)

        with open(file_path, "wb") as f:
            f.write(img_bytes)

        db = SessionLocal()
        try:
            db.add(GeneratedImage(
                id=file_id,
                user_id=user_id,
                chat_id=chat_id or None,
                provider=provider,
                prompt=prompt,
                filename=filename,
                file_path=file_path,
                mime_type=mime_type,
                size_bytes=len(img_bytes),
            ))
            db.commit()
        finally:
            db.close()

        logger.info(
            f"image_store: persisted image {file_id} "
            f"({len(img_bytes)} bytes) -> {file_path}"
        )
        return file_id

    except Exception as exc:
        logger.warning(f"image_store: persist failed for user={user_id}: {exc}")
        return None
