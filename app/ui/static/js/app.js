/* ============================================================
   CLIP Intelligence Platform — app.js
   Vanilla JS. No frameworks. Requires: Flask backend on :5001.
   ============================================================ */

// TODO: replace with server-side auth
const CLIP_PASSWORD = "CHANGEME";

// ── State ──────────────────────────────────────────────────
const APP = {
  currentView: "login",   // login | map | brief | runs
  scanState: null,        // { name, abbr } of selected state for scan panel
  briefRun: null,         // currently loaded run in brief view
  allRuns: [],            // cached run list for brief sidebar
  runsRefreshTimer: null, // auto-refresh handle for Live Runs view
  activeJobId: null,      // job_id of the currently polling scan
  pollTimer: null,        // handle for scan status polling interval
};

// ── Init ───────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
  createStars();
  setupLogin();
  setupNavLinks();
  setupScanPanel();
  setupBriefView();
  setupRunsView();
});

// ── Stars (CSS-animated, JS-positioned) ────────────────────
function createStars() {
  const container = document.getElementById("stars");
  const count = 75;
  for (let i = 0; i < count; i++) {
    const star = document.createElement("div");
    const size = Math.random() * 2.2 + 0.4;
    star.className = "star" + (i % 9 === 0 ? " pulsing" : "");
    star.style.cssText = [
      `left:${(Math.random() * 100).toFixed(2)}%`,
      `top:${(Math.random() * 100).toFixed(2)}%`,
      `width:${size.toFixed(2)}px`,
      `height:${size.toFixed(2)}px`,
      `opacity:${(Math.random() * 0.55 + 0.25).toFixed(2)}`,
      `animation-delay:${(Math.random() * 5).toFixed(2)}s`,
    ].join(";");
    container.appendChild(star);
  }
}

// ── Login ──────────────────────────────────────────────────
function setupLogin() {
  const form  = document.getElementById("login-form");
  const input = document.getElementById("password-input");

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (input.value === CLIP_PASSWORD) {
      animateLoginOut();
    } else {
      triggerShake(input);
    }
    input.value = "";
  });
}

function animateLoginOut() {
  const view = document.getElementById("view-login");
  view.classList.add("exit");
  view.addEventListener("animationend", () => {
    view.classList.add("hidden");
    showView("map");
    loadMapView();
  }, { once: true });
}

function triggerShake(input) {
  input.classList.remove("shake", "flash-gold");
  // Force reflow so animation re-triggers if called twice
  void input.offsetWidth;
  input.classList.add("shake", "flash-gold");
  input.addEventListener("animationend", () => {
    input.classList.remove("shake", "flash-gold");
  }, { once: true });
}

// ── View management ────────────────────────────────────────
function showView(name) {
  const views = { map: "view-map", brief: "view-brief", runs: "view-runs" };
  // Stop auto-refresh when leaving runs view
  if (name !== "runs" && APP.runsRefreshTimer) {
    clearTimeout(APP.runsRefreshTimer);
    APP.runsRefreshTimer = null;
  }
  Object.entries(views).forEach(([key, id]) => {
    const el = document.getElementById(id);
    if (key === name) {
      el.classList.remove("hidden");
      el.classList.add("fade-in");
      el.removeEventListener("animationend", clearFadeIn);
      el.addEventListener("animationend", clearFadeIn, { once: true });
    } else {
      el.classList.add("hidden");
    }
  });
  APP.currentView = name;
  updateFooterTabs(name);
}

function clearFadeIn(e) { e.currentTarget.classList.remove("fade-in"); }

function updateFooterTabs(view) {
  document.querySelectorAll(".footer-tab[data-view]").forEach((btn) => {
    const active = btn.getAttribute("data-view") === view;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// ── Nav links ──────────────────────────────────────────────
function setupNavLinks() {
  // Wordmark buttons → map
  ["btn-home", "btn-home-brief", "btn-home-runs"].forEach((id) => {
    document.getElementById(id)?.addEventListener("click", () => showView("map"));
  });

  // Header links
  document.getElementById("nav-to-brief")?.addEventListener("click", () => { showView("brief"); loadBriefSidebar(); });
  document.getElementById("nav-to-runs")?.addEventListener("click", () => { showView("runs"); loadRunsView(); });
  document.getElementById("nav-to-map")?.addEventListener("click", () => showView("map"));
  document.getElementById("nav-to-runs-from-brief")?.addEventListener("click", () => { showView("runs"); loadRunsView(); });
  document.getElementById("nav-runs-to-map")?.addEventListener("click", () => showView("map"));
  document.getElementById("nav-runs-to-brief")?.addEventListener("click", () => { showView("brief"); loadBriefSidebar(); });

  // Footer tabs — all instances via data-view attribute
  document.querySelectorAll(".footer-tab[data-view]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const v = btn.getAttribute("data-view");
      if (v === "brief") { showView("brief"); loadBriefSidebar(); }
      else if (v === "runs") { showView("runs"); loadRunsView(); }
      else showView("map");
    });
  });
}

// ═══════════════════════════════════════════════════════════
// MAP VIEW
// ═══════════════════════════════════════════════════════════

const STATE_ABBR = {
  "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
  "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
  "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
  "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
  "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
  "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
  "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
  "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
  "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
  "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
  "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
  "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
  "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
  "Puerto Rico": "PR",
};

let _mapLoaded = false;

async function loadMapView() {
  if (_mapLoaded) return;
  try {
    const resp = await fetch(
      "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"
    );
    if (!resp.ok) throw new Error("GeoJSON fetch failed");
    const geojson = await resp.json();
    renderMap(geojson);
    _mapLoaded = true;
  } catch (err) {
    console.error("Map load error:", err);
    const txt = document.getElementById("map-loading-text");
    if (txt) {
      txt.textContent = "Map unavailable — check network connection";
      txt.setAttribute("fill", "#a8b4d4");
    }
  }
}

// ── Projection ─────────────────────────────────────────────
function makeProjection(svgW, svgH) {
  const headerH = 68;
  const footerH = 52;
  const sidePad = 28;

  const drawH = svgH - headerH - footerH;
  const drawW = svgW - sidePad * 2;

  // Lower 48 geographic bounds
  const L48 = { minLon: -124.85, maxLon: -66.88, minLat: 24.40, maxLat: 49.38 };

  // Inset dimensions
  const akW = 185, akH = 130;
  const hiW = 120, hiH = 72;

  const AK_GEO = { minLon: -180, maxLon: -130, minLat: 51, maxLat: 71.5 };
  const HI_GEO = { minLon: -162, maxLon: -154, minLat: 18.8, maxLat: 22.4 };

  // Inset positions (bottom-left)
  const akArea = {
    x: sidePad + 8,
    y: svgH - footerH - akH - 8,
    w: akW, h: akH,
  };
  const hiArea = {
    x: sidePad + akW + 20,
    y: svgH - footerH - hiH - 8,
    w: hiW, h: hiH,
  };

  // L48 area — leave room for insets at bottom-left
  const l48Area = {
    x: sidePad,
    y: headerH,
    w: drawW,
    h: drawH,
  };

  function lin([lon, lat], geo, area) {
    const x = area.x + (lon - geo.minLon) / (geo.maxLon - geo.minLon) * area.w;
    const y = area.y + (1 - (lat - geo.minLat) / (geo.maxLat - geo.minLat)) * area.h;
    return [x, y];
  }

  return function project(abbr, coord) {
    let [lon, lat] = coord;
    if (abbr === "AK") {
      // Normalize antimeridian: Aleutian Islands may have lon > 0 wrapping
      if (lon > 0) lon -= 360;
      // Clamp to our AK bounds
      if (lon < AK_GEO.minLon || lon > AK_GEO.maxLon) return null;
      return lin([lon, lat], AK_GEO, akArea);
    }
    if (abbr === "HI") return lin([lon, lat], HI_GEO, hiArea);
    return lin([lon, lat], L48, l48Area);
  };
}

// ── Hardcoded geographic visual centers [lat, lon] ─────────
// Skipping AK and HI (inset-only, no labels per user preference).
// MI uses lower-peninsula center; NY uses upstate center (away from NYC).
const STATE_CENTERS = {
  AL: [32.75, -86.83], AZ: [34.29, -111.66], AR: [34.89, -92.44],
  CA: [37.25, -119.75], CO: [38.99, -105.55],
  FL: [28.50, -81.65], GA: [32.64, -83.44],
  ID: [44.45, -114.64], IL: [40.05, -89.20], IN: [39.90, -86.28],
  IA: [42.08, -93.50], KS: [38.52, -98.38], KY: [37.60, -84.87],
  LA: [31.50, -92.40], ME: [45.37, -69.24],
  MI: [43.80, -84.50], MN: [46.39, -94.64],
  MS: [32.75, -89.66], MO: [38.46, -92.53], MT: [47.03, -109.64],
  NE: [41.52, -99.79], NV: [39.36, -116.64],
  NM: [34.53, -106.25], NY: [42.75, -75.62],
  NC: [35.56, -79.39], ND: [47.46, -100.47], OH: [40.37, -82.74],
  OK: [35.58, -97.49], OR: [44.00, -120.55], PA: [40.82, -77.79],
  SC: [33.84, -80.90], SD: [44.37, -100.25],
  TN: [35.86, -86.35], TX: [31.47, -99.34], UT: [39.32, -111.09],
  VA: [37.52, -78.85], WA: [47.38, -120.50],
  WV: [38.64, -80.43], WI: [44.27, -89.62], WY: [42.79, -107.55],
};

// ── Leader line states: dot inside state, label off to the right ──
// All five NE labels stack at lon -67.8 (open Atlantic south of Maine's
// southern tip ~lat 43.1), spaced 0.9° apart so none overlap a state or
// each other.  NJ/DE/MD fan south along the mid-Atlantic shelf.
// Both positions are [lat, lon] geographic coordinates.
const LEADER_GEO = {
  VT: { dot: [44.07, -72.67], label: [42.7, -67.8] },
  NH: { dot: [43.68, -71.60], label: [41.8, -67.8] },
  MA: { dot: [42.27, -71.81], label: [40.9, -67.8] },
  RI: { dot: [41.68, -71.55], label: [40.0, -67.8] },
  CT: { dot: [41.65, -72.73], label: [39.1, -67.8] },
  NJ: { dot: [40.22, -74.50], label: [38.3, -70.5] },
  DE: { dot: [38.99, -75.50], label: [37.4, -72.5] },
  MD: { dot: [39.00, -76.70], label: [36.6, -74.0] },
};

// Project a [lat, lon] geographic point through the L48 projection
function geoProject(project, lat, lon) {
  return project("__L48__", [lon, lat]);
}

// ── GeoJSON → SVG path ─────────────────────────────────────
function geomToPath(geometry, abbr, project) {
  const polygons =
    geometry.type === "Polygon"
      ? [geometry.coordinates]
      : geometry.coordinates; // MultiPolygon

  const parts = [];
  for (const polygon of polygons) {
    for (const ring of polygon) {
      const pts = ring
        .map((coord) => project(abbr, coord))
        .filter(Boolean); // remove null (out-of-bounds AK island tips)
      if (pts.length < 3) continue;
      parts.push(
        `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}` +
        pts.slice(1).map((p) => `L${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("") +
        "Z"
      );
    }
  }
  return parts.join(" ");
}

// ── Render map ─────────────────────────────────────────────
function renderMap(geojson) {
  const svg         = document.getElementById("us-map");
  const statesGroup = document.getElementById("states-group");
  const labelsGroup = document.getElementById("labels-group");
  const insetGroup  = document.getElementById("inset-group");

  // Use SVG viewBox dimensions for projection
  const vb = svg.viewBox.baseVal;
  const project = makeProjection(vb.width, vb.height);

  // Remove loading text
  const loadingText = document.getElementById("map-loading-text");
  if (loadingText) loadingText.remove();

  // Draw inset border boxes
  drawInsetBox(insetGroup, project, "AK", "AK");
  drawInsetBox(insetGroup, project, "HI", "HI");

  // Single overlay path above all state fills — hover gold stroke goes here so
  // it is never clipped by a neighboring state's fill on shared edges.
  const hoverOverlay = document.createElementNS("http://www.w3.org/2000/svg", "path");
  hoverOverlay.setAttribute("id", "hover-overlay");
  hoverOverlay.setAttribute("fill", "none");
  hoverOverlay.setAttribute("stroke", "#f5c842");
  hoverOverlay.setAttribute("stroke-width", "2");
  hoverOverlay.setAttribute("vector-effect", "non-scaling-stroke");
  hoverOverlay.setAttribute("pointer-events", "none");
  hoverOverlay.style.opacity = "0";

  // Draw each state
  for (const feature of geojson.features) {
    const name = feature.properties?.name || "";
    const abbr = STATE_ABBR[name] || name.slice(0, 2).toUpperCase();

    const pathData = geomToPath(feature.geometry, abbr, project);
    if (!pathData) continue;

    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", pathData);
    path.setAttribute("data-state", name);
    path.setAttribute("data-abbr", abbr);
    path.setAttribute("class", "state-path");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    path.setAttribute("role", "button");
    path.setAttribute("tabindex", "0");
    path.setAttribute("aria-label", `${name} — click to set up a scan`);

    path.addEventListener("mouseover", () => {
      hoverOverlay.setAttribute("d", path.getAttribute("d"));
      hoverOverlay.style.opacity = "1";
    });
    path.addEventListener("mouseout", () => { hoverOverlay.style.opacity = "0"; });

    // Move to end of statesGroup on activation so the gold stroke renders
    // on top of all neighbor fills (SVG paints in DOM order).
    // Done only on click/keydown — not mouseenter — to avoid disrupting
    // the browser's click event sequence.
    path.addEventListener("click", () => {
      statesGroup.appendChild(path);
      onStateClick(name, abbr);
    });
    path.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        statesGroup.appendChild(path);
        onStateClick(name, abbr);
      }
    });

    statesGroup.appendChild(path);

    // ── Labels go into labelsGroup so they always render above fills ──
    if (abbr === "AK" || abbr === "HI" || abbr === "DC" || abbr === "PR") continue;

    const leaderCfg = LEADER_GEO[abbr];
    const center    = STATE_CENTERS[abbr];

    if (leaderCfg) {
      const dotPt = geoProject(project, leaderCfg.dot[0],   leaderCfg.dot[1]);
      const labPt = geoProject(project, leaderCfg.label[0], leaderCfg.label[1]);
      if (!dotPt || !labPt) continue;

      const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("cx", dotPt[0].toFixed(1));
      dot.setAttribute("cy", dotPt[1].toFixed(1));
      dot.setAttribute("r", "1.8");
      dot.setAttribute("class", "leader-dot");
      labelsGroup.appendChild(dot);

      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("x1", dotPt[0].toFixed(1));
      line.setAttribute("y1", dotPt[1].toFixed(1));
      line.setAttribute("x2", labPt[0].toFixed(1));
      line.setAttribute("y2", labPt[1].toFixed(1));
      line.setAttribute("class", "leader-line");
      labelsGroup.appendChild(line);

      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "state-label");
      label.setAttribute("x", labPt[0].toFixed(1));
      label.setAttribute("y", labPt[1].toFixed(1));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("dominant-baseline", "central");
      label.textContent = abbr;
      labelsGroup.appendChild(label);

    } else if (center) {
      const pt = geoProject(project, center[0], center[1]);
      if (!pt) continue;

      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "state-label");
      label.setAttribute("x", pt[0].toFixed(1));
      label.setAttribute("y", pt[1].toFixed(1));
      label.setAttribute("text-anchor", "middle");
      label.setAttribute("dominant-baseline", "central");
      label.textContent = abbr;
      labelsGroup.appendChild(label);
    }
  }
  svg.appendChild(hoverOverlay);
}

function drawInsetBox(group, project, abbr, label) {
  // Project a known coord in the inset to find its area bounds visually
  const corners = {
    AK: [[-180, 51], [-130, 71.5]],
    HI: [[-162, 18.8], [-154, 22.4]],
  };
  const [tl, br] = corners[abbr].map((c) => project(abbr, c));
  if (!tl || !br) return;

  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("class", "inset-box");
  rect.setAttribute("x",      Math.min(tl[0], br[0]));
  rect.setAttribute("y",      Math.min(tl[1], br[1]));
  rect.setAttribute("width",  Math.abs(br[0] - tl[0]));
  rect.setAttribute("height", Math.abs(br[1] - tl[1]));
  group.appendChild(rect);

  const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
  text.setAttribute("class", "inset-label");
  text.setAttribute("x", Math.min(tl[0], br[0]) + 4);
  text.setAttribute("y", Math.min(tl[1], br[1]) - 3);
  text.textContent = label;
  group.appendChild(text);
}

// ═══════════════════════════════════════════════════════════
// SCAN PANEL
// ═══════════════════════════════════════════════════════════

function onStateClick(stateName, stateAbbr) {
  APP.scanState = { name: stateName, abbr: stateAbbr };

  // Update panel heading
  document.getElementById("panel-state-label").textContent = stateName;
  document.querySelector("#panel-scan .panel-subheading").textContent = "New Scan";

  // Highlight active state on map
  document.querySelectorAll(".state-path").forEach((p) => {
    p.classList.toggle("active", p.getAttribute("data-abbr") === stateAbbr);
  });

  // Dim the map
  document.getElementById("map-container").classList.add("map-dimmed");

  openPanel();
  loadCitiesForPanel(stateAbbr);
}

function openPanel() {
  const panel = document.getElementById("panel-scan");
  panel.hidden = false;
  // Allow display:flex to apply before animating
  requestAnimationFrame(() => {
    requestAnimationFrame(() => panel.classList.add("open"));
  });
  panel.focus();
}

function closePanel() {
  _cbClosePanel();
  const panel = document.getElementById("panel-scan");
  panel.classList.remove("open");
  document.getElementById("map-container").classList.remove("map-dimmed");
  document.querySelectorAll(".state-path.active").forEach((p) => p.classList.remove("active"));
  panel.addEventListener("transitionend", () => {
    panel.hidden = true;
  }, { once: true });
  APP.scanState = null;
}

function setupScanPanel() {
  document.getElementById("btn-close-panel").addEventListener("click", () => {
    _stopPoll();
    closePanel();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && APP.currentView === "map") closePanel();
  });

  // Generic toggle groups (scope / mode / depth)
  document.querySelectorAll(".toggle-btn[data-group]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const group = btn.getAttribute("data-group");
      document.querySelectorAll(`.toggle-btn[data-group="${group}"]`).forEach((b) => {
        b.classList.remove("selected");
        b.setAttribute("aria-pressed", "false");
      });
      btn.classList.add("selected");
      btn.setAttribute("aria-pressed", "true");

      // Scope toggle: show/hide city dropdown
      if (group === "scope") {
        const isState = btn.getAttribute("data-value") === "state";
        const cityGroup = document.getElementById("city-field-group");
        const scopeNote  = document.getElementById("scope-note");
        cityGroup.classList.toggle("hidden", isState);
        if (isState && APP.scanState) {
          scopeNote.textContent = `All communities in ${APP.scanState.name} will be scanned.`;
          scopeNote.classList.remove("hidden");
        } else {
          scopeNote.classList.add("hidden");
        }
      }

      // Mode toggle: show/hide depth + mode-num (community only); zip-version (zip only)
      if (group === "mode") {
        const isZip = btn.getAttribute("data-value") === "zip";
        const depthGroup   = document.getElementById("depth-field-group");
        const modeNumRow   = document.getElementById("mode-num-row");
        const zipVersionRow = document.getElementById("zip-version-row");
        if (depthGroup)    depthGroup.classList.toggle("hidden", isZip);
        if (modeNumRow)    modeNumRow.style.display    = isZip ? "none" : "";
        if (zipVersionRow) zipVersionRow.style.display = isZip ? ""     : "none";

        // ZIP drill is city-only — force Single City and disable the Entire State button
        const stateScopeBtn = document.querySelector('.toggle-btn[data-group="scope"][data-value="state"]');
        const cityScopeBtn  = document.querySelector('.toggle-btn[data-group="scope"][data-value="city"]');
        if (isZip) {
          if (stateScopeBtn) stateScopeBtn.disabled = true;
          // If Entire State was selected, switch back to Single City
          if (stateScopeBtn?.classList.contains("selected")) {
            cityScopeBtn?.click();
          }
        } else {
          if (stateScopeBtn) stateScopeBtn.disabled = false;
        }
      }
    });
  });

  // Advanced disclosure
  const disclosureBtn = document.getElementById("disclosure-btn");
  const advancedBody  = document.getElementById("advanced-body");
  disclosureBtn.addEventListener("click", () => {
    const expanded = disclosureBtn.getAttribute("aria-expanded") === "true";
    disclosureBtn.setAttribute("aria-expanded", String(!expanded));
    advancedBody.classList.toggle("expanded", !expanded);
  });

  document.getElementById("btn-run-scan").addEventListener("click", submitScan);
  document.getElementById("btn-scan-new").addEventListener("click", _resetScanPanel);
  try {
    _cbInit();
  } catch (err) {
    console.error("[CLIP] Combobox init failed:", err);
  }
}

// Fetch cities with up to `retries` attempts and `delayMs` between each.
// Returns the parsed JSON array on success; throws on permanent failure.
async function fetchCitiesWithRetry(stateAbbr, retries = 3, delayMs = 500) {
  const url = `/api/cities?state=${encodeURIComponent(stateAbbr)}`;
  let lastErr;
  for (let i = 0; i < retries; i++) {
    try {
      const r = await fetch(url);
      if (r.ok) return await r.json();
      lastErr = new Error(`HTTP ${r.status}`);
    } catch (e) {
      lastErr = e;
    }
    if (i < retries - 1) await new Promise((res) => setTimeout(res, delayMs));
  }
  throw lastErr || new Error("cities fetch failed");
}

async function loadCitiesForPanel(stateAbbr) {
  const searchInput = document.getElementById("scan-city-search");
  const hiddenInput = document.getElementById("scan-city");
  searchInput.value = "";
  searchInput.placeholder = "Loading…";
  searchInput.disabled = true;
  hiddenInput.value = "";
  _cbSelected = null;
  _cbClosePanel();

  try {
    const cities = await fetchCitiesWithRetry(stateAbbr);
    if (!Array.isArray(cities)) throw new Error("unexpected response shape");
    console.log(`[CLIP] Cities loaded: ${stateAbbr}=${cities.length}`);
    _cbBuild(cities, APP.scanState?.name || stateAbbr);
    searchInput.placeholder = cities.length ? "Search cities…" : "No cities available";
    searchInput.disabled = !cities.length;
  } catch (err) {
    console.error(`[CLIP] cities fetch failed for ${stateAbbr}:`, err);
    _cbBuild([], stateAbbr);
    // Show a visible error inside the panel (bypass _cbOpenPanel's empty-cities guard)
    const panel = document.getElementById("city-listbox");
    const errEl = document.createElement("div");
    errEl.className = "combobox-empty";
    errEl.textContent = "Could not load cities — please refresh";
    panel.appendChild(errEl);
    const r = searchInput.getBoundingClientRect();
    panel.style.top   = (r.bottom + 3) + "px";
    panel.style.left  = r.left + "px";
    panel.style.width = r.width + "px";
    panel.removeAttribute("hidden");
    searchInput.setAttribute("aria-expanded", "true");
    _cbOpen = true;
    searchInput.placeholder = "Could not load cities";
    searchInput.disabled = false;
  }
}

async function submitScan() {
  const state   = APP.scanState;
  const scope   = document.querySelector('.toggle-btn[data-group="scope"].selected')?.getAttribute("data-value") || "city";
  const isStateWide = scope === "state";
  const city    = document.getElementById("scan-city").value;
  const mode    = document.querySelector('.toggle-btn[data-group="mode"].selected')?.getAttribute("data-value") || "community";
  const depth   = document.querySelector('.toggle-btn[data-group="depth"].selected')?.getAttribute("data-value") || "standard";
  const dryRun  = document.getElementById("flag-dry-run").checked;
  const mock    = document.getElementById("flag-mock").checked;
  const noCache = document.getElementById("flag-no-cache").checked;
  const forceRefresh = document.getElementById("flag-force-refresh").checked;
  const batch   = document.getElementById("flag-batch").checked;
  const preset      = document.getElementById("adv-preset").value;
  const modeNum     = document.getElementById("adv-mode-num").value;
  const zipVersion  = document.getElementById("adv-zip-version")?.value || "v1";

  if (!isStateWide && !city) {
    showToast("Please select a city.", "error");
    return;
  }

  const body = {
    target: isStateWide ? (state?.abbr.toLowerCase() || "") : city,
    state: state?.abbr || "",
    all: isStateWide,
    mode, depth,
    dry_run: dryRun, mock, no_cache: noCache,
    force_refresh: forceRefresh, batch,
    preset, mode_num: modeNum,
    zip_version: zipVersion,
  };

  const btn = document.getElementById("btn-run-scan");
  btn.disabled = true;
  btn.textContent = "Queuing…";

  try {
    const resp = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (!resp.ok) {
      showToast(data.error || "Scan rejected by server.", "error");
      return;
    }

    APP.activeJobId = data.job_id;
    _showScanProgress(body.target, isStateWide ? "Statewide" : city);
    _startPoll(data.job_id);

  } catch {
    showToast("Failed to queue scan. Check the server.", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Scan";
  }
}

// ── Scan job polling ────────────────────────────────────────

function _showScanProgress(communityId, displayName) {
  _renderedLogLines = 0;
  document.getElementById("btn-view-log").style.display = "none";
  document.querySelector(".panel-body").style.display = "none";
  const jobPanel = document.getElementById("scan-job-panel");
  jobPanel.classList.remove("hidden");
  document.getElementById("scan-job-community").textContent = displayName || communityId;
  document.getElementById("scan-job-log").textContent = "";
  document.getElementById("scan-job-error").classList.add("hidden");
  document.getElementById("scan-job-error").textContent = "";
  _setBadge("queued");
}

// Called on explicit "New Scan" — wipes log, clears stored log, hides View Log button.
function _resetScanPanel() {
  _stopPoll();
  APP.activeJobId = null;
  _lastCompletedLog = null;
  document.getElementById("scan-job-panel").classList.add("hidden");
  document.getElementById("scan-job-log").textContent = "";
  document.getElementById("scan-job-error").classList.add("hidden");
  document.querySelector(".panel-body").style.display = "";
  document.getElementById("btn-view-log").style.display = "none";
}

// Called after a successful scan — hides panel without wiping log or clearing stored log.
function _closeScanPanelAfterScan() {
  _stopPoll();
  APP.activeJobId = null;
  document.getElementById("scan-job-panel").classList.add("hidden");
  document.getElementById("scan-job-error").classList.add("hidden");
  document.querySelector(".panel-body").style.display = "";
}

function _setBadge(status) {
  const badge = document.getElementById("scan-job-badge");
  badge.textContent = status.charAt(0).toUpperCase() + status.slice(1);
  badge.className = "scan-job-badge " + status;
}

function _startPoll(jobId) {
  _stopPoll();
  APP.pollTimer = setInterval(() => _pollOnce(jobId), 3000);
}

function _stopPoll() {
  if (APP.pollTimer) {
    clearInterval(APP.pollTimer);
    APP.pollTimer = null;
  }
}

let _renderedLogLines = 0;
let _lastCompletedLog = null; // { jobId, communityId, lines }

async function _pollOnce(jobId) {
  try {
    const resp = await fetch(`/api/scan/status/${encodeURIComponent(jobId)}`);
    if (!resp.ok) { _stopPoll(); return; }
    const job = await resp.json();

    _setBadge(job.status);
    _renderNewLogLines(job.stage_log || []);

    if (job.status === "complete") {
      _lastCompletedLog = { jobId, communityId: job.community_id, lines: job.stage_log || [] };
      _stopPoll();
      _onScanComplete(jobId);
    } else if (job.status === "failed") {
      _stopPoll();
      const errEl = document.getElementById("scan-job-error");
      errEl.textContent = job.error || "Scan failed — check server logs.";
      errEl.classList.remove("hidden");
    }
  } catch {
    // network hiccup — keep polling
  }
}

function _renderNewLogLines(allLines) {
  const logEl = document.getElementById("scan-job-log");
  const newLines = allLines.slice(_renderedLogLines);
  _renderedLogLines = allLines.length;

  for (const raw of newLines) {
    const span = document.createElement("span");
    span.className = "scan-log-line";

    if (/Running\s+s\d_[a-z_]+\.\.\./.test(raw)) {
      span.classList.add("scan-log-stage");
    } else if (/—\s+(OK|cache hit)/.test(raw)) {
      span.classList.add("scan-log-ok");
    } else if (/FAILED/.test(raw)) {
      span.classList.add("scan-log-fail");
    }

    span.textContent = raw;
    logEl.appendChild(span);
  }

  if (newLines.length) {
    logEl.scrollTop = logEl.scrollHeight;
  }
}

async function _onScanComplete(jobId) {
  showToast("Scan complete — loading brief…", "success");
  try {
    const resp = await fetch(`/api/scan/result/${encodeURIComponent(jobId)}`);
    if (resp.status === 404) {
      showToast("Scan finished but no brief was produced.", "error");
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast(err.error || "Could not load brief.", "error");
      return;
    }
    const html = await resp.text();

    // Hide scan panel without wiping the log (log preserved in _lastCompletedLog).
    _closeScanPanelAfterScan();
    closePanel();
    APP.briefRun = {
      state_code: _lastCompletedLog.communityId.slice(0, 2).toUpperCase(),
      community_id: _lastCompletedLog.communityId
    };
    showView("brief");

    // Reveal View Log button now that we have a completed log.
    document.getElementById("btn-view-log").style.display = "";

    const frame   = document.getElementById("brief-frame");
    const loading = document.getElementById("brief-loading");
    const empty   = document.getElementById("brief-empty");
    const content = document.getElementById("brief-content-wrap");

    empty.classList.add("hidden");
    content.classList.remove("hidden");
    content.style.display = "flex";
    loading.classList.add("hidden");
    frame.srcdoc = html;
    frame.style.display = "block";

  } catch {
    showToast("Scan complete — brief load failed. Check Brief View.", "error");
  }
}

// ── Log modal ───────────────────────────────────────────────

function _showLogModal() {
  if (!_lastCompletedLog) return;
  const title = document.getElementById("log-modal-title");
  title.textContent = `Scan Log — ${_lastCompletedLog.communityId}`;

  const body = document.getElementById("log-modal-body");
  body.textContent = "";
  for (const raw of _lastCompletedLog.lines) {
    const span = document.createElement("span");
    span.className = "scan-log-line";
    if (/Running\s+s\d_[a-z_]+\.\.\./.test(raw))   span.classList.add("scan-log-stage");
    else if (/—\s+(OK|cache hit)/.test(raw))        span.classList.add("scan-log-ok");
    else if (/FAILED/.test(raw))                    span.classList.add("scan-log-fail");
    span.textContent = raw;
    body.appendChild(span);
  }

  document.getElementById("log-modal").classList.remove("hidden");
  body.scrollTop = 0;
}

function _closeLogModal() {
  document.getElementById("log-modal").classList.add("hidden");
}

// ═══════════════════════════════════════════════════════════
// BRIEF VIEW
// ═══════════════════════════════════════════════════════════

function setupBriefView() {
  // State dropdown change
  document.getElementById("brief-state-select").addEventListener("change", async (e) => {
    APP.allRuns = [];
    await refreshRunList(e.target.value);
  });

  // City search filter
  document.getElementById("city-search").addEventListener("input", (e) => {
    filterCityList(e.target.value);
  });

  // View Log button
  document.getElementById("btn-view-log").addEventListener("click", _showLogModal);

  // Log modal close
  document.getElementById("btn-close-log-modal").addEventListener("click", _closeLogModal);
  document.getElementById("log-modal-backdrop").addEventListener("click", _closeLogModal);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") _closeLogModal();
  });

  // "Run New Scan" from brief toolbar
  document.getElementById("btn-brief-new-scan").addEventListener("click", () => {
    if (APP.briefRun) {
      const abbr = APP.briefRun.state_code;
      const name = stateAbbrToName(abbr);
      APP.scanState = { name, abbr };
      showView("map");
      setTimeout(() => {
        document.getElementById("panel-state-label").textContent = name;
        document.getElementById("map-container").classList.add("map-dimmed");
        openPanel();
        loadCitiesForPanel(abbr);
      }, 350);
    }
  });
}

async function loadBriefSidebar() {
  // Populate state dropdown from /api/states if empty
  const stateSelect = document.getElementById("brief-state-select");
  if (stateSelect.options.length <= 1) {
    try {
      const resp = await fetch("/api/states");
      const states = await resp.json();
      states.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.code;
        opt.textContent = s.name;
        stateSelect.appendChild(opt);
      });
    } catch {
      console.error("Failed to load states");
    }
  }
  // Auto-load runs if a state is already selected
  if (stateSelect.value) {
    await refreshRunList(stateSelect.value);
  } else {
    await refreshRunList("");
  }
}

async function refreshRunList(stateCode) {
  const list = document.getElementById("city-run-list");
  list.innerHTML = '<li style="color:var(--mist);font-size:12px;padding:12px 14px;">Loading…</li>';

  const url = stateCode ? `/api/runs?state=${encodeURIComponent(stateCode)}` : "/api/runs";
  try {
    const resp = await fetch(url);
    APP.allRuns = await resp.json();
    renderRunList(APP.allRuns);
  } catch {
    list.innerHTML = '<li style="color:var(--mist);font-size:12px;padding:12px 14px;">Error loading runs</li>';
  }
}

function renderRunList(runs) {
  const list = document.getElementById("city-run-list");
  if (!runs.length) {
    list.innerHTML = '<li style="color:var(--mist);font-size:12px;padding:12px 14px;">No scan history found</li>';
    return;
  }
  list.innerHTML = runs.map((run) => `
    <li
      class="city-row${APP.briefRun?.run_id === run.run_id ? " selected" : ""}"
      role="option"
      aria-selected="${APP.briefRun?.run_id === run.run_id}"
      tabindex="0"
      data-run-id="${esc(run.run_id)}"
    >
      <span class="city-row-name">${esc(run.city)}</span>
      <span class="city-row-meta">${run.state_code} · ${run.date || "no date"} · ${esc(formatDisplay(run.preset))}${run.has_brief ? "" : " · no brief"}</span>
    </li>
  `).join("");

  list.querySelectorAll(".city-row").forEach((row) => {
    const runId = row.getAttribute("data-run-id");
    const handler = () => loadBrief(runId);
    row.addEventListener("click", handler);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); handler(); }
    });
  });
}

function filterCityList(query) {
  const q = query.toLowerCase().trim();
  const filtered = q ? APP.allRuns.filter((r) => r.city.toLowerCase().includes(q)) : APP.allRuns;
  renderRunList(filtered);
}

async function loadBrief(runId) {
  const run = APP.allRuns.find((r) => r.run_id === runId);
  if (!run) return;

  APP.briefRun = run;

  // Update city list selection
  document.querySelectorAll(".city-row").forEach((row) => {
    const sel = row.getAttribute("data-run-id") === runId;
    row.classList.toggle("selected", sel);
    row.setAttribute("aria-selected", String(sel));
  });

  // Show toolbar + content area
  const empty   = document.getElementById("brief-empty");
  const content = document.getElementById("brief-content-wrap");
  empty.classList.add("hidden");
  content.classList.remove("hidden");
  content.style.display = "flex";

  document.getElementById("brief-city-name").textContent = run.city;
  document.getElementById("brief-scan-date").textContent =
    run.date ? `Scan date: ${run.date}` : "No scan date";

  // Show loading state
  const frame   = document.getElementById("brief-frame");
  const loading = document.getElementById("brief-loading");
  frame.style.display = "none";
  loading.classList.remove("hidden");

  // If has_brief looks stale, do one fresh status check before wiring UI.
  // Handles the case where APP.allRuns was loaded before the scan completed.
  if (!run.has_brief) {
    try {
      const sr = await fetch(`/api/scan/status/${encodeURIComponent(runId)}`);
      if (sr.ok) {
        const fresh = await sr.json();
        if (fresh.has_brief) run.has_brief = true;
      }
    } catch { /* ignore — keep stale value */ }
  }

  // Wire up Download PDF button — only for community runs (not zip drill)
  const pdfBtn = document.getElementById("btn-download-pdf");
  if (pdfBtn) {
    const isZip = (run.mode || "") === "zip";
    if (!isZip && run.has_brief) {
      pdfBtn.href = `/api/brief/pdf?run_id=${encodeURIComponent(runId)}`;
      pdfBtn.style.display = "";
    } else {
      pdfBtn.style.display = "none";
    }
  }

  if (!run.has_brief) {
    loading.textContent = "No brief available for this scan.";
    return;
  }

  // Load brief into iframe via srcdoc (avoids same-origin issues)
  try {
    const resp = await fetch(`/api/brief?run_id=${encodeURIComponent(runId)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const html = await resp.text();
    frame.srcdoc = html;
    loading.classList.add("hidden");
    frame.style.display = "block";
  } catch (err) {
    loading.textContent = `Failed to load brief: ${err.message}`;
  }
}

function stateAbbrToName(abbr) {
  const entry = Object.entries(STATE_ABBR).find(([, a]) => a === abbr);
  return entry ? entry[0] : abbr;
}

// ═══════════════════════════════════════════════════════════
// LIVE RUNS VIEW
// ═══════════════════════════════════════════════════════════

function setupRunsView() {
  document.getElementById("btn-refresh-runs").addEventListener("click", () => loadRunsView());
}

async function loadRunsView() {
  const grid        = document.getElementById("runs-grid");
  const lastUpdated = document.getElementById("runs-last-updated");

  grid.innerHTML = '<div class="runs-empty">Loading…</div>';

  try {
    const resp = await fetch("/api/runs");
    const runs = await resp.json();
    renderRunCards(runs);
    lastUpdated.textContent = `Updated ${new Date().toLocaleTimeString()}`;

    // Auto-refresh while any run is in progress
    const hasRunning = runs.some((r) => r.status === "running");
    if (hasRunning && APP.currentView === "runs") {
      APP.runsRefreshTimer = setTimeout(loadRunsView, 8000);
    }
  } catch {
    grid.innerHTML = '<div class="runs-empty">Error loading runs — check server connection</div>';
  }
}

function renderRunCards(runs) {
  const grid = document.getElementById("runs-grid");
  if (!runs.length) {
    grid.innerHTML = '<div class="runs-empty">No scan runs found</div>';
    return;
  }

  const statusLabel = { running: "Running", done: "Done", failed: "Failed", unknown: "Unknown" };
  const modeLabel   = { "1": "Statewide Scan", "2": "Community Brief" };

  grid.innerHTML = runs.map((run) => {
    const st     = run.status || "unknown";
    const preset = formatDisplay(run.preset || "");
    const mode   = modeLabel[run.mode] || `Mode ${run.mode}`;
    return `
      <div class="run-card">
        <div class="run-status-bar ${st}"></div>
        <div class="run-card-body">
          <div class="run-card-top">
            <div class="run-card-city">${esc(run.city)}</div>
            <span class="run-state-chip">${esc(run.state_code)}</span>
          </div>
          <div class="run-card-meta">${esc(preset)} · ${esc(mode)} · ${run.date || "no date"}</div>
        </div>
        <div class="run-card-footer">
          <span class="status-badge ${st}">${statusLabel[st] || st}</span>
          ${run.has_brief
            ? `<button class="btn-view-brief-sm" data-run-id="${esc(run.run_id)}">View Brief</button>
               ${run.mode !== "zip" ? `<a class="btn-outline-sm" href="/api/brief/pdf?run_id=${esc(run.run_id)}" style="text-decoration:none;">PDF</a>` : ""}`
            : `<span style="font-size:11px;color:var(--mist);opacity:0.55;">No brief yet</span>`}
        </div>
      </div>`;
  }).join("");

  grid.querySelectorAll(".btn-view-brief-sm").forEach((btn) => {
    btn.addEventListener("click", () => {
      const runId = btn.getAttribute("data-run-id");
      showView("brief");
      loadBriefSidebar().then(() => loadBrief(runId));
    });
  });
}

// ── Format display ─────────────────────────────────────────
function formatDisplay(str) {
  return String(str || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Toast ──────────────────────────────────────────────────
let _toastTimer = null;

function showToast(message, type = "info") {
  const toast = document.getElementById("toast");
  toast.textContent = message;
  toast.className = `toast visible${type !== "info" ? " " + type : ""}`;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    toast.classList.remove("visible");
  }, 4000);
}

// ── HTML escape ────────────────────────────────────────────
function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ═══════════════════════════════════════════════════════════
// COMBOBOX — searchable city selector (scan panel)
// ═══════════════════════════════════════════════════════════

let _cbCities    = [];          // [{slug, name}] for the currently-loaded state
let _cbSelected  = null;        // {slug, name} of the confirmed selection
let _cbActiveIdx = -1;          // index within currently-visible options
let _cbOpen      = false;
let _cbDebounce  = null;

function _cbInit() {
  const input = document.getElementById("scan-city-search");
  const panel = document.getElementById("city-listbox");
  if (!input || !panel) return;

  // backdrop-filter on #panel-scan creates a containing block for position:fixed
  // descendants, making their coordinates panel-relative instead of viewport-relative.
  // Moving the listbox to body restores correct fixed positioning.
  document.body.appendChild(panel);

  // Open on focus if cities are loaded
  input.addEventListener("focus", () => {
    console.log(`[CLIP] input focus event: _cbCities.length=${_cbCities.length}, _cbOpen=${_cbOpen}`);
    if (_cbCities.length > 0 && !_cbOpen) {
      console.log("[CLIP] opening panel from focus event");
      _cbOpenPanel();
    }
  });

  // Filter with 50ms debounce; open if not already open
  input.addEventListener("input", () => {
    clearTimeout(_cbDebounce);
    _cbDebounce = setTimeout(() => {
      console.log(`[CLIP] input event debounce fired, filtering query="${input.value}"`);
      _cbFilter(input.value);
      console.log(`[CLIP] after filter: _cbOpen=${_cbOpen}, _cbCities.length=${_cbCities.length}`);
      if (!_cbOpen && _cbCities.length > 0) {
        console.log("[CLIP] opening panel from input event");
        _cbOpenPanel();
      } else {
        console.log(`[CLIP] NOT opening panel: _cbOpen=${_cbOpen}, _cbCities.length=${_cbCities.length}`);
      }
    }, 50);
  });

  input.addEventListener("keydown", _cbKeydown);

  // Close on outside click (capture phase so it fires before focus events)
  document.addEventListener("click", (e) => {
    const wrapper = input.closest(".combobox-wrapper");
    if (!wrapper?.contains(e.target) && !panel.contains(e.target)) _cbClosePanel();
  }, true);

  // Close if the scan panel scrolls (panel is position:fixed, would drift)
  document.getElementById("panel-scan")?.addEventListener("scroll", () => {
    if (_cbOpen) _cbClosePanel();
  });
}

function _cbBuild(cities, stateName) {
  _cbCities    = cities;
  _cbActiveIdx = -1;
  const panel  = document.getElementById("city-listbox");
  panel.innerHTML = "";
  if (!cities.length) return;

  // State group header
  const header = document.createElement("div");
  header.className = "combobox-group-header";
  header.setAttribute("role", "presentation");
  header.setAttribute("aria-hidden", "true");
  header.dataset.role = "group-header";
  header.textContent = stateName || "";
  panel.appendChild(header);

  cities.forEach((city) => {
    const opt = document.createElement("div");
    opt.className = "combobox-option";
    opt.setAttribute("role", "option");
    opt.setAttribute("aria-selected", city.slug === _cbSelected?.slug ? "true" : "false");
    opt.dataset.slug = city.slug;
    opt.dataset.name = city.name;
    // Display city name without the ", STATE" suffix — the group header carries state context
    opt.textContent = city.name.split(",")[0];
    // mousedown instead of click so the blur on the input fires after we record selection
    opt.addEventListener("mousedown", (e) => { e.preventDefault(); _cbSelect(city); });
    panel.appendChild(opt);
  });
}

function _cbFilter(query) {
  const q     = query.toLowerCase().trim();
  const panel = document.getElementById("city-listbox");
  let anyVisible = false;

  panel.querySelectorAll(".combobox-option").forEach((opt) => {
    const matches = !q || opt.dataset.name.toLowerCase().includes(q);
    if (matches) {
      opt.removeAttribute("hidden");
    } else {
      opt.setAttribute("hidden", "");
    }
    if (matches) anyVisible = true;
  });

  // Show/hide the group header with the options
  const header = panel.querySelector("[data-role='group-header']");
  if (header) {
    if (anyVisible) {
      header.removeAttribute("hidden");
    } else {
      header.setAttribute("hidden", "");
    }
  }

  // Empty-state notice
  let emptyEl = panel.querySelector(".combobox-empty");
  if (!anyVisible) {
    if (!emptyEl) {
      emptyEl = document.createElement("div");
      emptyEl.className = "combobox-empty";
      emptyEl.textContent = "No cities found";
      panel.appendChild(emptyEl);
    }
    emptyEl.removeAttribute("hidden");
  } else if (emptyEl) {
    emptyEl.setAttribute("hidden", "");
  }

  // Reset active index on each filter pass
  _cbActiveIdx = -1;
  _cbClearActive(panel);
}

function _cbKeydown(e) {
  const panel = document.getElementById("city-listbox");

  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (!_cbOpen) _cbOpenPanel();
    const visible = _cbVisibleOptions(panel);
    if (!visible.length) return;
    _cbActiveIdx = Math.min(_cbActiveIdx + 1, visible.length - 1);
    _cbSetActive(visible);

  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    const visible = _cbVisibleOptions(panel);
    if (!visible.length) return;
    _cbActiveIdx = Math.max(_cbActiveIdx - 1, 0);
    _cbSetActive(visible);

  } else if (e.key === "Enter") {
    e.preventDefault();
    // Flush any pending debounce so Enter always sees the current filter state
    if (_cbDebounce) {
      clearTimeout(_cbDebounce);
      _cbDebounce = null;
      _cbFilter(e.target.value);
      if (!_cbOpen && _cbCities.length > 0) _cbOpenPanel();
    }
    if (!_cbOpen) return;
    const visible = _cbVisibleOptions(panel);
    if (_cbActiveIdx >= 0 && _cbActiveIdx < visible.length) {
      const opt = visible[_cbActiveIdx];
      _cbSelect({ slug: opt.dataset.slug, name: opt.dataset.name });
    } else if (visible.length === 1) {
      // Auto-select when exactly one option is visible (e.g., "type Oxford, press Enter")
      const opt = visible[0];
      _cbSelect({ slug: opt.dataset.slug, name: opt.dataset.name });
    }

  } else if (e.key === "Escape") {
    _cbClosePanel();
    if (!_cbSelected) document.getElementById("scan-city-search").value = "";

  } else if (e.key === "Tab") {
    _cbClosePanel();
  }
}

function _cbVisibleOptions(panel) {
  return Array.from(panel.querySelectorAll(".combobox-option:not([hidden])"));
}

function _cbSetActive(visibleOpts) {
  _cbClearActive(document.getElementById("city-listbox"));
  const opt = visibleOpts[_cbActiveIdx];
  if (!opt) return;
  opt.classList.add("combobox-option--active");
  opt.id = "_cb_active";
  document.getElementById("scan-city-search").setAttribute("aria-activedescendant", "_cb_active");
  opt.scrollIntoView({ block: "nearest" });
}

function _cbClearActive(panel) {
  panel.querySelectorAll(".combobox-option--active").forEach((o) => {
    o.classList.remove("combobox-option--active");
    if (o.id === "_cb_active") o.removeAttribute("id");
  });
  document.getElementById("scan-city-search")?.removeAttribute("aria-activedescendant");
}

function _cbOpenPanel() {
  const input  = document.getElementById("scan-city-search");
  const panel  = document.getElementById("city-listbox");
  if (!panel || !input || !_cbCities.length) return;

  console.log("[CLIP] panel open called — positioning and showing");

  // Position under the input using fixed coords so the scan panel's
  // overflow:auto doesn't clip the dropdown.
  const r = input.getBoundingClientRect();
  console.log(`[CLIP] input rect: top=${r.top}, bottom=${r.bottom}, left=${r.left}, width=${r.width}`);
  panel.style.top   = (r.bottom + 3) + "px";
  panel.style.left  = r.left + "px";
  panel.style.width = r.width + "px";
  console.log(`[CLIP] panel positioned: top=${panel.style.top}, left=${panel.style.left}, width=${panel.style.width}`);
  panel.removeAttribute("hidden");
  console.log(`[CLIP] hidden attribute removed, panel should be visible`);
  input.setAttribute("aria-expanded", "true");
  _cbOpen = true;
}

function _cbClosePanel() {
  const panel = document.getElementById("city-listbox");
  const input = document.getElementById("scan-city-search");
  if (panel) panel.setAttribute("hidden", "");
  if (input) input.setAttribute("aria-expanded", "false");
  _cbOpen      = false;
  _cbActiveIdx = -1;
}

function _cbSelect(city) {
  console.log("[CLIP] Selected city:", city);
  _cbSelected = city;
  const searchInput = document.getElementById("scan-city-search");
  const hiddenInput = document.getElementById("scan-city");
  searchInput.value = city.name.split(",")[0];
  hiddenInput.value = city.slug;

  // Reflect aria-selected on all options
  const panel = document.getElementById("city-listbox");
  panel.querySelectorAll(".combobox-option").forEach((opt) => {
    opt.setAttribute("aria-selected", opt.dataset.slug === city.slug ? "true" : "false");
  });

  _cbClosePanel();
}
