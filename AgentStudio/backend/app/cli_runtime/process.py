# SPDX-License-Identifier: MIT
"""cli_runtime.process — spawn a child process and read its stdout line by line.

One small abstraction (``ProcHandle``) over two backends, because a single
approach does not work everywhere:

``asyncio.create_subprocess_exec`` — preferred, used on Linux (production) and
    anywhere the running loop supports subprocesses. Native async I/O, and
    cancellation propagates cleanly.

``subprocess.Popen`` on a reader thread — the fallback. ABStudio installs
    ``WindowsSelectorEventLoopPolicy`` in ``app/main.py``, and a Windows
    ``SelectorEventLoop`` raises ``NotImplementedError`` for *any* asyncio
    subprocess. Without this fallback, CLI mode simply cannot start on a Windows
    developer machine. ``ToolDispatcher._run_in_sandbox`` already solves the same
    problem the same way (blocking ``subprocess`` off-loaded to a thread), so this
    follows an established pattern in the codebase rather than inventing one.

Both backends present an identical async interface, so ``runner`` contains no
platform branching at all.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from typing import Dict, List, Optional, Sequence

from core.logger import logger

# Sentinel pushed onto the queue by a reader thread at EOF.
_EOF = object()


class ProcHandle:
    """A spawned child, readable as an async line stream.

    Contract:
      ``await readline()``  → one line of stdout as ``bytes``; ``b""`` at EOF.
      ``await wait()``      → exit code.
      ``terminate()`` / ``kill()`` → signal the child (and its group on POSIX).
      ``returncode``        → ``None`` while running.
      ``stderr_tail()``     → last captured stderr, for error messages.
    """

    def __init__(self) -> None:
        self._stderr: List[str] = []

    # ── interface ──────────────────────────────────────────────────────────
    async def readline(self) -> bytes:  # pragma: no cover - abstract
        raise NotImplementedError

    async def wait(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def terminate(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def kill(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def returncode(self) -> Optional[int]:  # pragma: no cover - abstract
        raise NotImplementedError

    @property
    def pid(self) -> int:  # pragma: no cover - abstract
        raise NotImplementedError

    def stderr_tail(self, limit: int = 4000) -> str:
        return "".join(self._stderr)[-limit:]


# ════════════════════════════════════════════════════════════════════════════
# asyncio backend
# ════════════════════════════════════════════════════════════════════════════

class _AsyncioProc(ProcHandle):
    """Backed by ``asyncio.subprocess``."""

    def __init__(self, proc: "asyncio.subprocess.Process") -> None:
        super().__init__()
        self._proc = proc
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Read stderr continuously so a chatty child cannot fill the pipe
        buffer and deadlock while we are blocked reading stdout."""
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                self._stderr.append(line.decode("utf-8", errors="replace"))
                if len(self._stderr) > 100:
                    del self._stderr[: len(self._stderr) - 100]
        except (asyncio.CancelledError, Exception):
            return

    async def readline(self) -> bytes:
        return await self._proc.stdout.readline()

    async def wait(self) -> int:
        code = await self._proc.wait()
        self._stderr_task.cancel()
        return code

    def terminate(self) -> None:
        _signal_group(self._proc.pid, 15, self._proc.terminate)

    def kill(self) -> None:
        _signal_group(self._proc.pid, 9, self._proc.kill)

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode

    @property
    def pid(self) -> int:
        return self._proc.pid


# ════════════════════════════════════════════════════════════════════════════
# thread backend
# ════════════════════════════════════════════════════════════════════════════

class _ThreadProc(ProcHandle):
    """Backed by ``subprocess.Popen`` with reader threads.

    Two daemon threads pump stdout into an ``asyncio.Queue`` (thread-safely, via
    ``call_soon_threadsafe``) and stderr into a bounded buffer. The queue is what
    makes a blocking pipe read awaitable, so the event loop is never blocked.
    """

    def __init__(self, popen: subprocess.Popen, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self._popen = popen
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._eof = False

        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        try:
            for line in iter(self._popen.stdout.readline, b""):
                self._loop.call_soon_threadsafe(self._queue.put_nowait, line)
        except Exception:
            pass
        finally:
            try:
                self._loop.call_soon_threadsafe(self._queue.put_nowait, _EOF)
            except RuntimeError:
                pass  # loop already closed

    def _pump_stderr(self) -> None:
        try:
            for line in iter(self._popen.stderr.readline, b""):
                self._stderr.append(line.decode("utf-8", errors="replace"))
                if len(self._stderr) > 100:
                    del self._stderr[: len(self._stderr) - 100]
        except Exception:
            pass

    async def readline(self) -> bytes:
        if self._eof:
            return b""
        item = await self._queue.get()
        if item is _EOF:
            self._eof = True
            return b""
        return item

    async def wait(self) -> int:
        return await asyncio.to_thread(self._popen.wait)

    def terminate(self) -> None:
        _signal_group(self._popen.pid, 15, self._popen.terminate)

    def kill(self) -> None:
        _signal_group(self._popen.pid, 9, self._popen.kill)

    @property
    def returncode(self) -> Optional[int]:
        return self._popen.returncode

    @property
    def pid(self) -> int:
        return self._popen.pid


# ════════════════════════════════════════════════════════════════════════════
# Signalling
# ════════════════════════════════════════════════════════════════════════════

def _signal_group(pid: int, sig: int, direct_fallback) -> None:
    """Signal the child's whole process group, falling back to just the child.

    Group-wide matters because the CLI may have spawned helpers of its own;
    signalling only the direct child would orphan them. Windows has no process
    groups here, so the direct call is the available guarantee.
    """
    if sys.platform != "win32":
        try:
            os.killpg(os.getpgid(pid), sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        direct_fallback()
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
# Spawn
# ════════════════════════════════════════════════════════════════════════════

async def spawn(
    argv: Sequence[str],
    *,
    cwd: str,
    env: Dict[str, str],
) -> ProcHandle:
    """Spawn ``argv`` and return a ``ProcHandle``.

    Always an argument vector — never a shell string — so no user-supplied
    content can be interpreted as a command.

    Tries asyncio first and transparently falls back to the thread backend when
    the running loop cannot create subprocesses (Windows ``SelectorEventLoop``).
    """
    kwargs: Dict[str, object] = {}
    if sys.platform != "win32":
        # New session ⇒ new process group, so the whole tree is signallable.
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        return _AsyncioProc(proc)
    except NotImplementedError:
        # Windows SelectorEventLoop (installed by app/main.py) has no subprocess
        # support at all. Use the blocking backend on reader threads instead.
        logger.info(
            "[CLI-PROC] asyncio subprocess unavailable on this event loop — "
            "using the threaded subprocess backend",
            loop=type(asyncio.get_running_loop()).__name__,
        )

    popen = await asyncio.to_thread(
        lambda: subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **({"start_new_session": True} if sys.platform != "win32" else {}),
        )
    )
    return _ThreadProc(popen, asyncio.get_running_loop())


__all__ = ["ProcHandle", "spawn"]
