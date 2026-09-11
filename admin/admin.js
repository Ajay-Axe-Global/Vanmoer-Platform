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
  // The Billing tab's data isn't part of loadAllDashboardData()/auto-refresh
  // (it's a heavier, less time-critical view than job stats) — loaded
  // lazily every time the tab is opened instead.
  document.querySelectorAll(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "billing") loadBillingTab();
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

  // IANA name (e.g. "Asia/Kolkata"), sent with every period-based request so
  // "Today"/"This week"/"This month" and the per-day chart bucket by the
  // viewer's calendar day instead of the server's UTC day.
  const VIEWER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

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
    const params = new URLSearchParams({ tz: VIEWER_TZ });
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
    const params = new URLSearchParams({ period: state.period, tz: VIEWER_TZ });
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
        <td>${renderRefCell(j)}</td>
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

  function renderRefCell(j) {
    if (!j.reference) return "—";
    const chips = j.reference.split(", ").map(r => `<span class="ref-chip">${r}</span>`).join("");
    const countBadge = j.reference_count > 1 ? ` <span class="badge">×${j.reference_count}</span>` : "";
    return `<div class="ref-cell">${chips}</div>${countBadge}`;
  }

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

  // ═══════════════════════════════════════════════════════════════════
  // Billing & Usage tab — Gemini token cost, computed server-side (see
  // admin/service.py's usage_* functions and helpers/billing.py) from
  // GeminiUsageLog rows written by helpers/jobs.log_job(). Every chart on
  // this tab owns its OWN period filter (via wireDropdownPeriodFilter(),
  // the same generic helper the Dashboard tab's client-bar/client-pie
  // charts already use) rather than one shared filter row — so changing
  // one chart's period never moves any other chart's data.
  // ═══════════════════════════════════════════════════════════════════

  const billingOverviewState = { period: "today", since: null, until: null };
  const billingDayState = { period: "month", since: null, until: null };
  const billingHdState = { period: "month", since: null, until: null };
  const billingClientBarState = { period: "month", since: null, until: null };
  const billingModelPieState = { period: "month", since: null, until: null };
  const billingTaskBarState = { period: "month", since: null, until: null };
  const billingUserPieState = { period: "month", since: null, until: null };

  let modelsCache = []; // distinct model_name values that have a pricing row — populated by loadPricingTable()

  function fmtInr(n) {
    return "₹" + Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 });
  }

  function fmtUsd(n) {
    return "$" + Number(n || 0).toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 });
  }

  // Filter OPTIONS are the full catalog (same reasoning as
  // populateSummaryFilters() on the Dashboard tab) — a period with no
  // matching rows for a given client/task/user/model shouldn't make that
  // option disappear from the dropdown. Only the Overview panel has these
  // four selects — the per-chart breakdowns below (by client/task/model/
  // user) don't need them, since each chart's whole job IS that breakdown.
  function populateBillingFilters() {
    const clientSel = document.getElementById("billing-filter-client");
    const taskSel = document.getElementById("billing-filter-task");
    const userSel = document.getElementById("billing-filter-user");
    const modelSel = document.getElementById("billing-filter-model");
    const prev = { client: clientSel.value, task: taskSel.value, user: userSel.value, model: modelSel.value };

    clientSel.innerHTML = `<option value="">All clients</option>` +
      clientsCache.map(c => `<option value="${c.slug}">${c.name}</option>`).join("");
    taskSel.innerHTML = `<option value="">All tasks</option>` +
      tasksCache.map(t => `<option value="${t.slug}">${t.name}</option>`).join("");
    userSel.innerHTML = `<option value="">All users</option>` +
      usersCache.map(u => `<option value="${u.id}">${u.name}</option>`).join("");
    modelSel.innerHTML = `<option value="">All models</option>` +
      modelsCache.map(m => `<option value="${m}">${m}</option>`).join("");

    clientSel.value = prev.client;
    taskSel.value = prev.task;
    userSel.value = prev.user;
    modelSel.value = prev.model;
  }

  function buildOverviewParams() {
    const params = periodParams(billingOverviewState);
    if (!params) return null;
    const clientFilter = document.getElementById("billing-filter-client").value;
    const taskFilter = document.getElementById("billing-filter-task").value;
    const userFilter = document.getElementById("billing-filter-user").value;
    const modelFilter = document.getElementById("billing-filter-model").value;
    if (clientFilter) params.set("client_slug", clientFilter);
    if (taskFilter) params.set("task_slug", taskFilter);
    if (userFilter) params.set("user_id", userFilter);
    if (modelFilter) params.set("model_name", modelFilter);
    return params;
  }

  ["billing-filter-client", "billing-filter-task", "billing-filter-user", "billing-filter-model"].forEach(id => {
    document.getElementById(id).addEventListener("change", loadBillingSummary);
  });

  async function loadBillingSummary() {
    const params = buildOverviewParams();
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/summary?${params}`);
    const s = await res.json();
    document.getElementById("billing-stat-cost-inr").textContent = fmtInr(s.total_cost_inr);
    document.getElementById("billing-stat-cost-usd").textContent = fmtUsd(s.total_cost_usd);
    document.getElementById("billing-stat-tokens").textContent = s.total_tokens.toLocaleString();
    document.getElementById("billing-stat-tokens-split").textContent =
      `${s.total_prompt_tokens.toLocaleString()} in · ${s.total_completion_tokens.toLocaleString()} out`;
    document.getElementById("billing-stat-calls").textContent = s.total_calls.toLocaleString();
    document.getElementById("billing-stat-jobs").textContent = s.total_jobs.toLocaleString();
    document.getElementById("billing-stat-avg").textContent = fmtInr(s.avg_cost_per_job_inr);
  }

  // Cost-per-day bars, same layout math as drawChart() (Dashboard tab) but
  // an independent copy — this one's values are ₹ floats (not integer file
  // counts) and highlights the period's single highest-cost day in a
  // brighter color, the "high demand day" visual cue.
  function drawBillingDayChart(days) {
    const svg = document.getElementById("billing-chart-svg");
    const tooltip = document.getElementById("billing-chart-tooltip");
    const W = 1080, H = 220, padL = 44, padB = 26, padT = 22;
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const maxVal = Math.max(1, ...days.map(d => d.cost_inr));
    const niceMax = maxVal * 1.15 || 5;
    const peakCost = Math.max(0, ...days.map(d => d.cost_inr));

    // Persistent label (top-right of the panel, next to the "Cost per day"
    // title) — the peak day's amount, visible at all times without needing
    // to hover; the tooltip below still gives the full per-bar breakdown on
    // hover, this is just the headline number for the busiest day.
    const peakLabel = document.getElementById("billing-day-peak-label");
    const peakDay = days.find(d => peakCost > 0 && d.cost_inr === peakCost);
    if (peakDay) {
      const label = new Date(peakDay.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short" });
      peakLabel.innerHTML = `Peak: <strong style="color:var(--text)">${fmtInr(peakDay.cost_inr)}</strong> on ${label}`;
    } else {
      peakLabel.textContent = "No spend in this period";
    }

    const n = days.length;
    const bandW = plotW / n;
    const barW = Math.min(24, bandW * 0.55);
    const baseY = padT + plotH;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (plotH * i) / 4;
      const val = (niceMax * i) / 4;
      grid += `<line x1="${padL}" y1="${y}" x2="${W - 10}" y2="${y}" stroke="#2c2c2a" stroke-width="1" />`;
      grid += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">₹${val.toFixed(0)}</text>`;
    }

    let bars = "", valueLabels = "", axisLabels = "", hitRects = "";
    days.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const x = cx - barW / 2;
      const h = Math.max((d.cost_inr / niceMax) * plotH, d.cost_inr > 0 ? 1 : 0);
      const isPeak = peakCost > 0 && d.cost_inr === peakCost;
      if (h > 0) bars += rectPath(x, baseY - h, barW, h, 4, isPeak ? "#f59e0b" : CHART_COLOR);

      // ₹ amount on top of every non-zero bar — visible without hovering
      // (the tooltip on hover still adds tokens/calls on top of this).
      if (d.cost_inr > 0) {
        const labelY = Math.max(baseY - h - 6, padT - 8);
        const amount = d.cost_inr >= 100 ? d.cost_inr.toFixed(0) : d.cost_inr.toFixed(1);
        valueLabels += `<text x="${cx}" y="${labelY}" text-anchor="middle" font-size="9.5" fill="${isPeak ? "#f59e0b" : "#9299a8"}" font-family="JetBrains Mono, monospace">₹${amount}</text>`;
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
        const wrapRect = document.getElementById("billing-chart-wrap").getBoundingClientRect();
        tooltip.style.left = `${e.clientX - wrapRect.left}px`;
        tooltip.style.top = `${e.clientY - wrapRect.top - 10}px`;
        tooltip.style.opacity = "1";
        const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
        tooltip.innerHTML = `
          <div class="t-date">${label}${d.cost_inr === peakCost && peakCost > 0 ? " · peak day" : ""}</div>
          <div class="t-row"><span class="t-dot" style="background:${CHART_COLOR}"></span>${fmtInr(d.cost_inr)} (${fmtUsd(d.cost_usd)})</div>
          <div class="t-row">${d.tokens.toLocaleString()} tokens · ${d.calls} call(s)</div>
        `;
      });
      hit.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
    });
  }

  // Generic ₹-cost bar chart, same layout as drawClientBarChart() (Dashboard
  // tab) but not tied to the "client_name" field — reused for both
  // Cost-by-client and Cost-by-task.
  function drawCostBarChart(svg, data, labelKey) {
    const W = 520, H = 220, padL = 40, padB = 30, padT = 22;
    const plotW = W - padL - 10, plotH = H - padT - padB;
    const maxVal = Math.max(1, ...data.map(d => d.cost_inr));
    const niceMax = maxVal * 1.15 || 5;
    const n = Math.max(data.length, 1);
    const bandW = plotW / n;
    const barW = Math.min(48, bandW * 0.5);
    const baseY = padT + plotH;

    let grid = "";
    for (let i = 0; i <= 4; i++) {
      const y = padT + plotH - (plotH * i) / 4;
      const val = (niceMax * i) / 4;
      grid += `<line x1="${padL}" y1="${y}" x2="${W - 10}" y2="${y}" stroke="#2c2c2a" stroke-width="1" />`;
      grid += `<text x="${padL - 8}" y="${y + 3}" text-anchor="end" font-size="10" fill="#6b7280" font-family="JetBrains Mono, monospace">₹${val.toFixed(0)}</text>`;
    }

    let bars = "", valueLabels = "", axisLabels = "";
    data.forEach((d, i) => {
      const cx = padL + bandW * i + bandW / 2;
      const x = cx - barW / 2;
      const h = Math.max((d.cost_inr / niceMax) * plotH, d.cost_inr > 0 ? 1 : 0);
      if (h > 0) bars += rectPath(x, baseY - h, barW, h, 4, d.color);
      if (d.cost_inr > 0) {
        const labelY = Math.max(baseY - h - 6, padT - 8);
        valueLabels += `<text x="${cx}" y="${labelY}" text-anchor="middle" font-size="10" fill="#9299a8" font-family="JetBrains Mono, monospace">₹${d.cost_inr.toFixed(0)}</text>`;
      }
      axisLabels += `<text x="${cx}" y="${H - 10}" text-anchor="middle" font-size="10" fill="#9299a8" font-family="JetBrains Mono, monospace">${truncate(d[labelKey], 10)}</text>`;
    });

    svg.innerHTML = grid + bars + valueLabels + axisLabels;
  }

  function taskColor(slug) {
    const idx = tasksCache.findIndex(t => t.slug === slug);
    return PALETTE[(idx < 0 ? 0 : idx) % PALETTE.length];
  }

  async function loadBillingDayChart() {
    const params = periodParams(billingDayState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/usage-by-day?${params}`);
    const days = await res.json();
    drawBillingDayChart(days);
  }

  async function loadHighDemandDays() {
    const params = periodParams(billingHdState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/high-demand-days?${params}`);
    const rows = await res.json();
    const el = document.getElementById("billing-high-demand-list");
    if (!rows.length) {
      el.innerHTML = `<div class="high-demand-empty">No usage in this period.</div>`;
      return;
    }
    el.innerHTML = rows.map((d, i) => {
      const label = new Date(d.date + "T00:00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
      return `
        <div class="high-demand-row">
          <div><span class="rank">${i + 1}</span><span class="date">${label}</span></div>
          <div class="cost">${fmtInr(d.cost_inr)} <span style="color:var(--text-muted);font-weight:400">· ${d.calls} call(s), ${d.tokens.toLocaleString()} tokens</span></div>
        </div>
      `;
    }).join("");
  }

  async function loadBillingClientBar() {
    const params = periodParams(billingClientBarState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/usage-by-client?${params}`);
    const rows = await res.json();
    drawCostBarChart(
      document.getElementById("billing-client-bar-svg"),
      rows.map(r => ({ ...r, color: clientColor(r.client_slug) })),
      "client_name"
    );
  }

  async function loadBillingTaskBar() {
    const params = periodParams(billingTaskBarState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/usage-by-task?${params}`);
    const rows = await res.json();
    drawCostBarChart(
      document.getElementById("billing-task-bar-svg"),
      rows.map(r => ({ ...r, color: taskColor(r.task_slug) })),
      "task_name"
    );
  }

  async function loadBillingModelPie() {
    const params = periodParams(billingModelPieState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/usage-by-model?${params}`);
    const rows = await res.json();
    drawPieChart(
      document.getElementById("billing-model-pie-svg"),
      document.getElementById("billing-model-pie-legend"),
      rows.map((r, i) => ({ label: r.model_name, value: r.cost_inr, color: PALETTE[i % PALETTE.length] }))
    );
  }

  async function loadBillingUserPie() {
    const params = periodParams(billingUserPieState);
    if (!params) return;
    const res = await VanmoerAuth.authFetch(`/api/admin/billing/usage-by-user?${params}`);
    const rows = await res.json();
    drawPieChart(
      document.getElementById("billing-user-pie-svg"),
      document.getElementById("billing-user-pie-legend"),
      rows.map((r, i) => ({ label: `${r.user_name} (${r.username})`, value: r.cost_inr, color: PALETTE[i % PALETTE.length] }))
    );
  }

  async function refreshBillingViews() {
    await Promise.all([
      loadBillingSummary(), loadBillingDayChart(), loadHighDemandDays(),
      loadBillingClientBar(), loadBillingTaskBar(), loadBillingModelPie(), loadBillingUserPie(),
    ]);
  }

  // ── Rates: pricing & exchange-rate management (hidden until the "⚙
  // Rates" toggle in the Overview panel is clicked) ───────────────────

  function fmtDateOnly(iso) {
    return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  }

  async function loadPricingTable() {
    const res = await VanmoerAuth.authFetch("/api/admin/billing/pricing");
    const rows = await res.json();
    modelsCache = [...new Set(rows.map(r => r.model_name))];
    document.querySelector("#pricing-table tbody").innerHTML = rows.map(p => `
      <tr>
        <td>${p.model_name}</td>
        <td>$${p.input_price_usd_per_million}</td>
        <td>$${p.output_price_usd_per_million}</td>
        <td>${fmtDateOnly(p.effective_from)}</td>
      </tr>
    `).join("") || `<tr><td colspan="4" style="color:var(--text-muted)">No pricing entered yet.</td></tr>`;
    return rows;
  }

  async function loadRateTable() {
    const res = await VanmoerAuth.authFetch("/api/admin/billing/exchange-rate");
    const rows = await res.json();
    document.querySelector("#rate-table tbody").innerHTML = rows.map(r => `
      <tr>
        <td>₹${r.usd_to_inr}</td>
        <td>${fmtDateOnly(r.effective_from)}</td>
      </tr>
    `).join("") || `<tr><td colspan="2" style="color:var(--text-muted)">No exchange rate entered yet.</td></tr>`;
    return rows;
  }

  async function loadPricingAndRateTables() {
    await Promise.all([loadPricingTable(), loadRateTable()]);
    populateBillingFilters(); // modelsCache just changed
  }

  document.getElementById("billing-rates-toggle").addEventListener("click", async () => {
    const section = document.getElementById("billing-rates-section");
    const toggle = document.getElementById("billing-rates-toggle");
    const opening = section.style.display === "none";
    section.style.display = opening ? "block" : "none";
    toggle.classList.toggle("active", opening);
    if (opening) await loadPricingAndRateTables();
  });

  document.getElementById("add-pricing-btn").addEventListener("click", async () => {
    const msg = document.getElementById("pricing-msg");
    const modelName = document.getElementById("pricing-model-name").value.trim();
    const inputPrice = document.getElementById("pricing-input-price").value;
    const outputPrice = document.getElementById("pricing-output-price").value;
    const effectiveFrom = document.getElementById("pricing-effective-from").value;
    if (!modelName || inputPrice === "" || outputPrice === "") {
      showMsg(msg, "Model, input price, and output price are all required.", false);
      return;
    }
    try {
      const res = await VanmoerAuth.authFetch("/api/admin/billing/pricing", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model_name: modelName,
          input_price_usd_per_million: parseFloat(inputPrice),
          output_price_usd_per_million: parseFloat(outputPrice),
          effective_from: effectiveFrom ? new Date(effectiveFrom).toISOString() : null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to add pricing");
      showMsg(msg, "Pricing added.", true);
      document.getElementById("pricing-model-name").value = "";
      document.getElementById("pricing-input-price").value = "";
      document.getElementById("pricing-output-price").value = "";
      document.getElementById("pricing-effective-from").value = "";
      await loadPricingAndRateTables();
      refreshBillingViews();
    } catch (e) {
      showMsg(msg, e.message, false);
    }
  });

  document.getElementById("add-rate-btn").addEventListener("click", async () => {
    const msg = document.getElementById("rate-msg");
    const rateValue = document.getElementById("rate-value").value;
    const effectiveFrom = document.getElementById("rate-effective-from").value;
    if (rateValue === "") {
      showMsg(msg, "Rate is required.", false);
      return;
    }
    try {
      const res = await VanmoerAuth.authFetch("/api/admin/billing/exchange-rate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          usd_to_inr: parseFloat(rateValue),
          effective_from: effectiveFrom ? new Date(effectiveFrom).toISOString() : null,
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to add exchange rate");
      showMsg(msg, "Exchange rate added.", true);
      document.getElementById("rate-value").value = "";
      document.getElementById("rate-effective-from").value = "";
      await loadPricingAndRateTables();
      refreshBillingViews();
    } catch (e) {
      showMsg(msg, e.message, false);
    }
  });

  async function loadBillingTab() {
    populateBillingFilters();
    await loadPricingTable(); // populates modelsCache for the Overview "All models" filter — table itself stays hidden until Rates is opened
    await refreshBillingViews();
  }

  (async function init() {
    initPeriodFilter();
    wireDropdownPeriodFilter("client-bar-period", "client-bar-custom-range", "client-bar-since", "client-bar-until", "client-bar-apply-btn", clientBarState, loadClientBarChart);
    wireDropdownPeriodFilter("client-pie-period", "client-pie-custom-range", "client-pie-since", "client-pie-until", "client-pie-apply-btn", clientPieState, loadClientPieChart);

    // Billing tab — one independent period picker per chart (see the
    // "Billing & Usage tab" section above for why).
    wireDropdownPeriodFilter("billing-ov-period", "billing-ov-custom-range", "billing-ov-since", "billing-ov-until", "billing-ov-apply-btn", billingOverviewState, loadBillingSummary);
    wireDropdownPeriodFilter("billing-day-period", "billing-day-custom-range", "billing-day-since", "billing-day-until", "billing-day-apply-btn", billingDayState, loadBillingDayChart);
    wireDropdownPeriodFilter("billing-hd-period", "billing-hd-custom-range", "billing-hd-since", "billing-hd-until", "billing-hd-apply-btn", billingHdState, loadHighDemandDays);
    wireDropdownPeriodFilter("billing-cbc-period", "billing-cbc-custom-range", "billing-cbc-since", "billing-cbc-until", "billing-cbc-apply-btn", billingClientBarState, loadBillingClientBar);
    wireDropdownPeriodFilter("billing-cbm-period", "billing-cbm-custom-range", "billing-cbm-since", "billing-cbm-until", "billing-cbm-apply-btn", billingModelPieState, loadBillingModelPie);
    wireDropdownPeriodFilter("billing-cbt-period", "billing-cbt-custom-range", "billing-cbt-since", "billing-cbt-until", "billing-cbt-apply-btn", billingTaskBarState, loadBillingTaskBar);
    wireDropdownPeriodFilter("billing-cbu-period", "billing-cbu-custom-range", "billing-cbu-since", "billing-cbu-until", "billing-cbu-apply-btn", billingUserPieState, loadBillingUserPie);

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
