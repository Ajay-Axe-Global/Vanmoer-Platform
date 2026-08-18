(function () {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("error");
  const submitBtn = document.getElementById("submit-btn");

  // Bfcache restores (e.g. navigating back to /login after logout) can
  // resurrect the DOM exactly as it was left mid-submit — button stuck on
  // "Signing in…" — since we never got the chance to reset it before the
  // page navigated away. Reset on every show, cached or not.
  window.addEventListener("pageshow", () => {
    submitBtn.disabled = false;
    submitBtn.textContent = "Sign in";
    errorBox.style.display = "none";
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorBox.style.display = "none";
    submitBtn.disabled = true;
    submitBtn.textContent = "Signing in…";

    const username = document.getElementById("username").value.trim();
    const password = document.getElementById("password").value;

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.error || "Login failed");
      }

      localStorage.setItem("vanmoer_auth", JSON.stringify(data));

      const grants = data.grants || [];
      if (data.role === "admin") {
        window.location.href = "/admin";
      } else if (grants.length === 1) {
        // Single grant — skip the picker and go straight to the task, same as before.
        window.location.href = `/app/${grants[0].client_slug}/${grants[0].task_slug}/`;
      } else if (grants.length > 1) {
        // Multiple grants (e.g. Carpenter Inbound + Outbound) — let them pick.
        window.location.href = "/dashboard";
      } else {
        throw new Error("This account has no client/task assigned. Contact an admin.");
      }
    } catch (err) {
      errorBox.textContent = err.message;
      errorBox.style.display = "block";
      submitBtn.disabled = false;
      submitBtn.textContent = "Sign in";
    }
  });
})();
