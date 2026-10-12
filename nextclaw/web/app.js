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
  function ago(ts) {
    if (!ts) return "never";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  }
  function pill(status) {
    return '<span class="pill ' + esc(status) + '">' + esc(status.replace(/_/g, " ")) + "</span>";
  }

  /* ---------- global error reporting ----------
     Vanilla equivalent of a React error boundary: catches uncaught JS
     errors and unhandled promise rejections and shows a recovery banner
     instead of leaving an unexplained blank screen. Never displays the
     API token or other secrets; guarded against recursive reporting. */
  var __fatalShown = false;
  function __sanitizeError(msg) {
    var s = String(msg == null ? "An unexpected error occurred." : msg);
    try {
      if (token && s.indexOf(token) !== -1) return "[details redacted]";
    } catch (e) {}
    return s.slice(0, 300);
  }
  function showFatalError(message) {
    if (__fatalShown) return;
    __fatalShown = true;
    try {
      var banner = document.createElement("div");
      banner.id = "fatal-error";
      banner.setAttribute("role", "alert");
      banner.innerHTML =
        '<div><strong>Something went wrong.</strong> ' +
        '<span class="stat-sub">' + esc(__sanitizeError(message)) + "</span></div>" +
        '<button class="btn primary small" id="fatal-reload">Reload</button>';
      document.body.appendChild(banner);
      document.getElementById("fatal-reload").addEventListener("click", function () {
        location.reload();
      });
    } catch (e) { /* never throw from the error handler itself */ }
  }
  window.addEventListener("error", function (ev) {
    showFatalError((ev && ev.message) || "An unexpected error occurred.");
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev && ev.reason;
    showFatalError((r && (r.message || r)) || "An unhandled promise rejection occurred.");
  });

  /* ---------- API client ---------- */
  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;
    var ctrl = null, timer = null;
    try {
      ctrl = new AbortController();
      timer = setTimeout(function () { ctrl.abort(); }, 30000);
    } catch (e) { /* older browsers: no timeout */ }
    return fetch(API + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      signal: ctrl ? ctrl.signal : undefined,
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      if (r.status === 401) { lock(); throw new Error("unauthorized"); }
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      });
    }).catch(function (e) {
      if (timer) clearTimeout(timer);
      if (e && e.name === "AbortError") {
        throw new Error("request timed out — the server took too long to respond");
      }
      throw e;
    });
  }

  /* ---------- toasts & modal ---------- */
  function toast(msg, kind) {
    var el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.textContent = msg;
    document.getElementById("toasts").appendChild(el);
    setTimeout(function () { el.remove(); }, 4200);
  }
  function modal(html) {
    var root = document.getElementById("modal-root");
    root.innerHTML = '<div class="modal-backdrop"><div class="modal" role="dialog" aria-modal="true">' + html + "</div></div>";
    root.querySelector(".modal-backdrop").addEventListener("click", function (e) {
      if (e.target.classList.contains("modal-backdrop")) closeModal();
    });
    var first = root.querySelector("input, select, textarea, button");
    if (first) first.focus();
  }
  function closeModal() { document.getElementById("modal-root").innerHTML = ""; }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeModal();
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
    document.getElementById("unlock").classList.remove("hidden");
  }
  function unlock(t) {
    token = t;
    storeSet("nc_token", t);
    document.getElementById("unlock").classList.add("hidden");
    document.getElementById("app").classList.remove("hidden");
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
  document.getElementById("theme-toggle").addEventListener("click", function () {
    theme = theme === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    document.querySelector('meta[name="theme-color"]')
      .setAttribute("content", theme === "dark" ? "#0b0e14" : "#f4f6fb");
    storeSet("nc_theme", theme);
  });

  /* ---------- mobile drawer ---------- */
  var navToggle = document.getElementById("nav-toggle");
  var drawerScrim = document.getElementById("drawer-scrim");
  function closeDrawer() {
    document.getElementById("app").classList.remove("drawer-open");
    navToggle.setAttribute("aria-expanded", "false");
    drawerScrim.classList.add("hidden");
  }
  navToggle.addEventListener("click", function () {
    var open = document.getElementById("app").classList.toggle("drawer-open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
    drawerScrim.classList.toggle("hidden", !open);
    if (open) {
      var first = document.querySelector(".sidebar nav a.active") ||
        document.querySelector(".sidebar nav a");
      if (first) first.focus();
    }
  });
  drawerScrim.addEventListener("click", closeDrawer);
  document.querySelectorAll(".sidebar nav a").forEach(function (a) {
    a.addEventListener("click", closeDrawer);
  });

  /* ---------- router ---------- */
  var view = document.getElementById("view");
  function route() {
    if (!token) return lock();
    var hash = location.hash || "#/overview";
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      a.classList.toggle("active", hash.indexOf("#/" + a.getAttribute("data-nav")) === 0);
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
        b.textContent = d.approvals.length; b.classList.remove("hidden");
        td.classList.remove("hidden");
      } else {
        b.classList.add("hidden");
        td.classList.add("hidden");
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

  /* ================= PAGES ================= */

  function renderOverview() {
    loading(view);
    Promise.all([
      api("/api/status"), api("/api/tasks?limit=50"), api("/api/delegations"),
      api("/api/approvals?status=pending"), api("/api/quota"),
      api("/api/scheduler/jobs"),
    ]).then(function (r) {
      var status = r[0], tasks = r[1].tasks, delegations = r[2].delegations,
          pending = r[3].approvals, quota = r[4], jobs = r[5].jobs;
      var active = tasks.filter(function (t) {
        return t.status === "running" || t.status === "awaiting_approval" || t.status === "queued";
      });
      var failed = tasks.filter(function (t) { return t.status === "failed"; }).slice(0, 5);
      var recentDone = tasks.filter(function (t) { return t.status === "completed"; }).slice(0, 6);
      var activeDeleg = delegations.filter(function (m) {
        return m.status === "created" || m.status === "running" || m.status === "verifying";
      });
      var nextJob = jobs.filter(function (j) { return j.enabled; })
        .sort(function (a, b) { return (a.next_run_at || 9e15) - (b.next_run_at || 9e15); })[0];

      view.innerHTML =
        '<div class="hero"><div><h2>Mission control</h2>' +
        "<p>Launch autonomous work, watch agents execute, verify the results.</p></div>" +
        '<button class="btn primary" id="ov-new">New mission</button></div>' +

        '<div class="card" style="margin-bottom:16px"><h3 class="row-head">Active work ' +
        '<span class="stat-sub">' + active.length + " task(s) · " + activeDeleg.length + " delegation(s)</span></h3>" +
        '<div id="ov-active">' + activeWorkHtml(active, activeDeleg) + "</div></div>" +

        '<div class="grid cols-2" style="margin-bottom:16px">' +
        '<div class="card"><h3 class="row-head">Needs attention <a href="#/approvals">Approvals →</a></h3>' +
        attentionHtml(pending, failed) + "</div>" +
        '<div class="card"><h3 class="row-head">Recent results <a href="#/tasks">Tasks →</a></h3>' +
        (recentDone.length ? '<ul class="rows" id="ov-recent">' + recentDone.map(recentRow).join("") + "</ul>"
          : '<div class="result-empty">No completed tasks yet.</div>') + "</div>" +
        "</div>" +

        '<div class="grid cols-4">' +
        statCard("API quota today", quota.daily_used + " / " + quota.daily_cap,
          quota.cooldown_active ? "cooldown: " + quota.cooldown_left_s + "s left" : "healthy") +
        statCard("Next scheduled run", nextJob ? fmtTime(nextJob.next_run_at) : "none",
          nextJob ? esc(nextJob.name) : "scheduler idle") +
        statCard("Providers", status.providers.count + " configured",
          (status.providers.default_name ? esc(status.providers.default_name) + " · default" : "no default") +
          (status.browser && status.browser.available ? " · browser ok" : "")) +
        statCard("Verification", "—", "checking recent tasks…") +
        "</div>";
      document.getElementById("ov-new").addEventListener("click", newTaskModal);
      enhanceOverview(active.slice(0, 3), recentDone.slice(0, 4));
    }).catch(function (e) { showError(view, e); });
  }

  function activeWorkHtml(active, activeDeleg) {
    var html = "";
    if (!active.length && !activeDeleg.length) {
      return '<div class="result-empty">Nothing running right now. Launch a mission to put agents to work.</div>';
    }
    active.forEach(function (t) {
      html += '<li><a class="rowlink-block" href="#/tasks/' + esc(t.id) + '">' +
        '<div class="r-main">' + pill(t.status) +
        '<span class="r-title">' + esc(t.goal.length > 80 ? t.goal.slice(0, 80) + "…" : t.goal) + "</span></div>" +
        '<div class="r-sub" data-step="' + esc(t.id) + '">loading current step…</div></a></li>';
    });
    activeDeleg.forEach(function (m) {
      var done = (m.subtask_counts && m.subtask_counts.completed) || 0;
      html += '<li><a class="rowlink-block" href="#/agents/' + esc(m.mission_id) + '">' +
        '<div class="r-main">' + pill(m.status) +
        '<span class="r-title">🤖 ' + esc(m.goal.length > 80 ? m.goal.slice(0, 80) + "…" : m.goal) + "</span></div>" +
        '<div class="r-sub">multi-agent · ' + done + "/" + m.subtasks_total + " subtasks · " + esc(ago(m.updated_at)) + "</div></a></li>";
    });
    return '<ul class="rows">' + html + "</ul>";
  }

  function attentionHtml(pending, failed) {
    var html = "";
    if (!pending.length && !failed.length) {
      return '<div class="result-empty">All clear — no approvals waiting, no failed tasks.</div>';
    }
    pending.slice(0, 4).forEach(function (a) {
      html += '<li><div class="r-main">' + pill("awaiting_approval") +
        '<span class="r-title">' + esc((a.summary || a.tool || "approval").slice(0, 70)) + "</span></div>" +
        '<div class="r-sub">task <span class="mono">' + esc(String(a.task_id || "").slice(0, 8)) + "</span></div></li>";
    });
    failed.forEach(function (t) {
      html += '<li><a class="rowlink-block" href="#/tasks/' + esc(t.id) + '">' +
        '<div class="r-main">' + pill("failed") +
        '<span class="r-title">' + esc(t.goal.length > 70 ? t.goal.slice(0, 70) + "…" : t.goal) + "</span></div>" +
        '<div class="r-sub">' + esc((t.error || "failed").slice(0, 90)) + "</div></a></li>";
    });
    return '<ul class="rows">' + html + "</ul>";
  }

  function recentRow(t) {
    return '<li><a class="rowlink-block" href="#/tasks/' + esc(t.id) + '">' +
      '<div class="r-main">' + pill(t.status) +
      '<span class="r-title">' + esc(t.goal.length > 70 ? t.goal.slice(0, 70) + "…" : t.goal) + "</span></div>" +
      '<div class="r-sub" data-verify="' + esc(t.id) + '">' + esc(ago(t.updated_at)) + "</div></a></li>";
  }

  /* Second-phase enhancement: current steps for active tasks, verification
     flags for recent ones. Bounded, real data only. */
  function enhanceOverview(activeTasks, recentTasks) {
    activeTasks.forEach(function (t) {
      api("/api/tasks/" + t.id).then(function (d) {
        var el = document.querySelector('[data-step="' + t.id + '"]');
        if (!el) return;
        var steps = d.steps || [];
        var cur = null;
        for (var i = steps.length - 1; i >= 0; i--) {
          if (steps[i].status === "running" || steps[i].status === "pending") { cur = steps[i]; break; }
        }
        if (!cur && steps.length) cur = steps[steps.length - 1];
        el.textContent = cur
          ? "current step: " + cur.name + " (" + cur.status.replace(/_/g, " ") + ")"
          : "queued — waiting to start";
      }).catch(function () {
        var el = document.querySelector('[data-step="' + t.id + '"]');
        if (el) el.textContent = "step info unavailable";
      });
    });
    var verified = 0, checked = 0;
    if (!recentTasks.length) { setVerifyStat(0, 0); return; }
    recentTasks.forEach(function (t) {
      api("/api/tasks/" + t.id).then(function (d) {
        checked++;
        var v = d.result && d.result.verified;
        if (v) verified++;
        var el = document.querySelector('[data-verify="' + t.id + '"]');
        if (el) el.innerHTML = esc(ago(t.updated_at)) + " · " +
          (v ? '<span class="pill done">verified</span>' : '<span class="pill queued">unverified</span>');
        if (checked === recentTasks.length) setVerifyStat(verified, checked);
      }).catch(function () {
        checked++;
        if (checked === recentTasks.length) setVerifyStat(verified, checked);
      });
    });
  }

  function setVerifyStat(verified, checked) {
    var cards = document.querySelectorAll("#view .grid.cols-4 .card");
    var last = cards[cards.length - 1];
    if (!last) return;
    last.querySelector(".stat").textContent = checked ? verified + " / " + checked : "—";
    last.querySelector(".stat-sub").textContent = checked
      ? "of recent tasks passed verification" : "no completed tasks yet";
  }

  function statCard(label, value, sub) {
    return '<div class="card"><h3>' + esc(label) + '</h3><div class="stat">' + esc(value) +
      '</div><div class="stat-sub">' + esc(sub) + "</div></div>";
  }

  function providerSummary(providers) {
    if (providers && providers.default_id !== undefined) {
      var dflt = providers.default_name
        ? '<strong>' + esc(providers.default_name) + '</strong>' +
          "<div class='stat-sub'>" + esc(providers.default_model || "no model") + " · default</div>"
        : "<div class='stat-sub'>no provider configured</div>";
      return '<div style="padding:8px 0">' + dflt +
        "<div class='stat-sub'>" + providers.count + " configured" +
        (providers.fallback_ids && providers.fallback_ids.length
          ? " · " + providers.fallback_ids.length + " fallback(s)" : "") + "</div></div>";
    }
    return Object.keys(providers).map(function (key) {
      var p = providers[key];
      var ok = (p.configured !== undefined) ? p.configured : !!p.enabled;
      var label = p.name || key;
      var sub = p.model || "—";
      if (p.is_default) sub += " · default";
      var st = ok ? '<span class="pill done">configured</span>' : '<span class="pill queued">not configured</span>';
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
        "<div><strong>" + esc(label) + "</strong><div class='stat-sub'>" + esc(sub) + "</div></div>" + st + "</div>";
    }).join("");
  }

  function taskTable(tasks, compact) {
    if (!tasks.length) return '<div class="empty"><h3>No tasks yet</h3><p>Create one to get started.</p></div>';
    var rows = tasks.map(function (t) {
      return "<tr class='rowlink' data-tid='" + esc(t.id) + "' tabindex='0'>" +
        "<td class='mono' data-th='ID'>" + esc(t.id.slice(0, 8)) + "</td>" +
        "<td data-th='Goal'>" + esc(t.goal.length > 60 ? t.goal.slice(0, 60) + "…" : t.goal) + "</td>" +
        (compact ? "" : "<td class='mono' data-th='Type'>" + esc(t.task_type) + "</td>") +
        "<td data-th='Status'>" + pill(t.status) + "</td>" +
        "<td class='stat-sub' data-th='Updated'>" + esc(ago(t.updated_at)) + "</td></tr>";
    }).join("");
    return "<table class='rtable'><thead><tr><th>ID</th><th>Goal</th>" + (compact ? "" : "<th>Type</th>") +
      "<th>Status</th><th>Updated</th></tr></thead><tbody>" + rows + "</tbody></table>";
  }

  function bindTaskRows(scope) {
    scope.querySelectorAll("[data-tid]").forEach(function (row) {
      function go() { location.hash = "#/tasks/" + row.getAttribute("data-tid"); }
      row.addEventListener("click", go);
      row.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
    });
  }

  /* ---------- tasks ---------- */
  var taskFilter = { status: "", q: "" };
  function renderTasks() {
    loading(view);
    var qs = "/api/tasks?limit=100" +
      (taskFilter.status ? "&status=" + encodeURIComponent(taskFilter.status) : "") +
      (taskFilter.q ? "&q=" + encodeURIComponent(taskFilter.q) : "");
    api(qs).then(function (d) {
      view.innerHTML =
        '<div class="page-head"><div><h1>Tasks</h1><p>' + d.tasks.length + " shown. Click a row to inspect.</p></div>" +
        '<button class="btn primary" id="new-task-btn">New task</button></div>' +
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
      document.getElementById("new-task-btn").addEventListener("click", newTaskModal);
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

  function newTaskModal() {
    modal(
      "<h2>New mission</h2>" +
      '<p class="stat-sub">Deterministic missions run offline with explicit verification. ' +
      "Model-driven missions use the configured provider. For team work, " +
      '<a href="#/agents">launch a multi-agent mission</a>.</p>' +
      '<label class="field"><span>Goal</span>' +
      '<textarea id="nt-goal" rows="3" placeholder="What should the agent accomplish?"></textarea>' +
      '<span class="field-err hidden" id="nt-goal-err"></span></label>' +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Execution mode</span><select id="nt-mode">' +
      '<option value="run">Deterministic (offline)</option><option value="run-agent">Model-driven</option>' +
      "</select></label>" +
      '<label class="field"><span>Mission type</span><select id="nt-type">' +
      Object.keys(TASK_TYPES).map(function (k) {
        return '<option value="' + k + '">' + TASK_TYPES[k].label + "</option>";
      }).join("") + "</select>" +
      '<span class="stat-sub" id="nt-type-desc"></span></label></div>' +
      '<details class="adv"><summary>Advanced parameters</summary>' +
      '<label class="field"><span>Parameters (JSON) — required: <span class="mono" id="nt-req"></span></span>' +
      '<textarea id="nt-params" rows="4" class="mono" spellcheck="false"></textarea>' +
      '<span class="field-err hidden" id="nt-params-err"></span></label></details>' +
      '<div id="nt-error" class="error hidden"></div>' +
      '<div class="actions"><button class="btn ghost" id="nt-cancel">Cancel</button>' +
      '<button class="btn primary" id="nt-create">Create &amp; run</button></div>'
    );
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
      if (inFlight) return; // duplicate-submission prevention
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

  /* ---------- task detail ---------- */
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

  function renderTaskDetail(taskId) {
    loading(view);
    api("/api/tasks/" + taskId).then(function (t) {
      var diag = classifyError(t.error);
      var failedStep = null;
      (t.steps || []).forEach(function (s) { if (s.status === "failed") failedStep = s; });
      var resumes = (t.events || []).filter(function (ev) {
        return ev.kind === "status" && ev.data && ev.data.status === "queued";
      }).length - 1; // first queue is the initial creation

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

      view.innerHTML =
        '<div class="page-head"><div><a href="#/tasks">← Tasks</a>' +
        "<h1 class='mono' style='font-size:20px;word-break:break-all'>" + esc(taskId) + "</h1>" +
        "<p>" + esc(t.goal) + "</p></div>" +
        '<div class="toolbar" id="task-actions"></div></div>' +
        '<div class="grid cols-4" style="margin-bottom:16px">' +
        statCard("Status", t.status.replace(/_/g, " "), t.task_type) +
        statCard("Created", fmtTime(t.created_at).split(",")[0], ago(t.created_at)) +
        statCard("Updated", fmtTime(t.updated_at).split(",")[0], ago(t.updated_at)) +
        '<div class="card"><h3>Verified</h3><div class="stat">' +
        (t.result && t.result.verified ? '<span class="pill done">yes</span>' : '<span class="pill queued">no</span>') +
        '</div><div class="stat-sub">model claims ≠ verified completion</div></div></div>' +
        diagHtml +
        (resumes > 0 ? '<p class="stat-sub" style="margin-bottom:14px">Resumed ' + resumes +
          " time" + (resumes > 1 ? "s" : "") + " from checkpoints.</p>" : "") +
        '<div class="grid cols-2"><div class="card"><h3>Execution timeline</h3><ul class="timeline" id="timeline"></ul></div>' +
        '<div class="card"><h3>Result &amp; evidence</h3><div id="result-pane">' + resultHtml + "</div></div></div>";
      renderActions(t);
      renderTimeline(t);
      var da = document.getElementById("diag-actions");
      if (da && (t.status === "failed" || t.status === "cancelled")) {
        da.innerHTML = '<button class="btn primary small" id="diag-resume">Resume from checkpoint</button>';
        document.getElementById("diag-resume").addEventListener("click", function () {
          api("/api/tasks/" + t.id + "/resume", { method: "POST" })
            .then(function () { toast("Task resumed", "ok"); route(); })
            .catch(function (e) { toast(e.message, "bad"); });
        });
      }
      if (t.status === "running" || t.status === "awaiting_approval" || t.status === "queued") {
        watchTask(taskId, function () { api("/api/tasks/" + taskId).then(function (nt) {
          renderActions(nt); renderTimeline(nt);
          var rp = document.getElementById("result-pane");
          if (rp) {
            rp.innerHTML = nt.result
              ? '<pre class="output">' + esc(JSON.stringify(nt.result, null, 2)) + "</pre>"
              : '<div class="result-empty">No result yet — the task is still working.</div>';
          }
          if (nt.status === "completed" || nt.status === "failed" || nt.status === "cancelled") {
            toast("Task " + nt.status, nt.status === "completed" ? "ok" : "bad");
          }
        }).catch(function () {}); });
      }
    }).catch(function (e) { showError(view, e); });
  }

  function renderActions(t) {
    var el = document.getElementById("task-actions");
    if (!el) return;
    var html = "";
    if (t.status === "running" || t.status === "awaiting_approval" || t.status === "queued") {
      html += '<button class="btn danger" id="a-cancel">Cancel</button>';
    }
    if (t.status === "failed" || t.status === "cancelled" || t.status === "awaiting_approval") {
      html += '<button class="btn" id="a-resume">Resume</button>';
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

  function renderTimeline(t) {
    var el = document.getElementById("timeline");
    if (!el) return;
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
    el.innerHTML = items.length ? items.join("") :
      '<li><div class="t-head"><span class="stat-sub">No steps recorded yet.</span></div></li>';
  }

  /* ---------- approvals ---------- */
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

  /* ---------- scheduler ---------- */
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
      "<td class='stat-sub' data-th='Next run'>" + esc(j.enabled ? ago(j.next_run_at) : "—") + "</td>" +
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

  /* ---------- providers ---------- */
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
    function pill(on, label, title) {
      return '<span class="pill ' + (on ? "done" : "queued") + '" title="' + esc(title) + '">' + label + "</span>";
    }
    return pill(caps.tools, "tools", "Native tool/function calling") +
      pill(caps.streaming, "stream", "Streaming responses") +
      pill(caps.vision, "vision", "Image input") +
      pill(caps.json_mode, "json", "JSON mode");
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
      // Unsaved: build a transient probe from the form fields.
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

  /* ---------- settings ---------- */
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
        '<div class="toolbar"><button class="btn small" id="theme-btn">Toggle theme</button>' +
        '<button class="btn small ghost" id="lock-btn2">Lock dashboard</button></div></div>' +
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
        '<div class="card" style="margin-top:16px"><h3>Permission policy — tools</h3>' +
        "<table class='rtable'><thead><tr><th>Tool</th><th>Risk</th><th>Approval</th><th>Description</th></tr></thead><tbody>" +
        policy.tools.map(function (t) {
          return "<tr><td class='mono' data-th='Tool'>" + esc(t.name) + "</td><td data-th='Risk'>" + esc(t.risk) + "</td><td data-th='Approval'>" +
            (t.requires_approval ? '<span class="pill awaiting_approval">required</span>' : '<span class="pill queued">no</span>') +
            "</td><td class='stat-sub' data-th='Description'>" + esc(t.description) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
      document.getElementById("theme-btn").addEventListener("click", function () {
        document.getElementById("theme-toggle").click();
      });
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
        // Adopt the new token for this session only (in memory); drop the
        // stale persisted token so a reload goes to the unlock screen.
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

  /* ---------- multi-agent delegations ---------- */
  var delegProfiles = null;
  var delegPollTimer = null;
  var delegDetailCache = null;

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
    var rows = missions.map(function (m) {
      var total = m.subtasks_total || 0;
      var done = (m.subtask_counts && m.subtask_counts.completed) || 0;
      var pct = total ? Math.round(100 * done / total) : 0;
      var goal = m.goal || "";
      return "<tr class='rowlink' data-mid='" + esc(m.mission_id) + "' tabindex='0'>" +
        "<td class='mono' data-th='ID'>" + esc(m.mission_id.slice(0, 10)) + "</td>" +
        "<td data-th='Goal'>" + esc(goal.length > 70 ? goal.slice(0, 70) + "…" : goal) + "</td>" +
        "<td data-th='Status'>" + pill(m.status) + "</td>" +
        "<td data-th='Progress'><div class='progress'><div class='bar' style='width:" + pct + "%'></div></div>" +
        "<span class='stat-sub'>" + done + "/" + total + "</span></td>" +
        "<td class='stat-sub' data-th='Updated'>" + esc(ago(m.updated_at)) + "</td></tr>";
    }).join("");
    view.innerHTML =
      '<div class="page-head"><div><h1>Multi-Agent</h1>' +
      "<p>Delegated missions: a lead plan executed by specialist worker agents.</p></div>" +
      '<div class="toolbar">' +
      '<button class="btn" id="deleg-settings-btn">Delegation settings</button>' +
      '<button class="btn primary" id="new-mission-btn"' + (enabled ? "" : " disabled") + ">New mission</button>" +
      "</div></div>" +
      (enabled ? "" :
        '<div class="error">Multi-agent delegation is currently disabled. Enable it in Delegation settings to launch missions.</div>') +
      '<div class="card" style="padding:6px 12px">' +
      (missions.length
        ? '<table class="rtable"><thead><tr><th>ID</th><th>Goal</th><th>Status</th><th>Progress</th><th>Updated</th></tr></thead><tbody>' +
          rows + "</tbody></table>"
        : '<div class="empty"><h3>No missions yet</h3><p>Launch a delegated mission to coordinate multiple worker agents.</p></div>') +
      "</div>";
    view.querySelectorAll("[data-mid]").forEach(function (tr) {
      function go() { location.hash = "#/agents/" + tr.getAttribute("data-mid"); }
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") go(); });
    });
    document.getElementById("deleg-settings-btn").addEventListener("click", delegSettingsModal);
    var nb = document.getElementById("new-mission-btn");
    if (nb && enabled) nb.addEventListener("click", function () { newMissionModal(null); });
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

  /* ----- new mission ----- */
  function newMissionModal(prefill) {
    prefill = prefill || {};
    delegProfilesCached().then(function (profiles) {
      var profileOpts = profiles.map(function (p) {
        return '<option value="' + esc(p.name) + '"' + (p.name === "researcher" ? " selected" : "") + ">" +
          esc(p.name) + " — " + esc(p.description.slice(0, 60)) + "</option>";
      }).join("");
      modal(
        "<h2>New delegated mission</h2>" +
        '<p class="stat-sub">The mission itself is the lead: it plans, delegates to specialist workers, ' +
        "and verifies the result. Workers run with the dashboard's default provider chain.</p>" +
        '<label class="field"><span>Mission goal</span>' +
        '<textarea id="nm-goal" rows="3" placeholder="What should the worker team accomplish?">' +
        esc(prefill.goal || "") + "</textarea></label>" +
        '<label class="field"><span>Planning mode</span><select id="nm-mode">' +
        '<option value="manual">Manual — I define the subtasks and their dependencies</option>' +
        '<option value="auto"' + (prefill.auto ? " selected" : "") + ">Automatic — a planner worker decomposes the goal first</option>" +
        "</select></label>" +
        '<div id="nm-auto-hint" class="hidden"><label class="field"><span>Context for the planner (optional)</span>' +
        '<textarea id="nm-hint" rows="2" placeholder="Constraints, scope, things to avoid…">' +
        esc(prefill.hint || "") + "</textarea></label>" +
        '<p class="stat-sub">A <span class="mono">planner</span> worker produces a subtask plan. ' +
        "After it completes, open the mission and choose “Launch plan as mission”.</p></div>" +
        '<div id="nm-manual"><h3 style="margin:4px 0 10px">Subtasks</h3><div id="nm-subs"></div>' +
        '<button class="btn small" id="nm-add-sub">+ Add subtask</button></div>' +
        "<h3 style='margin:18px 0 10px'>Budgets &amp; limits</h3>" +
        '<div class="grid cols-3">' +
        budgetField("nm-b-workers", "Max workers", 8, 1, 32) +
        budgetField("nm-b-conc", "Max concurrent", 2, 1, 8) +
        budgetField("nm-b-retries", "Retries / worker", 2, 0, 5) +
        budgetField("nm-b-calls", "Max model calls", 120, 1, 500) +
        budgetField("nm-b-runtime", "Max runtime (s)", 1800, 60, 7200) +
        budgetField("nm-b-depth", "Max depth", 2, 1, 3) +
        "</div>" +
        '<div id="nm-error" class="error hidden"></div>' +
        '<div class="actions"><button class="btn ghost" id="nm-cancel">Cancel</button>' +
        '<button class="btn primary" id="nm-create">Create &amp; launch</button></div>'
      );
      window.__nmProfiles = profileOpts;
      var subWrap = document.getElementById("nm-subs");
      function addSub(data) {
        var div = document.createElement("div");
        div.className = "subtask";
        div.innerHTML = subtaskEditor(window.__nmProfiles, data || {});
        subWrap.appendChild(div);
        bindSubtask(div);
        if (data) {
          if (data.profile) div.querySelector(".s-profile").value = data.profile;
          if (data.kind) {
            div.querySelector(".s-kind").value = data.kind;
            var isTool = data.kind === "tool";
            div.querySelector(".s-agent").classList.toggle("hidden", isTool);
            div.querySelector(".s-tool").classList.toggle("hidden", !isTool);
          }
        }
      }
      document.getElementById("nm-add-sub").addEventListener("click", function () { addSub(null); });
      (prefill.subtasks && prefill.subtasks.length ? prefill.subtasks : [null]).forEach(addSub);
      var modeSel = document.getElementById("nm-mode");
      function syncMode() {
        var auto = modeSel.value === "auto";
        document.getElementById("nm-manual").classList.toggle("hidden", auto);
        document.getElementById("nm-auto-hint").classList.toggle("hidden", !auto);
      }
      modeSel.addEventListener("change", syncMode);
      syncMode();
      document.getElementById("nm-cancel").addEventListener("click", closeModal);
      document.getElementById("nm-create").addEventListener("click", function () {
        var errEl = document.getElementById("nm-error");
        function fail(msg) { errEl.textContent = msg; errEl.classList.remove("hidden"); }
        var goal = document.getElementById("nm-goal").value.trim();
        if (!goal) return fail("Describe the mission goal first.");
        var auto = modeSel.value === "auto";
        var subtasks;
        if (auto) {
          var hint = document.getElementById("nm-hint").value.trim();
          subtasks = [{
            id: "plan", name: "Decompose goal into subtasks",
            kind: "agent", profile: "planner",
            goal: goal + (hint ? "\n\nContext: " + hint : "") +
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
        [["nm-b-workers", "max_workers"], ["nm-b-conc", "max_concurrent"],
         ["nm-b-retries", "max_retries_per_worker"], ["nm-b-calls", "max_model_calls"],
         ["nm-b-runtime", "max_runtime_s"], ["nm-b-depth", "max_depth"]].forEach(function (pair) {
          var v = parseInt(document.getElementById(pair[0]).value, 10);
          if (!isNaN(v)) budgets[pair[1]] = v;
        });
        api("/api/delegations", { method: "POST", body: { goal: goal, subtasks: subtasks, budgets: budgets } })
          .then(function (d) {
            closeModal();
            toast("Mission launched: " + d.mission_id.slice(0, 10), "ok");
            location.hash = "#/agents/" + d.mission_id;
          })
          .catch(function (e) { fail(e.message); });
      });
    }).catch(function (e) { toast(e.message, "bad"); });
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

  /* ----- mission detail ----- */
  function renderDelegationDetail(missionId) {
    stopDelegPoll();
    loading(view);
    api("/api/delegations/" + encodeURIComponent(missionId)).then(function (d) {
      delegDetailCache = d;
      renderDelegationBody(d);
      if (delegActive(d.status)) {
        delegPollTimer = setInterval(function () {
          api("/api/delegations/" + encodeURIComponent(missionId)).then(function (nd) {
            delegDetailCache = nd;
            updateDelegationLive(nd);
            if (!delegActive(nd.status)) stopDelegPoll();
          }).catch(function () {});
        }, 4000);
      }
    }).catch(function (e) { showError(view, e); });
  }

  function delegProgress(d) {
    var total = d.workers.length;
    var done = d.workers.filter(function (w) { return w.status === "completed"; }).length;
    return { total: total, done: done, pct: total ? Math.round(100 * done / total) : 0 };
  }

  function renderDelegationBody(d) {
    var p = delegProgress(d);
    var failedSubs = d.workers.filter(function (w) { return w.status === "failed" || w.status === "cancelled"; });
    var awaiting = d.workers.some(function (w) { return w.status === "awaiting_approval"; });
    var elapsed = Math.max(0, (d.status && delegActive(d.status) ? Date.now() / 1000 : d.updated_at) - d.created_at);
    view.innerHTML =
      '<div class="page-head"><div><a href="#/agents">← Multi-Agent</a>' +
      "<h1 class='mono' style='font-size:20px'>" + esc(d.mission_id) + "</h1>" +
      "<p>" + esc(d.goal) + "</p></div>" +
      '<div class="toolbar" id="deleg-actions"></div></div>' +
      (d.retry_of ? '<p class="stat-sub">Retry of <a href="#/agents/' + esc(d.retry_of) + '">' + esc(d.retry_of) + "</a></p>" : "") +
      '<div class="grid cols-4" style="margin-bottom:16px" id="deleg-stats">' + delegStatsHtml(d, p, elapsed) + "</div>" +
      (d.error ? '<div class="error">' + esc(d.error) + "</div>" : "") +
      (awaiting ? '<div class="error">A worker is waiting on human approval. Review it in <a href="#/approvals">Approvals</a>.</div>' : "") +
      '<div class="card" style="margin-bottom:16px"><h3>Workers &amp; dependencies</h3>' +
      '<div class="stat-sub" style="margin-bottom:10px">Plan order is execution order; a worker starts when its dependencies complete.</div>' +
      '<table class="rtable"><thead><tr><th>Subtask</th><th>Role</th><th>Status</th><th>Depends on</th><th>Attempts</th><th>Summary</th></tr></thead>' +
      "<tbody id='deleg-workers'></tbody></table></div>" +
      '<div class="grid cols-2">' +
      '<div class="card"><h3>Artifacts</h3><div id="deleg-artifacts"></div></div>' +
      '<div class="card"><h3>Verification</h3><div id="deleg-verification"></div></div>' +
      "</div>";
    renderDelegActions(d);
    updateDelegationLive(d);
  }

  function delegStatsHtml(d, p, elapsed) {
    return statCard("Status", d.status.replace(/_/g, " "), "updated " + ago(d.updated_at)) +
      statCard("Progress", p.done + " / " + p.total, p.pct + "% of subtasks completed") +
      statCard("Resources", String(d.usage.model_calls) + " model calls",
        d.usage.workers_launched + " workers · " + Math.round(elapsed) + "s elapsed") +
      statCard("Plan", String(d.plan.length) + " subtasks",
        "budgets: ≤" + (d.budgets.max_workers || "—") + " workers, ≤" +
        (d.budgets.max_concurrent || "—") + " concurrent");
  }

  function updateDelegationLive(d) {
    var p = delegProgress(d);
    var elapsed = Math.max(0, (delegActive(d.status) ? Date.now() / 1000 : d.updated_at) - d.created_at);
    var stats = document.getElementById("deleg-stats");
    if (stats) stats.innerHTML = delegStatsHtml(d, p, elapsed);
    var tb = document.getElementById("deleg-workers");
    if (tb) {
      tb.innerHTML = d.workers.map(function (w) {
        var deps = (w.depends_on || []).map(function (x) {
          return '<span class="dep">' + esc(x) + "</span>";
        }).join(" ") || "<span class='stat-sub'>—</span>";
        var sum = w.summary || w.error || "";
        return "<tr class='rowlink' data-wid='" + esc(w.subtask_id) + "' tabindex='0'>" +
          "<td data-th='Subtask'><strong>" + esc(w.name) + "</strong><br><span class='mono stat-sub'>" + esc(w.subtask_id) + "</span></td>" +
          "<td class='mono' data-th='Role'>" + esc(w.kind === "tool" ? "tool:" + w.tool : w.profile) + "</td>" +
          "<td data-th='Status'>" + pill(w.status) + "</td>" +
          "<td data-th='Depends on'>" + deps + "</td>" +
          "<td class='mono' data-th='Attempts'>" + esc(String(w.attempts)) + "</td>" +
          "<td class='stat-sub' data-th='Summary'>" + esc(sum.length > 90 ? sum.slice(0, 90) + "…" : sum) + "</td></tr>";
      }).join("");
      tb.querySelectorAll("[data-wid]").forEach(function (tr) {
        function open() { workerModal(tr.getAttribute("data-wid")); }
        tr.addEventListener("click", open);
        tr.addEventListener("keydown", function (e) { if (e.key === "Enter") open(); });
      });
    }
    var arts = document.getElementById("deleg-artifacts");
    if (arts) {
      arts.innerHTML = (d.artifacts && d.artifacts.length)
        ? "<ul class='artifacts'>" + d.artifacts.map(function (a) {
            return "<li><span class='mono'>" + esc(a.name) + "</span> " +
              "<span class='stat-sub'>v" + esc(String(a.version)) + " · " + esc((a.worker_id || "").slice(0, 10)) + "</span></li>";
          }).join("") + "</ul>"
        : "<p class='stat-sub'>No artifacts promoted yet.</p>";
    }
    var ver = document.getElementById("deleg-verification");
    if (ver) {
      ver.innerHTML = d.verification
        ? "<pre class='output'>" + esc(JSON.stringify(d.verification, null, 2).slice(0, 3000)) + "</pre>"
        : "<p class='stat-sub'>No verification report yet. Verification runs when the mission finishes.</p>";
    }
    renderDelegActions(d);
  }

  function renderDelegActions(d) {
    var el = document.getElementById("deleg-actions");
    if (!el) return;
    var html = "";
    var failed = d.workers.some(function (w) { return w.status === "failed"; });
    if (delegActive(d.status)) {
      html += '<button class="btn danger" id="deleg-cancel">Cancel mission</button>';
    }
    if ((d.status === "completed" || d.status === "failed") && failed) {
      html += '<button class="btn" id="deleg-retry">Retry failed subtasks</button>';
    }
    var planner = d.workers.filter(function (w) {
      return w.profile === "planner" && w.status === "completed";
    });
    if (planner.length) {
      html += '<button class="btn" id="deleg-launch-plan">Launch plan as mission</button>';
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
  }

  function workerModal(subtaskId) {
    var d = delegDetailCache;
    if (!d) return;
    var w = d.workers.filter(function (x) { return x.subtask_id === subtaskId; })[0];
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
    newMissionModal({ goal: d.goal, subtasks: mapped });
  }

  function WORKER_PROFILE_FALLBACK(name) {
    var known = (delegProfiles || []).map(function (p) { return p.name; });
    return known.indexOf(name) !== -1 ? name : "researcher";
  }

  /* ---------- boot ---------- */
  if (token) route(); else lock();
})();
