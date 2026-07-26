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

  // Pathway → color palette + list-card blurb explaining the redev angle.
  const PATHWAY_STYLE = {
    pathway_1_golf_ringed: {
      color: "#d93b3b", label: "1 · Golf, ringed",
      angle: "Overlay applies — density constrained. Look for adjoining out-parcels or reposition play (banquet, wellness, resort) instead of full residential conversion.",
    },
    pathway_1b_golf_partial: {
      color: "#ee7d3c", label: "1B · Golf, partial",
      angle: "Overlay likely applies. Order a boundary polygon and re-run the ring test manually — a course that classifies 'partial' at 12 samples may swing to 'not ringed' at higher resolution.",
    },
    pathway_2_golf_not_ringed: {
      color: "#f2b731", label: "2 · Golf, not ringed", star: true,
      angle: "★ TOP DEAL. No overlay — full density flexibility. Combine SB 1434 unlock with existing golf-course residential entitlements for a high-yield conversion.",
    },
    pathway_3_industrial: {
      color: "#5b6a7e", label: "3 · Industrial",
      angle: "Classic brownfield-to-mixed-use. Environmental remediation cost is the primary underwriting question — request Phase II ESA before LOI.",
    },
    pathway_4_commercial: {
      color: "#2f6fd9", label: "4 · Commercial",
      angle: "Retail apocalypse supply meets residential demand. Strip centers and struggling big-box on brownfield-adjacent land.",
    },
    pathway_5_office: {
      color: "#4682b4", label: "5 · Office",
      angle: "Post-COVID vacancy + SB 1434 unlock = teardown-to-rental play. Verify current occupancy and lease tails.",
    },
    pathway_6_institutional: {
      color: "#7b3f9f", label: "6 · Institutional",
      angle: "Long-tenured owners (churches, schools, hospitals). Patient-capital deals — sellers often need seller-financing or long closes.",
    },
    pathway_7_residential_redev: {
      color: "#3ea55c", label: "7 · Residential redev",
      angle: "Mobile-home parks and aging garden apartments. Existing entitlement plus SB 1434 unlock supports densification — mind tenant displacement rules.",
    },
    pathway_8_utility: {
      color: "#8b5a2b", label: "8 · Utility",
      angle: "⚠ 15-YEAR TITLE LOOKBACK required before you can rely on qualification. Any prior utility ownership disqualifies under §163.2525(4)(e).",
    },
    pathway_9_auto_fuel: {
      color: "#c15a12", label: "9 · Auto/fuel",
      angle: "Near-certain petroleum contamination. Cleanup cost IS the deal — Phase II ESA and remediation estimate before LOI.",
    },
    pathway_10_hospitality: {
      color: "#c62d8b", label: "10 · Hospitality",
      angle: "Older hotels/motels — often historic dry-cleaner exposure on-site. Soft ADR at aging properties makes these attractive teardowns.",
    },
    pathway_11_vacant_commercial: {
      color: "#1fa39a", label: "11 · Vacant commercial",
      angle: "Least-encumbered path — no tenants to relocate. Confirm no active enforcement action on the site before proceeding.",
    },
    pathway_12_mixed_use: {
      color: "#4b3fa6", label: "12 · Mixed use",
      angle: "Existing mixed-use eligible for intensification. Check current FAR utilization vs. what SB 1434 unlocks.",
    },
    pathway_13_other: {
      color: "#a4abb9", label: "13 · Other",
      angle: "Uncategorized — eyeball the DOR use code and current use before deciding whether to pursue.",
    },
    pathway_golf_pending: {
      color: "#c9c9c9", label: "Golf · pending ring test",
      angle: "Ring test hasn't run yet. Hit POST /admin/run-ring-test to classify.",
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
    legendList: $("legend-list"),
    legend: $("legend"),
    legendToggle: $("legend-toggle"),
    fCounty: $("f-county"),
    fPathway: $("f-pathway"),
    fEnv: $("f-env"),
    fRing: $("f-ring"),
    fUdb: $("f-udb"),
    fMinAcres: $("f-min-acres"),
    fReset: $("f-reset"),
    listContainer: $("list-container"),
    listCount: $("list-count"),
    listEmpty: $("list-empty"),
  };

  // Cache the most recent fetch so the List view can re-render on tab
  // switch without hitting the API again.
  let lastRows = [];

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
    const parts = [];

    parts.push('<div class="popup">');
    parts.push(
      `<h3>${esc(p.parcel_id || "—")}</h3>`,
      `<p class="owner">${esc(p.own_name || "(no owner listed)")}</p>`,
    );

    // Badges — quick visual scan of the important stuff.
    parts.push('<div class="badges">');
    parts.push(`<span class="badge" style="background:${style.color}22;border-color:${style.color}">${esc(style.label)}</span>`);
    if (p.env_trigger) {
      parts.push(`<span class="badge env">${esc(p.env_trigger)}</span>`);
    }
    if (p.udb_status === "inside") {
      parts.push('<span class="badge udb-in">UDB inside</span>');
    } else if (p.udb_status === "outside") {
      parts.push('<span class="badge udb-out">UDB outside</span>');
    }
    if (p.ring_test_result) {
      const cls = p.ring_test_result === "not_ringed" ? "ring-not" : "";
      const pct = p.ring_test_pct != null ? ` (${Math.round(p.ring_test_pct)}%)` : "";
      parts.push(`<span class="badge ${cls}">Ring: ${esc(p.ring_test_result)}${pct}</span>`);
    }
    if (p.adjacent_residential) {
      parts.push('<span class="badge adj">Adj. SF residential</span>');
    }
    if (p.utility_flag) {
      parts.push('<span class="badge util">Utility flag</span>');
    }
    parts.push("</div>");

    // Details
    parts.push("<dl>");
    parts.push(`<dt>County</dt><dd>${esc(county)}</dd>`);
    parts.push(`<dt>Acres</dt><dd>${fmtNum(p.acres, 2)}</dd>`);
    parts.push(`<dt>DOR use</dt><dd>${esc(p.dor_uc || "—")}</dd>`);
    if (p.brownfield_area_name) {
      parts.push(`<dt>Brownfield</dt><dd>${esc(p.brownfield_area_name)}</dd>`);
    }
    parts.push("</dl>");

    // Aerial view link
    if (p.latitude != null && p.longitude != null) {
      const g = `https://www.google.com/maps/@${p.latitude},${p.longitude},17z/data=!3m1!1e1`;
      parts.push(
        '<div class="actions">',
        `<a href="${g}" target="_blank" rel="noopener">Open aerial view ↗</a>`,
        "</div>"
      );
    }
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
    markerGroup.clearLayers();
    let totalAcres = 0;
    let adjacentCount = 0;
    for (const p of rows) {
      if (p.latitude == null || p.longitude == null) continue;
      markerGroup.addLayer(makeMarker(p));
      totalAcres += Number(p.acres) || 0;
      if (p.adjacent_residential) adjacentCount += 1;
    }
    els.kpiCount.textContent = fmtNum(rows.length);
    els.kpiAcres.textContent = fmtNum(totalAcres, 0);
    els.kpiAdjacent.textContent = fmtNum(adjacentCount);
    renderList(rows);
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

  function paSearchUrl(p) {
    // Search-based deep link, resilient to individual PA site URL rot.
    const q = `${p.parcel_id || ""} ${COUNTY_NAMES[p.county_fips] || ""} FL property appraiser`;
    return `https://www.google.com/search?q=${encodeURIComponent(q)}`;
  }

  function qualifyingReasons(p) {
    const reasons = [];

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

    // Gate 4 — adjacency
    reasons.push({
      kind: p.adjacent_residential ? "ok" : "warn",
      text: p.adjacent_residential
        ? "SF-residential parcel within 500 ft (Gate 4 ✓)"
        : "No SF-residential neighbor found — Gate 4 may fail on re-check",
    });

    // Gate 5C — Miami-Dade UDB
    if (p.udb_status) {
      const inside = p.udb_status === "inside";
      reasons.push({
        kind: inside ? "ok" : "warn",
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

    // Utility flag warning
    if (p.utility_flag) {
      reasons.push({
        kind: "warn",
        text: "Utility flag set — 15-year title lookback REQUIRED before proceeding",
      });
    }

    return reasons;
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
      addr ? esc(addr) : null,
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
    if (aer) {
      actions.push(`<a class="btn" href="${aer}" target="_blank" rel="noopener">Aerial view</a>`);
    }
    actions.push(
      `<a class="btn" href="${paSearchUrl(p)}" target="_blank" rel="noopener">Property appraiser</a>`,
    );
    parts.push(`<div class="card-actions">${actions.join("")}</div>`);

    card.innerHTML = parts.join("");

    // Also make the whole card clickable — matches the user's ask
    // ("shows the parcel and searches google for the address when clicked").
    // Click anywhere outside a link opens the same Google search.
    card.addEventListener("click", (e) => {
      if (e.target.closest("a")) return;
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
      loadParcels();
    });
  }

  function initLegend() {
    const frag = document.createDocumentFragment();
    for (const [key, style] of Object.entries(PATHWAY_STYLE)) {
      const li = document.createElement("li");
      const swatch = document.createElement("span");
      swatch.className = "swatch" + (style.star ? " star" : "");
      swatch.style.background = style.color;
      li.appendChild(swatch);
      const label = document.createElement("span");
      label.textContent = style.label;
      li.appendChild(label);
      li.title = key;
      li.style.cursor = "pointer";
      li.addEventListener("click", () => {
        els.fPathway.value = key;
        loadParcels();
      });
      frag.appendChild(li);
    }
    els.legendList.appendChild(frag);

    els.legendToggle.addEventListener("click", () => {
      const collapsed = els.legend.classList.toggle("collapsed");
      els.legendToggle.textContent = collapsed ? "+" : "−";
      els.legendToggle.setAttribute("aria-expanded", String(!collapsed));
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

  function initViewTabs() {
    const buttons = document.querySelectorAll(".view-btn");
    const views = {
      map: document.getElementById("view-map"),
      list: document.getElementById("view-list"),
      intel: document.getElementById("view-intel"),
    };
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const target = btn.dataset.view;
        buttons.forEach((b) => {
          const active = b === btn;
          b.classList.toggle("active", active);
          b.setAttribute("aria-selected", String(active));
        });
        Object.entries(views).forEach(([key, el]) => {
          if (!el) return;
          const active = key === target;
          el.classList.toggle("active", active);
          el.hidden = !active;
        });
        // Leaflet needs to recompute its container size when the map
        // pane comes back from display:none.
        if (target === "map") map.invalidateSize();
        // If the list was populated before the tab was first opened, the
        // rows are already rendered — no re-render needed.
      });
    });
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  initFilters();
  initLegend();
  initApiBadge();
  initViewTabs();
  loadParcels();
})();
