# SPDX-License-Identifier: MIT
"""A stand-in for the ``ainxt`` binary, for deterministic tests.

Accepts the same argv the real CLI does and emits the same ``streaming-json``
NDJSON, so ``runner.run_cli_turn`` can be exercised end-to-end with no model
traffic, no network and no real binary. Point ``ABSTUDIO_CLI_PATH`` at a launcher
that runs this file.

Behaviour is driven by ``FAKE_CLI_SCENARIO``:

``ok``          stream some text, then a normal ``end``
``tools``       call an MCP tool over HTTP (reading the workspace config.toml),
                then report what came back — this is what proves the tool plane
``error``       emit an ``error`` event and exit non-zero
``crash``       exit non-zero with NO terminal event (tests the exit-code path)
``hang``        sleep past the timeout (tests kill-and-report)
``badjson``     emit unparseable noise, then a valid ``end``
``noflags``     mimic the real CLI rejecting an unknown flag (exit 2)

Why a fake CLI rather than log-based debugging: the previous attempt at this
feature was diagnosed by adding two commits of dense ``[CLI-DIAG]`` logging,
because there was no way to reproduce a failure deterministically. Every failure
mode above is one this integration must survive, and each is now a test.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def emit_text(text: str) -> None:
    emit({"type": "text", "data": text})


def emit_end(*, turns: int = 1, session_id: str = "fake-session-0001") -> None:
    emit({
        "type": "end",
        "stopReason": "EndTurn",
        "sessionId": session_id,
        "requestId": "fake-request-0001",
        "usage": {"input_tokens": 123, "output_tokens": 45},
        "num_turns": turns,
    })


def parse_args(argv: list) -> argparse.Namespace:
    """Mirror the real 0.2.101 surface, and reject anything outside it.

    This is the guard that keeps the integration honest: if someone adds a flag
    the deployed binary does not accept, the fake CLI fails the test the same way
    the real binary fails a run.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--single", dest="prompt", default="")
    parser.add_argument("--prompt-file", dest="prompt_file", default="")
    parser.add_argument("--output-format", default="plain")
    parser.add_argument("--model", default="")
    parser.add_argument("--permission-mode", default="default")
    parser.add_argument("--max-turns", type=int, default=0)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--allow", action="append", default=[])
    parser.add_argument("--deny", action="append", default=[])
    parser.add_argument("--tools", default="")
    parser.add_argument("--rules", default="")
    parser.add_argument("--verbatim", action="store_true")
    parser.add_argument("--no-plan", action="store_true")
    parser.add_argument("--no-subagents", action="store_true")
    parser.add_argument("--resume", nargs="?", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--json-schema", default="")
    parser.add_argument("--system-prompt-override", default="")
    parser.add_argument("--worktree", nargs="?", default=None)
    parser.add_argument("--sandbox", default="")
    parser.add_argument("--reasoning-effort", default="")
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        sys.stderr.write(f"error: unexpected argument '{unknown[0]}' found\n")
        sys.exit(2)
    return args


def read_mcp_config(cwd: str) -> tuple:
    """Return ``(url, auth_header)`` from the workspace's ``.ainxt/config.toml``.

    Deliberately parsed with ``tomllib`` rather than a regex so a malformed file
    fails the test instead of being silently tolerated.
    """
    path = os.path.join(cwd or ".", ".ainxt", "config.toml")
    if not os.path.isfile(path):
        return "", ""
    try:
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:
        sys.stderr.write(f"fake-cli: could not parse {path}: {exc}\n")
        return "", ""
    servers = data.get("mcp_servers") or {}
    for _name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url") or ""
        auth = (entry.get("headers") or {}).get("Authorization") or ""
        if url:
            return url, auth
    return "", ""


def rpc(url: str, auth: str, method: str, params: dict, msg_id: int) -> dict:
    """One JSON-RPC call over streamable HTTP, exactly as the real CLI does."""
    import urllib.error
    import urllib.request

    payload = json.dumps({
        "jsonrpc": "2.0", "id": msg_id, "method": method, "params": params,
    }).encode()
    req = urllib.request.Request(url, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", auth)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"error": {"code": exc.code, "message": exc.read().decode("utf-8", "replace")}}
    except Exception as exc:
        return {"error": {"code": -1, "message": str(exc)}}
    if not body.strip():
        return {}
    try:
        return json.loads(body)
    except Exception:
        return {"error": {"code": -32700, "message": "non-JSON response"}}


def scenario_tools(args: argparse.Namespace) -> int:
    """Do what the real CLI does: handshake, list tools, call one, report."""
    url, auth = read_mcp_config(args.cwd)
    if not url:
        emit({"type": "error", "message": "fake-cli: no MCP server configured"})
        return 1

    emit_text("Connecting to tools... ")
    init = rpc(url, auth, "initialize", {
        "protocolVersion": "2025-06-18",
        "clientInfo": {"name": "fake-cli", "version": "1.0"},
    }, 1)
    if "error" in init:
        emit({"type": "error", "message": f"fake-cli: initialize failed: {init['error']}"})
        return 1

    rpc(url, auth, "notifications/initialized", {}, 0)

    listed = rpc(url, auth, "tools/list", {}, 2)
    tools = ((listed.get("result") or {}).get("tools") or [])
    names = [t.get("name") for t in tools]
    emit_text(f"found {len(names)} tools: {', '.join(names)}. ")

    target = os.environ.get("FAKE_CLI_TOOL", "") or (names[0] if names else "")
    if not target:
        emit({"type": "error", "message": "fake-cli: no tools exposed"})
        return 1

    raw_args = os.environ.get("FAKE_CLI_TOOL_ARGS", "{}")
    try:
        tool_args = json.loads(raw_args)
    except Exception:
        tool_args = {}

    called = rpc(url, auth, "tools/call", {"name": target, "arguments": tool_args}, 3)
    result = called.get("result") or {}
    content = (result.get("content") or [{}])[0].get("text", "")
    emit_text(f"tool said: {content}")
    emit_end(turns=2)
    return 0


def main() -> int:
    if os.environ.get("FAKE_CLI_DEBUG_ARGV"):
        # Diagnostic hook: dump the received argv so an argv-construction bug is
        # visible immediately instead of surfacing as a confusing parse error.
        sys.stderr.write("ARGV=" + repr(sys.argv[1:]) + "\n")
    args = parse_args(sys.argv[1:])
    scenario = os.environ.get("FAKE_CLI_SCENARIO", "ok")

    if args.prompt_file and os.path.isfile(args.prompt_file):
        with open(args.prompt_file, encoding="utf-8") as fh:
            args.prompt = fh.read()

    if args.output_format != "streaming-json":
        # The integration always asks for streaming-json; anything else means the
        # argv builder regressed.
        sys.stderr.write(f"fake-cli: unexpected --output-format {args.output_format!r}\n")
        return 2

    if scenario == "ok":
        emit_text("Hello ")
        emit_text("from ")
        emit_text("the CLI.")
        emit_end()
        return 0

    if scenario == "tools":
        return scenario_tools(args)

    if scenario == "error":
        emit({"type": "error", "message": "fake-cli: simulated model failure"})
        return 1

    if scenario == "crash":
        emit_text("partial output")
        sys.stderr.write("fake-cli: simulated crash\n")
        return 1

    if scenario == "hang":
        emit_text("starting...")
        time.sleep(float(os.environ.get("FAKE_CLI_HANG_SECONDS", "60")))
        emit_end()
        return 0

    if scenario == "badjson":
        sys.stdout.write("not json at all\n")
        sys.stdout.write('{"type":"unknown_future_event","data":"ignore me"}\n')
        sys.stdout.flush()
        emit_text("recovered")
        emit_end()
        return 0

    if scenario == "noflags":
        sys.stderr.write("error: unexpected argument '--made-up' found\n")
        return 2

    emit({"type": "error", "message": f"fake-cli: unknown scenario {scenario!r}"})
    return 1


if __name__ == "__main__":
    sys.exit(main())
