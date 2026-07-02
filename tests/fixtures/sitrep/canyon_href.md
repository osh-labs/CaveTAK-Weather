# EXPEDITION BRIEFING

**Mission:** Buckskin Gulch  |  **Type:** Canyon  |  **Window:** 2026-06-20 08:00–2026-06-20 18:00
**Location:** 37.0192, -111.9889  |  **Upstream domain:** HUC-12 150100021304 (+3 upstream, 842 km²)

## BLUF

**OVERALL POSTURE: High**  ·  Confidence: Moderate

| Hazard | Posture | Confidence | Window of concern |
|---|---|---|---|
| Flash flood | High | Moderate | 2026-06-20 09:00–2026-06-20 17:00 |
| Lightning | High | Moderate | 2026-06-20 08:00–2026-06-20 09:00 |
| Heat | Extreme Caution | Moderate | 2026-06-20 08:00–2026-06-20 09:00 |
| Cold/wet (assumes wet egress) | Minimal | Moderate | — |

## PHASE BREAKDOWN

### Approach (2026-06-20 08:00–2026-06-20 09:00)
- Lightning: High
- Heat: Extreme Caution (primary)
- Cold/wet: Minimal

### Technical span (2026-06-20 09:00–2026-06-20 17:00)
- Flash flood: High
- Heat: Extreme Caution (primary)
- Cold/wet: Minimal

### Egress (2026-06-20 17:00–2026-06-20 18:00)
- Lightning: High
- Cold/wet: Minimal (primary)
- Heat: Extreme Caution

## KEY DRIVERS (per active hazard)

### Flash flood
- GEFS P(precip/thunder) 65% ≥ 60% over upstream domain
- REFS neighborhood P(QPF) 45% concurs at High
- DATA GAP: forecast convective rate unavailable — the conservative slot-canyon fallback could not be evaluated.

### Lightning
- GEFS P(tstm) 50% ≥ 40%
- SPC slight risk over window
- AFD: scattered convection
- REFS neighborhood P(convection) 35% ≥ 20% (~3 km same-day)
- CAPE 1200 J/kg (moderate instability) — context only.

### Heat
- Heat index 95 °F → Extreme Caution
- Approach involves exertion under load: effective heat strain runs one category hotter than the ambient heat index suggests.

### Cold/wet
- Apparent temperature 92 °F (wet-party basis)
- Assumes a wet party on egress.

## UPSTREAM WATERSHED SUMMARY

Flash-flood assessment aggregates over the upstream contributing watershed of HUC-12 150100021304: 3 upstream HUC-12 unit(s), ~842 km², traced via tohuc-graph.

## SOURCE DATA

Threshold config: flash_flood=1.3.0;lightning=1.5.0;heat=1.0.0;cold_wet=1.0.0;confidence=1.2.0

Active NWS products:
- Flash Flood Warning: no
- Flash Flood Watch: no
- Flood Warning: no
- Flood Advisory: no
- Flood Watch: no
- Thunderstorm Warning: no
- AFD storm mode: scattered
- AFD excessive-rain / flood mention: no
- SPC outlook: slight

GEFS ensemble (upstream domain, cycle n/a):
- P(precip/thunder): 65%
- P(thunderstorm): 50%
- Convective rate: n/a
- CAPE: 1200 J/kg

REFS same-day supplement (cycle 20260620/12Z f09-f16):
- Neighborhood P(QPF): 45%
- Neighborhood P(lightning): 35%

Cross-ensemble agreement: partial

Derived fields (Open-Meteo):
- Heat index: 95 °F
- Apparent temp: 92 °F
- Wind: 8 mph
- Measurable window precip: yes
- Antecedent precip (24–72 h): unknown (data unavailable)

Source availability:
- openmeteo: ok

## NOTES
- Phases inferred from the overall window: approach = first hour, egress = last hour, technical span = everything in between (FR-9a).

## SOURCES (verify)

- NWS active alerts: https://api.weather.gov/alerts/active?point=37.0192,-111.9889
- NWS point forecast / AFD: https://forecast.weather.gov/MapClick.php?lat=37.0192&lon=-111.9889
- Model source (GEFS): https://www.nco.ncep.noaa.gov/pmb/products/gens
- Model source (REFS, same-day): https://www.nco.ncep.noaa.gov/pmb/products/refs

## DISCLAIMER

Planning reference only — not a forecast, not a decision. Conditions change fast and models can be wrong. Verify against the official NWS sources linked above, and let what you see in the field overrule this briefing. The go/no-go decision is yours and your party's.
