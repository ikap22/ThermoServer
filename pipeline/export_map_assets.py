#!/usr/bin/env python3
"""Build the draft map images and the numbers the page charts.

Writes data/overlays/quincy_{year}.png for 2008-2026, cropped to the
MapLibre overlay box, and frontend/metrics.json with zone temperatures.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from matplotlib import colormaps
from PIL import Image
from scipy.ndimage import distance_transform_edt, gaussian_filter
from rasterio.features import geometry_mask
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_bounds
from shapely.ops import unary_union
from rasterio.windows import Window, from_bounds

import geopandas as gpd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
OVERLAYS = ROOT / "data" / "overlays"
FACILITIES = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
ZONES = ROOT / "data" / "geojson" / "zones_quincy.geojson"
METRICS = ROOT / "frontend" / "metrics.json"
CLIPPED = OVERLAYS / "clipped"
BOUNDS = ROOT / "frontend" / "overlay_bounds.json"

OVERLAY_BBOX = (-119.90, 47.20, -119.80, 47.26)  # west, south, east, north
VMIN, VMAX = 30.0, 60.0
# Color the map as degrees above irrigated cropland, not raw °C.
# A 30–60 scale hides a 4°C rise; 4–16 makes that rise change color.
ANOM_MIN, ANOM_MAX = 4.0, 16.0
YEARS = range(2008, 2027)
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


def radiating(data: np.ndarray, footprints, baseline: float, transform) -> Image.Image:
    """Real ground temperature, fading with distance from the buildings.

    The building edge is not a wall. Hotter ground stays bright and cooler
    ground drops out, so the campus reads as a glow instead of a cut-out.
    The glow dissolves by 2 km. Zone rings are drawn separately on the map.
    """
    valid = np.isfinite(data)
    filled = np.where(valid, data, baseline)
    smooth = gaussian_filter(filled.astype(np.float64), sigma=1.15)
    anomaly = smooth - baseline
    foot = ~geometry_mask([footprints], out_shape=data.shape, transform=transform, all_touched=True)
    dist_m = distance_transform_edt(~foot) * abs(transform.a)
    radius, feather = 2000.0, 700.0
    fade = np.clip((radius - dist_m) / feather, 0.0, 1.0)
    glow = np.clip((anomaly - 2.0) / 10.0, 0.0, 1.0)
    scaled = np.clip((anomaly - ANOM_MIN) / (ANOM_MAX - ANOM_MIN), 0.0, 1.0)
    rgba = colormaps["inferno"](scaled)
    visible = valid & (dist_m <= radius) & (glow > 0.02)
    rgba[..., 3] = np.where(visible, fade * (0.28 + 0.67 * glow), 0.0)
    return Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA")


def colorize(data: np.ndarray, inside: np.ndarray, baseline: float) -> Image.Image:
    anomaly = data - baseline
    scaled = np.clip((anomaly - ANOM_MIN) / (ANOM_MAX - ANOM_MIN), 0, 1)
    rgba = colormaps["inferno"](np.where(np.isfinite(data), scaled, 0))
    rgba[..., 3] = np.where(inside & np.isfinite(data), 0.92, 0.0)
    return Image.fromarray((rgba * 255).astype(np.uint8), mode="RGBA")


def image_corners(transform, width: int, height: int, crs) -> list[list[float]]:
    left, top = transform * (0, 0)
    right, bottom = transform * (width, height)
    xs, ys = warp_transform(crs, "EPSG:4326", [left, right, right, left], [top, top, bottom, bottom])
    return [[round(xs[i], 6), round(ys[i], 6)] for i in range(4)]


def zone_parts(facilities: gpd.GeoDataFrame, zones: gpd.GeoDataFrame, crs) -> dict:
    """Building footprints, then the part of each ring outside the buildings."""
    footprints = unary_union(facilities.to_crs(crs).geometry)
    projected = zones.to_crs(crs)
    core = unary_union(projected.loc[projected["zone_type"] == "A_core", "geometry"])
    return {
        "foot": footprints,
        "a": core.difference(footprints),
        "b": unary_union(projected.loc[projected["zone_type"] == "B_perimeter", "geometry"]),
        "c": unary_union(projected.loc[projected["zone_type"] == "C_control", "geometry"]),
    }


def write_clipped() -> None:
    """Heat images with transparent pixels outside each zone."""
    CLIPPED.mkdir(parents=True, exist_ok=True)
    facilities = gpd.read_file(FACILITIES)
    zones = gpd.read_file(ZONES)
    bounds = {}
    for year in YEARS:
        with rasterio.open(RAW / f"lst_{year}.tif") as src:
            data = src.read(1)
            cropland = [
                row["C2_cropland"]
                for row in zone_means(year, facilities, zones).values()
                if row.get("C2_cropland") is not None
            ]
            baseline = float(np.mean(cropland))
            footprints = unary_union(facilities.to_crs(src.crs).geometry)
            radiating(data, footprints, baseline, src.transform).save(CLIPPED / f"quincy_{year}_field.png")
            parts = zone_parts(facilities, zones, src.crs)
            for name, geom in parts.items():
                if geom.is_empty:
                    inside = np.zeros(data.shape, dtype=bool)
                else:
                    inside = ~geometry_mask([geom], out_shape=data.shape, transform=src.transform)
                colorize(data, inside, baseline).save(CLIPPED / f"quincy_{year}_{name}.png")
            print(f"  {year}: cropland baseline {baseline:.1f} °C")
            bounds[str(year)] = image_corners(src.transform, src.width, src.height, src.crs)
    BOUNDS.write_text(json.dumps(bounds, indent=2))
    print(f"Saved {BOUNDS.relative_to(ROOT)}")


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
    print("Clipped zone images")
    write_clipped()
    print("Chart numbers")
    write_metrics()


if __name__ == "__main__":
    main()
