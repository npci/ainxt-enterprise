# Codebase indexing setup

Codebase indexing (AST chunking + embedding for semantic code search and
SDLC pipeline context — `workers/index_worker.py`, the `index-worker`
container) is optional and off by default, run whenever you actually want
to index a repository:

```bash
bash ./start-index-worker.sh
```

`index-worker` needs the Knowledge Base's embedding service (`embed-svc`)
reachable — it raises "No embed service configured" on every job without
`EMBED_SVC_URL` set. `start-index-worker.sh` brings up `embed-svc` first
(delegating to [`./start-embed-service.sh`](KB_SETUP.md), a no-op if it's
already running) before starting `index-worker`, so you don't have to
sequence the two yourself. It fails loudly up front (before starting any
container) if `EMBED_SVC_URL` is still empty afterwards, instead of letting
`index-worker` crash-loop silently — it has no healthcheck defined.

If `embed-svc` isn't configured yet at all, run [`./kb-setup.sh`](KB_SETUP.md)
first — codebase indexing reuses the same embedding service as document
search, there's no separate model choice for it.

Once `index-worker` is running, submit a repository for indexing via the
Index Router (`POST /ainxt/v1/api/index/...` — see
[Codebase Indexing](workers/external_integration_workers_codebase_indexing.md)
for the full pipeline). Tail its logs with:

```bash
docker compose logs -f index-worker
```

## Manual / advanced

`index-worker` lives behind its own Docker Compose profile (`index`),
separate from the Knowledge Base's `embed` profile — turning on document
search does not implicitly also start codebase indexing, and vice versa.
Bring it up by hand (embed-svc must already be reachable):

```bash
docker compose --profile index up -d --build index-worker
```

## CodeWiki, for comparison

CodeWiki (AI-generated repo documentation, the `codewiki-worker` container)
is a separate, unrelated optional feature — it has no dependency on
`embed-svc` at all; it shells out to its own `codewiki` CLI against a
separate LLM endpoint (`CODEWIKI_BASE_URL`/`CODEWIKI_API_KEY` in `.env`).
It's gated by `./install.sh`'s `WITH_CODEWIKI` build flag (on by default;
`--without-codewiki` to skip) rather than a runtime script, and by its own
`codewiki` Compose profile:

```bash
docker compose --profile codewiki up -d codewiki-worker
```

`codewiki-worker` only does useful work if the image was actually built
with `WITH_CODEWIKI=1` — an image built with `--without-codewiki` doesn't
have the `codewiki` CLI installed, so a manually-started `codewiki-worker`
against that image will start but fail every job (no healthcheck catches
this at container-start time). Rebuild with CodeWiki included
(`./install.sh` without `--without-codewiki`, or
`WITH_CODEWIKI=1 docker compose --profile codewiki up -d --build codewiki-worker`)
if you hit this.
