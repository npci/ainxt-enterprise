# SPDX-License-Identifier: Apache-2.0
# ============================================================
# BRD → FSD Pipeline
# Triggered by Jira Epic with label "BRD"
#
# Pipeline:
#   1. Parse BRD from epic description
#   2. Generate FSD using Claude Sonnet 4.6
#   3. HITL gate (returns pending state if not approved yet)
#   4. On approval: publish FSD to Confluence + attach PDF
#   5. Create Jira stories from FSD sections
#   6. Handoff to Feature pipeline
# ============================================================

import json
import re
import uuid
from datetime import datetime
from typing import Optional

from core.logger import logger, bind_context
from core.config import PLATFORM_NAME as _PLATFORM_NAME

# ── In-memory HITL state store (keyed by epic_key) ───────────
# Stores pending pipeline results waiting for human approval.
# Persisted per-process; survives within the same worker.
_hitl_state: dict = {}


# ============================================================
# BRD PARSER
# Extracts structured sections from the epic description
# ============================================================

_BRD_SECTIONS = [
    "purpose",
    "scope",
    "functional requirements",
    "non-functional requirements",
    "assumptions",
    "out of scope",
]

# Header patterns: "## Purpose", "**Purpose**", "Purpose:", "Purpose\n---"
_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,4}\s*|[*_]{2})?("
    + "|".join(re.escape(s) for s in _BRD_SECTIONS)
    + r")(?:[*_]{2})?\s*:?\s*(?:\n|$)",
    re.IGNORECASE,
)


def _parse_brd(description: str) -> dict:
    """
    Extract structured sections from the BRD epic description.
    Returns a dict with normalised section names as keys.
    Sections not found are returned as empty strings.
    """
    sections: dict = {s: "" for s in _BRD_SECTIONS}

    if not description:
        return sections

    # Find all section header positions
    matches = list(_HEADER_RE.finditer(description))

    if not matches:
        # No recognisable headers — treat the whole text as "purpose"
        sections["purpose"] = description.strip()
        return sections

    for i, m in enumerate(matches):
        section_name = m.group(1).strip().lower()
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(description)
        content = description[start:end].strip()
        if section_name in sections:
            sections[section_name] = content

    return sections


def _format_brd_for_prompt(sections: dict) -> str:
    """Format parsed BRD sections into a clean string for the LLM prompt."""
    lines = []
    for section, content in sections.items():
        if content:
            lines.append(f"## {section.title()}\n{content}")
    return "\n\n".join(lines) if lines else "No BRD content extracted."


# ============================================================
# FSD GENERATOR — calls Claude Sonnet 4.6 via gateway_claude
# ============================================================

def _generate_fsd(epic_key: str, epic_summary: str, brd_sections: dict) -> str:
    """
    Generate a detailed FSD from the parsed BRD sections using Claude Sonnet 4.6.
    Returns the FSD as a markdown string.
    """
    from gateway_claude import claude_gateway
    from core.model_registry import CLAUDE_PRIMARY_MODEL

    brd_text = _format_brd_for_prompt(brd_sections)

    system_prompt = (
        f"You are a senior software architect at AiNxt ({_PLATFORM_NAME}), "
        "an enterprise AI agentic platform. "
        "Your task is to convert a Business Requirements Document (BRD) into a comprehensive "
        "Functional Specification Document (FSD) that engineering teams can implement directly.\n\n"
        "The FSD must include:\n"
        "1. **Document Overview** — purpose, version, date, stakeholders\n"
        "2. **System Context** — how this feature fits into the AiNxt ecosystem\n"
        "3. **Functional Requirements** — detailed, numbered, testable requirements\n"
        "4. **Non-Functional Requirements** — performance, security (PCI/DSS), scalability\n"
        "5. **Data Flow Diagrams** — described in text/ASCII where applicable\n"
        "6. **API Contracts** — endpoint definitions, request/response schemas\n"
        "7. **User Stories** — formatted as:\n"
        "   Story: [title]\n"
        "   As a [role], I want [capability], so that [benefit].\n"
        "   Acceptance Criteria:\n"
        "   - [criterion 1]\n"
        "   - [criterion 2]\n"
        "8. **Error Handling** — failure modes and recovery strategies\n"
        "9. **Security Considerations** — PCI/DSS compliance requirements\n"
        "10. **Open Questions** — items requiring stakeholder clarification\n\n"
        "Be precise, complete, and engineer-ready. Do not omit any section."
    )

    prompt = (
        f"# BRD → FSD Conversion\n\n"
        f"**Epic:** {epic_key} — {epic_summary}\n\n"
        f"## Business Requirements Document\n\n"
        f"{brd_text}\n\n"
        f"---\n\n"
        f"Convert the above BRD into a detailed Functional Specification Document (FSD). "
        f"Follow the structure defined in the system prompt exactly."
    )

    logger.info(f"brd_fsd_pipeline: generating FSD for {epic_key} via Claude")

    fsd_chunks = []
    try:
        for token in claude_gateway.generate(
            prompt=prompt,
            model=CLAUDE_PRIMARY_MODEL,
            temperature=0,
            max_tokens=8000,
            stream=True,
        ):
            fsd_chunks.append(token)
    except Exception as e:
        logger.error(f"brd_fsd_pipeline: FSD generation failed for {epic_key}: {e}")
        return f"[ERROR generating FSD: {e}]"

    fsd = "".join(fsd_chunks).strip()
    logger.info(f"brd_fsd_pipeline: FSD generated for {epic_key} ({len(fsd)} chars)")
    return fsd


# ============================================================
# CONFLUENCE PUBLISH
# ============================================================

def _publish_to_confluence(
    epic_key: str,
    epic_summary: str,
    fsd_content: str,
    space_key: str = "",
    user_id: str = "",
    user_email: str = "",
) -> str:
    """
    Create a Confluence page for the FSD.
    Returns the page URL, or empty string on failure.
    """
    from tools.confluence_tools import confluence_create_page

    page_title = f"FSD: {epic_summary}"
    page_body  = (
        f"# Functional Specification Document\n\n"
        f"**Epic:** {epic_key} — {epic_summary}  \n"
        f"**Generated:** {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}  \n"
        f"**Pipeline:** BRD→FSD Auto-Generation\n\n"
        f"---\n\n"
        f"{fsd_content}"
    )

    try:
        result_json = confluence_create_page(
            title=page_title,
            body=page_body,
            space_key=space_key,
            user_id= user_id,
            user_email=user_email,
        )
        result = json.loads(result_json)
        if "error" in result:
            logger.error(f"brd_fsd_pipeline: Confluence error for {epic_key}: {result['error']}")
            return ""
        url = result.get("url", "")
        logger.info(f"brd_fsd_pipeline: Confluence page created for {epic_key}: {url}")
        return url
    except Exception as e:
        logger.error(f"brd_fsd_pipeline: Confluence publish failed for {epic_key}: {e}")
        return ""


# ============================================================
# JIRA STORY CREATION
# ============================================================

_STORY_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:Story:\s*|##\s*User Story[:\s]+|User Story[:\s]+)(.+?)(?=\n|$)",
    re.IGNORECASE,
)

_AC_BLOCK_RE = re.compile(
    r"Acceptance Criteria:\s*((?:\s*-\s*.+\n?)+)",
    re.IGNORECASE,
)


def _extract_stories(fsd_content: str) -> list:
    """
    Parse FSD for User Stories.
    Returns a list of dicts with keys: summary, description.
    """
    stories = []
    matches = list(_STORY_HEADER_RE.finditer(fsd_content))

    for i, m in enumerate(matches):
        story_title = m.group(1).strip()
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(fsd_content)
        block = fsd_content[start:end].strip()

        # Extract acceptance criteria from the story block
        ac_match = _AC_BLOCK_RE.search(block)
        ac_text  = ac_match.group(0).strip() if ac_match else ""

        # Combine "As a..." sentence + acceptance criteria as the description
        lines = block.split("\n")
        as_a  = next(
            (l.strip() for l in lines if l.strip().lower().startswith("as a")),
            "",
        )
        description = "\n".join(filter(None, [as_a, ac_text])) or block[:500]

        stories.append({
            "summary":     f"[{story_title}]",
            "description": description,
        })

    return stories


def _create_jira_stories(
    epic_key: str,
    fsd_content: str,
    jira_project: str = "",
    user_id: str = "",
    user_email: str = "",
) -> list:
    """
    Create Jira Story issues for each user story in the FSD.
    Returns a list of created issue URLs.
    """
    from tools.jira_tools import jira_create_issue

    stories = _extract_stories(fsd_content)
    if not stories:
        logger.info(f"brd_fsd_pipeline: no user stories found in FSD for {epic_key}")
        return []

    created_urls = []
    for story in stories:
        try:
            description = (
                f"{story['description']}\n\n"
                f"_Auto-generated from BRD→FSD pipeline for Epic {epic_key}_"
            )
            url = jira_create_issue(
                summary=story["summary"],
                description=description,
                project=jira_project,
                priority="Medium",
                issue_type="Story",
                user_id= user_id,
                user_email=user_email,
            )
            if url and not url.startswith("Error"):
                created_urls.append(url)
                logger.info(f"brd_fsd_pipeline: created Jira story: {url}")
            else:
                logger.warning(f"brd_fsd_pipeline: Jira story creation returned: {url}")
        except Exception as e:
            logger.error(f"brd_fsd_pipeline: failed to create Jira story '{story['summary']}': {e}")

    return created_urls


# ============================================================
# COMPLIANCE CHECK
# ============================================================




# ============================================================
# MAIN PIPELINE CLASS
# ============================================================

class BRDFSDPipeline:
    """
    BRD → FSD pipeline triggered by a Jira Epic with label "BRD".

    Workflow:
      run() → parse BRD → generate FSD → compliance check → store HITL state
      approve_brd_fsd() → publish to Confluence → create Jira stories → done
    """

    def run(
        self,
        epic_key:         str,
        epic_summary:     str,
        epic_description: str,
        confluence_space: str = "",
        jira_project:     str = "",
    ) -> dict:
        """
        Execute the BRD→FSD pipeline up to the HITL gate.

        Returns:
          {
            "status":           "pending_approval" | "error",
            "epic_key":         str,
            "fsd_content":      str,
            "brd_sections":     dict,
            "confluence_url":   "",          # empty until approved
            "stories_created":  [],          # empty until approved
            "hitl_status":      "pending",
            "hitl_id":          str,         # use to call approve_brd_fsd()
          }
        """
        logger.info(f"brd_fsd_pipeline: starting pipeline for {epic_key}")

        # ── Step 1: Parse BRD ──────────────────────────────────
        brd_sections = _parse_brd(epic_description)
        logger.info(
            f"brd_fsd_pipeline: parsed BRD sections for {epic_key}: "
            + ", ".join(k for k, v in brd_sections.items() if v)
        )

        # ── Step 2: Generate FSD via Claude ───────────────────
        fsd_content = _generate_fsd(epic_key, epic_summary, brd_sections)

        if fsd_content.startswith("[ERROR"):
            return {
                "status":          "error",
                "epic_key":        epic_key,
                "fsd_content":     fsd_content,
                "brd_sections":    brd_sections,
                "confluence_url":  "",
                "stories_created": [],
                "hitl_status":     "error",
                "error":           fsd_content,
            }

        # ── Step 4: Store HITL pending state ───────────────────
        hitl_id = str(uuid.uuid4())
        _hitl_state[epic_key] = {
            "hitl_id":          hitl_id,
            "epic_key":         epic_key,
            "epic_summary":     epic_summary,
            "fsd_content":      fsd_content,
            "brd_sections":     brd_sections,
            "confluence_space": confluence_space,
            "jira_project":     jira_project,
            "status":           "pending",
            "created_at":       datetime.utcnow().isoformat(),
        }

        logger.info(
            f"brd_fsd_pipeline: {epic_key} awaiting HITL approval (hitl_id={hitl_id})"
        )

        # Notify inbox
        try:
            from store.inbox_store import publish_inbox_item
            publish_inbox_item(
                user_id="platform",
                type="brd_fsd_pending_approval",
                title=f"[BRD→FSD] FSD ready for approval — {epic_key}",
                body=(
                    f"FSD generated for Epic **{epic_key}**: {epic_summary}\n"
                    f"Review and approve at `POST /sdlc/brd-fsd/{epic_key}/approve`"
                ),
                source_id=hitl_id,
                metadata={
                    "epic_key":    epic_key,
                    "hitl_id":     hitl_id,
                    "pipeline":    "brd_fsd",
                    "stage":       "hitl_pending",
                },
            )
        except Exception:
            pass

        return {
            "status":          "pending_approval",
            "epic_key":        epic_key,
            "fsd_content":     fsd_content,
            "brd_sections":    brd_sections,
            "confluence_url":  "",
            "stories_created": [],
            "hitl_status":     "pending",
            "hitl_id":         hitl_id,
        }


# ============================================================
# HITL APPROVAL
# ============================================================

def approve_brd_fsd(epic_key: str, note: str = "",
                    user_id: str = "", user_email: str = "",) -> dict:
    """
    HITL approval — proceed with Confluence publish + Jira story creation.

    Called by POST /sdlc/brd-fsd/{epic_key}/approve.

    Returns:
      {
        "status":           "approved",
        "epic_key":         str,
        "confluence_url":   str,
        "stories_created":  list[str],
        "hitl_status":      "approved",
        "note":             str,
      }
    """
    bind_context(correlation_id=epic_key, pipeline_stage="brd_fsd_approve")
    state = _hitl_state.get(epic_key)
    if not state:
        logger.warning(f"brd_fsd_pipeline: no pending HITL state for {epic_key}")
        return {
            "status":  "error",
            "epic_key": epic_key,
            "error":   f"No pending BRD→FSD pipeline found for epic {epic_key}. "
                       "Ensure the pipeline has been run first.",
        }

    if state.get("status") == "approved":
        logger.info(f"brd_fsd_pipeline: {epic_key} already approved")
        return {
            "status":          "already_approved",
            "epic_key":        epic_key,
            "confluence_url":  state.get("confluence_url", ""),
            "stories_created": state.get("stories_created", []),
            "hitl_status":     "approved",
        }

    epic_summary     = state["epic_summary"]
    fsd_content      = state["fsd_content"]
    confluence_space = state.get("confluence_space", "")
    jira_project     = state.get("jira_project", "")

    logger.info(f"brd_fsd_pipeline: HITL approved for {epic_key} — publishing to Confluence")

    # ── Step 4 (post-approval): Publish FSD to Confluence ─────
    confluence_url = _publish_to_confluence(
        epic_key=epic_key,
        epic_summary=epic_summary,
        fsd_content=fsd_content,
        space_key=confluence_space,
        user_id= user_id,
        user_email=user_email,
    )

    # ── Step 5: Create Jira stories from FSD ──────────────────
    stories_created = _create_jira_stories(
        epic_key=epic_key,
        fsd_content=fsd_content,
        jira_project=jira_project,
        user_id= user_id,
        user_email=user_email,
    )

    # ── Update HITL state ──────────────────────────────────────
    state["status"]          = "approved"
    state["confluence_url"]  = confluence_url
    state["stories_created"] = stories_created
    state["approved_at"]     = datetime.utcnow().isoformat()
    state["note"]            = note

    logger.info(
        f"brd_fsd_pipeline: {epic_key} approval complete — "
        f"confluence={confluence_url} stories={len(stories_created)}"
    )

    # Notify inbox
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id="platform",
            type="brd_fsd_approved",
            title=f"[BRD→FSD] FSD approved & published — {epic_key}",
            body=(
                f"FSD for Epic **{epic_key}** approved.\n"
                f"Confluence: {confluence_url or '(not published)'}\n"
                f"Jira Stories created: {len(stories_created)}"
                + (f"\nNote: {note}" if note else "")
            ),
            source_id=epic_key,
            metadata={
                "epic_key":       epic_key,
                "confluence_url": confluence_url,
                "stories_count":  len(stories_created),
                "pipeline":       "brd_fsd",
                "stage":          "approved",
            },
        )
    except Exception:
        pass

    return {
        "status":          "approved",
        "epic_key":        epic_key,
        "confluence_url":  confluence_url,
        "stories_created": stories_created,
        "hitl_status":     "approved",
        "note":            note,
    }


# ============================================================
# PIPELINE ENTRY POINT (for RQ worker)
# ============================================================

def run_brd_fsd_pipeline_job(payload: dict) -> dict:
    """
    RQ worker entry point for the BRD→FSD pipeline.
    payload keys: epic_key, summary, description, confluence_space, jira_project
    """
    epic_key         = payload.get("epic_key", payload.get("key", ""))
    bind_context(correlation_id=epic_key, pipeline_stage="brd_fsd_pipeline")
    epic_summary     = payload.get("summary", "")
    epic_description = payload.get("description", "")
    confluence_space = payload.get("confluence_space", "")
    jira_project     = payload.get("jira_project", "")

    pipeline = BRDFSDPipeline()
    return pipeline.run(
        epic_key=epic_key,
        epic_summary=epic_summary,
        epic_description=epic_description,
        confluence_space=confluence_space,
        jira_project=jira_project,
    )
