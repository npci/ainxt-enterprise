# AiNxt Enterprise — documentation index
This directory holds **per-module reference documentation** — 587 pages, one
per significant module, router or component. It is reference material: it describes
what each part of the codebase does, and it assumes you already have the platform
running.

**If you are new, start elsewhere:**

| Start here | For |
|---|---|
| [`GETTING_STARTED.md`](GETTING_STARTED.md) | Prerequisites, install, configuration, first login, optional features, troubleshooting |
| [`../README.md`](../README.md) | What AiNxt is, one-command setup, architecture, LLM configuration |
| [`../SUPPORT.md`](../SUPPORT.md) | Where to look for answers |
| [`../compliance/README.md`](../compliance/README.md) | Third-party component inventories and licensing notes |

## Layout

Pages are filed in topic directories rather than in one flat folder. Grouping is
derived from file names, so a page may reasonably belong to more than one group —
when in doubt use the full listing further down, or your editor's file search.

| Directory | Covers | Pages |
|---|---|---|
| [`reference/`](reference/) | Everything else | 123 |
| [`api/`](api/) | HTTP API & routers | 96 |
| [`documents/`](documents/) | Document processing | 52 |
| [`agents/`](agents/) | Agents & skills | 51 |
| [`models/`](models/) | Models, routing & spend | 29 |
| [`sdlc/`](sdlc/) | SDLC & governance | 29 |
| [`ui/`](ui/) | UI & ABStudio | 26 |
| [`chat/`](chat/) | Chat & collaboration | 24 |
| [`workers/`](workers/) | Background workers & scheduling | 24 |
| [`connectors/`](connectors/) | Connectors & integrations | 21 |
| [`knowledge/`](knowledge/) | Knowledge base, RAG & search | 20 |
| [`workflows/`](workflows/) | Workflows & triggers | 14 |
| [`infrastructure/`](infrastructure/) | Infrastructure & operations | 13 |
| [`storage/`](storage/) | Database & storage | 13 |
| [`security/`](security/) | Security, auth & compliance | 12 |
| [`mcp/`](mcp/) | MCP servers & bridge | 11 |
| [`clients/`](clients/) | CLI, desktop & IDE | 10 |
| [`evaluation/`](evaluation/) | Evaluation & quality | 8 |
| [`buddy/`](buddy/) | Buddy, calendar & tasks | 7 |

`reference/` is the honest remainder: pages whose file name did not place them in
any of the above. It is not a quality judgement, and it is the first place to look
if a page is not where you expected.

Two files stay at this level because they are entry points rather than reference
pages: this index and [`GETTING_STARTED.md`](GETTING_STARTED.md).

The full per-page listing follows, grouped the same way.

## HTTP API & routers  (96)
<details>
<summary>Show pages</summary>

- [admin_router](api/admin_router.md)
- [agents_router](api/agents_router.md)
- [api_agent_chat](api/api_agent_chat.md)
- [api_agent_templates](api/api_agent_templates.md)
- [api_agents](api/api_agents.md)
- [api_catalog](api/api_catalog.md)
- [api_chat](api/api_chat.md)
- [api_deps](api/api_deps.md)
- [api_documents](api/api_documents.md)
- [api_execution](api/api_execution.md)
- [api_factories](api/api_factories.md)
- [api_generation](api/api_generation.md)
- [api_governance](api/api_governance.md)
- [api_kb](api/api_kb.md)
- [api_keys_router](api/api_keys_router.md)
- [api_loops](api/api_loops.md)
- [api_mcp](api/api_mcp.md)
- [api_template_admin](api/api_template_admin.md)
- [api_templates](api/api_templates.md)
- [api_triggers](api/api_triggers.md)
- [api_workflows](api/api_workflows.md)
- [audit_router](api/audit_router.md)
- [auth_router](api/auth_router.md)
- [broadcast_router](api/broadcast_router.md)
- [budget_router](api/budget_router.md)
- [cached_ask_router](api/cached_ask_router.md)
- [chat_router](api/chat_router.md)
- [cli_updates_router](api/cli_updates_router.md)
- [coach_admin_router](api/coach_admin_router.md)
- [coach_router](api/coach_router.md)
- [code_conversations_router](api/code_conversations_router.md)
- [compliance_router](api/compliance_router.md)
- [compliance_scan_router](api/compliance_scan_router.md)
- [connectors_router](api/connectors_router.md)
- [cowork_admin_router](api/cowork_admin_router.md)
- [cowork_conversations_router](api/cowork_conversations_router.md)
- [cowork_dispatch_router](api/cowork_dispatch_router.md)
- [cowork_mcp_router](api/cowork_mcp_router.md)
- [cowork_policy_router](api/cowork_policy_router.md)
- [cowork_projects_router](api/cowork_projects_router.md)
- [cowork_tasks_router](api/cowork_tasks_router.md)
- [cowork_usage_router](api/cowork_usage_router.md)
- [dept_metrics_router](api/dept_metrics_router.md)
- [desktop_router](api/desktop_router.md)
- [digest_hod_router](api/digest_hod_router.md)
- [digest_manager_router](api/digest_manager_router.md)
- [discussions_router](api/discussions_router.md)
- [doc_download_router](api/doc_download_router.md)
- [docs_router](api/docs_router.md)
- [endpoint_mgmt_router](api/endpoint_mgmt_router.md)
- [endpoint_proxy_router](api/endpoint_proxy_router.md)
- [evals_router](api/evals_router.md)
- [feedback_router](api/feedback_router.md)
- [governance_router](api/governance_router.md)
- [graph_webhooks_router](api/graph_webhooks_router.md)
- [ide_router](api/ide_router.md)
- [inbox_router](api/inbox_router.md)
- [index_router](api/index_router.md)
- [jobs_router](api/jobs_router.md)
- [kb_router](api/kb_router.md)
- [knowledge_graph_router](api/knowledge_graph_router.md)
- [llm_spend_report_router](api/llm_spend_report_router.md)
- [mailbox_router](api/mailbox_router.md)
- [marketplace_router](api/marketplace_router.md)
- [mcp_governance_router](api/mcp_governance_router.md)
- [mcp_server_router](api/mcp_server_router.md)
- [memory_router](api/memory_router.md)
- [messages_compat_router](api/messages_compat_router.md)
- [model_governance_router](api/model_governance_router.md)
- [monthly_statement_router](api/monthly_statement_router.md)
- [n8n_router](api/n8n_router.md)
- [notifications_router](api/notifications_router.md)
- [presenton_lib_api_client](api/presenton_lib_api_client.md)
- [presenton_router](api/presenton_router.md)
- [products_router](api/products_router.md)
- [profile_router](api/profile_router.md)
- [projects_router](api/projects_router.md)
- [prompt_mgmt_router](api/prompt_mgmt_router.md)
- [review_router](api/review_router.md)
- [router_policy](api/router_policy.md)
- [sandbox_router](api/sandbox_router.md)
- [scim_router](api/scim_router.md)
- [sdlc_router](api/sdlc_router.md)
- [secure_code_gate_router](api/secure_code_gate_router.md)
- [session_router](api/session_router.md)
- [shared_api_routers](api/shared_api_routers.md)
- [skills_router](api/skills_router.md)
- [slack_router](api/slack_router.md)
- [teams_router](api/teams_router.md)
- [templates_router](api/templates_router.md)
- [threads_router](api/threads_router.md)
- [translation_service_api](api/translation_service_api.md)
- [vault_router](api/vault_router.md)
- [webhooks_router](api/webhooks_router.md)
- [zoho_router](api/zoho_router.md)

</details>

## Agents & skills  (54)
<details>
<summary>Show pages</summary>

- [agent_analytics](agents/agent_analytics.md)
- [agent_factory_pipeline](agents/agent_factory_pipeline.md)
- [agent_management](agents/agent_management.md)
- [agent_orchestration](agents/agent_orchestration.md)
- [agent_system](agents/agent_system.md)
- [agents_catalog](agents/agents_catalog.md)
- [agents_feature](agents/agents_feature.md)
- [agents_feature_card](agents/agents_feature_card.md)
- [agents_feature_dashboard](agents/agents_feature_dashboard.md)
- [agents_feature_editor](agents/agents_feature_editor.md)
- [agents_feature_factory_chat](agents/agents_feature_factory_chat.md)
- [checkpoint_agent_chat_store](agents/checkpoint_agent_chat_store.md)
- [core_agent_framework](agents/core_agent_framework.md)
- [dslar_skills](agents/dslar_skills.md)
- [dslar_skills_dslar_clause_chunking](agents/dslar_skills_dslar_clause_chunking.md)
- [dslar_skills_dslar_image_enrichment](agents/dslar_skills_dslar_image_enrichment.md)
- [dslar_skills_dslar_pdf_extraction](agents/dslar_skills_dslar_pdf_extraction.md)
- [dslar_skills_dslar_report_rendering](agents/dslar_skills_dslar_report_rendering.md)
- [sdlc_agent_loop](agents/sdlc_agent_loop.md)
- [sdlc_pipeline_agents](agents/sdlc_pipeline_agents.md)
- [shared_skills](agents/shared_skills.md)
- [skill_factory_pipeline](agents/skill_factory_pipeline.md)
- [skill_proposals](agents/skill_proposals.md)
- [skills_feature](agents/skills_feature.md)
- [specialized_skills](agents/specialized_skills.md)
- [specialized_skills_dpdp_onboarding](agents/specialized_skills_dpdp_onboarding.md)
- [swarm](agents/swarm.md)
- [swarm_execution](agents/swarm_execution.md)
- [swarm_planning](agents/swarm_planning.md)
- [tools_swarm_spawn](agents/tools_swarm_spawn.md)

</details>

## Document processing  (52)
<details>
<summary>Show pages</summary>

- [core_ocr](documents/core_ocr.md)
- [doc_generation](documents/doc_generation.md)
- [doc_generator](documents/doc_generator.md)
- [docker_execution_tool](documents/docker_execution_tool.md)
- [document_preview](documents/document_preview.md)
- [document_processing](documents/document_processing.md)
- [document_processing_docling_parser](documents/document_processing_docling_parser.md)
- [document_processing_legacy_parser](documents/document_processing_legacy_parser.md)
- [document_processing_paddle_ocr](documents/document_processing_paddle_ocr.md)
- [document_tools](documents/document_tools.md)
- [documents](documents/documents.md)
- [documents_generation](documents/documents_generation.md)
- [documents_guide](documents/documents_guide.md)
- [documents_preview](documents/documents_preview.md)
- [office](documents/office.md)
- [office_addin](documents/office_addin.md)
- [sandbox_docker_execution](documents/sandbox_docker_execution.md)
- [sandbox_document_execution](documents/sandbox_document_execution.md)

</details>

## Knowledge base, RAG & search  (20)
<details>
<summary>Show pages</summary>

- [core_infrastructure_resilience_storage](knowledge/core_infrastructure_resilience_storage.md)
- [embedding_service](knowledge/embedding_service.md)
- [embedding_service_cache](knowledge/embedding_service_cache.md)
- [indexers](knowledge/indexers.md)
- [indexing_and_search](knowledge/indexing_and_search.md)
- [kb_chat](knowledge/kb_chat.md)
- [kb_chat_chat_settings](knowledge/kb_chat_chat_settings.md)
- [kb_chat_core_chat](knowledge/kb_chat_core_chat.md)
- [kb_chat_enhancement_features](knowledge/kb_chat_enhancement_features.md)
- [kb_chat_export_template](knowledge/kb_chat_export_template.md)
- [kb_chat_file_image_handling](knowledge/kb_chat_file_image_handling.md)
- [kb_chat_list](knowledge/kb_chat_list.md)
- [kb_chat_panel](knowledge/kb_chat_panel.md)
- [kb_graph](knowledge/kb_graph.md)
- [kb_search_tools](knowledge/kb_search_tools.md)
- [knowledge_base](knowledge/knowledge_base.md)
- [knowledge_graph](knowledge/knowledge_graph.md)
- [shared_core_knowledge_base](knowledge/shared_core_knowledge_base.md)
- [shared_core_knowledge_base_document_store](knowledge/shared_core_knowledge_base_document_store.md)
- [shared_core_knowledge_base_entity_registry](knowledge/shared_core_knowledge_base_entity_registry.md)

</details>

## Chat & collaboration  (24)
<details>
<summary>Show pages</summary>

- [ChatActions](chat/ChatActions.md)
- [ChatPanel](chat/ChatPanel.md)
- [ChatPanelCore](chat/ChatPanelCore.md)
- [MessageContent](chat/MessageContent.md)
- [ai_ui_frontend_utils_chat_message](chat/ai_ui_frontend_utils_chat_message.md)
- [chat](chat/chat.md)
- [chat_and_messaging](chat/chat_and_messaging.md)
- [chat_settings](chat/chat_settings.md)
- [core_chat](chat/core_chat.md)
- [core_chat_logic](chat/core_chat_logic.md)
- [discussions](chat/discussions.md)
- [discussions_service](chat/discussions_service.md)
- [documents_chat](chat/documents_chat.md)
- [email_broadcast](chat/email_broadcast.md)
- [inbox](chat/inbox.md)
- [memory_system_chat_summarizer](chat/memory_system_chat_summarizer.md)
- [message](chat/message.md)
- [message_actions](chat/message_actions.md)
- [message_meta](chat/message_meta.md)
- [ppt_chat](chat/ppt_chat.md)
- [threads](chat/threads.md)
- [utils_thread_helpers](chat/utils_thread_helpers.md)
- [workflows_feature_editor_chat_panel](chat/workflows_feature_editor_chat_panel.md)
- [workflows_feature_factory_chat](chat/workflows_feature_factory_chat.md)

</details>

## Models, routing & spend  (29)
<details>
<summary>Show pages</summary>

- [app_models](models/app_models.md)
- [browser_automation_extension_llm](models/browser_automation_extension_llm.md)
- [budget](models/budget.md)
- [budget_manager](models/budget_manager.md)
- [budget_team_panel](models/budget_team_panel.md)
- [budget_utilization_view](models/budget_utilization_view.md)
- [claude_gateway](models/claude_gateway.md)
- [core_llm_handler](models/core_llm_handler.md)
- [gateway](models/gateway.md)
- [gemini_gateway](models/gemini_gateway.md)
- [llm_proxy](models/llm_proxy.md)
- [llm_proxy_core_circuit_breaker](models/llm_proxy_core_circuit_breaker.md)
- [llm_proxy_core_claude_cache](models/llm_proxy_core_claude_cache.md)
- [llm_proxy_core_logger](models/llm_proxy_core_logger.md)
- [llm_proxy_core_retry](models/llm_proxy_core_retry.md)
- [llm_proxy_gateway_claude](models/llm_proxy_gateway_claude.md)
- [llm_proxy_gateway_gemini](models/llm_proxy_gateway_gemini.md)
- [llm_proxy_gateway_openai](models/llm_proxy_gateway_openai.md)
- [llm_proxy_main](models/llm_proxy_main.md)
- [llm_spend](models/llm_spend.md)
- [llm_spend_fetchers](models/llm_spend_fetchers.md)
- [local_llm_gateway](models/local_llm_gateway.md)
- [loop_models](models/loop_models.md)
- [model_and_tool_listing](models/model_and_tool_listing.md)
- [model_routing](models/model_routing.md)
- [model_routing_core](models/model_routing_core.md)
- [ollama_gateway](models/ollama_gateway.md)
- [openai_gateway](models/openai_gateway.md)
- [services_budget_digest](models/services_budget_digest.md)

</details>

## SDLC & governance  (29)
<details>
<summary>Show pages</summary>

- [approval_actions](sdlc/approval_actions.md)
- [core_governance](sdlc/core_governance.md)
- [core_governance_client](sdlc/core_governance_client.md)
- [diff_approval](sdlc/diff_approval.md)
- [governance](sdlc/governance.md)
- [governance_actions](sdlc/governance_actions.md)
- [governance_feature](sdlc/governance_feature.md)
- [model_governance](sdlc/model_governance.md)
- [multi_repo_approval](sdlc/multi_repo_approval.md)
- [sdlc_baseline_gate](sdlc/sdlc_baseline_gate.md)
- [sdlc_cli_engine](sdlc/sdlc_cli_engine.md)
- [sdlc_coder_tools](sdlc/sdlc_coder_tools.md)
- [sdlc_gate_signal](sdlc/sdlc_gate_signal.md)
- [sdlc_governance](sdlc/sdlc_governance.md)
- [sdlc_governance_config](sdlc/sdlc_governance_config.md)
- [sdlc_governance_config_2](sdlc/sdlc_governance_config_2.md)
- [sdlc_governance_review](sdlc/sdlc_governance_review.md)
- [sdlc_loop_tools](sdlc/sdlc_loop_tools.md)
- [sdlc_metrics](sdlc/sdlc_metrics.md)
- [sdlc_normalizer](sdlc/sdlc_normalizer.md)
- [sdlc_patch_engine](sdlc/sdlc_patch_engine.md)
- [sdlc_pipeline](sdlc/sdlc_pipeline.md)
- [sdlc_pipeline_core](sdlc/sdlc_pipeline_core.md)
- [sdlc_pipeline_stepper](sdlc/sdlc_pipeline_stepper.md)
- [sdlc_planning_artifact](sdlc/sdlc_planning_artifact.md)
- [sdlc_state_machine](sdlc/sdlc_state_machine.md)
- [sdlc_status_model](sdlc/sdlc_status_model.md)
- [security_and_governance](sdlc/security_and_governance.md)
- [shared_core_sdlc_pipeline](sdlc/shared_core_sdlc_pipeline.md)

</details>

## Connectors & integrations  (21)
<details>
<summary>Show pages</summary>

- [confluence_tools](connectors/confluence_tools.md)
- [connector_adapters](connectors/connector_adapters.md)
- [connector_adapters_enterprise_collab](connectors/connector_adapters_enterprise_collab.md)
- [connector_infrastructure](connectors/connector_infrastructure.md)
- [connectors](connectors/connectors.md)
- [connectors_integrations](connectors/connectors_integrations.md)
- [github_tools](connectors/github_tools.md)
- [gitlab_tools](connectors/gitlab_tools.md)
- [jira_tools](connectors/jira_tools.md)
- [shared_integrations_connector_adapters](connectors/shared_integrations_connector_adapters.md)
- [shared_integrations_connector_adapters_atlassian](connectors/shared_integrations_connector_adapters_atlassian.md)
- [shared_integrations_connector_adapters_cloud_productivity](connectors/shared_integrations_connector_adapters_cloud_productivity.md)
- [shared_integrations_connector_infrastructure](connectors/shared_integrations_connector_infrastructure.md)
- [shared_integrations_connector_infrastructure_dpi_consent](connectors/shared_integrations_connector_infrastructure_dpi_consent.md)
- [shared_integrations_connector_infrastructure_engine](connectors/shared_integrations_connector_infrastructure_engine.md)
- [shared_integrations_connector_infrastructure_mcp_bridge](connectors/shared_integrations_connector_infrastructure_mcp_bridge.md)
- [shared_integrations_connector_infrastructure_metrics](connectors/shared_integrations_connector_infrastructure_metrics.md)
- [shared_integrations_connector_infrastructure_oauth2](connectors/shared_integrations_connector_infrastructure_oauth2.md)
- [shared_integrations_connector_infrastructure_registry](connectors/shared_integrations_connector_infrastructure_registry.md)
- [teams_config](connectors/teams_config.md)
- [teams_integration](connectors/teams_integration.md)

</details>

## Security, auth & compliance  (12)
<details>
<summary>Show pages</summary>

- [auth](security/auth.md)
- [authentication](security/authentication.md)
- [authentication_dependencies](security/authentication_dependencies.md)
- [authentication_ldap](security/authentication_ldap.md)
- [authentication_rbac](security/authentication_rbac.md)
- [authentication_sso](security/authentication_sso.md)
- [decision_engines_compliance](security/decision_engines_compliance.md)
- [guardrails](security/guardrails.md)
- [guardrails_tools](security/guardrails_tools.md)
- [privacy_service](security/privacy_service.md)
- [security_privacy](security/security_privacy.md)
- [security_scan_tools](security/security_scan_tools.md)

</details>

## Background workers & scheduling  (24)
<details>
<summary>Show pages</summary>

- [broadcast_coach_workers](workers/broadcast_coach_workers.md)
- [broadcast_coach_workers_broadcast](workers/broadcast_coach_workers_broadcast.md)
- [broadcast_coach_workers_coach](workers/broadcast_coach_workers_coach.md)
- [broadcast_coach_workers_graph_edges](workers/broadcast_coach_workers_graph_edges.md)
- [chat_agent_execution_workers](workers/chat_agent_execution_workers.md)
- [chat_agent_execution_workers_chat_agent](workers/chat_agent_execution_workers_chat_agent.md)
- [chat_agent_execution_workers_workflow](workers/chat_agent_execution_workers_workflow.md)
- [cowork_scheduler](workers/cowork_scheduler.md)
- [cowork_scheduling_workers](workers/cowork_scheduling_workers.md)
- [cowork_scheduling_workers_scheduler](workers/cowork_scheduling_workers_scheduler.md)
- [cowork_scheduling_workers_task_worker](workers/cowork_scheduling_workers_task_worker.md)
- [document_knowledge_workers](workers/document_knowledge_workers.md)
- [external_integration_workers](workers/external_integration_workers.md)
- [external_integration_workers_codebase_indexing](workers/external_integration_workers_codebase_indexing.md)
- [infrastructure_maintenance_workers](workers/infrastructure_maintenance_workers.md)
- [infrastructure_maintenance_workers_dlq](workers/infrastructure_maintenance_workers_dlq.md)
- [infrastructure_maintenance_workers_memory](workers/infrastructure_maintenance_workers_memory.md)
- [infrastructure_maintenance_workers_purge](workers/infrastructure_maintenance_workers_purge.md)
- [infrastructure_maintenance_workers_scheduling](workers/infrastructure_maintenance_workers_scheduling.md)
- [sdlc_pipeline_workers](workers/sdlc_pipeline_workers.md)
- [services_trigger_scheduler](workers/services_trigger_scheduler.md)
- [worker_orchestration](workers/worker_orchestration.md)
- [workers](workers/workers.md)

</details>

## Database & storage  (13)
<details>
<summary>Show pages</summary>

- [checkpoint_workflow_store](storage/checkpoint_workflow_store.md)
- [core_db_pool](storage/core_db_pool.md)
- [database](storage/database.md)
- [decision_engines_hardblock](storage/decision_engines_hardblock.md)
- [feedback](storage/feedback.md)
- [feedback_and_sharing](storage/feedback_and_sharing.md)
- [kv_store](storage/kv_store.md)
- [kv_store_sync_clients](storage/kv_store_sync_clients.md)
- [profiles_schema](storage/profiles_schema.md)
- [sandbox](storage/sandbox.md)
- [sandbox_image_building](storage/sandbox_image_building.md)
- [store](storage/store.md)
- [store_layer](storage/store_layer.md)

</details>

## Infrastructure & operations  (13)
<details>
<summary>Show pages</summary>

- [ConfigPanel](infrastructure/ConfigPanel.md)
- [config](infrastructure/config.md)
- [core_config](infrastructure/core_config.md)
- [core_infrastructure](infrastructure/core_infrastructure.md)
- [core_infrastructure_config_logging](infrastructure/core_infrastructure_config_logging.md)
- [core_infrastructure_observability](infrastructure/core_infrastructure_observability.md)
- [dept_metrics](infrastructure/dept_metrics.md)
- [gunicorn_config](infrastructure/gunicorn_config.md)
- [health_and_monitoring](infrastructure/health_and_monitoring.md)
- [monitoring](infrastructure/monitoring.md)
- [observability](infrastructure/observability.md)
- [observability_metrics](infrastructure/observability_metrics.md)
- [observability_tracing](infrastructure/observability_tracing.md)

</details>

## UI & ABStudio  (26)
<details>
<summary>Show pages</summary>

- [Canvas](ui/Canvas.md)
- [DebugLogView](ui/DebugLogView.md)
- [LoopItemsPicker](ui/LoopItemsPicker.md)
- [RunSettingsStrip](ui/RunSettingsStrip.md)
- [Sidebar](ui/Sidebar.md)
- [SubflowPicker](ui/SubflowPicker.md)
- [abstudio_backend](ui/abstudio_backend.md)
- [abstudio_frontend](ui/abstudio_frontend.md)
- [ai_ui_frontend](ui/ai_ui_frontend.md)
- [ai_ui_frontend_app_core](ui/ai_ui_frontend_app_core.md)
- [ai_ui_frontend_build_studio](ui/ai_ui_frontend_build_studio.md)
- [ai_ui_frontend_hooks](ui/ai_ui_frontend_hooks.md)
- [ai_ui_frontend_hooks_desktop](ui/ai_ui_frontend_hooks_desktop.md)
- [ai_ui_frontend_utils](ui/ai_ui_frontend_utils.md)
- [ai_ui_frontend_utils_file_preview](ui/ai_ui_frontend_utils_file_preview.md)
- [ai_ui_frontend_utils_ppt](ui/ai_ui_frontend_utils_ppt.md)
- [artifact_views](ui/artifact_views.md)
- [build_studio](ui/build_studio.md)
- [common_components](ui/common_components.md)
- [cowork_canvas](ui/cowork_canvas.md)
- [overview](ui/overview.md)
- [presenton_lib_payload_builder](ui/presenton_lib_payload_builder.md)
- [scope_picker](ui/scope_picker.md)
- [ui_dialog](ui/ui_dialog.md)
- [workflow_preview](ui/workflow_preview.md)
- [workflows_feature_editor_canvas](ui/workflows_feature_editor_canvas.md)

</details>

## CLI, desktop & IDE  (10)
<details>
<summary>Show pages</summary>

- [cli_runtime](clients/cli_runtime.md)
- [cli_runtime_runner](clients/cli_runtime_runner.md)
- [cli_runtime_session](clients/cli_runtime_session.md)
- [desktop_app](clients/desktop_app.md)
- [desktop_app_browser_automation](clients/desktop_app_browser_automation.md)
- [desktop_app_computer_use](clients/desktop_app_computer_use.md)
- [desktop_app_main_process](clients/desktop_app_main_process.md)
- [level_overrides](clients/level_overrides.md)
- [middleware_client_source](clients/middleware_client_source.md)
- [pptx_add_slide](clients/pptx_add_slide.md)

</details>

## Cowork, calendar & tasks  (7)
<details>
<summary>Show pages</summary>

- [calendar_tools](buddy/calendar_tools.md)
- [cowork_desktop](buddy/cowork_desktop.md)
- [cowork_enterprise](buddy/cowork_enterprise.md)
- [cowork_settings](buddy/cowork_settings.md)
- [desktop_app_cowork_engine](buddy/desktop_app_cowork_engine.md)
- [memory_system_cowork_memory](buddy/memory_system_cowork_memory.md)
- [task_tracker_tools](buddy/task_tracker_tools.md)

</details>

## Evaluation & quality  (8)
<details>
<summary>Show pages</summary>

- [coach](evaluation/coach.md)
- [coach_admin](evaluation/coach_admin.md)
- [coach_system](evaluation/coach_system.md)
- [engine_loop_evaluator](evaluation/engine_loop_evaluator.md)
- [evals_dashboard](evaluation/evals_dashboard.md)
- [evals_evolution](evaluation/evals_evolution.md)
- [evals_evolution_evaluation](evaluation/evals_evolution_evaluation.md)
- [evals_evolution_tier2](evaluation/evals_evolution_tier2.md)

</details>

## Other reference  (149)
<details>
<summary>Show pages</summary>

- [FileHandling](reference/FileHandling.md)
- [GETTING_STARTED](GETTING_STARTED.md)
- [advanced_reasoning](reference/advanced_reasoning.md)
- [ainxt_scripts](reference/ainxt_scripts.md)
- [app_core](reference/app_core.md)
- [app_main](reference/app_main.md)
- [artifacts_panel](reference/artifacts_panel.md)
- [ats_tools](reference/ats_tools.md)
- [audit_and_tracing](reference/audit_and_tracing.md)
- [brand_mark](reference/brand_mark.md)
- [browser_automation_extension](reference/browser_automation_extension.md)
- [browser_automation_extension_background](reference/browser_automation_extension_background.md)
- [browser_automation_extension_content](reference/browser_automation_extension_content.md)
- [checkpoint](reference/checkpoint.md)
- [cil](reference/cil.md)
- [cil_intent](reference/cil_intent.md)
- [cil_lexical](reference/cil_lexical.md)
- [cil_policy](reference/cil_policy.md)
- [ckms](reference/ckms.md)
- [code](reference/code.md)
- [code_block](reference/code_block.md)
- [code_editor](reference/code_editor.md)
- [code_editor_diff](reference/code_editor_diff.md)
- [code_editor_explorer](reference/code_editor_explorer.md)
- [code_editor_panel](reference/code_editor_panel.md)
- [codebase_manager](reference/codebase_manager.md)
- [compression_service](reference/compression_service.md)
- [constants](reference/constants.md)
- [context_engine](reference/context_engine.md)
- [core_factory_utils](reference/core_factory_utils.md)
- [core_mcp_manager](reference/core_mcp_manager.md)
- [core_workflow_repo](reference/core_workflow_repo.md)
- [data_tools](reference/data_tools.md)
- [decision_engines](reference/decision_engines.md)
- [decision_engines_core](reference/decision_engines_core.md)
- [dep_table](reference/dep_table.md)
- [dependency_utilities](reference/dependency_utilities.md)
- [dependency_utilities_manifest_writer](reference/dependency_utilities_manifest_writer.md)
- [dependency_utilities_resolution](reference/dependency_utilities_resolution.md)
- [dev_workspace](reference/dev_workspace.md)
- [dpi_consent](reference/dpi_consent.md)
- [email_tools](reference/email_tools.md)
- [endpoint_manager](reference/endpoint_manager.md)
- [engine_native_engine](reference/engine_native_engine.md)
- [enhancement_features](reference/enhancement_features.md)
- [export_template](reference/export_template.md)
- [file_and_asset_serving](reference/file_and_asset_serving.md)
- [file_image_handling](reference/file_image_handling.md)
- [history_panel](reference/history_panel.md)
- [hooks](reference/hooks.md)
- [kafka_event_consumer](reference/kafka_event_consumer.md)
- [layout_helpers](reference/layout_helpers.md)
- [lms_tools](reference/lms_tools.md)
- [local_files](reference/local_files.md)
- [login](reference/login.md)
- [loop_runner](reference/loop_runner.md)
- [manifest](reference/manifest.md)
- [mcp_servers](mcp/mcp_servers.md)
- [mcp_servers_base](mcp/mcp_servers_base.md)
- [mcp_servers_collaboration](mcp/mcp_servers_collaboration.md)
- [mcp_servers_content](mcp/mcp_servers_content.md)
- [mcp_servers_data](mcp/mcp_servers_data.md)
- [mcp_servers_platform](mcp/mcp_servers_platform.md)
- [mcp_servers_productivity](mcp/mcp_servers_productivity.md)
- [mcp_system](mcp/mcp_system.md)
- [mcp_system_registry](mcp/mcp_system_registry.md)
- [mcp_system_registry_master](mcp/mcp_system_registry_master.md)
- [mcp_system_registry_tools](mcp/mcp_system_registry_tools.md)
- [memory](reference/memory.md)
- [memory_panel](reference/memory_panel.md)
- [memory_system](reference/memory_system.md)
- [middleware](reference/middleware.md)
- [middleware_request_id](reference/middleware_request_id.md)
- [n8n_tools](reference/n8n_tools.md)
- [navigator_activity](reference/navigator_activity.md)
- [open_questions](reference/open_questions.md)
- [openai_compatible_endpoints](reference/openai_compatible_endpoints.md)
- [pipeline](reference/pipeline.md)
- [pipeline_core](reference/pipeline_core.md)
- [ppt_detection](reference/ppt_detection.md)
- [ppt_wizard](reference/ppt_wizard.md)
- [pptx_clean](reference/pptx_clean.md)
- [pptx_thumbnail](reference/pptx_thumbnail.md)
- [presenton_lib](reference/presenton_lib.md)
- [presenton_lib_layout_mapping](reference/presenton_lib_layout_mapping.md)
- [presenton_lib_layout_registry](reference/presenton_lib_layout_registry.md)
- [presenton_lib_stream_reader](reference/presenton_lib_stream_reader.md)
- [presenton_patches](reference/presenton_patches.md)
- [product_manager](reference/product_manager.md)
- [profile](reference/profile.md)
- [profiles](reference/profiles.md)
- [profiles_resolution](reference/profiles_resolution.md)
- [profiles_routing](reference/profiles_routing.md)
- [profiles_shaping](reference/profiles_shaping.md)
- [projects](reference/projects.md)
- [reaction_engines](reference/reaction_engines.md)
- [reaction_engines_react_loop](reference/reaction_engines_react_loop.md)
- [reaction_engines_recovery](reference/reaction_engines_recovery.md)
- [run_diff_tools](reference/run_diff_tools.md)
- [scripts](reference/scripts.md)
- [scripts_utilities](reference/scripts_utilities.md)
- [services](reference/services.md)
- [services_services](reference/services_services.md)
- [shared_core](reference/shared_core.md)
- [shared_core_tools](reference/shared_core_tools.md)
- [shared_features](reference/shared_features.md)
- [shared_integrations](reference/shared_integrations.md)
- [skeleton](reference/skeleton.md)
- [spinner](reference/spinner.md)
- [templates_feature](reference/templates_feature.md)
- [tool_integration](reference/tool_integration.md)
- [tool_utilities](reference/tool_utilities.md)
- [tools](reference/tools.md)
- [tools_canonical_seed](reference/tools_canonical_seed.md)
- [tools_feature](reference/tools_feature.md)
- [tools_m365_bridge](reference/tools_m365_bridge.md)
- [trace_panel](reference/trace_panel.md)
- [translation_service](reference/translation_service.md)
- [translation_service_cache](reference/translation_service_cache.md)
- [translation_service_engine](reference/translation_service_engine.md)
- [translator_tools](reference/translator_tools.md)
- [trigger_modal](reference/trigger_modal.md)
- [triggers_feature](reference/triggers_feature.md)
- [user_management](reference/user_management.md)
- [utils](reference/utils.md)
- [utils_editor_persistence](reference/utils_editor_persistence.md)
- [utils_make_id](reference/utils_make_id.md)
- [voice_and_tts](reference/voice_and_tts.md)
- [voice_mic](reference/voice_mic.md)
- [voice_mode](reference/voice_mode.md)
- [whisper_service](reference/whisper_service.md)
- [work_item_panel](reference/work_item_panel.md)
- [workflow_editor](workflows/workflow_editor.md)
- [workflow_editor_conditions](workflows/workflow_editor_conditions.md)
- [workflow_editor_conditions_cases](workflows/workflow_editor_conditions_cases.md)
- [workflow_editor_conditions_loop](workflows/workflow_editor_conditions_loop.md)
- [workflow_editor_edges](workflows/workflow_editor_edges.md)
- [workflow_editor_nodes](workflows/workflow_editor_nodes.md)
- [workflow_editor_nodes_branching](workflows/workflow_editor_nodes_branching.md)
- [workflow_editor_nodes_control_flow](workflows/workflow_editor_nodes_control_flow.md)
- [workflow_editor_nodes_execution](workflows/workflow_editor_nodes_execution.md)
- [workflow_factory_pipeline](workflows/workflow_factory_pipeline.md)
- [workflow_management](workflows/workflow_management.md)
- [workflow_system](workflows/workflow_system.md)
- [workflows_feature](workflows/workflows_feature.md)
- [workflows_feature_dashboard](workflows/workflows_feature_dashboard.md)
- [workspace_utilities](reference/workspace_utilities.md)

</details>

## Generated artifacts

Not documentation pages — output from the tooling that produced this directory:

- `first_module_tree.json`
- `index.html`
- `metadata.json`
- `module_tree.json`
