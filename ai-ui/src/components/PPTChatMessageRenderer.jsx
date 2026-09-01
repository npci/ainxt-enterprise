// SPDX-License-Identifier: Apache-2.0
// PPTChatMessageRenderer.jsx
// Renders rich PPT cards (progress, complete, error only)
// Outlines are now shown as regular chat text messages

import { useState, useCallback } from 'react';
import {
  PPTProgressMessage,
  PPTCompleteMessage,
  PPTErrorMessage,
} from './PPTChatMessage.jsx';

export default function PPTChatMessageRenderer({
  msg,
  activeChatId,
  setChats,
  generateOutline,
  confirmAndGenerate,
  downloadPresentation,
  pptState,
  pptConversation,
}) {
  // Local state for this specific message
  const [localState, setLocalState] = useState({
    loading: false,
    downloading: false,
  });

  // Helper to update this specific message in chats
  const updateMessage = useCallback((messageId, updates) => {
    setChats(currentChats => {
      const chat = currentChats.find(c => c.id === activeChatId);
      if (!chat) return currentChats;
      
      return currentChats.map(c => {
        if (c.id !== activeChatId) return c;
        return {
          ...c,
          messages: c.messages.map(m =>
            m.id === messageId ? { ...m, ...updates } : m
          ),
          updatedAt: Date.now(),
        };
      });
    });
  }, [activeChatId, setChats]);

  // Determine which view to show based on pptType
  const pptType = msg.pptType;

  // Progress card
  if (pptType === 'ppt_progress') {
    return (
      <PPTProgressMessage
        progress={pptState.progress || msg.pptProgress || 0}
      />
    );
  }

  // Complete card with download
  if (pptType === 'ppt_complete') {
    const result = msg.pptResult || {};
    // Clean title - remove any JSON or "Previous outline" text
    let cleanTitle = result.title || 'Presentation';
    if (cleanTitle.includes('Previous outline:')) {
      // Strip the JSON suffix appended to the title, keep the human-readable part
      cleanTitle = cleanTitle.split('Previous outline:')[0].trim() || 'Presentation';
    }
    // Only replace if the entire title is a raw JSON object
    if (cleanTitle.startsWith('{')) {
      cleanTitle = 'Presentation';
    }
    return (
      <PPTCompleteMessage
        title={cleanTitle}
        format={result.format || 'pptx'}
        onDownload={async () => {
          if (!result.id) return;
          setLocalState(s => ({ ...s, downloading: true }));
          try {
            await downloadPresentation(
              result.id,
              cleanTitle,
              result.format || 'pptx'
            );
          } finally {
            setLocalState(s => ({ ...s, downloading: false }));
          }
        }}
        downloading={localState.downloading}
      />
    );
  }

  // Error card
  if (pptType === 'ppt_error') {
    return (
      <PPTErrorMessage
        error={msg.pptError || 'Unknown error'}
        onRetry={() => {
          // Reset message to allow retry
          updateMessage(msg.id, {
            pptType: undefined,
            pptError: undefined,
          });
        }}
      />
    );
  }

  // Note: Outlines are now rendered as regular chat text messages
  // The ppt_outline type is no longer used - outlines appear as assistant messages
  // with markdown formatting instead of rich cards

  return null;
}
