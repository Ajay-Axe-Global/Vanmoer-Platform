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
    return auth;
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
