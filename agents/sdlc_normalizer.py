# SPDX-License-Identifier: MIT
"""
agents/sdlc_normalizer.py — TICKET_NORMALIZATION stage agent.

Converts a raw Jira ticket into a locked, structured WorkItem that every
downstream stage (CLASSIFYING, ANALYZING, DESIGNING, DIAGNOSING) consumes
instead of raw ticket text. Eliminates ambiguity at the source rather than
letting it propagate through 14 pipeline stages.

DESIGN NOTES
------------
- Model: haiku tier by default; override via SDLC_MODEL_NORMALIZE, which accepts
  either a router tier name or a concrete model id of any provider (resolved by
  sdlc_stage_hint → model_router, so it works on a harness with no Anthropic).
- Input: jira_get_issue_full() dict + repo context + workspace root
- Thin tickets: infers from ticket type + repo context, raises open_questions
- Thick tickets: distils — extracts structured fields, ignores narrative noise
- Returns (WorkItem, open_questions list) — empty list when ticket is complete
- WorkItem.locked = True means all fields confirmed; pipeline may proceed
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from core.logger import logger


# ── WorkItem ──────────────────────────────────────────────────────────────────

@dataclass
class WorkItem:
    """Canonical representation of what needs to be built or fixed.

    Populated by NormalizationAgent.normalize() and locked by
    apply_user_answers(). Every downstream stage receives this, never raw
    ticket text. locked=True means the pipeline may proceed without asking
    the user again.
    """
    problem_statement: str = ""
    acceptance_criteria: list = field(default_factory=list)
    scope: list = field(default_factory=list)
    out_of_scope: list = field(default_factory=list)
    constraints: list = field(default_factory=list)
    technical_hints: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    locked: bool = False
    jira_key: str = ""

    def to_dict(self) -> dict:
        return {
            "problem_statement": self.problem_statement,
            "acceptance_criteria": self.acceptance_criteria,
            "scope": self.scope,
            "out_of_scope": self.out_of_scope,
            "constraints": self.constraints,
            "technical_hints": self.technical_hints,
            "open_questions": self.open_questions,
            "locked": self.locked,
            "jira_key": self.jira_key,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkItem":
        if not isinstance(d, dict):
            return cls()
        return cls(
            problem_statement=str(d.get("problem_statement") or ""),
            acceptance_criteria=list(d.get("acceptance_criteria") or []),
            scope=list(d.get("scope") or []),
            out_of_scope=list(d.get("out_of_scope") or []),
            constraints=list(d.get("constraints") or []),
            technical_hints=list(d.get("technical_hints") or []),
            open_questions=list(d.get("open_questions") or []),
            locked=bool(d.get("locked")),
            jira_key=str(d.get("jira_key") or ""),
        )


# ── NormalizationAgent ────────────────────────────────────────────────────────

_NORMALIZATION_SCHEMA = {
    "problem_statement": "string — one precise paragraph describing the exact problem",
    "acceptance_criteria": ["list of specific, testable outcomes"],
    "scope": ["list of files/systems/components explicitly in scope"],
    "out_of_scope": ["list of explicit exclusions — things that must NOT be touched"],
    "constraints": ["list of things that must not change: backwards compat, API contracts, etc."],
    "technical_hints": ["list of any technical direction mentioned in the ticket"],
    "open_questions": [
        {
            "field": "which WorkItem field this answer fills: one of "
                     "problem_statement|scope|acceptance_criteria|out_of_scope|constraints|technical_hints",
            "question": "one concise sentence the user must clarify",
            "options": ["2-4 concrete candidate answers the user can choose from"],
            "recommended": 0,
            "rationale": "why this matters and why the recommended option is the sensible default",
        }
    ],
}


class NormalizationAgent:
    """Converts a raw Jira issue dict into a locked WorkItem.

    Call normalize() once per pipeline run immediately after PREFLIGHT/BASELINE_BUILD.
    The agent uses haiku (cheap, fast) for JSON extraction and raises open_questions
    only for fields it genuinely cannot infer from the ticket text or repo context.
    """

    def __init__(self, run_id: str = ""):
        self._run_id = run_id or ""

    def _model(self) -> str:
        from core.model_registry import sdlc_stage_hint
        return sdlc_stage_hint("normalize")

    def normalize(
        self,
        jira_dict: dict,
        repo_ctx: dict,
        workspace_root: str = "",
    ) -> tuple:
        """Extract a structured WorkItem from the raw Jira issue dict.

        Args:
            jira_dict: output of jira_get_issue_full() — includes comments,
                       acceptance_criteria, labels, etc.
            repo_ctx: {"language": ..., "framework": ..., "test_framework": ...}
            workspace_root: local workspace path (optional, used for context).

        Returns:
            (work_item: WorkItem, open_questions: list[dict])
            open_questions is empty when the ticket is complete and unambiguous.
        """
        key = jira_dict.get("key", "?")
        summary = jira_dict.get("summary", "")
        description = jira_dict.get("description", "")
        comments_raw = jira_dict.get("comments") or []
        ac_raw = jira_dict.get("acceptance_criteria", "")
        labels = jira_dict.get("labels") or []
        epic = jira_dict.get("epic_summary", "")
        attachments_text = str(jira_dict.get("attachments_text") or "")

        summary_len = len(summary)
        desc_len = len(description)
        n_comments = len(comments_raw)
        logger.info(
            f"[NORM {self._run_id}] jira={key} summary_len={summary_len} "
            f"desc_len={desc_len} comments={n_comments} attach_len={len(attachments_text)}"
        )

        comments_block = ""
        if comments_raw:
            parts = []
            for c in comments_raw[-5:]:
                author = c.get("author", "?")
                body = c.get("body", "").strip()[:500]
                parts.append(f"[{author}]: {body}")
            comments_block = "\n".join(parts)

        repo_lang = (repo_ctx or {}).get("language", "")
        repo_fw = (repo_ctx or {}).get("framework", "")
        repo_test = (repo_ctx or {}).get("test_framework", "")

        prompt = f"""You are a technical project manager extracting a structured Work Item from a Jira ticket.

=== JIRA TICKET ===
Key: {key}
Summary: {summary}
Description: {description}
Acceptance Criteria (custom field): {ac_raw or "(not set)"}
Labels: {", ".join(labels) or "(none)"}
Epic: {epic or "(none)"}
Recent Comments:
{comments_block or "(no comments)"}
Attachments:
{attachments_text[:8000] or "(no attachments)"}

=== REPO CONTEXT ===
Language: {repo_lang or "(unknown)"}
Framework: {repo_fw or "(unknown)"}
Test Framework: {repo_test or "(unknown)"}

=== TASK ===
Extract a structured Work Item. For each field:
- If the ticket provides clear information, extract it precisely.
- If the ticket is thin or ambiguous on a CRITICAL field (problem_statement, scope), add an open_question.
- Do NOT invent acceptance criteria or scope that is not stated or strongly implied.
- out_of_scope must list things explicitly excluded OR things a naive developer might accidentally touch.
- open_questions: only raise these if you genuinely cannot determine the field from the ticket.
  For EVERY open_question you raise, you MUST provide 2-4 concrete `options` the user can pick
  from, a `recommended` index (0-based) into that options list, and a one-line `rationale`. Make
  the options specific and actionable (real candidate scopes / criteria / directions), never
  generic placeholders like "yes/no" or "option A". The user answers by picking an option.

Respond with ONLY valid JSON matching this schema:
{json.dumps(_NORMALIZATION_SCHEMA, indent=2)}
"""
        try:
            from models.model_router import model_router
            result_raw = model_router.generate(prompt, model_hint=self._model())
        except Exception as e:
            logger.warning(f"[NORM {self._run_id}] LLM call failed: {e} — returning minimal WorkItem")
            wi = WorkItem(
                problem_statement=f"{summary}\n\n{description}".strip(),
                jira_key=key,
                locked=False,
            )
            return wi, [{"field": "problem_statement", "question": "LLM normalization failed — please clarify the problem"}]

        tokens_in = 0
        tokens_out = 0
        if isinstance(result_raw, dict):
            tokens_in = result_raw.get("usage", {}).get("input_tokens", 0)
            tokens_out = result_raw.get("usage", {}).get("output_tokens", 0)
            result_text = result_raw.get("content", "") or ""
        else:
            result_text = str(result_raw or "")

        parsed = _parse_json(result_text)

        ps = str(parsed.get("problem_statement") or "").strip()
        ac = [str(x) for x in (parsed.get("acceptance_criteria") or []) if x]
        scope = [str(x) for x in (parsed.get("scope") or []) if x]
        out_of_scope = [str(x) for x in (parsed.get("out_of_scope") or []) if x]
        constraints = [str(x) for x in (parsed.get("constraints") or []) if x]
        hints = [str(x) for x in (parsed.get("technical_hints") or []) if x]
        oqs_raw = parsed.get("open_questions") or []
        open_questions = []
        for q in oqs_raw:
            if not isinstance(q, dict) or not q.get("question"):
                continue
            opts = [str(o) for o in (q.get("options") or []) if str(o).strip()]
            rec = q.get("recommended")
            if not (isinstance(rec, int) and 0 <= rec < len(opts)):
                rec = 0 if opts else None
            open_questions.append({
                "field": str(q.get("field") or "").strip(),
                "question": str(q.get("question")).strip(),
                "options": opts,
                "recommended": rec,
                "rationale": str(q.get("rationale") or "").strip(),
            })

        logger.info(
            f"[NORM {self._run_id}] fields_extracted: "
            f"problem_statement={'yes' if ps else 'no'} "
            f"acceptance_criteria={len(ac)} "
            f"scope={len(scope)} out_of_scope={len(out_of_scope)}"
        )
        logger.info(
            f"[NORM {self._run_id}] model={self._model()} "
            f"prompt_chars={len(prompt)} tokens_in={tokens_in} tokens_out={tokens_out}"
        )

        if open_questions:
            oq_names = [q.get("field", "?") for q in open_questions]
            logger.info(
                f"[NORM {self._run_id}] open_questions={len(open_questions)} "
                f"— undetermined fields: {oq_names}"
            )
            wi = WorkItem(
                problem_statement=ps or f"{summary}\n{description}".strip(),
                acceptance_criteria=ac,
                scope=scope,
                out_of_scope=out_of_scope,
                constraints=constraints,
                technical_hints=hints,
                open_questions=open_questions,
                locked=False,
                jira_key=key,
            )
            return wi, open_questions

        logger.info(f"[NORM {self._run_id}] normalization complete — work_item locked (no open questions)")
        wi = WorkItem(
            problem_statement=ps or f"{summary}\n{description}".strip(),
            acceptance_criteria=ac,
            scope=scope,
            out_of_scope=out_of_scope,
            constraints=constraints,
            technical_hints=hints,
            open_questions=[],
            locked=True,
            jira_key=key,
        )
        return wi, []

    def apply_user_answers(self, work_item: WorkItem, answers: list) -> WorkItem:
        """Merge HITL user answers into the work_item and lock it.

        answers is a list of {field: str, answer: str} dicts from the UI.
        Each answer fills the corresponding WorkItem field.
        """
        n = len(answers or [])
        for ans in (answers or []):
            if not isinstance(ans, dict):
                continue
            f = str(ans.get("field") or "").strip().lower()
            a = str(ans.get("answer") or "").strip()
            if not f or not a:
                continue
            if f == "problem_statement":
                work_item.problem_statement = a
            elif f == "acceptance_criteria":
                work_item.acceptance_criteria.append(a)
            elif f == "scope":
                work_item.scope.append(a)
            elif f == "out_of_scope":
                work_item.out_of_scope.append(a)
            elif f == "constraints":
                work_item.constraints.append(a)
            elif f == "technical_hints":
                work_item.technical_hints.append(a)
        work_item.open_questions = []
        work_item.locked = True
        logger.info(
            f"[NORM {self._run_id}] normalization confirmed by user — "
            f"{n} answers merged, work_item locked"
        )
        return work_item


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    """Extract and parse JSON from LLM text, tolerating markdown fences."""
    if not text:
        return {}
    t = text.strip()
    import re as _re
    m = _re.search(r"\{[\s\S]+\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    try:
        return json.loads(t)
    except Exception:
        return {}
