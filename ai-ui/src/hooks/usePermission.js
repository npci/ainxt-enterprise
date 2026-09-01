// SPDX-License-Identifier: Apache-2.0
// usePermission — AD-level based RBAC hook
// ad_level: 0=most senior exec, 6=junior engineer
// Replaces old ROLE_LEVEL map (viewer/developer/operator/security/admin)

export function usePermission(user) {
  const adLevel    = user?.ad_level ?? 6;
  const isAdmin    = user?.role === "admin";
  const canApprove = user?.can_approve === true || isAdmin;
  const department = user?.department || "";

  // Any authenticated user can view; "developer" (maker actions on their own
  // items) is likewise granted to any authenticated user — least-privilege
  // gating for admin/approver-only controls happens via isAdmin/canApprove
  // below, not here.
  const isViewer    = !!user;
  const isDeveloper = !!user;
  const isOperator  = canApprove;
  const isSecurity  = isAdmin;
  const isC1Plus    = canApprove;

  // Legacy string-keyed shim for gradual caller migration. Known perm
  // strings mirror the real flags above; unknown perms fall back to
  // isAdmin (mirror-only — the server remains the real enforcement gate).
  const PERM_MAP = {
    admin:     isAdmin,
    security:  isSecurity,
    developer: isDeveloper,
    operator:  isOperator,
    viewer:    isViewer,
  };
  const can = (perm) => (perm in PERM_MAP ? PERM_MAP[perm] : isAdmin);

  return {
    canApprove,
    isAdmin,
    adLevel,
    department,
    can,
    isViewer,
    isDeveloper,
    isOperator,
    isSecurity,
    isC1Plus,
    level:       adLevel,
    role:        user?.role ?? "user",
  };
}
