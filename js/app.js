/**
 * NTL-LTER Data Viewer – app.js
 *
 * Responsibilities:
 *  1. Boot: load matchtable.json, lakelocations.json, last_updated.json
 *  2. Populate lake select (individual lakes + All north / All south groups)
 *  3. Populate variable select from matchtable
 *  4. On user change: fetch data/vars/<varcode>.json → filter → plot
 *  5. Depth dropdown: intersection of depths with >50 obs across all selected lakes
 *  6. Plotly rendering: time-series | annual means | monthly boxplots
 *  7. Map modal: Leaflet, selected lakes highlighted in coral
 *  8. Citation footer updated on variable change
 */

"use strict";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const NORTH_LAKES = ["Allequash", "Big Musky", "Crystal", "Crystal Bog", "Sparkling", "Trout", "Trout Bog"];
const SOUTH_LAKES = ["Mendota", "Monona", "Fish", "Wingra"];

const MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Plotly colour sequence (one per lake trace)
const TRACE_COLORS = [
  "#2a7da8", "#e8604c", "#5db87a", "#f0a500", "#8b5cf6",
  "#06b6d4", "#f97316", "#84cc16", "#ec4899", "#14b8a6", "#a855f7"
];

// Leaflet marker colours
const MARKER_DEFAULT = "#2a7da8";
const MARKER_SELECTED = "#e8604c";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const state = {
  matchtable: [],        // [{var, name, url}, ...]
  lakeLocations: [],     // [{lakeid, lake, region, lat, long}, ...]
  varData: null,         // raw records for current variable [{lakeid,lakename,year4,sampledate,depth,rep,value}]
  currentVar: null,      // current var code string
  map: null,             // Leaflet map instance
  markers: {},           // {lakeid: L.circleMarker}
  plotlyInitialized: false,
};

// ---------------------------------------------------------------------------
// DOM refs (grabbed once after DOMContentLoaded)
// ---------------------------------------------------------------------------

let elLakeSelect, elVarSelect, elDepthSelect, elPlotTypeGroup,
  elLogYAxis, elShowMapBtn, elCloseMapBtn,
  elMapModal, elLeafletMap, elChart, elChartLoading,
  elCitationLine, elLastUpdated, elModalHintPlural;

// elFreeYAxis,

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  elLakeSelect = document.getElementById("lakeSelect");
  elVarSelect = document.getElementById("varSelect");
  elDepthSelect = document.getElementById("depthSelect");
  elPlotTypeGroup = document.getElementById("plotTypeGroup");
  // elFreeYAxis        = document.getElementById("freeYAxis");
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
    const [matchtable, lakeLocations, lastUpdated] = await Promise.all([
      fetchJSON("data/matchtable.json"),
      fetchJSON("data/lakelocations.json"),
      fetchJSON("data/last_updated.json"),
    ]);

    state.matchtable = matchtable;
    state.lakeLocations = lakeLocations;

    populateLakeSelect(lakeLocations);
    populateVarSelect(matchtable);
    initMap(lakeLocations);
    updateLastUpdated(lastUpdated.updated);
    attachListeners();

    // Initial data fetch + plot
    await onSelectionChange();
  } catch (err) {
    showLoading(false);
    showError("Failed to load reference data: " + err.message);
    console.error(err);
  }
});

// ---------------------------------------------------------------------------
// Populate controls
// ---------------------------------------------------------------------------

function populateLakeSelect(lakeLocations) {
  const northGroup = document.getElementById("optNorth");
  const southGroup = document.getElementById("optSouth");

  // Clear and add "All …" sentinel options
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

function populateVarSelect(matchtable) {
  // Group by category inferred from the original R app ordering
  const groups = {
    Physical: ["wtemp", "o2", "o2sat", "iceduration"],
    Nutrients: ["doc", "dic", "toc", "tic", "no3no2", "nh4", "totnuf", "totnf", "drp", "totpuf", "totpf", "drsif"],
    Ions: ["ph", "alk", "ca", "mg", "na", "k", "so4", "cl", "cond"],
    Secchi: ["secview", "secnview"],
    Zooplankton: ["cladocera", "calanoid", "cyclopoid", "rotifer"],
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

// ---------------------------------------------------------------------------
// Event wiring
// ---------------------------------------------------------------------------

function attachListeners() {
  elLakeSelect.addEventListener("change", onSelectionChange);
  elVarSelect.addEventListener("change", onVarChange);
  elDepthSelect.addEventListener("change", renderPlot);
  // elFreeYAxis.addEventListener("change", renderPlot);
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

// ---------------------------------------------------------------------------
// Data fetching
// ---------------------------------------------------------------------------

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} fetching ${url}`);
  return res.json();
}

async function onVarChange() {
  await onSelectionChange();
}

async function onSelectionChange() {
  const varCode = elVarSelect.value;
  if (!varCode) return;

  updateCitation();

  // Only re-fetch if variable changed
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

  updateDepthSelect();
  updateMapMarkers();
  renderPlot();
}

// ---------------------------------------------------------------------------
// Depth dropdown
// ---------------------------------------------------------------------------

function getSelectedLakes() {
  const val = elLakeSelect.value;
  if (val === "__all_north__") return NORTH_LAKES;
  if (val === "__all_south__") return SOUTH_LAKES;
  return [val];
}

function updateDepthSelect() {
  if (!state.varData) return;
  const lakes = getSelectedLakes();

  // For each lake, collect depths with >50 observations; then intersect.
  const depthSets = lakes.map(lake => {
    const counts = {};
    state.varData
      .filter(r => r.lakename === lake)
      .forEach(r => { counts[r.depth] = (counts[r.depth] || 0) + 1; });
    return new Set(Object.entries(counts).filter(([, n]) => n > 50).map(([d]) => d));
  });

  // Intersection across all selected lakes
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

  // Default to 0 if available
  if (!elDepthSelect.value && intersection.has("0")) {
    elDepthSelect.value = "0";
  }
}

// ---------------------------------------------------------------------------
// Plotly rendering
// ---------------------------------------------------------------------------

function getPlotType() {
  const checked = elPlotTypeGroup.querySelector('input[name="plotType"]:checked');
  return checked ? checked.value : "plot.ts";
}

function renderPlot() {
  if (!state.varData) return;

  const lakes = getSelectedLakes();
  const depthRaw = elDepthSelect.value;
  const depth = depthRaw !== "" ? parseFloat(depthRaw) : 0;
  const plotType = getPlotType();
  // const freeY       = elFreeYAxis.checked;
  const logY = elLogYAxis.checked;
  const varName = state.matchtable.find(r => r.var === state.currentVar)?.name ?? state.currentVar;

  showLoading(false);

  const traces = [];

  if (plotType === "plot.mb") {
    // Monthly boxplots
    lakes.forEach((lake, i) => {
      const rows = state.varData.filter(
        r => r.lakename === lake && r.depth === depth && r.rep == 1
      );
      if (!rows.length) return;

      // Group by month
      const byMonth = Array.from({ length: 12 }, () => []);
      rows.forEach(r => {
        const m = new Date(r.sampledate).getMonth(); // 0-indexed
        if (m >= 0 && m < 12) byMonth[m].push(r.value);
      });

      // One box trace per month – Plotly groups them by x position
      MONTH_ABBR.forEach((mon, mi) => {
        const vals = byMonth[mi];
        if (!vals.length) return;
        traces.push({
          type: "box",
          name: lakes.length > 1 ? `${lake} – ${mon}` : mon,
          x: vals.map(() => mon),
          y: vals,
          marker: { color: TRACE_COLORS[i % TRACE_COLORS.length] },
          line: { color: TRACE_COLORS[i % TRACE_COLORS.length] },
          legendgroup: lake,
          showlegend: mi === 0,
          boxpoints: false,
        });
      });
    });

    const layout = buildLayout(varName, logY, "category");
    layout.xaxis.categoryarray = MONTH_ABBR;
    layout.boxmode = "group";
    drawPlotly(traces, layout);

  } else if (plotType === "plot.am") {
    // Annual means
    lakes.forEach((lake, i) => {
      const rows = state.varData.filter(
        r => r.lakename === lake && r.depth === depth && r.rep == 1 && r.year4 > 1981
      );
      if (!rows.length) return;

      // Aggregate by year
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

    const layout = buildLayout(varName, logY);
    layout.xaxis.title = "Year";
    drawPlotly(traces, layout);

  } else {
    // Time-series
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
      // autorange: freeY ? true : undefined,
    },
    hovermode: "closest",
    hoverlabel: {
      bgcolor: "#0d2b3e",
      bordercolor: "#2a7da8",
      font: { color: "#ffffff", size: 12 },
    },
  };
}

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

// ---------------------------------------------------------------------------
// Map modal
// ---------------------------------------------------------------------------

function initMap(lakeLocations) {
  state.map = L.map(elLeafletMap, { zoomControl: true }).setView([44.7, -89.55], 6);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    maxZoom: 18,
  }).addTo(state.map);

  lakeLocations.forEach(loc => {
    const marker = L.circleMarker([loc.lat, loc.long], circleStyle(false))
      .addTo(state.map)
      .bindPopup(`<strong>${loc.lake}</strong><br>${loc.region === "north" ? "Northern" : "Southern"} lakes`);
    state.markers[loc.lake] = marker;
  });
}

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

function updateMapMarkers() {
  if (!state.map) return;
  const selected = new Set(getSelectedLakes());
  Object.entries(state.markers).forEach(([lakeName, marker]) => {
    marker.setStyle(circleStyle(selected.has(lakeName)));
    if (selected.has(lakeName)) marker.bringToFront();
  });

  // Update modal hint plural
  if (elModalHintPlural) {
    elModalHintPlural.textContent = selected.size > 1 ? "s are" : " is";
  }
}

function openMap() {
  elMapModal.hidden = false;
  updateMapMarkers();
  // Leaflet needs a size invalidation when the container becomes visible
  setTimeout(() => state.map && state.map.invalidateSize(), 50);
}

function closeMap() {
  elMapModal.hidden = true;
}

// ---------------------------------------------------------------------------
// Citation footer
// ---------------------------------------------------------------------------

function updateCitation() {
  const varCode = elVarSelect.value;
  const row = state.matchtable.find(r => r.var === varCode);
  if (!row || !elCitationLine) return;

  elCitationLine.innerHTML =
    `A friendly reminder to please cite data! Data citation for this dataset can be found here: ` +
    `<a href="${row.url}" target="_blank" rel="noopener">EDI Dataset Page</a>`;
}

// ---------------------------------------------------------------------------
// UI helpers
// ---------------------------------------------------------------------------

function showLoading(on) {
  if (!elChartLoading) return;
  elChartLoading.classList.toggle("hidden", !on);
}

function showError(msg) {
  if (!elChart) return;
  elChart.innerHTML = `<div class="chart-loading" style="height:100%">
    <span style="color:#e8604c;font-size:.9rem">⚠ ${msg}</span>
  </div>`;
}

function updateLastUpdated(dateStr) {
  if (!elLastUpdated || !dateStr) return;
  const d = new Date(dateStr + "T00:00:00");
  elLastUpdated.textContent = "Updated " + d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}
