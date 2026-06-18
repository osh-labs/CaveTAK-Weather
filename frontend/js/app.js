/*
 * UpstreamWX PWA — app shell + view renderers.
 * Behavior/IA follow PRD §6.8 + Appendix D; visuals follow STYLE_GUIDE.md.
 * Data mirrors BriefingResponse/BriefingResult; at M0.4 the fetch below becomes
 * POST /v1/briefing and nothing else changes.
 */

import { icon, HAZARD_LABELS, PHASE_LABELS } from "./icons.js";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "forecast", label: "Forecast" },
  { id: "map", label: "Map" },
  { id: "hazards", label: "Hazards" },
  { id: "resources", label: "Resources" },
];

const ACK_KEY = "uwx.ack.v1"; // first-run acknowledgment (FR-31)
const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let state = { briefing: null, fromCache: false, tab: "overview", mapInitialized: false };

/* ── Data load ─────────────────────────────────────────────────────── */
async function loadBriefing() {
  // M0.4: replace with `fetch('/v1/briefing', {method:'POST', body: missionSpec})`.
  try {
    const res = await fetch("data/sample-briefing.json", { cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    state.fromCache = res.headers.get("x-from-sw-cache") === "1" || !navigator.onLine;
    return await res.json();
  } catch (e) {
    const cached = await caches.match("data/sample-briefing.json");
    if (cached) {
      state.fromCache = true;
      return await cached.json();
    }
    throw e;
  }
}

/* ── Small render helpers ──────────────────────────────────────────── */
function postureChip(label, sevClass, big = false) {
  return `<span class="posture-chip ${sevClass} ${big ? "is-lg" : ""}">${esc(label)}</span>`;
}

function confidenceTag(level) {
  const k = String(level).toLowerCase();
  return `<span class="confidence-tag is-${k}"><span class="confidence-tag__swatch"></span>${esc(level)} confidence</span>`;
}

/* ── 7.1/7.3 Header + mission card ─────────────────────────────────── */
function renderHeader(b) {
  const m = b.mission;
  const actIcon = m.activity === "cave" ? icon("cave", "brand__mark") : icon("canyon", "brand__mark");
  document.getElementById("header").innerHTML = `
    <div class="brand">
      <img src="icons/logo.jpg" class="brand__logo" alt="UpstreamWX Weather Briefing" />
    </div>
    <div class="app-header__spacer"></div>
    <span class="activity-pill">${actIcon}${esc(m.activity)}</span>
  `;
}

function missionCard(b) {
  const m = b.mission;
  const start = new Date(m.window_start), end = new Date(m.window_end);
  const fmtD = start.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  const fmtT = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `
    <section class="card mission-card">
      <div class="mission-card__main">
        <div class="eyebrow">Mission</div>
        <h1 class="mission-card__title">${esc(m.name)}
          <button class="mission-card__edit" aria-label="Edit mission">${icon("edit", "")}</button>
        </h1>
        <div class="mission-card__meta">${fmtD} · ${fmtT(start)}–${fmtT(end)} ${esc(m.timezone)}</div>
        <div class="mission-card__meta"><span class="mono">${m.lat.toFixed(4)}, ${m.lon.toFixed(4)}</span> · Upstream HUC-12 <span class="mono">${m.huc12.join(", ")}</span></div>
      </div>
      <div class="mission-card__posture">
        <div class="eyebrow">Overall posture</div>
        ${postureChip(b.overall_posture, overallSevClass(b), true)}
        ${confidenceTag(b.overall_confidence)}
      </div>
    </section>`;
}

function overallSevClass(b) {
  return { Minimal: "sev-minimal", Elevated: "sev-elevated", High: "sev-high", Extreme: "sev-extreme" }[b.overall_posture] || "";
}

/* ── 7.2 Tab bar ───────────────────────────────────────────────────── */
function renderTabs() {
  document.getElementById("tabs").innerHTML = TABS.map(
    (t) => `<button class="tab" role="tab" data-tab="${t.id}" aria-selected="${t.id === state.tab}">
      ${icon(t.id, "tab__icon")}<span>${t.label}</span></button>`
  ).join("");
  document.querySelectorAll(".tab").forEach((el) =>
    el.addEventListener("click", () => selectTab(el.dataset.tab))
  );
}

function selectTab(id) {
  state.tab = id;
  document.querySelectorAll(".tab").forEach((el) =>
    el.setAttribute("aria-selected", String(el.dataset.tab === id))
  );
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${id}`));
  document.querySelector("main").scrollTo({ top: 0 });
  if (id === "map" && state.briefing) {
    requestAnimationFrame(() => initLeafletMap(state.briefing));
  }
}

/* ── 7.4 Overview ──────────────────────────────────────────────────── */
function renderOverview(b) {
  const hazards = b.bluf
    .map((h) => {
      const win = h.window ? `<span class="hazard-line__window">${esc(h.window)}</span>` : "";
      return `<button class="hazard-line" data-goto="hazards">
        ${icon(h.hazard, "hazard-line__icon")}
        <div class="hazard-line__body">
          <div class="hazard-line__name">${HAZARD_LABELS[h.hazard]}</div>${win}
        </div>
        <div class="hazard-line__right">${postureChip(h.label, h.severity_class)}${confidenceTag(h.confidence)}</div>
      </button>`;
    })
    .join("");

  const metrics = b.metrics
    .map(
      (m) => `<div class="metric-card">
        <div class="metric-card__label">${icon(m.icon, "metric-card__icon")}<span class="eyebrow">${esc(m.label)}</span></div>
        <div class="metric-card__value">${esc(m.value)}<span style="font-size:14px;color:var(--color-text-muted)">${esc(m.unit)}</span></div>
        <div class="metric-card__sub">${esc(m.sub)}</div>
      </div>`
    )
    .join("");

  const phases = b.phases
    .map(
      (p) => `<div class="phase-seg">
        <div class="phase-seg__name">${PHASE_LABELS[p.phase]}</div>
        <div class="phase-seg__time">${esc(p.window)}</div>
        <div class="phase-seg__lead">${esc(p.lead_label)}</div>
        <div class="phase-seg__hazards">${esc(p.applicable)}</div>
        ${p.note ? `<div class="phase-seg__note">${esc(p.note)}</div>` : ""}
      </div>`
    )
    .join("");

  document.getElementById("view-overview").innerHTML = `
    ${missionCard(b)}
    <section class="card">
      <p class="summary">${esc(b.summary)}
        ${b.framed ? '<span class="framed-by">Summary wording only — all posture and severity values are deterministic engine output, not model-derived.</span>' : ""}
      </p>
    </section>
    <section class="card"><h2 class="section-title" style="margin-bottom:var(--space-2)">Hazards</h2>
      <div class="hazard-list">${hazards}</div>
    </section>
    <div class="metric-grid">${metrics}</div>
    <section class="card"><h2 class="section-title" style="margin-bottom:var(--space-3)">Phases</h2>
      <div class="phase-strip">${phases}</div>
      ${b.mission.phases_inferred ? '<div class="phase-seg__note" style="margin-top:var(--space-3)">Phases inferred from the overall window: approach = first hour, egress = last hour.</div>' : ""}
    </section>
    <div class="disclaimer">Planning reference only — not a forecast, not a decision. Conditions change fast and models can be wrong. Verify against the official NWS sources linked in Resources, and let what you see in the field overrule this briefing.</div>`;

  document.querySelectorAll('[data-goto="hazards"]').forEach((el) =>
    el.addEventListener("click", () => selectTab("hazards"))
  );
}

/* ── 7.6 Forecast ──────────────────────────────────────────────────── */
function renderForecast(b) {
  const f = b.forecast_hourly;
  const head = `<tr><th>Hour</th>${f.hours.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
  const rows = f.rows
    .map((r) => `<tr><td>${esc(r.label)}</td>${r.values.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`)
    .join("");

  document.getElementById("view-forecast").innerHTML = `
    <section class="card">
      <div class="forecast-tabs" style="margin-bottom:var(--space-3)">
        <button aria-selected="true">Hourly</button><button aria-selected="false">Daily</button><button aria-selected="false">Table</button>
      </div>
      <table class="fc-table"><thead>${head}</thead><tbody>${rows}</tbody></table>
    </section>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-2)">Temperature (°F)</h2>
      ${lineChart([b.temp_series.air, b.temp_series.feels], f.hours, ["var(--sev-elevated)", "var(--color-brand)"])}
      <div class="chart-caption">Air (amber) · Feels-like (cyan)</div>
    </section>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-2)">Wind &amp; gusts (mph)</h2>
      ${lineChart([b.wind_series.wind, b.wind_series.gust], f.hours, ["var(--color-brand)", "var(--color-text-muted)"])}
      <div class="chart-caption">Wind (cyan) · Gusts (grey)</div>
    </section>
    <div class="disclaimer">Forecast detail is the drill-down behind the hazard drivers. Derived fields from Open-Meteo (HRRR-derived); ensemble probabilities from in-house SREF + HREF.</div>`;
}

// Minimal dependency-free SVG line chart.
function lineChart(series, labels, colors) {
  const W = 320, H = 128;
  const padL = 30, padR = 8, padT = 8, padB = 20;
  const all = series.flat();
  const min = Math.min(...all), max = Math.max(...all);
  const span = max - min || 1;
  const x = (i) => padL + (i * (W - padL - padR)) / (labels.length - 1);
  const y = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  const GRID_N = 4;
  const grids = Array.from({ length: GRID_N }, (_, i) => {
    const frac = i / (GRID_N - 1);
    const val = min + frac * span;
    const yp = y(val).toFixed(1);
    return `<line x1="${padL}" y1="${yp}" x2="${W - padR}" y2="${yp}" stroke="var(--color-border)" stroke-width="1"/>
      <text x="${padL - 3}" y="${yp}" fill="var(--color-text-muted)" font-size="8" text-anchor="end" dominant-baseline="middle">${Math.round(val)}</text>`;
  }).join("");

  const lines = series
    .map((s, si) => {
      const d = s.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
      return `<path d="${d}" fill="none" stroke="${colors[si]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
    })
    .join("");
  const ticks = labels
    .map((l, i) => `<text x="${x(i)}" y="${H - 5}" fill="var(--color-text-muted)" font-size="9" text-anchor="middle">${esc(l)}</text>`)
    .join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img">${grids}${ticks}${lines}</svg>`;
}

/* ── 7.9 Hazards (phase-primary timeline + details) ────────────────── */
function barClass(cell) {
  if (!cell || cell.applicable === false) return "timeline__bar is-na";
  const w = cell.persistent ? "" : "is-windowed";
  const conf = `conf-${cell.confidence}`;
  return `timeline__bar bar-${cell.severity} ${w} ${conf}`;
}

function renderHazards(b) {
  const phaseHead = `<div></div>${["approach", "technical", "egress"]
    .map((p) => `<div class="timeline__phase-head">${PHASE_LABELS[p]}</div>`)
    .join("")}`;

  const rows = b.timeline
    .map((row) => {
      const cells = row.cells
        .map((c) => `<div class="timeline__cell"><div class="${barClass(c)}"></div></div>`)
        .join("");
      return `<div class="timeline__row">
        <div class="timeline__label">${icon(row.hazard, "")}${HAZARD_LABELS[row.hazard]}</div>${cells}
      </div>`;
    })
    .join("");

  const legend = `<div class="legend">
    ${["minimal", "elevated", "high", "extreme"]
      .map((s) => `<span class="legend__item"><span class="legend__swatch bar-${s}"></span>${s[0].toUpperCase() + s.slice(1)}</span>`)
      .join("")}
    <span class="legend__item"><span class="legend__swatch" style="background:var(--color-text-secondary)"></span>Solid = higher confidence</span>
    <span class="legend__item"><span class="legend__swatch" style="background:var(--color-text-secondary);opacity:0.35"></span>Faded = lower confidence</span>
  </div>`;

  const details = b.hazard_detail
    .map(
      (h) => `<details class="hazard-detail">
      <summary class="hazard-detail__summary">
        ${icon(h.hazard, "icon")}
        <span class="hazard-detail__name">${HAZARD_LABELS[h.hazard]}</span>
        ${postureChip(h.label, h.severity_class)}
        ${icon("chevron", "hazard-detail__chev")}
      </summary>
      <div class="hazard-detail__body">
        <div style="margin-top:var(--space-3)">${confidenceTag(h.confidence)}</div>
        <h4>Key drivers</h4><ul>${h.drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
        <h4>Threshold logic</h4>
        <p style="font-size:var(--text-label);color:var(--color-text-secondary);margin:var(--space-1) 0 0">${esc(h.logic)}</p>
        ${h.assumptions.map((a) => `<div class="assumption">${icon("alert", "")}<span>${esc(a)}</span></div>`).join("")}
      </div>
    </details>`
    )
    .join("");

  document.getElementById("view-hazards").innerHTML = `
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-2)">Hazards by phase</h2>
      <p style="font-size:var(--text-caption);color:var(--color-text-muted);margin:0 0 var(--space-3)">Organized by mission phase. A hazard appears only where it applies — no lightning across a sheltered technical span.</p>
      <div class="timeline">
        <div class="timeline__phases">${phaseHead}</div>
        ${rows}
      </div>
      ${legend}
    </section>
    <div style="display:flex;flex-direction:column;gap:var(--space-2)">${details}</div>
    <div class="disclaimer">Severity on the UpstreamWX ladder (Minimal / Elevated / High / Extreme); heat uses NWS Heat Index categories. Confidence shown as hatching and an explicit label; bar length distinguishes persistent from windowed hazards (display only).</div>`;
}

/* ── 7.11 Map ──────────────────────────────────────────────────────── */
function renderMap(b) {
  document.getElementById("view-map").innerHTML = `
    <div id="leaflet-map" aria-label="Mission area satellite map"></div>
    <div class="disclaimer">Planning map — satellite imagery via Esri. Mission point marks entry coordinates. No radar layer in v1.</div>`;
}

let _leafletMap = null;

function initLeafletMap(b) {
  const container = document.getElementById("leaflet-map");
  if (!container || !window.L) return;
  if (_leafletMap) { _leafletMap.invalidateSize(); return; }

  const m = b.mission;
  _leafletMap = L.map(container, { zoomControl: true, attributionControl: true })
    .setView([m.lat, m.lon], 13);

  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { attribution: "Tiles &copy; Esri &mdash; Esri, i-cubed, USDA, USGS, AEX, GeoEye", maxZoom: 18 }
  ).addTo(_leafletMap);

  // Place-name / boundary labels on top of satellite
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 18, opacity: 0.9 }
  ).addTo(_leafletMap);

  L.circleMarker([m.lat, m.lon], {
    radius: 9, fillColor: "#38bdf8", color: "#fff", weight: 2.5, fillOpacity: 1,
  }).addTo(_leafletMap)
    .bindTooltip(esc(m.name), { permanent: true, direction: "top", className: "map-tooltip" });

  state.mapInitialized = true;
}

/* ── 7.12 Resources ────────────────────────────────────────────────── */
function renderResources(b) {
  const links = b.resources
    .map(
      (r) => `<a class="resource-link" href="${esc(r.url)}" target="_blank" rel="noopener">
      ${icon(r.icon, "")}
      <div class="resource-link__body"><div class="resource-link__title">${esc(r.title)}</div>
        <div class="resource-link__sub">${esc(r.sub)}</div></div>
      ${icon("external", "resource-link__ext")}
    </a>`
    )
    .join("");

  const degraded = b.degraded
    ? `<div class="assumption" style="border-color:var(--color-warn)">${icon("alert", "")}<span>${esc(b.warnings.join(" "))}</span></div>`
    : "";

  document.getElementById("view-resources").innerHTML = `
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-3)">Verify against NWS</h2>
      ${links}
      ${degraded}
    </section>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-3)">Export &amp; offline</h2>
      <button class="btn-primary" id="export-pdf">Export briefing to PDF</button>
      <p style="font-size:var(--text-caption);color:var(--color-text-muted);margin-top:var(--space-3)">
        The most recent briefing is cached for offline review. ${b.cached || state.fromCache ? "Currently showing a cached copy." : "Online — showing the latest cycle."}
        Threshold matrix version <span class="mono">${esc(b.threshold_version)}</span>.
      </p>
    </section>
    <div class="disclaimer">UpstreamWX — planning reference only. Not an official forecast or warning. The go/no-go decision is the user's and the party's.</div>`;

  const pdf = document.getElementById("export-pdf");
  if (pdf) pdf.addEventListener("click", () => window.print());
}

/* ── Status / currency line (FR-39, FR-41) ─────────────────────────── */
function renderStatus(b) {
  const gen = new Date(b.generated_at);
  const t = gen.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
  const cached = b.cached || state.fromCache;
  document.getElementById("status").innerHTML = `
    ${cached ? `<span class="cached-badge">${icon("wifi_off", "")} Cached</span>` : ""}
    <span class="status-line__currency">Briefing current as of ${esc(t)} ${esc(b.mission.timezone)}</span>
    ${b.degraded ? `<span class="degraded-note">· one source degraded</span>` : ""}`;
}

/* ── First-run acknowledgment (FR-31, Appendix C §17.1) ────────────── */
function maybeShowAck() {
  if (localStorage.getItem(ACK_KEY)) return;
  const modal = document.getElementById("ack");
  modal.hidden = false;
  document.getElementById("ack-accept").addEventListener("click", () => {
    localStorage.setItem(ACK_KEY, new Date().toISOString());
    modal.hidden = true;
  });
}

/* ── Bootstrap ─────────────────────────────────────────────────────── */
async function main() {
  renderTabs();
  maybeShowAck();
  let b;
  try {
    b = await loadBriefing();
  } catch (e) {
    document.getElementById("view-overview").innerHTML =
      `<section class="card"><p class="summary">Could not load a briefing and no cached copy is available offline.</p></section>`;
    return;
  }
  state.briefing = b;
  renderHeader(b);
  renderOverview(b);
  renderForecast(b);
  renderMap(b);
  renderHazards(b);
  renderResources(b);
  renderStatus(b);
  selectTab(state.tab);

  window.addEventListener("online", () => renderStatus(b));
  window.addEventListener("offline", () => { state.fromCache = true; renderStatus(b); });
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

main();
