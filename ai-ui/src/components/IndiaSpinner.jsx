// SPDX-License-Identifier: MIT
/**
 * IndiaSpinner — Spinning tricolor ring with static Ashoka Chakra inside.
 * Uses Tailwind animate-spin on a conic-gradient div — guaranteed to work.
 */

const NAVY = "#000080";

function Chakra({ size }) {
  const R    = size / 2;
  const rimR = R * 0.82;
  const hubR = R * 0.16;

  const spokes = Array.from({ length: 24 }, (_, i) => {
    const angle = ((i * 15 - 90) * Math.PI) / 180;
    return {
      x1: R + Math.cos(angle) * (hubR + 1),
      y1: R + Math.sin(angle) * (hubR + 1),
      x2: R + Math.cos(angle) * (rimR - 1),
      y2: R + Math.sin(angle) * (rimR - 1),
    };
  });

  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={R} cy={R} r={rimR} fill="none" stroke={NAVY} strokeWidth={1.5} />
      {spokes.map((s, i) => (
        <line key={i} x1={s.x1} y1={s.y1} x2={s.x2} y2={s.y2}
          stroke={NAVY} strokeWidth={1} />
      ))}
      <circle cx={R} cy={R} r={hubR} fill={NAVY} />
    </svg>
  );
}

export default function IndiaSpinner({ size = 36, label = "Thinking…" }) {
  const inner = size - 4;   // white inner circle leaves 4px ring on each side

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 10 }}>
      {/* Spinning tricolor ring — conic-gradient + Tailwind animate-spin */}
      <div
        className="animate-spin"
        style={{
          width:        size,
          height:       size,
          borderRadius: "50%",
          background:   "conic-gradient(#FF9933 0% 33%, #f3f4f6 33% 66%, #138808 66% 100%)",
          display:      "flex",
          alignItems:   "center",
          justifyContent: "center",
          flexShrink:   0,
        }}
      >
        {/* White inner circle — holds the static chakra */}
        <div style={{
          width:        inner,
          height:       inner,
          borderRadius: "50%",
          background:   "#ffffff",
          display:      "flex",
          alignItems:   "center",
          justifyContent: "center",
        }}>
          <Chakra size={inner * 0.65} />
        </div>
      </div>

      {label && (
        <span style={{ fontSize: 13, color: "#6b7280", fontStyle: "italic" }}>{label}</span>
      )}
    </span>
  );
}
