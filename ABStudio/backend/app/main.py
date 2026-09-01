# SPDX-License-Identifier: Apache-2.0
"""FastAPI application entry point.

All domain routes live in app/api/*. This file only wires them together.
"""
import asyncio
import json
import os
import shutil
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(dotenv_path=_PLATFORM_ROOT / ".env", override=False)

# Expose the parent AiNxt platform root on sys.path so the standalone
# Build Studio backend can import shared credential modules
# (core.platform_credentials, store.credential_vault) used by
# workflow_repo.get_all_connection_env_vars() to pull per-user vault tokens
# (GitLab PAT, Atlassian creds, org-level secrets) into the sandbox env.
# Without this, those imports raise ImportError, the surrounding try/except
# swallows it silently, and tools run with no credentials.
_platform_root_str = str(_PLATFORM_ROOT)
if _platform_root_str not in sys.path:
    sys.path.insert(0, _platform_root_str)

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.core import workflow_repo
from app.engine import get_engine
from app.services import trigger_scheduler
from app.tools.canonical_tools import seed_canonical_tools, seed_canonical_skills
from app.core.config import factory_model
from app.models import AuthenticatedUser
from app.api.deps import require_access
from agent_factory.pipeline import seed_catalogs_from_legacy, AGENTS_FILE, _call_llm, _coerce_to_text
from app.api import (
    execution, chat, generation, documents,
    workflows, templates, agents, agent_templates, mcp,
    catalog, triggers, factories, agent_chat,
    agent_sample,  # Per-agent Sample Document upload / get / delete.
    kb,  # Build-Studio-only KB upload proxy (auto-approve, multi-file).
    template_admin,  # Optional feature-flagged template editor. To remove: delete this import + the include below + app/api/template_admin.py.
    loops,  # Loop Engineering P1 — CRUD + governance for Loop / Goal. /loops/{id}/run-stream lands in P2.
    governance,  # Governance/approval bridge for Build Studio artifacts.
)

# Route Build Studio logs through the shared gateway logger (core/logger.py)
# so every AB Studio record lands in the same structured, rotating agent.log
# as the rest of the platform (Gateway, CLI, IDE, Chats), carrying the
# structlog context (request_id, chat_id, user_id, span_id, client_source, …).
# Importing core.logger auto-runs _configure_logging_once(); we must NOT call
# logging.basicConfig() here or AB Studio would also emit plain-text lines to
# stdout via the root logger (double-logging + unstructured output).
from core.logger import logger

GENERATED_FILES_DIR = os.path.abspath(
    os.getenv(
        "GENERATED_FILES_DIR",
        # ABStudio/tmp — moved out of backend/data so users can find
        # generated artifacts without spelunking into the package tree.
        # Also pinned as the sandbox subprocess CWD in agent_factory/pipeline.py.
        os.path.join(os.path.dirname(__file__), "..", "..", "tmp"),
    )
)
os.makedirs(GENERATED_FILES_DIR, exist_ok=True)
os.environ["GENERATED_FILES_DIR"] = GENERATED_FILES_DIR

# Generated files survive for this many seconds after creation (mtime).
# Within the window the same file can be downloaded any number of times;
# after it expires the download endpoint returns 410 and a background
# sweeper removes the file from disk. Default: 24h.
GENERATED_FILES_TTL_SECONDS = int(os.getenv("GENERATED_FILES_TTL_SECONDS", "86400"))


def _is_expired(path: Path, now: float | None = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except FileNotFoundError:
        return True
    return age > GENERATED_FILES_TTL_SECONDS


# ── Per-user artifact isolation (Broken Access Control fix) ──────────────────
# Generated files used to live flat in GENERATED_FILES_DIR named
# ``{run_id}_{name}`` where run_id was only 8 hex chars (32 bits). Because the
# download endpoint checked authentication but not ownership, any authenticated
# user who saw or guessed a disk name could fetch another user's artifact
# (IDOR). We now nest each artifact under a per-user directory whose name is
# derived deterministically from the caller's identity, and enforce that a
# caller can only reach files inside THEIR OWN owner-dir.
#
# No server secret is involved: security does not rest on the owner tag being
# unguessable by third parties. At download time we recompute the expected tag
# from the *authenticated* caller's id and refuse anything that resolves
# outside it — an attacker cannot make the server serve a file under a tag they
# are not authenticated as. The tag is hashed (not the raw user id) only to
# avoid leaking the raw id in download URLs.
#
# Legacy flat files (created before this change, sitting directly under the
# base dir with no owner-dir) remain downloadable by any authenticated user —
# they predate the isolation scheme and age out naturally via the TTL sweeper.
#
# The algorithm itself lives in ``app.owner_tag`` — a stdlib-only module — so the
# CLI runtime and the gateway can share this exact definition instead of
# hand-copying it. Re-exported here for backward compatibility: existing callers
# and tests reference ``app.main.owner_tag``. Prefer importing from
# ``app.owner_tag`` in new code.
from app.owner_tag import (  # noqa: E402  (re-export, must follow the comment above)
    _OWNER_TAG_LEN,
    is_generated_path_allowed,
    owner_tag,
)


def rehome_generated_file(disk_name: str, user_id: str) -> str:
    """Move a just-created flat artifact into the caller's owner-dir.

    ``disk_name`` is a bare name already living directly under
    ``GENERATED_FILES_DIR`` (e.g. ``a1b2c3d4_deck.pptx``). Returns the new
    relative key the download URL should use:
      - ``"{owner_tag}/{disk_name}"`` on success, or
      - the original ``disk_name`` unchanged if ``user_id`` is empty (e.g.
        standalone/local-dev with no real identity) or the move fails — so the
        file stays reachable via the legacy flat path rather than being lost.

    The artifact filename itself is never altered; only a parent directory is
    added.
    """
    tag = owner_tag(user_id)
    if not tag:
        return disk_name
    base = Path(GENERATED_FILES_DIR).resolve()
    src = (base / disk_name).resolve()
    try:
        # Guard against a disk_name that escapes the base dir.
        src.relative_to(base)
    except ValueError:
        return disk_name
    if not src.is_file():
        return disk_name
    owner_dir = base / tag
    try:
        owner_dir.mkdir(parents=True, exist_ok=True)
        dest = owner_dir / src.name
        shutil.move(str(src), str(dest))
        # Anchor the TTL clock to ingestion time. A cross-filesystem move
        # falls back to copy2+unlink, preserving the source mtime; resetting
        # it here keeps the download window measured from when the artifact
        # entered the store rather than when its source was written.
        try:
            os.utime(str(dest), None)
        except OSError:
            pass
    except Exception:
        logger.exception(f'[AGENT] Failed to re-home generated file into owner dir: {disk_name}')
        return disk_name
    return f"{tag}/{src.name}"


_SKILL_BINARY_EXTENSIONS = {
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
    '.pdf', '.zip', '.whl', '.exe', '.db', '.pyc',
    '.xsd',
}
_SKILL_SKIP_FILENAMES = {'LICENSE.txt', '__init__.py'}


# ---------------------------------------------------------------------------
# QA-binary host check
# ---------------------------------------------------------------------------
# The Office skills (pptx/docx/xlsx) describe a visual-QA loop that depends on
# LibreOffice (`soffice`) for .pptx→.pdf conversion and Poppler (`pdftoppm`)
# for .pdf→images. On servers where those binaries aren't installed, calling
# them just produces a noisy "LibreOffice not found" error every run.
#
# We probe once at seed time and, when the binaries are missing, strip the
# Visual-QA / Converting-to-Images sections from the SKILL.md body and exclude
# anything under `scripts/office/` from the bundled-files manifest. The model
# never sees instructions it can't act on, so it doesn't try.

import shutil as _shutil  # local alias so the import survives module reuse

_QA_BINARY_NAMES = ("soffice", "libreoffice", "pdftoppm")

# Skills whose SKILL.md QA workflow depends on the binaries above.
_BINARY_DEPENDENT_SKILLS = {"pptx", "docx", "xlsx"}

# Replacement note injected when we strip the visual-QA section. Keeps the
# model honest about what's still available (content QA via markitdown).
_NO_VISUAL_QA_NOTICE = (
    "\n## QA (Content Only)\n\n"
    "Visual QA via LibreOffice / Poppler is unavailable in this environment "
    "(host binaries `soffice` and `pdftoppm` are not installed). Use\n\n"
    "```bash\n"
    "python -m markitdown output.pptx\n"
    "```\n\n"
    "to extract text and check for typos, leftover placeholders "
    "(`xxxx`, `lorem`, `ipsum`, `this slide layout`), missing content, or "
    "wrong order. **Do NOT attempt to convert slides to images, run "
    "`scripts/office/soffice.py`, or call `pdftoppm`** — those will fail. "
    "Trust your render and ship after a thorough content pass.\n"
)


def _qa_binaries_available() -> bool:
    """True if at least one renderer (soffice/libreoffice) AND pdftoppm exist."""
    has_office = bool(_shutil.which("soffice") or _shutil.which("libreoffice"))
    has_poppler = bool(_shutil.which("pdftoppm"))
    return has_office and has_poppler


# Computed once at module import time. Cheap (~3 PATH lookups).
_QA_BINARIES_OK = _qa_binaries_available()


_NOTICE_SENTINEL = "\x00NO_VISUAL_QA_NOTICE\x00"


def _strip_visual_qa(body: str) -> str:
    """Remove every reference to soffice / pdftoppm / LibreOffice from the body.

    Three passes:
      1. Replace the ``## QA`` block (visual-QA + Converting-to-Images
         children) with ``_NO_VISUAL_QA_NOTICE``.
      2. Delete any standalone ``## Converting to Images`` section the
         first pass didn't catch.
      3. Strip leftover lines that still mention ``soffice`` /
         ``libreoffice`` / ``pdftoppm`` / ``scripts/office/`` (the
         Dependencies block, the Reading-Content snippet, etc.) so the
         model never sees a broken hint.

    The notice itself is protected by a sentinel during pass 3 so we don't
    accidentally delete our own "LibreOffice not available" explanation.
    """
    import re

    # Track whether the source body had any binary-dependent refs so we know
    # whether to inject the explanatory notice (some skills mention soffice
    # inline without a dedicated ## QA block).
    needs_notice = bool(
        re.search(r"soffice|libreoffice|pdftoppm|scripts/office/",
                  body, flags=re.IGNORECASE)
    )

    # 1) Replace the ## QA block with a sentinel — we'll swap the real
    # notice back in at the end, after the leftover-line strip can't touch it.
    qa_pattern = re.compile(
        r"(?:^|\n)#{2,3}\s*QA[^\n]*\n.*?"
        r"(?=(?:\n#{2}\s+)|\Z)",
        flags=re.DOTALL | re.IGNORECASE,
    )
    body, n_qa = qa_pattern.subn("\n" + _NOTICE_SENTINEL + "\n", body, count=1)

    # 2) Delete any standalone "Converting to Images" section.
    conv_pattern = re.compile(
        r"(?:^|\n)#{2,3}\s*Converting to Images[^\n]*\n.*?"
        r"(?=(?:\n#{2}\s+)|\Z)",
        flags=re.DOTALL | re.IGNORECASE,
    )
    body = conv_pattern.sub("\n", body)

    # 3) Strip every line that still references a missing binary or the
    # skipped helper directory. Sentinel line is preserved untouched.
    leftover_pattern = re.compile(
        r"soffice|libreoffice|pdftoppm|scripts/office/",
        flags=re.IGNORECASE,
    )
    cleaned_lines = []
    for line in body.splitlines():
        if _NOTICE_SENTINEL in line:
            cleaned_lines.append(line)
            continue
        if leftover_pattern.search(line):
            continue  # drop this line
        cleaned_lines.append(line)
    body = "\n".join(cleaned_lines)

    # Swap the sentinel for the real notice. If the source had no ## QA
    # block but did mention the missing binaries inline, append the notice
    # at the bottom so the model knows why those refs are gone.
    if n_qa:
        body = body.replace(_NOTICE_SENTINEL, _NO_VISUAL_QA_NOTICE.strip())
    elif needs_notice:
        body = body.rstrip() + "\n\n" + _NO_VISUAL_QA_NOTICE.strip() + "\n"

    # Collapse 3+ blank lines to 2 for tidiness after the deletions.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip() + "\n"


def _should_skip_bundled_file(rel_path: str) -> bool:
    """True if a bundled file should be excluded when QA binaries are missing.

    Anything under ``scripts/office/`` depends on soffice — listing those in
    the manifest would invite the LLM to call them and trip the same error.
    """
    if _QA_BINARIES_OK:
        return False
    return rel_path.startswith("scripts/office/")


def _strip_frontmatter(text: str) -> str:
    """Drop the YAML frontmatter block at the top of SKILL.md, if present.

    The frontmatter holds ``name:`` / ``description:`` which we already
    persist as separate columns on ``skills_catalog`` — keeping it in the
    body would just be noise in the prompt.
    """
    import re
    m = re.match(r"^---\s*\n.*?\n---\s*\n", text, re.DOTALL)
    return text[m.end():] if m else text


def _describe_text_file(rel_path: str, text: str) -> str:
    """Best-effort one-line description for the manifest.

    Markdown → first non-empty line after the H1.
    Python   → first line of the module docstring, else first ``#`` comment.
    Other    → empty string.
    """
    lines = text.splitlines()
    if rel_path.endswith(".md"):
        seen_h1 = False
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# ") and not seen_h1:
                seen_h1 = True
                continue
            if stripped.startswith("#"):
                # H2/H3 etc — also acceptable as a description
                return stripped.lstrip("# ").strip()[:200]
            return stripped[:200]
        return ""

    if rel_path.endswith(".py"):
        # Walk past blank/comment lines until we hit the docstring or first code.
        in_doc = False
        doc_quote = None
        for line in lines:
            stripped = line.strip()
            if not in_doc:
                if not stripped or stripped.startswith("#") or stripped.startswith("from ") or stripped.startswith("import "):
                    # Try first comment line as a fallback if there's no docstring
                    if stripped.startswith("#"):
                        return stripped.lstrip("# ").strip()[:200]
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    doc_quote = stripped[:3]
                    body = stripped[3:]
                    # Single-line docstring case: """foo"""
                    if body.endswith(doc_quote) and len(body) > 3:
                        return body[:-3].strip()[:200]
                    if body:
                        return body.strip()[:200]
                    in_doc = True
                    continue
                # Code reached before any docstring
                return ""
            else:
                if doc_quote and doc_quote in stripped:
                    inner = stripped.split(doc_quote, 1)[0].strip()
                    return inner[:200] if inner else ""
                if stripped:
                    return stripped[:200]
        return ""

    return ""


def _collect_skill_assets(skill_dir, skill_name: str = ""):
    """Return ``(skill_md_body, bundled_files)`` for a skill folder.

    ``skill_md_body`` — SKILL.md with the YAML frontmatter stripped. Goes
    into ``skills_catalog.content``.
    ``bundled_files`` — every other text file as a dict with the keys the
    ``skill_files`` repo helper expects (rel_path, content, size_bytes,
    description, kind, abs_path). Goes into ``skill_files``.

    Binary files, ``LICENSE.txt``, and ``SKILL.md`` itself are skipped.

    Binary-dependent skills (pptx/docx/xlsx) get their Visual-QA section
    rewritten and their soffice-using helper scripts excluded when the
    host lacks ``soffice`` / ``pdftoppm`` — see ``_qa_binaries_available``.
    The check is host-wide and run once at module import (``_QA_BINARIES_OK``).
    """
    from pathlib import Path
    skill_dir = Path(skill_dir)
    skill_md_path = skill_dir / "SKILL.md"
    if not skill_md_path.is_file():
        raise FileNotFoundError(f"SKILL.md not found in {skill_dir}")

    body = _strip_frontmatter(skill_md_path.read_text(encoding="utf-8")).strip()

    # Strip the Visual-QA + Converting-to-Images sections when the host
    # can't actually run them. Only applies to skills that ship a binary-
    # dependent QA loop; other skills are unchanged.
    if not _QA_BINARIES_OK and skill_name in _BINARY_DEPENDENT_SKILLS:
        body = _strip_visual_qa(body)

    bundled: list = []
    for f in sorted(skill_dir.rglob("*")):
        if not f.is_file() or f == skill_md_path:
            continue
        if f.name in _SKILL_SKIP_FILENAMES:
            continue
        if f.suffix.lower() in _SKILL_BINARY_EXTENSIONS:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # Postgres TEXT columns reject NUL (\x00) bytes and abort the whole
        # INSERT. Because upsert_skill (skills_catalog) commits BEFORE
        # upsert_skill_files, a single NUL-containing file would roll back the
        # entire skill_files transaction while leaving the skill listed in the
        # catalog — the skill then appears attachable/manifested but every
        # read_skill_file call fails. Strip NULs (they're never meaningful in
        # a text reference/script) so one bad byte can't blank a skill.
        if "\x00" in text:
            text = text.replace("\x00", "")
        rel_path = f.relative_to(skill_dir).as_posix()
        # Skip soffice-dependent helper scripts when the host has no
        # LibreOffice — listing them in the manifest would just invite the
        # LLM to call them and trip the same "binary not found" error.
        if _should_skip_bundled_file(rel_path):
            continue
        kind = "script" if rel_path.startswith("scripts/") else "reference"
        bundled.append({
            "rel_path":    rel_path,
            "content":     text,
            "size_bytes":  len(text.encode("utf-8")),
            "description": _describe_text_file(rel_path, text),
            "kind":        kind,
            "abs_path":    str(f.resolve()).replace("\\", "/"),
        })

    return body, bundled


async def _seed_bundled_skills() -> None:
    """Seed the bundled skill folders into skills_catalog.

    Bundles SKILL.md plus all supplementary text files (reference docs,
    scripts, code examples) into a single content blob. Uses upsert so
    existing records are refreshed on every restart.

    Skills live under skills/ainxt-skills/. The seed path was previously stale,
    so the seed silently skipped on every start and the catalogue stayed empty.
    Fixed here — expect the bundled first-party skills to appear in the catalogue
    now, where before there were none.
    """
    import re

    skills_dir = Path(__file__).parent.parent.parent / "skills" / "ainxt-skills"
    if not skills_dir.exists():
        logger.warning(f'[AGENT] Bundled skills directory not found at {skills_dir} — skipping seed')
        return

    if _QA_BINARIES_OK:
        logger.info('[AGENT] QA binaries detected (soffice + pdftoppm). Office skills will include the visual-QA workflow.')
    else:
        logger.debug('[AGENT] QA binaries missing (soffice and/or pdftoppm not on PATH). Stripping visual-QA section + scripts/office/* from pptx/docx/xlsx skills — model will use markitdown for content QA only.')

    # Only the skills that ship in this repo. Anything not listed falls back to
    # the default category, so adding a skill folder does not require editing
    # this map first.
    category_map = {
        "doc-coauthoring": "productivity",
        "dslar-clause-chunking": "compliance",
        "dslar-clause1-validation": "compliance",
        "dslar-clauses-10-13-validation": "compliance",
        "dslar-clauses-2-5-validation": "compliance",
        "dslar-clauses-6-9-validation": "compliance",
        "dslar-image-enrichment": "compliance",
        "dslar-pdf-extraction": "compliance",
        "dslar-report-pdf": "compliance",
    }

    seeded = 0
    for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        skill_md_path = skill_dir / "SKILL.md"
        if not skill_md_path.exists():
            continue
        try:
            folder = skill_dir.name
            skill_md_raw = skill_md_path.read_text(encoding="utf-8")
            name = folder
            description = ""

            fm = re.match(r"^---\s*\n(.*?)\n---", skill_md_raw, re.DOTALL)
            if fm:
                for line in fm.group(1).splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:") and not description:
                        description = line.split(":", 1)[1].strip()

            body, bundled = _collect_skill_assets(skill_dir, skill_name=name)
            category = category_map.get(folder, "general")
            # SKILL.md body only — bundled files now live in skill_files and
            # the LLM pulls them on demand via read_skill_file.
            #
            # The catalog row MUST be written first: skill_files.skill_name has
            # a FK -> skills_catalog(name). But that ordering means a failure in
            # the file write (separate transaction) would leave a "phantom
            # skill" — listed/attachable/manifested, yet every read_skill_file
            # 404s. Guard the file write and roll the catalog row back on
            # failure so the skill either seeds fully or not at all.
            await workflow_repo.upsert_skill(
                name=name,
                content=body,
                description=description,
                category=category,
                generated=False,
                # Built-in seeder: mark as ``builtin`` so the Skills tab
                # displays these under the Built-in filter regardless of
                # whether an older row survived a schema migration without a
                # source value.
                source="builtin",
            )
            try:
                await workflow_repo.upsert_skill_files(name, bundled)
            except Exception:
                # ON DELETE CASCADE clears any partial skill_files rows too.
                await workflow_repo.delete_skill(name)
                raise
            seeded += 1
        except Exception:
            logger.warning(f'[AGENT] Failed to seed AiNxt skill from {skill_dir}', exc_info=True)

    logger.info(f'[AGENT] Seeded/updated {seeded} AiNxt skill(s) in catalog')


async def _migrate_orphaned_agents_from_registry() -> None:
    """
    One-time idempotent migration: any agent that exists in the legacy
    AgentRegistry (``backend/data/agents.json``) but not in the postgres
    ``agents`` table gets imported. After this runs once successfully,
    new deploys go straight to postgres and the JSON file becomes a
    read-only artefact.
    """
    if not AGENTS_FILE.exists():
        return
    try:
        data = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f'[AGENT] orphan migration: failed to read {AGENTS_FILE}: {exc}')
        return
    if not isinstance(data, dict):
        return

    migrated = 0
    skipped = 0
    for agent_id, a in data.items():
        if not isinstance(a, dict):
            continue
        existing = await workflow_repo.get_agent_by_id(agent_id)
        if existing:
            skipped += 1
            continue
        try:
            base_prompt = a.get("system_prompt", "") or a.get("instructions", "")
            persona = (a.get("persona") or "").strip()
            instructions = base_prompt + (f"\n\nPersona: {persona}" if persona else "")
            await workflow_repo.create_agent({
                "id":            agent_id,
                "name":          a.get("name", "Recovered"),
                "description":   a.get("description", ""),
                "instructions":  instructions,
                "provider":      "custom",
                "model_name":    a.get("model") or factory_model(),
                "tools":         a.get("tools", []),
                "skills":        a.get("skills", []),
                "guardrails":    a.get("guardrails", {}),
                "memory_config": a.get("memory_config", {}),
            }, "local-dev-user")
            migrated += 1
        except Exception as exc:
            logger.warning(f"[AGENT] orphan migration: failed for '{agent_id}': {exc}")

    if migrated:
        logger.info(f'[AGENT] Migrated {migrated} orphan agent(s) from {AGENTS_FILE.name} into postgres ({skipped} already present)')


# Background sweeper interval. Files only get deleted on the next tick
# after they expire, so this caps the staleness window.
_GENERATED_FILES_SWEEP_INTERVAL_SECONDS = 60


async def _generated_files_sweeper() -> None:
    """Periodically delete generated files older than the TTL.

    Artifacts live either directly under the base dir (legacy flat files) or
    one level down inside a per-user owner-dir (``{owner_tag}/{name}`` — see
    ``owner_tag``). We sweep both: expired files anywhere at depth 0 or 1 are
    removed, and owner-dirs left empty afterwards are pruned so the tree does
    not accumulate stale directories.
    """
    base = Path(GENERATED_FILES_DIR)

    def _sweep_file(entry: Path, now: float) -> None:
        if _is_expired(entry, now):
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                logger.exception(f'[AGENT] Sweeper failed to delete {entry}')

    while True:
        try:
            await asyncio.sleep(_GENERATED_FILES_SWEEP_INTERVAL_SECONDS)
            now = time.time()
            for entry in base.iterdir() if base.is_dir() else ():
                if entry.is_file():
                    _sweep_file(entry, now)
                elif entry.is_dir():
                    # Per-user owner-dir: sweep the files inside it, then drop
                    # the dir if it is now empty.
                    for child in entry.iterdir():
                        if child.is_file():
                            _sweep_file(child, now)
                    try:
                        next(entry.iterdir())
                    except StopIteration:
                        try:
                            entry.rmdir()
                        except OSError:
                            pass
                    except OSError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('[AGENT] Generated files sweeper iteration failed')


async def _cli_workspace_sweeper() -> None:
    """Periodically delete expired per-run CLI workspaces.

    Each CLI run gets a private directory (holding its MCP config, prompt file
    and any git checkout). They are kept after the run so a failure can be
    inspected, so something has to reclaim them. Also expires stale MCP run
    sessions, which are normally revoked by the runner but could leak if a worker
    were killed mid-run.

    A no-op when CLI mode is off, so this costs nothing in the default config.
    """
    interval = 3600  # hourly is ample for a 24h default TTL
    while True:
        try:
            await asyncio.sleep(interval)
            from app.cli_runtime.config import cli_mode_enabled
            if not cli_mode_enabled():
                continue
            from app.cli_runtime.session import get_registry
            from app.cli_runtime.workspace import sweep_workspaces

            expired_sessions = get_registry().sweep_expired()
            removed, kept = await asyncio.to_thread(sweep_workspaces)
            if removed or expired_sessions:
                logger.info(
                    '[AGENT] CLI sweeper: reclaimed resources',
                    workspaces_removed=removed, workspaces_kept=kept,
                    sessions_expired=expired_sessions,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('[AGENT] CLI workspace sweeper iteration failed')


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Validate + publish the CORS allow-list before anything else, so a bad
    # policy aborts startup instead of serving traffic. Only runs when this
    # app is actually served (standalone); the gateway mounts our routers and
    # never starts this lifespan. See _resolve_cors_origins().
    _apply_cors_origins()

    # Raise the default thread-pool size so 100-200 concurrent users don't
    # queue on Python's stock asyncio.to_thread executor (min(32, cpu+4)).
    # Every psycopg call is wrapped in to_thread; the pool needs to be at
    # least as large as the sum of Postgres pool max_sizes so a request
    # never blocks here while a free DB connection is available.
    # Sized for: workflow_repo (30) + checkpoint (25) + agent_chat (25)
    # = 80 worst-case concurrent DB callers + headroom for non-DB I/O.
    from concurrent.futures import ThreadPoolExecutor
    workers = int(os.getenv("AGENTCHAIN_THREADPOOL_WORKERS", "128"))
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="agentchain-io",
    ))
    logger.info(f'[AGENT] Default asyncio executor sized to {workers} workers')

    # DB init and tool/skill seeding MUST complete before get_engine().startup()
    # runs — NativeEngine._warm_singleton_tool_cache() looks up "code_executor"
    # and "read_skill_file" in tools_catalog immediately on startup and caches
    # whatever it finds (including an empty result) for the life of the
    # process. If the pool isn't open yet (workflow_repo.init_db() not called)
    # or the canonical rows aren't seeded yet, that lookup silently fails
    # (errors are swallowed by _resolve_catalog_tools' return_exceptions=True)
    # and the cache is poisoned with [] forever — permanently hiding
    # code_executor from every agent node that relies on auto-injection.
    # NOTE: any legacy ``public`` copies of ABStudio tables must be consolidated
    # into ``ainxt`` by the operator BEFORE deploy (one-time), via
    # db/sql/consolidate_abstudio_public_to_ainxt.sql. The app does not migrate
    # schema at startup.
    await workflow_repo.init_db()
    _startup_coros = [
        (seed_catalogs_from_legacy(), "Catalog seed"),
        (seed_canonical_tools(), "Canonical tools seed"),
        (seed_canonical_skills(), "Canonical skills seed"),
        (_seed_bundled_skills(), "AiNxt skills seed"),
        (_migrate_orphaned_agents_from_registry(), "Orphan agent migration"),
        (trigger_scheduler.init_scheduler(), "Trigger scheduler init"),
    ]
    for coro, label in _startup_coros:
        try:
            await coro
        except Exception:
            logger.exception(f'[AGENT] {label} failed')

    await get_engine().startup()
    await agent_chat.startup()
    sweeper_task = asyncio.create_task(_generated_files_sweeper())

    # ── CLI execution mode readiness ────────────────────────────────────────
    # Report the state of ABSTUDIO_CLI_MODE at startup. When the flag is on but
    # the environment is incomplete (no binary, no API key), that is stated here
    # rather than surfacing later as a failed user request.
    try:
        from app.cli_runtime.config import preflight as _cli_preflight
        _cli_status = _cli_preflight()
        if not _cli_status.enabled:
            logger.info('[AGENT] CLI execution mode is OFF (ABSTUDIO_CLI_MODE) — using the in-process engine')
        elif _cli_status.ready:
            logger.info(
                '[AGENT] CLI execution mode is ON — every agent turn will run in a '
                'spawned ainxt process',
                **_cli_status.as_log_fields(),
            )
            for _warning in _cli_status.warnings:
                logger.warning(f'[AGENT] CLI mode warning: {_warning}')
        else:
            logger.error(
                '[AGENT] CLI execution mode is ON but NOT READY — agent runs will '
                'fail until these are fixed',
                **_cli_status.as_log_fields(),
            )
    except Exception:
        logger.exception('[AGENT] CLI mode preflight failed')

    cli_sweeper_task = asyncio.create_task(_cli_workspace_sweeper())

    yield
    sweeper_task.cancel()
    cli_sweeper_task.cancel()
    for _task in (sweeper_task, cli_sweeper_task):
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    try:
        from app.cli_runtime.session import get_registry as _cli_registry
        _cli_registry().clear()
    except Exception:
        pass
    try:
        await trigger_scheduler.shutdown_scheduler()
    except Exception:
        pass
    await agent_chat.shutdown()
    await get_engine().shutdown()
    await workflow_repo.close_db()


app = FastAPI(
    title="Agent Chain Workflow Builder",
    description="Backend API for visual multi-agent workflow builder",
    version="2.0.0",
    lifespan=_lifespan,
)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Emit HSTS and other security headers on every response (Checkmarx: Missing HSTS Header).

    HSTS is set UNCONDITIONALLY, not gated on ``request.url.scheme == "https"``.
    Behind a reverse proxy / load balancer that terminates TLS upstream (the
    normal production deployment shape here), uvicorn sees a plain HTTP
    connection from the proxy and ``request.url.scheme`` reads back as
    ``"http"`` unless ``--proxy-headers`` + ``--forwarded-allow-ips`` are
    threaded through exactly right — so the old guard silently dropped the
    header on every real HTTPS deployment, which is exactly the scenario HSTS
    exists to protect. Per the HSTS spec, browsers ignore the header entirely
    when it arrives over a genuine (unproxied) plain-HTTP connection, so
    sending it unconditionally is also safe for local/dev HTTP use.
    """
    # DAST fix — Missing Security Headers: Content-Security-Policy, mirroring
    # gateway.py's SecurityHeadersMiddleware. /docs, /redoc, /openapi.json are
    # exempt because Swagger UI / ReDoc load their assets from
    # cdn.jsdelivr.net, which a same-origin default-src would block.
    _CSP_EXEMPT_PATH_PREFIXES = ("/docs", "/redoc", "/openapi.json")
    _CONTENT_SECURITY_POLICY = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' blob: data:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=(), payment=()"
        if not request.url.path.startswith(self._CSP_EXEMPT_PATH_PREFIXES):
            response.headers["Content-Security-Policy"] = self._CONTENT_SECURITY_POLICY
        return response


# ── CORS ─────────────────────────────────────────────────────────────────────
# NOTE: this middleware only applies when ABStudio is run as its own app
# (``run.py`` / ``uvicorn app.main:app`` on :8002 — development only). In
# production the gateway serves :8000, imports ABStudio's *routers* only
# (see gateway.py "ABStudio — direct router mount"), and applies its own CORS
# policy from CORS_ALLOWED_ORIGINS. Keep this policy strict anyway so the
# standalone path can never become the weak link if it is ever deployed.
#
# Security review (CORS default): the previous fallback list included
# "file://" and "null" with allow_credentials=True. ``Origin: null`` is NOT a
# trustworthy marker — any site can force it via a sandboxed iframe or a
# data: URL — so allowing it with credentials effectively allows every origin
# to make credentialed cross-origin calls. Both values are removed and must
# never be re-added.
_ALLOW_ORIGINS_ENV = "CORS_ALLOW_ORIGINS"

# Origins that must never be allowed alongside allow_credentials=True.
_CORS_FORBIDDEN_ORIGINS = ("null", "file://")


def _resolve_cors_origins() -> list[str]:
    """Resolve and validate the CORS allow-list, or raise RuntimeError.

    SEC-F-030 (2026-08-26): the dev-mode localhost fallback is removed. The
    previous behaviour fell back to a hardcoded localhost allow-list whenever
    CORS_ALLOW_ORIGINS was unset AND APP_ENV was (or defaulted/typo'd towards)
    a dev-like value — a misconfigured deployment env variable could silently
    downgrade a staging/prod-adjacent box to a permissive local-only policy
    instead of failing loudly. There is no longer any environment in which
    this process starts without an explicit, operator-supplied origin list;
    local development also sets CORS_ALLOW_ORIGINS explicitly (e.g. in
    ABStudio/backend/.env — see run.py) rather than relying on a fallback.
    """
    origins = [o.strip() for o in os.getenv(_ALLOW_ORIGINS_ENV, "").split(",") if o.strip()]

    if not origins:
        raise RuntimeError(
            f"{_ALLOW_ORIGINS_ENV} is not set. Refusing to start with a default "
            "CORS policy. Set an explicit comma-separated origin list, e.g. "
            f"{_ALLOW_ORIGINS_ENV}=http://localhost:5173,http://localhost:3000 "
            "for local development, or "
            f"{_ALLOW_ORIGINS_ENV}=https://ainxt.example.com for a deployed environment."
        )

    # "*" cannot be combined with credentials: browsers reject the pair, and
    # Starlette would echo the caller's Origin back, defeating the allow-list.
    if "*" in origins:
        raise RuntimeError(
            f"{_ALLOW_ORIGINS_ENV} must not contain '*' — a wildcard origin is "
            "incompatible with allow_credentials=True. List exact origins instead."
        )

    # Defence in depth: reject the values this fix exists to keep out, even if
    # they are supplied explicitly via the environment.
    forbidden = sorted({o for o in origins if o.lower() in _CORS_FORBIDDEN_ORIGINS})
    if forbidden:
        raise RuntimeError(
            f"{_ALLOW_ORIGINS_ENV} must not contain {forbidden} — these origins are "
            "attacker-controllable (any site can send 'Origin: null' from a "
            "sandboxed iframe) and are unsafe with allow_credentials=True."
        )

    return origins


# Deny-all until the startup hook validates and populates it.
#
# Validation deliberately runs at STARTUP, not at import. gateway.py imports
# helpers from this module (_seed_bundled_skills,
# _migrate_orphaned_agents_from_registry) during its own startup; raising at
# import time would break those imports in production, where this app object
# is never served and CORS_ALLOW_ORIGINS is legitimately unset (the gateway
# has its own CORS_ALLOWED_ORIGINS). Deferring scopes the check to the
# standalone path that actually serves this app.
#
# Starlette stores this exact list object and does a membership test per
# request (see starlette/middleware/cors.py: `origin in self.allow_origins`),
# so _apply_cors_origins() repopulates it IN PLACE. Do not rebind _origins to
# a new list — the middleware would keep pointing at the old empty one.
_origins: list[str] = []


def _apply_cors_origins() -> None:
    """Validate the allow-list and publish it to the live middleware."""
    resolved = _resolve_cors_origins()
    _origins[:] = resolved          # in-place: preserves the shared reference
    logger.info("[CORS] allow-list active: %s", resolved)


app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)

_routers = [
    execution.router, chat.router, generation.router, documents.router,
    workflows.router, templates.router, agents.router, agent_templates.router,
    mcp.router, catalog.router, triggers.router, factories.router,
    agent_chat.router,
    agent_sample.router,  # Per-agent Sample Document (look-and-feel reference).
    kb.router,  # Build-Studio-only KB upload proxy — auto-approve, multi-file.
    template_admin.router,  # Optional feature-flagged template editor (env: TEMPLATES_EDITABLE).
    loops.router,  # Loop Engineering P1 — /loops + /goals CRUD and governance.
    governance.router,  # Governance/approval submit + status for Build Studio artifacts.
]
for _r in _routers:
    app.include_router(_r)

# The MCP tool plane a spawned ainxt CLI calls back into (ABSTUDIO_CLI_MODE).
# Registered unconditionally: the route is inert unless a live run session
# exists, and every request must present that run's bearer token. Mounting it
# only when the flag is on would mean a mid-flight flag flip left the route
# missing, and the run's tools would silently vanish.
try:
    from app.cli_runtime.mcp_router import router as _cli_mcp_router
    app.include_router(_cli_mcp_router)
except Exception:
    logger.exception('[AGENT] could not mount the CLI MCP router — CLI mode will not work')


@app.get("/generated-files/{filename:path}")
async def download_generated_file(
    filename: str,
    current_user: AuthenticatedUser = Depends(require_access),
):
    """
    Download a generated artifact.

    Access control (Broken Access Control / IDOR fix): artifacts are stored
    under a per-user directory (``{owner_tag}/{name}``, see ``owner_tag``). A
    caller may only fetch files inside THEIR OWN owner-dir — the tag is
    recomputed from the authenticated identity, so a name belonging to another
    user resolves to a ``404`` (we return 404 rather than 403 so the endpoint
    does not confirm the existence of another user's file). Legacy flat files
    (directly under the base dir, no owner-dir — created before this change)
    stay downloadable by any authenticated user and age out via the TTL.

    The file remains downloadable for ``GENERATED_FILES_TTL_SECONDS`` after
    it was created (mtime-anchored). Repeated downloads within the window
    are allowed. Once expired the file is deleted and subsequent requests
    return ``410 Gone`` so the UI can show "expired" instead of a generic
    404. A background sweeper (see ``_generated_files_sweeper``) removes
    expired files even if no one re-hits the endpoint.
    """
    base = Path(GENERATED_FILES_DIR).resolve()
    base.mkdir(parents=True, exist_ok=True)
    target = (base / filename).resolve()
    try:
        rel = target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Ownership gate (legacy flat files allowed; owner-dir files scoped to the
    # caller; deeper nesting rejected). 404 rather than 403 so we never confirm
    # the existence of another user's artifact.
    if not is_generated_path_allowed(rel.parts, current_user.id):
        raise HTTPException(status_code=404, detail="File not found")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    if _is_expired(target):
        # Lazy cleanup: if the file is on disk but past TTL, drop it now so
        # we don't have to wait for the sweeper's next tick.
        try:
            target.unlink(missing_ok=True)
        except Exception:
            logger.exception(f'[AGENT] Failed to delete expired generated file: {target}')
        raise HTTPException(
            status_code=410,
            detail=f"File '{target.name}' has expired and is no longer available.",
        )

    ext = target.suffix.lower()
    media_type = {
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pdf": "application/pdf",
        ".md": "text/markdown",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")

    return FileResponse(
        path=str(target),
        media_type=media_type,
        # Present the bare artifact name to the browser, not the owner-dir path.
        filename=target.name,
    )


@app.get("/health")
async def health_check():
    health = await get_engine().health()
    pool = workflow_repo.get_pool()
    if pool is None:
        health["db"] = "ok"
        health["db_mode"] = "memory"
    else:
        try:
            def _check_db():
                with pool.connection() as conn:
                    conn.execute("SELECT 1").fetchone()
            await asyncio.to_thread(_check_db)
            health["db"] = "ok"
            health["db_mode"] = "postgres"
        except Exception as e:
            health["db"] = "error"
            health["db_error"] = str(e)

    # Which execution backend is actually serving agent turns, and how many CLI
    # processes are in flight. Worth surfacing here because "CLI mode is on but
    # not ready" is otherwise only visible in the startup log.
    try:
        from app.cli_runtime.config import preflight as _cli_preflight
        from app.cli_runtime.session import get_registry as _cli_registry
        _status = _cli_preflight(probe_version=False)
        health["execution_backend"] = "cli" if _status.enabled else "native"
        if _status.enabled:
            health["cli_ready"] = _status.ready
            health["cli_active_runs"] = _cli_registry().active_count()
            if _status.problems:
                health["cli_problems"] = _status.problems
    except Exception as e:
        health["execution_backend"] = "unknown"
        health["cli_error"] = str(e)

    return health


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
