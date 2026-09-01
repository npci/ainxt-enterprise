# SPDX-License-Identifier: Apache-2.0
# ============================================================
# ENTERPRISE TOOLS MODULE (FINAL PRODUCTION VERSION)
# AiNxt-GRADE • PCI-COMPLIANT • AGENTIC-READY • BUG-FREE
# ============================================================

import os
from typing import Generator, List

from core.logger import logger

from models.hybrid_retriever import hybrid_retrieve_context
from gateway_local_llm import get_local_gateway as _get_local_gateway

from core.prompts import CODE_PROMPT, GENERAL_PROMPT, OFFICE_PROMPT

GROUNDED_PROMPT = (
    "You are a senior software engineer with deep expertise in code analysis. "
    "You have been given code samples retrieved from the repository. "
    "Use them to answer the question thoroughly. "
    "Make reasonable inferences from file names, imports, class names, and patterns you see. "
    "Do NOT say 'the context does not contain enough information' — synthesize what you can "
    "and supplement with your knowledge of the frameworks and patterns visible in the code.\n\n"
    "IMPORTANT: At the end of your answer, add a '**Sources:**' section listing the specific "
    "file paths, class names, or function names from the retrieved context that you referenced. "
    "If no specific source is identifiable from the context, write 'Sources: inferred from codebase patterns'.\n\n"
    "Retrieved code context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)

KB_DOC_PROMPT = (
    "You are a precise document retrieval assistant for an internal knowledge base of "
    "official specifications, release notes, and policy documents.\n"
    "You have been given exact excerpts retrieved from these documents. "
    "Your job is to answer the question using ONLY the information present in the retrieved "
    "context below. Treat the context as the single source of truth.\n\n"
    "## STRICT GROUNDING RULES (these override any general helpfulness instinct)\n"
    "1. Use ONLY content that appears verbatim in the retrieved context. Do not supplement "
    "with training data, general knowledge, or industry conventions.\n"
    "2. Do NOT invent, synthesize, or infer field values, codes, table rows, "
    "prerequisites, steps, or section content that is not explicitly shown in the context.\n"
    "3. Do NOT expand abbreviations or acronyms unless the expansion appears verbatim in the "
    "retrieved context. For example: if the context says 'PR environment' and never defines "
    "PR, do NOT guess 'Pre-Release' or 'Pre-Production' — write 'PR' exactly as shown.\n"
    "4. Do NOT blend content across sections. If the user asks about one section "
    "(e.g. 'Prerequisites', 'Release Summary'), return only that section's content. "
    "Do not pull bullet points from 'Installation Procedure' or 'Checklist' into a "
    "'Prerequisites' answer.\n"
    "5. Do NOT invent sub-headings, categories, or groupings that are not in the source. "
    "If the source lists 4 bullet items, return 4 bullet items — do not regroup them under "
    "fabricated headings like 'Key Requirements' or 'Technical Impact'.\n"
    "6. Do NOT add interpretive narrative ('This ensures...', 'These represent...', "
    "'This is a targeted enhancement...'). Stop after the last verbatim point.\n"
    "7. Reproduce tables, field lists, and data exactly as they appear in the context — "
    "do not reformat into prose, reorder rows, or drop columns.\n"
    "8. Quote the exact values shown in the context for IDs, version numbers, dates, names. "
    "Never substitute a value from a document title or filename for a value from a table.\n"
    "9. EXCEPTION — SAMPLE / EXAMPLE / TEMPLATE REQUESTS\n\n"
    "When a user explicitly requests a sample, example, template, or format with sample "
    "values, and the available context contains a field definition, schema, table structure, "
    "message specification, API contract, or similar structured metadata (for example: field "
    "names, tags, data types, validation rules, mandatory/optional indicators, or enumerated "
    "values), a sample may be constructed using the following guidelines:\n\n"
    "   a) Use every field name, attribute, or tag exactly as defined in the available "
    "      context. Do not rename, remove, or alter them.\n\n"
    "   b) If the context provides an example value for a field, use that exact value.\n\n"
    "   c) For fields without example values, use clearly identifiable placeholder values "
    "      that align with the documented data type or format (for example: numeric field "
    "      → 123456, date field → YYYYMMDD, text field → SAMPLE_VALUE, boolean/flag field "
    "      → the first valid value defined in the context). Fields must not be left blank.\n\n"
    "   d) Preserve field requirements as defined in the context. Include all "
    "      Mandatory/Required fields. Optional fields may be included and clearly marked "
    "      as optional where appropriate.\n\n"
    "   e) Clearly label the generated output as:\n"
    "      'Sample (constructed from field definitions available in the retrieved context)'\n\n"
    "   f) This exception applies only when sufficient field definitions or structural "
    "      metadata are available in the context. If no such definitions exist, standard "
    "      response rules apply.\n\n"
    "   g) Any generated values that are not explicitly provided in the source context must "
    "      be treated as illustrative placeholders and must not be represented as actual "
    "      production, customer, business, or transaction data.\n\n"
    "## VERSION HANDLING\n"
    "- If the user asks about a specific version (e.g. 'v1.0.40') and the context does NOT "
    "contain that version, reply: 'The retrieved document excerpts do not contain "
    "information for version <X>. The closest available version in context is <Y>.'\n"
    "- Do NOT infer or extrapolate the content of one version from another.\n\n"
    "## WHEN INFORMATION IS MISSING\n"
    "If the retrieved context does not contain the exact information requested, say: "
    "'The retrieved document excerpts do not contain this specific information. "
    "Try rephrasing your query or ask about a specific section number.'\n\n"
    "## CITATIONS\n"
    "End your answer with a 'Sources:' line listing ONLY the sections you literally quoted "
    "from. Do not list sections you did not use. If the context shows a section_path "
    "breadcrumb (e.g. '... > Prerequisites'), cite the last segment.\n\n"
    "Retrieved document context:\n{context}\n\nQuestion: {question}\n\nAnswer:"
)

# Used when project-scoped question arrives but RAG returns 0 chunks
PROJECT_NO_CONTEXT_PROMPT = (
    "You are a helpful AI assistant for the '{repo}' project.\n\n"
    "The codebase has not been indexed yet (or no relevant code was found for this question), "
    "so you don't have access to the actual source files right now.\n\n"
    "However, based on the project metadata below and your general knowledge of software systems, "
    "answer the question as helpfully as possible. Be clear about what you know vs. what you're inferring.\n\n"
    "Project context (from metadata):\n{question}\n\n"
    "Instructions:\n"
    "- Answer what you can from the project name, description, and question context\n"
    "- If the question requires the actual source code, say: 'The codebase is not yet indexed — "
    "once indexing completes, I can give you a detailed answer from the actual source files'\n"
    "- Do NOT say 'I cannot provide information' — always give a best-effort answer\n\n"
    "Answer:"
)

# Used when the question is an overview/summary of the whole repo
CODEBASE_OVERVIEW_PROMPT = (
    "You are a senior software architect doing a codebase review.\n"
    "Below are code samples from the '{repo}' repository.\n\n"
    "Based on these samples, produce a structured overview covering:\n"
    "1. **Purpose** — what this application/service does\n"
    "2. **Tech Stack** — languages, frameworks, libraries you see\n"
    "3. **Key Components** — main modules, services, classes (name them specifically)\n"
    "4. **Architecture** — how the pieces fit together\n"
    "5. **Entry Points** — where execution starts (main files, routes, controllers)\n\n"
    "Be specific — reference actual file paths, class names, and function names from the context. "
    "Synthesize confidently from what you see.\n\n"
    "Code samples:\n{context}\n\n"
    "Codebase overview:"
)

_OVERVIEW_KEYWORDS = {
    # Whole-codebase intent only — NOT specific-entity questions.
    # Removed: "what is", "what does", "how does", "project", "codebase", "repo",
    # "repository", "about the", "purpose" — these match class/file questions and
    # also appear in the [Project:][Codebase:] prefix, causing false positives.
    "overview", "describe", "summarize", "summarise", "tell me about",
    "walk me through", "architecture", "code base",
    "this codebase", "this repository", "this repo", "entire codebase",
    "whole codebase", "high level", "high-level",
}

from agents.compliance_engine import compliance_engine

from tools.n8n_tool import n8n_tool

from tools.n8n_autonomous_builder import n8n_autonomous_tool

# ============================================================
# RETRIEVE TOOL
# ============================================================

def retrieve_tool(state):

    try:

        logger.info("TOOL retrieve_tool → start")

        try:
            from models.classifier import classify_query_complexity
            _complexity = classify_query_complexity(state.question)
        except Exception:
            _complexity = "simple"

        # Use the bare user question for embedding — the full state.question carries
        # [Project:][Codebase:] prefix + injected history which drowns the semantic
        # signal and produces irrelevant retrieval results.
        _retrieval_q = getattr(state, 'raw_question', None) or state.question
        context = hybrid_retrieve_context(
            _retrieval_q,
            state.repo_filter,
            user_ctx=state.user_ctx,
            complexity=_complexity,
        )

        # Ensure list safety
        if not context:
            state.context = []
        else:
            state.context = [
                chunk for chunk in context
                if chunk and isinstance(chunk, str) and chunk.strip()
            ]

        logger.info(
            f"TOOL retrieve_tool → loaded {len(state.context)} clean chunks"
        )

        return state

    except Exception as e:

        logger.error(f"retrieve_tool failed: {e}")

        state.context = []

        return state


# ============================================================
# LOCAL LLM TOOL
# ============================================================

def local_llm_tool(state):

    try:

        logger.info("TOOL local_llm_tool → enabled")

        state.use_local_llm = True

        return state

    except Exception as e:

        logger.error(f"local_llm_tool failed: {e}")

        return state




# ============================================================
# COMPLIANCE TOOL (AiNxt PCI ENFORCEMENT)
# ============================================================

def compliance_tool(state):

    try:

        logger.info("TOOL compliance_tool → start")

        findings = []

        # Check question
        if state.question:
            findings.extend(
                compliance_engine.analyze(state.question)
            )

        # Check context safely
        if state.context:
            for chunk in state.context:

                if chunk and isinstance(chunk, str):

                    findings.extend(
                        compliance_engine.analyze(chunk)
                    )

        state.compliance_flags = findings or []

        if findings:

            logger.critical(
                f"PCI VIOLATION DETECTED → count={len(findings)} "
                f"blocked={any(f.get('blocked', False) for f in findings)}"
            )

        else:

            logger.info("TOOL compliance_tool → clean")

        return state

    except Exception as e:

        logger.error(f"compliance_tool failed: {e}")

        state.compliance_flags = []

        return state


# ============================================================
# GENERATE ANSWER TOOL (FINAL ENTERPRISE VERSION)
# CRITICAL FIX: Proper context detection
# ============================================================

def generate_answer_tool(state, llm) -> Generator[str, None, None]:

    try:

        # ====================================================
        # PCI BLOCK CHECK
        # ====================================================

        if state.compliance_flags:

            if compliance_engine.should_block(state.compliance_flags):

                logger.critical(
                    "GENERATION BLOCKED → PCI violation"
                )

                yield "Response blocked due to PCI compliance violation"

                return


        # ====================================================
        # CLEAN CONTEXT (CRITICAL FIX)
        # ====================================================

        clean_context: List[str] = []

        if state.context:

            for chunk in state.context:

                if chunk and isinstance(chunk, str):

                    stripped = chunk.strip()

                    if stripped:
                        clean_context.append(stripped)


        has_context = len(clean_context) > 0


        # ====================================================
        # DOMAIN DETECTION — decides prompt and model tier
        # CODE domain  → CODE_PROMPT + context → GPT/Claude
        # GENERAL domain → GENERAL_PROMPT (ignore any stale context)
        #                  → Ollama (free, private, fast)
        # ====================================================

        try:
            from models.classifier import detect_query_domain
            _domain = detect_query_domain(state.question)
        except Exception:
            _domain = "code"   # safe default

        # The bare current turn — NOT state.question, which carries the flat
        # "User:/Assistant:" transcript gateway.py builds. Embedding that
        # transcript in the prompt double-sends history (the real turns already
        # ride structurally via state.messages / _prior below), and GPT-family
        # models then follow the literal in-message scaffolding and misread
        # follow-up/meta questions ("is this response correct?"). Used for both
        # intent detection and the prompt body.
        _raw_q = getattr(state, "raw_question", None) or state.question

        # ── Office assistant mode (Cowork scheduled tasks) ──────────────────────
        # When the agent runs in mode="office", bypass domain-based prompt
        # selection entirely and use OFFICE_PROMPT, which explicitly instructs
        # the model to produce email/message content rather than refusing.
        # This prevents the model's safety training from overriding the task
        # intent when the framed_question persona is not carried into the prompt.
        if getattr(state, "mode", None) == "office":
            _ctx_block = (
                f"\nCONTEXT (from connected apps / knowledge base):\n"
                + "\n\n".join(clean_context)
                + "\n\n"
            ) if has_context else ""
            prompt = OFFICE_PROMPT.format(context_block=_ctx_block, question=_raw_q)
            logger.info(
                f"TOOL generate_answer_tool → OFFICE_PROMPT "
                f"(mode=office, context_chunks={len(clean_context)})"
            )
        elif has_context:

            context_text = "\n\n".join(clean_context)
            # No truncation — send full context to LLM so spec tables and
            # document data are never silently cut mid-row.
            _q_lower = _raw_q.lower()

            # Detect codebase-overview questions so we can use a synthesis-first prompt.
            # Only triggers for explicit whole-codebase requests ("walk me through this
            # codebase", "overview", "architecture") — NOT specific-entity questions
            # like "What does BaseChannel do?" or "How does PaymentUtils work?".
            _is_overview = any(kw in _q_lower for kw in _OVERVIEW_KEYWORDS)
            _repo = getattr(state, "repo_filter", None) or ""

            if _is_overview and _repo:
                logger.info(
                    f"TOOL generate_answer_tool → CODEBASE_OVERVIEW_PROMPT "
                    f"(chunks={len(clean_context)}, repo={_repo})"
                )
                prompt = CODEBASE_OVERVIEW_PROMPT.format(
                    context=context_text,
                    repo=_repo,
                )

            elif _domain == "code":
                logger.info(
                    f"TOOL generate_answer_tool → CODE_PROMPT "
                    f"(chunks={len(clean_context)}, domain=code)"
                )
                prompt = CODE_PROMPT.format(
                    context=context_text,
                    question=_raw_q
                )

            else:
                logger.info(
                    f"TOOL generate_answer_tool → GROUNDED_PROMPT "
                    f"(chunks={len(clean_context)}, domain={_domain})"
                )
                prompt = GROUNDED_PROMPT.format(
                    context=context_text,
                    question=_raw_q
                )

        else:

            _repo = getattr(state, "repo_filter", None) or ""
            _q_lower = _raw_q.lower()
            _is_overview = any(kw in _q_lower for kw in _OVERVIEW_KEYWORDS)
            _has_project_ctx = "[Project:" in state.question or "[Codebase:" in state.question

            if _repo and (_is_overview or _has_project_ctx):
                logger.info(
                    f"TOOL generate_answer_tool → PROJECT_NO_CONTEXT_PROMPT (no vectors, repo={_repo})"
                )
                prompt = PROJECT_NO_CONTEXT_PROMPT.format(
                    repo=_repo,
                    question=_raw_q,
                )
            else:
                logger.info(
                    "TOOL generate_answer_tool → GENERAL_PROMPT (no context)"
                )
                prompt = GENERAL_PROMPT.format(
                    question=_raw_q
                )


        # ====================================================
        # NEURON ESCALATION SUPPORT
        # ====================================================

        # ── Point C: Eval context relevance (true fire-and-forget via thread) ──
        if has_context:
            import threading as _threading
            _q_c, _ctx_c, _sid_c = state.question, list(clean_context), getattr(state, "session_id", None)
            def _run_eval_c():
                try:
                    from core.evals import eval_engine
                    eval_engine.eval_retrieval_quality(_q_c, _ctx_c, session_id=_sid_c)
                except Exception:
                    pass
            _threading.Thread(target=_run_eval_c, daemon=True).start()

        if getattr(state, "use_local_llm", False):

            logger.info(
                "TOOL generate_answer_tool → using Local LLM"
            )

            stream = _get_local_gateway().stream(prompt)

            for chunk in stream:

                if chunk is None:
                    continue

                if isinstance(chunk, dict):
                    if chunk.get("done", False):
                        logger.info("GENERATION COMPLETE")
                        return
                    token = chunk.get("response")
                    if token:
                        yield token
                    continue

                done = getattr(chunk, "done", False)
                if done:
                    logger.info("GENERATION COMPLETE")
                    return

                token = getattr(chunk, "response", None)
                if token:
                    yield token

        else:

            logger.info(
                "TOOL generate_answer_tool → using model_router"
            )

            from models.model_router import model_router
            from models.classifier import classify_query_complexity

            _complexity = classify_query_complexity(state.question)

            # Routing decision:
            #   simple / general (no context, no code) → Local LLM (tier=simple)
            #   code / with context                    → GPT-5.2  (tier=medium)
            #   complex reasoning                      → Claude Sonnet (tier=complex)
            if has_context and _complexity == "simple":
                _complexity = "medium"

            import re as _re
            _CODE_RE = _re.compile(
                r"\b(program|code|script|function|class|algorithm|implement|write|create|build|develop"
                r"|debug|fix|error|exception|syntax|loop|array|list|dict|string|integer|float"
                r"|python|java|javascript|typescript|golang|rust|sql|bash|shell|html|css"
                r"|fibonacci|sort|search|recursion|api|http|json|xml|regex|parse|compile"
                r"|test|unittest|pytest|async|thread|concurrent|database|query)\b",
                _re.IGNORECASE,
            )
            _is_code_question = bool(_CODE_RE.search(state.question))
            if _is_code_question and _complexity == "simple":
                _complexity = "medium"

            # General chat with no codebase scope: downgrade to a fast local tier
            # (no cloud egress). Heavy models (complex/solution) are only justified
            # when there is retrieved codebase context to reason over.
            _repo_filter = getattr(state, "repo_filter", None)
            if not _repo_filter and not has_context and _complexity in ("complex", "deep", "solution"):
                # Configurable: set DOWNGRADE_MODEL to any model id you want to
                # cap to when there is no codebase context. Must be set in env.
                _complexity = os.getenv("DOWNGRADE_MODEL", "")
                logger.info(
                    "generate_answer_tool: no repo/context → downgraded to %s "
                    "(set DOWNGRADE_MODEL to change)", _complexity
                )

            # Route: Local LLM (simple/general) → GPT-5.2 (medium/code) → Claude (complex)
            # Honour explicit model_hint from the request; fall back to complexity.
            _hint = getattr(state, "model_hint", None) or _complexity

            # Build proper multi-turn messages list when conversation history exists.
            # This ensures local and cloud models both get real conversation turns
            # instead of a flat history-embedded string — critical for model switching.
            _state_messages = getattr(state, "messages", [])
            if len(_state_messages) > 1:
                # prior turns: everything except the last user message (which becomes `prompt`)
                _prior = _state_messages[:-1]
                _stream_payload = [*_prior, {"role": "user", "content": prompt}]
            else:
                _stream_payload = prompt

            token_yielded = False
            _answer_buf: List[str] = []
            for _tok in model_router.stream(_stream_payload, model_hint=_hint):
                # Skip dict sentinel (see model_router.stream docstring) —
                # only string tokens are appended to the answer buffer or
                # yielded downstream to the SSE consumer.
                if isinstance(_tok, dict):
                    continue
                if _tok:
                    token_yielded = True
                    _answer_buf.append(_tok)
                    yield _tok
            logger.info("GENERATION COMPLETE")
            # ── Point D: Eval answer quality (true fire-and-forget via thread) ──
            if _answer_buf:
                import threading as _threading
                _q_d = state.question
                _ans_d = "".join(_answer_buf)
                _ctx_d = list(clean_context)
                _sid_d = getattr(state, "session_id", None)
                # Snapshot model label before the thread starts to avoid
                # a race where the eval thread's own model_router call
                # overwrites last_model_label on the shared object.
                _model_d = getattr(model_router, "last_model_label", None) or None
                def _run_eval_d():
                    try:
                        from core.evals import eval_engine
                        eval_engine.eval_answer_quality(
                            _q_d, _ans_d, _ctx_d, session_id=_sid_d,
                            platform="agent_studio",
                            model=_model_d,
                        )
                    except Exception:
                        pass
                _threading.Thread(target=_run_eval_d, daemon=True).start()
            if not token_yielded:
                logger.warning(
                    "TOOL generate_answer_tool → model_router.stream() returned empty"
                )


    except Exception as e:

        logger.error(f"generate_answer_tool failed: {e}")

        yield "\nError generating response"


# ============================================================
# CONTEXT EVALUATION TOOL
# ============================================================

def evaluate_context_tool(state, llm):

    try:

        if not state.context:

            state.confidence = 0.0

            return state


        sample = [
            chunk for chunk in state.context
            if chunk and isinstance(chunk, str)
        ][:2]


        prompt = f"""
Evaluate whether the context is sufficient.

Return ONLY number between 0 and 1.

Question:
{state.question}

Context:
{sample}
"""


        from models.model_router import model_router

        text = model_router.generate(prompt).strip()


        try:
            confidence = float(text)
        except:
            confidence = 0.5


        state.confidence = confidence


        logger.info(
            f"TOOL evaluate_context_tool → confidence={confidence}"
        )


        return state


    except Exception as e:

        logger.error(f"evaluate_context_tool failed: {e}")

        state.confidence = 0.0

        return state