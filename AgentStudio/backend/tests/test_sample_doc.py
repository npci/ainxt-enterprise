# SPDX-License-Identifier: MIT
"""Unit tests for the per-agent Sample Document feature.

Scope kept intentionally narrow: only the pure-logic pieces that don't
require a Postgres, a running FastAPI app, or a live sandbox
subprocess. Larger integration checks (upload endpoint round-trip,
sandbox env injection at run time) belong in the Phase-4 manual-test
checklist — those touch real IO and are cheaper to verify by running
the app once than by mocking the entire stack.

Covered here:

* ``skill_manifest.sample_doc_directive`` — returns empty for missing /
  malformed inputs, renders the guidance block with the SAMPLE_DOC_*
  contract when both ``path`` and ``kind`` are set, advertises the
  original filename, appends user notes verbatim, and case-folds the
  kind before rendering.
* Source-level guarantees (no runtime imports needed):
  - ``document_tools._READ_DOCUMENT_CODE`` allow-lists ``SAMPLE_DOC_DIR``
    so ``read_document`` accepts the uploaded sample.
  - ``platform_tools.code_executor`` exposes ``SAMPLE_DOC_PATH`` /
    ``SAMPLE_DOC_KIND`` / ``SAMPLE_DOC_DIR`` as bare globals in the
    sandbox namespace.
  - ``ToolDispatcher._run_in_sandbox`` accepts ``sample_doc_path`` /
    ``sample_doc_kind`` kwargs and writes them into ``sandbox_env``.

``skill_manifest`` is loaded via ``importlib`` to skip
``app.core.__init__``'s heavy DB / logger imports — the module itself
is pure Python with only stdlib dependencies, so the direct load
is both accurate and fast.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def sample_doc_directive():
    """Load ``skill_manifest.sample_doc_directive`` without dragging in
    ``app.core.__init__`` (which pulls in psycopg / structlog / etc.,
    none of which are needed for the pure-string rendering under test)."""
    spec = importlib.util.spec_from_file_location(
        "_skill_manifest_under_test",
        str(_BACKEND / "app" / "core" / "skill_manifest.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod.sample_doc_directive


# ---------------------------------------------------------------------------
# sample_doc_directive
# ---------------------------------------------------------------------------


def test_empty_when_no_sample(sample_doc_directive):
    """No sample attached → no prompt block, no wasted tokens."""
    assert sample_doc_directive(None) == ""
    assert sample_doc_directive({}) == ""


def test_empty_when_kind_missing(sample_doc_directive):
    """Malformed metadata (path but no kind) is silently dropped so a
    corrupted row can't inject a broken block into the system prompt."""
    assert sample_doc_directive({"path": "/tmp/sample.docx"}) == ""


def test_empty_when_path_missing(sample_doc_directive):
    assert sample_doc_directive({"kind": "docx"}) == ""


def test_renders_core_block(sample_doc_directive):
    """Happy path: both fields set, block advertises SAMPLE_DOC_PATH,
    documents the "guidance, not a constraint" framing, and includes
    per-kind recipe hints so the LLM knows to open-as-base."""
    block = sample_doc_directive({
        "path": "/tmp/agent_samples/agent-abc/sample.docx",
        "kind": "docx",
    })
    assert block, "expected a non-empty prompt block"
    assert "SAMPLE_DOC_PATH" in block
    assert "docx" in block.lower()
    assert "guidance" in block.lower()
    assert "not a constraint" in block.lower()
    for keyword in ("Document(", "Presentation(", "load_workbook(", "read_document"):
        assert keyword in block


def test_advertises_filename(sample_doc_directive):
    block = sample_doc_directive({
        "path": "/tmp/agent_samples/agent-abc/sample.pptx",
        "kind": "pptx",
        "name": "approved-brd-2024.pptx",
    })
    assert "approved-brd-2024.pptx" in block


def test_appends_user_notes_verbatim(sample_doc_directive):
    """User's guidance textarea must reach the prompt unchanged so
    hints like "keep the cover page" actually reach the model."""
    notes = "Keep the cover page; feel free to change everything else."
    block = sample_doc_directive({
        "path":  "/tmp/sample.docx",
        "kind":  "docx",
        "notes": notes,
    })
    assert notes in block
    assert "User's guidance on the sample" in block


def test_case_folds_kind(sample_doc_directive):
    """The kind is stored lowercase, but a raw uppercase blob shouldn't
    render as ``KIND: DOCX`` — we normalise before rendering so an
    older row with any casing still produces a consistent prompt."""
    block = sample_doc_directive({
        "path": "/tmp/sample.DOCX",
        "kind": "DOCX",
    })
    assert block
    assert "`docx`" in block


# ---------------------------------------------------------------------------
# Source-level guarantees — no imports needed, just grep-in-source.
# ---------------------------------------------------------------------------


def test_read_document_allow_lists_sample_doc_dir():
    """``read_document``'s file-path guard must accept files that live
    under SAMPLE_DOC_DIR — otherwise a ``read_document`` call on the
    user's uploaded sample would 400 even though we deliberately
    surfaced its path via the sandbox env.

    The helper is defined inside ``_READ_DOCUMENT_CODE`` (a string
    exec'd inside the sandbox subprocess), so we assert on the source
    string rather than on a callable."""
    src = (_BACKEND / "app" / "tools" / "document_tools.py").read_text(encoding="utf-8")
    for var in (
        "GENERATED_FILES_DIR",
        "WORKFLOW_ARTIFACT_DIR",
        "RUNTIME_ARTIFACTS_DIR",
        "SAMPLE_DOC_DIR",
    ):
        assert var in src, f"document_tools missing env var {var!r}"


def test_code_executor_namespace_exposes_sample_doc_globals():
    """``code_executor`` exposes OUTPUT_DIR as a bare global in the
    sandbox namespace; the sample-doc paths do too for the same
    reason (prompt hints use both bare-name and os.environ forms)."""
    src = (_BACKEND / "app" / "tools" / "platform_tools.py").read_text(encoding="utf-8")
    for var in ("SAMPLE_DOC_PATH", "SAMPLE_DOC_KIND", "SAMPLE_DOC_DIR"):
        assert f'"{var}"' in src, f"platform_tools missing namespace entry {var!r}"


def test_dispatcher_threads_sample_doc_into_sandbox_env():
    """``ToolDispatcher._run_in_sandbox`` must accept the two kwargs
    and write ``SAMPLE_DOC_PATH`` / ``SAMPLE_DOC_DIR`` into
    ``sandbox_env`` before the subprocess launches — otherwise the
    prompt block would reference env vars that don't exist at run
    time."""
    src = (_BACKEND / "agent_factory" / "pipeline.py").read_text(encoding="utf-8")
    assert "sample_doc_path: str" in src, "dispatcher signature missing sample_doc_path"
    assert "sample_doc_kind: str" in src, "dispatcher signature missing sample_doc_kind"
    assert 'sandbox_env["SAMPLE_DOC_PATH"]' in src, "env not written for SAMPLE_DOC_PATH"
    assert 'sandbox_env["SAMPLE_DOC_DIR"]'  in src, "env not written for SAMPLE_DOC_DIR"


def test_native_engine_threads_sample_doc_into_catalog_dispatch():
    """The native workflow path (``ABSTUDIO_CLI_MODE`` off) must thread
    ``sample_doc_path`` / ``sample_doc_kind`` from the agent-node data
    into ``_CatalogTool`` → ``ToolDispatcher.dispatch`` — otherwise the
    prompt block advertising ``SAMPLE_DOC_PATH`` inside ``code_executor``
    references env vars that never get written to the sandbox.

    Source-string style (same as ``test_dispatcher_threads_sample_doc_into_sandbox_env``)
    to keep the check dependency-free: importing ``native_engine`` would
    pull in the full FastAPI / DB stack for a plumbing assertion."""
    src = (_BACKEND / "app" / "engine" / "native_engine.py").read_text(encoding="utf-8")

    # _CatalogTool must accept and store the two fields.
    assert "sample_doc_path: str" in src, \
        "_CatalogTool missing sample_doc_path kwarg"
    assert "sample_doc_kind: str" in src, \
        "_CatalogTool missing sample_doc_kind kwarg"
    assert "self._sample_doc_path" in src, \
        "_CatalogTool must retain sample_doc_path on the instance"
    assert "self._sample_doc_kind" in src, \
        "_CatalogTool must retain sample_doc_kind on the instance"

    # _CatalogTool.call must gate forwarding on code_executor so the
    # sample path never reaches unrelated tools.
    assert 'self.name == "code_executor"' in src and "sample_doc_path" in src, \
        "_CatalogTool.call must gate sample_doc forwarding on code_executor"

    # _resolve_catalog_tools must propagate the kwargs to every wrapper.
    assert "sample_doc_path=sample_doc_path" in src, \
        "_resolve_catalog_tools must forward sample_doc_path to _CatalogTool"
    assert "sample_doc_kind=sample_doc_kind" in src, \
        "_resolve_catalog_tools must forward sample_doc_kind to _CatalogTool"

    # _execute_agent_node must read the node's sample_doc and pass it
    # to _resolve_catalog_tools, applying the same missing-file guard
    # the CLI branch does.
    assert 'data.get("sample_doc")' in src, \
        "_execute_agent_node must read sample_doc from node data"
    assert "os.path.isfile(_sd_path)" in src, \
        "_execute_agent_node must guard against a missing sample_doc file"
    assert "sample_doc_path=_sd_path" in src, \
        "_execute_agent_node must forward the resolved sample_doc_path"
    assert "sample_doc_kind=_sd_kind" in src, \
        "_execute_agent_node must forward the resolved sample_doc_kind"
