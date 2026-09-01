# SPDX-License-Identifier: Apache-2.0
from tools.n8n_client import trigger_workflow
from core.logger import logger

def n8n_tool(state):

    logger.info("Triggering n8n workflow")

    payload = {
        "question": state.question,
        "context": state.context,
        "agent": "npc-agent"
    }

    result = trigger_workflow(payload)

    state.n8n_result = result

    return state