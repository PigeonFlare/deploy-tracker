(() => {
  "use strict";

  const DAY = 86400;
  const MIN_N = 5;
  const LINE_MIN_N = 15;
  const UP = new Set(["U", "B"]);
  const DOWNISH = new Set(["D", "P"]);
  const REASONS = {
    dns: "Domain doesn't resolve",
    timeout: "Timed out",
    refused: "Connection refused",
    connection: "Connection failed",
    tls: "Broken HTTPS",
    "http 404": "Page not found (404/410)",
    "http 5xx": "Server error (5xx)",
    removed: "Host says it was removed",
    parked: "Parked or for sale",
    "redirect loop": "Redirect loop",
  };
  const CATEGORY_LABEL = { apps: "Apps & tools", games: "Games", other: "Other" };

  const state = { tier: 100, category: "all", source: "all", breakdown: "tier", day: 90, search: "",
                  sort: { key: "votes", dir: -1 }, limit: 50 };
  let data = null, sites = [], dayTs = [], nowTs = 0;

  const $ = (sel) => document.querySelector(sel);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const pct = (v) => (v == null || isNaN(v) ? "–" : Math.round(v * 100) + "%");
  const fmtInt = d3.format(",");
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };

  // ---------- Theme ----------
  const root = document.documentElement;
  try {
    const saved = localStorage.getItem("theme");
    if (saved === "light" || saved === "dark") root.dataset.theme = saved;
  } catch (e) { /* storage unavailable */ }
  $("#theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* ignore */ }
    themeLabel();
    renderAll();
  });
  function themeLabel() {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    $("#theme").textContent = dark ? "Light mode" : "Dark mode";
  }
  themeLabel();
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { themeLabel(); renderAll(); });

  // ---------- Tooltip ----------
  const tip = $("#tooltip");
  function showTip(event, { title, rows = [], note }) {
    tip.replaceChildren();
    if (title) tip.append(el("div", "tt-title", title));
    for (const r of rows) {
      const row = el("div", "tt-row");
      if (r.color) { const i = el("i"); i.style.background = r.color; row.append(i); }
      row.append(el("b", null, r.value), el("span", null, r.label));
      tip.append(row);
    }
    if (note) tip.append(el("div", "tt-muted", note));
    tip.classList.add("on");
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = event.clientX + pad, y = event.clientY + pad;
    if (x + w > innerWidth - 8) x = event.clientX - w - pad;
    if (y + h > innerHeight - 8) y = event.clientY - h - pad;
    tip.style.left = Math.max(8, x) + "px";
    tip.style.top = Math.max(8, y) + "px";
  }
  const hideTip = () => tip.classList.remove("on");

  // ---------- Data ----------
  function statusOf(ch) {
    return UP.has(ch) ? "up" : ch === "P" ? "parked" : ch === "D" ? "down" : "none";
  }

  function hostGroup(s) {
    if (s.hosting) return { key: s.hosting, free: true };
    const tld = "." + s.domain.split(".").pop();
    return { key: [".com", ".io", ".ai", ".app", ".dev", ".org", ".net", ".so", ".xyz", ".me", ".co"].includes(tld) ? tld : "other TLD", free: false };
  }

  function prepare(raw) {
    data = raw;
    dayTs = raw.days.map((d) => Date.parse(d + "T12:00:00Z") / 1000);
    nowTs = dayTs.length ? dayTs[dayTs.length - 1] : Date.now() / 1000;
    sites = raw.sites.map((s) => {
      const obs = [];
      let ups = 0, total = 0, lastCh = ".";
      for (let i = 0; i < s.history.length; i++) {
        const ch = s.history[i];
        if (ch === ".") continue;
        obs.push([Math.max(0, Math.floor((dayTs[i] - s.created) / DAY)), UP.has(ch)]);
        total++; if (UP.has(ch)) ups++;
        lastCh = ch;
      }
      const g = hostGroup(s);
      return { ...s, obs, uptime: total ? ups / total : null, status: statusOf(lastCh),
               age: Math.max(0, Math.floor((nowTs - s.created) / DAY)), hostKey: g.key, free: g.free,
               reason: (s.last && s.last.reason) || null };
    });
  }

  function filtered({ ignoreTier = false } = {}) {
    return sites.filter((s) => s.rank <= data.top_n
      && (ignoreTier || s.rank <= state.tier)
      && (state.category === "all" || s.category === state.category)
      && (state.source === "all" || s.source === state.source));
  }

  // Survival at age x = share up on each site's first check at or after x days.
  function survival(list, maxAge) {
    const n = new Uint32Array(maxAge + 1), up = new Uint32Array(maxAge + 1);
    for (const s of list) {
      let x = 0;
      for (const [age, isUp] of s.obs) {
        for (; x <= Math.min(age, maxAge); x++) { n[x]++; if (isUp) up[x]++; }
        if (x > maxAge) break;
      }
    }
    const out = [];
    for (let x = 0; x <= maxAge; x++) out.push({ x, y: n[x] ? up[x] / n[x] : null, n: n[x] });
    return out;
  }

  const maxAgeOf = (list) => Math.min(365, d3.max(list, (s) => s.age) || 0);

  // ---------- Generic chart scaffolding ----------
  function frame(node, height, margin) {
    node.replaceChildren();
    const width = node.clientWidth;
    const svg = d3.select(node).append("svg").attr("width", width).attr("height", height)
      .attr("viewBox", `0 0 ${width} ${height}`);
    return { svg, width, height, iw: width - margin.left - margin.right, ih: height - margin.top - margin.bottom,
             g: svg.append("g").attr("transform", `translate(${margin.left},${margin.top})`) };
  }

  function emptyState(node, text) {
    node.replaceChildren(el("div", "empty-state", text));
  }

  function legend(node, items, box = false) {
    node.replaceChildren(...items.map((it) => {
      const span = el("span");
      const i = el("i", box ? "box" : null);
      i.style.background = it.color;
      span.append(i, document.createTextNode(it.label));
      return span;
    }));
  }

  // ---------- Breakdown series ----------
  function breakdownSeries() {
    const base = filtered({ ignoreTier: state.breakdown === "tier" });
    switch (state.breakdown) {
      case "tier":
        return [
          { label: "Top 10", color: css("--t10"), list: base.filter((s) => s.rank <= 10) },
          { label: "Top 50", color: css("--t50"), list: base.filter((s) => s.rank <= 50) },
          { label: "Top 100", color: css("--t100"), list: base },
        ];
      case "category":
        return ["apps", "games", "other"].map((c, i) => ({ label: CATEGORY_LABEL[c], color: css(`--s${i + 1}`), list: base.filter((s) => s.category === c) }));
      case "source":
        return data.sources.map((src, i) => ({ label: src.name, color: css(`--s${i + 1}`), list: base.filter((s) => s.source === src.name) }));
      default:
        return [
          { label: "Own domain", color: css("--s1"), list: base.filter((s) => !s.free) },
          { label: "Free subdomain", color: css("--s2"), list: base.filter((s) => s.free) },
        ];
    }
  }

  // ---------- Survival chart ----------
  function renderSurvival() {
    const node = $("#survival");
    const series = breakdownSeries().filter((s) => s.list.length);
    const maxAge = Math.max(30, maxAgeOf(series.flatMap((s) => s.list)));
    series.forEach((s) => { s.points = survival(s.list, maxAge); });
    legend($("#survival-legend"), series);
    if (!series.length) return emptyState(node, "No sites match these filters.");
    const labelled = series.length <= 4 && node.clientWidth > 520;
    const m = { top: 12, right: labelled ? 92 : 16, bottom: 30, left: 40 };
    const f = frame(node, 340, m);
    const x = d3.scaleLinear().domain([0, maxAge]).range([0, f.iw]);
    const y = d3.scaleLinear().domain([0, 1]).range([f.ih, 0]);
    f.g.append("g").attr("class", "gridlines").call(d3.axisLeft(y).ticks(5).tickSize(-f.iw).tickFormat(""));
    f.g.append("g").attr("class", "axis").call(d3.axisLeft(y).ticks(5).tickFormat(d3.format(".0%")).tickSize(0).tickPadding(8)).select(".domain").remove();
    const xt = (f.iw < 500 ? [0, 90, 180, 270, 365] : [0, 30, 60, 90, 120, 180, 240, 300, 365]).filter((t) => t <= maxAge);
    f.g.append("g").attr("class", "axis").attr("transform", `translate(0,${f.ih})`)
      .call(d3.axisBottom(x).tickValues(xt).tickFormat((d) => d + "d").tickSizeOuter(0));

    if (state.day <= maxAge) {
      f.g.append("line").attr("class", "marker-rule").attr("x1", x(state.day)).attr("x2", x(state.day)).attr("y1", 0).attr("y2", f.ih);
    }
    const line = d3.line().defined((d) => d.n >= LINE_MIN_N && d.y != null).x((d) => x(d.x)).y((d) => y(d.y)).curve(d3.curveMonotoneX);
    for (const s of series) {
      f.g.append("path").attr("fill", "none").attr("stroke", s.color).attr("stroke-width", 2)
        .attr("stroke-linejoin", "round").attr("stroke-linecap", "round").attr("d", line(s.points));
    }
    if (labelled) {
      const ends = series.map((s) => {
        const p = [...s.points].reverse().find((d) => d.n >= LINE_MIN_N && d.y != null);
        return p && { s, p, ty: y(p.y) };
      }).filter(Boolean).sort((a, b) => a.ty - b.ty);
      for (let i = 1; i < ends.length; i++) ends[i].ty = Math.max(ends[i].ty, ends[i - 1].ty + 14);
      for (const e of ends) {
        f.g.append("text").attr("class", "label strong").attr("x", x(e.p.x) + 8).attr("y", e.ty + 4).text(`${e.s.label} ${pct(e.p.y)}`);
      }
    }
    const cross = f.g.append("line").attr("class", "crosshair").attr("y1", 0).attr("y2", f.ih).style("opacity", 0);
    const dots = series.map((s) => f.g.append("circle").attr("r", 4).attr("fill", s.color).attr("stroke", css("--surface")).attr("stroke-width", 2).style("opacity", 0));
    f.g.append("rect").attr("class", "hit").attr("width", f.iw).attr("height", f.ih)
      .on("pointermove", (ev) => {
        const xi = Math.max(0, Math.min(maxAge, Math.round(x.invert(d3.pointer(ev)[0]))));
        cross.attr("x1", x(xi)).attr("x2", x(xi)).style("opacity", 1);
        const rows = [];
        series.forEach((s, i) => {
          const p = s.points[xi];
          const ok = p.n >= LINE_MIN_N && p.y != null;
          dots[i].style("opacity", ok ? 1 : 0).attr("cx", x(xi)).attr("cy", ok ? y(p.y) : 0);
          rows.push({ color: s.color, value: ok ? pct(p.y) : "–", label: `${s.label} · ${p.n} sites` });
        });
        showTip(ev, { title: `${xi} days after launch`, rows });
      })
      .on("pointerleave", () => { cross.style("opacity", 0); dots.forEach((d) => d.style("opacity", 0)); hideTip(); });
  }

  // ---------- Hero & KPIs ----------
  function renderHero() {
    const list = filtered();
    const maxAge = Math.max(state.day, maxAgeOf(list));
    const curve = survival(list, maxAge);
    const p = curve[state.day];
    $("#hero-days").textContent = state.day;
    $("#hero-pct").textContent = p && p.n >= MIN_N ? Math.round(p.y * 100) : "–";
    $("#hero-note").textContent = p && p.n >= MIN_N
      ? `Based on ${fmtInt(p.n)} sites at least ${state.day} days old.`
      : `Not enough sites are ${state.day} days old yet.`;

    $("#kpi-tracked").textContent = fmtInt(list.length);
    const months = new Set(list.map((s) => s.cohort)).size;
    $("#kpi-tracked-foot").textContent = `across ${months} launch month${months === 1 ? "" : "s"}`;
    const checked = list.filter((s) => s.status !== "none");
    const upNow = checked.filter((s) => s.status === "up").length;
    $("#kpi-up").textContent = pct(checked.length ? upNow / checked.length : null);
    $("#kpi-up-foot").textContent = `${fmtInt(upNow)} of ${fmtInt(checked.length)} sites`;

    const half = curve.find((d) => d.n >= 10 && d.y != null && d.y < 0.5);
    const lastOk = [...curve].reverse().find((d) => d.n >= 10);
    $("#kpi-half").textContent = half ? `${half.x}d` : lastOk ? `>${lastOk.x}d` : "–";

    const from = Math.max(1, dayTs.length - 30);
    let outages = 0, recovered = 0;
    for (const s of list) {
      const t = transitions(s.history, from);
      outages += t.filter((e) => e.kind === "down").length;
      recovered += t.filter((e) => e.kind === "up").length;
    }
    $("#kpi-outages").textContent = fmtInt(outages);
    $("#kpi-outages-foot").textContent = dayTs.length > 1 ? `${fmtInt(recovered)} recoveries` : "needs two days of checks";
  }

  // Status changes in a history string from index `from` on, skipping unchecked days.
  function transitions(history, from) {
    const out = [];
    let prev = null;
    for (let i = 0; i < history.length; i++) {
      const ch = history[i];
      if (ch === ".") continue;
      if (prev && i >= from) {
        if (UP.has(prev) && DOWNISH.has(ch)) out.push({ i, kind: "down", ch });
        else if (DOWNISH.has(prev) && UP.has(ch)) out.push({ i, kind: "up", ch });
      }
      prev = ch;
    }
    return out;
  }

  // ---------- Cohorts ----------
  function monthLabel(c, withYear) {
    const d = new Date(c + "-01T00:00:00Z");
    return d.toLocaleString("en", { month: "short", timeZone: "UTC" }) + (withYear ? " ’" + String(d.getUTCFullYear()).slice(2) : "");
  }

  function renderCohorts() {
    const node = $("#cohorts");
    const list = filtered();
    const keys = [["up", "Up", css("--up")], ["parked", "Parked", css("--parked")], ["down", "Down", css("--down")]];
    legend($("#cohort-legend"), keys.map(([, label, color]) => ({ label, color })), true);
    const groups = d3.groups(list.filter((s) => s.status !== "none"), (s) => s.cohort).sort((a, b) => d3.ascending(a[0], b[0]));
    if (!groups.length) return emptyState(node, "No checks yet.");
    const rows = groups.map(([c, ss]) => {
      const r = { cohort: c, total: ss.length };
      keys.forEach(([k]) => { r[k] = ss.filter((s) => s.status === k).length / ss.length; });
      return r;
    });
    const m = { top: 8, right: 4, bottom: 28, left: 40 };
    const f = frame(node, 300, m);
    const x = d3.scaleBand().domain(rows.map((r) => r.cohort)).range([0, f.iw]).paddingInner(0.25).paddingOuter(0.05);
    const y = d3.scaleLinear().domain([0, 1]).range([f.ih, 0]);
    f.g.append("g").attr("class", "gridlines").call(d3.axisLeft(y).ticks(4).tickSize(-f.iw).tickFormat(""));
    f.g.append("g").attr("class", "axis").call(d3.axisLeft(y).ticks(4).tickFormat(d3.format(".0%")).tickSize(0).tickPadding(8)).select(".domain").remove();
    const every = Math.ceil(rows.length / Math.max(1, Math.floor(f.iw / 46)));
    f.g.append("g").attr("class", "axis").attr("transform", `translate(0,${f.ih})`)
      .call(d3.axisBottom(x).tickSizeOuter(0).tickFormat((c, i) => (i % every ? "" : monthLabel(c, i === 0 || c.endsWith("-01")))));
    const gap = 2;
    for (const r of rows) {
      let acc = 0;
      const col = f.g.append("g");
      keys.forEach(([k, , color], idx) => {
        const v = r[k];
        if (!v) return;
        const y0 = y(acc), y1 = y(acc + v);
        const h = Math.max(0, y0 - y1 - (acc + v < 1 ? gap : 0));
        col.append("rect").attr("x", x(r.cohort)).attr("width", x.bandwidth()).attr("y", y1 + (acc + v < 1 ? gap : 0))
          .attr("height", h).attr("rx", Math.min(4, x.bandwidth() / 4)).attr("fill", color);
        acc += v;
      });
      col.append("rect").attr("class", "hit bar-hit").attr("x", x(r.cohort) - x.step() * 0.125).attr("width", x.step()).attr("y", 0).attr("height", f.ih)
        .on("pointermove", (ev) => showTip(ev, {
          title: `Launched ${monthLabel(r.cohort, true)}`,
          rows: keys.map(([k, label, color]) => ({ color, value: pct(r[k]), label })),
          note: `${r.total} sites, ${Math.round((nowTs - Date.parse(r.cohort + "-15T00:00:00Z") / 1000) / DAY)} days old on average`,
        }))
        .on("pointerleave", hideTip);
    }
  }

  // ---------- Reasons ----------
  function hbar(node, rows, { color, valueFmt, labelWidth = 170, height, tipFor }) {
    if (!rows.length) return emptyState(node, "Nothing to show yet.");
    const barH = 18, gapH = 10;
    const h = height || rows.length * (barH + gapH) + 24;
    const m = { top: 4, right: 52, bottom: 20, left: Math.min(labelWidth, node.clientWidth * 0.45) };
    const f = frame(node, h, m);
    const max = d3.max(rows, (r) => r.value) || 1;
    const x = d3.scaleLinear().domain([0, valueFmt === pct ? 1 : max]).range([0, f.iw]);
    const y = d3.scaleBand().domain(rows.map((r) => r.label)).range([0, f.ih]).paddingInner(0.35);
    f.g.append("g").attr("class", "gridlines").attr("transform", `translate(0,${f.ih})`)
      .call(d3.axisBottom(x).ticks(4).tickSize(-f.ih).tickFormat(""));
    f.g.append("g").attr("class", "axis").attr("transform", `translate(0,${f.ih})`)
      .call(d3.axisBottom(x).ticks(4).tickFormat(valueFmt === pct ? d3.format(".0%") : d3.format("d")).tickSizeOuter(0)).select(".domain").remove();
    f.g.append("line").attr("stroke", css("--axis")).attr("y1", 0).attr("y2", f.ih);
    for (const r of rows) {
      const yy = y(r.label), bh = y.bandwidth(), w = Math.max(1, x(r.value));
      f.g.append("path").attr("fill", r.color || color)
        .attr("d", `M0,${yy}h${Math.max(0, w - 4)}a4,4 0 0 1 4,4v${Math.max(0, bh - 8)}a4,4 0 0 1 -4,4h${-Math.max(0, w - 4)}z`);
      f.g.append("text").attr("class", "label").attr("x", -8).attr("y", yy + bh / 2 + 4).attr("text-anchor", "end").text(r.label);
      f.g.append("text").attr("class", "label strong").attr("x", w + 6).attr("y", yy + bh / 2 + 4).text(valueFmt(r.value));
      f.g.append("rect").attr("class", "hit bar-hit").attr("x", -m.left).attr("width", f.iw + m.left + m.right).attr("y", yy - 4).attr("height", bh + 8)
        .on("pointermove", (ev) => showTip(ev, tipFor(r))).on("pointerleave", hideTip);
    }
  }

  function renderReasons() {
    const list = filtered().filter((s) => s.status === "down" || s.status === "parked");
    const counts = d3.rollups(list, (v) => v.length, (s) => REASONS[s.status === "parked" ? "parked" : s.reason] || "Other")
      .map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
    hbar($("#reasons"), counts, {
      color: css("--s1"), valueFmt: fmtInt, height: 300,
      tipFor: (r) => ({ title: r.label, rows: [{ value: fmtInt(r.value), label: "sites" }, { value: pct(r.value / list.length), label: "of sites not up" }] }),
    });
  }

  // ---------- Status wall ----------
  function renderWall() {
    const node = $("#wall");
    const colors = { up: css("--up"), parked: css("--parked"), down: css("--down"), none: css("--none") };
    legend($("#wall-legend"), [["Up", colors.up], ["Parked", colors.parked], ["Down", colors.down], ["Filtered out", colors.none]].map(([label, color]) => ({ label, color })), true);
    const all = sites.filter((s) => s.rank <= data.top_n);
    if (!all.length) return emptyState(node, "No sites yet.");
    const keep = new Set(filtered());
    const cohorts = [...new Set(all.map((s) => s.cohort))].sort();
    const labelW = 56;
    const avail = node.clientWidth - labelW;
    const cell = Math.max(5, Math.floor(avail / data.top_n));
    const rowH = Math.max(cell, 16);
    const width = labelW + cell * data.top_n;
    node.style.overflowX = width > node.clientWidth ? "auto" : "visible";
    const height = cohorts.length * rowH + 22;
    node.replaceChildren();
    const svg = d3.select(node).append("svg").attr("width", width).attr("height", height);
    const g = svg.append("g").attr("transform", "translate(0,18)");
    [1, 10, 50, 100].forEach((r) => {
      if (r > data.top_n) return;
      svg.append("text").attr("class", "label").attr("x", labelW + (r - 0.5) * cell).attr("y", 11).attr("text-anchor", "middle").text("#" + r);
    });
    cohorts.forEach((c, row) => {
      g.append("text").attr("class", "label").attr("x", 0).attr("y", row * rowH + rowH / 2 + 4).text(monthLabel(c, true));
    });
    const index = new Map(cohorts.map((c, i) => [c, i]));
    const inset = cell >= 8 ? 1 : 0.5;
    g.selectAll("rect").data(all).join("rect").attr("class", "wall-cell")
      .attr("x", (s) => labelW + (s.rank - 1) * cell + inset).attr("y", (s) => index.get(s.cohort) * rowH + inset)
      .attr("width", cell - inset * 2).attr("height", rowH - inset * 2).attr("rx", Math.min(2, cell / 4))
      .attr("fill", (s) => (keep.has(s) ? colors[s.status] : colors.none))
      .on("pointermove", (ev, s) => showTip(ev, {
        title: s.title,
        rows: [{ value: statusText(s), label: s.domain }, { value: "#" + s.rank, label: `${monthLabel(s.cohort, true)} · ${fmtInt(s.votes)} votes` }],
        note: `${s.source} · ${CATEGORY_LABEL[s.category] || s.category} · ${s.age} days old`,
      }))
      .on("pointerleave", hideTip)
      .on("click", (ev, s) => window.open(s.url, "_blank", "noopener"));
  }

  function statusText(s) {
    if (s.status === "up") return "Up";
    if (s.status === "parked") return "Parked";
    if (s.status === "down") return "Down";
    return "Not checked";
  }

  // ---------- Fleet uptime ----------
  function renderUptime() {
    const list = filtered();
    const days = dayTs.map((t, i) => {
      let n = 0, up = 0, starts = 0;
      for (const s of list) {
        const ch = s.history[i];
        if (!ch || ch === ".") continue;
        n++; if (UP.has(ch)) up++;
      }
      return { i, t, date: new Date(t * 1000), n, y: n ? up / n : null, starts };
    });
    for (const s of list) for (const e of transitions(s.history, 0)) if (e.kind === "down") days[e.i].starts++;
    const node = $("#uptime");
    const pts = days.filter((d) => d.n);
    if (!pts.length) { emptyState(node, "No checks yet."); emptyState($("#outages"), ""); return; }
    const m = { top: 10, right: 12, bottom: 26, left: 40 };
    const f = frame(node, 220, m);
    const ext = d3.extent(pts, (d) => d.date);
    if (+ext[0] === +ext[1]) { ext[0] = new Date(+ext[0] - DAY * 1000); ext[1] = new Date(+ext[1] + DAY * 1000); }
    const x = d3.scaleTime().domain(ext).range([0, f.iw]);
    const lo = Math.max(0, Math.floor((d3.min(pts, (d) => d.y) - 0.05) * 10) / 10);
    const y = d3.scaleLinear().domain([lo, 1]).range([f.ih, 0]);
    f.g.append("g").attr("class", "gridlines").call(d3.axisLeft(y).ticks(4).tickSize(-f.iw).tickFormat(""));
    f.g.append("g").attr("class", "axis").call(d3.axisLeft(y).ticks(4).tickFormat(d3.format(".0%")).tickSize(0).tickPadding(8)).select(".domain").remove();
    f.g.append("g").attr("class", "axis").attr("transform", `translate(0,${f.ih})`).call(d3.axisBottom(x).ticks(Math.min(6, pts.length)).tickSizeOuter(0));
    const color = css("--s1");
    const area = d3.area().x((d) => x(d.date)).y0(f.ih).y1((d) => y(d.y)).curve(d3.curveMonotoneX);
    const grad = `grad-${Math.random().toString(36).slice(2)}`;
    const lg = f.svg.append("defs").append("linearGradient").attr("id", grad).attr("x1", 0).attr("x2", 0).attr("y1", 0).attr("y2", 1);
    lg.append("stop").attr("offset", "0%").attr("stop-color", color).attr("stop-opacity", 0.22);
    lg.append("stop").attr("offset", "100%").attr("stop-color", color).attr("stop-opacity", 0);
    f.g.append("path").attr("fill", `url(#${grad})`).attr("d", area(pts));
    f.g.append("path").attr("fill", "none").attr("stroke", color).attr("stroke-width", 2).attr("d", d3.line().x((d) => x(d.date)).y((d) => y(d.y)).curve(d3.curveMonotoneX)(pts));
    if (pts.length === 1) f.g.append("circle").attr("cx", x(pts[0].date)).attr("cy", y(pts[0].y)).attr("r", 4).attr("fill", color);
    const cross = f.g.append("line").attr("class", "crosshair").attr("y1", 0).attr("y2", f.ih).style("opacity", 0);
    const dot = f.g.append("circle").attr("r", 4).attr("fill", color).attr("stroke", css("--surface")).attr("stroke-width", 2).style("opacity", 0);
    const fmtDate = d3.utcFormat("%b %-d, %Y");
    const bis = d3.bisector((d) => d.date).center;
    f.g.append("rect").attr("class", "hit").attr("width", f.iw).attr("height", f.ih)
      .on("pointermove", (ev) => {
        const d = pts[bis(pts, x.invert(d3.pointer(ev)[0]))];
        cross.attr("x1", x(d.date)).attr("x2", x(d.date)).style("opacity", 1);
        dot.attr("cx", x(d.date)).attr("cy", y(d.y)).style("opacity", 1);
        showTip(ev, { title: fmtDate(d.date), rows: [{ color, value: pct(d.y), label: `up of ${fmtInt(d.n)} checked` }, { value: fmtInt(d.starts), label: "new outages" }] });
      })
      .on("pointerleave", () => { cross.style("opacity", 0); dot.style("opacity", 0); hideTip(); });

    const o = frame($("#outages"), 110, { top: 8, right: 12, bottom: 22, left: 40 });
    const xb = d3.scaleBand().domain(pts.map((d) => d.i)).range([0, o.iw]).paddingInner(0.2);
    const yb = d3.scaleLinear().domain([0, Math.max(3, d3.max(pts, (d) => d.starts))]).nice().range([o.ih, 0]);
    o.g.append("g").attr("class", "gridlines").call(d3.axisLeft(yb).ticks(2).tickSize(-o.iw).tickFormat(""));
    o.g.append("g").attr("class", "axis").call(d3.axisLeft(yb).ticks(2).tickFormat(d3.format("d")).tickSize(0).tickPadding(8)).select(".domain").remove();
    o.g.append("line").attr("stroke", css("--axis")).attr("x1", 0).attr("x2", o.iw).attr("y1", o.ih).attr("y2", o.ih);
    const bw = Math.min(14, xb.bandwidth());
    for (const d of pts) {
      const h = o.ih - yb(d.starts);
      const bx = xb(d.i) + (xb.bandwidth() - bw) / 2;
      if (h > 0) {
        const r = Math.min(4, bw / 2, h);
        o.g.append("path").attr("fill", css("--down"))
          .attr("d", `M${bx},${o.ih}v${-(h - r)}a${r},${r} 0 0 1 ${r},${-r}h${bw - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${h - r}z`);
      }
      o.g.append("rect").attr("class", "hit bar-hit").attr("x", xb(d.i)).attr("width", xb.step()).attr("y", 0).attr("height", o.ih)
        .on("pointermove", (ev) => showTip(ev, { title: fmtDate(d.date), rows: [{ color: css("--down"), value: fmtInt(d.starts), label: "sites went down" }] }))
        .on("pointerleave", hideTip);
    }
  }

  // ---------- Popularity ----------
  function renderPopularity() {
    const node = $("#popularity");
    const list = filtered({ ignoreTier: true }).filter((s) => s.status !== "none");
    if (!list.length) return emptyState(node, "No checks yet.");
    const size = 10;
    const buckets = d3.range(0, data.top_n, size).map((a) => {
      const ss = list.filter((s) => s.rank > a && s.rank <= a + size);
      return { label: `${a + 1}–${a + size}`, n: ss.length, y: ss.length ? ss.filter((s) => s.status === "up").length / ss.length : null,
               votes: d3.median(ss, (s) => s.votes) };
    });
    const overall = list.filter((s) => s.status === "up").length / list.length;
    const m = { top: 18, right: 8, bottom: 40, left: 40 };
    const f = frame(node, 360, m);
    const x = d3.scaleBand().domain(buckets.map((b) => b.label)).range([0, f.iw]).paddingInner(0.28);
    const y = d3.scaleLinear().domain([0, 1]).range([f.ih, 0]);
    f.g.append("g").attr("class", "gridlines").call(d3.axisLeft(y).ticks(5).tickSize(-f.iw).tickFormat(""));
    f.g.append("g").attr("class", "axis").call(d3.axisLeft(y).ticks(5).tickFormat(d3.format(".0%")).tickSize(0).tickPadding(8)).select(".domain").remove();
    const every = f.iw < 360 ? 2 : 1;
    f.g.append("g").attr("class", "axis").attr("transform", `translate(0,${f.ih})`)
      .call(d3.axisBottom(x).tickSizeOuter(0).tickFormat((d, i) => (i % every ? "" : d)));
    f.g.append("text").attr("class", "label").attr("x", f.iw / 2).attr("y", f.ih + 34).attr("text-anchor", "middle").text("Rank within launch month");
    const color = css("--s1");
    for (const b of buckets) {
      if (b.y == null) continue;
      const bx = x(b.label), bw = x.bandwidth(), h = f.ih - y(b.y), r = Math.min(4, bw / 2, h);
      f.g.append("path").attr("fill", color)
        .attr("d", `M${bx},${f.ih}v${-(h - r)}a${r},${r} 0 0 1 ${r},${-r}h${bw - 2 * r}a${r},${r} 0 0 1 ${r},${r}v${h - r}z`);
      f.g.append("rect").attr("class", "hit bar-hit").attr("x", bx - x.step() * 0.14).attr("width", x.step()).attr("y", 0).attr("height", f.ih)
        .on("pointermove", (ev) => showTip(ev, { title: `Ranks ${b.label}`, rows: [{ color, value: pct(b.y), label: "alive today" }, { value: fmtInt(b.n), label: "sites checked" }, { value: fmtInt(Math.round(b.votes || 0)), label: "median votes" }] }))
        .on("pointerleave", hideTip);
    }
    const first = buckets[0], last = [...buckets].reverse().find((b) => b.y != null);
    for (const b of [first, last]) {
      if (b && b.y != null) f.g.append("text").attr("class", "label strong").attr("x", x(b.label) + x.bandwidth() / 2).attr("y", y(b.y) - 6).attr("text-anchor", "middle").text(pct(b.y));
    }
    f.g.append("line").attr("stroke", css("--ink-2")).attr("stroke-width", 1).attr("x1", 0).attr("x2", f.iw).attr("y1", y(overall)).attr("y2", y(overall));
    $("#popularity-sub").textContent = `Share alive today by rank within launch month. The line marks all ranks together (${pct(overall)}).`;
  }

  // ---------- Hosting ----------
  function renderHosting() {
    const list = filtered().filter((s) => s.status !== "none");
    const rows = d3.rollups(list, (v) => ({ n: v.length, up: v.filter((s) => s.status === "up").length, free: v[0].free }), (s) => s.hostKey)
      .filter(([, r]) => r.n >= MIN_N)
      .map(([key, r]) => ({ label: key, value: r.up / r.n, n: r.n, free: r.free, color: r.free ? css("--s2") : css("--s1") }))
      .sort((a, b) => b.value - a.value).slice(0, 12);
    const node = $("#hosting");
    hbar(node, rows, {
      valueFmt: pct, labelWidth: 110, height: 340,
      tipFor: (r) => ({ title: r.label, rows: [{ color: r.color, value: pct(r.value), label: "alive today" }, { value: fmtInt(r.n), label: "sites" }], note: r.free ? "Free subdomain" : "Own domain" }),
    });
    const lg = el("div", "legend");
    legend(lg, [{ label: "Own domain", color: css("--s1") }, { label: "Free subdomain", color: css("--s2") }], true);
    node.prepend(lg);
  }

  // ---------- Log ----------
  function renderLog() {
    const node = $("#log");
    const from = Math.max(1, dayTs.length - 14);
    const events = [];
    for (const s of filtered()) for (const e of transitions(s.history, from)) events.push({ ...e, s });
    events.sort((a, b) => b.i - a.i || a.s.rank - b.s.rank);
    node.replaceChildren();
    if (!events.length) {
      node.append(el("li", "empty", dayTs.length > 1 ? "No status changes in the last 14 checks." : "Status changes show up here once there are two days of checks."));
      return;
    }
    const fmt = d3.utcFormat("%b %-d");
    for (const e of events.slice(0, 80)) {
      const li = el("li");
      const body = el("div");
      const a = el("a", "site", e.s.title);
      a.href = e.s.url; a.target = "_blank"; a.rel = "noopener";
      const detail = el("div", "detail", `${e.s.domain} · `);
      if (e.kind === "down") {
        detail.append(e.ch === "P" ? el("span", "st-parked", "Parked") : el("span", "st-down", "Went down"));
        if (e.ch !== "P" && e.s.status !== "up" && e.s.reason) detail.append(` · ${REASONS[e.s.reason] || e.s.reason}`);
      } else {
        detail.append(el("span", "st-up", "Back up"));
      }
      body.append(a, detail);
      li.append(el("span", "when", fmt(new Date(dayTs[e.i] * 1000))), body);
      node.append(li);
    }
  }

  // ---------- Table ----------
  function renderTable() {
    const q = state.search.trim().toLowerCase();
    let rows = filtered().filter((s) => !q || s.title.toLowerCase().includes(q) || s.domain.includes(q));
    const order = { up: 0, parked: 1, down: 2, none: 3 };
    const key = state.sort.key;
    const val = (s) => (key === "status" ? order[s.status] : key === "age" ? -s.created : key === "uptime" ? (s.uptime ?? -1) : s[key]);
    rows.sort((a, b) => {
      const va = val(a), vb = val(b);
      const c = typeof va === "string" ? va.localeCompare(vb) : va - vb;
      return c * state.sort.dir || a.cohort.localeCompare(b.cohort) || a.rank - b.rank;
    });
    $("#table-sub").textContent = `${fmtInt(rows.length)} sites${q ? " matching your search" : ""}. Rank is within the launch month.`;
    document.querySelectorAll("#table th[data-sort]").forEach((th) => {
      th.setAttribute("aria-sort", th.dataset.sort === key ? (state.sort.dir > 0 ? "ascending" : "descending") : "none");
    });
    const tbody = $("#table tbody");
    tbody.replaceChildren();
    const colors = { U: css("--up"), B: css("--up"), P: css("--parked"), D: css("--down") };
    for (const s of rows.slice(0, state.limit)) {
      const tr = el("tr");
      tr.append(el("td", "num", `#${s.rank}`));
      const site = el("td");
      const title = el("a", "site-title", s.title);
      title.href = s.post_url; title.target = "_blank"; title.rel = "noopener"; title.title = "Launch post";
      const dom = el("span", "site-domain");
      const link = el("a", null, s.domain);
      link.href = s.url; link.target = "_blank"; link.rel = "noopener";
      dom.append(link, document.createTextNode(` · ${monthLabel(s.cohort, true)}`));
      site.append(title, dom);
      tr.append(site, el("td", null, s.source), el("td", "nowrap", CATEGORY_LABEL[s.category] || s.category),
        el("td", "num", fmtInt(s.votes)), el("td", "num", s.age >= 60 ? `${Math.round(s.age / 30.4)}mo` : `${s.age}d`),
        el("td", "num", s.uptime == null ? "–" : pct(s.uptime)));
      const strip = el("div", "strip");
      const hist = s.history.slice(-30).padStart(30, ".");
      for (const ch of hist) {
        const i = el("i");
        if (colors[ch]) i.style.background = colors[ch];
        strip.append(i);
      }
      strip.setAttribute("aria-label", `Last 30 checks: ${hist.replace(/\./g, "").length} checked`);
      const td = el("td"); td.append(strip); tr.append(td);
      const st = el("span", `status st-${s.status}`, statusText(s));
      if (s.status !== "up" && s.reason && s.reason !== "parked") st.append(el("span", null, ` · ${REASONS[s.reason] || s.reason}`));
      const tdp = el("td"); tdp.append(st); tr.append(tdp);
      tbody.append(tr);
    }
    $("#more").hidden = rows.length <= state.limit;
  }

  // ---------- Wiring ----------
  function renderAll() {
    if (!data) return;
    hideTip();
    $("#count").textContent = `${fmtInt(filtered().length)} sites in view`;
    renderHero(); renderSurvival(); renderCohorts(); renderReasons(); renderWall();
    renderUptime(); renderPopularity(); renderHosting(); renderLog(); renderTable();
  }

  function bindSeg(node, onPick) {
    node.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      node.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
      onPick(b.dataset.value);
    });
  }
  document.querySelectorAll(".filters .seg").forEach((seg) => bindSeg(seg, (v) => {
    state[seg.dataset.filter] = seg.dataset.filter === "tier" ? +v : v;
    state.limit = 50;
    renderAll();
  }));
  bindSeg($("#breakdown"), (v) => { state.breakdown = v; renderSurvival(); });
  $("#source-filter").addEventListener("change", (ev) => { state.source = ev.target.value; state.limit = 50; renderAll(); });
  $("#day-slider").addEventListener("input", (ev) => { state.day = +ev.target.value; renderHero(); renderSurvival(); });
  $("#search").addEventListener("input", (ev) => { state.search = ev.target.value; state.limit = 50; renderTable(); });
  $("#more").addEventListener("click", () => { state.limit += 100; renderTable(); });
  document.querySelectorAll("#table th[data-sort]").forEach((th) => {
    th.tabIndex = 0;
    const pick = () => {
      const k = th.dataset.sort;
      state.sort = { key: k, dir: state.sort.key === k ? -state.sort.dir : (["votes", "uptime"].includes(k) ? -1 : 1) };
      renderTable();
    };
    th.addEventListener("click", pick);
    th.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
  });
  let resizeTimer, lastWidth = 0;
  new ResizeObserver((entries) => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastWidth) return;
    lastWidth = w;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderAll, 120);
  }).observe($("main"));

  fetch("data/tracker.json", { cache: "no-cache" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((raw) => {
      prepare(raw);
      const links = $("#source-links");
      raw.sources.forEach((src, i) => {
        if (i) links.append(document.createTextNode(i === raw.sources.length - 1 ? " and " : ", "));
        const a = el("a", null, src.name); a.href = src.url; a.target = "_blank"; a.rel = "noopener";
        links.append(a);
        const opt = el("option", null, src.name); opt.value = src.name;
        $("#source-filter").append(opt);
      });
      const when = new Date(raw.generated_at);
      $("#checked").textContent = `Checked ${when.toLocaleString("en", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
      renderAll();
    })
    .catch(() => {
      $("#checked").textContent = "No data yet";
      document.querySelectorAll(".chart").forEach((n) => emptyState(n, "The first daily check hasn't run yet."));
    });
})();
