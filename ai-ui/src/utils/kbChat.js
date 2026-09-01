// SPDX-License-Identifier: Apache-2.0
// kbChat.js — single source of truth for "is this a KB chat?"
//
// A KB chat is one that originated from the Knowledge Base → Chat
// surface (KbChatPanel). It's distinguished from a normal chat by:
//   1. rag_mode = "on" (the gateway treats this as force-KB retrieval), AND
//   2. at least one scope field set: product_id, domain, spec_version,
//      or kb_doc_id.
//
// Both conditions matter:
//   - rag_mode alone could be set by the legacy Generic|Knowledge Base
//     toggle that lived inside Chat.jsx (now removed) on older chats.
//     Treating those as KB chats would surprise users who had them
//     pinned in their main chat sidebar.
//   - Scope fields alone could in theory be set on a chat whose
//     rag_mode is still "off" (manual DB edit, partial state); we
//     intentionally exclude those too — KB chats are unambiguously
//     scope-driven and rag-on.
//
// New chats from KbChatPanel set both rag_mode='on' AND the scope on
// the local chat object at create time (see KbChatPanel.jsx) and the
// hydration path in Chat.jsx reads both columns from the server, so
// this predicate works identically before and after a page reload.

export function isKbChat(chat) {
  if (!chat) return false;
  if ((chat.rag_mode || "off") !== "on") return false;
  return Boolean(
    chat.product_id   ||
    chat.domain       ||
    chat.spec_version ||
    chat.kb_doc_id,
  );
}

// Convenience splitter for callers that want both halves.
export function splitChats(chats) {
  const kb = [], normal = [];
  for (const c of chats || []) {
    if (isKbChat(c)) kb.push(c); else normal.push(c);
  }
  return { kb, normal };
}
