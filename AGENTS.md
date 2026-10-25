# ThermoServer agent guide

Follow this schedule. Finish the current week's artifacts before starting the next week. Do not add an S3 bucket, a hosted database, a map token, or a fifth model feature.

Week 3 is done: `pipeline/fetch_landsat.py` pulls a clear summer Landsat scene, converts it to °C, and wrote `data/overlays/quincy_2024_preview.png` (Landsat 9, 2024-07-11, 0% cloud; campuses 45–50 °C vs a 43.2 °C area mean). Campus outlines in `data/geojson/facilities_quincy.geojson` are still placeholder rectangles. Buffer rings, the database, the model, the API, and the site are not built. Those were listed under week 3 in the spec and move to the start of week 4.

Dates below assume week 4 ends Saturday, Oct 24, 2026. Career-prep items (LinkedIn, resume, budget, outfits) and the separate portfolio site stay the student's tasks. This file only covers ThermoServer, plus what to bring to the reviews already on the spec schedule.

## Cloud: no S3

The live site never touches satellite files. All heavy work stays on the laptop. The deployed app only serves a SQLite file, a small `.joblib` model, and five PNG overlays.

- Do not create an S3 bucket. The spec’s own contingency says free hosts crash if they process rasters live, so statistics are precomputed and heat maps are static PNGs. An S3 bucket would add an AWS account, keys, and a billing setup for files that are a few megabytes. That conflicts with the zero-cost rule.
- Vercel hosts the static site and the PNGs (`frontend/public/overlays/`). MapLibre loads them by bounding box. No map token. Basemap is CartoDB Positron, which needs no key.
- Render hosts the FastAPI process. The SQLite file and `ml/decay_model.joblib` ship inside the deploy from GitHub (`https://github.com/ikap22/ThermoServer.git`). No hosted database.
- Planetary Computer is used only by the local scripts. The public site never calls it.
- Create the free Render and Vercel accounts in week 7, while the API already runs locally. Week 10 is the actual deploy, not the first time the accounts are created.

## Week 4 (through Oct 24): all summers, plus the rings

Spec sprint 2 starts here. Two scripts, two pictures.

- Extend `pipeline/fetch_landsat.py` with `--year all` for 2015–2026. Widen the search from July 1 to the spec window June 15–August 31. For each year, take the clear Landsat 8/9 scenes (cloud cover under 10%, QA mask already in the script) and save the median temperature and NDVI, not a single date. That is the spec’s smoke contingency. Write `data/raw/lst_{year}.tif`, `data/raw/ndvi_{year}.tif`, and a small preview per year.
- Add `pipeline/compute_buffers.py` for geometry only this week. Pull OpenStreetMap footprints for the four Quincy campuses (free Overpass API, no key). If a campus has no usable footprint, keep its rectangle and say so. Project to EPSG:32611 and write zone polygons to `data/geojson/zones_quincy.geojson`: A 0–250 m, B 250–1000 m, C 3000–5000 m. Do not compute temperatures yet.
- Draw the rings on the 2024 temperature raster. Keep zone C inside the irrigated valley. The bare hills north of Quincy run hotter than the campuses (~60 °C), so a control placed there would hide the data-center signal.

Status-report artifacts:

1. `data/overlays/zones_on_2024.png` — campus outlines and the three rings on the 2024 heat map.
2. `data/overlays/yearly_contact_sheet.png` — one thumbnail per year, 2015–2026, with scene count and mean °C in the caption. Plus the terminal summary (year, scene ids, cloud %).

Meetings this week are the student's, not code: GIS faculty review of the ring diagram, and stakeholder review 1 of a rough dashboard sketch. Bring artifact 1.

## Week 5 (Oct 25–31): ten-year metrics in SQLite

- Finish `compute_buffers.py` so it reads each year’s GeoTIFF, masks pixels inside each zone, and stores mean and median °C plus mean NDVI.
- Split zone C by NDVI inside the ring: C1 unirrigated (low NDVI) and C2 irrigated (high NDVI). Thresholds come from that year’s pixels, starting near 0.2 and 0.5, and are printed so they can be defended in the GIS review.
- Also store 250 m distance rings out to 2 km in a second table, `decay_samples`. The public table stays the spec schema; the extra rings exist so week 6 has a real decay curve instead of three points.
- One pre-2011 NDVI grid (Landsat 5 or 7, summer ~2009) is fetched only for the model feature `pre_construction_ndvi`. The public timeline stays 2015–2026, because the campuses were commissioned in 2011–2013 and 2015 is not before construction. If that older scene is unusable, fall back to 2015 NDVI and label it earliest-available.
- Schema matches the spec exactly:

```sql
CREATE TABLE thermal_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  facility_id TEXT NOT NULL, year INTEGER NOT NULL, zone_type TEXT NOT NULL,
  mean_surface_temp_c REAL NOT NULL, median_surface_temp_c REAL NOT NULL,
  delta_vs_c1_rangeland_c REAL NOT NULL, delta_vs_c2_cropland_c REAL NOT NULL,
  ndvi_mean REAL, UNIQUE (facility_id, year, zone_type));
```

- `pytest` check: every polygon is valid, every temperature is between -10 and 65 °C, every year from 2015–2026 has all four campuses.

Status-report artifacts:

1. A matplotlib bar or line chart, `data/overlays/msft_zones_2015_2026.png`, of one campus (Microsoft) with zone A, B, C1, and C2 across the decade.
2. Terminal output of `SELECT facility_id, year, zone_type, mean_surface_temp_c, delta_vs_c2_cropland_c FROM thermal_metrics` showing a full year for all four campuses.

## Week 6 (Nov 1–7): the model and the map images

- `pipeline/train_model.py` builds a table with exactly four columns: `distance_meters`, `campus_acreage`, `pre_construction_ndvi`, `baseline_temp_c`. Target is zone temperature minus the C2 baseline. Train `RandomForestRegressor`, print R² and RMSE, and save `ml/decay_model.joblib`. Spec goal is R² above 0.80. If the score is lower, report the real number and keep the model; do not add a fifth feature to chase the target. Megawatts stay a display field, not a model input.
- A test asserts predicted temperature falls as distance increases.
- Export clean overlays for 2015, 2018, 2021, 2024, and 2026: no axes, no titles, fixed color scale, cropped to the MapLibre box `[[-119.90,47.26],[-119.80,47.26],[-119.80,47.20],[-119.90,47.20]]`. Save as `data/overlays/quincy_{year}.png`.

Status-report artifacts:

1. `data/overlays/decay_curve.png` — predicted °C versus distance for a real campus, with the measured ring averages as points.
2. `data/overlays/quincy_2015.png` and `data/overlays/quincy_2026.png` side by side (the before/after the slider will use).

## Week 7 (Nov 8–14): FastAPI, still local

- Fill `backend/main.py`:
  - `GET /api/facilities` returns the campus GeoJSON.
  - `GET /api/facilities/{id}/timeseries` returns yearly zone temperatures.
  - `POST /api/forecast` takes acreage (and a power value that is accepted but not fed to the model) and returns decay points. Negative or extreme acreage returns HTTP 422.
- CORS allows `http://localhost` now and the Vercel domain later.
- pytest with FastAPI’s test client: HTTP 200, JSON shape, unknown campus id, under 100 ms locally.
- Create the free Render and Vercel accounts and connect them to `ikap22/ThermoServer`. Do not deploy yet.

Status-report artifacts:

1. Screenshot of `/docs` (Swagger) showing the three routes and a successful forecast response.
2. pytest transcript, all passed.

Meeting: software-engineer expert review over Zoom. Walk through the three routes and the tests.

## Week 8 (Nov 15–21): the map

- Static frontend in `frontend/`: MapLibre GL, Tailwind, Carto Positron, no API key. Campus polygons from `GET /api/facilities`. Heat overlays are the five PNGs as image sources on the fixed bounds.
- Year slider swaps 2015, 2018, 2021, 2024, 2026. A swipe control compares 2015 and 2026.
- Clicking a campus opens an empty drawer (charts arrive next week).
- Check desktop and a tablet-width window in the browser.

Status-report artifacts:

1. Screenshot of the map with a campus selected and the 2024 overlay on.
2. Screenshot of the 2015/2026 swipe at mid-drag.

Meeting: capstone code review (Zoom, ~60 min) of the API routes and the map. Apply readability fixes before week 9.

## Week 9 (Nov 22–28): charts and the simulator

- Chart.js in the campus drawer: ten-year lines for zones A, B, C1, C2, in °C, with the delta versus irrigated cropland written out in °C and °F. Grant County PUD context card uses the spec figures (~37% of county load, ~85% hydro from Wanapum and Priest Rapids) as static text, not a live utility API.
- Acreage and power sliders call `POST /api/forecast` and redraw the decay curve without a page reload. The chart caption states the curve is driven by acreage, distance, pre-construction NDVI, and baseline temperature.
- Run through the flow: pick a campus, read the trend, move the acreage slider.

Status-report artifacts:

1. Screenshot of the analytics drawer with the ten-year chart.
2. Screenshot of the simulator curve before and after raising acreage.

Meeting: stakeholder review 2. Have them pick a campus, read a trend, and move the acreage slider, and note where they stall. The spec’s usability bar is 80% completing it in under three minutes; one session will not prove that, so write down what was observed.

## Week 10 (Nov 29–Dec 5): deploy

- Push `main`. Render web service: `uvicorn backend.main:app --host 0.0.0.0 --port $PORT`, Python from `requirements.txt`. SQLite and the joblib file are in the repo. Free instances sleep; the first request after idle is slow, later ones should be under the spec’s 75 ms forecast budget.
- Vercel project rooted at `frontend/`, with the five PNGs in `frontend/public/overlays/`. Set the API base URL to the Render URL. Set Render CORS to the Vercel URL.
- Re-run pytest against the local app, then click through the live URL in Chrome, Firefox, and Safari. Lighthouse accessibility pass; fix contrast until the score is at least 90.
- README: how to run the pipeline, train the model, and start the API locally.

Status-report artifacts:

1. Screenshot of the live Vercel URL with the map loaded (include the URL in the report).
2. Lighthouse accessibility score screenshot.

Poster, colloquium dry run, and final docs stay on the spec’s finals week, after week 10.

## Order dependency

Week 5 cannot start until week 4’s rasters and zone polygons exist. Week 6 needs the database. Week 7 needs the model file. Week 8 needs the five PNGs and the API. Week 9 needs both drawers talking to that API. Week 10 only deploys what already works locally. If a week slips, cut the swipe control before cutting the year slider, the charts, or the forecast.
