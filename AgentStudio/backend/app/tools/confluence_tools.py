# SPDX-License-Identifier: MIT
"""
Confluence tools — ported from AiNxt Agentic Platform tools/confluence_tools.py.

Credentials come from env vars: CONFLUENCE_URL, CONFLUENCE_SPACE_KEY,
CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN.
Each tool's `code` string is self-contained and runs in the sandbox subprocess.

NOTE: These tools are marked `"draft": True` — they are present in the catalog
but will NOT be seeded into the database until the Confluence integration is
configured and the draft flag is removed.
"""

_HELPERS = '''
import os, json, base64, ssl, urllib.request, urllib.error, urllib.parse, re

def _conf_base():
    return os.environ.get("CONFLUENCE_URL", "").rstrip("/")

def _conf_space():
    return os.environ.get("CONFLUENCE_SPACE_KEY", "")

_CONF_NOT_CONFIGURED = (
    "You have not configured an Atlassian (Confluence) API token. "
    "Add it under Profile \u2192 Atlassian Token, then retry. "
    "(The platform does not use a shared/service Confluence account.)"
)

def _auth_header():
    # Uses the user's own Atlassian token. CONFLUENCE_* is preferred; JIRA_* is
    # the same per-user Atlassian credential (Confluence + Jira share one
    # Atlassian account) and is NOT a platform/service fallback.
    email = os.environ.get("CONFLUENCE_EMAIL", "") or os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN", "") or os.environ.get("JIRA_API_TOKEN", "")
    if not email or not token:
        raise PermissionError(_CONF_NOT_CONFIGURED)
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {creds}"

def _conf_http_message(code, body=""):
    """Map a Confluence HTTP status to a clear, actionable message."""
    if code == 401:
        return ("Confluence token is invalid or expired. "
                "Update it under Profile \u2192 Atlassian Token.")
    if code == 403:
        return ("Your Confluence token does not have permission for this space or action. "
                "Ask a Confluence admin for access.")
    if code == 404:
        return ("Confluence page/space not found, or your token has no access to it.")
    return f"HTTP {code}: {str(body)[:400]}"

def _request(method, path, body=None):
    import ssl
    # Security review F-07: TLS certificate verification is always enforced
    # (CWE-599). When REQUESTS_CA_BUNDLE / SSL_CERT_FILE points at a CA bundle
    # (a corporate CA, or the cert of a TLS-terminating proxy) we verify against
    # it; otherwise we verify against the system trust store. There is
    # deliberately no unverified fallback — an environment without a usable
    # trust anchor must be provisioned with one rather than silently accepting
    # forged certificates on a path that carries an API token.
    _ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE") or ""
    ctx = ssl.create_default_context(cafile=_ca_bundle or None)

    # ── LLM proxy path (production: gateway → LLM proxy → Atlassian Cloud) ──────────
    llm_proxy_url = os.environ.get("LLM_PROXY_URL", "").rstrip("/")
    if llm_proxy_url:
        email = os.environ.get("CONFLUENCE_EMAIL", "") or os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("CONFLUENCE_API_TOKEN", "") or os.environ.get("JIRA_API_TOKEN", "")
        if not email or not token:
            raise PermissionError(_CONF_NOT_CONFIGURED)
        proxy_payload = {
            "service": "confluence",
            "method":  method,
            "path":    path,
            "body":    body,
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
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        try:
            # Enterprise-grade timeout: 90s tolerates Atlassian Cloud latency
            # spikes when this call is chained from a generate-with-AI step
            # or an attached workflow.
            with opener.open(proxy_req, timeout=90) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")
            raise Exception(_conf_http_message(e.code, body_text))
        except urllib.error.URLError as e:
            raise Exception(f"Confluence proxy unreachable: {e.reason}")

    # ── Direct path (local dev: call Confluence directly) ─────────────────────
    url  = f"{_conf_base()}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Authorization": _auth_header(),
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        },
        method=method,
    )
    try:
        # Enterprise-grade timeout: 60s (was 20s). Confluence page
        # create/update under load can take 30s+; this keeps tool calls
        # resilient inside attached and AI-generation flows.
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise Exception(_conf_http_message(e.code, body_text))
    except urllib.error.URLError as e:
        raise Exception(f"Confluence unreachable: {e.reason}")

def _md_to_storage(md):
    """Convert basic Markdown to Confluence XHTML storage format."""
    lines   = md.split("\\n")
    out     = []
    in_code = False
    for line in lines:
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                lang = line[3:].strip() or "none"
                out.append(f\'<pre><code class="language-{lang}">\')
                in_code = True
            continue
        if in_code:
            out.append(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue
        m = re.match(r"^(#{1,6})\\s+(.*)", line)
        if m:
            lvl  = len(m.group(1))
            text = m.group(2).strip()
            out.append(f"<h{lvl}>{text}</h{lvl}>")
            continue
        if line.startswith("- ") or line.startswith("* "):
            out.append(f"<ul><li>{line[2:]}</li></ul>")
            continue
        line = re.sub(r"\\*\\*(.+?)\\*\\*", r"<strong>\\1</strong>", line)
        line = re.sub(r"\\*(.+?)\\*",       r"<em>\\1</em>",         line)
        line = re.sub(r"`(.+?)`",           r"<code>\\1</code>",     line)
        if line.strip():
            out.append(f"<p>{line}</p>")
        else:
            out.append("<p></p>")
    return "\\n".join(out)
'''

CONFLUENCE_TOOLS = [
    {
        "name": "confluence_create_page",
        "draft": True,
        "description": "Create a new Confluence page with a markdown body. Returns the page URL.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":     {"type": "string", "description": "Page title"},
                "body":      {"type": "string", "description": "Page content in Markdown"},
                "space_key": {"type": "string", "description": "Confluence space key. Defaults to CONFLUENCE_SPACE_KEY env var."},
                "parent_id": {"type": "string", "description": "Parent page ID (optional)"},
            },
            "required": ["title", "body"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        title     = inputs.get("title", "")
        body      = inputs.get("body", "")
        space_key = inputs.get("space_key") or _conf_space()
        parent_id = inputs.get("parent_id")
        if not space_key:
            return {"error": "space_key required. Pass it or set CONFLUENCE_SPACE_KEY env var."}
        storage = _md_to_storage(body)
        payload = {
            "type":  "page",
            "title": title,
            "space": {"key": space_key},
            "body":  {"storage": {"value": storage, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]
        result  = _request("POST", "/rest/api/content", payload)
        page_id = result.get("id", "?")
        url     = f"{_conf_base()}/pages/{page_id}"
        return {"result": url, "page_id": page_id, "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "confluence_update_page",
        "draft": True,
        "description": "Update an existing Confluence page. Auto-increments the version.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Confluence page ID"},
                "title":   {"type": "string", "description": "New page title"},
                "body":    {"type": "string", "description": "New page content in Markdown"},
            },
            "required": ["page_id", "title", "body"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        page_id = inputs.get("page_id", "")
        title   = inputs.get("title", "")
        body    = inputs.get("body", "")
        current = _request("GET", f"/rest/api/content/{page_id}?expand=version")
        version = current.get("version", {}).get("number", 1) + 1
        storage = _md_to_storage(body)
        payload = {
            "type":    "page",
            "title":   title,
            "version": {"number": version},
            "body":    {"storage": {"value": storage, "representation": "storage"}},
        }
        result = _request("PUT", f"/rest/api/content/{page_id}", payload)
        url    = f"{_conf_base()}/pages/{page_id}"
        return {"result": f"Page {page_id} updated (v{version}): {url}", "page_id": page_id, "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "confluence_get_page",
        "draft": True,
        "description": "Get a Confluence page by ID. Returns title, URL, version, and a content excerpt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string", "description": "Confluence page ID"},
            },
            "required": ["page_id"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        page_id = inputs.get("page_id", "")
        result  = _request("GET", f"/rest/api/content/{page_id}?expand=body.storage,version")
        title   = result.get("title", "")
        version = result.get("version", {}).get("number", 1)
        storage = result.get("body", {}).get("storage", {}).get("value", "")
        excerpt = re.sub(r"<[^>]+>", "", storage)[:500]
        url     = f"{_conf_base()}/pages/{page_id}"
        return {
            "result":  f"{title} (v{version}): {url}\\n\\n{excerpt}",
            "page_id": page_id,
            "title":   title,
            "version": version,
            "url":     url,
            "excerpt": excerpt,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "confluence_search",
        "draft": True,
        "description": "Search Confluence pages using CQL. Returns a list of matching pages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string", "description": "Search query text"},
                "space_key": {"type": "string", "description": "Limit search to this space (optional)"},
                "limit":     {"type": "integer", "description": "Max results", "default": 10},
            },
            "required": ["query"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        query     = inputs.get("query", "")
        space_key = inputs.get("space_key") or _conf_space()
        limit     = int(inputs.get("limit", 10))
        cql       = f\'text ~ "{query}" AND type = page\'
        if space_key:
            cql += f\' AND space = "{space_key}"\'
        encoded = urllib.parse.quote(cql)
        result  = _request("GET", f"/rest/api/content/search?cql={encoded}&limit={limit}&expand=space")
        pages   = result.get("results", [])
        if not pages:
            return {"result": f"No pages found for: {query}", "pages": []}
        items = []
        lines = [f"Confluence search results for \'{query}\':"]
        for p in pages:
            pid   = p.get("id", "?")
            title = p.get("title", "?")
            url   = f"{_conf_base()}/pages/{pid}"
            lines.append(f"• [{pid}] {title} — {url}")
            items.append({"page_id": pid, "title": title, "url": url})
        return {"result": "\\n".join(lines), "pages": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    {
        "name": "confluence_get_page_by_title",
        "draft": True,
        "description": "Find a Confluence page by its exact title within a space.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title":     {"type": "string", "description": "Exact page title"},
                "space_key": {"type": "string", "description": "Confluence space key. Defaults to CONFLUENCE_SPACE_KEY env var."},
            },
            "required": ["title"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        title     = inputs.get("title", "")
        space_key = inputs.get("space_key") or _conf_space()
        cql       = f\'title = "{title}" AND type = page\'
        if space_key:
            cql += f\' AND space = "{space_key}"\'
        encoded = urllib.parse.quote(cql)
        result  = _request("GET", f"/rest/api/content/search?cql={encoded}&limit=5")
        pages   = result.get("results", [])
        if not pages:
            return {"error": f"No page found with title: {title}"}
        p     = pages[0]
        pid   = p.get("id", "?")
        url   = f"{_conf_base()}/pages/{pid}"
        return {"result": f"Found: [{pid}] {p.get(\'title\')} — {url}", "page_id": pid, "title": p.get("title"), "url": url}
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
