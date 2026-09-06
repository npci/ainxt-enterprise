# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt PRODUCTION KEY LEAK DETECTOR (FINAL SAFE VERSION)
# PCI DSS / RBI / AiNxt COMPLIANT
# ============================================================

import re
from typing import List, Dict
from core.logger import logger


# ============================================================
# PRIVATE KEY PATTERNS (STRICT)
# ============================================================

PRIVATE_KEY_PATTERNS = [

    re.compile(
        "-----BEGIN " + "PRIVATE KEY-----[\\s\\S]+?-----END " + "PRIVATE KEY-----"
    ),

    re.compile(
        "-----BEGIN " + "RSA PRIVATE KEY-----[\\s\\S]+?-----END " + "RSA PRIVATE KEY-----"
    ),

    re.compile(
        "-----BEGIN " + "EC PRIVATE KEY-----[\\s\\S]+?-----END " + "EC PRIVATE KEY-----"
    ),

    re.compile(
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----[\\s\\S]+?-----END " + "OPENSSH PRIVATE KEY-----"
    ),

]


# ============================================================
# CERTIFICATE PATTERNS (STRICT)
# ============================================================

CERTIFICATE_PATTERNS = [

    re.compile(
        "-----BEGIN " + "CERTIFICATE-----[\\s\\S]+?-----END " + "CERTIFICATE-----"
    ),

]


# ============================================================
# SSH KEY PATTERNS (STRICT)
# ============================================================

SSH_KEY_PATTERNS = [

    re.compile(
        r"\bssh-rsa\s+[A-Za-z0-9+/]{100,}={0,3}"
    ),

    re.compile(
        r"\bssh-ed25519\s+[A-Za-z0-9+/]{60,}={0,3}"
    ),

]


# ============================================================
# KEY ASSIGNMENT PATTERNS (STRICT)
# Requires assignment operator (= or :)
# Prevents false positives
# ============================================================

KEY_ASSIGNMENT_PATTERNS = [

    re.compile(
        r"\b(private_key|secret_key|encryption_key)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]",
        re.IGNORECASE
    ),

]


# ============================================================
# PAYMENT KEY PATTERNS (STRICT AiNxt SAFE)
# Requires assignment context
# ============================================================

PAYMENT_KEY_PATTERNS = [

    re.compile(
        r"\b(LMK|ZMK|TPK|ZPK|TMK|PVK|CVK)\b\s*[:=]\s*[A-F0-9]{16,}",
        re.IGNORECASE
    ),

]


# ============================================================
# HELPER FUNCTION
# ============================================================

def detect_pattern(pattern, text, type_name):

    findings = []

    matches = pattern.findall(text)

    for match in matches:

        value = match

        if isinstance(match, tuple):
            value = match[0]

        findings.append({

            "type": type_name,
            "severity": "CRITICAL",
            "value": str(value)[:30] + "...",

        })

    return findings


# ============================================================
# MAIN DETECTOR
# ============================================================

def detect_key_leaks(text: str) -> List[Dict]:

    if not text:
        return []

    findings = []

    try:

        # Private keys
        for pattern in PRIVATE_KEY_PATTERNS:

            findings.extend(
                detect_pattern(pattern, text, "PRIVATE_KEY_LEAK")
            )


        # Certificates
        for pattern in CERTIFICATE_PATTERNS:

            findings.extend(
                detect_pattern(pattern, text, "CERTIFICATE_LEAK")
            )


        # SSH keys
        for pattern in SSH_KEY_PATTERNS:

            findings.extend(
                detect_pattern(pattern, text, "SSH_KEY_LEAK")
            )


        # Key assignments
        for pattern in KEY_ASSIGNMENT_PATTERNS:

            findings.extend(
                detect_pattern(pattern, text, "KEY_ASSIGNMENT_LEAK")
            )


        # Payment keys (STRICT format only)
        for pattern in PAYMENT_KEY_PATTERNS:

            findings.extend(
                detect_pattern(pattern, text, "PAYMENT_KEY_LEAK")
            )


        if findings:

            logger.critical(
                f"KEY LEAK DETECTED → {len(findings)} findings"
            )


    except Exception as e:

        logger.error(f"Key leak detection failed: {e}")

    return findings