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


# Degrees above that summer's irrigated cropland. Fixed cuts so the legend
# means the same thing in every year.
CLASS_EDGES = (0.0, 4.0, 8.0, 12.0)
CLASS_RGBA = np.array(
    [
        [33, 102, 172, 235],
        [146, 197, 222, 235],
        [254, 224, 139, 235],
        [244, 109, 67, 235],
        [165, 0, 38, 235],
    ],
    dtype=np.uint8,
)
VALLEY_NORTH = 47.26
BLOCK = 4


def class_image(data: np.ndarray, baseline: float, inside: np.ndarray) -> Image.Image:
    """Five distinct colors, from cooler than the fields to much hotter."""
    anomaly = data - baseline
    classes = np.digitize(anomaly, CLASS_EDGES)
    rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
    visible = inside & np.isfinite(data)
    for index, color in enumerate(CLASS_RGBA):
        rgba[(classes == index) & visible] = color
    return Image.fromarray(rgba, mode="RGBA")


def colorize(data: np.ndarray, inside: np.ndarray, baseline: float) -> Image.Image:
    return class_image(data, baseline, inside)


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
            lat = latitudes(src.transform, src.crs, data.shape)
            valley = np.isfinite(data) & (lat < VALLEY_NORTH)
            class_image(data, baseline, valley).save(CLIPPED / f"quincy_{year}_field.png")
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


def latitudes(transform, crs, shape) -> np.ndarray:
    rows, cols = np.indices(shape)
    xs, ys = rasterio.transform.xy(transform, rows, cols, offset="center")
    _, lats = warp_transform(crs, "EPSG:4326", np.ravel(xs), np.ravel(ys))
    return np.reshape(lats, shape)


def cropland_baseline(year: int) -> float:
    metrics = json.loads(METRICS.read_text())
    index = metrics["years"].index(year)
    values = [
        facility["series"]["C2_cropland"][index]
        for facility in metrics["facilities"].values()
    ]
    return float(np.mean(values))


def block_mean(values: np.ndarray, valid: np.ndarray, factor: int):
    height, width = values.shape
    height2, width2 = height - height % factor, width - width % factor
    blocked = values[:height2, :width2].reshape(height2 // factor, factor, width2 // factor, factor)
    mask = valid[:height2, :width2].reshape(height2 // factor, factor, width2 // factor, factor)
    count = mask.sum(axis=(1, 3))
    total = np.where(mask, blocked, 0).sum(axis=(1, 3))
    coarse = np.divide(total, np.maximum(count, 1))
    return coarse, count >= (factor * factor) // 2


def rook_sum(values: np.ndarray, valid: np.ndarray):
    filled = np.where(valid, values, 0.0)
    total = np.zeros(values.shape, dtype=np.float64)
    count = np.zeros(values.shape, dtype=np.float64)
    total[:-1, :] += filled[1:, :]
    count[:-1, :] += valid[1:, :]
    total[1:, :] += filled[:-1, :]
    count[1:, :] += valid[:-1, :]
    total[:, :-1] += filled[:, 1:]
    count[:, :-1] += valid[:, 1:]
    total[:, 1:] += filled[:, :-1]
    count[:, 1:] += valid[:, :-1]
    return total, count


def morans_i(values: np.ndarray, valid: np.ndarray) -> float:
    if int(valid.sum()) < 10:
        return 0.0
    centered = np.where(valid, values - values[valid].mean(), 0.0)
    total, count = rook_sum(centered, valid)
    weight = float(count[valid].sum())
    variance = float(np.sum(centered[valid] ** 2))
    if weight == 0 or variance == 0:
        return 0.0
    return float(valid.sum() / weight * np.sum(centered[valid] * total[valid]) / variance)


def cluster_labels(values: np.ndarray, valid: np.ndarray, baseline: float):
    """Like-with-like patches at the two ends of the temperature classes."""
    anomaly = np.where(valid, values - baseline, 0.0)
    total, count = rook_sum(anomaly, valid)
    lag = np.divide(total, count, out=np.zeros_like(total), where=count >= 3)
    hot = valid & (anomaly >= 12.0) & (lag >= 12.0)
    cool = valid & (anomaly <= -2.0) & (lag <= -2.0)
    return hot, cool


def moran_test(values: np.ndarray, valid: np.ndarray, observed: float) -> float:
    rng = np.random.default_rng(21)
    flat = values[valid].copy()
    as_high = 0
    for _ in range(199):
        rng.shuffle(flat)
        trial = np.zeros_like(values)
        trial[valid] = flat
        if morans_i(trial, valid) >= observed:
            as_high += 1
    return (1 + as_high) / 200


def write_classes() -> None:
    """Classified valley map, plus Moran's I and the hot/cool cluster overlay."""
    CLIPPED.mkdir(parents=True, exist_ok=True)
    facilities = gpd.read_file(FACILITIES)
    zones = gpd.read_file(ZONES)
    summary = {}
    for year in YEARS:
        with rasterio.open(RAW / f"lst_{year}.tif") as src:
            data = src.read(1).astype(np.float32)
            transform, crs = src.transform, src.crs
        baseline = cropland_baseline(year)
        lat = latitudes(transform, crs, data.shape)
        valley = np.isfinite(data) & (lat < VALLEY_NORTH)
        class_image(data, baseline, valley).save(CLIPPED / f"quincy_{year}_field.png")
        for name, geom in zone_parts(facilities, zones, crs).items():
            if geom.is_empty:
                inside = np.zeros(data.shape, dtype=bool)
            else:
                inside = ~geometry_mask([geom], out_shape=data.shape, transform=transform)
            class_image(data, baseline, inside & valley).save(CLIPPED / f"quincy_{year}_{name}.png")

        coarse, coarse_valid = block_mean(data, valley, BLOCK)
        observed = morans_i(coarse, coarse_valid)
        p_value = moran_test(coarse, coarse_valid, observed)
        hot, cool = cluster_labels(coarse, coarse_valid, baseline)
        hot_full = np.zeros(data.shape, dtype=bool)
        cool_full = np.zeros(data.shape, dtype=bool)
        hot_paint = np.repeat(np.repeat(hot, BLOCK, axis=0), BLOCK, axis=1)
        cool_paint = np.repeat(np.repeat(cool, BLOCK, axis=0), BLOCK, axis=1)
        hot_full[: hot_paint.shape[0], : hot_paint.shape[1]] = hot_paint
        cool_full[: cool_paint.shape[0], : cool_paint.shape[1]] = cool_paint
        hot_full &= valley
        cool_full &= valley
        rgba = np.zeros((*data.shape, 4), dtype=np.uint8)
        rgba[cool_full] = (33, 102, 172, 210)
        rgba[hot_full] = (165, 0, 38, 210)
        Image.fromarray(rgba, mode="RGBA").save(CLIPPED / f"quincy_{year}_cluster.png")

        n_valley = int(valley.sum())
        summary[str(year)] = {
            "morans_i": round(observed, 2),
            "p_value": round(p_value, 3),
            "n_blocks": int(coarse_valid.sum()),
            "block_m": int(round(abs(transform.a) * BLOCK)),
            "hot_cluster_pct": round(float(hot_full.sum()) / n_valley * 100, 1),
            "cool_cluster_pct": round(float(cool_full.sum()) / n_valley * 100, 1),
            "baseline_c": round(baseline, 1),
        }
        print(
            f"  {year}: I={observed:.2f} p={p_value:.3f}  "
            f"hot {summary[str(year)]['hot_cluster_pct']}%  cool {summary[str(year)]['cool_cluster_pct']}%"
        )
    path = ROOT / "frontend" / "spatial.json"
    path.write_text(json.dumps(summary, indent=2))
    print(f"Saved {path.relative_to(ROOT)}")


def main() -> None:
    OVERLAYS.mkdir(parents=True, exist_ok=True)
    print("Clipped zone images")
    write_clipped()
    print("Chart numbers")
    write_metrics()


if __name__ == "__main__":
    main()
