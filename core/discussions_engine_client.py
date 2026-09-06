# SPDX-License-Identifier: MIT
"""
Server-to-server client for services/discussions_engine (vendored, headless
Apache Answer — see docs/DISCUSSIONS_MODULE_IMPLEMENTATION_PLAN.md).

The browser never talks to this engine. Only this module does, on behalf of
whichever AiNxt user is making a Discussions request through the gateway.
Uses the same signed-assertion mechanism as before (core/discussions_assertion.py,
unchanged), just invoked here directly instead of via a browser redirect chain.

Per-user engine session tokens are cached in Redis (db=2, "transient" per
CLAUDE.md's Redis DB allocation — same class of data as agent run history) so
we don't mint a fresh token on every single write.
"""
import mimetypes
import os

import httpx

from core.config import (
    DISCUSSIONS_ENGINE_BASE_URL,
    DISCUSSIONS_ENGINE_UPLOAD_PATH,
    ENABLE_DISCUSSIONS,
    redis_client,
)
from core.discussions_assertion import ASSERTION_HEADER, make_assertion
from core.logger import logger

# Same-host default (http://127.0.0.1:8010) when the caller and the engine
# are co-located (gateway server). services/discussions_svc's worker calls
# from a different host (app04) — DISCUSSIONS_ENGINE_BASE_URL must be set
# there to the gateway server's address; see core/config.py for the full
# reasoning.
# The engine's compiled-in config (services/discussions_engine/vendor/answer-src/
# configs/config.yaml -> ui.base_url/api_base_url) mounts EVERY route — API and
# uploads alike — under /discussions, not at root. There's no env var for this
# (install_from_env.go only maps DB/site/admin fields), so every direct call to
# the engine needs this prefix or it silently falls through to the SPA's
# NoRoute placeholder instead of hitting the real handler. Confirmed live
# 2026-07-13: bare /answer/api/v1/badges returned the placeholder HTML comment;
# /discussions/answer/api/v1/badges returned real data.
ENGINE_API_PREFIX = "/discussions"
ENGINE_BASE_URL = DISCUSSIONS_ENGINE_BASE_URL
TOKEN_CACHE_DB = 2
TOKEN_CACHE_TTL = 3300  # under the engine's own token lifetime; refreshed on demand




def _require_enabled():
    if not ENABLE_DISCUSSIONS:
        raise RuntimeError("discussions_engine_client: ENABLE_DISCUSSIONS is false")


async def get_or_mint_token(user_claims: dict, force: bool = False) -> str:
    """Return this AiNxt user's Apache Answer engine session token, minting a
    new one via the assertion flow if there's no cached (or expired) one.

    force=True skips the cache read and always mints fresh — used by
    _authed_request()/upload_file() to recover from a stale cached token
    (e.g. the engine process restarted and lost its own session store, so a
    token we cached under the old TTL is now rejected with 401 even though
    it hasn't expired on our side)."""
    _require_enabled()
    sub = user_claims.get("sub")
    login_url = f"{ENGINE_API_PREFIX}/answer/api/v1/user-center/login/callback"
    logger.info(
        f"discussions_engine_client.get_or_mint_token: start sub={sub!r} force={force} "
        f"engine_base_url={ENGINE_BASE_URL!r} api_prefix={ENGINE_API_PREFIX!r}"
    )

    try:
        r = redis_client(TOKEN_CACHE_DB, decode_responses=True)
        cache_key = f"discussions:engine_token:{user_claims['sub']}"
        if not force:
            cached = r.get(cache_key)
            if cached:
                logger.info(
                    f"discussions_engine_client.get_or_mint_token: cache HIT sub={sub!r} "
                    f"(token len={len(cached)})"
                )
                return cached
            logger.info(f"discussions_engine_client.get_or_mint_token: cache MISS sub={sub!r}")
    except Exception as e:
        # Redis is a cache, not the source of truth — log and fall through to
        # minting fresh so a Redis blip doesn't take Discussions down.
        logger.warning(
            f"discussions_engine_client.get_or_mint_token: Redis cache unavailable "
            f"(db={TOKEN_CACHE_DB}) sub={sub!r}: {type(e).__name__}: {e} — minting fresh"
        )
        r = None
        cache_key = None

    assertion = make_assertion(user_claims)
    logger.info(
        f"discussions_engine_client.get_or_mint_token: minting via GET {ENGINE_BASE_URL}{login_url} "
        f"sub={sub!r} assertion_len={len(assertion)}"
    )
    try:
        async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
            resp = await client.get(
                login_url,
                headers={ASSERTION_HEADER: assertion},
                follow_redirects=False,
            )
    except httpx.HTTPError as e:
        # Connection refused / DNS / timeout — the engine is unreachable at
        # ENGINE_BASE_URL. This is the "wrong DISCUSSIONS_ENGINE_BASE_URL or
        # engine not running" case, distinct from an auth rejection below.
        logger.error(
            f"discussions_engine_client.get_or_mint_token: CANNOT REACH engine at "
            f"{ENGINE_BASE_URL}{login_url} sub={sub!r}: {type(e).__name__}: {e}"
        )
        raise RuntimeError("discussions engine login failed") from e

    # The engine 302s to /users/auth-landing?access_token=... on success.
    # There's no browser here to follow that redirect (and the landing
    # page is a frontend route that doesn't exist in this headless build
    # anyway) — extract the token straight from the Location header.
    location = resp.headers.get("location", "")
    content_type = resp.headers.get("content-type", "")
    token = location.split("access_token=")[-1] if "access_token=" in location else None
    logger.info(
        f"discussions_engine_client.get_or_mint_token: callback responded sub={sub!r} "
        f"status={resp.status_code} content_type={content_type!r} location={location!r} "
        f"token_found={token is not None}"
    )
    if not token:
        # Show a short snippet of the body so we can tell an SPA 'NoRoute'
        # placeholder (engine reachable but bridge route/plugin missing) apart
        # from a real auth-rejection error page (secret mismatch / JIT-provision
        # failure).
        body_snippet = (resp.text or "")[:500].replace("\n", " ")
        logger.error(
            f"discussions_engine_client.get_or_mint_token: LOGIN FAILED sub={sub!r} "
            f"status={resp.status_code} content_type={content_type!r} location={location!r} "
            f"body[:500]={body_snippet!r}"
        )
        raise RuntimeError("discussions engine login failed")

    if r is not None and cache_key is not None:
        try:
            r.set(cache_key, token, ex=TOKEN_CACHE_TTL)
        except Exception as e:
            logger.warning(
                f"discussions_engine_client.get_or_mint_token: could not cache token "
                f"sub={sub!r}: {type(e).__name__}: {e}"
            )
    logger.info(
        f"discussions_engine_client.get_or_mint_token: SUCCESS sub={sub!r} token_len={len(token)}"
    )
    return token


async def _authed_request(user_claims: dict, method: str, path: str, **kwargs) -> dict:
    sub = user_claims.get("sub")
    logger.info(
        f"discussions_engine_client._authed_request: {method} {ENGINE_BASE_URL}{path} sub={sub!r}"
    )
    token = await get_or_mint_token(user_claims)
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=15.0) as client:
        resp = await client.request(method, path, headers={"Authorization": token}, **kwargs)
        logger.info(
            f"discussions_engine_client._authed_request: {method} {path} sub={sub!r} "
            f"-> status={resp.status_code}"
        )
        if resp.status_code == 401:
            # Cached token stale relative to the engine's own session store
            # (restart, cache clear) — mint fresh and retry once before
            # surfacing an error.
            logger.info(
                f"discussions_engine_client._authed_request: 401 for {method} {path} sub={sub!r} "
                f"— re-minting token and retrying once"
            )
            token = await get_or_mint_token(user_claims, force=True)
            resp = await client.request(method, path, headers={"Authorization": token}, **kwargs)
            logger.info(
                f"discussions_engine_client._authed_request: retry {method} {path} sub={sub!r} "
                f"-> status={resp.status_code}"
            )
        if resp.status_code >= 400:
            body_snippet = (resp.text or "")[:500].replace("\n", " ")
            logger.error(
                f"discussions_engine_client._authed_request: {method} {path} sub={sub!r} "
                f"FAILED status={resp.status_code} body[:500]={body_snippet!r}"
            )
        resp.raise_for_status()
        return resp.json()


async def get_own_username(user_claims: dict) -> str:
    """The engine assigns each account its own username at JIT-provision
    time — it is NOT the deterministic email-local-part `assertionToUserInfo`
    requests (confirmed live: Answer falls back to a random hex username on
    a collision, e.g. "admin" already taken by another seeded account), so
    endpoints keyed by username (like badge/user/awards) need this real
    lookup rather than recomputing a guess."""
    data = await _authed_request(user_claims, "GET", f"{ENGINE_API_PREFIX}/answer/api/v1/user/info")
    return ((data or {}).get("data") or {}).get("username") or ""


async def create_question(user_claims: dict, title: str, content: str, tags: list[str]) -> dict:
    return await _authed_request(
        user_claims, "POST", f"{ENGINE_API_PREFIX}/answer/api/v1/question",
        json={"title": title, "content": content, "html": "", "tags": [{"slug_name": t} for t in tags]},
    )


async def create_answer(user_claims: dict, question_id: str, content: str) -> dict:
    return await _authed_request(
        user_claims, "POST", f"{ENGINE_API_PREFIX}/answer/api/v1/answer",
        json={"question_id": question_id, "content": content},
    )


async def cast_vote(user_claims: dict, target_type: str, target_id: str, direction: int) -> dict:
    # One generic endpoint for both questions and answers — the engine looks
    # up object_id across both (internal/router/answer_api_router.go:248-249,
    # internal/schema/vote_schema.go::VoteReq — a single object_id field, no
    # separate question/answer variant). target_type is accepted here only so
    # the AiNxt-side mirror write (routers/discussions_router.py) knows what
    # it wrote — it isn't sent to the engine.
    endpoint = f"{ENGINE_API_PREFIX}/answer/api/v1/vote/up" if direction > 0 else f"{ENGINE_API_PREFIX}/answer/api/v1/vote/down"
    return await _authed_request(user_claims, "POST", endpoint, json={"object_id": target_id, "is_cancel": False})


async def accept_answer(user_claims: dict, question_id: str, answer_id: str) -> dict:
    return await _authed_request(
        user_claims, "POST", f"{ENGINE_API_PREFIX}/answer/api/v1/answer/acceptance",
        json={"question_id": question_id, "answer_id": answer_id},
    )


async def delete_question(user_claims: dict, question_id: str) -> dict:
    # Apache Answer's delete routes are DELETE with the object id in the JSON
    # body (mirrors internal/router/answer_api_router.go's question/answer/
    # comment DELETE handlers). question_id / answer_id here are the ENGINE's
    # own ids (our DiscussionsQuestion.external_id), not AiNxt uuids.
    #
    # WORKAROUND for Apache Answer's soft-delete: DELETE only marks the row
    # `status=deleted`, it doesn't purge — the engine's title/content dedupe
    # (error.question.title.repeat / question_service.go::AddQuestionCheckTags)
    # still sees the row and rejects the user's *next* post of the same title
    # as a "duplicate submission". Pre-clear the title/content via PUT so the
    # dedupe hash no longer collides, THEN issue the soft-delete. The PUT is
    # best-effort — if it fails the delete still proceeds (better a stuck
    # dedupe than a stuck row).
    try:
        await _authed_request(
            user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/question",
            json={
                "id": question_id,
                "title": f"[deleted-{question_id}]",
                "content": "[deleted]",
                "html": "",
                "tags": [{"slug_name": "deleted"}],
                "edit_summary": "pre-delete title/content clear",
            },
        )
    except Exception as e:
        logger.warning(
            f"discussions_engine_client.delete_question: pre-delete clear failed for "
            f"question_id={question_id!r}: {type(e).__name__}: {e} — proceeding with DELETE"
        )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/question",
        json={"id": question_id},
    )


async def delete_answer(user_claims: dict, question_id: str, answer_id: str) -> dict:
    # Same soft-delete workaround as delete_question: Answer's DELETE only
    # marks status=deleted, and its answer_service.go::AddAnswerCheckContent
    # dedupes on content — re-posting the same reply body triggers "duplicate
    # submission". Pre-clear the content via PUT, then soft-delete. question_id
    # is the parent question's engine external_id, required by PUT /answer.
    try:
        await _authed_request(
            user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/answer",
            json={
                "id": answer_id,
                "question_id": question_id,
                "content": "[deleted]",
                "html": "",
                "edit_summary": "pre-delete content clear",
            },
        )
    except Exception as e:
        logger.warning(
            f"discussions_engine_client.delete_answer: pre-delete clear failed for "
            f"answer_id={answer_id!r}: {type(e).__name__}: {e} — proceeding with DELETE"
        )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/answer",
        json={"id": answer_id},
    )


async def delete_comment(user_claims: dict, comment_id: str) -> dict:
    # Comment delete keys on comment_id (same field name POST /comment returns
    # and add_comment() reads back as data.comment_id).
    #
    # Same soft-delete workaround: comment_service.go::AddCommentCheckContent
    # runs a per-object dedupe on original_text; re-adding an identical comment
    # under the same question/answer trips "duplicate submission". Pre-clear
    # via PUT /comment, then soft-delete.
    try:
        await _authed_request(
            user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/comment",
            json={"comment_id": comment_id, "original_text": "[deleted]"},
        )
    except Exception as e:
        logger.warning(
            f"discussions_engine_client.delete_comment: pre-delete clear failed for "
            f"comment_id={comment_id!r}: {type(e).__name__}: {e} — proceeding with DELETE"
        )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/comment",
        json={"comment_id": comment_id},
    )


# ── Admin token helper ───────────────────────────────────────────────────────
# Hard-delete endpoints require admin/moderator role on the engine side.
# We use the engine's own admin account (set during init) to perform these
# operations regardless of which AiNxt user triggered the delete.




# ── Hard (permanent) deletes ──────────────────────────────────────────────
# These call the new /permanent endpoints added to Apache Answer
# (internal/controller/*_controller.go HardDelete* handlers, registered at
# DELETE /answer/api/v1/{question,answer,comment}/permanent in
# internal/router/answer_api_router.go).
#
# Unlike the soft-delete helpers above, these:
#   • Physically remove the row and ALL related data in a single DB transaction
#     (answers, comments, revisions, tag-relations, activities, notifications).
#   • Clear the engine's duplicate-submission cache entry so the same title/
#     content can be re-posted immediately without hitting "duplicate submission".
#   • Are admin/moderator-only on the engine side — the caller (router) must
#     gate on role before invoking these.
#
# No pre-delete content-clear workaround is needed here because the row is
# gone entirely after the call — there is nothing left for the dedupe to match.

async def hard_delete_question(user_claims: dict, question_id: str) -> dict:
    """Permanently remove a question and all its related data from the engine.

    Cascades (all in one DB transaction on the engine side):
      question → answers → comments on question & answers → revisions →
      tag_rel → activity → notification rows.

    Any authenticated user can call this — the engine's /permanent endpoints
    have no rank/admin restriction (removed in the AiNxt build).

    question_id is the engine's own external id (DiscussionsQuestion.external_id),
    not the AiNxt UUID.
    """
    logger.info(
        f"discussions_engine_client.hard_delete_question: "
        f"sub={user_claims.get('sub')!r} question_id={question_id!r}"
    )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/question/permanent",
        json={"id": question_id},
    )


async def hard_delete_answer(user_claims: dict, answer_id: str) -> dict:
    """Permanently remove an answer and its related data from the engine.

    Cascades: answer → comments on the answer → revisions for the answer.

    Any authenticated user can call this — no rank/admin restriction.

    answer_id is the engine's own external id (DiscussionsAnswer.external_id),
    not the AiNxt UUID.
    """
    logger.info(
        f"discussions_engine_client.hard_delete_answer: "
        f"sub={user_claims.get('sub')!r} answer_id={answer_id!r}"
    )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/answer/permanent",
        json={"id": answer_id},
    )


async def hard_delete_comment(user_claims: dict, comment_id: str) -> dict:
    """Permanently remove a single comment from the engine.

    Any authenticated user can call this — no rank/admin restriction.

    comment_id is the engine's own external id (DiscussionsComment.external_id),
    not the AiNxt UUID.
    """
    logger.info(
        f"discussions_engine_client.hard_delete_comment: "
        f"sub={user_claims.get('sub')!r} comment_id={comment_id!r}"
    )
    return await _authed_request(
        user_claims, "DELETE", f"{ENGINE_API_PREFIX}/answer/api/v1/comment/permanent",
        json={"comment_id": comment_id},
    )


# ---- edits (edit_question / edit_answer / edit_comment) ----
# Apache Answer's edit routes are PUT with the object id in the JSON body
# (internal/router/answer_api_router.go PUT /question, /answer, /comment,
# schemas in internal/schema/question_schema.go::QuestionUpdate,
# answer_schema.go::AnswerUpdate, comment_schema.go::UpdateCommentReq).
# question_id/answer_id/comment_id here are the ENGINE's own ids
# (DiscussionsQuestion.external_id etc.), not AiNxt uuids. The engine enforces
# its own author check server-side; we still gate at the router before calling.

async def edit_question(user_claims: dict, question_id: str, title: str,
                         content: str, tags: list[str]) -> dict:
    return await _authed_request(
        user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/question",
        json={
            "id": question_id, "title": title, "content": content, "html": "",
            "tags": [{"slug_name": t} for t in tags], "edit_summary": "",
        },
    )


async def edit_answer(user_claims: dict, question_id: str, answer_id: str,
                       content: str) -> dict:
    return await _authed_request(
        user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/answer",
        json={
            "id": answer_id, "question_id": question_id, "content": content,
            "html": "", "edit_summary": "",
        },
    )


async def edit_comment(user_claims: dict, comment_id: str, content: str) -> dict:
    # Comment edit uses the same field name pattern as add_comment/delete_comment
    # (comment_id + original_text); engine treats original_text as the source of
    # truth and re-renders parsed_text server-side.
    return await _authed_request(
        user_claims, "PUT", f"{ENGINE_API_PREFIX}/answer/api/v1/comment",
        json={"comment_id": comment_id, "original_text": content},
    )


async def get_question_content(question_id: str) -> dict:
    """Read-only fetch, no per-user auth needed — Q&A content is public by
    design. Best-effort: empty dict on failure, callers must tolerate that."""
    try:
        async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
            resp = await client.get(f"{ENGINE_API_PREFIX}/answer/api/v1/question/info", params={"id": question_id})
            resp.raise_for_status()
            return resp.json().get("data") or {}
    except Exception as e:
        logger.warning(f"discussions_engine_client: get_question_content({question_id!r}) failed: {e}")
        return {}


# ---- directory reads (tags/badges/users) — public, no per-user auth needed ----
# Verified live against internal/router/answer_api_router.go: these routes sit
# in the "UnAuth" group, which still runs optional-auth middleware but never
# requires a token — safe to call anonymously for pure directory listings.

async def list_tags(page: int = 1, page_size: int = 30, query_cond: str = "popular") -> dict:
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(
            f"{ENGINE_API_PREFIX}/answer/api/v1/tags/page",
            params={"page": page, "page_size": page_size, "query_cond": query_cond},
        )
        resp.raise_for_status()
        return resp.json().get("data") or {}


async def list_badges() -> list:
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(f"{ENGINE_API_PREFIX}/answer/api/v1/badges")
        resp.raise_for_status()
        return resp.json().get("data") or []


async def user_ranking() -> dict:
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(f"{ENGINE_API_PREFIX}/answer/api/v1/user/ranking")
        resp.raise_for_status()
        return resp.json().get("data") or {}


async def user_badge_awards(username: str) -> list:
    """Badges the given Answer username has actually earned (badge_controller.go
    ::GetAllBadgeAwardListByUsername) — distinct from list_badges()'s global
    catalog, which has no notion of "mine"."""
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(f"{ENGINE_API_PREFIX}/answer/api/v1/badge/user/awards", params={"username": username})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        return data.get("list") or (data if isinstance(data, list) else [])


# ---- comments ----

async def list_comments(object_id: str, page: int = 1, page_size: int = 20) -> dict:
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(
            f"{ENGINE_API_PREFIX}/answer/api/v1/comment/page",
            params={"object_id": object_id, "page": page, "page_size": page_size, "query_cond": "created_at"},
        )
        resp.raise_for_status()
        return resp.json().get("data") or {}


async def add_comment(user_claims: dict, object_id: str, text: str) -> dict:
    return await _authed_request(
        user_claims, "POST", f"{ENGINE_API_PREFIX}/answer/api/v1/comment",
        json={"object_id": object_id, "original_text": text, "reply_comment_id": ""},
    )


# ---- file upload (markdown-embedded images) ----

async def upload_file(user_claims: dict, filename: str, content: bytes, content_type: str) -> str:
    """Returns a browser-reachable URL for the uploaded file.

    The engine's own response is an absolute URL rooted at ENGINE_BASE_URL
    (127.0.0.1:8010) — unreachable from any browser, since the whole point of
    this architecture is that nothing outside the gateway can reach the engine
    directly (confirmed live: the raw response 404s from a browser). Rewritten
    here to a gateway-relative path that routers/discussions_router.py's
    GET /discussions/uploads/{path} passes through to the same engine URL,
    server-to-server, exactly like every other engine call in this module.
    """
    token = await get_or_mint_token(user_claims)
    async with httpx.AsyncClient(base_url=ENGINE_BASE_URL, timeout=30.0) as client:
        resp = await client.post(
            f"{ENGINE_API_PREFIX}/answer/api/v1/file",
            headers={"Authorization": token},
            data={"source": "post"},
            files={"file": (filename, content, content_type)},
        )
        if resp.status_code == 401:
            token = await get_or_mint_token(user_claims, force=True)
            resp = await client.post(
                f"{ENGINE_API_PREFIX}/answer/api/v1/file",
                headers={"Authorization": token},
                data={"source": "post"},
                files={"file": (filename, content, content_type)},
            )
        resp.raise_for_status()
        engine_url = resp.json().get("data") or ""

    # Rewrite the engine's absolute URL ({ENGINE_BASE_URL}/...) into a
    # gateway-relative path routers/discussions_router.py's
    # GET /discussions/uploads/{path} can serve. Markdown-embedded <img> tags
    # are plain browser fetches — they never go through ai-ui's authFetch
    # (which prefixes API_BASE itself), so the URL handed back here must be a
    # fully gateway-routable path already.
    if engine_url.startswith(ENGINE_BASE_URL):
        path = engine_url[len(ENGINE_BASE_URL):]
        # The engine may return the upload path with or without its own
        # ENGINE_API_PREFIX ("/discussions"). The gateway only serves these
        # bytes at GET /discussions/uploads/{path} (routers/discussions_router.py),
        # so force the prefix in when the engine omitted it — otherwise the
        # browser fetches /ainxt/v1/api/uploads/... which matches no route (404,
        # broken <img>).
        if not path.startswith(ENGINE_API_PREFIX + "/"):
            path = ENGINE_API_PREFIX + path
        return "/ainxt/v1/api" + path
    return engine_url


def get_upload(path: str) -> tuple[bytes, str]:
    """Read an uploaded file's bytes off disk, for
    GET /discussions/uploads/{path} to stream back to the browser.

    The headless engine does NOT serve uploaded files over HTTP — its /uploads
    (and /discussions/uploads) routes fall through to the SPA's NoRoute handler
    and return an embedded placeholder HTML comment, never the image bytes
    (confirmed live 2026-07-14). The engine only *writes* the files, to its
    config.yaml `upload_path` (DISCUSSIONS_ENGINE_UPLOAD_PATH). So we read them
    straight from that directory. `path` is the URL tail after /uploads/, e.g.
    "post/5PCbryCA21d.png"."""
    base = os.path.realpath(DISCUSSIONS_ENGINE_UPLOAD_PATH)
    target = os.path.realpath(os.path.join(base, path))
    # Path-traversal guard: the resolved target must stay inside base.
    if target != base and not target.startswith(base + os.sep):
        raise FileNotFoundError(f"upload path escapes base: {path!r}")
    with open(target, "rb") as f:
        content = f.read()
    content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    return content, content_type
