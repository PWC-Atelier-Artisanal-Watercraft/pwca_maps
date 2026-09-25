"""Builds the North America overview (PMT base layer, FORMAT.md section 7.1) from Natural Earth 1:10m.

Usage: python tools/build_overview.py <natural_earth_dir> <out.pmt>

The directory holds the Natural Earth downloads as zips (README.md "Sources"): ne_10m_land, ne_10m_minor_islands,
ne_10m_lakes, ne_10m_lakes_north_america, ne_10m_rivers_lake_centerlines, ne_10m_rivers_north_america. Missing
supplements are skipped.

Land is class 1, lakes class 2 (drawn over land), rivers class 3 (lines). Small features only appear from Natural
Earth's own `min_zoom` (or `scalerank` when there is no min_zoom). Only the tiles over North America are kept; nothing
is clipped to the region, so there are no artificial coastlines. Natural Earth is public domain.
"""

import sys
import time
from pathlib import Path

import numpy as np

import packgeom as pg
import pmt
import shapefile

LEVELS = [  # tile_zoom, zoom_min, zoom_max, coord_bits, buffer, tolerance (m)
    (2, 2, 4, 12, 32, 2000.0),
    (5, 5, 7, 12, 32, 300.0),
    (7, 8, 9, 12, 32, 80.0),
]
REGION = (5.0, -180.0, 84.0, -50.0)  # south, west, north, east: North America with Greenland and the Caribbean
SOURCES = [  # file stem, class, polygon (True) or line
    ("ne_10m_land", 1, True),
    ("ne_10m_minor_islands", 1, True),
    ("ne_10m_lakes", 2, True),
    ("ne_10m_lakes_north_america", 2, True),
    ("ne_10m_rivers_lake_centerlines", 3, False),
    ("ne_10m_rivers_north_america", 3, False),
]


def min_zoom(attrs):
    """The zoom a Natural Earth feature appears from."""
    if not attrs:
        return 0.0
    for key in ("min_zoom", "MIN_ZOOM"):
        v = attrs.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    for key in ("scalerank", "SCALERANK"):
        v = attrs.get(key)
        if isinstance(v, (int, float)):
            return float(v) + 1.0  # scalerank 0-10 roughly tracks the zoom a feature becomes useful
    return 0.0


def in_region(parts):
    s, w, n, e = REGION
    for lon, lat in parts:
        if lon.max() >= w and lon.min() <= e and lat.max() >= s and lat.min() <= n:
            return True
    return False


def tile_range(zoom):
    s, w, n, e = REGION
    x0, y0 = pg.world_xy(w, n, zoom)
    x1, y1 = pg.world_xy(e, s, zoom)
    last = (1 << zoom) - 1
    return max(0, int(x0)), max(0, int(y0)), min(last, int(x1)), min(last, int(y1))


def load(ne_dir, log):
    feats = []
    for stem, cls, polygon in SOURCES:
        path = Path(ne_dir) / f"{stem}.zip"
        if not path.exists():
            log(f"{stem}: not found, skipped")
            continue
        n = 0
        for attrs, stype, parts in shapefile.read_zip(path):
            if not parts or not in_region(parts):
                continue
            if polygon != (stype in shapefile.POLYGON_TYPES):
                continue
            feats.append((cls, polygon, min_zoom(attrs), parts))
            n += 1
        log(f"{stem}: {n} features over North America")
    return feats


def build_level(feats, level, log):
    tz, zmin, zmax, bits, buf, tol_m = level
    wb = tz + bits
    x0, y0, x1, y1 = tile_range(tz)
    tiles = {}
    kept = 0
    for cls, polygon, mz, parts in feats:
        if mz > zmax:
            continue
        cut = {}
        if polygon:
            rings = []
            for lon, lat in parts:
                if lon.size < 4:
                    continue
                x, y = pg.world_xy(lon[:-1] if lon[0] == lon[-1] and lat[0] == lat[-1] else lon,
                                   lat[:-1] if lon[0] == lon[-1] and lat[0] == lat[-1] else lat, wb)
                outer = shapefile.ring_is_outer(lon, lat)
                x, y = pg.simplify_ring(x, y, tol_m * pg.units_per_metre(float(np.clip(np.mean(lat), -80, 80)), wb))
                r = pg.clean_ring(x, y)
                if r is not None:
                    rings.append(pg.orient(*r, outer=outer))
            if rings and any(pg.area2(x, y) > 0 for x, y in rings):
                cut = pg.cut_polygon(rings, tz, bits, buf)
            geom = pmt.POLYGON
        else:
            lines = []
            for lon, lat in parts:
                x, y = pg.world_xy(lon, lat, wb)
                keep = pg.douglas_peucker(x, y, tol_m * pg.units_per_metre(float(np.clip(np.mean(lat), -80, 80)), wb))
                line = pg.clean_line(x[keep], y[keep])
                if line is not None:
                    lines.append(line)
            if lines:
                cut = pg.cut_line(lines, tz, bits, buf)
            geom = pmt.LINE
        for (tx, ty), rings in cut.items():
            if x0 <= tx <= x1 and y0 <= ty <= y1:
                tiles.setdefault((tx, ty), []).append(
                    (geom, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in rings]))
        kept += 1
    log(f"level z{zmin}-{zmax} (tiles z{tz}, {tol_m:g} m): {kept} features -> {len(tiles)} tiles")
    return tiles


def build(ne_dir, log=print):
    feats = load(ne_dir, log)
    levels = []
    for level in LEVELS:
        levels.append({"tile_zoom": level[0], "zoom_min": level[1], "zoom_max": level[2], "coord_bits": level[3],
                       "buffer": level[4], "tolerance_dm": int(level[5] * 10), "tiles": build_level(feats, level, log)})
    strings = ["ne-overview", "Made with Natural Earth", "Public domain", "Natural Earth", "1:10m v5", "pwca_maps"]
    return pmt.build_pmt(layer_kind=pmt.KIND_BASE, levels=levels, strings=strings,
                         ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()),
                         data_time=int(time.time()))


def main(argv):
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    t0 = time.time()
    data = build(argv[1], lambda m: print(f"[{time.time() - t0:5.0f} s] {m}", flush=True))
    Path(argv[2]).write_bytes(data)
    print(f"wrote {argv[2]}: {len(data) / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
