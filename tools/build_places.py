"""Builds the PLACES layer of a pack (PMT 1.1, layer_kind 5, FORMAT.md 7.5) from an OpenStreetMap extract: one point
with its name for every city, town, village and hamlet.

Usage: python tools/build_places.py <extract.osm.pbf> <out_dir> [--name N] [--processes N]

Writes <out_dir>/<name>.pmt (default name: the extract's name + "-places"), LICENSE.txt, ATTRIBUTION.txt, SOURCES.json
and MANIFEST.sha256, like build_pack.py. One pass over the extract's nodes; North America takes a few minutes.

A place is an OSM node with `place` = city, town, village or hamlet and a name. The name written is the node's `name`
when a display can draw it (the characters in DISPLAY_CHARS below: Latin with its accents), else its `name:en` under
the same rule, else the place is left out and counted. Names are 1 to 127 bytes of UTF-8 (FORMAT.md section 6).
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

import osmpbf
import packgeom as pg
import pmt
from build_pack import ATTRIBUTION_TEXT, CREDIT, LICENSE_TEXT, git_commit, sha256

CLASSES = {"city": 1, "town": 2, "village": 3, "hamlet": 4}
# tile_zoom, zoom_min, zoom_max, coord_bits, classes. The zooms are the roads layer's; a class appears from the zoom
# Branding's style labels it at (city 8, town 10, village 12, hamlet 14). No buffer: a place is in exactly one tile, so
# a display that gathers the tiles in view never meets the same place twice.
LEVELS = [
    (8, 8, 9, 12, {1}),
    (10, 10, 11, 12, {1, 2}),
    (12, 12, 13, 12, {1, 2, 3}),
    (12, 14, 16, 16, {1, 2, 3, 4}),
]
TILE_FEATURE_BUDGET = 12_000  # as build_pack.py: under the display's 16,384 features per tile, with room


# The Hawaiian okina (U+02BB) and the modifier apostrophe of First Nations names (U+02BC) are not in the display's
# font (Jost): the left and right single quotes are written in their place (Branding, 2026-10-04), so those names are
# drawn rather than left out.
STAND_INS = str.maketrans({"\u02bb": "\u2018", "\u02bc": "\u2019"})


def display_char(ch):
    """Whether a display's label font is expected to hold this character: printable Basic Latin, Latin-1 Supplement,
    Latin Extended-A and -B, the curly quotes and the en and em dash. (The display checks every character against its
    real font again and skips a label it cannot draw.)"""
    c = ord(ch)
    if 0x20 <= c <= 0x7E or 0xA0 <= c <= 0x24F:
        return unicodedata.category(ch) != "Cc"
    return c in (0x2018, 0x2019, 0x2013, 0x2014)


def label_name(text):
    """The bytes of a name as a label, or None: stripped, one space between words, composed (NFC), the stand-ins
    written, every character a display character, 1 to pmt.NAME_MAX bytes."""
    if not text:
        return None
    text = unicodedata.normalize("NFC", " ".join(text.split())).translate(STAND_INS)
    if not text or not all(display_char(ch) for ch in text):
        return None
    raw = text.encode("utf-8")
    return raw if pmt.name_ok(raw) else None


def place_name(tags):
    """The label of a place node: its `name`, else its `name:en`; None when neither can be drawn."""
    return label_name(tags.get("name")) or label_name(tags.get("name:en"))


def want(tags):
    return tags.get("place") in CLASSES and bool(tags.get("name") or tags.get("name:en"))


def level_tiles(places, level):
    """{(tx, ty): [point features]} of one level, and its coverage grid cells. places: [(class, lat_e7, lon_e7, name
    bytes, node id)]. A tile's features are in class order, then by node id (the same bytes on every run)."""
    tz, _zmin, _zmax, bits, classes = level
    side = 1 << bits
    limit = (1 << tz) - 1
    tiles = {}
    for cls, la, lo, name, nid in places:
        if cls not in classes:
            continue
        x, y = pg.world_xy(lo / 1e7, la / 1e7, tz + bits)
        x, y = int(np.floor(x)), int(np.floor(y))
        tx, ty = x >> bits, y >> bits
        if not (0 <= tx <= limit and 0 <= ty <= limit):
            continue
        tiles.setdefault((tx, ty), []).append((cls, nid, x - tx * side, y - ty * side, name))
    out, grid, dropped = {}, {}, 0
    for key, items in tiles.items():
        items.sort(key=lambda it: (it[0], it[1]))
        if len(items) > TILE_FEATURE_BUDGET:  # the lowest classes go first
            dropped += len(items) - TILE_FEATURE_BUDGET
            items = items[:TILE_FEATURE_BUDGET]
        out[key] = [(pmt.POINT, cls, None, (x, y, name)) for cls, _nid, x, y, name in items]
        grid[(key[0] >> (tz - 8), key[1] >> (tz - 8))] = 1
    return out, grid, dropped


def collect(rows):
    """[(class, lat_e7, lon_e7, name bytes, node id)] from the extract's place nodes, and what was left out."""
    places, stats = [], {"no_drawable_name": 0, "used_name_en": 0}
    for nid, la, lo, tags in rows:
        name = label_name(tags.get("name"))
        if name is None:
            name = label_name(tags.get("name:en"))
            if name is None:
                stats["no_drawable_name"] += 1
                continue
            stats["used_name_en"] += 1
        places.append((CLASSES[tags["place"]], la, lo, name, nid))
    places.sort(key=lambda p: (p[0], p[4]))
    return places, stats


def build(places, stamp, commit):
    """The bytes of the places file, and per level (tiles, features)."""
    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    strings = ["osm-places", CREDIT, "ODbL 1.0", "OpenStreetMap", date, f"pwca_maps {commit}"]
    levels, counts, dropped = [], [], 0
    for level in LEVELS:
        tz, zmin, zmax, bits, _classes = level
        tiles, grid, d = level_tiles(places, level)
        dropped += d
        levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": 0,
                       "tolerance_dm": 0, "full_class": 0, "grid": grid, "tiles": tiles})
        counts.append((len(tiles), sum(len(f) for f in tiles.values())))
    data = pmt.build_pmt(layer_kind=pmt.KIND_PLACES, levels=levels, strings=strings,
                         ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()), data_time=stamp)
    return data, counts, dropped


# ---- the full names layer (--full; FORMAT.md 7.6) -------------------------------------------------------------------
# More classes: a class is a kind of name and how much it matters, so a display can rank the names of many tiles.
CITY_1M, CITY_250K, STATE = 6, 7, 5
WATER0 = 16  # water areas (lake, reservoir, bay), 16 the largest to 22 the smallest
RIVER0 = 24  # waterways, 24 a river that fills a zoom-8 tile, 25 a zoom-10 tile, 26 other rivers, 27 canal, 28 stream
ISLAND, ISLET = 32, 33
# tile_zoom, zoom_min, zoom_max, coord_bits: the full lines pack's levels.
FULL_LEVELS = [(4, 4, 5, 12), (6, 6, 7, 12), (8, 8, 9, 12), (10, 10, 11, 12), (12, 12, 13, 12), (12, 14, 14, 16),
               (14, 15, 16, 14)]
ZOOM_OF = {1: 8, 2: 10, 3: 12, 4: 14, CITY_1M: 4, CITY_250K: 6, STATE: 4, ISLAND: 12, ISLET: 14}
STATE_ZOOM_MAX = 9  # a state's name is for the views that show much of the state
WATER_TIERS = [(2e10, 4), (1e9, 6), (5e7, 8), (5e6, 10), (2.5e5, 12), (2e4, 14), (0.0, 15)]  # (from m2, first zoom)
BAY_NODE_TIER = 4  # a bay mapped as a point has no size: named from zoom 12
RIVER_FILL = 0.6  # a river is named in a tile cut at zoom 8 or 10 when its length there is this much of the tile's width
# The order of the names in a tile: what a display with room for a few names should take first.
ORDER = [CITY_1M, STATE, CITY_250K, 16, 1, 17, 18, 2, 24, 19, 25, 3, 20, ISLAND, 26, 21, 4, 27, ISLET, 22, 28]
RANK = {c: i for i, c in enumerate(ORDER)}
EARTH_CIRC = 40_075_016.686


def group_of(cls):
    """Names of the same group and text in one tile are one name (a lake mapped twice, a river's many ways)."""
    return 0 if cls in (1, 2, 3, 4, CITY_1M, CITY_250K) else 1 if cls == STATE else 2 if cls < RIVER0 else \
        3 if cls < ISLAND else 4


def population(tags):
    try:
        return int(float(tags.get("population", "0").replace(",", "").replace(" ", "")))
    except ValueError:
        return 0


def want_full(tags):
    return (tags.get("place") in CLASSES or tags.get("place") in ("state", "province", "island", "islet")) \
        and bool(tags.get("name") or tags.get("name:en"))


def want_bay(tags):
    return tags.get("natural") in ("bay", "strait") and bool(tags.get("name") or tags.get("name:en"))


def collect_full(place_rows, bay_rows, names_path):
    """(points, lines, stats). points: [(class, lat_e7, lon_e7, name, id, zoom_min, zoom_max, size)]; lines: the named
    waterways [(osm line class, lat_e7, lon_e7, name, id, metres)], placed tile by tile in level_tiles_full."""
    points, lines, stats = [], [], {"no_drawable_name": 0}

    def named(tags_or_text):
        name = place_name(tags_or_text) if isinstance(tags_or_text, dict) else label_name(tags_or_text)
        if name is None:
            stats["no_drawable_name"] += 1
        return name

    for nid, la, lo, tags in place_rows:
        name = named(tags)
        if name is None:
            continue
        place, pop = tags["place"], population(tags)
        if place in CLASSES:
            cls = CLASSES[place]
            if cls == 1 and pop >= 250_000:
                cls = CITY_1M if pop >= 1_000_000 else CITY_250K
        else:
            cls = STATE if place in ("state", "province") else ISLAND if place == "island" else ISLET
        points.append((cls, la, lo, name, nid, ZOOM_OF[cls], STATE_ZOOM_MAX if cls == STATE else 99, pop))
    for nid, la, lo, tags in bay_rows:
        name = named(tags)
        if name is not None:
            points.append((WATER0 + BAY_NODE_TIER, la, lo, name, nid, WATER_TIERS[BAY_NODE_TIER][1], 99, 0))
    if names_path:
        with open(names_path, encoding="utf-8") as f:
            for n, row in enumerate(f):
                typ, cls, size, la, lo, text = row.rstrip("\n").split("\t", 5)
                cls, size, la, lo, uid = int(cls), float(size), int(la), int(lo), (1 << 40) + n
                if typ == "L" and cls not in (16, 17, 18):
                    continue  # drains and ditches: not named
                name = named(text)
                if name is None:
                    continue
                if typ == "L":
                    lines.append((cls, la, lo, name, uid, size))
                else:
                    tier = next(i for i, (m2, _z) in enumerate(WATER_TIERS) if size >= m2)
                    points.append((WATER0 + tier, la, lo, name, uid, WATER_TIERS[tier][1], 99, size))
    return points, lines, stats


def level_tiles_full(points, lines, level, river_tier):
    """{(tx, ty): [point features]} of one full level. river_tier: {line id: its best tier so far}, filled from the
    zoomed-out levels down, so a river named on a wide view keeps its rank on the close ones."""
    tz, zmin, _zmax, bits = level
    side, limit = 1 << bits, (1 << tz) - 1
    tiles = {}

    def put(cls, la, lo, name, uid, size):
        x, y = pg.world_xy(lo / 1e7, la / 1e7, tz + bits)
        x, y = int(np.floor(x)), int(np.floor(y))
        tx, ty = x >> bits, y >> bits
        if 0 <= tx <= limit and 0 <= ty <= limit:
            tiles.setdefault((tx, ty), []).append((cls, uid, x - tx * side, y - ty * side, name, size))
        return tx, ty

    for cls, la, lo, name, uid, z0, z1, size in points:
        if z0 <= zmin <= z1:
            put(cls, la, lo, name, uid, size)
    # Waterways: the ways of one name in one tile are one name, at the middle of the longest of them.
    groups = {}
    for i, (cls, la, lo, name, uid, metres) in enumerate(lines):
        if (cls == 17 and zmin < 14) or (cls == 18 and zmin < 15):
            continue
        x, y = pg.world_xy(lo / 1e7, la / 1e7, tz)
        groups.setdefault((int(np.floor(x)), int(np.floor(y)), cls, name), []).append(i)
    for (_tx, ty, cls, name), members in groups.items():
        if cls == 16 and tz < 8:
            continue  # no river names on the whole-country views
        total = sum(lines[i][5] for i in members)
        if cls == 16:
            if tz <= 10:
                lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 0.5) / (1 << tz)))))
                if total < RIVER_FILL * EARTH_CIRC * math.cos(math.radians(lat)) / (1 << tz):
                    continue
                for i in members:
                    river_tier[lines[i][4]] = min(river_tier.get(lines[i][4], 2), 0 if tz == 8 else 1)
            out_cls = RIVER0 + min(river_tier.get(lines[i][4], 2) for i in members)
        else:
            out_cls = RIVER0 + (3 if cls == 17 else 4)
        best = max(members, key=lambda i: lines[i][5])
        put(out_cls, lines[best][1], lines[best][2], name, lines[best][4], total)
    out, grid, dropped = {}, {}, 0
    for key, items in tiles.items():
        items.sort(key=lambda it: (RANK[it[0]], -it[5], it[1]))
        seen, kept = set(), []
        for it in items:
            k = (group_of(it[0]), it[4])
            if k not in seen:
                seen.add(k)
                kept.append(it)
        if len(kept) > TILE_FEATURE_BUDGET:
            dropped += len(kept) - TILE_FEATURE_BUDGET
            kept = kept[:TILE_FEATURE_BUDGET]
        out[key] = [(pmt.POINT, cls, None, (x, y, name)) for cls, _uid, x, y, name, _size in kept]
        if tz >= 8:
            grid[(key[0] >> (tz - 8), key[1] >> (tz - 8))] = 1
    return out, grid, dropped


def build_full(points, lines, stamp, commit):
    """The bytes of the full names file, and per level (tiles, features)."""
    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    strings = ["osm-places", CREDIT, "ODbL 1.0", "OpenStreetMap", date, f"pwca_maps {commit}"]
    levels, counts, dropped, river_tier = [], [], 0, {}
    for level in FULL_LEVELS:
        tz, zmin, zmax, bits = level
        tiles, grid, d = level_tiles_full(points, lines, level, river_tier)
        dropped += d
        levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": 0,
                       "tolerance_dm": 0, "full_class": 0, "grid": grid if tz >= 8 else None, "tiles": tiles})
        counts.append((len(tiles), sum(len(f) for f in tiles.values())))
    data = pmt.build_pmt(layer_kind=pmt.KIND_PLACES, levels=levels, strings=strings,
                         ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()), data_time=stamp)
    return data, counts, dropped


def main_full(a):
    src, out = Path(a.extract), Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "") + "-places"
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)
    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    place_rows = osmpbf.tagged_nodes_parallel(src, "place", want_full, a.processes)
    log(f"place nodes: {len(place_rows)}")
    bay_rows = osmpbf.tagged_nodes_parallel(src, "natural", want_bay, a.processes)
    log(f"bay and strait nodes: {len(bay_rows)}")
    points, lines, stats = collect_full(place_rows, bay_rows, a.names)
    per = {}
    for pt in points:
        per[pt[0]] = per.get(pt[0], 0) + 1
    log(f"names: {len(points)} points by class {dict(sorted(per.items()))}, {len(lines)} named waterway ways; "
        f"{stats['no_drawable_name']} left out (no name a display can draw)")
    commit = git_commit()
    data, counts, dropped = build_full(points, lines, stamp, commit)
    path = out / f"{name}.pmt"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    log(f"wrote {path.name}: {len(data)} B, tiles and names per level {counts}"
        + (f"; {dropped} names over a tile's budget left out" if dropped else ""))
    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8", newline="\n")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8", newline="\n")
    info = {
        "format": "PMT 1.1 (FORMAT.md 7.6): the full names layer, layer_kind 5",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                     "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                     "writing_program": head["writing_program"], "extract_source": head["source"]}],
        "files": {path.name: {"bytes": len(data), "sha256": digest, "tiles_per_level": [t for t, _ in counts],
                              "names_per_level": [n for _, n in counts], "points": len(points),
                              "named_waterway_ways": len(lines),
                              "left_out_no_drawable_name": stats["no_drawable_name"]}},
    }
    (out / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    log(f"done: {path} ({len(data) / 1e6:.2f} MB); data as of {time.strftime('%Y-%m-%d', time.gmtime(stamp))}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extract")
    ap.add_argument("out_dir")
    ap.add_argument("--name", default=None, help="file name without .pmt (default: the extract's name + -places)")
    ap.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--full", action="store_true",
                    help="the full names layer (FORMAT.md 7.6): states, islands, bays, and with --names the lakes and "
                         "rivers; seven levels from zoom 4")
    ap.add_argument("--names", default=None, help="with --full: names.tsv written by build_pack.py --full (water)")
    a = ap.parse_args(argv[1:])
    if a.full:
        return main_full(a)
    src, out = Path(a.extract), Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "") + "-places"
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)

    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    rows = osmpbf.tagged_nodes_parallel(src, "place", want, a.processes)
    places, stats = collect(rows)
    per = {c: sum(1 for p in places if p[0] == c) for c in CLASSES.values()}
    log(f"places: {len(places)} ({', '.join(f'{n} {per[c]}' for n, c in CLASSES.items())}); "
        f"{stats['used_name_en']} with name:en, {stats['no_drawable_name']} left out (no name a display can draw)")
    commit = git_commit()
    data, counts, dropped = build(places, stamp, commit)
    path = out / f"{name}.pmt"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    log(f"wrote {path.name}: {len(data)} B, tiles and places per level {counts}"
        + (f"; {dropped} places over a tile's budget left out" if dropped else ""))

    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8", newline="\n")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8", newline="\n")
    info = {
        "format": "PMT 1.1 (FORMAT.md): places, layer_kind 5",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                     "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                     "writing_program": head["writing_program"], "extract_source": head["source"]}],
        "files": {path.name: {"bytes": len(data), "sha256": digest, "tiles_per_level": [t for t, _ in counts],
                              "places_per_level": [n for _, n in counts], "places": len(places),
                              "left_out_no_drawable_name": stats["no_drawable_name"],
                              "named_from_name_en": stats["used_name_en"]}},
    }
    (out / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    log(f"done: {path} ({len(data) / 1e6:.2f} MB); data as of {time.strftime('%Y-%m-%d', time.gmtime(stamp))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
