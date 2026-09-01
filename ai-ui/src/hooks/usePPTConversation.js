// SPDX-License-Identifier: Apache-2.0
// usePPTConversation.js
// Conversational state machine for PPT generation
// Outlines shown as chat text, editing via natural language prompts

import { useState, useCallback, useRef } from 'react';
import {
  parseSlideCount,
  parseTheme,
  parseTone,
  parseLanguage,
  parseYesNo,
  parseFormat,
  isCancelRequest,
  extractAllParams,
  getDefaultParam,
  formatParamForDisplay,
} from '../utils/pptParamParser';

// Question definitions - each step asks one question
const QUESTIONS = {
  ask_slides: {
    text: (topic) => `I'll help you create a presentation about "${topic}"! 📊\n\nHow many slides would you like? (1-8)`,
    quickReplies: ['1', '2', '3', '4', '5', '6', '7', '8'],
    parse: parseSlideCount,
    paramKey: 'n_slides',
    reprompt: "Please choose a number between 1 and 8.",
  },
  ask_theme: {
    text: () => 'Which theme would you prefer?',
    quickReplies: ['General', 'Swift'],
    parse: parseTheme,
    paramKey: 'theme',
    reprompt: 'Please choose a theme: General or Swift',
  },
  ask_tone: {
    text: () => 'What tone should the presentation have?',
    quickReplies: ['Professional', 'Educational', 'Casual', 'Sales Pitch', 'Funny'],
    parse: parseTone,
    paramKey: 'tone',
    reprompt: 'Please choose a tone: Professional, Educational, Casual, Sales Pitch, or Funny',
  },
  ask_language: {
    text: () => 'What language should the presentation be in?',
    quickReplies: ['English', 'Hindi', 'Tamil', 'Telugu', 'Kannada', 'Malayalam'],
    parse: parseLanguage,
    paramKey: 'language',
    reprompt: 'Which language? (English, Hindi, Tamil, Telugu, Kannada, Malayalam, Bengali, Gujarati)',
  },
  ask_toc: {
    text: () => 'Should I include a table of contents slide?',
    quickReplies: ['Yes', 'No'],
    parse: parseYesNo,
    paramKey: 'include_table_of_contents',
    reprompt: 'Include a table of contents? Yes or No?',
  },
  ask_format: {
    text: () => 'What export format would you like?',
    quickReplies: ['PPTX', 'PDF'],
    parse: parseFormat,
    paramKey: 'export_as',
    reprompt: 'PPTX or PDF?',
  },
};

const STEP_ORDER = [
  'ask_slides',
  'ask_theme',
  'ask_tone',
  'ask_language',
  'ask_toc',
  'ask_format',
];

// Format outline as chat text
function formatOutlineAsText(outline) {
  if (!outline || !outline.slides) return 'No outline available.';
  
  const lines = [
    `📋 **${outline.title || 'Presentation'}**`,
    '',
    ...outline.slides.map((slide, idx) => {
      const bullets = slide.bullets?.map(b => `   • ${b}`).join('\n') || '';
      return `${idx + 1}. ${slide.title}${bullets ? '\n' + bullets : ''}`;
    }),
    '',
    'Type **"confirm"** to generate the presentation, or tell me what you want to change (e.g., "add a slide about security" or "change slide 3 to...").',
  ];
  
  return lines.join('\n');
}

export function usePPTConversation({
  insertMessage,
  updateMessage,
  chats,
  activeChatId,
  setChats,
  generateOutline,
  confirmAndGenerate,
  downloadPresentation,
}) {
  const [state, setState] = useState({
    active: false,
    step: 'idle',
    topic: '',
    params: {
      n_slides: null,
      theme: null,
      tone: null,
      language: null,
      include_table_of_contents: null,
      export_as: null,
    },
    outline: null,
    outlineHistory: [], // Track outline versions for editing context
    presentationId: null,
    progress: 0,
    error: null,
  });

  const currentQuestionIdRef = useRef(null);

  // Helper to get next step that needs a value
  const getNextStep = useCallback((params, startFrom = 0) => {
    for (let i = startFrom; i < STEP_ORDER.length; i++) {
      const step = STEP_ORDER[i];
      const paramKey = QUESTIONS[step].paramKey;
      if (params[paramKey] === null || params[paramKey] === undefined) {
        return step;
      }
    }
    return 'confirm';
  }, []);

  // Helper to insert a bot message with quick replies
  const askQuestion = useCallback((step, isReprompt = false, customPrompt = null) => {
    const q = QUESTIONS[step];
    const messageId = `ppt-q-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
    currentQuestionIdRef.current = messageId;

    // Use custom prompt if provided (for custom slide count flow)
    const content = customPrompt || (isReprompt ? q.reprompt : q.text(state.topic));

    insertMessage({
      id: messageId,
      role: 'assistant',
      content,
      quickReplies: q.quickReplies,
      quickRepliesUsed: false,
      pptConversation: true,
      pptStep: step,
      timestamp: Date.now(),
    });

    setState(s => ({ ...s, step }));
  }, [insertMessage, state.topic]);

  // Start the conversation
  const start = useCallback((topic, preExtracted = {}) => {
    const newParams = { ...state.params };
    
    // Apply pre-extracted params
    Object.entries(preExtracted).forEach(([key, value]) => {
      if (value !== null && value !== undefined) {
        newParams[key] = value;
      }
    });

    // ALWAYS ask for slide count - don't pre-extract it
    newParams.n_slides = null;

    setState({
      active: true,
      step: 'idle',
      topic,
      params: newParams,
      outline: null,
      outlineHistory: [],
      presentationId: null,
      progress: 0,
      error: null,
    });

    // Find first unanswered question
    const nextStep = getNextStep(newParams, 0);
    
    if (nextStep === 'confirm') {
      // All params already collected!
      setTimeout(() => generateOutlineWithParams(newParams), 100);
    } else {
      setTimeout(() => askQuestion(nextStep), 100);
    }
  }, [getNextStep, askQuestion]);

  // Generate outline using existing usePPTChat function
  const generateOutlineWithParams = useCallback(async (params, editRequest = null) => {
    setState(s => ({ ...s, step: 'generating_outline' }));

    // Show loader message
    const loaderMsgId = `ppt-outline-loader-${Date.now()}`;
    insertMessage({
      id: loaderMsgId,
      role: 'assistant',
      content: editRequest ? 'Updating the outline...' : 'Generating outline',
      pptType: 'ppt_progress',
      pptProgress: 0,
      timestamp: Date.now(),
    });

    try {
      let nSlides = params.n_slides || getDefaultParam('n_slides');
      let includeTOC = params.include_table_of_contents !== null ? params.include_table_of_contents : getDefaultParam('include_table_of_contents');
      
      // Validation: If TOC is enabled, we need at least 3 slides
      if (includeTOC && nSlides < 3) {
        nSlides = 3; // Auto-adjust to minimum required
        insertMessage({
          id: `ppt-toc-adjust-${Date.now()}`,
          role: 'assistant',
          content: `ℹ️ I've adjusted the slide count to 3 since table of contents requires at least 3 slides (TOC slide + content slides).`,
          timestamp: Date.now(),
        });
      }

      const finalParams = {
        n_slides: nSlides,
        theme: params.theme || getDefaultParam('theme'),
        tone: params.tone || getDefaultParam('tone'),
        language: params.language || getDefaultParam('language'),
        include_table_of_contents: includeTOC,
        export_as: params.export_as || getDefaultParam('export_as'),
      };

      let outline;
      
      if (editRequest && state.outline) {
        // Editing existing outline - pass context to backend
        const context = {
          previous_outline: state.outline,
          edit_request: editRequest,
          history: state.outlineHistory,
        };
        
        outline = await generateOutline(state.topic, finalParams.n_slides, finalParams, context);
        // Ensure clean title after edit
        if (outline && !outline.title || outline.title.includes('Previous outline:')) {
          outline.title = state.topic;
        }
      } else {
        // Fresh outline
        outline = await generateOutline(state.topic, finalParams.n_slides, finalParams);
        // Ensure clean title
        if (outline && !outline.title) {
          outline.title = state.topic;
        }
      }

      // Add to history
      const newHistory = [...state.outlineHistory, outline];

      // Remove loader and insert outline
      updateMessage(loaderMsgId, {
        content: formatOutlineAsText(outline),
        pptConversation: true,
        pptStep: 'outline_review',
        pptType: null,
        pptProgress: null,
        quickReplies: ['Confirm', 'Edit'],
        quickRepliesUsed: false,
      });

      setState(s => ({ 
        ...s, 
        step: 'outline_review',
        outline,
        outlineHistory: newHistory,
        params: finalParams,
      }));
    } catch (e) {
      // Update loader to show error
      updateMessage(loaderMsgId, {
        content: `Sorry, I couldn't generate the outline: ${e.message}`,
        pptType: 'ppt_error',
        pptError: e.message,
      });
      setState(s => ({ ...s, step: 'error', error: e.message }));
    }
  }, [generateOutline, insertMessage, updateMessage, state.topic, state.outline, state.outlineHistory]);

  // Handle user reply
  const processUserReply = useCallback((text) => {
    if (isCancelRequest(text)) {
      insertMessage({
        id: `ppt-cancel-${Date.now()}`,
        role: 'assistant',
        content: 'No problem! Let me know if you want to create a presentation later. 👍',
        timestamp: Date.now(),
      });
      reset();
      return;
    }

    // Check if we're in outline review mode
    if (state.step === 'outline_review') {
      const lower = text.toLowerCase().trim();
      
      // User wants to confirm and generate
      if (lower === 'confirm' || lower === 'yes' || lower === 'looks good' || lower === 'generate') {
        confirmAndGeneratePresentation();
        return;
      }
      
      // User wants to edit - treat their message as an edit request
      if (lower.startsWith('edit') || lower.includes('change') || lower.includes('add') || 
          lower.includes('remove') || lower.includes('modify') || lower.includes('update')) {
        insertMessage({
          id: `ppt-edit-${Date.now()}`,
          role: 'assistant',
          content: `Got it! Let me update the outline based on: "${text}"`,
          timestamp: Date.now(),
        });
        generateOutlineWithParams(state.params, text);
        return;
      }
      
      // Default: treat as edit request
      insertMessage({
        id: `ppt-edit-${Date.now()}`,
        role: 'assistant',
        content: `Updating the outline...`,
        timestamp: Date.now(),
      });
      generateOutlineWithParams(state.params, text);
      return;
    }

    const currentStep = state.step;
    const q = QUESTIONS[currentStep];

    if (!q) {
      // Not in a question state
      return;
    }

    // Parse the user's response
    const value = q.parse(text);

    if (value === null || value === undefined) {
      // Couldn't parse - re-prompt
      askQuestion(currentStep, true);
      return;
    }

    // Update params
    const newParams = { ...state.params, [q.paramKey]: value };
    setState(s => ({ ...s, params: newParams }));

    // Mark quick replies as used
    if (currentQuestionIdRef.current) {
      updateMessage(currentQuestionIdRef.current, { quickRepliesUsed: true });
    }

    // Find next question
    const currentIndex = STEP_ORDER.indexOf(currentStep);
    const nextStep = getNextStep(newParams, currentIndex + 1);

    if (nextStep === 'confirm') {
      generateOutlineWithParams(newParams);
    } else {
      askQuestion(nextStep);
    }
  }, [state.step, state.params, state.outline, askQuestion, getNextStep, insertMessage, updateMessage, generateOutlineWithParams]);

  // Confirm outline and generate presentation
  const confirmAndGeneratePresentation = useCallback(async () => {
    setState(s => ({ ...s, step: 'generating' }));

    // Insert progress message
    const progressMsgId = `ppt-progress-${Date.now()}`;
    insertMessage({
      id: progressMsgId,
      role: 'assistant',
      content: 'Generating your presentation...',
      pptType: 'ppt_progress',
      pptProgress: 0,
      timestamp: Date.now(),
    });

    try {
      // Pass onRetry callback to show retry messages
      const onRetry = (step, attempt) => {
        updateMessage(progressMsgId, {
          pptType: 'ppt_progress',
          pptProgress: 5,
          content: `Retrying ${step} (attempt ${attempt}/${3})...`,
        });
      };

      const result = await confirmAndGenerate(state.outline, state.params, onRetry);

      // Update progress message to complete
      updateMessage(progressMsgId, {
        pptType: 'ppt_complete',
        pptResult: result,
        content: 'Your presentation is ready! You can continue chatting normally.',
      });

      // Explicitly set active: false so isActive() returns false
      setState(s => ({
        ...s,
        active: false,
        step: 'complete',
        presentationId: result.id,
        progress: 100,
      }));
    } catch (e) {
      insertMessage({
        id: `ppt-error-${Date.now()}`,
        role: 'assistant',
        content: `Generation failed after retries: ${e.message}. Please try again.`,
        pptType: 'ppt_error',
        pptError: e.message,
        timestamp: Date.now(),
      });
      setState(s => ({ ...s, step: 'error', error: e.message }));
    }
  }, [confirmAndGenerate, insertMessage, updateMessage, state.outline, state.params]);

  // Check if conversation is active
  const isActive = useCallback(() => {
    return state.active && !['complete', 'error'].includes(state.step);
  }, [state.active, state.step]);

  // Reset state
  const reset = useCallback(() => {
    setState({
      active: false,
      step: 'idle',
      topic: '',
      params: {
        n_slides: null,
        theme: null,
        tone: null,
        language: null,
        include_table_of_contents: null,
        export_as: null,
      },
      outline: null,
      outlineHistory: [],
      presentationId: null,
      progress: 0,
      error: null,
    });
    currentQuestionIdRef.current = null;
  }, []);

  return {
    state,
    start,
    processUserReply,
    isActive,
    reset,
  };
}
