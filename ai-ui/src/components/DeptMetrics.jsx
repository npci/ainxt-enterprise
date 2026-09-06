// SPDX-License-Identifier: MIT
import { useState, useEffect } from 'react'

const API = '/ainxt/v1/api'

function StatCard({ label, value, sub }) {
  return (
    <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
      <div className="text-xs text-gray-400 mb-1">{label}</div>
      <div className="text-2xl font-bold text-white">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  )
}

export default function DeptMetrics({ token }) {
  const [departments, setDepartments] = useState([])
  const [selectedDept, setSelectedDept] = useState('')
  const [days, setDays] = useState(7)
  const [stats, setStats] = useState(null)
  const [modelBreakdown, setModelBreakdown] = useState([])
  const [evals, setEvals] = useState([])
  const [loading, setLoading] = useState(false)

  const headers = { Authorization: `Bearer ${token}` }

  useEffect(() => {
    fetch(`${API}/dept-metrics/departments`, { headers })
      .then(r => r.json()).then(d => setDepartments(d.departments || []))
  }, [token])

  useEffect(() => {
    if (!selectedDept) return
    setLoading(true)
    Promise.all([
      fetch(`${API}/dept-metrics/${encodeURIComponent(selectedDept)}?days=${days}`, { headers }).then(r => r.json()),
      fetch(`${API}/dept-metrics/${encodeURIComponent(selectedDept)}/models?days=${days}`, { headers }).then(r => r.json()),
      fetch(`${API}/dept-metrics/${encodeURIComponent(selectedDept)}/evals?days=${days}`, { headers }).then(r => r.json()),
    ]).then(([s, m, e]) => {
      setStats(s)
      setModelBreakdown(m.models || [])
      setEvals(e.evals || [])
    }).finally(() => setLoading(false))
  }, [selectedDept, days, token])

  const s = stats?.summary || {}

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <h2 className="text-xl font-semibold text-white mb-1">Department Metrics</h2>
      <p className="text-sm text-gray-400 mb-6">Model usage, costs, and eval quality by department.</p>

      <div className="flex gap-4 mb-6">
        <div className="flex-1">
          <label className="block text-sm text-gray-400 mb-1">Department</label>
          <select
            className="w-full bg-gray-800 text-white rounded px-3 py-2 border border-gray-600 focus:outline-none"
            value={selectedDept}
            onChange={e => setSelectedDept(e.target.value)}
          >
            <option value="">Select department…</option>
            {departments.map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div>
          <label className="block text-sm text-gray-400 mb-1">Period</label>
          <select
            className="bg-gray-800 text-white rounded px-3 py-2 border border-gray-600 focus:outline-none"
            value={days}
            onChange={e => setDays(Number(e.target.value))}
          >
            <option value={1}>Last 24h</option>
            <option value={7}>Last 7 days</option>
            <option value={30}>Last 30 days</option>
          </select>
        </div>
      </div>

      {loading && <div className="text-gray-400 text-sm">Loading…</div>}

      {stats && !loading && (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
            <StatCard label="Total Requests" value={s.total_requests?.toLocaleString() ?? '—'} />
            <StatCard label="Total Tokens" value={s.total_tokens?.toLocaleString() ?? '—'} />
            <StatCard label="Cost (USD)" value={s.total_cost_usd ? `$${Number(s.total_cost_usd).toFixed(4)}` : '$0.00'} />
            <StatCard label="Avg Latency" value={s.avg_latency_ms ? `${Math.round(s.avg_latency_ms)}ms` : '—'} sub={`${s.unique_users ?? 0} unique users`} />
          </div>

          {modelBreakdown.length > 0 && (
            <div className="mb-6">
              <h3 className="text-sm font-medium text-gray-300 mb-3">Model Breakdown</h3>
              <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-700 text-gray-400">
                      <th className="px-4 py-2 text-left">Model</th>
                      <th className="px-4 py-2 text-right">Requests</th>
                      <th className="px-4 py-2 text-right">Tokens</th>
                      <th className="px-4 py-2 text-right">Cost</th>
                      <th className="px-4 py-2 text-right">Avg Latency</th>
                    </tr>
                  </thead>
                  <tbody>
                    {modelBreakdown.map(m => (
                      <tr key={m.model} className="border-b border-gray-700 last:border-0 text-white">
                        <td className="px-4 py-2 font-mono text-xs">{m.model}</td>
                        <td className="px-4 py-2 text-right">{m.requests?.toLocaleString()}</td>
                        <td className="px-4 py-2 text-right">{m.tokens?.toLocaleString()}</td>
                        <td className="px-4 py-2 text-right">${Number(m.cost_usd || 0).toFixed(4)}</td>
                        <td className="px-4 py-2 text-right">{m.avg_latency_ms ? `${Math.round(m.avg_latency_ms)}ms` : '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {evals.length > 0 && (
            <div>
              <h3 className="text-sm font-medium text-gray-300 mb-3">Eval Quality (daily)</h3>
              <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-700 text-gray-400">
                      <th className="px-4 py-2 text-left">Date</th>
                      <th className="px-4 py-2 text-right">Grounding</th>
                      <th className="px-4 py-2 text-right">Completeness</th>
                      <th className="px-4 py-2 text-right">Avg Chunks</th>
                      <th className="px-4 py-2 text-right">Evals</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evals.map(e => (
                      <tr key={e.day} className="border-b border-gray-700 last:border-0 text-white">
                        <td className="px-4 py-2">{e.day}</td>
                        <td className="px-4 py-2 text-right">
                          <span className={`${e.avg_grounding >= 0.6 ? 'text-green-400' : e.avg_grounding >= 0.3 ? 'text-yellow-400' : 'text-red-400'}`}>
                            {(e.avg_grounding * 100).toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-4 py-2 text-right">{(e.avg_completeness * 100).toFixed(1)}%</td>
                        <td className="px-4 py-2 text-right">{e.avg_chunks}</td>
                        <td className="px-4 py-2 text-right">{e.total_evals}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
