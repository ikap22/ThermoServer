#!/usr/bin/env python3
"""Query Planetary Computer for low-cloud Landsat 8/9 scenes over Quincy, WA.

Connects anonymously (no subscription key) and prints each matching scene,
including whether the lwir11 thermal band is present.
"""

from __future__ import annotations

from datetime import datetime

import planetary_computer
import pystac_client
from pystac import Item

STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "landsat-c2-l2"
# west, south, east, north — Quincy, Washington
BBOX = [-119.92, 47.19, -119.80, 47.27]
START = "2024-07-01"
END = "2024-08-31"
MAX_CLOUD_COVER = 10
THERMAL_BAND = "lwir11"


def connect() -> pystac_client.Client:
    """Open the Planetary Computer STAC catalog without an API key."""
    print("Connecting to Planetary Computer STAC API...")
    print(f"  endpoint: {STAC_URL}")

    catalog = pystac_client.Client.open(
        STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )
    print(f"Connected successfully: {catalog.title or catalog.id}")
    return catalog


def search_quincy(catalog: pystac_client.Client) -> list[Item]:
    """Return summer 2024 Landsat scenes with eo:cloud_cover < 10."""
    print()
    print("Search")
    print(f"  collection:  {COLLECTION}")
    print(f"  bbox:        {BBOX}")
    print(f"  dates:       {START} to {END}")
    print(f"  cloud cover: eo:cloud_cover < {MAX_CLOUD_COVER}")

    search = catalog.search(
        collections=[COLLECTION],
        bbox=BBOX,
        datetime=f"{START}/{END}",
        query={"eo:cloud_cover": {"lt": MAX_CLOUD_COVER}},
    )
    items = list(search.items())
    items.sort(key=lambda item: item.datetime or datetime.min)
    return items


def format_assets(item: Item) -> str:
    """List asset keys, marking the thermal band."""
    names = []
    for name in sorted(item.assets):
        if name == THERMAL_BAND:
            names.append(f"{name} (thermal)")
        else:
            names.append(name)
    return ", ".join(names)


def print_scene(index: int, total: int, item: Item) -> None:
    acquired = item.datetime.strftime("%Y-%m-%d") if item.datetime else "unknown"
    cloud = item.properties.get("eo:cloud_cover")
    if isinstance(cloud, (int, float)):
        cloud_text = f"{cloud:.2f}%"
    else:
        cloud_text = "unknown"
    thermal = "available" if THERMAL_BAND in item.assets else "not present"

    print(f"[{index}/{total}] {item.id}")
    print(f"  acquisition date: {acquired}")
    print(f"  cloud cover:      {cloud_text}")
    print(f"  {THERMAL_BAND}:           {thermal}")
    print(f"  assets ({len(item.assets)}): {format_assets(item)}")


def main() -> None:
    catalog = connect()
    items = search_quincy(catalog)

    print()
    print(f"Total matching cloud-free scenes: {len(items)}")

    if not items:
        print("No scenes matched the filters.")
        return

    print()
    for index, item in enumerate(items, start=1):
        print_scene(index, len(items), item)
        if index != len(items):
            print()


if __name__ == "__main__":
    main()
