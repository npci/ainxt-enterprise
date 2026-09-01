// SPDX-License-Identifier: Apache-2.0
/**
 * Security Validation Utility — v2
 *
 * Field-type based validation:
 *   - validateXSS()        → core XSS check, applied to ALL fields
 *   - validateIdentifier() → names, codes, tags (strict chars + XSS)
 *   - validateFreeText()   → descriptions, reasons, prompts (XSS only, all punctuation allowed)
 *   - validateURLField()   → URLs (scheme check + no script inside)
 *
 * Rules:
 *   - No min/max character length limits
 *   - No SQL injection checks (parameterized queries handle this)
 *   - No SPECIAL_CHARS blocking on free-text fields
 *   - Blocks: XSS tags, event handlers, JS schemes, function calls, encoding bypasses
 *   - Allows: all normal punctuation in free-text (& @ % $ * ! ~ ^ ' , . - etc.)
 *
 * ── Kill-switch (INPUT_SANITIZATION_ENABLED) ────────────────────────────────
 * Every validator here funnels through _checkXSS() and sanitizeInput() (see
 * below), both of which become no-ops when the backend's
 * `INPUT_SANITIZATION_ENABLED` flag is off — the same single kill-switch that
 * gates every check in core/security_validation.py. The handful of checks
 * that live outside those two functions (validateIdentifier()'s dangerous-char
 * deny-list, validateURLField()'s scheme requirement, the broadcast helpers'
 * CRLF/HTML-tag checks) each test the same flag explicitly, mirroring the
 * Python `_sanitization_enabled()` guard call-for-call. The flag itself is
 * fetched once from `GET /auth/ui-config` (a public, no-auth endpoint the
 * frontend already calls for other feature flags) and defaults to `true`
 * (fail-open, matching the backend's own default) until that response
 * arrives — so the very first render never blocks on a network round trip.
 */

import { API_BASE } from "../config";

// Module-level flag, populated asynchronously below. Defaults to `true` so
// validation behaves normally before the /auth/ui-config response lands —
// the backend remains the authoritative enforcer regardless of this flag's
// state client-side, so a stale/optimistic `true` here never widens what the
// server actually accepts.
let _sanitizationEnabled = true;

if ( typeof fetch !== "undefined" )
{
  fetch( `${ API_BASE }/auth/ui-config` )
    .then( ( r ) => ( r.ok ? r.json() : null ) )
    .then( ( d ) =>
    {
      if ( d && typeof d.input_sanitization_enabled === "boolean" )
      {
        _sanitizationEnabled = d.input_sanitization_enabled;
      }
    } )
    .catch( () => { /* network hiccup — keep the fail-open default */ } );
}

/** Read the live kill-switch value — same flag as core.config.INPUT_SANITIZATION_ENABLED. */
export function isSanitizationEnabled ()
{
  return _sanitizationEnabled;
}

// ── XSS Detection Patterns ──────────────────────────────────
// No 'g' flag — used with .test() which is stateful with /g
const XSS_PATTERNS = {
  // HTML/Script injection
  SCRIPT_TAG: /<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/i,
  IFRAME_TAG: /<iframe[^>]*>/i,
  OBJECT_TAG: /<object[^>]*>/i,
  EMBED_TAG: /<embed[^>]*>/i,
  LINK_TAG: /<link[^>]*>/i,
  META_TAG: /<meta[^>]*>/i,
  STYLE_TAG: /<style[^>]*>[^]*?<\/style>/i,
  HTML_TAG: /<\/?[a-z][a-z0-9]*[^>]*>/i,

  // Event handlers
  ON_EVENT: /\bon\w+\s*=\s*["']?[^"'>]*/i,

  // Dangerous schemes
  JAVASCRIPT_SCHEME: /javascript\s*:/i,
  VBSCRIPT_SCHEME: /vbscript\s*:/i,
  DATA_URI: /data\s*:\s*text\/html/i,

  // Function calls (JS injection via attribute values, URLs, etc.)
  FUNC_ALERT: /\balert\s*\(/i,
  FUNC_EVAL: /\beval\s*\(/i,
  FUNC_SETTIMEOUT: /\bsetTimeout\s*\(/i,
  FUNC_SETINTERVAL: /\bsetInterval\s*\(/i,
  FUNC_FUNCTION: /\bFunction\s*\(/i,
  FUNC_DOC_COOKIE: /document\s*\.\s*cookie/i,
  FUNC_DOC_WRITE: /document\s*\.\s*write\s*\(/i,
  FUNC_INNERHTML: /\.innerHTML\s*=/i,
  FUNC_OUTERHTML: /\.outerHTML\s*=/i,
  FUNC_WIN_LOCATION: /window\s*\.\s*location/i,
  FUNC_DOC_CREATE: /document\s*\.\s*createElement\s*\(/i,

  // Encoding bypasses
  HTML_ENTITY_SCRIPT: /&#x0*3[cC]\s*;?\s*s\s*c\s*r\s*i\s*p\s*t/i,   // &#x3C;script
  UNICODE_ESCAPE: /\\u003[cC]/i,                                    // \u003c

  // Control characters
  NULL_BYTES: /\x00/,
  CONTROL_CHARS: /[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]/,
};

// Chars dangerous in identifiers (names, codes, tags) — NOT applied to free text
const IDENTIFIER_DANGEROUS = /[<>{}[\]`|\\]/;

// ── Error Messages ──────────────────────────────────────────
const MSG = {
  required: "This field is required",
  xss_script: "Script tags are not allowed",
  xss_html: "HTML tags are not allowed",
  xss_iframe: "Iframes are not allowed",
  xss_event: "Event handlers are not allowed",
  xss_scheme: "JavaScript/VBScript URLs are not allowed",
  xss_func: "JavaScript function calls are not allowed",
  xss_encoding: "Encoded script patterns are not allowed",
  xss_control: "Control characters are not allowed",
  id_chars: "Characters < > { } [ ] ` | \\ are not allowed",
  url_scheme: "URL must start with http:// or https://",
  url_script: "Script content is not allowed in URLs",
  code_format: "Only uppercase letters, numbers, and underscores allowed",
  jira_format: "Jira key must be in format PROJECT-123",
  branch_format: "Branch can only contain letters, numbers, hyphens, underscores, dots, and forward slashes",
  no_spaces: "This field cannot contain spaces — use hyphens or underscores",
  no_email: "This looks like an email — please enter a display name",
  gitlab_format: "Only letters, numbers, dots, hyphens, and underscores allowed",
};

// ── Core XSS Checker (applied to ALL fields) ───────────────
function _checkXSS ( text )
{
  const errors = [];
  if ( !_sanitizationEnabled ) return errors;
  if ( typeof text !== "string" || !text.trim() ) return errors;

  const t = text;

  // Script/HTML tags
  if ( XSS_PATTERNS.SCRIPT_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_script } );
  if ( XSS_PATTERNS.IFRAME_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_iframe } );
  if ( XSS_PATTERNS.OBJECT_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.EMBED_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.LINK_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.META_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.STYLE_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.HTML_TAG.test( t ) ) errors.push( { type: "xss", message: MSG.xss_html } );

  // Event handlers
  if ( XSS_PATTERNS.ON_EVENT.test( t ) ) errors.push( { type: "xss", message: MSG.xss_event } );

  // Dangerous schemes
  if ( XSS_PATTERNS.JAVASCRIPT_SCHEME.test( t ) ) errors.push( { type: "xss", message: MSG.xss_scheme } );
  if ( XSS_PATTERNS.VBSCRIPT_SCHEME.test( t ) ) errors.push( { type: "xss", message: MSG.xss_scheme } );
  if ( XSS_PATTERNS.DATA_URI.test( t ) ) errors.push( { type: "xss", message: MSG.xss_scheme } );

  // Function calls
  if ( XSS_PATTERNS.FUNC_ALERT.test( t ) ||
    XSS_PATTERNS.FUNC_EVAL.test( t ) ||
    XSS_PATTERNS.FUNC_SETTIMEOUT.test( t ) ||
    XSS_PATTERNS.FUNC_SETINTERVAL.test( t ) ||
    XSS_PATTERNS.FUNC_FUNCTION.test( t ) ||
    XSS_PATTERNS.FUNC_DOC_COOKIE.test( t ) ||
    XSS_PATTERNS.FUNC_DOC_WRITE.test( t ) ||
    XSS_PATTERNS.FUNC_INNERHTML.test( t ) ||
    XSS_PATTERNS.FUNC_OUTERHTML.test( t ) ||
    XSS_PATTERNS.FUNC_WIN_LOCATION.test( t ) ||
    XSS_PATTERNS.FUNC_DOC_CREATE.test( t ) )
  {
    errors.push( { type: "xss", message: MSG.xss_func } );
  }

  // Encoding bypasses
  if ( XSS_PATTERNS.HTML_ENTITY_SCRIPT.test( t ) ||
    XSS_PATTERNS.UNICODE_ESCAPE.test( t ) )
  {
    errors.push( { type: "xss", message: MSG.xss_encoding } );
  }

  // Control characters / null bytes
  if ( XSS_PATTERNS.NULL_BYTES.test( t ) || XSS_PATTERNS.CONTROL_CHARS.test( t ) )
  {
    errors.push( { type: "xss", message: MSG.xss_control } );
  }

  // Deduplicate by message
  const seen = new Set();
  return errors.filter( e => { if ( seen.has( e.message ) ) return false; seen.add( e.message ); return true; } );
}


// ============================================================
// PUBLIC API — Category-based validators
// ============================================================

/**
 * validateXSS — raw XSS check, use for custom field validation
 * Applied to ALL field types as the base layer.
 */
export function validateXSS ( text )
{
  if ( typeof text !== "string" ) return { isValid: true, errors: [] };
  const errors = _checkXSS( text );
  return { isValid: errors.length === 0, errors };
}


/**
 * validateIdentifier — for names, codes, tags, labels
 * Blocks: XSS + dangerous chars (< > { } [ ] ` | \)
 * Allows: letters, numbers, spaces, hyphens, underscores, dots, parentheses
 */
export function validateIdentifier ( value )
{
  const errors = [];
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: ( value || "" ).trim() };
  }
  const trimmed = value.trim();

  // XSS check
  errors.push( ..._checkXSS( trimmed ) );

  // Dangerous chars for identifiers — gated by the same kill-switch, mirroring
  // Python's `if _sanitization_enabled() and IDENTIFIER_DANGEROUS.search(val)`.
  if ( _sanitizationEnabled && IDENTIFIER_DANGEROUS.test( trimmed ) )
  {
    errors.push( { type: "format", message: MSG.id_chars } );
  }

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


/**
 * validateFreeText — for descriptions, reasons, prompts, notes, custom_instructions
 * Blocks: XSS only (tags, event handlers, function calls, encoding bypasses)
 * Allows: ALL normal punctuation — & @ % $ * ! ~ ^ ' , . - ( ) " ? : ; # + =
 */
export function validateFreeText ( value )
{
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: ( value || "" ).trim() };
  }
  const trimmed = value.trim();
  const errors = _checkXSS( trimmed );
  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


/**
 * validateURLField — for URL fields (jira_url, gitlab_url, confluence_url, etc.)
 * Checks: must be http:// or https://, no <script> inside
 * Allows: all URL characters (/ : ? & = % # @ .)
 */
export function validateURLField ( value, fieldName = "URL" )
{
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: "" };
  }
  const trimmed = value.trim();
  // Kill-switch — mirrors Python's `if not _sanitization_enabled(): return True, [], val`.
  if ( !_sanitizationEnabled ) return { isValid: true, errors: [], sanitized: trimmed };
  const errors = [];

  // Scheme check — must be http or https
  if ( !/^https?:\/\//i.test( trimmed ) )
  {
    errors.push( { type: "format", message: `${ fieldName }: ${ MSG.url_scheme }` } );
    return { isValid: false, errors, sanitized: trimmed };
  }

  // No script/XSS inside the URL
  if ( XSS_PATTERNS.SCRIPT_TAG.test( trimmed ) ||
    XSS_PATTERNS.IFRAME_TAG.test( trimmed ) ||
    XSS_PATTERNS.HTML_TAG.test( trimmed ) ||
    /javascript\s*:/i.test( trimmed ) )
  {
    errors.push( { type: "xss", message: `${ fieldName }: ${ MSG.url_script }` } );
  }

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


// ── Broadcast email validators ──────────────────────────────
// Mirrors core/security_validation.py's broadcast-specific validators —
// keep both files in sync when either one changes.

// Broadcast subject lines feed straight into the email's `Subject:` header —
// a raw CR or LF would let a sender inject extra headers (e.g. Bcc:) into the
// outgoing message. _checkXSS()'s CONTROL_CHARS pattern deliberately skips
// \r/\n (0x0D/0x0A) since normal free-text fields want to keep real newlines,
// so this is enforced separately, specific to this one header-bound field.
const CRLF_RE = /[\r\n]/;

/**
 * validateBroadcastSubject — for the broadcast email `subject` field.
 * Blocks: CR/LF (header injection) + XSS.
 */
export function validateBroadcastSubject ( value )
{
  if ( typeof value !== "string" ) return { isValid: true, errors: [], sanitized: "" };
  const errors = [];
  // Kill-switch on the CRLF check specifically, mirroring Python's
  // `if _sanitization_enabled() and _CRLF_RE.search(subject): ... else: validate_xss(...)`.
  if ( _sanitizationEnabled && CRLF_RE.test( value ) )
  {
    errors.push( { type: "format", message: "Line breaks are not allowed in the subject line" } );
  } else
  {
    errors.push( ..._checkXSS( value ) );
  }
  return { isValid: errors.length === 0, errors, sanitized: sanitizeInput( value ) };
}

/**
 * validateBroadcastHtmlBody — for the broadcast email `html_body` field.
 * html_body is a legitimate rich-text/WYSIWYG email body — it's SUPPOSED to
 * contain ordinary HTML (<div>, <table>, <img>, <a>, <b>, ...), so running it
 * through validateFreeText()'s blanket HTML_TAG check would reject every real
 * broadcast. Only the genuinely dangerous constructs are blocked: <script>/
 * <iframe>/<object>/<embed>, inline event handlers, and javascript: URLs —
 * the same subset _checkXSS() would flag minus the "any HTML tag" rule.
 */
export function validateBroadcastHtmlBody ( html )
{
  if ( typeof html !== "string" || !html.trim() )
  {
    return { isValid: true, errors: [], sanitized: html || "" };
  }
  // Kill-switch — mirrors Python's `_check_html_body_xss()`:
  // `if not _sanitization_enabled() or ...: return []`.
  if ( !_sanitizationEnabled ) return { isValid: true, errors: [], sanitized: html };
  const errors = [];
  if ( XSS_PATTERNS.SCRIPT_TAG.test( html ) ) errors.push( { type: "xss", message: MSG.xss_script } );
  if ( XSS_PATTERNS.IFRAME_TAG.test( html ) ) errors.push( { type: "xss", message: MSG.xss_iframe } );
  if ( XSS_PATTERNS.OBJECT_TAG.test( html ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.EMBED_TAG.test( html ) ) errors.push( { type: "xss", message: MSG.xss_html } );
  if ( XSS_PATTERNS.ON_EVENT.test( html ) ) errors.push( { type: "xss", message: MSG.xss_event } );
  if ( XSS_PATTERNS.JAVASCRIPT_SCHEME.test( html ) ) errors.push( { type: "xss", message: MSG.xss_scheme } );
  if ( XSS_PATTERNS.NULL_BYTES.test( html ) ) errors.push( { type: "xss", message: MSG.xss_control } );

  const seen = new Set();
  const deduped = errors.filter( e => { if ( seen.has( e.message ) ) return false; seen.add( e.message ); return true; } );
  return { isValid: deduped.length === 0, errors: deduped, sanitized: html };
}


// ============================================================
// FORMAT VALIDATORS — per-field type
// ============================================================

/**
 * validateProductCode — uppercase + numbers + underscores, must start with letter
 */
export function validateProductCode ( code )
{
  const errors = [];
  if ( typeof code !== "string" || !code.trim() )
  {
    return { isValid: true, errors: [], sanitized: ( code || "" ).trim().toUpperCase() };
  }
  const trimmed = code.trim().toUpperCase();

  if ( !/^[A-Z][A-Z0-9_]*$/.test( trimmed ) )
  {
    errors.push( { type: "format", message: MSG.code_format } );
  }
  errors.push( ..._checkXSS( trimmed ) );

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


/**
 * validateJiraKey — PROJECT-123 format
 */
export function validateJiraKey ( value )
{
  const errors = [];
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: ( value || "" ).trim() };
  }
  const trimmed = value.trim().toUpperCase();

  if ( !/^[A-Z][A-Z0-9_]+-\d+$/.test( trimmed ) )
  {
    errors.push( { type: "format", message: MSG.jira_format } );
  }

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


/**
 * validateBranch — letters, numbers, hyphens, underscores, dots, forward slashes
 */
export function validateBranch ( value )
{
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: "" };
  }
  const trimmed = value.trim();
  const errors = [];

  if ( !/^[a-zA-Z0-9/_\-.]+$/.test( trimmed ) )
  {
    errors.push( { type: "format", message: MSG.branch_format } );
  }

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


/**
 * validateGitlabUsername — letters, numbers, dots, hyphens, underscores
 */
export function validateGitlabUsername ( value )
{
  if ( typeof value !== "string" || !value.trim() )
  {
    return { isValid: true, errors: [], sanitized: "" };
  }
  const trimmed = value.trim();
  const errors = [];

  if ( !/^[a-zA-Z0-9._-]+$/.test( trimmed ) )
  {
    errors.push( { type: "format", message: MSG.gitlab_format } );
  }

  return { isValid: errors.length === 0, errors, sanitized: trimmed };
}


// ============================================================
// BACKWARD COMPAT — old function names still work
// Components import these; they delegate to new functions.
// ============================================================

/** @deprecated Use validateIdentifier() */
export function validateProductName ( name )
{
  return validateIdentifier( name );
}

/** @deprecated Use validateFreeText() */
export function validateDescription ( description )
{
  return validateFreeText( description );
}

/** @deprecated Use validateXSS() */
export function validateSecurity ( text, options = {} )
{
  // Old signature returned { isValid, errors, sanitized }
  const result = validateXSS( text );
  return { ...result, sanitized: ( text || "" ).trim() };
}

/** @deprecated Use validateURLField() */
export function validateURL ( url, options = {} )
{
  const fieldName = options?.fieldName || "URL";
  return validateURLField( url, fieldName );
}

/** @deprecated Use validateIdentifier() per repo or just validateXSS() */
export function validateRepoName ( repoName )
{
  if ( typeof repoName !== "string" || !repoName.trim() )
  {
    return { isValid: true, errors: [], sanitized: "" };
  }
  // No strict org/repo format — repos can be group/subgroup/project
  return validateFreeText( repoName );
}

/** @deprecated */
export function validateRepos ( repos )
{
  if ( !Array.isArray( repos ) || repos.length === 0 )
  {
    return { isValid: true, errors: [], sanitized: [] };
  }
  const errors = [];
  const sanitized = repos.map( ( r, i ) =>
  {
    const val = typeof r === "string" ? r : r?.repo_name || "";
    const check = validateFreeText( val );
    if ( !check.isValid ) errors.push( ...check.errors.map( e => ( { ...e, index: i } ) ) );
    return typeof r === "string" ? check.sanitized : { ...r, repo_name: check.sanitized };
  } );
  return { isValid: errors.length === 0, errors, sanitized };
}

/** @deprecated */
export function validateDepartments ( depts )
{
  if ( !Array.isArray( depts ) || depts.length === 0 )
  {
    return { isValid: false, errors: [ { type: "required", message: "Select at least one department" } ], sanitized: [] };
  }
  const errors = [];
  const sanitized = depts.map( ( d, i ) =>
  {
    if ( typeof d === "string" )
    {
      const check = validateIdentifier( d );
      if ( !check.isValid ) errors.push( ...check.errors.map( e => ( { ...e, index: i } ) ) );
      return check.sanitized;
    }
    return d;
  } );
  return { isValid: errors.length === 0, errors, sanitized };
}

/** @deprecated */
export function validateProductForm ( form )
{
  const result = { isValid: true, errors: {}, sanitized: {} };
  for ( const [ key, val ] of Object.entries( form ) )
  {
    const check = validateFreeText( String( val || "" ) );
    if ( !check.isValid ) { result.isValid = false; result.errors[ key ] = check.errors; }
    result.sanitized[ key ] = check.sanitized;
  }
  return result;
}

/** @deprecated Use validateFreeText() */
export function validateSummary ( value )
{
  return validateFreeText( value );
}

/** Sanitize input — strip dangerous tags. Mirrors sanitize_input() in
 * core/security_validation.py, including the kill-switch: when
 * INPUT_SANITIZATION_ENABLED is off, the input passes through completely
 * unchanged (not even trimmed), exactly like the Python side. */
export function sanitizeInput ( input, options = {} )
{
  if ( typeof input !== "string" ) return "";
  if ( !_sanitizationEnabled ) return input;
  let s = input;
  s = s.replace( /\x00/g, "" );
  if ( options.allowFormatting )
  {
    s = s.replace( /[\x00-\x08\x0E-\x1F]/g, "" );
  } else
  {
    s = s.replace( /[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]/g, "" );
  }
  return s.trim();
}

/** Get first error message for a field */
export function getErrorMessage ( errors, field )
{
  if ( errors[ field ] && errors[ field ].length > 0 ) return errors[ field ][ 0 ].message;
  return "";
}

/** Check if field has errors */
export function hasErrors ( errors, field )
{
  return errors[ field ] && errors[ field ].length > 0;
}

export default {
  // New API
  isSanitizationEnabled,
  validateXSS,
  validateIdentifier,
  validateFreeText,
  validateURLField,
  validateProductCode,
  validateJiraKey,
  validateBranch,
  validateGitlabUsername,
  validateBroadcastSubject,
  validateBroadcastHtmlBody,
  // Backward compat
  validateProductName,
  validateDescription,
  validateSecurity,
  validateURL,
  validateRepoName,
  validateRepos,
  validateDepartments,
  validateProductForm,
  validateSummary,
  // Utilities
  sanitizeInput,
  getErrorMessage,
  hasErrors,
  // Constants (for components that need direct access)
  MSG,
};
