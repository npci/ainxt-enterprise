// SPDX-License-Identifier: MIT
import { useEffect, useState } from "react";
import {
  MessageSquare,
  Bot,
  Database,
  FolderKanban,
  Bell,
  DollarSign,
  LogOut,
  BookOpen,
  Layers,
  BarChart2,
  Activity,
  FlaskConical,
  UserCircle,
  ChevronLeft,
  Shield,
  Mail,
  Hammer,
  Globe,
  Plug,
  Cpu,
  Server,
  Briefcase,
  Lightbulb,
  Brain,
  Target,
  MessagesSquare,
  BookMarked,
} from "lucide-react";

import BrandMark from "./BrandMark";
import BrandWordmark from "./BrandWordmark";
import { API_BASE, authFetch } from '../config';

const SDLC_ACTIVE_STATES = new Set([
  "CREATED","CLASSIFYING","TICKET_NORMALIZATION","ANALYZING","DESIGNING",
  "DIAGNOSING","MANIFEST_VALIDATION","PRE_CODING_BUILD",
  "TRIAGING","TROUBLESHOOTING","SOLUTIONING",
  "CODING","REVIEWING","REVIEW_GATE","FIXING",
  "TESTING","SLT_RUNNING","COMMITTING",
  "AWAITING_CODE_APPROVAL","AWAITING_DESIGN_APPROVAL","AWAITING_SOLUTION_APPROVAL",
  "AWAITING_USER_INPUT","AWAITING_PR_APPROVAL","AWAITING_RE_REVIEW","MERGE_READY",
]);

// Section labels shown above each group (null = no label)
const GROUP_LABELS = [null, "Workspace", "Collaborate", "Build", "Observe", "Admin", "Settings", "Initiatives", null];

export default function Sidebar({ view, setView, user, onLogout, unreadCount = 0, collapsed = false, onToggleCollapse }) {

  // ── UI feature flags from backend (no auth required) ──────
  const [uiConfig, setUiConfig] = useState({
    enable_coach:       false,
    enable_discussions: false,
    hod_approval_enabled: false,
  });
  useEffect(() => {
    fetch(`${API_BASE}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setUiConfig(d); })
      .catch(() => {});
  }, []);

  const [sdlcActive, setSdlcActive] = useState(false);

  // ── SDLC active-run heartbeat — 30s ───────────────────────
  useEffect(() => {
    const poll = () => {
      fetch(`${API_BASE}/sdlc/stats`)
        .then(r => r.json())
        .then(d => {
          const byState = d.by_state || {};
          const active  = Object.entries(byState).some(
            ([state, count]) => count > 0 && SDLC_ACTIVE_STATES.has(state)
          );
          setSdlcActive(active);
        })
        .catch(() => {});
    };
    poll();
    const interval = setInterval(poll, 30000);
    return () => clearInterval(interval);
  }, []);

  // ── Budget data for sidebar widget — poll every 60s ───────
  const [budgetData, setBudgetData] = useState(null);

  useEffect(() => {
    if (!user) return;
    const uid = user.userId || user.email || "";
    if (!uid) return;
    const poll = () => {
      authFetch(`${API_BASE}/budget/me`, { headers: { "X-User-Id": uid } })
        .then(r => r.ok ? r.json() : null)
        .then(d => d && setBudgetData(d))
        .catch(() => {});
    };
    poll();                                    // fire once immediately
    const interval = setInterval(poll, 60000); // then every 60 seconds
    return () => clearInterval(interval);      // cleanup on unmount
  }, [user]);

  // ── AD-level based visibility ─────────────────────────────
  const adLevel = user?.ad_level ?? 6;
  const isAdmin = user?.role === "admin";
  const canSee  = (maxLevel) => adLevel <= maxLevel || isAdmin;

  // ── Email-broadcast allowlist probe (server reads BROADCAST_ALLOWED_EMAILS).
  // null = not yet known; true/false once /broadcast/access responds.
  const [broadcastAllowed, setBroadcastAllowed] = useState(null);
  useEffect(() => {
    let cancelled = false;
    authFetch(`${API_BASE}/broadcast/access`)
      .then(r => r.ok ? r.json() : { allowed: false })
      .then(d => !cancelled && setBroadcastAllowed(!!d.allowed))
      .catch(() => !cancelled && setBroadcastAllowed(false));
    return () => { cancelled = true; };
  }, []);

  // ── Nav config ────────────────────────────────────────────
  const navGroups = [
    [
      { view: "chat",      icon: MessageSquare, label: "Chat",           maxLevel: 6 },
      { view: "office",    icon: Briefcase,     label: "Buddy",         maxLevel: 6, desktopOnly: true ,beta:true},
      { view: "cowork",    icon: Cpu,           label: "Code",           maxLevel: 6, desktopOnly: true ,beta:true},
      // { view: "agents",    icon: Bot,           label: "Agents",         maxLevel: 0 },
      { view: "knowledge", icon: BookOpen,      label: "Knowledge Base", maxLevel: 6 },
      { view: "coach",     icon: Target,        label: "AiNxt Coach",    maxLevel: 6, beta:true,
        hidden: !uiConfig.enable_coach },
    ],
    [
      // Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default): no
      // real department/HOD hierarchy exists to route product approvals
      // through, so the product catalog is admin-only — see
      // routers/products_router.py's HOD_APPROVAL_ENABLED-aware guards.
      { view: "products",  icon: FolderKanban,  label: "Products",       maxLevel: 6,
        hidden: !uiConfig.hod_approval_enabled && !isAdmin },
      { view: "codebase",  icon: Database,      label: "Codebase",       maxLevel: 6 },
      { view: "codewiki",  icon: BookMarked,    label: "CodeWiki",       maxLevel: 6 },
      { view: "projects",  icon: FolderKanban,  label: "My Workspace",   maxLevel: 6 },
    ],
    [
      { view: "discussions", icon: MessagesSquare, label: "Discussions", maxLevel: 6,
        hidden: !uiConfig.enable_discussions },
      { view: "inbox",     icon: Bell,          label: "Inbox",          maxLevel: 6, badge: unreadCount },
      { view: "sdlc",      icon: Layers,        label: "SDLC Pipeline",  maxLevel: 6, pulse: sdlcActive, beta:true },
    ],
    [
      { view: "build-studio", icon: Hammer, label: "Agent Studio", maxLevel: 6,beta:true },
    ],
    [
      // Observe — admin-only. maxLevel: -1 is unreachable by any ad_level
      // (0-6), so canSee() only returns true via the isAdmin bypass.
      { view: "monitoring",       icon: Activity,     label: "Monitoring",       maxLevel: -1 },
      { view: "analytics",        icon: BarChart2,    label: "Analytics",        maxLevel: -1 },
      { view: "evals",            icon: FlaskConical, label: "Eval Observatory", maxLevel: -1 },

    ],
    [
      { view: "llm-provider-config", icon: Server,   label: "LLM Providers",   maxLevel: 0 },
      { view: "model-governance",  icon: Shield,      label: "Model Governance",    maxLevel: 1 },
      // { view: "skill-proposals", icon: Lightbulb,   label: "Skill Proposals", maxLevel: 3 },
      { view: "endpoint-manager", icon: Globe,       label: "Endpoints",       maxLevel: 0 },
      { view: "budget",          icon: DollarSign,  label: "Budget",          maxLevel: 6 },
      // Flat/admin-only mode (HOD_APPROVAL_ENABLED=false, the default): no
      // real department/seniority hierarchy exists, so only admins manage
      // level overrides — the ad_level<=2 (Director+) path only applies in
      // HOD-hierarchy mode. See routers/auth_router.py::_require_director.
      { view: "level-overrides", icon: UserCircle,  label: "Level Overrides", maxLevel: 2,
        hidden: !uiConfig.hod_approval_enabled && !isAdmin },
      { view: "broadcast",        icon: Mail,        label: "Email Broadcast", maxLevel: 0, hidden: broadcastAllowed !== true },
    ],
    [
      // Settings — configuration that powers the products (e.g. Connectors is
      // how Buddy reaches Outlook/Teams/Jira). Not a product surface itself.
      { view: "memory",      icon: Brain,       label: "Memory",      maxLevel: 0,beta:true },
      { view: "connectors",  icon: Plug,        label: "Connectors",  maxLevel: 6, beta:true },
      { view: "cowork-setup", icon: Briefcase,  label: "Buddy Setup", maxLevel: 6, desktopOnly: true,beta:true },
    ],
    [
      { view: "docs", icon: BookOpen, label: "Docs", maxLevel: 6 },
    ],
  ];

  const initials = (user?.name || user?.email || "U").charAt(0).toUpperCase();

  // ── UI ────────────────────────────────────────────────────
  return (
    <div className={`
      ${collapsed ? "w-14" : "w-56"}
      bg-white border-r border-gray-100
      flex flex-col h-full flex-shrink-0
      transition-all duration-200
    `}>

      {/* ── LOGO + COLLAPSE ───────────────────────────────── */}
      <div className={`
        border-b border-gray-100 flex-shrink-0 flex items-center
        ${collapsed ? "justify-center px-2 py-3" : "justify-between px-3 py-3"}
      `}>
        {!collapsed && (
          <div className="flex items-center gap-2">
            <div className="w-5 h-5 flex items-center justify-center flex-shrink-0">
              <BrandMark plated className="w-5 h-5 shadow-sm" />
            </div>
            <div className="flex items-baseline gap-1.5 leading-none">
              <BrandWordmark className="h-3.5" alt="AiNxt Enterprise" />
              <span className="text-[9px] text-indigo-700 font-bold tracking-widest uppercase">Enterprise</span>
              <span className="text-[9px] text-gray-900 tracking-tight font-semibold">v1.0</span>
            </div>
          </div>
        )}
        {collapsed && (
          <button
            onClick={onToggleCollapse}
            title="Expand sidebar"
            className="w-7 h-7 flex items-center justify-center cursor-pointer hover:opacity-80 transition"
          >
            <BrandMark plated className="w-7 h-7 shadow-sm" />
          </button>
        )}
        {!collapsed && (
          <button
            onClick={onToggleCollapse}
            title="Collapse sidebar"
            className="p-1 rounded-lg hover:bg-gray-100 text-gray-400 hover:text-gray-600 transition cursor-pointer flex-shrink-0"
          >
            <ChevronLeft size={13} />
          </button>
        )}
      </div>

      {/* ── NAV GROUPS ────────────────────────────────────── */}
      <div className="flex-1 py-2 overflow-y-auto overflow-x-hidden">
        {navGroups.map((group, gi) => {
          const _isDesktop = typeof window !== "undefined" && !!window.ainxtDesktop?.isDesktop;
          const visible = group.filter(item =>
            canSee(item.maxLevel)
              && !item.hidden
            && (!item.desktopOnly || _isDesktop));
          if (!visible.length) return null;
          const label = GROUP_LABELS[gi];

          return (
            <div key={gi} className={gi > 0 ? "mt-1" : ""}>

              {/* Section label — hidden when collapsed */}
              {label && !collapsed && (
                <div className="px-3 pt-2.5 pb-1">
                  <span className="text-[9.5px] font-bold text-gray-500 tracking-widest uppercase">
                    {label}
                  </span>
                </div>
              )}
              {label && !collapsed && gi > 0 && (
                <div className="mx-3 mb-1 border-t border-gray-100" />
              )}
              {/* Collapsed: just a faint separator between groups */}
              {collapsed && gi > 0 && (
                <div className="mx-3 my-1 border-t border-gray-100" />
              )}

              {visible.map(({ view: v, icon: Icon, label: itemLabel, badge, pulse,beta }) => {
                const isActive = view === v;
                return (
                  <button
                    key={v}
                    onClick={() => setView(v)}
                    title={collapsed ? itemLabel : undefined}
                    className={`
                      w-[calc(100%-8px)] mx-1 flex items-center
                      ${collapsed ? "justify-center px-0 py-2" : "gap-2.5 px-2.5 py-[7px]"}
                      text-sm rounded-lg transition-all duration-150 cursor-pointer relative
                      ${isActive
                        ? "bg-indigo-50 text-indigo-800 font-semibold"
                        : "text-gray-600 hover:text-gray-900 hover:bg-gray-50"}
                    `}
                  >
                    <Icon
                      size={14}
                      className={`flex-shrink-0 transition-colors duration-150 ${isActive ? "text-indigo-600" : "text-gray-400 group-hover:text-gray-600"}`}
                    />
                    {!collapsed && (
                      <span className="flex-1 text-left text-[12.5px] truncate">{itemLabel}</span>
                    )}

                    {/* Beta badge — flat, square, no shadow/border, "Beta" label */}
                    {beta && !collapsed && (
                      <span
                        className="inline-flex items-center justify-center flex-shrink-0
                          rounded-[3px] px-[5px] py-[2px] select-none"
                      >
                        <span style={{
                          display: "block",
                          fontSize: "9px",
                          fontWeight: 600,
                          letterSpacing: "0.02em",
                          lineHeight: "10px",
                          color: "rgb(139,92,246)",
                        }}>
                          Beta
                        </span>
                      </span>
                    )}
                    {beta && collapsed && (
                      <span className="absolute bottom-1 right-1 w-[6px] h-[6px] rounded-[1px]
                        bg-violet-400"
                      />
                    )}

                    {/* SDLC live pulse */}
                    {pulse && (
                      <span className={`relative flex h-2 w-2 flex-shrink-0 ${collapsed ? "absolute top-1 right-1" : ""}`}>
                        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                        <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
                      </span>
                    )}

                    {/* Inbox badge */}
                    {badge > 0 && !collapsed && (
                      <span className="bg-amber-500 text-white text-[10px] font-semibold rounded-full px-1.5 min-w-[16px] text-center leading-4">
                        {badge > 99 ? "99+" : badge}
                      </span>
                    )}
                    {badge > 0 && collapsed && (
                      <span className="absolute top-1 right-1 w-2 h-2 bg-amber-500 rounded-full" />
                    )}

                    {/* Active left-edge accent */}
                    {isActive && (
                      <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-[60%] bg-indigo-500 rounded-r-full" />
                    )}
                  </button>
                );
              })}
            </div>
          );
        })}
      </div>

      {/* ── USER FOOTER ───────────────────────────────────── */}
      {user && (
        <div className="border-t border-gray-100 px-2 py-2.5 flex-shrink-0">
          {collapsed ? (
            <div className="flex flex-col items-center gap-1.5">
              <div className="w-6 h-6 rounded-full brand-grad-vivid flex items-center justify-center shadow-sm">
                <span className="text-white text-[10px] font-bold">{initials}</span>
              </div>
              <button
                onClick={onLogout}
                title="Sign out"
                className="p-1 hover:bg-gray-100 rounded-lg text-gray-400 hover:text-gray-600 transition cursor-pointer"
              >
                <LogOut size={12} />
              </button>
            </div>
          ) : (
            <div>
            <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-full brand-grad-vivid flex items-center justify-center flex-shrink-0 shadow-sm">
                <span className="text-white text-[11px] font-bold">{initials}</span>
              </div>
              <div className="flex-1 min-w-0">
                <div className="text-[11.5px] font-semibold text-gray-800 truncate leading-tight">
                  {user.name || user.email}
                </div>
                <div className="flex items-center gap-1 mt-0.5">
                  {user.role === "admin" && (
                      <span className="text-[9px] font-bold text-indigo-700 bg-indigo-50 px-1 rounded">ADMIN</span>
                  )}
                  <span className="text-[10px] text-gray-400 truncate capitalize">
                    {user.department || user.role}
                  </span>
                </div>
              </div>
              <button
                onClick={() => setView("profile")}
                title="My Profile"
                className={`p-1 rounded-lg transition cursor-pointer flex-shrink-0 ${
                  view === "profile"
                    ? "bg-indigo-50 text-indigo-600"
                    : "text-gray-400 hover:text-gray-600 hover:bg-gray-100"
                }`}
              >
                <UserCircle size={13} />
              </button>
              <button
                onClick={onLogout}
                title="Sign out"
                className="p-1 hover:bg-gray-100 rounded-lg text-gray-400 hover:text-gray-600 transition cursor-pointer flex-shrink-0"
              >
                <LogOut size={13} />
              </button>
              </div>

              {/* Budget — speedometer gauge with needle */}
              {budgetData?.budget && (() => {
                // for testing commented below
                const total     = budgetData.budget?.max_cost_usd_total || 0;
                const spent     = budgetData.usage_total?.cost_usd_spent || 0;
                const remaining = budgetData.remaining_usd ?? Math.max(0, total - spent);
                const pct       = total > 0 ? Math.min(100, Math.round((spent / total) * 100)) : 0;

                // for testing hardcoded below
                // const total     =  100;
                // const spent     =  50;
                // const remaining =   Math.max(0, total - spent);
                // const pct       =  Math.min(100, Math.round((spent / total) * 100));

                // Use a full circle (r=16) but only show top half via clipPath
                // circumference = 2πr = ~100.5 — convenient for % math
                const r = 16, cx = 20, cy = 20;
                const circ = 2 * Math.PI * r;          // ≈ 100.53
                const half = circ / 2;                  // half arc length = top semicircle

                // strokeDasharray trick: draw only the top semicircle
                // green: 0–70% of half, amber: 70–90%, red: 90–100%
                const green = half * 0.70;
                const amber = half * 0.20;
                const red   = half * 0.10;

                // Needle: rotates from -180deg (left=0%) to 0deg (right=100%)
                const needleDeg   = -180 + (pct / 100) * 180;
                const needleRad   = (needleDeg * Math.PI) / 180;
                const needleLen   = r - 3;
                const nx          = cx + needleLen * Math.cos(needleRad);
                const ny          = cy + needleLen * Math.sin(needleRad);
                const needleColor = pct >= 90 ? "#f43f5e" : pct >= 70 ? "#f59e0b" : "#10b981";
                const textColor   = pct >= 90 ? "text-rose-500" : pct >= 70 ? "text-amber-500" : "text-emerald-500";

                // strokeDashoffset: circle starts at 3 o'clock; rotate -90° via transform
                // to start at 9 o'clock (left). Each segment offset = previous lengths.
                const segProps = (len, offset, color) => ({
                  fill: "none", stroke: color, strokeWidth: 4,
                  strokeDasharray: `${len} ${circ - len}`,
                  strokeDashoffset: -offset,
                  strokeLinecap: "butt",
                });

                return (
                  <div className="mt-2 rounded-md overflow-hidden border border-gray-100 shadow-sm bg-white">
                    <div className="flex items-center gap-2.5 px-2 py-1.5">

                      {/* Speedometer SVG */}
                      <svg width="40" height="24" viewBox="0 0 40 24" className="flex-shrink-0">
                        <g transform={`rotate(-180, ${cx}, ${cy})`}>
                          <circle cx={cx} cy={cy} r={r}
                            fill="none" stroke="#e0e7ff" strokeWidth="4"
                            strokeDasharray={`${half} ${half}`} strokeDashoffset="0"
                          />
                          <circle cx={cx} cy={cy} r={r} {...segProps(green, 0, "#10b981")} />
                          <circle cx={cx} cy={cy} r={r} {...segProps(amber, green, "#f59e0b")} />
                          <circle cx={cx} cy={cy} r={r} {...segProps(red, green + amber, "#f43f5e")} />
                        </g>
                        <line x1={cx} y1={cy} x2={nx.toFixed(2)} y2={ny.toFixed(2)}
                          stroke="#4338ca" strokeWidth="1.8" strokeLinecap="round" />
                        <circle cx={cx} cy={cy} r="2.5" fill="#4338ca" />
                        <circle cx={cx} cy={cy} r="1" fill="white" />
                      </svg>

                      {/* Divider */}
                      <div className="w-px self-stretch bg-gray-100 flex-shrink-0" />

                      {/* Numbers */}
                      <div className="flex flex-col min-w-0 flex-1 gap-0.5">

                        {/* Row 2 — spent amount prominent */}
                        <div className="flex items-baseline gap-1">
                          <span className="text-[13px] font-black text-gray-600 leading-none">${spent.toFixed(2)}</span>
                          <span className="text-[9px] text-gray-400 font-medium">/ ${total.toFixed(2)}</span>
                        </div>

                        {/* Row 3 — remaining with colored dot */}
                        <div className="flex items-center gap-1">
                          <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: needleColor }} />
                          <span className="text-[10px] font-semibold font-medium" style={{ color: needleColor }}>${remaining.toFixed(2)} remaining</span>
                        </div>

                      </div>
                    </div>
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      )}

    </div>
  );
}
