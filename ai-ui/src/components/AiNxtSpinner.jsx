// SPDX-License-Identifier: Apache-2.0
import { useEffect, useRef, useState } from "react";
import BrandMark from "./BrandMark";

// Claude-code-style live status line, branded with the AiNxt logo mark:
//
//     ◈  Thinking… (12s · out 188t)
//
// The AiNxt logo mark (gently "breathing") + a status word + a live-ticking
// elapsed timer + (optionally) a running output-token count. The timer starts
// the moment this component mounts — i.e. as soon as the status line appears —
// and updates every second until it unmounts.
//
// TRUTHFULNESS: there is NO fake auto-advancing timeline. The label is only
// ever one of:
//   1. `label`            — a live backend status string (obj.status). Wins.
//   2. `steps[stage]`     — an EXPLICIT phase label the caller knows is real
//                           (e.g. video: "Rendering frames" driven by a real
//                           backend stage). Only used when the caller opts in.
//   3. "Working"          — neutral fallback when the caller has no signal.
// The component never invents "Understanding → Searching → …" progress the
// backend never reported.
//
// Props:
//   steps    — OPTIONAL [{id,label}] explicit phase labels. Only meaningful
//              alongside `stage`. No default list (no fabricated phases).
//   stage    — integer index into `steps` (from a real backend stage), or null.
//   label    — live backend status string (obj.status), takes precedence.
//   outTok   — running output-token count (optional); shown as "· out Nt".
//   startAt  — optional epoch ms to anchor the timer to (defaults to mount time)
//              so the elapsed clock survives label/stage re-renders.

export default function AiNxtSpinner({
  steps = null,
  stage = null,
  label = null,
  outTok = null,
  startAt = null,
}) {
  // Anchor the clock to startAt (the original streamStartAt epoch ms) so that
  // when the spinner unmounts and remounts (e.g. the user switches KB chats
  // and comes back), the elapsed counter resumes from the correct value
  // instead of flashing back to 0.
  const startRef = useRef(startAt || Date.now());

  // Initialise elapsed directly from startAt so the very first render already
  // shows the correct elapsed time — no (0) flash before the effect fires.
  const [elapsed, setElapsed] = useState(() =>
    Math.floor((Date.now() - (startAt || startRef.current)) / 1000)
  );

  // Elapsed-time clock — starts on mount, ticks every second.
  useEffect(() => {
    // Re-anchor if startAt changes (e.g. a new request reuses the component).
    if (startAt) startRef.current = startAt;
    const tick = () => setElapsed(Math.floor((Date.now() - startRef.current) / 1000));
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [startAt]);

  // Resolve the label truthfully — no fabricated progression.
  let raw = "Working";
  if (label) {
    raw = label;                                   // live backend status
  } else if (steps && stage != null) {
    const idx = Math.max(0, Math.min(steps.length - 1, stage));
    raw = steps[idx]?.label || "Working";          // explicit real phase
  }
  // Backend strings may carry a trailing ellipsis; normalise to avoid "……".
  const displayLabel = raw.replace(/[.…]+$/, "");

  // Compact meta: "(12s · out 188t)". Token part only when we have a count.
  const metaParts = [`${elapsed}s`];
  if (outTok != null && outTok > 0) metaParts.push(`out ${outTok}t`);
  const meta = `(${metaParts.join(" · ")})`;

  return (
    <div className="flex items-center gap-2 text-xs text-gray-500 my-1">
      <BrandMark className="w-4 h-4 brand-breathe shrink-0" />
      <span>{displayLabel}…</span>
      <span className="text-gray-400 tabular-nums">{meta}</span>
    </div>
  );
}
