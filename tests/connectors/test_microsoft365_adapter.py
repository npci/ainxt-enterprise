# SPDX-License-Identifier: MIT
import pytest
from fastapi import HTTPException

from connectors.adapters.microsoft365 import Microsoft365Adapter
from connectors.base import ConnectorTool


def _tool(name: str) -> ConnectorTool:
    return ConnectorTool(
        name=name,
        description="",
        method="GET",
        path="",
        requires_scopes=[],
        cache_ttl_s=0,
        paginated=False,
        max_items=50,
        is_write=False,
        input_schema={},
    )


def test_outlook_read_email_normalizes_single_graph_message_object():
    adapter = Microsoft365Adapter()
    data = {
        "id": "msg-1",
        "subject": "CEO update",
        "from": {"emailAddress": {"address": "ceo@example.com", "name": "CEO"}},
        "receivedDateTime": "2026-07-14T09:00:00Z",
        "isRead": True,
        "bodyPreview": "Opening lines",
        "hasAttachments": False,
        "body": {"contentType": "html", "content": "<p>Full CEO mail body</p>"},
        "toRecipients": [
            {"emailAddress": {"address": "user@example.com"}},
        ],
        "ccRecipients": [
            {"emailAddress": {"address": "lead@example.com"}},
        ],
    }

    items = adapter._extract_items(data, _tool("outlook_read_email"))

    assert items == [
        {
            "id": "msg-1",
            "subject": "CEO update",
            "from": "ceo@example.com",
            "from_name": "CEO",
            "received_at": "2026-07-14T09:00:00Z",
            "is_read": True,
            "preview": "Opening lines",
            "has_attachments": False,
            "body": "<p>Full CEO mail body</p>",
            "content_type": "html",
            "to": ["user@example.com"],
            "cc": ["lead@example.com"],
        }
    ]


def test_outlook_search_emails_keeps_graph_value_list_behavior():
    adapter = Microsoft365Adapter()
    data = {
        "value": [
            {
                "id": "msg-1",
                "subject": "CEO update",
                "from": {"emailAddress": {"address": "ceo@example.com", "name": "CEO"}},
                "receivedDateTime": "2026-07-14T09:00:00Z",
                "isRead": False,
                "bodyPreview": "Opening lines",
                "hasAttachments": True,
            }
        ]
    }

    items = adapter._extract_items(data, _tool("outlook_search_emails"))

    assert items == [
        {
            "id": "msg-1",
            "subject": "CEO update",
            "from": "ceo@example.com",
            "from_name": "CEO",
            "received_at": "2026-07-14T09:00:00Z",
            "is_read": False,
            "preview": "Opening lines",
            "has_attachments": True,
        }
    ]


def test_calendar_list_events_normalizes_compact_event_fields():
    adapter = Microsoft365Adapter()
    data = {
        "value": [
            {
                "id": "evt-1",
                "subject": "Planning Sync",
                "start": {"dateTime": "2026-07-15T10:00:00", "timeZone": "India Standard Time"},
                "end": {"dateTime": "2026-07-15T10:30:00", "timeZone": "India Standard Time"},
                "organizer": {"emailAddress": {"address": "lead@example.com", "name": "Lead"}},
                "attendees": [
                    {
                        "emailAddress": {"address": "user@example.com", "name": "User"},
                        "type": "required",
                        "status": {"response": "accepted"},
                    }
                ],
                "location": {"displayName": "Teams"},
                "isOnlineMeeting": True,
                "bodyPreview": "Discuss roadmap and milestones" * 20,
                "onlineMeeting": {"joinUrl": "https://example.com/very/large"},
            }
        ]
    }

    items = adapter._extract_items(data, _tool("calendar_list_events"))

    assert items == [
        {
            "id": "evt-1",
            "subject": "Planning Sync",
            "start": "2026-07-15T10:00:00",
            "end": "2026-07-15T10:30:00",
            "organizer_email": "lead@example.com",
            "organizer_name": "Lead",
            "attendees": [
                {
                    "email": "user@example.com",
                    "name": "User",
                    "type": "required",
                    "response": "accepted",
                }
            ],
            "location": "Teams",
            "is_online_meeting": True,
            "preview": ("Discuss roadmap and milestones" * 20)[:240],
        }
    ]
    assert "onlineMeeting" not in items[0]


def test_calendar_event_client_side_filters():
    adapter = Microsoft365Adapter()
    items = [
        {
            "id": "evt-1",
            "subject": "Planning Sync",
            "preview": "Roadmap",
            "location": "Teams",
            "organizer_email": "lead@example.com",
            "attendees": [{"email": "user@example.com"}],
        },
        {
            "id": "evt-2",
            "subject": "Finance Review",
            "preview": "Budget",
            "location": "Room 1",
            "organizer_email": "finance@example.com",
            "attendees": [{"email": "other@example.com"}],
        },
    ]

    filtered = adapter._filter_calendar_events(
        items,
        {"subject_contains": "planning", "organizer_email": "lead@example.com", "attendee_email": "user@example.com"},
    )

    assert filtered == [items[0]]


def test_calendar_create_event_marks_optional_attendees_optional():
    adapter = Microsoft365Adapter()

    body = adapter._build_write_body(
        _tool("calendar_create_event"),
        {
            "subject": "Planning",
            "start": "2026-07-14T10:00:00",
            "end": "2026-07-14T10:30:00",
            "attendees": "required@example.com",
            "optional_attendees": "optional@example.com; Other Optional <other@example.com>",
        },
    )

    assert body["attendees"] == [
        {"emailAddress": {"address": "required@example.com"}, "type": "required"},
        {"emailAddress": {"address": "optional@example.com"}, "type": "optional"},
        {"emailAddress": {"address": "other@example.com"}, "type": "optional"},
    ]


def test_calendar_update_event_marks_optional_attendees_optional():
    adapter = Microsoft365Adapter()

    body = adapter._build_write_body(
        _tool("calendar_update_event"),
        {
            "attendees": "required@example.com",
            "optional_attendees": "optional@example.com",
        },
    )

    assert body["attendees"] == [
        {"emailAddress": {"address": "required@example.com"}, "type": "required"},
        {"emailAddress": {"address": "optional@example.com"}, "type": "optional"},
    ]


def test_outlook_send_mail_includes_xlsx_file_attachment():
    adapter = Microsoft365Adapter()
    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "report.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "contentBytes": "UEsDBAo=",
    }

    body = adapter._build_write_body(
        _tool("outlook_send_mail"),
        {
            "to": "user@example.com",
            "subject": "Report",
            "body": "Please find attached.",
            "_attachments": [attachment],
        },
    )

    assert body["message"]["attachments"] == [attachment]


def test_teams_send_message_includes_reference_attachment_and_html_link():
    adapter = Microsoft365Adapter()

    body = adapter._build_write_body(
        _tool("teams_send_message"),
        {
            "message": "Sharing the Excel report.",
            "_attachments": [
                {
                    "_teams_attachment": {
                        "id": "report.xlsx",
                        "contentType": "reference",
                        "name": "report.xlsx",
                        "contentUrl": "https://contoso.example/report.xlsx",
                    },
                    "_teams_link_html": '<p>Attachment: <a href="https://contoso.example/report.xlsx">report.xlsx</a></p>',
                }
            ],
        },
    )

    assert body["attachments"] == [
        {
            "id": "report.xlsx",
            "contentType": "reference",
            "name": "report.xlsx",
            "contentUrl": "https://contoso.example/report.xlsx",
        }
    ]
    assert body["body"]["contentType"] == "html"
    assert "report.xlsx" in body["body"]["content"]


def test_confirmed_action_resolves_outlook_attachment(monkeypatch):
    import routers.connectors_router as router

    captured = {}
    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "report.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "contentBytes": "UEsDBAo=",
    }

    def fake_resolve(params, user_id=""):
        captured["params_after_pop"] = params
        captured["user_id"] = user_id
        return "ok", [attachment]

    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", fake_resolve)

    params = router._prepare_m365_action_attachments(
        "microsoft_365",
        "outlook_send_mail",
        {"to": "user@example.com", "attachment_job_id": "job-1"},
        "user-1",
    )

    assert captured["user_id"] == "user-1"
    assert "attachment_job_id" not in captured["params_after_pop"]
    assert params["_attachments"] == [attachment]


def test_confirmed_action_blocks_pending_attachment(monkeypatch):
    import routers.connectors_router as router

    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", lambda params, user_id="": ("pending", []))

    with pytest.raises(HTTPException) as exc:
        router._prepare_m365_action_attachments(
            "microsoft_365",
            "outlook_send_mail",
            {"to": "user@example.com", "attachment_job_id": "job-1"},
            "user-1",
        )

    assert exc.value.status_code == 409


def test_confirmed_action_blocks_teams_when_upload_fails(monkeypatch):
    import routers.connectors_router as router

    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "report.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "contentBytes": "UEsDBAo=",
    }
    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", lambda params, user_id="": ("ok", [attachment]))
    monkeypatch.setattr(
        "connectors.mcp_bridge._teams_attachment_from",
        lambda att, user_id="": {"name": att["name"], "_upload_ok": False},
    )

    with pytest.raises(HTTPException) as exc:
        router._prepare_m365_action_attachments(
            "microsoft_365",
            "teams_send_message",
            {"channel_id": "19:channel", "message": "Sharing", "attachment_job_id": "job-1"},
            "user-1",
        )

    assert exc.value.status_code == 502


def test_resolve_doc_attachments_accepts_working_directory_xlsx(tmp_path, monkeypatch):
    from connectors.mcp_bridge import _resolve_doc_attachments

    workbook = tmp_path / "report.xlsx"
    workbook.write_bytes(b"xlsx bytes")
    monkeypatch.chdir(tmp_path)

    status, attachments = _resolve_doc_attachments({"attachment_file_path": "report.xlsx"}, user_id="user-1")

    assert status == "ok"
    assert attachments[0]["name"] == "report.xlsx"
    assert attachments[0]["contentType"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert attachments[0]["contentBytes"] == "eGxzeCBieXRlcw=="


def test_resolve_doc_attachments_rejects_path_outside_working_directory(tmp_path, monkeypatch):
    from connectors.mcp_bridge import _resolve_doc_attachments

    outside = tmp_path.parent / "outside.xlsx"
    outside.write_bytes(b"xlsx bytes")
    monkeypatch.chdir(tmp_path)

    status, attachments = _resolve_doc_attachments({"attachment_file_path": str(outside)}, user_id="user-1")

    assert status == "none"
    assert attachments == []


def test_confirmed_action_resolves_local_file_path_attachment(monkeypatch):
    import routers.connectors_router as router

    captured = {}
    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "report.xlsx",
        "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "contentBytes": "UEsDBAo=",
    }

    def fake_resolve(params, user_id=""):
        captured["params_after_pop"] = params
        captured["user_id"] = user_id
        return "ok", [attachment]

    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", fake_resolve)

    params = router._prepare_m365_action_attachments(
        "microsoft_365",
        "outlook_send_mail",
        {"to": "user@example.com", "attachment_file_path": "report.xlsx"},
        "user-1",
    )

    assert captured["user_id"] == "user-1"
    assert "attachment_file_path" not in captured["params_after_pop"]
    assert params["_attachments"] == [attachment]


def test_resolve_doc_attachments_accepts_non_ainxt_pdf(tmp_path, monkeypatch):
    from connectors.mcp_bridge import _resolve_doc_attachments

    pdf = tmp_path / "vendor-report.pdf"
    pdf.write_bytes(b"%PDF user supplied")
    monkeypatch.chdir(tmp_path)

    status, attachments = _resolve_doc_attachments({"attachment_file_path": "vendor-report.pdf"}, user_id="user-1")

    assert status == "ok"
    assert attachments[0]["name"] == "vendor-report.pdf"
    assert attachments[0]["contentType"] == "application/pdf"
    assert attachments[0]["contentBytes"] == "JVBERiB1c2VyIHN1cHBsaWVk"


def test_confirmed_action_resolves_teams_local_file_path_attachment(monkeypatch):
    import routers.connectors_router as router

    attachment = {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": "vendor-report.pdf",
        "contentType": "application/pdf",
        "contentBytes": "JVBERg==",
    }
    teams_attachment = {
        "name": "vendor-report.pdf",
        "_upload_ok": True,
        "_teams_attachment": {
            "id": "vendor-report.pdf",
            "contentType": "reference",
            "name": "vendor-report.pdf",
            "contentUrl": "https://contoso.example/vendor-report.pdf",
        },
        "_teams_link_html": '<p>Attachment: <a href="https://contoso.example/vendor-report.pdf">vendor-report.pdf</a></p>',
    }

    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", lambda params, user_id="": ("ok", [attachment]))
    monkeypatch.setattr("connectors.mcp_bridge._teams_attachment_from", lambda att, user_id="": teams_attachment)

    params = router._prepare_m365_action_attachments(
        "microsoft_365",
        "teams_send_chat_message",
        {"chat_id": "19:chat", "message": "Sharing", "attachment_file_path": "vendor-report.pdf"},
        "user-1",
    )

    assert params["_attachments"] == [teams_attachment]


def test_confirmed_action_unresolved_attachment_guides_sources(monkeypatch):
    import routers.connectors_router as router

    monkeypatch.setattr("connectors.mcp_bridge._resolve_doc_attachments", lambda params, user_id="": ("none", []))

    with pytest.raises(HTTPException) as exc:
        router._prepare_m365_action_attachments(
            "microsoft_365",
            "outlook_send_mail",
            {"to": "user@example.com", "attachment_file_path": "missing.pdf"},
            "user-1",
        )

    assert exc.value.status_code == 400
    assert "generated document job/artifact id" in exc.value.detail
    assert "Buddy-uploaded attachment id" in exc.value.detail
    assert "valid local file path" in exc.value.detail
