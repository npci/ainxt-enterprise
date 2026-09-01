// SPDX-License-Identifier: Apache-2.0
import { API_BASE as API } from "../../config";

// ── Utilization endpoint URL builders ────────────────────────────────────────
// Each returns a function (dimension) => url so a single UtilizationPie/Page can
// serve user / me / team breakdowns. Kept in a plain module (no components) so
// Vite fast-refresh stays happy.
export const utilizationEndpoints = {
  user: (userId) => (dimension) =>
    `${API}/budget/users/${encodeURIComponent(userId)}/utilization?dimension=${dimension}`,
  me: () => (dimension) =>
    `${API}/budget/me/utilization?dimension=${dimension}`,
  team: () => (dimension) =>
    `${API}/budget/team/utilization?dimension=${dimension}`,
};
