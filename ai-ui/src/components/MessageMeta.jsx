// SPDX-License-Identifier: Apache-2.0
/**
 * MessageMeta — standardised per-message stats chips.
 *
 * Used by Chat.jsx, Threads.jsx, Projects.jsx.
 *
 * Props
 * ─────
 *   msg        {object}  — message record with stats fields
 *   budget     {object}  — /budget/me response  (may be null)
 *   isLast     {bool}    — show budget chip only on the last assistant message
 *
 * msg fields consumed:
 *   modelLabel, inTok, outTok, tokenUsage, costUsd, latency,
 *   tokensToday, maxTokensToday, requestsToday, maxRequestsToday
 *
 * budget fields consumed:
 *   monthly_spend, monthly_limit, monthly_remaining
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Cpu, Clock, DollarSign, BarChart2, Zap, Wallet, TrendingDown, Target } from "lucide-react";

// ── Individual chips ──────────────────────────────────────────────────────────

function Chip({ className, children }) {
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs border ${className}`}>
      {children}
    </span>
  );
}

// ── Model chip ────────────────────────────────────────────────────────────────
function ModelChip({ label }) {
  if (!label) return null;
  if (label === "cached") {
    return (
      <Chip className="bg-sky-50 text-sky-600 border-sky-100 font-medium">
        <Zap size={10} /> Cached
      </Chip>
    );
  }
  return (
    <Chip className="bg-violet-50 text-violet-700 border-violet-100 font-medium" title={label}>
      <Cpu size={10} />
      <span className="max-w-[36ch] truncate">{label}</span>
    </Chip>
  );
}

// ── Token chip ────────────────────────────────────────────────────────────────
function TokenChip({ inTok, outTok, tokenUsage }) {
  if (inTok != null && outTok != null) {
    return (
      <Chip className="bg-blue-50 text-blue-700 border-blue-100">
        <span className="text-blue-400">↑</span>{inTok.toLocaleString()}
        <span className="text-blue-300 mx-0.5">·</span>
        <span className="text-blue-400">↓</span>{outTok.toLocaleString()}
        <span className="text-blue-300 ml-0.5">tok</span>
      </Chip>
    );
  }
  if (tokenUsage != null) {
    return (
      <Chip className="bg-blue-50 text-blue-700 border-blue-100">
        {tokenUsage.toLocaleString()} tok
      </Chip>
    );
  }
  return null;
}

// ── Cost chip (this message) ──────────────────────────────────────────────────
function CostChip({ costUsd }) {
  // Hide only when there is no cost data at all (null/undefined).
  // costUsd === 0 means a local/in-house model (free) — show "$0.00" so the
  // user can see the cost field is present and the model is free.
  if (costUsd == null) return null;
  return (
    <Chip className="bg-emerald-50 text-emerald-700 border-emerald-100 font-medium" title="Cost for this message">
      <DollarSign size={10} />
      {costUsd === 0 ? `$0.00` : costUsd < 0.01 ? `<$0.01` : `$${costUsd.toFixed(2)}`}
    </Chip>
  );
}

// ── Latency chip ──────────────────────────────────────────────────────────────
function LatencyChip({ latency }) {
  if (latency == null || latency <= 0) return null;
  return (
    <Chip className="bg-orange-50 text-orange-600 border-orange-100">
      <Clock size={10} />
      {latency.toFixed(1)}s
    </Chip>
  );
}

// ── Daily usage chip ──────────────────────────────────────────────────────────
function DailyUsageChip({ tokensToday, maxTokensToday, requestsToday, maxRequestsToday }) {
  if (tokensToday == null && requestsToday == null) return null;
  const nearLimit = maxTokensToday > 0 && tokensToday > maxTokensToday * 0.9;
  return (
    <>
      {tokensToday != null && (
        <Chip className={nearLimit
          ? "bg-red-50 text-red-600 border-red-100"
          : "bg-gray-50 text-gray-500 border-gray-100"}
          title="Tokens used today"
        >
          <BarChart2 size={10} />
          {tokensToday.toLocaleString()}
          {maxTokensToday > 0 ? `/${maxTokensToday.toLocaleString()}` : ""} tok/day
        </Chip>
      )}
      {requestsToday != null && (
        <Chip className="bg-gray-50 text-gray-500 border-gray-100" title="Requests today">
          {requestsToday}{maxRequestsToday ? `/${maxRequestsToday}` : ""} req
        </Chip>
      )}
    </>
  );
}

// ── Monthly budget chip ───────────────────────────────────────────────────────
function MonthlyBudgetChip({ budget }) {
  if (!budget) return null;
  const { monthly_spend, monthly_limit, monthly_remaining } = budget;
  if (monthly_spend == null) return null;

  const pct = monthly_limit > 0 ? monthly_spend / monthly_limit : 0;
  const isUnlimited = !monthly_limit || monthly_limit <= 0;

  // Color thresholds
  const chipClass = isUnlimited
    ? "bg-gray-50 text-gray-500 border-gray-100"
    : pct >= 0.9
      ? "bg-red-50 text-red-600 border-red-200"
      : pct >= 0.7
        ? "bg-amber-50 text-amber-600 border-amber-200"
        : "bg-emerald-50 text-emerald-700 border-emerald-200";

  const barClass = pct >= 0.9 ? "bg-red-400" : pct >= 0.7 ? "bg-amber-400" : "bg-emerald-400";

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs border font-medium ${chipClass}`}
      title={isUnlimited ? "Monthly spend (no limit set)" : `$${monthly_spend.toFixed(2)} used of $${monthly_limit.toFixed(2)} monthly budget`}
    >
      <Wallet size={10} />
      <span className="opacity-60 font-normal">Budget</span>

      {/* Mini progress bar (only when limit is set) */}
      {!isUnlimited && (
        <span className="inline-flex items-center gap-1">
          <span className="w-12 h-1.5 bg-gray-200 rounded-full overflow-hidden">
            <span
              className={`h-full rounded-full ${barClass} block`}
              style={{ width: `${Math.min(pct * 100, 100).toFixed(1)}%` }}
            />
          </span>
        </span>
      )}

      <span>
        ${monthly_spend.toFixed(2)}
        {!isUnlimited && ` / $${monthly_limit.toFixed(2)}`}
      </span>

      {/* Remaining */}
      {monthly_remaining != null && !isUnlimited && (
        <>
          <span className="opacity-40">·</span>
          <TrendingDown size={10} />
          <span className="opacity-80">${monthly_remaining.toFixed(2)} left</span>
        </>
      )}
    </span>
  );
}

function CoachHitsChip({ hits }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);
  const closeTimerRef = useRef(null);
  const chipRef = useRef(null);
  const popoverRef = useRef(null);

  // After opening, measure the popover height and flip it above/below the chip
  // so it always stays in the viewport and never gets clipped by overflow-hidden
  // ancestors (the popover is rendered via portal on document.body).
  useEffect(() => {
    if (!open || !pos) return;
    const el = popoverRef.current;
    if (!el) return;
    const height = el.getBoundingClientRect().height;
    const margin = 8;
    let top = pos.chipTop - height - margin;
    if (top < margin) {
      top = pos.chipBottom + margin;
    }
    setPos(p => ({ ...p, top }));
  }, [open, pos?.left, pos?.width]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!Array.isArray(hits) || hits.length === 0) return null;

  const severityRank = { critical: 4, high: 3, medium: 2, low: 1 };
  const maxSeverity = hits.reduce((max, hit) => {
    const sev = String(hit.severity || "low").toLowerCase();
    return (severityRank[sev] || 1) > (severityRank[max] || 1) ? sev : max;
  }, "low");

  const tone = maxSeverity === "critical" || maxSeverity === "high"
    ? "bg-red-50 text-red-700 border-red-200"
    : maxSeverity === "medium"
      ? "bg-amber-50 text-amber-700 border-amber-200"
      : "bg-indigo-50 text-indigo-700 border-indigo-100";

  const computePos = () => {
    const rect = chipRef.current?.getBoundingClientRect();
    if (!rect) return null;
    const pad = 12;
    const width = Math.min(420, window.innerWidth - 32);
    let left = rect.left + rect.width / 2 - width / 2;
    left = Math.max(pad, Math.min(left, window.innerWidth - width - pad));
    let top = rect.top - 8; // will be adjusted after measuring in effect
    return { left, top, width, chipTop: rect.top, chipBottom: rect.bottom };
  };

  const openPopover = () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    setPos(computePos());
    setOpen(true);
  };

  const closePopover = () => {
    if (closeTimerRef.current) clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(() => setOpen(false), 120);
  };

  return (
    <div
      className="relative inline-flex"
      onMouseEnter={openPopover}
      onMouseLeave={closePopover}
    >
      <button
        ref={chipRef}
        type="button"
        onFocus={openPopover}
        onBlur={closePopover}
        aria-label={`AiNxt Coach rule hits (${hits.length})`}
        className={`cursor-pointer inline-flex items-center px-2 py-0.5 rounded-full text-xs border font-medium ${tone}`}
      >
        <Target size={10} />
      </button>

      {open && pos && createPortal(
        <div
          ref={popoverRef}
          style={{ left: pos.left, top: pos.top, width: pos.width }}
          className="fixed z-[100] rounded-xl border border-slate-200 bg-white shadow-xl p-3 text-xs text-slate-700 pointer-events-auto"
          onMouseEnter={openPopover}
          onMouseLeave={closePopover}
        >
          <div className="flex items-center gap-2 font-semibold text-slate-900 mb-2">
            <Target size={13} className="text-indigo-600" />
            AiNxt Coach rule hits
          </div>
          <div className="space-y-2 max-h-[min(24rem,70vh)] overflow-auto">
            {hits.map((hit, idx) => (
              <div key={`${hit.id || hit.code || idx}`} className="rounded-lg border border-slate-100 bg-slate-50 p-2">
                <div className="flex items-start justify-between gap-2">
                  <div className="font-semibold text-slate-800">{hit.code || hit.id}</div>
                  <span className="shrink-0 rounded-full bg-white border border-slate-200 px-1.5 py-0.5 text-[10px] uppercase text-slate-500">
                    {hit.severity || "low"}
                  </span>
                </div>
                <div className="mt-1 text-slate-700 break-words">{hit.name || hit.id}</div>
                {hit.advice && (
                  <div className="mt-1.5 text-[11px] text-slate-600 leading-snug border-l-2 border-indigo-200 pl-2 break-words">
                    {hit.advice}
                  </div>
                )}
                <div className="mt-1 text-[10px] uppercase tracking-wide text-slate-400">{hit.category || "general"}</div>
              </div>
            ))}
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────────

export default function MessageMeta({ msg, budget, isLast = false }) {
  const hasCoachHits = Array.isArray(msg.coachHits) && msg.coachHits.length > 0;
  const hasAnyMeta = (
    msg.modelLabel || msg.inTok != null || msg.tokenUsage != null ||
    msg.costUsd    || msg.latency != null || hasCoachHits
  );
  if (!hasAnyMeta || msg.streaming || msg.role !== "assistant") return null;

  return (
    <div className="mt-3 pt-2 flex flex-wrap gap-1.5 items-center">
      <ModelChip label={msg.modelLabel} />
      <TokenChip inTok={msg.inTok} outTok={msg.outTok} tokenUsage={msg.tokenUsage} />
      <CostChip costUsd={msg.costUsd} />
      <LatencyChip latency={msg.latency} />
      <CoachHitsChip hits={msg.coachHits} />
      {/* DailyUsageChip hidden — tok/day & req counts shown in Analytics Dashboard instead */}
      {/* Budget chip: only on the last message to avoid repetition */}
      {isLast && <MonthlyBudgetChip budget={budget} />}
    </div>
  );
}
