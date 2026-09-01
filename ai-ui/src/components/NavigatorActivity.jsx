// SPDX-License-Identifier: Apache-2.0
// NavigatorActivity — live panel shown while explore stages run.
// Polls run events every 5 seconds to show what the navigator is doing.
// Accepts optional `activeStages` prop (array of stage names).
// Default covers ANALYZING, DESIGNING, PLAN, DIAGNOSING.
import { useState, useEffect, useRef } from "react";
import { Search, FileText, Folder, Database, CheckCircle2 } from "lucide-react";
import { API_BASE as API, apiFetch } from '../config';

const DEFAULT_ACTIVE_STAGES = ['ANALYZING', 'DESIGNING', 'PLAN', 'DIAGNOSING'];

function ToolLine({ line }) {
  const lower = line.toLowerCase();
  const isCacheHit = lower.includes('cache hit') || lower.includes('[explore-cache') && lower.includes('hit:');
  let icon = <Database size={12} className="text-gray-400" />;
  if (lower.includes('grep')) icon = <Search size={12} className="text-blue-400" />;
  else if (lower.includes('read_file') || lower.includes('read:')) icon = <FileText size={12} className="text-green-400" />;
  else if (lower.includes('list_tree')) icon = <Folder size={12} className="text-amber-400" />;
  return (
    <div className={`flex items-start gap-1.5 py-0.5 ${isCacheHit ? 'opacity-40' : ''}`}>
      <span className="mt-0.5 flex-shrink-0">{icon}</span>
      <span className="text-xs font-mono text-gray-600 break-all">{line}</span>
    </div>
  );
}

export default function NavigatorActivity({ run, activeStages = DEFAULT_ACTIVE_STAGES }) {
  const stage = run?.state;
  const runId = run?.id;
  const isActive = activeStages.includes(stage);
  const isComplete = run?.state && !activeStages.includes(run.state) &&
    run.events?.some(e => activeStages.includes(e.stage));

  const [events, setEvents] = useState([]);
  const [filesRead, setFilesRead] = useState([]);
  const [round, setRound] = useState(0);
  const bottomRef = useRef(null);

  // Use the first active stage as the poll filter (best-effort; covers the common case).
  // When the run is complete we scan all active stage names via the general events endpoint.
  const pollStage = isActive ? stage : (activeStages[0] || 'ANALYZING');

  useEffect(() => {
    if (!runId || (!isActive && !isComplete)) return;
    let cancelled = false;

    async function poll() {
      try {
        const resp = await apiFetch(
          `${API}/sdlc/runs/${runId}/events?stage=${pollStage}&actor=agent-loop&limit=50`
        );
        if (!resp.ok || cancelled) return;
        const data = await resp.json();
        const evts = Array.isArray(data) ? data : (data.events || []);
        const lines = evts.map(e => e.output || e.message || '').filter(Boolean);
        setEvents(lines);
        const readLines = lines.filter(l => l.includes('read_file') || l.includes('explore-read'));
        const paths = [...new Set(readLines.map(l => {
          const m = l.match(/['"](\w[\w/.-]*\.\w+)['"]/);
          return m ? m[1] : null;
        }).filter(Boolean))];
        setFilesRead(paths);
        const roundMatch = lines.join('').match(/round[=\s]+(\d+)/i);
        if (roundMatch) setRound(parseInt(roundMatch[1]));
      } catch (_) {}
    }

    poll();
    if (!isActive) return;
    const id = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [runId, isActive, isComplete, pollStage]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [events]);

  if (!isActive && events.length === 0) return null;

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 mt-2">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-gray-600">
          {isActive ? '🔄 Navigator Activity' : '✓ Navigator Complete'}
        </span>
        {round > 0 && (
          <span className="text-xs text-gray-500">Round {round}</span>
        )}
      </div>

      {events.length > 0 && (
        <div className="max-h-40 overflow-y-auto bg-white rounded border border-gray-100 p-2 mb-2">
          {events.map((line, i) => <ToolLine key={i} line={line} />)}
          <div ref={bottomRef} />
        </div>
      )}

      {filesRead.length > 0 && (
        <div>
          <p className="text-xs text-gray-500 mb-1">Files read ({filesRead.length}):</p>
          <div className="flex flex-wrap gap-1">
            {filesRead.map((f, i) => (
              <span key={i} className="text-xs bg-indigo-50 text-indigo-700 px-1.5 py-0.5 rounded font-mono">
                {f.split('/').pop()}
              </span>
            ))}
          </div>
        </div>
      )}

      {isComplete && (
        <div className="flex items-center gap-1 mt-1">
          <CheckCircle2 size={12} className="text-green-500" />
          <span className="text-xs text-gray-500">
            Navigator finished — {filesRead.length} file(s) read
          </span>
        </div>
      )}
    </div>
  );
}
