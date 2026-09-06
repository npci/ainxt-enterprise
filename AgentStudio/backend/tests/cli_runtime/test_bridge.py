# SPDX-License-Identifier: MIT
"""Bridge tool-list and permission-mode contract.

These pin the behaviour that a CLI-mode agent with a purpose-built tool attached
(e.g. ``gitlab_get_mr``) still gets ``code_executor`` — otherwise it can fetch
data but cannot produce the requested file. The native engine deliberately
withholds ``code_executor`` in that case; CLI mode restores it here because file
generation is served through ``code_executor`` over MCP.
"""

from __future__ import annotations

from app.cli_runtime.bridge import (
    _uploaded_file_names,
    _with_uploaded_files_directive,
    infer_permission_mode,
    mcp_tool_names,
)


class TestUploadedFilesDirective:
    def test_directive_lists_the_staged_files(self):
        docs = [
            {"file_name": "Sunflower_Description.docx", "parsed_text": "text"},
            {"file_name": "notes.txt", "parsed_text": "more"},
        ]
        out = _with_uploaded_files_directive("Give an overview.", docs)
        assert "inputs/Sunflower_Description.docx.txt" in out
        assert "inputs/notes.txt" in out
        assert "READ them" in out
        assert out.rstrip().endswith("Give an overview.")

    def test_no_docs_leaves_the_prompt_unchanged(self):
        assert _with_uploaded_files_directive("Hello", []) == "Hello"

    def test_docs_without_text_are_not_listed(self):
        docs = [{"file_name": "empty.pdf", "parsed_text": ""}]
        assert _with_uploaded_files_directive("Hi", docs) == "Hi"

    def test_names_match_what_workspace_would_stage(self):
        docs = [{"file_name": "a.docx", "parsed_text": "1"},
                {"file_name": "a.docx", "parsed_text": "2"}]
        names = _uploaded_file_names(docs)
        assert len(names) == 2 and len(set(names)) == 2


class TestMcpToolNames:
    def test_code_executor_is_added_alongside_an_attached_tool(self, monkeypatch):
        monkeypatch.delenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", raising=False)
        names = mcp_tool_names(["gitlab_get_mr"])
        assert "gitlab_get_mr" in names
        assert "code_executor" in names

    def test_code_executor_is_not_duplicated_when_already_present(self, monkeypatch):
        monkeypatch.delenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", raising=False)
        names = mcp_tool_names(["code_executor", "gitlab_get_mr"])
        assert names.count("code_executor") == 1

    def test_empty_input_still_yields_code_executor(self, monkeypatch):
        monkeypatch.delenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", raising=False)
        assert mcp_tool_names([]) == ["code_executor"]

    def test_the_force_flag_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", "false")
        names = mcp_tool_names(["gitlab_get_mr"])
        assert "code_executor" not in names
        assert names == ["gitlab_get_mr"]

    def test_order_is_preserved_and_deduplicated(self, monkeypatch):
        monkeypatch.delenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", raising=False)
        names = mcp_tool_names(["jira_search", "jira_search", "gitlab_get_mr"])
        assert names[:2] == ["jira_search", "gitlab_get_mr"]
        assert "code_executor" in names


class TestPermissionMode:
    def test_headless_runs_bypass_permissions(self):
        """A headless run has no human to approve tool calls. Verified against
        0.2.101, the softer modes (acceptEdits/dontAsk/auto) leave MCP tool calls
        gated and the run self-terminates as ``Cancelled`` after a couple of turns
        WITHOUT executing the tool; only ``bypassPermissions`` lets the agent call
        tools and finish. Access is still constrained by the per-run MCP token and
        the session's tool allow-list."""
        assert infer_permission_mode(["gitlab_get_mr", "code_executor"]) == "bypassPermissions"

    def test_a_read_only_tool_set_also_bypasses(self):
        assert infer_permission_mode(["gitlab_get_mr"]) == "bypassPermissions"

    def test_no_tools_still_bypasses(self):
        assert infer_permission_mode([]) == "bypassPermissions"

    def test_an_explicit_mode_wins(self):
        assert infer_permission_mode(["code_executor"], explicit="plan") == "plan"

    def test_the_bridge_flow_makes_a_gitlab_node_runnable(self, monkeypatch):
        """End-to-end of the two functions as bridge.run_agent_turn_via_cli uses
        them: names are expanded first (code_executor added), then the mode is
        inferred — a GitLab node ends up with both tools AND bypassPermissions, so
        it can fetch the MR and generate the file without being cancelled."""
        monkeypatch.delenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", raising=False)
        names = mcp_tool_names(["gitlab_get_mr"])
        assert "code_executor" in names
        assert infer_permission_mode(names) == "bypassPermissions"
