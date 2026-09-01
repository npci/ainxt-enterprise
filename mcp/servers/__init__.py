# SPDX-License-Identifier: Apache-2.0
# MCP Internal Servers — AiNxt platform tools exposed as spec-compliant MCP servers
# Transport: JSON-RPC 2.0 over stdio (subprocess) and SSE (FastAPI)
# Protocol: Model Context Protocol v1.0 (2024-11-05)

from mcp.servers.jira_server import JiraMCPServer
from mcp.servers.confluence_server import ConfluenceMCPServer
from mcp.servers.gitlab_server import GitLabMCPServer
from mcp.servers.database_server import DatabaseMCPServer
from mcp.servers.platform_server import PlatformMCPServer

# In-repo non-engineering MCP servers (registry-data-shaped, instant tier).
# Each wraps a corresponding tools/<slug>_tools.py module. See
# D:\MCPs\README.md for the original use-case coverage map.
from mcp.servers.kb_search_server      import KBSearchMCPServer
from mcp.servers.document_tools_server import DocumentToolsMCPServer
from mcp.servers.calendar_tools_server import CalendarToolsMCPServer
from mcp.servers.email_tools_server    import EmailToolsMCPServer
from mcp.servers.task_tracker_server   import TaskTrackerMCPServer
from mcp.servers.data_tools_server     import DataToolsMCPServer
from mcp.servers.ats_tools_server      import ATSToolsMCPServer
from mcp.servers.doc_generator_server  import DocGeneratorMCPServer
from mcp.servers.translator_server     import TranslatorMCPServer
from mcp.servers.lms_tools_server      import LMSToolsMCPServer

# Registry of all internal servers — keyed by their URL slug
INTERNAL_SERVERS: dict = {
    "jira":           JiraMCPServer,
    "confluence":     ConfluenceMCPServer,
    "gitlab":         GitLabMCPServer,
    "database":       DatabaseMCPServer,
    "platform":       PlatformMCPServer,
    "kb_search":      KBSearchMCPServer,
    "document_tools": DocumentToolsMCPServer,
    "calendar_tools": CalendarToolsMCPServer,
    "email_tools":    EmailToolsMCPServer,
    "task_tracker":   TaskTrackerMCPServer,
    "data_tools":     DataToolsMCPServer,
    "ats_tools":      ATSToolsMCPServer,
    "doc_generator":  DocGeneratorMCPServer,
    "translator":     TranslatorMCPServer,
    "lms_tools":      LMSToolsMCPServer,
}

__all__ = [
    "JiraMCPServer",
    "ConfluenceMCPServer",
    "GitLabMCPServer",
    "DatabaseMCPServer",
    "PlatformMCPServer",
    "KBSearchMCPServer",
    "DocumentToolsMCPServer",
    "CalendarToolsMCPServer",
    "EmailToolsMCPServer",
    "TaskTrackerMCPServer",
    "DataToolsMCPServer",
    "ATSToolsMCPServer",
    "DocGeneratorMCPServer",
    "TranslatorMCPServer",
    "LMSToolsMCPServer",
    "INTERNAL_SERVERS",
]
