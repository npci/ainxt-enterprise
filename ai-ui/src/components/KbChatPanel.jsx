// SPDX-License-Identifier: MIT
// KbChatPanel — wraps the KbScopeGraph scope picker and the "Chat with
// this scope" handoff into the main Chat experience.
//
// Behaviour:
//   1. User picks a node at any depth on the force-directed scope graph
//      (Domain / Product / Version / Document — any of them is a valid
//      scope target).
//   2. On confirm, we POST /chats to eagerly create a Chat row with
//      rag_mode='on' and the picked scope fields all persisted in a
//      single DB write. This guarantees the chat survives a refresh
//      *even if the user never sends a message* — historically the
//      row was created lazily by the Kafka consumer on the first
//      assistant turn, which lost both empty KB chats (no row ever)
//      and the scope/rag_mode fields (consumer didn't set them).
//   3. We only add the chat to App state (via onHandoff) after the
//      POST succeeds, so the UI never shows a chat that doesn't
//      exist server-side.
//   4. The 350 ms debounce in KbChat.jsx still owns subsequent scope
//      edits via PATCH /chats/{id}/scope — that path is unchanged.
//
// Props:
//   user                 — current logged-in user (unused for now,
//                          forwarded for parity with KnowledgeBase).
//   onHandoff(chatObj)   — caller wires this to App-level setChats +
//                          setActiveChatId + navigate("/chat"). The
//                          panel itself stays decoupled from routing.

import { useCallback } from "react";
import { API_BASE, authFetch } from "../config";
import KbScopeGraph from "./kb-graph/KbScopeGraph.jsx";
import { formatKbChatTitle } from "../utils/kbFormat.js";

export default function KbChatPanel({ user: _user, onHandoff }) {
  void _user;

  const handleScopeReady = useCallback(async (scope) => {
    if (!scope) return;
    // Drop scopes that have neither a domain nor a product — a completely
    // empty scope cannot be used for KB retrieval. Domain-only scope is
    // valid: the backend will search all products within that domain.
    if (!scope.domain && !scope.product_id) return;

    const chatId = crypto.randomUUID();
    const now = Date.now();

    // Build a human-friendly title from the resolved scope + IST timestamp.
    // Format: "{Product} · {Domain} · {Version} — DD-MMM-YYYY HH:MM IST".
    // Falls back to em-dash for any missing segment. See utils/kbFormat.js.
    const titleScope = {
      product_name: scope._productName || null,
      domain:       scope.domain       || null,
      spec_version: scope.spec_version || null,
    };
    const chatTitle = formatKbChatTitle(titleScope, new Date(now));

    // Eager DB-row creation: POST /chats writes id + title + rag_mode +
    // all four scope fields in a single atomic insert. We do NOT call
    // onHandoff until this succeeds, so the UI never displays a KB chat
    // that doesn't exist server-side (which is what caused refresh to
    // lose chats previously).
    const payload = {
      id:           chatId,
      title:        chatTitle,
      rag_mode:     "on",
      product_id:   scope.product_id    || null,
      domain:       scope.domain        || null,
      spec_version: scope.spec_version  || null,
      kb_doc_id:    scope.parent_doc_id || null,
    };

    try {
      const res = await authFetch(`${API_BASE}/chats`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify(payload),
      });
      if (!res.ok) {
        // Surface failure to the console; do NOT hand off — keeping the
        // user on the picker is better than showing a phantom chat that
        // disappears on refresh.
        console.error("KbChatPanel: POST /chats failed", res.status);
        return;
      }
    } catch (e) {
      console.error("KbChatPanel: POST /chats error", e);
      return;
    }

    // Local chat object — mirrors the shape App.createEmptyChat uses,
    // plus the four KB scope columns KbChat.jsx hydrates from for the
    // scope picker / KB grounding indicator. Matches what /chats returns
    // on the next page load so behaviour is identical pre/post refresh.
    const chatObj = {
      id:            chatId,
      title:         chatTitle,
      messages:      [],
      createdAt:     now,
      updatedAt:     now,
      rag_mode:      "on",
      product_id:    scope.product_id    || null,
      domain:        scope.domain        || null,
      spec_version:  scope.spec_version  || null,
      kb_doc_id:     scope.parent_doc_id || null,
      // Carry presentation labels so KbChat can render the breadcrumb
      // chips immediately, without a /products round-trip.
      _kb_scope_labels: {
        productName:  scope._productName  || null,
        documentName: scope._documentName || null,
      },
      // kbScopePending is intentionally NOT set here: the row already
      // exists server-side, so KbChat.jsx's post-/ask retry block is a
      // no-op for KB chats. We keep that block in place as a safety net
      // for any future code path that creates a KB chat without going
      // through POST /chats.
    };

    onHandoff?.(chatObj);
  }, [onHandoff]);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <KbScopeGraph onScopeReady={handleScopeReady} />
    </div>
  );
}
