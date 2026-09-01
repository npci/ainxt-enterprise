# SPDX-License-Identifier: Apache-2.0
"""
Canonical tools and skills seeded into postgres on startup.

To add tools: paste your implementations into the relevant file under
app/tools/ then restart the backend. seed_canonical_tools() runs
automatically and upserts every entry into tools_catalog.

Draft tools
-----------
Tools marked with `"draft": True` in their spec are NOT seeded on startup.
They live in the source file as ready-to-use implementations but are skipped
by seed_canonical_tools() until the integration is configured and the flag
is removed. To activate a draft tool: remove `"draft": True` from its dict
and restart the backend.

Service files
-------------
app/tools/jira_tools.py        — (all active)
    jira_create_issue, jira_list_issues, jira_get_issue,
    jira_update_issue, jira_add_comment, jira_link_issues,
    jira_recent_changes,
    jira_search_issues, jira_count_issues, jira_create_subtask,
    jira_get_transitions, jira_list_comments, jira_update_comment,
    jira_list_attachments,
    jira_list_watchers, jira_add_watcher,
    jira_remove_watcher, jira_list_link_types,
    jira_list_projects, jira_get_project, jira_get_current_user,
    jira_search_users, jira_list_issue_types

app/tools/gitlab_tools.py      — (all active)
    Repository / files:
        gitlab_read_file, gitlab_search_code,
        gitlab_list_tree, gitlab_list_commits, gitlab_compare_branches,
        gitlab_get_commit_diff, gitlab_create_or_update_file, gitlab_apply_patch
    Branches / tags / releases:
        gitlab_create_branch,
        gitlab_list_tags, gitlab_create_tag, gitlab_list_releases,
        gitlab_create_release
    Issues:
        gitlab_list_issues, gitlab_create_issue, gitlab_get_issue,
        gitlab_update_issue, gitlab_close_issue, gitlab_list_issue_notes,
        gitlab_add_issue_note
    Merge requests:
        gitlab_list_mrs, gitlab_create_mr, gitlab_get_mr, gitlab_merge_mr,
        gitlab_link_mr_to_jira,
        gitlab_get_mr_review_comments, gitlab_get_mr_reviews,
        gitlab_reply_to_review_comment, gitlab_get_mr_files,
        gitlab_create_mr_review,
        gitlab_approve_merge_request
    Pipelines / jobs:
        gitlab_list_pipelines, gitlab_get_pipeline, gitlab_trigger_pipeline,
        gitlab_cancel_pipeline, gitlab_retry_pipeline,
        gitlab_list_pipeline_variables, gitlab_list_jobs,
        gitlab_get_pipeline_jobs, gitlab_get_job_log,
        gitlab_retry_job
    Projects / groups / users:
        gitlab_get_project, gitlab_create_project, gitlab_search_projects,
        gitlab_list_groups, gitlab_search,
        gitlab_get_current_user, gitlab_get_user

app/tools/platform_tools.py    — active: code_executor, read_skill_file
                                  draft:  web_search, file_search, execute_code, llm_generate

app/tools/document_tools.py    — active: read_document
    Extract text (with OCR fallback) from PDFs, images, or Office documents
    referenced by absolute file path or http(s) URL. Wraps the same
    RapidOCR + pypdf/pypdfium2 pipeline used at the /agent-runner/attachment
    boundary. Use inside workflows where a document arrives after upload
    (Connector download, code_executor artefact, external fetch).

app/tools/confluence_tools.py  — draft: confluence_create_page, confluence_update_page,
                                         confluence_get_page, confluence_search,
                                         confluence_get_page_by_title

app/tools/memory_tools.py      — draft: memory_save, memory_get,
                                         memory_remember, memory_recall

app/tools/zoho_tools.py        — draft: zoho_apply_leave, zoho_lookup, zoho_update

app/tools/n8n_tools.py         — draft: n8n_trigger, n8n_list_workflows, n8n_get_execution

app/tools/m365_tools.py        — (all active) direct connector dispatch — 49 tools
    Outlook: outlook_search_emails, outlook_count_emails, outlook_read_email,
             outlook_reply_email, outlook_reply_all_email, outlook_forward_email,
             outlook_send_mail, outlook_list_folders, outlook_create_folder,
             outlook_move_email, outlook_delete_email, outlook_mark_email,
             outlook_create_draft, outlook_send_draft, outlook_list_attachments
    Calendar: calendar_list_events, calendar_create_event, calendar_update_event,
              calendar_cancel_event, calendar_delete_event, calendar_accept_event,
              calendar_decline_event, calendar_tentative_event, calendar_forward_event,
              calendar_find_meeting_times, calendar_get_schedule
    Teams (channels): teams_list_my_teams, teams_list_channels,
              teams_get_channel_messages, teams_send_message,
              teams_reply_channel_message, teams_list_channel_members,
              teams_list_members, teams_create_channel
    Teams (chats): teams_list_chats, teams_get_chat_messages,
              teams_send_chat_message, teams_start_chat, teams_get_chat_members
    Teams (meetings/presence): teams_list_meetings, teams_create_online_meeting,
              teams_list_transcripts, teams_get_transcript_content,
              teams_get_presence, teams_get_user_presence
    People/Org: people_search, org_direct_reports, org_get_manager
    OneDrive: onedrive_upload
    ToolDispatcher.dispatch() intercepts these in-process (same as Buddy/Cowork)
    and calls connector_registry.execute() directly — no sandbox, no HTTP bridge.
    All Graph logic stays in connectors/adapters/microsoft365.py. Each call
    runs against the requesting user's OWN M365 OAuth connection.

Env vars required per service
------------------------------
Jira        : JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT
GitLab      : GITLAB_URL, GITLAB_TOKEN
Confluence  : CONFLUENCE_URL, CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN, CONFLUENCE_SPACE_KEY
Memory      : REDIS_URL (session), DATABASE_URL (episodic)
Zoho        : ZOHO_PEOPLE_URL, ZOHO_CRM_URL, ZOHO_ACCESS_TOKEN
n8n         : N8N_URL, N8N_API_KEY
Microsoft365: PLATFORM_BASE_URL (used by m365_connection.py to check OAuth
              connection status on /tools-catalog requests — no bridge calls
              during tool execution). Platform host needs AZURE_AD_CLIENT_ID,
              AZURE_AD_TENANT_ID, AZURE_AD_CLIENT_SECRET, LLM_PROXY_URL for
              Graph egress. Requires a per-user Microsoft 365 OAuth connection.
Platform    : LLM_PROXY_URL (for web_search, file_search, execute_code, llm_generate)
(set via the Integrations panel in the UI)
"""

from __future__ import annotations


from typing import Dict, List

from app.tools.jira_tools import JIRA_TOOLS
# SCM tools — dispatch on SCM_PROVIDER (github = default, gitlab = alternative)
import os as _os
_SCM_PROVIDER = _os.getenv("SCM_PROVIDER", "github").lower().strip()
if _SCM_PROVIDER == "github":
    from app.tools.github_tools import GITHUB_TOOLS as _SCM_TOOLS
    _SCM_SERVICE = "github"
else:
    from app.tools.gitlab_tools import GITLAB_TOOLS as _SCM_TOOLS
    _SCM_SERVICE = "gitlab"
from app.tools.confluence_tools import CONFLUENCE_TOOLS
from app.tools.memory_tools import MEMORY_TOOLS
from app.tools.zoho_tools import ZOHO_TOOLS
from app.tools.n8n_tools import N8N_TOOLS
from app.tools.platform_tools import PLATFORM_TOOLS
from app.tools.document_tools import DOCUMENT_TOOLS
from app.tools.m365_tools import M365_TOOLS

from core.logger import logger
# Default `service` tag injected per module when a tool spec doesn't already
# declare one. The UI groups /tools-catalog rows by this field, and rows with
# an empty/"platform" service are hidden from the agent tool picker.
_MODULE_SERVICE_DEFAULTS = [
    (PLATFORM_TOOLS,      "platform"),
    (DOCUMENT_TOOLS,      "platform"),
    (JIRA_TOOLS,          "jira"),
    (_SCM_TOOLS,          _SCM_SERVICE),   # github (default) or gitlab based on SCM_PROVIDER
    (CONFLUENCE_TOOLS,    "confluence"),
    (MEMORY_TOOLS,        "memory"),
    (ZOHO_TOOLS,          "zoho"),
    (N8N_TOOLS,           "n8n"),
    # Microsoft 365 (Outlook/Teams/Calendar/People) — direct connector dispatch.
    # ToolDispatcher.dispatch() intercepts service=="microsoft_365" in-process
    # and calls connector_registry.execute() directly (same as Buddy/Cowork).
    # ConnectorEngine owns OAuth/scopes/Graph via connectors/adapters/microsoft365.py.
    # Requires a per-user M365 OAuth connection in ainxt.user_oauth_tokens.
    (M365_TOOLS,          "microsoft_365"),
]


def _with_service(specs: List[Dict], default_service: str) -> List[Dict]:
    """Return tool specs with `service` set, preserving any explicit tag."""
    tagged = []
    for spec in specs:
        if spec.get("service"):
            tagged.append(spec)
        else:
            tagged.append({**spec, "service": default_service})
    return tagged


CANONICAL_TOOLS: List[Dict] = [
    tool
    for module_specs, default_service in _MODULE_SERVICE_DEFAULTS
    for tool in _with_service(module_specs, default_service)
]

CANONICAL_SKILLS: List[Dict] = [
    # add skills here or import from a skills/ folder using the same pattern
]


# ---------------------------------------------------------------------------
# Seeding — do not edit below this line
# ---------------------------------------------------------------------------

async def seed_canonical_tools() -> int:
    """Upsert every non-draft entry in CANONICAL_TOOLS into ``tools_catalog``.

    Tools with ``"draft": True`` in their spec are skipped — they exist in the
    source file but are not seeded until the integration is configured and the
    flag is removed.

    Idempotent: runs on every startup. Returns the number of rows touched.
    """
    from app.core import workflow_repo

    purged = await workflow_repo.purge_deleted_tool_catalog_rows()
    if purged:
        logger.info(f'[AGENT] Purged {purged} deleted MCP tool row(s) from tools_catalog')

    written  = 0
    skipped  = 0
    for spec in CANONICAL_TOOLS:
        if spec.get("draft"):
            skipped += 1
            continue
        try:
            await workflow_repo.upsert_tool(
                name=spec["name"],
                code=spec["code"],
                description=spec["description"],
                input_schema=spec["input_schema"],
                generated=False,
                service=spec.get("service", ""),
            )
            written += 1
        except Exception as exc:
            logger.warning(f"[AGENT] seed_canonical_tools: failed to upsert '{spec['name']}': {exc}")

    if written:
        logger.info(f'[AGENT] Seeded {written} canonical tool(s) into tools_catalog')
    if skipped:
        logger.info(f'[AGENT] Skipped {skipped} draft tool(s) (integration not yet configured)')
    return written


async def seed_canonical_skills() -> int:
    """Upsert every entry in CANONICAL_SKILLS into ``skills_catalog``.

    Idempotent — safe to run on every startup.
    Returns the number of rows touched.
    """
    from app.core import workflow_repo

    written = 0
    for spec in CANONICAL_SKILLS:
        try:
            await workflow_repo.upsert_skill(
                name=spec["name"],
                content=spec["content"],
                description=spec["description"],
                category=spec.get("category", "general"),
                generated=False,
                source="builtin",
            )
            written += 1
        except Exception as exc:
            logger.warning(f"[AGENT] seed_canonical_skills: failed to upsert '{spec['name']}': {exc}")

    if written:
        logger.info(f'[AGENT] Seeded {written} canonical skill(s) into skills_catalog')
    return written
