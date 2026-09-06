# SPDX-License-Identifier: MIT
"""
Microsoft 365 custom adapter — Microsoft Graph API.

Handles Graph-specific quirks:
- OData $filter, $select, $orderby, $top query params
- @odata.nextLink cursor-based pagination
- Delta queries for incremental sync
- Unified token for Outlook + Teams (same Azure AD app)
"""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx
from connectors.net_relay import relay_request

from connectors.adapters.base import AdapterBase, AdapterPage
from connectors.base import ConnectorContext, ConnectorTool
from core.logger import logger, mask_email

GRAPH_BASE = "https://graph.microsoft.com"

def _split_recipients(raw) -> list[str]:
    """Split a recipient/attendee string into clean email addresses.

    Fixes #1 + G6: users (and the model) separate addresses with ';', ',', spaces
    or newlines, AND real Outlook recipients carry display names like
    'Doe, John <john@x.com>' — where the comma is INSIDE the name, not a separator.
    Naively splitting on ',' shredded those into bogus recipients. We use the stdlib
    RFC-5322 parser (email.utils.getaddresses), which correctly extracts the
    addr-spec from 'Name <addr>' forms and ignores commas inside quoted/display
    names. We then keep only tokens that look like real addresses, deduped in order.
    """
    import re
    from email.utils import getaddresses
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        parts = []
        for r in raw:
            parts.extend(_split_recipients(r))
        # de-dupe across the list while preserving order
        seen: dict[str, None] = {}
        for p in parts:
            if p not in seen:
                seen[p] = None
        return list(seen.keys())

    s = str(raw).strip()
    if not s:
        return []
    # getaddresses handles 'A <a@x>, "Doe, John" <j@y>; b@z' correctly. Normalise
    # ';' → ',' first (getaddresses only splits on ',').
    pairs = getaddresses([s.replace(";", ",")])
    out: dict[str, None] = {}
    for _name, addr in pairs:
        addr = (addr or "").strip().strip("<>").strip()
        # Keep only plausible addresses; a bare display name with no addr-spec is
        # dropped here (the send-side guard reports those separately).
        if addr and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr) and addr not in out:
            out[addr] = None
    # Fallback: if getaddresses found nothing (e.g. whitespace-only separators with
    # no commas), split on any of ; , whitespace as before.
    if not out:
        for t in re.split(r"[;,\s]+", s):
            t = t.strip().strip("<>").strip()
            if t and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", t) and t not in out:
                out[t] = None
    return list(out.keys())


# Matches the small set of inline tags an agent realistically emits in a message.
_HTML_TAG_RE = re.compile(
    r"</?(?:br|p|div|b|strong|i|em|u|ul|ol|li|a|span|h[1-6]|blockquote|code|pre|table|tr|td|th)\b[^>]*>",
    re.IGNORECASE,
)


def _to_teams_html(msg: str) -> str:
    """Render a message safely as Teams HTML.

    Teams requires contentType=html when attachment chips are present. Previously the
    message was always html-escaped, which corrupted agent-authored markup: a body
    containing <br> came out as the literal text "<br>" (and & became &amp;).

    - If the message ALREADY contains HTML markup, pass it through unchanged so it
      renders as intended.
    - Otherwise treat it as plain text: escape it, then convert newlines to <br> so
      paragraph breaks survive (escaping alone collapses them in HTML).
    """
    import html as _html_mod

    if not msg:
        return ""
    if _HTML_TAG_RE.search(msg):
        return msg
    return _html_mod.escape(msg).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


class Microsoft365Adapter(AdapterBase):
    """Custom adapter for Microsoft Graph API (Outlook + Teams)."""

    TIMEOUT = 25  # Graph can be slow
    # Sends carrying base64 attachments are much larger and were tripping the 25s
    # timeout (Fix #4). Raised from 60→120 to handle large attachment payloads
    # going through the relay (relay adds +15s on top, giving Graph 120s total).
    WRITE_TIMEOUT = 120
    # Auto-pagination bounds (G14): wall-clock budget + hard page cap so one read
    # tool can't run for minutes on a huge mailbox/chat.
    #
    # Raised 20s -> 40s and 25 -> 40 pages together: outlook_search_emails /
    # outlook_count_emails / teams_get_channel_messages / teams_get_chat_messages
    # max_items was separately raised 50/100 -> 2000 (see
    # _STALE_SEEDED_MAX_ITEMS_TOOLS in connectors/engine.py). At the ~50
    # items/page callers commonly request, reaching that 2000-item ceiling needs
    # up to 40 @odata.nextLink pages (2000 / 50) — the old MAX_AUTO_PAGES=25 cap
    # would have silently stopped pagination at ~1,250 items (partial=True)
    # well before the 2000 ceiling was reached, regardless of how much wall-clock
    # budget was available. Both bounds are raised together so neither one is
    # the accidental bottleneck. Override per-deployment via the env vars below.
    import os as _os
    PAGINATE_BUDGET_S = float(_os.getenv("M365_PAGINATE_BUDGET_S", "40"))
    MAX_AUTO_PAGES    = int(_os.getenv("M365_MAX_AUTO_PAGES", "40"))

    def execute(
        self,
        tool: ConnectorTool,
        params: dict,
        context: ConnectorContext,
        cursor: Optional[str] = None,
    ) -> AdapterPage:
        # If cursor is a full nextLink URL, use it directly
        if cursor and cursor.startswith("https://"):
            return self._fetch_nextlink(cursor, context)

        method = tool.method.upper()
        path = tool.path

        # ── OneDrive upload (binary PUT) — used to host files for Teams sends (#19) ──
        if tool.name == "onedrive_upload":
            return self._upload_onedrive(tool, params, context)

        # ── People search: exact email/UPN first, then resilient directory search ──
        if tool.name == "people_search":
            return self._search_people(tool, params, context)

        # ── Org hierarchy: rewrite /me/... → /users/{email}/... when targeting
        # someone other than the signed-in user (Fix #22 reportees) ──
        if tool.name in ("org_direct_reports", "org_get_manager"):
            ue = str(params.pop("user_email", "") or "").strip()
            if ue:
                from urllib.parse import quote as _q
                suffix = "directReports" if tool.name == "org_direct_reports" else "manager"
                path = f"/v1.0/users/{_q(ue)}/{suffix}"

        resolved_path, remaining = self._resolve_path(path, params)
        url = GRAPH_BASE + resolved_path

        headers = self.build_headers(context)
        # Render calendar datetimes (and the calendarView window) in IST instead of
        # UTC. Harmless for non-calendar endpoints, which ignore the Prefer header.
        headers.setdefault("Prefer", 'outlook.timezone="India Standard Time"')
        query_params = self._build_graph_params(tool, remaining, params)

        try:
            if method == "GET":
                resp = relay_request("GET", url, headers=headers, params=query_params, timeout=self.TIMEOUT)
            elif method == "DELETE":
                # Hard delete (e.g. calendar_delete_event → DELETE /me/events/{id}).
                # Graph returns 204 No Content on success.
                resp = relay_request("DELETE", url, headers=headers, timeout=self.WRITE_TIMEOUT)
                resp.raise_for_status()
                return AdapterPage(items=[{"status": "deleted", "tool": tool.name}], next_cursor=None, meta={})
            else:
                if tool.name == "teams_start_chat" and not remaining.get("current_user_id"):
                    current_oid = self._resolve_current_user_oid(headers, context)
                    if current_oid:
                        remaining["current_user_id"] = current_oid
                    else:
                        logger.warning("teams_start_chat: could not resolve signed-in user's Azure AD object id")
                body = self._build_write_body(tool, remaining)
                # Longer timeout for sends carrying attachments (Fix #4).
                has_att = bool(remaining.get("_attachment") or remaining.get("_attachments"))
                # Scale timeout by attachment payload size so very large files get
                # proportionally more time. att_timeout is the effective timeout passed
                # to relay_request; the relay adds +15s on top for its own HTTP call.
                if has_att or tool.is_write:
                    _atts_list = remaining.get("_attachments") or []
                    if isinstance(_atts_list, list):
                        _att_bytes = sum(
                            len(a.get("contentBytes", "") or "") * 3 // 4  # base64 → bytes approx
                            for a in _atts_list if isinstance(a, dict)
                        )
                    else:
                        _att_bytes = 0
                    # +1s per 50 KB of attachment, capped at 120s total so the full
                    # relay chain (120s Graph + 15s relay overhead = 135s) stays well
                    # within the gunicorn worker timeout (240s). Was 180s which could
                    # exceed the old 120s gunicorn timeout and cause ambiguous timeouts.
                    att_timeout = min(self.WRITE_TIMEOUT + max(0, _att_bytes // 51200), 240)  # cap raised: base=120, must be > base or scaling is useless
                else:
                    att_timeout = self.TIMEOUT
                _timeout = att_timeout
                # Honour PATCH (calendar_update_event); everything else uses POST.
                _method = "PATCH" if method == "PATCH" else "POST"
                if tool.name in ("outlook_send_mail", "teams_send_chat_message", "teams_send_message"):
                    import json as _json
                    _body_size = len(_json.dumps(body).encode("utf-8"))

                resp = relay_request(_method, url, headers=headers, json=body, timeout=_timeout)

                # Fix #30 retry: if teams_start_chat returns 400, the email may not be
                # the UPN. Resolve to OID via $filter and retry once automatically.
                if resp.status_code == 400 and tool.name == "teams_start_chat":
                    _email = str(remaining.get("user_email", "")).strip()
                    if _email and "@" in _email:
                        _oid = self._resolve_oid_by_mail(_email, headers, context)
                        if _oid:
                            logger.info(f"teams_start_chat: 400 retry with OID {_oid!r} for {mask_email(_email)!r}")
                            remaining["user_id"] = _oid
                            body = self._build_write_body(tool, remaining)
                            resp = relay_request(_method, url, headers=headers, json=body, timeout=_timeout)

                resp.raise_for_status()
                # Graph send endpoints return 202 Accepted with no body.
                if not resp.content:
                    return AdapterPage(items=[{"status": "sent", "tool": tool.name}], next_cursor=None, meta={})

            resp.raise_for_status()

            # Graph transcript `/content` returns text/vtt, not JSON. Return the
            # raw text as a single item rather than crashing on resp.json().
            ctype = resp.headers.get("content-type", "")
            if "application/json" not in ctype:
                return AdapterPage(
                    items=[{"content": resp.text, "content_type": ctype or "text/plain"}],
                    next_cursor=None,
                    meta={"content_type": ctype},
                )

            data = resp.json()

            items = self._extract_items(data, tool)
            next_link = data.get("@odata.nextLink")
            partial = False          # set True if pagination stops before exhaustion
            _threw_429 = False       # local per-call throttle guard

            # Auto-follow @odata.nextLink for paginated READ tools so callers that
            # don't loop still get complete results (Fix #23 meetings truncated to 3,
            # Fix #28 large group chats not fully fetched). Bounded by max_items and a
            # hard page cap so we never runaway. Only for GETs.
            AUTO_PAGE = {
                "calendar_list_events", "teams_get_chat_messages", "teams_list_chats",
                "teams_get_channel_messages", "outlook_search_emails", "people_search",
            }
            if method == "GET" and tool.name in AUTO_PAGE and next_link:
                cap = max(int(getattr(tool, "max_items", 50) or 50), 50)
                # Group chats often live beyond Graph's first /me/chats page. When the
                # caller is looking up a group by topic/member, scan deeper even if the
                # persisted connector definition still has the historical max_items=100.
                if tool.name == "teams_list_chats" and (
                    (params or {}).get("name_contains") or (params or {}).get("search_query")
                ):
                    try:
                        import os as _os
                        cap = max(cap, int((params or {}).get("limit") or 0), int(_os.getenv("M365_CHAT_SCAN_LIMIT", "1000")))
                    except Exception:
                        cap = max(cap, 1000)
                pages = 0
                # G14: wall-clock deadline so a huge mailbox can't make one tool call
                # run for minutes; and handle 429/Retry-After (respect a bounded
                # backoff, then continue) instead of silently aborting on the first
                # throttle. When we stop early, `partial` tells the caller results
                # were truncated (surfaced via meta).
                _deadline = time.time() + float(self.PAGINATE_BUDGET_S)
                while next_link and len(items) < cap and pages < self.MAX_AUTO_PAGES:
                    if time.time() >= _deadline:
                        partial = True
                        logger.info(f"m365 auto-paginate deadline hit for {tool.name} "
                                    f"({len(items)} items, {pages} pages)")
                        break
                    try:
                        page = self._fetch_nextlink(next_link, context)
                    except httpx.HTTPStatusError as he:
                        if he.response.status_code == 429:
                            # Throttled: respect Retry-After once (bounded), then retry
                            # the SAME nextLink. A second 429 stops with partial=True.
                            ra = he.response.headers.get("Retry-After")
                            try:
                                wait = min(float(ra), 10.0) if ra else 2.0
                            except Exception:
                                wait = 2.0
                            if _threw_429:
                                partial = True
                                break
                            _threw_429 = True
                            time.sleep(wait)
                            continue
                        logger.warning(f"m365 auto-paginate stopped for {tool.name}: {he}")
                        partial = True
                        break
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(f"m365 auto-paginate stopped for {tool.name}: {exc}")
                        partial = True
                        break
                    # nextlink returns raw Graph items — normalize them the same way.
                    items.extend(self._extract_items({"value": page.items}, tool))
                    next_link = page.next_cursor
                    pages += 1
                items = items[:cap]
                if next_link:
                    partial = True   # hit the cap with more available
                if pages > 0:
                    # Trace log: confirms @odata.nextLink was actually followed and
                    # how far — evidence trail for "email/Teams retrieval stops after
                    # N messages" style reports. Only logs when at least one extra
                    # page was fetched, so normal single-page tool calls stay silent.
                    logger.info(
                        f"m365 auto-paginate {tool.name}: followed {pages} extra "
                        f"page(s) beyond the first, total {len(items)} item(s) "
                        f"(cap={cap}, partial={partial})"
                    )

            # Fix #31: /me/people fallback — if people_search returned no results,
            # retry against the full org directory via /v1.0/users $filter.
            # /me/people only covers people you've recently interacted with; a colleague
            # you've never emailed/chatted with won't appear there. The /v1.0/users
            # endpoint covers the entire Azure AD tenant.
            if tool.name == "people_search" and not items:
                sq = (params or {}).get("search_query", "")
                if sq:
                    items = self._people_search_fallback(sq, context)
                    logger.info(f"people_search: /me/people empty, fallback returned {len(items)} results for {sq!r}")

            # Client-side name filter for teams_list_chats group lookup (#5/#17).
            if tool.name == "teams_list_chats":
                nc = (params or {}).get("name_contains") or (params or {}).get("search_query")
                if nc:
                    ncl = str(nc).lower()
                    terms = [t for t in re.split(r"\s+", ncl) if t]

                    def _chat_haystack(c: dict) -> str:
                        return " ".join([
                            c.get("topic", "") or "",
                            c.get("chat_type", "") or "",
                            " ".join(str(m or "") for m in c.get("members", [])),
                        ]).lower()

                    items = [
                        c for c in items
                        if ncl in _chat_haystack(c)
                        or (terms and all(t in _chat_haystack(c) for t in terms))
                    ]

            if tool.name == "calendar_list_events":
                items = self._filter_calendar_events(items, params or {})

            return AdapterPage(
                items=items,
                next_cursor=next_link,
                meta={
                    "odata_count": data.get("@odata.count"),
                    "odata_context": data.get("@odata.context", ""),
                    # G14: tell the caller results were truncated (deadline/cap/throttle)
                    # so the agent can say "showing first N" rather than imply completeness.
                    "partial": partial,
                },
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # ConnectorTokenRejected (NOT ConnectorReauthRequired): a 401 here
                # almost always just means this access token aged out — Entra access
                # tokens live ~1h. The engine refreshes and retries ONCE, and only
                # escalates to a real reconnect if that also fails. Raising
                # ConnectorReauthRequired directly used to deactivate the stored
                # token, so a routine hourly expiry forced a manual reconnect and
                # broke every scheduled task.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Microsoft 365 access token was rejected (401).")
            if e.response.status_code == 403:
                # 403 = the token is valid but LACKS the Graph permission this tool
                # needs (very common right after new tools ship — users must reconsent).
                # Surface a clear reconnect message instead of a raw error the agent
                # would otherwise narrate/hallucinate around (G19).
                from connectors.base import ConnectorScopeError
                raise ConnectorScopeError(
                    "Microsoft 365 blocked this action (403 — insufficient permission). "
                    "The connector needs a Graph scope that hasn't been granted yet. "
                    "Reconnect Microsoft 365 (Connectors page) to grant the new permissions, "
                    "then retry.")
            if e.response.status_code == 400:
                if tool.name == "teams_start_chat":
                    # Log the raw Graph error body to help diagnose UPN vs mail mismatches
                    try:
                        _err_body = e.response.json()
                    except Exception:
                        _err_body = e.response.text
                    logger.warning(f"teams_start_chat 400 from Graph: {_err_body}")
                    err_text = _err_body if isinstance(_err_body, str) else str(_err_body)
                    if len(err_text) > 800:
                        err_text = err_text[:800] + "..."
                    raise ValueError(
                        "Microsoft 365 returned 400 when creating the Teams chat. "
                        "This can be caused by Teams policy/license restrictions, missing Graph "
                        "permissions, a disabled/cross-tenant recipient, or an invalid user bind. "
                        f"Graph detail: {err_text}"
                    )
                if tool.name == "people_search":
                    raise ValueError(
                        "Microsoft 365 directory search failed. Retry with an exact work "
                        "email/UPN or a more specific name."
                    )
                raise ValueError(
                    f"Microsoft 365 rejected the {tool.name} request as invalid. Check the "
                    "recipient IDs, email addresses, dates, and required fields, then retry."
                )
            raise

    def _resolve_current_user_oid(self, headers: dict, context: ConnectorContext) -> str:
        """Resolve the signed-in user's Azure AD object id via /me.

        Graph createChat payloads are more reliable when both members use explicit
        /users/{id} bindings. Binding the current user as /me can return 400 in
        some tenants even though the OAuth token is otherwise valid for Teams.
        """
        try:
            resp = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/me",
                headers=headers,
                params={"$select": "id"},
                timeout=10,
            )
            if resp.status_code == 200:
                oid = (resp.json() if resp.content else {}).get("id", "")
                if oid:
                    return oid
            elif resp.status_code not in (400, 404):
                resp.raise_for_status()
        except Exception as exc:
            logger.warning(f"_resolve_current_user_oid failed: {exc}")
        return ""

    def _resolve_oid_by_mail(self, email: str, headers: dict, context: ConnectorContext) -> str:
        """Resolve an email/UPN to an Azure AD OID via $filter on mail and userPrincipalName.

        Used as a retry mechanism when teams_start_chat returns 400 — the email may
        differ from the UPN so /users/{email} fails, but $filter on mail finds the user.
        Returns the OID string on success, empty string on failure.
        """
        search_headers = {**headers, "ConsistencyLevel": "eventual"}
        escaped = email.replace("'", "''")
        try:
            # Try direct path first (fastest — works when email == UPN)
            from urllib.parse import quote as _q
            r = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users/{_q(email)}",
                headers=headers,
                params={"$select": "id"},
                timeout=10,
            )
            if r.status_code == 200:
                oid = r.json().get("id", "")
                if oid:
                    return oid
        except Exception:
            pass
        try:
            # Fallback: $filter on mail and userPrincipalName
            r2 = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=search_headers,
                params={
                    "$filter": f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'",
                    "$select": "id",
                    "$count": "true",
                    "$top": "2",
                },
                timeout=10,
            )
            if r2.status_code == 200:
                vals = r2.json().get("value", [])
                if len(vals) == 1 and vals[0].get("id"):
                    return vals[0]["id"]
        except Exception as exc:
            logger.warning(f"_resolve_oid_by_mail failed for {mask_email(email)!r}: {exc}")
        return ""

    def _people_search_fallback(self, search_query: str, context: ConnectorContext) -> list[dict]:
        """Fallback people search against the full Azure AD directory via /v1.0/users.

        Fix #31: Called when /me/people returns empty (colleague never interacted with).
        Uses $filter startswith on displayName for broad name matching, with a
        $search fallback. Requires ConsistencyLevel: eventual for $search.
        Returns a list of normalized person dicts (same shape as _normalize_person).
        """
        headers = self.build_headers(context)
        headers["ConsistencyLevel"] = "eventual"
        sq = search_query.strip()

        # Try $filter startswith(displayName, ...) first — works without special perms
        # and handles partial first-name searches like "Anshuman".
        try:
            resp = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=headers,
                params={
                    "$filter": f"startswith(displayName,'{sq}') or startswith(mail,'{sq}')",
                    "$select": "id,displayName,jobTitle,department,mail,userPrincipalName",
                    "$top": "25",
                    "$count": "true",
                },
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 200:
                vals = resp.json().get("value", [])
                if vals:
                    return [self._normalize_person(p) for p in vals]
        except Exception as exc:
            logger.warning(f"people_search fallback $filter failed for {sq!r}: {exc}")

        # Second fallback: $search "displayName:name" — broader but needs User.ReadBasic.All
        try:
            resp2 = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=headers,
                params={
                    "$search": f'"displayName:{sq}"',
                    "$select": "id,displayName,jobTitle,department,mail,userPrincipalName",
                    "$top": "25",
                    "$count": "true",
                    "$orderby": "displayName",
                },
                timeout=self.TIMEOUT,
            )
            if resp2.status_code == 200:
                vals2 = resp2.json().get("value", [])
                return [self._normalize_person(p) for p in vals2]
        except Exception as exc2:
            logger.warning(f"people_search fallback $search failed for {sq!r}: {exc2}")

        return []

    def _fetch_nextlink(self, nextlink_url: str, context: ConnectorContext) -> AdapterPage:
        """Follow an @odata.nextLink URL directly."""
        headers = self.build_headers(context)
        resp = relay_request("GET", nextlink_url, headers=headers, timeout=self.TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("value", [])
        return AdapterPage(
            items=items,
            next_cursor=data.get("@odata.nextLink"),
            meta={},
        )

    def _search_people(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """Resolve people by exact email/UPN first, then broader directory search."""
        from urllib.parse import quote

        query = str((params or {}).get("search_query") or "").strip()[:120]
        try:
            limit = min(int((params or {}).get("limit") or tool.max_items), tool.max_items)
        except Exception:
            limit = tool.max_items
        select = "id,displayName,jobTitle,department,mail,userPrincipalName"
        headers = self.build_headers(context)
        search_headers = {**headers, "ConsistencyLevel": "eventual"}

        def _items_from(resp) -> list[dict]:
            data = resp.json() if resp.content else {}
            raw = data.get("value", []) if isinstance(data, dict) else []
            return [self._normalize_person(i) for i in raw[:limit] if isinstance(i, dict)]

        def _safe_filter_value(value: str) -> str:
            return value.replace("'", "''")

        def _safe_search_value(value: str) -> str:
            return value.replace('"', " ").strip()

        if not query:
            return AdapterPage(items=[], next_cursor=None, meta={"search_strategy": "empty"})

        looks_like_email = bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", query))
        if looks_like_email:
            direct = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users/{quote(query)}",
                headers=headers,
                params={"$select": select},
                timeout=self.TIMEOUT,
            )
            if direct.status_code == 200:
                data = direct.json() if direct.content else {}
                if data.get("id"):
                    return AdapterPage(
                        items=[self._normalize_person(data)],
                        next_cursor=None,
                        meta={"search_strategy": "exact_user_path"},
                    )
            elif direct.status_code not in (404, 400):
                direct.raise_for_status()

            exact = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=search_headers,
                params={
                    "$filter": (
                        f"mail eq '{_safe_filter_value(query)}' or "
                        f"userPrincipalName eq '{_safe_filter_value(query)}'"
                    ),
                    "$select": select,
                    "$count": "true",
                    "$top": str(limit),
                },
                timeout=self.TIMEOUT,
            )
            if exact.status_code == 200:
                items = _items_from(exact)
                if items:
                    return AdapterPage(
                        items=items,
                        next_cursor=None,
                        meta={"search_strategy": "exact_mail_filter"},
                    )
            elif exact.status_code not in (400, 404):
                exact.raise_for_status()

        search_value = _safe_search_value(query)
        search = relay_request(
            "GET",
            f"{GRAPH_BASE}/v1.0/users",
            headers=search_headers,
            params={
                "$search": f'"displayName:{search_value}" OR "mail:{search_value}" OR "userPrincipalName:{search_value}"',
                "$select": select,
                "$count": "true",
                "$top": str(limit),
            },
            timeout=self.TIMEOUT,
        )
        if search.status_code == 200:
            return AdapterPage(
                items=_items_from(search),
                next_cursor=None,
                meta={"search_strategy": "directory_search"},
            )
        if search.status_code not in (400, 404):
            search.raise_for_status()

        prefix = _safe_filter_value(query)
        fallback = relay_request(
            "GET",
            f"{GRAPH_BASE}/v1.0/users",
            headers=search_headers,
            params={
                "$filter": (
                    f"startswith(displayName,'{prefix}') or "
                    f"startswith(mail,'{prefix}') or "
                    f"startswith(userPrincipalName,'{prefix}')"
                ),
                "$select": select,
                "$count": "true",
                "$top": str(limit),
            },
            timeout=self.TIMEOUT,
        )
        fallback.raise_for_status()
        return AdapterPage(
            items=_items_from(fallback),
            next_cursor=None,
            meta={"search_strategy": "prefix_filter"},
        )

    def _upload_onedrive(self, tool: ConnectorTool, params: dict, context: ConnectorContext) -> AdapterPage:
        """Upload a file to the user's OneDrive, create an org-scoped sharing link,
        and return that link as webUrl (Fix #19 + sharing-link pipeline).

        Pipeline:
          1. PUT /me/drive/root:/AiNxt Attachments/{name}:/content  -> driveItem
          2. POST /me/drive/items/{id}/createLink  -> org-scoped view link
          3. Return the sharing link webUrl (accessible to any org member)

        The driveItem's own webUrl requires the recipient to have direct access;
        the createLink URL works for anyone in the organisation (scope=organization).
        Falls back to the driveItem webUrl if createLink fails (e.g. policy blocks it).
        """
        import base64
        from urllib.parse import quote
        name = str(params.get("filename", "document")).replace("/", "_").replace("\\", "_")
        b64 = params.get("content_bytes", "") or ""
        try:
            raw = base64.b64decode(b64)
        except Exception:
            raw = b""
        ctype = params.get("content_type", "application/octet-stream")

        folder = "AiNxt Attachments"
        upload_path = f"/v1.0/me/drive/root:/{quote(folder)}/{quote(name)}:/content"
        url = GRAPH_BASE + upload_path
        headers = self.build_headers(context)
        headers["Content-Type"] = ctype

        try:
            # Step 1: upload the file bytes
            resp = relay_request("PUT", url, headers=headers, content=raw, timeout=self.WRITE_TIMEOUT)
            resp.raise_for_status()
            item = resp.json() if resp.content else {}
            item_id = item.get("id", "")
            fallback_url = item.get("webUrl", "")

            # Step 2: create an org-scoped sharing link so any org recipient can open it
            share_url = fallback_url
            if item_id:
                try:
                    link_endpoint = GRAPH_BASE + f"/v1.0/me/drive/items/{item_id}/createLink"
                    link_headers = self.build_headers(context)
                    link_headers["Content-Type"] = "application/json"
                    link_resp = relay_request(
                        "POST", link_endpoint,
                        headers=link_headers,
                        json={"type": "view", "scope": "organization"},
                        timeout=self.TIMEOUT,
                    )
                    link_resp.raise_for_status()
                    link_data = link_resp.json() if link_resp.content else {}
                    share_url = (link_data.get("link") or {}).get("webUrl") or fallback_url
                    logger.info(f"cowork_mcp: OneDrive sharing link created for {name}: {share_url}")
                except Exception as link_exc:
                    # createLink can fail if tenant policy disables org links.
                    # Fall back to driveItem webUrl rather than failing the whole send.
                    logger.warning(f"cowork_mcp: createLink failed for {name}, using driveItem webUrl: {link_exc}")
                    share_url = fallback_url

            return AdapterPage(
                items=[{"id": item_id, "name": item.get("name", name), "webUrl": share_url}],
                next_cursor=None, meta={},
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                # ConnectorTokenRejected (NOT ConnectorReauthRequired): a 401 here
                # almost always just means this access token aged out — Entra access
                # tokens live ~1h. The engine refreshes and retries ONCE, and only
                # escalates to a real reconnect if that also fails. Raising
                # ConnectorReauthRequired directly used to deactivate the stored
                # token, so a routine hourly expiry forced a manual reconnect and
                # broke every scheduled task.
                from connectors.base import ConnectorTokenRejected
                raise ConnectorTokenRejected("Microsoft 365 access token was rejected (401).")
            if e.response.status_code == 403:
                from connectors.base import ConnectorScopeError
                raise ConnectorScopeError(
                    "Microsoft 365 blocked this action (403 — insufficient permission). "
                    "The connector needs Files.ReadWrite scope that hasn't been granted yet. "
                    "Reconnect Microsoft 365 (Connectors page) to grant the new permissions, "
                    "then retry.")
            raise

    def _build_write_body(self, tool: ConnectorTool, remaining: dict) -> dict:
        """
        Shape the simple tool params into the Graph request body for WRITE tools.
        Keeps the connector's public schema simple (to/subject/body, message) while
        emitting the verbose structure Graph requires. Non-write/other POSTs fall
        back to sending the params as-is.
        """
        name = tool.name
        if name == "outlook_send_mail":
            recipients = [
                {"emailAddress": {"address": addr}}
                for addr in _split_recipients(remaining.get("to", ""))
            ]
            cc = [
                {"emailAddress": {"address": addr}}
                for addr in _split_recipients(remaining.get("cc", ""))
            ]
            message = {
                "subject": remaining.get("subject", ""),
                "body": {"contentType": "Text", "content": remaining.get("body", "")},
                "toRecipients": recipients,
            }
            if cc:
                message["ccRecipients"] = cc
            bcc = [
                {"emailAddress": {"address": addr}}
                for addr in _split_recipients(remaining.get("bcc", ""))
            ]
            if bcc:
                message["bccRecipients"] = bcc
            if remaining.get("importance"):
                message["importance"] = remaining["importance"]  # low | normal | high
            if remaining.get("html"):
                message["body"] = {"contentType": "HTML", "content": remaining.get("body", "")}
            # Optional document attachment(s) (resolved + base64-encoded in mcp_bridge
            # from the build_document job). Graph wants message.attachments as a
            # list of #microsoft.graph.fileAttachment objects. Fix #13/#18: accept a
            # LIST of attachments (multiple files in one send), not just one.
            atts = self._collect_attachments(remaining)
            if atts:
                message["attachments"] = atts

            return {"message": message, "saveToSentItems": True}
        if name == "outlook_create_draft":
            recipients = [{"emailAddress": {"address": a}} for a in _split_recipients(remaining.get("to", ""))]
            cc = [{"emailAddress": {"address": a}} for a in _split_recipients(remaining.get("cc", ""))]
            draft: dict = {
                "subject": remaining.get("subject", ""),
                "body": {"contentType": "HTML" if remaining.get("html") else "Text",
                         "content": remaining.get("body", "")},
            }
            if recipients:
                draft["toRecipients"] = recipients
            if cc:
                draft["ccRecipients"] = cc
            atts = self._collect_attachments(remaining)
            if atts:
                draft["attachments"] = atts
            return draft
        if name == "outlook_move_email":
            # POST /me/messages/{id}/move  — body {destinationId}
            return {"destinationId": remaining.get("destination_folder_id", "")
                    or remaining.get("destination", "")}
        if name == "outlook_mark_email":
            # PATCH /me/messages/{id}  — set isRead / flag / importance / categories
            body: dict = {}
            if "is_read" in remaining:
                body["isRead"] = bool(remaining["is_read"])
            if remaining.get("flag"):
                body["flag"] = {"flagStatus": remaining["flag"]}  # notFlagged|flagged|complete
            if remaining.get("importance"):
                body["importance"] = remaining["importance"]
            if remaining.get("categories"):
                body["categories"] = _split_recipients(remaining["categories"])
            return body
        if name == "outlook_create_folder":
            return {"displayName": remaining.get("name", "New Folder")}
        # Graph action endpoints that take NO body — send an empty object, never the
        # leftover params (which would be rejected / misinterpreted).
        if name in ("outlook_send_draft",):
            return {}
        if name == "teams_reply_channel_message":
            # POST /teams/{team}/channels/{chan}/messages/{msg}/replies
            html = bool(remaining.get("html"))
            return {"body": {"contentType": "html" if html else "text",
                             "content": remaining.get("message", "")}}
        if name == "teams_create_channel":
            b = {"displayName": remaining.get("name", ""),
                 "description": remaining.get("description", "")}
            mt = remaining.get("membership_type")
            if mt:
                b["membershipType"] = mt  # standard | private | shared
            return b
        if name == "teams_create_online_meeting":
            b: dict = {"subject": remaining.get("subject", "Meeting")}
            if remaining.get("start"):
                b["startDateTime"] = remaining["start"]
            if remaining.get("end"):
                b["endDateTime"] = remaining["end"]
            return b
        if name in ("teams_send_message", "teams_send_chat_message"):
            body = {"content": remaining.get("message", "")}
            payload: dict = {"body": body}
            # Fix #4/#19: Teams messages can carry hosted-content / attachment refs.
            # When mcp_bridge resolved document(s), it stores a link + name we surface
            # as a reference attachment so the file is not dropped silently. If the
            # content type is HTML (needed to embed attachment chips), mark it so.
            atts = self._collect_attachments(remaining)
            if atts:
                ref_atts = []
                content_links = []
                for a in atts:
                    if a.get("_teams_attachment"):
                        # Normal path: OneDrive upload succeeded — use reference attachment
                        ref_atts.append(a["_teams_attachment"])
                        if a.get("_teams_link_html"):
                            content_links.append(a["_teams_link_html"])
                    # Fallback path: OneDrive upload failed.
                    # Graph Teams messages do NOT support inline base64 fileAttachment
                        # (that type is Outlook-only). Log and skip — the message will
                        # send without the attachment rather than failing entirely.

                if ref_atts:
                    payload["attachments"] = ref_atts
                    # Teams requires contentType=html whenever attachment chips are present.
                    # The <attachment id="{guid}"> chip in content_links is already valid HTML.
                    body["contentType"] = "html"
                    body["content"] = (
                        f"<p>{_to_teams_html(remaining.get('message', '') or '')}</p>"
                        + "".join(content_links)
                    )

            return payload
        if name == "outlook_reply_email":
            # POST /me/messages/{id}/reply  — body is just {comment}
            return {"comment": remaining.get("comment", "")}
        if name == "outlook_reply_all_email":
            # POST /me/messages/{id}/replyAll — Graph reply-all resolves To/Cc itself
            # from the original message, so we only pass the comment. Fix #16.
            return {"comment": remaining.get("comment", "")}
        if name == "outlook_forward_email":
            recipients = [
                {"emailAddress": {"address": addr}}
                for addr in _split_recipients(remaining.get("to", ""))
            ]
            return {"comment": remaining.get("comment", ""), "toRecipients": recipients}
        if name in ("calendar_create_event", "calendar_update_event"):
            attendees = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in _split_recipients(remaining.get("attendees", ""))
            ]
            attendees.extend(
                {"emailAddress": {"address": a}, "type": "optional"}
                for a in _split_recipients(remaining.get("optional_attendees", ""))
            )
            tz = remaining.get("timezone", "India Standard Time")
            ev: dict = {}
            if remaining.get("subject"):
                ev["subject"] = remaining["subject"]
            if remaining.get("start"):
                ev["start"] = {"dateTime": remaining["start"], "timeZone": tz}
            if remaining.get("end"):
                ev["end"] = {"dateTime": remaining["end"], "timeZone": tz}
            if remaining.get("body"):
                ev["body"] = {"contentType": "HTML", "content": remaining["body"]}
            if attendees:
                ev["attendees"] = attendees
            if remaining.get("location"):
                ev["location"] = {"displayName": remaining["location"]}
            if remaining.get("is_online_meeting"):
                ev["isOnlineMeeting"] = True
                ev["onlineMeetingProvider"] = "teamsForBusiness"
            # For create, Graph requires subject/start/end; for update (PATCH) only the
            # changed fields are sent, so an empty-ish body just means "no change".
            if name == "calendar_create_event":
                ev.setdefault("subject", remaining.get("subject", ""))
                ev.setdefault("start", {"dateTime": remaining.get("start", ""), "timeZone": tz})
                ev.setdefault("end", {"dateTime": remaining.get("end", ""), "timeZone": tz})
            return ev
        if name == "calendar_cancel_event":
            # POST /me/events/{id}/cancel  — organizer cancels + notifies attendees.
            return {"comment": remaining.get("comment", "")}
        if name in ("calendar_accept_event", "calendar_decline_event",
                    "calendar_tentative_event"):
            # POST /me/events/{id}/{accept|decline|tentativelyAccept}
            # sendResponse controls whether the organizer is notified.
            b: dict = {"sendResponse": remaining.get("send_response", True)}
            if remaining.get("comment"):
                b["comment"] = remaining["comment"]
            # Graph's proposeNewTime variants also accept a proposedNewTime, but the
            # base accept/decline/tentative bodies are just comment + sendResponse.
            return b
        if name == "calendar_forward_event":
            # POST /me/events/{id}/forward — forward an event to other people.
            recipients = [
                {"emailAddress": {"address": a}}
                for a in _split_recipients(remaining.get("to", ""))
            ]
            return {"ToRecipients": recipients, "Comment": remaining.get("comment", "")}
        if name == "calendar_find_meeting_times":
            # POST /me/findMeetingTimes — suggest free slots for a set of attendees.
            attendees = [
                {"emailAddress": {"address": a}, "type": "required"}
                for a in _split_recipients(remaining.get("attendees", ""))
            ]
            b = {}
            if attendees:
                b["attendees"] = attendees
            if remaining.get("meeting_duration"):
                b["meetingDuration"] = remaining["meeting_duration"]  # ISO8601, e.g. PT30M
            if remaining.get("minimum_attendee_percentage") is not None:
                b["minimumAttendeePercentage"] = remaining["minimum_attendee_percentage"]
            return b
        if name == "calendar_get_schedule":
            # POST /me/calendar/getSchedule — free/busy for a list of mailboxes.
            tz = remaining.get("timezone", "India Standard Time")
            return {
                "schedules": _split_recipients(remaining.get("schedules", "")),
                "startTime": {"dateTime": remaining.get("start", ""), "timeZone": tz},
                "endTime":   {"dateTime": remaining.get("end", ""),   "timeZone": tz},
                "availabilityViewInterval": remaining.get("interval_minutes", 30),
            }
        if name == "teams_start_chat":
            # POST /chats — create a oneOnOne chat between the signed-in user and the
            # target. Graph is idempotent: if a 1:1 already exists it is returned. This
            # lets the agent message someone with NO prior conversation (Fix #10).
            #
            # Fix #30: Graph's createChat API requires user@odata.bind to use the
            # slash-separated path format (/users/{id-or-upn}), NOT the OData function
            # syntax (/users('{email}')) — the latter always returns 400 Bad Request.
            # Prefer the pre-resolved OID (injected by mcp_bridge as user_id) so the
            # bind is unambiguous; fall back to the raw email/UPN if no OID available.
            target = (
                str(remaining.get("user_id", "")).strip()
                or str(remaining.get("user_email", "")).strip()
            )
            current_user_id = str(remaining.get("current_user_id", "")).strip()
            current_bind = (
                f"https://graph.microsoft.com/v1.0/users/{current_user_id}"
                if current_user_id else "https://graph.microsoft.com/v1.0/me"
            )
            return {
                "chatType": "oneOnOne",
                "members": [
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": current_bind,
                    },
                    {
                        "@odata.type": "#microsoft.graph.aadUserConversationMember",
                        "roles": ["owner"],
                        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{target}",
                    },
                ],
            }
        return remaining

    def _collect_attachments(self, remaining: dict) -> list[dict]:
        """Return the list of resolved attachments injected by mcp_bridge.

        Supports both the legacy single `_attachment` dict and the new
        `_attachments` list (multiple files in one send — Fix #13/#18).
        """
        out: list[dict] = []
        multi = remaining.get("_attachments")
        if isinstance(multi, list):
            for a in multi:
                if isinstance(a, dict) and (a.get("contentBytes") or a.get("_teams_attachment")):
                    out.append(a)
        single = remaining.get("_attachment")
        if isinstance(single, dict) and (single.get("contentBytes") or single.get("_teams_attachment")):
            out.append(single)

        return out

    def _build_graph_params(self, tool: ConnectorTool, remaining: dict, original: dict) -> dict:
        """Build Graph OData query params — ENDPOINT-AWARE.

        Different Graph endpoints accept different params. Applying the email-style
        params (always-$top + receivedDateTime filter) to every endpoint returns
        400 Bad Request:
          • /me/joinedTeams, /teams/{id}/channels, transcript lists → reject $top
          • /me/events (calendar) → filters on start/dateTime, NOT receivedDateTime
        """
        q: dict = {}
        name = tool.name

        # $top only where the endpoint supports paging.
        TOP_OK = {
            "outlook_search_emails", "outlook_count_emails", "calendar_list_events",
            "teams_get_channel_messages", "teams_get_chat_messages", "people_search",
        }
        if name in TOP_OK:
            limit = remaining.pop("limit", tool.max_items)
            try:
                q["$top"] = min(int(limit), tool.max_items)
            except Exception:
                q["$top"] = tool.max_items
        else:
            remaining.pop("limit", None)

        # ── Outlook message search/count ──────────────────────────────
        if name in ("outlook_search_emails", "outlook_count_emails"):
            # Graph /me/messages: $search (full-text over from + subject + body) CANNOT
            # be combined with $filter or $orderby (Graph returns 400). So if the model
            # gave a free-text search_query, use $search ALONE; otherwise use the
            # structured $filter path. This fixes "irrelevant results" (subject-only).
            search_query = remaining.pop("search_query", None)
            if search_query:
                q["$search"] = f'"{search_query}"'
                # Drop any structured filters the model also set — they 400 with $search.
                for _k in ("from_address", "from_name", "subject_contains",
                           "date_from", "date_to", "is_read"):
                    remaining.pop(_k, None)
                # NOTE: no $orderby with $search (Graph 400). Results are relevance-ranked.
            else:
                filters = []
                if "from_address" in remaining:
                    filters.append(f"from/emailAddress/address eq '{remaining.pop('from_address')}'")
                if "from_name" in remaining:
                    filters.append(f"from/emailAddress/name eq '{remaining.pop('from_name')}'")
                if "subject_contains" in remaining:
                    filters.append(f"contains(subject, '{remaining.pop('subject_contains')}')")
                if "date_from" in remaining:
                    filters.append(f"receivedDateTime ge {remaining.pop('date_from')}T00:00:00Z")
                if "date_to" in remaining:
                    filters.append(f"receivedDateTime le {remaining.pop('date_to')}T23:59:59Z")
                if "is_read" in remaining:
                    filters.append(f"isRead eq {str(remaining.pop('is_read')).lower()}")
                if filters:
                    q["$filter"] = " and ".join(filters)
                q["$orderby"] = "receivedDateTime desc"
            q["$select"] = ("id,subject,from,receivedDateTime,isRead,bodyPreview,hasAttachments"
                            if name == "outlook_search_emails" else "id,from,receivedDateTime")

        # ── Calendar: use /me/calendarView (path set in seed) ─────────
        # calendarView REQUIRES startDateTime + endDateTime as QUERY PARAMS (not
        # $filter) and EXPANDS recurring events (which /me/events does not). When the
        # model omits dates, default to a today→+30d window (fixes the "only today"
        # and empty-result bugs). The Prefer: outlook.timezone header (set in execute)
        # makes the window + returned times render in IST.
        elif name == "calendar_list_events":
            import datetime as _dt
            _today = _dt.date.today()
            _df = remaining.pop("date_from", None) or _today.isoformat()
            _dt2 = remaining.pop("date_to", None) or (_today + _dt.timedelta(days=30)).isoformat()
            q["startDateTime"] = f"{_df}T00:00:00"
            q["endDateTime"]   = f"{_dt2}T23:59:59"
            q["$orderby"] = "start/dateTime"
            q["$select"] = "id,subject,start,end,organizer,attendees,onlineMeeting,isOnlineMeeting,location,bodyPreview"

        # ── People search (/me/people — recent contacts + org directory) ──────────
        # Primary: /me/people with $search covers people you've interacted with and
        # scores them by relevance. When this returns empty, execute() falls back to
        # a direct /v1.0/users $filter lookup (see _people_search_fallback).
        elif name == "people_search":
            if "search_query" in remaining:
                q["$search"] = f'"{remaining.pop("search_query")}"'
            # Only request the fields we surface — avoids pulling phones we then drop.
            q["$select"] = "id,displayName,jobTitle,department,scoredEmailAddresses,userPrincipalName"

        # ── Teams chats: expand members + topic so groups are findable by name (#5/#17) ──
        elif name == "teams_list_chats":
            q["$expand"] = "members"
            # Use the caller/env scan limit rather than the DB definition's old 100-item
            # ceiling. Large tenants can have hundreds of stale chats before the named
            # group, and Graph has no server-side topic search for /me/chats.
            try:
                import os as _os
                scan_limit = int(remaining.pop("limit", None) or _os.getenv("M365_CHAT_SCAN_LIMIT", "1000"))
            except Exception:
                scan_limit = 1000
            q["$top"] = min(max(scan_limit, 50), 50)  # Graph /me/chats page max is 50; auto-pagination scans the rest.
            remaining.pop("name_contains", None)  # applied client-side after fetch

        # ── Read a single email (full body) ───────────────────────────
        elif name == "outlook_read_email":
            q["$select"] = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                            "body,hasAttachments")

        # ── Teams channel + chat messages ─────────────────────────────
        elif name in ("teams_get_channel_messages", "teams_get_chat_messages"):
            q["$select"] = "id,messageType,body,from,createdDateTime,subject"

        # ── Org hierarchy (reportees / manager) — Fix #22 ─────────────
        elif name in ("org_direct_reports", "org_get_manager"):
            q["$select"] = "id,displayName,jobTitle,department,mail,userPrincipalName"

        # joinedTeams / channels / transcript lists → no query params (raw GET)
        return q

    def _extract_items(self, data: dict, tool: ConnectorTool) -> list[dict]:
        """Extract and normalize items from Graph API response."""
        if "value" in data:
            raw_items = data.get("value", [])
        else:
            raw_items = [data] if data and tool.name in ("outlook_read_email", "teams_start_chat") else []
        if not isinstance(raw_items, list):
            raw_items = [raw_items] if raw_items else []

        normalized = []
        for item in raw_items:
            if tool.name in ("outlook_search_emails", "outlook_count_emails"):
                normalized.append(self._normalize_email(item))
            elif tool.name == "outlook_read_email":
                normalized.append(self._normalize_email_full(item))
            elif tool.name in ("teams_get_channel_messages", "teams_get_chat_messages"):
                normalized.append(self._normalize_teams_message(item))
            elif tool.name == "people_search":
                normalized.append(self._normalize_person(item))
            elif tool.name == "teams_list_chats":
                normalized.append(self._normalize_chat(item))
            elif tool.name == "calendar_list_events":
                normalized.append(self._normalize_calendar_event(item))
            elif tool.name in ("org_direct_reports", "org_get_manager"):
                normalized.append({
                    "id": item.get("id", ""),
                    "display_name": item.get("displayName", ""),
                    "job_title": item.get("jobTitle", ""),
                    "department": item.get("department", ""),
                    "email": item.get("mail") or item.get("userPrincipalName", ""),
                })
            elif tool.name in ("teams_list_members", "teams_list_channel_members",
                               "teams_get_chat_members"):
                normalized.append(self._normalize_member(item))
            else:
                # joinedTeams, channels, calendar, transcripts → raw Graph item
                normalized.append(item)
        return normalized

    def _filter_calendar_events(self, items: list[dict], params: dict) -> list[dict]:
        """Apply lightweight client-side filters that Graph calendarView does not
        reliably support as full-text search. Operates on normalized events."""
        query = str(params.get("search_query") or params.get("subject_contains") or "").strip().lower()
        organizer = str(params.get("organizer_email") or "").strip().lower()
        attendee = str(params.get("attendee_email") or "").strip().lower()
        if not (query or organizer or attendee):
            return items

        out = []
        for event in items:
            if query:
                haystack = " ".join(
                    str(event.get(k) or "") for k in ("subject", "preview", "location")
                ).lower()
                if query not in haystack:
                    continue
            if organizer and organizer not in str(event.get("organizer_email") or "").lower():
                continue
            if attendee:
                attendees = event.get("attendees") or []
                if not any(attendee in str(a.get("email") or "").lower() for a in attendees if isinstance(a, dict)):
                    continue
            out.append(event)
        return out

    def _normalize_calendar_event(self, item: dict) -> dict:
        def _dt(value):
            if isinstance(value, dict):
                return value.get("dateTime", "")
            return value or ""

        def _email(value):
            if not isinstance(value, dict):
                return "", ""
            ea = value.get("emailAddress") or {}
            return ea.get("address", "") or "", ea.get("name", "") or ""

        org_email, org_name = _email(item.get("organizer"))
        attendees = []
        for attendee in (item.get("attendees") or [])[:20]:
            email, name = _email(attendee)
            if email or name:
                attendees.append({
                    "email": email,
                    "name": name,
                    "type": attendee.get("type", ""),
                    "response": (attendee.get("status") or {}).get("response", ""),
                })

        location = item.get("location") or {}
        return {
            "id": item.get("id", ""),
            "subject": item.get("subject", "(no subject)"),
            "start": _dt(item.get("start")),
            "end": _dt(item.get("end")),
            "organizer_email": org_email,
            "organizer_name": org_name,
            "attendees": attendees,
            "location": location.get("displayName", "") if isinstance(location, dict) else "",
            "is_online_meeting": bool(item.get("isOnlineMeeting")),
            "preview": (item.get("bodyPreview") or "")[:240],
        }

    def _normalize_member(self, item: dict) -> dict:
        """Whitelist a Teams member (conversationMember) to non-PII identity fields
        (G5). Raw Graph member objects can carry phone/userPrincipalName/roles beyond
        what's needed to identify + message someone — strip everything else, never
        surface phone numbers."""
        return {
            "id": item.get("userId") or item.get("id", ""),
            "display_name": item.get("displayName", ""),
            "email": item.get("email") or "",
            "roles": item.get("roles") or [],
        }

    def _normalize_person(self, item: dict) -> dict:
        """Return only non-sensitive directory fields (Fix #25).

        Handles both /me/people (scoredEmailAddresses) and /v1.0/users (mail)
        response shapes — the fallback search uses /v1.0/users directly.
        Work email is retained (needed to actually message/email the person) but
        mobile/phone numbers are never surfaced.
        """
        # /me/people → scoredEmailAddresses; /v1.0/users fallback → mail field
        emails = item.get("scoredEmailAddresses") or []
        primary_email = ""
        if emails and isinstance(emails, list):
            primary_email = (emails[0] or {}).get("address", "")
        if not primary_email:
            primary_email = item.get("mail") or item.get("userPrincipalName", "")
        return {
            "id": item.get("id", ""),
            "display_name": item.get("displayName", ""),
            "job_title": item.get("jobTitle", ""),
            "department": item.get("department", ""),
            "email": primary_email,
            # NOTE: phones / mobile deliberately omitted — see docstring.
        }

    def _normalize_chat(self, item: dict) -> dict:
        """Normalize a Teams chat, exposing topic + member names for name-based
        lookup of GROUP chats (Fix #5/#17)."""
        members = []
        for m in (item.get("members") or []):
            nm = m.get("displayName") or m.get("email")
            if nm:
                members.append(nm)
        return {
            "id": item.get("id", ""),
            "chat_type": item.get("chatType", ""),
            "topic": item.get("topic") or "",
            "members": members,
            "last_updated": item.get("lastUpdatedDateTime", ""),
        }

    def _normalize_email(self, item: dict) -> dict:
        sender = item.get("from", {}).get("emailAddress", {})
        return {
            "id": item.get("id", ""),
            "subject": item.get("subject", "(no subject)"),
            "from": sender.get("address", ""),
            "from_name": sender.get("name", ""),
            "received_at": item.get("receivedDateTime", ""),
            "is_read": item.get("isRead", False),
            "preview": item.get("bodyPreview", ""),
            "has_attachments": item.get("hasAttachments", False),
        }

    @staticmethod
    def _clean_email_body(raw: str, content_type: str, max_chars: int = 12_000) -> str:
        """Strip HTML tags → plain text and truncate to max_chars.

        Large broadcast emails (CEO all-hands, newsletters) arrive as 15–40 KB of
        raw HTML. Passing that blob directly to to_context_str() causes the 8 000-char
        budget to fire immediately, dropping the body entirely and leaving the LLM
        with only the 255-char bodyPreview.

        This helper:
          1. Strips all HTML tags with a regex (stdlib only, no extra deps).
          2. Decodes common HTML entities (&nbsp; &amp; &lt; &gt; &quot;).
          3. Collapses runs of blank lines / whitespace to single newlines.
          4. Truncates at max_chars with a clear marker so the LLM knows more exists.
        """
        text = raw or ""
        if content_type and "html" in content_type.lower():
            # Remove <style> and <script> blocks first (they add noise, not content)
            text = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", " ", text,
                          flags=re.IGNORECASE | re.DOTALL)
            # Block-level tags → newline so paragraphs stay readable
            text = re.sub(r"<(br|p|div|tr|li|h[1-6])[^>]*>", "\n", text,
                          flags=re.IGNORECASE)
            # Strip all remaining tags
            text = re.sub(r"<[^>]+>", "", text)
            # Decode common HTML entities
            text = (text
                    .replace("&nbsp;", " ")
                    .replace("&amp;", "&")
                    .replace("&lt;", "<")
                    .replace("&gt;", ">")
                    .replace("&quot;", '"')
                    .replace("&#39;", "'"))
        # Collapse runs of whitespace / blank lines
        text = re.sub(r"\r\n|\r", "\n", text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... email truncated at {max_chars} chars — ask me to read a specific section if you need more]"
        return text

    def _normalize_email_full(self, item: dict) -> dict:
        base = self._normalize_email(item)
        body = item.get("body", {})
        raw_content = body.get("content", "")
        content_type = body.get("contentType", "text")
        base["body"] = self._clean_email_body(raw_content, content_type)
        base["content_type"] = content_type

        # Surface To/Cc so the agent can offer a correct Reply-All (Fix #16).
        def _addrs(key):
            out = []
            for r in (item.get(key) or []):
                a = (r or {}).get("emailAddress", {})
                if a.get("address"):
                    out.append(a["address"])
            return out
        base["to"] = _addrs("toRecipients")
        base["cc"] = _addrs("ccRecipients")
        return base

    def _normalize_teams_message(self, item: dict) -> dict:
        sender = item.get("from", {})
        user = sender.get("user", {})
        body = item.get("body", {})
        return {
            "id": item.get("id", ""),
            "from": user.get("displayName", ""),
            "from_email": user.get("id", ""),
            "content": body.get("content", ""),
            "content_type": body.get("contentType", "text"),
            "created_at": item.get("createdDateTime", ""),
            "message_type": item.get("messageType", "message"),
        }

microsoft365_adapter = Microsoft365Adapter()
