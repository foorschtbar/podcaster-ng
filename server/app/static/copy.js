// Copy buttons and select-on-click for read-only fields (e.g. the worker token).
// Works without inline scripts (CSP); the text is always also selectable by hand.
(function () {
  function textOf(el) {
    return el.value !== undefined ? el.value : el.textContent;
  }

  function fallbackCopy(el) {
    // Older browsers and non-secure contexts: select the text and use the legacy command.
    var range;
    if (el.select) {
      el.focus();
      el.select();
      el.setSelectionRange(0, el.value.length);
    } else {
      range = document.createRange();
      range.selectNodeContents(el);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
    }
    try { return document.execCommand("copy"); } catch (e) { return false; }
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    var label = button.textContent;
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-copy"));
      if (!target) return;
      var done = function (ok) {
        button.textContent = ok ? "Copied ✓" : "Select it and press Ctrl+C";
        setTimeout(function () { button.textContent = label; }, 2500);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(textOf(target)).then(function () { done(true); },
          function () { done(fallbackCopy(target)); });
      } else {
        done(fallbackCopy(target));
      }
    });
  });

  document.querySelectorAll("input[data-select-all]").forEach(function (input) {
    var select = function () { input.select(); };
    input.addEventListener("focus", select);
    input.addEventListener("click", select);
  });
})();
