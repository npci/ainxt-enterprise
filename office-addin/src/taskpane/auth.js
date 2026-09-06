// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// Office add-in Entra SSO (scope §6.3).
//
// Uses Office.auth.getAccessToken() to obtain a token for THIS app's exposed
// API (access_as_user), then exchanges it server-side (On-Behalf-Of) at
// POST /auth/sso/office for an AiNxt platform JWT. No password prompt, no
// end-user Graph consent UI (centralized admin consent, §7.2).
//
// Returns { token, user } on success. Throws on failure — callers fall back
// to the password LoginScreen (e.g. Office SSO errors 13xxx, or SSO disabled).

const SSO_FALLBACK_CODES = new Set([
  13001, // user not signed in to Office
  13002, // user aborted consent
  13003, // unsupported host/platform
  13006, 13008, 13010,
  50001, // SSO not supported / not configured
]);

export function isSSOFallback(err) {
  const code = err && (err.code ?? err.errorCode);
  return code != null && SSO_FALLBACK_CODES.has(Number(code));
}

export async function officeSSO(authBase) {
  if (!(window.Office && Office.auth && Office.auth.getAccessToken)) {
    throw Object.assign(new Error("Office.auth unavailable"), { code: 13003 });
  }

  // Bootstrap token for our app (forMSGraphAccess lets the OBO reach Graph).
  const assertion = await Office.auth.getAccessToken({
    allowSignInPrompt: true,
    allowConsentPrompt: true,
    forMSGraphAccess: true,
  });

  const res = await fetch(`${authBase}/sso/office`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ assertion }),
  });
  if (!res.ok) {
    throw new Error(`Office SSO exchange failed (${res.status})`);
  }
  const data = await res.json();
  const token = data.access_token || data.token || "";
  if (!token) throw new Error("Office SSO returned no token");
  return { token, user: data };
}
