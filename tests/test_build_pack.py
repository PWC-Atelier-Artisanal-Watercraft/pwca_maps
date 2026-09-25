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


if __name__ == "__main__":
    unittest.main()
