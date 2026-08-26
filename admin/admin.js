(function () {
  const auth = VanmoerAuth.requireAuth("admin");
  if (!auth) return;

  const CHART_COLOR = "#4f7cff"; // matches --accent; kept literal (not var()) since it's injected via innerHTML

  // Fixed palette for the per-client/per-user charts below — cycled by index
  // (not hashed) so colors stay visually distinct even with few entries.
  const PALETTE = ["#4f7cff", "#f59e0b", "#34d399", "#f87171", "#a78bfa", "#22d3ee", "#fb923c", "#c084fc", "#facc15", "#38bdf8"];

  // Same client always gets the same color in both the bar and pie chart —
  // keyed off clientsCache's position (stable: list_clients() orders by name).
  function clientColor(slug) {
    const idx = clientsCache.findIndex(c => c.slug === slug);
    return PALETTE[(idx < 0 ? 0 : idx) % PALETTE.length];
  }

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

  function debounce(fn, wait) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), wait);
    };
  }

  // ═══════════════════════════════════════════════════════════════════
  // Dashboard tab: stat tiles
  // ═══════════════════════════════════════════════════════════════════
  async function loadStats() {
    const clientSlug = document.getElementById("chart-filter-client").value;
    const taskSlug = document.getElementById("chart-filter-task").value;
    const params = new URLSearchParams();
    if (clientSlug) params.set("client_slug", clientSlug);
    if (taskSlug) params.set("task_slug", taskSlug);

    const res = await VanmoerAuth.authFetch(`/api/admin/stats?${params}`);
    const s = await res.json();
    document.getElementById("stat-total").textContent = s.total_files.toLocaleString();
    document.getElementById("stat-rate").textContent = `${s.success_rate}%`;
    document.getElementById("stat-today").textContent = s.files_today.toLocaleString();
    document.getElementById("stat-week").textContent = s.files_this_week.toLocaleString();
    renderChart(s.series);
  }

  ["chart-filter-client", "chart-filter-task"].forEach(id => {
    document.getElementById(id).addEventListener("change", loadStats);
  });

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
    const W = 1080, H = 220, padL = 30, padB = 26, padT = 22;
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

    let bars = "", valueLabels = "", axisLabels = "", hitRects = "";
    days.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const x = cx - barW / 2;
      const h = Math.max((d.count / niceMax) * plotH, d.count > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(x, baseY - h, barW, h, 4, CHART_COLOR);

      if (d.count > 0) {
        const labelY = Math.max(baseY - h - 6, padT - 8);
        valueLabels += `<text x="${cx}" y="${labelY}" text-anchor="middle" font-size="10" fill="#9299a8" font-family="JetBrains Mono, monospace">${d.count}</text>`;
      }

      hitRects += `<rect class="hit" data-idx="${i}" x="${padL + bandW * i}" y="${padT}" width="${bandW}" height="${plotH}" fill="transparent" style="cursor:pointer" />`;

      if (i % 2 === 0 || n <= 10) {
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
        axisLabels += `<text x="${cx}" y="${H - 6}" text-anchor="middle" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">${label}</text>`;
      }
    });

    svg.innerHTML = grid + bars + valueLabels + axisLabels + hitRects;

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
  // Shared: dropdown period picker (Today/This week/This month/Custom via a
  // <select>, not the segmented-button control above) + generic pie chart.
  // Used by the two "files by client" charts and the productivity chart.
  // ═══════════════════════════════════════════════════════════════════
  function wireDropdownPeriodFilter(selectId, customRangeId, sinceId, untilId, applyId, state, onChange) {
    const select = document.getElementById(selectId);
    const customRange = document.getElementById(customRangeId);
    const sinceInput = document.getElementById(sinceId);
    const untilInput = document.getElementById(untilId);

    select.addEventListener("change", () => {
      state.period = select.value;
      if (state.period === "custom") {
        customRange.style.display = "flex";
        if (state.since && state.until) onChange();
      } else {
        customRange.style.display = "none";
        onChange();
      }
    });

    document.getElementById(applyId).addEventListener("click", () => {
      if (!sinceInput.value || !untilInput.value) return;
      state.since = sinceInput.value;
      state.until = untilInput.value;
      onChange();
    });
  }

  function periodParams(state) {
    const params = new URLSearchParams({ period: state.period });
    if (state.period === "custom") {
      if (!state.since || !state.until) return null;
      params.set("since", state.since);
      params.set("until", state.until);
    }
    return params;
  }

  function describeArcPath(cx, cy, r, startAngle, endAngle) {
    const start = { x: cx + r * Math.cos(startAngle), y: cy + r * Math.sin(startAngle) };
    const end = { x: cx + r * Math.cos(endAngle), y: cy + r * Math.sin(endAngle) };
    const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;
    return `M${cx},${cy} L${start.x},${start.y} A${r},${r} 0 ${largeArc} 1 ${end.x},${end.y} Z`;
  }

  // data: [{ label, value, color }]. Renders plain slices into `svg`, and a
  // swatch/name/value/% legend into `legendEl` — every entry gets a count in
  // the legend regardless of slice size (an in-slice label was tried, but a
  // thin sliver has no room to legibly hold text, which silently dropped its
  // count instead of just showing it smaller). Reused for the client/task
  // split chart and the productivity-by-user chart.
  function drawPieChart(svg, legendEl, data) {
    const nonZero = data.filter(d => d.value > 0);
    const total = nonZero.reduce((sum, d) => sum + d.value, 0);
    if (total <= 0) {
      svg.innerHTML = "";
      legendEl.innerHTML = `<div class="pie-empty">No files in this period.</div>`;
      return;
    }

    const cx = 90, cy = 90, r = 80;
    if (nonZero.length === 1) {
      // A single 100% slice degenerates to a zero-length arc (start === end
      // after a full 2π sweep) — draw a plain circle instead.
      svg.innerHTML = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${nonZero[0].color}" />`;
    } else {
      let angle = -Math.PI / 2; // start at 12 o'clock
      svg.innerHTML = nonZero.map(d => {
        const sweep = (d.value / total) * Math.PI * 2;
        const path = `<path d="${describeArcPath(cx, cy, r, angle, angle + sweep)}" fill="${d.color}" />`;
        angle += sweep;
        return path;
      }).join("");
    }

    legendEl.innerHTML = nonZero.map(d => `
      <div class="pie-legend-row">
        <span class="pie-legend-swatch" style="background:${d.color}"></span>
        <span class="pie-legend-name">${d.label}</span>
        <span class="pie-legend-value">${d.value.toLocaleString()} · ${Math.round(d.value / total * 100)}%</span>
      </div>
    `).join("");
  }

  // data: [{ client_name, client_slug, count, color }]
  function drawClientBarChart(svg, data) {
    const W = 520, H = 220, padL = 34, padB = 30, padT = 22;
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const maxVal = Math.max(1, ...data.map(d => d.count));
    const niceMax = Math.ceil(maxVal / 5) * 5 || 5;
    const n = Math.max(data.length, 1);
    const bandW = plotW / n;
    const barW = Math.min(48, bandW * 0.5);
    const baseY = padT + plotH;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (plotH * i) / 4;
      const val = Math.round((niceMax * i) / 4);
      grid += `<line x1="${padL}" y1="${y}" x2="${W - 10}" y2="${y}" stroke="#2c2c2a" stroke-width="1" />`;
      grid += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">${val}</text>`;
    }

    let bars = "", valueLabels = "", axisLabels = "";
    data.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const x = cx - barW / 2;
      const h = Math.max((d.count / niceMax) * plotH, d.count > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(x, baseY - h, barW, h, 4, d.color);
      if (d.count > 0) {
        const labelY = Math.max(baseY - h - 6, padT - 8);
        valueLabels += `<text x="${cx}" y="${labelY}" text-anchor="middle" font-size="10" fill="#9299a8" font-family="JetBrains Mono, monospace">${d.count}</text>`;
      }
      axisLabels += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#9299a8" font-family="JetBrains Mono, monospace">${truncate(d.client_name, 10)}</text>`;
    });

    svg.innerHTML = grid + bars + valueLabels + axisLabels;
  }

  const clientBarState = { period: "today", since: null, until: null };
  const clientPieState = { period: "today", since: null, until: null };

  async function loadClientBarChart() {
    const params = periodParams(clientBarState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/stats/by-client?${params}`);
    const rows = await res.json();
    drawClientBarChart(document.getElementById("client-bar-svg"),
      rows.map(r => ({ ...r, color: clientColor(r.client_slug) })));
  }

  async function loadClientPieChart() {
    const params = periodParams(clientPieState);
    if (!params) return;
    const taskSlug = document.getElementById("client-pie-task").value;
    if (taskSlug) params.set("task_slug", taskSlug);
    const res = await VanmoerAuth.authFetch(`/api/admin/stats/by-client?${params}`);
    const rows = await res.json();
    drawPieChart(
      document.getElementById("client-pie-svg"),
      document.getElementById("client-pie-legend"),
      rows.map(r => ({ label: r.client_name, value: r.count, color: clientColor(r.client_slug) }))
    );
  }

  document.getElementById("client-pie-task").addEventListener("change", loadClientPieChart);

  // ═══════════════════════════════════════════════════════════════════
  // Dashboard tab: grouped summary table + filters + drill-down modal
  // ═══════════════════════════════════════════════════════════════════
  let summaryCache = [];

  // ── Period filter: Today / This week / This month / a custom date range.
  // The date math lives server-side (admin/service.py:period_range) — this
  // just tracks which segment is active and, for "custom", the two dates —
  // so "Today" etc. mean the same thing everywhere instead of being
  // recomputed against the viewer's local clock in JS.
  const periodState = { period: "today", since: null, until: null };

  function initPeriodFilter() {
    // Scoped to #period-group, not just ".period-seg" — the Productivity
    // toggle button reuses that class for its pill styling but lives outside
    // this group and has no data-period; a page-wide selector here would
    // wire it into this handler too and stomp periodState.period to
    // undefined on every Productivity click.
    const segs = document.querySelectorAll("#period-group .period-seg");
    const customRange = document.getElementById("custom-range");
    const sinceInput = document.getElementById("custom-since");
    const untilInput = document.getElementById("custom-until");

    segs.forEach(btn => {
      btn.addEventListener("click", () => {
        segs.forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        periodState.period = btn.dataset.period;
        if (periodState.period === "custom") {
          customRange.style.display = "flex";
          if (periodState.since && periodState.until) refreshSummaryViews();
        } else {
          customRange.style.display = "none";
          refreshSummaryViews();
        }
      });
    });

    document.getElementById("custom-apply-btn").addEventListener("click", () => {
      if (!sinceInput.value || !untilInput.value) return;
      periodState.since = sinceInput.value;
      periodState.until = untilInput.value;
      refreshSummaryViews();
    });
  }

  // Filter OPTIONS are the full catalog (every user/client/task that exists),
  // not just whoever/whatever shows up in the current period's results — a
  // period with zero Carpenter jobs should still let you pick "Carpenter" to
  // see that zero, and switching periods shouldn't silently drop your
  // selection just because this period happens to have no matching rows.
  // Uses usersCache/clientsCache/tasksCache, loaded by loadUsers() /
  // loadClientsAndTasks() (see init()).
  function populateSummaryFilters() {
    const userSel = document.getElementById("filter-user");
    const clientSel = document.getElementById("filter-client");
    const taskSel = document.getElementById("filter-task");
    const prev = { user: userSel.value, client: clientSel.value, task: taskSel.value };

    userSel.innerHTML = `<option value="">All users</option>` +
      usersCache.map(u => `<option value="${u.id}">${u.name}</option>`).join("");
    clientSel.innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.slug}">${c.name}</option>`).join("");
    taskSel.innerHTML = `<option value="">All tasks</option>` +
      tasksCache.map(t => `<option value="${t.slug}">${t.name}</option>`).join("");

    userSel.value = prev.user;
    clientSel.value = prev.client;
    taskSel.value = prev.task;
  }

  // Shared by loadSummary() and loadProductivity() — both read the exact
  // same period/user/client/task/search filter row, they just aggregate the
  // result differently server-side.
  function buildSummaryFilterParams() {
    const params = periodParams(periodState);
    if (!params) return null;
    const userFilter = document.getElementById("filter-user").value;
    const clientFilter = document.getElementById("filter-client").value;
    const taskFilter = document.getElementById("filter-task").value;
    const search = document.getElementById("summary-search").value.trim();
    if (userFilter) params.set("user_id", userFilter);
    if (clientFilter) params.set("client_slug", clientFilter);
    if (taskFilter) params.set("task_slug", taskFilter);
    if (search) params.set("search", search);
    return params;
  }

  async function loadSummary() {
    const params = buildSummaryFilterParams();
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/jobs/summary?${params}`);
    summaryCache = await res.json();
    renderSummaryTable();
  }

  // Whether the productivity pie is currently toggled open — when it is,
  // any change to the shared filter row above should refresh it too.
  let productivityVisible = false;

  async function loadProductivity() {
    const params = buildSummaryFilterParams();
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/jobs/productivity?${params}`);
    const rows = await res.json();
    drawPieChart(
      document.getElementById("productivity-pie-svg"),
      document.getElementById("productivity-pie-legend"),
      rows.map((r, i) => ({ label: `${r.user_name} (${r.username})`, value: r.count, color: PALETTE[i % PALETTE.length] }))
    );
  }

  async function refreshSummaryViews() {
    await loadSummary();
    if (productivityVisible) await loadProductivity();
  }

  const debouncedRefreshSummaryViews = debounce(refreshSummaryViews, 300);

  document.getElementById("productivity-toggle").addEventListener("click", () => {
    productivityVisible = !productivityVisible;
    document.getElementById("productivity-toggle").classList.toggle("active", productivityVisible);
    document.getElementById("productivity-section").style.display = productivityVisible ? "block" : "none";
    if (productivityVisible) loadProductivity();
  });

  function renderSummaryTable() {
    const tbody = document.querySelector("#summary-table tbody");
    const tfoot = document.querySelector("#summary-table tfoot");
    if (summaryCache.length === 0) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No jobs match these filters.</td></tr>`;
      tfoot.innerHTML = "";
      return;
    }
    tbody.innerHTML = summaryCache.map(r => `
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

    const totalFiles = summaryCache.reduce((sum, r) => sum + r.count, 0);
    const totalFailed = summaryCache.reduce((sum, r) => sum + (r.failed_count || 0), 0);
    tfoot.innerHTML = `
      <tr class="total-row">
        <td colspan="3">Total</td>
        <td class="num">
          <span class="count-pill">${totalFiles}
            ${totalFailed > 0 ? `<span class="fail">· ${totalFailed} failed</span>` : ""}
          </span>
        </td>
        <td></td>
      </tr>
    `;
  }

  ["filter-user", "filter-client", "filter-task"].forEach(id => {
    document.getElementById(id).addEventListener("change", refreshSummaryViews);
  });
  document.getElementById("summary-search").addEventListener("input", debouncedRefreshSummaryViews);

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
    `).join("") : `<tr class="empty-row"><td colspan="6">${document.getElementById("modal-search").value.trim() ? "No jobs match this search." : "No jobs found."
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

    document.getElementById("chart-filter-client").innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.slug}">${c.name}</option>`).join("");
    document.getElementById("chart-filter-task").innerHTML = `<option value="">All tasks</option>` +
      tasksCache.map(t => `<option value="${t.slug}">${t.name}</option>`).join("");

    document.getElementById("users-filter-client").innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.slug}">${c.name}</option>`).join("");
    document.getElementById("users-filter-task").innerHTML = `<option value="">All tasks</option>` +
      tasksCache.map(t => `<option value="${t.slug}">${t.name}</option>`).join("");

    document.getElementById("client-pie-task").innerHTML = `<option value="">All tasks</option>` +
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

  // usersCache is the full, unfiltered catalog — used for edit/delete lookups
  // and the "All users" summary-filter dropdown. usersTableRows is whatever
  // the Users tab's own client/task filters currently return from the DB
  // (see list_users() in admin/service.py) and is what actually renders.
  let usersTableRows = [];

  async function loadUsers() {
    const res = await VanmoerAuth.authFetch("/api/admin/users");
    usersCache = await res.json();
  }

  // Counts come straight off the already-loaded full catalogs (usersCache /
  // clientsCache), not a separate fetch — both are refreshed on every action
  // that could change them, so this stays in sync for free.
  function updateUserStatCards() {
    document.getElementById("stat-total-users").textContent = usersCache.length.toLocaleString();
    document.getElementById("stat-total-clients").textContent = clientsCache.length.toLocaleString();
  }

  // Must run after loadUsers() has resolved (not in parallel with it) —
  // the no-filter branch reuses usersCache instead of firing an identical
  // second request to the same endpoint, which only works if usersCache is
  // already populated by the time this checks it.
  async function loadUsersTable() {
    const clientSlug = document.getElementById("users-filter-client").value;
    const taskSlug = document.getElementById("users-filter-task").value;
    if (!clientSlug && !taskSlug) {
      usersTableRows = usersCache;
      renderUsersTable();
      return;
    }

    const params = new URLSearchParams();
    if (clientSlug) params.set("client_slug", clientSlug);
    if (taskSlug) params.set("task_slug", taskSlug);
    const res = await VanmoerAuth.authFetch(`/api/admin/users?${params}`);
    usersTableRows = await res.json();
    renderUsersTable();
  }

  function renderUsersTable() {
    const tbody = document.querySelector("#users-table tbody");
    if (usersTableRows.length === 0) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="6">No users match these filters.</td></tr>`;
      return;
    }
    tbody.innerHTML = usersTableRows.map(u => `
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
        await loadUsersTable();
        populateSummaryFilters();
        updateUserStatCards();
      });
    });
    document.querySelectorAll("[data-reactivate]").forEach(btn => {
      btn.addEventListener("click", async () => {
        const res = await VanmoerAuth.authFetch(`/api/admin/users/${btn.dataset.reactivate}/reactivate`, { method: "POST" });
        const data = await res.json();
        if (!res.ok) return alert(data.error);
        await loadUsers();
        await loadUsersTable();
        populateSummaryFilters();
        updateUserStatCards();
      });
    });
  }

  ["users-filter-client", "users-filter-task"].forEach(id => {
    document.getElementById(id).addEventListener("change", loadUsersTable);
  });

  document.getElementById("user-role").addEventListener("change", (e) => {
    document.getElementById("assignment-block").style.display = e.target.value === "admin" ? "none" : "block";
  });

  document.getElementById("cancel-edit-btn").addEventListener("click", resetUserForm);

  document.getElementById("inline-add-client-btn").addEventListener("click", async () => {
    const name = document.getElementById("inline-client-name").value.trim();
    const msg = document.getElementById("inline-client-msg");
    if (!name) return;
    const res = await VanmoerAuth.authFetch("/api/admin/clients", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const data = await res.json();
    if (!res.ok) return showMsg(msg, data.error, false);
    showMsg(msg, `Added client "${data.name}".`, true);
    document.getElementById("inline-client-name").value = "";
    await loadClientsAndTasks();
    populateSummaryFilters();
    updateUserStatCards();
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
    await loadUsersTable();
    populateSummaryFilters();
    updateUserStatCards();
    await refreshSummaryViews();
  });

  // Everything that makes up the dashboard's current view — used both for
  // the initial load and for the background auto-refresh below. Each call
  // is its own API round-trip and none of them depend on each other's
  // result, so they're fired together (Promise.all) instead of one `await`
  // at a time — the page waits on the slowest single query instead of the
  // sum of all ~8. (Pairs with threaded=True on the dev server in main.py —
  // without that, Werkzeug's dev server would still serve these one at a
  // time no matter how they're fired here.)
  async function loadAllDashboardData() {
    // loadUsersTable() reuses usersCache when unfiltered (see its comment)
    // instead of firing an identical second request to /api/admin/users,
    // so it has to come after loadUsers() resolves rather than racing it.
    await Promise.all([loadClientsAndTasks(), loadUsers()]);
    await loadUsersTable();
    populateSummaryFilters();
    updateUserStatCards();

    const tasks = [loadStats(), loadSummary(), loadClientBarChart(), loadClientPieChart()];
    if (productivityVisible) tasks.push(loadProductivity());
    await Promise.all(tasks);
  }

  // Background auto-refresh — nothing here pushes updates into an already-
  // open tab on its own (no websocket/SSE), so without this, another
  // admin's changes (or your own from a second tab) only show up after a
  // manual browser refresh. Silent: no loader, no visible state change
  // beyond the numbers updating, and errors are swallowed since a transient
  // failure on a background tick shouldn't surface — the next tick retries.
  const AUTO_REFRESH_INTERVAL_MS = 30000;
  let isAutoRefreshing = false;

  async function autoRefreshDashboard() {
    if (isAutoRefreshing || document.visibilityState !== "visible") return;
    isAutoRefreshing = true;
    try {
      await loadAllDashboardData();
    } catch {
      // swallowed — see comment above
    } finally {
      isAutoRefreshing = false;
    }
  }

  setInterval(autoRefreshDashboard, AUTO_REFRESH_INTERVAL_MS);

  (async function init() {
    initPeriodFilter();
    wireDropdownPeriodFilter("client-bar-period", "client-bar-custom-range", "client-bar-since", "client-bar-until", "client-bar-apply-btn", clientBarState, loadClientBarChart);
    wireDropdownPeriodFilter("client-pie-period", "client-pie-custom-range", "client-pie-since", "client-pie-until", "client-pie-apply-btn", clientPieState, loadClientPieChart);

    // The full-page loader (visible from first paint, see #page-loader in
    // the CSS) covers this stretch instead of a blank dashboard, and the
    // `finally` guarantees it's dismissed even if a fetch fails, so errors
    // never leave the user staring at a stuck spinner.
    try {
      await loadAllDashboardData();
    } finally {
      document.getElementById("page-loader").classList.add("hidden");
    }
  })();
})();
