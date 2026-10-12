/* NextClaw dashboard — vanilla JS SPA, no build step. */
(function () {
  "use strict";

  var API = "";
  var token = localStorage.getItem("nc_token") || "";
  var theme = localStorage.getItem("nc_theme") || "dark";
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

  /* ---------- API client ---------- */
  function api(path, opts) {
    opts = opts || {};
    var headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(API + path, {
      method: opts.method || "GET",
      headers: headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      if (r.status === 401) { lock(); throw new Error("unauthorized"); }
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
        return j;
      });
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
    localStorage.removeItem("nc_token");
    document.getElementById("app").classList.add("hidden");
    document.getElementById("unlock").classList.remove("hidden");
  }
  function unlock(t) {
    token = t;
    localStorage.setItem("nc_token", t);
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
    localStorage.setItem("nc_theme", theme);
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
    var m = hash.match(/^#\/tasks\/([a-z0-9]+)$/);
    if (m) return renderTaskDetail(m[1]);
    var pages = {
      "#/overview": renderOverview,
      "#/tasks": renderTasks,
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
      if (d.approvals.length) { b.textContent = d.approvals.length; b.classList.remove("hidden"); }
      else b.classList.add("hidden");
    }).catch(function () {});
  }
  setInterval(refreshApprovalBadge, 15000);

  /* ---------- SSE ---------- */
  var sse = null;
  function stopSse() { if (sse) { sse.close(); sse = null; } }
  function watchTask(taskId, onEvent) {
    stopSse();
    try {
      // EventSource cannot send Authorization headers; pass the token as a
      // query param for this endpoint only. The token is already in the URL
      // bar's origin (localhost) and never logged server-side.
      sse = new EventSource(API + "/api/tasks/" + taskId + "/events?token=" + encodeURIComponent(token));
    } catch (e) { return; }
    sse.onmessage = function (ev) { try { onEvent(JSON.parse(ev.data)); } catch (e) {} };
    sse.addEventListener("done", function () { stopSse(); onEvent({ kind: "__done" }); });
    sse.onerror = function () { stopSse(); };
  }

  /* ================= PAGES ================= */

  function renderOverview() {
    loading(view);
    Promise.all([
      api("/api/status"), api("/api/tasks?limit=8"), api("/api/quota"),
      api("/api/scheduler/jobs"), api("/api/approvals?status=pending"),
    ]).then(function (r) {
      var status = r[0], tasks = r[1].tasks, quota = r[2], jobs = r[3].jobs, pending = r[4].approvals;
      var counts = {};
      tasks.forEach(function (t) { counts[t.status] = (counts[t.status] || 0) + 1; });
      var nextJob = jobs.filter(function (j) { return j.enabled; })
        .sort(function (a, b) { return (a.next_run_at || 9e15) - (b.next_run_at || 9e15); })[0];
      view.innerHTML =
        '<div class="page-head"><div><h1>Overview</h1><p>What your agents are doing right now.</p></div>' +
        '<button class="btn primary" id="new-task-btn">New task</button></div>' +
        '<div class="grid cols-4">' +
        statCard("Tasks (recent)", tasks.length, Object.keys(counts).map(function (k) { return k + ": " + counts[k]; }).join(" · ") || "none yet") +
        statCard("Pending approvals", pending.length, pending.length ? "action needed" : "all clear") +
        statCard("API quota today", quota.daily_used + " / " + quota.daily_cap,
          quota.cooldown_active ? "cooldown: " + quota.cooldown_left_s + "s left" : "healthy") +
        statCard("Next scheduled run", nextJob ? ago(nextJob.next_run_at) : "none",
          nextJob ? esc(nextJob.name) : "scheduler idle") +
        "</div>" +
        '<div class="grid cols-2" style="margin-top:16px">' +
        '<div class="card"><h3>Recent tasks</h3>' + taskTable(tasks.slice(0, 6), true) + "</div>" +
        '<div class="card"><h3>Providers</h3>' + providerSummary(status.providers) +
        '<div class="toolbar" style="margin-top:12px"><a class="btn small" href="#/providers">Configure</a></div></div>' +
        "</div>";
      document.getElementById("new-task-btn").addEventListener("click", newTaskModal);
    }).catch(function (e) { showError(view, e); });
  }

  function statCard(label, value, sub) {
    return '<div class="card"><h3>' + esc(label) + '</h3><div class="stat">' + esc(value) +
      '</div><div class="stat-sub">' + esc(sub) + "</div></div>";
  }

  function providerSummary(providers) {
    return Object.keys(providers).map(function (name) {
      var p = providers[name];
      var st = p.configured ? '<span class="pill done">configured</span>' : '<span class="pill queued">not configured</span>';
      return '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">' +
        "<div><strong>" + esc(name) + "</strong><div class='stat-sub'>" + esc(p.model || "—") + "</div></div>" + st + "</div>";
    }).join("");
  }

  function taskTable(tasks, compact) {
    if (!tasks.length) return '<div class="empty"><h3>No tasks yet</h3><p>Create one to get started.</p></div>';
    var rows = tasks.map(function (t) {
      return "<tr class='rowlink' data-tid='" + esc(t.id) + "' tabindex='0'>" +
        "<td class='mono'>" + esc(t.id.slice(0, 8)) + "</td>" +
        "<td>" + esc(t.goal.length > 60 ? t.goal.slice(0, 60) + "…" : t.goal) + "</td>" +
        (compact ? "" : "<td class='mono'>" + esc(t.task_type) + "</td>") +
        "<td>" + pill(t.status) + "</td>" +
        "<td class='stat-sub'>" + esc(ago(t.updated_at)) + "</td></tr>";
    }).join("");
    return "<table><thead><tr><th>ID</th><th>Goal</th>" + (compact ? "" : "<th>Type</th>") +
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

  function newTaskModal() {
    modal(
      "<h2>New task</h2>" +
      '<p class="stat-sub">Deterministic tasks run offline with explicit verification. ' +
      "Agent tasks use the configured model provider.</p>" +
      '<label class="field"><span>Goal</span>' +
      '<textarea id="nt-goal" rows="3" placeholder="What should the agent accomplish?"></textarea></label>' +
      '<div class="grid cols-2">' +
      '<label class="field"><span>Mode</span><select id="nt-mode">' +
      '<option value="run">Deterministic (offline)</option><option value="run-agent">Model-driven</option>' +
      "</select></label>" +
      '<label class="field"><span>Task type</span><select id="nt-type">' +
      '<option value="file_report">file_report — verified file inventory</option>' +
      '<option value="file_write">file_write — verified file creation</option>' +
      '<option value="workspace_inspect">workspace_inspect — verified listing</option>' +
      "</select></label></div>" +
      '<label class="field"><span>Parameters (JSON)</span>' +
      '<textarea id="nt-params" rows="3" class="mono" placeholder=\'{"root": ".", "pattern": "**/*.py"}\'></textarea></label>' +
      '<div id="nt-error" class="error hidden"></div>' +
      '<div class="actions"><button class="btn ghost" id="nt-cancel">Cancel</button>' +
      '<button class="btn primary" id="nt-create">Create &amp; run</button></div>'
    );
    document.getElementById("nt-mode").addEventListener("change", function (e) {
      document.getElementById("nt-type").disabled = e.target.value === "run-agent";
    });
    document.getElementById("nt-cancel").addEventListener("click", closeModal);
    document.getElementById("nt-create").addEventListener("click", function () {
      var errEl = document.getElementById("nt-error");
      var params = {};
      var raw = document.getElementById("nt-params").value.trim();
      if (raw) {
        try { params = JSON.parse(raw); }
        catch (e) { errEl.textContent = "Parameters are not valid JSON."; errEl.classList.remove("hidden"); return; }
      }
      var body = {
        goal: document.getElementById("nt-goal").value.trim(),
        mode: document.getElementById("nt-mode").value,
        task_type: document.getElementById("nt-type").value,
        params: params,
      };
      if (!body.goal) { errEl.textContent = "Describe the goal first."; errEl.classList.remove("hidden"); return; }
      api("/api/tasks", { method: "POST", body: body }).then(function (d) {
        closeModal();
        toast("Task created: " + d.task_id.slice(0, 8), "ok");
        location.hash = "#/tasks/" + d.task_id;
      }).catch(function (e) {
        errEl.textContent = e.message; errEl.classList.remove("hidden");
      });
    });
  }

  /* ---------- task detail ---------- */
  function renderTaskDetail(taskId) {
    loading(view);
    api("/api/tasks/" + taskId).then(function (t) {
      view.innerHTML =
        '<div class="page-head"><div><a href="#/tasks">← Tasks</a>' +
        "<h1 class='mono' style='font-size:20px'>" + esc(taskId) + "</h1>" +
        "<p>" + esc(t.goal) + "</p></div>" +
        '<div class="toolbar" id="task-actions"></div></div>' +
        '<div class="grid cols-4" style="margin-bottom:16px">' +
        statCard("Status", t.status.replace(/_/g, " "), t.task_type) +
        statCard("Created", fmtTime(t.created_at).split(",")[0], ago(t.created_at)) +
        statCard("Updated", fmtTime(t.updated_at).split(",")[0], ago(t.updated_at)) +
        '<div class="card"><h3>Verified</h3><div class="stat">' +
        (t.result && t.result.verified ? '<span class="pill done">yes</span>' : '<span class="pill queued">no</span>') +
        '</div><div class="stat-sub">model claims ≠ verified completion</div></div></div>' +
        (t.error ? '<div class="error">' + esc(t.error) + "</div>" : "") +
        '<div class="grid cols-2"><div class="card"><h3>Execution timeline</h3><ul class="timeline" id="timeline"></ul></div>' +
        '<div class="card"><h3>Result &amp; evidence</h3><pre class="output" id="result-pre">' +
        esc(JSON.stringify(t.result, null, 2) || "no result yet") + "</pre></div></div>";
      renderActions(t);
      renderTimeline(t);
      if (t.status === "running" || t.status === "awaiting_approval" || t.status === "queued") {
        watchTask(taskId, function () { api("/api/tasks/" + taskId).then(function (nt) {
          renderActions(nt); renderTimeline(nt);
          document.getElementById("result-pre").textContent = JSON.stringify(nt.result, null, 2) || "no result yet";
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
    return "<table><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Task</th></tr></thead><tbody>" +
      events.map(function (e) {
        return "<tr><td class='stat-sub'>" + esc(fmtTime(e.ts)) + "</td><td>" + esc(e.actor) +
          "</td><td class='mono'>" + esc(e.action) + "</td><td class='mono'>" +
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
        '<div class="card" style="padding:6px 12px;margin-bottom:16px"><table><thead><tr>' +
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
    return "<tr><td><strong>" + esc(j.name || j.id) + "</strong><div class='stat-sub mono'>" + esc(j.id) + "</div></td>" +
      "<td>" + esc(fmtInterval(j.interval_s)) + "</td>" +
      "<td>" + (j.enabled ? '<span class="pill done">enabled</span>' : '<span class="pill queued">disabled</span>') + "</td>" +
      "<td class='stat-sub'>" + esc(j.enabled ? ago(j.next_run_at) : "—") + "</td>" +
      "<td>" + (last ? pill(last.status) + " <span class='stat-sub'>" + esc(ago(last.started_at)) + "</span>" : "—") + "</td>" +
      '<td><div class="toolbar">' +
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
    return "<table><thead><tr><th>Run</th><th>Job</th><th>Trigger</th><th>Status</th><th>Started</th></tr></thead><tbody>" +
      runs.map(function (r) {
        return "<tr><td class='mono'>#" + esc(r.id) + "</td><td class='mono'>" + esc(r.job_id) +
          "</td><td>" + esc(r.trigger || "") + "</td><td>" + pill(r.status) + "</td><td class='stat-sub'>" +
          esc(ago(r.started_at)) + "</td></tr>";
      }).join("") + "</tbody></table>";
  }

  function schedAction(jobId, action) {
    api("/api/scheduler/jobs/" + jobId + "/" + action, { method: "POST" })
      .then(function () { toast("Job " + action.replace("-", " ") + "d", "ok"); renderScheduler(); })
      .catch(function (e) { toast(e.message, "bad"); });
  }

  /* ---------- providers ---------- */
  function renderProviders() {
    loading(view);
    Promise.all([api("/api/providers"), api("/api/quota")]).then(function (r) {
      var p = r[0].providers, quota = r[1];
      view.innerHTML =
        '<div class="page-head"><div><h1>Providers</h1>' +
        "<p>Keys live in environment variables and are never shown here.</p></div></div>" +
        '<div class="grid cols-2">' +
        providerCard("gemini", p.gemini, "Free-tier friendly. Function calling verified.") +
        providerCard("ollama", p.ollama, "Local models. No quota, needs local RAM.") +
        "</div>" +
        '<div class="card" style="margin-top:16px"><h3>Usage &amp; quota</h3>' +
        '<div class="kv"><dt>Requests today</dt><dd>' + quota.daily_used + " / " + quota.daily_cap + "</dd>" +
        "<dt>Cooldown</dt><dd>" + (quota.cooldown_active ? "active — " + quota.cooldown_left_s + "s left" : "none") + "</dd>" +
        "<dt>Active provider</dt><dd class='mono'>" + esc(r[0].active) + "</dd></div></div>";
    }).catch(function (e) { showError(view, e); });
  }

  function providerCard(name, p, blurb) {
    var idp = name.replace(/[^a-z]/g, "");
    return '<div class="card"><h3>' + esc(name) + '</h3><p class="stat-sub">' + esc(blurb) + "</p>" +
      '<div class="kv"><dt>Status</dt><dd>' + (p.configured ? '<span class="pill done">configured</span>' : '<span class="pill queued">not configured</span>') + "</dd>" +
      "<dt>Model</dt><dd class='mono'>" + esc(p.model || "—") + "</dd>" +
      (p.url ? "<dt>Endpoint</dt><dd class='mono'>" + esc(p.url) + "</dd>" : "") + "</div>" +
      '<div class="toolbar"><button class="btn small" id="pt-' + idp + '">Test connectivity</button></div>' +
      '<div class="stat-sub" id="pr-' + idp + '" style="margin-top:8px"></div></div>';
  }

  function renderProvidersBound() {
    ["gemini", "ollama"].forEach(function (name) {
      var idp = name.replace(/[^a-z]/g, "");
      var btn = document.getElementById("pt-" + idp);
      if (btn) btn.addEventListener("click", function () {
        var res = document.getElementById("pr-" + idp);
        res.textContent = "Testing…";
        api("/api/providers/test", { method: "POST", body: { provider: name } }).then(function (d) {
          res.innerHTML = d.ok
            ? '<span class="pill done">reachable</span> <span class="stat-sub">' + esc((d.models || d.models_sample || []).join(", ")) + "</span>"
            : '<span class="pill failed">failed</span> <span class="stat-sub">' + esc(d.error || "") + "</span>";
        }).catch(function (e) { res.textContent = e.message; });
      });
    });
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
        '<div class="card" style="margin-top:16px"><h3>Permission policy — tools</h3>' +
        "<table><thead><tr><th>Tool</th><th>Risk</th><th>Approval</th><th>Description</th></tr></thead><tbody>" +
        policy.tools.map(function (t) {
          return "<tr><td class='mono'>" + esc(t.name) + "</td><td>" + esc(t.risk) + "</td><td>" +
            (t.requires_approval ? '<span class="pill awaiting_approval">required</span>' : '<span class="pill queued">no</span>') +
            "</td><td class='stat-sub'>" + esc(t.description) + "</td></tr>";
        }).join("") + "</tbody></table></div>";
      document.getElementById("theme-btn").addEventListener("click", function () {
        document.getElementById("theme-toggle").click();
      });
      document.getElementById("lock-btn2").addEventListener("click", lock);
    }).catch(function (e) { showError(view, e); });
  }

  /* ---------- boot ---------- */
  if (token) route(); else lock();
  // Re-bind provider test buttons after providers page renders.
  var _observer = new MutationObserver(function () { renderProvidersBound(); });
  _observer.observe(document.getElementById("view"), { childList: true });
})();
