(function () {
  const auth = VanmoerAuth.requireAuth();
  if (!auth) return;

  document.getElementById("logout-btn").addEventListener("click", () => VanmoerAuth.logout());

  if (auth.role === "admin") {
    window.location.href = "/admin";
    return;
  }

  const grants = auth.grants || [];
  if (grants.length === 1) {
    window.location.href = `/app/${grants[0].client_slug}/${grants[0].task_slug}/`;
    return;
  }

  document.getElementById("grid").innerHTML = grants.map(g => `
    <a class="card" href="/app/${g.client_slug}/${g.task_slug}/">
      <div class="client">${g.client_name}</div>
      <div class="task">${g.task_name}</div>
    </a>
  `).join("") || `<p style="color:#8b93a1">No tasks assigned. Contact an admin.</p>`;
})();
