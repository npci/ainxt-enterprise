# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt MCP TOOL REGISTRY  (Phase 10)
# Central registry for all executable tools on the platform.
#
# A "tool" is any callable that the agent or workflow engine
# can invoke by name. Tools are registered with metadata so
# the orchestrator and agent builder can discover them.
#
# Usage:
#   tool_registry.register(ToolDefinition(...))
#   tool_registry.execute("retrieve", input="What is UPI?")
#   tool_registry.discover(tag="retrieval")
# ============================================================

import ipaddress
import math
import os
import re as _re_url
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from core.logger import logger
from core.config import RDB_REGISTRY
from core.kv import get_kv, KVError


# ── SEC-01: SSRF guard ───────────────────────────────────────────────────────
# Allowlist of approved URL prefixes for HTTP tool endpoints.
# Override via TOOL_ENDPOINT_ALLOWLIST env var (comma-separated prefixes).
_DEFAULT_ENDPOINT_ALLOWLIST = [
    "https://",   # only HTTPS by default; add specific internal prefixes as needed
]
_ENDPOINT_ALLOWLIST: Optional[List[str]] = None

# Private / link-local CIDR ranges that must never be reached
_PRIVATE_CIDRS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / AWS IMDS
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_BLOCKED_SCHEMES = {"file", "gopher", "ftp", "data", "javascript"}


def _get_endpoint_allowlist() -> List[str]:
    global _ENDPOINT_ALLOWLIST
    if _ENDPOINT_ALLOWLIST is None:
        raw = os.getenv("TOOL_ENDPOINT_ALLOWLIST", "")
        if raw.strip():
            _ENDPOINT_ALLOWLIST = [p.strip() for p in raw.split(",") if p.strip()]
        else:
            _ENDPOINT_ALLOWLIST = _DEFAULT_ENDPOINT_ALLOWLIST
    return _ENDPOINT_ALLOWLIST


def _validate_tool_endpoint(url: str) -> None:
    """
    Raise ValueError if url is unsafe (SSRF guard — SEC-01).

    Checks:
    1. Scheme must not be in _BLOCKED_SCHEMES
    2. URL must start with an approved prefix from the allowlist
    3. Hostname must not resolve to a private/link-local IP range
    """
    if not url:
        return  # empty endpoint is fine (fn-based tool)

    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()

    if scheme in _BLOCKED_SCHEMES:
        raise ValueError(f"Tool endpoint scheme {scheme!r} is not permitted")

    allowlist = _get_endpoint_allowlist()
    if not any(url.startswith(prefix) for prefix in allowlist):
        raise ValueError(
            f"Tool endpoint {url!r} does not match any approved prefix. "
            f"Approved prefixes: {allowlist}"
        )

    # Resolve hostname and check against private ranges
    hostname = parsed.hostname or ""
    if hostname:
        try:
            addr = ipaddress.ip_address(hostname)
            for cidr in _PRIVATE_CIDRS:
                if addr in cidr:
                    raise ValueError(
                        f"Tool endpoint hostname {hostname!r} resolves to a "
                        f"private/reserved address ({addr}) — SSRF risk"
                    )
        except ValueError as ve:
            # Re-raise SSRF errors; ignore "not a valid IP" (hostname, not IP literal)
            if "SSRF" in str(ve) or "private" in str(ve).lower():
                raise
            # Hostname is a DNS name — we cannot resolve it here without a network call.
            # Log a warning; runtime validation happens at call time via the HTTP client.
            logger.debug(f"_validate_tool_endpoint: {hostname!r} is a DNS name, skipping IP check")


# ── SEC-02: tool-name allow-list ─────────────────────────────────────────────
# Tool names loaded from Postgres (register_db_tools) or a live API call
# (hot_register) come from untrusted input — production has previously stored
# rows such as `<script>alert(document.domain)</script>`,
# `' OR username LIKE 'admin%'--`, `<h1>Hello</h1>`, and an empty string as
# MCPServer.name (QA/pen-test artifacts that were never cleaned up). Every
# such row was registered as a live tool and shown wherever tool names are
# rendered (discovery lists, agent tool pickers, marketplace stats) with no
# sanitisation — a stored-XSS / identifier-injection risk. A tool name is a
# machine identifier, never freeform user text, so it is safe to constrain to
# a strict charset: letters, digits, underscore, hyphen, and dot (the last for
# namespaced names like "jira.create_issue"), 1-128 chars, must start with a
# letter or digit (rejects "" and pure-punctuation strings).
_TOOL_NAME_RE = _re_url.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _validate_tool_name(name: str) -> str:
    """Return ``name`` unchanged if it is a safe tool identifier, else raise
    ValueError. Callers that load names from an untrusted source (DB rows,
    API payloads) MUST call this before constructing a ToolDefinition."""
    name = (name or "").strip()
    if not _TOOL_NAME_RE.match(name):
        raise ValueError(
            f"Tool name {name!r} is not a valid identifier — only letters, "
            f"digits, '_', '-', '.' are allowed (1-128 chars, must start "
            f"with a letter or digit)"
        )
    return name


# ============================================================
# MARKETPLACE STATS (DB=3 — same as marketplace_router)
# Backend selected via REDIS_CLIENT_CONFIG_DB3.
# ============================================================

_stats_rc = None

def _get_stats_redis():
    global _stats_rc
    if _stats_rc is None:
        try:
            c = get_kv(RDB_REGISTRY, decode_responses=True)
            c.ping()
            _stats_rc = c
        except KVError:
            pass
    return _stats_rc


def _record_tool_stat(tool_name: str, error: bool) -> None:
    """Write call + optional error + last_used to marketplace:stats:tool:<name>."""
    try:
        rc = _get_stats_redis()
        if rc is None:
            return
        key = f"marketplace:stats:tool:{tool_name}"
        rc.hincrby(key, "calls", 1)
        if error:
            rc.hincrby(key, "errors", 1)
        rc.hset(key, "last_used", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    except Exception:
        pass  # stats are non-critical


# ============================================================
# TOOL DEFINITION
# ============================================================

@dataclass
class ToolDefinition:
    """
    Metadata + callable for a single platform tool.

    Fields:
        name          Unique tool name (snake_case).
        description   What the tool does (shown in discovery).
        fn            The Python callable to invoke (may be None for HTTP tools).
        tags          Discovery tags e.g. ["retrieval", "pci", "docker"].
        input_schema  JSON-schema dict describing expected input (optional).
        version       Semver string, default "1.0.0".
        author        Who registered the tool.
        enabled       Tools can be disabled without unregistering.
        http_endpoint Optional URL of a remote MCP tool server (Phase 13).
                      When set, execute() POSTs the kwargs as JSON to this URL
                      instead of calling fn.
    """
    name:          str
    description:   str
    fn:            Optional[Callable] = None
    tags:          List[str]          = field(default_factory=list)
    input_schema:  Dict[str, Any]     = field(default_factory=dict)
    version:       str                = "1.0.0"
    author:        str                = "platform"
    enabled:       bool               = True
    http_endpoint: Optional[str]      = None
    # ── P2: per-tool execution config ────────────────────────────────────────
    timeout_sec:   int                = 30    # wall-clock timeout per execution
    retry_count:   int                = 0     # retry on transient failure (0 = no retry)
    is_write_op:   bool               = False # write ops are excluded from parallel batches


# ============================================================
# EXECUTION RESULT
# ============================================================

@dataclass
class ToolResult:
    tool_name:  str
    success:    bool
    output:     Any
    error:      Optional[str]
    duration_ms: float
    executed_at: str


# ============================================================
# TOOL REGISTRY
# ============================================================

class ToolRegistry:
    """
    Thread-safe in-memory registry for platform tools.

    Responsibilities:
    - Store and retrieve ToolDefinitions by name
    - Discover tools by tag
    - Execute tools safely with timing + error capture
    - Prevent duplicate registrations (warn + overwrite)
    """

    def __init__(self):
        self._tools: Dict[str, ToolDefinition] = {}
        logger.info("ToolRegistry initialised")

    # --------------------------------------------------------
    # REGISTER
    # --------------------------------------------------------

    def register(self, tool: ToolDefinition) -> None:
        """Register a tool. Overwrites if name already exists."""
        if tool.name in self._tools:
            logger.warning(
                f"ToolRegistry: overwriting existing tool {tool.name!r}"
            )
        self._tools[tool.name] = tool
        logger.info(
            f"ToolRegistry: registered {tool.name!r} "
            f"tags={tool.tags} v{tool.version}"
        )

    def unregister(self, name: str) -> bool:
        """Remove a tool. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            logger.info(f"ToolRegistry: unregistered {name!r}")
            return True
        return False

    def enable(self, name: str) -> None:
        self._get_or_raise(name).enabled = True

    def disable(self, name: str) -> None:
        self._get_or_raise(name).enabled = False

    # --------------------------------------------------------
    # QUERY
    # --------------------------------------------------------

    def get(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_all(self, enabled_only: bool = True) -> List[ToolDefinition]:
        tools = list(self._tools.values())
        if enabled_only:
            tools = [t for t in tools if t.enabled]
        return tools

    def discover(
        self,
        tag: Optional[str] = None,
        query: Optional[str] = None,
    ) -> List[ToolDefinition]:
        """
        Find tools by tag and/or keyword search in name/description.
        Both filters are ANDed when both are provided.
        """
        results = self.list_all()

        if tag:
            results = [t for t in results if tag.lower() in t.tags]

        if query:
            q = query.lower()
            results = [
                t for t in results
                if q in t.name.lower() or q in t.description.lower()
            ]

        return results

    def names(self) -> List[str]:
        return list(self._tools.keys())

    # --------------------------------------------------------
    # EXECUTE
    # --------------------------------------------------------

    def execute(self, tool_name: str, *args, current_user: dict | None = None, **kwargs) -> ToolResult:
        """
        Execute a registered tool by name.
        Captures timing, errors, and returns a ToolResult.
        Never raises — errors are captured in ToolResult.error.

        current_user: optional JWT-decoded user dict (sub, department).
            When provided, web-search tools are subject to governance and budget gating
            before execution. Non-search tools are unaffected.
        """
        from core.proxy_tool_use import (
            _WEB_SEARCH_TOOL_NAMES,
            _execute_with_web_search_governance,
        )

        tool = self._tools.get(tool_name)

        if tool is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=f"Tool {tool_name!r} not found in registry",
                duration_ms=0.0,
                executed_at=datetime.utcnow().isoformat(),
            )

        if not tool.enabled:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=f"Tool {tool_name!r} is disabled",
                duration_ms=0.0,
                executed_at=datetime.utcnow().isoformat(),
            )

        # ── Web-search governance gate ────────────────────────────────────────
        # Intercept before any execution path (HTTP or fn) so governance is
        # enforced regardless of how the tool is backed.
        if tool_name in _WEB_SEARCH_TOOL_NAMES:
            started_gate = datetime.utcnow()

            def _tool_executor_shim(name: str, inputs: dict):
                # Delegate to the actual tool execution below (HTTP or fn).
                # We call self._execute_direct() to skip the governance wrapper
                # and avoid infinite recursion.
                return self._execute_direct(name, inputs)

            _req_id = kwargs.pop("_request_id", "") or ""
            _model  = kwargs.pop("_model", "") or ""
            try:
                output = _execute_with_web_search_governance(
                    request_id=_req_id,
                    model=_model,
                    tool_name=tool_name,
                    tool_inputs=kwargs,
                    tool_executor=_tool_executor_shim,
                    current_user=current_user,
                )
                duration = (datetime.utcnow() - started_gate).total_seconds() * 1000
                _record_tool_stat(tool_name, error=False)
                return ToolResult(
                    tool_name=tool_name,
                    success=True,
                    output=output,
                    error=None,
                    duration_ms=duration,
                    executed_at=started_gate.isoformat(),
                )
            except Exception as e:
                duration = (datetime.utcnow() - started_gate).total_seconds() * 1000
                logger.error(f"ToolRegistry: web-search governance gate for {tool_name!r} raised {e}")
                _record_tool_stat(tool_name, error=True)
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    output=None,
                    error=str(e),
                    duration_ms=duration,
                    executed_at=started_gate.isoformat(),
                )

        started = datetime.utcnow()

        # Phase 13: distributed HTTP execution
        if tool.http_endpoint:
            try:
                import json as _json
                import urllib.request as _urllib
                # SEC-01: re-validate at call time (endpoint may have been set before guard existed)
                _validate_tool_endpoint(tool.http_endpoint)
                payload = _json.dumps(kwargs).encode()
                req = _urllib.Request(
                    tool.http_endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = _urllib.urlopen(req, timeout=tool.timeout_sec)
                try:
                    raw = resp.read().decode()
                    try:
                        output = _json.loads(raw)
                    except Exception:
                        output = raw
                finally:
                    resp.close()
                duration = (datetime.utcnow() - started).total_seconds() * 1000
                logger.info(f"ToolRegistry: HTTP tool {tool_name!r} OK in {duration:.1f}ms")
                _record_tool_stat(tool_name, error=False)
                return ToolResult(
                    tool_name=tool_name,
                    success=True,
                    output=output,
                    error=None,
                    duration_ms=duration,
                    executed_at=started.isoformat(),
                )
            except Exception as e:
                duration = (datetime.utcnow() - started).total_seconds() * 1000
                logger.error(f"ToolRegistry: HTTP tool {tool_name!r} failed → {e}")
                _record_tool_stat(tool_name, error=True)
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    output=None,
                    error=str(e),
                    duration_ms=duration,
                    executed_at=started.isoformat(),
                )

        if tool.fn is None:
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=f"Tool {tool_name!r} has no callable and no http_endpoint",
                duration_ms=0.0,
                executed_at=datetime.utcnow().isoformat(),
            )

        try:
            output = tool.fn(*args, **kwargs)
            duration = (datetime.utcnow() - started).total_seconds() * 1000
            logger.info(
                f"ToolRegistry: executed {tool_name!r} "
                f"in {duration:.1f}ms success=True"
            )
            _record_tool_stat(tool_name, error=False)
            return ToolResult(
                tool_name=tool_name,
                success=True,
                output=output,
                error=None,
                duration_ms=duration,
                executed_at=started.isoformat(),
            )
        except Exception as e:
            duration = (datetime.utcnow() - started).total_seconds() * 1000
            logger.error(f"ToolRegistry: {tool_name!r} raised {e}")
            _record_tool_stat(tool_name, error=True)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                output=None,
                error=str(e),
                duration_ms=duration,
                executed_at=started.isoformat(),
            )

    # --------------------------------------------------------
    # P2: TOOL RANKING
    # --------------------------------------------------------

    def rank_tools(
        self,
        query: str,
        candidates: Optional[List[ToolDefinition]] = None,
    ) -> List[ToolDefinition]:
        """
        Rank tools by TF-IDF cosine similarity between the query tokens and
        each tool's name + description + tags.  No LLM call — pure token math.
        Falls back to original order on any error.

        Args:
            query:      The user's goal / question string.
            candidates: Subset to rank. Defaults to all enabled tools.

        Returns:
            Ranked list (highest relevance first), capped at 15 tools.
        """
        tools = candidates if candidates is not None else self.list_all()
        if not tools or not query:
            return tools[:15]
        try:
            import re as _re
            _tok = lambda s: _re.findall(r"[a-z0-9]+", s.lower())
            q_tokens = _tok(query)
            if not q_tokens:
                return tools[:15]

            # Build corpus: one "document" per tool
            docs = [" ".join(_tok(t.name + " " + t.description + " " + " ".join(t.tags)))
                    for t in tools]

            # Term frequency per doc
            def _tf(tokens: list) -> dict:
                freq: dict = {}
                for tok in tokens:
                    freq[tok] = freq.get(tok, 0) + 1
                total = max(len(tokens), 1)
                return {k: v / total for k, v in freq.items()}

            # IDF across corpus
            all_toks = set(q_tokens)
            for doc in docs:
                all_toks.update(_tok(doc))
            N = len(docs)
            idf: dict = {}
            for tok in all_toks:
                df = sum(1 for doc in docs if tok in _tok(doc))
                idf[tok] = math.log((N + 1) / (df + 1)) + 1.0

            q_tf = _tf(q_tokens)
            q_vec = {tok: q_tf.get(tok, 0) * idf.get(tok, 1.0) for tok in q_tokens}
            q_norm = math.sqrt(sum(v * v for v in q_vec.values())) or 1.0

            scores: List[Tuple[float, ToolDefinition]] = []
            for tool, doc in zip(tools, docs):
                d_tokens = _tok(doc)
                d_tf = _tf(d_tokens)
                dot = sum(q_vec.get(tok, 0) * d_tf.get(tok, 0) * idf.get(tok, 1.0)
                          for tok in q_tokens)
                d_norm = math.sqrt(sum((d_tf.get(tok, 0) * idf.get(tok, 1.0)) ** 2
                                       for tok in d_tokens)) or 1.0
                scores.append((dot / (q_norm * d_norm), tool))

            scores.sort(key=lambda x: x[0], reverse=True)
            return [t for _, t in scores[:15]]
        except Exception as _e:
            logger.debug(f"ToolRegistry.rank_tools fallback (error: {_e})")
            return tools[:15]

    # --------------------------------------------------------
    # P2: PARALLEL EXECUTION
    # --------------------------------------------------------

    def execute_parallel(
        self,
        tool_calls: List[Tuple[str, Dict]],
        max_workers: int = 4,
    ) -> List[ToolResult]:
        """
        Execute a batch of independent tool calls in parallel using ThreadPoolExecutor.

        Safety rules:
        - Tools with ``is_write_op=True`` are executed sequentially AFTER all
          read-only tools complete (prevents interleaved writes).
        - Per-tool ``timeout_sec`` is respected via future.result(timeout=...).
        - Per-tool ``retry_count`` is honoured with exponential back-off.
        - Results are returned in the SAME ORDER as ``tool_calls``.

        Args:
            tool_calls:  List of (tool_name, kwargs_dict) pairs.
            max_workers: Thread pool size (default 4).

        Returns:
            List[ToolResult] in the same order as tool_calls.
        """
        if not tool_calls:
            return []

        # Split into read-only (parallelisable) and write ops (sequential)
        read_calls:  List[Tuple[int, str, Dict]] = []
        write_calls: List[Tuple[int, str, Dict]] = []
        for idx, (name, kwargs) in enumerate(tool_calls):
            tool = self._tools.get(name)
            if tool and tool.is_write_op:
                write_calls.append((idx, name, kwargs))
            else:
                read_calls.append((idx, name, kwargs))

        results: Dict[int, ToolResult] = {}

        def _execute_with_retry(idx: int, name: str, kwargs: Dict) -> None:
            tool = self._tools.get(name)
            timeout = tool.timeout_sec if tool else 30
            retries = tool.retry_count if tool else 0
            last_result: Optional[ToolResult] = None
            for attempt in range(retries + 1):
                if attempt > 0:
                    time.sleep(min(0.5 * (2 ** (attempt - 1)), 8.0))  # exp back-off, cap 8s
                last_result = self.execute(name, **kwargs)
                if last_result.success:
                    break
            results[idx] = last_result  # type: ignore[assignment]

        # Execute read-only tools in parallel
        if read_calls:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(read_calls))) as pool:
                futures = {
                    pool.submit(_execute_with_retry, idx, name, kwargs): idx
                    for idx, name, kwargs in read_calls
                }
                for future in as_completed(futures):
                    try:
                        future.result(timeout=60)  # outer safety net
                    except Exception as _fe:
                        idx = futures[future]
                        name = tool_calls[idx][0]
                        results[idx] = ToolResult(
                            tool_name=name, success=False, output=None,
                            error=f"Parallel execution error: {_fe}",
                            duration_ms=0.0, executed_at=datetime.utcnow().isoformat(),
                        )

        # Execute write ops sequentially (preserves ordering guarantees)
        for idx, name, kwargs in write_calls:
            _execute_with_retry(idx, name, kwargs)

        return [results[i] for i in range(len(tool_calls))]

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    # --------------------------------------------------------
    # HOT LOADING — DB-backed tools (G10)
    # --------------------------------------------------------

    def register_db_tools(self) -> int:
        """
        Load all PRODUCTION MCPServer records from Postgres and register
        them as HTTP tools in this registry.  Called once at startup by
        MCPRegistry._bootstrap() so every enabled server row is immediately
        available to the agent / workflow engine without a restart.

        Returns the number of tools successfully loaded.
        """
        loaded = 0
        try:
            from db.database import SessionLocal
            from db.models import MCPServer
            db = SessionLocal()
            try:
                servers = (
                    db.query(MCPServer)
                    .filter(MCPServer.status == "PRODUCTION", MCPServer.enabled.is_(True))
                    .all()
                )
                for srv in servers:
                    try:
                        # SEC-02: srv.name is an untrusted DB row — validate it
                        # BEFORE constructing the ToolDefinition so an unsafe
                        # name never reaches self.register() / discovery /
                        # marketplace stats (all of which render tool names
                        # verbatim). Production has previously carried
                        # QA/pen-test rows such as
                        # `<script>alert(document.domain)</script>` and
                        # `' OR username LIKE 'admin%'--` as tool names here.
                        # (Endpoint is intentionally NOT run through the
                        # HTTPS-only _validate_tool_endpoint SSRF guard in this
                        # DB-loaded path — unlike hot_register(), this has
                        # never enforced that, and legitimate internal MCP
                        # servers here may use non-HTTPS endpoints; tightening
                        # that is a separate, endpoint-allowlist change.)
                        _safe_name = _validate_tool_name(srv.name)
                        defn = ToolDefinition(
                            name=_safe_name,
                            description=f"[DB-loaded] MCP server tool: {_safe_name}",
                            fn=None,
                            http_endpoint=srv.endpoint or "",
                            tags=["mcp", "db-loaded"],
                            version="1.0.0",
                            author=srv.created_by or "platform",
                            enabled=srv.enabled,
                        )
                        self.register(defn)
                        loaded += 1
                    except Exception as e:
                        logger.warning(
                            f"ToolRegistry.register_db_tools: skipping {srv.name!r} → {e}"
                        )
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"ToolRegistry.register_db_tools: DB unavailable → {e}")
        logger.info(f"ToolRegistry.register_db_tools: loaded {loaded} tool(s) from Postgres")
        return loaded

    def hot_register(self, tool_data: dict) -> ToolDefinition:
        """
        Register a tool live from a plain dict — no restart required.

        Expected keys (all str unless noted):
            name          — unique tool name (required)
            description   — human-readable description
            endpoint_url  — remote HTTP endpoint for this tool
            url           — alias for endpoint_url (marketplace compat)
            auth_type     — e.g. "none" | "bearer" | "api_key" (stored as tag)
            tags          — list[str] of discovery tags (optional)
            input_schema  — JSON-schema dict (optional)
            version       — semver string (optional, default "1.0.0")
            author        — who registered it (optional)

        Note: this path already sits behind routers/marketplace_router.py's
        validate_tool_register_request()/validate_identifier() XSS + dangerous-
        char check before the caller ever reaches here, and user-facing
        marketplace tool names may legitimately contain spaces/punctuation
        that check allows — so it does NOT run the stricter _validate_tool_name
        charset check applied to register_db_tools() (see that method's
        comment). If a second untrusted caller of hot_register() is added
        later without that upstream validation, revisit this.

        Returns the registered ToolDefinition.
        """
        name = tool_data.get("name", "").strip()
        if not name:
            raise ValueError("hot_register: 'name' is required")

        endpoint = (
            tool_data.get("endpoint_url")
            or tool_data.get("url")
            or ""
        )
        auth_type = tool_data.get("auth_type", "none")
        tags = list(tool_data.get("tags") or [])
        if "mcp" not in tags:
            tags.append("mcp")
        if auth_type and auth_type != "none":
            tags.append(f"auth:{auth_type}")

        # SEC-01: validate endpoint before storing
        try:
            _validate_tool_endpoint(endpoint)
        except ValueError as _ve:
            raise ValueError(f"hot_register: unsafe endpoint for {name!r}: {_ve}") from _ve

        defn = ToolDefinition(
            name=name,
            description=tool_data.get("description") or f"Hot-registered MCP tool: {name}",
            fn=None,
            http_endpoint=endpoint,
            tags=tags,
            input_schema=tool_data.get("input_schema") or {},
            version=tool_data.get("version") or "1.0.0",
            author=tool_data.get("author") or "api",
            enabled=True,
        )
        self.register(defn)
        logger.info(f"ToolRegistry.hot_register: live-registered {name!r} endpoint={endpoint!r}")
        return defn

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def _execute_direct(self, tool_name: str, inputs: dict):
        """Execute a tool's underlying fn or HTTP endpoint, bypassing the governance wrapper.

        Only called from within the web-search governance shim to avoid infinite recursion.
        All other callers must go through execute().

        For web-search tool names: always routes through /llm/web-search on the proxy
        (LLM proxy server) regardless of whether the tool has an http_endpoint or fn, because
        the LLM proxy server is the only server with internet egress.
        """
        from core.proxy_tool_use import _WEB_SEARCH_TOOL_NAMES
        if tool_name in _WEB_SEARCH_TOOL_NAMES:
            # Web-search tools must always execute on the LLM proxy server via the proxy endpoint.
            # _execute_with_web_search_governance() handles the actual proxy call;
            # this path should not be reached for search tools, but guard defensively.
            raise RuntimeError(
                f"Web-search tool {tool_name!r} must be executed via /llm/web-search "
                f"on the proxy server, not directly. This is a code path error."
            )

        tool = self._tools.get(tool_name)
        if tool is None:
            raise KeyError(f"Tool {tool_name!r} not found")
        if not tool.enabled:
            raise RuntimeError(f"Tool {tool_name!r} is disabled")

        if tool.http_endpoint:
            import json as _json
            import urllib.request as _urllib
            _validate_tool_endpoint(tool.http_endpoint)
            payload = _json.dumps(inputs).encode()
            req = _urllib.Request(
                tool.http_endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            import contextlib
            with contextlib.closing(_urllib.urlopen(req, timeout=tool.timeout_sec)) as resp:
                raw = resp.read().decode()
                try:
                    return _json.loads(raw)
                except Exception:
                    return raw

        if tool.fn is None:
            raise RuntimeError(f"Tool {tool_name!r} has no callable and no http_endpoint")

        return tool.fn(**inputs)

    def _get_or_raise(self, name: str) -> ToolDefinition:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Tool {name!r} not found")
        return tool

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# ============================================================
# SINGLETON
# ============================================================

tool_registry = ToolRegistry()
