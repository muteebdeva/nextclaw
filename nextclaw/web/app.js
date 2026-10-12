/* NextClaw dashboard — vanilla JS SPA, no build step. */
(function () {
  "use strict";

  var API = "";
  /* Storage may throw (blocked cookies/storage in webviews & private mode);
     fall back to memory so the page never dies on boot. */
  var memStore = {};
  function storeGet(k) { try { return localStorage.getItem(k); } catch (e) { return memStore[k] || null; } }
  function storeSet(k, v) { try { localStorage.setItem(k, v); } catch (e) { memStore[k] = v; } }
  function storeDel(k) { try { localStorage.removeItem(k); } catch (e) { delete memStore[k]; } }
  var token = storeGet("nc_token") || "";
  var theme = storeGet("nc_theme") || "dark";
  document.documentElement.setAttribute("data-theme", theme);

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmtTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString();
  }
  function fmtDur(s) {
    if (s == null || s < 0) return "—";
    s = Math.round(s);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
  }
  function ago(ts) {
    if (!ts) return "—";
    var s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function inDur(ts) {
    if (!ts) return "—";
    var s = Math.max(0, Math.floor(ts - Date.now() / 1000));
    if (s < 60) return "in " + s + "s";
    if (s < 3600) return "in " + Math.floor(s / 60) + "m";
    if (s < 86400) return "in " + Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return "in " + Math.floor(s / 86400) + "d";
  }
  function pill(status) {
    var cls = String(status || "unknown").replace(/[^a-z0-9_]/g, "");
    var label = String(status || "unknown").replace(/_/g, " ");
    return '<span class="pill ' + cls + '">' + esc(label) + "</span>";
  }
  function __sanitizeError(msg) {
    // Never leak anything that looks like a credential in error text.
    return String(msg || "").replace(/(api[_-]?key|token|secret|password)\s*[:=]\s*\S+/gi, "$1: [redacted]");
  }
  function showFatalError(message) {
    var bar = document.getElementById("fatal-error");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "fatal-error";
      bar.setAttribute("role", "alert");
      bar.innerHTML = '<span id="fatal-msg"></span><button class="btn small" id="fatal-reload">Reload</button>';
      document.body.prepend(bar);
      document.getElementById("fatal-reload").addEventListener("click", function () {
        location.reload();
      });
    }
    document.getElementById("fatal-msg").textContent = message;
  }
  window.addEventListener("error", function (ev) {
    showFatalError("Dashboard error: " + __sanitizeError(ev.message || "unknown"));
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev.reason;
    showFatalError("Dashboard error: " + __sanitizeError((r && r.message) || String(r)));
  });

  function api(path, opts) {
    opts = opts || {};
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 30000);
    return fetch(API + path, {
      method: opts.method || "GET",
      headers: {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
      },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl.signal,
    }).then(function (r) {
      clearTimeout(timer);
      return r.json().then(function (j) {
        if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
        return j;
      });
    }).catch(function (e) {
      clearTimeout(timer);
      if (e.name === "AbortError") throw new Error("Request timed out after 30s — the server may be busy.");
      throw e;
    });
  }

  function toast(msg, kind) {
    var box = document.getElementById("toasts");
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }

  function modal(html) {
    var root = document.getElementById("modal-root");
    root.innerHTML = '<div class="modal-backdrop"><div class="modal" role="dialog" aria-modal="true">' +
      html + "</div></div>";
    root.querySelector(".modal-backdrop").addEventListener("click", function (e) {
      if (e.target.classList.contains("modal-backdrop")) closeModal();
    });
    var first = root.querySelector("input, textarea, select, button");
    if (first) first.focus();
  }
  function closeModal() { document.getElementById("modal-root").innerHTML = ""; }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      if (!document.getElementById("search-overlay").classList.contains("hidden")) closeSearch();
      else closeModal();
    }
  });

  function loading(el) { el.innerHTML = '<div class="spinner" role="status" aria-label="loading"></div>'; }
  function showError(el, err) {
    el.innerHTML = '<div class="empty"><h3>Something went wrong</h3><p>' + esc(err.message) + '</p>' +
      '<button class="btn" onclick="location.reload()">Retry</button></div>';
  }

  /* ---------- auth / lock ---------- */
  function lock() {
    token = "";
    storeDel("nc_token");
    document.getElementById("app").classList.add("hidden");
    document.getElementById("tabbar").classList.add("hidden");
    document.getElementById("unlock").classList.remove("hidden");
  }
  function unlock(t) {
    token = t;
    storeSet("nc_token", t);
    document.getElementById("unlock").classList.add("hidden");
    document.getElementById("app").classList.remove("hidden");
    document.getElementById("tabbar").classList.remove("hidden");
    route();
    refreshApprovalBadge();
  }
  document.getElementById("unlock-btn").addEventListener("click", function () {
    var t = document.getElementById("token-input").value.trim();
    var errEl = document.getElementById("unlock-error");
    if (!t) { errEl.textContent = "Enter the token first."; errEl.classList.remove("hidden"); return; }
    token = t; // tentative; verified by the status call
    api("/api/status").then(function () {
      errEl.classList.add("hidden");
      unlock(t);
      toast("Connected to NextClaw", "ok");
    }).catch(function (e) {
      token = "";
      errEl.textContent = "Could not connect: " + e.message;
      errEl.classList.remove("hidden");
    });
  });
  document.getElementById("lock-btn").addEventListener("click", lock);
  function toggleTheme() {
    theme = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    document.querySelector('meta[name="theme-color"]')
      .setAttribute("content", theme === "dark" ? "#0b0e14" : "#f4f6fb");
    storeSet("nc_theme", theme);
  }
  document.querySelectorAll(".theme-toggle").forEach(function (b) {
    b.addEventListener("click", toggleTheme);
  });

  /* ---------- icons ---------- */
  function ic(paths) {
    return '<svg viewBox="0 0 24 24" width="19" height="19" aria-hidden="true">' + paths + "</svg>";
  }
  var P = 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" fill="none"';
  var ICONS = {
    overview: ic('<rect x="3" y="3" width="7" height="7" rx="1.5" ' + P + '/><rect x="14" y="3" width="7" height="7" rx="1.5" ' + P + '/><rect x="3" y="14" width="7" height="7" rx="1.5" ' + P + '/><rect x="14" y="14" width="7" height="7" rx="1.5" ' + P + '/>'),
    plus: ic('<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 8v8M8 12h8" ' + P + '/>'),
    agents: ic('<circle cx="8" cy="8" r="3.5" ' + P + '/><circle cx="16.5" cy="9.5" r="2.5" ' + P + '/><path d="M2.5 20c.8-3.4 3-5 5.5-5s4.7 1.6 5.5 5M14 15.6c2.3.3 4.2 1.7 5 4.4" ' + P + '/>'),
    tasks: ic('<path d="M8 6h13M8 12h13M8 18h13" ' + P + '/><circle cx="4" cy="6" r="1.4" fill="currentColor"/><circle cx="4" cy="12" r="1.4" fill="currentColor"/><circle cx="4" cy="18" r="1.4" fill="currentColor"/>'),
    approvals: ic('<path d="M9 11l3 3 8-8" ' + P + '/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9" ' + P + '/>'),
    scheduler: ic('<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 7v5l3 2" ' + P + '/>'),
    providers: ic('<rect x="4" y="4" width="16" height="16" rx="2" ' + P + '/><path d="M9 9h6v6H9zM4 10h-2M4 14h-2M22 10h-2M22 14h-2M10 4V2M14 4V2M10 22v-2M14 22v-2" ' + P + '/>'),
    tools: ic('<path d="M14.7 6.3a4.5 4.5 0 0 0-6 6L3 18l3 3 5.7-5.7a4.5 4.5 0 0 0 6-6L14 13l-3-3 3.7-3.7Z" ' + P + '/>'),
    memory: ic('<ellipse cx="12" cy="5" rx="8" ry="3" ' + P + '/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5" ' + P + '/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3" ' + P + '/>'),
    settings: ic('<circle cx="12" cy="12" r="3" ' + P + '/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.9 2.9l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.9-2.9l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.9-2.9l.1.1a1.7 1.7 0 0 0 1.9.3h0a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 1 1.5h0a1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.9 2.9l-.1.1a1.7 1.7 0 0 0-.3 1.9v0a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1Z" ' + P + '/>'),
    pulse: ic('<path d="M3 12h4l3 8 4-16 3 8h4" ' + P + '/>'),
    clock: ic('<circle cx="12" cy="12" r="9" ' + P + '/><path d="M12 7v5l3 2" ' + P + '/>'),
    gauge: ic('<path d="M12 15l4-6" ' + P + '/><path d="M4 15a8 8 0 1 1 16 0" ' + P + '/>'),
    chart: ic('<path d="M4 20V10M10 20V4M16 20v-7M22 20H2" ' + P + '/>'),
    calendar: ic('<rect x="3" y="5" width="18" height="16" rx="2" ' + P + '/><path d="M8 3v4M16 3v4M3 10h18" ' + P + '/>'),
  };

  /* ---------- sidebar nav ---------- */
  var NAV = [
    { id: "overview", label: "Overview", hash: "#/overview", icon: "overview" },
    { id: "new", label: "New Mission", action: "new", icon: "plus" },
    { id: "agents", label: "Multi-Agent", hash: "#/agents", icon: "agents" },
    { id: "tasks", label: "Tasks", hash: "#/tasks", icon: "tasks" },
    { id: "approvals", label: "Approvals", hash: "#/approvals", icon: "approvals", badge: "nav-approval-badge" },
    { id: "scheduler", label: "Scheduler", hash: "#/scheduler", icon: "scheduler" },
    { id: "providers", label: "Providers", hash: "#/providers", icon: "providers" },
    { id: "tools", label: "Tools", hash: "#/tools", icon: "tools" },
    { id: "memory", label: "Memory", hash: "#/memory", icon: "memory" },
    { id: "settings", label: "Settings", hash: "#/settings", icon: "settings" },
  ];
  function buildNav() {
    var nav = document.getElementById("main-nav");
    nav.innerHTML = NAV.map(function (n) {
      var badge = n.badge ? ' <span id="' + n.badge + '" class="badge hidden"></span>' : "";
      if (n.action === "new") {
        return '<button class="navlike" data-action="new">' + ICONS[n.icon] + "<span>" + esc(n.label) + "</span></button>";
      }
      return '<a href="' + n.hash + '" data-nav="' + n.id + '">' + ICONS[n.icon] + "<span>" + esc(n.label) + "</span>" + badge + "</a>";
    }).join("");
    nav.querySelector('[data-action="new"]').addEventListener("click", function () {
      closeDrawer();
      newMissionModal();
    });
    nav.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", closeDrawer);
    });
  }
  buildNav();

  /* ---------- mobile drawer ---------- */
  var navToggle = document.getElementById("nav-toggle");
  var drawerScrim = document.getElementById("drawer-scrim");
  function closeDrawer() {
    document.getElementById("app").classList.remove("drawer-open");
    navToggle.setAttribute("aria-expanded", "false");
    drawerScrim.classList.add("hidden");
    var tm = document.getElementById("tab-more");
    if (tm) tm.setAttribute("aria-expanded", "false");
  }
  function openDrawer() {
    document.getElementById("app").classList.add("drawer-open");
    navToggle.setAttribute("aria-expanded", "true");
    drawerScrim.classList.remove("hidden");
    var tm = document.getElementById("tab-more");
    if (tm) tm.setAttribute("aria-expanded", "true");
    var first = document.querySelector(".sidebar nav a.active") || document.querySelector(".sidebar nav a");
    if (first) first.focus();
  }
  navToggle.addEventListener("click", function () {
    if (document.getElementById("app").classList.contains("drawer-open")) closeDrawer();
    else openDrawer();
  });
  drawerScrim.addEventListener("click", closeDrawer);
  document.getElementById("tab-more").addEventListener("click", function () {
    if (document.getElementById("app").classList.contains("drawer-open")) closeDrawer();
    else openDrawer();
  });
  document.getElementById("tab-new").addEventListener("click", function () { newMissionModal(); });

  /* ---------- search ---------- */
  var searchOverlay = document.getElementById("search-overlay");
  var searchInput = document.getElementById("search-input");
  var searchResults = document.getElementById("search-results");
  function openSearch() {
    searchOverlay.classList.remove("hidden");
    searchInput.value = "";
    searchResults.innerHTML = "";
    setTimeout(function () { searchInput.focus(); }, 30);
    renderSearchPages("");
  }
  function closeSearch() {
    searchOverlay.classList.add("hidden");
  }
  document.getElementById("search-open").addEventListener("click", openSearch);
  document.getElementById("topbar-search-m").addEventListener("click", openSearch);
  document.getElementById("search-backdrop").addEventListener("click", closeSearch);
  document.addEventListener("keydown", function (e) {
    var tag = (document.activeElement && document.activeElement.tagName) || "";
    var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openSearch(); }
    else if (e.key === "/" && !typing && token) { e.preventDefault(); openSearch(); }
  });
  var searchTimer = null;
  searchInput.addEventListener("input", function () {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { runSearch(searchInput.value.trim()); }, 250);
  });
  function renderSearchPages(q) {
    var pages = NAV.filter(function (n) {
      return n.hash && (!q || n.label.toLowerCase().indexOf(q.toLowerCase()) !== -1);
    });
    if (!pages.length) return "";
    return '<div class="search-group">Pages</div>' + pages.map(function (n) {
      return '<a class="search-item" href="' + n.hash + '" data-close-search>' +
        ICONS[n.icon] + "<span>" + esc(n.label) + "</span>" +
        '<span class="s-sub">page</span></a>';
    }).join("");
  }
  function runSearch(q) {
    if (!q) {
      searchResults.innerHTML = renderSearchPages("");
      bindSearchClose();
      return;
    }
    searchResults.innerHTML = renderSearchPages(q) + '<div class="search-group">Searching…</div>';
    Promise.all([
      api("/api/tasks?limit=100&q=" + encodeURIComponent(q)).catch(function () { return { tasks: [] }; }),
      api("/api/delegations").catch(function () { return { delegations: [] }; }),
    ]).then(function (r) {
      var html = renderSearchPages(q);
      var tasks = r[0].tasks || [];
      var ql = q.toLowerCase();
      var missions = (r[1].delegations || []).filter(function (m) {
        return (m.goal || "").toLowerCase().indexOf(ql) !== -1 ||
          (m.mission_id || "").toLowerCase().indexOf(ql) !== -1;
      });
      if (tasks.length) {
        html += '<div class="search-group">Tasks</div>' + tasks.slice(0, 6).map(function (t) {
          return '<a class="search-item" href="#/tasks/' + esc(t.id) + '" data-close-search>' +
            pill(t.status) + "<span>" + esc(t.goal.length > 60 ? t.goal.slice(0, 60) + "…" : t.goal) + "</span>" +
            '<span class="s-sub mono">' + esc(t.id.slice(0, 8)) + "</span></a>";
        }).join("");
      }
      if (missions.length) {
        html += '<div class="search-group">Multi-agent missions</div>' + missions.slice(0, 6).map(function (m) {
          return '<a class="search-item" href="#/agents/' + esc(m.mission_id) + '" data-close-search>' +
            pill(m.status) + "<span>" + esc((m.goal || "").length > 60 ? m.goal.slice(0, 60) + "…" : m.goal) + "</span>" +
            '<span class="s-sub mono">' + esc(m.mission_id.slice(0, 8)) + "</span></a>";
        }).join("");
      }
      if (!tasks.length && !missions.length) html += '<div class="empty"><p>No matches for "' + esc(q) + '".</p></div>';
      searchResults.innerHTML = html;
      bindSearchClose();
    });
  }
  function bindSearchClose() {
    searchResults.querySelectorAll("[data-close-search]").forEach(function (a) {
      a.addEventListener("click", closeSearch);
    });
  }

  /* ---------- router ---------- */
  var view = document.getElementById("view");
  function route() {
    if (!token) return lock();
    var hash = location.hash || "#/overview";
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      a.classList.toggle("active", hash.indexOf("#/" + a.getAttribute("data-nav")) === 0);
    });
    document.querySelectorAll("#tabbar [data-tab]").forEach(function (a) {
      a.classList.toggle("active", hash.indexOf("#/" + a.getAttribute("data-tab")) === 0);
    });
    stopSse();
    stopDelegPoll();
    if (typeof closeDrawer === "function") closeDrawer();
    var m = hash.match(/^#\/tasks\/([a-z0-9]+)$/);
    if (m) return renderTaskDetail(m[1]);
    var m2 = hash.match(/^#\/agents\/([a-z0-9_]+)$/);
    if (m2) return renderDelegationDetail(m2[1]);
    var pages = {
      "#/overview": renderOverview,
      "#/tasks": renderTasks,
      "#/agents": renderDelegations,
      "#/approvals": renderApprovals,
      "#/scheduler": renderScheduler,
      "#/providers": renderProviders,
      "#/tools": renderTools,
      "#/memory": renderMemory,
      "#/settings": renderSettings,
    };
    (pages[hash.split("?")[0]] || renderOverview)();
  }
  window.addEventListener("hashchange", route);

  function refreshApprovalBadge() {
    if (!token) return;
    api("/api/approvals?status=pending").then(function (d) {
      var b = document.getElementById("nav-approval-badge");
      var td = document.getElementById("topbar-approval-badge");
      if (d.approvals.length) {
        if (b) { b.textContent = d.approvals.length; b.classList.remove("hidden"); }
        if (td) td.classList.remove("hidden");
      } else {
        if (b) b.classList.add("hidden");
        if (td) td.classList.add("hidden");
      }
    }).catch(function () {});
  }
  setInterval(refreshApprovalBadge, 15000);

  /* ---------- SSE ---------- */
  var sse = null, sseTimer = null, sseTries = 0;
  function stopSse() {
    if (sseTimer) { clearTimeout(sseTimer); sseTimer = null; }
    if (sse) { try { sse.close(); } catch (e) {} sse = null; }
  }
  function watchTask(taskId, onEvent) {
    stopSse();
    sseTries = 0;
    connectSse(taskId, onEvent);
  }
  function connectSse(taskId, onEvent) {
    // EventSource cannot send Authorization headers, so we mint a
    // short-lived single-use ticket via the authenticated API and put
    // only the ticket in the URL — never the long-lived dashboard token.
    api("/api/sse/ticket", { method: "POST" }).then(function (t) {
      try {
        sse = new EventSource(API + "/api/tasks/" + taskId + "/events?ticket=" + encodeURIComponent(t.ticket));
      } catch (e) { return; }
      sse.onmessage = function (ev) { try { onEvent(JSON.parse(ev.data)); } catch (e) {} };
      sse.addEventListener("done", function () { stopSse(); onEvent({ kind: "__done" }); });
      sse.onerror = function () {
        stopSse();
        // The ticket was single-use; reconnect with a fresh one.
        // Bounded retries with backoff so a dead server can't hot-loop.
        if (sseTries >= 5) return;
        sseTries += 1;
        sseTimer = setTimeout(function () { connectSse(taskId, onEvent); }, 1500 * sseTries);
      };
    }).catch(function () { /* ticket minting failed; leave SSE off */ });
  }

  /* ================= shared components ================= */

  function statCard(label, value, sub, icon, iconCls) {
    return '<div class="card stat-card"><div class="stat-top"><span class="stat-label">' + esc(label) + "</span>" +
      '<span class="stat-icon ' + (iconCls || "accent") + '">' + (ICONS[icon] || "") + "</span></div>" +
      '<div class="stat">' + value + '</div><div class="stat-sub">' + sub + "</div></div>";
  }

  function initials(name) {
    var words = String(name || "?").trim().split(/\s+/);
    return (words[0][0] + (words[1] ? words[1][0] : "")).toUpperCase();
  }

  /* Topological levels of a delegation plan (by depends_on). */
  function planLevels(plan) {
    var byId = {}, level = {}, visiting = {};
    plan.forEach(function (s) { byId[s.id] = s; });
    function lv(s) {
      if (level[s.id] !== undefined) return level[s.id];
      if (visiting[s.id]) return 0; // corrupt cycle: break it instead of overflowing
      visiting[s.id] = true;
      var deps = (s.depends_on || []).filter(function (d) { return byId[d]; });
      var l = deps.length ? 1 + Math.max.apply(null, deps.map(function (d) { return lv(byId[d]); })) : 0;
      visiting[s.id] = false;
      level[s.id] = l;
      return l;
    }
    plan.forEach(lv);
    var groups = {};
    plan.forEach(function (s) { (groups[level[s.id]] = groups[level[s.id]] || []).push(s); });
    return Object.keys(groups).map(Number).sort(function (a, b) { return a - b; })
      .map(function (k) { return groups[k]; });
  }

  function levelStatus(subs, workerById) {
    var sts = subs.map(function (s) {
      var w = workerById[s.id];
      return w ? w.status : "pending";
    });
    if (sts.every(function (x) { return x === "completed"; })) return "done";
    if (sts.some(function (x) { return x === "failed" || x === "cancelled"; })) return "failed";
    if (sts.some(function (x) { return x === "running" || x === "awaiting_approval"; })) return "running";
    return "pending";
  }

  function stageLabel(subs) {
    var names = subs.map(function (s) { return s.name || s.id; });
    var label = names.slice(0, 2).join(" + ");
    if (names.length > 2) label += " +" + (names.length - 2);
    return label.length > 26 ? label.slice(0, 26) + "…" : label;
  }

  function stagesHtml(levels, workerById) {
    return '<div class="stages">' + levels.map(function (subs, i) {
      var st = levelStatus(subs, workerById);
      var sep = i ? '<span class="stage-arrow">→</span>' : "";
      return sep + '<span class="stage ' + st + '" title="' +
        esc(subs.map(function (s) { return s.name || s.id; }).join(", ")) + '">' +
        esc(stageLabel(subs)) + "</span>";
    }).join("") + "</div>";
  }

  function workerAvatar(w) {
    var label = w.kind === "tool" ? ("tool:" + (w.tool || "")) : (w.profile || w.name || "?");
    var tip = esc((w.name || "") + " · " + String(w.status || "").replace(/_/g, " "));
    return '<span class="agent-chip" title="' + tip + '"><span class="agent-dot ' +
      esc(String(w.status || "pending")) + '">' + esc(initials(label)) + "</span>" +
      esc(label.length > 14 ? label.slice(0, 14) + "…" : label) + "</span>";
  }

  /* Mission card: works for delegations (multi-agent) and single tasks. */
  function missionCard(kind, item) {
    var id, goal, status, age, pills, stages, progress, avatars, link;
    if (kind === "delegation") {
      id = item.mission_id;
      goal = item.goal || "";
      status = item.status;
      age = ago(item.updated_at);
      pills = '<span class="pill type-multi">Multi-agent</span>' + pill(status);
      var plan = item.plan || [];
      var wById = {};
      (item.workers || []).forEach(function (w) { wById[w.subtask_id] = w; });
      var levels = planLevels(plan);
      stages = plan.length ? stagesHtml(levels, wById) : "";
      var total = plan.length;
      var done = (item.subtask_counts && item.subtask_counts.completed) || 0;
      var pct = total ? Math.round(100 * done / total) : 0;
      progress = '<div class="m-progress-row"><div class="progress"><div class="bar" style="width:' + pct +
        '%"></div></div><div class="m-pct">' + pct + "%</div></div>";
      avatars = '<div class="agents-row">' + (item.workers || []).slice(0, 6).map(workerAvatar).join("") + "</div>";
      link = "#/agents/" + encodeURIComponent(id);
    } else {
      var t = item;
      id = t.id;
      goal = t.goal || "";
      status = t.status;
      age = ago(t.updated_at);
      pills = '<span class="pill type-single">Single agent</span>' + pill(status);
      var steps = t.steps || [];
      if (steps.length) {
        stages = '<div class="stages">' + steps.slice(0, 6).map(function (s, i) {
          var st = s.status === "completed" ? "done" : (s.status === "running" ? "running" : (s.status === "failed" ? "failed" : "pending"));
          var nm = s.name || s.tool || "step";
          return (i ? '<span class="stage-arrow">→</span>' : "") + '<span class="stage ' + st + '" title="' +
            esc(nm) + '">' + esc(nm.length > 22 ? nm.slice(0, 22) + "…" : nm) + "</span>";
        }).join("") + (steps.length > 6 ? '<span class="stat-sub">+' + (steps.length - 6) + " more</span>" : "") + "</div>";
        var sdone = steps.filter(function (s) { return s.status === "completed"; }).length;
        var spct = Math.round(100 * sdone / steps.length);
        progress = '<div class="m-progress-row"><div class="progress"><div class="bar" style="width:' + spct +
          '%"></div></div><div class="m-pct">' + spct + "%</div></div>";
      } else {
        stages = "";
        progress = "";
      }
      avatars = '<div class="agents-row"><span class="agent-chip" title="Agent task · ' +
        esc(String(status).replace(/_/g, " ")) + '"><span class="agent-dot ' + esc(status) + '">AI</span>agent</span></div>';
      link = "#/tasks/" + id;
    }
    var title = goal.length > 70 ? goal.slice(0, 70) + "…" : goal;
    return '<div class="card mission" data-link="' + esc(link) + '" tabindex="0" role="link" aria-label="' +
      esc(title) + '">' +
      '<div class="m-head"><div class="m-title">' + esc(title) + '</div><div class="m-pills">' + pills + "</div></div>" +
      '<div class="m-sub">' + esc(age) + ' · <span class="mono">' + esc(String(id).slice(0, 10)) + "</span></div>" +
      stages + progress + avatars + "</div>";
  }

  function bindMissionCards(scope) {
    scope.querySelectorAll(".mission[data-link]").forEach(function (el) {
      function go() { location.hash = el.getAttribute("data-link"); }
      el.addEventListener("click", go);
      el.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
    });
  }

  function tabsBar(tabs, activeId) {
    return '<div class="tabs" role="tablist">' + tabs.map(function (t) {
      return '<button role="tab" data-tab="' + esc(t.id) + '"' +
        (t.id === activeId ? ' class="active" aria-selected="true"' : ' aria-selected="false"') + ">" +
        esc(t.label) + "</button>";
    }).join("") + "</div>";
  }

  /* ================= overview ================= */

  function renderOverview() {
    loading(view);
    Promise.all([
      api("/api/status"), api("/api/tasks?limit=50"), api("/api/delegations"),
      api("/api/approvals?status=pending"), api("/api/quota"),
      api("/api/scheduler/jobs"), api("/api/providers/configs"),
    ]).then(function (r) {
      var status = r[0], tasks = r[1].tasks, delegations = r[2].delegations,
          pending = r[3].approvals, quota = r[4], jobs = r[5].jobs, configs = r[6].configs;
      var active = tasks.filter(function (t) {
        return t.status === "running" || t.status === "awaiting_approval" || t.status === "queued";
      });
      var activeDeleg = delegations.filter(function (m) {
        return m.status === "created" || m.status === "running" || m.status === "verifying";
      });
      var recent = tasks.filter(function (t) {
        return t.status === "completed" || t.status === "failed";
      }).slice(0, 5);
      var nextJob = jobs.filter(function (j) { return j.enabled; })
        .sort(function (a, b) { return (a.next_run_at || 9e15) - (b.next_run_at || 9e15); })[0];
      var qPct = quota.daily_cap ? Math.round(100 * quota.daily_used / quota.daily_cap) : 0;

      // success rate from the loaded sample (completed vs completed+failed)
      var done = tasks.filter(function (t) { return t.status === "completed"; }).length;
      var failedN = tasks.filter(function (t) { return t.status === "failed"; }).length;
      var srTotal = done + failedN;
      var srPct = srTotal ? Math.round(100 * done / srTotal) : null;

      view.innerHTML =
        '<div class="welcome"><div><h1>Welcome back, Muteeb</h1>' +
        "<p>Your AI agents are ready to work.</p></div>" +
        '<button class="btn primary" id="ov-new">＋ New Mission</button></div>' +

        '<div class="stat-strip">' +
        '<div class="card"><div class="ss-label">Active</div><div class="ss-val">' + (active.length + activeDeleg.length) + "</div></div>" +
        '<div class="card"><div class="ss-label">Approvals</div><div class="ss-val">' + pending.length + "</div></div>" +
        '<div class="card"><div class="ss-label">Quota</div><div class="ss-val">' + quota.daily_used + "/" + quota.daily_cap + "</div></div>" +
        '<div class="card"><div class="ss-label">Success</div><div class="ss-val">' + (srPct === null ? "—" : srPct + "%") + "</div></div>" +
        "</div>" +

        '<div class="grid cols-5" style="margin-bottom:16px">' +
        statCard("Active Missions", active.length + activeDeleg.length,
          activeDeleg.length + " multi-agent + " + active.length + " single", "pulse", "accent") +
        statCard("Pending Approvals", pending.length,
          pending.length ? "needs your review" : "all clear", "clock", "warn") +
        '<div class="card stat-card"><div class="stat-top"><span class="stat-label">API Quota Today</span>' +
        '<span class="stat-icon ok">' + ICONS.gauge + "</span></div>" +
        '<div class="stat">' + quota.daily_used + ' <span class="stat-sub">/ ' + quota.daily_cap + "</span></div>" +
        '<div class="meter"><div class="bar" style="width:' + Math.min(100, qPct) + '%"></div></div>' +
        '<div class="stat-sub" style="margin-top:6px">' + qPct + "% used" +
        (quota.cooldown_active ? " · cooldown " + quota.cooldown_left_s + "s" : "") + "</div></div>" +
        statCard("Success Rate", srPct === null ? "—" : srPct + "%",
          srPct === null ? "no finished tasks yet" : done + "/" + srTotal + " tasks", "chart", "accent") +
        statCard("Next Scheduled", nextJob ? inDur(nextJob.next_run_at) : "—",
          nextJob ? esc(nextJob.name || nextJob.id) : "scheduler idle", "calendar", "ok") +
        "</div>" +

        '<div class="ov-grid">' +
        '<div class="card"><h3 class="row-head">Active Missions <a href="#/agents">View all →</a></h3>' +
        '<div id="ov-missions" style="display:flex;flex-direction:column;gap:12px">' +
        '<div class="skel" style="min-height:120px"></div></div></div>' +
        '<div class="ov-side">' +
        '<div class="card"><h3 class="row-head">Recent Tasks <a href="#/tasks">View all →</a></h3>' +
        recentTable(recent) + "</div>" +
        '<div class="card"><h3 class="row-head">Providers <a href="#/providers">Manage →</a></h3>' +
        '<div id="ov-providers">' + providerRows(configs, status.providers) + "</div>" +
        '<a href="#/providers" class="btn ghost small" style="margin-top:10px;width:100%">＋ Add Provider</a></div>' +
        '<div class="card"><h3>System Health</h3><div id="ov-health">' + healthHtml(status) + "</div></div>" +
        "</div></div>";
      document.getElementById("ov-new").addEventListener("click", newMissionModal);
      // second phase: mission cards need plan/worker detail — fetch bounded
      loadMissionCards(active.slice(0, 4), activeDeleg.slice(0, 4));
      bindTaskRows(view);
    }).catch(function (e) { showError(view, e); });
  }

  function recentTable(recent) {
    if (!recent.length) return '<div class="result-empty">No tasks yet.</div>';
    return "<table class='rtable'><thead><tr><th>ID</th><th>Goal</th><th>Status</th><th>Updated</th></tr></thead><tbody>" +
      recent.map(function (t) {
        return "<tr class='rowlink' data-tid='" + esc(t.id) + "' tabindex='0'>" +
          "<td class='mono' data-th='ID'>" + esc(t.id.slice(0, 8)) + "</td>" +
          "<td data-th='Goal'>" + esc(t.goal.length > 44 ? t.goal.slice(0, 44) + "…" : t.goal) + "</td>" +
          "<td data-th='Status'>" + pill(t.status) + "</td>" +
          "<td class='stat-sub' data-th='Updated'>" + esc(ago(t.updated_at)) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function bindTaskRows(scope) {
    scope.querySelectorAll("[data-tid]").forEach(function (row) {
      function go() { location.hash = "#/tasks/" + row.getAttribute("data-tid"); }
      row.addEventListener("click", go);
      row.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
    });
  }

  /* Bounded second-phase: fetch detail for active items so mission cards show
     real stages, progress, and agents. Failures degrade to a simple row. */
  function loadMissionCards(activeTasks, activeDeleg) {
    var box = document.getElementById("ov-missions");
    if (!box) return;
    var jobs = [];
    activeDeleg.forEach(function (m) {
      jobs.push(api("/api/delegations/" + encodeURIComponent(m.mission_id))
        .then(function (d) { return { kind: "delegation", item: Object.assign({}, m, d) }; })
        .catch(function () { return { kind: "delegation", item: m }; }));
    });
    activeTasks.forEach(function (t) {
      jobs.push(api("/api/tasks/" + t.id)
        .then(function (d) { return { kind: "task", item: Object.assign({}, t, d) }; })
        .catch(function () { return { kind: "task", item: t }; }));
    });
    Promise.all(jobs).then(function (cards) {
      var el = document.getElementById("ov-missions");
      if (!el) return;
      if (!cards.length) {
        el.innerHTML = '<div class="result-empty">Nothing running right now. Launch a mission to put agents to work.</div>';
        return;
      }
      el.innerHTML = cards.map(function (c) { return missionCard(c.kind, c.item); }).join("");
      bindMissionCards(el);
    });
  }

  function providerRows(configs, routing) {
    if (!configs.length) return '<div class="result-empty">No providers configured.</div>';
    return configs.slice(0, 5).map(function (c) {
      var st = c.last_test_at
        ? (c.last_test_ok ? '<span class="status-dot on"></span>' : '<span class="status-dot off"></span>')
        : '<span class="status-dot"></span>';
      var label = c.last_test_at ? (c.last_test_ok ? "online" : "offline") : "not tested";
      var sub = esc(c.model || "no model") + (routing && c.id === routing.default_id ? " · default" : "");
      return '<div class="prov-row">' + st +
        '<div class="p-main"><div class="p-name">' + esc(c.name) + '</div><div class="p-sub">' + sub + "</div></div>" +
        '<span class="stat-sub">' + label + "</span></div>";
    }).join("");
  }

  function healthHtml(status) {
    var h = status.health || {};
    var b = status.browser || {};
    function row(name, entry, extra) {
      var ok = entry && entry.ok;
      var label = entry ? entry.label : "—";
      var dot = ok === true ? "on" : (ok === false ? "off" : "");
      var p = ok === true ? '<span class="pill done">' + esc(label) + "</span>"
        : ok === false ? '<span class="pill failed">' + esc(label) + "</span>"
        : '<span class="pill queued">' + esc(label) + "</span>";
      return '<div class="health-row"><span class="status-dot ' + dot + '"></span>' +
        '<div class="p-main"><div class="p-name" style="font-weight:600;font-size:13.5px">' + esc(name) + "</div>" +
        (extra ? '<div class="p-sub">' + esc(extra) + "</div>" : "") + "</div>" + p + "</div>";
    }
    var allOk = h.database && h.database.ok && h.task_runner && h.task_runner.ok;
    var rows =
      row("Database", h.database) +
      row("Tool runner", h.task_runner) +
      row("Browser automation", b.available ? { ok: true, label: "Available" } : { ok: false, label: "Unavailable" },
        b.available ? b.browser || "" : (b.error || "")) +
      row("Scheduler", h.scheduler) +
      row("Telegram (optional)", h.telegram);
    return '<div class="health-summary">' +
      (allOk ? '<span class="status-dot on"></span> All systems operational'
             : '<span class="status-dot off"></span> Attention needed') + "</div>" + rows;
  }

  /* ================= tasks ================= */
  var taskFilter = { status: "", q: "" };
  function renderTasks() {
    loading(view);
    var qs = "/api/tasks?limit=100" +
      (taskFilter.status ? "&status=" + encodeURIComponent(taskFilter.status) : "") +
      (taskFilter.q ? "&q=" + encodeURIComponent(taskFilter.q) : "");
    api(qs).then(function (d) {
      view.innerHTML =
        '<div class="page-head"><div><h1>Tasks</h1><p>' + d.tasks.length + " shown. Click a row to inspect.</p></div>" +
        '<button class="btn primary" id="new-task-btn">New mission</button></div>' +
        '<div class="filters">' +
        '<select id="f-status" aria-label="Filter by status">' +
        ["", "queued", "running", "awaiting_approval", "completed", "failed", "cancelled"].map(function (s) {
          return '<option value="' + s + '"' + (taskFilter.status === s ? " selected" : "") + ">" +
            (s || "all statuses") + "</option>";
        }).join("") + "</select>" +
        '<input id="f-q" type="text" placeholder="Search goals or IDs…" value="' + esc(taskFilter.q) + '" aria-label="Search tasks">' +
        "</div>" +
        '<div class="card" style="padding:6px 12px">' + taskTable(d.tasks) + "</div>";
      bindTaskRows(view);
      document.getElementById("new-task-btn").addEventListener("click", newMissionModal);
      document.getElementById("f-status").addEventListener("change", function (e) {
        taskFilter.status = e.target.value; renderTasks();
      });
      document.getElementById("f-q").addEventListener("input", function (e) {
        taskFilter.q = e.target.value;
        clearTimeout(window.__tq);
        window.__tq = setTimeout(renderTasks, 350);
      });
    }).catch(function (e) { showError(view, e); });
  }

  function taskTable(tasks) {
    if (!tasks.length) return '<div class="empty"><h3>No tasks yet</h3><p>Create one to get started.</p></div>';
    var rows = tasks.map(function (t) {
      return "<tr class='rowlink' data-tid='" + esc(t.id) + "' tabindex='0'>" +
        "<td class='mono' data-th='ID'>" + esc(t.id.slice(0, 8)) + "</td>" +
        "<td data-th='Goal'>" + esc(t.goal.length > 60 ? t.goal.slice(0, 60) + "…" : t.goal) + "</td>" +
        "<td class='mono' data-th='Type'>" + esc(t.task_type) + "</td>" +
        "<td data-th='Status'>" + pill(t.status) + "</td>" +
        "<td class='stat-sub' data-th='Updated'>" + esc(ago(t.updated_at)) + "</td></tr>";
    }).join("");
    return "<table class='rtable'><thead><tr><th>ID</th><th>Goal</th><th>Type</th>" +
      "<th>Status</th><th>Updated</th></tr></thead><tbody>" + rows + "</tbody></table>";
  }

  /* Supported deterministic task types and their required params.
     Only these exist in the backend (models.py); nothing else is offered. */
  var TASK_TYPES = {
    file_report: { label: "File report", desc: "Inventory files matching a pattern, with a verified report.",
      required: ["root", "pattern", "output"],
      example: { root: ".", pattern: "**/*.py", output: "report.md" } },
    file_write: { label: "File write", desc: "Write content to a file, verified by hash.",
      required: ["path", "content"],
      example: { path: "notes/todo.md", content: "# Todo\n" } },
    browser_research: { label: "Web research", desc: "Fetch a page and save a verified research report.",
      required: ["url", "output"],
      example: { url: "https://example.com", output: "research.md" } },
  };

  /* ================= task detail (tabbed) ================= */
  function classifyError(err) {
    var e = String(err || "");
    if (!e) return null;
    var l = e.toLowerCase();
    if (/timed?\s?out|deadline exceeded|read timeout/.test(l))
      return { kind: "timeout", label: "Timeout",
        title: "The task timed out",
        hint: "A step exceeded its time budget. Resuming re-runs from the last checkpoint — usually safe." };
    if (/401|unauthorized|forbidden|invalid api key|authentication|auth failed|bad credentials/.test(l))
      return { kind: "auth", label: "Authentication",
        title: "Authentication failed",
        hint: "The provider rejected the credentials. Check the key in Providers — auth errors are never retried automatically." };
    if (/429|rate.?limit|quota|cooldown|insufficient_quota|resource_exhausted/.test(l))
      return { kind: "provider", label: "Rate limit",
        title: "Provider rate limit or quota",
        hint: "The provider refused the request. Wait out the cooldown or switch the default provider." };
    if (/connection|network|dns|econnrefused|econnreset|socket|provider|model not found|overloaded|503|502/.test(l))
      return { kind: "provider", label: "Provider error",
        title: "The model provider failed",
        hint: "The request never completed. Fallback providers are tried automatically where configured." };
    if (/verif|acceptance|criterion not met|unmet/.test(l))
      return { kind: "verification", label: "Verification",
        title: "Verification failed",
        hint: "The work finished but acceptance criteria were not met. Inspect the evidence before retrying." };
    return { kind: "tool", label: "Step failure",
      title: "A step failed",
      hint: "A tool call or execution step returned an error. The failed step is highlighted in the timeline." };
  }

  var taskTab = "overview";
  function renderTaskDetail(taskId) {
    loading(view);
    api("/api/tasks/" + taskId).then(function (t) {
      t.id = taskId;
      renderTaskShell(t);
      renderTaskTab(t, taskTab);
      renderTaskActions(t);
      if (t.status === "running" || t.status === "awaiting_approval" || t.status === "queued") {
        watchTask(taskId, function () { api("/api/tasks/" + taskId).then(function (nt) {
          nt.id = taskId;
          renderTaskActions(nt);
          renderTaskTab(nt, taskTab);
          if (nt.status === "completed" || nt.status === "failed" || nt.status === "cancelled") {
            stopSse();
            renderTaskShell(nt);
            renderTaskTab(nt, taskTab);
            renderTaskActions(nt);
            toast("Task " + nt.status, nt.status === "completed" ? "ok" : "bad");
          }
        }).catch(function () {}); });
      }
    }).catch(function (e) { showError(view, e); });
  }

  function renderTaskShell(t) {
    var goal = t.goal || "";
    view.innerHTML =
      '<a class="back-link" href="#/tasks">← Tasks</a>' +
      '<div class="page-head"><div><h1 style="font-size:22px">Task Details</h1>' +
      '<p class="mono" style="word-break:break-all">' + esc(t.id) + "</p></div>" +
      '<div class="toolbar"><div id="task-actions" style="display:flex;gap:10px;flex-wrap:wrap"></div>' +
      '<span style="align-self:center">' + pill(t.status) + "</span></div></div>" +
      tabsBar([
        { id: "overview", label: "Overview" },
        { id: "execution", label: "Execution" },
        { id: "logs", label: "Logs" },
        { id: "verification", label: "Verification" },
      ], taskTab) +
      '<div id="task-tab-body"></div>';
    view.querySelectorAll(".tabs [data-tab]").forEach(function (b) {
      b.addEventListener("click", function () {
        taskTab = b.getAttribute("data-tab");
        view.querySelectorAll(".tabs [data-tab]").forEach(function (x) {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-selected", x === b ? "true" : "false");
        });
        api("/api/tasks/" + t.id).then(function (nt) { nt.id = t.id; renderTaskTab(nt, taskTab); })
          .catch(function () {});
      });
    });
  }

  function renderTaskTab(t, tab) {
    var body = document.getElementById("task-tab-body");
    if (!body) return;
    if (tab === "execution") { body.innerHTML = execTabHtml(t); return; }
    if (tab === "logs") { body.innerHTML = logsTabHtml(t); return; }
    if (tab === "verification") { body.innerHTML = verifyTabHtml(t); return; }
    body.innerHTML = taskOverviewHtml(t);
    var da = document.getElementById("diag-actions");
    if (da && (t.status === "failed" || t.status === "cancelled")) {
      da.innerHTML = '<button class="btn primary small" id="diag-resume">Resume from checkpoint</button>';
      document.getElementById("diag-resume").addEventListener("click", function () {
        api("/api/tasks/" + t.id + "/resume", { method: "POST" })
          .then(function () { toast("Task resumed", "ok"); route(); })
          .catch(function (e) { toast(e.message, "bad"); });
      });
    }
  }

  function taskOverviewHtml(t) {
    var diag = classifyError(t.error);
    var failedStep = null;
    (t.steps || []).forEach(function (s) { if (s.status === "failed") failedStep = s; });
    var resumes = (t.events || []).filter(function (ev) {
      return ev.kind === "status" && ev.data && ev.data.status === "queued";
    }).length - 1;
    var diagHtml = "";
    if (diag) {
      diagHtml = '<div class="diag diag-kind-' + diag.kind + '" role="alert">' +
        '<div class="diag-kind">' + esc(diag.label) + "</div>" +
        "<h4>" + esc(diag.title) + "</h4>" +
        "<p>" + esc(diag.hint) + "</p>" +
        (failedStep ? '<p>Failed step: <strong>' + esc(failedStep.name) + '</strong> ' +
          '<span class="mono stat-sub">' + esc(failedStep.tool || "") + "</span></p>" : "") +
        '<pre>' + esc(t.error) + "</pre>" +
        '<div class="diag-actions" id="diag-actions"></div></div>';
    }
    var execMs = null;
    if (t.created_at && t.updated_at) execMs = t.updated_at - t.created_at;
    var resultHtml;
    if (!t.result) {
      resultHtml = '<div class="result-empty">No result yet. ' +
        (t.status === "running" || t.status === "queued" || t.status === "awaiting_approval"
          ? "The task is still working — results appear here when it finishes."
          : "The task ended without producing a result.") + "</div>";
    } else {
      var verified = !!t.result.verified;
      resultHtml = (verified ? "" :
        '<p class="stat-sub" style="margin-bottom:8px"><span class="pill queued">unverified</span> ' +
        "finished, but acceptance criteria were not confirmed</p>") +
        '<pre class="output">' + esc(JSON.stringify(t.result, null, 2)) + "</pre>";
    }
    return '<div class="card" style="margin-bottom:16px"><h3>Goal</h3>' +
      "<p>" + esc(t.goal) + "</p>" +
      '<div class="kv">' +
      "<dt>Created</dt><dd>" + esc(fmtTime(t.created_at)) + "</dd>" +
      "<dt>Updated</dt><dd>" + esc(fmtTime(t.updated_at)) + "</dd>" +
      "<dt>Execution time</dt><dd>" + esc(fmtDur(execMs)) + "</dd>" +
      "<dt>Mode</dt><dd>" + esc(t.task_type === "agent" ? "Model-driven" : "Deterministic") + "</dd>" +
      (resumes > 0 ? "<dt>Resumes</dt><dd>" + resumes + " from checkpoint(s)</dd>" : "") +
      "</div></div>" +
      diagHtml +
      '<div class="card"><h3>Result &amp; evidence</h3>' + resultHtml + "</div>";
  }

  function execTabHtml(t) {
    var items = [];
    (t.steps || []).forEach(function (s) {
      var args = s.args ? JSON.stringify(s.args) : "{}";
      var out = "";
      if (s.result && s.result.output) {
        var str = JSON.stringify(s.result.output);
        out = '<pre class="output">' + esc(str.length > 1200 ? str.slice(0, 1200) + "…" : str) + "</pre>";
      }
      items.push('<li class="' + esc(s.status) + '"><div class="t-head"><strong>' + esc(s.name) +
        '</strong><span class="mono stat-sub">' + esc(s.tool) + "</span>" + pill(s.status) + "</div>" +
        '<div class="t-meta mono">' + esc(args.length > 200 ? args.slice(0, 200) + "…" : args) + "</div>" +
        (s.error ? '<div class="t-meta" style="color:var(--bad)">' + esc(s.error) + "</div>" : "") + out + "</li>");
    });
    if ((t.turns || []).length) {
      items.push('<li class="done"><div class="t-head"><strong>Model conversation</strong>' +
        '<span class="stat-sub">' + t.turns.length + " turns checkpointed</span></div>" +
        '<div class="t-meta">Full history persisted in SQLite; shown truncated in the API.</div></li>');
    }
    return '<div class="card"><h3>Execution timeline</h3><ul class="timeline">' +
      (items.length ? items.join("") :
        '<li><div class="t-head"><span class="stat-sub">No steps recorded yet.</span></div></li>') +
      "</ul></div>";
  }

  function logsTabHtml(t) {
    var events = t.events || [];
    if (!events.length) return '<div class="card"><div class="result-empty">No events recorded for this task.</div></div>';
    return '<div class="card" style="padding:6px 12px"><table class="rtable"><thead><tr>' +
      "<th>Time</th><th>Event</th><th>Detail</th></tr></thead><tbody>" +
      events.map(function (ev) {
        var detail = "";
        try {
          var d = ev.data || {};
          detail = d.status || d.error || d.message || JSON.stringify(d).slice(0, 160);
        } catch (e) { detail = ""; }
        return "<tr><td class='stat-sub mono' data-th='Time'>" + esc(fmtTime(ev.ts)) + "</td>" +
          "<td class='mono' data-th='Event'>" + esc(ev.kind || "") + "</td>" +
          "<td class='stat-sub' data-th='Detail'>" + esc(String(detail).slice(0, 200)) + "</td></tr>";
      }).join("") + "</tbody></table></div>";
  }

  function verifyTabHtml(t) {
    var v = t.result && t.result.verified;
    var acc = (t.result && t.result.acceptance) || (t.result && t.result.acceptance_criteria) || [];
    return '<div class="card"><h3>Verification</h3>' +
      '<p>' + (v ? '<span class="pill done">verified</span>' : '<span class="pill queued">unverified</span>') +
      ' <span class="stat-sub">model claims ≠ verified completion</span></p>' +
      (acc.length
        ? "<ul class='crit'>" + acc.map(function (c) {
            return "<li>" + esc(typeof c === "string" ? c : JSON.stringify(c)) + "</li>";
          }).join("") + "</ul>"
        : '<p class="stat-sub">No acceptance criteria recorded for this task.</p>') +
      (t.result ? '<h3 style="margin-top:16px">Raw result</h3><pre class="output">' +
        esc(JSON.stringify(t.result, null, 2).slice(0, 4000)) + "</pre>" : "") +
      "</div>";
  }

  function renderTaskActions(t) {
    var el = document.getElementById("task-actions");
    if (!el) return;
    var html = "";
    if (t.status === "running" || t.status === "awaiting_approval" || t.status === "queued") {
      html += '<button class="btn danger small" id="a-cancel">Cancel</button>';
    }
    if (t.status === "failed" || t.status === "cancelled" || t.status === "awaiting_approval") {
      html += '<button class="btn small" id="a-resume">Resume</button>';
    }
    el.innerHTML = html;
    var c = document.getElementById("a-cancel");
    if (c) c.addEventListener("click", function () {
      if (!confirm("Cancel this task? The agent will stop at the next checkpoint.")) return;
      api("/api/tasks/" + t.id + "/cancel", { method: "POST" })
        .then(function () { toast("Cancel requested", "ok"); })
        .catch(function (e) { toast(e.message, "bad"); });
    });
    var r = document.getElementById("a-resume");
    if (r) r.addEventListener("click", function () {
      api("/api/tasks/" + t.id + "/resume", { method: "POST" })
        .then(function () { toast("Task resumed", "ok"); route(); })
        .catch(function (e) { toast(e.message, "bad"); });
    });
  }

  /* ================= approvals ================= */
  function renderApprovals() {
    loading(view);
    Promise.all([api("/api/approvals?status=pending"), api("/api/audit?limit=40")]).then(function (r) {
      var pending = r[0].approvals, audit = r[1].events;
      view.innerHTML =
        '<div class="page-head"><div><h1>Approvals</h1>' +
        "<p>High-risk tool actions wait here for your explicit decision. A model's request is never permission by itself.</p></div></div>" +
        (pending.length ? '<div class="grid cols-2">' + pending.map(approvalCard).join("") + "</div>"
          : '<div class="empty"><h3>No pending approvals</h3><p>Agents are not waiting on you.</p></div>') +
        '<div class="card" style="margin-top:16px"><h3>Recent audit events</h3>' + auditTable(audit) + "</div>";
      pending.forEach(function (a) {
        document.getElementById("ap-ok-" + a.id).addEventListener("click", function () { decide(a.id, "approved"); });
        document.getElementById("ap-no-" + a.id).addEventListener("click", function () { decide(a.id, "rejected"); });
      });
    }).catch(function (e) { showError(view, e); });
  }

  function approvalCard(a) {
    return '<div class="card"><h3 class="mono">' + esc(a.tool) + '</h3>' +
      '<div class="stat-sub">task <span class="mono">' + esc(a.task_id.slice(0, 8)) + "</span> · " + esc(ago(a.created_at)) + "</div>" +
      (a.reason ? '<p class="stat-sub">' + esc(a.reason) + "</p>" : "") +
      '<pre class="output">' + esc(JSON.stringify(a.args, null, 2)) + "</pre>" +
      '<div class="actions" style="display:flex;gap:10px;justify-content:flex-end;margin-top:12px">' +
      '<button class="btn danger" id="ap-no-' + a.id + '">Reject</button>' +
      '<button class="btn ok" id="ap-ok-' + a.id + '">Approve</button></div></div>';
  }

  function decide(id, decision) {
    api("/api/approvals/" + id, { method: "POST", body: { decision: decision } })
      .then(function () {
        toast("Approval " + decision, decision === "approved" ? "ok" : "bad");
        refreshApprovalBadge();
        renderApprovals();
      })
      .catch(function (e) { toast(e.message, "bad"); });
  }

  function auditTable(events) {
    if (!events.length) return '<div class="empty"><p>No audit events yet.</p></div>';
    return "<table class='rtable'><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Task</th></tr></thead><tbody>" +
      events.map(function (e) {
        return "<tr><td class='stat-sub' data-th='Time'>" + esc(fmtTime(e.ts)) + "</td><td data-th='Actor'>" + esc(e.actor) +
          "</td><td class='mono' data-th='Action'>" + esc(e.action) + "</td><td class='mono' data-th='Task'>" +
          esc((e.task_id || "").slice(0, 8)) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  /* ================= scheduler ================= */
  function renderScheduler() {
    loading(view);
    Promise.all([api("/api/scheduler/jobs"), api("/api/scheduler/runs?limit=15")]).then(function (r) {
      var jobs = r[0].jobs, runs = r[1].runs;
      view.innerHTML =
        '<div class="page-head"><div><h1>Scheduler</h1><p>Persistent jobs. State survives restarts; runs never overlap.</p></div></div>' +
        '<div class="card" style="padding:6px 12px;margin-bottom:16px"><table class="rtable"><thead><tr>' +
        "<th>Job</th><th>Interval</th><th>Status</th><th>Next run</th><th>Last run</th><th>Actions</th></tr></thead><tbody>" +
        (jobs.length ? jobs.map(jobRow).join("") : '<tr><td colspan="6"><div class="empty"><p>No jobs yet. Run <code>nextclaw scheduler ensure</code> in the terminal.</p></div></td></tr>') +
        "</tbody></table></div>" +
        '<div class="card"><h3>Recent runs</h3>' + runTable(runs) + "</div>";
      jobs.forEach(function (j) {
        var en = document.getElementById("sj-en-" + j.id), dis = document.getElementById("sj-dis-" + j.id),
            rn = document.getElementById("sj-rn-" + j.id);
        if (en) en.addEventListener("click", function () { schedAction(j.id, "enable"); });
        if (dis) dis.addEventListener("click", function () { schedAction(j.id, "disable"); });
        if (rn) rn.addEventListener("click", function () { schedAction(j.id, "run-now"); });
      });
    }).catch(function (e) { showError(view, e); });
  }

  function jobRow(j) {
    var last = j.last_run;
    return "<tr><td data-th='Job'><strong>" + esc(j.name || j.id) + "</strong><div class='stat-sub mono'>" + esc(j.id) + "</div></td>" +
      "<td data-th='Interval'>" + esc(fmtInterval(j.interval_s)) + "</td>" +
      "<td data-th='Status'>" + (j.enabled ? '<span class="pill done">enabled</span>' : '<span class="pill queued">disabled</span>') + "</td>" +
      "<td class='stat-sub' data-th='Next run'>" + esc(j.enabled ? inDur(j.next_run_at) : "—") + "</td>" +
      "<td data-th='Last run'>" + (last ? pill(last.status) + " <span class='stat-sub'>" + esc(ago(last.started_at)) + "</span>" : "—") + "</td>" +
      '<td data-th="Actions"><div class="toolbar">' +
      (j.enabled
        ? '<button class="btn small" id="sj-dis-' + j.id + '">Disable</button>'
        : '<button class="btn small" id="sj-en-' + j.id + '">Enable</button>') +
      '<button class="btn small" id="sj-rn-' + j.id + '">Run now</button>' +
      "</div></td></tr>";
  }

  function fmtInterval(s) {
    if (!s) return "—";
    if (s < 3600) return Math.round(s / 60) + "m";
    return (s / 3600) + "h";
  }

  function runTable(runs) {
    if (!runs.length) return '<div class="empty"><p>No runs recorded yet.</p></div>';
    return "<table class='rtable'><thead><tr><th>Run</th><th>Job</th><th>Trigger</th><th>Status</th><th>Started</th></tr></thead><tbody>" +
      runs.map(function (r) {
        return "<tr><td class='mono' data-th='Run'>#" + esc(r.id) + "</td><td class='mono' data-th='Job'>" + esc(r.job_id) +
          "</td><td data-th='Trigger'>" + esc(r.trigger || "") + "</td><td data-th='Status'>" + pill(r.status) + "</td><td class='stat-sub' data-th='Started'>" +
          esc(ago(r.started_at)) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function schedAction(jobId, action) {
    api("/api/scheduler/jobs/" + jobId + "/" + action, { method: "POST" })
      .then(function () { toast("Job " + action.replace("-", " ") + "d", "ok"); renderScheduler(); })
      .catch(function (e) { toast(e.message, "bad"); });
  }

  /* ================= providers ================= */
  var ADAPTER_LABELS = {
    openai_compat: "OpenAI-compatible", anthropic: "Anthropic",
    gemini: "Gemini", ollama: "Ollama", bedrock: "Bedrock"
  };

  function renderProviders() {
    loading(view);
    Promise.all([
      api("/api/providers/configs"), api("/api/providers/presets"),
      api("/api/providers/routing"), api("/api/quota")
    ]).then(function (r) {
      var configs = r[0].configs, presets = r[1].presets,
          routing = r[2], quota = r[3];
      var byId = {};
      configs.forEach(function (c) { byId[c.id] = c; });

      var html =
        '<div class="page-head"><div><h1>AI Providers &amp; Models</h1>' +
        "<p>Keys stay on the server and are never shown in full. " +
        "Prefer <span class='mono'>env:VAR_NAME</span> references over pasted keys.</p></div>" +
        '<div><button class="btn" id="p-add">Add provider</button></div></div>' +

        '<div class="card"><h3>Routing</h3>' +
        '<div class="kv"><dt>Default model</dt><dd><select id="p-default">' +
        configs.filter(function (c) { return c.enabled; }).map(function (c) {
          return '<option value="' + esc(c.id) + '"' +
            (c.id === routing.default_id ? " selected" : "") + ">" +
            esc(c.name) + " · " + esc(c.model || "no model") + "</option>";
        }).join("") + "</select></dd>" +
        "<dt>Fallbacks (in order)</dt><dd id='p-fallbacks'>" +
        routing.fallback_ids.map(function (id, i) {
          var c = byId[id];
          if (!c) return "";
          return '<div class="fb-row"><span class="mono">' + esc(c.name) + "</span>" +
            ' <button class="btn small" data-fb="up" data-id="' + esc(id) + '">↑</button>' +
            ' <button class="btn small" data-fb="down" data-id="' + esc(id) + '">↓</button>' +
            ' <button class="btn small" data-fb="rm" data-id="' + esc(id) + '">✕</button></div>';
        }).join("") + "</dd></div>" +
        '<div class="toolbar"><button class="btn small" id="p-save-routing">Save routing</button>' +
        ' <span class="stat-sub">Fallbacks are tried in order on transient failures, ' +
        "rate limits, and quota exhaustion. Auth errors never retry the same provider.</span></div></div>" +

        '<div class="grid cols-2" style="margin-top:16px">' +
        configs.map(providerCard).join("") + "</div>" +

        '<div class="card" style="margin-top:16px"><h3>Usage &amp; quota</h3>' +
        '<div class="kv"><dt>Requests today</dt><dd>' + quota.daily_used + " / " + quota.daily_cap + "</dd>" +
        "<dt>Cooldown</dt><dd>" + (quota.cooldown_active ? "active — " + quota.cooldown_left_s + "s left" : "none") +
        "</dd></div></div>" +

        '<div class="card hidden" id="p-form-card" style="margin-top:16px"><h3 id="p-form-title">Add provider</h3>' +
        '<div class="kv">' +
        '<dt>Preset</dt><dd><select id="pf-preset"><option value="">— custom —</option>' +
        presets.map(function (p) {
          return '<option value="' + esc(p.id) + '">' + esc(p.display) + "</option>";
        }).join("") + "</select> <span class='stat-sub' id='pf-preset-note'></span></dd>" +
        '<dt>Name</dt><dd><input id="pf-name" placeholder="my-openai"></dd>' +
        '<dt>Adapter</dt><dd><select id="pf-adapter">' +
        Object.keys(ADAPTER_LABELS).map(function (a) {
          return '<option value="' + a + '">' + ADAPTER_LABELS[a] + "</option>";
        }).join("") + "</select></dd>" +
        '<dt>Base URL</dt><dd><input id="pf-base" class="mono" placeholder="https://api.openai.com"></dd>' +
        '<dt>Model</dt><dd><input id="pf-model" class="mono" placeholder="gpt-5-mini"> ' +
        '<button class="btn small" id="pf-discover" type="button">Discover</button> ' +
        '<select id="pf-models" class="hidden"></select></dd>' +
        '<dt>API key</dt><dd><input id="pf-key" type="password" autocomplete="off" placeholder="leave blank to keep existing"> ' +
        '<span class="stat-sub">stored server-side, 0600</span></dd>' +
        '<dt>Key from env var</dt><dd><input id="pf-keyenv" class="mono" placeholder="OPENAI_API_KEY"> ' +
        '<span class="stat-sub">preferred — wins over a stored key</span></dd>' +
        '<dt>AWS secret key</dt><dd><input id="pf-awssecret" type="password" autocomplete="off" placeholder="Bedrock only"></dd>' +
        '<dt>Extra headers (JSON)</dt><dd><input id="pf-headers" class="mono" placeholder=\'{"HTTP-Referer": "https://example.com"}\'></dd>' +
        '<dt>Extra params (JSON)</dt><dd><input id="pf-params" class="mono" placeholder=\'{"api-version": "2024-10-21"}\'></dd>' +
        '<dt>Timeout (s)</dt><dd><input id="pf-timeout" type="number" value="60" min="5" max="600"></dd>' +
        '<dt>Max retries</dt><dd><input id="pf-retries" type="number" value="2" min="0" max="10"></dd>' +
        "</div>" +
        '<div class="toolbar">' +
        '<label><input type="checkbox" id="pf-private"> allow private/loopback URL (LAN inference)</label> ' +
        '<label><input type="checkbox" id="pf-compat"> JSON tool-compat mode (experimental)</label> ' +
        '<label><input type="checkbox" id="pf-enabled" checked> enabled</label></div>' +
        '<div class="toolbar"><button class="btn" id="pf-save">Save</button> ' +
        '<button class="btn small" id="pf-cancel">Cancel</button> ' +
        '<span class="stat-sub" id="pf-err"></span></div></div>';

      view.innerHTML = html;
      window._ncPresets = presets;
      window._ncConfigs = configs;
      window._ncRouting = routing;
      bindProviders();
    }).catch(function (e) { showError(view, e); });
  }

  function capBadges(caps) {
    function p(on, label, title) {
      return '<span class="pill ' + (on ? "done" : "queued") + '" title="' + esc(title) + '">' + label + "</span>";
    }
    return p(caps.tools, "tools", "Native tool/function calling") +
      p(caps.streaming, "stream", "Streaming responses") +
      p(caps.vision, "vision", "Image input") +
      p(caps.json_mode, "json", "JSON mode");
  }

  function providerCard(c) {
    var caps = c.capabilities || {};
    var test = c.last_test_at
      ? (c.last_test_ok ? '<span class="pill done">ok</span>'
         : '<span class="pill failed" title="' + esc(c.last_test_error || "") + '">failed</span>')
      : '<span class="stat-sub">not tested</span>';
    return '<div class="card"><h3>' + esc(c.name) +
      (c.enabled ? "" : ' <span class="pill queued">disabled</span>') + "</h3>" +
      '<div class="kv">' +
      "<dt>Adapter</dt><dd>" + esc(ADAPTER_LABELS[c.adapter] || c.adapter) + "</dd>" +
      "<dt>Model</dt><dd class='mono'>" + esc(c.model || "—") + "</dd>" +
      (c.base_url ? "<dt>Endpoint</dt><dd class='mono'>" + esc(c.base_url) + "</dd>" : "") +
      "<dt>Key</dt><dd class='mono'>" + esc(c.api_key_masked || "—") + "</dd>" +
      "<dt>Capabilities</dt><dd>" + capBadges(caps) +
      (caps.note ? ' <span class="stat-sub">' + esc(caps.note) + "</span>" : "") + "</dd>" +
      "<dt>Last test</dt><dd>" + test + "</dd></div>" +
      '<div class="toolbar">' +
      '<button class="btn small" data-act="test" data-id="' + esc(c.id) + '">Test</button> ' +
      '<button class="btn small" data-act="prompt" data-id="' + esc(c.id) + '">Test prompt</button> ' +
      '<button class="btn small" data-act="edit" data-id="' + esc(c.id) + '">Edit</button> ' +
      '<button class="btn small" data-act="toggle" data-id="' + esc(c.id) + '">' + (c.enabled ? "Disable" : "Enable") + "</button> " +
      '<button class="btn small" data-act="default" data-id="' + esc(c.id) + '">Set default</button> ' +
      '<button class="btn small" data-act="fallback" data-id="' + esc(c.id) + '">± Fallback</button> ' +
      '<button class="btn small" data-act="del" data-id="' + esc(c.id) + '">Delete</button></div>' +
      '<div class="stat-sub" id="pr-' + esc(c.id) + '" style="margin-top:8px"></div>' +
      '<div class="hidden" id="pp-' + esc(c.id) + '" style="margin-top:8px">' +
      '<textarea id="ppt-' + esc(c.id) + '" rows="2" style="width:100%" placeholder="Test prompt…"></textarea>' +
      '<div class="toolbar"><button class="btn small" data-act="send" data-id="' + esc(c.id) + '">Send</button></div>' +
      '<div class="mono" id="ppo-' + esc(c.id) + '" style="white-space:pre-wrap"></div></div></div>';
  }

  function bindProviders() {
    var addBtn = document.getElementById("p-add");
    if (addBtn) addBtn.addEventListener("click", function () { openForm(null); });

    document.getElementById("p-save-routing").addEventListener("click", function () {
      var def = document.getElementById("p-default").value;
      api("/api/providers/routing", {
        method: "POST", body: { default_id: def, fallback_ids: window._ncRouting.fallback_ids }
      }).then(function () { toast("Routing saved"); renderProviders(); })
        .catch(function (e) { toast(e.message, "bad"); });
    });

    view.querySelectorAll("[data-fb]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var fb = window._ncRouting.fallback_ids.slice();
        var id = btn.getAttribute("data-id"), op = btn.getAttribute("data-fb");
        var i = fb.indexOf(id);
        if (op === "rm") fb.splice(i, 1);
        if (op === "up" && i > 0) { fb[i] = fb[i - 1]; fb[i - 1] = id; }
        if (op === "down" && i < fb.length - 1) { fb[i] = fb[i + 1]; fb[i + 1] = id; }
        window._ncRouting.fallback_ids = fb;
        api("/api/providers/routing", { method: "POST", body: { fallback_ids: fb } })
          .then(function () { renderProviders(); })
          .catch(function (e) { toast(e.message, "bad"); });
      });
    });

    view.querySelectorAll("[data-act]").forEach(function (btn) {
      btn.addEventListener("click", function () { cardAction(btn); });
    });
  }

  function cardAction(btn) {
    var id = btn.getAttribute("data-id"), act = btn.getAttribute("data-act");
    var res = document.getElementById("pr-" + id);
    if (act === "test") {
      res.textContent = "Testing…";
      api("/api/providers/configs/" + id + "/test", { method: "POST" }).then(function (d) {
        res.innerHTML = d.ok
          ? '<span class="pill done">reachable</span> <span class="stat-sub">' + esc((d.models || d.models_sample || []).join(", ")) + " " + esc(d.note || "") + "</span>"
          : '<span class="pill failed">failed</span> <span class="stat-sub">' + esc(d.error || "") + "</span>";
      }).catch(function (e) { res.textContent = e.message; });
    } else if (act === "prompt") {
      document.getElementById("pp-" + id).classList.toggle("hidden");
    } else if (act === "send") {
      var prompt = document.getElementById("ppt-" + id).value.trim();
      var out = document.getElementById("ppo-" + id);
      if (!prompt) { out.textContent = "Enter a prompt first."; return; }
      out.textContent = "Thinking…";
      api("/api/providers/configs/" + id + "/chat", { method: "POST", body: { prompt: prompt } })
        .then(function (d) {
          out.textContent = d.text + "\n\n— " + d.model + " · " + d.latency_ms + "ms · " + d.finish_reason;
        })
        .catch(function (e) { out.textContent = "Error: " + e.message; });
    } else if (act === "edit") {
      var cfg = window._ncConfigs.filter(function (c) { return c.id === id; })[0];
      openForm(cfg);
    } else if (act === "toggle") {
      var c2 = window._ncConfigs.filter(function (c) { return c.id === id; })[0];
      api("/api/providers/configs/" + id, { method: "PUT", body: { enabled: !c2.enabled } })
        .then(function () { renderProviders(); }).catch(function (e) { toast(e.message, "bad"); });
    } else if (act === "default") {
      api("/api/providers/routing", { method: "POST", body: { default_id: id } })
        .then(function () { toast("Default updated"); renderProviders(); })
        .catch(function (e) { toast(e.message, "bad"); });
    } else if (act === "fallback") {
      var fb = window._ncRouting.fallback_ids.slice();
      var i = fb.indexOf(id);
      if (i >= 0) fb.splice(i, 1); else fb.push(id);
      api("/api/providers/routing", { method: "POST", body: { fallback_ids: fb } })
        .then(function () { renderProviders(); }).catch(function (e) { toast(e.message, "bad"); });
    } else if (act === "del") {
      if (!confirm("Delete this provider configuration?")) return;
      api("/api/providers/configs/" + id, { method: "DELETE" })
        .then(function () { renderProviders(); }).catch(function (e) { toast(e.message, "bad"); });
    }
  }

  function openForm(cfg) {
    var card = document.getElementById("p-form-card");
    card.classList.remove("hidden");
    document.getElementById("p-form-title").textContent = cfg ? "Edit provider" : "Add provider";
    card.dataset.editId = cfg ? cfg.id : "";
    var presets = window._ncPresets || [];
    function fill(p) {
      document.getElementById("pf-adapter").value = p.adapter;
      document.getElementById("pf-base").value = p.base_url || "";
      document.getElementById("pf-model").value = p.default_model || "";
      document.getElementById("pf-keyenv").value = p.api_key_env || "";
      document.getElementById("pf-params").value = p.extra_params ? JSON.stringify(p.extra_params) : "";
      document.getElementById("pf-private").checked = !!p.allow_private;
      document.getElementById("pf-preset-note").textContent = p.notes || "";
    }
    document.getElementById("pf-preset").onchange = function (e) {
      var p = presets.filter(function (x) { return x.id === e.target.value; })[0];
      if (p) {
        if (!cfg) document.getElementById("pf-name").value = p.display.toLowerCase().replace(/[^a-z0-9]+/g, "-");
        fill(p);
      } else document.getElementById("pf-preset-note").textContent = "";
    };
    if (cfg) {
      document.getElementById("pf-name").value = cfg.name;
      document.getElementById("pf-adapter").value = cfg.adapter;
      document.getElementById("pf-base").value = cfg.base_url || "";
      document.getElementById("pf-model").value = cfg.model || "";
      document.getElementById("pf-keyenv").value = cfg.api_key_env || "";
      document.getElementById("pf-params").value = JSON.stringify(cfg.extra_params || {});
      document.getElementById("pf-headers").value = JSON.stringify(cfg.extra_headers || {});
      document.getElementById("pf-timeout").value = cfg.timeout_s || 60;
      document.getElementById("pf-retries").value = cfg.max_retries != null ? cfg.max_retries : 2;
      document.getElementById("pf-private").checked = !!cfg.allow_private;
      document.getElementById("pf-compat").checked = !!cfg.compat_json_tools;
      document.getElementById("pf-enabled").checked = !!cfg.enabled;
      document.getElementById("pf-key").placeholder = cfg.api_key_masked
        ? "stored (" + cfg.api_key_masked + ") — leave blank to keep"
        : "leave blank for none";
    } else {
      ["pf-name", "pf-base", "pf-model", "pf-key", "pf-keyenv", "pf-awssecret",
       "pf-headers", "pf-params"].forEach(function (i) { document.getElementById(i).value = ""; });
      document.getElementById("pf-timeout").value = "60";
      document.getElementById("pf-retries").value = "2";
      ["pf-private", "pf-compat"].forEach(function (i) { document.getElementById(i).checked = false; });
      document.getElementById("pf-enabled").checked = true;
      document.getElementById("pf-key").placeholder = "leave blank to keep existing";
    }
    document.getElementById("pf-save").onclick = saveForm;
    document.getElementById("pf-cancel").onclick = function () { card.classList.add("hidden"); };
    document.getElementById("pf-discover").onclick = discoverModels;
    card.scrollIntoView();
  }

  function parseJsonField(id, errEl) {
    var v = document.getElementById(id).value.trim();
    if (!v) return {};
    try { return JSON.parse(v); }
    catch (e) { errEl.textContent = "Invalid JSON in " + id; throw e; }
  }

  function saveForm() {
    var card = document.getElementById("p-form-card");
    var errEl = document.getElementById("pf-err");
    errEl.textContent = "";
    var body;
    try {
      body = {
        name: document.getElementById("pf-name").value.trim(),
        adapter: document.getElementById("pf-adapter").value,
        display_name: "",
        base_url: document.getElementById("pf-base").value.trim(),
        model: document.getElementById("pf-model").value.trim(),
        api_key_env: document.getElementById("pf-keyenv").value.trim(),
        extra_headers: parseJsonField("pf-headers", errEl),
        extra_params: parseJsonField("pf-params", errEl),
        timeout_s: parseInt(document.getElementById("pf-timeout").value, 10) || 60,
        max_retries: parseInt(document.getElementById("pf-retries").value, 10) || 0,
        allow_private: document.getElementById("pf-private").checked,
        compat_json_tools: document.getElementById("pf-compat").checked,
        enabled: document.getElementById("pf-enabled").checked
      };
    } catch (e) { return; }
    var key = document.getElementById("pf-key").value;
    var aws = document.getElementById("pf-awssecret").value;
    if (key) body.api_key = key;
    if (aws) body.aws_secret_key = aws;
    if (body.adapter === "bedrock" && !body.extra_params.region) {
      errEl.textContent = "Bedrock needs a region in extra params, e.g. {\"region\": \"us-east-1\"}";
      return;
    }
    var editId = card.dataset.editId;
    var req = editId
      ? api("/api/providers/configs/" + editId, { method: "PUT", body: body })
      : api("/api/providers/configs", { method: "POST", body: body });
    req.then(function () { toast("Provider saved"); renderProviders(); })
      .catch(function (e) { errEl.textContent = e.message; });
  }

  function discoverModels() {
    var card = document.getElementById("p-form-card");
    var editId = card.dataset.editId;
    var sel = document.getElementById("pf-models");
    sel.innerHTML = "<option>…</option>";
    sel.classList.remove("hidden");
    function done(models) {
      sel.innerHTML = models.length
        ? models.map(function (m) { return "<option>" + esc(m) + "</option>"; }).join("")
        : "<option value=''>no models found</option>";
    }
    if (editId) {
      api("/api/providers/configs/" + editId + "/models").then(function (d) { done(d.models || []); })
        .catch(function (e) { done([]); toast(e.message, "bad"); });
    } else {
      var body = {
        name: "probe", adapter: document.getElementById("pf-adapter").value,
        base_url: document.getElementById("pf-base").value.trim(),
        model: document.getElementById("pf-model").value.trim() || "probe",
        api_key: document.getElementById("pf-key").value,
        api_key_env: document.getElementById("pf-keyenv").value.trim(),
        allow_private: document.getElementById("pf-private").checked
      };
      api("/api/providers/configs", { method: "POST", body: body }).then(function (c) {
        return api("/api/providers/configs/" + c.id + "/models").then(function (d) {
          done(d.models || []);
          return api("/api/providers/configs/" + c.id, { method: "DELETE" }).catch(function () {});
        });
      }).catch(function (e) { done([]); toast(e.message, "bad"); });
    }
    sel.onchange = function () {
      if (sel.value) document.getElementById("pf-model").value = sel.value;
    };
  }

  /* ================= tools ================= */
  function renderTools() {
    loading(view);
    api("/api/policy").then(function (policy) {
      var tools = policy.tools || [];
      var risky = tools.filter(function (t) { return t.requires_approval; }).length;
      view.innerHTML =
        '<div class="page-head"><div><h1>Tools</h1>' +
        "<p>" + tools.length + " tools in the registry · " + risky + " require approval. " +
        "High-risk calls surface in <a href='#/approvals'>Approvals</a>.</p></div></div>" +
        '<div class="card" style="padding:6px 12px"><table class="rtable"><thead><tr>' +
        "<th>Tool</th><th>Risk</th><th>Approval</th><th>Description</th></tr></thead><tbody>" +
        tools.map(function (t) {
          return "<tr><td class='mono' data-th='Tool'>" + esc(t.name) + "</td>" +
            "<td data-th='Risk'>" + esc(t.risk) + "</td><td data-th='Approval'>" +
            (t.requires_approval ? '<span class="pill awaiting_approval">required</span>' : '<span class="pill queued">no</span>') +
            "</td><td class='stat-sub' data-th='Description'>" + esc(t.description) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }).catch(function (e) { showError(view, e); });
  }

  /* ================= memory ================= */
  var memQuery = "";
  function renderMemory() {
    loading(view);
    api("/api/memory" + (memQuery ? "?query=" + encodeURIComponent(memQuery) : "") + "&limit=50")
      .then(function (d) {
        var entries = d.entries || [];
        view.innerHTML =
          '<div class="page-head"><div><h1>Memory</h1>' +
          "<p>What your agents remember. Read-only here — agents write memories during tasks.</p></div></div>" +
          '<div class="filters"><input id="mem-q" type="text" placeholder="Search memories…" ' +
          'value="' + esc(memQuery) + '" aria-label="Search memories"></div>' +
          '<div class="card">' +
          (entries.length ? entries.map(memEntry).join("")
            : '<div class="empty"><h3>No memories' + (memQuery ? ' match "' + esc(memQuery) + '"' : " yet") +
              "</h3><p>Agents store durable facts here as they work.</p></div>") +
          "</div>";
        document.getElementById("mem-q").addEventListener("input", function (e) {
          memQuery = e.target.value;
          clearTimeout(window.__mq);
          window.__mq = setTimeout(renderMemory, 400);
        });
      }).catch(function (e) { showError(view, e); });
  }

  function memEntry(e) {
    return '<div class="mem-entry"><div class="m-text">' + esc(e.text) + "</div>" +
      '<div class="m-meta">' +
      (e.source ? '<span class="tag">' + esc(e.source) + "</span>" : "") +
      (e.tags || []).map(function (t) { return '<span class="tag">' + esc(t) + "</span>"; }).join("") +
      "<span>updated " + esc(ago(e.updated_at)) + "</span>" +
      "</div></div>";
  }

  /* ================= settings ================= */
  function renderSettings() {
    loading(view);
    Promise.all([api("/api/status"), api("/api/policy")]).then(function (r) {
      var status = r[0], policy = r[1];
      view.innerHTML =
        '<div class="page-head"><div><h1>Settings</h1><p>Workspace, limits, and the active permission policy.</p></div></div>' +
        '<div class="grid cols-2">' +
        '<div class="card"><h3>Runtime</h3><div class="kv">' +
        "<dt>Workspace</dt><dd class='mono'>" + esc(status.workspace_root) + "</dd>" +
        "<dt>Data dir</dt><dd class='mono'>" + esc(status.data_dir) + "</dd>" +
        "<dt>Offline mode</dt><dd>" + (status.offline ? "on" : "off") + "</dd>" +
        "<dt>Version</dt><dd class='mono'>" + esc(status.version) + "</dd></div>" +
        '<div class="toolbar"><button class="btn small ghost" id="lock-btn2">Lock dashboard</button></div></div>' +
        '<div class="card"><h3>Execution limits</h3><div class="kv">' +
        "<dt>Max turns</dt><dd>" + esc(policy.max_turns) + "</dd>" +
        "<dt>Step attempts</dt><dd>" + esc(policy.max_step_attempts) + "</dd>" +
        "<dt>Task timeout</dt><dd>" + esc(policy.task_timeout_s) + "s</dd>" +
        "<dt>Model timeout</dt><dd>" + esc(policy.llm_timeout_s) + "s</dd></div>" +
        '<p class="stat-sub">Limits are configured via environment / config file. ' +
        "Model and endpoint can be changed from Providers.</p></div></div>" +
        '<div class="card" style="margin-top:16px"><h3>Security — API token</h3>' +
        "<p class='stat-sub'>Token source: <span class='mono'>" + esc(status.token_source || "file") + "</span>. " +
        (status.token_source === "env"
          ? "Managed via NEXTCLAW_API_TOKEN; rotation is disabled — change the environment variable instead."
          : "Rotating invalidates the current token immediately. Other sessions and saved tokens stop working.") +
        "</p><div class='toolbar'><button class='btn small danger' id='rotate-token-btn'" +
        (status.token_source === "env" ? " disabled" : "") + ">Rotate API token</button></div></div>" +
        '<div class="card" style="margin-top:16px"><h3>Permission policy</h3>' +
        "<p class='stat-sub'>" + (policy.tools || []).length + " registered tools. " +
        "<a href='#/tools'>Browse the full tool registry →</a></p></div>";
      document.getElementById("lock-btn2").addEventListener("click", lock);
      var rotBtn = document.getElementById("rotate-token-btn");
      if (rotBtn && !rotBtn.disabled) rotBtn.addEventListener("click", rotateTokenModal);
    }).catch(function (e) { showError(view, e); });
  }

  function rotateTokenModal() {
    modal(
      "<h2>Rotate API token</h2>" +
      '<p class="stat-sub">This generates a new dashboard token and <strong>invalidates the current one immediately</strong>. ' +
      "Other browser sessions, saved tokens, and scripts using the old token will stop working. " +
      "The new token is shown <strong>once</strong> — save it somewhere safe.</p>" +
      '<div id="rt-new" class="hidden"><label class="field"><span>New token (shown once)</span>' +
      '<textarea id="rt-token" rows="2" class="mono" readonly></textarea></label>' +
      '<div class="toolbar"><button class="btn small" id="rt-copy">Copy</button></div></div>' +
      '<div id="rt-error" class="error hidden"></div>' +
      '<div class="actions"><button class="btn ghost" id="rt-cancel">Cancel</button>' +
      '<button class="btn primary danger" id="rt-confirm">Rotate token</button></div>'
    );
    document.getElementById("rt-cancel").addEventListener("click", closeModal);
    document.getElementById("rt-confirm").addEventListener("click", function () {
      var errEl = document.getElementById("rt-error");
      api("/api/auth/rotate", { method: "POST", body: { confirm: true } }).then(function (d) {
        token = d.token;
        storeDel("nc_token");
        document.getElementById("rt-new").classList.remove("hidden");
        document.getElementById("rt-token").value = d.token;
        document.getElementById("rt-confirm").disabled = true;
        document.getElementById("rt-cancel").textContent = "Done";
        toast("Token rotated", "ok");
      }).catch(function (e) {
        errEl.textContent = e.message; errEl.classList.remove("hidden");
      });
    });
    document.getElementById("rt-copy").addEventListener("click", function () {
      var ta = document.getElementById("rt-token");
      ta.select();
      try { document.execCommand("copy"); toast("Copied", "ok"); }
      catch (e) { toast("Copy manually", "bad"); }
    });
  }

  /* ================= multi-agent delegations ================= */
  var delegProfiles = null;
  var delegPollTimer = null;
  var delegDetailCache = null;
  var delegTab = "plan";

  function stopDelegPoll() {
    if (delegPollTimer) { clearInterval(delegPollTimer); delegPollTimer = null; }
  }

  function delegProfilesCached() {
    if (delegProfiles) return Promise.resolve(delegProfiles);
    return api("/api/delegations/profiles").then(function (d) {
      delegProfiles = d.profiles;
      return delegProfiles;
    });
  }

  function delegActive(status) {
    return status === "created" || status === "running" || status === "verifying";
  }

  /* ----- mission list ----- */
  function renderDelegations() {
    stopDelegPoll();
    loading(view);
    Promise.all([api("/api/delegations"), api("/api/delegations/settings")])
      .then(function (res) { renderDelegationList(res[0].delegations, res[1].enabled); })
      .catch(function (e) { showError(view, e); });
  }

  function renderDelegationList(missions, enabled) {
    view.innerHTML =
      '<div class="page-head"><div><h1>Multi-Agent</h1>' +
      "<p>Delegated missions: a lead plan executed by specialist worker agents.</p></div>" +
      '<div class="toolbar">' +
      '<button class="btn" id="deleg-settings-btn">Delegation settings</button>' +
      '<button class="btn primary" id="new-mission-btn"' + (enabled ? "" : " disabled") + ">New mission</button>" +
      "</div></div>" +
      (enabled ? "" :
        '<div class="error">Multi-agent delegation is currently disabled. Enable it in Delegation settings to launch missions.</div>') +
      '<div id="deleg-list" style="display:flex;flex-direction:column;gap:12px">' +
      (missions.length ? '<div class="skel" style="min-height:120px"></div>'
        : '<div class="empty"><h3>No missions yet</h3><p>Launch a delegated mission to coordinate multiple worker agents.</p></div>') +
      "</div>";
    document.getElementById("deleg-settings-btn").addEventListener("click", delegSettingsModal);
    var nb = document.getElementById("new-mission-btn");
    if (nb && enabled) nb.addEventListener("click", function () { newMissionModal(); });
    if (!missions.length) return;
    // bounded detail fetch for mission cards
    Promise.all(missions.slice(0, 12).map(function (m) {
      return api("/api/delegations/" + encodeURIComponent(m.mission_id))
        .then(function (d) { return { item: Object.assign({}, m, d) }; })
        .catch(function () { return { item: m }; });
    })).then(function (cards) {
      var box = document.getElementById("deleg-list");
      if (!box) return;
      box.innerHTML = cards.map(function (c) { return missionCard("delegation", c.item); }).join("");
      bindMissionCards(box);
    });
  }

  function delegSettingsModal() {
    api("/api/delegations/settings").then(function (s) {
      modal(
        "<h2>Delegation settings</h2>" +
        '<p class="stat-sub">Controls whether new multi-agent missions can be launched. ' +
        "In-flight missions are unaffected by disabling.</p>" +
        '<label class="check"><input type="checkbox" id="ds-enabled"' +
        (s.enabled ? " checked" : "") + "> <span>Enable multi-agent delegation</span></label>" +
        '<div id="ds-error" class="error hidden"></div>' +
        '<div class="actions"><button class="btn ghost" id="ds-cancel">Cancel</button>' +
        '<button class="btn primary" id="ds-save">Save</button></div>'
      );
      document.getElementById("ds-cancel").addEventListener("click", closeModal);
      document.getElementById("ds-save").addEventListener("click", function () {
        var on = document.getElementById("ds-enabled").checked;
        api("/api/delegations/settings", { method: "PUT", body: { enabled: on } })
          .then(function () { closeModal(); toast("Delegation " + (on ? "enabled" : "disabled"), "ok"); renderDelegations(); })
          .catch(function (e) {
            var el = document.getElementById("ds-error");
            el.textContent = e.message; el.classList.remove("hidden");
          });
      });
    }).catch(function (e) { toast(e.message, "bad"); });
  }

  /* ----- mission detail (tabbed) ----- */
  function renderDelegationDetail(missionId) {
    stopDelegPoll();
    loading(view);
    api("/api/delegations/" + encodeURIComponent(missionId)).then(function (d) {
      delegDetailCache = d;
      renderDelegationShell(d);
      renderDelegTab(d, delegTab);
      if (delegActive(d.status)) {
        delegPollTimer = setInterval(function () {
          api("/api/delegations/" + encodeURIComponent(missionId)).then(function (nd) {
            delegDetailCache = nd;
            renderDelegTab(nd, delegTab, true);
            if (!delegActive(nd.status)) { stopDelegPoll(); renderDelegationShell(nd); renderDelegTab(nd, delegTab); }
          }).catch(function () {});
        }, 4000);
      }
    }).catch(function (e) { showError(view, e); });
  }

  function renderDelegationShell(d) {
    var goal = d.goal || "";
    view.innerHTML =
      '<a class="back-link" href="#/agents">← Multi-Agent</a>' +
      '<div class="page-head"><div><h1 style="font-size:22px">' + esc(goal.length > 80 ? goal.slice(0, 80) + "…" : goal) + "</h1>" +
      '<p class="mono" style="word-break:break-all">' + esc(d.mission_id) + "</p></div>" +
      '<div class="toolbar"><div id="deleg-actions" style="display:flex;gap:10px;flex-wrap:wrap"></div>' +
      '<span style="align-self:center">' + pill(d.status) + "</span></div></div>" +
      (d.retry_of ? '<p class="stat-sub" style="margin-bottom:12px">Retry of <a href="#/agents/' + esc(d.retry_of) + '">' + esc(d.retry_of) + "</a></p>" : "") +
      tabsBar([
        { id: "plan", label: "Plan" },
        { id: "agents", label: "Agents" },
        { id: "files", label: "Files" },
        { id: "logs", label: "Logs" },
        { id: "verification", label: "Verification" },
      ], delegTab) +
      '<div id="deleg-tab-body"></div>';
    view.querySelectorAll(".tabs [data-tab]").forEach(function (b) {
      b.addEventListener("click", function () {
        delegTab = b.getAttribute("data-tab");
        view.querySelectorAll(".tabs [data-tab]").forEach(function (x) {
          x.classList.toggle("active", x === b);
          x.setAttribute("aria-selected", x === b ? "true" : "false");
        });
        renderDelegTab(delegDetailCache, delegTab);
      });
    });
    renderDelegActions(d);
  }

  function renderDelegTab(d, tab, live) {
    var body = document.getElementById("deleg-tab-body");
    if (!body || !d) return;
    if (tab === "agents") { body.innerHTML = delegAgentsHtml(d); bindWorkerRows(body, d); }
    else if (tab === "files") { body.innerHTML = delegFilesHtml(d); }
    else if (tab === "logs") { renderDelegLogs(body, d, live); }
    else if (tab === "verification") { body.innerHTML = delegVerifyHtml(d); }
    else { body.innerHTML = delegPlanHtml(d); bindWorkerRows(body, d); }
    renderDelegActions(d);
  }

  function workerStatusText(w, workers) {
    if (w.status === "completed") return w.summary || "Done";
    if (w.status === "failed") return w.error || w.errors[0] || "Failed";
    if (w.status === "cancelled") return "Cancelled";
    if (w.status === "awaiting_approval") return "Waiting on your approval";
    if (w.status === "running") return w.summary || "Working…";
    var deps = (w.depends_on || []).filter(function (dep) {
      var dw = workers.filter(function (x) { return x.subtask_id === dep; })[0];
      return dw && dw.status !== "completed";
    });
    if (deps.length) {
      var names = deps.map(function (dep) {
        var dw = workers.filter(function (x) { return x.subtask_id === dep; })[0];
        return dw ? dw.name : dep;
      });
      return "Waiting for " + names.slice(0, 2).join(", ") + (names.length > 2 ? " +" + (names.length - 2) : "");
    }
    return "Queued";
  }

  function delegPlanHtml(d) {
    var plan = d.plan || [];
    var wById = {};
    (d.workers || []).forEach(function (w) { wById[w.subtask_id] = w; });
    if (!plan.length) return '<div class="card"><div class="result-empty">No plan recorded.</div></div>';
    var levels = planLevels(plan);
    var dag = '<div class="dag">' + levels.map(function (subs, i) {
      var col = '<div class="dag-level"><div class="dag-level-head">Stage ' + (i + 1) + "</div>" +
        subs.map(function (s) {
          var w = wById[s.id] || {};
          var st = w.status || "pending";
          var role = s.kind === "tool" ? "tool:" + (s.tool || "") : (s.profile || "worker");
          var deps = (s.depends_on || []).map(function (x) {
            return '<span class="dep">' + esc(x) + "</span>";
          }).join("");
          return '<div class="dag-node" data-wid="' + esc(s.id) + '" tabindex="0" role="link" style="cursor:pointer">' +
            '<div class="n-role"><span class="agent-dot ' + esc(st) + '">' + esc(initials(role)) + "</span>" +
            esc(role.length > 16 ? role.slice(0, 16) + "…" : role) + "</div>" +
            '<div class="n-name">' + esc(s.name || s.id) + "</div>" + pill(st) +
            (deps ? '<div class="n-deps">' + deps + "</div>" : "") + "</div>";
        }).join("") + "</div>";
      return (i ? '<div class="dag-conn">→</div>' : "") + col;
    }).join("") + "</div>";
    var p = delegProgress(d);
    return '<div class="card" style="margin-bottom:16px"><h3 class="row-head">Execution plan ' +
      '<span class="stat-sub">' + p.done + "/" + p.total + " completed · " + p.pct + "%</span></h3>" +
      '<div class="m-progress-row"><div class="progress"><div class="bar" style="width:' + p.pct + '%"></div></div></div>' +
      '<p class="stat-sub">A worker starts when its dependencies complete. Click a node to inspect it.</p>' + dag + "</div>";
  }

  function delegProgress(d) {
    var total = (d.workers || []).length;
    var done = (d.workers || []).filter(function (w) { return w.status === "completed"; }).length;
    return { total: total, done: done, pct: total ? Math.round(100 * done / total) : 0 };
  }

  function delegAgentsHtml(d) {
    var workers = d.workers || [];
    if (!workers.length) return '<div class="card"><div class="result-empty">No workers yet.</div></div>';
    return '<div class="card" style="padding:6px 18px">' + workers.map(function (w) {
      var role = w.kind === "tool" ? "tool:" + (w.tool || "") : (w.profile || "worker");
      return '<div class="prov-row" data-wid="' + esc(w.subtask_id) + '" tabindex="0" role="link" style="cursor:pointer">' +
        '<span class="agent-dot ' + esc(w.status) + '">' + esc(initials(role)) + "</span>" +
        '<div class="p-main"><div class="p-name">' + esc(role.length > 20 ? role.slice(0, 20) + "…" : role) +
        ' <span class="stat-sub">· ' + esc(w.name || "") + "</span></div>" +
        '<div class="p-sub">' + esc(workerStatusText(w, workers)) + "</div></div>" +
        '<span class="stat-sub">' + esc(ago(w.updated_at)) + "</span>" + pill(w.status) + "</div>";
    }).join("") + "</div>";
  }

  function delegFilesHtml(d) {
    var arts = d.artifacts || [];
    (d.workers || []).forEach(function (w) {
      (w.artifacts || []).forEach(function (a) {
        if (!arts.some(function (x) { return x.name === a; })) arts.push({ name: a, worker_id: w.subtask_id });
      });
    });
    if (!arts.length) return '<div class="card"><div class="result-empty">No artifacts produced yet.</div></div>';
    return '<div class="card"><h3>Artifacts</h3><ul class="artifacts">' + arts.map(function (a) {
      return "<li><span class='mono'>" + esc(a.name) + "</span> " +
        (a.version ? "<span class='stat-sub'>v" + esc(String(a.version)) + "</span> " : "") +
        "<span class='stat-sub'>· " + esc((a.worker_id || "").slice(0, 12)) + "</span></li>";
    }).join("") + "</ul></div>";
  }

  function renderDelegLogs(body, d, live) {
    if (live && body.dataset.loaded) return; // don't refetch on every poll tick
    var workers = (d.workers || []).filter(function (w) { return w.task_id; });
    if (!workers.length) {
      body.innerHTML = '<div class="card"><div class="result-empty">No worker tasks to show logs for yet.</div></div>';
      body.dataset.loaded = "1";
      return;
    }
    if (!live) {
      body.innerHTML = '<div class="card"><h3>Worker logs</h3>' +
        workers.map(function (w, i) {
          return '<div class="wlog"><button class="wlog-head" data-wl="' + i + '">' +
            esc(w.name || w.subtask_id) + " " + pill(w.status) + "</button>" +
            '<div class="wlog-body hidden" id="wlog-' + i + '"><div class="skel"></div></div></div>';
        }).join("") + "</div>";
      body.dataset.loaded = "1";
      workers.forEach(function (w, i) {
        var btn = body.querySelector('[data-wl="' + i + '"]');
        var pane = document.getElementById("wlog-" + i);
        btn.addEventListener("click", function () {
          pane.classList.toggle("hidden");
          if (pane.dataset.done) return;
          pane.dataset.done = "1";
          api("/api/tasks/" + w.task_id).then(function (t) {
            var evs = t.events || [];
            pane.innerHTML = evs.length
              ? "<table class='rtable'><thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead><tbody>" +
                evs.slice(-30).map(function (ev) {
                  var det = "";
                  try {
                    var dd = ev.data || {};
                    det = dd.status || dd.error || dd.message || JSON.stringify(dd).slice(0, 140);
                  } catch (e) {}
                  return "<tr><td class='stat-sub mono' data-th='Time'>" + esc(fmtTime(ev.ts)) + "</td>" +
                    "<td class='mono' data-th='Event'>" + esc(ev.kind || "") + "</td>" +
                    "<td class='stat-sub' data-th='Detail'>" + esc(String(det).slice(0, 180)) + "</td></tr>";
                }).join("") + "</tbody></table>"
              : "<p class='stat-sub'>No events recorded.</p>";
          }).catch(function (e) { pane.innerHTML = "<p class='stat-sub'>Could not load logs: " + esc(e.message) + "</p>"; });
        });
      });
    }
  }

  function delegVerifyHtml(d) {
    var v = d.verification;
    if (!v) return '<div class="card"><div class="result-empty">No verification report yet. Verification runs when the mission finishes.</div></div>';
    var overall = v.verified === true ? '<span class="pill done">verified</span>'
      : v.verified === false ? '<span class="pill failed">not verified</span>'
      : '<span class="pill queued">unknown</span>';
    var checks = v.checks || v.criteria || [];
    return '<div class="card"><h3>Verification ' + overall + "</h3>" +
      (v.summary ? "<p>" + esc(v.summary) + "</p>" : "") +
      (checks.length ? "<ul class='crit'>" + checks.map(function (c) {
        if (typeof c === "string") return "<li>" + esc(c) + "</li>";
        return "<li>" + esc(c.name || c.criterion || JSON.stringify(c)) +
          (c.passed === true ? ' <span class="pill done">pass</span>' :
           c.passed === false ? ' <span class="pill failed">fail</span>' : "") + "</li>";
      }).join("") + "</ul>" : "") +
      '<h3 style="margin-top:16px">Raw report</h3><pre class="output">' +
      esc(JSON.stringify(v, null, 2).slice(0, 4000)) + "</pre></div>";
  }

  function bindWorkerRows(scope, d) {
    scope.querySelectorAll("[data-wid]").forEach(function (el) {
      function open() { workerModal(el.getAttribute("data-wid")); }
      el.addEventListener("click", open);
      el.addEventListener("keydown", function (e) { if (e.key === "Enter") open(); });
    });
  }

  function renderDelegActions(d) {
    var el = document.getElementById("deleg-actions");
    if (!el) return;
    var html = "";
    var failed = (d.workers || []).some(function (w) { return w.status === "failed"; });
    var awaiting = (d.workers || []).some(function (w) { return w.status === "awaiting_approval"; });
    if (delegActive(d.status)) {
      html += '<button class="btn danger small" id="deleg-cancel">Cancel</button>';
    }
    if ((d.status === "completed" || d.status === "failed") && failed) {
      html += '<button class="btn small" id="deleg-retry">Retry failed</button>';
    }
    var planner = (d.workers || []).filter(function (w) {
      return w.profile === "planner" && w.status === "completed";
    });
    if (planner.length) {
      html += '<button class="btn small" id="deleg-launch-plan">Launch plan as mission</button>';
    }
    el.innerHTML = html;
    var c = document.getElementById("deleg-cancel");
    if (c) c.addEventListener("click", function () {
      if (!confirm("Cancel this mission? Running workers will stop at the next checkpoint.")) return;
      api("/api/delegations/" + encodeURIComponent(d.mission_id) + "/cancel", { method: "POST" })
        .then(function () { toast("Cancel requested", "ok"); renderDelegationDetail(d.mission_id); })
        .catch(function (e) { toast(e.message, "bad"); });
    });
    var r = document.getElementById("deleg-retry");
    if (r) r.addEventListener("click", function () {
      api("/api/delegations/" + encodeURIComponent(d.mission_id) + "/retry", { method: "POST" })
        .then(function (nd) {
          toast("Retry launched: " + nd.mission_id.slice(0, 10), "ok");
          location.hash = "#/agents/" + nd.mission_id;
        })
        .catch(function (e) { toast(e.message, "bad"); });
    });
    var lp = document.getElementById("deleg-launch-plan");
    if (lp) lp.addEventListener("click", function () { launchFromPlanner(planner[0]); });
    if (awaiting) {
      var warn = document.createElement("div");
      warn.className = "error";
      warn.style.marginBottom = "12px";
      warn.innerHTML = 'A worker is waiting on human approval. Review it in <a href="#/approvals">Approvals</a>.';
      var tb = document.getElementById("deleg-tab-body");
      if (tb && !document.getElementById("deleg-await-warn")) {
        warn.id = "deleg-await-warn";
        tb.parentNode.insertBefore(warn, tb);
      }
    } else {
      var old = document.getElementById("deleg-await-warn");
      if (old) old.remove();
    }
  }

  function workerModal(subtaskId) {
    var d = delegDetailCache;
    if (!d) return;
    var w = (d.workers || []).filter(function (x) { return x.subtask_id === subtaskId; })[0];
    if (!w) return;
    var res = (d.results && d.results[subtaskId]) || {};
    var evidence = res.evidence ? JSON.stringify(res.evidence, null, 2) : "";
    if (evidence.length > 3000) evidence = evidence.slice(0, 3000) + "…";
    modal(
      "<h2>" + esc(w.name) + ' <span class="mono stat-sub">' + esc(w.subtask_id) + "</span></h2>" +
      "<p>" + pill(w.status) + ' <span class="mono stat-sub">' +
      esc(w.kind === "tool" ? "tool: " + w.tool : w.profile) + " · " + esc(String(w.attempts)) + " attempt(s)</span></p>" +
      (w.goal ? '<label class="field"><span>Instructions</span><pre class="output">' + esc(w.goal) + "</pre></label>" : "") +
      (w.depends_on && w.depends_on.length ? '<p class="stat-sub">Depends on: ' +
        w.depends_on.map(function (x) { return '<span class="dep">' + esc(x) + "</span>"; }).join(" ") + "</p>" : "") +
      (w.acceptance_criteria && w.acceptance_criteria.length ? '<label class="field"><span>Acceptance criteria</span><ul class="crit">' +
        w.acceptance_criteria.map(function (c) { return "<li>" + esc(typeof c === "string" ? c : JSON.stringify(c)) + "</li>"; }).join("") +
        "</ul></label>" : "") +
      (w.summary ? '<label class="field"><span>Summary</span><pre class="output">' + esc(w.summary) + "</pre></label>" : "") +
      (w.errors && w.errors.length ? '<label class="field"><span>Errors</span><pre class="output err">' +
        esc(w.errors.join("\n")) + "</pre></label>" : "") +
      (w.error ? '<label class="field"><span>Task error</span><pre class="output err">' + esc(w.error) + "</pre></label>" : "") +
      (evidence ? '<label class="field"><span>Evidence</span><pre class="output">' + esc(evidence) + "</pre></label>" : "") +
      (w.artifacts && w.artifacts.length ? '<p class="stat-sub">Artifacts: ' +
        w.artifacts.map(function (a) { return '<span class="mono">' + esc(a) + "</span>"; }).join(", ") + "</p>" : "") +
      (w.task_id ? '<p><a href="#/tasks/' + esc(w.task_id) + '">Open worker task →</a></p>' : "") +
      '<div class="actions"><button class="btn" id="wm-close">Close</button></div>'
    );
    document.getElementById("wm-close").addEventListener("click", closeModal);
  }

  function launchFromPlanner(w) {
    var d = delegDetailCache;
    var res = (d.results && d.results[w.subtask_id]) || {};
    var text = res.summary || "";
    var m = text.match(/\{[\s\S]*\}/);
    if (!m) { toast("Could not find a JSON plan in the planner output.", "bad"); return; }
    var parsed;
    try { parsed = JSON.parse(m[0]); }
    catch (e) { toast("Planner output is not valid JSON.", "bad"); return; }
    var subs = parsed.subtasks || parsed.plan || [];
    if (!Array.isArray(subs) || !subs.length) { toast("No subtasks found in the plan.", "bad"); return; }
    var mapped = subs.map(function (s, i) {
      return {
        id: String(s.id || ("s" + i)),
        name: String(s.name || s.id || ("subtask-" + i)),
        kind: "agent",
        profile: WORKER_PROFILE_FALLBACK(s.profile),
        goal: String(s.goal || ""),
        depends_on: Array.isArray(s.depends_on) ? s.depends_on.map(String) : [],
        acceptance_criteria: Array.isArray(s.acceptance_criteria) ? s.acceptance_criteria : [],
        max_retries: 2,
      };
    }).filter(function (s) { return s.goal; });
    if (!mapped.length) { toast("Plan subtasks have no goals.", "bad"); return; }
    newMissionModal({ goal: d.goal, subtasks: mapped, tab: "multi" });
  }

  function WORKER_PROFILE_FALLBACK(name) {
    var known = (delegProfiles || []).map(function (p) { return p.name; });
    return known.indexOf(name) !== -1 ? name : "researcher";
  }

  /* ================= new mission modal (tabbed) ================= */
  var missionTab = "simple";
  function newMissionModal(prefill) {
    prefill = prefill || {};
    if (prefill.tab) missionTab = prefill.tab;
    modal(
      '<div style="display:flex;align-items:center;gap:10px;margin-bottom:4px">' +
      '<button class="icon-btn" id="nm-back" aria-label="Back" style="width:32px;height:32px">←</button>' +
      "<div><h2 style='margin:0'>New Mission</h2>" +
      '<p class="stat-sub" style="margin:2px 0 0">Describe what you want your agents to do.</p></div></div>' +
      tabsBar([
        { id: "simple", label: "Simple" },
        { id: "multi", label: "Multi-Agent" },
        { id: "template", label: "Template" },
      ], missionTab) +
      '<div id="nm-body"></div>'
    );
    document.getElementById("nm-back").addEventListener("click", closeModal);
    document.querySelectorAll("#modal-root .tabs [data-tab]").forEach(function (b) {
      b.addEventListener("click", function () {
        missionTab = b.getAttribute("data-tab");
        document.querySelectorAll("#modal-root .tabs [data-tab]").forEach(function (x) {
          x.classList.toggle("active", x === b);
        });
        renderMissionTab(prefill);
      });
    });
    renderMissionTab(prefill);
  }

  function renderMissionTab(prefill) {
    var body = document.getElementById("nm-body");
    if (!body) return;
    if (missionTab === "multi") {
      // Don't let users compose a mission that the backend will reject.
      api("/api/delegations/settings").then(function (s) {
        if (!s.enabled) {
          body.innerHTML = '<div class="error">Multi-agent delegation is currently disabled. ' +
            "Enable it in <a href='#/agents'>Multi-Agent → Delegation settings</a> to launch missions.</div>";
          return;
        }
        renderMultiForm(body, prefill);
      }).catch(function (e) {
        body.innerHTML = '<div class="error">' + esc(e.message) + "</div>";
      });
      return;
    }
    if (missionTab === "template") renderTemplatePicker(body);
    else renderSimpleForm(body, prefill);
  }

  function renderSimpleForm(body, prefill) {
    prefill = prefill || {};
    body.innerHTML =
      '<p class="stat-sub">Deterministic missions run offline with explicit verification. ' +
      "Model-driven missions use the configured provider.</p>" +
      '<label class="field"><span>Goal</span>' +
      '<textarea id="nt-goal" rows="3" maxlength="2000" placeholder="What should the agent accomplish?">' +
      esc(prefill.goal || "") + "</textarea>" +
      '<span class="field-err hidden" id="nt-goal-err"></span></label>' +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Execution mode</span><select id="nt-mode">' +
      '<option value="run">Deterministic (offline)</option><option value="run-agent">Model-driven</option>' +
      "</select></label>" +
      '<label class="field"><span>Mission type</span><select id="nt-type">' +
      Object.keys(TASK_TYPES).map(function (k) {
        return '<option value="' + k + '"' + (prefill.type === k ? " selected" : "") + ">" + TASK_TYPES[k].label + "</option>";
      }).join("") + "</select>" +
      '<span class="stat-sub" id="nt-type-desc"></span></label></div>' +
      '<details class="adv"><summary>Advanced parameters</summary>' +
      '<label class="field"><span>Parameters (JSON) — required: <span class="mono" id="nt-req"></span></span>' +
      '<textarea id="nt-params" rows="4" class="mono" spellcheck="false">' +
      esc(prefill.params ? JSON.stringify(prefill.params, null, 2) : "") + "</textarea>" +
      '<span class="field-err hidden" id="nt-params-err"></span></label></details>' +
      '<div id="nt-error" class="error hidden"></div>' +
      '<div class="actions"><button class="btn ghost" id="nt-cancel">Cancel</button>' +
      '<button class="btn primary" id="nt-create">Create &amp; run</button></div>';
    var modeSel = document.getElementById("nt-mode");
    var typeSel = document.getElementById("nt-type");
    var typeDesc = document.getElementById("nt-type-desc");
    var reqEl = document.getElementById("nt-req");
    var paramsEl = document.getElementById("nt-params");
    function syncType() {
      var tt = TASK_TYPES[typeSel.value];
      typeDesc.textContent = tt.desc;
      reqEl.textContent = tt.required.join(", ");
      paramsEl.placeholder = JSON.stringify(tt.example, null, 2);
      var isAgent = modeSel.value === "run-agent";
      typeSel.disabled = isAgent;
      paramsEl.disabled = isAgent;
      paramsEl.closest("details").style.opacity = isAgent ? ".45" : "1";
    }
    modeSel.addEventListener("change", syncType);
    typeSel.addEventListener("change", syncType);
    syncType();
    function fieldErr(id, msg) {
      var el = document.getElementById(id);
      if (msg) { el.textContent = msg; el.classList.remove("hidden"); }
      else el.classList.add("hidden");
    }
    document.getElementById("nt-cancel").addEventListener("click", closeModal);
    var createBtn = document.getElementById("nt-create");
    var inFlight = false;
    createBtn.addEventListener("click", function () {
      if (inFlight) return;
      var errEl = document.getElementById("nt-error");
      errEl.classList.add("hidden");
      var goal = document.getElementById("nt-goal").value.trim();
      if (!goal) { fieldErr("nt-goal-err", "Describe the goal first."); return; }
      fieldErr("nt-goal-err", null);
      var mode = modeSel.value, params = {};
      if (mode === "run") {
        var raw = paramsEl.value.trim();
        if (!raw) {
          fieldErr("nt-params-err", "Parameters are required for deterministic missions. See the example above.");
          return;
        }
        try { params = JSON.parse(raw); }
        catch (e) { fieldErr("nt-params-err", "Not valid JSON: " + e.message); return; }
        if (typeof params !== "object" || Array.isArray(params)) {
          fieldErr("nt-params-err", "Parameters must be a JSON object."); return;
        }
        var missing = TASK_TYPES[typeSel.value].required.filter(function (k) { return !(k in params); });
        if (missing.length) {
          fieldErr("nt-params-err", "Missing required parameter(s): " + missing.join(", ")); return;
        }
        fieldErr("nt-params-err", null);
      }
      inFlight = true;
      createBtn.disabled = true;
      createBtn.innerHTML = '<span class="spinner inline" aria-hidden="true"></span> Creating…';
      api("/api/tasks", {
        method: "POST",
        body: { goal: goal, mode: mode, task_type: typeSel.value, params: params },
      }).then(function (d) {
        closeModal();
        toast("Mission created", "ok");
        location.hash = "#/tasks/" + d.task_id;
      }).catch(function (e) {
        inFlight = false;
        createBtn.disabled = false;
        createBtn.textContent = "Create & run";
        errEl.textContent = e.message +
          (e.message.indexOf("timed out") !== -1 ? " — the mission may still have been created; check Tasks before retrying." : "");
        errEl.classList.remove("hidden");
      });
    });
  }

  function renderTemplatePicker(body) {
    var tpls = [
      { type: "file_report", name: "File inventory report",
        desc: "Inventory files matching a pattern and get a verified report.",
        goal: "Inventory the Python files in this workspace and report the totals.",
        params: { root: ".", pattern: "**/*.py", output: "report.md" } },
      { type: "browser_research", name: "Web research brief",
        desc: "Fetch a page and save a verified research report.",
        goal: "Research the topic at the given URL and save a brief with key facts.",
        params: { url: "https://example.com", output: "research.md" } },
      { type: "file_write", name: "Write a file",
        desc: "Write content to a file, verified by hash.",
        goal: "Write the project notes file with the given content.",
        params: { path: "notes/todo.md", content: "# Todo\n\n- [ ] First item\n" } },
    ];
    body.innerHTML =
      '<p class="stat-sub">Start from a pre-filled deterministic mission. You can edit everything before creating it.</p>' +
      '<div class="tpl-grid">' + tpls.map(function (t, i) {
        return '<button class="tpl-card" data-tpl="' + i + '"><h4>' + esc(t.name) + "</h4><p>" + esc(t.desc) + "</p></button>";
      }).join("") + "</div>";
    body.querySelectorAll("[data-tpl]").forEach(function (b) {
      b.addEventListener("click", function () {
        var t = tpls[+b.getAttribute("data-tpl")];
        missionTab = "simple";
        newMissionModal({ goal: t.goal, type: t.type, params: t.params });
      });
    });
  }

  function renderMultiForm(body, prefill) {
    prefill = prefill || {};
    delegProfilesCached().then(function (profiles) {
      var profileOpts = profiles.map(function (p) {
        return '<option value="' + esc(p.name) + '">' + esc(p.name) + "</option>";
      }).join("");
      body.innerHTML =
        '<p class="stat-sub">The mission itself is the lead: it plans, delegates to specialist workers, ' +
        "and verifies the result. Workers run with the dashboard's default provider chain.</p>" +
        '<label class="field"><span>Mission goal</span>' +
        '<textarea id="nm-goal" rows="3" placeholder="What should the worker team accomplish?">' +
        esc(prefill.goal || "") + "</textarea></label>" +
        '<div class="grid cols-2" style="margin-bottom:16px">' +
        '<label class="field"><span>Execution mode</span><select id="nm-exec-mode">' +
        '<option value="auto">Automatic (recommended) — planner decomposes the goal</option>' +
        '<option value="manual">Custom agents — I define subtasks manually</option>' +
        "</select></label>" +
        '<div><span class="stat-sub" style="display:block;margin-bottom:8px;font-weight:600;color:var(--muted)">Allow parallel execution</span>' +
        '<label class="switch-row" style="padding:0"><span class="s-sub">Run independent subtasks at the same time</span>' +
        '<span class="switch"><input type="checkbox" id="nm-parallel" checked><span class="track"></span></span></label></div>' +
        "</div>" +
        '<div class="field"><span style="display:block;font-size:13px;color:var(--muted);margin-bottom:8px;font-weight:600">Available specialists</span>' +
        '<div class="prof-grid">' + profiles.map(function (p) {
          return '<label class="prof-card"><input type="checkbox" class="nm-prof" value="' + esc(p.name) + '" checked>' +
            '<span><span class="p-name">' + esc(p.name) + "</span>" +
            '<span class="p-desc" style="display:block">' + esc(p.description) + "</span></span></label>";
        }).join("") + "</div>" +
        '<p class="stat-sub">Checked specialists are suggested to the planner and pre-selected for manual subtasks. ' +
        "Workers always use the dashboard's default provider chain.</p></div>" +
        '<div id="nm-manual-wrap" class="hidden"><h3 style="margin:4px 0 10px">Subtasks</h3><div id="nm-subs"></div>' +
        '<button class="btn small" id="nm-add-sub">+ Add subtask</button></div>' +
        "<h3 style='margin:18px 0 10px'>Resource limits</h3>" +
        '<div class="grid cols-3">' +
        budgetField("nm-b-workers", "Max workers", 5, 1, 32) +
        budgetField("nm-b-conc", "Max concurrent", 2, 1, 8) +
        budgetField("nm-b-retries", "Retries / worker", 3, 0, 5) +
        "</div>" +
        '<details class="adv"><summary>Advanced limits</summary><div class="grid cols-3" style="margin-top:12px">' +
        budgetField("nm-b-calls", "Max model calls", 120, 1, 500) +
        budgetField("nm-b-runtime", "Max runtime (s)", 1800, 60, 7200) +
        budgetField("nm-b-depth", "Max depth", 2, 1, 3) +
        "</div></details>" +
        '<div id="nm-error" class="error hidden"></div>' +
        '<div class="actions"><button class="btn ghost" id="nm-cancel">Cancel</button>' +
        '<button class="btn primary" id="nm-create">Create Mission</button></div>';
      window.__nmProfiles = profileOpts;
      var subWrap = document.getElementById("nm-subs");
      function defaultProfile() {
        var checked = body.querySelector(".nm-prof:checked");
        return checked ? checked.value : (profiles[0] && profiles[0].name);
      }
      function addSub(data) {
        var div = document.createElement("div");
        div.className = "subtask";
        div.innerHTML = subtaskEditor(window.__nmProfiles, data || { profile: defaultProfile() });
        subWrap.appendChild(div);
        bindSubtask(div);
        if (data && data.profile) div.querySelector(".s-profile").value = data.profile;
      }
      document.getElementById("nm-add-sub").addEventListener("click", function () { addSub(null); });
      (prefill.subtasks && prefill.subtasks.length ? prefill.subtasks : [null]).forEach(addSub);
      var execSel = document.getElementById("nm-exec-mode");
      var parallelBox = document.getElementById("nm-parallel");
      function syncExec() {
        var manual = execSel.value === "manual";
        document.getElementById("nm-manual-wrap").classList.toggle("hidden", !manual);
      }
      execSel.addEventListener("change", syncExec);
      syncExec();
      document.getElementById("nm-cancel").addEventListener("click", closeModal);
      document.getElementById("nm-create").addEventListener("click", function () {
        var errEl = document.getElementById("nm-error");
        function fail(msg) { errEl.textContent = msg; errEl.classList.remove("hidden"); }
        var goal = document.getElementById("nm-goal").value.trim();
        if (!goal) return fail("Describe the mission goal first.");
        var auto = execSel.value === "auto";
        var team = Array.prototype.map.call(body.querySelectorAll(".nm-prof:checked"),
          function (c) { return c.value; });
        var subtasks;
        if (auto) {
          var teamHint = team.length
            ? "\n\nAvailable specialist profiles (prefer these): " +
              team.map(function (n) {
                var p = profiles.filter(function (x) { return x.name === n; })[0];
                return n + (p ? " — " + p.description : "");
              }).join("; ") + "."
            : "";
          subtasks = [{
            id: "plan", name: "Decompose goal into subtasks",
            kind: "agent", profile: "planner",
            goal: goal + teamHint +
              "\n\nReturn ONLY the JSON plan object specified in your instructions.",
            depends_on: [], acceptance_criteria: [], max_retries: 1, timeout_s: 600,
          }];
        } else {
          subtasks = [];
          var divs = subWrap.querySelectorAll(".subtask");
          if (!divs.length) return fail("Add at least one subtask.");
          for (var i = 0; i < divs.length; i++) {
            var st = readSubtask(divs[i], i, fail);
            if (!st) return;
            subtasks.push(st);
          }
        }
        var budgets = {};
        var conc = parseInt(document.getElementById("nm-b-conc").value, 10);
        if (parallelBox.checked && conc < 2) { conc = 2; document.getElementById("nm-b-conc").value = "2"; }
        if (!parallelBox.checked) { conc = 1; document.getElementById("nm-b-conc").value = "1"; }
        [["nm-b-workers", "max_workers"], ["nm-b-conc", "max_concurrent"],
         ["nm-b-retries", "max_retries_per_worker"], ["nm-b-calls", "max_model_calls"],
         ["nm-b-runtime", "max_runtime_s"], ["nm-b-depth", "max_depth"]].forEach(function (pair) {
          var v = parseInt(document.getElementById(pair[0]).value, 10);
          if (!isNaN(v)) budgets[pair[1]] = v;
        });
        budgets.max_concurrent = conc;
        var createBtn = document.getElementById("nm-create");
        createBtn.disabled = true;
        createBtn.innerHTML = '<span class="spinner inline" aria-hidden="true"></span> Launching…';
        api("/api/delegations", { method: "POST", body: { goal: goal, subtasks: subtasks, budgets: budgets } })
          .then(function (d) {
            closeModal();
            toast("Mission launched: " + d.mission_id.slice(0, 10), "ok");
            location.hash = "#/agents/" + d.mission_id;
          })
          .catch(function (e) {
            createBtn.disabled = false;
            createBtn.textContent = "Create Mission";
            fail(e.message);
          });
      });
    }).catch(function (e) {
      body.innerHTML = '<div class="error">' + esc(e.message) + "</div>";
    });
  }

  function budgetField(id, label, val, min, max) {
    return '<label class="field"><span>' + esc(label) + "</span>" +
      '<input id="' + id + '" type="number" min="' + min + '" max="' + max + '" value="' + val + '"></label>';
  }

  function subtaskEditor(profileOpts, data) {
    data = data || {};
    var kind = data.kind || "agent";
    return '<div class="sub-head"><strong>Subtask</strong>' +
      '<button class="btn ghost small sub-remove" type="button">Remove</button></div>' +
      '<div class="grid cols-3">' +
      '<label class="field"><span>ID</span><input class="s-id" type="text" value="' + esc(data.id || "") + '" placeholder="e.g. research"></label>' +
      '<label class="field"><span>Name</span><input class="s-name" type="text" value="' + esc(data.name || "") + '" placeholder="Short label"></label>' +
      '<label class="field"><span>Kind</span><select class="s-kind">' +
      '<option value="agent"' + (kind === "agent" ? " selected" : "") + ">agent — LLM worker</option>" +
      '<option value="tool"' + (kind === "tool" ? " selected" : "") + ">tool — single tool call</option>" +
      "</select></label></div>" +
      '<div class="s-agent' + (kind === "tool" ? " hidden" : "") + '">' +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Specialist role</span><select class="s-profile">' + profileOpts + "</select></label>" +
      '<label class="field"><span>Depends on (comma-separated IDs)</span><input class="s-deps" type="text" value="' +
      esc((data.depends_on || []).join(", ")) + '" placeholder="research"></label></div>' +
      '<label class="field"><span>Goal / instructions</span>' +
      '<textarea class="s-goal" rows="2" placeholder="Exactly what this worker should do">' + esc(data.goal || "") + "</textarea></label></div>" +
      '<div class="s-tool' + (kind === "agent" ? " hidden" : "") + '">' +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Tool name</span><input class="s-toolname" type="text" value="' + esc(data.tool || "") + '" placeholder="fs.list"></label>' +
      '<label class="field"><span>Depends on (comma-separated IDs)</span><input class="s-deps2" type="text" value="' +
      esc((data.depends_on || []).join(", ")) + '" placeholder="research"></label></div>' +
      '<label class="field"><span>Tool args (JSON)</span>' +
      '<textarea class="s-args mono" rows="2" placeholder=\'{"path": "."}\'>' + esc(data.argsJson || "") + "</textarea></label></div>" +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Acceptance criteria (one per line)</span>' +
      '<textarea class="s-criteria" rows="2" placeholder="tests pass">' + esc((data.acceptance_criteria || []).join("\n")) + "</textarea></label>" +
      '<label class="field"><span>Max retries</span><input class="s-retries" type="number" min="0" max="5" value="' +
      (data.max_retries != null ? data.max_retries : 2) + '"></label></div>';
  }

  function bindSubtask(div) {
    div.querySelector(".sub-remove").addEventListener("click", function () { div.remove(); });
    var kindSel = div.querySelector(".s-kind");
    kindSel.addEventListener("change", function () {
      var isTool = kindSel.value === "tool";
      div.querySelector(".s-agent").classList.toggle("hidden", isTool);
      div.querySelector(".s-tool").classList.toggle("hidden", !isTool);
    });
  }

  function readSubtask(div, i, fail) {
    function val(sel) { return div.querySelector(sel).value.trim(); }
    var kind = div.querySelector(".s-kind").value;
    var id = val(".s-id") || ("s" + i);
    var st = {
      id: id,
      name: val(".s-name") || id,
      kind: kind,
      depends_on: val(kind === "tool" ? ".s-deps2" : ".s-deps").split(",").map(function (s) { return s.trim(); }).filter(Boolean),
      acceptance_criteria: val(".s-criteria").split("\n").map(function (s) { return s.trim(); }).filter(Boolean),
      max_retries: parseInt(val(".s-retries"), 10) || 0,
    };
    if (kind === "agent") {
      st.profile = div.querySelector(".s-profile").value;
      st.goal = val(".s-goal");
      if (!st.goal) { fail("Subtask '" + id + "' needs a goal."); return null; }
    } else {
      st.tool = val(".s-toolname");
      if (!st.tool) { fail("Subtask '" + id + "' needs a tool name."); return null; }
      var raw = val(".s-args");
      if (raw) {
        try { st.args = JSON.parse(raw); }
        catch (e) { fail("Subtask '" + id + "': tool args are not valid JSON."); return null; }
      }
      st.goal = "";
    }
    return st;
  }

  /* ---------- boot ---------- */
  if (token) route(); else lock();
})();
