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
  document.getElementById("btn-close-panel").addEventListener("click", closePanel);

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
}

async function loadCitiesForPanel(stateAbbr) {
  const select = document.getElementById("scan-city");
  select.innerHTML = '<option value="">Loading…</option>';
  try {
    const resp = await fetch(`/api/cities?state=${encodeURIComponent(stateAbbr)}`);
    const cities = await resp.json();
    select.innerHTML = cities.length
      ? cities.map((c) => `<option value="${esc(c.slug)}">${esc(c.name)}</option>`).join("")
      : '<option value="">No cities available</option>';
  } catch {
    select.innerHTML = '<option value="">Error loading cities</option>';
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
  const preset  = document.getElementById("adv-preset").value;
  const modeNum = document.getElementById("adv-mode-num").value;

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
    showToast(`Scan queued — run ID: ${data.run_id}`, "success");
    closePanel();
  } catch {
    showToast("Failed to queue scan. Check the server.", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Scan";
  }
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
            ? `<button class="btn-view-brief-sm" data-run-id="${esc(run.run_id)}">View Brief</button>`
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
