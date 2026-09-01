// SPDX-License-Identifier: Apache-2.0
// ── Shared "sort by utilisation" logic for the budget list panes ──
// Utilisation is ranked on absolute cost spent (USD). Users without a
// cost cap (uncapped or no budget row) are always grouped after capped
// users, and ordered among themselves by dollars spent.

export const SORT_DESC = "desc";
export const SORT_ASC  = "asc";

export function toggleSortDirection(direction) {
  return direction === SORT_ASC ? SORT_DESC : SORT_ASC;
}

// Dollars spent all-time — the same figure each row renders.
function costSpent(row) {
  return Number(row?.usage_total?.cost_usd_spent) || 0;
}

// A row is "capped" when it has a budget with a positive cost ceiling.
// `has_budget` is only sent by /budget/team; treat its absence as capped.
function hasCostCap(row) {
  return row?.has_budget !== false && (Number(row?.max_cost_usd_total) || 0) > 0;
}

// Stable tie-break so equal spenders never reshuffle between renders.
function rowLabel(row) {
  return String(row?.display_name || row?.email || row?.user_id || "").toLowerCase();
}

/**
 * Returns a new array sorted by utilisation. Capped users come first in
 * both directions; `direction` orders spend within each group.
 */
export function sortByUtilisation(rows, direction = SORT_DESC) {
  if (!Array.isArray(rows)) return [];
  const dir = direction === SORT_ASC ? 1 : -1;
  return [...rows].sort((a, b) => {
    const capA = hasCostCap(a);
    const capB = hasCostCap(b);
    if (capA !== capB) return capA ? -1 : 1;
    const bySpend = (costSpent(a) - costSpent(b)) * dir;
    if (bySpend !== 0) return bySpend;
    return rowLabel(a).localeCompare(rowLabel(b));
  });
}
