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

// 24-hour clock with a colon: "1300" -> "13:00", "1300–1400" -> "13:00–14:00".
// Only rewrites bare HHMM tokens, so years/HUC codes are left alone.
const fmtClock = (s) => String(s).replace(/\b(\d{2})(\d{2})\b/g, "$1:$2");

let state = { briefing: null, fromCache: false, tab: "overview", mapInitialized: false };

/* ── Acronym glossary (Resources card + tap-to-define) ─────────────────
 * Definitions for the acronyms that show up in the BLUF/SITREP and hazard
 * cards. Surfaced two ways: a glossary card in Resources and inline
 * tap-to-define wherever the term appears in rendered text. Reference-only —
 * plain definitions, no posture or recommendation (PRD §6.8). */
const GLOSSARY = [
  ["BLUF", "Bottom Line Up Front", "The one-line summary at the top of the briefing, the headline before the detail."],
  ["SITREP", "Situation Report", "The structured, section-by-section hazard report this briefing is built around."],
  ["NWS", "National Weather Service", "The U.S. agency that issues official forecasts, watches, and warnings. Always the authority to verify against."],
  ["WFO", "Weather Forecast Office", "A local NWS office responsible for forecasts and warnings in its area (e.g. WFO MRX)."],
  ["AFD", "Area Forecast Discussion", "The forecaster's plain-language reasoning behind the local forecast, published by each WFO."],
  ["SPC", "Storm Prediction Center", "The NWS center that issues severe-thunderstorm and tornado outlooks."],
  ["SREF", "Short-Range Ensemble Forecast", "An NWS ensemble of model runs, used here for hazard probabilities beyond the same-day window."],
  ["HREF", "High-Resolution Ensemble Forecast", "An NWS high-resolution (~3 km) ensemble, used here for same-day (~6 to 36 h) probabilities."],
  ["HRRR", "High-Resolution Rapid Refresh", "An hourly-updating high-resolution NWS model. The Open-Meteo derived fields shown here are HRRR-based."],
  ["HUC-12", "Hydrologic Unit Code (12-digit)", "A USGS watershed identifier. HUC-12 is the small sub-watershed scale used to aggregate rain upstream of your point."],
  ["HUC", "Hydrologic Unit Code", "A USGS nested watershed identifier. Smaller units (more digits) mean a finer drainage area."],
  ["QPF", "Quantitative Precipitation Forecast", "Forecast precipitation amount (e.g. inches) over a given period."],
  ["NEP", "Neighborhood Ensemble Probability", "The probability an event occurs within a neighborhood of a point across the ensemble members."],
];

const GLOSSARY_MAP = new Map(GLOSSARY.map(([acr, term, def]) => [acr, { term, def }]));
// Single alternation, longest acronym first so "HUC-12" wins over "HUC".
const GLOSSARY_RE = new RegExp(
  "\\b(" +
    GLOSSARY.map(([a]) => a)
      .sort((a, b) => b.length - a.length)
      .map((a) => a.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
      .join("|") +
    ")\\b",
  "g"
);

// Wrap known acronyms in rendered text with tap-to-define buttons. Walks text
// nodes so existing markup/escaping is preserved; skips links, buttons, the
// glossary card itself, and already-linked terms.
function linkifyAcronyms(root) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      GLOSSARY_RE.lastIndex = 0;
      if (!node.nodeValue || !GLOSSARY_RE.test(node.nodeValue)) return NodeFilter.FILTER_REJECT;
      if (node.parentElement.closest("a, button, .glossary-term, [data-no-glossary]"))
        return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);

  for (const node of targets) {
    const frag = document.createDocumentFragment();
    let last = 0;
    const text = node.nodeValue;
    GLOSSARY_RE.lastIndex = 0;
    let m;
    while ((m = GLOSSARY_RE.exec(text))) {
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "glossary-term";
      btn.dataset.acr = m[1];
      btn.textContent = m[1];
      frag.appendChild(btn);
      last = m.index + m[1].length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
}

let _glossaryPop = null;

function hideGlossaryPopover() {
  if (_glossaryPop) _glossaryPop.hidden = true;
}

function showGlossaryPopover(acr, anchor) {
  const entry = GLOSSARY_MAP.get(acr);
  if (!entry) return;
  if (!_glossaryPop) {
    _glossaryPop = document.createElement("div");
    _glossaryPop.className = "glossary-pop";
    _glossaryPop.setAttribute("role", "tooltip");
    document.body.appendChild(_glossaryPop);
  }
  _glossaryPop.innerHTML = `<div class="glossary-pop__acr">${esc(acr)}</div>
    <div class="glossary-pop__term">${esc(entry.term)}</div>
    <div class="glossary-pop__def">${esc(entry.def)}</div>`;
  _glossaryPop.hidden = false;

  // Position under the term, flipping above if it would overflow, clamped to the viewport.
  const r = anchor.getBoundingClientRect();
  const pop = _glossaryPop.getBoundingClientRect();
  const margin = 8;
  let top = r.bottom + 6;
  if (top + pop.height > window.innerHeight - margin) top = Math.max(margin, r.top - pop.height - 6);
  let left = r.left + r.width / 2 - pop.width / 2;
  left = Math.max(margin, Math.min(left, window.innerWidth - pop.width - margin));
  _glossaryPop.style.top = `${Math.round(top)}px`;
  _glossaryPop.style.left = `${Math.round(left)}px`;
}

// One delegated handler for every tap-to-define term, plus dismissal.
function initGlossaryInteractions() {
  document.addEventListener("click", (e) => {
    const term = e.target.closest(".glossary-term");
    if (term) {
      e.preventDefault();
      e.stopPropagation();
      if (!_glossaryPop || _glossaryPop.hidden || _glossaryPop.dataset.acr !== term.dataset.acr) {
        showGlossaryPopover(term.dataset.acr, term);
        if (_glossaryPop) _glossaryPop.dataset.acr = term.dataset.acr;
      } else {
        hideGlossaryPopover();
      }
      return;
    }
    if (!e.target.closest(".glossary-pop")) hideGlossaryPopover();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideGlossaryPopover(); });
  window.addEventListener("scroll", hideGlossaryPopover, true);
  window.addEventListener("resize", hideGlossaryPopover);
}

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

function confidenceTag(level, big = false) {
  // Three-stop orange track (low/moderate/high). The stop for the engine's level
  // is filled, the others hollow; the level rides the position, not hue (FR-36).
  // The thin→thick segment ramp is fixed decoration. Display-only (FR-17).
  const k = String(level).toLowerCase();
  const stops = ["low", "moderate", "high"]
    .map((s) => `<span class="confidence__stop confidence__stop--${s} ${s === k ? "is-active" : ""}"></span>`)
    .join("");
  return `<div class="confidence ${big ? "is-lg" : ""}" title="${esc(level)} confidence">
    <div class="confidence__label">${esc(level)} confidence</div>
    <div class="confidence__track">
      <span class="confidence__seg confidence__seg--a"></span>
      <span class="confidence__seg confidence__seg--b"></span>
      ${stops}
    </div>
  </div>`;
}

/* ── 7.1/7.3 Header + mission card ─────────────────────────────────── */
function renderHeader(b) {
  const m = b.mission;
  const actSrc = m.activity === "cave" ? "icons/cave.png" : "icons/canyon.png";
  document.getElementById("header").innerHTML = `
    <div class="brand">
      <img src="icons/logo.jpg" class="brand__logo" alt="UpstreamWX Weather Briefing" />
    </div>
    <div class="app-header__spacer"></div>
    <span class="activity-pill"><img src="${actSrc}" class="activity-pill__icon" alt="" />${esc(m.activity)}</span>
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
        <div class="mission-card__meta"><span class="mono">${m.lat.toFixed(4)}, ${m.lon.toFixed(4)}</span></div>
        ${b.watershed ? `<div class="mission-card__meta">Watershed area <span class="mono">${b.watershed.area_sq_mi.toFixed(1)} mi²</span></div>` : ""}
      </div>
      <div class="mission-card__posture">
        <div class="eyebrow">Overall posture</div>
        ${postureChip(b.overall_posture, overallSevClass(b), true)}
        ${confidenceTag(b.overall_confidence, true)}
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
  hideGlossaryPopover();
  document.querySelectorAll(".tab").forEach((el) =>
    el.setAttribute("aria-selected", String(el.dataset.tab === id))
  );
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== `view-${id}`));
  document.querySelector("main").scrollTo({ top: 0 });
  if (id === "map" && state.briefing) {
    requestAnimationFrame(() => initLeafletMap(state.briefing));
  }
  if (id === "forecast" && _fcSync) requestAnimationFrame(_fcSync);
}

/* ── 7.4 Overview ──────────────────────────────────────────────────── */
function renderOverview(b) {
  const hazards = b.bluf
    .map((h) => {
      const win = h.window ? `<span class="hazard-line__window">${esc(fmtClock(h.window))}</span>` : "";
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
        <div class="phase-seg__time">${esc(fmtClock(p.window))}</div>
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
    <section class="card"><h2 class="section-title" style="margin-bottom:var(--space-3)">Mission Phases</h2>
      <div class="phase-strip">${phases}</div>
      ${b.mission.phases_inferred ? '<div class="phase-seg__note" style="margin-top:var(--space-3)">Phases inferred from the overall window: approach = first hour, egress = last hour.</div>' : ""}
    </section>
    <div class="disclaimer">Planning reference only — not a forecast, not a decision. Conditions change fast and models can be wrong. Verify against the official NWS sources linked in Resources, and let what you see in the field overrule this briefing.</div>`;

  document.querySelectorAll('[data-goto="hazards"]').forEach((el) =>
    el.addEventListener("click", () => selectTab("hazards"))
  );
  linkifyAcronyms(document.getElementById("view-overview"));
}

/* ── 7.6 Forecast ──────────────────────────────────────────────────── */
function renderForecast(b) {
  const f = b.forecast_hourly;
  const hours = f.hours.map(fmtClock);
  const head = `<tr><th>Hour</th>${hours.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>`;
  const rows = f.rows
    .map((r) => `<tr><td>${esc(r.label)}</td>${r.values.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`)
    .join("");

  document.getElementById("view-forecast").innerHTML = `
    <section class="card">
      <div class="forecast-tabs" style="margin-bottom:var(--space-3)">
        <button aria-selected="true">Hourly</button><button aria-selected="false">Daily</button><button aria-selected="false">Table</button>
      </div>
      <div class="fc-scroll">
        <div class="fc-scroll__viewport" data-fc-scroll>
          <table class="fc-table"><thead>${head}</thead><tbody>${rows}</tbody></table>
        </div>
        <div class="fc-scroll__more" aria-hidden="true">${icon("chevron", "fc-scroll__chev")}</div>
      </div>
    </section>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-2)">Temperature (°F)</h2>
      ${lineChart([b.temp_series.air, b.temp_series.feels], hours, ["var(--sev-high)", "var(--sev-extreme)"])}
      <div class="chart-caption">Air (orange) · Feels-like (red)</div>
    </section>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-2)">Wind &amp; gusts (mph)</h2>
      ${lineChart([b.wind_series.wind, b.wind_series.gust], hours, ["var(--color-brand)", "var(--color-text-muted)"])}
      <div class="chart-caption">Wind (cyan) · Gusts (grey)</div>
    </section>
    <div class="disclaimer">Forecast detail is the drill-down behind the hazard drivers. Derived fields from Open-Meteo (HRRR-derived); ensemble probabilities from in-house SREF + HREF.</div>`;
  flushChartInits();
  initForecastScroll();
  linkifyAcronyms(document.getElementById("view-forecast"));
}

// Toggle the right-edge "more" indicator on the hourly table as it scrolls (FR — long windows overflow).
let _fcSync = null;
function initForecastScroll() {
  const wrap = document.querySelector("[data-fc-scroll]");
  if (!wrap) return;
  _fcSync = () => {
    const atEnd = wrap.scrollLeft + wrap.clientWidth >= wrap.scrollWidth - 1;
    wrap.parentElement.classList.toggle(
      "is-scrollable", wrap.scrollWidth > wrap.clientWidth + 1 && !atEnd
    );
  };
  wrap.addEventListener("scroll", _fcSync, { passive: true });
  window.addEventListener("resize", _fcSync);
  requestAnimationFrame(_fcSync);
}

let _chartSeq = 0;
const _pendingCharts = [];

// Interactive SVG line chart with touch/mouse crosshair.
function lineChart(series, labels, colors) {
  const id = `chart-${++_chartSeq}`;
  const W = 320, H = 128;
  const padL = 30, padR = 8, padT = 8, padB = 20;
  const all = series.flat();
  const min = Math.min(...all), max = Math.max(...all);
  const span = max - min || 1;
  const xFn = (i) => padL + (i * (W - padL - padR)) / (labels.length - 1);
  const yFn = (v) => padT + (1 - (v - min) / span) * (H - padT - padB);

  const GRID_N = 4;
  const grids = Array.from({ length: GRID_N }, (_, i) => {
    const frac = i / (GRID_N - 1);
    const val = min + frac * span;
    const yp = yFn(val).toFixed(1);
    return `<line x1="${padL}" y1="${yp}" x2="${W - padR}" y2="${yp}" stroke="var(--color-border)" stroke-width="1"/>
      <text x="${padL - 3}" y="${yp}" fill="var(--color-text-muted)" font-size="8" text-anchor="end" dominant-baseline="middle">${Math.round(val)}</text>`;
  }).join("");

  const lines = series.map((s, si) => {
    const d = s.map((v, i) => `${i ? "L" : "M"}${xFn(i).toFixed(1)} ${yFn(v).toFixed(1)}`).join(" ");
    return `<path d="${d}" fill="none" stroke="${colors[si]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
  }).join("");

  // Anchor the first label to its start and the last to its end so neither
  // overruns the chart edge (the rightmost label was being clipped).
  const ticks = labels.map((l, i) => {
    const anchor = i === 0 ? "start" : i === labels.length - 1 ? "end" : "middle";
    return `<text x="${xFn(i)}" y="${H - 5}" fill="var(--color-text-muted)" font-size="9" text-anchor="${anchor}">${esc(l)}</text>`;
  }).join("");

  // Crosshair overlay — sits on top of lines; hidden until interaction
  const xhHLines = colors.map((c, si) =>
    `<line data-xh="hl" data-si="${si}" x1="${padL}" y1="0" x2="${padL}" y2="0" stroke="${c}" stroke-width="1" opacity="0.55" stroke-dasharray="2 3"/>`
  ).join("");
  const xhDots = colors.map((c, si) =>
    `<circle data-xh="dot" data-si="${si}" r="4" fill="${c}" stroke="var(--color-surface)" stroke-width="1.5" cx="-99" cy="-99"/>`
  ).join("");
  // Each value gets a surface-colored plate behind it so the label never reads on top of a data line.
  const xhVals = colors.map((c, si) =>
    `<rect data-xh="valbg" data-si="${si}" fill="var(--color-surface)" fill-opacity="0.85" rx="2" height="12" x="-99" y="-99" width="0"/>
     <text data-xh="val" data-si="${si}" font-size="9" font-weight="600" fill="${c}" dominant-baseline="middle" x="-99" y="-99"></text>`
  ).join("");

  const xhair = `<g data-xh="group" visibility="hidden" pointer-events="none">
    <line data-xh="v" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="var(--color-text-muted)" stroke-width="1" opacity="0.4"/>
    ${xhHLines}${xhDots}${xhVals}
    <rect data-xh="xlabel-bg" fill="var(--color-surface-3)" rx="2" height="11" y="${H - padB + 2}" x="0" width="16"/>
    <text data-xh="xlabel" font-size="9" font-weight="600" fill="var(--color-text)" text-anchor="middle" y="${H - 5}" x="0"></text>
  </g>`;

  _pendingCharts.push({ id, series, labels, min, span, W, H, padL, padR, padT, padB });
  return `<svg id="${id}" class="chart" viewBox="0 0 ${W} ${H}" role="img">${grids}${ticks}${lines}${xhair}</svg>`;
}

function flushChartInits() {
  _pendingCharts.forEach(initChartInteractivity);
  _pendingCharts.length = 0;
}

function initChartInteractivity({ id, series, labels, min, span, W, H, padL, padR, padT, padB }) {
  const svg = document.getElementById(id);
  if (!svg) return;

  const group   = svg.querySelector('[data-xh="group"]');
  const vLine   = svg.querySelector('[data-xh="v"]');
  const xlabel  = svg.querySelector('[data-xh="xlabel"]');
  const xlbg    = svg.querySelector('[data-xh="xlabel-bg"]');
  const hLines  = [...svg.querySelectorAll('[data-xh="hl"]')];
  const dots    = [...svg.querySelectorAll('[data-xh="dot"]')];
  const vals    = [...svg.querySelectorAll('[data-xh="val"]')];
  const valBgs  = [...svg.querySelectorAll('[data-xh="valbg"]')];

  const dataW = W - padL - padR;
  const dataH = H - padT - padB;
  const yFn = (v) => padT + (1 - (v - min) / span) * dataH;

  function update(clientX) {
    const rect = svg.getBoundingClientRect();
    const svgX = ((clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(labels.length - 1, Math.round((svgX - padL) * (labels.length - 1) / dataW)));
    const cx = padL + (i * dataW) / (labels.length - 1);

    // Vertical line
    vLine.setAttribute("x1", cx); vLine.setAttribute("x2", cx);

    // X label with fitted background
    const lbl = labels[i];
    const bgW = lbl.length * 6 + 4;
    xlbg.setAttribute("x", cx - bgW / 2); xlbg.setAttribute("width", bgW);
    xlabel.setAttribute("x", cx); xlabel.textContent = lbl;

    series.forEach((s, si) => {
      const v = s[i];
      const cy = yFn(v);

      // Horizontal guide from Y-axis to dot
      hLines[si].setAttribute("y1", cy); hLines[si].setAttribute("y2", cy);
      hLines[si].setAttribute("x2", cx);

      // Dot at intersection
      dots[si].setAttribute("cx", cx); dots[si].setAttribute("cy", cy);

      // Value label: lift off the data line — series 0 above its dot, series 1 below —
      // and clamp inside the plot so it never sits on top of the curve (or clips the edge).
      const txt = String(Math.round(v));
      const dir = si === 0 ? -1 : 1;
      const labelY = Math.max(padT + 7, Math.min(H - padB - 7, cy + dir * 10));
      const inRight = cx > W / 2;
      const x = inRight ? cx - 7 : cx + 7;
      vals[si].setAttribute("x", x);
      vals[si].setAttribute("text-anchor", inRight ? "end" : "start");
      vals[si].setAttribute("y", labelY);
      vals[si].textContent = txt;

      // Plate sized to the text, anchored on the same side as the label.
      const bgW = txt.length * 6 + 6;
      valBgs[si].setAttribute("x", (inRight ? x - bgW + 3 : x - 3).toFixed(1));
      valBgs[si].setAttribute("y", (labelY - 6).toFixed(1));
      valBgs[si].setAttribute("width", bgW);
    });

    group.setAttribute("visibility", "visible");
  }

  function hide() { group.setAttribute("visibility", "hidden"); }

  let captured = false;
  svg.addEventListener("pointerdown", (e) => {
    captured = true;
    svg.setPointerCapture(e.pointerId);
    update(e.clientX);
  });
  svg.addEventListener("pointermove", (e) => {
    // Show on mouse hover without press; show on any captured (touch) drag
    if (e.pointerType === "mouse" || captured) update(e.clientX);
  });
  svg.addEventListener("pointerup",     () => { captured = false; });
  svg.addEventListener("pointercancel", () => { captured = false; hide(); });
  svg.addEventListener("pointerleave",  () => { if (!captured) hide(); });
}

/* ── 7.9 Hazards (phase-primary timeline + details) ────────────────── */
function barClass(cell) {
  if (!cell || cell.applicable === false) return "timeline__bar is-na";
  const w = cell.persistent ? "" : "is-windowed";
  const conf = `conf-${cell.confidence}`;
  return `timeline__bar bar-${cell.severity} ${w} ${conf}`;
}

// Render the engine's threshold logic as one tier per line, with the internal
// appendix/section citation stripped (user-facing copy carries no references).
function thresholdLogicHtml(logic) {
  const lines = String(logic)
    .replace(/\s*\((?:Appendix B|§)[^)]*\)/g, "")
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean);
  return `<ul class="logic-list">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
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
    <span class="legend__item"><span class="legend__swatch legend__swatch--conf-low" style="background:var(--color-text-secondary)"></span>Striped = lower confidence</span>
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
        <div class="hazard-detail__confidence">${confidenceTag(h.confidence)}</div>
        <h4>Key drivers</h4><ul>${h.drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
        <h4>Threshold logic</h4>
        ${thresholdLogicHtml(h.logic)}
        ${h.assumptions.map((a) => `<div class="assumption">${icon("alert", "")}<span>${esc(a)}</span></div>`).join("")}
      </div>
    </details>`
    )
    .join("");

  document.getElementById("view-hazards").innerHTML = `
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-3)">Hazards by phase</h2>
      <div class="timeline">
        <div class="timeline__phases">${phaseHead}</div>
        ${rows}
      </div>
      ${legend}
    </section>
    <div style="display:flex;flex-direction:column;gap:var(--space-2)">${details}</div>
    <div class="disclaimer">Severity on the UpstreamWX ladder (Minimal / Elevated / High / Extreme); heat uses NWS Heat Index categories. Confidence shown as hatching and an explicit label; bar length distinguishes persistent from windowed hazards (display only).</div>`;
  linkifyAcronyms(document.getElementById("view-hazards"));
}

/* ── 7.11 Map ──────────────────────────────────────────────────────── */
function renderMap(b) {
  document.getElementById("view-map").innerHTML = `
    <div id="leaflet-map" aria-label="Mission area topographic map"></div>
    <div class="disclaimer">Planning map. The shaded basin is the approximate upstream watershed feeding the mission point. Tap either for details.</div>`;
}

let _leafletMap = null;
let _poiMarker = null;
let _moveMode = false;

function poiPopupHtml(m) {
  return `<div class="map-pop">
    <div class="map-pop__title">${esc(m.name)}</div>
    <div class="map-pop__row"><span class="mono">${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}</span></div>
    <button class="map-pop__btn" data-move-point>Move point</button>
  </div>`;
}

function initLeafletMap(b) {
  const container = document.getElementById("leaflet-map");
  if (!container || !window.L) return;
  if (_leafletMap) { _leafletMap.invalidateSize(); return; }

  const m = b.mission;
  _leafletMap = L.map(container, { zoomControl: true, attributionControl: true, maxZoom: 16 })
    .setView([m.lat, m.lon], 13);

  // Dark topographic basemap: Esri dark-gray canvas + dark hillshade for terrain relief.
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
    { attribution: "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, USGS, NGA", maxZoom: 16 }
  ).addTo(_leafletMap);
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade_Dark/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 16, opacity: 0.45 }
  ).addTo(_leafletMap);
  // Place-name / boundary labels (kept under the vector overlays).
  L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 16, opacity: 0.9 }
  ).addTo(_leafletMap);

  // Upstream watershed: 20%-opacity blue fill, full-opacity thin border; tap for HUC + area.
  if (b.watershed?.geometry) {
    const w = b.watershed;
    const layer = L.geoJSON(w.geometry, {
      style: { color: "#38bdf8", weight: 1.5, opacity: 1, fillColor: "#38bdf8", fillOpacity: 0.2 },
    }).addTo(_leafletMap);
    layer.bindPopup(
      `<div class="map-pop">
        <div class="map-pop__title">Approximate Watershed</div>
        <div class="map-pop__row">HUC-12 <span class="mono">${esc(w.huc12.join(", "))}</span></div>
        <div class="map-pop__row">Area <span class="mono">${w.area_sq_mi.toFixed(1)} mi²</span></div>
      </div>`,
      { className: "map-popup" }
    );
    _leafletMap.fitBounds(layer.getBounds(), { padding: [24, 24] });
  }

  // Mission point: tap for coordinates + a move-point action.
  _poiMarker = L.circleMarker([m.lat, m.lon], {
    radius: 9, fillColor: "#fbbf24", color: "#fff", weight: 2.5, fillOpacity: 1,
  }).addTo(_leafletMap)
    .bindTooltip(esc(m.name), { permanent: true, direction: "top", className: "map-tooltip" })
    .bindPopup(poiPopupHtml(m), { className: "map-popup" });

  // Wire the "Move point" button each time the POI popup opens.
  _poiMarker.on("popupopen", (e) => {
    const btn = e.popup.getElement()?.querySelector("[data-move-point]");
    if (btn) btn.addEventListener("click", () => {
      _moveMode = true;
      container.classList.add("is-moving-point");
      _poiMarker.closePopup();
    });
  });

  // In move mode, the next map tap relocates the point (display-only on the mock data).
  _leafletMap.on("click", (e) => {
    if (!_moveMode) return;
    _moveMode = false;
    container.classList.remove("is-moving-point");
    m.lat = e.latlng.lat;
    m.lon = e.latlng.lng;
    _poiMarker.setLatLng(e.latlng);
    _poiMarker.setPopupContent(poiPopupHtml(m));
    renderOverview(b); // keep the mission card's coordinates in sync
  });

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

  const glossary = GLOSSARY.map(
    ([acr, term, def]) => `<div class="glossary-item">
      <div class="glossary-item__head">
        <span class="glossary-item__acr">${esc(acr)}</span>
        <span class="glossary-item__term">${esc(term)}</span>
      </div>
      <div class="glossary-item__def">${esc(def)}</div>
    </div>`
  ).join("");

  document.getElementById("view-resources").innerHTML = `
    <button class="about-link" id="open-about">
      ${icon("info", "")}
      <div class="resource-link__body">
        <div class="resource-link__title">About &amp; Methodology</div>
        <div class="resource-link__sub">How this briefing is calculated: the engine, data sources, and thresholds.</div>
      </div>
      ${icon("chevron", "about-link__chev")}
    </button>
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-3)">Verify against NWS</h2>
      ${links}
      ${degraded}
    </section>
    <section class="card" data-no-glossary>
      <h2 class="section-title" style="display:flex;align-items:center;gap:var(--space-2);margin-bottom:var(--space-3)">${icon("book", "section-title__icon")}Glossary</h2>
      <p style="font-size:var(--text-caption);color:var(--color-text-muted);margin:0 0 var(--space-3)">Acronyms used in the briefing. These terms are also tappable wherever they appear.</p>
      <div class="glossary-list">${glossary}</div>
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

  linkifyAcronyms(document.getElementById("view-resources"));
  const pdf = document.getElementById("export-pdf");
  if (pdf) pdf.addEventListener("click", () => window.print());
  const about = document.getElementById("open-about");
  if (about) about.addEventListener("click", openAbout);
}

/* ── About & methodology (FR-20 "how this is calculated") ──────────────
 * A reference sub-page of Resources, not a sixth primary view (FR-32):
 * documents the deterministic engine (FR-13/FR-19/NFR-4), data sourcing
 * (§8), and the Appendix B hazard thresholds (FR-20/FR-20a). Reference-only —
 * describes how postures are derived, never issues a recommendation. */
const ABOUT_SOURCES = [
  ["NWS API", "api.weather.gov", "Forecast discussions (AFD), watches, warnings, advisories, and NWS Heat Index categories. The authoritative anchor and a mandatory source.", "doc"],
  ["Open-Meteo", "HRRR-derived fields", "Derived numerical fields (QPF, precip probability, CAPE / lifted index, temperature, humidity, apparent temperature, wind) feeding all four hazard models.", "model"],
  ["SREF (in-house)", "NCEP GRIB2, processed server-side", "Short-Range Ensemble probabilities of precip and thunder, with member spread, over the upstream domain to the full planning horizon.", "model"],
  ["HREF (in-house)", "~3 km, same-day ~6 to 36 h", "High-Resolution Ensemble neighborhood probabilities (1 h / 3 h QPF, lightning, reflectivity) that sharpen the same-day window. The engine takes the higher of SREF and HREF.", "model"],
  ["SPC outlook", "Storm Prediction Center", "Categorical and probabilistic severe and thunderstorm outlook, a secondary cross-check for lightning.", "alert"],
  ["USGS NHD / WBD", "NLDI and Watershed Boundary Dataset", "The stream network and watershed boundaries used to delineate the upstream contributing basin (a pour-point trace, with a HUC-12 fallback).", "map"],
];

const ABOUT_THRESHOLDS = [
  ["flash_flood", "Flash flood", "Upstream contributing basin", [
    ["Extreme", "sev-extreme", "Active NWS Flash Flood Warning for the area or the upstream domain"],
    ["High", "sev-high", "Flash Flood Watch, or SREF P(precip/thunder) at or above 60% over the upstream domain with a convective-rate proxy"],
    ["Elevated", "sev-elevated", "SREF P 20 to 60% with measurable forecast precip, no watch or warning yet"],
    ["Minimal", "sev-minimal", "Low convective probability, dry upstream forecast"],
  ], "Same-day windows also evaluate HREF neighborhood P(QPF) and take the higher tier. Antecedent rain bumps up one tier. For slot canyons, a rate at or above 0.5 in/hr over the domain is treated as at least High."],
  ["lightning", "Lightning", "Activity point and approach corridor (excluded in the technical span)", [
    ["Extreme", "sev-extreme", "Active thunderstorm warning, or SREF P(tstm) at or above 70%, or SPC categorical thunder or severe"],
    ["High", "sev-high", "SREF P(tstm) 40 to 69%, or SPC Slight or Enhanced during an exposed phase"],
    ["Elevated", "sev-elevated", "SREF P(tstm) 15 to 39%, or SPC Marginal, or AFD mentions afternoon convection"],
    ["Minimal", "sev-minimal", "SREF P(tstm) below 15%, no convective mention"],
  ], "CAPE and lifted index modulate confidence and severity but never set the tier. HREF P(lightning) and P(reflectivity) sharpen the same-day window."],
  ["heat", "Heat stress", "Activity point, using NWS Heat Index categories", [
    ["Extreme Danger", "heat-extreme_danger", "Heat index at or above 125 °F"],
    ["Danger", "heat-danger", "103 to 124 °F"],
    ["Extreme Caution", "heat-extreme_caution", "90 to 103 °F"],
    ["Caution", "heat-caution", "80 to 90 °F"],
  ], "Uses the established NWS categories directly rather than the four-tier ladder. On the exertion-loaded approach, effective strain runs about one category hotter than ambient."],
  ["cold_wet", "Cold / wet hypothermia", "Activity point, apparent temperature, assuming the party exits wet", [
    ["Extreme", "sev-extreme", "Apparent temperature at or below 32 °F, wet at or below freezing"],
    ["High", "sev-high", "33 to 45 °F, strong risk for a wet party"],
    ["Elevated", "sev-elevated", "46 to 60 °F, the deceptively mild band"],
    ["Minimal", "sev-minimal", "Above 60 °F with low wind"],
  ], "Bands are intentionally warmer than dry-cold thresholds because wet clothing loses most of its insulation. A dry cave with no immersion may be discounted by roughly one tier."],
];

function renderAbout(b) {
  const sources = ABOUT_SOURCES.map(
    ([name, access, desc, ic]) => `<div class="about-source">
      ${icon(ic, "about-source__icon")}
      <div>
        <div class="about-source__name">${esc(name)} <span class="about-source__access">${esc(access)}</span></div>
        <div class="about-source__desc">${esc(desc)}</div>
      </div>
    </div>`
  ).join("");

  const thresholds = ABOUT_THRESHOLDS.map(
    ([hz, title, basis, rows, note]) => `<div class="about-haz">
      <div class="about-haz__head">${icon(hz, "about-haz__icon")}<span class="about-haz__title">${esc(title)}</span></div>
      <div class="about-haz__basis">${esc(basis)}</div>
      <div class="about-matrix">${rows
        .map(([tier, cls, cond]) => `<div class="about-matrix__row">
          <span class="posture-chip ${cls} about-matrix__tier">${esc(tier)}</span>
          <span class="about-matrix__cond">${esc(cond)}</span>
        </div>`)
        .join("")}</div>
      <p class="about-haz__note">${esc(note)}</p>
    </div>`
  ).join("");

  document.getElementById("view-about").innerHTML = `
    <button class="about-back" id="close-about">${icon("arrow_left", "about-back__icon")}Resources</button>
    <h1 class="about-title">About &amp; Methodology</h1>
    <p class="about-lede">UpstreamWX is a planning-reference briefing for caving and canyoneering. It gathers official and modeled weather, assesses four life-safety hazards (flash flooding, lightning, heat, and cold/wet hypothermia), and shows the reasoning. It never tells you whether to go.</p>
    <p class="about-p">The hazard posture labels (Minimal, Elevated, High, Extreme) follow standard risk-management terminology. As outdoor adventurers, our internal risk assessment is calibrated differently than most, so a posture like "High" or "Extreme" may read as stronger than you expect. Treat it as a prompt to look closer, not as a verdict.</p>

    <section class="card">
      <div class="eyebrow">The deterministic engine</div>
      <p class="about-p">Every hazard posture, confidence level, and window of concern is decided by a deterministic, documented rule engine. Identical inputs always produce an identical result. The Claude language model only frames the wording of the summary. It can never compute, raise, or lower a posture.</p>
      <ul class="about-list">
        <li>Four hazards are scored independently on a common scale (Minimal, Elevated, High, Extreme), except heat, which uses the NWS Heat Index categories.</li>
        <li>Each hazard applies only in the mission phases where it is relevant (approach, technical span, egress) and per activity type. A cave technical span is treated as isolated from surface weather and shows flash flood only.</li>
        <li>The overall mission posture is the maximum across all applicable hazards, and every hazard stays visible, so a high lightning posture on approach is never hidden behind a low flood posture.</li>
        <li>A confidence qualifier per hazard comes from SREF ensemble agreement and cross-source consistency, including SREF and HREF agreement on same-day windows.</li>
      </ul>
    </section>

    <section class="card">
      <div class="eyebrow">Data sourcing</div>
      <p class="about-p">Providers sit behind a common interface, so the engine never depends on a specific source. If a non-mandatory source is unavailable the briefing still renders, marking that input as unavailable.</p>
      <div class="about-sources">${sources}</div>
    </section>

    <section class="card">
      <div class="eyebrow">How the watershed is delineated</div>
      <p class="about-p">Flash-flood risk is assessed over the upstream contributing basin, the land that drains toward your point, not the point itself. This matters because a slot can flood under a clear sky from rain falling miles upstream. Aggregating probability over the drainage area is what catches that.</p>
      <ul class="about-list">
        <li>Your raw coordinate is snapped onto the mapped stream network with a raindrop trace. It follows the terrain downhill along a flow-direction grid until it reaches a stream, which is a hydrologically correct snap rather than a blind nudge.</li>
        <li>From that on-network pour point, the exact upstream contributing basin is delineated by splitting the catchment over the national NHD stream network. This is the precise drainage area above your point, not a coarse approximation.</li>
        <li>If a point will not snap, the system falls back to a deterministic alternative: resolve the containing USGS HUC-12 sub-watershed and collect every HUC-12 that drains into it from the Watershed Boundary Dataset. This is snap-free and reproducible, but coarser, since it counts whole sub-watersheds.</li>
        <li>Areas are measured on an equal-area projection, and each delineation is cached, so the same point yields the same basin and is reused across briefings.</li>
      </ul>
      <p class="about-p">The map's shaded basin is this delineated domain, labeled approximate. Surface delineation is a defensible proxy for canyoneering, but for caves the true karst recharge area can cross surface divides through underground conduits, so it may differ from the surface watershed. The briefing states this caveat for caving locations.</p>
    </section>

    <section class="card">
      <div class="eyebrow">Derived hazard thresholds</div>
      <p class="about-p">Every cut point is externalized, versioned configuration, never hard-coded. The engine loads the thresholds at runtime, so tuning one is a configuration edit with provenance, not a code change. The values below are the accepted initial configuration, refined through field testing. Cut points that ride on an established system (NWS warnings and watches, Heat Index categories, the SPC outlook) are standard. The numeric probability and temperature break points are UpstreamWX proposals.</p>
      <div class="about-hazards">${thresholds}</div>
      <p class="about-haz__note about-wrap" style="margin-top:var(--space-3)">Loaded threshold matrix version: <span class="mono">${esc(b.threshold_version)}</span></p>
    </section>

    <div class="disclaimer">Reference only. Not a forecast, not a decision. These thresholds describe how the briefing reasons about hazards. They do not replace official NWS products or your own judgment in the field.</div>`;

  linkifyAcronyms(document.getElementById("view-about"));
  const back = document.getElementById("close-about");
  if (back) back.addEventListener("click", closeAbout);
}

function openAbout() {
  hideGlossaryPopover();
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-about"));
  document.querySelector("main").scrollTo({ top: 0 });
}

function closeAbout() {
  selectTab("resources");
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
  initGlossaryInteractions();
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
  renderAbout(b);
  renderStatus(b);
  selectTab(state.tab);

  window.addEventListener("online", () => renderStatus(b));
  window.addEventListener("offline", () => { state.fromCache = true; renderStatus(b); });
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}

main();
