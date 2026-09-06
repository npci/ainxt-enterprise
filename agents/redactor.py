# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt UNIFIED REDACTOR
# In-place substitution for ALL compliance types.
# Called with the set of enabled types from compliance_config.json.
# Returns (redacted_text, list_of_type_names_that_were_redacted).
# ============================================================

import re

from agents.pii_detector import (
    CARD_PAN_REGEX, CVV_REGEX, EXPIRY_REGEX, PIN_BLOCK_REGEX,
    INDIA_PAN_REGEX, AADHAAR_REGEX, ACCOUNT_REGEX, IFSC_REGEX,
    UPI_REGEX, EMAIL_REGEX, PHONE_REGEX, IP_ADDRESS_REGEX,
    luhn_check, verhoeff_check, _normalize_at_dot, _sep_pattern,
    expiry_in_card_context,
)
from agents.secret_detector import (
    AWS_KEY_PATTERN, JWT_PATTERN, GENERIC_API_KEY_PATTERN,
    BEARER_TOKEN_PATTERN, STRIPE_KEY_PATTERN,
    KNOWN_TOKEN_PATTERN, iter_env_secret_values,
)
from agents.key_leak_detector import (
    PRIVATE_KEY_PATTERNS, CERTIFICATE_PATTERNS,
    SSH_KEY_PATTERNS, KEY_ASSIGNMENT_PATTERNS, PAYMENT_KEY_PATTERNS,
)

# 16-digit cards in grouped 4-4-4-4 format.
# Handles: space, hyphen, dot, tab, double-space, or no separator between groups.
# Lookahead/lookbehind instead of \b so embedded PANs (letter-adjacent digits) are caught.
_GROUPED_PAN_RE = re.compile(
    r'(?<!\d)(\d{4})[ \t\-.]{0,2}(\d{4})[ \t\-.]{0,2}(\d{4})[ \t\-.]{0,2}(\d{4})(?!\d)'
)

# AMEX 4-6-5 format (e.g. "3782 822463 10005" or "3782-822463-10005")
_AMEX_PAN_RE = re.compile(
    r'(?<!\d)(\d{4})[ \t\-.]{0,2}(\d{6})[ \t\-.]{0,2}(\d{5})(?!\d)'
)

# Raw unspaced 13–19 digit card numbers (no \b — catches embedded/letter-adjacent)
_RAW_PAN_RE = re.compile(r'(?<!\d)\d{13,19}(?!\d)')


def redact_all(text: str, types_to_redact: set) -> tuple:
    """
    Redact all types listed in types_to_redact from text.
    Returns (redacted_text, list_of_type_names_that_were_redacted).
    """
    if not text or not types_to_redact:
        return text, []

    redacted = []

    # ── PRIVATE_KEY_LEAK (full PEM blocks — must run before header-only pattern) ──
    if "PRIVATE_KEY_LEAK" in types_to_redact:
        for pattern in PRIVATE_KEY_PATTERNS:
            out, n = pattern.subn("[PRIVATE KEY REDACTED]", text)
            if n and "PRIVATE_KEY_LEAK" not in redacted:
                redacted.append("PRIVATE_KEY_LEAK")
            text = out

    # ── CERTIFICATE_LEAK ───────────────────────────────────────
    if "CERTIFICATE_LEAK" in types_to_redact:
        for pattern in CERTIFICATE_PATTERNS:
            out, n = pattern.subn("[CERTIFICATE REDACTED]", text)
            if n and "CERTIFICATE_LEAK" not in redacted:
                redacted.append("CERTIFICATE_LEAK")
            text = out

    # ── SSH_KEY_LEAK ───────────────────────────────────────────
    if "SSH_KEY_LEAK" in types_to_redact:
        for pattern in SSH_KEY_PATTERNS:
            out, n = pattern.subn("[SSH KEY REDACTED]", text)
            if n and "SSH_KEY_LEAK" not in redacted:
                redacted.append("SSH_KEY_LEAK")
            text = out

    # ── KEY_ASSIGNMENT_LEAK: private_key = 'value' → private_key = '***' ──
    if "KEY_ASSIGNMENT_LEAK" in types_to_redact:
        def _key_assign(m):
            return re.sub(r"['\"][^'\"]{16,}['\"]", "'***'", m.group(0), count=1)
        for pattern in KEY_ASSIGNMENT_PATTERNS:
            out = pattern.sub(_key_assign, text)
            if out != text and "KEY_ASSIGNMENT_LEAK" not in redacted:
                redacted.append("KEY_ASSIGNMENT_LEAK")
            text = out

    # ── PAYMENT_KEY_LEAK: LMK = HEX... → LMK = [KEY REDACTED] ──
    if "PAYMENT_KEY_LEAK" in types_to_redact:
        def _payment_key(m):
            return re.sub(r'[A-F0-9]{16,}', '[KEY REDACTED]', m.group(0), flags=re.IGNORECASE, count=1)
        for pattern in PAYMENT_KEY_PATTERNS:
            out = pattern.sub(_payment_key, text)
            if out != text and "PAYMENT_KEY_LEAK" not in redacted:
                redacted.append("PAYMENT_KEY_LEAK")
            text = out

    # ── SECRET / API_KEY — AWS key ─────────────────────────────
    if "API_KEY" in types_to_redact or "SECRET" in types_to_redact:
        out, n = AWS_KEY_PATTERN.subn("AKIA***REDACTED***", text)
        if n and "API_KEY" not in redacted:
            redacted.append("API_KEY")
        text = out

    # ── ACCESS_TOKEN — JWT ─────────────────────────────────────
    if "ACCESS_TOKEN" in types_to_redact:
        out, n = JWT_PATTERN.subn("[JWT REDACTED]", text)
        if n and "ACCESS_TOKEN" not in redacted:
            redacted.append("ACCESS_TOKEN")
        text = out

    # ── SECRET / API_KEY — generic key=value assignments ───────
    if "API_KEY" in types_to_redact or "SECRET" in types_to_redact:
        def _generic_key(m):
            return m.group(0).replace(m.group(2), "***", 1)
        out = GENERIC_API_KEY_PATTERN.sub(_generic_key, text)
        if out != text and "API_KEY" not in redacted:
            redacted.append("API_KEY")
        text = out

    # ── SECRET — env-var assignments (NAME=value) ──────────────
    # GENERIC_API_KEY_PATTERN above cannot see these: `\b` does not fire inside SNAKE_CASE, its
    # name list lacks bare token/password, and its value charset excludes `/ + = .`. So
    # `AWS_SECRET_ACCESS_KEY=...` and `export GITLAB_TOKEN=glpat-...` reached the provider intact.
    # Only the VALUE is masked — the name is left readable so the engineer can still see which
    # variable was involved, and so masking never changes the shape of the line.
    if "SECRET" in types_to_redact or "API_KEY" in types_to_redact:
        _hit = False
        for _name, _value in iter_env_secret_values(text):
            text = text.replace(_value, "[REDACTED_SECRET]")
            _hit = True
        if _hit and "SECRET" not in redacted:
            redacted.append("SECRET")

    # ── SECRET — bare credentials with a known issuer prefix ───
    # A token with no `NAME=` in front: embedded in a clone URL, or pasted alone in tool output.
    if "SECRET" in types_to_redact or "API_KEY" in types_to_redact:
        out = KNOWN_TOKEN_PATTERN.sub("[REDACTED_SECRET]", text)
        if out != text and "SECRET" not in redacted:
            redacted.append("SECRET")
        text = out

    # ── ACCESS_TOKEN — Bearer tokens ───────────────────────────
    if "ACCESS_TOKEN" in types_to_redact:
        out, n = BEARER_TOKEN_PATTERN.subn("Bearer [REDACTED]", text)
        if out != text and "ACCESS_TOKEN" not in redacted:
            redacted.append("ACCESS_TOKEN")
        text = out

    # ── API_KEY — Stripe keys ───────────────────────────────────
    if "API_KEY" in types_to_redact:
        out, n = STRIPE_KEY_PATTERN.subn(lambda m: m.group(1) + "_[REDACTED]", text)
        if n and "API_KEY" not in redacted:
            redacted.append("API_KEY")
        text = out

    # ── PAN — AMEX 4-6-5 format (must run before 4-4-4-4 to avoid partial match) ──
    if "PAN" in types_to_redact:
        def _pan_amex(m):
            digits = m.group(1) + m.group(2) + m.group(3)
            if luhn_check(digits):
                return "XXXX-XXXXXX-" + m.group(3)
            return m.group(0)
        out = _AMEX_PAN_RE.sub(_pan_amex, text)
        if out != text:
            redacted.append("PAN")
        text = out

    # ── PAN — grouped 4-4-4-4 (handles space/hyphen/dot/tab/double-space/embedded) ──
    if "PAN" in types_to_redact:
        def _pan_grouped(m):
            digits = m.group(1) + m.group(2) + m.group(3) + m.group(4)
            if luhn_check(digits):
                return "XXXX-XXXX-XXXX-" + m.group(4)
            return m.group(0)
        out = _GROUPED_PAN_RE.sub(_pan_grouped, text)
        if out != text and "PAN" not in redacted:
            redacted.append("PAN")
        text = out

        # Also catch raw unspaced 13–19 digit sequences (no \b — catches embedded PANs)
        def _pan_raw(m):
            if luhn_check(m.group()):
                v = m.group()
                return "X" * (len(v) - 4) + v[-4:]
            return m.group()
        out = _RAW_PAN_RE.sub(_pan_raw, text)
        if out != text and "PAN" not in redacted:
            redacted.append("PAN")
        text = out

    # ── CVV: cvv: 123 → cvv: *** ───────────────────────────────
    if "CVV" in types_to_redact:
        def _cvv(m):
            return m.group(0).replace(m.group(2), "***", 1)
        out = CVV_REGEX.sub(_cvv, text)
        if out != text and "CVV" not in redacted:
            redacted.append("CVV")
        text = out

    # ── EXPIRY: 03/25 → **/** (only when it's a CARD expiry, not a plain date) ──
    if "EXPIRY" in types_to_redact:
        _src = text
        _n = 0
        def _expiry_sub(m):
            nonlocal _n
            if expiry_in_card_context(_src, m.start(), m.end()):
                _n += 1
                return "**/**"
            return m.group(0)
        out = EXPIRY_REGEX.sub(_expiry_sub, text)
        if _n and "EXPIRY" not in redacted:
            redacted.append("EXPIRY")
        text = out

    # ── PIN_BLOCK: pin: 1234 → pin: **** ───────────────────────
    if "PIN_BLOCK" in types_to_redact:
        def _pin(m):
            return m.group(0).replace(m.group(2), "****", 1)
        out = PIN_BLOCK_REGEX.sub(_pin, text)
        if out != text and "PIN_BLOCK" not in redacted:
            redacted.append("PIN_BLOCK")
        text = out

    # ── PII types ──────────────────────────────────────────────

    # INDIA_PAN: detect in both raw and token-stripped text (catches underscore/dot
    # separated forms like AB_CP_E1_234_F), then redact using separator-tolerant pattern.
    if "INDIA_PAN" in types_to_redact:
        _tok_strip = ' '.join(re.sub(r'[\-\._]', '', t) for t in re.split(r'[\s|,;]+', text) if t)
        _pan_hits  = set(INDIA_PAN_REGEX.findall(text)) | set(INDIA_PAN_REGEX.findall(_tok_strip))
        for pan in _pan_hits:
            out, n = re.subn(_sep_pattern(pan), pan.upper()[:5] + "****", text, flags=re.IGNORECASE)
            if n:
                text = out
                if "INDIA_PAN" not in redacted:
                    redacted.append("INDIA_PAN")

    # AADHAAR: Verhoeff-validated 12-digit → XXXX-XXXX-NNNN
    # Strip any non-digit separator before validation so mixed-separator formats
    # like "1234.5678.9012", "1234_5678_9012", "1234, 5678, 9012" are all caught,
    # matching the separator set AADHAAR_REGEX now tolerates.
    if "AADHAAR" in types_to_redact:
        def _aadhaar(m):
            digits = re.sub(r'\D', '', m.group())
            if len(digits) == 12 and verhoeff_check(digits):
                return "XXXX-XXXX-" + digits[-4:]
            return m.group()
        out = AADHAAR_REGEX.sub(_aadhaar, text)
        if out != text and "AADHAAR" not in redacted:
            redacted.append("AADHAAR")
        text = out

    # ACCOUNT_NUMBER: keyword-anchored → keyword + XXXXNNNN
    if "ACCOUNT_NUMBER" in types_to_redact:
        def _account(m):
            acc = m.group(2)
            masked = acc[:4] + "X" * max(0, len(acc) - 4)
            return m.group(0).replace(acc, masked, 1)
        out = ACCOUNT_REGEX.sub(_account, text)
        if out != text and "ACCOUNT_NUMBER" not in redacted:
            redacted.append("ACCOUNT_NUMBER")
        text = out

    # IFSC: SBIN0001234 → SBIN0****
    if "IFSC_CODE" in types_to_redact:
        out, n = IFSC_REGEX.subn(lambda m: m.group()[:5] + "****", text)
        if n and "IFSC_CODE" not in redacted:
            redacted.append("IFSC_CODE")
        text = out

    # EMAIL + UPI: deobfuscate first so "rajesh @ gmail dot com" and
    # "9876543210 @ upi" are caught even in obfuscated form.
    # EMAIL runs before UPI so full addresses are masked before UPI
    # processes the same text and matches the shorter user@provider prefix.
    _email_norm = _normalize_at_dot(text)
    if "EMAIL" in types_to_redact:
        def _email(m):
            addr = m.group()
            local, domain = addr.split('@', 1)
            return local[:2] + "***@" + domain
        out, n = EMAIL_REGEX.subn(_email, _email_norm)
        if n and "EMAIL" not in redacted:
            redacted.append("EMAIL")
        text = out if n else _email_norm  # stay on normalized base

    if "UPI" in types_to_redact:
        out, n = UPI_REGEX.subn(lambda m: m.group()[:2] + "***@***", text)
        if n and "UPI" not in redacted:
            redacted.append("UPI")
        text = out

    # MOBILE: 9876543210 → 98****3210 (keep first 2 + last 4)
    if "MOBILE" in types_to_redact:
        def _mobile(m):
            v = m.group()
            if len(v) >= 6:
                return v[:2] + "*" * (len(v) - 6) + v[-4:]
            return v[:1] + "*" * max(0, len(v) - 2) + v[-1:]
        out, n = PHONE_REGEX.subn(_mobile, text)
        if n and "MOBILE" not in redacted:
            redacted.append("MOBILE")
        text = out

    # IP_ADDRESS: 192.168.1.100 → 192.168.*.*
    if "IP_ADDRESS" in types_to_redact:
        def _ip(m):
            parts = m.group().split(".")
            return f"{parts[0]}.{parts[1]}.*.*"
        out, n = IP_ADDRESS_REGEX.subn(_ip, text)
        if n and "IP_ADDRESS" not in redacted:
            redacted.append("IP_ADDRESS")
        text = out

    return text, redacted
