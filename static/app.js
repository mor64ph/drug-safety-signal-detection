/* reportscope — the only site-wide script.
 *
 * Kept in a file rather than inline so that script-src can eventually drop
 * 'unsafe-inline'; the per-page <script> blocks are what still require it.
 *
 * One job: acknowledge a form submission. A drug lookup scans the whole scored
 * table and a sleeping free-tier instance has to wake up first, so there can be
 * several seconds between the click and the next paint. Without feedback the
 * button reads as dead and gets pressed again.
 */
(function () {
  "use strict";

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
