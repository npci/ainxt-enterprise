# SPDX-License-Identifier: Apache-2.0
"""
Jira tools — adapted from AiNxt Agentic Platform jira_tools.py.

core.* imports stripped; credentials come from env vars injected by the
connections system (JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN).
Each tool's `code` string is self-contained and runs in the sandbox subprocess.
"""

# ---------------------------------------------------------------------------
# Shared helper block — included verbatim at the top of every tool's code
# ---------------------------------------------------------------------------

_HELPERS = '''
import os, json, base64, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone

def _jira_base():
    return os.environ.get("JIRA_URL", "").rstrip("/")

def _default_project():
    return os.environ.get("JIRA_PROJECT", "")

_JIRA_NOT_CONFIGURED = (
    "You have not configured an Atlassian (Jira) API token. "
    "Add it under Profile \u2192 Atlassian Token, then retry. "
    "(The platform does not use a shared/service Jira account.)"
)

def _auth_header():
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    if not email or not token:
        raise PermissionError(_JIRA_NOT_CONFIGURED)
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"

def _jira_http_message(code, body=""):
    """Map a Jira HTTP status to a clear, actionable message."""
    if code == 401:
        return ("Jira token is invalid or expired. "
                "Update it under Profile \u2192 Atlassian Token.")
    if code == 403:
        return ("Your Jira token does not have permission for this project or action. "
                "Ask a Jira admin for access.")
    if code == 404:
        return ("Jira issue/project not found, or your token has no access to it.")
    return f"HTTP {code}: {str(body)[:400]}"

_HTTPS_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("FORWARD_PROXY_URL")
    or ""
)

def _make_opener():
    import ssl
    # Security review F-07: TLS certificate verification is always enforced
    # (CWE-599). When REQUESTS_CA_BUNDLE / SSL_CERT_FILE points at a CA bundle
    # (a corporate CA, or the cert of a TLS-terminating proxy) we verify against
    # it; otherwise we verify against the system trust store. There is
    # deliberately no unverified fallback — an environment without a usable
    # trust anchor must be provisioned with one rather than silently accepting
    # forged certificates on a path that carries an API token.
    _ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or ""
    _ssl_ctx = ssl.create_default_context(cafile=_ca_bundle or None)
    https_handler = urllib.request.HTTPSHandler(context=_ssl_ctx)
    if _HTTPS_PROXY:
        proxy_handler = urllib.request.ProxyHandler({"https": _HTTPS_PROXY, "http": _HTTPS_PROXY})
        return urllib.request.build_opener(proxy_handler, https_handler)
    return urllib.request.build_opener(https_handler)

def _request(method, path, payload=None):
    # ── LLM proxy path (production: gateway → LLM proxy → Atlassian Cloud) ──────────
    # When LLM_PROXY_URL is set, this host cannot reach Atlassian directly.
    # Relay the call through POST /atlassian/proxy on the LLM proxy service.
    llm_proxy_url = os.environ.get("LLM_PROXY_URL", "").rstrip("/")
    if llm_proxy_url:
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        if not email or not token:
            raise PermissionError(_JIRA_NOT_CONFIGURED)
        proxy_payload = {
            "service": "jira",
            "method":  method,
            "path":    path,
            "body":    payload,
            "email":   email,
            "token":   token,
        }
        proxy_data = json.dumps(proxy_payload).encode()
        proxy_req  = urllib.request.Request(
            f"{llm_proxy_url}/atlassian/proxy",
            data=proxy_data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        opener = _make_opener()
        try:
            # Enterprise-grade timeout: 90s tolerates slow Atlassian Cloud
            # responses (rate-limit retries, multi-region replication lag)
            # when this call sits inside an attached/generate-with-AI flow.
            with opener.open(proxy_req, timeout=90) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise Exception(_jira_http_message(e.code, body))
        except urllib.error.URLError as e:
            # Surface expired tokens / network failures as structured errors
            # rather than crashing the whole tool-call loop.
            raise Exception(f"Jira proxy unreachable: {e.reason}")

    # ── Direct path (local dev: call Jira directly) ────────────────────────────
    url  = f"{_jira_base()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": _auth_header(),
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
        method=method,
    )
    opener = _make_opener()
    try:
        # Enterprise-grade timeout: 60s. Direct Atlassian Cloud calls under
        # load (search/JQL, attachments) frequently exceed 15s; we raise the
        # ceiling so chained agents and attached workflows don't abort mid-run.
        with opener.open(req, timeout=60) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise Exception(_jira_http_message(e.code, body))
    except urllib.error.URLError as e:
        raise Exception(f"Jira unreachable: {e.reason}")

def _account_id_from_email(email):
    email = (email or "").strip()
    if not email:
        raise ValueError("assignee_email_id is required.")
    path = "/rest/api/3/user/search?query=" + urllib.parse.quote(email)
    users = _request("GET", path)
    if not isinstance(users, list) or not users:
        raise ValueError(f"No Jira user found for email: {email}")
    exact = next(
        (u for u in users if (u.get("emailAddress") or "").lower() == email.lower()),
        users[0],
    )
    account_id = exact.get("accountId")
    if not account_id:
        raise ValueError(f"Jira user found for email {email}, but accountId is missing.")
    return account_id


def _adf_text(text):
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }

def _adf_to_text(node):
    if node is None: return ""
    if isinstance(node, str): return node
    if isinstance(node, list): return "\\n".join(_adf_to_text(n) for n in node)
    t = node.get("type", "")
    text = node.get("text")
    if text is not None: return str(text)
    children = node.get("content", [])
    if t in ("doc", "paragraph", "listItem", "blockquote"):
        return " ".join(_adf_to_text(c) for c in children if c)
    if t in ("bulletList", "orderedList"):
        parts = []
        for i, c in enumerate(children, 1):
            prefix = f"{i}. " if t == "orderedList" else "• "
            parts.append(prefix + _adf_to_text(c))
        return "\\n".join(parts)
    if t in ("heading", "codeBlock"):
        return _adf_to_text(children)
    return " ".join(_adf_to_text(c) for c in children if c)
'''

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

JIRA_TOOLS = [
    {
        "name": "jira_create_issue",
        "description": "Create a new Jira issue and return its URL and issue key. Defaults: priority=Medium, issue_type=Bug. Project falls back to the JIRA_PROJECT env var if not provided. Use this for standard issue creation. Prefer jira_create_subtask when creating a child of an existing issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary":     {"type": "string",  "description": "Issue title/summary"},
                "description": {"type": "string",  "description": "Issue body"},
                "project":     {"type": "string",  "description": "Jira project key (e.g. AiNxt). Defaults to JIRA_PROJECT env var."},
                "priority":    {"type": "string",  "description": "Priority level. Pass as a plain string. Valid values: Lowest, Low, Medium, High, Highest, Critical. Default: Medium.", "default": "Medium"},
                "issue_type":  {"type": "string",  "description": "Issue type. Pass as a plain string exactly as shown — no quotes needed. Valid values: Bug, Task, Story, Epic, Subtask, Improvement, New Feature. Note: 'New Feature' has a space but is still passed as a plain string value here (quoting rules only apply inside JQL queries). Default: Bug.", "default": "Bug"},
            },
            "required": ["summary", "description"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        proj        = (inputs.get("project") or _default_project() or "").upper()
        summary     = inputs.get("summary", "")
        description = inputs.get("description", "")
        priority    = inputs.get("priority", "Medium")
        issue_type  = inputs.get("issue_type", "Bug")

        if not proj:
            return {"error": "Project key required. Pass 'project' or set JIRA_PROJECT env var."}

        payload = {
            "fields": {
                "project":     {"key": proj},
                "summary":     summary,
                "description": _adf_text(description),
                "issuetype":   {"name": issue_type},
                "priority":    {"name": priority},
            }
        }
        result    = _request("POST", "/rest/api/3/issue", payload)
        issue_key = result.get("key", "UNKNOWN")
        url       = f"{_jira_base()}/browse/{issue_key}"
        return {"result": url, "issue_key": issue_key, "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "jira_list_issues",
        "description": """List Jira issues for a project, optionally filtered by status.

IMPORTANT DEFAULTS:
  - If 'status' is omitted, ALL issues across ALL statuses are returned.
  - If 'jql' is provided, it overrides both 'project' and 'status' entirely.
  - To fetch all issues and classify them (e.g. by status), omit 'status' or use 'jql'.

WHEN TO USE EACH PARAMETER:
  - Fetch ALL issues in a project          → provide only 'project' (omit 'status')
  - Fetch issues of ONE specific status    → provide 'project' + 'status'
  - Fetch issues of MULTIPLE statuses      → use 'jql' with IN operator
  - Fetch with ANY complex filter          → use 'jql'

JQL QUICK REFERENCE (use when providing the 'jql' parameter):
  Structure : <field> <operator> <value> [AND|OR ...]
  Operators : =, !=, ~, !~, <, >, <=, >=, IN, NOT IN, IS EMPTY, IS NOT EMPTY, WAS, CHANGED
  Keywords  : AND, OR, NOT, ORDER BY <field> ASC|DESC

  Common fields : project, status, priority, assignee, issuetype, labels, component,
                  fixVersion, created, updated, duedate, summary, reporter

  Functions : currentUser(), now(), startOfDay(), endOfDay(),
              startOfWeek(), endOfWeek(), startOfMonth(), endOfMonth(), MEMBERSOF("group")

  Relative dates : -1h, -1d, -7d, -1w, -2w  e.g. created >= -7d

  Status values  : To Do, Open, In Progress, Under Review, Ready for Testing,
                   Testing, Fixed, Done, Closed, Resolved, Reopened
  Priority values: Lowest, Low, Medium, High, Highest, Critical
  Issue types    : Bug, Task, Story, Epic, Subtask, Improvement, "New Feature"
                   (single-word values need no quotes; multi-word values MUST be quoted)

JQL QUOTING RULES:
  - Single-word values : no quotes needed  → issuetype = Bug
  - Multi-word values  : MUST use quotes   → issuetype = "New Feature"
  - Always quote       : multi-word statuses, priorities, or any value containing spaces
  Examples:
    issuetype = Bug                    ✓ single word, no quotes
    issuetype = "New Feature"          ✓ multi-word, quotes required
    issuetype IN (Bug, Task, Story)    ✓ single words in list, no quotes
    issuetype IN (Bug, "New Feature")  ✓ mixed list, quote only multi-word entries
    status = "In Progress"             ✓ multi-word status, quotes required
    status = Done                      ✓ single word, no quotes

JQL EXAMPLES:
  # ALL issues in a project (no status filter) — use this when asked to classify/group by status
  project = "MYPROJ" ORDER BY status ASC

  # Multiple specific statuses
  project = "MYPROJ" AND status IN ("To Do", "In Progress", "Ready for Testing", "Fixed", "Done") ORDER BY status ASC

  # Single status
  project = "MYPROJ" AND status = "In Progress" ORDER BY priority DESC

  # High-priority bugs assigned to me
  project = "MYPROJ" AND issuetype = Bug AND priority = High AND assignee = currentUser()

  # New Feature issues (multi-word type — must quote)
  project = "MYPROJ" AND issuetype = "New Feature" AND status != Done

  # Overdue issues
  project = "MYPROJ" AND duedate < now() AND status != Done

  # Text search in summary
  project = "MYPROJ" AND summary ~ "payment failure" AND status != Done
""",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Jira project key. Used to build the default JQL when 'jql' is not provided."},
                "status":  {"type": "string", "description": "Optional status filter for simple single-status queries. Common values: To Do, In Progress, Under Review, Ready for Testing, Testing, Fixed, Done, Closed. OMIT this field to fetch ALL issues across all statuses. Ignored when 'jql' is provided."},
                "jql":     {"type": "string", "description": "Optional raw JQL query. When provided, overrides 'project' and 'status' entirely. Use for: fetching all issues (no status filter), multiple statuses, priorities, assignees, date ranges, custom fields, etc. Example to fetch all: 'project = \"MYPROJ\" ORDER BY status ASC'. Example for multiple statuses: 'project = \"MYPROJ\" AND status IN (\"To Do\", \"In Progress\", \"Done\") ORDER BY status ASC'"},
            },
            "required": ["project"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        project = inputs.get("project", "").upper()
        status  = inputs.get("status", "")
        jql     = inputs.get("jql", "")
        if not jql:
            if status:
                jql = f\'project = "{project}" AND status = "{status}" ORDER BY created DESC\'
            else:
                jql = f\'project = "{project}" ORDER BY status ASC, created DESC\'
        issues         = []
        next_page_token = None
        page_size       = 50
        while True:
            payload = {
                "jql":       jql,
                "maxResults": page_size,
                "fields":    ["summary", "priority", "status"],
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            result          = _request("POST", "/rest/api/3/search/jql", payload)
            page            = result.get("issues", [])
            issues.extend(page)
            next_page_token = result.get("nextPageToken")
            if not page or not next_page_token:
                break
        if not issues:
            return {"result": f"No issues found for query: {jql}", "issues": []}
        lines = [f"Issues ({len(issues)} found):"]
        items = []
        for issue in issues:
            key         = issue.get("key")
            summary     = issue.get("fields", {}).get("summary", "")
            priority    = (issue.get("fields", {}).get("priority") or {}).get("name", "N/A")
            status_name = (issue.get("fields", {}).get("status") or {}).get("name", "N/A")
            lines.append(f"• [{key}] {summary} (Status: {status_name}, Priority: {priority})")
            items.append({"key": key, "summary": summary, "priority": priority, "status": status_name})
        return {"result": "\\n".join(lines), "issues": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "jira_get_issue",
        "description": "Get full details of a specific Jira issue by key and return a human-readable formatted string. Use this to display issue details to the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key    = (inputs.get("issue_key") or inputs.get("key") or "").strip().upper()
        if not key:
            return {"error": "Missing required Jira issue key. Expected input field: issue_key."}
        result = _request("GET", f"/rest/api/3/issue/{key}")
        fields = result.get("fields", {})
        raw_desc  = fields.get("description") or {}
        desc_text = _adf_to_text(raw_desc) if isinstance(raw_desc, dict) else str(raw_desc or "")
        data = {
            "key":         key,
            "summary":     fields.get("summary", ""),
            "description": desc_text,
            "status":      (fields.get("status") or {}).get("name", ""),
            "priority":    (fields.get("priority") or {}).get("name", ""),
            "assignee":    ((fields.get("assignee") or {}).get("displayName", "Unassigned")),
            "issue_type":  (fields.get("issuetype") or {}).get("name", ""),
            "url":         f"{_jira_base()}/browse/{key}",
        }
        result_str = (
            f"Issue: {data[\'key\']}\\n"
            f"Summary: {data[\'summary\']}\\n"
            f"Status: {data[\'status\']}\\n"
            f"Priority: {data[\'priority\']}\\n"
            f"Assignee: {data[\'assignee\']}\\n"
            f"Description: {data[\'description\']}\\n"
            f"URL: {data[\'url\']}"
        )
        return {"result": result_str, **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "jira_update_issue",
        "description": "Update a Jira issue — transition status, change priority, reassign, or add a comment (multiple changes in one call). Use jira_get_transitions first to discover valid status names for the issue. For adding only a comment, jira_add_comment is a simpler alternative.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key":         {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
                "status":            {"type": "string", "description": "New status name. Common values: To Do, In Progress, Under Review, Testing, Done, Closed, Resolved, Reopened. Must match an available workflow transition — use jira_get_transitions to check valid values for a specific issue."},
                "priority":          {"type": "string", "description": "New priority. Valid values: Lowest, Low, Medium, High, Highest, Critical."},
                "assignee_email_id": {"type": "string", "description": "Email address of new assignee. The tool resolves it to Jira accountId internally."},
                "comment":           {"type": "string", "description": "Comment text to add"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key            = inputs.get("issue_key", "").upper()
        status         = inputs.get("status")
        priority       = inputs.get("priority")
        assignee_email = inputs.get("assignee_email_id")
        comment        = inputs.get("comment")
        results        = []

        if status:
            trans  = _request("GET", f"/rest/api/3/issue/{key}/transitions")
            target = next(
                (t for t in trans.get("transitions", []) if t["name"].lower() == status.lower()),
                None,
            )
            if target:
                _request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": target["id"]}})
                results.append(f"Status → {status}")
            else:
                results.append(f"Status \'{status}\' not found")

        if priority:
            _request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"priority": {"name": priority}}})
            results.append(f"Priority → {priority}")

        if assignee_email:
            assignee_account_id = _account_id_from_email(assignee_email)
            _request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"assignee": {"accountId": assignee_account_id}}})
            results.append(f"Assignee → {assignee_email}")

        if comment:
            _request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _adf_text(comment)})
            results.append("Comment added")

        summary = f"[{key}] Updated: {\', \'.join(results)}" if results else f"[{key}] Nothing to update."
        return {"result": summary, "changes": results}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "jira_add_comment",
        "description": "Post a new comment to a Jira issue. Prefer this over jira_update_issue when only adding a comment — it is simpler and more explicit. To edit an existing comment, use jira_update_comment (you will need the comment_id from jira_list_comments first).",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
                "comment":   {"type": "string", "description": "Comment text"},
            },
            "required": ["issue_key", "comment"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key     = inputs.get("issue_key", "").upper()
        comment = inputs.get("comment", "")
        _request("POST", f"/rest/api/3/issue/{key}/comment", {"body": _adf_text(comment)})
        return {"result": f"Comment added to {key}"}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "jira_link_issues",
        "description": "Link two Jira issues together (e.g. 'relates to', 'blocks', 'is blocked by'). inward_key is the source/blocker issue; outward_key is the target/blocked issue. Use jira_list_link_types to see all valid link type names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "inward_key":  {"type": "string", "description": "Source issue key — the issue that is the blocker or origin of the link (e.g. AiNxt-10)"},
                "outward_key": {"type": "string", "description": "Target issue key — the issue that is blocked or the destination of the link (e.g. AiNxt-20)"},
                "link_type":   {"type": "string", "description": "Link type name e.g. 'relates to', 'blocks', 'is blocked by', 'duplicates'. Use jira_list_link_types to get valid names.", "default": "relates to"},
            },
            "required": ["inward_key", "outward_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        inward    = inputs.get("inward_key", "").upper()
        outward   = inputs.get("outward_key", "").upper()
        link_type = inputs.get("link_type", "relates to")
        _request("POST", "/rest/api/3/issueLink", {
            "type":         {"name": link_type},
            "inwardIssue":  {"key": inward},
            "outwardIssue": {"key": outward},
        })
        return {"result": f"Linked {inward} {link_type} {outward}"}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_recent_changes                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_recent_changes",
        "description": """List issues updated in the last N hours in a project.

For simple filtering, provide 'project' and 'hours'. For dynamic or complex filtering,
provide a raw 'jql' parameter instead — it overrides project+hours when supplied.

JQL QUICK REFERENCE (use when providing the 'jql' parameter):
  Structure : <field> <operator> <value> [AND|OR ...]
  Operators : =, !=, ~, IN, NOT IN, IS EMPTY, WAS, CHANGED AFTER/BEFORE
  Keywords  : AND, OR, NOT, ORDER BY <field> ASC|DESC

  Common fields : project, status, priority, assignee, issuetype, updated, created, duedate

  Functions : currentUser(), now(), startOfDay(), endOfDay(),
              startOfWeek(), endOfWeek(), startOfMonth(), endOfMonth()

  Relative dates : -1h, -6h, -1d, -7d, -1w, -2w, -1m
                   e.g. updated >= -7d  (last 7 days)
                        updated >= -1w  (last 1 week)

  CHANGED operator: status CHANGED AFTER -1w  (issues whose status changed in last week)
  WAS operator    : status WAS "In Progress" AND status != "In Progress"

  Status values  : To Do, Open, In Progress, Under Review, Testing, Done, Closed, Resolved
  Priority values: Lowest, Low, Medium, High, Highest, Critical
  Issue types    : Bug, Task, Story, Epic, Subtask, Improvement, "New Feature"
                   (single-word values need no quotes; multi-word values MUST be quoted)

JQL QUOTING RULES:
  - Single-word values : no quotes needed  → issuetype = Bug
  - Multi-word values  : MUST use quotes   → issuetype = "New Feature"
  - Always quote       : multi-word statuses, priorities, or any value containing spaces
  Examples:
    issuetype = Bug                    ✓ single word, no quotes
    issuetype = "New Feature"          ✓ multi-word, quotes required
    status = "In Progress"             ✓ multi-word status, quotes required
    status = Done                      ✓ single word, no quotes
    priority IN (High, Critical)       ✓ single words in list, no quotes

JQL EXAMPLES:
  # Issues updated in the last week filtered by status
  project = "MYPROJ" AND updated >= -1w AND status = "In Progress" ORDER BY updated DESC

  # High-priority issues changed recently
  project = "MYPROJ" AND updated >= -2d AND priority IN (High, Critical) ORDER BY priority DESC

  # Issues whose status changed in the last week
  project = "MYPROJ" AND status CHANGED AFTER -1w ORDER BY updated DESC

  # Recently updated bugs assigned to me
  project = "MYPROJ" AND updated >= -7d AND issuetype = Bug AND assignee = currentUser()

  # Recently updated New Feature issues (multi-word type — must quote)
  project = "MYPROJ" AND updated >= -7d AND issuetype = "New Feature" ORDER BY updated DESC

  # Issues updated this month
  project = "MYPROJ" AND updated >= startOfMonth() ORDER BY updated DESC
""",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Jira project key. Used to build the default JQL when 'jql' is not provided."},
                "hours":   {"type": "integer", "description": "Look-back window in hours for simple queries. Examples: 1, 6, 24 (default), 48, 72. Ignored when 'jql' is provided.", "default": 24},
                "jql":     {"type": "string", "description": "Optional raw JQL query. When provided, overrides 'project' and 'hours' entirely. Use for complex filters: specific statuses, priorities, issue types, day/week ranges, CHANGED/WAS operators, etc. Example: 'project = \"MYPROJ\" AND updated >= -1w AND status IN (\"In Progress\", \"Under Review\") ORDER BY updated DESC'"},
            },
            "required": ["project"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        project = inputs.get("project", "").upper()
        hours   = int(inputs.get("hours", 24))
        jql     = inputs.get("jql", "")
        if not jql:
            jql = f\'project = "{project}" AND updated >= "-{hours}h" ORDER BY updated DESC\'
        issues          = []
        next_page_token = None
        page_size       = 50
        while True:
            payload = {
                "jql":        jql,
                "maxResults": page_size,
                "fields":     ["summary", "updated", "status", "priority"],
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            result          = _request("POST", "/rest/api/3/search/jql", payload)
            page            = result.get("issues", [])
            issues.extend(page)
            next_page_token = result.get("nextPageToken")
            if not page or not next_page_token:
                break
        if not issues:
            return {"result": f"No issues found for query: {jql}", "issues": []}
        lines = [f"Recent changes ({len(issues)} found):"]
        items = []
        for issue in issues:
            key     = issue.get("key")
            summary = issue.get("fields", {}).get("summary", "")
            updated = issue.get("fields", {}).get("updated", "")[:10]
            status  = (issue.get("fields", {}).get("status") or {}).get("name", "N/A")
            lines.append(f"• [{key}] {summary} (Status: {status}, updated: {updated})")
            items.append({"key": key, "summary": summary, "updated": updated, "status": status})
        return {"result": "\\n".join(lines), "issues": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_search_issues                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_search_issues",
        "description": """Search Jira issues using a raw JQL (Jira Query Language) query.

JQL QUERY STRUCTURE:
  <field> <operator> <value> [AND|OR <field> <operator> <value>]

COMMON FIELDS:
  project, status, priority, assignee, reporter, issuetype, summary,
  description, labels, component, fixVersion, created, updated, duedate, comment

OPERATORS:
  =          Exact match:          project = "MYPROJ"
  !=         Not equal:            assignee != currentUser()
  ~          Contains (text):      summary ~ "login bug"
  !~         Does not contain:     summary !~ "deprecated"
  <, >, <=, >= Comparisons:        duedate < now()
  IN         Match list:           status IN ("In Progress", "Under Review", "Testing")
  NOT IN     Exclude list:         status NOT IN ("Done", "Closed")
  IS EMPTY   Field has no value:   assignee IS EMPTY
  IS NOT EMPTY Field has a value:  duedate IS NOT EMPTY
  WAS        Past state:           status WAS "Resolved"
  CHANGED    Modified in period:   status CHANGED AFTER -1w

KEYWORDS:
  AND   All conditions must be true:  priority = High AND status = Open
  OR    At least one must be true:    component = "UI" OR component = "API"
  NOT   Negate a condition:           NOT status = Done
  ORDER BY  Sort results:             ORDER BY created DESC
             (use ASC or DESC; common sort fields: created, updated, priority, duedate)

FUNCTIONS (dynamic values):
  currentUser()          The currently authenticated user
  now()                  Current date/time
  startOfDay()           Start of today
  endOfDay()             End of today
  startOfWeek()          Start of current week
  endOfWeek()            End of current week
  startOfMonth()         Start of current month
  endOfMonth()           End of current month
  MEMBERSOF("group")     All members of a Jira group

RELATIVE DATE SYNTAX:
  -1d   = last 1 day      -7d  = last 7 days
  -1w   = last 1 week     -2w  = last 2 weeks
  -1h   = last 1 hour     -24h = last 24 hours
  Example: created >= -7d   (issues created in the last 7 days)

COMMON QUERY EXAMPLES:
  # High-priority open issues assigned to me
  priority = High AND assignee = currentUser() AND status != Done

  # All open issues in a project
  project = "MYPROJ" AND status IN ("To Do", "In Progress", "Open")

  # Overdue issues
  project = "MYPROJ" AND duedate < now() AND status != Closed

  # Recently created issues (last 7 days)
  project = "MYPROJ" AND created >= -7d ORDER BY created DESC

  # Issues updated in the last week
  status CHANGED AFTER -1w ORDER BY updated DESC

  # Issues that were resolved but reopened
  status WAS "Resolved" AND status = "Open"

  # Issues assigned to anyone in a group
  assignee IN MEMBERSOF("developers")

  # Issues by type
  issuetype = Epic AND status != Done

  # Search within a custom field
  "Custom Field Name" ~ "search term"

  # Subtask issues assigned to me
  project IN subTaskIssueTypes() AND assignee = currentUser()

  # Issues with a specific label
  project = "MYPROJ" AND labels = "OKR" ORDER BY priority DESC

  # Issues in a specific sprint
  project = "MYPROJ" AND sprint in openSprints()

  # Issues with no assignee
  project = "MYPROJ" AND assignee IS EMPTY

  # Issues by multiple statuses and types
  project = "MYPROJ" AND issuetype IN (Bug, Task, Story) AND status IN ("To Do", "In Progress")

JQL QUOTING RULES — CRITICAL:
  Single-word values : NO quotes needed
    issuetype = Bug                    ✓
    issuetype = Epic                   ✓
    status = Done                      ✓
    priority = High                    ✓

  Multi-word values  : MUST be quoted (any value containing a space)
    issuetype = "New Feature"          ✓  (space → must quote)
    status = "In Progress"             ✓  (space → must quote)
    status = "To Do"                   ✓  (space → must quote)
    status = "Ready for Testing"       ✓  (space → must quote)

  Lists (IN operator): quote only the multi-word entries
    issuetype IN (Bug, Task, Story)              ✓  all single-word, no quotes
    issuetype IN (Bug, "New Feature")            ✓  mixed, quote only multi-word
    status IN ("To Do", "In Progress", Done)     ✓  mixed, quote only multi-word

  WRONG examples (will cause JQL parse error):
    issuetype = New Feature            ✗  missing quotes around multi-word value
    status = In Progress               ✗  missing quotes around multi-word value

TIPS:
  - Use ORDER BY at the end: ... ORDER BY priority DESC, created ASC
  - Combine AND/OR with parentheses for clarity: (status = Open OR status = "In Progress") AND priority = High
  - For text search use ~: summary ~ "payment failure"
  - Project keys don't need quotes if single-word: project = MYPROJ or project = "MYPROJ" both work
""",
        "input_schema": {
            "type": "object",
            "properties": {
                "jql":        {"type": "string",  "description": "JQL query string. Examples: 'project = MYPROJ AND status IN (\"To Do\", \"In Progress\") ORDER BY priority DESC' or 'assignee = currentUser() AND duedate < now() AND status != Done'"},
                "max_results": {"type": "integer", "description": "Max results to return", "default": 150},
            },
            "required": ["jql"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        jql             = inputs.get("jql", "")
        max_results     = int(inputs.get("max_results", 0))  # 0 means fetch all
        issues          = []
        next_page_token = None
        page_size       = 50
        while True:
            payload = {
                "jql":        jql,
                "maxResults": page_size,
                "fields":     ["summary", "status", "priority", "assignee"],
            }
            if next_page_token:
                payload["nextPageToken"] = next_page_token
            result          = _request("POST", "/rest/api/3/search/jql", payload)
            page            = result.get("issues", [])
            issues.extend(page)
            next_page_token = result.get("nextPageToken")
            if not page or not next_page_token:
                break
            if max_results and len(issues) >= max_results:
                issues = issues[:max_results]
                break
        if not issues:
            return {"result": "No issues found.", "issues": []}
        items = []
        lines = [f"JQL results ({len(issues)} found):"]
        for issue in issues:
            key      = issue.get("key")
            summary  = issue.get("fields", {}).get("summary", "")
            status   = (issue.get("fields", {}).get("status") or {}).get("name", "")
            priority = (issue.get("fields", {}).get("priority") or {}).get("name", "")
            lines.append(f"• [{key}] {summary} ({status}, {priority})")
            items.append({"key": key, "summary": summary, "status": status, "priority": priority})
        return {"result": "\\n".join(lines), "issues": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_count_issues                                                    #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_count_issues",
        "description": """Get the total count of Jira issues matching a JQL query — fast, no pagination needed.

Use this tool when the user asks for a COUNT or TOTAL NUMBER of issues (e.g. 'How many bugs are in project X?',
'What is the total count of open issues?'). Do NOT use jira_search_issues just to count — use this instead.

Uses POST /rest/api/3/search/approximate-count which returns a fast approximate count without fetching issues.

JQL QUICK REFERENCE:
  issuetype = Bug                              → count all bugs
  project = "MYPROJ" AND issuetype = Bug       → count bugs in a project
  project = "MYPROJ" AND status = "In Progress"→ count in-progress issues
  project = "MYPROJ" AND priority = High       → count high priority issues
  project = "MYPROJ" AND issuetype = "New Feature" → multi-word type must be quoted

JQL QUOTING RULES:
  Single-word values : no quotes  → issuetype = Bug, status = Done, priority = High
  Multi-word values  : MUST quote → issuetype = "New Feature", status = "In Progress"
""",
        "input_schema": {
            "type": "object",
            "properties": {
                "jql": {"type": "string", "description": "JQL query to count matching issues. Examples: 'project = \"MYPROJ\" AND issuetype = Bug' or 'project = \"MYPROJ\" AND status = \"In Progress\"'"},
            },
            "required": ["jql"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        jql    = inputs.get("jql", "")
        result = _request("POST", "/rest/api/3/search/approximate-count", {"jql": jql})
        count  = result.get("count", 0)
        return {"result": f"Total issues matching '{jql}': {count}", "count": count}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_create_subtask                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_create_subtask",
        "description": "Create a Subtask issue linked to a parent Jira issue. Returns the new subtask URL and issue key. The project key is inferred from parent_key (e.g. AiNxt from AiNxt-123) if not explicitly provided and JIRA_PROJECT env var is also unset. description is optional. Use this instead of jira_create_issue when the new issue must be a child of an existing issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "parent_key":  {"type": "string", "description": "Parent issue key e.g. AiNxt-123"},
                "summary":     {"type": "string", "description": "Subtask title"},
                "description": {"type": "string", "description": "Subtask body"},
                "project":     {"type": "string", "description": "Jira project key. Defaults to JIRA_PROJECT env var, or inferred from parent_key if neither is set."},
            },
            "required": ["parent_key", "summary"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        parent_key  = inputs.get("parent_key", "").upper()
        summary     = inputs.get("summary", "")
        description = inputs.get("description", "")
        proj        = (inputs.get("project") or _default_project() or "").upper()
        if not proj:
            proj = parent_key.split("-")[0]
        payload = {
            "fields": {
                "project":     {"key": proj},
                "parent":      {"key": parent_key},
                "summary":     summary,
                "description": _adf_text(description),
                "issuetype":   {"name": "Subtask"},
            }
        }
        result    = _request("POST", "/rest/api/3/issue", payload)
        issue_key = result.get("key", "UNKNOWN")
        url       = f"{_jira_base()}/browse/{issue_key}"
        return {"result": url, "issue_key": issue_key, "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_get_transitions                                                 #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_get_transitions",
        "description": "Get the list of available workflow transitions (status changes) for a specific Jira issue. Returns each transition's id and name. Call this before jira_update_issue (when changing status) to discover which status names are valid for that issue's current workflow — valid transitions vary by project and current status. Do not guess status names; always verify with this tool first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key    = inputs.get("issue_key", "").upper()
        result = _request("GET", f"/rest/api/3/issue/{key}/transitions")
        trans  = result.get("transitions", [])
        items  = [{"id": t["id"], "name": t["name"]} for t in trans]
        names  = [t["name"] for t in trans]
        return {"result": f"Transitions for {key}: {', '.join(names)}", "transitions": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_comments                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_comments",
        "description": "List all comments on a Jira issue (up to 150). Returns each comment's id, author display name, and body text (truncated to 300 characters). Use the comment id returned here as the comment_id input for jira_update_comment. Call this first when you need to read, reference, or edit existing comments on an issue.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key    = inputs.get("issue_key", "").upper()
        result = _request("GET", f"/rest/api/3/issue/{key}/comment?maxResults=150")
        comments = result.get("comments", [])
        if not comments:
            return {"result": f"No comments on {key}.", "comments": []}
        items = []
        lines = [f"Comments on {key}:"]
        for c in comments:
            author = (c.get("author") or {}).get("displayName", "?")
            body   = _adf_to_text(c.get("body", {}))[:300]
            cid    = c.get("id", "?")
            lines.append(f"• [{cid}] {author}: {body}")
            items.append({"id": cid, "author": author, "body": body})
        return {"result": "\\n".join(lines), "comments": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_update_comment                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_update_comment",
        "description": "Replace the body of an existing comment on a Jira issue. Requires the comment_id, which must be obtained first by calling jira_list_comments. To add a new comment instead, use jira_add_comment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key":  {"type": "string", "description": "Jira issue key"},
                "comment_id": {"type": "string", "description": "Comment ID"},
                "comment":    {"type": "string", "description": "New comment text"},
            },
            "required": ["issue_key", "comment_id", "comment"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key        = inputs.get("issue_key", "").upper()
        comment_id = inputs.get("comment_id", "")
        comment    = inputs.get("comment", "")
        _request("PUT", f"/rest/api/3/issue/{key}/comment/{comment_id}", {"body": _adf_text(comment)})
        return {"result": f"Comment {comment_id} on {key} updated."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_attachments                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_attachments",
        "description": "List all file attachments on a Jira issue. Returns each attachment's id, filename, and size in bytes. Use this to inspect what files are attached to an issue before referencing or downloading them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key    = inputs.get("issue_key", "").upper()
        result = _request("GET", f"/rest/api/3/issue/{key}?fields=attachment")
        attachments = (result.get("fields") or {}).get("attachment", [])
        if not attachments:
            return {"result": f"No attachments on {key}.", "attachments": []}
        items = []
        lines = [f"Attachments on {key}:"]
        for a in attachments:
            aid      = a.get("id", "?")
            filename = a.get("filename", "?")
            size     = a.get("size", 0)
            lines.append(f"• [{aid}] {filename} ({size} bytes)")
            items.append({"id": aid, "filename": filename, "size": size})
        return {"result": "\\n".join(lines), "attachments": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_watchers                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_watchers",
        "description": "List all users watching a Jira issue. Returns a list of display names and email addresses. Call this to check who is currently watching an issue, or before calling jira_remove_watcher to confirm the user is actually a watcher.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key": {"type": "string", "description": "Jira issue key"},
            },
            "required": ["issue_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key    = inputs.get("issue_key", "").upper()
        result = _request("GET", f"/rest/api/3/issue/{key}/watchers")
        watchers = result.get("watchers", [])
        names    = [(w.get("displayName") or w.get("emailAddress", "?")) for w in watchers]
        return {"result": f"Watchers on {key}: {', '.join(names) or 'none'}", "watchers": names}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_add_watcher                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_add_watcher",
        "description": "Add a watcher to a Jira issue by their email address. The tool resolves the email to the Atlassian account ID internally.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key":         {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
                "assignee_email_id": {"type": "string", "description": "Email address of the user to add as a watcher"},
            },
            "required": ["issue_key", "assignee_email_id"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        key        = inputs.get("issue_key", "").upper()
        email      = inputs.get("assignee_email_id", "")
        account_id = _account_id_from_email(email)
        _request("POST", f"/rest/api/3/issue/{key}/watchers", account_id)
        return {"result": f"Watcher {email} added to {key}."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_remove_watcher                                                  #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_remove_watcher",
        "description": "Remove a watcher from a Jira issue by their email address. The tool resolves the email to the Atlassian account ID internally.",
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_key":         {"type": "string", "description": "Jira issue key e.g. AiNxt-123"},
                "assignee_email_id": {"type": "string", "description": "Email address of the watcher to remove"},
            },
            "required": ["issue_key", "assignee_email_id"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        import urllib.parse
        key        = inputs.get("issue_key", "").upper()
        email      = inputs.get("assignee_email_id", "")
        account_id = _account_id_from_email(email)
        _request("DELETE", f"/rest/api/3/issue/{key}/watchers?accountId={urllib.parse.quote(account_id)}")
        return {"result": f"Watcher {email} removed from {key}."}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_link_types                                                 #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_link_types",
        "description": "List all available issue link type names in Jira (e.g. 'relates to', 'blocks', 'is blocked by', 'duplicates', 'clones'). Returns each type's id, name, inward label, and outward label. Call this before jira_link_issues when you are unsure of the exact link type name — the name must match exactly.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        result = _request("GET", "/rest/api/3/issueLinkType")
        types  = result.get("issueLinkTypes", [])
        items  = [{"id": t["id"], "name": t["name"], "inward": t.get("inward"), "outward": t.get("outward")} for t in types]
        names  = [t["name"] for t in types]
        return {"result": f"Link types: {', '.join(names)}", "link_types": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_projects                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_projects",
        "description": "List all Jira projects accessible to the authenticated user (up to 150). Returns each project's key and name. Use project keys from this list as the 'project' input for all other project-scoped tools (jira_list_issues, jira_create_issue, etc.). Call this when you don't know the project key.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        result   = _request("GET", "/rest/api/3/project/search?maxResults=150")
        projects = result.get("values", [])
        if not projects:
            return {"result": "No projects found.", "projects": []}
        items = [{"key": p["key"], "name": p["name"]} for p in projects]
        lines = ["Projects:"] + [f"• [{p['key']}] {p['name']}" for p in projects]
        return {"result": "\\n".join(lines), "projects": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_get_project                                                     #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_get_project",
        "description": "Get details of a specific Jira project by its key. Returns key, name, description (truncated to 300 chars), lead (project lead display name), and self URL. Use this when you need project metadata beyond what jira_list_projects returns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_key": {"type": "string", "description": "Jira project key"},
            },
            "required": ["project_key"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        proj   = inputs.get("project_key", "").upper()
        result = _request("GET", f"/rest/api/3/project/{proj}")
        data   = {
            "key":         result.get("key"),
            "name":        result.get("name"),
            "description": (result.get("description") or "")[:300],
            "lead":        (result.get("lead") or {}).get("displayName", ""),
            "url":         result.get("self", ""),
        }
        return {"result": f"Project {data['key']}: {data['name']} (Lead: {data['lead']})", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_get_current_user                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_get_current_user",
        "description": "Get the profile of the currently authenticated Jira user.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        result = _request("GET", "/rest/api/3/myself")
        data   = {
            "account_id":    result.get("accountId"),
            "display_name":  result.get("displayName"),
            "email":         result.get("emailAddress", ""),
            "active":        result.get("active", True),
        }
        return {"result": f"Current user: {data['display_name']} ({data['email']})", **data}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_search_users                                                    #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_search_users",
        "description": "Search for Jira users by name or email address. Returns each matching user's accountId, displayName, and emailAddress. Use this to look up a user before assigning an issue (jira_update_issue), adding a watcher (jira_add_watcher), or filtering issues by assignee in JQL. The tool resolves email to accountId internally in most tools, but this is useful for verification or when building JQL with specific assignees.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name or email to search"},
            },
            "required": ["query"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        import urllib.parse
        query  = inputs.get("query", "")
        result = _request("GET", f"/rest/api/3/user/search?query={urllib.parse.quote(query)}&maxResults=150")
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        items = [{"account_id": u.get("accountId"), "display_name": u.get("displayName"), "email": u.get("emailAddress", "")} for u in result]
        lines = [f"Users matching '{query}':"] + [f"• {u['display_name']} ({u['email']})" for u in items]
        return {"result": "\\n".join(lines), "users": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # jira_list_issue_types                                                #
    # ------------------------------------------------------------------ #
    {
        "name": "jira_list_issue_types",
        "description": "List all issue types available in Jira. Returns each type's id, name, and subtask flag (true if it is a subtask type). Use this to discover valid issue_type values before calling jira_create_issue or jira_create_subtask, especially when the project may have custom issue types beyond the standard set (Bug, Task, Story, Epic, Subtask, Improvement, New Feature).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        result = _request("GET", "/rest/api/3/issuetype")
        if not isinstance(result, list):
            return {"error": f"Unexpected response: {result}"}
        items = [{"id": t["id"], "name": t["name"], "subtask": t.get("subtask", False)} for t in result]
        names = [t["name"] for t in result]
        return {"result": f"Issue types: {', '.join(names)}", "issue_types": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
