// SPDX-License-Identifier: MIT
/**
 * Empty-state block shown in the Build Studio template grids when no
 * templates match the active filters. The "filtered" variant offers a
 * reset action; the "empty" variant just explains the section is bare.
 */
function TemplatesEmptyState({ filtered, onReset, noun = 'templates' }) {
    if (filtered) {
        return (
            <div className="dashboard-empty animate-fade-in">
                <div className="dashboard-empty-icon">
                    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                        <circle cx="11" cy="11" r="8" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
                    </svg>
                </div>
                <h3 className="dashboard-empty-title">No {noun} found</h3>
                <p className="dashboard-empty-sub">Try a different search or category.</p>
                <button className="dashboard-empty-btn" onClick={onReset}>Reset filters</button>
            </div>
        );
    }

    return (
        <div className="dashboard-empty animate-fade-in">
            <div className="dashboard-empty-icon">
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <rect x="3" y="3" width="7" height="7" rx="1" />
                    <rect x="14" y="3" width="7" height="7" rx="1" />
                    <rect x="3" y="14" width="7" height="7" rx="1" />
                    <rect x="14" y="14" width="7" height="7" rx="1" />
                </svg>
            </div>
            <h3 className="dashboard-empty-title">No {noun} yet</h3>
            <p className="dashboard-empty-sub">Templates will appear here once added.</p>
        </div>
    );
}

export default TemplatesEmptyState;
