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
    if (!document.getElementById("logout-btn")) {
      this.mountUserMenu(auth);
    }
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
