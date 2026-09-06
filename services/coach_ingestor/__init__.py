# SPDX-License-Identifier: MIT
# ============================================================
# COACH INGESTOR PACKAGE
# ============================================================
#
# Normalises raw Coach emit payloads into encrypted, redacted coach_event
# rows and triggers rule evaluation. Redact-at-write: raw prompts never
# reach the database.
#
#   from services.coach_ingestor import ingest
#   event_id = ingest(payload)
# ============================================================

from services.coach_ingestor.ingestor import ingest

__all__ = ["ingest"]
