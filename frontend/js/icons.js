/*
 * Inline SVG icons — no icon-font dependency (offline-safe), currentColor so they
 * inherit text tone. STYLE_GUIDE.md §8. Each returns an <svg> string at 24px grid,
 * 1.75 stroke. Call icon(name, className).
 */

const PATHS = {
  // hazard glyphs
  flash_flood:
    '<path d="M3 13c2 0 2 1.5 4.5 1.5S10 13 12 13s2 1.5 4.5 1.5S19 13 21 13"/><path d="M3 18c2 0 2 1.5 4.5 1.5S10 18 12 18s2 1.5 4.5 1.5S19 18 21 18"/><path d="M12 3l4 5h-3v4h-2V8H8z"/>',
  lightning: '<path d="M13 2L4 14h6l-1 8 9-12h-6z"/>',
  heat:
    '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  cold_wet:
    '<path d="M12 2v14"/><path d="M12 8l3-3M12 8L9 5M12 16l3 3M12 16l-3 3"/><path d="M5 12h14M5 12l3-2M5 12l3 2M19 12l-3-2M19 12l-3 2"/>',
  // tab icons
  overview: '<rect x="3" y="3" width="7" height="9" rx="1"/><rect x="14" y="3" width="7" height="5" rx="1"/><rect x="14" y="12" width="7" height="9" rx="1"/><rect x="3" y="16" width="7" height="5" rx="1"/>',
  forecast: '<path d="M3 17l5-5 4 3 6-7"/><path d="M3 21h18"/>',
  map: '<path d="M9 4L3 6v14l6-2 6 2 6-2V4l-6 2-6-2z"/><path d="M9 4v14M15 6v14"/>',
  hazards: '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  resources: '<path d="M4 5a2 2 0 012-2h9l5 5v11a2 2 0 01-2 2H6a2 2 0 01-2-2z"/><path d="M14 3v6h6"/>',
  // resource glyphs
  doc: '<path d="M5 3h9l5 5v13H5z"/><path d="M14 3v5h5M8 13h8M8 17h8"/>',
  alert: '<path d="M12 3l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
  model: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>',
  calc: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h2M8 15h2M14 11h2v6"/>',
  // chrome
  edit: '<path d="M4 20h4L18 9l-4-4L4 16z"/><path d="M14 5l4 4"/>',
  chevron: '<path d="M6 9l6 6 6-6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  arrow_left: '<path d="M15 5l-7 7 7 7"/>',
  book: '<path d="M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2z"/><path d="M19 17H6a2 2 0 00-2 2"/><path d="M8 7h7M8 11h7"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 13v6H5V6h6"/>',
  cave: '<path d="M3 21V12a9 9 0 0118 0v9h-5v-5a4 4 0 00-8 0v5z"/>',
  canyon: '<path d="M4 3v18M20 3v18"/><path d="M4 14c3 0 4-2 6-2s3 2 6 2"/><path d="M4 9c3 0 4-2 6-2s3 2 6 2"/>',
  wifi_off: '<path d="M2 8.8a16 16 0 0120 0"/><path d="M5 12.5a11 11 0 0114 0"/><path d="M8.5 16a6 6 0 017 0"/><path d="M12 20h.01"/><path d="M2 2l20 20"/>',
  pin: '<path d="M12 21s7-6.5 7-12a7 7 0 10-14 0c0 5.5 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/>',
  reload: '<path d="M21 12a9 9 0 01-15.2 6.6"/><path d="M3 12a9 9 0 0115.2-6.6"/><polyline points="21 3 21 9 15 9"/><polyline points="3 21 3 15 9 15"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 11-4 0v-.09A1.65 1.65 0 008 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H2a2 2 0 110-4h.09A1.65 1.65 0 004.6 8a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06a1.65 1.65 0 001.82.33H9a1.65 1.65 0 001-1.51V2a2 2 0 114 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82V9a1.65 1.65 0 001.51 1H22a2 2 0 110 4h-.09a1.65 1.65 0 00-1.51 1z"/>',
};

export function icon(name, className = "") {
  const body = PATHS[name] || "";
  return `<svg class="${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

export const HAZARD_LABELS = {
  flash_flood: "Flash flood",
  lightning: "Lightning",
  heat: "Heat",
  cold_wet: "Cold / wet",
};

export const PHASE_LABELS = {
  approach: "Approach",
  technical: "Technical",
  egress: "Egress",
};
