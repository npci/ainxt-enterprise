# SPDX-License-Identifier: MIT
# ============================================================
# CONFLUENCE TOOLS
# Confluence Cloud REST API v2
#
# Env vars:
#   CONFLUENCE_URL        — https://your-org.atlassian.net/wiki
#   CONFLUENCE_SPACE_KEY  — default space (e.g. "AiNxt" or "ENG")
# ============================================================

import os
import json
import base64
import urllib.request
import urllib.parse
from typing import Optional

from core.logger import logger

# ── Env helpers (read lazily so .env loaded by gateway takes effect) ──

def _conf_base()  -> str: return os.getenv("CONFLUENCE_URL",       "").rstrip("/")
def _conf_space() -> str: return os.getenv("CONFLUENCE_SPACE_KEY",  "ENG")

# ── Auth helpers ──────────────────────────────────────────────

def _auth_header(email: str, token: str) -> dict:
    """Build Basic auth header from the caller-supplied user credentials."""
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    }


def _auth_for_user(user_id: str = "", user_email: str = "") -> tuple:
    """Return (auth_email, api_token) from the user's stored profile token.

    Raises PermissionError if the user has no stored Atlassian token.
    Service-account credentials are never used.
    """
    from core.platform_credentials import get_atlassian_creds
    return get_atlassian_creds(user_id=user_id, email=user_email)


def _do_request_direct(method: str, url: str, data: Optional[bytes],
                       auth_email: str = "", auth_token: str = "") -> dict:
    """Raw HTTP call to Confluence — used only in local dev (no LLM_PROXY_URL)."""
    req = urllib.request.Request(
        url, data=data,
        headers=_auth_header(email=auth_email, token=auth_token),
        method=method,
    )
    try:
        r = urllib.request.urlopen(req, timeout=15)
        try:
            return json.loads(r.read().decode())
        finally:
            r.close()
    except urllib.error.HTTPError as e:
        code = e.code
        msg  = e.read().decode()
        logger.error(f"Confluence {method} {url} → {code}: {msg[:200]}")
        if code in (400, 401, 403, 404, 422):
            raise urllib.error.HTTPError(url, code, msg, {}, None)
        raise RuntimeError(f"HTTP {code}: {msg[:200]}")


def _request(method: str, path: str, body: Optional[dict] = None,
             auth_email: str = "", auth_token: str = "") -> dict:
    """
    Send a Confluence REST API call.

    In production (LLM_PROXY_URL set): routes through the LLM proxy server LLM proxy.
    Confluence is Atlassian Cloud — only reachable from the LLM proxy server, not from the gateway.

    In local dev (LLM_PROXY_URL unset): calls Confluence directly for convenience.
    """
    if not auth_email or not auth_token:
        raise PermissionError(
            "No Atlassian personal access token found for this user. "
            "Please add your Atlassian token under Profile → Atlassian Token before accessing Confluence."
        )
    e = auth_email
    t = auth_token

    from core.circuit_breaker import get_breaker
    from core.retry import retry_llm

    proxy_url = os.getenv("LLM_PROXY_URL", "").rstrip("/")

    if proxy_url:
        # ── Production path: relay through the LLM proxy server LLM proxy ──
        import httpx

        full_path = f"/rest/api/content{path}"
        req_body: dict = {"service": "confluence", "method": method, "path": full_path}
        if body is not None:
            req_body["body"] = body
        if e:
            req_body["email"] = e
        if t:
            req_body["token"] = t

        # ── Correlation ID propagation ─────────────────────────────────────────
        # Inject request_id / chat_id from thread-local logger context so that
        # the llm_proxy service logs every Confluence API call under the same
        # identifiers as the originating gateway /ask or SDLC pipeline request.
        try:
            from core.logger import get_request_id, get_chat_id
            _rid = get_request_id()
            _cid = get_chat_id()
            if _rid and _rid != "-":
                req_body["request_id"] = _rid
            if _cid and _cid != "-":
                req_body["chat_id"] = _cid
        except Exception:
            pass
        # ──────────────────────────────────────────────────────────────────────

        def _do_proxy():
            from core.proxy_tool_use import llm_proxy_headers as _lph
            resp = httpx.post(
                f"{proxy_url}/atlassian/proxy",
                json=req_body,
                headers=_lph(),
                timeout=30.0,
            )
            if resp.status_code in (400, 401, 403, 404, 422):
                raise urllib.error.HTTPError(
                    full_path, resp.status_code, resp.text[:200], {}, None
                )
            if not resp.is_success:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()

        try:
            return get_breaker("confluence").call(
                lambda: retry_llm(_do_proxy, max_attempts=3, base_delay=1.0)
            )
        except urllib.error.HTTPError as http_err:
            return {"error": f"HTTP {http_err.code}: {http_err.reason}"}
        except RuntimeError as rt_err:
            return {"error": str(rt_err)}
        except Exception as exc:
            logger.error(f"Confluence request failed: {exc}")
            return {"error": str(exc)}

    else:
        # ── Local dev fallback: call Confluence directly ──
        base = _conf_base()
        if not base or not e or not t:
            return {"error": "Confluence credentials not configured (CONFLUENCE_URL/EMAIL/API_TOKEN)"}
        url  = f"{base}/rest/api/content{path}"
        data = json.dumps(body).encode() if body else None

        try:
            return get_breaker("confluence").call(
                lambda: retry_llm(
                    lambda: _do_request_direct(method, url, data, auth_email=e, auth_token=t),
                    max_attempts=3, base_delay=1.0,
                )
            )
        except urllib.error.HTTPError as http_err:
            return {"error": f"HTTP {http_err.code}: {http_err.reason}"}
        except RuntimeError as rt_err:
            return {"error": str(rt_err)}
        except Exception as exc:
            logger.error(f"Confluence request failed: {exc}")
            return {"error": str(exc)}


# ── Markdown → Confluence storage format (basic) ──────────────

def _md_to_storage(markdown: str) -> str:
    """Convert markdown to Confluence XHTML storage format (simplified)."""
    import re
    # Escape bare & FIRST (markdown doesn't use & for syntax).
    # Must happen before we inject any HTML tags so we don't double-escape.
    html = (markdown or "").replace("&", "&amp;")
    # Headers
    html = re.sub(r"^### (.+)$", r"<h3>\1</h3>", html, flags=re.MULTILINE)
    html = re.sub(r"^## (.+)$",  r"<h2>\1</h2>", html, flags=re.MULTILINE)
    html = re.sub(r"^# (.+)$",   r"<h1>\1</h1>", html, flags=re.MULTILINE)
    # Bold / italic
    html = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html)
    html = re.sub(r"\*(.+?)\*",     r"<em>\1</em>",         html)
    # Code blocks
    html = re.sub(
        r"```(\w+)?\n(.*?)```",
        r'<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">\1</ac:parameter><ac:plain-text-body><![CDATA[\2]]></ac:plain-text-body></ac:structured-macro>',
        html, flags=re.DOTALL
    )
    # Inline code
    html = re.sub(r"`(.+?)`", r"<code>\1</code>", html)
    # Bullet lists
    lines = html.split("\n")
    out   = []
    in_ul = False
    for line in lines:
        if line.startswith("- ") or line.startswith("* "):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{line[2:]}</li>")
        else:
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(line)
    if in_ul:
        out.append("</ul>")
    html = "\n".join(out)
    # Paragraphs
    paragraphs = [p.strip() for p in html.split("\n\n") if p.strip()]
    result     = []
    for p in paragraphs:
        if p.startswith("<h") or p.startswith("<ul") or p.startswith("<ac:"):
            result.append(p)
        else:
            result.append(f"<p>{p}</p>")
    final = "\n".join(result)
    # Escape bare < and > inside <p> and <li> text content only.
    # Inline tags <strong>, <em>, <code> are kept; everything else is escaped.
    _INLINE_TAG = re.compile(r'</?(?:strong|em|code)\b[^>]*>', re.IGNORECASE)

    def _escape_text(text: str) -> str:
        """Escape < and > that are not part of inline HTML tags."""
        parts_out = []
        last = 0
        for m in _INLINE_TAG.finditer(text):
            chunk = text[last:m.start()]
            parts_out.append(chunk.replace("<", "&lt;").replace(">", "&gt;"))
            parts_out.append(m.group(0))   # keep the inline tag as-is
            last = m.end()
        parts_out.append(text[last:].replace("<", "&lt;").replace(">", "&gt;"))
        return "".join(parts_out)

    # Apply to content inside <p>...</p> and <li>...</li>
    final = re.sub(r'(?<=<p>)(.*?)(?=</p>)',  lambda m: _escape_text(m.group(1)), final, flags=re.DOTALL)
    final = re.sub(r'(?<=<li>)(.*?)(?=</li>)', lambda m: _escape_text(m.group(1)), final, flags=re.DOTALL)
    return final


# ── Public API ────────────────────────────────────────────────

def confluence_create_page(
        title:       str,
        body:        str,
        space_key:   str = "",
        parent_id:   Optional[str] = None,
        user_id:     str = "",
        user_email:  str = "",
        repo_name:   str = "",
) -> str:
    """
    Create a Confluence page with the given title and markdown body.
    Returns JSON string with page_id and url.

    space_key resolution:
      1. Explicit ``space_key`` arg
      2. Product linked to ``repo_name`` (product_repos → products.confluence_space)
      3. CONFLUENCE_SPACE_KEY env var

    Auth resolution: user's stored Atlassian token; raises PermissionError if not found.
    """
    if not space_key and repo_name:
        try:
            from core.platform_credentials import get_product_for_repo
            ctx = get_product_for_repo(repo_name)
            space_key = ctx.get("confluence_space", "")
        except Exception:
            pass
    space = space_key or _conf_space()
    try:
        from core.prompt_sanitizer import sanitize as _san
        title = _san(title)
        body  = _san(body)
    except Exception:
        pass
    storage = _md_to_storage(body)

    auth_email, auth_token = _auth_for_user(user_id, user_email)

    payload = {
        "type":  "page",
        "title": title,
        "space": {"key": space},
        "body":  {
            "storage": {
                "value":          storage,
                "representation": "storage",
            }
        },
    }
    if parent_id:
        payload["ancestors"] = [{"id": parent_id}]

    result = _request("POST", "", payload, auth_email=auth_email, auth_token=auth_token)
    if "error" in result:
        return json.dumps(result)

    page_id  = result.get("id", "")
    web_link = result.get("_links", {}).get("webui", "")
    full_url = f"{_conf_base()}{web_link}" if web_link and not web_link.startswith("http") else web_link

    logger.info(f"Confluence page created: {title} → {full_url}")
    return json.dumps({"page_id": page_id, "url": full_url, "title": title, "space": space})


def confluence_update_page(
        page_id:    str,
        title:      str,
        body:       str,
        user_id:    str = "",
        user_email: str = "",
) -> str:
    """
    Update an existing Confluence page (increments version automatically).
    Returns JSON string with page_id and url.
    """
    auth_email, auth_token = _auth_for_user(user_id, user_email)

    # Fetch current version
    current = _request("GET", f"/{page_id}?expand=version",
                       auth_email=auth_email, auth_token=auth_token)
    if "error" in current:
        return json.dumps(current)

    version_number = current.get("version", {}).get("number", 1) + 1
    storage        = _md_to_storage(body)

    payload = {
        "type":    "page",
        "title":   title,
        "version": {"number": version_number},
        "body":    {
            "storage": {
                "value":          storage,
                "representation": "storage",
            }
        },
    }

    result   = _request("PUT", f"/{page_id}", payload, auth_email=auth_email, auth_token=auth_token)
    if "error" in result:
        return json.dumps(result)

    web_link = result.get("_links", {}).get("webui", "")
    full_url = f"{_conf_base()}{web_link}" if web_link and not web_link.startswith("http") else web_link

    logger.info(f"Confluence page updated: {page_id} → {title}")
    return json.dumps({"page_id": page_id, "url": full_url, "title": title, "version": version_number})


def confluence_get_page(page_id: str, user_id: str = "", user_email: str = "") -> str:
    """
    Retrieve a Confluence page by ID.
    Returns JSON string with title, url, and body excerpt.
    """
    auth_email, auth_token = _auth_for_user(user_id, user_email)
    result = _request("GET", f"/{page_id}?expand=body.storage,version",
                      auth_email=auth_email, auth_token=auth_token)
    if "error" in result:
        return json.dumps(result)

    web_link = result.get("_links", {}).get("webui", "")
    full_url = f"{_conf_base()}{web_link}" if web_link and not web_link.startswith("http") else web_link
    body_val = result.get("body", {}).get("storage", {}).get("value", "")[:500]

    return json.dumps({
        "page_id": page_id,
        "title":   result.get("title", ""),
        "url":     full_url,
        "version": result.get("version", {}).get("number", 1),
        "excerpt": body_val,
    })


def confluence_search(query: str, space_key: str = "",
                      user_id: str = "", user_email: str = "") -> str:
    """
    Search Confluence using CQL.
    Returns JSON string with list of matching pages.
    """
    auth_email, auth_token = _auth_for_user(user_id, user_email)
    space  = space_key or _conf_space()
    cql    = f'type=page AND space="{space}" AND text~"{query}"'
    params = urllib.parse.urlencode({"cql": cql, "limit": 10})
    result = _request("GET", f"?{params}", auth_email=auth_email, auth_token=auth_token)

    if "error" in result:
        return json.dumps(result)

    pages = []
    for item in result.get("results", []):
        web_link = item.get("_links", {}).get("webui", "")
        full_url = f"{_conf_base()}{web_link}" if web_link and not web_link.startswith("http") else web_link
        pages.append({
            "page_id": item.get("id", ""),
            "title":   item.get("title", ""),
            "url":     full_url,
        })

    return json.dumps({"results": pages, "total": len(pages), "query": query})


def confluence_get_page_by_title(title: str, space_key: str = "",
                                 user_id: str = "", user_email: str = "") -> str:
    """
    Find a Confluence page by exact title within a space.
    Returns JSON string with page_id and url, or error if not found.
    """
    auth_email, auth_token = _auth_for_user(user_id, user_email)
    space  = space_key or _conf_space()
    cql    = f'type=page AND space="{space}" AND title="{title}"'
    params = urllib.parse.urlencode({"cql": cql, "limit": 1})
    result = _request("GET", f"?{params}", auth_email=auth_email, auth_token=auth_token)

    if "error" in result:
        return json.dumps(result)

    results = result.get("results", [])
    if not results:
        return json.dumps({"error": f"Page not found: {title!r} in space {space!r}"})

    item     = results[0]
    web_link = item.get("_links", {}).get("webui", "")
    full_url = f"{_conf_base()}{web_link}" if web_link and not web_link.startswith("http") else web_link
    return json.dumps({"page_id": item.get("id", ""), "title": title, "url": full_url})
