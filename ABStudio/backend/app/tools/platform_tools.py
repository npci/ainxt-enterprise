# SPDX-License-Identifier: Apache-2.0
"""
Platform tools — code_executor, read_skill_file, and draft utility tools.

Active tools (seeded on startup):
  code_executor    — execute Python code in a sandbox, returns generated files
  read_skill_file  — fetch a bundled file from an attached skill on demand

Draft tools (NOT seeded until LLM_PROXY_URL is configured):
  web_search     — search the web via the internal proxy
  file_search    — hybrid semantic + keyword search over the knowledge base
  execute_code   — run Python in a subprocess (stdout/stderr); bash/shell
                   removed (security review F-08 — arbitrary shell command
                   injection by design)
  llm_generate   — call the internal LLM proxy and return the response

Env vars required for draft tools:
  LLM_PROXY_URL — internal LLM/search proxy base URL
"""

import inspect

from app.core.skill_manifest import NO_SUBDIRS_CLAUSE
from app.tools import _sandbox_net_guard

# ---------------------------------------------------------------------------
# code_executor
# ---------------------------------------------------------------------------
# The LLM writes Python code that generates files (PDF, DOCX, PPTX, CSV,
# etc.) and writes them to the OUTPUT_DIR path injected into the exec
# namespace. After execution, every file found there is moved to
# GENERATED_FILES_DIR and a download URL is returned.
#
# The egress guard's IP-range lists and bounded-DNS-resolve mechanics are
# NOT duplicated by hand here — see the module comment in
# app/tools/_sandbox_net_guard.py for why (this is the same canonical
# snippet document_tools.py's read_document SSRF guard uses).
# ---------------------------------------------------------------------------

_NET_GUARD_SRC = inspect.getsource(_sandbox_net_guard)

_CODE_EXECUTOR_CODE = _NET_GUARD_SRC + '''
import sys, os, json, io, pathlib, shutil, uuid, traceback, threading, subprocess as _subprocess, socket as _socket
from urllib.parse import quote

# os.chdir is process-global. FastAPI may dispatch tool calls on a thread
# pool, so concurrent code_executor invocations must not race on CWD.
_CHDIR_LOCK = threading.Lock()

# Allowlist for the CWD-rescue scan below. Frozenset hoisted out of the
# hot path so it isn't rebuilt per call.
_RESCUABLE_EXTS = frozenset({
    ".pptx", ".pdf", ".docx", ".xlsx", ".csv", ".txt", ".md",
    ".png", ".jpg", ".jpeg", ".svg", ".html", ".json", ".zip",
})

def _stamp_now(dest):
    # Anchor the download-store TTL clock to ingestion time. shutil.move across
    # filesystems and shutil.copy2 both PRESERVE the source mtime; when the
    # source is an older intermediate/template that carried an mtime past the
    # TTL, the freshly-served file would be "born expired" (download returns
    # 410). Resetting mtime to now makes the 24h window start when the artifact
    # actually enters GENERATED_FILES_DIR. Best-effort — never fail the run.
    try:
        os.utime(str(dest), None)
    except OSError:
        pass


def _move_unique(src, dest_dir, name):
    # shutil.move raises on Windows when dest exists; POSIX overwrites
    # silently. Both are wrong here, so uniquify with a uuid suffix that
    # preserves the file extension.
    dest = pathlib.Path(dest_dir) / name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}")
    try:
        shutil.move(str(src), str(dest))
        _stamp_now(dest)
        return dest
    except (OSError, shutil.Error):
        return None


def _copy_unique(src, dest_dir, name):
    """Copy ``src`` into ``dest_dir/name``, uniquifying if necessary."""
    dest = pathlib.Path(dest_dir) / name
    if dest.exists():
        dest = dest.with_name(f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}")
    try:
        shutil.copy2(str(src), str(dest))
        _stamp_now(dest)
        return dest
    except (OSError, shutil.Error):
        return None

# --- Intranet-only network policy -------------------------------------------
# Block connections to public internet AND link-local/metadata addresses;
# allow ONLY the curated intranet ranges. This is an ALLOWLIST (the opposite
# polarity of read_document's SSRF denylist) — see the module comment in
# _sandbox_net_guard.py for why the two guards can't share one list.
# _resolve_host_with_timeout / _is_intranet_ip / _SANDBOX_NET_TIMEOUT come
# from the _sandbox_net_guard snippet concatenated above this source.
_socket.setdefaulttimeout(_SANDBOX_NET_TIMEOUT)

_orig_connect = _socket.socket.connect
def _guarded_connect(self, address):
    if isinstance(address, (tuple, list)):
        host = address[0]
        try:
            ip_str = _resolve_host_with_timeout(host)
            if not _is_intranet_ip(ip_str):
                raise OSError(f"Network policy: external internet access is blocked (tried {host})")
        except OSError:
            raise
        except Exception:
            pass
    return _orig_connect(self, address)

_socket.socket.connect = _guarded_connect

# --- subprocess.Popen guard --------------------------------------------------
# Models occasionally generate code that shells out to a CLI that is not
# installed on this host (libreoffice, pdftoppm, node, etc.) — on Windows
# that raises FileNotFoundError: [WinError 2] which manifests as an opaque
# "Traceback (most recent call last):" in the UI. Wrap Popen so the missing
# command is surfaced as a clean, actionable message.
_orig_popen_init = _subprocess.Popen.__init__
def _guarded_popen_init(self, args, *a, **kw):
    try:
        return _orig_popen_init(self, args, *a, **kw)
    except FileNotFoundError as e:
        if isinstance(args, (list, tuple)) and args:
            cmd = str(args[0])
        else:
            cmd = str(args).split()[0] if args else "<unknown>"
        raise FileNotFoundError(
            f"Command '{cmd}' is not installed on this server. "
            "Avoid shelling out to platform-specific CLIs — "
            "use a pure-Python library instead."
        ) from e
_subprocess.Popen.__init__ = _guarded_popen_init
# ---------------------------------------------------------------------------

def run(inputs):
    code = (inputs.get("code") or "").strip()
    if not code:
        return {"error": "No code provided"}

    generated_files_dir = os.environ.get("GENERATED_FILES_DIR", "")
    if not generated_files_dir:
        import tempfile
        generated_files_dir = tempfile.mkdtemp()

    run_id = str(uuid.uuid4())[:8]
    run_output_dir = os.path.join(generated_files_dir, f"run_{run_id}")
    os.makedirs(run_output_dir, exist_ok=True)

    namespace = {
        "__name__": "__code_executor__",
        "OUTPUT_DIR": run_output_dir,
        "WORKFLOW_ARTIFACT_DIR": os.environ.get("WORKFLOW_ARTIFACT_DIR", ""),
        # Per-agent Sample Document (look-and-feel reference) — see
        # ``app/api/agent_sample.py`` and ``skill_manifest.sample_doc_directive``.
        # These three globals are always defined; when no sample is attached
        # they're empty strings so ``if SAMPLE_DOC_PATH: ...`` is the natural
        # gate. Mirrors ``os.environ["SAMPLE_DOC_*"]`` for LLM code that
        # prefers bare names (matches how ``OUTPUT_DIR`` is exposed).
        "SAMPLE_DOC_PATH": os.environ.get("SAMPLE_DOC_PATH", ""),
        "SAMPLE_DOC_KIND": os.environ.get("SAMPLE_DOC_KIND", ""),
        "SAMPLE_DOC_DIR":  os.environ.get("SAMPLE_DOC_DIR", ""),
    }

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    error_msg  = None

    # Models routinely call Presentation().save("foo.pptx") with a bare
    # relative path. Because exec() runs in-process, that lands in the
    # backend's CWD (project root on Windows) instead of OUTPUT_DIR, the
    # collector below misses it, and the user sees "no files generated"
    # while .pptx files silently accumulate in D:\\ainxt-platform.
    # Fix: chdir into run_output_dir for the duration of exec (serialized
    # by _CHDIR_LOCK because CWD is process-global), then if nothing landed
    # in run_output_dir, rescue any new files in the original CWD.
    _orig_cwd = os.getcwd()

    with _CHDIR_LOCK:
        # Snapshot CWD names inside the lock so a concurrent caller can't
        # create files between snapshot and rescue. listdir is name-only —
        # no per-entry stat, unlike pathlib.iterdir + is_file.
        try:
            _orig_cwd_before = set(os.listdir(_orig_cwd))
        except OSError:
            _orig_cwd_before = set()

        # The skill docs also expose WORKFLOW_ARTIFACT_DIR, and models often
        # save there instead of OUTPUT_DIR. Snapshot its existing files so the
        # post-exec sweep below can lift only the NEW ones (relative paths from
        # this run) without re-collecting artifacts from prior runs.
        _artifact_dir = os.environ.get("WORKFLOW_ARTIFACT_DIR", "")
        _artifact_before = set()
        if _artifact_dir and os.path.isdir(_artifact_dir):
            try:
                for _dp, _dn, _fn in os.walk(_artifact_dir):
                    for _f in _fn:
                        _artifact_before.add(os.path.join(_dp, _f))
            except OSError:
                pass

        try:
            from contextlib import redirect_stdout, redirect_stderr
            os.chdir(run_output_dir)
            with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                exec(compile(code, "<code_executor>", "exec"), namespace)
        except SystemExit as _se:
            # Skill scripts run via runpy.run_path(run_name="__main__") end in
            # ``sys.exit(main())``. SystemExit is a BaseException, so without
            # this branch it escapes run() and kills the sandbox wrapper BEFORE
            # it can print the JSON result -> the dispatcher sees exit-0 with no
            # stdout ("produced no output") and burns all 5 retries. A zero/None
            # code is a normal successful exit and must NOT be treated as error.
            _code = _se.code
            if _code not in (0, None):
                error_msg = "Script exited with code %r\\n%s" % (_code, traceback.format_exc())
        except Exception:
            error_msg = traceback.format_exc()
        finally:
            try:
                os.chdir(_orig_cwd)
            except OSError:
                pass

        # Always rescue: mixed-output runs (one file to OUTPUT_DIR, one
        # stray to CWD) would otherwise orphan the stray.
        try:
            with os.scandir(_orig_cwd) as it:
                for entry in it:
                    if entry.name in _orig_cwd_before:
                        continue
                    # Concurrency guard: when the sandbox CWD == GENERATED_FILES_DIR
                    # (the default), every dispatch's run_<id>/ directory is a
                    # sibling here. Without this skip a long-running call would
                    # snapshot _orig_cwd_before, a parallel call would create
                    # its own run_<id>/, and the first call would steal it.
                    if entry.name.startswith("run_"):
                        continue
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        continue
                    if is_dir:
                        _move_unique(entry.path, run_output_dir, entry.name)
                        continue
                    if not entry.is_file():
                        continue
                    if os.path.splitext(entry.name)[1].lower() not in _RESCUABLE_EXTS:
                        continue
                    _move_unique(entry.path, run_output_dir, entry.name)
        except OSError:
            pass

    stdout_text = stdout_buf.getvalue()

    # os.walk (not iterdir) so files nested inside subdirs the model
    # created — or a rescued CWD subtree from above — are also collected.
    generated_files = []
    try:
        for dirpath, _dirs, files in os.walk(run_output_dir):
            for fname in files:
                src = pathlib.Path(dirpath) / fname
                if not src.is_file():
                    continue
                dest = _move_unique(src, generated_files_dir, f"{run_id}_{fname}")
                if dest is None:
                    continue
                ext = src.suffix.lstrip(".").lower() or "bin"
                # `filename` is the human-readable name shown in chat;
                # `disk_name` matches what the URL serves. Frontend indexes
                # both so the LLM can reference either in markdown links.
                generated_files.append({
                    "filename":     src.name,
                    "disk_name":    dest.name,
                    "download_url": f"/generated-files/{quote(dest.name, safe='')}",
                    "format":       ext,
                    "path":         str(dest),
                })
    except Exception:
        pass

    # Second sweep: deliverable files the model wrote into
    # WORKFLOW_ARTIFACT_DIR (a documented, in-scope path) instead of
    # OUTPUT_DIR. Without this they are orphaned, the run reports "no files
    # generated", and the model wastes a full regeneration.
    #
    # Two safety constraints, because WORKFLOW_ARTIFACT_DIR doubles as the
    # working dir for multi-step (DSLAR) pipelines whose later steps read
    # back intermediates like enriched.json:
    #   1. COPY, never move — leave the original in place so downstream
    #      pipeline steps still find it.
    #   2. Only pick up download-deliverable extensions (pptx/pdf/docx/...),
    #      so intermediate .json/.txt scratch files aren't turned into chips.
    if _artifact_dir and os.path.isdir(_artifact_dir):
        try:
            for dirpath, _dirs, files in os.walk(_artifact_dir):
                for fname in files:
                    full = os.path.join(dirpath, fname)
                    if full in _artifact_before:
                        continue
                    src = pathlib.Path(full)
                    if not src.is_file():
                        continue
                    if src.suffix.lower() not in _RESCUABLE_EXTS:
                        continue
                    unique = f"{run_id}_{fname}"
                    dest = pathlib.Path(generated_files_dir) / unique
                    if dest.exists():
                        dest = dest.with_name(f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}")
                    try:
                        shutil.copy2(str(src), str(dest))
                        _stamp_now(dest)
                    except (OSError, shutil.Error):
                        continue
                    ext = src.suffix.lstrip(".").lower() or "bin"
                    generated_files.append({
                        "filename":     src.name,
                        "disk_name":    dest.name,
                        "download_url": f"/generated-files/{quote(dest.name, safe='')}",
                        "format":       ext,
                        "path":         str(dest),
                    })
        except Exception:
            pass

    # Cleanup empty run dir
    try:
        shutil.rmtree(run_output_dir, ignore_errors=True)
    except Exception:
        pass

    if error_msg:
        return {
            "error":           error_msg[:2000],
            "stdout":          stdout_text[:500],
            "generated_files": generated_files,
        }

    if not generated_files:
        return {
            "stdout":  stdout_text[:2000],
            "message": (
                "Code executed successfully but no files were generated. "
                "Write output files to the OUTPUT_DIR variable, e.g.: "
                "import os; path = os.path.join(OUTPUT_DIR, 'output.pdf')"
            ),
        }

    return {
        "generated_files": generated_files,
        "stdout":          stdout_text[:500],
    }
'''

# ---------------------------------------------------------------------------
# Draft tool helpers (web_search, file_search, execute_code, llm_generate)
# ---------------------------------------------------------------------------

_PROXY_HELPERS = '''
import os, json, urllib.request, urllib.error

def _proxy_base():
    return os.environ.get("LLM_PROXY_URL", "").rstrip("/")

def _request(method, path, payload=None, extra_headers=None):
    url  = f"{_proxy_base()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # Enterprise-grade timeout: 180s (was 30s). The platform proxy
        # fronts long-running LLM-generation tools (llm_generate, web_search
        # with re-ranking, file_search across large KBs) and is the call
        # path used when one flow is attached to another via "generate with
        # AI" — a 30s ceiling truncated those legitimately slow operations.
        # Errors (HTTP, expired tokens, network) still propagate as
        # structured exceptions for the agent to recover from.
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise Exception(f"HTTP {e.code}: {body[:400]}")
    except urllib.error.URLError as e:
        raise Exception(f"LLM proxy unreachable: {e.reason}")
'''

_WEB_SEARCH_CODE = _PROXY_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        query       = inputs.get("query", "")
        max_results = min(int(inputs.get("max_results", 10)), 20)
        result      = _request("POST", "/search", {"query": query, "max_results": max_results})
        results     = result.get("results", [])
        if not results:
            return {"result": f"No results found for: {query}", "results": []}
        lines = [f"Web search results for \'{query}\':"]
        items = []
        for r in results[:max_results]:
            title   = r.get("title", "?")
            snippet = r.get("snippet", "")[:200]
            url     = r.get("url", "")
            lines.append(f"• {title}\\n  {snippet}\\n  {url}")
            items.append({"title": title, "snippet": snippet, "url": url})
        return {"result": "\\n".join(lines), "results": items}
    except Exception as e:
        return {"error": str(e)}
'''

_FILE_SEARCH_CODE = _PROXY_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        query     = inputs.get("query", "")
        namespace = inputs.get("namespace", "")
        top_k     = min(int(inputs.get("top_k", 6)), 20)
        payload   = {"query": query, "top_k": top_k}
        if namespace:
            payload["namespace"] = namespace
        result = _request("POST", "/retrieve", payload)
        chunks = result.get("chunks", result.get("results", []))
        if not chunks:
            return {"result": f"No knowledge base results for: {query}", "chunks": []}
        lines = [f"Knowledge base results for \'{query}\':"]
        items = []
        for c in chunks[:top_k]:
            title  = c.get("title", c.get("doc_id", "?"))
            text   = c.get("chunk_text", c.get("text", ""))[:300]
            score  = c.get("score", 0)
            source = c.get("source_url", c.get("url", ""))
            lines.append(f"• {title} (score: {score:.2f})\\n  {text}")
            items.append({"title": title, "text": text, "score": score, "source": source})
        return {"result": "\\n".join(lines), "chunks": items}
    except Exception as e:
        return {"error": str(e)}
'''

_EXECUTE_CODE_CODE = '''
import subprocess, sys, tempfile, os, uuid, pathlib, shutil
from urllib.parse import quote

def run(inputs: dict) -> dict:
    try:
        code     = inputs.get("code", "")
        language = inputs.get("language", "python").lower()
        timeout  = min(int(inputs.get("timeout", 30)), 60)

        # Pin every file the executed code writes to GENERATED_FILES_DIR so
        # downstream chat can render download chips and nothing leaks to the
        # backend process CWD (which would otherwise be the project root on
        # Windows). Mirrors _CODE_EXECUTOR_CODE's collection contract.
        generated_files_dir = os.environ.get("GENERATED_FILES_DIR", "")
        if not generated_files_dir:
            generated_files_dir = tempfile.mkdtemp()
        run_id = uuid.uuid4().hex[:8]
        run_output_dir = os.path.join(generated_files_dir, f"run_{run_id}")
        os.makedirs(run_output_dir, exist_ok=True)

        child_env = {**os.environ, "OUTPUT_DIR": run_output_dir}

        if language in ("python", "py"):
            with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
                f.write(code)
                fname = f.name
            try:
                proc = subprocess.run(
                    [sys.executable, fname],
                    capture_output=True, text=True, timeout=timeout,
                    cwd=run_output_dir,
                    env=child_env,
                )
            finally:
                os.unlink(fname)
        else:
            # Security review F-08: bash/sh/shell execution was removed.
            # subprocess.run(["bash", "-c", code]) handed an LLM-controlled
            # string straight to a shell — arbitrary OS command injection by
            # design, with no equivalent value over the python path (any
            # shell command can be run via subprocess/os from Python).
            return {
                "error": (
                    f"Unsupported language: {language}. Only 'python' is "
                    "supported by execute_code."
                )
            }

        # Collect anything the child wrote to its CWD / OUTPUT_DIR and lift it
        # into GENERATED_FILES_DIR with a run_id prefix so /generated-files/<x>
        # serves it back to the browser.
        # Recursive collection (os.walk, not iterdir) so files the code wrote
        # to a subdirectory are still picked up — parity with
        # _CODE_EXECUTOR_CODE's collection contract.
        generated_files = []
        try:
            for dirpath, _dirs, files in os.walk(run_output_dir):
                for fname in files:
                    src = pathlib.Path(dirpath) / fname
                    if not src.is_file():
                        continue
                    dest = _move_unique(src, generated_files_dir, f"{run_id}_{src.name}")
                    if dest is None:
                        continue
                    ext = src.suffix.lstrip(".").lower() or "bin"
                    generated_files.append({
                        "filename":     src.name,
                        "disk_name":    dest.name,
                        "download_url": f"/generated-files/{quote(dest.name, safe='')}",
                        "format":       ext,
                        "path":         str(dest),
                    })
        except Exception:
            pass
        try:
            shutil.rmtree(run_output_dir, ignore_errors=True)
        except Exception:
            pass

        # generated_files FIRST so any downstream truncation keeps the
        # download URLs (defense-in-depth alongside the engine-side
        # structure-aware shortener).
        return {
            "generated_files": generated_files,
            "result":          proc.stdout[:3000] if proc.stdout else "(no output)",
            "stdout":          proc.stdout[:3000],
            "stderr":          proc.stderr[:1000],
            "exit_code":       proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Code execution timed out after {timeout}s."}
    except Exception as e:
        return {"error": str(e)}
'''

# KNOWN LIMITATION — compliance coverage:
# This tool runs inside an isolated sandbox subprocess (urllib only) and POSTs
# the prompt directly to the LLM proxy's /llm/generate. Platform compliance
# (PCI/PII detection + redaction) lives EXCLUSIVELY in the backend gateway layer
# (Tier 1, agents/compliance_engine.py) and the standalone LLM proxy performs no
# compliance of its own. The sandbox cannot reach the compliance engine, so the
# prompt sent from here is NOT scanned by Tier 1. This is accepted because the
# prompt is agent-generated (swarm-authored), not raw end-user PII input. If this
# tool is ever changed to forward untrusted user text, gate it at the ABStudio
# backend dispatch layer (which CAN import agents.compliance_engine) BEFORE the
# sandbox subprocess is launched — do NOT reintroduce compliance into the proxy.
_LLM_GENERATE_CODE = _PROXY_HELPERS + '''
def run(inputs: dict) -> dict:
    try:
        prompt     = inputs.get("prompt", "")
        model_hint = inputs.get("model_hint", "balanced")
        max_tokens = int(inputs.get("max_tokens", 1024))
        payload    = {"prompt": prompt, "model_hint": model_hint, "max_tokens": max_tokens}
        result     = _request("POST", "/llm/generate", payload)
        text       = result.get("text") or result.get("content") or result.get("response") or str(result)
        return {"result": text, "model": result.get("model", "unknown")}
    except Exception as e:
        return {"error": str(e)}
'''

# ---------------------------------------------------------------------------
# read_skill_file
# ---------------------------------------------------------------------------
# Progressive disclosure for skills: instead of inlining every bundled file
# (editing.md, pythonpptx.md, scripts/...) into the system prompt, the LLM
# pulls them on demand via this tool. The sandbox subprocess inherits the
# parent's POSTGRES_* env vars so it can read the skill_files table directly.
# ---------------------------------------------------------------------------

_READ_SKILL_FILE_CODE = '''
import os, json

_MAX_BYTES = 200_000  # ~50k tokens; anything bigger should be executed, not read

def _pg_uri():
    # The code_executor sandbox runs in a SEPARATE subprocess and cannot borrow
    # the parent's shared connection pool, so it opens its own connection from
    # the platform POSTGRES_* env vars (inherited by the subprocess).
    host = os.environ.get("POSTGRES_HOST", "").strip()
    if not host:
        return ""
    port = os.environ.get("POSTGRES_PORT", "").strip() or "5432"
    from core.config import POSTGRES_DB as _cfg_db, POSTGRES_SCHEMA as _cfg_schema
    db   = os.environ.get("POSTGRES_DB",   "").strip() or _cfg_db
    user = os.environ.get("POSTGRES_USER", "").strip() or "ainxtadm"
    pwd  = os.environ.get("POSTGRES_PASSWORD", "").strip()
    # Carry both schemas so skill_files / skills_catalog resolve whether they
    # sit in `public` (legacy) or the configured schema (post schema-consolidation).
    schema = os.environ.get("POSTGRES_SCHEMA", _cfg_schema).strip() or _cfg_schema
    return (
        "postgresql" + "://" + user + ":" + pwd + "@" + host + ":" + port + "/" + db
        + f"?options=-csearch_path%3Dpublic,{schema}"
    )


def run(inputs):
    skill    = (inputs.get("skill") or "").strip()
    rel_path = (inputs.get("rel_path") or "").strip()
    if not skill or not rel_path:
        return {"error": "Both 'skill' and 'rel_path' are required."}

    try:
        import psycopg
    except ImportError:
        return {"error": "psycopg is not installed in the sandbox."}

    uri = _pg_uri()
    if not uri:
        return {"error": "POSTGRES_HOST is not set; cannot reach skill_files."}

    try:
        with psycopg.connect(uri, connect_timeout=5) as conn:
            # Diagnostic: capture where this connection actually resolves
            # tables. If read_skill_file misbehaves in an environment we
            # can't shell into, this string travels back in the error and
            # tells us the real search_path + schema without needing logs.
            try:
                sp   = conn.execute("SHOW search_path").fetchone()[0]
                curs = conn.execute("SELECT current_schema()").fetchone()[0]
                _diag = f"search_path={sp!r} current_schema={curs!r}"
            except Exception:
                _diag = "search_path=<unavailable>"
            row = conn.execute(
                "SELECT content, size_bytes, abs_path "
                "FROM skill_files WHERE skill_name = %s AND rel_path = %s",
                (skill, rel_path),
            ).fetchone()
            if not row:
                exists = conn.execute(
                    "SELECT 1 FROM skills_catalog WHERE name = %s",
                    (skill,),
                ).fetchone()
                if not exists:
                    # If the skill really is attached but skills_catalog looks
                    # empty from here, the connection is resolving the wrong
                    # schema. _diag makes that visible instead of leaving the
                    # LLM to guess ("blocked by policy").
                    return {"error": f"unknown skill '{skill}' ({_diag})"}
                avail = [
                    r[0] for r in conn.execute(
                        "SELECT rel_path FROM skill_files WHERE skill_name = %s "
                        "ORDER BY rel_path LIMIT 20",
                        (skill,),
                    ).fetchall()
                ]
                return {
                    "error": (
                        f"skill '{skill}' has no bundled file '{rel_path}'. "
                        f"Available: {', '.join(avail) if avail else '(none)'}. "
                        f"Only use files listed here — do not guess names from "
                        f"other skills or examples."
                    ),
                }
    except Exception as exc:
        return {"error": f"DB lookup failed: {exc}"}

    content, size_bytes, abs_path = row
    if size_bytes and size_bytes > _MAX_BYTES:
        return {
            "error": (
                f"file too large ({size_bytes} bytes); execute via code_executor "
                f"using abs_path {abs_path} instead of reading it."
            ),
            "abs_path": abs_path,
        }

    return {
        "result": f"# {skill}/{rel_path} ({size_bytes} bytes)\\n\\n{content}",
        "skill":    skill,
        "rel_path": rel_path,
        "abs_path": abs_path,
        "size_bytes": size_bytes,
    }
'''

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PLATFORM_TOOLS = [
    {
        "name": "code_executor",
        "description": (
            "Execute Python code in a sandboxed subprocess and return any files it generates. "
            "Install Python packages on demand: "
            "subprocess.run([sys.executable, '-m', 'pip', 'install', 'pkg']). "
            "Prefer pure-Python libraries: python-pptx, reportlab, python-docx, openpyxl, "
            "Pillow, matplotlib. "
            "Do NOT shell out to platform-specific CLIs (libreoffice, unoconv, soffice, "
            "pdftoppm, pdflatex, node, npm, bash, etc.) — they are not guaranteed to be "
            "installed and will crash the run on Windows hosts. "
            f"ALWAYS write output files DIRECTLY into the injected OUTPUT_DIR variable "
            f"({NO_SUBDIRS_CLAUSE}; write files at its root). "
            "NEVER hardcode '/tmp' or any other absolute path — '/tmp' does not exist on "
            "Windows. Use: os.path.join(OUTPUT_DIR, 'filename.ext'). "
            "Returns generated_files[] with {filename, download_url, format}. "
            "When linking the file for the user, ALWAYS use the `download_url` value VERBATIM "
            "as the markdown href, e.g. `[filename](download_url)`. Do NOT construct your own "
            "URL from the filename."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        f"Python code to execute. Write output files DIRECTLY to OUTPUT_DIR "
                        f"({NO_SUBDIRS_CLAUSE}). Example: import os; "
                        "open(os.path.join(OUTPUT_DIR,'out.txt'),'w').write('hi')"
                    ),
                }
            },
            "required": ["code"],
        },
        "service": "platform",
        "code": _CODE_EXECUTOR_CODE,
    },
    {
        "name": "read_skill_file",
        "description": (
            "Fetch a specific bundled file from an attached skill (reference docs "
            "or script source). Call on demand when the SKILL.md manifest indicates "
            "a file would help. Returns the file content as a string. For bundled "
            "scripts, prefer invoking them via code_executor with their abs_path "
            "rather than reading the source. IMPORTANT: Only request files that are "
            "listed in the manifest for THIS skill — do not guess file names from "
            "other skills or from examples in this description."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "Skill name as listed in the ## Skills section, e.g. 'pptx'.",
                },
                "rel_path": {
                    "type": "string",
                    "description": (
                        "Relative path from the skill root, e.g. 'editing.md' "
                        "or 'scripts/thumbnail.py'. Must match a path from the "
                        "manifest exactly — do not guess names from other skills."
                    ),
                },
            },
            "required": ["skill", "rel_path"],
        },
        "service": "platform",
        "code": _READ_SKILL_FILE_CODE,
    },

    # ------------------------------------------------------------------ #
    # Draft tools — require LLM_PROXY_URL to be configured                #
    # ------------------------------------------------------------------ #
    # ARCH-F-014 (2026-08-26): still draft, and for a second reason beyond
    # the LLM_PROXY_URL requirement noted above — _WEB_SEARCH_CODE below
    # posts to "/search", but services/llm_proxy/main.py's actual endpoint
    # is "/llm/web-search" (see WebSearchRequest / async def web_search()
    # there). The path was never wired up, so this tool would fail every
    # call even with draft removed. When it IS implemented, department
    # isolation is already in place at app/api/catalog.py::list_tools_catalog
    # (_web_search_visible_to / ABSTUDIO_WEB_SEARCH_DEPARTMENTS, default
    # "marketing") so un-drafting this entry does not make it visible to
    # every department by default.
    {
        "name": "web_search",
        "draft": True,
        "service": "platform",
        "description": "Search the web via the internal proxy. Returns titles, snippets, and URLs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Search query"},
                "max_results": {"type": "integer", "description": "Max results to return (max 20)", "default": 10},
            },
            "required": ["query"],
        },
        "code": _WEB_SEARCH_CODE,
    },

    {
        "name": "file_search",
        "draft": True,
        "service": "platform",
        "description": "Search the internal knowledge base using hybrid semantic + keyword search.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query":     {"type": "string",  "description": "Search query"},
                "namespace": {"type": "string",  "description": "Knowledge base namespace/repo filter (optional)"},
                "top_k":     {"type": "integer", "description": "Number of results to return (max 20)", "default": 6},
            },
            "required": ["query"],
        },
        "code": _FILE_SEARCH_CODE,
    },

    {
        "name": "execute_code",
        "draft": True,
        "service": "platform",
        "description": (
            "Execute Python code in a sandboxed subprocess. Returns stdout, stderr, "
            "exit code, and any download links for files the code produced. "
            "ALWAYS write output files to the injected OUTPUT_DIR environment variable "
            "(also the subprocess CWD) — e.g. os.path.join(os.environ['OUTPUT_DIR'], 'out.pptx'). "
            "Never hardcode '/tmp', absolute Windows paths, or repo-relative paths."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code":     {"type": "string",  "description": "Python code to execute. Write output files to OUTPUT_DIR (env var) — they are returned as download links."},
                "language": {"type": "string",  "description": "python (only supported value)", "default": "python"},
                "timeout":  {"type": "integer", "description": "Timeout in seconds (max 60)", "default": 30},
            },
            "required": ["code"],
        },
        "code": _EXECUTE_CODE_CODE,
    },

    {
        "name": "llm_generate",
        "draft": True,
        "service": "platform",
        "description": "Send a prompt to the best available LLM via the internal proxy and return the response.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt":     {"type": "string",  "description": "The prompt to send to the LLM"},
                "model_hint": {"type": "string",  "description": "Preferred model hint: fast | smart | balanced", "default": "balanced"},
                "max_tokens": {"type": "integer", "description": "Max tokens in the response", "default": 1024},
            },
            "required": ["prompt"],
        },
        "code": _LLM_GENERATE_CODE,
    },
]
