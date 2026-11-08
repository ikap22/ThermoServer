const SHORT_NAME = {
  "msft-columbia": "Microsoft Columbia Center",
  "vantage-quincy": "Vantage Quincy",
  "sabey-sdc": "Sabey SDC",
  "h5-quincy": "H5 Data Centers",
};

const yearInput = document.getElementById("year");
const yearLabel = document.getElementById("year-label");
const drawer = document.getElementById("drawer");
const acresInput = document.getElementById("acres");
const mwInput = document.getElementById("mw");
const forecastPanel = document.getElementById("forecast-panel");

let metrics = null;
let heatBounds = null;
const heatYear = {};
let selectedId = null;
let historyChart = null;
let decayChart = null;
let campusesCache = null;
const markers = {};

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      satellite: {
        type: "raster",
        tiles: [
          "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        ],
        tileSize: 256,
        attribution: "Imagery &copy; Esri",
      },
    },
    layers: [{ id: "satellite", type: "raster", source: "satellite" }],
  },
  center: [-119.838, 47.243],
  zoom: 12.6,
});

map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");

function draftCurve(acreage, mw) {
  const peak = 5 + (acreage - 70) * 0.035 + (mw - 100) * 0.008;
  const scale = 400 + acreage * 6;
  const distances = [0, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000];
  const temps = distances.map((distance) => Math.max(0, peak * Math.exp(-distance / scale)));
  const radius = Math.min(5000, Math.max(250, scale * Math.log(Math.max(peak, 0.5) / 0.5)));
  return { distances, temps, radius, peak };
}

function ring(lon, lat, radiusM) {
  const coords = [];
  const latScale = 111320;
  const lonScale = 111320 * Math.cos((lat * Math.PI) / 180);
  for (let i = 0; i <= 64; i += 1) {
    const angle = (i / 64) * Math.PI * 2;
    coords.push([
      lon + (radiusM * Math.cos(angle)) / lonScale,
      lat + (radiusM * Math.sin(angle)) / latScale,
    ]);
  }
  return { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: coords } };
}

function centroid(feature) {
  const ringCoords = feature.geometry.coordinates[0];
  let lon = 0;
  let lat = 0;
  ringCoords.forEach(([x, y]) => {
    lon += x;
    lat += y;
  });
  return [lon / ringCoords.length, lat / ringCoords.length];
}

function subtract(left, right) {
  return left.map((value, index) => (
    value == null || right[index] == null ? null : +(value - right[index]).toFixed(2)
  ));
}

function meanDivergence() {
  const ids = Object.keys(metrics.facilities);
  return metrics.years.map((_, index) => {
    const values = ids.map((id) => {
      const series = metrics.facilities[id].series;
      return series.A_core[index] - series.C2_cropland[index];
    });
    return values.reduce((sum, value) => sum + value, 0) / values.length;
  });
}

function drawSpark(values) {
  const svg = document.getElementById("spark");
  const width = 220;
  const height = 64;
  const min = Math.min(...values) - 0.4;
  const max = Math.max(...values) + 0.4;
  const points = values.map((value, index) => {
    const x = 4 + (index / (values.length - 1)) * (width - 8);
    const y = height - 8 - ((value - min) / (max - min)) * (height - 16);
    return `${x},${y}`;
  }).join(" ");
  svg.innerHTML = `<polyline points="${points}" fill="none" stroke="#fb923c" stroke-width="2" />`;
}

function chartTheme() {
  return {
    color: "#cbd5e1",
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { position: "bottom", labels: { boxWidth: 12, color: "#cbd5e1" } } },
    scales: {
      x: { ticks: { color: "#94a3b8", maxTicksLimit: 6 }, grid: { color: "rgba(255,255,255,0.06)" } },
      y: { min: -2, max: 16, ticks: { color: "#94a3b8" }, grid: { color: "rgba(255,255,255,0.06)" }, title: { display: true, text: "°C above crops", color: "#94a3b8" } },
    },
  };
}

function showForecast() {
  const acreage = Number(acresInput.value);
  const mw = Number(mwInput.value);
  document.getElementById("acre-label").textContent = `${acreage} acres`;
  document.getElementById("mw-label").textContent = `${mw} MW`;
  const curve = draftCurve(acreage, mw);
  forecastPanel.classList.add("open");
  if (decayChart) decayChart.resize();
  document.getElementById("forecast-summary").textContent =
    `About ${curve.peak.toFixed(1)}°C above cropland at the building edge, down to 0.5°C at ${(curve.radius / 1000).toFixed(1)} km.*`;
  if (decayChart) {
    decayChart.data.labels = curve.distances.map((distance) => distance >= 1000 ? `${distance / 1000} km` : `${distance} m`);
    decayChart.data.datasets[0].data = curve.temps;
    decayChart.update();
  }
  if (!selectedId || !campusesCache || !map.getSource("plume")) return;
  const feature = campusesCache.features.find((item) => item.properties.id === selectedId);
  const [lon, lat] = centroid(feature);
  map.getSource("plume").setData({ type: "FeatureCollection", features: [ring(lon, lat, curve.radius)] });
}

function openCampus(feature) {
  const props = feature.properties;
  selectedId = props.id;
  const record = metrics.facilities[props.id];
  const aboveCrop = gapSeries(props.id);
  const rangeAbove = subtract(record.series.C1_rangeland, record.series.C2_cropland);
  const last = aboveCrop.length - 1;
  const early = average(aboveCrop.slice(0, 3));
  const late = average(aboveCrop.slice(-3));

  document.getElementById("early-gap").textContent = `${early.toFixed(1)}°C`;
  document.getElementById("late-gap").textContent = `${late.toFixed(1)}°C`;
  document.getElementById("change-gap").textContent = `+${(late - early).toFixed(1)}°C`;
  document.getElementById("campus-name").textContent = record.name;
  document.getElementById("campus-meta").textContent = `${record.operator} · Quincy, WA`;
  document.getElementById("stat-acres").textContent = `${record.acreage} acres`;
  document.getElementById("stat-mw").textContent = `${record.estimated_mw} MW`;
  document.getElementById("stat-zone").textContent =
    `Zone A in ${metrics.years[last]}: ${aboveCrop[last].toFixed(1)}°C above irrigated cropland.`;
  document.getElementById("stat-crop").textContent =
    `Versus cropland that year: ${aboveCrop[last].toFixed(1)}°C hotter`;
  document.getElementById("stat-range").textContent =
    `Versus dry rangeland: ${(record.series.A_core[last] - record.series.C1_rangeland[last]).toFixed(1)}°C`;

  historyChart.data.labels = metrics.years;
  historyChart.data.datasets[0].data = aboveCrop;
  historyChart.data.datasets[1].data = metrics.years.map(() => 0);
  historyChart.data.datasets[2].data = rangeAbove;
  historyChart.update();

  campusesCache.features.forEach((item) => {
    const on = item.properties.id === props.id;
    map.setFeatureState({ source: "campuses", id: item.properties.id }, { selected: on });
    markers[item.properties.id].classList.toggle("selected", on);
  });
  drawer.classList.add("open");
  document.getElementById("pud").style.display = "none";
  historyChart.resize();
  if (forecastPanel.classList.contains("open")) showForecast();
}

function closeDrawer() {
  drawer.classList.remove("open");
  document.getElementById("pud").style.display = "";
  if (selectedId && markers[selectedId]) markers[selectedId].classList.remove("selected");
  if (selectedId) map.setFeatureState({ source: "campuses", id: selectedId }, { selected: false });
  selectedId = null;
  if (map.getSource("plume")) {
    map.getSource("plume").setData({ type: "FeatureCollection", features: [] });
  }
}

function makeCharts() {
  const theme = chartTheme();
  historyChart = new Chart(document.getElementById("history"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Campus core", data: [], borderColor: "#fb923c", backgroundColor: "rgba(251,146,60,0.15)", fill: true, pointRadius: 2, borderWidth: 3, tension: 0.25 },
        { label: "Irrigated cropland", data: [], borderColor: "#38bdf8", pointRadius: 0, tension: 0.2 },
        { label: "Dry rangeland", data: [], borderColor: "#94a3b8", pointRadius: 0, tension: 0.2 },
      ],
    },
    options: theme,
  });
  decayChart = new Chart(document.getElementById("decay"), {
    type: "line",
    data: {
      labels: [],
      datasets: [{ label: "°C above cropland", data: [], borderColor: "#fb923c", pointRadius: 2, tension: 0.25 }],
    },
    options: { ...theme, plugins: { legend: { display: false } } },
  });
}

function addMarkers(data) {
  data.features.forEach((feature) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "campus-pill";
    button.innerHTML = `<span class="pill-name">${SHORT_NAME[feature.properties.id] || feature.properties.name}</span><span class="pill-gap"></span>`;
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openCampus(feature);
    });
    markers[feature.properties.id] = button;
    const [lon, lat] = centroid(feature);
    new maplibregl.Marker({ element: button, anchor: "bottom" }).setLngLat([lon, lat]).addTo(map);
  });
}

map.on("load", async () => {
  const [facilityData, zoneData, metricData] = await Promise.all([
    fetch("../data/geojson/facilities_quincy.geojson").then((response) => response.json()),
    fetch("../data/geojson/zones_quincy.geojson").then((response) => response.json()),
    fetch("metrics.json?v=2008").then((response) => response.json()),
  ]);
  metrics = metricData;
  campusesCache = facilityData;
  drawSpark(meanDivergence());
  fillRise();

  heatBounds = await fetch("overlay_bounds.json?v=2008").then((response) => response.json());
  const startYear = Number(yearInput.value);
  addHeatLayer("field", startYear, "visible");
  ["a", "b", "c"].forEach((part) => addHeatLayer(part, startYear, "none"));

  map.addSource("zones", { type: "geojson", data: zoneData });
  [
    ["zone-c", "C_control", "#38bdf8", "none"],
    ["zone-b", "B_perimeter", "#fbbf24", "none"],
    ["zone-a", "A_core", "#fb923c", "none"],
  ].forEach(([id, zoneType, color, visibility]) => {
    map.addLayer({
      id,
      type: "line",
      source: "zones",
      filter: ["==", ["get", "zone_type"], zoneType],
      layout: { visibility },
      paint: { "line-color": color, "line-width": 2 },
    });
  });

  map.addSource("campuses", { type: "geojson", data: facilityData, promoteId: "id" });
  map.addLayer({
    id: "campuses-fill",
    type: "fill",
    source: "campuses",
    paint: {
      "fill-color": ["case", ["boolean", ["feature-state", "selected"], false], "#fb923c", "#ffffff"],
      "fill-opacity": 0.02,
    },
  });
  map.addLayer({
    id: "campuses-line",
    type: "line",
    source: "campuses",
    paint: {
      "line-color": ["case", ["boolean", ["feature-state", "selected"], false], "#fb923c", "#ffffff"],
      "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 2, 1],
      "line-opacity": 0.55,
    },
  });
  map.addSource("plume", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "plume",
    type: "line",
    source: "plume",
    paint: { "line-color": "#fb923c", "line-width": 2, "line-dasharray": [2, 1] },
  });

  addMarkers(facilityData);
  updatePills();
  map.on("click", "campuses-fill", (event) => openCampus(event.features[0]));
  map.on("mouseenter", "campuses-fill", () => { map.getCanvas().style.cursor = "pointer"; });
  map.on("mouseleave", "campuses-fill", () => { map.getCanvas().style.cursor = ""; });
  makeCharts();
});

function gapSeries(id) {
  const series = metrics.facilities[id].series;
  return subtract(series.A_core, series.C2_cropland);
}

function average(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function updatePills() {
  if (!metrics) return;
  const index = metrics.years.indexOf(Number(yearInput.value));
  Object.entries(markers).forEach(([id, button]) => {
    const gap = gapSeries(id)[index];
    button.querySelector(".pill-gap").textContent = `${gap.toFixed(1)}°C above fields`;
  });
}

function fillRise() {
  const list = document.getElementById("rise-list");
  list.innerHTML = "";
  Object.entries(metrics.facilities).forEach(([id, record]) => {
    const gaps = gapSeries(id);
    const early = average(gaps.slice(0, 3));
    const late = average(gaps.slice(-3));
    const row = document.createElement("div");
    row.className = "rise-row";
    row.innerHTML = `<span>${SHORT_NAME[id]}</span><span class="now">${late.toFixed(1)}°C</span><span class="delta">+${(late - early).toFixed(1)}°C</span>`;
    list.appendChild(row);
  });
  const caption = document.getElementById("spark-caption");
  if (caption) {
    const all = metrics.years.map((_, index) => average(Object.keys(metrics.facilities).map((id) => gapSeries(id)[index])));
    const earlyLabel = `${metrics.years[0]}–${String(metrics.years[2]).slice(2)}`;
    const lateLabel = `${metrics.years.at(-3)}–${String(metrics.years.at(-1)).slice(2)}`;
    caption.textContent = `${earlyLabel}: ${average(all.slice(0, 3)).toFixed(1)}°C above fields → ${lateLabel}: ${average(all.slice(-3)).toFixed(1)}°C above fields`;
  }
}

const ZONE_PARTS = { a: "show-a", b: "show-b", c: "show-c" };

function heatUrl(year, part) {
  const suffix = part === "field" ? "field" : part;
  return `../data/overlays/clipped/quincy_${year}_${suffix}.png`;
}

function addHeatLayer(part, year, visibility) {
  heatYear[part] = year;
  map.addSource(`heat-${part}`, {
    type: "image",
    url: heatUrl(year, part),
    coordinates: heatBounds[String(year)],
  });
  map.addLayer({
    id: `heat-${part}`,
    type: "raster",
    source: `heat-${part}`,
    layout: { visibility },
  });
}

function showHeat(part, year) {
  if (heatYear[part] === year) return;
  const source = map.getSource(`heat-${part}`);
  if (!source || !heatBounds[String(year)]) return;
  heatYear[part] = year;
  try {
    const pending = source.updateImage({
      url: heatUrl(year, part),
      coordinates: heatBounds[String(year)],
    });
    if (pending && typeof pending.catch === "function") pending.catch(() => {});
  } catch (error) {
    if (error.name !== "AbortError") console.error(error);
  }
}

function applyHeat() {
  const year = Number(yearInput.value);
  yearLabel.textContent = year;
  updatePills();
  if (!map.getSource("heat-field") || !heatBounds || !heatBounds[String(year)]) return;
  showHeat("field", year);
  Object.entries(ZONE_PARTS).forEach(([part, inputId]) => {
    const on = document.getElementById(inputId).checked;
    map.setLayoutProperty(`heat-${part}`, "visibility", on ? "visible" : "none");
    if (on) showHeat(part, year);
  });
}

yearInput.addEventListener("input", applyHeat);

function setZone(id, visible) {
  if (!map.getLayer(id)) return;
  map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}
document.getElementById("show-a").addEventListener("change", (event) => {
  setZone("zone-a", event.target.checked);
  applyHeat();
});
document.getElementById("show-b").addEventListener("change", (event) => {
  setZone("zone-b", event.target.checked);
  applyHeat();
});
document.getElementById("show-c").addEventListener("change", (event) => {
  setZone("zone-c", event.target.checked);
  applyHeat();
});

acresInput.addEventListener("input", showForecast);
mwInput.addEventListener("input", showForecast);
document.getElementById("forecast-acres").addEventListener("click", showForecast);
document.getElementById("forecast-mw").addEventListener("click", showForecast);
document.getElementById("close").addEventListener("click", closeDrawer);
