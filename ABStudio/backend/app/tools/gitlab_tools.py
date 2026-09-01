# SPDX-License-Identifier: Apache-2.0
"""
GitLab tools — adapted from AiNxt Agentic Platform gitlab_tools.py.

core.* imports stripped; credentials come from env vars injected by the
connections system (GITLAB_URL, GITLAB_TOKEN).
Each tool's `code` string is self-contained and runs in the sandbox subprocess.
"""

# ---------------------------------------------------------------------------
# Shared helper block — included verbatim at the top of every tool's code
# ---------------------------------------------------------------------------

_GITLAB_HELPERS = '''
import os, json, base64, threading, urllib.request, urllib.error
from urllib.parse import quote as _url_quote

_GITLAB_URL  = os.getenv("GITLAB_URL", "https://<YOUR_GITLAB_URL>").rstrip("/")
_HTTPS_PROXY = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or ""
_NO_PROXY    = os.getenv("NO_PROXY") or os.getenv("no_proxy") or ""
_GITLAB_API  = f"{_GITLAB_URL}/api/v4"
_thread_local = threading.local()

def _resolve_token() -> str:
    return getattr(_thread_local, "token", None) or os.getenv("GITLAB_TOKEN", "")

_GITLAB_NOT_CONFIGURED = (
    "You have not configured a GitLab personal access token. "
    "Add it under Profile \u2192 GitLab Token, then retry. "
    "(The platform does not use a shared/service GitLab account.)"
)

def _proj(repo: str) -> str:
    return _url_quote(repo, safe="")

def _headers() -> dict:
    # Per-user token ONLY — no platform/service-account fallback. If the user has
    # not configured a GitLab token, fail with a clear, actionable error rather
    # than issuing an unauthenticated request.
    token = _resolve_token()
    if not token:
        raise PermissionError(_GITLAB_NOT_CONFIGURED)
    return {"Content-Type": "application/json", "PRIVATE-TOKEN": token}

def _gitlab_http_error(code, reason, body=""):
    """Map a GitLab HTTP status to a clear, actionable error message."""
    if code == 401:
        msg = ("GitLab token is invalid or expired. "
               "Update it under Profile \u2192 GitLab Token.")
    elif code == 403:
        msg = ("Your GitLab token does not have access to this repository or path. "
               "Ask a project owner for access, or use a token with the required scope.")
    elif code == 404:
        msg = ("Repository or path not found, or your GitLab token has no access to it.")
    else:
        msg = f"HTTP {code}: {reason}"
    return {"error": msg, "status": code, "body": body}

def _clean(text: str) -> str:
    return str(text) if text is not None else ""

def _bypass_proxy_for(host: str) -> bool:
    """True when ``host`` matches a NO_PROXY entry and must be reached directly.

    Follows the widely-used convention: comma-separated hosts, case-insensitive,
    where an entry matches the host itself or any subdomain (a leading dot is
    optional), and ``*`` bypasses the proxy for every host. Needed because the
    on-prem GitLab (``git.npci.org.in`` → internal 10.x) is directly reachable
    and MUST NOT be tunnelled through the internet egress proxy, which rejects a
    CONNECT to an internal host with ``400 Bad Request``.
    """
    if not _NO_PROXY or not host:
        return False
    host = host.lower()
    for raw in _NO_PROXY.split(","):
        entry = raw.strip().lower().lstrip(".")
        if not entry:
            continue
        if entry == "*" or host == entry or host.endswith("." + entry):
            return True
    return False


def _make_opener():
    from urllib.parse import urlsplit
    _host = urlsplit(_GITLAB_URL).hostname or ""
    if _HTTPS_PROXY and not _bypass_proxy_for(_host):
        handler = urllib.request.ProxyHandler({"https": _HTTPS_PROXY, "http": _HTTPS_PROXY})
        return urllib.request.build_opener(handler)
    # Force a direct connection (empty ProxyHandler) so a process-wide
    # HTTPS_PROXY set for internet egress cannot leak in via urllib's
    # environment auto-detection for a NO_PROXY / internal host.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _get(path: str):
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITLAB_API}{path}"
    req    = urllib.request.Request(url, headers=headers)
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _gitlab_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _post(path: str, payload: dict) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITLAB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _gitlab_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _put(path: str, payload: dict) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITLAB_API}{path}"
    data   = json.dumps(payload).encode()
    req    = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    opener = _make_opener()
    try:
        with opener.open(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:    body = e.read().decode()
        except: body = ""
        return _gitlab_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}

def _delete(path: str) -> dict:
    try:    headers = _headers()
    except PermissionError as e: return {"error": str(e), "status": 401}
    url    = f"{_GITLAB_API}{path}"
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
        return _gitlab_http_error(e.code, e.reason, body)
    except Exception as e:
        return {"error": str(e)}
'''

# Extra helpers needed only by branch-aware tools (create_mr, create_branch)
_BRANCH_HELPERS = '''
_DEFAULT_BRANCH_CACHE = {}

def _detect_default_branch(repo: str) -> str:
    if repo in _DEFAULT_BRANCH_CACHE:
        return _DEFAULT_BRANCH_CACHE[repo]
    result = _get(f"/projects/{_proj(repo)}")
    if isinstance(result, dict) and "default_branch" in result:
        branch = result["default_branch"] or "main"
        _DEFAULT_BRANCH_CACHE[repo] = branch
        return branch
    return "main"

def _find_existing_mr(repo: str, source_branch: str):
    result = _get(
        f"/projects/{_proj(repo)}/merge_requests"
        f"?state=opened&source_branch={_url_quote(source_branch, safe=\'\')}&per_page=10"
    )
    if isinstance(result, list):
        for mr in result:
            if mr.get("source_branch") == source_branch:
                return mr
    return None
'''

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

GITLAB_TOOLS = [
    {
        "name": "gitlab_read_file",
        "description": "Read and return the full content of a file from a GitLab repository. Content is base64-decoded automatically — you receive plain text. branch defaults to 'main'. Use this to inspect existing code before editing. To make targeted edits to an existing file, prefer gitlab_apply_patch (partial replace) or gitlab_create_or_update_file (full rewrite).",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Namespace/project e.g. ainxt/payment-service"},
                "path":   {"type": "string", "description": "File path e.g. src/main/PaymentService.java"},
                "branch": {"type": "string", "description": "Branch name", "default": "main"},
            },
            "required": ["repo", "path"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo         = inputs.get("repo", "")
        path         = inputs.get("path", "")
        branch       = inputs.get("branch", "main")
        encoded_path = _url_quote(path, safe="")
        result       = _get(f"/projects/{_proj(repo)}/repository/files/{encoded_path}?ref={branch}")
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
        "name": "gitlab_search_code",
        "description": "Search for a code pattern or symbol within the blob (file content) of a GitLab repository. Returns matching filename, start line number, and a snippet of the matching code (up to 300 chars). Results are capped at 20 per page. Use this to locate where a function, class, or string is defined or used across the codebase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "query":       {"type": "string",  "description": "Code pattern or symbol to search"},
                "max_results": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["repo", "query"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        query       = inputs.get("query", "")
        max_results = int(inputs.get("max_results", 10))
        encoded     = _url_quote(query, safe="")
        results     = _get(
            f"/projects/{_proj(repo)}/search"
            f"?scope=blobs&search={encoded}&per_page={min(max_results, 20)}"
        )
        if isinstance(results, dict) and "error" in results:
            return {"error": results["error"]}
        if not isinstance(results, list) or not results:
            return {"result": f"No results found for \'{query}\' in {repo}.", "matches": []}
        lines   = [f"GitLab code search: \'{query}\' in {repo} — {len(results)} result(s)\\n"]
        matches = []
        for r in results[:max_results]:
            fname     = r.get("filename", "?")
            ref       = r.get("ref", "")
            data      = r.get("data", "").strip()
            startline = r.get("startline", "")
            lines.append(f"• {fname}:{startline} (branch: {ref})")
            if data:
                lines.append(f"  {data[:300].replace(chr(10), \' ↩ \')}")
            matches.append({"filename": fname, "ref": ref, "startline": startline})
        return {"result": "\\n".join(lines), "matches": matches}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_list_issues",
        "description": "List issues in a GitLab project filtered by state. state values: 'open' (default), 'closed', 'all'. Returns each issue's iid, title, and state. The iid (internal ID) is required for gitlab_get_issue, gitlab_update_issue, gitlab_close_issue, and gitlab_add_issue_note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Namespace/project"},
                "state": {"type": "string",  "description": "open | closed | all", "default": "open"},
                "limit": {"type": "integer", "description": "Max issues to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo     = inputs.get("repo", "")
        state    = inputs.get("state", "open")
        limit    = int(inputs.get("limit", 20))
        gl_state = "opened" if state == "open" else state
        result   = _get(f"/projects/{_proj(repo)}/issues?state={gl_state}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        if not result:
            return {"result": f"No {state} issues found in {repo}.", "issues": []}
        lines  = [f"Issues in {repo} ({state}):"]
        issues = []
        for issue in result:
            lines.append(f"  #{issue[\'iid\']} [{issue[\'state\']}] {issue[\'title\']} — {(issue.get(\'author\') or {}).get(\'username\', \'?\')}")
            issues.append({"iid": issue["iid"], "title": issue["title"], "state": issue["state"]})
        return {"result": "\\n".join(lines), "issues": issues}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_create_issue",
        "description": "Create a new issue in a GitLab project. Returns the issue iid and web_url. body supports markdown and is optional. labels is an optional array of label name strings (e.g. ['bug', 'priority::high']). Use the returned iid with gitlab_update_issue, gitlab_close_issue, or gitlab_add_issue_note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Namespace/project"},
                "title":  {"type": "string", "description": "Issue title"},
                "body":   {"type": "string", "description": "Issue description (markdown)"},
                "labels": {"type": "array",  "description": "Label names", "items": {"type": "string"}},
            },
            "required": ["repo", "title"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo    = inputs.get("repo", "")
        title   = _clean(inputs.get("title", ""))
        body    = _clean(inputs.get("body", ""))
        labels  = inputs.get("labels", [])
        payload = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        result = _post(f"/projects/{_proj(repo)}/issues", payload)
        if "error" in result:
            return {"error": result["error"]}
        return {
            "result": f"Issue created: {result.get(\'web_url\', \'?\' )} (#{result.get(\'iid\', \'?\')})",
            "url": result.get("web_url"),
            "iid": result.get("iid"),
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_list_mrs",
        "description": "List merge requests in a GitLab project filtered by state. state values: 'open' (default), 'closed', 'merged', 'all'. Returns each MR's iid, title, state, and source_branch. The iid (internal ID) is required for all other MR tools: gitlab_get_mr, gitlab_create_mr_review, gitlab_merge_mr, gitlab_get_mr_files, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Namespace/project"},
                "state": {"type": "string",  "description": "open | closed | merged | all", "default": "open"},
                "limit": {"type": "integer", "description": "Max MRs to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo     = inputs.get("repo", "")
        state    = inputs.get("state", "open")
        limit    = int(inputs.get("limit", 20))
        gl_state = "opened" if state == "open" else state
        result   = _get(f"/projects/{_proj(repo)}/merge_requests?state={gl_state}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        if not result:
            return {"result": f"No {state} MRs found in {repo}.", "mrs": []}
        lines = [f"Merge Requests in {repo} ({state}):"]
        mrs   = []
        for mr in result:
            lines.append(f"  !{mr[\'iid\']} [{mr[\'state\']}] {mr[\'title\']} ← {mr.get(\'source_branch\',\'?\')} — {(mr.get(\'author\') or {}).get(\'username\',\'?\')}")
            mrs.append({"iid": mr["iid"], "title": mr["title"], "state": mr["state"], "source_branch": mr.get("source_branch")})
        return {"result": "\\n".join(lines), "mrs": mrs}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_create_mr",
        "description": "Create a merge request in a GitLab project. Idempotent — if an open MR already exists for the source branch, returns the existing MR instead of creating a duplicate. base (target branch) auto-detects the repository's default branch when set to 'main'. Returns the MR iid and web_url, which are needed for gitlab_create_mr_review, gitlab_merge_mr, gitlab_get_mr_files, and other MR tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string", "description": "Namespace/project"},
                "title": {"type": "string", "description": "MR title"},
                "body":  {"type": "string", "description": "MR description (markdown)"},
                "head":  {"type": "string", "description": "Source branch"},
                "base":  {"type": "string", "description": "Target branch", "default": "main"},
            },
            "required": ["repo", "title", "head"],
        },
        "code": _GITLAB_HELPERS + _BRANCH_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        title = _clean(inputs.get("title", ""))
        body  = _clean(inputs.get("body", ""))
        head  = inputs.get("head", "")
        base  = inputs.get("base", "main")
        if base == "main":
            base = _detect_default_branch(repo)
        payload = {
            "title": title, "description": body,
            "source_branch": head, "target_branch": base,
            "remove_source_branch": False,
        }
        result = _post(f"/projects/{_proj(repo)}/merge_requests", payload)
        if "error" in result:
            body_text = result.get("body", "")
            err_str   = str(result.get("error", ""))
            if "409" in err_str or "already exists" in body_text.lower():
                existing = _find_existing_mr(repo, head)
                if existing:
                    return {"result": f"MR already exists: {existing[\'web_url\']} (!{existing[\'iid\']})", "url": existing["web_url"], "iid": existing["iid"]}
            return {"error": result["error"]}
        return {
            "result": f"MR created: {result.get(\'web_url\',\'?\')} (!{result.get(\'iid\',\'?\')})",
            "url": result.get("web_url"),
            "iid": result.get("iid"),
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_create_branch",
        "description": "Create a new branch in a GitLab project. Idempotent — if the branch already exists, returns it without error. from_branch auto-detects the repository's default branch when set to 'main'. Returns the branch URL. Create the branch before calling gitlab_create_or_update_file or gitlab_apply_patch on a non-default branch.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Namespace/project"},
                "branch":      {"type": "string", "description": "New branch name"},
                "from_branch": {"type": "string", "description": "Source branch", "default": "main"},
            },
            "required": ["repo", "branch"],
        },
        "code": _GITLAB_HELPERS + _BRANCH_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        branch      = inputs.get("branch", "")
        from_branch = inputs.get("from_branch", "main")
        if from_branch == "main":
            from_branch = _detect_default_branch(repo)
        url      = f"{_GITLAB_URL}/{repo}/-/tree/{branch}"
        existing = _get(f"/projects/{_proj(repo)}/repository/branches/{_url_quote(branch, safe=\'\')}")
        if isinstance(existing, dict) and "error" not in existing and existing.get("name"):
            return {"result": f"Branch exists (reusing): {branch} — {url}", "url": url}
        result = _post(f"/projects/{_proj(repo)}/repository/branches", {"branch": branch, "ref": from_branch})
        if "error" in result:
            err_str  = str(result.get("error", ""))
            body_txt = result.get("body", "")
            if "already exists" in body_txt.lower() or "already exists" in err_str.lower():
                return {"result": f"Branch exists (reusing): {branch} — {url}", "url": url}
            return {"error": result["error"]}
        return {"result": f"Branch created: {branch} from {from_branch} — {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_create_or_update_file",
        "description": "Create a new file or fully overwrite an existing file in a GitLab repository via a single commit. Auto-detects whether to create or update based on whether the file already exists on the branch. Returns the file URL. For partial edits (replacing a specific block of code), prefer gitlab_apply_patch — it is safer and avoids accidentally overwriting unrelated changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Namespace/project"},
                "path":    {"type": "string", "description": "File path in the repo"},
                "content": {"type": "string", "description": "New file content"},
                "message": {"type": "string", "description": "Commit message"},
                "branch":  {"type": "string", "description": "Target branch", "default": "main"},
            },
            "required": ["repo", "path", "content", "message"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo         = inputs.get("repo", "")
        path         = inputs.get("path", "")
        content      = _clean(inputs.get("content", ""))
        message      = _clean(inputs.get("message", "Update file"))
        branch       = inputs.get("branch", "main")
        encoded_path = _url_quote(path, safe="")
        proj_path    = f"/projects/{_proj(repo)}/repository/files/{encoded_path}"
        existing     = _get(f"{proj_path}?ref={branch}")
        exists       = isinstance(existing, dict) and "error" not in existing and existing.get("file_name")
        payload      = {"branch": branch, "commit_message": message, "content": content, "encoding": "text"}
        result       = _put(proj_path, payload) if exists else _post(proj_path, payload)
        if "error" in result:
            return {"error": result["error"]}
        action   = "Updated" if exists else "Created"
        file_url = f"{_GITLAB_URL}/{repo}/-/blob/{branch}/{path}"
        return {"result": f"{action} {path} on {branch} — {file_url}", "url": file_url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_apply_patch",
        "description": "Apply a targeted SEARCH/REPLACE edit to an existing file in a GitLab repository. Reads the current file content, finds the first occurrence of the search block, replaces it with the replace block, and commits the result. CRITICAL: the search string must match the file content exactly — whitespace, indentation, and newlines must be identical. Fails with an error if the search block is not found. Prefer this over gitlab_create_or_update_file when making partial edits to avoid overwriting unrelated code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Namespace/project"},
                "path":    {"type": "string", "description": "File path"},
                "search":  {"type": "string", "description": "Exact block of existing code to find"},
                "replace": {"type": "string", "description": "New code to substitute"},
                "branch":  {"type": "string", "description": "Branch", "default": "main"},
                "message": {"type": "string", "description": "Commit message", "default": "Apply patch"},
            },
            "required": ["repo", "path", "search", "replace"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo         = inputs.get("repo", "")
        path         = inputs.get("path", "")
        search       = inputs.get("search", "")
        replace      = inputs.get("replace", "")
        branch       = inputs.get("branch", "main")
        message      = inputs.get("message", "Apply patch")
        encoded_path = _url_quote(path, safe="")
        file_result  = _get(f"/projects/{_proj(repo)}/repository/files/{encoded_path}?ref={branch}")
        if isinstance(file_result, dict) and "error" in file_result:
            return {"error": file_result["error"]}
        if isinstance(file_result, dict) and file_result.get("encoding") == "base64":
            current = base64.b64decode(file_result["content"]).decode("utf-8")
        else:
            current = file_result.get("content", "") if isinstance(file_result, dict) else ""
        search_stripped = search.strip()
        if search_stripped not in current:
            return {"error": f"Search block not found in {repo}/{path} on branch \'{branch}\'. Whitespace must match exactly."}
        new_content = current.replace(search_stripped, replace.strip(), 1)
        if new_content == current:
            return {"error": "No change produced — search block identical to replace block."}
        proj_path = f"/projects/{_proj(repo)}/repository/files/{encoded_path}"
        payload   = {"branch": branch, "commit_message": message, "content": new_content, "encoding": "text"}
        result    = _put(proj_path, payload)
        if "error" in result:
            return {"error": result["error"]}
        file_url = f"{_GITLAB_URL}/{repo}/-/blob/{branch}/{path}"
        return {"result": f"Patch applied to {path} on {branch} — {file_url}", "url": file_url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_get_mr",
        "description": "Get full details of a specific merge request by its iid. Returns iid, title, state (opened/closed/merged), source_branch, target_branch, author username, web_url, and description (truncated to 500 chars). mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "mr_iid": {"type": "integer", "description": "MR internal ID (iid)"},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        mr_iid = int(inputs.get("mr_iid", 0))
        result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        data = {
            "iid":           mr_iid,
            "title":         result.get("title", ""),
            "state":         result.get("state", ""),
            "source_branch": result.get("source_branch", ""),
            "target_branch": result.get("target_branch", ""),
            "author":        (result.get("author") or {}).get("username", ""),
            "url":           result.get("web_url", ""),
            "description":   (result.get("description") or "")[:500],
        }
        result_str = (
            f"MR !{data[\'iid\']}: {data[\'title\']}\\n"
            f"State: {data[\'state\']}\\n"
            f"Branch: {data[\'source_branch\']} → {data[\'target_branch\']}\\n"
            f"Author: {data[\'author\']}\\n"
            f"URL: {data[\'url\']}"
        )
        return {"result": result_str, **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_link_mr_to_jira",
        "description": "Post a formatted markdown comment on a GitLab MR that links to a Jira issue. The comment includes a clickable hyperlink to the Jira ticket (e.g. '[AiNxt-123](https://ainxt.atlassian.net/browse/AiNxt-123)'). This does NOT use GitLab's native Jira integration — it simply posts a note. mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":     {"type": "string",  "description": "Namespace/project"},
                "mr_iid":   {"type": "integer", "description": "MR internal ID"},
                "jira_key": {"type": "string",  "description": "Jira issue key e.g. AiNxt-123"},
            },
            "required": ["repo", "mr_iid", "jira_key"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo     = inputs.get("repo", "")
        mr_iid   = int(inputs.get("mr_iid", 0))
        jira_key = inputs.get("jira_key", "")
        body = (
            f"**Jira Reference:** [{jira_key}](https://ainxt.atlassian.net/browse/{jira_key})\\n\\n"
            f"This MR is linked to Jira issue {jira_key}."
        )
        result = _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes", {"body": body})
        if "error" in result:
            return {"error": result["error"]}
        url = f"{_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"
        return {"result": f"Jira link comment posted on MR !{mr_iid}: {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_get_mr_review_comments",
        "description": "Fetch human review comments on a merge request, with system notes and AI-generated comments (containing '[AiNxt]' or 'AI-Generated') filtered out. Returns each comment's id, author username, and body. Use this to read meaningful reviewer feedback. note_id from results can be passed to gitlab_reply_to_review_comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "mr_iid": {"type": "integer", "description": "MR internal ID"},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        mr_iid = int(inputs.get("mr_iid", 0))
        notes  = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes?per_page=100&sort=asc")
        lines  = [f"Review comments on MR !{mr_iid} in {repo}:"]
        items  = []
        count  = 0
        if isinstance(notes, list):
            for c in notes:
                if c.get("system"): continue
                author = (c.get("author") or {}).get("username", "?")
                body   = c.get("body", "").strip()
                cid    = c.get("id", "?")
                if "[AiNxt]" in body or "AI-Generated" in body: continue
                lines.append(f"\\n[note #{cid}] {author}\\n  {body}")
                items.append({"id": cid, "author": author, "body": body})
                count += 1
        if count == 0:
            return {"result": f"No review comments found on MR !{mr_iid}.", "comments": []}
        lines.append(f"\\nTotal: {count} comment(s)")
        return {"result": "\\n".join(lines), "comments": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_get_mr_reviews",
        "description": "Fetch the approval state of a merge request. Returns approved (boolean) and approved_by (list of usernames who have approved). Call this before gitlab_merge_mr to verify the MR has the required approvals. mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "mr_iid": {"type": "integer", "description": "MR internal ID"},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        mr_iid = int(inputs.get("mr_iid", 0))
        result = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/approvals")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        approved_by = [(a.get("user") or {}).get("username", "?") for a in (result.get("approved_by") or [])]
        approved    = result.get("approved", False)
        return {
            "result": f"Approvals on MR !{mr_iid}:\\n  Approved by: {', '.join(approved_by)}\\n  Approved: {approved}",
            "approved": approved,
            "approved_by": approved_by,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_reply_to_review_comment",
        "description": "Reply to an existing comment thread on a merge request. note_id must come from gitlab_get_mr_review_comments. Automatically finds the discussion thread containing that note and posts the reply in-thread. Falls back to a top-level note if the thread cannot be found. Use this when responding to a specific reviewer comment; use gitlab_create_mr_review to post a general MR note.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string",  "description": "Namespace/project"},
                "mr_iid":  {"type": "integer", "description": "MR internal ID"},
                "note_id": {"type": "integer", "description": "Note ID to reply to"},
                "body":    {"type": "string",  "description": "Reply text"},
            },
            "required": ["repo", "mr_iid", "note_id", "body"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo          = inputs.get("repo", "")
        mr_iid        = int(inputs.get("mr_iid", 0))
        note_id       = int(inputs.get("note_id", 0))
        body          = _clean(inputs.get("body", ""))
        discussions   = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/discussions?per_page=100")
        discussion_id = None
        if isinstance(discussions, list):
            for d in discussions:
                for note in (d.get("notes") or []):
                    if note.get("id") == note_id:
                        discussion_id = d.get("id")
                        break
                if discussion_id: break
        if not discussion_id:
            result = _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes", {"body": body})
        else:
            result = _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/discussions/{discussion_id}/notes", {"body": body})
        if "error" in result:
            return {"error": result["error"]}
        url = f"{_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"
        return {"result": f"Reply posted to note #{note_id}: {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_get_mr_files",
        "description": "Get the list of changed files in a merge request with structured diff data. Returns each file's filename, status (added/modified/deleted), additions count, deletions count, and full patch text. mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "mr_iid":    {"type": "integer", "description": "MR internal ID"},
                "max_files": {"type": "integer", "description": "Max files to return", "default": 20},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        mr_iid    = int(inputs.get("mr_iid", 0))
        max_files = int(inputs.get("max_files", 20))
        result    = _get(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/changes")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        changes = result.get("changes", []) if isinstance(result, dict) else []
        files   = []
        for f in changes[:max_files]:
            diff      = f.get("diff", "")
            additions = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
            deletions = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
            status    = "added" if f.get("new_file") else ("deleted" if f.get("deleted_file") else "modified")
            files.append({
                "filename":  f.get("new_path", f.get("old_path", "")),
                "status":    status,
                "additions": additions,
                "deletions": deletions,
                "patch":     diff,
            })
        return {"result": f"{len(files)} file(s) changed in MR !{mr_iid}", "files": files}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_create_mr_review",
        "description": "Post a review note on a merge request with an optional action. event values: APPROVE (approves the MR and posts the note), REQUEST_CHANGES (posts the note only — GitLab does not natively block merges on this), COMMENT (posts the note only, default). body supports markdown. mr_iid from gitlab_list_mrs or gitlab_create_mr. To approve without a note, use gitlab_approve_merge_request instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "mr_iid": {"type": "integer", "description": "MR internal ID"},
                "body":   {"type": "string",  "description": "Review summary (markdown)"},
                "event":  {"type": "string",  "description": "APPROVE | REQUEST_CHANGES | COMMENT", "default": "COMMENT"},
            },
            "required": ["repo", "mr_iid", "body"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        mr_iid = int(inputs.get("mr_iid", 0))
        body   = _clean(inputs.get("body", ""))
        event  = inputs.get("event", "COMMENT")
        if event == "APPROVE":
            _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/approve", {})
        result = _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/notes", {"body": body})
        if "error" in result:
            return {"error": result["error"]}
        url = f"{_GITLAB_URL}/{repo}/-/merge_requests/{mr_iid}"
        return {"result": f"MR review posted (event={event}): {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "gitlab_merge_mr",
        "description": "Merge an approved merge request. merge_method values: 'squash' (squashes all source branch commits into a single commit on target, default), 'merge' (creates a merge commit preserving all commits), 'rebase' (rebases source commits onto target). Returns the resulting commit SHA. Verify approvals with gitlab_get_mr_reviews before merging. mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":         {"type": "string",  "description": "Namespace/project"},
                "mr_iid":       {"type": "integer", "description": "MR internal ID"},
                "merge_method": {"type": "string",  "description": "squash | merge | rebase", "default": "squash"},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo         = inputs.get("repo", "")
        mr_iid       = int(inputs.get("mr_iid", 0))
        merge_method = inputs.get("merge_method", "squash")
        squash       = merge_method == "squash"
        result       = _put(
            f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/merge",
            {"squash": squash, "should_remove_source_branch": False},
        )
        if "error" in result:
            return {"error": result["error"]}
        sha = result.get("sha", "?")
        return {"result": f"MR !{mr_iid} merged ({merge_method}) — commit sha: {sha}", "sha": sha}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_commits                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_commits",
        "description": "List recent commits on a branch in a GitLab repository. Returns each commit's short SHA, title (first line of message), author name, and date (YYYY-MM-DD). branch defaults to 'main'. Use the SHA from results with gitlab_get_commit_diff to inspect what changed in a specific commit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "branch": {"type": "string",  "description": "Branch name", "default": "main"},
                "limit":  {"type": "integer", "description": "Max commits to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        branch = inputs.get("branch", "main")
        limit  = int(inputs.get("limit", 20))
        result = _get(f"/projects/{_proj(repo)}/repository/commits?ref_name={branch}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        items = []
        lines = [f"Commits on {branch} in {repo}:"]
        for c in result:
            sha     = c.get("short_id", c.get("id", "?")[:8])
            title   = c.get("title", "")
            author  = c.get("author_name", "?")
            date    = (c.get("created_at") or "")[:10]
            lines.append(f"• {sha} {date} {author}: {title}")
            items.append({"sha": sha, "title": title, "author": author, "date": date})
        return {"result": "\\n".join(lines), "commits": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_tree                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_tree",
        "description": "List files and directories at a given path in a GitLab repository. Leave path empty to list the root directory. Returns each entry's name, type ('blob' for file, 'tree' for directory), and full path. Set recursive=true to list all files in all subdirectories (up to 100 entries). Use this to explore the repo structure before reading specific files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "path":      {"type": "string",  "description": "Directory path (empty for root)", "default": ""},
                "branch":    {"type": "string",  "description": "Branch name", "default": "main"},
                "recursive": {"type": "boolean", "description": "List recursively", "default": False},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        path      = inputs.get("path", "")
        branch    = inputs.get("branch", "main")
        recursive = inputs.get("recursive", False)
        params    = f"?ref={branch}&per_page=100&recursive={'true' if recursive else 'false'}"
        if path:
            params += f"&path={_url_quote(path, safe='')}"
        result = _get(f"/projects/{_proj(repo)}/repository/tree{params}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        items = [{"name": f["name"], "type": f["type"], "path": f["path"]} for f in result]
        lines = [f"Tree of {repo}/{path or ''} ({branch}):"] + [f"  {'📁' if f['type'] == 'tree' else '📄'} {f['path']}" for f in result]
        return {"result": "\\n".join(lines), "entries": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_compare_branches                                              #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_compare_branches",
        "description": "Compare two branches or commit SHAs in a GitLab repository. Returns the number of commits between them and a list of changed file paths. Use this to understand what diverged between a feature branch and main, or to check what a commit range introduced before creating an MR.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Namespace/project"},
                "from_":  {"type": "string", "description": "Source branch or commit SHA"},
                "to":     {"type": "string", "description": "Target branch or commit SHA"},
            },
            "required": ["repo", "from_", "to"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        from_ = inputs.get("from_", "")
        to    = inputs.get("to", "")
        result = _get(f"/projects/{_proj(repo)}/repository/compare?from={_url_quote(from_, safe='')}&to={_url_quote(to, safe='')}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        commits = result.get("commits", [])
        diffs   = result.get("diffs", [])
        files   = [d.get("new_path", d.get("old_path", "?")) for d in diffs]
        return {
            "result": f"Compare {from_}...{to}: {len(commits)} commit(s), {len(diffs)} file(s) changed",
            "commits": len(commits),
            "files_changed": files,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_commit_diff                                               #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_commit_diff",
        "description": "Get the file-level diff for a specific commit SHA in a GitLab repository. Returns each changed file's filename, status (added/modified/deleted), and patch text (truncated to 2000 chars per file). SHA can be obtained from gitlab_list_commits. Use this to inspect exactly what a specific commit changed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Namespace/project"},
                "sha":  {"type": "string", "description": "Commit SHA"},
            },
            "required": ["repo", "sha"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        sha    = inputs.get("sha", "")
        result = _get(f"/projects/{_proj(repo)}/repository/commits/{sha}/diff")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        files = []
        for d in result:
            files.append({
                "filename":  d.get("new_path", d.get("old_path", "")),
                "status":    "added" if d.get("new_file") else ("deleted" if d.get("deleted_file") else "modified"),
                "patch":     d.get("diff", "")[:2000],
            })
        return {"result": f"Diff for {sha[:8]}: {len(files)} file(s) changed", "files": files}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_project                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_project",
        "description": "Get details of a GitLab project by its namespace/path (e.g. 'ainxt/payment-service'). Returns id, name, description (truncated to 300 chars), default_branch, visibility (private/internal/public), web_url, star_count, and forks_count. Use gitlab_search_projects first if you don't know the exact project path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Namespace/project e.g. ainxt/payment-service"},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        result = _get(f"/projects/{_proj(repo)}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        data = {
            "id":             result.get("id"),
            "name":           result.get("name"),
            "description":    (result.get("description") or "")[:300],
            "default_branch": result.get("default_branch"),
            "visibility":     result.get("visibility"),
            "url":            result.get("web_url"),
            "stars":          result.get("star_count", 0),
            "forks":          result.get("forks_count", 0),
        }
        return {"result": f"{data['name']} ({data['visibility']}) — {data['url']}", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_create_project                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_create_project",
        "description": "Create a new GitLab project. visibility defaults to 'private'; other values: 'internal', 'public'. namespace is an optional group path to create the project under (e.g. 'ainxt/team-a') — use gitlab_list_groups to find valid namespace paths. Returns the new project id and web_url.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "Project name"},
                "description": {"type": "string", "description": "Project description"},
                "visibility":  {"type": "string", "description": "private | internal | public", "default": "private"},
                "namespace":   {"type": "string", "description": "Group/namespace path (optional)"},
            },
            "required": ["name"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        payload = {
            "name":        inputs.get("name", ""),
            "description": inputs.get("description", ""),
            "visibility":  inputs.get("visibility", "private"),
        }
        if inputs.get("namespace"):
            payload["namespace_id"] = inputs["namespace"]
        result = _post("/projects", payload)
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Project created: {result.get('web_url', '?')}", "url": result.get("web_url"), "id": result.get("id")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_search_projects                                               #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_search_projects",
        "description": "Search for GitLab projects by name, ordered by last activity. Returns each project's id, full name with namespace (e.g. 'AiNxt / payment-service'), and web_url. Use the path_with_namespace as the 'repo' input for other tools. Use gitlab_get_project for full project details once you have the path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",  "description": "Search term"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        query  = inputs.get("query", "")
        limit  = int(inputs.get("limit", 10))
        result = _get(f"/projects?search={_url_quote(query, safe='')}&per_page={limit}&order_by=last_activity_at")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        items = [{"id": p["id"], "name": p["name_with_namespace"], "url": p.get("web_url")} for p in result]
        lines = [f"Projects matching '{query}':"] + [f"• {p['name_with_namespace']} — {p.get('web_url', '?')}" for p in result]
        return {"result": "\\n".join(lines), "projects": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_issue                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_issue",
        "description": "Get full details of a specific GitLab issue by its iid. Returns iid, title, state (opened/closed), description (truncated to 500 chars), author username, assignee usernames, labels, and web_url. issue_iid comes from gitlab_list_issues or gitlab_create_issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "issue_iid": {"type": "integer", "description": "Issue internal ID (iid)"},
            },
            "required": ["repo", "issue_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        issue_iid = int(inputs.get("issue_iid", 0))
        result    = _get(f"/projects/{_proj(repo)}/issues/{issue_iid}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        data = {
            "iid":         result.get("iid"),
            "title":       result.get("title", ""),
            "state":       result.get("state", ""),
            "description": (result.get("description") or "")[:500],
            "author":      (result.get("author") or {}).get("username", ""),
            "assignees":   [(a.get("username", "")) for a in (result.get("assignees") or [])],
            "labels":      result.get("labels", []),
            "url":         result.get("web_url", ""),
        }
        return {"result": f"Issue #{data['iid']}: {data['title']} ({data['state']}) — {data['url']}", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_update_issue                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_update_issue",
        "description": "Update a GitLab issue's title, description, or labels. Only fields you provide are changed — omitted fields are left unchanged. Note: labels replaces all existing labels (not additive). To reopen a closed issue, use this tool with the GitLab API directly (state_event=reopen) — or use gitlab_close_issue to close. issue_iid from gitlab_list_issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "issue_iid":   {"type": "integer", "description": "Issue iid"},
                "title":       {"type": "string",  "description": "New title"},
                "description": {"type": "string",  "description": "New description"},
                "labels":      {"type": "array",   "description": "Label names", "items": {"type": "string"}},
            },
            "required": ["repo", "issue_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        issue_iid = int(inputs.get("issue_iid", 0))
        payload   = {}
        if inputs.get("title"):       payload["title"]       = inputs["title"]
        if inputs.get("description"): payload["description"] = inputs["description"]
        if inputs.get("labels"):      payload["labels"]      = ",".join(inputs["labels"])
        result = _put(f"/projects/{_proj(repo)}/issues/{issue_iid}", payload)
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Issue #{issue_iid} in {repo} updated.", "url": result.get("web_url", "")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_close_issue                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_close_issue",
        "description": "Close an open GitLab issue by setting its state to 'closed'. issue_iid from gitlab_list_issues or gitlab_create_issue. Returns the issue URL. To add a closing comment at the same time, call gitlab_add_issue_note before or after closing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "issue_iid": {"type": "integer", "description": "Issue iid"},
            },
            "required": ["repo", "issue_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        issue_iid = int(inputs.get("issue_iid", 0))
        result    = _put(f"/projects/{_proj(repo)}/issues/{issue_iid}", {"state_event": "close"})
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Issue #{issue_iid} in {repo} closed.", "url": result.get("web_url", "")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_issue_notes                                              #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_issue_notes",
        "description": "List human-written comments (notes) on a GitLab issue. System-generated notes (e.g. 'issue was closed') are filtered out. Returns each note's id, author username, and body (truncated to 300 chars). Fetches up to 50 notes in ascending order. issue_iid from gitlab_list_issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "issue_iid": {"type": "integer", "description": "Issue iid"},
            },
            "required": ["repo", "issue_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        issue_iid = int(inputs.get("issue_iid", 0))
        notes     = _get(f"/projects/{_proj(repo)}/issues/{issue_iid}/notes?per_page=50&sort=asc")
        if isinstance(notes, dict) and "error" in notes:
            return {"error": notes["error"]}
        items = []
        lines = [f"Notes on issue #{issue_iid}:"]
        for n in (notes if isinstance(notes, list) else []):
            if n.get("system"): continue
            author = (n.get("author") or {}).get("username", "?")
            body   = n.get("body", "")[:300]
            nid    = n.get("id", "?")
            lines.append(f"• [{nid}] {author}: {body}")
            items.append({"id": nid, "author": author, "body": body})
        return {"result": "\\n".join(lines), "notes": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_add_issue_note                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_add_issue_note",
        "description": "Post a new comment (note) on a GitLab issue. body supports markdown. issue_iid from gitlab_list_issues or gitlab_create_issue. Returns the issue URL. Use this to add status updates, ask questions, or provide context on an issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":      {"type": "string",  "description": "Namespace/project"},
                "issue_iid": {"type": "integer", "description": "Issue iid"},
                "body":      {"type": "string",  "description": "Comment text (markdown)"},
            },
            "required": ["repo", "issue_iid", "body"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo      = inputs.get("repo", "")
        issue_iid = int(inputs.get("issue_iid", 0))
        body      = _clean(inputs.get("body", ""))
        result    = _post(f"/projects/{_proj(repo)}/issues/{issue_iid}/notes", {"body": body})
        if "error" in result:
            return {"error": result["error"]}
        url = f"{_GITLAB_URL}/{repo}/-/issues/{issue_iid}"
        return {"result": f"Note added to issue #{issue_iid}: {url}", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_approve_merge_request                                         #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_approve_merge_request",
        "description": "Approve a GitLab merge request as the currently authenticated user. Records the approval without posting a comment. Use gitlab_get_mr_reviews to verify the approval was recorded. To approve and post a review comment at the same time, use gitlab_create_mr_review with event=APPROVE. mr_iid from gitlab_list_mrs or gitlab_create_mr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "mr_iid": {"type": "integer", "description": "MR internal ID"},
            },
            "required": ["repo", "mr_iid"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        mr_iid = int(inputs.get("mr_iid", 0))
        result = _post(f"/projects/{_proj(repo)}/merge_requests/{mr_iid}/approve", {})
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"MR !{mr_iid} approved."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_pipelines                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_pipelines",
        "description": "List CI/CD pipelines for a GitLab project, optionally filtered by branch and/or status. status values: 'running', 'pending', 'success', 'failed', 'canceled'. Returns each pipeline's id, ref (branch), status, and url. Pipeline id from here is required for gitlab_get_pipeline, gitlab_cancel_pipeline, gitlab_retry_pipeline, gitlab_get_pipeline_jobs, and gitlab_list_pipeline_variables.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "branch": {"type": "string",  "description": "Filter by branch"},
                "status": {"type": "string",  "description": "running | pending | success | failed | canceled"},
                "limit":  {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        branch = inputs.get("branch", "")
        status = inputs.get("status", "")
        limit  = int(inputs.get("limit", 10))
        params = f"?per_page={limit}&order_by=id&sort=desc"
        if branch: params += f"&ref={_url_quote(branch, safe='')}"
        if status: params += f"&status={status}"
        result = _get(f"/projects/{_proj(repo)}/pipelines{params}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = []
        lines = [f"Pipelines in {repo}:"]
        for p in (result if isinstance(result, list) else []):
            pid    = p.get("id")
            ref    = p.get("ref", "?")
            pstatus = p.get("status", "?")
            lines.append(f"• #{pid} [{pstatus}] {ref}")
            items.append({"id": pid, "ref": ref, "status": pstatus, "url": p.get("web_url", "")})
        return {"result": "\\n".join(lines), "pipelines": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_pipeline                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_pipeline",
        "description": "Get details of a specific GitLab pipeline by its id. Returns id, status (running/pending/success/failed/canceled), ref (branch), short SHA, duration (seconds), and url. pipeline_id from gitlab_list_pipelines. Use gitlab_get_pipeline_jobs to see individual job statuses within this pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["repo", "pipeline_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        pipeline_id = int(inputs.get("pipeline_id", 0))
        result      = _get(f"/projects/{_proj(repo)}/pipelines/{pipeline_id}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        data = {
            "id":       result.get("id"),
            "status":   result.get("status"),
            "ref":      result.get("ref"),
            "sha":      result.get("sha", "")[:8],
            "duration": result.get("duration"),
            "url":      result.get("web_url", ""),
        }
        return {"result": f"Pipeline #{data['id']} [{data['status']}] {data['ref']} — {data['url']}", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_trigger_pipeline                                              #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_trigger_pipeline",
        "description": "Trigger a new CI/CD pipeline run for a branch in GitLab. Returns the new pipeline id and url. Use gitlab_get_pipeline with the returned id to poll the pipeline status, or gitlab_get_pipeline_jobs to monitor individual job progress.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string", "description": "Namespace/project"},
                "branch": {"type": "string", "description": "Branch to run pipeline on", "default": "main"},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        branch = inputs.get("branch", "main")
        result = _post(f"/projects/{_proj(repo)}/pipeline", {"ref": branch})
        if "error" in result:
            return {"error": result["error"]}
        pid = result.get("id")
        url = result.get("web_url", "")
        return {"result": f"Pipeline #{pid} triggered on {branch} — {url}", "id": pid, "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_cancel_pipeline                                               #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_cancel_pipeline",
        "description": "Cancel a running or pending GitLab pipeline. pipeline_id from gitlab_list_pipelines. Only works on pipelines in 'running' or 'pending' state — has no effect on already completed pipelines. Use gitlab_get_pipeline to verify the pipeline status before cancelling.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["repo", "pipeline_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        pipeline_id = int(inputs.get("pipeline_id", 0))
        result      = _post(f"/projects/{_proj(repo)}/pipelines/{pipeline_id}/cancel", {})
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Pipeline #{pipeline_id} cancelled."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_retry_pipeline                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_retry_pipeline",
        "description": "Retry all failed jobs in a GitLab pipeline, creating a new pipeline run. pipeline_id from gitlab_list_pipelines. Returns the new pipeline id. Use gitlab_get_pipeline to monitor the retried pipeline's status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["repo", "pipeline_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        pipeline_id = int(inputs.get("pipeline_id", 0))
        result      = _post(f"/projects/{_proj(repo)}/pipelines/{pipeline_id}/retry", {})
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Pipeline #{pipeline_id} retried.", "id": result.get("id")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_pipeline_variables                                       #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_pipeline_variables",
        "description": "List the CI/CD variables that were passed to a specific pipeline at trigger time. Returns each variable's key and value. pipeline_id from gitlab_list_pipelines. Useful for auditing what environment variables or parameters a pipeline was run with.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["repo", "pipeline_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        pipeline_id = int(inputs.get("pipeline_id", 0))
        result      = _get(f"/projects/{_proj(repo)}/pipelines/{pipeline_id}/variables")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = [{"key": v.get("key"), "value": v.get("value")} for v in (result if isinstance(result, list) else [])]
        return {"result": f"{len(items)} variable(s) in pipeline #{pipeline_id}", "variables": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_jobs                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_jobs",
        "description": "List CI/CD jobs across the entire GitLab project (not scoped to a specific pipeline). scope filter values: 'created', 'pending', 'running', 'failed', 'success', 'canceled', 'skipped'. Returns each job's id, name, status, and ref (branch). Job id from here is needed for gitlab_get_job_log and gitlab_retry_job. Use gitlab_get_pipeline_jobs instead when you have a specific pipeline_id and want jobs scoped to that pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "scope":  {"type": "string",  "description": "created | pending | running | failed | success | canceled | skipped"},
                "limit":  {"type": "integer", "description": "Max results", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo  = inputs.get("repo", "")
        scope = inputs.get("scope", "")
        limit = int(inputs.get("limit", 20))
        params = f"?per_page={limit}"
        if scope: params += f"&scope[]={scope}"
        result = _get(f"/projects/{_proj(repo)}/jobs{params}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = []
        lines = [f"Jobs in {repo}:"]
        for j in (result if isinstance(result, list) else []):
            jid    = j.get("id")
            name   = j.get("name", "?")
            status = j.get("status", "?")
            ref    = j.get("ref", "?")
            lines.append(f"• #{jid} [{status}] {name} ({ref})")
            items.append({"id": jid, "name": name, "status": status, "ref": ref})
        return {"result": "\\n".join(lines), "jobs": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_job_log                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_job_log",
        "description": "Get the console log output of a GitLab CI/CD job. Returns the last 3000 characters of the log (truncated from the start if longer) and a 'truncated' boolean flag. job_id from gitlab_list_jobs or gitlab_get_pipeline_jobs. Use this to diagnose build failures or inspect job output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "job_id": {"type": "integer", "description": "Job ID"},
            },
            "required": ["repo", "job_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        import urllib.request, urllib.error
        repo   = inputs.get("repo", "")
        job_id = int(inputs.get("job_id", 0))
        url    = f"{_GITLAB_API}/projects/{_proj(repo)}/jobs/{job_id}/trace"
        req    = urllib.request.Request(url, headers=_headers())
        opener = _make_opener()
        with opener.open(req, timeout=60) as resp:
            log = resp.read().decode("utf-8", errors="replace")
        return {"result": log[-3000:] if len(log) > 3000 else log, "truncated": len(log) > 3000}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_pipeline_jobs                                             #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_pipeline_jobs",
        "description": "List all jobs for a specific GitLab pipeline, grouped by stage. Returns each job's id, name, status, and stage. pipeline_id from gitlab_list_pipelines or gitlab_trigger_pipeline. Use this to see which jobs passed/failed within a pipeline. Use gitlab_list_jobs instead for a project-wide job listing without a specific pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string",  "description": "Namespace/project"},
                "pipeline_id": {"type": "integer", "description": "Pipeline ID"},
            },
            "required": ["repo", "pipeline_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        pipeline_id = int(inputs.get("pipeline_id", 0))
        result      = _get(f"/projects/{_proj(repo)}/pipelines/{pipeline_id}/jobs?per_page=100")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = []
        lines = [f"Jobs in pipeline #{pipeline_id}:"]
        for j in (result if isinstance(result, list) else []):
            jid    = j.get("id")
            name   = j.get("name", "?")
            status = j.get("status", "?")
            stage  = j.get("stage", "?")
            lines.append(f"• #{jid} [{status}] {stage}/{name}")
            items.append({"id": jid, "name": name, "status": status, "stage": stage})
        return {"result": "\\n".join(lines), "jobs": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_retry_job                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_retry_job",
        "description": "Retry a failed or cancelled GitLab CI/CD job. job_id from gitlab_list_jobs or gitlab_get_pipeline_jobs. Returns the new job id. Use gitlab_get_job_log on the new job id to monitor the retry output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":   {"type": "string",  "description": "Namespace/project"},
                "job_id": {"type": "integer", "description": "Job ID"},
            },
            "required": ["repo", "job_id"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        job_id = int(inputs.get("job_id", 0))
        result = _post(f"/projects/{_proj(repo)}/jobs/{job_id}/retry", {})
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Job #{job_id} retried.", "new_job_id": result.get("id")}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_tags                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_tags",
        "description": "List tags in a GitLab repository ordered by version descending. Returns each tag's name and short commit SHA. Tag names from here are used as the 'tag' input for gitlab_create_release. Use gitlab_create_tag to create a new tag before creating a release.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Namespace/project"},
                "limit": {"type": "integer", "description": "Max tags to return", "default": 20},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        limit  = int(inputs.get("limit", 20))
        result = _get(f"/projects/{_proj(repo)}/repository/tags?per_page={limit}&order_by=version&sort=desc")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = [{"name": t["name"], "sha": (t.get("commit") or {}).get("short_id", "?")} for t in (result if isinstance(result, list) else [])]
        lines = [f"Tags in {repo}:"] + [f"• {t['name']} ({t['sha']})" for t in items]
        return {"result": "\\n".join(lines), "tags": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_create_tag                                                    #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_create_tag",
        "description": "Create a tag in a GitLab repository. ref can be a branch name or commit SHA. Providing a message creates an annotated tag (stores tagger name, date, and message); omitting message creates a lightweight tag (just a pointer to a commit). Tag name is typically a version string (e.g. 'v1.2.0'). Create the tag before calling gitlab_create_release.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":    {"type": "string", "description": "Namespace/project"},
                "tag":     {"type": "string", "description": "Tag name e.g. v1.2.0"},
                "ref":     {"type": "string", "description": "Branch or commit SHA to tag", "default": "main"},
                "message": {"type": "string", "description": "Annotated tag message (optional)"},
            },
            "required": ["repo", "tag"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo    = inputs.get("repo", "")
        tag     = inputs.get("tag", "")
        ref     = inputs.get("ref", "main")
        message = inputs.get("message", "")
        payload = {"tag_name": tag, "ref": ref}
        if message:
            payload["message"] = message
        result = _post(f"/projects/{_proj(repo)}/repository/tags", payload)
        if "error" in result:
            return {"error": result["error"]}
        return {"result": f"Tag '{tag}' created at {ref} in {repo}."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_releases                                                 #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_releases",
        "description": "List releases in a GitLab project. Returns each release's tag_name, release name, and self URL. A tag must exist before a release can be created — use gitlab_create_tag first if needed. Use gitlab_create_release to publish a new release.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":  {"type": "string",  "description": "Namespace/project"},
                "limit": {"type": "integer", "description": "Max releases to return", "default": 10},
            },
            "required": ["repo"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo   = inputs.get("repo", "")
        limit  = int(inputs.get("limit", 10))
        result = _get(f"/projects/{_proj(repo)}/releases?per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = [{"tag": r.get("tag_name"), "name": r.get("name"), "url": r.get("_links", {}).get("self", "")} for r in (result if isinstance(result, list) else [])]
        lines = [f"Releases in {repo}:"] + [f"• {r['tag']} — {r['name']}" for r in items]
        return {"result": "\\n".join(lines), "releases": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_create_release                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_create_release",
        "description": "Create a release in a GitLab project tied to an existing tag. The tag must already exist — create it first with gitlab_create_tag if needed. description supports markdown and is used as the release notes. Returns the release URL. Use gitlab_list_releases to verify the release was created.",
        "input_schema": {
            "type": "object",
            "properties": {
                "repo":        {"type": "string", "description": "Namespace/project"},
                "tag":         {"type": "string", "description": "Tag name for the release"},
                "name":        {"type": "string", "description": "Release name"},
                "description": {"type": "string", "description": "Release notes (markdown)"},
            },
            "required": ["repo", "tag", "name"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        repo        = inputs.get("repo", "")
        tag         = inputs.get("tag", "")
        name        = inputs.get("name", "")
        description = inputs.get("description", "")
        result      = _post(f"/projects/{_proj(repo)}/releases", {"tag_name": tag, "name": name, "description": description})
        if "error" in result:
            return {"error": result["error"]}
        url = (result.get("_links") or {}).get("self", "")
        return {"result": f"Release '{name}' ({tag}) created in {repo}.", "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_current_user                                              #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_current_user",
        "description": "Get the profile of the currently authenticated GitLab user (the owner of the GITLAB_TOKEN). Returns id, username, name, email, and state (active/blocked). Use this to get your own username before filtering issues or MRs by assignee, or to verify which account is being used.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        result = _get("/user")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        data = {
            "id":       result.get("id"),
            "username": result.get("username"),
            "name":     result.get("name"),
            "email":    result.get("email", ""),
            "state":    result.get("state"),
        }
        return {"result": f"Current user: {data['name']} (@{data['username']})", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_get_user                                                      #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_get_user",
        "description": "Look up a GitLab user by their exact username. Returns id, username, name, and state (active/blocked). The numeric id is needed for API calls that require a user_id. Use gitlab_get_current_user to look up the authenticated user instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "username": {"type": "string", "description": "GitLab username"},
            },
            "required": ["username"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        username = inputs.get("username", "")
        result   = _get(f"/users?username={_url_quote(username, safe='')}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        users = result if isinstance(result, list) else []
        if not users:
            return {"error": f"User '{username}' not found."}
        u = users[0]
        data = {"id": u.get("id"), "username": u.get("username"), "name": u.get("name"), "state": u.get("state")}
        return {"result": f"User: {data['name']} (@{data['username']}) — id={data['id']}", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_list_groups                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_list_groups",
        "description": "List GitLab groups accessible to the authenticated user. Returns each group's id, name, and full_path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max groups to return", "default": 20},
            },
            "required": [],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        limit  = int(inputs.get("limit", 20))
        result = _get(f"/groups?per_page={limit}&order_by=name")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items = [{"id": g.get("id"), "name": g.get("name"), "path": g.get("full_path")} for g in (result if isinstance(result, list) else [])]
        lines = ["Groups:"] + [f"• [{g['id']}] {g['name']} ({g['path']})" for g in items]
        return {"result": "\\n".join(lines), "groups": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # gitlab_search                                                        #
    # ------------------------------------------------------------------ #
    {
        "name": "gitlab_search",
        "description": "Global search across GitLab. scope values and what each returns: 'projects' (project name and url), 'issues' (issue title and iid), 'merge_requests' (MR title and iid), 'blobs' (filename and matching code snippet), 'users' (username and name). Default scope is 'projects'. For code search within a specific repo, use gitlab_search_code instead.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "scope": {"type": "string", "description": "projects | issues | merge_requests | blobs | users", "default": "projects"},
                "limit": {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        "code": _GITLAB_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        query  = inputs.get("query", "")
        scope  = inputs.get("scope", "projects")
        limit  = int(inputs.get("limit", 10))
        result = _get(f"/search?scope={scope}&search={_url_quote(query, safe='')}&per_page={limit}")
        if isinstance(result, dict) and "error" in result:
            return {"error": result["error"]}
        items  = result if isinstance(result, list) else []
        lines  = [f"GitLab search '{query}' ({scope}): {len(items)} result(s)"]
        for r in items:
            name = r.get("name") or r.get("title") or r.get("filename") or str(r.get("id", "?"))
            lines.append(f"• {name}")
        return {"result": "\\n".join(lines), "results": items[:limit]}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
