#!/usr/bin/env python3
"""Fetch summer Landsat 8/9 medians over Quincy, WA.

For each year, keeps the clearest June 15–August 31 scenes that fully cover
the analysis box, masks cloud and shadow, and writes the median surface
temperature and NDVI. `--year all` also builds the status-report contact sheet.

Writes:
  data/raw/lst_{year}.tif
  data/raw/ndvi_{year}.tif
  data/raw/scenes_{year}.json
  data/overlays/quincy_{year}_preview.png
  data/overlays/yearly_contact_sheet.png   (--year all)
  data/overlays/yearly_scene_summary.txt   (--year all)

Usage:
  python pipeline/fetch_landsat.py --year 2024
  python pipeline/fetch_landsat.py --year all
"""

from __future__ import annotations

import argparse
import json
import os
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import planetary_computer as pc
import pystac_client
import rasterio
from pystac import Item
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import box, shape

import geopandas as gpd

FACILITIES = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
RAW_DIR = ROOT / "data" / "raw"
OVERLAY_DIR = ROOT / "data" / "overlays"

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
MAX_CLOUD_COVER = 10
MAX_SCENES = 4
YEARS = range(2015, 2027)

ANALYSIS_BBOX = (-119.97, 47.16, -119.74, 47.30)
OVERLAY_BBOX = (-119.90, 47.20, -119.80, 47.26)

ST_SCALE, ST_OFFSET = 0.00341802, 149.0
SR_SCALE, SR_OFFSET = 0.0000275, -0.2
KELVIN = 273.15
QA_CLOUD_MASK = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)


def find_scenes(catalog: pystac_client.Client, year: int) -> list[Item]:
    """Clearest summer scenes that fully cover the Quincy analysis box."""
    search = catalog.search(
        collections=[COLLECTION],
        bbox=ANALYSIS_BBOX,
        datetime=f"{year}-06-15/{year}-08-31",
        query={
            "eo:cloud_cover": {"lt": MAX_CLOUD_COVER},
            "platform": {"in": ["landsat-8", "landsat-9"]},
        },
    )
    aoi = box(*ANALYSIS_BBOX)
    items = [item for item in search.items() if shape(item.geometry).contains(aoi)]
    items.sort(key=lambda item: (item.properties["eo:cloud_cover"], str(item.datetime)))
    if not items:
        raise RuntimeError(f"No summer {year} scenes fully cover the Quincy analysis area.")
    chosen = items[:MAX_SCENES]
    print(f"{year}: {len(items)} clear scenes, using {len(chosen)}", flush=True)
    for item in chosen:
        print(
            f"  {item.id}  {item.datetime.date()}  "
            f"cloud {item.properties['eo:cloud_cover']:.2f}%",
            flush=True,
        )
    return chosen


def read_window(item: Item, asset: str):
    last_error = None
    for attempt in range(3):
        try:
            with rasterio.open(item.assets[asset].href) as src:
                bbox_crs = transform_bounds("EPSG:4326", src.crs, *ANALYSIS_BBOX)
                window = from_bounds(*bbox_crs, transform=src.transform)
                window = window.round_offsets().round_lengths()
                window = window.intersection(Window(0, 0, src.width, src.height))
                return src.read(1, window=window), src.window_transform(window), src.crs
        except Exception as exc:
            last_error = exc
            print(f"  retry {item.id} {asset} ({attempt + 1}/3): {exc}", flush=True)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Failed to read {asset} from {item.id}") from last_error


def on_grid(item: Item, asset: str, grid: dict, fill: int, nodata: int | None) -> np.ndarray:
    """Read one asset and snap it onto the reference grid."""
    raw, transform, crs = read_window(item, asset)
    dest = np.full(grid["shape"], fill, dtype=np.float32)
    reproject(
        source=raw,
        destination=dest,
        src_transform=transform,
        src_crs=crs,
        src_nodata=nodata,
        dst_transform=grid["transform"],
        dst_crs=grid["crs"],
        dst_nodata=fill,
        resampling=Resampling.nearest,
    )
    return dest


def reference_grid(item: Item) -> dict:
    with rasterio.open(item.assets["lwir11"].href) as src:
        crs = src.crs
        bbox_crs = transform_bounds("EPSG:4326", crs, *ANALYSIS_BBOX)
        window = from_bounds(*bbox_crs, transform=src.transform)
        window = window.round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        return {
            "shape": (int(window.height), int(window.width)),
            "transform": src.window_transform(window),
            "crs": crs,
        }


def scene_arrays(item: Item, grid: dict) -> tuple[np.ndarray, np.ndarray]:
    st = on_grid(item, "lwir11", grid, fill=0, nodata=0)
    red = on_grid(item, "red", grid, fill=0, nodata=0)
    nir = on_grid(item, "nir08", grid, fill=0, nodata=0)
    qa = on_grid(item, "qa_pixel", grid, fill=QA_CLOUD_MASK, nodata=None)

    clear = (qa.astype(np.uint16) & QA_CLOUD_MASK) == 0
    valid = clear & (st > 0) & (red > 0) & (nir > 0)

    lst = np.full(grid["shape"], np.nan, dtype=np.float32)
    lst[valid] = st[valid] * ST_SCALE + ST_OFFSET - KELVIN

    red_r = red * SR_SCALE + SR_OFFSET
    nir_r = nir * SR_SCALE + SR_OFFSET
    ndvi = np.full(grid["shape"], np.nan, dtype=np.float32)
    denom = nir_r + red_r
    ok = valid & (denom != 0)
    ndvi[ok] = (nir_r[ok] - red_r[ok]) / denom[ok]
    print(f"  valid pixels: {int(np.count_nonzero(valid)):,} / {valid.size:,}", flush=True)
    return lst, ndvi


def median_stack(arrays: list[np.ndarray]) -> np.ndarray:
    stack = np.stack(arrays, axis=0)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="All-NaN")
        return np.nanmedian(stack, axis=0).astype(np.float32)


def write_geotiff(path: Path, data: np.ndarray, transform, crs) -> None:
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": np.nan,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)


def plot_preview(path: Path, lst: np.ndarray, transform, crs, year: int, scene_ids: list[str]) -> None:
    campuses = gpd.read_file(FACILITIES).to_crs(crs)
    height, width = lst.shape
    left, top = transform.c, transform.f
    extent = (left, left + width * transform.a, top + height * transform.e, top)
    vmin, vmax = np.nanpercentile(lst, [2, 98])

    fig, ax = plt.subplots(figsize=(10, 8))
    image = ax.imshow(lst, cmap="inferno", extent=extent, vmin=vmin, vmax=vmax)
    campuses.boundary.plot(ax=ax, color="cyan", linewidth=1.2)
    for _, row in campuses.iterrows():
        center = row.geometry.centroid
        ax.annotate(row["id"], (center.x, center.y), color="cyan", fontsize=7, ha="center")
    fig.colorbar(image, ax=ax, label="Land Surface Temperature (°C)")
    ax.set_title(f"Quincy median LST {year} ({len(scene_ids)} scenes)")
    ax.set_xlabel(f"Easting (m, {crs.to_string()})")
    ax.set_ylabel("Northing (m)")
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


def campus_summary(lst: np.ndarray, transform, crs) -> None:
    campuses = gpd.read_file(FACILITIES).to_crs(crs)
    print("Mean LST inside each campus polygon:", flush=True)
    for _, row in campuses.iterrows():
        inside = ~geometry_mask([row.geometry], out_shape=lst.shape, transform=transform)
        print(f"  {row['id']:<16} {np.nanmean(lst[inside]):6.1f} °C", flush=True)


def fetch_year(catalog: pystac_client.Client, year: int, force: bool) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    marker = RAW_DIR / f"scenes_{year}.json"
    lst_path = RAW_DIR / f"lst_{year}.tif"
    if marker.exists() and lst_path.exists() and not force:
        print(f"{year}: already fetched, skipping (pass --force to redo)", flush=True)
        return json.loads(marker.read_text())

    items = find_scenes(catalog, year)
    grid = reference_grid(items[0])
    lst_layers = []
    ndvi_layers = []
    for item in items:
        print(f"  reading {item.id}", flush=True)
        lst, ndvi = scene_arrays(item, grid)
        lst_layers.append(lst)
        ndvi_layers.append(ndvi)

    lst = median_stack(lst_layers)
    ndvi = median_stack(ndvi_layers)
    write_geotiff(lst_path, lst, grid["transform"], grid["crs"])
    write_geotiff(RAW_DIR / f"ndvi_{year}.tif", ndvi, grid["transform"], grid["crs"])

    scene_ids = [item.id for item in items]
    record = {
        "year": year,
        "scene_ids": scene_ids,
        "dates": [item.datetime.date().isoformat() for item in items],
        "cloud_cover": [item.properties["eo:cloud_cover"] for item in items],
        "mean_lst_c": round(float(np.nanmean(lst)), 2),
        "max_lst_c": round(float(np.nanmax(lst)), 2),
        "mean_ndvi": round(float(np.nanmean(ndvi)), 3),
    }
    marker.write_text(json.dumps(record, indent=2))
    preview = OVERLAY_DIR / f"quincy_{year}_preview.png"
    plot_preview(preview, lst, grid["transform"], grid["crs"], year, scene_ids)
    print(
        f"{year}: mean {record['mean_lst_c']:.1f} °C, max {record['max_lst_c']:.1f} °C, "
        f"NDVI {record['mean_ndvi']:.2f}",
        flush=True,
    )
    if len(YEARS) == 1:
        campus_summary(lst, grid["transform"], grid["crs"])
    return record


def write_contact_sheet(records: list[dict]) -> None:
    fig, axes = plt.subplots(3, 4, figsize=(16, 11))
    by_year = {record["year"]: record for record in records}
    image = None
    for ax, year in zip(axes.ravel(), YEARS):
        path = RAW_DIR / f"lst_{year}.tif"
        record = by_year.get(year)
        if not path.exists():
            ax.set_title(f"{year}: missing")
            ax.axis("off")
            continue
        with rasterio.open(path) as src:
            data = src.read(1)
        image = ax.imshow(data, cmap="inferno", vmin=30, vmax=60)
        count = len(record["scene_ids"]) if record else "?"
        mean = record["mean_lst_c"] if record else float(np.nanmean(data))
        ax.set_title(f"{year}  n={count}  {mean:.1f}°C", fontsize=10)
        ax.axis("off")
    if image is not None:
        fig.colorbar(image, ax=axes.ravel().tolist(), label="Land Surface Temperature (°C)", shrink=0.85)
    fig.suptitle("Quincy summer median LST, 2015–2026", fontsize=14)
    out = OVERLAY_DIR / "yearly_contact_sheet.png"
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)

    lines = ["year  scenes  mean_c  max_c  scene_ids"]
    for year in YEARS:
        record = by_year.get(year)
        if record is None:
            lines.append(f"{year}  MISSING")
            continue
        ids = ", ".join(
            f"{scene_id} ({cloud:.1f}%, {date})"
            for scene_id, cloud, date in zip(record["scene_ids"], record["cloud_cover"], record["dates"])
        )
        lines.append(
            f"{record['year']}  {len(record['scene_ids'])}  {record['mean_lst_c']:.1f}  "
            f"{record['max_lst_c']:.1f}  {ids}"
        )
    summary = OVERLAY_DIR / "yearly_scene_summary.txt"
    summary.write_text("\n".join(lines) + "\n")
    print("\n".join(lines), flush=True)
    print(f"\nSaved {out.relative_to(ROOT)}", flush=True)
    print(f"Saved {summary.relative_to(ROOT)}", flush=True)


def load_records() -> list[dict]:
    records = []
    for year in YEARS:
        marker = RAW_DIR / f"scenes_{year}.json"
        if marker.exists():
            records.append(json.loads(marker.read_text()))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", default="2024", help="A year, or 'all' for 2015-2026")
    parser.add_argument("--force", action="store_true", help="Redo years that were already fetched")
    args = parser.parse_args()

    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", GDAL_HTTP_TIMEOUT="60"):
        if args.year == "all":
            catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)
            failures = []
            for year in YEARS:
                try:
                    fetch_year(catalog, year, args.force)
                except Exception as exc:
                    failures.append(f"{year}: {exc}")
                    print(f"{year} FAILED: {exc}", flush=True)
            write_contact_sheet(load_records())
            if failures:
                raise SystemExit("Missing years:\n" + "\n".join(failures))
            return

        year = int(args.year)
        catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)
        record = fetch_year(catalog, year, args.force)
        campus_path = RAW_DIR / f"lst_{year}.tif"
        with rasterio.open(campus_path) as src:
            campus_summary(src.read(1), src.transform, src.crs)
        print(json.dumps(record, indent=2), flush=True)


if __name__ == "__main__":
    main()
