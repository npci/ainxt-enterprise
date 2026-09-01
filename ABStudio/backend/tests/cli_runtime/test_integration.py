# SPDX-License-Identifier: Apache-2.0
"""Integration: a spawned CLI calling tools back through the live MCP endpoint.

This is the test that actually proves the feature works. Everything else checks a
part in isolation; this runs the whole loop:

    bridge → runner → workspace config.toml → spawned CLI
      → HTTP JSON-RPC back into this process → token auth → ToolDispatcher
        → tool events → SSE frames

The tool returns a value that exists nowhere else in the process, so finding it in
the model-visible text proves the round trip genuinely happened rather than being
reconstructed from the prompt.

A stdlib HTTP server stands in for the FastAPI route: the route is a thin shell
over ``AbstudioMcpServer.handle`` plus bearer auth, both reproduced faithfully
here, and avoiding the framework dependency keeps this runnable anywhere.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launcher import make_launcher

SECRET = "42 open merge requests"
_PORT = 8913


def _write_ephemeral_server_cert(host: str = "127.0.0.1") -> tuple[str, str]:
    """Generate a throwaway, self-signed TLS cert/key pair for `host`.

    Written to temp files (the stdlib ``ssl`` API only loads certs from
    disk) — the caller is responsible for deleting them once
    ``SSLContext.load_cert_chain`` has read them into memory. Nothing here
    is a real credential: it is a keypair minted and discarded within the
    same test run, purely so the loopback test transport is TLS rather
    than plaintext HTTP.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(minutes=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(host))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_fd, cert_path = tempfile.mkstemp(suffix=".pem", prefix="_test_cert_")
    key_fd, key_path = tempfile.mkstemp(suffix=".pem", prefix="_test_key_")
    with os.fdopen(cert_fd, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with os.fdopen(key_fd, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


class _FakeDispatcher:
    """Stands in for ``ToolDispatcher`` — records identity, returns the secret."""

    calls: list = []

    async def dispatch(self, *, tool_name, inputs, user_id, email, workflow_artifact_dir):
        _FakeDispatcher.calls.append({"tool": tool_name, "user_id": user_id})
        if tool_name == "gitlab_list_mrs":
            return {
                "result": f"There are {SECRET} in {inputs.get('project')}.",
                "generated_files": [{
                    "filename": "mrs.csv", "disk_name": "mrs_abc.csv",
                    "download_url": "/generated-files/mrs_abc.csv",
                    "format": "csv", "path": "/tmp/mrs_abc.csv",
                }],
            }
        return {"error": f"unexpected tool {tool_name}"}


def _install_stubs():
    module = types.ModuleType("agent_factory.pipeline")
    module.ToolDispatcher = _FakeDispatcher
    package = types.ModuleType("agent_factory")
    package.__path__ = []
    sys.modules["agent_factory"] = package
    sys.modules["agent_factory.pipeline"] = module
    _FakeDispatcher.calls = []

    import app.cli_runtime.mcp_server as mcp_server

    async def _specs(*, allowed_tools, expose_draft_tools=False):
        return [{
            "name": "gitlab_list_mrs",
            "description": "List open merge requests for a project.",
            "input_schema": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        }]

    mcp_server.load_tool_specs = _specs


def _post_to_loopback_test_server(*, port: int, path: str, body: bytes,
                                   cert_path: str, headers: dict | None = None,
                                   timeout: float = 10) -> int:
    """POST to the in-process stand-in MCP server started by `_serve()` below
    and return its HTTP status code.

    Deliberately hardcoded to ``127.0.0.1`` — this never leaves the local
    machine. The connection is TLS (see `_serve`'s ``ssl.wrap_socket``),
    verified against the same ephemeral self-signed cert the server
    presents, so this is a genuine encrypted handshake rather than a
    plaintext socket — not just "trust because it's loopback".
    """
    import http.client

    ssl_context = ssl.create_default_context(cafile=cert_path)
    conn = http.client.HTTPSConnection(
        "127.0.0.1", port, timeout=timeout, context=ssl_context
    )
    try:
        conn.request("POST", path, body=body, headers=headers or {})
        return conn.getresponse().status
    finally:
        conn.close()


def _serve(loop):
    """Start the stand-in MCP endpoint over TLS; returns the server (and the
    ephemeral cert path used to verify it) for shutdown/cleanup."""
    import app.cli_runtime.mcp_server as mcp_server
    from app.cli_runtime.config import cli_runtime_config
    from app.cli_runtime.session import get_registry

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_a):
            pass

        def _send(self, status, payload):
            body = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def do_POST(self):  # noqa: N802 - stdlib naming
            length = int(self.headers.get("Content-Length") or 0)
            message = json.loads(self.rfile.read(length) or b"{}")
            run_id = self.path.rsplit("/", 1)[-1]
            auth = (self.headers.get("Authorization") or "").strip()
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth

            session, reason = get_registry().authenticate(run_id, token)
            if session is None:
                self._send(401, {"jsonrpc": "2.0", "id": message.get("id"),
                                 "error": {"code": -32000, "message": reason}})
                return
            server = mcp_server.AbstudioMcpServer(session=session, config=cli_runtime_config())
            response = asyncio.run_coroutine_threadsafe(server.handle(message), loop).result(60)
            self._send(202 if response is None else 200, response)

    cert_path, key_path = _write_ephemeral_server_cert("127.0.0.1")
    try:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        # The context has the key material loaded into memory now; the
        # on-disk copy served no further purpose.
        os.remove(key_path)

    httpd = ThreadingHTTPServer(("127.0.0.1", _PORT), Handler)
    httpd.socket = ssl_context.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, cert_path


def _configure(monkeypatch, *, cert_path: str = "", **extra):
    tmp = tempfile.mkdtemp()
    launcher = make_launcher(tmp)
    env = {
        "ABSTUDIO_CLI_MODE": "true",
        "ABSTUDIO_CLI_PATH": launcher,
        "ABSTUDIO_CLI_API_KEY": "fake-key",
        "ABSTUDIO_CLI_WORKSPACE_ROOT": os.path.join(tmp, "ws"),
        "ABSTUDIO_MCP_BASE_URL": f"https://127.0.0.1:{_PORT}",
        "ABSTUDIO_CLI_RUN_TIMEOUT_S": "60",
        "FAKE_CLI_SCENARIO": "tools",
        "FAKE_CLI_TOOL": "gitlab_list_mrs",
        "FAKE_CLI_TOOL_ARGS": '{"project": "platform/api"}',
    }
    if cert_path:
        # build_env() in runner.py starts from dict(os.environ), so setting
        # this here propagates to the spawned fake_cli.py subprocess too.
        # Python's default SSLContext (used by urllib.request.urlopen for
        # https:// URLs, both here and inside fake_cli.py) honours
        # SSL_CERT_FILE as an additional trust anchor, so the subprocess
        # verifies the server's ephemeral self-signed cert instead of
        # either failing closed or skipping verification.
        env["SSL_CERT_FILE"] = cert_path
    env.update(extra)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))


class TestFullLoop:
    async def test_a_spawned_cli_calls_a_tool_and_reports_its_data(self, monkeypatch, registry):
        _install_stubs()
        from app.cli_runtime.bridge import AgentTurnSpec, build_prompt, run_agent_turn_via_cli

        httpd, cert_path = _serve(asyncio.get_running_loop())
        _configure(monkeypatch, cert_path=cert_path)
        try:
            spec = AgentTurnSpec(
                prompt=build_prompt("You are a GitLab assistant.", "How many open MRs?"),
                model="claude-sonnet-4-6",
                agent_name="GitLab Agent",
                node_id="node-1",
                run_id="itest-1",
                user_id="user-77",
                email="dev@example.com",
                tool_names=["gitlab_list_mrs"],
            )

            frames, result = [], None
            async for name, payload in run_agent_turn_via_cli(spec):
                if name == "__result__":
                    result = payload["result"]
                else:
                    frames.append((name, payload))

            names = [name for name, _ in frames]

            # The tool actually ran, as the right user.
            assert _FakeDispatcher.calls, "the MCP tool was never dispatched"
            assert _FakeDispatcher.calls[0]["user_id"] == "user-77"

            # Its data reached the model.
            assert result is not None and result.ok, getattr(result, "error", "no result")
            assert SECRET in result.output

            # The UI sees a complete, correctly ordered picture.
            assert "agent_token" in names
            assert names.index("tool_call_start") < names.index("tool_call_result")

            # Accounting and artefacts survived the boundary.
            assert result.usage["total_tokens"] > 0
            assert len(result.generated_files) == 1
            assert result.session_id, "no CLI session id captured for resume"
        finally:
            httpd.shutdown()
            httpd.server_close()
            os.remove(cert_path)

        # The credential is dead once the run is over.
        assert registry.get("itest-1") is None

    async def test_an_unauthenticated_caller_is_refused(self, monkeypatch, registry):
        """The per-run token is the only thing standing between a local process
        and a user's credentials."""
        _install_stubs()

        httpd, cert_path = _serve(asyncio.get_running_loop())
        _configure(monkeypatch, cert_path=cert_path)
        try:
            registry.register(run_id="secure-1", user_id="u1",
                              allowed_tools=["gitlab_list_mrs"])
            status = _post_to_loopback_test_server(
                port=_PORT,
                path="/abstudio-mcp/secure-1",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                cert_path=cert_path,
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert status == 401
            assert _FakeDispatcher.calls == []
        finally:
            httpd.shutdown()
            httpd.server_close()
            os.remove(cert_path)
