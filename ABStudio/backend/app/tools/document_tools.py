# SPDX-License-Identifier: Apache-2.0
"""
Document tools — read_document.

Active tools (seeded on startup):
  read_document — extract text (with OCR fallback) from a PDF, image, or
                  Office document referenced by absolute path or URL.

The tool is a thin wrapper around ``app.core.ocr_pipeline.extract`` so any
improvement to the pipeline (Camelot skipping, salvage-pass dedup, etc.)
benefits agents automatically without a per-tool code update.

Why this tool exists
--------------------
User-uploaded attachments are already OCR'd at the API boundary
(``POST /agent-runner/attachment``) BEFORE the workflow runs, so a plain
"Start → Agent → End" flow never needs this tool. The tool is needed
when a document arrives *inside* the workflow:

  * a Connector node downloads a PDF from SharePoint / Outlook attachment,
  * a code_executor step writes a scanned page to disk,
  * a URL is fetched by the agent from an external system,

and an agent then wants to read the text. The agent calls
``read_document({"file_path": "..."})`` or ``read_document({"url": "..."})``
and gets back the same extraction envelope the attachment route produces.

Sandbox constraints
-------------------
Every tool runs in an isolated subprocess (see ToolDispatcher). The sandbox
does NOT have ``app.*`` on its PYTHONPATH, so this tool re-implements the
OCR entry point using RapidOCR + pypdf/pypdfium2 directly (both permissively
licensed — no PyMuPDF/AGPL-3.0). That keeps the sandbox self-contained and
lets pip auto-install the runtime deps on first use.
The behaviour intentionally mirrors ``_extract_image`` / ``_extract_pdf``
in ``app/core/ocr_pipeline.py`` so results are consistent whether the
document arrived via the attachment route or via this tool.
"""

from __future__ import annotations

import inspect

from app.tools import _sandbox_net_guard

# ---------------------------------------------------------------------------
# read_document — sandbox source
# ---------------------------------------------------------------------------
# Runs inside the subprocess sandbox. The ToolDispatcher passes a JSON
# ``inputs`` dict on stdin; this ``run(inputs)`` prints one JSON line on
# stdout. Errors are returned as ``{"error": ...}`` so the LLM can react.
#
# The IP-denylist + bounded-DNS-resolve logic used by the SSRF guard below is
# NOT duplicated by hand here. It's read once from ``_sandbox_net_guard``
# (a self-contained, stdlib-only module also used by platform_tools.py's
# code_executor egress guard) via ``inspect.getsource`` and concatenated into
# this embedded source at import time in THIS process. Both sandbox copies
# are therefore always byte-identical to that one file — editing the net
# policy once updates both tools on the next process restart, which makes
# the "keep these two lists in sync" failure mode (previously caught by
# code review — the two hand-copied lists had already drifted) structurally
# impossible rather than merely documented.
# ---------------------------------------------------------------------------

_NET_GUARD_SRC = inspect.getsource(_sandbox_net_guard)

_READ_DOCUMENT_CODE = _NET_GUARD_SRC + '''
import os, sys, io, json, tempfile, subprocess, urllib.parse, urllib.request
import hashlib, time
from pathlib import Path

# Bounded so a runaway extraction (e.g. a 500-page PDF the user meant to
# summarise) doesn't blow past the sandbox 1 MB stdout cap and get killed.
_MAX_CHARS  = 60_000
_MAX_BYTES  = 25 * 1024 * 1024  # 25 MB — matches the attachment route ceiling

# ── SSRF / LFI guards (security review F-01) ──────────────────────────────
# read_document is reachable from LLM tool-call arguments (directly, or via
# indirect prompt injection embedded in a Jira ticket / uploaded document),
# so both of its input modes need to be constrained:
#   * file_path — must resolve inside a known workflow-artifact directory,
#     never an arbitrary host path (/etc/passwd, another user's files, .env).
#   * url       — must not resolve to a private/loopback/link-local address,
#     which would otherwise let a prompt reach internal services or the
#     cloud metadata endpoint (169.254.169.254).
# _resolve_host_with_timeout / _is_private_or_reserved_ip come from the
# _sandbox_net_guard snippet concatenated above. This is a DENYLIST check
# (block private/reserved, allow everything else) — the correct polarity
# for a tool whose job is fetching arbitrary public URLs. See the module
# comment in _sandbox_net_guard.py for why this differs from code_executor's
# allowlist-based guard.


def _assert_public_host(host):
    """Resolve ``host`` and raise ValueError if it lands in a private,
    loopback, link-local, or metadata IP range. Bounded by a timeout so a
    black-holed / slow DNS server can't hang the tool.

    Note: this resolves once, ahead of the actual request. It does not fully
    close DNS-rebinding races (a determined attacker could return a public IP
    here and a private one at connect-time) — closing that gap needs a
    dedicated egress proxy or a pinned-IP HTTP client, which is out of scope
    for this guard. This matches the immediate mitigation the security review
    calls for; the proxy is tracked as a longer-term follow-up.
    """
    try:
        ip_str = _resolve_host_with_timeout(host)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot resolve host {host!r}: {exc}") from exc
    if _is_private_or_reserved_ip(ip_str):
        raise ValueError(
            f"url host {host!r} resolves to a private/internal address "
            f"({ip_str}) — blocked by network policy"
        )


def _resolve_allowed_roots():
    """Directories the sandbox is allowed to read files from via file_path.

    Sourced from the same env vars the dispatcher already injects for this
    run (see agent_factory/pipeline.py ToolDispatcher._run_in_sandbox):
    GENERATED_FILES_DIR, WORKFLOW_ARTIFACT_DIR, RUNTIME_ARTIFACTS_DIR,
    and — when the agent has an attached Sample Document — SAMPLE_DOC_DIR
    so the LLM can `read_document({"file_path": os.environ["SAMPLE_DOC_PATH"]})`
    to study its content and layout mid-run. SAMPLE_DOC_DIR is missing on
    the vast majority of runs (no sample attached), which is fine — a
    missing env var just skips that root.
    """
    roots = []
    for var in (
        "GENERATED_FILES_DIR",
        "WORKFLOW_ARTIFACT_DIR",
        "RUNTIME_ARTIFACTS_DIR",
        "SAMPLE_DOC_DIR",
    ):
        val = os.environ.get(var)
        if val:
            try:
                roots.append(Path(val).resolve())
            except OSError:
                pass
    return roots


def _assert_path_in_allowed_roots(path):
    """Raise ValueError unless ``path`` resolves inside a known artifact root.

    Uses Path.resolve() + relative_to() rather than a hand-rolled
    startswith(root + os.sep) prefix check — the latter mishandles an
    allow-root that itself resolves to a filesystem root (e.g. "C:\\\\" on
    Windows, "/" on POSIX), where root + separator produces a doubled
    separator that a legitimate child path never starts with.
    """
    roots = _resolve_allowed_roots()
    if not roots:
        raise ValueError(
            "no workflow artifact directories are configured; file_path reads are disabled."
        )
    try:
        resolved = Path(path).resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve file_path: {exc}") from exc
    for root in roots:
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise ValueError("file_path must be inside an allowed workflow artifact directory.")

# ── Content-hash OCR result cache (mirrors app.core.ocr_cache) ────────────
# Sandbox subprocesses can\\'t import app.*, so we reimplement the cache
# using stdlib. We deliberately point at the SAME on-disk directory the
# chat-attachment pipeline (ocr_pipeline.extract) uses, so a file OCR\\'d
# via chat upload gets a cache hit when later read mid-workflow via this
# tool (and vice-versa). Cache is best-effort: any error → cache miss.
_CACHE_MAX_ENTRIES = 500

def _cache_dir():
    """Return backend/runtime_artifacts/ocr_cache/, creating it if possible.

    The tool source lives at ``backend/app/tools/document_tools.py``, so
    walking three levels up from this sandbox\\'s embedded __file__ isn\\'t
    reliable (the code is exec\\'d, not imported from disk). We instead
    honor an env var if the parent process set one, else fall back to
    walking up from the current working directory looking for a
    ``runtime_artifacts`` sibling — the same layout ocr_cache.py assumes.
    """
    override = os.environ.get("ABSTUDIO_OCR_CACHE_DIR")
    if override:
        p = override
    else:
        # Walk up looking for a "backend" ancestor, then use its
        # runtime_artifacts/ocr_cache. This matches ocr_cache.py\\'s
        # _CACHE_DIR resolution (parent.parent.parent of app/core/ocr_cache.py
        # == backend/, then + runtime_artifacts/ocr_cache).
        here = os.path.abspath(os.getcwd())
        p = None
        cur = here
        for _ in range(6):
            cand = os.path.join(cur, "runtime_artifacts", "ocr_cache")
            if os.path.isdir(os.path.join(cur, "runtime_artifacts")):
                p = cand
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        if not p:
            # Last-resort: system temp. Correctness (never blocks) beats
            # cross-path sharing when we can\\'t find the canonical dir.
            p = os.path.join(tempfile.gettempdir(), "abstudio_ocr_cache")
    try:
        os.makedirs(p, exist_ok=True)
    except OSError:
        pass
    return p


def _sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _options_fp(options):
    """Stable 16-char hash of options that influence output. Mirrors
    _options_fingerprint in ocr_cache.py."""
    canonical = json.dumps(options, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _cache_entry_path(sha, opt_fp):
    return os.path.join(_cache_dir(), sha + "_" + opt_fp + ".json")


def _cache_get(sha, opt_fp):
    try:
        path = _cache_entry_path(sha, opt_fp)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        # Touch atime so LRU eviction keeps recently-used entries warm.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return payload
    except Exception:
        return None


def _cache_put(sha, opt_fp, payload):
    try:
        target = _cache_entry_path(sha, opt_fp)
        cache_dir = os.path.dirname(target)
        fd, tmp_name = tempfile.mkstemp(
            prefix="." + sha[:8] + "_", suffix=".json", dir=cache_dir,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, target)
        except Exception:
            try: os.unlink(tmp_name)
            except OSError: pass
            raise
        _cache_maybe_evict(cache_dir)
    except Exception:
        pass


def _cache_maybe_evict(cache_dir):
    try:
        entries = []
        for name in os.listdir(cache_dir):
            if name.endswith(".json") and not name.startswith("."):
                full = os.path.join(cache_dir, name)
                if os.path.isfile(full):
                    entries.append(full)
        if len(entries) <= _CACHE_MAX_ENTRIES:
            return
        entries.sort(key=lambda p: os.stat(p).st_atime)
        for p in entries[: len(entries) - _CACHE_MAX_ENTRIES]:
            try: os.unlink(p)
            except OSError: pass
    except Exception:
        pass


def _cache_stale(warnings):
    """Return True if any warning says \\'X unavailable\\' for a lib that
    is NOW importable — matches _stale_missing_lib_warnings in ocr_pipeline.
    Cheap probe: try importing the sentinel names inline."""
    if not warnings:
        return False
    text = " ".join(str(w).lower() for w in warnings)
    probes = []
    if "pypdf" in text: probes.append("pypdf")
    if "pypdfium2" in text: probes.append("pypdfium2")
    if "rapidocr" in text: probes.append("rapidocr_onnxruntime")
    if "openpyxl" in text: probes.append("openpyxl")
    if "pandas" in text: probes.append("pandas")
    if "python-docx" in text or "python_docx" in text: probes.append("docx")
    if "python-pptx" in text or "python_pptx" in text: probes.append("pptx")
    if "beautifulsoup" in text or "bs4" in text: probes.append("bs4")
    if "striprtf" in text: probes.append("striprtf")
    if "tabulate" in text: probes.append("tabulate")
    for mod in probes:
        try:
            __import__(mod)
            return True   # lib available now → cached result is stale
        except ImportError:
            continue
    return False


# ── Path A (chat attachment) key sniffing ─────────────────────────────
# Path A writes cache entries under a DIFFERENT options fingerprint than
# we do. Both live in the same directory, so if the user uploaded a file
# in chat first (Path A cached it), a later read_document call would
# still miss its own key. We fix that with a dual-key lookup: after our
# own miss, probe Path A\\'s canonical key. If the payload is intact
# enough to reuse (untruncated, or long enough that our own extraction
# wouldn\\'t have produced more), we serve it.
#
# Path A defaults live in ocr_pipeline.ExtractionOptions.to_cache_key():
#   force_ocr=False, describe_visuals=False, ocr_lang="en",
#   extract_images=True, extract_tables=True
# The pipeline\\'s _options_fingerprint hashes json.dumps(sort_keys=True)
# of those 5 fields and takes the first 16 hex chars — identical algo
# to our own _options_fp() so we can just call it with the right dict.
_PATH_A_DEFAULT_OPTS = {
    "describe_visuals": False,
    "extract_images":   True,
    "extract_tables":   True,
    "force_ocr":        False,
    "ocr_lang":         "en",
}


def _path_a_opt_fp():
    return _options_fp(_PATH_A_DEFAULT_OPTS)


def _cache_get_path_a(sha):
    """Try Path A\\'s key. Return the payload only if it\\'s safe to reuse
    at Path B\\'s max_chars budget.

    Safety rule: we can serve a Path A payload iff either
        (a) truncated == False   → we have the whole document, or
        (b) len(text) >= _MAX_CHARS → even truncated it fills our budget
    otherwise Path A returned less than our own extraction would have,
    and we must re-run.
    """
    try:
        payload = _cache_get(sha, _path_a_opt_fp())
        if not payload:
            return None
        text = payload.get("text") or ""
        truncated = bool(payload.get("truncated", False))
        if not truncated or len(text) >= _MAX_CHARS:
            return payload
        return None
    except Exception:
        return None


_IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "tiff", "tif", "bmp", "webp"})
_STRUCTURED_EXTS = frozenset({
    "pdf", "docx", "pptx", "xlsx", "xls", "csv", "html", "htm", "rtf",
    "txt", "md", "json",
})


def _ensure_pkg(pkg_import_name, pip_name=None):
    """Import a package, pip-installing it into the sandbox on demand.

    RapidOCR + pypdf/pypdfium2 are ~50 MB together — we only pay the
    install cost the first time this tool runs on a fresh sandbox host.
    """
    try:
        return __import__(pkg_import_name)
    except ImportError:
        pass
    pip_pkg = pip_name or pkg_import_name
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", pip_pkg],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise ImportError(f"pip install {pip_pkg} failed: {exc}") from exc
    return __import__(pkg_import_name)


def _download(url, dest_path):
    """Fetch ``url`` to ``dest_path`` with a size cap. HTTPS + HTTP only.

    Blocks requests to private/loopback/link-local/metadata IP ranges (see
    ``_assert_public_host``) so an LLM prompt can't use this tool to reach
    internal services or the cloud metadata endpoint (SSRF).
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"only http(s) URLs are allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("url has no hostname")
    _assert_public_host(parsed.hostname)
    req = urllib.request.Request(url, headers={"User-Agent": "abstudio-read-document/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        # Size guard — read in chunks so a huge stream is stopped early.
        total = 0
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise ValueError(f"remote file exceeded {_MAX_BYTES} bytes")
                fh.write(chunk)
    return dest_path


def _ext_of(path_or_name, mime_hint=""):
    _, ext = os.path.splitext(path_or_name or "")
    ext = (ext or "").lstrip(".").lower()
    if ext:
        return ext
    # Fall back to the MIME hint when the filename has no extension
    # (common for downloaded artifacts named after their entity id).
    mime = (mime_hint or "").lower()
    if "pdf" in mime:              return "pdf"
    if "png" in mime:              return "png"
    if "jpeg" in mime or "jpg" in mime: return "jpg"
    if "tiff" in mime or "tif" in mime: return "tif"
    if "webp" in mime:             return "webp"
    if "bmp" in mime:              return "bmp"
    return ""


def _preprocess_image(img_bytes):
    """Grayscale + contrast + sharpen + soft binarise → cleaner OCR input.

    Mirrors ``_preprocess_image_for_ocr`` in ``app/core/ocr_pipeline.py``
    so results are consistent between the attachment route and this tool.
    """
    try:
        PIL = _ensure_pkg("PIL", "Pillow")
        from PIL import Image, ImageOps, ImageFilter
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(img_bytes)) as im:
            im = ImageOps.exif_transpose(im).convert("L")
            im = ImageOps.autocontrast(im, cutoff=2)
            im = im.filter(ImageFilter.SHARPEN)
            im = im.point(lambda p: 255 if p > 180 else (0 if p < 80 else p))
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=False)
            return buf.getvalue()
    except Exception:
        return None


_OCR_ENGINE = None


def _get_ocr():
    """Lazy-load RapidOCR once per sandbox invocation."""
    global _OCR_ENGINE
    if _OCR_ENGINE is not None:
        return _OCR_ENGINE
    for mod_name, pip_name in [
        ("rapidocr_onnxruntime", "rapidocr_onnxruntime"),
        ("rapidocr", "rapidocr"),
    ]:
        try:
            mod = _ensure_pkg(mod_name, pip_name)
        except Exception:
            continue
        RapidOCR = getattr(mod, "RapidOCR", None)
        if RapidOCR is None:
            continue
        try:
            _OCR_ENGINE = RapidOCR(use_angle_cls=True)
        except TypeError:
            try:
                _OCR_ENGINE = RapidOCR()
            except Exception:
                _OCR_ENGINE = None
        if _OCR_ENGINE is not None:
            return _OCR_ENGINE
    return None


def _ocr_bytes(img_bytes, ext="png"):
    """Returns (text, line_count, extraction_failed).

    ARCH-F-ABS1-002: previously returned ("", 0) on both "no text in image"
    and "OCR engine crashed" — callers had no way to distinguish the two.
    extraction_failed=True marks the latter case and is logged as a warning.
    """
    engine = _get_ocr()
    if engine is None:
        logger.warning("OCR engine unavailable")
        return "", 0, True
    fd, tmp = tempfile.mkstemp(suffix=f".{ext or 'png'}")
    try:
        os.write(fd, img_bytes)
    finally:
        os.close(fd)
    try:
        raw = engine(tmp)
    except Exception as exc:
        logger.warning("OCR engine call failed: %s", exc)
        return "", 0, True
    finally:
        try: os.unlink(tmp)
        except OSError: pass
    if raw is None:
        return "", 0, False
    # New rapidocr package: RapidOCROutput with .txts
    txts = getattr(raw, "txts", None)
    if txts is not None:
        lines = [str(t) for t in txts if t]
        return "\\n".join(lines).strip(), len(lines), False
    # Legacy: (boxes, elapsed) or bare boxes
    boxes = raw[0] if isinstance(raw, tuple) and raw else raw
    if not boxes:
        return "", 0, False
    lines = []
    for det in boxes:
        try:
            text = det[1] if len(det) >= 2 else ""
        except Exception:
            text = ""
        if text:
            lines.append(str(text))
    return "\\n".join(lines).strip(), len(lines), False


def _extract_image_file(path):
    with open(path, "rb") as fh:
        raw_bytes = fh.read()
    ext = _ext_of(path)
    pre = _preprocess_image(raw_bytes)
    text, _, _extraction_failed = _ocr_bytes(pre or raw_bytes, ext or "png")
    return {"text": text, "engine": "rapidocr" if text else "image-empty", "page_count": 1}


def _extract_pdf_file(path):
    """Multi-pass PDF extraction — mirrors the shape of
    ``app/core/ocr_pipeline._extract_pdf`` (text-layer → OCR fallback)
    while staying sandbox-safe (RapidOCR + pypdf/pypdfium2, both permissively
    licensed — no PyMuPDF/AGPL-3.0)."""
    try:
        pypdf = _ensure_pkg("pypdf")
    except ImportError as exc:
        return {"text": "", "engine": "empty", "page_count": 0,
                "warnings": [f"pypdf unavailable: {exc}"]}

    try:
        reader = pypdf.PdfReader(path)
    except Exception as exc:
        return {"text": "", "engine": "empty", "page_count": 0,
                "warnings": [f"could not open PDF: {exc}"]}

    warnings = []
    text_parts = []
    empty_pages = 0
    page_count = len(reader.pages)
    for i in range(page_count):
        try:
            page_text = (reader.pages[i].extract_text() or "").strip()
        except Exception as exc:
            page_text = ""
            warnings.append(f"text-layer page {i + 1}: {exc}")
        if page_text:
            text_parts.append(page_text)
        else:
            empty_pages += 1

    text_layer = "\n\n".join(text_parts).strip()
    fully_scanned = (
        not text_layer
        or (page_count > 0 and empty_pages / page_count >= 0.3)
    )

    if fully_scanned:
        # OCR every page at 2.2x zoom (158 dpi) — matches the salvage pass DPI
        # in ocr_pipeline. Bounded to 50 pages so a huge scanned book doesn't
        # blow the sandbox timeout / stdout cap.
        try:
            pdfium = _ensure_pkg("pypdfium2")
        except ImportError as exc:
            warnings.append(f"pypdfium2 unavailable for OCR pass: {exc}")
            return {"text": "", "engine": "empty", "page_count": page_count,
                    "warnings": warnings}

        ocr_parts = []
        max_pages = min(page_count, 50)
        try:
            pdf_doc = pdfium.PdfDocument(path)
        except Exception as exc:
            warnings.append(f"pypdfium2 could not open PDF for OCR: {exc}")
            return {"text": "", "engine": "empty", "page_count": page_count,
                    "warnings": warnings}
        try:
            for i in range(max_pages):
                try:
                    page = pdf_doc.get_page(i)
                    try:
                        bitmap = page.render(scale=2.2)
                        pil_image = bitmap.to_pil()
                    finally:
                        page.close()
                    buf = io.BytesIO()
                    pil_image.save(buf, format="PNG")
                    img_bytes = buf.getvalue()
                    pre = _preprocess_image(img_bytes) or img_bytes
                    txt, _, _extraction_failed = _ocr_bytes(pre, "png")
                    if txt:
                        ocr_parts.append(f"## Page {i + 1}\n{txt}")
                except Exception as exc:
                    warnings.append(f"ocr page {i + 1}: {exc}")
        finally:
            pdf_doc.close()
        if page_count > max_pages:
            warnings.append(
                f"OCR truncated at {max_pages}/{page_count} pages to bound latency."
            )
        final = "\n\n".join(ocr_parts).strip()
        return {
            "text": final,
            "engine": "rapidocr" if final else "empty",
            "page_count": page_count,
            "warnings": warnings,
        }

    return {
        "text": text_layer,
        "engine": "text-layer",
        "page_count": page_count,
        "warnings": warnings,
    }


def _extract_structured_file(path, ext):
    """Delegate DOCX / PPTX / XLSX / XLS / CSV / HTML / RTF / TXT / MD / JSON to
    per-format extractors that MIRROR the shape of
    ``core.document_parser.parse_file_structured`` (Path A — chat attachment
    route). Same input file therefore produces byte-identical text whether it
    entered via chat upload or via this mid-workflow tool call.

    Format mapping (matches ``core/document_parser.py``):
      xlsx / xls  → pandas + tabulate GitHub-flavoured Markdown tables,
                    one ``## SheetName`` heading per sheet (multi-sheet only)
      csv         → pandas + tabulate GitHub Markdown table
      docx        → python-docx walking body elements, ``#..######`` for
                    headings, Markdown pipe tables for embedded tables,
                    ``- bullet`` for list paragraphs
      pptx        → python-pptx per-slide with ``### Slide N`` heading,
                    ``## title`` from the title placeholder, ``- item``
                    for indented bullets, Markdown pipe tables for shape
                    tables
      html / htm  → BeautifulSoup drop script/style/head/meta/noscript,
                    then get_text(separator='\\n')
      rtf         → striprtf.rtf_to_text
      txt / md    → raw read
      json        → json.load → json.dumps(indent=2)
    """
    try:
        if ext == "txt" or ext == "md":
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return {"text": fh.read(), "engine": "text", "page_count": 1}

        if ext == "json":
            import json as _json
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                data = _json.load(fh)
            return {"text": _json.dumps(data, indent=2), "engine": "json",
                    "page_count": 1}

        if ext in ("html", "htm"):
            bs4 = _ensure_pkg("bs4", "beautifulsoup4")
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                soup = bs4.BeautifulSoup(fh.read(), "html.parser")
            # Strip noise BEFORE get_text so JS/CSS/meta doesn't pollute output.
            for tag in soup(["script", "style", "head", "meta", "noscript"]):
                tag.decompose()
            return {"text": soup.get_text(separator="\\n", strip=True),
                    "engine": "html", "page_count": 1}

        if ext == "rtf":
            striprtf_mod = _ensure_pkg("striprtf", "striprtf")
            # striprtf exports rtf_to_text under striprtf.striprtf
            try:
                from striprtf.striprtf import rtf_to_text
            except ImportError:
                rtf_to_text = getattr(striprtf_mod, "rtf_to_text", None)
                if rtf_to_text is None:
                    raise
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                return {"text": rtf_to_text(fh.read()), "engine": "rtf",
                        "page_count": 1}

        if ext == "csv":
            pd = _ensure_pkg("pandas", "pandas")
            df = pd.read_csv(path)
            try:
                tabulate_mod = _ensure_pkg("tabulate", "tabulate")
                text = tabulate_mod.tabulate(df, headers="keys",
                                             tablefmt="github", showindex=False)
            except ImportError:
                text = df.to_string(index=False)
            return {"text": text, "engine": "csv", "page_count": 1}

        if ext in ("xlsx", "xls"):
            pd = _ensure_pkg("pandas", "pandas")
            # openpyxl is the engine pandas needs for .xlsx; xlrd for legacy .xls.
            if ext == "xlsx":
                _ensure_pkg("openpyxl", "openpyxl")
            else:
                _ensure_pkg("xlrd", "xlrd")
            try:
                tabulate_mod = _ensure_pkg("tabulate", "tabulate")
                _tab = lambda df: tabulate_mod.tabulate(
                    df, headers="keys", tablefmt="github", showindex=False)
            except ImportError:
                _tab = lambda df: df.to_string(index=False)
            xl = pd.ExcelFile(path)
            parts = []
            for sheet in xl.sheet_names:
                df = xl.parse(sheet)
                if df.empty:
                    continue
                if len(xl.sheet_names) > 1:
                    parts.append(f"## {sheet}\\n")
                parts.append(_tab(df))
            text = "\\n\\n".join(parts) if parts else "[Excel file is empty]"
            return {"text": text, "engine": "xlsx",
                    "page_count": len(xl.sheet_names)}

        if ext == "docx":
            docx_mod = _ensure_pkg("docx", "python-docx")
            d = docx_mod.Document(path)
            _HEADING = {
                "heading 1": "#", "heading 2": "##", "heading 3": "###",
                "heading 4": "####", "heading 5": "#####", "heading 6": "######",
                "title": "#", "subtitle": "##",
            }

            def _table_to_md(table):
                rows = []
                for i, row in enumerate(table.rows):
                    cells = [c.text.replace("\\n", " ").strip() for c in row.cells]
                    rows.append("| " + " | ".join(cells) + " |")
                    if i == 0:
                        rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
                return "\\n".join(rows)

            lines = []
            for block in d.element.body:
                tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag
                if tag == "tbl":
                    for tbl in d.tables:
                        if tbl._tbl is block:
                            lines.append(_table_to_md(tbl))
                            lines.append("")
                            break
                elif tag == "p":
                    for para in d.paragraphs:
                        if para._p is block:
                            text = para.text.strip()
                            if not text:
                                lines.append("")
                                break
                            style = para.style.name.lower() if para.style else ""
                            if style in _HEADING:
                                lines.append(f"{_HEADING[style]} {text}")
                            elif style in ("list paragraph", "list bullet", "list number"):
                                lines.append(f"- {text}")
                            else:
                                lines.append(text)
                            break
            return {"text": "\\n".join(lines), "engine": "docx", "page_count": 1}

        if ext == "pptx":
            pptx_mod = _ensure_pkg("pptx", "python-pptx")
            prs = pptx_mod.Presentation(path)
            slides = []
            for i, slide in enumerate(prs.slides, start=1):
                parts = []
                title_ph = slide.shapes.title
                if title_ph and title_ph.has_text_frame:
                    title_text = title_ph.text_frame.text.strip()
                    if title_text:
                        parts.append(f"## {title_text}")
                for shape in slide.shapes:
                    if shape == title_ph:
                        continue
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            line = para.text.strip()
                            if line:
                                parts.append(f"- {line}" if para.level > 0 else line)
                    elif shape.has_table:
                        tbl = shape.table
                        rows = []
                        for r_idx, row in enumerate(tbl.rows):
                            cells = [cell.text.replace("\\n", " ").strip()
                                     for cell in row.cells]
                            rows.append("| " + " | ".join(cells) + " |")
                            if r_idx == 0:
                                rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
                        parts.append("\\n".join(rows))
                if parts:
                    slides.append(f"### Slide {i}\\n" + "\\n".join(parts))
            text = "\\n\\n".join(slides) if slides else "[Presentation has no text content]"
            return {"text": text, "engine": "pptx", "page_count": len(prs.slides)}

    except ImportError as exc:
        return {"text": "", "engine": "empty", "page_count": 0,
                "warnings": [f"library for .{ext} unavailable: {exc}"]}
    except Exception as exc:
        return {"text": "", "engine": "empty", "page_count": 0,
                "warnings": [f".{ext} parse failed: {exc}"]}
    return {"text": "", "engine": "empty", "page_count": 0,
            "warnings": [f"unsupported extension .{ext}"]}


def _truncate(result):
    original = len(result.get("text") or "")
    result["original_char_count"] = original
    if original > _MAX_CHARS:
        result["text"] = result["text"][:_MAX_CHARS]
        result["truncated"] = True
    else:
        result["truncated"] = False
    result["char_count"] = len(result.get("text") or "")
    return result


def run(inputs):
    file_path = (inputs.get("file_path") or "").strip()
    url       = (inputs.get("url") or "").strip()
    mime_hint = (inputs.get("mime_type") or "").strip()
    force_ocr = bool(inputs.get("force_ocr"))

    if not file_path and not url:
        return {"error": "Provide either 'file_path' (absolute) or 'url' (http/https)."}
    if file_path and url:
        return {"error": "Provide only one of 'file_path' or 'url', not both."}

    tmp_download = None
    try:
        if url:
            # Name the temp file by the URL basename so extension sniffing
            # still works. We don't trust the server-declared MIME alone.
            base = os.path.basename(urllib.parse.urlparse(url).path) or "download"
            fd, tmp_download = tempfile.mkstemp(prefix="doc_", suffix="_" + base)
            os.close(fd)
            try:
                _download(url, tmp_download)
            except Exception as exc:
                return {"error": f"download failed: {exc}"}
            source_path = tmp_download
            source_name = base
        else:
            if not os.path.isabs(file_path):
                return {"error": "file_path must be an absolute path."}
            try:
                _assert_path_in_allowed_roots(file_path)
            except ValueError as exc:
                return {"error": str(exc)}
            if not os.path.exists(file_path):
                return {"error": f"file_path does not exist: {file_path}"}
            try:
                if os.path.getsize(file_path) > _MAX_BYTES:
                    return {"error": f"file exceeds {_MAX_BYTES} byte limit."}
            except OSError as exc:
                return {"error": f"cannot stat file_path: {exc}"}
            source_path = file_path
            source_name = os.path.basename(file_path)

        ext = _ext_of(source_name, mime_hint)
        if not ext:
            return {"error": "cannot determine file type; pass 'mime_type' hint."}

        if ext in _IMAGE_EXTS:
            result = _extract_image_file(source_path)
        elif ext == "pdf":
            result = _extract_pdf_file(source_path)
        elif ext in _STRUCTURED_EXTS:
            result = _extract_structured_file(source_path, ext)
        else:
            return {"error": f"unsupported file extension: .{ext}"}

        result.setdefault("warnings", [])
        result["source"] = source_name
        result["cache_hit"] = False
        result["cache_source"] = None
        final = _truncate(result)

        return final
    finally:
        if tmp_download:
            try: os.unlink(tmp_download)
            except OSError: pass
'''


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DOCUMENT_TOOLS = [
    {
        "name": "read_document",
        "description": (
            "Extract readable text from a document file (PDF, image, DOCX, PPTX, "
            "XLSX, XLS, CSV, HTML, RTF, TXT, MD, JSON) by absolute file path OR "
            "http(s) URL. PDFs use the embedded text layer when available and "
            "fall back to OCR (RapidOCR, local, no API key) for scanned pages. "
            "Standalone images (PNG/JPG/TIFF/BMP/WEBP) are OCR'd directly. "
            "Office formats produce the SAME GitHub-flavoured Markdown output "
            "as the chat-attachment pipeline: Excel/CSV → Markdown tables, "
            "DOCX → Markdown headings + pipe tables, PPTX → per-slide "
            "'### Slide N' sections. "
            "Use this when a document arrives INSIDE the workflow — e.g. a "
            "Connector downloads a PDF from SharePoint, or code_executor writes "
            "a scanned page to disk. Do NOT use it for files the user attached "
            "in the chat: those are already extracted at upload time and their "
            "text is already in the current prompt. "
            "Returns: text (up to 60,000 chars, truncated flag set if exceeded), "
            "engine ('text-layer'|'rapidocr'|'docx'|'pptx'|'xlsx'|'csv'|'html'"
            "|'rtf'|'json'|'text'|...), page_count, char_count, warnings[], "
            "source, cache_hit (bool), cache_source ('path_a'|null when miss "
            "or Path B write)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute path to a local file (PDF, image, or Office "
                        "document). Mutually exclusive with 'url'."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "http(s) URL of a remote document. The file is downloaded "
                        "with a 25 MB size cap. Mutually exclusive with 'file_path'."
                    ),
                },
                "mime_type": {
                    "type": "string",
                    "description": (
                        "Optional MIME hint (e.g. 'application/pdf', 'image/png'). "
                        "Only needed when the file/URL has no extension."
                    ),
                },
                "no_cache": {
                    "type": "boolean",
                    "description": "Deprecated — caching is disabled. Kept for backward compatibility.",
                },
                "force_ocr": {
                    "type": "boolean",
                    "description": (
                        "If true, force OCR even when a text layer is present."
                    ),
                },
            },
            "required": [],
        },
        "service": "platform",
        "code": _READ_DOCUMENT_CODE,
    },
]
