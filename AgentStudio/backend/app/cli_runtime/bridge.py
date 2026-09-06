# SPDX-License-Identifier: MIT
"""cli_runtime.bridge — the one place both execution paths delegate a turn to the CLI.

ABStudio has two independent execution paths, and this is the single most
important fact about integrating with it:

* ``POST /run-stream`` (workflows) → ``NativeEngine._run_agent``
* ``POST /agent-runner/chat-stream`` (saved agents) → ``AgentRunner.run``

The previous attempt at this feature wired CLI delegation into ``NativeEngine``
only, then spent its entire life unable to work out why nothing happened — the
agent-chat endpoint never goes through ``NativeEngine`` at all. Its final commit
message records the discovery. Hence one shared entry point, called from both
sites, so neither can silently miss the CLI.

``run_agent_turn_via_cli`` is an async generator yielding
``(sse_event_name, payload)`` tuples. It deliberately does not know how to format
SSE or which node is final: the workflow caller wraps tuples with ``make_sse``
and decides ``agent_complete`` vs ``agent_progress``, while the chat caller feeds
its own sink. Keeping formatting out of here is what lets one implementation
serve both vocabularies.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from core.logger import logger

from .config import CliRuntimeConfig, cli_runtime_config
from .event_mapper import CliTurnResult, merge
from .mcp_server import ENGINE_NATIVE_TOOLS
from .runner import CliTurnRequest, run_cli_turn


@dataclass
class AgentTurnSpec:
    """A single agent turn, expressed independently of which path requested it."""

    prompt: str
    model: str
    agent_name: str = "Agent"
    node_id: str = ""
    run_id: str = ""

    user_id: str = ""
    email: str = ""

    tool_names: List[str] = field(default_factory=list)
    skill_names: List[str] = field(default_factory=list)

    # ``plan`` for read-only agents, ``acceptEdits`` when the agent may write.
    permission_mode: str = ""
    max_turns: int = 0

    repo: str = ""
    repo_ref: str = ""
    workflow_artifact_dir: str = ""
    resume_session_id: str = ""

    # Per-agent Sample Document (look-and-feel reference). Populated
    # from ``agents.sample_doc`` when the AgentRunner builds this spec;
    # empty strings otherwise. Threaded into the RunSession so the
    # MCP-side ToolDispatcher can expose them as SAMPLE_DOC_PATH /
    # SAMPLE_DOC_KIND inside the ``code_executor`` sandbox at tool-call
    # time. See ``app/api/agent_sample.py`` and
    # ``app/core/skill_manifest.sample_doc_directive``.
    sample_doc_path: str = ""
    sample_doc_kind: str = ""

    # User-uploaded documents (``{file_name, parsed_text, ...}``). Staged as real
    # files in the CLI working directory so any node can read them directly.
    documents: List[dict] = field(default_factory=list)

    # Stream ``agent_token`` frames. False for non-final workflow nodes, matching
    # the native engine, which only streams tokens for the final agent.
    emit_tokens: bool = True


def infer_permission_mode(tool_names: List[str], explicit: str = "") -> str:
    """Choose a CLI permission mode for this headless turn.

    Returns ``bypassPermissions``.

    This is a **headless** run: there is no human at a terminal to approve a tool
    call interactively. Verified against 0.2.101, the softer modes all leave MCP
    tool calls gated and the run self-terminates as ``stopReason=Cancelled`` after
    a couple of turns *without ever executing the tool* — the model narrates "let
    me call the tool" and then the turn is cancelled:

        permission-mode  | result
        ---------------- | -----------------------------------------
        acceptEdits      | Cancelled after 2 turns, 0 tool calls
        dontAsk          | Cancelled after 3 turns, 0 tool calls
        auto             | Cancelled after 4 turns, 0 tool calls
        bypassPermissions| EndTurn, tools called, run completes

    Only ``bypassPermissions`` lets a headless agent actually invoke tools and
    finish. That is safe here because access is already constrained by TWO other
    layers that do not depend on the CLI's own prompt: (1) the per-run MCP bearer
    token, and (2) the session's fixed tool/skill allow-list. The CLI's own file
    tools operate only inside the private per-run workspace. So the interactive
    approval gate adds no security here — it only breaks the run.

    ``explicit`` still wins, so a caller can override per turn if ever needed.
    """
    if explicit:
        return explicit
    return "bypassPermissions"


def mcp_tool_names(tool_names: List[str]) -> List[str]:
    """Tools to expose over MCP for this turn.

    Includes ``ask_human`` / ``spawn_swarm``: they are advertised so the model can
    still see the capability, but the MCP server answers them with a sentinel and
    the caller runs the native implementation. Hiding them instead would make the
    model insist the capability does not exist.

    ``code_executor`` is always ensured (unless ``ABSTUDIO_CLI_FORCE_CODE_EXECUTOR``
    is set false). The native engine deliberately withholds ``code_executor`` from
    a node that already has a purpose-built tool attached (e.g. ``gitlab_get_mr``),
    to stop such nodes defaulting to ad-hoc Python. In CLI mode, though, file
    generation is *also* served through ``code_executor`` over MCP, so a node with
    GitLab attached that is asked for a DOCX/PDF would otherwise have no way to
    produce the file. Adding it here (CLI path only) restores that capability
    without touching the native engine's rule; the model still decides when to use
    it. Prompts already carry the "only call code_executor when a file is
    explicitly requested" guardrail, so merely exposing it does not force its use.
    """
    names = [t for t in dict.fromkeys(tool_names) if t]
    if _force_code_executor() and "code_executor" not in names:
        names.append("code_executor")
    return names


def _force_code_executor() -> bool:
    """Whether to always expose ``code_executor`` in CLI mode (default: True)."""
    raw = (os.getenv("ABSTUDIO_CLI_FORCE_CODE_EXECUTOR", "") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def _uploaded_file_names(documents: List[dict]) -> List[str]:
    """Leaf names, as staged, of the uploaded documents (see workspace.stage_documents)."""
    from .workspace import _INPUTS_DIR, _safe_file_name

    names: List[str] = []
    seen: set = set()
    for doc in documents or []:
        if not isinstance(doc, dict):
            continue
        if not str(doc.get("parsed_text") or doc.get("text") or "").strip():
            continue
        raw = doc.get("file_name") or doc.get("filename") or "attachment"
        leaf = _safe_file_name(raw)
        if not leaf.lower().endswith(".txt"):
            leaf = f"{leaf}.txt"
        candidate, n = leaf, 1
        while candidate in seen:
            stem, dot, ext = leaf.rpartition(".")
            candidate = f"{stem}_{n}.{ext}" if dot else f"{leaf}_{n}"
            n += 1
        seen.add(candidate)
        names.append(f"{_INPUTS_DIR}/{candidate}")
    return names


def _with_uploaded_files_directive(prompt: str, documents: List[dict]) -> str:
    """Prepend an instruction telling the agent to read the staged upload files.

    The files themselves are written into the CLI working directory by
    ``workspace.stage_documents`` (every node gets its own copy). This makes the
    agent aware they exist and names them, so it reads the file(s) before
    answering instead of claiming nothing was attached — the exact failure a
    non-first workflow node hit when small-doc text injection skipped it.
    """
    names = _uploaded_file_names(documents)
    if not names:
        return prompt
    listing = "\n".join(f"  - {n}" for n in names)
    directive = (
        "Uploaded files are available in your working directory. "
        "READ them (they are plain-text extracts) BEFORE responding, and base "
        "your answer on their contents. You may read them as many times as "
        "needed. Available uploaded file(s):\n"
        f"{listing}\n\n"
    )
    return directive + (prompt or "")


async def run_agent_turn_via_cli(
    spec: AgentTurnSpec,
    *,
    config: Optional[CliRuntimeConfig] = None,
) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
    """Run one agent turn in a spawned CLI, yielding ``(sse_event, payload)``.

    The final tuple is always ``("__result__", {"result": CliTurnResult})`` — an
    internal frame, never sent to a client, that hands the caller the accumulated
    text, usage, files and CLI session id. Using the stream itself to return the
    result keeps this a single-pass generator, so a caller cannot forget to
    collect it.

    Errors are reported through that result (``result.error``), not raised, so the
    caller decides between an ``error`` SSE frame and the emergency native
    fallback. ``CancelledError`` propagates after the child is killed.
    """
    cfg = config or cli_runtime_config()
    run_id = spec.run_id or f"abs-{uuid.uuid4().hex[:16]}"
    started = time.monotonic()

    tools = mcp_tool_names(spec.tool_names)
    prompt = _with_uploaded_files_directive(spec.prompt, spec.documents)
    request = CliTurnRequest(
        prompt=prompt,
        model=spec.model,
        run_id=run_id,
        user_id=spec.user_id,
        email=spec.email,
        agent_name=spec.agent_name,
        node_id=spec.node_id,
        tool_names=tools,
        skill_names=list(spec.skill_names or []),
        permission_mode=infer_permission_mode(tools, spec.permission_mode),
        max_turns=spec.max_turns or cfg.max_turns,
        repo=spec.repo,
        repo_ref=spec.repo_ref,
        workflow_artifact_dir=spec.workflow_artifact_dir,
        sample_doc_path=getattr(spec, "sample_doc_path", "") or "",
        sample_doc_kind=getattr(spec, "sample_doc_kind", "") or "",
        documents=list(spec.documents or []),
        resume_session_id=spec.resume_session_id,
    )

    result = CliTurnResult()

    # ``run_cli_turn`` owns the session's lifetime; it hands it over through this
    # callback the moment it is registered (before the spawn), so ``merge`` can
    # drain tool events for the whole run without either side guessing.
    holder: Dict[str, Any] = {}
    events = run_cli_turn(
        request, config=cfg,
        on_session=lambda s: holder.__setitem__("session", s),
    )

    async for frame in merge(
        events,
        session_provider=lambda: holder.get("session"),
        agent_name=spec.agent_name,
        result=result,
        emit_tokens=spec.emit_tokens,
    ):
        yield frame

    # Keep only files a user should see as downloads. An agent that drives
    # ``code_executor`` iteratively writes scratch files (a dumped diff it reads
    # around output truncation, a per-file split, a temp JSON) into the same
    # artifact directory as the real deliverable; every one is reported as a
    # generated file and shown as downloadable. filter_deliverables drops obvious
    # intermediates (.diff/.txt/.json/…) while honouring any type the prompt
    # explicitly asked for, and fails open so a real output is never lost.
    if result.generated_files:
        from .sanitize import filter_deliverables

        before = len(result.generated_files)
        result.generated_files = filter_deliverables(result.generated_files, spec.prompt)
        hidden = before - len(result.generated_files)
        if hidden:
            logger.info(
                "[CLI-BRIDGE] hid intermediate files from the download list",
                run_id=run_id, hidden=hidden,
                kept=[f.get("filename") for f in result.generated_files],
            )

    logger.info(
        "[CLI-BRIDGE] turn finished",
        run_id=run_id, agent=spec.agent_name, node_id=spec.node_id,
        duration_s=round(time.monotonic() - started, 1),
        **result.as_log_fields(),
    )

    if result.engine_native_requests:
        # Surfaced for the caller; the tools themselves cannot run in the CLI.
        logger.info(
            "[CLI-BRIDGE] engine-native tools were requested",
            run_id=run_id,
            tools=[r.get("tool") for r in result.engine_native_requests],
        )

    yield "__result__", {"result": result}


def build_prompt(instructions: str, user_input: str) -> str:
    """Compose the single prompt handed to the CLI.

    The engine has already assembled everything policy-relevant into
    ``instructions`` — system prompt, skills section, KB context, file-generation
    directives, compliance preamble. This only appends the live turn input, and
    the CLI is spawned with ``--verbatim`` so it forwards the text unmodified.

    Tool names are deliberately NOT rewritten here. The earlier attempt
    regex-replaced bare tool names with ``server__tool`` forms inside the
    instructions; that is unnecessary (the CLI resolves MCP tools from its own
    manifest) and actively risky, since the same words often appear in prose.

    Two CLI-only presentation guards are applied so internal filesystem details
    never reach the user (see ``sanitize``): the engine's injected absolute
    artifact path is neutralised to the symbolic ``WORKFLOW_ARTIFACT_DIR``, and a
    short directive tells the model to reference files by name/link only. Output
    is additionally scrubbed as a backstop in the event mapper.
    """
    from .sanitize import download_guidance, neutralize_artifact_path

    instructions = neutralize_artifact_path((instructions or "").strip())
    if instructions:
        instructions = instructions + download_guidance()
    user_input = (user_input or "").strip()
    if instructions and user_input:
        return f"{instructions}\n\n---\n\nTask input:\n{user_input}"
    return instructions or user_input


__all__ = [
    "AgentTurnSpec",
    "run_agent_turn_via_cli",
    "build_prompt",
    "infer_permission_mode",
    "mcp_tool_names",
    "ENGINE_NATIVE_TOOLS",
]
