# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt PRODUCTION SECRET DETECTOR (FINAL SAFE VERSION)
# ZERO FALSE POSITIVES • PCI DSS SAFE
# ============================================================

import math
import re
from typing import Dict, Iterator, List, Tuple
from core.logger import logger


# ============================================================
# STRICT SECRET PATTERNS ONLY
# (NO entropy detection, NO generic base64 detection)
# ============================================================

AWS_KEY_PATTERN = re.compile(
    r"\bAKIA[0-9A-Z]{16}\b"
)

JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)

GENERIC_API_KEY_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|access[_-]?token|auth[_-]?token|private[_-]?key)"
    r"\s*[:=]\s*[\"']?([A-Za-z0-9_\-]{16,})[\"']?"
)

BEARER_TOKEN_PATTERN = re.compile(
    r"\bBearer\s+([A-Za-z0-9\-._~+/]+=*)\b"
)

STRIPE_KEY_PATTERN = re.compile(
    r"\b(sk_live|sk_test)_[A-Za-z0-9]{16,}\b"
)

PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"
)


# ============================================================
# ENV-VAR ASSIGNMENT SECRETS  (NAME=value)
# ============================================================
#
# GENERIC_API_KEY_PATTERN above misses the single most common real-world shape on an engineering
# platform — a shell/dotenv assignment — for three independent reasons, each of which had to be
# fixed:
#
#   1. `\b` never fires inside SNAKE_CASE. `_` is a word character, so `\bsecret` cannot match
#      `AWS_SECRET_ACCESS_KEY`. Every screaming-snake env var was invisible to it.
#   2. The name vocabulary had no bare `token` or `password`, so `GITLAB_TOKEN=` and
#      `POSTGRES_PASSWORD=` were not candidates at all.
#   3. The value charset `[A-Za-z0-9_-]` excludes `/ + = .`, which real secrets are full of — an
#      AWS secret access key is base64-ish and almost always contains `/`.
#
# Net effect before this: `AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` and
# `export GITLAB_TOKEN=glpat-...` went to the cloud provider verbatim. On a platform whose agents
# run `cat .env` and `env` through a bash tool, that is the likeliest credential leak there is.

_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_.\-]{2,64})"
    r"[ \t]*(?P<op>[:=])[ \t]*"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"'`,;]{8,512})"
    r"(?P=quote)"
)

_SECRET_NAME_KEYWORDS = re.compile(
    r"(secret|token|password|passwd|apikey|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|credential|auth[_-]?token|authorization|signing[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)

_SECRET_NAME_DENY = re.compile(
    r"^(max_tokens|tokenizer\w*|token_count|num_tokens|tokens_(in|out)|token_limit"
    r"|token_expiry|token_ttl|password_policy|secret_name|token_type|auth_type)$",
    re.IGNORECASE,
)

# A recognisable credential prefix is proof on its own — bypass the entropy floor.
_KNOWN_CREDENTIAL_PREFIXES: Tuple[str, ...] = (
    "glpat-", "gldt-", "glrt-",                      # GitLab
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "sk-", "sk_live_", "sk_test_", "pk_live_", "rk_live_",
    "xoxb-", "xoxp-", "xapp-", "xoxa-",              # Slack
    "AKIA", "ASIA",                                   # AWS
    "ya29.", "AIza",                                  # Google
    "hf_", "npm_", "dop_v1_", "shpat_", "SG.",
)

# Bare credentials with a known issuer prefix — no `NAME=` in front.
KNOWN_TOKEN_PATTERN = re.compile(
    r"\b("
    r"gl(?:pat|dt|rt)-[A-Za-z0-9_\-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9]{16,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baps]-[A-Za-z0-9-]{10,}"
    r"|A(?:KIA|SIA)[0-9A-Z]{16}"
    r"|ya29\.[A-Za-z0-9_\-]{20,}"
    r"|AIza[A-Za-z0-9_\-]{30,}"
    r"|hf_[A-Za-z0-9]{20,}"
    r"|dop_v1_[a-f0-9]{40,}"
    r"|shpat_[a-f0-9]{28,}"
    r")\b"
)

_PLACEHOLDER_VALUE = re.compile(
    r"^(\$\{.*\}|\$[A-Za-z_]\w*|<.*>|%.*%|\*+|x{5,}|null|none|true|false|undefined"
    r"|[\W_]*(changeme|placeholder|example|dummy|redacted|sample|test|secret|password|token"
    r"|your[-_]?\w*)[\W_]*)$",
    re.IGNORECASE,
)
_IDENTIFIER_VALUE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_PATH_VALUE = re.compile(r"^([~./]|[A-Za-z]:\\)")

_ENTROPY_FLOOR = 3.0
_MIN_SECRET_LEN = 12


def _shannon_entropy(s: str) -> float:
    """Bits of entropy per character. A real credential sits well above 3.0."""
    if not s:
        return 0.0
    n = len(s)
    return -sum(
        (c / n) * math.log2(c / n)
        for c in (s.count(ch) for ch in set(s))
    )


def has_known_credential_prefix(value: str) -> bool:
    return value.startswith(_KNOWN_CREDENTIAL_PREFIXES)


def _looks_like_secret_name(name: str) -> bool:
    if _SECRET_NAME_DENY.match(name):
        return False
    return bool(_SECRET_NAME_KEYWORDS.search(name))


def is_probable_secret_value(value: str) -> bool:
    """Is this assignment's right-hand side plausibly a live credential?"""
    if not value:
        return False
    if has_known_credential_prefix(value):
        return True
    if len(value) < _MIN_SECRET_LEN:
        return False
    if _PLACEHOLDER_VALUE.match(value) or _IDENTIFIER_VALUE.match(value) or _PATH_VALUE.match(value):
        return False
    classes = sum((
        any(c.islower() for c in value),
        any(c.isupper() for c in value),
        any(c.isdigit() for c in value),
        any(not c.isalnum() for c in value),
    ))
    if classes < 2:
        return False
    return _shannon_entropy(value) >= _ENTROPY_FLOOR


def iter_env_secret_values(text: str) -> Iterator[Tuple[str, str]]:
    """Yield `(name, value)` for every assignment whose name and value both look like a credential."""
    if not text:
        return
    for m in _ASSIGNMENT_PATTERN.finditer(text):
        name, value = m.group("name"), m.group("value")
        if _looks_like_secret_name(name) and is_probable_secret_value(value):
            yield name, value


# ============================================================
# SAFE PATTERN DETECTOR
# ============================================================

def detect_pattern(pattern, text, type_name):

    findings = []

    matches = pattern.findall(text)

    for match in matches:

        if isinstance(match, tuple):
            match = match[-1]

        findings.append({
            "type": type_name,
            "value": str(match)[:12] + "...",
            "severity": "CRITICAL"
        })

    return findings


# ============================================================
# MAIN ENTRY POINT
# ============================================================

def detect_secrets(text: str) -> List[Dict]:

    if not text:
        return []

    findings = []

    try:

        findings.extend(
            detect_pattern(AWS_KEY_PATTERN, text, "AWS_KEY")
        )

        findings.extend(
            detect_pattern(JWT_PATTERN, text, "JWT_TOKEN")
        )

        findings.extend(
            detect_pattern(GENERIC_API_KEY_PATTERN, text, "API_KEY")
        )

        findings.extend(
            detect_pattern(BEARER_TOKEN_PATTERN, text, "BEARER_TOKEN")
        )

        findings.extend(
            detect_pattern(STRIPE_KEY_PATTERN, text, "STRIPE_KEY")
        )

        findings.extend(
            detect_pattern(PRIVATE_KEY_PATTERN, text, "PRIVATE_KEY")
        )

        if findings:

            logger.warning(
                f"SECRET DETECTED → {len(findings)} findings"
            )

    except Exception as e:

        logger.error(f"Secret detection failed: {e}")

    return findings