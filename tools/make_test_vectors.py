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


VECTORS = {"water_minimal": water_minimal, "water_levels": water_levels, "zones": zones}


def main(argv):
    out = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parent.parent / "tests" / "vectors"
    out.mkdir(parents=True, exist_ok=True)
    for name, make in VECTORS.items():
        data = pmt.build_pmt(**make())
        (out / f"{name}.pmt").write_bytes(data)
        (out / f"{name}.txt").write_bytes(pmt.dump(pmt.PmtFile(data)))
        print(f"{name}: {len(data)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
