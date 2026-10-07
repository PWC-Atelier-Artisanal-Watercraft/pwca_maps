"""Tests for the symbols and street names layers (PMT 1.3, FORMAT-1.3-SYMBOLS-STREET-NAMES.md): tools/pmt.py's two
new kinds and tools/build_extras.py. Run: python -m unittest discover -s tests"""

import struct
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build_extras as bx  # noqa: E402
import make_test_vectors  # noqa: E402
import pmt  # noqa: E402
from pmt import LINE, POINT, POLYGON, PmtError  # noqa: E402


def fix_header_crc(data):
    data = bytearray(data)
    hsize = struct.unpack_from("<I", data, 8)[0]
    struct.pack_into("<I", data, 12, 0)
    struct.pack_into("<I", data, 12, pmt.crc32(bytes(data[:hsize])))
    return bytes(data)


class Format(unittest.TestCase):
    def test_symbol_tile(self):
        feats = [(POINT, 1, None, (5, 6, b"Dock")), (POINT, 3, None, (0, 4096, b"")), (POINT, 500, None, (7, 7, b"x"))]
        blob = pmt.encode_tile(feats, 12, 0, pmt.KIND_SYMBOLS)
        # head 0x0B = point, class 1; dx 5, dy 6 (zigzag 10, 12); a name of 4 bytes
        self.assertEqual(blob[:8], bytes([0x0B, 10, 12, 4]) + b"Dock")
        self.assertEqual(pmt.decode_tile(blob, 12, 0, 0, pmt.KIND_SYMBOLS), feats)
        with self.assertRaises(PmtError):  # a places file never holds a point without a name
            pmt.encode_tile([(POINT, 1, None, (5, 6, b""))], 12, 0, pmt.KIND_PLACES)
        with self.assertRaises(PmtError):
            pmt.decode_tile(pmt.encode_tile([(POINT, 1, None, (5, 6, b""))], 12, 0, pmt.KIND_SYMBOLS), 12, 0, 0,
                            pmt.KIND_PLACES)
        for bad in ([(LINE, 1, None, [[(0, 0), (1, 1)]])], [(POLYGON, 1, None, [[(0, 0), (1, 1), (1, 0)]])],
                    [(POINT, 1, 0, (1, 1, b"a"))], [(POINT, 1, None, (1, 1, b"a" * 128))],
                    [(POINT, 1, None, (1, 1, b"a\x01"))], [(POINT, 1, None, (4097, 1, b"a"))]):
            with self.assertRaises(PmtError):
                pmt.encode_tile(bad, 12, 0, pmt.KIND_SYMBOLS)

    def test_symbol_name_rules(self):
        """A name that is not strict UTF-8 without control characters: the symbol stays, its name comes back None."""
        blob = bytes([0x0B, 10, 12, 2, 0xC3, 0x28])
        self.assertEqual(pmt.decode_tile(blob, 12, 0, 0, pmt.KIND_SYMBOLS), [(POINT, 1, None, (5, 6, None))])
        for bad in (bytes([0x0B, 10, 12]), bytes([0x0B, 10, 12, 3, 65]), bytes([0x0B, 10, 12, 128]) + b"a" * 128,
                    bytes([0x0F, 0, 10, 12, 0]), bytes([0x0A, 10, 12, 0])):
            with self.assertRaises(PmtError):
                pmt.decode_tile(bad, 12, 0, 0, pmt.KIND_SYMBOLS)

    def test_street_tile(self):
        feats = [(LINE, 6, None, (b"Elm St", [(1, 2), (300, 2)])), (LINE, 1, None, (b"A", [(0, 0), (9, 9), (16384, 0)]))]
        blob = pmt.encode_tile(feats, 14, 0, pmt.KIND_STREETS)
        # head 0x32 = line, class 6; the name; 2 points; (1, 2) then (+299, 0)
        self.assertEqual(blob[:14], bytes([0x32, 6]) + b"Elm St" + bytes([2, 2, 4, 0xD6, 0x04, 0]))
        self.assertEqual(pmt.decode_tile(blob, 14, 0, 0, pmt.KIND_STREETS), feats)
        for bad in ([(LINE, 1, None, (b"", [(0, 0), (1, 1)]))], [(LINE, 1, None, (b"A", [(0, 0)]))],
                    [(LINE, 1, 0, (b"A", [(0, 0), (1, 1)]))], [(LINE, 1, None, (b"A", [(0, 0), (16385, 1)]))],
                    [(POINT, 1, None, (1, 1, b"A"))], [(LINE, 1, None, (b"a" * 128, [(0, 0), (1, 1)]))]):
            with self.assertRaises(PmtError):
                pmt.encode_tile(bad, 14, 0, pmt.KIND_STREETS)
        # A roads-style line (a part count, no name) is not a street names feature, and the other way round.
        with self.assertRaises(PmtError):
            pmt.decode_tile(pmt.encode_tile([(LINE, 1, None, [[(0, 0), (1, 1)]])], 14, 0, pmt.KIND_ROADS), 14, 0, 0,
                            pmt.KIND_STREETS)
        for bad in (bytes([0x32]), bytes([0x32, 0, 2, 0, 0, 2, 2]), bytes([0x32, 1, 65, 1, 0, 0]),
                    bytes([0x32, 1, 65, 2, 0, 0]), bytes([0x36, 0, 1, 65, 2, 0, 0, 2, 2]), bytes([0x32, 5, 65, 2, 0, 0, 2, 2])):
            with self.assertRaises(PmtError):
                pmt.decode_tile(bad, 14, 0, 0, pmt.KIND_STREETS)
        # A name that is not drawable text: the line comes back with the name None (a reader skips the feature).
        blob = bytes([0x32, 2, 0xC3, 0x28, 2, 0, 0, 2, 2])
        self.assertEqual(pmt.decode_tile(blob, 14, 0, 0, pmt.KIND_STREETS), [(LINE, 6, None, (None, [(0, 0), (1, 1)]))])

    def test_minor_version(self):
        """A symbols or street names file states 1.3; the same bytes stating 1.1 are refused."""
        for name in ("symbols", "street_names"):
            data = pmt.build_pmt(**make_test_vectors.VECTORS[name]())
            self.assertEqual(struct.unpack_from("<HH", data, 4), (1, 3))
            pmt.PmtFile(data)
            for minor in (0, 1, 2):
                low = bytearray(data)
                struct.pack_into("<H", low, 6, minor)
                with self.assertRaises(PmtError):
                    pmt.PmtFile(fix_header_crc(low))

    def test_dump_lines(self):
        text = pmt.dump(pmt.PmtFile(pmt.build_pmt(**make_test_vectors.street_names()))).decode("utf-8")
        self.assertIn('feature nline class=6 name="North Washington Avenue"\npart 2 2000,5000 7000,5050\n', text)
        text = pmt.dump(pmt.PmtFile(pmt.build_pmt(**make_test_vectors.symbols()))).decode("utf-8")
        self.assertIn('feature point class=3 300,16383 name=""\n', text)
        self.assertIn('feature point class=1 1600,3200 name="Harbor Fuel Dock"\n', text)


class Builder(unittest.TestCase):
    def test_symbol_classes(self):
        c = bx.symbol_classes
        self.assertEqual(c({"leisure": "marina"}), (bx.MARINA,))
        self.assertEqual(c({"leisure": "marina", "fuel": "yes"}), (bx.FUEL, bx.MARINA))
        self.assertEqual(c({"leisure": "marina", "fuel:diesel": "yes"}), (bx.FUEL, bx.MARINA))
        self.assertEqual(c({"leisure": "slipway"}), (bx.RAMP,))
        self.assertEqual(c({"leisure": "slipway", "access": "private"}), ())
        self.assertEqual(c({"waterway": "fuel"}), (bx.FUEL,))
        self.assertEqual(c({"amenity": "fuel", "boat": "yes"}), (bx.FUEL,))
        self.assertEqual(c({"amenity": "fuel"}), ())
        self.assertEqual(c({"amenity": "fuel"}, True), (bx.ROAD_FUEL,))
        self.assertEqual(c({"seamark:type": "small_craft_facility",
                            "seamark:small_craft_facility:category": "slipway; fuel_station"}), (bx.FUEL, bx.RAMP))
        self.assertEqual(c({"leisure": "park"}), ())
        self.assertEqual(bx.symbol_name({"brand": "Shell"}), b"Shell")
        self.assertEqual(bx.symbol_name({"name": "東京", "name:en": "Dock"}), b"Dock")
        self.assertEqual(bx.symbol_name({}), b"")

    def test_extras_way(self):
        self.assertEqual(bx.extras_way({"highway": "residential", "name": " Elm   Street "}), ("r", 6, b"Elm Street"))
        self.assertIsNone(bx.extras_way({"highway": "residential"}))
        self.assertIsNone(bx.extras_way({"highway": "motorway_link", "name": "Ramp"}))
        self.assertIsNone(bx.extras_way({"highway": "service", "name": "Alley"}))
        self.assertIsNone(bx.extras_way({"highway": "pedestrian", "area": "yes", "name": "Square"}))
        self.assertEqual(bx.extras_way({"leisure": "marina", "name": "Bay"}), ("s", (bx.MARINA,), b"Bay"))

    def test_straight_runs(self):
        x = np.array([0.0, 100.0, 200.0, 300.0, 300.0, 300.0])
        y = np.array([0.0, 1.0, 0.0, 1.0, 100.0, 200.0])
        self.assertEqual(bx.straight_runs(x, y, 2.0), [(0, 3), (3, 5)])
        self.assertEqual(bx.straight_runs(x, y, 0.1), [(0, 1), (1, 2), (2, 3), (3, 5)])
        # A line that turns back on itself is cut there, however near it stays to the straight line.
        self.assertEqual(bx.straight_runs(np.array([0.0, 100.0, 50.0]), np.array([0.0, 0.0, 0.5]), 5.0),
                         [(0, 1), (1, 2)])
        self.assertEqual(bx.straight_runs(np.array([0.0, 5.0]), np.array([0.0, 5.0]), 1.0), [(0, 1)])

    def test_split_at_tiles(self):
        side = 16384
        one = bx.split_at_tiles(100.0, 200.0, 300.0, 200.0, side)
        self.assertEqual(one, [(0, 0, 100, 200, 300, 200, 200.0)])
        two = bx.split_at_tiles(side - 100.0, 50.0, side + 300.0, 50.0, side)
        self.assertEqual([t[:6] for t in two], [(0, 0, side - 100, 50, side, 50), (1, 0, 0, 50, 300, 50)])
        self.assertEqual([round(t[6]) for t in two], [100, 300])
        four = bx.split_at_tiles(side - 10.0, side - 20.0, side + 30.0, side + 20.0, side)
        self.assertEqual(sorted(t[:2] for t in four), [(0, 0), (1, 0), (1, 1)])

    def test_merge_symbols(self):
        pts = [(2, 450000000, -930000000, b"", 10), (2, 450000900, -930000000, b"Bay Marina", 21),
               (1, 450000000, -930000000, b"", 12), (2, 451000000, -930000000, b"", 14)]
        kept, merged = bx.merge_symbols(pts)
        self.assertEqual(merged, 1)
        self.assertEqual([(p[0], p[4]) for p in kept], [(1, 12), (2, 14), (2, 21)])

    def test_tiles(self):
        pts = [(1, 450000000, -930000000, b"Dock", 2), (3, 450000000, -930000000, b"Ramp", 4)]
        counts = []
        for level in bx.SYMBOL_LEVELS:
            tiles, grid, dropped = bx.symbol_level_tiles(pts, level)
            self.assertEqual((len(tiles), len(grid), dropped), (1, 1, 0))
            feats = pmt.decode_tile(next(iter(tiles.values())), level[3], 0, 0, pmt.KIND_SYMBOLS)
            counts.append([(f[1], f[3][2]) for f in feats])
        self.assertEqual(counts[0], [(1, b"")])  # zoom 10: no ramp yet, no names
        self.assertEqual(counts[1], [(1, b""), (3, b"")])
        self.assertEqual(counts[3], [(1, b"Dock"), (3, b"Ramp")])
        store, names = bx.RunStore(), [b"Elm Street", b"Oak Avenue"]
        for k in range(5):
            store.add(7, 9, 6, 100 + k, 0, 0, k, 100 + k, k)
        store.add(7, 9, 2, 50, 1, 0, 0, 50, 0)
        tiles, grid, stats, used = bx.street_level_tiles(store, names, bx.STREET_LEVELS[1])
        feats = pmt.decode_tile(tiles[(7, 9)], 14, 0, 0, pmt.KIND_STREETS)
        self.assertEqual([(f[1], f[3][0], f[3][1][1][0]) for f in feats],
                         [(2, b"Oak Avenue", 50), (6, b"Elm Street", 104), (6, b"Elm Street", 103),
                          (6, b"Elm Street", 102)])
        self.assertEqual((stats["runs"], used), (4, {b"Elm Street", b"Oak Avenue"}))


if __name__ == "__main__":
    unittest.main()
