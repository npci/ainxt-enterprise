# SPDX-License-Identifier: Apache-2.0
"""Per-run workspaces: path safety, TOML generation, clone classification, TTL."""

from __future__ import annotations

import os
import tempfile
import tomllib

from app.cli_runtime import workspace as ws
from app.cli_runtime.config import cli_runtime_config


def _root(monkeypatch) -> str:
    root = tempfile.mkdtemp()
    monkeypatch.setenv("ABSTUDIO_CLI_WORKSPACE_ROOT", root)
    return root


class TestPathSafety:
    def test_traversal_sequences_are_neutralised(self):
        assert "/" not in ws.safe_run_id("../../etc/passwd")
        assert "\\" not in ws.safe_run_id(r"..\..\windows")

    def test_an_empty_id_still_yields_a_name(self):
        assert ws.safe_run_id("") == "run"
        assert ws.safe_run_id("   ") == "run"

    def test_ordinary_ids_are_preserved(self):
        assert ws.safe_run_id("wf-thread1-node2") == "wf-thread1-node2"

    def test_a_workspace_cannot_escape_its_root(self, monkeypatch):
        root = _root(monkeypatch)
        path = ws.prepare_workspace("../../../evil")
        assert os.path.abspath(path).startswith(os.path.abspath(root))

    def test_preparing_twice_reuses_the_directory(self, monkeypatch):
        _root(monkeypatch)
        first = ws.prepare_workspace("r1")
        marker = os.path.join(first, "marker.txt")
        with open(marker, "w") as f:
            pass
        second = ws.prepare_workspace("r1")
        assert first == second and os.path.isfile(marker)


class TestMcpConfig:
    def test_the_generated_file_is_valid_toml(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        path = ws.write_mcp_config(
            workspace=workspace, config=cli_runtime_config(), run_id="r1", token="tok",
        )
        with open(path, "rb") as fh:
            parsed = tomllib.load(fh)
        assert "abstudio" in parsed["mcp_servers"]

    def test_it_declares_http_transport_with_the_run_url(self, monkeypatch):
        _root(monkeypatch)
        monkeypatch.setenv("ABSTUDIO_MCP_BASE_URL", "http://127.0.0.1:8000")
        workspace = ws.prepare_workspace("r7")
        path = ws.write_mcp_config(
            workspace=workspace, config=cli_runtime_config(), run_id="r7", token="tok",
        )
        with open(path, "rb") as fh:
            entry = tomllib.load(fh)["mcp_servers"]["abstudio"]
        assert entry["url"] == "http://127.0.0.1:8000/abstudio-mcp/r7"
        assert entry["enabled"] is True

    def test_the_token_becomes_a_bearer_header(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        path = ws.write_mcp_config(
            workspace=workspace, config=cli_runtime_config(), run_id="r1", token="abc123",
        )
        with open(path, "rb") as fh:
            headers = tomllib.load(fh)["mcp_servers"]["abstudio"]["headers"]
        assert headers["Authorization"] == "Bearer abc123"

    def test_adversarial_tokens_round_trip_exactly(self, monkeypatch):
        """A quote or control character must not be able to break out of the
        string and corrupt the config."""
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        token = 'has"quote\\backslash\tand\x01ctrl'
        path = ws.write_mcp_config(
            workspace=workspace, config=cli_runtime_config(), run_id="r1", token=token,
        )
        with open(path, "rb") as fh:
            headers = tomllib.load(fh)["mcp_servers"]["abstudio"]["headers"]
        assert headers["Authorization"] == f"Bearer {token}"

    def test_rewriting_replaces_rather_than_appends(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        cfg = cli_runtime_config()
        ws.write_mcp_config(workspace=workspace, config=cfg, run_id="r1", token="t1")
        path = ws.write_mcp_config(workspace=workspace, config=cfg, run_id="r1", token="t2")
        with open(path, "rb") as fh:
            headers = tomllib.load(fh)["mcp_servers"]["abstudio"]["headers"]
        assert headers["Authorization"] == "Bearer t2"


class TestPromptFile:
    def test_content_is_written_verbatim(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        prompt = "line one\nline two\n\ttabbed \"quoted\""
        path = ws.write_prompt_file(workspace, prompt)
        assert open(path, encoding="utf-8").read() == prompt


class TestStageDocuments:
    def test_uploaded_docs_are_written_into_inputs_dir(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        docs = [
            {"file_name": "Sunflower_Description.docx", "parsed_text": "A sunflower is..."},
            {"file_name": "notes.txt", "parsed_text": "some notes"},
        ]
        manifest = ws.stage_documents(workspace, docs)
        assert len(manifest) == 2
        # Binary type gets a .txt suffix; .txt keeps its name.
        p1 = os.path.join(workspace, "inputs", "Sunflower_Description.docx.txt")
        p2 = os.path.join(workspace, "inputs", "notes.txt")
        assert os.path.isfile(p1) and open(p1, encoding="utf-8").read() == "A sunflower is..."
        assert os.path.isfile(p2) and open(p2, encoding="utf-8").read() == "some notes"

    def test_size_does_not_matter_all_docs_staged(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r2")
        big = {"file_name": "big.docx", "parsed_text": "x" * 500_000}
        small = {"file_name": "small.docx", "parsed_text": "tiny"}
        manifest = ws.stage_documents(workspace, [big, small])
        assert len(manifest) == 2

    def test_docs_without_text_are_skipped(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r3")
        manifest = ws.stage_documents(workspace, [{"file_name": "empty.pdf", "parsed_text": ""}])
        assert manifest == []

    def test_duplicate_names_do_not_clobber(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r4")
        docs = [
            {"file_name": "a.docx", "parsed_text": "first"},
            {"file_name": "a.docx", "parsed_text": "second"},
        ]
        manifest = ws.stage_documents(workspace, docs)
        assert len(manifest) == 2
        names = {m["name"] for m in manifest}
        assert len(names) == 2  # both preserved under distinct names

    def test_traversal_in_filename_is_neutralised(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r5")
        docs = [{"file_name": "../../etc/passwd", "parsed_text": "x"}]
        manifest = ws.stage_documents(workspace, docs)
        # Whatever landed, it must be inside the inputs dir, not outside.
        staged = os.path.join(workspace, manifest[0]["name"])
        assert os.path.abspath(staged).startswith(
            os.path.abspath(os.path.join(workspace, "inputs"))
        )

    def test_empty_input_is_safe(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r6")
        assert ws.stage_documents(workspace, []) == []


class TestRescueWorkspaceFiles:
    def _gen_dir(self, monkeypatch):
        import tempfile
        gd = tempfile.mkdtemp()
        monkeypatch.setenv("GENERATED_FILES_DIR", gd)
        return gd

    def test_a_model_written_md_is_registered_with_a_download_url(self, monkeypatch):
        gd = self._gen_dir(monkeypatch)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue1")
        with open(os.path.join(ws_dir, "rose_description.md"), "w", encoding="utf-8") as fh:
            fh.write("# Rose\nA rose is a flower.")
        rescued = ws.rescue_workspace_files(ws_dir, "rescue1")
        assert len(rescued) == 1
        r = rescued[0]
        assert r["filename"] == "rose_description.md"
        assert r["download_url"].startswith("/generated-files/")
        assert r["download_url"].endswith(r["disk_name"])
        # The file was actually copied into GENERATED_FILES_DIR.
        assert os.path.isfile(os.path.join(gd, r["disk_name"]))

    def test_our_own_scaffolding_is_never_rescued(self, monkeypatch):
        self._gen_dir(monkeypatch)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue2")
        ws.write_prompt_file(ws_dir, "hi")
        os.makedirs(os.path.join(ws_dir, ".ainxt"), exist_ok=True)
        with open(os.path.join(ws_dir, ".mcp.json"), "w") as f:
            pass
        os.makedirs(os.path.join(ws_dir, "inputs"), exist_ok=True)
        rescued = ws.rescue_workspace_files(ws_dir, "rescue2")
        assert rescued == []

    def test_unknown_scratch_extensions_are_ignored(self, monkeypatch):
        self._gen_dir(monkeypatch)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue3")
        with open(os.path.join(ws_dir, "scratch.pyc"), "w") as f:
            pass
        with open(os.path.join(ws_dir, "tempfile.tmp"), "w") as f:
            pass
        assert ws.rescue_workspace_files(ws_dir, "rescue3") == []

    def test_deliverable_docx_is_rescued(self, monkeypatch):
        self._gen_dir(monkeypatch)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue4")
        with open(os.path.join(ws_dir, "report.docx"), "wb") as fh:
            fh.write(b"PK\x03\x04 fake docx")
        rescued = ws.rescue_workspace_files(ws_dir, "rescue4")
        assert [r["filename"] for r in rescued] == ["report.docx"]

    def test_no_gen_dir_is_safe(self, monkeypatch):
        monkeypatch.delenv("GENERATED_FILES_DIR", raising=False)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue5")
        with open(os.path.join(ws_dir, "x.md"), "w") as fh:
            fh.write("x")
        # No download store configured → nothing rescued, no crash.
        assert ws.rescue_workspace_files(ws_dir, "rescue5") == []

    def test_rescue_with_user_id_nests_under_owner_dir(self, monkeypatch):
        """IDOR fix: a user_id nests the rescued file under {owner_tag}/ and
        the download_url / disk_name carry that prefix."""
        import hashlib

        gd = self._gen_dir(monkeypatch)
        _root(monkeypatch)
        ws_dir = ws.prepare_workspace("rescue6")
        with open(os.path.join(ws_dir, "note.md"), "w", encoding="utf-8") as fh:
            fh.write("hi")
        rescued = ws.rescue_workspace_files(ws_dir, "rescue6", "user-xyz")
        assert len(rescued) == 1
        tag = hashlib.sha256(b"user-xyz").hexdigest()[:16]
        r = rescued[0]
        assert r["disk_name"].startswith(f"{tag}/")
        assert r["download_url"] == f"/generated-files/{r['disk_name']}"
        # Physically stored inside the owner-dir.
        assert os.path.isfile(os.path.join(gd, tag, os.path.basename(r["disk_name"])))

    def test_owner_tag_is_the_canonical_function_not_a_copy(self):
        """workspace must REUSE app.owner_tag, not re-implement it.

        This used to assert against a hardcoded ``sha256(b"abc")[:16]``, which
        re-implemented the algorithm a third time inside the test — so changing
        ``_OWNER_TAG_LEN`` in the canonical module would have left this test
        passing while the copies silently diverged.

        Asserting object identity makes drift structurally impossible: there is
        exactly one function, so there is nothing left to keep in lockstep.
        """
        import app.owner_tag as ot
        assert ws._owner_tag is ot.owner_tag


class TestCloneState:
    def test_missing_when_there_is_no_git_directory(self, monkeypatch):
        _root(monkeypatch)
        assert ws.clone_state(ws.prepare_workspace("r1")) == "missing"

    def test_empty_when_only_scaffolding_is_present(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        repo = ws.repo_dir(workspace)
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        with open(os.path.join(repo, "README.md"), "w") as f:
            pass
        assert ws.clone_state(workspace) == "empty"

    def test_cloned_when_real_content_exists(self, monkeypatch):
        _root(monkeypatch)
        workspace = ws.prepare_workspace("r1")
        repo = ws.repo_dir(workspace)
        os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        with open(os.path.join(repo, "main.py"), "w") as f:
            pass
        assert ws.clone_state(workspace) == "cloned"


class TestCloneUrls:
    def test_a_bare_path_resolves_against_the_gitlab_host(self, monkeypatch):
        monkeypatch.setenv("GITLAB_URL", "https://git.example.com")
        url = ws._authenticated_url("group/project", "")
        assert url == "https://git.example.com/group/project.git"

    def test_the_token_is_never_embedded_in_the_url(self):
        """ARCH-F-ABS1-008: the token is passed via a git HTTP header
        (_git_auth_header), not embedded in the clone URL, so it can never
        leak via `ps`, `/proc`, git error messages, or `.git/config`."""
        url = ws._authenticated_url("https://git.example.com/g/p", "glpat-xyz")
        assert url == "https://git.example.com/g/p.git"
        assert "glpat-xyz" not in url
        assert "@" not in url

    def test_no_token_means_no_credentials_in_the_url(self):
        url = ws._authenticated_url("https://git.example.com/g/p", "")
        assert "@" not in url

    def test_the_auth_header_carries_the_token_instead(self):
        assert ws._git_auth_header("glpat-xyz") == [
            "-c", "http.extraHeader=Authorization: Bearer glpat-xyz",
        ]
        assert ws._git_auth_header("") == []

    def test_a_clone_without_a_user_token_fails_with_guidance(self, monkeypatch):
        """No service-account fallback: a run must act as the user, and an
        unconfigured token has to say so rather than borrow wider credentials."""
        _root(monkeypatch)
        monkeypatch.setattr(ws, "resolve_git_token", lambda *a, **k: "")
        result = ws.ensure_repo(
            workspace=ws.prepare_workspace("r1"), repo="group/project",
            user_id="u1", email="a@b.c",
        )
        assert result.ok is False
        assert "token" in result.error.lower()


class TestSweep:
    def test_fresh_workspaces_are_kept(self, monkeypatch):
        _root(monkeypatch)
        ws.prepare_workspace("fresh")
        removed, kept = ws.sweep_workspaces(ttl_seconds=3600)
        assert removed == 0 and kept == 1

    def test_expired_workspaces_are_removed(self, monkeypatch):
        _root(monkeypatch)
        path = ws.prepare_workspace("old")
        old = 1_000_000
        os.utime(path, (old, old))
        removed, _ = ws.sweep_workspaces(ttl_seconds=60)
        assert removed == 1 and not os.path.isdir(path)

    def test_sweeping_a_missing_root_is_harmless(self, monkeypatch):
        monkeypatch.setenv("ABSTUDIO_CLI_WORKSPACE_ROOT", os.path.join(tempfile.mkdtemp(), "absent"))
        assert ws.sweep_workspaces(ttl_seconds=60) == (0, 0)
