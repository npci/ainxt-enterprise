# SPDX-License-Identifier: MIT
# ============================================================
# WEBHOOKS ROUTER — /webhooks
# Receives Jira and GitLab webhook events and fires SDLC pipelines
# via the job queue (rq workers).
#
# Jira:   POST /webhooks/jira
# GitLab: POST /webhooks/gitlab
#
# GitLab events handled (X-Gitlab-Event header):
#   Push Hook                    → re-index repo on default branch push
#   Merge Request Hook (opened/reopened/updated) → MR review pipeline
#   Merge Request Hook (merged)  → re-index repo
#   Note Hook (MR note)          → address MR comments / merge on approval
# ============================================================

import hashlib
import hmac
import os
import re
import threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from core.logger import logger

router = APIRouter(prefix="/webhooks", tags=["sdlc"])

# Optional secrets — if set, webhook payloads are verified
_JIRA_WEBHOOK_SECRET   = os.getenv("JIRA_WEBHOOK_SECRET", "")
_GITLAB_WEBHOOK_SECRET = os.getenv("GITLAB_WEBHOOK_SECRET", "")
_GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
# SEC-03: shared secret for event-driven workflow trigger endpoint
_WORKFLOW_TRIGGER_SECRET = os.getenv("WORKFLOW_TRIGGER_SECRET", "")

# Regex to extract embedded run_id from PR body.
# Pattern matches both the default prefix (ainxt_run_id) and a legacy prefix
# (<legacy>_run_id) so existing legacy PRs continue to be tracked after migration.
# The prefix is driven by SDLC_RUN_ID_PREFIX in core/config.py.
from core.config import SDLC_BRANCH_PREFIX as _SDLC_BRANCH_PREFIX
from core.config import SDLC_RUN_ID_PREFIX as _SDLC_RUN_ID_PREFIX
_RUN_ID_RE = re.compile(
    r"<!--\s*" + re.escape(_SDLC_RUN_ID_PREFIX) + r":\s*([a-f0-9\-]{36})\s*-->"
)


# ── Helpers ───────────────────────────────────────────────────

def _adf_to_text(node) -> str:
    """Recursively extract plain text from Jira's Atlassian Document Format (ADF)."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = []
        for child in node.get("content", []):
            parts.append(_adf_to_text(child))
        return "\n".join(p for p in parts if p)
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    return ""


import re as _re
_REPO_RE = _re.compile(
    r'(?i)repo\s*:\s*'
    r'(?:https?://[^/]+/)?'                  # optional full URL prefix (any GitLab host)
    r'([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)'  # namespace/project
)

def _extract_repo(desc: str) -> str:
    """
    Extract repo from description text in any of these forms:
      repo: ainxt/payment-service
      repo: https://<YOUR_GITLAB_URL>/team/payment-service
      use the repo: https://...   (embedded in a sentence)
    Always returns namespace/project format, never a full URL.
    """
    m = _REPO_RE.search(desc)
    return m.group(1).rstrip("/") if m else ""


# ── Jira Event Model ──────────────────────────────────────────

class JiraIssue(BaseModel):
    key:        str
    summary:    str
    description: Optional[str] = ""
    issue_type:  Optional[str] = "Story"    # Story | Bug | Task
    priority:    Optional[str] = "Medium"
    repo:        Optional[str] = ""         # linked repo name
    assignee:    Optional[str] = ""


# ── Helper: GitLab token verification ─────────────────────────

def _verify_gitlab_token(token_header: Optional[str], secret: str) -> bool:
    """GitLab uses a simple string comparison (X-Gitlab-Token header)."""
    if not secret:
        return True   # skip if no secret configured
    if not token_header:
        return False
    return hmac.compare_digest(secret, token_header)


def _verify_github_signature(body: bytes, sig_header: Optional[str], secret: str) -> bool:
    """GitHub uses HMAC-SHA256: X-Hub-Signature-256: sha256=<hex>"""
    if not secret:
        return True   # skip if no secret configured
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    import hashlib as _hs
    expected = "sha256=" + hmac.new(secret.encode(), body, _hs.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


# ── Extract run_id from PR body ───────────────────────────────

def _extract_run_id(pr_body: str) -> Optional[str]:
    """Pull the embedded run_id from our PR description HTML comment."""
    if not pr_body:
        return None
    m = _RUN_ID_RE.search(pr_body)
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────
# POST /webhooks/jira
# ─────────────────────────────────────────────────────────────

@router.post("/jira")
async def jira_webhook(request: Request):
    """
    Jira webhook receiver.

    Jira sends a JSON payload on issue events (created/updated).
    We extract the issue and classify:
      - Bug / Incident → run_bug_pipeline
      - Story / Task / Feature → run_feature_pipeline

    Configure in Jira:
      URL: https://your-platform/webhooks/jira
      Events: Issue Created, Issue Updated (optional)

    SEC-04: Verifies X-Jira-Webhook-Secret header against JIRA_WEBHOOK_SECRET env var.
    """
    # SEC-04: verify Jira webhook secret when configured
    if _JIRA_WEBHOOK_SECRET:
        provided = request.headers.get("X-Jira-Webhook-Secret", "")
        if not provided:
            raise HTTPException(status_code=401, detail="X-Jira-Webhook-Secret header is required")
        if not hmac.compare_digest(_JIRA_WEBHOOK_SECRET, provided):
            raise HTTPException(status_code=403, detail="Invalid Jira webhook secret")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Detect Jira native format ──────────────────────────────
    if "issue" in payload:
        issue_raw   = payload["issue"]
        fields      = issue_raw.get("fields", {})
        issue_type  = (fields.get("issuetype") or {}).get("name", "Story")
        priority    = (fields.get("priority")   or {}).get("name", "Medium")
        assignee    = ((fields.get("assignee")  or {}) or {}).get("displayName", "")
        # Jira Cloud sends description as ADF (Atlassian Document Format — a nested dict).
        # Extract plain text recursively so repo: extraction works regardless of format.
        raw_desc = fields.get("description", "") or ""
        desc = _adf_to_text(raw_desc) if isinstance(raw_desc, dict) else (raw_desc or "")
        repo = _extract_repo(desc)

        issue_dict = {
            "key":         issue_raw.get("key", ""),
            "summary":     fields.get("summary", ""),
            "description": desc,
            "issue_type":  issue_type,
            "priority":    priority,
            "repo":        repo,
            "assignee":    assignee,
        }
        event = payload.get("webhookEvent", "")

        # Only trigger SDLC on new issue creation — silently drop everything else
        if event and event != "jira:issue_created":
            return {"accepted": True}

        logger.info(f"webhooks/jira: event={event} key={issue_dict['key']} type={issue_type}")
    else:
        issue_dict = payload

    if not issue_dict.get("key"):
        raise HTTPException(status_code=400, detail="Missing issue key")

    # ── Validate required fields BEFORE triggering any pipeline ───────────
    # Return HTTP 200 (not 4xx) so Jira does not retry the webhook.
    _missing = []
    if not issue_dict.get("summary", "").strip():
        _missing.append("summary")
    if not issue_dict.get("description", "").strip():
        _missing.append("description")
    if not issue_dict.get("repo", "").strip():
        _missing.append("repo (add a line 'repo: owner/repo-name' to the ticket description)")

    if _missing:
        _key = issue_dict.get("key", "")
        _reason = f"Missing required fields: {', '.join(_missing)}"
        logger.warning(f"webhooks/jira: REJECTED {_key} — {_reason}")

        # Post a comment on the Jira ticket so the creator knows what to fix
        try:
            from tools.jira_tools import jira_add_comment
            jira_add_comment(
                issue_key=_key,
                comment=(
                    f"⚠️ *AiNxt SDLC Pipeline — Not Triggered*\n\n"
                    f"The pipeline was not started because the following required fields are missing:\n"
                    + "\n".join(f"• {m}" for m in _missing)
                    + "\n\nPlease update the ticket with:\n"
                    "• A clear *description* of the feature/bug\n"
                    "• A *repo* line in the description — e.g. `repo: your-org/your-repo`\n\n"
                    "Once updated, re-trigger by transitioning the issue or contacting your platform admin."
                ),
            )
        except Exception as _je:
            logger.warning(f"webhooks/jira: could not post Jira comment — {_je}")

        return {
            "accepted":  False,
            "jira_key":  _key,
            "reason":    _reason,
            "message":   "Pipeline not triggered — update the ticket and re-trigger",
        }

    # ── BRD→FSD pipeline: Epic with label "BRD" ───────────────
    # Jira native format carries labels in fields.labels (list of strings).
    # Direct-POST format (our own JiraIssue model) does not include labels,
    # so we also check the raw payload's fields when available.
    _raw_labels: list = []
    if "issue" in payload:
        _raw_labels = payload["issue"].get("fields", {}).get("labels") or []
    elif isinstance(issue_dict.get("labels"), list):
        _raw_labels = issue_dict["labels"]

    if "BRD" in _raw_labels and issue_dict.get("issue_type", "").lower() in (
        "epic", "story", "task", ""
    ):
        logger.info(
            f"webhooks/jira: BRD label detected on {issue_dict['key']} — "
            "enqueuing BRD→FSD pipeline"
        )
        from core.job_queue import enqueue_sdlc_job, Q_SDLC

        brd_payload = {
            "epic_key":         issue_dict["key"],
            "key":              issue_dict["key"],   # for dedup guard in enqueue_sdlc_job
            "summary":          issue_dict.get("summary", ""),
            "description":      issue_dict.get("description", ""),
            "confluence_space": "",      # uses CONFLUENCE_SPACE_KEY env default
            "jira_project":     issue_dict["key"].split("-")[0] if "-" in issue_dict["key"] else "",
            "assignee":         issue_dict.get("assignee", ""),
        }
        try:
            brd_job_id = enqueue_sdlc_job(
                "agents.brd_fsd_pipeline.run_brd_fsd_pipeline_job",
                brd_payload,
                queue_name=Q_SDLC,
            )
        except RuntimeError as _brd_err:
            logger.warning(f"webhooks/jira: BRD pipeline enqueue failed: {_brd_err}")
            return {"accepted": False, "jira_key": issue_dict["key"], "reason": str(_brd_err)}

        logger.info(
            f"webhooks/jira: BRD→FSD pipeline enqueued for {issue_dict['key']} "
            f"job={brd_job_id}"
        )

        # Inbox notification
        try:
            from store.inbox_store import publish_inbox_item
            publish_inbox_item(
                user_id="platform",
                type="brd_fsd_started",
                title=f"[BRD→FSD] Pipeline started — {issue_dict['key']}",
                body=(
                    f"BRD→FSD pipeline triggered for Epic "
                    f"**{issue_dict['key']}**: {issue_dict.get('summary', '')}"
                ),
                source_id=str(brd_job_id),
                metadata={
                    "epic_key": issue_dict["key"],
                    "pipeline": "brd_fsd",
                    "stage":    "triggered",
                    "job_id":   str(brd_job_id),
                },
            )
        except Exception:
            pass

        return {
            "accepted":  True,
            "pipeline":  "brd_fsd",
            "jira_key":  issue_dict["key"],
            "job_id":    brd_job_id,
            "message":   f"BRD→FSD pipeline enqueued (job {brd_job_id})",
        }

    # ── Governance trigger flag (STEP 8, 2026-07-17) ───────────
    # Opt-in PART 2 EA/IS/DPDP gate. Sourced from env SDLC_GOVERNANCE_ON_WEBHOOK
    # (default false) OR a Jira "governance" label on the ticket. Native Jira
    # webhook format carries labels at payload["issue"]["fields"]["labels"]; the
    # direct-POST format (else-branch above, issue_dict = payload) has no label
    # field on our JiraIssue model, so a caller using that path should set
    # run_governance_review directly on the JSON body instead — issue_dict.get(...)
    # below preserves whatever the caller already passed.
    _gov_env_on = os.getenv("SDLC_GOVERNANCE_ON_WEBHOOK", "false").strip().lower() in ("1", "true", "yes")
    _gov_jira_labels = payload.get("issue", {}).get("fields", {}).get("labels", []) if "issue" in payload else []
    _gov_label_hit = any(str(l).strip().lower() == "governance" for l in (_gov_jira_labels or []))
    issue_dict["run_governance_review"] = bool(issue_dict.get("run_governance_review", False)) or _gov_env_on or _gov_label_hit
    if issue_dict["run_governance_review"]:
        logger.info(
            f"[SDLC-GOV] webhooks/jira: governance review opted-in for {issue_dict.get('key', '')} "
            f"(env={_gov_env_on} label={_gov_label_hit})"
        )

    # ── Route by issue type ────────────────────────────────────
    from core.job_queue import enqueue_sdlc_job
    issue_type = issue_dict.get("issue_type", "Story").lower()
    pipeline   = "bug" if issue_type in ("bug", "incident", "defect", "hotfix") else "feature"
    fn_name    = (
        "workers.sdlc_worker.run_bug_pipeline_job"
        if pipeline == "bug"
        else "workers.sdlc_worker.run_feature_pipeline_job"
    )

    try:
        job_id = enqueue_sdlc_job(fn_name, issue_dict)
    except RuntimeError as _rate_err:
        # Per-reporter rate limit hit — tell Jira 200 OK (no retry) but log + comment
        _key = issue_dict.get("key", "")
        logger.warning(f"webhooks/jira: SDLC rate-limit for {_key}: {_rate_err}")
        try:
            from tools.jira_tools import jira_add_comment
            jira_add_comment(
                issue_key=_key,
                comment=(
                    f"⚠️ *AiNxt SDLC Pipeline — Rate Limited*\n\n"
                    f"Too many active SDLC pipelines from the same reporter. "
                    f"Please wait for your existing pipelines to complete before creating new tickets.\n\n"
                    f"_Reason: {_rate_err}_"
                ),
            )
        except Exception:
            pass
        return {"accepted": False, "jira_key": _key, "reason": str(_rate_err)}

    logger.info(f"webhooks/jira: enqueued {pipeline} pipeline for {issue_dict['key']} job={job_id}")

    # Notify inbox so engineers see the trigger regardless of UI source
    try:
        from store.inbox_store import publish_inbox_item
        publish_inbox_item(
            user_id="platform",
            type="sdlc_started",
            title=f"[SDLC/{pipeline.upper()}] Pipeline started — {issue_dict['key']}",
            body=(
                f"{pipeline.capitalize()} pipeline triggered via Jira webhook for "
                f"**{issue_dict['key']}**: {issue_dict.get('summary', '')}\n"
                f"Repo: {issue_dict.get('repo', 'auto-detect')} | "
                f"Priority: {issue_dict.get('priority', 'Medium')}"
            ),
            source_id=str(job_id),
            metadata={"jira_key": issue_dict["key"], "pipeline": pipeline, "stage": "triggered", "job_id": str(job_id)},
        )
    except Exception:
        pass

    return {
        "accepted":  True,
        "pipeline":  pipeline,
        "jira_key":  issue_dict["key"],
        "job_id":    job_id,
        "message":   f"{pipeline.capitalize()} pipeline enqueued (job {job_id})",
    }


# ─────────────────────────────────────────────────────────────
# POST /webhooks/gitlab
# ─────────────────────────────────────────────────────────────

@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_event: Optional[str]  = Header(None),
    x_gitlab_token: Optional[str]  = Header(None),
):
    """
    GitLab webhook receiver.

    Listens for:
      - Push Hook (default branch)           → re-index repo
      - Merge Request Hook (opened/updated)  → run_pr_review_pipeline
      - Merge Request Hook (merged)          → re-index repo
      - Note Hook (MR note with approval)    → address MR comments / merge

    Configure in GitLab (Project → Settings → Webhooks):
      URL:    https://your-platform/webhooks/gitlab
      Token:  GITLAB_WEBHOOK_SECRET env var
      Events: Push events, Merge request events, Comments
    """
    if _GITLAB_WEBHOOK_SECRET:
        if not _verify_gitlab_token(x_gitlab_token, _GITLAB_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event      = x_gitlab_event or ""
    object_kind = payload.get("object_kind", "")
    repo        = (payload.get("project") or payload.get("repository") or {}).get("path_with_namespace", "")

    logger.info(f"webhooks/gitlab: event={event} kind={object_kind} repo={repo}")

    # ── Push Hook → re-index default branch ─────────────────────
    if event == "Push Hook" or object_kind == "push":
        ref            = payload.get("ref", "")              # "refs/heads/main"
        default_branch = payload.get("project", {}).get("default_branch", "main")
        pushed_branch  = ref.replace("refs/heads/", "")
        clone_url      = (payload.get("project") or {}).get("http_url", "")
        repo_name      = (repo or "").replace("/", "_").lower()

        if pushed_branch == default_branch and clone_url and repo_name:
            try:
                from core.job_queue import enqueue_index_job
                index_job_id = enqueue_index_job(
                    "workers.index_worker.run_index_job",
                    {
                        "repo_name":    repo_name,
                        "repo_path":    clone_url,
                        "triggered_by": f"gitlab_push/{pushed_branch}",
                        "drop_index":   False,
                    },
                )
                logger.info(
                    f"webhooks/gitlab: push to {repo}@{pushed_branch} → "
                    f"re-index job {index_job_id}"
                )
                return {
                    "accepted":     True,
                    "event":        event,
                    "repo":         repo,
                    "branch":       pushed_branch,
                    "index_job_id": index_job_id,
                    "message":      f"Re-index job enqueued for {repo_name} (job {index_job_id})",
                }
            except Exception as _ie:
                logger.error(f"webhooks/gitlab: failed to enqueue re-index job: {_ie}")

        return {"accepted": True, "event": event, "message": "Push event — non-default branch ignored"}

    # ── Merge Request Hook ───────────────────────────────────────
    if event == "Merge Request Hook" or object_kind == "merge_request":
        attrs       = payload.get("object_attributes", {})
        mr_action   = attrs.get("action", "")          # open | reopen | update | merge | close
        mr_iid      = attrs.get("iid")
        mr_title    = attrs.get("title", "")
        mr_body     = attrs.get("description", "") or ""
        head_branch = attrs.get("source_branch", "")
        base_branch = attrs.get("target_branch", "main")
        mr_author   = (payload.get("user") or {}).get("username", "")
        mr_url      = attrs.get("url", "")
        clone_url   = (payload.get("project") or {}).get("http_url", "")

        # MR merged → re-index
        if mr_action == "merge":
            repo_name = (repo or "").replace("/", "_").lower()
            if clone_url and repo_name:
                try:
                    from core.job_queue import enqueue_index_job
                    index_job_id = enqueue_index_job(
                        "workers.index_worker.run_index_job",
                        {
                            "repo_name":    repo_name,
                            "repo_path":    clone_url,
                            "triggered_by": f"gitlab_mr_merge/!{mr_iid}",
                            "drop_index":   False,
                        },
                    )
                    logger.info(
                        f"webhooks/gitlab: MR !{mr_iid} merged to {repo} "
                        f"→ re-index job {index_job_id}"
                    )
                except Exception as _ie:
                    logger.error(f"webhooks/gitlab: MR merge re-index failed: {_ie}")
            return {"accepted": True, "event": event, "action": mr_action, "merged": True}

        # MR opened / reopened / updated → PR review pipeline
        if mr_action in ("open", "reopen", "update"):
            # Skip MRs created by our own coding agent
            if head_branch.startswith(f"{_SDLC_BRANCH_PREFIX}/"):
                logger.info(f"webhooks/gitlab: skipping MR review for AiNxt branch '{head_branch}'")
                return {
                    "accepted": True,
                    "event":    event,
                    "action":   mr_action,
                    "message":  f"AiNxt branch '{head_branch}' — MR review pipeline skipped",
                }

            # Extract Jira key from MR title/body
            _jira_re  = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
            _jira_key = ""
            for _src in (mr_title, mr_body):
                _m = _jira_re.search(_src)
                if _m:
                    _jira_key = _m.group(1)
                    break

            from store.sdlc_store import create_run as _cr
            _run = _cr(
                run_type="pr_review",
                jira_key=_jira_key,
                jira_summary=mr_title,
                repo=repo,
                triggered_by=f"gitlab_webhook/{mr_author}",
            )
            run_id = _run["id"]
            logger.info(f"webhooks/gitlab: pre-created MR review run {run_id} for MR !{mr_iid}")

            from core.job_queue import enqueue_sdlc_job, enqueue_security_scan_job
            mr_dict = {
                "_run_id":  run_id,
                "number":   mr_iid,
                "title":    mr_title,
                "body":     mr_body,
                "repo":     repo,
                "branch":   head_branch,
                "base":     base_branch,
                "author":   mr_author,
                "url":      mr_url,
                "diff_url": "",
            }
            job_id = enqueue_sdlc_job("workers.sdlc_worker.run_pr_review_pipeline_job", mr_dict)

            sec_job_id = None
            try:
                sec_dict = {
                    "repo":      repo,
                    "branch":    head_branch,
                    "number":    mr_iid,
                    "clone_url": clone_url,
                    "run_id":    run_id,
                }
                sec_job_id = enqueue_security_scan_job(sec_dict)
                logger.info(f"webhooks/gitlab: security scan enqueued job={sec_job_id} mr=!{mr_iid}")
            except Exception as _se:
                logger.warning(f"webhooks/gitlab: security scan enqueue failed (non-blocking): {_se}")

            # Inline fallback thread
            _mr_dict_copy  = mr_dict.copy()
            _inline_run_id = run_id

            def _run_inline():
                import time as _time
                _time.sleep(1)
                try:
                    from core.config import RDB_QUEUE
                    from core.kv import get_kv
                    _rc = get_kv(RDB_QUEUE, decode_responses=True)
                    acquired = _rc.set(f"pr_review:running:{_inline_run_id}", "1", nx=True, ex=30)
                except Exception:
                    acquired = True

                if not acquired:
                    logger.info(f"webhooks/gitlab: RQ worker claimed MR review run {_inline_run_id} — thread skipping")
                    return

                try:
                    from store.sdlc_store import get_run as _gr
                    _run_state = (_gr(_inline_run_id) or {}).get("state", "CREATED")
                    if _run_state != "CREATED":
                        return
                    logger.info(f"webhooks/gitlab: inline thread executing MR review for run {_inline_run_id}")
                    from agents.sdlc_pipeline import run_pr_review_pipeline
                    run_pr_review_pipeline(_mr_dict_copy, _inline_run_id)
                except Exception as _ex:
                    logger.error(f"webhooks/gitlab: inline MR review thread failed → {_ex}")

            threading.Thread(target=_run_inline, daemon=True).start()

            try:
                from store.inbox_store import publish_inbox_item
                publish_inbox_item(
                    user_id="platform",
                    type="sdlc_started",
                    title=f"[SDLC/MR-REVIEW] Review started — MR !{mr_iid}",
                    body=(
                        f"MR review pipeline triggered via GitLab webhook for "
                        f"**MR !{mr_iid}** in {repo}\n"
                        f"Title: {mr_title} | Author: {mr_author}"
                    ),
                    source_id=run_id,
                    metadata={"mr_number": mr_iid, "repo": repo, "pipeline": "pr_review",
                              "stage": "triggered", "job_id": str(job_id), "run_id": run_id},
                )
            except Exception:
                pass

            return {
                "accepted":        True,
                "event":           event,
                "action":          mr_action,
                "mr":              mr_iid,
                "run_id":          run_id,
                "job_id":          job_id,
                "sec_scan_job_id": sec_job_id,
                "message":         f"MR review + security scan enqueued (run {run_id}, job {job_id})",
            }

        return {"accepted": True, "event": event, "action": mr_action, "message": "MR event ignored"}

    # ── Note Hook (reviewer comment / approval on MR) ────────────
    if event == "Note Hook" or object_kind == "note":
        note      = payload.get("object_attributes", {})
        noteable  = note.get("noteable_type", "")
        if noteable != "MergeRequest":
            return {"accepted": True, "event": event, "message": "Non-MR note ignored"}

        mr_iid    = (payload.get("merge_request") or {}).get("iid")
        mr_body   = (payload.get("merge_request") or {}).get("description", "") or ""
        reviewer  = (payload.get("user") or {}).get("username", "unknown")
        note_body = note.get("note", "").lower()

        run_id = _extract_run_id(mr_body)
        if not run_id:
            logger.info(f"webhooks/gitlab: MR !{mr_iid} note has no {_SDLC_RUN_ID_PREFIX} — ignored")
            return {"accepted": True, "event": event, "message": "No run_id — ignored"}

        from core.job_queue import enqueue_pr_comments_job, enqueue_merge_pr_job

        # Detect approval keywords in note body (GitLab doesn't have a separate review event)
        if any(kw in note_body for kw in ("approved", "lgtm", ":+1:", "✅ approve")):
            logger.info(f"webhooks/gitlab: MR !{mr_iid} approved by {reviewer} run={run_id}")
            try:
                from store.sdlc_store import update_run_state, add_run_event
                update_run_state(run_id, "MERGE_READY",
                                 pr_number=mr_iid,
                                 context_patch={"reviewer": reviewer})
                add_run_event(run_id,
                              from_state="AWAITING_RE_REVIEW",
                              to_state="MERGE_READY",
                              actor=reviewer,
                              output=f"Reviewer {reviewer} approved — auto-merging",
                              data={"mr_number": mr_iid, "reviewer": reviewer})
            except Exception as ex:
                logger.warning(f"webhooks/gitlab: state update failed → {ex}")

            job_id = enqueue_merge_pr_job(run_id)
            return {
                "accepted":     True,
                "event":        event,
                "review_state": "approved",
                "run_id":       run_id,
                "job_id":       job_id,
                "message":      f"Merge job enqueued (job {job_id})",
            }

        if any(kw in note_body for kw in ("changes requested", "needs changes", "request changes")):
            logger.info(f"webhooks/gitlab: MR !{mr_iid} changes_requested by {reviewer} run={run_id}")
            try:
                from store.sdlc_store import update_run_state, add_run_event
                update_run_state(run_id, "PR_REVIEW_COMMENTS_RECEIVED",
                                 pr_number=mr_iid,
                                 context_patch={"reviewer": reviewer})
                add_run_event(run_id,
                              from_state="AWAITING_PR_APPROVAL",
                              to_state="PR_REVIEW_COMMENTS_RECEIVED",
                              actor=reviewer,
                              output=f"Reviewer {reviewer} requested changes",
                              data={"mr_number": mr_iid, "reviewer": reviewer})
            except Exception as ex:
                logger.warning(f"webhooks/gitlab: state update failed → {ex}")

            job_id = enqueue_pr_comments_job(run_id)
            return {
                "accepted":     True,
                "event":        event,
                "review_state": "changes_requested",
                "run_id":       run_id,
                "job_id":       job_id,
                "message":      f"MR comment addressing job enqueued (job {job_id})",
            }

        return {"accepted": True, "event": event, "message": "Note recorded — no action taken"}

    # All other events — acknowledge and skip
    return {"accepted": True, "event": event, "object_kind": object_kind, "message": "Event ignored"}


# ── P11: Event-driven workflow trigger ───────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# POST /webhooks/github
# ─────────────────────────────────────────────────────────────

@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: Optional[str]        = Header(None),
    x_hub_signature_256: Optional[str]   = Header(None),
):
    """
    GitHub webhook receiver.

    Listens for:
      - push (default branch)                      → re-index repo
      - pull_request (opened/reopened/synchronize) → run_pr_review_pipeline
      - pull_request (closed + merged)             → re-index repo
      - pull_request_review (approved/changes_requested) → merge or address comments

    Configure in GitHub (Repo → Settings → Webhooks):
      Payload URL:  https://your-platform/webhooks/github
      Content type: application/json
      Secret:       GITHUB_WEBHOOK_SECRET env var
      Events:       Push, Pull requests, Pull request reviews
    """
    # Read raw body first — needed for HMAC-SHA256 signature verification
    body = await request.body()
    if _GITHUB_WEBHOOK_SECRET:
        if not _verify_github_signature(body, x_hub_signature_256, _GITHUB_WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        import json as _json
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event = x_github_event or ""
    repo  = (payload.get("repository") or {}).get("full_name", "")  # "owner/repo"

    logger.info(f"webhooks/github: event={event} repo={repo}")

    # ── Push → re-index default branch ──────────────────────────
    if event == "push":
        ref            = payload.get("ref", "")           # "refs/heads/main"
        default_branch = (payload.get("repository") or {}).get("default_branch", "main")
        pushed_branch  = ref.replace("refs/heads/", "")
        clone_url      = (payload.get("repository") or {}).get("clone_url", "")
        repo_name      = (repo or "").replace("/", "_").lower()

        if pushed_branch == default_branch and clone_url and repo_name:
            try:
                from core.job_queue import enqueue_index_job
                index_job_id = enqueue_index_job(
                    "workers.index_worker.run_index_job",
                    {
                        "repo_name":    repo_name,
                        "repo_path":    clone_url,
                        "triggered_by": f"github_push/{pushed_branch}",
                        "drop_index":   False,
                    },
                )
                logger.info(
                    f"webhooks/github: push to {repo}@{pushed_branch} → "
                    f"re-index job {index_job_id}"
                )
                return {
                    "accepted":     True,
                    "event":        event,
                    "repo":         repo,
                    "branch":       pushed_branch,
                    "index_job_id": index_job_id,
                    "message":      f"Re-index job enqueued for {repo_name} (job {index_job_id})",
                }
            except Exception as _ie:
                logger.error(f"webhooks/github: failed to enqueue re-index job: {_ie}")

        return {"accepted": True, "event": event, "message": "Push event — non-default branch ignored"}

    # ── Pull Request ─────────────────────────────────────────────
    if event == "pull_request":
        pr          = payload.get("pull_request") or {}
        action      = payload.get("action", "")       # opened/reopened/synchronize/closed
        pr_number   = pr.get("number")
        pr_title    = pr.get("title", "")
        pr_body     = pr.get("body", "") or ""
        head_branch = (pr.get("head") or {}).get("ref", "")
        base_branch = (pr.get("base") or {}).get("ref", "main")
        pr_author   = (pr.get("user") or {}).get("login", "")
        pr_url      = pr.get("html_url", "")
        clone_url   = (payload.get("repository") or {}).get("clone_url", "")
        merged      = pr.get("merged", False)

        # PR merged → re-index
        if action == "closed" and merged:
            repo_name = (repo or "").replace("/", "_").lower()
            if clone_url and repo_name:
                try:
                    from core.job_queue import enqueue_index_job
                    index_job_id = enqueue_index_job(
                        "workers.index_worker.run_index_job",
                        {
                            "repo_name":    repo_name,
                            "repo_path":    clone_url,
                            "triggered_by": f"github_pr_merge/#{pr_number}",
                            "drop_index":   False,
                        },
                    )
                    logger.info(
                        f"webhooks/github: PR #{pr_number} merged to {repo} "
                        f"→ re-index job {index_job_id}"
                    )
                except Exception as _ie:
                    logger.error(f"webhooks/github: PR merge re-index failed: {_ie}")
            return {"accepted": True, "event": event, "action": action, "merged": True}

        # PR opened/reopened/synchronize → PR review pipeline
        if action in ("opened", "reopened", "synchronize"):
            # Skip PRs created by our own coding agent
            if head_branch.startswith(f"{_SDLC_BRANCH_PREFIX}/"):
                logger.info(f"webhooks/github: skipping PR review for AiNxt branch '{head_branch}'")
                return {
                    "accepted": True,
                    "event":    event,
                    "action":   action,
                    "message":  f"AiNxt branch '{head_branch}' — PR review pipeline skipped",
                }

            # Extract ticket key (Jira/GitHub Issues) from PR title/body
            _ticket_re  = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
            _ticket_key = ""
            for _src in (pr_title, pr_body):
                _m = _ticket_re.search(_src)
                if _m:
                    _ticket_key = _m.group(1)
                    break

            from store.sdlc_store import create_run as _cr
            _run = _cr(
                run_type="pr_review",
                jira_key=_ticket_key,
                jira_summary=pr_title,
                repo=repo,
                triggered_by=f"github_webhook/{pr_author}",
            )
            run_id = _run["id"]
            logger.info(f"webhooks/github: pre-created PR review run {run_id} for PR #{pr_number}")

            from core.job_queue import enqueue_sdlc_job, enqueue_security_scan_job
            mr_dict = {
                "_run_id":  run_id,
                "number":   pr_number,
                "title":    pr_title,
                "body":     pr_body,
                "repo":     repo,
                "branch":   head_branch,
                "base":     base_branch,
                "author":   pr_author,
                "url":      pr_url,
                "diff_url": "",
            }
            job_id = enqueue_sdlc_job("workers.sdlc_worker.run_pr_review_pipeline_job", mr_dict)

            sec_job_id = None
            try:
                sec_dict = {
                    "repo":      repo,
                    "branch":    head_branch,
                    "number":    pr_number,
                    "clone_url": clone_url,
                    "run_id":    run_id,
                }
                sec_job_id = enqueue_security_scan_job(sec_dict)
                logger.info(f"webhooks/github: security scan enqueued job={sec_job_id} pr=#{pr_number}")
            except Exception as _se:
                logger.warning(f"webhooks/github: security scan enqueue failed (non-blocking): {_se}")

            # Inline fallback thread (mirrors GitLab handler)
            _mr_dict_copy  = mr_dict.copy()
            _inline_run_id = run_id

            def _run_inline():
                import time as _time
                _time.sleep(1)
                try:
                    from core.config import RDB_QUEUE
                    from core.kv import get_kv
                    _rc = get_kv(RDB_QUEUE, decode_responses=True)
                    acquired = _rc.set(f"pr_review:running:{_inline_run_id}", "1", nx=True, ex=30)
                except Exception:
                    acquired = True

                if not acquired:
                    logger.info(f"webhooks/github: RQ worker claimed PR review run {_inline_run_id} — thread skipping")
                    return

                try:
                    from store.sdlc_store import get_run as _gr
                    _run_state = (_gr(_inline_run_id) or {}).get("state", "CREATED")
                    if _run_state != "CREATED":
                        return
                    logger.info(f"webhooks/github: inline thread executing PR review for run {_inline_run_id}")
                    from agents.sdlc_pipeline import run_pr_review_pipeline
                    run_pr_review_pipeline(_mr_dict_copy, _inline_run_id)
                except Exception as _ex:
                    logger.error(f"webhooks/github: inline PR review thread failed → {_ex}")

            threading.Thread(target=_run_inline, daemon=True).start()

            try:
                from store.inbox_store import publish_inbox_item
                publish_inbox_item(
                    user_id="platform",
                    type="sdlc_started",
                    title=f"[SDLC/PR-REVIEW] Review started — PR #{pr_number}",
                    body=(
                        f"PR review pipeline triggered via GitHub webhook for "
                        f"**PR #{pr_number}** in {repo}\n"
                        f"Title: {pr_title} | Author: {pr_author}"
                    ),
                    source_id=run_id,
                    metadata={"pr_number": pr_number, "repo": repo, "pipeline": "pr_review",
                              "stage": "triggered", "job_id": str(job_id), "run_id": run_id},
                )
            except Exception:
                pass

            return {
                "accepted":        True,
                "event":           event,
                "action":          action,
                "pr":              pr_number,
                "run_id":          run_id,
                "job_id":          job_id,
                "sec_scan_job_id": sec_job_id,
                "message":         f"PR review + security scan enqueued (run {run_id}, job {job_id})",
            }

        return {"accepted": True, "event": event, "action": action, "message": "PR event ignored"}

    # ── Pull Request Review ───────────────────────────────────────
    if event == "pull_request_review":
        review       = payload.get("review") or {}
        pr           = payload.get("pull_request") or {}
        pr_number    = pr.get("number")
        pr_body      = pr.get("body", "") or ""
        reviewer     = (review.get("user") or {}).get("login", "unknown")
        review_state = review.get("state", "").lower()   # "approved" / "changes_requested"

        run_id = _extract_run_id(pr_body)
        if not run_id:
            logger.info(f"webhooks/github: PR #{pr_number} review has no {_SDLC_RUN_ID_PREFIX} — ignored")
            return {"accepted": True, "event": event, "message": "No run_id — ignored"}

        from core.job_queue import enqueue_pr_comments_job, enqueue_merge_pr_job

        if review_state == "approved":
            logger.info(f"webhooks/github: PR #{pr_number} approved by {reviewer} run={run_id}")
            try:
                from store.sdlc_store import update_run_state, add_run_event
                update_run_state(run_id, "MERGE_READY",
                                 pr_number=pr_number,
                                 context_patch={"reviewer": reviewer})
                add_run_event(run_id,
                              from_state="AWAITING_RE_REVIEW",
                              to_state="MERGE_READY",
                              actor=reviewer,
                              output=f"Reviewer {reviewer} approved — auto-merging",
                              data={"pr_number": pr_number, "reviewer": reviewer})
            except Exception as ex:
                logger.warning(f"webhooks/github: state update failed → {ex}")

            job_id = enqueue_merge_pr_job(run_id)
            return {
                "accepted":     True,
                "event":        event,
                "review_state": "approved",
                "run_id":       run_id,
                "job_id":       job_id,
                "message":      f"Merge job enqueued (job {job_id})",
            }

        if review_state == "changes_requested":
            logger.info(f"webhooks/github: PR #{pr_number} changes_requested by {reviewer} run={run_id}")
            try:
                from store.sdlc_store import update_run_state, add_run_event
                update_run_state(run_id, "PR_REVIEW_COMMENTS_RECEIVED",
                                 pr_number=pr_number,
                                 context_patch={"reviewer": reviewer})
                add_run_event(run_id,
                              from_state="AWAITING_PR_APPROVAL",
                              to_state="PR_REVIEW_COMMENTS_RECEIVED",
                              actor=reviewer,
                              output=f"Reviewer {reviewer} requested changes",
                              data={"pr_number": pr_number, "reviewer": reviewer})
            except Exception as ex:
                logger.warning(f"webhooks/github: state update failed → {ex}")

            job_id = enqueue_pr_comments_job(run_id)
            return {
                "accepted":     True,
                "event":        event,
                "review_state": "changes_requested",
                "run_id":       run_id,
                "job_id":       job_id,
                "message":      f"PR comment addressing job enqueued (job {job_id})",
            }

        return {"accepted": True, "event": event, "message": "Review recorded — no action taken"}

    # All other events — acknowledge and skip
    return {"accepted": True, "event": event, "message": "Event ignored"}


# ── P11: Event-driven workflow trigger ───────────────────────────────────────

@router.post("/workflow-trigger/{event_name}", status_code=200)
async def trigger_workflow_by_event(
    event_name: str,
    request: Request,
    payload: Optional[dict] = None,
):
    """
    P11: Trigger all active scheduled_workflows that match event_trigger=event_name.

    Called by external systems (CI/CD, monitoring, etc.) to fire event-driven workflows.
    SEC-03: Requires X-Workflow-Secret header matching WORKFLOW_TRIGGER_SECRET env var.
    SEC-17: payload uses Optional[dict] = None (not a mutable default).

    Returns count of workflows enqueued.
    """
    # SEC-03: verify shared secret when configured
    if _WORKFLOW_TRIGGER_SECRET:
        provided = request.headers.get("X-Workflow-Secret", "")
        if not provided:
            raise HTTPException(status_code=401, detail="X-Workflow-Secret header is required")
        if not hmac.compare_digest(_WORKFLOW_TRIGGER_SECRET, provided):
            raise HTTPException(status_code=403, detail="Invalid workflow trigger secret")

    triggered = 0
    try:
        from db.database import SessionLocal
        from db.models import ScheduledWorkflow

        db = SessionLocal()
        try:
            matching = (
                db.query(ScheduledWorkflow)
                .filter(
                    ScheduledWorkflow.is_active == True,
                    ScheduledWorkflow.event_trigger == event_name,
                )
                .all()
            )

            for wf in matching:
                try:
                    from core.job_queue import enqueue_job
                    enqueue_job(
                        fn_name="workers.durable_workflow_worker.execute_durable_workflow",
                        payload={
                            "workflow_id":  str(wf.id),
                            "workflow_def": wf.workflow_def or {},
                            "triggered_by": f"event:{event_name}",
                            "event_payload": payload or {},
                        },
                        queue_name="workflows",
                        timeout=3600,
                    )
                    triggered += 1
                    logger.info(
                        f"webhooks/workflow-trigger: enqueued {wf.name} (id={wf.id}) "
                        f"for event={event_name}"
                    )
                except Exception as e:
                    logger.error(f"webhooks/workflow-trigger: failed to enqueue {wf.name}: {e}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"webhooks/workflow-trigger failed: {e}")

    return {
        "event_name": event_name,
        "triggered":  triggered,
        "status":     "ok",
    }
