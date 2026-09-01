# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PLATFORM CREDENTIALS — central resolver for per-user and
# org-level credentials, and product context lookup.
#
# Resolution order for every credential type:
#   1. Caller-supplied user_id  → user_tokens table (Fernet-encrypted)
#   2. Caller-supplied email    → user_tokens lookup by email → user_id
#   No platform/service-account fallback: if the user has not configured a
#   token, resolution raises PermissionError. Every GitLab/Jira/Confluence
#   operation must be authorised with the requesting user's OWN token.
#
# Product context resolution:
#   get_product_for_repo(repo_name) → {jira_project_key, confluence_space,
#                                       jira_url, confluence_url, product_id, product_name}
#   Callers inject this into SDLC / agent pipelines so every automated
#   action (ticket creation, Confluence page) lands in the right project/space.
# ============================================================

import os
from typing import Optional, Tuple
from core.logger import logger, mask_email


# ── Managed integration credential env-var names ──────────────
# These carry per-user secrets (GitLab PAT, Atlassian email/token). They must
# NEVER be inherited from the platform's os.environ (.env / service account) into
# a tool sandbox — otherwise a user with no configured token would silently act
# as the platform service account. Callers strip these from the inherited env and
# inject ONLY the requesting user's resolved values. Non-secret endpoint vars
# (GITLAB_URL, JIRA_URL, CONFLUENCE_URL) are intentionally excluded so tools can
# still discover which instance to target.
MANAGED_CREDENTIAL_ENV_KEYS = frozenset({
    "GITLAB_TOKEN",
    "GITHUB_TOKEN",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "CONFLUENCE_EMAIL",
    "CONFLUENCE_API_TOKEN",
})


def sanitized_environ() -> dict:
    """Return a copy of ``os.environ`` with all managed credential keys removed.

    Use this as the base env for any tool sandbox subprocess so platform-level
    integration secrets can never leak in. Per-user tokens are injected on top.
    """
    return {k: v for k, v in os.environ.items() if k not in MANAGED_CREDENTIAL_ENV_KEYS}


# ── Internal: decrypt a user_tokens row ──────────────────────
# SEC-F-020/032 follow-up (2026-08-26): delegates to
# store/credential_vault.py's decrypt_value(), which transparently handles
# both the current AES-256-GCM format and legacy (unprefixed) Fernet tokens
# written before this migration — was previously a second, independent
# Fernet-only implementation duplicating routers/profile_router.py's. Now
# both readers of user_tokens.encrypted_value share one crypto code path.

def _decrypt(encrypted: str) -> Optional[str]:
    try:
        from store.credential_vault import decrypt_value
        key = os.getenv("FERNET_KEY", "") or os.getenv("VAULT_ENCRYPTION_KEY", "")
        if not key:
            return None
        return decrypt_value(encrypted)
    except Exception as e:
        logger.warning(f"platform_credentials: decrypt failed: {e}")
        return None


def _token_by_user_id(user_id: str, token_type: str) -> Optional[str]:
    """Return decrypted token for (user_id, token_type) or None."""
    try:
        from db.database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("""
                    SELECT encrypted_value FROM user_tokens
                    WHERE user_id = :uid AND token_type = :ttype AND is_active = TRUE
                """),
                {"uid": user_id, "ttype": token_type},
            ).fetchone()
        if row:
            logger.info(f"TS- platform_credentials:_token_by_user_id fetched token {token_type}")
            return _decrypt(row[0])
    except Exception as e:
        logger.warning(f"platform_credentials: user_tokens lookup failed: {e}")
    return None


def _token_by_email(email: str, token_type: str) -> Optional[str]:
    """Resolve user_id from email then fetch the token."""
    if not email:
        return None
    try:
        from db.database import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id FROM users WHERE email = :e LIMIT 1"),
                {"e": email},
            ).fetchone()
        if row:
            # NB: this only resolved email→user_id; the token itself has NOT been
            # fetched yet (that is the _token_by_user_id call below, which may still
            # return None if the user has no stored token of this type).
            logger.info(f"TS- platform_credentials:_token_by_email resolved user_id for {token_type} lookup")
            return _token_by_user_id(str(row[0]), token_type)
    except Exception as e:
        logger.warning(f"platform_credentials: email→user_id lookup failed: {e}")
    return None


# ── Public: GitLab token ──────────────────────────────────────

def get_gitlab_token(user_id: str = "", email: str = "") -> str:
    """
    Resolve a GitLab PAT for the given user.
    Order: user_id → email → raises PermissionError.

    Admin/service-account credentials are never used; every git operation
    must be authorised with the requesting user's own stored token.

    The returned value is ALWAYS the bare PAT — the stored credential may be a
    bare token, ``username:token`` OR ``username@token`` (the Profile UI supports
    all three so it can also be baked into an HTTPS clone URL), and this
    normalises every shape via :func:`extract_gitlab_pat`. This is the single,
    authoritative normalization point: every consumer — the ``oauth2:<token>@``
    clone-URL builders (early-checkout, ``_authenticated_clone_url``,
    ``inject_gitlab_token``) and the ``PRIVATE-TOKEN`` REST header (``set_token``)
    — needs the bare token. Cross-verified against two runs on identical code:
    a stored ``username:token`` produced the broken ``oauth2:username:token@``
    userinfo (3 colon segments → HTTP Basic: Access denied), while a bare PAT on
    the same path succeeded. Normalising here makes both behave identically.
    """
    logger.info(f"TS- platform_credentials:get_gitlab_token received - {user_id}, {mask_email(email)}")
    if user_id:
        t = _token_by_user_id(user_id, "gitlab")
        if t:
            logger.info(f"platform_credentials: gitlab token from user_id={user_id}")
            return extract_gitlab_pat(t)
    if email:
        t = _token_by_email(email, "gitlab")
        if t:
            logger.debug(f"platform_credentials: gitlab token from email={mask_email(email)}")
            return extract_gitlab_pat(t)
    raise PermissionError(
        "No GitLab personal access token found for this user. "
        "Please add your GitLab token under Profile → GitLab Token before accessing this repository."
    )


def extract_gitlab_pat(stored_value: str) -> str:
    """Extract the bare GitLab PAT from a stored credential value.

    The Profile UI stores the GitLab credential in a user-chosen shape — a bare
    token, or ``username:token`` / ``username@token`` (so it can also be baked
    into an HTTPS clone URL). The GitLab REST API ``PRIVATE-TOKEN`` header needs
    the BARE token only. This returns that bare token regardless of format:

      1. If a ``glpat-``/``gloas-``/``glptt-`` prefixed substring is present,
         return it (this is unambiguous — GitLab's own token prefixes).
      2. Otherwise, if the value contains ``username:token`` or
         ``username@token``, return the segment after the last ``:`` / ``@``.
      3. Otherwise return the value unchanged (assume it is already a bare
         token — a legacy 20-char PAT has no prefix).

    No validation / rejection is performed: an invalid token is passed through so
    the GitLab API can return an authoritative 401 rather than us guessing.
    """
    v = (stored_value or "").strip()
    if not v:
        return v
    import re
    m = re.search(r"(?:glpat-|gloas-|glptt-)[A-Za-z0-9_\-]+", v)
    if m:
        return m.group(0)
    # No recognizable prefix: peel off a leading ``username:`` or ``username@``.
    for sep in ("@", ":"):
        if sep in v:
            return v.rsplit(sep, 1)[-1].strip()
    return v


def extract_atlassian_creds(stored_value: str, fallback_email: str = "") -> Tuple[str, str]:
    """Split a stored Atlassian credential into ``(email, api_token)``.

    The Atlassian Cloud REST API authenticates with
    ``Authorization: Basic base64(email:api_token)``, so BOTH halves are needed.
    The Profile UI stores them joined as ``email:token``, but older/hand-entered
    rows may hold a bare token. This normalises either shape:

      1. ``"a@b.com:ATATT..."`` -> ``("a@b.com", "ATATT...")``
      2. ``"ATATT..."``         -> ``(fallback_email, "ATATT...")``
      3. ``""``                 -> ``(fallback_email, "")``

    The split is on the FIRST colon only. An Atlassian API token is base64-ish and
    may itself contain a colon, so ``rsplit`` would truncate a valid token — unlike
    :func:`extract_gitlab_pat`, where the token alphabet makes ``rsplit`` safe.

    A value is treated as ``email:token`` only when the part before the colon looks
    like an email address. That keeps a bare token containing a colon from being
    mistaken for a pair and silently mangled.

    No validation / rejection is performed: an invalid credential is passed through
    so Atlassian can return an authoritative 401 rather than us guessing.
    """
    v = (stored_value or "").strip()
    if not v:
        return (fallback_email, "")
    if ":" in v:
        head, tail = v.split(":", 1)
        # "@" in the first segment is what distinguishes "email:token" from a bare
        # token that happens to contain a colon.
        if "@" in head:
            return (head.strip(), tail.strip())
    return (fallback_email, v)


def inject_gitlab_token(url: str, token: str) -> str:
    """Inject a GitLab PAT into an HTTPS clone URL as ``oauth2:<token>@``.

    Uses GitLab's standard CI userinfo form ``oauth2:<token>@host`` — NOT a
    bare ``<token>@host``. With the token normalized to a bare PAT
    (get_gitlab_token → extract_gitlab_pat), a bare-token userinfo has no
    password half, so git treats the PAT as a username and blocks on a
    password prompt ("could not read Password ... No such device or address").
    The ``oauth2:`` prefix supplies the username so the PAT is the password.
    Mirrors _authenticated_clone_url and the state-machine/baseline builders.
    """
    if token and "https://" in url:
        return url.replace("https://", f"https://oauth2:{token}@", 1)
    return url


# ── Public: GitHub token ──────────────────────────────────────
# Mirrors the GitLab helpers above so routers/index_router.py and the SDLC
# pipeline can resolve credentials the same way regardless of SCM_PROVIDER
# (core.config.SCM_PROVIDER).

def get_github_token(user_id: str = "", email: str = "") -> str:
    """
    Resolve a GitHub PAT for the given user.
    Order: user_id → email → raises PermissionError.

    Admin/service-account credentials are never used; every git operation
    must be authorised with the requesting user's own stored token.
    """
    if user_id:
        t = _token_by_user_id(user_id, "github")
        if t:
            logger.debug(f"platform_credentials: github token from user_id={user_id}")
            return t
    if email:
        t = _token_by_email(email, "github")
        if t:
            logger.debug(f"platform_credentials: github token from email={email}")
            return t
    raise PermissionError(
        "No GitHub personal access token found for this user. "
        "Please add your GitHub token under Profile → GitHub Token before accessing this repository."
    )


def extract_github_pat(stored_value: str) -> str:
    """Extract the bare GitHub PAT from a stored credential value.

    GitHub PATs (classic ``ghp_...``, fine-grained ``github_pat_...``, OAuth
    ``gho_...``) are always used bare — GitHub's REST API takes
    ``Authorization: Bearer <token>`` with no username component. Still,
    the Profile UI may store a ``username:token`` pair for layout symmetry
    with the GitLab field, so unwrap that shape the same way
    :func:`extract_gitlab_pat` does.
    """
    v = (stored_value or "").strip()
    if not v:
        return v
    import re
    m = re.search(r"(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]+", v)
    if m:
        return m.group(0)
    for sep in ("@", ":"):
        if sep in v:
            return v.rsplit(sep, 1)[-1].strip()
    return v


def inject_github_token(url: str, token: str) -> str:
    """Inject a GitHub PAT into an HTTPS clone URL (as the username segment,
    matching GitHub's supported ``https://<token>@github.com/...`` form)."""
    if token and "https://" in url:
        return url.replace("https://", f"https://{token}@")
    return url


def get_scm_token(user_id: str = "", email: str = "") -> str:
    """Resolve the active-provider (SCM_PROVIDER) token for the given user."""
    from core.config import SCM_PROVIDER
    if SCM_PROVIDER == "github":
        return get_github_token(user_id=user_id, email=email)
    return get_gitlab_token(user_id=user_id, email=email)


def inject_scm_token(url: str, token: str) -> str:
    """Inject the active-provider token into an HTTPS clone URL."""
    from core.config import SCM_PROVIDER
    if SCM_PROVIDER == "github":
        return inject_github_token(url, token)
    return inject_gitlab_token(url, token)


def strip_gitlab_token(url: str) -> str:
    """
    Remove any embedded credentials (``user:token@`` / ``oauth2:token@``) from an
    HTTPS git URL, returning the bare ``https://host[:port]/path`` form.

    Credentials must NEVER be persisted in ``repo_index_status.git_url`` — a stored
    URL is shared per-repo, so a baked-in token would be reused by every user's SDLC
    run. Callers strip on store and re-inject the requesting user's own token at
    clone time (see ``build_run_clone_url``). SSH (``git@``) and non-URL strings are
    returned unchanged.
    """
    if not url or "://" not in url:
        return url
    from urllib.parse import urlsplit, urlunsplit
    try:
        sp = urlsplit(url)
        if not sp.hostname:
            return url
        netloc = sp.hostname
        if sp.port:
            netloc = f"{netloc}:{sp.port}"
        return urlunsplit((sp.scheme, netloc, sp.path, sp.query, sp.fragment))
    except Exception:
        return url


def build_run_clone_url(stored_url: str, user_id: str = "", email: str = "") -> str:
    """
    Build the clone URL for a workspace: strip any credentials baked into the
    stored URL, then re-inject the appropriate GitLab PAT.

    User-triggered runs (``user_id`` and/or ``email`` supplied): the token is
    resolved from that user's own ``user_tokens`` entry and NOTHING else. If the
    user has not configured a token, this raises ``PermissionError`` — no
    fallback to a shared/service token, and no unauthenticated clone.

    Background/service jobs (no user identity supplied at all — e.g. nightly
    workspace sync, dependency builder): fall back to the service-account
    ``GITLAB_TOKEN`` env. This path has no requesting user, so the per-user
    guarantee does not apply. Callers that represent a user MUST pass user_id or
    email so they never silently reach this branch.
    """
    bare = strip_gitlab_token(stored_url)
    if user_id or email:
        # User context — user's own token only; raises if unconfigured.
        token = get_gitlab_token(user_id=user_id, email=email)
        logger.debug(
            f"platform_credentials: run clone URL authed for "
            f"user_id={user_id or '-'} email={email or '-'}"
        )
        return inject_gitlab_token(bare, token)

    # Service context — no requesting user; use the service-account token.
    token = os.getenv("GITLAB_TOKEN", "")
    if not token:
        logger.warning(
            "platform_credentials: service clone has no GITLAB_TOKEN; cloning without credentials"
        )
        return bare
    return inject_gitlab_token(bare, token)


# ── Public: Atlassian creds (Jira + Confluence) ───────────────

def get_atlassian_creds(user_id: str = "", email: str = "") -> Tuple[str, str]:
    """
    Resolve Atlassian (email, api_token) for Basic-auth calls.
    Order: user_id → email → raises PermissionError.

    Service-account credentials are never used; every Jira/Confluence operation
    must be authorised with the requesting user's own stored Atlassian token.

    The stored value is normalised through :func:`extract_atlassian_creds`, so the
    returned pair is ALWAYS a clean ``(email, bare_token)``. Callers build the
    header as ``base64(f"{email}:{token}")``; returning the raw stored string here
    (which is itself ``email:token``) put the address in twice and produced
    ``base64("a@b.com:a@b.com:ATATT...")``.
    """
    # User's personal token — auth email is their own email
    if user_id:
        t = _token_by_user_id(user_id, "atlassian")
        if t:
            # When the stored value already carries the email, use it directly and
            # skip the users lookup — that also keeps working if the DB query fails.
            pair_email, pair_token = extract_atlassian_creds(t)
            if pair_email:
                logger.debug(f"TS- platform_credentials:get_atlassian_creds atlassian token from user_id={user_id}")
                return (pair_email, pair_token)
            try:
                from db.database import engine
                from sqlalchemy import text
                with engine.connect() as conn:
                    row = conn.execute(
                        text("SELECT email FROM users WHERE id = :uid LIMIT 1"),
                        {"uid": user_id},
                    ).fetchone()
                if row:
                    logger.debug(f"TS- platform_credentials:get_atlassian_creds atlassian token from user_id={user_id}")
                    return extract_atlassian_creds(t, fallback_email=row[0])
            except Exception:
                pass
    if email:
        t = _token_by_email(email, "atlassian")
        if t:
            logger.debug(f"platform_credentials: atlassian token from email={mask_email(email)}")
            return extract_atlassian_creds(t, fallback_email=email)
    raise PermissionError(
        "No Atlassian personal access token found for this user. "
        "Please add your Atlassian token under Profile → Atlassian Token before accessing Jira/Confluence."
    )


# ── Public: Product context from repo ────────────────────────

def get_product_for_repo(repo_name: str) -> dict:
    """
    Look up the Product linked to a repo (via product_repos table).
    Returns a dict with:
        product_id, product_name, product_code,
        jira_project_key, confluence_space,
        jira_url, confluence_url
    or an empty dict if not found.

    repo_name can be the slug (e.g. "upi_stats") or full path ("group/upi-stats").
    Matching is fuzzy: tries exact repo_name match first, then slug-only.
    """
    logger.info(f"TS- platform_credentials:get_product_for_repo - {repo_name}")
    if not repo_name:
        return {}
    try:
        from db.database import engine
        from sqlalchemy import text

        # Normalise: strip trailing slashes, extract last segment as slug
        slug = repo_name.rstrip("/").split("/")[-1].lower().replace("-", "_").replace(".", "_")

        with engine.connect() as conn:
            # Try exact match first, then slug match on the repo_name column
            row = conn.execute(
                text("""
                    SELECT p.id, p.name, p.code,
                           p.jira_project_key, p.confluence_space,
                           p.jira_url, p.confluence_url
                    FROM product_repos pr
                    JOIN products p ON p.id = pr.product_id
                    WHERE p.is_active = TRUE
                      AND p.status    = 'ACTIVE'
                      AND (
                          pr.repo_name = :exact
                          OR LOWER(REPLACE(REPLACE(pr.repo_name, '-', '_'), '.', '_'))
                               LIKE '%' || :slug || '%'
                      )
                    ORDER BY
                      -- prefer exact over fuzzy
                      (CASE WHEN pr.repo_name = :exact THEN 0 ELSE 1 END)
                    LIMIT 1
                """),
                {"exact": repo_name, "slug": slug},
            ).fetchone()

        if row:
            return {
                "product_id":        str(row[0]),
                "product_name":      row[1] or "",
                "product_code":      row[2] or "",
                "jira_project_key":  row[3] or "",
                "confluence_space":  row[4] or "",
                "jira_url":          row[5] or "",
                "confluence_url":    row[6] or "",
            }
    except Exception as e:
        logger.warning(f"platform_credentials: get_product_for_repo({repo_name!r}) failed: {e}")
    return {}
