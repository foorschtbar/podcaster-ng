// Hover / touch / keyboard tooltips for the server-rendered column charts.
// All text is inserted with textContent; the page works without this script (table view below the charts).
(function () {
  function init(svg) {
    var data = JSON.parse(svg.getAttribute("data-points"));
    var unit = svg.getAttribute("data-unit");
    var left = +svg.getAttribute("data-left");
    var plotW = +svg.getAttribute("data-plot-width");
    var viewW = +svg.getAttribute("data-view-width");
    var wrap = svg.parentNode;
    var tip = wrap.querySelector(".tip");
    var cursor = svg.querySelector(".cursor");
    var bars = svg.querySelectorAll(".bar");
    var n = data.values.length, slot = plotW / n, current = -1;

    function hide() {
      current = -1;
      tip.hidden = true;
      cursor.classList.remove("on");
      bars.forEach(function (b) { b.classList.remove("on"); });
    }

    function show(i) {
      i = Math.max(0, Math.min(n - 1, i));
      if (i === current) return;
      current = i;
      var x = left + i * slot + slot / 2;
      cursor.setAttribute("x1", x);
      cursor.setAttribute("x2", x);
      cursor.classList.add("on");
      bars.forEach(function (b) { b.classList.toggle("on", +b.getAttribute("data-i") === i); });
      tip.textContent = "";
      var value = document.createElement("strong");
      value.textContent = data.values[i].toLocaleString();
      var label = document.createElement("span");
      label.textContent = " " + unit + " · " + new Date(data.days[i] + "T00:00:00Z").toLocaleDateString(undefined,
        { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
      tip.appendChild(value);
      tip.appendChild(label);
      tip.hidden = false;
      var px = x * svg.getBoundingClientRect().width / viewW;
      var half = tip.offsetWidth / 2;
      var max = wrap.clientWidth - tip.offsetWidth - 4;
      tip.style.left = Math.max(4, Math.min(max, svg.offsetLeft + px - half)) + "px";
    }

    function indexAt(ev) {
      var r = svg.getBoundingClientRect();
      return Math.floor(((ev.clientX - r.left) * viewW / r.width - left) / slot);
    }

    svg.addEventListener("pointermove", function (ev) { show(indexAt(ev)); });
    svg.addEventListener("pointerdown", function (ev) { show(indexAt(ev)); });
    svg.addEventListener("pointerleave", function (ev) { if (ev.pointerType === "mouse") hide(); });
    svg.addEventListener("focus", function () { show(current < 0 ? n - 1 : current); });
    svg.addEventListener("blur", hide);
    svg.addEventListener("keydown", function (ev) {
      if (ev.key === "ArrowLeft") { show((current < 0 ? n : current) - 1); ev.preventDefault(); }
      else if (ev.key === "ArrowRight") { show((current < 0 ? -1 : current) + 1); ev.preventDefault(); }
      else if (ev.key === "Escape") { hide(); }
    });
    document.addEventListener("pointerdown", function (ev) { if (!wrap.contains(ev.target)) hide(); });
  }
  document.querySelectorAll("svg.col-chart").forEach(init);
})();
