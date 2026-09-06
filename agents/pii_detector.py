# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt-GRADE PCI / PII DETECTOR  (PRODUCTION)
#
# PCI:  Payment card PAN, CVV, PIN block, card expiry
# PII:  Indian PAN card, Aadhaar, email, mobile,
#       account number, IFSC, account+name combo, UPI
# ============================================================

import re
from typing import List, Dict
from core.logger import logger


# ============================================================
# LUHN CHECK — payment card PAN validation
# ============================================================

def luhn_check(number: str) -> bool:
    try:
        digits = [int(d) for d in number]
        checksum = 0
        for i, digit in enumerate(reversed(digits)):
            if i % 2 == 1:
                digit *= 2
                if digit > 9:
                    digit -= 9
            checksum += digit
        return checksum % 10 == 0
    except Exception:
        return False


# ============================================================
# VERHOEFF CHECK — Aadhaar number validation
# UIDAI uses Verhoeff algorithm as the check-digit scheme.
# ============================================================

_V_TABLE_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_TABLE_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]

def verhoeff_check(number: str) -> bool:
    """Return True if number passes Verhoeff check-digit validation."""
    try:
        c = 0
        for i, digit in enumerate(reversed(number)):
            c = _V_TABLE_D[c][_V_TABLE_P[i % 8][int(digit)]]
        return c == 0
    except Exception:
        return False


# ============================================================
# REGEX PATTERNS
# ============================================================

# Payment card PAN: 13–19 digits (Luhn-validated separately)
CARD_PAN_REGEX = re.compile(r'\b\d{13,19}\b')

# CVV: keyword-anchored to prevent false positives on any 3-digit number
CVV_REGEX = re.compile(
    r'(?i)(cvv|cvc|cvv2|cvc2|security\s*code)[^\d]{0,5}(\d{3,4})'
)

# Card expiry: MM/YY or MM/YYYY
# Negative lookahead (?![\/\-]) prevents matching day/month inside a full date
# like "01/01/1985" — after the match the next char must NOT be another / or -.
# Also require 2-digit year >= 20 so "01/01" (year=01) is excluded.
EXPIRY_REGEX = re.compile(
    r'\b(0[1-9]|1[0-2])[\/\-]([2-9]\d|20\d{2}|2[1-9]\d{2})\b(?![\/\-\d])'
)

# Card-expiry CONTEXT gate. MM/YY[YY] is also a perfectly normal calendar date
# (e.g. "06/2026" in a project timeline), so flagging every match as card expiry
# floods documents with redactions. Only treat it as card expiry when a payment
# keyword OR a card-number-like digit run sits nearby.
_EXPIRY_CTX_WINDOW = 40
_CARD_CONTEXT_REGEX = re.compile(
    r'(?i)\b(exp(?:iry|iration|\.?|\s*date)?|valid\s*(?:thru|through|till|until)|'
    r'cvv|cvc|card\s*(?:no\.?|number|#)?|debit\s*card|credit\s*card|visa|master\s*card|rupay|amex)\b'
)
_CARD_NUMBER_NEARBY = re.compile(r'\d(?:[ \-]?\d){11,}')  # 12+ digits ≈ a card number


def expiry_in_card_context(text: str, start: int, end: int) -> bool:
    """True only when an MM/YY[YY] match is plausibly a CARD expiry (payment keyword
    or card number nearby). A bare date in prose returns False → not flagged/redacted."""
    lo = max(0, start - _EXPIRY_CTX_WINDOW)
    hi = min(len(text), end + _EXPIRY_CTX_WINDOW)
    window = text[lo:start] + " " + text[end:hi]   # surrounding context, excluding the match
    return bool(_CARD_CONTEXT_REGEX.search(window) or _CARD_NUMBER_NEARBY.search(window))

# PIN block: plain PIN (keyword-anchored) or ISO 9564 hex block (16 hex chars)
PIN_BLOCK_REGEX = re.compile(
    r'(?i)\b(pin\s*(?:block)?|pinblock)[^\d]{0,10}(\d{4,8}|[0-9A-Fa-f]{16})\b'
)

# Indian PAN card: AAAAA9999A (5 alpha, 4 digit, 1 alpha)
# 4th char must be a valid entity type code (reduces false positives)
# Case-insensitive — people often type PANs in lowercase (cdgpk2028l, etc.)
INDIA_PAN_REGEX = re.compile(r'\b[A-Z]{3}[ABCFGHLJPTK][A-Z]\d{4}[A-Z]\b', re.IGNORECASE)

# Aadhaar: 12 digits, optional separator (space/dash/dot/underscore/comma/slash,
# 1-3 chars so "1234, 5678, 9012" and "1234 - 5678 - 9012" are also caught,
# not just a single character) after each group of 4.
AADHAAR_REGEX = re.compile(r'\b\d{4}(?:[\s,\-_./]{1,3})?\d{4}(?:[\s,\-_./]{1,3})?\d{4}\b')

# Bank account number: keyword-anchored, 9–18 digits, optional inter-digit separators
# Matches "account 9876543210", "account 9876 5432 10", "acct: 9876-5432-10", "acct: 9876_5432_10"
# Word boundaries (\b) prevent false positives on words like "prime" or "account_number"
ACCOUNT_REGEX = re.compile(
    r'(?i)\b(account|acct|a/c|acc)\b[^\d]{0,10}(\d[\d\s\-\._]{7,21}\d)'
)

# Name pattern used for account+name proximity check:
# Two or more title-case words (e.g. "John Doe", "Rahul Kumar Singh")
NAME_REGEX = re.compile(r'\b([A-Z][a-z]{1,20})(\s+[A-Z][a-z]{1,20}){1,3}\b')

# IFSC: 4 alpha + 0 + 6 alphanumeric
IFSC_REGEX = re.compile(r'\b[A-Z]{4}0[A-Z0-9]{6}\b')

# IPv4 address: 0.0.0.0 – 255.255.255.255 (each octet validated)
IP_ADDRESS_REGEX = re.compile(
    r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
)

# UPI VPA: user@provider format
UPI_REGEX = re.compile(r'\b[a-zA-Z0-9.\-_]{2,}@[a-zA-Z]{2,}\b')

# Email
EMAIL_REGEX = re.compile(
    r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b'
)

# Phone numbers — covers all of:
#   9876543210              Indian mobile, no prefix (starts 6-9, 10 digits)
#   +91 9876543210          Indian with +91 prefix (space separator)
#   +91-9876543210          Indian with +91 prefix (dash separator)
#   +919876543210           Indian with +91 prefix (no separator)
#   0091 9876543210         Indian with 0091 prefix
#   +1 800 555 1234         US/Canada international
#   +1-800-555-1234         US with dashes
#   +44 7911 123456         UK international
#   +65 6123 4567           Singapore
#
# Pattern 1: explicit country-code prefix (+/00) then local digits
#   country code: 1-3 digits after +/00
#   optional area code in parens or plain (up to 4 digits)
#   two groups of 3-6 digits with optional separator
# Pattern 2: Indian mobile without any prefix (must start 6-9, exactly 10 digits)
PHONE_REGEX = re.compile(
    r'(?<!\d)'
    r'(?:'
    r'(?:\+|00)\d{1,3}'                              # country code (1-3 digits after +/00)
    r'[\s\-\.]?(?:\(?\d{1,4}\)?[\s\-\.]?)?'         # optional area code (with or without parens)
    r'\d{3,6}[\s\-\.]?\d{3,6}'                      # subscriber number (two halves)
    r'|'
    r'[6-9]\d{9}'                                    # Indian mobile, plain (no prefix)
    r')'
    r'(?!\d)',
    re.ASCII
)


# ============================================================
# OBFUSCATION HELPERS
# ============================================================

_SEP_DIGIT_RE = re.compile(r'(?<=\d)[ \t\-\._](?=\d)')
_SEP_ALNUM_RE = re.compile(r'(?<=[A-Za-z0-9])[ \t\-\._](?=[A-Za-z0-9])')


def _sep_pattern(clean: str) -> str:
    """Build separator-tolerant regex from a clean string.
    e.g. 'ABCDE1234F' → 'A[\\s\\-\\._]?B[\\s\\-\\._]?C...' — matches with any separator."""
    return r'[\s\-\._]?'.join(re.escape(c) for c in clean)


def _normalize_at_dot(text: str) -> str:
    """Resolve common @/dot obfuscation so email/UPI regex can match.
    [at] (at) ' @ ' → @     [dot] (dot) ' dot ' → ."""
    t = re.sub(r'\[at\]|\(at\)',    '@', text, flags=re.IGNORECASE)
    t = re.sub(r'[ \t]+@[ \t]+',   '@', t)
    t = re.sub(r'\[dot\]|\(dot\)', '.', t,    flags=re.IGNORECASE)
    t = re.sub(r'(?<=[a-zA-Z0-9])[ \t]+dot[ \t]+(?=[a-zA-Z0-9])', '.', t, flags=re.IGNORECASE)
    return t


# ============================================================
# MAIN DETECTOR
# ============================================================

def detect_pii(text: str) -> List[Dict]:

    findings: List[Dict] = []

    if not text:
        return findings

    try:

        # ── Payment card PAN (Luhn-validated) ───────────────────
        # Strip all inter-digit separators (space, tab, hyphen, dot, underscore)
        # so "4111 1111 1111 1111", "4111-1111-1111-1111", "4111_1111_1111_1111"
        # and "4111.1111.1111.1111" are all caught.
        _pan_text = _SEP_DIGIT_RE.sub('', text)
        _pan_seen: set = set()
        for match in CARD_PAN_REGEX.findall(_pan_text):
            if match not in _pan_seen and luhn_check(match):
                _pan_seen.add(match)
                findings.append({
                    "type":     "PAN",
                    "value":    match,
                    "category": "PCI",
                    "severity": "CRITICAL",
                })
                logger.warning(f"PAN DETECTED → {match[:6]}{'*' * (len(match) - 6)}")

        # ── CVV ─────────────────────────────────────────────────
        for match in CVV_REGEX.findall(text):
            cvv = match[1]
            findings.append({
                "type":     "CVV",
                "value":    "***",
                "category": "PCI",
                "severity": "CRITICAL",
            })
            logger.warning(f"CVV DETECTED")

        # ── Card expiry (context-gated: skip plain calendar dates) ──
        for m in EXPIRY_REGEX.finditer(text):
            if not expiry_in_card_context(text, m.start(), m.end()):
                continue  # bare date (e.g. a timeline) — not a card expiry
            findings.append({
                "type":     "EXPIRY",
                "value":    m.group(0),
                "category": "PCI",
                "severity": "HIGH",
            })

        # ── PIN block ────────────────────────────────────────────
        for match in PIN_BLOCK_REGEX.findall(text):
            pin_val = match[1] if isinstance(match, tuple) else match
            findings.append({
                "type":     "PIN_BLOCK",
                "value":    "****",
                "category": "PCI",
                "severity": "CRITICAL",
            })
            logger.warning("PIN_BLOCK DETECTED")

        # ── Indian PAN card (ABCDE1234F format) ──────────────────
        # Also scan a token-stripped copy: strip internal [\-\._] within each
        # whitespace/pipe/comma token so "ABCD_E1_234F" → "ABCDE1234F" while
        # preserving word boundaries (unlike whole-text alnum stripping).
        _india_pan_seen: set = set()
        _tok_stripped_pan = ' '.join(
            re.sub(r'[\-\._]', '', t) for t in re.split(r'[\s|,;]+', text) if t
        )
        for _src in dict.fromkeys([text, _tok_stripped_pan]):   # dedup if same
            for match in INDIA_PAN_REGEX.findall(_src):
                m_up = match.upper()
                if m_up not in _india_pan_seen:
                    _india_pan_seen.add(m_up)
                    findings.append({
                        "type":     "INDIA_PAN",
                        "value":    m_up[:5] + "****",
                        "category": "PII",
                        "severity": "CRITICAL",
                    })
                    logger.warning(f"INDIA_PAN DETECTED → {m_up[:5]}****")

        # ── Aadhaar (12 digits, Verhoeff-validated) ───────────────
        # Also scan a stripped copy to catch any-separator spacing: "1 2 3 4...", "1_2_3_4..."
        _aadhaar_stripped = _SEP_DIGIT_RE.sub('', text)
        _aadhaar_seen: set = set()
        for _src in (text, _aadhaar_stripped):
            for match in AADHAAR_REGEX.findall(_src):
                digits_only = re.sub(r'\D', '', match)
                if len(digits_only) == 12 and digits_only not in _aadhaar_seen and verhoeff_check(digits_only):
                    _aadhaar_seen.add(digits_only)
                    findings.append({
                        "type":     "AADHAAR",
                        "value":    "XXXX-XXXX-" + digits_only[-4:],
                        "category": "PII",
                        "severity": "CRITICAL",
                    })
                    logger.warning("AADHAAR DETECTED")

        # ── Bank account number ──────────────────────────────────
        acc_positions: List[int] = []
        for match in ACCOUNT_REGEX.finditer(text):
            acc = re.sub(r'[\s\-\._]', '', match.group(2))   # strip separators including underscore
            if not (9 <= len(acc) <= 18):
                continue  # skip if digit count outside valid range after stripping
            acc_positions.append(match.start())
            findings.append({
                "type":     "ACCOUNT_NUMBER",
                "value":    acc[:4] + "****",
                "category": "PII",
                "severity": "CRITICAL",
            })

        # ── Account number + Name combination (proximity) ────────
        # Flag when a person name appears within 200 chars of an account number.
        if acc_positions:
            for name_match in NAME_REGEX.finditer(text):
                name_start = name_match.start()
                for acc_pos in acc_positions:
                    if abs(name_start - acc_pos) <= 200:
                        findings.append({
                            "type":     "ACCOUNT_NAME_COMBO",
                            "value":    name_match.group()[:20] + "…",
                            "category": "PII",
                            "severity": "CRITICAL",
                        })
                        logger.warning(
                            f"ACCOUNT_NAME_COMBO DETECTED near pos={acc_pos}"
                        )
                        break  # one finding per name match is enough

        # ── IFSC code ────────────────────────────────────────────
        # Also scan token-stripped copy to catch "SBIN_0005678", "HDFC-0001234",
        # "HDF.C000.1234" — same token-by-token approach as INDIA_PAN above.
        _ifsc_seen: set = set()
        _tok_stripped_ifsc = ' '.join(
            re.sub(r'[\-\._]', '', t) for t in re.split(r'[\s|,;]+', text) if t
        )
        for _src in dict.fromkeys([text, _tok_stripped_ifsc]):
            for match in IFSC_REGEX.findall(_src):
                if match not in _ifsc_seen:
                    _ifsc_seen.add(match)
                    findings.append({
                        "type":     "IFSC_CODE",
                        "value":    match,
                        "category": "PII",
                        "severity": "HIGH",
                    })

        # ── UPI VPA + Email ──────────────────────────────────────
        # Also scan @/dot-normalized copy to catch "[at]", "(at)", " @ ", "[dot]", " dot "
        _email_norm = _normalize_at_dot(text)
        _upi_seen:   set = set()
        _email_seen: set = set()

        for _src in (text, _email_norm):
            email_matches_src = set(EMAIL_REGEX.findall(_src))
            for match in UPI_REGEX.findall(_src):
                if match not in email_matches_src and match not in _upi_seen:
                    _upi_seen.add(match)
                    findings.append({
                        "type":     "UPI",
                        "value":    match,
                        "category": "PII",
                        "severity": "HIGH",
                    })
                    logger.warning("UPI DETECTED")
            for match in email_matches_src:
                if match not in _email_seen:
                    _email_seen.add(match)
                    findings.append({
                        "type":     "EMAIL",
                        "value":    match[:3] + "***@***",
                        "category": "PII",
                        "severity": "HIGH",
                    })
                    logger.warning("EMAIL DETECTED")

        # ── Mobile number ────────────────────────────────────────
        # Also scan a stripped copy so "9876 543210", "9876-543210", "9876_543210" are caught
        # by the plain-Indian pattern ([6-9]\d{9}) which requires consecutive digits.
        _phone_stripped = _SEP_DIGIT_RE.sub('', text)
        _mobile_seen: set = set()
        for _src in (text, _phone_stripped):
            for match in PHONE_REGEX.findall(_src):
                digits = re.sub(r'[\s\-\.]', '', match)
                if digits not in _mobile_seen:
                    _mobile_seen.add(digits)
                    findings.append({
                        "type":     "MOBILE",
                        "value":    digits[:2] + "****" + digits[-4:],
                        "category": "PII",
                        "severity": "HIGH",
                    })
                    logger.warning(f"MOBILE DETECTED")

        # ── IPv4 address ─────────────────────────────────────────
        # RFC-1918 private ranges (10.x, 172.16-31.x, 192.168.x) are internal
        # network addresses that appear legitimately in architecture diagrams,
        # config examples, and devops content. They are not PCI DSS cardholder
        # data and should not trigger compliance findings.
        import ipaddress as _ipaddress
        _ip_seen: set = set()
        for match in IP_ADDRESS_REGEX.findall(text):
            if match not in _ip_seen:
                _ip_seen.add(match)
                try:
                    if _ipaddress.ip_address(match).is_private:
                        continue  # skip RFC-1918 — not PCI-sensitive
                except ValueError:
                    pass
                parts = match.split(".")
                masked = f"{parts[0]}.{parts[1]}.*.*"
                findings.append({
                    "type":     "IP_ADDRESS",
                    "value":    masked,
                    "category": "PII",
                    "severity": "HIGH",
                })
                logger.warning(f"IP_ADDRESS DETECTED → {masked}")

    except Exception as e:
        logger.error(f"PII detection failed → {e}")

    return findings


# ============================================================
# PII REDACTOR — in-place substitution of soft-PII types
# Hard credentials (PAN/CVV/PIN/keys) are left for the hard-block gate.
# Returns (redacted_text, list_of_type_names_that_were_redacted).
# ============================================================

def redact_pii(text: str):
    if not text:
        return text, []

    redacted = []

    # ── INDIA_PAN ─────────────────────────────────────────────────
    # Detect in both original and token-stripped text, then redact using
    # separator-tolerant pattern so "ABCD_E1_234F" is also replaced in original.
    _tok_strip = ' '.join(re.sub(r'[\-\._]', '', t) for t in re.split(r'[\s|,;]+', text) if t)
    _pan_matches = set(INDIA_PAN_REGEX.findall(text)) | set(INDIA_PAN_REGEX.findall(_tok_strip))
    for pan in _pan_matches:
        out, n = re.subn(_sep_pattern(pan), pan.upper()[:5] + "****", text, flags=re.IGNORECASE)
        if n:
            text = out
            if "INDIA_PAN" not in redacted:
                redacted.append("INDIA_PAN")

    # ── AADHAAR ───────────────────────────────────────────────────
    # Verhoeff-validated 12-digit → XXXX-XXXX-NNNN
    # Callback strips any non-digit separator (space, hyphen, dot, underscore,
    # comma, slash) before validation, matching the separator set AADHAAR_REGEX
    # itself now tolerates.
    def _aadhaar(m):
        digits = re.sub(r'\D', '', m.group())
        if len(digits) == 12 and verhoeff_check(digits):
            return "XXXX-XXXX-" + digits[-4:]
        return m.group()
    # Also run on digit-stripped copy so per-char spacing is caught
    _digit_stripped = _SEP_DIGIT_RE.sub('', text)
    for _src_text in (text, _digit_stripped) if _digit_stripped != text else (text,):
        out = AADHAAR_REGEX.sub(_aadhaar, _src_text)
        if out != _src_text:
            text = out
            if "AADHAAR" not in redacted:
                redacted.append("AADHAAR")
            break

    # ── ACCOUNT_NUMBER ────────────────────────────────────────────
    def _account(m):
        acc = m.group(2)
        masked = acc[:4] + "X" * max(0, len(acc) - 4)
        return m.group(0).replace(acc, masked, 1)
    out = ACCOUNT_REGEX.sub(_account, text)
    if out != text:
        redacted.append("ACCOUNT_NUMBER")
    text = out

    # ── IFSC ──────────────────────────────────────────────────────
    # Detect in token-stripped, redact using separator-tolerant pattern.
    _tok_strip2 = ' '.join(re.sub(r'[\-\._]', '', t) for t in re.split(r'[\s|,;]+', text) if t)
    _ifsc_matches = set(IFSC_REGEX.findall(text)) | set(IFSC_REGEX.findall(_tok_strip2))
    for ifsc in _ifsc_matches:
        out, n = re.subn(_sep_pattern(ifsc), ifsc[:5] + "****", text, flags=re.IGNORECASE)
        if n:
            text = out
            if "IFSC_CODE" not in redacted:
                redacted.append("IFSC_CODE")

    # ── UPI + EMAIL ───────────────────────────────────────────────
    # Normalize @/dot obfuscation first so [at], (at), ' @ ', [dot], ' dot ' are resolved.
    _norm = _normalize_at_dot(text)
    _email_set = set(EMAIL_REGEX.findall(_norm))

    def _upi(m):
        if m.group() in _email_set:
            return m.group()
        return m.group()[:2] + "***@***"
    out, n = UPI_REGEX.subn(_upi, _norm)
    if n:
        redacted.append("UPI")
    text = out if n else _norm  # use normalized text as base even if no UPI

    def _email(m):
        addr = m.group()
        local, domain = addr.split('@', 1)
        return local[:2] + "***@" + domain
    out, n = EMAIL_REGEX.subn(_email, text)
    if n:
        redacted.append("EMAIL")
    text = out

    # ── MOBILE ───────────────────────────────────────────────────
    # Detect in digit-stripped copy, replace via separator-tolerant pattern in original.
    _digit_stripped_mobile = _SEP_DIGIT_RE.sub('', text)
    _mobile_seen: set = set()
    for _src in (text, _digit_stripped_mobile):
        for m in PHONE_REGEX.finditer(_src):
            digits = re.sub(r'[\s\-\._]', '', m.group())
            if digits in _mobile_seen:
                continue
            _mobile_seen.add(digits)
            if len(digits) >= 6:
                masked = digits[:2] + "*" * (len(digits) - 6) + digits[-4:]
            else:
                masked = digits[:1] + "*" * max(0, len(digits) - 2) + digits[-1:]
            replaced, n = re.subn(_sep_pattern(m.group()), masked, text)
            if n:
                text = replaced
                if "MOBILE" not in redacted:
                    redacted.append("MOBILE")

    return text, redacted
