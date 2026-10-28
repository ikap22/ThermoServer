#!/usr/bin/env python3
"""Build Quincy campus footprints and distance zones. Geometry only.

Replaces a placeholder rectangle with an OpenStreetMap footprint when a
parcel of plausible size is found. Projects to UTM zone 11N, builds
zone A (0-250 m), zone B (250-1000 m), and zone C (3000-5000 m), and
clips zone C to the irrigated valley south of 47.26 N so the bare hills
north of Quincy are not used as the control.

Week 5 fills temperatures. This script does not read them into the database.

Writes:
  data/geojson/facilities_quincy.geojson   updated footprints
  data/geojson/zones_quincy.geojson
  data/overlays/zones_on_2024.png
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import geopandas as gpd
import rasterio
from matplotlib.patches import Patch
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

FACILITIES_PATH = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
ZONES_PATH = ROOT / "data" / "geojson" / "zones_quincy.geojson"
LST_2024 = ROOT / "data" / "raw" / "lst_2024.tif"
ZONES_PNG = ROOT / "data" / "overlays" / "zones_on_2024.png"

UTM = "EPSG:32611"
WGS84 = "EPSG:4326"
ACRE_M2 = 4046.8564224
A_M, B_M, C_INNER_M, C_OUTER_M = 250, 1000, 3000, 5000
# North of this line is the dry escarpment, which runs hotter than the campuses.
VALLEY_NORTH = 47.26
ANALYSIS_BBOX = (-119.97, 47.16, -119.74, 47.30)
# Named OpenStreetMap parcels. The spec rectangles put Vantage and H5 next to
# Microsoft; the mapped campuses are several kilometers east. Sabey has no
# feature under that name; Intergate Quincy is Sabey's campus there.
OSM_WAYS = {
    "msft-columbia": 283051497,
    "vantage-quincy": 283051473,
    "sabey-sdc": 283051472,
    "h5-quincy": 283051474,
}
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)


def acres(geom) -> float:
    series = gpd.GeoSeries([geom], crs=WGS84).to_crs(UTM)
    return float(series.area.iloc[0] / ACRE_M2)


def way_polygon(element: dict) -> Polygon | None:
    coords = [(pt["lon"], pt["lat"]) for pt in element.get("geometry", [])]
    if len(coords) < 4:
        return None
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    geom = Polygon(coords)
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty or geom.geom_type != "Polygon":
        return None
    return geom


def fetch_osm() -> list[dict]:
    ids = ",".join(str(way_id) for way_id in OSM_WAYS.values())
    query = f"""
    [out:json][timeout:60];
    way(id:{ids});
    out geom;
    """
    payload = urllib.parse.urlencode({"data": query}).encode()
    headers = {
        "User-Agent": "ThermoServerCapstone/1.0 (educational; Quincy WA)",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    last_error = None
    for url in OVERPASS_URLS:
        try:
            request = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                payload_json = json.load(response)
            print(f"OpenStreetMap: {len(payload_json.get('elements', []))} ways from {url}")
            return payload_json.get("elements", [])
        except Exception as exc:
            last_error = exc
            print(f"Overpass failed ({url}): {exc}")
    raise RuntimeError("Could not reach Overpass") from last_error


def load_footprints() -> gpd.GeoDataFrame:
    facilities = gpd.read_file(FACILITIES_PATH)
    if facilities.crs is None:
        facilities = facilities.set_crs(WGS84)
    try:
        elements = fetch_osm()
    except Exception as exc:
        print(f"OSM lookup failed ({exc}). Keeping the footprints already on disk.")
        if "footprint_source" not in facilities.columns:
            facilities["footprint_source"] = "placeholder"
        return facilities

    by_id = {}
    for element in elements:
        geom = way_polygon(element)
        if geom is not None:
            by_id[element["id"]] = (geom, element.get("tags", {}).get("name", ""))

    geometries = []
    sources = []
    osm_names = []
    for _, row in facilities.iterrows():
        match = by_id.get(OSM_WAYS[row["id"]])
        if match is None:
            print(f"  {row['id']}: OSM way missing, keeping the current footprint")
            geometries.append(row.geometry)
            sources.append(row.get("footprint_source", "placeholder"))
            osm_names.append("")
            continue
        geom, name = match
        print(f"  {row['id']}: OSM '{name}' ({acres(geom):.0f} acres)")
        geometries.append(geom)
        sources.append("osm-name")
        osm_names.append(name)
    facilities = facilities.set_geometry(geometries)
    facilities["footprint_source"] = sources
    facilities["osm_name"] = osm_names
    return facilities


def build_zones(facilities: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    projected = facilities.to_crs(UTM)
    valley = box(ANALYSIS_BBOX[0], ANALYSIS_BBOX[1], ANALYSIS_BBOX[2], VALLEY_NORTH)
    cores = unary_union([geom.buffer(A_M) for geom in projected.geometry])
    cores_wgs = gpd.GeoSeries([cores], crs=UTM).to_crs(WGS84).iloc[0]

    rows = []
    for _, row in projected.iterrows():
        footprint = row.geometry
        rings = {
            "A_core": footprint.buffer(A_M),
            "B_perimeter": footprint.buffer(B_M).difference(footprint.buffer(A_M)),
            "C_control": footprint.buffer(C_OUTER_M).difference(footprint.buffer(C_INNER_M)),
        }
        for zone_type, geom in rings.items():
            geom_wgs = gpd.GeoSeries([geom], crs=UTM).to_crs(WGS84).iloc[0]
            if zone_type == "C_control":
                geom_wgs = geom_wgs.intersection(valley).difference(cores_wgs)
            if geom_wgs.is_empty:
                print(f"  {row['id']} {zone_type} is empty after clipping")
                continue
            rows.append({
                "facility_id": row["id"],
                "zone_type": zone_type,
                "geometry": geom_wgs,
            })
    zones = gpd.GeoDataFrame(rows, crs=WGS84)
    return zones


ZONE_STYLE = (
    ("C_control", "#4C78A8", "Blue: farmland 3–5 km away, the comparison"),
    ("B_perimeter", "#F2C14E", "Gold: 250 m to 1 km from the buildings"),
    ("A_core", "#E4572E", "Red: within 250 m of the buildings"),
)


def plot_zones(facilities: gpd.GeoDataFrame, zones: gpd.GeoDataFrame) -> None:
    """One panel per campus so the rings do not pile on top of each other."""
    if not LST_2024.exists():
        print(f"No {LST_2024.name} yet; zone map skipped.")
        return
    with rasterio.open(LST_2024) as src:
        lst = src.read(1)
        transform, crs = src.transform, src.crs
    height, width = lst.shape
    left, top = transform.c, transform.f
    extent = (left, left + width * transform.a, top + height * transform.e, top)

    facilities_utm = facilities.to_crs(crs)
    zones_utm = zones.to_crs(crs)
    fig, axes = plt.subplots(2, 2, figsize=(12, 11))
    image = None
    for ax, (_, campus) in zip(axes.ravel(), facilities_utm.iterrows()):
        image = ax.imshow(lst, cmap="inferno", extent=extent, vmin=30, vmax=60)
        campus_zones = zones_utm[zones_utm["facility_id"] == campus["id"]]
        for zone_type, color, _ in ZONE_STYLE:
            subset = campus_zones[campus_zones["zone_type"] == zone_type]
            if subset.empty:
                continue
            subset.plot(ax=ax, color=color, alpha=0.55, linewidth=0)
            subset.boundary.plot(ax=ax, color="white", linewidth=1.0)
        others = facilities_utm[facilities_utm["id"] != campus["id"]]
        others.boundary.plot(ax=ax, color="white", linewidth=0.6, linestyle="--")
        footprint = facilities_utm[facilities_utm["id"] == campus["id"]]
        footprint.boundary.plot(ax=ax, color="white", linewidth=1.6)

        control = campus_zones[campus_zones["zone_type"] == "C_control"]
        minx, miny, maxx, maxy = control.total_bounds
        pad = 350
        ax.set_xlim(minx - pad, maxx + pad)
        ax.set_ylim(miny - pad, maxy + pad)
        ax.set_title(campus["name"], fontsize=12)
        ax.set_xticks([])
        ax.set_yticks([])
        x0, y0 = minx - pad + 500, miny - pad + 500
        ax.plot([x0, x0 + 1000], [y0, y0], color="white", linewidth=3, solid_capstyle="butt")
        ax.text(x0 + 500, y0 + 280, "1 km", color="white", ha="center", va="bottom", fontsize=8)

    fig.suptitle(
        "Quincy, summer 2024: where temperature is measured around each data center",
        fontsize=13,
        y=0.98,
    )
    fig.text(
        0.46,
        0.935,
        "Red is next to the buildings. Gold is the nearby neighborhood. Blue is far farmland, the comparison.",
        ha="center",
        fontsize=10,
    )
    colorbar_ax = fig.add_axes((0.90, 0.28, 0.018, 0.52))
    fig.colorbar(image, cax=colorbar_ax, label="Ground temperature (°C)")
    fig.legend(
        handles=[Patch(facecolor=color, edgecolor="white", alpha=0.75, label=label) for _, color, label in ZONE_STYLE]
        + [Patch(facecolor="none", edgecolor="black", label="Solid white outline: this data center. Dashed: a neighboring one.")],
        loc="lower center",
        ncol=2,
        frameon=False,
        bbox_to_anchor=(0.46, 0.02),
    )
    fig.subplots_adjust(left=0.04, right=0.88, top=0.86, bottom=0.14, wspace=0.14, hspace=0.32)
    ZONES_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(ZONES_PNG, dpi=160)
    plt.close(fig)
    print(f"Saved {ZONES_PNG.relative_to(ROOT)}")


def main() -> None:
    facilities = load_footprints()
    FACILITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if FACILITIES_PATH.exists():
        FACILITIES_PATH.unlink()
    facilities.to_file(FACILITIES_PATH, driver="GeoJSON")
    print(f"Saved {FACILITIES_PATH.relative_to(ROOT)}")

    zones = build_zones(facilities)
    if ZONES_PATH.exists():
        ZONES_PATH.unlink()
    zones.to_file(ZONES_PATH, driver="GeoJSON")
    print(f"Saved {ZONES_PATH.relative_to(ROOT)} ({len(zones)} polygons)")
    for _, row in zones.iterrows():
        print(f"  {row['facility_id']:<16} {row['zone_type']:<12} {acres(row.geometry):7.0f} acres")
    plot_zones(facilities, zones)


if __name__ == "__main__":
    main()
