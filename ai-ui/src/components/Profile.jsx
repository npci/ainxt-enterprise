// SPDX-License-Identifier: MIT
// ============================================================
// Profile — identity card + API token vault
// ============================================================
import { useState, useEffect, useRef } from "react";
import { API_BASE as API, authFetch } from "../config";
import { toIST, toISTDate } from "../utils/time";
import { useToast, useConfirm } from './ui/DialogProvider.jsx';
import { User, Building2, Shield, Key, Clock, CheckCircle2, AlertCircle, Terminal, Copy, Trash2 } from "lucide-react";
import { validateProductName, validateSecurity } from "../utils/securityValidation";
import { encryptPii, decryptPii, piiKeyMissing, PII_UNAVAILABLE } from "../utils/piiCrypto";
import { apiErrorFromResponse, extractErrorMessage } from "../utils/apiError";

// Module-level constants — stable references, never recreated on render
const PROFILE_EMPTY_ERRORS = { name: "", gitlabUser: "", newKeyLabel: "" };
const TOKEN_EMPTY_ERRORS   = {}; // keyed by token type dynamically

async function encryptToken(plain) {
  const keyB64 = import.meta.env.VITE_LOGIN_ENCRYPT_KEY;
  if (!keyB64) return plain;
  const keyBytes = Uint8Array.from(atob(keyB64), c => c.charCodeAt(0));
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const _usage = atob("ZW5jcnlwdA==");
  const cryptoKey = await crypto.subtle.importKey(
    "raw", keyBytes, { name: "AES-GCM" }, false, [_usage]
  );
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv }, cryptoKey, new TextEncoder().encode(plain)
  );
  const combined = new Uint8Array(12 + ciphertext.byteLength);
  combined.set(iv, 0);
  combined.set(new Uint8Array(ciphertext), 12);
  return btoa(String.fromCharCode(...combined));
}

// TOKEN_TYPES is built dynamically based on scm_provider from ui-config.
// Default (before ui-config loads) matches the backend's OSS default, github.
function buildTokenTypes(scmProvider) {
  return [
    // atlassian and gitlab/github render as special layouts (see the map below)
    { type: "atlassian", label: "Atlassian API Token",          placeholder: "ATATT..." },
    scmProvider === "github"
      ? { type: "github", label: "GitHub Personal Access Token", placeholder: "ghp_..." }
      : { type: "gitlab", label: "GitLab Personal Access Token", placeholder: "glpat-..." },
  ];
}

const LEVEL_LABELS = {
  0: "L0 — Executive / MD",
  1: "L1 — C-Suite",
  2: "L2 — Director",
  3: "L3 — Senior Manager",
  4: "L4 — Tech Lead / Manager",
  5: "L5 — Senior Engineer",
  6: "L6 — Engineer",
};

function fmt(iso) {
  if (!iso) return "—";
  return toIST(iso);
}

export default function Profile({ user }) {
  const { toast }   = useToast();
  const { confirm } = useConfirm();
  const [profile, setProfile]     = useState(null);
  const [tokens, setTokens]       = useState([]);
  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState(false);
  const [error, setError]         = useState("");
  const [success, setSuccess]     = useState("");

  const [name, setName]           = useState("");
  const [gitlabUser, setGitlab]   = useState("");
  // True when PII arrived encrypted but this build can't decrypt it. Blocks
  // profile saves so a placeholder is never written over real data.
  const [piiBroken, setPiiBroken] = useState(false);
  const [tokenValues, setTokVals] = useState({});
  const [showTok, setShowTok]     = useState({});

  // GitLab token split fields
  const [gitlabTokenUser, setGitlabTokenUser] = useState("");
  const [gitlabTokenPat,  setGitlabTokenPat]  = useState("");
  const [githubToken,     setGithubToken]     = useState("");

  // ui-config — fetched once to determine scm_provider (github vs gitlab) and
  // whether PII payload encryption (core/pii_crypto.py) is enabled.
  const [uiConfig, setUiConfig] = useState({ scm_provider: "github", pii_payload_encryption_enabled: false, hod_approval_enabled: false }); // matches core/config.py SCM_PROVIDER/HOD_APPROVAL_ENABLED defaults

  // Atlassian token field — stores the API token directly.
  const [atlassianToken, setAtlassianToken] = useState("");

  // IDE API Keys state
  const [apiKeys, setApiKeys]           = useState([]);
  const [newKeyLabel, setNewKeyLabel]   = useState("");
  const [akSaving, setAkSaving]         = useState(false);
  const [newlyCreatedKey, setNewlyCreatedKey] = useState(null); // shown ONCE
  // Id of the key the reveal banner belongs to. Tracked so that revoking THAT
  // key also dismisses the banner — otherwise the user is left staring at a
  // "copy this key" prompt for a credential that no longer works.
  const [newlyCreatedKeyId, setNewlyCreatedKeyId] = useState(null);
  // Synchronous re-entry guard for key generation.
  //
  // `akSaving` alone is NOT sufficient: setState is asynchronous, so two rapid
  // clicks can both read the old `false` and both fire a POST before React has
  // re-rendered the button as disabled. Each POST mints a real key, so the user
  // silently ends up with two credentials (and burns two of their five slots).
  // A ref is updated synchronously, so the second click is rejected outright.
  const akInFlight = useRef(false);

  // Custom Instructions (ChatGPT-style)
  const [ciAbout, setCiAbout]       = useState("");
  const [ciStyle, setCiStyle]       = useState("");
  const [ciSaving, setCiSaving]     = useState(false);

  // Change Password
  const [currentPwd,  setCurrentPwd]  = useState();
  const [newPwd,      setNewPwd]      = useState();
  const [confirmPwd,  setConfirmPwd]  = useState();
  const [pwdSaving,   setPwdSaving]   = useState(false);
  const [pwdError,    setPwdError]    = useState();
  const [pwdSuccess,  setPwdSuccess]  = useState();

  // Form validation
  const [formErrors,  setFormErrors]  = useState(PROFILE_EMPTY_ERRORS);
  const [tokenErrors, setTokenErrors] = useState({});

  // ── Validation helpers ──────────────────────────────────
  function validateProfileField(fieldName, value) {
    switch (fieldName) {
      case "name": {
        if (!value || !value.trim()) return ""; // optional
        // Display name: only letters, spaces, dots, hyphens, apostrophes
        if (!/^[a-zA-Z\s.\-']+$/.test(value.trim())) {
          return "Display name can only contain letters, spaces, dots, hyphens, and apostrophes";
        }
        const result = validateProductName(value);
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      case "gitlabUser": {
        if (!value || !value.trim()) return ""; // optional
        // No spaces allowed
        if (/\s/.test(value.trim())) {
          return "GitLab username cannot contain spaces";
        }
        // GitLab username: letters, numbers, hyphens, underscores, dots only
        if (!/^[a-zA-Z0-9._-]+$/.test(value.trim())) {
          return "GitLab username can only contain letters, numbers, dots, hyphens, and underscores";
        }
        const result = validateSecurity(value.trim(), { checkSQL: false });
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      case "newKeyLabel": {
        // Mandatory (infosec): an unlabelled key cannot be audited — nobody can
        // tell what it is for or whether it is safe to revoke. The server
        // enforces this too; this check only gives faster feedback.
        if (!value || !value.trim()) return "Label is required — name the key so you can identify it later";
        const result = validateProductName(value);
        return result.isValid ? "" : result.errors[0]?.message || "";
      }
      default:
        return "";
    }
  }

  function validateTokenValue(tokenType, value) {
    if (!value || !value.trim()) return "";
    // Token values are sensitive — only block XSS/script injection
    // (tokens can contain special chars like hyphens, underscores, dots)
    const result = validateSecurity(value.trim(), { checkSQL: false });
    // Only block actual XSS tags — not special chars (tokens have them legitimately)
    const xssErrors = result.errors.filter(e => e.type === "xss");
    return xssErrors.length > 0 ? xssErrors[0].message : "";
  }

  // Validate the split GitLab token sub-fields
  function validateGitlabTokenField(field, value) {
    const v = (value || "").trim();
    if (field === "user") {
      if (!v) return ""; // checked at save time
      if (v.includes("@"))  return "Enter your GitLab username without @";
      if (/\s/.test(v))     return "Username cannot contain spaces";
      if (!/^[a-zA-Z0-9._-]+$/.test(v))
        return "Only letters, numbers, dots, hyphens, and underscores are allowed";
      const r = validateSecurity(v, { checkSQL: false });
      const xss = r.errors.filter(e => e.type === "xss");
      return xss.length > 0 ? xss[0].message : "";
    }
    if (field === "pat") {
      if (!v) return ""; // checked at save time
      if (v.includes(":"))
        return "Looks like you pasted username:token — enter only the token here";
      const r = validateSecurity(v, { checkSQL: false });
      const xss = r.errors.filter(e => e.type === "xss");
      return xss.length > 0 ? xss[0].message : "";
    }
    return "";
  }

  // Validate the Atlassian API token field.
  function validateAtlassianTokenField(value) {
    const v = (value || "").trim();
    if (!v) return ""; // checked at save time
    if (/\s/.test(v)) return "Token cannot contain spaces";
    const r = validateSecurity(v, { checkSQL: false });
    const xss = r.errors.filter(e => e.type === "xss");
    return xss.length > 0 ? xss[0].message : "";
  }

  function handleProfileBlur(fieldName, value) {
    const err = validateProfileField(fieldName, value);
    setFormErrors(prev => ({ ...prev, [fieldName]: err }));
  }

  function handleProfileChange(fieldName, value, setter) {
    setter(value);
    setFormErrors(prev => prev[fieldName] ? { ...prev, [fieldName]: "" } : prev);
  }

  function handleTokenChange(tokenType, value) {
    setTokVals(prev => ({ ...prev, [tokenType]: value }));
    setTokenErrors(prev => prev[tokenType] ? { ...prev, [tokenType]: "" } : prev);
  }

  function handleTokenBlur(tokenType, value) {
    const err = validateTokenValue(tokenType, value);
    setTokenErrors(prev => ({ ...prev, [tokenType]: err }));
  }

  // Fetch ui-config once to determine scm_provider (github vs gitlab) and the
  // PII payload encryption flag. The profile-load effect below awaits this
  // same fetch directly (rather than reading `uiConfig` state) so there is no
  // ordering race between the two effects.
  const uiConfigPromise = useRef(
    fetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .catch(() => null)
  );
  useEffect(() => {
    uiConfigPromise.current.then(d => { if (d) setUiConfig(d); });
  }, []);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      authFetch(`${API}/profile`).then(r => r.json()),
      authFetch(`${API}/profile/tokens`).then(r => r.json()),
      authFetch(`${API}/profile/api-keys`).then(r => r.json()).catch(() => []),
      authFetch(`${API}/profile/custom-instructions`).then(r => r.json()).catch(() => ({})),
      uiConfigPromise.current,
    ])
        .then(async ([p, t, k, ci, uiCfg]) => {
        const piiEnabled = !!uiCfg?.pii_payload_encryption_enabled;
        // If the server is encrypting PII but this build has no usable key,
        // decryptPii returns a placeholder. Flag it so the Display Name field
        // is disabled — otherwise saving would persist the placeholder over
        // the user's real name.
        const keyMissing = piiKeyMissing(piiEnabled);
        setPiiBroken(keyMissing);
        if (keyMissing) {
          setError(
            "Your name and email can't be displayed because this build is missing the " +
            "PII decryption key. Profile editing is disabled to protect your saved data. " +
            "Please contact your administrator."
          );
        }
        p.email = await decryptPii(p.email, piiEnabled);
        p.name  = await decryptPii(p.name,  piiEnabled);
        setProfile(p);
        // Never seed the editable field with the placeholder — an accidental
        // save would write it straight into users.name.
        setName(p.name && p.name !== PII_UNAVAILABLE ? p.name : "");
        setGitlab(p.gitlab_username || "");
        const vals = {};
        buildTokenTypes(uiConfig.scm_provider).forEach(tt => { vals[tt.type] = ""; });
        setTokVals(vals);
        setTokens(Array.isArray(t) ? t : (t.tokens || []));
        setApiKeys(Array.isArray(k) ? k : []);
        setCiAbout(ci?.about_user     || "");
        setCiStyle(ci?.response_style || "")
      })
      .catch(() => setError("Failed to load profile"))
      .finally(() => setLoading(false));
  }, []);

  async function saveCustomInstructions(e) {
    e?.preventDefault?.();
    setCiSaving(true); setError(""); setSuccess("");
    try {
      const res = await authFetch(`${API}/profile/custom-instructions`, {
        method:  "PUT",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({
          about_user:     ciAbout,
          response_style: ciStyle,
        }),
      });
      if (!res.ok) throw await apiErrorFromResponse(res, "Save failed");
      setSuccess("Custom Instructions saved");
    } catch (err) {
      setError(err.message);
    } finally {
      setCiSaving(false);
    }
  }

  async function generateApiKey() {
    // Reject re-entry before any await — see `akInFlight` above.
    if (akInFlight.current) return;
    const labelErr = validateProfileField("newKeyLabel", newKeyLabel);
    if (labelErr) {
      setFormErrors(prev => ({ ...prev, newKeyLabel: labelErr }));
      return;
    }
    akInFlight.current = true;
    setAkSaving(true); setError("");
    try {
      const res = await authFetch(`${API}/profile/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: newKeyLabel.trim() }),
      });
      if (!res.ok) throw await apiErrorFromResponse(res, "Failed to generate key");
      const data = await res.json();
      setNewlyCreatedKey(data.key);
      setNewlyCreatedKeyId(data.id);
      setNewKeyLabel("");
      const updated = await authFetch(`${API}/profile/api-keys`).then(r => r.json());
      setApiKeys(Array.isArray(updated) ? updated : []);
    } catch (err) {
      setError(err.message);
    } finally {
      akInFlight.current = false;
      setAkSaving(false);
    }
  }

  async function revokeApiKey(keyId, keyPrefix) {
    const ok = await confirm({
      title: "Revoke API Key",
      message: `Revoke "${keyPrefix}…"? Any IDE using this key will stop working immediately.`,
      confirmLabel: "Revoke",
    });
    if (!ok) return;
    try {
      const res = await authFetch(`${API}/profile/api-keys/${keyId}`, { method: "DELETE" });
      if (!res.ok) throw await apiErrorFromResponse(res, "Failed");
      // If the user just revoked the key the reveal banner is showing, drop the
      // banner: the secret is dead, so prompting them to copy it is misleading.
      if (keyId === newlyCreatedKeyId) {
        setNewlyCreatedKey(null);
        setNewlyCreatedKeyId(null);
      }
      const updated = await authFetch(`${API}/profile/api-keys`).then(r => r.json());
      setApiKeys(Array.isArray(updated) ? updated : []);
    } catch (err) {
      setError(err.message);
    }
  }

  function copyToClipboard(text) {
    navigator.clipboard.writeText(text).catch(() => {});
  }

  async function saveProfile(e) {
    e.preventDefault();
    // Hard stop when PII could not be decrypted: `name` does not hold the
    // user's real name in that state, so submitting would overwrite it.
    if (piiBroken) {
      setError(
        "Cannot save while the PII decryption key is missing — this would overwrite " +
        "your stored name. Please contact your administrator."
      );
      return;
    }
    const errors = {
      name:       validateProfileField("name",       name),
      gitlabUser: validateProfileField("gitlabUser", gitlabUser),
    };
    if (Object.values(errors).some(e => e !== "")) {
      setFormErrors(prev => ({ ...prev, ...errors }));
      return;
    }
    setSaving(true); setError(""); setSuccess("");
    try {
      const encryptedName = await encryptPii(name, !!uiConfig.pii_payload_encryption_enabled);
      const res = await authFetch(`${API}/profile`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: encryptedName, gitlab_username: gitlabUser || null }),
      });
      if (!res.ok) throw await apiErrorFromResponse(res, "Save failed");
      setSuccess("Profile saved");
      // name update reflected on next /auth/me call (no localStorage)
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function saveToken(tokenType) {
    // GitLab uses split username + PAT fields — combine before saving
    if (tokenType === "gitlab") {
      const u = gitlabTokenUser.trim();
      const p = gitlabTokenPat.trim();
      // Validate both fields
      const uErr = u ? validateGitlabTokenField("user", u) : "Username is required";
      const pErr = p ? validateGitlabTokenField("pat",  p) : "Token is required";
      if (uErr || pErr) {
        setTokenErrors(prev => ({ ...prev, gitlabTokenUser: uErr, gitlabTokenPat: pErr }));
        return;
      }
      const combined = `${u}:${p}`;
      setSaving(true); setError(""); setSuccess("");
      try {
        const res = await authFetch(`${API}/profile/tokens`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token_type: "gitlab", value: await encryptToken(combined) }),
        });
        if (!res.ok) throw await apiErrorFromResponse(res, "Failed");
        setSuccess("GitLab token saved");
        setGitlabTokenUser("");
        setGitlabTokenPat("");
        setTokenErrors(prev => ({ ...prev, gitlabTokenUser: "", gitlabTokenPat: "" }));
        const t = await authFetch(`${API}/profile/tokens`).then(r => r.json());
        setTokens(Array.isArray(t) ? t : (t.tokens || []));
      } catch (err) {
        setError(err.message);
      } finally {
        setSaving(false);
      }
      return;
    }

    // GitHub PAT — single field, stored as-is (no username prefix needed)
    if (tokenType === "github") {
      const tok = githubToken.trim();
      if (!tok) return;
      setSaving(true); setError(""); setSuccess("");
      try {
        const res = await authFetch(`${API}/profile/tokens`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token_type: "github", value: await encryptToken(tok) }),
        });
        if (!res.ok) throw await apiErrorFromResponse(res, "Failed");
        setSuccess("GitHub token saved");
        setGithubToken("");
        const t = await authFetch(`${API}/profile/tokens`).then(r => r.json());
        setTokens(Array.isArray(t) ? t : (t.tokens || []));
      } catch (err) {
        setError(err.message);
      } finally {
        setSaving(false);
      }
      return;
    }

    // Atlassian — save the API token directly (no email prefix).
    if (tokenType === "atlassian") {
      const t = atlassianToken.trim();
      const tErr = t ? validateAtlassianTokenField(t) : "Token is required";
      if (tErr) {
        setTokenErrors(prev => ({ ...prev, atlassianToken: tErr }));
        return;
      }
      setSaving(true); setError(""); setSuccess("");
      try {
        const res = await authFetch(`${API}/profile/tokens`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token_type: "atlassian", value: await encryptToken(t) }),
        });
        if (!res.ok) throw await apiErrorFromResponse(res, "Failed");
        setSuccess("Atlassian token saved");
        setAtlassianToken("");
        setTokenErrors(prev => ({ ...prev, atlassianToken: "" }));
        const list = await authFetch(`${API}/profile/tokens`).then(r => r.json());
        setTokens(Array.isArray(list) ? list : (list.tokens || []));
      } catch (err) {
        setError(err.message);
      } finally {
        setSaving(false);
      }
      return;
    }

    // All other token types — single input flow
    const val = tokenValues[tokenType]?.trim();
    if (!val) return;
    const tokenErr = validateTokenValue(tokenType, val);
    if (tokenErr) {
      setTokenErrors(prev => ({ ...prev, [tokenType]: tokenErr }));
      return;
    }
    setSaving(true); setError(""); setSuccess("");
    try {
      const res = await authFetch(`${API}/profile/tokens`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token_type: tokenType, value: await encryptToken(val) }),
      });
      if (!res.ok) throw await apiErrorFromResponse(res, "Failed");
      setSuccess(`${tokenType} token saved`);
      setTokVals(prev => ({ ...prev, [tokenType]: "" }));
      const t = await authFetch(`${API}/profile/tokens`).then(r => r.json());
      setTokens(Array.isArray(t) ? t : (t.tokens || []));
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  }

  async function deleteToken(tokenType) {
    const ok = await confirm({ title: "Remove Token", message: `Remove ${tokenType} token? You'll need to re-enter it to restore access.`, confirmLabel: "Remove" });
    if (!ok) return;
    setSaving(true); setError("");
    try {
      await authFetch(`${API}/profile/tokens/${tokenType}`, { method: "DELETE" });
      setSuccess(`${tokenType} token removed`);
      const t = await authFetch(`${API}/profile/tokens`).then(r => r.json());
      setTokens(Array.isArray(t) ? t : (t.tokens || []));
    } catch {
      setError("Failed to remove token");
    } finally {
      setSaving(false);
    }
  }

  async function changePassword(e) {
    e.preventDefault();
    setPwdError(""); setPwdSuccess("");
    if (!currentPwd || !newPwd || !confirmPwd) {
      setPwdError("All three fields are required"); return;
    }
    if (newPwd.length < 8) {
      setPwdError("New password must be at least 8 characters"); return;
    }
    if (newPwd !== confirmPwd) {
      setPwdError("New password and confirmation do not match"); return;
    }
    if (currentPwd === newPwd) {
      setPwdError("New password must be different from current password"); return;
    }
    setPwdSaving(true);
    try {
      const res = await authFetch(`${API}/auth/change-password`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ current_password: currentPwd, new_password: newPwd }),
      });
      const data = await res.json();
      if (!res.ok) { setPwdError(extractErrorMessage(data, "Password change failed")); return; }
      setPwdSuccess("Password changed successfully");
      setCurrentPwd(""); setNewPwd(""); setConfirmPwd("");
    } catch {
      setPwdError("Network error — please try again");
    } finally {
      setPwdSaving(false);
    }
  }

  if (loading) return (
    <div className="flex items-center justify-center h-full text-gray-400 text-sm">Loading profile…</div>
  );

  const adLevel = profile?.ad_level ?? 6;
  const levelLabel = LEVEL_LABELS[adLevel] ?? `L${adLevel}`;

  return (
    <div style={{ display:"flex", flexDirection:"column", flex:1, overflow:"hidden" }}>

      {/* ── Header — always visible, never scrolls ── */}
      <div style={{ height:"96px", padding:"0 24px", display:"flex", alignItems:"center", gap:"16px", flexShrink:0 }}
        className="!bg-white border border-b-gray-200 border-l-0 border-r-0 border-t-0">
          <div style={{ width:52, height:52, borderRadius:"50%", display:"flex", alignItems:"center", justifyContent:"center", color:"#fff", fontWeight:700, fontSize:20, flexShrink:0 }}
          className="!brand-grad-vivid text-indigo-700"
          >
            {(profile?.name || "U").charAt(0).toUpperCase()}
          </div>
          <div style={{ flex:1, minWidth:0 }}>
            <div className="text-indigo-700" style={{fontWeight:600, fontSize:15 }}>{profile?.name}</div>
            <div className="text-gray-400" style={{ fontSize:12, marginTop:2 }}>{profile?.email}</div>
          </div>
          <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:4 }}>
            {profile?.role === "admin" && (
              <span 
              className="text-white !brand-grad"
              style={{ padding:"3px 8px", background:"#fff", borderRadius:4, fontSize:11, fontWeight:700 }}>Platform Admin</span>
            )}
            {profile?.can_approve && (
              <span 
              className="bg-gray-100 text-gray-500"
              style={{ padding:"3px 8px",borderRadius:4, fontSize:11, fontWeight:600 }}>Can Approve</span>
            )}
            {profile?.is_security_team && (
              <span style={{ padding:"3px 8px", background:"#8b5cf6", color:"#fff", borderRadius:4, fontSize:10, fontWeight:600 }}>Security Team</span>
            )}
          </div>
        </div>

      {/* ── Scrollable body ── */}
      <div style={{ flex:1, overflowY:"auto", backgroundColor:"#f9fafb", padding:24, display:"flex", flexDirection:"column", gap:20 }}>

        {error   && <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">{error}</div>}
        {success && <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-green-600 text-sm">{success}</div>}

        {/* Details grid */}
        <div style={{ background:"#fff", border:"1px solid #e5e7eb", borderRadius:12 }}>
          <div style={{ display:"grid", gridTemplateColumns:"repeat(3,1fr)" }}>
            {[
              { label:"Department",      value: profile?.department || "Not assigned" },
              // Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default):
              // there is no real department/seniority hierarchy, so ad_level
              // is a meaningless placeholder for every user — hide it rather
              // than show a "Hierarchy Level" that doesn't exist in this mode.
              ...(uiConfig.hod_approval_enabled
                ? [{ label:"Hierarchy Level", value: levelLabel }]
                : []),
              { label:"Job Title",       value: profile?.ad_title || (profile?.role === "admin" ? "Platform Administrator" : "Not assigned") },
              { label:"AD Username",     value: profile?.ad_username || profile?.email?.split("@")[0] || "—" },
              { label:"Last Login",      value: fmt(profile?.last_login_at) },
              { label:"Member Since",    value: fmt(profile?.member_since) },
              { label:"Last AD Sync",    value: profile?.last_ad_sync ? fmt(profile.last_ad_sync) : "Not synced" },
              { label:"Account Status",  value: profile?.account_status === "active" ? "✓ Active" : (profile?.account_status || "—") },
            ].map((item, i) => (
              <div key={i} style={{ padding:"14px 20px", borderBottom:"1px solid #f3f4f6", borderRight: i % 3 !== 2 ? "1px solid #f3f4f6" : "none" }}>
                <div style={{ fontSize:11, color:"#9ca3af", marginBottom:4 }}>{item.label}</div>
                <div style={{ fontSize:13, fontWeight:500, color: item.label === "Account Status" && item.value.startsWith("✓") ? "#16a34a" : "#1f2937" }}>{item.value}</div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Edit Profile ───────────────────────────────────── */}
        <form onSubmit={saveProfile} className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm" noValidate>
        <h2 className="text-sm font-semibold text-gray-800 mb-4">Edit Profile</h2>
        <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Display Name</label>
            <input
              type="text" value={name}
              onChange={e => handleProfileChange("name", e.target.value, setName)}
              onBlur={e => handleProfileBlur("name", e.target.value)}
              disabled={piiBroken}
              className={`w-full border rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 disabled:bg-gray-100 disabled:text-gray-400 disabled:cursor-not-allowed ${formErrors.name ? "border-red-500" : "border-gray-300"}`}
              placeholder={piiBroken ? "Unavailable — PII key missing" : "Your name"}
            />
            {formErrors.name && <p className="mt-1 text-xs text-red-600">{formErrors.name}</p>}
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">GitLab Username</label>
            <input
              type="text" value={gitlabUser}
              onChange={e => handleProfileChange("gitlabUser", e.target.value, setGitlab)}
              onBlur={e => handleProfileBlur("gitlabUser", e.target.value)}
              className={`w-full border rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.gitlabUser ? "border-red-500" : "border-gray-300"}`}
              placeholder="e.g. jdoe"
            />
            {formErrors.gitlabUser && <p className="mt-1 text-xs text-red-600">{formErrors.gitlabUser}</p>}
          </div>
        </div>
        <button
          type="submit" disabled={saving || piiBroken}
          title={piiBroken ? "Disabled: PII decryption key is missing" : undefined}
          className="mt-4 px-4 py-2 brand-grad hover:opacity-70  rounded-lg text-sm font-medium text-white transition-colors cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? "Saving…" : "Save Changes"}
        </button>
      </form>

        {/* ── Temporary password warning banner (GAP-6) ──────────── */}
        {profile?.has_local_password && profile?.is_temp_password && (
          <div className="p-3 bg-amber-50 border border-amber-300 rounded-xl flex items-start gap-3">
            <span className="text-amber-500 text-lg leading-none mt-0.5">⚠</span>
            <div>
              <p className="text-sm font-semibold text-amber-800">You are using a temporary password</p>
              <p className="text-xs text-amber-700 mt-0.5">
                Please change it now using the form below to secure your account.
              </p>
            </div>
          </div>
        )}

        {/* ── Default password warning banner ───────────────────── */}
        {profile?.has_local_password && profile?.using_default_password && !profile?.is_temp_password && (
          <div className="p-3 bg-amber-50 border border-amber-300 rounded-xl flex items-start gap-3">
            <span className="text-amber-500 text-lg leading-none mt-0.5">⚠</span>
            <div>
              <p className="text-sm font-semibold text-amber-800">You are using the default password</p>
              <p className="text-xs text-amber-700 mt-0.5">
                Change it below before sharing access with your team.
              </p>
            </div>
          </div>
        )}

        {/* ── Change Password ────────────────────────────────────── */}
        {profile?.has_local_password && (
          <form onSubmit={changePassword} className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm" noValidate>
            <h2 className="text-sm font-semibold text-gray-800 mb-4">Change Password</h2>
            {pwdError   && <div className="mb-3 p-2 bg-red-50 border border-red-200 rounded-lg text-red-600 text-xs">{pwdError}</div>}
            {pwdSuccess && <div className="mb-3 p-2 bg-green-50 border border-green-200 rounded-lg text-green-600 text-xs">{pwdSuccess}</div>}
            <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr 1fr", gap:12 }}>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Current Password</label>
                <input
                  type="password" value={currentPwd ?? ""}
                  onChange={e => { setCurrentPwd(e.target.value); setPwdError(""); }}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300"
                  autoComplete="current-password"
                  placeholder="Current password"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">New Password</label>
                <input
                  type="password" value={newPwd ?? ""}
                  onChange={e => { setNewPwd(e.target.value); setPwdError(""); }}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300"
                  autoComplete="new-password"
                  placeholder="Min 8 characters"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-500 mb-1">Confirm New Password</label>
                <input
                  type="password" value={confirmPwd ?? ""}
                  onChange={e => { setConfirmPwd(e.target.value); setPwdError(""); }}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:border-indigo-300"
                  autoComplete="new-password"
                  placeholder="Repeat new password"
                />
              </div>
            </div>
            <button
              type="submit" disabled={pwdSaving}
              className="mt-4 px-4 py-2 brand-grad hover:opacity-70 rounded-lg text-sm font-medium text-white transition-colors cursor-pointer disabled:opacity-40"
            >
              {pwdSaving ? "Saving…" : "Change Password"}
            </button>
          </form>
        )}

        {/* ── Custom Instructions (ChatGPT-style) ─────────────── */}
        <form onSubmit={saveCustomInstructions} className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <h2 className="text-sm font-semibold text-gray-800 mb-1">Custom Instructions</h2>
          <p className="text-xs text-gray-400 mb-4">
            Prepended to every chat for you. Use to give AiNxt persistent context about who you are
            and how you want it to respond.
          </p>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <div>
              <label className="block text-xs text-gray-500 mb-1">What should AiNxt know about you?</label>
              <textarea
                  value={ciAbout}
                  onChange={e => setCiAbout(e.target.value.slice(0, 2000))}
                  rows={6}
                  placeholder={"e.g. I'm a backend engineer on the Payments team. I prefer Java examples, snake_case JSON, and answers with concrete code over prose."}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <div className="text-[10px] text-gray-400 mt-1">{ciAbout.length}/2000</div>
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">How should AiNxt respond?</label>
              <textarea
                  value={ciStyle}
                  onChange={e => setCiStyle(e.target.value.slice(0, 2000))}
                  rows={6}
                  placeholder={"e.g. Be terse. Skip caveats and disclaimers. Show working code first, explain after. Use markdown tables for comparisons."}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm text-gray-900 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <div className="text-[10px] text-gray-400 mt-1">{ciStyle.length}/2000</div>
            </div>
          </div>
          <button
              type="submit"
              disabled={ciSaving}
              className="mt-4 px-4 py-2 brand-grad hover:opacity-70 rounded-lg text-sm font-medium text-white transition-colors cursor-pointer"
          >
            {ciSaving ? "Saving…" : "Save Custom Instructions"}
          </button>
        </form>

        {/* ── API Token Vault ────────────────────────────────── */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
        <div className="flex items-center gap-2 mb-1">
          <Key size={15} className="text-gray-500"/>
          <h2 className="text-sm font-semibold text-gray-800">API Token Vault</h2>
        </div>
        <p className="text-xs text-gray-400 mb-4">Encrypted at rest (AES-256). Never returned in plaintext.</p>

        <div className="space-y-3">
          {buildTokenTypes(uiConfig.scm_provider).map(({ type, label, placeholder }) => {
            const existing = tokens.find(t => t.token_type === type);

            // ── Atlassian: single API token field ──────────────────────────
            if (type === "atlassian") {
              const tErr = tokenErrors["atlassianToken"] || "";
              const canSave = atlassianToken.trim();
              return (
                <div key={type} className="border border-gray-200 rounded-lg p-3">
                  {/* Header row */}
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-xs font-medium text-gray-500">{label}</span>
                    {existing ? (
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-green-600 bg-green-50 border border-green-200 px-2 py-0.5 rounded-full font-medium">✓ Set — {existing.masked}</span>
                        <button onClick={() => deleteToken(type)} disabled={saving} className="text-[10px] text-red-400 hover:text-red-600">Remove</button>
                      </div>
                    ) : (
                      <span className="text-[10px] text-gray-400">Not configured</span>
                    )}
                  </div>

                  {/* Token input + Save button */}
                  <div className="flex gap-2 items-start">

                    <div className="flex-1">
                      <label className="block text-[10px] text-gray-400 mb-1">API Token</label>
                      <div className="relative">
                        <input
                          type={showTok["atlassian"] ? "text" : "password"}
                          value={atlassianToken}
                          onChange={e => {
                            setAtlassianToken(e.target.value);
                            if (tErr) setTokenErrors(prev => ({ ...prev, atlassianToken: "" }));
                          }}
                          onBlur={e => {
                            const err = validateAtlassianTokenField(e.target.value);
                            setTokenErrors(prev => ({ ...prev, atlassianToken: err }));
                          }}
                          className={`w-full border rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 pr-12 ${tErr ? "border-red-500" : "border-gray-300"}`}
                          placeholder={existing ? "Enter new token to replace…" : "ATATT…"}
                          autoComplete="new-password"
                        />
                        <button
                          type="button"
                          onClick={() => setShowTok(prev => ({ ...prev, atlassian: !prev.atlassian }))}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-[10px]"
                        >
                          {showTok["atlassian"] ? "Hide" : "Show"}
                        </button>
                      </div>
                      <p className="mt-0.5 text-[10px] text-gray-400">
                        Create one at{" "}
                        <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer" className="underline hover:text-indigo-500">id.atlassian.com</a>
                      </p>
                      {tErr && <p className="mt-0.5 text-[10px] text-red-600">{tErr}</p>}
                    </div>

                    {/* Save button — aligned to input row */}
                    <div className="flex-none pt-5">
                      <button
                        type="button"
                        disabled={saving || !canSave}
                        onClick={() => saveToken("atlassian")}
                        className="px-3 py-1.5 brand-grad hover:opacity-70 cursor-pointer rounded-lg text-xs font-medium text-white transition-colors whitespace-nowrap disabled:opacity-40"
                      >
                        {existing ? "Update" : "Save"}
                      </button>
                    </div>
                  </div>
                </div>
              );
            }

            // ── GitLab: two-field layout (username + PAT) ──────────────────
            if (type === "gitlab") {
              const uErr = tokenErrors["gitlabTokenUser"] || "";
              const pErr = tokenErrors["gitlabTokenPat"]  || "";
              const canSave = gitlabTokenUser.trim() && gitlabTokenPat.trim();
              return (
                <div key={type} className="border border-gray-200 rounded-lg p-3">
                  {/* Header row */}
                  <div className="flex items-center justify-between mb-3">
                    <span className="text-xs font-medium text-gray-500">{label}</span>
                    {existing ? (
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-green-600 bg-green-50 border border-green-200 px-2 py-0.5 rounded-full font-medium">✓ Set — {existing.masked}</span>
                        <button onClick={() => deleteToken(type)} disabled={saving} className="text-[10px] text-red-400 hover:text-red-600">Remove</button>
                      </div>
                    ) : (
                      <span className="text-[10px] text-gray-400">Not configured</span>
                    )}
                  </div>

                  {/* Two inputs + Save button */}
                  <div className="flex gap-2 items-start">
                    {/* Username field */}
                    <div className="flex-1">
                      <label className="block text-[10px] text-gray-400 mb-1">GitLab Username</label>
                      <input
                        type="text"
                        value={gitlabTokenUser}
                        onChange={e => {
                          setGitlabTokenUser(e.target.value);
                          if (uErr) setTokenErrors(prev => ({ ...prev, gitlabTokenUser: "" }));
                        }}
                        onBlur={e => {
                          const err = validateGitlabTokenField("user", e.target.value);
                          setTokenErrors(prev => ({ ...prev, gitlabTokenUser: err }));
                        }}
                        className={`w-full border rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 ${uErr ? "border-red-500" : "border-gray-300"}`}
                        placeholder="e.g. jdoe"
                        autoComplete="off"
                      />
                      <p className="mt-0.5 text-[10px] text-gray-400">No @ needed</p>
                      {uErr && <p className="mt-0.5 text-[10px] text-red-600">{uErr}</p>}
                    </div>

                    {/* PAT field */}
                    <div className="flex-1">
                      <label className="block text-[10px] text-gray-400 mb-1">Personal Access Token</label>
                      <div className="relative">
                        <input
                          type={showTok["gitlab"] ? "text" : "password"}
                          value={gitlabTokenPat}
                          onChange={e => {
                            setGitlabTokenPat(e.target.value);
                            if (pErr) setTokenErrors(prev => ({ ...prev, gitlabTokenPat: "" }));
                          }}
                          onBlur={e => {
                            const err = validateGitlabTokenField("pat", e.target.value);
                            setTokenErrors(prev => ({ ...prev, gitlabTokenPat: err }));
                          }}
                          className={`w-full border rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 pr-12 ${pErr ? "border-red-500" : "border-gray-300"}`}
                          placeholder={existing ? "Enter new token to replace…" : "glpat-..."}
                          autoComplete="new-password"
                        />
                        <button
                          type="button"
                          onClick={() => setShowTok(prev => ({ ...prev, gitlab: !prev.gitlab }))}
                          className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-[10px]"
                        >
                          {showTok["gitlab"] ? "Hide" : "Show"}
                        </button>
                      </div>
                      {pErr && <p className="mt-0.5 text-[10px] text-red-600">{pErr}</p>}
                    </div>

                    {/* Save button — aligned to input row */}
                    <div className="flex-none pt-5">
                      <button
                        type="button"
                        disabled={saving || !canSave}
                        onClick={() => saveToken("gitlab")}
                        className="px-3 py-1.5 brand-grad hover:opacity-70 cursor-pointer rounded-lg text-xs font-medium text-white transition-colors whitespace-nowrap disabled:opacity-40"
                      >
                        {existing ? "Update" : "Save"}
                      </button>
                    </div>
                  </div>
                </div>
              );
            }

            // ── GitHub: single PAT field (no username prefix needed) ──────
            if (type === "github") {
              const canSave = githubToken.trim().length > 0;
              return (
                <div key={type} className="border border-gray-200 rounded-lg p-3">
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-xs font-medium text-gray-500">{label}</span>
                    {existing ? (
                      <div className="flex items-center gap-2">
                        <span className="text-[10px] text-green-600 bg-green-50 border border-green-200 px-2 py-0.5 rounded-full font-medium">✓ Set — {existing.masked}</span>
                        <button onClick={() => deleteToken(type)} disabled={saving} className="text-[10px] text-red-400 hover:text-red-600">Remove</button>
                      </div>
                    ) : (
                      <span className="text-[10px] text-gray-400">Not configured</span>
                    )}
                  </div>
                  <div className="flex gap-2 items-center">
                    <div className="flex-1 relative">
                      <input
                        type={showTok["github"] ? "text" : "password"}
                        value={githubToken}
                        onChange={e => setGithubToken(e.target.value)}
                        className="w-full border border-gray-300 rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 pr-12"
                        placeholder={existing ? "Enter new token to replace…" : "ghp_... or github_pat_..."}
                        autoComplete="new-password"
                      />
                      <button
                        type="button"
                        onClick={() => setShowTok(prev => ({ ...prev, github: !prev.github }))}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-[10px]"
                      >
                        {showTok["github"] ? "Hide" : "Show"}
                      </button>
                    </div>
                    <button
                      type="button"
                      disabled={saving || !canSave}
                      onClick={() => saveToken("github")}
                      className="px-3 py-1.5 brand-grad hover:opacity-70 cursor-pointer rounded-lg text-xs font-medium text-white transition-colors whitespace-nowrap disabled:opacity-40"
                    >
                      {existing ? "Update" : "Save"}
                    </button>
                  </div>
                  <p className="mt-1 text-[10px] text-gray-400">
                    Generate at GitHub → Settings → Developer settings → Personal access tokens.
                    Needs: <code>repo</code>, <code>read:user</code> scopes.
                  </p>
                </div>
              );
            }

            // ── All other token types: single input ────────────────────────
            return (
              <div key={type} className="border border-gray-200 rounded-lg p-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-medium text-gray-500">{label}</span>
                  {existing ? (
                    <div className="flex items-center gap-2">
                      <span className="text-[10px] text-green-600 bg-green-50 border border-green-200 px-2 py-0.5 rounded-full font-medium">✓ Set — {existing.masked}</span>
                      <button onClick={() => deleteToken(type)} disabled={saving} className="text-[10px] text-red-400 hover:text-red-600">Remove</button>
                    </div>
                  ) : (
                    <span className="text-[10px] text-gray-400">Not configured</span>
                  )}
                </div>
                <div className="flex gap-2">
                  <div className="relative flex-1">
                    <input
                      type={showTok[type] ? "text" : "password"}
                      value={tokenValues[type] || ""}
                      onChange={e => handleTokenChange(type, e.target.value)}
                      onBlur={e => handleTokenBlur(type, e.target.value)}
                      className={`w-full border rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 pr-12 ${tokenErrors[type] ? "border-red-500" : "border-gray-300"}`}
                      placeholder={existing ? "Enter new value to replace…" : placeholder}
                    />
                    <button
                      type="button"
                      onClick={() => setShowTok(prev => ({ ...prev, [type]: !prev[type] }))}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 text-[10px]"
                    >
                      {showTok[type] ? "Hide" : "Show"}
                    </button>
                  </div>
                  <button
                    type="button"
                    disabled={saving || !tokenValues[type]?.trim()}
                    onClick={() => saveToken(type)}
                    className="px-3 py-1.5 brand-grad hover:opacity-70 cursor-pointer rounded-lg text-xs font-medium text-white transition-colors whitespace-nowrap"
                  >
                    {existing ? "Update" : "Save"}
                  </button>
                </div>
                {tokenErrors[type] && <p className="mt-1 text-xs text-red-600">{tokenErrors[type]}</p>}
              </div>
            );
          })}
        </div>
      </div>

        {/* ── API Keys ───────────────────────────────────────── */}
        <div className="bg-white border border-gray-200 rounded-xl p-5 shadow-sm">
          <div className="flex items-center gap-2 mb-1">
            <Terminal size={15} className="text-gray-500"/>
            <h2 className="text-sm font-semibold text-gray-800">API Keys</h2>
          </div>
          <p className="text-xs text-gray-400 mb-4">
            Use these keys to connect Kilo Code, Cursor, or any OpenAI-compatible IDE plugin to AiNxt.
            Set the base URL to <code className="bg-gray-100 px-1 rounded text-gray-600">{window.location.origin + API}</code> and paste the key as the API key.
          </p>

          {/* One-time key reveal banner */}
          {newlyCreatedKey && (
            <div className="mb-4 p-3 bg-amber-50 border border-amber-300 rounded-lg">
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-semibold text-amber-800 mb-1">Copy your new API key — it won't be shown again</p>
                  <code className="text-xs text-amber-900 break-all font-mono">{newlyCreatedKey}</code>
                </div>
                <button
                  onClick={() => {
                    copyToClipboard(newlyCreatedKey);
                    setNewlyCreatedKey(null);
                    setNewlyCreatedKeyId(null);
                  }}
                  className="flex items-center gap-1 px-2 py-1 bg-amber-600 hover:bg-amber-700 text-white text-xs rounded shrink-0"
                >
                  <Copy size={12}/> Copy &amp; dismiss
                </button>
              </div>
            </div>
          )}

          {/* Existing keys list — endpoint keys (label "endpoint:*") are excluded */}
          {apiKeys.filter(k => k.is_active && !k.label?.startsWith("endpoint:")).length > 0 && (
            <div className="space-y-2 mb-4">
              {apiKeys.filter(k => k.is_active && !k.label?.startsWith("endpoint:")).map(k => (
                <div key={k.id} className="flex items-center justify-between border border-gray-200 rounded-lg px-3 py-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-xs font-mono text-gray-700">{k.key_prefix}…</span>
                      {k.label && <span className="text-[10px] text-gray-400 bg-gray-100 px-1.5 py-0.5 rounded">{k.label}</span>}
                    </div>
                    {/* DATE ONLY — deliberately no clock time.
                        The backend stamps these columns with datetime.utcnow(),
                        i.e. a NAIVE UTC value, into timestamptz columns. Postgres
                        reads the naive value as server-local (Asia/Calcutta), so
                        every stored timestamp sits 5h30m behind the real instant.
                        Showing the time therefore displayed a visibly wrong clock
                        value; the date is correct except for keys created between
                        00:00 and 05:30 IST, where the shift crosses midnight.
                        This is a display-side mitigation only — the underlying
                        naive-utcnow() issue is platform-wide and tracked
                        separately. Do NOT switch these back to toIST() without
                        fixing the write path first. */}
                    <div className="text-[10px] text-gray-400 mt-0.5">
                      Created {k.created_at ? toISTDate(k.created_at) : "—"}
                      {k.last_used_at
                        ? <> · Last used {toISTDate(k.last_used_at)}</>
                        : <> · Never used</>}
                      {k.expires_at && (
                        <> · <span className={k.is_expiring_soon ? "text-amber-600 font-medium" : ""}>
                          Expires {toISTDate(k.expires_at)}
                        </span></>
                      )}
                    </div>
                    {/* Expiring-soon nudge: there is no rotation flow — the user
                        revokes this key and generates a replacement. */}
                    {k.is_expiring_soon && (
                      <div className="text-[10px] text-amber-700 mt-1 flex items-center gap-1">
                        <AlertCircle size={10}/>
                        Expiring soon — revoke this key and generate a new one before it stops working.
                      </div>
                    )}
                  </div>
                  <button
                    onClick={() => revokeApiKey(k.id, k.key_prefix)}
                    className="flex items-center gap-1 text-[10px] text-red-400 hover:text-red-600 ml-4 shrink-0"
                  >
                    <Trash2 size={11}/> Revoke
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Generate new key */}
          {apiKeys.filter(k => k.is_active && !k.label?.startsWith("endpoint:")).length < 5 ? (
            <div>
              <div className="flex gap-2 items-center">
                <input
                  type="text"
                  value={newKeyLabel}
                  onChange={e => handleProfileChange("newKeyLabel", e.target.value, setNewKeyLabel)}
                  onBlur={e => handleProfileBlur("newKeyLabel", e.target.value)}
                  placeholder="Label (required) — e.g. Kilo Code laptop"
                  className={`flex-1 border rounded-lg px-3 py-1.5 text-xs text-gray-900 focus:outline-none focus:border-indigo-300 ${formErrors.newKeyLabel ? "border-red-500" : "border-gray-300"}`}
                />
                <button
                  onClick={generateApiKey}
                  disabled={akSaving || !newKeyLabel.trim()}
                  title={!newKeyLabel.trim() ? "Enter a label first" : undefined}
                  className="px-3 py-1.5 brand-grad hover:opacity-70 rounded-lg text-xs font-medium text-white transition-colors whitespace-nowrap cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {akSaving ? "Generating…" : "Generate Key"}
                </button>
              </div>
              {formErrors.newKeyLabel && <p className="mt-1 text-xs text-red-600">{formErrors.newKeyLabel}</p>}
            </div>
          ) : (
            <p className="text-xs text-gray-400">Maximum of 5 active keys reached. Revoke one to create a new key.</p>
          )}
        </div>

      </div>{/* end scrollable body */}
    </div>
  );
}