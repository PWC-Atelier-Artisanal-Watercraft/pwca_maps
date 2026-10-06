"""Builds the detail water layer (PMT, FORMAT.md section 7.2) from an OpenStreetMap extract.

Usage: python tools/build_pack.py <extract.osm.pbf> <out_dir> [--name NAME] [--sea water-polygons-split-4326.zip]
       [--processes N] [--region-zoom Z] [--max-file-bytes B] [--low-priority] [--layer water|roads|land] [--full]
       [--minor]

--layer roads builds the roads (FORMAT.md 7.4, 7.6), --layer land the land cover (FORMAT.md 7.7: housing, commerce
and industry areas, parking, parks, woods, wetland, beaches) with the same passes and the same tile cutting.

What goes in (README.md "Sources"):
- water areas: natural=water, waterway=riverbank, landuse=reservoir, as closed ways and multipolygon relations;
  intermittent water and waste-water basins are left out; closed areas under 1 ha are dropped;
- river and canal centre lines (waterway=river|canal);
- the sea (class 1), from the osmdata.openstreetmap.de water polygons when --sea is given; an inland extract
  doesn't need it.

Each area is simplified per level (L0 150 m, L1 40 m, L2 10 m, L3 4 m), oriented (outer rings clockwise on screen),
cut into buffered tiles and written as PMT files with four levels, next to SOURCES.json, ATTRIBUTION.txt and
LICENSE.txt. L0 (cut at zoom 8, serving zooms 8 and 9: the display's zoomed-out views, 2026-10-02) also leaves out
inland areas under 25 ha, which are under a few pixels there.
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
# tile_zoom, zoom_min, zoom_max, coord_bits, buffer, tolerance (m), min area of an inland area (m2), line classes kept
# (None: all).
LEVELS = [
    (8, 8, 9, 12, 32, 150.0, 250_000.0, None),  # zoomed out (the owner, 2026-10-02: the whole map, every zoom)
    (10, 10, 11, 12, 32, 40.0, 0.0, None),
    (12, 12, 13, 12, 32, 10.0, 0.0, None),
    (12, 14, 16, 16, 512, 4.0, 0.0, None),
]
# The roads layer (--layer roads; layer_kind 4, FORMAT.md 1.1 section 7.4): lines only, the larger roads from the
# zoomed-out levels, every drivable road from zoom 14 (the owner, 2026-10-02: "I want the entire US map"). The buffer
# of the 12-bit levels is 64 units (4 px where the tile zoom is the display zoom): a display draws each tile inside its
# own square, so a road just outside the square must be in the tile for half its stroke width (up to 2.5 px there).
ROAD_LEVELS = [
    (8, 8, 9, 12, 64, 150.0, 0.0, {1, 2}),
    (10, 10, 11, 12, 64, 40.0, 0.0, {1, 2, 3}),
    (12, 12, 13, 12, 64, 10.0, 0.0, {1, 2, 3, 4, 5}),
    (12, 14, 16, 16, 512, 4.0, 0.0, {1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13}),  # the only level with the links
]
# The FULL packs (--full; the owner, 2026-10-04: "I want as much detail as possible for the USA"; FORMAT.md 7.6).
# Water: every pond and stream, and a fifth level cut at zoom 14 for the close zooms (a display at zoom 16 then reads
# a tile one sixteenth the size of a zoom-12 tile). Line classes: 16 river, 17 canal, 18 stream, 19 drain or ditch.
LEVELS_FULL = [
    (8, 8, 9, 12, 32, 150.0, 250_000.0, {16, 17}),
    (10, 10, 11, 12, 32, 40.0, 10_000.0, {16, 17}),
    (12, 12, 13, 12, 32, 10.0, 2_000.0, {16, 17}),
    (12, 14, 14, 16, 512, 4.0, 400.0, {16, 17, 18}),
    (14, 15, 16, 14, 256, 2.0, 0.0, None),
]
# Lines: the roads, and with them every other line a map shows. Two zoomed-out levels (cut at zoom 4 and 6: motorways
# and borders on the whole-country views), the roads' own levels with railways and borders added, and a last level cut
# at zoom 14 with the taxiways, piers and dams as well. The MINOR ways (service roads, tracks, paths: two thirds of
# all the ways) are a file of their own with that last level only (--minor): a display reads it from zoom 15, and may
# leave it out when it is short of time.
LINE_LEVELS_FULL = [
    (4, 4, 5, 12, 64, 2500.0, 0.0, {1, 24, 25}),
    (6, 6, 7, 12, 64, 600.0, 0.0, {1, 2, 24, 25}),
    (8, 8, 9, 12, 64, 150.0, 0.0, {1, 2, 24, 25}),
    (10, 10, 11, 12, 64, 40.0, 0.0, {1, 2, 3, 17, 24, 25}),
    (12, 12, 13, 12, 64, 10.0, 0.0, {1, 2, 3, 4, 5, 17, 18, 20, 24, 25}),
    (12, 14, 14, 16, 512, 4.0, 0.0, {1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 17, 18, 19, 20, 21, 22, 23, 24, 25}),
    (14, 15, 16, 14, 256, 2.0, 0.0, {1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 17, 18, 19, 20, 21, 22, 23, 24, 25}),
]
# In the store only: the motorways and trunk roads joined into long chains through their junctions, for the two
# levels cut below zoom 8 (written there as classes 1 and 2).
LOW_CHAINS = {1: 101, 2: 102}
LOW_RANK = {25: 100, 24: 99, 1: 60, 2: 50}
MINOR_LEVELS = [(14, 15, 16, 14, 256, 2.0, 0.0, None)]
# Land cover (--layer land; layer_kind 6, PMT 1.2, FORMAT.md 7.7): the full water pack's five levels. A level leaves
# out the areas under its smallest size (a few pixels there); its last field is the AREA classes it keeps (None: all).
LAND_LEVELS = [
    (8, 8, 9, 12, 32, 150.0, 1_000_000.0, {1, 5, 6, 7}),
    (10, 10, 11, 12, 32, 40.0, 100_000.0, {1, 2, 3, 5, 6, 7, 8}),
    (12, 12, 13, 12, 32, 10.0, 10_000.0, {1, 2, 3, 5, 6, 7, 8}),
    (12, 14, 14, 16, 512, 4.0, 1_000.0, None),
    (14, 15, 16, 14, 256, 2.0, 0.0, None),
]
MIN_AREA_LAND_M2 = 100.0  # the land-cover pack keeps every area from 100 m2 (its levels choose by zoom)
NAME_EVERY_M = 1500.0  # names.tsv: a named waterway's label points are this far apart
MIN_AREA_FULL_M2 = 50.0  # a full water pack keeps every water area from 50 m2 (its levels choose by zoom)
MIN_AREA_M2 = 10_000.0
TILE_FEATURE_BUDGET = 12_000  # under the display's 16,384 features or parts per tile, with room (fit_tile)
TILE_POINT_BUDGET = 100_000  # under the display's 131,072 points per tile, with room (fit_tile)
REGION_ZOOM = 7  # regions of z7 tiles (about 300 km): one worker job each
REGION_MARGIN_DEG = 0.05  # features within this of a region are cut for it (the largest tile buffer is < 0.003 deg)
MAX_FILE_BYTES = 2_000_000_000  # under 2 GiB, with room for the header block
INDEX_BYTES = 16  # an index entry: counted with each tile towards MAX_FILE_BYTES
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


# Road classes (the roads layer, FORMAT.md section 7.4). A link (a ramp, a slip road) has its road's class plus
# ROAD_LINK, so a display can draw it thinner than its road or not at all; links are only in the level that serves
# zoom 14 and up (Branding, 2026-10-04: at zooms 12 and 13 two carriageways and their ramps made one broad band).
# Service roads, tracks, paths, footways and roads under construction or proposed are left out.
ROAD_LINK = 8
ROAD_CLASSES = {"motorway": 1, "trunk": 2, "primary": 3, "secondary": 4, "tertiary": 5, "unclassified": 6,
                "residential": 6, "living_street": 6, "motorway_link": 1 + ROAD_LINK, "trunk_link": 2 + ROAD_LINK,
                "primary_link": 3 + ROAD_LINK, "secondary_link": 4 + ROAD_LINK, "tertiary_link": 5 + ROAD_LINK}


def road_kind(tags):
    """(None, road class) of a road way, or None."""
    c = ROAD_CLASSES.get(tags.get("highway"))
    if c is None or tags.get("area") == "yes":
        return None
    return (None, c)


# ---- the full packs' classes (FORMAT.md 7.6) ------------------------------------------------------------------------

def tag_name(tags):
    """A feature's name for a label, as one line of text, or None."""
    n = tags.get("name") or tags.get("name:en")
    return " ".join(n.split()) if n and n.strip() else None


def line_class_full(tags):
    if tags.get("intermittent") == "yes" or tags.get("tunnel") in ("yes", "culvert"):
        return None
    return {"river": 16, "canal": 17, "stream": 18, "drain": 19, "ditch": 19}.get(tags.get("waterway"))


def way_kind_full(tags):
    """(area class, line class, name, bay) of a way for the full water pack, or None. bay: a named bay or strait, which
    is not drawn (the sea or the lake under it is) and only gives a label."""
    a, line = area_class(tags), line_class_full(tags)
    bay = tags.get("natural") in ("bay", "strait")
    if a is None and line is None and not bay:
        return None
    name = tag_name(tags)
    if a is None and line is None and name is None:
        return None
    return (a, line, name, bay)


def water_relation_full(tags):
    if tags.get("type") != "multipolygon":
        return False
    return area_class(tags) is not None or (tags.get("natural") in ("bay", "strait") and tag_name(tags) is not None)


# Other roads and lines. Classes 9 to 13 are the links (a road's class + 8).
LINE_SERVICE, LINE_TRACK, LINE_PATH, LINE_RAIL, LINE_RUNWAY, LINE_TAXIWAY, LINE_FERRY, LINE_PIER, LINE_DAM = (
    7, 8, 16, 17, 18, 19, 20, 21, 22)
LINE_RAIL_MINOR = 23  # a yard, siding or spur, a tram, a light railway: from zoom 14
SEA_BORDERS = ("territorial", "maritime", "eez", "contiguous", "baseline")
LINE_STATE, LINE_COUNTRY = 24, 25
MINOR_ROADS = {"service": LINE_SERVICE, "busway": LINE_SERVICE, "road": 6, "track": LINE_TRACK, "path": LINE_PATH,
               "footway": LINE_PATH, "cycleway": LINE_PATH, "bridleway": LINE_PATH, "pedestrian": LINE_PATH,
               "steps": LINE_PATH}
RAILS = ("rail", "light_rail", "narrow_gauge", "subway", "tram", "monorail", "funicular")
MINOR = {LINE_SERVICE, LINE_TRACK, LINE_PATH}
# Classes whose ways are joined end to end (join_lines); the small classes are stored way by way.
JOINED = set(range(1, 7)) | set(range(9, 14)) | {LINE_RAIL, LINE_RAIL_MINOR, LINE_STATE, LINE_COUNTRY}
SKIP = 0  # a way that is left out even when a border relation holds it (a border out at sea)


def line_kind_full(tags):
    """(None, line class) of a way for the full lines pack, or None. (None, SKIP): a sea border, never drawn."""
    hw = tags.get("highway")
    if hw:
        if tags.get("area") == "yes":
            return None
        c = ROAD_CLASSES.get(hw) or MINOR_ROADS.get(hw)
        if c == LINE_PATH and hw == "footway" and tags.get("footway") in ("sidewalk", "crossing"):
            return None  # a pavement beside a road that is drawn already
        return (None, c) if c and c not in MINOR else None
    rw = tags.get("railway")
    if rw in RAILS:
        if tags.get("tunnel") == "yes":
            return None
        return (None, LINE_RAIL if rw in ("rail", "narrow_gauge") and not tags.get("service") else LINE_RAIL_MINOR)
    aw = tags.get("aeroway")
    if aw in ("runway", "taxiway"):
        return (None, LINE_RUNWAY if aw == "runway" else LINE_TAXIWAY)
    if tags.get("route") == "ferry":
        return (None, LINE_FERRY)
    if tags.get("man_made") in ("pier", "breakwater", "groyne"):
        return (None, LINE_PIER)
    if tags.get("waterway") in ("dam", "weir", "lock_gate"):
        return (None, LINE_DAM)
    if (tags.get("maritime") == "yes" or tags.get("border_type") in SEA_BORDERS
            or tags.get("boundary") == "maritime" or tags.get("natural") == "coastline"):
        return (None, SKIP)
    return None


def line_kind_minor(tags):
    """(None, line class) of a service road, track or path (the minor ways' file), or None."""
    hw = tags.get("highway")
    c = MINOR_ROADS.get(hw) if hw and tags.get("area") != "yes" else None
    if c not in MINOR or (hw == "footway" and tags.get("footway") in ("sidewalk", "crossing")):
        return None
    return (None, c)


def border_relation(tags):
    return tags.get("boundary") == "administrative" and tags.get("admin_level") in ("2", "4")


# ---- land cover (FORMAT.md 7.7) -------------------------------------------------------------------------------------

# The higher class wins where two areas overlap (a wood in a park in a housing area), so the numbers are the order.
LAND_RESIDENTIAL, LAND_COMMERCIAL, LAND_INDUSTRIAL, LAND_PARKING, LAND_PARK, LAND_FOREST, LAND_WETLAND, LAND_BEACH = (
    1, 2, 3, 4, 5, 6, 7, 8)
LAND_LEISURE = ("park", "garden", "golf_course", "pitch", "playground", "recreation_ground", "nature_reserve",
                "dog_park")
LAND_GRASS = ("grass", "meadow", "village_green", "recreation_ground", "cemetery")


def land_class(tags):
    """Polygon class of land-cover tags, or None. Tags that give two classes give the higher one."""
    na, lu = tags.get("natural"), tags.get("landuse")
    if na in ("beach", "sand"):
        return LAND_BEACH
    if na == "wetland":
        return LAND_WETLAND
    if na == "wood" or lu == "forest":
        return LAND_FOREST
    if tags.get("leisure") in LAND_LEISURE or lu in LAND_GRASS:
        return LAND_PARK
    if tags.get("amenity") == "parking":
        return None if tags.get("parking") in ("underground", "multi-storey") else LAND_PARKING
    if lu in ("industrial", "railway"):
        return LAND_INDUSTRIAL
    if lu in ("commercial", "retail"):
        return LAND_COMMERCIAL
    if lu == "residential":
        return LAND_RESIDENTIAL
    return None


def land_kind(tags):
    """(area class, None) of a way for the land-cover pack, or None."""
    c = land_class(tags)
    return None if c is None else (c, None)


def land_relation(tags):
    return tags.get("type") == "multipolygon" and land_class(tags) is not None


def label_point(outer, inners=()):
    """A point inside an area for its label: the middle of the widest stretch of the area along the horizontal line
    through the middle of its largest outer ring. outer: [(lat, lon)] arrays in degrees; returns (lat, lon)."""
    la, lo = max(outer, key=lambda r: area_m2(r[0], r[1]))
    py = (float(la.min()) + float(la.max())) / 2
    xs = []
    for rla, rlo in [(la, lo)] + list(inners):
        y2, x2 = np.roll(rla, -1), np.roll(rlo, -1)
        cross = (rla > py) != (y2 > py)
        if cross.any():
            xs.extend((rlo[cross] + (py - rla[cross]) * (x2[cross] - rlo[cross]) / (y2[cross] - rla[cross])).tolist())
    xs.sort()
    best = None
    for a, b in zip(xs[0::2], xs[1::2]):
        if best is None or b - a > best[1] - best[0]:
            best = (a, b)
    if best is None:
        return float(la.mean()), float(lo.mean())
    return py, (best[0] + best[1]) / 2


def line_length_m(la, lo):
    """Length of a line given in degrees, in metres."""
    k = math.cos(math.radians(float(np.mean(la))))
    return float(np.hypot(np.diff(lo) * k, np.diff(la)).sum()) * math.pi / 180 * EARTH_R


def rank_of(layer):
    return {"water-full": LINE_RANK_WATER, "lines-full": LINE_RANK, "minor-full": LINE_RANK,
            "land-full": LINE_RANK}.get(layer)  # land cover has no lines: the rank only says "a full pack"


def is_full(layer):
    return layer.endswith("-full")


def is_lines(layer):
    return layer in ("roads", "lines-full", "minor-full")


def is_land(layer):
    return layer == "land-full"


def kind_of(layer):
    return pmt.KIND_LAND if is_land(layer) else pmt.KIND_ROADS if is_lines(layer) else pmt.KIND_WATER


def levels_of(layer):
    return {"roads": ROAD_LEVELS, "water-full": LEVELS_FULL, "lines-full": LINE_LEVELS_FULL,
            "minor-full": MINOR_LEVELS, "land-full": LAND_LEVELS}.get(layer, LEVELS)


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


def join_lines(way_refs, greedy=False):
    """Joins open ways (node id arrays) end to end where exactly two of them meet at a node, so a road split at every
    bridge or tag change becomes one line (fewer features per tile, no gaps from a tile's budget). Closed ways stay
    alone. Returns the chains as node id arrays. greedy: a chain also runs on through a junction, with any way there
    that is not in a chain yet (the long chains of the whole-country levels)."""
    n = len(way_refs)
    ends = {}
    for i, r in enumerate(way_refs):
        if len(r) >= 2 and r[0] != r[-1]:
            ends.setdefault(int(r[0]), []).append(i)
            ends.setdefault(int(r[-1]), []).append(i)
    used = [False] * n

    def other(node, cur):
        at = ends.get(node, ())
        if greedy:
            return next((j for j in at if not used[j] and j != cur), None)
        if len(at) != 2:
            return None
        j = at[1] if at[0] == cur else at[0]
        return None if used[j] or j == cur else j

    chains = []
    for i in range(n):
        if used[i]:
            continue
        used[i] = True
        r = way_refs[i]
        if len(r) < 2 or r[0] == r[-1]:
            chains.append(np.asarray(r, np.int64))
            continue
        fwd, back = list(r), []
        # forward from the last node, then backward from the first
        cur, node = i, int(fwd[-1])
        while (j := other(node, cur)) is not None:
            used[j] = True
            seg = list(way_refs[j])
            seg = seg if int(seg[0]) == node else seg[::-1]
            fwd.extend(seg[1:])
            cur, node = j, int(fwd[-1])
        cur, node = i, int(fwd[0])
        while (j := other(node, cur)) is not None:
            used[j] = True
            seg = list(way_refs[j])
            seg = seg if int(seg[-1]) == node else seg[::-1]
            back = seg[:-1] + back
            cur, node = j, int(seg[0])
        chains.append(np.asarray(back + fwd, np.int64))
    return chains


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


def collect_store(path, store_dir, log, processes, sea=None, layer="water", progress=None):
    """Reads the extract into feature stores under store_dir ("poly": sea, then areas from ways, then from relations;
    "line": river and canal lines, or with layer "roads" the road lines only), in the order a tile draws them. Returns
    (the zoom-8 cells with data, stats)."""
    t0 = time.time()
    full = is_full(layer)
    progress = progress or (lambda phase, fraction: None)

    def passing(phase):
        osmpbf.PROGRESS = lambda done, total: progress(phase, done / total)

    passing("relations")
    border = {}  # way id -> border class (the full lines pack: the member ways of country and state borders)
    names = open(Path(store_dir).parent / "names.tsv", "w", encoding="utf-8", newline="\n") if layer == "water-full" \
        else None

    def put_name(typ, cls, size, lat_deg, lon_deg, name):
        names.write(f"{typ}\t{cls}\t{size:.0f}\t{int(round(lat_deg * 1e7))}\t{int(round(lon_deg * 1e7))}\t{name}\n")

    if layer == "roads":
        rels, member_ids, sea = [], np.zeros(0, np.int64), None
        passing("ways")
        ws = osmpbf.ways_parallel(path, road_kind, member_ids, processes)
    elif layer == "minor-full":
        rels, member_ids, sea = [], np.zeros(0, np.int64), None
        passing("ways")
        ws = osmpbf.ways_parallel(path, line_kind_minor, member_ids, processes)
    elif layer == "lines-full":
        sea = None
        for _rid, tags, ms in osmpbf.relations_parallel(path, border_relation, processes):
            c = LINE_COUNTRY if tags.get("admin_level") == "2" else LINE_STATE
            for typ, m, _role in ms:
                if typ == 1:
                    border[m] = max(border.get(m, 0), c)
        rels, member_ids = [], np.array(sorted(border), np.int64)
        log(f"relations: {member_ids.size} member ways of country and state borders ({time.time() - t0:.0f} s)")
        passing("ways")
        ws = osmpbf.ways_parallel(path, line_kind_full, member_ids, processes)
    elif is_land(layer):
        sea = None
        rels = osmpbf.relations_parallel(path, land_relation, processes)
        member_ids = np.unique(np.array([m for _, _, ms in rels for typ, m, _ in ms if typ == 1], np.int64))
        log(f"relations: {len(rels)} land-cover multipolygons, {member_ids.size} member ways "
            f"({time.time() - t0:.0f} s)")
        passing("ways")
        ws = osmpbf.ways_parallel(path, land_kind, member_ids, processes)
    elif full:
        rels = osmpbf.relations_parallel(path, water_relation_full, processes)
        member_ids = np.unique(np.array([m for _, _, ms in rels for typ, m, _ in ms if typ == 1], np.int64))
        log(f"relations: {len(rels)} water multipolygons, {member_ids.size} member ways ({time.time() - t0:.0f} s)")
        passing("ways")
        ws = osmpbf.ways_parallel(path, way_kind_full, member_ids, processes)
    else:
        rels = osmpbf.relations_parallel(path, water_relation, processes)
        member_ids = np.unique(np.array([m for _, _, ms in rels for typ, m, _ in ms if typ == 1], np.int64))
        log(f"relations: {len(rels)} water multipolygons, {member_ids.size} member ways ({time.time() - t0:.0f} s)")
        passing("ways")
        ws = osmpbf.ways_parallel(path, way_kind, member_ids, processes)  # (id, (area, line) or () for members, refs)
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
    passing("nodes")
    lat, lon, cells = osmpbf.nodes_e7_parallel(path, needed, processes, cell_zoom=8)
    osmpbf.PROGRESS = None
    progress("store", 0.0)
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
    min_area = MIN_AREA_LAND_M2 if is_land(layer) else MIN_AREA_FULL_M2 if full else MIN_AREA_M2
    classify = land_class if is_land(layer) else area_class
    if is_lines(layer):
        # Each class's ways joined end to end where two of them meet, then stored as lines (join_lines). The full
        # pack's small classes (service roads, tracks, paths and the like) are stored way by way.
        by_class, n_ways, n_alone = {}, 0, 0
        for i, (wid, kind, refs) in enumerate(ws):
            ws[i] = None
            lc = kind[1] if kind else border.get(wid, SKIP)
            if lc == SKIP:
                continue
            n_ways += 1
            if full and lc not in JOINED:
                la, lo = coords(refs)
                if la.size >= 2:
                    lines.add(lc, [(la, lo, False)])
                    n_alone += 1
            else:
                by_class.setdefault(lc, []).append(refs)
        for lc in sorted(by_class):
            for chain in join_lines(by_class[lc]):
                la, lo = coords(chain)
                if la.size >= 2:
                    lines.add(lc, [(la, lo, False)])
            if layer == "lines-full" and lc in LOW_CHAINS:
                for chain in join_lines(by_class[lc], greedy=True):
                    la, lo = coords(chain)
                    if la.size >= 2:
                        lines.add(LOW_CHAINS[lc], [(la, lo, False)])
            by_class[lc] = None
        log(f"roads: {n_ways} ways joined into {len(lines)} lines"
            + (f" ({n_alone} of them stored way by way)" if full else "") + f" ({time.time() - t0:.0f} s)")
        ws = []
    for wid, kind, refs in ws:
        cls, lc = kind[:2] if kind else (None, None)
        name, bay = kind[2:4] if kind and len(kind) > 2 else (None, False)
        closed = len(refs) >= 4 and refs[0] == refs[-1]
        if closed and (cls is not None or (bay and name)):
            la, lo = coords(refs[:-1])
            if la.size >= 3:
                m2 = area_m2(deg(la), deg(lo))
                if cls is not None:
                    if m2 < min_area:
                        stats["small"] += 1
                    else:
                        polys.add(cls, [(la, lo, True)])
                if name and m2 >= min_area:
                    put_name("B" if cls is None else "A", cls or 0, m2, *label_point([(deg(la), deg(lo))]), name)
        if lc is not None:
            la, lo = coords(refs)
            if la.size >= 2:
                lines.add(lc, [(la, lo, False)])
                if name:
                    # A label point every NAME_EVERY_M along the way, each standing for its share of the length, so
                    # a long river has its name in every tile it crosses.
                    m = line_length_m(deg(la), deg(lo))
                    k = max(1, min(int(la.size), int(round(m / NAME_EVERY_M))))
                    for j in range(k):
                        at = min(int(la.size) - 1, int((j + 0.5) * la.size / k))
                        put_name("L", lc, m / k, float(deg(la[at])), float(deg(lo[at])), name)
    is_member = np.isin(np.fromiter((w[0] for w in ws), np.int64, len(ws)), member_ids)
    way_by_id = {ws[i][0]: ws[i][2] for i in np.flatnonzero(is_member).tolist()}
    del member_ids, is_member
    for rid, tags, members in rels:
        cls = classify(tags)
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
        if total < min_area:
            stats["small"] += 1
            continue
        if cls is not None:
            polys.add(cls, rings)
        if names and tag_name(tags):
            holes = [(deg(la), deg(lo)) for la, lo, outer in rings if not outer]
            put_name("B" if cls is None else "A", cls or 0, total,
                     *label_point([(deg(la), deg(lo)) for la, lo in outer_ll], holes), tag_name(tags))
    n_poly, n_line, n_points = len(polys), len(lines), polys.points + lines.points
    polys.close()
    lines.close()
    if names:
        names.close()
    what = "land-cover areas" if is_land(layer) else "water areas (with the sea pieces)"
    log(f"features: {n_poly} {what}, {n_line} river/canal lines; dropped {stats['small']} "
        f"under {min_area:.0f} m2, {stats['unclosed']} unclosed ways, {stats['orphan_holes']} holes outside their outers; "
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


def apply_coverage(tiles, cells, tz, bits, buf, sea_cls=1):
    """The coverage grid for a level, and its tiles without the open sea: in a zoom-8 cell where most tiles are only
    the full square of sea, the cell defaults to full (2), those tiles are dropped and the cell's land tiles are stored
    as empty tiles; elsewhere the cell is 1 (absent = empty). sea_cls None: a layer without a sea (land cover): every
    cell is 1 and every tile is stored."""
    sq = full_square(bits, buf)
    is_open_sea = lambda feats: (sea_cls is not None and len(feats) == 1 and feats[0][0] == pmt.POLYGON
                                 and feats[0][1] == sea_cls and len(feats[0][3]) == 1 and feats[0][3][0] == sq)
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


def level_tiles(polys, lines, level, region=None, land=False):
    """One level's tiles {(x, y): features} from polygons [(class, [(lat, lon, outer)])] and lines
    [(class, lat, lon)], drawn in that order; with region = (rz, rx, ry), only that quadtree node's tiles. land: a
    land-cover level: its last field names the AREA classes it keeps, and no class is spared its smallest size."""
    tz, zmin, zmax, bits, buf, tol_m, min_m2, line_classes = level
    wb = tz + bits
    tiles = {}
    for cls, rings in polys:
        if land and line_classes is not None and cls not in line_classes:
            continue
        world = []
        upm = 0.0
        for la, lo, outer in rings:
            x, y = pg.world_xy(lo, la, wb)
            upm = pg.units_per_metre(float(np.mean(la)), wb)
            x, y = pg.simplify_ring(x, y, tol_m * upm)
            r = pg.clean_ring(x, y)
            if r is None:
                continue
            world.append(pg.orient(*r, outer=outer))
        if not world or not any(pg.area2(x, y) > 0 for x, y in world):
            continue
        # The level's smallest inland area (the sea, class 1, is never left out): outer rings minus holes.
        if min_m2 and (land or cls != 1) and sum(pg.area2(x, y) for x, y in world) / 2 < min_m2 * upm * upm:
            continue
        for key, parts in pg.cut_polygon(world, tz, bits, buf, region).items():
            tiles.setdefault(key, []).append(
                (pmt.POLYGON, cls, None, [list(zip(x.tolist(), y.tolist())) for x, y in parts]))
    for cls, la, lo in lines:
        if line_classes is not None and cls not in line_classes:
            continue
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


def _line_length(feature):
    return sum(math.hypot(x1 - x0, y1 - y0) for part in feature[3] for (x0, y0), (x1, y1) in zip(part, part[1:]))


# What a full pack's tile gives up first when it is over the display's budget: the lowest rank, the shortest first.
LINE_RANK = {1: 100, 2: 96, 25: 95, 24: 94, 3: 90, 4: 86, 5: 82, 17: 78, 6: 70, 9: 64, 10: 63, 11: 62, 12: 61, 13: 60,
             18: 58, 20: 57, 21: 56, 22: 55, 19: 54, 23: 52, 7: 30, 8: 26, 16: 20}
LINE_RANK_WATER = {16: 100, 17: 90, 18: 40, 19: 30}


def _poly_area(feature):
    return abs(sum(x0 * y1 - x1 * y0 for part in feature[3] for (x0, y0), (x1, y1) in zip(part, part[1:] + part[:1])))


def fit_tile(feats, rank=None, sea_cls=1):
    """A tile within TILE_FEATURE_BUDGET features and parts: the display decodes a tile into fixed scratch (16,384
    features or parts, pwca_argos map_view.c) and skips a bigger one whole. Over the budget, the shortest lines go first
    (2026-10-03: two zoom-8 tiles of the Yukon-Kuskokwim delta held about 18,000 river pieces of 3 points or fewer).
    The same for TILE_POINT_BUDGET points (the display's 131,072). Returns (features kept, lines dropped). sea_cls: the
    area class that is never left out (None: a layer without a sea, land cover)."""
    parts = sum(len(f[3]) for f in feats)
    points = sum(len(p) for f in feats for p in f[3])
    fits = lambda n: n <= TILE_FEATURE_BUDGET and parts <= TILE_FEATURE_BUDGET and points <= TILE_POINT_BUDGET
    if fits(len(feats)):
        return feats, 0
    drop, n = set(), len(feats)
    order = [i for i, f in enumerate(feats) if f[0] == pmt.LINE]
    if rank:  # a full pack: the least of the line classes first, the shortest first within a class
        order.sort(key=lambda i: (rank.get(feats[i][1], 50), _line_length(feats[i])))
    else:
        order.sort(key=lambda i: _line_length(feats[i]))
    if rank:  # then the smallest areas (never the sea)
        order += sorted((i for i, f in enumerate(feats) if f[0] == pmt.POLYGON and f[1] != sea_cls),
                        key=lambda i: _poly_area(feats[i]))
    for i in order:
        if fits(n - len(drop)):
            break
        drop.add(i)
        parts -= len(feats[i][3])
        points -= sum(len(p) for p in feats[i][3])
    return [f for i, f in enumerate(feats) if i not in drop], len(drop)


def low_level_tiles(store_dir, layer):
    """{level index: {tile key: blob}} for the levels cut below zoom 8 (the full lines pack's whole-country views).
    Such a tile is larger than a region, so these levels are cut once from the whole store, not region by region."""
    table = levels_of(layer)
    low = [(i, lv) for i, lv in enumerate(table) if lv[0] < 8]
    if not low:
        return {}
    _poly_store, line_store = _stores(str(store_dir))
    chains = {v: k for k, v in LOW_CHAINS.items()}  # the long chains stand in for their classes here
    wanted = sorted((set().union(*[lv[7] for _, lv in low]) - set(LOW_CHAINS)) | set(chains))
    lines = [(chains.get(c, c), rings[0][0], rings[0][1], line_length_m(rings[0][0], rings[0][1])) for c, rings in
             (line_store.feature(int(i)) for i in np.flatnonzero(np.isin(line_store.cls, wanted)))]
    out = {}
    for i, lv in low:
        # A piece shorter than twice the level's tolerance is under a pixel there: left out.
        tiles = level_tiles([], [(c, la, lo) for c, la, lo, m in lines if m >= 2 * lv[5]], lv, None)
        out[i] = {key: pmt.encode_tile(fit_tile(feats, LOW_RANK)[0], lv[3], lv[4], pmt.KIND_ROADS)
                  for key, feats in tiles.items()}
    _STORES.clear()
    return out


def region_job(job):
    """Worker: one region's tiles for every level, encoded: (region, [(grid, {key: blob}) per level], features)."""
    store_dir, region, cells, layer = job
    poly_store, line_store = _stores(store_dir)
    box = region_box_e7(*region, REGION_MARGIN_DEG)
    polys = [poly_store.feature(i) for i in poly_store.select(*box)]
    lines = [(c, rings[0][0], rings[0][1]) for c, rings in (line_store.feature(i) for i in line_store.select(*box))]
    out = []
    for level in levels_of(layer):
        tz, bits, buf = level[0], level[3], level[4]
        if tz < 8:  # cut once from the whole store (low_level_tiles)
            out.append(({}, {}))
            continue
        land = is_land(layer)
        sea_cls = None if land else 1
        grid, tiles = apply_coverage(level_tiles(polys, lines, level, region, land), cells, tz, bits, buf, sea_cls)
        enc = {}
        for key, feats in tiles.items():
            feats, dropped = fit_tile(feats, rank_of(layer), sea_cls)
            if dropped:
                print(f"tile z{tz} {key[0]},{key[1]}: {dropped} "
                      f"{'least features' if is_full(layer) else 'shortest lines'} left out (over the display's "
                      f"{TILE_FEATURE_BUDGET} features or parts)", flush=True)
            enc[key] = pmt.encode_tile(feats, bits, buf, kind_of(layer))
        out.append((grid, enc))
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
        k32.GetCurrentProcess.restype = ctypes.c_void_p  # a pseudo-handle: pointer-sized, or the call silently fails
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        if not k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000):  # BELOW_NORMAL_PRIORITY_CLASS
            print("warning: could not lower the priority", flush=True)
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
    ap.add_argument("--high-priority", action="store_true", help="run at high priority (every worker process too)")
    ap.add_argument("--keep-store", action="store_true", help="keep the feature store (<out_dir>/store)")
    ap.add_argument("--reuse-store", action="store_true",
                    help="skip the passes over the extract: cut from a complete store kept by an earlier run")
    ap.add_argument("--layer", choices=("water", "roads", "land"), default="water",
                    help="water (layer_kind 2, written as PMT 1.0), roads (layer_kind 4, written as PMT 1.1), or land "
                         "(land cover: layer_kind 6, written as PMT 1.2, FORMAT.md 7.7)")
    ap.add_argument("--minor", action="store_true",
                    help="with --layer roads: the minor ways' file (service roads, tracks, paths), zoom 15 and up")
    ap.add_argument("--full", action="store_true",
                    help="the full pack of the layer (FORMAT.md 7.6): water with every pond and stream and a level "
                         "cut at zoom 14 (names.tsv beside it for build_places.py); roads with every other line")
    a = ap.parse_args(argv[1:])
    if not 0 <= a.region_zoom <= 8:
        ap.error("--region-zoom must be 0-8 (regions hold whole zoom-8 coverage cells)")
    if a.low_priority:
        lower_priority()
    if a.high_priority:
        os.environ["PWCA_MAPS_PRIORITY"] = "high"  # the worker processes read it as they start
        osmpbf.apply_priority()
    layer = {"water": "water-full", "roads": "lines-full"}[a.layer] if a.full and a.layer != "land" else a.layer
    if a.layer == "land":
        if a.sea:
            ap.error("--sea goes with the water layer")
        layer = "land-full"  # the land-cover pack has one form
    if a.minor:
        if a.layer != "roads" or a.full:
            ap.error("--minor goes with --layer roads, without --full")
        layer = "minor-full"
    src = Path(a.extract)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    name = a.name or src.name.split(".")[0].replace("-latest", "")
    t0 = time.time()
    log = lambda msg: print(f"[{time.time() - t0:6.0f} s] {msg}", flush=True)

    # How far the build is, as a line "PROGRESS n% ..." each time the whole percent changes. The shares of the phases
    # are rough (from the North America builds of 2026-10): the time left is a guide, not a promise.
    phases = [("relations", 0.03), ("ways", 0.40), ("nodes", 0.05), ("store", 0.07), ("tiles", 0.45)]
    label = {"relations": "reading relations", "ways": "reading ways", "nodes": "reading points",
             "store": "sorting and joining", "tiles": "cutting tiles"}
    shown = {"pct": -1}

    def progress(phase, fraction):
        k = [n for n, _ in phases].index(phase)
        pct = int(100 * (sum(w for _, w in phases[:k]) + phases[k][1] * max(0.0, min(1.0, fraction))))
        if pct == shown["pct"]:
            return
        shown["pct"] = pct
        el = time.time() - t0
        left = el * (100 - pct) / pct if pct >= 3 else None
        log(f"PROGRESS {pct}% of this build ({label[phase]} {int(100 * fraction)}%)"
            + (f", roughly {int(left // 3600)} h {int(left % 3600 // 60):02d} min left" if left is not None else ""))

    head = osmpbf.header(src)
    stamp = head["replication_timestamp"] or int(src.stat().st_mtime)
    store_dir = out / "store"
    done_marker = store_dir / "COMPLETE"
    if a.reuse_store and done_marker.exists():
        cells = {tuple(c) for c in np.load(store_dir / "cells.npy").tolist()}
        log(f"reusing the feature store in {store_dir} ({len(cells)} zoom-8 cells with data)")
    else:
        if store_dir.exists():
            shutil.rmtree(store_dir)
        cells, _ = collect_store(src, store_dir, log, a.processes, a.sea, layer, progress)
        np.save(store_dir / "cells.npy", np.array(sorted(cells), np.int32).reshape(-1, 2))
        done_marker.write_text(f"{src.name} {stamp}\n", encoding="utf-8")
    gc.collect()
    regions = regions_of(cells, a.region_zoom)
    log(f"{len(regions)} regions (zoom {a.region_zoom}) over {len(cells)} zoom-8 cells; cutting with {a.processes} "
        f"processes")

    date = time.strftime("%Y-%m-%d", time.gmtime(stamp))
    commit = git_commit()
    roads = is_lines(layer)
    land = is_land(layer)
    strings = ["osm-land" if land else "osm-roads" if roads else "osm-water", CREDIT, "ODbL 1.0", "OpenStreetMap", date,
               f"pwca_maps {commit}"]
    files = []  # (path, bytes, sha256, tiles per level)
    lv_table = levels_of(layer)

    def new_acc():
        return [({}, {}) for _ in lv_table]  # per level: grid, tiles

    def flush(acc):
        levels = []
        for (grid, tiles), (tz, zmin, zmax, bits, buf, tol, _min_m2, _cls) in zip(acc, lv_table):
            levels.append({"tile_zoom": tz, "zoom_min": zmin, "zoom_max": zmax, "coord_bits": bits, "buffer": buf,
                           "tolerance_dm": int(tol * 10), "full_class": 0 if roads or land else 1,
                           "grid": grid if tz >= 8 else None, "tiles": tiles})
        data = pmt.build_pmt(layer_kind=kind_of(layer), levels=levels, strings=strings,
                             ids=dict(zip(pmt.ID_FIELDS, range(6))), build_time=int(time.time()), data_time=stamp)
        path = out / f"{name}.part{len(files) + 1}.pmt"
        path.write_bytes(data)
        files.append((path, len(data), hashlib.sha256(data).hexdigest(), [len(t) for _, t in acc]))
        log(f"wrote {path.name}: {len(data) / 1e6:.1f} MB, tiles per level {[len(t) for _, t in acc]}")

    jobs = [(str(store_dir), region, rc, layer) for region, rc in regions.items()]
    acc, acc_bytes, done, last_log = new_acc(), 0, 0, time.time()
    for i, blobs in low_level_tiles(store_dir, layer).items():  # the levels cut below zoom 8: in the first file
        acc[i][1].update(blobs)
        acc_bytes += sum(len(b) + INDEX_BYTES for b in blobs.values())
        log(f"level {i} (cut at zoom {lv_table[i][0]}): {len(blobs)} tiles from the whole store")
    pool = multiprocessing.get_context("spawn").Pool(a.processes) if a.processes > 1 else None
    try:
        results = pool.imap(region_job, jobs) if pool else map(region_job, jobs)
        for region, res, nfeat in results:
            size = sum(len(b) + INDEX_BYTES for _, blobs in res for b in blobs.values())
            if acc_bytes and acc_bytes + size > a.max_file_bytes:
                flush(acc)
                acc, acc_bytes = new_acc(), 0
            for (grid, tiles), (g, blobs) in zip(acc, res):
                grid.update(g)
                tiles.update(blobs)
            acc_bytes += size
            done += 1
            progress("tiles", done / len(jobs))
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
        _STORES.clear()
        gc.collect()
        shutil.rmtree(store_dir, ignore_errors=True)

    (out / "LICENSE.txt").write_text(LICENSE_TEXT, encoding="utf-8", newline="\n")
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION_TEXT, encoding="utf-8", newline="\n")
    sources = [{"name": "OpenStreetMap", "license": "ODbL 1.0", "file": src.name, "sha256": sha256(src),
                "replication_timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
                "writing_program": head["writing_program"], "extract_source": head["source"]}]
    if a.sea:
        sources.append({"name": "OpenStreetMap water polygons (osmdata.openstreetmap.de)", "license": "ODbL 1.0",
                        "file": Path(a.sea).name, "sha256": sha256(a.sea)})
    info = {
        "format": "PMT 1.2 (FORMAT.md 7.7): land cover, layer_kind 6" if land
        else "PMT 1.1 (FORMAT.md): roads, layer_kind 4" if roads else "PMT 1.0 (FORMAT.md)",
        "tool": {"repository": "https://github.com/PWC-Atelier-Artisanal-Watercraft/pwca_maps", "commit": commit},
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sources": sources,
        "files": {dst.name: {"bytes": n, "sha256": h, "tiles_per_level": t} for (_, n, h, t), dst in zip(files, final)},
    }
    (out / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    manifest = [f"{sha256(q)}  {q.name}" for q in sorted(out.iterdir()) if q.is_file() and q.name != "MANIFEST.sha256"]
    (out / "MANIFEST.sha256").write_text("\n".join(manifest) + "\n", encoding="utf-8", newline="\n")
    total = sum(n for _, n, _, _ in files)
    log(f"done: {len(final)} file(s), {total / 1e6:.1f} MB in {out}; data as of {date}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
