/* SB 1434 Scanner — frontend logic.
 *
 * Zero-build vanilla JS. Fetches from /qualifying-parcels, renders one
 * Leaflet CircleMarker per row, wires filter controls, and shows a
 * per-parcel detail popup.
 *
 * API base URL resolution order:
 *   1. ?api=https://... on the current URL
 *   2. window.SB1434_API set by an inline <script> before app.js loads
 *   3. Default: production Render service
 */

(() => {
  "use strict";

  // ------------------------------------------------------------------
  // Config
  // ------------------------------------------------------------------

  const DEFAULT_API = "https://sb1434-scanner-api.onrender.com";
  const params = new URLSearchParams(window.location.search);
  const API_BASE = (
    params.get("api") ||
    window.SB1434_API ||
    DEFAULT_API
  ).replace(/\/$/, "");

  const CENTER = [26.1, -80.2];
  const ZOOM = 10;
  const PARCEL_LIMIT = 5000;

  // Pathway → color palette + label + short (for the floating map legend)
  // + list-card blurb explaining the redev angle + a one-liner used in the
  // map popup.
  const PATHWAY_STYLE = {
    pathway_1_golf_ringed: {
      color: "#d93b3b", label: "1 · Golf, ringed", short: "Golf (ringed)",
      one_liner: "Golf course ringed by SF residential — overlay applies, density constrained.",
      angle: "Overlay applies — density constrained. Look for adjoining out-parcels or reposition play (banquet, wellness, resort) instead of full residential conversion.",
    },
    pathway_1b_golf_partial: {
      color: "#ee7d3c", label: "1B · Golf, partial", short: "Golf (partial)",
      one_liner: "Golf course partially ringed by SF residential — overlay likely applies, verify boundary.",
      angle: "Overlay likely applies. Order a boundary polygon and re-run the ring test manually — a course that classifies 'partial' at 12 samples may swing to 'not ringed' at higher resolution.",
    },
    pathway_2_golf_not_ringed: {
      color: "#f2b731", label: "2 · Golf, not ringed", short: "Golf (not ringed) ★", star: true,
      one_liner: "Golf course without SF residential ring — no overlay constraints, full density flexibility.",
      angle: "★ TOP DEAL. No overlay — full density flexibility. Combine SB 1434 unlock with existing golf-course residential entitlements for a high-yield conversion.",
    },
    pathway_3_industrial: {
      color: "#5b6a7e", label: "3 · Industrial", short: "Industrial",
      one_liner: "Industrial site on contaminated land — environmental remediation + redevelopment play.",
      angle: "Classic brownfield-to-mixed-use. Environmental remediation cost is the primary underwriting question — request Phase II ESA before LOI.",
    },
    pathway_4_commercial: {
      color: "#2f6fd9", label: "4 · Commercial", short: "Commercial",
      one_liner: "Aging retail on brownfield-adjacent land — infill conversion opportunity.",
      angle: "Retail apocalypse supply meets residential demand. Strip centers and struggling big-box on brownfield-adjacent land.",
    },
    pathway_5_office: {
      color: "#4682b4", label: "5 · Office", short: "Office",
      one_liner: "Underperforming office on qualifying land — teardown-to-rental play.",
      angle: "Post-COVID vacancy + SB 1434 unlock = teardown-to-rental play. Verify current occupancy and lease tails.",
    },
    pathway_6_institutional: {
      color: "#7b3f9f", label: "6 · Institutional", short: "Institutional",
      one_liner: "Institutional site (school, church, hospital) — patient-capital acquisition.",
      angle: "Long-tenured owners (churches, schools, hospitals). Patient-capital deals — sellers often need seller-financing or long closes.",
    },
    pathway_7_residential_redev: {
      color: "#3ea55c", label: "7 · Residential redev", short: "Residential redev",
      one_liner: "Existing residential 5+ acres — mobile-home park or garden apartment densification.",
      angle: "Mobile-home parks and aging garden apartments. Existing entitlement plus SB 1434 unlock supports densification — mind tenant displacement rules.",
    },
    pathway_8_utility: {
      color: "#8b5a2b", label: "8 · Utility", short: "Utility",
      one_liner: "Utility-owned parcel — requires 15-year title lookback before proceeding.",
      angle: "⚠ 15-YEAR TITLE LOOKBACK required before you can rely on qualification. Any prior utility ownership disqualifies under §163.2525(4)(e).",
    },
    pathway_9_auto_fuel: {
      color: "#c15a12", label: "9 · Auto/fuel", short: "Auto/fuel",
      one_liner: "Auto/fuel site — near-certain petroleum contamination, cleanup cost IS the deal.",
      angle: "Near-certain petroleum contamination. Cleanup cost IS the deal — Phase II ESA and remediation estimate before LOI.",
    },
    pathway_10_hospitality: {
      color: "#c62d8b", label: "10 · Hospitality", short: "Hospitality",
      one_liner: "Aging hotel/motel on qualifying land — teardown for higher-yield use.",
      angle: "Older hotels/motels — often historic dry-cleaner exposure on-site. Soft ADR at aging properties makes these attractive teardowns.",
    },
    pathway_11_vacant_commercial: {
      color: "#1fa39a", label: "11 · Vacant commercial", short: "Vacant commercial",
      one_liner: "Vacant commercial in brownfield/cleanup zone — least-encumbered starting point.",
      angle: "Least-encumbered path — no tenants to relocate. Confirm no active enforcement action on the site before proceeding.",
    },
    pathway_12_mixed_use: {
      color: "#4b3fa6", label: "12 · Mixed use", short: "Mixed use",
      one_liner: "Existing mixed-use — intensification via SB 1434 unlock.",
      angle: "Existing mixed-use eligible for intensification. Check current FAR utilization vs. what SB 1434 unlocks.",
    },
    pathway_13_other: {
      color: "#a4abb9", label: "13 · Other", short: "Other",
      one_liner: "Passed all five gates but doesn't match a specific pathway — review individually.",
      angle: "Uncategorized — eyeball the DOR use code and current use before deciding whether to pursue.",
    },
    pathway_golf_pending: {
      color: "#c9c9c9", label: "Golf · pending ring test", short: "Golf · pending",
      one_liner: "Golf course — ring test hasn't run yet.",
      angle: "Ring test hasn't run yet. Hit POST /admin/run-ring-test to classify.",
    },
  };

  // County-specific property-appraiser links. Direct URLs where the site
  // supports deep-linking; search pages otherwise (user still gets to the
  // right parcel in one more click).
  const PA_URLS = {
    "23": {
      label: "Miami-Dade Property Appraiser",
      url: "https://www.miamidade.gov/pa/property_search.asp",
    },
    "16": {
      label: "Broward County Property Appraiser",
      url: "https://web.bcpa.net/BcpaClient/#/Record-Search",
    },
    "60": {
      label: "Palm Beach County Property Appraiser",
      url: "https://www.pbcgov.org/papa/",
    },
  };

  const COUNTY_NAMES = { "23": "Miami-Dade", "16": "Broward", "60": "Palm Beach" };

  // ------------------------------------------------------------------
  // DOM refs
  // ------------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const els = {
    map: $("map"),
    loading: $("loading"),
    toast: $("toast"),
    apiBadge: $("api-badge"),
    kpiCount: $("kpi-count"),
    kpiAcres: $("kpi-acres"),
    kpiAdjacent: $("kpi-adjacent"),
    kpiGolf: $("kpi-golf"),
    fCounty: $("f-county"),
    fPathway: $("f-pathway"),
    fEnv: $("f-env"),
    fRing: $("f-ring"),
    fUdb: $("f-udb"),
    fMinAcres: $("f-min-acres"),
    fReset: $("f-reset"),
    fSearch: $("f-search"),
    fSearchClear: $("f-search-clear"),
    listContainer: $("list-container"),
    listCount: $("list-count"),
    listEmpty: $("list-empty"),
    welcomeBanner: $("welcome-banner"),
    welcomeDismiss: $("welcome-dismiss"),
    welcomeIntelLink: $("welcome-intel-link"),
    learnLink: $("learn-link"),
  };

  // Most-recent fetched rows (server-filtered). The search box then does
  // a client-side substring match on top of these; visibleRows is what
  // both the map + list render from.
  let lastRows = [];
  let searchQuery = "";
  const WELCOME_STORAGE_KEY = "sb1434.welcomeDismissed.v1";

  // ------------------------------------------------------------------
  // Map
  // ------------------------------------------------------------------

  const map = L.map("map", { preferCanvas: true }).setView(CENTER, ZOOM);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);

  // All parcel markers live in a single layer group so we can clear+re-add
  // on every filter change without touching the tile layer.
  let markerGroup = L.layerGroup().addTo(map);

  // ------------------------------------------------------------------
  // Marker rendering
  // ------------------------------------------------------------------

  function radiusForAcres(acres) {
    const a = Math.max(0, Number(acres) || 0);
    // Scale by sqrt(acres) so a 400-acre course looks 4x a 25-acre parcel.
    const r = Math.sqrt(a);
    return Math.max(5, Math.min(20, r));
  }

  function styleForPathway(pathway) {
    return PATHWAY_STYLE[pathway] || PATHWAY_STYLE.pathway_13_other;
  }

  function makeMarker(parcel) {
    const style = styleForPathway(parcel.pathway_hint);
    const isTopDeal = parcel.pathway_hint === "pathway_2_golf_not_ringed";
    const baseRadius = radiusForAcres(parcel.acres);
    // Top deals get 30% bigger so the "gold" markers stand out.
    const radius = isTopDeal ? baseRadius * 1.3 : baseRadius;

    const marker = L.circleMarker(
      [parcel.latitude, parcel.longitude],
      {
        radius,
        color: "#1a1e26",
        weight: isTopDeal ? 2 : 1,
        opacity: 1,
        fillColor: style.color,
        fillOpacity: 0.82,
      }
    );

    marker.bindPopup(() => renderPopup(parcel), {
      maxWidth: 340,
      className: "sb1434-popup",
    });

    return marker;
  }

  function renderPopup(p) {
    const county = COUNTY_NAMES[p.county_fips] || p.county_fips || "—";
    const style = styleForPathway(p.pathway_hint);
    const addr = addressString(p);
    const parts = [];

    parts.push('<div class="popup">');

    // Header: owner name (fallback), then acres/county line, then a
    // prominent parcel-id chip with Copy button — the ID is what a user
    // needs to paste into the county property-appraiser search.
    parts.push(
      `<h3>${esc(p.own_name || "(no owner listed)")}</h3>`,
      `<p class="owner">${fmtNum(p.acres, 2)} acres · ${esc(county)}${p.dor_uc ? ` · DOR ${esc(p.dor_uc)}` : ""}</p>`,
    );
    parts.push(
      '<div class="parcel-row">',
      `<code class="parcel-id">${esc(p.parcel_id || "—")}</code>`,
      `<button class="copy-btn" data-copy="${esc(p.parcel_id || "")}" type="button">Copy</button>`,
      "</div>",
    );
    if (!addr) {
      parts.push('<p class="no-addr">No address on file — search by parcel ID.</p>');
    } else {
      parts.push(`<p class="owner" style="margin-top:-4px">${esc(addr)}</p>`);
    }

    // Pathway pill
    parts.push('<div class="badges">');
    parts.push(`<span class="badge" style="background:${style.color}22;border-color:${style.color}">${esc(style.label)}</span>`);
    parts.push("</div>");

    // Redevelopment angle — one-liner
    if (style.one_liner) {
      parts.push('<span class="section-h">Redevelopment angle</span>');
      parts.push(`<div class="angle">${esc(style.one_liner)}</div>`);
    }

    // Why it qualifies
    parts.push('<div class="why">');
    parts.push('<span class="section-h">Why it qualifies</span><ul>');
    for (const r of qualifyingReasons(p)) {
      parts.push(`<li class="r-${r.kind}">${esc(r.text)}</li>`);
    }
    parts.push("</ul></div>");

    // Next steps
    const steps = nextStepsFor(p);
    if (steps.length) {
      parts.push('<div class="steps">');
      parts.push('<span class="section-h">Next steps</span><ul>');
      for (const s of steps) parts.push(`<li>${esc(s)}</li>`);
      parts.push("</ul></div>");
    }

    // Action links
    const actions = [];
    actions.push(
      `<a class="primary" href="${googleSearchUrl(p)}" target="_blank" rel="noopener">Search Google</a>`,
    );
    const aer = aerialUrl(p);
    if (aer) actions.push(`<a href="${aer}" target="_blank" rel="noopener">Aerial view</a>`);
    const pa = paLink(p);
    if (pa) actions.push(`<a href="${pa.url}" target="_blank" rel="noopener" title="${esc(pa.label)}">County Appraiser</a>`);
    parts.push(`<div class="actions">${actions.join("")}</div>`);

    parts.push("</div>");
    return parts.join("");
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtNum(v, decimals = 0) {
    if (v == null || Number.isNaN(Number(v))) return "—";
    return Number(v).toLocaleString(undefined, {
      maximumFractionDigits: decimals,
      minimumFractionDigits: decimals ? Math.min(decimals, 2) : 0,
    });
  }

  // ------------------------------------------------------------------
  // Filters + fetch
  // ------------------------------------------------------------------

  function currentFilters() {
    const q = new URLSearchParams();
    q.set("limit", String(PARCEL_LIMIT));
    const map = {
      county: els.fCounty.value,
      pathway: els.fPathway.value,
      env_trigger: els.fEnv.value,
      ring_test_result: els.fRing.value,
      udb_status: els.fUdb.value,
    };
    for (const [k, v] of Object.entries(map)) if (v) q.set(k, v);
    const minAcres = els.fMinAcres.value.trim();
    if (minAcres !== "") q.set("min_acres", minAcres);
    return q;
  }

  let inflight = null;

  async function loadParcels() {
    if (inflight) inflight.abort();
    inflight = new AbortController();
    const q = currentFilters();
    const url = `${API_BASE}/qualifying-parcels?${q.toString()}`;
    showLoading(true);
    try {
      const r = await fetch(url, { signal: inflight.signal });
      if (!r.ok) {
        const detail = await r.text().catch(() => "");
        throw new Error(`HTTP ${r.status} · ${detail.slice(0, 200)}`);
      }
      const rows = await r.json();
      renderParcels(rows);
      hideToast();
    } catch (e) {
      if (e.name === "AbortError") return;
      console.error("loadParcels failed:", e);
      showToast(`Failed to load parcels: ${e.message}`);
      renderParcels([]);
    } finally {
      showLoading(false);
      inflight = null;
    }
  }

  function renderParcels(rows) {
    lastRows = rows;
    rerender();
  }

  // Client-side search filter applied on top of the server-side filters.
  // Substring match, case-insensitive, on parcel_id + owner name.
  function applySearch(rows) {
    const q = searchQuery.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((p) => {
      const pid = String(p.parcel_id || "").toLowerCase();
      const owner = String(p.own_name || "").toLowerCase();
      return pid.includes(q) || owner.includes(q);
    });
  }

  function rerender() {
    const rows = applySearch(lastRows);
    markerGroup.clearLayers();
    let totalAcres = 0;
    let adjacentCount = 0;
    let golfCount = 0;
    for (const p of rows) {
      if (p.latitude == null || p.longitude == null) continue;
      markerGroup.addLayer(makeMarker(p));
      totalAcres += Number(p.acres) || 0;
      if (p.adjacent_residential) adjacentCount += 1;
      if (p.pathway_hint === "pathway_2_golf_not_ringed") golfCount += 1;
    }
    els.kpiCount.textContent = fmtNum(rows.length);
    els.kpiAcres.textContent = fmtNum(totalAcres, 0);
    els.kpiAdjacent.textContent = fmtNum(adjacentCount);
    if (els.kpiGolf) els.kpiGolf.textContent = fmtNum(golfCount);
    renderList(rows);

    // Search-affordance: if exactly one row matches, zoom to it and pop.
    if (searchQuery.trim() && rows.length === 1 && rows[0].latitude != null) {
      const p = rows[0];
      map.setView([p.latitude, p.longitude], 16, { animate: true });
      // Give Leaflet a beat to finish the zoom before opening the popup
      // (openPopup during a pan can race).
      setTimeout(() => {
        const marker = findMarkerFor(p);
        if (marker) marker.openPopup();
      }, 250);
    }
  }

  function findMarkerFor(parcel) {
    let hit = null;
    markerGroup.eachLayer((layer) => {
      const ll = layer.getLatLng ? layer.getLatLng() : null;
      if (!ll) return;
      if (Math.abs(ll.lat - parcel.latitude) < 1e-6 &&
          Math.abs(ll.lng - parcel.longitude) < 1e-6) {
        hit = layer;
      }
    });
    return hit;
  }

  // ------------------------------------------------------------------
  // List view
  // ------------------------------------------------------------------

  function renderList(rows) {
    els.listCount.textContent = fmtNum(rows.length);
    els.listEmpty.classList.toggle("hidden", rows.length > 0);
    // Build the entire DOM once, then swap in — avoids reflow-per-card.
    const frag = document.createDocumentFragment();
    for (const p of rows) frag.appendChild(makeCard(p));
    els.listContainer.replaceChildren(frag);
  }

  function addressString(p) {
    const addr = String(p.phy_addr1 || "").trim();
    const city = String(p.phy_city || "").trim();
    const zip = String(p.phy_zipcd || "").trim();
    if (!addr && !city && !zip) return null;
    const parts = [];
    if (addr) parts.push(addr);
    if (city) parts.push(city);
    if (city || zip) parts.push("FL");
    if (zip) parts.push(zip);
    return parts.join(", ");
  }

  function googleSearchUrl(p) {
    const addr = addressString(p);
    const q = addr
      ? `${addr} ${p.own_name || ""}`.trim()
      : `${p.parcel_id || ""} ${COUNTY_NAMES[p.county_fips] || ""} FL property appraiser`;
    return `https://www.google.com/search?q=${encodeURIComponent(q)}`;
  }

  function aerialUrl(p) {
    if (p.latitude == null || p.longitude == null) return null;
    return `https://www.google.com/maps/@${p.latitude},${p.longitude},17z/data=!3m1!1e1`;
  }

  function paLink(p) {
    return PA_URLS[p.county_fips] || null;
  }

  function qualifyingReasons(p) {
    const reasons = [];

    // Acreage — every qualifying parcel is ≥ 5 acres, but showing the
    // actual number here anchors the popup's "what am I looking at" answer.
    if (p.acres != null) {
      reasons.push({
        kind: "ok",
        text: `${fmtNum(p.acres, 1)} acres (Gate 1 · minimum 5 required)`,
      });
    }

    // Gate 3 — environmental trigger
    if (p.env_trigger === "brownfield_area" || p.env_trigger === "both") {
      const name = p.brownfield_area_name ? ` "${p.brownfield_area_name}"` : "";
      reasons.push({
        kind: "ok",
        text: `Inside FDEP brownfield area${name} (Trigger B)`,
      });
    }
    if (p.env_trigger === "cleanup_site" || p.env_trigger === "both") {
      reasons.push({
        kind: "ok",
        text: "Within 1,500 ft of a DEP cleanup site (Trigger A)",
      });
    }

    // Gate 4 — adjacency. When we don't detect a residential neighbor in
    // the DOR data, the parcel STILL qualifies (screening only surfaces
    // rows that passed all five statutory gates at run time); the missing
    // signal is a data-quality caveat, not a disqualification. Show it as
    // a neutral note (blue), not an orange warning.
    if (p.adjacent_residential) {
      reasons.push({
        kind: "ok",
        text: "Residential parcel within 500 ft (Gate 4 ✓)",
      });
    } else {
      reasons.push({
        kind: "note",
        text:
          "Residential adjacency not detected in DOR data. This parcel " +
          "still qualifies under all five statutory gates — verify adjacency " +
          "via aerial view. DOR records may not capture every residential use.",
      });
    }

    // Gate 5C — Miami-Dade UDB
    if (p.udb_status) {
      const inside = p.udb_status === "inside";
      reasons.push({
        kind: inside ? "ok" : "info",
        text: inside
          ? "Inside Miami-Dade Urban Development Boundary"
          : "Outside Miami-Dade UDB — expect additional entitlement friction",
      });
    }

    // Ring test detail for golf
    if (p.ring_test_result) {
      const pct = p.ring_test_pct != null ? `${Math.round(p.ring_test_pct)}%` : "?";
      const label =
        p.ring_test_result === "not_ringed"
          ? `Ring test ${pct} → NOT ringed — overlay does not apply`
          : p.ring_test_result === "ringed"
          ? `Ring test ${pct} → ringed — overlay applies`
          : `Ring test ${pct} → partially ringed — review`;
      reasons.push({
        kind: p.ring_test_result === "not_ringed" ? "ok" : "info",
        text: label,
      });
    }

    // Utility flag warning (real warning — this one is actionable).
    if (p.utility_flag) {
      reasons.push({
        kind: "warn",
        text: "Utility flag set — 15-year title lookback REQUIRED before proceeding",
      });
    }

    return reasons;
  }

  function nextStepsFor(p) {
    const steps = [];
    if (p.utility_flag) {
      steps.push("Order a 15-year title history to confirm prior utility use — parcels ever owned by a public utility are excluded under §163.2525(4)(e).");
    }
    if (p.ring_test_result === "partially_ringed") {
      const pct = p.ring_test_pct != null ? Math.round(p.ring_test_pct) : null;
      steps.push(
        `Ring test is borderline${pct != null ? ` (${pct}%)` : ""} — get a boundary survey and re-run the perimeter analysis before treating this as a Pathway 1B.`,
      );
    }
    if (!p.adjacent_residential) {
      steps.push("Verify residential adjacency manually via aerial view — DOR records may not capture every residential use.");
    }
    if (p.env_trigger === "cleanup_site" || p.env_trigger === "both") {
      steps.push("Confirm cleanup-site status and contamination scope with FDEP before advancing — Trigger A relies on DEP records that may need a Phase II ESA to substantiate.");
    }
    if (p.udb_status === "outside") {
      steps.push("Parcel sits outside Miami-Dade's Urban Development Boundary — expect additional entitlement hurdles at the county comp-plan level.");
    }
    if (p.pathway_hint === "pathway_9_auto_fuel") {
      steps.push("Auto/fuel site — expect petroleum contamination. Get a Phase II ESA and a remediation estimate before LOI.");
    }
    if (p.pathway_hint === "pathway_2_golf_not_ringed") {
      steps.push("Top-tier golf opportunity — no SB 1434 overlay applies. Explore whether existing golf-course residential entitlements can be layered onto the unlock.");
    }
    // Always the last step — the visual sanity check every deal deserves.
    steps.push("Review aerial imagery before investing more time (link in the Actions bar below).");
    return steps;
  }

  function makeCard(p) {
    const style = styleForPathway(p.pathway_hint);
    const county = COUNTY_NAMES[p.county_fips] || p.county_fips || "—";
    const addr = addressString(p);
    const isTopDeal = p.pathway_hint === "pathway_2_golf_not_ringed";

    const card = document.createElement("article");
    card.className = "card" + (isTopDeal ? " card-top" : "");
    card.style.borderLeftColor = style.color;

    const parts = [];

    // Header row: pathway pill + acres/county
    parts.push('<div class="card-head">');
    parts.push(
      `<span class="pathway-pill" style="background:${style.color}1a;color:${style.color};border-color:${style.color}66">`,
      `<span class="pathway-dot${style.star ? " star" : ""}" style="background:${style.color}"></span>`,
      `${esc(style.label)}`,
      `</span>`,
    );
    parts.push(`<span class="card-meta">${fmtNum(p.acres, 1)} acres · ${esc(county)}</span>`);
    parts.push("</div>");

    // Identity
    parts.push(`<h3 class="card-title">${esc(p.own_name || "(no owner listed)")}</h3>`);
    const idLine = [
      `<span class="mono">${esc(p.parcel_id || "—")}</span>`,
      p.dor_uc ? `DOR ${esc(p.dor_uc)}` : null,
      addr ? esc(addr) : '<span class="no-addr">No address on file</span>',
    ].filter(Boolean).join(" · ");
    parts.push(`<p class="card-id">${idLine}</p>`);

    // Why it qualifies
    parts.push('<div class="card-why"><span class="section-h">Why it qualifies</span><ul>');
    for (const r of qualifyingReasons(p)) {
      parts.push(`<li class="r-${r.kind}">${esc(r.text)}</li>`);
    }
    parts.push("</ul></div>");

    // Redevelopment angle
    if (style.angle) {
      parts.push(
        '<div class="card-angle">',
        '<span class="section-h">Redevelopment angle</span>',
        `<p>${esc(style.angle)}</p>`,
        "</div>",
      );
    }

    // Actions
    const actions = [];
    actions.push(
      `<a class="btn primary" href="${googleSearchUrl(p)}" target="_blank" rel="noopener">Search Google</a>`,
    );
    const aer = aerialUrl(p);
    if (aer) actions.push(`<a class="btn" href="${aer}" target="_blank" rel="noopener">Aerial view</a>`);
    const pa = paLink(p);
    if (pa) {
      actions.push(
        `<a class="btn" href="${pa.url}" target="_blank" rel="noopener" title="${esc(pa.label)}">County Appraiser</a>`,
      );
    }
    actions.push(
      `<button class="btn copy-btn" type="button" data-copy="${esc(p.parcel_id || "")}">Copy Parcel ID</button>`,
    );
    parts.push(`<div class="card-actions">${actions.join("")}</div>`);

    card.innerHTML = parts.join("");

    // Whole card is a click-target — outside links + copy button — opens
    // the Google search in a new tab (matches "click a card → Google").
    card.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      window.open(googleSearchUrl(p), "_blank", "noopener");
    });

    return card;
  }

  // ------------------------------------------------------------------
  // UI wiring
  // ------------------------------------------------------------------

  function debounce(fn, ms) {
    let t;
    return (...args) => {
      clearTimeout(t);
      t = setTimeout(() => fn(...args), ms);
    };
  }

  function initFilters() {
    const rerun = () => loadParcels();
    ["fCounty", "fPathway", "fEnv", "fRing", "fUdb"].forEach((k) => {
      els[k].addEventListener("change", rerun);
    });
    els.fMinAcres.addEventListener("input", debounce(rerun, 350));
    els.fReset.addEventListener("click", () => {
      els.fCounty.value = "";
      els.fPathway.value = "";
      els.fEnv.value = "";
      els.fRing.value = "";
      els.fUdb.value = "";
      els.fMinAcres.value = "";
      // Clear the search too — reset means reset EVERYTHING.
      if (els.fSearch) {
        els.fSearch.value = "";
        searchQuery = "";
        if (els.fSearchClear) els.fSearchClear.hidden = true;
      }
      // Recenter the map to the tri-county default so a user who zoomed
      // in on one result via the search box gets a clean slate.
      map.setView(CENTER, ZOOM);
      loadParcels();
    });

    // Golf Opportunities KPI acts as a shortcut to the Pathway 2 filter.
    els.kpiGolf?.parentElement?.addEventListener("click", () => {
      els.fPathway.value = "pathway_2_golf_not_ringed";
      loadParcels();
    });
  }

  // Floating map legend as a Leaflet control (bottom-right, collapsible).
  function initLegend() {
    const legendControl = L.control({ position: "bottomright" });
    legendControl.onAdd = () => {
      const div = L.DomUtil.create("div", "map-legend");
      const rows = Object.entries(PATHWAY_STYLE)
        .map(
          ([key, style]) => `
            <li data-pathway="${key}">
              <span class="swatch${style.star ? " star" : ""}" style="background:${style.color}"></span>
              <span>${esc(style.short || style.label)}</span>
            </li>`,
        )
        .join("");
      div.innerHTML = `
        <div class="map-legend-head">
          <span>Pathway legend</span>
          <span class="map-legend-caret">▾</span>
        </div>
        <ul class="map-legend-list">${rows}</ul>
      `;
      // Clicks/scrolls inside the legend shouldn't pan the map.
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      // Header toggles collapse
      div.querySelector(".map-legend-head").addEventListener("click", () => {
        div.classList.toggle("collapsed");
      });
      // Item click filters by that pathway
      div.querySelectorAll(".map-legend-list li").forEach((li) => {
        li.addEventListener("click", (e) => {
          e.stopPropagation();
          els.fPathway.value = li.dataset.pathway;
          loadParcels();
        });
      });
      return div;
    };
    legendControl.addTo(map);
  }

  // ------------------------------------------------------------------
  // Welcome banner (localStorage-backed dismissal)
  // ------------------------------------------------------------------

  function initWelcomeBanner() {
    if (!els.welcomeBanner) return;
    let dismissed = false;
    try {
      dismissed = localStorage.getItem(WELCOME_STORAGE_KEY) === "1";
    } catch { /* private-mode / storage disabled — ignore */ }
    els.welcomeBanner.hidden = dismissed;

    els.welcomeDismiss?.addEventListener("click", () => {
      els.welcomeBanner.hidden = true;
      try { localStorage.setItem(WELCOME_STORAGE_KEY, "1"); } catch {}
    });
    els.welcomeIntelLink?.addEventListener("click", (e) => {
      e.preventDefault();
      switchToView("intel");
    });
  }

  // ------------------------------------------------------------------
  // Search (client-side substring match on lastRows)
  // ------------------------------------------------------------------

  function initSearch() {
    const onInput = debounce(() => {
      searchQuery = els.fSearch.value || "";
      els.fSearchClear.hidden = !searchQuery;
      rerender();
    }, 200);
    els.fSearch.addEventListener("input", onInput);
    els.fSearch.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        els.fSearch.value = "";
        onInput();
      }
    });
    els.fSearchClear.addEventListener("click", () => {
      els.fSearch.value = "";
      searchQuery = "";
      els.fSearchClear.hidden = true;
      rerender();
      els.fSearch.focus();
    });
  }

  // ------------------------------------------------------------------
  // Copy-parcel-ID buttons (both popups + list cards, delegated globally)
  // ------------------------------------------------------------------

  function initCopyDelegation() {
    document.addEventListener("click", (e) => {
      const btn = e.target.closest(".copy-btn");
      if (!btn) return;
      const val = btn.dataset.copy || "";
      if (!val) return;
      e.stopPropagation();
      const done = () => {
        const prev = btn.textContent;
        btn.classList.add("copied");
        btn.textContent = "Copied ✓";
        setTimeout(() => {
          btn.classList.remove("copied");
          btn.textContent = prev;
        }, 1200);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(val).then(done, done);
      } else {
        // Fallback for browsers/insecure origins without the Clipboard API.
        const ta = document.createElement("textarea");
        ta.value = val;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand("copy"); } catch {}
        document.body.removeChild(ta);
        done();
      }
    });
  }

  function initApiBadge() {
    // Show host only — the full URL is noisy for a badge.
    try {
      const u = new URL(API_BASE);
      els.apiBadge.textContent = `API: ${u.host}`;
      els.apiBadge.title = API_BASE;
    } catch {
      els.apiBadge.textContent = `API: ${API_BASE}`;
    }
  }

  function showLoading(on) {
    els.loading.classList.toggle("hidden", !on);
  }
  function showToast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.remove("hidden");
  }
  function hideToast() {
    els.toast.classList.add("hidden");
  }

  // ------------------------------------------------------------------
  // View toggle (Map / Intel)
  // ------------------------------------------------------------------

  // View-tab state is factored out so other places (welcome banner link,
  // "Learn how this works" button in the sidebar) can jump to a specific view.
  function switchToView(target) {
    document.querySelectorAll(".view-btn").forEach((b) => {
      const active = b.dataset.view === target;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    for (const key of ["map", "list", "intel"]) {
      const el = document.getElementById(`view-${key}`);
      if (!el) continue;
      const active = key === target;
      el.classList.toggle("active", active);
      el.hidden = !active;
    }
    // Leaflet needs to recompute its container size when the map pane
    // comes back from display:none.
    if (target === "map") map.invalidateSize();
  }

  function expandAllIntelSections() {
    document.querySelectorAll(".intel-section").forEach((s) => { s.open = true; });
  }

  function initViewTabs() {
    document.querySelectorAll(".view-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchToView(btn.dataset.view));
    });
    // The "Learn how this works" chip is an onboarding path for a first-
    // timer — expand every Intel section so the full walkthrough is
    // visible in one scroll, rather than making them click into each
    // accordion. Users who reach Intel via the tab button keep the
    // default behavior (only §1 and §6 open).
    els.learnLink?.addEventListener("click", () => {
      switchToView("intel");
      expandAllIntelSections();
    });
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  initFilters();
  initLegend();
  initApiBadge();
  initViewTabs();
  initWelcomeBanner();
  initSearch();
  initCopyDelegation();
  loadParcels();
})();
