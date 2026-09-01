# SPDX-License-Identifier: Apache-2.0
"""Path/output sanitisation — internal filesystem details must not reach users."""

from __future__ import annotations

from app.cli_runtime.sanitize import (
    download_guidance,
    filter_deliverables,
    neutralize_artifact_path,
    scrub_paths,
)


def _f(name, fmt=None):
    return {"filename": name, "disk_name": "x_" + name,
            "format": fmt if fmt is not None else name[name.rfind("."):]}


class TestScrubPaths:
    def test_windows_absolute_path_is_redacted(self):
        out = scrub_paths(r"The file exists at D:\Java\AiNxt\ABStudio\tmp\b7_MR.docx now.")
        assert "D:\\" not in out
        assert "ABStudio" not in out
        assert "[file]" in out

    def test_runtime_artifacts_tree_is_redacted_both_slash_styles(self):
        a = scrub_paths(r"saved to runtime_artifacts\workflows\wf_x\MR.docx")
        b = scrub_paths("saved to runtime_artifacts/workflows/wf_x/MR.docx")
        assert "runtime_artifacts" not in a and "[file]" in a
        assert "runtime_artifacts" not in b and "[file]" in b

    def test_posix_internal_path_is_redacted(self):
        out = scrub_paths("wrote /var/lib/abstudio/runtime_artifacts/x/MR.docx ok")
        assert "/var/lib" not in out and "[file]" in out

    def test_download_url_is_preserved(self):
        text = "Download it here: /generated-files/run1/MR_698_Overview.docx"
        out = scrub_paths(text)
        assert "/generated-files/run1/MR_698_Overview.docx" in out

    def test_download_url_preserved_even_next_to_a_redacted_path(self):
        text = (r"Saved to D:\Java\ABStudio\tmp\x.docx. "
                "Link: /generated-files/run1/x.docx")
        out = scrub_paths(text)
        assert "/generated-files/run1/x.docx" in out
        assert "D:\\" not in out

    def test_plain_prose_is_untouched(self):
        text = "The document has 2 pages and 3 tables."
        assert scrub_paths(text) == text

    def test_empty_and_none_are_safe(self):
        assert scrub_paths("") == ""
        assert scrub_paths(None) == ""

    def test_is_idempotent(self):
        once = scrub_paths(r"at D:\a\b\c.docx")
        assert scrub_paths(once) == once


class TestArtifactPathNeutralization:
    def test_absolute_path_is_replaced_with_symbolic_name(self):
        instr = ("Do the task.\n\nRuntime artifact directory for this workflow run: "
                 r"D:\Java\AiNxt\runtime_artifacts\workflows\wf_x. Use WORKFLOW_ARTIFACT_DIR "
                 "for files that must be shared.")
        out = neutralize_artifact_path(instr)
        assert "D:\\" not in out
        assert "WORKFLOW_ARTIFACT_DIR" in out
        assert "Runtime artifact directory for this workflow run:" in out

    def test_no_artifact_line_is_left_unchanged(self):
        instr = "Just do the task and reply."
        assert neutralize_artifact_path(instr) == instr


class TestDownloadGuidance:
    def test_guidance_mentions_the_key_rules(self):
        g = download_guidance().lower()
        assert "path" in g
        assert "download" in g


class TestFilterDeliverables:
    def test_the_real_mr_scenario_keeps_only_the_docx(self):
        """The exact reported case: an agent dumped diffs.txt + 4 *.diff scratch
        files alongside the real MR_698_Overview.docx. Only the docx should show."""
        files = [
            _f("diffs.txt"),
            _f("gateway_claude.py.diff"),
            _f("gateway_openai.py.diff"),
            _f("gateway_gemini.py.diff"),
            _f("gateway_local_llm.py.diff"),
            _f("MR_698_Overview.docx"),
        ]
        kept = filter_deliverables(files, "generate a docx overview of the MR")
        assert [f["filename"] for f in kept] == ["MR_698_Overview.docx"]

    def test_multiple_genuine_deliverables_are_all_kept(self):
        files = [_f("a.docx"), _f("chart.png"), _f("data.csv")]
        kept = filter_deliverables(files, "make a report")
        assert len(kept) == 3

    def test_an_explicitly_requested_intermediate_type_is_kept(self):
        """If the user actually asked for a .txt, don't hide it."""
        files = [_f("summary.txt"), _f("report.docx")]
        kept = filter_deliverables(files, "give me the summary as a txt file")
        names = {f["filename"] for f in kept}
        assert "summary.txt" in names and "report.docx" in names

    def test_unknown_extensions_are_kept_not_hidden(self):
        files = [_f("model.onnx", fmt=".onnx")]
        kept = filter_deliverables(files, "export the model")
        assert len(kept) == 1

    def test_all_intermediates_are_hidden(self):
        """If a node produced only known scratch types, hide them all — in a
        workflow the real deliverable comes from another node."""
        files = [_f("mr698.diff.txt"), _f("mr698_bundle.json")]
        kept = filter_deliverables(files, "fetch the MR and pass it on")
        assert kept == []

    def test_strict_json_boilerplate_does_not_keep_a_scratch_json(self):
        """The exact regression: the workflow prompt says 'Node outputs must
        remain strict JSON' — that must NOT make a scratch .json look requested."""
        files = [_f("mr698_bundle.json"), _f("mr698.diff.txt")]
        prompt = ("Fetch the MR using the tools. Use WORKFLOW_ARTIFACT_DIR. "
                  "Node outputs must remain strict JSON.")
        kept = filter_deliverables(files, prompt)
        assert kept == []

    def test_a_genuinely_requested_json_file_is_kept(self):
        files = [_f("data.json"), _f("scratch.txt")]
        kept = filter_deliverables(files, "export the results as a json file")
        names = {f["filename"] for f in kept}
        assert "data.json" in names and "scratch.txt" not in names

    def test_dotted_extension_request_is_honored(self):
        files = [_f("out.txt")]
        kept = filter_deliverables(files, "save it to out.txt please")
        assert len(kept) == 1

    def test_empty_input_is_safe(self):
        assert filter_deliverables([], "x") == []
        assert filter_deliverables(None, "x") == []
