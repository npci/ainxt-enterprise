// SPDX-License-Identifier: Apache-2.0
// presenton-logger.js
// Simple client-side logger persisted to localStorage for Presenton events

const MAX_ENTRIES = 200;

export const presentonLogger = {
  keyFor: (id) => `presenton_log_${id}`,
  add: (presentationId, evt) => {
    try {
      const key = presentonLogger.keyFor(presentationId || 'global');
      const raw = localStorage.getItem(key);
      const list = raw ? JSON.parse(raw) : [];
      list.push(evt);
      if (list.length > MAX_ENTRIES) list.splice(0, list.length - MAX_ENTRIES);
      localStorage.setItem(key, JSON.stringify(list));
    } catch (e) {
      console.warn('presentonLogger.add failed', e);
    }
  },
  get: (presentationId) => {
    try {
      const raw = localStorage.getItem(presentonLogger.keyFor(presentationId || 'global'));
      return raw ? JSON.parse(raw) : [];
    } catch (e) { return []; }
  },
  clear: (presentationId) => {
    try { localStorage.removeItem(presentonLogger.keyFor(presentationId || 'global')); } catch (e) {}
  }
};
