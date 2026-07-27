/**
 * NTL-LTER Data Viewer – app.js
 *
 * Client-side application logic for exploring North Temperate Lakes
 * Long-Term Ecological Research (NTL-LTER) data. Handles dataset loading,
 * UI reactivity (cascading dropdowns for depth, species, and gear),
 * dynamic control visibility, Leaflet map modal, and Plotly charting.
 */

"use strict";

// ===========================================================================
// Constants & Configuration
// ===========================================================================

/** Lake name groupings by region */
const NORTH_LAKES = [
  "Allequash Lake",
  "Big Musky Lake",
  "Crystal Lake",
  "Crystal Bog",
  "Sparkling Lake",
  "Trout Lake",
  "Trout Bog"
];

const SOUTH_LAKES = [
  "Lake Mendota",
  "Lake Monona",
  "Fish Lake",
  "Lake Wingra"
];

/** Abbreviated month names for monthly boxplot x-axis ordering */
const MONTH_ABBR = [
  "Jan", "Feb", "March", "April", "May", "June",
  "July", "Aug", "Sep", "Oct", "Nov", "Dec"
];

/** Variable codes that represent Catch / CPUE abundance datasets */
const CATCH_VARS = ["rusty", "fish"];

/** Color palette for multi-lake trace lines and bars */
const TRACE_COLORS = [
  "#2a7da8", "#e8604c", "#5db87a", "#f0a500", "#8b5cf6",
  "#06b6d4", "#f97316", "#84cc16", "#ec4899", "#14b8a6", "#a855f7"
];

/** Leaflet map marker styles */
const MARKER_DEFAULT = "#2a7da8";
const MARKER_SELECTED = "#e8604c";

// ===========================================================================
// Application State
// ===========================================================================

const state = {
  /** Reference lookup table mapping variable codes to metadata & dataset URLs */
  matchtable: [],
  /** Geospatial metadata for all 11 NTL-LTER lakes */
  lakeLocations: [],
  /** Raw dataset records loaded for the currently selected variable */
  varData: null,
  /** Code string of the currently active variable (e.g., 'wtemp', 'fish') */
  currentVar: null,
  /** Leaflet map instance */
  map: null,
  /** Dictionary of active Leaflet circle markers indexed by lake name */
  markers: {},
  /** Flag tracking whether Plotly chart has been initially created */
  plotlyInitialized: false,
};

// ===========================================================================
// DOM Element References
// ===========================================================================

let elLakeSelect, elVarSelect, elDepthSelect, elDepthField,
  elSpeciesSelect, elSpeciesField, elGearSelect, elGearField,
  elPlotTypeGroup, elOptAM, elOptMB, elLogYAxis,
  elShowMapBtn, elCloseMapBtn, elMapModal, elLeafletMap,
  elChart, elChartLoading, elCitationLine, elLastUpdated, elModalHintPlural;

// ===========================================================================
// Application Boot & Initialization
// ===========================================================================

document.addEventListener("DOMContentLoaded", async () => {
  // Bind DOM elements
  elLakeSelect = document.getElementById("lakeSelect");
  elVarSelect = document.getElementById("varSelect");
  elDepthSelect = document.getElementById("depthSelect");
  elDepthField = document.getElementById("depthField");
  elSpeciesSelect = document.getElementById("speciesSelect");
  elSpeciesField = document.getElementById("speciesField");
  elGearSelect = document.getElementById("gearSelect");
  elGearField = document.getElementById("gearField");
  elPlotTypeGroup = document.getElementById("plotTypeGroup");
  elOptAM = document.getElementById("optAM");
  elOptMB = document.getElementById("optMB");
  elLogYAxis = document.getElementById("logYAxis");
  elShowMapBtn = document.getElementById("showMapBtn");
  elCloseMapBtn = document.getElementById("closeMapBtn");
  elMapModal = document.getElementById("mapModal");
  elLeafletMap = document.getElementById("leafletMap");
  elChart = document.getElementById("chart");
  elChartLoading = document.getElementById("chartLoading");
  elCitationLine = document.getElementById("citationLine");
  elLastUpdated = document.getElementById("lastUpdated");
  elModalHintPlural = document.getElementById("modalHintPlural");

  showLoading(true);

  try {
    // Fetch static metadata files in parallel
    const [matchtable, lakeLocations, lastUpdated] = await Promise.all([
      fetchJSON("data/matchtable.json"),
      fetchJSON("data/lakelocations.json"),
      fetchJSON("data/last_updated.json"),
    ]);

    state.matchtable = matchtable;
    state.lakeLocations = lakeLocations;

    // Populate dropdown selectors and citation info
    populateLakeSelect(lakeLocations);
    populateVarSelect(matchtable);
    updateLastUpdated(lastUpdated.updated);
    attachListeners();

    // Trigger initial variable load and plot rendering
    await onSelectionChange();
  } catch (err) {
    showLoading(false);
    showError("Failed to load reference data: " + err.message);
    console.error(err);
  }
});

// ===========================================================================
// Helper Functions & Dropdown Builders
// ===========================================================================

/**
 * Checks if a given variable code belongs to Catch / CPUE datasets.
 * @param {string} varCode - The short variable code (e.g., 'rusty', 'wtemp')
 * @returns {boolean} True if the variable is a catch variable
 */
function isCatchVar(varCode) {
  return CATCH_VARS.includes(varCode);
}

/**
 * Populates the Lake dropdown selector with optgroups for Northern and Southern lakes.
 * @param {Array<Object>} lakeLocations - Array of lake metadata objects
 */
function populateLakeSelect(lakeLocations) {
  const northGroup = document.getElementById("optNorth");
  const southGroup = document.getElementById("optSouth");

  northGroup.innerHTML = `<option value="__all_north__">All northern lakes</option>`;
  southGroup.innerHTML = `<option value="__all_south__">All southern lakes</option>`;

  lakeLocations.forEach(loc => {
    const opt = document.createElement("option");
    opt.value = loc.lake;
    opt.textContent = loc.lake;
    if (loc.region === "north") northGroup.appendChild(opt);
    else southGroup.appendChild(opt);
  });
}

/**
 * Populates the Variable dropdown selector categorized by scientific groupings.
 * @param {Array<Object>} matchtable - Reference dataset mapping array
 */
function populateVarSelect(matchtable) {
  const groups = {
    Physical: ["wtemp", "o2", "o2sat", "iceduration"],
    Nutrients: ["doc", "dic", "toc", "tic", "no3no2", "nh4", "totnuf", "totnf", "drp", "totpuf", "totpf", "drsif"],
    Ions: ["ph", "alk", "ca", "mg", "na", "k", "so4", "cl", "cond"],
    Secchi: ["secview", "secnview"],
    Zooplankton: ["cladocera", "calanoid", "cyclopoid", "rotifer"],
    "Catch / Abundance": ["rusty", "fish"],
  };

  elVarSelect.innerHTML = "";
  Object.entries(groups).forEach(([groupName, vars]) => {
    const og = document.createElement("optgroup");
    og.label = groupName;
    vars.forEach(v => {
      const row = matchtable.find(r => r.var === v);
      if (!row) return;
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = row.name;
      og.appendChild(opt);
    });
    elVarSelect.appendChild(og);
  });
}

// ===========================================================================
// Event Wiring
// ===========================================================================

/** Attaches event listeners to interactive UI elements */
function attachListeners() {
  elLakeSelect.addEventListener("change", onLakeChange);
  elVarSelect.addEventListener("change", onVarChange);
  elDepthSelect.addEventListener("change", renderPlot);
  elSpeciesSelect.addEventListener("change", onSpeciesChange);
  elGearSelect.addEventListener("change", renderPlot);
  elLogYAxis.addEventListener("change", renderPlot);
  elPlotTypeGroup.addEventListener("change", renderPlot);

  elShowMapBtn.addEventListener("click", openMap);
  elCloseMapBtn.addEventListener("click", closeMap);
  elMapModal.addEventListener("click", e => {
    if (e.target === elMapModal) closeMap();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !elMapModal.hidden) closeMap();
  });
}

// ===========================================================================
// User Action Handlers
// ===========================================================================

/**
 * Utility helper to fetch and parse JSON data with HTTP error handling.
 * @param {string} url - Target URL to fetch
 * @returns {Promise<any>} Parsed JSON payload
 */
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} fetching ${url}`);
  return res.json();
}

/** Handles user changing the selected variable */
async function onVarChange() {
  await onSelectionChange();
}

/** Handles user changing the selected lake(s) */
async function onLakeChange() {
  updateMapMarkers();
  if (isCatchVar(state.currentVar)) {
    updateSpeciesSelect();
    updateGearSelect();
  } else {
    updateDepthSelect();
  }
  renderPlot();
}

/** Handles user changing the selected species */
function onSpeciesChange() {
  updateGearSelect();
  renderPlot();
}

/**
 * Primary state transition coordinator. Triggered when variable changes or on initial boot.
 * Fetches required variable dataset JSON and refreshes dependent UI controls.
 */
async function onSelectionChange() {
  const varCode = elVarSelect.value;
  if (!varCode) return;

  updateCitation();

  // Re-fetch JSON dataset if a new variable was chosen
  if (varCode !== state.currentVar) {
    showLoading(true);
    try {
      state.varData = await fetchJSON(`data/vars/${varCode}.json`);
      state.currentVar = varCode;
    } catch (err) {
      showLoading(false);
      showError(`Could not load data for "${varCode}": ${err.message}`);
      return;
    }
  }

  updateControlVisibility();
  updateMapMarkers();

  if (isCatchVar(state.currentVar)) {
    updateSpeciesSelect();
    updateGearSelect();
  } else {
    updateDepthSelect();
  }

  renderPlot();
}

/**
 * Adjusts the visibility of sidebar inputs based on variable type:
 * - Hides depth field & shows species/gear dropdowns for catch variables.
 * - Restricts plot options to Time-Series only for catch variables.
 */
function updateControlVisibility() {
  const catchVar = isCatchVar(state.currentVar);

  // Toggle Depth vs Species & Gear controls
  elDepthField.hidden = catchVar;
  elSpeciesField.hidden = !catchVar;
  elGearField.hidden = !catchVar;

  // Restrict plot types: Catch datasets support Time-Series only
  if (catchVar) {
    const tsRadio = elPlotTypeGroup.querySelector('input[value="plot.ts"]');
    if (tsRadio) tsRadio.checked = true;
    elOptAM.hidden = true;
    elOptMB.hidden = true;
  } else {
    elOptAM.hidden = false;
    elOptMB.hidden = false;
  }
}

// ===========================================================================
// Catch Variable Controls (Species & Gear Cascading Filters)
// ===========================================================================

/**
 * Returns an array of individual lake names corresponding to the current lake select value.
 * @returns {Array<string>} Array of lake names
 */
function getSelectedLakes() {
  const val = elLakeSelect.value;
  if (val === "__all_north__") return NORTH_LAKES;
  if (val === "__all_south__") return SOUTH_LAKES;
  return [val];
}

/** Dynamically populates the species dropdown based on current lake selection */
function updateSpeciesSelect() {
  if (!state.varData || !isCatchVar(state.currentVar)) return;

  const selectedLakes = getSelectedLakes();
  const lakeData = state.varData.filter(r => selectedLakes.includes(r.lakename));
  const speciesList = [...new Set(lakeData.map(r => r.spname))].filter(Boolean).sort();

  const prevSpecies = elSpeciesSelect.value;
  elSpeciesSelect.innerHTML = "";

  speciesList.forEach(sp => {
    const opt = document.createElement("option");
    opt.value = sp;
    opt.textContent = sp;
    elSpeciesSelect.appendChild(opt);
  });

  // Preserve previous selection if valid, default to WALLEYE or first entry
  if (speciesList.includes(prevSpecies)) {
    elSpeciesSelect.value = prevSpecies;
  } else if (speciesList.includes("WALLEYE")) {
    elSpeciesSelect.value = "WALLEYE";
  } else if (speciesList.length > 0) {
    elSpeciesSelect.value = speciesList[0];
  }
}

/** Dynamically populates gear dropdown based on selected lake(s) AND selected species */
function updateGearSelect() {
  if (!state.varData || !isCatchVar(state.currentVar)) return;

  const selectedLakes = getSelectedLakes();
  const selectedSpecies = elSpeciesSelect.value;

  // Filter records by lake(s) AND species to isolate available gear types
  const filteredData = state.varData.filter(
    r => selectedLakes.includes(r.lakename) && r.spname === selectedSpecies
  );
  // Count unique years each gear type was used
  const gearYearsMap = {};
  filteredData.forEach(r => {
    if (!r.gearid) return;
    if (!gearYearsMap[r.gearid]) {
      gearYearsMap[r.gearid] = new Set();
    }
    gearYearsMap[r.gearid].add(r.year4);
  });

  // Exclude gears that only appear in a single year
  const gearList = Object.keys(gearYearsMap)
    .filter(gear => gearYearsMap[gear].size > 1)
    .sort();

  const prevGear = elGearSelect.value;
  elGearSelect.innerHTML = "";

  gearList.forEach(g => {
    const opt = document.createElement("option");
    opt.value = g;
    opt.textContent = g;
    elGearSelect.appendChild(opt);
  });

  // Preserve previous choice or pick common defaults
  if (gearList.includes(prevGear)) {
    elGearSelect.value = prevGear;
  } else if (gearList.includes("Electrofishing")) {
    elGearSelect.value = "Electrofishing";
  } else if (gearList.includes("Crayfish Trap")) {
    elGearSelect.value = "Crayfish Trap";
  } else if (gearList.length > 0) {
    elGearSelect.value = gearList[0];
  }
}

// ===========================================================================
// Depth Control (Standard Physical / Chemical / Biological Variables)
// ===========================================================================

/** Dynamically populates depth choices based on common sampling depths across selected lakes */
function updateDepthSelect() {
  if (!state.varData) return;
  const lakes = getSelectedLakes();

  const depthSets = lakes.map(lake => {
    const counts = {};
    state.varData
      .filter(r => r.lakename === lake)
      .forEach(r => { counts[r.depth] = (counts[r.depth] || 0) + 1; });
    return new Set(Object.entries(counts).filter(([, n]) => n > 10).map(([d]) => d));
  });

  // Find intersection of depths available in all currently selected lakes
  const intersection = depthSets.reduce((acc, set) => {
    if (acc === null) return set;
    return new Set([...acc].filter(d => set.has(d)));
  }, null) || new Set();

  const sortedDepths = [...intersection].map(Number).sort((a, b) => a - b);

  const prevDepth = elDepthSelect.value;
  elDepthSelect.innerHTML = "";
  sortedDepths.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    if (String(d) === prevDepth) opt.selected = true;
    elDepthSelect.appendChild(opt);
  });

  if (!elDepthSelect.value && intersection.has("0")) {
    elDepthSelect.value = "0";
  }
}

// ===========================================================================
// Plot Rendering Pipeline
// ===========================================================================

/** Reads the active plot type radio button value */
function getPlotType() {
  const checked = elPlotTypeGroup.querySelector('input[name="plotType"]:checked');
  return checked ? checked.value : "plot.ts";
}

/** Main entry point for chart rendering; routes to catch or standard plotters */
function renderPlot() {
  if (!state.varData) return;

  const catchVar = isCatchVar(state.currentVar);
  const lakes = getSelectedLakes();
  const logY = elLogYAxis.checked;
  const rowMatch = state.matchtable.find(r => r.var === state.currentVar);
  const varName = rowMatch ? rowMatch.name : state.currentVar;

  showLoading(false);

  if (catchVar) {
    renderCatchPlot(lakes, varName, logY);
  } else {
    renderStandardPlot(lakes, varName, logY);
  }
}

/**
 * Renders annual CPUE bar charts for Catch / Abundance variables.
 * @param {Array<string>} lakes - Active lake names
 * @param {string} varName - Descriptive variable name for axes
 * @param {boolean} logY - Whether y-axis should be log-scaled
 */
function renderCatchPlot(lakes, varName, logY) {
  const selectedSpecies = elSpeciesSelect.value;
  const selectedGear = elGearSelect.value;
  const traces = [];

  lakes.forEach((lake, i) => {
    const rows = state.varData.filter(
      r => r.lakename === lake && r.spname === selectedSpecies && r.gearid === selectedGear
    );
    if (!rows.length) return;

    // Aggregate annual mean CPUE
    const byYear = {};
    rows.forEach(r => {
      if (!byYear[r.year4]) byYear[r.year4] = { totalCPUE: 0, count: 0 };
      byYear[r.year4].totalCPUE += r.CPUE;
      byYear[r.year4].count += 1;
    });

    const years = Object.keys(byYear).map(Number).sort((a, b) => a - b);
    const cpues = years.map(y => byYear[y].totalCPUE / byYear[y].count);

    traces.push({
      type: "bar",
      name: lake,
      x: years,
      y: cpues,
      marker: { color: TRACE_COLORS[i % TRACE_COLORS.length] },
    });
  });

  const layout = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "#fafcff",
    margin: { t: 20, r: 20, b: 60, l: 70 },
    font: { family: "'Inter', sans-serif", size: 13, color: "#1a2936" },
    legend: {
      orientation: "h",
      y: -0.18,
      x: 0,
      bgcolor: "rgba(0,0,0,0)",
      font: { size: 12 },
    },
    xaxis: {
      type: "linear",
      title: { text: "Year", font: { size: 12 } },
      tickformat: "d",
      gridcolor: "#e0eaf2",
      linecolor: "#cdd8e2",
      tickfont: { size: 11 },
    },
    yaxis: {
      title: { text: "CPUE", font: { size: 12 } },
      type: logY ? "log" : "linear",
      gridcolor: "#e0eaf2",
      linecolor: "#cdd8e2",
      tickfont: { size: 11 },
    },
    barmode: "group",
    bargap: 0.5,        // Adds gap space so sparse years don't stretch into massive bars
    bargroupgap: 0.1,   // Adds spacing between grouped bars per year
    hovermode: "closest",
  };

  drawPlotly(traces, layout);
}

/**
 * Renders standard time-series, annual means, or monthly boxplots.
 * @param {Array<string>} lakes - Active lake names
 * @param {string} varName - Descriptive variable name
 * @param {boolean} logY - Whether y-axis should be log-scaled
 */
function renderStandardPlot(lakes, varName, logY) {
  const depthRaw = elDepthSelect.value;
  const depth = depthRaw !== "" ? parseFloat(depthRaw) : 0;
  const plotType = getPlotType();
  const traces = [];

  if (plotType === "plot.mb") {
    // Monthly Boxplots
    lakes.forEach((lake, i) => {
      const rows = state.varData.filter(
        r => r.lakename === lake && r.depth === depth && r.rep == 1
      );
      if (!rows.length) return;

      const xVals = [];
      const yVals = [];
      rows.forEach(r => {
        const m = new Date(r.sampledate).getMonth();
        if (m >= 0 && m < 12) {
          xVals.push(MONTH_ABBR[m]);
          yVals.push(r.value);
        }
      });

      if (!xVals.length) return;

      traces.push({
        type: "box",
        name: lake,
        x: xVals,
        y: yVals,
        marker: { color: TRACE_COLORS[i % TRACE_COLORS.length] },
        line: { color: TRACE_COLORS[i % TRACE_COLORS.length] },
        boxpoints: false,
      });
    });

    const layout = buildLayout(varName, logY, "category");
    layout.xaxis.categoryorder = "array";
    layout.xaxis.categoryarray = MONTH_ABBR;
    layout.boxmode = "group";
    layout.boxgap = 0.2;
    layout.boxgroupgap = 0.1;
    drawPlotly(traces, layout);

  } else if (plotType === "plot.am") {
    // Annual Means
    lakes.forEach((lake, i) => {
      const rows = state.varData.filter(
        r => r.lakename === lake && r.depth === depth && r.rep == 1 && r.year4 > 1981
      );
      if (!rows.length) return;

      const byYear = {};
      rows.forEach(r => {
        if (!byYear[r.year4]) byYear[r.year4] = [];
        byYear[r.year4].push(r.value);
      });
      const years = Object.keys(byYear).map(Number).sort((a, b) => a - b);
      const means = years.map(y => {
        const vals = byYear[y];
        return vals.reduce((a, b) => a + b, 0) / vals.length;
      });

      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: lake,
        x: years,
        y: means,
        line: { color: TRACE_COLORS[i % TRACE_COLORS.length], width: 2 },
        marker: { color: TRACE_COLORS[i % TRACE_COLORS.length], size: 5 },
      });
    });

    const layout = buildLayout(varName, logY, "linear");
    layout.xaxis.tickformat = "d";
    drawPlotly(traces, layout);

  } else {
    // Time-Series (Default)
    lakes.forEach((lake, i) => {
      const rows = state.varData
        .filter(r => r.lakename === lake && r.depth === depth && r.rep == 1)
        .sort((a, b) => a.sampledate.localeCompare(b.sampledate));
      if (!rows.length) return;

      traces.push({
        type: "scatter",
        mode: "lines+markers",
        name: lake,
        x: rows.map(r => r.sampledate),
        y: rows.map(r => r.value),
        line: { color: TRACE_COLORS[i % TRACE_COLORS.length], width: 1.5 },
        marker: { color: TRACE_COLORS[i % TRACE_COLORS.length], size: 3 },
      });
    });

    const layout = buildLayout(varName, logY);
    drawPlotly(traces, layout);
  }
}

/**
 * Constructs a base Plotly layout configuration.
 * @param {string} yTitle - Axis title string for y-axis
 * @param {boolean} logY - Log scale flag
 * @param {string} [xType="date"] - Plotly x-axis type ('date', 'linear', 'category')
 * @returns {Object} Plotly layout object
 */
function buildLayout(yTitle, logY, xType = "date") {
  return {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "#fafcff",
    margin: { t: 20, r: 20, b: 60, l: 70 },
    font: { family: "'Inter', sans-serif", size: 13, color: "#1a2936" },
    legend: {
      orientation: "h",
      y: -0.18,
      x: 0,
      bgcolor: "rgba(0,0,0,0)",
      font: { size: 12 },
    },
    xaxis: {
      type: xType,
      gridcolor: "#e0eaf2",
      linecolor: "#cdd8e2",
      tickfont: { size: 11 },
    },
    yaxis: {
      title: { text: yTitle, font: { size: 12 } },
      type: logY ? "log" : "linear",
      gridcolor: "#e0eaf2",
      linecolor: "#cdd8e2",
      tickfont: { size: 11 },
    },
    hovermode: "closest",
  };
}

/**
 * Draws or updates the Plotly chart container.
 * @param {Array<Object>} traces - Plotly data series array
 * @param {Object} layout - Plotly layout configuration
 */
function drawPlotly(traces, layout) {
  const config = {
    responsive: true,
    displayModeBar: true,
    modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
    displaylogo: false,
  };

  if (!traces.length) {
    Plotly.purge(elChart);
    elChart.innerHTML = `<div class="chart-loading" style="height:100%">
      <span style="color:#4a6375;font-size:.9rem">No data available for this selection.</span>
    </div>`;
    return;
  }

  if (state.plotlyInitialized) {
    Plotly.react(elChart, traces, layout, config);
  } else {
    Plotly.newPlot(elChart, traces, layout, config);
    state.plotlyInitialized = true;
  }
}

// ===========================================================================
// Map Modal & Geospatial Leaflet Rendering
// ===========================================================================

/**
 * Initializes the Leaflet map and plots initial circle markers.
 * @param {Array<Object>} lakeLocations - Geospatial metadata array
 */
function initMap(lakeLocations) {
  state.map = L.map(elLeafletMap, { zoomControl: true }).setView([44.7, -89.55], 6);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 18,
  }).addTo(state.map);

  lakeLocations.forEach(loc => {
    const marker = L.circleMarker([loc.lat, loc.long], circleStyle(false))
      .addTo(state.map)
      .bindTooltip(`<strong>${loc.lake}</strong>`);
    state.markers[loc.lake] = marker;
  });
}

/**
 * Generates Leaflet marker styling based on selection state.
 * @param {boolean} selected - True if lake is currently active
 * @returns {Object} Leaflet CircleMarker option styling
 */
function circleStyle(selected) {
  return {
    radius: selected ? 11 : 8,
    fillColor: selected ? MARKER_SELECTED : MARKER_DEFAULT,
    color: selected ? "#a33828" : "#1a5a7a",
    weight: selected ? 2.5 : 1.5,
    opacity: 1,
    fillOpacity: selected ? 0.9 : 0.65,
  };
}

/** Updates Leaflet map marker highlights to reflect selected lake(s) */
function updateMapMarkers() {
  if (!state.map) return;
  const selected = new Set(getSelectedLakes());
  Object.entries(state.markers).forEach(([lakeName, marker]) => {
    marker.setStyle(circleStyle(selected.has(lakeName)));
    if (selected.has(lakeName)) marker.bringToFront();
  });

  if (elModalHintPlural) {
    elModalHintPlural.textContent = selected.size > 1 ? "s are" : " is";
  }
}

/** Opens the site location map modal */
function openMap() {
  elMapModal.hidden = false;
  if (!state.map) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      initMap(state.lakeLocations);
      updateMapMarkers();
      state.map.invalidateSize();
    }));
  } else {
    updateMapMarkers();
    setTimeout(() => state.map.invalidateSize(), 100);
  }
}

/** Closes the site location map modal */
function closeMap() {
  elMapModal.hidden = true;
}

// ===========================================================================
// Footer Data Citations & UI Utilities
// ===========================================================================

/** Updates dataset EDI citation link in the footer */
function updateCitation() {
  const varCode = elVarSelect.value;
  const row = state.matchtable.find(r => r.var === varCode);
  if (!row || !elCitationLine) return;
  let cite_url = row.url;

  // Handle special case for Southern Lakes Ice Duration dataset
  if (elVarSelect.value == "iceduration" && [...SOUTH_LAKES, "__all_south__"].includes(elLakeSelect.value))
    cite_url = "https://portal.edirepository.org/nis/mapbrowse?scope=knb-lter-ntl&identifier=33";

  elCitationLine.innerHTML =
    `A friendly reminder to please cite data! Data citation for this dataset can be found here: ` +
    `<a href="${cite_url}" target="_blank" rel="noopener">EDI Dataset Page</a>`;
}

/** Toggles loading overlay visibility */
function showLoading(on) {
  if (!elChartLoading) return;
  elChartLoading.classList.toggle("hidden", !on);
}

/** Displays error message overlay inside chart container */
function showError(msg) {
  if (!elChart) return;
  elChart.innerHTML = `<div class="chart-loading" style="height:100%">
    <span style="color:#e8604c;font-size:.9rem">⚠ ${msg}</span>
  </div>`;
}

/** Displays dataset last update timestamp in UI */
function updateLastUpdated(dateStr) {
  if (!elLastUpdated || !dateStr) return;
  const d = new Date(dateStr + "T00:00:00");
  elLastUpdated.textContent = "Updated " + d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}