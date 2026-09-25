"""Builds the detail water layer (PMT, FORMAT.md section 7.2) from an OpenStreetMap extract.

Usage: python tools/build_pack.py <extract.osm.pbf> <out_dir> [--name NAME] [--sea water-polygons-split-4326.zip]
       [--processes N] [--region-zoom Z] [--max-file-bytes B] [--low-priority]

What goes in (README.md "Sources"):
- water areas: natural=water, waterway=riverbank, landuse=reservoir, as closed ways and multipolygon relations;
  intermittent water and waste-water basins are left out; closed areas under 1 ha are dropped;
- river and canal centre lines (waterway=river|canal);
- the sea (class 1), from the osmdata.openstreetmap.de water polygons when --sea is given; an inland extract
  doesn't need it.

Each area is simplified per level (L1 40 m, L2 10 m, L3 4 m), oriented (outer rings clockwise on screen), cut into
buffered tiles and written as PMT files with three levels, next to SOURCES.json, ATTRIBUTION.txt and LICENSE.txt.
The coverage grid marks the zoom-8 cells where the extract has data; a cell where most tiles are open sea defaults to
"full", and only its other tiles are stored (FORMAT.md section 4.2).

Scale (all of North America): the passes over the extract keep only compact arrays (way node ids, node coordinates in
1e-7 degrees) and write the kept features to a feature store on disk (featurestore.py). Worker processes then build
the tiles one region (a quadtree node at --region-zoom) at a time from the memory-mapped store and return them
encoded. Regions are written in quadtree order, starting a new file before one would pass --max-file-bytes (FAT32
cards; the format's 32-bit offsets); each file's coverage grid holds only its own regions' cells. A region's tiles are
exactly the tiles a single whole-extract build makes there.
"""

import argparse
import hashlib
import json
import math
import gc
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

import featurestore
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
REGION_ZOOM = 7  # regions of z7 tiles (about 300 km): one worker job each
REGION_MARGIN_DEG = 0.05  # features within this of a region are cut for it (the largest tile buffer is < 0.003 deg)
MAX_FILE_BYTES = 2_000_000_000  # under 2 GiB, with room for the header block and index
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


def water_relation(tags):
    return tags.get("type") == "multipolygon" and area_class(tags) is not None


def way_kind(tags):
    """(area class, line class) of a way, or None when it is neither."""
    a, line = area_class(tags), line_class(tags)
    return None if a is None and line is None else (a, line)


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


def collect_store(path, store_dir, log, processes, sea=None):
    """Reads the extract into feature stores under store_dir ("poly": sea, then areas from ways, then from relations;
    "line": river and canal lines), in the order a tile draws them. Returns (the zoom-8 cells with data, stats)."""
    t0 = time.time()
    rels = osmpbf.relations_parallel(path, water_relation, processes)
    member_ids = np.unique(np.array([m for _, _, ms in rels for typ, m, _ in ms if typ == 1], np.int64))
    log(f"relations: {len(rels)} water multipolygons, {member_ids.size} member ways ({time.time() - t0:.0f} s)")
    ws = osmpbf.ways_parallel(path, way_kind, member_ids, processes)  # (id, (area, line) or () for members, refs)
    del member_ids
    refs_total = sum(len(r) for _, _, r in ws)
    log(f"ways: {len(ws)}, {refs_total} node references ({time.time() - t0:.0f} s)")
    if ws:
        needed = np.concatenate([r for _, _, r in ws])
        needed.sort()
        keep = np.ones(needed.size, bool)
        keep[1:] = needed[1:] != needed[:-1]
        needed = needed[keep]
        del keep
    else:
        needed = np.zeros(0, np.int64)
    lat, lon, cells = osmpbf.nodes_e7_parallel(path, needed, processes, cell_zoom=8)
    missing = np.iinfo(np.int32).min
    log(f"nodes: {needed.size} needed, {int((lat == missing).sum())} missing; {len(cells)} zoom-8 cells with data "
        f"({time.time() - t0:.0f} s)")

    def coords(refs):
        """(lat, lon) in 1e-7 degrees, without missing nodes."""
        idx = np.searchsorted(needed, refs)
        la, lo = lat[idx], lon[idx]
        ok = la != missing
        return la[ok], lo[ok]

    store_dir = Path(store_dir)
    polys = featurestore.StoreWriter(store_dir / "poly")
    lines = featurestore.StoreWriter(store_dir / "line")
    stats = {"sea": 0, "small": 0, "unclosed": 0, "orphan_holes": 0}
    if sea:
        for cls, rings in sea_polygon_iter(sea, cells):
            polys.add(cls, [(featurestore.e7(la), featurestore.e7(lo), outer) for la, lo, outer in rings])
            stats["sea"] += 1
        log(f"sea: {stats['sea']} water polygons from {Path(sea).name} ({time.time() - t0:.0f} s)")
    deg = featurestore.degrees
    for wid, kind, refs in ws:
        cls, lc = kind if kind else (None, None)
        if cls is not None and len(refs) >= 4 and refs[0] == refs[-1]:
            la, lo = coords(refs[:-1])
            if la.size >= 3:
                if area_m2(deg(la), deg(lo)) < MIN_AREA_M2:
                    stats["small"] += 1
                else:
                    polys.add(cls, [(la, lo, True)])
        if lc is not None:
            la, lo = coords(refs)
            if la.size >= 2:
                lines.add(lc, [(la, lo, False)])
    way_by_id = {wid: refs for wid, _, refs in ws}
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
        total = sum(area_m2(deg(la), deg(lo)) for la, lo in outer_ll)
        rings = [(la, lo, True) for la, lo in outer_ll]
        for r in inners:
            la, lo = coords(r)
            if la.size < 3:
                continue
            # Keep a hole only inside one of the outers: a stray inner ring would otherwise fill as water.
            if any(inside(deg(lo[0]), deg(la[0]), deg(olo), deg(ola)) for ola, olo in outer_ll):
                rings.append((la, lo, False))
                total -= area_m2(deg(la), deg(lo))
            else:
                stats["orphan_holes"] += 1
        if total < MIN_AREA_M2:
            stats["small"] += 1
            continue
        polys.add(cls, rings)
    n_poly, n_line, n_points = len(polys), len(lines), polys.points + lines.points
    polys.close()
    lines.close()
    log(f"features: {n_poly} water areas (with the sea pieces), {n_line} river/canal lines; dropped {stats['small']} "
        f"under 1 ha, {stats['unclosed']} unclosed ways, {stats['orphan_holes']} holes outside their outers; "
        f"{n_points} points in the store ({time.time() - t0:.0f} s)")
    return cells, stats


def sea_polygons(path, cells, log):
    """Water polygons (class 1) from the osmdata split shapefile that meet the zoom-8 cells with data."""
    polys = list(sea_polygon_iter(path, cells))
    log(f"sea: {len(polys)} water polygons from {Path(path).name}")
    return polys


def sea_polygon_iter(path, cells):
    """Streams sea_polygons' result: (1, [(lat, lon, is_outer)]) per polygon."""
    if not cells:
        return
    n = 1 << 8
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    lon_w, lon_e = min(xs) / n * 360 - 180, (max(xs) + 1) / n * 360 - 180
    lat_n = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * min(ys) / n))))
    lat_s = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (max(ys) + 1) / n))))
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
            yield 1, rings


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


def level_tiles(polys, lines, level, region=None):
    """One level's tiles {(x, y): features} from polygons [(class, [(lat, lon, outer)])] and lines
    [(class, lat, lon)], drawn in that order; with region = (rz, rx, ry), only that quadtree node's tiles."""
    tz, zmin, zmax, bits, buf, tol_m = level
    wb = tz + bits
    tiles = {}
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
        for key, parts in pg.cut_polygon(world, tz, bits, buf, region).items():
            tiles.setdefault(key, []).append(
                (pmt.POLYGON, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
    for cls, la, lo in lines:
        x, y = pg.world_xy(lo, la, wb)
        keep = pg.douglas_peucker(x, y, tol_m * pg.units_per_metre(float(np.mean(la)), wb))
        line = pg.clean_line(x[keep], y[keep])
        if line is None:
            continue
        for key, parts in pg.cut_line([line], tz, bits, buf, region).items():
            tiles.setdefault(key, []).append(
                (pmt.LINE, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
    return tiles


def region_box_e7(rz, rx, ry, margin_deg):
    """(south, west, north, east) of a quadtree node, grown by margin_deg, in 1e-7 degrees."""
    n = 1 << rz
    west, east = rx / n * 360 - 180, (rx + 1) / n * 360 - 180
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ry / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ry + 1) / n))))
    return tuple(int(round(v * 1e7)) for v in (south - margin_deg, west - margin_deg, north + margin_deg,
                                                east + margin_deg))


_STORES = {}


def _stores(store_dir):
    if store_dir not in _STORES:
        _STORES.clear()
        _STORES[store_dir] = (featurestore.Store(Path(store_dir) / "poly"), featurestore.Store(Path(store_dir) / "line"))
    return _STORES[store_dir]


def region_job(job):
    """Worker: one region's tiles for every level, encoded: (region, [(grid, {key: blob}) per level], features)."""
    store_dir, region, cells = job
    poly_store, line_store = _stores(store_dir)
    box = region_box_e7(*region, REGION_MARGIN_DEG)
    polys = [poly_store.feature(i) for i in poly_store.select(*box)]
    lines = [(c, rings[0][0], rings[0][1]) for c, rings in (line_store.feature(i) for i in line_store.select(*box))]
    out = []
    for level in LEVELS:
        tz, bits, buf = level[0], level[3], level[4]
        grid, tiles = apply_coverage(level_tiles(polys, lines, level, region), cells, tz, bits, buf)
        out.append((grid, {key: pmt.encode_tile(feats, bits, buf) for key, feats in tiles.items()}))
    return region, out, len(polys) + len(lines)


def regions_of(cells, rz):
    """{region (rz, rx, ry): [its zoom-8 cells]} in quadtree (Morton) order."""
    shift = 8 - rz
    by_region = {}
    for cx, cy in sorted(cells):
        by_region.setdefault((rz, cx >> shift, cy >> shift), []).append((cx, cy))
    return dict(sorted(by_region.items(), key=lambda kv: pmt.morton(kv[0][1], kv[0][2])))


def lower_priority():
    """Below-normal priority (Windows: the worker processes inherit it), so a long build leaves the machine usable."""
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
    else:
        os.nice(10)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extract")
    ap.add_argument("out_dir")
    ap.add_argument("--name", default=None, help="pack file name (default: the extract's name)")
    ap.add_argument("--sea", default=None, help="osmdata water-polygons-split-4326.zip (for coastal extracts)")
    ap.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--region-zoom", type=int, default=REGION_ZOOM, help="regions: quadtree nodes at this zoom (0-8)")
    ap.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    ap.add_argument("--low-priority", action="store_true", help="run below normal priority")
    ap.add_argument("--keep-store", action="store_true", help="keep the feature store (<out_dir>/store)")
    a = ap.parse_args(argv[1:])
    if not 0 <= a.region_zoom <= 8:
        ap.error("--region-zoom must be 0-8 (regions hold whole zoom-8 coverage cells)")
    if a.low_priority:
        lower_priority()
    src = Path(a.extract)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "")
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)

    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    store_dir = out / "store"
    if store_dir.exists():
        shutil.rmtree(store_dir)
    cells, _ = collect_store(src, store_dir, log, a.processes, a.sea)
    gc.collect()
    regions = regions_of(cells, a.region_zoom)
    log(f"{len(regions)} regions (zoom {a.region_zoom}) over {len(cells)} zoom-8 cells; cutting with {a.processes} "
        f"processes")

    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    commit = git_commit()
    strings = ["osm-water", CREDIT, "ODbL 1.0", "OpenStreetMap", date, f"pwca_maps {commit}"]
    files = []  # (path, bytes, sha256, tiles per level)

    def new_acc():
        return [({}, {}) for _ in LEVELS]  # per level: grid, tiles

    def flush(acc):
        levels = []
        for (grid, tiles), (tz, zmin, zmax, bits, buf, tol) in zip(acc, LEVELS):
            levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": buf,
                           "tolerance_dm": int(tol * 10), "full_class": 1, "grid": grid, "tiles": tiles})
        data = pmt.build_pmt(layer_kind=pmt.KIND_WATER, levels=levels, strings=strings,
                             ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()), data_time=stamp)
        path = out / f"{name}.part{len(files) + 1}.pmt"
        path.write_bytes(data)
        files.append((path, len(data), hashlib.sha256(data).hexdigest(), [len(t) for _, t in acc]))
        log(f"wrote {path.name}: {len(data) / 1e6:.1f} MB, tiles per level {[len(t) for _, t in acc]}")

    jobs = [(str(store_dir), region, rc) for region, rc in regions.items()]
    acc, acc_bytes, done, last_log = new_acc(), 0, 0, time.time()
    pool = multiprocessing.get_context("spawn").Pool(a.processes) if a.processes > 1 else None
    try:
        results = pool.imap(region_job, jobs) if pool else map(region_job, jobs)
        for region, res, nfeat in results:
            size = sum(len(b) for _, blobs in res for b in blobs.values())
            if acc_bytes and acc_bytes + size > a.max_file_bytes:
                flush(acc)
                acc, acc_bytes = new_acc(), 0
            for (grid, tiles), (g, blobs) in zip(acc, res):
                grid.update(g)
                tiles.update(blobs)
            acc_bytes += size
            done += 1
            if time.time() - last_log > 60 or done == len(jobs):
                last_log = time.time()
                el = time.time() - t0
                log(f"regions {done}/{len(jobs)} (last z{region[0]} {region[1]},{region[2]}: {nfeat} features, "
                    f"{size / 1e6:.1f} MB); this file {acc_bytes / 1e6:.0f} MB")
    finally:
        if pool:
            pool.close()
            pool.join()
    if acc_bytes or not files:
        flush(acc)
    if len(files) == 1:
        final = [out / f"{name}.pmt"]
    else:
        final = [out / f"{name}-{i + 1:02d}.pmt" for i in range(len(files))]
    for (path, *_), dst in zip(files, final):
        path.replace(dst)
    if not a.keep_store:
        shutil.rmtree(store_dir, ignore_errors=True)

    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8")
    sources = [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                "writing_program": head["writing_program"], "extract_source": head["source"]}]
    if a.sea:
        sources.append({"name": "OpenStreetMap water polygons (osmdata.openstreetmap.de)", "license": "ODbL 1.0",
                        "file": Path(a.sea).name, "sha256": sha256(a.sea)})
    info = {
        "format": "PMT 1.0 (FORMAT.md)",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
        "files": {dst.name: {"bytes": n, "sha256": h, "tiles_per_level": t} for (_, n, h, t), dst in zip(files, final)},
    }
    (out / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    total = sum(n for _, n, _, _ in files)
    log(f"done: {len(final)} file(s), {total / 1e6:.1f} MB in {out}; data as of {date}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
