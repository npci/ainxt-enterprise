# Runbook — SDLC pipeline test: CLI phases on Anthropic, in-process phases on OpenAI

Goal for this session:
1. Install & bring up the stack.
2. Run the SDLC pipeline in a **mixed** model setup — the `ainxt` **CLI phases stay
   on Anthropic** (Claude), and **every in-process LLM call runs on OpenAI**.
3. Trigger a run from JIRA and confirm each phase used the intended provider.
4. Produce a portable patch of the code changes to move to another laptop.

These changes were already made in this working tree (see **§6 file list**). This
runbook is written so a fresh session/agent can execute it top-to-bottom.

---

## 0. Background you must know before touching env vars

Two model "transports" exist, and the same-looking env vars route to different places.
Getting this wrong sends an OpenAI id to the Claude-only CLI and suspends the phase.

| Transport | How it runs | Provider in this test |
|---|---|---|
| **CLI** | `ainxt` binary subprocess, model via `--model`, hardwired to `api.anthropic.com` by `bin/config.toml` | **Anthropic** |
| **In-process** | `model_router.generate()` / `_llm()` inside the worker | **OpenAI** |

**Phases per transport (verified against the live call sites):**

- **CLI (keep Anthropic):** `classify`, `plan`, `implement`, governance `review` (SCAN),
  governance `fix`.
- **In-process (move to OpenAI):** `normalize`, `code_review` (diff review),
  `manifest_validate` (already OpenAI by default), `locate`, and the in-process
  patch-engine `coder`/`fixer` path.

**⚠️ DO-NOT-TOUCH env vars** (they leak into a CLI/Claude spawn):
- `SDLC_MODEL_PLAN` → feeds `cli_model_for("plan")` (a CLI spawn). Leave **unset**.
- `SDLC_MODEL_CODER` → feeds the governance-fix CLI spawn. Leave **unset**.
- `SDLC_TIER_COMPLEX_MODEL`, `SDLC_TIER_SOLUTION_MODEL`, `SDLC_TIER_SIMPLE_MODEL` →
  these remap the tiers the **CLI phases** resolve through. Leave **unset** for this
  mixed test (setting them would push the CLI phases onto OpenAI too).
- `SDLC_CLI_CLASSIFY_MODEL`, `SDLC_CLI_PLAN_MODEL`, `SDLC_CLI_IMPLEMENT_MODEL` →
  leave **unset** so they default to Claude tiers (haiku/complex).

**No-op vars:** `SDLC_MODEL_ANALYZE/DESIGN/SYNTHESIS/DIAGNOSE/SOLUTION_REVIEW/`
`CROSS_MODEL_REVIEW` have no live call site in the current 3-phase CLI pipeline —
setting them does nothing. Don't rely on them.

---

## 1. Prerequisites

- Docker + Docker Compose, and `bin/ainxt` + `bin/config.toml` present (they are — this
  enables the `sdlc-worker`).
- **Both** API keys available:
  - `ANTHROPIC_API_KEY` — required, powers the CLI phases.
  - `OPENAI_API_KEY` — required, powers the in-process phases.
- For the JIRA trigger: `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` (ticket read + status
  comments), and optionally `JIRA_WEBHOOK_SECRET`.
- A target repo the pipeline can clone: `GITLAB_TOKEN`/`SCM_PROVIDER` (or GitHub token),
  and per-user PAT decryption `FERNET_KEY`.

---

## 2. Install & bring up the stack

```bash
cd <repo-root>            # .../ainxt-enterprise-main
./install.sh              # docker mode is the default; interactive provider prompt
# non-interactive alternative:
# AINXT_PROVIDER=anthropic ./install.sh --docker --yes
```

`install.sh` creates `.env` from `.env.example`, generates secrets, brings up
`postgres redis kafka gateway kafka-consumer ai-ui` and — because `bin/ainxt` exists —
`sdlc-worker`. It health-gates on `GET /health`.

If you picked a single provider at install, that's fine — we set both keys manually next.

---

## 3. Configure `.env` for the mixed setup

Edit `.env` (never commit it). Set the keys and the per-phase overrides. Use `set_env`
style or edit by hand:

```dotenv
# --- keys: BOTH providers ---
ANTHROPIC_API_KEY=sk-ant-...        # CLI phases (Claude)
OPENAI_API_KEY=sk-...               # in-process phases (OpenAI)

# --- OpenAI concrete model ids (also defaulted in docker-compose anchor; set here to be explicit) ---
OPENAI_CODING_MODEL=gpt-5.4         # "medium" tier
OPENAI_LATEST_MODEL=gpt-5.5         # "deep" tier
OPENAI_SIMPLE_MODEL=gpt-5-mini      # "mini" tier

# --- IN-PROCESS phases → OpenAI (tier names route to OpenAI from OPENAI_* + key) ---
SDLC_MODEL_NORMALIZE=mini           # normalize → gpt-5-mini
SDLC_MODEL_CODE_REVIEW=deep         # diff review → gpt-5.5 (was Opus)
SDLC_MODEL_LOCATE=mini              # locate → gpt-5-mini
SDLC_MODEL_MANIFEST_VALIDATE=deep   # already OpenAI by default; explicit for clarity

# --- CLI phases: leave UNSET so they default to Claude tiers ---
# SDLC_CLI_CLASSIFY_MODEL=          # (unset → haiku → Claude)
# SDLC_CLI_PLAN_MODEL=              # (unset → complex → Claude)
# SDLC_CLI_IMPLEMENT_MODEL=         # (unset → complex → Claude)

# --- MUST stay unset (would leak OpenAI ids into a Claude-only CLI spawn) ---
# SDLC_MODEL_PLAN=      SDLC_MODEL_CODER=
# SDLC_TIER_COMPLEX_MODEL=  SDLC_TIER_SOLUTION_MODEL=  SDLC_TIER_SIMPLE_MODEL=

# --- JIRA ---
JIRA_URL=https://your-org.atlassian.net
JIRA_EMAIL=you@org.com
JIRA_API_TOKEN=...
# JIRA_WEBHOOK_SECRET=...           # only if using the live webhook
```

Apply the changes (env is read at call time, but restart to reload the process env):

```bash
docker compose up -d gateway sdlc-worker
```

### 3a. Ensure the OpenAI provider is enabled in the registry

Tier names `medium`/`deep`/`mini` reach OpenAI from `OPENAI_*_MODEL` + `OPENAI_API_KEY`
alone. Seeding the DB provider is still recommended (enables the admin screen + concrete-id
routing):

```bash
docker exec ainxt-gateway python db/bootstrap_llm_providers.py \
  --family openai --slug openai-env --name "OpenAI (from .env)" \
  --key-env-var OPENAI_API_KEY
```

Re-run is safe (idempotent). It auto-discovers and enables OpenAI model rows.

---

## 4. Verify model resolution BEFORE a full run

Confirm each phase resolves to the intended provider (quick, no pipeline needed):

```bash
docker exec ainxt-sdlc-worker python - <<'PY'
from core.model_registry import sdlc_stage_hint, cli_classify_model, cli_plan_model, cli_implement_model
print("normalize (want OpenAI):", sdlc_stage_hint("normalize"))       # mini
print("code_review (want OpenAI):", sdlc_stage_hint("code_review"))    # deep
print("manifest_validate (OpenAI):", sdlc_stage_hint("manifest_validate"))  # deep
print("locate (want OpenAI):", sdlc_stage_hint("locate"))             # mini
print("CLI classify (want Claude):", cli_classify_model())            # claude-haiku-*
print("CLI plan (want Claude):", cli_plan_model())                    # claude-sonnet-*
print("CLI implement (want Claude):", cli_implement_model())          # claude-sonnet-*
PY
```

Expected: the first four are OpenAI (tier names or gpt-* ids); the last three are `claude-*`.
If a CLI line shows a gpt id, you set a DO-NOT-TOUCH var — unset it and restart.

---

## 5. Trigger the SDLC pipeline from JIRA

### Option A — live JIRA webhook (`/webhooks/jira`)
Point a Jira automation/webhook at `POST https://<gateway>/webhooks/jira` on
`jira:issue_created`. The description **must** contain a `repo: owner/repo` line;
`summary` + `description` + `repo` are required. If `JIRA_WEBHOOK_SECRET` is set, send it
in the `X-Jira-Webhook-Secret` header.

Quick curl (flat body works when there's no `issue` key; secret header only if configured):
```bash
curl -sS -X POST https://<gateway>/webhooks/jira \
  -H 'Content-Type: application/json' \
  -H 'X-Jira-Webhook-Secret: <JIRA_WEBHOOK_SECRET-if-set>' \
  -d '{"webhookEvent":"jira:issue_created",
       "issue":{"key":"PROJ-123",
         "fields":{"summary":"Add X","description":"Do X.\nrepo: myorg/myrepo",
                   "issuetype":{"name":"Task"}}}}'
```

### Option B — manual trigger (no webhook wiring; recommended for a controlled test)
Authenticated REST call; reads the Jira ticket by key and enqueues a run:
```bash
# get a bearer token first (login via UI or the auth endpoint), then:
curl -sS -X POST https://<gateway>/sdlc/feature \
  -H "Authorization: Bearer <TOKEN>" -H 'Content-Type: application/json' \
  -d '{"jira_key":"PROJ-123","summary":"Add X","description":"Do X.","repo":"myorg/myrepo"}'
# → returns {"run_id": "..."}; for a bug use POST /sdlc/bug
```

Poll status:
```bash
curl -sS -H "Authorization: Bearer <TOKEN>" https://<gateway>/sdlc/runs/<run_id>
```

---

## 6. Verify the run used the right providers

Tail the worker log and confirm per-phase model tags:

```bash
docker compose logs -f sdlc-worker        # or: tail -f ./log/agent.log
```

Look for:
- `[NORM ...]` → OpenAI model (gpt-5-mini).
- `[MANIFEST-OPENAI ...]` (OpenAI-tier direct path) → gpt-5.5. (If you had pinned a
  concrete non-OpenAI id it would log `[MANIFEST-VALIDATE ... (router)]` instead.)
- `[REVIEW ...]` diff review → gpt-5.5.
- `[SDLC-CLI ...]` classify/plan/implement → `claude-*` (`--model claude-...`).
- **No** `model guard`/`suspended`/empty-model lines.

If any in-process phase shows a Claude model, its `SDLC_MODEL_<STAGE>` override didn't take
— recheck §3 and that you restarted the worker.

---

## 7. Files changed (for the patch)

```
core/model_registry.py
agents/sdlc_normalizer.py
agents/sdlc_pipeline/_phases.py
agents/sdlc_governance/config.py
docker-compose.yml
.env.example
env.example
SDLC_MIXED_MODEL_RUNBOOK.md   # this file (optional to include)
```

`.env` is intentionally NOT included (secrets; local only).

---

## 8. Create a portable patch for another laptop

This working tree is **not** a git repo, so there is no built-in baseline to diff against.
**Primary method for this task: Option A (git patch).** Option B is a fallback only if a
pristine copy of the original release is unavailable.

### Option A — git patch  ✅ PRIMARY  (needs the ORIGINAL pristine source)
On this laptop, using a clean copy of the same release (e.g. re-extract the original
`ainxt-enterprise-main.zip`):
```bash
# 1. pristine baseline
cp -r /path/to/pristine-ainxt /tmp/base && cd /tmp/base
git init -q && git add -A && git commit -qm baseline

# 2. overlay the 7 changed files from the working tree
WT=/e/AiNxt-OSS/ainxt-enterprise-main/ainxt-enterprise-main
for f in core/model_registry.py agents/sdlc_normalizer.py \
         agents/sdlc_pipeline/_phases.py agents/sdlc_governance/config.py \
         docker-compose.yml .env.example env.example SDLC_MIXED_MODEL_RUNBOOK.md; do
  mkdir -p "$(dirname "$f")"; cp "$WT/$f" "$f"; done

# 3. produce the patch
git add -A && git diff --cached > /tmp/ainxt-sdlc-generic-models.patch
```
On the OTHER laptop (same pristine release checked out; `git apply` works even without a
repo, matching files by path):
```bash
cd /path/to/other-laptop-repo
git apply --check ainxt-sdlc-generic-models.patch   # dry run; must be clean
git apply ainxt-sdlc-generic-models.patch           # or: patch -p1 < ...
```

### Option B — overlay bundle  (FALLBACK — only if no pristine source is available)
```bash
cd <repo-root>
tar czf ainxt-sdlc-generic-models.tgz \
  core/model_registry.py agents/sdlc_normalizer.py \
  agents/sdlc_pipeline/_phases.py agents/sdlc_governance/config.py \
  docker-compose.yml .env.example env.example SDLC_MIXED_MODEL_RUNBOOK.md
```
On the other laptop (same original repo version), from its repo root:
```bash
tar xzf ainxt-sdlc-generic-models.tgz     # overlays the 7 files in place
python -m py_compile core/model_registry.py agents/sdlc_normalizer.py \
  agents/sdlc_pipeline/_phases.py agents/sdlc_governance/config.py   # sanity
docker compose config >/dev/null && echo "compose OK"
```
> Option B overwrites those files wholesale — safe only if the other laptop is on the same
> original version. If it has its own edits, use Option A (a real diff) instead.

---

## 9. Rollback

- Revert env: remove the `SDLC_MODEL_*` overrides from `.env`, `docker compose up -d gateway sdlc-worker`.
- Revert code: restore the 7 files from your pristine copy / VCS, or `git apply -R` the patch.
