// SPDX-License-Identifier: Apache-2.0
// hooks/usePPTChat.js
// Manages the PPT generation state machine for inline chat flow
// Uses EXISTING presenton-api.js functions — no new API calls

import { useState, useRef, useCallback } from 'react';
import { authFetch, API_BASE as API, PRESENTON_BASE } from '../config';
import { buildCreatePayload, buildPreparePayload, buildExportPayload } from '../lib/presenton-payload';
import { LAYOUT_GROUPS } from '../lib/presenton-layout-registry';
import { validateFreeText } from '../utils/securityValidation';

const DEFAULT_PARAMS = {
  n_slides: 8,
  theme: 'general',
  tone: 'professional',
  language: 'English',
  verbosity: 'standard',
  include_table_of_contents: false,
  export_as: 'pptx',
};

const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 2000;

const FALLBACK_THEMES = [
  { id: 'general', name: 'General', color: '#1A2744', preview: 'dark', description: 'General purpose layouts for common presentation elements' },
  { id: 'swift', name: 'Swift', color: '#7C3AED', preview: 'dark', description: 'Swift and minimal layouts' },
];

const TONES = ['professional', 'educational', 'casual', 'sales_pitch', 'funny'];
const VERBOSITIES = ['concise', 'standard', 'text-heavy'];
const LANGUAGES = ['English', 'Hindi', 'Tamil', 'Telugu', 'Kannada', 'Malayalam', 'Bengali', 'Gujarati'];
const SLIDE_COUNTS = [1, 2, 3, 4, 5, 6, 7, 8];

export { FALLBACK_THEMES, TONES, VERBOSITIES, LANGUAGES, SLIDE_COUNTS };

export function usePPTChat(user) {
  const [pptState, setPptState] = useState({
    status: 'idle',
    params: { ...DEFAULT_PARAMS },
    outline: null,
    presentationId: null,
    progress: 0,
    error: null,
  });
  const pollRef = useRef(null);

  // Get user ID for API calls
  const getUserId = useCallback(() => {
    return user?.email || user?.userId || '';
  }, [user]);

  // Helper to make authenticated Presenton API calls
  const presentonApiCall = useCallback(async (path, options = {}) => {
    const userId = getUserId();
    const url = path.startsWith('http') ? path : `${PRESENTON_BASE}${path}`;
    const headers = {
      'Content-Type': 'application/json',
      'X-User-Id': userId,
      ...(options.headers || {}),
    };
    
    const resp = await fetch(url, {
      ...options,
      headers,
      credentials: 'include',
      cache: 'no-store',
    });
    
    if (!resp.ok) {
      const errorText = await resp.text();
      let errorData;
      try {
        errorData = JSON.parse(errorText);
      } catch {
        errorData = { message: errorText };
      }
      throw new Error(errorData.message || `HTTP ${resp.status}: ${resp.statusText}`);
    }
    
    return resp;
  }, [getUserId]);

  // Helper: Retry with exponential backoff
  const retryWithBackoff = useCallback(async (fn, maxRetries = MAX_RETRIES, delayMs = RETRY_DELAY_MS) => {
    let lastError;
    for (let attempt = 1; attempt <= maxRetries; attempt++) {
      try {
        return await fn();
      } catch (error) {
        lastError = error;
        console.warn(`[Presenton] Attempt ${attempt}/${maxRetries} failed:`, error.message);

        if (attempt < maxRetries) {
          const delay = delayMs * Math.pow(2, attempt - 1); // Exponential backoff: 2s, 4s, 8s
          console.log(`[Presenton] Retrying in ${delay}ms...`);
          await new Promise(resolve => setTimeout(resolve, delay));
        }
      }
    }
    throw lastError;
  }, []);

  // Update specific parameters
  const updateParams = useCallback((updates) => {
    setPptState((s) => ({ ...s, params: { ...s.params, ...updates } }));
  }, []);

  // Reset state and clear intervals
  const reset = useCallback((clearOnly = false) => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    // Also clear any progress intervals
    if (window._pptProgressInterval) {
      clearInterval(window._pptProgressInterval);
      window._pptProgressInterval = null;
    }
    if (!clearOnly) {
      setPptState({
        status: 'idle',
        params: { ...DEFAULT_PARAMS },
        outline: null,
        presentationId: null,
        progress: 0,
        error: null,
      });
    }
  }, []);

  // Generate outline using existing backend endpoint
  // EXACTLY matches PPTWizard.jsx: { prompt, n_slides }
  const generateOutline = useCallback(
    async (topic, nSlides = pptState.params.n_slides, params = {}, context = null) => {
      // Client-side pre-check mirroring validate_presenton_outline_request()
      // in core/security_validation.py — prompt is mandatory free text.
      // Backend remains the authoritative enforcer.
      const promptCheck = validateFreeText(topic || "");
      if (!topic || !topic.trim() || !promptCheck.isValid) {
        const err = new Error(promptCheck.errors[0]?.message || 'Prompt is required');
        setPptState((s) => ({ ...s, status: 'error', error: err.message }));
        throw err;
      }
      setPptState((s) => ({ ...s, status: 'generating_outline', error: null }));
      try {
        const uid = getUserId();
        
        // EXACTLY like PPTWizard.jsx - only prompt and n_slides
        const r = await authFetch(`${API}/ppt/outline`, {
          method: 'POST',
          headers: { 
            'Content-Type': 'application/json',
            'X-User-Id': uid,
          },
          body: JSON.stringify({ 
            prompt: topic, 
            n_slides: nSlides,
          }),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const outline = await r.json();
        setPptState((s) => ({ ...s, status: 'outline_ready', outline }));
        return outline;
      } catch (e) {
        setPptState((s) => ({ ...s, status: 'error', error: e.message }));
        throw e;
      }
    },
    [pptState.params.n_slides, getUserId]
  );

  // Confirm outline and start generation using Presenton APIs
  const confirmAndGenerate = useCallback(
    async (outline, params, onRetry) => {
      setPptState((s) => ({ ...s, status: 'generating', progress: 5, error: null }));

      try {
        const slides = outline?.slides || [];
        const userId = getUserId();

        // Step 1: Create presentation (with retry)
        const createPayload = buildCreatePayload(outline.title || params.topic, {
          n_slides: slides.length,
          language: params.language,
          tone: params.tone,
          verbosity: params.verbosity,
          include_table_of_contents: params.include_table_of_contents,
          include_title_slide: false,
          user_id: userId,
        });

        const createResp = await retryWithBackoff(() =>
          presentonApiCall('/api/v1/ppt/presentation/create', {
            method: 'POST',
            body: JSON.stringify(createPayload),
          })
        );
        const createData = await createResp.json();
        const presentationId = createData?.id;

        if (!presentationId) {
          throw new Error('Create response did not include presentation id');
        }

        // Step 2: Prepare presentation (with retry)
        const layoutFromRegistry = LAYOUT_GROUPS[params.theme];
        
        if (!layoutFromRegistry) {
          throw new Error(`No layout found for theme "${params.theme}" in local registry`);
        }
        
        const preparePayload = buildPreparePayload(
          { title: outline.title, slides },
          { ...params, selectedTheme: params.theme },
          presentationId,
          layoutFromRegistry
        );

        // Notify about retry if needed
        let prepareAttempt = 0;
        await retryWithBackoff(async () => {
          prepareAttempt++;
          if (prepareAttempt > 1 && onRetry) {
            onRetry(`prepare`, prepareAttempt);
          }
          return presentonApiCall('/api/v1/ppt/presentation/prepare', {
            method: 'POST',
            body: JSON.stringify(preparePayload),
          });
        });

        // Step 3: Start SSE stream (fire-and-forget)
        presentonApiCall(`/api/v1/ppt/presentation/stream/${encodeURIComponent(presentationId)}?user_id=${encodeURIComponent(userId)}`, {
          method: 'GET',
        }).catch((err) => {
        });

        setPptState((s) => ({ ...s, presentationId }));

        // Step 4: Poll for completion
        return new Promise((resolve, reject) => {
          const progressInterval = setInterval(() => {
            setPptState((s) => ({
              ...s,
              progress: s.progress < 88 ? s.progress + Math.random() * 4 : s.progress,
            }));
          }, 2000);

          pollRef.current = setInterval(async () => {
            try {
              const metaResp = await presentonApiCall(
                `/api/v1/ppt/presentation/${encodeURIComponent(presentationId)}?user_id=${encodeURIComponent(userId)}`,
                { method: 'GET' }
              );
              const m = await metaResp.json();

              if (m && m.slides && m.slides.length > 0) {
                const genCount = m.slides.filter((s) => s.content && Object.keys(s.content).length > 0).length;
                const progress = m.n_slides ? Math.min(90, (genCount / m.n_slides) * 100) : Math.min(90, genCount * 10);

                setPptState((s) => ({ ...s, progress: Math.max(s.progress, progress) }));

                if (m.slides.every((s) => s.content && Object.keys(s.content).length > 0)) {
                  clearInterval(pollRef.current);
                  clearInterval(progressInterval);
                  pollRef.current = null;

                  setPptState((s) => ({
                    ...s,
                    status: 'complete',
                    progress: 100,
                  }));

                  resolve({
                    id: presentationId,
                    title: m.title || outline.title,
                    n_slides: m.n_slides || slides.length,
                    format: params.export_as || 'pptx',
                    theme: params.theme || 'general',
                    tone: params.tone || 'professional',
                  });
                }
              }
            } catch (e) {
              console.warn('[Presenton] Polling error:', e);
            }
          }, 3000);
        });
      } catch (e) {
        setPptState((s) => ({ ...s, status: 'error', error: e.message }));
        throw e;
      }
    },
    [getUserId, presentonApiCall, retryWithBackoff]
  );

  // Download presentation
  const downloadPresentation = useCallback(async (presentationId, title, format) => {
    const userId = getUserId();
    // Export payload requires userId and role fields
    const exportPayload = {
      id: presentationId,
      title: title || 'Presentation',
      userId: userId,  // Required by export endpoint
      role: 'user',    // Required by export endpoint
    };
    // Export endpoint requires user_id as query param
    const path = format === 'pdf' 
      ? `/api/export-as-pdf?user_id=${encodeURIComponent(userId)}`
      : `/api/export-as-pptx?user_id=${encodeURIComponent(userId)}`;
    
    const resp = await presentonApiCall(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(exportPayload),
    });
    
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${title.replace(/[^a-z0-9]/gi, '_')}.${format}`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }, [presentonApiCall, getUserId]);

  return {
    pptState,
    setPptState,
    updateParams,
    reset,
    generateOutline,
    confirmAndGenerate,
    downloadPresentation,
  };
}
