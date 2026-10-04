"""Writes the PMT test vectors: tests/vectors/<name>.pmt and its canonical dump <name>.txt.

Usage: python tools/make_test_vectors.py [out_dir]   (default tests/vectors)

The vectors are synthetic (no map data) and deterministic: running this again must give the same bytes, which
tests/test_pmt.py checks. Every reader of the format must print the same dump for each vector.
"""

import sys
from pathlib import Path

import pmt
from pmt import LINE, POLYGON

BUILD_TIME = 1790208000
DATA_TIME = 1790121600
IDS = {"layer": 0, "credit": 1, "license": 2, "source": 3, "source_date": 4, "build": 5}
OSM_STRINGS = ["osm-water", "© OpenStreetMap contributors", "ODbL 1.0", "OpenStreetMap", "2026-09-20",
               "test-vectors"]


def water_minimal():
    """One level: a lake with an island, sea reaching into the buffer, a river line, an empty tile."""
    lake = [[(100, 100), (3000, 150), (3100, 2900), (200, 3000)],        # outer: clockwise on screen
            [(1000, 1000), (1000, 2000), (2000, 2000), (2000, 1000)]]    # island: the other way
    sea = [[(-32, -32), (4128, -32), (4128, 50), (-32, 60)]]
    river = [[(0, 4000), (512, 3500), (1024, 3900), (4128, 3000)]]
    far = [[(0, 0), (4096, 0), (4096, 4096)]]
    level = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 13, "coord_bits": 12, "buffer": 32, "tolerance_dm": 100,
             "tiles": {(1000, 1500): [(POLYGON, 2, None, lake), (POLYGON, 1, None, sea), (LINE, 16, None, river)],
                       (1001, 1500): [],
                       (1000, 1501): [(POLYGON, 5, None, far)]}}
    return dict(layer_kind=pmt.KIND_WATER, levels=[level], strings=OSM_STRINGS, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


def water_levels():
    """Three levels: a coverage grid, 300 tiles over two index pages with shared blobs, 16-bit coordinates."""
    l1 = {"tile_zoom": 10, "zoom_min": 10, "zoom_max": 11, "coord_bits": 12, "buffer": 32, "tolerance_dm": 400,
          "full_class": 1, "grid": {(62, 94): 1, (62, 95): 2, (63, 94): 2},
          "tiles": {(249, 377): [(POLYGON, 2, None, [[(10, 10), (4000, 20), (2000, 4000)]])],
                    (250, 378): []}}
    tiles = {}
    for x in range(1000, 1020):
        for y in range(1500, 1515):
            s = 200 + 700 * ((x + y) % 5)  # five sizes: many tiles share a blob
            tiles[(x, y)] = [(POLYGON, 2, None, [[(100, 100), (100 + s, 100), (100 + s, 100 + s), (100, 100 + s)]])]
    l2 = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 13, "coord_bits": 12, "buffer": 32, "tolerance_dm": 100,
          "tiles": tiles}
    ring = [(-512, -512), (66048, -512), (66048, 66048), (30000, 40000), (-512, 66048)]
    l3 = {"tile_zoom": 12, "zoom_min": 14, "zoom_max": 16, "coord_bits": 16, "buffer": 512, "tolerance_dm": 40,
          "tiles": {(1000, 1500): [(POLYGON, 1, None, [ring]),
                                   (LINE, 17, None, [[(0, 0), (65536, 65536)], [(5, 5), (6, 7)],
                                                     [(-500, 60000), (123, 456), (65000, 1)]])]}}
    return dict(layer_kind=pmt.KIND_WATER, levels=[l1, l2, l3], strings=OSM_STRINGS, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


def zones():
    """Zones with attribute records (names with UTF-8, quotes and backslashes, an unknown tag) and string escapes."""
    attrs = [
        pmt.encode_attr([(pmt.TAG_NAME, "Test Idle Zone Ñ"), (pmt.TAG_SOURCE_REF, "68D-24.test")]),
        pmt.encode_attr([(pmt.TAG_NAME, "Test 25 mph zone"), (pmt.TAG_SPEED_DKMH, 402), (pmt.TAG_MONTHS, 0b111000000111),
                         (pmt.TAG_DAYS, 0b1100000), (pmt.TAG_FLAGS, 1)]),
        pmt.encode_attr([(pmt.TAG_NAME, 'Quote " and \\ backslash'), (99, b"\x00\x01unknown tag")]),
    ]
    level = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 16, "coord_bits": 16, "buffer": 512, "tolerance_dm": 10,
             "tiles": {(2000, 3000): [(POLYGON, 1, 0, [[(1000, 1000), (9000, 1200), (8000, 9000)]]),
                                      (POLYGON, 3, 1, [[(20000, 20000), (40000, 20000), (40000, 40000),
                                                        (20000, 40000)]])],
                       (2001, 3000): [(POLYGON, 5, 2, [[(0, 0), (65536, 0), (65536, 65536), (0, 65536)],
                                                        [(100, 100), (100, 200), (200, 200)]])]}}
    strings = ["fwc-zones-test", "Florida FWC (test data)", "Public record (test)", "FWC", "2026-08", "test-vectors",
               "tab\there\x01 and \"quotes\""]
    return dict(layer_kind=pmt.KIND_ZONES, levels=[level], strings=strings, ids=IDS, attrs=attrs,
                build_time=BUILD_TIME, data_time=DATA_TIME)


def zones_shapes():
    """Zone shapes for drawing, in tile (2000, 3000): two squares sharing an edge (dashes run only round the pair), a
    strip 600 units wide (about 2.3 px at zoom 12: a solid edge, no hatch) and a large rectangle (hatched)."""
    attrs = [pmt.encode_attr([(pmt.TAG_NAME, "Pair west")]), pmt.encode_attr([(pmt.TAG_NAME, "Pair east")]),
             pmt.encode_attr([(pmt.TAG_NAME, "Thin strip")]), pmt.encode_attr([(pmt.TAG_NAME, "Large rectangle")])]

    def rect(x0, y0, x1, y1):
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1)]]  # clockwise on screen

    level = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 16, "coord_bits": 16, "buffer": 512, "tolerance_dm": 10,
             "tiles": {(2000, 3000): [(POLYGON, 1, 0, rect(4000, 4000, 20000, 20000)),
                                      (POLYGON, 1, 1, rect(20000, 4000, 36000, 20000)),
                                      (POLYGON, 1, 2, rect(4000, 30000, 40000, 30600)),
                                      (POLYGON, 5, 3, rect(8000, 40000, 56000, 60000))]}}
    strings = ["zones-shapes-test", "Test shapes", "Public domain (test)", "none", "2026-09", "test-vectors"]
    return dict(layer_kind=pmt.KIND_ZONES, levels=[level], strings=strings, ids=IDS, attrs=attrs,
                build_time=BUILD_TIME, data_time=DATA_TIME)


def base_minimal():
    """An overview tile (base layer): land with a lake and a river; absent tiles are water."""
    land = [[(-32, 1000), (2000, 900), (4128, 1500), (4128, 4128), (-32, 4128)]]
    lake = [[(1500, 2500), (2500, 2400), (2600, 3300), (1600, 3400)]]
    river = [[(300, 3900), (1200, 3000), (1550, 2950)]]
    level = {"tile_zoom": 5, "zoom_min": 5, "zoom_max": 9, "coord_bits": 12, "buffer": 32, "tolerance_dm": 3000,
             "tiles": {(7, 11): [(POLYGON, 1, None, land), (POLYGON, 2, None, lake), (LINE, 3, None, river)],
                       (7, 12): [(POLYGON, 1, None, [[(-32, -32), (4128, -32), (4128, 4128), (-32, 4128)]])]}}
    strings = ["ne-overview", "Made with Natural Earth", "Public domain", "Natural Earth", "1:10m", "test-vectors"]
    return dict(layer_kind=pmt.KIND_BASE, levels=[level], strings=strings, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


SEAM_TILE = (1000, 1500)  # top-left tile of the 3 x 3 block the seam polygon covers


def seam_rings():
    """A rough polygon with two holes spanning a 3 x 3 tile block, in level world units (tz 12, 12 bits)."""
    import math
    import numpy as np
    import packgeom as pg
    rng = np.random.default_rng(11)
    side = 1 << 12
    ox, oy = SEAM_TILE[0] * side, SEAM_TILE[1] * side
    cx, cy = ox + 1.5 * side, oy + 1.5 * side

    def star(r0, r1, n, phase):
        t = np.linspace(0, 2 * math.pi, n, endpoint=False) + phase
        r = rng.uniform(r0, r1, n)
        return np.rint(cx + r * np.cos(t)).astype(np.int64), np.rint(cy + r * np.sin(t)).astype(np.int64)

    outer = pg.orient(*star(0.9 * side, 1.45 * side, 90, 0.0), True)
    holes = []
    for dx, dy in ((-0.9, -0.2), (0.7, 0.6)):  # two islands straddling tile borders
        hx, hy = star(0.08 * side, 0.2 * side, 24, 0.3)
        holes.append(pg.orient(hx + int(dx * side), hy + int(dy * side), False))
    return [outer] + holes


def water_seam():
    """One polygon with holes cut into 3 x 3 buffered tiles: renderers must draw it as if it were never cut."""
    import packgeom as pg
    tiles = {}
    for key, rings in pg.cut_polygon(seam_rings(), 12, 12, 32).items():
        tiles[key] = [(POLYGON, 2, None, [list(zip(x.tolist(), y.tolist())) for x, y in rings])]
    level = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 14, "coord_bits": 12, "buffer": 32, "tolerance_dm": 100,
             "tiles": tiles}
    return dict(layer_kind=pmt.KIND_WATER, levels=[level], strings=OSM_STRINGS, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


ROAD_STRINGS = ["osm-roads", "© OpenStreetMap contributors", "ODbL 1.0", "OpenStreetMap", "2026-09-20",
                "test-vectors"]


def roads():
    """A roads file (layer kind 4, version 1.1): a zoomed-out level with a coverage grid and two classes, and a detail
    level with all six classes crossing each other, so a renderer's class order shows (a larger road over a smaller
    one), with lines running into the buffer and a class no renderer knows (7: never drawn)."""
    l0 = {"tile_zoom": 8, "zoom_min": 8, "zoom_max": 9, "coord_bits": 12, "buffer": 64, "tolerance_dm": 1500,
          "grid": {(40, 90): 1, (41, 90): 1},
          "tiles": {(40, 90): [(LINE, 1, None, [[(-64, 2000), (1500, 2100), (4160, 1800)]]),
                               (LINE, 2, None, [[(1000, -64), (1100, 4160)], [(3000, 500), (3500, 900), (3600, 1500)]])]}}
    lo, hi = -512, 66048
    detail = [(LINE, 6, None, [[(lo, y), (hi, y)] for y in (8192, 16384, 24576)]),
              (LINE, 5, None, [[(20000, lo), (20000, hi)]]),
              (LINE, 4, None, [[(30000, lo), (30000, hi)]]),
              (LINE, 3, None, [[(0, 0), (65536, 65536)]]),
              (LINE, 2, None, [[(lo, 40000), (hi, 40000)]]),
              (LINE, 1, None, [[(lo, 50000), (32768, 50000), (50000, 20000), (hi, 20000)]]),
              (LINE, 7, None, [[(lo, 60000), (hi, 60000)]])]
    l1 = {"tile_zoom": 12, "zoom_min": 14, "zoom_max": 16, "coord_bits": 16, "buffer": 512, "tolerance_dm": 40,
          "tiles": {(650, 1450): detail, (651, 1450): []}}
    return dict(layer_kind=pmt.KIND_ROADS, levels=[l0, l1], strings=ROAD_STRINGS, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


ROAD_SEAM_TILE = (1000, 1500)  # top-left tile of the 2 x 2 block the seam roads cross


def road_seam_lines():
    """Roads over a 2 x 2 tile block, in level world units (tz 12, 12 bits): [(class, x array, y array)], one of each
    class 1 to 5, crossing the block's inner tile borders at several angles, one of them along a border."""
    import numpy as np
    side = 1 << 12
    ox, oy = ROAD_SEAM_TILE[0] * side, ROAD_SEAM_TILE[1] * side
    rel = [(5, [(300, 500), (2500, 700), (5000, 400), (7800, 900)]),
           (4, [(600, 7900), (900, 5000), (700, 3000), (1100, 200)]),
           (3, [(200, 200), (4096, 4096), (7900, 7700)]),
           (2, [(4100, 300), (4090, 3000), (4110, 6000), (4080, 7900)]),  # along the block's vertical border
           (1, [(100, 6000), (2000, 4300), (4096, 4096), (6000, 3900), (8000, 2000)])]
    return [(c, np.array([ox + x for x, _ in pts], np.int64), np.array([oy + y for _, y in pts], np.int64))
            for c, pts in rel]


def roads_seam():
    """Roads cut into 2 x 2 buffered tiles: renderers must draw them as if they were never cut."""
    import packgeom as pg
    tiles = {}
    for cls, x, y in road_seam_lines():
        for key, parts in pg.cut_line([(x, y)], 12, 12, 64).items():
            tiles.setdefault(key, []).append((LINE, cls, None, [list(zip(px.tolist(), py.tolist())) for px, py in parts]))
    level = {"tile_zoom": 12, "zoom_min": 12, "zoom_max": 14, "coord_bits": 12, "buffer": 64, "tolerance_dm": 100,
             "tiles": tiles}
    return dict(layer_kind=pmt.KIND_ROADS, levels=[level], strings=ROAD_STRINGS, ids=IDS,
                build_time=BUILD_TIME, data_time=DATA_TIME)


def road_seam_lines_text():
    """The uncut seam roads: 'line <class> <n>' then n lines 'x y', coordinates relative to ROAD_SEAM_TILE's corner."""
    side = 1 << 12
    lines = []
    for cls, x, y in road_seam_lines():
        lines.append(f"line {cls} {x.size}")
        lines += [f"{a - ROAD_SEAM_TILE[0] * side} {b - ROAD_SEAM_TILE[1] * side}" for a, b in zip(x.tolist(), y.tolist())]
    return ("\n".join(lines) + "\n").encode("ascii")


def seam_rings_text():
    """The uncut seam polygon: 'ring <n>' then n lines 'x y', coordinates relative to SEAM_TILE's corner."""
    side = 1 << 12
    lines = []
    for x, y in seam_rings():
        lines.append(f"ring {x.size}")
        lines += [f"{a - SEAM_TILE[0] * side} {b - SEAM_TILE[1] * side}" for a, b in zip(x.tolist(), y.tolist())]
    return ("\n".join(lines) + "\n").encode("ascii")


VECTORS = {"water_minimal": water_minimal, "water_levels": water_levels, "zones": zones, "water_seam": water_seam,
           "base_minimal": base_minimal, "zones_shapes": zones_shapes, "roads": roads, "roads_seam": roads_seam}


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "tests" / "vectors"
    out.mkdir(parents=True, exist_ok=True)
    for name, make in VECTORS.items():
        data = pmt.build_pmt(**make())
        (out / f"{name}.pmt").write_bytes(data)
        (out / f"{name}.txt").write_bytes(pmt.dump(pmt.PmtFile(data)))
        print(f"{name}: {len(data)} bytes")
    (out / "water_seam_rings.txt").write_bytes(seam_rings_text())
    (out / "roads_seam_lines.txt").write_bytes(road_seam_lines_text())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
