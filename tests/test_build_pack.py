"""Tests for the pieces of tools/build_pack.py that don't need an OSM extract."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_pack  # noqa: E402
import pmt  # noqa: E402
import shapefile  # noqa: E402

TZ, BITS, BUF = 10, 12, 32


class Coverage(unittest.TestCase):
    def test_open_sea_cell_defaults_to_full(self):
        sq = build_pack.full_square(BITS, BUF)
        sea = [(pmt.POLYGON, 1, None, [sq])]
        coast = [(pmt.POLYGON, 1, None, [[(0, 0), (4096, 0), (0, 4096)]])]
        tiles = {}
        for dx in range(4):  # cell (10, 20) at zoom 8 holds tiles 40-43 x 80-83 at zoom 10
            for dy in range(4):
                if (dx, dy) in ((0, 0), (1, 1)):
                    tiles[(40 + dx, 80 + dy)] = coast
                elif (dx, dy) not in ((3, 3), (2, 3)):
                    tiles[(40 + dx, 80 + dy)] = sea
        tiles[(44, 80)] = coast  # the neighbouring cell (11, 20): mostly land
        grid, out = build_pack.apply_coverage(tiles, {(10, 20), (11, 20)}, TZ, BITS, BUF)
        self.assertEqual(grid, {(10, 20): 2, (11, 20): 1})
        self.assertNotIn((42, 80), out)                    # open sea: dropped, the grid says full
        self.assertEqual(out[(43, 83)], [])                # land in a full cell: stored empty
        self.assertEqual(out[(40, 80)], coast)             # coast kept
        self.assertEqual(out[(44, 80)], coast)
        self.assertNotIn((45, 81), out)                    # absent in an empty-default cell: land

    def test_sea_polygons_from_a_shapefile(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "water.zip"
            # One square of sea (clockwise in lon/lat: outer) near (44.6, -63.5), one far away.
            shapefile.write_zip(path, [(5, [((-63.6, -63.6, -63.4, -63.4, -63.6), (44.5, 44.7, 44.7, 44.5, 44.5))]),
                                       (5, [((10, 10, 11, 11, 10), (50, 51, 51, 50, 50))])])
            cells = {(int((-63.5 + 180) / 360 * 256), 92)}
            polys = build_pack.sea_polygons(path, cells, log=lambda m: None)
        self.assertEqual(len(polys), 1)
        cls, rings = polys[0]
        self.assertEqual(cls, 1)
        la, lo, outer = rings[0]
        self.assertTrue(outer and la.size == 4 and np.all(lo < -63))


def square(lat, lon, side_m):
    """A square area [(lat, lon, outer)] in degrees with its north-west corner at (lat, lon)."""
    dlat = side_m / 111_320.0
    dlon = dlat / np.cos(np.radians(lat))
    return [(np.array([lat, lat, lat - dlat, lat - dlat]), np.array([lon, lon + dlon, lon + dlon, lon]), True)]


class Land(unittest.TestCase):
    """The land-cover pack (FORMAT.md 7.7)."""

    def test_classes(self):
        c = build_pack.land_class
        self.assertEqual(c({"landuse": "residential"}), 1)
        self.assertEqual(c({"landuse": "retail"}), 2)
        self.assertEqual(c({"landuse": "railway"}), 3)
        self.assertEqual(c({"amenity": "parking"}), 4)
        self.assertIsNone(c({"amenity": "parking", "parking": "underground"}))
        self.assertEqual(c({"leisure": "golf_course"}), 5)
        self.assertEqual(c({"landuse": "cemetery"}), 5)
        self.assertEqual(c({"landuse": "forest"}), 6)
        self.assertEqual(c({"natural": "wood", "leisure": "park"}), 6)   # two classes: the higher one
        self.assertEqual(c({"natural": "wetland", "landuse": "forest"}), 7)
        self.assertEqual(c({"natural": "beach"}), 8)
        for tags in ({}, {"natural": "water"}, {"landuse": "farmland"}, {"building": "yes"}, {"highway": "service"}):
            self.assertIsNone(c(tags), tags)
        self.assertEqual(build_pack.land_kind({"natural": "wood"}), (6, None))
        self.assertIsNone(build_pack.land_kind({"waterway": "river"}))
        self.assertTrue(build_pack.land_relation({"type": "multipolygon", "landuse": "forest"}))
        self.assertFalse(build_pack.land_relation({"type": "boundary", "landuse": "forest"}))
        self.assertFalse(build_pack.land_relation({"type": "multipolygon", "natural": "water"}))

    def test_a_level_keeps_its_classes_and_sizes(self):
        level = (12, 12, 13, 12, 32, 10.0, 10_000.0, {1, 5})
        small_housing = (1, square(45.0, -93.0, 50))      # 2,500 m2: under the level's smallest size
        parking = (4, square(45.0, -93.01, 200))          # a class the level leaves out
        park = (5, square(45.0, -93.02, 200))
        tiles = build_pack.level_tiles([small_housing, parking, park], [], level, None, land=True)
        self.assertEqual(sorted(f[1] for feats in tiles.values() for f in feats), [5])
        # The water layer's rules are as they were: its class 1 (the sea) is never left out for its size, and the
        # last field of a level names line classes only.
        tiles = build_pack.level_tiles([small_housing, parking, park], [], level, None)
        self.assertEqual(sorted(f[1] for feats in tiles.values() for f in feats), [1, 4, 5])
        for feats in tiles.values():
            for f in feats:
                self.assertEqual(f[0], pmt.POLYGON)
                pmt.decode_tile(pmt.encode_tile([f], 12, 32, pmt.KIND_LAND), 12, 32, 0, pmt.KIND_LAND)

    def test_no_sea_in_the_coverage(self):
        sq = build_pack.full_square(BITS, BUF)
        housing = [(pmt.POLYGON, 1, None, [sq])]  # a tile wholly inside a housing area: class 1 is not the sea here
        tiles = {(40 + dx, 80 + dy): housing for dx in range(4) for dy in range(4)}
        grid, out = build_pack.apply_coverage(tiles, {(10, 20)}, TZ, BITS, BUF, None)
        self.assertEqual(grid, {(10, 20): 1})
        self.assertEqual(out, tiles)

    def test_a_tile_over_the_budget_gives_up_its_smallest_areas(self):
        big = (pmt.POLYGON, 1, None, [[(0, 0), (4000, 0), (4000, 4000), (0, 4000)]])
        mid = (pmt.POLYGON, 6, None, [[(0, 0), (900, 0), (900, 900), (0, 900)]])
        tiny = (pmt.POLYGON, 1, None, [[(0, 0), (10, 0), (10, 10), (0, 10)]])
        budget = build_pack.TILE_FEATURE_BUDGET
        build_pack.TILE_FEATURE_BUDGET = 2
        try:
            kept, dropped = build_pack.fit_tile([tiny, big, mid], build_pack.rank_of("land-full"), None)
            self.assertEqual((kept, dropped), ([big, mid], 1))
            kept, dropped = build_pack.fit_tile([tiny, big, mid], build_pack.rank_of("water-full"))
            self.assertEqual((kept, dropped), ([tiny, big], 1))  # water: class 1 is the sea, never left out
        finally:
            build_pack.TILE_FEATURE_BUDGET = budget

    def test_layer_tables(self):
        self.assertTrue(build_pack.is_land("land-full") and build_pack.is_full("land-full"))
        self.assertFalse(build_pack.is_lines("land-full"))
        self.assertEqual(build_pack.kind_of("land-full"), pmt.KIND_LAND)
        self.assertEqual(build_pack.kind_of("minor-full"), pmt.KIND_ROADS)
        self.assertEqual(build_pack.kind_of("water-full"), pmt.KIND_WATER)
        levels = build_pack.levels_of("land-full")
        self.assertEqual([(lv[0], lv[1], lv[2], lv[3], lv[4]) for lv in levels],
                         [(lv[0], lv[1], lv[2], lv[3], lv[4]) for lv in build_pack.levels_of("water-full")])
        # A level holds exactly the classes the display draws at one of its zooms (Branding's style, 2026-10-06: the
        # first zoom of each class).
        first_zoom = {1: 8, 2: 11, 3: 11, 4: 14, 5: 10, 6: 8, 7: 8, 8: 11}
        for lv in levels:
            kept = lv[7] if lv[7] is not None else set(first_zoom)
            self.assertEqual(kept, {c for c, z in first_zoom.items() if z <= lv[2]}, lv)


if __name__ == "__main__":
    unittest.main()
