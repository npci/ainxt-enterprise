// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect } from 'react'
import { ChevronDown, ChevronRight, Shield, Users } from 'lucide-react'
import { authFetch, API_BASE as API } from '../config'
import { validateIdentifier } from '../utils/securityValidation'

const CLAUDE_HAIKU_4_5 = ['claude', 'haiku', '4-5', '20251001'].join('-')

// Display metadata for known model IDs — labels and colours for the governance UI.
// This is a capability display map, NOT a list of defaults or required models.
// Models not in this map fall through to providerMeta() which derives label/provider
// from the model ID string, so unknown/custom models still render correctly.
const PROVIDER_META = {
  'claude-sonnet-4-6':          { label: 'Claude Sonnet 4.6',  provider: 'Claude',  color: 'bg-orange-100 text-orange-700' },
  [CLAUDE_HAIKU_4_5]:           { label: 'Claude Haiku 4.5',   provider: 'Claude',  color: 'bg-orange-100 text-orange-700' },
  'claude-opus-4-7':            { label: 'Claude Opus 4.7',    provider: 'Claude',  color: 'bg-orange-100 text-orange-700' },
  'claude-opus-4-6':            { label: 'Claude Opus 4.6',    provider: 'Claude',  color: 'bg-orange-100 text-orange-700' },
  'gpt-5.4':                    { label: 'GPT-5.4',            provider: 'OpenAI',  color: 'bg-green-100 text-green-700'  },
  'gpt-5-5':                    { label: 'GPT-5-5 (Latest)',   provider: 'OpenAI',  color: 'bg-green-100 text-green-700'  },
  'gpt-5-mini':                 { label: 'GPT-5 Mini',         provider: 'OpenAI',  color: 'bg-green-100 text-green-700'  },
  'gemini-3.5-flash':           { label: 'Gemini 3.5 Flash (Coding)',      provider: 'Gemini', color: 'bg-blue-100 text-blue-700' },
  'gemini-3.1-flash-lite':      { label: 'Gemini 3.1 Flash-Lite (Coding)', provider: 'Gemini', color: 'bg-blue-100 text-blue-700' },
  'gemini-3.1-flash-image':     { label: 'Gemini 3.1 Flash Image',         provider: 'Gemini', color: 'bg-blue-100 text-blue-700' },
}

function providerMeta(modelId) {
  if (modelId.startsWith('local:')) {
    const name = modelId.slice(6)
    return { label: name, provider: 'Local', color: 'bg-purple-100 text-purple-700' }
  }
  return PROVIDER_META[modelId] || { label: modelId, provider: 'Unknown', color: 'bg-gray-100 text-gray-600' }
}

function Toggle({ on, onChange, disabled }) {
  return (
    <button
      type="button"
      onClick={onChange}
      disabled={disabled}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors focus:outline-none
        ${on ? 'bg-green-500' : 'bg-gray-300'} ${disabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform
        ${on ? 'translate-x-4.5' : 'translate-x-0.5'}`} />
    </button>
  )
}

function Toast({ message, onClose }) {
  useEffect(() => {
    if (!message) return
    const t = setTimeout(onClose, 3000)
    return () => clearTimeout(t)
  }, [message, onClose])

  if (!message) return null
  return (
    <div className="fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-700 shadow-lg">
      <span className="h-2 w-2 rounded-full bg-green-500" />
      {message}
    </div>
  )
}

// ── "Models" — user-level access control ────────────────────────────────────
// Pick a model, then grant/restrict it for individual users. There is no
// department axis in this UI any more — governance is purely per-user.
// A missing per-user rule means "allowed" (fail-open, matches
// filter_allowed_models()'s server-side default).

function ModelsSection({ models, onToast }) {
  const [users, setUsers]         = useState([])
  const [userPerms, setUserPerms] = useState([])
  const [expanded, setExpanded]   = useState({})   // modelId → bool
  const [search, setSearch]       = useState({})   // modelId → string
  const [saving, setSaving]       = useState(null) // "userId:modelId"

  useEffect(() => {
    authFetch(`${API}/model-governance/users`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUsers(d.users || []) })
    authFetch(`${API}/model-governance/user-permissions`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUserPerms(d.permissions || []) })
  }, [])

  function getUserPermission(userId, modelId) {
    return userPerms.find(p => p.user_id === userId && p.model_id === modelId)
  }

  function getUserAllowed(userId, modelId) {
    const p = getUserPermission(userId, modelId)
    return p === undefined ? true : p.allowed   // no rule = allowed
  }

  function getUserWebSearchAllowed(userId, modelId) {
    const p = getUserPermission(userId, modelId)
    return p === undefined ? false : !!p.web_search_allowed
  }

  async function saveUserPermission(userId, modelId, updates) {
    const existing = getUserPermission(userId, modelId)
    const payload = {
      user_id: userId,
      model_id: modelId,
      allowed: existing === undefined ? true : existing.allowed,
      web_search_allowed: existing === undefined ? false : !!existing.web_search_allowed,
      ...updates,
    }

    // Client-side pre-check mirroring validate_model_permission_request() in
    // core/security_validation.py — model_id/user_id are identifiers.
    // `department` is intentionally not sent (governance is user-only now;
    // the backend resolves it from the target user's own record). Backend
    // remains the authoritative enforcer either way.
    if (
      !validateIdentifier(payload.model_id).isValid ||
      !validateIdentifier(payload.user_id).isValid
    ) {
      console.error('Invalid model_id/user_id for model-governance user save', payload)
      return
    }

    const key = `${userId}:${modelId}`
    setSaving(key)
    try {
      await authFetch(`${API}/model-governance/user`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      setUserPerms(prev => {
        const current = prev.find(p => p.user_id === userId && p.model_id === modelId)
        const nextRecord = current
          ? { ...current, ...payload }
          : { user_id: userId, model_id: modelId, ...payload }
        return current
          ? prev.map(p => p.user_id === userId && p.model_id === modelId ? nextRecord : p)
          : [...prev, nextRecord]
      })
    } finally {
      setSaving(null)
    }
  }

  async function toggleUser(userId, modelId) {
    const newAllowed = !getUserAllowed(userId, modelId)
    await saveUserPermission(userId, modelId, {
      allowed: newAllowed,
      web_search_allowed: newAllowed ? getUserWebSearchAllowed(userId, modelId) : false,
    })
    onToast?.(`${providerMeta(modelId).label} ${newAllowed ? 'enabled' : 'disabled'} for user`)
  }

  async function toggleUserWebSearch(userId, modelId) {
    await saveUserPermission(userId, modelId, {
      web_search_allowed: !getUserWebSearchAllowed(userId, modelId),
    })
  }

  if (models.length === 0) return null

  return (
    <div className="rounded-xl border border-gray-200 bg-white shadow-sm overflow-hidden">
      <div className="flex items-center gap-2 border-b border-gray-100 bg-gray-50 px-4 py-3">
        <Users className="h-4 w-4 text-gray-400" />
        <span className="text-sm font-semibold text-gray-700">Models</span>
        <span className="ml-1 text-xs text-gray-400">— select a model to edit access per user</span>
      </div>

      {users.length === 0 && (
        <div className="px-4 py-6 text-center text-sm text-gray-400">No users found</div>
      )}

      {models.map(modelId => {
        const meta   = providerMeta(modelId)
        const isOpen = expanded[modelId]
        return (
          <div key={modelId} className="border-b border-gray-100 last:border-0">
            {/* Model accordion header */}
            <button
              type="button"
              onClick={() => setExpanded(prev => ({ ...prev, [modelId]: !prev[modelId] }))}
              className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-gray-50 transition-colors"
            >
              <div className="flex items-center gap-2">
                {isOpen
                  ? <ChevronDown className="h-4 w-4 text-gray-400" />
                  : <ChevronRight className="h-4 w-4 text-gray-400" />}
                <span className="text-sm font-medium text-gray-800">{meta.label}</span>
                <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${meta.color}`}>
                  {meta.provider}
                </span>
              </div>
              <div className="flex items-center gap-2 text-xs text-gray-400">
                <span>{users.length} users</span>
                {userPerms.filter(p => p.model_id === modelId && !p.allowed).length > 0 && (
                  <span className="rounded-full bg-red-100 px-2 py-0.5 text-red-600 font-medium">
                    {userPerms.filter(p => p.model_id === modelId && !p.allowed).length} restricted
                  </span>
                )}
              </div>
            </button>

            {/* User list */}
            {isOpen && (() => {
              const q = (search[modelId] || '').toLowerCase()
              // restricted first, then alphabetical; filter by search
              const filtered = users
                .filter(u => !q || (u.name || '').toLowerCase().includes(q) || u.email.toLowerCase().includes(q))
                .sort((a, b) => {
                  const aR = !getUserAllowed(a.id, modelId)
                  const bR = !getUserAllowed(b.id, modelId)
                  if (aR !== bR) return aR ? -1 : 1
                  return (a.name || a.email).localeCompare(b.name || b.email)
                })
              const restrictedCount = users.filter(u => !getUserAllowed(u.id, modelId)).length

              return (
                <div className="border-t border-gray-100">
                  {/* Sticky search + summary bar */}
                  <div className="sticky top-0 z-10 flex items-center gap-3 bg-white px-4 py-2 border-b border-gray-100 shadow-sm">
                    <input
                      type="text"
                      placeholder="Search users…"
                      value={search[modelId] || ''}
                      onChange={e => setSearch(prev => ({ ...prev, [modelId]: e.target.value }))}
                      className="flex-1 rounded-md border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs text-gray-700 placeholder-gray-400 focus:border-indigo-300 focus:outline-none focus:ring-1 focus:ring-indigo-100"
                    />
                    <span className="shrink-0 text-xs text-gray-400">
                      {filtered.length} of {users.length} users
                      {restrictedCount > 0 && (
                        <span className="ml-2 text-red-500 font-medium">{restrictedCount} restricted</span>
                      )}
                    </span>
                  </div>

                  {/* Scrollable user rows */}
                  <div className="max-h-72 overflow-y-auto divide-y divide-gray-100 bg-gray-50">
                    {filtered.length === 0 && (
                      <div className="px-6 py-6 text-center text-sm text-gray-400">
                        {users.length === 0 ? 'No users found' : 'No users match your search'}
                      </div>
                    )}
                    {filtered.map(u => {
                      const effectiveAllowed   = getUserAllowed(u.id, modelId)
                      const effectiveWebSearch = effectiveAllowed && getUserWebSearchAllowed(u.id, modelId)
                      const key = `${u.id}:${modelId}`
                      return (
                        <div
                          key={u.id}
                          className={`flex items-center justify-between px-6 py-2.5 hover:bg-white transition-colors
                            ${!effectiveAllowed ? 'bg-red-50 hover:bg-red-50' : ''}`}
                        >
                          <div className="flex min-w-0 flex-col">
                            <span className="truncate text-sm text-gray-800">{u.name || u.email}</span>
                            <span className="truncate text-xs text-gray-400">{u.email}</span>
                          </div>
                          <div className="ml-4 flex shrink-0 items-center gap-4">
                            {!effectiveAllowed && (
                              <span className="text-xs font-medium text-red-500">Restricted</span>
                            )}
                            <div className="flex items-center gap-2 text-xs text-gray-500">
                              <span>Access</span>
                              <Toggle
                                on={effectiveAllowed}
                                onChange={() => toggleUser(u.id, modelId)}
                                disabled={saving === key}
                              />
                            </div>
                            <div className="flex items-center gap-2 text-xs text-gray-500">
                              <span>Web Search</span>
                              <Toggle
                                on={effectiveWebSearch}
                                onChange={() => toggleUserWebSearch(u.id, modelId)}
                                disabled={saving === key || !effectiveAllowed}
                              />
                            </div>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })()}
          </div>
        )
      })}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function ModelGovernance() {
  const [models, setModels] = useState([])
  const [toast, setToast]   = useState('')

  useEffect(() => {
    authFetch(`${API}/model-governance/models`)
      .then(r => r.json()).then(d => setModels(d.models || []))
  }, [])

  return (
    <div className="h-full overflow-y-auto ">
      <Toast message={toast} onClose={() => setToast('')} />
      <div className="flex flex-col">
    {/* Header */}
      <div className="border-b border-gray-200 p-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
            <Shield className="h-5 w-5 text-indigo-500" />
            <div>
            <h1 className="text-sm font-semibold  text-indigo-700">Model Access</h1>
            <p className="text-xs text-gray-500">Control which AI models each user can access, and whether they can use Web Search.</p>
            </div>
          </div>
        </div>
      </div>
      <div className="mx-auto max-w-3xl px-6 py-8">
        <ModelsSection models={models} onToast={setToast} />
      </div>
    </div>
  )
}
