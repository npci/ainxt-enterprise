# SPDX-License-Identifier: MIT
"""
Security Validation Module — v2

Field-type based validation:
  - validate_xss()         → core XSS check, applied to ALL fields
  - validate_identifier()  → names, codes, tags (strict chars + XSS)
  - validate_free_text()   → descriptions, reasons, prompts (XSS only, all punctuation allowed)
  - validate_url_field()   → URLs (scheme check + no script inside)

Rules:
  - No min/max character length limits
  - No SQL injection checks (parameterized queries handle this)
  - No SPECIAL_CHARS blocking on free-text fields
  - Blocks: XSS tags, event handlers, JS schemes, function calls, encoding bypasses
  - Allows: all normal punctuation in free-text (& @ % $ * ! ~ ^ ' , . - etc.)

── Global kill-switch (INPUT_SANITIZATION_ENABLED) ───────────────────────────
Every check in this module funnels through _check_xss() (XSS/script/event-
handler/encoding detection) and sanitize_input() (null-byte + control-char
stripping) — see _sanitization_enabled() below. Both become no-ops when
core.config.INPUT_SANITIZATION_ENABLED=False: _check_xss() reports no errors
and sanitize_input() returns the input completely unchanged. The few checks
that live outside those two functions (validate_identifier()'s character
deny-list, validate_url_field()'s scheme requirement, validate_email_field()'s
format regex, and the auth-specific `/` deny-list) each check the same flag
explicitly, so disabling it makes every validator in this file a pure
pass-through: whatever the caller sent is what comes back, unexamined.

This is a single GLOBAL flag (not per-router, per-field, or hot-reloadable —
it's read fresh from core.config on every call, but core.config itself reads
the env var once at process start, consistent with every other feature flag
in that module). It does not affect required-field checks or enum/format
validation that exists for application correctness rather than security
(e.g. "priority must be one of Low/Medium/High/Critical", Jira key format,
password minimum length) — those keep running even with sanitization off,
since skipping them would break app logic rather than just widen what
characters are accepted.
"""

import re
from typing import Dict, List, Optional, Any, Tuple

import core.config as _config


def _sanitization_enabled() -> bool:
    """Read the live value of the global kill-switch on every call (not cached
    at import time) so a test or an ops tool that reloads core.config takes
    effect without needing to reimport this module."""
    return bool(getattr(_config, "INPUT_SANITIZATION_ENABLED", True))


# ── XSS Detection Patterns ──────────────────────────────────────
XSS_PATTERNS = {
    # HTML/Script injection
    "SCRIPT_TAG":     re.compile(r"<script\b[^<]*(?:(?!</script>)<[^<]*)*</script>", re.I),
    "IFRAME_TAG":     re.compile(r"<iframe[^>]*>", re.I),
    "OBJECT_TAG":     re.compile(r"<object[^>]*>", re.I),
    "EMBED_TAG":      re.compile(r"<embed[^>]*>", re.I),
    "LINK_TAG":       re.compile(r"<link[^>]*>", re.I),
    "META_TAG":       re.compile(r"<meta[^>]*>", re.I),
    "STYLE_TAG":      re.compile(r"<style[^>]*>[\s\S]*?</style>", re.I),
    "HTML_TAG":       re.compile(r"</?[a-z][a-z0-9]*[^>]*>", re.I),

    # Event handlers
    "ON_EVENT":       re.compile(r"\bon\w+\s*=\s*['\"]?[^'\"'>]*", re.I),

    # Dangerous schemes
    "JAVASCRIPT":     re.compile(r"javascript\s*:", re.I),
    "VBSCRIPT":       re.compile(r"vbscript\s*:", re.I),
    "DATA_URI":       re.compile(r"data\s*:\s*text/html", re.I),

    # Function calls
    "FUNC_ALERT":     re.compile(r"\balert\s*\(", re.I),
    "FUNC_EVAL":      re.compile(r"\beval\s*\(", re.I),
    "FUNC_SETTIMEOUT": re.compile(r"\bsetTimeout\s*\(", re.I),
    "FUNC_SETINTERVAL": re.compile(r"\bsetInterval\s*\(", re.I),
    "FUNC_FUNCTION":  re.compile(r"\bFunction\s*\(", re.I),
    "FUNC_DOC_COOKIE": re.compile(r"document\s*\.\s*cookie", re.I),
    "FUNC_DOC_WRITE": re.compile(r"document\s*\.\s*write\s*\(", re.I),
    "FUNC_INNERHTML": re.compile(r"\.innerHTML\s*=", re.I),
    "FUNC_OUTERHTML": re.compile(r"\.outerHTML\s*=", re.I),
    "FUNC_WIN_LOC":   re.compile(r"window\s*\.\s*location", re.I),
    "FUNC_DOC_CREATE": re.compile(r"document\s*\.\s*createElement\s*\(", re.I),

    # Encoding bypasses
    "HTML_ENTITY_SCRIPT": re.compile(r"&#x0*3[cC]\s*;?\s*s\s*c\s*r\s*i\s*p\s*t", re.I),
    "UNICODE_ESCAPE":     re.compile(r"\\u003[cC]", re.I),

    # Control characters
    "NULL_BYTES":     re.compile(r"\x00"),
    "CONTROL_CHARS":  re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]"),
}

# Chars dangerous in identifiers (names, codes, tags) — NOT applied to free text
IDENTIFIER_DANGEROUS = re.compile(r"[<>{}\[\]`|\\]")

# ── Error Messages ──────────────────────────────────────────────
MSG = {
    "required":      "This field is required",
    "xss_script":    "Script tags are not allowed",
    "xss_html":      "HTML tags are not allowed",
    "xss_iframe":    "Iframes are not allowed",
    "xss_event":     "Event handlers are not allowed",
    "xss_scheme":    "JavaScript/VBScript URLs are not allowed",
    "xss_func":      "JavaScript function calls are not allowed",
    "xss_encoding":  "Encoded script patterns are not allowed",
    "xss_control":   "Control characters are not allowed",
    "id_chars":      "Characters < > { } [ ] ` | \\ are not allowed",
    "url_scheme":    "URL must start with http:// or https://",
    "url_script":    "Script content is not allowed in URLs",
}


# ── Core XSS Checker ────────────────────────────────────────────

def _check_xss(text: str) -> List[str]:
    """Check text for XSS patterns. Returns list of error messages."""
    if not _sanitization_enabled():
        return []
    if not isinstance(text, str) or not text.strip():
        return []

    errors = []

    # Script/HTML tags
    if XSS_PATTERNS["SCRIPT_TAG"].search(text):    errors.append(MSG["xss_script"])
    if XSS_PATTERNS["IFRAME_TAG"].search(text):    errors.append(MSG["xss_iframe"])
    if XSS_PATTERNS["OBJECT_TAG"].search(text):    errors.append(MSG["xss_html"])
    if XSS_PATTERNS["EMBED_TAG"].search(text):     errors.append(MSG["xss_html"])
    if XSS_PATTERNS["LINK_TAG"].search(text):      errors.append(MSG["xss_html"])
    if XSS_PATTERNS["META_TAG"].search(text):      errors.append(MSG["xss_html"])
    if XSS_PATTERNS["STYLE_TAG"].search(text):     errors.append(MSG["xss_html"])
    if XSS_PATTERNS["HTML_TAG"].search(text):      errors.append(MSG["xss_html"])

    # Event handlers
    if XSS_PATTERNS["ON_EVENT"].search(text):      errors.append(MSG["xss_event"])

    # Dangerous schemes
    if XSS_PATTERNS["JAVASCRIPT"].search(text):    errors.append(MSG["xss_scheme"])
    if XSS_PATTERNS["VBSCRIPT"].search(text):      errors.append(MSG["xss_scheme"])
    if XSS_PATTERNS["DATA_URI"].search(text):      errors.append(MSG["xss_scheme"])

    # Function calls
    func_patterns = [
        "FUNC_ALERT", "FUNC_EVAL", "FUNC_SETTIMEOUT", "FUNC_SETINTERVAL",
        "FUNC_FUNCTION", "FUNC_DOC_COOKIE", "FUNC_DOC_WRITE",
        "FUNC_INNERHTML", "FUNC_OUTERHTML", "FUNC_WIN_LOC", "FUNC_DOC_CREATE",
    ]
    for p in func_patterns:
        if XSS_PATTERNS[p].search(text):
            errors.append(MSG["xss_func"])
            break

    # Encoding bypasses
    if XSS_PATTERNS["HTML_ENTITY_SCRIPT"].search(text) or \
       XSS_PATTERNS["UNICODE_ESCAPE"].search(text):
        errors.append(MSG["xss_encoding"])

    # Control characters / null bytes
    if XSS_PATTERNS["NULL_BYTES"].search(text) or \
       XSS_PATTERNS["CONTROL_CHARS"].search(text):
        errors.append(MSG["xss_control"])

    # Deduplicate
    return list(dict.fromkeys(errors))


def sanitize_input(text: str, allow_formatting: bool = False) -> str:
    """Strip null bytes and control characters."""
    if not isinstance(text, str):
        return ""
    if not _sanitization_enabled():
        return text
    text = XSS_PATTERNS["NULL_BYTES"].sub("", text)
    if allow_formatting:
        text = re.sub(r"[\x00-\x08\x0E-\x1F]", "", text)
    else:
        text = XSS_PATTERNS["CONTROL_CHARS"].sub("", text)
    return text.strip()


# ============================================================
# PUBLIC API — Category-based validators
# ============================================================

def validate_xss(text: str) -> Tuple[bool, List[str]]:
    """Core XSS check — applied to ALL fields."""
    errors = _check_xss(text)
    return len(errors) == 0, errors


def validate_identifier(value: str) -> Tuple[bool, List[str], str]:
    """For names, codes, tags, labels. Blocks XSS + dangerous chars."""
    if not value or not str(value).strip():
        return True, [], ""
    val = str(value).strip()
    errors = _check_xss(val)
    if _sanitization_enabled() and IDENTIFIER_DANGEROUS.search(val):
        errors.append(MSG["id_chars"])
    return len(errors) == 0, errors, sanitize_input(val)


def validate_free_text(value: str) -> Tuple[bool, List[str], str]:
    """For descriptions, reasons, prompts. XSS only — all punctuation allowed."""
    if not value or not str(value).strip():
        return True, [], ""
    val = str(value).strip()
    errors = _check_xss(val)
    return len(errors) == 0, errors, sanitize_input(val, allow_formatting=True)


def validate_url_field(value: str, field_name: str = "URL") -> Tuple[bool, List[str], str]:
    """For URL fields. Must be http/https, no script inside."""
    if not value or not str(value).strip():
        return True, [], ""
    val = str(value).strip()
    if not _sanitization_enabled():
        return True, [], val
    errors = []

    # Scheme check
    if not re.match(r"^https?://", val, re.I):
        errors.append(f"{field_name}: {MSG['url_scheme']}")
        return False, errors, val

    # No script/XSS inside URL
    if XSS_PATTERNS["SCRIPT_TAG"].search(val) or \
       XSS_PATTERNS["IFRAME_TAG"].search(val) or \
       XSS_PATTERNS["HTML_TAG"].search(val) or \
       XSS_PATTERNS["JAVASCRIPT"].search(val):
        errors.append(f"{field_name}: {MSG['url_script']}")

    return len(errors) == 0, errors, val


# ============================================================
# BACKWARD COMPAT — old function names still work
# Routers import these; they delegate to new functions.
# ============================================================

def validate_security(text: str, check_sql: bool = True) -> Tuple[bool, List[str]]:
    """@deprecated — use validate_xss(). Kept for router compatibility."""
    return validate_xss(text)


def validate_product_name(name: str) -> Tuple[bool, List[str], str]:
    """@deprecated — use validate_identifier()."""
    return validate_identifier(name)


def validate_product_code(code: str) -> Tuple[bool, List[str], str]:
    """Product code: uppercase + numbers + underscores."""
    if not code or not str(code).strip():
        return True, [], ""
    val = str(code).strip().upper()
    errors = _check_xss(val)
    if not re.match(r"^[A-Z][A-Z0-9_]*$", val):
        errors.append("Only uppercase letters, numbers, and underscores allowed")
    return len(errors) == 0, errors, val


def validate_description(description: Optional[str]) -> Tuple[bool, List[str], str]:
    """@deprecated — use validate_free_text()."""
    return validate_free_text(description or "")


def validate_url(url: str, field_name: str = "URL") -> Tuple[bool, List[str], str]:
    """@deprecated — use validate_url_field()."""
    return validate_url_field(url, field_name)


def validate_repo_name(repo_name: str) -> Tuple[bool, List[str], str]:
    """Repo name — no strict format, just XSS check."""
    return validate_free_text(repo_name or "")


def validate_departments(depts: List[str]) -> Tuple[bool, List[str], List[str]]:
    """Department list — XSS check per item."""
    if not depts or len(depts) == 0:
        return False, ["Select at least one department"], []
    errors = []
    sanitized = []
    for dept in depts:
        if isinstance(dept, str):
            ok, errs, val = validate_identifier(dept)
            if not ok:
                errors.extend(errs)
            sanitized.append(val)
    return len(errors) == 0, errors, sanitized


# ============================================================
# COMPOSITE VALIDATORS — called by routers
# Each uses the appropriate category per field.
# ============================================================

def _body_val(body, field, default=""):
    """Safely get a field from a Pydantic model or dict."""
    if hasattr(body, field):
        return getattr(body, field)
    if isinstance(body, dict):
        return body.get(field, default)
    return default


def _flatten_errors(field_errors: Dict[str, List[str]]) -> str:
    """Flatten field errors into a single string for HTTP 400 response."""
    msgs = []
    for field, errors in field_errors.items():
        for e in errors:
            msgs.append(f"{field}: {e}")
    return "; ".join(msgs)


def validate_create_product_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_identifier(_body_val(body, "name"))
    if not ok: field_errors["name"] = errs
    sanitized["name"] = val

    ok, errs, val = validate_product_code(_body_val(body, "code"))
    if not ok: field_errors["code"] = errs
    sanitized["code"] = val

    ok, errs, val = validate_free_text(_body_val(body, "description", ""))
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    ok, errs, val = validate_url_field(_body_val(body, "jira_url", ""), "Jira URL")
    if not ok: field_errors["jira_url"] = errs
    sanitized["jira_url"] = val

    ok, errs, val = validate_url_field(_body_val(body, "confluence_url", ""), "Confluence URL")
    if not ok: field_errors["confluence_url"] = errs
    sanitized["confluence_url"] = val

    ok, errs, san = validate_departments(_body_val(body, "departments", []))
    if not ok: field_errors["departments"] = errs
    sanitized["departments"] = san

    # Repos — XSS check each repo_name
    repos = _body_val(body, "repos", [])
    sanitized_repos = []
    for r in (repos or []):
        rn = getattr(r, "repo_name", "") if hasattr(r, "repo_name") else (r.get("repo_name", "") if isinstance(r, dict) else "")
        br = getattr(r, "branch", "main") if hasattr(r, "branch") else (r.get("branch", "main") if isinstance(r, dict) else "main")
        ok, errs, val = validate_free_text(rn)
        if not ok: field_errors.setdefault("repos", []).extend(errs)
        sanitized_repos.append({"repo_name": val, "branch": sanitize_input(br or "main")})
    sanitized["repos"] = sanitized_repos

    return len(field_errors) == 0, field_errors, sanitized


def validate_update_product_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name = _body_val(body, "name")
    if name is not None:
        ok, errs, val = validate_identifier(name)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    desc = _body_val(body, "description")
    if desc is not None:
        ok, errs, val = validate_free_text(desc)
        if not ok: field_errors["description"] = errs
        sanitized["description"] = val

    jira = _body_val(body, "jira_url")
    if jira is not None:
        ok, errs, val = validate_url_field(jira, "Jira URL")
        if not ok: field_errors["jira_url"] = errs
        sanitized["jira_url"] = val

    conf = _body_val(body, "confluence_url")
    if conf is not None:
        ok, errs, val = validate_url_field(conf, "Confluence URL")
        if not ok: field_errors["confluence_url"] = errs
        sanitized["confluence_url"] = val

    depts = _body_val(body, "departments")
    if depts is not None:
        ok, errs, san = validate_departments(depts)
        if not ok: field_errors["departments"] = errs
        sanitized["departments"] = san

    return len(field_errors) == 0, field_errors, sanitized


def validate_agent_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    # name — mandatory
    name_raw = str(_body_val(body, "name", "") or "").strip()
    if not name_raw:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name_raw)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    # description — mandatory
    desc_raw = str(_body_val(body, "description", "") or "").strip()
    if not desc_raw:
        field_errors["description"] = [MSG["required"]]
        sanitized["description"] = ""
    else:
        ok, errs, val = validate_free_text(desc_raw)
        if not ok: field_errors["description"] = errs
        sanitized["description"] = val

    # system_prompt — mandatory
    prompt_raw = str(_body_val(body, "system_prompt", "") or "").strip()
    if not prompt_raw:
        field_errors["system_prompt"] = [MSG["required"]]
        sanitized["system_prompt"] = ""
    else:
        ok, errs, val = validate_free_text(prompt_raw)
        if not ok: field_errors["system_prompt"] = errs
        sanitized["system_prompt"] = val

    # Tags — identifier check per tag
    tags = _body_val(body, "tags", [])
    san_tags = []
    for tag in (tags if isinstance(tags, list) else []):
        ok, errs, val = validate_identifier(str(tag))
        if not ok: field_errors.setdefault("tags", []).extend(errs)
        else: san_tags.append(val)
    sanitized["tags"] = san_tags

    sanitized["version"] = sanitize_input(str(_body_val(body, "version", "1.0.0")))
    sanitized["author"] = sanitize_input(str(_body_val(body, "author", "platform")))
    sanitized["kb_namespace"] = sanitize_input(str(_body_val(body, "kb_namespace", "") or "")) or None

    return len(field_errors) == 0, field_errors, sanitized


def validate_workflow_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name_raw = str(_body_val(body, "name", "") or "").strip()
    if not name_raw:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name_raw)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    ok, errs, val = validate_free_text(_body_val(body, "description", ""))
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    # Steps
    steps = _body_val(body, "steps", [])
    san_steps = []
    for i, step in enumerate(steps if isinstance(steps, list) else []):
        step_name = str(getattr(step, "name", "") if hasattr(step, "name") else (step.get("name", "") if isinstance(step, dict) else ""))
        step_input = str(getattr(step, "input", "") if hasattr(step, "input") else (step.get("input", "") if isinstance(step, dict) else ""))
        step_errs = []

        ok, errs, val = validate_identifier(step_name)
        if not ok: step_errs.extend([f"Step {i+1} name: {e}" for e in errs])

        ok, errs, val2 = validate_free_text(step_input)
        if not ok: step_errs.extend([f"Step {i+1} input: {e}" for e in errs])

        if step_errs: field_errors.setdefault("steps", []).extend(step_errs)
        san_steps.append({
            "id": getattr(step, "id", f"step_{i+1}") if hasattr(step, "id") else (step.get("id", f"step_{i+1}") if isinstance(step, dict) else f"step_{i+1}"),
            "name": sanitize_input(step_name),
            "step_type": getattr(step, "step_type", "llm") if hasattr(step, "step_type") else (step.get("step_type", "llm") if isinstance(step, dict) else "llm"),
            "input": sanitize_input(step_input, allow_formatting=True),
            "depends_on": list(getattr(step, "depends_on", []) if hasattr(step, "depends_on") else (step.get("depends_on", []) if isinstance(step, dict) else [])),
        })
    sanitized["steps"] = san_steps

    return len(field_errors) == 0, field_errors, sanitized


def validate_skill_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    # name — mandatory
    name_raw = str(_body_val(body, "name", "") or "").strip()
    if not name_raw:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name_raw)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    # description — mandatory
    desc_raw = str(_body_val(body, "description", "") or "").strip()
    if not desc_raw:
        field_errors["description"] = [MSG["required"]]
        sanitized["description"] = ""
    else:
        ok, errs, val = validate_free_text(desc_raw)
        if not ok: field_errors["description"] = errs
        sanitized["description"] = val

    tags = _body_val(body, "tags", [])
    san_tags = []
    for tag in (tags if isinstance(tags, list) else []):
        ok, errs, val = validate_identifier(str(tag))
        if not ok: field_errors.setdefault("tags", []).extend(errs)
        else: san_tags.append(val)
    sanitized["tags"] = san_tags

    examples = _body_val(body, "examples", [])
    san_examples = []
    for ex in (examples if isinstance(examples, list) else []):
        ok, errs, val = validate_free_text(str(ex))
        if not ok: field_errors.setdefault("examples", []).extend(errs)
        else: san_examples.append(val)
    sanitized["examples"] = san_examples

    sanitized["author"] = sanitize_input(str(_body_val(body, "author", "platform")))
    sanitized["version"] = sanitize_input(str(_body_val(body, "version", "1.0.0")))

    return len(field_errors) == 0, field_errors, sanitized


def validate_budget_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    user_id = str(_body_val(body, "user_id", "")).strip()
    if not user_id:
        field_errors["user_id"] = [MSG["required"]]
    else:
        ok, errs = validate_xss(user_id)
        if not ok: field_errors["user_id"] = errs
    sanitized["user_id"] = sanitize_input(user_id)

    reason = str(_body_val(body, "reason", "") or "").strip()
    if reason:
        ok, errs, val = validate_free_text(reason)
        if not ok: field_errors["reason"] = errs
        sanitized["reason"] = val
    else:
        sanitized["reason"] = ""

    justification = str(_body_val(body, "justification", "") or "").strip()
    if justification:
        ok, errs, val = validate_free_text(justification)
        if not ok: field_errors["justification"] = errs
        sanitized["justification"] = val
    else:
        sanitized["justification"] = ""

    return len(field_errors) == 0, field_errors, sanitized


def validate_tool_register_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    # name — mandatory
    name_raw = str(_body_val(body, "name", "") or "").strip()
    if not name_raw:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name_raw)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    # description — mandatory
    desc_raw = str(_body_val(body, "description", "") or "").strip()
    if not desc_raw:
        field_errors["description"] = [MSG["required"]]
        sanitized["description"] = ""
    else:
        ok, errs, val = validate_free_text(desc_raw)
        if not ok: field_errors["description"] = errs
        sanitized["description"] = val

    url = str(_body_val(body, "url", "")).strip()
    if not url:
        field_errors["url"] = ["Endpoint URL is required"]
        sanitized["url"] = ""
    else:
        ok, errs, val = validate_url_field(url, "URL")
        if not ok: field_errors["url"] = errs
        sanitized["url"] = val

    tags = _body_val(body, "tags", [])
    san_tags = []
    for tag in (tags if isinstance(tags, list) else []):
        ok, errs, val = validate_identifier(str(tag))
        if not ok: field_errors.setdefault("tags", []).extend(errs)
        else: san_tags.append(val)
    sanitized["tags"] = san_tags

    return len(field_errors) == 0, field_errors, sanitized


def validate_external_server_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_identifier(_body_val(body, "name"))
    if not ok: field_errors["name"] = errs
    sanitized["name"] = val

    transport = str(_body_val(body, "transport", "")).strip()
    if transport not in ("stdio", "sse"):
        field_errors["transport"] = ["Transport must be 'stdio' or 'sse'"]
    sanitized["transport"] = transport

    sse_url = str(_body_val(body, "sse_url", "") or "").strip()
    if transport == "sse" and sse_url:
        ok, errs, val = validate_url_field(sse_url, "SSE URL")
        if not ok: field_errors["sse_url"] = errs
        sanitized["sse_url"] = val
    else:
        sanitized["sse_url"] = sse_url

    command = str(_body_val(body, "command", "") or "").strip()
    ok, errs, val = validate_free_text(command)
    if not ok: field_errors["command"] = errs
    sanitized["command"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_add_repo_request(body: Any) -> Tuple[bool, List[str], Dict[str, str]]:
    repo = str(_body_val(body, "repo_name", "")).strip()
    branch = str(_body_val(body, "branch", "main")).strip()
    ok, errs, val = validate_free_text(repo)
    return ok, errs, {"repo_name": val, "branch": sanitize_input(branch or "main")}


def validate_level_override_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    user_id = str(_body_val(body, "user_id", "")).strip()
    if not user_id:
        field_errors["user_id"] = [MSG["required"]]
    else:
        ok, errs = validate_xss(user_id)
        if not ok: field_errors["user_id"] = errs
    sanitized["user_id"] = sanitize_input(user_id)

    reason = str(_body_val(body, "reason", "")).strip()
    if not reason:
        field_errors["reason"] = [MSG["required"]]
    else:
        ok, errs, val = validate_free_text(reason)
        if not ok: field_errors["reason"] = errs
        sanitized["reason"] = val
    if "reason" not in sanitized:
        sanitized["reason"] = sanitize_input(reason, allow_formatting=True)

    return len(field_errors) == 0, field_errors, sanitized


# ── Auth validators (DAST fix — "Poor Input Validation") ────────────────────
# auth_router.py's pre-authentication endpoints (/auth/register, /auth/login)
# previously accepted unconstrained strings with no server-side validation.
# These reuse the same category validators as every other router.

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# The DAST report explicitly calls out `/` alongside `<` and `>` as an
# unvalidated character. validate_identifier()'s shared IDENTIFIER_DANGEROUS
# deny-list intentionally does NOT include `/` (some of the ~14 other routers
# using validate_identifier() legitimately allow it, e.g. tag/path-like
# values), so it is enforced here as an auth-specific addition instead of
# changing the shared function for every caller.
_AUTH_NAME_EXTRA_DANGEROUS = re.compile(r"/")


def validate_email_field(value: str) -> Tuple[bool, List[str], str]:
    """Basic e-mail format check + XSS check. Does not verify deliverability."""
    if not value or not str(value).strip():
        return False, [MSG["required"]], ""
    val = str(value).strip().lower()
    errors = _check_xss(val)
    if _sanitization_enabled() and not _EMAIL_RE.match(val):
        errors.append("Please enter a valid email address")
    return len(errors) == 0, errors, sanitize_input(val)


_PASSWORD_SPECIAL_RE = re.compile(r"[^A-Za-z0-9]")


def password_strength_errors(password: str) -> List[str]:
    """Return a list of unmet password-complexity rules, empty if all are met.

    Rules: minimum 8 characters, at least one uppercase letter, at least one
    digit, at least one special (non-alphanumeric) character. Deliberately
    NOT gated by INPUT_SANITIZATION_ENABLED — like the minimum-length check it
    replaces, this is a password-strength requirement, not an XSS/character-
    set sanitization concern, so the kill switch for the latter must not
    silently disable it (see the module docstring's "keep running even with
    sanitization off" note on minimum length, which this supersedes).

    Shared by validate_register_request() below and
    routers/auth_router.py::change_password() (new_password) — the ONE place
    users choose a new password, whether at signup or via the forced-reset
    flow (POST /auth/forgot-password only ever generates the temp password
    itself, which is not user-chosen and is exempt).
    """
    errors: List[str] = []
    if len(password) < 8:
        errors.append("Password must be at least 8 characters")
    if not any(c.isupper() for c in password):
        errors.append("Password must contain at least one uppercase letter")
    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one number")
    if not _PASSWORD_SPECIAL_RE.search(password):
        errors.append("Password must contain at least one special character")
    return errors


def validate_register_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """Validate/sanitize a RegisterRequest (routers/auth_router.py::register).

    - email    → format + XSS check (validate_email_field)
    - name     → allow-list identifier check, blocks < > { } [ ] ` | \\ (validate_identifier)
    - org_id   → same allow-list identifier check
    - password → intentionally NOT run through XSS checks (a password is never
                 rendered); password_strength_errors() enforces complexity
                 (length, uppercase, digit, special char) as defense-in-depth
                 — the client already enforces this, but the server must not
                 rely on it.
    - role     → already allow-listed by the caller (register()) against {"admin","user"}
                 before this validator runs; not re-validated here.
    """
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_email_field(_body_val(body, "email", ""))
    if not ok: field_errors["email"] = errs
    sanitized["email"] = val

    ok, errs, val = validate_identifier(_body_val(body, "name", ""))
    if _sanitization_enabled() and _AUTH_NAME_EXTRA_DANGEROUS.search(str(_body_val(body, "name", "") or "")):
        ok = False
        errs = errs + [MSG["id_chars"]]
    if not ok: field_errors["name"] = errs
    elif not val:
        field_errors["name"] = [MSG["required"]]
    sanitized["name"] = val

    org_id = _body_val(body, "org_id", "")
    if org_id and str(org_id).strip():
        ok, errs, val = validate_identifier(str(org_id))
        if _sanitization_enabled() and _AUTH_NAME_EXTRA_DANGEROUS.search(str(org_id)):
            ok = False
            errs = errs + [MSG["id_chars"]]
        if not ok: field_errors["org_id"] = errs
        sanitized["org_id"] = val
    else:
        sanitized["org_id"] = ""

    password = str(_body_val(body, "password", "") or "")
    _pw_errors = password_strength_errors(password)
    if _pw_errors:
        field_errors["password"] = _pw_errors
    sanitized["password"] = password  # never sanitized/altered — hashed as-is

    return len(field_errors) == 0, field_errors, sanitized


def validate_login_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """Validate/sanitize a LoginRequest (routers/auth_router.py::login).

    Deliberately lightweight — the login endpoint must not leak information
    about which field was "wrong" beyond generic format issues, and the
    password itself is verified against the stored hash, not against a
    character-set policy (a legitimate existing password may contain
    characters a NEW-password policy would reject).
    """
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_email_field(_body_val(body, "email", ""))
    if not ok: field_errors["email"] = errs
    sanitized["email"] = val

    password = str(_body_val(body, "password", "") or "")
    if not password:
        field_errors["password"] = [MSG["required"]]
    sanitized["password"] = password  # never sanitized/altered — verified as-is

    return len(field_errors) == 0, field_errors, sanitized


def validate_profile_update_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name = _body_val(body, "name")
    if name is not None and str(name).strip():
        ok, errs, val = validate_identifier(str(name))
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val
    else:
        sanitized["name"] = None

    gitlab = _body_val(body, "gitlab_username")
    if gitlab is not None and str(gitlab).strip():
        g = str(gitlab).strip()
        if not re.match(r"^[a-zA-Z0-9._-]+$", g):
            field_errors["gitlab_username"] = ["Only letters, numbers, dots, hyphens, and underscores allowed"]
        sanitized["gitlab_username"] = sanitize_input(g)
    else:
        sanitized["gitlab_username"] = None

    return len(field_errors) == 0, field_errors, sanitized


def validate_token_upsert_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    value = str(_body_val(body, "value", "")).strip()
    if not value:
        field_errors["value"] = ["Token value cannot be empty"]
    else:
        # XSS-only — tokens legitimately contain special chars
        xss_errors = _check_xss(value)
        xss_only = [e for e in xss_errors if "script" in e.lower() or "iframe" in e.lower() or "HTML" in e or "tag" in e.lower()]
        if xss_only:
            field_errors["value"] = xss_only
    sanitized["value"] = value  # raw — decrypt happens after

    label = _body_val(body, "label")
    if label and str(label).strip():
        ok, errs, val = validate_identifier(str(label))
        if not ok: field_errors["label"] = errs
        sanitized["label"] = val
    else:
        sanitized["label"] = None

    return len(field_errors) == 0, field_errors, sanitized


# ── Thread validators ───────────────────────────────────────────

_VALID_PRIORITIES = {"Low", "Medium", "High", "Critical"}
_VALID_MSG_TYPES  = {"text", "ainxt_analysis", "system"}
_VALID_INTENTS    = {"chat", "pipeline"}


def validate_thread_title(title: str) -> Tuple[bool, List[str], str]:
    if not title or not str(title).strip():
        return False, [MSG["required"]], ""
    return validate_identifier(title)


def validate_thread_description(description: Optional[str]) -> Tuple[bool, List[str], str]:
    return validate_free_text(description or "")


def validate_thread_priority(priority: str) -> Tuple[bool, List[str], str]:
    if priority not in _VALID_PRIORITIES:
        return False, [f"Priority must be one of: {', '.join(sorted(_VALID_PRIORITIES))}"], priority
    return True, [], priority


def validate_thread_labels(labels: List[str]) -> Tuple[bool, List[str], List[str]]:
    errors = []
    sanitized = []
    for label in (labels or []):
        ok, errs, val = validate_identifier(str(label))
        if not ok: errors.extend(errs)
        sanitized.append(val)
    return len(errors) == 0, errors, sanitized


def validate_message_content(content: str) -> Tuple[bool, List[str], str]:
    if not content or not str(content).strip():
        return False, [MSG["required"]], ""
    return validate_free_text(content)


def validate_author_name(author_name: Optional[str]) -> Tuple[bool, List[str], Optional[str]]:
    if not author_name or not str(author_name).strip():
        return True, [], author_name
    return validate_free_text(str(author_name))


def _validate_id_field(value: str, field_name: str) -> Tuple[bool, List[str], str]:
    if not value or not str(value).strip():
        return True, [], ""
    val = str(value).strip()
    ok, errs = validate_xss(val)
    return ok, errs, sanitize_input(val)


def validate_create_thread_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_thread_title(_body_val(body, "title", ""))
    if not ok: field_errors["title"] = errs
    sanitized["title"] = val

    ok, errs, val = validate_thread_description(_body_val(body, "description", ""))
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    ok, errs, val = validate_thread_priority(_body_val(body, "priority", "Medium"))
    if not ok: field_errors["priority"] = errs
    sanitized["priority"] = val

    ok, errs, val = validate_thread_labels(_body_val(body, "labels", []))
    if not ok: field_errors["labels"] = errs
    sanitized["labels"] = val

    # product_id — mandatory
    product_id_raw = str(_body_val(body, "product_id", "") or "").strip()
    if not product_id_raw:
        field_errors["product_id"] = [MSG["required"]]
        sanitized["product_id"] = None
    else:
        ok, errs, val = _validate_id_field(product_id_raw, "product_id")
        if not ok: field_errors["product_id"] = errs
        sanitized["product_id"] = val or None

    # repo, project_id, created_by — optional, XSS check only
    for fld in ["repo", "project_id", "created_by"]:
        raw = str(_body_val(body, fld, "") or "").strip()
        ok, errs, val = _validate_id_field(raw, fld)
        if not ok: field_errors[fld] = errs
        sanitized[fld] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_thread_message_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_message_content(_body_val(body, "content", ""))
    if not ok: field_errors["content"] = errs
    sanitized["content"] = val

    ok, errs, val = validate_author_name(_body_val(body, "author_name"))
    if not ok: field_errors["author_name"] = errs
    sanitized["author_name"] = val

    msg_type = _body_val(body, "message_type", "text")
    if msg_type not in _VALID_MSG_TYPES:
        field_errors["message_type"] = [f"Must be one of: {', '.join(sorted(_VALID_MSG_TYPES))}"]
    sanitized["message_type"] = msg_type

    intent = _body_val(body, "ainxt_intent", "chat")
    if intent not in _VALID_INTENTS:
        field_errors["ainxt_intent"] = [f"Must be one of: {', '.join(sorted(_VALID_INTENTS))}"]
    sanitized["ainxt_intent"] = intent

    ok, errs, val = _validate_id_field(str(_body_val(body, "parent_message_id", "") or ""), "parent_message_id")
    if not ok: field_errors["parent_message_id"] = errs
    sanitized["parent_message_id"] = val or None

    return len(field_errors) == 0, field_errors, sanitized


# ── HITL validators ─────────────────────────────────────────────

_VALID_HITL_ACTIONS = {"approved", "modified", "rejected"}


def validate_hitl_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    action = _body_val(body, "action", "")
    if action not in _VALID_HITL_ACTIONS:
        field_errors["action"] = [f"Must be one of: {', '.join(sorted(_VALID_HITL_ACTIONS))}"]
    sanitized["action"] = action

    note = str(_body_val(body, "note", "") or "").strip()
    if note:
        ok, errs, val = validate_free_text(note)
        if not ok: field_errors["note"] = errs
        sanitized["note"] = val
    else:
        sanitized["note"] = ""

    return len(field_errors) == 0, field_errors, sanitized


def validate_reaction_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    emoji = str(_body_val(body, "emoji", "")).strip()
    if not emoji:
        field_errors["emoji"] = [MSG["required"]]
    sanitized["emoji"] = emoji

    user_id = str(_body_val(body, "user_id", "")).strip()
    if not user_id:
        field_errors["user_id"] = [MSG["required"]]
    else:
        ok, errs = validate_xss(user_id)
        if not ok: field_errors["user_id"] = errs
    sanitized["user_id"] = user_id

    return len(field_errors) == 0, field_errors, sanitized


# ── SDLC validators ─────────────────────────────────────────────

_VALID_SDLC_PRIORITIES = {"Low", "Medium", "High", "Critical"}
_VALID_LANGUAGE_OVERRIDES = {"python", "java", "go", "javascript", "typescript", "rust", "c", "cpp", "csharp", "ruby", "php", "kotlin", "swift", "scala"}


def _validate_jira_key(value: str) -> Tuple[bool, List[str], str]:
    if not value or not str(value).strip():
        return False, [MSG["required"]], ""
    val = str(value).strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9_]+-\d+$", val):
        return False, ["Jira key must be in format PROJECT-123"], val
    return True, [], val


def validate_sdlc_trigger_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /sdlc/feature, /sdlc/bug — FeatureRequest/BugRequest. `summary` is
    mandatory (matches the router's own existing rule); `description` is
    optional. Both are rendered into inbox notifications and Jira/branch-name
    text, so XSS-only (validate_free_text) — no character allow-list, since
    real summaries legitimately use punctuation."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    summary_raw = str(_body_val(body, "summary", "") or "").strip()
    if not summary_raw:
        field_errors["summary"] = [MSG["required"]]
        sanitized["summary"] = ""
    else:
        ok, errs, val = validate_free_text(summary_raw)
        if not ok: field_errors["summary"] = errs
        sanitized["summary"] = val

    ok, errs, val = validate_free_text(_body_val(body, "description", ""))
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_sdlc_pipeline_request(body: Any, pipeline_type: str = "feature") -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    # jira_key — mandatory, format-validated
    ok, errs, val = _validate_jira_key(_body_val(body, "jira_key", ""))
    if not ok: field_errors["jira_key"] = errs
    sanitized["jira_key"] = val

    # summary — mandatory
    summary_raw = str(_body_val(body, "summary", "") or "").strip()
    if not summary_raw:
        field_errors["summary"] = [MSG["required"]]
        sanitized["summary"] = ""
    else:
        ok, errs, val = validate_free_text(summary_raw)
        if not ok: field_errors["summary"] = errs
        sanitized["summary"] = val

    ok, errs, val = validate_free_text(_body_val(body, "description", ""))
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    ok, errs, val = validate_free_text(_body_val(body, "repo", ""))
    if not ok: field_errors["repo"] = errs
    sanitized["repo"] = val

    priority = str(_body_val(body, "priority", "Medium") or "Medium")
    sanitized["priority"] = priority if priority in _VALID_SDLC_PRIORITIES else "Medium"

    sanitized["assignee"] = sanitize_input(str(_body_val(body, "assignee", "") or ""))

    lang = str(_body_val(body, "language_override", "") or "").strip().lower()
    sanitized["language_override"] = lang if lang in _VALID_LANGUAGE_OVERRIDES else ""

    sanitized["product_id"] = sanitize_input(str(_body_val(body, "product_id", "") or "")) or None
    sanitized["branch"] = sanitize_input(str(_body_val(body, "branch", "") or ""))

    return len(field_errors) == 0, field_errors, sanitized


def validate_pr_review_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    pr_number = _body_val(body, "pr_number")
    sanitized["pr_number"] = pr_number

    ok, errs, val = validate_free_text(_body_val(body, "title", ""))
    if not ok: field_errors["title"] = errs
    sanitized["title"] = val

    ok, errs, val = validate_free_text(_body_val(body, "body", ""))
    if not ok: field_errors["body"] = errs
    sanitized["body"] = val

    ok, errs, val = validate_free_text(_body_val(body, "repo", ""))
    if not ok: field_errors["repo"] = errs
    sanitized["repo"] = val

    sanitized["branch"] = sanitize_input(str(_body_val(body, "branch", "") or ""))
    sanitized["base"] = sanitize_input(str(_body_val(body, "base", "main") or "main"))
    sanitized["author"] = sanitize_input(str(_body_val(body, "author", "") or ""))

    for url_field in ["url", "diff_url"]:
        raw = str(_body_val(body, url_field, "") or "").strip()
        if raw:
            ok, errs, val = validate_url_field(raw, url_field)
            if not ok: field_errors[url_field] = errs
            sanitized[url_field] = val
        else:
            sanitized[url_field] = ""

    return len(field_errors) == 0, field_errors, sanitized


def validate_sdlc_approval_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    feedback = str(_body_val(body, "feedback", "") or "").strip()
    if feedback:
        ok, errs, val = validate_free_text(feedback)
        if not ok: field_errors["feedback"] = errs
        sanitized["feedback"] = val
    else:
        sanitized["feedback"] = ""

    sanitized["approved_by"] = sanitize_input(str(_body_val(body, "approved_by", "user") or "user"))

    return len(field_errors) == 0, field_errors, sanitized


def validate_sdlc_reject_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    reason = str(_body_val(body, "reason", "")).strip()
    if not reason:
        field_errors["reason"] = [MSG["required"]]
    else:
        ok, errs, val = validate_free_text(reason)
        if not ok: field_errors["reason"] = errs
        sanitized["reason"] = val
    if "reason" not in sanitized:
        sanitized["reason"] = ""

    sanitized["rejected_by"] = sanitize_input(str(_body_val(body, "rejected_by", "user") or "user"))

    return len(field_errors) == 0, field_errors, sanitized


def validate_sdlc_cancel_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    reason = str(_body_val(body, "reason", "Cancelled by user")).strip()
    ok, errs, val = validate_free_text(reason)
    if not ok: field_errors["reason"] = errs
    sanitized["reason"] = val or "Cancelled by user"

    sanitized["cancelled_by"] = sanitize_input(str(_body_val(body, "cancelled_by", "user") or "user"))

    return len(field_errors) == 0, field_errors, sanitized


def validate_chat_title(title: str) -> Tuple[bool, List[str], str]:
    """PATCH /chats/{id}/title, POST /chats (create) — chat title, rendered
    verbatim in the sidebar/tab. XSS-only (validate_free_text); the router's
    own [:500]/[:400] length caps stay unchanged, applied by the caller."""
    return validate_free_text(title or "")


def validate_chat_scope_fields(domain: Optional[str], spec_version: Optional[str]) -> Tuple[bool, Dict[str, List[str]], Dict[str, Optional[str]]]:
    """PATCH /chats/{id}/scope, POST /chats — `domain`/`spec_version` are
    short KB-scope tags (e.g. "Tech", "v3"), rendered back into the UI and
    used in SQL WHERE clauses (parameterized) — identifier allow-list."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Optional[str]] = {}

    if domain is not None:
        ok, errs, val = validate_identifier(domain)
        if not ok: field_errors["domain"] = errs
        sanitized["domain"] = val or None
    else:
        sanitized["domain"] = None

    if spec_version is not None:
        ok, errs, val = validate_identifier(spec_version)
        if not ok: field_errors["spec_version"] = errs
        sanitized["spec_version"] = val or None
    else:
        sanitized["spec_version"] = None

    return len(field_errors) == 0, field_errors, sanitized


def validate_chat_artifact_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /chats/{id}/artifacts — `title` is an identifier-ish label,
    `content` is the free-text artifact body (HTML/code/markdown/etc. —
    intentionally NOT run through the blanket HTML_TAG deny-list, since
    'html'-type artifacts are supposed to contain HTML; only the genuinely
    dangerous <script>/<iframe>/event-handler/javascript: subset is checked,
    same rationale as the broadcast html_body validator above)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    title = str(_body_val(body, "title", "Untitled") or "Untitled")
    ok, errs, val = validate_free_text(title)
    if not ok: field_errors["title"] = errs
    sanitized["title"] = val or "Untitled"

    content = str(_body_val(body, "content", "") or "")
    html_errs = _check_html_body_xss(content)
    if html_errs: field_errors["content"] = html_errs
    sanitized["content"] = content

    return len(field_errors) == 0, field_errors, sanitized


def validate_prompt_template_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /prompt-templates — `name` is an identifier, `body` (the prompt
    text itself) is free text."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name = str(_body_val(body, "name", "") or "").strip()
    if not name:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    body_text = str(_body_val(body, "body", "") or "").strip()
    if not body_text:
        field_errors["body"] = [MSG["required"]]
        sanitized["body"] = ""
    else:
        ok, errs, val = validate_free_text(body_text)
        if not ok: field_errors["body"] = errs
        sanitized["body"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_endpoint_mgmt_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST/PUT /endpoints (EndpointCreate/EndpointUpdate) — `name` is an
    identifier (rendered in admin lists), `description` is free text.
    Both are optional on update (None means "leave unchanged" per the
    router's own contract), so a None is passed through untouched."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name = _body_val(body, "name", None)
    if name is not None:
        ok, errs, val = validate_identifier(str(name))
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val
    else:
        sanitized["name"] = None

    description = _body_val(body, "description", None)
    if description is not None:
        ok, errs, val = validate_free_text(str(description))
        if not ok: field_errors["description"] = errs
        sanitized["description"] = val
    else:
        sanitized["description"] = None

    return len(field_errors) == 0, field_errors, sanitized


def validate_model_permission_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /model-governance and /model-governance/user (ModelPermissionBody /
    UserPermissionBody) — `department` and `model_id` (and `user_id`, when
    present) are identifiers: they're used in SQL lookups and rendered back
    in admin UI lists, so they go through the identifier allow-list rather
    than free text."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_identifier(_body_val(body, "department"))
    if not ok: field_errors["department"] = errs
    sanitized["department"] = val

    ok, errs, val = validate_identifier(_body_val(body, "model_id"))
    if not ok: field_errors["model_id"] = errs
    sanitized["model_id"] = val

    user_id = _body_val(body, "user_id", None)
    if user_id is not None:
        ok, errs, val = validate_identifier(str(user_id))
        if not ok: field_errors["user_id"] = errs
        sanitized["user_id"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_discussion_title_and_tags(title: str, tags: List[str]) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST/PUT /discussions/questions — `title` is free text (XSS-only,
    rendered in the discussion list); `tags` are short identifier-ish labels
    (matched against the `tags` allow-list engine-side)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    ok, errs, val = validate_free_text(title or "")
    if not ok: field_errors["title"] = errs
    sanitized["title"] = val

    san_tags = []
    for tag in (tags or []):
        ok, errs, val = validate_identifier(str(tag))
        if not ok: field_errors.setdefault("tags", []).extend(errs)
        else: san_tags.append(val)
    sanitized["tags"] = san_tags

    return len(field_errors) == 0, field_errors, sanitized


def validate_docs_upload_scope(domain: str, spec_version: str) -> Tuple[bool, Dict[str, List[str]], Dict[str, Optional[str]]]:
    """POST /docs/upload — `domain`/`spec_version` KB-scope tags (Form fields,
    arrive as plain strings; "" means unset). Same identifier allow-list as
    the chat router's per-chat scope fields."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Optional[str]] = {}

    domain = (domain or "").strip()
    if domain:
        ok, errs, val = validate_identifier(domain)
        if not ok: field_errors["domain"] = errs
        sanitized["domain"] = val
    else:
        sanitized["domain"] = ""

    spec_version = (spec_version or "").strip()
    if spec_version:
        ok, errs, val = validate_identifier(spec_version)
        if not ok: field_errors["spec_version"] = errs
        sanitized["spec_version"] = val
    else:
        sanitized["spec_version"] = ""

    return len(field_errors) == 0, field_errors, sanitized


def validate_coach_note_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /coach/admin/coach-user, /coach/admin/preview-message — admin-typed
    `subject`/`body` (or `custom_note`) that override the auto-generated
    coaching message. XSS-only free text; None (not overridden) passes
    through unchanged so the caller's own fallback-to-generated-message
    logic is untouched."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    subject = _body_val(body, "subject", None)
    if subject is not None:
        ok, errs, val = validate_free_text(str(subject))
        if not ok: field_errors["subject"] = errs
        sanitized["subject"] = val
    else:
        sanitized["subject"] = None

    body_text = _body_val(body, "body", None)
    if body_text is not None:
        ok, errs, val = validate_free_text(str(body_text))
        if not ok: field_errors["body"] = errs
        sanitized["body"] = val
    else:
        sanitized["body"] = None

    custom_note = _body_val(body, "custom_note", None)
    if custom_note is not None:
        ok, errs, val = validate_free_text(str(custom_note))
        if not ok: field_errors["custom_note"] = errs
        sanitized["custom_note"] = val
    else:
        sanitized["custom_note"] = None

    return len(field_errors) == 0, field_errors, sanitized


def validate_governance_trigger_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /sdlc/governance — repo/base_branch/head_branch feed into git
    clone/checkout commands, so they go through validate_identifier() rather
    than validate_free_text(). `/` is allowed (repo namespaces, e.g.
    'group/project') — only the identifier deny-list (< > { } [ ] ` | \\) applies."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    repo = str(_body_val(body, "repo", "") or "").strip()
    if not repo:
        field_errors["repo"] = [MSG["required"]]
        sanitized["repo"] = ""
    else:
        ok, errs, val = validate_identifier(repo)
        if not ok: field_errors["repo"] = errs
        sanitized["repo"] = val

    head_branch = str(_body_val(body, "head_branch", "") or "").strip()
    if not head_branch:
        field_errors["head_branch"] = [MSG["required"]]
        sanitized["head_branch"] = ""
    else:
        ok, errs, val = validate_identifier(head_branch)
        if not ok: field_errors["head_branch"] = errs
        sanitized["head_branch"] = val

    base_branch = str(_body_val(body, "base_branch", "main") or "main").strip()
    ok, errs, val = validate_identifier(base_branch)
    if not ok: field_errors["base_branch"] = errs
    sanitized["base_branch"] = val or "main"

    return len(field_errors) == 0, field_errors, sanitized


def validate_governance_suppression_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /sdlc/governance-suppressions (and bulk variant) — reason/rule/skill
    are free text/identifier-ish fields rendered back in the governance panel."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    for fld in ("skill", "rule"):
        raw = str(_body_val(body, fld, "") or "").strip()
        if raw:
            ok, errs, val = validate_identifier(raw)
            if not ok: field_errors[fld] = errs
            sanitized[fld] = val
        else:
            sanitized[fld] = raw

    reason = _body_val(body, "reason", None)
    if reason:
        ok, errs, val = validate_free_text(str(reason))
        if not ok: field_errors["reason"] = errs
        sanitized["reason"] = val
    else:
        sanitized["reason"] = reason

    return len(field_errors) == 0, field_errors, sanitized


def validate_governance_decision_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /sdlc/runs/{run_id}/governance/domains/{domain}/findings/{fp}/decision
    and the domain send-back endpoint — `comment` is mandatory free text when
    decision == send_back; `fp_justification` (mark-FP flow) is optional free
    text."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    comment = str(_body_val(body, "comment", "") or "")
    if comment.strip():
        ok, errs, val = validate_free_text(comment)
        if not ok: field_errors["comment"] = errs
        sanitized["comment"] = val
    else:
        sanitized["comment"] = comment

    fp_just = _body_val(body, "fp_justification", None)
    if fp_just:
        ok, errs, val = validate_free_text(str(fp_just))
        if not ok: field_errors["fp_justification"] = errs
        sanitized["fp_justification"] = val
    else:
        sanitized["fp_justification"] = fp_just

    reason = _body_val(body, "reason", None)
    if reason:
        ok, errs, val = validate_free_text(str(reason))
        if not ok: field_errors["reason"] = errs
        sanitized["reason"] = val
    else:
        sanitized["reason"] = reason

    return len(field_errors) == 0, field_errors, sanitized


def validate_sdlc_revision_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    feedback = str(_body_val(body, "feedback", "")).strip()
    if not feedback:
        field_errors["feedback"] = [MSG["required"]]
    else:
        ok, errs, val = validate_free_text(feedback)
        if not ok: field_errors["feedback"] = errs
        sanitized["feedback"] = val
    if "feedback" not in sanitized:
        sanitized["feedback"] = ""

    sanitized["revised_by"] = sanitize_input(str(_body_val(body, "revised_by", "user") or "user"))

    return len(field_errors) == 0, field_errors, sanitized


# ── CodeWiki validators ──────────────────────────────────────────

def validate_codewiki_generate_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /codewiki/generate — codebase_name/branch feed into filesystem
    Path(...) construction downstream, so they go through validate_identifier()
    (allow-list, blocks < > { } [ ] ` | \\) rather than validate_free_text()."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    codebase_name = str(_body_val(body, "codebase_name", "") or "").strip()
    if not codebase_name:
        field_errors["codebase_name"] = [MSG["required"]]
        sanitized["codebase_name"] = ""
    else:
        ok, errs, val = validate_identifier(codebase_name)
        if not ok: field_errors["codebase_name"] = errs
        sanitized["codebase_name"] = val

    repo_url = str(_body_val(body, "repo_url", "") or "").strip()
    if not repo_url:
        field_errors["repo_url"] = [MSG["required"]]
        sanitized["repo_url"] = ""
    else:
        ok, errs, val = validate_url_field(repo_url, "repo_url")
        if not ok: field_errors["repo_url"] = errs
        sanitized["repo_url"] = val

    branch = str(_body_val(body, "branch", "main") or "main").strip()
    ok, errs, val = validate_identifier(branch)
    if not ok: field_errors["branch"] = errs
    sanitized["branch"] = val or "main"

    return len(field_errors) == 0, field_errors, sanitized


def validate_codewiki_regenerate_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /codewiki/regenerate and /codewiki/retry — codebase_name only."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    codebase_name = str(_body_val(body, "codebase_name", "") or "").strip()
    if not codebase_name:
        field_errors["codebase_name"] = [MSG["required"]]
        sanitized["codebase_name"] = ""
    else:
        ok, errs, val = validate_identifier(codebase_name)
        if not ok: field_errors["codebase_name"] = errs
        sanitized["codebase_name"] = val

    return len(field_errors) == 0, field_errors, sanitized


# ── Document generation/download validators ─────────────────────

def validate_themed_doc_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /docs/generate-themed — `filename` is attacker-controlled and used
    to name the generated file on disk, so it goes through validate_identifier()
    (blocks < > { } [ ] ` | \\, which also covers path separators like ../)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    filename = str(_body_val(body, "filename", "") or "").strip()
    if filename:
        ok, errs, val = validate_identifier(filename)
        if not ok: field_errors["filename"] = errs
        sanitized["filename"] = val
    else:
        sanitized["filename"] = ""

    title = str(_body_val(body, "title", "") or "").strip()
    ok, errs, val = validate_free_text(title)
    if not ok: field_errors["title"] = errs
    sanitized["title"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_doc_generate_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /docs/generate — free-text fields rendered back into the chat UI
    and/or the generated document; XSS check only (validate_free_text)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    for fld in ["title", "content_md", "question", "source_doc_name", "prev_doc_name"]:
        raw = str(_body_val(body, fld, "") or "")
        if raw.strip():
            ok, errs, val = validate_free_text(raw)
            if not ok: field_errors[fld] = errs
            sanitized[fld] = val
        else:
            sanitized[fld] = raw

    return len(field_errors) == 0, field_errors, sanitized


# ── Cowork/Buddy admin validators ────────────────────────────────

def validate_cowork_note_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /buddy/memory/note — persisted + later rendered back into the
    agent's system-prompt snippet."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    note = str(_body_val(body, "note", "") or "").strip()
    if not note:
        field_errors["note"] = [MSG["required"]]
        sanitized["note"] = ""
    else:
        ok, errs, val = validate_free_text(note)
        if not ok: field_errors["note"] = errs
        sanitized["note"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_cowork_prefs_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """PUT /buddy/prefs — only the known string-ish keys the router already
    persists (email_signature, tone, team_aliases, channel_aliases) are
    sanitized here; the router's own allow-list still governs which keys are
    actually stored."""
    field_errors: Dict[str, List[str]] = {}
    prefs_in = _body_val(body, "prefs", {}) or {}
    sanitized_prefs: Dict[str, Any] = dict(prefs_in)

    for k in ("email_signature", "tone"):
        v = prefs_in.get(k)
        if isinstance(v, str) and v.strip():
            ok, errs, val = validate_free_text(v)
            if not ok: field_errors[k] = errs
            sanitized_prefs[k] = val

    for k in ("team_aliases", "channel_aliases"):
        v = prefs_in.get(k)
        if isinstance(v, dict):
            san = {}
            for alias, target in v.items():
                ok, errs, val = validate_identifier(str(alias))
                if not ok: field_errors.setdefault(k, []).extend(errs)
                ok2, errs2, val2 = validate_free_text(str(target))
                if not ok2: field_errors.setdefault(k, []).extend(errs2)
                san[val or str(alias)] = val2
            sanitized_prefs[k] = san

    return len(field_errors) == 0, field_errors, {"prefs": sanitized_prefs}


def validate_cowork_role_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST/PUT /buddy/roles — `name` is an identifier; `system_prompt`/
    `description` are free text (rendered/consumed by the LLM, not by the
    filesystem); the three allow-list fields are validated per-item since
    they're later matched against real connector/skill/subagent names."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    name = str(_body_val(body, "name", "") or "").strip()
    if not name:
        field_errors["name"] = [MSG["required"]]
        sanitized["name"] = ""
    else:
        ok, errs, val = validate_identifier(name)
        if not ok: field_errors["name"] = errs
        sanitized["name"] = val

    system_prompt = str(_body_val(body, "system_prompt", "") or "").strip()
    if not system_prompt:
        field_errors["system_prompt"] = [MSG["required"]]
        sanitized["system_prompt"] = ""
    else:
        ok, errs, val = validate_free_text(system_prompt)
        if not ok: field_errors["system_prompt"] = errs
        sanitized["system_prompt"] = val

    description = str(_body_val(body, "description", "") or "")
    ok, errs, val = validate_free_text(description)
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    for fld in ("allowed_connectors", "skill_names", "subagent_allowlist"):
        items = _body_val(body, fld, []) or []
        san_items = []
        for item in items:
            ok, errs, val = validate_identifier(str(item))
            if not ok: field_errors.setdefault(fld, []).extend(errs)
            else: san_items.append(val)
        sanitized[fld] = san_items

    return len(field_errors) == 0, field_errors, sanitized


# ── Connector validators ────────────────────────────────────────

_CONNECTOR_FREE_TEXT_PARAMS = ("body", "subject", "message", "content", "text")


def validate_connector_action_params(params: Dict[str, Any]) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /connectors/action, /connectors/execute — the free-text params
    that get sent to an external system (Outlook/Teams/Slack/etc.) via
    `params["body"/"subject"/"message"/"content"/"text"]`. XSS-only, since
    these are legitimate human-written messages with full punctuation.
    Non-string / absent params pass through untouched."""
    field_errors: Dict[str, List[str]] = {}
    sanitized = dict(params or {})
    for key in _CONNECTOR_FREE_TEXT_PARAMS:
        val = sanitized.get(key)
        if isinstance(val, str) and val.strip():
            ok, errs, clean = validate_free_text(val)
            if not ok: field_errors[key] = errs
            sanitized[key] = clean
    return len(field_errors) == 0, field_errors, sanitized


def validate_connector_definition_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST/PUT /connectors/definitions — admin-only connector definition CRUD."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    display_name = str(_body_val(body, "display_name", "") or "").strip()
    if not display_name:
        field_errors["display_name"] = [MSG["required"]]
        sanitized["display_name"] = ""
    else:
        ok, errs, val = validate_free_text(display_name)
        if not ok: field_errors["display_name"] = errs
        sanitized["display_name"] = val

    description = str(_body_val(body, "description", "") or "")
    ok, errs, val = validate_free_text(description)
    if not ok: field_errors["description"] = errs
    sanitized["description"] = val

    icon_url = str(_body_val(body, "icon_url", "") or "").strip()
    if icon_url:
        ok, errs, val = validate_url_field(icon_url, "icon_url")
        if not ok: field_errors["icon_url"] = errs
        sanitized["icon_url"] = val
    else:
        sanitized["icon_url"] = ""

    base_url = str(_body_val(body, "base_url", "") or "").strip()
    if base_url:
        ok, errs, val = validate_url_field(base_url, "base_url")
        if not ok: field_errors["base_url"] = errs
        sanitized["base_url"] = val
    else:
        sanitized["base_url"] = ""

    return len(field_errors) == 0, field_errors, sanitized


# ── Broadcast email validators ──────────────────────────────────

# Broadcast subject lines feed straight into the email's `Subject:` header — a
# raw CR or LF would let a sender inject extra headers (e.g. Bcc:) into the
# outgoing message. _check_xss()'s CONTROL_CHARS pattern deliberately skips
# \r/\n (0x0D/0x0A) since normal free-text fields want to keep real newlines,
# so this is enforced separately, specific to this one header-bound field.
_CRLF_RE = re.compile(r"[\r\n]")

# html_body is a legitimate rich-text/WYSIWYG email body — it's SUPPOSED to
# contain ordinary HTML (<div>, <table>, <img>, <a>, <b>, ...), so running it
# through validate_free_text()'s blanket HTML_TAG check would reject every
# real broadcast. Only the genuinely dangerous constructs are blocked here:
# <script>/<iframe>/<object>/<embed>, inline event handlers, and javascript:
# URLs — the same subset _check_xss() would flag minus the "any HTML tag" rule.
def _check_html_body_xss(html: str) -> List[str]:
    if not _sanitization_enabled() or not isinstance(html, str) or not html.strip():
        return []
    errors = []
    if XSS_PATTERNS["SCRIPT_TAG"].search(html):  errors.append(MSG["xss_script"])
    if XSS_PATTERNS["IFRAME_TAG"].search(html):  errors.append(MSG["xss_iframe"])
    if XSS_PATTERNS["OBJECT_TAG"].search(html):  errors.append(MSG["xss_html"])
    if XSS_PATTERNS["EMBED_TAG"].search(html):   errors.append(MSG["xss_html"])
    if XSS_PATTERNS["ON_EVENT"].search(html):    errors.append(MSG["xss_event"])
    if XSS_PATTERNS["JAVASCRIPT"].search(html):  errors.append(MSG["xss_scheme"])
    if XSS_PATTERNS["NULL_BYTES"].search(html):  errors.append(MSG["xss_control"])
    return list(dict.fromkeys(errors))


def validate_broadcast_send_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /broadcast/send — subject/html_body/text_body."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    subject = str(_body_val(body, "subject", "") or "")
    if _sanitization_enabled() and _CRLF_RE.search(subject):
        field_errors["subject"] = ["Line breaks are not allowed in the subject line"]
    else:
        ok, errs = validate_xss(subject)
        if not ok: field_errors["subject"] = errs
    sanitized["subject"] = sanitize_input(subject)

    html_body = str(_body_val(body, "html_body", "") or "")
    html_errs = _check_html_body_xss(html_body)
    if html_errs: field_errors["html_body"] = html_errs
    sanitized["html_body"] = html_body  # HTML content preserved verbatim; only scanned above

    text_body = _body_val(body, "text_body", None)
    if text_body:
        ok, errs, val = validate_free_text(str(text_body))
        if not ok: field_errors["text_body"] = errs
        sanitized["text_body"] = val
    else:
        sanitized["text_body"] = text_body

    return len(field_errors) == 0, field_errors, sanitized


def validate_presenton_outline_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /ppt/outline (OutlineRequest) — `prompt` is mandatory free text.
    Note: the value is ALSO passed through core.prompt_sanitizer.sanitize()
    before hitting the LLM (control-char/encoding safety for the model API
    call) — this validator is the separate XSS/security gate for the value
    as stored/echoed in the app (independent concern, same kill-switch)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    prompt_raw = str(_body_val(body, "prompt", "") or "").strip()
    if not prompt_raw:
        field_errors["prompt"] = [MSG["required"]]
        sanitized["prompt"] = ""
    else:
        ok, errs, val = validate_free_text(prompt_raw)
        if not ok: field_errors["prompt"] = errs
        sanitized["prompt"] = val

    return len(field_errors) == 0, field_errors, sanitized


def validate_presenton_generate_request(body: Any) -> Tuple[bool, Dict[str, List[str]], Dict[str, Any]]:
    """POST /ppt/generate (GenerateRequest) — `prompt` mandatory free text;
    `template` is an identifier (selects a template dir/catalogue entry
    server-side, so allow-listed rather than free text)."""
    field_errors: Dict[str, List[str]] = {}
    sanitized: Dict[str, Any] = {}

    prompt_raw = str(_body_val(body, "prompt", "") or "").strip()
    if not prompt_raw:
        field_errors["prompt"] = [MSG["required"]]
        sanitized["prompt"] = ""
    else:
        ok, errs, val = validate_free_text(prompt_raw)
        if not ok: field_errors["prompt"] = errs
        sanitized["prompt"] = val

    ok, errs, val = validate_identifier(str(_body_val(body, "template", "") or ""))
    if not ok: field_errors["template"] = errs
    sanitized["template"] = val or "general"

    return len(field_errors) == 0, field_errors, sanitized


# ── Keep old PATTERNS dict for any code that references it directly ──
PATTERNS = XSS_PATTERNS
ERROR_MESSAGES = {"general": MSG, "xss": MSG, "sql": {}}
