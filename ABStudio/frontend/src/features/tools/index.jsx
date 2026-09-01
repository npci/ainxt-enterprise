// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useMemo, useRef } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';
import './tools-overhaul.css';

const label = s => s.charAt(0).toUpperCase() + s.slice(1);
const initials = s => (s || '?').slice(0, 2).toUpperCase();

export default function ToolsDashboard() {
  const pageRef = useRef(null);
  // Holds { el, prevOverflow } while the modal is open so we can safely
  // restore the dashboard scroll container's overflow on close — captured
  // once at open time to avoid stale-read races on re-renders.
  const scrollLockRef = useRef(null);
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [search, setSearch] = useState('');
  const [selectedTool, setSelectedTool] = useState(null);
  const [modalRect, setModalRect] = useState({ top: 0, height: 0 });
  const [collapsedGroups, setCollapsedGroups] = useState(new Set());

  const toggleGroup = (service) => {
    setCollapsedGroups(prev => {
      const next = new Set(prev);
      next.has(service) ? next.delete(service) : next.add(service);
      return next;
    });
  };

  useEffect(() => {
    (pageRef.current?.closest('.dashboard-content-area') || pageRef.current?.closest('.main-content'))?.scrollTo({ top: 0, behavior: 'auto' });
  }, []);

  useEffect(() => {
    fetch(`${API_BASE}/tools-catalog`, { headers: buildAuthHeaders() })
      .then(r => {
        if (!r.ok) throw new Error(r.statusText);
        return r.json();
      })
      .then(data => setTools(data.tools || []))
      .catch(err => setError(err.message || 'Failed to load tools'))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!selectedTool) return;
    const handler = e => {
      if (e.key === 'Escape') closeTool();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [selectedTool]);

  // The Build Studio scroll container makes `position: fixed` anchor to the
  // wrong reference when ancestors are transformed. We instead absolutely
  // position the modal at the current scrollTop of the dashboard scroll
  // owner, so it always lands in the user's current viewport region — and
  // we lock that container's overflow so the cards behind don't scroll.
  const openTool = tool => {
    const scrollOwner =
      pageRef.current?.closest('.dashboard-content-area') ||
      pageRef.current?.closest('.main-content');
    if (scrollOwner && !scrollLockRef.current) {
      scrollLockRef.current = { el: scrollOwner, prevOverflow: scrollOwner.style.overflow };
      scrollOwner.style.overflow = 'hidden';
    }
    setModalRect({
      top: scrollOwner?.scrollTop ?? 0,
      // Match the visible scroll-container height so the overlay never extends
      // past the bottom of the viewport — keeps the modal fully on-screen.
      height: scrollOwner?.clientHeight ?? window.innerHeight,
    });
    setSelectedTool(tool);
  };

  const closeTool = () => {
    const lock = scrollLockRef.current;
    if (lock) {
      lock.el.style.overflow = lock.prevOverflow;
      scrollLockRef.current = null;
    }
    setSelectedTool(null);
  };

  // Belt-and-suspenders: if this component unmounts while a tool is open
  // (e.g. user navigates away), make sure we don't leave the container
  // permanently scroll-locked.
  useEffect(() => {
    return () => {
      const lock = scrollLockRef.current;
      if (lock) {
        lock.el.style.overflow = lock.prevOverflow;
        scrollLockRef.current = null;
      }
    };
  }, []);

  const { sortedGroups, filteredCount, serviceCount } = useMemo(() => {
    const q = search.toLowerCase();
    const filtered = q
      ? tools.filter(t =>
          t.name.toLowerCase().includes(q) ||
          t.description.toLowerCase().includes(q)
        )
      : tools;
    const grp = filtered.reduce((acc, tool) => {
      const key = tool.service || 'other';
      (acc[key] = acc[key] || []).push(tool);
      return acc;
    }, {});
    const entries = Object.entries(grp).sort(([a], [b]) => a.localeCompare(b));
    return { sortedGroups: entries, filteredCount: filtered.length, serviceCount: entries.length };
  }, [tools, search]);

  return (
    <div ref={pageRef} className="tools-dashboard animate-fade-in">
      <div className="tools-header">
        <span className="tools-header-eyebrow">Catalog</span>
        <h2 className="tools-title">Tools</h2>
        <p className="tools-subtitle">
          Discover and inspect every integration tool available to your agents.
          <span className="tools-stat-chip">
            <strong>{filteredCount}</strong> tool{filteredCount !== 1 ? 's' : ''}
          </span>
          <span className="tools-stat-chip">
            <strong>{serviceCount}</strong> service{serviceCount !== 1 ? 's' : ''}
          </span>
        </p>
      </div>

      {error && (
        <div className="tools-error">{error}</div>
      )}

      <div className="tools-search-bar">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <circle cx="11" cy="11" r="8" />
          <path d="m21 21-4.35-4.35" />
        </svg>
        <input
          type="text"
          placeholder="Search tools by name or description..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="tools-search-input"
        />
        <span className="tools-search-count">{filteredCount}</span>
      </div>

      {loading ? (
        <div className="tools-loading">Loading tools...</div>
      ) : serviceCount === 0 ? (
        <div className="tools-empty">
          <strong className="tools-empty-title">No tools found</strong>
          <span>{search ? `Nothing matches “${search}”. Try a different keyword.` : 'No tools available yet.'}</span>
        </div>
      ) : (
        <div className="tools-groups" style={{ width: '100%' }}>
          {sortedGroups.map(([service, items]) => {
            const isCollapsed = collapsedGroups.has(service);
            const svcLabel = label(service);
            return (
              <div key={service} className={`tools-group ${isCollapsed ? 'tools-group--collapsed' : ''}`} style={{ width: '100%' }}>
                <button
                  type="button"
                  className="tools-group-header"
                  onClick={() => toggleGroup(service)}
                  aria-expanded={!isCollapsed}
                >
                  <span className="tools-group-icon" data-service={service.toLowerCase()}>
                    {initials(service)}
                  </span>
                  <div className="tools-group-title-block">
                    <span className="tools-group-label">{svcLabel}</span>
                    <span className="tools-group-sublabel">
                      {items.length} tool{items.length !== 1 ? 's' : ''} available
                    </span>
                  </div>
                  <span className="tools-group-count">{items.length}</span>
                  <svg
                    className={`tools-group-chevron ${isCollapsed ? 'tools-group-chevron--collapsed' : ''}`}
                    width="14" height="14" viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
                  >
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </button>
                {!isCollapsed && (
                  <div className="tools-cards">
                    {items.map(tool => {
                      const svcKey = tool.service || 'other';
                      const svc = svcKey.toLowerCase();
                      return (
                        <button
                          key={`${svcKey}__${tool.name}`}
                          className="tool-card"
                          onClick={() => openTool(tool)}
                        >
                          <div className="tool-card-head">
                            <span className="tool-card-icon" data-service={svc}>
                              {initials(svcKey)}
                            </span>
                            <div className="tool-card-name">{tool.name}</div>
                          </div>
                          <p className="tool-card-desc">{tool.description}</p>
                          <div className="tool-card-footer">
                            <span className="tool-card-badge" data-service={svc}>
                              <span className="tool-card-badge-dot" />
                              {svcLabel}
                            </span>
                            <span className="tool-card-cta">
                              View details
                              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M5 12h14" />
                                <path d="m12 5 7 7-7 7" />
                              </svg>
                            </span>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {selectedTool && (
        <div
          className="tm-overlay"
          style={{ top: modalRect.top, height: modalRect.height }}
          onClick={closeTool}
        >
          <div className="tm-modal" onClick={e => e.stopPropagation()}>
            <header className="tm-header">
              <div className="tm-heading">
                <div className="tm-title-row">
                  <h2 className="tm-title">{selectedTool.name}</h2>
                  <span className="tm-service-chip" data-service={(selectedTool.service || 'other').toLowerCase()}>
                    <span className="tm-service-dot" />
                    {label(selectedTool.service || 'other')}
                  </span>
                </div>
                {selectedTool.description && (
                  <p className="tm-description">{selectedTool.description}</p>
                )}
              </div>
              <button
                type="button"
                className="tm-close"
                onClick={closeTool}
                aria-label="Close"
              >
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <line x1="18" y1="6" x2="6" y2="18" />
                  <line x1="6" y1="6" x2="18" y2="18" />
                </svg>
              </button>
            </header>

            <div className="tm-body">
              {selectedTool.input_schema?.properties && (
                <section className="tm-section">
                  <h4 className="tm-section-title">Input Parameters</h4>
                  <div className="tm-table-wrap">
                    <table className="tm-table">
                      <thead>
                        <tr>
                          <th>Parameter</th>
                          <th>Type</th>
                          <th>Required</th>
                          <th>Description</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(selectedTool.input_schema.properties).map(([param, schema]) => (
                          <tr key={param}>
                            <td><code>{param}</code></td>
                            <td>{schema.type || '—'}</td>
                            <td>{(selectedTool.input_schema.required || []).includes(param) ? 'Yes' : 'No'}</td>
                            <td>{schema.description || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              )}

              {selectedTool.code && (
                <section className="tm-section">
                  <h4 className="tm-section-title">Code</h4>
                  <pre className="tm-code"><code>{selectedTool.code}</code></pre>
                </section>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
