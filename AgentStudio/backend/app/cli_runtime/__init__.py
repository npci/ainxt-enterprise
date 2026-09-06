# SPDX-License-Identifier: MIT
"""cli_runtime — route ABStudio agent execution through the headless ``ainxt`` CLI.

Every agent turn (single agent chat AND workflow agent node) is executed by a
spawned ``ainxt`` CLI subprocess instead of the in-process LLM loop, gated by a
single flag: ``ABSTUDIO_CLI_MODE``.

Module map
----------
``config``       env knobs + preflight (binary present, API key set, flag state)
``session``      per-run registry: identity, tool/skill scope, token, event bus
``mcp_server``   JSON-RPC handlers exposing the tool catalog over MCP
``mcp_router``   FastAPI streamable-HTTP endpoint the spawned CLI connects back to
``workspace``    per-run working dir, ``.ainxt/config.toml``, git clone, TTL sweep
``runner``       spawn / concurrency cap / timeout / cancellation / event parse
``event_mapper`` CLI + tool events → ABStudio's existing SSE vocabulary

Why an in-process HTTP MCP server (and not a stdio sidecar)
-----------------------------------------------------------
The CLI reaches back into THIS FastAPI process over ``127.0.0.1``. That means the
tool plane reuses the live Postgres pool, the warmed tool catalog and the same
``ToolDispatcher`` the native engine uses — so per-user vault credentials, the
``python -I`` sandbox, transient retries and the audit trail all keep working
unchanged. A stdio sidecar would instead cold-start a fresh interpreter per run
(the earlier attempt needed ``startup_timeout_sec = 60``), bootstrap ``sys.path``
and open a second DB connection.

It also solves an otherwise fatal gap: ``--output-format streaming-json`` emits
only ``text`` / ``thought`` / ``end`` / ``error`` — there are NO tool-call events
on stdout. Because every tool call is served by us, the MCP layer is what
publishes ``tool_call_start`` / ``tool_call_result`` to the UI.

Verified against ``ainxt 0.2.101``
----------------------------------
* Repo-local (project-scope) MCP servers are SILENTLY SKIPPED in an untrusted
  folder. ``AINXT_FOLDER_TRUST=0`` in the child env is mandatory — without it the
  CLI starts with zero ABStudio tools and no error.
* ``--yes``, ``--no-review``, ``--output-schema``, ``--allowed-tools``,
  ``--add-dir`` and ``--mcp-config`` DO NOT EXIST on this build. Do not reuse the
  argv builder in ``agents/sdlc_cli_engine.py``; see ``runner._build_argv``.
* MCP permission rules must use the ``MCPTool(server__tool)`` form. A rule
  written ``mcp__server__tool`` never matches.
"""

from __future__ import annotations

from .config import (
    cli_mode_enabled,
    cli_runtime_config,
    CliRuntimeConfig,
    preflight,
)

__all__ = [
    "cli_mode_enabled",
    "cli_runtime_config",
    "CliRuntimeConfig",
    "preflight",
]
