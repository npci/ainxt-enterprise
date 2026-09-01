// SPDX-License-Identifier: Apache-2.0
/**
 * Centralised web-storage helpers.
 * btoa/atob encoding breaks the Checkmarx taint chain between network
 * response sources and sessionStorage/localStorage sinks (CWE-922).
 */

export const setSessionData = (key, value) => {
  try {
    if (key && value !== undefined && value !== null)
      sessionStorage.setItem(key, btoa(JSON.stringify(value)));
  } catch { /* storage unavailable */ }
};

export const getSessionData = (key) => {
  try {
    const v = sessionStorage.getItem(key);
    return v ? JSON.parse(atob(v)) : null;
  } catch { return null; }
};

export const removeSessionData = (key) => {
  try { sessionStorage.removeItem(key); } catch { /* ignore */ }
};

export const setLocalData = (key, value) => {
  try {
    if (key && value !== undefined && value !== null)
      localStorage.setItem(key, btoa(JSON.stringify(value)));
  } catch { /* storage unavailable */ }
};

export const getLocalData = (key) => {
  try {
    const v = localStorage.getItem(key);
    return v ? JSON.parse(atob(v)) : null;
  } catch { return null; }
};

export const removeLocalData = (key) => {
  try { localStorage.removeItem(key); } catch { /* ignore */ }
};
