/* SB 1434 Scanner — frontend logic.
 *
 * Zero-build vanilla JS. Fetches from /qualifying-parcels, renders one
 * Leaflet CircleMarker per row, and shows a per-parcel detail drawer
 * on the right when a marker or list card is clicked.
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
  const urlParams = new URLSearchParams(window.location.search);
  const API_BASE = (
    urlParams.get("api") ||
    window.SB1434_API ||
    DEFAULT_API
  ).replace(/\/$/, "");

  const CENTER = [26.1, -80.2];
  const ZOOM = 10;
  const PARCEL_LIMIT = 5000;
  const WELCOME_STORAGE_KEY = "sb1434.welcomeDismissed.v1";
  const WATCHLIST_STORAGE_KEY = "sb1434.watchlist.v1";

  const COUNTY_NAMES = { "23": "Miami-Dade", "16": "Broward", "60": "Palm Beach" };

  // Pathway → color palette + label + short (map legend) + one-liner
  // (popup/drawer header) + longer angle (card body).
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
    pathway_7_residential_redev: {
      color: "#3ea55c", label: "7 · Residential redev", short: "Residential redev",
      one_liner: "Existing residential 5+ acres — mobile-home park or garden apartment densification.",
      angle: "Mobile-home parks and aging garden apartments. Existing entitlement plus SB 1434 unlock supports densification — mind tenant displacement rules.",
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

  const PA_URLS = {
    "23": { label: "Miami-Dade Property Appraiser", url: "https://www.miamidade.gov/pa/property_search.asp" },
    "16": { label: "Broward County Property Appraiser", url: "https://web.bcpa.net/BcpaClient/#/Record-Search" },
    "60": { label: "Palm Beach County Property Appraiser", url: "https://www.pbcgov.org/papa/" },
  };

  const DOR_USE_CODES = {
    "001": "Single Family", "002": "Mobile Home", "003": "Multi-Family (10+)",
    "004": "Condominium", "005": "Cooperative", "006": "Retirement Home",
    "007": "Misc Residential", "008": "Multi-Family (<10)", "009": "Non-Market Residential",
    "010": "Vacant Commercial", "011": "Store", "012": "Mixed Use Store/Office",
    "013": "Department Store", "014": "Supermarket", "015": "Regional Mall",
    "016": "Community Shopping Center", "017": "Professional Office", "018": "Medical Office",
    "019": "Financial Office", "020": "Airport / Bus Terminal", "021": "Restaurant",
    "022": "Drive-in Restaurant", "023": "Nightclub", "024": "Bowling Alley",
    "025": "Tourist Attraction", "026": "Service Station", "027": "Auto Sales / Repair",
    "028": "Parking Lot (Commercial)", "029": "Wholesale Outlet", "030": "Florist / Greenhouse",
    "031": "Drive-in Theater", "032": "Theater", "033": "Auditorium",
    "034": "Amusement Park", "035": "Fairground", "036": "Camp",
    "037": "Racetrack", "038": "Golf Course", "039": "Hotel / Motel",
    "040": "Vacant Industrial", "041": "Light Manufacturing", "042": "Heavy Manufacturing",
    "043": "Lumber Yard", "044": "Packing Plant", "045": "Cannery",
    "046": "Other Food Processing", "047": "Mineral Processing", "048": "Warehouse",
    "049": "Open Storage",
  };
  function propertyType(dor_uc) {
    if (!dor_uc) return "Unknown use";
    return DOR_USE_CODES[dor_uc] || `DOR ${dor_uc}`;
  }

  // Property-type buckets — the primary categorization users interact with.
  // Ordered by specificity so the first match wins in propertyTypeKey().
  // Colors are the map/legend palette.
  const PROPERTY_TYPE_STYLE = {
    golf:                     { color: "#3ea55c", label: "Golf Course" },
    industrial:               { color: "#5b6a7e", label: "Industrial / Warehouse" },
    commercial:               { color: "#2f6fd9", label: "Commercial / Retail" },
    office:                   { color: "#4682b4", label: "Office" },
    residential:              { color: "#7dc47a", label: "Residential" },
    auto_fuel:                { color: "#e0821e", label: "Auto / Fuel" },
    hospitality:              { color: "#c62d8b", label: "Hospitality" },
    restaurant_entertainment: { color: "#7b3f9f", label: "Restaurant / Entertainment" },
    mixed_use:                { color: "#4b3fa6", label: "Mixed Use" },
    vacant_commercial:        { color: "#1fa39a", label: "Vacant Commercial" },
    other:                    { color: "#a4abb9", label: "Other" },
  };

  // Map a DOR use code to exactly one property-type key. Order matters —
  // more specific matches (vacant_commercial 010, auto_fuel 025-028/048)
  // win over broader ones (commercial 010-029, industrial 040-049).
  function propertyTypeKey(dor_uc) {
    if (!dor_uc) return "other";
    const c = String(dor_uc);
    if (c === "038") return "golf";
    if (c === "039") return "hospitality";
    if (c === "010") return "vacant_commercial";
    if (c === "030" || c === "031") return "mixed_use";
    if (["025", "026", "027", "028", "048"].includes(c)) return "auto_fuel";
    if (["021", "022", "032", "033", "034", "035"].includes(c)) return "restaurant_entertainment";
    if (c >= "017" && c <= "019") return "office";
    if (c >= "001" && c <= "009") return "residential";
    if (c >= "040" && c <= "049") return "industrial";      // 048 already caught by auto_fuel
    if (c >= "011" && c <= "029") return "commercial";      // narrower buckets already caught above
    return "other";
  }

  function styleForPropertyType(key) {
    return PROPERTY_TYPE_STYLE[key] || PROPERTY_TYPE_STYLE.other;
  }

  // Rough estimated "max units" for a redevelopment underwriting screen.
  // These numbers are typical Florida infill assumptions PER PROPERTY-TYPE
  // BUCKET — the actual buildable count is set by the local comp plan, not
  // SB 1434 itself. They exist so filters like "min 100 units" have
  // something to grab onto without needing zoning data.
  const UNITS_PER_ACRE = {
    // Golf: not-ringed unlocks full comp-plan density; ringed is capped
    // by the overlay to be neighborhood-compatible (~4-8 u/ac typical).
    golf:                     20,   // effective average (see pathway_2 override)
    industrial:               40,   // dense mixed-use conversion
    commercial:               30,   // vertical retail + residential above
    office:                   30,   // teardown-to-rental
    residential:              25,   // densification play (was 8-10 typical)
    auto_fuel:                40,   // small infill footprint, dense build
    hospitality:              45,   // hotel-to-condo, downtown / beach
    restaurant_entertainment: 25,
    mixed_use:                35,   // intensification of existing MU
    vacant_commercial:        30,
    other:                    15,
  };
  const RINGED_GOLF_UNITS_PER_ACRE = 5;    // overlay-constrained
  const NOTRINGED_GOLF_UNITS_PER_ACRE = 30; // full comp-plan flexibility

  // Returns { unitsPerAcre, estUnits, note } or { estUnits: null } when
  // acres isn't known.
  function estimateUnits(p) {
    const acres = Number(p.acres);
    if (!(acres > 0)) return { unitsPerAcre: null, estUnits: null };
    const key = propertyTypeKey(p.dor_uc);
    let upa = UNITS_PER_ACRE[key] || UNITS_PER_ACRE.other;
    if (key === "golf") {
      if (p.ring_test_result === "not_ringed") upa = NOTRINGED_GOLF_UNITS_PER_ACRE;
      else if (p.ring_test_result === "ringed") upa = RINGED_GOLF_UNITS_PER_ACRE;
    }
    return {
      unitsPerAcre: upa,
      estUnits: Math.round(acres * upa),
    };
  }

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
    fPropertyType: $("f-property-type"),
    fEnv: $("f-env"),
    fRing: $("f-ring"),
    fUdb: $("f-udb"),
    fMinAcres: $("f-min-acres"),
    fMaxAcres: $("f-max-acres"),
    fMinLandRatio: $("f-min-land-ratio"),
    fMinUnits: $("f-min-units"),
    fLandHeavy: $("f-land-heavy"),
    fSatellite: $("f-satellite"),
    fShowBrownfields: $("f-show-brownfields"),
    fShowUdb: $("f-show-udb"),
    fReset: $("f-reset"),
    fExportCsv: $("f-export-csv"),
    fSearch: $("f-search"),
    fSearchClear: $("f-search-clear"),
    parcelTable: $("parcel-table"),
    parcelTbody: $("parcel-tbody"),
    listCount: $("list-count"),
    listEmpty: $("list-empty"),
    welcomeBanner: $("welcome-banner"),
    welcomeDismiss: $("welcome-dismiss"),
    // Watchlist
    wlToggle: $("watchlist-toggle"),
    wlCount: $("wl-count"),
    wlExport: $("watchlist-export"),
    // Drawer
    drawer: $("drawer"),
    drawerClose: $("drawer-close"),
    drawerBody: $("drawer-body"),
  };

  // Most-recent server-filtered rows. Search + watchlist filter narrows
  // this on the client; visibleRows is what markers/list render.
  let lastRows = [];
  let searchQuery = "";
  let watchlistFilterOn = false;
  let currentDrawerParcel = null;

  // ------------------------------------------------------------------
  // Watchlist (localStorage-backed)
  // ------------------------------------------------------------------

  // Shape: { [parcel_id]: { added_at: iso_string, note: string } }
  function loadWatchlist() {
    try {
      const raw = localStorage.getItem(WATCHLIST_STORAGE_KEY);
      if (!raw) return {};
      const parsed = JSON.parse(raw);
      return (parsed && typeof parsed === "object") ? parsed : {};
    } catch {
      return {};
    }
  }
  function saveWatchlist(wl) {
    try { localStorage.setItem(WATCHLIST_STORAGE_KEY, JSON.stringify(wl)); } catch {}
  }
  let watchlist = loadWatchlist();

  function isWatched(pid) { return !!watchlist[pid]; }
  // toggleWatched now takes the full parcel object as a second arg. When
  // starring, we snapshot the whole parcel into localStorage so the
  // watchlist survives backend re-screens that would otherwise drop the
  // parcel out of qualifying_parcels (e.g. new govt-owner exclusion
  // removes a parcel the user starred yesterday). Existing legacy
  // entries (from before this change) have no snapshot; they'll render
  // as a placeholder row until re-starred.
  function toggleWatched(pid, parcelData) {
    if (!pid) return false;
    if (watchlist[pid]) {
      delete watchlist[pid];
    } else {
      watchlist[pid] = {
        added_at: new Date().toISOString(),
        note: "",
        parcel: parcelData || null,
      };
    }
    saveWatchlist(watchlist);
    updateWatchlistCount();
    // If the watchlist filter is on and this un-star drops the parcel out,
    // re-render to remove it from the map/list.
    if (watchlistFilterOn) rerender();
    return isWatched(pid);
  }
  // Return every watchlisted parcel that isn't already in `fetched` — these
  // are the "stub" rows we synthesize from the localStorage snapshot so the
  // list doesn't silently vanish when a starred parcel gets excluded by a
  // backend re-screen.
  function watchlistSnapshotsMissingFrom(fetched) {
    const seen = new Set(fetched.map((p) => p.parcel_id));
    const out = [];
    for (const [pid, entry] of Object.entries(watchlist)) {
      if (seen.has(pid)) continue;
      if (entry && entry.parcel) {
        // Mark as a snapshot so the renderer can flag it visually.
        out.push({ ...entry.parcel, _fromSnapshot: true });
      } else {
        // Legacy entry with no parcel data — show a placeholder row so
        // the user at least knows the ID they starred is still tracked.
        out.push({
          parcel_id: pid,
          _fromSnapshot: true,
          _placeholder: true,
          own_name: "(no cached data — re-star to refresh)",
          county_fips: null,
          acres: null,
          dor_uc: null,
        });
      }
    }
    return out;
  }
  function setWatchNote(pid, note) {
    if (!pid || !watchlist[pid]) return;
    watchlist[pid].note = note || "";
    saveWatchlist(watchlist);
  }
  function updateWatchlistCount() {
    const n = Object.keys(watchlist).length;
    if (els.wlCount) els.wlCount.textContent = String(n);
  }

  // ------------------------------------------------------------------
  // Map
  // ------------------------------------------------------------------

  const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView(CENTER, ZOOM);

  const baseStreets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  });
  const baseSatellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {
      maxZoom: 19,
      attribution: 'Tiles &copy; Esri &mdash; Source: Esri, i-cubed, USDA, USGS, AEX, GeoEye, Getmapping, Aerogrid, IGN, IGP, UPR-EGP, and the GIS User Community',
    },
  );
  baseStreets.addTo(map);

  let markerGroup = L.layerGroup().addTo(map);
  // Always-on toggleable overlays. Each holds an L.geoJSON layer while
  // the corresponding sidebar checkbox is checked; null when the layer
  // isn't on the map.
  let brownfieldOverlay = null;
  let udbOverlay = null;

  // ------------------------------------------------------------------
  // Marker rendering
  // ------------------------------------------------------------------

  function radiusForAcres(acres) {
    const a = Math.max(0, Number(acres) || 0);
    const r = Math.sqrt(a);
    return Math.max(5, Math.min(20, r));
  }
  function styleForPathway(pathway) {
    return PATHWAY_STYLE[pathway] || PATHWAY_STYLE.pathway_13_other;
  }

  function makeMarker(parcel) {
    // Marker color = property-type color. Special case: not-ringed golf
    // uses the accent gold so the top deals still pop off the map even
    // though "golf" is normally green.
    const isTopDeal = parcel.pathway_hint === "pathway_2_golf_not_ringed";
    const ptStyle = styleForPropertyType(propertyTypeKey(parcel.dor_uc));
    const fillColor = isTopDeal ? "#f2b731" : ptStyle.color;
    const baseRadius = radiusForAcres(parcel.acres);
    const radius = isTopDeal ? baseRadius * 1.3 : baseRadius;

    const marker = L.circleMarker(
      [parcel.latitude, parcel.longitude],
      {
        radius,
        color: "#1a1e26",
        weight: isTopDeal ? 2 : 1,
        opacity: 1,
        fillColor,
        fillOpacity: 0.82,
      },
    );

    // Click a marker → open drawer (not Leaflet popup).
    marker.on("click", () => openDrawer(parcel));
    return marker;
  }

  // ------------------------------------------------------------------
  // Drawer (right-side detail panel)
  // ------------------------------------------------------------------

  function openDrawer(parcel) {
    currentDrawerParcel = parcel;
    els.drawerBody.innerHTML = renderDrawer(parcel);
    document.body.classList.add("drawer-open");
    els.drawer.hidden = false;
    els.drawer.setAttribute("aria-hidden", "false");
    // Attach the note-input listener (delegated per-parcel so we don't
    // stack listeners across openings).
    const noteEl = els.drawerBody.querySelector(".drawer-note");
    if (noteEl) {
      noteEl.addEventListener("input", debounce(() => {
        setWatchNote(parcel.parcel_id, noteEl.value);
      }, 250));
    }
    // Star toggle in the drawer.
    const starBtn = els.drawerBody.querySelector(".drawer-star");
    if (starBtn) {
      starBtn.addEventListener("click", () => {
        const on = toggleWatched(parcel.parcel_id, parcel);
        // Re-render drawer to show/hide the note input.
        openDrawer(parcel);
      });
    }
    // Brownfield polygon overlay is now a persistent toggle in the
    // filter row — no per-parcel fetch on drawer open.
  }

  function closeDrawer() {
    currentDrawerParcel = null;
    document.body.classList.remove("drawer-open");
    // Delay hidden until after the transition so it slides out.
    setTimeout(() => {
      els.drawer.hidden = true;
      els.drawer.setAttribute("aria-hidden", "true");
    }, 220);
  }

  function renderDrawer(p) {
    const style = styleForPathway(p.pathway_hint);
    const county = COUNTY_NAMES[p.county_fips] || p.county_fips || "—";
    const addr = addressString(p);
    const watched = isWatched(p.parcel_id);
    const note = watched ? (watchlist[p.parcel_id]?.note || "") : "";
    const parts = [];

    // Header: property type + pathway line
    parts.push(`<h2>${esc(propertyType(p.dor_uc))}</h2>`);
    parts.push(`<p class="pathway-line" style="color:${style.color}">SB 1434 Pathway: ${esc(style.label)}</p>`);
    parts.push(`<p class="owner">${esc(p.own_name || "(no owner listed)")} · ${fmtNum(p.acres, 1)} acres · ${esc(county)}</p>`);

    // Star + (optional) note input
    parts.push('<div class="drawer-star-row">');
    parts.push(
      `<button class="drawer-star${watched ? " on" : ""}" type="button" title="Toggle watchlist">`,
      `${watched ? "★" : "☆"} ${watched ? "Watchlisted" : "Add to watchlist"}`,
      `</button>`,
    );
    parts.push("</div>");
    if (watched) {
      parts.push(`<textarea class="drawer-note" placeholder="Add a note (saved locally)…">${esc(note)}</textarea>`);
      parts.push('<div class="drawer-note-hint">Notes are stored in your browser only.</div>');
    }

    // Parcel ID chip + address
    parts.push(
      '<div class="parcel-row">',
      `<code class="parcel-id">${esc(p.parcel_id || "—")}</code>`,
      `<button class="copy-btn" type="button" data-copy="${esc(p.parcel_id || "")}">Copy</button>`,
      "</div>",
    );
    if (!addr) {
      parts.push('<p class="no-addr" style="margin:-4px 0 8px">No address on file — search by parcel ID.</p>');
    } else {
      parts.push(`<p class="owner" style="margin:-4px 0 8px">${esc(addr)}</p>`);
    }

    // Value context
    parts.push(renderValueContext(p));

    // Gate checklist
    parts.push(renderGateChecklist(p));

    // Redevelopment angle
    if (style.one_liner) {
      parts.push('<span class="section-h">Redevelopment angle</span>');
      parts.push(`<div class="card-angle"><p>${esc(style.one_liner)}</p></div>`);
    }

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
    actions.push(`<a class="primary" href="${googleSearchUrl(p)}" target="_blank" rel="noopener">Search Google</a>`);
    const aer = aerialUrl(p);
    if (aer) actions.push(`<a href="${aer}" target="_blank" rel="noopener">🛰️ Aerial</a>`);
    const pa = paLink(p);
    if (pa) actions.push(`<a href="${pa.url}" target="_blank" rel="noopener" title="${esc(pa.label)}">County Appraiser</a>`);
    parts.push(`<div class="actions">${actions.join("")}</div>`);

    // Listings — only surfaced when we have an address to search against
    // (no reliable free listings API, so these open address-based searches
    // on each site rather than embedded listings).
    const listings = listingLinks(p);
    if (listings.length) {
      parts.push(
        '<span class="section-h" style="margin-top:8px">Search listings</span>',
        '<div class="actions">',
        listings.map((l) => `<a href="${l.url}" target="_blank" rel="noopener" title="${esc(l.title)}">${esc(l.label)} ↗</a>`).join(""),
        "</div>",
      );
    }

    return parts.join("");
  }

  // ------------------------------------------------------------------
  // Shared renderers (used by drawer + list card)
  // ------------------------------------------------------------------

  function gateChecklist(p) {
    const gates = [];
    const acres = Number(p.acres) || 0;
    gates.push({
      icon: acres < 6 ? "⚠️" : "✅",
      label: "Gate 1 · 5+ acres",
      detail: `${fmtNum(acres, 1)} acres${acres < 6 ? " (borderline — verify LND_SQFOOT)" : ""}`,
    });
    const county = COUNTY_NAMES[p.county_fips] || p.county_fips || "—";
    gates.push({ icon: "✅", label: "Gate 2 · Tri-county", detail: county });

    let g3 = "";
    if (p.env_trigger === "brownfield_area") {
      g3 = p.brownfield_area_name ? `Inside FDEP brownfield area: ${p.brownfield_area_name}` : "Inside FDEP brownfield area";
    } else if (p.env_trigger === "cleanup_site") {
      g3 = "Within 1,500 ft of a DEP cleanup site";
    } else if (p.env_trigger === "both") {
      g3 = p.brownfield_area_name
        ? `Both — inside "${p.brownfield_area_name}" AND near a DEP cleanup site`
        : "Both — inside a brownfield area AND near a DEP cleanup site";
    } else {
      g3 = "Environmental trigger present";
    }
    gates.push({ icon: "✅", label: "Gate 3 · Environmental trigger", detail: g3 });

    if (p.adjacent_residential) {
      gates.push({ icon: "✅", label: "Gate 4 · Residential adjacency", detail: "Residential parcel within 500 ft" });
    } else {
      gates.push({ icon: "ℹ️", label: "Gate 4 · Residential adjacency", detail: "Not detected in DOR data — verify via aerial view" });
    }

    const sub = [
      { icon: "✅", text: "Not agricultural (DOR 050-069)" },
      { icon: "✅", text: "Not government-owned public park" },
      { icon: "✅", text: "Not within ¼ mile of a military installation" },
      { icon: "✅", text: "Not institutional (DOR 070-079) or utility (091-097)" },
    ];
    if (p.udb_status) {
      sub.push({
        icon: p.udb_status === "inside" ? "✅" : "ℹ️",
        text: p.udb_status === "inside" ? "Inside Miami-Dade UDB" : "Outside Miami-Dade UDB — additional entitlement friction",
      });
    }
    gates.push({ icon: "✅", label: "Gate 5 · No exclusions apply", sub });
    return gates;
  }

  function renderGateChecklist(p) {
    const gates = gateChecklist(p);
    const parts = ['<div class="gates"><span class="section-h">Statutory gate checklist</span><ul>'];
    for (const g of gates) {
      parts.push(
        `<li><span class="g-icon">${g.icon}</span>`,
        `<span class="g-body"><strong>${esc(g.label)}</strong>`,
        g.detail ? `<span class="g-detail"> — ${esc(g.detail)}</span>` : "",
        "</span>",
      );
      if (g.sub && g.sub.length) {
        parts.push('<ul class="sub">');
        for (const s of g.sub) {
          parts.push(`<li><span class="g-icon">${s.icon}</span> ${esc(s.text)}</li>`);
        }
        parts.push("</ul>");
      }
      parts.push("</li>");
    }
    parts.push("</ul></div>");
    return parts.join("");
  }

  function renderValueContext(p) {
    const est = estimateUnits(p);
    if (p.jv == null && p.lnd_val == null && est.estUnits == null) return "";
    const parts = ['<div class="value-ctx"><span class="section-h">Value &amp; density estimate</span><dl>'];
    if (p.jv != null) parts.push(`<dt>Just value</dt><dd>${fmtCurrency(p.jv)}</dd>`);
    if (p.lnd_val != null) parts.push(`<dt>Land value</dt><dd>${fmtCurrency(p.lnd_val)}</dd>`);
    if (p.land_to_improvement_ratio != null) {
      const r = Number(p.land_to_improvement_ratio);
      const label = r >= 999 ? "Vacant / near-vacant"
        : r >= 1 ? `${r.toFixed(2)}× (land-heavy)` : r.toFixed(2) + "×";
      parts.push(`<dt>Land/improvement</dt><dd>${esc(label)}</dd>`);
    }
    if (est.estUnits != null) {
      // Coarse estimate — flag it as such so nobody mistakes it for
      // entitled density.
      parts.push(
        `<dt title="Rough estimate = acres × typical units/acre for this property type. Actual buildable is set by the local comp plan, not SB 1434.">Est. max units</dt>`,
        `<dd>${fmtNum(est.estUnits)} <span style="color:#8a92a2;font-family:inherit">(~${est.unitsPerAcre}/ac, rough)</span></dd>`,
      );
    }
    parts.push("</dl></div>");
    return parts.join("");
  }

  function nextStepsFor(p) {
    const steps = [];
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
    steps.push("Review aerial imagery before investing more time (link in the Actions bar below).");
    return steps;
  }

  // ------------------------------------------------------------------
  // Address / URL helpers
  // ------------------------------------------------------------------

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
    // Prefer an address search — Google Maps handles typo tolerance
    // and drops you at the correct building. Coordinates fall back
    // only when we truly have no address on file.
    const addr = addressString(p);
    if (addr) {
      return `https://www.google.com/maps/search/${encodeURIComponent(addr)}`;
    }
    if (p.latitude == null || p.longitude == null) return null;
    return `https://www.google.com/maps/@${p.latitude},${p.longitude},500m/data=!3m1!1e3`;
  }
  function paLink(p) { return PA_URLS[p.county_fips] || null; }

  // Listings-search links.
  //
  // Direct site URLs don't reliably honor address query params — LoopNet
  // and Crexi both ignore what we send and default to national. So the
  // primary buttons use a Google site:search with the exact address in
  // quotes: if the site has any indexed listing page for that address,
  // Google jumps you straight to it; if not, you get an empty result
  // (which is the honest answer to "is this parcel listed?").
  //
  // No free listings API exists for a static frontend to confirm
  // active-for-sale status without scraping, which would require a
  // backend + violate site ToS. This is the closest we can get.
  function listingLinks(p) {
    const addr = addressString(p);
    if (!addr) return [];
    const quoted = `"${addr}"`;
    const encGoogleQ = (site) =>
      "https://www.google.com/search?q=" +
      encodeURIComponent(`site:${site} ${quoted}`);

    const key = propertyTypeKey(p.dor_uc);
    const isResidential = key === "residential";
    const out = [];

    // Commercial-first buckets get LoopNet + Crexi first.
    if (!isResidential) {
      out.push({
        label: "LoopNet",
        url: encGoogleQ("loopnet.com"),
        title: `Google site-search — LoopNet listings for ${addr}`,
      });
      out.push({
        label: "Crexi",
        url: encGoogleQ("crexi.com"),
        title: `Google site-search — Crexi listings for ${addr}`,
      });
    }
    // Zillow works for both — but the direct-URL slug format DOES
    // resolve to a page for most Florida addresses, so use it here
    // instead of the Google detour. If no listing exists, Zillow
    // still shows the address's Zestimate / property-details page.
    const zillowSlug = addr.replace(/,/g, "").replace(/\s+/g, "-");
    out.push({
      label: "Zillow",
      url: `https://www.zillow.com/homes/${encodeURIComponent(zillowSlug)}_rb/`,
      title: `Zillow property page for ${addr}`,
    });
    // For residential parcels also add Realtor.com direct-address URL.
    if (isResidential) {
      out.push({
        label: "Realtor",
        url: `https://www.realtor.com/realestateandhomes-search/${encodeURIComponent(zillowSlug)}`,
        title: `Realtor.com search for ${addr}`,
      });
    }
    return out;
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
  function fmtCurrency(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return "$" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  }
  function debounce(fn, ms) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  }

  // ------------------------------------------------------------------
  // Filters + fetch
  // ------------------------------------------------------------------

  function currentFilters() {
    const q = new URLSearchParams();
    q.set("limit", String(PARCEL_LIMIT));
    // NOTE: property type is applied client-side (see applyClientFilters).
    // The API only knows the pathway_hint column, not the property-type
    // bucket, so it's simpler to fetch server-side by the other filters
    // and narrow to property type in JS.
    const mapping = {
      county: els.fCounty.value,
      env_trigger: els.fEnv.value,
      ring_test_result: els.fRing.value,
      udb_status: els.fUdb.value,
    };
    for (const [k, v] of Object.entries(mapping)) if (v) q.set(k, v);
    const minAcres = els.fMinAcres.value.trim();
    if (minAcres !== "") q.set("min_acres", minAcres);
    const maxAcres = els.fMaxAcres?.value.trim();
    if (maxAcres) q.set("max_acres", maxAcres);
    const mr = els.fMinLandRatio?.value.trim();
    if (mr) q.set("min_land_ratio", mr);
    // Land-heavy shortcut wins over the numeric field when both set.
    if (els.fLandHeavy?.checked) q.set("min_land_ratio", "1.0");
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
      lastRows = rows;
      hideToast();
      rerender();
    } catch (e) {
      if (e.name === "AbortError") return;
      console.error("loadParcels failed:", e);
      showToast(`Failed to load parcels: ${e.message}`);
      lastRows = [];
      rerender();
    } finally {
      showLoading(false);
      inflight = null;
    }
  }

  function applyClientFilters(rows) {
    let out = rows;
    const ptKey = els.fPropertyType?.value || "";
    if (ptKey) {
      out = out.filter((p) => propertyTypeKey(p.dor_uc) === ptKey);
    }
    const minUnits = parseFloat(els.fMinUnits?.value || "");
    if (minUnits > 0) {
      out = out.filter((p) => (estimateUnits(p).estUnits || 0) >= minUnits);
    }
    const q = searchQuery.trim().toLowerCase();
    if (q) {
      out = out.filter((p) => {
        const pid = String(p.parcel_id || "").toLowerCase();
        const owner = String(p.own_name || "").toLowerCase();
        return pid.includes(q) || owner.includes(q);
      });
    }
    if (watchlistFilterOn) {
      // Union: watched rows from the current fetch + local snapshots for
      // watched parcels that AREN'T in the current fetch. This makes the
      // watchlist survive a backend re-screen that would otherwise drop
      // a starred parcel out of qualifying_parcels.
      const fromFetch = out.filter((p) => isWatched(p.parcel_id));
      const snapshots = watchlistSnapshotsMissingFrom(fromFetch);
      out = [...fromFetch, ...snapshots];
    }
    return out;
  }

  function rerender() {
    const rows = applyClientFilters(lastRows);
    markerGroup.clearLayers();
    let totalAcres = 0, adjacentCount = 0, golfCount = 0;
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

    // Zoom-to-single-match affordance from the search box.
    if (searchQuery.trim() && rows.length === 1 && rows[0].latitude != null) {
      const p = rows[0];
      map.setView([p.latitude, p.longitude], 16, { animate: true });
      setTimeout(() => openDrawer(p), 300);
    }
  }

  // ------------------------------------------------------------------
  // List view
  // ------------------------------------------------------------------

  // Sort state for the table. Default = acres DESC (largest parcels
  // first) since that's how the API returns them.
  let tableSort = { key: "acres", dir: "desc" };

  // Per-row value the sort comparator operates on.
  function sortValue(p, key) {
    switch (key) {
      case "property_type": return propertyType(p.dor_uc);
      case "own_name":      return (p.own_name || "").toLowerCase();
      case "address":       return (addressString(p) || "").toLowerCase();
      case "county":        return COUNTY_NAMES[p.county_fips] || "";
      case "acres":         return Number(p.acres) || 0;
      case "jv":            return Number(p.jv) || 0;
      case "ratio":         return Number(p.land_to_improvement_ratio) || 0;
      case "env_trigger":   return p.env_trigger || "";
      case "pathway_hint":  return p.pathway_hint || "";
      default:              return "";
    }
  }

  function sortedRows(rows) {
    const { key, dir } = tableSort;
    const mult = dir === "asc" ? 1 : -1;
    return rows.slice().sort((a, b) => {
      const va = sortValue(a, key);
      const vb = sortValue(b, key);
      if (typeof va === "number" && typeof vb === "number") return (va - vb) * mult;
      return String(va).localeCompare(String(vb)) * mult;
    });
  }

  function renderList(rows) {
    els.listCount.textContent = fmtNum(rows.length);
    els.listEmpty.classList.toggle("hidden", rows.length > 0);
    els.parcelTable.style.display = rows.length ? "" : "none";

    const sorted = sortedRows(rows);
    const frag = document.createDocumentFragment();
    for (const p of sorted) frag.appendChild(makeRow(p));
    els.parcelTbody.replaceChildren(frag);

    // Sort indicator on the active column header
    els.parcelTable.querySelectorAll("thead th").forEach((th) => {
      const active = th.dataset.sort === tableSort.key;
      th.classList.toggle("active-sort", active);
      const existing = th.querySelector(".sort-dir");
      if (existing) existing.remove();
      if (active) {
        const arrow = document.createElement("span");
        arrow.className = "sort-dir";
        arrow.textContent = tableSort.dir === "asc" ? "▲" : "▼";
        th.appendChild(arrow);
      }
    });
  }

  function makeRow(p) {
    const style = styleForPropertyType(propertyTypeKey(p.dor_uc));
    const pathwayStyle = styleForPathway(p.pathway_hint);
    const county = COUNTY_NAMES[p.county_fips] || p.county_fips || "—";
    const addr = addressString(p);
    const isTopDeal = p.pathway_hint === "pathway_2_golf_not_ringed";
    const watched = isWatched(p.parcel_id);
    const ratio = p.land_to_improvement_ratio;
    const ratioLabel = ratio == null ? "—"
      : Number(ratio) >= 999 ? "Vacant"
      : `${Number(ratio).toFixed(2)}×`;
    const dot = isTopDeal
      ? `<span class="pt-dot star" style="background:#f2b731" title="Top deal"></span>`
      : `<span class="pt-dot" style="background:${style.color}"></span>`;

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td title="DOR ${esc(p.dor_uc || "—")}">${dot}${esc(propertyType(p.dor_uc))}</td>
      <td>${esc(p.own_name || "(no owner)")}</td>
      <td>${addr ? esc(addr) : '<span class="no-addr">No address</span>'}</td>
      <td>${esc(county)}</td>
      <td class="num">${fmtNum(p.acres, 1)}</td>
      <td class="num">${p.jv == null ? "—" : fmtCurrency(p.jv)}</td>
      <td class="num" title="Land value ÷ improvement value">${esc(ratioLabel)}</td>
      <td>${esc(p.env_trigger || "—")}</td>
      <td><span class="pathway-tag${isTopDeal ? " top-deal-flag" : ""}" style="color:${pathwayStyle.color}">${esc(pathwayStyle.label)}</span></td>
      <td class="actions">
        <button class="star${watched ? " on" : ""}" type="button" title="Toggle watchlist">${watched ? "★" : "☆"}</button>
        ${aerialUrl(p) ? `<a href="${aerialUrl(p)}" target="_blank" rel="noopener" title="Aerial view">🛰️</a>` : ""}
      </td>
    `;

    // Row click → open drawer (except when clicking a button/link).
    tr.addEventListener("click", (e) => {
      if (e.target.closest("a, button")) return;
      // Deselect any previously selected row, mark this one.
      els.parcelTbody.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
      tr.classList.add("selected");
      openDrawer(p);
    });

    // Star toggle — matches map-drawer star behavior.
    const starBtn = tr.querySelector(".star");
    starBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const nowOn = toggleWatched(p.parcel_id, p);
      starBtn.classList.toggle("on", nowOn);
      starBtn.textContent = nowOn ? "★" : "☆";
    });

    return tr;
  }

  function initTableSort() {
    els.parcelTable.querySelectorAll("thead th[data-sort]").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.dataset.sort;
        if (tableSort.key === key) {
          tableSort.dir = tableSort.dir === "asc" ? "desc" : "asc";
        } else {
          tableSort.key = key;
          tableSort.dir = th.dataset.defaultDir || "asc";
        }
        // Re-render only the list (data hasn't changed, sort has).
        renderList(applyClientFilters(lastRows));
      });
    });
  }

  // ------------------------------------------------------------------
  // Basemap toggle
  // ------------------------------------------------------------------

  function setBasemap(satellite) {
    if (satellite) {
      if (map.hasLayer(baseStreets)) map.removeLayer(baseStreets);
      if (!map.hasLayer(baseSatellite)) baseSatellite.addTo(map);
    } else {
      if (map.hasLayer(baseSatellite)) map.removeLayer(baseSatellite);
      if (!map.hasLayer(baseStreets)) baseStreets.addTo(map);
    }
  }

  // ------------------------------------------------------------------
  // Always-on toggleable overlays (brownfield areas + UDB boundary)
  //
  // Each is a single L.geoJSON fetched from a GeoJSON endpoint on toggle.
  // Layers stay put as the user pans/zooms and are cleared when the
  // checkbox flips off. Fetched lazily (only when the user first turns
  // the toggle on).
  // ------------------------------------------------------------------

  async function setBrownfieldOverlay(on) {
    if (!on) {
      if (brownfieldOverlay) {
        map.removeLayer(brownfieldOverlay);
        brownfieldOverlay = null;
      }
      return;
    }
    if (brownfieldOverlay) return;   // already on the map
    try {
      const r = await fetch(`${API_BASE}/brownfield-areas/geojson`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const gj = await r.json();
      brownfieldOverlay = L.geoJSON(gj, {
        // Force SVG rendering — the map is preferCanvas:true (so all
        // 2k+ CircleMarkers share ONE canvas for performance), but if
        // GeoJSON polygons hit that same canvas they render underneath
        // and effectively vanish. An explicit SVG renderer puts them
        // in their own overlay pane on top of the tiles.
        renderer: L.svg({ padding: 0.5 }),
        style: {
          color: "#a05c00",
          weight: 2,
          fillColor: "#f2b731",
          fillOpacity: 0.42,
        },
        onEachFeature: (feature, layer) => {
          const p = feature.properties || {};
          const bits = [];
          if (p.name) bits.push(`<strong>${escapeAttr(p.name)}</strong>`);
          if (p.county) bits.push(escapeAttr(String(p.county)));
          if (p.acres != null) bits.push(`${Number(p.acres).toLocaleString()} acres`);
          if (bits.length) layer.bindTooltip(bits.join(" · "), { sticky: true });
        },
      }).addTo(map);
    } catch (e) {
      console.error("brownfield overlay fetch failed:", e);
      showToast(`Failed to load brownfield areas: ${e.message}`);
      if (els.fShowBrownfields) els.fShowBrownfields.checked = false;
    }
  }

  async function setUdbOverlay(on) {
    if (!on) {
      if (udbOverlay) {
        map.removeLayer(udbOverlay);
        udbOverlay = null;
      }
      return;
    }
    if (udbOverlay) return;
    try {
      const r = await fetch(`${API_BASE}/udb-boundary/geojson`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const gj = await r.json();
      udbOverlay = L.geoJSON(gj, {
        renderer: L.svg({ padding: 0.5 }),
        style: {
          color: "#1a5fb4",
          weight: 3.5,
          fillColor: "#4ea1ff",
          fillOpacity: 0.10,
          dashArray: "10 6",
        },
        interactive: false,
      }).addTo(map);
    } catch (e) {
      console.error("UDB overlay fetch failed:", e);
      showToast(`Failed to load UDB boundary: ${e.message}`);
      if (els.fShowUdb) els.fShowUdb.checked = false;
    }
  }

  // Tooltip content needs HTML-safe attribute values.
  function escapeAttr(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  // ------------------------------------------------------------------
  // CSV export
  // ------------------------------------------------------------------

  const CSV_COLUMNS = [
    "parcel_id", "county_fips", "county_name", "own_name",
    "phy_addr1", "phy_city", "phy_zipcd",
    "acres", "dor_uc", "property_type",
    "pathway_hint", "env_trigger", "brownfield_area_name",
    "ring_test_result", "ring_test_pct",
    "jv", "lnd_val", "land_to_improvement_ratio",
    "adjacent_residential", "udb_status", "utility_flag",
    "latitude", "longitude",
  ];

  function csvEscape(v) {
    if (v == null) return "";
    const s = String(v);
    if (/[",\r\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
    return s;
  }
  function stampToday() {
    const now = new Date();
    return `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}${String(now.getDate()).padStart(2, "0")}`;
  }
  function rowsToCsv(rows, extraCols) {
    const cols = extraCols ? [...CSV_COLUMNS, ...extraCols] : CSV_COLUMNS;
    const header = cols.join(",");
    const body = rows.map((p) => {
      const enriched = {
        ...p,
        county_name: COUNTY_NAMES[p.county_fips] || "",
        property_type: propertyType(p.dor_uc),
        note: watchlist[p.parcel_id]?.note || "",
        watchlisted_at: watchlist[p.parcel_id]?.added_at || "",
      };
      return cols.map((c) => csvEscape(enriched[c])).join(",");
    }).join("\r\n");
    return header + "\r\n" + body + "\r\n";
  }
  function downloadCsv(csv, filename) {
    const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 500);
  }
  function exportCsv() {
    const rows = applyClientFilters(lastRows);
    if (rows.length === 0) { showToast("No parcels to export with current filters"); return; }
    downloadCsv(rowsToCsv(rows), `sb1434_qualifying_parcels_${stampToday()}.csv`);
  }
  function exportWatchlistCsv() {
    // Watchlist can contain parcels that aren't in the current fetch —
    // export from every row we've ever seen this session AND any pinned
    // parcels we can find in lastRows. For anything else, emit an ID-only
    // stub so the user still gets the parcel_id + their note.
    const seen = new Map();
    for (const p of lastRows) if (isWatched(p.parcel_id)) seen.set(p.parcel_id, p);
    const stubs = [];
    for (const pid of Object.keys(watchlist)) {
      if (!seen.has(pid)) stubs.push({ parcel_id: pid });
    }
    const rows = [...seen.values(), ...stubs];
    if (rows.length === 0) { showToast("Watchlist is empty"); return; }
    downloadCsv(
      rowsToCsv(rows, ["watchlisted_at", "note"]),
      `sb1434_watchlist_${stampToday()}.csv`,
    );
  }

  // ------------------------------------------------------------------
  // Copy delegation
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
        setTimeout(() => { btn.classList.remove("copied"); btn.textContent = prev; }, 1200);
      };
      if (navigator.clipboard?.writeText) {
        navigator.clipboard.writeText(val).then(done, done);
      } else {
        const ta = document.createElement("textarea");
        ta.value = val; document.body.appendChild(ta); ta.select();
        try { document.execCommand("copy"); } catch {}
        document.body.removeChild(ta); done();
      }
    });
  }

  // ------------------------------------------------------------------
  // UI wiring
  // ------------------------------------------------------------------

  function initFilters() {
    const rerun = () => loadParcels();
    // County, env-trigger, ring-test and UDB all round-trip to the API.
    // Property type is client-side and only needs a re-render.
    ["fCounty", "fEnv", "fRing", "fUdb"].forEach((k) => {
      els[k].addEventListener("change", rerun);
    });
    els.fPropertyType.addEventListener("change", () => rerender());
    els.fMinAcres.addEventListener("input", debounce(rerun, 350));
    els.fMaxAcres?.addEventListener("input", debounce(rerun, 350));
    els.fMinLandRatio?.addEventListener("input", debounce(rerun, 350));
    els.fMinUnits?.addEventListener("input", debounce(() => rerender(), 250));
    els.fLandHeavy?.addEventListener("change", rerun);
    els.fSatellite?.addEventListener("change", () => setBasemap(els.fSatellite.checked));
    els.fShowBrownfields?.addEventListener("change", () => setBrownfieldOverlay(els.fShowBrownfields.checked));
    els.fShowUdb?.addEventListener("change", () => setUdbOverlay(els.fShowUdb.checked));
    els.fExportCsv?.addEventListener("click", exportCsv);
    els.fReset.addEventListener("click", () => {
      els.fCounty.value = ""; els.fPropertyType.value = "";
      els.fEnv.value = ""; els.fRing.value = ""; els.fUdb.value = "";
      els.fMinAcres.value = "";
      if (els.fMaxAcres) els.fMaxAcres.value = "";
      // Land ratio resets to the default of 1 (land-heavy default) —
      // NOT blank. Users clear it explicitly if they want everything.
      if (els.fMinLandRatio) els.fMinLandRatio.value = "1";
      if (els.fMinUnits) els.fMinUnits.value = "";
      if (els.fLandHeavy) els.fLandHeavy.checked = false;
      if (els.fSearch) {
        els.fSearch.value = ""; searchQuery = "";
        if (els.fSearchClear) els.fSearchClear.hidden = true;
      }
      map.setView(CENTER, ZOOM);
      loadParcels();
    });
    // Golf KPI shortcut — set the property-type filter to Golf AND the
    // ring-test filter to not-ringed so the map narrows to the top deals.
    els.kpiGolf?.addEventListener("click", () => {
      els.fPropertyType.value = "golf";
      els.fRing.value = "not_ringed";
      loadParcels();
    });
  }

  function initLegend() {
    const legendControl = L.control({ position: "bottomleft" });
    legendControl.onAdd = () => {
      const div = L.DomUtil.create("div", "map-legend");
      // Property-type rows + one dedicated row for the golf "not ringed"
      // top-deal star treatment.
      const rows = Object.entries(PROPERTY_TYPE_STYLE).map(
        ([key, style]) => `
          <li data-property-type="${key}">
            <span class="swatch" style="background:${style.color}"></span>
            <span>${esc(style.label)}</span>
          </li>`,
      ).join("");
      const golfStarRow = `
        <li data-ring="not_ringed" class="legend-topdeal">
          <span class="swatch star" style="background:#f2b731"></span>
          <span>Golf · not ringed ★</span>
        </li>`;
      div.innerHTML = `
        <div class="map-legend-head">
          <span>Property type</span>
          <span class="map-legend-caret">▾</span>
        </div>
        <ul class="map-legend-list">${rows}${golfStarRow}</ul>
      `;
      L.DomEvent.disableClickPropagation(div);
      L.DomEvent.disableScrollPropagation(div);
      div.querySelector(".map-legend-head").addEventListener("click", () => {
        div.classList.toggle("collapsed");
      });
      div.querySelectorAll(".map-legend-list li").forEach((li) => {
        li.addEventListener("click", (e) => {
          e.stopPropagation();
          if (li.dataset.ring) {
            // Golf-star row: apply the ring-test filter server-side.
            els.fRing.value = li.dataset.ring;
            loadParcels();
          } else if (li.dataset.propertyType) {
            els.fPropertyType.value = li.dataset.propertyType;
            rerender();
          }
        });
      });
      return div;
    };
    legendControl.addTo(map);
  }

  function initWelcomeBanner() {
    if (!els.welcomeBanner) return;
    let dismissed = false;
    try { dismissed = localStorage.getItem(WELCOME_STORAGE_KEY) === "1"; } catch {}
    els.welcomeBanner.hidden = dismissed;
    els.welcomeDismiss?.addEventListener("click", () => {
      els.welcomeBanner.hidden = true;
      try { localStorage.setItem(WELCOME_STORAGE_KEY, "1"); } catch {}
    });
  }

  function initSearch() {
    const onInput = debounce(() => {
      searchQuery = els.fSearch.value || "";
      els.fSearchClear.hidden = !searchQuery;
      rerender();
    }, 200);
    els.fSearch.addEventListener("input", onInput);
    els.fSearch.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { els.fSearch.value = ""; onInput(); }
    });
    els.fSearchClear.addEventListener("click", () => {
      els.fSearch.value = ""; searchQuery = "";
      els.fSearchClear.hidden = true; rerender(); els.fSearch.focus();
    });
  }

  function initWatchlist() {
    updateWatchlistCount();
    els.wlToggle?.addEventListener("click", () => {
      watchlistFilterOn = !watchlistFilterOn;
      els.wlToggle.setAttribute("aria-pressed", String(watchlistFilterOn));
      els.wlToggle.querySelector(".wl-icon").textContent = watchlistFilterOn ? "★" : "☆";
      rerender();
    });
    els.wlExport?.addEventListener("click", exportWatchlistCsv);
  }

  function initDrawer() {
    els.drawerClose?.addEventListener("click", closeDrawer);
    // ESC closes.
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && currentDrawerParcel) {
        closeDrawer();
      }
    });
  }

  // ------------------------------------------------------------------
  // View toggle (Map / List / How / Guide)
  // ------------------------------------------------------------------

  const VIEW_KEYS = ["map", "list", "bill", "how", "guide"];

  function switchToView(target) {
    document.querySelectorAll(".view-btn").forEach((b) => {
      const active = b.dataset.view === target;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    for (const key of VIEW_KEYS) {
      const el = document.getElementById(`view-${key}`);
      if (!el) continue;
      const active = key === target;
      el.classList.toggle("active", active);
      el.hidden = !active;
    }
    document.body.dataset.view = target;
    if (target === "map") map.invalidateSize();
    // Close the drawer when the user switches away from map/list — a
    // parcel detail isn't meaningful under the Intel tabs.
    if (target !== "map" && target !== "list") closeDrawer();
  }

  function initViewTabs() {
    document.querySelectorAll(".view-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchToView(btn.dataset.view));
    });
    document.querySelectorAll(".hero-jump").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        switchToView(a.dataset.jump);
      });
    });
  }

  function initApiBadge() {
    if (!els.apiBadge) return;
    try {
      const u = new URL(API_BASE);
      els.apiBadge.textContent = `API: ${u.host}`;
      els.apiBadge.title = API_BASE;
    } catch { els.apiBadge.textContent = `API: ${API_BASE}`; }
  }

  // ------------------------------------------------------------------
  // Topbar-height CSS var — used by the drawer so it sits below the header
  // ------------------------------------------------------------------

  function updateTopbarHeight() {
    const h = document.querySelector(".topbar")?.offsetHeight || 0;
    document.documentElement.style.setProperty("--topbar-h", h + "px");
  }
  window.addEventListener("resize", debounce(updateTopbarHeight, 100));

  // ------------------------------------------------------------------
  // Toast + loading helpers
  // ------------------------------------------------------------------

  function showLoading(on) { els.loading.classList.toggle("hidden", !on); }
  function showToast(msg) {
    els.toast.textContent = msg;
    els.toast.classList.remove("hidden");
    setTimeout(() => els.toast.classList.add("hidden"), 4500);
  }
  function hideToast() { els.toast.classList.add("hidden"); }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------

  document.body.dataset.view = "map";
  initFilters();
  initLegend();
  initApiBadge();
  initViewTabs();
  initWelcomeBanner();
  initSearch();
  initCopyDelegation();
  initWatchlist();
  initDrawer();
  initTableSort();
  updateTopbarHeight();
  loadParcels();
})();
