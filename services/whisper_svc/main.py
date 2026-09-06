# SPDX-License-Identifier: MIT
# ============================================================
# WHISPER STT SERVICE — FastAPI microservice (default port 8006)
#
# Speech-to-text for Voice Mode, kept OUT of the uvicorn gateway process
# (the no-lazy-load-ML-in-uvicorn rule — see embed_svc / privacy_svc). The
# gateway's POST /voice/stt proxies here when WHISPER_SVC_URL is set.
#
#   POST /transcribe   (multipart: file=<audio>) → { "text": "...", "language": "..." }
#   GET  /health       → { status, model, device }
#
# Model is loaded at module IMPORT time on CPU (faster-whisper / CTranslate2),
# never lazily, never on MPS/GPU-by-default. Air-gap: set WHISPER_MODEL to a
# local model dir or a size name whose weights are already cached on the box.
#
# Start (only when STT is wanted — not part of the default stack):
#   uvicorn services.whisper_svc.main:app --host 0.0.0.0 --port 8006 --workers 1
# ============================================================

import os
import sys

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, File, HTTPException, UploadFile

# Model size or a local directory of pre-downloaded weights (air-gap).
_MODEL_NAME = os.getenv("WHISPER_MODEL", "base")
# CPU only — mirrors embed_svc. Never default to GPU/MPS in this process.
_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")  # int8 = small footprint on CPU

_model = None
_load_error: str | None = None

try:
    # faster-whisper (CTranslate2) — fast, CPU-friendly.
    from faster_whisper import WhisperModel  # type: ignore

    _model = WhisperModel(_MODEL_NAME, device=_DEVICE, compute_type=_COMPUTE)
    print(f"[whisper_svc] loaded faster-whisper '{_MODEL_NAME}' device={_DEVICE} compute={_COMPUTE}",
          file=sys.stderr)
except Exception as e:  # noqa: BLE001
    _load_error = str(e)
    print(f"[whisper_svc] model load failed: {e} — service will 503 until deps/model present",
          file=sys.stderr)

app = FastAPI(title="AiNxt Whisper STT", version="1.0.0")


@app.get("/health")
def health():
    return {
        "status": "ok" if _model is not None else "degraded",
        "model": _MODEL_NAME,
        "device": _DEVICE,
        "compute_type": _COMPUTE,
        "load_error": _load_error,
    }


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    if _model is None:
        raise HTTPException(status_code=503, detail=f"whisper model unavailable: {_load_error}")
    audio = await file.read()
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio")

    # faster-whisper accepts a file path or a binary stream; use a temp file for
    # broad container-format support (webm/ogg/wav/m4a) via the bundled ffmpeg.
    import tempfile

    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        segments, info = _model.transcribe(tmp_path, beam_size=1)
        text = "".join(seg.text for seg in segments).strip()
        return {"text": text, "language": getattr(info, "language", "")}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"transcription failed: {e}")
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
