# SPDX-License-Identifier: MIT
"""
Cowork connector MCP bridge.

Exposes the gateway's per-user connectors + Knowledge Base as MCP tools to the
local full agent (desktop Cowork), so the same engine the Code tab drives can
read Outlook/Teams/Jira/etc. and the KB without re-implementing connectors.

Served over SSE by routers/cowork_mcp_router.py (user resolved from the JWT).

Compliance (AiNxt):
  - INPUT (tool arguments) is hard-blocked if it carries PAN/secret/PII
    (prevents exfiltration via tool args) — reuses mcp/servers/base._compliance_check.
  - OUTPUT (connector / KB results) is REDACTED, not blocked, so the user can
    still read their own data (redact-and-proceed rule).
  - WRITE connector tools are advertised but NOT executed here — they require an
    explicit user confirm and go through the compliance-gated POST /connectors/action.
"""
import asyncio
import json
import os
import os as _os
import re as _re_recip
from concurrent.futures import ThreadPoolExecutor

from core.logger import logger, mask_email

# Dedicated thread pool for BLOCKING tool work (connector I/O, doc enqueue) so it
# never runs on the event loop. Bounded → natural back-pressure under 2k users.
# (feedback_scale_2k_users) run_code does NOT use this pool — its long wait is
# handled with async Redis so it never ties up a pool thread.
_TOOL_POOL = ThreadPoolExecutor(
    max_workers=int(os.getenv("BUDDY_TOOL_POOL", "64")),
    thread_name_prefix="buddy-tool",
)

# Per-tenant connector rate limit (protects M365/Graph etc. from throttling us).
# Redis token bucket: max N calls per user+connector per window.
_RATE_MAX = int(os.getenv("BUDDY_CONNECTOR_RATE_MAX", "60"))     # calls
_RATE_WINDOW = int(os.getenv("BUDDY_CONNECTOR_RATE_WINDOW", "60"))  # seconds
_UUID_RE = _re_recip.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re_recip.IGNORECASE,
)  # matches plain UUIDs — used to detect ChatAttachment ids passed as attachment_job_id

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "cowork-connectors"
SERVER_VERSION = "1.0.0"

_KB_TOOL = "knowledge_base_search"
_DOC_TOOL = "generate_document"
_MEMORY_TOOL = "remember"
_CODE_TOOL = "run_code"
_SKILL_TOOL = "get_document_skill"   # read a doc skill's SKILL.md
_BUILD_TOOL = "build_document"       # build docx/pptx/xlsx/pdf via agent-authored wrapper code in the sandbox
_RESEARCH_TOOL = "deep_research"     # multi-model, cross-vendor cited research report
_VERSIONS_TOOL = "list_document_versions"  # version history for an iterated document
_ANALYZE_TOOL = "analyze_data"       # bind a dataset file + run analysis in the sandbox (ADA)
_REVISE_TOOL = "revise_artifact"     # AI co-edit: apply an edit to a doc's source → new version (Canvas)

# ── compliance helpers ───────────────────────────────────────────────────────
def _block_input(args: dict):
    """Return a block reason if the tool arguments carry sensitive data, else None."""
    try:
        from mcp.servers.base import _compliance_check
        return _compliance_check(json.dumps(args, default=str))
    except Exception as e:
        logger.debug(f"cowork_mcp: input compliance check skipped → {e}")
        return None

# Accept an addr-spec, optionally inside a "Display Name <addr>" form. We only need
# to confirm SOME token in each recipient is a plausible email — a bare name is not.
_EMAIL_RE = _re_recip.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+")

def _invalid_recipients(arguments: dict, fields) -> list:
    """Return recipient tokens that are NOT valid email addresses (G4).

    Splits each field on ; , and whitespace-outside-brackets and checks every entry
    contains a syntactically valid address (handles 'Name <a@b.com>'). Returns the
    offending raw tokens (empty list = all good / no recipients given). Recipients
    that are already resolved by other params (e.g. a Teams chat) aren't screened
    here — this is only for email/calendar address fields.
    """
    bad = []
    for f in fields:
        raw = arguments.get(f)
        if not raw:
            continue
        # Split on ; , or whitespace, but keep "<...>" groups intact enough that the
        # regex can still find the addr-spec inside a display-name form.
        parts = _re_recip.split(r"[;,]|\s{2,}", str(raw))
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if not _EMAIL_RE.search(p):
                bad.append(p[:60])
    return bad

def _redact_output(text: str) -> str:
    """Redact PAN/PII from a result before it reaches the agent (never block reads).

    Contact identifiers (EMAIL, MOBILE, UPI) are deliberately PRESERVED: connector
    reads (e.g. Outlook inbox) legitimately carry sender/recipient addresses, and the
    agent needs them intact to reply/forward. Redacting a sender's address to "[EMAIL]"
    makes the follow-up outlook_reply/send resolve 0 recipients. Mirrors the write-path
    exclusion (_OUTBOUND_BLOCK_TYPES) and the /ask keep_types exemption.
    """
    from core.config import COMPLIANCE_SCAN_TOOL_RESULTS
    if not COMPLIANCE_SCAN_TOOL_RESULTS:
        logger.debug("cowork_mcp: output redaction skipped reason=tool_results_disabled")
        return text or ""
    try:
        from agents.compliance_engine import ComplianceEngine
        redacted, _types = ComplianceEngine().redact_text(
            text or "", keep_types={"EMAIL", "MOBILE", "UPI"}
        )
        return redacted
    except Exception as e:
        logger.debug(f"cowork_mcp: output redaction skipped → {e}")
        return text or ""

# Financial / secret compliance types that must NEVER leave the org in an
# outbound connector write (email / Teams message). Recipient-normal types
# (EMAIL, MOBILE, UPI) are deliberately excluded. Mirrors the BLOCKING_TYPES
# security set minus the contact identifiers.
_OUTBOUND_BLOCK_TYPES = {
    "PAN", "CVV", "EXPIRY", "PIN_BLOCK", "INDIA_PAN", "AADHAAR",
    "ACCOUNT_NUMBER", "ACCOUNT_NAME_COMBO", "IFSC_CODE",
    "SECRET", "API_KEY", "ACCESS_TOKEN",
    "PRIVATE_KEY_LEAK", "CERTIFICATE_LEAK", "SSH_KEY_LEAK",
    "KEY_ASSIGNMENT_LEAK", "PAYMENT_KEY_LEAK",
}

# ── tool catalog (per user) ──────────────────────────────────────────────────
def _write_tool_names(user_id: str) -> set:
    try:
        from connectors.registry import connector_registry
        return {
            f"{t['connector']}__{t['tool']}"
            for t in connector_registry.list_connected_tools(user_id)
            if t.get("is_write")
        }
    except Exception as e:
        logger.debug(f"cowork_mcp: write-tool lookup failed → {e}")
        return set()

def _connector_allowed(full_name: str, allowed) -> bool:
    """A connector tool 'connector__tool' is allowed if the role places either the
    bare connector slug OR the fully-qualified tool name on its allowlist. Empty/None
    allowlist = no per-role restriction (generic Cowork)."""
    if not allowed:
        return True
    if full_name in allowed:
        return True
    return full_name.split("__", 1)[0] in allowed

def _ensure_pat_connectors_connected(user_id: str) -> None:
    """
    Proactively auto-connect PAT connectors (GitLab, Jira) for this user.

    Root-cause fix for "GitLab tools missing from Buddy tool list":
    connector_registry.get_user_tools() only returns connectors that have an
    active row in user_oauth_tokens.  For PAT connectors the auto-connect logic
    in ConnectorEngine._try_auto_connect_pat() only fires on the first tool CALL
    — by which point the agent has already seen an empty tool list and given up.

    This function runs at the start of list_tools() so the user_oauth_tokens row
    is written BEFORE the tool list is built, making GitLab/Jira tools visible
    immediately after the user saves their PAT in Profile → API Token Vault.
    """
    _PAT_CONNECTORS = ("gitlab", "jira_connector")
    try:
        from connectors.engine import connector_engine
        from connectors.registry import connector_registry
        connected = set(connector_registry._get_connected_connectors(user_id))
        for connector_name in _PAT_CONNECTORS:
            if connector_name not in connected:
                try:
                    connector_engine._try_auto_connect_pat(user_id, connector_name)
                except Exception as _e:
                    # WARNING, not debug: a failure here means the user's GitLab/Jira
                    # tools are ABSENT from the tool list, so the agent silently falls
                    # back to a shell/git guess. This log is the only way to diagnose
                    # "Buddy went to the command line instead of GitLab".
                    logger.warning(
                        f"cowork_mcp: PAT auto-connect FAILED for {connector_name} "
                        f"(user={user_id}) → {_e}. Its tools will be MISSING from the "
                        f"Buddy tool list; check Profile → API Token Vault."
                    )
    except Exception as e:
        logger.warning(
            f"cowork_mcp: _ensure_pat_connectors_connected failed (user={user_id}) → {e}. "
            f"GitLab/Jira tools may be missing from the Buddy tool list."
        )


def list_tools(user_id: str, allowed=None) -> list:
    """MCP tools/list payload for this user: connected connector tools (optionally
    scoped to the selected role/plugin via `allowed`) + KB search + doc generation."""
    # Proactively auto-connect PAT connectors (GitLab, Jira) so their tools appear
    # in the list even before the first tool call.  No-op if already connected.
    _ensure_pat_connectors_connected(user_id)

    tools = []
    try:
        from connectors.registry import connector_registry
        writes = _write_tool_names(user_id)
        # Org/department connector policy (enterprise controls): an admin can
        # allow/deny a connector or a specific tool for the whole org or a dept.
        # Enforced here so the DESKTOP Cowork agent honours the same rules as the
        # server office path. Fail-open per-tool (never invent a denial).
        try:
            from services.cowork_policy import org_denies_tool as _org_denies
        except Exception:
            _org_denies = None
        # Tools hidden from Cowork — onedrive_upload causes the AI to attempt
        # base64-encoding local Windows files and uploading them to OneDrive,
        # which never works for email/Teams attachments. Local file attachments
        # must go via the paperclip (📎) upload flow, not OneDrive.
        _COWORK_HIDDEN = {"microsoft_365__onedrive_upload"}
        for t in connector_registry.get_user_tools(user_id):
            name = t["name"]  # e.g. microsoft_365__outlook_send_mail
            if name in _COWORK_HIDDEN:
                continue  # hidden from Cowork — see comment above
            if not _connector_allowed(name, allowed):
                continue  # role/plugin does not grant this connector
            if _org_denies is not None and "__" in name:
                _conn, _tool = name.split("__", 1)
                if _org_denies(user_id, _conn, _tool):
                    continue  # org/dept policy denies this connector/tool
            desc = t.get("description", "")
            if name in writes:
                desc = (f"{desc}  ⚠ WRITE action — requires explicit user confirmation; "
                        f"calling it here will NOT send. Propose the action and let the user approve.")
            # CLI v0.2.101 uses __ as its own server__tool delimiter, so a tool name
            # that already contains __ (e.g. microsoft_365__outlook_send_mail) causes
            # the qualified name ainxt_buddy__microsoft_365__outlook_send_mail to have
            # two __ separators and the CLI drops it with "Skipping MCP tool".
            # Expose the tool with a single-underscore connector-tool separator so the
            # qualified name is ainxt_buddy__microsoft_365_outlook_send_mail (one __).
            # call_tool() reverses this mapping before dispatching.
            exposed_name = name.replace("__", "_", 1) if "__" in name else name
            tools.append({
                "name": exposed_name,
                "description": desc,
                "inputSchema": t.get("input_schema") or {"type": "object", "properties": {}},
            })
        # Visibility for the "Buddy used the command line instead of GitLab/Jira"
        # class of bug: if the dev connectors did not make it into the tool list, the
        # agent literally cannot call them and will improvise. Log which ones landed
        # so this is diagnosable from the server log alone.
        _exposed = {t["name"] for t in tools}
        _dev_missing = [
            slug for slug, prefix in (("gitlab", "gitlab_"), ("jira", "jira_"))
            if not any(n.startswith(prefix) for n in _exposed)
        ]
        if _dev_missing:
            logger.info(
                f"cowork_mcp: tool list for user={user_id} has NO "
                f"{'/'.join(_dev_missing)} tools (not connected, or denied by "
                f"role/org policy) — the agent cannot answer MR/ticket questions"
            )
    except Exception as e:
        logger.warning(
            f"cowork_mcp: connector tool list FAILED (user={user_id}) → {e}. "
            f"The agent will see NO connector tools this session and may fall back "
            f"to shell/local guesses."
        )

    # NOTE: knowledge_base_search is intentionally NOT exposed in Cowork. The
    # platform KB lives in CHAT only (deliberate 10k-doc isolation + Cowork
    # parity: Cowork's knowledge comes from connectors + granted files, not a KB).
    tools.append({
        "name": _DOC_TOOL,
        "description": "Generate a plain Markdown (.md) document from content you provide. Returns a "
                       "[DOCJOB:...] marker — include it VERBATIM so the user gets a download button.\n"
                       "FOR Word/PowerPoint/Excel/PDF: do NOT use this tool — use the document SKILLS "
                       "(get_document_skill then build_document) for high-quality, editable, previewable files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["md"], "description": "Markdown only (use the skill for docx/pptx/xlsx/pdf)"},
                "title": {"type": "string", "description": "Document title"},
                "content_md": {"type": "string", "description": "Full document content as markdown"},
            },
            "required": ["format", "title", "content_md"],
        },
    })

    # ── Claude document SKILLS (Word/PowerPoint/Excel/PDF — true Cowork parity) ──
    # The agent reads the skill, authors the build code per its rules, and runs it
    # in the isolated doc sandbox → professionally styled, EDITABLE file + preview.
    tools.append({
        "name": _SKILL_TOOL,
        "description": "Read the document skill for a format — the exact rules, fill-in skeleton and "
                       "composition-wrapper API for a professional, on-brand file. ALWAYS call this FIRST "
                       "before building any Word (.docx), PowerPoint (.pptx), Excel (.xlsx), or PDF document, "
                       "then follow its guidance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["docx", "pptx", "xlsx", "pdf"], "description": "Format whose skill to read"},
            },
            "required": ["format"],
        },
    })
    tools.append({
        "name": _BUILD_TOOL,
        "description": "Build a document by running the code you authored (per the skill from get_document_skill) "
                       "in a secure sandbox. Use the preinstalled composition wrapper for the `format`:\n"
                       "  • docx → JS, const doc = require('ainxt-doc'); … d.save()  → /work/output.docx\n"
                       "  • pptx → JS, const deck = require('ainxt-deck'); … d.save() → /work/output.pptx\n"
                       "  • xlsx → Python, from ainxt_sheet import Book; … b.save()   → /work/output.xlsx\n"
                       "  • pdf  → JS, require('ainxt-doc') (authored as a Word doc, auto-exported to a polished PDF)\n"
                       "Returns a [DOCJOB:...] marker — include it VERBATIM so the user gets a rendered preview + "
                       "download. If the build errors, the message tells you what to fix; correct and call again. "
                       "ITERATIVE EDITING: to revise, call again with updated code.\n"
                       "IMAGES (multimodal): to embed AI-GENERATED visuals (cover art, illustrations, concept "
                       "graphics), pass `images`: each {name, prompt} is generated by an approved provider "
                       "(Imagen/DALL-E) and placed in the build dir as `name` BEFORE your code runs — reference it "
                       "by that filename in your code (the wrapper's image helper / native chart). Prefer real DATA "
                       "charts via the wrapper's native charting, not images.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "format": {"type": "string", "enum": ["docx", "pptx", "xlsx", "pdf"], "description": "Document format"},
                "title": {"type": "string", "description": "Document title (used for the filename)"},
                "code": {"type": "string", "description": "Build code in the language for `format` (see above)"},
                "artifact_id": {"type": "string", "description": "To REVISE an earlier document as a new "
                                "version (keeping history), pass the artifact_id returned by its previous "
                                "build. Omit for a brand-new document."},
                "images": {
                    "type": "array",
                    "description": "Optional AI-generated images to embed. Each is written to the build dir as "
                                   "`name` before the code runs; reference it by that filename.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Filename to save as (e.g. 'cover.png')."},
                            "prompt": {"type": "string", "description": "What the image should depict."},
                            "aspect_ratio": {"type": "string", "description": "1:1 | 16:9 | 9:16 | 4:3 (default 16:9)."},
                        },
                        "required": ["name", "prompt"],
                    },
                },
            },
            "required": ["format", "title", "code"],
        },
    })

    # Version history for an iterated document (Canvas/Pages parity).
    tools.append({
        "name": _VERSIONS_TOOL,
        "description": "List the version history of a document you built (by its artifact_id) — every "
                       "revision with its version number, title, format and timestamp. Use it to tell the "
                       "user what versions exist, or to pick which version to revise.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The document's artifact_id (from a build_document result)."},
            },
            "required": ["artifact_id"],
        },
    })

    # ── Deep research (multi-model, cross-vendor) — AiNxt's standout ──────────
    # No competitor cross-examines across model vendors. This decomposes a
    # question, analyses each angle with DIFFERENT models (Claude + GPT),
    # synthesises a cited report, then has the OTHER vendor independently review
    # it. The agent should first GATHER material (emails, docs, files, pages) and
    # pass it in `sources` so the report carries real [n] citations.
    tools.append({
        "name": _RESEARCH_TOOL,
        "description": "Produce a rigorous, decision-ready RESEARCH REPORT using AiNxt's multi-model engine: "
                       "the question is decomposed into angles, each analysed by a DIFFERENT model "
                       "multiple models, synthesised into a cited report, then INDEPENDENTLY "
                       "reviewed by the other vendor's model. Use this for any 'research / analyse / compare / "
                       "brief me on / write a report on' task. For grounded, citable output, FIRST gather "
                       "material with your connector/file tools (emails, Teams, SharePoint, Drive, attachments) "
                       "and pass each item in `sources` — the report will cite them as [n]. Returns Markdown; "
                       "to deliver as a file, pass `build` or follow with get_document_skill + build_document.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The research question / topic to investigate."},
                "depth": {"type": "string", "enum": ["quick", "standard", "deep"],
                          "description": "quick=3 angles (fast, default), standard=5, deep=7.", "default": "quick"},
                "sources": {
                    "type": "array",
                    "description": "Material you gathered to ground the report (cited as [n]). Omit for an "
                                   "analytical synthesis from model knowledge.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "Source name (email subject, doc title, page)."},
                            "content": {"type": "string", "description": "The source text/excerpt."},
                            "url": {"type": "string", "description": "Optional link/reference."},
                        },
                        "required": ["content"],
                    },
                },
                "build": {"type": "string", "enum": ["docx", "pdf", "pptx"],
                          "description": "Optional: also note how to deliver the report as this file format."},
            },
            "required": ["query"],
        },
    })

    # Sandboxed code execution for DATA ANALYSIS — runs server-side in an
    # isolated, network-disabled, ephemeral container (Cowork's "code in
    # a VM"). This is NOT a shell on the user's machine: it cannot reach the
    # user's OS, the network, or any connector — it only computes over data the
    # agent passes in and returns the printed result.
    tools.append({
        "name": _CODE_TOOL,
        "description": "Run a short script in a SECURE SANDBOX to compute or analyse data — totals, "
                       "averages, parsing/reshaping a CSV/JSON, generating a chart's numbers, date math, "
                       "etc. The sandbox has NO internet, NO access to the user's computer, and is destroyed "
                       "after each run; only what the script prints (stdout) is returned. Use this instead of "
                       "doing arithmetic in your head when accuracy matters. Default language is python "
                       "(pandas-style stdlib). Put the data INTO the script (e.g. as a variable) — the sandbox "
                       "cannot open the user's files or fetch URLs. Never put secrets/card numbers in code.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The script to run. Print results to stdout."},
                "language": {"type": "string", "description": "Language (default 'python').", "default": "python"},
            },
            "required": ["code"],
        },
    })

    # Data analysis on a dataset (ADA parity). The dataset is BOUND as a file in
    # the sandbox so the script reads it via open() — keeps the code clean and lets
    # the agent analyse a real uploaded/fetched file (CSV/TSV/JSON) accurately.
    tools.append({
        "name": _ANALYZE_TOOL,
        "description": "Analyse a DATASET accurately in the secure sandbox. Pass the dataset text in `data` "
                       "(CSV/TSV/JSON — e.g. the contents of a file you Read or a connector export) and a "
                       "Python `code` script that reads the bound file by `filename` and PRINTS the results. "
                       "Use this for totals, averages, group-bys, distributions, growth/trends, outliers — "
                       "anything where exact numbers matter. The sandbox is network-isolated and ephemeral; "
                       "use Python stdlib (csv, json, statistics, math). For a chart or a polished table, take "
                       "the numbers this returns and build a spreadsheet/deck with build_document.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "The dataset content as text (CSV/TSV/JSON)."},
                "filename": {"type": "string", "description": "Name the data is saved as in the sandbox "
                             "(your code opens this). Default 'data.csv'.", "default": "data.csv"},
                "code": {"type": "string", "description": "Python script: open(filename), compute, print results. "
                         "Stdlib only (csv/json/statistics/math)."},
            },
            "required": ["data", "code"],
        },
    })

    # AI co-edit an existing document (Canvas/Pages parity): apply a change to a
    # prior build's source and produce a NEW version, keeping history. Cheaper and
    # more reliable than regenerating from scratch.
    tools.append({
        "name": _REVISE_TOOL,
        "description": "Revise a document you already built (by its artifact_id) with a natural-language "
                       "instruction — e.g. 'shorten the summary', 'add a risks section', 'change the tone to "
                       "formal', 'swap the cover image'. It loads the latest version, applies your change, and "
                       "produces a NEW version (history kept). Use this for follow-up edits instead of "
                       "rewriting the whole build from scratch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "The document's artifact_id (from a build_document result)."},
                "instruction": {"type": "string", "description": "The change to apply, in plain language."},
            },
            "required": ["artifact_id", "instruction"],
        },
    })

    # Durable memory: the agent persists a fact so it carries across tasks.
    tools.append({
        "name": _MEMORY_TOOL,
        "description": "Save a durable fact to your persistent Cowork memory so you remember it in "
                       "FUTURE tasks (it is injected into your system prompt every session). Use this when "
                       "the user states a lasting preference, recurring context, key contact/channel, or "
                       "decision you should not have to ask about again (e.g. 'the settlement deck always "
                       "uses the corporate template', 'my manager is Priya', 'report figures in INR "
                       "crore'). Keep each note to one short sentence. Do NOT store secrets, passwords, card "
                       "numbers, or other sensitive data. Save only what helps you serve this user better.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "One short fact/preference to remember long-term."},
            },
            "required": ["note"],
        },
    })
    return tools

# ── tool dispatch ────────────────────────────────────────────────────────────
def _text_result(text: str, is_error: bool = False) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if is_error:
        out["isError"] = True
    return out

def call_tool(user_id: str, name: str, arguments: dict, allowed=None) -> dict:
    """Execute one tool call. Returns an MCP tool-result dict. `allowed` (the
    selected role/plugin's connector allowlist) hard-restricts connector calls.

    Wrapped in an OTLP span (no-op unless telemetry configured) so enterprise
    dashboards see every Cowork tool call — name + connector + status only, NEVER
    the arguments/results (those are compliance-sensitive)."""
    from core.otel import cowork_span
    # CLI v0.2.101 drops tools whose name contains __ (its own server__tool delimiter).
    # list_tools() exposes connector tools with a single _ between connector and tool
    # (e.g. microsoft_365_outlook_send_mail). Reverse that mapping here so the rest
    # of the dispatch chain sees the canonical double-underscore name.
    name = _restore_tool_name(name)
    connector_slug = name.split("__", 1)[0] if "__" in name else name
    with cowork_span("cowork.tool_call", **{
        "cowork.tool": name,
        "cowork.connector": connector_slug,
        "enduser.id": str(user_id),
    }) as span:
        result = _call_tool_inner(user_id, name, arguments, allowed)
        if span is not None:
            try:
                span.set_attribute("cowork.is_error", bool(result.get("isError")))
            except Exception:
                pass
        return result


# Known connector names (from connector_definitions) — used to reverse the
# single-underscore tool name back to the canonical double-underscore form.
# e.g. microsoft_365_outlook_send_mail → microsoft_365__outlook_send_mail
_KNOWN_CONNECTOR_PREFIXES = (
    "microsoft_365_", "gmail_", "jira_connector_", "slack_", "github_",
    "confluence_", "jira_", "google_drive_", "onedrive_", "sharepoint_",
    "gitlab_",  # GitLab PAT connector — tools exposed as gitlab_list_issues etc.
)

def _restore_tool_name(name: str) -> str:
    """Reverse the single→double underscore rename done in list_tools().
    microsoft_365_outlook_send_mail → microsoft_365__outlook_send_mail"""
    for prefix in _KNOWN_CONNECTOR_PREFIXES:
        if name.startswith(prefix) and "__" not in name:
            connector = prefix.rstrip("_")
            tool = name[len(prefix):]
            return f"{connector}__{tool}"
    return name

def _call_tool_inner(user_id: str, name: str, arguments: dict, allowed=None) -> dict:
    arguments = arguments or {}

    block = _block_input(arguments)
    if block:
        return _text_result(f"[BLOCKED] {block}", is_error=True)

    if name == _DOC_TOOL:
        return _generate_document(user_id, arguments)

    if name == _MEMORY_TOOL:
        return _remember(user_id, arguments)

    if name == _CODE_TOOL:
        return _run_code(arguments)

    if name == _SKILL_TOOL:
        return _get_document_skill(arguments)

    if name == _BUILD_TOOL:
        return _build_document(user_id, arguments)

    if name == _RESEARCH_TOOL:
        return _deep_research(user_id, arguments)

    if name == _VERSIONS_TOOL:
        return _list_document_versions(user_id, arguments)

    if name == _ANALYZE_TOOL:
        return _analyze_data(arguments)

    if name == _REVISE_TOOL:
        return _revise_artifact(user_id, arguments)

    if "__" in name:
        if not _connector_allowed(name, allowed):
            return _text_result(
                "This connector is not available for the selected role/plugin.", is_error=True)
        connector, _, tool = name.partition("__")
        return _connector_call(user_id, connector, tool, name, arguments)

    return _text_result(f"Unknown tool: {name}", is_error=True)

def _kb_search(query: str) -> dict:
    try:
        from agents.state import AgentState
        from agents.tools import retrieve_tool
        st = AgentState(question=str(query or ""))
        st = retrieve_tool(st) or st
        ctx = getattr(st, "context", None) or ""
        if isinstance(ctx, (list, tuple)):
            ctx = "\n\n".join(str(c) for c in ctx)
        text = (ctx or "").strip() or "No relevant Knowledge Base passages found."
        return _text_result(_redact_output(text)[:8000])
    except Exception as e:
        return _text_result(f"Knowledge Base search error: {e}", is_error=True)

def _md_to_sections(md: str) -> list:
    """Parse the agent-authored markdown into the doc worker's section structure
    deterministically — NO second LLM call (model-agnostic). The agent already
    wrote the content; we render exactly that."""
    sections, cur = [], None

    def _new(heading="", level=2):
        s = {"heading": heading, "content": "", "bullets": [], "level": min(max(level, 2), 4)}
        sections.append(s)
        return s

    for line in (md or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            level = len(s) - len(s.lstrip("#"))
            heading = s.lstrip("#").strip()
            if level <= 1:        # top-level heading == the title; skip
                continue
            cur = _new(heading, level)
        elif s[:2] in ("- ", "* ") or s.startswith("• "):
            cur = cur or _new()
            cur["bullets"].append(s.lstrip("-*• ").strip())
        else:
            cur = cur or _new()
            cur["content"] = (cur["content"] + " " + s).strip() if cur["content"] else s
    if not sections:
        sections = [{"heading": "", "content": (md or "").strip(), "bullets": [], "level": 2}]
    return sections

def _generate_document(user_id: str, args: dict) -> dict:
    """Enqueue a real document-generation job (same worker the browser uses) and
    return a [DOCJOB:...] marker the renderer turns into a download button."""
    fmt = (args.get("format") or "docx").lower()
    title = (args.get("title") or "Document").strip()
    content_md = args.get("content_md") or ""
    # Compliance: hard-block sensitive content on this OUTBOUND deliverable.
    blk = _block_input({"title": title, "content_md": content_md})
    if blk:
        return _text_result(f"[BLOCKED] Document not generated: {blk}", is_error=True)
    try:
        import uuid
        from core.job_queue import Q_DOC, enqueue_job
        job_id = str(uuid.uuid4())
        enqueue_job(
            "workers.doc_worker.generate_doc_job",
            {
                # Pre-structured sections → the worker renders directly, no LLM
                # restructuring (deterministic + model-agnostic). content_md kept
                # for the audit trail + compliance.
                "job_id": job_id, "format": fmt, "title": title,
                "sections": _md_to_sections(content_md),
                "content_md": content_md, "user_id": str(user_id),
                "chat_id": (args.get("chat_id") or None),   # link to conversation memory if provided
                "use_template": fmt == "pptx",
            },
            queue_name=Q_DOC, timeout=180, retry_count=1,
        )
        _safe_base = "".join(c for c in title if c.isalnum() or c in " -_")[:40] or "document"
        _deliver_ext = {"pdf": "pdf", "docx": "docx", "pptx": "pptx", "xlsx": "xlsx"}.get(fmt, fmt)
        safe_name = f"{_safe_base}.{_deliver_ext}"
        return _text_result(
            f"Document queued for generation. Include this marker EXACTLY in your reply so the user gets a "
            f"download button:\n[DOCJOB:{job_id}:{fmt}:{safe_name}]\n"
            f"Then tell the user they can ask for changes (shorten, add sections, change tone) and you'll "
            f"regenerate an updated version."
        )
    except Exception as e:
        return _text_result(f"Document generation failed to enqueue: {e}", is_error=True)

# In-house AiNxt document craft skills (skills/ainxt_doc_craft/<fmt>/...).
# The agent is handed the SKILL.md + fill-in SKELETON and authors code with the
# composition wrappers (ainxt-doc / ainxt-deck / ainxt_sheet) preinstalled in the
# doc sandbox (see docker/doc-sandbox/Dockerfile).
_DOCSKILLS_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "skills", "ainxt_doc_craft")
_SKILL_FORMATS = {"docx", "pptx", "xlsx", "pdf"}

# Which skill file(s) to hand the agent per format, + how it must write output.
# ainxt_doc_craft has NO pdf/ folder — pdf is authored as a Word doc (ainxt-doc)
# then exported to a polished PDF by the sandbox, so it reuses the docx skill.
# The SKELETON is a working fill-in template using the wrapper for that format.
_SKILL_PLAN = {
    "docx": {"files": [("docx", "SKILL.md"), ("docx", "SKELETON.js")],
             "write": "JavaScript using the preinstalled `ainxt-doc` module (const doc = require('ainxt-doc')) that finishes with d.save(), writing /work/output.docx"},
    "pptx": {"files": [("pptx", "SKILL.md"), ("pptx", "SKELETON.js")],
             "write": "JavaScript using the preinstalled `ainxt-deck` module (const deck = require('ainxt-deck')) that finishes with d.save(), writing /work/output.pptx"},
    "xlsx": {"files": [("xlsx", "SKILL.md"), ("xlsx", "SKELETON.py")],
             "write": "Python using the preinstalled `ainxt_sheet` module (from ainxt_sheet import Book) that finishes with b.save(), writing /work/output.xlsx"},
    "pdf":  {"files": [("docx", "SKILL.md"), ("docx", "SKELETON.js")],
             "write": "JavaScript using the preinstalled `ainxt-doc` module (const doc = require('ainxt-doc')) that finishes with d.save(), writing /work/output.docx (automatically exported to a polished PDF) — author it exactly as a Word document"},
}

def _get_document_skill(args: dict) -> dict:
    """Return the SKILL.md (+ fill-in SKELETON) for a document format so the agent can
    author code per the platform composition-wrapper rules (skill-driven generation)."""
    fmt = (args.get("format") or "docx").lower()
    plan = _SKILL_PLAN.get(fmt)
    if not plan:
        return _text_result(
            f"No document skill for {fmt!r}. Supported: docx, pptx, xlsx, pdf.", is_error=True)
    chunks = []
    for sub, fname in plan["files"]:
        path = _os.path.join(_DOCSKILLS_DIR, sub, fname)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                chunks.append(f"=== {sub}/{fname} ===\n{fh.read()}")
        except Exception as e:
            logger.warning(f"cowork_mcp: skill file load failed {path} → {e}")
    if not chunks:
        return _text_result(f"Could not load the {fmt} skill files.", is_error=True)
    md = "\n\n".join(chunks)
    if len(md) > 36000:  # keep agent context bounded
        md = md[:36000] + "\n\n[... truncated; the rules above cover creation ...]"
    brand = _load_brand_guide()
    note = (
        f"This is the {fmt.upper()} document skill. Follow these rules AND the brand guidelines "
        f"below, then call build_document with format='{fmt}' and `code` = {plan['write']}.\n\n"
        f"{brand}\n\n{md}"
    )
    return _text_result(note)

def _load_brand_guide() -> str:
    """Brand guidelines, injected into every document skill so all generated
    docs carry the configured org identity (colours, typography, title bar, footer, logo).
    Path is controlled by DOC_BRAND_FILE env var (default: brand/BRAND.md).
    Internal/enterprise: set DOC_BRAND_FILE=brand/INTERNAL_BRAND.md in your .env."""
    _brand_rel = _os.getenv("DOC_BRAND_FILE", "brand/BRAND.md")
    path = _os.path.join(_DOCSKILLS_DIR, _brand_rel)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return "# Brand: use deep navy (#1F3864) headings/title bar, Arial, 'Confidential' footer."

def _build_document(user_id: str, args: dict) -> dict:
    """Enqueue a skill-driven document build: agent-authored code runs in the
    isolated doc sandbox → styled, editable file + in-app preview. Returns a
    [DOCJOB:...] marker (same download/preview flow as generate_document).
    Formats: docx/pptx (node, ainxt-doc/ainxt-deck), xlsx (python, ainxt_sheet), pdf (ainxt-doc → exported)."""
    import uuid as _uuidmod
    fmt = (args.get("format") or "docx").lower()
    title = (args.get("title") or "Document").strip()
    code = args.get("code") or ""
    # Iterative editing: a revision passes the artifact_id from a prior build; a
    # brand-new doc gets a fresh artifact_id so the agent can revise it later.
    artifact_id = (args.get("artifact_id") or "").strip() or str(_uuidmod.uuid4())
    is_revision = bool((args.get("artifact_id") or "").strip())
    # Optional AI-generated images embedded into the doc (bounded; sanitised in
    # the executor). Only name/prompt/aspect are honoured.
    images = []
    for im in (args.get("images") or [])[:8]:
        if isinstance(im, dict) and (im.get("prompt") or "").strip() and (im.get("name") or "").strip():
            images.append({"name": str(im["name"])[:64], "prompt": str(im["prompt"])[:600],
                           "aspect_ratio": str(im.get("aspect_ratio") or "16:9")[:8]})
    if fmt not in _SKILL_FORMATS:
        return _text_result(
            f"build_document supports docx, pptx, xlsx, pdf (got {fmt!r}).", is_error=True)
    if not code.strip():
        # The agent sometimes calls build_document before writing the code. Return
        # actionable GUIDANCE (NOT is_error, so the agent retries instead of giving
        # up) telling it to write the full script and call again.
        _src = {"docx": "JavaScript using require('ainxt-doc'), ending with d.save()",
                "pptx": "JavaScript using require('ainxt-deck'), ending with d.save()",
                "xlsx": "Python using `from ainxt_sheet import Book`, ending with b.save()",
                "pdf":  "JavaScript using require('ainxt-doc'), ending with d.save() (authored as a Word doc)"}.get(fmt, "build script")
        return _text_result(
            f"build_document was called with an EMPTY `code`. Write the COMPLETE {fmt} build "
            f"script now ({_src}, per the rules from get_document_skill) and call build_document "
            f"AGAIN with the full script in the `code` argument. Do not call it without code.",
            is_error=False)
    # Soft library-check: if the code doesn't reference the expected wrapper
    # library for this format, the sandbox will almost certainly fail with a cryptic
    # Node/Python error. Guide the agent to call get_document_skill first so it gets
    # the correct template — this is NOT is_error so the agent retries rather than
    # giving up. Only fires when the library name is completely absent (a partial
    # import or a comment containing it still passes).
    _expected_lib = {
        "docx": "ainxt-doc", "pdf": "ainxt-doc",
        "pptx": "ainxt-deck",
        "xlsx": "ainxt_sheet",
    }.get(fmt, "")
    if _expected_lib and _expected_lib not in code:
        return _text_result(
            f"The build code doesn't use `{_expected_lib}` (the required library for {fmt.upper()} "
            f"documents). Call `get_document_skill` with format='{fmt}' first to get the correct "
            f"template and API reference, then write the complete build script and call "
            f"build_document again with the full code.",
            is_error=False)
    # Compliance: AUDIT-and-proceed, never hard-block (redact-don't-block rule).
    # The build code runs in a network-isolated sandbox and produces a LOCAL file
    # the same authenticated user downloads — it is NOT an outbound send, and the
    # user's request already passed input compliance. Business figures the LLM
    # writes into the doc (transaction counts, ₹ values, large chart integers)
    # routinely trip the Luhn/account heuristic as false positives; hard-blocking
    # them kills legitimate documents. We log the finding for audit and build the
    # doc with the ORIGINAL code (redacting inside code would corrupt JS/Python
    # syntax). Genuine outbound leakage is still blocked at the connector-write
    # boundary (_OUTBOUND_BLOCK_TYPES), which is where it matters.
    blk = _block_input({"code": code, "title": title})
    if blk:
        logger.info(f"cowork_mcp: doc build compliance flag (audited, not blocked) "
                    f"user={user_id} fmt={fmt} reason={blk}")
    try:
        from core.job_queue import Q_DOC, enqueue_job
        job_id = str(_uuidmod.uuid4())
        enqueue_job(
            "workers.doc_skill_worker.build_doc_skill_job",
            {"job_id": job_id, "format": fmt, "title": title, "code": code,
             "images": images, "artifact_id": artifact_id,
             # Link Buddy-generated docs to the conversation when the session
             # supplies one, so they land in that chat's document memory
             # (recall/revise parity with Chat). Optional & backward-compatible.
             "user_id": str(user_id), "chat_id": (args.get("chat_id") or None)},
            queue_name=Q_DOC, timeout=1800, retry_count=0,   # 30 min — slow PPT/PDF renders
        )
        _safe_base = "".join(c for c in title if c.isalnum() or c in " -_")[:40] or "document"
        # Include the file extension in the marker filename so the frontend
        # DocDownloadButton shows the correct name (e.g. "Document.pdf") and
        # the browser download dialog opens the file with the right application.
        # Mirrors doc_download_router.py's build_doc_marker() which always uses
        # a filename that includes the extension.
        _deliver_ext = {"pdf": "pdf", "docx": "docx", "pptx": "pptx", "xlsx": "xlsx"}.get(fmt, fmt)
        safe_name = f"{_safe_base}.{_deliver_ext}"
        kind = {"docx": "Word document", "pptx": "presentation", "xlsx": "spreadsheet", "pdf": "PDF"}.get(fmt, "document")
        verb = "Rebuilding a new version of" if is_revision else "Building"
        return _text_result(
            f"{verb} your {kind} in the secure sandbox. Include this marker EXACTLY in your reply "
            f"so the user gets a rendered preview + download button:\n[DOCJOB:{job_id}:{fmt}:{safe_name}]\n"
            f"This document's artifact_id is `{artifact_id}` — to REVISE it later (new version, keeping "
            f"history), call build_document again with artifact_id='{artifact_id}' and the updated code. "
            f"Tell the user they can ask for edits (shorten, add a slide/section, change wording, swap an "
            f"image) and you'll produce an updated version."
        )
    except Exception as e:
        return _text_result(f"Document build failed to enqueue: {e}", is_error=True)

def _list_document_versions(user_id: str, args: dict) -> dict:
    """Return the version history for a document artifact (RBAC-scoped to the
    requesting user). Used for iterative editing — see what versions exist."""
    artifact_id = (args.get("artifact_id") or "").strip()
    if not artifact_id:
        return _text_result("Provide the document's `artifact_id` to list its versions.", is_error=True)
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            rows = (db.query(GeneratedDocument)
                    .filter(GeneratedDocument.artifact_id == artifact_id,
                            GeneratedDocument.user_id == str(user_id))
                    .order_by(GeneratedDocument.version.asc())
                    .all())
        finally:
            db.close()
        if not rows:
            return _text_result(f"No versions found for artifact_id `{artifact_id}` (or not yours).")
        lines = [f"Document **{rows[-1].title}** — {len(rows)} version(s):"]
        for r in rows:
            ts = r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "?"
            lines.append(f"- v{r.version} · {r.format} · {ts} · file_id `{r.id}`")
        lines.append(f"\nTo revise, call build_document with artifact_id='{artifact_id}' and updated code.")
        return _text_result("\n".join(lines))
    except Exception as e:
        return _text_result(f"Could not list versions: {e}", is_error=True)

def _revise_artifact(user_id: str, args: dict) -> dict:
    """AI co-edit (Canvas/Pages parity): load a document's latest version source,
    apply a natural-language edit with the LLM, and rebuild as a NEW version —
    keeping full history. Far cheaper for the agent than regenerating from scratch,
    and it's the server engine a collaborative canvas UI drives."""
    artifact_id = (args.get("artifact_id") or "").strip()
    instruction = (args.get("instruction") or "").strip()
    if not artifact_id or not instruction:
        return _text_result("revise_artifact needs `artifact_id` and `instruction` (what to change).", is_error=True)
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            row = (db.query(GeneratedDocument)
                   .filter(GeneratedDocument.artifact_id == artifact_id,
                           GeneratedDocument.user_id == str(user_id))
                   .order_by(GeneratedDocument.version.desc())
                   .first())
        finally:
            db.close()
    except Exception as e:
        return _text_result(f"Could not load the document to revise: {e}", is_error=True)
    if not row:
        return _text_result(f"No document found for artifact_id `{artifact_id}` (or not yours).", is_error=True)

    fmt = (row.format or "docx").lower()
    source = row.content_md or ""
    title = row.title or "Document"
    if not source.strip():
        return _text_result(
            "That version has no stored editable source to revise from — rebuild it with build_document.",
            is_error=True)

    is_md = fmt == "md"
    kind = "Markdown document" if is_md else f"{fmt} build script"
    from models.model_router import model_router
    revised = (model_router.generate(
        f"You are editing a {kind} (the current source is below). Apply this change EXACTLY and return "
        f"ONLY the full revised {'markdown' if is_md else 'script'} — no commentary, no code fences.\n\n"
        f"CHANGE REQUESTED: {instruction}\n\nCURRENT SOURCE:\n{source[:60000]}",
        model_hint="complex") or "").strip()
    # Strip accidental code fences the model may add.
    if revised.startswith("```"):
        revised = revised.split("\n", 1)[-1]
        if revised.rstrip().endswith("```"):
            revised = revised.rstrip()[:-3]
    revised = revised.strip()
    if not revised:
        return _text_result("The revision produced no output — try rephrasing the change.", is_error=True)

    if is_md:
        return _generate_document(user_id, {"format": "md", "title": title, "content_md": revised})
    return _build_document(user_id, {"format": fmt, "title": title, "code": revised, "artifact_id": artifact_id})

_RESEARCH_DEPTHS = {"quick": 3, "standard": 5, "deep": 7}
# Cross-vendor rotation: primary model (complex) ↔ secondary model (medium). This
# multi-model diversity is the differentiator — angles get genuinely different
# reasoning, and the synthesis is reviewed by the OTHER model. Model-agnostic:
# uses model_router hints, never hardcodes a provider SDK.
# Angles use the fast hint for SPEED (avoids per-angle retry latency that timed
# out the CLI). Cross-model diversity is preserved at the synthesis (complex) +
# independent review (medium) stages.
_RESEARCH_ROTATION = ["haiku", "haiku"]
_RESEARCH_LABELS = {
    "medium": "Secondary model",
    "complex": "Primary model",
    "haiku": "Fast model",
}

def _deep_research(user_id: str, args: dict) -> dict:
    """Multi-model, cross-vendor research report (AiNxt standout).

    Pipeline: decompose (GPT) → per-angle analysis (Claude + GPT, in parallel) →
    synthesis with [n] citations (Claude) → independent cross-vendor review (GPT).
    Grounded on `sources` the agent gathered; honest about un-sourced claims.
    """
    import concurrent.futures as _cf

    query = (args.get("query") or "").strip()
    if not query:
        return _text_result("deep_research needs a `query` (the question to investigate).", is_error=True)
    depth = (args.get("depth") or "quick").lower()   # quick by default = fast enough for the CLI tool timeout
    n_angles = _RESEARCH_DEPTHS.get(depth, 3)

    # Normalise + bound the gathered sources (cited as [n]).
    norm_sources = []
    for i, s in enumerate(args.get("sources") or [], start=1):
        if isinstance(s, str):
            s = {"title": f"Source {i}", "content": s}
        if not isinstance(s, dict):
            continue
        content = str(s.get("content") or "").strip()[:4000]
        if not content:
            continue
        norm_sources.append({
            "n": len(norm_sources) + 1,
            "title": str(s.get("title") or f"Source {len(norm_sources) + 1}").strip()[:200],
            "content": content,
            "url": str(s.get("url") or "").strip()[:500],
        })
        if len(norm_sources) >= 20:
            break

    if norm_sources:
        src_block = "\n\n".join(f"[{s['n']}] {s['title']}\n{s['content']}" for s in norm_sources)
        grounding = ("You are given NUMBERED SOURCES below. Base factual claims on them and cite with "
                     "[n] matching the source number. If the sources don't cover something, say so plainly. "
                     "NEVER invent a citation number that isn't in the sources.")
    else:
        src_block = ("(no external sources attached — produce an analytical synthesis from domain knowledge "
                     "and clearly flag any claim that would need source verification)")
        grounding = ("No external sources were attached. Reason analytically and clearly flag claims needing "
                     "verification. Do NOT fabricate citations or source numbers.")

    from models.model_router import model_router

    def _gen(prompt: str, hint: str) -> str:
        try:
            out = (model_router.generate(prompt, model_hint=hint) or "").strip()
            # generate() returns an error string on failure rather than raising.
            if out.lower().startswith(("error:", "[error", "llm proxy")):
                logger.warning(f"deep_research: model {hint} returned error → {out[:120]}")
                return ""
            return out
        except Exception as e:
            logger.warning(f"deep_research: model {hint} call failed → {e}")
            return ""

    def _gen_bounded(prompt: str, hint: str, timeout_s: float) -> str:
        """Run _gen with a hard wall-clock cap so a slow/retrying model can never
        blow the CLI's tool-call timeout — we return what we have instead."""
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(_gen, prompt, hint)
            try:
                return fut.result(timeout=timeout_s)
            except Exception:
                logger.warning(f"deep_research: {hint} exceeded {timeout_s}s — skipping")
                return ""

    # 1) DECOMPOSE — plan the investigation (fast model hint).
    decomp = _gen(
        f"You are a senior research lead. Plan an investigation of this question:\n\n{query}\n\n"
        f"Break it into exactly {n_angles} distinct, non-overlapping sub-questions that together fully "
        f"answer it. Each must be specific and independently researchable. Return ONLY the sub-questions, "
        f"one per line, no numbering, no preamble.",
        "haiku",
    )
    angles = [ln.strip(" -•\t0123456789.").strip() for ln in decomp.splitlines() if ln.strip()][:n_angles]
    angles = [a for a in angles if len(a) > 8] or [query]

    # 2) PER-ANGLE ANALYSIS — cross-vendor, run in parallel.
    def _analyze(item):
        idx, angle = item
        hint = _RESEARCH_ROTATION[idx % len(_RESEARCH_ROTATION)]
        text = _gen(
            f"{grounding}\n\nSOURCES:\n{src_block}\n\n"
            f"OVERALL QUESTION: {query}\nSUB-QUESTION TO ANSWER NOW: {angle}\n\n"
            f"Give a specific analysis of ONLY this sub-question (100–150 words). Cite [n] where a source "
            f"supports a claim. Be concrete — figures, named factors, trade-offs. No filler.",
            hint,
        )
        return {"angle": angle, "hint": hint, "text": text}

    with _cf.ThreadPoolExecutor(max_workers=min(4, max(1, len(angles)))) as ex:
        findings = [f for f in ex.map(_analyze, list(enumerate(angles))) if f["text"]]

    if not findings:
        return _text_result(
            "deep_research could not produce findings — the model backend returned nothing. "
            "Try again, or narrow the question.", is_error=True)

    # 3) SYNTHESIS — final cited report (primary/complex model hint).
    findings_block = "\n\n".join(f"### {f['angle']}\n{f['text']}" for f in findings)
    report = _gen_bounded(
        f"{grounding}\n\nSOURCES:\n{src_block}\n\nRESEARCH QUESTION: {query}\n\n"
        f"FINDINGS FROM YOUR RESEARCH TEAM:\n{findings_block}\n\n"
        f"Write a CONCISE, decision-ready research brief in Markdown (aim ~400 words), with EXACTLY:\n"
        f"# {query}\n"
        f"## Executive Summary  (3–4 sentences)\n"
        f"## Key Findings  (4–6 bullets; add [n] citations where sources support them)\n"
        f"## Recommendations  (3–4 specific, actionable bullets)\n"
        f"Use inline [n] citations matching the numbered sources. Be specific. Keep it tight. "
        f"Do not invent sources or citations.",
        "complex", 28.0,
    ) or findings_block

    # 4) INDEPENDENT CROSS-VENDOR REVIEW — the OTHER vendor critiques (GPT-5.4).
    # Hard-capped: if GPT is slow/retrying, skip it rather than time out the tool;
    # the report still returns (graceful — the review is a bonus, not a blocker).
    critique = _gen_bounded(
        f"You are a skeptical peer reviewer from a different team. Here is a research report answering: "
        f"{query}\n\nREPORT:\n{report[:6000]}\n\n"
        f"In 3–4 terse bullets: flag the weakest or unsupported claims and the most important MISSING angle. "
        f"Be concrete.",
        "medium", 14.0,
    )

    # 5) ASSEMBLE — report + sources + cross-model review + transparent method footer.
    models_used = sorted({f["hint"] for f in findings} | {"complex", "medium"})
    method = ", ".join(_RESEARCH_LABELS.get(m, m) for m in models_used)
    src_list = ""
    if norm_sources:
        src_list = "\n\n## Sources\n" + "\n".join(
            f"[{s['n']}] {s['title']}" + (f" — {s['url']}" if s["url"] else "") for s in norm_sources)
    critique_block = f"\n\n## Independent Review (cross-model)\n{critique}" if critique else ""
    footer = (f"\n\n---\n*Multi-model research — {len(angles)} angles analysed across {method}; "
              f"synthesis by primary model; independent cross-model review by secondary model.*")

    full = _redact_output(report + src_list + critique_block + footer)[:24000]

    build_fmt = (args.get("build") or "").lower()
    if build_fmt in {"docx", "pdf", "pptx"}:
        full += (f"\n\n(To deliver this as a {build_fmt.upper()} file: call get_document_skill('{build_fmt}') "
                 f"then build_document with this report as the content.)")
    return _text_result(full)

def _remember(user_id: str, args: dict) -> dict:
    """Persist a durable fact to the user's Cowork memory (agent-driven memory
    auto-update). Compliance-guarded so secrets/PII never land in the prompt
    store — fail-closed if compliance can't run."""
    note = (args.get("note") or "").strip()
    if not note:
        return _text_result("Nothing to remember — provide a short `note`.", is_error=True)
    blk = _block_input({"note": note})
    if blk:
        return _text_result(f"[BLOCKED] Not remembered (sensitive content): {blk}", is_error=True)
    try:
        from memory.cowork_memory import add_note
        add_note(str(user_id), note)
        return _text_result(f"Remembered: \"{note}\". I'll keep this in mind in future tasks.")
    except Exception as e:
        return _text_result(f"Could not save to memory: {e}", is_error=True)

# Bounded wait for a sandbox result handed back over Redis by the exec worker.
_EXEC_WAIT_SECONDS = 75

def _validate_code_arg(args: dict):
    code = args.get("code") or ""
    if not code.strip():
        return None, None, _text_result(
            "Nothing to run — provide a `code` script that prints its result.", is_error=True)
    return code, (args.get("language") or "python").lower(), None

def _run_code_enqueue(code: str, language: str, files: dict = None):
    """Enqueue a sandbox job on exec_queue. Returns (job_id, error_result).
    error_result is a tool-result dict on back-pressure/unavailable, else None.
    `files` (optional): {name: content} data files bound into the sandbox (ADA)."""
    try:
        from core.job_queue import Q_EXEC, enqueue_job, _rq_available
    except Exception:
        return None, None  # signal: no RQ → inline (dev)
    if not _rq_available:
        return None, None
    import uuid as _uuid
    job_id = str(_uuid.uuid4())
    try:
        payload = {"job_id": job_id, "code": code, "language": language}
        if files:
            payload["files"] = files
        enqueue_job(
            "workers.exec_worker.run_code_job",
            payload,
            queue_name=Q_EXEC, timeout=90, retry_count=0,
        )
    except RuntimeError:
        return None, _text_result(
            "The analysis sandbox is busy right now — please ask me to run that again in a moment.",
            is_error=True)
    return job_id, None

def _finalize_exec(res: dict, language: str) -> dict:
    if res.get("image_missing"):
        return _text_result(
            f"The sandbox image for '{language}' isn't installed on the server; try python, or ask an admin "
            f"to pre-pull it.", is_error=True)
    output = (res.get("output") or "").strip()
    if res.get("success"):
        return _text_result(output or "(the script produced no output — remember to print() your result)")
    return _text_result(f"Script exited with an error (exit={res.get('exit_code')}):\n{output}", is_error=True)

def _run_code_inline(language: str, code: str, files: dict = None) -> dict:
    logger.warning("cowork_mcp: RQ unavailable — running run_code INLINE (dev fallback, not for prod scale)")
    try:
        # Docker-ONLY, even in the dev fallback: never the host-FS subprocess executor.
        from sandbox.docker_executor import docker_executor
        if not docker_executor.is_available():
            return {"success": False, "sandbox_unavailable": True, "exit_code": -1, "language": language,
                    "output": "The secure code sandbox (Docker) isn't running, so code can't be executed "
                              "right now — Cowork never runs code outside the isolated sandbox."}
        return docker_executor.execute(code=code, language=language, files=files) or {}
    except Exception as e:
        return {"success": False, "output": f"Sandbox unavailable: {e}", "exit_code": -1, "language": language}

def _run_code(args: dict) -> dict:
    """SYNC run_code (REST/inline path + dev). Enqueues to exec_queue and waits on
    a SYNC Redis BLPOP. The async tool path (`_run_code_async`) is preferred under
    load — it waits without holding a thread. (feedback_scale_2k_users)"""
    code, language, err = _validate_code_arg(args)
    if err:
        return err
    job_id, err = _run_code_enqueue(code, language)
    if err:
        return err
    if job_id is None:
        return _finalize_exec(_run_code_inline(language, code), language)
    try:
        import redis as _redis_lib
        from core.config import REDIS_HOST, REDIS_PORT
        r = _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5,
                             decode_responses=True, socket_connect_timeout=2)
        popped = r.blpop(f"cowork:exec:result:{job_id}", timeout=_EXEC_WAIT_SECONDS)
        if not popped:
            return _text_result("The analysis took too long and was stopped. Try a smaller computation.",
                                is_error=True)
        return _finalize_exec(json.loads(popped[1]), language)
    except Exception as e:
        return _text_result(f"Could not retrieve the sandbox result: {e}", is_error=True)

def _analyze_data(args: dict) -> dict:
    """Data analysis (ADA): bind a dataset file into the isolated sandbox and run
    the agent's analysis script against it. Returns the printed analysis. The data
    is bound as a FILE (read via open()) so the script stays clean and the dataset
    can be sizeable. Python stdlib (csv/json/statistics) — no network, ephemeral."""
    data = args.get("data")
    filename = (args.get("filename") or "data.csv").strip() or "data.csv"
    code = args.get("code") or ""
    if not isinstance(data, str) or not data.strip():
        return _text_result(
            "analyze_data needs `data` — the dataset content as text (CSV/TSV/JSON).", is_error=True)
    if not code.strip():
        return _text_result(
            f"analyze_data needs `code` — a Python script that reads the bound file '{filename}' "
            f"(stdlib: csv/json/statistics/math) and PRINTS the analysis (totals, averages, group-bys, "
            f"trends). Do not inline the data; read it from the file.", is_error=True)
    files = {filename: data[:2_000_000]}  # bind dataset (≈2 MB cap)
    job_id, err = _run_code_enqueue(code, "python", files=files)
    if err:
        return err
    if job_id is None:
        return _finalize_exec(_run_code_inline("python", code, files=files), "python")
    try:
        import redis as _redis_lib
        from core.config import REDIS_HOST, REDIS_PORT
        r = _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5,
                             decode_responses=True, socket_connect_timeout=2)
        popped = r.blpop(f"cowork:exec:result:{job_id}", timeout=_EXEC_WAIT_SECONDS)
        if not popped:
            return _text_result(
                "The analysis took too long and was stopped. Try a smaller dataset or computation.",
                is_error=True)
        return _finalize_exec(json.loads(popped[1]), "python")
    except Exception as e:
        return _text_result(f"Could not retrieve the analysis result: {e}", is_error=True)

async def _run_code_async(args: dict) -> dict:
    """ASYNC run_code — enqueue, then await an ASYNC Redis BLPOP so a long sandbox
    run never ties up an event-loop or thread-pool thread. Used by handle()."""
    code, language, err = _validate_code_arg(args)
    if err:
        return err
    # Enqueue is quick + may touch Redis/DB → run off-loop.
    job_id, err = await asyncio.get_event_loop().run_in_executor(
        _TOOL_POOL, _run_code_enqueue, code, language)
    if err:
        return err
    if job_id is None:
        res = await asyncio.get_event_loop().run_in_executor(_TOOL_POOL, _run_code_inline, language, code)
        return _finalize_exec(res, language)
    try:
        import redis.asyncio as _aioredis
        from core.config import REDIS_HOST, REDIS_PORT
        try:
            from core.config import REDIS_PASSWORD as _RP
        except Exception:
            _RP = None
        r = _aioredis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5, password=_RP or None,
                            decode_responses=True, socket_connect_timeout=2)
        popped = await r.blpop(f"cowork:exec:result:{job_id}", timeout=_EXEC_WAIT_SECONDS)
        if not popped:
            return _text_result("The analysis took too long and was stopped. Try a smaller computation.",
                                is_error=True)
        return _finalize_exec(json.loads(popped[1]), language)
    except Exception as e:
        return _text_result(f"Could not retrieve the sandbox result: {e}", is_error=True)

def _connector_rate_limited(user_id: str, connector: str) -> bool:
    """Per-tenant token bucket via Redis (best-effort). True = over the limit.
    Protects external APIs (M365/Graph) from us hammering them under load."""
    try:
        import redis as _redis_lib
        from core.config import REDIS_HOST, REDIS_PORT
        r = _redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=5,
                             decode_responses=True, socket_connect_timeout=1)
        key = f"cowork:rate:{user_id}:{connector}"
        n = r.incr(key)
        if n == 1:
            r.expire(key, _RATE_WINDOW)
        return n > _RATE_MAX
    except Exception:
        return False  # never block on a rate-limiter outage

def _resolve_single_attachment(job_id: str, user_id: str = ""):
    """Resolve one build_document job id into a Graph fileAttachment dict.

    UUID guard: if job_id is a plain UUID it may be a ChatAttachment id (the agent
    sometimes passes attachment_id value via attachment_job_id by mistake). Try the
    ChatAttachment DB first — if found, return immediately without waiting 30s in Redis.
    """
    job_id = str(job_id or "").strip()
    if not job_id:
        return ("none", None)
    # ── UUID guard ────────────────────────────────────────────────────────────────
    if _UUID_RE.match(job_id):

        att = _resolve_file_path_attachment(f"chat_attachment:{job_id}", user_id=user_id)
        if att:

            return ("ok", att)

    import base64
    import os
    import json as _json
    import time as _time
    try:
        from workers.doc_worker import DOC_DIR, _R
    except Exception as exc:
        logger.warning(f"cowork_mcp: doc store unavailable for attachment: {exc}")
        return ("pending", None)
    meta = None
    for _ in range(30):  # builds are usually < 30s; cap at 30s to avoid holding a thread pool worker for 90s
        try:
            raw = _R.get(f"doc:result:{job_id}")
        except Exception:
            raw = None
        if raw:
            try:
                meta = _json.loads(raw if isinstance(raw, str) else raw.decode())
            except Exception:
                meta = None
            break
        _time.sleep(1)
    if not meta or (meta.get("status") and meta.get("status") != "done"):
        _status_val = (meta or {}).get("status", "no-meta")

        return ("pending", None)  # still building — caller should retry
    file_id = (meta.get("file_id") or "").strip()
    ext = (meta.get("format") or "").strip().lstrip(".")
    fname = (meta.get("filename") or (f"document.{ext}" if ext else "document")).strip()
    if not file_id or not ext:
        return ("pending", None)

    # Resolve the real on-disk path from the GeneratedDocument DB row rather than
    # reconstructing it. Documents are saved under a per-user/per-chat subdirectory
    # (core.config.user_doc_dir → DOC_STORAGE_DIR/{user}/{chat}/{file_id}.{ext}), NOT
    # directly under the flat DOC_DIR root — guessing os.path.join(DOC_DIR, ...) here
    # produced a path that never exists, so this ALWAYS returned "none" for every
    # document built after the per-user/per-chat directory layout was introduced.
    # db.file_path is the same authoritative source routers/doc_download_router.py's
    # download_document() and _resolve_artifact_attachment() already trust.
    path = None
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            query = db.query(GeneratedDocument).filter(GeneratedDocument.id == file_id)
            if user_id:
                query = query.filter(GeneratedDocument.user_id == str(user_id))
            row = query.first()
        finally:
            db.close()
        if row and row.file_path and os.path.exists(row.file_path):
            path = row.file_path
            fname = row.filename or fname
            ext = (row.format or ext or "").lstrip(".")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"cowork_mcp: attachment DB lookup failed {file_id}: {exc}")

    if not path:
        # Fallback: legacy flat layout (pre per-user/per-chat refactor), kept for
        # any document that predates the directory-layout change.
        _legacy_path = os.path.join(DOC_DIR, f"{file_id}.{ext}")
        if os.path.exists(_legacy_path):
            path = _legacy_path

    if not path:
        return ("none", None)  # file missing from disk — not a transient pending state
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception as exc:

        logger.warning(f"cowork_mcp: attachment read failed {path}: {exc}")
        return ("none", None)  # read error — not a transient pending state
    mime = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf":  "application/pdf",
    }.get(ext, "application/octet-stream")
    return ("ok", {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": fname,
        "contentType": mime,
        "contentBytes": base64.b64encode(data).decode("utf-8"),
    })

_ATTACHMENT_MIME_MAP = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xls":  "application/vnd.ms-excel",
    "csv":  "text/csv",
    "pdf":  "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt":  "text/plain",
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
}

def _resolve_file_path_attachment(raw_path: str, user_id: str = ""):
    """Resolve a local file into a Graph fileAttachment dict (base64-encoded bytes).

    Fix #32: The original implementation only searched cwd, so files in the user's
    Downloads/Desktop/Documents were silently rejected. Resolution order:
      1. ChatAttachment DB lookup by filename — covers files the user uploaded in chat
      2. Absolute path — accepted directly if the file exists
      3. Relative/filename-only — searched across cwd, /work, ~/Downloads, ~/Desktop,
         ~/Documents so the agent can attach files without needing a full path.
    """
    if not raw_path:
        return None
    import base64
    import os as _os
    from pathlib import Path

    raw = str(raw_path).strip()
    # Accept raw IDs and UI-style mentions, not just literal paths. The browser Buddy
    # confirm card often only knows the ChatAttachment id for a manually uploaded file.
    m = _re_recip.search(r"/chat/attachments/([A-Za-z0-9_.:-]+)", raw)
    attachment_id = ""
    if m:
        attachment_id = m.group(1).strip()
    elif raw.startswith("chat_attachment:"):
        attachment_id = raw.split(":", 1)[1].strip()
    elif raw.startswith("attachment:"):
        attachment_id = raw.split(":", 1)[1].strip()
    fname_only = Path(raw).name  # just the filename part for DB lookup

    # ── 1. ChatAttachment DB lookup (files uploaded by the user in this session) ──
    # When the user drags/uploads a file into the chat, it is stored in ChatAttachment
    # with storage_path pointing to either a local disk path OR a MinIO object
    # ("minio:<object_name>"). We use storage.load() which handles both backends.
    # Extension/MIME is derived from file_name (the original filename), NOT from
    # storage_path which may be a UUID with no meaningful extension on MinIO.
    try:
        from db.database import SessionLocal as _SL
        from db.models import ChatAttachment as _CA
        from core.storage import storage as _storage
        _db = _SL()
        try:
            if attachment_id:
                q = _db.query(_CA).filter(_CA.id == attachment_id)
                if user_id:
                    q = q.filter(_CA.user_id == str(user_id))
                row = q.order_by(_CA.created_at.desc()).first()

                # Fallback: if user_id filter found nothing, try without it (handles
                # user_id mismatch between upload storage and MCP query — e.g. when
                # chat_router stores under "user_id" key but MCP queries by "sub").
                if not row and user_id:
                    row = _db.query(_CA).filter(_CA.id == attachment_id)\
                             .order_by(_CA.created_at.desc()).first()
                    if row:

                        logger.info(f"cowork_mcp: found attachment {attachment_id!r} via "
                                    f"id-only fallback (user_id mismatch: "
                                    f"stored={row.user_id!r}, queried={user_id!r})")

            else:
                q = _db.query(_CA).filter(_CA.file_name == fname_only)
                if user_id:
                    q = q.filter(_CA.user_id == str(user_id))
                row = q.order_by(_CA.created_at.desc()).first()

                # Fallback: filename-only search when user_id filter finds nothing.
                # Safe because the user is already authenticated; this only widens
                # the search within the same server, not across tenants.
                if not row and user_id:
                    row = _db.query(_CA).filter(_CA.file_name == fname_only)\
                             .order_by(_CA.created_at.desc()).first()
                    if row:

                        logger.info(f"cowork_mcp: found {fname_only!r} via filename-only "
                                    f"fallback (user_id mismatch: stored={row.user_id!r}, "
                                    f"queried={user_id!r})")

            if row and row.storage_path:
                data = _storage.load(row.storage_path)
                if data:
                    # Use original file_name for extension — storage_path may be a UUID
                    ext = Path(row.file_name).suffix.lower().lstrip(".")
                    mime = _ATTACHMENT_MIME_MAP.get(ext, "application/octet-stream")

                    logger.info(f"cowork_mcp: resolved chat-uploaded attachment {fname_only} "
                                f"({len(data)} bytes) via storage backend")
                    return {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "name": row.file_name,
                        "contentType": mime,
                        "contentBytes": base64.b64encode(data).decode("utf-8"),
                    }
                else:

                    logger.warning(f"cowork_mcp: storage.load returned None for {row.storage_path!r}")
        finally:
            _db.close()
    except Exception as exc:

        logger.warning(f"cowork_mcp: ChatAttachment DB lookup failed for {fname_only!r}: {exc}")

    # ── 2 & 3. Filesystem search ──────────────────────────────────────────────────

    candidate = Path(raw).expanduser()
    home = Path.home()
    search_bases = [
        Path.cwd().resolve(),
        Path("/work"),
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]

    path: Path | None = None
    if candidate.is_absolute():
        resolved = candidate.resolve()
        if resolved.is_file():
            path = resolved
    else:
        for base in search_bases:
            try:
                attempt = (base / candidate).resolve()
                if attempt.is_file():
                    path = attempt
                    break
            except Exception:
                continue

    if path is None:
        logger.warning(f"cowork_mcp: attachment file not found: {raw!r}")
        return None

    ext = path.suffix.lower().lstrip(".")
    mime = _ATTACHMENT_MIME_MAP.get(ext)
    if not mime:
        logger.warning(f"cowork_mcp: unsupported attachment type .{ext} for {path.name}")
        return None

    max_bytes = int(_os.getenv("M365_ATTACHMENT_FILE_MAX_BYTES", str(10 * 1024 * 1024)))
    try:
        if path.stat().st_size > max_bytes:
            logger.warning(f"cowork_mcp: attachment too large ({path.stat().st_size} bytes): {path.name}")
            return None
        data = path.read_bytes()
    except Exception as exc:
        logger.warning(f"cowork_mcp: local attachment read failed {path}: {exc}")
        return None

    logger.info(f"cowork_mcp: resolved local attachment {path.name} ({len(data)} bytes)")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": path.name,
        "contentType": mime,
        "contentBytes": base64.b64encode(data).decode("utf-8"),
    }

def _teams_attachment_from(att: dict, user_id: str = "") -> dict:
    """Wrap a resolved Outlook fileAttachment for the Teams send path.

    Teams chat/channel messages cannot embed base64 file bytes directly; the file
    must be hosted (OneDrive/SharePoint) and referenced. We upload the bytes to the
    user's OneDrive (via the M365 connector's onedrive_upload tool) and produce a
    Graph message `attachment` reference plus a link chip so the file is delivered
    rather than silently dropped (#19). If the upload is unavailable, we degrade
    gracefully to a link-only reference so the send does not fail outright.
    """
    name = att.get("name", "document")
    web_url = None
    try:
        from connectors.registry import connector_registry

        resp = connector_registry.execute(
            "microsoft_365", "onedrive_upload",
            {"filename": name, "content_bytes": att.get("contentBytes", ""),
             "content_type": att.get("contentType", "application/octet-stream")},
            user_id, query_text="onedrive_upload",
        )
        # Fix: check resp.items directly rather than relying on resp.success which
        # can silently return False if the ConnectorResponse attribute is named
        # differently or the response shape changed. The items list is the ground truth.
        items = getattr(resp, "items", None) or []

        if isinstance(items, list) and items and isinstance(items[0], dict):
            web_url = items[0].get("webUrl") or items[0].get("web_url")
        if not web_url:

            logger.warning(
                f"cowork_mcp: OneDrive upload for {name!r} returned no webUrl — "
                f"resp.success={getattr(resp, 'success', None)!r}, "
                f"resp.error={getattr(resp, 'error', None)!r}, items={items!r}"
            )

    except Exception as exc:  # noqa: BLE001

        logger.warning(f"cowork_mcp: teams attachment upload failed for {name}: {exc}")
    # Graph requires attachments[].id to be a GUID, and the body must contain
    # <attachment id="{same-guid}"></attachment> — a plain filename or <a href> is rejected.
    import uuid as _uuid_mod
    att_guid = str(_uuid_mod.uuid4())
    ref = {
        "id": att_guid,
        "contentType": "reference",
        "name": name,
    }
    if web_url:
        ref["contentUrl"] = web_url
    # Docs require: <attachment id="{guid}"></attachment> in the HTML body — NOT a hyperlink.
    # The guid here must exactly match ref["id"] above.
    if web_url:
        link_html = f'<attachment id="{att_guid}"></attachment>'
    else:
        link_html = f'<p>Attachment prepared: {name} (upload unavailable — please share manually)</p>'
    return {
        "_teams_attachment": ref,
        "_teams_link_html": link_html,
        "name": name,
        # G15: signal whether the file actually reached OneDrive. When false, the
        # caller must NOT claim the file was attached — the message goes without it.
        "_upload_ok": bool(web_url),
    }

def _resolve_doc_attachments(arguments: dict, user_id: str = ""):
    """Resolve one OR MANY built documents into Graph fileAttachments (Fix #13/#18).

    Accepts `attachment_job_id` (single) and/or `attachment_job_ids` (list, or a
    comma/;-separated string, or a folder of DOCJOB markers). Pops the attachment
    keys off `arguments`. Returns ("none"|"pending"|"ok", list_of_attachments).
    """
    import re as _re
    ids: list[str] = []
    single = arguments.pop("attachment_job_id", None)
    attachment_id_single = arguments.pop("attachment_id", None)
    attachment_id_multi = arguments.pop("attachment_ids", None)
    # G18: attach an EXISTING document by its artifact_id (a doc built earlier, not a
    # fresh build job) — e.g. "email the deck you made earlier". Resolved from the
    # GeneratedDocument store rather than the async job result.
    artifact = arguments.pop("attachment_artifact_id", None)
    multi = arguments.pop("attachment_job_ids", None)
    file_single = arguments.pop("attachment_file_path", None)
    file_multi = arguments.pop("attachment_file_paths", None)
    if single:
        ids.append(str(single))
    if isinstance(multi, (list, tuple)):
        ids.extend(str(m) for m in multi)
    elif isinstance(multi, str) and multi.strip():
        ids.extend(p for p in _re.split(r"[;,\s]+", multi.strip()) if p)
    file_paths: list[str] = []
    if file_single:
        file_paths.append(str(file_single))
    if attachment_id_single:
        file_paths.append(f"chat_attachment:{attachment_id_single}")
    if isinstance(file_multi, (list, tuple)):
        file_paths.extend(str(p) for p in file_multi)
    elif isinstance(file_multi, str) and file_multi.strip():
        file_paths.extend(p for p in _re.split(r"[;,\n]+", file_multi.strip()) if p.strip())
    if isinstance(attachment_id_multi, (list, tuple)):
        file_paths.extend(f"chat_attachment:{p}" for p in attachment_id_multi if str(p).strip())
    elif isinstance(attachment_id_multi, str) and attachment_id_multi.strip():
        file_paths.extend(f"chat_attachment:{p}" for p in _re.split(r"[;,\n\s]+", attachment_id_multi.strip()) if p.strip())
    # Normalise any pasted DOCJOB markers to bare ids.
    norm: list[str] = []
    for j in ids:
        j = str(j).strip()
        if "DOCJOB" in j:
            try:
                j = j.split("DOCJOB:", 1)[1].split(":", 1)[0].strip().rstrip("]")
            except Exception:
                pass
        if j and j not in norm:
            norm.append(j)

    resolved: list[dict] = []
    for j in norm:
        status, att = _resolve_single_attachment(j, user_id=user_id)
        if status == "pending":
            return ("pending", [])
        if status == "ok" and att:
            resolved.append(att)
    # Resolve any existing artifact(s) by id.
    for aid in ([artifact] if isinstance(artifact, str) and artifact.strip() else
                (artifact if isinstance(artifact, (list, tuple)) else [])):
        att = _resolve_artifact_attachment(str(aid).strip(), user_id=user_id)
        if att:
            resolved.append(att)
    for fp in file_paths:
        att = _resolve_file_path_attachment(fp.strip(), user_id=user_id)
        if att:
            resolved.append(att)
    if not norm and not artifact and not file_paths and not attachment_id_single and not attachment_id_multi:
        return ("none", [])
    return ("ok" if resolved else "none", resolved)

def _resolve_artifact_attachment(artifact_id: str, user_id: str = ""):
    """Resolve an EXISTING built document (by artifact_id / file_id) into a Graph
    fileAttachment (G18). Reads the latest version's file from disk and base64s it.
    Returns the attachment dict or None."""
    if not artifact_id:
        return None
    import base64
    import os as _os
    try:
        from db.database import SessionLocal
        from db.models import GeneratedDocument
        db = SessionLocal()
        try:
            query = db.query(GeneratedDocument).filter(
                (GeneratedDocument.artifact_id == artifact_id) |
                (GeneratedDocument.id == artifact_id)
            )
            if user_id:
                query = query.filter(GeneratedDocument.user_id == str(user_id))
            row = (query.order_by(GeneratedDocument.version.desc(),
                                  GeneratedDocument.created_at.desc())
                   .first())
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"cowork_mcp: artifact attachment lookup failed {artifact_id}: {exc}")
        return None
    if not row or not row.file_path or not _os.path.exists(row.file_path):
        return None
    try:
        with open(row.file_path, "rb") as fh:
            data = fh.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"cowork_mcp: artifact read failed {row.file_path}: {exc}")
        return None
    ext = (row.format or "").lstrip(".") or _os.path.splitext(row.file_path)[1].lstrip(".")
    mime = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pdf":  "application/pdf",
    }.get(ext, "application/octet-stream")
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": row.filename or f"document.{ext}",
        "contentType": mime,
        "contentBytes": base64.b64encode(data).decode("utf-8"),
    }

def _resolve_teams_user_oid(user_id: str, query: str) -> dict:
    """Resolve a Teams 1:1 target to exactly one Azure AD user, or report ambiguity."""
    def _candidate(item: dict) -> dict:
        return {
            "id": item.get("id", ""),
            "display_name": item.get("displayName", ""),
            "email": item.get("mail") or item.get("userPrincipalName", ""),
            "job_title": item.get("jobTitle", ""),
            "department": item.get("department", ""),
        }

    def _safe_filter_value(value: str) -> str:
        return value.replace("'", "''")

    def _safe_search_value(value: str) -> str:
        return value.replace('"', " ").strip()

    def _result(status: str, **kwargs) -> dict:
        return {"status": status, **kwargs}

    q = str(query or "").strip()[:120]
    if not q:
        return _result("not_found", message="No Teams recipient was provided.")

    try:
        from connectors.net_relay import relay_request
        from connectors.engine import connector_engine as _ce
        from urllib.parse import quote as _q
        from connectors.adapters.microsoft365 import GRAPH_BASE

        defn = _ce._load_definition("microsoft_365")
        token_row = _ce._get_token_row(user_id, "microsoft_365", defn)
        ctx = _ce._get_context(user_id, "microsoft_365", token_row, defn)

        headers = {
            "Authorization": f"Bearer {ctx.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        search_headers = {**headers, "ConsistencyLevel": "eventual"}
        select = "id,displayName,jobTitle,department,mail,userPrincipalName"
        looks_like_email = bool(_EMAIL_RE.fullmatch(q))

        if looks_like_email:
            r = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users/{_q(q)}",
                headers=headers,
                params={"$select": select},
                timeout=10,
            )
            if r.status_code == 200:
                item = r.json()
                oid = item.get("id", "")
                if oid:
                    logger.info(f"teams_start_chat: direct resolve {q!r} → OID {oid!r}")
                    return _result("resolved", oid=oid, candidate=_candidate(item))
            elif r.status_code not in (400, 404):
                r.raise_for_status()

            escaped = _safe_filter_value(q)
            r2 = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=search_headers,
                params={
                    "$filter": f"mail eq '{escaped}' or userPrincipalName eq '{escaped}'",
                    "$select": select,
                    "$count": "true",
                    "$top": "5",
                },
                timeout=10,
            )
            if r2.status_code == 200:
                vals = r2.json().get("value", [])
                exact = [v for v in vals if v.get("id")]
                if len(exact) == 1:
                    oid = exact[0]["id"]
                    logger.info(f"teams_start_chat: exact resolve {q!r} → OID {oid!r}")
                    return _result("resolved", oid=oid, candidate=_candidate(exact[0]))
                if len(exact) > 1:
                    return _result("ambiguous", candidates=[_candidate(v) for v in exact[:5]])
            elif r2.status_code not in (400, 404):
                r2.raise_for_status()

        search_value = _safe_search_value(q)
        search = relay_request(
            "GET",
            f"{GRAPH_BASE}/v1.0/users",
            headers=search_headers,
            params={
                "$search": f'"displayName:{search_value}" OR "mail:{search_value}" OR "userPrincipalName:{search_value}"',
                "$select": select,
                "$count": "true",
                "$top": "5",
            },
            timeout=10,
        )
        if search.status_code == 200:
            vals = [v for v in search.json().get("value", []) if v.get("id")]
        else:
            vals = []
            if search.status_code not in (400, 404):
                search.raise_for_status()

        if not vals:
            escaped = _safe_filter_value(q)
            fallback = relay_request(
                "GET",
                f"{GRAPH_BASE}/v1.0/users",
                headers=search_headers,
                params={
                    "$filter": (
                        f"startswith(displayName,'{escaped}') or "
                        f"startswith(mail,'{escaped}') or "
                        f"startswith(userPrincipalName,'{escaped}')"
                    ),
                    "$select": select,
                    "$count": "true",
                    "$top": "5",
                },
                timeout=10,
            )
            if fallback.status_code == 200:
                vals = [v for v in fallback.json().get("value", []) if v.get("id")]
            elif fallback.status_code not in (400, 404):
                fallback.raise_for_status()

        candidates = [_candidate(v) for v in vals[:5]]
        ql = q.lower()
        exact_matches = [
            c for c in candidates
            if c.get("email", "").lower() == ql or c.get("display_name", "").lower() == ql
        ]
        if len(exact_matches) == 1:
            return _result("resolved", oid=exact_matches[0]["id"], candidate=exact_matches[0])
        if candidates:
            return _result("ambiguous", candidates=candidates)
        return _result("not_found", message=f"No Microsoft 365 user matched {q!r}.")
    except Exception as exc:
        logger.warning(f"teams_start_chat: OID resolution failed for {q!r}: {exc}")
        return _result("not_found", message=f"Microsoft 365 user lookup failed for {q!r}.")

def _format_people_candidates(candidates: list[dict]) -> str:
    lines = []
    for idx, c in enumerate((candidates or [])[:5], start=1):
        bits = [c.get("display_name") or "(no name)", c.get("email") or "(no email)"]
        extra = ", ".join(v for v in (c.get("job_title"), c.get("department")) if v)
        if extra:
            bits.append(extra)
        lines.append(f"{idx}. " + " — ".join(bits))
    return "\n".join(lines)

def _connector_call(user_id: str, connector: str, tool: str, full_name: str, arguments: dict) -> dict:
    is_write = full_name in _write_tool_names(user_id)

    # Fix #30: For teams_start_chat, resolve the target email to an Azure AD OID before
    # building the Graph request body. Graph's POST /chats requires user@odata.bind to
    # use /users/{oid} (slash path), not /users('{email}') (OData function syntax).
    # We inject the resolved OID as user_id so the adapter prefers it over user_email.
    if tool == "teams_start_chat" and isinstance(arguments, dict):
        _target_email = str(arguments.get("user_email", "")).strip()
        # Guard: if the AI passed a display name instead of an email (no '@'),
        # block and ask it to resolve via people_search first.
        if _target_email and "@" not in _target_email and not arguments.get("user_id"):
            return _text_result(
                f"I can't start a Teams chat with {_target_email!r} — that looks like a display name, "
                "not an email address. Please use people_search to find the exact person first, "
                "then call teams_start_chat with their email address.",
                is_error=True,
            )
        if _target_email and not arguments.get("user_id"):
            _resolved = _resolve_teams_user_oid(user_id, _target_email)
            if _resolved.get("status") == "resolved" and _resolved.get("oid"):
                # Best case: resolved to an OID — use it for an unambiguous bind
                _oid = _resolved["oid"]
                arguments["user_id"] = _oid
                logger.info(f"teams_start_chat: injected OID {_oid!r} for {mask_email(_target_email)!r}")
            elif _resolved.get("status") == "ambiguous":
                # Multiple matches — must ask user to pick before proceeding
                return _text_result(
                    "I found multiple Microsoft 365 users matching that Teams recipient. "
                    "Ask the user which exact person to use, then retry with the selected email:\n"
                    f"{_format_people_candidates(_resolved.get('candidates') or [])}",
                    is_error=True,
                )
            else:
                # OID resolution failed (user not found in directory, or lookup error).
                # Do NOT block — fall through and let Graph try with the email directly.
                # POST /chats with users/{email} works when email == UPN on most tenants.
                # The 400 handler in the adapter will surface a clear error if Graph rejects it.
                logger.warning(
                    f"teams_start_chat: OID resolution returned {_resolved.get('status')!r} "
                    f"for {_target_email!r} — proceeding with email as UPN fallback"
                )

    # Attach a built document to an outgoing email. Resolve BEFORE the compliance
    # text scan (the attachment is binary, not free-text to screen). The agent
    # passes attachment_job_id from the [DOCJOB:...] marker; we wait for the async
    # build, then inject a Graph fileAttachment the adapter turns into message.attachments.
    # Capture whether the caller supplied ANY attachment param BEFORE _resolve_doc_attachments
    # pops them off `arguments`. Used below to detect the silent-drop case where the user
    # asked to attach a file but none could be resolved (file not on server / not uploaded).
    _ATTACHMENT_PARAM_KEYS = (
        "attachment_job_id", "attachment_job_ids", "attachment_artifact_id",
        "attachment_file_path", "attachment_file_paths", "attachment_id", "attachment_ids",
    )
    _had_attachment_params = (
        tool in ("outlook_send_mail", "teams_send_message", "teams_send_chat_message")
        and isinstance(arguments, dict)
        and any(arguments.get(k) for k in _ATTACHMENT_PARAM_KEYS)
    )
    if _had_attachment_params:
        _att_status, _atts = _resolve_doc_attachments(arguments, user_id=user_id)

        if _att_status == "pending":
            # Track retry count so the agent knows when to give up instead of looping forever.
            _retry_count = int((arguments or {}).pop("_attachment_retry", 0)) + 1

            if _retry_count >= 3:
                return _text_result(
                    f"The document generation timed out after {_retry_count} retries — the file "
                    "may have failed to build. Please try generating the document again with "
                    "build_document, then retry the send once you see the download button appear.",
                    is_error=True)
            return _text_result(
                f"The document(s) are still being generated (attempt {_retry_count}/3). "
                f"Wait ~15 seconds, then call {tool} again with the SAME attachment id(s) "
                f"and _attachment_retry={_retry_count} to send with the file(s) attached. "
                "If it is still pending after 3 attempts, the build has failed.",
                is_error=True)
        if _att_status == "none":
            # Fix: the caller asked to attach a file but nothing could be resolved.
            # This happens when the file only exists on the user's local machine (not
            # uploaded to the server) or the ChatAttachment / job id is wrong.
            # Do NOT silently send without the attachment — fail loudly so the agent
            # can tell the user to upload the file via the chat file-upload UI first.
            return _text_result(
                f"I couldn't attach the file(s) to the {tool.replace('_', ' ')} — "
                "none of the provided attachment references could be resolved on the server. "
                "This usually means the file only exists on your local machine and hasn't "
                "been uploaded yet. To fix this: drag-and-drop or use the 📎 button to "
                "upload the file into this chat first, then retry the send — I'll attach "
                "the uploaded file automatically. The message was NOT sent.",
                is_error=True)
        if _att_status == "ok" and _atts:
            if tool == "outlook_send_mail":
                arguments["_attachments"] = _atts
            else:
                # Teams cannot inline base64 file bytes the way Outlook can; surface
                # each file as a reference attachment + link so it is not dropped (#19).
                _teams_atts = [_teams_attachment_from(a, user_id) for a in _atts]
                arguments["_attachments"] = _teams_atts
                # G15 (revised): if OneDrive upload failed, fall back to sending the
                # file as a base64 fileAttachment directly in the Teams message.
                # Graph supports fileAttachment in Teams for files < 4 MB. For larger
                # files we still block and tell the user to fix OneDrive permissions.
                _failed_atts = [a for a in _teams_atts if not a.get("_upload_ok")]
                _failed = [a["name"] for a in _failed_atts]
                if _failed:
                    # Find the original resolved attachments for the failed files
                    _failed_names = set(_failed)
                    _fallback_atts = [a for a in _atts if a.get("name") in _failed_names]
                    _large = [a["name"] for a in _fallback_atts
                              if len(a.get("contentBytes", "")) * 3 // 4 > 4 * 1024 * 1024]
                    if _large:
                        # File too large for inline Teams attachment — block and explain

                        return _text_result(
                            "I couldn't attach the file(s) to Teams — the OneDrive upload "
                            f"failed for: {', '.join(_large)} and the file(s) are too large "
                            "for inline attachment (>4 MB). This usually means the "
                            "Files.ReadWrite permission isn't granted. Reconnect Microsoft 365 "
                            "and retry. The message was NOT sent.",
                            is_error=True)
                    # Small files: inject as base64 fileAttachment directly

                    # Replace failed _teams_atts entries with base64 fileAttachment dicts
                    _final_atts = []
                    for ta in _teams_atts:
                        if ta.get("_upload_ok"):
                            _final_atts.append(ta)
                        else:
                            # Find the original resolved att for this file
                            _orig = next((a for a in _atts if a.get("name") == ta.get("name")), None)
                            if _orig:
                                _final_atts.append(_orig)  # raw base64 fileAttachment
                    arguments["_attachments"] = _final_atts

    # WRITE actions reach this code path only AFTER the desktop's can_use_tool
    # permission gate — i.e. the user has explicitly approved this exact send
    # (see desktop/src/cowork/coworkSession.js confirm flow). We then apply the
    # SAME compliance gate as POST /connectors/action: outgoing free-text is
    # HARD-BLOCKED (never redacted) on PAN/PII, because an outbound email/Teams
    # message must not leak sensitive data. This is the gated send path, not a
    # bypass — no write hits the upstream API without (a) the human confirm and
    # (b) this compliance block.
    if is_write:
        text_blob = " ".join(
            str(v) for k, v in (arguments or {}).items()
            if k in ("body", "message", "subject", "content", "text")
        )
        if text_blob.strip():
            try:
                from agents.compliance_engine import compliance_engine
                chk = compliance_engine.validate_input(text_blob)
                # Outbound HARD-BLOCK set: financial / secret data must never leave
                # the org via an email/message. We block on these finding TYPES
                # regardless of the global redact-vs-block config (which is redact-
                # only for chat UX) — an outbound send is a different threat model.
                # EMAIL/MOBILE/UPI are intentionally NOT here: recipient addresses
                # and phone numbers are legitimate, expected content in mail.
                hits = {f.get("type") for f in chk.get("findings", [])} & _OUTBOUND_BLOCK_TYPES
                if hits:
                    logger.warning(f"cowork_mcp: BLOCKED write {full_name} → {sorted(hits)}")
                    return _text_result(
                        f"This message was NOT sent — compliance policy blocks outbound content "
                        f"containing {', '.join(sorted(hits))}. Remove the sensitive data and try again.",
                        is_error=True,
                    )
            except Exception as _ce:
                logger.warning(f"cowork_mcp: write compliance check failed → {_ce}")

    # ── Recipient-verification guard (Fix #20 + G4) ──────────────────────────────
    # The model sometimes resolves a partial/hallucinated recipient from prior
    # conversation and fires a send at the WRONG person. Guard ALL outbound sends —
    # Teams (chat_id/channel_id must be a real id, not a free-text name) AND email/
    # calendar (every to/cc/bcc/attendee must be a syntactically valid address, not a
    # bare name like "the finance team"). Refuse with guidance so the agent resolves
    # + confirms first, rather than mis-sending.
    if isinstance(arguments, dict):
        if tool in ("teams_send_chat_message", "teams_send_message"):
            cid = str(arguments.get("chat_id") or arguments.get("channel_id") or "").strip()
            # A real Teams chat/channel id looks like "19:xxx@unq.gbl.spaces" or a UUID.
            # An email address (user@domain.com) has NO colon before the @.
            # Teams IDs always start with "19:" so they always have a colon before @.
            import re as _re_cid
            _is_plain_email = bool(_re_cid.match(r'^[^@:\s]+@[^@\s]+\.[^@\s]+$', cid))
            looks_like_id = bool(cid) and not _is_plain_email and (
                ":" in cid or len(cid) > 30)
            if not looks_like_id:
                return _text_result(
                    "I can't send this yet — the Teams recipient isn't a verified chat/channel id "
                    f"(got {cid!r}). First resolve the exact person/group with people_search or "
                    "teams_list_chats (name_contains=...), confirm the recipient with the user, then "
                    "send using the returned chat_id. This prevents messaging the wrong person.",
                    is_error=True)
        elif tool in ("outlook_send_mail", "outlook_forward_email",
                      "calendar_create_event", "calendar_forward_event"):
            _rcpt_fields = ("to", "cc", "bcc", "attendees", "optional_attendees")
            _bad = _invalid_recipients(arguments, _rcpt_fields)
            if _bad:
                return _text_result(
                    "I can't send this yet — these recipients aren't valid email addresses "
                    f"(got {_bad!r}). Resolve each person's exact address with people_search, "
                    "confirm with the user, then send using real addresses. This prevents "
                    "sending to the wrong person.",
                    is_error=True)

    # Per-tenant backoff so we don't get throttled/blocked by the upstream API.
    if _connector_rate_limited(user_id, connector):
        return _text_result(
            f"You've made a lot of {connector} requests very quickly — give it a moment and try again.",
            is_error=True)
    try:
        from connectors.registry import connector_registry
        resp = connector_registry.execute(
            connector, tool, arguments, user_id,
            query_text=json.dumps(arguments, default=str)[:200],
        )
        if getattr(resp, "success", False):
            if is_write:
                # teams_start_chat is a write action but it returns the chat id needed
                # for the actual send; do not replace that with a generic success line.
                if tool == "teams_start_chat":
                    try:
                        text = resp.to_context_str()
                    except Exception:
                        text = json.dumps(
                            {"count": getattr(resp, "count", 0), "items": getattr(resp, "items", [])},
                            default=str,
                        )
                    return _text_result(_redact_output(text))
                # Honest confirmation — emitted ONLY when the upstream API actually
                # accepted the action (e.g. Graph 202). Never claim "sent" otherwise.
                return _text_result(f"Done — the {tool.replace('_', ' ')} action completed successfully.")

            # ── people_search disambiguation guard ────────────────────────────────
            # If the search returned multiple people whose display names are similar
            # (e.g. two "Anshuman" results), surface ALL of them and ask the user to
            # confirm which one they meant BEFORE the agent proceeds to send/start a
            # chat. Without this guard the AI silently picks the first result and
            # messages the wrong person.
            if tool == "people_search":
                items = getattr(resp, "items", []) or []
                sq = str(arguments.get("search_query", "")).strip().lower()
                if sq and len(items) > 1:
                    # Check if the query matches multiple distinct people
                    # (same first name / partial name hit multiple records)
                    name_matches = [
                        p for p in items
                        if sq in (p.get("display_name") or "").lower()
                        or sq in (p.get("email") or "").lower()
                    ]
                    if len(name_matches) > 1:
                        lines = [
                            f"  {i+1}. **{p.get('display_name', 'Unknown')}** "
                            f"— {p.get('job_title') or p.get('department') or 'N/A'} "
                            f"({p.get('email', 'no email')})"
                            for i, p in enumerate(name_matches)
                        ]
                        return _text_result(
                            f"I found {len(name_matches)} people matching '{arguments.get('search_query', sq)}':\n\n"
                            + "\n".join(lines)
                            + "\n\nWhich one did you mean? Please confirm the name or share their email "
                            "so I send the message to the right person.",
                            is_error=False,
                        )

            try:
                text = resp.to_context_str()
            except Exception:
                text = json.dumps(
                    {"count": getattr(resp, "count", 0), "items": getattr(resp, "items", [])},
                    default=str,
                )
            return _text_result(_redact_output(text))
        err = getattr(resp, "error", "connector call failed")
        # Surface the real failure so the agent NEVER reports a false success.
        return _text_result(
            f"The {connector} {'send' if is_write else 'request'} did not complete — {err}",
            is_error=True,
        )
    except Exception as e:
        return _text_result(f"Connector exception: {e}", is_error=True)

# ── JSON-RPC 2.0 dispatch (user-scoped) ──────────────────────────────────────
def _ok(id_, result):
    return {"jsonrpc": "2.0", "id": id_, "result": result}

def _err(id_, code, message, data=None):
    e = {"code": code, "message": message}
    if data:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": e}

async def handle(body: dict, user_id: str, allowed=None):
    """Process one JSON-RPC message for this user. `allowed` scopes connector tools
    to the selected role/plugin. Returns a response dict or None."""
    if not isinstance(body, dict):
        return _err(None, -32700, "Parse error")
    if body.get("jsonrpc") != "2.0":
        return _err(body.get("id"), -32600, "Invalid Request: jsonrpc must be '2.0'")

    method = body.get("method", "")
    id_ = body.get("id")
    params = body.get("params") or {}

    try:
        if method == "initialize":
            return _ok(id_, {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}, "resources": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method in ("initialized", "notifications/initialized"):
            return None
        if method == "ping":
            return _ok(id_, {})
        if method == "tools/list":
            # Registry lookup can hit the DB/external metadata → off the event loop.
            loop = asyncio.get_event_loop()
            tools = await loop.run_in_executor(_TOOL_POOL, list_tools, user_id, allowed)
            return _ok(id_, {"tools": tools})
        if method == "tools/call":
            name = params.get("name", "")
            call_args = params.get("arguments") or {}
            if name == _CODE_TOOL:
                # Long sandbox wait handled with async Redis — never blocks a thread.
                result = await _run_code_async(call_args)
            else:
                # Connector/doc/memory work is blocking I/O → dedicated thread pool,
                # so 2k concurrent tool calls never pin the event loop.
                loop = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    _TOOL_POOL, call_tool, user_id, name, call_args, allowed)
            return _ok(id_, result)
        if method.startswith("notifications/"):
            return None
        if id_ is None:
            return None
        return _err(id_, -32601, f"Method not found: {method}")
    except Exception as e:
        logger.error(f"cowork_mcp: dispatch error in {method} → {e}")
        return _err(id_, -32603, "Internal error", str(e))
