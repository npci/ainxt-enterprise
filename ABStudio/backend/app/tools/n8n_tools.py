# SPDX-License-Identifier: Apache-2.0
"""
n8n tools — trigger webhooks and manage workflows via the n8n API.

Env vars:
  N8N_URL       — required; n8n base URL e.g. http://localhost:5678
  N8N_API_KEY   — n8n API key (Settings → API → Create API Key)
  N8N_WEBHOOK_URL — optional override for the webhook base URL
Each tool's `code` string is self-contained and runs in the sandbox subprocess.

NOTE: These tools are marked `"draft": True` — they are present in the catalog
but will NOT be seeded into the database until the n8n integration is
configured and the draft flag is removed.
"""

_HELPERS = '''
import os, json, urllib.request, urllib.error

def _n8n_base():
    base = os.environ.get("N8N_URL", "").rstrip("/")
    if not base:
        raise Exception("N8N_URL is not set — configure it to your n8n instance URL")
    return base

def _n8n_api_key():
    return os.environ.get("N8N_API_KEY", "")

def _headers():
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    key = _n8n_api_key()
    if key:
        h["X-N8N-API-KEY"] = key
    return h

def _request(method, path, payload=None):
    url  = f"{_n8n_base()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req  = urllib.request.Request(url, data=data, headers=_headers(), method=method)
    try:
        # Enterprise-grade timeout: 90s (was 30s). n8n webhook executions
        # can chain many steps; we let them finish before declaring failure
        # so attached flows aren't aborted by a slow downstream workflow.
        with urllib.request.urlopen(req, timeout=90) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise Exception(f"HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise Exception(f"n8n unreachable: {e.reason}")
'''

N8N_TOOLS = [
    # ------------------------------------------------------------------ #
    # n8n_trigger                                                          #
    # ------------------------------------------------------------------ #
    {
        "name": "n8n_trigger",
        "draft": True,
        "description": "Trigger an n8n webhook workflow with a JSON payload. Returns the webhook response.",
        "input_schema": {
            "type": "object",
            "properties": {
                "payload":      {"type": "object", "description": "JSON payload to send to the webhook"},
                "webhook_path": {"type": "string", "description": "Webhook path e.g. /webhook/my-workflow. Defaults to N8N_WEBHOOK_PATH env var."},
            },
            "required": ["payload"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        payload      = inputs.get("payload", {})
        webhook_path = inputs.get("webhook_path") or os.environ.get("N8N_WEBHOOK_PATH", "/webhook/trigger")
        if not webhook_path.startswith("/"):
            webhook_path = "/" + webhook_path
        result = _request("POST", webhook_path, payload)
        return {"result": f"Webhook triggered: {webhook_path}", "response": result}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # n8n_list_workflows                                                   #
    # ------------------------------------------------------------------ #
    {
        "name": "n8n_list_workflows",
        "draft": True,
        "description": "List all workflows in n8n.",
        "input_schema": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "description": "Return only active workflows", "default": False},
            },
            "required": [],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        active_only = inputs.get("active_only", False)
        result      = _request("GET", "/api/v1/workflows")
        workflows   = result.get("data", result) if isinstance(result, dict) else result
        if not isinstance(workflows, list):
            return {"error": f"Unexpected response: {result}"}
        if active_only:
            workflows = [w for w in workflows if w.get("active")]
        items = [{"id": w.get("id"), "name": w.get("name"), "active": w.get("active", False)} for w in workflows]
        lines = [f"n8n workflows ({len(items)}):"] + [f"• [{w[\'id\']}] {w[\'name\']} ({\'active\' if w[\'active\'] else \'inactive\'})" for w in items]
        return {"result": "\\n".join(lines), "workflows": items}
    except Exception as e:
        return {"error": str(e)}
''',
    },

    # ------------------------------------------------------------------ #
    # n8n_get_execution                                                    #
    # ------------------------------------------------------------------ #
    {
        "name": "n8n_get_execution",
        "draft": True,
        "description": "Get the status and output of an n8n workflow execution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "execution_id": {"type": "string", "description": "n8n execution ID"},
            },
            "required": ["execution_id"],
        },
        "code": _HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        execution_id = inputs.get("execution_id", "")
        result       = _request("GET", f"/api/v1/executions/{execution_id}")
        data         = result.get("data", result) if isinstance(result, dict) else result
        status       = data.get("status", data.get("finished", "unknown"))
        workflow_id  = data.get("workflowId", "?")
        started      = data.get("startedAt", "?")
        finished     = data.get("stoppedAt", "?")
        return {
            "result":       f"Execution {execution_id}: {status} (workflow {workflow_id})",
            "execution_id": execution_id,
            "status":       status,
            "workflow_id":  workflow_id,
            "started_at":   started,
            "finished_at":  finished,
        }
    except Exception as e:
        return {"error": str(e)}
''',
    },
]
