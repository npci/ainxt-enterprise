# SPDX-License-Identifier: Apache-2.0
"""
Regression test — email/Teams message retrieval silently capped by a stale
seeded max_items, well below the intended TOOL_MAX_ITEMS ceiling.

Root cause: `outlook_search_emails` / `outlook_count_emails` (seeded 50) and
`teams_get_channel_messages` (seeded 100) / `teams_get_chat_messages` (seeded
50) were seeded in connectors/seed.py with max_items values that matched
Graph's page size back when TOOL_MAX_ITEMS also allowed only that many.
TOOL_MAX_ITEMS was later raised (2000 for all four) so large mailboxes/
channels/chats page through fully, but the seeded per-tool value was never
bumped to match. `ConnectorEngine._get_tool()` always preferred the seeded
value, so `tool.max_items` stayed at the old small number — which made the
adapter's `@odata.nextLink` auto-follow loop (connectors/adapters/
microsoft365.py) stop immediately after the very first Graph page
(`len(items) < cap` was e.g. `50 < 50` → False), even though Graph reported
more pages were available.

Fix: `_get_tool()` now takes `max(seeded_value, TOOL_MAX_ITEMS[tool_name])`
ONLY for the tools in `_STALE_SEEDED_MAX_ITEMS_TOOLS` — every other tool's
seeded ceiling is preserved exactly as-is (no DB write, no behavior change
for GitLab/Jira/Slack/calendar/org-lookup tools, or any other Teams tool
such as teams_list_chats whose seeded value already matches its ceiling).
"""
import pytest

from connectors.engine import (
    ConnectorEngine,
    TOOL_MAX_ITEMS,
    _STALE_SEEDED_MAX_ITEMS_TOOLS,
    _MAX_ITEMS_DEFAULT,
)


def _defn_with_tool(tool_name: str, seeded_max_items) -> dict:
    tool: dict = {
        "name": tool_name,
        "description": "",
        "method": "GET",
        "path": "/v1.0/me/messages",
        "input_schema": {},
        "requires_scopes": [],
        "cache_ttl_s": 300,
        "paginated": True,
        "is_write": False,
    }
    if seeded_max_items is not None:
        tool["max_items"] = seeded_max_items
    return {"name": "microsoft_365", "tools": [tool]}


# The actual seeded value each affected tool ships with today in
# connectors/seed.py — used to reproduce the exact stale-config scenario
# rather than a synthetic placeholder.
_ACTUAL_SEEDED_VALUES = {
    "outlook_search_emails": 50,
    "outlook_count_emails": 50,
    "teams_get_channel_messages": 100,
    "teams_get_chat_messages": 50,
}


@pytest.mark.parametrize("tool_name", sorted(_STALE_SEEDED_MAX_ITEMS_TOOLS))
def test_stale_seeded_max_items_is_raised_to_ceiling(tool_name):
    """Every known-stale tool (outlook_search_emails, outlook_count_emails,
    teams_get_channel_messages, teams_get_chat_messages) must resolve to the
    higher TOOL_MAX_ITEMS ceiling, not its stale seeded value — this is the
    actual bug fix."""
    engine = ConnectorEngine()
    assert tool_name in _ACTUAL_SEEDED_VALUES, (
        f"{tool_name!r} is in _STALE_SEEDED_MAX_ITEMS_TOOLS but this test doesn't "
        "know its real seeded value — add it to _ACTUAL_SEEDED_VALUES above."
    )
    seeded_value = _ACTUAL_SEEDED_VALUES[tool_name]
    defn = _defn_with_tool(tool_name, seeded_max_items=seeded_value)

    tool = engine._get_tool(defn, tool_name)

    expected_ceiling = TOOL_MAX_ITEMS[tool_name]
    assert expected_ceiling > seeded_value, (
        "test fixture assumption: ceiling must exceed the actual stale seeded value"
    )
    assert tool.max_items == expected_ceiling


@pytest.mark.parametrize("tool_name", sorted(_STALE_SEEDED_MAX_ITEMS_TOOLS))
def test_stale_seeded_max_items_never_lowers_a_larger_seeded_value(tool_name):
    """If an operator ever seeds a value ABOVE the TOOL_MAX_ITEMS ceiling, the
    fix must not clamp it back down — it's max(), not a hard override."""
    engine = ConnectorEngine()
    huge_value = TOOL_MAX_ITEMS[tool_name] + 5000
    defn = _defn_with_tool(tool_name, seeded_max_items=huge_value)

    tool = engine._get_tool(defn, tool_name)

    assert tool.max_items == huge_value


@pytest.mark.parametrize(
    "tool_name,seeded_max_items",
    [
        ("calendar_list_events", 200),
        # teams_list_chats: seeded 1000 == its TOOL_MAX_ITEMS ceiling (1000), so
        # it's deliberately NOT in _STALE_SEEDED_MAX_ITEMS_TOOLS — nothing to
        # override, and this asserts the fix doesn't touch it either way.
        ("teams_list_chats", 1000),
        ("teams_list_channel_members", 500),
        ("teams_list_members", 1000),
        ("teams_get_chat_members", 500),
        ("teams_list_meetings", 200),
        ("teams_list_my_teams", 50),
        ("teams_list_channels", 50),
        ("people_search", 25),
        ("gitlab_list_my_mrs", 50),
        ("gitlab_search_code", 20),
        ("jira_search_issues", 50),
        ("outlook_list_folders", 200),
        ("org_direct_reports", 100),
        ("slack_search_messages", 100),
        ("slack_get_channel_messages", 100),
    ],
)
def test_unrelated_tools_keep_their_exact_seeded_max_items(tool_name, seeded_max_items):
    """Every tool NOT in _STALE_SEEDED_MAX_ITEMS_TOOLS must be completely
    unaffected by this fix — no widened ceilings for GitLab/Jira/Slack/calendar/
    org-lookup tools, or other Teams tools (list_chats, list_members, etc.),
    even when they're absent from TOOL_MAX_ITEMS (which would otherwise fall
    back to the much larger _MAX_ITEMS_DEFAULT)."""
    engine = ConnectorEngine()
    assert tool_name not in _STALE_SEEDED_MAX_ITEMS_TOOLS, (
        f"{tool_name!r} was moved into _STALE_SEEDED_MAX_ITEMS_TOOLS — remove it "
        "from this 'unrelated tools' list and cover it in the affected-tools test instead."
    )
    defn = _defn_with_tool(tool_name, seeded_max_items=seeded_max_items)

    tool = engine._get_tool(defn, tool_name)

    assert tool.max_items == seeded_max_items


def test_tool_with_no_seeded_max_items_still_falls_back_as_before():
    """A tool with no `max_items` key at all in its seeded dict must keep
    falling back to TOOL_MAX_ITEMS / _MAX_ITEMS_DEFAULT exactly as before —
    this fix must not change that pre-existing fallback behaviour."""
    engine = ConnectorEngine()
    defn = _defn_with_tool("some_unlisted_tool", seeded_max_items=None)

    tool = engine._get_tool(defn, "some_unlisted_tool")

    assert tool.max_items == _MAX_ITEMS_DEFAULT
