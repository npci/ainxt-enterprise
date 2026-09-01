# SPDX-License-Identifier: Apache-2.0
# ============================================================
# URL CREDENTIAL MASKING
# ============================================================
#
# CodeWiki accepts repo URLs with an embedded access token, e.g.:
#   https://username:<access-token>@git.example.com/org/repo
#
# The token has to be STORED as-is (it's what the worker uses to actually
# `git clone` the repo — see workers/codewiki_worker.py), but it must never
# be shown back to the user in plaintext anywhere it's displayed: the wiki
# grid/cards, the "codebase already exists" error message, the live/stored
# generation logs ("Cloning <url>..."), or the application's own server
# logs. mask_repo_url() is the single place that redaction happens, so
# every display site calls the same function instead of each re-inventing
# (and potentially getting wrong) its own regex.
#
# Only the PASSWORD component of the URL's userinfo is masked -- the
# username is left visible (it's not a secret on its own, and keeping it
# visible helps an operator recognize which account/token a codebase was
# registered with). A URL with no embedded credentials at all (the common
# case for public repos) is returned completely unchanged.
# ============================================================

import re
from urllib.parse import urlsplit, urlunsplit

_MASK = "****"

# Matches `scheme://username:password@` -- used by mask_text() to redact
# any URL credentials embedded ANYWHERE inside a longer string (an
# exception message, a traceback, a log line), as opposed to
# mask_repo_url() which expects its entire input to BE a single URL.
_CREDENTIAL_IN_TEXT_RE = re.compile(r"://([^\s/:@]+):([^\s/@]+)@")


def mask_repo_url(url: str | None) -> str | None:
    """Return `url` with any embedded password/token replaced by `****`.

    Examples:
        mask_repo_url("https://alice:MY_TOKEN_HERE@git.example.com/org/repo")
            -> "https://alice:****@git.example.com/org/repo"
        mask_repo_url("https://github.com/org/repo")
            -> "https://github.com/org/repo"  (unchanged -- no credentials)

    Fails safe: any URL this can't confidently parse (None, empty string,
    a malformed value, an SSH-style `git@host:org/repo.git` URL that
    urlsplit doesn't recognize as having a distinct userinfo component) is
    returned completely unchanged rather than raising -- masking is a
    defense-in-depth display concern, not something that should ever be
    able to break a codebase's URL or crash whatever is trying to show it.
    """
    if not url:
        return url

    try:
        parts = urlsplit(url)
    except Exception:
        return url

    if not parts.password:
        # No password/token component to hide (plain URL, username-only
        # URL, or a scheme urlsplit couldn't decompose e.g. git@host:...).
        return url

    userinfo = f"{parts.username or ''}:{_MASK}"
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    netloc = f"{userinfo}@{host}"

    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def mask_text(text: str | None) -> str | None:
    """Redact any `scheme://user:token@host` credentials found ANYWHERE
    inside a longer string, replacing the password with `****`.

    Use this (instead of mask_repo_url()) for free-form text that might
    have a credentialed URL embedded in it but isn't itself just a bare
    URL -- exception messages, log lines, error_message columns. Handles
    multiple embedded URLs in the same string. As a defense-in-depth
    layer alongside GitPython's own credential redaction in
    GitCommandError messages (confirmed present as of GitPython 3.1.40:
    a failed `git clone` already shows `https://*****:*****@host/...` in
    its own exception text) -- this covers any OTHER exception type, or
    any future GitPython version, that might not redact on its own.

    Idempotent: a password that's ALREADY entirely asterisks (GitPython's
    own `*****` convention, 5 stars, vs. this function's `****`, 4) is
    left untouched rather than being re-masked into a different-looking
    (but equally redacted) placeholder -- purely cosmetic, but avoids
    visibly different output depending on whether GitPython or this
    function did the redacting first.
    """
    if not text:
        return text

    def _replace(m: re.Match) -> str:
        password = m.group(2)
        if password and set(password) == {"*"}:
            return m.group(0)  # already masked (any number of stars) -- leave as-is
        return f"://{m.group(1)}:{_MASK}@"

    return _CREDENTIAL_IN_TEXT_RE.sub(_replace, text)
