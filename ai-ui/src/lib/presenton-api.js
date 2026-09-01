// SPDX-License-Identifier: Apache-2.0
// presenton-api.js
// Wrapper around Presenton API endpoints using presentonFetch from project config

import { presentonFetch, PRESENTON_POLL_INTERVAL, PRESENTON_MAX_RETRIES, PRESENTON_TIMEOUT } from '../config';

const RETRY_DELAY_BASE = 4000; // Increased from 2s to 3s for long operations
const MAX_RETRY_DELAY = 60000; // Max 60s between retries

/**
 * Custom error class for Presenton API errors
 */
export class PresentonApiError extends Error {
  constructor(message, status, responseData, isTimeout = false) {
    super(message);
    this.name = 'PresentonApiError';
    this.status = status;
    this.responseData = responseData;
    this.isTimeout = isTimeout;
  }
}

/**
 * Returns a secure random float in [0, 1) using Web Crypto API.
 * Uses crypto.getRandomValues() — a CSPRNG — for jitter calculation.
 */
function _secureFloat() {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0] / (0xFFFFFFFF + 1);
}

/**
 * Calculate retry delay with exponential backoff and jitter
 */
function getRetryDelay(attempt) {
  const exponentialDelay = RETRY_DELAY_BASE * Math.pow(2, attempt);
  const jitter = _secureFloat() * 1000; // Add 0-1s jitter via Web Crypto API
  return Math.min(exponentialDelay + jitter, MAX_RETRY_DELAY);
}

/**
 * Check if error is retryable
 */
function isRetryableError(error) {
  // Don't retry client errors (4xx)
  if (error instanceof PresentonApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  // Retry timeouts, network errors, and server errors (5xx)
  return true;
}

async function retryableFetch(path, options = {}, retries = PRESENTON_MAX_RETRIES) {
  let attempt = 0;
  let lastErr = null;
  
  // Use longer timeout for generation endpoints
  const isGenerationEndpoint = path.includes('/create') || path.includes('/prepare') || path.includes('/stream/');
  const timeout = isGenerationEndpoint ? PRESENTON_TIMEOUT : (options.timeout || 60000);
  
  while (attempt < retries) {
    try {
      const resp = await presentonFetch(path, { ...options, timeout });
      
      // Handle HTTP errors
      if (!resp.ok) {
        const errorText = await resp.text();
        let errorData;
        try {
          errorData = JSON.parse(errorText);
        } catch {
          errorData = { message: errorText };
        }
        
        // Don't retry 4xx errors (client errors)
        if (resp.status >= 400 && resp.status < 500) {
          throw new PresentonApiError(
            errorData.message || `HTTP ${resp.status}: ${resp.statusText}`,
            resp.status,
            errorData
          );
        }
        
        // Retry 5xx errors
        throw new Error(`Server error ${resp.status}: ${errorData.message || resp.statusText}`);
      }
      
      return resp;
    } catch (e) {
      lastErr = e;
      attempt++;
      
      // Check if we should retry
      if (!isRetryableError(e)) {
        throw e;
      }
      
      if (attempt < retries) {
        const delay = getRetryDelay(attempt - 1);
        console.warn(`[Presenton API] Retry ${attempt}/${retries} after ${Math.round(delay)}ms for ${path}`, e.message);
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }
  
  // All retries exhausted
  throw new PresentonApiError(
    `Failed after ${retries} attempts: ${lastErr.message}`,
    lastErr.status || 0,
    lastErr.responseData || {},
    lastErr.name === 'AbortError' || lastErr.message?.includes('timeout')
  );
}

export async function createPresentation(payload, userId) {
  const headers = { 'Content-Type': 'application/json' };
  if (userId) headers['X-User-Id'] = userId;
  
  const resp = await retryableFetch('/api/v1/ppt/presentation/create', {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
  });
  return resp.json();
}

export async function streamOutlines(presentationId, onChunk, onComplete, onError, signal) {
  // wrapper around presenton stream endpoint using fetch + reader
  const url = `/presentation?id=${encodeURIComponent(presentationId)}&stream=true&_rsc=outlines`;
  try {
    const resp = await presentonFetch(url, { method: 'GET', signal });
    if (!resp.ok) throw new Error(`Stream HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split(/\r?\n/);
      buf = parts.pop();
      for (const p of parts) {
        if (p) onChunk(p);
      }
    }
    if (buf) onChunk(buf);
    onComplete && onComplete();
  } catch (e) {
    if (onError) onError(e); else throw e;
  }
}

export async function fetchTemplateLayout(group, userId) {
  const endpoint = `/api/template?group=${encodeURIComponent(group)}${userId ? `&user_id=${encodeURIComponent(userId)}` : ''}`;
  const headers = {};
  if (userId) headers['X-User-Id'] = userId;
  
  const resp = await retryableFetch(endpoint, { 
    method: 'GET',
    headers 
  });
  return resp.json();
}

export async function prepare(payload, userId) {
  const headers = { 'Content-Type': 'application/json' };
  if (userId) headers['X-User-Id'] = userId;
  
  try {
    const resp = await retryableFetch('/api/v1/ppt/presentation/prepare', {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
    });
    return resp.json();
  } catch (e) {
    // Log detailed error for debugging
    console.error('[Presenton API] Prepare failed:', {
      error: e.message,
      status: e.status,
      responseData: e.responseData,
      payload: {
        presentation_id: payload.presentation_id,
        outline_count: payload.outlines?.length,
        layout_name: payload.layout?.name,
      }
    });
    throw e;
  }
}

/**
 * Start the SSE stream to generate slide content with automatic reconnection.
 * Retries happen in the background without UI notification.
 * 
 * @param {string} presentationId - The presentation ID to stream
 * @param {object} options - { onStatusChange, maxRetries }
 * @param {AbortSignal} signal - Optional abort signal
 * @param {string} userId - Optional user ID for authentication
 * @returns {Promise<boolean>} - true if stream completed, false if failed
 */
export async function streamPresentation(presentationId, options = {}, signal, userId) {
  const { onStatusChange, maxRetries = 5 } = options; // Increased from 3 to 5
  const url = `/api/v1/ppt/presentation/stream/${encodeURIComponent(presentationId)}${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`;
  
  let attempt = 0;
  let lastError = null;
  let isCompleted = false;
  let hasConnected = false;
  
  // Update UI status only for major state changes (not retries)
  const updateUIStatus = (status, message) => {
    onStatusChange?.(status, message);
  };
  
  // Log to console only (backend logging)
  const logRetry = (message) => {
    // console.log(`[Presenton Stream Retry] ${message}`);
  };
  
  // Show initial connecting status once
  updateUIStatus('connecting', 'Connecting to presentation engine...');
  
  while (attempt < maxRetries && !signal?.aborted) {
    attempt++;
    
    // Only log retries to console, don't show in UI
    if (attempt > 1) {
      logRetry(`Attempt ${attempt}/${maxRetries}`);
    }
    
    try {
      const resp = await presentonFetch(url, { 
        method: 'GET',
        signal,
        redirect: 'follow'
      });
      
      if (!resp.ok) {
        throw new Error(`Stream HTTP ${resp.status}: ${resp.statusText}`);
      }
      
      // Only update UI on first successful connection
      if (!hasConnected) {
        hasConnected = true;
        updateUIStatus('connected', 'Generating presentation...');
      }
      
      // Read the SSE stream to completion
      const reader = resp.body.getReader();
      const dec = new TextDecoder('utf-8');
      let lastActivity = Date.now();
      // Increased from 2 min to 5 min - PPT generation can take a long time
      const ACTIVITY_TIMEOUT = 300000; // 5 minutes without data = reconnect
      
      while (true) {
        // Check for inactivity timeout
        if (Date.now() - lastActivity > ACTIVITY_TIMEOUT) {
          throw new Error('Stream inactive for too long');
        }
        
        // Use a shorter read timeout to allow checking for abort more frequently
        const readPromise = reader.read();
        const timeoutPromise = new Promise((_, reject) => {
          setTimeout(() => reject(new Error('Read timeout')), 30000); // 30s read timeout
        });
        
        let value, done;
        try {
          const result = await Promise.race([readPromise, timeoutPromise]);
          value = result.value;
          done = result.done;
        } catch (readErr) {
          // Read timeout - check if we should continue or reconnect
          if (signal?.aborted) {
            throw new Error('Aborted');
          }
          // Continue loop to check activity timeout and retry read
          continue;
        }
        
        if (done) {
          isCompleted = true;
          updateUIStatus('completed', 'Generation complete!');
          return true;
        }
        
        // Activity detected
        lastActivity = Date.now();
        
        // Decode but don't process - server is working
        dec.decode(value, { stream: true });
      }
      
    } catch (e) {
      lastError = e;
      
      // Don't retry if aborted
      if (signal?.aborted || e.name === 'AbortError') {
        updateUIStatus('aborted', 'Generation cancelled');
        return false;
      }
      
      // Check if generation might already be complete (server finished quickly)
      try {
        const metadata = await fetchMetadata(presentationId);
        if (metadata?.slides?.every(s => s.content && Object.keys(s.content).length > 0)) {
          updateUIStatus('completed', 'Generation complete!');
          return true;
        }
      } catch (metaErr) {
        // Ignore metadata fetch errors
      }
      
      if (attempt < maxRetries) {
        const delay = getRetryDelay(attempt - 1);
        logRetry(`Waiting ${Math.round(delay/1000)}s before retry...`);
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }
  
  // All retries exhausted - only show final failure to user
  updateUIStatus('failed', 'Generation failed. Please try again.');
  console.warn('[Presenton] Stream failed after retries:', lastError);
  return false;
}

export async function fetchMetadata(id, userId) {
  const endpoint = `/api/v1/ppt/presentation/${encodeURIComponent(id)}${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`;
  const headers = {};
  if (userId) headers['X-User-Id'] = userId;
  
  const resp = await retryableFetch(endpoint, { 
    method: 'GET',
    headers 
  });
  return resp.json();
}

export async function updatePresentation(payload, userId) {
  // Backend requires PATCH method (not POST)
  const headers = { 'Content-Type': 'application/json' };
  if (userId) headers['X-User-Id'] = userId;
  
  const resp = await retryableFetch('/api/v1/ppt/presentation/update', {
    method: 'PATCH',
    headers,
    body: JSON.stringify(payload),
  });
  return resp.json();
}

export async function exportPresentation(payload, format = 'pptx', userId, role = 'user') {
  // Backend reads user_id from query param and header, NOT from body
  const path = format === 'pdf' 
    ? `/api/export-as-pdf${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`
    : `/api/export-as-pptx${userId ? `?user_id=${encodeURIComponent(userId)}` : ''}`;
  
  const headers = { 'Content-Type': 'application/json' };
  if (userId) headers['X-User-Id'] = userId;
  
  // Send all required fields in body
  const bodyPayload = {
    id: payload.id,
    title: payload.title,
    userId: userId || payload.userId || null,
    role: role || payload.role || 'user',
  };
  
  const resp = await retryableFetch(path, {
    method: 'POST',
    headers,
    body: JSON.stringify(bodyPayload),
  });
  const blob = await resp.blob();
  return blob;
}

/**
 * Stream RSC (React Server Component) data from Presenton.
 * These are Next.js internal streams that provide real-time updates.
 * 
 * @param {string} presentationId - The presentation ID
 * @param {string} rscParam - The RSC parameter (e.g., '3kr05', '1chqt')
 * @param {function} onChunk - Callback for each chunk received
 * @param {function} onComplete - Callback when stream completes
 * @param {function} onError - Callback on error
 * @param {AbortSignal} signal - Abort signal for cancellation
 */
export async function streamPresentationRSC(presentationId, rscParam, onChunk, onComplete, onError, signal) {
  const url = `/presentation?id=${encodeURIComponent(presentationId)}&stream=true&_rsc=${encodeURIComponent(rscParam)}`;
  
  try {
    const resp = await presentonFetch(url, { method: 'GET', signal });
    if (!resp.ok) {
      throw new Error(`RSC Stream HTTP ${resp.status}: ${resp.statusText}`);
    }
    
    const reader = resp.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';
    
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      
      buf += dec.decode(value, { stream: true });
      const parts = buf.split(/\r?\n/);
      buf = parts.pop(); // Keep incomplete line in buffer
      
      for (const p of parts) {
        if (p) onChunk(p);
      }
    }
    
    if (buf) onChunk(buf); // Process remaining buffer
    onComplete && onComplete();
  } catch (e) {
    if (onError) onError(e); else throw e;
  }
}

/**
 * Poll all Presenton endpoints for complete status.
 * Combines metadata polling with optional RSC streams.
 * 
 * @param {string} presentationId - The presentation ID
 * @param {object} callbacks - { onMetadata, onRSCChunk, onError, onComplete }
 * @param {AbortSignal} signal - Abort signal
 */
export async function pollPresentationStatus(presentationId, callbacks = {}, signal) {
  const { onMetadata, onRSCChunk, onError, onComplete } = callbacks;
  
  // Start RSC streams in parallel (optional - for real-time updates)
  const rscPromises = [];
  if (onRSCChunk) {
    // Common RSC parameters used by Next.js
    const rscParams = ['3kr05', '1chqt'];
    for (const param of rscParams) {
      rscPromises.push(
        streamPresentationRSC(
          presentationId,
          param,
          (chunk) => onRSCChunk(param, chunk),
          () => {}, // onComplete - we don't wait for these
          (err) => console.warn(`RSC stream ${param} error:`, err),
          signal
        )
      );
    }
  }
  
  // Poll metadata until complete or error
  const pollInterval = PRESENTON_POLL_INTERVAL || 3000;
  let isComplete = false;
  
  while (!isComplete && !signal?.aborted) {
    try {
      const metadata = await fetchMetadata(presentationId);
      
      if (onMetadata) {
        onMetadata(metadata);
      }
      
      // Check if all slides have content
      if (metadata?.slides?.length > 0 && 
          metadata.slides.every(s => s.content && Object.keys(s.content).length > 0)) {
        isComplete = true;
        if (onComplete) onComplete(metadata);
        return metadata;
      }
    } catch (e) {
      if (onError) onError(e);
      // Continue polling on error
    }
    
    // Wait before next poll
    await new Promise(r => setTimeout(r, pollInterval));
  }
  
  // Wait for RSC streams (they're mostly for real-time UI updates)
  await Promise.allSettled(rscPromises);
  
  return null;
}
