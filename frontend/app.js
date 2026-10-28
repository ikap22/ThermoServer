const YEARS = [];
for (let year = 2015; year <= 2026; year += 1) YEARS.push(year);

const OVERLAY_COORDS = [
  [-119.90, 47.26],
  [-119.80, 47.26],
  [-119.80, 47.20],
  [-119.90, 47.20],
];

const yearInput = document.getElementById("year");
const yearLabel = document.getElementById("year-label");
const drawer = document.getElementById("drawer");
const acresInput = document.getElementById("acres");
const mwInput = document.getElementById("mw");

let metrics = null;
let selectedId = null;
let historyChart = null;
let decayChart = null;

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/positron",
  center: [-119.84, 47.246],
  zoom: 12,
});

map.addControl(new maplibregl.NavigationControl(), "top-right");

function draftCurve(acreage, mw) {
  const peak = 5 + (acreage - 70) * 0.035 + (mw - 100) * 0.008;
  const scale = 400 + acreage * 6;
  const distances = [0, 100, 250, 500, 750, 1000, 1500, 2000, 3000, 5000];
  const temps = distances.map((distance) => Math.max(0, peak * Math.exp(-distance / scale)));
  const radius = Math.min(5000, Math.max(250, scale * Math.log(peak / 0.5)));
  return { distances, temps, radius };
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

function updateDecay() {
  const acreage = Number(acresInput.value);
  const mw = Number(mwInput.value);
  document.getElementById("acre-label").textContent = acreage;
  document.getElementById("mw-label").textContent = mw;
  if (!decayChart) return;
  const curve = draftCurve(acreage, mw);
  decayChart.data.labels = curve.distances;
  decayChart.data.datasets[0].data = curve.temps;
  decayChart.update();

  if (!selectedId || !campusesCache || !map.getSource("plume")) return;
  const feature = campusesCache.features.find((item) => item.properties.id === selectedId);
  const [lon, lat] = centroid(feature);
  map.getSource("plume").setData({ type: "FeatureCollection", features: [ring(lon, lat, curve.radius)] });
}

function openCampus(feature) {
  const props = feature.properties;
  selectedId = props.id;
  const record = metrics.facilities[props.id];
  document.getElementById("campus-name").textContent = record.name;
  document.getElementById("campus-meta").textContent = `${record.operator} · ${record.acreage} acres · ${record.estimated_mw} MW`;
  const last = record.series.A_core.length - 1;
  const gap = record.series.A_core[last] - record.series.C2_cropland[last];
  document.getElementById("campus-gap").textContent =
    `In ${metrics.years[last]}, the core averaged ${gap.toFixed(1)}°C above irrigated cropland in the control ring.`;

  historyChart.data.labels = metrics.years;
  historyChart.data.datasets[0].data = record.series.A_core;
  historyChart.data.datasets[1].data = record.series.C2_cropland;
  historyChart.data.datasets[2].data = record.series.C1_rangeland;
  historyChart.update();

  map.setFilter("campuses-fill", ["!=", ["get", "id"], ""]);
  campusesCache.features.forEach((item) => {
    map.setFeatureState({ source: "campuses", id: item.properties.id }, { selected: item.properties.id === props.id });
  });
  drawer.classList.add("open");
  historyChart.resize();
  updateDecay();
}

let campusesCache = null;

function makeCharts() {
  historyChart = new Chart(document.getElementById("history"), {
    type: "line",
    data: {
      labels: [],
      datasets: [
        { label: "Campus core", data: [], borderColor: "#c0392b", pointRadius: 2, tension: 0.15 },
        { label: "Irrigated cropland", data: [], borderColor: "#1a7f37", pointRadius: 2, tension: 0.15 },
        { label: "Dry rangeland", data: [], borderColor: "#9a6b12", pointRadius: 2, tension: 0.15 },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "bottom" } },
      scales: { y: { title: { display: true, text: "°C" } } },
    },
  });

  decayChart = new Chart(document.getElementById("decay"), {
    type: "line",
    data: {
      labels: [],
      datasets: [{ label: "°C above baseline", data: [], borderColor: "#c0392b", pointRadius: 2, tension: 0.2 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { title: { display: true, text: "meters from edge" } },
        y: { min: 0, title: { display: true, text: "°C" } },
      },
    },
  });
  updateDecay();
}

map.on("load", async () => {
  const [facilityData, zoneData, metricData] = await Promise.all([
    fetch("../data/geojson/facilities_quincy.geojson").then((response) => response.json()),
    fetch("../data/geojson/zones_quincy.geojson").then((response) => response.json()),
    fetch("metrics.json").then((response) => response.json()),
  ]);
  metrics = metricData;
  campusesCache = facilityData;

  YEARS.forEach((year) => {
    map.addSource(`heat-${year}`, {
      type: "image",
      url: `../data/overlays/quincy_${year}.png`,
      coordinates: OVERLAY_COORDS,
    });
    map.addLayer({
      id: `heat-${year}`,
      type: "raster",
      source: `heat-${year}`,
      layout: { visibility: year === 2024 ? "visible" : "none" },
    });
  });

  map.addSource("zones", { type: "geojson", data: zoneData });
  map.addLayer({
    id: "zone-c",
    type: "line",
    source: "zones",
    filter: ["==", ["get", "zone_type"], "C_control"],
    layout: { visibility: "none" },
    paint: { "line-color": "#1d4e89", "line-width": 1.5 },
  });
  map.addLayer({
    id: "zone-b",
    type: "line",
    source: "zones",
    filter: ["==", ["get", "zone_type"], "B_perimeter"],
    layout: { visibility: "none" },
    paint: { "line-color": "#d97706", "line-width": 1.5 },
  });
  map.addLayer({
    id: "zone-a",
    type: "line",
    source: "zones",
    filter: ["==", ["get", "zone_type"], "A_core"],
    paint: { "line-color": "#c0392b", "line-width": 2 },
  });

  map.addSource("campuses", { type: "geojson", data: facilityData, promoteId: "id" });
  map.addLayer({
    id: "campuses-fill",
    type: "fill",
    source: "campuses",
    paint: {
      "fill-color": ["case", ["boolean", ["feature-state", "selected"], false], "#f4a261", "#ffffff"],
      "fill-opacity": 0.45,
    },
  });
  map.addLayer({
    id: "campuses-line",
    type: "line",
    source: "campuses",
    paint: { "line-color": "#111", "line-width": 2 },
  });
  map.addLayer({
    id: "campus-labels",
    type: "symbol",
    source: "campuses",
    layout: {
      "text-field": ["get", "name"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 12,
      "text-offset": [0, 1.2],
    },
    paint: { "text-color": "#111", "text-halo-color": "#fff", "text-halo-width": 1.5 },
  });

  map.addSource("plume", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({
    id: "plume",
    type: "line",
    source: "plume",
    paint: { "line-color": "#c0392b", "line-width": 2, "line-dasharray": [2, 1] },
  });

  map.on("click", "campuses-fill", (event) => {
    openCampus(event.features[0]);
  });
  map.on("mouseenter", "campuses-fill", () => {
    map.getCanvas().style.cursor = "pointer";
  });
  map.on("mouseleave", "campuses-fill", () => {
    map.getCanvas().style.cursor = "";
  });

  makeCharts();
});

yearInput.addEventListener("input", () => {
  const year = Number(yearInput.value);
  yearLabel.textContent = year;
  if (!map.getLayer("heat-2015")) return;
  YEARS.forEach((item) => {
    map.setLayoutProperty(`heat-${item}`, "visibility", item === year ? "visible" : "none");
  });
});

function setZone(id, visible) {
  if (!map.getLayer(id)) return;
  map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}
document.getElementById("show-a").addEventListener("change", (event) => setZone("zone-a", event.target.checked));
document.getElementById("show-b").addEventListener("change", (event) => setZone("zone-b", event.target.checked));
document.getElementById("show-c").addEventListener("change", (event) => setZone("zone-c", event.target.checked));

acresInput.addEventListener("input", updateDecay);
mwInput.addEventListener("input", updateDecay);
document.getElementById("forecast").addEventListener("click", updateDecay);
document.getElementById("close").addEventListener("click", () => {
  drawer.classList.remove("open");
  if (selectedId) {
    map.setFeatureState({ source: "campuses", id: selectedId }, { selected: false });
  }
  selectedId = null;
  if (map.getSource("plume")) {
    map.getSource("plume").setData({ type: "FeatureCollection", features: [] });
  }
});
