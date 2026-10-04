"""Tests for the pieces of tools/build_places.py that don't need an OSM extract."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_places  # noqa: E402
import pmt  # noqa: E402


class Names(unittest.TestCase):
    def test_label_name(self):
        ok = build_places.label_name
        self.assertEqual(ok("Duluth"), b"Duluth")
        self.assertEqual(ok("  Sault   Ste.\tMarie "), b"Sault Ste. Marie")       # stripped, one space between words
        self.assertEqual(ok("Montréal"), "Montréal".encode())
        self.assertEqual(ok("Montréal"), "Montréal".encode())              # composed (NFC)
        self.assertEqual(ok("Māʻili"), "Mā‘ili".encode())      # Latin Extended-A; U+02BB written as U+2018
        self.assertEqual(ok("Łutselkʼe"), "Łutselk’e".encode())  # U+02BC written as U+2019
        self.assertEqual(ok("Łutselk’e"), "Łutselk’e".encode())
        for bad in (None, "", "   ", "ᐃᖃᓗᐃᑦ", "Москва", "東京", "A​B", "Tab\x01", "x" * 128):
            self.assertIsNone(ok(bad), repr(bad))
        self.assertEqual(len(ok("x" * 127)), 127)

    def test_place_name_falls_back_to_name_en(self):
        self.assertEqual(build_places.place_name({"name": "ᐃᖃᓗᐃᑦ", "name:en": "Iqaluit"}), b"Iqaluit")
        self.assertEqual(build_places.place_name({"name": "Nuuk", "name:en": "Godthab"}), b"Nuuk")
        self.assertIsNone(build_places.place_name({"name": "ᐃᖃᓗᐃᑦ"}))
        self.assertIsNone(build_places.place_name({"name": "ᐃᖃᓗᐃᑦ", "name:en": "東京"}))

    def test_collect(self):
        rows = [(5, 450000000, -930000000, {"place": "town", "name": "B"}),
                (3, 450000000, -930000000, {"place": "city", "name": "A"}),
                (9, 450000000, -930000000, {"place": "hamlet", "name": "ᐃᖃᓗᐃᑦ"}),
                (7, 450000000, -930000000, {"place": "village", "name": "ᐃᖃᓗᐃᑦ", "name:en": "C"})]
        places, stats = build_places.collect(rows)
        self.assertEqual([(p[0], p[3]) for p in places], [(1, b"A"), (2, b"B"), (3, b"C")])  # class order
        self.assertEqual(stats, {"no_drawable_name": 1, "used_name_en": 1})


class Tiles(unittest.TestCase):
    def test_a_place_is_in_one_tile_of_each_level_that_has_its_class(self):
        # A town and a hamlet near 45 N, 93 W.
        places = [(2, 450000000, -930000000, b"Town", 11), (4, 450010000, -930010000, b"Hamlet", 12)]
        data, counts, dropped = build_places.build(places, 1790121600, "test")
        self.assertEqual(dropped, 0)
        self.assertEqual(counts, [(0, 0), (1, 1), (1, 1), (1, 2)])  # L0 cities only; the hamlet only in L3
        pf = pmt.PmtFile(data)
        self.assertEqual((pf.kind, pf.minor), (pmt.KIND_PLACES, 1))
        for i, lv in enumerate(pf.levels):
            self.assertEqual(lv.buffer, 0)
            side = 1 << lv.coord_bits
            for entry in pf.entries(i):
                for geom, _cls, attr, (x, y, name) in pf.tile(i, entry):
                    self.assertEqual((geom, attr), (pmt.POINT, None))
                    self.assertTrue(0 <= x < side and 0 <= y < side and name)
        # The detail tile: the town before the hamlet (class order), 16-bit coordinates.
        feats = pf.tile(3, next(pf.entries(3)))
        self.assertEqual([(f[1], f[3][2]) for f in feats], [(2, b"Town"), (4, b"Hamlet")])
        # The same place at two levels is the same spot on the ground: tile x * side + local x scales by 2^bits.
        (t1,) = [(pmt.unmorton(e[0]), pf.tile(1, e)) for e in pf.entries(1)]
        (t3,) = [(pmt.unmorton(e[0]), pf.tile(3, e)) for e in pf.entries(3)]
        wx1 = (t1[0][0] * 4096 + t1[1][0][3][0]) / (1 << (10 + 12))
        wx3 = (t3[0][0] * 65536 + t3[1][0][3][0]) / (1 << (12 + 16))
        self.assertAlmostEqual(wx1, wx3, places=6)

    def test_budget_keeps_the_highest_classes(self):
        old = build_places.TILE_FEATURE_BUDGET
        build_places.TILE_FEATURE_BUDGET = 3
        try:
            places = [(4, 450000000 + i, -930000000, b"h%d" % i, 100 + i) for i in range(5)]
            places += [(2, 450000000, -930000000, b"t", 1)]
            tiles, _grid, dropped = build_places.level_tiles(places, build_places.LEVELS[3])
        finally:
            build_places.TILE_FEATURE_BUDGET = old
        (feats,) = tiles.values()
        self.assertEqual(dropped, 3)
        self.assertEqual([f[1] for f in feats], [2, 4, 4])


if __name__ == "__main__":
    unittest.main()
