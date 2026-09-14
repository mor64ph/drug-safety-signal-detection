/* reportscope — the only site-wide script.
 *
 * Kept in a file rather than inline so that script-src can eventually drop
 * 'unsafe-inline'; the per-page <script> blocks are what still require it.
 *
 * Two jobs:
 *
 *  1. Acknowledge a form submission. A drug lookup scans the whole scored
 *     table and a sleeping free-tier instance has to wake up first, so there
 *     can be several seconds between the click and the next paint. Without
 *     feedback the button reads as dead and gets pressed again.
 *
 *  2. Run the theme toggle. Note what is *not* here: choosing the theme on
 *     load. That happens in a synchronous inline script in the page head,
 *     because by the time this file executes the first paint has already
 *     happened and switching now would be a visible flash.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- theme */

  var THEME_KEY = "reportscope-theme";

  function currentTheme() {
    return document.documentElement.getAttribute("data-bs-theme") === "dark"
      ? "dark" : "light";
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-bs-theme", theme);
    var buttons = document.querySelectorAll("[data-theme-toggle]");
    for (var i = 0; i < buttons.length; i++) {
      // The visible label is swapped by CSS off the same attribute. This is
      // only the accessible name, which CSS cannot set.
      buttons[i].setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      buttons[i].setAttribute(
        "aria-label",
        theme === "dark" ? "Switch to the light theme"
                         : "Switch to the dark theme");
    }
  }

  function initTheme() {
    applyTheme(currentTheme());

    document.addEventListener("click", function (event) {
      var button = event.target.closest && event.target.closest("[data-theme-toggle]");
      if (!button) return;
      var next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        // An explicit choice outranks the OS preference from here on.
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* Private mode. The theme still applies for this page. */
      }
    });

    // Follow the OS while the reader has not expressed a preference of their
    // own. Once they have, their choice is stored and this stops applying.
    var query = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
    if (query && query.addEventListener) {
      query.addEventListener("change", function (event) {
        var saved = null;
        try {
          saved = localStorage.getItem(THEME_KEY);
        } catch (e) { /* ignore */ }
        if (!saved) applyTheme(event.matches ? "dark" : "light");
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTheme);
  } else {
    initTheme();
  }

  /* ------------------------------------------------------- pending state */

  var PENDING_LABEL = "Working…";

  function onSubmit(event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    // Let the browser's own validation win: an invalid form fires submit but
    // never navigates, and marking it busy would strand the button.
    if (form.noValidate === false && form.checkValidity && !form.checkValidity()) {
      return;
    }

    if (form.getAttribute("aria-busy") === "true") {
      // Second submit of a form already in flight.
      event.preventDefault();
      return;
    }
    form.setAttribute("aria-busy", "true");

    var button = form.querySelector('button[type="submit"], input[type="submit"]');
    if (!button) return;

    // Never disable it. A disabled submit is omitted from the payload, so a
    // form distinguishing two buttons by name would lose which one was used.
    if (button.tagName === "BUTTON" && button.dataset.pendingLabel !== "off") {
      button.dataset.idleLabel = button.innerHTML;
      button.textContent = button.dataset.pendingLabel || PENDING_LABEL;
    }
  }

  function reset() {
    var forms = document.querySelectorAll('form[aria-busy="true"]');
    for (var i = 0; i < forms.length; i++) {
      var form = forms[i];
      form.removeAttribute("aria-busy");
      var button = form.querySelector('button[type="submit"]');
      if (button && button.dataset.idleLabel !== undefined) {
        button.innerHTML = button.dataset.idleLabel;
      }
    }
  }

  document.addEventListener("submit", onSubmit, true);

  // Coming back via the back button restores the page from the bfcache with the
  // DOM exactly as it was left, so a button parked on "Working..." would stay
  // that way. pageshow fires on both a fresh load and a cache restore.
  window.addEventListener("pageshow", reset);
})();
