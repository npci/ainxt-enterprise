# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt AGENTIC PLATFORM — GitLab Integration Tools
#
# Provides read/write access to GitLab repositories via the
# REST API v4 using a GITLAB_TOKEN environment variable.
#
# Tools exposed:
#   gitlab_list_my_mrs             — the user's MRs across ALL projects (no repo arg)
#   gitlab_list_my_issues          — the user's issues across ALL projects (no repo arg)
#   gitlab_read_file               — read a file from a repo
#   gitlab_list_branches           — list branches in a repo
#   gitlab_list_issues             — list open/closed issues
#   gitlab_create_issue            — create a new issue
#   gitlab_list_mrs                — list merge requests
#   gitlab_create_mr               — create a merge request (idempotent)
#   gitlab_create_branch           — create a branch (idempotent)
#   gitlab_create_or_update_file   — create or update a file via commit
#   gitlab_comment_on_mr           — post a comment on an MR
#   gitlab_get_mr                  — get MR details
#   gitlab_link_mr_to_jira         — add Jira link comment to MR
#   gitlab_get_mr_review_comments  — fetch all MR notes/comments
#   gitlab_get_mr_reviews          — fetch MR approval state
#   gitlab_reply_to_review_comment — reply to a note thread
#   gitlab_get_mr_files            — get changed files with diffs
#   gitlab_create_mr_review        — post an MR review note
#   gitlab_merge_mr                — merge a merge request
# ============================================================

import os
import json
import threading
from typing import Optional
from urllib.parse import quote as _url_quote

from core.logger import logger

_GITLAB_URL  = os.getenv("GITLAB_URL", "https://<YOUR_GITLAB_URL>").rstrip("/")
_HTTPS_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or ""

# Per-thread token override — set by the SDLC pipeline after resolving from user_tokens.
# Avoids process-wide os.environ mutation which is unsafe across concurrent workers.
_thread_local = threading.local()


def set_token(token: str) -> None:
    """Set a per-thread GitLab token (called by pipeline after user_tokens lookup)."""
    # Normalize token:
    # If token is like "user:REAL_TOKEN", use "REAL_TOKEN"
    _token = token
    if _token and ":" in _token:
        # split only once so tokens containing ':' later aren't broken
        _token = _token.split(":", 1)[1]
    _thread_local.token = _token


def _resolve_token() -> str:
    """Return the active GitLab token: thread-local first, env var fallback."""
    return getattr(_thread_local, "token", None) or os.getenv("GITLAB_TOKEN", "")


def _clean(text: str) -> str:
    """Strip control/breaking characters before sending to GitLab API."""
    try:
        from core.prompt_sanitizer import sanitize
        return sanitize(str(text) if text is not None else "")
    except Exception:
        return str(text) if text is not None else ""
_GITLAB_API = f"{_GITLAB_URL}/api/v4"


def _proj(repo: str) -> str:
    """URL-encode 'namespace/project' for GitLab project path segments."""
    return _url_quote(repo, safe="")


def _headers() -> dict:
    token = _resolve_token()
    h: dict = {"Content-Type": "application/json"}
    if token:
        h["PRIVATE-TOKEN"] = token
    return h


# ============================================================
# LOW-LEVEL HTTP HELPERS
# ============================================================

def _make_opener():
    """Return a urllib opener that honours HTTPS_PROXY when set."""
    import urllib.request
    if _HTTPS_PROXY:
        handler = urllib.request.ProxyHandler({
            "https": _HTTPS_PROXY,
            "http":  _HTTPS_PROXY,
        })
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()


def _get(path: str) -> dict | list:
    import urllib.request
    from urllib.error import HTTPError
    from core.circuit_breaker import get_breaker

    url    = f"{_GITLAB_API}{path}"
    _tok   = _resolve_token()
    logger.info(f"[GitLab._get] url={url} token={'set('+str(len(_tok))+'chars)' if _tok else 'MISSING'} base={_GITLAB_URL}")
    req    = urllib.request.Request(url, headers=_headers())
    opener = _make_opener()

    def _do():
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())

    try:
        return get_breaker("gitlab").call(_do)
    except RuntimeError:
        logger.warning("GitLab GET %s circuit OPEN", path)
        return {"error": "circuit_open"}
    except HTTPError as _http_exc:
        # Log the HTTP status so an expected 404 (file/ref absent) is distinguishable
        # from an auth 401/403 or a 5xx — the bare "HTTP error" hid all of these. Match
        # the _post/_put convention. CWE-209: log the code/reason only, never the body
        # (which may echo the token or file contents).
        _code = getattr(_http_exc, "code", "?")
        logger.error("GitLab GET %s failed: HTTP %s", path, _code)
        return {"error": "http_error", "status": _code}
    except Exception as _exc:
        # Log the exception TYPE (e.g. URLError/SSLError/timeout/JSONDecodeError) so a
        # non-HTTP failure — DNS/TLS/proxy/timeout/bad-JSON — is diagnosable. CWE-209:
        # class name only, never str(_exc), which could contain the URL+token.
        logger.error("GitLab GET %s failed: unexpected error (%s)", path, type(_exc).__name__)
        return {"error": "unexpected_error"}


def _post(path: str, payload: dict) -> dict:
    import urllib.request
    from urllib.error import HTTPError
    from core.circuit_breaker import get_breaker

    url    = f"{_GITLAB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=_headers(), method="POST")
    opener = _make_opener()

    def _do():
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())

    try:
        return get_breaker("gitlab").call(_do)
    except RuntimeError:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.warning("GitLab POST %s circuit OPEN", path)
        return {"error": "circuit_open"}
    except HTTPError as _http_exc:
        _code = _http_exc.code
        try:
            body = _http_exc.read().decode()
        except Exception:  # noqa: BLE001
            body = ""
        logger.error("GitLab POST %s failed: HTTP %s", path, _code)
        return {"error": f"HTTP {_code}", "body": body}
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.error("GitLab POST %s failed", path)
        return {"error": "request failed"}


def _put(path: str, payload: dict) -> dict:
    import urllib.request
    from urllib.error import HTTPError
    from core.circuit_breaker import get_breaker

    url    = f"{_GITLAB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=_headers(), method="PUT")
    opener = _make_opener()

    def _do():
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())

    try:
        return get_breaker("gitlab").call(_do)
    except RuntimeError:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.warning("GitLab PUT %s circuit OPEN", path)
        return {"error": "circuit_open"}
    except HTTPError as _http_exc:
        _code = _http_exc.code
        try:
            body = _http_exc.read().decode()
        except Exception:  # noqa: BLE001
            body = ""
        logger.error("GitLab PUT %s failed: HTTP %s", path, _code)
        return {"error": f"HTTP {_code}", "body": body}
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.error("GitLab PUT %s failed", path)
        return {"error": "request failed"}

def _delete(path: str) -> dict:
    import urllib.request
    from urllib.error import HTTPError
    from core.circuit_breaker import get_breaker

    url    = f"{_GITLAB_API}{path}"
    req    = urllib.request.Request(url, headers=_headers(), method="DELETE")
    opener = _make_opener()

    def _do():
        with opener.open(req, timeout=60) as resp:
            body = resp.read().decode() or ""
            try:
                return json.loads(body) if body else {"status": resp.status}
            except Exception:
                return {"status": resp.status}

    try:
        return get_breaker("gitlab").call(_do)
    except RuntimeError:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.warning("GitLab DELETE %s circuit OPEN", path)
        return {"error": "circuit_open"}
    except HTTPError as _http_exc:
        _code = _http_exc.code
        try:
            body = _http_exc.read().decode()
        except Exception:  # noqa: BLE001
            body = ""
        logger.error("GitLab DELETE %s failed: HTTP %s", path, _code)
        return {"error": f"HTTP {_code}", "body": body, "status": _code}
    except Exception:  # noqa: BLE001
        # SECURITY: exception variable not referenced in log (CWE-209).
        logger.error("GitLab DELETE %s failed", path)
        return {"error": "request failed"}


# ============================================================
# DEFAULT BRANCH DETECTION
# ============================================================

_DEFAULT_BRANCH_CACHE: dict = {}


def _detect_default_branch(repo: str) -> str:
    """
    Return the default branch for a GitLab project.
    Uses the project metadata endpoint. Falls back to 'main'.
    """
    if repo in _DEFAULT_BRANCH_CACHE:
        return _DEFAULT_BRANCH_CACHE[repo]

    result = _get(f"/projects/{_proj(repo)}")
    if isinstance(result, dict) and "default_branch" in result:
        branch = result["default_branch"] or "main"
        logger.info(f"GitLab default branch for '{repo}': '{branch}'")
        _DEFAULT_BRANCH_CACHE[repo] = branch
        return branch

    logger.warning(f"GitLab: could not detect default branch for '{repo}', using 'main'")
    return "main"


# ============================================================
# TOOL FUNCTIONS
# ============================================================

def gitlab_read_file(repo: str, path: str, branch: str = "main") -> str:
    """
    Read a file from a GitLab repository.

    Args:
        repo:   Namespace/project e.g. "ainxt/payment-service"
        path:   File path e.g. "src/main/java/PaymentService.java"
        branch: Branch name (default: "main")

    Returns:
        Decoded file content as a string, or error message.
    """
    import base64
    try:
        encoded_path = _url_quote(path, safe="")
        result = _get(f"/projects/{_proj(repo)}/repository/files/{encoded_path}?ref={branch}")
        if isinstance(result, dict) and "error" in result:
            logger.error(f"gitlab_read_file({repo}, {path}, {branch}): {result['error']}")
            return f"[Error reading {repo}/{path}: {result['error']}]"
        if isinstance(result, dict) and result.get("encoding") == "base64":
            try:
                return base64.b64decode(result["content"]).decode("utf-8")
            except Exception:  # noqa: BLE001
                logger.error("gitlab_read_file: base64 decode failed")
                return f"[Error decoding {repo}/{path}]"
        return result.get("content", "[No content]") if isinstance(result, dict) else "[No content]"
    except Exception:  # noqa: BLE001
        logger.error("gitlab_read_file: unexpected error")
        return f"[Error reading {repo}/{path}]"


def gitlab_search_code(repo: str, query: str, max_results: int = 10) -> str:
    """
    Search for a code pattern or symbol across a GitLab repository.
    Uses the GitLab blobs search API (scope=blobs).

    Args:
        repo:        Namespace/project e.g. "ainxt/payment-service"
        query:       Code pattern, symbol name, or string to search for
        max_results: Max matches to return (default 10)

    Returns:
        Formatted string with matching file paths and snippets, or error message.
    """
    try:
        encoded_query = _url_quote(query, safe="")
        results = _get(
            f"/projects/{_proj(repo)}/search"
            f"?scope=blobs&search={encoded_query}&per_page={min(max_results, 20)}"
        )
        if isinstance(results, dict) and "error" in results:
            return f"[GitLab search error: {results['error']}]"
        if not isinstance(results, list) or not results:
            return f"No results found for '{query}' in {repo}."

        lines = [f"GitLab code search: '{query}' in {repo} — {len(results)} result(s)\n"]
        for r in results[:max_results]:
            fname    = r.get("filename", "?")
            ref      = r.get("ref", "")
            data     = r.get("data", "").strip()
            startline = r.get("startline", "")
            lines.append(f"• {fname}:{startline} (branch: {ref})")
            if data:
                snippet = data[:300].replace("\n", " ↩ ")
                lines.append(f"  {snippet}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        logger.error("gitlab_search_code: unexpected error")
        return f"[Error searching {repo}]"


def gitlab_apply_patch(
        repo: str,
        path: str,
        search: str,
        replace: str,
        branch: str = "main",
        message: str = "Apply patch",
) -> str:
    """
    Apply a surgical SEARCH/REPLACE patch to a file in a GitLab repository.

    Reads the current file, finds the exact `search` block, replaces it with
    `replace`, and commits the result.  Prefer this over gitlab_create_or_update_file
    for modifying existing files — it preserves all unchanged code.

    Args:
        repo:    Namespace/project e.g. "ainxt/payment-service"
        path:    File path e.g. "src/main/java/UpiController.java"
        search:  Exact block of existing code to find (whitespace must match exactly)
        replace: New code to substitute in its place
        branch:  Branch to read from and commit to (default: "main")
        message: Commit message

    Returns:
        Confirmation string with file URL, or an error message beginning with "[".
    """
    current = gitlab_read_file(repo, path, branch)
    if current.startswith("[Error") or current.startswith("["):
        return current
    search_stripped = search.strip()
    if search_stripped not in current:
        return (
            f"[PatchError] Search block not found in {repo}/{path} on branch {branch!r}. "
            "Whitespace and indentation must match exactly. "
            "Call gitlab_read_file first to copy the exact text you want to replace."
        )
    new_content = current.replace(search_stripped, replace.strip(), 1)
    if new_content == current:
        return f"[PatchError] No change produced — search block identical to replace block in {path}."
    return gitlab_create_or_update_file(repo, path, new_content, message, branch)


def gitlab_list_projects(limit: int = 50, membership: bool = True, search: str = "") -> list:
    """
    List GitLab projects/repositories the authenticated user has access to.

    Args:
        limit:      Maximum number of projects to return (default 50, max 100)
        membership: Only return projects the user is a member of (default True)
        search:     Filter projects by name
    """
    params_parts = [
        f"membership={str(membership).lower()}",
        f"per_page={min(limit, 100)}",
        "order_by=last_activity_at",
        "sort=desc",
    ]
    if search:
        params_parts.append(f"search={_url_quote(search)}")
    path = f"/projects?{'&'.join(params_parts)}"
    result = _get(path)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error: {result['error']}")
    if not isinstance(result, list):
        raise RuntimeError(f"GitLab API returned unexpected response: {type(result).__name__}")
    return [
        {
            "id": p.get("id"),
            "name": p.get("name"),
            "path_with_namespace": p.get("path_with_namespace"),
            "description": p.get("description", ""),
            "visibility": p.get("visibility"),
            "last_activity_at": p.get("last_activity_at"),
            "web_url": p.get("web_url"),
        }
        for p in result
    ]


def _ref_project(row: dict) -> str:
    """Extract the plain 'namespace/project' path from an instance-wide MR/issue row.

    The instance-wide /merge_requests and /issues endpoints do not return a project
    path directly. references.full is the closest thing, but it carries the item
    suffix ("acme/payments!7" for an MR, "acme/payments#7" for an issue), which is
    not a usable project_id for a follow-up call. Strip at the first ! or #, and fall
    back to web_url (the segment before /-/) then the numeric project_id.
    """
    full = ((row.get("references") or {}).get("full") or "").strip()
    if full:
        for sep in ("!", "#"):
            if sep in full:
                return full.split(sep, 1)[0]
        return full
    url = row.get("web_url") or ""
    if "/-/" in url:
        tail = url.split("/-/", 1)[0]
        # drop scheme + host → leave namespace/project
        parts = tail.split("://", 1)[-1].split("/", 1)
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return str(row.get("project_id", "") or "")


def _my_scoped(kind: str, scope: str, state: str, limit: int) -> list:
    """Shared helper for the cross-project "my work" endpoints.

    Hits the INSTANCE-WIDE /merge_requests or /issues endpoint (no project in the
    path), which GitLab scopes to the authenticated user. This is what answers
    "show me my open merge requests" — a question that names no project and
    therefore cannot be served by any /projects/:id/... tool.

    Args:
        kind:  "merge_requests" | "issues"
        scope: "assigned_to_me" | "created_by_me" | "all"
        state: "open" | "closed" | "merged" | "all"
        limit: max rows
    """
    gl_state = "opened" if state == "open" else state
    parts = [
        f"scope={scope}",
        f"per_page={min(max(int(limit or 20), 1), 50)}",
        "order_by=updated_at",
        "sort=desc",
    ]
    if gl_state and gl_state != "all":
        parts.append(f"state={gl_state}")
    result = _get(f"/{kind}?{'&'.join(parts)}")
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error: {result['error']}")
    if not isinstance(result, list):
        raise RuntimeError(f"GitLab API returned unexpected response: {type(result).__name__}")
    return result


def gitlab_list_my_mrs(scope: str = "assigned_to_me", state: str = "open",
                       limit: int = 20) -> list:
    """
    List the authenticated user's merge requests ACROSS ALL PROJECTS.

    This is the tool for "show me my open merge requests", "what's waiting on my
    review", "my MRs" — questions that name no project. Unlike gitlab_list_mrs it
    needs NO repo argument.

    Args:
        scope: "assigned_to_me" (default — needs my review/action),
               "created_by_me" (MRs I opened), or "all"
        state: "open" (default) | "closed" | "merged" | "all"
        limit: Maximum number of MRs to return (default 20, max 50)
    """
    if scope not in ("assigned_to_me", "created_by_me", "all"):
        scope = "assigned_to_me"
    rows = _my_scoped("merge_requests", scope, state, limit)
    return [
        {
            "iid":           mr.get("iid"),
            "project":       _ref_project(mr),
            "title":         mr.get("title"),
            "state":         mr.get("state"),
            "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"),
            "author":        (mr.get("author") or {}).get("username", "?"),
            "draft":         bool(mr.get("draft") or mr.get("work_in_progress")),
            "updated_at":    mr.get("updated_at"),
            "web_url":       mr.get("web_url"),
        }
        for mr in rows
    ]


def gitlab_list_my_issues(scope: str = "assigned_to_me", state: str = "open",
                          limit: int = 20) -> list:
    """
    List the authenticated user's GitLab issues ACROSS ALL PROJECTS.

    This is the tool for "my open issues", "issues assigned to me" — questions that
    name no project. Unlike gitlab_list_issues it needs NO repo argument.

    Args:
        scope: "assigned_to_me" (default) | "created_by_me" | "all"
        state: "open" (default) | "closed" | "all"
        limit: Maximum number of issues to return (default 20, max 50)
    """
    if scope not in ("assigned_to_me", "created_by_me", "all"):
        scope = "assigned_to_me"
    rows = _my_scoped("issues", scope, state, limit)
    return [
        {
            "iid":        i.get("iid"),
            "project":    _ref_project(i),
            "title":      i.get("title"),
            "state":      i.get("state"),
            "labels":     i.get("labels") or [],
            "author":     (i.get("author") or {}).get("username", "?"),
            "updated_at": i.get("updated_at"),
            "web_url":    i.get("web_url"),
        }
        for i in rows
    ]


def gitlab_get_project(repo: str) -> dict:
    """
    Get details of a GitLab project.

    Args:
        repo: Namespace/project path e.g. "myorg/myrepo"

    Returns:
        Dict with project details (id, name, description, visibility,
        default_branch, web_url, star_count, forks_count, open_issues_count).
    """
    result = _get(f"/projects/{_proj(repo)}")
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error: {result['error']}")
    if not isinstance(result, dict):
        raise RuntimeError(f"GitLab API returned unexpected response for project {repo!r}")
    return {
        "id":                result.get("id"),
        "name":              result.get("name"),
        "path_with_namespace": result.get("path_with_namespace"),
        "description":       result.get("description", ""),
        "visibility":        result.get("visibility"),
        "default_branch":    result.get("default_branch"),
        "web_url":           result.get("web_url"),
        "last_activity_at":  result.get("last_activity_at"),
        "star_count":        result.get("star_count", 0),
        "forks_count":       result.get("forks_count", 0),
        "open_issues_count": result.get("open_issues_count", 0),
    }


def gitlab_list_commits(repo: str, ref_name: str = "", limit: int = 25) -> list:
    """
    List recent commits for a GitLab project branch.

    Args:
        repo:     Namespace/project path e.g. "myorg/myrepo"
        ref_name: Branch, tag, or commit SHA (default: project's default branch)
        limit:    Max commits to return (default 25, max 50)

    Returns:
        List of commit dicts with id, short_id, title, author_name,
        authored_date, message, and web_url.
    """
    params_parts = [f"per_page={min(limit, 50)}"]
    if ref_name:
        params_parts.append(f"ref_name={_url_quote(ref_name)}")
    path = f"/projects/{_proj(repo)}/repository/commits?{'&'.join(params_parts)}"
    result = _get(path)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error: {result['error']}")
    if not isinstance(result, list):
        raise RuntimeError(f"GitLab API returned unexpected response for commits in {repo!r}")
    return [
        {
            "id":            c.get("id"),
            "short_id":      c.get("short_id"),
            "title":         c.get("title"),
            "author_name":   c.get("author_name"),
            "authored_date": c.get("authored_date"),
            "message":       c.get("message", ""),
            "web_url":       c.get("web_url"),
        }
        for c in result
    ]


def gitlab_list_branches(repo: str, search: str = "", limit: int = 50) -> list:
    """
    List branches in a GitLab project.

    Args:
        repo:   Namespace/project path e.g. "myorg/myrepo"
        search: Optional filter — GitLab's native `search` query param matches
                branch names containing this string (default: no filter, all branches)
        limit:  Max branches to return (default 50, max 100)

    Returns:
        List of branch dicts with name, default (bool), protected (bool),
        merged (bool), and the branch tip's short_id/title/committed_date,
        plus web_url.
    """
    params_parts = [f"per_page={min(limit, 100)}"]
    if search:
        params_parts.append(f"search={_url_quote(search)}")
    path = f"/projects/{_proj(repo)}/repository/branches?{'&'.join(params_parts)}"
    result = _get(path)
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error: {result['error']}")
    if not isinstance(result, list):
        raise RuntimeError(f"GitLab API returned unexpected response for branches in {repo!r}")
    return [
        {
            "name":            b.get("name"),
            "default":         b.get("default", False),
            "protected":       b.get("protected", False),
            "merged":          b.get("merged", False),
            "commit_short_id": (b.get("commit") or {}).get("short_id"),
            "commit_title":    (b.get("commit") or {}).get("title"),
            "committed_date":  (b.get("commit") or {}).get("committed_date"),
            "web_url":         b.get("web_url", f"{_GITLAB_URL}/{repo}/-/tree/{b.get('name', '')}"),
        }
        for b in result
    ]


def gitlab_list_issues(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List issues in a GitLab project.

    Args:
        repo:  Namespace/project
        state: "open" | "closed" | "all"  (GitLab uses "opened"/"closed")
        limit: Maximum number of issues to return
    """
    # GitLab state values
    gl_state = "opened" if state == "open" else state
    result = _get(f"/projects/{_proj(repo)}/issues?state={gl_state}&per_page={limit}")
    if isinstance(result, dict) and "error" in result:
        return f"[Error listing issues: {result['error']}]"
    if not isinstance(result, list):
        return f"[Unexpected response: {result}]"
    if not result:
        return f"No {state} issues found in {repo}."
    lines = [f"Issues in {repo} ({state}):"]
    for issue in result:
        lines.append(
            f"  #{issue['iid']} [{issue['state']}] {issue['title']} "
            f"— {(issue.get('author') or {}).get('username', '?')}"
        )
    return "\n".join(lines)


def gitlab_create_issue(repo: str, title: str, body: str = "", labels: list = None) -> str:
    """
    Create a new issue in a GitLab project.

    Args:
        repo:   Namespace/project
        title:  Issue title
        body:   Issue description (markdown)
        labels: Optional list of label names

    Returns:
        URL of the created issue, or error message.
    """
    title = _clean(title)
    body  = _clean(body)
    payload: dict = {"title": title, "description": body}
    if labels:
        payload["labels"] = ",".join(labels)
    result = _post(f"/projects/{_proj(repo)}/issues", payload)
    if "error" in result:
        return f"[Error creating issue: {result['error']}]"
    return f"Issue created: {result.get('web_url', '?')} (#{result.get('iid', '?')})"


def gitlab_list_mrs(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List merge requests in a GitLab project.

    Args:
        repo:  Namespace/project
        state: "open" | "closed" | "merged" | "all"
        limit: Maximum number of MRs to return
    """
    gl_state = "opened" if state == "open" else state
    result = _get(f"/projects/{_proj(repo)}/merge_requests?state={gl_state}&per_page={limit}")
    if isinstance(result, dict) and "error" in result:
        return f"[Error listing MRs: {result['error']}]"
    if not isinstance(result, list):
        return f"[Unexpected response: {result}]"
    if not result:
        return f"No {state} MRs found in {repo}."
    lines = [f"Merge Requests in {repo} ({state}):"]
    for mr in result:
        lines.append(
            f"  !{mr['iid']} [{mr['state']}] {mr['title']} "
            f"← {mr.get('source_branch', '?')} "
            f"— {(mr.get('author') or {}).get('username', '?')}"
        )
    return "\n".join(lines)


def gitlab_create_mr(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    draft: bool = False,
) -> str:
    """
    Create a merge request in a GitLab project.
    Idempotent: if an MR already exists for the source branch, returns the
    existing MR instead of failing.

    Args:
        repo:  Namespace/project
        title: MR title
        body:  MR description (markdown)
        head:  Source branch name
        base:  Target branch name (default: "main"; auto-detected if "main")
        draft: If True, prefix the title with "Draft: " (GitLab's draft
               convention) so the MR is created non-mergeable.
    Returns:
        URL of the created/existing MR.
    """
    if base == "main":
        base = _detect_default_branch(repo)

    title = _clean(title)
    body  = _clean(body)

    if draft and not title.lower().startswith(("draft:", "wip:")):
        title = f"Draft: {title}"

    # Prepend any waiver banners from the run context (waive mode audit trail).
    # Banners are un-collapsible markers that appear at the top of the MR
    # description so reviewers see them at merge time.
    # We attempt to look up the run by matching the source branch to sdlc_runs.branch.
    try:
        from db.database import SessionLocal as _SL
        from sqlalchemy import text as _sqt
        _sess = _SL()
        try:
            _row = _sess.execute(
                _sqt("SELECT context FROM sdlc_runs WHERE branch = :b LIMIT 1"),
                {"b": head},
            ).fetchone()
            if _row and _row.context:
                _ctx = _row.context if isinstance(_row.context, dict) else {}
                _banners = _ctx.get("waiver_banners") or []
                if _banners:
                    banner_block = "\n".join(f"> {b}" for b in _banners)
                    body = banner_block + "\n\n---\n\n" + body
        finally:
            _sess.close()
    except Exception:
        pass  # non-fatal — banner is best-effort

    payload = {
        "title":          title,
        "description":    body,
        "source_branch":  head,
        "target_branch":  base,
        "remove_source_branch": False,
    }
    result = _post(f"/projects/{_proj(repo)}/merge_requests", payload)

    if "error" in result:
        # GitLab returns 409 or message about existing MR
        body_text = result.get("body", "")
        err_str   = str(result.get("error", ""))
        if "409" in err_str or "already exists" in body_text.lower() or "409" in body_text:
            existing = _find_existing_mr(repo, head)
            if existing:
                logger.info(
                    f"GitLab MR already exists for {repo}/{head} — "
                    f"returning existing MR !{existing['iid']}"
                )
                return f"MR created: {existing['web_url']} (!{existing['iid']})"
        return f"[Error creating MR: {result['error']}]"

    return f"MR created: {result.get('web_url', '?')} (!{result.get('iid', '?')})"


def _find_existing_mr(repo: str, source_branch: str) -> dict | None:
    """Return the first open MR whose source branch matches, or None."""
    result = _get(
        f"/projects/{_proj(repo)}/merge_requests"
        f"?state=opened&source_branch={_url_quote(source_branch, safe='')}&per_page=10"
    )
    if isinstance(result, list):
        for mr in result:
            if mr.get("source_branch") == source_branch:
                return mr
    return None

def gitlab_compare(repo: str, from_ref: str, to_ref: str) -> dict:
    """Compare two refs via GitLab's repository compare API.

    Returns the raw payload (a dict with 'diffs' and 'commits'), or
    {"error": ...} on failure. Never raises. `from_ref` is the base and
    `to_ref` is the branch under test — 'diffs' lists the files that differ.
    """
    try:
        result = _get(
            f"/projects/{_proj(repo)}/repository/compare"
            f"?from={_url_quote(from_ref, safe='')}&to={_url_quote(to_ref, safe='')}"
        )
        if isinstance(result, dict):
            return result
        return {"error": f"unexpected compare response: {type(result).__name__}"}
    except Exception as e:
        logger.warning(f"[GITLAB] compare failed repo={repo} {from_ref}..{to_ref}: {e}")
        return {"error": str(e)}


def gitlab_branch_has_changes(repo: str, base: str, head: str) -> Optional[bool]:
    """Return True if `head` has ≥1 file diff over `base`, False if there is no
    diff at all, or None when it cannot be determined (API error / malformed
    response). Callers guarding MR creation should treat None as "undetermined"
    and FAIL-OPEN (proceed) so a transient GitLab hiccup never silently drops a
    real change.
    """
    cmp = gitlab_compare(repo, base, head)
    if not isinstance(cmp, dict) or "error" in cmp:
        return None
    diffs = cmp.get("diffs")
    if diffs is None:
        return None
    return len(diffs) > 0

def gitlab_set_mr_draft(repo: str, mr_iid: int, draft: bool) -> dict:
    """
    Flip an existing MR's draft/mergeable state by rewriting its title with
    (or without) GitLab's "Draft: " prefix convention.

    Fetches the current title, computes the new title (adding "Draft: " when
    draft=True, or stripping any existing "Draft: "/"WIP: " prefix when
    draft=False), then PUTs the update.

    Args:
        repo:   Namespace/project
        mr_iid: Merge request internal ID
        draft:  Desired draft state

    Returns:
        dict with at least "title" and "web_url" of the updated MR, or
        {"error": ...} on failure.
    """
    try:
        current = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}")
        if isinstance(current, dict) and "error" in current:
            raise RuntimeError(current["error"])

        current_title = current.get("title", "") if isinstance(current, dict) else ""

        if draft:
            if current_title.lower().startswith(("draft:", "wip:")):
                new_title = current_title
            else:
                new_title = f"Draft: {current_title}"
        else:
            new_title = current_title
            for prefix in ("Draft: ", "draft: ", "WIP: ", "wip: "):
                if new_title.startswith(prefix):
                    new_title = new_title[len(prefix):]
                    break

        result = _put(
            f"/projects/{_proj(repo)}/merge_requests/{mr_iid}",
            {"title": new_title},
        )
        if isinstance(result, dict) and "error" in result:
            raise RuntimeError(result["error"])

        logger.info("[GITLAB] Draft flag flipped", repo=repo, mr_iid=mr_iid, draft=draft)
        return {
            "title":   result.get("title", new_title),
            "web_url": result.get("web_url", ""),
            "iid":     result.get("iid", mr_iid),
        }
    except Exception as e:
        logger.error("[GITLAB] set MR draft failed", repo=repo, mr_iid=mr_iid, error=str(e))
        return {"error": str(e)}



# ============================================================
# BRANCH & FILE OPERATIONS
# ============================================================

def gitlab_create_branch(repo: str, branch: str, from_branch: str = "main") -> str:
    """
    Create a new branch in a GitLab project.
    Idempotent: reuses the branch if it already exists.

    Args:
        repo:        Namespace/project
        branch:      New branch name
        from_branch: Source branch (default: "main"; auto-detected if "main")
    """
    if from_branch == "main":
        from_branch = _detect_default_branch(repo)

    url = f"{_GITLAB_URL}/{repo}/-/tree/{branch}"

    # Check if branch already exists
    existing = _get(f"/projects/{_proj(repo)}/repository/branches/{_url_quote(branch, safe='')}")
    if isinstance(existing, dict) and "error" not in existing and existing.get("name"):
        logger.info(f"GitLab branch already exists, reusing: {repo}/{branch}")
        return f"Branch exists (reusing): {branch} — {url}"

    result = _post(
        f"/projects/{_proj(repo)}/repository/branches",
        {"branch": branch, "ref": from_branch},
    )
    if "error" in result:
        err_str  = str(result.get("error", ""))
        body_txt = result.get("body", "")
        if "already exists" in body_txt.lower() or "already exists" in err_str.lower():
            logger.info(f"GitLab branch already exists (race), reusing: {repo}/{branch}")
            return f"Branch exists (reusing): {branch} — {url}"
        return f"[Error creating branch: {result['error']}]"

    logger.info(f"GitLab branch created: {repo}/{branch} (from {from_branch})")
    return f"Branch created: {branch} from {from_branch} — {url}"

def gitlab_delete_branch(repo: str, branch: str) -> str:
    """
    Delete a branch in a GitLab project. Best-effort: a 404 (already gone) is
    treated as success so callers can use this for idempotent cleanup.

    Args:
        repo:   Namespace/project
        branch: Branch name to delete
    """
    encoded = _url_quote(branch, safe="")
    result  = _delete(f"/projects/{_proj(repo)}/repository/branches/{encoded}")
    if isinstance(result, dict) and "error" in result:
        if result.get("status") == 404:
            logger.info(f"GitLab branch already absent: {repo}/{branch}")
            return f"Branch already absent: {branch}"
        return f"[Error deleting branch: {result['error']}]"
    logger.info(f"GitLab branch deleted: {repo}/{branch}")
    return f"Branch deleted: {branch}"


def gitlab_create_or_update_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> str:
    """
    Create or update a file in a GitLab repository via a commit.

    Args:
        repo:    Namespace/project
        path:    File path in the repo
        content: New file content (plain text)
        message: Commit message
        branch:  Target branch (default: "main")

    Returns:
        Confirmation string with file URL.
    """
    content = _clean(content)
    message = _clean(message)
    encoded_path = _url_quote(path, safe="")
    proj_path    = f"/projects/{_proj(repo)}/repository/files/{encoded_path}"

    # Check if file exists (determines POST vs PUT)
    existing = _get(f"{proj_path}?ref={branch}")
    exists   = isinstance(existing, dict) and "error" not in existing and existing.get("file_name")

    payload = {
        "branch":         branch,
        "commit_message": message,
        "content":        content,
        "encoding":       "text",
    }

    if exists:
        result = _put(proj_path, payload)
        action = "Updated"
    else:
        result = _post(proj_path, payload)
        action = "Created"

    if "error" in result:
        return f"[Error writing file: {result['error']}]"

    file_url = f"{_GITLAB_URL}/{repo}/-/blob/{branch}/{path}"
    logger.info(f"GitLab {action} {repo}/{path} on {branch}")
    return f"{action} {path} on {branch} — {file_url}"


# ============================================================
# ATOMIC BATCH COMMIT  (W-A: atomic, retried, resumable commit)
# ============================================================

# Injectable sleep so unit tests can patch out the real backoff delay.
# Production code calls _sleep(secs); tests monkeypatch this module attribute.
import time as _time
_sleep = _time.sleep

# Number of attempts for a transient batch-commit failure. Read at call time
# (env-overridable, no restart) — default 3.
def _commit_retries() -> int:
    try:
        n = int(os.getenv("SDLC_COMMIT_RETRIES", "3"))
        return n if n >= 1 else 3
    except (TypeError, ValueError):
        logger.warning("SDLC_COMMIT_RETRIES is not a valid int — using default 3")
        return 3


# Exponential backoff schedule (seconds) between attempts. Index i is the wait
# BEFORE attempt i+1. If more attempts than entries, the last value repeats.
_COMMIT_BACKOFF = [2, 8, 20]

# Gitaly "4:Deadline Exceeded" is a server-side gRPC timeout — usually transient
# load on the GitLab/Gitaly tier.  Needs longer waits than a 429 or 5xx.
_DEADLINE_BACKOFF = [15, 30, 60]


def _is_transient_commit_error(result: dict) -> bool:
    """True when a failed batch commit looks retryable (Gitaly deadline, rate
    limit, 5xx, or a raw connection error). Mirrors the circuit-breaker intent
    but at the request level so ONE atomic commit can be retried whole."""
    err  = str(result.get("error", "")).lower()
    body = str(result.get("body", "")).lower()
    blob = err + " " + body
    if "deadline exceeded" in blob:           # Gitaly "4:Deadline Exceeded"
        return True
    if "http 429" in blob or " 429" in blob:  # rate limited
        return True
    for code in ("500", "502", "503", "504"):
        if f"http {code}" in blob:
            return True
    # urllib connection-level failures surface as plain strings (no HTTP code)
    if "error" in result and "http " not in err and (
        "timed out" in blob or "timeout" in blob or "connection" in blob
        or "reset" in blob or "refused" in blob or "urlopen" in blob
    ):
        return True
    return False


def gitlab_batch_commit(repo: str, branch: str, actions: list, message: str) -> str:
    """
    Commit multiple files in ONE atomic transaction via the GitLab Commits API
    (POST /projects/:id/repository/commits with an actions[] array).

    Either every file lands or none do — this replaces the per-file commit loop
    that left a run half-committed (and FAILED) when a single Gitaly
    "4:Deadline Exceeded" hit one file mid-loop.

    Args:
        repo:    Namespace/project (e.g. "ainxt/payment-service").
        branch:  Target branch (must already exist).
        actions: List of file actions. Each entry is a dict with:
                   action    — only "create" | "update" | "delete" are
                               actually supported end-to-end today. "move"/
                               "chmod" values are recognized by the action
                               whitelist below and passed through as-is, but
                               NO caller builds them and the entry builder
                               only copies action/file_path(/content) — a
                               "move" would 400 on GitLab for want of the
                               required `previous_path` key. Plumb that in
                               before relying on move/chmod.
                   file_path — path in the repo
                   content   — file content (plain text; omitted for "delete")
                 Callers map is_new==True → "create" else "update".
                 "delete" is honoured verbatim — pre-flight never rewrites it
                 into an update; a delete for a path already absent on the
                 branch is dropped so retries are idempotent.
        message: Commit message.

    Retry: the whole commit is retried up to SDLC_COMMIT_RETRIES (default 3)
    times on transient failures (Gitaly deadline, 429, 5xx, connection errors)
    with exponential backoff. The sleep is module-level `_sleep` so tests can
    patch it.

    Returns:
        "Batch commit OK: <N> file(s) on <branch> — <sha> (<web_url>)" on success,
        or "[Error batch commit: ...]" on a non-transient or exhausted failure.
    """
    # Build a clean, GitLab-shaped actions array. Sanitize content/message the
    # same way gitlab_create_or_update_file does.
    gl_actions = []
    for a in actions or []:
        fp = a.get("file_path") or a.get("path") or ""
        if not fp:
            continue
        act = (a.get("action") or "update").lower()
        if act not in ("create", "update", "delete", "move", "chmod"):
            act = "update"
        entry = {"action": act, "file_path": fp}
        # GitLab rejects/ignores `content` on a delete — omit it rather than
        # sending an empty string. create/update keep today's exact shape.
        if act != "delete":
            entry["content"] = _clean(a.get("content", ""))
        gl_actions.append(entry)

    if not gl_actions:
        return "[Error batch commit: no file actions provided]"

    # ── Pre-flight: resolve correct create/update action for every file ──────
    # HEAD-check each file on the branch BEFORE the first commit attempt.
    # This eliminates the "A file with this name doesn't exist" 400 error that
    # occurs when is_new was misclassified — e.g. a new file under a new package
    # path that wasn't in the file_tree captured at analysis time.
    # Cost: N GET calls once — cheaper than a failed commit + full retry cycle.
    #
    # Only create/update actions are eligible for correction — a caller-supplied
    # "delete"/"move"/"chmod" is an explicit intent and must NEVER be rewritten
    # into an update (that would silently resurrect a file the caller asked to
    # remove). A delete whose path is already absent on the branch is dropped
    # instead, so a retried/resumed commit is idempotent rather than 400-ing the
    # whole atomic transaction.
    proj_id = _proj(repo)
    ref_enc = _url_quote(branch, safe="")
    _preflight_fixed   = 0
    _preflight_dropped = 0
    _resolved: list = []
    _preflight_kept_inconclusive = 0
    for a in gl_actions:
        fp_enc  = _url_quote(a["file_path"], safe="")
        chk     = _get(f"/projects/{proj_id}/repository/files/{fp_enc}?ref={ref_enc}")
        exists  = isinstance(chk, dict) and "error" not in chk and chk.get("file_name")
        if a["action"] == "delete":
            if exists:
                _resolved.append(a)
                continue
            # `_get` returns {"error": ...} for EVERY failure mode — a genuine
            # 404, but also 5xx, auth failures, timeouts, and a RuntimeError
            # when the "gitlab" circuit breaker is OPEN. Only a confirmed 404
            # proves the file is actually absent; any other error is
            # inconclusive. Misreading an inconclusive probe as "already gone"
            # would silently drop a real, human-approved deletion — unlike a
            # wrong create/update (self-correcting via the mid-run 400 retry
            # below), a dropped delete is silent and unrecoverable. So on an
            # inconclusive probe we keep the delete and let GitLab arbitrate:
            # a real 400 there is loud and recoverable.
            _is_404 = (
                    isinstance(chk, dict)
                    and "error" in chk
                    and str(chk.get("error", "")).startswith("HTTP 404")
            )
            if _is_404:
                _preflight_dropped += 1
                continue          # confirmed absent — nothing to delete
            _preflight_kept_inconclusive += 1
            logger.warning(
                f"GitLab batch commit: pre-flight existence probe inconclusive for "
                f"delete of {a['file_path']!r} on {repo}/{branch} "
                f"({chk.get('error') if isinstance(chk, dict) else 'unknown'}) — "
                f"keeping delete in payload rather than assuming it's already gone"
            )
            _resolved.append(a)
            continue
        if a["action"] not in ("create", "update"):
            _resolved.append(a)   # move/chmod: pass through untouched
            continue
        correct = "update" if exists else "create"
        if a["action"] != correct:
            a["action"] = correct
            _preflight_fixed += 1
        _resolved.append(a)
    if _preflight_fixed or _preflight_dropped or _preflight_kept_inconclusive:
        logger.info(
            f"GitLab batch commit: pre-flight corrected {_preflight_fixed}/{len(gl_actions)} "
            f"action(s), dropped {_preflight_dropped} already-absent delete(s), "
            f"kept {_preflight_kept_inconclusive} delete(s) with inconclusive probe "
            f"on {repo}/{branch}"
        )
    gl_actions = _resolved
    if not gl_actions:
        # Every action was an already-applied delete — a genuine no-op, NOT an
        # error. Keep the "Batch commit OK:" prefix so callers that only check
        # for a "[Error" prefix treat this as success.
        logger.info(f"GitLab batch commit: nothing to do on {repo}/{branch} (all deletes already applied)")
        return f"Batch commit OK: 0 file(s) on {branch} — nothing to do (all actions already applied)"

    payload = {
        "branch":         branch,
        "commit_message": _clean(message),
        "actions":        gl_actions,
    }

    path          = f"/projects/{proj_id}/repository/commits"
    max_attempts  = _commit_retries()
    last_result: dict = {}
    _action_resolved  = False  # action-correction fires at most once; free (no attempt cost)
    attempt = 0

    while True:
        attempt += 1
        result = _post(path, payload)

        if "error" not in result:
            sha     = result.get("id", "") or result.get("short_id", "")
            web_url = result.get("web_url", "")
            logger.info(
                f"GitLab batch commit OK: {len(gl_actions)} file(s) on {repo}/{branch} "
                f"sha={sha[:12] if sha else '?'}"
            )
            return (
                f"Batch commit OK: {len(gl_actions)} file(s) on {branch} — "
                f"{sha} ({web_url})"
            )

        last_result = result
        body = str(result.get("body", "")).lower()

        # create/update action mismatch — correct per-file and retry for free.
        # Pre-flight above should make this branch rarely fire; kept as safety net.
        # "Free" = doesn't count against the attempt budget (attempt -= 1 then continue).
        _action_err = (
                "already exists" in body
                or "doesn't exist" in body
                or "does not exist" in body
                or "not exist" in body
        )
        if _action_err and not _action_resolved:
            _action_resolved = True
            attempt -= 1  # free correction — not deducted from retry budget
            fixed = 0
            for a in gl_actions:
                # Same rule as the pre-flight: never rewrite an explicit
                # delete/move/chmod into a create/update.
                if a["action"] not in ("create", "update"):
                    continue
                fp_enc  = _url_quote(a["file_path"], safe="")
                chk     = _get(f"/projects/{proj_id}/repository/files/{fp_enc}?ref={ref_enc}")
                exists  = isinstance(chk, dict) and "error" not in chk and chk.get("file_name")
                correct = "update" if exists else "create"
                if a["action"] != correct:
                    a["action"] = correct
                    fixed += 1
            logger.info(
                f"GitLab batch commit: mid-run action correction on {repo}/{branch} "
                f"— corrected {fixed}/{len(gl_actions)} action(s), retrying atomically"
            )
            payload["actions"] = gl_actions
            continue  # re-enter loop with corrected payload; attempt counter unchanged

        # Decide whether to retry the whole atomic commit.
        if attempt < max_attempts and _is_transient_commit_error(last_result):
            # Gitaly "4:Deadline Exceeded" is a server-side gRPC timeout — needs
            # longer backoff than a 429 or 5xx to give GitLab time to recover.
            _err_blob = (
                    str(last_result.get("error", "")).lower()
                    + " "
                    + str(last_result.get("body", "")).lower()
            )
            _is_dl = "deadline exceeded" in _err_blob
            backoff_table = _DEADLINE_BACKOFF if _is_dl else _COMMIT_BACKOFF
            wait = backoff_table[min(attempt - 1, len(backoff_table) - 1)]
            logger.warning(
                f"GitLab batch commit transient failure on {repo}/{branch} "
                f"(attempt {attempt}/{max_attempts}) — retrying in {wait}s: "
                f"{last_result.get('error')}"
            )
            _sleep(wait)
            continue
        break

    return f"[Error batch commit: {last_result.get('error', 'unknown error')}]"


# ============================================================
# MR COMMENT / REVIEW OPERATIONS
# ============================================================

def gitlab_comment_on_mr(repo: str, mr_iid: int, body: str) -> str:
    """
    Post a note (comment) on a merge request.

    Args:
        repo:   Namespace/project
        mr_iid: MR internal ID (iid)
        body:   Comment body (markdown)
    """
    body = _clean(body)
    result = _post(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes",
        {"body": body},
    )
    if "error" in result:
        return f"[Error posting MR comment: {result['error']}]"
    url = result.get("noteable_iid", mr_iid)
    logger.info(f"GitLab MR!{mr_iid} comment posted in {repo}")
    return f"Comment posted on MR !{mr_iid}: {_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"


def gitlab_get_mr(repo: str, mr_iid: int) -> str:
    """Get details of a specific merge request."""
    result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}")
    if isinstance(result, dict) and "error" in result:
        return f"[Error: {result['error']}]"
    return (
        f"MR !{mr_iid}: {result.get('title','')}\n"
        f"State: {result.get('state','')}\n"
        f"Branch: {result.get('source_branch','')} → {result.get('target_branch','')}\n"
        f"Author: {(result.get('author') or {}).get('username','')}\n"
        f"URL: {result.get('web_url','')}\n"
        f"Body:\n{(result.get('description') or '')[:500]}"
    )


def gitlab_link_mr_to_jira(repo: str, mr_iid: int, jira_key: str) -> str:
    """Add a Jira ticket reference as an MR comment."""
    body = (
        f"**Jira Reference:** [{jira_key}]"
        f"(https://ainxt.atlassian.net/browse/{jira_key})\n\n"
        f"This MR is linked to Jira issue {jira_key}."
    )
    return gitlab_comment_on_mr(repo, mr_iid, body)


# Stable marker every governance note starts with (matches
# agents.sdlc_governance.engine._render_md's first line) — used to find + update
# a prior governance note instead of stacking a new one each run.
_GOVERNANCE_NOTE_MARKER = "## Governance Review"


def gitlab_post_governance_note(project: str, mr_iid: int, report_md: str) -> str:
    """
    Post/update the governance review note on an MR — NOTE BODY ONLY, no inline
    line anchors (deployment rule). Idempotent: finds a PRIOR governance note (by the
    stable ``_GOVERNANCE_NOTE_MARKER`` header the report_md always starts with)
    and UPDATEs it in place via GitLab REST v4's PUT notes endpoint; if none is
    found, creates a new one. Caller must call set_token() first — this makes no
    assumption about which token is active. Never raises: any error is logged
    and surfaced as a returned "[Error ...]" string (mirrors every other
    gitlab_* tool in this module), so a note-post failure can be treated as
    best-effort by callers without a try/except.
    """
    body = _clean(report_md or "")
    if not body.strip():
        logger.warning(f"gitlab_post_governance_note: empty report_md for MR !{mr_iid} in {project} — skipped")
        return "[Skipped: empty report_md]"

    notes = _get(f"/projects/{_proj(project)}/merge_requests/{mr_iid}/notes?per_page=100&sort=desc")
    existing_id = None
    if isinstance(notes, list):
        for n in notes:
            if isinstance(n, dict) and not n.get("system") and (n.get("body") or "").startswith(_GOVERNANCE_NOTE_MARKER):
                existing_id = n.get("id")
                break

    if existing_id:
        result = _put(
            f"/projects/{_proj(project)}/merge_requests/{mr_iid}/notes/{existing_id}",
            {"body": body},
        )
        if isinstance(result, dict) and "error" in result:
            logger.error(f"gitlab_post_governance_note: update failed for MR !{mr_iid}: {result['error']}")
            return f"[Error updating governance note: {result['error']}]"
        logger.info(f"GitLab MR!{mr_iid} governance note updated (note #{existing_id}) in {project}")
        return f"Governance note updated on MR !{mr_iid}: {_GITLAB_URL}/{project}/-/merge_requests/{mr_iid}"

    result = _post(f"/projects/{_proj(project)}/merge_requests/{mr_iid}/notes", {"body": body})
    if isinstance(result, dict) and "error" in result:
        logger.error(f"gitlab_post_governance_note: create failed for MR !{mr_iid}: {result['error']}")
        return f"[Error posting governance note: {result['error']}]"
    logger.info(f"GitLab MR!{mr_iid} governance note created in {project}")
    return f"Governance note posted on MR !{mr_iid}: {_GITLAB_URL}/{project}/-/merge_requests/{mr_iid}"


def gitlab_get_mr_review_comments(repo: str, mr_iid: int) -> str:
    """
    Fetch all notes (comments) on a merge request.

    Returns:
        Formatted list of review notes as a string.
    """
    notes = _get(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes?per_page=100&sort=asc"
    )

    lines = [f"Review comments on MR !{mr_iid} in {repo}:"]
    count = 0

    if isinstance(notes, list):
        for c in notes:
            if c.get("system"):
                continue   # skip system events (branch created, etc.)
            author = (c.get("author") or {}).get("username", "?")
            body   = c.get("body", "").strip()
            cid    = c.get("id", "?")
            # Skip our own bot comments
            if "[AiNxt]" in body or "AI-Generated" in body:
                continue
            lines.append(f"\n[note #{cid}] {author}\n  {body}")
            count += 1

    if count == 0:
        return f"No review comments found on MR !{mr_iid}."

    lines.append(f"\nTotal: {count} comment(s)")
    return "\n".join(lines)

def gitlab_get_mr_diff_notes(repo: str, mr_iid: int) -> list:
    """
    Fetch MR discussion notes WITH their diff `position` preserved (unlike
    gitlab_get_mr_review_comments, which flattens to a string and drops it).

    Reads the discussions endpoint (positioned DiffNotes carry a `position`
    object). Returns a list of dicts — one per human, non-system, non-AiNxt note:

        {
          "author":   "<username>",
          "body":     "<comment text>",
          "new_path": "<file path in the new revision, or ''>",
          "old_path": "<file path in the old revision, or ''>",
          "new_line": <int or None>,
          "old_line": <int or None>,
          "note_id":  <int>,
        }

    Un-positioned (general) notes come back with empty paths / None lines so the
    caller can still surface them as whole-MR feedback. Never raises — returns
    [] on transport error.
    """
    discussions = _get(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/discussions?per_page=100"
    )
    out: list = []
    if not isinstance(discussions, list):
        logger.warning(
            f"gitlab_get_mr_diff_notes: unexpected discussions payload for MR !{mr_iid} in {repo}"
        )
        return out
    for d in discussions:
        for note in (d.get("notes") or []):
            if note.get("system"):
                continue
            body = (note.get("body") or "").strip()
            if not body:
                continue
            # Skip our own bot comments (posted as [AiNxt] / AI-Generated).
            if "[AiNxt]" in body or "AI-Generated" in body:
                continue
            pos = note.get("position") or {}
            out.append({
                "author":   (note.get("author") or {}).get("username", "?"),
                "body":     body,
                "new_path": pos.get("new_path") or "",
                "old_path": pos.get("old_path") or "",
                "new_line": pos.get("new_line"),
                "old_line": pos.get("old_line"),
                "note_id":  note.get("id"),
            })
    logger.info(
        f"gitlab_get_mr_diff_notes: {len(out)} human note(s) on MR !{mr_iid} in {repo}"
    )
    return out

def gitlab_get_mr_reviews(repo: str, mr_iid: int) -> str:
    """
    Fetch approval state for a merge request.
    """
    result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/approvals")
    if isinstance(result, dict) and "error" in result:
        return f"[Error fetching approvals: {result['error']}]"
    approved_by = [
        a.get("user", {}).get("username", "?")
        for a in (result.get("approved_by") or [])
    ]
    approved = result.get("approved", False)
    lines = [f"Approvals on MR !{mr_iid}:"]
    if approved_by:
        lines.append(f"  Approved by: {', '.join(approved_by)}")
    lines.append(f"  Approved: {approved}")
    return "\n".join(lines)


def gitlab_reply_to_review_comment(repo: str, mr_iid: int, note_id: int, body: str) -> str:
    """
    Reply to a note thread on a merge request.
    """
    # Find the discussion ID for this note
    discussions = _get(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/discussions?per_page=100"
    )
    discussion_id = None
    if isinstance(discussions, list):
        for d in discussions:
            for note in (d.get("notes") or []):
                if note.get("id") == note_id:
                    discussion_id = d.get("id")
                    break
            if discussion_id:
                break

    if not discussion_id:
        # Fall back to a new top-level note
        return gitlab_comment_on_mr(repo, mr_iid, body)

    result = _post(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}"
        f"/discussions/{discussion_id}/notes",
        {"body": body},
    )
    if "error" in result:
        return f"[Error replying to note #{note_id}: {result['error']}]"
    logger.info(f"GitLab: replied to note #{note_id} on MR !{mr_iid}")
    return f"Reply posted to note #{note_id}: {_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"


def gitlab_get_mr_files(repo: str, mr_iid: int, max_files: int = 20) -> list:
    """
    Return changed files for an MR with their diff content.

    Each item: {"filename", "status", "additions", "deletions", "patch"}
    """
    result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/changes")
    if isinstance(result, dict) and "error" in result:
        raise RuntimeError(f"GitLab API error in gitlab_get_mr_files: {result['error']}")
    changes = result.get("changes", []) if isinstance(result, dict) else []
    files = []
    for f in changes[:max_files]:
        diff = f.get("diff", "")
        # Count +/- lines
        additions = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
        deletions = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
        status = "added" if f.get("new_file") else ("deleted" if f.get("deleted_file") else "modified")
        files.append({
            "filename":  f.get("new_path", f.get("old_path", "")),
            "status":    status,
            "additions": additions,
            "deletions": deletions,
            "patch":     diff,
        })
    return files


# ── Governance (STEP 9, 2026-07-17) — standalone review helpers ────────────
# Thin wrappers reused by workers/sdlc_worker.py::run_governance_review_job for
# the repo/MR standalone mode (no existing sdlc_runs row / VERIFIED_DIFF artifact
# to read a diff from). Both are read-only GET calls — set_token() must be
# called by the caller before invoking either, same as every other function here.

def gitlab_get_mr_diff(repo: str, mr_iid: int) -> tuple:
    """
    Build a (diff_text, changed_files, source_branch, target_branch) tuple for an
    MR, for a standalone governance review that has no CodingStateMachine/
    VERIFIED_DIFF to read a diff from. Reuses gitlab_get_mr_files (per-file unified
    diff patches) — concatenated into one diff_text. Never raises: a GitLab error
    returns ("", [], "", "") so the caller treats it as "no diff available".
    """
    result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}")
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"gitlab_get_mr_diff: {result['error']}")
        return "", [], "", ""
    source_branch = result.get("source_branch", "") if isinstance(result, dict) else ""
    target_branch = result.get("target_branch", "") if isinstance(result, dict) else ""
    files = gitlab_get_mr_files(repo, mr_iid, max_files=200)
    chunks, changed_files = [], []
    for f in files:
        patch = f.get("patch") or ""
        if patch.strip():
            chunks.append(patch)
        if f.get("filename"):
            changed_files.append(f["filename"])
    return "\n".join(chunks), changed_files, source_branch, target_branch


def gitlab_get_project_clone_url(repo: str) -> str:
    """
    Resolve the bare (no-credentials) HTTPS clone URL for a project. Standalone
    governance clones (repo/branch mode) have no repo_index_status row to read
    git_url from (unlike CodingStateMachine._ensure_run_workspace), so this hits
    the GitLab project API directly. Returns "" on any error/missing field.
    """
    result = _get(f"/projects/{_proj(repo)}")
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"gitlab_get_project_clone_url: {result['error']}")
        return ""
    return (result or {}).get("http_url_to_repo", "") if isinstance(result, dict) else ""


def gitlab_create_mr_review(
    repo: str,
    mr_iid: int,
    body: str,
    event: str = "COMMENT",           # APPROVE | REQUEST_CHANGES | COMMENT
    comments: list = None,
) -> str:
    """
    Post an MR review note. For APPROVE/REQUEST_CHANGES, uses the GitLab
    approvals API. Falls back to a plain note for COMMENT.

    Args:
        repo:      Namespace/project
        mr_iid:    MR internal ID
        body:      Review summary (markdown)
        event:     APPROVE | REQUEST_CHANGES | COMMENT
        comments:  Ignored (GitLab inline requires diff position data)
    """
    body = _clean(body)
    if event == "APPROVE":
        _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/approve", {})

    result = _post(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes",
        {"body": body},
    )
    if "error" in result:
        return f"[Error creating MR review: {result['error']}]"

    url = f"{_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"
    logger.info(f"GitLab MR!{mr_iid} review posted in {repo} ({event})")
    return f"MR review posted (event={event}): {url}"


def gitlab_merge_mr(
    repo: str,
    mr_iid: int,
    merge_method: str = "squash",
) -> str:
    """
    Merge a merge request.

    Args:
        repo:         Namespace/project
        mr_iid:       MR internal ID
        merge_method: "squash" | "merge" | "rebase" (default: "squash")

    Returns:
        Confirmation string or error.
    """
    squash = merge_method == "squash"
    result = _put(
        f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/merge",
        {"squash": squash, "should_remove_source_branch": False},
    )
    if "error" in result:
        return f"[Error merging MR !{mr_iid}: {result['error']}]"
    sha = result.get("sha", "?")
    logger.info(f"GitLab: merged MR !{mr_iid} in {repo} ({merge_method}) sha={sha}")
    return f"MR !{mr_iid} merged ({merge_method}) — commit sha: {sha}"


# ============================================================
# COMMIT STATUS (CI INTEGRATION)
# ============================================================

def gitlab_set_commit_status(
    repo: str,
    work_dir: str,
    state: str,
    description: str,
    context: str = "ainxt/security-scan",
) -> None:
    """
    Post a commit status to GitLab (shows in MR pipeline status).
    state: "pending" | "running" | "success" | "failed" | "canceled"
    """
    import subprocess as _sp

    token = _resolve_token()
    if not token:
        logger.warning("[GitLab] no GitLab token available — cannot post commit status")
        return

    try:
        r = _sp.run(
            ["git", "rev-parse", "HEAD"],
            cwd=work_dir, capture_output=True, text=True, timeout=300
        )
        sha = r.stdout.strip()
    except Exception:
        logger.warning("[GitLab] Could not get HEAD SHA — skipping commit status")
        return

    if not sha:
        return

    result = _post(
        f"/projects/{_proj(repo)}/statuses/{sha}",
        {"state": state, "description": description[:140], "name": context},
    )
    if "error" in result:
        logger.warning(f"[GitLab] Failed to post commit status: {result['error']}")
    else:
        logger.info(f"[GitLab] Commit status '{state}' posted to {repo}@{sha[:8]}")


# ============================================================
# REPO FILE TREE
# ============================================================

def _get_file_tree(repo: str, branch: str) -> list:
    """
    Return all blob paths in the repo (recursive file tree).
    Handles pagination (GitLab returns max 100 per page).
    """
    paths = []
    page  = 1
    while True:
        result = _get(
            f"/projects/{_proj(repo)}/repository/tree"
            f"?ref={branch}&recursive=true&per_page=100&page={page}"
        )
        if not isinstance(result, list) or not result:
            break
        paths.extend(item["path"] for item in result if item.get("type") == "blob")
        if len(result) < 100:
            break
        page += 1
    return paths
