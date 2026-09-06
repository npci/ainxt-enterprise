# SPDX-License-Identifier: MIT
"""
GitHub tools — ABStudio equivalent of gitlab_tools.py.

Provides read/write access to GitHub repositories via the REST API.
Credentials come from env vars injected by the connections system
(GITHUB_TOKEN). Each tool's `code` string is self-contained and runs
in the sandbox subprocess.

Activated when SCM_PROVIDER=github (OSS default).
A GitLab-based deployment sets SCM_PROVIDER=gitlab → this file is never loaded.
"""

# ---------------------------------------------------------------------------
# Shared helper block — included verbatim at the top of every tool's code
# ---------------------------------------------------------------------------

_GITHUB_HELPERS = '''
import os, json, base64, threading, urllib.request, urllib.error
from urllib.parse import quote as _url_quote

_GITHUB_API   = "https://api.github.com"
_HTTPS_PROXY  = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or ""
_thread_local = threading.local()

def _resolve_token() -> str:
    return getattr(_thread_local, "token", None) or os.getenv("GITHUB_TOKEN", "")

_GITHUB_NOT_CONFIGURED = (
    "You have not configured a GitHub personal access token. "
    "Add it under Profile \\u2192 GitHub Token, then retry. "
    "(The platform does not use a shared/service GitHub account.)"
)

def _headers() -> dict:
    token = _resolve_token()
    if not token:
        raise PermissionError(_GITHUB_NOT_CONFIGURED)
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    }

def _github_http_error(code, reason, body=""):
    if code == 401:
        msg = ("GitHub token is invalid or expired. "
               "Update it under Profile \\u2192 GitHub Token.")
    elif code == 403:
        msg = ("Your GitHub token does not have access to this repository or path. "
               "Check token scopes (needs repo, read:user).")
    elif code == 404:
        msg = ("Repository or path not found, or your GitHub token has no access to it.")
    else:
        msg = f"HTTP {code}: {reason}"
    return {"error": msg, "status": code, "body": body}

def _make_opener():
    if _HTTPS_PROXY:
        handler = urllib.request.ProxyHandler({"https": _HTTPS_PROXY, "http": _HTTPS_PROXY})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener()

def _get(path: str):
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITHUB_API}{path}"
    req    = urllib.request.Request(url, headers=headers)
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _github_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _post(path: str, payload: dict) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITHUB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _github_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _patch(path: str, payload: dict) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITHUB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _github_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _put(path: str, payload: dict) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITHUB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _github_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _delete(path: str) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITHUB_API}{path}"
    req    = urllib.request.Request(url, headers=headers, method="DELETE")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=60) as resp:
            body = resp.read().decode() or ""
            try:    return json.loads(body) if body else {"status": resp.status}
            except: return {"status": resp.status}
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _github_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}
'''

# Branch helpers needed by create_pr, create_branch
_BRANCH_HELPERS = '''
_DEFAULT_BRANCH_CACHE = {}

def _detect_default_branch(repo: str) -> str:
    if repo in _DEFAULT_BRANCH_CACHE:
        return _DEFAULT_BRANCH_CACHE[repo]
    result = _get(f"/repos/{repo}")
    if isinstance(result, dict) and "default_branch" in result:
        branch = result["default_branch"] or "main"
        _DEFAULT_BRANCH_CACHE[repo] = branch
        return branch
    return "main"

def _find_existing_pr(repo: str, head: str):
    result = _get(f"/repos/{repo}/pulls?state=open&head={_url_quote(head, safe=\'\')}:&per_page=10")
    if isinstance(result, list):
        for pr in result:
            if (pr.get("head") or {}).get("ref") == head:
                return pr
    return None
'''

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GITHUB_TOOLS = [
    {
        "name": "github_read_file",
        "description": "Read and return the full content of a file from a GitHub repository. Content is base64-decoded automatically — you receive plain text. branch defaults to 'main'. Use this to inspect existing code before editing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Owner/repo e.g. ainxt/payment-service"},
                "path":   {"type": "string", "description": "File path e.g. src/main/PaymentService.java"},
                "branch": {"type": "string", "description": "Branch name", "default": "main"},
            },
            "required": ["repo", "path"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        path   = inputs.get("path", "")
        branch = inputs.get("branch", "main")
        result = _get(f"/repos/{repo}/contents/{_url_quote(path, safe=\'\')}?ref={branch}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if isinstance(result, dict) and result.get("encoding") == "base64":
            content = base64.b64decode(result["content"]).decode("utf-8")
            return {"result": content}
        return {"result": result.get("content", "") if isinstance(result, dict) else ""}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_search_code",
        "description": "Search for a code pattern or symbol within a GitHub repository. Returns matching filename, line number, and a snippet. Results are capped at 20.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Owner/repo"},
                "query":       {"type": "string",  "description": "Code pattern or symbol to search"},
                "max_results": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["repo", "query"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        query       = inputs.get("query", "")
        max_results = int(inputs.get("max_results", 10))
        encoded     = _url_quote(f"{query} repo:{repo}", safe="")
        results     = _get(f"/search/code?q={encoded}&per_page={min(max_results, 20)}")
        if isinstance(results, dict) and "error" in results:
            return {"error": results["error"]}
        items = results.get("items", []) if isinstance(results, dict) else []
        if not items:
            return {"result": f"No results found for \'{query}\' in {repo}.", "matches": []}
        lines   = [f"GitHub code search: \'{query}\' in {repo} — {len(items)} result(s)\\n"]
        matches = []
        for r in items[:max_results]:
            fname = r.get("path", "?")
            url   = r.get("html_url", "")
            lines.append(f"• {fname} — {url}")
            matches.append({"filename": fname, "url": url})
        return {"result": "\\n".join(lines), "matches": matches}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_list_issues",
        "description": "List issues in a GitHub repository filtered by state. state values: 'open' (default), 'closed', 'all'. Returns each issue's number, title, and state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Owner/repo"},
                "state": {"type": "string",  "description": "open | closed | all", "default": "open"},
                "limit": {"type": "integer", "description": "Max issues to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        state = inputs.get("state", "open")
        limit = int(inputs.get("limit", 20))
        result = _get(f"/repos/{repo}/issues?state={state}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        issues = [{"number": i.get("number"), "title": i.get("title"), "state": i.get("state")} for i in result]
        return {"result": f"{len(issues)} issue(s) in {repo}", "issues": issues}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_create_issue",
        "description": "Create a new issue in a GitHub repository. Returns the issue URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Owner/repo"},
                "title":  {"type": "string", "description": "Issue title"},
                "body":   {"type": "string", "description": "Issue description (Markdown)"},
                "labels": {"type": "array",  "items": {"type": "string"}, "description": "Labels"},
            },
            "required": ["repo", "title"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        title  = inputs.get("title", "")
        body   = inputs.get("body", "")
        labels = inputs.get("labels") or []
        payload = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        result = _post(f"/repos/{repo}/issues", payload)
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        url = result.get("html_url", "?")
        return {"result": f"Issue created: {url}", "url": url, "number": result.get("number")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_list_prs",
        "description": "List pull requests in a GitHub repository. state: 'open' (default), 'closed', 'all'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Owner/repo"},
                "state": {"type": "string",  "description": "open | closed | all", "default": "open"},
                "limit": {"type": "integer", "description": "Max PRs to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        state = inputs.get("state", "open")
        limit = int(inputs.get("limit", 20))
        result = _get(f"/repos/{repo}/pulls?state={state}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        prs = [{"number": p.get("number"), "title": p.get("title"), "state": p.get("state"),
                "head": (p.get("head") or {}).get("ref"), "base": (p.get("base") or {}).get("ref")}
               for p in result]
        return {"result": f"{len(prs)} PR(s) in {repo}", "prs": prs}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_create_pr",
        "description": "Create a pull request in a GitHub repository. Returns the PR URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "Owner/repo"},
                "title": {"type": "string", "description": "PR title"},
                "body":  {"type": "string", "description": "PR description (Markdown)"},
                "head":  {"type": "string", "description": "Source branch"},
                "base":  {"type": "string", "description": "Target branch (default: main)"},
                "draft": {"type": "boolean", "description": "Create as draft PR", "default": False},
            },
            "required": ["repo", "title", "head"],
        },
        "code": _GITHUB_HELPERS + _BRANCH_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        title = inputs.get("title", "")
        body  = inputs.get("body", "")
        head  = inputs.get("head", "")
        base  = inputs.get("base") or _detect_default_branch(repo)
        draft = bool(inputs.get("draft", False))
        existing = _find_existing_pr(repo, head)
        if existing:
            url = existing.get("html_url", "?")
            return {"result": f"PR already exists: {url}", "url": url, "number": existing.get("number")}
        result = _post(f"/repos/{repo}/pulls", {"title": title, "body": body, "head": head, "base": base, "draft": draft})
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        url = result.get("html_url", "?")
        return {"result": f"PR created: {url}", "url": url, "number": result.get("number")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_merge_pr",
        "description": "Merge a pull request. merge_method: 'merge' | 'squash' | 'rebase' (default: squash).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":         {"type": "string",  "description": "Owner/repo"},
                "pr_number":    {"type": "integer", "description": "PR number"},
                "merge_method": {"type": "string",  "description": "merge | squash | rebase", "default": "squash"},
            },
            "required": ["repo", "pr_number"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo         = inputs.get("repo", "")
        pr_number    = int(inputs.get("pr_number", 0))
        merge_method = inputs.get("merge_method", "squash")
        result = _put(f"/repos/{repo}/pulls/{pr_number}/merge", {"merge_method": merge_method})
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        sha = result.get("sha", "?")
        return {"result": f"PR #{pr_number} merged ({merge_method}) — commit sha: {sha}", "sha": sha}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_comment_on_pr",
        "description": "Post a comment on a GitHub pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Owner/repo"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "body":      {"type": "string",  "description": "Comment text (Markdown)"},
            },
            "required": ["repo", "pr_number", "body"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        pr_number = int(inputs.get("pr_number", 0))
        body      = inputs.get("body", "")
        # PR comments go to the issues endpoint (GitHub treats PRs as issues for comments)
        result = _post(f"/repos/{repo}/issues/{pr_number}/comments", {"body": body})
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        url = result.get("html_url", "?")
        return {"result": f"Comment posted: {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_get_pr",
        "description": "Get details of a GitHub pull request including title, body, state, head/base branches, and URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Owner/repo"},
                "pr_number": {"type": "integer", "description": "PR number"},
            },
            "required": ["repo", "pr_number"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        pr_number = int(inputs.get("pr_number", 0))
        result    = _get(f"/repos/{repo}/pulls/{pr_number}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        return {
            "result": f"PR #{pr_number}: {result.get(\'title\', \'?\')}",
            "number": result.get("number"),
            "title":  result.get("title"),
            "body":   result.get("body"),
            "state":  result.get("state"),
            "head":   (result.get("head") or {}).get("ref"),
            "base":   (result.get("base") or {}).get("ref"),
            "url":    result.get("html_url"),
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_get_pr_files",
        "description": "Get the list of files changed in a GitHub pull request, with patch diffs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Owner/repo"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "max_files": {"type": "integer", "description": "Max files to return", "default": 20},
            },
            "required": ["repo", "pr_number"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        pr_number = int(inputs.get("pr_number", 0))
        max_files = int(inputs.get("max_files", 20))
        result    = _get(f"/repos/{repo}/pulls/{pr_number}/files?per_page={max_files}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        files = [{"filename": f.get("filename"), "status": f.get("status"),
                  "additions": f.get("additions"), "deletions": f.get("deletions"),
                  "patch": f.get("patch", "")} for f in result]
        return {"result": f"{len(files)} file(s) changed in PR #{pr_number}", "files": files}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_create_pr_review",
        "description": "Submit a review on a GitHub pull request. event: 'APPROVE', 'REQUEST_CHANGES', or 'COMMENT'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Owner/repo"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "body":      {"type": "string",  "description": "Review summary"},
                "event":     {"type": "string",  "description": "APPROVE | REQUEST_CHANGES | COMMENT"},
            },
            "required": ["repo", "pr_number", "body", "event"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        pr_number = int(inputs.get("pr_number", 0))
        body      = inputs.get("body", "")
        event     = inputs.get("event", "COMMENT").upper()
        if event not in ("APPROVE", "REQUEST_CHANGES", "COMMENT"):
            event = "COMMENT"
        result = _post(f"/repos/{repo}/pulls/{pr_number}/reviews", {"body": body, "event": event})
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        url = result.get("html_url", "?")
        return {"result": f"PR review posted (event={event}): {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_create_branch",
        "description": "Create a new branch in a GitHub repository from a source branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Owner/repo"},
                "branch":      {"type": "string", "description": "New branch name"},
                "from_branch": {"type": "string", "description": "Source branch (default: main)"},
            },
            "required": ["repo", "branch"],
        },
        "code": _GITHUB_HELPERS + _BRANCH_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        branch      = inputs.get("branch", "")
        from_branch = inputs.get("from_branch") or _detect_default_branch(repo)
        # Check if branch already exists
        existing = _get(f"/repos/{repo}/git/ref/heads/{_url_quote(branch, safe=\'\')}")
        if "error" not in existing and existing.get("object", {}).get("sha"):
            return {"result": f"Branch exists (reusing): {branch}", "branch": branch}
        # Get SHA of source branch
        source = _get(f"/repos/{repo}/git/ref/heads/{_url_quote(from_branch, safe=\'\')}")
        if "error" in source:
            return {"error": f"Source branch not found: {from_branch}"}
        sha = (source.get("object") or {}).get("sha", "")
        if not sha:
            return {"error": f"Could not get SHA for {from_branch}"}
        result = _post(f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha})
        if isinstance(result, dict) and "error" in result:
            err = str(result.get("error", ""))
            if "422" in err or "already exists" in err.lower():
                return {"result": f"Branch exists (reusing): {branch}", "branch": branch}
            return {"error": result["error"]}
        return {"result": f"Branch created: {branch} from {from_branch}", "branch": branch}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_delete_branch",
        "description": "Delete a branch from a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Owner/repo"},
                "branch": {"type": "string", "description": "Branch name to delete"},
            },
            "required": ["repo", "branch"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        branch = inputs.get("branch", "")
        result = _delete(f"/repos/{repo}/git/refs/heads/{_url_quote(branch, safe=\'\')}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        return {"result": f"Branch deleted: {branch}"}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_create_or_update_file",
        "description": "Create or update a single file in a GitHub repository. Automatically detects whether to create or update.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Owner/repo"},
                "path":    {"type": "string", "description": "File path in the repo"},
                "content": {"type": "string", "description": "New file content (plain text)"},
                "message": {"type": "string", "description": "Commit message"},
                "branch":  {"type": "string", "description": "Branch name", "default": "main"},
            },
            "required": ["repo", "path", "content", "message"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo    = inputs.get("repo", "")
        path    = inputs.get("path", "")
        content = inputs.get("content", "")
        message = inputs.get("message", "Update file")
        branch  = inputs.get("branch", "main")
        encoded_path = _url_quote(path, safe="")
        # Check if file exists to get its SHA (required for updates)
        existing = _get(f"/repos/{repo}/contents/{encoded_path}?ref={branch}")
        sha = existing.get("sha") if isinstance(existing, dict) and "sha" in existing else None
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch":  branch,
        }
        if sha:
            payload["sha"] = sha
        result = _put(f"/repos/{repo}/contents/{encoded_path}", payload)
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        action = "updated" if sha else "created"
        url = (result.get("content") or {}).get("html_url", "?")
        return {"result": f"File {action}: {path} — {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_apply_patch",
        "description": "Apply a targeted patch (search-and-replace) to a file in a GitHub repository. Reads the current file, applies the patch, and writes it back.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Owner/repo"},
                "path":        {"type": "string", "description": "File path"},
                "search":      {"type": "string", "description": "Exact text to find"},
                "replacement": {"type": "string", "description": "Text to replace it with"},
                "branch":      {"type": "string", "description": "Branch name", "default": "main"},
                "message":     {"type": "string", "description": "Commit message"},
            },
            "required": ["repo", "path", "search", "replacement"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        path        = inputs.get("path", "")
        search      = inputs.get("search", "")
        replacement = inputs.get("replacement", "")
        branch      = inputs.get("branch", "main")
        message     = inputs.get("message") or f"Apply patch to {path}"
        encoded_path = _url_quote(path, safe="")
        existing = _get(f"/repos/{repo}/contents/{encoded_path}?ref={branch}")
        if isinstance(existing, dict) and "error" in existing:
            return {"error": existing["error"]}
        sha     = existing.get("sha", "")
        content = base64.b64decode(existing.get("content", "")).decode("utf-8")
        if search not in content:
            return {"error": f"Search string not found in {path}"}
        new_content = content.replace(search, replacement, 1)
        payload = {
            "message": message,
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "branch":  branch,
            "sha":     sha,
        }
        result = _put(f"/repos/{repo}/contents/{encoded_path}", payload)
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        url = (result.get("content") or {}).get("html_url", "?")
        return {"result": f"Patch applied to {path} — {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_list_branches",
        "description": "List branches in a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Owner/repo"},
                "limit": {"type": "integer", "description": "Max branches to return", "default": 30},
            },
            "required": ["repo"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        limit = int(inputs.get("limit", 30))
        result = _get(f"/repos/{repo}/branches?per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        branches = [b.get("name") for b in result]
        return {"result": f"{len(branches)} branch(es) in {repo}", "branches": branches}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_get_commit",
        "description": "Get details of a specific commit in a GitHub repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Owner/repo"},
                "sha":  {"type": "string", "description": "Commit SHA"},
            },
            "required": ["repo", "sha"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        sha    = inputs.get("sha", "")
        result = _get(f"/repos/{repo}/commits/{sha}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        commit  = result.get("commit") or {}
        message = commit.get("message", "")
        author  = (commit.get("author") or {}).get("name", "?")
        date    = (commit.get("author") or {}).get("date", "?")
        files   = [f.get("filename") for f in (result.get("files") or [])]
        return {
            "result":  f"Commit {sha[:8]}: {message[:80]}",
            "sha":     sha,
            "message": message,
            "author":  author,
            "date":    date,
            "files":   files,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_get_pr_diff",
        "description": "Get the unified diff of all files changed in a GitHub pull request.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Owner/repo"},
                "pr_number": {"type": "integer", "description": "PR number"},
                "max_files": {"type": "integer", "description": "Max files to include", "default": 20},
            },
            "required": ["repo", "pr_number"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        pr_number = int(inputs.get("pr_number", 0))
        max_files = int(inputs.get("max_files", 20))
        result    = _get(f"/repos/{repo}/pulls/{pr_number}/files?per_page={max_files}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        parts = []
        for f in result:
            header = f"### {f[\'filename\']}  [{f[\'status\']}]  +{f[\'additions\']} -{f[\'deletions\']}\\n"
            patch  = f.get("patch", "") or "(binary or no diff)"
            if len(patch) > 3000:
                patch = patch[:3000] + "\\n... (truncated)"
            parts.append(header + "```diff\\n" + patch + "\\n```")
        return {"result": f"PR #{pr_number} — {len(result)} file(s) changed:\\n\\n" + "\\n\\n".join(parts)}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_get_workflow_runs",
        "description": "Get recent GitHub Actions workflow runs for a repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Owner/repo"},
                "limit": {"type": "integer", "description": "Max runs to return", "default": 10},
            },
            "required": ["repo"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        limit = int(inputs.get("limit", 10))
        result = _get(f"/repos/{repo}/actions/runs?per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        runs = result.get("workflow_runs", []) if isinstance(result, dict) else []
        summary = [{"id": r.get("id"), "name": r.get("name"), "status": r.get("status"),
                    "conclusion": r.get("conclusion"), "branch": r.get("head_branch"),
                    "url": r.get("html_url")} for r in runs]
        return {"result": f"{len(summary)} workflow run(s) in {repo}", "runs": summary}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "github_list_files",
        "description": "List all files in a GitHub repository at a given branch (recursive). Returns a flat list of file paths.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Owner/repo"},
                "branch": {"type": "string", "description": "Branch name", "default": "main"},
            },
            "required": ["repo"],
        },
        "code": _GITHUB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        branch = inputs.get("branch", "main")
        result = _get(f"/repos/{repo}/git/trees/{branch}?recursive=1")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        files = [item["path"] for item in result.get("tree", []) if item.get("type") == "blob"]
        return {"result": f"{len(files)} file(s) in {repo}@{branch}", "files": files}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
