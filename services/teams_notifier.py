# SPDX-License-Identifier: Apache-2.0
# ============================================================
# TEAMS NOTIFIER — proactive push notifications to Teams
#
# Called by SDLC pipeline, webhooks, etc. to send status
# updates back into the Teams conversation/channel where
# the work was triggered.
#
# Usage:
#   from services.teams_notifier import teams_notifier
#   teams_notifier.notify_pr_created(thread_id, pr_url, run_id)
#   teams_notifier.notify_approval_required(thread_id, run_id, state, summary)
# ============================================================

from core.logger import logger


def _get_conv(thread_id: str):
    """
    Resolve (conv_id, service_url) for a AiNxt thread_id.
    Returns (None, None) if no Teams conversation is mapped.
    """
    try:
        from services.teams_adapter import get_conversation_for_thread, get_service_url
        conv_id = get_conversation_for_thread(thread_id)
        if not conv_id:
            return None, None
        service_url = get_service_url(conv_id) or ""
        return conv_id, service_url
    except Exception as e:
        logger.warning(f"TeamsNotifier: lookup failed → {e}")
        return None, None


class TeamsNotifier:
    """
    High-level notification helpers for the AiNxt SDLC lifecycle.
    All methods are fire-and-forget (silent on error).
    """

    # ── PR created ────────────────────────────────────────────────────────────

    def notify_pr_created(self, thread_id: str, pr_url: str, run_id: str,
                          branch: str = ""):
        """
        Send a Teams message when a PR is created by the pipeline.

        Args:
            thread_id: AiNxt thread that triggered the pipeline.
            pr_url:    GitLab MR URL.
            run_id:    SDLC run ID.
            branch:    Feature/fix branch name.
        """
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_message
            branch_info = f" on `{branch}`" if branch else ""
            send_message(
                service_url, conv_id,
                f"🔀 **PR Created**{branch_info}\n"
                f"Run `{run_id[:8]}` → [Open PR]({pr_url})\n"
                f"Awaiting review in GitLab.",
            )
        except Exception as e:
            logger.warning(f"TeamsNotifier.notify_pr_created → {e}")

    # ── Bug fixed ─────────────────────────────────────────────────────────────

    def notify_bug_fixed(self, thread_id: str, jira_key: str, run_id: str,
                         pr_url: str = ""):
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_message
            pr_part = f"\n[View PR]({pr_url})" if pr_url else ""
            send_message(
                service_url, conv_id,
                f"🐛 **Bug Fixed** — `{jira_key}`\n"
                f"Run `{run_id[:8]}` completed successfully.{pr_part}",
            )
        except Exception as e:
            logger.warning(f"TeamsNotifier.notify_bug_fixed → {e}")

    # ── Workflow completed ────────────────────────────────────────────────────

    def notify_workflow_completed(self, thread_id: str, workflow_name: str,
                                  run_id: str = "", status: str = "completed"):
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_message
            icon = "✅" if status == "completed" else "❌"
            send_message(
                service_url, conv_id,
                f"{icon} **Workflow {status.capitalize()}** — `{workflow_name}`"
                + (f"\nRun `{run_id[:8]}`" if run_id else ""),
            )
        except Exception as e:
            logger.warning(f"TeamsNotifier.notify_workflow_completed → {e}")

    # ── Approval required (HITL Adaptive Card) ────────────────────────────────

    def notify_approval_required(self, thread_id: str, run_id: str,
                                 state: str, summary: str = ""):
        """
        Push an Adaptive Card with Approve / Reject buttons to Teams when
        the SDLC pipeline reaches a HITL gate.
        """
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_adaptive_card, build_hitl_card
            card = build_hitl_card(run_id, state, summary)
            send_adaptive_card(service_url, conv_id, card)
        except Exception as e:
            logger.warning(f"TeamsNotifier.notify_approval_required → {e}")

    # ── Pipeline failed ───────────────────────────────────────────────────────

    def notify_pipeline_failed(self, thread_id: str, run_id: str, error: str = ""):
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_message
            send_message(
                service_url, conv_id,
                f"❌ **Pipeline Failed** — Run `{run_id[:8]}`\n"
                + (f"Error: {error[:300]}" if error else "Check the SDLC tab for details."),
            )
        except Exception as e:
            logger.warning(f"TeamsNotifier.notify_pipeline_failed → {e}")

    # ── Generic message ───────────────────────────────────────────────────────

    def send(self, thread_id: str, text: str):
        """Send any freeform message to the Teams conversation for a thread."""
        conv_id, service_url = _get_conv(thread_id)
        if not conv_id:
            return
        try:
            from integrations.teams_client import send_message
            send_message(service_url, conv_id, text)
        except Exception as e:
            logger.warning(f"TeamsNotifier.send → {e}")


# ── Singleton ─────────────────────────────────────────────────────────────────

teams_notifier = TeamsNotifier()
