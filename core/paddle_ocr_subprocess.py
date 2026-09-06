# SPDX-License-Identifier: MIT
"""
Subprocess isolation wrapper for PaddleOCR.

Why this exists
---------------
paddlepaddle 2.6.x on Linux CPU accumulates corrupt state inside its native
inference session (OneDNN primitive cache + OMP thread pool) after a
variable number of OCR calls within the same process. Once corrupted,
subsequent calls raise `RuntimeError: could not execute a primitive` on
inputs that would have succeeded moments earlier — same reader instance,
same image shape, same dtype. Observed live under gunicorn on the
Technology.pdf ingestion run:

    page 119 (image 1792×2528 uint8) → 39 cells, 2942ms   ← OK
    page 121 (image 1792×2528 uint8) → predict_det crash  ← FAIL
    page 122 (image 1792×2528 uint8) → predict_det crash  ← FAIL

The crash is stateful, not input-dependent. Fork-safety env vars
(KMP_INIT_AT_FORK, OMP_NUM_THREADS=1, MKL_NUM_THREADS=1,
MKL_THREADING_LAYER=SEQUENTIAL, KMP_AFFINITY=disabled,
FLAGS_use_mkldnn=false) reduce the crash rate substantially — under
gunicorn we went from crashing ~half of scanned pages to 54/88 — but
they do not eliminate the drift.

The only bulletproof fix is to run every `reader.ocr()` call inside a
throwaway subprocess. When the child exits, its entire address space
(including all Paddle native state) is reclaimed by the OS, so no state
can accumulate across calls.

Design
------
- `multiprocessing.get_context("spawn")` — fresh Python interpreter,
  no inherited parent memory, no fork() involved. Slower to start
  (~1.5s cold on this hardware) but immune to fork corruption.
- ONE long-lived child per gunicorn worker, recycled after N successful
  calls to bound state accumulation. Amortizes the spawn cost across
  many OCR pages while still resetting state periodically.
- Communication via a single `multiprocessing.Pipe`. Images are shipped
  as raw bytes + shape + dtype tuples — avoids pickling numpy arrays
  in a way that might trigger version-mismatch bugs.
- Hard timeout per call. If the child hangs (rare but possible under
  thread-pool deadlock), the parent kills it and spawns a fresh one.
  The parent NEVER blocks indefinitely.
- Fatal crashes inside the child (SIGSEGV from Paddle's C++ core, or
  the "could not execute a primitive" RuntimeError) cause the parent
  to recycle the child before returning the error to the caller, so
  the NEXT OCR call starts on clean state.

Activation
----------
This module is enabled by setting `PADDLE_OCR_ISOLATE=1` in the parent
process. `core/docling_parser.py` sets this automatically before
registering PaddleOcrModel, so production deployments do not need to
remember the env var. When unset, the code path is bypassed and
PaddleOcrModel behaves exactly as before.

The six fork-safety knobs (KMP_*, OMP_*, MKL_*, FLAGS_use_mkldnn) are
set INSIDE the child process (see `_child_main()`), not inherited from
the parent. This isolates PaddleOCR's conservative threading settings
from the rest of the embed service (embeddings, reranking, etc.).
"""
from __future__ import annotations

import multiprocessing as _mp
import os
import queue as _queue
import threading
import time
import traceback
from typing import Any, Optional

import numpy as np

try:
    from core.logger import logger as _log  # type: ignore
except Exception:  # pragma: no cover
    import logging as _logging
    _log = _logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────────

# Recycle the child after this many successful ocr() calls. Keeps native-state
# accumulation bounded. Tuned so:
#   - Cold-start cost (~1.5s spawn + ~2s PaddleOCR model load) is amortized
#     across ≥ RECYCLE_EVERY calls
#   - We never let the child run long enough to drift into corruption territory
#     (empirically, corruption sets in somewhere between 5 and 30 calls under
#      gunicorn with our current env vars)
RECYCLE_EVERY = int(os.environ.get("PADDLE_OCR_ISOLATE_RECYCLE", "20"))

# Hard timeout for a single ocr() call. Anything above this indicates the
# child is wedged (e.g. thread-pool deadlock). Kill + respawn.
CALL_TIMEOUT_S = float(os.environ.get("PADDLE_OCR_ISOLATE_TIMEOUT", "60"))

# How long we wait for the child to acknowledge readiness after spawn.
# Must exceed cold PaddleOCR model load time (~2s on this hardware, up
# to ~10s if the CDN download path fires).
STARTUP_TIMEOUT_S = float(os.environ.get("PADDLE_OCR_ISOLATE_STARTUP", "45"))

# Number of independent OCR child processes.
#
# Each child is a fully separate OS process with its own pipe, its own Paddle
# native state and its own lock, so N children give genuine N-way OCR
# parallelism with no shared mutable state — races are impossible by
# construction rather than by discipline.
#
# Sizing guidance (each child holds the PP-OCRv4 models, ~500 MB RSS):
#     1 → ~500 MB, no parallelism (previous behaviour)
#     3 → ~1.5 GB, ~3x OCR throughput   (default)
#     4 → ~2.0 GB, ~4x OCR throughput
# Keep this <= the number of physical cores available to the embed service and
# well within its memory budget. Set to 1 to restore single-child behaviour.
POOL_SIZE = max(1, min(8, int(os.environ.get("PADDLE_OCR_POOL_SIZE", "3"))))


# ── Child-side entry point ───────────────────────────────────────────────────

def _child_main(conn, options: dict) -> None:
    """Runs inside the spawned child.

    Instantiates ONE PaddleOCR reader and services requests forever from
    the parent's pipe. Each request is a dict:

        {"op": "ocr", "image_bytes": <bytes>, "shape": (h, w, c),
         "dtype": "uint8", "cls": bool}
        {"op": "shutdown"}

    Response for "ocr" is either:
        {"ok": True, "result": <PaddleOCR.ocr() return>}
        {"ok": False, "error": "<exc-type>: <msg>", "traceback": "..."}
    """
    # Set the fork-safety / thread-pool knobs INSIDE the child, not the
    # parent. This keeps the parent gunicorn worker free to use all its
    # threads for embeddings / reranking while the PaddleOCR child gets
    # the conservative settings that prevent "could not execute a primitive".
    # These are applied before importing paddleocr so they take effect during
    # model load and inference.
    os.environ.setdefault("KMP_INIT_AT_FORK", "FALSE")
    os.environ.setdefault("KMP_AFFINITY", "disabled")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("MKL_THREADING_LAYER", "SEQUENTIAL")
    os.environ.setdefault("FLAGS_use_mkldnn", "false")

    # Import inside the child so the parent doesn't drag paddleocr into
    # its own address space (which would defeat the point of isolation).
    try:
        from paddleocr import PaddleOCR
    except Exception:
        try:
            conn.send({"ok": False, "error": f"import", "traceback": traceback.format_exc()})
        except Exception:
            pass
        return

    try:
        reader = PaddleOCR(**options)
    except Exception:
        try:
            conn.send({"ok": False, "error": f"init", "traceback": traceback.format_exc()})
        except Exception:
            pass
        return

    # Signal ready to parent
    try:
        conn.send({"ok": True, "ready": True})
    except Exception:
        return

    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            break

        op = msg.get("op")
        if op == "shutdown":
            break
        if op != "ocr":
            try:
                conn.send({"ok": False, "error": f"unknown op: {op!r}", "traceback": ""})
            except Exception:
                break
            continue

        try:
            shape = tuple(msg["shape"])
            dtype = np.dtype(msg["dtype"])
            im = np.frombuffer(msg["image_bytes"], dtype=dtype).reshape(shape)
            # frombuffer produces read-only arrays; PaddleOCR mutates.
            im = np.ascontiguousarray(im)
            result = reader.ocr(im, cls=bool(msg.get("cls", False)))
            try:
                conn.send({"ok": True, "result": result})
            except Exception:
                break
        except Exception:
            # Send the failure back but do NOT re-raise — the parent
            # decides whether to retry or recycle us.
            try:
                conn.send({
                    "ok": False,
                    "error": f"",
                    "traceback": traceback.format_exc(),
                })
            except Exception:
                break

    try:
        conn.close()
    except Exception:
        pass


# ── Parent-side: one worker child ────────────────────────────────────────────

class _PaddleOcrChild:
    """One spawn-based PaddleOCR child process plus its pipe.

    Thread safety
    -------------
    The child is single-threaded and the parent talks to it over ONE duplex
    pipe, so every operation on this object MUST be serialized.  A
    `threading.RLock` guards the full request/response cycle.

    This lock is not optional.  Without it the following race destroys the
    child (observed in production, 2026-07-30):

      * Sending a 13 MB page image is not atomic — multiprocessing splits it
        into ~207 sequential 64 KB `write()` syscalls, and each one re-reads
        `self._handle` from the shared connection object.
      * If another thread calls `_recycle()` partway through that loop, the
        pipe is closed and `self._handle` becomes None.  The writing thread
        then fails with `TypeError: 'NoneType' object cannot be interpreted
        as an integer` (caught mid-close) or `BrokenPipeError` (caught after).
      * Each victim runs `_recycle()` in turn, killing the fresh child another
        thread just spawned.  The logs showed the same pid recycled twice
        0.5 ms apart — a self-sustaining recycle storm that failed 140 of 168
        page batches.

    PaddleOcrSubprocessPool additionally hands out one child per caller, so in
    normal operation this lock is uncontended — it is the correctness backstop
    that makes a shared child safe rather than the throughput mechanism.
    """

    def __init__(self, options: dict, index: int = 0):
        self._options = dict(options)  # frozen copy for child spawn
        self._index = index            # position in the pool, for log clarity
        self._ctx = _mp.get_context("spawn")
        self._proc: Optional[Any] = None
        self._conn: Optional[Any] = None
        self._call_count = 0
        # Reentrant: ocr() holds the lock and may call _recycle(), which is
        # also lock-protected when invoked directly from other entry points.
        self._lock = threading.RLock()
        self._start_child()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def _start_child(self) -> None:
        """Spawn a new child and wait for its ready signal."""
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_child_main,
            args=(child_conn, self._options),
            daemon=True,  # dies with parent — no zombies on gunicorn reload
        )
        proc.start()
        # child_conn is used by the child; parent doesn't need its handle.
        child_conn.close()

        t0 = time.perf_counter()
        if not parent_conn.poll(timeout=STARTUP_TIMEOUT_S):
            self._force_kill(proc, parent_conn)
            raise RuntimeError(
                f"PaddleOCR subprocess did not become ready within "
                f"{STARTUP_TIMEOUT_S:.0f}s"
            )
        try:
            ready = parent_conn.recv()
        except (EOFError, OSError) as e:
            self._force_kill(proc, parent_conn)
            raise RuntimeError(f"PaddleOCR subprocess died before ready: {type(e).__name__}") from e

        if not ready.get("ok"):
            self._force_kill(proc, parent_conn)
            raise RuntimeError(
                f"PaddleOCR subprocess init failed: {ready.get('error')!r}\n"
                f"{ready.get('traceback', '')}"
            )
        self._proc = proc
        self._conn = parent_conn
        self._call_count = 0
        _log.info(
            f"[PaddleOCR][SUBPROC] child[{self._index}] ready pid={proc.pid} "
            f"startup_ms={int((time.perf_counter() - t0) * 1000)}"
        )

    def _force_kill(self, proc, conn) -> None:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        try:
            if proc is not None and proc.is_alive():
                proc.terminate()
                proc.join(timeout=2)
                if proc.is_alive():
                    proc.kill()
                    proc.join(timeout=2)
        except Exception:
            pass

    def _recycle(self, reason: str) -> None:
        """Tear down and respawn the child."""
        _log.info(
            f"[PaddleOCR][SUBPROC] recycling child[{self._index}] "
            f"pid={getattr(self._proc, 'pid', '?')} reason={reason}"
        )
        if self._conn is not None and self._proc is not None and self._proc.is_alive():
            try:
                self._conn.send({"op": "shutdown"})
                self._proc.join(timeout=3)
            except Exception:
                pass
        self._force_kill(self._proc, self._conn)
        self._proc = None
        self._conn = None
        self._start_child()

    def shutdown(self) -> None:
        """Tear down the child cleanly (called on process exit).

        Lock-protected so a shutdown cannot race an in-flight ocr() call.
        """
        with self._lock:
            if self._proc is not None:
                try:
                    if self._conn is not None:
                        self._conn.send({"op": "shutdown"})
                        self._proc.join(timeout=3)
                except Exception:
                    pass
                self._force_kill(self._proc, self._conn)
            self._proc = None
            self._conn = None

    # ── Public API ───────────────────────────────────────────────────────

    def ocr(self, image: np.ndarray, use_angle_cls: bool) -> Any:
        """Run PaddleOCR on `image` in the child.

        Returns the raw PaddleOCR.ocr() output (list of pages, each a
        list of [box, (text, score)] entries, or [None] if no text).

        On failure inside the child (Paddle exception, primitive error),
        re-raises the exception in the parent so calling code sees the
        same behavior as an in-process call — Docling's per-page
        fallback path already handles this correctly.

        On timeout or child death, kills + respawns and re-raises.

        Thread safety: the entire request/response cycle is serialized by
        `self._lock`.  Sending a multi-megabyte image is ~207 sequential
        64 KB pipe writes; without the lock a concurrent `_recycle()` can
        close the pipe mid-loop and corrupt every in-flight caller.
        """
        with self._lock:
            if self._proc is None or not self._proc.is_alive():
                _log.warning("[PaddleOCR][SUBPROC] child not alive at call time — respawning")
                self._start_child()

            # Recycle proactively before we approach corruption territory.
            if self._call_count >= RECYCLE_EVERY:
                self._recycle(f"call_count={self._call_count} ≥ {RECYCLE_EVERY}")

            # Ship the image as raw bytes.
            if image.dtype != np.uint8:
                image = image.astype(np.uint8)
            if not image.flags["C_CONTIGUOUS"]:
                image = np.ascontiguousarray(image)

            msg = {
                "op": "ocr",
                "image_bytes": image.tobytes(),
                "shape": tuple(image.shape),
                "dtype": str(image.dtype),
                "cls": bool(use_angle_cls),
            }

            try:
                self._conn.send(msg)
            except (BrokenPipeError, OSError, TypeError) as e:
                # TypeError is included deliberately: when a pipe is closed
                # while a write loop is in flight, multiprocessing raises
                # "TypeError: 'NoneType' object cannot be interpreted as an
                # integer" from write(self._handle, buf) rather than
                # BrokenPipeError. The lock should now prevent this, but we
                # keep the handler so any residual case still recycles
                # cleanly instead of escaping as an unhandled TypeError.
                _log.error("[PaddleOCR][SUBPROC] send failed — respawning")
                self._recycle("send_failed")
                raise RuntimeError(f"PaddleOCR subprocess pipe broken: {e}") from e

            if not self._conn.poll(timeout=CALL_TIMEOUT_S):
                _log.error(
                    f"[PaddleOCR][SUBPROC] child pid={getattr(self._proc, 'pid', '?')} "
                    f"timed out after {CALL_TIMEOUT_S:.0f}s — killing and respawning"
                )
                self._recycle("timeout")
                raise RuntimeError(
                    f"PaddleOCR subprocess timed out after {CALL_TIMEOUT_S:.0f}s"
                )

            try:
                reply = self._conn.recv()
            except (EOFError, OSError) as e:
                _log.error(
                    "[PaddleOCR][SUBPROC] child pid=%s died mid-call — respawning",
                    getattr(self._proc, 'pid', '?'),
                )
                self._recycle("child_died")
                raise RuntimeError(f"PaddleOCR subprocess died: {e}") from e

            if not reply.get("ok"):
                err = reply.get("error", "<no error>")
                tb = reply.get("traceback", "")
                # If it's the "could not execute a primitive" crash, recycle
                # the child before returning — subsequent calls in the same
                # child process are likely to hit the same corrupted state.
                if "could not execute a primitive" in err:
                    _log.error(
                        f"[PaddleOCR][SUBPROC] child hit 'could not execute a primitive' "
                        f"— recycling before returning error\n{tb}"
                    )
                    self._recycle("primitive_error")
                raise RuntimeError(f"PaddleOCR (subprocess): {err}\n{tb}")

            self._call_count += 1
            return reply["result"]


# ── Parent-side: multi-child pool ────────────────────────────────────────────

class PaddleOcrSubprocessPool:
    """A pool of N independent PaddleOCR child processes.

    Why a pool
    ----------
    With a single child, every OCR call in the service queues behind every
    other one.  On a document where most pages need OCR that child is the
    entire bottleneck — parallel page conversion gains nothing because all
    threads serialize on one pipe.

    Each child here is a separate OS process with its own pipe, its own
    Paddle native state and its own lock.  There is no shared mutable state
    between them, so N children deliver genuine N-way parallelism and races
    are impossible by construction rather than by discipline.

    Checkout model
    --------------
    Callers acquire an idle child from a `queue.Queue`, use it, and return
    it in a `finally` block.  The queue provides the blocking/backpressure:
    when all children are busy the caller waits instead of piling more work
    onto a child that is already mid-transfer.

    A child that fails is still returned to the queue — `_PaddleOcrChild`
    recycles itself internally on error, so the object stays usable.
    """

    def __init__(self, options: dict, size: Optional[int] = None):
        self._size = max(1, int(size or POOL_SIZE))
        self._options = dict(options)
        self._children: list = []
        self._idle: "_queue.Queue" = _queue.Queue()

        _t0 = time.perf_counter()
        for i in range(self._size):
            child = _PaddleOcrChild(self._options, index=i)
            self._children.append(child)
            self._idle.put(child)
        _log.info(
            f"[PaddleOCR][POOL] initialized size={self._size} "
            f"startup_ms={int((time.perf_counter() - _t0) * 1000)}"
        )

    @property
    def size(self) -> int:
        return self._size

    def ocr(self, image: np.ndarray, use_angle_cls: bool) -> Any:
        """Check out an idle child, run OCR on it, and return it to the pool.

        Blocks while every child is busy — that backpressure is intentional:
        it bounds how many multi-megabyte images are in flight at once.
        """
        child = self._idle.get()          # blocks until a child is free
        try:
            return child.ocr(image, use_angle_cls)
        finally:
            # Always return the child, including after a failure. The child
            # recycles its own subprocess on error, so it is ready for reuse.
            self._idle.put(child)

    def shutdown(self) -> None:
        """Tear down every child. Safe to call multiple times."""
        for child in self._children:
            try:
                child.shutdown()
            except Exception:
                _log.warning("[PaddleOCR][POOL] child shutdown failed")
        self._children = []
        # Drain the queue so stale references are not handed out.
        while True:
            try:
                self._idle.get_nowait()
            except Exception:
                break


# ── Module-level singleton (one per gunicorn worker) ─────────────────────────

_pool: Optional[PaddleOcrSubprocessPool] = None

# Guards creation/teardown of the singleton itself. Without it, two threads
# arriving at get_pool() simultaneously can both see `_pool is None` and each
# construct a pool — spawning a second set of children where one set is
# immediately orphaned (leaked processes holding ~500 MB each).
_pool_lock = threading.Lock()


def is_enabled() -> bool:
    """True when PADDLE_OCR_ISOLATE=1 explicitly enables it.

    Default is OFF so existing behavior is preserved unless the env var
    is set. Flip to `!= "0"` if we want to enable by default later.
    """
    return os.environ.get("PADDLE_OCR_ISOLATE", "0") == "1"


def get_pool(options: dict) -> PaddleOcrSubprocessPool:
    """Return the process-wide singleton pool, creating it if needed.

    `options` is only used the first time — it becomes the frozen
    PaddleOCR constructor kwargs for every child spawn. If you need
    to change options after the pool is created, call shutdown_pool()
    first.

    Thread-safe: uses double-checked locking so concurrent first-callers
    cannot each spawn a set of children.
    """
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:          # re-check under the lock
                _pool = PaddleOcrSubprocessPool(options)
    return _pool


def shutdown_pool() -> None:
    """Tear down the singleton pool. Safe to call multiple times."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown()
            _pool = None
