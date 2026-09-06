# SPDX-License-Identifier: MIT
# ============================================================
# AUDIT SIGNER — HMAC-SHA256 signatures for audit events
# ============================================================

import hashlib
import hmac
import json
import os
from typing import List, Optional

# Minimum key length. HMAC accepts any length, so nothing stops a one-character
# key from producing valid-looking signatures — the check has to live here.
_MIN_KEY_LEN = 32

# Values that are never a real key. `.env.example` ships
# AUDIT_SIGNING_KEY=change-me-in-production, and until this check existed the
# only guard was "is it non-empty" — so an install that copied the template
# without editing it signed its tamper-evident audit log with a value published
# in the repository. Anyone could forge an entry and it would verify.
# Deliberately limited to phrases that only appear in template text. Words like
# "secret", "password" or "test" were in an earlier draft of this list and are
# wrong to include: a legitimate operator-chosen key may contain them, and a
# random base64 key can contain a short word by chance. A false positive here
# stops a correctly-configured deployment from starting, which is worse than the
# narrow gap it would close.
_PLACEHOLDER_MARKERS = (
    "change-me", "changeme", "change_me",
    "replace-me", "replaceme", "replace_me",
    "your-key", "your-secret", "your_secret",
    "placeholder", "todo", "xxxx",
)


def reject_weak_key(value: Optional[str], var_name: str = "AUDIT_SIGNING_KEY") -> str:
    """Return the key, or raise ValueError explaining exactly what is wrong.

    Separate from the import-time check below so it can be unit-tested without
    manipulating the environment and re-importing this module.
    """
    if not value or not value.strip():
        raise ValueError(
            f"{var_name} must be set. No default is allowed — "
            f"generate one with: openssl rand -hex 32"
        )
    v = value.strip()
    lowered = v.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            raise ValueError(
                f"{var_name} still looks like a template value "
                f"(it contains {marker!r}). The audit log is signed with this "
                f"key, so a value published in the repository means any entry "
                f"can be forged and will still verify. "
                f"Generate one with: openssl rand -hex 32"
            )
    if len(v) < _MIN_KEY_LEN:
        raise ValueError(
            f"{var_name} is {len(v)} characters; at least {_MIN_KEY_LEN} are "
            f"required so the signature cannot be brute-forced. "
            f"Generate one with: openssl rand -hex 32"
        )
    return v


# No default signing key — fail loudly in all environments.
# Prod startup is also protected by validate_prod_config() in core/config.py.
# Raise at import time so a misconfigured deployment crashes immediately rather
# than signing tamper-evident audit events with a guessable key.
AUDIT_SIGNING_KEY = reject_weak_key(os.getenv("AUDIT_SIGNING_KEY"))


def _canonical(event_dict: dict) -> bytes:
    """
    Produce a deterministic JSON representation (sorted keys, UTF-8).
    Normalises datetime strings: strips timezone suffix so that the
    value signed at write-time (naive UTC isoformat) matches the value
    retrieved from PostgreSQL TIMESTAMPTZ (may carry +00:00 suffix).
    """
    clean = {}
    for k, v in event_dict.items():
        if k == "signature":          # never include the signature in its own hash
            continue
        if isinstance(v, str) and ("+00:00" in v or v.endswith("Z")):
            v = v.replace("+00:00", "").rstrip("Z")
        clean[k] = v
    return json.dumps(clean, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sign_event(event_dict: dict) -> str:
    """
    Compute HMAC-SHA256 over the canonical form of event_dict.
    Returns the hex-digest signature string.
    """
    canonical = _canonical(event_dict)
    sig = hmac.new(
        AUDIT_SIGNING_KEY.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    return sig


def verify_event(event_dict: dict, signature: str) -> bool:
    """
    Verify that signature matches the event.
    Returns True if valid, False otherwise.
    """
    if not signature:
        return False
    expected = sign_event(event_dict)
    return hmac.compare_digest(expected, signature)


def verify_chain(events: List[dict]) -> dict:
    """
    Verify the signature of each event in a list.

    Returns:
        {
          "valid": bool,           # True iff all events are verified
          "total": int,
          "verified": int,
          "first_invalid_index": int | None,
        }
    """
    total    = len(events)
    verified = 0
    first_invalid = None

    for i, event in enumerate(events):
        sig = event.get("signature", "")
        if verify_event(event, sig):
            verified += 1
        else:
            if first_invalid is None:
                first_invalid = i

    return {
        "valid":               verified == total,
        "total":               total,
        "verified":            verified,
        "first_invalid_index": first_invalid,
    }
