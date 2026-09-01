# SPDX-License-Identifier: Apache-2.0
"""
Document-skill sandbox executor.

Runs Claude's document skills inside the isolated `ainxt-doc-sandbox` image —
network-disabled, non-root, memory/CPU/pids capped, read-only root FS with a
single writable bind-mounted work dir. The office agent authors the build code
per each skill's SKILL.md; we run it here and render a page-image preview
(soffice → pdf → pdftoppm) so the result shows in-app.

Per format:
  docx → node  (docx-js)    → output.docx
  pptx → node  (pptxgenjs)  → output.pptx
  xlsx → python (openpyxl)  → output.xlsx
  pdf  → node  (docx-js)    → output.docx → exported to output.pdf (beautiful)

Returns artifact BYTES (not container paths) so callers can persist/serve them.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from core.logger import logger

IMAGE = os.getenv("AINXT_DOC_SANDBOX_IMAGE", "ainxt-doc-sandbox:latest")
BUILD_TIMEOUT_S = int(os.getenv("AINXT_DOC_BUILD_TIMEOUT_S", "1800"))  # 30 min
PREVIEW_DPI = int(os.getenv("AINXT_DOC_PREVIEW_DPI", "150"))           # sharper previews
MAX_PREVIEW_PAGES = int(os.getenv("AINXT_DOC_PREVIEW_PAGES", "20"))

# Text→image generation for embedding AI-generated visuals in docs (multimodal).
# Routed through the LLM proxy's /llm/imagen (approved providers ONLY: Imagen /
# DALL-E — never stock-photo APIs). Images are generated HERE (in the worker) and
# written into the sandbox work dir BEFORE the build runs, so the build code can
# embed them by filename while the sandbox itself stays network-isolated.
# No hardcoded localhost default — an unset/unreachable value fails the
# httpx call in _generate_doc_image() below, which the caller already
# handles by skipping that image.
_LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "")
DOC_IMAGE_TIMEOUT_S = int(os.getenv("AINXT_DOC_IMAGE_TIMEOUT_S", "120"))
MAX_DOC_IMAGES = int(os.getenv("AINXT_DOC_MAX_IMAGES", "8"))
_IMG_PROVIDER = os.getenv("AINXT_DOC_IMAGE_PROVIDER", "openai")  # gemini|openai


def _safe_image_name(name: str, idx: int) -> str:
    """Sanitise an agent-supplied image filename (no path traversal / abs paths)."""
    base = os.path.basename(str(name or "").strip()) or f"image_{idx}.png"
    base = "".join(c for c in base if c.isalnum() or c in "._-") or f"image_{idx}.png"
    if not base.lower().endswith((".png", ".jpg", ".jpeg")):
        base += ".png"
    return base[:64]


def _generate_doc_image(prompt: str, aspect_ratio: str, provider: str) -> bytes:
    """Generate one image via the approved-provider imagen endpoint. Returns raw
    bytes (PNG/JPEG). Raises on failure so the caller can skip just that image."""
    import httpx
    payload = {
        "provider": provider if provider in ("gemini", "openai") else _IMG_PROVIDER,
        "prompt": prompt,
        "aspect_ratio": aspect_ratio or "16:9",
        "number_of_images": 1,
    }
    with httpx.Client(timeout=DOC_IMAGE_TIMEOUT_S) as client:
        resp = client.post(f"{_LLM_PROXY_URL}/llm/imagen", json=payload)
        resp.raise_for_status()
        data = resp.content  # endpoint returns raw image bytes
        if not data or len(data) < 128:
            raise RuntimeError("imagen returned empty/too-small image")
        return data

# Per-format build recipe. `src` is the file the agent's code is written to;
# `interp` runs it; `built` is the file that code produces; `deliver` is the
# extension of the file the user downloads. `convert` = the built file is run
# through soffice to produce the deliverable (used for pdf: docx → pdf).
_FORMATS = {
    "docx": {"interp": "node",    "src": "build.js", "built": "output.docx", "deliver": "docx", "convert": False},
    "pptx": {"interp": "node",    "src": "build.js", "built": "output.pptx", "deliver": "pptx", "convert": False},
    "xlsx": {"interp": "python3", "src": "build.py", "built": "output.xlsx", "deliver": "xlsx", "convert": False},
    "pdf":  {"interp": "node",    "src": "build.js", "built": "output.docx", "deliver": "pdf",  "convert": True},
}


@dataclass
class DocBuildResult:
    ok: bool
    error: str = ""
    logs: str = ""
    doc_bytes: bytes = b""
    ext: str = "docx"
    pdf_bytes: bytes = b""
    page_images: list[bytes] = field(default_factory=list)  # JPEG bytes, page order


def docker_available() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


def image_present() -> bool:
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=10).returncode == 0
    except Exception:
        return False


def supported_formats() -> list[str]:
    return list(_FORMATS.keys())


def build(code: str, fmt: str, images: list | None = None) -> DocBuildResult:
    """Run agent-authored build `code` for `fmt` in the sandbox; return the
    deliverable file + a page-image preview.

    `images` (optional): list of {name, prompt, aspect_ratio?, provider?}. Each is
    generated via the approved-provider imagen endpoint and written into the work
    dir as `name` BEFORE the build runs, so the build code can embed it by
    filename (e.g. docx ImageRun / pptxgenjs addImage / openpyxl add_image). Image
    generation failures are non-fatal — the doc still builds without that image."""
    fmt = (fmt or "docx").lower()
    spec = _FORMATS.get(fmt)
    if not spec:
        return DocBuildResult(ok=False, error=f"Unsupported document format: {fmt!r}")
    if not docker_available():
        return DocBuildResult(ok=False, error="Document sandbox unavailable: Docker is not running.")
    if not image_present():
        return DocBuildResult(
            ok=False,
            error=f"Document sandbox image '{IMAGE}' not built. Run: bash docker/doc-sandbox/build.sh",
        )

    deliver_file = f"output.{spec['deliver']}"
    # Container script: run the agent's code → (convert to deliverable if needed)
    # → render a page-image preview. Read-only root → writable HOME/profile on tmpfs.
    parts = [
        "set -e", "cd /work", "export HOME=/tmp",
        f"{spec['interp']} {spec['src']}",
        f"test -f {spec['built']}",
    ]
    if spec["convert"]:
        # Export the built source (e.g. docx) to the deliverable (pdf) via soffice.
        parts.append(
            f"soffice --headless -env:UserInstallation=file:///tmp/loprofile "
            f"--convert-to {spec['deliver']} --outdir /work {spec['built']} >/dev/null 2>&1"
        )
        parts.append(f"test -f {deliver_file}")
    # Preview: pdf deliverable rasterizes directly; others render via soffice→pdf.
    if spec["deliver"] == "pdf":
        parts.append(f"( pdftoppm -jpeg -r {PREVIEW_DPI} {deliver_file} page >/dev/null 2>&1 ) || true")
    else:
        parts.append(
            f"( soffice --headless -env:UserInstallation=file:///tmp/loprofile "
            f"    --convert-to pdf --outdir /work {deliver_file} >/dev/null 2>&1 "
            f"  && pdftoppm -jpeg -r {PREVIEW_DPI} output.pdf page >/dev/null 2>&1 ) || true"
        )
    run_script = "; ".join(parts)

    workdir = tempfile.mkdtemp(prefix="ainxt-doc-")
    try:
        src_path = os.path.join(workdir, spec["src"])
        with open(src_path, "w", encoding="utf-8") as fh:
            fh.write(code)

        # Pre-generate any requested images into the work dir (network-isolated
        # sandbox can't fetch them itself). Each failure is non-fatal.
        image_paths: list[str] = []
        for idx, img in enumerate((images or [])[:MAX_DOC_IMAGES]):
            try:
                if not isinstance(img, dict):
                    continue
                prompt = str(img.get("prompt") or "").strip()
                if not prompt:
                    continue
                name = _safe_image_name(img.get("name"), idx)
                data = _generate_doc_image(prompt, str(img.get("aspect_ratio") or "16:9"),
                                           str(img.get("provider") or _IMG_PROVIDER))
                img_path = os.path.join(workdir, name)
                with open(img_path, "wb") as ifh:
                    ifh.write(data)
                image_paths.append(img_path)
                logger.info(f"doc image generated: {name} ({len(data)} bytes)")
            except Exception as exc:
                logger.warning(f"doc image generation failed (non-fatal): {exc}")

        # The container runs as uid 10001 (USER sandbox). The host worker
        # often runs under a restrictive umask (e.g. 0o077) so the script
        # and image files end up mode 0o600 — owner-only — and the sandbox
        # user gets EACCES when opening them. Make every file the build
        # script needs world-readable, and make the workdir world-rwx so
        # the build can also write output.* / page-*.jpg back to it.
        try:
            os.chmod(workdir, 0o777)
            os.chmod(src_path, 0o644)
            for p in image_paths:
                try: os.chmod(p, 0o644)
                except Exception: pass
        except Exception as exc:
            logger.warning(f"doc_executor: chmod workdir/files failed (continuing): {exc}")

        cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", "1g", "--cpus", "1",
            "--pids-limit", "256",
            "--read-only", "--tmpfs", "/tmp",
            "-v", f"{workdir}:/work",
            IMAGE, "sh", "-c", run_script,
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=BUILD_TIMEOUT_S, text=True)
        except subprocess.TimeoutExpired:
            return DocBuildResult(ok=False, error=f"Document build timed out after {BUILD_TIMEOUT_S}s.")

        logs = (proc.stdout or "") + (proc.stderr or "")
        deliver_path = os.path.join(workdir, deliver_file)

        # Robustness: the build script is SUPPOSED to write the wrapper's default
        # output path (/work/output.<ext>), but a model that deviates (e.g. after a
        # repair round) sometimes saves under a custom filename like
        # "My Report.xlsx". The code ran fine and the deliverable exists — it's just
        # named differently — so rather than fail the whole job, fall back to the
        # newest file in the workdir with the deliverable extension. Only applies
        # when the process itself succeeded (rc==0); a real crash still fails.
        if proc.returncode == 0 and not os.path.exists(deliver_path):
            _ext = spec["deliver"]
            _candidates = [
                os.path.join(workdir, n) for n in os.listdir(workdir)
                if n.lower().endswith(f".{_ext}") and n != spec["src"]
            ]
            if _candidates:
                _newest = max(_candidates, key=os.path.getmtime)
                logger.warning(
                    f"doc_executor: expected {deliver_file} not found; using "
                    f"model-named output {os.path.basename(_newest)!r} instead"
                )
                deliver_path = _newest

        if proc.returncode != 0 or not os.path.exists(deliver_path):
            return DocBuildResult(ok=False, error=_summarize_error(logs), logs=logs[-4000:], ext=spec["deliver"])

        with open(deliver_path, "rb") as fh:
            doc_bytes = fh.read()

        pdf_bytes = b""
        pdf_path = os.path.join(workdir, deliver_file if spec["deliver"] == "pdf" else "output.pdf")
        if os.path.exists(pdf_path):
            with open(pdf_path, "rb") as fh:
                pdf_bytes = fh.read()

        page_images: list[bytes] = []
        for name in sorted(os.listdir(workdir)):
            if name.startswith("page") and name.endswith(".jpg"):
                with open(os.path.join(workdir, name), "rb") as fh:
                    page_images.append(fh.read())
                if len(page_images) >= MAX_PREVIEW_PAGES:
                    break

        return DocBuildResult(
            ok=True, ext=spec["deliver"], doc_bytes=doc_bytes, pdf_bytes=pdf_bytes,
            page_images=page_images, logs=logs[-2000:],
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# Back-compat: the docx-only entry point used by the first cut.
def build_docx(code: str) -> DocBuildResult:
    return build(code, "docx")


def _summarize_error(logs: str) -> str:
    """Pull the most useful line out of node/python/soffice stderr for the agent."""
    lines = [l.strip() for l in (logs or "").splitlines() if l.strip()]
    for l in reversed(lines):
        if any(k in l for k in ("Error", "error", "Cannot", "Traceback", "Exception", "throw")):
            return l[:300]
    return (lines[-1][:300] if lines else "Document build failed (no output produced).")
