// SPDX-License-Identifier: Apache-2.0
"use strict";
/**
 * Single source of truth for WHICH CLI wire protocol the desktop app drives.
 *
 * The `ainxt` CLI has spoken two different protocols across its versions, and the
 * desktop app supports both so that upgrading the CLI and upgrading the app are
 * independent steps:
 *
 *   • "streamjson"  Non-interactive newline-delimited JSON over
 *                   `--full --print --input-format stream-json`, using the
 *                   control_request / can_use_tool wire format and SSE MCP.
 *                   This is the DEFAULT.
 *
 *   • "acp"         Agent-Client-Protocol JSON-RPC 2.0 over `agent stdio`
 *                   (initialize → authenticate → session/new → session/prompt),
 *                   streamable-HTTP MCP, and use_tool/search_tool tool wrapping.
 *                   Opt-in.
 *
 * Selection is by the AINXT_CLI_PROTOCOL env var, set once in the launcher:
 *
 *     AINXT_CLI_PROTOCOL=streamjson   (or "old")   [DEFAULT]
 *     AINXT_CLI_PROTOCOL=acp          (or "new")
 *
 * The protocol is chosen by this flag alone — never inferred from the binary's
 * filename, which is identical across versions. An unset or unrecognised value
 * falls back to "streamjson" rather than failing, so a misconfigured launcher
 * still starts.
 */

const ACP_ALIASES = new Set(["acp", "new", "json-rpc", "jsonrpc"]);
const STREAMJSON_ALIASES = new Set(["streamjson", "stream-json", "old", "legacy"]);

/**
 * @returns {"acp" | "streamjson"}
 */
function resolveProtocol() {
  const flag = String(process.env.AINXT_CLI_PROTOCOL || "").trim().toLowerCase();
  if (ACP_ALIASES.has(flag)) return "acp";
  if (STREAMJSON_ALIASES.has(flag)) return "streamjson";
  // Unset / unrecognised → streamjson (the default).
  return "streamjson";
}

function isAcp() {
  return resolveProtocol() === "acp";
}

module.exports = { resolveProtocol, isAcp };
