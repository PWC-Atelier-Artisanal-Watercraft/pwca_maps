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


def display_char(ch):
    """Whether a display's label font is expected to hold this character: printable Basic Latin, Latin-1 Supplement,
    Latin Extended-A and -B, the modifier apostrophes of Hawaiian and of First Nations names, and the curly quotes
    and dashes. (The display checks every character against its real font again and skips a label it cannot draw.)"""
    c = ord(ch)
    if 0x20 <= c <= 0x7E or 0xA0 <= c <= 0x24F:
        return unicodedata.category(ch) != "Cc"
    return c in (0x2BB, 0x2BC, 0x2018, 0x2019, 0x2013, 0x2014)


def label_name(text):
    """The bytes of a name as a label, or None: stripped, one space between words, composed (NFC), every character a
    display character, 1 to pmt.NAME_MAX bytes."""
    if not text:
        return None
    text = unicodedata.normalize("NFC", " ".join(text.split()))
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


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("extract")
    ap.add_argument("out_dir")
    ap.add_argument("--name", default=None, help="file name without .pmt (default: the extract's name + -places)")
    ap.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args(argv[1:])
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
