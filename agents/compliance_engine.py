# SPDX-License-Identifier: Apache-2.0
# ============================================================
# AiNxt ENTERPRISE PCI / PII COMPLIANCE ENGINE (PRODUCTION)
# ============================================================
# Strategy: config-driven, per-type toggle.
#   Each type can be: enabled+redact | enabled+block | off
#   Default (config/compliance_config.json): ALL types → redact, none block.
#   Output is NEVER blocked regardless of config — output always proceeds.
# ============================================================

import json
import os
import re
import time
import hashlib
import threading
from collections import OrderedDict
from typing import List, Dict, Set, Optional

import httpx

# Audit log — append-only JSONL written when COMPLIANCE_AUDIT_LOG is set.
# Each line: {ts, input, findings, redacted, was_redacted}
_AUDIT_LOG_PATH = os.getenv("COMPLIANCE_AUDIT_LOG")
_audit_lock = threading.Lock()
_tl = threading.local()  # per-thread latency tracking (thread-safe, no locks needed)

from core.logger import logger, get_request_id, get_chat_id, get_user_id, get_span_id
from core.config import COMPLIANCE_SERVICE_ENABLED, PRIVACY_SERVICE_ENABLED

# Persistent HTTP client — reused across all ML calls (connection pooling, keep-alive)
# Read timeout raised from 2.0s → 15s (env-tunable) so the privacy ML service has
# time to finish inference on large prompts (e.g. a ~1 MB attachment now injected
# in full after the /ask 10K cap was lifted). The privacy_svc /filter model runs a
# single CPU forward pass over the whole text, so big inputs take several seconds;
# a 2s cap made them fail/return nothing. 15s stays well under the 240s gunicorn
# worker budget (this call is synchronous, before the LLM call). Tune with
# COMPLIANCE_ML_READ_TIMEOUT / COMPLIANCE_ML_WRITE_TIMEOUT if needed.
_ML_READ_TIMEOUT  = float(os.getenv("COMPLIANCE_ML_READ_TIMEOUT",  "15.0"))
_ML_WRITE_TIMEOUT = float(os.getenv("COMPLIANCE_ML_WRITE_TIMEOUT", "5.0"))
_http_client = httpx.Client(
    timeout=httpx.Timeout(connect=0.5, read=_ML_READ_TIMEOUT, write=_ML_WRITE_TIMEOUT, pool=0.5),
    limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
)

# ── ML result cache (content-hash → findings) ──────────────────────────────
# The privacy_svc /filter call is a blocking HTTP round-trip and is the single
# most expensive step in validate_input/analyze. The result for a given text is
# deterministic, so caching by sha256(text) is safe. This eliminates redundant
# re-scans of byte-identical content — e.g. the browser-agent passthrough lane
# re-sends the full (uncompressed) tool-result history every turn, so prior
# rounds' DOM snapshots would otherwise be re-scanned every round (O(N^2)).
# Bounded LRU (OrderedDict) so memory can't grow unbounded. Thread-safe.
_ML_CACHE_MAX = int(os.getenv("COMPLIANCE_ML_CACHE_SIZE", "2048"))
_ML_CACHE_ENABLED = os.getenv("COMPLIANCE_ML_CACHE", "true").lower() == "true"
_ml_cache: "OrderedDict[str, List[Dict]]" = OrderedDict()
_ml_cache_lock = threading.Lock()


def _ml_cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


def _ml_cache_get(key: str):
    """Return cached findings (a fresh copy) or None on miss. Refreshes LRU order."""
    with _ml_cache_lock:
        hit = _ml_cache.get(key)
        if hit is None:
            return None
        _ml_cache.move_to_end(key)
        # Return a shallow copy of the list of dicts so callers can't mutate the
        # cached entry (findings dicts are read-only downstream, but be safe).
        return [dict(f) for f in hit]


def _ml_cache_put(key: str, findings: List[Dict]) -> None:
    with _ml_cache_lock:
        _ml_cache[key] = [dict(f) for f in findings]
        _ml_cache.move_to_end(key)
        while len(_ml_cache) > _ML_CACHE_MAX:
            _ml_cache.popitem(last=False)

from agents.pii_detector   import detect_pii
from agents.secret_detector import detect_secrets
from agents.key_leak_detector import detect_key_leaks
from agents.redactor import redact_all

# ============================================================
# COMPLIANCE BLOCK SENTINEL
# ============================================================
# Canonical sentinel emitted by gateways when compliance blocks input. Centralized
# so programmatic (non-user-facing) callers — e.g. the SDLC file-read / per-file LLM
# paths — can DETECT a block instead of mistaking the block string for real content.
COMPLIANCE_BLOCK_SENTINEL = "Request blocked due to PCI violation"


def is_compliance_block(text) -> bool:
    """True if `text` is (or contains) the compliance-block sentinel. Used by
    programmatic callers to detect a block and drop the content rather than feed
    the sentinel forward as if it were file/model output."""
    return isinstance(text, str) and COMPLIANCE_BLOCK_SENTINEL in text

# ============================================================
# ML PRIVACY FILTER — label mapping
# ============================================================
# Gate decision: call ML for ALL natural language text (not just when regex
# patterns hit). Regex gate was too narrow — "Send money to Ramesh" has no
# pattern match but contains a person name. Only pure code blocks skip ML.

# Map privacy-filter entity_group labels → compliance type names
_ML_LABEL_MAP: Dict[str, str | None] = {
    "account_number":  "ACCOUNT_NUMBER",
    "private_email":   "EMAIL",
    "private_phone":   "MOBILE",
    "classified_entity": "SECRET",
    # Informational — logged but not blocked/redacted as a compliance type
    "private_person":  None,
    "private_address": None,
    "private_date":    None,
    "private_url":     None,
}


# ============================================================
# DEFAULT CONFIG — fallback when config file is absent
# ============================================================

_DEFAULT_TYPES = [
    "PAN", "CVV", "EXPIRY", "PIN_BLOCK",
    "SECRET", "API_KEY", "ACCESS_TOKEN",
    "PRIVATE_KEY_LEAK", "CERTIFICATE_LEAK", "SSH_KEY_LEAK",
    "KEY_ASSIGNMENT_LEAK", "PAYMENT_KEY_LEAK",
    "INDIA_PAN", "AADHAAR",
    "EMAIL", "MOBILE", "UPI",
    "ACCOUNT_NUMBER", "IFSC_CODE", "ACCOUNT_NAME_COMBO",
    "IP_ADDRESS",
]

_DEFAULT_CONFIG: Dict = {
    "types": {t: {"enabled": True, "action": "redact"} for t in _DEFAULT_TYPES}
}

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "compliance_config.json"
)


# ============================================================
# SEVERITY LEVELS
# ============================================================

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH     = "HIGH"
SEVERITY_MEDIUM   = "MEDIUM"
SEVERITY_LOW      = "LOW"


# ============================================================
# COMPLIANCE ENGINE
# ============================================================

class ComplianceEngine:

    # Toggle: set COMPLIANCE_SERVICE_ENABLED=true in .env to activate.
    # Enforced here so no call site needs changing.
    enabled = COMPLIANCE_SERVICE_ENABLED

    def __init__(self):
        self._config = self._load_config()
        if not self.enabled:
            logger.info("ComplianceEngine: disabled (COMPLIANCE_SERVICE_ENABLED=false)")
            return
        logger.info(
            f"ComplianceEngine initialized — "
            f"redact={len(self._redact_types())} block={len(self._block_types())} "
            f"off={len(self._off_types())}"
        )

    # ========================================================
    # CONFIG MANAGEMENT
    # ========================================================

    def _load_config(self) -> Dict:
        # COMPLIANCE_CONFIG env var takes priority over the file (set in .env)
        env_json = os.getenv("COMPLIANCE_CONFIG")
        if env_json:
            try:
                cfg = json.loads(env_json)
                logger.info("ComplianceEngine: loaded config from COMPLIANCE_CONFIG env var")
                return self._backfill_new_types(cfg)
            except Exception as e:
                logger.error(f"ComplianceEngine: invalid COMPLIANCE_CONFIG env var ({e}), falling through to file")
        try:
            if os.path.exists(_CONFIG_PATH):
                with open(_CONFIG_PATH, "r") as f:
                    cfg = json.load(f)
                logger.info(f"ComplianceEngine: loaded config from {_CONFIG_PATH}")
                return self._backfill_new_types(cfg)
        except Exception as e:
            logger.error(f"ComplianceEngine: failed to load config ({e}), using defaults")
        return dict(_DEFAULT_CONFIG)

    def _backfill_new_types(self, cfg: Dict) -> Dict:
        """Add any _DEFAULT_TYPES that are missing from a loaded config, then persist."""
        types = cfg.setdefault("types", {})
        added = []
        for t in _DEFAULT_TYPES:
            if t not in types:
                types[t] = {"enabled": True, "action": "redact"}
                added.append(t)
        if added:
            logger.info(f"ComplianceEngine: backfilled new types → {added}")
            try:
                os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
                with open(_CONFIG_PATH, "w") as f:
                    json.dump(cfg, f, indent=2)
            except Exception as e:
                logger.warning(f"ComplianceEngine: could not persist backfilled config: {e}")
        return cfg

    def reload_config(self) -> Dict:
        self._config = self._load_config()
        logger.info(
            f"ComplianceEngine: config reloaded — "
            f"redact={len(self._redact_types())} block={len(self._block_types())}"
        )
        return self.get_config()

    def get_config(self) -> Dict:
        return dict(self._config)

    def update_type(self, type_name: str, enabled: bool = None, action: str = None) -> Dict:
        """Update a single type's enabled/action. Persists to disk."""
        types = self._config.setdefault("types", {})
        entry = types.setdefault(type_name, {"enabled": True, "action": "redact"})
        if enabled is not None:
            entry["enabled"] = enabled
        if action is not None:
            if action not in ("redact", "block", "off"):
                raise ValueError(f"action must be 'redact', 'block', or 'off', got {action!r}")
            entry["action"] = action
        self._save_config()
        return self.get_config()

    def update_config(self, patch: Dict) -> Dict:
        """Merge patch into current config and persist."""
        if "types" in patch:
            cfg_types = self._config.setdefault("types", {})
            for type_name, vals in patch["types"].items():
                entry = cfg_types.setdefault(type_name, {"enabled": True, "action": "redact"})
                if "enabled" in vals:
                    entry["enabled"] = bool(vals["enabled"])
                if "action" in vals:
                    if vals["action"] not in ("redact", "block", "off"):
                        raise ValueError(f"Invalid action for {type_name}: {vals['action']!r}")
                    entry["action"] = vals["action"]
        self._save_config()
        return self.get_config()

    def _save_config(self):
        try:
            os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
            with open(_CONFIG_PATH, "w") as f:
                json.dump(self._config, f, indent=2)
        except Exception as e:
            logger.error(f"ComplianceEngine: failed to persist config: {e}")

    # ========================================================
    # TYPE SETS (derived from config)
    # ========================================================

    def _redact_types(self) -> Set[str]:
        if not self.enabled:
            return set()
        return {
            t for t, cfg in self._config.get("types", {}).items()
            if cfg.get("enabled", True) and cfg.get("action") == "redact"
        }

    def _block_types(self) -> Set[str]:
        # Empty set also neutralises the module-level BLOCKING_TYPES export.
        if not self.enabled:
            return set()
        return {
            t for t, cfg in self._config.get("types", {}).items()
            if cfg.get("enabled", True) and cfg.get("action") == "block"
        }

    def _off_types(self) -> Set[str]:
        return {
            t for t, cfg in self._config.get("types", {}).items()
            if not cfg.get("enabled", True) or cfg.get("action") == "off"
        }

    # ========================================================
    # ML PRIVACY FILTER HELPERS
    # ========================================================

    def _should_call_privacy_svc(self, text: str) -> bool:
        """Call ML for all natural language text. Skip only pure code blocks."""
        if not self.enabled or not PRIVACY_SERVICE_ENABLED:
            return False
        if not os.getenv("PRIVACY_SVC_URL"):
            return False
        if not text.strip():
            return False
        return not self._is_pure_code(text)

    def _is_pure_code(self, text: str) -> bool:
        """Return True only when text is predominantly code syntax with no natural language."""
        code_signals = [
            "import "    in text,
            "from "      in text and "import " in text,
            "def "       in text,
            "function "  in text,
            "class "     in text,
            ";" in text and ("{" in text or "}" in text),
            "</"         in text,
            "SELECT "    in text.upper() and " FROM " in text.upper(),
        ]
        active = sum(bool(s) for s in code_signals)
        if active < 2:
            return False
        # Additional guard: code files have longer average token length than prose
        words = text.split()
        avg_len = sum(len(w) for w in words) / max(len(words), 1)
        return avg_len > 5

    def _call_privacy_svc(self, text: str) -> List[Dict]:
        """Call privacy_svc /filter; return normalized findings. Never raises.

        Cached by sha256(text): the ML verdict is deterministic, so byte-identical
        text (e.g. re-sent tool-result history) is served from an in-memory LRU
        instead of a repeated blocking HTTP round-trip. A cache hit records zero
        latency and leaves _tl.ml_called False so metrics reflect the skipped call.
        """
        if _ML_CACHE_ENABLED:
            _key = _ml_cache_key(text)
            _cached = _ml_cache_get(_key)
            if _cached is not None:
                _tl.privacy_svc_latency_ms = 0.0
                _tl.ml_called = False
                return _cached
        else:
            _key = None
        try:
            # No hardcoded localhost default — set PRIVACY_SVC_URL in .env; an
            # unset value fails the request below, caught by the except clause
            # further down (compliance falls back to the built-in filter).
            url = os.getenv("PRIVACY_SVC_URL", "")
            _t0 = time.perf_counter()
            r = _http_client.post(f"{url}/filter", json={
                "texts":      [text],
                "request_id": get_request_id(),
                "chat_id":    get_chat_id(),
                "user_id":    get_user_id(),
                "span_id":    get_span_id(),
            })
            _tl.privacy_svc_latency_ms = round((time.perf_counter() - _t0) * 1000, 2)
            _tl.ml_called = True
            if r.status_code != 200:
                return []
            entities = r.json().get("results", [[]])[0]
            block_types = self._block_types()
            findings = []
            for ent in entities:
                compliance_type = _ML_LABEL_MAP.get(ent.get("entity_group", ""))
                try:
                    score = float(ent.get("score", 0.0))
                except (TypeError, ValueError):
                    score = 0.0
                if compliance_type:
                    findings.append({
                        "type":     compliance_type,
                        "value":    ent.get("word", ""),
                        "score":    score,
                        "category": "ML",
                        "severity": self._severity(compliance_type),
                        "blocked":  compliance_type in block_types,
                    })
                else:
                    # Informational — keep for logging/audit, severity LOW
                    label = ent.get("entity_group", "UNKNOWN")
                    findings.append({
                        "type":     f"ML_{label.upper()}",
                        "value":    ent.get("word", ""),
                        "score":    score,
                        "category": "ML",
                        "severity": SEVERITY_LOW,
                        "blocked":  False,
                    })
            if findings:
                logger.info(f"PrivacyFilter ML: detected {len(findings)} entities")
            # Cache only clean 200 results — never cache non-200 / exception paths,
            # so a transient service failure is not memoized as "no findings".
            if _ML_CACHE_ENABLED and _key is not None:
                _ml_cache_put(_key, findings)
            return findings
        except Exception as e:
            _tl.privacy_svc_latency_ms = round((time.perf_counter() - _t0) * 1000, 2)
            _tl.ml_called = True
            logger.debug(f"PrivacyFilter ML call failed (non-fatal): {e}")
            return []

    # ========================================================
    # MAIN ANALYSIS
    # ========================================================

    def analyze(self, text: str) -> List[Dict]:
        if not self.enabled:
            return []          # no findings → nothing to redact or block
        if not text:
            return []
        findings: List[Dict] = []
        try:
            findings.extend(self._normalize(detect_pii(text),       "PII"))
            findings.extend(self._normalize(detect_secrets(text),   "SECRET"))
            findings.extend(self._normalize(detect_key_leaks(text), "KEY"))

            # Short-circuit: if a block-configured type is already found by regex,
            # the request will be rejected regardless — skip the ML HTTP call.
            block_types = self._block_types()
            regex_blocked = any(f.get("type") in block_types for f in findings)

            # ML layer — runs for natural language text when PRIVACY_SVC_URL is set,
            # unless regex already found a block-type (request will be rejected anyway).
            if not regex_blocked and self._should_call_privacy_svc(text):
                ml_raw = self._call_privacy_svc(text)
                # Filter ML ACCOUNT_NUMBER findings that lack an account keyword:
                # the model misclassifies short digit strings (order IDs, IFSC, partial
                # Aadhaar) when no explicit account context is present.
                filtered_ml = []
                for mf in ml_raw:
                    if mf.get("type") == "ACCOUNT_NUMBER" and mf.get("category") == "ML":
                        has_kw = bool(re.search(
                            r'\b(?:account|acct|a/c|acc)\b', text, re.IGNORECASE
                        ))
                        if not has_kw:
                            continue  # ML ACCOUNT_NUMBER requires keyword anchor
                    filtered_ml.append(mf)
                findings.extend(filtered_ml)
            # Deduplicate: same (type, value) from regex + ML → keep first occurrence
            seen: Set[tuple] = set()
            deduped: List[Dict] = []
            for f in findings:
                key = (f.get("type"), f.get("value"))
                if key not in seen:
                    seen.add(key)
                    deduped.append(f)
            findings = deduped
            if findings:
                logger.critical(f"PCI VIOLATION DETECTED → count={len(findings)}")
        except Exception as e:
            logger.error(f"ComplianceEngine analyze failed: {e}")
        return findings

    # ========================================================
    # INPUT VALIDATION — redact, then check block-configured types
    # ========================================================

    def validate_input(self, text: str, keep_types: Optional[Set[str]] = None) -> Dict:
        """
        1. Run full analysis (regex + ML) on original text.
        2. Redact all types configured as action='redact' via regex.
        3. Also redact ML-detected values missed by regex.
        4. Block if any block-configured types remain.
        Returns redacted_text for callers to use in the LLM call.

        keep_types: types to LEAVE UNREDACTED in the prompt (block checks still
        apply). Used by the tool-driven assistant path (Cowork/CLI) to preserve
        contact identifiers like EMAIL/MOBILE/UPI that connector tool calls need —
        redacting them to "[EMAIL]" turns a Graph query into an empty result.
        Never include card/secret types here.
        """
        # Disabled → allow through untouched, same dict shape callers expect.
        if not self.enabled:
            return {
                "allowed":                True,
                "blocked":                False,
                "blocked_types":          [],
                "findings":               [],
                "redacted_text":          text,   # original text, unmodified
                "was_redacted":           False,
                "redacted_types":         [],
                "total_latency_ms":       0.0,
                "privacy_svc_latency_ms": 0.0,
                "ml_called":              False,
            }

        _t_start = time.perf_counter()
        _tl.privacy_svc_latency_ms = 0.0
        _tl.ml_called = False

        keep_types   = keep_types or set()
        redact_types = self._redact_types() - keep_types
        block_types  = self._block_types()

        # Step 1: Full analysis (regex detectors + optional ML layer)
        findings = self.analyze(text)

        # Step 2: Regex-based redaction
        try:
            redacted_text, redacted_types = redact_all(text, redact_types)
        except Exception as e:
            logger.error(f"Redaction failed: {e}")
            redacted_text, redacted_types = text, []

        # Step 3: Redact ML-detected values that regex missed.
        # Guards:
        #   - skip ML_ informational types (private_person etc.)
        #   - skip values < 4 chars (tokenizer noise: "SB", "IN", "000")
        #   - mask consistently (same format as regex layer) instead of [TYPE] literal
        ml_findings = [f for f in findings if f.get("category") == "ML" and f.get("value")]
        for mf in ml_findings:
            val = mf.get("value", "").strip()
            compliance_type = mf.get("type", "")
            if not val or compliance_type.startswith("ML_") or compliance_type in keep_types:
                continue
            if len(val) < 4:
                continue  # tokenizer noise — too short to be real PII
            try:
                ml_score = float(mf.get("score", 0.0))
            except (TypeError, ValueError):
                ml_score = 0.0

            # Per-type guards against ML false positives:
            # ACCOUNT_NUMBER requires (a) keyword anchor present in original text
            # OR (b) >= 9 stripped digits AND high confidence score.
            # Without a keyword (account/acct/a/c/acc), short digit strings like
            # order IDs, IFSC codes, or partial Aadhaar patterns get misclassified.
            if compliance_type == "ACCOUNT_NUMBER":
                # ML ACCOUNT_NUMBER requires an account keyword anchor in the input.
                # Without one, any digit string (order IDs, IFSC codes, Aadhaar, etc.)
                # scores high as an account number — the regex layer already requires the
                # keyword, so the ML layer must match that bar to avoid false positives.
                has_keyword = bool(re.search(
                    r'\b(?:account|acct|a/c|acc)\b', text, re.IGNORECASE
                ))
                if not has_keyword:
                    continue

            # Use consistent masking: keep first 2 + last 2, mask middle with *
            def _ml_mask(m, ct=compliance_type):
                v = m.group()
                digits = re.sub(r'\D', '', v)
                if len(digits) >= 6:
                    return digits[:2] + "*" * (len(digits) - 4) + digits[-2:]
                return v[:2] + "***" + v[-2:] if len(v) >= 5 else "***"
            out, n = re.subn(rf'\b{re.escape(val)}\b', _ml_mask, redacted_text)
            if n > 0:
                redacted_text = out
                if compliance_type not in redacted_types:
                    redacted_types.append(compliance_type)

        was_redacted = bool(redacted_types)
        if was_redacted:
            logger.info(f"PII REDACTED (input) → types={redacted_types}")

        # Step 4: Block check
        blocked    = False
        block_hits = [f["type"] for f in findings if f["type"] in block_types]
        if block_hits:
            logger.critical(f"PCI BLOCK TRIGGERED → types={block_hits}")
            blocked = True

        result = {
            "allowed":                 not blocked,
            "blocked":                 blocked,
            "blocked_types":           block_hits,
            "findings":                findings,
            "redacted_text":           redacted_text,
            "was_redacted":            was_redacted,
            "redacted_types":          redacted_types,
            "total_latency_ms":        round((time.perf_counter() - _t_start) * 1000, 2),
            "privacy_svc_latency_ms":  getattr(_tl, "privacy_svc_latency_ms", 0.0),
            "ml_called":               getattr(_tl, "ml_called", False),
        }

        if _AUDIT_LOG_PATH:
            try:
                # Never persist raw PCI/PII to disk — audit log keeps only the
                # redacted text plus finding type counts. The 'value' field is
                # truncated to first 2 + last 2 chars so investigators can
                # correlate without re-exposing the secret.
                _safe_findings = []
                for f in findings:
                    v = (f.get("value") or "")[:]
                    if len(v) >= 6:
                        v_masked = v[:2] + "*" * (len(v) - 4) + v[-2:]
                    elif len(v) > 0:
                        v_masked = "***"
                    else:
                        v_masked = ""
                    _safe_findings.append({"type": f["type"], "value_masked": v_masked})
                entry = json.dumps({
                    "ts":           time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "input":        redacted_text,           # redacted, not raw
                    "findings":     _safe_findings,
                    "redacted":     redacted_text,
                    "was_redacted": was_redacted,
                    "blocked":      blocked,
                })
                with _audit_lock:
                    with open(_AUDIT_LOG_PATH, "a") as _af:
                        _af.write(entry + "\n")
            except Exception as _ae:
                logger.debug(f"Compliance audit log write failed: {_ae}")

        return result

    # ========================================================
    # OUTPUT VALIDATION — redact only, NEVER block
    # ========================================================

    def validate_output(self, text: str) -> Dict:
        """
        Redact all configured types from LLM output.
        Output is NEVER blocked — blocked is always False.
        """
        if not self.enabled:
            return {
                "allowed":       True,
                "blocked":       False,
                "findings":      [],
                "redacted_text": text,
                "was_redacted":  False,
            }

        redact_types = self._redact_types() | self._block_types()  # redact even block-configured types on output

        try:
            redacted_text, redacted_types = redact_all(text, redact_types)
        except Exception as e:
            logger.error(f"Output redaction failed: {e}")
            redacted_text, redacted_types = text, []

        was_redacted = bool(redacted_types)
        if was_redacted:
            logger.info(f"PII REDACTED (output) → types={redacted_types}")

        return {
            "allowed":       True,
            "blocked":       False,   # output is NEVER blocked
            "findings":      [],
            "redacted_text": redacted_text,
            "was_redacted":  was_redacted,
        }

    # ========================================================
    # TEXT REDACTION HELPER
    # ========================================================

    def redact_text(self, text: str, keep_types: Optional[Set[str]] = None):
        """Redact all configured types from text. Returns (redacted_text, type_names).

        keep_types: types to LEAVE UNREDACTED — used by the tool-driven assistant
        path (Cowork/Buddy connector read results) to preserve contact identifiers
        like EMAIL/MOBILE/UPI. Redacting a sender's address to "[EMAIL]" makes the
        agent reply to a masked recipient → Graph resolves 0 recipients. Never
        include card/secret types here (see _OUTBOUND_BLOCK_TYPES in mcp_bridge).
        """
        if not self.enabled:
            return text, []

        keep_types = keep_types or set()
        try:
            return redact_all(text, (self._redact_types() | self._block_types()) - keep_types)
        except Exception as e:
            logger.error(f"redact_text failed: {e}")
            return text, []

    # ========================================================
    # NORMALIZE FINDINGS
    # ========================================================

    def _normalize(self, raw: List[Dict], category: str) -> List[Dict]:
        block_types = self._block_types()
        out = []
        for item in raw:
            t = item.get("type", "UNKNOWN")
            out.append({
                "type":     t,
                "value":    item.get("value"),
                "category": category,
                "severity": self._severity(t),
                "blocked":  t in block_types,
            })
        return out

    # ========================================================
    # SEVERITY CLASSIFIER
    # ========================================================

    def _severity(self, t: str) -> str:
        if t in ("PAN", "CVV", "PIN_BLOCK", "EXPIRY",
                 "INDIA_PAN", "AADHAAR", "ACCOUNT_NAME_COMBO",
                 "PRIVATE_KEY_LEAK", "PAYMENT_KEY_LEAK"):
            return SEVERITY_CRITICAL
        if t in ("ACCOUNT_NUMBER", "IFSC_CODE", "EMAIL", "MOBILE",
                 "SECRET", "API_KEY", "ACCESS_TOKEN",
                 "CERTIFICATE_LEAK", "SSH_KEY_LEAK", "KEY_ASSIGNMENT_LEAK"):
            return SEVERITY_HIGH
        if t == "UPI":
            return SEVERITY_MEDIUM
        return SEVERITY_LOW

    # ========================================================
    # BACKWARD COMPAT — should_block (respects config)
    # ========================================================

    def should_block(self, findings: List[Dict]) -> bool:
        if not self.enabled:
            return False
        block_types = self._block_types()
        for f in findings:
            if f.get("type") in block_types:
                logger.critical(f"PCI BLOCK → type={f.get('type')}")
                return True
        return False


# ============================================================
# SINGLETON INSTANCE
# ============================================================

compliance_engine = ComplianceEngine()

# Backward-compat: expose BLOCKING_TYPES as a property of the module
# (callers that import BLOCKING_TYPES directly get the current block set)
BLOCKING_TYPES = compliance_engine._block_types()
