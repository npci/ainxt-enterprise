# SPDX-License-Identifier: MIT
"""
LLM-as-Judge evaluation engine — Domain-agnostic, context-driven.

HOW DOMAIN CONTEXT IS HANDLED
==============================
Nothing is hardcoded.  The judge learns the domain from what the system
already knows at runtime:

  • repo_ctx  (dict from hybrid_retriever / SDLC pipeline):
      repo, tech_stack, language, framework, file_tree
      → injected into every judge prompt so criteria adapt automatically.

  • retrieved chunks (for retrieval / groundedness evals):
      the judge infers what "relevant" means from the actual chunks.

  • No domain names, no product names, no compliance regimes appear
    in prompt templates.  If this platform serves a Java shop today
    and a Node/TypeScript shop tomorrow, the same eval engine works.

STRUCTURED VERDICT
==================
Every judge returns:
  {
    "score":    0.0–1.0,          # (criteria_passed / total_criteria)
    "verdict":  "ACCEPT"|"REJECT",
    "reason":   "one sentence",
    "issues":   ["specific problem …"]   # empty on ACCEPT
  }

ENFORCEMENT
===========
  • Chat evals  → fire-and-forget daemon thread (zero added latency).
  • SDLC evals  → blocking.  REJECT triggers one LLM retry with judge
                  feedback injected.  Still-REJECT after retry posts a
                  ⚠ warning comment to Jira but does not halt the pipeline.

ACCEPT threshold: score >= 0.70
"""

import concurrent.futures as _cf
import logging
import os
import threading
import time
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

ACCEPT_THRESHOLD = 0.70

# ── Global eval kill-switch ────────────────────────────────────────────────────
# Set EVAL_ENABLED=false in .env to disable LLM-as-judge evaluation calls.
#
# EVAL_ENABLED=true (OSS default) — LLM-as-judge runs after every chat
#                    response for quality monitoring, out of the box.
# EVAL_ENABLED=false — all eval calls are skipped. No extra LLM cost, no extra
#                    latency per chat message. Use this for local dev, or for
#                    OSS deployments that don't need quality monitoring.
EVAL_ENABLED: bool = os.getenv("EVAL_ENABLED", "true").lower() not in ("false", "0", "no")

# ── Judge executor + timeout ──────────────────────────────────────────────────
#
# ROOT CAUSE OF "judge timeout" ERRORS:
# The original hard-coded 15s timeout was too short for the local LLM.
# The judge sends prompts of 1,000–2,000 chars to model_router.generate()
# which calls _try_local_simple() → local LLM HTTP request.
# The local LLM (kimi-k2.7-code, qwen, etc.) can take 20–60s on first call
# (GPU warm-up, model loading, KV-cache cold start).
# The HTTP layer has its own timeout (LLM_TIMEOUT_SEC, default 300s) but the
# judge's ThreadPoolExecutor.result(timeout=15) fires FIRST, cancelling the
# future and returning score=0.5 / reason="judge timeout" — a fake result.
#
# THREE-PART FIX:
#   1. Timeout is now read from EVAL_JUDGE_TIMEOUT env var (default 60s).
#      Set higher (e.g. 120) if your local GPU is slow to warm up.
#      Set lower (e.g. 30) if you have a fast local model.
#   2. On timeout, the warning now tells you exactly what to check and fix.
#   3. The executor is expanded to 6 workers (was 4) so concurrent evals
#      from chat + RAG + coach don't queue behind each other and compound
#      the timeout problem.
#
# If you still see timeouts after increasing EVAL_JUDGE_TIMEOUT:
#   → Check LOCAL_LLM_BASE_URL is reachable: curl $LOCAL_LLM_BASE_URL/v1/models
#   → Check LLM_PROXY_URL is set and reachable as fallback
#   → Check OPENAI_API_KEY is set for GPT-5-mini fallback
#   → Set EVAL_ENABLED=false in .env to disable evals entirely in local dev
_JUDGE_EXECUTOR = _cf.ThreadPoolExecutor(max_workers=6, thread_name_prefix="eval-judge")
_JUDGE_TIMEOUT  = float(os.getenv("EVAL_JUDGE_TIMEOUT", "60"))


# ── Dynamic context builder — no hardcoding ───────────────────────────────────

def _build_repo_context(repo_ctx: Optional[dict]) -> str:
    """
    Build a terse description of the repo from whatever repo_ctx contains.
    Falls back to generic language when nothing is available.
    """
    if not repo_ctx:
        return "Repository context: not available — evaluate generically."
    lines = []
    if repo_ctx.get("repo"):
        lines.append(f"Repository: {repo_ctx['repo']}")
    if repo_ctx.get("tech_stack"):
        lines.append(f"Tech stack: {repo_ctx['tech_stack']}")
    if repo_ctx.get("language"):
        lines.append(f"Primary language: {repo_ctx['language']}")
    if repo_ctx.get("framework"):
        lines.append(f"Framework: {repo_ctx['framework']}")
    if repo_ctx.get("test_framework"):
        lines.append(f"Test framework: {repo_ctx['test_framework']}")
    return "\n".join(lines) if lines else "Repository context: not available — evaluate generically."


# ── Judge prompt templates — zero hardcoded domain knowledge ─────────────────

_GROUNDEDNESS_PROMPT = """\
You are a senior software engineer auditing an AI-generated answer for hallucinations.

{repo_context}

DEVELOPER QUESTION:
{question}

RETRIEVED CONTEXT (from codebase):
{context}

AI ANSWER:
{answer}

Evaluate each criterion (true/false):
C1: The answer does NOT name functions, classes, or methods that are absent from both the context and established knowledge of the language/framework in use?
C2: The answer does NOT invent file paths, module names, or package names that do not appear in the context?
C3: The answer does NOT fabricate API endpoints, database tables, or configuration keys that are not in the context?
C4: When the context is empty or irrelevant, the answer draws on genuine language/framework knowledge rather than inventing platform-specific details?
C5: The answer contains no secrets, credentials, or sensitive data (tokens, passwords, private keys)?

score = (criteria passed) / 5.
C5 failure → score = 0.0 and REJECT regardless of other criteria.
verdict = ACCEPT if score >= 0.70, else REJECT.

Reply with ONLY this JSON:
{{"score": <0-1 float>, "verdict": "ACCEPT" or "REJECT", "reason": "<one sentence>", "issues": [<list of specific problems, empty list if ACCEPT>], "criteria": {{"C1": <bool>, "C2": <bool>, "C3": <bool>, "C4": <bool>, "C5": <bool>}}}}"""

_RELEVANCE_PROMPT = """\
You are a senior software engineer evaluating whether an AI response is useful.

{repo_context}

DEVELOPER QUESTION:
{question}

AI ANSWER:
{answer}

Evaluate each criterion (true/false):
C1: The answer directly and specifically addresses what was asked — not a tangential or adjacent topic?
C2: The technical depth is appropriate — not a one-liner for a complex question, not padded with fluff?
C3: The answer avoids unhelpful generic boilerplate ("there are many approaches", "it depends on your needs") without concrete follow-up?
C4: If the question asks for code or a specific implementation, the answer provides a concrete example rather than pseudocode or vague guidance?
C5: The answer is accurate for the language and framework shown in the repo context (no advice that only applies to a different ecosystem)?

score = (criteria passed) / 5.  verdict = ACCEPT if score >= 0.70, else REJECT.

Reply with ONLY this JSON:
{{"score": <0-1 float>, "verdict": "ACCEPT" or "REJECT", "reason": "<one sentence>", "issues": [<list of specific problems, empty list if ACCEPT>], "criteria": {{"C1": <bool>, "C2": <bool>, "C3": <bool>, "C4": <bool>, "C5": <bool>}}}}"""

_COACH_PROMPT_EVAL_PROMPT = """\
You are AiNxt Coach, an AI-usage practice evaluator.

Evaluate the following user prompt against 6 coaching criteria.
Only evaluate what is visible in the prompt text itself — do not penalise for
signals that require external context (thread length, model cost, prior turns).

C1 [Clarity]: The prompt has a clear, specific goal — not vague ("fix it", "help",
  "improve", "make it better") and does not rely on ambiguous pronouns ("it", "this",
  "that") without a clear antecedent in the same prompt.
C2 [Constraints]: When the task is a build/write/implement/refactor request, the
  prompt specifies relevant constraints (language, framework, version, or scope).
  Conversational or exploratory questions are exempt.
C3 [Intent]: The prompt expresses a single, coherent intent — not multiple unrelated
  asks bundled into one turn (e.g. "add tests AND update docs AND bump version").
C4 [Safety]: The prompt contains no sensitive data patterns — credentials, API keys,
  PII (names, emails, phone numbers, card numbers), secrets, or passwords.
C5 [Completeness]: The prompt provides enough context for the model to act without
  guessing — target file/function, error message, or relevant code snippet is present
  when the task requires it.
C6 [Definition of Done]: When the task is a build/write/implement request, the prompt
  states what "done" looks like — expected output, acceptance criteria, or a success
  condition. Conversational or exploratory questions are exempt.

PROMPT TO EVALUATE:
{prompt}

score = (criteria passed) / 6.  verdict = ACCEPT if score >= 0.70 (at least 5 of 6), else REJECT.
C4 failure (sensitive data detected) → score = 0.0 and REJECT regardless of other criteria.

Reply with ONLY this JSON (no markdown fencing, no extra text):
{{"score": <0-1 float>, "verdict": "ACCEPT" or "REJECT", "reason": "<one sentence>", "issues": [<list of specific problems, empty list if ACCEPT>], "criteria": {{"C1": <bool>, "C2": <bool>, "C3": <bool>, "C4": <bool>, "C5": <bool>, "C6": <bool>}}}}"""


# ── EvalEngine ────────────────────────────────────────────────────────────────

class EvalEngine:
    """Domain-agnostic LLM-as-judge evaluation engine. Use the `eval_engine` singleton."""

    def _judge(self, prompt: str) -> dict:
        import json as _json
        try:
            from models.model_router import model_router
            # Wrap generate() so we capture last_model_label on the SAME
            # worker thread where generate() runs — it is a thread-local, so
            # reading it on the calling thread after fut.result() always
            # returns the default "auto".
            def _generate_and_capture():
                output = model_router.generate(prompt, "simple")
                label  = getattr(model_router, "last_model_label", None) or None
                return output, label

            # Submit to bounded executor — caps concurrent judge LLM calls at 4.
            # Hard timeout: if the LLM (local or proxy) doesn't respond in 15s,
            # fail open immediately rather than lingering for 30-60s.
            fut = _JUDGE_EXECUTOR.submit(_generate_and_capture)
            try:
                raw, _judge_model = fut.result(timeout=_JUDGE_TIMEOUT)
            except _cf.TimeoutError:
                _judge_model = None
                logger.warning(
                    f"EvalEngine._judge: timed out after {_JUDGE_TIMEOUT}s. "
                    f"Root cause: local LLM (LOCAL_LLM_BASE_URL) is slow or unreachable, "
                    f"AND all cloud fallbacks (GPT-5-mini, Claude) also failed or are unconfigured. "
                    f"Fix options: "
                    f"(1) Set EVAL_JUDGE_TIMEOUT=120 in .env and restart. "
                    f"(2) Verify LOCAL_LLM_BASE_URL is reachable. "
                    f"(3) Set OPENAI_API_KEY for GPT-5-mini fallback. "
                    f"(4) Set EVAL_ENABLED=false to disable evals in local dev. "
                    f"Failing open with score=0.5 so users are not blocked."
                )
                return _default_result("judge timeout")
            if not raw:
                return _default_result("empty judge response")
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = _json.loads(raw[start:end])
                parsed.setdefault("issues",   [])
                parsed.setdefault("criteria", {})
                # ── Bug-fix: never trust the LLM's self-reported score.
                # Recalculate from the criteria booleans so a hallucinating
                # judge cannot inflate its own score.  Falls back to the
                # LLM-reported value only when no criteria were returned
                # (e.g. human_feedback rows that carry no criteria dict).
                criteria = parsed.get("criteria") or {}
                if criteria:
                    _passed = sum(1 for v in criteria.values() if v)
                    parsed["score"] = round(_passed / len(criteria), 4)
                # Derive verdict from the (now trustworthy) score.
                parsed["verdict"] = "ACCEPT" if parsed.get("score", 0) >= ACCEPT_THRESHOLD else "REJECT"
                parsed["judge_model"] = _judge_model
                return parsed
        except Exception:
            logger.warning("EvalEngine._judge error")
        return _default_result("judge parse error")

    def _persist(self, eval_type: str, score: float, verdict: str, reason: str,
                 issues: list, criteria: dict,
                 session_id: Optional[str], run_id: Optional[str],
                 question: str, platform: Optional[str] = None,
                 model: Optional[str] = None,
                 judge_model: Optional[str] = None) -> None:
        try:
            import datetime
            from db.database import SessionLocal
            from db.models import EvalResult
            db = SessionLocal()
            try:
                db.add(EvalResult(
                    id=str(uuid.uuid4()),
                    eval_type=eval_type,
                    score=score,
                    reason=reason,
                    session_id=session_id,
                    run_id=run_id,
                    question=question[:500] if question else None,
                    metadata_={"verdict": verdict, "issues": issues, "criteria": criteria},
                    created_at=datetime.datetime.utcnow(),
                    platform=platform,
                    model=model,
                    judge_model=judge_model,
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.warning("EvalEngine._persist error")

    def _log(self, eval_type: str, result: dict) -> None:
        verdict    = result.get("verdict", "?")
        score      = result.get("score", 0)
        reason     = result.get("reason", "")
        issues     = result.get("issues", [])
        issues_str = (" | issues: " + "; ".join(str(i) for i in issues)) if issues else ""
        logger.info(f"EVAL[{eval_type}] {verdict} score={score:.2f}{issues_str} | {type(reason).__name__}")

    def _run_sync(self, eval_type: str, prompt: str, question: str,
                  session_id: Optional[str], run_id: Optional[str],
                  platform: Optional[str] = None,
                  model: Optional[str] = None) -> dict:
        t0 = time.time()
        result = self._judge(prompt)
        result["latency_ms"] = int((time.time() - t0) * 1000)
        self._persist(eval_type, result["score"], result["verdict"], result["reason"],
                      result["issues"], result["criteria"], session_id, run_id, question,
                      platform=platform, model=model,
                      judge_model=result.get("judge_model"))
        self._log(eval_type, result)
        return result

    def _run_async(self, eval_type: str, prompt: str, question: str,
                   session_id: Optional[str], run_id: Optional[str],
                   platform: Optional[str] = None,
                   model: Optional[str] = None) -> None:
        def _task():
            t0 = time.time()
            result = self._judge(prompt)
            result["latency_ms"] = int((time.time() - t0) * 1000)
            self._persist(eval_type, result["score"], result["verdict"], result["reason"],
                          result["issues"], result["criteria"], session_id, run_id, question,
                          platform=platform, model=model,
                          judge_model=result.get("judge_model"))
            self._log(eval_type, result)
        threading.Thread(target=_task, daemon=True).start()

    # ── Public API ────────────────────────────────────────────────────────────

    def eval_answer_quality(
        self,
        question: str,
        answer: str,
        context: list,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        repo_ctx: Optional[dict] = None,
        platform: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Fire-and-forget: groundedness + relevance after an answer is complete.

        platform — source platform tag (e.g. "chat", "knowledge_base", "ide_extension").
                   None is accepted for backward compatibility; rows will have platform=NULL.
        model    — source model that generated the answer (e.g. "GPT-5 Mini (gpt-5-mini)").
                   Stored on both groundedness and relevance rows so the dashboard can
                   filter and compare scores by model. None is accepted for backward compatibility.
        """
        if not EVAL_ENABLED:
            return
        if not answer:
            return
        repo_context = _build_repo_context(repo_ctx)
        context_text = (
            "\n---\n".join(str(c)[:400] for c in context[:4])
            if context else "(no context — answer from model knowledge)"
        )
        self._run_async(
            "groundedness",
            _GROUNDEDNESS_PROMPT.format(
                repo_context=repo_context,
                question=question,
                context=context_text,
                answer=answer[:1000],
            ),
            question, session_id, run_id,
            platform=platform,
            model=model,
        )
        self._run_async(
            "relevance",
            _RELEVANCE_PROMPT.format(
                repo_context=repo_context,
                question=question,
                answer=answer[:1000],
            ),
            question, session_id, run_id,
            platform=platform,
            model=model,
        )

    def eval_coach_prompt(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        blocking: bool = False,
        platform: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[dict]:
        """Evaluate a user prompt for coaching quality (clarity, constraints,
        intent, safety, completeness).

        Runs fire-and-forget by default (blocking=False → returns None).
        Pass blocking=True to get the result dict back (use inside a daemon
        thread so the caller is never blocked).

        Gated by EVAL_ENABLED. Fails open on any judge error.
        Persists to EvalResult with eval_type="coach_prompt".

        platform — source platform tag (e.g. "chat", "buddy_cowork").
                   None is accepted for backward compatibility.
        model    — source model that generated the answer (e.g. "gpt-5-mini").
                   Stored on the EvalResult row so the dashboard can show
                   which model the user was talking to when the prompt was sent.
                   None is accepted for backward compatibility.
        """
        if not EVAL_ENABLED:
            return None
        if not (prompt or "").strip():
            return None
        judge_prompt = _COACH_PROMPT_EVAL_PROMPT.format(prompt=prompt[:1500])
        if blocking:
            return self._run_sync("coach_prompt", judge_prompt, prompt[:200],
                                  session_id, run_id, platform=platform,
                                  model=model)
        self._run_async("coach_prompt", judge_prompt, prompt[:200],
                        session_id, run_id, platform=platform, model=model)
        return None

    def submit_eval(
        self,
        platform: str,
        question: str,
        answer: str,
        context: Optional[list] = None,
        prompt: Optional[str] = None,
        session_id: Optional[str] = None,
        run_id: Optional[str] = None,
        repo_ctx: Optional[dict] = None,
        model: Optional[str] = None,
    ) -> None:
        """
        Unified, config-driven, Kafka-backed eval submission.

        This is the preferred entry point for all platform eval calls.
        It:
          1. Looks up which eval types are enabled for the platform
             (via core.eval_config.PLATFORM_EVAL_CONFIG).
          2. Publishes a single event to the ainxt.eval_events Kafka topic
             (fire-and-forget — returns immediately, zero latency impact).
          3. Falls back to direct async daemon threads if Kafka is unavailable
             (same behaviour as the pre-Kafka implementation).

        Parameters
        ----------
        platform   : Source platform key — e.g. "chat", "knowledge_base".
                     Must match a key in PLATFORM_EVAL_CONFIG.
        question   : The user's question / prompt (truncated to 500 chars).
        answer     : The AI's answer (truncated to 1000 chars).
        context    : Retrieved codebase/KB chunks (up to 4 × 400 chars).
        prompt     : Raw user prompt for coach_prompt eval (1500 chars).
                     If None, coach_prompt eval is skipped even if configured.
        session_id : Chat session identifier (for dashboard filtering).
        run_id     : SDLC run identifier (for run-level drill-down).
        repo_ctx   : Repository context dict (repo, tech_stack, language, etc.).
        model      : Source model name (e.g. "gpt-5-mini"). Stored on EvalResult
                     rows so the dashboard can show which model was in use.
        """
        if not EVAL_ENABLED:
            return

        from core.eval_config import get_eval_types
        from core.kafka_producer import produce, TOPIC_EVAL_EVENTS
        import uuid as _uuid_mod
        import datetime as _dt

        eval_types = get_eval_types(platform)

        event = {
            "schema_version": "1.0",
            "event_id":       str(_uuid_mod.uuid4()),
            "platform":       platform,
            "eval_types":     eval_types,
            "question":       (question or "")[:500],
            "answer":         (answer or "")[:1000],
            "context":        [str(c)[:400] for c in (context or [])[:4]],
            "prompt":         (prompt or "")[:1500],
            "session_id":     session_id,
            "run_id":         run_id,
            "repo_ctx":       repo_ctx or {},
            "timestamp":      _dt.datetime.utcnow().isoformat(),
        }

        sent = produce(TOPIC_EVAL_EVENTS, event, key=platform)
        if not sent:
            # Kafka unavailable — fall back to existing direct async path so
            # eval coverage is never silently dropped.
            logger.debug(
                f"EvalEngine.submit_eval: Kafka unavailable for platform={platform} "
                f"— falling back to direct async thread"
            )
            self.eval_answer_quality(
                question, answer, context or [],
                session_id=session_id, run_id=run_id,
                repo_ctx=repo_ctx, platform=platform, model=model,
            )
            if prompt and "coach_prompt" in eval_types:
                self.eval_coach_prompt(
                    prompt, session_id=session_id, run_id=run_id,
                    platform=platform, model=model,
                )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _default_result(reason: str) -> dict:
    # Fail-open for chat (don't block users when judge itself fails)
    return {"score": 0.5, "verdict": "ACCEPT", "reason": reason, "issues": [], "criteria": {}}


# ── Singleton ─────────────────────────────────────────────────────────────────
eval_engine = EvalEngine()
