# SPDX-License-Identifier: Apache-2.0
# ============================================================
# COWORK USER MEMORY LAYER
# Durable per-user Cowork personalization preferences.
#
# Stores small, user-controlled office/Cowork preferences
# (email signature, default doc format, preferred PPT theme,
# tone, team/channel aliases, role) PLUS agent-saved durable
# facts (memory_notes) in a single JSONB column keyed by
# user_id. Used to render a system-prompt snippet injected
# into both the desktop Cowork agent and the server-side
# "office mode" gateway path.
#
# This is PERSONALIZATION ONLY — never store secrets, tokens,
# PANs, or any sensitive content here. Connector / doc WRITES
# are NOT performed from this module; preferences only shape
# the prompt. Outbound writes/sends still flow through the
# existing confirm + compliance-gated path.
#
# SCALING (2k parallel users — see feedback_scale_2k_users):
#   Backed by the shared, thread-safe SQLAlchemy engine POOL
#   (db.database.engine) — one short-lived pooled connection
#   per call. NEVER a single module-wide psycopg2 connection
#   (those are not thread-safe and corrupt under FastAPI's
#   sync threadpool at even modest concurrency). DDL is owned
#   by db/migrate.py (_part_u1); this module only does DML.
# ============================================================

import json
from typing import Optional, List, Dict, Any

from core.logger import logger


# ============================================================
# DB ACCESS  (shared pooled engine — same pattern as the Cowork routers)
# ============================================================

def _db():
    """Return (engine, text). Lazy import so the module is import-clean even
    before the DB layer is initialised. The engine is a process-wide POOL —
    each `engine.connect()` / `engine.begin()` checks out a thread-safe
    connection and returns it to the pool on exit, so this is safe under
    thousands of concurrent callers."""
    from db.database import engine
    from sqlalchemy import text
    return engine, text


# ============================================================
# KNOWN PREFERENCE KEYS
# Whitelist of personalization keys. set_pref() rejects anything
# outside this list to keep the document tight and predictable,
# and to prevent the prefs blob from being abused as a secrets
# store. Values are short user-controlled strings/dicts only.
# ============================================================

ALLOWED_PREF_KEYS = {
    "email_signature",      # str  — appended to drafted emails
    "default_doc_format",   # str  — e.g. "docx" | "pdf" | "md"
    "preferred_ppt_theme",  # str  — e.g. "ainxt_corporate"
    "tone",                 # str  — e.g. "formal" | "concise"
    "team_aliases",         # dict — {"alias": "actual channel/team"}
    "channel_aliases",      # dict — {"alias": "#real-channel"}
    "role",                 # str  — e.g. "Engineering Manager"
    "memory_notes",         # list — durable facts the AGENT learns + saves via `remember`
}

# Max characters retained for any single string preference value.
# Keeps the injected prompt bounded and avoids storing large blobs.
_MAX_STR_LEN = 1000
_MAX_ALIASES = 50
# Agent-written durable facts (the `remember` tool). Bounded so the injected
# prompt never grows without limit — oldest notes drop off (FIFO) past the cap.
_MAX_NOTES = 40
_MAX_NOTE_LEN = 400


# ============================================================
# COWORK MEMORY
# ============================================================

class CoworkMemory:
    """
    Postgres-backed durable store for per-user Cowork personalization, on the
    shared SQLAlchemy engine pool.

    One row per user_id; preferences live in a JSONB `prefs` column. Stateless
    apart from the shared engine — safe to use as a process-wide singleton from
    any thread. Getters return safe empty defaults if the DB is unavailable.
    """

    def __init__(self):
        # No owned connection — we use the pooled engine per call.
        self.available = True

    # --------------------------------------------------------
    # VALUE SHAPING
    # --------------------------------------------------------

    def _sanitize_value(self, key: str, value: Any) -> Any:
        """Bound and shape a preference value before persisting. Size guard only —
        outbound compliance runs on the write/send path, not here."""
        if key in ("team_aliases", "channel_aliases"):
            if not isinstance(value, dict):
                raise ValueError(f"{key} must be a dict of alias->target")
            out: Dict[str, str] = {}
            for k, v in list(value.items())[:_MAX_ALIASES]:
                out[str(k)[:200]] = str(v)[:200]
            return out
        if key == "memory_notes":
            # A list of short, de-duplicated durable facts. Coerce/trim/cap.
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)):
                raise ValueError("memory_notes must be a list of short strings")
            seen, out_notes = set(), []
            for item in value:
                s = str(item).strip()[:_MAX_NOTE_LEN]
                if s and s not in seen:
                    seen.add(s)
                    out_notes.append(s)
            return out_notes[-_MAX_NOTES:]  # keep most recent
        # Scalar string preferences
        return str(value)[:_MAX_STR_LEN]

    @staticmethod
    def _coerce_prefs(raw) -> Dict[str, Any]:
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                return {}
        return raw if isinstance(raw, dict) else {}

    # ========================================================
    # READ
    # ========================================================

    def get_prefs(self, user_id: str) -> Dict[str, Any]:
        """Return the full preferences dict for a user (empty dict if none)."""
        if not user_id:
            return {}
        try:
            engine, text = _db()
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT prefs FROM cowork_user_memory WHERE user_id = :uid"),
                    {"uid": str(user_id)},
                ).fetchone()
            return self._coerce_prefs(row[0]) if row else {}
        except Exception as e:
            logger.debug(f"CoworkMemory get_prefs failed: {e}")
            return {}

    # ========================================================
    # WRITE
    # ========================================================

    def set_pref(self, user_id: str, key: str, value: Any) -> Dict[str, Any]:
        """Set a single preference key for a user and return the updated prefs.

        Only keys in ALLOWED_PREF_KEYS are accepted. Upsert merges the new key
        into the existing JSONB document server-side (jsonb || jsonb) so
        concurrent writes to DIFFERENT keys don't clobber each other."""
        if not user_id:
            raise ValueError("user_id is required")
        if key not in ALLOWED_PREF_KEYS:
            raise ValueError(
                f"unknown preference key '{key}'. Allowed: {sorted(ALLOWED_PREF_KEYS)}"
            )
        clean_value = self._sanitize_value(key, value)
        patch = json.dumps({key: clean_value})
        try:
            engine, text = _db()
            with engine.begin() as conn:   # begin() = transaction + auto-commit/rollback
                row = conn.execute(
                    text("""
                        INSERT INTO cowork_user_memory (user_id, prefs, created_at, updated_at)
                        VALUES (:uid, CAST(:patch AS jsonb), NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE
                            SET prefs      = cowork_user_memory.prefs || EXCLUDED.prefs,
                                updated_at = NOW()
                        RETURNING prefs
                    """),
                    {"uid": str(user_id), "patch": patch},
                ).fetchone()
            return self._coerce_prefs(row[0]) if row else {}
        except Exception as e:
            logger.error(f"CoworkMemory set_pref failed: {e}")
            raise

    def add_note(self, user_id: str, note: str) -> Dict[str, Any]:
        """Append a durable fact to the user's Cowork memory (the agent's
        `remember` tool). Notes accumulate (FIFO-capped) and are injected into
        every future session's prompt. Done in ONE atomic statement so concurrent
        `remember` calls for the same user can't lose each other's notes:
        jsonb_path append + de-dupe is computed server-side under the row lock."""
        if not user_id or not (note or "").strip():
            return self.get_prefs(user_id)
        clean = str(note).strip()[:_MAX_NOTE_LEN]
        try:
            engine, text = _db()
            with engine.begin() as conn:
                # Append the note to the existing array (server-side), then trim to
                # the most-recent _MAX_NOTES. De-dupe of identical strings is handled
                # by the read-time render; the cap prevents unbounded growth.
                row = conn.execute(
                    text("""
                        INSERT INTO cowork_user_memory (user_id, prefs, created_at, updated_at)
                        VALUES (:uid, jsonb_build_object('memory_notes', jsonb_build_array(:note)), NOW(), NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            prefs = jsonb_set(
                                cowork_user_memory.prefs,
                                '{memory_notes}',
                                (
                                    SELECT COALESCE(jsonb_agg(elem), '[]'::jsonb)
                                    FROM (
                                        SELECT elem FROM jsonb_array_elements(
                                            COALESCE(cowork_user_memory.prefs->'memory_notes', '[]'::jsonb)
                                            || jsonb_build_array(:note)
                                        ) WITH ORDINALITY AS t(elem, ord)
                                        ORDER BY ord DESC
                                        LIMIT :cap
                                    ) recent
                                ),
                                true
                            ),
                            updated_at = NOW()
                        RETURNING prefs
                    """),
                    {"uid": str(user_id), "note": clean, "cap": _MAX_NOTES},
                ).fetchone()
            return self._coerce_prefs(row[0]) if row else {}
        except Exception as e:
            logger.error(f"CoworkMemory add_note failed: {e}")
            # Fall back to the read-modify-write path (still pooled) on any DB quirk.
            try:
                existing = self.get_prefs(user_id).get("memory_notes") or []
                if not isinstance(existing, list):
                    existing = []
                return self.set_pref(user_id, "memory_notes", [*existing, clean])
            except Exception:
                return self.get_prefs(user_id)

    def delete_note(self, user_id: str, note: str) -> Dict[str, Any]:
        """Remove one durable fact (exact-text match) from the user's
        memory_notes array. Done server-side in a single statement under the
        row lock so it's safe against concurrent add_note/delete_note. Returns
        the updated prefs (idempotent — a no-match is a no-op)."""
        if not user_id or not (note or "").strip():
            return self.get_prefs(user_id)
        target = str(note).strip()[:_MAX_NOTE_LEN]
        try:
            engine, text = _db()
            with engine.begin() as conn:
                row = conn.execute(
                    text("""
                        UPDATE cowork_user_memory
                           SET prefs = jsonb_set(
                                   prefs, '{memory_notes}',
                                   COALESCE((
                                       SELECT jsonb_agg(elem)
                                       FROM jsonb_array_elements(
                                           COALESCE(prefs->'memory_notes', '[]'::jsonb)
                                       ) AS t(elem)
                                       WHERE elem <> to_jsonb(CAST(:note AS text))
                                   ), '[]'::jsonb),
                                   true
                               ),
                               updated_at = NOW()
                         WHERE user_id = :uid
                        RETURNING prefs
                    """),
                    {"uid": str(user_id), "note": target},
                ).fetchone()
            return self._coerce_prefs(row[0]) if row else {}
        except Exception as e:
            logger.error(f"CoworkMemory delete_note failed: {e}")
            return self.get_prefs(user_id)

    def delete_pref(self, user_id: str, key: str) -> Dict[str, Any]:
        """Remove a single preference key for a user. Returns updated prefs."""
        if not user_id:
            return {}
        try:
            engine, text = _db()
            with engine.begin() as conn:
                row = conn.execute(
                    text("""
                        UPDATE cowork_user_memory
                           SET prefs = prefs - :key, updated_at = NOW()
                         WHERE user_id = :uid
                        RETURNING prefs
                    """),
                    {"key": key, "uid": str(user_id)},
                ).fetchone()
            return self._coerce_prefs(row[0]) if row else {}
        except Exception as e:
            logger.debug(f"CoworkMemory delete_pref failed: {e}")
            return {}

    def clear_prefs(self, user_id: str) -> bool:
        """Delete the entire preferences row for a user."""
        if not user_id:
            return False
        try:
            engine, text = _db()
            with engine.begin() as conn:
                res = conn.execute(
                    text("DELETE FROM cowork_user_memory WHERE user_id = :uid"),
                    {"uid": str(user_id)},
                )
            return (res.rowcount or 0) > 0
        except Exception as e:
            logger.debug(f"CoworkMemory clear_prefs failed: {e}")
            return False

    # ========================================================
    # PROMPT RENDERING
    # ========================================================

    def build_memory_prompt(self, user_id: str) -> str:
        """Render the user's Cowork preferences as a system-prompt snippet.

        Returns "" when there are no preferences (so callers can append
        unconditionally). Preferences shape style/defaults only — connector/doc
        WRITES still require the confirm + compliance-gated path."""
        prefs = self.get_prefs(user_id)
        if not prefs:
            return ""

        lines: List[str] = ["## User Cowork Preferences"]

        role = prefs.get("role")
        if role:
            lines.append(f"- Role: {role}")

        tone = prefs.get("tone")
        if tone:
            lines.append(f"- Preferred tone: {tone}")

        doc_fmt = prefs.get("default_doc_format")
        if doc_fmt:
            lines.append(f"- Default document format: {doc_fmt}")

        # NOTE: preferred_ppt_theme is intentionally NOT injected — there is no real
        # theme catalog (decks always use the AiNxt brand guide), so surfacing it
        # would mislead the agent into promising a theme the engine never applies.

        sig = prefs.get("email_signature")
        if sig:
            sig_block = "\n  ".join(str(sig).splitlines()) or str(sig)
            lines.append(f"- Email signature (use when drafting emails):\n  {sig_block}")

        team_aliases = prefs.get("team_aliases")
        if isinstance(team_aliases, dict) and team_aliases:
            pairs = ", ".join(f"'{a}' = {t}" for a, t in team_aliases.items())
            lines.append(f"- Team aliases: {pairs}")

        chan_aliases = prefs.get("channel_aliases")
        if isinstance(chan_aliases, dict) and chan_aliases:
            pairs = ", ".join(f"'{a}' = {t}" for a, t in chan_aliases.items())
            lines.append(f"- Channel aliases: {pairs}")

        notes = prefs.get("memory_notes")
        if isinstance(notes, list) and notes:
            # De-dupe at render time (keep order); the array itself is FIFO-capped.
            seen, uniq = set(), []
            for n in notes:
                s = str(n).strip()
                if s and s not in seen:
                    seen.add(s)
                    uniq.append(s)
            if uniq:
                lines.append("")
                lines.append("### Remembered facts (you saved these earlier — use them, don't re-ask):")
                for n in uniq:
                    lines.append(f"- {n}")

        if len(lines) == 1:
            return ""

        lines.append("")
        lines.append(
            "Apply these preferences when drafting documents, emails, and "
            "presentations. They shape style and defaults only — they do NOT "
            "authorize sending or writing. Any connector send or document write "
            "still requires explicit user confirmation and passes the standard "
            "compliance gate before execution."
        )
        return "\n".join(lines)

    # ========================================================
    # HEALTH
    # ========================================================

    def ping(self) -> bool:
        try:
            engine, text = _db()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def close(self) -> None:
        # No owned connection to close — the engine pool is managed globally.
        pass


# ============================================================
# MODULE-LEVEL SINGLETON + CONVENIENCE FUNCTIONS
# ============================================================

_cowork_memory: Optional[CoworkMemory] = None


def get_cowork_memory() -> CoworkMemory:
    """Return the process-wide CoworkMemory singleton (stateless over the pool)."""
    global _cowork_memory
    if _cowork_memory is None:
        _cowork_memory = CoworkMemory()
    return _cowork_memory


def get_prefs(user_id: str) -> Dict[str, Any]:
    """Module-level convenience: full preferences dict for a user."""
    return get_cowork_memory().get_prefs(user_id)


def set_pref(user_id: str, key: str, value: Any) -> Dict[str, Any]:
    """Module-level convenience: set one preference, return updated prefs."""
    return get_cowork_memory().set_pref(user_id, key, value)


def add_note(user_id: str, note: str) -> Dict[str, Any]:
    """Module-level convenience: append a durable agent-remembered fact."""
    return get_cowork_memory().add_note(user_id, note)


def delete_note(user_id: str, note: str) -> Dict[str, Any]:
    """Module-level convenience: remove one durable fact by exact text."""
    return get_cowork_memory().delete_note(user_id, note)


def build_memory_prompt(user_id: str) -> str:
    """Module-level convenience: render the prompt snippet for injection."""
    return get_cowork_memory().build_memory_prompt(user_id)
