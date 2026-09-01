// SPDX-License-Identifier: Apache-2.0
// presenton-stream.js
// Read streaming endpoints from Presenton using Fetch and ReadableStream

import { presentonFetch } from '../config';
import { presentonLogger } from './presenton-logger';

export async function readPresentonStream(presentationId, rsc, onChunk, onComplete, onError, signal) {
  // URL is relative to PRESENTON_BASE — presentonFetch prepends /presenton automatically
  const url = `/presentation?id=${encodeURIComponent(presentationId)}&stream=true&_rsc=${rsc}`;
  try {
    const resp = await presentonFetch(url, { method: 'GET', signal });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error(`Stream HTTP ${resp.status}: ${txt}`);
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let done = false;
    let buffer = '';
    while (!done) {
      const { value, done: streamDone } = await reader.read();
      if (streamDone) {
        done = true;
        break;
      }
      if (value) {
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split(/\r?\n/);
        buffer = parts.pop();
        for (const part of parts) {
          if (!part) continue;
          try {
            onChunk(part);
            presentonLogger.add(presentationId, { type: 'stream_chunk', payload: part, ts: new Date().toISOString(), rsc });
          } catch (e) {
            // swallow chunk handler errors
            console.warn('onChunk handler error', e);
          }
        }
      }
    }

    // flush remaining buffer
    if (buffer) {
      onChunk(buffer);
      presentonLogger.add(presentationId, { type: 'stream_chunk', payload: buffer, ts: new Date().toISOString(), rsc });
    }

    onComplete && onComplete();
  } catch (e) {
    presentonLogger.add(presentationId, { type: 'stream_error', message: e.message, ts: new Date().toISOString(), rsc });
    if (onError) onError(e);
    throw e;
  }
}
