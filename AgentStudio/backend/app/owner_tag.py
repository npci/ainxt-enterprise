# SPDX-License-Identifier: MIT
"""Canonical per-user artifact ownership tagging (Broken Access Control fix).

This module is the SINGLE definition of the owner-tag scheme that gates
cross-tenant generated-file access. It previously existed as three hand-copied
implementations — ``app.main.owner_tag``,
``app.cli_runtime.workspace._owner_tag`` and ``gateway._abs_owner_tag`` — kept
in sync only by comments, with the gateway copy having no test coverage at all.
A security-critical invariant held together by comments will eventually drift,
so the algorithm now lives here and every consumer imports it.

Import-light by design
----------------------
Only ``hashlib`` is imported, and this module deliberately sits at
``app.owner_tag`` rather than ``app.core.owner_tag``: ``app/core/__init__.py``
re-exports ``workflow_repo``, so importing anything under ``app.core`` drags in
~511 modules (including structlog and the DB layer). ``app/__init__.py`` is
empty, so ``app.owner_tag`` stays genuinely cheap.

That matters because the consumers cannot afford a heavy import:

* ``app.cli_runtime.workspace`` is imported without ``fastapi`` or ``app.main``
  in ``sys.modules`` and must stay unit-testable in isolation.
* ``gateway`` must not import ``app.main`` for a 3-line helper — doing so loads
  ~1423 modules, constructs a second ``FastAPI`` app object, and runs
  ``load_dotenv(override=True)``, mutating ~36 process env vars as a side
  effect of the import.

Do NOT add dependencies to this module.

Threat model
------------
No server secret is involved: security does not rest on the owner tag being
unguessable. At download time the expected tag is recomputed from the
*authenticated* caller's id and anything resolving outside it is refused — an
attacker cannot make the server serve a file under a tag they are not
authenticated as. The tag is hashed (rather than using the raw user id) only to
avoid leaking the raw id in download URLs.
"""
from __future__ import annotations

import hashlib

# Truncation length for the owner-dir name. Changing this orphans every
# existing artifact (old dirs stop matching the recomputed tag), so treat it as
# a storage-format constant, not a tunable.
_OWNER_TAG_LEN = 16


def owner_tag(user_id: str) -> str:
    """Deterministic, non-secret per-user directory name.

    ``sha256(user_id)[:16]`` — stable across requests so it can be recomputed
    from the caller's identity at download time, and filesystem-safe.

    Returns ``""`` for an empty/whitespace-only id (e.g. standalone local-dev
    with no real identity); callers treat that as "no owner-dir" and fall back
    to the legacy flat path rather than creating a directory named "".
    """
    uid = (user_id or "").strip()
    if not uid:
        return ""
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:_OWNER_TAG_LEN]


def is_generated_path_allowed(rel_parts: tuple[str, ...], user_id: str) -> bool:
    """Ownership decision for a requested generated-file path.

    ``rel_parts`` is the requested path relative to ``GENERATED_FILES_DIR``
    (already traversal-checked by the caller). Returns True iff the caller may
    read it:

      - a single component (``deck.pptx``)  → legacy flat file, allowed for any
        authenticated caller (predates per-user isolation; ages out via TTL);
      - two components (``{owner_tag}/name``) → allowed only when the first
        equals this caller's owner tag;
      - anything else (deeper nesting)       → denied.

    Callers should return 404 (not 403) on a False verdict so the response never
    confirms the existence of another user's artifact.
    """
    if len(rel_parts) == 1:
        return True
    if len(rel_parts) == 2:
        return rel_parts[0] == owner_tag(user_id)
    return False
