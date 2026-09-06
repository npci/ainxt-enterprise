// SPDX-License-Identifier: MIT
import { useEffect, useMemo, useState } from 'react';

/**
 * TriggerPicker — schedule editor that matches the AiNxt Routines UI pattern.
 *
 * Renders the small "Select a trigger" card with the type-tabs (Once / Hourly
 * / Daily / Weekdays / Weekly / Custom) and the relevant control underneath.
 * Operates on a controlled `schedule` object the parent owns.
 *
 * Times are interpreted in IST (Asia/Kolkata) on the backend. We label the
 * card with "IST" so the user knows the timezone is fixed and matches
 * the screenshots they shared.
 */

const TABS = [
    { id: 'once',     label: 'Once' },
    { id: 'hourly',   label: 'Hourly' },
    { id: 'daily',    label: 'Daily' },
    { id: 'weekdays', label: 'Weekdays' },
    { id: 'weekly',   label: 'Weekly' },
    { id: 'custom',   label: 'Custom' },
];

const DAYS = [
    { id: 'monday',    label: 'Monday' },
    { id: 'tuesday',   label: 'Tuesday' },
    { id: 'wednesday', label: 'Wednesday' },
    { id: 'thursday',  label: 'Thursday' },
    { id: 'friday',    label: 'Friday' },
    { id: 'saturday',  label: 'Saturday' },
    { id: 'sunday',    label: 'Sunday' },
];

function pad(n) {
    return String(n).padStart(2, '0');
}

function formatTime12h(hhmm) {
    if (!hhmm || !hhmm.includes(':')) return '12:00 AM';
    const [h, m] = hhmm.split(':').map((v) => parseInt(v, 10));
    if (Number.isNaN(h) || Number.isNaN(m)) return '12:00 AM';
    const period = h >= 12 ? 'PM' : 'AM';
    const hour12 = h % 12 || 12;
    return `${pad(hour12)}:${pad(m)} ${period}`;
}

function dayLabel(id) {
    return (DAYS.find((d) => d.id === id) || DAYS[0]).label;
}

function defaultOnce() {
    // 30 min in the future, in local IST
    const now = new Date(Date.now() + 30 * 60 * 1000);
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

export function describeSchedule(schedule) {
    if (!schedule || !schedule.type) return 'Not scheduled';
    const t = schedule.type;
    if (t === 'once') {
        if (!schedule.run_at) return 'Runs once';
        try {
            const d = new Date(schedule.run_at);
            const opts = { day: '2-digit', month: 'short', year: 'numeric' };
            return `Runs once on ${d.toLocaleDateString('en-IN', opts)}, ${formatTime12h(`${pad(d.getHours())}:${pad(d.getMinutes())}`)} IST`;
        } catch {
            return `Runs once at ${schedule.run_at}`;
        }
    }
    if (t === 'hourly') {
        return `Runs every hour at minute ${schedule.at_minute ?? 0}`;
    }
    if (t === 'daily') {
        return `Runs daily at ${formatTime12h(schedule.at_time)} IST`;
    }
    if (t === 'weekdays') {
        return `Runs weekdays at ${formatTime12h(schedule.at_time)} IST`;
    }
    if (t === 'weekly') {
        return `Runs every ${dayLabel(schedule.day_of_week)} at ${formatTime12h(schedule.at_time)} IST`;
    }
    if (t === 'custom') {
        return `Runs on cron: ${schedule.cron || '(empty)'}`;
    }
    return 'Not scheduled';
}

function TriggerPicker({ schedule, onChange, onRemove }) {
    const value = schedule || { type: 'daily', at_time: '18:00' };

    // Track display state for the "Once" datetime-local input. Keeping it as
    // string in the schedule itself avoids tz-conversion bugs.
    const localOnceValue = useMemo(() => {
        if (value.type !== 'once') return defaultOnce();
        if (!value.run_at) return defaultOnce();
        // The backend echoes back ISO with seconds — datetime-local wants HH:mm
        const trimmed = value.run_at.replace('Z', '').split('+')[0].split('.')[0];
        // Drop seconds if present
        return trimmed.length >= 16 ? trimmed.slice(0, 16) : trimmed;
    }, [value.type, value.run_at]);

    const handleTab = (id) => {
        if (id === 'once') {
            onChange({ type: 'once', run_at: defaultOnce() });
        } else if (id === 'hourly') {
            onChange({ type: 'hourly', at_minute: 0 });
        } else if (id === 'daily') {
            onChange({ type: 'daily', at_time: '18:00' });
        } else if (id === 'weekdays') {
            onChange({ type: 'weekdays', at_time: '18:00' });
        } else if (id === 'weekly') {
            onChange({ type: 'weekly', at_time: '18:00', day_of_week: 'monday' });
        } else {
            onChange({ type: 'custom', cron: '0 18 * * *' });
        }
    };

    return (
        <div className="trigger-picker">
            <div className="trigger-picker-header">
                <span className="trigger-picker-summary">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                        <circle cx="12" cy="12" r="10" />
                        <polyline points="12 6 12 12 16 14" />
                    </svg>
                    {describeSchedule(value)}
                </span>
                {onRemove && (
                    <button
                        type="button"
                        className="trigger-picker-remove"
                        onClick={onRemove}
                        aria-label="Remove trigger"
                        title="Remove trigger"
                    >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                    </button>
                )}
            </div>

            <div className="trigger-picker-tabs">
                {TABS.map((tab) => (
                    <button
                        key={tab.id}
                        type="button"
                        className={`trigger-picker-tab ${value.type === tab.id ? 'is-active' : ''}`}
                        onClick={() => handleTab(tab.id)}
                    >
                        {tab.label}
                    </button>
                ))}
            </div>

            <div className="trigger-picker-body">
                {value.type === 'once' && (
                    <label className="trigger-picker-field">
                        <span className="trigger-picker-label">Run at</span>
                        <input
                            type="datetime-local"
                            className="trigger-picker-input"
                            value={localOnceValue}
                            onChange={(e) => onChange({ ...value, run_at: e.target.value })}
                        />
                    </label>
                )}

                {value.type === 'hourly' && (
                    <label className="trigger-picker-field">
                        <span className="trigger-picker-label">At minute</span>
                        <input
                            type="number"
                            min="0"
                            max="59"
                            className="trigger-picker-input trigger-picker-input--narrow"
                            value={value.at_minute ?? 0}
                            onChange={(e) => onChange({
                                ...value,
                                at_minute: Math.max(0, Math.min(59, parseInt(e.target.value, 10) || 0)),
                            })}
                        />
                    </label>
                )}

                {(value.type === 'daily' || value.type === 'weekdays') && (
                    <label className="trigger-picker-field">
                        <span className="trigger-picker-label">At</span>
                        <input
                            type="time"
                            className="trigger-picker-input"
                            value={value.at_time || '18:00'}
                            onChange={(e) => onChange({ ...value, at_time: e.target.value })}
                        />
                    </label>
                )}

                {value.type === 'weekly' && (
                    <div className="trigger-picker-fields-row">
                        <label className="trigger-picker-field">
                            <span className="trigger-picker-label">At</span>
                            <input
                                type="time"
                                className="trigger-picker-input"
                                value={value.at_time || '18:00'}
                                onChange={(e) => onChange({ ...value, at_time: e.target.value })}
                            />
                        </label>
                        <label className="trigger-picker-field">
                            <span className="trigger-picker-label">On</span>
                            <select
                                className="trigger-picker-input"
                                value={value.day_of_week || 'monday'}
                                onChange={(e) => onChange({ ...value, day_of_week: e.target.value })}
                            >
                                {DAYS.map((d) => (
                                    <option key={d.id} value={d.id}>{d.label}</option>
                                ))}
                            </select>
                        </label>
                    </div>
                )}

                {value.type === 'custom' && (
                    <label className="trigger-picker-field">
                        <span className="trigger-picker-label">Cron expression (IST)</span>
                        <input
                            type="text"
                            className="trigger-picker-input"
                            placeholder="m h dom mon dow — e.g. 0 18 * * 1-5"
                            value={value.cron || ''}
                            onChange={(e) => onChange({ ...value, cron: e.target.value })}
                        />
                    </label>
                )}

                <p className="trigger-picker-hint">
                    Runs are staggered by a few minutes to spread server load.
                </p>
            </div>
        </div>
    );
}

export default TriggerPicker;
