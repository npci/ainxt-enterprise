#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Seed AiNxt Cowork's pre-loaded OFFICE skill library into SkillRecord (skills_pg).

Claude Cowork ships ~132 skills across 18 domains out of the box. This seeds a
curated NON-engineering office set so Cowork/plugins have skills immediately —
no import required. All are BEHAVIORAL skills (plain-text SOPs injected into the
agent's system prompt — model-agnostic, no code execution), status=PRODUCTION,
visibility=public. Idempotent (upsert by name).

Run:  venv/bin/python scripts/seed_cowork_skills.py
Also safe to call from startup via seed_cowork_skills().
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (name, description, body)  — body is the SOP injected into the system prompt.
COWORK_SKILLS = [
    ("email_drafting", "Draft clear, professional emails in the user's voice.",
     "When drafting an email: open with the purpose in one line, keep paragraphs short, use a polite "
     "professional tone, end with a clear ask or next step, and append the user's saved email signature. "
     "Never send — always present the draft for confirmation."),
    ("email_summarisation", "Summarise an inbox/thread into key points + actions.",
     "Summarise emails as: (1) one-line gist, (2) key points as bullets, (3) action items with owners/dates, "
     "(4) anything needing a reply. Group by sender/thread. Flag anything urgent first."),
    ("document_summarisation", "Summarise long documents/PDFs into a brief.",
     "Produce: a 2-3 sentence executive summary, then section-wise bullets, then open questions/risks. "
     "Preserve figures and decisions verbatim. Keep it under one page unless asked."),
    ("report_writing", "Write a structured business report.",
     "Structure: Title, Executive Summary, Background, Findings (with sub-headings + bullets), "
     "Recommendations, Next Steps. Write for a non-technical exec audience. Offer to generate it as a Word/PDF."),
    ("meeting_notes", "Turn raw notes/transcripts into clean minutes.",
     "Output: Attendees, Agenda, Discussion (per topic), Decisions, Action Items (owner + due date). "
     "Be concise and factual; do not invent attendees or decisions."),
    ("presentation_outline", "Build a slide deck outline.",
     "Produce a slide-by-slide outline: title slide, agenda, 4-6 content slides (each = heading + 3-5 bullets), "
     "a stats/summary slide, and a closing/next-steps slide. Offer to generate it as a PowerPoint."),
    ("spreadsheet_builder", "Plan/produce an Excel with the right columns + formulas.",
     "Clarify the rows/columns, propose a header row, note any totals/variance/conditional formatting, "
     "then offer to generate the .xlsx. Keep numbers exactly as provided."),
    ("status_update", "Write a crisp status update / standup.",
     "Format: Done, In progress, Blocked, Next. One line each. Pull facts from connectors/tickets when available; "
     "never fabricate progress."),
    ("action_item_extraction", "Extract action items from any content.",
     "List action items as: [Owner] — [Action] — [Due]. Only include explicit or clearly-implied commitments."),
    ("proofreading", "Proofread and tighten text without changing meaning.",
     "Fix grammar, clarity, and tone; keep the author's intent and facts. Show the cleaned version; "
     "list notable changes only if asked."),
    ("translation", "Translate business text between languages.",
     "Translate faithfully, preserving tone and formatting. Keep names, figures, and acronyms intact. "
     "Note any term that doesn't translate cleanly."),
    ("calendar_digest", "Summarise a calendar into a daily/weekly digest.",
     "Output a chronological digest: time, title, attendees, location/link, and a one-line prep note per meeting. "
     "Flag conflicts and back-to-backs."),
    # ── AiNxt platform productivity skills — self-contained, localised to AiNxt. ──
    ("internal_comms", "Draft internal comms (3P updates, newsletters, FAQs, status/leadership/incident reports) in AiNxt format.",
     "Identify the communication type, then apply its structure. **3P update** (default for team/project updates): "
     "Progress (what advanced), Plans (what's next + owner/date), Problems (blockers + the explicit ask). "
     "**Newsletter**: headline + 3-5 highlight bullets + 'what this means for you' + a CTA. "
     "**FAQ**: restate question, direct answer FIRST, brief context, where to learn more. "
     "**Status report**: RAG (Red/Amber/Green) up top, then scope/timeline/budget, then risks + mitigations. "
     "**Leadership update**: lead with the decision + business impact in one line, <150 words, no jargon. "
     "**Incident report**: what happened, impact, timeline (IST), root cause, remediation, follow-ups w/ owners. "
     "Always lead with the conclusion; be specific (numbers, dates IST, owners); never invent data (use 'TBD'); "
     "never include secrets/PII."),
    ("doc_coauthoring", "Co-author a document in 3 stages: gather context, refine section-by-section, reader-test before sign-off.",
     "Don't jump to a full draft. **Stage 1 Context**: ask a tight set of meta-questions (type, audience, the "
     "decision/action wanted, length/format, any template), then invite a context dump. **Stage 2 Refine**: "
     "confirm a section outline first; build section-by-section starting with the hardest/most-uncertain; edit "
     "incrementally (never reprint the whole doc for a small change); check for gaps before prose. **Stage 3 Reader "
     "test**: at ~80% complete, read it as the intended reader with no prior context, flag ambiguities/undefined "
     "terms/missing 'so what', fix them, then ask for final sign-off. One clarifying round per stage. Produce the "
     "file via the document tools when wanted."),
    ("brand_style", "Apply AiNxt brand voice + visual consistency to office artifacts (docs, emails, decks).",
     "Voice: clear, formal, trustworthy (a national payments institution); plain language; expand acronyms on first "
     "use; active voice; no marketing hype in internal comms. Visual (for generated docs/decks): one clear heading "
     "hierarchy, a restrained palette (single accent + neutral text on light background, consistent throughout), "
     "generous white space, tables for structured data, charts only when they add insight; figures in INR (crore "
     "where that's house style). Document files already use the AiNxt corporate template via the doc tools — keep "
     "content consistent with it; never override the user's explicit format/theme request."),
]


def seed_cowork_skills(org_id: str = "default") -> int:
    from db.database import SessionLocal
    from db.models import SkillRecord
    n = 0
    db = SessionLocal()
    try:
        for name, desc, body in COWORK_SKILLS:
            existing = db.query(SkillRecord).filter(SkillRecord.name == name, SkillRecord.org_id == org_id).first()
            tags = ["cowork", "office", "preloaded"]
            if existing:
                existing.description = desc
                existing.code = body
                existing.skill_type = "behavioral"
                existing.status = "PRODUCTION"
                existing.is_production = True
                existing.visibility = "public"
                existing.tags = tags
            else:
                db.add(SkillRecord(
                    name=name, org_id=org_id, description=desc, code=body,
                    skill_type="behavioral", status="PRODUCTION", is_production=True,
                    visibility="public", tags=tags, created_by="seed:cowork",
                    input_schema={}, output_schema={}, permissions=[], tools=[],
                ))
            n += 1
        db.commit()
    finally:
        db.close()
    return n


# ── A ready-to-use role specialist that BUNDLES the office skills ────────────
# "Exec Assistant" = specialist prompt + scoped connectors + a curated skill set.
# This is the concrete example for testing role specialists end-to-end.
_ROLE = {
    "name": "Exec Assistant",
    "description": "Executive office assistant: drafts polished internal comms and documents in "
                   "AiNxt style, working from your Outlook mail/calendar, Teams, Jira, and Confluence. "
                   "Bundles internal-comms, doc co-authoring, email, meeting-notes, report, calendar, and brand skills.",
    "system_prompt": (
        "You are the Exec Assistant — a senior executive office assistant for AiNxt staff. You draft "
        "polished internal communications, status updates, briefs, minutes, and documents on the user's "
        "behalf in AiNxt's voice. Be proactive and concise: anticipate what the user needs, lead with the "
        "answer, propose a clear next step. Ground drafts in the user's connected apps (Outlook mail/calendar, "
        "Teams, Jira, Confluence) when relevant. You NEVER send, post, or write anything without explicit "
        "confirmation, and never put secrets or PII in a draft. Apply your bundled skills' SOPs automatically."
    ),
    "allowed_connectors": ["microsoft_365", "jira", "confluence"],
    "skill_names": ["internal_comms", "doc_coauthoring", "email_drafting", "meeting_notes",
                    "report_writing", "calendar_digest", "brand_style"],
    "subagent_allowlist": [],
    "department": "",
    "visibility": "public",
}


def seed_cowork_role() -> str:
    """Upsert the 'Exec Assistant' role specialist (idempotent by name+dept). Seeded
    as a PUBLIC + APPROVED org-wide example (the seed acts as the governance approver)."""
    from services.cowork_roles import (
        CoworkRole, create_role, get_role_by_name, update_role, set_role_status,
    )
    fields = {
        "system_prompt": _ROLE["system_prompt"],
        "description": _ROLE["description"],
        "allowed_connectors": _ROLE["allowed_connectors"],
        "skill_names": _ROLE["skill_names"],
        "subagent_allowlist": _ROLE["subagent_allowlist"],
        "visibility": _ROLE["visibility"],
    }
    existing = get_role_by_name(_ROLE["name"], _ROLE["department"])
    if existing:
        update_role(existing.id, **fields)
        rid, verb = existing.id, "updated"
    else:
        rid = create_role(CoworkRole(name=_ROLE["name"], department=_ROLE["department"],
                                     created_by="seed:cowork", **fields)).id
        verb = "created"
    set_role_status(rid, "APPROVED", "seed:cowork")   # org-wide example, pre-approved
    return f"{verb} {_ROLE['name']} ({rid}) [public/APPROVED]"


if __name__ == "__main__":
    count = seed_cowork_skills()
    print(f"Seeded/updated {count} Cowork office skills (behavioral, PRODUCTION, public).")
    print(f"Role specialist: {seed_cowork_role()}")
