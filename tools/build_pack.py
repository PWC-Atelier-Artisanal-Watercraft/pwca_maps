"""Builds the detail water layer (PMT, FORMAT.md section 7.2) from an OpenStreetMap extract.

Usage: python tools/build_pack.py <extract.osm.pbf> <out_dir> [--name NAME] [--sea water-polygons-split-4326.zip]

What goes in (README.md "Sources"):
- water areas: natural=water, waterway=riverbank, landuse=reservoir, as closed ways and multipolygon relations;
  intermittent water and waste-water basins are left out; closed areas under 1 ha are dropped;
- river and canal centre lines (waterway=river|canal);
- the sea (class 1), from the osmdata.openstreetmap.de water polygons when --sea is given; an inland extract
  doesn't need it.

Each area is simplified per level (L1 40 m, L2 10 m, L3 4 m), oriented (outer rings clockwise on screen), cut into
buffered tiles and written as one PMT file with three levels, next to SOURCES.json, ATTRIBUTION.txt and
LICENSE.txt. The coverage grid marks the zoom-8 cells where the extract has data; a cell where most tiles are open sea
defaults to "full", and only its other tiles are stored (FORMAT.md section 4.2).
"""

import argparse
import hashlib
import json
import math
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

import osmpbf
import packgeom as pg
import pmt
import shapefile

TOOL = Path(__file__).resolve()
LEVELS = [  # tile_zoom, zoom_min, zoom_max, coord_bits, buffer, tolerance (m)
    (10, 10, 11, 12, 32, 40.0),
    (12, 12, 13, 12, 32, 10.0),
    (12, 14, 16, 16, 512, 4.0),
]
MIN_AREA_M2 = 10_000.0
EARTH_R = 6_378_137.0
CREDIT = "© OpenStreetMap contributors"
LICENSE_TEXT = ("This map data is made available under the Open Database License (ODbL) 1.0: "
                "https://opendatacommons.org/licenses/odbl/1-0/. Contents © OpenStreetMap contributors.\n")
ATTRIBUTION_TEXT = ("Map data © OpenStreetMap contributors, available under the Open Database License (ODbL): "
                    "openstreetmap.org/copyright. Built with the recipe published at "
                    "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps. Not for navigation.\n")


# ---- classification (FORMAT.md 7.2) -------------------------------------------------------------------------------

def area_class(tags):
    """Polygon class for water-area tags, or None."""
    if tags.get("intermittent") == "yes" or tags.get("water") in ("wastewater", "reflecting_pool"):
        return None
    if tags.get("landuse") == "basin":
        return None
    if tags.get("natural") == "water":
        w = tags.get("water", "")
        if w == "reservoir":
            return 3
        if w in ("river", "stream", "rapids"):
            return 4
        if w in ("canal", "ditch", "drain", "lock", "moat", "harbour", "marina"):
            return 5
        return 2
    if tags.get("waterway") == "riverbank":
        return 4
    if tags.get("landuse") == "reservoir":
        return 3
    return None


def line_class(tags):
    if tags.get("intermittent") == "yes":
        return None
    return {"river": 16, "canal": 17}.get(tags.get("waterway"))


# ---- rings from relations -------------------------------------------------------------------------------------------

def assemble_rings(way_refs):
    """Joins ways (node id arrays) end to end into closed rings; returns (rings, count of ways left unclosed)."""
    rings, open_ = [], []
    for refs in way_refs:
        if len(refs) >= 4 and refs[0] == refs[-1]:
            rings.append(refs[:-1])
        elif len(refs) >= 2:
            open_.append(list(refs))
    by_end = {}
    for i, r in enumerate(open_):
        by_end.setdefault(r[0], []).append(i)
        by_end.setdefault(r[-1], []).append(i)
    used = [False] * len(open_)
    unclosed = 0
    for i in range(len(open_)):
        if used[i]:
            continue
        used[i] = True
        chain = list(open_[i])
        while chain[0] != chain[-1]:
            nxt = next((j for j in by_end.get(chain[-1], []) if not used[j]), None)
            if nxt is None:
                break
            used[nxt] = True
            seg = open_[nxt]
            chain.extend(seg[1:] if seg[0] == chain[-1] else seg[::-1][1:])
        if chain[0] == chain[-1] and len(chain) >= 4:
            rings.append(np.array(chain[:-1], np.int64))
        else:
            unclosed += 1
    return rings, unclosed


def inside(px, py, x, y):
    """Ray-casting point-in-ring test (vectorised over the ring)."""
    xn, yn = np.roll(x, -1), np.roll(y, -1)
    cond = (y > py) != (yn > py)
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = x + (py - y) * (xn - x) / (yn - y)
    return bool(np.count_nonzero(cond & (px < xc)) % 2)


# ---- build ----------------------------------------------------------------------------------------------------------

def area_m2(lat, lon):
    lat0 = math.radians(float(np.mean(lat)))
    x = np.radians(lon) * EARTH_R * math.cos(lat0)
    y = np.radians(lat) * EARTH_R
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y))) / 2


def git_commit():
    try:
        return subprocess.run(["git", "-C", str(TOOL.parent), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def collect(path, log):
    """Water polygons [(class, [(lat, lon, is_outer)])] and lines [(class, lat, lon)] from the extract."""
    t0 = time.time()
    rels = osmpbf.relations(path, lambda t: t.get("type") == "multipolygon" and area_class(t) is not None)
    member_ids = np.unique(np.array([m for _, _, ms in rels for typ, m, _ in ms if typ == 1], np.int64))
    log(f"relations: {len(rels)} water multipolygons, {member_ids.size} member ways ({time.time() - t0:.0f} s)")
    wanted = lambda t: area_class(t) is not None or line_class(t) is not None
    ws = osmpbf.ways(path, wanted, member_ids)
    log(f"ways: {len(ws)} ({time.time() - t0:.0f} s)")
    needed = np.unique(np.concatenate([r for _, _, r in ws])) if ws else np.zeros(0, np.int64)
    lat, lon, cells = osmpbf.nodes(path, needed, cell_zoom=8)
    log(f"nodes: {needed.size} needed, {int(np.isnan(lat).sum())} missing; {len(cells)} zoom-8 cells with data "
        f"({time.time() - t0:.0f} s)")

    def coords(refs):
        idx = np.searchsorted(needed, refs)
        la, lo = lat[idx], lon[idx]
        ok = ~np.isnan(la)
        return la[ok], lo[ok]

    way_by_id = {wid: refs for wid, _, refs in ws}
    polys, lines, stats = [], [], {"small": 0, "unclosed": 0, "orphan_holes": 0}
    for wid, tags, refs in ws:
        cls = area_class(tags)
        if cls is not None and len(refs) >= 4 and refs[0] == refs[-1]:
            la, lo = coords(refs[:-1])
            if la.size >= 3:
                if area_m2(la, lo) < MIN_AREA_M2:
                    stats["small"] += 1
                else:
                    polys.append((cls, [(la, lo, True)]))
        lc = line_class(tags)
        if lc is not None:
            la, lo = coords(refs)
            if la.size >= 2:
                lines.append((lc, la, lo))
    for rid, tags, members in rels:
        cls = area_class(tags)
        outer_refs = [way_by_id[m] for t, m, role in members if t == 1 and role in ("outer", "") and m in way_by_id]
        inner_refs = [way_by_id[m] for t, m, role in members if t == 1 and role == "inner" and m in way_by_id]
        outers, u1 = assemble_rings(outer_refs)
        inners, u2 = assemble_rings(inner_refs)
        stats["unclosed"] += u1 + u2
        outer_ll = [coords(r) for r in outers]
        outer_ll = [(la, lo) for la, lo in outer_ll if la.size >= 3]
        if not outer_ll:
            continue
        total = sum(area_m2(la, lo) for la, lo in outer_ll)
        rings = [(la, lo, True) for la, lo in outer_ll]
        for r in inners:
            la, lo = coords(r)
            if la.size < 3:
                continue
            # Keep a hole only inside one of the outers: a stray inner ring would otherwise fill as water.
            if any(inside(lo[0], la[0], olo, ola) for ola, olo in outer_ll):
                rings.append((la, lo, False))
                total -= area_m2(la, lo)
            else:
                stats["orphan_holes"] += 1
        if total < MIN_AREA_M2:
            stats["small"] += 1
            continue
        polys.append((cls, rings))
    log(f"features: {len(polys)} water areas, {len(lines)} river/canal lines; dropped {stats['small']} under 1 ha, "
        f"{stats['unclosed']} unclosed ways, {stats['orphan_holes']} holes outside their outers")
    return polys, lines, cells


def sea_polygons(path, cells, log):
    """Water polygons (class 1) from the osmdata split shapefile that meet the zoom-8 cells with data."""
    if not cells:
        return []
    n = 1 << 8
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    lon_w, lon_e = min(xs) / n * 360 - 180, (max(xs) + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * min(ys) / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (max(ys) + 1) / n))))
    polys = []
    for stype, parts in shapefile.records_in_box(path, (lat_s, lon_w, lat_n, lon_e)):
        if stype not in shapefile.POLYGON_TYPES:
            continue
        rings = []
        for lon, lat in parts:
            if lon.size >= 4 and lon[0] == lon[-1] and lat[0] == lat[-1]:
                lon, lat = lon[:-1], lat[:-1]
            if lon.size >= 3:
                rings.append((lat, lon, shapefile.ring_is_outer(lon, lat)))
        if rings:
            polys.append((1, rings))
    log(f"sea: {len(polys)} water polygons from {Path(path).name}")
    return polys


def full_square(bits, buf):
    side = 1 << bits
    return [(-buf, -buf), (side + buf, -buf), (side + buf, side + buf), (-buf, side + buf)]


def apply_coverage(tiles, cells, tz, bits, buf):
    """The coverage grid for a level, and its tiles without the open sea: in a zoom-8 cell where most tiles are only
    the full square of sea, the cell defaults to full (2), those tiles are dropped and the cell's land tiles are stored
    as empty tiles; elsewhere the cell is 1 (absent = empty)."""
    sq = full_square(bits, buf)
    is_open_sea = lambda feats: (len(feats) == 1 and feats[0][0] == pmt.POLYGON and feats[0][1] == 1
                                 and len(feats[0][3]) == 1 and feats[0][3][0] == sq)
    shift = tz - 8
    per_cell = {}
    for key, feats in tiles.items():
        per_cell.setdefault((key[0] >> shift, key[1] >> shift), []).append((key, is_open_sea(feats)))
    grid, out = {}, dict(tiles)
    side = 1 << shift
    for cell in cells:
        members = per_cell.get(cell, [])
        n_sea = sum(1 for _, sea in members if sea)
        if n_sea * 2 > side * side:
            grid[cell] = 2
            present = {k for k, _ in members}
            for key, sea in members:
                if sea:
                    del out[key]
            for dx in range(side):
                for dy in range(side):
                    key = ((cell[0] << shift) + dx, (cell[1] << shift) + dy)
                    if key not in present:
                        out[key] = []  # land: stored empty, since an absent tile here would be sea
        else:
            grid[cell] = 1
    return grid, out


def build_level(polys, lines, level, log):
    tz, zmin, zmax, bits, buf, tol_m = level
    wb = tz + bits
    tiles = {}
    n_poly = n_line = 0
    for cls, rings in polys:
        world = []
        for la, lo, outer in rings:
            x, y = pg.world_xy(lo, la, wb)
            x, y = pg.simplify_ring(x, y, tol_m * pg.units_per_metre(float(np.mean(la)), wb))
            r = pg.clean_ring(x, y)
            if r is None:
                continue
            world.append(pg.orient(*r, outer=outer))
        if not world or not any(pg.area2(x, y) > 0 for x, y in world):
            continue
        for key, parts in pg.cut_polygon(world, tz, bits, buf).items():
            tiles.setdefault(key, []).append(
                (pmt.POLYGON, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
        n_poly += 1
    for cls, la, lo in lines:
        x, y = pg.world_xy(lo, la, wb)
        keep = pg.douglas_peucker(x, y, tol_m * pg.units_per_metre(float(np.mean(la)), wb))
        line = pg.clean_line(x[keep], y[keep])
        if line is None:
            continue
        for key, parts in pg.cut_line([line], tz, bits, buf).items():
            tiles.setdefault(key, []).append(
                (pmt.LINE, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
        n_line += 1
    log(f"level z{zmin}-{zmax} (tiles z{tz}, {tol_m:g} m): {n_poly} areas, {n_line} lines -> {len(tiles)} tiles")
    return tiles


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extract")
    ap.add_argument("out_dir")
    ap.add_argument("--name", default=None, help="pack file name (default: the extract's name)")
    ap.add_argument("--sea", default=None, help="osmdata water-polygons-split-4326.zip (for coastal extracts)")
    a = ap.parse_args(argv[1:])
    src = Path(a.extract)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "")
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)

    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    polys, lines, cells = collect(src, log)
    if a.sea:
        polys = sea_polygons(a.sea, cells, log) + polys
    levels = []
    for level in LEVELS:
        tz, bits, buf = level[0], level[3], level[4]
        grid, tiles = apply_coverage(build_level(polys, lines, level, log), cells, tz, bits, buf)
        full = sum(1 for v in grid.values() if v == 2)
        if full:
            log(f"  {full} of {len(grid)} cells default to open sea; {len(tiles)} tiles stored")
        levels.append({"tile_zoom": tz, "zoom_min": level[1], "zoom_max": level[2], "coord_bits": bits,
                       "buffer": buf, "tolerance_dm": int(level[5] * 10), "full_class": 1, "grid": grid,
                       "tiles": tiles})
    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    commit = git_commit()
    strings = ["osm-water", CREDIT, "ODbL 1.0", "OpenStreetMap", date, f"pwca_maps {commit}"]
    data = pmt.build_pmt(layer_kind=pmt.KIND_WATER, levels=levels, strings=strings,
                         ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()), data_time=stamp)
    pack = out / f"{name}.pmt"
    pack.write_bytes(data)
    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8")
    sources = {
        "format": "PMT 1.0 (FORMAT.md)",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                     "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                     "writing_program": head["writing_program"], "extract_source": head["source"]}],
        "files": {pack.name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}},
    }
    (out / "SOURCES.json").write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in sorted(out.iterdir())
                if p.is_file() and p.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    pf = pmt.PmtFile(data)
    counts = ", ".join(f"L{i + 1} {lv.entry_count} tiles" for i, lv in enumerate(pf.levels))
    log(f"wrote {pack} ({len(data) / 1e6:.1f} MB; {counts}); data as of {date}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
