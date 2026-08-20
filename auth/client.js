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
