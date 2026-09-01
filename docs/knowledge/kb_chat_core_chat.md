# `kb_chat_core_chat` Module Overview

## Purpose

The `kb_chat_core_chat` module is the central frontend chat controller for the Knowledge-Base (KB) chat experience in `ai-ui/src/components/KbChat.jsx`. It owns the primary conversation lifecycle actions—sending messages, continuing generation, regenerating responses, stopping streaming, and voice-based input—along with user-facing quality and collaboration features such as message feedback and chat sharing. This module binds the KB chat UI to the backend chat APIs and provides the stateful handlers that drive the conversation flow.

## Architecture

```mermaid
flowchart TB
    subgraph KbChatCoreChat["kb_chat_core_chat"]
        KbChat["KbChat.jsx<br/>main chat container"]
        CoreChatLogic["core_chat_logic"]
        FeedbackSharing["feedback_and_sharing"]
    end

    KbChat --> CoreChatLogic
    KbChat --> FeedbackSharing

    subgraph CoreChatLogic["core_chat_logic"]
        handleContinue["handleContinue"]
        handleRegenerate["handleRegenerate"]
        stopGeneration["stopGeneration"]
        sendMessageForVoice["sendMessageForVoice"]
    end

    subgraph FeedbackSharing["feedback_and_sharing"]
        submitFeedback["submitFeedback"]
        handleShareChat["handleShareChat"]
    end

    KbChat -->|uses| authFetch["config.js<br/>authFetch"]
    KbChat -->|uses| useToast["DialogProvider.jsx<br/>toast notifications"]

    CoreChatLogic -->|POST /chat/*| ChatRouter["chat_router.py"]
    FeedbackSharing -->|POST /chat/messages/{id}/feedback| FeedbackRouter["feedback_router.py"]
    FeedbackSharing -->|POST /chats/{id}/share| ChatRouter

    ChatRouter --> Postgres[(Postgres)]
    FeedbackRouter --> Postgres
    FeedbackRouter --> Redis[(Redis)]
```

## Core Components

| Component | File | Responsibility |
|-----------|------|----------------|
| `KbChat` | `ai-ui/src/components/KbChat.jsx` | Main KB chat container; manages active chat state and orchestrates child handlers. |
| `handleContinue` | `ai-ui/src/components/KbChat.jsx` | Resumes or continues an ongoing assistant response. |
| `handleRegenerate` | `ai-ui/src/components/KbChat.jsx` | Re-runs the last assistant turn to produce a new response. |
| `stopGeneration` | `ai-ui/src/components/KbChat.jsx` | Halts an in-progress streaming response. |
| `sendMessageForVoice` | `ai-ui/src/components/KbChat.jsx` | Submits a transcribed voice message into the chat. |
| `submitFeedback` | `ai-ui/src/components/KbChat.jsx` | Submits thumbs-down (or thumbs-up) ratings and issue details for assistant messages. |
| `handleShareChat` | `ai-ui/src/components/KbChat.jsx` | Creates a public, read-only share link for the current chat session. |

## References

- [core_chat_logic](../chat/core_chat_logic.md) — message lifecycle handlers (continue, regenerate, stop, voice send).
- [feedback_and_sharing](../storage/feedback_and_sharing.md) — message feedback submission and public chat sharing.
- [config](../infrastructure/config.md) — `authFetch` and API base configuration.
- [chat_router](../api/chat_router.md) — backend chat and share endpoints.
- [feedback_router](../api/feedback_router.md) — backend message feedback endpoint.