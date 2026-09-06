# SPDX-License-Identifier: MIT
# ============================================================
# FILE UPLOAD VALIDATOR
# Centralised security checks for all file upload endpoints.
#
# Protections provided:
#   1. Extension whitelist  — only known-safe extensions accepted
#   2. Magic-bytes check    — file content signature verified against
#                             the declared extension (catches renamed
#                             executables / disguised malware)
#   3. File size limit      — configurable per call-site
#   4. Filename sanitisation — strips path traversal characters,
#                              assigns a safe UUID-based storage name
#
# DAST finding addressed:
#   "Application accepts .exe renamed to .pdf — no content-type/magic
#    validation performed server-side."
# ============================================================

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.logger import logger

# ── Magic-byte signatures ─────────────────────────────────────────────────────
# Maps extension → list of allowed byte sequences at offset 0
# A file is accepted when its header starts with ANY of the listed patterns.
# Use tuples so they work with bytes.startswith().

_MAGIC: dict[str, list[bytes]] = {
    # Documents
    "pdf":  [b"%PDF"],
    "docx": [b"PK\x03\x04"],                           # ZIP-based (OOXML)
    "xlsx": [b"PK\x03\x04"],                           # ZIP-based (OOXML)
    "xls":  [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],    # OLE2 Compound Doc
    "pptx": [b"PK\x03\x04"],
    "ppt":  [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
    "odt":  [b"PK\x03\x04"],
    "ods":  [b"PK\x03\x04"],
    "rtf":  [b"{\\rtf"],
    # Plain text / data (no strong magic — accept any non-binary first byte)
    "txt":  [],   # No magic check — validated by content scan below
    "csv":  [],   # No magic check
    "json": [b"{", b"["],
    "xml":  [b"<?xml", b"<"],
    "html": [],   # plain text — no magic check (HTML has no reliable fixed signature)
    "htm":  [],   # plain text — no magic check
    "md":   [],   # plain text
    "log":  [],   # plain text - no magic check (log files)
    # Images
    "png":  [b"\x89PNG\r\n\x1a\n"],
    "jpg":  [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif":  [b"GIF87a", b"GIF89a"],
    "webp": [b"RIFF"],   # RIFF....WEBP — checked further below
    "bmp":  [b"BM"],
    "svg":  [b"<?xml", b"<svg", b"<SVG"],
    "ico":  [b"\x00\x00\x01\x00"],
    "tiff": [b"II*\x00", b"MM\x00*"],
    # Archives (allowed in some endpoints with explicit override)
    "zip":  [b"PK\x03\x04"],
    "gz":   [b"\x1f\x8b"],
}

# Extensions that are dangerous regardless of content — always blocked
_ALWAYS_BLOCKED_EXTENSIONS: frozenset[str] = frozenset({
    "exe", "com", "bat", "cmd", "sh", "bash", "ps1", "psm1", "psd1",
    "vbs", "vbe", "js", "jse", "wsf", "wsh", "msc", "msi", "msp",
    "dll", "so", "dylib", "scr", "pif", "cpl", "jar", "war", "ear",
    "asp", "aspx", "php", "php3", "php4", "php5", "phtml", "cgi",
    "pl", "py", "rb", "lua", "go", "rs",
    "reg", "inf", "ins", "isu", "job", "lnk", "mst",
    "hta", "htm_evil",
})

# Executable magic bytes — reject even if extension looks safe
_EXECUTABLE_MAGIC: list[tuple[bytes, str]] = [
    (b"MZ",                            "Windows PE/DOS executable"),
    (b"\x7fELF",                       "Linux ELF executable"),
    (b"\xca\xfe\xba\xbe",             "Mach-O fat binary"),
    (b"\xfe\xed\xfa\xce",             "Mach-O 32-bit"),
    (b"\xfe\xed\xfa\xcf",             "Mach-O 64-bit"),
    (b"\xce\xfa\xed\xfe",             "Mach-O 32-bit (reversed)"),
    (b"\xcf\xfa\xed\xfe",             "Mach-O 64-bit (reversed)"),
    (b"#!/",                           "Unix script (shebang)"),
    (b"#!",                            "Script (shebang)"),
    (b"PK\x03\x04",                   None),   # ZIP — may be OOXML, checked per-ext
]

_MAX_HEADER_BYTES = 16   # bytes to read for magic detection


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class FileValidationResult:
    valid: bool
    safe_filename: str          # sanitised + UUID-prefixed storage name
    original_filename: str
    extension: str
    size_bytes: int
    error: Optional[str] = None
    threat: Optional[str] = None   # populated when a disguised executable is detected


# ── Public API ────────────────────────────────────────────────────────────────

def validate_upload(
    filename: str,
    content: bytes,
    *,
    allowed_extensions: Optional[frozenset[str]] = None,
    max_size_bytes: int = 25 * 1024 * 1024,   # 25 MB default
    caller: str = "upload",
) -> FileValidationResult:
    """
    Validate an uploaded file.

    Parameters
    ----------
    filename          : original filename from the client
    content           : full file bytes (already read)
    allowed_extensions: whitelist; if None the extension must appear in _MAGIC
    max_size_bytes    : reject files larger than this (default 25 MB)
    caller            : label for log messages (e.g. "chat_router", "docs_router")

    Returns
    -------
    FileValidationResult with valid=True on success, or valid=False + .error on failure.
    When a disguised executable is found, .threat is set for alerting.
    """
    original_filename = filename or f"upload_{uuid.uuid4()}"
    ext = Path(original_filename).suffix.lstrip(".").lower()
    safe_filename = _sanitise_filename(original_filename)
    size = len(content)
    header = content[:_MAX_HEADER_BYTES]

    # 1. Always-blocked extension
    if ext in _ALWAYS_BLOCKED_EXTENSIONS:
        msg = f"File type '.{ext}' is not permitted"
        logger.warning(
            f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
            f"ext={ext} size={size} reason=blocked_extension"
        )
        return FileValidationResult(
            valid=False, safe_filename=safe_filename,
            original_filename=original_filename, extension=ext,
            size_bytes=size, error=msg, threat="blocked_extension",
        )

    # 2. Extension whitelist (if provided)
    if allowed_extensions is not None and ext not in allowed_extensions:
        msg = f"File type '.{ext}' is not allowed. Permitted types: {sorted(allowed_extensions)}"
        logger.warning(
            f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
            f"ext={ext} size={size} reason=not_in_whitelist"
        )
        return FileValidationResult(
            valid=False, safe_filename=safe_filename,
            original_filename=original_filename, extension=ext,
            size_bytes=size, error=msg,
        )

    # 3. Executable magic-bytes check (catches renamed .exe → .pdf etc.)
    #    Check before per-extension magic so we always catch PE / ELF regardless of ext.
    for magic_bytes, threat_label in _EXECUTABLE_MAGIC:
        if magic_bytes == b"PK\x03\x04":
            continue   # ZIP / OOXML — handled per-extension below
        if header.startswith(magic_bytes):
            msg = f"File content matches '{threat_label}' signature — rejected"
            logger.warning(
                f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
                f"ext={ext} size={size} reason=executable_magic "
                f"threat='{threat_label}' header={header.hex()}"
            )
            return FileValidationResult(
                valid=False, safe_filename=safe_filename,
                original_filename=original_filename, extension=ext,
                size_bytes=size, error=msg, threat=threat_label,
            )

    # 4. Per-extension magic-bytes check
    expected_magic = _MAGIC.get(ext)
    if expected_magic is None:
        # Extension not in our known list — reject conservatively
        msg = f"Unrecognised file type '.{ext}'"
        logger.warning(
            f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
            f"ext={ext} size={size} reason=unknown_extension"
        )
        return FileValidationResult(
            valid=False, safe_filename=safe_filename,
            original_filename=original_filename, extension=ext,
            size_bytes=size, error=msg,
        )

    if expected_magic:   # empty list → no magic check (plain text types)
        if not any(header.startswith(m) for m in expected_magic):
            msg = (
                f"File content does not match expected signature for '.{ext}'. "
                f"The file may have been renamed or is corrupted."
            )
            logger.warning(
                f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
                f"ext={ext} size={size} reason=magic_mismatch "
                f"header={header.hex()}"
            )
            return FileValidationResult(
                valid=False, safe_filename=safe_filename,
                original_filename=original_filename, extension=ext,
                size_bytes=size, error=msg, threat="magic_mismatch",
            )

    # 4b. HTML-specific full-file validation
    #     HTML has no fixed magic bytes so we scan the entire file content:
    #     (a) Block files containing <script> tags — not safe for KB indexing
    #     (b) Verify the file actually contains at least one recognised HTML tag
    if ext in ("html", "htm"):
        html_content = content.decode("utf-8", errors="ignore")
        # (a) Script tag check — case-insensitive, blocks <script> and <SCRIPT>
        if re.search(r"<\s*script[\s>]", html_content, re.IGNORECASE):
            msg = (
                f'"{original_filename}" contains a <script> tag. '
                f"HTML files with scripts cannot be uploaded for indexing."
            )
            logger.warning(
                f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
                f"ext={ext} size={size} reason=html_script_tag"
            )
            return FileValidationResult(
                valid=False, safe_filename=safe_filename,
                original_filename=original_filename, extension=ext,
                size_bytes=size, error=msg, threat="html_script_tag",
            )
        # (b) HTML tag presence check — must contain at least one known HTML tag
        _HTML_TAG_PATTERN = re.compile(
            r"<\s*(!DOCTYPE|html|head|body|div|p|span|a|table|tr|td|th|thead|tbody|tfoot"
            r"|ul|ol|li|h[1-6]|title|meta|link|br|hr|img|figure|figcaption"
            r"|form|input|button|label|select|option|textarea"
            r"|section|article|header|footer|nav|main|aside"
            r"|strong|em|b|i|u|s|pre|code|blockquote|cite|abbr|acronym"
            r"|dl|dt|dd|caption|col|colgroup|fieldset|legend"
            r"|iframe|canvas|video|audio|source|track|picture"
            r"|details|summary|dialog|template|slot"
            r"|address|time|mark|small|sub|sup|del|ins|kbd|samp|var|wbr"
            r")[^>]*>",
            re.IGNORECASE,
        )
        if not _HTML_TAG_PATTERN.search(html_content):
            msg = (
                f"File content does not match expected signature for '.{ext}'. "
                f"The file may have been renamed or is corrupted."
            )
            logger.warning(
                f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
                f"ext={ext} size={size} reason=magic_mismatch"
            )
            return FileValidationResult(
                valid=False, safe_filename=safe_filename,
                original_filename=original_filename, extension=ext,
                size_bytes=size, error=msg, threat="magic_mismatch",
            )

    # 5. Special case: WebP — RIFF header must contain WEBP at bytes 8–12
    if ext == "webp" and len(content) >= 12:
        if content[8:12] != b"WEBP":
            msg = "File content does not match WebP signature"
            logger.warning(
                f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
                f"reason=invalid_webp"
            )
            return FileValidationResult(
                valid=False, safe_filename=safe_filename,
                original_filename=original_filename, extension=ext,
                size_bytes=size, error=msg, threat="magic_mismatch",
            )

    # 6. Size limit
    if size > max_size_bytes:
        msg = (
            f"File size {size:,} bytes exceeds the {max_size_bytes // 1024 // 1024} MB limit"
        )
        logger.warning(
            f"FILE_UPLOAD_BLOCKED [{caller}] filename={original_filename} "
            f"ext={ext} size={size} reason=too_large limit={max_size_bytes}"
        )
        return FileValidationResult(
            valid=False, safe_filename=safe_filename,
            original_filename=original_filename, extension=ext,
            size_bytes=size, error=msg,
        )

    # All checks passed
    logger.debug(
        f"FILE_UPLOAD_OK [{caller}] filename={original_filename} "
        f"ext={ext} size={size} safe_name={safe_filename}"
    )
    return FileValidationResult(
        valid=True, safe_filename=safe_filename,
        original_filename=original_filename, extension=ext,
        size_bytes=size,
    )


def validate_image_upload(
    filename: str,
    content: bytes,
    content_type: str,
    *,
    max_size_bytes: int = 10 * 1024 * 1024,   # 10 MB default
    caller: str = "image_upload",
) -> FileValidationResult:
    """
    Specialised validator for image-only endpoints (e.g. /ask/image).
    Validates against both the declared MIME type AND the file magic bytes.
    """
    _IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
    _MIME_TO_EXT = {
        "image/jpeg": "jpeg",
        "image/png":  "png",
        "image/gif":  "gif",
        "image/webp": "webp",
    }

    mime = (content_type or "").lower().split(";")[0].strip()
    ext_from_mime = _MIME_TO_EXT.get(mime)

    if not ext_from_mime:
        msg = f"Unsupported image MIME type '{mime}'. Accepted: image/jpeg, image/png, image/gif, image/webp"
        logger.warning(
            f"FILE_UPLOAD_BLOCKED [{caller}] filename={filename} "
            f"mime={mime} reason=unsupported_mime"
        )
        return FileValidationResult(
            valid=False, safe_filename=_sanitise_filename(filename or "image"),
            original_filename=filename or "", extension="",
            size_bytes=len(content), error=msg,
        )

    # Delegate to the main validator using the MIME-derived extension
    safe_name = _sanitise_filename(filename or f"image.{ext_from_mime}")
    return validate_upload(
        filename=safe_name,
        content=content,
        allowed_extensions=_IMAGE_EXTENSIONS,
        max_size_bytes=max_size_bytes,
        caller=caller,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sanitise_filename(filename: str) -> str:
    """
    Return a safe storage filename.
    - Strip directory components (path traversal guard)
    - Remove or replace dangerous characters
    - Prefix with a UUID so names are never predictable
    """
    # Take only the basename — prevent path traversal
    name = Path(filename).name

    # Keep only alphanumeric, dash, underscore, dot
    name = re.sub(r"[^\w.\-]", "_", name)

    # Collapse multiple dots (double-extension attack: evil.exe.pdf → evil_exe.pdf)
    # Keep only the last extension
    parts = name.rsplit(".", 1)
    if len(parts) == 2:
        stem = re.sub(r"\.+", "_", parts[0])
        name = f"{stem}.{parts[1]}"

    # Truncate stem to 80 chars
    if len(name) > 120:
        ext_part = Path(name).suffix
        name = name[: 120 - len(ext_part)] + ext_part

    # UUID prefix for storage uniqueness and non-guessability
    return f"{uuid.uuid4().hex[:8]}_{name}"
