/**
 * Shared client-side auth helpers, loaded by the admin dashboard and every
 * client task page. The JWT lives in localStorage (not a cookie), so it is
 * never auto-attached to page navigations — each protected page must check
 * it on load and attach it manually to every API call.
 */
const VanmoerAuth = {
  KEY: "vanmoer_auth",

  get() {
    try {
      return JSON.parse(localStorage.getItem(this.KEY));
    } catch {
      return null;
    }
  },

  logout() {
    localStorage.removeItem(this.KEY);
    window.location.href = "/login";
  },

  /** Redirects to /login if there's no token, or if `role` is given and doesn't match. */
  requireAuth(role) {
    const auth = this.get();
    if (!auth || !auth.token) {
      window.location.href = "/login";
      return null;
    }
    if (role && auth.role !== role) {
      window.location.href = "/login";
      return null;
    }
    // Pages that already manage their own header/logout (admin dashboard,
    // the multi-grant task picker) have a #logout-btn — skip those so this
    // doesn't add a second, redundant logout affordance. Every plain task
    // page (no header of its own) gets the floating menu for free.
    // The floating user menu is skipped on pages that already render their
    // own logout button (the dashboard picker, admin panel, and a few older
    // task pages like Carpenter/Sabic Outbound with a bespoke header) so
    // there's never a duplicate logout affordance. The task-switcher drawer
    // is a SEPARATE concern from that — it's gated on its own, by URL
    // (any /app/... task page, see mountTaskDrawer()), so those same older
    // task pages still get it even though they keep their own logout button.
    if (!document.getElementById("logout-btn")) {
      this.mountUserMenu(auth);
    }
    this.mountTaskDrawer(auth);
    return auth;
  },

  /**
   * Like requireAuth(), but for a specific client/task page: also verifies
   * the token actually grants access to (clientSlug, taskSlug) — admins
   * pass automatically. requireAuth() alone only proves "logged in", so a
   * user with grants for other tasks could otherwise load any task page
   * directly by URL even without a grant for it (the server-side API
   * routes are still protected, but the page shell itself would render).
   * Redirects to /dashboard, not /login, since the user IS authenticated —
   * they just don't belong on this particular task.
   */
  requireTaskAccess(clientSlug, taskSlug) {
    const auth = this.requireAuth();
    if (!auth) return null;
    if (auth.role === "admin") return auth;
    const grants = auth.grants || [];
    const hasAccess = grants.some(
      (g) => g.client_slug === clientSlug && g.task_slug === taskSlug
    );
    if (!hasAccess) {
      window.location.href = "/dashboard";
      return null;
    }
    return auth;
  },

  /**
   * requireTaskAccess() for the page currently being viewed — reads
   * (clientSlug, taskSlug) straight off the URL, since every task page is
   * served at /app/<client_slug>/<task_slug>/... . Lets each task template
   * gate itself without the server having to pass its own slugs into the
   * page (which would mean touching every task.py + template pair instead
   * of just this one place).
   */
  requireCurrentTaskAccess() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    // parts = ["app", "<client_slug>", "<task_slug>", ...]
    const [, clientSlug, taskSlug] = parts;
    return this.requireTaskAccess(clientSlug, taskSlug);
  },

  /**
   * Same check as requireCurrentTaskAccess(), but meant to run from <head>
   * — before the body has parsed — instead of at the bottom of the page.
   * Touches no DOM (no mountUserMenu call, since document.body doesn't
   * exist yet this early), just the redirect. Called this early, an
   * unauthorized visitor's browser starts navigating away before the task
   * page's own markup ever paints, instead of flashing it for a moment
   * and then bouncing (which is what happened when this check only ran in
   * a <script> at the bottom of body, after the whole page had rendered).
   * Uses location.replace() so the blocked page never lands in history.
   */
  guardCurrentTaskPage() {
    const parts = window.location.pathname.split("/").filter(Boolean);
    const [, clientSlug, taskSlug] = parts;
    const auth = this.get();
    if (!auth || !auth.token) {
      window.location.replace("/login");
      return false;
    }
    if (auth.role !== "admin") {
      const grants = auth.grants || [];
      const hasAccess = grants.some(
        (g) => g.client_slug === clientSlug && g.task_slug === taskSlug
      );
      if (!hasAccess) {
        window.location.replace("/dashboard");
        return false;
      }
    }
    return true;
  },

  /** Floating top-right account icon — click to see name/username + log out. */
  mountUserMenu(auth) {
    if (document.getElementById("vma-user-menu")) return;

    const style = document.createElement("style");
    style.textContent = `
      #vma-user-menu { position: fixed; top: 16px; right: 16px; z-index: 1000;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
      #vma-user-btn { width: 36px; height: 36px; border-radius: 50%;
        background: #171a21; border: 1px solid #262a33; color: #8b93a1;
        display: flex; align-items: center; justify-content: center;
        cursor: pointer; padding: 0; transition: border-color .15s, color .15s; }
      #vma-user-btn:hover { border-color: #4f7cff; color: #e8eaed; }
      #vma-user-dropdown { display: none; position: absolute; top: 44px; right: 0;
        min-width: 190px; background: #171a21; border: 1px solid #262a33;
        border-radius: 10px; padding: 12px 14px; box-shadow: 0 8px 24px rgba(0,0,0,.4); }
      #vma-user-dropdown.open { display: block; }
      #vma-user-dropdown .vma-name { color: #e8eaed; font-size: 13px; font-weight: 600; }
      #vma-user-dropdown .vma-username { color: #8b93a1; font-size: 12px; margin-top: 2px; }
      #vma-user-dropdown hr { border: none; border-top: 1px solid #262a33; margin: 10px 0; }
      #vma-logout-btn { width: 100%; background: transparent; border: 1px solid #2a2f3a;
        color: #8b93a1; border-radius: 7px; padding: 7px 10px; font-size: 12.5px;
        cursor: pointer; font-family: inherit; transition: border-color .15s, color .15s; }
      #vma-logout-btn:hover { border-color: #ff6b6b; color: #ff6b6b; }
    `;
    document.head.appendChild(style);

    const wrap = document.createElement("div");
    wrap.id = "vma-user-menu";
    wrap.innerHTML = `
      <button id="vma-user-btn" type="button" aria-label="Account menu">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
          stroke-linecap="round" stroke-linejoin="round" width="18" height="18">
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
          <circle cx="12" cy="7" r="4"></circle>
        </svg>
      </button>
      <div id="vma-user-dropdown">
        <div class="vma-name">${auth.name || ""}</div>
        <div class="vma-username">@${auth.username || ""}</div>
        <hr>
        <button id="vma-logout-btn" type="button">Log out</button>
      </div>
    `;
    document.body.appendChild(wrap);

    const btn = document.getElementById("vma-user-btn");
    const dropdown = document.getElementById("vma-user-dropdown");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      dropdown.classList.toggle("open");
    });
    document.addEventListener("click", (e) => {
      if (!wrap.contains(e.target)) dropdown.classList.remove("open");
    });
    document.getElementById("vma-logout-btn").addEventListener("click", () => this.logout());
  },

  /**
   * Left-side slide-in drawer listing every client/task this account is
   * granted, so a user with several tasks can switch between them from
   * WITHIN a task page instead of having to navigate back to /dashboard
   * every time. Starts closed. Only mounted when there's actually more
   * than one grant to switch between — a single-grant account never sees
   * the picker at /dashboard either (auth/dashboard.js redirects it
   * straight through), so a switcher with nothing else to switch to would
   * just be a dead button here. `auth.grants` here is the LOGIN RESPONSE's
   * grants list (client_name/task_name included), not the JWT payload's
   * own embedded grants (slugs only) — see routes/auth_routes.py.
   */
  mountTaskDrawer(auth) {
    if (document.getElementById("vma-task-drawer")) return;
    const currentPath = window.location.pathname;
    // Only real task pages (/app/<client_slug>/<task_slug>/...) get the
    // switcher — never /dashboard (it already IS the switcher) or /admin
    // (its own nav covers this).
    if (!currentPath.startsWith("/app/")) return;
    const grants = auth.grants || [];
    if (grants.length < 2) return;

    const style = document.createElement("style");
    style.textContent = `
      #vma-drawer-toggle { position: fixed; top: 16px; left: 16px; z-index: 1000;
        width: 36px; height: 36px; border-radius: 8px; background: #171a21;
        border: 1px solid #262a33; color: #8b93a1; display: flex; align-items: center;
        justify-content: center; cursor: pointer; padding: 0;
        transition: border-color .15s, color .15s;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
      #vma-drawer-toggle:hover { border-color: #4f7cff; color: #e8eaed; }
      #vma-drawer-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.5);
        z-index: 998; opacity: 0; pointer-events: none; transition: opacity .15s; }
      #vma-drawer-overlay.open { opacity: 1; pointer-events: auto; }
      #vma-task-drawer { position: fixed; top: 0; left: 0; bottom: 0; width: 280px;
        max-width: 82vw; background: #12141a; border-right: 1px solid #262a33;
        z-index: 999; transform: translateX(-100%); transition: transform .2s ease;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        display: flex; flex-direction: column; padding: 64px 0 16px; overflow-y: auto; }
      #vma-task-drawer.open { transform: translateX(0); }
      #vma-task-drawer h2 { font-size: 11px; text-transform: uppercase; letter-spacing: .6px;
        color: #8b93a1; padding: 0 20px 12px; margin: 0; }
      #vma-task-drawer a { display: block; text-decoration: none; color: inherit;
        padding: 12px 20px; border-left: 3px solid transparent; }
      #vma-task-drawer a:hover { background: #171a21; }
      #vma-task-drawer a.active { border-left-color: #4f7cff; background: #171a21; }
      #vma-task-drawer .vma-client { font-size: 14px; font-weight: 600; color: #e8eaed; }
      #vma-task-drawer .vma-task { font-family: 'Consolas', 'DM Mono', monospace;
        font-size: 11px; color: #8b93a1; margin-top: 2px; }
    `;
    document.head.appendChild(style);

    const overlay = document.createElement("div");
    overlay.id = "vma-drawer-overlay";
    document.body.appendChild(overlay);

    const toggle = document.createElement("button");
    toggle.id = "vma-drawer-toggle";
    toggle.type = "button";
    toggle.setAttribute("aria-label", "Switch task");
    toggle.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round" width="18" height="18">
        <line x1="3" y1="6" x2="21" y2="6"></line>
        <line x1="3" y1="12" x2="21" y2="12"></line>
        <line x1="3" y1="18" x2="21" y2="18"></line>
      </svg>`;
    document.body.appendChild(toggle);

    const drawer = document.createElement("div");
    drawer.id = "vma-task-drawer";
    drawer.innerHTML = `
      <h2>Switch task</h2>
      ${grants.map((g) => {
        const href = `/app/${g.client_slug}/${g.task_slug}/`;
        const isActive = currentPath.startsWith(`/app/${g.client_slug}/${g.task_slug}`);
        return `<a href="${href}" class="${isActive ? "active" : ""}">
          <div class="vma-client">${g.client_name}</div>
          <div class="vma-task">${g.task_name}</div>
        </a>`;
      }).join("")}
    `;
    document.body.appendChild(drawer);

    const closeDrawer = () => {
      drawer.classList.remove("open");
      overlay.classList.remove("open");
    };
    const openDrawer = () => {
      drawer.classList.add("open");
      overlay.classList.add("open");
    };

    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      if (drawer.classList.contains("open")) closeDrawer();
      else openDrawer();
    });
    overlay.addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeDrawer();
    });
  },

  /** fetch() wrapper that attaches the Authorization header and handles 401s. */
  async authFetch(url, options = {}) {
    const auth = this.get();
    const headers = Object.assign({}, options.headers || {}, {
      Authorization: `Bearer ${auth ? auth.token : ""}`,
    });
    const res = await fetch(url, Object.assign({}, options, { headers }));
    if (res.status === 401) {
      this.logout();
      throw new Error("Session expired");
    }
    return res;
  },

  /**
   * Downloads a protected file. A plain <a href> can't carry the
   * Authorization header, so this fetches the bytes with authFetch and
   * triggers a save via a throwaway object URL instead.
   */
  async downloadFile(url, filename) {
    const res = await this.authFetch(url);
    if (!res.ok) throw new Error("Download failed");
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(objectUrl);
  },
};
