#!/usr/bin/env python3
"""Fetch a summer Landsat 8/9 scene over Quincy, WA and verify the thermal signature.

Writes, for the requested year:
  data/raw/lst_{year}.tif                 land surface temperature (deg C)
  data/raw/ndvi_{year}.tif                NDVI
  data/overlays/quincy_{year}_preview.png verification plot with campus outlines

Usage: python pipeline/fetch_landsat.py [--year 2024]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import planetary_computer as pc
import pystac_client
import rasterio
from pystac import Item
from rasterio.features import geometry_mask
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds
from shapely.geometry import box, shape

ROOT = Path(__file__).resolve().parent.parent
FACILITIES = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
RAW_DIR = ROOT / "data" / "raw"
OVERLAY_DIR = ROOT / "data" / "overlays"

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
MAX_CLOUD_COVER = 10

# west, south, east, north (WGS84). The analysis box covers the 5 km control
# ring around every campus; the overlay box matches the MapLibre image source.
ANALYSIS_BBOX = (-119.97, 47.16, -119.74, 47.30)
OVERLAY_BBOX = (-119.90, 47.20, -119.80, 47.26)

# Landsat Collection 2 Level-2 scale factors
ST_SCALE, ST_OFFSET = 0.00341802, 149.0
SR_SCALE, SR_OFFSET = 0.0000275, -0.2
KELVIN = 273.15

# QA_PIXEL bits: 1 dilated cloud, 2 cirrus, 3 cloud, 4 cloud shadow
QA_CLOUD_MASK = (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4)


def find_scene(year: int) -> Item:
    """Return the clearest summer Landsat 8/9 scene that fully covers ANALYSIS_BBOX."""
    catalog = pystac_client.Client.open(STAC_URL, modifier=pc.sign_inplace)
    search = catalog.search(
        collections=[COLLECTION],
        bbox=ANALYSIS_BBOX,
        datetime=f"{year}-07-01/{year}-08-31",
        query={
            "eo:cloud_cover": {"lt": MAX_CLOUD_COVER},
            "platform": {"in": ["landsat-8", "landsat-9"]},
        },
    )
    aoi = box(*ANALYSIS_BBOX)
    items = [item for item in search.items() if shape(item.geometry).contains(aoi)]
    if not items:
        raise SystemExit(f"No summer {year} scenes fully cover the Quincy analysis area.")

    items.sort(key=lambda item: item.properties["eo:cloud_cover"])
    print(f"{len(items)} candidate scenes for summer {year}; using the clearest.")
    return items[0]


def read_window(item: Item, asset: str, bbox_crs: tuple[float, float, float, float]):
    """Read one asset clipped to bbox_crs (in the raster's CRS)."""
    with rasterio.open(item.assets[asset].href) as src:
        window = from_bounds(*bbox_crs, transform=src.transform)
        window = window.round_offsets().round_lengths()
        window = window.intersection(Window(0, 0, src.width, src.height))
        return src.read(1, window=window), src.window_transform(window), src.crs


def compute_lst_ndvi(item: Item):
    with rasterio.open(item.assets["lwir11"].href) as src:
        crs = src.crs
    bbox_crs = transform_bounds("EPSG:4326", crs, *ANALYSIS_BBOX)

    st, transform, _ = read_window(item, "lwir11", bbox_crs)
    red, _, _ = read_window(item, "red", bbox_crs)
    nir, _, _ = read_window(item, "nir08", bbox_crs)
    qa, _, _ = read_window(item, "qa_pixel", bbox_crs)

    clear = (qa & QA_CLOUD_MASK) == 0
    valid = clear & (st > 0) & (red > 0) & (nir > 0)

    lst = np.full(st.shape, np.nan, dtype=np.float32)
    lst[valid] = st[valid] * ST_SCALE + ST_OFFSET - KELVIN

    red_r = red.astype(np.float32) * SR_SCALE + SR_OFFSET
    nir_r = nir.astype(np.float32) * SR_SCALE + SR_OFFSET
    ndvi = np.full(st.shape, np.nan, dtype=np.float32)
    denom = nir_r + red_r
    ok = valid & (denom != 0)
    ndvi[ok] = (nir_r[ok] - red_r[ok]) / denom[ok]

    print(f"Masked {np.count_nonzero(~valid)} of {valid.size} pixels (cloud, shadow, or nodata).")
    return lst, ndvi, transform, crs


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


def plot_preview(path: Path, lst: np.ndarray, transform, crs, item: Item, year: int) -> None:
    campuses = gpd.read_file(FACILITIES).to_crs(crs)
    h, w = lst.shape
    left, top = transform.c, transform.f
    extent = (left, left + w * transform.a, top + h * transform.e, top)

    ov_left, ov_bottom, ov_right, ov_top = transform_bounds("EPSG:4326", crs, *OVERLAY_BBOX)

    vmin, vmax = np.nanpercentile(lst, [2, 98])
    fig, ax = plt.subplots(figsize=(10, 8))
    im = ax.imshow(lst, cmap="inferno", extent=extent, vmin=vmin, vmax=vmax)
    campuses.boundary.plot(ax=ax, color="cyan", linewidth=1.2)
    for _, row in campuses.iterrows():
        c = row.geometry.centroid
        ax.annotate(row["id"], (c.x, c.y), color="cyan", fontsize=7, ha="center",
                    xytext=(0, 14), textcoords="offset points")
    ax.add_patch(plt.Rectangle((ov_left, ov_bottom), ov_right - ov_left, ov_top - ov_bottom,
                               fill=False, edgecolor="white", linestyle="--", linewidth=0.8))
    fig.colorbar(im, ax=ax, label="Land Surface Temperature (°C)")
    ax.set_title(f"Quincy, WA Thermal Footprint - {item.datetime.date()} ({item.id})")
    ax.set_xlabel(f"Easting (m, {crs.to_string()})")
    ax.set_ylabel("Northing (m)")
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def campus_summary(lst: np.ndarray, transform, crs) -> None:
    campuses = gpd.read_file(FACILITIES).to_crs(crs)
    print("\nMean LST inside each campus polygon:")
    for _, row in campuses.iterrows():
        inside = ~geometry_mask([row.geometry], out_shape=lst.shape, transform=transform)
        print(f"  {row['id']:<16} {np.nanmean(lst[inside]):6.1f} °C")


def fetch_year(year: int) -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    OVERLAY_DIR.mkdir(parents=True, exist_ok=True)

    item = find_scene(year)
    print(f"Scene: {item.id}  acquired {item.datetime.date()}  "
          f"cloud cover {item.properties['eo:cloud_cover']:.2f}%")

    lst, ndvi, transform, crs = compute_lst_ndvi(item)

    write_geotiff(RAW_DIR / f"lst_{year}.tif", lst, transform, crs)
    write_geotiff(RAW_DIR / f"ndvi_{year}.tif", ndvi, transform, crs)

    print(f"\nRaster CRS: {crs.to_string()}  shape: {lst.shape}")
    print(f"Mean Quincy summer LST: {np.nanmean(lst):.1f} °C")
    print(f"Max ground LST:         {np.nanmax(lst):.1f} °C")
    print(f"Mean NDVI:              {np.nanmean(ndvi):.2f}")
    campus_summary(lst, transform, crs)

    preview = OVERLAY_DIR / f"quincy_{year}_preview.png"
    plot_preview(preview, lst, transform, crs, item, year)
    print(f"\nSaved {preview.relative_to(ROOT)}")
    print(f"Saved data/raw/lst_{year}.tif and data/raw/ndvi_{year}.tif")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--year", type=int, default=2024)
    args = parser.parse_args()
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"):
        fetch_year(args.year)


if __name__ == "__main__":
    main()
