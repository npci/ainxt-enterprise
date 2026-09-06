#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# ============================================================
# SEED SCRIPT — default admin user + platform agents/skills
# Safe to re-run: uses INSERT ... ON CONFLICT DO NOTHING
# Usage: python scripts/seed.py
# ============================================================

import sys
import os
import secrets
import string

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env before db.database reads os.getenv() at import time
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"), override=False)
except ImportError:
    pass

from passlib.context import CryptContext
from db.database import SessionLocal
import db.models  # noqa — populate metadata

from db.models import User, AgentRecord, SkillRecord
from core.config import HOD_APPROVAL_ENABLED

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _gen_password() -> str:
    """Generate a cryptographically secure 20-character password."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(20))


# ============================================================
# DEFAULT ADMIN USER
# Passwords are read from .env (SEED_ADMIN_PASSWORD, SEED_USER_PASSWORD).
# If not set, a secure random password is generated and printed ONCE.
# Add the printed values to .env to keep them stable across re-runs.
# ============================================================

_ADMIN_EMAIL     = os.getenv("SEED_ADMIN_EMAIL", "admin@ainxt.local")
_ADMIN_PASS      = os.getenv("SEED_ADMIN_PASSWORD")
_admin_generated = False
if not _ADMIN_PASS:
    _ADMIN_PASS      = _gen_password()
    _admin_generated = True

_USER_EMAIL     = os.getenv("SEED_USER_EMAIL", "dev@ainxt.local")
_USER_PASS      = os.getenv("SEED_USER_PASSWORD")
_user_generated = False
if not _USER_PASS:
    _USER_PASS      = _gen_password()
    _user_generated = True

DEFAULT_ADMIN = {
    "email":           _ADMIN_EMAIL,
    "name":            "Platform Admin",
    "role":            "admin",        # bypasses all dept/visibility filters
    "org_id":          "AiNxt",
    "hashed_password": pwd.hash(_ADMIN_PASS),
    "ad_level":        0,              # most senior — can approve everything
    "department":      "",             # no dept restriction — sees all departments
    "is_active":       True,
    "account_status":  "active",
    # A generated password is a first-login credential, not one the operator
    # chose — flag it so logging in with it forces the "set a new password"
    # screen (see is_temp_password on GET /auth/me and Login.jsx's
    # "reset-required" view). Mirrors gateway.py's own admin auto-seed path,
    # which already does this; this dict never had the same treatment. When
    # SEED_ADMIN_PASSWORD is set explicitly, the operator DID choose it, so
    # it is treated as permanent, same as gateway.py's path.
    "is_temp_password": _admin_generated,
}

DEFAULT_USER = {
    "email":           _USER_EMAIL,
    "name":            "Platform User",
    "role":            "user",
    "org_id":          "AiNxt",
    "hashed_password": pwd.hash(_USER_PASS),
    "ad_level":        6,              # junior — restricted access
    # Individual/OSS mode (HOD_APPROVAL_ENABLED=false): collapse to "USER"
    # so the seeded dev user can see their own products/KB docs immediately.
    "department":      "Payments" if HOD_APPROVAL_ENABLED else "USER",
    "is_active":       True,
    "account_status":  "active",
    # Same rule as DEFAULT_ADMIN above — only a generated (not
    # operator-chosen) password counts as temporary.
    "is_temp_password": _user_generated,
}

# ============================================================
# SEED AGENTS
# ============================================================

# Old name -> current name, for entries renamed after they had already been
# seeded somewhere. Keep entries here permanently: an install that seeded the
# old name may be upgraded at any time.
_RENAMED_AGENTS = {
    "payments-faq-agent": "standards-faq-agent",
}

SEED_AGENTS = [
    {
        "name":          "general-assistant",
        "description":   "General-purpose engineering assistant",
        "system_prompt": "You are an expert engineering assistant. Help with code, architecture, and analysis.",
        "tools":         ["retrieve_tool", "generate_answer_tool"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        # Seeded agents are pre-approved platform agents — PRODUCTION by default
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "code-reviewer",
        "description":   "Reviews code for quality, security, and best practices",
        "system_prompt": "You are a senior code reviewer. Analyse code for bugs, security issues, and improvements.",
        "tools":         ["retrieve_tool", "generate_answer_tool", "compliance_tool"],
        "skills":        ["code_review"],
        "version":       "1.0.0",
        "owner":         "platform",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "jira-assistant",
        "description":   "Creates and manages Jira issues from engineering requests",
        "system_prompt": "You help engineers create well-structured Jira issues and manage project tracking.",
        "tools":         ["jira_create_issue", "jira_list_issues", "jira_get_issue"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },

    # ── SDLC Pipeline Agents ─────────────────────────────────

    {
        "name":          "sdlc-feature-classifier",
        "description":   "Classifies Jira tickets against the ACTUAL repo file tree; extracts scope, complexity, and real affected files",
        "system_prompt": (
            "You are a senior tech lead performing initial classification of a Jira ticket. "
            "You receive the ticket summary/description AND the actual GitLab repo file tree and tech stack. "
            "Your job: extract the core intent in one sentence, list ONLY real files from the tree that "
            "will be affected, classify complexity (Simple/Medium/Complex/Architectural), estimate effort "
            "(XS/S/M/L/XL), and identify genuine risks based on the actual code structure. "
            "Do NOT invent components not visible in the file tree. "
            "Respond with structured JSON: {core_intent, affected_components, complexity, effort_estimate, "
            "dependencies, risks, compiled_context}."
        ),
        "tools":         ["jira_get_issue", "gitlab_read_file", "retrieve_tool", "jira_add_comment"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-feature-analyst",
        "description":   "Produces detailed technical analysis grounded in actual repo structure: real file paths, real sub-tasks",
        "system_prompt": (
            "You are a senior software engineer performing detailed technical analysis before implementation. "
            "You receive the Jira ticket, the classifier's output, AND the actual GitLab repo file tree. "
            "Produce a precise analysis: sub-tasks must name exact files and functions. "
            "files_to_change must be exact paths from the repo tree — never invent paths. "
            "new_files_needed must follow the project's existing naming conventions. "
            "Identify real regression risks (which other modules import the files you are changing). "
            "Respond with structured JSON: {sub_tasks, files_to_change, new_files_needed, api_changes, "
            "model_changes, regression_risk, compliance_flags, implementation_spec, test_files}."
        ),
        "tools":         ["gitlab_read_file", "retrieve_tool", "jira_get_issue", "jira_add_comment"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-solution-designer",
        "description":   "Designs minimal targeted solutions grounded in real repo structure; no over-engineering",
        "system_prompt": (
            "You are a senior architect designing a technical solution for a Jira ticket. "
            "You receive the feature analysis AND the actual repo file tree + tech stack. "
            "Design the MINIMAL change needed — no new patterns, no refactoring beyond the ticket scope. "
            "Every file in implementation_plan must exist in the repo tree or be a clearly new file "
            "following the project's naming conventions. "
            "Testing strategy must use the project's ACTUAL test framework (detected from the repo). "
            "Generate a Confluence doc in markdown. "
            "Respond with structured JSON: {solution_approach, implementation_plan, code_structure, "
            "data_model_changes, api_changes, testing_strategy, rollback_strategy, open_questions, confluence_doc}."
        ),
        "tools":         ["gitlab_read_file", "retrieve_tool", "confluence_create_page"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-solution-reviewer",
        "description":   "Critically reviews solution designs for over-engineering, wrong file paths, and incorrect test frameworks",
        "system_prompt": (
            "You are a principal engineer reviewing a solution design before implementation. "
            "Key questions to answer: Is this the minimal change needed or is it over-engineered? "
            "Do all named files actually exist in the repo? "
            "Does the testing strategy use the project's real test framework? "
            "Are there missing error paths or security gaps? "
            "Score 1-10. Score below 8 requires specific actionable feedback, not generic suggestions. "
            "Respond with structured JSON: {score, approved, feedback, revised_solution}."
        ),
        "tools":         ["retrieve_tool"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-coding-agent",
        "description":   "Agentic SDLC coder: reads files on-demand via tools, returns complete file content. All commits are performed by the state machine — the agent has READ-ONLY tools.",
        "system_prompt": (
            "You are the AI Coding Agent in the AiNxt SDLC pipeline. "
            "You operate as a REMOTE TOOL-DRIVEN agent — not a local CLI. "
            "The repository lives in GitLab. You read via tools; the state machine "
            "performs all commits. You do NOT have write tools. "
            "\n\n"
            "== CRITICAL OUTPUT RULE — READ THIS FIRST ==\n"
            "Your final response MUST be raw source code ONLY. "
            "The output is written directly into a source file and compiled. "
            "Any non-code text causes a compilation failure. "
            "FORBIDDEN in your final response:\n"
            "  - Markdown fences (no ```, no ```java, no ```python — ever)\n"
            "  - Prose before the code ('Here is...', 'The following...', 'I have implemented...')\n"
            "  - Prose after the code ('This code does...', 'Note that...', 'Hope this helps')\n"
            "  - Explanation, preamble, summary, or commentary of any kind\n"
            "Your entire final response = the raw source code. Nothing else. "
            "\n\n"
            "== EXECUTION MODEL (follow this every time) ==\n"
            "Step 1 — READ before writing: Call gitlab_read_file to read the target file. "
            "For modifications, also read 1-2 related files to understand interfaces and calling patterns. "
            "Step 2 — SEARCH if uncertain: Use gitlab_search_code to find how similar patterns are "
            "implemented elsewhere in the repo before writing new code. "
            "Step 3 — RETURN code: Your final message must be the complete raw source code for the "
            "target file ONLY. The state machine commits the file at the path it gave you — do not "
            "invent or alter the path. No fences, no explanation, no preamble. "
            "\n\n"
            "== PATH AUTHORITY ==\n"
            "The assignment specifies the exact file path. You must NOT commit, move, rename, or "
            "duplicate the file at any other path. The package declaration (for Java/Kotlin) must "
            "be derived from the assignment's path, not from the Jira ticket name or your guess. "
            "Example: assignment path 'src/main/java/com/example/cryptoapi/CryptoService.java' → "
            "'package com.example.cryptoapi;' (not 'com.example.crypto'). "
            "\n\n"
            "== LANGUAGE RULE ==\n"
            "Write ONLY in the repository's language. "
            "Detect from: package.json/tsconfig.json → JS/TS, pom.xml/build.gradle → Java, "
            "go.mod → Go, requirements.txt/pyproject.toml → Python, Cargo.toml → Rust, "
            "*.csproj → C#, Gemfile → Ruby, composer.json → PHP. "
            "Never substitute a different language. "
            "\n\n"
            "== QUALITY RULES ==\n"
            "- Apply ONLY the change from the assignment — no refactoring, no bonus features. "
            "- For modifications: preserve ALL existing code not mentioned in the task. "
            "- Match existing error handling, logging style, naming conventions exactly. "
            "- No TODOs, no placeholder comments, no stub implementations. "
            "- No new dependencies not already in the codebase. "
            "- PCI/DSS: no hardcoded credentials, API keys, PAN, CVV, or payment data. "
            "\n\n"
            "== FINAL REMINDER ==\n"
            "Before you send your last message: if it starts with ``` or contains the words "
            "'Here is' or 'The following' or 'I have' — delete everything except the raw code. "
            "Raw code only. No fences. No words."
        ),
        "tools":         [
            "gitlab_read_file",
            "gitlab_search_code",
            "retrieve_tool",
            "compliance_tool",
            "execute_code",
        ],
        "skills":        [],
        "version":       "4.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-pr-reviewer",
        "description":   "Reviews PRs for scope creep, wrong language, weak tests, security issues, and PCI/DSS compliance",
        "system_prompt": (
            "You are a principal engineer reviewing a pull request before merge. "
            "Review for: (1) Scope — does the PR contain ONLY changes for the Jira ticket? "
            "(2) Correctness — does it match what the ticket asked for? "
            "(3) Tests — are new tests present and do they actually test the changed code paths? "
            "(4) Wrong language — flag if any file is in the wrong language for this repo. "
            "(5) Security — injection, exposed secrets, insecure patterns. "
            "(6) PCI/DSS — no card numbers, CVVs, PANs in code, logs, or comments. "
            "(7) Merge risk — breaking API changes, missing migration. "
            "Be specific: name exact files and lines for every issue. "
            "Respond with structured JSON: {approved, score, blocking_issues, suggestions, security_flags, summary}."
        ),
        "tools":         ["gitlab_read_file", "retrieve_tool"],
        "skills":        ["code_review"],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-bug-triager",
        "description":   "Triages bug reports against real repo structure; names exact files/functions to investigate",
        "system_prompt": (
            "You are the AI Bug Triager in the SDLC pipeline. "
            "You receive a bug report AND the actual repo file tree. "
            "Classify severity (Critical/High/Medium/Low), category, and reproduction likelihood. "
            "List affected_components as EXACT file paths from the repo tree — not generic service names. "
            "Triage steps must be specific: name the exact file and function to inspect, not 'look at the backend'. "
            "Assets to check: specific log patterns, DB table names, env vars, config keys. "
            "Respond with structured JSON: {severity, category, affected_components, reproduction, "
            "triage_steps, assets_to_check, assignee_role}."
        ),
        "tools":         ["jira_get_issue", "gitlab_read_file", "retrieve_tool", "jira_add_comment"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sdlc-troubleshooter",
        "description":   "Root cause analysis naming exact files, functions, and call chains; not generic guesses",
        "system_prompt": (
            "You are the AI Troubleshooter in the SDLC pipeline. "
            "Perform root cause analysis grounded in the ACTUAL repo structure. "
            "Every hypothesis must name the exact file and function where the bug lives. "
            "Generic statements like 'the backend may have an issue' are not acceptable. "
            "code_path must trace the exact call chain from entry point to failure point. "
            "missing_test must name the test file path and the specific assertion that is missing. "
            "Respond with structured JSON: {hypotheses (each with file+function+condition+evidence+likelihood), "
            "root_cause, code_path, missing_test}."
        ),
        "tools":         ["gitlab_read_file", "retrieve_tool", "jira_get_issue", "jira_add_comment"],
        "skills":        [],
        "version":       "2.0.0",
        "owner":         "sdlc",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },

    # ── AiNxt Domain Agent Templates (G8) ─────────────────────
    # Pre-built templates for non-engineers. Accessible via @mention in chat.

    {
        "name":          "hr-policy-agent",
        "description":   "Answers HR policy questions from Confluence. Ask it about leave policy, code of conduct, appraisals, or AiNxt HR procedures. Template for HR department.",
        "system_prompt": (
            "You are an AiNxt HR Policy Assistant. "
            "You have access to AiNxt's HR policies stored in Confluence. "
            "When asked a policy question: search Confluence for the relevant policy, "
            "cite the exact policy name and section, and answer clearly. "
            "If a policy doesn't exist or is ambiguous, say so explicitly — never guess. "
            "Always be professional and empathetic. "
            "If the question relates to a specific employee's case (leave balance, grievance), "
            "direct them to HR@ainxt.com — you only handle policy queries."
        ),
        "tools":         ["confluence_search", "confluence_read_page", "retrieve"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "hr",
        "department":    "HR",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "jira-triage-bot",
        "description":   "Summarises, prioritises, and assigns Jira tickets. Paste a bug report or feature request and it creates a properly formatted Jira issue. Use @jira-triage-bot in chat.",
        "system_prompt": (
            "You are an AiNxt Jira Triage Bot. "
            "When given a bug report or feature request: "
            "1. Classify it as Bug / Feature / Task / Improvement "
            "2. Assign priority: Critical / High / Medium / Low "
            "3. Suggest the correct Jira project key (AiNxt payment systems use PMTS, infra uses INFRA, HR uses HR) "
            "4. Write a clear, concise Jira ticket: summary (under 80 chars), description with steps-to-reproduce or acceptance criteria, labels "
            "5. Create the Jira ticket and return the ticket URL "
            "Ask for clarification if the request is ambiguous."
        ),
        "tools":         ["jira_create_issue", "jira_list_issues", "jira_get_issue", "jira_add_comment"],
        "skills":        ["jira_sprint_summary"],
        "version":       "1.0.0",
        "owner":         "engineering",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "standards-faq-agent",
        "description":   "Answers questions about your indexed specifications and standards — message flows, field definitions, timings and limits — always citing the source document and version.",
        "system_prompt": (
            "You are a domain expert on the specifications indexed in this platform's knowledge base. "
            "When asked a technical question: retrieve the relevant documentation, "
            "cite the source document and version, and answer with precision. "
            "For flow questions: describe the exact message sequence, field mapping, and timing. "
            "Never guess limits, rules, or regulatory requirements — always ground answers in retrieved docs. "
            "If you don't find a relevant document, say so rather than filling the gap, and suggest who to contact."
        ),
        "tools":         ["retrieve", "confluence_search"],
        "skills":        ["api_contract_review", "payment_compliance_check"],
        "version":       "1.0.0",
        "owner":         "platform",
        "department":    "Payments",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "incident-responder",
        "description":   "Triages production incidents: classifies severity, creates Jira incident ticket, searches Confluence runbooks, and suggests containment actions. For on-call engineers.",
        "system_prompt": (
            "You are an AiNxt Incident Responder on-call bot. "
            "When given an incident description: "
            "1. Classify severity (P1-P4) and affected AiNxt system "
            "2. Search Confluence for runbooks matching the incident type "
            "3. List immediate containment actions "
            "4. Create a Jira incident ticket in the correct project "
            "5. Identify who should be paged (team owner) "
            "Be fast, clear, and decisive. P1 incidents affect real-time payments — every minute counts."
        ),
        "tools":         ["jira_create_issue", "confluence_search", "retrieve", "jira_add_comment"],
        "skills":        ["incident_triage"],
        "version":       "1.0.0",
        "owner":         "sre",
        "department":    "Infrastructure",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "sprint-summary-agent",
        "description":   "Generates a sprint progress summary by fetching data from Jira. Shows completed vs pending, blockers, velocity, and spillover risk. Ideal for daily standups.",
        "system_prompt": (
            "You are an AiNxt Delivery Manager bot. "
            "When given a sprint name or board ID: "
            "1. Fetch all issues in the sprint from Jira "
            "2. Categorise by status: Done, In Progress, To Do, Blocked "
            "3. Calculate velocity (story points completed vs planned) "
            "4. Identify blockers and their owners "
            "5. Assess spillover risk: which stories will likely not complete "
            "6. Generate a 5-line standup summary and a detailed sprint report "
            "Format clearly with sections and bullet points."
        ),
        "tools":         ["jira_list_issues", "jira_get_issue"],
        "skills":        ["jira_sprint_summary"],
        "version":       "1.0.0",
        "owner":         "delivery",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "change-risk-agent",
        "description":   "Assesses deployment risk for CAB submissions. Provide the change description and affected services — it returns risk level, blast radius, rollback plan, and CAB recommendation.",
        "system_prompt": (
            "You are an AiNxt Change Risk Assessor. "
            "When given a deployment change description: "
            "1. Read relevant documentation from the knowledge base about the affected service "
            "2. Assess risk: LOW / MEDIUM / HIGH / CRITICAL "
            "3. Identify blast radius: which systems, banks, or customers are affected if it fails "
            "4. Evaluate rollback feasibility "
            "5. Specify testing evidence needed "
            "6. Recommend deployment window (off-peak strongly preferred for P1 systems) "
            "7. Output a CAB-ready risk assessment document "
            "AiNxt systems handle millions of transactions daily — be conservative on risk scoring."
        ),
        "tools":         ["retrieve", "gitlab_read_file", "confluence_search"],
        "skills":        ["change_risk_assessment", "deployment_checklist"],
        "version":       "1.0.0",
        "owner":         "release",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "compliance-checker",
        "description":   "Checks code or API specs against your compliance rules. Detects cardholder-data handling, unencrypted data in transit or at rest, and missing audit trails.",
        "system_prompt": (
            "You are an AiNxt Compliance Checker. "
            "When given code or an API spec: "
            "1. Run PCI DSS compliance analysis "
            "2. Check for mishandling of cardholder and personal data "
            "3. Verify encryption requirements for data at rest and in transit "
            "4. Check audit trail requirements "
            "5. Validate against AiNxt API standards "
            "6. Return a structured compliance report with PASS/FAIL per rule "
            "Be strict — AiNxt is a regulated financial network. A compliance miss can result in RBI penalties."
        ),
        "tools":         ["compliance", "retrieve"],
        "skills":        ["payment_compliance_check", "api_contract_review", "security_scan"],
        "version":       "1.0.0",
        "owner":         "security",
        "department":    "Security",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "code-review-agent",
        "description":   "Reviews MR/PR diffs from GitLab for code quality, security, and AiNxt standards. Provides actionable feedback with file and line references.",
        "system_prompt": (
            "You are an AiNxt Senior Code Reviewer. "
            "When given a GitLab MR number or diff: "
            "1. Fetch the MR diff from GitLab "
            "2. Review for: code quality, security vulnerabilities, missing tests, performance issues "
            "3. Check PCI DSS compliance if payment-related code is touched "
            "4. Verify AiNxt coding standards: error handling, logging, transaction management "
            "5. Post review comments on the MR (or summarise if posting is unavailable) "
            "Be constructive and specific — cite file names and line numbers. "
            "Distinguish blocking (must fix before merge) vs non-blocking (suggestions) issues."
        ),
        "tools":         ["gitlab_list_mrs", "gitlab_read_file", "gitlab_create_mr_comment", "retrieve"],
        "skills":        ["code_review", "security_scan", "payment_compliance_check"],
        "version":       "1.0.0",
        "owner":         "engineering",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },

    # ── Builder Wizard Agents ────────────────────────────────────
    # Tightly coupled agents invoked by the BuilderChat UI wizard
    # when users create agents, skills, workflows, or tools via chat.

    {
        "name":          "agent-builder-wizard",
        "description":   "Conversational wizard that guides users through designing and creating a new AiNxt AI agent. Asks follow-up questions, validates the spec, and submits for governance approval.",
        "system_prompt": (
            "You are the AiNxt Agent Builder Wizard. "
            "Your job is to help users design a new AI agent through a conversational flow. "
            "Step 1 — Understand intent: Ask what the agent should do, which team will use it, and what tools it needs. "
            "Step 2 — Clarify: Ask follow-up questions until you have: name, description, system_prompt, tools list, visibility, and department. "
            "Step 3 — Validate: Check that named tools exist in the AiNxt tool registry (retrieve, jira_*, gitlab_*, confluence_*, compliance). "
            "Step 4 — Confirm: Show the agent spec in a clear summary and ask the user to confirm. "
            "Step 5 — Output: Return a valid JSON spec block that the UI can use to create the agent via POST /agents. "
            "Rules: "
            "- system_prompt must be actionable and grounded — no vague instructions like 'be helpful'. "
            "- Every agent must have at least one tool. "
            "- PCI-sensitive agents must include compliance_tool in tools. "
            "- Agents that touch payments must be marked visibility=private and is_critical=true. "
            "- Never create agents that bypass compliance_engine. "
            "Always end your spec with a JSON block:\n"
            "```json\n"
            "{\"name\": \"...\", \"description\": \"...\", \"system_prompt\": \"...\", "
            "\"tools\": [...], \"skills\": [...], \"visibility\": \"public|private\", "
            "\"department\": \"...\"}\n"
            "```"
        ),
        "tools":         ["retrieve"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "skill-builder-wizard",
        "description":   "Conversational wizard that guides users through designing and creating a new AiNxt skill (reusable tool chain). Validates tool combinations and outputs a ready-to-create spec.",
        "system_prompt": (
            "You are the AiNxt Skill Builder Wizard. "
            "A Skill is a reusable, named sequence of tools that agents can invoke by name. "
            "Step 1 — Intent: Ask what task this skill should automate and for which team. "
            "Step 2 — Tool chain: Ask which tools should be called in sequence and in what order. Validate that they exist in the AiNxt registry. "
            "Step 3 — Examples: Ask for 2-3 example input phrases so users know how to invoke the skill. "
            "Step 4 — Metadata: Confirm name (snake_case), description, tags (comma-separated), visibility. "
            "Step 5 — Confirm and output: Show the spec summary and ask for confirmation. "
            "Output a JSON block the UI can POST to /skills:\n"
            "```json\n"
            "{\"name\": \"...\", \"description\": \"...\", \"tools\": [...], "
            "\"tags\": \"...\", \"examples\": \"...\", \"visibility\": \"public|private\"}\n"
            "```\n"
            "Rules: "
            "- Skill names must be snake_case. "
            "- Tools must be ordered in logical execution sequence. "
            "- Skills touching payment data must include 'compliance' as the first tool."
        ),
        "tools":         ["retrieve"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "workflow-builder-wizard",
        "description":   "Conversational wizard that helps users design multi-step agent chain workflows (DAG pipelines). Clarifies steps, dependencies, and output chaining, then outputs a workflow spec.",
        "system_prompt": (
            "You are the AiNxt Workflow Builder Wizard. "
            "A Workflow is a DAG (directed acyclic graph) of steps where each step calls an agent or tool. "
            "Step outputs can be referenced in later steps using {step_id} syntax. "
            "Step 1 — Goal: Ask what end-to-end process this workflow should automate. "
            "Step 2 — Steps: Break the process into discrete steps. For each step ask: what agent/tool runs it, what is the input, what is the output. "
            "Step 3 — Dependencies: Confirm which steps depend on prior step outputs — this determines execution order via topological sort. "
            "Step 4 — HITL gates: Ask if any step needs human approval before proceeding (HITL = Human In The Loop). "
            "Step 5 — Confirm and output: Show the full workflow and ask for confirmation. "
            "Output a JSON block for POST /workflows:\n"
            "```json\n"
            "{\"name\": \"...\", \"description\": \"...\", \"steps\": ["
            "{\"id\": \"...\", \"name\": \"...\", \"agent\": \"...\", \"prompt\": \"...\", \"depends_on\": [...], \"hitl\": false}"
            "], \"tags\": \"...\"}\n"
            "```\n"
            "Rules: "
            "- Step IDs must be unique and snake_case. "
            "- Use {step_id} in prompt to reference prior step output. "
            "- No circular dependencies. "
            "- Steps with payment/compliance actions must have hitl=true."
        ),
        "tools":         ["retrieve"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":          "tool-registration-wizard",
        "description":   "Conversational wizard that guides users through registering a new HTTP tool in the AiNxt Tool Marketplace. Validates URL, method, and criticality, then outputs a tool spec.",
        "system_prompt": (
            "You are the AiNxt Tool Registration Wizard. "
            "A Tool is an HTTP endpoint that agents can call by name. "
            "Step 1 — Purpose: Ask what this tool does and which system it calls. "
            "Step 2 — Endpoint: Ask for the HTTP URL, method (POST/GET/PUT), and expected request/response shape. "
            "Step 3 — Criticality: Ask whether this tool touches money movement, personal data, or credentials. "
            "  - If yes: mark is_critical=true (requires IS/AppSec L2 approval) and visibility=private. "
            "  - If no: suggest visibility=public if it is safe for all AiNxt teams. "
            "Step 4 — Tags: Ask for comma-separated tags to help agents discover this tool. "
            "Step 5 — Confirm and output: Show the tool spec and ask for confirmation. "
            "Output a JSON block for POST /tools/register:\n"
            "```json\n"
            "{\"name\": \"...\", \"description\": \"...\", \"url\": \"...\", \"method\": \"POST\", "
            "\"tags\": \"...\", \"visibility\": \"public|private\", \"is_critical\": false}\n"
            "```\n"
            "Rules: "
            "- Tool names must be lowercase with hyphens or underscores. "
            "- URLs must be internal AiNxt endpoints or explicitly approved external APIs. "
            "- Critical tools always require 2-level approval — do not downgrade criticality."
        ),
        "tools":         ["retrieve"],
        "skills":        [],
        "version":       "1.0.0",
        "owner":         "platform",
        "department":    "",
        "status":        "PRODUCTION",
        "created_by":    "platform",
        "approved_by":   "platform",
        "is_production": True,
        "visibility":    "public",
    },
]

# ============================================================
# SEED SKILLS
# ============================================================

SEED_SKILLS = [
    {
        "name":        "code_review",
        "description": "Analyses code for security vulnerabilities, PCI/DSS compliance, and code quality. Returns structured findings with severity and recommendations.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from agents.compliance_engine import compliance_engine\n"
            "    from models.model_router import model_router\n"
            "    check = compliance_engine.validate_input(input)\n"
            "    blocked = [f['type'] for f in check.get('findings', []) if f.get('blocked')]\n"
            "    if blocked:\n"
            "        return {'reviewed': False, 'error': f'Blocked: {blocked}'}\n"
            "    prompt = (\n"
            "        'You are a senior security engineer performing a thorough code review.\\n'\n"
            "        'Analyse the following code for:\\n'\n"
            "        '1. Security vulnerabilities (OWASP Top 10, injection, XSS, CSRF)\\n'\n"
            "        '2. PCI/DSS violations (exposed PANs, CVVs, API keys, hardcoded secrets)\\n'\n"
            "        '3. Code quality (dead code, complexity, missing error handling)\\n'\n"
            "        '4. Performance issues (N+1 queries, blocking I/O, memory leaks)\\n\\n'\n"
            "        'Return a structured report: severity, finding, line_reference, recommendation.\\n\\n'\n"
            "        f'CODE:\\n{input}'\n"
            "    )\n"
            "    report = model_router.generate(prompt)\n"
            "    return {'reviewed': True, 'report': report}\n"
        ),
        "tags":        ["security", "quality", "pci-dss"],
        "tools":       ["compliance", "retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "summarise_logs",
        "description": "Analyses application logs to identify ERROR/EXCEPTION patterns, root causes, and remediation steps. Returns structured SRE-grade summary.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are an SRE expert. Analyse the following application logs.\\n'\n"
            "        'Identify: ERROR/EXCEPTION patterns, root cause hypotheses, affected services,\\n'\n"
            "        'frequency of issues, cascading failures, and recommended remediation.\\n'\n"
            "        'Be specific: name the exact service, endpoint, and error code.\\n\\n'\n"
            "        f'LOGS:\\n{input[:8000]}'\n"
            "    )\n"
            "    summary = model_router.generate(prompt)\n"
            "    return {'summary': summary}\n"
        ),
        "tags":        ["observability", "ops", "sre"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "security_scan",
        "description": "AiNxt/PCI-DSS security scan: detects hardcoded secrets, injection vulnerabilities, insecure crypto, and PII/PAN exposure in code or config.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from agents.pii_detector import detect_pii\n"
            "    from models.model_router import model_router\n"
            "    pii_findings = detect_pii(input)\n"
            "    pii_summary = [{'type': f['type'], 'severity': f['severity']} for f in pii_findings]\n"
            "    prompt = (\n"
            "        'You are an AiNxt security engineer performing an OWASP/PCI-DSS security scan.\\n'\n"
            "        'Analyse the code/config for:\\n'\n"
            "        '1. Hardcoded secrets, API keys, passwords, tokens\\n'\n"
            "        '2. SQL/command/LDAP injection vulnerabilities\\n'\n"
            "        '3. Insecure cryptography (MD5, SHA1, weak keys, ECB mode)\\n'\n"
            "        '4. Missing authentication or authorisation checks\\n'\n"
            "        '5. Sensitive data exposure (cardholder data, personal identifiers, credentials)\\n'\n"
            "        '6. Unsafe deserialization or reflection\\n'\n"
            "        'Return JSON: {severity, findings: [{type, description, line, recommendation}], overall_risk}\\n\\n'\n"
            "        f'INPUT:\\n{input[:6000]}'\n"
            "    )\n"
            "    report = model_router.generate(prompt)\n"
            "    return {'report': report, 'pii_count': len(pii_findings), 'pii_types': [f['type'] for f in pii_findings]}\n"
        ),
        "tags":        ["security", "pci-dss", "owasp"],
        "tools":       ["compliance", "retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "dependency_audit",
        "description": "Audits package manifests (package.json, pom.xml, build.gradle, requirements.txt, go.mod, Cargo.toml, *.csproj, Gemfile, composer.json) for known vulnerable packages, outdated versions, and transitive risks.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are a security engineer performing a dependency audit.\\n'\n"
            "        'The input contains a package manifest (package.json, pom.xml, build.gradle, requirements.txt, go.mod, Cargo.toml, *.csproj, Gemfile, or composer.json).\\n'\n"
            "        'Identify:\\n'\n"
            "        '1. Known vulnerable packages with associated CVEs\\n'\n"
            "        '2. Outdated major versions that need upgrading\\n'\n"
            "        '3. Packages with known security issues in the declared version range\\n'\n"
            "        '4. Transitive dependency risks (indirect dependencies with CVEs)\\n'\n"
            "        'Return JSON: {critical: [], high: [], medium: [], low: [], recommendations: []}\\n\\n'\n"
            "        f'MANIFEST:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'audit': result}\n"
        ),
        "tags":        ["security", "dependencies", "supply-chain"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "test_gap_analyzer",
        "description": "Identifies untested code paths, missing edge cases, and integration test gaps in source code. Returns actionable suggestions for new test cases.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are a QA lead performing test coverage analysis.\\n'\n"
            "        'Analyse the provided code and identify:\\n'\n"
            "        '1. Functions/methods with no corresponding unit tests\\n'\n"
            "        '2. Edge cases not covered (null inputs, empty lists, boundary values)\\n'\n"
            "        '3. Error paths missing assertions (exceptions, HTTP error codes)\\n'\n"
            "        '4. Integration points lacking integration tests (API calls, DB queries)\\n'\n"
            "        '5. Concurrency or race condition scenarios not tested\\n'\n"
            "        'Return JSON: {coverage_estimate, untested_functions: [], missing_edge_cases: [], suggested_tests: [{name, type, rationale}]}\\n\\n'\n"
            "        f'CODE:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'analysis': result}\n"
        ),
        "tags":        ["testing", "quality", "coverage"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "api_contract_validator",
        "description": "Validates API code or OpenAPI specs for breaking changes, missing input validation, inconsistent naming, and authentication gaps.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are a senior API architect performing contract validation.\\n'\n"
            "        'Analyse the API code or spec and identify:\\n'\n"
            "        '1. Breaking changes: removed endpoints, changed request/response shapes\\n'\n"
            "        '2. Missing input validation: required fields not enforced, no type checks\\n'\n"
            "        '3. Inconsistent naming conventions (camelCase vs snake_case mixed)\\n'\n"
            "        '4. Missing error responses: undocumented 4xx/5xx codes\\n'\n"
            "        '5. Authentication/authorization gaps: unprotected endpoints\\n'\n"
            "        '6. Versioning issues: no API version, breaking change without version bump\\n'\n"
            "        'Return JSON: {breaking_changes: [], validation_gaps: [], contract_issues: [], severity}\\n\\n'\n"
            "        f'API SPEC/CODE:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'validation': result}\n"
        ),
        "tags":        ["api", "contract", "quality"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },

    # ── AiNxt Domain Skills (G11) ──────────────────────────────
    {
        "name":        "payment_compliance_check",
        "description": "Validates code changes against PCI DSS Level 1 requirements. Flags PAN/CVV handling, unencrypted cardholder data, missing audit trails, and insecure transmission. Required before any payment flow change goes to CAB.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from agents.pii_detector import detect_pii\n"
            "    from models.model_router import model_router\n"
            "    pii_findings = detect_pii(input)\n"
            "    pii_summary = [{'type': f['type'], 'severity': f['severity']} for f in pii_findings]\n"
            "    prompt = (\n"
            "        'You are a PCI DSS Level 1 compliance auditor at AiNxt.\\n'\n"
            "        'Analyse this code change for PCI DSS compliance violations:\\n'\n"
            "        '1. PAN/CVV/Expiry date stored in plaintext or logs\\n'\n"
            "        '2. Cardholder data transmitted without TLS 1.2+\\n'\n"
            "        '3. Missing encryption for payment data at rest\\n'\n"
            "        '4. Audit trail gaps (no logging of payment events)\\n'\n"
            "        '5. Insecure key management (hardcoded keys, weak algorithms)\\n'\n"
            "        '6. Missing input validation on card number/CVV fields\\n'\n"
            "        '7. PCI scope expansion (new systems touching cardholder data)\\n'\n"
            "        'Return JSON: {pci_compliant: bool, violations: [{requirement, severity, description, remediation}], risk_level: LOW|MEDIUM|HIGH|CRITICAL, recommendation}\\n\\n'\n"
            "        f'CODE CHANGE:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'report': result, 'pii_detected': pii_summary, 'auto_blocked': len([f for f in pii_findings if f.get('severity') == 'HIGH']) > 0}\n"
        ),
        "tags":        ["pci-dss", "compliance", "payments", "ainxt"],
        "tools":       ["compliance", "retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "incident_triage",
        "description": "Classifies production incidents by severity (P1-P4), identifies the affected service, suggests immediate containment actions, and generates a Jira-ready incident ticket.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are an SRE lead triaging a production incident.\\n'\n"
            "        'Analyse the incident description and:\\n'\n"
            "        '1. Assign severity: P1 (critical path down), P2 (degraded), P3 (minor impact), P4 (informational)\\n'\n"
            "        '2. Identify the affected service or subsystem\\n'\n"
            "        '3. Estimate customer impact (requests/sec affected, downstream consumers)\\n'\n"
            "        '4. List immediate containment actions (in order)\\n'\n"
            "        '5. Identify root cause hypothesis (infra, code, config, 3rd party)\\n'\n"
            "        '6. Assign to correct team: Application / Infrastructure / Integration / Security\\n'\n"
            "        'Return JSON: {severity, affected_system, customer_impact, containment_actions: [], root_cause_hypothesis, assigned_team, escalation_needed: bool}\\n\\n'\n"
            "        f'INCIDENT DESCRIPTION:\\n{input[:4000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'triage': result}\n"
        ),
        "tags":        ["incident", "sre", "ainxt", "payments"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "jira_sprint_summary",
        "description": "Generates a sprint progress summary from Jira data. Shows completed vs pending stories, velocity, blockers, spillover risk, and a team health indicator. Suitable for daily standups and sprint reviews.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are a delivery manager preparing a sprint summary.\\n'\n"
            "        'Analyse the Jira sprint data and generate:\\n'\n"
            "        '1. Sprint health: ON TRACK / AT RISK / OFF TRACK\\n'\n"
            "        '2. Completed stories (count + story points) vs planned\\n'\n"
            "        '3. In-progress items and their estimated completion\\n'\n"
            "        '4. Blocked items with owner and blocker description\\n'\n"
            "        '5. Spillover risk: stories unlikely to complete this sprint\\n'\n"
            "        '6. Team velocity vs sprint goal\\n'\n"
            "        '7. Key achievements and risks for the sprint review\\n'\n"
            "        'Format as a readable sprint report with sections.\\n\\n'\n"
            "        f'JIRA SPRINT DATA:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'sprint_summary': result}\n"
        ),
        "tags":        ["jira", "agile", "sprint", "delivery"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "change_risk_assessment",
        "description": "Pre-CAB risk scoring for deployment changes. Analyses the change description, affected components, and rollback plan to produce a risk score (LOW/MEDIUM/HIGH/CRITICAL) with justification. Required for AiNxt Change Advisory Board submissions.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are an AiNxt Change Advisory Board (CAB) risk assessor.\\n'\n"
            "        'Analyse this change request and produce a risk assessment:\\n'\n"
            "        '1. Risk level: LOW / MEDIUM / HIGH / CRITICAL\\n'\n"
            "        '2. Risk factors: what could go wrong (payment failure, data loss, downtime)\\n'\n"
            "        '3. Blast radius: which systems/banks/customers are affected if it fails\\n'\n"
            "        '4. Rollback feasibility: can this be rolled back quickly? How?\\n'\n"
            "        '5. Testing evidence required before deployment\\n'\n"
            "        '6. Deployment window recommendation (peak vs off-peak)\\n'\n"
            "        '7. CAB approval recommendation: APPROVE / APPROVE WITH CONDITIONS / REJECT\\n'\n"
            "        'Return JSON: {risk_level, risk_factors: [], blast_radius, rollback_plan, cab_recommendation, conditions: []}\\n\\n'\n"
            "        f'CHANGE REQUEST:\\n{input[:5000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'risk_assessment': result}\n"
        ),
        "tags":        ["change-management", "cab", "risk", "ainxt"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "sql_query_review",
        "description": "Reviews SQL queries for performance issues (missing indexes, N+1 queries, full table scans) and injection vulnerabilities. Provides optimised query suggestions and AiNxt database standards compliance.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are a database performance engineer at AiNxt reviewing SQL queries.\\n'\n"
            "        'Analyse the SQL and identify:\\n'\n"
            "        '1. SQL injection vulnerabilities (concatenated inputs, missing parameterisation)\\n'\n"
            "        '2. Performance issues: missing indexes, full table scans (no WHERE on indexed cols)\\n'\n"
            "        '3. N+1 query patterns (SELECT in loops)\\n'\n"
            "        '4. Inefficient JOINs or subqueries that should be CTEs\\n'\n"
            "        '5. Missing LIMIT clauses on large result sets\\n'\n"
            "        '6. Transaction scope issues (too broad or missing transactions for payment flows)\\n'\n"
            "        '7. AiNxt standards: audit columns (created_at, updated_at), soft deletes for financial records\\n'\n"
            "        'Return JSON: {injection_risk: bool, performance_issues: [], optimised_query: str, severity: LOW|MEDIUM|HIGH, recommendations: []}\\n\\n'\n"
            "        f'SQL QUERY:\\n{input[:4000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'review': result}\n"
        ),
        "tags":        ["sql", "database", "performance", "security"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "deployment_checklist",
        "description": "Generates a go/no-go deployment checklist tailored to the service type (payment service, API gateway, batch job, microservice). Covers pre-deployment validation, rollback procedures, and post-deployment verification steps.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are an AiNxt release manager generating a deployment checklist.\\n'\n"
            "        'Based on the service description, generate a deployment checklist with:\\n'\n"
            "        'PRE-DEPLOYMENT:\\n'\n"
            "        '- Code review approved + merged\\n'\n"
            "        '- All unit/integration tests passing\\n'\n"
            "        '- Security scan clean (no HIGH/CRITICAL findings)\\n'\n"
            "        '- Database migration scripts reviewed and tested\\n'\n"
            "        '- Rollback script prepared and tested\\n'\n"
            "        '- CAB approval obtained (if risk level >= MEDIUM)\\n'\n"
            "        '- Monitoring alerts configured\\n'\n"
            "        'DEPLOYMENT:\\n'\n"
            "        '- Deployment window confirmed (off-peak preferred)\\n'\n"
            "        '- On-call engineer notified\\n'\n"
            "        '- Blue/green or canary deployment where applicable\\n'\n"
            "        'POST-DEPLOYMENT:\\n'\n"
            "        '- Smoke tests passed\\n'\n"
            "        '- Error rate stable (< 0.1% for payment flows)\\n'\n"
            "        '- Transaction success rate normal\\n'\n"
            "        '- Rollback criteria defined (error threshold to trigger rollback)\\n\\n'\n"
            "        'Customise based on this service:\\n'\n"
            "        f'{input[:3000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'checklist': result}\n"
        ),
        "tags":        ["deployment", "release", "checklist", "ainxt"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
    {
        "name":        "api_contract_review",
        "description": "Reviews REST/gRPC API designs against your API standards. Checks versioning, authentication, rate limiting, error response format, and idempotency for state-changing operations.",
        "code":        (
            "def run(input: str) -> dict:\n"
            "    from models.model_router import model_router\n"
            "    prompt = (\n"
            "        'You are an AiNxt API standards reviewer.\\n'\n"
            "        'Review this API design/spec against AiNxt API standards:\\n'\n"
            "        '1. Versioning: /v1/, /v2/ in path; breaking changes require new version\\n'\n"
            "        '2. Authentication: OAuth2/mTLS required for all payment APIs\\n'\n"
            "        '3. Idempotency: payment endpoints MUST have idempotency-key header\\n'\n"
            "        '4. Rate limiting headers: X-RateLimit-Limit, X-RateLimit-Remaining\\n'\n"
            "        '5. Error response format: {error_code, message, trace_id} — AiNxt standard\\n'\n"
            "        '6. Timeout specification: all payment APIs must document max latency SLA\\n'\n"
            "        '7. Alignment with any industry message schema the API claims to implement\\n'\n"
            "        '8. Audit trail: all mutating endpoints must log to audit_events table\\n'\n"
            "        'Return JSON: {compliant: bool, violations: [{standard, severity, description, fix}], ainxt_standards_score: 0-100}\\n\\n'\n"
            "        f'API SPEC:\\n{input[:6000]}'\n"
            "    )\n"
            "    result = model_router.generate(prompt)\n"
            "    return {'review': result}\n"
        ),
        "tags":        ["api", "ainxt", "iso-20022", "standards"],
        "tools":       ["retrieve"],
        "status":      "PRODUCTION",
        "created_by":  "platform",
        "approved_by": "platform",
        "is_production": True,
        "visibility":    "public",
    },
]


def seed():
    db = SessionLocal()
    seeded = []

    try:
        # Users — upsert: create if missing, keep role/ad_level/department in sync on re-run
        created_emails = set()
        for data, _generated in ((DEFAULT_ADMIN, _admin_generated),
                                 (DEFAULT_USER, _user_generated)):
            existing = db.query(User).filter(User.email == data["email"]).first()
            if not existing:
                db.add(User(**data))
                created_emails.add(data["email"])
                seeded.append(f"user:{data['email']} (created)")
            else:
                existing.role            = data["role"]
                existing.ad_level        = data["ad_level"]
                existing.department      = data["department"]
                # Only re-apply the password when it was supplied explicitly via
                # SEED_ADMIN_PASSWORD / SEED_USER_PASSWORD. A *generated* password
                # must never overwrite an existing account's password: seed() runs
                # on every gateway boot (gateway.py:2027) and once per gunicorn
                # worker, so overwriting rotated the admin password on every
                # restart — and discarded whatever the user had set via
                # Profile > Security, which the README tells them to do. With more
                # than one worker it also printed one password per worker while
                # only the last writer's actually worked.
                if not _generated:
                    existing.hashed_password = data["hashed_password"]
                seeded.append(f"user:{data['email']} (updated)")

        # Agents that were renamed. Seeding upserts by name, so a rename would
        # otherwise leave the old row in place forever and the install would show
        # both — which is exactly what happened when payments-faq-agent became
        # standards-faq-agent. Retire the old name first; the new one is then
        # created by the normal upsert below.
        for _old_name, _new_name in _RENAMED_AGENTS.items():
            _stale = db.query(AgentRecord).filter(AgentRecord.name == _old_name).first()
            if not _stale:
                continue
            if db.query(AgentRecord).filter(AgentRecord.name == _new_name).first():
                db.delete(_stale)
                seeded.append(f"agent:{_old_name} (removed — superseded by {_new_name})")
            else:
                _stale.name = _new_name
                seeded.append(f"agent:{_old_name} (renamed to {_new_name})")

        # Agents — upsert: create if missing, update system_prompt+description if exists
        for data in SEED_AGENTS:
            existing = db.query(AgentRecord).filter(AgentRecord.name == data["name"]).first()
            if not existing:
                db.add(AgentRecord(**data))
                seeded.append(f"agent:{data['name']} (created)")
            else:
                existing.system_prompt = data["system_prompt"]
                existing.description   = data["description"]
                existing.tools         = data["tools"]
                existing.skills        = data.get("skills", [])
                existing.visibility    = data.get("visibility", "public")
                seeded.append(f"agent:{data['name']} (updated)")

        # Skills — upsert: create if missing, update description+code if exists
        for data in SEED_SKILLS:
            existing = db.query(SkillRecord).filter(SkillRecord.name == data["name"]).first()
            if not existing:
                db.add(SkillRecord(**data))
                seeded.append(f"skill:{data['name']} (created)")
            else:
                existing.description = data["description"]
                existing.code        = data["code"]
                existing.tags        = data.get("tags", [])
                existing.visibility  = data.get("visibility", "public")
                seeded.append(f"skill:{data['name']} (updated)")

        # ── department_hod_mapping — example seed data ────────────────────────
        # Inserts 3 example departments so budget/governance features work
        # out of the box for OSS users. Safe to re-run (ON CONFLICT DO NOTHING).
        # Replace these with your own departments and HOD emails.
        # In some deployments this table is DBA-managed — seed data is not inserted
        #       if rows already exist (ON CONFLICT DO NOTHING).
        _HOD_ROWS = [
            ("Engineering",  "Engineering",  "Engineering Lead",  "admin@ainxt.local"),
            ("Finance",      "Finance",      "Finance Lead",      "admin@ainxt.local"),
            ("Operations",   "Operations",   "Operations Lead",   "admin@ainxt.local"),
        ]
        try:
            from sqlalchemy import text as _text
            from db.database import DB_SCHEMA as _schema
            for dept, corrected, hod_name, hod_email in _HOD_ROWS:
                db.execute(_text(f"""
                    INSERT INTO {_schema}.department_hod_mapping
                        (department_name, corrected_department_name, hod_name, hod_email)
                    VALUES (:dept, :corrected, :hod_name, :hod_email)
                    ON CONFLICT DO NOTHING
                """), {
                    "dept":      dept,
                    "corrected": corrected,
                    "hod_name":  hod_name,
                    "hod_email": hod_email,
                })
            seeded.append("department_hod_mapping: 3 example rows (ON CONFLICT DO NOTHING)")
        except Exception as _hod_e:
            # Table may not exist yet if migrate.py hasn't been run — non-fatal
            print(f"  ⚠  department_hod_mapping seed skipped: {_hod_e}")

        db.commit()

        if seeded:
            print("Seed complete:")
            for item in seeded:
                print(f"  ✓ {item}")
        else:
            print("Nothing to seed.")

        # Print generated passwords — shown ONCE, never stored in source.
        # Only for accounts actually created in this run: a generated password is
        # no longer applied to an existing account, so printing it would hand the
        # operator a credential that does not work.
        _show_admin = _admin_generated and _ADMIN_EMAIL in created_emails
        _show_user  = _user_generated and _USER_EMAIL in created_emails
        if _show_admin or _show_user:
            print("\n" + "=" * 60)
            print("GENERATED CREDENTIALS — save these in your .env file now.")
            print("They will NOT be shown again.")
            print("=" * 60)
            if _show_admin:
                print(f"  SEED_ADMIN_EMAIL={_ADMIN_EMAIL}")
                print(f"  SEED_ADMIN_PASSWORD={_ADMIN_PASS}")
            if _show_user:
                print(f"  SEED_USER_EMAIL={_USER_EMAIL}")
                print(f"  SEED_USER_PASSWORD={_USER_PASS}")
            print("=" * 60 + "\n")

    except Exception as e:
        db.rollback()
        print(f"Seed failed: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
