/* RapidPulse — single-page UI (no build step). Hash routes: #/page/arg?k=v */
"use strict";

const S = { app: "all", range: "1h", cfrom: null, cto: null, charts: [], timer: null, meta: null, svcOrder: [], route: null, epq: {} };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const view = () => $("#view");

// ------------------------------------------------------------------ utils
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const num = (v, d = 0) => v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: 0 });
const ms = (v) => v == null ? "—" : v >= 60000 ? (v / 60000).toFixed(1) + " min" : v >= 1000 ? (v / 1000).toFixed(2) + " s" : v >= 10 ? Math.round(v) + " ms" : Number(v).toFixed(1) + " ms";
const pct = (v) => v == null ? "—" : Number(v).toFixed(v < 10 ? 2 : 1) + "%";
const usd = (v) => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: v < 1 ? 4 : 2 });
const bytes = (v) => { if (v == null) return "—"; const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; while (v >= 1024 && i < 4) { v /= 1024; i++; } return v.toFixed(1) + " " + u[i]; };
const tsLabel = (s) => new Date(s * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const nsLabel = (n) => tsLabel(n / 1e9);
const ago = (s) => { const d = Date.now() / 1000 - s; return d < 60 ? Math.round(d) + "s ago" : d < 3600 ? Math.round(d / 60) + "m ago" : d < 86400 ? Math.round(d / 3600) + "h ago" : Math.round(d / 86400) + "d ago"; };
const HEALTH_LABEL = { healthy: "Healthy", warning: "Warning", critical: "Critical", unknown: "No data", ok: "OK", error: "Error", down: "Down", firing: "Firing", resolved: "Resolved", running: "Running", exited: "Exited" };
const status = (h, label) => `<span class="status ${esc(h)}">${esc(label || HEALTH_LABEL[h] || h)}</span>`;
const statusColor = (h) => css({ healthy: "--good", ok: "--good", warning: "--warning", critical: "--critical", error: "--critical", down: "--critical" }[h] || "--unknown");
const shortId = (id) => id ? id.slice(0, 12) + "…" : "";

/* Categorical color follows the entity: slots assigned in a fixed order to known
   services; past eight, everything folds to a neutral "other" color (never cycled). */
function svcColor(name) {
  let i = S.svcOrder.indexOf(name);
  if (i < 0) { S.svcOrder.push(name); i = S.svcOrder.length - 1; }
  return i < 8 ? css(`--series-${i + 1}`) : css("--unknown");
}
const catColor = (i) => i < 8 ? css(`--series-${i + 1}`) : css("--unknown");

function hashState() {
  const h = location.hash.replace(/^#\/?/, "") || "overview";
  const [path, query] = h.split("?");
  const parts = path.split("/").map(decodeURIComponent);
  return { page: parts[0] || "overview", arg: parts.slice(1).join("/"), params: new URLSearchParams(query || "") };
}
function go(page, arg, params) {
  const q = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null)).toString() : "";
  location.hash = `#/${page}${arg ? "/" + encodeURIComponent(arg) : ""}${q}`;
}

/* The selected time window: a preset ("range") or a fixed custom window ("from"/"to", epoch seconds).
   A drill-down window in the URL (#/traces?from=..&to=..) takes precedence. */
function baseTime() {
  if (S.range === "custom" && S.cfrom && S.cto) return { from: String(S.cfrom), to: String(S.cto === "now" ? Math.floor(Date.now() / 1000) : S.cto) };
  return { range: S.range === "custom" ? "1h" : S.range };
}
function timeParams() {
  const hp = S.route?.params;
  return hp?.get("from") ? { from: hp.get("from"), to: hp.get("to") || "" } : baseTime();
}
function exportQuery(extra = {}) {
  return new URLSearchParams({ ...(S.app !== "all" ? { app: S.app } : {}), ...timeParams(), tz: String(new Date().getTimezoneOffset()), ...extra }).toString();
}
function toast(msg, ms_ = 4000) {
  const el = document.createElement("div"); el.className = "toast"; el.textContent = msg; document.body.appendChild(el);
  setTimeout(() => el.remove(), ms_);
}
/* Fetch a generated file and save it, so a failure shows a message instead of a broken download. */
async function download(url, label) {
  toast(`Preparing ${label}…`, 2500);
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error((await r.text()) || r.statusText);
    const name = (r.headers.get("content-disposition") || "").match(/filename="?([^";]+)"?/)?.[1] || "rapidpulse.pdf";
    const a = document.createElement("a"); a.href = URL.createObjectURL(await r.blob()); a.download = name;
    document.body.appendChild(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
  } catch (e) { toast(`Could not create ${label}: ${e.message}`, 7000); }
}

async function api(path, params = {}, opts = {}) {
  const p = new URLSearchParams();
  if (S.app !== "all") p.set("app", S.app);
  if (!opts.noTime) for (const [k, v] of Object.entries(opts.useRouteWindow === false ? baseTime() : timeParams())) p.set(k, v);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null) p.set(k, v);
  const r = await fetch(path + (p.toString() ? "?" + p : ""), opts.init);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}
const post = (path, body, method = "POST") => fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined }).then(async (r) => { if (!r.ok) throw new Error(await r.text()); return r.json(); });

// ------------------------------------------------------------------ tables
function table(cols, rows, { onClick, empty = "No data in this time range", id, sortKey, maxRows } = {}) {
  const tid = id || "t" + Math.random().toString(36).slice(2, 8);
  if (!rows || !rows.length) return `<div class="empty">${esc(empty)}</div>`;
  const shown = maxRows ? rows.slice(0, maxRows) : rows;
  TABLES[tid] = { cols, rows, onClick, sortKey, maxRows };
  const head = cols.map((c, i) => `<th class="${c.num ? "num" : ""}" data-col="${i}" title="Sort">${esc(c.label)}</th>`).join("");
  const body = shown.map((r, i) => `<tr class="${onClick ? "click" : ""}" data-i="${i}">${cols.map((c) => `<td class="${c.num ? "num" : ""} ${c.cls || ""}">${c.html ? c.html(r) : esc(c.fmt ? c.fmt(r[c.key], r) : r[c.key] ?? "—")}</td>`).join("")}</tr>`).join("");
  return `<div class="tbl-wrap"><table data-tid="${tid}"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
const TABLES = {};
document.addEventListener("click", (e) => {
  const th = e.target.closest("th[data-col]");
  if (th) {
    const tbl = th.closest("table"); const t = TABLES[tbl.dataset.tid]; if (!t) return;
    const c = t.cols[+th.dataset.col]; const key = c.sortBy || c.key; if (!key) return;
    const dir = t.sortKey === key ? -1 : 1; t.sortKey = dir === 1 ? key : null;
    t.rows.sort((a, b) => { const x = typeof key === "function" ? key(a) : a[key], y = typeof key === "function" ? key(b) : b[key]; return (x > y ? -1 : x < y ? 1 : 0) * dir; });
    tbl.closest(".tbl-wrap").outerHTML = table(t.cols, t.rows, { onClick: t.onClick, id: tbl.dataset.tid, sortKey: t.sortKey, maxRows: t.maxRows });
    return;
  }
  const tr = e.target.closest("tr.click");
  if (tr && !e.target.closest("a,button,input,select")) {
    const t = TABLES[tr.closest("table").dataset.tid];
    if (t?.onClick) t.onClick(t.rows[+tr.dataset.i]);
  }
});

// ------------------------------------------------------------------ charts
function baseOpt() {
  const txt = css("--text-secondary"), grid = css("--grid");
  return {
    animation: false, textStyle: { color: txt, fontFamily: "Segoe UI, system-ui, sans-serif" },
    grid: { left: 52, right: 16, top: 30, bottom: 26 },
    tooltip: { trigger: "axis", backgroundColor: css("--surface-2"), borderColor: css("--border"), textStyle: { color: css("--text-primary"), fontSize: 12 }, axisPointer: { type: "line", lineStyle: { color: css("--text-muted") } } },
    xAxis: { type: "time", splitNumber: 4, axisLine: { lineStyle: { color: grid } }, axisLabel: { color: txt, hideOverlap: true }, splitLine: { show: false } },
    yAxis: { type: "value", splitLine: { lineStyle: { color: grid } }, axisLabel: { color: txt } },
    legend: { top: 0, right: 0, textStyle: { color: txt, fontSize: 11 }, icon: "roundRect", itemWidth: 10, itemHeight: 10 },
  };
}
function mkChart(el, opt) {
  if (typeof el === "string") el = $(el);
  if (!el) return null;
  const c = echarts.init(el, null, { renderer: "canvas" });
  c.setOption(opt);
  S.charts.push(c);
  return c;
}
/** One-axis time series. series: [{name, data:[[t_ms, v]], color, area, stack}] */
function lineChart(el, series, { unit = "", fmt = (v) => num(v, 2), drill, stacked = false, bars = false } = {}) {
  const o = baseOpt();
  o.legend.show = series.length > 1;
  o.yAxis.axisLabel.formatter = (v) => fmt(v) + (unit && !["ms", "%", "$"].includes(unit) ? "" : "");
  o.tooltip.valueFormatter = (v) => v == null ? "—" : fmt(v);
  o.series = series.map((s) => ({
    name: s.name, type: bars ? "bar" : "line", data: s.data, showSymbol: false, symbolSize: 8, connectNulls: false,
    stack: stacked ? "s" : undefined, barMaxWidth: 14, barGap: "10%",
    lineStyle: { width: 2, color: s.color, type: s.dashed ? "dashed" : "solid" }, itemStyle: { color: s.color, borderRadius: stacked ? 0 : [3, 3, 0, 0], borderColor: css("--surface-1"), borderWidth: bars && stacked ? 1 : 0 },
    areaStyle: s.area ? { color: s.color, opacity: 0.12 } : undefined, emphasis: { focus: "series" },
  }));
  const c = mkChart(el, o);
  if (c && drill) {
    c.getZr().on("click", (p) => {
      const pt = c.convertFromPixel({ seriesIndex: 0 }, [p.offsetX, p.offsetY]);
      if (pt && pt[0]) drill(Math.round(pt[0] / 1000));
    });
    c.getZr().setCursorStyle?.("pointer");
  }
  return c;
}
const toPts = (times, points) => times.map((t, i) => [t * 1000, points[i]]);
function seriesOf(res, metric, group = "") { return (res.series.find((s) => s.metric === metric && s.group === group) || { points: [] }).points; }

function drillToTraces(params) {
  return (t) => { const step = (S.lastStep || 60); go("traces", "", { ...params, from: t - step, to: t + step * 2 }); };
}

/* Tile with the model of the most recent LLM call; refreshed with the page, so a model change shows by itself. */
function modelTile(cm) {
  if (!cm) return { label: "Current AI model", value: "—", foot: "No LLM calls recorded yet", href: "#/ai" };
  const others = (cm.others || []).slice(0, 2).map((o) => `${esc(o.label)} (${num(o.calls)})`).join(", ");
  return { label: "Current AI model", value: esc(cm.label), href: "#/ai",
    foot: `<span class="mono" title="${esc(cm.model)}${cm.provider ? " · " + esc(cm.provider) : ""} · ${esc(cm.service || "")}">${esc(cm.model)}</span> · ${ago(cm.last_seen)}${others ? `<br>also in the last hour: ${others}` : ""}` };
}

function tiles(items) {
  return `<div class="tiles">${items.map((t) => `<div class="tile ${t.href ? "clickable" : ""}" ${t.href ? `onclick="location.hash='${t.href}'"` : ""}>
    <div class="label">${esc(t.label)}</div><div class="value">${t.value}${t.unit ? `<small>${esc(t.unit)}</small>` : ""}</div>
    ${t.foot ? `<div class="foot">${t.foot}</div>` : ""}</div>`).join("")}</div>`;
}
const card = (title, sub, body, extra = "") => `<div class="card"><div class="card-head"><div><h2>${esc(title)}</h2>${sub ? `<p class="sub">${sub}</p>` : ""}</div>${extra}</div>${body}</div>`;

// ------------------------------------------------------------------ drawer
function openDrawer(title, html) { $("#drawerTitle").textContent = title; $("#drawerBody").innerHTML = html; $("#drawer").hidden = false; }
$("#drawerClose").onclick = () => { $("#drawer").hidden = true; };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });
const kv = (obj) => `<div class="kv">${Object.entries(obj).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => `<div>${esc(k)}</div><div class="mono">${esc(typeof v === "object" ? JSON.stringify(v) : v)}</div>`).join("")}</div>`;

// ------------------------------------------------------------- captured HTTP request (headers + payload)
const isRequestAttr = (k) => /^http\.request\.(header\.|body$)/i.test(k);
const withoutRequestAttrs = (a) => Object.fromEntries(Object.entries(a || {}).filter(([k]) => !isRequestAttr(k)));
function prettyBody(body) {
  if (body == null) return "";
  try { return JSON.stringify(JSON.parse(body), null, 2); } catch { return String(body); }
}
function curlOf(r) {
  const q = (s) => `'${String(s).replace(/'/g, `'\\''`)}'`;
  const skip = /^(host|content-length|connection|accept-encoding|traceparent|tracestate|baggage)$/i;
  const parts = [`curl -X ${r.method || "GET"} ${q(r.url || "")}`];
  for (const [k, v] of Object.entries(r.headers || {})) {
    if (!skip.test(k)) parts.push(`  -H ${q(`${k}: ${v === "[redacted]" ? "<" + k + ">" : v}`)}`);
  }
  if (r.body != null && !/… \[truncated/.test(r.body)) parts.push(`  --data-raw ${q(r.body)}`);
  return parts.join(" \\\n");
}
function copyText(text, what) {
  const done = () => toast(`${what} copied to the clipboard`);
  const legacy = () => {      // plain-HTTP dashboards (no Clipboard API), or when the browser refuses it
    const ta = Object.assign(document.createElement("textarea"), { value: text });
    ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.focus(); ta.select();
    try { if (document.execCommand("copy")) done(); else toast("Copy failed: select the text and press Ctrl+C"); }
    catch { toast("Copy failed: select the text and press Ctrl+C"); } finally { ta.remove(); }
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, legacy);
  else legacy();
}
const NO_CAPTURE = `<p class="muted">No headers or payload were captured for this request. Turn on request capture for the
  service (Installation Guide, section 6) — Python containers instrumented by <span class="mono">instrument-container.py</span> capture them by default.</p>`;
function requestBlock(r, key) {
  if (!r) return NO_CAPTURE;
  const hdrs = Object.entries(r.headers || {});
  const size = r.body_size != null ? ` · ${num(+r.body_size)} bytes` : "";
  return `<div class="req">
    <div class="req-line"><span class="pill">${esc(r.method || "?")}</span> <span class="mono">${esc(r.url || "")}</span>${r.status ? ` <span class="pill ${r.status >= 400 ? "bad" : ""}">${esc(r.status)}</span>` : ""}
      <span class="req-actions"><a href="#" class="btn" data-copy-curl="${esc(key)}">Copy as cURL</a>${r.body != null ? ` <a href="#" class="btn" data-copy-body="${esc(key)}">Copy payload</a>` : ""}</span></div>
    <h3>Request headers <span class="muted">(${hdrs.length})</span></h3>
    ${hdrs.length ? `<div class="kv">${hdrs.map(([k, v]) => `<div>${esc(k)}</div><div class="mono ${v === "[redacted]" ? "muted" : ""}">${esc(v)}</div>`).join("")}</div>` : `<p class="muted">Not captured.</p>`}
    <h3>Request payload <span class="muted">${esc(r.content_type || "")}${size}</span></h3>
    ${r.body != null ? `<pre class="req-body">${esc(prettyBody(r.body))}</pre>` : `<p class="muted">${/^(GET|HEAD|DELETE|OPTIONS)$/i.test(r.method || "") ? "No payload (" + esc(r.method) + " request)." : "No payload captured."}</p>`}
  </div>`;
}
function bindRequestCopy(root, lookup) {
  root.querySelectorAll("[data-copy-curl]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); copyText(curlOf(lookup(a.dataset.copyCurl)), "cURL command"); }));
  root.querySelectorAll("[data-copy-body]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); copyText(lookup(a.dataset.copyBody).body, "Payload"); }));
}

// ================================================================== PAGES
const PAGES = {};

// ------------------------------------------------------------- Overview
PAGES.overview = { title: "Overview", async render() {
  const d = await api("/api/overview");
  const k = d.kpi;
  S.lastStep = d.series.step;
  view().innerHTML = `
    ${tiles([
      { label: "Requests (traces)", value: num(k.traces), foot: `${num(k.failed_traces)} failed`, href: "#/traces" },
      { label: "Failed requests", value: pct(k.trace_error_rate), foot: k.trace_error_rate >= 5 ? status("critical", "Above 5%") : status("healthy", "Within target"), href: "#/traces?status=error" },
      { label: "End-to-end p95", value: ms(k.trace_p95_ms), foot: `avg ${ms(k.trace_avg_ms)}`, href: "#/traces?sort=slowest" },
      modelTile(d.current_model),
      { label: "LLM calls", value: num(k.llm_calls), foot: `${num(k.llm_tokens)} tokens`, href: "#/ai" },
      { label: "LLM cost", value: usd(k.llm_cost), foot: `MCP calls ${num(k.mcp_calls)}`, href: "#/ai" },
      { label: "DB queries", value: num(k.db_calls), foot: `p95 ${ms(k.db_p95_ms)}`, href: "#/database" },
      { label: "Alerts firing", value: num(k.alerts_firing), foot: k.alerts_critical ? status("critical", `${k.alerts_critical} critical`) : status("healthy", "None critical"), href: "#/alerts" },
      { label: "Hosts / containers", value: `${num(k.hosts - k.hosts_down)}/${num(k.hosts)}`, foot: `containers ${k.containers - k.containers_unhealthy}/${k.containers} healthy`, href: "#/infra" },
    ])}
    <div class="grid g3">
      ${card("Throughput", "Requests per minute, all service entry points · click to drill into traces", `<div class="chart" id="cRpm"></div>`)}
      ${card("Error rate", "% of entry requests failing", `<div class="chart" id="cErr"></div>`)}
      ${card("Latency p95", "95th percentile response time across services", `<div class="chart" id="cP95"></div>`)}
    </div>
    ${card("Service health", "Every service that received traffic in the range · click for details", table([
      { label: "Service", key: "service", html: (r) => `<span class="chip" style="background:${svcColor(r.service)}"></span>${esc(r.display || r.service)} <span class="muted">${esc(r.tier || "")}</span>` },
      { label: "Health", key: "health", html: (r) => status(r.health) },
      { label: "Req/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) },
      { label: "Error %", key: "error_rate", num: 1, html: (r) => `<span class="${r.error_rate >= 5 ? "bad" : ""}">${pct(r.error_rate)}</span>` },
      { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "p99", key: "p99_ms", num: 1, fmt: ms },
      { label: "Apdex", key: "apdex", num: 1, fmt: (v) => v == null ? "—" : v.toFixed(2) },
      { label: "Hosts", key: "hosts", html: (r) => `<span class="muted">${esc(r.hosts || "")}</span>` },
    ], d.services, { onClick: (r) => go("service", r.service) }))}
    <div class="grid g2">
      ${card("Slowest endpoints (p95)", "", table([
        { label: "Service", key: "service" }, { label: "Endpoint", key: "operation", cls: "wrap" },
        { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }], d.top_slow,
        { onClick: (r) => go("traces", "", { service: r.service, operation: r.operation.split(" ").slice(1).join(" ") || r.operation, sort: "slowest" }) }))}
      ${card("Most errors", "Grouped by service and operation", table([
        { label: "Service", key: "service" }, { label: "Operation", key: "operation", cls: "wrap" },
        { label: "Errors", key: "errors", num: 1, html: (r) => `<span class="bad">${num(r.errors)}</span>` }, { label: "Error %", key: "error_rate", num: 1, fmt: pct }], d.top_errors,
        { onClick: (r) => go("traces", "", { service: r.service, status: "error" }), empty: "No errors 🎉" }))}
    </div>`;
  const t = d.series.times;
  lineChart("#cRpm", [{ name: "req/min", data: toPts(t, seriesOf(d.series, "rpm")), color: css("--series-1"), area: true }], { fmt: (v) => num(v, 1), drill: drillToTraces({}) });
  lineChart("#cErr", [{ name: "error %", data: toPts(t, seriesOf(d.series, "error_rate")), color: css("--series-8"), area: true }], { fmt: (v) => num(v, 1) + "%", drill: drillToTraces({ status: "error" }) });
  lineChart("#cP95", [{ name: "p95", data: toPts(t, seriesOf(d.series, "p95_ms")), color: css("--series-7") }], { fmt: ms, drill: drillToTraces({ sort: "slowest" }) });
} };

// ------------------------------------------------------------- Service map
PAGES.map = { title: "Service map", async render() {
  const d = await api("/api/map");
  view().innerHTML = `
    <div class="card"><div class="card-head"><div><h2>Service dependency map</h2>
      <p class="sub">Derived automatically from distributed traces. Node outline = health; edge label = calls/min · avg latency · error %. Click a node or an edge.</p></div>
      <div class="legend">${["healthy", "warning", "critical", "unknown"].map((h) => status(h)).join("")}
        <span>▢ service</span><span>◯ database</span><span>◇ LLM</span><span>△ MCP / tools</span><span>⬡ external</span></div></div>
      <div id="mapChart" style="height:calc(100vh - 230px);min-height:480px"></div></div>`;
  if (!d.nodes.length) { $("#mapChart").innerHTML = `<div class="empty">No traffic yet. Point an OpenTelemetry exporter at this server.</div>`; return; }
  // Layered layout: column = longest call path from an entry service.
  const level = {}; d.nodes.forEach((n) => (level[n.id] = 0));
  for (let k = 0; k < d.nodes.length; k++) d.edges.forEach((e) => { if (e.source !== e.target && level[e.target] < level[e.source] + 1 && level[e.source] + 1 < 12) level[e.target] = level[e.source] + 1; });
  const cols = {}; d.nodes.forEach((n) => (cols[level[n.id]] ||= []).push(n));
  const order = { service: 0, mcp: 1, llm: 2, database: 3, external: 4 };
  Object.values(cols).forEach((c) => c.sort((a, b) => (order[a.type] ?? 0) - (order[b.type] ?? 0) || a.id.localeCompare(b.id)));
  const symbol = { service: "roundRect", database: "circle", llm: "diamond", mcp: "triangle", external: "path://M50 0 L100 25 L100 75 L50 100 L0 75 L0 25 Z" };
  // Positions in the container's own pixel space, so ECharts' fit-to-view scales uniformly (no stretching).
  const box = $("#mapChart").getBoundingClientRect();
  const maxLevel = Math.max(1, ...Object.values(level));
  const padX = 110, padY = 70;
  const nodes = d.nodes.map((n) => {
    const col = cols[level[n.id]], i = col.indexOf(n);
    const px = padX + (level[n.id] / maxLevel) * (box.width - 2 * padX);
    const py = padY + ((i + 0.5) / col.length) * (box.height - 2 * padY) + (col.length === 1 ? 0 : (level[n.id] % 2) * 18);
    const sub = n.rpm ? `${num(n.rpm, 1)} rpm · ${ms(n.avg_ms)} · ${pct(n.error_rate)}` : "no inbound traffic";
    return { id: n.id, name: n.id, x: px, y: py, raw: n,
      symbol: symbol[n.type] || "roundRect", symbolSize: n.type === "service" ? [150, 48] : 46,
      itemStyle: { color: css("--surface-2"), borderColor: statusColor(n.health), borderWidth: 3 },
      label: { show: true, position: n.type === "service" ? "inside" : "bottom", color: css("--text-primary"), fontSize: 12,
        formatter: `{b|${(n.label || n.id).replace(/[{}|]/g, "")}}\n{s|${sub}}`, rich: { b: { fontWeight: 600, fontSize: 12, color: css("--text-primary") }, s: { fontSize: 10.5, color: css("--text-secondary"), padding: [2, 0, 0, 0] } } } };
  });
  // Invisible corner anchors pin the data bounding box to the panel, so the fit-to-view scale is exactly 1.
  for (const [x, y] of [[0, 0], [box.width, box.height]]) nodes.push({ id: `__anchor${x}`, name: "", x, y, symbolSize: 0, label: { show: false }, tooltip: { show: false }, silent: true, raw: null });
  const maxRpm = Math.max(1, ...d.edges.map((e) => e.rpm));
  const links = d.edges.map((e) => ({ source: e.source, target: e.target, raw: e,
    lineStyle: { width: 1.5 + 4 * Math.sqrt(e.rpm / maxRpm), color: e.error_rate >= 5 ? css("--critical") : e.error_rate >= 1 ? css("--warning") : css("--text-muted"), curveness: 0.08, opacity: 0.85 },
    // Labels on service-to-service hops (the request journey); dependency edges label on hover.
    label: { show: e.type === "service" || e.error_rate >= 5, formatter: `${num(e.rpm, 1)}/min · ${ms(e.avg_ms)}${e.error_rate ? " · " + pct(e.error_rate) : ""}`, fontSize: 10.5, color: css("--text-secondary"), backgroundColor: css("--surface-1"), padding: [1, 3] },
    emphasis: { label: { show: true } } }));
  const c = mkChart("#mapChart", {
    animation: false,
    tooltip: { backgroundColor: css("--surface-2"), borderColor: css("--border"), textStyle: { color: css("--text-primary"), fontSize: 12 },
      formatter: (p) => !p.data?.raw ? "" : p.dataType === "edge" ? `<b>${esc(p.data.source)} → ${esc(p.data.target)}</b><br>${num(p.data.raw.calls)} calls · ${num(p.data.raw.rpm, 1)}/min<br>avg ${ms(p.data.raw.avg_ms)} · errors ${pct(p.data.raw.error_rate)}`
        : `<b>${esc(p.data.raw.label)}</b> (${esc(p.data.raw.type)})<br>${HEALTH_LABEL[p.data.raw.health]}<br>${num(p.data.raw.rpm, 1)} rpm · avg ${ms(p.data.raw.avg_ms)} · p95 ${ms(p.data.raw.p95_ms)} · err ${pct(p.data.raw.error_rate)}${p.data.raw.hosts?.length ? "<br>hosts: " + esc(p.data.raw.hosts.join(", ")) : ""}` },
    series: [{ type: "graph", layout: "none", roam: true, draggable: true, data: nodes, links, edgeSymbol: ["none", "arrow"], edgeSymbolSize: 9,
      emphasis: { focus: "adjacency" }, left: 0, top: 0, width: box.width, height: box.height }],
  });
  c.on("click", (p) => {
    if (p.dataType === "node" && p.data.raw) {
      const n = p.data.raw;
      const actions = n.type === "service" ? `<a href="#/service/${encodeURIComponent(n.id)}">Open service →</a> · ` : n.type === "llm" ? `<a href="#/ai">AI monitoring →</a> · ` : n.type === "database" ? `<a href="#/database">Database →</a> · ` : "";
      const inE = d.edges.filter((e) => e.target === n.id), outE = d.edges.filter((e) => e.source === n.id);
      openDrawer(n.label, `<div>${status(n.health)} <span class="pill">${esc(n.type)}</span></div>
        ${kv({ "Requests / min": num(n.rpm, 2), "Avg latency": ms(n.avg_ms), "p95 latency": ms(n.p95_ms), "Error rate": pct(n.error_rate), Hosts: (n.hosts || []).join(", "), OS: (n.os || []).join(", "), Containers: (n.containers || []).join(", ") })}
        <div>${actions}<a href="#/traces?${n.type === "service" ? "service=" + encodeURIComponent(n.id) : ""}">Traces →</a></div>
        ${card("Called by", "", table([{ label: "Caller", key: "source" }, { label: "Calls/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "Error %", key: "error_rate", num: 1, fmt: pct }], inE, { empty: "Entry point" }))}
        ${card("Calls", "", table([{ label: "Dependency", key: "target" }, { label: "Calls/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "Error %", key: "error_rate", num: 1, fmt: pct }], outE, { empty: "Leaf node" }))}`);
    } else if (p.dataType === "edge") {
      const tgtIsService = d.nodes.find((n) => n.id === p.data.target)?.type === "service";
      go("traces", "", { service: tgtIsService ? p.data.target : p.data.source });
    }
  });
} };

// ------------------------------------------------------------- Services
PAGES.services = { title: "Services & APIs", async render() {
  const [svcs, eps] = await Promise.all([api("/api/services"), api("/api/endpoints")]);
  view().innerHTML = card("Services", "Entry-point traffic per service", table([
    { label: "Service", key: "service", html: (r) => `<span class="chip" style="background:${svcColor(r.service)}"></span>${esc(r.display || r.service)}` },
    { label: "Health", key: "health", html: (r) => status(r.health) }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
    { label: "Req/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) }, { label: "Errors", key: "errors", num: 1, fmt: (v) => num(v) },
    { label: "Error %", key: "error_rate", num: 1, fmt: pct }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p50", key: "p50_ms", num: 1, fmt: ms },
    { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "p99", key: "p99_ms", num: 1, fmt: ms }, { label: "Max", key: "max_ms", num: 1, fmt: ms },
    { label: "Hosts / containers", key: "hosts", html: (r) => `<span class="muted">${esc([r.hosts, r.containers].filter(Boolean).join(" · "))}</span>` },
  ], svcs, { onClick: (r) => go("service", r.service) })) + endpointCard("API endpoints", "All server endpoints · status codes from the HTTP semantic conventions", eps, true);
  bindEndpointSearch(eps, true);
} };

/* Endpoint table with a search box: every word must match the service or the endpoint (method + route).
   The text survives auto-refresh, per page. */
function endpointMatches(rows, text) {
  const words = (text || "").toLowerCase().split(/\s+/).filter(Boolean);
  return words.length ? rows.filter((r) => { const h = `${r.service || ""} ${r.operation || ""}`.toLowerCase(); return words.every((w) => h.includes(w)); }) : rows;
}
const epKey = () => S.route.page + "/" + (S.route.arg || "");
const epCount = (n, total, q) => q ? `${n} of ${total} endpoints` : `${total} endpoints`;
function endpointCard(title, sub, rows, withService) {
  const qv = S.epq[epKey()] || "";
  const shown = endpointMatches(rows, qv);
  const search = `<div class="toolbar"><span class="muted" id="epCount">${epCount(shown.length, rows.length, qv)}</span>
    <input type="search" class="ep-search" id="epSearch" placeholder="Search endpoints, e.g. POST chat" value="${esc(qv)}" aria-label="Search API endpoints"></div>`;
  const body = shown.length || !qv ? endpointTable(shown, withService) : `<div class="empty">No endpoint matches “${esc(qv)}”.</div>`;
  return card(title, sub, `<div id="epTable">${body}</div>`, search);
}
function bindEndpointSearch(rows, withService) {
  const box = $("#epSearch"); if (!box) return;
  box.addEventListener("input", () => {
    S.epq[epKey()] = box.value;
    const shown = endpointMatches(rows, box.value);
    $("#epTable").innerHTML = shown.length || !box.value ? endpointTable(shown, withService) : `<div class="empty">No endpoint matches “${esc(box.value)}”.</div>`;
    $("#epCount").textContent = epCount(shown.length, rows.length, box.value);
  });
}

function codesHtml(codes) {
  return Object.entries(codes || {}).sort().map(([c, n]) => `<span class="pill" title="${n} responses">${c[0] >= "5" ? "⛔ " : c[0] === "4" ? "⚠ " : ""}${esc(c)}: ${num(n)}</span>`).join(" ");
}
function endpointTable(rows, withService) {
  return table([
    ...(withService ? [{ label: "Service", key: "service" }] : []),
    { label: "Endpoint", key: "operation", cls: "wrap mono" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
    { label: "Req/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) }, { label: "Error %", key: "error_rate", num: 1, html: (r) => `<span class="${r.error_rate >= 5 ? "bad" : ""}">${pct(r.error_rate)}</span>` },
    { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "p99", key: "p99_ms", num: 1, fmt: ms },
    { label: "Status codes", key: "codes", html: (r) => codesHtml(r.codes) },
  ], rows, { onClick: (r) => go("traces", "", { service: r.service || S.route.arg, operation: r.operation.split(" ").slice(1).join(" ") || r.operation }) });
}

PAGES.service = { title: "Service", async render(arg) {
  const d = await api(`/api/services/${encodeURIComponent(arg)}`);
  const s = d.summary || {};
  S.lastStep = d.series.step;
  $("#pageTitle").textContent = `Service · ${arg}`;
  const t = d.series.times;
  view().innerHTML = `
    ${tiles([{ label: "Calls", value: num(s.calls) }, { label: "Req/min", value: num(s.rpm, 1) }, { label: "Error rate", value: pct(s.error_rate) },
      { label: "Avg", value: ms(s.avg_ms) }, { label: "p95", value: ms(s.p95_ms) }, { label: "p99", value: ms(s.p99_ms) }, { label: "Apdex", value: s.apdex == null ? "—" : s.apdex.toFixed(2) }])}
    <div class="grid g3">
      ${card("Throughput", "req/min · click to drill down", `<div class="chart" id="sRpm"></div>`)}
      ${card("Error rate", "%", `<div class="chart" id="sErr"></div>`)}
      ${card("Response time", "avg / p95 / p99", `<div class="chart" id="sLat"></div>`)}
    </div>
    ${endpointCard("Endpoints", "Incoming requests to this service", d.endpoints, false)}
    <div class="grid g2">
      ${card("Downstream calls", "Exit calls from this service to APIs, databases, LLMs and MCP tools", table([
        { label: "Type", key: "category", html: (r) => `<span class="pill">${esc(r.category)}</span>` }, { label: "Target", key: "peer" },
        { label: "Operation", key: "operation", cls: "wrap mono" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
        { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "Error %", key: "error_rate", num: 1, fmt: pct }], d.exits, { maxRows: 40 }))}
      ${card("Callers", "Services calling this one", table([{ label: "Caller", key: "name" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
        { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "Errors", key: "errors", num: 1, fmt: (v) => num(v) }], d.deps_in, { empty: "Entry point (called by users)" }))}
    </div>
    <div class="grid g2">
      ${card("Errors", "Grouped by operation and type", table([{ label: "Operation", key: "operation", cls: "wrap" }, { label: "Type", key: "error_type" },
        { label: "Message", key: "message", cls: "wrap" }, { label: "Count", key: "n", num: 1, fmt: (v) => num(v) }, { label: "Last", key: "last_ns", fmt: (v) => ago(v / 1e9) }],
        d.errors, { onClick: (r) => go("trace", r.sample_trace), empty: "No errors 🎉" }))}
      ${card("Slowest traces", "", traceTable(d.slow_traces))}
    </div>
    ${card("Runs on", "Hosts, containers and versions seen in spans", table([{ label: "Host", key: "host" }, { label: "Container", key: "container" }, { label: "Version", key: "service_version" }], d.hosts, { onClick: () => go("infra") }))}`;
  bindEndpointSearch(d.endpoints, false);
  lineChart("#sRpm", [{ name: "req/min", data: toPts(t, seriesOf(d.series, "rpm")), color: svcColor(arg), area: true }], { fmt: (v) => num(v, 1), drill: drillToTraces({ service: arg }) });
  lineChart("#sErr", [{ name: "error %", data: toPts(t, seriesOf(d.series, "error_rate")), color: css("--series-8"), area: true }], { fmt: (v) => num(v, 1) + "%", drill: drillToTraces({ service: arg, status: "error" }) });
  lineChart("#sLat", [{ name: "avg", data: toPts(t, seriesOf(d.series, "avg_ms")), color: css("--series-1") }, { name: "p95", data: toPts(t, seriesOf(d.series, "p95_ms")), color: css("--series-2") },
    { name: "p99", data: toPts(t, seriesOf(d.series, "p99_ms")), color: css("--series-3") }], { fmt: ms, drill: drillToTraces({ service: arg, sort: "slowest" }) });
} };

// ------------------------------------------------------------- Traces
function traceTable(rows) {
  return table([
    { label: "Time", key: "start_ns", fmt: (v) => nsLabel(v) },
    { label: "Root", key: "root_name", html: (r) => `<span class="chip" style="background:${svcColor(r.root_service)}"></span><b>${esc(r.root_service)}</b> <span class="mono">${esc(r.root_name)}</span>`, cls: "wrap" },
    { label: "Duration", key: "duration_ms", num: 1, fmt: ms }, { label: "Spans", key: "span_count", num: 1 },
    { label: "Services", key: "services", sortBy: (r) => r.services.length, html: (r) => r.services.map((s) => `<span class="chip" title="${esc(s)}" style="background:${svcColor(s)}"></span>`).join("") },
    { label: "Status", key: "error_count", html: (r) => r.error_count ? status("error", `${r.error_count} error span${r.error_count > 1 ? "s" : ""}`) : status("ok") },
    { label: "Trace ID", key: "trace_id", html: (r) => `<span class="mono muted">${esc(shortId(r.trace_id))}</span>` },
    { label: "Report", key: "trace_id", sortBy: () => 0, html: (r) => `<a href="#" class="trace-pdf" data-trace="${esc(r.trace_id)}" title="Download this trace as a PDF report">⤓ PDF</a>` },
  ], rows, { onClick: (r) => go("trace", r.trace_id), empty: "No traces match. Widen the time range or relax the filters." });
}

PAGES.traces = { title: "Traces", async render() {
  const p = S.route.params;
  const f = { service: p.get("service") || "", status: p.get("status") || "", min_ms: p.get("min_ms") || "", q: p.get("q") || "", attr: p.get("attr") || "", operation: p.get("operation") || "", sort: p.get("sort") || "recent" };
  const [rows, meta] = await Promise.all([api("/api/traces", { ...f, limit: 300 }), S.meta ? S.meta : api("/api/meta", {}, { noTime: true })]);
  const services = [...new Set(meta.services.map((s) => s.service))];
  const win = p.get("from") ? `<span class="pill">Window ${tsLabel(+p.get("from"))} → ${tsLabel(+p.get("to"))} <a href="#" id="clearWin">✕</a></span>` : "";
  view().innerHTML = `
    <div class="card"><form class="filters" id="tf">
      <label>Service<select name="service"><option value="">Any</option>${services.map((s) => `<option ${s === f.service ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
      <label>Status<select name="status"><option value="">Any</option><option value="error" ${f.status === "error" ? "selected" : ""}>Errors only</option><option value="ok" ${f.status === "ok" ? "selected" : ""}>Successful</option></select></label>
      <label>Min duration (ms)<input type="number" name="min_ms" value="${esc(f.min_ms)}" style="width:110px"></label>
      <label>Endpoint contains<input type="text" name="operation" value="${esc(f.operation)}" placeholder="/api/chat"></label>
      <label>Trace ID / text<input type="text" name="q" value="${esc(f.q)}" placeholder="4bf92f35…"></label>
      <label>Attribute / payload contains<input type="text" name="attr" value="${esc(f.attr)}" placeholder="user@corp.com, conversation id or payload text"></label>
      <label>Sort<select name="sort"><option value="recent">Most recent</option><option value="slowest" ${f.sort === "slowest" ? "selected" : ""}>Slowest</option></select></label>
      <button class="primary">Search</button> ${win}
    </form></div>
    ${card("Trace distribution", "Each dot is one request · y = end-to-end duration · ✕ = failed · click a dot to open its trace", `<div class="chart tall" id="scatter"></div>`)}
    ${card(`${rows.length} traces`, "", traceTable(rows))}`;
  $("#tf").onsubmit = (e) => { e.preventDefault(); const fd = Object.fromEntries(new FormData(e.target)); if (p.get("from")) { fd.from = p.get("from"); fd.to = p.get("to"); } go("traces", "", fd); };
  $("#clearWin")?.addEventListener("click", (e) => { e.preventDefault(); const fd = Object.fromEntries(new FormData($("#tf"))); go("traces", "", fd); });
  const o = baseOpt();
  o.tooltip = { ...o.tooltip, trigger: "item", formatter: (x) => `${esc(x.data[2])}<br>${ms(x.data[1])} · ${x.data[3] ? "failed" : "ok"}` };
  o.yAxis.axisLabel.formatter = (v) => ms(v);
  o.legend.show = true;
  o.series = [
    { name: "ok", type: "scatter", symbolSize: 8, itemStyle: { color: css("--series-1"), opacity: 0.75, borderColor: css("--surface-1"), borderWidth: 1 }, data: rows.filter((r) => !r.error_count).map((r) => [r.start_ns / 1e6, r.duration_ms, `${r.root_service} ${r.root_name}`, 0, r.trace_id]) },
    { name: "failed", type: "scatter", symbol: "path://M0 0 L10 10 M10 0 L0 10", symbolSize: 9, itemStyle: { color: css("--critical"), borderColor: css("--critical"), borderWidth: 2.5 }, data: rows.filter((r) => r.error_count).map((r) => [r.start_ns / 1e6, r.duration_ms, `${r.root_service} ${r.root_name}`, 1, r.trace_id]) },
  ];
  const c = mkChart("#scatter", o);
  c?.on("click", (x) => go("trace", x.data[4]));
} };

// ------------------------------------------------------------- Trace detail
PAGES.trace = { title: "Trace", async render(arg) {
  let d;
  try { d = await api(`/api/traces/${encodeURIComponent(arg)}`, {}, { noTime: true }); }
  catch (e) { view().innerHTML = `<div class="card empty">Trace ${esc(arg)} not found — it may have aged out of raw retention.</div>`; return; }
  $("#pageTitle").textContent = "Trace " + shortId(d.trace_id);
  const total = d.duration_ms || 1;
  const comps = d.breakdown;
  const compColor = (c) => c.type === "service" ? svcColor(c.component) : css({ llm: "--series-7", database: "--series-4", mcp: "--series-5", http: "--unknown" }[c.type] || "--unknown");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span style="left:${f * 100}%">${ms(total * f)}</span>`).join("");
  const rows = d.spans.map((s, i) => {
    const left = (s.offset_ms / total) * 100, width = Math.max(0.15, (s.duration_ms / total) * 100);
    const color = s.peer && ["database", "llm", "mcp"].includes(s.peer_type) && !s.child_count ? compColor({ type: s.peer_type }) : svcColor(s.service);
    const icon = { llm: "◇", db: "◯", mcp: "△", http: "", internal: "" }[s.category] || "";
    return `<div class="wf-row" data-i="${i}">
      <div class="wf-name" style="padding-left:${8 + s.depth * 14}px" title="${esc(s.service)} · ${esc(s.name)}">
        ${s.status === "error" ? `<span class="status error" title="error"></span>` : ""}<span class="chip" style="background:${svcColor(s.service)}"></span>
        <span>${icon} ${esc(s.category === "db" ? (s.db_statement || s.name).slice(0, 70) : s.name)}</span><span class="svc">${esc(s.service)}</span></div>
      <div class="wf-track"><div class="wf-bar ${s.status === "error" ? "err" : ""} ${s.critical ? "crit-path" : ""}" style="left:${left}%;width:${width}%;background:${color}" title="${esc(ms(s.duration_ms))} (self ${esc(ms(s.self_ms))})"></div></div>
      <div class="wf-dur">${ms(s.duration_ms)}</div></div>`;
  }).join("");
  const rc = d.root_cause, bn = d.bottleneck;
  view().innerHTML = `
    <div class="toolbar"><span class="muted mono">${esc(d.trace_id)}</span>
      <a class="btn trace-pdf" href="#" data-trace="${esc(d.trace_id)}">⤓ Download trace report (PDF)</a>
      <a class="btn" href="/api/traces/${encodeURIComponent(d.trace_id)}" download="rapidpulse-trace-${esc(d.trace_id)}.json">⤓ Raw data (JSON)</a></div>
    ${tiles([{ label: "End-to-end duration", value: ms(d.duration_ms) }, { label: "Spans", value: num(d.span_count) }, { label: "Services", value: num(d.services.length), foot: esc(d.services.join(", ")) },
      { label: "Errors", value: num(d.error_count), foot: d.error_count ? status("error", "Failed spans present") : status("ok", "No errors") },
      { label: "LLM calls / tokens", value: `${d.ai.llm_calls}`, foot: `${num(d.ai.tokens_in)} in · ${num(d.ai.tokens_out)} out` }, { label: "LLM cost", value: usd(d.ai.cost), foot: `${d.ai.mcp_calls} MCP tool calls` },
      { label: "Started", value: `<span style="font-size:15px">${nsLabel(d.start_ns)}</span>` }])}
    ${rc ? `<div class="callout"><b>Root cause:</b> ${status("error", rc.service)} — <span class="mono">${esc(rc.name)}</span>${rc.error_type ? ` · <b>${esc(rc.error_type)}</b>` : ""}: ${esc(rc.error_message || "")} <a href="#" data-span="${rc.span_id}">Show span</a></div>` : ""}
    <div class="callout info"><b>Biggest time consumer:</b> <span class="chip" style="background:${svcColor(bn.service)}"></span>${esc(bn.service)} — <span class="mono">${esc(bn.name)}</span> spent ${ms(bn.self_ms)} of its own time (${pct(100 * bn.self_ms / total)} of the request). <a href="#" data-span="${bn.span_id}">Show span</a></div>
    ${card("Request", d.request ? `What the client sent to <b>${esc(d.request.service)}</b> · secrets in headers and payload are masked · <a href="#" data-span="${esc(d.request.span_id)}">Show span</a>` : "Headers and payload of the request that started this trace",
      requestBlock(d.request, "trace"))}
    ${card("Where the time went", "Self time per component (time not spent waiting on a child call); concurrent work can overlap", `
      <div class="stackbar">${comps.filter((c) => c.self_ms > 0).map((c) => `<div style="flex:${c.self_ms};background:${compColor(c)}" title="${esc(c.component)}: ${ms(c.self_ms)} (${c.pct}%)"></div>`).join("")}</div>
      <div class="legend">${comps.map((c) => `<span><span class="chip" style="background:${compColor(c)}"></span>${esc(c.component)} <b>${ms(c.self_ms)}</b> (${c.pct}%)${c.errors ? " " + status("error", c.errors + " err") : ""}</span>`).join("")}</div>`)}
    ${card("Execution journey", "Waterfall of every span · outlined bars = critical path · red outline = error · click a row for attributes", `
      <div class="wf"><div class="wf-axis"><div style="padding:3px 8px">Span</div><div class="ticks">${ticks}</div><div></div></div>${rows}</div>`)}
    <div class="grid g2">
      ${card("Correlated logs", "Log records carrying this trace id", table([{ label: "Time", key: "ts_ns", fmt: (v) => new Date(v / 1e6).toLocaleTimeString() }, { label: "Service", key: "service" },
        { label: "Level", key: "severity", html: (r) => r.severity === "ERROR" ? status("error", "ERROR") : r.severity === "WARN" ? status("warning", "WARN") : esc(r.severity) }, { label: "Message", key: "body", cls: "wrap mono" }], d.logs, { empty: "No logs exported with this trace id" }))}
      ${card("Infrastructure at the time", "Nearest host/container sample to the request", table([{ label: "Host / container", key: "host" }, { label: "OS", key: "os" }, { label: "CPU", key: "cpu", num: 1, html: (r) => `<span class="${r.cpu > 85 ? "bad" : ""}">${pct(r.cpu)}</span>` },
        { label: "Memory", key: "mem_pct", num: 1, fmt: pct }, { label: "Status", key: "status", html: (r) => r.status ? status(r.status) : "" }], d.infra, { empty: "No agent data for the hosts in this trace" }))}
    </div>`;
  const show = (i) => {
    const s = d.spans[i];
    $$(".wf-row").forEach((r) => r.classList.toggle("sel", +r.dataset.i === i));
    openDrawer(s.name, `<div>${status(s.status === "error" ? "error" : "ok")} <span class="pill">${esc(s.category)}</span> <span class="pill">${esc(s.kind)}</span> ${s.critical ? '<span class="pill">critical path</span>' : ""}</div>
      ${kv({ Service: s.service, Version: s.service_version, Host: s.host, Container: s.container, Duration: ms(s.duration_ms), "Self time": ms(s.self_ms), Offset: ms(s.offset_ms),
        "HTTP": s.http_method ? `${s.http_method} ${s.http_route || ""} → ${s.http_status ?? ""}` : "", Target: s.peer, "DB system": s.db_system, Model: s.llm_model,
        Tokens: s.tokens_in || s.tokens_out ? `${s.tokens_in} in / ${s.tokens_out} out` : "", Cost: s.cost_usd ? usd(s.cost_usd) : "", "Error type": s.error_type, "Error message": s.error_message, "Span ID": s.span_id, "Parent ID": s.parent_id })}
      ${s.db_statement ? `<div><h2 style="font-size:13px">Statement (literals removed)</h2><pre>${esc(s.db_statement)}</pre></div>` : ""}
      ${s.request || s.category === "http" ? `<div><h2 style="font-size:13px">Request</h2>${requestBlock(s.request, "span")}</div>` : ""}
      <div><h2 style="font-size:13px">Attributes</h2>${kv(withoutRequestAttrs(s.attributes))}</div>
      ${s.events?.length ? `<div><h2 style="font-size:13px">Events</h2><pre>${esc(JSON.stringify(s.events, null, 2))}</pre></div>` : ""}
      <div><h2 style="font-size:13px">Resource</h2>${kv(s.resource)}</div>`);
    bindRequestCopy($("#drawerBody"), () => s.request);
  };
  bindRequestCopy(view(), () => d.request);
  $$(".wf-row").forEach((r) => (r.onclick = () => show(+r.dataset.i)));
  $$("[data-span]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); show(d.spans.findIndex((s) => s.span_id === a.dataset.span)); }));
} };

// ------------------------------------------------------------- AI
PAGES.ai = { title: "AI monitoring", async render() {
  const d = await api("/api/ai");
  const t = d.totals || {}, m = d.mcp_totals || {};
  S.lastStep = d.series.step;
  const tm = d.series.times;
  view().innerHTML = `
    ${tiles([modelTile(d.current_model), { label: "LLM calls", value: num(t.calls), foot: `${num(t.rpm, 1)}/min` }, { label: "LLM failure rate", value: pct(t.error_rate), foot: t.error_rate >= 5 ? status("critical", "Above 5%") : status("healthy", "OK") },
      { label: "LLM latency p95", value: ms(t.p95_ms), foot: `avg ${ms(t.avg_ms)}` }, { label: "Input tokens", value: num(t.tokens_in) }, { label: "Output tokens", value: num(t.tokens_out) },
      { label: "Estimated cost", value: usd(t.cost), foot: t.calls ? `${usd(t.cost / t.calls)} per call` : "" }, { label: "MCP tool calls", value: num(m.calls), foot: `failures ${pct(m.error_rate)} · p95 ${ms(m.p95_ms)}` }])}
    <div class="grid g2">
      ${card("LLM calls per interval", "click to drill into traces", `<div class="chart" id="aCalls"></div>`)}
      ${card("Token consumption", "Input vs output tokens per interval", `<div class="chart" id="aTok"></div>`)}
      ${card("Estimated cost by model", "USD per interval, stacked", `<div class="chart" id="aCost"></div>`)}
      ${card("LLM latency p95", "", `<div class="chart" id="aLat"></div>`)}
    </div>
    ${card("Models", "Usage, latency, failures and cost per model", table([{ label: "Model", key: "operation", cls: "mono" }, { label: "Provider", key: "peer" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
      { label: "Failure %", key: "error_rate", num: 1, html: (r) => `<span class="${r.error_rate >= 5 ? "bad" : ""}">${pct(r.error_rate)}</span>` }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms },
      { label: "Tokens in", key: "tokens_in", num: 1, fmt: (v) => num(v) }, { label: "Tokens out", key: "tokens_out", num: 1, fmt: (v) => num(v) }, { label: "Cost", key: "cost", num: 1, fmt: usd }], d.models))}
    <div class="grid g2">
      ${card("LLM usage by calling service", "Which API spends the tokens", table([{ label: "Service", key: "service", html: (r) => `<span class="chip" style="background:${svcColor(r.service)}"></span>${esc(r.service)}` }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
        { label: "Failure %", key: "error_rate", num: 1, fmt: pct }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "Tokens", key: "tokens_in", num: 1, html: (r) => num(r.tokens_in + r.tokens_out) }, { label: "Cost", key: "cost", num: 1, fmt: usd }], d.callers))}
      ${card("MCP tools", "Tool executions seen on both client and server side", table([{ label: "Service", key: "service" }, { label: "Tool", key: "operation", cls: "mono" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
        { label: "Failure %", key: "error_rate", num: 1, html: (r) => `<span class="${r.error_rate >= 5 ? "bad" : ""}">${pct(r.error_rate)}</span>` }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }], d.tools))}
    </div>
    <div class="grid g2">
      ${card("Recent AI failures", "LLM and MCP calls that failed", table([{ label: "Time", key: "start_ns", fmt: nsLabel }, { label: "Service", key: "service" }, { label: "Type", key: "category", html: (r) => `<span class="pill">${esc(r.category)}</span>` },
        { label: "Model / tool", key: "operation", cls: "mono" }, { label: "Error", key: "error_message", cls: "wrap err-text", html: (r) => `${esc(r.error_type || "")}: ${esc(r.error_message || "")}` }], d.failures, { onClick: (r) => go("trace", r.trace_id), empty: "No AI failures 🎉" }))}
      ${card("Slowest LLM calls", "", table([{ label: "Time", key: "start_ns", fmt: nsLabel }, { label: "Service", key: "service" }, { label: "Model", key: "operation", cls: "mono" }, { label: "Duration", key: "duration_ms", num: 1, fmt: ms },
        { label: "Tokens", key: "tokens_in", num: 1, html: (r) => `${num(r.tokens_in)} / ${num(r.tokens_out)}` }, { label: "Cost", key: "cost_usd", num: 1, fmt: usd }], d.slowest, { onClick: (r) => go("trace", r.trace_id) }))}
    </div>`;
  lineChart("#aCalls", [{ name: "calls", data: toPts(tm, seriesOf(d.series, "calls")), color: css("--series-7"), area: true }], { fmt: (v) => num(v), bars: true, drill: drillToTraces({}) });
  lineChart("#aTok", [{ name: "input", data: toPts(tm, seriesOf(d.series, "tokens_in")), color: css("--series-1") }, { name: "output", data: toPts(tm, seriesOf(d.series, "tokens_out")), color: css("--series-2") }], { fmt: (v) => v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "k" : num(v) });
  const ct = d.cost_by_model.times;
  lineChart("#aCost", d.cost_by_model.series.map((s, i) => ({ name: s.group, data: toPts(ct, s.points), color: catColor(i) })), { fmt: usd, stacked: true, bars: true });
  lineChart("#aLat", [{ name: "p95", data: toPts(tm, seriesOf(d.series, "p95_ms")), color: css("--series-7") }], { fmt: ms });
} };

// ------------------------------------------------------------- Database
PAGES.database = { title: "Database", async render() {
  const d = await api("/api/database");
  const mnames = await api("/api/metrics/names", {}, { noTime: true }).catch(() => []);
  const dbm = mnames.filter((n) => /^(mssql|sqlserver|db|postgres|mysql)\./i.test(n.name));
  const tot = d.databases.reduce((a, r) => ({ calls: a.calls + r.calls, errors: a.errors + r.errors }), { calls: 0, errors: 0 });
  S.lastStep = d.series.step;
  const tm = d.series.times;
  view().innerHTML = `
    ${tiles([{ label: "Queries", value: num(tot.calls) }, { label: "Query failure rate", value: pct(tot.calls ? 100 * tot.errors / tot.calls : 0) },
      { label: "Slow queries", value: num(d.slow.length), foot: `≥ ${ms(d.slow_threshold_ms)} (raw retention)` },
      ...d.connectivity.map((c) => ({ label: c.name, value: c.last_ok ? status("healthy", "Connected") : status("critical", "Unreachable"), foot: `uptime ${pct(c.uptime)} · ${ms(c.avg_ms)}` })),
      ...d.front_services.map((s) => ({ label: `${s.display || s.service} (API)`, value: ms(s.p95_ms), unit: "p95", foot: `${num(s.rpm, 1)} rpm · err ${pct(s.error_rate)}`, href: `#/service/${encodeURIComponent(s.service)}` }))])}
    <div class="grid g2">
      ${card("Query volume", "click to drill down", `<div class="chart" id="dCalls"></div>`)}
      ${card("Query execution time", "avg and p95", `<div class="chart" id="dLat"></div>`)}
    </div>
    ${card("Databases", "", table([{ label: "Database", key: "peer" }, { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) }, { label: "Calls/min", key: "rpm", num: 1, fmt: (v) => num(v, 1) },
      { label: "Error %", key: "error_rate", num: 1, fmt: pct }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "p99", key: "p99_ms", num: 1, fmt: ms }, { label: "Max", key: "max_ms", num: 1, fmt: ms }], d.databases))}
    ${card("Top queries by total time", "Statements normalised (literals removed) · total time = calls × avg", table([{ label: "Database", key: "peer" }, { label: "Caller", key: "service" }, { label: "Statement", key: "operation", cls: "wrap mono" },
      { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms }, { label: "Max", key: "max_ms", num: 1, fmt: ms },
      { label: "Total", key: "calls", num: 1, sortBy: (r) => r.calls * (r.avg_ms || 0), html: (r) => ms(r.calls * (r.avg_ms || 0)) }, { label: "Error %", key: "error_rate", num: 1, fmt: pct }], d.queries, { maxRows: 50 }))}
    <div class="grid g2">
      ${card("Slow queries", "Individual executions above the threshold · click to see the full request", table([{ label: "Time", key: "start_ns", fmt: nsLabel }, { label: "Caller", key: "service" },
        { label: "Statement", key: "db_statement", cls: "wrap mono", fmt: (v) => (v || "").slice(0, 160) }, { label: "Duration", key: "duration_ms", num: 1, html: (r) => `<span class="bad">${ms(r.duration_ms)}</span>` }], d.slow, { onClick: (r) => go("trace", r.trace_id), empty: "No slow queries 🎉" }))}
      ${card("Database errors", "", table([{ label: "Time", key: "start_ns", fmt: nsLabel }, { label: "Database", key: "peer" }, { label: "Error", key: "error_message", cls: "wrap err-text" }], d.errors, { onClick: (r) => go("trace", r.trace_id), empty: "No database errors 🎉" }))}
    </div>
    ${dbm.length ? card("Database server metrics", "Reported by the RapidPulse agent's SQL Server collector", `<div class="grid g4">${dbm.map((m, i) => `<div><div class="sub">${esc(m.name)} <span class="muted">${esc(m.unit || "")}</span></div><div class="chart" style="height:150px" id="dm${i}"></div></div>`).join("")}</div>`) : ""}`;
  lineChart("#dCalls", [{ name: "queries", data: toPts(tm, seriesOf(d.series, "calls")), color: css("--series-4"), area: true }], { fmt: (v) => num(v), drill: drillToTraces({}) });
  lineChart("#dLat", [{ name: "avg", data: toPts(tm, seriesOf(d.series, "avg_ms")), color: css("--series-1") }, { name: "p95", data: toPts(tm, seriesOf(d.series, "p95_ms")), color: css("--series-2") }], { fmt: ms, drill: drillToTraces({ sort: "slowest" }) });
  dbm.forEach(async (m, i) => {
    const s = await api("/api/metrics/series", { name: m.name });
    lineChart(`#dm${i}`, [{ name: m.name, data: s.points.map((p) => [p[0] * 1000, p[1]]), color: catColor(0) }], { fmt: (v) => num(v, 1) });
  });
} };

// ------------------------------------------------------------- Infrastructure
const meter = (v) => `<div class="meter"><div class="${v > 90 ? "hi" : v > 75 ? "mid" : ""}" style="width:${Math.min(100, v || 0)}%"></div></div>`;
PAGES.infra = { title: "Infrastructure", async render() {
  const [hosts, checks] = await Promise.all([api("/api/infra/hosts", {}, { noTime: true }), api("/api/checks")]);
  view().innerHTML = `
    ${card("Availability checks", "Synthetic HTTP / TCP checks from the RapidPulse server and agents", table([{ label: "Check", key: "name" }, { label: "Type", key: "type", html: (r) => `<span class="pill">${esc(r.type)}</span>` },
      { label: "Target", key: "target", cls: "mono" }, { label: "Service", key: "service" }, { label: "Status", key: "last_ok", html: (r) => r.last_ok ? status("healthy", "Up") : status("critical", "Down") },
      { label: "Uptime", key: "uptime", num: 1, fmt: pct }, { label: "Avg latency", key: "avg_ms", num: 1, fmt: ms }, { label: "Last result", key: "last_message", cls: "wrap" }, { label: "From", key: "source" }], checks, { empty: "No checks configured. Add monitors in pulse.yaml or run the agent with --check." }))}
    <div class="grid g2">${hosts.map((h, i) => {
      const m = h.metrics || {};
      return `<div class="card host-card"><div class="card-head"><div><h2>${h.os === "windows" ? "⊞" : "🐧"} ${esc(h.host)}</h2><p class="sub">${esc(h.os_version || h.os || "")} · ${esc(h.role || "")} · ${esc(h.instance_type || "")} ${h.instance_id ? "· " + esc(h.instance_id) : ""} · ${esc(h.ip || "")}</p></div>
        <div>${status(h.status, h.status === "down" ? `No data ${ago(h.last_seen)}` : undefined)}</div></div>
        <div class="bars"><span>CPU</span>${meter(m.cpu)}<span class="num">${pct(m.cpu)}</span><span>Memory</span>${meter(m.mem_pct)}<span class="num">${pct(m.mem_pct)}</span>
          <span>Disk</span>${meter(m.disk_pct)}<span class="num">${pct(m.disk_pct)}</span></div>
        <div class="sub">Network in ${bytes(m.net_in)}/s · out ${bytes(m.net_out)}/s · TCP conns ${num(m.tcp_conns)} · processes ${num(m.procs)} · ${h.cpus || "?"} vCPU / ${num(h.mem_total, 0)} GB</div>
        ${h.services.length ? `<div class="sub">Services: ${h.services.map((s) => `<a href="#/service/${encodeURIComponent(s)}"><span class="chip" style="background:${svcColor(s)}"></span>${esc(s)}</a>`).join(" · ")}</div>` : ""}
        ${h.containers.length ? table([{ label: "Container", key: "container" }, { label: "Status", key: "status", html: (r) => status(r.health === "unhealthy" ? "critical" : r.status, `${r.status}${r.health ? " / " + r.health : ""}`) },
          { label: "CPU", key: "cpu", num: 1, fmt: pct }, { label: "Mem", key: "mem_pct", num: 1, fmt: pct }, { label: "Restarts", key: "restarts", num: 1, html: (r) => `<span class="${r.restarts ? "bad" : ""}">${num(r.restarts)}</span>` }], h.containers) : ""}
        <div style="margin-top:8px"><button data-host="${esc(h.host)}">Trends →</button></div></div>`;
    }).join("") || `<div class="card empty">No agents reporting. Run <code>python agent/pulse_agent.py --server http://&lt;pulse&gt;:4400</code> on each server.</div>`}</div>`;
  $$("[data-host]").forEach((b) => (b.onclick = () => hostDrawer(b.dataset.host)));
} };
async function hostDrawer(host) {
  openDrawer(host, `<div class="sub">Averages per interval over the selected range</div>${["CPU %", "Memory %", "Disk %", "Network (bytes/s)", "Container CPU %"].map((t, i) => `<div><h2 style="font-size:13px">${t}</h2><div class="chart" style="height:170px" id="hd${i}"></div></div>`).join("")}`);
  const d = await api(`/api/infra/hosts/${encodeURIComponent(host)}`);
  const pts = (k) => d.host.map((r) => [r.t * 1000, r[k]]);
  lineChart("#hd0", [{ name: "cpu", data: pts("cpu"), color: css("--series-1"), area: true }], { fmt: (v) => num(v, 1) + "%" });
  lineChart("#hd1", [{ name: "memory", data: pts("mem"), color: css("--series-2"), area: true }], { fmt: (v) => num(v, 1) + "%" });
  lineChart("#hd2", [{ name: "disk", data: pts("disk"), color: css("--series-3") }], { fmt: (v) => num(v, 1) + "%" });
  lineChart("#hd3", [{ name: "in", data: pts("net_in"), color: css("--series-1") }, { name: "out", data: pts("net_out"), color: css("--series-2") }], { fmt: bytes });
  const names = [...new Set(d.containers.map((c) => c.container))];
  lineChart("#hd4", names.map((n, i) => ({ name: n, data: d.containers.filter((c) => c.container === n).map((c) => [c.t * 1000, c.cpu]), color: catColor(i) })), { fmt: (v) => num(v, 1) + "%" });
}

// ------------------------------------------------------------- Alerts
let ALERT_METRICS = [];
PAGES.alerts = { title: "Alerts", noAuto: false, async render() {
  const [firing, history, rules] = await Promise.all([api("/api/alerts/events", { state: "firing" }, { noTime: true }), api("/api/alerts/events", {}, { noTime: true }), api("/api/alerts/rules", {}, { noTime: true })]);
  if (!ALERT_METRICS.length) ALERT_METRICS = await api("/api/alerts/metrics", {}, { noTime: true });
  view().innerHTML = `
    ${card(`Firing now (${firing.length})`, "Evaluated every 30 seconds", table([{ label: "Severity", key: "severity", html: (r) => status(r.severity === "critical" ? "critical" : "warning", r.severity) },
      { label: "Alert", key: "rule_name" }, { label: "Scope", key: "scope_key", cls: "mono" }, { label: "Value", key: "value", num: 1, fmt: (v) => num(v, 2) }, { label: "Threshold", key: "threshold", num: 1, fmt: (v) => num(v, 2) },
      { label: "Since", key: "started", fmt: ago }, { label: "", key: "id", html: (r) => r.acknowledged ? '<span class="muted">acknowledged</span>' : `<button data-ack="${r.id}">Acknowledge</button>` }], firing, { empty: "Nothing firing 🎉" }))}
    <div class="card"><div class="card-head"><div><h2>Alert rules</h2><p class="sub">Leave a scope empty to evaluate every service / host / model separately</p></div>
      <div><button id="evalNow">Evaluate now</button> <button class="primary" id="newRule">New rule</button></div></div>
      ${table([{ label: "On", key: "enabled", html: (r) => `<input type="checkbox" data-toggle="${r.id}" ${r.enabled ? "checked" : ""}>` }, { label: "Name", key: "name" },
        { label: "Condition", key: "metric", html: (r) => `<span class="mono">${esc(r.metric)} ${esc(r.op)} ${r.threshold}</span> over ${r.window_min}m` },
        { label: "Scope", key: "scope", html: (r) => Object.entries(r.scope || {}).map(([k, v]) => `<span class="pill">${esc(k)}=${esc(v)}</span>`).join(" ") || '<span class="muted">all</span>' },
        { label: "Severity", key: "severity", html: (r) => status(r.severity === "critical" ? "critical" : "warning", r.severity) }, { label: "Notify", key: "channels", html: (r) => `<span class="muted">${(r.channels || []).length || "default"}</span>` },
        { label: "", key: "id", html: (r) => `<button data-edit="${r.id}">Edit</button> <button data-test="${r.id}">Test</button> <button data-del="${r.id}">Delete</button>` }], rules)}</div>
    ${card("History", "Last 200 alert events", table([{ label: "State", key: "state", html: (r) => status(r.state) }, { label: "Severity", key: "severity" }, { label: "Alert", key: "rule_name" },
      { label: "Scope", key: "scope_key", cls: "mono" }, { label: "Peak value", key: "value", num: 1, fmt: (v) => num(v, 2) }, { label: "Started", key: "started", fmt: tsLabel },
      { label: "Resolved", key: "resolved", fmt: (v) => v ? tsLabel(v) : "—" }, { label: "Duration", key: "resolved", html: (r) => r.resolved ? ms((r.resolved - r.started) * 1000) : "ongoing" }], history))}`;
  $$("[data-ack]").forEach((b) => (b.onclick = async () => { await post(`/api/alerts/events/${b.dataset.ack}/ack`); render(); }));
  $$("[data-del]").forEach((b) => (b.onclick = async () => { if (confirm("Delete this rule?")) { await fetch(`/api/alerts/rules/${b.dataset.del}`, { method: "DELETE" }); render(); } }));
  $$("[data-toggle]").forEach((b) => (b.onchange = async () => { const r = rules.find((x) => x.id === +b.dataset.toggle); await post("/api/alerts/rules", { ...r, enabled: b.checked }); }));
  $$("[data-edit]").forEach((b) => (b.onclick = () => ruleEditor(rules.find((x) => x.id === +b.dataset.edit))));
  $$("[data-test]").forEach((b) => (b.onclick = async () => {
    const r = await post(`/api/alerts/rules/${b.dataset.test}/test`); const rule = rules.find((x) => x.id === +b.dataset.test);
    openDrawer(`Test: ${rule.name}`, `<p class="sub">Current values over the last ${rule.window_min} minutes. Condition: <span class="mono">${esc(rule.op)} ${rule.threshold}</span></p>` +
      table([{ label: "Scope", key: "scope", cls: "mono" }, { label: "Value", key: "value", num: 1, fmt: (v) => num(v, 2) }, { label: "Would fire", key: "value", html: (x) => ({ ">": x.value > rule.threshold, ">=": x.value >= rule.threshold, "<": x.value < rule.threshold, "<=": x.value <= rule.threshold, "==": x.value == rule.threshold }[rule.op]) ? status("critical", "Yes") : status("healthy", "No") }], r.values, { empty: "No data for this metric yet" }));
  }));
  $("#newRule").onclick = () => ruleEditor({ name: "", metric: "service.error_rate", op: ">", threshold: 5, window_min: 5, severity: "warning", cooldown_min: 30, scope: {}, channels: [], enabled: true });
  $("#evalNow").onclick = async () => { const r = await post("/api/alerts/evaluate"); alert(`Evaluated: ${r.fired} fired, ${r.resolved} resolved`); render(); };
} };
function ruleEditor(rule) {
  const opts = ALERT_METRICS.map((m) => `<option value="${m.metric}" ${m.metric === rule.metric ? "selected" : ""}>${m.metric} — ${esc(m.description)} (${m.unit})</option>`).join("");
  const scopeKeys = ["app", "service", "operation", "peer", "host", "container", "name"];
  openDrawer(rule.id ? `Edit rule #${rule.id}` : "New alert rule", `<form id="ruleForm" class="filters" style="flex-direction:column;align-items:stretch">
    <label>Name<input type="text" name="name" value="${esc(rule.name)}" required></label>
    <label>Metric<select name="metric" id="rmetric">${opts}</select></label>
    <div class="filters"><label>Operator<select name="op">${[">", ">=", "<", "<=", "=="].map((o) => `<option ${o === rule.op ? "selected" : ""}>${esc(o)}</option>`).join("")}</select></label>
      <label>Threshold<input type="number" step="any" name="threshold" value="${rule.threshold}"></label>
      <label>Window (min)<input type="number" name="window_min" value="${rule.window_min}" style="width:90px"></label>
      <label>Severity<select name="severity">${["critical", "warning", "info"].map((s) => `<option ${s === rule.severity ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label>Re-notify (min)<input type="number" name="cooldown_min" value="${rule.cooldown_min}" style="width:90px"></label></div>
    <p class="sub" id="scopeHint"></p>
    <div class="filters">${scopeKeys.map((k) => `<label data-scope="${k}">${k}<input type="text" name="scope.${k}" value="${esc((rule.scope || {})[k] || "")}" placeholder="any"></label>`).join("")}</div>
    <label>Notification channels (one per line: webhook URL — Slack/Teams/generic — or the word <code>email</code>)<textarea name="channels" rows="3">${esc((rule.channels || []).join("\n"))}</textarea></label>
    <label class="chk"><span><input type="checkbox" name="enabled" ${rule.enabled ? "checked" : ""}> Enabled</span></label>
    <div><button class="primary">Save rule</button></div></form>`);
  const syncScope = () => { const m = ALERT_METRICS.find((x) => x.metric === $("#rmetric").value); $$("[data-scope]").forEach((l) => (l.style.display = m.scope.includes(l.dataset.scope) ? "" : "none")); $("#scopeHint").textContent = `Scope keys for this metric: ${m.scope.join(", ")} (blank = every one separately)`; };
  $("#rmetric").onchange = syncScope; syncScope();
  $("#ruleForm").onsubmit = async (e) => {
    e.preventDefault(); const fd = new FormData(e.target); const scope = {};
    for (const [k, v] of fd.entries()) if (k.startsWith("scope.") && v && $(`[data-scope="${k.slice(6)}"]`).style.display !== "none") scope[k.slice(6)] = v;
    try {
      await post("/api/alerts/rules", { id: rule.id, name: fd.get("name"), metric: fd.get("metric"), op: fd.get("op"), threshold: +fd.get("threshold"), window_min: +fd.get("window_min"), severity: fd.get("severity"),
        cooldown_min: +fd.get("cooldown_min"), scope, channels: fd.get("channels").split("\n").map((s) => s.trim()).filter(Boolean), enabled: fd.get("enabled") === "on" });
      $("#drawer").hidden = true; render();
    } catch (err) { alert(err.message); }
  };
}

// ------------------------------------------------------------- Analytics
PAGES.analytics = { title: "Analytics", async render() {
  const p = S.route.params;
  const f = { metrics: p.get("metrics") || "p95_ms", group_by: p.get("group_by") ?? "service", category: p.get("category") || "", role: p.get("role") || "entry", service: p.get("service") || "", compare: p.get("compare") || "" };
  const meta = S.meta || await api("/api/meta", {}, { noTime: true });
  const services = [...new Set(meta.services.map((s) => s.service))];
  const M = { rpm: "Throughput (req/min)", calls: "Calls", error_rate: "Error rate %", avg_ms: "Avg latency", p50_ms: "p50 latency", p95_ms: "p95 latency", p99_ms: "p99 latency", max_ms: "Max latency", tokens: "LLM tokens", cost: "LLM cost (USD)" };
  const [series, rep] = await Promise.all([
    api("/api/analytics/series", { metrics: f.metrics, group_by: f.group_by, category: f.category, role: f.role, service: f.service, compare: f.compare && !f.group_by ? "1" : "" }),
    api("/api/analytics/report", { group_by: f.group_by ? f.group_by + (f.group_by === "service" ? ",operation" : "") : "service,operation", category: f.category, role: f.role, service: f.service })]);
  S.lastStep = series.step;
  const csvUrl = `/api/analytics/report?${new URLSearchParams({ ...(S.app !== "all" ? { app: S.app } : {}), ...timeParams(), group_by: f.group_by ? f.group_by + (f.group_by === "service" ? ",operation" : "") : "service,operation", format: "csv", ...(f.category ? { category: f.category } : {}), ...(f.role ? { role: f.role } : {}), ...(f.service ? { service: f.service } : {}) })}`;
  const fmt = f.metrics.endsWith("_ms") ? ms : f.metrics === "error_rate" ? (v) => num(v, 2) + "%" : f.metrics === "cost" ? usd : (v) => num(v, 1);
  view().innerHTML = `
    <div class="card"><form class="filters" id="af">
      <label>Metric<select name="metrics">${Object.entries(M).map(([k, v]) => `<option value="${k}" ${k === f.metrics ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Split by<select name="group_by">${[["", "Nothing (total)"], ["service", "Service"], ["operation", "Endpoint / operation"], ["category", "Category"], ["peer", "Dependency"]].map(([k, v]) => `<option value="${k}" ${k === f.group_by ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Span role<select name="role">${[["entry", "Incoming requests"], ["exit", "Outgoing calls"], ["", "Both"]].map(([k, v]) => `<option value="${k}" ${k === f.role ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Category<select name="category">${[["", "Any"], ["http", "HTTP"], ["db", "Database"], ["llm", "LLM"], ["mcp", "MCP"]].map(([k, v]) => `<option value="${k}" ${k === f.category ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>Service<select name="service"><option value="">Any</option>${services.map((s) => `<option ${s === f.service ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>
      <label class="chk"><span><input type="checkbox" name="compare" value="1" ${f.compare ? "checked" : ""}> Compare with previous period</span></label>
      <button class="primary">Apply</button></form></div>
    ${card(M[f.metrics], `Interval ${ms(series.step * 1000)} · use the Range selector for real-time (15 min) through historical (30 days) · click the chart to see traces`, `<div class="chart tall" id="anChart"></div>`)}
    <div class="card"><div class="card-head"><div><h2>Report</h2><p class="sub">Current period vs the previous period of equal length</p></div><a class="btn" href="${csvUrl}">Export CSV</a></div>
      ${table([...rep.group_by.map((g) => ({ label: g, key: g, cls: g === "operation" ? "wrap mono" : "" })), { label: "Calls", key: "calls", num: 1, fmt: (v) => num(v) },
        { label: "Δ calls", key: "prev_calls", num: 1, html: (r) => delta(r.calls, r.prev_calls, false) }, { label: "Error %", key: "error_rate", num: 1, fmt: pct },
        { label: "Prev error %", key: "prev_error_rate", num: 1, fmt: pct }, { label: "Avg", key: "avg_ms", num: 1, fmt: ms }, { label: "p95", key: "p95_ms", num: 1, fmt: ms },
        { label: "Δ p95", key: "prev_p95_ms", num: 1, html: (r) => delta(r.p95_ms, r.prev_p95_ms, true) }, { label: "p99", key: "p99_ms", num: 1, fmt: ms }, { label: "Apdex", key: "apdex", num: 1, fmt: (v) => v == null ? "—" : v.toFixed(2) },
        { label: "Cost", key: "cost", num: 1, fmt: (v) => v ? usd(v) : "—" }], rep.rows, { maxRows: 200, onClick: (r) => go("traces", "", { service: r.service, operation: r.operation ? r.operation.split(" ").slice(1).join(" ") || r.operation : "" }) })}</div>`;
  $("#af").onsubmit = (e) => { e.preventDefault(); go("analytics", "", Object.fromEntries(new FormData(e.target))); };
  const t = series.times;
  const lines = series.series.map((s, i) => ({ name: s.group || M[f.metrics], data: toPts(t, s.points), color: f.group_by === "service" ? svcColor(s.group) : catColor(i) }));
  (series.previous || []).forEach((s) => lines.push({ name: "previous period", data: toPts(t, s.points), color: css("--text-muted"), dashed: true }));
  lineChart("#anChart", lines, { fmt, drill: drillToTraces({ service: f.service }) });
} };
function delta(cur, prev, higherIsWorse) {
  if (cur == null || prev == null || !prev) return '<span class="muted">—</span>';
  const d = (100 * (cur - prev)) / prev;
  const worse = higherIsWorse ? d > 10 : false;
  return `<span class="${worse ? "bad" : ""}">${d > 0 ? "▲" : d < 0 ? "▼" : ""} ${Math.abs(d).toFixed(0)}%</span>`;
}

// ------------------------------------------------------------- Logs
PAGES.logs = { title: "Logs", async render() {
  const p = S.route.params;
  const f = { service: p.get("service") || "", severity: p.get("severity") || "", q: p.get("q") || "" };
  const rows = await api("/api/logs", { ...f, limit: 500 });
  view().innerHTML = `<div class="card"><form class="filters" id="lf">
      <label>Service<input type="text" name="service" value="${esc(f.service)}"></label>
      <label>Level<select name="severity">${["", "ERROR", "WARN", "INFO", "DEBUG"].map((s) => `<option ${s === f.severity ? "selected" : ""} value="${s}">${s || "Any"}</option>`).join("")}</select></label>
      <label>Contains<input type="text" name="q" value="${esc(f.q)}"></label><button class="primary">Search</button></form></div>
    ${card(`${rows.length} log records`, "OTLP logs · rows with a trace id link to the trace", table([{ label: "Time", key: "ts_ns", fmt: nsLabel }, { label: "Service", key: "service" },
      { label: "Level", key: "severity", html: (r) => r.severity === "ERROR" ? status("error", "ERROR") : r.severity === "WARN" ? status("warning", "WARN") : esc(r.severity) },
      { label: "Message", key: "body", cls: "wrap mono" }, { label: "Trace", key: "trace_id", html: (r) => r.trace_id ? `<a href="#/trace/${r.trace_id}">${shortId(r.trace_id)}</a>` : "" }], rows))}`;
  $("#lf").onsubmit = (e) => { e.preventDefault(); go("logs", "", Object.fromEntries(new FormData(e.target))); };
} };

// ================================================================== shell
async function render() {
  S.charts.forEach((c) => c.dispose()); S.charts = [];
  S.route = hashState();
  const page = PAGES[S.route.page] || PAGES.overview;
  $("#pageTitle").textContent = page.title;
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === S.route.page || (S.route.page === "service" && a.dataset.page === "services") || (S.route.page === "trace" && a.dataset.page === "traces")));
  try { await page.render(S.route.arg); }
  catch (e) { console.error(e); view().innerHTML = `<div class="card callout">Could not load this view: ${esc(e.message)}</div>`; }
}
async function refreshChrome() {
  try {
    const [meta, firing, sys] = await Promise.all([api("/api/meta", {}, { noTime: true }), api("/api/alerts/events", { state: "firing" }, { noTime: true }), api("/api/system", {}, { noTime: true })]);
    S.meta = meta;
    [...new Set(meta.services.map((s) => s.service))].sort().forEach((s) => svcColor(s));
    const sel = $("#appSel"); const cur = sel.value;
    sel.innerHTML = `<option value="all">All applications</option>` + meta.apps.map((a) => `<option ${a === cur ? "selected" : ""}>${esc(a)}</option>`).join("");
    if (meta.apps.length === 1 && S.app === "all" && !localStorage.getItem("pulse.app.chosen")) { /* keep "all" */ }
    const n = firing.length; const b = $("#alertCount"); b.hidden = !n; b.textContent = n;
    $("#sysinfo").innerHTML = `v${esc(sys.version)} · ${num(sys.counters.spans)} spans ingested since start<br>store ${bytes(sys.db_bytes)} · raw ${sys.retention.raw_retention_hours}h · history ${sys.retention.rollup_retention_days}d`;
  } catch (e) { console.warn(e); }
}

// ------------------------------------------------------------ time window, refresh, export
const pad2 = (n) => String(n).padStart(2, "0");
const toLocalInput = (sec) => { const d = new Date(sec * 1000); return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`; };
function showCustom(on) {
  $("#customRange").hidden = !on;
  if (on) {
    const now = Math.floor(Date.now() / 1000);
    const toNow = !S.cto || S.cto === "now";
    $("#crFrom").value = toLocalInput(S.cfrom || now - 3600); $("#crTo").value = toLocalInput(toNow ? now : S.cto);
    $("#crToMode").value = toNow ? "now" : "at"; $("#crTo").hidden = toNow;
  }
}
function applyWindow() {   // a new window replaces any drill-down window in the URL
  if (S.route?.params.get("from")) { const p = Object.fromEntries(S.route.params); delete p.from; delete p.to; go(S.route.page, S.route.arg, p); }
  else render();
}
async function refreshNow() {
  const b = $("#refreshBtn"); if (b.disabled) return;
  b.classList.add("spin"); b.disabled = true;
  const y = window.scrollY;
  try { await Promise.all([refreshChrome(), render()]); window.scrollTo(0, y); }
  finally { b.classList.remove("spin"); b.disabled = false; }
}
function initTimeControls() {
  if (S.range === "custom") showCustom(true);
  $("#customRange").onsubmit = (e) => {
    e.preventDefault();
    const toNow = $("#crToMode").value === "now";   // "Now": the window keeps ending at the current time
    const f = Math.floor(new Date($("#crFrom").value).getTime() / 1000);
    const t = toNow ? Math.floor(Date.now() / 1000) : Math.floor(new Date($("#crTo").value).getTime() / 1000);
    if (!f || !t || isNaN(f) || isNaN(t)) { toast("Enter both a start and an end date and time."); return; }
    if (f >= t) { toast("The start must be before the end."); return; }
    if (t - f < 60) { toast("Choose a window of at least one minute."); return; }
    S.range = "custom"; S.cfrom = f; S.cto = toNow ? "now" : t;
    try { localStorage.setItem("pulse.range", "custom"); localStorage.setItem("pulse.custom", JSON.stringify({ from: f, to: S.cto })); } catch {}
    $("#rangeSel").value = "custom";
    applyWindow();
  };
  $("#crToMode").onchange = (e) => { $("#crTo").hidden = e.target.value === "now"; if (!$("#crTo").hidden) $("#crTo").focus(); };
  $("#refreshBtn").onclick = () => refreshNow();
  $$("#exportMenu a[data-audience]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault(); $("#exportMenu").open = false;
    const aud = a.dataset.audience;
    download(`/api/reports/pdf?${exportQuery({ audience: aud })}`, aud === "stakeholder" ? "the stakeholder summary" : "the developer report");
  }));
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#exportMenu")) $("#exportMenu").open = false;
    const tp = e.target.closest(".trace-pdf");
    if (tp) { e.preventDefault(); e.stopPropagation(); download(`/api/traces/${encodeURIComponent(tp.dataset.trace)}/pdf?tz=${new Date().getTimezoneOffset()}`, "the trace report"); }
  }, true);
}

function initTheme() {
  let t = null; try { t = localStorage.getItem("pulse.theme"); } catch {}
  if (t) document.documentElement.dataset.theme = t;
  $("#themeBtn").onclick = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next; try { localStorage.setItem("pulse.theme", next); } catch {}
    render();
  };
}

(async function init() {
  initTheme();
  try {
    S.range = localStorage.getItem("pulse.range") || "1h"; S.app = localStorage.getItem("pulse.app") || "all";
    const c = JSON.parse(localStorage.getItem("pulse.custom") || "null"); if (c) { S.cfrom = c.from; S.cto = c.to; }
  } catch {}
  if (S.range === "custom" && !(S.cfrom && S.cto)) S.range = "1h";
  $("#rangeSel").value = S.range;
  initTimeControls();
  await refreshChrome();
  $("#appSel").value = S.app;
  if ($("#appSel").value !== S.app) S.app = "all";
  $("#rangeSel").onchange = (e) => {
    if (e.target.value === "custom") { showCustom(true); $("#crFrom").focus(); return; }   // applied with the Apply button
    showCustom(false);
    S.range = e.target.value; try { localStorage.setItem("pulse.range", S.range); } catch {}
    applyWindow();
  };
  $("#appSel").onchange = (e) => { S.app = e.target.value; try { localStorage.setItem("pulse.app", S.app); } catch {} ; render(); };
  $("#navToggle").onclick = () => $("#nav").classList.toggle("open");
  window.addEventListener("hashchange", () => { $("#nav").classList.remove("open"); $("#drawer").hidden = true; render(); });
  window.addEventListener("resize", () => S.charts.forEach((c) => c.resize()));
  await render();
  setInterval(() => {
    if (!$("#autoRefresh").checked || !$("#drawer").hidden || document.hidden) return;
    if (["trace", "alerts"].includes(S.route.page) || S.route.params.get("from") || (S.range === "custom" && S.cto !== "now")) return;
    if (document.activeElement?.id === "epSearch" || $("#exportMenu").open) return;   // don't disturb typing or an open menu
    refreshNow();
  }, 30000);
})();
