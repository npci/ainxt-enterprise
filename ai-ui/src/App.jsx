// SPDX-License-Identifier: Apache-2.0
import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation, Routes, Route, Navigate } from "react-router-dom";
import { API_BASE as API, PORTAL_BASE, authFetch, apiFetch } from "./config";
import { decryptPii } from "./utils/piiCrypto";
import { coworkOfficeClearKey } from "./hooks/useDesktop.js";
import Login from "./components/Login.jsx";
import Sidebar from "./components/Sidebar.jsx";
import Chat from "./components/Chat.jsx";
import DocsPanel from "./components/DocsPanel.jsx";
import AgentsCatalog from "./components/AgentsCatalog.jsx";
import BuildStudio from "./components/BuildStudio.jsx";
import CodebaseManager from "./components/CodebaseManager.jsx";
import Projects from "./components/Projects.jsx";
import Discussions from "./components/Discussions.jsx";
import Inbox from "./components/Inbox.jsx";
import BudgetManager from "./components/BudgetManager.jsx";
import SDLCPipeline from "./components/SDLCPipeline.jsx";
import AgentAnalytics from "./components/AgentAnalytics.jsx";
import Monitoring from "./components/Monitoring.jsx";
import EvalsDashboard from "./components/EvalsDashboard.jsx";
import Coach from "./components/Coach.jsx";
import ModelGovernance from "./components/ModelGovernance.jsx";
import DeptMetrics from "./components/DeptMetrics.jsx";
import KnowledgeBase from "./components/KnowledgeBase.jsx";
import KnowledgeGraph from "./components/KnowledgeGraph.jsx";
import Connectors from "./components/Connectors.jsx";
import CodeWikiDocs from "./components/CodeWikiDocs.jsx";
import CoworkSettings from "./components/CoworkSettings.jsx";
import TeamsConfig from "./components/TeamsConfig.jsx";
import Code from "./components/Code.jsx";
import Office from "./components/Office.jsx";
import Profile from "./components/Profile.jsx";
import ProductManager from "./components/ProductManager.jsx";
import LevelOverrides from "./components/LevelOverrides.jsx";
import SkillProposals from "./components/SkillProposals.jsx";
import Memory from "./components/Memory.jsx";
import EmailBroadcast from "./components/EmailBroadcast.jsx";
import EndpointManager from "./components/EndpointManager.jsx";
import ErrorBoundary from "./components/ErrorBoundary.jsx";
import { ToastProvider, ConfirmProvider } from "./components/ui/DialogProvider.jsx";

// Map URL pathnames to view keys
const PATH_TO_VIEW = {
  "/chat":              "chat",          // Chat at root - works with all nginx configs
  "/agents":           "agents",
  "/knowledge":        "knowledge",
  "/graph":            "graph",
  "/products":         "products",
  "/codebase":         "codebase",
  "/codewiki":         "codewiki",
  "/projects":         "projects",
  "/inbox":            "inbox",
  "/sdlc":             "sdlc",
  "/build-studio":     "build-studio",
  "/monitoring":       "monitoring",
  "/analytics":        "analytics",
  "/evals":            "evals",
  "/model-governance": "model-governance",
  "/skill-proposals":  "skill-proposals",
  "/budget-manager":    "budget",
  "/level-overrides":  "level-overrides",
  "/broadcast":        "broadcast",
  "/endpoint-manager": "endpoint-manager",
  "/dept-metrics":     "dept-metrics",
  "/memory":           "memory",
  "/connectors":       "connectors",
  "/cowork-setup":     "cowork-setup",
  "/office":           "office",
  "/code":             "cowork",
  "/documents":         "docs",
  "/profile":          "profile",
  "/coach":             "coach",
  "/discussions":       "discussions",
};

// Map view keys to URL pathnames (use root for chat)
const VIEW_TO_PATH = {
  ...Object.fromEntries(
    Object.entries(PATH_TO_VIEW).map(([path, view]) => [view, path])
  ),
  "chat": "/chat",  // Navigate to root for chat
};


// ─────────────────────────────────────────────────────────────

function stripSystemPrefix(content) {
  if (!content) return content;
  return content.replace(/^\[(STYLE INSTRUCTION|CONTEXT):[^\]]*\]\n\n?/g, "").trimStart();
}

export default function App() {

  const navigate  = useNavigate();
  const location  = useLocation();

  // Derive the current view from the URL path; fall back to "chat"
  // const view = PATH_TO_VIEW[location.pathname] ?? "chat";
  const path = location.pathname.replace( /^\/portal/, "" ) || "/";
  const view = PATH_TO_VIEW[ path ] ?? "chat";

  // Navigate to the view's URL when the sidebar (or any caller) calls setView
  function setView(v) {
    const path = VIEW_TO_PATH[v] ?? "/";
    if (location.pathname !== path) navigate(path);
  }

  // ── Auth state — source of truth is /auth/me (httpOnly cookie) ──
  const [user, setUser]         = useState(null);
  const [authChecked, setAuthChecked] = useState(false);

  // ── Feature flags — probed once after session is confirmed ──
  // null = not yet known (hide until confirmed); true = enabled; false = disabled

  // Guard against React StrictMode double-invoking the mount effect in dev.
  // StrictMode mounts → unmounts → remounts every component to surface side
  // effects. Without this ref, beginSso() fires twice → two browser tabs open.
  const _ssoStarted = useRef(false);

  // PII payload encryption flag (core/pii_crypto.py), fetched once from the
  // unauthenticated /auth/ui-config endpoint — used to decrypt "pii:v1:"
  // email/name fields returned by /auth/me below.
  const _piiEnabledPromise = useRef(
    apiFetch(`${API}/auth/ui-config`)
      .then(r => r.ok ? r.json() : null)
      .then(d => !!d?.pii_payload_encryption_enabled)
      .catch(() => false)
  );

  // On mount: restore session by calling /auth/me — never localStorage
  useEffect(() => {
    // ── Desktop SSO auto-login (Electron only) ──────────────────────────────
    // window.ainxtDesktop is injected by preload.js via contextBridge and is
    // undefined in every browser context — this entire block is unreachable on
    // web, so Login.jsx, the browser SSO callback, and the Office add-in are
    // completely untouched.
    if (window.ainxtDesktop?.isDesktop) {
      if (_ssoStarted.current) return; // StrictMode guard — only run once
      _ssoStarted.current = true;
      (async () => {
        try {
          // main.js ran silentRelogin() before loadURL and injected the cookie
          // into Chromium's jar. On every launch after the first, /auth/me
          // returns 200 here with no SSO interaction at all.
          const meRes = await authFetch(`${API}/auth/me`);
          if (meRes.ok) {
            const data = await meRes.json();
            const _piiOn = await _piiEnabledPromise.current;
            data.email = await decryptPii(data.email, _piiOn);
            data.name  = await decryptPii(data.name,  _piiOn);
            setUser({
              userId:          data.id,
              email:           data.email,
              name:            data.name,
              role:            data.role,
              ad_level:        data.ad_level ?? 6,
              department:      data.department ?? "",
              can_approve:     data.can_approve ?? false,
              is_hod:          data.is_hod ?? false,
              hod_departments: data.hod_departments ?? [],
              is_reporting_manager: data.is_reporting_manager ?? false,
              ad_username:     data.ad_username ?? "",
              employee_id:     data.employee_id ?? "",
            });
            setAuthChecked(true);
            return; // already authenticated — Login.jsx never renders
          }

          // /auth/me failed → no cookie → first run or token expired.
          // beginSso() opens the system browser → Microsoft login → loopback
          // redirect → main.js exchanges the code → _persistDesktopLogin()
          // injects the auth_token cookie AND writes the API key for the CLI.
          // On an Entra-joined device (AzureAdPrt=YES) this is zero-click.
          const sso = await window.ainxtDesktop.beginSso();
          if (sso?.ok) {
            const me2 = await authFetch(`${API}/auth/me`);
            if (me2.ok) {
              const data = await me2.json();
              const _piiOn = await _piiEnabledPromise.current;
              data.email = await decryptPii(data.email, _piiOn);
              data.name  = await decryptPii(data.name,  _piiOn);
              setUser({
                userId:          data.id,
                email:           data.email,
                name:            data.name,
                role:            data.role,
                ad_level:        data.ad_level ?? 6,
                department:      data.department ?? "",
                can_approve:     data.can_approve ?? false,
                is_hod:          data.is_hod ?? false,
                hod_departments: data.hod_departments ?? [],
                is_reporting_manager: data.is_reporting_manager ?? false,
                ad_username:     data.ad_username ?? "",
                employee_id:     data.employee_id ?? "",
              });
              setAuthChecked(true);
              return; // SSO succeeded — Login.jsx never renders
            }
          }
          // SSO failed (Azure not configured → 400, user not registered → 403,
          // network issue, browser tab closed, etc.) — fall through so
          // Login.jsx renders as the fallback, exactly as before.
        } catch { /* fall through to Login.jsx */ }
        // Always reach here on any failure path — show Login.jsx.
        setAuthChecked(true);
      })();
      return; // async block above owns setAuthChecked — skip the sync path below
    }

    // ── [EXISTING, UNCHANGED] Cookie-based session restore (web + non-desktop) ──
    authFetch(`${API}/auth/me`)
      .then(r => r.ok ? r.json() : null)
      .then(async data => {
        if (data) {
          const _piiOn = await _piiEnabledPromise.current;
          data.email = await decryptPii(data.email, _piiOn);
          data.name  = await decryptPii(data.name,  _piiOn);
          setUser({
            userId:          data.id,
            email:           data.email,
            name:            data.name,
            role:            data.role,
            ad_level:        data.ad_level ?? 6,
            department:      data.department ?? "",
            can_approve:     data.can_approve ?? false,
            is_hod:          data.is_hod ?? false,
            hod_departments: data.hod_departments ?? [],
            is_reporting_manager: data.is_reporting_manager ?? false,
            ad_username:     data.ad_username ?? "",
            employee_id:     data.employee_id ?? "",
          });

        }
      })
      .catch(() => {})
      .finally(() => setAuthChecked(true));
  }, []);

  function handleAuth(authData) {
    setUser(authData);
    setAuthChecked(true);
    // Probe feature flags after login (same as the /auth/me restore path)

  }

  function handleLogout() {
    authFetch(`${API}/auth/logout`, { method: "POST" }).catch(() => {});
    // Desktop: wipe the OS-persisted CLI API key + Entra refresh token so the next
    // user on a shared machine can't inherit this session (no-op on web).
    coworkOfficeClearKey().catch(() => {});
    setChats([]);
    setActiveChatId(null);
    setUser(null);
    navigate("/", { replace: true });
    // Normalize URL to /portal/ (with trailing slash) so hard reload works
    if (document.location.pathname === PORTAL_BASE) {
      window.history.replaceState(null, "", `${PORTAL_BASE}/`);
    }
  }

  // ── Sidebar collapse (persisted in localStorage) ──────────
  const [navCollapsed, setNavCollapsed] = useState(() => {
    try { return localStorage.getItem("nav_collapsed") === "1"; } catch { return false; }
  });
  function toggleNav() {
    setNavCollapsed(c => {
      const next = !c;
      try { localStorage.setItem("nav_collapsed", next ? "1" : "0"); } catch { /* ignore */ }
      return next;
    });
  }

  // ── Tab-visibility refresh ─────────────────────────────────
  // Only remount non-chat views when returning after 10+ minutes away.
  // Short absences (alt+tab, quick switch) no longer destroy form state.
  const [refreshKey, setRefreshKey] = useState(0);
  const lastRefreshAt = useRef(Date.now()); // init to now so first tab-back doesn't trigger remount

  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      // 10 minutes — prevents losing in-progress form data on short tab switches
      if (now - lastRefreshAt.current < 10 * 60 * 1000) return;
      lastRefreshAt.current = now;
      setRefreshKey(k => k + 1);
    }
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, []);

  // ── Inbox unread count — deferred 3s after login to not compete with critical path ─
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    const userId = user?.userId || "";
    if (!userId) return;
    const poll = () => {
      authFetch(`${API}/inbox/unread-count?user=${userId}`)
        .then(r => r.json())
        .then(d => setUnreadCount(d.unread_count || 0))
        .catch(() => {});
    };
    // Defer first poll by 3s — login should render immediately, inbox badge can wait
    let intervalId;
    const delay = setTimeout(() => {
      poll();
      intervalId = setInterval(poll, 5 * 60 * 1000);
    }, 3000);
    return () => {
      clearTimeout(delay);
      clearInterval(intervalId);
    };
  }, [user]);

  // DB is the source of truth — no localStorage for chat data
  const [chats, setChats] = useState([]);
  const [activeChatId, setActiveChatId] = useState(null);
  const [chatsLoading, setChatsLoading] = useState(false);

  // ── Projects state — hoisted so streaming survives route navigation (mirrors Chat pattern) ──
  // Keyed by project ID so each workspace has independent messages and loading state.
  const [projectMessages, setProjectMessages] = useState({}); // { [projectId]: Message[] }
  const [projectLoading, setProjectLoading]   = useState({}); // { [projectId]: boolean }
  const [activeProjectId, setActiveProjectId] = useState(null);
  const projectAbortRef = useRef(null); // AbortController for the active stream

  // Fetch chat list from DB — deferred 500ms after login so the UI renders first
  useEffect(() => {
    if (!user) return;
    setChatsLoading(true);
    const t = setTimeout(() => {
      authFetch(`${API}/chats`)
        .then(r => r.json())
        .then(data => {
          const fetched = (data.chats || []).map(c => ({
            id:           c.id,
            title:        stripSystemPrefix(c.title) || "New Chat",
            messages:     [],
            fromBackend:  true,
            messageCount: c.message_count || 0,
            pinned:       c.is_pinned || false,
            updatedAt:    c.updated_at ? new Date(c.updated_at).getTime() : Date.now(),
            // KB chat persistence — isKbChat() requires rag_mode='on' AND at
            // least one scope field. Without these the KB chat panel filters
            // out every chat after a page refresh, making history look lost.
            // Backend (/chats) already returns them; we just need to keep them.
            rag_mode:     c.rag_mode || "off",
            product_id:   c.product_id   || null,
            domain:       c.domain       || null,
            spec_version: c.spec_version || null,
            kb_doc_id:    c.kb_doc_id    || null,
          }));
          if (fetched.length > 0) {
            setChats(fetched);
            setActiveChatId(fetched[0].id);
          } else {
            const blank = createEmptyChat();
            setChats([blank]);
            setActiveChatId(blank.id);
          }
        })
        .catch(() => {
          const blank = createEmptyChat();
          setChats([blank]);
          setActiveChatId(blank.id);
        })
        .finally(() => setChatsLoading(false));
    }, 500);
    return () => clearTimeout(t);
  }, [user]);

  function createEmptyChat() {
    return {
      id: crypto.randomUUID(),
      title: "New Chat",
      messages: [],
      createdAt: Date.now(),
      updatedAt: Date.now()
    };
  }

  // ── Show login if not authenticated ───────────────────────
  // Wait for /auth/me to complete before rendering — avoids flash of login screen
  if (!authChecked) return null;
  if (!user) {
    return <Login onAuth={handleAuth} />;
  }

  // ── Main app ──────────────────────────────────────────────

  return (
    <ToastProvider>
    <ConfirmProvider>
    <div className="flex h-screen w-screen overflow-hidden bg-white">

      <Sidebar
        view={view}
        setView={setView}
        user={user}
        onLogout={handleLogout}
        unreadCount={unreadCount}
        collapsed={navCollapsed}
        onToggleCollapse={toggleNav}
      />

      <div className="flex flex-col flex-1 min-w-0 h-full overflow-hidden">
        <Routes>
          {/* Path claim only — Chat is keep-alive, rendered outside <Routes> below. */}
          <Route path="/chat" element={null} />

          {/* Products — stable key like Chat, no refreshKey to preserve form state */}
          <Route path="/products" element={
            <ErrorBoundary key="products">
              <ProductManager user={user} />
            </ErrorBoundary>
          } />

          {/* All other views — remount on route change OR tab-refocus after 10 min (refreshKey) */}
          <Route path="/agents" element={
            <ErrorBoundary key={`agents-${refreshKey}`}>
              <AgentsCatalog user={user} />
            </ErrorBoundary>
          } />
          <Route path="/build-studio" element={
            <ErrorBoundary key="build-studio">
              <BuildStudio />
            </ErrorBoundary>
          } />
          <Route path="/codebase" element={
            <ErrorBoundary key={`codebase-${refreshKey}`}>
              <CodebaseManager user={user} />
            </ErrorBoundary>
          } />
          <Route path="/codewiki" element={
            <ErrorBoundary key="codewiki">
              <CodeWikiDocs />
            </ErrorBoundary>
          } />
          <Route path="/knowledge" element={
            <ErrorBoundary key={`knowledge-${refreshKey}`}>
              <KnowledgeBase
                user={user}
                chats={chats}
                setChats={setChats}
                chatsLoading={chatsLoading}
                setActiveChatId={setActiveChatId}
              />
            </ErrorBoundary>
          } />
          <Route path="/graph" element={
            <ErrorBoundary key={`graph-${refreshKey}`}>
              <KnowledgeGraph user={user} />
            </ErrorBoundary>
          } />
          <Route path="/connectors" element={
            <ErrorBoundary key={`connectors-${refreshKey}`}>
              <Connectors user={user} />
            </ErrorBoundary>
          } />
          <Route path="/cowork-setup" element={
            <ErrorBoundary key={`cowork-setup-${refreshKey}`}>
              <CoworkSettings user={user} />
            </ErrorBoundary>
          } />
          <Route path="/skill-proposals" element={
            <ErrorBoundary key={`skill-proposals-${refreshKey}`}>
              <SkillProposals user={user} />
            </ErrorBoundary>
          } />
          <Route path="/memory" element={
            <ErrorBoundary key={`memory-${refreshKey}`}>
              <Memory user={user} />
            </ErrorBoundary>
          } />
          <Route path="/projects" element={
            <ErrorBoundary key={`projects-${refreshKey}`}>
              <Projects
                user={user}
                projectMessages={projectMessages}
                setProjectMessages={setProjectMessages}
                projectLoading={projectLoading}
                setProjectLoading={setProjectLoading}
                activeProjectId={activeProjectId}
                setActiveProjectId={setActiveProjectId}
                projectAbortRef={projectAbortRef}
              />
            </ErrorBoundary>
          } />
          <Route path="/inbox" element={
            <ErrorBoundary key={`inbox-${refreshKey}`}>
              <Inbox user={user} onUnreadChange={setUnreadCount} />
            </ErrorBoundary>
          } />
          <Route path="/budget-manager" element={
            <ErrorBoundary key={`budget-${refreshKey}`}>
              <BudgetManager user={user} />
            </ErrorBoundary>
          } />
          <Route path="/level-overrides" element={
            <ErrorBoundary key={`level-overrides-${refreshKey}`}>
              <LevelOverrides user={user} />
            </ErrorBoundary>
          } />
          <Route path="/broadcast" element={
            <ErrorBoundary key={`broadcast-${refreshKey}`}>
              <EmailBroadcast user={user} />
            </ErrorBoundary>
          } />
          <Route path="/sdlc" element={
            <ErrorBoundary key={`sdlc-${refreshKey}`}>
              <SDLCPipeline user={user} />
            </ErrorBoundary>
          } />
          <Route path="/monitoring" element={
            <ErrorBoundary key={`monitoring-${refreshKey}`}>
              <Monitoring user={user} />
            </ErrorBoundary>
          } />
          <Route path="/analytics" element={
            <ErrorBoundary key={`analytics-${refreshKey}`}>
              <AgentAnalytics user={user} />
            </ErrorBoundary>
          } />
          <Route path="/evals" element={
            <ErrorBoundary key={`evals-${refreshKey}`}>
              <EvalsDashboard />
            </ErrorBoundary>
          } />
          <Route path="/model-governance" element={
            <ErrorBoundary key={`model-governance-${refreshKey}`}>
              <ModelGovernance />
            </ErrorBoundary>
          } />
          <Route path="/endpoint-manager" element={
            <ErrorBoundary key={`endpoint-manager-${refreshKey}`}>
              <EndpointManager user={user} />
            </ErrorBoundary>
          } />
          <Route path="/dept-metrics" element={
            <ErrorBoundary key={`dept-metrics-${refreshKey}`}>
              <DeptMetrics token={user?.token} />
            </ErrorBoundary>
          } />
          <Route path="/documents" element={
            <ErrorBoundary key={`docs-${refreshKey}`}>
              <DocsPanel />
            </ErrorBoundary>
          } />
          <Route path="/profile" element={
            <ErrorBoundary key={`profile-${refreshKey}`}>
              <Profile user={user} />
            </ErrorBoundary>
          } />
          {/* Buddy/Office is KEPT ALIVE outside <Routes> (rendered once, toggled with
              CSS) so switching tabs never unmounts it. Unmounting tore down the
              coworkOffice:onEvent listener, so a mid-answer tab switch dropped the CLI's
              streamed tokens + final result and the turn surfaced as a bare "error" /
              "0 tok" on return. Keeping it mounted preserves the listener and the
              in-flight conversation. This empty route only claims the /office path so
              the "*" fallback below doesn't redirect Buddy to /chat — the real UI is the
              persistent <Office> block after </Routes>. */}
          <Route path="/office" element={null} />
          <Route path="/code" element={
            <ErrorBoundary key={`code-${refreshKey}`}>
              <Code user={user} />
            </ErrorBoundary>
          } />

          <Route path="/coach" element={
            <ErrorBoundary key={`coach-${refreshKey}`}>
              <Coach user={user} />
            </ErrorBoundary>
          } />
          <Route path="/discussions" element={
            <ErrorBoundary key={`discussions-${refreshKey}`}>
              <Discussions user={user} />
            </ErrorBoundary>
          } />

          {/* Fallback — redirect unknown paths to root (chat) */}
          <Route path="*" element={<Navigate to="/chat" replace />} />
        </Routes>

        {/* Chat — keep-alive. Always mounted, visibility toggled via CSS so navigation
            never destroys component state (draft input, scroll position, active stream). */}
        <div className={`flex-1 min-h-0 ${path === "/chat" ? "flex flex-col" : "hidden"}`}>
          <ErrorBoundary key="chat">
            <Chat chats={chats} setChats={setChats} activeChatId={activeChatId} setActiveChatId={setActiveChatId} user={user} chatsLoading={chatsLoading} />
          </ErrorBoundary>
        </div>

        {/* Buddy/Office — KEEP-ALIVE. Rendered once and toggled with CSS instead of
            being a <Route> (which unmounts on tab switch). Unmounting removed the
            coworkOffice:onEvent listener, so a mid-answer tab switch dropped the CLI's
            streamed tokens + final result and the turn came back as a bare "error" /
            "0 tok". Staying mounted keeps the listener attached, so the answer keeps
            streaming into the conversation while you're on another tab and is simply
            there when you return. Uses `hidden` + display:none so it occupies no layout
            when inactive. Stable key => never remounts. */}
        <div className={`flex-1 min-h-0 ${path === "/office" ? "flex flex-col" : "hidden"}`}>
          <ErrorBoundary key="office">
            <Office user={user} />
          </ErrorBoundary>
        </div>
      </div>

    </div>
    </ConfirmProvider>
    </ToastProvider>
  );
}
