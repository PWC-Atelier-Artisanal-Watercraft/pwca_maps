"""Builds the SYMBOLS file (PMT 1.3, layer_kind 7) and the STREET NAMES file (layer_kind 8) of a pack from an
OpenStreetMap extract. The format is FORMAT-1.3-SYMBOLS-STREET-NAMES.md.

Usage: python tools/build_extras.py <extract.osm.pbf> <out_dir> [--name N] [--only symbols|streets] [--road-fuel]
                                    [--processes N] [--high-priority] [--max-file-bytes N]

Writes <out_dir>/<name>-symbols.pmt and <out_dir>/<name>-streets.pmt (default name: the extract's name), LICENSE.txt,
ATTRIBUTION.txt, SOURCES.json and MANIFEST.sha256: a pack directory that tools/assemble_pack.py puts beside the other
layers of a card pack. No file that is already built is read or changed.

Symbols: a point with a class for every fuel dock (1), marina (2) and boat ramp (3) that is not private, with its
name when it has one a display can draw. With --road-fuel the filling stations on roads are written as class 4.
Street names: every named road of the roads layer's classes 1 to 6 (motorway to residential) as straight runs, each
a line of two points with the name, cut at the tile edges.

One pass over the extract's tagged points, one over its ways, one for the ways' points; then the tiles. A line
"PROGRESS n% of this build (...)" is printed each time the whole percent changes.
"""

import argparse
import array
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

import osmpbf
import packgeom as pg
import pmt
from build_pack import (ATTRIBUTION_TEXT, CREDIT, INDEX_BYTES, LICENSE_TEXT, MAX_FILE_BYTES, ROAD_CLASSES,
                        TILE_FEATURE_BUDGET, git_commit, join_lines, label_point, sha256)
from build_places import label_name

FUEL, MARINA, RAMP, ROAD_FUEL = 1, 2, 3, 4
CLASS_NAMES = {FUEL: "fuel", MARINA: "marina", RAMP: "boat_ramp", ROAD_FUEL: "road_fuel"}
# tile_zoom, zoom_min, zoom_max, coord_bits, classes: the names file's levels from zoom 10. Fuel and marina from zoom
# 10, the boat ramp from 12 (Branding's style, pwca_logos d536870); a road's filling station from 12.
SYMBOL_LEVELS = [
    (10, 10, 11, 12, {FUEL, MARINA}),
    (12, 12, 13, 12, {FUEL, MARINA, RAMP, ROAD_FUEL}),
    (12, 14, 14, 16, {FUEL, MARINA, RAMP, ROAD_FUEL}),
    (14, 15, 16, 14, {FUEL, MARINA, RAMP, ROAD_FUEL}),
]
SYMBOL_NAME_ZOOM = 14  # a symbol's name is written in the levels that serve this zoom and closer (drawn from 14)
SYMBOL_MERGE_M = 60.0  # two symbols of one class nearer than this are one (a marina mapped as a point and an area)
# tile_zoom, zoom_min, zoom_max, coord_bits, classes, straightness in metres, shortest run in metres per name
# character. Both levels have 2^28 units round the world. Branding: motorway, trunk and primary from zoom 14, the rest
# from 15 and 16; a name is drawn only where its road stays within half the text's height of a straight line.
STREET_LEVELS = [
    (12, 14, 14, 16, {1, 2, 3}, 24.0, 12.0),
    (14, 15, 16, 14, {1, 2, 3, 4, 5, 6}, 6.0, 1.5),
]
STREET_WORLD_BITS = 28
RUNS_PER_NAME = 3  # the longest runs of one name and class kept in a tile
REGION_ZOOM = 7  # a file over --max-file-bytes is split between regions of zoom-7 tiles, as build_pack.py does

# Tagged points worth decoding: (key, value) pairs, and keys with any value.
SCAN_PAIRS = {"leisure": ("marina", "slipway"), "waterway": ("fuel",), "amenity": ("fuel",)}
SCAN_KEYS = ("seamark:type",)


def symbol_classes(tags, road_fuel=False):
    """The symbol classes of an OSM object's tags, ascending; () for none. A private place is left out."""
    if tags.get("access") in ("private", "no"):
        return ()
    out = set()
    cats = set((tags.get("seamark:small_craft_facility:category") or "").replace(" ", "").split(";"))
    leisure = tags.get("leisure")
    if leisure == "marina" or tags.get("seamark:harbour:category") == "marina":
        out.add(MARINA)
    if leisure == "slipway" or "slipway" in cats:
        out.add(RAMP)
    if tags.get("waterway") == "fuel" or "fuel_station" in cats:
        out.add(FUEL)
    if MARINA in out and (tags.get("fuel") == "yes" or any(k.startswith("fuel:") and v == "yes"
                                                             for k, v in tags.items())):
        out.add(FUEL)  # a marina that sells fuel is two symbols at one point; a display shows fuel (the style, 8)
    if tags.get("amenity") == "fuel" and FUEL not in out:
        if tags.get("boat") == "yes" or tags.get("seamark:type") or MARINA in out:
            out.add(FUEL)
        elif road_fuel:
            out.add(ROAD_FUEL)
    return tuple(sorted(out))


def symbol_name(tags):
    """A symbol's name as label bytes, or b"": its name, else its English name, else its brand or operator."""
    for key in ("name", "name:en", "brand", "operator"):
        name = label_name(tags.get(key))
        if name:
            return name
    return b""


def _symbol_nodes_in(block, road_fuel):
    """Dense nodes that are symbols: [(node id, lat_e7, lon_e7, classes, name bytes)]. Only nodes with one of the
    SCAN tags are decoded in Python (as osmpbf._tagged_nodes_in does for one key)."""
    strings, groups, gran, lat_off, lon_off = block
    index = {}
    for i, s in enumerate(strings):
        index.setdefault(s, i)
    pairs = [(index[k], index[v]) for k, vs in SCAN_PAIRS.items() if k in index for v in vs if v in index]
    keys = [index[k] for k in SCAN_KEYS if k in index]
    if not pairs and not keys:
        return []
    out = []
    for g in groups:
        for fn, dense in osmpbf.fields(g):
            if fn != 2:
                continue
            ids = la = lo = kv = None
            for f2, v in osmpbf.fields(dense):
                if f2 == 1:
                    ids = np.cumsum(osmpbf.zigzag(osmpbf.varints(v)))
                elif f2 == 8:
                    la = np.cumsum(osmpbf.zigzag(osmpbf.varints(v)))
                elif f2 == 9:
                    lo = np.cumsum(osmpbf.zigzag(osmpbf.varints(v)))
                elif f2 == 10:
                    kv = osmpbf.varints(v)
            if ids is None or kv is None or not kv.size:
                continue
            z = kv == 0
            node_of = np.cumsum(z) - z  # the node each entry belongs to (its delimiter included)
            starts = np.r_[0, np.flatnonzero(z)[:-1] + 1]
            off = np.arange(kv.size) - starts[node_of]
            nxt = np.r_[kv[1:], 0]
            hit = np.zeros(kv.size, bool)
            for k, val in pairs:
                hit |= (kv == k) & (nxt == val)
            for k in keys:
                hit |= kv == k
            for n in np.unique(node_of[np.flatnonzero(hit & (off % 2 == 0) & ~z)]).tolist():
                s = int(starts[n])
                e = s
                while e < kv.size and kv[e] != 0:
                    e += 1
                p = kv[s:e]
                tags = {strings[int(p[i])]: strings[int(p[i + 1])] for i in range(0, len(p) - 1, 2)}
                classes = symbol_classes(tags, road_fuel)
                if classes:
                    out.append((int(ids[n]), int(osmpbf._e7(np.int64(lat_off + gran * la[n]))),
                                int(osmpbf._e7(np.int64(lon_off + gran * lo[n]))), classes, symbol_name(tags)))
    return out


def extras_way(tags):
    """What a way gives: ("r", road class, name bytes) for a named road, ("s", symbol classes, name bytes) for a
    symbol mapped as a line or an area, else None. The options reach the worker processes through the environment."""
    only = os.environ.get("PWCA_EXTRAS_ONLY", "")
    hw = tags.get("highway")
    if hw is not None:
        c = ROAD_CLASSES.get(hw)
        if only == "symbols" or c is None or c > 6 or tags.get("area") == "yes":
            return None
        name = label_name(tags.get("name")) or label_name(tags.get("name:en"))
        return ("r", c, name) if name else None
    if only == "streets":
        return None
    classes = symbol_classes(tags, os.environ.get("PWCA_EXTRAS_ROAD_FUEL") == "1")
    return ("s", classes, symbol_name(tags)) if classes else None


# ---- symbols --------------------------------------------------------------------------------------------------------

def merge_symbols(points):
    """points: [(class, lat_e7, lon_e7, name, uid)]. Of two of one class within SYMBOL_MERGE_M the one with a name
    stays (then the smaller uid). Returns the kept points, sorted by class and uid, and how many were merged away."""
    kept, cells = [], {}
    for p in sorted(points, key=lambda q: (q[0], not q[3], q[4])):
        cls, la, lo = p[0], p[1] / 1e7, p[2] / 1e7
        x, y = pg.world_xy(lo, la, 26)
        x, y = float(x), float(y)
        d = SYMBOL_MERGE_M * pg.units_per_metre(la, 26)
        cx, cy = int(x // d), int(y // d)
        near = False
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for qx, qy in cells.get((cls, ix, iy), ()):
                    if math.hypot(qx - x, qy - y) < d:
                        near = True
        if not near:
            cells.setdefault((cls, cx, cy), []).append((x, y))
            kept.append(p)
    kept.sort(key=lambda q: (q[0], q[4]))
    return kept, len(points) - len(kept)


def symbol_level_tiles(points, level):
    """{(tx, ty): [point features]} of one level and its coverage grid cells. A tile's features are in class order,
    then by uid (the same bytes on every run); a level that serves only zooms below SYMBOL_NAME_ZOOM has no names."""
    tz, _zmin, zmax, bits, classes = level
    side, limit = 1 << bits, (1 << tz) - 1
    tiles = {}
    for cls, la, lo, name, uid in points:
        if cls not in classes:
            continue
        x, y = pg.world_xy(lo / 1e7, la / 1e7, tz + bits)
        x, y = int(np.floor(x)), int(np.floor(y))
        tx, ty = x >> bits, y >> bits
        if 0 <= tx <= limit and 0 <= ty <= limit:
            tiles.setdefault((tx, ty), []).append((cls, uid, x - tx * side, y - ty * side,
                                                   name if zmax >= SYMBOL_NAME_ZOOM else b""))
    out, grid, dropped = {}, {}, 0
    for key, items in tiles.items():
        items.sort(key=lambda it: (it[0], it[1]))
        if len(items) > TILE_FEATURE_BUDGET:
            dropped += len(items) - TILE_FEATURE_BUDGET
            items = items[:TILE_FEATURE_BUDGET]
        out[key] = pmt.encode_tile([(pmt.POINT, cls, None, (x, y, name)) for cls, _uid, x, y, name in items],
                                   bits, 0, pmt.KIND_SYMBOLS)
        grid[(key[0] >> (tz - 8), key[1] >> (tz - 8))] = 1
    return out, grid, dropped


# ---- street names ---------------------------------------------------------------------------------------------------

def straight_runs(x, y, tol):
    """Cuts a line (numpy float arrays) into runs (i, j) of point indexes: from i the run goes on while every point
    of it stays within tol of the straight line from its first point to its last and the line does not turn back."""
    n, out, i = len(x), [], 0
    while i < n - 1:
        j = i + 1
        while j + 1 < n:
            k = j + 1
            dx, dy = float(x[k] - x[i]), float(y[k] - y[i])
            length = math.hypot(dx, dy)
            if length == 0.0 or (float(x[k] - x[j]) * dx + float(y[k] - y[j]) * dy) <= 0.0:
                break
            d = np.abs((x[i + 1:k] - x[i]) * dy - (y[i + 1:k] - y[i]) * dx)
            if float(d.max()) > tol * length:
                break
            j = k
        out.append((i, j))
        i = j
    return out


def split_at_tiles(x0, y0, x1, y1, side):
    """A straight run in world units cut at the tile edges: [(tx, ty, ax, ay, bx, by, length)], the points in the
    tile's own units (0 to side), the length in units."""
    ts = [0.0, 1.0]
    for a0, a1 in ((x0, x1), (y0, y1)):
        lo, hi = (a0, a1) if a0 <= a1 else (a1, a0)
        k = math.floor(lo / side) + 1
        while k * side < hi:
            ts.append((k * side - a0) / (a1 - a0))
            k += 1
    ts.sort()
    out = []
    for ta, tb in zip(ts, ts[1:]):
        if tb <= ta:
            continue
        ax, ay = x0 + (x1 - x0) * ta, y0 + (y1 - y0) * ta
        bx, by = x0 + (x1 - x0) * tb, y0 + (y1 - y0) * tb
        tx, ty = int((ax + bx) / 2 // side), int((ay + by) / 2 // side)
        pts = [min(side, max(0, int(round(v)))) for v in (ax - tx * side, ay - ty * side, bx - tx * side,
                                                          by - ty * side)]
        if pts[0] == pts[2] and pts[1] == pts[3]:
            continue
        out.append((tx, ty, pts[0], pts[1], pts[2], pts[3], math.hypot(bx - ax, by - ay)))
    return out


class RunStore:
    """The runs of one level as columns (a few tens of bytes a run, where a tuple would take hundreds)."""

    def __init__(self):
        self.key = array.array("Q")  # tx << 32 | ty
        self.cls = array.array("B")
        self.length = array.array("I")
        self.name = array.array("I")  # index into the list of names
        self.xy = array.array("i")  # ax, ay, bx, by

    def add(self, tx, ty, cls, length, name_id, ax, ay, bx, by):
        self.key.append((tx << 32) | ty)
        self.cls.append(cls)
        self.length.append(min(int(length), 0xFFFFFFFF))
        self.name.append(name_id)
        self.xy.extend((ax, ay, bx, by))

    def __len__(self):
        return len(self.key)


def street_level_tiles(store, names, level):
    """{(tx, ty): tile blob} of one level and its coverage grid cells, with counts. A tile's runs: the larger road
    first (class ascending), then the longer run; at most RUNS_PER_NAME of one name and class."""
    tz, _zmin, _zmax, bits = level[:4]
    stats = {"runs": 0, "over_budget": 0, "by_class": {}}
    if not len(store):
        return {}, {}, stats, set()
    key = np.frombuffer(store.key, np.uint64)
    cls = np.frombuffer(store.cls, np.uint8)
    length = np.frombuffer(store.length, np.uint32)
    name = np.frombuffer(store.name, np.uint32)
    xy = np.frombuffer(store.xy, np.int32).reshape(-1, 4)
    order = np.lexsort((name, -length.astype(np.int64), cls, key))
    skey = key[order]
    cuts = np.r_[0, np.flatnonzero(skey[1:] != skey[:-1]) + 1, skey.size]
    out, grid, used = {}, {}, set()
    for a, b in zip(cuts[:-1].tolist(), cuts[1:].tolist()):
        rows = order[a:b]
        k = int(skey[a])
        tx, ty = k >> 32, k & 0xFFFFFFFF
        feats, seen = [], {}
        for c, nid, (ax, ay, bx, by) in zip(cls[rows].tolist(), name[rows].tolist(), xy[rows].tolist()):
            n = seen.get((c, nid), 0)
            if n >= RUNS_PER_NAME:
                continue
            seen[(c, nid)] = n + 1
            feats.append((pmt.LINE, c, None, (names[nid], [(ax, ay), (bx, by)])))
        if len(feats) > TILE_FEATURE_BUDGET:  # the smallest roads' shortest runs go first
            stats["over_budget"] += len(feats) - TILE_FEATURE_BUDGET
            feats = feats[:TILE_FEATURE_BUDGET]
        for _g, c, _a, (nm, _pts) in feats:
            stats["by_class"][c] = stats["by_class"].get(c, 0) + 1
            used.add(nm)
        stats["runs"] += len(feats)
        out[(tx, ty)] = pmt.encode_tile(feats, bits, 0, pmt.KIND_STREETS)
        grid[(tx >> (tz - 8), ty >> (tz - 8))] = 1
    return out, grid, stats, used


# ---- files ----------------------------------------------------------------------------------------------------------

def write_layer(out, name, kind, layer_name, specs, per_level, stamp, commit, max_bytes, log):
    """Writes one layer as <name>.pmt, or as <name>-01.pmt, -02 ... when it is over max_bytes (cut between regions of
    zoom-7 tiles). specs: [(tile_zoom, zoom_min, zoom_max, coord_bits, tolerance_dm)]; per_level: [(tiles {key: blob},
    grid)]. Returns [(path, bytes, sha256, tiles per level)]."""
    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    strings = [layer_name, CREDIT, "ODbL 1.0", "OpenStreetMap", date, f"pwca_maps {commit}"]
    regions = {}
    for i, ((tz, *_), (tiles, _grid)) in enumerate(zip(specs, per_level)):
        for (tx, ty), blob in tiles.items():
            r = (tx >> (tz - REGION_ZOOM), ty >> (tz - REGION_ZOOM))
            regions.setdefault(r, [dict() for _ in specs])[i][(tx, ty)] = blob
    files = []

    def flush(acc):
        levels = []
        for (tz, zmin, zmax, bits, tol_dm), tiles in zip(specs, acc):
            grid = {(tx >> (tz - 8), ty >> (tz - 8)): 1 for tx, ty in tiles}
            levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": 0,
                           "tolerance_dm": tol_dm, "full_class": 0, "grid": grid, "tiles": tiles})
        data = pmt.build_pmt(layer_kind=kind, levels=levels, strings=strings, ids=dict(zip(pmt.ID_FIELDS, range(6))),
                             build_time=int(time.time()), data_time=stamp)
        path = out / f"{name}.part{len(files) + 1}.pmt"
        path.write_bytes(data)
        files.append((path, len(data), hashlib.sha256(data).hexdigest(), [len(t) for t in acc]))

    acc, acc_bytes = [dict() for _ in specs], 0
    for r in sorted(regions, key=lambda q: pmt.morton(q[0], q[1])):
        size = sum(len(b) + INDEX_BYTES for tiles in regions[r] for b in tiles.values())
        if acc_bytes and acc_bytes + size > max_bytes:
            flush(acc)
            acc, acc_bytes = [dict() for _ in specs], 0
        for tiles, more in zip(acc, regions[r]):
            tiles.update(more)
        acc_bytes += size
    flush(acc)
    final = [out / f"{name}.pmt"] if len(files) == 1 else [out / f"{name}-{i + 1:02d}.pmt" for i in range(len(files))]
    done = []
    for (path, n, digest, counts), dst in zip(files, final):
        path.replace(dst)
        done.append((dst, n, digest, counts))
        log(f"wrote {dst.name}: {n} B, tiles per level {counts}")
    return done


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extract")
    ap.add_argument("out_dir")
    ap.add_argument("--name", default=None, help="file name stem (default: the extract's name)")
    ap.add_argument("--only", choices=("symbols", "streets"), default=None, help="build one of the two files")
    ap.add_argument("--road-fuel", action="store_true",
                    help="also write the filling stations on roads, as symbol class 4 (no display draws it yet)")
    ap.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--max-file-bytes", type=int, default=MAX_FILE_BYTES)
    ap.add_argument("--high-priority", action="store_true", help="run at high priority (every worker process too)")
    a = ap.parse_args(argv[1:])
    if a.high_priority:
        os.environ["PWCA_MAPS_PRIORITY"] = "high"
        osmpbf.apply_priority()
    os.environ["PWCA_EXTRAS_ONLY"] = a.only or ""
    os.environ["PWCA_EXTRAS_ROAD_FUEL"] = "1" if a.road_fuel else "0"
    do_symbols, do_streets = a.only != "streets", a.only != "symbols"
    src, out = Path(a.extract), Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "")
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)

    phases = [("points", 0.10), ("ways", 0.35), ("nodes", 0.10), ("runs", 0.30), ("tiles", 0.15)]
    label = {"points": "reading tagged points", "ways": "reading ways", "nodes": "reading the ways' points",
             "runs": "cutting straight runs", "tiles": "writing tiles and files"}
    shown = {"pct": -1}

    def progress(phase, fraction):
        k = [n for n, _ in phases].index(phase)
        pct = int(100 * (sum(w for _, w in phases[:k]) + phases[k][1] * max(0.0, min(1.0, fraction))))
        if pct != shown["pct"]:
            shown["pct"] = pct
            log(f"PROGRESS {pct}% of this build ({label[phase]} {int(100 * fraction)}%)")

    def passing(phase):
        osmpbf.PROGRESS = lambda done, total: progress(phase, done / total)

    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    commit = git_commit()

    # 1. Symbols mapped as points.
    points = []  # (class, lat_e7, lon_e7, name, uid); uid = 2 * node id, or 2 * way id + 1
    if do_symbols:
        passing("points")
        for nid, la, lo, classes, nm in osmpbf.map_blocks(src, _symbol_nodes_in, (a.road_fuel,), a.processes):
            points.extend((c, la, lo, nm, 2 * nid) for c in classes)
        log(f"symbols mapped as points: {len(points)}")
    progress("points", 1.0)

    # 2. The ways: named roads, and symbols mapped as lines or areas.
    passing("ways")
    ws = osmpbf.ways_parallel(src, extras_way, None, a.processes)
    groups, sym_ways, n_road_ways = {}, [], 0
    for i, (wid, kind, refs) in enumerate(ws):
        ws[i] = None
        if kind[0] == "r":
            groups.setdefault((kind[1], kind[2]), []).append(refs)
            n_road_ways += 1
        else:
            sym_ways.append((wid, kind[1], kind[2], refs))
    del ws
    log(f"ways: {n_road_ways} named road ways with {len(groups)} names by class; {len(sym_ways)} symbols mapped as "
        f"lines or areas")
    parts = [r for g in groups.values() for r in g] + [r for *_, r in sym_ways]
    needed = np.unique(np.concatenate(parts)) if parts else np.zeros(0, np.int64)
    del parts
    passing("nodes")
    lat, lon, _cells = osmpbf.nodes_e7_parallel(src, needed, a.processes)
    osmpbf.PROGRESS = None
    missing = np.iinfo(np.int32).min
    log(f"points of the ways: {needed.size} needed, {int((lat == missing).sum())} missing")

    def coords(refs):
        """(lat, lon) in degrees, without missing points."""
        idx = np.searchsorted(needed, refs)
        la, lo = lat[idx], lon[idx]
        ok = la != missing
        return la[ok] / 1e7, lo[ok] / 1e7

    for wid, classes, nm, refs in sym_ways:
        la, lo = coords(refs)
        if not la.size:
            continue
        if la.size >= 4 and refs[0] == refs[-1]:
            pla, plo = label_point([(la[:-1], lo[:-1])])
        else:
            pla, plo = float(la[la.size // 2]), float(lo[lo.size // 2])
        points.extend((c, int(round(pla * 1e7)), int(round(plo * 1e7)), nm, 2 * wid + 1) for c in classes)

    files, info_files = [], {}
    # 3. Street names: each name's ways joined end to end, cut into straight runs, the runs cut at the tile edges.
    if do_streets:
        names, stores = [], [RunStore() for _ in STREET_LEVELS]
        stats = {"chains": 0, "runs_too_short": 0}
        total, done = max(1, n_road_ways), 0
        for (cls, nm), way_refs in sorted(groups.items()):
            name_id = len(names)
            names.append(nm)
            chars = len(nm.decode("utf-8"))
            for chain in join_lines(way_refs):
                la, lo = coords(chain)
                if la.size < 2:
                    continue
                stats["chains"] += 1
                x, y = pg.world_xy(lo, la, STREET_WORLD_BITS)
                upm = pg.units_per_metre(float(la[la.size // 2]), STREET_WORLD_BITS)
                for store, (tz, _zmin, _zmax, bits, classes, tol_m, per_char_m) in zip(stores, STREET_LEVELS):
                    if cls not in classes:
                        continue
                    shortest, side, limit = chars * per_char_m * upm, 1 << bits, (1 << tz) - 1
                    for i, j in straight_runs(x, y, tol_m * upm):
                        x0, y0, x1, y1 = float(x[i]), float(y[i]), float(x[j]), float(y[j])
                        if math.hypot(x1 - x0, y1 - y0) < shortest:
                            stats["runs_too_short"] += 1
                            continue
                        for tx, ty, ax, ay, bx, by, length in split_at_tiles(x0, y0, x1, y1, side):
                            if length < shortest:
                                stats["runs_too_short"] += 1
                            elif 0 <= tx <= limit and 0 <= ty <= limit:
                                store.add(tx, ty, cls, length, name_id, ax, ay, bx, by)
            done += len(way_refs)
            progress("runs", done / total)
        del groups
        log(f"street names: {stats['chains']} joined lines, runs kept per level {[len(s) for s in stores]}, "
            f"{stats['runs_too_short']} runs shorter than their name left out")
        progress("tiles", 0.0)
        per_level, level_stats, used = [], [], set()
        for store, level in zip(stores, STREET_LEVELS):
            tiles, grid, st, u = street_level_tiles(store, names, level)
            per_level.append((tiles, grid))
            level_stats.append(st)
            used |= u
        over = sum(st["over_budget"] for st in level_stats)
        specs = [(tz, zmin, zmax, bits, int(tol * 10)) for tz, zmin, zmax, bits, _c, tol, _m in STREET_LEVELS]
        for path, n, digest, counts in write_layer(out, f"{name}-streets", pmt.KIND_STREETS, "osm-street-names",
                                                   specs, per_level, stamp, commit, a.max_file_bytes, log):
            files.append(path)
            info_files[path.name] = {
                "bytes": n, "sha256": digest, "tiles_per_level": counts,
                "format": "PMT 1.3 (FORMAT-1.3-SYMBOLS-STREET-NAMES.md): street names, layer_kind 8",
                "named_road_ways": n_road_ways, "names": len(used),
                "runs_per_level": [st["runs"] for st in level_stats],
                "runs_by_road_class_per_level": [dict(sorted(st["by_class"].items())) for st in level_stats],
                "runs_shorter_than_their_name_left_out": stats["runs_too_short"],
                "runs_over_a_tiles_budget_left_out": over}
    progress("tiles", 0.5)

    # 4. Symbols.
    if do_symbols:
        n_all = len(points)
        points, merged = merge_symbols(points)
        per_class = {}
        for p in points:
            per_class[p[0]] = per_class.get(p[0], 0) + 1
        by_name = {CLASS_NAMES[c]: n for c, n in sorted(per_class.items())}
        log(f"symbols: {len(points)} by class {by_name} ({n_all} mapped, {merged} merged with one nearer than "
            f"{SYMBOL_MERGE_M:.0f} m); {sum(1 for p in points if p[3])} with a name")
        per_level, dropped = [], 0
        for level in SYMBOL_LEVELS:
            tiles, grid, d = symbol_level_tiles(points, level)
            per_level.append((tiles, grid))
            dropped += d
        specs = [(tz, zmin, zmax, bits, 0) for tz, zmin, zmax, bits, _c in SYMBOL_LEVELS]
        for path, n, digest, counts in write_layer(out, f"{name}-symbols", pmt.KIND_SYMBOLS, "osm-symbols", specs,
                                                   per_level, stamp, commit, a.max_file_bytes, log):
            files.append(path)
            info_files[path.name] = {
                "bytes": n, "sha256": digest, "tiles_per_level": counts,
                "format": "PMT 1.3 (FORMAT-1.3-SYMBOLS-STREET-NAMES.md): symbols, layer_kind 7",
                "symbols": len(points), "symbols_by_class": by_name,
                "symbols_with_a_name": sum(1 for p in points if p[3]),
                "merged_with_a_near_one": merged, "over_a_tiles_budget_left_out": dropped,
                "road_fuel_written": bool(a.road_fuel)}
    progress("tiles", 0.9)

    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8", newline="\n")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8", newline="\n")
    info = {
        "format": "PMT 1.3 (FORMAT-1.3-SYMBOLS-STREET-NAMES.md): symbols (layer_kind 7), street names (layer_kind 8)",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                     "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                     "writing_program": head["writing_program"], "extract_source": head["source"]}],
        "files": info_files,
    }
    (out / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    progress("tiles", 1.0)
    log(f"done: {len(files)} file(s), {sum(p.stat().st_size for p in files) / 1e6:.2f} MB in {out}; data as of "
        f"{time.strftime('%Y-%m-%d', time.gmtime(stamp))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
