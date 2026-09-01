/* Theme toggle, table sorting/filtering, and the chart hover layer.
   No dependencies, no build step - this file is served as-is. */

(function () {
  "use strict";

  /* ------------------------------------------------------------- theme */
  var root = document.documentElement;
  var STORAGE_KEY = "cfb-elo-theme";

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  function currentTheme() {
    return root.getAttribute("data-theme") || (systemPrefersDark() ? "dark" : "light");
  }

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    document.querySelectorAll("[data-theme-label]").forEach(function (el) {
      el.textContent = theme === "dark" ? "Light" : "Dark";
    });
  }

  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "dark" || saved === "light") applyTheme(saved);
    else applyTheme(currentTheme());
  } catch (e) {
    applyTheme(currentTheme());
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      try {
        localStorage.setItem(STORAGE_KEY, next);
      } catch (e) {
        /* private browsing - the toggle still works for this page view */
      }
    });
  });

  /* ------------------------------------------------------ table sorting */
  var table = document.getElementById("rankings-table");

  function cellValue(row, index, kind) {
    var cell = row.cells[index];
    if (!cell) return kind === "num" ? 0 : "";
    if (kind === "num") {
      var raw = cell.dataset.value;
      if (raw === undefined) raw = cell.textContent.replace(/[^0-9.\-]/g, "");
      var n = parseFloat(raw);
      return isNaN(n) ? 0 : n;
    }
    return cell.textContent.trim().toLowerCase();
  }

  if (table) {
    var headers = Array.prototype.slice.call(table.tHead.rows[0].cells);
    headers.forEach(function (th, index) {
      if (!th.dataset.sort) return;
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");

      function sort() {
        var kind = th.dataset.sort;
        var currently = th.getAttribute("aria-sort");
        var dir;
        if (currently === "ascending") dir = "descending";
        else if (currently === "descending") dir = "ascending";
        else dir = th.dataset.default === "desc" ? "descending" : "ascending";

        headers.forEach(function (other) { other.removeAttribute("aria-sort"); });
        th.setAttribute("aria-sort", dir);

        var sign = dir === "ascending" ? 1 : -1;
        var body = table.tBodies[0];
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var av = cellValue(a, index, kind);
          var bv = cellValue(b, index, kind);
          if (av < bv) return -1 * sign;
          if (av > bv) return 1 * sign;
          return 0;
        });
        var frag = document.createDocumentFragment();
        rows.forEach(function (r) { frag.appendChild(r); });
        body.appendChild(frag);
      }

      th.addEventListener("click", sort);
      th.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sort();
        }
      });
    });
  }

  /* ---------------------------------------------------- table filtering */
  var search = document.getElementById("team-search");
  var confFilter = document.getElementById("conf-filter");
  var emptyMsg = document.querySelector(".empty-msg");

  function applyFilters() {
    if (!table) return;
    var term = (search && search.value ? search.value : "").trim().toLowerCase();
    var conf = confFilter ? confFilter.value : "";
    var visible = 0;

    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      var matchesTeam = !term || (row.dataset.team || "").indexOf(term) !== -1;
      var matchesConf = !conf || row.dataset.conference === conf;
      var show = matchesTeam && matchesConf;
      row.hidden = !show;
      if (show) visible++;
    });

    if (emptyMsg) emptyMsg.hidden = visible !== 0;
  }

  if (search) search.addEventListener("input", applyFilters);
  if (confFilter) confFilter.addEventListener("change", applyFilters);

  /* --------------------------------------------------------- team page */
  var teamPage = document.getElementById("team-page");

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function pct(v) { return Math.round(v * 100) + "%"; }
  function signed(v) { return (v >= 0 ? "+" : "") + v.toFixed(1); }
  function cls(v) { return v > 0 ? "up" : (v < 0 ? "down" : "muted"); }

  function resumeMarkup(game, emptyText) {
    if (!game) return '<p class="muted">' + esc(emptyText) + "</p>";
    return (
      '<p class="resume">' +
      "<strong>" + esc(game.opponent) + "</strong>" +
      '<span class="muted">' + esc(game.score) + " &middot; " + esc(game.weekLabel) + "</span>" +
      '<span class="chip">opponent Elo ' + Math.round(game.opponentRating).toLocaleString() + "</span>" +
      "</p>"
    );
  }

  function drawChart(svg, chart, caption) {
    svg.setAttribute("viewBox", "0 0 " + chart.width + " " + chart.height);
    var right = chart.width - 16;
    var parts = [];

    chart.yTicks.forEach(function (tick) {
      parts.push('<line class="grid" x1="48" x2="' + right + '" y1="' + tick.y + '" y2="' + tick.y + '"></line>');
      parts.push('<text class="axis-label" x="40" y="' + (tick.y + 4) + '" text-anchor="end">' + esc(tick.label) + "</text>");
    });
    if (chart.baselineY) {
      parts.push('<line class="baseline" x1="48" x2="' + right + '" y1="' + chart.baselineY + '" y2="' + chart.baselineY + '"></line>');
      parts.push('<text class="axis-note" x="' + (chart.width - 18) + '" y="' + (chart.baselineY - 6) + '" text-anchor="end">average team</text>');
    }
    parts.push('<path class="series-area" d="' + esc(chart.area) + '"></path>');
    parts.push('<path class="series-line" d="' + esc(chart.path) + '"></path>');
    chart.points.forEach(function (p, i) {
      var last = i === chart.points.length - 1;
      parts.push(
        '<circle class="series-dot' + (last ? " is-last" : "") + '" cx="' + p.x + '" cy="' + p.y +
        '" r="' + (last ? 4.5 : 3) + '" data-label="' + esc(p.label) + '" data-value="' + p.value + '"></circle>'
      );
    });
    parts.push('<line class="crosshair" y1="16" y2="' + (chart.height - 28) + '" hidden></line>');

    svg.innerHTML = parts.join("");
    var first = chart.points[0];
    var last = chart.points[chart.points.length - 1];
    svg.setAttribute("aria-label",
      "Elo rating by week, from " + first.value + " in the " + first.label.toLowerCase() +
      " to " + last.value + " now.");
    caption.innerHTML =
      esc(first.label) + " (" + first.value + ") &rarr; " + esc(last.label) + " (" + last.value +
      "). Hover for any week.";
  }

  function renderTeam(team) {
    document.title = team.school + " · " + document.title.split("·").pop().trim();

    var logo = teamPage.querySelector("[data-team-logo]");
    if (team.logo) {
      logo.src = team.logo;
      logo.hidden = false;
    }
    teamPage.querySelector("[data-team-name]").textContent = team.school;
    teamPage.querySelector("[data-team-meta]").textContent =
      team.conference + " · " + team.record + " overall · " + team.conferenceRecord + " in conference";

    teamPage.querySelector("[data-team-rank]").textContent = "#" + team.rank;
    var move = teamPage.querySelector("[data-team-rank-move]");
    if (team.rankChange) {
      move.className = team.rankChange > 0 ? "up" : "down";
      move.textContent = (team.rankChange > 0 ? "▲ " : "▼ ") + Math.abs(team.rankChange) + " this week";
    }

    teamPage.querySelector("[data-team-rating]").textContent = Math.round(team.rating).toLocaleString();
    teamPage.querySelector("[data-team-rating-label]").textContent =
      "Elo rating (" + signed(team.ratingChange) + " on the season)";
    teamPage.querySelector("[data-team-sos-rank]").textContent = "#" + team.sosRank;
    teamPage.querySelector("[data-team-sos-label]").textContent =
      "Strength of schedule (" + Math.round(team.sos).toLocaleString() + " avg opponent)";

    if (team.chart) {
      var panel = teamPage.querySelector("[data-chart-panel]");
      drawChart(panel.querySelector("[data-chart]"), team.chart,
                panel.querySelector("[data-chart-caption]"));
      panel.hidden = false;
    }

    teamPage.querySelector("[data-best-win]").innerHTML = resumeMarkup(team.bestWin, "No wins yet.");
    teamPage.querySelector("[data-worst-loss]").innerHTML = resumeMarkup(team.worstLoss, "Undefeated.");

    var body = teamPage.querySelector("[data-gamelog]");
    if (!team.games.length) {
      teamPage.querySelector("[data-gamelog-wrap]").hidden = true;
      teamPage.querySelector("[data-gamelog-empty]").hidden = false;
    } else {
      body.innerHTML = team.games.map(function (g) {
        var opponent = g.opponentSlug
          ? '<a href="team.html?t=' + esc(g.opponentSlug) + '">' + esc(g.opponent) + "</a>"
          : esc(g.opponent);
        return (
          "<tr>" +
          '<td class="muted">' + esc(g.weekLabel) + "</td>" +
          "<td>" +
            '<span class="muted loc">' + esc(g.prefix) + "</span> " + opponent +
            (g.neutral ? ' <span class="chip">neutral</span>' : "") +
            (g.upset ? ' <span class="chip upset">upset</span>' : "") +
          "</td>" +
          "<td>" +
            '<span class="result ' + (g.won ? "win" : (g.tied ? "tie" : "loss")) + '">' + esc(g.result) + "</span> " +
            esc(g.score) +
          "</td>" +
          '<td class="num muted">' + pct(g.winProbability) + "</td>" +
          '<td class="num"><span class="' + cls(g.eloChange) + '">' + signed(g.eloChange) + "</span></td>" +
          "</tr>"
        );
      }).join("");
    }

    teamPage.hidden = false;
    document.getElementById("team-loading").hidden = true;
    attachChartHover(teamPage);
  }

  if (teamPage) {
    var slug = new URLSearchParams(window.location.search).get("t");
    var missing = document.getElementById("team-missing");
    var loading = document.getElementById("team-loading");

    fetch(teamPage.dataset.teamsUrl)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        var team = data.teams[slug];
        if (!team) {
          loading.hidden = true;
          missing.hidden = false;
          return;
        }
        renderTeam(team);
      })
      .catch(function (err) {
        loading.hidden = true;
        missing.hidden = false;
        missing.querySelector("h2").textContent = "Couldn't load team data";
        if (window.console) console.error("teams.json failed to load:", err);
      });
  }

  /* -------------------------------------------------------- chart hover */
  function attachChartHover(root) {
  (root || document).querySelectorAll(".chart-figure").forEach(function (figure) {
    var svg = figure.querySelector(".rating-chart");
    var tooltip = figure.querySelector(".chart-tooltip");
    if (!svg || !tooltip) return;

    var dots = Array.prototype.slice.call(svg.querySelectorAll(".series-dot"));
    var crosshair = svg.querySelector(".crosshair");
    if (!dots.length) return;

    var viewBox = svg.viewBox.baseVal;

    function clear() {
      dots.forEach(function (d) { d.classList.remove("is-active"); });
      if (crosshair) crosshair.hidden = true;
      tooltip.hidden = true;
    }

    function move(event) {
      var rect = svg.getBoundingClientRect();
      var clientX = event.touches ? event.touches[0].clientX : event.clientX;
      // Map the pointer from screen pixels into viewBox units.
      var vx = ((clientX - rect.left) / rect.width) * viewBox.width;

      var nearest = dots[0];
      var best = Infinity;
      dots.forEach(function (dot) {
        var distance = Math.abs(parseFloat(dot.getAttribute("cx")) - vx);
        if (distance < best) {
          best = distance;
          nearest = dot;
        }
      });

      dots.forEach(function (d) { d.classList.remove("is-active"); });
      nearest.classList.add("is-active");

      var cx = parseFloat(nearest.getAttribute("cx"));
      var cy = parseFloat(nearest.getAttribute("cy"));
      if (crosshair) {
        crosshair.setAttribute("x1", cx);
        crosshair.setAttribute("x2", cx);
        crosshair.hidden = false;
      }

      tooltip.innerHTML =
        "<strong>" + nearest.dataset.value + "</strong>" +
        '<span class="muted">' + nearest.dataset.label + "</span>";
      tooltip.hidden = false;
      tooltip.style.left = (cx / viewBox.width) * rect.width + "px";
      tooltip.style.top = (cy / viewBox.height) * rect.height + "px";
    }

    svg.addEventListener("mousemove", move);
    svg.addEventListener("touchmove", move, { passive: true });
    svg.addEventListener("mouseleave", clear);
    svg.addEventListener("touchend", clear);
  });
  }

  // Charts rendered straight into the HTML (none today, but harmless) get
  // their hover layer at load; the team page attaches its own after render.
  attachChartHover(document);
})();
