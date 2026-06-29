/*
 * UpstreamWX PWA — app shell + view renderers.
 * Behavior/IA follow PRD §6.8 + Appendix D; visuals follow STYLE_GUIDE.md.
 * Data mirrors BriefingResponse/BriefingResult; at M0.4 the fetch below becomes
 * POST /v1/briefing and nothing else changes.
 */

import { icon, HAZARD_LABELS, PHASE_LABELS } from "./icons.js";

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "map", label: "Map" },
  { id: "hazards", label: "Hazards" },
  { id: "briefing", label: "Briefing" },
  { id: "forecast", label: "Forecast" },
  { id: "resources", label: "Resources" },
];

const ACK_KEY = "uwx.ack.v1"; // first-run acknowledgment (FR-31)
const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// 24-hour clock with a colon: "1300" -> "13:00", "1300–1400" -> "13:00–14:00".
// Only rewrites bare HHMM tokens, so years/HUC codes are left alone.
const fmtClock = (s) => String(s).replace(/\b(\d{2})(\d{2})\b/g, "$1:$2");

let state = { briefing: null, fromCache: false, tab: "overview", mapInitialized: false, appVersion: null };

/* ── Mission spec (the POST /v1/briefing request) ──────────────────────
 * The PWA holds a mission spec, persists it locally, and re-fetches a live
 * briefing whenever the point or mission details change (M0.4). The spec
 * mirrors the API's MissionSpec. */
const API_BRIEFING = "/v1/briefing";
const API_FRAME = "/v1/briefing/frame";
const API_WATERSHED_WARM = "/v1/watershed/warm";
const MISSION_KEY = "uwx.mission.v1";
// Seed mission used on first run when nothing is saved (a real CONUS point).
// Radius of Concern (FR-3): discrete, non-linear slider stops in miles; the data
// model stores km. UI orange = the --sev-high token (frontend/styles/tokens.css).
const ROC_STOPS_MI = [10, 20, 50, 100, 200];
const ROC_DEFAULT_MI = 20;
const MI_TO_KM = 1.609344;
const UI_ORANGE = "#f0883e";

function nearestRocIndex(mi) {
  let best = 0;
  for (let i = 1; i < ROC_STOPS_MI.length; i++) {
    if (Math.abs(ROC_STOPS_MI[i] - mi) < Math.abs(ROC_STOPS_MI[best] - mi)) best = i;
  }
  return best;
}
// Miles for the slider, defaulting when a spec carries no RoC (back-compat).
function rocMiFromSpec(spec) {
  if (spec && Number.isFinite(spec.radius_km)) return spec.radius_km / MI_TO_KM;
  return ROC_DEFAULT_MI;
}

/* ── User preferences (modular, localStorage) ──────────────────────────
 * App-wide settings kept separate from the per-mission spec, so the settings
 * panel can grow new keys without touching the mission contract.
 * • laoc_radius_km: Lightning Area of Concern disk (PRD §16.1). Stored in km;
 *   slider in miles. UI yellow is distinct from orange RoC/phase sliders.
 * • approach_hrs / egress_hrs: phase-marker defaults sent to the API; the
 *   backend infers 1 h each when null, so the shipped defaults match exactly. */
const PREFS_KEY = "uwx.prefs.v1";
const LAOC_STOPS_MI = [10, 15, 25, 50, 100];
const LAOC_DEFAULT_MI = 15;
const UI_YELLOW = "#facc15";
const PHASE_STOPS_HR = [0.5, 1, 1.5, 2, 3];
const PHASE_DEFAULT_HR = 1;
const DEFAULT_PREFS = {
  laoc_radius_km: LAOC_DEFAULT_MI * MI_TO_KM,
  approach_hrs: PHASE_DEFAULT_HR,
  egress_hrs: PHASE_DEFAULT_HR,
};

function loadPrefs() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PREFS_KEY)); } catch (e) { /* private mode */ }
  return { ...DEFAULT_PREFS, ...(saved && typeof saved === "object" ? saved : {}) };
}
function savePrefs(prefs) {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(prefs)); } catch (e) { /* private mode */ }
}
function nearestLaocIndex(mi) {
  let best = 0;
  for (let i = 1; i < LAOC_STOPS_MI.length; i++) {
    if (Math.abs(LAOC_STOPS_MI[i] - mi) < Math.abs(LAOC_STOPS_MI[best] - mi)) best = i;
  }
  return best;
}
function laocMiFromPrefs(prefs) {
  if (prefs && Number.isFinite(prefs.laoc_radius_km)) return prefs.laoc_radius_km / MI_TO_KM;
  return LAOC_DEFAULT_MI;
}
function nearestPhaseIndex(hrs) {
  let best = 0;
  for (let i = 1; i < PHASE_STOPS_HR.length; i++) {
    if (Math.abs(PHASE_STOPS_HR[i] - hrs) < Math.abs(PHASE_STOPS_HR[best] - hrs)) best = i;
  }
  return best;
}
function phaseLabel(hrs) {
  return hrs < 1 ? `${Math.round(hrs * 60)} min` : `${hrs} hr`;
}

const DEFAULT_SPEC = {
  lat: 34.665, lon: -85.361667, activity: "cave",
  start: "2026-06-18T13:00", end: "2026-06-18T22:00",
  name: "Pettyjohn's Cave", slot: false, frame: null,
  radius_km: ROC_DEFAULT_MI * MI_TO_KM,
};
// Canonical default name shown italic/grey in the planner until the user types their own.
const DEFAULT_NAME = DEFAULT_SPEC.name;

function savedSpec() {
  try { return JSON.parse(localStorage.getItem(MISSION_KEY)); } catch (e) { return null; }
}
function persistSpec(spec) {
  try { localStorage.setItem(MISSION_KEY, JSON.stringify(spec)); } catch (e) { /* private mode */ }
}
// Build a request spec from a rendered briefing's mission block.
function specFromBriefing(b) {
  const m = b.mission;
  return {
    lat: m.lat, lon: m.lon, activity: m.activity,
    start: m.window_start, end: m.window_end,
    name: m.name, slot: m.is_slot, frame: null,
    radius_km: m.radius_km ?? null,
    tz_name: m.tz_name ?? null,
  };
}

// ── Tier display-label config ────────────────────────────────────────
// Maps backend API tier strings ("Minimal", "Elevated", "High", "Extreme") to
// user-facing display names.  Populated from data/display-config.json at startup;
// the identity mapping below is the safe fallback when that file is absent.
let TIER_LABELS = { Minimal: "Minimal", Elevated: "Elevated", High: "High", Extreme: "Extreme" };

function displayTier(t) {
  return TIER_LABELS[t] ?? t;
}

// Remaps the tier portion of a "Hazard — Tier" lead-label string.
function displayLeadLabel(s) {
  return s.replace(/— (.+)$/, (_, t) => `— ${displayTier(t)}`);
}

// Single-pass replacement of all configured label strings in backend-authored text
// (threshold logic, driver copy). Sorts longest key first so "Extreme Caution"
// matches before "Extreme", avoiding double-substitution.
function displayLogic(s) {
  const entries = Object.entries(TIER_LABELS)
    .filter(([k, v]) => k !== v)
    .sort(([a], [b]) => b.length - a.length);
  if (!entries.length) return s;
  const re = new RegExp(
    `\\b(${entries.map(([k]) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})\\b`,
    "g"
  );
  return s.replace(re, (m) => TIER_LABELS[m] ?? m);
}

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
  ["CAPE", "Convective Available Potential Energy", "A measure of atmospheric instability (J/kg). Higher CAPE means more energy available for storm development; used to modulate lightning confidence, not to set the tier directly."],
  ["P(tstm)", "Probability of Thunderstorms", "The SREF ensemble probability that at least one member produces a thunderstorm over the aggregation domain. The primary lightning tier driver beyond the same-day window."],
  ["P(precip)", "Probability of Precipitation", "The SREF ensemble probability of measurable precipitation over the upstream watershed. A primary input to the flash flood tier."],
  ["P(ltg)", "Probability of Lightning", "The HREF same-day probability of a lightning strike within the aggregation domain (~6–36 h window). Sharpens the lightning tier when HREF is in range."],
  ["RoC", "Radius of Concern", "The user-configured maximum distance from the expedition point used to clip the upstream watershed before SREF/HREF aggregation. Smaller values focus the weather domain on the immediate drainage area."],
  ["LAoC", "Lightning Area of Concern", "The disk around the expedition point over which SREF and HREF lightning probabilities are aggregated. Lightning uses a disk, not the upstream watershed, because it is a point and corridor hazard — not basin-routed."],
];

const GLOSSARY_MAP = new Map(GLOSSARY.map(([acr, term, def]) => [acr, { term, def }]));
// Single alternation, longest acronym first so "HUC-12" wins over "HUC".
// Per-alternative \b: terms ending in a non-word char (e.g. "P(tstm)") use (?!\w)
// instead of \b on the right — \b after ")" is never a word boundary.
const GLOSSARY_RE = new RegExp(
  "(" +
    GLOSSARY.map(([a]) => a)
      .sort((a, b) => b.length - a.length)
      .map((a) => {
        const escaped = a.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const post = /\w$/.test(a) ? "\\b" : "(?!\\w)";
        return "\\b" + escaped + post;
      })
      .join("|") +
    ")",
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
// Live path (M0.4): POST the mission spec to the API. On failure (offline, or a
// static-only deployment with no backend) fall back to the bundled sample so the PWA
// still renders. The render layer is identical either way — both shapes are the same
// structured contract.
// Demo mode: the static GitHub Pages build has no API behind it, so it renders the
// bundled sample briefing for UI review. The PRODUCTION app (served single-origin by
// the API) must NEVER fall back to sample data — a failed fetch surfaces the real
// error instead, so outages are visible rather than masked by a stale demo briefing.
// Force demo locally with ?demo for offline UI work.
const DEMO_MODE =
  /\.github\.io$/i.test(location.hostname) ||
  new URLSearchParams(location.search).has("demo");

// POST the mission spec and return the freshly generated briefing, or throw with a
// useful message. This is the live path; it never substitutes other data on failure.
async function postBriefing(spec) {
  // Fold app-wide prefs into the request without persisting them into the mission spec.
  // approach_end / egress_start (FR-9a) are computed from the user's phase-time pref and
  // sent explicitly so the backend uses the user's preference rather than the 1-hr default.
  const prefs = loadPrefs();
  const approachEnd = addHoursLocal(spec.start, prefs.approach_hrs ?? PHASE_DEFAULT_HR);
  const egressStart = addHoursLocal(spec.end, -(prefs.egress_hrs ?? PHASE_DEFAULT_HR));
  const body = {
    ...spec,
    approach_end: approachEnd,
    egress_start: egressStart,
    lightning_radius_km: prefs.laoc_radius_km,
  };
  const res = await fetch(API_BRIEFING, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `server returned ${res.status}`;
    try {
      const j = await res.clone().json();
      if (j && j.detail) detail = `${res.status}: ${typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)}`;
    } catch (_) { /* non-JSON body (e.g. proxy 504) — keep the status */ }
    throw new Error(detail);
  }
  state.fromCache = !navigator.onLine;
  return await res.json();
}

// Fire-and-forget watershed cache warm: kick off the slow upstream delineation the moment
// the planner reports a new point, so it's ready (or in flight) by the time the user
// generates the briefing. Best-effort — a failed or disabled warm just means the briefing
// pays the cold cost as before, so errors are swallowed and never surface in the UI.
function warmWatershed(lat, lon) {
  if (DEMO_MODE) return; // no live API behind a static build
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return;
  fetch(API_WATERSHED_WARM, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ lat, lon }),
    keepalive: true,
  }).catch(() => {});
}

let _warmTimer = null;
// Debounce so dragging the marker or a search→place→reseed burst fires one warm, not many.
function warmWatershedDebounced(lat, lon) {
  if (_warmTimer) clearTimeout(_warmTimer);
  _warmTimer = setTimeout(() => {
    _warmTimer = null;
    warmWatershed(lat, lon);
  }, 350);
}

// Stream the Haiku plain-language narrative into the Risk Discussion card.
// Called immediately after renderAll(); runs in the background without blocking the UI.
// If the server returns 204 (no API key) the card is hidden; on any error it is removed.
function streamSummary(spec) {
  if (DEMO_MODE) return; // sample data already has b.summary; nothing to stream
  const card = document.getElementById("risk-discussion");
  const textEl = document.getElementById("risk-discussion-text");
  if (!card || !textEl) return;

  const prefs = loadPrefs();
  const body = {
    ...spec,
    approach_end: addHoursLocal(spec.start, prefs.approach_hrs ?? PHASE_DEFAULT_HR),
    egress_start: addHoursLocal(spec.end, -(prefs.egress_hrs ?? PHASE_DEFAULT_HR)),
    lightning_radius_km: prefs.laoc_radius_km,
  };

  fetch(API_FRAME, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  }).then(async (res) => {
    if (res.status === 204 || !res.ok || !res.body) {
      card.hidden = true;
      return;
    }
    textEl.textContent = ""; // clear loading placeholder
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let text = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE events are separated by double newlines.
      const parts = buf.split("\n\n");
      buf = parts.pop(); // keep trailing incomplete chunk
      for (const part of parts) {
        if (!part.startsWith("data: ")) continue;
        let evt;
        try { evt = JSON.parse(part.slice(6)); } catch (_) { continue; }
        if (evt.done) {
          // Streaming complete — append the framing attribution.
          const note = document.createElement("span");
          note.className = "framed-by";
          note.textContent =
            "Summary wording only — all posture and severity values are deterministic engine output, not model-derived.";
          textEl.after(note);
          return;
        }
        if (evt.text) {
          text += evt.text;
          textEl.textContent = text;
        }
      }
    }
  }).catch(() => {
    if (card) card.hidden = true;
  });
}

// Load the bundled sample (demo builds and ?demo only).
async function loadSample() {
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

// Initial load. Demo builds render the sample; production fetches a live briefing and
// surfaces the error on failure (no sample fallback) so the real state is visible.
async function loadBriefing(spec) {
  if (DEMO_MODE) return await loadSample();
  return await postBriefing(spec || DEFAULT_SPEC);
}

// Re-fetch and re-render for an updated mission spec (point move / mission edit).
// A failed live fetch must NOT silently swap in other data — that reads as "the edit
// did nothing". Surface the failure and keep the current briefing on screen.
async function refresh(spec) {
  persistSpec(spec);
  const status = document.getElementById("status");
  if (DEMO_MODE) {
    if (status) {
      status.innerHTML =
        `<span class="status-line__currency">Demo preview — connect the API to generate a live briefing for your edits.</span>`;
    }
    return;
  }
  if (status) status.innerHTML = `<span class="status-line__currency">Updating briefing…</span>`;
  let b;
  try {
    b = await postBriefing(spec);
  } catch (e) {
    if (status) {
      status.innerHTML =
        `<span class="status-line__currency">Could not update briefing (${esc(String(e.message || e))}). ` +
        `Showing the previous briefing — try again in a moment.</span>`;
    }
    return;
  }
  renderAll(b);
  streamSummary(spec);
}

/* ── Small render helpers ──────────────────────────────────────────── */
function postureChip(label, sevClass, big = false) {
  return `<span class="posture-chip ${sevClass} ${big ? "is-lg" : ""}">${esc(label)}</span>`;
}

function confidenceTag(level, big = false) {
  // SVG signal-bar style: three bars whose tops share a single diagonal line,
  // so bar i's right top and bar (i+1)'s left top are continuous (no jump).
  // Low = 1 filled, moderate = 2, high = all 3 (FR-36).
  const k = String(level).toLowerCase();
  const activeCount = k === "low" ? 1 : k === "moderate" ? 2 : 3;
  const vW = 100, vH = 18, gap = 4;
  const bW = (vW - 2 * gap) / 3;
  // Diagonal: y = y0 × (vW − x) / vW, passing from y0 at x=0 to 0 at x=vW.
  // y0=14 gives bar 0 a 4 px minimum height; bar 2's right edge reaches full vH.
  const y0 = 14;
  const diagY = (x) => y0 * (vW - x) / vW;
  const bars = [0, 1, 2].map((i) => {
    const xL = i * (bW + gap), xR = xL + bW;
    const yTL = diagY(xL), yTR = Math.max(0, diagY(xR));
    const fill = i < activeCount ? "var(--sev-high)" : "var(--color-surface-3)";
    return `<polygon points="${xL.toFixed(1)},${vH} ${xR.toFixed(1)},${vH} ${xR.toFixed(1)},${yTR.toFixed(1)} ${xL.toFixed(1)},${yTL.toFixed(1)}" fill="${fill}"/>`;
  }).join("");
  return `<div class="confidence ${big ? "is-lg" : ""}" title="${esc(level)} confidence">
    <svg class="confidence__bars" viewBox="0 0 ${vW} ${vH}" aria-hidden="true">${bars}</svg>
    <div class="confidence__label">${esc(level)} confidence</div>
  </div>`;
}

/* ── 7.1/7.3 Header + mission card ─────────────────────────────────── */
function renderHeader(b) {
  const m = b.mission;
  const actSrc = m.activity === "cave" ? "icons/cave.png" : "icons/canyon.png";
  document.getElementById("header").innerHTML = `
    <div class="brand">
      <img src="icons/logo.png" class="brand__logo" alt="UpstreamWX Weather Briefing" />
    </div>
    <div class="app-header__spacer"></div>
    <span class="activity-pill"><img src="${actSrc}" class="activity-pill__icon" alt="" />${esc(m.activity)}</span>
    <button class="header-reload" id="header-reload" type="button" aria-label="Reload app" title="Reload app">
      ${icon("reload", "header-reload__icon")}
    </button>
  `;
  document.getElementById("header-reload").addEventListener("click", () => window.location.reload());
}

function missionCard(b) {
  const m = b.mission;
  const start = new Date(m.window_start), end = new Date(m.window_end);
  // Render the window in the mission's own zone (tz_name), not the viewer's browser
  // zone, so a trip planned from another timezone still reads as local wall-clock (FR-9).
  const tz = m.tz_name || undefined;
  const fmtD = start.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: tz });
  const fmtT = (d) => d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz });
  return `
    <section class="card mission-card">
      <div class="mission-card__main">
        <div class="eyebrow">Expedition</div>
        <h1 class="mission-card__title">${esc(m.name)}
          <button class="mission-card__edit" aria-label="Edit expedition">${icon("edit", "")}</button>
        </h1>
        <div class="mission-card__meta">${fmtD} · ${fmtT(start)}–${fmtT(end)} ${esc(m.timezone)}</div>
        <div class="mission-card__meta"><span class="mono">${m.lat.toFixed(4)}, ${m.lon.toFixed(4)}</span></div>
        ${b.watershed ? `<div class="mission-card__meta">Watershed area <span class="mono">${b.watershed.area_sq_mi.toFixed(1)} mi²</span></div>` : ""}
      </div>
      <div class="mission-card__posture">
        <div class="eyebrow">Overall posture</div>
        ${postureChip(displayTier(b.overall_posture), overallSevClass(b), true)}
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
    requestAnimationFrame(() => initMainMap(state.briefing));
  }
  if (id === "forecast" && _fcSync) requestAnimationFrame(_fcSync);
}

/* ── 7.4 Overview ──────────────────────────────────────────────────── */
function renderOverview(b) {
  const hazards = b.bluf
    .map((h) => {
      const win = h.window ? `<span class="hazard-line__window">${esc(fmtClock(h.window))}</span>` : "";
      return `<button class="hazard-line" data-goto="hazards" data-hazard="${esc(h.hazard)}">
        ${icon(h.hazard, "hazard-line__icon")}
        <div class="hazard-line__body">
          <div class="hazard-line__name">${HAZARD_LABELS[h.hazard]}</div>${win}
        </div>
        <div class="hazard-line__right">${postureChip(displayTier(h.label), h.severity_class)}${confidenceTag(h.confidence)}</div>
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
        <div class="phase-seg__lead">${esc(displayLeadLabel(p.lead_label))}</div>
        <div class="phase-seg__hazards">${esc(p.applicable)}</div>
        ${p.note ? `<div class="phase-seg__note">${esc(p.note)}</div>` : ""}
      </div>`
    )
    .join("");

  document.getElementById("view-overview").innerHTML = `
    ${missionCard(b)}
    <details class="hazard-detail" id="risk-discussion">
      <summary class="hazard-detail__summary">
        <span class="hazard-detail__name">Risk Discussion</span>
        ${icon("chevron", "hazard-detail__chev")}
      </summary>
      <div class="hazard-detail__body">
        <p class="summary" id="risk-discussion-text">
          ${b.summary ? esc(b.summary) : '<span class="risk-discussion-loading">Loading…</span>'}
        </p>
        ${b.framed ? '<span class="framed-by">Summary wording only — all posture and severity values are deterministic engine output, not model-derived.</span>' : ""}
      </div>
    </details>
    <section class="card"><h2 class="section-title" style="margin-bottom:var(--space-2)">Hazards</h2>
      <div class="hazard-list">${hazards}</div>
    </section>
    <div class="metric-grid">${metrics}</div>
    <section class="card"><h2 class="section-title" style="margin-bottom:var(--space-3)">Expedition Phases</h2>
      <div class="phase-strip">${phases}</div>
      ${b.mission.phases_inferred ? '<div class="phase-seg__note" style="margin-top:var(--space-3)">Phases inferred from the overall window: approach = first hour, egress = last hour.</div>' : ""}
    </section>
    <div class="disclaimer">Planning reference only — not a forecast, not a decision. Conditions change fast and models can be wrong. Verify against the official NWS sources linked in Resources, and let what you see in the field overrule this briefing.</div>`;

  document.querySelectorAll('[data-goto="hazards"]').forEach((el) =>
    el.addEventListener("click", () => {
      const hazard = el.dataset.hazard;
      selectTab("hazards");
      if (hazard) {
        // selectTab scrolls to top; wait two frames for the view to be painted,
        // then open the matching card and scroll it into view.
        requestAnimationFrame(() => requestAnimationFrame(() => {
          const target = document.querySelector(`.hazard-detail[data-hazard="${hazard}"]`);
          if (target) {
            target.open = true;
            target.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }));
      }
    })
  );
  const edit = document.querySelector(".mission-card__edit");
  if (edit) edit.addEventListener("click", () => openMissionPlanner(specFromBriefing(b)));
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
    ${renderRiskInputs(b.risk_inputs)}`;
  flushChartInits();
  initForecastScroll();
  linkifyAcronyms(document.getElementById("view-forecast"));
}

// Risk analysis inputs section — shows the scalar engine inputs (SREF/HREF probs,
// physical params, NWS alerts) so users can verify what drove each hazard tier (FR-20).
function renderRiskInputs(ri) {
  if (!ri || !Object.keys(ri).length) return "";

  function riCard(label, iconName, value, unit, sub) {
    return `<div class="metric-card">
      <div class="metric-card__label">${icon(iconName, "metric-card__icon")}<span class="eyebrow">${esc(label)}</span></div>
      <div class="metric-card__value">${esc(String(value))}<span style="font-size:14px;color:var(--color-text-muted)">${esc(unit)}</span></div>
      <div class="metric-card__sub">${esc(sub)}</div>
    </div>`;
  }

  const cards = [];
  if (ri.sref_p_precip != null)
    cards.push(riCard("SREF P(precip)", "flash_flood", ri.sref_p_precip, "%", "Flood input"));
  if (ri.sref_p_tstm != null)
    cards.push(riCard("SREF P(tstm)", "lightning", ri.sref_p_tstm, "%", "T-storm input"));
  if (ri.href_in_range && ri.href_p_precip != null)
    cards.push(riCard("HREF P(QPF)", "flash_flood", ri.href_p_precip, "%", "Same-day flood"));
  if (ri.href_in_range && ri.href_p_lightning != null)
    cards.push(riCard("HREF P(ltg)", "lightning", ri.href_p_lightning, "%", "Same-day ltg"));
  if (ri.cape_jkg != null)
    cards.push(riCard("CAPE", "lightning", ri.cape_jkg, " J/kg", "Instability"));
  if (ri.convective_rate_in_per_hr != null)
    cards.push(riCard("Conv. rate", "flash_flood", ri.convective_rate_in_per_hr, " in/hr", "Peak rate"));
  if (!cards.length) return "";

  const badges = [];
  if (ri.flash_flood_warning) badges.push(`<span class="ri-badge ri-badge--warn">Flash flood warning</span>`);
  if (ri.flash_flood_watch) badges.push(`<span class="ri-badge ri-badge--watch">Flash flood watch</span>`);
  if (ri.flood_watch) badges.push(`<span class="ri-badge ri-badge--watch">Flood watch</span>`);
  if (ri.thunderstorm_warning) badges.push(`<span class="ri-badge ri-badge--warn">Thunderstorm warning</span>`);
  if (ri.spc_category) badges.push(`<span class="ri-badge">SPC ${esc(ri.spc_category)}</span>`);
  if (ri.href_in_range && ri.href_cycle) badges.push(`<span class="ri-badge">HREF ${esc(ri.href_cycle)}</span>`);

  return `<section class="card">
    <h2 class="section-title" style="margin-bottom:var(--space-2)">Risk analysis inputs</h2>
    <div class="metric-grid">${cards.join("")}</div>
    ${badges.length ? `<div class="ri-badges">${badges.join("")}</div>` : ""}
    <div class="disclaimer">Raw inputs to the deterministic engine — SREF/HREF probabilities aggregated over the upstream watershed. The Hazards view shows how each threshold fired.</div>
  </section>`;
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

  // Show at most ~5 evenly-spaced tick labels so they don't overlap on mobile.
  // step=1 for ≤5 labels, step=2 for 6-10, step=3 for 11-15, etc.
  const tickStep = Math.max(1, Math.ceil(labels.length / 5));
  const ticks = labels.map((l, i) => {
    if (i % tickStep !== 0) return "";
    const anchor = i === 0 ? "start" : "middle";
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

// Render only the threshold-logic entry for the current posture.  If the logic
// string has semicolon-separated "Tier = condition" entries (flash_flood style),
// extract the matching one; for Minimal (not listed), frame it as "below the
// lowest defined threshold".  Single-block logic (heat, lightning) is shown as-is.
function thresholdLogicHtml(logic, currentLabel = "") {
  const normalized = displayLogic(String(logic))
    .replace(/\s*\((?:Appendix B|§)[^)]*\)/g, "")
    .replace(/\s*\(FR-\d+[a-z]?\)/g, "");

  const entries = normalized.split(/;\s*/).map((s) => s.trim()).filter(Boolean);

  if (entries.length <= 1) {
    // Non-tiered copy (heat, lightning, cold_wet full block) — show as-is.
    return `<ul class="logic-list"><li>${esc(normalized)}</li></ul>`;
  }

  if (currentLabel) {
    // Try matching "Label = …" or "Label: …" at the start of any entry.
    const pat = new RegExp(`^${currentLabel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*[=:]`, "i");
    const hit = entries.find((e) => pat.test(e));
    if (hit) return `<ul class="logic-list"><li>${esc(hit)}</li></ul>`;

    // Label not found (Minimal tier absent from the logic) — frame as "below
    // the lowest defined threshold", which is the last semicolon entry.
    const lowestDefined = entries[entries.length - 1];
    if (lowestDefined) {
      return `<ul class="logic-list"><li>Below: ${esc(lowestDefined)}</li></ul>`;
    }
  }

  return `<ul class="logic-list">${entries.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>`;
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
    ${["Minimal", "Elevated", "High", "Extreme"]
      .map((s) => `<span class="legend__item"><span class="legend__swatch bar-${s.toLowerCase()}"></span>${esc(displayTier(s))}</span>`)
      .join("")}
    <span class="legend__item"><span class="legend__swatch" style="background:var(--color-text-secondary)"></span>Solid = higher confidence</span>
    <span class="legend__item"><span class="legend__swatch legend__swatch--conf-low" style="background:var(--color-text-secondary)"></span>Striped = lower confidence</span>
  </div>`;

  const details = b.hazard_detail
    .map(
      (h) => `<details class="hazard-detail" data-hazard="${esc(h.hazard)}">
      <summary class="hazard-detail__summary">
        ${icon(h.hazard, "icon")}
        <span class="hazard-detail__name">${HAZARD_LABELS[h.hazard]}</span>
        ${postureChip(displayTier(h.label), h.severity_class)}
        ${icon("chevron", "hazard-detail__chev")}
      </summary>
      <div class="hazard-detail__body">
        <div class="hazard-detail__confidence">${confidenceTag(h.confidence)}</div>
        <h4>Key drivers</h4><ul>${h.drivers.map((d) => `<li>${esc(d)}</li>`).join("")}</ul>
        <h4>Threshold logic</h4>
        ${thresholdLogicHtml(h.logic, h.label)}
        ${h.assumptions.map((a) => `<div class="assumption">${icon("alert", "")}<span>${esc(a)}</span></div>`).join("")}
      </div>
    </details>`
    )
    .join("");

  document.getElementById("view-hazards").innerHTML = `
    <section class="card">
      <h2 class="section-title" style="margin-bottom:var(--space-3)">Hazards by Expedition Phase</h2>
      <div class="timeline">
        <div class="timeline__phases">${phaseHead}</div>
        ${rows}
      </div>
      ${legend}
    </section>
    <div style="display:flex;flex-direction:column;gap:var(--space-2)">${details}</div>
    <div class="disclaimer">Severity on the UpstreamWX ladder (${["Minimal", "Elevated", "High", "Extreme"].map(displayTier).join(" / ")}). Confidence shown as hatching and an explicit label; bar length distinguishes persistent hazards from time-windowed hazards (display only).</div>`;
  linkifyAcronyms(document.getElementById("view-hazards"));
}

/* ── 7.11 Map ──────────────────────────────────────────────────────── */
function renderMap(b) {
  // A re-render recreates the map container; drop any prior MapLibre instance so it
  // rebuilds against the new briefing (point / watershed may have changed).
  if (_mainMap) { _mainMap.remove(); _mainMap = null; }
  state.mapInitialized = false;
  const hasExcluded = !!b.watershed?.excluded_geometry;
  const hasRoc = !!(b.roc?.geometry || (b.roc?.center && b.roc?.radius_km));
  const hasLaoc = !!(b.laoc?.geometry || (b.laoc?.center && b.laoc?.radius_km));
  document.getElementById("view-map").innerHTML = `
    <div id="leaflet-map" aria-label="Expedition area topographic map"></div>
    <div class="map-legend">
      <span class="map-legend__item"><span class="map-legend__swatch map-legend__swatch--watershed"></span>Watershed</span>
      ${hasExcluded ? '<span class="map-legend__item"><span class="map-legend__swatch map-legend__swatch--excluded"></span>Outside RoC</span>' : ""}
      ${hasRoc ? '<span class="map-legend__item"><span class="map-legend__roc-line"></span>Radius of Concern</span>' : ""}
      ${hasLaoc ? '<span class="map-legend__item"><span class="map-legend__laoc-line"></span>Lightning area of concern</span>' : ""}
    </div>
    <div class="disclaimer">Planning map. The shaded basin is the approximate upstream watershed feeding the expedition point. Tap either for details.</div>`;
}

let _mainMap = null;
let _poiMarker = null;
let _moveMode = false;

function poiPopupHtml(m) {
  return `<div class="map-pop">
    <div class="map-pop__title">${esc(m.name)}</div>
    <div class="map-pop__row"><span class="mono">${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}</span></div>
    <button class="map-pop__btn" data-move-point>Move point</button>
  </div>`;
}

// ── Base map styles ────────────────────────────────────────────────────
// All sources are free and key-free. "Light" is OpenFreeMap's minimal light vector
// style; "Aerial" is Esri World Imagery raster; "WX Topo" is built below.
const STYLE_LIGHT = "https://tiles.openfreemap.org/styles/positron";  // OpenFreeMap minimal light
const STYLE_FIORD = "https://tiles.openfreemap.org/styles/fiord";    // OpenFreeMap fiord (muted dark)

// Esri World Imagery (satellite) — kept as the imagery base. maxzoom = native tile
// ceiling; MapLibre auto-upscales above this without blank tiles.
const RASTER_AERIAL = {
  type: "raster",
  tiles: ["https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"],
  tileSize: 256, maxzoom: 19,
  attribution: "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics",
};

// Minimal MapLibre style document wrapping a single raster source (used for Aerial).
function rasterBaseStyle(src) {
  return { version: 8, sources: { base: src }, layers: [{ id: "base", type: "raster", source: "base" }] };
}

// ── WX Topo: key-free dark/light topographic base ──────────────────────
// Hill shading comes from the AWS Terrarium DEM (decoded natively by MapLibre as a
// raster-dem source); contour lines are generated client-side by maplibre-contour from
// the same DEM; water, roads, and labels are OpenFreeMap's OpenMapTiles vector tiles.
// The builder is theme-parameterized so the same recipe yields the dark default and a
// future light variant — call buildWxTopoStyle({ dark: false }) for light.
const OFM_VECTOR_URL = "https://tiles.openfreemap.org/planet";              // OpenMapTiles vector tiles
const OFM_GLYPHS     = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";
const TERRARIUM_URL  = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
const OFM_ATTRIB     = '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> · <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">© OpenMapTiles</a> · <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">© OpenStreetMap</a>';
const TERRAIN_ATTRIB = '<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md" target="_blank" rel="noopener">Terrain: Mapzen / AWS</a>';

const WX_TOPO_DARK = {
  bg: "#0a0e14", land: "#0f1620", water: "#15384f",
  shadow: "#04070b", highlight: "#3a4a5a", accent: "#10204a",
  contour: "#5d6b4e", contourIndex: "#8d9a6b", contourLabel: "#b3c08f",
  road: "#3a4654", roadMinor: "#28313d", label: "#cdd6e0", peak: "#d9c89a",
};
const WX_TOPO_LIGHT = {
  bg: "#f3efe6", land: "#e8e3d5", water: "#a9cce0",
  shadow: "#9a978a", highlight: "#ffffff", accent: "#cfe6f2",
  contour: "#b6a079", contourIndex: "#8c7548", contourLabel: "#6a5836",
  road: "#cdbfa9", roadMinor: "#ded3bf", label: "#33302a", peak: "#7a5a30",
};

// Lazily create the shared DEM source for hillshade + contours (one custom protocol per
// maplibregl instance). Returns null if maplibre-contour failed to load — callers then
// fall back to native hillshade with no contour lines.
let _demSource = null;
function getDemSource() {
  if (_demSource) return _demSource;
  if (!window.mlcontour || !window.maplibregl) return null;
  _demSource = new mlcontour.DemSource({
    url: TERRARIUM_URL, encoding: "terrarium", maxzoom: 13, worker: true,
  });
  _demSource.setupMaplibre(maplibregl);
  return _demSource;
}

// Build a complete MapLibre style for the WX Topo base. dark selects the palette.
function buildWxTopoStyle({ dark = true } = {}) {
  const P = dark ? WX_TOPO_DARK : WX_TOPO_LIGHT;
  const dem = getDemSource();

  const sources = {
    openmaptiles: { type: "vector", url: OFM_VECTOR_URL, attribution: OFM_ATTRIB },
    // Shared DEM through maplibre-contour's protocol when available (one fetch feeds both
    // hillshade and contours); otherwise native terrarium tiles for hillshade only.
    dem: dem
      ? { type: "raster-dem", encoding: "terrarium", tileSize: 514, maxzoom: 13,
          tiles: [dem.sharedDemProtocolUrl], attribution: TERRAIN_ATTRIB }
      : { type: "raster-dem", encoding: "terrarium", tileSize: 256, maxzoom: 15,
          tiles: [TERRARIUM_URL], attribution: TERRAIN_ATTRIB },
  };
  if (dem) {
    sources.contours = {
      type: "vector", maxzoom: 15,
      tiles: [dem.contourProtocolUrl({
        multiplier: 3.28084,        // metres → feet (US contour convention)
        overzoom: 1,
        thresholds: {               // per-zoom [minor interval, index interval] in feet
          10: [1000, 5000], 11: [500, 2500], 12: [400, 2000],
          13: [200, 1000], 14: [100, 500], 15: [40, 200],
        },
        elevationKey: "ele", levelKey: "level", contourLayer: "contours",
      })],
    };
  }

  const layers = [
    { id: "bg", type: "background", paint: { "background-color": P.bg } },
    { id: "landcover", type: "fill", source: "openmaptiles", "source-layer": "landcover",
      paint: { "fill-color": P.land, "fill-opacity": 0.5 } },
    { id: "hillshade", type: "hillshade", source: "dem",
      paint: {
        "hillshade-exaggeration": 0.45,
        "hillshade-shadow-color": P.shadow,
        "hillshade-highlight-color": P.highlight,
        "hillshade-accent-color": P.accent,
        "hillshade-illumination-direction": 315,
      } },
    { id: "water", type: "fill", source: "openmaptiles", "source-layer": "water",
      paint: { "fill-color": P.water, "fill-opacity": 0.85 } },
    { id: "waterway", type: "line", source: "openmaptiles", "source-layer": "waterway",
      paint: { "line-color": P.water,
        "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.4, 14, 1.4] } },
  ];

  if (dem) {
    layers.push(
      { id: "contour", type: "line", source: "contours", "source-layer": "contours",
        filter: ["!=", ["get", "level"], 1],
        paint: { "line-color": P.contour, "line-width": 0.5, "line-opacity": 0.5 } },
      { id: "contour-index", type: "line", source: "contours", "source-layer": "contours",
        filter: ["==", ["get", "level"], 1],
        paint: { "line-color": P.contourIndex, "line-width": 1, "line-opacity": 0.75 } },
    );
  }

  layers.push(
    { id: "road-minor", type: "line", source: "openmaptiles", "source-layer": "transportation",
      filter: ["!", ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]]],
      minzoom: 11,
      paint: { "line-color": P.roadMinor,
        "line-width": ["interpolate", ["linear"], ["zoom"], 11, 0.4, 16, 2] } },
    { id: "road-major", type: "line", source: "openmaptiles", "source-layer": "transportation",
      filter: ["in", ["get", "class"], ["literal", ["motorway", "trunk", "primary"]]],
      paint: { "line-color": P.road,
        "line-width": ["interpolate", ["linear"], ["zoom"], 7, 0.6, 16, 3] } },
  );

  if (dem) {
    layers.push({
      id: "contour-label", type: "symbol", source: "contours", "source-layer": "contours",
      filter: ["==", ["get", "level"], 1], minzoom: 12,
      layout: {
        "symbol-placement": "line", "symbol-spacing": 320,
        "text-field": ["concat", ["number-format", ["get", "ele"], { "max-fraction-digits": 0 }], " ft"],
        "text-font": ["Noto Sans Regular"], "text-size": 10, "text-rotation-alignment": "map",
      },
      paint: { "text-color": P.contourLabel, "text-halo-color": P.bg, "text-halo-width": 1.4 },
    });
  }

  // Mountain peaks: name + spot elevation (feet), the signature topo annotation.
  // Placed before city labels so summits win collisions on the topographic base.
  layers.push({
    id: "mountain-peak", type: "symbol", source: "openmaptiles", "source-layer": "mountain_peak",
    filter: ["==", ["geometry-type"], "Point"], minzoom: 9,
    layout: {
      "symbol-sort-key": ["coalesce", ["get", "rank"], 99],
      "text-field": ["concat",
        ["coalesce", ["get", "name:en"], ["get", "name"], ""],
        ["case", ["has", "ele_ft"],
          ["concat", "\n", ["number-format", ["get", "ele_ft"], { "max-fraction-digits": 0 }], " ft"],
          ""],
      ],
      "text-font": ["Noto Sans Regular"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 9, 10, 14, 13],
      "text-anchor": "top", "text-offset": [0, 0.4], "text-max-width": 8,
    },
    paint: { "text-color": P.peak, "text-halo-color": P.bg, "text-halo-width": 1.4 },
  });

  layers.push({
    id: "place", type: "symbol", source: "openmaptiles", "source-layer": "place",
    filter: ["match", ["get", "class"], ["city", "town", "village"], true, false],
    layout: {
      "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]],
      "text-font": ["Noto Sans Regular"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 6, 11, 12, 15],
    },
    paint: { "text-color": P.label, "text-halo-color": P.bg, "text-halo-width": 1.4 },
  });

  return { version: 8, glyphs: OFM_GLYPHS, sources, layers };
}

// Layer-switcher entries shared by the briefing and planner maps.
const BASE_LAYERS = [
  { label: "WX Topo", key: "wxtopo" },
  { label: "Simple", key: "fiord" },
  { label: "Streets", key: "light" },
  { label: "Aerial", key: "aerial" },
];

// Resolve a switcher key to a MapLibre style (URL, raster style object, or built style).
function baseStyleForKey(key) {
  if (key === "fiord") return STYLE_FIORD;
  if (key === "light") return STYLE_LIGHT;
  if (key === "aerial") return rasterBaseStyle(RASTER_AERIAL);
  return buildWxTopoStyle({ dark: true });   // "wxtopo" — default base
}

// Approximate circle as a GeoJSON Polygon (equirectangular; ≤1 % error at CONUS latitudes
// for radii up to 200 mi). Used for the live RoC preview on the planner.
function circlePolygon(lng, lat, radiusKm, steps = 64) {
  const pts = [];
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * 2 * Math.PI;
    pts.push([
      lng + (radiusKm / (111.32 * Math.cos(lat * Math.PI / 180))) * Math.sin(a),
      lat + (radiusKm / 110.574) * Math.cos(a),
    ]);
  }
  return { type: "Feature", geometry: { type: "Polygon", coordinates: [pts] } };
}

// Compute [[w,s],[e,n]] bounding box from any GeoJSON geometry for map.fitBounds().
function geoBBox(geom) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  const v = ([lng, lat]) => {
    w = Math.min(w, lng); e = Math.max(e, lng); s = Math.min(s, lat); n = Math.max(n, lat);
  };
  function walk(g) {
    if (!g) return;
    if (g.type === "Feature") { walk(g.geometry); return; }
    if (g.type === "FeatureCollection") { g.features.forEach(walk); return; }
    if (g.type === "GeometryCollection") { g.geometries.forEach(walk); return; }
    const c = g.coordinates;
    if (g.type === "Point") v(c);
    else if (g.type === "LineString" || g.type === "MultiPoint") c.forEach(v);
    else if (g.type === "Polygon" || g.type === "MultiLineString") c.forEach(r => r.forEach(v));
    else if (g.type === "MultiPolygon") c.forEach(p => p.forEach(r => r.forEach(v)));
  }
  walk(geom);
  return [[w, s], [e, n]];
}

// Register an 8×8 diagonal-hatch pattern image once per map (idempotent). Mirrors the
// --confidence-hatch visual token; used for the excluded-watershed fill.
function addHatchImage(map) {
  if (map.hasImage("roc-hatch")) return;
  const sz = 8;
  const canvas = document.createElement("canvas");
  canvas.width = sz; canvas.height = sz;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "rgba(56,189,248,0.05)";
  ctx.fillRect(0, 0, sz, sz);
  ctx.strokeStyle = "rgba(125,211,252,0.55)";
  ctx.lineWidth = 1.1;
  ctx.beginPath(); ctx.moveTo(0, sz); ctx.lineTo(sz, 0); ctx.stroke();
  const id = ctx.getImageData(0, 0, sz, sz);
  map.addImage("roc-hatch", { width: sz, height: sz, data: new Uint8Array(id.data) });
}

// Minimal custom MapLibre layer-switcher control: stacked text buttons, one active at a time.
function makeLayerSwitcherControl(layers, onSwitch) {
  let _el = null;
  return {
    onAdd() {
      _el = document.createElement("div");
      _el.className = "maplibregl-ctrl maplibregl-ctrl-group map-layer-switcher";
      layers.forEach(({ label, key }, i) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.textContent = label;
        btn.dataset.key = key;
        if (i === 0) btn.classList.add("active");
        btn.addEventListener("click", () => {
          _el.querySelectorAll("button").forEach(b => b.classList.remove("active"));
          btn.classList.add("active");
          onSwitch(key);
        });
        _el.appendChild(btn);
      });
      return _el;
    },
    onRemove() { _el = null; },
  };
}

// Add all briefing GeoJSON sources/layers and the POI marker. Called after the initial
// style loads and after every base-style switch (setStyle clears all sources/layers).
function applyBriefingLayers(map, b) {
  addHatchImage(map);
  const m = b.mission;
  const w = b.watershed;

  // Watershed: kept (clipped) basin in cyan (FR-3).
  if (w?.geometry) {
    map.addSource("watershed", { type: "geojson", data: w.geometry });
    map.addLayer({ id: "watershed-fill", type: "fill", source: "watershed",
      paint: { "fill-color": "#38bdf8", "fill-opacity": 0.2 } });
    map.addLayer({ id: "watershed-line", type: "line", source: "watershed",
      paint: { "line-color": "#38bdf8", "line-width": 1.5 } });

    // Excluded remainder (beyond the RoC): hatched grey.
    if (w.excluded_geometry) {
      map.addSource("excluded", { type: "geojson", data: w.excluded_geometry });
      map.addLayer({ id: "excluded-fill", type: "fill", source: "excluded",
        paint: { "fill-pattern": "roc-hatch" } });
      map.addLayer({ id: "excluded-line", type: "line", source: "excluded",
        paint: { "line-color": "#64748b", "line-width": 1, "line-dasharray": [2, 3], "line-opacity": 0.7 } });
    }
  }

  // Radius of Concern ring: dashed orange. Prefer backend geometry; fall back to computed.
  const rocGeom = b.roc?.geometry
    || (b.roc?.center && b.roc?.radius_km
      ? circlePolygon(b.roc.center[0], b.roc.center[1], b.roc.radius_km) : null);
  if (rocGeom) {
    map.addSource("roc", { type: "geojson", data: rocGeom });
    map.addLayer({ id: "roc-line", type: "line", source: "roc",
      paint: { "line-color": UI_ORANGE, "line-width": 1, "line-dasharray": [4, 4] } });
  }

  // Lightning Area of Concern ring: solid yellow (PRD §16.1). Not fed to fitBounds so
  // the view centres on the watershed, not the potentially larger LAoC disk.
  const laocGeom = b.laoc?.geometry
    || (b.laoc?.center && b.laoc?.radius_km
      ? circlePolygon(b.laoc.center[0], b.laoc.center[1], b.laoc.radius_km) : null);
  if (laocGeom) {
    map.addSource("laoc", { type: "geojson", data: laocGeom });
    map.addLayer({ id: "laoc-line", type: "line", source: "laoc",
      paint: { "line-color": UI_YELLOW, "line-width": 1.5 } });
  }

  // Fit to the kept watershed bounds (FR-1).
  if (w?.geometry) {
    map.fitBounds(geoBBox(w.geometry), { padding: 24, maxZoom: 14 });
  }

  // Rebuild the POI marker (remove prior instance — markers survive setStyle as DOM elements).
  if (_poiMarker) { _poiMarker.remove(); _poiMarker = null; }
  const markerEl = document.createElement("div");
  markerEl.className = "map-poi-marker";
  markerEl.innerHTML =
    `<span class="map-poi-marker__label">${esc(m.name)}</span>` +
    `<div class="map-poi-marker__dot"></div>`;
  const popup = new maplibregl.Popup({ offset: 14, className: "map-popup" }).setHTML(poiPopupHtml(m));
  popup.on("open", () => {
    const btn = popup.getElement()?.querySelector("[data-move-point]");
    if (btn) btn.addEventListener("click", () => {
      _moveMode = true;
      map.getCanvas().style.cursor = "crosshair";
      popup.remove();
    }, { once: true });
  });
  _poiMarker = new maplibregl.Marker({ element: markerEl, anchor: "bottom" })
    .setLngLat([m.lon, m.lat])
    .setPopup(popup)
    .addTo(map);
}

function initMainMap(b) {
  const container = document.getElementById("leaflet-map");
  if (!container || !window.maplibregl) return;
  if (_mainMap) { _mainMap.resize(); return; }

  const m = b.mission;
  _mainMap = new maplibregl.Map({
    container, style: baseStyleForKey("wxtopo"),
    center: [m.lon, m.lat], zoom: 13, maxZoom: 19,
    attributionControl: true,
  });

  _mainMap.on("load", () => {
    applyBriefingLayers(_mainMap, b);

    // Move-mode: next tap relocates the POI and re-fetches (FR-1, FR-38). Wired once;
    // layer-scoped popup/watershed handlers below also survive style switches.
    _mainMap.on("click", (e) => {
      if (!_moveMode) return;
      _moveMode = false;
      _mainMap.getCanvas().style.cursor = "";
      const mission = state.briefing?.mission;
      if (!mission) return;
      mission.lat = e.lngLat.lat;
      mission.lon = e.lngLat.lng;
      if (_poiMarker) _poiMarker.setLngLat([mission.lon, mission.lat]);
      refresh(specFromBriefing(state.briefing));
    });

    // Hover cursor for clickable layers (layer-scoped; no-op when layer absent after setStyle).
    _mainMap.on("mouseenter", "watershed-fill", () => {
      if (!_moveMode) _mainMap.getCanvas().style.cursor = "pointer";
    });
    _mainMap.on("mouseleave", "watershed-fill", () => {
      if (!_moveMode) _mainMap.getCanvas().style.cursor = "";
    });
    _mainMap.on("mouseenter", "laoc-line", () => {
      if (!_moveMode) _mainMap.getCanvas().style.cursor = "pointer";
    });
    _mainMap.on("mouseleave", "laoc-line", () => {
      if (!_moveMode) _mainMap.getCanvas().style.cursor = "";
    });

    // Click popups (layer-scoped; survive setStyle because the same layer IDs are
    // re-added by applyBriefingLayers on each style switch).
    _mainMap.on("click", "watershed-fill", (e) => {
      if (_moveMode) return;
      const wb = state.briefing?.watershed;
      if (!wb) return;
      new maplibregl.Popup({ className: "map-popup" })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-pop">
          <div class="map-pop__title">Approximate Watershed</div>
          <div class="map-pop__row">HUC-12 <span class="mono">${esc(wb.huc12.join(", "))}</span></div>
          <div class="map-pop__row">Area <span class="mono">${wb.area_sq_mi.toFixed(1)} mi²</span></div>
        </div>`)
        .addTo(_mainMap);
    });
    _mainMap.on("click", "excluded-fill", (e) => {
      if (_moveMode) return;
      new maplibregl.Popup({ className: "map-popup" })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-pop">
          <div class="map-pop__title">Outside Radius of Concern</div>
          <div class="map-pop__row">Excluded from the weather-data domain</div>
        </div>`)
        .addTo(_mainMap);
    });
    _mainMap.on("click", "laoc-line", (e) => {
      if (_moveMode) return;
      new maplibregl.Popup({ className: "map-popup" })
        .setLngLat(e.lngLat)
        .setHTML(`<div class="map-pop">
          <div class="map-pop__title">Lightning Area of Concern</div>
          <div class="map-pop__row">Lightning assessed within this radius of the activity</div>
        </div>`)
        .addTo(_mainMap);
    });

    _mainMap.addControl(
      makeLayerSwitcherControl(BASE_LAYERS, (key) => {
        _mainMap.setStyle(baseStyleForKey(key));
        _mainMap.once("style.load", () => {
          if (state.briefing) applyBriefingLayers(_mainMap, state.briefing);
        });
      }),
      "top-right"
    );
  });

  state.mapInitialized = true;
}

/* ── 7.11 Briefing (full Markdown SITREP + optional Haiku framing) ── */

// Convert a bare URL or [label](url) fragment into a safe <a> tag.
// Tokenises text so URLs are extracted before HTML escaping, preventing
// esc() from double-encoding '&' inside query strings.
function _inlineFormat(text) {
  const tokens = [];
  const re = /\[([^\]]+)\]\((https?:\/\/[^)]+)\)|(https?:\/\/[^\s<>"]+)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) tokens.push({ t: "text", v: text.slice(last, m.index) });
    tokens.push({ t: "link", label: m[1] != null ? m[1] : m[3], url: m[2] != null ? m[2] : m[3] });
    last = m.index + m[0].length;
  }
  if (last < text.length) tokens.push({ t: "text", v: text.slice(last) });
  return tokens.map((tok) => {
    if (tok.t === "link") {
      return `<a href="${esc(tok.url)}" target="_blank" rel="noopener noreferrer">${esc(tok.label)}</a>`;
    }
    // Escape HTML then apply **bold** and _italic_.
    return esc(tok.v)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/_((?:[^_])+)_/g, "<em>$1</em>");
  }).join("");
}

// Lightweight Markdown → HTML converter for the deterministic SITREP skeleton.
// Handles headings, pipe tables, bullet lists, paragraphs, bold, and URLs.
// Only a subset of Markdown is produced by render.py / frame.py, so a full
// parser is unnecessary; this keeps the bundle zero-dependency.
function renderMarkdown(md) {
  if (!md) return '<p class="briefing-empty">No briefing text available for this cycle.</p>';
  const lines = md.split("\n");
  const out = [];
  let inList = false;
  let inTable = false;

  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();

    if (line.startsWith("### ")) {
      closeList(); closeTable();
      out.push(`<h3 class="bmd-h3">${_inlineFormat(line.slice(4))}</h3>`);
    } else if (line.startsWith("## ")) {
      closeList(); closeTable();
      out.push(`<h2 class="bmd-h2">${_inlineFormat(line.slice(3))}</h2>`);
    } else if (line.startsWith("# ")) {
      closeList(); closeTable();
      out.push(`<h1 class="bmd-h1">${_inlineFormat(line.slice(2))}</h1>`);
    } else if (line.startsWith("|")) {
      // Pipe table row; peek ahead to detect the header separator.
      const cells = line.split("|").slice(1, -1).map((c) => c.trim());
      const next = (lines[i + 1] || "").trim();
      if (!inTable && /^\|[\s:|-]+\|/.test(next)) {
        closeList();
        out.push('<div class="bmd-table-wrap"><table class="bmd-table"><thead><tr>');
        cells.forEach((c) => out.push(`<th>${_inlineFormat(c)}</th>`));
        out.push("</tr></thead><tbody>");
        inTable = true;
        i++; // skip separator line
      } else if (!inTable) {
        closeList();
        out.push('<div class="bmd-table-wrap"><table class="bmd-table"><tbody><tr>');
        cells.forEach((c) => out.push(`<td>${_inlineFormat(c)}</td>`));
        out.push("</tr>");
        inTable = true;
      } else {
        out.push("<tr>");
        cells.forEach((c) => out.push(`<td>${_inlineFormat(c)}</td>`));
        out.push("</tr>");
      }
    } else if (line.startsWith("- ")) {
      closeTable();
      if (!inList) { out.push('<ul class="bmd-list">'); inList = true; }
      out.push(`<li>${_inlineFormat(line.slice(2))}</li>`);
    } else if (line.trim() === "") {
      closeList(); closeTable();
    } else {
      closeList(); closeTable();
      out.push(`<p class="bmd-p">${_inlineFormat(line)}</p>`);
    }
  }
  closeList(); closeTable();
  return out.join("");
}

function renderBriefing(b) {
  const framedNote = b.framed
    ? `<div class="briefing-framed-note">Summary wording only (top section) generated by LLM — all hazard postures and severity tiers are deterministic engine output, not model-derived.</div>`
    : "";
  document.getElementById("view-briefing").innerHTML =
    `${framedNote}<div class="briefing-md">${renderMarkdown(b.markdown)}</div>`;
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
      <p style="font-size:var(--text-caption);color:var(--color-text-muted);margin-top:var(--space-3);overflow-wrap:anywhere">
        The most recent briefing is cached for offline review. ${b.cached || state.fromCache ? "Currently showing a cached copy." : "Online — showing the latest cycle."}
        Threshold matrix version <span class="mono">${esc(b.threshold_version)}</span>.
      </p>
    </section>
    <div class="disclaimer">UpstreamWX — planning reference only. Not an official forecast or warning. The go/no-go decision is always the responsibility of the user and their party.${formatAppVersion(state.appVersion) ? `<br><span style="opacity:0.7">${esc(formatAppVersion(state.appVersion))}</span>` : ""}</div>`;

  linkifyAcronyms(document.getElementById("view-resources"));
  const pdf = document.getElementById("export-pdf");
  if (pdf) pdf.addEventListener("click", () => exportBriefingPdf(b));
  const about = document.getElementById("open-about");
  if (about) about.addEventListener("click", openAbout);
}

/* Export the current briefing to PDF (FR-27). Hands the structured briefing to
 * the print template (frontend/pdf/briefing-pdf.html) through localStorage and
 * navigates to it with ?print=1, which renders the briefing and triggers the
 * browser's Save-as-PDF. Single-tab on purpose: iOS traps the print preview in
 * a popup tab that can't be dismissed, so we stay in the current tab and the
 * template offers a Back control to return here. Single-origin, so it works for
 * the deployed PWA and offline (template + logo are precached by the SW). */
function exportBriefingPdf(b) {
  const briefing = b || state.briefing;
  if (!briefing) return;
  try {
    localStorage.setItem("uwx.pdf.briefing", JSON.stringify(briefing));
  } catch (e) {
    // Storage blocked (private mode / quota): the template falls back to the
    // last-cached briefing. Rare; don't block the export.
  }
  window.location.assign("pdf/briefing-pdf.html?print=1");
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
    ["Extreme", "sev-extreme", "Active Flash Flood Warning for the area or the upstream domain"],
    ["High", "sev-high", "Flash Flood Watch or Flood Warning, or SREF P(precip) at or above 60% over the upstream domain with a convective-rate proxy"],
    ["Elevated", "sev-elevated", "Flood Advisory, Flood Watch, or AFD excessive-rainfall mention; or SREF P(precip) 20 to 59% with measurable forecast precip"],
    ["Minimal", "sev-minimal", "Low convective probability, dry upstream forecast"],
  ], "Same-day windows also evaluate HREF neighborhood P(QPF) (at or above 40% → High, 10 to 39% → Elevated) and take the higher of the SREF- and HREF-derived tiers. Antecedent rain bumps up one tier. For slot canyons, a convective rate at or above 0.5 in/hr over the domain is treated as at least High."],
  ["lightning", "Lightning", "Activity point and approach corridor (excluded in the technical span)", [
    ["Extreme", "sev-extreme", "Active thunderstorm warning, or SREF P(tstm) at or above 80% (85% when HREF is in-window), or HREF P(lightning) at or above 45%, or SPC categorical / Moderate / High risk"],
    ["High", "sev-high", "SREF P(tstm) 40 to 79%, or HREF P(lightning) 20 to 44%, or SPC Slight or Enhanced during an exposed phase"],
    ["Elevated", "sev-elevated", "SREF P(tstm) 15 to 39%, or HREF P(lightning) 8 to 19%, or SPC Marginal, or AFD describes isolated or scattered convection"],
    ["Minimal", "sev-minimal", "SREF P(tstm) below 15%, no convective signal"],
  ], "CAPE and lifted index modulate confidence but never set the tier. When the AFD describes isolated convection the final tier is capped at Elevated; scattered caps at High — unless HREF P(lightning) is at or above 60%, which bypasses the cap. HREF P(lightning) and P(reflectivity) are evaluated only in the same-day window (~6–36 h)."],
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

// Stripe-hosted donation page (FR: free, donation-supported). Opened in a new tab.
const DONATE_URL = "https://donate.stripe.com/3cIbIT75o6Jxept4aw4c800";
const CONTACT_EMAIL = "info@southeastexpeditionmed.com";

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
          <span class="posture-chip ${cls} about-matrix__tier">${esc(displayTier(tier))}</span>
          <span class="about-matrix__cond">${esc(cond)}</span>
        </div>`)
        .join("")}</div>
      <p class="about-haz__note">${esc(note)}</p>
    </div>`
  ).join("");

  document.getElementById("view-about").innerHTML = `
    <button class="about-back" id="close-about">${icon("arrow_left", "about-back__icon")}Resources</button>
    <h1 class="about-title">About UpstreamWX</h1>
    <p class="about-lede">UpstreamWX is a planning-reference briefing for caving and canyoneering. It gathers official and modeled weather, assesses four life-safety hazards (flash flooding, lightning, heat, and cold/wet hypothermia), and shows the reasoning. It never tells you whether to go.</p>

    <section class="card">
      <div class="eyebrow">Who makes UpstreamWX</div>
      <p class="about-p">UpstreamWX is built and maintained by Southeast Expedition Medical, a small team of cavers and wilderness medicine practitioners. We built the briefing tool we wanted for our own trips: one place that pulls the official forecast, the ensemble models, and the upstream watershed together, and shows the reasoning behind the risk assessment. It is free, has no ads, and asks for no personal data.</p>
      <a class="about-contact" href="mailto:${CONTACT_EMAIL}">${CONTACT_EMAIL}</a>
    </section>

    <section class="card about-support">
      <div class="eyebrow">Support UpstreamWX</div>
      <p class="about-p">Running UpstreamWX has real costs: the servers that process the SREF and HREF ensembles, the map and watershed data, and the time to keep the app up to date. It stays free because people who find it useful chip in. If it saved you time planning a trip, a donation helps keep it online for the next expedition.</p>
      <a class="about-donate" href="${DONATE_URL}" target="_blank" rel="noopener noreferrer">${icon("external", "about-donate__icon")}Donate</a>
      <p class="about-support__note">Secure checkout handled by Stripe. Give once or monthly, any amount.</p>
    </section>

    <div class="about-section-head">How this is calculated</div>
    <p class="about-p">The hazard posture labels (${["Minimal", "Elevated", "High", "Extreme"].map(displayTier).join(", ")}) follow standard risk-management terminology. As outdoor adventurers, our internal risk assessment is calibrated differently than most, so a posture like "${displayTier("High")}" or "${displayTier("Extreme")}" may read as stronger than you expect. Treat it as a prompt to look closer, not as a verdict. Our hazard postures are a measurement of assessed risk, not whether that risk can be mitigated.</p>

    <details class="card about-fold">
      <summary class="about-fold__summary">
        <span class="about-fold__head">
          <span class="eyebrow">The deterministic engine</span>
          <span class="about-fold__teaser">Why identical inputs always produce an identical posture, and what the summary writer can and cannot do.</span>
        </span>
        ${icon("chevron", "about-fold__chev")}
      </summary>
      <div class="about-fold__body">
        <p class="about-p">Every hazard posture, confidence level, and window of concern is decided by a deterministic, documented rule engine. Identical inputs always produce an identical result. The language model used for summary generation only frames the wording of the summary. It can never compute, raise, or lower a posture.</p>
        <ul class="about-list">
          <li>Four hazards are scored independently on a common scale (${["Minimal", "Elevated", "High", "Extreme"].map(displayTier).join(", ")}).</li>
          <li>Each hazard applies only in the expedition phases where it is relevant (approach, technical span, egress) and per activity type. A cave technical span is treated as isolated from surface weather and shows flash flood only.</li>
          <li>The overall expedition posture is the maximum across all applicable hazards, and every hazard stays visible, so a high lightning posture on approach is never hidden behind a low flood posture.</li>
          <li>A confidence qualifier per hazard comes from SREF ensemble agreement and cross-source consistency, including SREF and HREF agreement on same-day windows.</li>
        </ul>
      </div>
    </details>

    <details class="card about-fold">
      <summary class="about-fold__summary">
        <span class="about-fold__head">
          <span class="eyebrow">Data sourcing</span>
          <span class="about-fold__teaser">The official and modeled weather sources behind each hazard.</span>
        </span>
        ${icon("chevron", "about-fold__chev")}
      </summary>
      <div class="about-fold__body">
        <p class="about-p">Providers sit behind a common interface, so the engine never depends on a specific source. If a non-mandatory source is unavailable the briefing still renders, marking that input as unavailable.</p>
        <div class="about-sources">${sources}</div>
      </div>
    </details>

    <details class="card about-fold">
      <summary class="about-fold__summary">
        <span class="about-fold__head">
          <span class="eyebrow">How the watershed is delineated</span>
          <span class="about-fold__teaser">How the upstream basin that drains toward your point is traced.</span>
        </span>
        ${icon("chevron", "about-fold__chev")}
      </summary>
      <div class="about-fold__body">
        <p class="about-p">Flash-flood risk is assessed over the upstream contributing basin, the land that drains toward your point, not the point itself. This matters because a slot can flood under a clear sky from rain falling miles upstream. Aggregating probability over the drainage area is what catches that.</p>
        <ul class="about-list">
          <li>Your raw coordinate is snapped onto the mapped stream network with a raindrop trace. It follows the terrain downhill along a flow-direction grid until it reaches a stream, which is a hydrologically correct snap rather than a blind nudge.</li>
          <li>From that on-network pour point, the exact upstream contributing basin is delineated by splitting the catchment over the national NHD stream network. This is the precise drainage area above your point, not a coarse approximation.</li>
          <li>If a point will not snap, the system falls back to a deterministic alternative: resolve the containing USGS HUC-12 sub-watershed and collect every HUC-12 that drains into it from the Watershed Boundary Dataset. This is snap-free and reproducible, but coarser, since it counts whole sub-watersheds.</li>
          <li>Areas are measured on an equal-area projection, and each delineation is cached, so the same point yields the same basin and is reused across briefings.</li>
        </ul>
        <p class="about-p">The map's shaded basin is this delineated domain, labeled approximate. Surface delineation is a defensible proxy for canyoneering, but in karst terrain, this is a rough estimate at best. Underground drainage can cut across surface drainage basins, and cannot be readily modeled by available methodologies. Flash flood risk is intentionally assessed using the 16-km SREF grid, which gives a reasonable buffer area around the edges of the watershed and somewhat accounts for the influence of subsurface drainage. However, user judgment is always required.</p>
      </div>
    </details>

    <details class="card about-fold">
      <summary class="about-fold__summary">
        <span class="about-fold__head">
          <span class="eyebrow">Derived hazard thresholds</span>
          <span class="about-fold__teaser">The versioned cut points behind each hazard tier.</span>
        </span>
        ${icon("chevron", "about-fold__chev")}
      </summary>
      <div class="about-fold__body">
        <p class="about-p">Every cut point is externalized, versioned configuration, never hard-coded. The engine loads the thresholds at runtime, so tuning one is a configuration edit with provenance, not a code change. The values below are the accepted initial configuration, refined through field testing. Cut points that ride on an established system (NWS warnings and watches, Heat Index categories, the SPC outlook) are standard. The numeric probability and temperature break points are UpstreamWX proposals.</p>
        <div class="about-hazards">${thresholds}</div>
        <p class="about-haz__note about-wrap" style="margin-top:var(--space-3)">Loaded threshold matrix version: <span class="mono">${esc(b.threshold_version)}</span></p>
      </div>
    </details>

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
  // Show currency in the mission's local zone to match the window/label (FR-9, FR-41).
  const tz = b.mission.tz_name || undefined;
  const t = gen.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: tz });
  const cached = b.cached || state.fromCache;
  document.getElementById("status").innerHTML = `
    ${cached ? `<span class="cached-badge">${icon("wifi_off", "")} <span>Available<br>offline</span></span>` : ""}
    <span class="status-line__currency">Briefing current as of:<br>${esc(t)} ${esc(b.mission.timezone)}</span>
    ${b.degraded ? `<span class="degraded-note">· one source degraded</span>` : ""}`;
}

/* ── First-run acknowledgment (FR-31, Appendix C §17.1) ────────────── */
// Returns true if the ack was shown this load (so the caller defers any
// follow-on, like the first-run planner, until the user accepts).
function maybeShowAck(onAccept) {
  if (localStorage.getItem(ACK_KEY)) return false;
  const modal = document.getElementById("ack");
  modal.hidden = false;
  document.getElementById("ack-accept").addEventListener("click", () => {
    localStorage.setItem(ACK_KEY, new Date().toISOString());
    modal.hidden = true;
    if (onAccept) onAccept();
  });
  return true;
}

/* ── Mission planner (FR-1, FR-9, FR-33) ───────────────────────────────
 * Map-based mission editor: geocode an address or paste coordinates, long-press
 * to drop/move the point, drag it, use GPS, switch the topo/aerial/street
 * basemap, and name the point in its tooltip. Saving rebuilds the MissionSpec
 * and re-fetches a live briefing (refresh()), so the upstream watershed
 * re-traces for the new point (FR-1, FR-38). Input-only: no posture or
 * recommendation is shown here (FR-39). */
let _mpMap = null;
let _mpMarker = null;
let _mpRoc = null;
let _mpSpec = null;
// True once the user has manually edited the end field; suppresses the auto-follow
// that keeps end = start + 4 h while the end is still at its default.
let _mpEndUserSet = false;
// True while the name input still holds DEFAULT_NAME untouched; cleared on first keystroke.
let _nameIsDefault = false;
// Fallback view (CONUS center) when no point is set yet.
const MP_DEFAULT_CENTER = [-111.5, 39.5];  // MapLibre uses [lng, lat]

// Return "YYYY-MM-DDTHH:MM" for the current moment expressed in tzName (or browser local time).
function nowInTz(tzName) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  if (tzName) {
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: tzName, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(now);
      const g = (t) => parts.find((p) => p.type === t)?.value ?? "00";
      const hr = g("hour") === "24" ? "00" : g("hour");
      return `${g("year")}-${g("month")}-${g("day")}T${hr}:${g("minute")}`;
    } catch (_) { /* fall through */ }
  }
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

// Return "YYYY-MM-DDTHH:MM" for the next whole hour, expressed in tzName if supplied.
function nextWholeHour(tzName) {
  const now = new Date();
  const next = new Date(now);
  next.setMinutes(0, 0, 0);
  next.setHours(next.getHours() + 1);
  const pad = (n) => String(n).padStart(2, "0");
  if (tzName) {
    try {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: tzName, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(next);
      const g = (t) => parts.find((p) => p.type === t)?.value ?? "00";
      const hr = g("hour") === "24" ? "00" : g("hour");
      return `${g("year")}-${g("month")}-${g("day")}T${hr}:${g("minute")}`;
    } catch (_) { /* fall through to local time */ }
  }
  return `${next.getFullYear()}-${pad(next.getMonth() + 1)}-${pad(next.getDate())}T${pad(next.getHours())}:00`;
}

// Add h hours to a datetime-local string ("YYYY-MM-DDTHH:MM"), returning the same format.
function addHoursLocal(str, h) {
  const [date, time = "00:00"] = str.split("T");
  const [yr, mo, da] = date.split("-").map(Number);
  const [hour, mn] = time.split(":").map(Number);
  const d = new Date(yr, mo - 1, da, hour + h, mn);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// Reflect the slider index in the readout ("20 mi") and keep _mpSpec.radius_km in km.
function updateRocReadout(idx) {
  const el = document.getElementById("mp-radius-value");
  if (el) el.textContent = `${ROC_STOPS_MI[idx]} mi`;
}

// Live preview of the Radius of Concern on the planner map: a fine dashed orange ring
// centered on the current point, redrawn as the point or slider moves (FR-3).
function initPlannerRocLayer() {
  if (!_mpMap) return;
  _mpMap.addSource("mp-roc", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  _mpMap.addLayer({
    id: "mp-roc-line", type: "line", source: "mp-roc",
    paint: { "line-color": UI_ORANGE, "line-width": 1, "line-opacity": 0.95, "line-dasharray": [4, 4] },
  });
}

function updatePlannerRoc() {
  if (!_mpMap || !_mpSpec || !Number.isFinite(_mpSpec.lat) || !_mpSpec.radius_km) return;
  const src = _mpMap.getSource("mp-roc");
  if (src) src.setData(circlePolygon(_mpSpec.lon, _mpSpec.lat, _mpSpec.radius_km));
}

function inLatLonRange(lat, lon) {
  return (
    Number.isFinite(lat) && Number.isFinite(lon) &&
    lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
  );
}

// Parse free-form coordinates so a coordinate paste skips the geocoder. Accepts
// decimal "lat, lon" (comma or whitespace separated) and DMS
// (e.g. 34°39'54"N 85°21'42"W and common variants). Returns {lat, lon} in
// range, else null.
function parseCoords(str) {
  const s = String(str).trim();
  const dec = s.match(/^(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)$/);
  if (dec) {
    const lat = parseFloat(dec[1]);
    const lon = parseFloat(dec[2]);
    return inLatLonRange(lat, lon) ? { lat, lon } : null;
  }
  const dmsRe = /(\d{1,3})\s*[°d:\s]\s*(\d{1,2}(?:\.\d+)?)?\s*['m:\s]?\s*(\d{1,2}(?:\.\d+)?)?\s*["s]?\s*([NSEW])/gi;
  const parts = [];
  let mt;
  while ((mt = dmsRe.exec(s)) && parts.length < 2) {
    const deg = parseFloat(mt[1]);
    const min = mt[2] ? parseFloat(mt[2]) : 0;
    const sec = mt[3] ? parseFloat(mt[3]) : 0;
    const hemi = mt[4].toUpperCase();
    let dd = deg + min / 60 + sec / 3600;
    if (hemi === "S" || hemi === "W") dd = -dd;
    parts.push({ dd, hemi });
  }
  if (parts.length === 2) {
    const latP = parts.find((p) => p.hemi === "N" || p.hemi === "S");
    const lonP = parts.find((p) => p.hemi === "E" || p.hemi === "W");
    if (latP && lonP && inLatLonRange(latP.dd, lonP.dd)) return { lat: latP.dd, lon: lonP.dd };
  }
  return null;
}

// Free, attribution-bearing geocoder (Nominatim/OpenStreetMap). Called only on
// explicit submit (one request, honoring the ≤1 req/s usage policy).
async function geocodeAddress(q) {
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(q)}`;
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(res.status);
  const hits = await res.json();
  if (!hits.length) return null;
  return { lat: parseFloat(hits[0].lat), lon: parseFloat(hits[0].lon), label: hits[0].display_name };
}

function setPlannerStatus(msg, isError = false) {
  const el = document.getElementById("mp-search-status");
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle("is-error", !!isError);
}

// Update the label text on the planner marker element (replaces setTooltipContent).
function updateMarkerLabel(text) {
  if (!_mpMarker) return;
  const label = _mpMarker.getElement()?.querySelector(".mp-marker__label");
  if (label) label.textContent = text;
}

// Create or relocate the mission marker and sync the spec's coordinates.
// Name is read from the dedicated #mp-name field, not a popup.
function placeOrMoveMarker(latlng) {  // latlng is {lat, lng}
  if (!_mpMap) return;
  _mpSpec.lat = latlng.lat;
  _mpSpec.lon = latlng.lng;
  warmWatershedDebounced(latlng.lat, latlng.lng); // pre-warm the basin while the user keeps planning
  updatePlannerRoc();
  if (_mpMarker) {
    _mpMarker.setLngLat([latlng.lng, latlng.lat]);
  } else {
    const el = document.createElement("div");
    el.className = "mp-marker";
    el.innerHTML = `<span class="mp-marker__label">${esc(document.getElementById("mp-name")?.value || _mpSpec.name || "Expedition")}</span><div class="mp-marker__dot"></div>`;
    _mpMarker = new maplibregl.Marker({ element: el, draggable: true, anchor: "bottom" })
      .setLngLat([latlng.lng, latlng.lat])
      .addTo(_mpMap);
    _mpMarker.on("dragend", () => {
      const ll = _mpMarker.getLngLat();
      _mpSpec.lat = ll.lat;
      _mpSpec.lon = ll.lng;
      warmWatershedDebounced(ll.lat, ll.lng); // pre-warm after a drag
      updatePlannerRoc();
    });
  }
}

// Long-press to drop/move the point — use a short press timer on the map container,
// cancelled on drag/scroll/zoom. Mirrors the chart crosshair's pointer handling.
function initPlannerLongPress(container) {
  let timer = null;
  let sx = 0;
  let sy = 0;
  const clear = () => { if (timer) { clearTimeout(timer); timer = null; } };
  container.addEventListener("pointerdown", (e) => {
    if ((e.button && e.button !== 0) || e.target.closest(".maplibregl-ctrl")) return;
    sx = e.clientX;
    sy = e.clientY;
    const rect = container.getBoundingClientRect();
    const pt = _mpMap.unproject([e.clientX - rect.left, e.clientY - rect.top]);
    clear();
    timer = setTimeout(() => { timer = null; placeOrMoveMarker({ lat: pt.lat, lng: pt.lng }); }, 450);
  });
  container.addEventListener("pointermove", (e) => {
    if (timer && Math.hypot(e.clientX - sx, e.clientY - sy) > 10) clear();
  });
  container.addEventListener("pointerup", clear);
  container.addEventListener("pointercancel", clear);
  container.addEventListener("pointerleave", clear);
  _mpMap.on("movestart", clear);
  // Desktop fallback: right-click drops/moves the point.
  _mpMap.on("contextmenu", (e) => {
    e.preventDefault();
    placeOrMoveMarker({ lat: e.lngLat.lat, lng: e.lngLat.lng });
  });
}

function onPlannerLayerSwitch(key) {
  if (!_mpMap) return;
  _mpMap.setStyle(baseStyleForKey(key));
  _mpMap.once("style.load", () => {
    initPlannerRocLayer();
    updatePlannerRoc();
  });
}

function initPlannerMap() {
  const container = document.getElementById("mp-map");
  if (!container || !window.maplibregl) return;
  const hasPoint = _mpSpec && Number.isFinite(_mpSpec.lat);
  const center = hasPoint ? [_mpSpec.lon, _mpSpec.lat] : MP_DEFAULT_CENTER;
  const zoom = hasPoint ? 12 : 6;

  if (_mpMap) {
    _mpMap.resize();
    _mpMap.jumpTo({ center, zoom: hasPoint ? Math.max(_mpMap.getZoom(), 12) : 6 });
    if (hasPoint) placeOrMoveMarker({ lat: _mpSpec.lat, lng: _mpSpec.lon });
    return;
  }

  _mpMap = new maplibregl.Map({
    container, style: baseStyleForKey("wxtopo"),
    center, zoom, maxZoom: 19,
    attributionControl: true,
  });

  _mpMap.on("load", () => {
    initPlannerRocLayer();
    // Seed a marker at the starting point so a save is always valid; the user can
    // long-press, drag, search, or use GPS to move it.
    if (hasPoint) placeOrMoveMarker({ lat: _mpSpec.lat, lng: _mpSpec.lon });
  });

  initPlannerLongPress(container);
  _mpMap.addControl(makeLayerSwitcherControl(BASE_LAYERS, onPlannerLayerSwitch), "top-right");
}

// Open the planner over a starting spec (the saved/current mission, or a seed).
function openMissionPlanner(spec) {
  hideGlossaryPopover();
  _mpSpec = { ...(spec || DEFAULT_SPEC) };

  // Mission name — dedicated field above the search bar. Show the default in italic/grey
  // to signal it hasn't been explicitly named yet; tap clears it for quick entry.
  const nameEl = document.getElementById("mp-name");
  if (nameEl) {
    nameEl.value = _mpSpec.name || DEFAULT_NAME;
    _nameIsDefault = !_mpSpec.name || _mpSpec.name === DEFAULT_NAME;
    nameEl.classList.toggle("is-placeholder-name", _nameIsDefault);
  }

  document.getElementById("mp-activity").value = _mpSpec.activity || "canyon";
  document.getElementById("mp-slot").checked = !!_mpSpec.slot;

  // Smart time defaults: use the next whole hour in the location's timezone when the
  // spec's start is absent or in the past (first-run seed, stale saved missions).
  const tz = _mpSpec.tz_name ?? null;
  const startStr = String(_mpSpec.start || "").slice(0, 16);
  const startIsStale = !startStr || startStr <= nowInTz(tz);
  const startVal = startIsStale ? nextWholeHour(tz) : startStr;
  document.getElementById("mp-start").value = startVal;

  const endStr = String(_mpSpec.end || "").slice(0, 16);
  const endIsStale = !endStr || endStr <= startVal;
  document.getElementById("mp-end").value = endIsStale ? addHoursLocal(startVal, 4) : endStr;
  // Reset the flag so end auto-follows start again unless the user edits it.
  _mpEndUserSet = !endIsStale;

  // Radius of Concern: snap the saved value to the nearest stop and store it back in km.
  const rocIdx = nearestRocIndex(rocMiFromSpec(_mpSpec));
  _mpSpec.radius_km = ROC_STOPS_MI[rocIdx] * MI_TO_KM;
  document.getElementById("mp-radius").value = String(rocIdx);
  updateRocReadout(rocIdx);
  document.getElementById("mp-search-input").value = "";
  setPlannerStatus("");
  document.getElementById("mission-planner").hidden = false;
  requestAnimationFrame(initPlannerMap);
}

function closeMissionPlanner() {
  const modal = document.getElementById("mission-planner");
  if (modal) modal.hidden = true;
}

// Wire the planner's static controls once at startup.
function initPlannerControls() {
  document.getElementById("mp-search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = document.getElementById("mp-search-input").value.trim();
    if (!q || !_mpMap) return;
    const coords = parseCoords(q);
    if (coords) {
      _mpMap.jumpTo({ center: [coords.lon, coords.lat], zoom: Math.max(_mpMap.getZoom(), 13) });
      placeOrMoveMarker({ lat: coords.lat, lng: coords.lon });
      setPlannerStatus(`Coordinates ${coords.lat.toFixed(5)}, ${coords.lon.toFixed(5)}`);
      return;
    }
    setPlannerStatus("Searching…");
    try {
      const hit = await geocodeAddress(q);
      if (!hit) {
        setPlannerStatus("No match. Try a more specific address or paste coordinates.", true);
        return;
      }
      _mpMap.jumpTo({ center: [hit.lon, hit.lat], zoom: Math.max(_mpMap.getZoom(), 13) });
      placeOrMoveMarker({ lat: hit.lat, lng: hit.lon });
      setPlannerStatus(hit.label);
    } catch (err) {
      setPlannerStatus("Search is unavailable right now. Paste coordinates instead.", true);
    }
  });

  const gps = document.getElementById("mp-gps");
  if (!("geolocation" in navigator)) {
    gps.disabled = true;
    gps.title = "Location unavailable on this device";
  } else {
    gps.addEventListener("click", () => {
      if (!_mpMap) return;
      setPlannerStatus("Locating…");
      navigator.geolocation.getCurrentPosition(
        (pos) => {
          const { latitude, longitude } = pos.coords;
          _mpMap.jumpTo({ center: [longitude, latitude], zoom: 14 });
          placeOrMoveMarker({ lat: latitude, lng: longitude });
          setPlannerStatus(`Current location ${latitude.toFixed(5)}, ${longitude.toFixed(5)}`);
        },
        () => setPlannerStatus("Could not get your location. Check permissions or paste coordinates.", true),
        { enableHighAccuracy: true, timeout: 10000 }
      );
    });
  }

  const rocSlider = document.getElementById("mp-radius");
  rocSlider.addEventListener("input", () => {
    const idx = parseInt(rocSlider.value, 10) || 0;
    _mpSpec.radius_km = ROC_STOPS_MI[idx] * MI_TO_KM;
    updateRocReadout(idx);
    updatePlannerRoc();
  });

  // Start time: warn if in the past; auto-advance end by 4 h if end hasn't been set or
  // if the new start has moved past the existing end.
  document.getElementById("mp-start").addEventListener("change", () => {
    const startVal = document.getElementById("mp-start").value;
    if (!startVal) return;
    const tz = _mpSpec?.tz_name ?? null;
    if (startVal <= nowInTz(tz)) {
      setPlannerStatus("Start time is in the past — missions cannot be planned retroactively.", true);
    } else {
      setPlannerStatus("");
    }
    const endEl = document.getElementById("mp-end");
    if (!_mpEndUserSet || !endEl.value || startVal >= endEl.value) {
      endEl.value = addHoursLocal(startVal, 4);
    }
  });

  // End time: once the user touches this field it stops auto-following start.
  document.getElementById("mp-end").addEventListener("change", () => { _mpEndUserSet = true; });

  // Name field: clear default placeholder on focus, restore on blur if empty, sync to spec.
  const nameInput = document.getElementById("mp-name");
  if (nameInput) {
    nameInput.addEventListener("focus", () => {
      if (_nameIsDefault) {
        nameInput.value = "";
        nameInput.classList.remove("is-placeholder-name");
      }
    });
    nameInput.addEventListener("blur", () => {
      if (!nameInput.value.trim()) {
        nameInput.value = DEFAULT_NAME;
        _nameIsDefault = true;
        nameInput.classList.add("is-placeholder-name");
        if (_mpSpec) _mpSpec.name = DEFAULT_NAME;
        updateMarkerLabel(DEFAULT_NAME);
      }
    });
    nameInput.addEventListener("input", () => {
      _nameIsDefault = false;
      nameInput.classList.remove("is-placeholder-name");
      _mpSpec.name = nameInput.value;
      updateMarkerLabel(nameInput.value || "Expedition");
    });
  }

  document.getElementById("mp-cancel").addEventListener("click", closeMissionPlanner);

  document.getElementById("mp-save").addEventListener("click", () => {
    if (!_mpSpec || !Number.isFinite(_mpSpec.lat)) {
      setPlannerStatus("Set a point first — long-press the map, search, or use your location.", true);
      return;
    }
    const startVal = document.getElementById("mp-start").value;
    const endVal = document.getElementById("mp-end").value;
    if (!startVal || !endVal) {
      setPlannerStatus("Set a start and end time for the mission.", true);
      return;
    }
    const tz = _mpSpec?.tz_name ?? null;
    if (startVal <= nowInTz(tz)) {
      setPlannerStatus("Start time must be in the future — missions cannot be planned in the past.", true);
      return;
    }
    // Duration check: parse wall-clock strings as browser-local Date objects for arithmetic
    // (both values are in the same timezone so the offset cancels out).
    const startMs = new Date(startVal + ":00").getTime();
    const endMs = new Date(endVal + ":00").getTime();
    const prefs = loadPrefs();
    const minDurationHr = (prefs.approach_hrs ?? PHASE_DEFAULT_HR) + (prefs.egress_hrs ?? PHASE_DEFAULT_HR);
    const minDurationMs = minDurationHr * 3600 * 1000;
    if (endMs - startMs < minDurationMs) {
      const minLabel = phaseLabel(minDurationHr);
      setPlannerStatus(`End time must be at least ${minLabel} after start (approach + egress time).`, true);
      return;
    }
    const spec = {
      lat: _mpSpec.lat,
      lon: _mpSpec.lon,
      activity: document.getElementById("mp-activity").value,
      name: (document.getElementById("mp-name")?.value || "").trim() || DEFAULT_NAME,
      start: startVal,
      end: endVal,
      slot: document.getElementById("mp-slot").checked,
      party_size: _mpSpec.party_size ?? null,
      radius_km: _mpSpec.radius_km ?? null,
      frame: null,
    };
    closeMissionPlanner();
    refresh(spec);
  });
}

/* ── Settings (app-wide user prefs) ────────────────────────────────── */
function updateLaocReadout(idx) {
  const el = document.getElementById("settings-laoc-value");
  if (el) el.textContent = `${LAOC_STOPS_MI[idx]} mi`;
}
function updatePhaseReadout(id, idx) {
  const el = document.getElementById(id);
  if (el) el.textContent = phaseLabel(PHASE_STOPS_HR[idx]);
}

// Open the settings sheet, seeding all sliders from saved prefs.
function openSettings() {
  hideGlossaryPopover();
  const prefs = loadPrefs();
  const laocIdx = nearestLaocIndex(laocMiFromPrefs(prefs));
  const approachIdx = nearestPhaseIndex(prefs.approach_hrs ?? PHASE_DEFAULT_HR);
  const egressIdx = nearestPhaseIndex(prefs.egress_hrs ?? PHASE_DEFAULT_HR);

  const laocSlider = document.getElementById("settings-laoc");
  if (laocSlider) laocSlider.value = String(laocIdx);
  updateLaocReadout(laocIdx);

  const approachSlider = document.getElementById("settings-approach");
  if (approachSlider) approachSlider.value = String(approachIdx);
  updatePhaseReadout("settings-approach-value", approachIdx);

  const egressSlider = document.getElementById("settings-egress");
  if (egressSlider) egressSlider.value = String(egressIdx);
  updatePhaseReadout("settings-egress-value", egressIdx);

  document.getElementById("settings-modal").hidden = false;
}

function closeSettings() {
  const modal = document.getElementById("settings-modal");
  if (modal) modal.hidden = true;
}

// Wire the gear button and the settings sheet's controls once at startup.
function initSettingsControls() {
  const gear = document.getElementById("settings-open");
  if (gear) {
    gear.innerHTML = icon("settings", "");
    gear.addEventListener("click", openSettings);
  }
  document.getElementById("settings-cancel")?.addEventListener("click", closeSettings);
  // Backdrop click (outside the sheet) dismisses without saving.
  document.getElementById("settings-modal")?.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeSettings();
  });

  const laocSlider = document.getElementById("settings-laoc");
  laocSlider?.addEventListener("input", () => {
    updateLaocReadout(parseInt(laocSlider.value, 10) || 0);
  });

  const approachSlider = document.getElementById("settings-approach");
  approachSlider?.addEventListener("input", () => {
    updatePhaseReadout("settings-approach-value", parseInt(approachSlider.value, 10) || 0);
  });

  const egressSlider = document.getElementById("settings-egress");
  egressSlider?.addEventListener("input", () => {
    updatePhaseReadout("settings-egress-value", parseInt(egressSlider.value, 10) || 0);
  });

  document.getElementById("settings-save")?.addEventListener("click", () => {
    const laocIdx = parseInt(laocSlider?.value ?? "1", 10) || 0;
    const approachIdx = parseInt(approachSlider?.value ?? "1", 10) || 0;
    const egressIdx = parseInt(egressSlider?.value ?? "1", 10) || 0;
    const prefs = loadPrefs();
    prefs.laoc_radius_km = LAOC_STOPS_MI[laocIdx] * MI_TO_KM;
    prefs.approach_hrs = PHASE_STOPS_HR[approachIdx];
    prefs.egress_hrs = PHASE_STOPS_HR[egressIdx];
    savePrefs(prefs);
    closeSettings();
    // Re-generate so changes take effect (live path only; demo renders the frozen sample).
    if (!DEMO_MODE) {
      const spec = savedSpec() || (state.briefing ? specFromBriefing(state.briefing) : DEFAULT_SPEC);
      refresh(spec);
    }
  });
}

/* ── Add to Home Screen ────────────────────────────────────────────────
 * Capture the browser's install prompt and surface a one-tap "Install app" pill in the
 * status bar. Chromium fires `beforeinstallprompt`; Safari/iOS never do (users install
 * via Share → Add to Home Screen), so the pill just stays hidden there — pure
 * progressive enhancement. The apex landing page (landing/) points here for this button.
 */
function initInstallPrompt() {
  const btn = document.getElementById("install-app");
  if (!btn) return;
  // Already installed (running standalone) → no prompt to offer.
  const installed =
    window.matchMedia?.("(display-mode: standalone)").matches ||
    window.navigator.standalone === true;
  let deferred = null;
  if (!installed) {
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault(); // suppress the default mini-infobar; the pill drives install
      deferred = e;
      btn.hidden = false;
    });
  }
  btn.addEventListener("click", async () => {
    if (!deferred) return;
    btn.hidden = true; // a captured prompt can only be used once
    deferred.prompt();
    try { await deferred.userChoice; } catch (_) {}
    deferred = null;
  });
  window.addEventListener("appinstalled", () => {
    deferred = null;
    btn.hidden = true;
  });
}

/* ── Bootstrap ─────────────────────────────────────────────────────── */
function renderAll(b) {
  state.briefing = b;
  renderHeader(b);
  renderOverview(b);
  renderMap(b);
  renderHazards(b);
  renderBriefing(b);
  renderForecast(b);
  renderResources(b);
  renderAbout(b);
  renderStatus(b);
  selectTab(state.tab);
}

async function main() {
  try {
    const cfg = await fetch("data/display-config.json").then((r) => r.json());
    if (cfg?.tier_labels) Object.assign(TIER_LABELS, cfg.tier_labels);
    if (cfg?.heat_labels) Object.assign(TIER_LABELS, cfg.heat_labels);
  } catch (_) { /* keep identity defaults */ }
  renderTabs();
  initGlossaryInteractions();
  initPlannerControls();
  initSettingsControls();
  initInstallPrompt();
  // First run with no saved mission: present the planner so the user picks a
  // point. Defer until the ack is accepted when it's showing this load.
  const promptFirstRun = () => {
    if (savedSpec()) return;
    openMissionPlanner(state.briefing ? specFromBriefing(state.briefing) : DEFAULT_SPEC);
  };
  const ackShown = maybeShowAck(promptFirstRun);
  let b;
  try {
    b = await loadBriefing(savedSpec() || DEFAULT_SPEC);
  } catch (e) {
    document.getElementById("view-overview").innerHTML =
      `<section class="card"><p class="summary">Could not generate a briefing ` +
      `(${esc(String(e.message || e))}). The briefing service may be busy or a data ` +
      `source is unavailable — please try again shortly.</p></section>`;
    return;
  }
  const initialSpec = savedSpec() || DEFAULT_SPEC;
  renderAll(b);
  streamSummary(initialSpec);
  if (!ackShown) promptFirstRun();

  window.addEventListener("online", () => renderStatus(state.briefing));
  window.addEventListener("offline", () => { state.fromCache = true; renderStatus(state.briefing); });
}

// ── Release / stale-client detection ──────────────────────────────────
// The deployed release is published at version.json (stamped by deploy.sh / Pages CI).
// On boot we read it and register the service worker as sw.js?v=<release>, so each
// release is a distinct worker URL — forcing the browser to reinstall and evict the old
// caches. While the tab stays open we re-check version.json; when the deployed release
// changes underneath us the running code is stale, so we surface a non-dismissible reload
// banner (docs/deployment-workflow.md).
async function fetchRelease() {
  try {
    const res = await fetch("version.json", { cache: "no-store" });
    if (!res.ok) return null;
    const v = await res.json();
    return v && v.version ? String(v.version) : null;
  } catch (_) {
    return null; // offline / unstamped local dev — no release to compare against
  }
}

// Returns "UpstreamWX v0.5.0 - Public Beta" for pre-1.0 or "UpstreamWX v1.0.0" for 1.0+.
// Returns null when the version is unknown (local dev / offline first boot).
function formatAppVersion(release) {
  if (!release) return null;
  const raw = release.startsWith("v") ? release.slice(1) : release;
  const parts = raw.split(".").map(Number);
  const major = parts[0] ?? 0;
  const label = `UpstreamWX v${raw}`;
  return major < 1 ? `${label} - Public Beta` : label;
}

function showUpdateBanner() {
  const el = document.getElementById("update-banner");
  if (!el || !el.hidden) return;
  el.hidden = false;
  const btn = document.getElementById("update-reload");
  if (btn) btn.addEventListener("click", () => {
    // Flag that this reload was user-initiated via the banner, so the controllerchange
    // listener below knows not to fire a second reload (which causes a blank screen in
    // PWA standalone mode when clients.claim() fires mid-load).
    try { sessionStorage.setItem("uwx-reloading", "1"); } catch (_) {}
    window.location.reload();
  });
}

(async function initReleaseWatch() {
  const bootRelease = await fetchRelease();
  state.appVersion = bootRelease;

  if ("serviceWorker" in navigator) {
    // Reload once when a freshly deployed worker takes control, so the page runs the new
    // shell instead of the one this tab booted with. Guarded to fire only on an UPDATE
    // (a controller was already active), never on the first-ever registration.
    let _swRefreshing = false;
    const _hadController = !!navigator.serviceWorker.controller;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
      if (_swRefreshing || !_hadController) return;
      // If the banner already triggered a reload, the new SW taking control is expected —
      // don't fire a second reload (blank-screen race in PWA standalone mode).
      try {
        if (sessionStorage.getItem("uwx-reloading")) {
          sessionStorage.removeItem("uwx-reloading");
          return;
        }
      } catch (_) {}
      _swRefreshing = true;
      window.location.reload();
    });
    // Tie the worker URL to the release so a new release reinstalls the SW; fall back to
    // the bare worker when the release is unknown (offline boot / unstamped local dev).
    const swUrl = bootRelease ? `sw.js?v=${encodeURIComponent(bootRelease)}` : "sw.js";
    const register = () => navigator.serviceWorker.register(swUrl).catch(() => {});
    if (document.readyState === "complete") register();
    else window.addEventListener("load", register);
  }

  // Nothing to compare against without a known boot release; skip the drift watch.
  if (!bootRelease) return;
  const check = async () => {
    const current = await fetchRelease();
    if (current && current !== bootRelease) showUpdateBanner();
  };
  // Re-check periodically, and whenever the tab regains focus or connectivity.
  setInterval(check, 5 * 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) check(); });
  window.addEventListener("online", check);
})();

main();
