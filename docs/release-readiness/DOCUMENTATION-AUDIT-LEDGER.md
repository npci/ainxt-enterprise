# Documentation, Repository & Public Presentation Audit — Ledger

Persistent record of the public-presentation audit. Findings are never deleted;
resolved ones are marked and kept as regression scenarios.

| Field | Value |
|---|---|
| Audit date | 2026-08-29 |
| Branch | `master` |
| Baseline commit | `076421b73d45ae6ba983979b7e42425d84fa4c56` |
| Working tree at start | clean |
| Tracked files | 2062 |
| Scope | Documentation, repository presentation, links, leakage, hygiene, contributor and security docs |
| Explicitly out of scope | `LICENSE`, `NOTICE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `DCO`, `AUTHORS`, `MAINTAINERS.md`, `CODEOWNERS`, `OSSMETADATA` — declared legally verified; **validated, not modified** |

Statuses: OPEN · FIXED · VERIFIED · KNOWN_LIMITATION · REQUIRES_LEGAL · REQUIRES_HUMAN_DECISION

---

## DOC-001 — Badge label typo on the first line of the landing page

| | |
|---|---|
| **Category** | Presentation |
| **File** | `README.md:3` |
| **Severity** | MEDIUM (HIGH for first impression) |
| **Expected** | `oss_lifecycle` |
| **Actual** | `oss_lifecyce` — rendered as a live badge reading "oss lifecyce | active" |
| **Fix** | Corrected the shields.io label |
| **Action taken** | FIXED |
| **Verification** | `grep -c 'oss_lifecyce-' README.md` → 0 |
| **Status** | **VERIFIED** |

The single most visible defect in the repository: a spelling error in the first
rendered element a GitHub visitor sees. The alt text already said "Lifecycle".

---

## DOC-002 — `docs/` had 587 files and no entry point

| | |
|---|---|
| **Category** | Information architecture |
| **File** | `docs/` |
| **Severity** | **HIGH** |
| **Expected** | A navigable index |
| **Actual** | 587 flat markdown files, no `README.md`/`index.md`/`SUMMARY.md`. Clicking `docs/` on GitHub showed an alphabetical wall from `Canvas.md` to `zoho_router.md` |
| **Fix** | Added `docs/README.md` — states plainly that these are per-module reference pages, points newcomers to `GETTING_STARTED.md` / `README.md` / `SUPPORT.md` / `compliance/` first, then groups all 587 pages into 17 collapsible topic sections. Also names the 4 non-markdown generated artifacts in the directory |
| **Action taken** | FIXED |
| **Verification** | 587 links present; every one resolves (`readme-links` check green) |
| **Status** | **VERIFIED** |

The worst presentation problem found. Content already existed and was good — it
was simply unnavigable.

---

## DOC-003 — Developer-machine path shipped inside an LLM prompt

| | |
|---|---|
| **Category** | Internal information leak |
| **File** | `agents/orchestrator.py:457` |
| **Severity** | MEDIUM |
| **Expected** | A neutral illustrative path |
| **Actual** | `"path": "/Users/admin/Desktop"` embedded in a prompt template sent to the model |
| **Classification** | DOCUMENTATION PROBLEM — not a credential, but a macOS developer path published in a prompt, which also biases the model toward probing it |
| **Fix** | Replaced with `/absolute/path/to/directory` |
| **Action taken** | FIXED |
| **Verification** | `grep -c '/Users/admin' agents/orchestrator.py` → 0; file parses |
| **Status** | **VERIFIED** |

---

## DOC-004 — "HOD" used three times, never expanded

| | |
|---|---|
| **Category** | Terminology |
| **File** | `README.md` (seed-step comments) |
| **Severity** | LOW |
| **Expected** | Expansion on first use |
| **Actual** | "HOD department mappings", "HOD budget/governance features" — an external reader has no way to know this means Head of Department |
| **Fix** | Expanded on first use |
| **Action taken** | FIXED |
| **Status** | **VERIFIED** |

Other internal-sounding terms were checked and found already explained in place:
**ABStudio** ("visual agent and workflow builder (React + FastAPI)") and
**HSM** (own section, marked optional). The third such term, the RustyCluster KV
backend, was removed entirely rather than explained — see REL-009 below.

---

## DOC-005 — Documented environment variables absent from the template

| | |
|---|---|
| **Category** | Documentation accuracy |
| **File** | `.env.example` |
| **Severity** | MEDIUM |
| **Expected** | Every variable the docs tell you to set is present in the template you are told to copy |
| **Actual** | `SEED_ADMIN_PASSWORD` (README: "set SEED_ADMIN_PASSWORD in .env before first boot") and `WITH_OCR` were both absent |
| **Fix** | Added both with explanatory comments, including the non-obvious behaviour that a *generated* password is never re-applied to an existing account whereas an explicitly set one is |
| **Action taken** | FIXED |
| **Verification** | 88 variables, 0 duplicate keys; README's "(N vars)" claim re-synced |
| **Status** | **VERIFIED** |

---

## DOC-006 — Flagship install URLs do not resolve

| | |
|---|---|
| **Category** | Broken link / release blocker |
| **File** | `README.md:19`, `README.md:51`, `install.sh:5`, `install.sh:13` |
| **Severity** | **CRITICAL at publication** |
| **Expected** | The headline command works when copied |
| **Actual** | `https://raw.githubusercontent.com/npci/ainxt-enterprise/main/install.sh` → **404**; `https://github.com/npci/ainxt-enterprise.git` → **404** |
| **Classification** | REQUIRES_HUMAN_DECISION — the real org/repo path is not knowable from inside the repository |
| **Fix** | Replaced the vague `<!-- update URLs once repo is public -->` note with an explicit pre-publication instruction naming the exact lines to change. `install.sh` already honours `AINXT_REPO_URL` for overriding without editing |
| **Action taken** | Partial — note made actionable; the URL itself cannot be verified pre-publication |
| **Status** | **REQUIRES_HUMAN_DECISION** |

**This is the one item that will embarrass the project on day one if missed.** The
very first thing a visitor copies is a `curl` command that 404s.

---

## DOC-007 — 8 MB generated HTML bundle at the repository root

| | |
|---|---|
| **Category** | Repository hygiene / presentation |
| **File** | `documentation.html` (8.0 MB, tracked) |
| **Severity** | LOW–MEDIUM |
| **Actual** | The largest tracked file in the repository, a single-page generated bundle sitting in the root listing with no explanation. Also the only remaining tracked file containing `your-registry.example.com` strings (inside generated content) |
| **Recommended** | Move under `docs/`, or drop it in favour of the now-indexed `docs/` tree, or explain it in the README |
| **Action taken** | None — deleting or relocating an 8 MB deliverable is an owner decision |
| **Status** | **REQUIRES_HUMAN_DECISION** |

---

## DOC-008 — Orphaned `env.example` still shipped

| | |
|---|---|
| **Category** | Documentation confusion |
| **File** | `env.example` (1027 variables, tracked) |
| **Severity** | MEDIUM |
| **Actual** | Referenced by no file in the repository, sits beside the real `.env.example` (88 vars), and carries **13 duplicate keys** of its own. A visitor cannot tell which template is authoritative |
| **Recommended** | Delete, or relocate as a clearly-labelled reference inventory |
| **Action taken** | None — deletion is an owner decision. Excluded from the CI `env-duplicates` gate so it cannot mask regressions in the real template |
| **Status** | **REQUIRES_HUMAN_DECISION** |

---

## DOC-009 — `desktop/.npmrc` publishes internal infrastructure guidance

| | |
|---|---|
| **Category** | Internal information leak |
| **File** | `desktop/.npmrc`, `docs/sandbox_image_building.md` |
| **Severity** | MEDIUM |
| **Actual** | Committed guidance stating "NPCI network blocks github.com", "npm PACKAGES resolve fine via the Nexus registry in `~/.npmrc`", "Ask infra for the mirror base URLs", plus two commented `your-registry.example.com` mirror URLs |
| **Classification** | DOCUMENTATION PROBLEM — nothing is functionally broken (all mirror lines are commented), but it instructs external developers to work around a network restriction that does not apply to them and to contact a team they cannot reach |
| **Recommended** | Replace with a vendor-neutral note that Electron downloads binaries from GitHub at install time and `electron_mirror` may be set behind a corporate proxy |
| **Action taken** | None this pass — outside the documentation set under review; carried forward |
| **Status** | **OPEN** |

---

## DOC-010 — `assets/Logo/Adobe` design working file (1.4 MB)

| | |
|---|---|
| **Category** | Repository hygiene |
| **Severity** | LOW |
| **Actual** | The second-largest tracked file is an Adobe design source asset. Legitimate to keep if brand source files are intended to ship; otherwise it is working material |
| **Status** | **REQUIRES_HUMAN_DECISION** |

---

## DOC-011 — Installer left the audit-log signing key at a world-known value

| | |
|---|---|
| **Category** | Security hygiene |
| **File** | `install.sh`, `.env.example:332` |
| **Severity** | **HIGH** |
| **Expected** | Every secret the installer is responsible for is generated |
| **Actual** | `install.sh` generated `POSTGRES_PASSWORD`, `JWT_SECRET` and `SECRET_KEY` but **not** `AUDIT_SIGNING_KEY`, which `.env.example` ships as the literal string `change-me-in-production`. `core/config.py:852` rejects that value in production ("must be set in prod — no default allowed"), so production is protected — but every development and OSS install ran with a publicly-known key **signing the audit log**, silently, unless the operator noticed the placeholder |
| **Fix** | `install.sh` now generates `AUDIT_SIGNING_KEY` alongside the other three |
| **Action taken** | FIXED |
| **Verification** | Exercised `gen_secret`/`set_env` against a fresh copy of the template: `change-me-in-production` occurrences 2 → 0, both keys populated with 48-hex-char values |
| **Status** | **VERIFIED** |

Found by the Phase 13 placeholder sweep. A placeholder in a template is correct;
a placeholder that nothing ever replaces is not.

---

## DOC-012 — CONTRIBUTING invites contributions the README says are not accepted

| | |
|---|---|
| **Category** | Contradictory instructions |
| **File** | `CONTRIBUTING.md` (lines 4, 32-38), `README.md` (Contributing), `.github/ISSUE_TEMPLATE/*`, `.github/PULL_REQUEST_TEMPLATE.md` |
| **Severity** | MEDIUM (HIGH reputationally — this is the kind of thing that gets screenshotted) |
| **Expected** | One consistent answer to "can I contribute?" |
| **Actual** | `CONTRIBUTING.md` opens "Thank you for your interest in contributing! This document explains how to get involved" and lists "**Bug reports** — open an issue", "**Reviews** — review open pull requests". The README states external PRs and issues are "**not currently accepted or triaged**". Neither issue template nor the PR template carried any status, so the entire action path — CONTRIBUTING says "open an issue" → template gives no warning → issue is never read — was unguarded |

### Fix, in two parts

**Part 1 — DONE.** Added a status note to all three templates, so anyone about to
file something sees the posture at the moment they would write it, and is pointed
at `SUPPORT.md` for answers and at a private advisory for security reports. These
files are not in the legally-verified set. YAML frontmatter preserved (still the
first line), all relative links verified to resolve from their subdirectory depth.

The README already reconciles correctly, describing `CONTRIBUTING.md` as "the
workflow the maintaining team follows — which is the workflow external
contributions will follow when they open."

**Part 2 — DONE, with explicit owner authorisation.** A reader landing directly on
`CONTRIBUTING.md` still saw an unqualified invitation, and no README or template
change reaches that reader. `CONTRIBUTING.md` is in the declared legally-verified
set, so the options were put to the owner, who chose the narrow edit: a single
qualifying paragraph immediately above the "Ways to Contribute" bullet list — the
exact place that says "open an issue".

Deliberately **additive only, 7 inserted lines, 0 deletions**. The opening
paragraph, DCO section, licensing statement, Code of Conduct reference and every
other section are untouched. No legal claim is altered.

The wording offered in the decision preview referenced "the Status note above",
which belonged to the wider option that was not chosen. It was made
self-contained instead, pointing at `README.md#contributing` and `SECURITY.md`
rather than at a note that does not exist.

| | |
|---|---|
| **Action taken** | **FIXED** — templates + narrow `CONTRIBUTING.md` qualifier |
| **Verification** | `git diff --stat`: 7 insertions, 0 deletions. Both new links resolve (`## Contributing` anchor present in README; `SECURITY.md` exists). All nine other legally-verified files checksum-unchanged. All 9 CI checks green |
| **Status** | **VERIFIED** |
| **Note** | `CONTRIBUTING.md` checksum changed `d41dc5af53f2` → `cd7b168e3be9` by owner decision. If the file requires re-sign-off, this is the only change to it in the entire audit |

---

## Validated and found CORRECT — recorded so regressions are visible

| Area | Result |
|---|---|
| Repository hygiene | **Excellent.** Zero debris tracked — no `__pycache__`, `.DS_Store`, `node_modules`, `.venv`, `dist`, `build`, `*.log`, `*.pyc`, databases or secrets. `.gitignore` is doing its job |
| Placeholders in public-facing files | **None.** No `TODO`, `FIXME`, `TBD`, `XXX`, `change-me`, `your-api-key`, `<REPLACE>` in README, SUPPORT, `.env.example`, GETTING_STARTED, compliance, compose or install.sh |
| Placeholder emails | All `@example.com` occurrences are legitimate — HTML input `placeholder` attributes (`OSSProgram.jsx`) and test fixtures. No fake maintainer contacts |
| Documentation accuracy (Phase 4) | **Every** documented command, script, path and compose profile exists: `db/migrate.py`, `scripts/seed.py`, `gunicorn.conf.py`, `install.sh` (+`--local`, `--with-ocr`), `stop-local.sh`, `compliance/generate-sbom.sh`, `npm run dev`/`build`, and the `kafka`/`observability`/`embed` profiles |
| Internal hostnames in public docs | **Clean** — zero `npci` hostnames in README, SUPPORT, `.env.example`, GETTING_STARTED, compliance, compose, install.sh |
| Lockfiles | **Clean** — 0 internal-registry references (previously 489) |
| Secrets in env templates | **Clean** — every populated `*_KEY`/`*_TOKEN`/`*_PASSWORD` value is a non-credential |
| External links | All non-localhost links valid except DOC-006. Apache licence URL, pgvector, ollama.ai and all shields.io badges resolve |
| Legal file presence | All 10 present: LICENSE (201 lines), NOTICE (169), CONTRIBUTING (262), CODE_OF_CONDUCT (13), SECURITY (37), DCO (34), AUTHORS, MAINTAINERS, CODEOWNERS, OSSMETADATA. **Content not reviewed — declared legally verified** |
| Community health | Issue templates (bug, feature), PR template, CODEOWNERS, and CI (`.github/workflows/ci.yml`) all present |

---

## Not fixed — carried forward from the setup audit

These are engineering findings from the companion
`NEWCOMER-SETUP-LEDGER.md`, restated here only where they affect public perception.

| Ref | Item | Status |
|---|---|---|
| SETUP-018 | ~10 GB uncompressed image; **7 `LicenseRef-NVIDIA-Proprietary` components as hard dependencies** in an Apache-2.0 release | **REQUIRES_LEGAL** |
| SETUP-019 | `CONTRIBUTING.md` invites bug reports, feature requests and PR reviews; README states contributions are "not currently accepted or triaged". `SECURITY.md` says "No email alias is published"; `MAINTAINERS.md` publishes `opensource@npci.org.in`. **Both files are in the legally-verified set and were not modified** | **REQUIRES_HUMAN_DECISION** |
| SETUP-032 | `model: "local"` routing | FIXED |
| SETUP-035 | 7 routes advertised in OpenAPI that return 404 | OPEN |
| — | `dependabot.yml` absent | REQUIRES_HUMAN_DECISION |
| — | No screenshots or demo in README, though the product has a working UI | REQUIRES_HUMAN_DECISION |

---

## REL-009 — Internal-only KV backend referenced throughout a public release

| | |
|---|---|
| **Severity** | HIGH |
| **Expected** | A public repository contains no code paths, dependencies or documentation for a backend the public cannot obtain |
| **Actual** | The RustyCluster KV backend was present across 90 files: two full client implementations (`core/kv/rustycluster_impl.py`, `core/kv/async_rustycluster_impl.py`), live code paths in `core/kv/queue.py` (queue/worker/scheduler construction), `core/kv/factory.py`, `core/config.py` and `gateway.py`; a `py-rustycluster-client` install block in `requirements.txt` pointing at a private registry; `RUSTYCLUSTER_PASSWORD` / `RUSTYCLUSTER_CONFIG_PATH` in `env.example` and `core/ckms/bootstrap.py`; a dedicated README section; and ~200 references across 35 reference docs plus the generated `documentation.html`, `docs/index.html` and both `module_tree.json` files. 88 tests were parametrised over a backend whose client package is not on PyPI, so they could only ever skip |
| **Fix** | Removed. Redis is now the only backend. The per-DB indirection (`REDIS_CLIENT_CONFIG_DB{n}`) was deliberately **kept**, because it is how call sites address a logical database and it is what would let a second backend be added without touching several hundred call sites. `core/config.py` gained an explicit migration guard: a carried-forward `RUSTYCLUSTER` value fails at config load with a message naming the variable in force and what to set instead, rather than a bare "invalid value" |
| **Action taken** | FIXED |
| **Status** | **VERIFIED** |

Two defects were found while doing this, both by the checks written for the work
rather than by inspection:

* The removed-backend guard existed only in `kv_backend_for()`, not in the
  module-level `REDIS_CLIENT_CONFIG` check, so an operator setting the *global*
  variable got a generic validation error instead of the migration message. The
  duplication was the cause, so the two copies were replaced by one
  `_validate_kv_backend()` helper used by both paths.
* Five mermaid edges in `docs/kv_store.md` and two more in `documentation.html`
  still pointed at node IDs whose declarations had been deleted, which renders as
  dangling edges. Found by a ghost-node-reference check, not by reading the diff.

**Validation performed:** full test suite run against live Postgres + Redis both
at `HEAD` and after the change, failure sets compared line by line — **50 failures
before, the same 50 after, zero new**; passing tests 924 → 925 (the one added
test), skipped 89 → 1 as the 88 unrunnable RUSTYCLUSTER-parametrised variants
disappeared. 23 KV-touching modules import clean; KV get/set, pipeline, Lua
script, 9-DB health probe, RQ queue and worker construction all exercised against
live Redis. Gateway image rebuilt and restarted: healthy, `/health` reports all
nine logical DBs as `backend: REDIS, ok: true`, boot log free of KV errors, login
returns a correct 401 rather than a 500. All 2,989 mermaid diagrams in `docs/`
checked for fence balance, `subgraph`/`end` balance and ghost node references;
`documentation.html` re-verified as 637 intact `PAGES` entries, and every large
embedded JSON literal in `docs/index.html` re-parsed.

---

## Change record

| File | Change | Reason |
|---|---|---|
| `README.md` | Badge label typo; actionable pre-publication URL note; HOD expanded; docs index + getting-started links added to the documentation table; var count re-synced | DOC-001, 002, 004, 005, 006 |
| `docs/README.md` | **New** — index of all 587 reference pages | DOC-002 |
| `agents/orchestrator.py` | `/Users/admin/Desktop` → `/absolute/path/to/directory` | DOC-003 |
| `.env.example` | Added `SEED_ADMIN_PASSWORD`, `SEED_USER_PASSWORD`, `WITH_OCR` | DOC-005 |
| `docs/release-readiness/DOCUMENTATION-AUDIT-LEDGER.md` | **New** — this ledger | Phase 21 |
| `core/kv/rustycluster_impl.py`, `core/kv/async_rustycluster_impl.py` | **Deleted** | REL-009 |
| `core/kv/queue.py`, `core/kv/factory.py`, `core/kv/base.py`, `core/kv/health.py`, `core/kv/errors.py`, `core/kv/__init__.py` | RustyCluster code paths removed; interface contract documented in place of the two-backend framing | REL-009 |
| `core/config.py` | `_validate_kv_backend()` helper replaces two divergent copies of the backend validation; removed-backend migration guard | REL-009 |
| `core/job_queue.py`, `gateway.py`, `workers/start_workers.py`, `workers/chat_worker.py`, `core/ckms/bootstrap.py`, + 15 further modules | Dead branches and stale comments removed | REL-009 |
| `requirements.txt`, `env.example`, `README.md` | Private-registry install block, `RUSTYCLUSTER_*` variables and the README section removed; `REDIS_CLIENT_CONFIG=REDIS` retained with corrected commentary | REL-009 |
| `tests/conftest.py`, `tests/kv/*`, `tests/config/*`, `tests/auth/*`, `tests/core/*` | Imports of the deleted modules removed; 88 unrunnable parametrised variants dropped; two tests added covering the migration guard | REL-009 |
| 35 files in `docs/`, `documentation.html`, `docs/index.html`, `docs/module_tree.json`, `docs/first_module_tree.json` | ~200 references removed, including 5 mermaid diagrams restructured without breaking them | REL-009 |
| `ai-ui/src/components/BrandMark.jsx` | `onError` rendered a constant instead of the state variable, so the documented SVG → PNG → glyph fallback skipped the PNG entirely | Phase A defect |

**Validation performed:** ASCII architecture diagrams verified byte-identical
(54 box-drawing lines before and after); all 9 CI static checks green; every
README link resolves; `.env.example` has 0 duplicate keys; `orchestrator.py`
parses; no file in the legally-verified set was modified (checksums unchanged).
