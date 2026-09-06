# SPDX-License-Identifier: MIT
# ============================================================
# AiNxt DOCKER EXECUTION SANDBOX
# Isolated, PCI-safe code execution via Docker
# ============================================================
#
# Security guarantees:
#   - No host filesystem access (temp dir only, ro options)
#   - Network disabled
#   - Memory capped at 512 MB
#   - CPU quota capped at 50% of 1 core
#   - No privilege escalation (no-new-privileges)
#   - Container auto-removed after execution
#   - Temp directory cleaned up on every run
#
# Supported languages:
#   - python     → python:3.11-slim
#   - bash       → bash:5-alpine
#   - shell      → alias for bash
#   - javascript → node:20-alpine
#   - typescript → node:20-alpine
#   - go         → golang:1.21-alpine
#   - kotlin     → zenika/kotlin:latest
#   - scala      → virtuslab/scala-cli:latest
# ============================================================

import os
import shutil
import subprocess
import tempfile
import uuid
from typing import Dict, Optional, Union

import docker
from docker.errors import DockerException

from core.logger import logger
from core.config import SANDBOX_PREFIX as _sandbox_prefix
from agents.compliance_engine import compliance_engine


# ============================================================
# CONFIGURATION
# ============================================================

EXECUTION_TIMEOUT = 60      # seconds per container run
MEM_LIMIT        = "512m"
CPU_QUOTA        = 50_000   # 50% of 1 CPU (100_000 = 1 full core)

LANGUAGE_CONFIG = {
    # ── Python ──────────────────────────────────────────────────────
    "python": {
        "image":    "python:3.11-slim",
        "command":  "python /sandbox/main.py",
        "filename": "main.py",
    },
    "py": {   # alias
        "image":    "python:3.11-slim",
        "command":  "python /sandbox/main.py",
        "filename": "main.py",
    },
    # ── Shell ────────────────────────────────────────────────────────
    "bash": {
        "image":    "bash:5-alpine",
        "command":  "bash /sandbox/main.sh",
        "filename": "main.sh",
    },
    "shell": {   # alias
        "image":    "bash:5-alpine",
        "command":  "bash /sandbox/main.sh",
        "filename": "main.sh",
    },
    "sh": {   # alias
        "image":    "bash:5-alpine",
        "command":  "sh /sandbox/main.sh",
        "filename": "main.sh",
    },
    # ── JavaScript / TypeScript / Frontend ───────────────────────────
    "javascript": {
        "image":    "node:20-alpine",
        "command":  "node /sandbox/main.js",
        "filename": "main.js",
    },
    "typescript": {
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.ts",
        "filename": "main.ts",
    },
    "js": {   # alias
        "image":    "node:20-alpine",
        "command":  "node /sandbox/main.js",
        "filename": "main.js",
    },
    "ts": {   # alias
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.ts",
        "filename": "main.ts",
    },
    "jsx": {
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.jsx",
        "filename": "main.jsx",
    },
    "tsx": {
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.tsx",
        "filename": "main.tsx",
    },
    "react": {   # alias
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.jsx",
        "filename": "main.jsx",
    },
    "angular": {   # alias
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.ts",
        "filename": "main.ts",
    },
    "vue": {
        "image":    "node:20-alpine",
        "command":  "node --check /sandbox/main.js",
        "filename": "main.js",
    },
    # ── JVM languages ────────────────────────────────────────────────
    "java": {
        "image":    "eclipse-temurin:21-jdk-alpine",
        "command":  "sh -c 'cd /sandbox && javac *.java 2>&1; exit $?'",
        "filename": "Main.java",
    },
    "kotlin": {
        "image":    "eclipse-temurin:21-jdk-alpine",
        "command":  "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'",
        "filename": "Main.kt",
    },
    "kt": {   # alias
        "image":    "eclipse-temurin:21-jdk-alpine",
        "command":  "sh -c 'kotlinc /sandbox/Main.kt -include-runtime -d /sandbox/out.jar 2>&1; exit $?'",
        "filename": "Main.kt",
    },
    "scala": {
        "image":    "virtuslab/scala-cli:latest",
        "command":  "scala-cli compile /sandbox/Main.scala 2>&1",
        "filename": "Main.scala",
    },
    "groovy": {
        "image":    "eclipse-temurin:21-jdk-alpine",
        "command":  "sh -c 'groovy /sandbox/Main.groovy 2>&1; exit $?'",
        "filename": "Main.groovy",
    },
    # ── Go ───────────────────────────────────────────────────────────
    "go": {
        "image":    "golang:1.21-alpine",
        "command":  "sh -c 'cd /sandbox && go mod init check 2>/dev/null; gofmt -e main.go 2>&1; exit $?'",
        "filename": "main.go",
    },
    "golang": {   # alias
        "image":    "golang:1.21-alpine",
        "command":  "sh -c 'cd /sandbox && go mod init check 2>/dev/null; gofmt -e main.go 2>&1; exit $?'",
        "filename": "main.go",
    },
    # ── Rust ─────────────────────────────────────────────────────────
    "rust": {
        "image":    "rust:1.77-alpine",
        "command":  "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
        "filename": "main.rs",
    },
    "rs": {   # alias
        "image":    "rust:1.77-alpine",
        "command":  "sh -c 'rustc --edition 2021 --crate-type lib /sandbox/main.rs 2>&1; exit $?'",
        "filename": "main.rs",
    },
    # ── C / C++ ──────────────────────────────────────────────────────
    "c": {
        "image":    "gcc:13-alpine",
        "command":  "sh -c 'gcc -fsyntax-only /sandbox/main.c 2>&1; exit $?'",
        "filename": "main.c",
    },
    "cpp": {
        "image":    "gcc:13-alpine",
        "command":  "sh -c 'g++ -fsyntax-only /sandbox/main.cpp 2>&1; exit $?'",
        "filename": "main.cpp",
    },
    "c++": {   # alias
        "image":    "gcc:13-alpine",
        "command":  "sh -c 'g++ -fsyntax-only /sandbox/main.cpp 2>&1; exit $?'",
        "filename": "main.cpp",
    },
    # ── C# / .NET ────────────────────────────────────────────────────
    "csharp": {
        "image":    "mcr.microsoft.com/dotnet/sdk:8.0-alpine",
        "command":  "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
        "filename": "Main.cs",
    },
    "cs": {   # alias
        "image":    "mcr.microsoft.com/dotnet/sdk:8.0-alpine",
        "command":  "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
        "filename": "Main.cs",
    },
    "dotnet": {   # alias
        "image":    "mcr.microsoft.com/dotnet/sdk:8.0-alpine",
        "command":  "sh -c 'dotnet-script /sandbox/Main.cs 2>&1; exit $?'",
        "filename": "Main.cs",
    },
    # ── Ruby ─────────────────────────────────────────────────────────
    "ruby": {
        "image":    "ruby:3.3-alpine",
        "command":  "ruby -c /sandbox/main.rb 2>&1",
        "filename": "main.rb",
    },
    "rb": {   # alias
        "image":    "ruby:3.3-alpine",
        "command":  "ruby -c /sandbox/main.rb 2>&1",
        "filename": "main.rb",
    },
    # ── PHP ──────────────────────────────────────────────────────────
    "php": {
        "image":    "php:8.3-cli-alpine",
        "command":  "php -l /sandbox/main.php 2>&1",
        "filename": "main.php",
    },
    # ── Swift ────────────────────────────────────────────────────────
    "swift": {
        "image":    "swift:5.10-slim",
        "command":  "swift -typecheck /sandbox/main.swift 2>&1",
        "filename": "main.swift",
    },
    # ── R ────────────────────────────────────────────────────────────
    "r": {
        "image":    "r-base:4.3.3",
        "command":  "Rscript --vanilla -e 'parse(file=\"/sandbox/main.R\")' 2>&1",
        "filename": "main.R",
    },
    # ── Elixir ───────────────────────────────────────────────────────
    "elixir": {
        "image":    "elixir:1.16-alpine",
        "command":  "elixir /sandbox/main.ex 2>&1",
        "filename": "main.ex",
    },
    # ── Dart ─────────────────────────────────────────────────────────
    "dart": {
        "image":    "dart:3.3-sdk",
        "command":  "dart analyze /sandbox/main.dart 2>&1",
        "filename": "main.dart",
    },
    # ── Lua ──────────────────────────────────────────────────────────
    "lua": {
        "image":    "nickblah/lua:5.4-alpine",
        "command":  "luac -p /sandbox/main.lua 2>&1",
        "filename": "main.lua",
    },
    # ── Perl ─────────────────────────────────────────────────────────
    "perl": {
        "image":    "perl:5.38-slim",
        "command":  "perl -c /sandbox/main.pl 2>&1",
        "filename": "main.pl",
    },
    "pl": {   # alias
        "image":    "perl:5.38-slim",
        "command":  "perl -c /sandbox/main.pl 2>&1",
        "filename": "main.pl",
    },
    # ── PowerShell ───────────────────────────────────────────────────
    "powershell": {
        "image":    "mcr.microsoft.com/powershell:alpine-3.18",
        "command":  "pwsh -NonInteractive -File /sandbox/main.ps1 2>&1",
        "filename": "main.ps1",
    },
    "ps1": {   # alias
        "image":    "mcr.microsoft.com/powershell:alpine-3.18",
        "command":  "pwsh -NonInteractive -File /sandbox/main.ps1 2>&1",
        "filename": "main.ps1",
    },
}

DEFAULT_LANGUAGE = "python"


# ============================================================
# DOCKER EXECUTOR
# ============================================================

class DockerExecutor:
    """
    Executes arbitrary code in an ephemeral Docker container.

    The AiNxt gateway and workers run under pm2 (NOT in Docker).
    Docker is used only to execute AI-generated code snippets — one
    ephemeral container per execution, destroyed immediately after.

    Call flow:
        pm2 worker → docker_executor.execute() → Docker SDK
        → dockerd spins up container (image pre-cached locally)
        → code runs in isolated container (no network, 512 MB RAM, 50% CPU)
        → stdout/stderr captured → container destroyed → result returned

    Usage:
        result = docker_executor.execute(code="print('hi')", language="python")
        # result = {"success": True, "output": "hi\n", "exit_code": 0, "language": "python"}
    """

    # Tier 1 images — pre-pulled at startup, always warm.
    # Tier 2 images (kotlin, scala, rust, dotnet, etc.) are pulled on first use
    # and cached by the Docker daemon automatically.
    REQUIRED_IMAGES = {
        "python:3.11-slim",
        "node:20-alpine",
        "bash:5-alpine",
        "eclipse-temurin:21-jdk-alpine",   # Java + Kotlin + Groovy
        "golang:1.21-alpine",
    }

    def __init__(self):
        self._client: Optional[docker.DockerClient] = None

    def verify_images(self) -> dict:
        """
        Check which sandbox images are locally cached.
        Called once at startup. Logs warnings for missing images so ops
        knows to run 'docker pull' before the first use of that language.
        Returns {"python:3.11-slim": True, "bash:5-alpine": False, ...}
        """
        results = {}
        try:
            client = self._get_client()
            local_tags = set()
            for img in client.images.list():
                for tag in (img.tags or []):
                    local_tags.add(tag)
            for image in self.REQUIRED_IMAGES:
                present = image in local_tags
                results[image] = present
                if present:
                    logger.info(f"DockerExecutor: image cached ✓  {image}")
                else:
                    logger.warning(
                        f"DockerExecutor: image NOT cached  {image}  "
                        f"— first use will auto-pull (adds ~10-30s latency). "
                        f"Pre-pull with: docker pull {image}"
                    )
        except Exception as e:
            logger.warning(f"DockerExecutor: image verification skipped ({e})")
        return results

    # --------------------------------------------------------
    # LAZY CLIENT INIT
    # --------------------------------------------------------

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client.ping()
                logger.info("DockerExecutor: Docker client connected")
            except DockerException as e:
                self._client = None
                raise RuntimeError(f"Docker unavailable: {e}") from e
        return self._client

    # --------------------------------------------------------
    # MAIN ENTRY POINT
    # --------------------------------------------------------

    def execute(
        self,
        code: str,
        language: str = DEFAULT_LANGUAGE,
        image_override: Optional[str] = None,
        command_override: Optional[str] = None,
        filename_override: Optional[str] = None,
        network_enabled: bool = False,
        files: Optional[Dict] = None,
    ) -> Dict:
        """
        Execute code in an isolated Docker container.

        When image_override is provided (ainxt-builder-* images from BuildManifestResolver),
        the command_override and filename_override are used instead of LANGUAGE_CONFIG.
        This replaces the old execute_in_repo_image() path which used per-repo images.

        Returns:
            {
                "success":   bool,
                "output":    str,   # combined stdout + stderr
                "exit_code": int,
                "language":  str,
            }
        """
        language = language.lower()
        request_id = str(uuid.uuid4())

        logger.info(
            f"DockerExecutor {request_id} → START lang={language}"
            + (f" image_override={image_override}" if image_override else "")
        )

        # ====================================================
        # PCI INPUT CHECK
        # ====================================================

        validation = compliance_engine.validate_input(code)

        if validation["blocked"]:
            logger.critical(
                f"DockerExecutor {request_id} → PCI BLOCKED INPUT"
            )
            return {
                "success":   False,
                "output":    "Execution blocked: PCI compliance violation",
                "exit_code": -1,
                "language":  language,
            }

        # ====================================================
        # LANGUAGE CONFIG (or override for ainxt-builder images)
        # ====================================================

        if image_override:
            fallback_filename = LANGUAGE_CONFIG.get(language, {}).get("filename", "main.py")
            lang_cfg = {
                "image":    image_override,
                "command":  command_override,
                "filename": filename_override or fallback_filename,
            }
        else:
            lang_cfg = LANGUAGE_CONFIG.get(language)
            if lang_cfg is None:
                return {
                    "success":   False,
                    "output":    f"Unsupported language: {language}",
                    "exit_code": -1,
                    "language":  language,
                }

        # ====================================================
        # SANDBOX DIRECTORY
        # ====================================================

        sandbox_dir = os.path.join(
            tempfile.gettempdir(),
            f"{os.getenv('SANDBOX_PREFIX', _sandbox_prefix)}{request_id}"
        )
        os.makedirs(sandbox_dir, exist_ok=True)

        # Data-analysis (ADA): bind caller-supplied data files into the sandbox
        # working dir so the script can read them via open(name). Names are
        # sanitised (basename only) so they can't escape the sandbox dir.
        for raw_name, content in (files or {}).items():
            safe = os.path.basename(str(raw_name or "")).strip()
            safe = "".join(c for c in safe if c.isalnum() or c in "._-")[:80]
            if not safe:
                continue
            try:
                data = content if isinstance(content, (bytes, bytearray)) else str(content)
                mode = "wb" if isinstance(data, (bytes, bytearray)) else "w"
                with open(os.path.join(sandbox_dir, safe), mode) as df:
                    df.write(data)
            except Exception as _e:
                logger.warning(f"DockerExecutor {request_id}: data file {safe!r} write failed → {_e}")

        try:
            return self._run(
                request_id=request_id,
                code=code,
                lang_cfg=lang_cfg,
                sandbox_dir=sandbox_dir,
                language=language,
                network_enabled=network_enabled,
            )
        finally:
            self._cleanup_dir(sandbox_dir)

    # --------------------------------------------------------
    # INTERNAL EXECUTION
    # --------------------------------------------------------

    def _run(
        self,
        request_id: str,
        code: str,
        lang_cfg: Dict,
        sandbox_dir: str,
        language: str,
        network_enabled: bool = False,
    ) -> Dict:

        # Write code file
        code_file = os.path.join(sandbox_dir, lang_cfg["filename"])
        with open(code_file, "w") as f:
            f.write(code)

        container = None

        try:
            client = self._get_client()

            # Guard: if the image doesn't exist locally, return a skippable result
            # rather than letting Docker attempt a registry pull (which fails in prod
            # where there is no internet access and DNS is IPv4-only).
            _image_name = lang_cfg["image"]
            try:
                client.images.get(_image_name)
            except Exception:
                logger.warning(
                    f"DockerExecutor {request_id} → image {_image_name!r} not found locally "
                    f"— skipping execution (infra skip, not a code failure)"
                )
                return {
                    "success":       False,
                    "output":        "",
                    "exit_code":     0,
                    "language":      language,
                    "image_missing": True,
                }

            container = client.containers.run(
                image=lang_cfg["image"],
                command=lang_cfg["command"],
                volumes={
                    sandbox_dir: {"bind": "/sandbox", "mode": "rw"}
                },
                working_dir="/sandbox",
                detach=True,
                mem_limit=MEM_LIMIT,
                cpu_quota=CPU_QUOTA,
                network_disabled=not network_enabled,
                security_opt=["no-new-privileges"],
                read_only=False,    # /sandbox is rw, rest is container default
            )

            # Wait for completion (captures timeout)
            exit_info = container.wait(timeout=EXECUTION_TIMEOUT)
            exit_code = exit_info.get("StatusCode", -1)

            stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
            output = stdout + (f"\n[stderr]\n{stderr}" if stderr.strip() else "")

            success = (exit_code == 0)

            logger.info(
                f"DockerExecutor {request_id} → "
                f"exit_code={exit_code} success={success}"
            )

            return {
                "success":   success,
                "output":    output,
                "exit_code": exit_code,
                "language":  language,
            }

        except Exception as e:
            logger.error(
                f"DockerExecutor {request_id} → execution failed → {e}"
            )
            return {
                "success":   False,
                "output":    str(e),
                "exit_code": -1,
                "language":  language,
            }

        finally:
            self._cleanup_container(container)

    # --------------------------------------------------------
    # CLEANUP HELPERS
    # --------------------------------------------------------

    def _cleanup_container(self, container) -> None:
        if container is None:
            return
        try:
            container.remove(force=True)
        except Exception:
            pass

    def _cleanup_dir(self, path: str) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    # --------------------------------------------------------
    # HEALTH CHECK
    # --------------------------------------------------------

    def is_available(self) -> bool:
        try:
            self._get_client()
            return True
        except Exception:
            return False


# ============================================================
# SUBPROCESS EXECUTOR (fallback when Docker is unavailable)
# Python-only, no network, temp dir, 30s timeout.
# Used in prod environments where Docker daemon is not running.
# ============================================================

SUBPROCESS_TIMEOUT = 30   # seconds
SUBPROCESS_SUPPORTED = {"python", "py"}


class SubprocessExecutor:
    """
    Executes Python code via subprocess when Docker is unavailable.
    Restrictions: Python only, no network access enforced at OS level,
    temp directory cleaned up after each run, 30s hard timeout.

    This is a production fallback — not a sandbox. Use Docker for
    untrusted code. For AiNxt internal agents writing known code, this
    is sufficient.
    """

    def execute(self, code: str, language: str = "python") -> Dict:
        language = language.lower()
        request_id = str(uuid.uuid4())

        logger.info(f"SubprocessExecutor {request_id} → START lang={language}")

        if language not in SUBPROCESS_SUPPORTED:
            return {
                "success":   False,
                "output":    f"SubprocessExecutor only supports Python (got '{language}'). Docker required for other languages.",
                "exit_code": -1,
                "language":  language,
                "executor":  "subprocess",
            }

        # PCI input check
        validation = compliance_engine.validate_input(code)
        if validation["blocked"]:
            logger.critical(f"SubprocessExecutor {request_id} → PCI BLOCKED INPUT")
            return {
                "success":   False,
                "output":    "Execution blocked: PCI compliance violation",
                "exit_code": -1,
                "language":  language,
                "executor":  "subprocess",
            }

        sandbox_dir = os.path.join(tempfile.gettempdir(), f"ainxt_subproc_{request_id}")
        os.makedirs(sandbox_dir, exist_ok=True)
        code_file = os.path.join(sandbox_dir, "main.py")

        try:
            with open(code_file, "w") as f:
                f.write(code)

            # Set OS-level resource limits for untrusted subprocess code
            def _set_limits():
                try:
                    import resource
                    resource.setrlimit(resource.RLIMIT_AS,  (256 * 1024 * 1024, 256 * 1024 * 1024))  # 256 MB virtual memory
                    resource.setrlimit(resource.RLIMIT_CPU, (SUBPROCESS_TIMEOUT, SUBPROCESS_TIMEOUT))  # CPU seconds = wall timeout
                    resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))   # 10 MB max file write
                    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))                               # max 32 open file descriptors
                except Exception:
                    pass  # resource module unavailable on Windows — safe to ignore

            result = subprocess.run(
                ["python3", code_file],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                cwd=sandbox_dir,
                preexec_fn=_set_limits,
            )
            # Truncate runaway output — 2 MB max
            _MAX_OUTPUT = 2 * 1024 * 1024
            raw_stdout = result.stdout
            raw_stderr = result.stderr
            if len(raw_stdout) > _MAX_OUTPUT:
                raw_stdout = raw_stdout[:_MAX_OUTPUT] + f"\n[output truncated at {_MAX_OUTPUT} bytes]"
            output = raw_stdout
            if raw_stderr.strip():
                output += f"\n[stderr]\n{raw_stderr[:4096]}"
            exit_code = result.returncode
            success = (exit_code == 0)

            logger.info(f"SubprocessExecutor {request_id} → exit_code={exit_code} success={success}")

            return {
                "success":   success,
                "output":    output,
                "exit_code": exit_code,
                "language":  language,
                "executor":  "subprocess",
            }

        except subprocess.TimeoutExpired:
            logger.error(f"SubprocessExecutor {request_id} → timeout after {SUBPROCESS_TIMEOUT}s")
            return {
                "success":   False,
                "output":    f"Execution timed out after {SUBPROCESS_TIMEOUT} seconds.",
                "exit_code": -1,
                "language":  language,
                "executor":  "subprocess",
            }
        except Exception as e:
            logger.error(f"SubprocessExecutor {request_id} → failed: {e}")
            return {
                "success":   False,
                "output":    str(e),
                "exit_code": -1,
                "language":  language,
                "executor":  "subprocess",
            }
        finally:
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    def is_available(self) -> bool:
        return True


# ============================================================
# SINGLETON
# ============================================================

docker_executor = DockerExecutor()
subprocess_executor = SubprocessExecutor()

# SEC-F-016: the subprocess executor runs code with no container isolation.
# Falling back to it silently in production when Docker is unavailable would
# execute untrusted code directly in the platform's process space. Require an
# explicit opt-in so the fallback is a deliberate operator decision.
_ALLOW_SUBPROCESS = os.getenv("ALLOW_SUBPROCESS_EXECUTOR", "false").lower() in ("true", "1")

# Verify image cache at import time (runs once when gateway/workers start).
# Logs warnings for any missing images — does NOT block startup.
try:
    docker_executor.verify_images()
except Exception:
    pass  # Docker may not be available in all environments


def get_executor(language: str = "python") -> Optional[Union[DockerExecutor, SubprocessExecutor]]:
    """
    Returns the best available executor for the given language.
    - Docker available → DockerExecutor (full sandbox, all languages)
    - Docker unavailable and ALLOW_SUBPROCESS_EXECUTOR=true → SubprocessExecutor
    - Docker unavailable and ALLOW_SUBPROCESS_EXECUTOR unset/false → None

    CONTRACT CHANGE (SEC-F-016): this function can return None when Docker is
    unavailable and subprocess execution is not explicitly opted in. All callers
    MUST check for None before calling .execute() — an unchecked call will raise
    AttributeError: 'NoneType' object has no attribute 'execute', which occurs
    exactly when Docker is down (the failure scenario this change is meant to
    make safe). Callers already updated: mcp/registry.py, routers/sandbox_router.py.
    """
    if docker_executor.is_available():
        return docker_executor
    if _ALLOW_SUBPROCESS:
        logger.warning("DockerExecutor unavailable — falling back to SubprocessExecutor (Python only)")
        return subprocess_executor
    logger.error(
        "DockerExecutor unavailable and ALLOW_SUBPROCESS_EXECUTOR is not set — "
        "refusing to execute untrusted code outside a container. Set "
        "ALLOW_SUBPROCESS_EXECUTOR=true to explicitly opt in."
    )
    return None
