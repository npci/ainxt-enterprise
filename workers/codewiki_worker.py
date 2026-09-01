# SPDX-License-Identifier: Apache-2.0
# ============================================================
# CODEWIKI WORKER — clone a public repo and generate documentation
#
# IMPORTANT: this worker shells out to the real `codewiki` CLI (the exact
# `codewiki generate --github-pages --verbose --output <dir>` command an
# operator would type by hand) instead of calling the generator library
# directly. The CLI package installed in this venv has been manually
# patched in several places (dependency analysis, clustering, module
# naming, etc.) — invoking the library internals directly bypassed those
# patches and could silently drift from what a manual terminal run
# produces. Running the actual CLI as a subprocess guarantees the UI's
# "Generate Wiki" button does exactly what a manual
# `codewiki generate --github-pages --verbose --output <dir>` run does,
# with the terminal's own stdout/stderr captured and streamed into the
# job row's `logs` column so the UI can show it live.
# ============================================================

import os
import re
import sys
import json
import subprocess
import tempfile
import threading
import time as _time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
from urllib.parse import urlparse

from git import Repo as GitRepo

from db.database import pg_raw_connection, DB_SCHEMA
from core.url_masking import mask_repo_url, mask_text


# No in-repo fallback default -- CODEWIKI_DOCS_DIR must point at a directory
# OUTSIDE the repo checkout (generated docs are runtime data, not source;
# see docs/codewiki-server-deployment.md section 4.1). Read lazily via
# _require_codewiki_docs_dir() rather than raising here at import time:
# this module has no guaranteed .env-loading dependency of its own (neither
# it nor db/database.py calls load_dotenv() -- that only happens via
# whichever entry point started this process, e.g. workers/start_workers.py,
# and RQ's dynamic `importlib.import_module("workers.codewiki_worker")` job
# dispatch does not guarantee that happened first in every process). Raising
# eagerly here could fire a false positive purely due to import-order timing
# even when CODEWIKI_DOCS_DIR is genuinely set in .env.
_CODEWIKI_DOCS_DIR_ENV_VAR = "CODEWIKI_DOCS_DIR"


def _require_codewiki_docs_dir() -> str:
    """Return CODEWIKI_DOCS_DIR, or raise a clear RuntimeError if it's not
    set -- checked at the point of actual use (inside run_codewiki_doc_job()),
    not at module import time.
    """
    value = os.getenv(_CODEWIKI_DOCS_DIR_ENV_VAR)
    if not value:
        raise RuntimeError(
            f"{_CODEWIKI_DOCS_DIR_ENV_VAR} is not set. This must point at a "
            "directory OUTSIDE the repo checkout (generated docs are "
            "runtime data, not source -- see "
            "docs/codewiki-server-deployment.md section 4.1). There is no "
            "in-repo fallback default."
        )
    return value

# Cap how much log text we persist per job — a verbose CLI run over a large
# monorepo can emit megabytes of output; keep only the tail so the UI stays
# responsive and the DB row doesn't grow unbounded. Older lines are dropped
# from the front, newest kept.
_MAX_LOG_CHARS = int(os.getenv("CODEWIKI_MAX_LOG_CHARS", "2000000"))  # ~2MB


# Defaults for the CODEWIKI_* config-sync env vars below, matching this
# platform's currently-deployed codewiki setup (the values already saved in
# ~/.codewiki/config.json on the machine this was written on) -- so a fresh
# server that sets NONE of the optional CODEWIKI_* overrides still gets a
# working, previously-validated configuration rather than the codewiki
# package's own generic defaults (e.g. fallback_model=glm-4p5). Override any
# of these via their respective CODEWIKI_* env var if the deployment needs a
# different model/budget.
# These name specific locally-served models. They are already overridable per
# call via CODEWIKI_MAIN_MODEL / _CLUSTER_MODEL / _FALLBACK_MODEL; reading the
# same vars here means a deployment that sets them once gets them everywhere,
# including in any code path that consults the default directly.
_CODEWIKI_DEFAULT_MAIN_MODEL = os.getenv("CODEWIKI_MAIN_MODEL", "")
_CODEWIKI_DEFAULT_CLUSTER_MODEL = os.getenv("CODEWIKI_CLUSTER_MODEL", "")
_CODEWIKI_DEFAULT_FALLBACK_MODEL = os.getenv("CODEWIKI_FALLBACK_MODEL", "")
_CODEWIKI_DEFAULT_MAX_TOKENS_FOR_GENERATION = 32768
_CODEWIKI_DEFAULT_MAX_TOKENS_FOR_CLUSTERING = 131072
_CODEWIKI_DEFAULT_MAX_TOKEN_PER_MODULE = 36369
_CODEWIKI_DEFAULT_MAX_TOKEN_PER_LEAF_MODULE = 16000
_CODEWIKI_DEFAULT_MAX_DEPTH = 2


def _sync_codewiki_config_from_env(log_info, log_warning) -> None:
    """Write the `codewiki` CLI's own persistent config (normally set once,
    interactively, via `codewiki config set`) from this platform's own env
    vars instead, so a server deployment never needs a manual, per-machine
    `codewiki config set ...` step.

    The CLI itself has no built-in env-var support for this (confirmed by
    reading cli/commands/generate.py -- it loads exclusively via
    ConfigManager().load(), which reads ~/.codewiki/config.json + the system
    keyring/an encrypted fallback file). This uses ConfigManager's own
    public save() API directly (the same thing `codewiki config set` calls
    under the hood) rather than reaching into any private internals, so it
    stays correct even if the package's internal config-file format changes
    later.

    Base URL / API key reuse the platform's EXISTING NEURON_BASE_URL /
    NEURON_API_KEY env vars as their fallback (confirmed identical to this
    machine's currently-configured codewiki setup) -- CODEWIKI_BASE_URL/
    CODEWIKI_API_KEY only need to be set if this deployment wants codewiki
    to use a DIFFERENT endpoint/key than the rest of the platform. base_url
    is normalized the same way the old (now-removed) in-process
    implementation did: a trailing '/v1' is appended if not already
    present, since NEURON_BASE_URL itself does not include it but the
    OpenAI-compatible client codewiki uses expects it.

    main_model deliberately does NOT fall back to NEURON_MODEL_NAME --
    that env var can point at whatever model the platform's OWN chat/agent
    features are currently using (subject to change independently of
    codewiki), whereas codewiki's main model has been separately validated
    and should stay pinned to CODEWIKI_DEFAULT_MAIN_MODEL unless explicitly
    overridden via CODEWIKI_MAIN_MODEL.

    Deliberately opt-in and non-destructive:
      - Only runs at all if a base URL resolves (CODEWIKI_BASE_URL or
        NEURON_BASE_URL). If neither is set, this function does nothing and
        whatever config already exists on this machine (e.g. from a prior
        manual `codewiki config set`) is used unchanged -- so a deployment
        that prefers the manual, one-time setup keeps working exactly as
        before.
      - Every individual field is itself optional in ConfigManager.save()
        (None = "leave whatever is already saved unchanged"), so partially
        overlapping manual + env-based configuration merges safely rather
        than one blanking out the other.
      - Runs on every job (cheap -- local file/keyring writes only, no
        network) rather than once at process startup, so a config change in
        the platform's own env (e.g. rotating the LLM API key) takes effect
        on the very next job without needing to restart the worker.
    """
    base_url = os.getenv("CODEWIKI_BASE_URL") or os.getenv("NEURON_BASE_URL")
    if not base_url:
        return
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url = base_url + "/v1"

    def _int_env(name: str, default: int) -> int:
        raw = os.getenv(name)
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            log_warning(f"codewiki: ignoring non-integer {name}={raw!r}, using default {default}", env_var=name)
            return default

    from codewiki.cli.config_manager import ConfigManager

    manager = ConfigManager()
    manager.load()  # no-op if no config exists yet; save() below creates one
    manager.save(
        api_key=os.getenv("CODEWIKI_API_KEY") or os.getenv("NEURON_API_KEY") or None,
        base_url=base_url,
        main_model=os.getenv("CODEWIKI_MAIN_MODEL") or _CODEWIKI_DEFAULT_MAIN_MODEL,
        cluster_model=os.getenv("CODEWIKI_CLUSTER_MODEL") or _CODEWIKI_DEFAULT_CLUSTER_MODEL,
        fallback_model=os.getenv("CODEWIKI_FALLBACK_MODEL") or _CODEWIKI_DEFAULT_FALLBACK_MODEL,
        provider=os.getenv("CODEWIKI_PROVIDER") or None,
        max_tokens_for_generation=_int_env("CODEWIKI_MAX_TOKENS_FOR_GENERATION", _CODEWIKI_DEFAULT_MAX_TOKENS_FOR_GENERATION),
        max_tokens_for_clustering=_int_env("CODEWIKI_MAX_TOKENS_FOR_CLUSTERING", _CODEWIKI_DEFAULT_MAX_TOKENS_FOR_CLUSTERING),
        max_token_per_module=_int_env("CODEWIKI_MAX_TOKEN_PER_MODULE", _CODEWIKI_DEFAULT_MAX_TOKEN_PER_MODULE),
        max_token_per_leaf_module=_int_env("CODEWIKI_MAX_TOKEN_PER_LEAF_MODULE", _CODEWIKI_DEFAULT_MAX_TOKEN_PER_LEAF_MODULE),
        max_depth=_int_env("CODEWIKI_MAX_DEPTH", _CODEWIKI_DEFAULT_MAX_DEPTH),
    )
    log_info(
        "codewiki: synced CLI config from platform env vars",
        base_url=base_url,
        main_model=os.getenv("CODEWIKI_MAIN_MODEL") or _CODEWIKI_DEFAULT_MAIN_MODEL,
        cluster_model=os.getenv("CODEWIKI_CLUSTER_MODEL") or _CODEWIKI_DEFAULT_CLUSTER_MODEL,
    )


def _derive_repo_name(repo_url: str) -> str:
    """Derive a filesystem-safe repo name from a Git URL."""
    parsed = urlparse(repo_url)
    path = parsed.path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    name = path.split("/")[-1] if path else "unknown-repo"
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)


def _update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    set_clause = ", ".join(f"{k} = %s" for k in fields.keys())
    values = list(fields.values()) + [job_id]
    sql = f"""
        UPDATE {DB_SCHEMA}.codewiki_doc_jobs
           SET {set_clause}
         WHERE id = %s
    """
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, values)
        conn.commit()


def _append_log(job_id: str, text: str) -> None:
    """Append `text` to the job's `logs` column, trimmed to _MAX_LOG_CHARS."""
    if not text:
        return
    with pg_raw_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {DB_SCHEMA}.codewiki_doc_jobs
                   SET logs = right(logs || %s, %s),
                       updated_at = NOW()
                 WHERE id = %s
                """,
                (text, _MAX_LOG_CHARS, job_id),
            )
        conn.commit()


def _list_markdown_pages(output_dir: Path) -> List[Dict[str, Any]]:
    """Return a sorted list of markdown pages found under output_dir."""
    pages: List[Dict[str, Any]] = []
    for root, _dirs, files in os.walk(output_dir):
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = Path(root) / fname
            rel_path = fpath.relative_to(output_dir).as_posix()
            title = fpath.stem.replace("_", " ").replace("-", " ").title()
            pages.append({
                "title": title,
                "file": fname,
                "path": rel_path,
            })
    # Sort overview / catalog first, then alphabetical
    def _sort_key(p):
        lower = p["file"].lower()
        if lower.startswith("overview"):
            return (0, lower)
        if lower.startswith("catalog"):
            return (1, lower)
        return (2, lower)
    pages.sort(key=_sort_key)
    return pages


def _strip_utf8_bom_in_repo(repo_path: Path) -> int:
    """Remove UTF-8 BOM (U+FEFF) from text files in repo_path. Returns number of files rewritten.

    The function attempts to detect files that start with the UTF-8 BOM (0xEF,0xBB,0xBF)
    and rewrites them without the BOM. Binary files or unreadable files are skipped.
    """
    rewritten = 0
    for root, _dirs, files in os.walk(repo_path):
        for fname in files:
            fpath = Path(root) / fname
            try:
                # Read bytes first, then attempt to decode using utf-8-sig to handle BOMs robustly
                with open(fpath, "rb") as b:
                    raw = b.read()
                if not raw:
                    continue
                # Attempt to decode with utf-8-sig (handles BOM) first, falling back to utf-8 then latin-1.
                try:
                    text = raw.decode("utf-8-sig")
                except Exception:
                    try:
                        text = raw.decode("utf-8")
                    except Exception:
                        try:
                            text = raw.decode("latin-1")
                        except Exception:
                            # Give up on undecodable files
                            continue

                # If the decoded text started with a BOM character, remove it.
                if text and text[0] == '\ufeff':
                    text = text[1:]

                # Write normalized UTF-8 without BOM
                try:
                    with open(fpath, "w", encoding="utf-8", newline="\n") as out:
                        out.write(text)
                    rewritten += 1
                except Exception:
                    continue
            except Exception:
                # Skip binary or inaccessible files
                continue
    return rewritten


def _codewiki_cli_env() -> dict:
    """Build the environment for the `codewiki` CLI subprocess.

    The CLI reads its LLM credentials from `codewiki config set` (persisted
    to ~/.codewiki/config.json + system keyring/credentials file on the host
    running the worker) — exactly like a manual terminal run would. We do
    NOT inject NEURON_* platform env vars here: the whole point of shelling
    out to the real CLI is to use the SAME configuration path (and the same
    manually-patched package) an operator already validated by hand, rather
    than re-deriving config independently in the worker (which is what the
    previous implementation did, and how it drifted from manual runs).
    """
    env = os.environ.copy()
    # Force unbuffered, UTF-8 output so streamed log lines show up promptly
    # and non-ASCII repo content doesn't crash the log capture on Windows.
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_codewiki_cli(
    job_id: str,
    repo_path: Path,
    output_dir: Path,
    log_info,
    log_error,
) -> None:
    """Run `codewiki generate --github-pages --verbose --output <output_dir>`
    as a real subprocess with `cwd=repo_path`, exactly as a user would type
    it in a terminal after `cd`-ing into the cloned repo. Streams combined
    stdout/stderr line-by-line into the job's `logs` column as it runs, and
    raises RuntimeError if the process exits non-zero.
    """
    # Use the exact same Python interpreter this worker process is running
    # under (the project venv) so `python -m codewiki` resolves the same
    # manually-patched package installed there — equivalent to running the
    # `codewiki` console-script entry point from an activated venv shell.
    cmd = [
        sys.executable, "-m", "codewiki", "generate",
        "--github-pages",
        "--verbose",
        "--output", str(output_dir),
    ]
    log_info(
        "codewiki: launching CLI subprocess",
        job_id=job_id,
        cmd=" ".join(cmd),
        cwd=str(repo_path),
    )
    _append_log(job_id, f"$ {' '.join(cmd)}\n(cwd: {repo_path})\n\n")

    proc = subprocess.Popen(
        cmd,
        cwd=str(repo_path),
        env=_codewiki_cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        # `codewiki generate` can prompt for y/n confirmation when an output
        # directory already has a previous generation attempt (see
        # generate.py's click.confirm calls). This job always generates into
        # a brand-new timestamped output_dir, so that prompt never fires in
        # practice — stdin is closed defensively so the process fails fast
        # instead of hanging forever if it ever does.
        stdin=subprocess.DEVNULL,
    )

    # Stream output line-by-line: log to the app logger AND persist to the
    # job's `logs` column in small batches so the UI's poll can show
    # progress live without hammering the DB on every single line.
    #
    # Flushes on WHICHEVER comes first: _LOG_FLUSH_LINES lines buffered, or
    # _LOG_FLUSH_SECONDS elapsed since the last flush -- checked on its own
    # background timer (see _flush_timer_loop() below), NOT just after each
    # new line arrives. `for line in proc.stdout` BLOCKS waiting for the
    # next line, so a check that only runs between lines can never fire
    # during a genuinely silent gap (e.g. Phase 2/3 waiting on a single
    # slow LLM call that produces no terminal output at all in between) --
    # confirmed directly: a naive "check elapsed time in the same loop"
    # version of this only ever flushed once the NEXT line finally arrived,
    # which defeats the purpose. Running the time check on an independent
    # thread means the UI's live log view is guaranteed to reflect
    # everything captured so far within _LOG_FLUSH_SECONDS, regardless of
    # how long the subprocess goes without printing anything.
    _LOG_FLUSH_LINES = 20
    _LOG_FLUSH_SECONDS = 3.0
    buffer: list[str] = []
    buffer_lock = threading.Lock()
    stop_flush_timer = threading.Event()

    def _flush():
        with buffer_lock:
            if not buffer:
                return
            text = "".join(buffer)
            buffer.clear()
        _append_log(job_id, text)

    def _flush_timer_loop():
        # Wakes up every _LOG_FLUSH_SECONDS and flushes whatever's
        # buffered, independent of whether new lines have arrived --
        # this is what actually bounds the UI's staleness during a
        # genuinely silent gap, since the main loop below can't.
        while not stop_flush_timer.wait(_LOG_FLUSH_SECONDS):
            _flush()

    flush_thread = threading.Thread(target=_flush_timer_loop, daemon=True)
    flush_thread.start()

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            with buffer_lock:
                buffer.append(line)
                flush_due = len(buffer) >= _LOG_FLUSH_LINES
            if flush_due:
                _flush()
    finally:
        # Stop the timer thread and do one final flush of whatever's left,
        # even if the loop above exited via an unexpected exception (e.g. a
        # decode error on a single malformed line) rather than the
        # subprocess's stdout simply closing normally -- so a non-fatal
        # hiccup here never silently drops already-captured output. This
        # does NOT protect against the worker process itself being killed
        # outright (SIGKILL allows no further code to run, in this or any
        # process, including this finally block) -- that failure mode is
        # instead handled by the periodic orphaned-job recovery sweep, see
        # recover_orphaned_codewiki_jobs() below.
        stop_flush_timer.set()
        flush_thread.join(timeout=5)
        _flush()

    returncode = proc.wait()
    log_info("codewiki: CLI subprocess exited", job_id=job_id, returncode=returncode)

    if returncode != 0:
        raise RuntimeError(
            f"codewiki generate exited with code {returncode}. See job logs for details."
        )


def run_codewiki_doc_job(payload: dict) -> None:
    """Entry point called by the job queue.

    The RQ enqueue path currently passes a single payload dict as the first
    positional argument. Accept that dict and extract expected fields here.

    This function logs each major phase so operators can trace progress in
    agent.log: received -> running -> cloning -> preprocessing -> generating -> completed/failed.
    """
    # Extract expected fields with validation
    job_id = payload.get("job_id") if isinstance(payload, dict) else None
    codebase_name = payload.get("codebase_name") if isinstance(payload, dict) else None
    repo_url = payload.get("repo_url") if isinstance(payload, dict) else None
    branch = payload.get("branch") if isinstance(payload, dict) else "main"

    # Use the platform structlog logger for structured key/value logging
    try:
        from core.logger import logger as app_logger
    except Exception:
        app_logger = None

    def _info(msg, **kw):
        if app_logger is not None:
            app_logger.info(msg, **kw)
        else:
            print("INFO:", msg, kw)

    def _error(msg, **kw):
        if app_logger is not None:
            # Use exception logging when an exception is present
            if 'exc' in kw and kw.get('exc'):
                app_logger.exception(msg, **{k: v for k, v in kw.items() if k != 'exc'})
            else:
                app_logger.error(msg, **kw)
        else:
            print("ERROR:", msg, kw)

    def _warn(msg, **kw):
        if app_logger is not None:
            app_logger.warning(msg, **kw)
        else:
            print("WARN:", msg, kw)

    # Backwards-compatible aliases used in earlier edits
    log_info = _info
    log_error = _error
    log_warning = _warn

    if not job_id or not codebase_name or not repo_url:
        # Invalid payload — record a failure if we have an id, otherwise log and bail
        if job_id:
            _update_job(job_id, status="failed", error_message="invalid job payload")
        _error("codewiki: invalid job payload", job_id=job_id, codebase_name=codebase_name)
        return

    # Masked in this log line -- repo_url may embed a credential (e.g.
    # https://user:token@host/org/repo, per the CodeWiki panel's supported
    # URL form). The RAW repo_url variable itself is untouched and still
    # used below for the actual `git clone` call -- only what gets written
    # to agent.log / the job's own displayed `logs` column is masked.
    _info("codewiki: job received", job_id=job_id, codebase_name=codebase_name, repo_url=mask_repo_url(repo_url), branch=branch)
    _update_job(job_id, status="running", error_message=None, logs="")
    _info("codewiki: job marked running", job_id=job_id)

    # Sync the codewiki CLI's own persistent config (~/.codewiki/config.json
    # + keyring) from this platform's env vars, if provided -- see
    # _sync_codewiki_config_from_env()'s docstring. No-op if neither
    # CODEWIKI_BASE_URL nor NEURON_BASE_URL is set, so a deployment relying
    # on a manual one-time `codewiki config set` keeps working exactly as
    # before.
    try:
        _sync_codewiki_config_from_env(log_info, log_warning)
    except Exception as _cfg_e:
        log_warning("codewiki: failed to sync CLI config from env (continuing with existing config)", error=str(_cfg_e))

    temp_clone_dir: Path | None = None
    try:
        safe_codebase = re.sub(r"[^a-zA-Z0-9_\-]", "_", codebase_name)
        safe_branch = re.sub(r"[^a-zA-Z0-9_\-]", "_", branch)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        output_dir = Path(_require_codewiki_docs_dir()) / safe_codebase / safe_branch / timestamp
        output_dir.mkdir(parents=True, exist_ok=True)
        log_info("codewiki: created output_dir", job_id=job_id, output_dir=str(output_dir))

        # Clone the repo into a temp directory
        temp_clone_dir = Path(tempfile.mkdtemp(prefix="codewiki_clone_"))
        clone_path = temp_clone_dir / "repo"
        log_info("codewiki: starting git clone", job_id=job_id, clone_path=str(clone_path))
        # Masked in the job's own `logs` column, which the UI polls and
        # displays live/verbatim while the job is pending/running -- the
        # actual clone_from() call below still uses the real, unmasked
        # `repo_url` variable.
        _append_log(job_id, f"Cloning {mask_repo_url(repo_url)} (branch: {branch})...\n")
        GitRepo.clone_from(
            repo_url,
            str(clone_path),
            branch=branch,
            single_branch=True,
            depth=1,
        )
        log_info("codewiki: clone completed", job_id=job_id, clone_path=str(clone_path))
        _append_log(job_id, "Clone completed.\n\n")

        # Strip UTF-8 BOMs from text files in the repository to avoid parser errors
        log_info("codewiki: stripping BOMs (if any)", job_id=job_id)
        rewritten_count = _strip_utf8_bom_in_repo(clone_path)
        if rewritten_count:
            log_info("codewiki: stripped BOMs", job_id=job_id, files_rewritten=rewritten_count)
            _append_log(job_id, f"Stripped UTF-8 BOM from {rewritten_count} file(s) (auto-fix).\n\n")
            # Record a non-fatal note in the job row so UI operators see it
            _update_job(job_id, error_message=f"Stripped BOM from {rewritten_count} files (auto-fix)")

        # Run the real `codewiki generate --github-pages --verbose --output <dir>`
        # CLI as a subprocess — identical to a manual terminal invocation,
        # including every manual patch applied to the installed package.
        _run_codewiki_cli(job_id, clone_path, output_dir, log_info, log_error)

        pages = _list_markdown_pages(output_dir)
        log_info("codewiki: pages collected", job_id=job_id, pages_count=len(pages))

        # Record the commit that was actually documented so a future
        # "Regenerate" can diff against it (see codewiki_router.py's
        # regenerate dry-run).
        commit_sha = ""
        try:
            commit_sha = GitRepo(str(clone_path)).head.commit.hexsha
        except Exception:
            pass

        _update_job(
            job_id,
            status="completed",
            codebase_name=codebase_name,
            output_dir=str(output_dir),
            pages=json.dumps(pages),
            last_commit_sha=commit_sha,
        )
        log_info("codewiki: job completed successfully", job_id=job_id, codebase_name=codebase_name)

    except Exception as exc:  # noqa: BLE001
        # Log full exception stack to the common logs and record failure in DB
        # Defense-in-depth credential redaction: a git clone failure's
        # exception message can embed the repo_url (GitPython itself
        # already redacts the password in GitCommandError as of 3.1.40,
        # but mask_text() here is a second, library-independent layer that
        # also covers any OTHER exception type that might happen to quote
        # the raw payload/repo_url somewhere in its message).
        safe_exc_text = mask_text(str(exc))
        log_error("codewiki: job failed", exc=exc, job_id=job_id)
        _append_log(job_id, f"\nERROR: {safe_exc_text}\n")
        _update_job(
            job_id,
            status="failed",
            error_message=safe_exc_text,
        )
    finally:
        if temp_clone_dir and temp_clone_dir.exists():
            import shutil
            try:
                shutil.rmtree(temp_clone_dir, ignore_errors=True)
                log_info("codewiki: cleaned up temp clone", job_id=job_id)
            except Exception as _e:
                log_warning("codewiki: failed to remove temp clone", job_id=job_id, error=str(_e))


# ============================================================
# ORPHANED-JOB RECOVERY
#
# Problem:
#   run_codewiki_doc_job()'s own except/finally blocks are what write
#   status='failed' back to Postgres on any FAILURE. Those only run if the
#   worker process itself keeps running long enough to reach them. If the
#   process is killed outright instead (OOM, host restart, `taskkill`, the
#   `codewiki generate` subprocess or its parent worker crashing) with no
#   Python exception ever raised, none of that code executes -- the job's
#   DB row is left saying 'running' forever, and (confirmed by direct
#   inspection during a real incident) its RQ job entry in Redis is simply
#   gone: nothing is left anywhere that could ever un-stick it.
#
#   codewiki_queue deliberately has NO RQ job_timeout (see
#   core/job_queue.py's enqueue_codewiki_job() -- generation can
#   legitimately take up to ~2 days), so the fixed-staleness-threshold
#   approach used by workers/kb_cleanup_worker.py's
#   recover_stale_indexing_docs() (stuck > RQ timeout + buffer = orphaned)
#   does not apply here: there is no timeout to be "longer than".
#
# Solution:
#   Use RQ's own job registry as the source of truth instead of a fixed
#   time threshold. A job that is GENUINELY still running always has a
#   live RQ job (queued, started, or deferred) under the SAME id as the
#   Postgres row (see enqueue_codewiki_job()'s job_id= kwarg -- the two
#   ids are always kept identical, by design, specifically so this kind of
#   cross-check is possible). If core.job_queue.get_job_status() reports
#   the job doesn't exist at all, the worker that was supposed to be
#   processing it is definitively gone -- there's no other way that
#   combination (Postgres says running, RQ has never heard of it) can
#   arise short of a bug in enqueue_codewiki_job() itself.
#
#   A minimum age gate (_MIN_AGE_MINUTES) avoids a benign race against
#   enqueue_codewiki_job() itself -- the tiny window between INSERTing the
#   'running' row and the q.enqueue() call actually landing in Redis.
#
# Wired into:
#   workers/start_workers.py -- interval_jobs list (every 10 minutes,
#   alongside kb_stale_recovery, which this mirrors).
# ============================================================

_MIN_AGE_MINUTES = 5


def recover_orphaned_codewiki_jobs() -> dict:
    """Find codewiki_doc_jobs rows stuck in 'running' whose RQ job no
    longer exists at all, and reset them to 'failed' with an explanatory
    error_message so the UI's Retry button (see routers/codewiki_router.py's
    POST /retry) can re-run them from scratch.

    Called every 10 minutes by the cron scheduler in start_workers.py.

    Returns:
        {"recovered": N, "checked": M} -- N rows reset, out of M candidates
        old enough to check at all.
    """
    try:
        from core.job_queue import get_job_status
        from core.logger import logger as _logger

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=_MIN_AGE_MINUTES)

        with pg_raw_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, codebase_name, updated_at
                      FROM {DB_SCHEMA}.codewiki_doc_jobs
                     WHERE status = 'running'
                       AND updated_at < %s
                    """,
                    (cutoff,),
                )
                candidates = cur.fetchall()

        if not candidates:
            _logger.debug("[CODEWIKI_CLEANUP] No running jobs old enough to check")
            return {"recovered": 0, "checked": 0}

        recovered = 0
        for job_id, codebase_name, updated_at in candidates:
            status_info = get_job_status(str(job_id))
            # get_job_status() returns status="unknown" with an explicit
            # "Job not found" error both when RQ/Redis is genuinely
            # unavailable AND when the job id simply doesn't exist in
            # Redis -- deliberately fail closed (treat "can't tell" the
            # same as "still running", i.e. do nothing) rather than risk
            # mass-failing every in-flight job during a transient Redis
            # blip. Only the specific, unambiguous "Job not found" case
            # (Redis IS reachable, this exact id just isn't in it) means
            # recover; genuine other errors are logged and skipped.
            if status_info.get("status") != "unknown":
                continue
            if status_info.get("error") != "Job not found":
                _logger.warning(
                    f"[CODEWIKI_CLEANUP] Could not determine RQ status for "
                    f"job {job_id} ({codebase_name}) — skipping this cycle: "
                    f"{status_info.get('error')}"
                )
                continue

            _error_msg = (
                "Processing was interrupted (worker process crashed or was "
                f"killed while this job was running, last update "
                f"{updated_at.isoformat()}). Click Retry to run it again."
            )
            _update_job(str(job_id), status="failed", error_message=_error_msg)
            _append_log(str(job_id), f"\nERROR: {_error_msg}\n")
            _logger.warning(
                f"[CODEWIKI_CLEANUP] Recovered orphaned codewiki job: "
                f"job_id={job_id} codebase_name='{codebase_name}' "
                f"stuck_since={updated_at.isoformat()}"
            )
            recovered += 1

        if recovered:
            _logger.info(
                f"[CODEWIKI_CLEANUP] Recovered {recovered} orphaned codewiki "
                f"job(s) (running -> failed) out of {len(candidates)} checked"
            )
        return {"recovered": recovered, "checked": len(candidates)}

    except Exception as exc:
        try:
            from core.logger import logger as _logger
            _logger.error(f"[CODEWIKI_CLEANUP][ERROR] recover_orphaned_codewiki_jobs failed: {exc}")
        except Exception:
            pass
        return {"recovered": 0, "checked": 0}
