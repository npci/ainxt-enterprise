// SPDX-License-Identifier: MIT
// Copyright 2026 National Payments Corporation of India.
// lib/schedule.js — scheduling math shared by the side panel (sidepanel.js) and
// the service worker (background.js). Pure functions over a schedule object
// { frequency:"once"|"daily"|"weekly"|"monthly", date:"YYYY-MM-DD", time:"HH:MM" (24h) }.
// Times are interpreted in the machine's local timezone (chrome.alarms fires on
// wall-clock time), so nextRun is an absolute epoch-ms instant.

export function scheduleToDate(schedule) {
  const [y, m, d] = String(schedule.date || "").split("-").map(Number);
  const [hh, mm] = String(schedule.time || "").split(":").map(Number);
  return new Date(y, (m || 1) - 1, d || 1, hh || 0, mm || 0, 0, 0);
}

export function advance(date, frequency) {
  const d = new Date(date.getTime());
  if (frequency === "daily") d.setDate(d.getDate() + 1);
  else if (frequency === "weekly") d.setDate(d.getDate() + 7);
  else if (frequency === "monthly") d.setMonth(d.getMonth() + 1);
  return d;
}

// Next fire instant (epoch ms), or null if the schedule is incomplete.
// "once" returns the exact datetime (may be in the past — the caller decides
// whether that means "fire now"); recurring rolls forward past `fromMs`.
export function computeNextRun(schedule, fromMs = Date.now()) {
  if (!schedule || !schedule.date || !schedule.time) return null;
  let d = scheduleToDate(schedule);
  if (Number.isNaN(d.getTime())) return null;
  if (schedule.frequency === "once") return d.getTime();
  let guard = 0;
  while (d.getTime() <= fromMs && guard < 6000) {
    d = advance(d, schedule.frequency);
    guard++;
  }
  return d.getTime();
}

// 12-hour clock (hour 1–12 + "AM"/"PM") → 24-hour hour (0–23).
export function to24h(hour12, ampm) {
  let h = Number(hour12) % 12;
  if (ampm === "PM") h += 12;
  return h;
}

// 24-hour hour (0–23) → { hour: 1–12, ampm: "AM"|"PM" }.
export function to12h(h24) {
  const ampm = h24 >= 12 ? "PM" : "AM";
  let h = h24 % 12;
  if (h === 0) h = 12;
  return { hour: h, ampm };
}
