// SPDX-License-Identifier: Apache-2.0
import { useState, useCallback } from 'react';
import { API_BASE, buildAuthHeaders } from '../../config/api';
import FactoryChatShell from '../_shared/FactoryChatShell';
import { useFactoryChatStream } from '../_shared/useFactoryChatStream';
import PlanCard from '../../components/common/PlanCard';

/**
 * SkillFactoryChat — AI-powered skill creation, built on the shared
 * FactoryChatShell + useFactoryChatStream. Keeps the skill-specific
 * SKILL.md preview/editor, bundled-file editing, visibility selector,
 * quality badge, and .md download.
 */

const WELCOME_SUGGESTIONS = [
    { icon: '📝', label: 'Extract action items from meeting notes' },
    { icon: '🧾', label: 'Parse invoices into structured JSON' },
    { icon: '📈', label: 'Turn raw metrics into a weekly status report' },
    { icon: '🐍', label: 'Analyze a document with Python helper scripts' },
];

const SKILL_ICON = (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2L15 9H22L16.5 13.5L18.5 21L12 16.5L5.5 21L7.5 13.5L2 9H9L12 2Z" />
    </svg>
);

function SkillFactoryChat({ onClose, onCreated }) {
    const [inputValue, setInputValue] = useState('');
    const [assembledSkill, setAssembledSkill] = useState(null);
    const [isSaving, setIsSaving] = useState(false);
    const [saveError, setSaveError] = useState('');
    const [existingMatches, setExistingMatches] = useState([]);
    const [showPreview, setShowPreview] = useState(true);
    const [editContent, setEditContent] = useState('');
    const [editBundles, setEditBundles] = useState({});
    const [visibility, setVisibility] = useState('private');
    const [planCard, setPlanCard] = useState(null);

    const onMessage = useCallback((ev) => {
        if (ev.data?.assembled) {
            const asm = ev.data.assembled;
            setAssembledSkill(asm);
            setEditContent(asm.content || '');
            const bundles = {};
            for (const f of asm.bundle_files || []) bundles[f.rel_path] = f.content || '';
            setEditBundles(bundles);
        }
        if (ev.stage === 'plan_card') {
            setPlanCard(ev.data?.plan_card ?? null);
        } else if (ev.stage) {
            setPlanCard(null);
        }
        if (ev.stage === 'suggest_existing') {
            setExistingMatches(ev.data?.existing_matches || []);
        } else if (ev.data?.existing_matches === undefined) {
            setExistingMatches([]);
        }
    }, []);

    const onReset = useCallback(() => {
        setSaveError('');
        setExistingMatches([]);
        setPlanCard(null);
    }, []);

    const {
        messages, suggestions, stage, isLoading, sessionId, sendMessage,
    } = useFactoryChatStream({ endpoint: '/skill-factory/chat', onMessage, onReset });

    const handleSend = (text) => {
        setInputValue('');
        sendMessage(text);
    };

    const handleChipClick = (text) => {
        if (!text || isLoading) return;
        sendMessage(text);
    };

    const handleBuildAnyway = () => {
        if (isLoading) return;
        handleSend("None of these fit — let's continue building a new skill.");
    };

    const handleSave = async () => {
        if (!sessionId || isSaving) return;
        setIsSaving(true);
        setSaveError('');
        try {
            const contentChanged = assembledSkill && editContent.trim() !== (assembledSkill.content || '').trim();
            const bundleOverrides = Object.entries(editBundles)
                .filter(([rel]) => {
                    const orig = (assembledSkill?.bundle_files || []).find((f) => f.rel_path === rel);
                    return orig && editBundles[rel] !== (orig.content || '');
                })
                .map(([rel_path, content]) => ({ rel_path, content }));

            const res = await fetch(`${API_BASE}/skill-factory/confirm`, {
                method: 'POST',
                headers: buildAuthHeaders(),
                body: JSON.stringify({
                    session_id: sessionId,
                    visibility,
                    ...(contentChanged ? { content_override: editContent } : {}),
                    ...(bundleOverrides.length ? { bundle_overrides: bundleOverrides } : {}),
                }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Save failed');
            onCreated(data);
        } catch (err) {
            setSaveError(err.message);
            setIsSaving(false);
        }
    };

    const handleDownload = async () => {
        if (!sessionId || !assembledSkill) return;
        try {
            const res = await fetch(`${API_BASE}/skill-factory/${sessionId}/download`, {
                headers: buildAuthHeaders(),
            });
            if (!res.ok) throw new Error('Download failed');
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${assembledSkill.name || 'skill'}.md`;
            a.click();
            URL.revokeObjectURL(url);
        } catch (err) {
            setSaveError(err.message);
        }
    };

    const isConfirm = stage === 'confirm' && assembledSkill;

    const hero = (
        <div style={S.heroCard}>
            <div style={S.heroIcon}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 2L15 9H22L16.5 13.5L18.5 21L12 16.5L5.5 21L7.5 13.5L2 9H9L12 2Z" />
                </svg>
            </div>
            <div style={S.heroTitle}>What skill do you have in mind?</div>
            <div style={S.heroSub}>
                Describe the capability in plain language — I'll ask a few questions, draft the skill, then
                iterate against test prompts until it triggers reliably. If it needs a helper script or reference
                doc, I'll generate those too.
            </div>
        </div>
    );

    const matchCards = stage === 'suggest_existing' && existingMatches.length > 0 ? (
        <div style={S.matchGroup}>
            {existingMatches.map((m) => {
                const conf = Math.round((m._match?.confidence || 0) * 100);
                return (
                    <div key={`skill-${m.id}`} style={S.matchCard}>
                        <div style={S.matchCardTop}>
                            <span style={S.matchName}>{m.name}</span>
                            <span style={S.matchBadge}>Skill · {conf}% match</span>
                        </div>
                        {m.description && <div style={S.matchDesc}>{m.description}</div>}
                        {m._match?.reason && <div style={S.matchReason}>{m._match.reason}</div>}
                    </div>
                );
            })}
            <div style={S.matchHint}>This skill is already in your catalog and ready to use in agents and workflows.</div>
            <button type="button" style={S.buildAnywayBtn} onClick={handleBuildAnyway} disabled={isLoading}>
                Continue building
            </button>
        </div>
    ) : null;

    // Preview/editor + save bar go above the input.
    const aboveInput = isConfirm ? (
        <>
            <div style={S.previewWrap}>
                <div style={S.previewHeader}>
                    <span style={S.previewTitle}>Generated SKILL.md</span>
                    <button type="button" style={S.previewToggle} onClick={() => setShowPreview((v) => !v)}>
                        {showPreview ? 'Hide' : 'Show & edit'}
                    </button>
                </div>
                {showPreview && (
                    <div style={S.previewBody}>
                        <textarea
                            style={S.previewTextarea}
                            value={editContent}
                            onChange={(e) => setEditContent(e.target.value)}
                            spellCheck={false}
                            rows={14}
                        />
                        {Object.keys(editBundles).length > 0 && (
                            <div style={S.bundleEditGroup}>
                                <div style={S.bundleEditLabel}>Bundled files</div>
                                {Object.keys(editBundles).map((rel) => (
                                    <div key={rel} style={{ marginTop: '8px' }}>
                                        <div style={S.bundleFileName}>{rel}</div>
                                        <textarea
                                            style={S.bundleTextarea}
                                            value={editBundles[rel]}
                                            onChange={(e) => setEditBundles((prev) => ({ ...prev, [rel]: e.target.value }))}
                                            spellCheck={false}
                                            rows={8}
                                        />
                                    </div>
                                ))}
                            </div>
                        )}
                    </div>
                )}
            </div>

            <div style={S.applyBar}>
                <div style={{ flex: 1, minWidth: 0 }}>
                    {saveError && <div style={{ color: '#f87171', fontSize: '12px', marginBottom: '6px' }}>{saveError}</div>}
                    <div style={{ fontSize: '12px', color: '#64748b', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '6px' }}>
                        <span>Ready:</span>
                        <span style={{ color: '#4f46e5', fontWeight: 600 }}>{assembledSkill.display_name || assembledSkill.name}</span>
                        <span style={{ color: '#94a3b8' }}>· {assembledSkill.category}</span>
                        {assembledSkill.quality && typeof assembledSkill.quality.critique_score === 'number' && (
                            <span
                                title={`Iterated ${assembledSkill.quality.iterations || 1}× — structure ${assembledSkill.quality.critique_score}/100`}
                                style={{
                                    padding: '2px 8px', borderRadius: 999, fontSize: '11px', fontWeight: 600,
                                    background: assembledSkill.quality.passed ? '#dcfce7' : '#fef3c7',
                                    color: assembledSkill.quality.passed ? '#15803d' : '#a16207',
                                }}
                            >
                                {assembledSkill.quality.passed ? '✓' : '⚠'} {assembledSkill.quality.critique_score}/100
                            </span>
                        )}
                        {assembledSkill.bundle_files && assembledSkill.bundle_files.length > 0 && (
                            <span
                                title={assembledSkill.bundle_files.map((f) => f.rel_path).join('\n')}
                                style={{ padding: '2px 8px', borderRadius: 999, fontSize: '11px', fontWeight: 600, background: '#eef2ff', color: '#4338ca' }}
                            >
                                +{assembledSkill.bundle_files.length} file{assembledSkill.bundle_files.length === 1 ? '' : 's'}
                            </span>
                        )}
                    </div>
                    {assembledSkill.bundle_files && assembledSkill.bundle_files.length > 0 && (
                        <div style={{ marginTop: '6px', fontSize: '11px', color: '#64748b', display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                            {assembledSkill.bundle_files.map((f) => (
                                <span
                                    key={f.rel_path}
                                    title={f.description || ''}
                                    style={{
                                        padding: '2px 6px', borderRadius: '4px', background: '#f1f5f9',
                                        color: f.kind === 'script' ? '#0369a1' : '#7c3aed',
                                        fontFamily: 'var(--font-family-mono, ui-monospace, monospace)', fontSize: '10px',
                                    }}
                                >
                                    {f.rel_path}
                                </span>
                            ))}
                        </div>
                    )}
                </div>
                <div style={{ display: 'flex', gap: '8px', flexShrink: 0, alignItems: 'center' }}>
                    <div style={S.visibilityGroup} title="Public: all users · Private: your department">
                        <button type="button" style={S.visibilityBtn(visibility === 'private')} onClick={() => setVisibility('private')}>Private</button>
                        <button type="button" style={S.visibilityBtn(visibility === 'public')} onClick={() => setVisibility('public')}>Public</button>
                    </div>
                    <button onClick={handleDownload} title="Download as .md file" style={S.downloadBtn}>
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                            <polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                        </svg>
                        .md
                    </button>
                    <button style={S.applyBtn(isSaving)} onClick={handleSave} disabled={isSaving}>
                        {isSaving ? (<><div style={S.btnSpinner} />Saving...</>) : (
                            <>
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                                    <polyline points="20 6 9 17 4 12" />
                                </svg>
                                Save Skill
                            </>
                        )}
                    </button>
                </div>
            </div>
        </>
    ) : null;

    // Welcome chips before any conversation; stream suggestions afterwards.
    // Hidden entirely in confirm stage (matches prior behaviour).
    let shownSuggestions = [];
    if (stage !== 'confirm') {
        shownSuggestions = messages.length === 0 ? WELCOME_SUGGESTIONS : suggestions;
    }

    const planCardNode = planCard && !isLoading ? (
        <PlanCard
            planCard={planCard}
            disabled={isLoading}
            onAccept={(answers) => { setPlanCard(null); handleSend(`__plan_card__:${JSON.stringify(answers)}`); }}
            onChangeSomething={() => { setPlanCard(null); handleSend("I'd like to change something — let's talk it through."); }}
        />
    ) : null;
    const belowMessagesNode = planCardNode || matchCards;

    return (
        <FactoryChatShell
            title="Skill Factory"
            subtitle="Describe it, we'll build it"
            icon={SKILL_ICON}
            onClose={onClose}
            messages={messages}
            suggestions={shownSuggestions}
            isLoading={isLoading}
            onSend={handleSend}
            onChipSelect={handleChipClick}
            inputValue={inputValue}
            setInputValue={setInputValue}
            inputPlaceholder={stage === 'confirm' ? 'Describe changes or click Save Skill...' : 'Describe what your skill should do...'}
            hero={hero}
            belowMessages={belowMessagesNode}
            renderAboveInput={aboveInput}
        />
    );
}

const S = {
    heroCard: {
        display: 'flex', flexDirection: 'column', alignItems: 'center', textAlign: 'center',
        padding: '36px 24px 28px', margin: 'auto 0',
    },
    heroIcon: {
        width: '44px', height: '44px', borderRadius: '14px',
        background: 'linear-gradient(135deg, #eef2ff, #e0e7ff)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        color: '#4f46e5', marginBottom: '14px', boxShadow: '0 2px 8px rgba(99,102,241,0.12)',
    },
    heroTitle: { fontSize: '16px', fontWeight: 700, color: '#0f172a', letterSpacing: '-0.01em', marginBottom: '6px' },
    heroSub: { fontSize: '12.5px', color: '#64748b', lineHeight: 1.6, maxWidth: '380px' },
    applyBar: {
        display: 'flex', alignItems: 'center', gap: '12px', padding: '12px 20px',
        borderTop: '1px solid #e2e8f0', background: '#f8fafc', flexShrink: 0,
    },
    applyBtn: (disabled) => ({
        display: 'flex', alignItems: 'center', gap: '6px', padding: '9px 18px',
        background: disabled ? '#c7d2fe' : '#4f46e5', border: 'none', borderRadius: '10px',
        color: '#fff', fontSize: '13px', fontWeight: 600, cursor: disabled ? 'not-allowed' : 'pointer',
        flexShrink: 0, transition: 'all 0.15s', boxShadow: disabled ? 'none' : '0 4px 12px rgba(99,102,241,0.35)',
    }),
    downloadBtn: {
        display: 'flex', alignItems: 'center', gap: '5px', padding: '9px 14px',
        background: '#ffffff', border: '1px solid #e2e8f0', borderRadius: '10px',
        color: '#475569', fontSize: '13px', fontWeight: 600, cursor: 'pointer', flexShrink: 0, transition: 'all 0.15s',
    },
    btnSpinner: {
        width: '12px', height: '12px', borderRadius: '50%', border: '2px solid rgba(255,255,255,0.3)',
        borderTopColor: '#fff', animation: 'spin 0.8s linear infinite', marginRight: '6px',
    },
    matchGroup: { display: 'flex', flexDirection: 'column', gap: '10px', padding: '4px 0 2px' },
    matchCard: {
        display: 'flex', flexDirection: 'column', gap: '6px', padding: '12px 14px',
        background: '#ffffff', border: '1px solid #dbe2ea', borderRadius: '12px', boxShadow: '0 1px 3px rgba(15,23,42,0.05)',
    },
    matchCardTop: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' },
    matchName: { fontSize: '13px', fontWeight: 650, color: '#0f172a', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
    matchBadge: { flexShrink: 0, fontSize: '10.5px', fontWeight: 600, color: '#4f46e5', background: '#eef2ff', borderRadius: '999px', padding: '2px 8px' },
    matchDesc: { fontSize: '12px', color: '#475569', lineHeight: 1.5 },
    matchReason: { fontSize: '11.5px', color: '#64748b', fontStyle: 'italic', lineHeight: 1.45 },
    matchHint: { fontSize: '11.5px', color: '#64748b', lineHeight: 1.5 },
    buildAnywayBtn: {
        alignSelf: 'flex-start', padding: '7px 14px', background: 'transparent',
        border: '1px dashed #cbd5e1', borderRadius: '9px', color: '#475569', fontSize: '12px', fontWeight: 550, cursor: 'pointer',
    },
    previewWrap: {
        borderTop: '1px solid #e2e8f0', background: '#ffffff', flexShrink: 0, maxHeight: '46vh', overflowY: 'auto',
    },
    previewHeader: {
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 20px',
        position: 'sticky', top: 0, background: '#ffffff', zIndex: 1, borderBottom: '1px solid #f1f5f9',
    },
    previewTitle: { fontSize: '12px', fontWeight: 650, color: '#334155' },
    previewToggle: { background: 'transparent', border: 'none', color: '#4f46e5', fontSize: '12px', fontWeight: 600, cursor: 'pointer' },
    previewBody: { padding: '10px 20px 14px' },
    previewTextarea: {
        width: '100%', boxSizing: 'border-box', fontFamily: 'var(--font-family-mono, ui-monospace, monospace)',
        fontSize: '12px', lineHeight: 1.5, color: '#0f172a', border: '1px solid #e2e8f0', borderRadius: '8px',
        padding: '10px 12px', resize: 'vertical', background: '#f8fafc',
    },
    bundleEditGroup: { marginTop: '12px' },
    bundleEditLabel: { fontSize: '11px', fontWeight: 650, color: '#475569', textTransform: 'uppercase', letterSpacing: '0.03em' },
    bundleFileName: {
        fontSize: '11px', color: '#4338ca', fontWeight: 600,
        fontFamily: 'var(--font-family-mono, ui-monospace, monospace)', marginBottom: '4px',
    },
    bundleTextarea: {
        width: '100%', boxSizing: 'border-box', fontFamily: 'var(--font-family-mono, ui-monospace, monospace)',
        fontSize: '11.5px', lineHeight: 1.5, color: '#0f172a', border: '1px solid #e2e8f0', borderRadius: '8px',
        padding: '8px 10px', resize: 'vertical', background: '#f8fafc',
    },
    visibilityGroup: { display: 'inline-flex', borderRadius: '9px', overflow: 'hidden', border: '1px solid #e2e8f0' },
    visibilityBtn: (active) => ({
        padding: '8px 12px', border: 'none', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
        background: active ? '#4f46e5' : '#ffffff', color: active ? '#ffffff' : '#475569',
    }),
};

export default SkillFactoryChat;
