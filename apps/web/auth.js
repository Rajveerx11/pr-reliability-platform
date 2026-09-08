"use strict";

// Only a CSRF token enters JavaScript memory. Authentication stays in HttpOnly cookies.
window.reviewerSession = (() => {
  let current = null;
  async function session() {
    const response = await fetch("/auth/session", {credentials: "same-origin", cache: "no-store"});
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) {
        current = null;
        document.dispatchEvent(new Event("reviewer:signed-out"));
      }
      throw new Error(response.status === 503
        ? "GitHub access check unavailable. Try again shortly."
        : "Session expired or access removed. Sign in with GitHub.");
    }
    current = await response.json();
    return current;
  }
  async function mutate(path, options = {}) {
    const identity = await session();
    return fetch(path, {...options, credentials: "same-origin", cache: "no-store",
      headers: {...options.headers, "X-CSRF-Token": identity.csrf_token}});
  }
  async function start(onReady, onError) {
    const login = document.getElementById("github-login");
    const logout = document.getElementById("logout");
    const name = document.getElementById("reviewer-name");
    const signedOut = () => {
      login.hidden = false; logout.hidden = true;
      name.textContent = "Sign in to view your repositories.";
    };
    document.addEventListener("reviewer:signed-out", signedOut);
    logout.addEventListener("click", async () => {
      try {
        // The last CSRF token permits logout even if GitHub itself is unavailable.
        const response = await fetch("/auth/logout", {method: "POST", credentials: "same-origin",
          headers: {"X-CSRF-Token": current?.csrf_token || ""}});
        if (!response.ok && response.status !== 401) throw new Error("Logout failed. Reload and retry.");
        location.replace("/dashboard");
      } catch (error) { onError(error); }
    });
    if (new URLSearchParams(location.search).get("login") === "failed") {
      history.replaceState(null, "", location.pathname);
      signedOut(); onError(new Error("GitHub login failed or access was denied. Try again."));
      return;
    }
    try {
      // Local session proof allows sign-out even before GitHub recovers from an outage.
      const local = await fetch("/auth/logout-token", {credentials: "same-origin", cache: "no-store"});
      if (local.ok) {
        current = await local.json();
        logout.hidden = false;
      }
      const identity = await session();
      login.hidden = true; logout.hidden = false;
      name.textContent = `${identity.login} · ${identity.role}`;
      await onReady(identity);
    } catch (error) { if (!current) signedOut(); onError(error); }
    setInterval(async () => {
      if (!current) return;
      try {
        const response = await mutate("/auth/session/rotate", {method: "POST"});
        if (!response.ok && response.status !== 401) throw new Error("Session rotation failed.");
        await session(); // Also recovers a concurrent rotation by another tab.
      } catch (error) { onError(error); }
    }, 15 * 60 * 1000);
  }
  return {session, mutate, start};
})();
