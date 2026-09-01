#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# ============================================================
# COMPLIANCE SCAN ROUTER — desktop browser / computer-use PII filter
#
# NOTE ON FILE PATH:
#   The task requested routers/compliance_router.py, but that path is ALREADY
#   occupied by an existing, in-use router (POST /compliance/batch,
#   GET /compliance/runs/{id}/report, /verify, /export/audit). Overwriting it
#   would destroy live endpoints, which violates the "do NOT edit shared files"
#   rule. This new endpoint therefore lives in its own file with its OWN
#   APIRouter (same prefix="/compliance"). FastAPI allows multiple routers to
#   share a prefix; the orchestrator must include BOTH. See integration steps.
#
# Endpoint:
#   POST /compliance/scan  — redact (or check) extracted text + screenshots
#                            BEFORE they enter agent context.
#
# Purpose:
#   The desktop "computer use" / browser-use path extracts text and screenshots
#   from the user's screen. That content can contain PCI/PII (PANs, Aadhaar,
#   secrets, account numbers, emails). This router redacts it via the canonical
#   ComplianceEngine before it is fed into the model's context window.
#
# AiNxt guardrail: this is a READ path (content flowing INTO context) → we REDACT
#   and PROCEED, never hard-block the user. Detected types are surfaced in
#   `types` for caller awareness, but `blocked` stays False for text on the
#   redact path. Outbound writes/sends are gated elsewhere
#   (POST /connectors/action, workers/doc_worker.py) — never here.
#
# Image scanning is BEST-EFFORT and currently NOT available: there is no OCR /
#   vision PII path wired in. We return an explicit flag rather than silently
#   passing an unscanned screenshot into context.
# ============================================================

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.dependencies import get_current_user
from core.logger import logger
from agents.compliance_engine import compliance_engine

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ── Screenshot redaction (computer-use) ───────────────────────────────────────
class ScanImageRequest(BaseModel):
    image_b64: str


@router.post("/scan-image")
async def scan_image(body: ScanImageRequest, current_user: dict = Depends(get_current_user)):
    """Redact PAN/PII from a screenshot before it reaches the model (computer-use).

    OCRs the image (pytesseract), runs the compliance engine over the recognized
    text, and draws opaque boxes over the words that overlap a finding. Returns
    {ok, image_b64, findings}. If OCR is not available, returns {ok: false} so the
    caller MUST NOT surface the raw screenshot (PCI-safe fail-closed).
    """
    import base64
    from io import BytesIO
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return {"ok": False, "reason": "Pillow not available"}
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return {"ok": False, "reason": "image OCR not configured (install pytesseract + tesseract)"}

    try:
        raw = base64.b64decode(body.image_b64)
        img = Image.open(BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, detail="invalid image")

    try:
        from agents.compliance_engine import compliance_engine
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        draw = ImageDraw.Draw(img)
        findings = 0
        n = len(data.get("text", []))
        for i in range(n):
            word = (data["text"][i] or "").strip()
            if not word:
                continue
            chk = compliance_engine.validate_input(word)
            hit = chk.get("blocked") or bool(chk.get("findings"))
            if hit:
                x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
                draw.rectangle([x, y, x + w, y + h], fill="black")
                findings += 1
        out = BytesIO()
        img.save(out, format="PNG")
        return {"ok": True, "image_b64": base64.b64encode(out.getvalue()).decode(), "findings": findings}
    except Exception as exc:
        logger.warning(f"scan-image failed → {exc}")
        return {"ok": False, "reason": str(exc)}


# ── Schemas ───────────────────────────────────────────────────────────────────
class ScanRequest(BaseModel):
    text:      Optional[str] = None
    image_b64: Optional[str] = None
    mode:      str = "redact"  # "redact" | "check"


class ScanResponse(BaseModel):
    redacted_text:        Optional[str] = None
    blocked:              bool = False
    types:                List[str] = []
    was_redacted:         bool = False
    image_scanned:        bool = False
    image_scan_available: bool = False
    note:                 Optional[str] = None


# ── Endpoint ──────────────────────────────────────────────────────────────────
@router.post("/scan", response_model=ScanResponse)
async def scan(
    body: ScanRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    Redact (or check) extracted text + screenshots from the desktop
    computer-use / browser filter before they enter agent context.

    mode="redact" (default): return redacted text + the set of detected types.
    mode="check":            run detection only; do not return a redacted body —
                             just report whether anything sensitive was found.

    READ path → REDACT and PROCEED. We never block the user here.
    """
    mode = (body.mode or "redact").strip().lower()
    if mode not in ("redact", "check"):
        raise HTTPException(400, detail="mode must be 'redact' or 'check'")

    if not body.text and not body.image_b64:
        raise HTTPException(400, detail="Provide at least one of: text, image_b64")

    resp = ScanResponse(
        blocked=False,
        types=[],
        was_redacted=False,
        image_scanned=False,
        image_scan_available=False,
    )

    detected: List[str] = []

    # ── Text path ─────────────────────────────────────────────────────────────
    if body.text:
        # validate_input runs the full regex + ML analysis and produces a redacted
        # body. It is the authoritative read-path entry point. We use its findings
        # to report detected types, but we DO NOT hard-block on this read path.
        try:
            result = compliance_engine.validate_input(body.text)
        except Exception as exc:
            logger.error(f"Compliance scan: validate_input failed: {exc}")
            raise HTTPException(500, detail="Compliance scan failed")

        # Detected types = union of redacted types and any block-configured hits.
        for t in (result.get("redacted_types") or []):
            if t not in detected:
                detected.append(t)
        for t in (result.get("blocked_types") or []):
            if t not in detected:
                detected.append(t)
        # Also surface analysis findings not already captured (e.g. types
        # configured "off" that still matched), so the caller sees the full picture.
        for f in (result.get("findings") or []):
            ft = f.get("type")
            if ft and ft not in detected:
                detected.append(ft)

        resp.was_redacted = bool(result.get("was_redacted"))

        if mode == "redact":
            # Prefer the engine's redacted body; fall back to the redact_text helper.
            redacted = result.get("redacted_text")
            if redacted is None:
                redacted, rtypes = compliance_engine.redact_text(body.text)
                for t in (rtypes or []):
                    if t not in detected:
                        detected.append(t)
            resp.redacted_text = redacted
        # mode == "check": leave redacted_text as None — caller only wants the verdict.

        # Never log raw extracted content; counts + types only.
        if detected:
            logger.info(
                f"Compliance scan (text): mode={mode} types={detected} "
                f"user={current_user.get('email')}"
            )

    # ── Image path (best-effort, currently unavailable) ───────────────────────
    if body.image_b64:
        # There is no OCR / vision PII path wired in yet. We must NOT silently
        # pass an unscanned screenshot into agent context — return an explicit
        # flag so the desktop caller can decide (e.g. warn the user / withhold
        # the screenshot from context).
        resp.image_scan_available = False
        resp.image_scanned = False
        note = (
            "image scanning not yet available — screenshot was NOT scanned for "
            "PII/PCI; do not feed it into context without a vision PII path"
        )
        resp.note = (resp.note + "; " + note) if resp.note else note
        logger.warning(
            "Compliance scan: image submitted but image PII scanning unavailable "
            f"user={current_user.get('email')}"
        )

    # Dedupe while preserving order.
    resp.types = list(dict.fromkeys(detected))
    return resp
