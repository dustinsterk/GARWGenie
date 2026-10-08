"""
Aerial imagery basemap.

Slippy-map tiles (the XYZ scheme everything from OSM to Esri uses) placed under
the racing line in the local ENU frame.

Two projection notes that matter:

* Tiles are Web Mercator; the track is a local equirectangular projection.
  Over a circuit the difference is a fraction of a percent, but rather than
  wave it away, each tile is positioned from *its own* lat/lon bounds. That
  confines the mismatch to within a single tile — under 0.1 m at zoom 18 —
  instead of letting it accumulate across the mosaic.
* Tile rows run north-to-south while ENU y runs south-to-north, so tile images
  are flipped vertically before display.
"""

from __future__ import annotations

import hashlib
import math
import os
import urllib.request
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Tuple

TILE_PX = 256
#: meters per pixel at zoom 0 on the equator
EQUATOR_RES = 156543.033928

CACHE_DIR = os.path.join(
    os.path.expanduser("~/.garw_genie"), "cache", "tiles")

USER_AGENT = "GARW-Genie-LapAnalysis/1.0 (lap analysis)"


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    url: str                    # format string with {z} {x} {y}
    attribution: str
    max_zoom: int = 19


PROVIDERS = {
    "esri_imagery": Provider(
        key="esri_imagery",
        label="Esri satellite",
        url=("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Imagery/MapServer/tile/{z}/{y}/{x}"),
        attribution=("Esri, Maxar, Earthstar Geographics, "
                     "and the GIS User Community"),
        max_zoom=19),
    "esri_topo": Provider(
        key="esri_topo",
        label="Esri topographic",
        url=("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Topo_Map/MapServer/tile/{z}/{y}/{x}"),
        attribution="Esri and the GIS User Community",
        max_zoom=19),
    "esri_street": Provider(
        key="esri_street",
        label="Esri streets",
        url=("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Street_Map/MapServer/tile/{z}/{y}/{x}"),
        attribution="Esri and the GIS User Community",
        max_zoom=19),
}

DEFAULT_PROVIDER = "esri_imagery"


# --------------------------------------------------------------------------
# Tile maths
# --------------------------------------------------------------------------


def deg2tile(lat: float, lon: float, z: int) -> Tuple[float, float]:
    """Fractional tile coordinates for a lat/lon at zoom z."""
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    lat = max(min(lat, 85.05112878), -85.05112878)
    r = math.radians(lat)
    y = (1.0 - math.log(math.tan(r) + 1.0 / math.cos(r)) / math.pi) / 2.0 * n
    return x, y


def tile2deg(x: float, y: float, z: int) -> Tuple[float, float]:
    """Lat/lon of a fractional tile coordinate (the tile's NW corner)."""
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def tile_bounds(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    """(north, west, south, east) in degrees for tile x/y/z."""
    north, west = tile2deg(x, y, z)
    south, east = tile2deg(x + 1, y + 1, z)
    return north, west, south, east


def resolution(lat: float, z: int) -> float:
    """Ground meters per pixel."""
    return EQUATOR_RES * math.cos(math.radians(lat)) / (2.0 ** z)


def pick_zoom(lat: float, extent_m: float, target_px: float = 1600.0,
              max_zoom: int = 19) -> int:
    """Zoom giving roughly `target_px` across the track.

    Too low and the imagery is mush; too high and you fetch hundreds of tiles
    for no visible gain, which is rude to the tile server and slow for you.
    """
    if extent_m <= 0:
        return 16
    want = extent_m / target_px                 # meters per pixel
    z = math.log2(EQUATOR_RES * math.cos(math.radians(lat)) / want)
    return int(max(1, min(max_zoom, round(z))))


def tiles_for_bbox(lat_min: float, lon_min: float,
                   lat_max: float, lon_max: float, z: int,
                   margin: int = 1, limit: int = 140
                   ) -> List[Tuple[int, int, int]]:
    """Tile list covering a bounding box, with a margin of extra tiles."""
    x0, y0 = deg2tile(lat_max, lon_min, z)      # NW
    x1, y1 = deg2tile(lat_min, lon_max, z)      # SE
    xa, xb = int(math.floor(x0)) - margin, int(math.floor(x1)) + margin
    ya, yb = int(math.floor(y0)) - margin, int(math.floor(y1)) + margin
    n = 2 ** z
    out: List[Tuple[int, int, int]] = []
    for tx in range(xa, xb + 1):
        for ty in range(ya, yb + 1):
            if not (0 <= ty < n):
                continue
            out.append((z, tx % n, ty))
            if len(out) >= limit:
                return out
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def tile_url(provider: Provider, z: int, x: int, y: int) -> str:
    return provider.url.format(z=z, x=x, y=y)


def cache_path(provider: Provider, z: int, x: int, y: int) -> str:
    stem = hashlib.sha1(provider.url.encode()).hexdigest()[:10]
    return os.path.join(CACHE_DIR, stem, str(z), str(x), f"{y}.img")


def load_cached(provider: Provider, z: int, x: int, y: int) -> Optional[bytes]:
    path = cache_path(provider, z, x, y)
    try:
        if os.path.getsize(path) > 0:
            with open(path, "rb") as fh:
                return fh.read()
    except OSError:
        return None
    return None


def store_cached(provider: Provider, z: int, x: int, y: int,
                 data: bytes) -> None:
    path = cache_path(provider, z, x, y)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        pass                      # a cache that can't be written is not fatal


def fetch_tile(provider: Provider, z: int, x: int, y: int,
               timeout: float = 8.0) -> Optional[bytes]:
    """Return tile bytes, from cache if possible. None on any failure."""
    cached = load_cached(provider, z, x, y)
    if cached is not None:
        return cached
    req = urllib.request.Request(tile_url(provider, z, x, y),
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
    except Exception:                            # noqa: BLE001
        return None
    if not data:
        return None
    store_cached(provider, z, x, y, data)
    return data


# --------------------------------------------------------------------------
# Placement in the local frame
# --------------------------------------------------------------------------


def tile_rect_local(z: int, x: int, y: int, project) -> Tuple[float, float,
                                                              float, float]:
    """Tile bounds as (x_left, y_bottom, width, height) in local ENU meters.

    `project` takes (lat, lon) and returns (x, y) — pass the same projection
    the track uses so the imagery lands under the racing line rather than
    beside it.
    """
    north, west, south, east = tile_bounds(x, y, z)
    x_left, y_bottom = project(south, west)
    x_right, y_top = project(north, east)
    return x_left, y_bottom, x_right - x_left, y_top - y_bottom


def plan(lat: float, lon: float, lat_min: float, lon_min: float,
         lat_max: float, lon_max: float, extent_m: float,
         provider: Provider, target_px: float = 1600.0,
         budget: int = 140, margin: int = 1
         ) -> Tuple[int, List[Tuple[int, int, int]]]:
    """Zoom level and tile list covering a track's bounding box.

    When the ideal zoom needs more tiles than the budget allows, the zoom is
    *reduced* until it fits rather than the tile list being cut short. Cutting
    it short is much worse than it sounds: the list is built column by column,
    so truncation removes whole columns from one side of the map and leaves a
    blank strip beside the track. Half a circuit at full resolution is not a
    reasonable trade for the whole circuit at one level less.
    """
    z = pick_zoom(lat, extent_m, target_px, provider.max_zoom)
    tiles: List[Tuple[int, int, int]] = []
    while z >= 8:
        tiles = tiles_for_bbox(lat_min, lon_min, lat_max, lon_max, z,
                               margin=margin, limit=budget * 4)
        if len(tiles) <= budget:
            return z, tiles
        z -= 1
    return z, tiles[:budget]
