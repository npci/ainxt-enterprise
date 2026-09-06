// SPDX-License-Identifier: MIT
/* Shared red/green diff body. Used by the chat diff cards (Code.jsx Diff /
   ToolDiff) and by the lite-IDE editor panel's Diff view, so both render an
   agent's change identically. `lines` = [{kind:"+"|"-"|"@@"|" ", line}]. */
export default function DiffLines({ lines = [], truncated = 0, className = "" }) {
  return (
    <pre className={`text-xs font-mono overflow-x-auto bg-white m-0 leading-5 ${className}`}>
      {lines.map((l, i) => (
        <div key={i} className={
          l.kind === "+" ? "bg-emerald-50 text-emerald-800" :
          l.kind === "-" ? "bg-red-50 text-red-800" :
          l.kind === "@@" ? "bg-indigo-50 text-indigo-700" : "text-gray-600"
        }>
          <span className="px-2 inline-block w-full whitespace-pre">{l.kind === "@@" ? "⋯" : l.kind}{l.line}</span>
        </div>
      ))}
      {truncated > 0 && (
        <div className="text-gray-400"><span className="px-2 inline-block">… {truncated} more line{truncated !== 1 ? "s" : ""}</span></div>
      )}
    </pre>
  );
}
