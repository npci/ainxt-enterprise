# SPDX-License-Identifier: Apache-2.0
# ============================================================
# N8N AUTONOMOUS WORKFLOW BUILDER
# ============================================================
#
# Uses Claude to generate a valid n8n workflow JSON from a
# plain-English task description, then creates, validates,
# and activates it via the n8n REST API.
#
# Entry points:
#   autonomous_build(task_description) → {workflow_id, webhook_path, url}
#   n8n_autonomous_tool(state)         → state (for orchestrator compatibility)
# ============================================================

import json
import uuid

from core.logger import logger


# ============================================================
# WORKFLOW JSON GENERATION
# ============================================================

def generate_workflow_definition(task_description: str) -> dict:
    """
    Use Claude (via model_router) to generate a valid n8n workflow JSON
    from a plain-English task description.
    Raises ValueError if the LLM output cannot be parsed as JSON.
    """
    from models.model_router import model_router

    prompt = f"""You are an expert n8n automation engineer.

Generate a complete, valid n8n workflow JSON for the following task:

{task_description}

Requirements:
- Must start with a Webhook trigger node
- Must include at least one action node (HTTP Request, Code, etc.)
- Must return a response via a Respond to Webhook node
- Must follow n8n workflow JSON schema exactly
- Node IDs must be unique UUIDs
- Connections must reference valid node IDs

Return ONLY valid JSON — no markdown, no explanation, no code fences.
"""

    response = model_router.generate(prompt, model_hint="complex")

    # Strip markdown code fences if present
    text = (response or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text  = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"n8n_builder: LLM returned invalid JSON: {e}\n{text[:500]}")
        raise ValueError(f"Could not parse workflow JSON from LLM: {e}")


# ============================================================
# VALIDATION
# ============================================================

def validate_workflow(workflow_json: dict) -> tuple[bool, str]:
    """
    Basic structural validation before sending to n8n.
    Returns (valid: bool, reason: str).
    """
    nodes = workflow_json.get("nodes") or []
    if not nodes:
        return False, "workflow has no nodes"

    node_ids = {n.get("id") for n in nodes}
    has_webhook = any(
        str(n.get("type", "")).lower().endswith("webhook")
        for n in nodes
    )
    if not has_webhook:
        return False, "workflow missing Webhook trigger node"

    # Validate connection references
    connections = workflow_json.get("connections") or {}
    for source_id, targets in connections.items():
        for _key, outputs in targets.items():
            for output_list in outputs:
                for conn in output_list:
                    target_id = conn.get("node")
                    if target_id and target_id not in node_ids:
                        return False, f"connection references unknown node '{target_id}'"

    return True, ""


# ============================================================
# FULL AUTONOMOUS BUILD FLOW
# ============================================================

def autonomous_build(task_description: str) -> dict:
    """
    Full pipeline:
      1. Generate workflow JSON via Claude
      2. Validate structure
      3. Create in n8n
      4. Activate
      5. Return {workflow_id, webhook_path, url}
    """
    from tools.n8n_client import create_workflow, activate_workflow, N8N_BASE_URL

    logger.info(f"n8n_builder: autonomous build for: {task_description[:100]}")

    # Step 1: Generate
    workflow_json = generate_workflow_definition(task_description)
    workflow_json.setdefault("name", f"ainxt-auto-{uuid.uuid4().hex[:8]}")

    # Step 2: Validate
    valid, reason = validate_workflow(workflow_json)
    if not valid:
        logger.error(f"n8n_builder: validation failed — {reason}")
        raise ValueError(f"Generated workflow failed validation: {reason}")

    # Step 3: Create in n8n
    created = create_workflow(workflow_json)
    if "error" in created:
        raise RuntimeError(f"n8n create_workflow failed: {created['error']}")

    workflow_id = created.get("id")
    logger.info(f"n8n_builder: workflow created id={workflow_id}")

    # Step 4: Activate
    activate_result = activate_workflow(workflow_id)
    if "error" in activate_result:
        logger.warning(f"n8n_builder: activate failed — {activate_result['error']}")

    # Step 5: Extract webhook path
    webhook_path = _extract_webhook_path(workflow_json)

    result = {
        "workflow_id":   workflow_id,
        "workflow_name": workflow_json.get("name"),
        "webhook_path":  webhook_path,
        "url": f"{N8N_BASE_URL}/webhook/{webhook_path}" if webhook_path else None,
    }
    logger.info(f"n8n_builder: autonomous build complete → {result}")
    return result


def _extract_webhook_path(workflow_json: dict) -> str:
    """Extract the webhook trigger path from the workflow definition."""
    for node in (workflow_json.get("nodes") or []):
        if str(node.get("type", "")).lower().endswith("webhook"):
            path = node.get("parameters", {}).get("path")
            if path:
                return path.lstrip("/")
    return ""


# ============================================================
# ORCHESTRATOR TOOL ENTRY POINT
# ============================================================

def n8n_autonomous_tool(state):
    """
    Entry point compatible with orchestrator AgentState.
    Reads state.question, runs autonomous_build(), stores result in state.
    """
    logger.info("n8n_autonomous_tool: invoked")
    task = getattr(state, "question", "") or ""
    try:
        result = autonomous_build(task)
        state.n8n_workflow = result
    except Exception as e:
        logger.error(f"n8n_autonomous_tool failed: {e}")
        state.n8n_workflow = {"error": str(e)}
    return state
