#!/usr/bin/env python3
"""Build the draft map images and the numbers the page charts.

Writes data/overlays/quincy_{year}.png for 2015-2026, cropped to the
MapLibre overlay box, and frontend/metrics.json with zone temperatures.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from matplotlib import colormaps
from PIL import Image
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OVERLAYS = ROOT / "data" / "overlays"
FACILITIES = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
ZONES = ROOT / "data" / "geojson" / "zones_quincy.geojson"
METRICS = ROOT / "frontend" / "metrics.json"

OVERLAY_BBOX = (-119.90, 47.20, -119.80, 47.26)  # west, south, east, north
VMIN, VMAX = 30.0, 60.0
YEARS = range(2015, 2027)
# Inside the valley control ring: low greenness vs irrigated fields.
NDVI_RANGELAND = 0.30
NDVI_CROPLAND = 0.50


def crop(path: Path) -> np.ndarray:
    with rasterio.open(path) as src:
        left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *OVERLAY_BBOX)
        window = from_bounds(left, bottom, right, top, transform=src.transform)
        window = window.round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        return src.read(1, window=window)


def write_png(year: int) -> None:
    data = crop(RAW / f"lst_{year}.tif")
    scaled = np.clip((data - VMIN) / (VMAX - VMIN), 0, 1)
    rgba = colormaps["inferno"](np.where(np.isfinite(data), scaled, 0))
    rgba[..., 3] = np.where(np.isfinite(data), 0.78, 0.0)
    image = Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA")
    out = OVERLAYS / f"quincy_{year}.png"
    image.save(out)
    print(f"  {out.name} {image.size[0]}x{image.size[1]}")


def zone_means(year: int, facilities: gpd.GeoDataFrame, zones: gpd.GeoDataFrame) -> dict:
    with rasterio.open(RAW / f"lst_{year}.tif") as lst_src, rasterio.open(RAW / f"ndvi_{year}.tif") as ndvi_src:
        lst = lst_src.read(1)
        ndvi = ndvi_src.read(1)
        transform, crs = lst_src.transform, lst_src.crs
    projected = zones.to_crs(crs)
    out = {}
    for _, campus in facilities.iterrows():
        campus_zones = projected[projected["facility_id"] == campus["id"]]
        stats = {}
        for _, zone in campus_zones.iterrows():
            inside = ~geometry_mask([zone.geometry], out_shape=lst.shape, transform=transform)
            if zone["zone_type"] != "C_control":
                stats[zone["zone_type"]] = round(float(np.nanmean(lst[inside])), 2)
                continue
            rangeland = inside & np.isfinite(ndvi) & (ndvi < NDVI_RANGELAND)
            cropland = inside & np.isfinite(ndvi) & (ndvi >= NDVI_CROPLAND)
            stats["C1_rangeland"] = round(float(np.nanmean(lst[rangeland])), 2) if rangeland.any() else None
            stats["C2_cropland"] = round(float(np.nanmean(lst[cropland])), 2) if cropland.any() else None
            stats["C_control"] = round(float(np.nanmean(lst[inside])), 2)
        out[campus["id"]] = stats
    return out


def write_metrics() -> None:
    facilities = gpd.read_file(FACILITIES)
    zones = gpd.read_file(ZONES)
    by_year = {year: zone_means(year, facilities, zones) for year in YEARS}
    payload = {"years": list(YEARS), "facilities": {}}
    for _, campus in facilities.iterrows():
        series = {key: [] for key in ("A_core", "B_perimeter", "C1_rangeland", "C2_cropland")}
        for year in YEARS:
            stats = by_year[year][campus["id"]]
            for key in series:
                series[key].append(stats.get(key))
        payload["facilities"][campus["id"]] = {
            "name": campus["name"],
            "operator": campus["operator"],
            "acreage": campus["acreage"],
            "estimated_mw": campus["estimated_mw"],
            "series": series,
        }
        core = series["A_core"][-1]
        crop = series["C2_cropland"][-1]
        print(f"  {campus['id']}: 2024 core {core} °C, cropland {crop} °C")
    METRICS.write_text(json.dumps(payload, indent=2))
    print(f"Saved {METRICS.relative_to(ROOT)}")


def main() -> None:
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    print("Overlay images")
    for year in YEARS:
        write_png(year)
    print("Chart numbers")
    write_metrics()


if __name__ == "__main__":
    main()
