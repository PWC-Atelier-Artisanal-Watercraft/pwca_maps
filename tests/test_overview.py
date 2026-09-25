"""Tests for tools/shapefile.py and tools/build_overview.py on synthetic shapefiles (no downloaded data)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_overview  # noqa: E402
import pmt  # noqa: E402
import shapefile  # noqa: E402

# ESRI order: outer rings clockwise in lon/lat, holes counter-clockwise.
LAND = [(-120, -120, -80, -80, -120), (30, 60, 60, 30, 30)]          # clockwise: north, east along the top, south
LAKE_HOLE = [(-100, -95, -95, -100, -100), (40, 40, 45, 45, 40)]     # counter-clockwise: a hole in the land
LAKE = [(-110, -110, -105, -105, -110), (50, 52, 52, 50, 50)]
RIVER = [(-115, -100, -90), (35, 38, 36)]
FAR_AWAY = [(10, 20, 20, 10, 10), (30, 30, 20, 20, 30)]               # Europe: not over North America


class Shapefiles(unittest.TestCase):
    def test_round_trip_and_ring_rule(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.zip"
            shapefile.write_zip(path, [(5, [LAND, LAKE_HOLE]), (3, [RIVER])], [("min_zoom", "N", 6)],
                                [{"min_zoom": 2}, {"min_zoom": 7}])
            recs = shapefile.read_zip(path)
        self.assertEqual([t for _, t, _ in recs], [5, 3])
        self.assertEqual([a["min_zoom"] for a, _, _ in recs], [2, 7])
        (lon, lat), (hlon, hlat) = recs[0][2]
        self.assertTrue(shapefile.ring_is_outer(lon, lat))
        self.assertFalse(shapefile.ring_is_outer(hlon, hlat))


class Overview(unittest.TestCase):
    def test_build_classes_levels_and_region(self):
        with tempfile.TemporaryDirectory() as d:
            shapefile.write_zip(Path(d) / "ne_10m_land.zip", [(5, [LAND, LAKE_HOLE]), (5, [FAR_AWAY])],
                                [("min_zoom", "N", 6)], [{"min_zoom": 0}, {"min_zoom": 0}])
            shapefile.write_zip(Path(d) / "ne_10m_lakes.zip", [(5, [LAKE])], [("min_zoom", "N", 6)],
                                [{"min_zoom": 6}])
            shapefile.write_zip(Path(d) / "ne_10m_rivers_lake_centerlines.zip", [(3, [RIVER])],
                                [("min_zoom", "N", 6)], [{"min_zoom": 3}])
            data = build_overview.build(d, log=lambda m: None)
        pf = pmt.PmtFile(data)
        self.assertEqual(pf.kind, pmt.KIND_BASE)
        self.assertEqual([(lv.zoom_min, lv.zoom_max) for lv in pf.levels], [(2, 4), (5, 7), (8, 9)])
        seen = {}
        for i in range(len(pf.levels)):
            for e in pf.entries(i):
                x, y = pmt.unmorton(e[0])
                n = 1 << pf.levels[i].tile_zoom
                self.assertLess(x / n, 0.5)  # everything is in the western hemisphere
                for geom, cls, _attr, parts in pf.tile(i, e):
                    seen.setdefault(i, set()).add((geom, cls))
        self.assertEqual(seen[0], {(pmt.POLYGON, 1), (pmt.LINE, 3)})  # the lake only appears from zoom 6
        self.assertIn((pmt.POLYGON, 2), seen[1])


if __name__ == "__main__":
    unittest.main()
