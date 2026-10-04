/* Wall monitor dashboard. Plain JS + hand-drawn SVG, kept light for a Pi 2/3.
 *
 *   /?screen=1          rotates through dashboard.json screens["1"]
 *   /?view=app:cocina   pins one view (no rotation)
 *   ← / →  previous / next view, space pauses rotation
 */
(function () {
  "use strict";

  var params = new URLSearchParams(location.search);
  var state = {
    config: {}, payload: null, idx: 0, paused: false,
    pinned: params.get("view") || (location.pathname.indexOf("/app/") === 0 ? "app:" + location.pathname.slice(5) : null),
    screen: params.get("screen") || "1",
    rotateTimer: null, lastRenderKey: null
  };
  var $ = function (id) { return document.getElementById(id); };
  var STATE_LABEL = { up: "UP", degraded: "WARNING", down: "DOWN", unknown: "UNKNOWN" };
  var STATE_ICON = { up: "✓", degraded: "!", down: "✕", unknown: "?" };

  // ------------------------------------------------------------- formatting
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function compact(n) {
    if (n == null || isNaN(n)) return "–";
    var a = Math.abs(n);
    if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e4) return (n / 1e3).toFixed(0) + "K";
    if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
    if (a >= 100 || Number.isInteger(n)) return String(Math.round(n));
    return n.toFixed(1);
  }
  function ms(v) {
    if (v == null) return "–";
    return v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms";
  }
  function dur(s) {
    if (s == null) return "–";
    if (s < 90) return Math.round(s) + " s";
    if (s < 5400) return Math.round(s / 60) + " min";
    if (s < 172800) return Math.round(s / 3600) + " h";
    return Math.round(s / 86400) + " d";
  }
  function hhmm(ts) {
    var d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function now() { return Math.floor(Date.now() / 1000); }
  function host(url) { return String(url || "").replace(/^https?:\/\//, "").replace(/\/$/, ""); }

  function pill(st) {
    st = STATE_LABEL[st] ? st : "unknown";
    return '<span class="pill ' + st + '"><span class="ico">' + STATE_ICON[st] + "</span>" + STATE_LABEL[st] + "</span>";
  }
  function stat(label, value, unit, cls) {
    return '<div class="stat ' + (cls || "") + '"><div class="label">' + esc(label) + '</div><div class="value">' +
      esc(value) + (unit ? "<small>" + esc(unit) + "</small>" : "") + "</div></div>";
  }

  // ------------------------------------------------------------- charts
  // Charts are declared in HTML as <div class="chart" data-chart="id"> and drawn
  // after layout, when their pixel size is known.
  var pending = {};
  var chartSeq = 0;
  function chartSlot(spec) {
    var id = "c" + (++chartSeq);
    pending[id] = spec;
    return '<div class="chart" data-chart="' + id + '" style="height:100%"></div>';
  }
  function drawCharts() {
    Object.keys(pending).forEach(function (id) {
      var el = document.querySelector('[data-chart="' + id + '"]');
      if (el) draw(el, pending[id]);
    });
    pending = {};
  }

  function niceMax(v) {
    if (!v || v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(v)));
    var steps = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i++) if (steps[i] * p >= v) return steps[i] * p;
    return 10 * p;
  }

  // spec: {kind: "area"|"columns", points: [[ts, v], ...], color, fmt, stack: [[ts, v]], title, keys}
  function draw(el, spec) {
    var title = spec.title ? '<div class="chart-title">' + spec.title + "</div>" : "";
    el.innerHTML = title + '<div class="plot"></div>';
    var plot = el.lastChild;
    var W = plot.clientWidth, H = plot.clientHeight;
    if (W < 40 || H < 30) return;
    var rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
    var padL = 3.2 * rem, padB = 1.3 * rem, padT = 0.5 * rem, padR = 0.6 * rem;
    var pts = spec.points || [];
    if (!pts.length) { plot.innerHTML = '<div class="empty">' + esc(spec.empty || "No data yet") + "</div>"; return; }
    var vals = pts.map(function (p) { return p[1]; }).filter(function (v) { return v != null; });
    var max = niceMax(Math.max.apply(null, vals.concat([spec.minMax || 0])));
    var t0 = pts[0][0], t1 = pts[pts.length - 1][0];
    var step = pts.length > 1 ? (pts[1][0] - pts[0][0]) : 1;
    var iw = W - padL - padR, ih = H - padT - padB;
    var x = spec.kind === "columns"
      ? function (t) { return padL + ((t - t0) / step + 0.5) * (iw / pts.length); }
      : function (t) { return padL + (t1 === t0 ? iw / 2 : (t - t0) / (t1 - t0) * iw); };
    var y = function (v) { return padT + ih - (v / max) * ih; };
    var fmt = spec.fmt || compact;
    var svg = ['<svg width="' + W + '" height="' + H + '">'];

    [0, 0.5, 1].forEach(function (f) {
      var gy = y(max * f);
      svg.push('<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + gy + '" y2="' + gy + '" stroke="' +
        (f === 0 ? "var(--axis)" : "var(--grid)") + '" stroke-width="1"/>');
      svg.push('<text x="' + (padL - 0.5 * rem) + '" y="' + (gy + 0.25 * rem) + '" text-anchor="end">' + esc(fmt(max * f)) + "</text>");
    });

    // x labels: local-time boundaries, at most ~6 of them
    var span = t1 - t0;
    var every = span > 3 * 86400 ? 86400 : span > 36000 ? 6 * 3600 : span > 7200 ? 3600 : 900;
    var last = -Infinity;
    pts.forEach(function (p) {
      var d = new Date(p[0] * 1000);
      var secs = d.getHours() * 3600 + d.getMinutes() * 60;
      var hit = every === 86400 ? secs === 0 : secs % every === 0;
      var px = x(p[0]);
      if (hit && px - last > 4 * rem) {
        var label = every === 86400 ? d.getDate() + "/" + (d.getMonth() + 1) : hhmm(p[0]);
        svg.push('<text x="' + px + '" y="' + (H - 0.2 * rem) + '" text-anchor="middle">' + label + "</text>");
        last = px;
      }
    });

    if (spec.kind === "columns") {
      var slot = iw / pts.length;
      var bw = Math.max(1, Math.min(24, slot - 2));
      var r = Math.min(4, bw / 2);
      var stack = {};
      (spec.stack || []).forEach(function (p) { stack[p[0]] = p[1]; });
      pts.forEach(function (p) {
        var v = p[1] || 0, e = Math.min(stack[p[0]] || 0, v);
        var cx = x(p[0]) - bw / 2, base = y(0);
        if (e > 0) {
          var ey = y(e);
          svg.push('<rect x="' + cx + '" y="' + ey + '" width="' + bw + '" height="' + (base - ey) + '" fill="var(--critical)"/>');
          base = ey - 2;  // 2px surface gap between stacked segments
        }
        if (v - e > 0) {
          var top = y(v);
          if (base - top > 0.5) svg.push('<path d="' + roundedTop(cx, top, bw, base - top, r) + '" fill="' + spec.color + '"/>');
        }
      });
    } else {
      var segs = [], cur = [];
      pts.forEach(function (p) {
        if (p[1] == null) { if (cur.length) segs.push(cur); cur = []; } else cur.push([x(p[0]), y(p[1])]);
      });
      if (cur.length) segs.push(cur);
      segs.forEach(function (s) {
        var line = s.map(function (q, i) { return (i ? "L" : "M") + q[0].toFixed(1) + "," + q[1].toFixed(1); }).join("");
        if (spec.area !== false && s.length > 1) {
          svg.push('<path d="' + line + "L" + s[s.length - 1][0].toFixed(1) + "," + y(0) + "L" + s[0][0].toFixed(1) + "," + y(0) +
            'Z" fill="' + spec.color + '" fill-opacity="0.12"/>');
        }
        svg.push('<path d="' + line + '" fill="none" stroke="' + spec.color + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>');
      });
      var lastPt = segs.length ? segs[segs.length - 1].slice(-1)[0] : null;
      if (lastPt) {
        svg.push('<circle cx="' + lastPt[0] + '" cy="' + lastPt[1] + '" r="4.5" fill="' + spec.color + '" stroke="var(--surface)" stroke-width="2"/>');
      }
    }
    svg.push('<line class="cross" x1="0" x2="0" y1="' + padT + '" y2="' + (padT + ih) + '" stroke="var(--ink-2)" stroke-width="1" visibility="hidden"/>');
    svg.push("</svg>");
    plot.innerHTML = svg.join("");
    attachHover(plot, pts, x, spec, padL, iw);
  }

  function roundedTop(x, y, w, h, r) {
    r = Math.min(r, h);
    return "M" + x + "," + (y + h) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y +
      "H" + (x + w - r) + "Q" + (x + w) + "," + y + " " + (x + w) + "," + (y + r) + "V" + (y + h) + "Z";
  }

  // Crosshair + tooltip, for when the page is opened on a laptop. The kiosk has
  // no pointer, so this costs nothing there.
  function attachHover(plot, pts, x, spec, padL, iw) {
    var tip = $("tip"), cross = plot.querySelector(".cross");
    var stack = {};
    (spec.stack || []).forEach(function (p) { stack[p[0]] = p[1]; });
    plot.onmousemove = function (ev) {
      var rect = plot.getBoundingClientRect();
      var mx = ev.clientX - rect.left;
      var best = null, bd = Infinity;
      pts.forEach(function (p) { var d = Math.abs(x(p[0]) - mx); if (d < bd) { bd = d; best = p; } });
      if (!best) return;
      cross.setAttribute("x1", x(best[0])); cross.setAttribute("x2", x(best[0]));
      cross.setAttribute("visibility", "visible");
      var fmt = spec.tipFmt || spec.fmt || compact;
      var extra = spec.stack ? " · 5xx " + (stack[best[0]] || 0) : "";
      tip.innerHTML = "<b>" + esc(best[1] == null ? "–" : fmt(best[1])) + "</b> " + esc(spec.unit || "") + extra +
        '<div class="muted">' + hhmm(best[0]) + "</div>";
      tip.hidden = false;
      tip.style.left = (ev.clientX + 14) + "px";
      tip.style.top = (ev.clientY - 10) + "px";
    };
    plot.onmouseleave = function () { tip.hidden = true; cross.setAttribute("visibility", "hidden"); };
  }

  function seriesPoints(h, key) {
    if (!h || !h[key]) return [];
    return h[key].map(function (v, i) { return [h.start + i * h.step, v]; });
  }

  // ------------------------------------------------------------- top bar
  function meter(label, pct, detail) {
    if (pct == null) return "";
    var cls = pct >= 90 ? "crit" : pct >= 75 ? "warn" : "";
    return '<div class="meter ' + cls + '"><div class="row"><span>' + esc(label) + "</span><b>" + Math.round(pct) + "%" +
      (detail ? ' <span class="muted">' + esc(detail) + "</span>" : "") + '</b></div><div class="track"><div class="fill" style="width:' +
      Math.min(100, pct) + '%"></div></div></div>';
  }

  function renderHost(h) {
    if (!h) { $("host").innerHTML = ""; return; }
    var parts = [meter("CPU", h.cpu_pct), meter("Memory", h.mem && h.mem.used_pct)];
    (h.disks || []).forEach(function (d) {
      if (d.used_pct != null) parts.push(meter("Disk " + d.path, d.used_pct, d.free_gb + " GB free"));
    });
    if (h.load) parts.push('<div class="chip">Load <b class="num">' + h.load[0].toFixed(2) + "</b></div>");
    var dk = h.docker;
    if (dk) {
      if (!dk.ok) {
        parts.push('<div class="chip"><span class="dot unknown"></span>Docker: ' + esc(dk.error || "unavailable") + "</div>");
      } else {
        var stopped = dk.containers.filter(function (c) { return !c.running; });
        parts.push('<div class="chip"><span class="dot ' + (stopped.length ? "down" : "up") + '"></span>Docker ' +
          (dk.containers.length - stopped.length) + "/" + dk.containers.length + " running" +
          (stopped.length ? " · stopped: " + esc(stopped.map(function (c) { return c.name; }).join(", ")) : "") + "</div>");
      }
    }
    if (h.uptime_s != null) parts.push('<div class="chip muted">up ' + dur(h.uptime_s) + "</div>");
    $("host").innerHTML = parts.join("");
  }

  // ------------------------------------------------------------- overview
  function visibleMetrics(app) {
    var m = app.metrics && app.metrics.metrics;
    return (m || []).filter(function (x) { return !x.hidden; });
  }

  // Metrics the app marked `overview: true`; apps on gem 0.1 get their first numbers.
  function overviewMetrics(app, n) {
    var nums = visibleMetrics(app).filter(function (x) { return x.type === "number" && x.value != null; });
    var flagged = nums.filter(function (x) { return x.overview; });
    return (flagged.length ? flagged : nums).slice(0, n);
  }

  // Status marker for a metric over its threshold: icon + word, never color alone.
  function levelTag(m) {
    if (m.level !== "warning" && m.level !== "critical") return "";
    return ' <span class="lvl ' + m.level + '"><i>' + (m.level === "critical" ? "✕" : "!") + "</i>" +
      (m.level === "critical" ? "critical" : "warning") + "</span>";
  }

  function renderOverview(data) {
    var apps = data.apps || [];
    var cols = apps.length <= 1 ? 1 : apps.length <= 4 ? 2 : 3;
    var html = ['<div class="grid" style="--cols:' + cols + '">'];
    apps.forEach(function (a) {
      var t = a.traffic || {};
      var head = '<div class="card-head"><div><div class="card-title">' + esc(a.name) + '</div><div class="card-sub">' +
        esc(host(a.url)) + (a.public && a.public.code ? " · HTTP " + a.public.code + " · " + ms(a.public.ms) : "") + "</div></div>" +
        '<div class="card-side">' + pill(a.state) +
        (a.state !== "up" && a.since ? '<div class="card-sub">for ' + dur(now() - a.since) + "</div>" : "") + "</div></div>";

      var stats = '<div class="stats">' +
        stat("Requests / min", t.available ? compact(t.rpm) : "–", null, "hero") +
        stat("Visitors 24 h", t.available ? compact(t.visitors_24h) : "–") +
        stat("p95 1 h", t.has_latency ? ms(t.p95_ms_1h) : "–") +
        stat("5xx 1 h", t.available ? compact(t.err5_1h) : "–") + "</div>";

      var middle;
      if (a.state === "down" || (a.state === "degraded" && a.reasons && a.reasons.length)) {
        middle = '<div style="display:grid;grid-template-rows:auto 1fr;gap:.5rem;min-height:0"><div class="reasons">' +
          a.reasons.slice(0, 3).map(function (r) { return "<div>" + esc(r) + "</div>"; }).join("") + "</div>" +
          chartSlot({ kind: "area", points: seriesPoints(t.history, "req"), color: "var(--series-1)", unit: "requests / 10 min", empty: t.error }) + "</div>";
      } else {
        middle = chartSlot({ kind: "area", points: seriesPoints(t.history, "req"), color: "var(--series-1)",
          title: "Requests per 10 min · last 24 h", unit: "requests / 10 min", empty: t.error });
      }

      var mini = overviewMetrics(a, 3).map(function (m) {
        return "<span>" + esc(m.label) + " <b>" + esc(compact(m.value)) + (m.unit ? " " + esc(m.unit) : "") + "</b>" + levelTag(m) + "</span>";
      });
      if (a.cert_days != null) mini.push("<span>TLS <b>" + a.cert_days + " d</b></span>");
      if (a.metrics && a.metrics.app && a.metrics.app.rss_mb) mini.push("<span>RAM <b>" + Math.round(a.metrics.app.rss_mb) + " MB</b></span>");

      html.push('<section class="card ' + esc(a.state) + '">' + head + stats + middle + '<div class="mini">' + mini.join("") + "</div></section>");
    });
    html.push("</div>");
    $("view").innerHTML = html.join("");
  }

  // ------------------------------------------------------------- app page
  function widget(m) {
    var body;
    var err = m.error ? '<div class="widget-error">' + esc(m.stale ? "showing last value · " + m.error : m.error) + "</div>" : "";
    if (m.type === "number") {
      body = '<div class="stat"><div class="value" style="font-size:2.6rem">' + esc(compact(m.value)) +
        (m.unit ? "<small>" + esc(m.unit) + "</small>" : "") + "</div></div>";
    } else if (m.type === "series") {
      body = chartSlot({ kind: (m.value || []).length <= 31 ? "columns" : "area", points: m.value || [],
        color: "var(--series-1)", unit: m.unit || "" });
    } else if (m.type === "table") {
      var v = m.value || { columns: [], rows: [] };
      body = "<table><tr>" + v.columns.map(function (c, i) { return "<th" + (i ? ' class="n"' : "") + ">" + esc(c) + "</th>"; }).join("") + "</tr>" +
        v.rows.slice(0, 6).map(function (r) {
          return "<tr>" + r.map(function (c, i) {
            return "<td" + (i ? ' class="n"' : ' class="path"') + ">" + esc(typeof c === "number" ? compact(c) : c) + "</td>";
          }).join("") + "</tr>";
        }).join("") + "</table>";
    } else {
      body = '<div class="text-value">' + esc(m.value == null ? "–" : m.value) + "</div>";
    }
    return '<div class="panel"><h3>' + esc(m.label) + "</h3><div style=\"min-height:0;display:grid;grid-template-rows:" +
      (err ? "auto " : "") + '1fr">' + err + body + "</div></div>";
  }

  function topTable(rows, label, empty) {
    if (!rows || !rows.length) return '<div class="empty">' + esc(empty) + "</div>";
    return "<table><tr><th>" + esc(label) + '</th><th class="n">24 h</th></tr>' + rows.slice(0, 6).map(function (r) {
      return '<tr><td class="path">' + esc(r[0]) + '</td><td class="n">' + compact(r[1]) + "</td></tr>";
    }).join("") + "</table>";
  }

  function renderApp(data, id) {
    var a = (data.apps || []).filter(function (x) { return x.id === id; })[0];
    if (!a) { $("view").innerHTML = '<div class="empty">Unknown app "' + esc(id) + '"</div>'; return; }
    var t = a.traffic || {};
    var mx = a.metrics || {};
    var info = mx.app || {};

    var meta = [];
    if (info.rails) meta.push("<span>Rails <b>" + esc(info.rails) + "</b></span>");
    if (info.ruby) meta.push("<span>Ruby <b>" + esc(info.ruby) + "</b></span>");
    if (info.revision) meta.push("<span>rev <b>" + esc(info.revision) + "</b></span>");
    if (info.uptime_s != null) meta.push("<span>process up <b>" + dur(info.uptime_s) + "</b></span>");
    if (info.rss_mb != null) meta.push("<span>RAM <b>" + Math.round(info.rss_mb) + " MB</b></span>");
    (mx.databases || []).forEach(function (d) {
      meta.push('<span><span class="dot ' + (d.ok ? "up" : "down") + '" style="display:inline-block;width:.6rem;height:.6rem;border-radius:50%"></span> ' +
        esc(d.name) + " <b>" + (d.ok ? ms(d.ms) : "error") + "</b></span>");
    });
    if (a.cert_days != null) meta.push("<span>TLS <b>" + a.cert_days + " days</b></span>");
    if (mx.status === "not_installed") meta.push("<span>metrics gem <b>not installed</b></span>");
    if (mx.status === "unconfigured") meta.push("<span>metrics <b>no port configured</b></span>");
    if (mx.status === "error") meta.push("<span>metrics <b>" + esc(mx.error) + "</b></span>");

    var head = '<div class="app-head">' + pill(a.state) + '<div class="name">' + esc(a.name) + "</div>" +
      '<div class="muted" style="font-size:1.1rem">' + esc(host(a.url)) + "</div>" +
      (a.state !== "up" && a.since ? '<div class="muted">for ' + dur(now() - a.since) + "</div>" : "") +
      '<div class="meta" style="flex-basis:100%">' + meta.join("") + "</div>" +
      (a.reasons && a.reasons.length ? '<div class="reasons" style="flex-basis:100%">' +
        a.reasons.map(function (r) { return "<div>" + esc(r) + "</div>"; }).join("") + "</div>" : "") + "</div>";

    var kpis = '<div class="kpis">' +
      stat("Requests / min", t.available ? compact(t.rpm) : "–", null, "hero") +
      stat("Requests 24 h", t.available ? compact(t.req_24h) : "–") +
      stat("Page views 24 h", t.available ? compact(t.pages_24h) : "–") +
      stat("Visitors 24 h", t.available ? compact(t.visitors_24h) : "–") +
      stat("p50 / p95 1 h", t.has_latency ? ms(t.p50_ms_1h) + " / " + ms(t.p95_ms_1h) : "–") +
      stat("4xx / 5xx 1 h", t.available ? compact(t.err4_1h) + " / " + compact(t.err5_1h) : "–") +
      stat("Health check", a.public ? (a.public.code || "error") + " · " + ms(a.public.ms) : "–") + "</div>";

    var minutePts = seriesPoints(t.minute, "req");
    var charts = '<div class="row" style="--n:' + (t.has_latency ? 3 : 2) + '">' +
      '<div class="panel"><h3>Requests per 10 min · last 24 h</h3>' +
      chartSlot({ kind: "area", points: seriesPoints(t.history, "req"), color: "var(--series-1)", unit: "requests", empty: t.error }) + "</div>" +
      '<div class="panel"><h3>Requests per minute · last hour <span class="chart-title" style="display:inline-flex;margin-left:1rem">' +
      '<span class="key"><i style="background:var(--series-1)"></i>OK</span><span class="key"><i style="background:var(--critical)"></i>5xx</span></span></h3>' +
      chartSlot({ kind: "columns", points: minutePts, stack: seriesPoints(t.minute, "err5"), color: "var(--series-1)", unit: "requests", empty: t.error }) + "</div>" +
      (t.has_latency ? '<div class="panel"><h3>Response time p95 · last 24 h</h3>' +
        chartSlot({ kind: "area", points: seriesPoints(t.history, "p95"), color: "var(--series-2)", fmt: ms, unit: "" }) + "</div>" : "") +
      "</div>";

    var custom = visibleMetrics(a).filter(function (m) { return m.type !== "number"; }).slice(0, 3);
    var numbers = visibleMetrics(a).filter(function (m) { return m.type === "number"; });
    var bottom = [];
    if (numbers.length) {
      bottom.push('<div class="panel"><h3>' + esc(info.name || a.name) + '</h3><div class="kpis big" style="align-content:start;gap:1.6rem 2.6rem">' +
        numbers.slice(0, 6).map(function (m) {
          return '<div>' + stat(m.label, m.value == null ? "–" : compact(m.value), m.unit) + levelTag(m) +
            (m.error ? '<div class="widget-error">' + esc(m.error) + "</div>" : "") + "</div>";
        }).join("") + "</div></div>");
    }
    custom.forEach(function (m) { bottom.push(widget(m)); });
    if (bottom.length < 4) bottom.push('<div class="panel"><h3>Top pages</h3>' + topTable(t.top_pages, "path", t.available ? "No page views yet" : (t.error || "No traffic data")) + "</div>");
    if (bottom.length < 4 && t.top_errors && t.top_errors.length) bottom.push('<div class="panel"><h3>Server errors</h3>' + topTable(t.top_errors, "status path", "") + "</div>");

    $("view").innerHTML = '<div class="app">' + head + kpis + charts +
      '<div class="widgets" style="--n:' + bottom.length + '">' + bottom.join("") + "</div></div>";
  }

  // ------------------------------------------------------------- chrome
  function renderEvents(data) {
    var evs = (data.events || []).slice(0, 4);
    $("events").innerHTML = evs.map(function (e) {
      return '<span class="ev"><span class="dot ' + esc(e.to) + '"></span>' + hhmm(e.ts) + " · " + esc(e.name) + " " +
        esc(STATE_LABEL[e.to] || e.to) + "</span>";
    }).join("") || '<span class="muted">No status changes recorded</span>';
  }

  function playlist() {
    var data = state.payload && state.payload.data;
    var apps = data ? data.apps || [] : [];
    var ids = apps.map(function (a) { return "app:" + a.id; });
    if (state.pinned) return [state.pinned];
    var screens = state.config.screens || {};
    // "apps" expands to every app the collector found, in the order the apps declared.
    var list = [];
    (screens[state.screen] || ["overview", "apps"]).forEach(function (v) {
      if (v === "apps") ids.forEach(function (id) { if (list.indexOf(id) < 0) list.push(id); });
      else if ((v === "overview" || ids.indexOf(v) >= 0) && list.indexOf(v) < 0) list.push(v);
    });
    if (state.config.focus_problems !== false) {
      var bad = apps.filter(function (a) { return a.state === "down"; }).map(function (a) { return "app:" + a.id; });
      // Something is down: alternate the overview with the broken apps only.
      if (bad.length && list.indexOf("overview") >= 0) return ["overview"].concat(bad);
    }
    return list.length ? list : ["overview"];
  }

  function currentView() {
    var list = playlist();
    if (state.idx >= list.length) state.idx = 0;
    return list[state.idx];
  }

  function render() {
    var p = state.payload;
    var data = p && p.data;
    var stale = state.config.stale_after_seconds || 240;
    var banner = [];
    if (p && p.error) banner.push("Cannot reach the server: " + p.error + (p.fetched_at ? " — showing data from " + hhmm(p.fetched_at) : ""));
    if (data && data.problems && data.problems.length) banner.push("Config: " + data.problems.slice(0, 2).join(" · "));
    if (data && now() - data.generated_at > stale) banner.push("The collector has not run for " + dur(now() - data.generated_at) + " — check the cron job on the server.");
    $("banner").hidden = !banner.length;
    $("banner").textContent = banner.join("  ·  ");
    if (!data) { $("view").innerHTML = '<div class="empty">Waiting for data…</div>'; return; }

    var view = currentView();
    $("title").textContent = state.config.title || (data.host && data.host.hostname) || "Servidor";
    var app = view.indexOf("app:") === 0 ? (data.apps || []).filter(function (a) { return "app:" + a.id === view; })[0] : null;
    $("view-name").textContent = app ? "/ " + app.name : "/ overview";
    renderHost(data.host);
    pending = {};
    if (app) renderApp(data, app.id); else renderOverview(data);
    renderEvents(data);
    var list = playlist();
    $("dots").innerHTML = list.length > 1 ? list.map(function (_, i) { return '<span class="' + (i === state.idx ? "on" : "") + '"></span>'; }).join("") : "";
    requestAnimationFrame(drawCharts);
    tick();
  }

  function tick() {
    var d = new Date();
    $("clock").textContent = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    var data = state.payload && state.payload.data;
    if (data) {
      var age = now() - data.generated_at;
      $("fresh").textContent = "data " + dur(Math.max(0, age)) + " old";
      $("fresh").className = "fresh " + (age > (state.config.stale_after_seconds || 240) ? "late" : "muted");
    }
  }

  function schedule() {
    clearTimeout(state.rotateTimer);
    if (state.paused || state.pinned) return;
    state.rotateTimer = setTimeout(function () { advance(1); }, (state.config.rotate_seconds || 20) * 1000);
  }
  function advance(dir) {
    var n = playlist().length;
    state.idx = (state.idx + dir + n) % n;
    render();
    schedule();
  }

  function fetchJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) { return r.json(); });
  }
  function poll() {
    fetchJSON("/api/status").then(function (p) { state.payload = p; render(); })
      .catch(function (e) { state.payload = { data: state.payload && state.payload.data, error: "local server: " + e.message }; render(); });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "ArrowRight") advance(1);
    else if (e.key === "ArrowLeft") advance(-1);
    else if (e.key === " ") { state.paused = !state.paused; schedule(); }
  });
  document.addEventListener("mousemove", function () { document.body.classList.add("pointer"); }, { once: true });
  var resizeTimer;
  window.addEventListener("resize", function () { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 300); });

  fetchJSON("/api/config").catch(function () { return {}; }).then(function (cfg) {
    state.config = cfg || {};
    poll();
    setInterval(poll, 15000);
    setInterval(tick, 1000);
    schedule();
  });
})();
