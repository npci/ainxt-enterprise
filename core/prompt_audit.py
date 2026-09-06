# SPDX-License-Identifier: MIT
# ============================================================
# PROMPT AUDIT LOG  —  core/prompt_audit.py
# ============================================================
#
# Shared utility for writing compliance-blocked user prompts
# to user_prompts.log.
#
# Used by:
#   - gateway.py  (direct write at compliance block time)
#   - workers/kafka_consumer.py  (write when consuming Kafka events)
#
# Writing directly from gateway.py guarantees the audit entry is
# written even when Kafka is disabled (KAFKA_ENABLED=false) or the
# kafka_consumer worker is not running.
# ============================================================

import json
import logging
import os

from core.logger import (
    _LOG_DIR,
    SizeAndTimeRotatingFileHandler,
    LOG_MAX_BYTES,
    LOG_ROTATION_WHEN,
    LOG_ROTATION_INTERVAL,
    LOG_BACKUP_COUNT,
    LOG_ROTATION_UTC,
    logger as _app_logger,
)

# ── Dedicated file logger for user_prompts.log ────────────────────────────────

_PROMPT_LOG_FILE = os.path.join(_LOG_DIR, "user_prompts.log")

_prompt_file_logger = logging.getLogger("ainxt.user_prompts")
_prompt_file_logger.setLevel(logging.INFO)
_prompt_file_logger.propagate = False  # do not leak into root / agent.log

if not any(
    isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == _PROMPT_LOG_FILE
    for h in _prompt_file_logger.handlers
):
    _ph = SizeAndTimeRotatingFileHandler(
        filename=_PROMPT_LOG_FILE,
        max_bytes=LOG_MAX_BYTES,
        when=LOG_ROTATION_WHEN,
        interval=LOG_ROTATION_INTERVAL,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
        errors="replace",
        utc=LOG_ROTATION_UTC,
        delay=False,
    )
    _ph.setLevel(logging.INFO)
    _ph.setFormatter(logging.Formatter("%(message)s"))
    _prompt_file_logger.addHandler(_ph)


def log_user_prompt(
    *,
    timestamp: str,
    user_id: str,
    user_name: str,
    login_id: str,
    chat_id: str,
    request_id: str,
    prompt: str,
    compliance_blocked: bool = False,
    block_reason: str = "",
    block_policy: str = "",
    block_category: str = "",
    confidence_score: float | None = None,
) -> None:
    """
    Write a single JSON line to user_prompts.log for every compliance-blocked
    user-initiated prompt.

    Fields:
      timestamp          – UTC ISO-8601 ms timestamp of the event
      user_id            – authenticated user identifier
      user_name          – display name of the authenticated user
      login_id           – login / AD identifier / email used for the user
      chat_id            – conversation / session identifier
      request_id         – gateway request correlation ID
      prompt             – the raw user question text
      compliance_blocked – True when the compliance/guardrail engine rejected the prompt
      block_reason       – comma-separated violation types (e.g. "HARDBLOCK,pci_pan")
      block_policy       – policy name that triggered the block
                           (e.g. "AI Safety policy", "compliance policy")
      block_category     – semantic category that triggered the block
                           (e.g. "criminal_justice" for HardBlock, or
                           "pci_pan,pii_mobile" for PCI/PII violations).
      confidence_score   – Optional float in [0.0, 1.0] indicating the safety
                           stack's confidence that motivated the block decision
                           (hardblock regex confidence or PII detector confidence).
                           Use None when no score is available from the upstream
                           detector. Higher values indicate stronger confidence.

    This function is safe to call from any process (gateway, consumer, worker).
    The underlying logger is process-safe via file locking in SizeAndTimeRotatingFileHandler.
    """
    try:
        # Normalize confidence_score: coerce numeric strings, clamp into [0, 1],
        # and emit None when the value is missing or unparseable so downstream
        # consumers can distinguish "no score" from "score = 0.0".
        _normalized_confidence: float | None
        if confidence_score is None:
            _normalized_confidence = None
        else:
            try:
                _cs = float(confidence_score)
                if _cs < 0.0:
                    _cs = 0.0
                elif _cs > 1.0:
                    _cs = 1.0
                _normalized_confidence = round(_cs, 4)
            except (TypeError, ValueError):
                _normalized_confidence = None

        _prompt_file_logger.info(
            json.dumps(
                {
                    "timestamp":          timestamp,
                    "user_id":            user_id,
                    "user_name":          user_name,
                    "login_id":           login_id,
                    "chat_id":            chat_id,
                    "request_id":         request_id,
                    "prompt":             prompt,
                    "compliance_blocked": compliance_blocked,
                    "block_reason":       block_reason,
                    "block_policy":       block_policy,
                    "block_category":     block_category,
                    "confidence_score":   _normalized_confidence,
                },
                ensure_ascii=False,
            )
        )
    except Exception as _e:
        _app_logger.warning(f"prompt_audit: failed to write user_prompts.log: {_e}")
