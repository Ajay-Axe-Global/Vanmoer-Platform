(function () {
  const auth = VanmoerAuth.requireAuth("admin");
  if (!auth) return;

  let editingUserId = null; // null = "Add user" mode, else "Update user" mode
  let usersCache = [];
  let clientsCache = [];
  let tasksCache = [];
  let currentGrants = []; // [{client_slug, client_name, task_slug, task_name}, ...]

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
    if (currentGrants.some(g => g.client_slug === clientSlug && g.task_slug === taskSlug)) return; // no dupes
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

  async function loadReports() {
    const [byUserRes, byClientRes] = await Promise.all([
      VanmoerAuth.authFetch("/api/admin/jobs/by-user"),
      VanmoerAuth.authFetch("/api/admin/jobs/by-client"),
    ]);
    const byUser = await byUserRes.json();
    const byClient = await byClientRes.json();

    document.querySelector("#jobs-by-user-table tbody").innerHTML = byUser.map(r => `
      <tr><td>${r.name} (${r.username})</td><td>${r.count}</td></tr>
    `).join("") || `<tr><td colspan="2">No jobs yet.</td></tr>`;

    document.querySelector("#jobs-by-client-table tbody").innerHTML = byClient.map(r => `
      <tr><td>${r.client}</td><td>${r.count}</td></tr>
    `).join("") || `<tr><td colspan="2">No jobs yet.</td></tr>`;
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
    await loadReports();
  });

  (async function init() {
    await loadClientsAndTasks();
    await loadUsers();
    await loadReports();
  })();
})();
