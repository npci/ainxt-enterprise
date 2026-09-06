# SPDX-License-Identifier: MIT
"""MCP connection test endpoint."""
from fastapi import APIRouter, Depends
from app.models import McpTestRequest, McpTestResponse, AuthenticatedUser
from app.core.mcp_manager import test_mcp_connection
from app.api.deps import require_access

router = APIRouter()


@router.post("/mcp/test-connection", response_model=McpTestResponse)
async def test_mcp(
    request: McpTestRequest,
    current_user: AuthenticatedUser = Depends(require_access),
):
    try:
        # Forward user_id so any *_credential_id refs in the config can be
        # decrypted via vault.decrypt (RBAC + audit honoured for the probe).
        result = await test_mcp_connection(
            request.server_type, request.config,
            user_id=current_user.id,
        )
        return McpTestResponse(**result)
    except Exception as e:
        return McpTestResponse(status="error", message=str(e))
