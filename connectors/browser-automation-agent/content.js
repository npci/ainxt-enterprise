// SPDX-License-Identifier: MIT
// content.js — runs in every page. Receives action requests from the side
// panel (via chrome.tabs.sendMessage) and executes them in the page DOM.

(() => {
  // Tear down any previous instance (e.g. after extension service-worker restart
  // the old content-script context becomes invalid; clearing it lets us re-register
  // a fresh message listener instead of silently failing).
  if (typeof window.__bAA_installed === 'function') {
    try { window.__bAA_installed(); } catch (_) {}
  }

  // Current document context — switched by switch_frame action
  let _currentDoc = document;

  // Recorder state
  let _recording = false;
  let _recordedSteps = [];
  let _lastRecordedUrl = null;
  // Tracks elements already captured by onRecordChange so post-click scan skips them.
  // Element-keyed (not selector-keyed) so it survives the move to array-valued selectors.
  let _recentlyChangedByEvent = new Set();
  // Holds the date picker container while a picker interaction is in progress; null otherwise
  let _suppressDatePickerClicks = null;
  // Holds the react-select control selector while waiting for the user to pick an option
  let _pendingSelectControl = null;
  let _pendingSelectClearId  = null;

  // Network mock state
  let _networkMocks = [];
  let _originalFetch = null;

  // QA Debug Mode capture state
  let _qaCapturing = false;
  let _qaConsoleLogs = [];
  let _qaNetworkEvents = [];
  let _qaOriginalConsole = {};

  // ---------- page-world eval (avoids MV3 extension CSP restriction on eval) ----------

  function evalInPageWorld(expr) {
    return new Promise((resolve, reject) => {
      const id = "__ba_" + Math.random().toString(36).slice(2);
      const onResult = (e) => {
        window.removeEventListener(id, onResult);
        if (e.detail.ok) resolve(e.detail.value);
        else reject(new Error(e.detail.error));
      };
      window.addEventListener(id, onResult);
      const code =
        "(function __baEvalInPageWorld(){var __id=" + JSON.stringify(id) + ";try{var __r=(" + expr + ");" +
        "Promise.resolve(__r).then(function(v){window.dispatchEvent(new CustomEvent(__id,{detail:{ok:true,value:v}}))}" +
        ",function(e){window.dispatchEvent(new CustomEvent(__id,{detail:{ok:false,error:e&&e.message||String(e)}}))})" +
        "}catch(e){window.dispatchEvent(new CustomEvent(__id,{detail:{ok:false,error:e&&e.message||String(e)}}))}})()" +
        "\n//# sourceURL=ba-page-eval.js";
      // Use blob: URL so pages with strict CSP (no unsafe-inline/unsafe-eval) still work.
      // Blobs from the extension origin match chrome-extension:// in the page's allowed sources.
      const blob = new Blob([code], { type: "application/javascript" });
      const url = URL.createObjectURL(blob);
      const el = document.createElement("script");
      el.src = url;
      el.onload = () => { URL.revokeObjectURL(url); el.remove(); };
      el.onerror = () => { URL.revokeObjectURL(url); el.remove(); reject(new Error("evalInPageWorld: script load failed")); };
      document.documentElement.appendChild(el);
    });
  }

  // ---------- selectors ----------

  // Targeted CSS selector covering all elements that can have an implicit or explicit role.
  // Used by resolve() to avoid querySelectorAll("*") on content-heavy pages.
  const ROLE_SELECTOR = "a,button,input,textarea,select,nav,main,header,footer,h1,h2,h3,h4,h5,h6,img,ul,ol,li,table,dialog,[role]";

  const IMPLICIT_ROLES = {
    A: (el) => (el.hasAttribute("href") ? "link" : null),
    BUTTON: () => "button",
    INPUT: (el) => {
      const t = (el.type || "text").toLowerCase();
      if (["button", "submit", "reset"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "search") return "searchbox";
      return "textbox";
    },
    TEXTAREA: () => "textbox",
    SELECT: () => "combobox",
    NAV: () => "navigation",
    MAIN: () => "main",
    HEADER: () => "banner",
    FOOTER: () => "contentinfo",
    H1: () => "heading",
    H2: () => "heading",
    H3: () => "heading",
    H4: () => "heading",
    H5: () => "heading",
    H6: () => "heading",
    IMG: () => "img",
    UL: () => "list",
    OL: () => "list",
    LI: () => "listitem",
    TABLE: () => "table",
    DIALOG: () => "dialog",
  };

  function getRole(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const fn = IMPLICIT_ROLES[el.tagName];
    return fn ? fn(el) : null;
  }

  // Cache of accessible names, scoped to a single snapshot pass. getAccessibleName
  // is invoked up to ~150× per pageSnapshot() (plus in resolve() role matching)
  // and each computation walks an 8-branch DOM lookup. The cache is reset at the
  // top of pageSnapshot() so names never go stale across DOM mutations.
  let _accNameCache = new WeakMap();

  function getAccessibleName(el) {
    if (_accNameCache.has(el)) return _accNameCache.get(el);
    const name = computeAccessibleName(el);
    _accNameCache.set(el, name);
    return name;
  }

  function computeAccessibleName(el) {
    const aria = el.getAttribute("aria-label");
    if (aria) return aria.trim();
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      const ids = labelledby.split(/\s+/);
      const doc = _currentDoc || document;
      const text = ids
        .map((id) => doc.getElementById(id)?.textContent || "")
        .join(" ")
        .trim();
      if (text) return text;
    }
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      const id = el.id;
      if (id) {
        const lbl = (_currentDoc || document).querySelector(`label[for="${cssEscape(id)}"]`);
        if (lbl?.textContent) return lbl.textContent.trim();
      }
      const wrap = el.closest("label");
      if (wrap) {
        const t = Array.from(wrap.childNodes)
          .filter((n) => n.nodeType === 3 || (n.nodeType === 1 && !["INPUT", "TEXTAREA", "SELECT"].includes(n.tagName)))
          .map((n) => n.textContent)
          .join("")
          .trim();
        if (t) return t;
      }
      const ph = el.getAttribute("placeholder");
      if (ph) return ph.trim();
    }
    if (el.tagName === "IMG") {
      const alt = el.getAttribute("alt");
      if (alt) return alt.trim();
    }
    const title = el.getAttribute("title");
    if (title) return title.trim();
    const txt = (el.textContent || "").trim();
    if (txt) return txt.replace(/\s+/g, " ");
    return "";
  }

  function cssEscape(s) {
    return CSS && CSS.escape ? CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
  }

  function isVisible(el) {
    if (!el || !(el instanceof Element)) return false;
    // offsetParent is spec'd to be null for <body>/<html> themselves (and for
    // position:fixed elements) regardless of whether they are actually
    // visible — so it's only a meaningful "possibly hidden" signal for
    // ordinary elements. Without this exemption isVisible(document.body)
    // always returned false, so ANY step targeting "body" (a reasonable
    // selector for "read/act on the whole page") failed on every page.
    const isRootEl = el.ownerDocument && (el === el.ownerDocument.body || el === el.ownerDocument.documentElement);
    // Cheap checks first: getComputedStyle is the expensive call (isVisible runs
    // on hundreds of candidates per snapshot), so fetch it lazily and at most once.
    let style = null;
    if (!isRootEl && el.offsetParent === null) {
      style = getComputedStyle(el);
      if (style.position !== "fixed") return false;
    }
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return false;
    style = style || getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none" || style.opacity === "0") return false;
    return true;
  }

  function isEnabled(el) {
    if (!el) return false;
    return !el.disabled && el.getAttribute("aria-disabled") !== "true";
  }

  // Briefly draw a glowing outline over an element so the user can see which
  // element the agent is acting on. Purely visual: position:fixed +
  // pointer-events:none means it never intercepts the click; auto-removed; and
  // wrapped in try/catch so it can never break an action.
  function flashTarget(el) {
    try {
      if (!el || !el.getBoundingClientRect) return;
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) return;
      const box = document.createElement("div");
      box.style.cssText =
        `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
        `border:2px solid #4f8cff;border-radius:4px;box-shadow:0 0 0 3px rgba(79,140,255,.35);` +
        `pointer-events:none;z-index:2147483647;transition:opacity .25s ease;opacity:1;`;
      (document.body || document.documentElement).appendChild(box);
      setTimeout(() => { box.style.opacity = "0"; }, 350);
      setTimeout(() => box.remove(), 650);
    } catch (_) {}
  }

  // Shared by click_at/hover_at: resolve CSS-pixel viewport coordinates to an
  // element. If the model echoed device pixels from a retina screenshot
  // (coords exceed the viewport but fit viewport×dpr), rescale them first.
  function resolveViewportPoint(x, y, actionName) {
    if (x == null || y == null) throw new Error(`${actionName} requires numeric x and y`);
    x = Number(x); y = Number(y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error(`${actionName} requires numeric x and y`);
    const vw = window.innerWidth, vh = window.innerHeight, dpr = window.devicePixelRatio || 1;
    if ((x > vw || y > vh) && dpr > 1 && x <= vw * dpr && y <= vh * dpr) {
      x = Math.round(x / dpr);
      y = Math.round(y / dpr);
    }
    if (x < 0 || y < 0 || x > vw || y > vh) {
      throw new Error(`${actionName} (${x},${y}) is outside the viewport ${vw}x${vh}`);
    }
    const el = document.elementFromPoint(x, y);
    if (!el) throw new Error(`No element at (${x},${y})`);
    return { x, y, el };
  }

  // Tracks the last ladder rung that resolved a match (for diagnostics in step results).
  let _lastMatchedSelector = null;

  // Elements captured by the most recent pageSnapshot(), in emission order.
  // Lets the LLM target an element as "ref=N" (1-based index into the snapshot's
  // INTERACTIVE ELEMENTS list) — the Set-of-Marks addressing scheme. Refs are
  // snapshot-scoped: a stale ref (element removed/hidden since the snapshot)
  // resolves to nothing and falls into the normal not-found → heal path.
  let _snapshotRegistry = [];

  // Open shadow roots under the current document, in depth-first host order.
  // Finding them requires a full querySelectorAll("*") walk — the single most
  // expensive DOM scan in this file — so the result is cached per generation:
  // reset alongside _accNameCache in execAction()/pageSnapshot() and on every
  // waitFor poll tick, which collapses the ~5 wildcard scans one snapshot used
  // to pay (and one per resolve() call while polling) into one. null = not yet
  // collected this generation.
  let _shadowRootCache = null;

  function collectShadowRoots(root) {
    const roots = [];
    const scan = (node) => {
      for (const host of node.querySelectorAll("*")) {
        if (host.shadowRoot) { roots.push(host.shadowRoot); scan(host.shadowRoot); }
      }
    };
    scan(root);
    return roots;
  }

  // Pierce open shadow roots: like querySelectorAll, but also matches inside any
  // element.shadowRoot, so web-component content (design systems,
  // Salesforce LWC, Ionic, Polymer, etc.) is reachable by resolve() and the
  // snapshot. Without this, querySelectorAll only sees the flat document and the
  // agent/recorder are blind to anything inside a custom element. Closed shadow
  // roots are inaccessible by design and silently skipped. Emission order matches
  // the old recursive walk: light-DOM matches first, then each shadow root's.
  function deepQuerySelectorAll(selector, root) {
    const defaultRoot = _currentDoc || document;
    root = root || defaultRoot;
    let shadowRoots;
    if (root === defaultRoot) {
      if (_shadowRootCache === null) _shadowRootCache = collectShadowRoots(root);
      shadowRoots = _shadowRootCache;
    } else {
      shadowRoots = collectShadowRoots(root);
    }
    const out = [];
    const seen = new Set();
    const match = (node) => {
      let matched;
      try { matched = node.querySelectorAll(selector); } catch { return; }
      for (const el of matched) { if (!seen.has(el)) { seen.add(el); out.push(el); } }
    };
    match(root);
    for (const sr of shadowRoots) match(sr);
    return out;
  }

  function resolve(target) {
    if (!target) return [];
    // Ladder: try each rung; return elements from the first one that matches anything.
    if (Array.isArray(target)) {
      for (const t of target) {
        const r = resolve(t);
        if (r.length > 0) { _lastMatchedSelector = t; return r; }
      }
      return [];
    }
    const doc = _currentDoc || document;

    // ref=N — 1-based index into the last snapshot's INTERACTIVE ELEMENTS list.
    // Validates the element is still attached and visible; a stale ref returns
    // [] so it behaves like any other failed selector (advances ladder / heals).
    const refMatch = target.match(/^ref=(\d+)$/);
    if (refMatch) {
      const el = _snapshotRegistry[Number(refMatch[1]) - 1];
      if (!el || !el.isConnected) return [];
      // Hidden radios/checkboxes are legitimately in the snapshot (quiz sites
      // hide them with CSS) — accept those; everything else must still be visible.
      const tp = (el.getAttribute?.("type") || "").toLowerCase();
      const hiddenToggle = el.tagName === "INPUT" && (tp === "radio" || tp === "checkbox");
      return isVisible(el) || hiddenToggle ? [el] : [];
    }

    // role=ROLE[name="..."]
    const roleMatch = target.match(
      /^role=([a-zA-Z]+)(?:\[name=(?:"([^"]+)"|'([^']+)')\])?$/,
    );
    if (roleMatch) {
      const wantedRole = roleMatch[1].toLowerCase();
      const wantedName = (roleMatch[2] || roleMatch[3] || "").toLowerCase();
      const all = deepQuerySelectorAll(ROLE_SELECTOR);
      return all.filter((el) => {
        const role = (getRole(el) || "").toLowerCase();
        if (role !== wantedRole) return false;
        if (!wantedName) return true;
        const name = getAccessibleName(el).toLowerCase();
        return name === wantedName || name.includes(wantedName);
      });
    }

    // text="..."
    const textMatch = target.match(/^text=(?:"([^"]+)"|'([^']+)')$/);
    if (textMatch) {
      const txt = (textMatch[1] || textMatch[2]).trim();
      if (!txt) return [];
      // Walk text NODES (TreeWalker over the document + each open shadow root)
      // instead of reading textContent on every element — textContent re-walks
      // the whole subtree per element, which made this rung O(N²) on deep pages
      // (and it runs inside waitFor's poll loop). An element matches on its OWN
      // text-node content, same as before; whitespace-only nodes can't contain
      // a non-empty trimmed txt, so their parents are skipped outright.
      const NON_TEXT = new Set(["SCRIPT", "STYLE", "HEAD", "TITLE", "META", "LINK", "NOSCRIPT"]);
      if (_shadowRootCache === null) _shadowRootCache = collectShadowRoots(doc);
      const candidates = [];
      const seen = new Set();
      for (const root of [doc, ..._shadowRootCache]) {
        const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let n;
        while ((n = walker.nextNode())) {
          if (!/\S/.test(n.textContent)) continue;
          const p = n.parentElement;
          if (!p || seen.has(p) || NON_TEXT.has(p.tagName)) continue;
          seen.add(p);
          candidates.push(p);
        }
      }
      return candidates.filter((el) => {
        const own = Array.from(el.childNodes)
          .filter((n) => n.nodeType === 3)
          .map((n) => n.textContent)
          .join("")
          .trim();
        return own === txt || own.includes(txt);
      });
    }

    // xpath=...
    if (target.startsWith("xpath=")) {
      const xp = target.slice("xpath=".length);
      const result = doc.evaluate(
        xp,
        doc,
        null,
        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,
        null,
      );
      const out = [];
      for (let i = 0; i < result.snapshotLength; i++) {
        out.push(result.snapshotItem(i));
      }
      return out;
    }

    // CSS selector (pierces shadow roots; combinators that cross a shadow
    // boundary won't match, which matches how shadow DOM scoping works anyway).
    return deepQuerySelectorAll(target);
  }

  function findOne(target, opts = {}) {
    // Ladder: try each rung; "0 visible elements" advances to the next rung
    // so a bad first rung doesn't shadow a good later one.
    if (Array.isArray(target)) {
      const errors = [];
      for (const t of target) {
        try {
          const el = findOne(t, opts);
          _lastMatchedSelector = t;
          return el;
        } catch (e) {
          errors.push(`${t} — ${e.message}`);
        }
      }
      throw new Error(
        `All ${target.length} ladder selectors failed:\n  ${errors.join("\n  ")}`,
      );
    }
    const all = resolve(target);
    const visible = all.filter((el) =>
      opts.allowHidden ? true : isVisible(el),
    );
    if (visible.length === 0) {
      throw new Error(
        `Selector resolved 0 ${opts.allowHidden ? "elements" : "visible elements"}: ${target}`,
      );
    }
    if (visible.length > 1 && !opts.allowMany) {
      const interactive = visible.filter(isEnabled);
      if (interactive.length === 1) return interactive[0];
      const pool = interactive.length > 1 ? interactive : visible;
      // Prefer elements inside a <form> when exactly one matches
      const inForm = pool.filter(el => !!el.closest("form"));
      if (inForm.length === 1) return inForm[0];
      // Prefer elements fully within the viewport
      const inViewport = (inForm.length > 1 ? inForm : pool).filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 &&
          r.top >= 0 && r.left >= 0 &&
          r.bottom <= (window.innerHeight || document.documentElement.clientHeight) &&
          r.right <= (window.innerWidth || document.documentElement.clientWidth);
      });
      if (inViewport.length >= 1) return inViewport[0];
    }
    return visible[0];
  }

  // ---------- waits ----------

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  // ---------- DOM settle detection ----------
  // Stamps the time of the last meaningful DOM change. childList + characterData
  // only — attribute churn (CSS animations, carousels) would never go quiet.
  let _lastMutationTs = 0;
  let _mutationObserver = null;

  function ensureMutationTracker() {
    if (_mutationObserver) return;
    _lastMutationTs = Date.now();
    _mutationObserver = new MutationObserver(() => { _lastMutationTs = Date.now(); });
    _mutationObserver.observe(document.documentElement || document, {
      childList: true,
      characterData: true,
      subtree: true,
    });
  }

  // Conservative "the page says it's still loading" probe. Kept tight on
  // purpose: a false positive only costs the settle budget (bounded), but a
  // broad selector (e.g. bare [class*="load"]) would penalize every page.
  const LOADING_INDICATOR_SELECTOR = [
    '[aria-busy="true"]',
    '[role="progressbar"]',
    ".spinner",
    ".loader",
    ".loading-spinner",
    ".skeleton",
  ].join(",");

  function hasVisibleLoadingIndicator() {
    try {
      return Array.from(document.querySelectorAll(LOADING_INDICATOR_SELECTOR)).some(isVisible);
    } catch {
      return false;
    }
  }

  function domLooksSettled(quietMs) {
    ensureMutationTracker();
    return (
      document.readyState === "complete" &&
      Date.now() - _lastMutationTs >= quietMs &&
      !hasVisibleLoadingIndicator()
    );
  }

  // Resolves once the page looks settled (loaded + mutation-quiet + no visible
  // loading indicator) or when maxMs is spent. Never rejects — the caller
  // decides what an unsettled page means.
  async function waitForDomSettled({ quietMs = 500, maxMs = 4000 } = {}) {
    const start = Date.now();
    ensureMutationTracker();
    while (Date.now() - start < maxMs) {
      if (domLooksSettled(quietMs)) return { settled: true, elapsedMs: Date.now() - start };
      await sleep(100);
    }
    return { settled: false, elapsedMs: Date.now() - start };
  }

  async function waitFor(condition, target, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    let lastErr = null;
    while (Date.now() < deadline) {
      // New shadow hosts can mount mid-wait (e.g. a web-component dialog the
      // condition is polling for) — re-collect once per tick, not per rung.
      _shadowRootCache = null;
      try {
        if (await checkCondition(condition, target)) return true;
      } catch (e) {
        lastErr = e;
      }
      await sleep(50);
    }
    throw new Error(
      `wait timed out (${timeoutMs}ms) for ${condition}` +
        (lastErr ? `: ${lastErr.message}` : ""),
    );
  }

  async function checkCondition(condition, target) {
    // Ladder semantics:
    //   detached  → AND (no rung matches anything)
    //   everything else → OR (any rung satisfying succeeds)
    if (Array.isArray(target)) {
      if (condition === "detached") {
        for (const t of target) {
          const stillThere = await checkCondition("attached", t).catch(() => false);
          if (stillThere) return false;
        }
        return true;
      }
      for (const t of target) {
        if (await checkCondition(condition, t).catch(() => false)) return true;
      }
      return false;
    }
    if (!condition) {
      return resolve(target).some(isVisible);
    }
    if (condition === "visible") {
      return resolve(target).some(isVisible);
    }
    if (condition === "attached") {
      return resolve(target).length > 0;
    }
    if (condition === "detached") {
      return resolve(target).length === 0;
    }
    if (condition === "enabled") {
      return resolve(target).some((el) => isVisible(el) && isEnabled(el));
    }
    if (condition.startsWith("text:")) {
      const want = condition.slice("text:".length);
      return resolve(target).some((el) => (el.textContent || "").includes(want));
    }
    if (condition.startsWith("url_matches:")) {
      const re = new RegExp(condition.slice("url_matches:".length));
      const href = (_currentDoc || document).defaultView?.location.href || location.href;
      return re.test(href);
    }
    if (condition === "network_idle") {
      const doc = _currentDoc || document;
      // Same-origin subframe context: the mutation tracker watches the top
      // document, so fall back to the frame's readyState alone.
      if (doc !== document) return doc.readyState === "complete";
      return domLooksSettled(500);
    }
    if (condition.startsWith("js:")) {
      const expr = condition.slice("js:".length);
      try {
        return !!(await evalInPageWorld(expr));
      } catch {
        return false;
      }
    }
    return false;
  }

  // ---------- assertions ----------

  function getActual({ target, matcher, attr }) {
    // Ladder semantics:
    //   present/visible/enabled → OR (any rung satisfies)
    //   absent/hidden/disabled  → AND (all rungs satisfy)
    //   count                   → unique elements across all rungs
    //   value/text/attribute    → first rung whose element produces a value
    if (Array.isArray(target)) {
      if (matcher === "count") {
        const seen = new Set();
        for (const t of target) for (const el of resolve(t)) seen.add(el);
        return seen.size;
      }
      if (matcher === "present" || matcher === "visible" || matcher === "enabled") {
        return target.some((t) => getActual({ target: t, matcher, attr }) === true);
      }
      if (matcher === "absent" || matcher === "hidden" || matcher === "disabled") {
        return target.every((t) => getActual({ target: t, matcher, attr }) === true);
      }
      // Value-style matchers: first rung that yields a non-empty value wins
      for (const t of target) {
        const v = getActual({ target: t, matcher, attr });
        if (v !== null && v !== undefined && v !== "") return v;
      }
      return null;
    }

    const all = resolve(target || "*");

    if (matcher === "count") return all.length;
    if (matcher === "present") return all.length > 0;
    if (matcher === "absent") return all.length === 0;
    if (matcher === "visible") return all.some(isVisible);
    if (matcher === "hidden") return all.length > 0 && !all.some(isVisible);
    if (matcher === "enabled") return all.some(isEnabled);
    if (matcher === "disabled") return all.length > 0 && !all.some(isEnabled);

    if (matcher && matcher.startsWith("attribute:")) {
      const name = matcher.slice("attribute:".length);
      const el = all[0];
      if (!el) return null;
      if (name === "value") return el.value ?? el.getAttribute("value");
      return el.getAttribute(name);
    }

    const el = all[0];
    if (!el) return null;
    if ("value" in el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
      return el.value;
    }
    return (el.textContent || "").trim();
  }

  function compare(actual, matcher, expected) {
    if (
      ["visible", "hidden", "present", "absent", "enabled", "disabled"].includes(matcher)
    ) {
      return actual === (expected === undefined ? true : !!expected);
    }
    if (matcher === "count") return Number(actual) === Number(expected);
    if (matcher === "contains") return String(actual).includes(String(expected));
    if (matcher === "not_contains") return !String(actual).includes(String(expected));
    if (matcher === "matches") return new RegExp(expected).test(String(actual));
    if (matcher === "not_equals") return String(actual) !== String(expected);
    if (matcher && matcher.startsWith("attribute:")) return String(actual) === String(expected);
    return String(actual) === String(expected);
  }

  // ---------- input helpers ----------

  function dispatchInputChange(el, text) {
    // Use InputEvent with inputType so React 17+ synthetic event system registers the change.
    try {
      el.dispatchEvent(new InputEvent("input", {
        bubbles: true, cancelable: true,
        inputType: "insertText",
        data: (text != null ? String(text) : null),
      }));
    } catch (_) {
      el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function typeInto(el, text) {
    el.focus();
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
      const proto =
        el.tagName === "INPUT"
          ? window.HTMLInputElement.prototype
          : window.HTMLTextAreaElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      // Clear first so React detects a value change even if text matches current value.
      setter.call(el, "");
      el.dispatchEvent(new InputEvent("input", { bubbles: true, cancelable: true, inputType: "deleteContentBackward" }));
      setter.call(el, text);
      dispatchInputChange(el, text);
      el.dispatchEvent(new Event("blur", { bubbles: true }));
    } else if (el.isContentEditable) {
      el.focus();
      // Select all existing content then replace — execCommand keeps React/contentEditable in sync.
      try {
        document.execCommand("selectAll");
        document.execCommand("insertText", false, text);
      } catch (_) {
        el.textContent = text;
      }
      dispatchInputChange(el, text);
    } else {
      el.value = text;
      dispatchInputChange(el, text);
    }
  }

  function pressKey(target, value) {
    const el = target ? findOne(target) : document.activeElement;
    const parts = String(value).split("+").map((s) => s.trim());
    const key = parts.pop();
    const init = {
      bubbles: true,
      cancelable: true,
      key,
      code: keyCode(key),
      ctrlKey: parts.includes("Ctrl") || parts.includes("Control"),
      altKey: parts.includes("Alt"),
      shiftKey: parts.includes("Shift"),
      metaKey:
        parts.includes("Cmd") || parts.includes("Meta") || parts.includes("Command"),
    };
    el.dispatchEvent(new KeyboardEvent("keydown", init));
    el.dispatchEvent(new KeyboardEvent("keypress", init));
    el.dispatchEvent(new KeyboardEvent("keyup", init));
    if (key === "Enter" && el.form && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
      try {
        el.form.requestSubmit();
      } catch {
        el.form.submit();
      }
    }
  }

  function keyCode(key) {
    const map = {
      Enter: "Enter", Tab: "Tab", Escape: "Escape",
      ArrowUp: "ArrowUp", ArrowDown: "ArrowDown",
      ArrowLeft: "ArrowLeft", ArrowRight: "ArrowRight",
      Backspace: "Backspace", Space: "Space",
    };
    if (map[key]) return map[key];
    if (key.length === 1) return "Key" + key.toUpperCase();
    return key;
  }

  // ---------- snapshot ----------

  // Pick the element that actually holds the page's main content. A bare
  // querySelector over a selector list returns the first match in DOCUMENT
  // order — on many blogs that's an empty #content/main mount node (client-side
  // rendering), which made page_text/get_page_text come back empty while the
  // real article text sat in <body>. Walk the candidates in priority order,
  // take the first with substantial text, and never return a scope emptier
  // than the body itself.
  function pickMainContentEl(doc) {
    const candidates = ["main", "[role='main']", "#main", "#content", ".main-content", "article"];
    let best = null, bestLen = 0;
    for (const sel of candidates) {
      const el = doc.querySelector(sel);
      const len = (el?.innerText || "").trim().length;
      if (len >= 200) return el;
      if (len > bestLen) { best = el; bestLen = len; }
    }
    const bodyLen = (doc.body?.innerText || "").trim().length;
    return bodyLen > bestLen ? doc.body : (best || doc.body);
  }

  // Flip-books/maps/design tools draw their content on <canvas> (or one giant
  // image) — innerText only sees the app shell around it, so the model "reads"
  // the nav buttons and concludes it read the page. Flag the page as visual
  // when rendered canvas/large-image area dominates the viewport while
  // extractable text is thin. Traverses exactly what the snapshot traverses
  // (_currentDoc + open shadow roots, no iframe descent — after switch_frame
  // both look inside the frame).
  function detectVisualContent(textLen) {
    const vpArea = window.innerWidth * window.innerHeight;
    if (!vpArea || textLen >= 600) return false;
    const visibleArea = (el) => {
      const r = el.getBoundingClientRect();
      const w = Math.min(r.right, window.innerWidth) - Math.max(r.left, 0);
      const h = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
      return w > 0 && h > 0 ? w * h : 0;
    };
    let area = 0;
    for (const c of deepQuerySelectorAll("canvas")) area += visibleArea(c);
    // Only individually-big images count (image-based flip-books) so
    // thumbnails/logos/hero images don't accumulate into a false positive.
    for (const img of deepQuerySelectorAll("img")) {
      const a = visibleArea(img);
      if (a >= 0.2 * vpArea) area += a;
    }
    // Stacked layers (flip-books often layer canvases) can't exceed ratio 1.
    return Math.min(area, vpArea) / vpArea >= 0.3;
  }

  function pageSnapshot() {
    const doc = _currentDoc || document;

    // Reset the per-snapshot accessible-name and shadow-root caches so both
    // reflect the current DOM.
    _accNameCache = new WeakMap();
    _shadowRootCache = null;

    // Radio/checkbox inputs are always included even when hidden (quiz sites hide them with CSS).
    // Other form elements + interactive elements require visibility.
    const CAP = 150;
    const seen = new Set();
    const collected = [];
    for (const el of deepQuerySelectorAll("input[type=radio], input[type=checkbox]")) {
      if (seen.has(el)) continue;
      seen.add(el);
      collected.push(el);
    }
    // Visible form controls first, then other interactive elements (preserves the
    // original priority order). Stop as soon as we hit the cap so the expensive
    // isVisible() check never runs over the entire DOM on heavy pages.
    const visiblePasses = [
      "input:not([type=radio]):not([type=checkbox]), textarea, select, button",
      "a, [role], [data-testid], [data-test], [data-cy]",
    ];
    for (const sel of visiblePasses) {
      if (collected.length >= CAP) break;
      for (const el of deepQuerySelectorAll(sel)) {
        if (collected.length >= CAP) break;
        if (seen.has(el) || !isVisible(el)) continue;
        seen.add(el);
        collected.push(el);
      }
    }
    // Refresh the ref=N registry: index i in this array is ref i+1.
    _snapshotRegistry = collected.slice(0, CAP);
    const interactive = _snapshotRegistry
      .map((el, i) => {
        const t = (el.getAttribute("type") || "").toLowerCase();
        const r = el.getBoundingClientRect();
        const isFormInput = el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA";
        // Remember which test-id attribute actually matched so selectors can use
        // the exact attribute ([data-cy=...] vs [data-testid=...]).
        const testidAttr = ["data-testid", "data-test", "data-cy"].find((a) => el.getAttribute(a));
        return {
          ref: i + 1,
          rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
          tag: el.tagName.toLowerCase(),
          role: getRole(el),
          name: getAccessibleName(el).slice(0, 100),
          id: el.id || undefined,
          testid: testidAttr ? el.getAttribute(testidAttr) : undefined,
          testid_attr: testidAttr,
          type: el.getAttribute("type") || undefined,
          // include name= attr for inputs so LLM can build selectors like input[name="chk_opt_14"]
          input_name: isFormInput ? (el.getAttribute("name") || undefined) : undefined,
          // include value= for radio/checkbox so LLM knows e.g. val_A_14
          value: (t === "radio" || t === "checkbox") ? (el.getAttribute("value") || undefined) : undefined,
          href: el.getAttribute("href") || undefined,
          placeholder: el.getAttribute("placeholder") || undefined,
        };
      });

    // Visible body text so LLM can read questions, labels, and option text.
    const mainEl = pickMainContentEl(doc);
    const page_text = (mainEl?.innerText || "").replace(/[ \t]+/g, " ").trim().slice(0, 8000) || undefined;
    const visual_content = detectVisualContent((page_text || "").length);

    // Detect open modals/dialogs so the LLM knows one is active and targets elements inside it.
    const openDialogs = deepQuerySelectorAll(
      '[role="dialog"],[role="alertdialog"],.modal,.dialog,[class*="modal"],[class*="dialog"]'
    ).filter(isVisible).map((d) => ({
      title: (
        d.getAttribute("aria-label") ||
        d.querySelector('[role="heading"],[class*="title"],[class*="header"] h1,[class*="header"] h2')?.textContent?.trim() ||
        d.querySelector("h1,h2,h3")?.textContent?.trim() || ""
      ).slice(0, 120),
      id: d.id || undefined,
    }));

    return {
      url: doc.defaultView?.location.href || location.href,
      title: doc.title,
      viewport: { w: window.innerWidth, h: window.innerHeight },
      dpr: window.devicePixelRatio || 1,
      open_dialogs: openDialogs.length ? openDialogs : undefined,
      headings: deepQuerySelectorAll("h1, h2, h3")
        .filter(isVisible)
        .slice(0, 12)
        .map((h) => ({
          level: h.tagName.toLowerCase(),
          text: (h.textContent || "").trim().slice(0, 120),
        })),
      interactive,
      page_text,
      // undefined (not false) keeps snapshots of normal pages byte-identical.
      visual_content: visual_content || undefined,
    };
  }

  // ---------- read_page / find helpers ----------

  // Give an element a stable [N] ref in the CURRENT snapshot generation,
  // appending to the registry when it wasn't part of the original snapshot.
  // The registry only grows within a generation, so refs handed out by
  // read_page/find stay valid ref=N targets until the next pageSnapshot().
  function ensureRef(el) {
    let idx = _snapshotRegistry.indexOf(el);
    if (idx === -1) {
      _snapshotRegistry.push(el);
      idx = _snapshotRegistry.length - 1;
    }
    return idx + 1;
  }

  const INTERACTIVE_TAGS = new Set(["a", "button", "input", "textarea", "select"]);
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "checkbox", "radio", "tab", "menuitem", "menuitemradio",
    "menuitemcheckbox", "option", "combobox", "textbox", "searchbox", "switch", "slider",
  ]);
  function isInteractiveEl(el) {
    if (INTERACTIVE_TAGS.has(el.tagName.toLowerCase())) return true;
    const role = (el.getAttribute("role") || "").toLowerCase();
    return INTERACTIVE_ROLES.has(role);
  }

  // Tags whose text content read_page reports in "all" mode.
  const TEXT_TAGS = new Set([
    "p", "li", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote",
    "pre", "figcaption", "dt", "dd", "label", "legend", "caption", "summary",
  ]);
  const LANDMARK_ROLES = new Set([
    "navigation", "main", "banner", "contentinfo", "form", "table", "dialog", "list", "region",
  ]);
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "META", "LINK", "HEAD", "TITLE", "SVG"]);

  // One-line description of an element for the read_page tree (leaner cousin of
  // the snapshot's formatElementLine — that one lives in lib/llm.js).
  function describeElementLine(el) {
    const tag = el.tagName.toLowerCase();
    const parts = [tag];
    const role = getRole(el);
    if (role && role !== tag) parts.push(`role=${role}`);
    const type = el.getAttribute("type");
    if (type) parts.push(`type=${JSON.stringify(type)}`);
    const name = getAccessibleName(el);
    if (name) parts.push(JSON.stringify(name.slice(0, 80)));
    if (el.id) parts.push(/^[A-Za-z0-9_-]+$/.test(el.id) ? `#${el.id}` : `id=${JSON.stringify(el.id)}`);
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      const v = el.value;
      if (v) parts.push(`value=${JSON.stringify(String(v).slice(0, 40))}`);
      if (el.checked) parts.push("checked");
      if (el.disabled) parts.push("disabled");
    }
    const ph = el.getAttribute("placeholder");
    if (ph) parts.push(`placeholder=${JSON.stringify(ph)}`);
    return parts.join(" ");
  }

  // Text tree of the page (or a sub-tree) for the read_page action. Descends
  // into open shadow roots like deepQuerySelectorAll. `filter: "all"` adds text
  // content (paragraphs, list items, table cells) and landmarks to the
  // interactive listing; below a reported text tag only interactive descendants
  // are emitted so paragraph content isn't double-reported. Depth counts
  // EMITTED levels, so wrapper-div soup doesn't burn the indent budget.
  function buildPageTree(root, { filter = "interactive", maxDepth = Infinity, maxChars = 15000 } = {}) {
    const lines = [];
    let used = 0;
    let truncated = false;
    const push = (line, depth) => {
      const text = "  ".repeat(Math.min(depth, 10)) + line;
      if (used + text.length + 1 > maxChars) { truncated = true; return false; }
      lines.push(text);
      used += text.length + 1;
      return true;
    };
    const visit = (node, depth, textMode) => {
      if (truncated || depth > maxDepth) return;
      // toUpperCase: SVG (and other foreign-namespace) elements report a
      // lowercase tagName, unlike HTML elements.
      if (!(node instanceof Element) || SKIP_TAGS.has(node.tagName.toUpperCase())) return;
      let emitted = false;
      let childTextMode = textMode;
      const tag = node.tagName.toLowerCase();
      const tp = (node.getAttribute("type") || "").toLowerCase();
      const hiddenToggle = node.tagName === "INPUT" && (tp === "radio" || tp === "checkbox");
      if (isInteractiveEl(node) && (isVisible(node) || hiddenToggle)) {
        emitted = push(`[${ensureRef(node)}] ${describeElementLine(node)}`, depth);
      } else if (filter === "all" && !textMode && isVisible(node)) {
        if (TEXT_TAGS.has(tag)) {
          const text = (node.innerText || "").replace(/\s+/g, " ").trim();
          if (text) {
            emitted = push(`${tag} ${JSON.stringify(text.slice(0, 300))}`, depth);
            childTextMode = true; // don't re-report this text via descendants
          }
        } else {
          const role = getRole(node);
          if (role && LANDMARK_ROLES.has(role)) {
            const name = getAccessibleName(node).slice(0, 60);
            emitted = push(`${tag} role=${role}${name ? ` ${JSON.stringify(name)}` : ""}`, depth);
          }
        }
      }
      const childDepth = emitted ? depth + 1 : depth;
      if (node.shadowRoot) {
        for (const c of node.shadowRoot.children) visit(c, childDepth, childTextMode);
      }
      for (const c of node.children) visit(c, childDepth, childTextMode);
    };
    visit(root, 0, false);
    if (truncated) {
      lines.push(`…TRUNCATED at ${maxChars} chars — re-read a sub-tree with read_page { ref: N } to see more`);
    }
    return lines.join("\n");
  }

  // Durable selector suggestion for a find() match, following the preference
  // order the prompts teach: role=…[name] > test-id > #id > [name] > text=.
  function durableSelectorFor(el, ref) {
    const role = getRole(el);
    const name = getAccessibleName(el);
    if (role && name && name.length <= 60 && !name.includes('"')) return `role=${role}[name="${name}"]`;
    const testidAttr = ["data-testid", "data-test", "data-cy"].find((a) => el.getAttribute(a));
    if (testidAttr) return `[${testidAttr}="${el.getAttribute(testidAttr)}"]`;
    if (el.id && /^[A-Za-z0-9_-]+$/.test(el.id)) return `#${el.id}`;
    const nm = el.getAttribute("name");
    if (nm && !nm.includes('"')) return `${el.tagName.toLowerCase()}[name="${nm}"]`;
    const txt = (el.textContent || "").replace(/\s+/g, " ").trim();
    if (txt && txt.length <= 40 && !txt.includes('"')) return `text="${txt}"`;
    return `ref=${ref}`;
  }

  // Words the query may use for each role — lets "add to cart button" reward
  // actual buttons without requiring the word in the accessible name.
  const ROLE_SYNONYMS = {
    button: ["button", "btn", "submit"],
    link: ["link", "anchor"],
    textbox: ["field", "input", "box", "textbox", "text"],
    searchbox: ["search", "searchbox", "field", "input"],
    checkbox: ["checkbox", "check", "toggle"],
    radio: ["radio"],
    combobox: ["dropdown", "select", "combobox", "picker"],
    img: ["image", "icon", "picture", "img", "logo"],
    heading: ["heading", "title", "header"],
  };

  // Local, LLM-free pass of the `find` action: score the current snapshot
  // registry against a natural-language query. Exact accessible-name match >
  // case-insensitive substring > token overlap, plus a role-synonym bonus.
  // Returns ranked matches with refs usable directly as ref=N targets.
  function scoreFindCandidates(query, limit = 20) {
    const q = query.toLowerCase().trim();
    const qTokens = q.split(/\s+/).filter(Boolean);
    const results = [];
    _snapshotRegistry.forEach((el, i) => {
      if (!el || !el.isConnected || !el.getAttribute) return;
      const role = (getRole(el) || "").toLowerCase();
      const roleWords = ROLE_SYNONYMS[role] || (role ? [role] : []);
      const textTokens = qTokens.filter((t) => !roleWords.includes(t));
      let score = qTokens.some((t) => roleWords.includes(t)) ? 2 : 0;
      const fields = [
        getAccessibleName(el),
        el.getAttribute("aria-label"),
        el.getAttribute("placeholder"),
        el.getAttribute("title"),
        (el.textContent || "").trim().slice(0, 120),
        el.id,
        el.getAttribute("name"),
        el.getAttribute("value"),
      ];
      let best = 0;
      for (const f of fields) {
        if (!f) continue;
        const t = String(f).toLowerCase();
        if (t === q) { best = Math.max(best, 10); break; }
        if (t.includes(q)) { best = Math.max(best, 8); continue; }
        if (textTokens.length) {
          const overlap = textTokens.filter((tok) => t.includes(tok)).length / textTokens.length;
          best = Math.max(best, overlap * 6);
        }
      }
      score += best;
      if (score >= 3) results.push({ el, ref: i + 1, score });
    });
    results.sort((a, b) => b.score - a.score);
    return results.slice(0, limit).map(({ el, ref, score }) => ({
      ref,
      score: Math.round(score * 10) / 10,
      role: getRole(el) || undefined,
      name: getAccessibleName(el).slice(0, 80) || undefined,
      selector: durableSelectorFor(el, ref),
      snippet: (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80) || undefined,
    }));
  }

  // ---------- step dispatcher ----------

  // Parses a date string from a datepick value into { year, month (0-based), day }.
  function parseDateForPick(str) {
    const m1 = str.match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (m1) return { year: +m1[1], month: +m1[2] - 1, day: +m1[3] };
    const m2 = str.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
    if (m2) return { year: +m2[3], month: +m2[1] - 1, day: +m2[2] };
    const m3 = str.match(/^(\d{1,2})-(\d{1,2})-(\d{4})/);
    if (m3) return { year: +m3[3], month: +m3[2] - 1, day: +m3[1] };
    const d = new Date(str);
    if (!isNaN(d)) return { year: d.getFullYear(), month: d.getMonth(), day: d.getDate() };
    return null;
  }

  // Parses a calendar header string like "May 2026", "April 2025", "2026-05"
  // into { year, month (0-based) }. Returns null if unrecognised.
  function parseDisplayedMonthYear(text) {
    const MONTHS = {
      january:0,february:1,march:2,april:3,may:4,june:5,
      july:6,august:7,september:8,october:9,november:10,december:11,
      jan:0,feb:1,mar:2,apr:3,jun:5,jul:6,aug:7,sep:8,oct:9,nov:10,dec:11,
    };
    const m1 = text.match(/([a-z]+)\s+(\d{4})/i) || text.match(/(\d{4})\s+([a-z]+)/i);
    if (m1) {
      const mStr = (m1[1].length > 4 ? m1[1] : m1[2]).toLowerCase();
      const yStr = m1[1].length === 4 ? m1[1] : m1[2];
      const mon = MONTHS[mStr];
      if (mon !== undefined) return { year: Number(yStr), month: mon };
    }
    const m2 = text.match(/(\d{4})-(\d{2})/) || text.match(/(\d{2})\/(\d{4})/);
    if (m2) {
      const [, a, b] = m2;
      if (a.length === 4) return { year: Number(a), month: Number(b) - 1 };
      return { year: Number(b), month: Number(a) - 1 };
    }
    return null;
  }

  // REQ-18: parse a "cmd+shift" style modifier string into MouseEvent flags.
  // `cmd`/`command` maps to metaKey on mac and ctrlKey elsewhere — the
  // platform's "open link in background tab" chord — so a modifier-click reads
  // the same cross-platform (FR18.3).
  function mouseModifiers(modifiers) {
    const flags = { ctrlKey: false, metaKey: false, shiftKey: false, altKey: false };
    if (!modifiers) return flags;
    const isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || "");
    for (const raw of String(modifiers).toLowerCase().split("+")) {
      const p = raw.trim();
      if (p === "ctrl" || p === "control") flags.ctrlKey = true;
      else if (p === "shift") flags.shiftKey = true;
      else if (p === "alt" || p === "option") flags.altKey = true;
      else if (p === "meta" || p === "cmd" || p === "command") {
        if (isMac) flags.metaKey = true; else flags.ctrlKey = true;
      }
    }
    return flags;
  }

  async function execAction(step) {
    const t = step.timeout_ms || 15000;
    // Bound the accessible-name and shadow-root caches to a single action so
    // resolve() never reads state left over from a prior, since-mutated DOM.
    _accNameCache = new WeakMap();
    _shadowRootCache = null;
    _lastMatchedSelector = null;

    switch (step.action) {
      case "click": {
        let el;
        try {
          await waitFor("visible", step.target, t);
          el = findOne(step.target);
        } catch (visErr) {
          // Selector may point to a CSS-hidden radio/checkbox — allow it
          const all = resolve(step.target);
          const allToggle = all.length > 0 && all.every((e) => {
            const tp = (e.getAttribute?.("type") || "").toLowerCase();
            return e.tagName === "INPUT" && (tp === "radio" || tp === "checkbox");
          });
          if (allToggle) {
            await waitFor("attached", step.target, t);
            el = findOne(step.target, { allowHidden: true });
          } else if (step.selectorFallback) {
            await waitFor("visible", step.selectorFallback, t);
            el = findOne(step.selectorFallback);
          } else {
            throw visErr;
          }
        }
        el.scrollIntoView({ block: "center", behavior: "instant" });
        flashTarget(el);
        // Hidden radio/checkbox: prefer label click, then native setter fallback
        const elType = (el.getAttribute?.("type") || "").toLowerCase();
        if (!isVisible(el) && el.tagName === "INPUT" && (elType === "radio" || elType === "checkbox")) {
          const doc2 = _currentDoc || document;
          const lbl = el.id
            ? doc2.querySelector(`label[for="${cssEscape(el.id)}"]`)
            : el.closest("label");
          if (lbl && isVisible(lbl)) {
            lbl.click();
          } else {
            el.click();
            if (!el.checked) {
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked")?.set;
              if (setter) setter.call(el, true);
              el.dispatchEvent(new Event("change", { bubbles: true }));
              el.dispatchEvent(new Event("input", { bubbles: true }));
            }
          }
        } else {
          // For option-like elements, fire mousemove first so react-select registers hover/focus.
          const elRole = (el.getAttribute('role') || '').toLowerCase();
          if (['option', 'menuitem', 'menuitemradio'].includes(elRole)) {
            el.scrollIntoView({ block: 'nearest' });
            el.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, cancelable: true }));
          }
          // Always dispatch the full pointer+mouse sequence before click.
          // React-select, Angular Material, Radix UI etc. gate on pointerdown/mousedown —
          // a bare el.click() skips those and fails to open or commit dropdown interactions.
          // REQ-18: modifier flags (cmd/ctrl/shift/alt) ride on every event so a
          // handler that checks e.metaKey/ctrlKey sees the chord.
          const mods = mouseModifiers(step.modifiers);
          const mopts = { bubbles: true, cancelable: true, ...mods };
          el.dispatchEvent(new MouseEvent('pointerdown', mopts));
          el.dispatchEvent(new MouseEvent('mousedown',   mopts));
          el.dispatchEvent(new MouseEvent('mouseup',     mopts));
          el.dispatchEvent(new MouseEvent('pointerup',   mopts));
          // With modifiers, dispatch a click event carrying them (el.click()
          // can't); without, el.click() keeps native anchor/label activation.
          if (mods.ctrlKey || mods.metaKey || mods.shiftKey || mods.altKey) {
            el.dispatchEvent(new MouseEvent('click', mopts));
          } else {
            el.click();
          }
        }
        return {};
      }
      case "right_click": {
        // REQ-17: synthetic context-menu open. We never rely on the OS menu
        // (unreachable from the page); a page that binds `contextmenu` gets its
        // custom menu, and we surface a note when nothing appears to react.
        await waitFor("visible", step.target, t);
        const el = findOne(step.target);
        el.scrollIntoView({ block: "center", behavior: "instant" });
        flashTarget(el);
        const opts = { bubbles: true, cancelable: true, button: 2, buttons: 2 };
        el.dispatchEvent(new MouseEvent("pointerdown", opts));
        el.dispatchEvent(new MouseEvent("mousedown", opts));
        const handled = !el.dispatchEvent(new MouseEvent("contextmenu", opts)); // preventDefault → false
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("pointerup", opts));
        const text = (el.textContent || "").trim().slice(0, 60);
        return { actual: `right-clicked <${el.tagName.toLowerCase()}>${text ? ` "${text}"` : ""}${handled ? "" : " (no page contextmenu handler reacted — a native OS menu can't be driven)"}` };
      }
      case "right_click_at": {
        // Coordinate right-click — the last-resort variant, paralleling click_at.
        const { x, y, el } = resolveViewportPoint(step.x, step.y, "right_click_at");
        flashTarget(el);
        const opts = { bubbles: true, cancelable: true, button: 2, buttons: 2, clientX: x, clientY: y };
        el.dispatchEvent(new MouseEvent("pointerdown", opts));
        el.dispatchEvent(new MouseEvent("mousedown", opts));
        const handled = !el.dispatchEvent(new MouseEvent("contextmenu", opts));
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("pointerup", opts));
        return { actual: `right-clicked at (${x},${y})${handled ? "" : " (no page contextmenu handler reacted)"}` };
      }
      case "triple_click": {
        // REQ-17: select the whole line/field contents — for clearing a
        // rich-text/contentEditable field before typing. Fires three clicks
        // (detail 1/2/3) and selects the element's text so a follow-up type
        // replaces it.
        await waitFor("visible", step.target, t);
        const el = findOne(step.target);
        el.scrollIntoView({ block: "center", behavior: "instant" });
        flashTarget(el);
        for (let d = 1; d <= 3; d++) {
          el.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true, detail: d }));
          el.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true, detail: d }));
          el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, detail: d }));
        }
        try {
          if (typeof el.select === "function") el.select();
          else {
            const sel = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(el);
            sel.removeAllRanges();
            sel.addRange(range);
          }
        } catch { /* selection is best-effort */ }
        return { actual: "triple-clicked (selected contents)" };
      }
      case "click_at": {
        // Coordinate click — the visual-fallback primitive.
        const { x, y, el } = resolveViewportPoint(step.x, step.y, "click_at");
        flashTarget(el);
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y };
        el.dispatchEvent(new MouseEvent("pointerdown", opts));
        el.dispatchEvent(new MouseEvent("mousedown", opts));
        el.dispatchEvent(new MouseEvent("mouseup", opts));
        el.dispatchEvent(new MouseEvent("pointerup", opts));
        if (typeof el.click === "function") el.click();
        const text = (el.textContent || "").trim().slice(0, 60);
        return { actual: `clicked <${el.tagName.toLowerCase()}>${text ? ` "${text}"` : ""} at (${x},${y})` };
      }
      case "hover_at": {
        // Coordinate hover — for canvas apps, custom tooltip triggers, and
        // drag-start patterns keyed to pointer position rather than element
        // identity. Fuller event sequence than element-targeted "hover" below:
        // pointermove/mousemove matter for CSS/JS hover state on some widgets.
        const { x, y, el } = resolveViewportPoint(step.x, step.y, "hover_at");
        flashTarget(el);
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y };
        el.dispatchEvent(new MouseEvent("pointermove", opts));
        el.dispatchEvent(new MouseEvent("mouseover", opts));
        el.dispatchEvent(new MouseEvent("mousemove", opts));
        const text = (el.textContent || "").trim().slice(0, 60);
        return { actual: `hovered <${el.tagName.toLowerCase()}>${text ? ` "${text}"` : ""} at (${x},${y})` };
      }
      case "dblclick": {
        await waitFor("visible", step.target, t);
        const el = findOne(step.target);
        el.scrollIntoView({ block: "center", behavior: "instant" });
        flashTarget(el);
        el.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, cancelable: true, ...mouseModifiers(step.modifiers) }));
        return {};
      }
      case "hover": {
        await waitFor("visible", step.target, t);
        const el = findOne(step.target);
        flashTarget(el);
        el.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true }));
        el.dispatchEvent(new MouseEvent("mouseenter", { bubbles: true, cancelable: true }));
        return {};
      }
      case "type": {
        let el;
        try {
          await waitFor("visible", step.target, t);
          el = findOne(step.target);
        } catch (visErr) {
          if (step.selectorFallback) {
            await waitFor("visible", step.selectorFallback, t);
            el = findOne(step.selectorFallback);
          } else {
            throw visErr;
          }
        }
        flashTarget(el);
        typeInto(el, String(step.value ?? ""));
        return {};
      }
      case "clear": {
        await waitFor("visible", step.target, t);
        const el = findOne(step.target);
        flashTarget(el);
        typeInto(el, "");
        return {};
      }
      case "select": {
        await waitFor("visible", step.target, t);
        let el = findOne(step.target);
        // SELECT is mapped to implicit role="combobox" by the recorder, so the
        // ladder's first rung is often role=combobox[name=...] and may resolve to
        // a wrapper (or a hybrid picker's visible trigger) instead of the real
        // native <select>. Recover by descending into a child <select> when present.
        if (el.tagName !== "SELECT") {
          const innerSelect = el.querySelector && el.querySelector("select");
          if (innerSelect) el = innerSelect;
        }
        flashTarget(el);
        if (el.tagName === "SELECT") {
          const wanted = String(step.value ?? "");
          const wantedLc = wanted.toLowerCase().trim();
          const optText = (o) => (o.textContent || "").trim();
          const options = Array.from(el.options);
          const match =
            options.find((o) => o.value === wanted) ||
            options.find((o) => optText(o).toLowerCase() === wantedLc) ||
            options.find((o) => optText(o).toLowerCase().includes(wantedLc));
          if (!match) {
            throw new Error(
              `select: option "${wanted}" not found among ${options.length} <option>s`,
            );
          }
          // Invalidate React's value tracker so controlled <select>s don't revert
          // the value on the next render (safe no-op when React isn't managing this).
          if (el._valueTracker) el._valueTracker.setValue("");
          el.selectedIndex = match.index;
          // input first, then change — order React's synthetic event system expects.
          el.dispatchEvent(new Event("input",  { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          if (el.value !== match.value) {
            throw new Error(
              `select: value did not commit (expected "${match.value}", got "${el.value}")`,
            );
          }
        } else {
          // If the recorded selector resolved to the inner combobox input
          // (react-select v5+ tags its hidden input with role="combobox"),
          // walk up to the control wrapper — pointerdown on the bare input
          // does not open the menu.
          if (el.tagName === "INPUT" && el.getAttribute("role") === "combobox") {
            const wrapper = findReactSelectControlAncestor(el);
            if (wrapper) el = wrapper;
          }
          // Custom dropdown (React Select, Ant Design, Zoho, etc.): click to open, then pick option.
          el.scrollIntoView({ block: "center", behavior: "instant" });
          el.focus();

          // Fire the complete pointer+mouse sequence needed to open the dropdown.
          // Prefer clicking the react-select dropdown indicator (the arrow) because its
          // onMouseDown always calls onMenuToggle(), regardless of the openMenuOnClick prop.
          // Fall back to firing on the control container if no indicator is found.
          const dropdownIndicator =
            Array.from(el.querySelectorAll('[aria-hidden="true"]')).pop() || null;
          const fireTrigger = () => {
            const t = dropdownIndicator || el;
            t.dispatchEvent(new MouseEvent("pointerdown", { bubbles: true, cancelable: true }));
            t.dispatchEvent(new MouseEvent("mousedown",   { bubbles: true, cancelable: true }));
            t.dispatchEvent(new MouseEvent("mouseup",     { bubbles: true, cancelable: true }));
            t.dispatchEvent(new MouseEvent("pointerup",   { bubbles: true, cancelable: true }));
            t.click();
          };
          // Keyboard fallback: focus the react-select input and press ArrowDown.
          // React-select's onKeyDown always opens the menu for ArrowDown regardless of config.
          const fireInputOpen = () => {
            const input = el.querySelector('input');
            if (!input) return;
            input.focus();
            input.dispatchEvent(new KeyboardEvent('keydown', {
              key: 'ArrowDown', code: 'ArrowDown', keyCode: 40, which: 40,
              bubbles: true, cancelable: true,
            }));
          };

          // Query visible option candidates across both _currentDoc and document so that
          // portal-based dropdowns (React portals, Ant Design, Radix UI) are found even
          // when _currentDoc points to an iframe. [class*="item"] removed — it matches nearly
          // every styled element on most pages causing false-positive picks.
          const queryCandidates = () => {
            const optSelector =
              '[role="option"],[role="menuitem"],[role="menuitemradio"],[role="listitem"],' +
              'li[class],[class*="option"],[class*="menu-item"],[class*="select-item"]';
            const docs = [_currentDoc || document];
            if (_currentDoc && _currentDoc !== document) docs.push(document);
            const seen = new Set();
            const results = [];
            for (const d of docs) {
              for (const el of d.querySelectorAll(optSelector)) {
                if (!seen.has(el) && isVisible(el)) { seen.add(el); results.push(el); }
              }
            }
            return results;
          };

          // Fire trigger, then verify the dropdown actually opened within 1000ms.
          // If no candidates appear, re-fire once before falling through to the main poll.
          fireTrigger();
          const opened = await new Promise((resolve) => {
            const deadline = Date.now() + 1000;
            const check = () => {
              if (queryCandidates().length > 0) { resolve(true); return; }
              if (Date.now() < deadline) setTimeout(check, 50);
              else resolve(false);
            };
            check();
          });
          if (!opened) {
            // Keyboard fallback: focus input + ArrowDown, then mouse fallback
            fireInputOpen();
            await new Promise(r => setTimeout(r, 300));
            if (queryCandidates().length === 0) {
              fireTrigger();
              await new Promise(r => setTimeout(r, 80));
            }
          }

          const targetText = String(step.value ?? "").toLowerCase().trim();
          // innerText strips hidden/icon child nodes better than textContent for rich dropdowns.
          const optText = c => (c.innerText || c.textContent || "").trim();

          // Wait up to 3s for the matching option to appear.
          const optionEl = await new Promise((resolve) => {
            const deadline = Date.now() + 3000;
            const poll = () => {
              const candidates = queryCandidates();
              const match =
                candidates.find(c => optText(c) === step.value) ||
                candidates.find(c => optText(c).toLowerCase() === targetText) ||
                candidates.find(c => optText(c).toLowerCase().includes(targetText));
              if (match) { resolve(match); return; }
              if (Date.now() < deadline) setTimeout(poll, 80);
              else resolve(null);
            };
            poll();
          });
          if (!optionEl) throw new Error(`select: option "${step.value}" not found in custom dropdown`);

          // Stale element guard: React may reconcile between poll-resolve and click.
          // A detached element's click() silently does nothing — no error, no selection.
          let target = optionEl;
          if (!target.isConnected) {
            target = queryCandidates().find(c => optText(c) === step.value) ||
                     queryCandidates().find(c => optText(c).toLowerCase() === targetText) ||
                     queryCandidates().find(c => optText(c).toLowerCase().includes(targetText));
            if (!target) throw new Error(`select: option "${step.value}" became detached and could not be re-found`);
          }

          // Full pointer sequence on option click: React Select v5+, Radix UI, and Headless UI
          // use onPointerDown on options to cancel the blur that would close the dropdown,
          // then onClick to commit selection. A bare click() skips this and loses the selection.
          target.scrollIntoView({ block: "nearest" });
          const rect = target.getBoundingClientRect();
          const coords = {
            clientX: Math.round(rect.left + rect.width / 2),
            clientY: Math.round(rect.top + rect.height / 2),
            button: 0,
          };
          // Hover first — react-select v5 uses onMouseMove to set highlightedIndex.
          // Without a highlighted option, some commit paths silently no-op.
          target.dispatchEvent(new MouseEvent("mouseover", { bubbles: true, cancelable: true, ...coords }));
          target.dispatchEvent(new MouseEvent("mouseenter", { bubbles: false, cancelable: false, ...coords }));
          target.dispatchEvent(new MouseEvent("mousemove",  { bubbles: true, cancelable: true, ...coords }));
          target.dispatchEvent(new MouseEvent("pointerdown",{ bubbles: true, cancelable: true, ...coords }));
          target.dispatchEvent(new MouseEvent("mousedown",  { bubbles: true, cancelable: true, ...coords }));
          target.dispatchEvent(new MouseEvent("mouseup",    { bubbles: true, cancelable: true, ...coords }));
          target.dispatchEvent(new MouseEvent("pointerup",  { bubbles: true, cancelable: true, ...coords }));
          target.click();

          // Verify selection committed: if the menu is still showing options, the
          // mouse path silently no-opped (happens on some react-select builds where
          // the option only commits via keyboard). Poll up to 350ms but exit as soon
          // as the menu closes, so a successful click doesn't always cost 350ms.
          for (let waited = 0; waited < 350 && queryCandidates().length > 0; waited += 50) {
            await sleep(50);
          }
          if (queryCandidates().length > 0) {
            const input = el.querySelector('input') ||
                          el.parentElement?.querySelector('input[role="combobox"]');
            if (input) {
              input.focus();
              const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
              if (setter) setter.call(input, step.value);
              else input.value = step.value;
              input.dispatchEvent(new InputEvent("input", {
                bubbles: true, cancelable: true,
                inputType: "insertText", data: String(step.value),
              }));
              // Let react-select filter & re-render the option list — poll up to
              // 250ms, exiting as soon as filtered options appear.
              for (let waited = 0; waited < 250 && queryCandidates().length === 0; waited += 50) {
                await sleep(50);
              }
              input.dispatchEvent(new KeyboardEvent("keydown", {
                key: "Enter", code: "Enter", keyCode: 13, which: 13,
                bubbles: true, cancelable: true,
              }));
              input.dispatchEvent(new KeyboardEvent("keyup", {
                key: "Enter", code: "Enter", keyCode: 13, which: 13,
                bubbles: true, cancelable: true,
              }));
            }
          }
        }
        return {};
      }
      case "check":
      case "uncheck": {
        // Use "attached" — radio/checkbox inputs are often visually hidden in styled forms
        let el;
        try {
          await waitFor("attached", step.target, t);
          el = findOne(step.target, { allowHidden: true });
        } catch (attachErr) {
          if (step.selectorFallback) {
            await waitFor("attached", step.selectorFallback, t);
            el = findOne(step.selectorFallback, { allowHidden: true });
          } else {
            throw attachErr;
          }
        }
        const want = step.action === "check";
        // Flash the visible label when present (the real input is often CSS-hidden).
        {
          const doc2 = _currentDoc || document;
          const flashLbl = el.id
            ? doc2.querySelector(`label[for="${cssEscape(el.id)}"]`)
            : el.closest("label");
          flashTarget(flashLbl && isVisible(flashLbl) ? flashLbl : el);
        }
        if (el.checked !== want) {
          // Prefer clicking the associated <label> (handles hidden inputs in styled/quiz forms)
          const doc2 = _currentDoc || document;
          const lbl = el.id
            ? doc2.querySelector(`label[for="${cssEscape(el.id)}"]`)
            : el.closest("label");
          if (lbl && isVisible(lbl)) {
            lbl.click();
          } else {
            el.click();
          }
          // Native setter fallback if click alone didn't flip the state
          if (el.checked !== want) {
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked")?.set;
            if (setter) setter.call(el, want);
            el.dispatchEvent(new Event("change", { bubbles: true }));
            el.dispatchEvent(new Event("input", { bubbles: true }));
          }
        }
        return {};
      }
      case "press_key": {
        pressKey(step.target, step.value);
        return {};
      }
      case "scroll": {
        if (step.target) {
          const el = findOne(step.target);
          if (step.value === "into_view" || step.value == null) {
            el.scrollIntoView({ block: "center" });
          } else if (step.value === "bottom") {
            el.scrollTop = el.scrollHeight;
          } else {
            el.scrollTop = Number(step.value) || 0;
          }
        } else {
          if (step.value === "bottom") {
            window.scrollTo(0, document.body.scrollHeight);
          } else {
            window.scrollBy(0, Number(step.value) || 0);
          }
        }
        return {};
      }
      case "scroll_to": {
        // Like "scroll" but ref/selector-driven and reports the settled rect,
        // so the agent (and vision) knows the target landed on-screen —
        // ref=N (from read_page/find) is already a first-class selector via
        // resolve(), so no new ref plumbing is needed here.
        await waitFor("attached", step.target, t);
        const el = findOne(step.target, { allowHidden: true });
        el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        // Settle wait: instant scrolling lands within a couple of rAF ticks —
        // no smooth-scroll is used elsewhere in this codebase, so this stays
        // consistent rather than introducing a new async-polling primitive.
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        const r = el.getBoundingClientRect();
        const rect = { x: Math.round(r.x), y: Math.round(r.y), width: Math.round(r.width), height: Math.round(r.height) };
        return { actual: `scrolled into view at (${rect.x},${rect.y},${rect.width}x${rect.height})`, rect };
      }
      case "zoom_region": {
        // Resolves a target/ref (+ padding) into a CSS-pixel rect for REQ-08's
        // zoom action. The actual screenshot capture/crop happens in
        // background.js (only it can call chrome.tabs.captureVisibleTab) —
        // this only answers "what rect, in what DPR".
        await waitFor("attached", step.target, t);
        const el = findOne(step.target, { allowHidden: true });
        el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
        await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
        const r = el.getBoundingClientRect();
        const pad = Number(step.padding) || 0;
        const vw = window.innerWidth, vh = window.innerHeight;
        const x = Math.max(0, Math.round(r.x - pad));
        const y = Math.max(0, Math.round(r.y - pad));
        const rect = {
          x, y,
          width: Math.max(1, Math.min(vw - x, Math.round(r.width + pad * 2))),
          height: Math.max(1, Math.min(vh - y, Math.round(r.height + pad * 2))),
        };
        return { rect, dpr: window.devicePixelRatio || 1 };
      }
      case "wait": {
        await waitFor(step.condition || "visible", step.target, t);
        return {};
      }
      case "extract": {
        await waitFor("attached", step.target, t);
        const el = findOne(step.target, { allowHidden: true });
        let val;
        if (step.attr) {
          val =
            step.attr === "value"
              ? el.value ?? el.getAttribute("value")
              : el.getAttribute(step.attr);
        } else if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
          val = el.value;
        } else {
          val = (el.textContent || "").trim();
        }
        return { variable: step.value, value: val };
      }
      case "summarize": {
        const scope = step.target
          ? findOne(step.target)
          : (_currentDoc || document).body;
        const doc = _currentDoc || document;
        const meta = `URL: ${doc.defaultView?.location.href || location.href}\nTitle: ${doc.title}\n\n`;
        // Preserve line breaks — collapsing ALL whitespace flattens code diffs,
        // tables, and lists into one unreviewable line. Only squeeze runs of
        // spaces/tabs and cap blank-line runs.
        const body = (scope.innerText || "")
          .replace(/[ \t]+/g, " ")
          .replace(/\n{3,}/g, "\n\n")
          .trim()
          .slice(0, 15000);
        return { variable: step.value, value: meta + body };
      }
      case "assert": {
        await waitFor(inferAssertWait(step.matcher), step.target, t).catch(() => {});
        const actual = getActual({
          target: step.target,
          matcher: step.matcher || "equals",
          attr: step.attr,
        });
        const passed = compare(actual, step.matcher || "equals", step.expected);
        return {
          actual,
          passed,
          reason: passed
            ? undefined
            : `expected ${JSON.stringify(step.expected)} but got ${JSON.stringify(actual)}`,
        };
      }

      // ---- new actions ----

      case "upload_file": {
        await waitFor("attached", step.target, t);
        const el = findOne(step.target, { allowHidden: true });
        if (el.tagName !== "INPUT" || (el.type || "").toLowerCase() !== "file") {
          throw new Error("upload_file: target must be <input type='file'>");
        }
        flashTarget(el);
        const content = step.value || step.content || "";
        const filename = step.filename || "upload.bin";
        const mimeType = step.mime_type || "application/octet-stream";
        let fileData;
        if (typeof content === "string" && content.startsWith("data:")) {
          const base64 = content.split(",")[1] || "";
          const bytes = atob(base64);
          const arr = new Uint8Array(bytes.length);
          for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
          fileData = arr.buffer;
        } else {
          fileData = String(content);
        }
        const file = new File([fileData], filename, { type: mimeType });
        const dt = new DataTransfer();
        dt.items.add(file);
        const nativeSetter = Object.getOwnPropertyDescriptor(
          window.HTMLInputElement.prototype,
          "files",
        )?.set;
        if (nativeSetter) {
          nativeSetter.call(el, dt.files);
        } else {
          Object.defineProperty(el, "files", { value: dt.files, configurable: true });
        }
        el.dispatchEvent(new Event("change", { bubbles: true }));
        el.dispatchEvent(new Event("input", { bubbles: true }));
        return {};
      }

      case "drop_file": {
        // Drag-and-drop image/file upload for drop zones with no <input
        // type="file"> (e.g. accept a "screenshot:last" artifact, already
        // resolved to a data: URL by the runner before this dispatches) —
        // combines upload_file's File/DataTransfer construction with drag's
        // event sequence, targeting an element or a viewport coordinate.
        const content = step.value || step.content || "";
        const filename = step.filename || "upload.bin";
        const mimeType = step.mime_type || "application/octet-stream";
        let fileData;
        if (typeof content === "string" && content.startsWith("data:")) {
          const base64 = content.split(",")[1] || "";
          const bytes = atob(base64);
          const arr = new Uint8Array(bytes.length);
          for (let i = 0; i < bytes.length; i++) arr[i] = bytes.charCodeAt(i);
          fileData = arr.buffer;
        } else {
          fileData = String(content);
        }
        const file = new File([fileData], filename, { type: mimeType });
        const dt = new DataTransfer();
        dt.items.add(file);

        let el, x, y;
        if (step.target) {
          await waitFor("visible", step.target, t);
          el = findOne(step.target);
          const r = el.getBoundingClientRect();
          x = r.x + r.width / 2;
          y = r.y + r.height / 2;
        } else {
          ({ x, y, el } = resolveViewportPoint(step.x, step.y, "drop_file"));
        }
        flashTarget(el);
        const opts = { bubbles: true, cancelable: true, dataTransfer: dt, clientX: x, clientY: y };
        el.dispatchEvent(new DragEvent("dragenter", opts));
        el.dispatchEvent(new DragEvent("dragover", opts));
        el.dispatchEvent(new DragEvent("drop", opts));
        return { actual: `dropped "${filename}" onto <${el.tagName.toLowerCase()}>` };
      }

      case "drag": {
        await waitFor("visible", step.target, t);
        const src = findOne(step.target);
        const dstSelector = step.destination || step.value;
        if (!dstSelector) throw new Error("drag: requires a destination field");
        await waitFor("visible", dstSelector, t);
        const dst = findOne(dstSelector);
        flashTarget(src);
        flashTarget(dst);
        const dt = new DataTransfer();
        src.dispatchEvent(new DragEvent("dragstart", { bubbles: true, cancelable: true, dataTransfer: dt }));
        dst.dispatchEvent(new DragEvent("dragenter", { bubbles: true, cancelable: true, dataTransfer: dt }));
        dst.dispatchEvent(new DragEvent("dragover", { bubbles: true, cancelable: true, dataTransfer: dt }));
        dst.dispatchEvent(new DragEvent("drop", { bubbles: true, cancelable: true, dataTransfer: dt }));
        src.dispatchEvent(new DragEvent("dragend", { bubbles: true, cancelable: true, dataTransfer: dt }));
        return {};
      }

      case "switch_frame": {
        if (!step.target || step.target === "top" || step.target === "main") {
          _currentDoc = document;
          return {};
        }
        const iframe = findOne(step.target, { allowHidden: true });
        if (!iframe || iframe.tagName !== "IFRAME") {
          throw new Error("switch_frame: target must be an <iframe> element or 'top'");
        }
        const frameDoc = iframe.contentDocument;
        if (!frameDoc) {
          throw new Error("switch_frame: cannot access iframe document (may be cross-origin)");
        }
        _currentDoc = frameDoc;
        return {};
      }

      case "accessibility_audit": {
        const scope = step.target
          ? findOne(step.target, { allowHidden: true })
          : (_currentDoc || document).body;
        const violations = runA11yAudit(scope);
        return {
          variable: step.variable || step.value,
          value: violations.length,
          violations,
          actual: String(violations.length),
          passed: violations.length === 0,
        };
      }

      case "assert_performance": {
        const metrics = getPerformanceMetrics();
        const metric = String(step.metric || "LCP");
        const value = metrics[metric];
        const maxMs = step.max_ms != null ? Number(step.max_ms) : null;
        const passed = value != null && (maxMs === null || value <= maxMs);
        return {
          value,
          actual: value != null ? `${value}ms` : "unavailable",
          passed,
          metric,
          reason: !passed
            ? value == null
              ? `Metric ${metric} not yet available`
              : `${metric} = ${value}ms exceeds max ${maxMs}ms`
            : undefined,
        };
      }

      case "mock_network": {
        installNetworkMock({
          url: step.url || step.target,
          method: step.method,
          response: step.response || step.value,
          status: step.status || 200,
        });
        return {};
      }

      case "clear_network_mocks": {
        clearNetworkMocks();
        return {};
      }

      case "read_page": {
        const filter = step.filter === "all" ? "all" : "interactive";
        const maxChars = Number(step.max_chars) > 0 ? Number(step.max_chars) : 15000;
        const maxDepth = Number(step.max_depth) > 0 ? Number(step.max_depth) : Infinity;
        const ref = Number(step.ref) > 0 ? Number(step.ref) : null;
        let root;
        if (ref) {
          // A ref-scoped read must keep the registry generation the ref came
          // from — refreshing the snapshot here would renumber everything.
          root = _snapshotRegistry[ref - 1];
          if (!root || !root.isConnected) {
            throw new Error(`read_page: ref=${ref} is stale — the element left the page since the snapshot`);
          }
        } else {
          pageSnapshot(); // fresh registry so emitted refs are current
          root = (_currentDoc || document).body;
        }
        const tree = buildPageTree(root, { filter, maxDepth, maxChars });
        return {
          variable: step.variable || undefined,
          value: tree,
          actual: `read_page filter=${filter}${ref ? ` ref=${ref}` : ""}: ${tree.length} chars`,
        };
      }

      case "find": {
        const query = String(step.query || step.value || "").trim();
        if (!query) throw new Error("find requires a query");
        const limit = Number(step.limit) > 0 ? Math.min(Number(step.limit), 50) : 20;
        pageSnapshot(); // fresh registry so returned refs are current
        const matches = scoreFindCandidates(query, limit);
        const summary = matches.length
          ? matches.map((m) => `[${m.ref}] ${m.role || ""} ${JSON.stringify(m.name || m.snippet || "")} → ${m.selector}`).join("\n")
          : "no matches";
        return {
          variable: step.variable || undefined,
          value: summary,
          matches,
          // Top score ≥ 6 means substring/strong-token match — the runner skips
          // the LLM fallback pass in that case.
          confident: matches.length > 0 && matches[0].score >= 6,
          actual: `find ${JSON.stringify(query)}: ${matches.length} match(es)`,
        };
      }

      case "get_page_text": {
        const doc2 = _currentDoc || document;
        const scope = step.target
          ? findOne(step.target, { allowHidden: true })
          : pickMainContentEl(doc2);
        const maxChars = Number(step.max_chars) > 0 ? Number(step.max_chars) : 20000;
        let text = (scope?.innerText || "")
          .replace(/[ \t]+/g, " ")
          .replace(/\n{3,}/g, "\n\n")
          .trim();
        const total = text.length;
        const visual = detectVisualContent(total);
        if (!total) {
          // An empty string is an observation the model can't act on — say what
          // happened and what to do about it instead.
          return {
            variable: step.variable || undefined,
            value: visual
              ? "(no readable text — this page renders its content on canvas/images, which DOM text extraction cannot see; read it visually with request_screenshot and the page's next/previous controls)"
              : "(no visible text on the page — it may still be loading; wait for condition network_idle and read again)",
            actual: "read 0 of 0 chars",
          };
        }
        if (total > maxChars) {
          text = text.slice(0, maxChars) +
            `\n…TRUNCATED (${total} chars total) — pass a narrower target or a larger max_chars to read more`;
        } else if (visual && total < 200) {
          text += "\n\n(NOTE: the main content of this page is canvas/image-rendered — the text above is only the app shell; read the content visually with request_screenshot)";
        }
        return {
          variable: step.variable || undefined,
          value: text,
          actual: `read ${Math.min(total, maxChars)} of ${total} chars`,
        };
      }

      case "read_console_messages": {
        const limit = Number(step.limit) > 0 ? Number(step.limit) : 20;
        const match = buildTextFilter(step.filter);
        let entries = _qaConsoleLogs.map((e) => ({ level: e.level, text: e.args, ts: e.ts }));
        if (step.level) entries = entries.filter((e) => e.level === String(step.level).toLowerCase());
        if (match) entries = entries.filter((e) => match(e.text));
        const out = entries.slice(-limit);
        if (step.clear) _qaConsoleLogs = [];
        return {
          variable: step.variable || undefined,
          value: out.length ? JSON.stringify(out) : "no console messages captured",
          actual: `${out.length} console message(s)${_qaCapturing ? "" : " (capture not armed)"}`,
        };
      }

      case "datepick": {
        const dateStr = String(step.value || "");
        if (!dateStr) throw new Error("datepick: no date value provided");
        // Prefer isoDate (YYYY-MM-DD, unambiguous) recorded at capture time.
        // Fall back to best-guess parse of the raw display value for old recordings.
        const parsed = parseDateForPick(step.isoDate || dateStr);
        if (!parsed) throw new Error(`datepick: cannot parse date "${dateStr}"`);
        const doc2 = _currentDoc || document;

        // Locate the trigger input recorded alongside the container
        let inp = null;
        if (step.inputTarget) {
          try {
            await waitFor("attached", step.inputTarget, t);
            inp = findOne(step.inputTarget, { allowHidden: true });
          } catch (_) {}
        }

        // Fast-path: set value directly on the input using the native setter.
        // This is jQuery-version-agnostic — avoids the jQuery internal data-key
        // mismatch that occurs when multiple jQuery/jQuery UI versions are loaded
        // (e.g. 1.6 + UI 1.8 alongside 3.x + UI 1.14).  dateStr is the raw display
        // value recorded from the input, so it is already in the picker's own format.
        if (inp) {
          const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
          if (nativeSetter) nativeSetter.call(inp, dateStr);
          else inp.value = dateStr;
          inp.dispatchEvent(new Event("change", { bubbles: true }));
          inp.dispatchEvent(new Event("input",  { bubbles: true }));
          // Flatpickr: also sync its internal selectedDates state
          if (inp._flatpickr) {
            inp._flatpickr.setDate(new Date(parsed.year, parsed.month, parsed.day), false);
          }
          return {};
        }

        // Helper: find the first visible calendar container
        const findOpenCalendar = () => {
          const candidates = [
            doc2.querySelector("#ui-datepicker-div"),
            doc2.querySelector(".flatpickr-calendar.open"),
            doc2.querySelector('[class*="react-datepicker__month-container"]'),
            doc2.querySelector('[class*="ant-picker-dropdown"]:not([class*="hidden"])'),
          ];
          return candidates.find(el => el && isVisible(el)) || null;
        };

        // No inputTarget (old recordings): open the calendar via native DOM events.
        // Native FocusEvent triggers jQuery UI's focus handler regardless of which
        // jQuery version is current — no jQuery API call needed.
        for (const jqInp of doc2.querySelectorAll("input.hasDatepicker")) {
          jqInp.scrollIntoView({ block: "center", behavior: "instant" });
          jqInp.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
          jqInp.dispatchEvent(new FocusEvent("focus", { bubbles: true }));
          // Poll up to 300ms for the calendar to open, exiting as soon as it appears.
          for (let waited = 0; waited < 300 && !findOpenCalendar(); waited += 50) {
            await sleep(50);
          }
          if (findOpenCalendar()) { inp = jqInp; break; }
        }

        // Wait briefly for calendar to appear (max 3 s to avoid long hangs)
        let calContainer = findOpenCalendar();
        if (!calContainer) {
          await new Promise(r => setTimeout(r, 400));
          calContainer = findOpenCalendar();
        }
        if (!calContainer) throw new Error("datepick: calendar did not open");

        // Navigate to the correct month/year (up to 24 steps)
        const hdrSelector =
          ".ui-datepicker-title,.flatpickr-current-month," +
          ".react-datepicker__current-month,[class*='calendar-header'],[class*='month-year'],[class*='picker-header']";
        for (let nav = 0; nav < 24; nav++) {
          const hdrEl = calContainer.querySelector(hdrSelector);
          if (!hdrEl) break;
          const hdrBefore = (hdrEl.textContent || "").trim();
          const cur = parseDisplayedMonthYear(hdrBefore);
          if (!cur) break;
          if (cur.year === parsed.year && cur.month === parsed.month) break;
          const goNext = cur.year < parsed.year || (cur.year === parsed.year && cur.month < parsed.month);
          const navBtn = calContainer.querySelector(
            goNext
              ? '[class*="next"]:not([disabled]),.ui-datepicker-next,[aria-label*="next" i]'
              : '[class*="prev"]:not([disabled]),.ui-datepicker-prev,[aria-label*="prev" i]'
          );
          if (!navBtn) break;
          navBtn.click();
          // Poll up to 200ms for the header to update, exiting as soon as it changes.
          for (let waited = 0; waited < 200; waited += 40) {
            await sleep(40);
            if ((calContainer.querySelector(hdrSelector)?.textContent || "").trim() !== hdrBefore) break;
          }
        }

        // Click the target day cell (exclude overflow "other month" cells)
        const dayStr = String(parsed.day);
        const dayCandidates = [...calContainer.querySelectorAll("td a,td span,td button,[class*='day']")]
          .filter(el => {
            if ((el.textContent || "").trim() !== dayStr) return false;
            const cell = el.closest("td");
            if (cell?.classList.contains("ui-datepicker-other-month")) return false;
            if (cell?.classList.contains("react-datepicker__day--outside-month")) return false;
            if (el.classList.contains("prevMonthDay") || el.classList.contains("nextMonthDay")) return false;
            return true;
          });
        if (dayCandidates.length === 0) throw new Error(`datepick: day ${dayStr} not found in calendar`);
        dayCandidates[0].scrollIntoView({ block: "nearest" });
        dayCandidates[0].click();
        await new Promise(r => setTimeout(r, 300));
        return {};
      }

      default:
        throw new Error(`unsupported action: ${step.action}`);
    }
  }

  function inferAssertWait(matcher) {
    if (matcher === "absent" || matcher === "hidden") return "detached";
    if (matcher === "count") return "attached";
    return "visible";
  }

  // ---------- accessibility audit ----------

  function runA11yAudit(scope) {
    const violations = [];

    scope.querySelectorAll("img").forEach((img) => {
      if (
        !img.getAttribute("alt") &&
        !img.getAttribute("aria-label") &&
        img.getAttribute("role") !== "presentation" &&
        img.getAttribute("role") !== "none"
      ) {
        violations.push({
          rule: "img-alt",
          element: img.outerHTML.slice(0, 120),
          message: "Image missing alt attribute",
        });
      }
    });

    scope.querySelectorAll("button, [role='button']").forEach((btn) => {
      if (!getAccessibleName(btn)) {
        violations.push({
          rule: "button-name",
          element: btn.outerHTML.slice(0, 120),
          message: "Button has no accessible name",
        });
      }
    });

    scope
      .querySelectorAll(
        "input:not([type='hidden']):not([type='submit']):not([type='reset']):not([type='button'])," +
          "textarea, select",
      )
      .forEach((input) => {
        if (!getAccessibleName(input)) {
          violations.push({
            rule: "label",
            element: input.outerHTML.slice(0, 120),
            message: "Form input has no associated label",
          });
        }
      });

    scope.querySelectorAll("a[href]").forEach((link) => {
      if (!getAccessibleName(link)) {
        violations.push({
          rule: "link-name",
          element: link.outerHTML.slice(0, 120),
          message: "Link has no accessible name",
        });
      }
    });

    const seenIds = new Map();
    scope.querySelectorAll("[id]").forEach((el) => {
      const id = el.id;
      if (seenIds.has(id)) {
        const first = seenIds.get(id);
        if (!first._auditReported) {
          first._auditReported = true;
          violations.push({ rule: "duplicate-id", element: id, message: `Duplicate id="${id}"` });
        }
      } else {
        seenIds.set(id, el);
      }
    });

    return violations;
  }

  // ---------- performance metrics ----------

  function getPerformanceMetrics() {
    const metrics = {};
    try {
      const nav = performance.getEntriesByType("navigation")[0];
      if (nav) {
        metrics["TTFB"] = Math.round(nav.responseStart - nav.requestStart);
        metrics["DOMContentLoaded"] = Math.round(nav.domContentLoadedEventEnd - nav.startTime);
        metrics["Load"] = Math.round(nav.loadEventEnd - nav.startTime);
      }
      for (const entry of performance.getEntriesByType("paint")) {
        if (entry.name === "first-paint") metrics["FP"] = Math.round(entry.startTime);
        if (entry.name === "first-contentful-paint") metrics["FCP"] = Math.round(entry.startTime);
      }
      const lcpEntries = performance.getEntriesByType("largest-contentful-paint");
      if (lcpEntries.length) {
        metrics["LCP"] = Math.round(lcpEntries[lcpEntries.length - 1].startTime);
      }
    } catch {}
    return metrics;
  }

  // ---------- QA Debug Mode / agent-loop capture ----------

  // Substring or /regex/ filter used by the read_console_messages /
  // read_network_requests actions. Returns null (no filtering) for empty input.
  function buildTextFilter(filter) {
    const f = String(filter || "").trim();
    if (!f) return null;
    const m = f.match(/^\/(.+)\/([a-z]*)$/);
    if (m) {
      try {
        const re = new RegExp(m[1], m[2]);
        return (text) => re.test(String(text));
      } catch { /* fall through to substring */ }
    }
    const needle = f.toLowerCase();
    return (text) => String(text).toLowerCase().includes(needle);
  }

  // Credential-shaped fields never leave the page in the QA capture buffers —
  // this is defense-in-depth for the QA-Debug-Mode root-cause path (the only
  // path that forwards raw bodies to the model; read_network_requests already
  // strips bodies entirely). Handles JSON bodies (walks keys) and falls back
  // to a key=value regex for form-encoded/plain-text bodies and console args.
  const QA_SENSITIVE_KEY_RE = /password|token|secret|auth(?:orization)?|api[_-]?key/i;
  const QA_FALLBACK_REDACT_RE = /((?:password|token|secret|auth(?:orization)?|api[_-]?key)\s*[:=]\s*)("?)([^&"'\n,}]+)\2/gi;
  function redactCredentialShapedText(str) {
    if (typeof str !== "string" || !str) return str;
    try {
      const obj = JSON.parse(str);
      const walk = (o) => {
        if (!o || typeof o !== "object") return;
        for (const k of Object.keys(o)) {
          if (QA_SENSITIVE_KEY_RE.test(k)) o[k] = "[redacted]";
          else if (o[k] && typeof o[k] === "object") walk(o[k]);
        }
      };
      walk(obj);
      return JSON.stringify(obj);
    } catch {
      return str.replace(QA_FALLBACK_REDACT_RE, "$1$2[redacted]$2");
    }
  }

  // Idempotent: safe to call while already capturing. `reset` (default true —
  // the per-step QA Debug semantics) clears the buffers; the agent loop arms
  // with { reset: false } once per run so entries accumulate across steps.
  // Console wrappers are installed only once per capture session, so repeated
  // calls never stack wrapper-on-wrapper.
  let _qaNetListenerInstalled = false;
  // F-10: the channel name is randomized per page-injection (mirrors
  // evalInPageWorld's per-call random id, content.js:42) so a page script
  // can't forge "observed network traffic" by guessing a fixed event name.
  // Generated once and reused across startQACapture() calls in this page's
  // life, matching window.__qa_fetch_installed's own persistence contract.
  let _qaNetEventName = null;
  function startQACapture({ reset = true } = {}) {
    if (reset || !_qaCapturing) {
      _qaConsoleLogs = [];
      _qaNetworkEvents = [];
    }
    const alreadyCapturing = _qaCapturing;
    _qaCapturing = true;

    if (!alreadyCapturing) {
      for (const level of ["log", "warn", "error", "info", "debug"]) {
        _qaOriginalConsole[level] = console[level].bind(console);
        console[level] = function (...args) {
          _qaOriginalConsole[level](...args);
          if (_qaConsoleLogs.length < 50) {
            let argStr;
            try { argStr = JSON.stringify(args); } catch { argStr = String(args); }
            argStr = redactCredentialShapedText(argStr);
            _qaConsoleLogs.push({ level, args: argStr.slice(0, 500), ts: Math.round(performance.now()) });
          }
        };
      }
    }

    if (!_qaNetEventName) _qaNetEventName = "__qa_net_" + Math.random().toString(36).slice(2);
    if (!_qaNetListenerInstalled) {
      _qaNetListenerInstalled = true;
      window.addEventListener(_qaNetEventName, (e) => {
        if (!_qaCapturing || _qaNetworkEvents.length >= 30) return;
        const d = e.detail;
        if (!d || typeof d !== "object") return;
        // F-10: never trust the page's payload shape — whitelist and cap
        // every field instead of pushing e.detail verbatim.
        _qaNetworkEvents.push({
          url: typeof d.url === "string" ? d.url.slice(0, 2048) : "",
          method: typeof d.method === "string" ? d.method.slice(0, 16) : "GET",
          status: typeof d.status === "number" ? d.status : null,
          ok: typeof d.ok === "boolean" ? d.ok : null,
          durationMs: typeof d.durationMs === "number" ? d.durationMs : null,
          requestBody: typeof d.requestBody === "string" ? d.requestBody.slice(0, 1024) : null,
          responseBody: typeof d.responseBody === "string" ? d.responseBody.slice(0, 2048) : null,
          error: typeof d.error === "string" ? d.error.slice(0, 500) : null,
        });
      });
    }

    evalInPageWorld(`(function(){
      if (window.__qa_fetch_installed) return;
      window.__qa_fetch_installed = true;
      const _real = window.fetch;
      const __qaEventName = ${JSON.stringify(_qaNetEventName)};
      const __baKeyRe = new RegExp(${JSON.stringify(QA_SENSITIVE_KEY_RE.source)}, ${JSON.stringify(QA_SENSITIVE_KEY_RE.flags)});
      const __baFallbackRe = new RegExp(${JSON.stringify(QA_FALLBACK_REDACT_RE.source)}, ${JSON.stringify(QA_FALLBACK_REDACT_RE.flags)});
      const __baQaRedact = (s) => {
        if (typeof s !== 'string' || !s) return s;
        try {
          const obj = JSON.parse(s);
          const walk = (o) => {
            if (!o || typeof o !== 'object') return;
            for (const k of Object.keys(o)) {
              if (__baKeyRe.test(k)) o[k] = '[redacted]';
              else if (o[k] && typeof o[k] === 'object') walk(o[k]);
            }
          };
          walk(obj);
          return JSON.stringify(obj);
        } catch {
          return s.replace(__baFallbackRe, '$1$2[redacted]$2');
        }
      };
      window.fetch = async function(...args) {
        const url = typeof args[0]==='string' ? args[0] : (args[0] instanceof URL ? args[0].href : (args[0]?.url||''));
        const method = ((args[1]?.method)||'GET').toUpperCase();
        let reqBody = null;
        if (['POST','PUT','PATCH','DELETE'].includes(method) && args[1]?.body != null) {
          try { reqBody = __baQaRedact(String(args[1].body)).slice(0,1024); } catch {}
        }
        const t0 = performance.now();
        let status=null,ok=null,respBody=null,err=null;
        try {
          const resp = await _real.apply(this,args);
          status=resp.status; ok=resp.ok;
          const dur=Math.round(performance.now()-t0);
          if (!resp.ok) { try { respBody=__baQaRedact(await resp.clone().text()).slice(0,2048); } catch {} }
          window.dispatchEvent(new CustomEvent(__qaEventName,{detail:{url,method,status,ok,durationMs:dur,requestBody:reqBody,responseBody:respBody,error:null}}));
          return resp;
        } catch(e) {
          const dur=Math.round(performance.now()-t0);
          window.dispatchEvent(new CustomEvent(__qaEventName,{detail:{url,method,status:null,ok:null,durationMs:dur,requestBody:reqBody,responseBody:null,error:e.message||String(e)}}));
          throw e;
        }
      };
    })()`).catch(() => {});
  }

  function stopQACapture() {
    _qaCapturing = false;
    for (const level of ["log", "warn", "error", "info", "debug"]) {
      if (_qaOriginalConsole[level]) console[level] = _qaOriginalConsole[level];
    }
    _qaOriginalConsole = {};
  }

  function getQACapture() {
    return { consoleLogs: _qaConsoleLogs.slice(), networkEvents: _qaNetworkEvents.slice() };
  }

  // ---------- network mocking ----------

  function installNetworkMock({ url, method, response, status }) {
    if (!_originalFetch) {
      _originalFetch = window.fetch;
      window.fetch = function (...args) {
        const [resource, init] = args;
        const reqUrl =
          typeof resource === "string"
            ? resource
            : resource instanceof URL
              ? resource.href
              : resource?.url || "";
        const reqMethod = ((init?.method) || "GET").toUpperCase();
        const mock = _networkMocks.find(
          (m) =>
            (!m.url || reqUrl.includes(m.url)) &&
            (!m.method || m.method.toUpperCase() === reqMethod),
        );
        if (mock) {
          const body =
            typeof mock.response === "object" && mock.response !== null
              ? JSON.stringify(mock.response)
              : String(mock.response ?? "");
          return Promise.resolve(new Response(body, { status: mock.status || 200 }));
        }
        return _originalFetch.apply(this, args);
      };
    }
    _networkMocks.push({ url, method, response, status });
  }

  function clearNetworkMocks() {
    _networkMocks = [];
    if (_originalFetch) {
      window.fetch = _originalFetch;
      _originalFetch = null;
    }
  }

  // ---------- recorder ----------

  function isTailwindClass(cls) {
    return /^(?:hover:|focus:|active:|disabled:|sm:|md:|lg:|xl:|2xl:)?(?:flex|grid|block|inline|hidden|relative|absolute|fixed|sticky|static|overflow|truncate|whitespace|break|[pmb][xylrtbse]?-|gap-|space-[xy]-|w-|h-|min-|max-|text-|font-|leading-|tracking-|align-|justify-|items-|content-|self-|order-|col-|row-|border(?:-|$)|rounded(?:-|$)|shadow(?:-|$)|opacity-|bg-|from-|via-|to-|ring(?:-|$)|outline|cursor-|pointer-|select-|resize|transition|duration-|ease-|delay-|animate-|scale-|rotate-|translate-|skew-|origin-|z-|sr-only|not-sr-only|appearance-|list-|table-|caption-|object-|float-|clear-|aspect-|container|columns-|grow|shrink|basis-|place-)/.test(cls);
  }

  // React 18 useId() generates IDs like :r0:, :r1:, :ra: — unstable across renders.
  function isGeneratedId(id) {
    return /^:[a-zA-Z][a-zA-Z0-9]*:$/.test(id);
  }

  // Walks up from `el` and returns the nearest stable ancestor we can anchor an XPath to:
  // an ancestor with a non-generated id, role=form|dialog|navigation|main, or a <form id>.
  function getStableAncestor(el) {
    let cur = el.parentElement;
    while (cur && cur !== document.body && cur !== document.documentElement) {
      if (cur.id && !isGeneratedId(cur.id)) return { el: cur, kind: "id" };
      const role = cur.getAttribute("role");
      if (role && ["form", "dialog", "navigation", "main"].includes(role)) {
        return { el: cur, kind: "role", role };
      }
      if (cur.tagName === "FORM" && cur.id && !isGeneratedId(cur.id)) {
        return { el: cur, kind: "form" };
      }
      cur = cur.parentElement;
    }
    return null;
  }

  function buildContextualXPath(el, anchor) {
    const tag = el.tagName.toLowerCase();
    let head;
    if (anchor.kind === "id" || anchor.kind === "form") {
      head = `//${anchor.el.tagName.toLowerCase()}[@id='${anchor.el.id}']`;
    } else if (anchor.kind === "role") {
      head = `//*[@role='${anchor.role}']`;
    } else {
      return null;
    }
    const text = (el.textContent || "").trim();
    if (text && text.length <= 50 && ["BUTTON", "A"].includes(el.tagName)) {
      return `${head}//${tag}[normalize-space()="${text.replace(/"/g, '\\"')}"]`;
    }
    const nameAttr = el.getAttribute("name");
    if (nameAttr) return `${head}//${tag}[@name='${nameAttr}']`;
    const ph = el.getAttribute("placeholder");
    if (ph) return `${head}//${tag}[@placeholder="${ph.replace(/"/g, '\\"')}"]`;
    const aria = el.getAttribute("aria-label");
    if (aria) return `${head}//${tag}[@aria-label="${aria.replace(/"/g, '\\"')}"]`;
    return null;
  }

  // Existing CSS path builder, extracted so it can be one rung of the ladder.
  function buildCssPath(el) {
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && parts.length < 3) {
      let sel = cur.tagName.toLowerCase();
      if (cur.id && !isGeneratedId(cur.id)) { parts.unshift(`#${cur.id}`); break; }
      const cls = (typeof cur.className === "string" ? cur.className : "")
        .trim()
        .split(/\s+/)
        .filter((c) => c && !/^[a-z0-9]{10,}$/.test(c) && !isTailwindClass(c))
        .slice(0, 2);
      if (cls.length) {
        sel += "." + cls.join(".");
      } else {
        const siblings = cur.parentElement
          ? Array.from(cur.parentElement.children).filter(s => s.tagName === cur.tagName)
          : [];
        const idx = siblings.indexOf(cur);
        if (siblings.length > 1 && idx >= 0) sel += `:nth-of-type(${idx + 1})`;
      }
      parts.unshift(sel);
      cur = cur.parentElement;
    }
    return parts.join(" > ") || el.tagName.toLowerCase();
  }

  // Returns an ordered ladder of independently-stable selectors for `el`.
  // Playback tries each rung until one resolves; only after all fail does the LLM healer run.
  function buildSelectorLadder(el) {
    const out = [];
    const seen = new Set();
    const push = (s) => { if (s && !seen.has(s)) { seen.add(s); out.push(s); } };

    // 1. Test attributes — strongest, framework-agnostic
    const testid = el.getAttribute("data-testid");
    if (testid) push(`[data-testid="${testid}"]`);
    const dataTest = el.getAttribute("data-test");
    if (dataTest) push(`[data-test="${dataTest}"]`);
    const dataCy = el.getAttribute("data-cy");
    if (dataCy) push(`[data-cy="${dataCy}"]`);

    // 2. Role + accessible name — survives DOM/class renames
    const role = getRole(el);
    const accName = getAccessibleName(el);
    if (role && accName) push(`role=${role}[name="${accName.replace(/"/g, '\\"')}"]`);

    const isFormField = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    const tag = el.tagName.toLowerCase();

    // 3. Stable #id (skip React 18 useId-generated)
    if (el.id && !isGeneratedId(el.id)) {
      push(isFormField ? `${tag}[id='${el.id}']` : `#${el.id}`);
    }

    // 4. [name=...] for form fields
    if (isFormField) {
      const nameAttr = el.getAttribute("name");
      if (nameAttr) push(`${tag}[name='${nameAttr}']`);
    }

    // 5. text="..." for buttons / links
    const text = (el.textContent || "").trim();
    if (text && text.length <= 50 && ["BUTTON", "A"].includes(el.tagName)) {
      push(`text="${text.replace(/"/g, '\\"')}"`);
    }

    // 6. Form-field accessibility anchors
    if (isFormField) {
      const ph = el.getAttribute("placeholder");
      if (ph) push(`[placeholder="${ph.replace(/"/g, '\\"')}"]`);
    }
    const aria = el.getAttribute("aria-label");
    if (aria) push(`[aria-label="${aria.replace(/"/g, '\\"')}"]`);

    // 7. Contextual XPath anchored to nearest stable landmark
    const anchor = getStableAncestor(el);
    if (anchor) {
      const xp = buildContextualXPath(el, anchor);
      if (xp) push(`xpath=${xp}`);
    }
    // Bare-text XPath for non-form elements (survives DOM moves)
    if (text && text.length <= 60 && !isFormField) {
      push(`xpath=//${tag}[normalize-space(.)="${text.replace(/"/g, '\\"')}"]`);
    }

    // 8. CSS path — last resort, structural
    push(buildCssPath(el));

    return out.length > 0 ? out : [tag];
  }

  // Returns the OUTERMOST ancestor that is a known date-picker container, or null.
  // We walk all the way up rather than using .closest() so that inner elements like
  // table.ui-datepicker-calendar or td.ui-datepicker-week-end (which also contain
  // "datepicker" in their class names) don't short-circuit before we reach the real
  // top-level container (e.g. div#ui-datepicker-div.ui-datepicker).
  function getDatePickerContainer(el) {
    const sel =
      '[class*="datepicker"],[class*="DatePicker"],[class*="date-picker"],' +
      '[class*="daterangepicker"],[class*="flatpickr"],' +
      '[class*="ant-picker"],[class*="MuiDatePicker"],[class*="MuiPickersPopper"],' +
      '[class*="react-datepicker"],[class*="rdp"],[class*="picker-panel"],' +
      '[class*="calendar-wrap"],[class*="calendar-container"],' +
      '[data-testid*="datepicker"],[data-testid*="date-picker"],' +
      '[aria-label*="calendar"],[aria-label*="date picker"],' +
      '[role="dialog"][class*="pick"]';
    let found = null;
    let cur = el.parentElement;
    while (cur && cur !== document.documentElement) {
      if (cur.matches(sel)) found = cur;
      cur = cur.parentElement;
    }
    return found;
  }

  // Given any element inside a react-select tree (or other custom dropdown), return
  // the outermost CONTROL container — the div whose pointerdown opens the menu.
  // Two-pass walk: prefer the standard __control / css-HASH-control wrapper, then fall
  // back to aria-based detection. Never returns a bare <input> — in react-select v5+
  // the inner input carries role="combobox" but pointerdown on it does not open the menu.
  function findReactSelectControlAncestor(el) {
    // Pass 1: standard react-select control wrapper (BEM or Emotion)
    let node = el;
    while (node && node !== document.body) {
      const cls = typeof node.className === 'string' ? node.className : '';
      if (/(?:^|\s)\w+__control(?:\s|--|$)/.test(cls) ||
          /(?:^|\s)css-[a-z0-9]+-control(?:\s|--|$)/.test(cls)) return node;
      node = node.parentElement;
    }
    // Pass 2: Ant Design / Headless UI / Radix — but skip bare INPUT
    node = el;
    while (node && node !== document.body) {
      if (node.tagName !== 'INPUT') {
        if (node.getAttribute('aria-haspopup') === 'listbox' ||
            node.getAttribute('role') === 'combobox') return node;
      }
      node = node.parentElement;
    }
    return null;
  }

  function onRecordClick(e) {
    if (!_recording) return;
    let el = e.target;
    // Icon-only buttons fire click with e.target on the inner <svg>/<path>.
    // Inner SVG nodes have offsetParent === null (rejected by isVisible at
    // playback) and no role/name (ladder degrades to CSS path / XPath), so the
    // recorded step can't be replayed. Re-target to the clickable ancestor.
    if (el instanceof SVGElement) {
      const clickable = el.closest(
        'button, a, [role="button"], [role="link"], [role="menuitem"], [role="tab"], [onclick]'
      );
      if (clickable) {
        el = clickable;
      } else {
        let n = el;
        while (n && n instanceof SVGElement) n = n.parentElement;
        if (n) el = n;
      }
    }
    // React-select v5+: the inner input carries role="combobox" and the INPUT filter
    // below would otherwise skip it. Arm _pendingSelectControl with the WRAPPER
    // (not the input) so the next option-click collapses into a select action targeting
    // the right element.
    if (el.tagName === "INPUT" && el.getAttribute("role") === "combobox") {
      const wrapper = findReactSelectControlAncestor(el);
      if (wrapper) {
        _pendingSelectControl = buildSelectorLadder(wrapper);
        clearTimeout(_pendingSelectClearId);
        _pendingSelectClearId = setTimeout(() => { _pendingSelectControl = null; }, 5000);
        return;
      }
    }
    // Skip form inputs that are handled by onRecordChange, but allow button-type inputs (submit/button/reset/image)
    const inputType = (el.getAttribute("type") || "text").toLowerCase();
    const isButtonInput = el.tagName === "INPUT" && ["submit", "button", "reset", "image"].includes(inputType);
    if (!isButtonInput && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName)) return;
    if (_lastRecordedUrl && location.href !== _lastRecordedUrl) {
      const navStep = { action: "navigate", url: location.href };
      _recordedSteps.push(navStep);
      chrome.runtime.sendMessage({ type: "recordedStep", step: navStep });
      _lastRecordedUrl = location.href;
    }
    // If the click is inside a custom date picker, suppress it and arm the
    // post-click scan to record a single 'datepick' step instead.
    const pickerContainer = getDatePickerContainer(el);
    if (pickerContainer) {
      if (!_suppressDatePickerClicks) {
        _suppressDatePickerClicks = pickerContainer;
        // Safety net: clear after 3 s if no value change is detected
        setTimeout(() => {
          if (_suppressDatePickerClicks === pickerContainer) _suppressDatePickerClicks = null;
        }, 3000);
      }
      // Do NOT record a click step — post-click scan will emit datepick
    } else {
      const optionEl = el.closest('[role="option"],[role="menuitem"],[role="menuitemradio"]');
      if (optionEl && _pendingSelectControl) {
        // Collapse indicator-click + option-click into one robust select action
        const optText = (optionEl.innerText || optionEl.textContent || '').trim();
        const step = { action: 'select', target: _pendingSelectControl, value: optText };
        _recordedSteps.push(step);
        chrome.runtime.sendMessage({ type: 'recordedStep', step });
        _pendingSelectControl = null;
        clearTimeout(_pendingSelectClearId);
      } else if (optionEl) {
        // Option clicked but _pendingSelectControl wasn't armed — find the owning trigger via ARIA
        const optText = (optionEl.innerText || optionEl.textContent || '').trim();
        const listbox = optionEl.closest('[role="listbox"]');
        let triggerEl = null;
        if (listbox && listbox.id) {
          triggerEl = document.querySelector(`[aria-controls="${listbox.id}"],[aria-owns="${listbox.id}"]`);
        }
        if (!triggerEl) {
          triggerEl = document.querySelector('[aria-expanded="true"][aria-haspopup],[aria-expanded="true"][role="combobox"]');
        }
        // If the found trigger is the inner combobox input, walk up to the actual
        // control wrapper — react-select v5+ won't reopen the menu from a click on
        // the bare input.
        if (triggerEl && triggerEl.tagName === "INPUT") {
          triggerEl = findReactSelectControlAncestor(triggerEl) || triggerEl;
        }
        if (triggerEl && optText) {
          const step = { action: 'select', target: buildSelectorLadder(triggerEl), value: optText };
          _recordedSteps.push(step);
          chrome.runtime.sendMessage({ type: 'recordedStep', step });
        } else {
          const step = { action: 'click', target: buildSelectorLadder(optionEl) };
          _recordedSteps.push(step);
          chrome.runtime.sendMessage({ type: 'recordedStep', step });
        }
      } else {
        const rsControl = findReactSelectControlAncestor(el);
        if (rsControl) {
          // User clicked inside a react-select control — arm pending, suppress click recording
          _pendingSelectControl = buildSelectorLadder(rsControl);
          clearTimeout(_pendingSelectClearId);
          _pendingSelectClearId = setTimeout(() => { _pendingSelectControl = null; }, 5000);
        } else {
          const step = { action: "click", target: buildSelectorLadder(el) };
          _recordedSteps.push(step);
          chrome.runtime.sendMessage({ type: "recordedStep", step });
        }
      }
    }

    // Snapshot text/date inputs before the click resolves so we can detect
    // programmatic value changes made by custom date-picker widgets (which don't
    // fire a native change event that onRecordChange would catch).
    const preValues = new Map();
    for (const inp of document.querySelectorAll(
      'input[type="text"],input[type="date"],input[type="datetime-local"],input:not([type])'
    )) {
      preValues.set(inp, inp.value);
    }
    setTimeout(() => {
      for (const [inp, prev] of preValues) {
        const newVal = inp.value;
        if (!newVal || newVal === prev) continue;
        if (_recentlyChangedByEvent.has(inp)) continue;
        const sel = buildSelectorLadder(inp);
        if (_suppressDatePickerClicks) {
          const containerSel = buildSelectorLadder(_suppressDatePickerClicks);
          // Derive an unambiguous ISO date from the picker's own library so that
          // dd/mm and mm/dd formats are both handled correctly during execution.
          let isoDate = null;
          const jQ = window.$;
          if (inp.classList.contains("hasDatepicker") && jQ && jQ.datepicker?.parseDate) {
            try {
              const inst = jQ.datepicker._getInst(inp);
              const fmt = inst?.settings?.dateFormat || jQ.datepicker._defaults?.dateFormat || "mm/dd/yy";
              const d = jQ.datepicker.parseDate(fmt, newVal);
              isoDate = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
            } catch (_) {}
          }
          if (!isoDate && inp._flatpickr?.selectedDates?.[0]) {
            const d = inp._flatpickr.selectedDates[0];
            isoDate = `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
          }
          if (!isoDate) {
            const p = parseDateForPick(newVal);
            if (p) isoDate = `${p.year}-${String(p.month+1).padStart(2,"0")}-${String(p.day).padStart(2,"0")}`;
          }
          const dpStep = { action: "datepick", target: containerSel, inputTarget: sel, value: newVal, ...(isoDate ? { isoDate } : {}) };
          _recordedSteps.push(dpStep);
          chrome.runtime.sendMessage({ type: "recordedStep", step: dpStep });
          _suppressDatePickerClicks = null;
        } else {
          const typeStep = { action: "type", target: sel, value: newVal };
          _recordedSteps.push(typeStep);
          chrome.runtime.sendMessage({ type: "recordedStep", step: typeStep });
        }
      }
    }, 500);
  }

  function onRecordChange(e) {
    if (!_recording) return;
    const el = e.target;
    const sel = buildSelectorLadder(el);
    // Mark this element so the post-click scan in onRecordClick doesn't double-record it
    _recentlyChangedByEvent.add(el);
    setTimeout(() => _recentlyChangedByEvent.delete(el), 600);
    if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file") {
      const file = el.files && el.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = (evt) => {
        const step = {
          action: "upload_file",
          target: sel,
          value: evt.target.result,
          filename: file.name,
          mime_type: file.type || "application/octet-stream",
        };
        _recordedSteps.push(step);
        chrome.runtime.sendMessage({ type: "recordedStep", step });
      };
      reader.readAsDataURL(file);
      return;
    }
    let step;
    if (el.tagName === "SELECT") {
      step = { action: "select", target: sel, value: el.value };
    } else if (el.type === "checkbox" || el.type === "radio") {
      step = { action: el.checked ? "check" : "uncheck", target: sel };
    } else {
      step = { action: "type", target: sel, value: el.value };
    }
    _recordedSteps.push(step);
    chrome.runtime.sendMessage({ type: "recordedStep", step });
  }

  function onRecordKeydown(e) {
    if (!_recording) return;
    const specials = ["Enter", "Escape"];
    if (!specials.includes(e.key) && !e.ctrlKey && !e.metaKey) return;
    const parts = [];
    if (e.ctrlKey) parts.push("Ctrl");
    if (e.metaKey) parts.push("Cmd");
    if (e.altKey) parts.push("Alt");
    if (e.shiftKey) parts.push("Shift");
    parts.push(e.key);
    const step = { action: "press_key", value: parts.join("+") };
    _recordedSteps.push(step);
    chrome.runtime.sendMessage({ type: "recordedStep", step });
  }

  function startRecording() {
    _recording = true;
    _recordedSteps = [];
    _lastRecordedUrl = location.href;
    // Record a leading navigate step for the page the recording starts on, so the
    // recording is self-contained and replays on the right page — important when
    // combining multiple recordings (each session navigates to its own start page).
    const navStep = { action: "navigate", url: location.href };
    _recordedSteps.push(navStep);
    chrome.runtime.sendMessage({ type: "recordedStep", step: navStep });
    document.addEventListener("click", onRecordClick, true);
    document.addEventListener("change", onRecordChange, true);
    document.addEventListener("keydown", onRecordKeydown, true);
  }

  function stopRecording() {
    _recording = false;
    _pendingSelectControl = null;
    clearTimeout(_pendingSelectClearId);
    document.removeEventListener("click", onRecordClick, true);
    document.removeEventListener("change", onRecordChange, true);
    document.removeEventListener("keydown", onRecordKeydown, true);
    return _recordedSteps;
  }

  // ---------- agent highlight overlay ----------
  // Shows on-page what the agent is about to act on, like Claude/Gemini's
  // browser agents. Pure DOM overlay; auto-clears after a short delay.
  let _hlBox = null, _hlLabel = null, _hlTimer = null;

  function clearHighlight() {
    if (_hlTimer) { clearTimeout(_hlTimer); _hlTimer = null; }
    if (_hlBox) { _hlBox.remove(); _hlBox = null; }
    if (_hlLabel) { _hlLabel.remove(); _hlLabel = null; }
  }

  function drawHighlight(target, label) {
    clearHighlight();
    let el;
    try { el = resolve(target)[0]; } catch (_) { el = null; }
    if (!el || typeof el.getBoundingClientRect !== "function") return;
    try { el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" }); } catch (_) {}
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) return;

    _hlBox = document.createElement("div");
    Object.assign(_hlBox.style, {
      position: "fixed", left: r.left + "px", top: r.top + "px",
      width: r.width + "px", height: r.height + "px",
      border: "2px solid #6d5efc", borderRadius: "4px",
      boxShadow: "0 0 0 3px rgba(109,94,252,.30)",
      background: "rgba(109,94,252,.08)",
      zIndex: "2147483647", pointerEvents: "none", boxSizing: "border-box",
      transition: "all .12s ease",
    });
    document.documentElement.appendChild(_hlBox);

    if (label) {
      _hlLabel = document.createElement("div");
      _hlLabel.textContent = label;
      Object.assign(_hlLabel.style, {
        position: "fixed", left: r.left + "px",
        top: Math.max(0, r.top - 20) + "px",
        font: "11px/16px -apple-system,system-ui,sans-serif", color: "#fff",
        background: "#6d5efc", padding: "1px 6px", borderRadius: "3px",
        zIndex: "2147483647", pointerEvents: "none", whiteSpace: "nowrap",
      });
      document.documentElement.appendChild(_hlLabel);
    }
    _hlTimer = setTimeout(clearHighlight, 1200);
  }

  // ---------- Set-of-Marks overlay ----------
  // Numbers every element from the last snapshot's registry directly on the page
  // so a screenshot taken while visible carries the labels (captureVisibleTab
  // renders them for free — no canvas compositing, no DPR math). The runner shows
  // them only for the snapshot→capture window, then hides them immediately.
  let _somContainer = null;

  function hideSoM() {
    if (_somContainer) { _somContainer.remove(); _somContainer = null; }
  }

  function showSoM() {
    hideSoM();
    if (!_snapshotRegistry.length) return 0;
    const vw = window.innerWidth, vh = window.innerHeight;
    _somContainer = document.createElement("div");
    _somContainer.style.cssText =
      "position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647;";
    let labeled = 0;
    _snapshotRegistry.forEach((el, i) => {
      try {
        if (!el.isConnected) return;
        const r = el.getBoundingClientRect();
        // Only label what's actually in the viewport — offscreen labels would
        // stack at the edges and clutter the screenshot without being clickable.
        if (!r.width && !r.height) return;
        if (r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) return;
        const box = document.createElement("div");
        box.style.cssText =
          `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
          "outline:1px solid rgba(109,94,252,.9);pointer-events:none;box-sizing:border-box;";
        const tag = document.createElement("div");
        tag.textContent = String(i + 1);
        tag.style.cssText =
          `position:fixed;left:${Math.max(0, r.left)}px;top:${Math.max(0, r.top - 14)}px;` +
          "font:700 11px/14px -apple-system,system-ui,sans-serif;color:#fff;background:#6d5efc;" +
          "padding:0 4px;border-radius:2px;pointer-events:none;white-space:nowrap;";
        _somContainer.appendChild(box);
        _somContainer.appendChild(tag);
        labeled++;
      } catch (_) {}
    });
    document.documentElement.appendChild(_somContainer);
    return labeled;
  }

  // ---------- form state (pre-submit diff) ----------
  // What would be submitted if this target were clicked: the filled fields of
  // the target's enclosing form (fallback: all visible fields on the page).
  // Password values are masked before they leave the page.
  function collectFormState(target) {
    let scope = null;
    try {
      const el = target ? resolve(target)[0] : null;
      scope = el ? (el.closest("form") || el.form || null) : null;
    } catch (_) {}
    const candidates = scope
      ? Array.from(scope.querySelectorAll("input, select, textarea"))
      : deepQuerySelectorAll("input, select, textarea").filter(isVisible);
    const fields = [];
    for (const f of candidates) {
      if (fields.length >= 25) break;
      const type = (
        f.getAttribute("type") ||
        (f.tagName === "SELECT" ? "select" : f.tagName === "TEXTAREA" ? "textarea" : "text")
      ).toLowerCase();
      if (["hidden", "submit", "button", "reset", "image"].includes(type)) continue;
      let value;
      if (type === "checkbox" || type === "radio") {
        if (!f.checked) continue;
        value = f.value && f.value !== "on" ? f.value : "checked";
      } else if (f.tagName === "SELECT") {
        value = f.selectedOptions?.[0]?.textContent?.trim() || f.value;
      } else {
        value = f.value;
      }
      if (value == null || value === "") continue;
      if (type === "password") value = "•••";
      const label =
        getAccessibleName(f) || f.getAttribute("placeholder") || f.getAttribute("name") || f.id || type;
      fields.push({ label: String(label).slice(0, 60), type, value: String(value).slice(0, 120) });
    }
    return fields;
  }

  // ---------- persistent run annotation layer ----------
  // Unlike the transient highlight, these badges accumulate over a run: every
  // past action stays visible (greyed ✓), the upcoming action pulses, and
  // extracted values float next to their source elements. One rAF-throttled
  // scroll/resize listener repositions everything; the runner clears the layer
  // at run end (navigation clears it for free — the script re-injects).
  let _annContainer = null;
  let _annEntries = []; // { key, el, badge, box }
  let _annRafPending = false;
  let _annListenersBound = false;
  const MAX_ANNOTATIONS = 30;

  function _annReposition() {
    _annRafPending = false;
    for (const e of _annEntries) {
      try {
        const r = e.el.isConnected ? e.el.getBoundingClientRect() : null;
        const hidden = !r || (!r.width && !r.height);
        e.badge.style.display = hidden ? "none" : "";
        if (e.box) e.box.style.display = hidden ? "none" : "";
        if (hidden) continue;
        e.badge.style.left = Math.max(0, r.left) + "px";
        e.badge.style.top = Math.max(0, r.top - 16) + "px";
        if (e.box) {
          e.box.style.left = r.left + "px";
          e.box.style.top = r.top + "px";
          e.box.style.width = r.width + "px";
          e.box.style.height = r.height + "px";
        }
      } catch (_) {}
    }
  }

  function _annSchedule() {
    if (_annRafPending) return;
    _annRafPending = true;
    requestAnimationFrame(_annReposition);
  }

  function clearAnnotations() {
    if (_annContainer) { _annContainer.remove(); _annContainer = null; }
    _annEntries = [];
    if (_annListenersBound) {
      window.removeEventListener("scroll", _annSchedule, true);
      window.removeEventListener("resize", _annSchedule);
      _annListenersBound = false;
    }
  }

  const ANN_STYLES = {
    next:      { bg: "#6d5efc", prefix: "▶ ", box: true },
    done:      { bg: "#9aa0a6", prefix: "✓ ", box: false },
    extracted: { bg: "#0f9d58", prefix: "", box: false },
  };

  function annotate({ target, state, label }) {
    const style = ANN_STYLES[state] || ANN_STYLES.done;
    let el = null;
    try { el = target ? resolve(target)[0] : null; } catch (_) {}
    if (!el || typeof el.getBoundingClientRect !== "function") return false;

    if (!_annContainer) {
      _annContainer = document.createElement("div");
      _annContainer.style.cssText =
        "position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483646;";
      document.documentElement.appendChild(_annContainer);
    }
    if (!_annListenersBound) {
      window.addEventListener("scroll", _annSchedule, true);
      window.addEventListener("resize", _annSchedule);
      _annListenersBound = true;
    }

    // Same target annotated again (e.g. "next" completing as "done") replaces
    // its previous badge; extracted-value badges live independently.
    const key = (state === "extracted" ? "x::" : "a::") + JSON.stringify(target);
    const existingIdx = _annEntries.findIndex((e) => e.key === key);
    if (existingIdx !== -1) {
      const old = _annEntries.splice(existingIdx, 1)[0];
      old.badge.remove();
      if (old.box) old.box.remove();
    }
    while (_annEntries.length >= MAX_ANNOTATIONS) {
      const old = _annEntries.shift();
      old.badge.remove();
      if (old.box) old.box.remove();
    }

    const badge = document.createElement("div");
    badge.textContent = style.prefix + String(label || state).slice(0, 60);
    badge.style.cssText =
      "position:fixed;font:600 10px/14px -apple-system,system-ui,sans-serif;color:#fff;" +
      `background:${style.bg};padding:1px 5px;border-radius:3px;pointer-events:none;` +
      "white-space:nowrap;max-width:280px;overflow:hidden;text-overflow:ellipsis;" +
      (state === "done" ? "opacity:.7;" : "");
    _annContainer.appendChild(badge);

    let box = null;
    if (style.box) {
      box = document.createElement("div");
      box.style.cssText =
        `position:fixed;border:2px solid ${style.bg};border-radius:4px;` +
        `box-shadow:0 0 0 3px rgba(109,94,252,.25);pointer-events:none;box-sizing:border-box;`;
      _annContainer.appendChild(box);
    }

    _annEntries.push({ key, el, badge, box });
    _annSchedule();
    return true;
  }

  // ---------- message bridge ----------

  const _runtimeListener = (msg, _sender, sendResponse) => {
    (async () => {
      try {
        // Each message starts a fresh cache generation — a stale shadow-root
        // list from a prior message must never leak into this one's resolve().
        _shadowRootCache = null;
        if (msg?.type === "snapshot") {
          sendResponse({ ok: true, snapshot: pageSnapshot() });
          return;
        }
        if (msg?.type === "waitForDomSettled") {
          sendResponse({ ok: true, ...(await waitForDomSettled(msg.opts)) });
          return;
        }
        if (msg?.type === "execAction") {
          const result = await execAction(msg.step);
          // Which ladder rung actually matched (set by resolve/findOne on ladder
          // targets) — lets the runner persist the selector that really worked.
          if (result && typeof result === "object" && _lastMatchedSelector != null) {
            result.matched_selector = result.matched_selector ?? _lastMatchedSelector;
          }
          sendResponse({ ok: true, result });
          return;
        }
        if (msg?.type === "startRecording") {
          startRecording();
          sendResponse({ ok: true });
          return;
        }
        if (msg?.type === "stopRecording") {
          const steps = stopRecording();
          sendResponse({ ok: true, steps });
          return;
        }
        if (msg?.type === "resetFrame") {
          _currentDoc = document;
          sendResponse({ ok: true });
          return;
        }
        if (msg?.type === "getViewportSize") {
          // Read back the actual inner size after a resize_window action —
          // chrome.windows.update sets OUTER dimensions, which differ from
          // window.innerWidth/innerHeight by the browser chrome's own size.
          // Also doubles as the DPR source for zoom's literal-coordinate path.
          sendResponse({ ok: true, width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio || 1 });
          return;
        }
        if (msg?.type === "highlight")     { drawHighlight(msg.target, msg.label); sendResponse({ ok: true }); return; }
        if (msg?.type === "clearHighlight"){ clearHighlight(); sendResponse({ ok: true }); return; }
        if (msg?.type === "showSoM")       { sendResponse({ ok: true, labeled: showSoM() }); return; }
        if (msg?.type === "hideSoM")       { hideSoM(); sendResponse({ ok: true }); return; }
        if (msg?.type === "collectFormState") { sendResponse({ ok: true, fields: collectFormState(msg.target) }); return; }
        if (msg?.type === "annotate")         { sendResponse({ ok: true, drawn: annotate(msg) }); return; }
        if (msg?.type === "clearAnnotations") { clearAnnotations(); sendResponse({ ok: true }); return; }
        if (msg?.type === "resolveProbe") {
          // Side-effect-free selector check: which of these targets resolve on
          // the current page? Used by the pre-run scan and dry-run mode.
          const results = (msg.targets || []).map((target) => {
            try {
              _lastMatchedSelector = null;
              const els = resolve(target);
              return {
                target,
                found: els.length > 0,
                visible: els.some(isVisible),
                matched: _lastMatchedSelector || undefined,
              };
            } catch (e) {
              return { target, found: false, visible: false, error: e.message || String(e) };
            }
          });
          sendResponse({ ok: true, results });
          return;
        }
        if (msg?.type === "startQACapture") { startQACapture({ reset: msg.reset !== false }); sendResponse({ ok: true }); return; }
        if (msg?.type === "stopQACapture")  { stopQACapture();  sendResponse({ ok: true }); return; }
        if (msg?.type === "getQACapture")   { sendResponse({ ok: true, capture: getQACapture() }); return; }
        if (msg?.type === "getQANetworkEvents") {
          // In-page fetch/XHR entries for the read_network_requests action —
          // the runner merges these with the background webRequest buffer.
          const events = _qaNetworkEvents.slice();
          if (msg.clear) _qaNetworkEvents = [];
          sendResponse({ ok: true, events });
          return;
        }
        sendResponse({ ok: false, error: "unknown message" });
      } catch (e) {
        sendResponse({ ok: false, error: e.message || String(e) });
      }
    })();
    return true; // keep async port open
  };
  chrome.runtime.onMessage.addListener(_runtimeListener);

  // Expose cleanup so the next re-injection can tear down this instance cleanly.
  window.__bAA_installed = () => {
    try { chrome.runtime.onMessage.removeListener(_runtimeListener); } catch (_) {}
    try { _mutationObserver?.disconnect(); } catch (_) {}
    clearHighlight();
    hideSoM();
    clearAnnotations();
    stopRecording();
  };
})();
