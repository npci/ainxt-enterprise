// SPDX-License-Identifier: Apache-2.0
/* CoworkScheduler — the dedicated "Scheduler" panel for Buddy (desktop).
 *
 * Replaces the old thin list+create modals with a real scheduler UX:
 *   - a task table (schedule summary, next run, last-run status badge)
 *   - a detail drawer per task: edit, pause/resume, run-now, delete
 *   - a 7-day run history (success/fail strip + per-run status/output/error)
 *
 * All data already exists on the backend (routers/cowork_tasks_router.py):
 *   GET    /buddy/tasks[?project_id]        list
 *   POST   /buddy/tasks                     create {prompt,cron,role,project_id,tz}
 *   PUT    /buddy/tasks/{id}                edit {prompt?,cron?,role?,status?}
 *   DELETE /buddy/tasks/{id}                delete
 *   POST   /buddy/tasks/{id}/run-now        enqueue immediately
 *   GET    /buddy/tasks/{id}/history?limit  run history (status/output/error/created_at)
 *
 * AiNxt guardrail (unchanged): scheduled runs never send anything unattended —
 * outbound actions still require the pre-approval path.
 *
 * Email delivery: the backend worker automatically extracts the recipient from
 * the task prompt and sends via Outlook. No manual delivery configuration needed.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Clock, Plus, Pencil, Trash2, Play, Pause, X, CheckCircle2, XCircle,
  RotateCcw, Loader2, CalendarClock, MinusCircle, ChevronRight,
} from "lucide-react";
import { API_BASE, authFetch } from "../config";
import { useConfirm } from "./ui/DialogProvider";

const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const WEEKDAYS_SHORT = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHLY_ORDINALS = [
  { value: 1, label: "First" },
  { value: 2, label: "Second" },
  { value: 3, label: "Third" },
  { value: 4, label: "Fourth" },
  { value: "last", label: "Last" },
];

// ── cron helpers ─────────────────────────────────────────────────────────────
// We only build/parse the friendly subset (daily / weekly / monthly at HH:MM);
// anything else is shown/edited as a raw cron string ("custom").
function pad2(n) { return String(n).padStart(2, "0"); }

function parseCron(cron) {
  const parts = (cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return { cadence: "custom", time: "09:00", dow: 1, dom: 1 };
  const [m, h, dom, mon, dow] = parts;
  const isNum = (x) => /^\d+$/.test(x);
  if (!isNum(m) || !isNum(h)) return { cadence: "custom", time: "09:00", dow: 1, dom: 1 };
  const time = `${pad2(+h)}:${pad2(+m)}`;
  if (dom === "*" && mon === "*" && dow === "*") return { cadence: "daily", time, dow: 1, dom: 1 };
  if (dom === "*" && mon === "*" && isNum(dow))  return { cadence: "weekly", time, dow: +dow % 7, dom: 1 };
  if (isNum(dom) && mon === "*" && dow === "*")  return { cadence: "monthly", time, dow: 1, dom: +dom };
  return { cadence: "custom", time: "09:00", dow: 1, dom: 1 };
}

function buildCron({ cadence, time, dow, dom }) {
  const [h, m] = (time || "09:00").split(":").map((n) => parseInt(n, 10) || 0);
  if (cadence === "daily")   return `${m} ${h} * * *`;
  if (cadence === "weekly")  return `${m} ${h} * * ${dow}`;
  if (cadence === "monthly") return `${m} ${h} ${dom} * *`;
  return null; // custom handled separately
}

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

// Human-readable summary of a cron expression (best-effort for the friendly subset).
function cronToText(cron) {
  const p = parseCron(cron);
  if (p.cadence === "daily")   return `Every day at ${p.time}`;
  if (p.cadence === "weekly")  return `Every ${WEEKDAYS[p.dow] || "week"} at ${p.time}`;
  if (p.cadence === "monthly") return `Monthly on the ${ordinal(p.dom)} at ${p.time}`;
  return cron || "—";
}

// ── Outlook-style Recurrence ────────────────────────────────────────────────
// The RecurrenceEditor below is the source of truth for how a user schedules a
// task. It produces (a) a standard 5-field cron string that the existing
// backend scheduler already understands, and (b) a natural-language summary
// that we show in the UI. Users NEVER see the cron string — it is an
// implementation detail of the scheduling backend.

const DEFAULT_RECURRENCE = () => ({
  pattern: "daily",              // 'minutely' | 'hourly' | 'daily' | 'weekly' | 'monthly'
  interval: 1,                   // "every N …"
  time: "09:00",                 // for daily/weekly/monthly
  weekdays: [4],                 // for weekly (Thu, matching the screenshot)
  weekdayOnly: false,            // Daily → "Every weekday"
  monthDay: 1,                   // Monthly "Day N of every M months"
  monthlyMode: "day",            // 'day' | 'weekday'
  monthOrd: 1,                   // 1..4 or 'last'
  monthWeekday: 1,               // 0..6
  window: { enabled: false, start: "09:00", end: "18:00" },   // minutely/hourly only
  windowDays: { enabled: false, days: [1, 2, 3, 4, 5] },      // minutely/hourly only
  range: {
    start: toDateInput(new Date()),
    end: { mode: "by", date: "" },   // 'by' | 'after' | 'none'  — default to "End by" (date required)
  },
});

// When the user picks a new pattern from the dropdown, seed a sensible
// `interval` default so switching cadences doesn't leave stale values around
// (e.g. Weekly with interval=15 carried over from Minutely). Mirrors the
// per-radio defaults the old inline radios used to apply.
function pickPattern(next, current) {
  const patch = { pattern: next };
  if (next === "minutely") patch.interval = Math.max(10, current.interval || 10);
  else if (next === "hourly") patch.interval = current.interval || 2;
  else if (next === "daily") patch.interval = 1;
  else patch.interval = current.interval && current.interval <= 12 ? current.interval : 1;
  return patch;
}

function toDateInput(d) {
  const y = d.getFullYear(), m = pad2(d.getMonth() + 1), day = pad2(d.getDate());
  return `${y}-${m}-${day}`;
}

function fmtDateForSummary(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-").map((n) => parseInt(n, 10));
  const dt = new Date(Date.UTC(y, (m || 1) - 1, d || 1));
  return dt.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

function timeToMinutes(t) {
  const [h, m] = (t || "00:00").split(":").map((n) => parseInt(n, 10) || 0);
  return { h: h % 24, m: m % 60 };
}

// Build a 5-field cron from the recurrence state. Returns "" when the state is
// invalid (e.g. weekly with no weekdays picked) — the caller uses that to
// disable Save. See §3 of the design plan for the templates.
function buildCronFromRecurrence(r) {
  if (!r) return "";
  const { h, m } = timeToMinutes(r.time || "09:00");
  const N = Math.max(1, parseInt(r.interval, 10) || 1);

  const winDays = (r.windowDays && r.windowDays.enabled && r.windowDays.days.length)
    ? [...r.windowDays.days].sort().join(",")
    : "*";
  const win = r.window && r.window.enabled ? r.window : null;

  switch (r.pattern) {
    case "minutely": {
      const step = Math.min(59, Math.max(10, N));
      if (win) {
        const { h: sh } = timeToMinutes(win.start);
        const { h: eh, m: em } = timeToMinutes(win.end);
        // In cron, the hour range is inclusive of the full hour (e.g. 9-18
        // means hours 9 through 18, so the task fires at 18:00, 18:15, …).
        // If the end time is exactly on the hour (e.g. 18:00), use eh-1 as
        // the last allowed hour so the task stops before the 18th hour.
        // If the end time has minutes (e.g. 18:30), keep eh — we do want
        // fires at 18:00, 18:15, 18:30 in that case.
        const lastHour = (em === 0 && eh > sh) ? eh - 1 : eh;
        const hrange = sh === lastHour ? `${sh}` : `${sh}-${lastHour}`;
        return `*/${step} ${hrange} * * ${winDays}`;
      }
      return `*/${step} * * * ${winDays === "*" ? "*" : winDays}`;
    }
    case "hourly": {
      const step = Math.min(23, Math.max(1, N));
      if (win) {
        const { h: sh, m: sm } = timeToMinutes(win.start);
        const { h: eh, m: em } = timeToMinutes(win.end);
        // Same hour-range fix as minutely: if the end time is exactly on the
        // hour (e.g. 18:00), use eh-1 so the task doesn't fire during the
        // 18th hour. If the end has minutes (e.g. 18:30), keep eh.
        const lastHour = (em === 0 && eh > sh) ? eh - 1 : eh;
        const hrange = sh === lastHour ? `${sh}` : `${sh}-${lastHour}`;
        // Use the window start's own minute (sm) so the task fires at :sm
        // past each hour within the window — e.g. "07:00–09:00 every 1h"
        // → "0 7-8/1 * * *"; "07:30–09:30 every 1h" → "30 7-9/1 * * *".
        return `${sm} ${hrange}/${step} * * ${winDays}`;
      }
      return `0 */${step} * * ${winDays === "*" ? "*" : winDays}`;
    }
    case "daily": {
      if (r.weekdayOnly) return `${m} ${h} * * 1-5`;
      return `${m} ${h} */${N} * *`;
    }
    case "weekly": {
      const days = (r.weekdays || []).slice().sort();
      if (days.length === 0) return "";
      // "Every N weeks" is enforced by the scheduler (interval_weeks column);
      // the cron itself just matches those weekdays every week.
      return `${m} ${h} * * ${days.join(",")}`;
    }
    case "monthly": {
      if (r.monthlyMode === "weekday") {
        // Outlook: "The [First..Fourth|Last] [Weekday] of every N months".
        // croniter accepts DOW#n (e.g. "MON#1") and DOW#L for "last".
        const dow = r.monthWeekday;
        const suffix = r.monthOrd === "last" ? "L" : String(r.monthOrd);
        // Every-N-months enforced by scheduler (interval_months).
        return `${m} ${h} * * ${dow}#${suffix}`;
      }
      const dom = Math.min(31, Math.max(1, parseInt(r.monthDay, 10) || 1));
      // Every-N-months enforced by scheduler (interval_months).
      // Days 29-31 are stored as-is in the cron; the backend scheduler
      // clamps to the last valid day of each month at fire time (e.g. day 31
      // fires on the 30th in April/June/Sep/Nov, and on the 28th/29th in Feb).
      return `${m} ${h} ${dom} */${N} *`;
    }
    default:
      return "";
  }
}

// Natural-language description of the recurrence (what the user sees).
function describeRecurrence(r) {
  if (!r) return "";
  const t = r.time || "09:00";
  const N = Math.max(1, parseInt(r.interval, 10) || 1);
  const rangeSuffix = describeRange(r.range);

  let head = "";
  switch (r.pattern) {
    case "minutely": {
      head = `Every ${N === 1 ? "minute" : `${N} minutes`}`;
      const bits = [];
      if (r.window && r.window.enabled) bits.push(`${r.window.start}–${r.window.end}`);
      if (r.windowDays && r.windowDays.enabled && r.windowDays.days.length && r.windowDays.days.length < 7)
        bits.push(r.windowDays.days.map((d) => WEEKDAYS_SHORT[d]).join(", "));
      if (bits.length) head += `, ${bits.join(" ")}`;
      break;
    }
    case "hourly": {
      head = `Every ${N === 1 ? "hour" : `${N} hours`}`;
      const bits = [];
      if (r.window && r.window.enabled) bits.push(`${r.window.start}–${r.window.end}`);
      if (r.windowDays && r.windowDays.enabled && r.windowDays.days.length && r.windowDays.days.length < 7)
        bits.push(r.windowDays.days.map((d) => WEEKDAYS_SHORT[d]).join(", "));
      if (bits.length) head += `, ${bits.join(" ")}`;
      break;
    }
    case "daily": {
      if (r.weekdayOnly) head = `Every weekday at ${t}`;
      else head = N === 1 ? `Every day at ${t}` : `Every ${N} days at ${t}`;
      break;
    }
    case "weekly": {
      const days = (r.weekdays || []).slice().sort();
      const dayList = days.map((d) => WEEKDAYS[d]).join(", ") || "—";
      head = N === 1
        ? `Every ${dayList} at ${t}`
        : `Every ${N} weeks on ${dayList} at ${t}`;
      break;
    }
    case "monthly": {
      if (r.monthlyMode === "weekday") {
        const ord = MONTHLY_ORDINALS.find((o) => o.value === r.monthOrd)?.label || "First";
        head = N === 1
          ? `The ${ord} ${WEEKDAYS[r.monthWeekday]} of every month at ${t}`
          : `The ${ord} ${WEEKDAYS[r.monthWeekday]} of every ${N} months at ${t}`;
      } else {
        head = N === 1
          ? `Day ${r.monthDay} of every month at ${t}`
          : `Day ${r.monthDay} of every ${N} months at ${t}`;
      }
      break;
    }
    default: head = "—";
  }
  return rangeSuffix ? `${head}, ${rangeSuffix}` : head;
}

function describeRange(range) {
  if (!range) return "";
  if (range.end?.mode === "by" && range.end.date) return `until ${fmtDateForSummary(range.end.date)}`;
  if (range.end?.mode === "after" && range.end.count) return `${range.end.count} occurrences`;
  return "";
}

// Try to parse a saved recurrence blob (from TaskOut.recurrence) back into the
// editor state. When the saved blob is missing (legacy row) we return null and
// the editor falls back to DEFAULT_RECURRENCE().
function parseRecurrence(raw) {
  if (!raw || typeof raw !== "object") return null;
  const d = DEFAULT_RECURRENCE();
  return {
    ...d,
    ...raw,
    range: { ...d.range, ...(raw.range || {}), end: { ...d.range.end, ...(raw.range?.end || {}) } },
    window: { ...d.window, ...(raw.window || {}) },
    windowDays: { ...d.windowDays, ...(raw.windowDays || {}) },
    weekdays: Array.isArray(raw.weekdays) ? raw.weekdays.slice() : d.weekdays,
  };
}

// Convert the range picker's date fields into ISO strings the backend can store
// in a TIMESTAMPTZ column. `start` is interpreted as the beginning of the day
// (00:00) in the user's local time, `end by` as end-of-day (23:59:59.999) so
// the whole selected day counts. We attach the local offset via new Date().
function isoStartOfDay(dateStr) {
  if (!dateStr) return null;
  const [y, m, d] = dateStr.split("-").map((n) => parseInt(n, 10));
  return new Date(y, (m || 1) - 1, d || 1, 0, 0, 0, 0).toISOString();
}
function isoEndOfDay(dateStr) {
  if (!dateStr) return null;
  const [y, m, d] = dateStr.split("-").map((n) => parseInt(n, 10));
  return new Date(y, (m || 1) - 1, d || 1, 23, 59, 59, 999).toISOString();
}

// Relative time like "in 3h", "in 2d", "5m ago" from an ISO string.
function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const diff = t - Date.now();
  const abs = Math.abs(diff);
  const mins = Math.round(abs / 60000);
  const hrs = Math.round(abs / 3600000);
  const days = Math.round(abs / 86400000);
  let s;
  if (mins < 1) s = "now";
  else if (mins < 60) s = `${mins}m`;
  else if (hrs < 24) s = `${hrs}h`;
  else s = `${days}d`;
  if (s === "now") return "now";
  return diff >= 0 ? `in ${s}` : `${s} ago`;
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// Normalise a run/last status to one of: ok | error | skipped | never.
function normStatus(s) {
  if (!s) return "never";
  if (s === "done" || s === "success" || s === "ok") return "ok";
  if (s === "error" || s === "failed") return "error";
  return "skipped"; // skipped_disabled / not_found / anything else
}

function StatusBadge({ status, className = "" }) {
  const n = normStatus(status);
  const map = {
    ok:      { cls: "bg-green-50 text-green-700 border-green-200",  Icon: CheckCircle2, label: "ok" },
    error:   { cls: "bg-red-50 text-red-700 border-red-200",        Icon: XCircle,      label: "failed" },
    skipped: { cls: "bg-amber-50 text-amber-700 border-amber-200",  Icon: MinusCircle,  label: "skipped" },
    never:   { cls: "bg-gray-50 text-gray-500 border-gray-200",     Icon: MinusCircle,  label: "never run" },
  }[n];
  const { cls, Icon, label } = map;
  return (
    <span className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded border text-[11px] font-medium ${cls} ${className}`}>
      <Icon className="w-3 h-3" /> {label}
    </span>
  );
}

export default function CoworkScheduler({
  projectId = "", projectName = "", roles = [],
  initialCreate = false, initialPrompt = "",
  onClose, onToast,
}) {
  const [tasks, setTasks] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);
  const [limits, setLimits] = useState({ max_schedulers_per_user: 5, max_runs_per_scheduler: 25 });
  const [history, setHistory] = useState(null);      // null = not loaded, [] = loaded empty
  const [histLoading, setHistLoading] = useState(false);
  const [editing, setEditing] = useState(null);      // task object being edited, or {} for new, null = closed
  const [busyId, setBusyId] = useState("");
  const [err, setErr] = useState("");
  const { confirm } = useConfirm();

  const selected = useMemo(() => tasks.find((t) => t.id === selectedId) || null, [tasks, selectedId]);

  // Refs so the background poll/focus refresh can read the latest ids without
  // re-registering effects, and so overlapping refreshes never stack up.
  const selectedIdRef = useRef(selectedId);
  useEffect(() => { selectedIdRef.current = selectedId; }, [selectedId]);
  const loadInFlightRef = useRef(false);
  const histInFlightRef = useRef(false);

  // `quiet=true` skips the loading spinner — used by background polling/focus
  // refresh so an already-open panel doesn't flicker every ~20s.
  const load = useCallback(async (quiet = false) => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    if (!quiet) setLoading(true);
    try {
      const q = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
      const r = await authFetch(`${API_BASE}/buddy/tasks${q}`);
      const d = await r.json();
      setTasks(Array.isArray(d) ? d : (d?.tasks || []));
    } catch { if (!quiet) setTasks([]); }
    finally { if (!quiet) setLoading(false); loadInFlightRef.current = false; }
  }, [projectId]);

  useEffect(() => { load(); }, [load]);

  // Fetch server-side scheduler limits once on mount so the UI reflects the
  // deployment's env-configured values (BUDDY_SCHED_MAX_PER_USER / BUDDY_SCHED_MAX_RUNS).
  useEffect(() => {
    authFetch(`${API_BASE}/buddy/tasks/limits`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => { if (data) setLimits(data); })
      .catch(() => {}); // silently fall back to defaults
  }, []);

  useEffect(() => { if (initialCreate) setEditing({ prompt: initialPrompt || "" }); }, [initialCreate, initialPrompt]);

  const loadHistory = useCallback(async (id, quiet = false) => {
    if (!id || histInFlightRef.current) return;
    histInFlightRef.current = true;
    if (!quiet) { setHistLoading(true); setHistory(null); }
    try {
      const r = await authFetch(`${API_BASE}/buddy/tasks/${id}/history?limit=200`);
      const d = await r.json();
      setHistory(Array.isArray(d) ? d : (d?.runs || d?.history || []));
    } catch { if (!quiet) setHistory([]); }
    finally { if (!quiet) setHistLoading(false); histInFlightRef.current = false; }
  }, []);

  // load history whenever the selected task changes
  useEffect(() => { if (selectedId) loadHistory(selectedId); else setHistory(null); }, [selectedId, loadHistory]);

  // ── Live updates while the panel is open ──────────────────────────────────
  // A task can fire on its own via the cron schedule (not just via "Run now"),
  // and nothing else in this component ever re-fetches after that happens —
  // so an already-open panel kept showing stale last_run/next_run until the
  // app was restarted and the component remounted. Poll quietly in the
  // background and also refresh immediately when the tab regains focus.
  useEffect(() => {
    const tick = () => {
      if (document.visibilityState === "hidden") return;
      load(true);
      if (selectedIdRef.current) loadHistory(selectedIdRef.current, true);
    };
    const iv = setInterval(tick, 20000);
    const onFocus = () => tick();
    const onVisibility = () => { if (document.visibilityState === "visible") tick(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      clearInterval(iv);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load, loadHistory]);

  const toast = (msg) => { onToast ? onToast(msg) : null; };

  const toggleStatus = async (t) => {
    const next = t.status === "paused" ? "active" : "paused";
    setBusyId(t.id); setErr("");
    try {
      const r = await authFetch(`${API_BASE}/buddy/tasks/${t.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: next }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || "Update failed");
      await load();
      toast(next === "paused" ? "⏸ Task paused" : "▶ Task resumed");
    } catch (e) { setErr(String(e?.message || e)); }
    finally { setBusyId(""); }
  };

  const runNow = async (t) => {
    setBusyId(t.id); setErr("");
    try {
      const r = await authFetch(`${API_BASE}/buddy/tasks/${t.id}/run-now`, { method: "POST" });
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || "Couldn't run now");
      toast("▶ Running now — the result will appear in history shortly.");
      // The run executes async on the connector queue; refresh history after a beat.
      setTimeout(() => { loadHistory(t.id); load(); }, 4000);
    } catch (e) { setErr(String(e?.message || e)); }
    finally { setBusyId(""); }
  };

  const remove = async (t) => {
    const ok = await confirm({
      title: "Delete scheduled task",
      message: `Delete this scheduled task?\n\n${t.prompt?.slice(0, 120) || ""}`,
      confirmLabel: "Delete",
      variant: "danger",
    });
    if (!ok) return;
    setBusyId(t.id); setErr("");
    try {
      await authFetch(`${API_BASE}/buddy/tasks/${t.id}`, { method: "DELETE" });
      if (selectedId === t.id) setSelectedId(null);
      // Close the edit modal if it was open for the deleted task
      if (editing?.id === t.id) setEditing(null);
      await load();
      toast("🗑 Task deleted");
    } catch (e) { setErr(String(e?.message || e)); }
    finally { setBusyId(""); }
  };

  // 7-day rollup for the selected task's history.
  const week = useMemo(() => {
    if (!Array.isArray(history)) return null;
    const cutoff = Date.now() - 7 * 86400000;
    const recent = history.filter((h) => new Date(h.created_at).getTime() >= cutoff);
    const ok = recent.filter((h) => normStatus(h.status) === "ok").length;
    const fail = recent.filter((h) => normStatus(h.status) === "error").length;
    return { total: recent.length, ok, fail };
  }, [history]);

  // Derived: true when the user has reached the max active+paused scheduler count.
  const atLimit = tasks.filter((t) => t.status !== "completed").length >= limits.max_schedulers_per_user;

  return (
    <div className="absolute inset-0 z-30 bg-white flex flex-col">
      {/* Header */}
      <div className="flex items-center gap-2 px-5 py-3 border-b border-gray-200">
        <CalendarClock className="w-5 h-5 text-indigo-600" />
        <h2 className="font-semibold text-gray-800">
          Scheduler{projectName ? <span className="text-gray-400 font-normal"> — {projectName}</span> : ""}
        </h2>
        <div className="ml-auto flex flex-col items-end gap-0.5">
          <button
            onClick={() => { if (!atLimit) setEditing({ prompt: "" }); }}
            disabled={atLimit}
            title={atLimit ? `Max ${limits.max_schedulers_per_user} active schedulers allowed` : "New task"}
            className={`inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white ${atLimit ? "opacity-50 cursor-not-allowed" : "hover:bg-indigo-700"}`}
          >
            <Plus className="w-4 h-4" /> New task
          </button>
          {atLimit && (
            <p className="text-[11px] text-amber-600">
              Max {limits.max_schedulers_per_user} active schedulers reached
            </p>
          )}
        </div>
        <button onClick={onClose} className="p-1 text-gray-400 hover:text-gray-700" title="Close">
          <X className="w-5 h-5" />
        </button>
      </div>

      <p className="px-5 pt-2 text-xs text-gray-500">
        Recurring tasks Buddy runs on its own and executes the task at the scheduled time.
      </p>
      {err && <div className="mx-5 mt-2 text-xs text-red-600 bg-red-50 border border-red-200 rounded-md px-2 py-1.5">{err}</div>}

      {/* Body: master list + detail drawer */}
      <div className="flex-1 min-h-0 flex">
        {/* List */}
        <div className={`min-h-0 overflow-y-auto ${selected ? "w-1/2 border-r border-gray-200" : "w-full"}`}>
          {loading ? (
            <div className="flex items-center justify-center h-40 text-gray-400 text-sm">
              <Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading schedules…
            </div>
          ) : tasks.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-64 text-center text-gray-400">
              <Clock className="w-8 h-8 mb-2 opacity-40" />
              <p className="text-sm">No scheduled tasks{projectName ? " in this project" : ""} yet.</p>
              <button onClick={() => setEditing({ prompt: "" })}
                className="mt-3 inline-flex items-center gap-1.5 px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">
                <Plus className="w-4 h-4" /> Schedule a task
              </button>
            </div>
          ) : (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-gray-50 text-gray-500 text-[11px] uppercase tracking-wide">
                <tr>
                  <th className="text-left font-medium px-4 py-2">Task</th>
                  <th className="text-left font-medium px-3 py-2">Schedule</th>
                  <th className="text-left font-medium px-3 py-2">Next run</th>
                  <th className="text-left font-medium px-3 py-2">Last</th>
                  <th className="px-2 py-2"></th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => {
                  const isSel = t.id === selectedId;
                  const paused = t.status === "paused";
                  const completed = t.status === "completed";
                  // Prefer the natural-language summary saved with the task
                  // (from the new RecurrenceEditor). Legacy rows without one
                  // fall back to the best-effort cron parse.
                  const scheduleLabel = t.summary || cronToText(t.cron);
                  return (
                    <tr
                      key={t.id}
                      onClick={completed ? undefined : () => setSelectedId(t.id)}
                      title={completed ? "This schedule has finished. Delete it to remove from the list." : undefined}
                      className={`border-t border-gray-100 ${
                        completed
                          ? "opacity-50 cursor-not-allowed bg-gray-50"
                          : `cursor-pointer hover:bg-indigo-50/40 ${isSel ? "bg-indigo-50/60" : ""}`
                      }`}
                    >
                      <td className="px-4 py-2.5 max-w-[16rem]">
                        <div className="truncate text-gray-800">{t.prompt}</div>
                        {paused && <span className="text-[10px] text-amber-600">paused</span>}
                        {completed && <span className="text-[10px] text-gray-500">completed</span>}
                      </td>
                      <td className="px-3 py-2.5 text-gray-600">{scheduleLabel}</td>
                      <td className="px-3 py-2.5 text-gray-600">
                        {completed ? <span className="text-gray-400">—</span>
                          : paused ? <span className="text-gray-400">—</span>
                          : t.next_run_at ? <span title={fmtDateTime(t.next_run_at)}>{relTime(t.next_run_at)}</span>
                          : <span className="text-gray-400">pending</span>}
                      </td>
                      <td className="px-3 py-2.5"><StatusBadge status={t.last_run_status} /></td>
                      <td className="px-2 py-2.5 text-right">
                        {completed ? (
                          // Completed tasks are otherwise inert — the only
                          // action available is delete, offered inline so the
                          // user can clear the row without opening a drawer.
                          <button
                            onClick={(e) => { e.stopPropagation(); remove(t); }}
                            disabled={busyId === t.id}
                            className="p-1 text-gray-400 hover:text-red-600 disabled:opacity-40"
                            title="Delete finished schedule"
                          >
                            <Trash2 className="w-4 h-4 inline" />
                          </button>
                        ) : (
                          <ChevronRight className="w-4 h-4 text-gray-300 inline" />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Detail drawer */}
        {selected && (
          <div className="w-1/2 min-h-0 overflow-y-auto p-5">
            <div className="flex items-start gap-2 mb-3">
              <div className="min-w-0 flex-1">
                <div className="text-[11px] text-gray-400 uppercase tracking-wide mb-1">Task</div>
                <div className="text-sm text-gray-800 whitespace-pre-wrap">{selected.prompt}</div>
              </div>
              <button onClick={() => setSelectedId(null)} className="p-1 text-gray-400 hover:text-gray-700"><X className="w-4 h-4" /></button>
            </div>

            {/* Action bar */}
            <div className="flex flex-wrap gap-2 mb-4">
              <button onClick={() => setEditing(selected)} disabled={busyId === selected.id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40">
                <Pencil className="w-3.5 h-3.5" /> Edit
              </button>
              <button onClick={() => toggleStatus(selected)} disabled={busyId === selected.id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-40">
                {selected.status === "paused" ? <><Play className="w-3.5 h-3.5" /> Resume</> : <><Pause className="w-3.5 h-3.5" /> Pause</>}
              </button>
              <button onClick={() => runNow(selected)} disabled={busyId === selected.id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-indigo-300 text-indigo-700 hover:bg-indigo-50 disabled:opacity-40">
                {busyId === selected.id ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Play className="w-3.5 h-3.5" />} Run now
              </button>
              <button onClick={() => remove(selected)} disabled={busyId === selected.id}
                className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs rounded-md border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40">
                <Trash2 className="w-3.5 h-3.5" /> Delete
              </button>
            </div>

            {/* Meta */}
            <div className="grid grid-cols-2 gap-3 text-xs mb-4">
              <div>
                <div className="text-gray-400 mb-0.5">Schedule</div>
                <div className="text-gray-700">{selected.summary || cronToText(selected.cron)}</div>
              </div>
              <div><div className="text-gray-400 mb-0.5">Timezone</div><div className="text-gray-700">{selected.tz || "UTC"}</div></div>
              <div><div className="text-gray-400 mb-0.5">Next run</div><div className="text-gray-700">{selected.status === "paused" ? "paused" : selected.status === "completed" || !selected.next_run_at ? "No further runs scheduled" : `${fmtDateTime(selected.next_run_at)} (${relTime(selected.next_run_at) || "pending"})`}</div></div>
              <div><div className="text-gray-400 mb-0.5">Last run</div><div className="text-gray-700 flex items-center gap-2">{fmtDateTime(selected.last_run_at)} <StatusBadge status={selected.last_run_status} /></div></div>
              {selected.role && <div><div className="text-gray-400 mb-0.5">Role</div><div className="text-gray-700">{selected.role}</div></div>}
            </div>

            {/* 7-day history */}
            <div className="flex items-center gap-2 mb-2">
              <div className="text-[11px] text-gray-400 uppercase tracking-wide">Last 7 days</div>
              {week && <span className="text-[11px] text-gray-500">{week.ok}/{week.total} ok{week.fail ? ` · ${week.fail} failed` : ""}</span>}
              <button onClick={() => loadHistory(selected.id)} className="ml-auto p-1 text-gray-400 hover:text-gray-700" title="Refresh history">
                <RotateCcw className="w-3.5 h-3.5" />
              </button>
            </div>

            {histLoading ? (
              <div className="flex items-center text-gray-400 text-xs py-4"><Loader2 className="w-4 h-4 animate-spin mr-2" /> Loading history…</div>
            ) : !Array.isArray(history) || history.length === 0 ? (
              <p className="text-xs text-gray-400 py-2">No runs recorded yet.</p>
            ) : (
              <>
                {/* success/fail strip (most recent 14) */}
                <div className="flex gap-1 mb-3">
                  {history.slice(0, 14).reverse().map((h) => {
                    const n = normStatus(h.status);
                    const c = n === "ok" ? "bg-green-400" : n === "error" ? "bg-red-400" : "bg-amber-300";
                    return <span key={h.id} title={`${fmtDateTime(h.created_at)} · ${h.status}`} className={`h-4 w-3 rounded-sm ${c}`} />;
                  })}
                </div>
                {/* run list */}
                <div className="space-y-1.5">
                  {history.slice(0, 30).map((h) => (
                    <div key={h.id} className="border border-gray-100 rounded-md px-2.5 py-1.5">
                      <div className="flex items-center gap-2">
                        <StatusBadge status={h.status} />
                        <span className="text-[11px] text-gray-500">{fmtDateTime(h.created_at)}</span>
                      </div>
                      {h.error && <div className="mt-1 text-[11px] text-red-600 break-words">{h.error}</div>}
                      {!h.error && h.output && <div className="mt-1 text-[11px] text-gray-500 line-clamp-2 break-words">{h.output}</div>}
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* Create / edit modal */}
      {editing && (
        <TaskEditor
          key={editing.id || "new"}
          task={editing.id ? editing : null}
          defaultPrompt={editing.prompt || ""}
          roles={roles}
          projectId={projectId}
          limits={limits}
          tasks={tasks}
          onClose={() => setEditing(null)}
          onSaved={async (msg, savedTask) => {
            setEditing(null);
            await load();
            if (selectedId) loadHistory(selectedId);
            toast(msg);
          }}
        />
      )}
    </div>
  );
}

// ── Create / edit form (Outlook-style Recurrence editor) ──────────────────
// The form has three concerns:
//   1. What Buddy should do (prompt textarea, unchanged).
//   2. When it runs (RecurrenceEditor — the "Recurrence pattern" + "Range of
//      recurrence" panels, styled after Outlook's Appointment Recurrence).
//   3. Which role to run as (optional).
//
// The recurrence editor emits a validated 5-field cron string PLUS the range
// fields (starts_at / ends_at / max_runs / interval_weeks / interval_months)
// which the backend enforces in workers/cowork_scheduler.py. Users never see
// the cron string — the preview shows a plain-English sentence.
function TaskEditor({ task, defaultPrompt, roles, projectId, limits, tasks, onClose, onSaved }) {
  const [prompt, setPrompt] = useState(task?.prompt || defaultPrompt || "");
  const [role, setRole] = useState(task?.role || "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState("");

  // Seed the recurrence state from the saved task's `recurrence` JSONB when
  // available; otherwise use the default (Daily at 09:00). Legacy tasks that
  // predate the recurrence column show the default — the natural-language
  // description in the list still uses the raw cron via cronToText().
  const [recurrence, setRecurrence] = useState(
    () => parseRecurrence(task?.recurrence) || DEFAULT_RECURRENCE()
  );

  const cron = useMemo(() => buildCronFromRecurrence(recurrence), [recurrence]);
  const summary = useMemo(() => describeRecurrence(recurrence), [recurrence]);
  const cronValid = cron && cron.split(/\s+/).length === 5;

  const save = async () => {
    setErr("");
    if (!prompt.trim()) { setErr("Describe what Buddy should do."); return; }
    if (!cronValid) { setErr("Pick a recurrence pattern."); return; }
    if (recurrence.pattern === "weekly" && (!recurrence.weekdays || recurrence.weekdays.length === 0)) {
      setErr("Pick at least one weekday.");
      return;
    }
    // End date is now mandatory (the "End After" radio has been removed).
    if (!recurrence.range?.end?.date) {
      setErr("Pick an end date for the schedule.");
      return;
    }
    // Enforce per-user scheduler limit on the client side (server also enforces).
    const maxPerUser = limits?.max_schedulers_per_user ?? 5;
    if (!task?.id && (tasks || []).filter((t) => t.status !== "completed").length >= maxPerUser) {
      setErr(`You can only have ${maxPerUser} active schedulers. Delete one before creating a new one.`);
      return;
    }
    setSaving(true);
    try {
      const tz = (() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; } catch { return "UTC"; } })();

      const starts_at = isoStartOfDay(recurrence.range?.start);
      const ends_at   = isoEndOfDay(recurrence.range?.end?.date);
      // Always send the configured max_runs cap — the backend enforces it too.
      const max_runs  = limits?.max_runs_per_scheduler ?? 25;

      // "Every N weeks" / "Every N months" are enforced by the scheduler, not
      // the cron string — only pass the interval to the backend when N > 1.
      const N = Math.max(1, parseInt(recurrence.interval, 10) || 1);
      const interval_weeks  = recurrence.pattern === "weekly"  && N > 1 ? N : null;
      const interval_months = recurrence.pattern === "monthly" && N > 1 ? N : null;

      const payload = {
        prompt: prompt.trim(),
        cron,
        role: role || null,
        tz,
        starts_at,
        ends_at,
        max_runs,
        interval_weeks,
        interval_months,
        recurrence,
        summary,
      };

      let r;
      if (task?.id) {
        r = await authFetch(`${API_BASE}/buddy/tasks/${task.id}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
      } else {
        r = await authFetch(`${API_BASE}/buddy/tasks`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...payload, project_id: projectId || null }),
        });
      }
      if (!r.ok) throw new Error((await r.json().catch(() => ({})))?.detail || "Couldn't save the task.");
      const savedTask = await r.json().catch(() => null);
      onSaved(task?.id ? "✅ Task updated" : `✅ Scheduled — ${(summary || "").toLowerCase()}`, savedTask);
    } catch (e) { setErr(String(e?.message || e)); }
    finally { setSaving(false); }
  };

  return (
    <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/30" onMouseDown={() => !saving && onClose()}>
      <div className="bg-white rounded-xl shadow-xl border border-gray-200 w-[42rem] max-h-[92vh] overflow-y-auto p-5" onMouseDown={(e) => e.stopPropagation()}>
        <h3 className="font-semibold text-gray-800 mb-3">{task?.id ? "Edit scheduled task" : "Schedule a recurring task"}</h3>

        <label className="block text-xs font-medium text-gray-600 mb-1">What should Buddy do?</label>
        <textarea rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)}
          autoFocus
          placeholder="e.g. Summarise my calendar for the day and email me a digest"
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm outline-none focus:border-gray-400 mb-3" />

        <RecurrenceEditor value={recurrence} onChange={setRecurrence} maxRuns={limits?.max_runs_per_scheduler ?? 25} />

        <div className="text-xs text-gray-500 mt-3 mb-3">
          {cronValid
            ? <>Runs: <span className="text-gray-700 font-medium">{summary}</span> <span className="text-gray-400">({(() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return "local time"; } })()})</span></>
            : <span className="text-red-600">Pick a recurrence pattern above.</span>}
        </div>

        {Array.isArray(roles) && roles.length > 0 && (
          <div className="mb-3">
            <label className="block text-xs font-medium text-gray-600 mb-1">Run as role <span className="text-gray-400 font-normal">(optional)</span></label>
            <select value={role} onChange={(e) => setRole(e.target.value)}
              className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm bg-white outline-none">
              <option value="">Generic Buddy</option>
              {roles.map((r) => <option key={r.id || r.name} value={r.name || r.id}>{r.name || r.id}</option>)}
            </select>
          </div>
        )}

        {err && <div className="text-xs text-red-600 bg-red-50 border border-red-200 rounded-md px-2 py-1.5 mb-2">{err}</div>}
        <div className="flex justify-end gap-2">
          <button onClick={() => !saving && onClose()} className="px-3 py-1.5 text-sm rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50">Cancel</button>
          <button onClick={save} disabled={saving || !prompt.trim() || !cronValid}
            className="px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-40 flex items-center gap-1.5">
            {saving ? <><Loader2 className="w-4 h-4 animate-spin" /> Saving…</> : (task?.id ? "Save changes" : "Schedule")}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── RecurrenceEditor ─────────────────────────────────────────────────────
// Two panels stacked vertically, styled after Outlook's Appointment Recurrence
// (see debug/Screenshot 2026-08-10 173114.png). All input flows through the
// `onChange` callback; the parent owns the state so it can compute cron and
// summary reactively.
function RecurrenceEditor({ value, onChange, maxRuns = 25 }) {
  const set = (patch) => onChange({ ...value, ...patch });
  const setRange = (patch) => onChange({ ...value, range: { ...value.range, ...patch } });
  const setRangeEnd = (patch) => onChange({ ...value, range: { ...value.range, end: { ...value.range.end, ...patch } } });
  const setWindow = (patch) => onChange({ ...value, window: { ...value.window, ...patch } });
  const setWindowDays = (patch) => onChange({ ...value, windowDays: { ...value.windowDays, ...patch } });

  const p = value.pattern;
  const isCustomStep = p === "minutely" || p === "hourly";

  const toggleWeekday = (i) => {
    const days = value.weekdays || [];
    const next = days.includes(i) ? days.filter((d) => d !== i) : [...days, i];
    set({ weekdays: next });
  };
  const toggleWindowDay = (i) => {
    const days = value.windowDays.days || [];
    const next = days.includes(i) ? days.filter((d) => d !== i) : [...days, i];
    setWindowDays({ days: next });
  };

  return (
    <div className="space-y-3">
      {/* Recurrence pattern panel */}
      <fieldset className="border border-gray-200 rounded-lg p-3">
        <legend className="text-xs font-semibold text-gray-700 px-1">Recurrence pattern</legend>

        {/* Pattern selector — collapses the five stacked radio rows into a single dropdown. */}
        <div className="flex items-center gap-3 py-1.5">
          <label htmlFor="recurrence-pattern" className="text-sm text-gray-700 w-24 shrink-0">Repeats</label>
          <select
            id="recurrence-pattern"
            value={p}
            onChange={(e) => set(pickPattern(e.target.value, value))}
            className="border border-gray-300 rounded-lg px-2 py-1.5 text-sm bg-white outline-none focus:border-gray-400"
          >
            <option value="minutely">Minutely</option>
            <option value="hourly">Hourly</option>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </div>

        {/* Minutely */}
        {p === "minutely" && (
          <div className="flex items-center gap-2 text-sm py-1.5 pl-[6.5rem]">
            <span className="text-gray-600">Every</span>
            <input type="number" min={10} max={59} step={5} value={value.interval || 10}
              onChange={(e) => {
                const raw = +e.target.value || 10;
                const snapped = Math.round(raw / 5) * 5;
                set({ interval: Math.min(59, Math.max(10, snapped)) });
              }}
              className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center" />
            <span className="text-gray-600">minute(s)</span>
          </div>
        )}

        {/* Hourly */}
        {p === "hourly" && (
          <div className="flex items-center gap-2 text-sm py-1.5 pl-[6.5rem]">
            <span className="text-gray-600">Every</span>
            <input type="number" min={1} max={23} value={value.interval || 2}
              onChange={(e) => set({ interval: Math.min(23, Math.max(1, +e.target.value || 1)) })}
              className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center" />
            <span className="text-gray-600">hour(s)</span>
          </div>
        )}

        {/* Daily */}
        {p === "daily" && (
          <div className="flex flex-col gap-1.5 text-sm py-1.5 pl-[6.5rem]">
            <div className="flex items-center gap-2">
              <label className="inline-flex items-center gap-1.5">
                <input type="radio" name="daily-mode"
                  checked={!value.weekdayOnly} onChange={() => set({ weekdayOnly: false })} />
                Every
              </label>
              <input type="number" min={1} max={365} value={value.interval || 1}
                disabled={value.weekdayOnly}
                onChange={(e) => set({ interval: Math.max(1, +e.target.value || 1) })}
                className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center disabled:bg-gray-50" />
              <span className="text-gray-600">day(s)</span>
              <span className="text-gray-500 mx-2">at</span>
              <input type="time" value={value.time || "09:00"}
                onChange={(e) => set({ time: e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm" />
            </div>
            <label className="inline-flex items-center gap-1.5">
              <input type="radio" name="daily-mode"
                checked={!!value.weekdayOnly} onChange={() => set({ weekdayOnly: true })} />
              Every weekday
            </label>
          </div>
        )}

        {/* Weekly */}
        {p === "weekly" && (
          <div className="flex-1 space-y-1.5 text-sm py-1.5 pl-[6.5rem]">
            <div className="flex items-center gap-2">
              <span className="text-gray-600">Recur every</span>
              <input type="number" min={1} max={52} value={value.interval || 1}
                onChange={(e) => set({ interval: Math.max(1, +e.target.value || 1) })}
                className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center" />
              <span className="text-gray-600">week(s) on:</span>
              <span className="text-gray-500 ml-auto">at</span>
              <input type="time" value={value.time || "09:00"}
                onChange={(e) => set({ time: e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm" />
            </div>
            <div className="grid grid-cols-4 gap-1.5">
              {WEEKDAYS.map((d, i) => (
                <label key={i} className="inline-flex items-center gap-1.5 text-gray-700">
                  <input type="checkbox" checked={(value.weekdays || []).includes(i)} onChange={() => toggleWeekday(i)} />
                  {d}
                </label>
              ))}
            </div>
          </div>
        )}

        {/* Monthly */}
        {p === "monthly" && (
          <div className="flex-1 space-y-1.5 text-sm py-1.5 pl-[6.5rem]">
            <div className="flex items-center gap-2 flex-wrap">
              <label className="inline-flex items-center gap-1.5">
                <input type="radio" name="monthly-mode"
                  checked={value.monthlyMode !== "weekday"} onChange={() => set({ monthlyMode: "day" })} />
                Day
              </label>
              <input type="number" min={1} max={31} value={value.monthDay || 1}
                disabled={value.monthlyMode === "weekday"}
                onChange={(e) => set({ monthDay: Math.min(31, Math.max(1, +e.target.value || 1)) })}
                className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center disabled:bg-gray-50" />
              <span className="text-gray-600">of every</span>
              <input type="number" min={1} max={12} value={value.interval || 1}
                onChange={(e) => set({ interval: Math.min(12, Math.max(1, +e.target.value || 1)) })}
                className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center" />
              <span className="text-gray-600">month(s)</span>
              <span className="text-gray-500 ml-auto">at</span>
              <input type="time" value={value.time || "09:00"}
                onChange={(e) => set({ time: e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm" />
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <label className="inline-flex items-center gap-1.5">
                <input type="radio" name="monthly-mode"
                  checked={value.monthlyMode === "weekday"} onChange={() => set({ monthlyMode: "weekday" })} />
                The
              </label>
              <select value={String(value.monthOrd || 1)}
                disabled={value.monthlyMode !== "weekday"}
                onChange={(e) => {
                  const v = e.target.value; set({ monthOrd: v === "last" ? "last" : parseInt(v, 10) });
                }}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm bg-white disabled:bg-gray-50">
                {MONTHLY_ORDINALS.map((o) => <option key={String(o.value)} value={String(o.value)}>{o.label}</option>)}
              </select>
              <select value={value.monthWeekday ?? 1}
                disabled={value.monthlyMode !== "weekday"}
                onChange={(e) => set({ monthWeekday: +e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm bg-white disabled:bg-gray-50">
                {WEEKDAYS.map((d, i) => <option key={i} value={i}>{d}</option>)}
              </select>
              <span className="text-gray-600">of every</span>
              <input type="number" min={1} max={12} value={value.interval || 1}
                disabled={value.monthlyMode !== "weekday"}
                onChange={(e) => set({ interval: Math.min(12, Math.max(1, +e.target.value || 1)) })}
                className="w-16 border border-gray-300 rounded px-1.5 py-1 text-sm text-center disabled:bg-gray-50" />
              <span className="text-gray-600">month(s)</span>
            </div>
          </div>
        )}

        {/* Time window — only meaningful for Minutely / Hourly */}
        {isCustomStep && (
          <div className="mt-2 pt-2 border-t border-gray-100 space-y-1.5 text-sm">
            <label className="inline-flex items-center gap-1.5">
              <input type="checkbox" checked={!!value.window?.enabled} onChange={(e) => setWindow({ enabled: e.target.checked })} />
              <span className="text-gray-700">Only between</span>
              <input type="time" value={value.window?.start || "09:00"} disabled={!value.window?.enabled}
                onChange={(e) => setWindow({ start: e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm disabled:bg-gray-50" />
              <span className="text-gray-700">and</span>
              <input type="time" value={value.window?.end || "18:00"} disabled={!value.window?.enabled}
                onChange={(e) => setWindow({ end: e.target.value })}
                className="border border-gray-300 rounded px-1.5 py-1 text-sm disabled:bg-gray-50" />
            </label>
            <div className="flex items-center gap-2 flex-wrap">
              <label className="inline-flex items-center gap-1.5">
                <input type="checkbox" checked={!!value.windowDays?.enabled}
                  onChange={(e) => setWindowDays({ enabled: e.target.checked })} />
                <span className="text-gray-700">Only on:</span>
              </label>
              {WEEKDAYS_SHORT.map((d, i) => (
                <label key={i} className={`inline-flex items-center gap-1 ${value.windowDays?.enabled ? "" : "opacity-40"}`}>
                  <input type="checkbox" disabled={!value.windowDays?.enabled}
                    checked={(value.windowDays?.days || []).includes(i)}
                    onChange={() => toggleWindowDay(i)} />
                  {d}
                </label>
              ))}
            </div>
          </div>
        )}
      </fieldset>

      {/* Range of recurrence panel */}
      <fieldset className="border border-gray-200 rounded-lg p-3">
        <legend className="text-xs font-semibold text-gray-700 px-1">Range of recurrence</legend>
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
          <div className="flex items-center gap-2">
            <span className="text-gray-600 w-20">Start date</span>
            <input
              type="date"
              value={value.range?.start || ""}
              onChange={(e) => setRange({ start: e.target.value })}
              className="border border-gray-300 rounded px-1.5 py-1 text-sm"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-gray-600 w-20">End date</span>
            <input
              type="date"
              value={value.range?.end?.date || ""}
              onChange={(e) => setRangeEnd({ mode: "by", date: e.target.value })}
              className="border border-gray-300 rounded px-1.5 py-1 text-sm"
            />
          </div>
        </div>
        <p className="mt-2 text-xs text-blue-600 flex items-center gap-1.5">
          <span>ℹ️</span> Max {maxRuns} occurrences will run for a scheduler.
        </p>
      </fieldset>
    </div>
  );
}
