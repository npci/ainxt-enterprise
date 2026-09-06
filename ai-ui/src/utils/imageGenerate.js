// SPDX-License-Identifier: MIT
import { MODEL_IMAGE } from "../config";
// Shared helpers for /chat/image-generate calls. Used by:
//   - Chat.jsx handleImageGenerate (toolbar shortcut)
//   - Chat.jsx sendMessage image-intent branch
//
// Image generation has exactly ONE backing model: gemini-3.1-flash-image.
// That's the only image-capable model the platform exposes. Claude /
// OpenAI / Local models do NOT have an image-gen endpoint here — when the
// user picks one of those for an image-intent prompt, the caller routes
// the prompt through the normal /ask chat path so the chosen model gives
// its own text response (typically a refusal). This util is therefore
// ONLY called when we actually want to invoke the gemini image model.

export const IMAGE_GEN_ENDPOINT   = "/chat/image-generate";
export const IMAGE_DEFAULT_RATIO  = "16:9";
export const IMAGE_ARTIFACT_TITLE = "Generated image";

export const blobToDataUrl = (blob) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onloadend = () => resolve(r.result);
  r.onerror   = () => reject(new Error("read failed"));
  r.readAsDataURL(blob);
});

// Parse a numeric header value (returns null when missing or NaN).
const _hNum = (resp, key) => {
  const raw = resp.headers.get(key);
  if (raw == null || raw === "") return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
};

// POST /chat/image-generate, decode the response, return
// {md, artifacts, modelLabel, costUsd, inTok, outTok, tokenUsage, latencySec}.
// All cost/token/latency fields are populated from response headers when the
// backend supplies them (X-Cost-USD / X-Input-Tokens / X-Output-Tokens /
// X-Token-Usage / X-Latency-Sec); they may be null for older deploys.
// Throws on non-2xx with the server text (truncated by caller if needed).
export async function generateImage({
  api,
  authFetch,
  prompt,
  chatId,
  messageId,
  aspectRatio = IMAGE_DEFAULT_RATIO,
  // Original uploaded-image attachment_ids forwarded from the /ask routing
  // response. Stored on the user ChatMessage row in Postgres so the L2-img
  // history-inject block in gateway.py can find the image caption on
  // follow-up turns ("explain the image I attached").
  attachmentIds = [],
  // The user's original question before backend enrichment (e.g. "improve
  // this image"). When present, /chat/image-generate stores this as the
  // user message content instead of the long enriched prompt so that chat
  // history shows the original phrasing after a page reload.
  // Empty string for toolbar-shortcut calls (no enrichment happens there).
  originalQuestion = "",
}) {
  const resp = await authFetch(`${api}${IMAGE_GEN_ENDPOINT}`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({
      prompt,
      chat_id:        chatId,
      aspect_ratio:   aspectRatio,
      message_id:     messageId,
      // Only include when non-empty to keep the payload lean for the
      // common case (toolbar shortcut / no uploaded image).
      ...(attachmentIds && attachmentIds.length > 0
        ? { attachment_ids: attachmentIds }
        : {}),
      // Only include when set (routed image-intent turns). Absent for
      // toolbar-shortcut calls where prompt IS the original question.
      ...(originalQuestion
        ? { original_question: originalQuestion }
        : {}),
    }),
  });
  if (!resp.ok) {
    const errText = await resp.text().catch(() => "");
    // Try to pull the friendly "detail" field FastAPI puts on
    // HTTPException payloads so we can show the readable message
    // ("Image generation model not available…") instead of the raw
    // JSON envelope.
    let detail = errText;
    try {
      const j = JSON.parse(errText);
      if (j && typeof j.detail === "string") detail = j.detail;
    } catch { /* not JSON — keep raw text */ }
    const e = new Error(detail || `Image generation failed (${resp.status})`);
    // 503 = both providers unavailable. The caller checks this flag to
    // render the message as a normal chat reply rather than an "Error:"
    // prefixed failure.
    e.status      = resp.status;
    e.unavailable = (resp.status === 503);
    throw e;
  }
  const artifactId = resp.headers.get("X-Artifact-Id") || null;
  // Backend always sets X-Model-Label to the actual gemini image model id
  // (e.g. "gemini-3.1-flash-image"). Fall back to a sensible default if
  // missing so the chip never renders empty.
  const modelLabel = resp.headers.get("X-Model-Label") || MODEL_IMAGE;
  const costUsd    = _hNum(resp, "X-Cost-USD");
  const inTok      = _hNum(resp, "X-Input-Tokens");
  const outTok     = _hNum(resp, "X-Output-Tokens");
  const tokenUsage = _hNum(resp, "X-Token-Usage");
  // Server-measured wall-clock latency (seconds) around generate_imagen().
  // Preferred over a client-side stopwatch so the live chip matches the
  // value persisted to the ChatMessage row — i.e. identical before and
  // after a page refresh. null on older deploys that don't send the header.
  const latencySec = _hNum(resp, "X-Latency-Sec");
  const dataUrl    = await blobToDataUrl(await resp.blob());
  return {
    md:        `![generated image](${dataUrl})`,
    artifacts: artifactId ? [{ id: artifactId, title: IMAGE_ARTIFACT_TITLE, type: "html" }] : [],
    modelLabel,
    costUsd,
    inTok,
    outTok,
    tokenUsage,
    latencySec,
  };
}
