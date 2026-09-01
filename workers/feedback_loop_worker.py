# SPDX-License-Identifier: Apache-2.0
"""
workers/feedback_loop_worker.py — P6: Learning / feedback loop worker.

Runs every 1h via the cron scheduler in start_workers.py.

Tasks:
  1. FeedbackProcessor.process_recent_feedback() — extract preferences from
     thumbs-up, compute chunk quality penalties from thumbs-down.

The worker is intentionally lightweight — all heavy logic lives in
services/feedback_processor.py for testability.
"""

from core.logger import logger


def run_feedback_loop() -> dict:
    """
    Process recent feedback to improve retrieval quality.

    Returns a summary dict for logging/monitoring.
    Called every 1h by the cron scheduler in start_workers.py.
    """
    result = {
        "preferences_stored": 0,
        "chunks_penalized":   0,
        "error":              None,
    }
    try:
        from services.feedback_processor import FeedbackProcessor
        processor = FeedbackProcessor()
        summary = processor.process_recent_feedback(lookback_hours=2)
        result.update(summary)
        logger.info(
            f"feedback_loop_worker: preferences_stored={result['preferences_stored']} "
            f"chunks_penalized={result['chunks_penalized']}"
        )
    except Exception as e:
        logger.error(f"feedback_loop_worker failed: {e}")
        result["error"] = str(e)
    return result
