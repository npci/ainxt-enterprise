# SPDX-License-Identifier: Apache-2.0
"""
scripts/migrate_all_to_aes_gcm.py

SEC-F-020 / SEC-F-032 — bulk re-encryption of every remaining Fernet
(AES-128-CBC + HMAC) ciphertext to AES-256-GCM.

CONTEXT
-------
Three separate at-rest encryption stores were migrated from Fernet to
AES-256-GCM in application code:

    1. store/credential_vault.py        -> credential_vault.encrypted
    2. routers/profile_router.py /
       core/platform_credentials.py     -> user_tokens.encrypted_value
    3. services/coach_ingestor/crypto.py -> coach_event.prompt_redacted

All three migrations are LAZY by design: new writes always use AES-256-GCM,
but a row that is never re-written keeps decrypting fine under the OLD
Fernet format forever (each decrypt_value()/decrypt() auto-detects the
format by its prefix — "v2:" / no-prefix / "enc:v1:" / "enc:v2:"). That is
deliberately safe and requires no downtime, but it also means AES-128 never
goes away unless something forces every row to be re-written.

This script does that: it reads every row, decrypts it (works for both old
and new formats), and re-encrypts+re-writes it (always produces the new
AES-256-GCM format). After running this against every environment/replica,
every row in all three tables is on AES-256-GCM, and — ONLY THEN — the
Fernet decrypt fallback code in the four modules listed above can be safely
deleted as a separate, later change (do not delete it before confirming
zero legacy rows remain; see verify_no_legacy_ciphertext() below).

USAGE
-----
    # Dry run (default) - reports what WOULD change, writes nothing:
    python scripts/migrate_all_to_aes_gcm.py

    # Actually re-encrypt:
    python scripts/migrate_all_to_aes_gcm.py --apply

    # Re-encrypt only one store:
    python scripts/migrate_all_to_aes_gcm.py --apply --only credential_vault
    python scripts/migrate_all_to_aes_gcm.py --apply --only user_tokens
    python scripts/migrate_all_to_aes_gcm.py --apply --only coach_event

    # After migrating, confirm no legacy ciphertext remains anywhere:
    python scripts/migrate_all_to_aes_gcm.py --verify-only

SAFETY
------
  - Runs inside the store's own SessionLocal so migration is transactional
    per row (commit after each successful update - a mid-run crash leaves
    already-migrated rows migrated and does not corrupt any row).
  - A row whose decrypt fails (wrong key configured, truly corrupt data) is
    SKIPPED and logged, never deleted or overwritten with garbage.
  - Requires FERNET_KEY (or VAULT_ENCRYPTION_KEY) to be set - the same key
    already used everywhere else; no new secret is needed.
  - coach_event additionally requires COACH_FERNET_KEY (or falls back to
    FERNET_KEY) - matching services/coach_ingestor/crypto.py's own
    resolution order.
"""

from __future__ import annotations

import argparse
import sys


def _migrate_credential_vault(apply: bool) -> tuple[int, int, int]:
    """Re-encrypt every credential_vault row. Returns (total, migrated, skipped)."""
    from store.credential_vault import list_credentials, get_credential_value, rotate_credential

    total = migrated = skipped = 0
    for cred in list_credentials():
        name = cred["name"]
        total += 1
        try:
            plaintext = get_credential_value(name)
        except Exception as exc:
            print(f"  [credential_vault] SKIP {name!r}: decrypt failed ({exc.__class__.__name__}: {exc})")
            skipped += 1
            continue
        if plaintext is None:
            print(f"  [credential_vault] SKIP {name!r}: no value found")
            skipped += 1
            continue

        if not apply:
            print(f"  [credential_vault] WOULD migrate {name!r}")
            migrated += 1
            continue

        try:
            rotate_credential(name, plaintext)
            print(f"  [credential_vault] migrated {name!r}")
            migrated += 1
        except Exception as exc:
            print(f"  [credential_vault] SKIP {name!r}: re-encrypt/write failed ({exc.__class__.__name__}: {exc})")
            skipped += 1

    return total, migrated, skipped


def _migrate_user_tokens(apply: bool) -> tuple[int, int, int]:
    """Re-encrypt every user_tokens row. Returns (total, migrated, skipped)."""
    from db.database import SessionLocal
    from sqlalchemy import text
    from store.credential_vault import decrypt_value, encrypt_value

    total = migrated = skipped = 0
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT id, user_id, token_type, encrypted_value FROM user_tokens")
        ).fetchall()

        for row_id, user_id, token_type, encrypted_value in rows:
            total += 1
            label = f"user_id={user_id} type={token_type}"
            try:
                plaintext = decrypt_value(encrypted_value)
            except Exception as exc:
                print(f"  [user_tokens] SKIP {label}: decrypt failed ({exc.__class__.__name__}: {exc})")
                skipped += 1
                continue

            if not apply:
                print(f"  [user_tokens] WOULD migrate {label}")
                migrated += 1
                continue

            try:
                new_ciphertext = encrypt_value(plaintext)
                db.execute(
                    text("UPDATE user_tokens SET encrypted_value = :enc, updated_at = NOW() WHERE id = :id"),
                    {"enc": new_ciphertext, "id": row_id},
                )
                db.commit()
                print(f"  [user_tokens] migrated {label}")
                migrated += 1
            except Exception as exc:
                db.rollback()
                print(f"  [user_tokens] SKIP {label}: re-encrypt/write failed ({exc.__class__.__name__}: {exc})")
                skipped += 1
    finally:
        db.close()

    return total, migrated, skipped


def _migrate_coach_event(apply: bool) -> tuple[int, int, int]:
    """Re-encrypt every coach_event.prompt_redacted value. Returns (total, migrated, skipped).

    coach_event has a COMPOSITE primary key (event_id, ts) — see
    db/models.py::CoachEvent — so both columns are needed to target a row
    for UPDATE; event_id alone is not unique."""
    from db.database import SessionLocal
    from sqlalchemy import text
    from services.coach_ingestor import crypto

    total = migrated = skipped = 0
    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT event_id, ts, prompt_redacted FROM coach_event WHERE prompt_redacted IS NOT NULL")
        ).fetchall()

        for event_id, ts, prompt_redacted in rows:
            # Dev-mode plaintext (no prefix at all) has nothing to migrate —
            # crypto.encrypt() would just encrypt it for the first time, which
            # is a *new* behaviour change, not a re-encryption. Only rows
            # already under the LEGACY "enc:v1:" prefix are in scope here.
            if not prompt_redacted.startswith(crypto._ENC_PREFIX_V1):
                continue
            total += 1

            try:
                plaintext = crypto.decrypt(prompt_redacted)
                if plaintext in ("[decryption failed]", "[encrypted — key unavailable]"):
                    raise ValueError(plaintext)
            except Exception as exc:
                print(f"  [coach_event] SKIP event_id={event_id}: decrypt failed ({exc.__class__.__name__}: {exc})")
                skipped += 1
                continue

            if not apply:
                print(f"  [coach_event] WOULD migrate event_id={event_id}")
                migrated += 1
                continue

            try:
                new_ciphertext = crypto.encrypt(plaintext)
                db.execute(
                    text(
                        "UPDATE coach_event SET prompt_redacted = :enc "
                        "WHERE event_id = :event_id AND ts = :ts"
                    ),
                    {"enc": new_ciphertext, "event_id": event_id, "ts": ts},
                )
                db.commit()
                print(f"  [coach_event] migrated event_id={event_id}")
                migrated += 1
            except Exception as exc:
                db.rollback()
                print(f"  [coach_event] SKIP event_id={event_id}: re-encrypt/write failed ({exc.__class__.__name__}: {exc})")
                skipped += 1
    finally:
        db.close()

    return total, migrated, skipped


def verify_no_legacy_ciphertext() -> bool:
    """Report whether any row in any of the three stores is still on the
    legacy (Fernet) format. Returns True iff every row is already on
    AES-256-GCM (or plaintext/empty, for coach_event dev-mode rows) — i.e.
    it is now SAFE to remove the Fernet decrypt fallback code."""
    from db.database import SessionLocal
    from sqlalchemy import text

    all_clear = True
    db = SessionLocal()
    try:
        legacy_vault = db.execute(
            text("SELECT COUNT(*) FROM credential_vault WHERE encrypted NOT LIKE 'v2:%'")
        ).scalar()
        if legacy_vault:
            print(f"  [credential_vault] {legacy_vault} row(s) still on legacy Fernet format")
            all_clear = False

        legacy_tokens = db.execute(
            text("SELECT COUNT(*) FROM user_tokens WHERE encrypted_value NOT LIKE 'v2:%'")
        ).scalar()
        if legacy_tokens:
            print(f"  [user_tokens] {legacy_tokens} row(s) still on legacy Fernet format")
            all_clear = False

        legacy_coach = db.execute(
            text(
                "SELECT COUNT(*) FROM coach_event "
                "WHERE prompt_redacted LIKE 'enc:v1:%'"
            )
        ).scalar()
        if legacy_coach:
            print(f"  [coach_event] {legacy_coach} row(s) still on legacy Fernet format")
            all_clear = False
    finally:
        db.close()

    if all_clear:
        print("\nAll clear — no legacy Fernet ciphertext remains in any of the three stores.")
        print("It is now safe to remove the Fernet decrypt fallback from:")
        print("  - store/credential_vault.py       (_get_fernet / legacy branch in decrypt_value)")
        print("  - core/platform_credentials.py    (delegates to credential_vault - nothing to remove directly)")
        print("  - routers/profile_router.py       (delegates to credential_vault - nothing to remove directly)")
        print("  - services/coach_ingestor/crypto.py (_get_fernet / enc:v1: branch in decrypt)")
    else:
        print("\nLegacy ciphertext still present — do NOT remove the Fernet fallback yet.")
        print("Run this script with --apply to migrate the remaining rows.")

    return all_clear


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true",
                         help="Actually re-encrypt and write rows. Without this flag, runs a dry run only.")
    parser.add_argument("--only", choices=["credential_vault", "user_tokens", "coach_event"],
                         help="Migrate only this one store instead of all three.")
    parser.add_argument("--verify-only", action="store_true",
                         help="Skip migration entirely; just report whether any legacy ciphertext remains.")
    args = parser.parse_args()

    if args.verify_only:
        ok = verify_no_legacy_ciphertext()
        return 0 if ok else 1

    stores = {
        "credential_vault": _migrate_credential_vault,
        "user_tokens":       _migrate_user_tokens,
        "coach_event":       _migrate_coach_event,
    }
    targets = [args.only] if args.only else list(stores.keys())

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"=== migrate_all_to_aes_gcm.py — mode={mode} targets={targets} ===\n")

    grand_total = grand_migrated = grand_skipped = 0
    for name in targets:
        print(f"--- {name} ---")
        total, migrated, skipped = stores[name](args.apply)
        print(f"    total={total} migrated={migrated} skipped={skipped}\n")
        grand_total += total
        grand_migrated += migrated
        grand_skipped += skipped

    print(f"=== done — total={grand_total} migrated={grand_migrated} skipped={grand_skipped} ===")
    if not args.apply:
        print("This was a DRY RUN. Re-run with --apply to actually re-encrypt these rows.")
    else:
        print("\nRun with --verify-only to confirm no legacy ciphertext remains before removing the Fernet fallback code.")

    return 0 if grand_skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
