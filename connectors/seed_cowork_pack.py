# SPDX-License-Identifier: Apache-2.0
"""
Cowork connector pack — Gmail/Slack already live in seed.py; this adds the 5 new
custom-adapter connectors (Google Drive, DocuSign, Zoom, Jira, Confluence) scaffolded
by the cowork-connector-pack workflow (2026-05-30), modeled on the M365 adapter.

SCAFFOLD: most of these are external SaaS and are egress-blocked (demo-only).
Each pairs with connectors/adapters/<name>.py and an entry in engine.py adapter_map.
seed.py does: SEED_CONNECTORS += COWORK_PACK_CONNECTORS.
"""

_ENV_GOOGLE_OAUTH_CREDENTIAL     = "GOOGLE_CLIENT_SECRET"
_ENV_DOCUSIGN_OAUTH_CREDENTIAL   = "DOCUSIGN_CLIENT_SECRET"
_ENV_ZOOM_OAUTH_CREDENTIAL       = "ZOOM_CLIENT_SECRET"
_ENV_ATLASSIAN_OAUTH_CREDENTIAL  = "ATLASSIAN_CLIENT_SECRET"

COWORK_PACK_CONNECTORS = [   {   'name': 'google_drive',
        'display_name': 'Google Drive',
        'description': 'Google Drive — drive_search_files, drive_get_file_metadata, drive_get_file_text.',
        'icon_url': '/icons/google_drive.svg',
        'category': 'productivity',
        'auth_type': 'oauth2',
        'has_custom_adapter': True,
        'rate_limit_per_min': 60,
        'is_builtin': True,
        'base_url': 'https://www.googleapis.com/drive/v3',
        'auth_config': {   'authorize_url': 'https://accounts.google.com/o/oauth2/v2/auth',
                           'token_url': 'https://oauth2.googleapis.com/token',
                           'client_id_env': 'GOOGLE_CLIENT_ID',
                           'client_secret_env': _ENV_GOOGLE_OAUTH_CREDENTIAL,
                           'pkce': True,
                           'extra_params': {'access_type': 'offline', 'prompt': 'consent'},
                           'revoke_url': 'https://oauth2.googleapis.com/revoke',
                           'scopes': [   'openid',
                                         'email',
                                         'profile',
                                         'https://www.googleapis.com/auth/drive.readonly']},
        'tools': [   {   'name': 'drive_search_files',
                         'description': 'Search Google Drive files and folders by name, full-text content, '
                                        "MIME type, or modified date. Use for 'find my drive files', 'search "
                                        "google drive for', 'documents named', 'spreadsheets modified after' "
                                        'queries. Returns file metadata (id, name, mimeType, owner, '
                                        'modifiedTime, webViewLink).',
                         'method': 'GET',
                         'path': '/files',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': True,
                         'max_items': 50,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'query': {   'type': 'string',
                                                                            'description': 'Free-text term '
                                                                                           'matched against '
                                                                                           'file name OR '
                                                                                           'full-text '
                                                                                           'content.'},
                                                               'name_contains': {   'type': 'string',
                                                                                    'description': 'Substring '
                                                                                                   'the file '
                                                                                                   'name '
                                                                                                   'must '
                                                                                                   'contain.'},
                                                               'mime_type': {   'type': 'string',
                                                                                'description': 'Exact MIME '
                                                                                               'type filter, '
                                                                                               'e.g. '
                                                                                               "'application/vnd.google-apps.document' "
                                                                                               '(Docs), '
                                                                                               "'application/vnd.google-apps.folder' "
                                                                                               '(folders), '
                                                                                               "'application/pdf'."},
                                                               'modified_after': {   'type': 'string',
                                                                                     'description': 'Return '
                                                                                                    'only '
                                                                                                    'files '
                                                                                                    'modified '
                                                                                                    'after '
                                                                                                    'this '
                                                                                                    'date. '
                                                                                                    'RFC3339 '
                                                                                                    'or bare '
                                                                                                    'YYYY-MM-DD.'},
                                                               'include_trashed': {   'type': 'boolean',
                                                                                      'description': 'Include '
                                                                                                     'trashed '
                                                                                                     'files. '
                                                                                                     'Defaults '
                                                                                                     'to '
                                                                                                     'false.'},
                                                               'limit': {   'type': 'integer',
                                                                            'description': 'Max files to '
                                                                                           'return '
                                                                                           '(hard-capped at '
                                                                                           '50).'}},
                                             'required': []}},
                     {   'name': 'drive_get_file_metadata',
                         'description': 'Get full metadata for a single Google Drive file by ID: name, '
                                        'mimeType, size, owner, created/modified time, parents, description, '
                                        'and webViewLink. Use when a file id is already known and you need '
                                        'details (not content).',
                         'method': 'GET',
                         'path': '/files/{file_id}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'file_id': {   'type': 'string',
                                                                              'description': 'Google Drive '
                                                                                             'file ID.'}},
                                             'required': ['file_id']}},
                     {   'name': 'drive_get_file_text',
                         'description': 'Get the plain-text contents of a Google Drive file by ID. '
                                        'Google-native Docs/Sheets/Slides are exported to text (text/plain '
                                        'or text/csv); text files are downloaded directly. Binary files '
                                        "(PDF, images) return a marker rather than raw bytes. Use for 'read "
                                        "the contents of', 'summarize this drive doc', 'what does this file "
                                        "say'.",
                         'method': 'GET',
                         'path': '/files/{file_id}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'file_id': {   'type': 'string',
                                                                              'description': 'Google Drive '
                                                                                             'file ID to '
                                                                                             'read.'}},
                                             'required': ['file_id']}}]},
    {   'name': 'docusign',
        'display_name': 'DocuSign',
        'description': 'DocuSign — docusign_list_envelopes, docusign_get_envelope, docusign_create_envelope.',
        'icon_url': '/icons/docusign.svg',
        'category': 'productivity',
        'auth_type': 'oauth2',
        'has_custom_adapter': True,
        'rate_limit_per_min': 60,
        'is_builtin': True,
        'base_url': 'https://demo.docusign.net/restapi',
        'auth_config': {   'authorize_url': 'https://account-d.docusign.com/oauth/auth',
                           'token_url': 'https://account-d.docusign.com/oauth/token',
                           'client_id_env': 'DOCUSIGN_CLIENT_ID',
                           'client_secret_env': _ENV_DOCUSIGN_OAUTH_CREDENTIAL,
                           'pkce': True,
                           'extra_params': {},
                           'scopes': ['signature', 'impersonation']},
        'tools': [   {   'name': 'docusign_list_envelopes',
                         'description': "List envelopes from the user's DocuSign account, optionally "
                                        'filtered by status (sent, delivered, completed, declined, voided) '
                                        "and a from_date window. Use for phrasings like 'show my DocuSign "
                                        "envelopes', 'what documents are awaiting signature', 'list "
                                        "completed signatures'.",
                         'method': 'GET',
                         'path': '/v2.1/accounts/{account_id}/envelopes',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': True,
                         'max_items': 50,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'account_id': {   'type': 'string',
                                                                                 'description': 'DocuSign '
                                                                                                'account ID '
                                                                                                '(GUID) that '
                                                                                                'owns the '
                                                                                                'envelopes.'},
                                                               'from_date': {   'type': 'string',
                                                                                'description': 'ISO-8601 '
                                                                                               'start of the '
                                                                                               'time window, '
                                                                                               'e.g. '
                                                                                               '2026-05-01T00:00:00Z. '
                                                                                               'Defaults to '
                                                                                               'the last 30 '
                                                                                               'days if '
                                                                                               'omitted.'},
                                                               'status': {   'type': 'string',
                                                                             'description': 'Filter by '
                                                                                            'envelope '
                                                                                            'status: sent, '
                                                                                            'delivered, '
                                                                                            'completed, '
                                                                                            'declined, '
                                                                                            'voided, '
                                                                                            'created.'},
                                                               'limit': {   'type': 'integer',
                                                                            'description': 'Maximum number '
                                                                                           'of envelopes to '
                                                                                           'return.'}},
                                             'required': ['account_id']}},
                     {   'name': 'docusign_get_envelope',
                         'description': 'Get the status and metadata of a single DocuSign envelope by ID. '
                                        "Use for 'what is the status of envelope X', 'is my document signed "
                                        "yet', 'check signature progress'.",
                         'method': 'GET',
                         'path': '/v2.1/accounts/{account_id}/envelopes/{envelope_id}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'account_id': {   'type': 'string',
                                                                                 'description': 'DocuSign '
                                                                                                'account ID '
                                                                                                '(GUID) that '
                                                                                                'owns the '
                                                                                                'envelope.'},
                                                               'envelope_id': {   'type': 'string',
                                                                                  'description': 'The '
                                                                                                 'envelope '
                                                                                                 'ID (GUID) '
                                                                                                 'to fetch '
                                                                                                 'status '
                                                                                                 'for.'}},
                                             'required': ['account_id', 'envelope_id']}},
                     {   'name': 'docusign_create_envelope',
                         'description': 'Create and send a DocuSign envelope for signature: attach a '
                                        'base64-encoded document and add a single signer. WRITE action — '
                                        'requires explicit user confirmation before sending.',
                         'method': 'POST',
                         'path': '/v2.1/accounts/{account_id}/envelopes',
                         'requires_scopes': [],
                         'cache_ttl_s': 0,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': True,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'account_id': {   'type': 'string',
                                                                                 'description': 'DocuSign '
                                                                                                'account ID '
                                                                                                '(GUID) '
                                                                                                'under which '
                                                                                                'to create '
                                                                                                'the '
                                                                                                'envelope.'},
                                                               'signer_email': {   'type': 'string',
                                                                                   'description': 'Email '
                                                                                                  'address '
                                                                                                  'of the '
                                                                                                  'recipient '
                                                                                                  'who must '
                                                                                                  'sign.'},
                                                               'signer_name': {   'type': 'string',
                                                                                  'description': 'Full name '
                                                                                                 'of the '
                                                                                                 'signer.'},
                                                               'document_base64': {   'type': 'string',
                                                                                      'description': 'Base64-encoded '
                                                                                                     'PDF '
                                                                                                     'document '
                                                                                                     'to be '
                                                                                                     'signed.'},
                                                               'document_name': {   'type': 'string',
                                                                                    'description': 'File '
                                                                                                   'name of '
                                                                                                   'the '
                                                                                                   'document, '
                                                                                                   'e.g. '
                                                                                                   'agreement.pdf.'},
                                                               'email_subject': {   'type': 'string',
                                                                                    'description': 'Subject '
                                                                                                   'line of '
                                                                                                   'the '
                                                                                                   'signature-request '
                                                                                                   'email.'},
                                                               'status': {   'type': 'string',
                                                                             'description': "'sent' to send "
                                                                                            'immediately for '
                                                                                            'signature '
                                                                                            '(default), or '
                                                                                            "'created' to "
                                                                                            'save as a '
                                                                                            'draft.'}},
                                             'required': [   'account_id',
                                                             'signer_email',
                                                             'signer_name',
                                                             'document_base64']}}]},
    {   'name': 'zoom',
        'display_name': 'Zoom',
        'description': 'Zoom — zoom_list_meetings, zoom_get_meeting, zoom_create_meeting.',
        'icon_url': '/icons/zoom.svg',
        'category': 'communication',
        'auth_type': 'oauth2',
        'has_custom_adapter': True,
        'rate_limit_per_min': 60,
        'is_builtin': True,
        'base_url': 'https://api.zoom.us/v2',
        'auth_config': {   'authorize_url': 'https://zoom.us/oauth/authorize',
                           'token_url': 'https://zoom.us/oauth/token',
                           'client_id_env': 'ZOOM_CLIENT_ID',
                           'client_secret_env': _ENV_ZOOM_OAUTH_CREDENTIAL,
                           'pkce': True,
                           'extra_params': {},
                           'scopes': ['meeting:read', 'meeting:write', 'user:read']},
        'tools': [   {   'name': 'zoom_list_meetings',
                         'description': "List the current user's Zoom meetings. Filter by type (scheduled | "
                                        "live | upcoming). Use for 'my zoom meetings', 'upcoming zoom "
                                        "calls', 'list meetings' queries.",
                         'method': 'GET',
                         'path': '/users/me/meetings',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': True,
                         'max_items': 50,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'type': {   'type': 'string',
                                                                           'description': 'Meeting list '
                                                                                          'type: scheduled, '
                                                                                          'live, or upcoming '
                                                                                          '(default '
                                                                                          'scheduled)'},
                                                               'limit': {   'type': 'integer',
                                                                            'description': 'Max meetings to '
                                                                                           'return (default '
                                                                                           '25, max 50)'}}}},
                     {   'name': 'zoom_get_meeting',
                         'description': 'Get details for a single Zoom meeting by its numeric meeting ID. '
                                        "Use for 'show meeting <id>', 'details of zoom meeting' queries.",
                         'method': 'GET',
                         'path': '/meetings/{meeting_id}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'meeting_id': {   'type': 'string',
                                                                                 'description': 'Zoom '
                                                                                                'numeric '
                                                                                                'meeting '
                                                                                                'ID'}},
                                             'required': ['meeting_id']}},
                     {   'name': 'zoom_create_meeting',
                         'description': 'Create (schedule) a new Zoom meeting for the current user. WRITE '
                                        'action — requires explicit user confirmation before sending.',
                         'method': 'POST',
                         'path': '/users/me/meetings',
                         'requires_scopes': [],
                         'cache_ttl_s': 0,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': True,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'topic': {   'type': 'string',
                                                                            'description': 'Meeting topic / '
                                                                                           'title'},
                                                               'start_time': {   'type': 'string',
                                                                                 'description': 'Start time '
                                                                                                'in ISO 8601 '
                                                                                                '(e.g. '
                                                                                                '2026-06-01T15:00:00Z)'},
                                                               'duration': {   'type': 'integer',
                                                                               'description': 'Meeting '
                                                                                              'duration in '
                                                                                              'minutes'},
                                                               'timezone': {   'type': 'string',
                                                                               'description': 'IANA timezone '
                                                                                              '(e.g. '
                                                                                              'Asia/Kolkata)'},
                                                               'agenda': {   'type': 'string',
                                                                             'description': 'Optional '
                                                                                            'meeting agenda '
                                                                                            '/ description'}},
                                             'required': ['topic']}}]},
    {   'name': 'jira',
        'display_name': 'Jira',
        'description': 'Jira — jira_search_issues, jira_get_issue, jira_create_issue, jira_add_comment.',
        'icon_url': '/icons/jira.svg',
        'category': 'devtools',
        'auth_type': 'oauth2',
        'has_custom_adapter': True,
        'rate_limit_per_min': 60,
        'is_builtin': True,
        'base_url': 'https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3',
        'auth_config': {   'authorize_url': 'https://auth.atlassian.com/authorize',
                           'token_url': 'https://auth.atlassian.com/oauth/token',
                           'client_id_env': 'ATLASSIAN_CLIENT_ID',
                           'client_secret_env': _ENV_ATLASSIAN_OAUTH_CREDENTIAL,
                           'pkce': False,
                           'extra_params': {'audience': 'api.atlassian.com', 'prompt': 'consent'},
                           'scopes': [   'read:jira-work',
                                         'write:jira-work',
                                         'read:jira-user',
                                         'offline_access']},
        'tools': [   {   'name': 'jira_search_issues',
                         'description': "Search Jira issues using a JQL query. Use for 'my open issues', "
                                        "'bugs in project X', 'issues assigned to', 'tickets updated this "
                                        "week' queries. Returns key, summary, status, assignee, priority, "
                                        'type.',
                         'method': 'GET',
                         'path': '/search',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': True,
                         'max_items': 50,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'jql': {   'type': 'string',
                                                                          'description': 'JQL query string, '
                                                                                         "e.g. 'project = "
                                                                                         'PAY AND status = '
                                                                                         '"In Progress" '
                                                                                         'ORDER BY updated '
                                                                                         "DESC'"},
                                                               'fields': {   'type': 'string',
                                                                             'description': 'Comma-separated '
                                                                                            'Jira fields to '
                                                                                            'return '
                                                                                            '(default: '
                                                                                            'summary,status,assignee,reporter,priority,issuetype,created,updated)'},
                                                               'limit': {   'type': 'integer',
                                                                            'description': 'Max issues to '
                                                                                           'return (default '
                                                                                           '25, hard max '
                                                                                           '50)'}},
                                             'required': ['jql']}},
                     {   'name': 'jira_get_issue',
                         'description': 'Get full details of a single Jira issue by its key (e.g. PAY-123), '
                                        'including summary, description, status, assignee, priority, labels.',
                         'method': 'GET',
                         'path': '/issue/{issue_key}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'issue_key': {   'type': 'string',
                                                                                'description': 'Jira issue '
                                                                                               'key, e.g. '
                                                                                               "'PAY-123'"},
                                                               'fields': {   'type': 'string',
                                                                             'description': 'Comma-separated '
                                                                                            'fields to '
                                                                                            'return (default '
                                                                                            'includes '
                                                                                            'summary, '
                                                                                            'description, '
                                                                                            'status, '
                                                                                            'assignee, '
                                                                                            'labels)'}},
                                             'required': ['issue_key']}},
                     {   'name': 'jira_create_issue',
                         'description': 'Create a new Jira issue in a project. WRITE action — requires '
                                        'explicit user confirmation before sending.',
                         'method': 'POST',
                         'path': '/issue',
                         'requires_scopes': [],
                         'cache_ttl_s': 0,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': True,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'project_key': {   'type': 'string',
                                                                                  'description': 'Project '
                                                                                                 'key the '
                                                                                                 'issue '
                                                                                                 'belongs '
                                                                                                 'to, e.g. '
                                                                                                 "'PAY'"},
                                                               'summary': {   'type': 'string',
                                                                              'description': 'Short issue '
                                                                                             'summary / '
                                                                                             'title'},
                                                               'description': {   'type': 'string',
                                                                                  'description': 'Issue '
                                                                                                 'description '
                                                                                                 '(plain '
                                                                                                 'text; '
                                                                                                 'converted '
                                                                                                 'to ADF)'},
                                                               'issue_type': {   'type': 'string',
                                                                                 'description': 'Issue type '
                                                                                                'name, e.g. '
                                                                                                "'Task', "
                                                                                                "'Bug', "
                                                                                                "'Story' "
                                                                                                '(default '
                                                                                                "'Task')"},
                                                               'priority': {   'type': 'string',
                                                                               'description': 'Priority '
                                                                                              'name, e.g. '
                                                                                              "'High', "
                                                                                              "'Medium'"},
                                                               'labels': {   'type': 'string',
                                                                             'description': 'Comma-separated '
                                                                                            'labels to '
                                                                                            'apply'}},
                                             'required': ['project_key', 'summary']}},
                     {   'name': 'jira_add_comment',
                         'description': 'Add a comment to an existing Jira issue. WRITE action — requires '
                                        'explicit user confirmation before sending.',
                         'method': 'POST',
                         'path': '/issue/{issue_key}/comment',
                         'requires_scopes': [],
                         'cache_ttl_s': 0,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': True,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'issue_key': {   'type': 'string',
                                                                                'description': 'Jira issue '
                                                                                               'key to '
                                                                                               'comment on, '
                                                                                               'e.g. '
                                                                                               "'PAY-123'"},
                                                               'comment': {   'type': 'string',
                                                                              'description': 'Comment text '
                                                                                             '(plain text; '
                                                                                             'converted to '
                                                                                             'ADF)'}},
                                             'required': ['issue_key', 'comment']}}]},
    {   'name': 'confluence',
        'display_name': 'Confluence',
        'description': 'Confluence — confluence_search_pages, confluence_get_page, confluence_create_page.',
        'icon_url': '/icons/confluence.svg',
        'category': 'productivity',
        'auth_type': 'oauth2',
        'has_custom_adapter': True,
        'rate_limit_per_min': 60,
        'is_builtin': True,
        'base_url': 'https://your-domain.atlassian.net/wiki/rest/api',
        'auth_config': {   'authorize_url': 'https://auth.atlassian.com/authorize',
                           'token_url': 'https://auth.atlassian.com/oauth/token',
                           'client_id_env': 'ATLASSIAN_CLIENT_ID',
                           'client_secret_env': _ENV_ATLASSIAN_OAUTH_CREDENTIAL,
                           'pkce': False,
                           'extra_params': {'audience': 'api.atlassian.com', 'prompt': 'consent'},
                           'scopes': [   'read:confluence-content.all',
                                         'read:confluence-content.summary',
                                         'read:confluence-space.summary',
                                         'write:confluence-content',
                                         'search:confluence',
                                         'offline_access']},
        'tools': [   {   'name': 'confluence_search_pages',
                         'description': 'Search Confluence pages using CQL (Confluence Query Language), or '
                                        'supply a simple text query + optional space_key and the adapter '
                                        "builds the CQL. Use for 'find page about', 'search confluence', "
                                        "'wiki pages about' queries.",
                         'method': 'GET',
                         'path': '/content/search',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': True,
                         'max_items': 50,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'cql': {   'type': 'string',
                                                                          'description': 'Raw CQL query '
                                                                                         'string (e.g. '
                                                                                         "'type=page AND "
                                                                                         'text ~ '
                                                                                         '"onboarding"\'). '
                                                                                         'If omitted, built '
                                                                                         'from '
                                                                                         'query/space_key.'},
                                                               'query': {   'type': 'string',
                                                                            'description': 'Free-text search '
                                                                                           'term (used to '
                                                                                           'build CQL when '
                                                                                           'cql is not '
                                                                                           'supplied)'},
                                                               'space_key': {   'type': 'string',
                                                                                'description': 'Restrict '
                                                                                               'search to a '
                                                                                               'Confluence '
                                                                                               'space key '
                                                                                               '(e.g. '
                                                                                               "'ENG')"},
                                                               'limit': {   'type': 'integer',
                                                                            'description': 'Max results '
                                                                                           '(default 25, max '
                                                                                           '50)'}},
                                             'required': []}},
                     {   'name': 'confluence_get_page',
                         'description': 'Get a single Confluence page by ID including its body (storage '
                                        "HTML), title, space, and version. Use for 'read the page', 'show "
                                        "page content', 'open confluence page' queries.",
                         'method': 'GET',
                         'path': '/content/{page_id}',
                         'requires_scopes': [],
                         'cache_ttl_s': 300,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': False,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'page_id': {   'type': 'string',
                                                                              'description': 'Confluence '
                                                                                             'content/page '
                                                                                             'ID'}},
                                             'required': ['page_id']}},
                     {   'name': 'confluence_create_page',
                         'description': 'Create a new Confluence page in a space with a title and '
                                        'storage-format body. WRITE action — requires explicit user '
                                        'confirmation before sending.',
                         'method': 'POST',
                         'path': '/content',
                         'requires_scopes': [],
                         'cache_ttl_s': 0,
                         'paginated': False,
                         'max_items': 1,
                         'is_write': True,
                         'input_schema': {   'type': 'object',
                                             'properties': {   'space_key': {   'type': 'string',
                                                                                'description': 'Target space '
                                                                                               'key (e.g. '
                                                                                               "'ENG')"},
                                                               'title': {   'type': 'string',
                                                                            'description': 'Page title'},
                                                               'body': {   'type': 'string',
                                                                           'description': 'Page body in '
                                                                                          'Confluence '
                                                                                          'storage (XHTML) '
                                                                                          'format'},
                                                               'parent_id': {   'type': 'string',
                                                                                'description': 'Optional '
                                                                                               'parent page '
                                                                                               'ID to nest '
                                                                                               'the new page '
                                                                                               'under'}},
                                             'required': ['space_key', 'title', 'body']}}]}]

# ── Google Calendar ──────────────────────────────────────────────────────────
# Reuses the Google OAuth client already required by Gmail and Drive, so enabling
# it costs the operator no extra app registration — only the added scope, which
# the user grants on connect.
GOOGLE_CALENDAR_CONNECTORS = [
    {
        "name": "google_calendar",
        "display_name": "Google Calendar",
        "description": (
            "Connect to Google Calendar — list upcoming events, read an event, "
            "and create events. Use for 'my calendar', 'meetings', 'am I free' queries."
        ),
        "icon_url": "/icons/google_calendar.svg",
        "category": "productivity",
        "auth_type": "oauth2",
        "has_custom_adapter": True,
        "rate_limit_per_min": 60,
        "is_builtin": True,
        "base_url": "https://www.googleapis.com/calendar/v3",
        "auth_config": {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "client_id_env": "GOOGLE_CLIENT_ID",
            "client_secret_env": _ENV_GOOGLE_OAUTH_CREDENTIAL,
            "pkce": True,
            "scopes": [
                "openid", "email", "profile",
                "https://www.googleapis.com/auth/calendar.readonly",
                "https://www.googleapis.com/auth/calendar.events",
            ],
            "extra_params": {"access_type": "offline", "prompt": "consent"},
            "revoke_url": "https://oauth2.googleapis.com/revoke",
        },
        "tools": [
            {
                "name": "calendar_list_events",
                "description": (
                    "List calendar events in a date range. Recurring events are "
                    "expanded to individual occurrences. Defaults to the next 14 days."
                ),
                "method": "GET",
                "path": "/calendars/primary/events",
                "requires_scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
                "cache_ttl_s": 120,
                "paginated": True,
                "max_items": 100,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "date_from": {"type": "string", "description": "YYYY-MM-DD or RFC-3339. Defaults to now."},
                        "date_to": {"type": "string", "description": "YYYY-MM-DD or RFC-3339. Inclusive of the whole day."},
                        "search_query": {"type": "string", "description": "Free-text match on title, description, location, attendees"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "calendar_get_event",
                "description": "Read one calendar event in full by its event ID.",
                "method": "GET",
                "path": "/calendars/primary/events/{event_id}",
                "requires_scopes": ["https://www.googleapis.com/auth/calendar.readonly"],
                "cache_ttl_s": 300,
                "paginated": False,
                "max_items": 1,
                "is_write": False,
                "input_schema": {
                    "type": "object",
                    "properties": {"event_id": {"type": "string"}},
                    "required": ["event_id"],
                },
            },
            {
                "name": "calendar_create_event",
                "description": (
                    "Create a calendar event. Requires confirmation before it runs. "
                    "A start with no end becomes a one-hour meeting; date-only "
                    "start and end create an all-day event."
                ),
                "method": "POST",
                "path": "/calendars/primary/events",
                "requires_scopes": ["https://www.googleapis.com/auth/calendar.events"],
                "cache_ttl_s": 0,
                "paginated": False,
                "max_items": 1,
                "is_write": True,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "start": {"type": "string", "description": "YYYY-MM-DD (all-day) or RFC-3339 datetime"},
                        "end": {"type": "string", "description": "Optional. Defaults to one hour after start."},
                        "description": {"type": "string"},
                        "location": {"type": "string"},
                        "attendees": {"type": "string", "description": "Comma-separated email addresses"},
                        "calendar_id": {"type": "string", "description": "Defaults to 'primary'"},
                    },
                    "required": ["title", "start"],
                },
            },
        ],
    },
]
