(function () {
  const auth = VanmoerAuth.requireAuth("admin");
  if (!auth) return;

  const CHART_COLOR = "#4f7cff"; // matches --accent; kept literal (not var()) since it's injected via innerHTML

  // ═══════════════════════════════════════════════════════════════════
  // Tabs
  // ═══════════════════════════════════════════════════════════════════
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
    });
  });

  document.getElementById("logout-btn").addEventListener("click", () => VanmoerAuth.logout());
  document.getElementById("backup-btn").addEventListener("click", async () => {
    await VanmoerAuth.authFetch("/api/admin/backup", { method: "POST" });
    alert("Backup complete.");
  });

  function showMsg(el, text, ok) {
    el.textContent = text;
    el.className = "msg " + (ok ? "ok" : "err");
    el.style.display = "block";
  }

  function fmtTimestamp(iso) {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  }

  // ═══════════════════════════════════════════════════════════════════
  // Dashboard tab: stat tiles
  // ═══════════════════════════════════════════════════════════════════
  async function loadStats() {
    const res = await VanmoerAuth.authFetch("/api/admin/stats");
    const s = await res.json();
    document.getElementById("stat-total").textContent = s.total_files.toLocaleString();
    document.getElementById("stat-rate").textContent = `${s.success_rate}%`;
    document.getElementById("stat-today").textContent = s.files_today.toLocaleString();
    document.getElementById("stat-week").textContent = s.files_this_week.toLocaleString();
    renderChart(s.series);
  }

  // ── Single-series bar chart: files (distinct references) per day ─────
  // Deliberately not stacked by success/failed — "files" (reference_count)
  // and "run outcome" are different concepts (see dashboard_stats() in
  // admin/service.py); mixing them in one stack would make the bar heights
  // not reconcile with the "Total files" stat tile above it. Run reliability
  // has its own stat tile (Success rate) instead.
  function renderChart(series) {
    drawChart(series);
  }

  function drawChart(days) {
    const svg = document.getElementById("chart-svg");
    const tooltip = document.getElementById("chart-tooltip");
    const W = 1080, H = 220, padL = 30, padB = 26, padT = 10;
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const maxVal = Math.max(1, ...days.map(d => d.count));
    const niceMax = Math.ceil(maxVal / 5) * 5 || 5;

    const n = days.length;
    const bandW = plotW / n;
    const barW = Math.min(24, bandW * 0.55);
    const baseY = padT + plotH;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (plotH * i) / 4;
      const val = Math.round((niceMax * i) / 4);
      grid += `<line x1="${padL}" y1="${y}" x2="${W - 10}" y2="${y}" stroke="#2c2c2a" stroke-width="1" />`;
      grid += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">${val}</text>`;
    }

    let bars = "", axisLabels = "", hitRects = "";
    days.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const x = cx - barW / 2;
      const h = Math.max((d.count / niceMax) * plotH, d.count > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(x, baseY - h, barW, h, 4, CHART_COLOR);

      hitRects += `<rect class="hit" data-idx="${i}" x="${padL + bandW * i}" y="${padT}" width="${bandW}" height="${plotH}" fill="transparent" style="cursor:pointer" />`;

      if (i % 2 === 0 || n <= 10) {
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
        axisLabels += `<text x="${cx}" y="${H - 6}" text-anchor="middle" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">${label}</text>`;
      }
    });

    svg.innerHTML = grid + bars + axisLabels + hitRects;

    svg.querySelectorAll(".hit").forEach(hit => {
      const idx = parseInt(hit.dataset.idx, 10);
      const d = days[idx];
      hit.addEventListener("mousemove", (e) => {
        const wrapRect = document.getElementById("chart-wrap").getBoundingClientRect();
        tooltip.style.left = `${e.clientX - wrapRect.left}px`;
        tooltip.style.top = `${e.clientY - wrapRect.top - 10}px`;
        tooltip.style.opacity = "1";
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
        tooltip.innerHTML = `
          <div class="t-date">${label}</div>
          <div class="t-row"><span class="t-dot" style="background:${CHART_COLOR}"></span>Files: ${d.count}</div>
        `;
      });
      hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    });
  }

  function rectPath(x, y, w, h, topRadius, fill) {
    const r = Math.min(topRadius, h, w / 2);
    const fillAttr = fill ? ` fill="${fill}"` : "";
    if (r <= 0) return `<rect x="${x}" y="${y}" width="${w}" height="${h}"${fillAttr} />`;
    return `<path d="M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z"${fillAttr} />`;
  }

  // ═══════════════════════════════════════════════════════════════════
  // Dashboard tab: grouped summary table + filters + drill-down modal
  // ═══════════════════════════════════════════════════════════════════
  let summaryCache = [];

  async function loadSummary() {
    const res = await VanmoerAuth.authFetch("/api/admin/jobs/summary");
    summaryCache = await res.json();

    const users = [...new Map(summaryCache.map(r => [r.user_id, r.user_name])).entries()];
    const clients = [...new Set(summaryCache.map(r => r.client_slug + "::" + r.client_name))];
    const tasks = [...new Set(summaryCache.map(r => r.task_slug + "::" + r.task_name))];

    document.getElementById("filter-user").innerHTML = `<option value="">All users</option>` +
      users.map(([id, name]) => `<option value="${id}">${name}</option>`).join("");
    document.getElementById("filter-client").innerHTML = `<option value="">All clients</option>` +
      clients.map(c => { const [slug, name] = c.split("::"); return `<option value="${slug}">${name}</option>`; }).join("");
    document.getElementById("filter-task").innerHTML = `<option value="">All tasks</option>` +
      tasks.map(t => { const [slug, name] = t.split("::"); return `<option value="${slug}">${name}</option>`; }).join("");

    renderSummaryTable();
  }

  function renderSummaryTable() {
    const userFilter = document.getElementById("filter-user").value;
    const clientFilter = document.getElementById("filter-client").value;
    const taskFilter = document.getElementById("filter-task").value;
    const search = document.getElementById("summary-search").value.trim().toLowerCase();

    const filtered = summaryCache.filter(r =>
      (!userFilter || String(r.user_id) === userFilter) &&
      (!clientFilter || r.client_slug === clientFilter) &&
      (!taskFilter || r.task_slug === taskFilter) &&
      (!search || [r.user_name, r.username, r.client_name, r.task_name].join(" ").toLowerCase().includes(search))
    );

    const tbody = document.querySelector("#summary-table tbody");
    if (filtered.length === 0) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No jobs match these filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = filtered.map(r => `
      <tr class="clickable" data-user-id="${r.user_id}" data-client-slug="${r.client_slug}" data-task-slug="${r.task_slug}"
          data-user-name="${r.user_name}" data-client-name="${r.client_name}" data-task-name="${r.task_name}">
        <td>${r.user_name} <span class="mono">(${r.username})</span></td>
        <td>${r.client_name}</td>
        <td>${r.task_name}</td>
        <td class="num">
          <span class="count-pill">${r.count}
            ${r.failed_count > 0 ? `<span class="fail">· ${r.failed_count} failed</span>` : ""}
          </span>
        </td>
        <td class="mono">${r.last_run ? fmtTimestamp(r.last_run) : "—"}</td>
      </tr>
    `).join("");

    tbody.querySelectorAll("tr.clickable").forEach(tr => {
      tr.addEventListener("click", () => openDrillDown(tr.dataset));
    });
  }

  ["filter-user", "filter-client", "filter-task"].forEach(id => {
    document.getElementById(id).addEventListener("change", renderSummaryTable);
  });
  document.getElementById("summary-search").addEventListener("input", renderSummaryTable);

  let modalJobsCache = [];

  async function openDrillDown({ userId, clientSlug, taskSlug, userName, clientName, taskName }) {
    document.getElementById("modal-title").textContent = `${userName} — ${clientName} / ${taskName}`;
    document.getElementById("modal-sub").textContent = "Loading…";
    document.getElementById("modal-overlay").classList.add("open");
    document.getElementById("modal-search").value = "";
    document.querySelector("#modal-table tbody").innerHTML = "";

    const params = new URLSearchParams({ user_id: userId, client_slug: clientSlug, task_slug: taskSlug });
    const res = await VanmoerAuth.authFetch(`/api/admin/jobs?${params}`);
    modalJobsCache = await res.json();
    renderModalTable(modalJobsCache);
  }

  function renderModalTable(jobs) {
    document.getElementById("modal-sub").textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;
    document.querySelector("#modal-table tbody").innerHTML = jobs.length ? jobs.map(j => `
      <tr>
        <td class="mono">${fmtTimestamp(j.timestamp)}</td>
        <td>${j.reference || "—"}${j.reference_count > 1 ? ` <span class="badge">×${j.reference_count}</span>` : ""}</td>
        <td class="mono" title="${j.source_filename || ""}">${truncate(j.source_filename)}</td>
        <td class="num">${j.row_count ?? "—"}</td>
        <td><span class="badge status-${j.status}">${j.status}</span></td>
        <td>${j.status === "success" && j.download_url ? `<button class="dl-btn" data-url="${j.download_url}">⬇ Download</button>` : ""}</td>
      </tr>
    `).join("") : `<tr class="empty-row"><td colspan="6">${
      document.getElementById("modal-search").value.trim() ? "No jobs match this search." : "No jobs found."
    }</td></tr>`;

    document.querySelectorAll(".dl-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        await VanmoerAuth.downloadFile(btn.dataset.url, "output.xlsx");
      });
    });
  }

  document.getElementById("modal-search").addEventListener("input", (e) => {
    const search = e.target.value.trim().toLowerCase();
    if (!search) return renderModalTable(modalJobsCache);
    const filtered = modalJobsCache.filter(j =>
      [j.reference, j.source_filename, j.status, fmtTimestamp(j.timestamp)]
        .filter(Boolean).join(" ").toLowerCase().includes(search)
    );
    renderModalTable(filtered);
  });

  function truncate(s, n = 40) {
    if (!s) return "—";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  document.getElementById("modal-close").addEventListener("click", closeModal);
  document.getElementById("modal-overlay").addEventListener("click", (e) => {
    if (e.target.id === "modal-overlay") closeModal();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });
  function closeModal() { document.getElementById("modal-overlay").classList.remove("open"); }

  // ═══════════════════════════════════════════════════════════════════
  // Users tab (behavior unchanged from before, just re-scoped)
  // ═══════════════════════════════════════════════════════════════════
  let editingUserId = null;
  let usersCache = [];
  let clientsCache = [];
  let tasksCache = [];
  let currentGrants = [];

  async function loadClientsAndTasks() {
    const [clientsRes, tasksRes] = await Promise.all([
      VanmoerAuth.authFetch("/api/admin/clients"),
      VanmoerAuth.authFetch("/api/admin/tasks"),
    ]);
    clientsCache = await clientsRes.json();
    tasksCache = await tasksRes.json();

    document.getElementById("user-client").innerHTML =
      clientsCache.map(c => `<option value="${c.slug}">${c.name}</option>`).join("");
    document.getElementById("user-task").innerHTML =
      tasksCache.map(t => `<option value="${t.slug}">${t.name}</option>`).join("");
  }

  function renderGrantChips() {
    document.getElementById("grant-chips").innerHTML = currentGrants.map((g, i) => `
      <span class="chip">${g.client_name} / ${g.task_name}<button type="button" data-remove-grant="${i}">&times;</button></span>
    `).join("");
    document.querySelectorAll("[data-remove-grant]").forEach(btn => {
      btn.addEventListener("click", () => {
        currentGrants.splice(parseInt(btn.dataset.removeGrant, 10), 1);
        renderGrantChips();
      });
    });
  }

  document.getElementById("add-grant-btn").addEventListener("click", () => {
    const clientSlug = document.getElementById("user-client").value;
    const taskSlug = document.getElementById("user-task").value;
    const client = clientsCache.find(c => c.slug === clientSlug);
    const task = tasksCache.find(t => t.slug === taskSlug);
    if (!client || !task) return;
    if (currentGrants.some(g => g.client_slug === clientSlug && g.task_slug === taskSlug)) return;
    currentGrants.push({ client_slug: client.slug, client_name: client.name, task_slug: task.slug, task_name: task.name });
    renderGrantChips();
  });

  function resetUserForm() {
    editingUserId = null;
    currentGrants = [];
    document.getElementById("user-form-title").textContent = "Add user";
    document.getElementById("add-user-btn").textContent = "Add user";
    document.getElementById("cancel-edit-btn").style.display = "none";
    document.getElementById("user-password").placeholder = "";
    ["user-name", "user-username", "user-password"].forEach(id => document.getElementById(id).value = "");
    document.getElementById("user-role").value = "user";
    document.getElementById("assignment-block").style.display = "block";
    renderGrantChips();
  }

  function startEditUser(id) {
    const u = usersCache.find(x => x.id === id);
    if (!u) return;
    editingUserId = id;
    currentGrants = (u.grants || []).map(g => ({
      client_slug: g.client_slug, client_name: g.client,
      task_slug: g.task_slug, task_name: g.task,
    }));
    document.getElementById("user-form-title").textContent = `Edit user: ${u.username}`;
    document.getElementById("add-user-btn").textContent = "Update user";
    document.getElementById("cancel-edit-btn").style.display = "block";
    document.getElementById("user-name").value = u.name;
    document.getElementById("user-username").value = u.username;
    document.getElementById("user-password").value = "";
    document.getElementById("user-password").placeholder = "Leave blank to keep current password";
    document.getElementById("user-role").value = u.role;
    document.getElementById("assignment-block").style.display = u.role === "admin" ? "none" : "block";
    renderGrantChips();
    document.querySelector('[data-tab="users"]').click();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function loadUsers() {
    const res = await VanmoerAuth.authFetch("/api/admin/users");
    usersCache = await res.json();
    document.querySelector("#users-table tbody").innerHTML = usersCache.map(u => `
      <tr class="${u.is_active ? "" : "inactive"}">
        <td>${u.name}</td>
        <td>${u.username}</td>
        <td><span class="badge">${u.role}</span></td>
        <td>${(u.grants || []).map(g => `${g.client}/${g.task}`).join(", ") || "—"}</td>
        <td><span class="badge ${u.is_active ? "" : "inactive"}">${u.is_active ? "active" : "inactive"}</span></td>
        <td>
          <div class="row-actions">
            <button data-edit="${u.id}">Edit</button>
            ${u.is_active
              ? `<button class="danger" data-delete="${u.id}">Delete</button>`
              : `<button class="reactivate" data-reactivate="${u.id}">Reactivate</button>`}
          </div>
        </td>
      </tr>
    `).join("");

    document.querySelectorAll("[data-edit]").forEach(btn => {
      btn.addEventListener("click", () => startEditUser(parseInt(btn.dataset.edit, 10)));
    });
    document.querySelectorAll("[data-delete]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const u = usersCache.find(x => x.id === parseInt(btn.dataset.delete, 10));
        if (!confirm(`Delete user "${u.username}"? Their job history is kept, but they will no longer be able to log in.`)) return;
        const res = await VanmoerAuth.authFetch(`/api/admin/users/${u.id}`, { method: "DELETE" });
        const data = await res.json();
        if (!res.ok) return alert(data.error);
        if (editingUserId === u.id) resetUserForm();
        await loadUsers();
      });
    });
    document.querySelectorAll("[data-reactivate]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const res = await VanmoerAuth.authFetch(`/api/admin/users/${btn.dataset.reactivate}/reactivate`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) return alert(data.error);
        await loadUsers();
      });
    });
  }

  document.getElementById("user-role").addEventListener("change", (e) => {
    document.getElementById("assignment-block").style.display = e.target.value === "admin" ? "none" : "block";
  });

  document.getElementById("cancel-edit-btn").addEventListener("click", resetUserForm);

  document.getElementById("add-client-btn").addEventListener("click", async () => {
    const name = document.getElementById("client-name").value.trim();
    const msg = document.getElementById("client-msg");
    if (!name) return;
    const res = await VanmoerAuth.authFetch("/api/admin/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) return showMsg(msg, data.error, false);
    showMsg(msg, `Added client "${data.name}".`, true);
    document.getElementById("client-name").value = "";
    await loadClientsAndTasks();
  });

  document.getElementById("add-user-btn").addEventListener("click", async () => {
    const msg = document.getElementById("user-msg");
    const role = document.getElementById("user-role").value;
    const payload = {
      name: document.getElementById("user-name").value.trim(),
      username: document.getElementById("user-username").value.trim(),
      password: document.getElementById("user-password").value,
      role,
      grants: role === "user" ? currentGrants.map(g => ({ client_slug: g.client_slug, task_slug: g.task_slug })) : [],
    };

    const isEdit = editingUserId !== null;
    if (!isEdit && !payload.password) {
      return showMsg(msg, "Password is required for a new user", false);
    }
    if (role === "user" && payload.grants.length === 0) {
      return showMsg(msg, "Add at least one client/task grant", false);
    }

    const res = await VanmoerAuth.authFetch(
      isEdit ? `/api/admin/users/${editingUserId}` : "/api/admin/users",
      {
        method: isEdit ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    const data = await res.json();
    if (!res.ok) return showMsg(msg, data.error, false);
    showMsg(msg, isEdit ? `Updated user "${data.username}".` : `Added user "${data.username}".`, true);
    resetUserForm();
    await loadUsers();
    await loadSummary();
  });

  (async function init() {
    await loadClientsAndTasks();
    await loadUsers();
    await loadStats();
    await loadSummary();
  })();
})();
