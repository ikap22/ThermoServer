#!/usr/bin/env python3
"""Professor-review figures from the Quincy zone temperatures and the 2024 scene.

Writes:
  data/overlays/standard_deviation_and_grouped_barchart.png
  data/overlays/buffer_rings_before_after.png

The bar chart uses the zone means already stored in frontend/metrics.json.
The standard-deviation classes are computed from the 2024 summer median.
The reference distribution is the valley floor south of 47.26 N, so the bare
hills north of Quincy are not treated as the natural background.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / ".mplconfig"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import geopandas as gpd
import numpy as np
import rasterio
from matplotlib.colors import ListedColormap
from matplotlib.patches import Patch
from rasterio.features import geometry_mask
from shapely.ops import unary_union

METRICS = ROOT / "frontend" / "metrics.json"
FACILITIES = ROOT / "data" / "geojson" / "facilities_quincy.geojson"
ZONES = ROOT / "data" / "geojson" / "zones_quincy.geojson"
RAW = ROOT / "data" / "raw"
OVERLAYS = ROOT / "data" / "overlays"
UTM = "EPSG:32611"
VALLEY_NORTH = 47.26
NDVI_RANGELAND = 0.30
NDVI_CROPLAND = 0.50

CAMPUS_ORDER = ("msft-columbia", "vantage-quincy", "sabey-sdc", "h5-quincy")
CAMPUS_LABELS = {
    "msft-columbia": "Microsoft",
    "vantage-quincy": "Vantage",
    "sabey-sdc": "Sabey",
    "h5-quincy": "H5",
}
CAMPUS_COLORS = {
    "msft-columbia": "#4C78A8",
    "vantage-quincy": "#F58518",
    "sabey-sdc": "#E45756",
    "h5-quincy": "#54A24B",
}
CHART_YEARS = (2016, 2020, 2024)
# z-score breaks. Class 5 is the hot tail used as the plume screen.
Z_EDGES = (-np.inf, -1.5, -0.5, 0.5, 1.5, np.inf)
Z_COLORS = ("#2166AC", "#92C5DE", "#F6E8C3", "#EF8A62", "#B2182B")
Z_LABELS = (
    "< −1.5 SD  cooler than the valley",
    "−1.5 to −0.5 SD",
    "−0.5 to +0.5 SD  near the valley mean",
    "+0.5 to +1.5 SD",
    "> +1.5 SD  hot tail",
)


def load_metrics() -> dict:
    return json.loads(METRICS.read_text())


def series_value(metrics: dict, facility_id: str, key: str, year: int) -> float:
    years = metrics["years"]
    values = metrics["facilities"][facility_id]["series"][key]
    return float(values[years.index(year)])


def baseline(metrics: dict, key: str, year: int) -> float:
    values = [series_value(metrics, facility_id, key, year) for facility_id in CAMPUS_ORDER]
    return float(np.mean(values))


def plot_bars_and_spectrum(metrics: dict) -> None:
    with rasterio.open(RAW / "lst_2024.tif") as src:
        lst = src.read(1).astype(np.float32)
        transform, crs = src.transform, src.crs
    valley = unary_union(
        gpd.read_file(ZONES).to_crs(crs).geometry
    ).envelope  # placeholder replaced below
    del valley
    # Valley floor only: south of the escarpment. Hills run hotter than the campuses.
    height, width = lst.shape
    rows, cols = np.indices(lst.shape)
    xs, ys = rasterio.transform.xy(transform, rows, cols, offset="center")
    lons, lats = rasterio.warp.transform(crs, "EPSG:4326", np.ravel(xs), np.ravel(ys))
    lat = np.reshape(lats, lst.shape)
    valid = np.isfinite(lst)
    reference = valid & (lat < VALLEY_NORTH)
    ref_pixels = lst[reference]
    mean = float(np.mean(ref_pixels))
    std = float(np.std(ref_pixels, ddof=1))
    z = np.full(lst.shape, np.nan, dtype=np.float32)
    z[valid] = (lst[valid] - mean) / std
    classes = np.digitize(z, (-1.5, -0.5, 0.5, 1.5))  # 0..4, nan stays out via mask

    print(f"2024 valley reference: n={reference.sum():,}  mean={mean:.2f} °C  sd={std:.2f} °C")
    for edge, label in zip((-1.5, -0.5, 0.5, 1.5), ("-1.5", "-0.5", "+0.5", "+1.5")):
        print(f"  {label} SD = {mean + edge * std:.1f} °C")

    zones = gpd.read_file(ZONES).to_crs(crs)
    facilities = gpd.read_file(FACILITIES).to_crs(crs)
    core = unary_union(zones.loc[zones["zone_type"] == "A_core", "geometry"])
    control = unary_union(zones.loc[zones["zone_type"] == "C_control", "geometry"])
    core_mask = ~geometry_mask([core], out_shape=lst.shape, transform=transform)
    control_mask = ~geometry_mask([control], out_shape=lst.shape, transform=transform)
    for name, mask in (("valley", reference), ("campus cores", core_mask & valid), ("control ring", control_mask & valid)):
        counts = np.bincount(classes[mask], minlength=5)
        share = counts / counts.sum() * 100
        print(f"  {name}: " + "  ".join(f"c{i + 1} {share[i]:.1f}%" for i in range(5)))

    fig, axes = plt.subplots(1, 2, figsize=(14.5, 7.2), gridspec_kw={"width_ratios": (1.15, 1)})
    ax = axes[0]
    x = np.arange(len(CHART_YEARS))
    bar_width = 0.13
    series = [(facility_id, CAMPUS_LABELS[facility_id], CAMPUS_COLORS[facility_id], "A_core") for facility_id in CAMPUS_ORDER]
    series.append(("c2", "Irrigated cropland", "#1B9E77", "C2_cropland"))
    series.append(("c1", "Dry rangeland", "#8C6D46", "C1_rangeland"))
    for index, (key, label, color, metric) in enumerate(series):
        if metric in ("C2_cropland", "C1_rangeland"):
            heights = [baseline(metrics, metric, year) for year in CHART_YEARS]
        else:
            heights = [series_value(metrics, key, metric, year) for year in CHART_YEARS]
        offset = (index - 2.5) * bar_width
        bars = ax.bar(x + offset, heights, width=bar_width, color=color, label=label, edgecolor="white", linewidth=0.4)
        for bar, value in zip(bars, heights):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.25, f"{value:.0f}", ha="center", va="bottom", fontsize=7, color="#333333")
        print(label, " ".join(f"{year}:{height:.1f}" for year, height in zip(CHART_YEARS, heights)))

    ax.set_xticks(x)
    ax.set_xticklabels([str(year) for year in CHART_YEARS])
    ax.set_ylim(28, 54)
    ax.set_ylabel("Summer mean surface temperature (°C)")
    ax.set_xlabel("Year")
    ax.set_title("Campus cores against the two baselines")
    ax.legend(frameon=False, ncol=2, fontsize=8, loc="upper left")
    ax.axhline(0, color="none")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.text(
        0.0,
        -0.18,
        "Axis starts at 28°C. Cropland and rangeland are the mean of the four campuses’ 3–5 km rings.",
        transform=ax.transAxes,
        fontsize=8,
        color="#444444",
    )

    map_ax = axes[1]
    left, top = transform.c, transform.f
    extent = (left, left + width * transform.a, top + height * transform.e, top)
    cmap = ListedColormap(Z_COLORS)
    cmap.set_bad("#FFFFFF")
    shown = np.ma.array(classes, mask=~valid)
    map_ax.imshow(shown, cmap=cmap, vmin=-0.5, vmax=4.5, extent=extent, interpolation="nearest", zorder=1)
    facilities.boundary.plot(ax=map_ax, color="black", linewidth=1.1, zorder=3)
    bounds = facilities.total_bounds
    pad = 2500
    map_ax.set_xlim(bounds[0] - pad, bounds[2] + pad)
    map_ax.set_ylim(bounds[1] - pad, bounds[3] + pad)
    map_ax.set_xticks([])
    map_ax.set_yticks([])
    map_ax.set_title("2024 pixels, in standard deviations of the valley")
    cuts = [mean + edge * std for edge in (-1.5, -0.5, 0.5, 1.5)]
    legend = [
        Patch(facecolor=color, edgecolor="#666666", label=f"{label}   ({bound})")
        for color, label, bound in zip(
            Z_COLORS,
            ("< −1.5 SD", "−1.5 to −0.5", "−0.5 to +0.5", "+0.5 to +1.5", "> +1.5 SD"),
            (f"< {cuts[0]:.0f}°C", f"{cuts[0]:.0f}–{cuts[1]:.0f}°C", f"{cuts[1]:.0f}–{cuts[2]:.0f}°C", f"{cuts[2]:.0f}–{cuts[3]:.0f}°C", f"> {cuts[3]:.0f}°C"),
        )
    ]
    map_ax.legend(handles=legend, loc="lower center", bbox_to_anchor=(0.5, -0.28), frameon=False, fontsize=8, ncol=1)
    map_ax.text(
        0.0,
        -0.34,
        f"Valley floor south of 47.26°N: mean {mean:.1f}°C, SD {std:.1f}°C. Black outlines are the campuses.",
        transform=map_ax.transAxes,
        fontsize=8,
        color="#444444",
    )

    fig.suptitle("Quincy summer temperature: campus means and a 5-class standard-deviation screen", fontsize=13)
    fig.subplots_adjust(left=0.07, right=0.98, top=0.88, bottom=0.22, wspace=0.18)
    out = OVERLAYS / "standard_deviation_and_grouped_barchart.png"
    fig.savefig(out, dpi=160)
    plt.close(fig)
    print(f"Saved {out.relative_to(ROOT)}")


def plot_before_after() -> None:
    facilities = gpd.read_file(FACILITIES)
    zones = gpd.read_file(ZONES)
    campus_id = "msft-columbia"
    with rasterio.open(RAW / "lst_2024.tif") as lst_src, rasterio.open(RAW / "ndvi_2024.tif") as ndvi_src:
        lst = lst_src.read(1)
        ndvi = ndvi_src.read(1)
        transform, crs = lst_src.transform, lst_src.crs
    facilities_utm = facilities.to_crs(crs)
    zones_utm = zones.to_crs(crs)
    footprint = facilities_utm.loc[facilities_utm["id"] == campus_id].geometry.iloc[0]
    campus_zones = zones_utm[zones_utm["facility_id"] == campus_id]
    single = footprint.buffer(5000)

    height, width = lst.shape
    left, top = transform.c, transform.f
    extent = (left, left + width * transform.a, top + height * transform.e, top)
    control = campus_zones.loc[campus_zones["zone_type"] == "C_control"]
    core = campus_zones.loc[campus_zones["zone_type"] == "A_core"]
    plume = campus_zones.loc[campus_zones["zone_type"] == "B_perimeter"]
    inside_c = ~geometry_mask(list(control.geometry), out_shape=lst.shape, transform=transform)
    split = np.zeros(lst.shape + (4,), dtype=np.float32)
    rangeland = inside_c & np.isfinite(ndvi) & (ndvi < NDVI_RANGELAND)
    cropland = inside_c & np.isfinite(ndvi) & (ndvi >= NDVI_CROPLAND)
    mixed = inside_c & np.isfinite(ndvi) & (ndvi >= NDVI_RANGELAND) & (ndvi < NDVI_CROPLAND)
    split[rangeland] = (0.55, 0.43, 0.27, 0.75)
    split[cropland] = (0.11, 0.62, 0.47, 0.75)
    split[mixed] = (0.75, 0.75, 0.75, 0.45)

    minx, miny, maxx, maxy = control.total_bounds
    pad = 400

    fig, axes = plt.subplots(1, 2, figsize=(13.2, 7.0))
    for ax in axes:
        ax.imshow(lst, cmap="inferno", extent=extent, vmin=30, vmax=60, alpha=0.28)
        ax.set_xlim(minx - pad, maxx + pad)
        ax.set_ylim(miny - pad, maxy + pad)
        ax.set_xticks([])
        ax.set_yticks([])

    gpd.GeoSeries([single], crs=crs).plot(ax=axes[0], color="#9E9E9E", alpha=0.45, edgecolor="#333333", linewidth=1.0)
    gpd.GeoSeries([footprint], crs=crs).boundary.plot(ax=axes[0], color="black", linewidth=1.4)
    axes[0].set_title("Before: one ring, 0–5 km")
    axes[0].text(
        0.5,
        -0.08,
        "The building and the far fields are in the same polygon,\nso a hot roof and cool crops are averaged together.",
        transform=axes[0].transAxes,
        ha="center",
        fontsize=9,
        color="#333333",
    )

    core.plot(ax=axes[1], color="#E4572E", alpha=0.55)
    plume.plot(ax=axes[1], color="#F2C14E", alpha=0.50)
    axes[1].imshow(split, extent=extent, interpolation="nearest")
    core.boundary.plot(ax=axes[1], color="#9C2F14", linewidth=0.8)
    plume.boundary.plot(ax=axes[1], color="#8A6A10", linewidth=0.8)
    control.boundary.plot(ax=axes[1], color="#1F4E79", linewidth=1.0)
    gpd.GeoSeries([footprint], crs=crs).boundary.plot(ax=axes[1], color="black", linewidth=1.4)
    axes[1].set_title("After: core, plume, and two baselines")
    axes[1].legend(
        handles=[
            Patch(facecolor="#E4572E", alpha=0.7, label="Core, 0–250 m"),
            Patch(facecolor="#F2C14E", alpha=0.7, label="Plume, 250 m–1 km"),
            Patch(facecolor="#1B9E77", alpha=0.75, label="C2 irrigated cropland, NDVI ≥ 0.50"),
            Patch(facecolor="#8C6D46", alpha=0.75, label="C1 dry rangeland, NDVI < 0.30"),
            Patch(facecolor="#BFBFBF", alpha=0.6, label="Mixed NDVI, not used as a baseline"),
        ],
        loc="lower center",
        bbox_to_anchor=(0.5, -0.28),
        frameon=False,
        fontsize=8,
    )

    fig.suptitle("Microsoft Columbia Center: from one buffer to separate measurement rings", fontsize=13)
    fig.text(
        0.5,
        0.02,
        "The same distances are used at Vantage, Sabey, and H5. Pale background is the 2024 temperature scene.",
        ha="center",
        fontsize=9,
        color="#333333",
    )
    fig.subplots_adjust(left=0.03, right=0.98, top=0.88, bottom=0.18, wspace=0.08)
    out = OVERLAYS / "buffer_rings_before_after.png"
    fig.savefig(out, dpi=160)
    plt.close(fig)
    print(f"Saved {out.relative_to(ROOT)}")
    print(f"  C1 pixels {int(rangeland.sum()):,}   C2 pixels {int(cropland.sum()):,}   mixed {int(mixed.sum()):,}")


def main() -> None:
    plot_bars_and_spectrum(load_metrics())
    plot_before_after()


if __name__ == "__main__":
    main()
