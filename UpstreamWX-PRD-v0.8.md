---
project: UpstreamWX
type: PRD
status: draft
version: 0.8
date: 2026-06-16
host: upstreamwx.com
author: Chris Lee
---

# UpstreamWX — Product Requirements Document (Draft v0.8)

## 0. Document Control

| Field | Value |
|---|---|
| Product | UpstreamWX |
| Host | `upstreamwx.com` |
| Form factor | Progressive Web App (PWA); no native app |
| Status | Draft v0.9 — HREF same-day high-resolution supplement added alongside SREF (de-risked, Spike C); compute environment resolved (existing UpstreamWX EC2); all decision items in §13 resolved |

**Changelog v0.8 → v0.9:** Added the **HREF (High-Resolution Ensemble Forecast)** as a *supplement* to SREF for same-day (≲36 h) briefings — NCEP's ~3 km convection-allowing ensemble, processed in-house from the same NOMADS `ensprod` GRIB2 + `.idx` pattern. HREF sharpens the flash-flood and lightning signal at convective scale inside the same-day window via neighborhood ensemble probability (`APCP` for flood; explicit `LTNG` and `REFC` for lightning); SREF (~16 km) still owns the longer planning horizon to 87 h. The engine **runs both in-range and takes the higher tier** (FR-19), using SREF↔HREF agreement as a cross-source confidence cue (FR-17). New FR-7a; updated §6.4 inputs, §6.3 FR-12 cadence, §7, §8, §11, §12, and Appendix B §16.1/§16.2/§16.5. Feasibility resolved YES (Spike C, `docs/m0.0/spike-c-report.md`): same retrieval machinery as SREF, now shared in `src/upstreamwx/grib/`.

**Changelog v0.7 → v0.8:** Compute environment resolved — small always-on backend on the existing UpstreamWX EC2 (scalable), recurring SREF/AFD refresh server-side, one-time/batch pre-processing on a dev machine (§7, §13 M). Cost model updated: hosting reuses provisioned infrastructure, so recurring cash cost at hundreds of users is near-zero beyond the existing instance; the real constraint is EC2 headroom for the SREF job (§11.3–11.4).

**Changelog v0.6 → v0.7:** Added FR-20a requiring all hazard thresholds to live in externalized, versioned configuration with provenance — never hard-coded — so values change without code changes. The Appendix B matrices are now the **accepted initial configuration**, tuned through field testing rather than a pre-build redline (§13 B resolved; Appendix B retitled accordingly). Companion development roadmap created (roadmap.md) with build milestones M0.0–M0.5, including an M0.0 foundation/de-risk phase ahead of the core build.

**Changelog v0.5 → v0.6:** Added a User Interface and Information Architecture specification (§6.8, FR-32–FR-42) and a screen inventory (Appendix D §18), derived from the reference mockups for **visual chrome and layout only**. The PRD remains the source of truth: the UI adopts the mockups' dark briefing chrome, tabbed IA, metric cards, and timeline Gantt, but uses the PRD's four hazards, the Minimal/Elevated/High/Extreme ladder, phase-primary organization, the upstream-watershed map overlay, per-hazard confidence (timeline hatching + explicit label), a display-only persistent/windowed distinction, and the reference-only disclaimer. Dropped from the mockups: alpine hazards, rough terrain, the alternate severity legend, "All Systems Go," push alerts, multi-waypoint routes, and the radar layer (deferred). Single-point location retained; routes deferred.

**Changelog v0.4 → v0.5:** Flash flood logic rescoped. The quantitative QPF-vs-FFG ratio (R) and FFG ingestion are **deferred to v1.x** — disproportionate build effort (fragmented RFC-level FFG distribution, areal QPF aggregation over the watershed polygon, duration/grid alignment). v1 flood logic now runs on active NWS flood products (near-term authoritative anchor) plus SREF ensemble probability over the upstream domain (planning horizon), with the antecedent-wetness modifier and the conservative slot fallback. Rationale: an active Flash Flood Watch/Warning already encodes the professional QPF-vs-FFG determination, so v1 ships defensibly without computing R in-house.
| Owner | Chris Lee |
| Brand | UpstreamWX (community-service tool; not a SEM product at this time) |

This draft reflects the discovery answers of 2026-06-16. Where an answer was not given or where two answers conflict, the assumption is stated inline and tagged **[ASSUMPTION]** or **[CONFLICT]** and carried into §13 Open Questions.

**Changelog v0.1 → v0.2:** Hazard model broadened from flash-flood-only to a multi-hazard, phase-aware model (flash flood, lightning, heat, cold/wet hypothermia) assessed across mission phases (approach, in-canyon/in-cave, egress). Flash flood remains the headline differentiator; the other three are first-class hazards, not footnotes.

**Changelog v0.2 → v0.3:** Data-source decisions resolved — Open-Meteo for derived numerical fields and **in-house processing of SREF** (no longer a conflict). Phase model refined: approach and egress both carry lightning plus thermal hazard, with heat weighted higher on approach and cold weighted higher on egress. Activity-type split added to the technical span — in a **canyon**, surface weather (flash flood + heat + cold) applies but lightning does not; in a **cave**, the system is isolated from surface weather and flash flooding is the sole surface-derived risk.

**Changelog v0.3 → v0.4:** Remaining open items resolved — four-hazard v1 set confirmed (A); phase inference set to first-hour approach / last-hour egress (B2); no accounts in v1 (E); free-form location placement, no catalog (G). Two pieces proposed for review: **Appendix B (§16)** proposed hazard threshold matrices and confidence logic; **Appendix C (§17)** proposed plain-language disclaimer and first-run acknowledgment.

---

## 1. Summary

UpstreamWX is a free, donation-supported PWA that produces a mission-specific weather briefing for caving and canyoneering trips across the contiguous United States. It synthesizes National Weather Service (NWS) forecast-office products, derived numerical model output, and Short-Range Ensemble Forecast (SREF) probability fields into a structured, BLUF-format situation report (SITREP) covering the four weather hazards that govern these sports: **flash flooding, lightning, heat stress, and cold/wet hypothermia**. The product is explicitly a **reference-only decision-support tool**: it surfaces a structured hazard assessment and links to authoritative source data for user verification. It does not issue go/no-go decisions and does not assume decision authority.

The hazards are assessed by **mission phase**, and the technical span differs by activity type:

- **Approach hike** (cave and canyon) — lightning plus thermal, with **heat weighted higher** (exposed terrain, midday exertion under load).
- **Technical span — canyon** — flash flooding (upstream watershed) plus surface heat and cold; **lightning does not apply** down in the slot.
- **Technical span — cave** — isolated from surface weather; **flash flooding is the sole surface-derived risk**. Caves are thermally stable, so surface heat/cold and lightning do not apply underground.
- **Egress** (cave and canyon) — lightning plus thermal, with **cold weighted higher** (a wet party exiting into cold air, wind, or a late-day temperature drop).

The core technical differentiator remains the **upstream-watershed flash flood model**: precipitation and convective probability are aggregated over the USGS hydrologic units that drain *into* the activity location, rather than reported at the point itself. No existing consumer product performs that aggregation for these sports. The lightning, heat, and cold/hypothermia models reuse much of the same ingested data (convective indices, SREF thunderstorm probability, temperature, humidity, wind) applied at the point and phase where each hazard matters.

---

## 2. Problem Statement

Caving and canyoneering carry several weather-driven, life-safety hazards, and the existing tool landscape addresses none of them in a mission-aware way:

1. **Flash flooding** — the dominant in-canyon/in-cave hazard, driven by precipitation falling outside the participant's field of view, often outside cell coverage, in the watershed *upstream* of the activity location. Point forecasts at the location do not capture it.
2. **Lightning** — a primary hazard on exposed approach hikes, rim travel, and slow egress, where a party cannot quickly reach shelter.
3. **Heat stress** — high heat index on approach and on exposed canyon sections drives heat illness, compounded by exertion and load.
4. **Cold / wet hypothermia** — a party exiting a wet canyon or a cool cave into cold air, wind, or a falling evening temperature is at hypothermia risk even when ambient conditions look benign on a standard forecast.

Existing tools do not address these as a set:

- General weather apps (Windy, Mountain-Forecast) report point forecasts with no watershed aggregation, no hazard logic, no phase awareness, and no synthesis.
- Climbing-specific tools (Climbit, the now-unmaintained ClimbingWeather.com) are point/crag oriented, with no flash flood model and no caving/canyoneering framing.
- The only watershed-aware product, the NWS Salt Lake City Southern Utah Flash Flood Outlook, is a static regional graphic — flood only, no national coverage, no mission framing, no ensemble or model drill-down.

The synthesis a meteorologically literate trip leader performs manually — reading the Area Forecast Discussion (AFD), checking HRRR for convective timing, checking SREF for probability and confidence, aggregating precipitation over the upstream drainage, and separately reasoning about lightning, heat, and post-exit cold — has no automated equivalent. UpstreamWX automates that synthesis across all four hazards and presents it in a phase-structured briefing format suited to consequential field decisions.

---

## 3. Goals and Non-Goals

### 3.1 Goals
1. Produce a phase-aware, multi-hazard weather briefing (flash flood, lightning, heat, cold/wet hypothermia) for any caving or canyoneering location in CONUS, with the upstream-watershed flash flood model as the technical centerpiece.
2. Present output in BLUF/SITREP format: posture and confidence first, drivers and timing second, full source data on drill-down.
3. Keep every hazard determination deterministic, documented, and reproducible.
4. Make the most recent briefing available offline (cached) and exportable to PDF.
5. Remain free to the user, sustainable at the scale of hundreds of users, funded by optional donation.

### 3.2 Non-Goals (v1)
1. Climbing support (deferred to a later phase; different hazard model).
2. Go/no-go recommendations or any output framed as a decision.
3. Native iOS/Android apps.
4. Satellite-messenger (inReach/Zoleo) integration — explicitly out of scope.
5. In-canyon / in-cave real-time alerting to a field device.
6. Full in-house GRIB2 pipeline for *all* numerical fields. v1 sources general derived fields from Open-Meteo; only the SREF ensemble is processed from native GRIB2 in-house (see §6.2, §7). A broader in-house pipeline (e.g., native HRRR) is a later-phase option.

---

## 4. Target Users

**Primary persona — Instructor / trip leader (SEM-adjacent).** A wilderness-medicine-literate trip leader or instructor planning a caving or canyoneering objective for a party. Comfortable with structured risk information, expects defensible reasoning, wants to verify against source data. Values a fast top-line read with the option to interrogate the underlying numbers.

**Secondary overlap — Search and Rescue.** SAR personnel assessing watershed flood risk for an objective or an ongoing operation. Same information needs, higher tolerance for technical depth, same reference-only posture.

**Account model (v1, confirmed).** No account system; missions are stored client-side on the device (see §6.3). An optional account for cross-device mission sync is deferred to a later phase.

---

## 5. Product Scope by Phase

| Phase | Scope |
|---|---|
| **MVP (v1)** | Caving + canyoneering. CONUS. Phase-aware multi-hazard SITREP (flash flood, lightning, heat, cold/wet hypothermia) with activity-type-specific technical span. Persistent + clearable mission object. Cached-offline + PDF export. Open-Meteo derived fields + NWS AFD/alerts + in-house SREF ensemble processing. |
| **v1.x** | Quantitative QPF-vs-FFG flood refinement (FFG ingestion + areal QPF aggregation, §16.1). Antecedent-precipitation / soil-saturation refinement. Multi-day mission windows. Mission sharing via link. |
| **v2** | Climbing hazard model (lightning/CAPE, rock-drying, ridge wind). Optional account + cross-device sync. |
| **v3+** | Karst-specific recharge modeling beyond surface HUC; user-contributed observations. |

---

## 6. Functional Requirements

### 6.1 Location and Watershed Resolution

- **FR-1.** The user shall specify an activity location by **free-form** map pin, search, or coordinates anywhere in CONUS. There is no curated catalog of pre-defined sites; any valid CONUS point is accepted, and its upstream watershed is traced on demand (FR-3) and cached for reuse.
- **FR-2.** The system shall resolve the location to its containing USGS hydrologic unit at the finest available resolution. **[ASSUMPTION]** HUC-12 (Watershed Boundary Dataset) is the practical national finest tier; HUC-10 is the fallback where HUC-12 is unavailable or where upstream tracing fails.
- **FR-3.** The system shall determine the **upstream contributing drainage** — the set of hydrologic units that drain into the activity location — and treat that domain, not the point, as the precipitation aggregation area for flash flood logic.
- **FR-4.** For caving locations, the system shall display an explicit caveat that surface HUC delineation is a proxy and that true karst recharge zones may not follow surface topography (see §10 and §13).

### 6.2 Data Ingestion

- **FR-5.** The system shall ingest the NWS Area Forecast Discussion (AFD) and active watches/warnings/advisories for the relevant zone(s) directly from the NWS API (`api.weather.gov`). The AFD forecaster discussion is available from no other source and is mandatory.
- **FR-6.** The system shall ingest derived numerical forecast fields (precipitation, QPF, convective indices, wind, temperature, relative humidity, apparent temperature) from **Open-Meteo**, which serves HRRR-derived output as JSON and is free for non-commercial use.
- **FR-7.** The system shall **process raw SREF ensemble fields in-house** — probability of measurable precipitation, probability of thunderstorms, and QPF exceedance/member spread — over the upstream domain, ingesting native GRIB2 from NCEP/EMC (NOMADS or the AWS/Google open-data mirrors). This is a committed backend component, scheduled to the SREF run cycle and cached. (Resolves the prior FR-6/FR-7 conflict: derived fields come from Open-Meteo, SREF is processed directly.)
- **FR-7a.** The system shall additionally **process raw HREF ensemble fields in-house** as a *same-day high-resolution supplement* to SREF (FR-7) — NCEP's ~3 km convection-allowing ensemble, ingested as native GRIB2 from NOMADS `ensprod` using the same `.idx` byte-range machinery as SREF. From the HREF `prob` product it shall extract **neighborhood ensemble probability (NEP)** of precipitation accumulation (`APCP`, 1 h/3 h windows → flash flood) and of convection (explicit `LTNG` lightning probability and `REFC` composite-reflectivity probability → lightning), with `CAPE` probability and the `sprd` product as supporting fields. HREF is used **only within the same-day window (≈6–36 h)**: it runs twice daily (00/12Z) to a 48 h horizon, so for the longer planning horizon the engine falls back to SREF (FR-7). Where both are in range the engine evaluates **both ensembles and takes the higher hazard tier** (FR-19), treating SREF↔HREF agreement as a confidence input (FR-17). Ingestion is **conditional** — HREF is fetched only when an active mission window falls in range, and only for that window's forecast hours — and cached. Feasibility de-risked in Spike C (§6.2 source pattern shared with FR-7; `docs/m0.0/spike-c-report.md`). The cold-start 0–6 h window is left to the HRRR-derived Open-Meteo layer (FR-6), where HREF spin-up skill is weakest.
- **FR-8.** *(v1.x, deferred.)* The system shall ingest NWS Flash Flood Guidance (FFG) for the relevant area to support the quantitative QPF/FFG flood refinement (§16.1). Deferred from v1 because gridded FFG distribution is fragmented across RFCs; v1 flood logic relies on active NWS flood products plus SREF ensemble probability instead.

### 6.3 Mission Object

- **FR-9.** The user shall be able to create a persistent **mission**: activity type (cave / canyon), location, date, time window, and optional party size and route note.
- **FR-9a.** The mission time window shall support optional **phase markers** — approach start, expected technical span, and egress end — so the engine can assess each hazard against the phase in which it is relevant. When the user supplies only an overall window, the engine shall infer phases as: **approach = the first hour** of the window, **egress = the last hour**, and **technical span = everything in between**. The briefing shall state that phases were inferred this way.
- **FR-10.** The mission shall persist across sessions on the device.
- **FR-11.** The user shall be able to **clear the mission and start fresh** at any time.
- **FR-12.** A briefing shall be (re)generated for the active mission on demand and on a refresh schedule while the mission window is within range. **[ASSUMPTION]** Refresh cadence aligned to SREF/AFD update cycles (SREF runs every 6 hours; AFDs issued roughly twice daily and updated as needed). When the mission window is within the same-day range, refresh additionally aligns to the **HREF cycle (00/12Z)** so the high-resolution supplement (FR-7a) stays current.

### 6.4 Hazard Rule Engine (Deterministic Core)

The engine produces a **vector of hazard postures** — one per hazard class — each mapped to the mission phase in which it is relevant. All postures are deterministic and documented; the language model frames wording only (§6.5) and never computes or alters a posture.

- **FR-13.** All hazard determinations shall be produced by a **deterministic, documented rule engine**. The language model performs natural-language framing only and shall not alter, infer, or override any hazard posture.
- **FR-14.** The engine shall compute four independent hazard postures over a common documented tiered scale. **[ASSUMPTION]** Tiers: Minimal / Elevated / High / Extreme. The hazard classes, the phases in which they apply, and their inputs:

  | Hazard | Phases where it applies | Spatial basis | Primary inputs |
  |---|---|---|---|
  | **Flash flood** | Technical span (canyon and cave) | Upstream contributing watershed (HUC-12) | Active NWS flood products, SREF P(precip)/P(thunder) over the upstream domain, **HREF neighborhood P(1 h/3 h QPF) over the upstream domain for same-day windows (FR-7a)**, derived convective-rate/reflectivity proxy, antecedent-precip proxy. *(v1.x: QPF-vs-FFG ratio — see §16.1.)* |
  | **Lightning** | Approach + egress only (surface, exposed); **excluded** in the technical span | Activity location + approach corridor | CAPE / lifted index, SREF P(thunderstorm), **HREF neighborhood P(lightning)/P(reflectivity) for same-day windows (FR-7a)**, derived convective/lightning proxy, AFD thunder mentions, SPC convective outlook |
  | **Heat stress** | Approach (weighted up) + canyon technical span + egress | Activity location (point) | Temperature + relative humidity → NWS heat index; apparent temperature; time-of-day exposure |
  | **Cold / wet hypothermia** | Egress (weighted up) + canyon technical span + approach | Activity location at the relevant time | Air temperature and wind (wind chill / apparent temp), evaluated under an assumed-wet party state on egress; evening temperature drop; elevation adjustment |

- **FR-14a.** The engine shall apply hazards by phase **and activity type** per the following applicability matrix. A cell marked "↑" means that hazard is weighted higher in that phase when forming the phase posture.

  | Phase | Canyon | Cave |
  |---|---|---|
  | **Approach** | Lightning, Heat ↑, Cold | Lightning, Heat ↑, Cold |
  | **Technical span** | Flash flood, Heat, Cold (no lightning) | Flash flood only (isolated from surface) |
  | **Egress** | Lightning, Cold ↑, Heat | Lightning, Cold ↑, Heat |

- **FR-14b.** Thermal hazard weighting shall be phase-dependent: on **approach**, heat is the weighted-primary thermal hazard and cold is secondary; on **egress**, cold is the weighted-primary thermal hazard and heat is secondary. Both are still computed and shown in each phase; weighting governs which leads the phase line and how the phase posture is formed.
- **FR-14c.** Lightning shall be **excluded from the technical span** for both activity types (sheltered in-slot; isolated underground). For a **cave** technical span, only flash flood shall be evaluated; surface heat, cold, and lightning shall be omitted as not applicable, and the briefing shall state that the cave interior is treated as isolated from surface weather. **[ASSUMPTION]** Canyon sections that are known to be exposed (non-slot) inherit approach-style lightning exposure; absent section-level data, v1 treats the whole technical span as sheltered and notes the assumption.
- **FR-15.** Heat stress tiers shall map to the established **NWS heat index categories** (Caution / Extreme Caution / Danger / Extreme Danger) rather than an invented scale, for defensibility.
- **FR-16.** Cold/wet hypothermia posture shall be computed under the explicit assumption that the party is **wet on egress** (standard for canyoneering and wet caves); this assumption shall be stated in the briefing so the user can discount it if it does not apply.
- **FR-17.** The engine shall compute a **confidence** qualifier per hazard, derived primarily from SREF ensemble spread and inter-source agreement. For same-day windows where HREF is in range (FR-7a), **SREF↔HREF agreement** is an additional confidence input: concurrence of the two independent ensembles raises confidence, material disagreement lowers it (§16.5).
- **FR-18.** The engine shall identify, per hazard, the **time window of concern** within the relevant mission phase.
- **FR-19.** The engine shall derive an **overall mission posture** as the maximum across all hazards that are applicable to the mission's phases and activity type (per FR-14a), while preserving and displaying each hazard and phase separately (a High lightning posture on approach must not be hidden behind a Minimal flood posture in the slot).
- **FR-20.** The threshold matrix and rule logic for every hazard shall be versioned and surfaced to the user (a "how this is calculated" reference), supporting the reference-only/defensible posture. The initial configured matrices are in **Appendix B (§16)**.
- **FR-20a.** All hazard thresholds shall be stored as **externalized, versioned configuration — never hard-coded** in engine logic. The engine shall load thresholds at runtime so that any change requires a config edit, not a code change. The Appendix B values are the **accepted initial configuration**, to be tuned through field testing rather than a pre-build review. Each config version shall carry provenance (effective date, rationale, and source of the change) to preserve defensibility and reproducibility (NFR-4).

### 6.5 SITREP Generation

- **FR-21.** The system shall render the rule-engine output as a **BLUF-format SITREP** using Claude Haiku (`claude-haiku-4-5`) for natural-language framing only.
- **FR-22.** The SITREP structure shall be: (1) BLUF — overall mission posture, then a one-line posture + confidence for each applicable hazard, with the window of concern for any that are Elevated or higher; (2) Phase breakdown — approach, technical span (activity-type-specific per FR-14a), egress, each listing its applicable hazard(s) with the phase-weighted thermal hazard leading; (3) Key drivers per active hazard; (4) Upstream-watershed summary (flash flood); (5) Drill-down to all source fields; (6) Source links (NWS AFD, alerts, model source) for verification; (7) Disclaimer.
- **FR-23.** The BLUF shall surface every hazard at Elevated or higher; a non-flood hazard shall never be omitted because the flood posture is low.
- **FR-24.** The model prompt shall receive the structured multi-hazard object and bounded source excerpts; it shall be constrained to narrate the provided postures and shall not be given authority to compute or change any risk.
- **FR-25.** Generated SITREP text shall be cached with its inputs so the same inputs reproduce the same briefing record.

### 6.6 Offline and Export

- **FR-26.** The PWA shall cache the most recent fully generated briefing (BLUF + drill-down data) for offline viewing.
- **FR-27.** The user shall be able to export the current briefing to **PDF** for offline use.
- **FR-28.** New briefing generation requires connectivity; offline mode is review-only of the cached briefing.

### 6.7 Liability and Disclaimer

- **FR-29.** Every briefing and the PDF export shall carry a persistent disclaimer stating the product is for reference only, is not a decision-making tool, and that the user must verify against authoritative NWS sources.
- **FR-30.** Every briefing shall link directly to the authoritative NWS source products used.
- **FR-31.** First-run and periodic acknowledgment of the reference-only nature, plus a persistent disclaimer on every briefing and PDF export. Proposed plain-language text is in **Appendix C (§17)**.

### 6.8 User Interface and Information Architecture

Visual direction (dark, glanceable, field-oriented "weather briefing" chrome) is taken from the reference mockups; **content and behavior are governed by this PRD, not the mockups**. Where a mockup element has no PRD basis it is dropped; where the PRD requires something the mockups omit, it is added. A screen-by-screen layout description is in **Appendix D (§18)**.

- **FR-32.** The app shall use a five-view information architecture: **Overview** (the BLUF), **Forecast**, **Map**, **Hazards**, **Resources**. Overview is the BLUF of FR-22; Forecast/Map/Hazards are the drill-down (FR-22 items 3–5); Resources holds source links, verification, PDF export, and offline status (FR-26, FR-27, FR-30).
- **FR-33.** The mission header shall expose the editable mission (FR-9) including an explicit **cave / canyon selector**, since activity type drives the hazard applicability matrix (FR-14a).
- **FR-34.** Hazard presentation shall be **phase-primary**: the Hazards view shall organize hazards by mission phase (approach → technical span → egress) as the primary axis, with wall-clock time as a secondary axis. Each hazard shall appear only in the phases where it applies under FR-14a (e.g., no lightning bar across a canyon technical span; a cave technical span shows flash flood only).
- **FR-35.** Severity shall be rendered on the **PRD ladder only** — Minimal / Elevated / High / Extreme (heat uses its NWS categories per FR-15). The mockups' Low/Moderate/Elevated/Extreme legend is not used. **[ASSUMPTION — proposed color mapping for review]** Minimal = green, Elevated = amber, High = orange, Extreme = red.
- **FR-36.** Per-hazard **confidence (FR-17)** shall be shown two ways: (a) on the timeline, bars are **solid for higher confidence and hatched for lower confidence**, adopting the mockups' "possible" hatching as the ensemble-spread cue; and (b) in each hazard's detail, an explicit **confidence label** (High / Moderate / Low). Confidence is never collapsed to a single mission-level value.
- **FR-37.** The timeline shall distinguish a **through-period (persistent)** hazard from an **acute, windowed** one as a *display attribute only*, not a change to the hazard model: a hazard at Elevated-or-higher across all applicable phases renders as a full-period bar; a hazard elevated only in specific phase(s) renders as windowed bar(s). Bar color encodes severity in both cases; persistence is orthogonal to severity.
- **FR-38.** The Map view shall render the **traced upstream contributing watershed (FR-3)** as an overlay, plus active NWS alert polygons and point conditions. **Radar/nowcast layers are out of scope for v1** (deferred); the v1 map is planning-oriented, not a live radar surface.
- **FR-39.** The overall mission posture (max across applicable hazards, FR-19) may be shown as a summary, but the UI shall contain **no go/no-go or "all clear / all systems go" language** of any kind (FR-25). Any status indicator shall describe **data currency** (e.g., "Briefing current as of <time>"), not a recommendation.
- **FR-40.** The reference-only disclaimer (Appendix C) shall be persistently visible on the Overview and on every briefing surface and export; the Resources view shall present the verify-against-NWS source links (FR-26).
- **FR-41.** The UI shall surface offline state: when showing a cached briefing (FR-26), it shall clearly indicate the briefing is cached and show its generation timestamp.
- **FR-42.** Implementation of these screens shall follow the environment's frontend design guidance at build time; this PRD specifies behavior and IA, not final visual tokens.

---

## 7. Technical Architecture (Proposed)

```
            ┌─────────────────────────────────────────────┐
            │              upstreamwx.com (PWA)        │
            │  Static frontend · service worker · IndexedDB │
            │  Mission store (client) · cached briefing      │
            │  PDF export (client-side render)               │
            └───────────────┬───────────────────────────────┘
                            │ HTTPS (briefing request / fetch cached)
            ┌───────────────▼───────────────────────────────┐
            │                  Backend / API                  │
            │  ┌────────────┐  ┌──────────────┐  ┌──────────┐ │
            │  │ Watershed   │  │ Ingestion     │  │ Rule     │ │
            │  │ resolver    │  │ orchestrator  │  │ engine   │ │
            │  │ (HUC/WBD,   │  │ (NWS, Open-   │  │ (determ- │ │
            │  │ upstream    │  │ Meteo, SREF/  │  │ inistic) │ │
            │  │ trace)      │  │ HREF, SPC)    │  │          │ │
            │  └────────────┘  └──────────────┘  └────┬─────┘ │
            │                                          │       │
            │                          ┌───────────────▼─────┐ │
            │                          │ SITREP framer        │ │
            │                          │ (Claude Haiku 4.5)   │ │
            │                          └─────────────────────┘ │
            │  Briefing cache (inputs + output) · scheduler     │
            └───────────────┬───────────────────────────────────┘
                            │
   ┌────────────────────────┼─────────────────────────────────────┐
   │                        │                                       │
┌──▼──────────┐   ┌─────────▼─────────┐   ┌──────────────┐   ┌──────▼──────┐
│ NWS API      │   │ Open-Meteo         │   │ SREF+HREF     │   │ USGS WBD     │
│ AFD, alerts, │   │ HRRR-derived       │   │ GRIB2 (NOMADS │   │ HUC-12       │
│ warnings,    │   │ JSON fields        │   │ ensprod),     │   │ (hosted)     │
│ heat index   │   │ (free non-comm.)   │   │ in-house      │   │              │
└──────────────┘   └────────────────────┘   └──────────────┘   └─────────────┘
```

**Notes**
- USGS Watershed Boundary Dataset (HUC-12) is downloaded once and hosted; upstream-contributing-area traces are computed **on demand** per free-form pin and cached for reuse. The hosted WBD is a capital item; tracing is light recurring compute.
- The **SREF processor** is a scheduled backend job: it pulls native GRIB2 on the SREF run cycle, extracts the ensemble probability fields over each active upstream domain, and caches the result for the rule engine. This is the heaviest backend component and the main recurring compute load.
- The **HREF processor** (FR-7a) shares the SREF processor's retrieval and aggregation code (the `.idx` byte-range subsetting and polygon zonal reduction live in a common GRIB module). It is the *same-day high-resolution supplement*: it runs on the HREF cycle (00/12Z) **only when an active mission window is in range (≈6–36 h)**, fetching just that window's forecast hours from the ~3 km `ensprod` `prob` product. Because HREF publishes one file per forecast hour, its per-cycle work is bounded by the in-range hours needed, not the full 48 h.
- Open-Meteo supplies the remaining derived fields by REST/JSON; no GRIB handling is needed for those.
- Briefings are generated server-side and cached; the client fetches the latest cached briefing and stores it for offline review. This caps LLM and API cost regardless of how often a user reopens the app.
- Backend is a **small always-on service on the existing UpstreamWX EC2 instance** (already provisioned, scalable on demand), with a scheduler on that instance driving recurring SREF/AFD refresh and briefing regeneration. **One-time and batch pre-processing** — watershed trace cache warming, validation-corpus preparation, threshold-config builds, and SREF extraction-tooling development — runs **on a dev machine**, with results deployed to the backend. The recurring SREF extraction itself runs server-side so briefings stay current on the SREF cycle.

---

## 8. Data Sources

| Source | Role | Access | Cost posture |
|---|---|---|---|
| NWS API (`api.weather.gov`) | AFD, watches/warnings/advisories, heat index categories; FFG *(v1.x)* | Public REST | Free; mandatory |
| Open-Meteo | Derived numerical fields: QPF, PoP, convective indices (CAPE/LI), temperature, relative humidity, apparent temperature, wind — feeding all four hazard models | REST/JSON | Free for non-commercial use |
| SREF ensemble (in-house) | Raw probability of thunderstorms / precip, member spread (flash flood + lightning); full planning horizon to 87 h | Native GRIB2 from NCEP (NOMADS), processed and cached server-side | Free data; recurring compute for scheduled processing |
| HREF ensemble (in-house) | ~3 km convection-allowing **neighborhood** probability — `APCP` 1 h/3 h QPF (flash flood), `LTNG`/`REFC` (lightning) — **same-day supplement (≈6–36 h)** to SREF | Native GRIB2 from NCEP NOMADS `ensprod`, same `.idx` machinery as SREF; processed and cached server-side, fetched conditionally per in-range mission | Free data; bounded recurring compute (in-range hours only) |
| SPC convective outlook | Categorical/probabilistic severe + thunderstorm outlook (lightning) | SPC / NWS | Free; secondary input |
| USGS Watershed Boundary Dataset | HUC-12 delineation, upstream tracing (flash flood) | Bulk download, self-hosted | Free; one-time capital |

---

## 9. Non-Functional Requirements

- **NFR-1.** PWA installable, responsive, usable on a phone at a trailhead.
- **NFR-2.** Cached briefing readable with zero connectivity.
- **NFR-3.** Briefing generation latency target ≤ ~10 s on demand. **[ASSUMPTION]** Acceptable given the planning (not real-time) use case.
- **NFR-4.** Deterministic reproducibility: identical inputs yield an identical hazard posture (LLM framing may vary in wording but not in stated posture/confidence/window).
- **NFR-5.** Source attribution and disclaimer present on every briefing and export.
- **NFR-6.** Graceful degradation: if a non-mandatory source is unavailable, the briefing renders with that input marked unavailable rather than failing.
- **NFR-7.** No collection of sensitive personal data; missions stored client-side by default.

---

## 10. Known Hazard-Model Limitations (to disclose in-product)

1. **Karst recharge ≠ surface watershed.** Surface HUC-12 delineation is a defensible proxy for canyoneering but can misrepresent the true recharge area for cave systems, which may cross surface divides via subsurface conduits. v1 discloses this explicitly; v3+ may add karst-specific modeling.
2. **Probability-of-precipitation semantics.** NWS PoP expresses areal coverage, a point that is widely misread by recreationists and has direct life-safety consequences in slot canyons. The SITREP must phrase probability in plain, unambiguous terms.
3. **Derived-field fidelity (Open-Meteo).** Open-Meteo serves HRRR-derived fields as JSON; it may smooth or reinterpret native model output, and for the sharpest convective timing the native HRRR is finer. v1 accepts this for the derived layer while processing SREF natively in-house, and discloses the model source. A native HRRR pipeline is a later-phase option.
3a. **HREF range and convective-scale uncertainty (FR-7a).** The HREF supplement reaches only **48 h** (used to ≈36 h), so beyond the same-day window the briefing relies on SREF alone — the high-resolution view is a near-term sharpening, not a planning-horizon tool. Even within range, convection-allowing ensembles are known to be **underdispersive** on convective initiation location/timing and can fire **spurious afternoon convection**; an HREF probability is a neighborhood likelihood, not a guarantee of a storm at the point. The briefing treats HREF as one input, surfaces it alongside SREF, and never lets a single ensemble's high-resolution signal close the decision loop.
4. **Non-flood hazards are point/corridor estimates.** Lightning, heat, and cold/hypothermia are assessed at the activity location and approach corridor, not aggregated over a watershed. Local terrain (slot shading, cold-air drainage, exposure) can deviate from the forecast point; the briefing states this.
5. **Wet-egress assumption.** Cold/hypothermia posture assumes the party exits wet. This is the conservative default for canyoneering and wet caves but may overstate risk for a dry cave; the assumption is shown so the user can discount it.
6. **Cave isolation and slot-shelter assumptions.** The cave interior is treated as isolated from surface weather (flash flood only), and the canyon technical span is treated as sheltered from lightning. Both are reasonable defaults but are coarse: a shallow cave entrance series, or an exposed non-slot canyon section, can violate them. v1 states these assumptions in the briefing.
7. **Reference-only.** The product never closes the decision loop. It is an input to the user's judgment, not a substitute for it.

---

## 11. Cost Model (Capital / Labor / Recurring Separated)

Scale basis: **hundreds of users at 12 months**, free with optional donation.

### 11.1 One-Time Capital (Infrastructure / Data)
- USGS WBD (HUC-12) acquisition and hosting setup — free data, modest storage.
- No location catalog to precompute (free-form placement, per §13 G). Upstream-contributing-area traces are computed on demand per pin and cached; cache warming for popular areas is an optional, low-cost background task, not an upfront catalog build.
- Domain registration (`upstreamwx.com`) — nominal.
- **Net capital outlay is low**; dominated by storage, not licensing or catalog construction.

### 11.2 One-Time Labor (Development)
Distinct from capital. Major build components, in rough descending effort:
1. **SREF GRIB2 processor** — scheduled native ingestion, ensemble-field extraction over upstream domains, caching. The heaviest single backend component (committed, not optional). The **HREF same-day supplement (FR-7a)** reuses this processor's retrieval/aggregation code (shared GRIB module), so its incremental labor is the conditional per-hour fetch loop and HREF-specific field selection, not a second pipeline.
2. Watershed resolver + upstream tracing.
3. Deterministic rule engine + versioned threshold matrices for all four hazards (flood, lightning, heat, cold/wet), including the phase/activity applicability matrix and thermal weighting.
4. Ingestion orchestrator (NWS products/AFD + Open-Meteo + SREF processor + SPC outlook).
5. SITREP framer integration (Haiku) with strict prompt constraints.
6. PWA shell, offline cache, PDF export.
7. Disclaimer/acknowledgment + source-link plumbing.

Deferred to v1.x (removed from v1 to keep scope tight): FFG ingestion across RFCs and areal QPF aggregation over the watershed polygon for the quantitative QPF/FFG flood refinement (§16.1). This was the most complex flood-side component and is not required to ship a defensible v1.

### 11.3 Recurring Operating Cost (Monthly)
| Line item | Driver | Estimate at hundreds of users |
|---|---|---|
| NWS API | Request volume | $0 |
| USGS WBD hosting | Storage | Negligible |
| App hosting (always-on backend + static + scheduler) | Runs on the **existing UpstreamWX EC2** | Marginal — reuses provisioned infrastructure; incremental cost is scaling headroom if needed, not a new instance |
| Open-Meteo (derived fields) | API calls | $0 (free non-commercial use) |
| SREF processing compute | Scheduled GRIB2 pulls + extraction per active upstream domain, on the existing EC2 | **Largest recurring workload**, but on already-provisioned hardware — cost is CPU time and the scaling headroom it consumes, not a separate bill. Bounded by run cadence and active-domain count, not user count |
| HREF processing compute (FR-7a) | Conditional GRIB2 pulls on the HREF cycle (00/12Z), only for in-range missions and only the needed forecast hours | Incremental over SREF: ~3 km grids decode to a higher peak memory (~0.9 GB observed for a small field set vs ~0.5 GB for SREF — Spike C), and per-hour files add HTTP round-trips. Still fits the existing EC2; bound it by fetching only exposure-window hours and decoding fields sequentially |
| Claude Haiku 4.5 (SITREP framing) | Briefings generated | **Small.** See §11.4 |

### 11.4 LLM Cost Detail (Claude Haiku 4.5)
Published rates: **$1.00 / million input tokens, $5.00 / million output tokens**, with prompt caching reducing cached input to ~$0.10 / million and batch processing at 50% off.

Per-briefing estimate (structured hazard object + bounded AFD excerpt in, BLUF prose out): on the order of ~4,000 input + ~800 output tokens ≈ **$0.008 per briefing** at standard rates, lower with prompt caching on the fixed system/rule prompt and lower again via batch generation of scheduled briefings.

At hundreds of users generating, conservatively, a few hundred briefings per day, Haiku spend is in the **low tens of dollars per month** — not the cost driver. With Open-Meteo and the NWS API free, the SREF workload running on the already-provisioned UpstreamWX EC2, and Haiku small, the **recurring cash cost at hundreds of users is near-zero beyond the existing instance**. The real constraint is EC2 headroom for the SREF job, not a monthly bill; scale the instance if active-domain count grows.

---

## 12. Data-Source Decisions (Resolved)

| Decision | Outcome | Rationale |
|---|---|---|
| Derived numerical fields | **Open-Meteo** | Free for non-commercial use; serves HRRR-derived fields as JSON; fits a free, donation-funded tool at hundreds of users with no licensing exposure. |
| SREF ensemble | **Processed in-house from native GRIB2** | No derived/commercial API reliably exposes raw SREF probability and member spread; native processing is the only way to meet the raw-ensemble requirement. Committed backend component (see §6.2 FR-7, §7, §11.2). |
| HREF ensemble (same-day supplement) | **Processed in-house from native GRIB2**, reusing the SREF machinery | Same rationale as SREF — no API exposes raw HREF neighborhood probability — and the cost is small because the retrieval/aggregation code is shared and ingestion is conditional and window-scoped. Adds convection-allowing (~3 km) flash-flood/lightning detail SREF cannot resolve inside the same-day window (see §6.2 FR-7a, §7, §11; Spike C). |
| Forecaster discussion | **NWS API (`api.weather.gov`)** | The AFD is available from no other source; mandatory regardless of the derived-field choice. |

This resolves the v0.2 open items C (derived API) and D (SREF sourcing). The cost consequence is favorable — no commercial data licensing — at the expense of carrying the SREF processing job in-house as the main recurring compute load (§11.3).

The residual trade-off to keep in view: Open-Meteo is best-effort (no commercial SLA). For a reference-only community tool this is acceptable, and graceful degradation (NFR-6) covers transient unavailability. If reliability later proves insufficient, a paid provider can be revisited as a swap behind the same ingestion interface.

---

## 13. Open Questions / Decisions Required

All v0.2 open items are now resolved. Decisions recorded below for traceability.

- **A. Phase-1 sport split and hazard set. — RESOLVED (v0.4):** caving + canyoneering, with the four hazards (flash flood, lightning, heat, cold/wet hypothermia) as the **confirmed v1 set**. Wind, water-temperature drop, and other candidates are deferred beyond v1.
- **B. Hazard tier scales and thresholds. — RESOLVED (v0.7):** the Appendix B matrices plus confidence qualifier are **accepted as the initial configured values**, to be tuned through field testing rather than a pre-build redline. All thresholds are externalized config, never hard-coded (FR-20a), so tuning never requires a code change. The numeric cut points remain UpstreamWX-origin (vs the established category systems they sit on) and field testing is the mechanism for refining them.
- **B2. Phase inference. — RESOLVED (v0.4):** inference is acceptable as default; approach = first hour, egress = last hour, technical span = the remainder (FR-9a).
- **C. Derived API decision. — RESOLVED (v0.3):** Open-Meteo selected for derived fields. See §12.
- **D. Raw SREF sourcing. — RESOLVED (v0.3):** SREF processed in-house from native GRIB2; the prior conflict is closed because derived fields come from Open-Meteo while SREF is handled directly. See §6.2 FR-7 and §12.
- **E. Account model. — RESOLVED (v0.4):** no accounts in v1; missions stored client-side. Optional account/sync deferred to a later phase.
- **F. Disclaimer language. — PROPOSED (v0.4), pending your review:** plain-language (non-legalese) disclaimer and first-run acknowledgment text in **Appendix C (§17)**.
- **G. Location selection. — RESOLVED (v0.4):** free-form pin/search/coordinate placement anywhere in CONUS, with on-demand cached watershed tracing; no curated catalog (FR-1, §11.1).
- **H. UI/UX spec location. — RESOLVED (v0.6):** specified inside the PRD (§6.8 + Appendix D §18), consistent with the single-source-of-truth principle. Proceeded on the documented lean since Q1 was not separately answered; reversible if a standalone design doc is later preferred.
- **I. Hazard view organization. — RESOLVED (v0.6):** phase-primary, wall-clock secondary (FR-34).
- **J. Confidence rendering. — RESOLVED (v0.6):** both timeline hatching and an explicit per-hazard confidence label (FR-36).
- **K. Persistent vs windowed hazards. — RESOLVED (v0.6):** display-only distinction; no change to the hazard model (FR-37).
- **L. Radar/nowcast layer. — RESOLVED (v0.6):** deferred beyond v1; v1 map shows watershed overlay, alert polygons, and point conditions (FR-38).
- **Mockup elements explicitly dropped (v0.6):** alpine hazards (high winds, icing, blowing snow), non-weather "rough terrain," the Low/Moderate/Elevated/Extreme legend, "All Systems Go" status, "Add as Alert" push notifications, and multi-waypoint routes. Severity ladder remains the PRD's Minimal/Elevated/High/Extreme (FR-35).
- **M. Compute environment. — RESOLVED (v0.8):** small always-on backend on the **existing UpstreamWX EC2 instance** (scalable on demand); recurring SREF/AFD refresh runs server-side via a scheduler; one-time/batch pre-processing runs on a dev machine. No serverless, no new instance for v1 (§7, §11.3).

---

## 14. Success Signals (community tool, not revenue)

- Briefings generated per active mission window.
- Repeat use across distinct missions per user.
- Qualitative trust feedback from the SEM/SAR community.
- Donation conversion as a soft sustainability signal (not a primary KPI).

---

## 15. Appendix — SITREP Output Skeleton (illustrative)

```
UPSTREAMWX — MISSION BRIEFING
Mission: <name>  |  Type: Canyon/Cave  |  Window: <date/time range>
Location: <coords>  |  Upstream domain: HUC-12 <ids>

BLUF
  OVERALL POSTURE: <max across hazards>   Confidence: <Low|Mod|High>
    Flash flood : <Minimal|Elevated|High|Extreme>  (<window of concern>)
    Lightning   : <Minimal|Elevated|High|Extreme>  (<window of concern>)
    Heat        : <Caution|Ext. Caution|Danger|Ext. Danger>
    Cold/wet    : <Minimal|Elevated|High|Extreme>  (assumes wet egress)

PHASE BREAKDOWN
  Approach (<time>)  : Lightning <..>, Heat <..> (primary), Cold <..>
  Technical span (<time>)
    if CANYON         : Flash flood <..>, Heat <..>, Cold <..>  (no lightning — sheltered)
    if CAVE           : Flash flood <..>  (interior isolated from surface weather)
  Egress (<time>)    : Lightning <..>, Cold <..> (primary), Heat <..>

KEY DRIVERS (per active hazard)
  Flash flood : active NWS products <..>, SREF P(precip/thunder) <..>,
                convective rate <..>, antecedent precip <..>
  Lightning   : CAPE/LI <..>, SREF P(tstm) <..>, SPC outlook <..>
  Heat        : heat index <..>, apparent temp <..>
  Cold/wet    : egress air temp <..>, wind chill <..>

UPSTREAM WATERSHED SUMMARY
  <plain-language aggregation over the contributing drainage>

SOURCE DATA (drill-down)
  <full numerical fields, model source, AFD key messages>

SOURCES (verify)
  NWS AFD: <link>   Active alerts: <link>   Model source: <link>

DISCLAIMER
  Reference only. Not a decision-making tool. Verify against NWS.
```

*Every hazard posture, confidence, and window is produced solely by the deterministic rule engine. Claude Haiku frames wording only and cannot change a posture.*

---

## 16. Appendix B — Hazard Threshold Matrices (initial configured values)

These are the **accepted initial configuration**, loaded by the engine as versioned config (FR-20a) and tuned through field testing rather than a pre-build redline. Two kinds of cut point appear below: those that ride on an **established category system** (NWS Heat Index categories, SPC convective outlook, active NWS warnings/watches, the QPF-vs-FFG comparison technique) are noted as such; the **numeric probability and temperature cut points are UpstreamWX-origin** and are the values field testing will most likely refine. All tiers use the common scale Minimal / Elevated / High / Extreme except heat, which uses the NWS categories directly per FR-15.

### 16.1 Flash Flood

v1 flood logic is driven by signals that are cheap to obtain: active NWS flood products as the authoritative near-term anchor, and SREF ensemble probability over the upstream domain for the planning horizon. The quantitative QPF-vs-FFG ratio is a v1.x refinement (see below), deferred because it is disproportionate to build for v1.

| Tier | Condition (any one triggers) |
|---|---|
| **Extreme** | Active **Flash Flood Warning** for the area or the upstream domain |
| **High** | Active **Flash Flood Watch**; OR SREF P(precip/thunderstorm) ≥ 60% over the upstream domain with a meaningful convective-rate proxy |
| **Elevated** | SREF P(precip/thunderstorm) 20–60% over the upstream domain with measurable forecast precip; no watch/warning yet |
| **Minimal** | Low convective probability; dry upstream forecast; no active products |

Modifiers:
- **Antecedent wetness** — significant rainfall in the prior 24–72 h (proxy for saturated soils / elevated baseflow) bumps the tier up one level.
- **Slot fallback** — for a **slot** canyon, treat any forecast convective rainfall rate over the upstream domain exceeding ~0.5 in/hr as **at least High**, because slots flood at low totals. Intentionally conservative; flag it in the briefing.

Why this is enough for v1: an active **Flash Flood Watch/Warning already encodes the QPF-vs-FFG determination** made by NWS forecasters with radar nowcasting we do not have, so the products carry the near-term expert judgment directly. SREF probability over the upstream domain covers the planning horizon beyond warning lead time, which is where a trip leader spends most of their attention.

**HREF same-day overlay (FR-7a).** When the mission window is within HREF range (≈6–36 h), the engine *also* evaluates HREF **neighborhood** P(QPF) over the upstream domain and takes the higher resulting tier (FR-19). Because HREF probabilities are neighborhood/3 km/11-member, they are **not comparable to SREF's grid-point/16 km/27-member** values and carry their own versioned cut points (FR-20a) — proposed initial set, tuned by field testing:

| Tier | HREF condition (any one triggers) |
|---|---|
| **High** | HREF neighborhood **P(≥0.5 in/1 h) ≥ 40%** over the upstream domain, OR P(≥1 in/3 h) ≥ 40% |
| **Elevated** | HREF neighborhood P(≥0.5 in/1 h) 10–40% over the upstream domain |

For a **slot** canyon the slot fallback applies to the HREF signal too: a non-trivial neighborhood probability of ≥0.5 in/1 h over the upstream domain is treated as **at least High**, flagged in the briefing. These HREF break points (40% / 10%) are **UpstreamWX proposals**, distinct from the SREF ones above.

**v1.x quantitative refinement (deferred — QPF/FFG ratio).** Once FFG ingestion and areal QPF aggregation exist, add a basin-specific ratio R = (forecast QPF over the upstream domain, for the FFG duration) ÷ (FFG for that duration), with proposed tiers Extreme at R ≥ 1.0, High at 0.5 ≤ R < 1.0, Elevated at 0.25 ≤ R < 0.5. This sharpens the assessment *before* a watch is issued. It is out of v1 scope because: (1) gridded FFG is fragmented across RFCs with no single clean national API; (2) "QPF over the upstream domain" requires sampling and aggregating forecast precip across the watershed polygon, layered on the watershed trace; and (3) QPF and FFG must be aligned on duration, grid, and projection. The active-warning anchor covers the near term in the meantime.

Established vs proposed: warning/watch overrides and the eventual QPF-vs-FFG technique are standard NWS operational practice; the probability cut points, the 0.5 in/hr slot fallback, and the R break points are **UpstreamWX proposals**.

### 16.2 Lightning (approach and egress only)

Primary basis is SREF probability of thunderstorms over the exposure window at the location, cross-checked against the SPC convective outlook and AFD. CAPE/Lifted Index modulate confidence and severity but are not the primary trigger.

| Tier | Condition (any one triggers) |
|---|---|
| **Extreme** | Active Severe Thunderstorm / Thunderstorm warning; OR SREF P(tstm) ≥ 70% in the exposure window; OR SPC categorical thunder/severe over the window |
| **High** | SREF P(tstm) 40–69%; OR SPC Slight/Enhanced risk during an exposed phase |
| **Elevated** | SREF P(tstm) 15–39%; OR SPC Marginal; OR AFD mentions isolated/scattered afternoon convection during an exposed phase |
| **Minimal** | SREF P(tstm) < 15%; no convective mention |

Supporting context (instability), used to modulate confidence/severity, not to set the tier:
- CAPE < 500 J/kg: minimal instability; 500–1000: marginal; 1000–2500: moderate; > 2500: strong.

Established components: SPC outlook categories and the active-warning override are standard. The P(tstm) cut points (15 / 40 / 70%) are **UpstreamWX proposals**. Note in-product: the forecast is for planning; in the field, "when thunder roars, go indoors" and direct observation govern.

**HREF same-day overlay (FR-7a).** Within HREF range (≈6–36 h) the engine also evaluates HREF neighborhood convection probability over the exposure window and takes the higher tier (FR-19). HREF gives an **explicit** `LTNG` neighborhood P(lightning) plus `REFC` P(composite reflectivity ≥ 40 dBZ) as a convective-mode proxy — a sharper same-day lightning signal than SREF P(tstm). Proposed initial cut points (versioned config, FR-20a; distinct from SREF's because of the neighborhood/3 km/11-member basis): **Extreme** at HREF P(lightning) ≥ 60% or P(reflectivity ≥ 40 dBZ) ≥ 60% in the exposure window; **High** at 30–60%; **Elevated** at 10–30%. These break points are **UpstreamWX proposals**.

### 16.3 Heat Stress (NWS Heat Index categories, per FR-15)

Uses the **established NWS Heat Index categories** directly rather than the Minimal/Elevated/High/Extreme scale.

| NWS category | Heat index range | Notes |
|---|---|---|
| **Caution** | 80–90 °F | Fatigue possible with prolonged exposure/activity |
| **Extreme Caution** | 90–103 °F | Heat cramps/exhaustion likely with prolonged exposure/activity |
| **Danger** | 103–124 °F | Heat exhaustion likely; heat stroke possible |
| **Extreme Danger** | ≥ 125 °F | Heat stroke highly likely |

Modifier (UpstreamWX proposal): because the approach phase involves **exertion under load**, the briefing shall add an advisory that effective heat strain runs one category hotter than the ambient heat index suggests, and shall surface heat at **Caution or above** as a phase-relevant line on approach. The category bands themselves are the NWS standard and are not changed.

### 16.4 Cold / Wet Hypothermia (egress-weighted; assumes wet party)

There is no single official "wet hypothermia index," so this matrix is a **UpstreamWX proposal grounded in wilderness-medicine reasoning** and is the one most in need of your review. It is built on **apparent temperature** (air temperature adjusted for wind) at the relevant window, under the assumption the party is wet on egress (FR-16). Bands are intentionally warmer than dry-condition cold thresholds, because wet clothing loses most of its insulating value and drives evaporative and conductive heat loss.

| Tier | Apparent temperature (wet party) | Rationale |
|---|---|---|
| **Extreme** | ≤ 32 °F | Wet at/below freezing → rapid, severe hypothermia |
| **High** | 33–45 °F | Strong hypothermia risk for a wet party, compounded by wind/fatigue |
| **Elevated** | 46–60 °F | The "deceptively mild" band that catches wet, tired parties |
| **Minimal** | > 60 °F with low wind | Low risk, though still possible with sustained immersion |

Modifiers:
- **Wind** is already folded into apparent temperature; sustained wind on an exposed egress can push a band colder.
- **Dry party** — if the activity is a dry cave with no immersion, the user may discount by roughly one tier; the briefing states the wet assumption so this is visible.
- Established components: none of the band edges are an official standard; they are proposals. The use of apparent temperature/wind chill is standard.

### 16.5 Confidence Qualifier (all hazards, per FR-17)

Derived from SREF ensemble agreement and cross-source consistency. Proposed operationalization:

| Confidence | Condition |
|---|---|
| **High** | ≥ 75% of SREF members support the assigned tier condition, and AFD / SPC / derived fields are consistent |
| **Moderate** | 40–75% member support, or partial disagreement among sources |
| **Low** | < 40% member support, or sources materially conflict |

The agreement fractions (40% / 75%) are **UpstreamWX proposals**.

**SREF↔HREF cross-ensemble agreement (FR-7a, FR-17).** For same-day windows where HREF is in range, the two independent ensembles provide a cross-source check on top of within-ensemble member support:

- **Concurrence** — both SREF and HREF point to the same tier (or both clearly below it): treat as a confidence *raise* (a Moderate from member spread alone may lift to High).
- **Divergence** — the ensembles materially disagree on the same-day flood/lightning tier: cap confidence at **Moderate at most**, and surface both signals in the briefing rather than hiding the disagreement (reference-only posture). HREF's known convective-scale underdispersion (§10 item 3a) means HREF agreement should *corroborate*, not by itself manufacture, High confidence.

This cross-ensemble rule is a **UpstreamWX proposal**, tuned with the member-support fractions through field testing.

---

## 17. Appendix C — Proposed Disclaimer and Acknowledgment Text (for review)

Written as plain product copy, not legalese, per your instruction. Three placements: a one-time first-run acknowledgment, a persistent footer on every briefing, and the same footer on every PDF export.

### 17.1 First-run acknowledgment (shown once; must tap to continue)

> **Before you use UpstreamWX — read this.**
>
> This tool gathers National Weather Service data, weather-model output, and ensemble probabilities and summarizes them for caving and canyoneering trips. It exists to save you time and to flag hazards worth a closer look.
>
> It does not tell you whether to go. It is not an official forecast or warning. The data can be incomplete, delayed, or wrong — and a slot canyon or cave can flood from rain you never see.
>
> Use it as one input. Check the official NWS sources it links to. In the field, watch the sky and the water: what you observe there beats anything on this screen. The decision to enter, continue, or turn around is always yours and your party's.
>
> **[ I understand — continue ]**

### 17.2 Persistent briefing footer (on screen, every briefing)

> Planning reference only — not a forecast, not a decision. Conditions change fast and models can be wrong. Verify against the official NWS sources linked above, and let what you see in the field overrule this briefing.

### 17.3 PDF export footer (every page or final block)

> UpstreamWX — planning reference only. Generated &lt;timestamp&gt; from NWS, Open-Meteo, and SREF data. Not an official forecast or warning. Verify against official NWS sources. The go/no-go decision is the user's and the party's.

### 17.4 Note to Chris (not user-facing)

The text above is intentionally plain and is product copy, not legal advice. The reference-only framing, the "decision is yours" language, and the source links all support the defensible posture. If you ever want a binding assumption-of-risk or waiver attached to account creation or a paid tier, that specific instrument is worth a lawyer's eye — but for a free, reference-only community tool, plain honest language like the above is the right register and arguably more effective than legalese, which people skip.

---

## 18. Appendix D — Screen Inventory and Layout (chrome from mockups, content per PRD)

The reference mockups contribute **visual chrome and layout only** (dark theme, hex-mark header, tabbed IA, metric cards, timeline Gantt, map waypoint cards). The hazard set, severity ladder, phase organization, watershed overlay, confidence rendering, and disclaimer below are governed by this PRD. Mockup elements with no PRD basis (alpine hazards such as high winds / icing / blowing snow, the Low/Moderate/Elevated/Extreme legend, "All Systems Go," radar layer, "Add as Alert," multi-waypoint routes) are intentionally absent in v1.

### 18.1 Global chrome
- Header: product mark + "UpstreamWX," mission title (editable), date and time window, and the **cave/canyon** indicator (FR-33).
- Overall posture summary (max across applicable hazards, FR-19) shown top-right as information, never as a recommendation (FR-39).
- Persistent reference-only disclaimer line visible on Overview (FR-40).
- Tab bar: Overview · Forecast · Map · Hazards · Resources (FR-32).

### 18.2 Overview (the BLUF)
- Overall posture + the four hazards, each as a one-line posture + confidence, windows of concern for any at Elevated+ (FR-22 item 1, FR-36).
- Plain-language mission summary (Haiku-framed, FR-21).
- Glanceable metric cards (temp / feels-like, wind, precip chance, etc.) as supporting context.
- Phase strip: approach → technical span → egress, with the phase-weighted thermal hazard leading each (FR-14b).

### 18.3 Forecast
- Hourly and daily numerical detail across the mission window: temperature and apparent temperature, wind and gusts, precip chance and amount, humidity.
- Charts for temperature/feels-like and wind/gusts. This view is the drill-down behind the hazard drivers (FR-22 item 3).

### 18.4 Map
- Single free-form mission point (FR-1).
- **Upstream contributing watershed overlay** (HUC-12 trace, FR-3, FR-38) — the defining flash-flood visual.
- Active NWS alert polygons; point conditions callout.
- No radar layer in v1 (FR-38).

### 18.5 Hazards (phase-primary)
- Primary axis = mission phase (approach / technical span / egress); secondary axis = wall-clock (FR-34).
- Per-hazard bars appear only in applicable phases per FR-14a; bar color = severity on the PRD ladder (FR-35); solid vs hatched = confidence (FR-36); full-period vs windowed = persistence (FR-37).
- Expandable hazard detail rows: drivers, explicit confidence label, and the relevant threshold logic (Appendix B), with the assumptions stated (wet-egress, cave isolation, slot shelter).

### 18.6 Resources
- Verify-against-NWS source links: AFD, active alerts, model/source provenance (FR-26, FR-40).
- PDF export of the current briefing (FR-27).
- Offline/cached-state indicator with generation timestamp (FR-41).
- "How this is calculated" reference (versioned threshold matrices, FR-20).
- First-run acknowledgment is shown once before first use (FR-31, Appendix C).
