# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt AGENTIC PLATFORM — GitHub Integration Tools
#
# Provides read/write access to GitHub repositories via the
# REST API using a GITHUB_TOKEN environment variable.
#
# Tools exposed:
#   github_read_file     — read a file from a repo
#   github_list_issues   — list open/closed issues
#   github_create_issue  — create a new issue
#   github_list_prs      — list pull requests
#   github_create_pr     — create a pull request
# ============================================================

import os
import json
import threading
from typing import Optional

from core.logger import logger

GITHUB_API = "https://api.github.com"

# Per-thread token override — set by the connector adapter / SDLC pipeline after
# resolving the requesting user's own PAT from user_tokens. Mirrors the same
# pattern used by tools/gitlab_tools.py so both backends can be swapped via
# SCM_PROVIDER without touching call sites that inject the token.
_thread_local = threading.local()


def set_token(token: str) -> None:
    """Set a per-thread GitHub token (called by the connector adapter after
    resolving the requesting user's own PAT). Accepts a bare token or a
    "user:token" pair for symmetry with gitlab_tools.set_token()."""
    _token = token
    if _token and ":" in _token:
        _token = _token.split(":", 1)[1]
    _thread_local.token = _token


def _resolve_token() -> str:
    """Return the active GitHub token: thread-local first, env var fallback."""
    return getattr(_thread_local, "token", None) or os.getenv("GITHUB_TOKEN", "")


def _headers() -> dict:
    token = _resolve_token()
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(path: str) -> dict:
    import urllib.request
    from urllib.error import HTTPError
    from core.circuit_breaker import get_breaker

    url = f"{GITHUB_API}{path}"
    req = urllib.request.Request(url, headers=_headers())

    try:
        get_breaker("github").call(lambda: None)  # circuit-breaker state check only
    except RuntimeError:
        logger.warning("GitHub GET %s circuit OPEN", path)
        return {"error": "circuit_open"}

    try:
        import contextlib
        with contextlib.closing(urllib.request.urlopen(req, timeout=10)) as _conn:
            _body = _conn.read().decode()
        return json.loads(_body)
    except HTTPError as e:
        _not_found = (e.code == 404)  # status code used for control flow only
        if _not_found:
            logger.debug("GitHub GET %s: not found (404)", path)
        else:
            logger.error("GitHub GET %s failed with an HTTP error", path)
        return {"error": "not_found" if _not_found else "http_error"}
    except Exception:  # noqa: BLE001
        logger.error("GitHub GET %s failed", path)
        return {"error": "request failed"}


def _post(path: str, payload: dict) -> dict:
    import urllib.request
    from core.circuit_breaker import get_breaker

    url = f"{GITHUB_API}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )

    try:
        get_breaker("github").call(lambda: None)  # circuit-breaker state check only
    except RuntimeError:
        logger.warning("GitHub POST %s circuit OPEN", path)
        return {"error": "circuit_open"}

    try:
        import contextlib
        with contextlib.closing(urllib.request.urlopen(req, timeout=10)) as _conn:
            _body = _conn.read().decode()
        return json.loads(_body)
    except Exception:  # noqa: BLE001
        logger.error("GitHub POST %s failed", path)
        return {"error": "request failed"}


# ============================================================
# DEFAULT BRANCH DETECTION
# ============================================================

_DEFAULT_BRANCH_CACHE: dict = {}   # repo → branch name (in-process cache)
_CANDIDATE_BRANCHES = ("main", "master", "develop", "dev")


def _detect_default_branch(repo: str) -> str:
    """
    Return the first of main / master / develop / dev that exists in *repo*.
    Result is cached for the lifetime of the process.
    Falls back to 'main' if none of the candidates exist or on any error.
    """
    if repo in _DEFAULT_BRANCH_CACHE:
        return _DEFAULT_BRANCH_CACHE[repo]

    for candidate in _CANDIDATE_BRANCHES:
        result = _get(f"/repos/{repo}/git/ref/heads/{candidate}")
        if "error" not in result and result.get("object", {}).get("sha"):
            logger.info(f"GitHub default branch for '{repo}': '{candidate}'")
            _DEFAULT_BRANCH_CACHE[repo] = candidate
            return candidate

    logger.warning(f"GitHub: could not detect default branch for '{repo}', using 'main'")
    return "main"


# ============================================================
# TOOL FUNCTIONS
# ============================================================

def github_read_file(repo: str, path: str, branch: str = "main") -> str:
    """
    Read a file from a GitHub repository.

    Args:
        repo:   Owner/repo e.g. "ainxt/payment-service"
        path:   File path e.g. "src/main/java/PaymentService.java"
        branch: Branch name (default: "main")

    Returns:
        Decoded file content as a string, or error message.
    """
    import base64
    result = _get(f"/repos/{repo}/contents/{path}?ref={branch}")
    if "error" in result:
        return f"[Error reading {repo}/{path}: {result['error']}]"
    if result.get("encoding") == "base64":
        try:
            content = base64.b64decode(result["content"]).decode("utf-8")
            return content
        except Exception:
            return f"[Error decoding {repo}/{path}]"
    return result.get("content", "[No content]")


def github_list_issues(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List issues in a GitHub repository.

    Args:
        repo:  Owner/repo
        state: "open" | "closed" | "all"
        limit: Maximum number of issues to return

    Returns:
        Formatted list of issues as a string.
    """
    result = _get(f"/repos/{repo}/issues?state={state}&per_page={limit}")
    if isinstance(result, dict) and "error" in result:
        return f"[Error listing issues: {result['error']}]"
    if not isinstance(result, list):
        return f"[Unexpected response: {result}]"
    if not result:
        return f"No {state} issues found in {repo}."
    lines = [f"Issues in {repo} ({state}):"]
    for issue in result:
        lines.append(
            f"  #{issue['number']} [{issue['state']}] {issue['title']} "
            f"— {issue.get('user', {}).get('login', '?')}"
        )
    return "\n".join(lines)


def github_create_issue(repo: str, title: str, body: str = "", labels: list = None) -> str:
    """
    Create a new issue in a GitHub repository.

    Args:
        repo:   Owner/repo
        title:  Issue title
        body:   Issue body (markdown)
        labels: Optional list of label names

    Returns:
        URL of the created issue, or error message.
    """
    payload = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    result = _post(f"/repos/{repo}/issues", payload)
    if "error" in result:
        return f"[Error creating issue: {result['error']}]"
    return f"Issue created: {result.get('html_url', '?')} (#{result.get('number', '?')})"


def github_list_prs(repo: str, state: str = "open", limit: int = 20) -> str:
    """
    List pull requests in a GitHub repository.

    Args:
        repo:  Owner/repo
        state: "open" | "closed" | "all"
        limit: Maximum number of PRs to return

    Returns:
        Formatted list of PRs as a string.
    """
    result = _get(f"/repos/{repo}/pulls?state={state}&per_page={limit}")
    if isinstance(result, dict) and "error" in result:
        return f"[Error listing PRs: {result['error']}]"
    if not isinstance(result, list):
        return f"[Unexpected response: {result}]"
    if not result:
        return f"No {state} PRs found in {repo}."
    lines = [f"Pull Requests in {repo} ({state}):"]
    for pr in result:
        lines.append(
            f"  #{pr['number']} [{pr['state']}] {pr['title']} "
            f"← {pr.get('head', {}).get('ref', '?')} "
            f"— {pr.get('user', {}).get('login', '?')}"
        )
    return "\n".join(lines)


def github_create_pr(
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
) -> str:
    """
    Create a pull request in a GitHub repository.
    Idempotent: if a PR already exists for the head branch, returns the
    existing PR instead of failing with 422.

    Args:
        repo:  Owner/repo
        title: PR title
        body:  PR description (markdown)
        head:  Source branch name
        base:  Target branch name (default: "main"; auto-detected if not found)

    Returns:
        URL of the created PR, or error message.
    """
    # Auto-detect default branch if caller left the default "main"
    if base == "main":
        base = _detect_default_branch(repo)

    payload = {"title": title, "body": body, "head": head, "base": base}
    result = _post(f"/repos/{repo}/pulls", payload)

    if "error" in result:
        err_str = str(result.get("error", ""))
        # 422 = "A pull request already exists for this branch"
        # Look up the existing PR and return it instead of failing.
        if "422" in err_str:
            existing = _find_existing_pr(repo, head)
            if existing:
                logger.info(
                    f"GitHub PR already exists for {repo}/{head} — "
                    f"returning existing PR #{existing['number']}"
                )
                return f"PR created: {existing['html_url']} (#{existing['number']})"
        return f"[Error creating PR: {result['error']}]"

    return f"PR created: {result.get('html_url', '?')} (#{result.get('number', '?')})"


def _find_existing_pr(repo: str, head: str) -> dict | None:
    """Return the first open PR whose head branch matches `head`, or None."""
    # GitHub filters by head as "owner:branch" or just "branch"
    result = _get(f"/repos/{repo}/pulls?state=open&head={head}&per_page=10")
    if isinstance(result, list):
        for pr in result:
            if pr.get("head", {}).get("ref") == head:
                return pr
    # Fallback: search without owner prefix (handles fork vs same-repo branches)
    result2 = _get(f"/repos/{repo}/pulls?state=open&per_page=50")
    if isinstance(result2, list):
        for pr in result2:
            if pr.get("head", {}).get("ref") == head:
                return pr
    return None


# ============================================================
# ENTERPRISE EXTENSIONS
# ============================================================

def _patch(path: str, payload: dict) -> dict:
    import urllib.request as ureq
    url = f"{GITHUB_API}{path}"
    data = json.dumps(payload).encode()
    req = ureq.Request(
        url, data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        import contextlib
        with contextlib.closing(ureq.urlopen(req, timeout=10)) as _conn:
            _body = _conn.read().decode()
        return json.loads(_body)
    except Exception:  # noqa: BLE001
        logger.error("GitHub PATCH %s failed", path)
        return {"error": "request failed"}


def _put_gh(path: str, payload: dict) -> dict:
    import urllib.request as ureq
    url = f"{GITHUB_API}{path}"
    data = json.dumps(payload).encode()
    req = ureq.Request(
        url, data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        import contextlib
        with contextlib.closing(ureq.urlopen(req, timeout=10)) as _conn:
            _body = _conn.read().decode()
        return json.loads(_body)
    except Exception:  # noqa: BLE001
        logger.error("GitHub PUT %s failed", path)
        return {"error": "request failed"}


def github_create_branch(repo: str, branch: str, from_branch: str = "main") -> str:
    """
    Create a new branch in a GitHub repository.
    If the branch already exists (HTTP 422), returns success using the existing branch.

    Args:
        repo:        Owner/repo (e.g. "ainxt/payments")
        branch:      New branch name
        from_branch: Source branch (default: "main"; auto-detected if not found)

    Returns:
        Confirmation string with branch URL.
    """
    # Auto-detect if the requested base branch doesn't exist
    if from_branch == "main":
        from_branch = _detect_default_branch(repo)

    url = f"https://github.com/{repo}/tree/{branch}"

    # Check if branch already exists — reuse if so (idempotent)
    existing = _get(f"/repos/{repo}/git/ref/heads/{branch}")
    if "error" not in existing and existing.get("object", {}).get("sha"):
        logger.info(f"GitHub branch already exists, reusing: {repo}/{branch}")
        return f"Branch exists (reusing): {branch} — {url}"

    # Get SHA of source branch
    source = _get(f"/repos/{repo}/git/ref/heads/{from_branch}")
    if "error" in source:
        return f"[Error reading source branch: {source['error']}]"
    sha = source.get("object", {}).get("sha", "")
    if not sha:
        return f"[Could not get SHA for {from_branch}]"

    result = _post(f"/repos/{repo}/git/refs", {
        "ref": f"refs/heads/{branch}",
        "sha": sha,
    })
    if "error" in result:
        # 422 = branch already exists (race condition) — treat as success
        err_str = str(result.get("error", ""))
        if "422" in err_str or "already exists" in err_str.lower():
            logger.info(f"GitHub branch already exists (422), reusing: {repo}/{branch}")
            return f"Branch exists (reusing): {branch} — {url}"
        return f"[Error creating branch: {result['error']}]"

    logger.info(f"GitHub branch created: {repo}/{branch} (from {from_branch})")
    return f"Branch created: {branch} from {from_branch} — {url}"


def github_create_or_update_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> str:
    """
    Create or update a file in a GitHub repository.

    Args:
        repo:    Owner/repo
        path:    File path in the repo
        content: New file content (plain text; will be base64-encoded)
        message: Commit message
        branch:  Target branch (default: "main")

    Returns:
        Confirmation string with commit URL.
    """
    import base64

    encoded = base64.b64encode(content.encode()).decode()

    # Get current SHA if file exists (required for updates)
    existing = _get(f"/repos/{repo}/contents/{path}?ref={branch}")
    sha = existing.get("sha")

    payload = {
        "message": message,
        "content": encoded,
        "branch":  branch,
    }
    if sha:
        payload["sha"] = sha

    result = _put_gh(f"/repos/{repo}/contents/{path}", payload)
    if "error" in result:
        return f"[Error writing file: {result['error']}]"

    commit_url = result.get("commit", {}).get("html_url", "?")
    action = "Updated" if sha else "Created"
    logger.info(f"GitHub {action} {repo}/{path} on {branch}")
    return f"{action} {path} on {branch} — commit: {commit_url}"


def github_comment_on_pr(repo: str, pr_number: int, body: str) -> str:
    """
    Post a review comment on a pull request.

    Args:
        repo:      Owner/repo
        pr_number: PR number
        body:      Comment body (markdown)

    Returns:
        Confirmation string with comment URL.
    """
    result = _post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": body})
    if "error" in result:
        return f"[Error posting PR comment: {result['error']}]"
    url = result.get("html_url", "?")
    logger.info(f"GitHub PR#{pr_number} comment posted → {url}")
    return f"Comment posted on PR #{pr_number}: {url}"


def github_get_pr(repo: str, pr_number: int) -> str:
    """Get details of a specific pull request."""
    result = _get(f"/repos/{repo}/pulls/{pr_number}")
    if "error" in result:
        return f"[Error: {result['error']}]"
    return (
        f"PR #{pr_number}: {result.get('title','')}\n"
        f"State: {result.get('state','')}\n"
        f"Branch: {result.get('head',{}).get('ref','')} → {result.get('base',{}).get('ref','')}\n"
        f"Author: {result.get('user',{}).get('login','')}\n"
        f"URL: {result.get('html_url','')}\n"
        f"Body:\n{result.get('body','')[:500]}"
    )


def github_link_pr_to_jira(repo: str, pr_number: int, jira_key: str) -> str:
    """
    Add a Jira ticket reference to a PR comment (links work items).
    """
    body = (
        f"**Jira Reference:** [{jira_key}]"
        f"(https://ainxt.atlassian.net/browse/{jira_key})\n\n"
        f"This PR is linked to Jira issue {jira_key}."
    )
    return github_comment_on_pr(repo, pr_number, body)


def github_get_pr_review_comments(repo: str, pr_number: int) -> str:
    """
    Fetch all review comments (inline + general) on a pull request.

    Args:
        repo:      Owner/repo
        pr_number: PR number

    Returns:
        Formatted list of review comments as a string.
    """
    # Inline review comments (on specific lines)
    inline = _get(f"/repos/{repo}/pulls/{pr_number}/comments?per_page=100")
    # General PR comments (issue-level)
    general = _get(f"/repos/{repo}/issues/{pr_number}/comments?per_page=100")

    lines = [f"Review comments on PR #{pr_number} in {repo}:"]
    count = 0

    if isinstance(inline, list):
        for c in inline:
            path    = c.get("path", "")
            line    = c.get("line") or c.get("original_line", "?")
            author  = c.get("user", {}).get("login", "?")
            body    = c.get("body", "").strip()
            cid     = c.get("id", "?")
            lines.append(f"\n[inline #{cid}] {author} on {path}:{line}\n  {body}")
            count += 1

    if isinstance(general, list):
        for c in general:
            author  = c.get("user", {}).get("login", "?")
            body    = c.get("body", "").strip()
            cid     = c.get("id", "?")
            # Skip bot-generated comments (our own notifications)
            if "[AiNxt]" in body or "AI-Generated" in body:
                continue
            lines.append(f"\n[general #{cid}] {author}\n  {body}")
            count += 1

    if count == 0:
        return f"No review comments found on PR #{pr_number}."

    lines.append(f"\nTotal: {count} comment(s)")
    return "\n".join(lines)


def github_get_pr_reviews(repo: str, pr_number: int) -> str:
    """
    Fetch all reviews (approved/changes_requested/commented) on a PR.

    Args:
        repo:      Owner/repo
        pr_number: PR number

    Returns:
        Formatted review summary string.
    """
    result = _get(f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100")
    if isinstance(result, dict) and "error" in result:
        return f"[Error fetching reviews: {result['error']}]"
    if not isinstance(result, list):
        return f"[Unexpected response: {result}]"
    if not result:
        return f"No reviews found on PR #{pr_number}."

    lines = [f"Reviews on PR #{pr_number}:"]
    for r in result:
        state  = r.get("state", "?")
        author = r.get("user", {}).get("login", "?")
        body   = (r.get("body") or "").strip()[:200]
        lines.append(f"  {author}: {state}" + (f" — {body}" if body else ""))
    return "\n".join(lines)


def github_reply_to_review_comment(repo: str, pr_number: int, comment_id: int, body: str) -> str:
    """
    Reply to a specific inline review comment on a pull request.

    Args:
        repo:       Owner/repo
        pr_number:  PR number
        comment_id: ID of the review comment to reply to
        body:       Reply text (markdown)

    Returns:
        Confirmation string or error.
    """
    result = _post(
        f"/repos/{repo}/pulls/{pr_number}/comments/{comment_id}/replies",
        {"body": body},
    )
    if "error" in result:
        return f"[Error replying to comment #{comment_id}: {result['error']}]"
    url = result.get("html_url", "?")
    logger.info(f"GitHub: replied to review comment #{comment_id} on PR #{pr_number} → {url}")
    return f"Reply posted to comment #{comment_id}: {url}"


def github_get_pr_files(repo: str, pr_number: int, max_files: int = 20) -> list:
    """
    Return changed files for a PR with their patch/diff content.

    Each item: {"filename", "status", "additions", "deletions", "patch"}
    patch is the unified diff for that file (lines prefixed +/-).
    """
    result = _get(f"/repos/{repo}/pulls/{pr_number}/files?per_page={max_files}")
    if isinstance(result, dict) and "error" in result:
        logger.warning(f"github_get_pr_files: {result['error']}")
        return []
    if not isinstance(result, list):
        return []
    files = []
    for f in result:
        files.append({
            "filename":  f.get("filename", ""),
            "status":    f.get("status", ""),          # added|modified|removed
            "additions": f.get("additions", 0),
            "deletions": f.get("deletions", 0),
            "patch":     f.get("patch", ""),            # unified diff — may be absent for binary
        })
    return files


def github_create_pr_review(
    repo: str,
    pr_number: int,
    body: str,
    event: str = "COMMENT",            # APPROVE | REQUEST_CHANGES | COMMENT
    comments: list = None,             # [{"path": str, "line": int, "body": str}]
) -> str:
    """
    Submit an official GitHub Pull Request Review (shows in Reviews tab).

    Args:
        repo:       Owner/repo
        pr_number:  PR number
        body:       Overall review summary (markdown)
        event:      APPROVE | REQUEST_CHANGES | COMMENT
        comments:   Inline comments on specific lines (optional)

    Returns:
        Confirmation string or error.
    """
    payload: dict = {"body": body, "event": event}
    if comments:
        # GitHub requires commit_id for inline comments; fetch the latest PR head SHA
        pr_data = _get(f"/repos/{repo}/pulls/{pr_number}")
        head_sha = pr_data.get("head", {}).get("sha", "") if isinstance(pr_data, dict) else ""
        if head_sha:
            payload["commit_id"] = head_sha
            # Filter to only comments that have a valid line number
            valid = [c for c in comments if c.get("path") and c.get("line") and c.get("body")]
            if valid:
                payload["comments"] = [
                    {"path": c["path"], "line": c["line"], "body": c["body"],
                     "side": "RIGHT"}
                    for c in valid
                ]

    result = _post(f"/repos/{repo}/pulls/{pr_number}/reviews", payload)
    if "error" in result:
        return f"[Error creating PR review: {result['error']}]"
    url = result.get("html_url", "?")
    logger.info(f"GitHub PR#{pr_number} review posted → {url} ({event})")
    return f"PR review posted (event={event}): {url}"


def github_merge_pr(repo: str, pr_number: int, merge_method: str = "squash") -> str:
    """
    Merge a pull request.

    Args:
        repo:         Owner/repo
        pr_number:    PR number
        merge_method: "merge" | "squash" | "rebase" (default: "squash")

    Returns:
        Confirmation string or error.
    """
    import urllib.request as ureq

    url  = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}/merge"
    data = json.dumps({"merge_method": merge_method}).encode()
    req  = ureq.Request(
        url, data=data,
        headers={**_headers(), "Content-Type": "application/json"},
        method="PUT",
    )
    try:
        import contextlib
        with contextlib.closing(ureq.urlopen(req, timeout=15)) as _conn:
            _result = json.loads(_conn.read().decode())
        _sha = _result.get("sha", "?")
        logger.info("GitHub: merged PR #%s in %s (%s) sha=%s", pr_number, repo, merge_method, _sha)
        return f"PR #{pr_number} merged ({merge_method}) — commit sha: {_sha}"
    except Exception:  # noqa: BLE001
        logger.error("GitHub merge PR #%s failed", pr_number)
        return f"[Error merging PR #{pr_number}]"


# ============================================================
# SDLC PIPELINE SUPPORT — functions required by agents/
# ============================================================

def _get_file_tree(repo: str, branch: str) -> list:
    """
    Return a flat list of all file paths in the repo at the given branch.
    Uses the Git Trees API with recursive=1 for a single-request full tree.
    Mirrors gitlab_tools._get_file_tree() — same return shape (list of str).
    """
    result = _get(f"/repos/{repo}/git/trees/{branch}?recursive=1")
    if isinstance(result, dict) and "tree" in result:
        return [item["path"] for item in result["tree"] if item.get("type") == "blob"]
    logger.warning(f"GitHub _get_file_tree: unexpected response for {repo}@{branch}: {result}")
    return []


def github_branch_has_changes(repo: str, base: str, head: str) -> Optional[bool]:
    """
    Return True if *head* is ahead of *base* (i.e. has commits not in base),
    False if identical, None on error.
    Mirrors gitlab_tools.gitlab_branch_has_changes().
    """
    result = _get(f"/repos/{repo}/compare/{base}...{head}")
    if isinstance(result, dict) and "ahead_by" in result:
        return result["ahead_by"] > 0
    logger.warning(f"GitHub branch_has_changes: unexpected response for {repo} {base}...{head}: {result}")
    return None


def github_delete_branch(repo: str, branch: str) -> str:
    """
    Delete a branch from a GitHub repository.
    Mirrors gitlab_tools.gitlab_delete_branch().

    Returns:
        "Branch deleted: {branch}" on success, or "[Error ...]".
    """
    import urllib.request as ureq
    url = f"{GITHUB_API}/repos/{repo}/git/refs/heads/{branch}"
    req = ureq.Request(url, headers=_headers(), method="DELETE")
    try:
        with ureq.urlopen(req, timeout=10) as resp:
            # 204 No Content = success
            logger.info(f"GitHub branch deleted: {repo}/{branch}")
            return f"Branch deleted: {branch}"
    except ureq.HTTPError as e:
        if e.code == 422:
            return f"[Error deleting branch {branch}: branch does not exist or is protected]"
        logger.error(f"GitHub delete branch {repo}/{branch} failed: HTTP {e.code}")
        return f"[Error deleting branch {branch}: HTTP {e.code}]"
    except Exception as e:
        logger.error(f"GitHub delete branch {repo}/{branch} failed: {e}")
        return f"[Error deleting branch {branch}: {e}]"


def github_set_pr_draft(repo: str, pr_number: int, draft: bool) -> dict:
    """
    Set a PR's draft status.
    draft=False → mark as ready for review (REST PATCH, supported for all PATs).
    draft=True  → convert to draft (also via REST PATCH on GitHub).
    Mirrors gitlab_tools.gitlab_set_mr_draft().
    """
    result = _patch(f"/repos/{repo}/pulls/{pr_number}", {"draft": draft})
    if "error" in result:
        logger.warning(f"GitHub set_pr_draft {repo}#{pr_number} draft={draft} failed: {result['error']}")
    else:
        logger.info(f"GitHub PR #{pr_number} draft={draft} set in {repo}")
    return result


def github_post_governance_note(repo: str, pr_number: int, report_md: str) -> str:
    """
    Post a governance review report as a PR comment.
    Mirrors gitlab_tools.gitlab_post_governance_note() — maps to a plain PR comment
    since GitHub has no MR-note concept separate from PR comments.
    """
    return github_comment_on_pr(repo, pr_number, report_md)


def github_batch_commit(repo: str, branch: str, actions: list, message: str) -> str:
    """
    Commit multiple files atomically using the GitHub Git Data API.
    Equivalent to gitlab_tools.gitlab_batch_commit() — same call signature and
    return format.

    GitHub approach (single atomic commit):
      1. GET current HEAD SHA of the branch
      2. GET the base tree SHA from that commit
      3. POST a new tree with all file changes
      4. POST a new commit pointing at the new tree
      5. PATCH the branch ref to point at the new commit

    Args:
        repo:    Owner/repo (e.g. "ainxt/payment-service")
        branch:  Target branch (must already exist)
        actions: List of file action dicts:
                   {"action": "create"|"update"|"delete", "file_path": str, "content": str}
        message: Commit message

    Returns:
        "Batch commit OK: N file(s) on {branch} — {sha}" on success,
        or "[Error batch commit: ...]" on failure.
    """
    if not actions:
        return "[Error batch commit: no file actions provided]"

    # ── 1. Get current HEAD SHA ───────────────────────────────────────────────
    ref_result = _get(f"/repos/{repo}/git/ref/heads/{branch}")
    if "error" in ref_result:
        return f"[Error batch commit: could not get HEAD of {branch}: {ref_result['error']}]"
    head_sha = (ref_result.get("object") or {}).get("sha", "")
    if not head_sha:
        return f"[Error batch commit: no SHA for branch {branch}]"

    # ── 2. Get base tree SHA ──────────────────────────────────────────────────
    commit_result = _get(f"/repos/{repo}/git/commits/{head_sha}")
    if "error" in commit_result:
        return f"[Error batch commit: could not get commit {head_sha}: {commit_result['error']}]"
    base_tree_sha = (commit_result.get("tree") or {}).get("sha", "")
    if not base_tree_sha:
        return f"[Error batch commit: no tree SHA for commit {head_sha}]"

    # ── 3. Build tree entries ─────────────────────────────────────────────────
    tree_entries = []
    n_files = 0
    for a in actions:
        fp  = (a.get("file_path") or a.get("path") or "").strip()
        act = (a.get("action") or "update").lower()
        if not fp:
            continue
        if act == "delete":
            # GitHub deletes: set sha=null in the tree
            tree_entries.append({"path": fp, "mode": "100644", "type": "blob", "sha": None})
        else:
            content = a.get("content", "")
            tree_entries.append({"path": fp, "mode": "100644", "type": "blob", "content": content})
        n_files += 1

    if not tree_entries:
        return "[Error batch commit: no valid file actions after filtering]"

    tree_result = _post(f"/repos/{repo}/git/trees", {
        "base_tree": base_tree_sha,
        "tree": tree_entries,
    })
    if "error" in tree_result:
        return f"[Error batch commit: tree creation failed: {tree_result['error']}]"
    new_tree_sha = tree_result.get("sha", "")
    if not new_tree_sha:
        return "[Error batch commit: tree creation returned no SHA]"

    # ── 4. Create commit ──────────────────────────────────────────────────────
    commit_payload = {
        "message": message,
        "tree": new_tree_sha,
        "parents": [head_sha],
    }
    new_commit = _post(f"/repos/{repo}/git/commits", commit_payload)
    if "error" in new_commit:
        return f"[Error batch commit: commit creation failed: {new_commit['error']}]"
    new_commit_sha = new_commit.get("sha", "")
    if not new_commit_sha:
        return "[Error batch commit: commit creation returned no SHA]"

    # ── 5. Update branch ref ──────────────────────────────────────────────────
    ref_update = _patch(f"/repos/{repo}/git/refs/heads/{branch}", {"sha": new_commit_sha})
    if "error" in ref_update:
        return f"[Error batch commit: ref update failed: {ref_update['error']}]"

    web_url = f"https://github.com/{repo}/commit/{new_commit_sha}"
    logger.info(f"GitHub batch commit OK: {n_files} file(s) on {branch} — {new_commit_sha}")
    return f"Batch commit OK: {n_files} file(s) on {branch} — {new_commit_sha} ({web_url})"


def github_get_pr_diff_notes(repo: str, pr_number: int) -> list:
    """
    Return inline review comments on a PR as a list of dicts.
    Mirrors gitlab_tools.gitlab_get_mr_diff_notes() — same return shape:
      [{"body": str, "path": str, "line": int|None}, ...]
    """
    result = _get(f"/repos/{repo}/pulls/{pr_number}/comments")
    if not isinstance(result, list):
        logger.warning(f"GitHub get_pr_diff_notes: unexpected response for {repo}#{pr_number}")
        return []
    notes = []
    for c in result:
        notes.append({
            "body":     c.get("body", ""),
            "path":     c.get("path", ""),
            "line":     c.get("line") or c.get("original_line"),
            "id":       c.get("id"),
            "author":   (c.get("user") or {}).get("login", ""),
        })
    return notes
