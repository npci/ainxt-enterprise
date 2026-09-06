# Knowledge Base setup

The Knowledge Base (document parsing, embedding, and reranking for semantic
search / RAG) is optional and off by default. It runs in its own container
(`services/embed_svc`, the `embed-svc` service in `docker-compose.yml`) so its
ML dependencies stay isolated from the main app.

```bash
./kb-setup.sh
```

`./install.sh` offers to run this for you interactively at the end of a fresh
install. It's also a standalone, re-runnable script — say no then and run it
later at any time, with no fresh install needed. It asks which models to
use, writes them to `.env`, then delegates the actual build/start to
`./start-embed-service.sh` (builds `embed-svc` locally from the repo — there
is no published image to pull — starts `embed-svc` and `kb-worker`, pulls/
downloads the chosen models, and waits for `embed-svc` to report healthy),
and finally restarts the gateway so it picks up the new
`EMBED_SVC_URL`/`PARSE_SVC_URL`.

If you just want to (re)start `embed-svc` itself — it crashed, you rebuilt
the image, you're wiring up [codebase indexing](workers/external_integration_workers_codebase_indexing.md) — without
re-answering the model questionnaire, run `./start-embed-service.sh`
directly. It reads whatever's already in `.env` (`EMBED_PROVIDER`,
`USE_DOCLING_PARSER`, `EMBED_SVC_PORT`) rather than asking.

Leave `EMBED_SVC_URL` empty in `.env` to run without the Knowledge Base —
`/health` reports `degraded` and the Knowledge Base page shows a banner
pointing back at this script until it's run.

## Embedding model

Turns document text into vectors for search. `kb-setup.sh` offers:

| Choice | Model | Notes | `.env` vars it sets |
|---|---|---|---|
| Default | Ollama `nomic-embed-text` | Local, free | `OLLAMA_EMBED_MODEL` |
| Cloud | OpenAI `text-embedding-3-small` | Needs `OPENAI_API_KEY` | `OPENAI_EMBED_MODEL` |
| Custom | Any OpenAI-compatible `/v1/embeddings` endpoint | Needs URL + key | `NOMIC_EMBED_URL`, `NOMIC_EMBED_API_KEY` |

The custom option works with any endpoint implementing the OpenAI-compatible
embeddings shape — no code change needed for a new provider there.

## Reranker model

Re-scores search results for relevance. Unlike the embedding model, this is
**not** free-text-configurable: `services/embed_svc/reranker.py` checks
`RERANKER_VARIANT` against a hardcoded allow-list and silently falls back to
the default if the value isn't recognized. Supported variants:

| Variant | Model | Notes |
|---|---|---|
| `bge_large` | BAAI/bge-reranker-large | Best accuracy, ~560MB — **default** |
| `bge_base` | BAAI/bge-reranker-base | Lighter, ~279MB |
| `tinybert` | cross-encoder/ms-marco-TinyBERT-L-2-v2 | Smallest/fastest |
| `bge_v2_m3`, `jina_tiny`, `jina_v2`, `qwen_06b`, `gte_modernbert` | — | Also supported |

Adding a model outside this list requires a code change to the
`_VALID_MODELS` allow-list in `services/embed_svc/reranker.py`, not just a
setup-time choice.

## Parsing / OCR

Turns documents into structured text. Docling is the only parsing engine
currently wired into the code (`core/docling_parser.py`), with PaddleOCR as
its optional add-on for scanned/image-based pages — PaddleOCR has no
independent existence outside Docling's pipeline. Declining Docling falls
back to the legacy per-format parsers (markitdown/pdfplumber/BeautifulSoup),
which is the same behavior the app already has with Docling disabled.

| Setting | `.env` var | Notes |
|---|---|---|
| Docling parsing | `USE_DOCLING_PARSER` | `1` (default when KB is enabled) or `0` for legacy parsers |
| PaddleOCR add-on | `WITH_OCR` | Build arg read by `docker-compose.yml`; `1` to include it, `0` (default) uses the bundled rapidocr engine only |

## Changing your choices later

Run `./kb-setup.sh` again at any time — it's re-runnable and does not require
a fresh install. It writes to your existing `.env` and rebuilds/restarts only
the `embed-svc`, `kb-worker`, and `gateway` containers.

See [`knowledge/embedding_service.md`](knowledge/embedding_service.md) for
the service's architecture and API.
