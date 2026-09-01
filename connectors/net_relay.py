# SPDX-License-Identifier: Apache-2.0
"""
Outbound HTTP relay for connectors.

In deployments where the gateway server has no direct internet egress, outbound
HTTP calls are relayed through the LLM proxy server (configured via LLM_PROXY_URL).
Cloud connectors (Microsoft 365 / Graph and the OAuth token endpoints) relay their
HTTP calls through the LLM proxy's `POST /net/forward`, exactly like Jira/Confluence
relay through /atlassian/proxy.

When LLM_PROXY_URL is unset (local dev, or a host with direct internet access),
calls go direct.

Returns a real httpx.Response in BOTH paths, so call sites use it transparently
(.status_code, .json(), .text, .content, .headers, .raise_for_status()).
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


def relay_request(
    method: str,
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    data: Optional[dict] = None,
    json: Optional[dict] = None,
    content: Optional[bytes] = None,
    timeout: float = 30.0,
) -> httpx.Response:
    """Make an outbound HTTP call, relaying through the LLM proxy server when
    LLM_PROXY_URL is set (for hosts without direct internet access). Returns an httpx.Response.

    `content` carries a raw binary body (e.g. OneDrive file upload); over the relay
    it is base64-encoded so the JSON forward channel stays text-safe.
    """
    proxy = os.environ.get("LLM_PROXY_URL", "").rstrip("/")
    method = method.upper()

    if not proxy:
        # Direct path — dev, or a host that already has internet egress.
        return httpx.request(
            method, url, headers=headers, params=params,
            data=data, json=json, content=content, timeout=timeout,
        )

    # Relay through the LLM proxy server (configured via LLM_PROXY_URL).
    payload: dict = {"method": method, "url": url, "timeout": timeout}
    if headers:           payload["headers"] = headers
    if params:            payload["params"] = params
    if data is not None:  payload["data"] = data
    if json is not None:  payload["json_body"] = json
    if content is not None:
        import base64 as _b64
        payload["content_b64"] = _b64.b64encode(content).decode("ascii")

    relay = httpx.post(f"{proxy}/net/forward", json=payload, timeout=timeout + 15.0)
    relay.raise_for_status()
    env = relay.json()

    # Reconstruct a real httpx.Response so callers are unaffected.
    return httpx.Response(
        status_code=int(env.get("status", 502)),
        headers={"content-type": env.get("content_type") or "application/json"},
        content=(env.get("text") or "").encode("utf-8"),
        request=httpx.Request(method, url),
    )
