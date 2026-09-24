"""Measure how big the water layers of an OpenStreetMap extract become in the planned map-pack encoding.

Usage: python tools/measure_water.py <extract.osm.pbf> [--json out.json]

What it counts (see README.md, "Pack format"):
- water areas: natural=water, waterway=riverbank, landuse=reservoir (ways, and multipolygon relations' member ways);
- coastline ways (natural=coastline) and river/canal centre lines (waterway=river|canal);
- closed water ways under 1 ha are dropped (not rideable);
- per level, Douglas-Peucker simplification at the level's tolerance (L1 40 m, L2 10 m, L3 4 m, in local ground
  metres), then quantisation to z12 Web Mercator tiles (L1 1024, L2 4096, L3 65536 units per tile side) and zigzag
  delta varint encoding, which is what the pack stores. Tile clipping overhead is not included (a few percent).

Standard library + numpy only: a minimal streaming PBF reader (zlib blobs, protobuf fields, vectorised varints).
"""

import json
import math
import struct
import sys
import time
import zlib

import numpy as np

LEVELS = [("L1", 40.0, 1024), ("L2", 10.0, 4096), ("L3", 4.0, 65536)]
MIN_AREA_M2 = 10_000.0
EARTH_R = 6_378_137.0
AREA_TAGS = {("natural", "water"), ("waterway", "riverbank"), ("landuse", "reservoir")}
LINE_TAGS = {("waterway", "river"), ("waterway", "canal")}
COAST_TAG = ("natural", "coastline")


# ---- protobuf / PBF ------------------------------------------------------------------------------------------------

def read_varint(buf, pos):
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, pos
        shift += 7


def fields(buf):
    pos, n = 0, len(buf)
    while pos < n:
        key, pos = read_varint(buf, pos)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, pos = read_varint(buf, pos)
        elif wt == 2:
            ln, pos = read_varint(buf, pos)
            v = buf[pos:pos + ln]
            pos += ln
        elif wt == 1:
            v = buf[pos:pos + 8]
            pos += 8
        elif wt == 5:
            v = buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"wire type {wt}")
        yield fn, v


def varints(b):
    a = np.frombuffer(b, dtype=np.uint8)
    if a.size == 0:
        return np.zeros(0, np.uint64)
    ends = np.flatnonzero(a < 0x80)
    starts = np.concatenate(([0], ends[:-1] + 1))
    grp = np.repeat(np.arange(ends.size), ends - starts + 1)
    k = (np.arange(a.size) - starts[grp]).astype(np.uint64)
    return np.add.reduceat((a & 0x7F).astype(np.uint64) << (np.uint64(7) * k), starts)


def zigzag(v):
    return (v >> np.uint64(1)).astype(np.int64) ^ -((v & np.uint64(1)).astype(np.int64))


def blocks(path):
    with open(path, "rb") as f:
        while True:
            h = f.read(4)
            if len(h) < 4:
                return
            hdr = f.read(struct.unpack(">I", h)[0])
            btype, size = None, 0
            for fn, v in fields(hdr):
                if fn == 1:
                    btype = bytes(v).decode()
                elif fn == 3:
                    size = v
            blob = f.read(size)
            data = None
            for fn, v in fields(blob):
                if fn == 1:
                    data = bytes(v)
                elif fn == 3:
                    data = zlib.decompress(v)
            if btype == "OSMData" and data is not None:
                yield data


def primitive_block(data):
    strings, groups, gran, lat_off, lon_off = [], [], 100, 0, 0
    for fn, v in fields(data):
        if fn == 1:
            strings = [bytes(s).decode("utf-8", "replace") for f2, s in fields(v) if f2 == 1]
        elif fn == 2:
            groups.append(v)
        elif fn == 17:
            gran = v
        elif fn == 19:
            lat_off = v
        elif fn == 20:
            lon_off = v
    return strings, groups, gran, lat_off, lon_off


def tags_of(keys, vals, strings):
    return {(strings[int(k)], strings[int(v)]) for k, v in zip(keys, vals)}


# ---- passes --------------------------------------------------------------------------------------------------------

def pass_relations(path):
    """Member way ids of water multipolygon relations."""
    members = []
    for data in blocks(path):
        strings, groups, *_ = primitive_block(data)
        for g in groups:
            for fn, rel in fields(g):
                if fn != 4:
                    continue
                keys = vals = memids = types = np.zeros(0, np.uint64)
                for f2, v in fields(rel):
                    if f2 == 2:
                        keys = varints(v)
                    elif f2 == 3:
                        vals = varints(v)
                    elif f2 == 9:
                        memids = np.cumsum(zigzag(varints(v)))
                    elif f2 == 10:
                        types = varints(v)
                t = tags_of(keys, vals, strings)
                if ("type", "multipolygon") in t and t & AREA_TAGS:
                    members.append(memids[types == 1])
    return np.unique(np.concatenate(members)) if members else np.zeros(0, np.int64)


def pass_ways(path, member_ways):
    """Selected ways: (category, refs). Categories: area, member, coast, line."""
    ways = []
    for data in blocks(path):
        strings, groups, *_ = primitive_block(data)
        for g in groups:
            for fn, w in fields(g):
                if fn != 3:
                    continue
                wid, keys, vals, refs = 0, np.zeros(0, np.uint64), np.zeros(0, np.uint64), None
                for f2, v in fields(w):
                    if f2 == 1:
                        wid = v
                    elif f2 == 2:
                        keys = varints(v)
                    elif f2 == 3:
                        vals = varints(v)
                    elif f2 == 8:
                        refs = v
                t = tags_of(keys, vals, strings)
                if t & AREA_TAGS:
                    cat = "area"
                elif COAST_TAG in t:
                    cat = "coast"
                elif t & LINE_TAGS:
                    cat = "line"
                else:
                    i = np.searchsorted(member_ways, wid)
                    if i < member_ways.size and member_ways[i] == wid:
                        cat = "member"
                    else:
                        continue
                if refs is not None:
                    ways.append((cat, np.cumsum(zigzag(varints(refs)))))
    return ways


def pass_nodes(path, needed):
    lat = np.full(needed.size, np.nan)
    lon = np.full(needed.size, np.nan)
    for data in blocks(path):
        strings, groups, gran, lat_off, lon_off = primitive_block(data)
        for g in groups:
            for fn, dense in fields(g):
                if fn != 2:
                    continue
                ids = la = lo = None
                for f2, v in fields(dense):
                    if f2 == 1:
                        ids = np.cumsum(zigzag(varints(v)))
                    elif f2 == 8:
                        la = np.cumsum(zigzag(varints(v)))
                    elif f2 == 9:
                        lo = np.cumsum(zigzag(varints(v)))
                if ids is None:
                    continue
                pos = np.searchsorted(needed, ids)
                pos[pos >= needed.size] = 0
                hit = needed[pos] == ids
                lat[pos[hit]] = 1e-9 * (lat_off + gran * la[hit])
                lon[pos[hit]] = 1e-9 * (lon_off + gran * lo[hit])
    return lat, lon


# ---- geometry ------------------------------------------------------------------------------------------------------

def douglas_peucker(x, y, tol):
    n = x.size
    if n <= 2:
        return np.ones(n, bool)
    keep = np.zeros(n, bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        dx, dy = x[b] - x[a], y[b] - y[a]
        seg = math.hypot(dx, dy)
        px, py = x[a + 1:b] - x[a], y[a + 1:b] - y[a]
        d = np.abs(px * dy - py * dx) / seg if seg > 0 else np.hypot(px, py)
        i = int(np.argmax(d))
        if d[i] > tol:
            m = a + 1 + i
            keep[m] = True
            stack.append((a, m))
            stack.append((m, b))
    return keep


def varint_bytes(v):
    v = np.maximum(v, 1)
    return (np.floor(np.log2(v.astype(np.float64)) / 7).astype(np.int64) + 1).sum()


def encoded_size(lat, lon, units_per_tile):
    scale = 4096 * units_per_tile  # z12: 4096 tiles per axis
    xq = np.floor((lon + 180.0) / 360.0 * scale).astype(np.int64)
    s = np.sin(np.radians(np.clip(lat, -85.05, 85.05)))
    yq = np.floor((0.5 - np.log((1 + s) / (1 - s)) / (4 * math.pi)) * scale).astype(np.int64)
    dx, dy = np.diff(xq, prepend=0), np.diff(yq, prepend=0)
    zz = lambda d: ((d << 1) ^ (d >> 63)).astype(np.uint64)
    return int(varint_bytes(zz(dx)) + varint_bytes(zz(dy))) + 3  # + ring header (count, flags)


def main():
    path = sys.argv[1]
    out_json = sys.argv[sys.argv.index("--json") + 1] if "--json" in sys.argv else None
    t0 = time.time()
    members = pass_relations(path)
    ways = pass_ways(path, members)
    needed = np.unique(np.concatenate([r for _, r in ways])) if ways else np.zeros(0, np.int64)
    lat, lon = pass_nodes(path, needed)
    t_read = time.time() - t0

    stats = {"file": path, "ways": {}, "raw_vertices": 0, "dropped_small": 0, "missing_nodes": 0,
             "levels": {name: {"vertices": 0, "bytes": 0} for name, _, _ in LEVELS}}
    for cat, refs in ways:
        idx = np.searchsorted(needed, refs)
        la, lo = lat[idx], lon[idx]
        ok = ~np.isnan(la)
        if not ok.all():
            stats["missing_nodes"] += int((~ok).sum())
            la, lo = la[ok], lo[ok]
        if la.size < 2:
            continue
        lat0 = math.radians(float(la.mean()))
        x = np.radians(lo) * EARTH_R * math.cos(lat0)
        y = np.radians(la) * EARTH_R
        if cat == "area" and refs[0] == refs[-1]:
            area = 0.5 * abs(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1]))
            if area < MIN_AREA_M2:
                stats["dropped_small"] += 1
                continue
        stats["ways"][cat] = stats["ways"].get(cat, 0) + 1
        stats["raw_vertices"] += int(la.size)
        for name, tol, units in LEVELS:
            keep = douglas_peucker(x, y, tol)
            stats["levels"][name]["vertices"] += int(keep.sum())
            stats["levels"][name]["bytes"] += encoded_size(la[keep], lo[keep], units)
    stats["seconds"] = round(time.time() - t0, 1)
    stats["read_seconds"] = round(t_read, 1)
    total = sum(l["bytes"] for l in stats["levels"].values())
    stats["total_bytes"] = total
    print(json.dumps(stats, indent=2))
    if out_json:
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)


if __name__ == "__main__":
    main()
