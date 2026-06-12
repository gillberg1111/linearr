/* Linearr UI runtime (v3.5.0).
 * Progressive enhancement only — every form works without this file.
 * 1. Toasts: frosted-glass notifications; server flash banners are converted
 *    into toasts on load (no-JS users keep the banners).
 * 2. AJAX forms: <form data-ajax> posts via fetch with X-Linearr-Ajax: 1;
 *    the server answers {ok, message, changed}. Spinner on the submitter,
 *    toast the message, reload iff changed (toast survives via sessionStorage).
 * 3. [data-timestamp] -> relative "2h ago" text.
 * 4. Home-page playlist search + type filter (#playlistFilter / .filter-pill).
 */
(function () {
  "use strict";

  /* ---------------- toasts ---------------- */
  var stack = null;
  function ensureStack() {
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    return stack;
  }

  function toast(message, category) {
    var el = document.createElement("div");
    el.className = "toast toast-" + (category === "error" ? "error" : "ok");
    var span = document.createElement("span");
    span.className = "toast-msg";
    span.textContent = message;
    var btn = document.createElement("button");
    btn.className = "toast-close";
    btn.type = "button";
    btn.setAttribute("aria-label", "Dismiss");
    btn.textContent = "\u00d7";
    btn.addEventListener("click", function () { dismiss(el); });
    el.appendChild(span);
    el.appendChild(btn);
    ensureStack().appendChild(el);
    var ttl = category === "error" ? 10000 : 5000;
    el._timer = setTimeout(function () { dismiss(el); }, ttl);
    return el;
  }

  function dismiss(el) {
    if (el._timer) clearTimeout(el._timer);
    el.classList.add("toast-out");
    setTimeout(function () { el.remove(); }, 250);
  }

  // Server-rendered flash banners -> toasts (keep banners for no-JS).
  function adoptFlashes() {
    var flashes = document.querySelectorAll(".flashes .flash");
    if (!flashes.length) return;
    flashes.forEach(function (f) {
      toast(f.textContent.trim(),
            f.classList.contains("flash-error") ? "error" : "ok");
    });
    var wrap = document.querySelector(".flashes");
    if (wrap) wrap.remove();
  }

  // Toast handoff across the post-action reload.
  var PENDING_KEY = "linearr.pendingToast";
  function showPendingToast() {
    var raw = sessionStorage.getItem(PENDING_KEY);
    if (!raw) return;
    sessionStorage.removeItem(PENDING_KEY);
    try {
      var p = JSON.parse(raw);
      toast(p.message, p.category);
    } catch (e) { /* ignore */ }
  }

  /* ---------------- AJAX forms ---------------- */
  function formData(form, submitter) {
    // The clicked button's name/value must be included — the pill toggles
    // depend on it. new FormData(form, submitter) does that natively
    // (Chrome 112+ / Safari 17+), but older browsers silently IGNORE the
    // second argument rather than throwing, so verify and append manually.
    var fd;
    try {
      fd = new FormData(form, submitter || undefined);
    } catch (e) {
      fd = new FormData(form);
    }
    if (submitter && submitter.name && !fd.has(submitter.name)) {
      fd.append(submitter.name, submitter.value);
    }
    return fd;
  }

  function wireAjaxForms() {
    document.addEventListener("submit", function (ev) {
      var form = ev.target;
      if (!form.matches || !form.matches("form[data-ajax]")) return;
      // Respect confirm() guards wired via onsubmit (they run before this
      // listener; if they cancelled, defaultPrevented is already true).
      if (ev.defaultPrevented) return;

      var submitter = ev.submitter;
      if (submitter === undefined) {
        // Browser doesn't report which button was clicked (Safari <15.4).
        // A multi-button pill form would post the wrong value — let those
        // submit natively (classic redirect + banner still works).
        var buttons = form.querySelectorAll("button[type=submit], button:not([type])");
        if (buttons.length > 1) return;
        submitter = buttons[0] || null;
      }
      ev.preventDefault();
      if (submitter) {
        submitter.classList.add("is-busy");
        submitter.disabled = true;
      }

      fetch(form.action, {
        method: "POST",
        body: formData(form, submitter),
        headers: { "X-Linearr-Ajax": "1" },
        credentials: "same-origin",
      })
        .then(function (resp) {
          var ct = resp.headers.get("Content-Type") || "";
          if (ct.indexOf("application/json") === -1) {
            // Session expired (redirect chain landed on the login page) or
            // an unexpected response: a plain reload routes the user right.
            window.location.reload();
            return null;
          }
          return resp.json();
        })
        .then(function (data) {
          if (!data) return;
          if (data.ok && data.changed) {
            sessionStorage.setItem(PENDING_KEY, JSON.stringify({
              message: data.message, category: "ok",
            }));
            window.location.reload();
            return;
          }
          toast(data.message, data.ok ? "ok" : "error");
          if (submitter) {
            submitter.classList.remove("is-busy");
            submitter.disabled = false;
          }
        })
        .catch(function () {
          toast("Request failed — is the server reachable?", "error");
          if (submitter) {
            submitter.classList.remove("is-busy");
            submitter.disabled = false;
          }
        });
    });
  }

  /* ---------------- relative timestamps ---------------- */
  function renderTimestamps() {
    document.querySelectorAll("[data-timestamp]").forEach(function (el) {
      var iso = el.getAttribute("data-timestamp");
      var t = Date.parse(iso);
      if (isNaN(t)) return;
      var s = Math.max(0, (Date.now() - t) / 1000);
      var txt;
      if (s < 90) txt = "just now";
      else if (s < 5400) txt = Math.round(s / 60) + "m ago";
      else if (s < 129600) txt = Math.round(s / 3600) + "h ago";
      else txt = Math.round(s / 86400) + "d ago";
      el.textContent = txt;
      el.title = new Date(t).toLocaleString();
    });
  }

  /* ---------------- home-page filter ---------------- */
  function wireCardFilter() {
    var input = document.getElementById("playlistFilter");
    var pills = document.querySelectorAll(".filter-pill");
    if (!input && !pills.length) return;
    var activeType = "all";

    function apply() {
      var q = (input ? input.value : "").trim().toLowerCase();
      document.querySelectorAll(".playlist-card").forEach(function (card) {
        var hay = card.getAttribute("data-search") || "";
        var type = card.getAttribute("data-type") || "";
        var ok = (activeType === "all" || type === activeType) &&
                 (!q || hay.indexOf(q) !== -1);
        card.style.display = ok ? "" : "none";
      });
    }

    if (input) input.addEventListener("input", apply);
    pills.forEach(function (p) {
      p.addEventListener("click", function () {
        pills.forEach(function (x) { x.classList.remove("is-active"); });
        p.classList.add("is-active");
        activeType = p.getAttribute("data-type") || "all";
        apply();
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    adoptFlashes();
    showPendingToast();
    wireAjaxForms();
    renderTimestamps();
    wireCardFilter();
  });
})();
