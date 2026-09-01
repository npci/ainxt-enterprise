# Discussions Router

## Overview

The **Discussions Router** (`routers/discussions_router.py`) is the single API surface for the platform's internal Q&A / forum system. It exposes a FastAPI router mounted at `/discussions` and implements a **dual-write + mirror-read** architecture:

- **Writes** (ask, answer, vote, comment, accept, edit, delete) are sent to a headless **Apache Answer** engine running on `127.0.0.1` only, and simultaneously mirrored into AiNxt's own Postgres tables in the *same request*.
- **Reads** (list questions, get question detail, list comments, experts, stats) are served directly from the Postgres mirror tables — no engine round trip.
- Every meaningful action is logged to an append-only `discussions_events` table that serves as a **feedback spine** for future self-improvement workers.

The browser never talks to the Apache Answer engine directly. All engine communication is server-to-server via [`core/discussions_engine_client.py`](../reference/shared_core.md), which transparently handles per-user session tokens.

---

## Architecture

```mermaid
graph TB
    subgraph Browser["Browser (ai-ui)"]
        UI["Discussions.jsx<br/>Frontend Component"]
    end

    subgraph Gateway["Gateway / FastAPI"]
        Router["discussions_router.py<br/>APIRouter /discussions"]
    end

    subgraph Engine["Apache Answer (Headless)"]
        AnswerAPI["Answer REST API<br/>127.0.0.1:8010 only"]
        Uploads["Upload Disk Store"]
    end

    subgraph Postgres["AiNxt Postgres"]
        Q["discussions_questions"]
        A["discussions_answers"]
        C["discussions_comments"]
        V["discussions_votes"]
        E["discussions_events"]
        BR["discussions_bot_runs"]
        NG["discussion_notify_groups"]
        U["users"]
    end

    subgraph Workers["Background Workers"]
        BotJob["discussions_svc<br/>agent_bridge.py<br/>@AiNxt Bot"]
        EmailJob["discussion_notify.py<br/>Email Sender"]
        Inbox["inbox_store.py<br/>SSE Inbox Push"]
    end

    UI -->|"HTTP /discussions/*"| Router
    Router -->|"write: engine call"| AnswerAPI
    Router -->|"write: mirror row"| Q
    Router -->|"write: mirror row"| A
    Router -->|"write: mirror row"| C
    Router -->|"write: mirror row"| V
    Router -->|"log event"| E
    Router -->|"read: mirror only"| Q
    Router -->|"read: mirror only"| A
    Router -->|"read: mirror only"| C
    Router -->|"read: engine passthrough"| AnswerAPI
    Router -->|"enqueue bot job"| BotJob
    Router -->|"enqueue email job"| EmailJob
    Router -->|"publish inbox item"| Inbox
    Router -->|"resolve authors"| U
    Router -->|"serve uploaded file"| Uploads
    BotJob -->|"post reply"| AnswerAPI
    BotJob -->|"mirror reply"| A
    BotJob -->|"log event"| E
    EmailJob -->|"send email"| External["SMTP / Relay"]
    Inbox -->|"SSE push"| Browser
```

### Design Principles

| Principle | Implementation |
|---|---|
| **Single kill switch** | Deleting this file + one `ENABLE_DISCUSSIONS` gate line in `gateway.py` disables the entire module. |
| **Engine is headless** | Apache Answer runs on localhost only; no browser can reach it. All calls go through the router → engine client. |
| **Mirror-first reads** | Reads never touch the engine — they query AiNxt's own Postgres tables for speed and to enrich with user identity (name, department). |
| **Dual-write consistency** | Every write hits the engine first, then the mirror in the same DB transaction. If the engine call fails, the mirror write never happens. |
| **Redact-and-proceed** | Compliance checks never hard-block a post — sensitive content is redacted in place and the post proceeds. |
| **Non-blocking notifications** | Email and inbox notifications are enqueued to RQ; failures never roll back an already-committed post. |

---

## Component Map

```mermaid
graph LR
    subgraph Router["discussions_router.py"]
        subgraph Routes["Route Handlers"]
            AQ["ask_question<br/>POST /questions"]
            LQ["list_questions<br/>GET /questions"]
            GQ["get_question<br/>GET /questions/:id"]
            PA["post_answer<br/>POST /questions/:id/answers"]
            VT["vote<br/>POST /:type/:id/vote"]
            AC["accept<br/>POST /questions/:id/accept"]
            LC["list_comments<br/>GET /:type/:id/comments"]
            PC["post_comment<br/>POST /:type/:id/comments"]
            DQ["delete_question<br/>DELETE /questions/:id"]
            DA["delete_answer<br/>DELETE /answers/:id"]
            DC["delete_comment<br/>DELETE /:type/:id/comments/:cid"]
            EQ["edit_question<br/>PUT /questions/:id"]
            EA["edit_answer<br/>PUT /answers/:id"]
            EC["edit_comment<br/>PUT /:type/:id/comments/:cid"]
            UL["upload_image<br/>POST /upload"]
            GU["get_upload<br/>GET /uploads/:path"]
            LT["list_tags<br/>GET /tags"]
            LB["list_badges<br/>GET /badges"]
            LU["list_users<br/>GET /users"]
            MB["my_badges<br/>GET /badges/mine"]
            LE["list_experts<br/>GET /experts"]
            DS["get_discussion_stats<br/>GET /stats"]
        end

        subgraph Helpers["Helper Functions"]
            RA["_resolve_authors"]
            AF["_author_fields"]
            RC["_redact_and_check"]
            HR["_humanize_reason"]
            EG["_engine"]
            LE2["_log_event"]
            MTB["_maybe_trigger_bot"]
            NP["_notify_people"]
            NGE["_notify_group_emails"]
            RO["_require_owner"]
        end

        subgraph Models["Request Models"]
            AQR["AskQuestionReq"]
            PAR["PostAnswerReq"]
            VR["VoteReq"]
            AAR["AcceptAnswerReq"]
            CR["CommentReq"]
            EQR["EditQuestionReq"]
            EAR["EditAnswerReq"]
            ECR["EditCommentReq"]
        end
    end
```

---

## Request Models

All request bodies are Pydantic `BaseModel` subclasses validated by FastAPI before the handler runs.

| Model | Used By | Fields |
|---|---|---|
| `AskQuestionReq` | `ask_question` | `title: str`, `content: str`, `tags: list[str]`, `notify_emails: list[str]` |
| `PostAnswerReq` | `post_answer` | `content: str` |
| `VoteReq` | `vote` | `direction: int` (1 or -1) |
| `AcceptAnswerReq` | `accept` | `answer_id: str` |
| `CommentReq` | `post_comment` | `content: str` |
| `EditQuestionReq` | `edit_question` | `title: str`, `content: str`, `tags: list[str]` |
| `EditAnswerReq` | `edit_answer` | `content: str` |
| `EditCommentReq` | `edit_comment` | `content: str` |

---

## Key Helper Functions

### `_redact_and_check(text, stage) → str`

Runs user-submitted text through the [`ComplianceEngine`](../reference/shared_core.md) (`agents/compliance_engine.py`). Per house rule, the discussions module **never hard-blocks** a post — it returns the redacted text and logs any blocking findings as warnings. On any exception, the original text is returned unchanged (fail-open).

### `_engine(coro) → result`

Awaits an engine client coroutine and translates `httpx.HTTPStatusError` (4xx from Apache Answer) into a proper `HTTPException(422)` with a human-readable detail. It inspects the engine's `RespBody` for:
- Per-field form validation errors (in `data` as a list of `FormErrorField`)
- A human-readable `msg` (if it's not a raw i18n key)
- A `reason` code that gets humanized via `_humanize_reason()`

### `_resolve_authors(db, user_ids) → dict`

Batch-resolves `author_user_id` (raw JWT `sub`) to `{name, department}` by joining against the `users` table. This is essential because discussions rows store only the bare UUID — every read needs this join to display a real name instead of a UUID. The system bot (`ainxt-system-bot`) is pre-populated as `{"name": "AiNxt", "department": None}`.

### `_log_event(db, event_type, actor_user_id, ...)`

Writes a single row to `discussions_events` — the append-only feedback spine. Event types include: `question_asked`, `answer_posted`, `vote_cast`, `answer_accepted`, `comment_posted`, `question_edited`, `answer_edited`, `comment_edited`, `question_deleted`, `answer_deleted`, `comment_deleted`, `ainxt_mentioned`, `ainxt_replied`.

### `_maybe_trigger_bot(db, content, mirror_id, ...)`

Scans post content for `@AiNxt` mentions. If found:
1. Creates a `DiscussionsBotRun` row with status `pending`.
2. Logs an `ainxt_mentioned` event.
3. Enqueues an RQ job via `enqueue_discussions_job()` to the dedicated discussions queue.
4. If the queue is unavailable (back-pressure), the row stays `pending` — the user's own post is never failed over a bot trigger.

The bot job is consumed by [`services/discussions_svc/agent_bridge.py`](../chat/discussions_service.md)::`run_discussions_bot_job`, which runs the `AgentRunner` to generate a reply and posts it back to both the engine and the mirror.

### `_notify_people(db, emails, current_user, ...)`

Notifies tagged recipients about a newly-posted discussion:
- **Internal users** (matched by email in `users` table) receive an **in-app inbox item** (SSE-pushed via `publish_inbox_item`) **and** an email.
- **External/unknown emails** receive an **email only**.

Recipients are merged from the poster's `notify_emails` list and the DB-managed `discussion_notify_groups` flat list. Email delivery is fanned out to RQ (retry + DLQ) so it never blocks the poster's request. All failures are swallowed — the post has already been committed.

### `_require_owner(row, current_user)`

Author-only gate for delete and edit operations. Raises `403` if `row.author_user_id != current_user["sub"]`. This is defense-in-depth — the engine enforces its own author check server-side as well.

---

## Data Flow

### Write Path (e.g., `ask_question`)

```mermaid
sequenceDiagram
    participant Browser
    participant Router as discussions_router
    participant Compliance as ComplianceEngine
    participant Engine as Apache Answer
    participant DB as Postgres Mirror
    participant RQ as Job Queue
    participant Inbox as Inbox Store

    Browser->>Router: POST /discussions/questions
    Router->>Compliance: _redact_and_check(title, content)
    Compliance-->>Router: redacted text
    Router->>Engine: engine_create_question(title, content, tags)
    Engine-->>Router: { data: { id: "external_id" } }
    Router->>DB: INSERT DiscussionsQuestion (mirror row)
    Router->>DB: INSERT DiscussionsEvent (question_asked)
    Router->>Router: _maybe_trigger_bot(content)
    Router->>DB: INSERT DiscussionsBotRun (if @AiNxt)
    Router->>RQ: enqueue_discussions_job (if @AiNxt)
    Router->>DB: COMMIT
    Router->>Router: _notify_people(emails)
    Router->>Inbox: publish_inbox_item (internal users)
    Router->>RQ: enqueue email job (all recipients)
    Router-->>Browser: { id, external_id }
```

### Read Path (e.g., `get_question`)

```mermaid
sequenceDiagram
    participant Browser
    participant Router as discussions_router
    participant DB as Postgres Mirror

    Browser->>Router: GET /discussions/questions/:id
    Router->>DB: SELECT DiscussionsQuestion WHERE id = :id
    Router->>DB: SELECT DiscussionsAnswer WHERE question_id = :id
    Router->>DB: SELECT DiscussionsVote WHERE user_id = me AND target_id IN (...)
    Router->>DB: SELECT User (batch resolve authors)
    DB-->>Router: question + answers + votes + author info
    Router-->>Browser: { question, answers[], my_votes{} }
```

> **Note:** Reads never touch the Apache Answer engine. All data is served from the Postgres mirror, enriched with user identity (name, department) and the current user's vote state.

### Delete Path (e.g., `delete_question`)

```mermaid
sequenceDiagram
    participant Browser
    participant Router as discussions_router
    participant Engine as Apache Answer
    participant DB as Postgres Mirror

    Browser->>Router: DELETE /discussions/questions/:id
    Router->>DB: SELECT question (ownership check)
    Router->>Router: _require_owner(question, user)
    Router->>Engine: engine_delete_question(external_id)
    Note over Engine: Pre-clears title/content via PUT<br/>(dedupe workaround), then soft-deletes
    Router->>DB: DELETE comments + votes for question + its answers
    Router->>DB: DELETE question (answers cascade)
    Router->>DB: INSERT DiscussionsEvent (question_deleted)
    Router->>DB: COMMIT
    Router-->>Browser: { deleted: id }
```

### @AiNxt Bot Trigger Flow

```mermaid
sequenceDiagram
    participant Router as discussions_router
    participant DB as Postgres
    participant RQ as Discussions Queue
    participant Worker as discussions_svc worker
    participant Agent as AgentRunner
    participant Engine as Apache Answer

    Router->>Router: _maybe_trigger_bot(content with @AiNxt)
    Router->>DB: INSERT DiscussionsBotRun (status=pending)
    Router->>DB: INSERT DiscussionsEvent (ainxt_mentioned)
    Router->>RQ: enqueue_discussions_job(run_id, payload)
    RQ->>Worker: run_discussions_bot_job(payload)
    Worker->>DB: UPDATE BotRun status=running
    Worker->>Engine: get_question_content(question_id)
    Worker->>Agent: agent_runner.run(DISCSSIONS_BOT_AGENT_NAME, message)
    Agent-->>Worker: { answer, compliance_flags }
    Worker->>Engine: create_answer(question_id, answer)
    Engine-->>Worker: { data: { info: { id } } }
    Worker->>DB: INSERT DiscussionsAnswer (mirror)
    Worker->>DB: UPDATE question.answer_count += 1
    Worker->>DB: INSERT DiscussionsEvent (ainxt_replied)
    Worker->>DB: UPDATE BotRun status=complete
```

---

## API Endpoints

### Questions

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `POST` | `/discussions/questions` | `ask_question` | User | Create a new question. Redacts content, writes to engine + mirror, triggers bot/notifications. |
| `GET` | `/discussions/questions` | `list_questions` | User | List questions with filtering (tag, unanswered, mine, status, search) and sorting (newest, active, votes). Served from mirror. |
| `GET` | `/discussions/questions/{id}` | `get_question` | User | Get a single question with all answers, comments, and the current user's vote state. Served from mirror. |
| `PUT` | `/discussions/questions/{id}` | `edit_question` | Owner | Edit question title/content/tags. Engine first, then mirror. |
| `DELETE` | `/discussions/questions/{id}` | `delete_question` | Owner | Delete question + cascaded answers/comments/votes. Engine first, then mirror. |

### Answers

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `POST` | `/discussions/questions/{id}/answers` | `post_answer` | User | Post an answer. Redacts content, writes to engine + mirror, triggers bot. |
| `PUT` | `/discussions/answers/{id}` | `edit_answer` | Owner | Edit answer content. Engine first, then mirror. |
| `DELETE` | `/discussions/answers/{id}` | `delete_answer` | Owner | Delete answer, decrement parent's count, clear accepted pointer if needed. |
| `POST` | `/discussions/questions/{id}/accept` | `accept` | User | Accept an answer. Engine first, then mirror (un-accepts previous if any). |

### Votes

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `POST` | `/discussions/{type}/{id}/vote` | `vote` | User | Cast or update a vote (±1). Engine first, then mirror (upsert vote row, update count). |

### Comments

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `GET` | `/discussions/{type}/{id}/comments` | `list_comments` | None | List comments for a question or answer. Served from mirror. |
| `POST` | `/discussions/{type}/{id}/comments` | `post_comment` | User | Post a comment. Redacts content, writes to engine + mirror, triggers bot. |
| `PUT` | `/discussions/{type}/{id}/comments/{cid}` | `edit_comment` | Owner | Edit comment content. Engine first, then mirror. |
| `DELETE` | `/discussions/{type}/{id}/comments/{cid}` | `delete_comment` | Owner | Delete comment, decrement parent's comment count. |

### Metadata & Gamification

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `GET` | `/discussions/tags` | `list_tags` | None | List tags (engine passthrough). |
| `GET` | `/discussions/badges` | `list_badges` | None | List all badges (engine passthrough). |
| `GET` | `/discussions/badges/mine` | `my_badges` | User | List current user's earned badges (engine passthrough). |
| `GET` | `/discussions/users` | `list_users` | None | User reputation ranking (engine passthrough). |
| `GET` | `/discussions/experts` | `list_experts` | User | Top answerers per tag (computed from mirror — no engine round trip). |
| `GET` | `/discussions/stats` | `get_discussion_stats` | Admin | Total/replied/closed counts per discussion type (question, feedback, issue). |

### File Uploads

| Method | Path | Handler | Auth | Description |
|---|---|---|---|---|
| `POST` | `/discussions/upload` | `upload_image` | User | Upload a file to the engine's disk store. Returns a gateway-relative URL. |
| `GET` | `/discussions/uploads/{path}` | `get_upload` | None | Serve an uploaded file's bytes from disk (path-traversal guarded). No auth — inline images must render for anyone who can see the post. |

---

## Dependencies

```mermaid
graph TD
    Router["discussions_router.py"]

    Router -->|"auth"| AuthDep["auth/dependencies.py<br/>get_current_user"]
    Router -->|"compliance"| CompEngine["agents/compliance_engine.py<br/>compliance_engine"]
    Router -->|"engine calls"| EngineClient["core/discussions_engine_client.py<br/>18+ async functions"]
    Router -->|"job queue"| JobQueue["core/job_queue.py<br/>enqueue_discussions_job, enqueue_job"]
    Router -->|"logging"| Logger["core/logger.py<br/>logger"]
    Router -->|"database"| DB["db/database.py<br/>get_db"]
    Router -->|"ORM models"| Models["db/models.py<br/>DiscussionsQuestion, Answer,<br/>Comment, Vote, Event,<br/>BotRun, NotifyGroup, User"]
    Router -->|"inbox"| Inbox["store/inbox_store.py<br/>publish_inbox_item"]
    Router -->|"email (via RQ)"| EmailSvc["services/discussion_notify.py<br/>send_discussion_email"]
    Router -->|"bot (via RQ)"| BotSvc["services/discussions_svc/<br/>agent_bridge.py<br/>run_discussions_bot_job"]

    EngineClient -->|"HTTP"| Answer["Apache Answer Engine<br/>127.0.0.1:8010"]
    BotSvc -->|"runs agent"| AgentBuilder["agents/agent_builder.py<br/>AgentRunner"]
    BotSvc -->|"posts reply"| EngineClient
```

### External Module References

| Dependency | Module | Purpose |
|---|---|---|
| `auth.dependencies.get_current_user` | [shared_core](../reference/shared_core.md) (authentication) | JWT-based user authentication; provides `current_user` dict with `sub`, `email`, `name`, `role`. |
| `agents.compliance_engine.compliance_engine` | [shared_core](../reference/shared_core.md) (agent_system) | PII/sensitive content redaction on all user-submitted text. |
| `core.discussions_engine_client.*` | [shared_core](../reference/shared_core.md) (core_infrastructure) | Server-to-server HTTP client for Apache Answer; handles per-user session token minting/caching. |
| `core.job_queue.enqueue_discussions_job` | [shared_core](../reference/shared_core.md) (core_infrastructure) | Enqueue @AiNxt bot reply jobs to a dedicated discussions RQ queue. |
| `core.job_queue.enqueue_job` | [shared_core](../reference/shared_core.md) (core_infrastructure) | Enqueue email notification jobs to the default RQ queue. |
| `db.database.get_db` | [shared_core](../reference/shared_core.md) (database) | SQLAlchemy session factory. |
| `db.models.*` | [shared_core](../reference/shared_core.md) (database) | ORM models for all mirror tables + `User` + `DiscussionNotifyGroup` + `DiscussionsBotRun`. |
| `store.inbox_store.publish_inbox_item` | [shared_core](../reference/shared_core.md) (store_layer) | Publish in-app inbox items with SSE push for real-time delivery. |
| `services.discussion_notify.send_discussion_email` | [shared_core](../reference/shared_core.md) (services) | RQ worker that renders and sends discussion mention emails. |
| `services.discussions_svc.agent_bridge.run_discussions_bot_job` | [discussions_service](../chat/discussions_service.md) | RQ worker that runs the @AiNxt bot agent and posts its reply. |

---

## Database Schema (Mirror Tables)

The router reads from and writes to the following Postgres tables, all defined in [`db/models.py`](../reference/shared_core.md):

```mermaid
erDiagram
    DiscussionsQuestion ||--o{ DiscussionsAnswer : "has answers"
    DiscussionsQuestion ||--o{ DiscussionsComment : "has comments (target_id)"
    DiscussionsQuestion ||--o{ DiscussionsVote : "has votes (target_id)"
    DiscussionsAnswer ||--o{ DiscussionsComment : "has comments (target_id)"
    DiscussionsAnswer ||--o{ DiscussionsVote : "has votes (target_id)"
    DiscussionsQuestion ||--|| DiscussionsAnswer : "accepted_answer_id → answer.id"

    DiscussionsQuestion {
        UUID id PK
        String external_id "Engine's own question id"
        String author_user_id "JWT sub"
        String title
        Text content
        JSONB tags
        Integer vote_count
        Integer answer_count
        Integer comment_count
        UUID accepted_answer_id
        DateTime created_at
        DateTime updated_at
    }

    DiscussionsAnswer {
        UUID id PK
        String external_id
        UUID question_id FK
        String author_user_id
        Text content
        Integer vote_count
        Boolean is_accepted
        Integer comment_count
        DateTime created_at
        DateTime updated_at
    }

    DiscussionsComment {
        UUID id PK
        String external_id
        String target_type "question|answer"
        UUID target_id "question.id or answer.id"
        String author_user_id
        Text content
        DateTime created_at
        DateTime updated_at
    }

    DiscussionsVote {
        UUID id PK
        String target_type "question|answer"
        UUID target_id
        String user_id
        Integer direction "1 or -1"
    }

    DiscussionsEvent {
        UUID id PK
        String event_type
        String actor_user_id
        String target_type
        UUID target_id
        JSONB payload
        DateTime created_at
    }
```

> **Note:** Comments and votes are **not** FK-linked to their target — they key on `target_id` which can be either a question or answer UUID. This is why deletes must explicitly sweep comments/votes by `target_id`.

---

## Compliance & Security

### Redact-and-Proceed Policy

All user-submitted text (question titles, content, answers, comments) passes through `_redact_and_check()` before being sent to the engine or stored in the mirror. The [`ComplianceEngine`](../reference/shared_core.md) validates input and returns:
- `redacted_text`: the text with sensitive content redacted
- `findings`: list of detected issues with `blocked` flags

The discussions module **never hard-blocks** — it uses the redacted text and logs warnings for any blocking findings. This is a deliberate house rule: an internal forum should not silently reject posts.

### Ownership Enforcement

Delete and edit operations use `_require_owner()` to verify that `row.author_user_id == current_user["sub"]`. This check is authoritative on the AiNxt side — the client never gates the button. The engine also enforces its own author check server-side as defense-in-depth.

### Upload Path Traversal Guard

`get_upload()` uses `os.path.realpath()` to resolve the requested path and verifies it stays within the configured `DISCUSSIONS_ENGINE_UPLOAD_PATH` base directory. Any escape attempt raises `FileNotFoundError` → HTTP 404.

---

## Engine Error Humanization

Apache Answer returns error responses with a `RespBody` structure containing `reason` (a dotted i18n key like `error.answer.restrict_answer`) and `msg`. Some reason codes have **no translation** in any language bundle, causing `Tr()` to return the raw key verbatim.

The `_engine()` helper and `_humanize_reason()` function handle this:

1. If `data` is a list of form validation errors → join `error_msg` fields.
2. If `msg` is present and doesn't look like a raw key → use it directly.
3. Otherwise → humanize the `reason`/`msg` by stripping the `error.` prefix and replacing `.` and `_` with spaces.

A curated `_FRIENDLY_REASONS` dict provides custom messages for known tricky codes (e.g., `error.answer.restrict_answer` → "You've already posted a reply to this discussion...").

---

## Frontend Integration

The router is consumed by the [`Discussions.jsx`](../ui/ai_ui_frontend.md) component in the ai-ui frontend. Key integration points:

- **List view**: `GET /discussions/questions` with query params (`sort`, `tag[]`, `unanswered`, `mine`, `status`, `q`)
- **Detail view**: `GET /discussions/questions/{id}` returns the question, all answers, and the current user's vote state in a single payload
- **Admin overview**: `GET /discussions/stats` powers a modal with total/replied/closed counts per discussion type, with click-through filtering
- **Status drill-down**: The `status` query param (`replied`, `closed`, or absent for "raised") on `list_questions` matches the `/stats` predicates so card counts equal filtered result counts
- **Tag filtering**: Uses Postgres JSONB `has_any` (`?|`) operator for multi-select tag filters
- **Expert view**: `GET /discussions/experts` returns top answerers per tag, computed entirely from mirror data via a `CROSS JOIN LATERAL jsonb_array_elements_text` query

---

## Related Documentation

| Document | Description |
|---|---|
| [discussions_service.md](../chat/discussions_service.md) | The @AiNxt bot worker service that consumes RQ jobs and posts AI-generated replies. |
| [shared_core.md](../reference/shared_core.md) | Core infrastructure including `discussions_engine_client.py`, `job_queue.py`, `compliance_engine.py`, database models, and inbox store. |
| [ai_ui_frontend.md](../ui/ai_ui_frontend.md) | Frontend `Discussions.jsx` component that consumes this router's API. |
