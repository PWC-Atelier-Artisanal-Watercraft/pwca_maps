"""Tests for tools/pmt.py and the test vectors. Run: python -m unittest discover -s tests"""

import random
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import make_test_vectors  # noqa: E402
import pmt  # noqa: E402
from pmt import LINE, POINT, POLYGON, PmtError  # noqa: E402

VECTORS = ROOT / "tests" / "vectors"


def fix_header_crc(data):
    data = bytearray(data)
    hsize = struct.unpack_from("<I", data, 8)[0]
    struct.pack_into("<I", data, 12, 0)
    struct.pack_into("<I", data, 12, pmt.crc32(bytes(data[:hsize])))
    return bytes(data)


class Vectors(unittest.TestCase):
    def test_reproducible_bytes_and_dump(self):
        for name, make in make_test_vectors.VECTORS.items():
            with self.subTest(name):
                data = pmt.build_pmt(**make())
                self.assertEqual(data, (VECTORS / f"{name}.pmt").read_bytes())
                self.assertEqual(pmt.dump(pmt.PmtFile(data)), (VECTORS / f"{name}.txt").read_bytes())
        self.assertEqual(make_test_vectors.seam_rings_text(), (VECTORS / "water_seam_rings.txt").read_bytes())
        self.assertEqual(make_test_vectors.road_seam_lines_text(), (VECTORS / "roads_seam_lines.txt").read_bytes())

    def test_minor_version_by_kind(self):
        """A base, water or zones file is written as 1.0 (unchanged bytes); roads and places as 1.1; land cover as
        1.2."""
        seen = set()
        for name, make in make_test_vectors.VECTORS.items():
            spec = make()
            kind = spec["layer_kind"]
            minor = struct.unpack_from("<H", pmt.build_pmt(**spec), 6)[0]
            want = 2 if kind == pmt.KIND_LAND else 1 if kind in (pmt.KIND_ROADS, pmt.KIND_PLACES) else 0
            self.assertEqual(minor, want, name)
            seen.add(want)
        self.assertEqual(seen, {0, 1, 2})

    def test_round_trip_and_lookup(self):
        for name, make in make_test_vectors.VECTORS.items():
            spec = make()
            pf = pmt.PmtFile(pmt.build_pmt(**spec))
            for i, lv in enumerate(spec["levels"]):
                for (x, y), feats in lv["tiles"].items():
                    with self.subTest(name=name, level=i, tile=(x, y)):
                        entry = pf.find(i, x, y)
                        self.assertIsNotNone(entry)
                        if isinstance(feats, (bytes, bytearray)):  # a tile given as bytes: it decodes
                            pf.tile(i, entry)
                        else:
                            self.assertEqual(pf.tile(i, entry), feats)
                self.assertIsNone(pf.find(i, 0, 0))

    def test_pages_and_shared_blobs(self):
        pf = pmt.PmtFile(pmt.build_pmt(**make_test_vectors.water_levels()))
        lv = pf.levels[1]
        self.assertEqual((lv.entry_count, lv.page_count), (300, 2))
        self.assertEqual(lv.index_offset % pmt.ALIGN, 0)
        self.assertEqual(len({e[1] for e in pf.entries(1)}), 5)  # 300 tiles, five distinct blobs

    def test_coverage_grid(self):
        pf = pmt.PmtFile(pmt.build_pmt(**make_test_vectors.water_levels()))
        self.assertEqual(pf.coverage(0, 249, 377), 1)  # cell (62, 94)
        self.assertEqual(pf.coverage(0, 249, 381), 2)  # cell (62, 95)
        self.assertEqual(pf.coverage(0, 252, 377), 2)  # cell (63, 94)
        self.assertEqual(pf.coverage(0, 0, 0), 0)
        self.assertEqual(pf.coverage(1, 1000, 1500), 1)  # no grid

    def test_attributes(self):
        pf = pmt.PmtFile(pmt.build_pmt(**make_test_vectors.zones()))
        items = dict(pmt.decode_attr(pf.attrs[1]))
        self.assertEqual(items[pmt.TAG_NAME].decode(), "Test 25 mph zone")
        self.assertEqual(pmt.get_varint(items[pmt.TAG_SPEED_DKMH], 0, 2)[0], 402)
        self.assertEqual(pf.string(pf.ids["credit"]), "Florida FWC (test data)")


class Rejects(unittest.TestCase):
    def setUp(self):
        self.good = pmt.build_pmt(**make_test_vectors.water_minimal())

    def assertRejected(self, data):
        with self.assertRaises(PmtError):
            pf = pmt.PmtFile(data)
            for i in range(len(pf.levels)):
                for entry in pf.entries(i):
                    pf.tile(i, entry)

    def test_bad_magic_version_crc_size(self):
        d = bytearray(self.good)
        d[0] = ord("X")
        self.assertRejected(bytes(d))
        d = bytearray(self.good)
        struct.pack_into("<H", d, 4, 2)
        self.assertRejected(fix_header_crc(d))
        d = bytearray(self.good)
        d[100] ^= 1
        self.assertRejected(bytes(d))
        self.assertRejected(self.good[:-1])
        self.assertRejected(self.good + b"\0")

    def test_every_truncation(self):
        for n in range(0, len(self.good), 7):
            self.assertRejected(self.good[:n])

    def test_tile_crc(self):
        pf = pmt.PmtFile(self.good)
        _key, off, length, _crc = next(e for e in pf.entries(0) if e[2])
        d = bytearray(self.good)
        d[off + length // 2] ^= 0x40
        self.assertRejected(bytes(d))

    def test_bad_tile_contents(self):
        def one(blob, bits=12, buffer=32, attrs=0):
            with self.assertRaises(PmtError):
                pmt.decode_tile(blob, bits, buffer, attrs)

        one(b"\x80\x80\x80\x80\x80\x01")                  # varint longer than 5 bytes
        one(b"\xff\xff\xff\xff\x1f")                      # varint over 32 bits
        one(b"\x0b")                                       # runs past the end
        one(bytes([(2 << 3) | 3, 1, 3, 0, 0, 2, 0, 0, 2]))  # geometry type 3
        one(bytes([(2 << 3) | 1, 0]))                      # part count 0
        one(bytes([(2 << 3) | 1, 1, 2, 0, 0, 2, 0]))       # polygon ring with 2 points
        one(bytes([(2 << 3) | 2, 1, 1, 0, 0]))             # line with 1 point
        one(bytes([(2 << 3) | 5, 0, 1, 3, 0, 0, 2, 0, 0, 2]))  # attribute index with no attribute table
        far = bytearray([(2 << 3) | 2, 1, 2, 0, 0])
        pmt.put_varint(far, pmt.zigzag(4096 + 33))
        pmt.put_varint(far, 0)
        one(bytes(far))                                    # point beyond the buffer
        one(bytes([(2 << 3) | 2, 1, 2, 0, 0, 2, 0, 0x80])) # trailing partial varint

    def test_kind_and_geometry(self):
        """FORMAT 1.1: roads hold lines only, places points only, the 1.0 kinds no point; a roads or places file that
        says 1.0 is refused; a kind the reader does not know is not decoded."""
        line = bytes([(2 << 3) | 2, 1, 2, 0, 0, 2, 0])
        polygon = bytes([(2 << 3) | 1, 1, 3, 0, 0, 2, 0, 0, 2])
        point = bytes([(1 << 3) | 3, 20, 20, 2]) + b"Ab"
        self.assertEqual(pmt.decode_tile(line, 12, 32, 0, pmt.KIND_ROADS), [(LINE, 2, None, [[(0, 0), (1, 0)]])])
        self.assertEqual(pmt.decode_tile(point, 12, 32, 0, pmt.KIND_PLACES), [(POINT, 1, None, (10, 10, b"Ab"))])
        for blob, kind in ((polygon, pmt.KIND_ROADS), (point, pmt.KIND_ROADS), (point, pmt.KIND_WATER),
                           (point, pmt.KIND_ZONES), (point, pmt.KIND_BASE), (line, pmt.KIND_PLACES),
                           (polygon, pmt.KIND_PLACES), (line, 0), (line, 7), (line, 255),
                           (line, pmt.KIND_LAND), (point, pmt.KIND_LAND)):  # 1.2: land cover holds polygons only
            with self.subTest(kind=kind, blob=blob), self.assertRaises(PmtError):
                pmt.decode_tile(blob, 12, 32, 0, kind)
        self.assertEqual(pmt.decode_tile(polygon, 12, 32, 0, pmt.KIND_LAND),
                         [(POLYGON, 2, None, [[(0, 0), (1, 0), (1, 1)]])])
        d = bytearray(pmt.build_pmt(**make_test_vectors.land_cover()))
        struct.pack_into("<H", d, 6, 1)  # a land-cover file that says 1.1
        self.assertRejected(fix_header_crc(d))
        d = bytearray(pmt.build_pmt(**make_test_vectors.roads()))
        struct.pack_into("<H", d, 6, 0)  # a roads file that says 1.0
        self.assertRejected(fix_header_crc(d))
        d = bytearray(self.good)
        struct.pack_into("<H", d, 6, 1)  # a water file that says 1.1: any minor of the major is accepted
        pmt.PmtFile(fix_header_crc(d))
        d = bytearray(self.good)
        d[20] = pmt.KIND_ROADS  # a water file relabelled roads: the minor (0) refuses it at open
        self.assertRejected(fix_header_crc(d))
        struct.pack_into("<H", d, 6, 1)  # and with 1.1 its polygons are refused in the tiles
        self.assertRejected(fix_header_crc(d))

    def test_places_vector_skips_two_names(self):
        """The hand-made places tile: four points decode, the two bad names come back as None, the others as bytes."""
        pf = pmt.PmtFile(pmt.build_pmt(**make_test_vectors.places()))
        names = [f[3][2] for f in pf.tile(1, pf.find(1, 652, 1450))]
        self.assertEqual(names, [b"Good", None, None, b"Also good"])
        dump = pmt.dump(pf)
        self.assertIn(b"feature point class=3 200,300 name=-\n", dump)
        self.assertIn('feature point class=3 20000,40000 name="Māʻili"\n'.encode(), dump)

    def test_point_names(self):
        """Places: a name of 1 to 127 bytes inside the tile; a name that is not strict UTF-8 or holds a control
        character comes back as None (the label is skipped, the tile is kept)."""
        def point(name, cls=1):
            return bytes([(cls << 3) | 3, 20, 20, len(name)]) + name

        ok = lambda blob: pmt.decode_tile(blob, 12, 32, 0, pmt.KIND_PLACES)
        self.assertEqual(ok(point("Montréal".encode()))[0][3], (10, 10, "Montréal".encode()))
        self.assertEqual(ok(point(b"x" * 127))[0][3][2], b"x" * 127)
        two = ok(point(b"A") + point(b"B", 4))
        self.assertEqual([(f[1], f[3]) for f in two], [(1, (10, 10, b"A")), (4, (20, 20, b"B"))])
        for bad in (b"\xc3", b"\xc0\xaf", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xff", b"a\x00b", b"a\x1fb",
                    b"a\x7fb", "a\u0085b".encode(), b"\x80"):
            self.assertIsNone(ok(point(bad))[0][3][2], bad)
            with self.assertRaises(PmtError):
                pmt.encode_tile([(POINT, 1, None, (10, 10, bad))], 12, 32, pmt.KIND_PLACES)
        for blob in (bytes([(1 << 3) | 3, 20, 20, 0]),                 # a name of length 0
                     bytes([(1 << 3) | 3, 20, 20, 128]) + b"x" * 128,  # over the limit
                     bytes([(1 << 3) | 3, 20, 20, 5]) + b"abcd",       # past the tile's end
                     bytes([(1 << 3) | 3, 20, 20]),                    # no name at all
                     bytes([(1 << 3) | 7, 0, 20, 20, 1]) + b"a",       # a point with an attribute
                     bytes([(1 << 3) | 3, 0xC2, 0x40, 0, 1]) + b"a"):  # beyond the buffer
            with self.subTest(blob=blob), self.assertRaises(PmtError):
                ok(blob)
        with self.assertRaises(PmtError):
            pmt.encode_tile([(POINT, 1, None, (10, 10, b""))], 12, 32, pmt.KIND_PLACES)
        with self.assertRaises(PmtError):
            pmt.encode_tile([(POINT, 1, None, (10, 10, b"x" * 128))], 12, 32, pmt.KIND_PLACES)
        with self.assertRaises(PmtError):
            pmt.encode_tile([(POINT, 1, None, (10, 10, b"a"))], 12, 32, pmt.KIND_ROADS)

    def test_encoder_refuses_what_readers_reject(self):
        with self.assertRaises(PmtError):
            pmt.encode_tile([(POLYGON, 1, None, [[(0, 0), (1, 1)]])], 12, 32)
        with self.assertRaises(PmtError):
            pmt.encode_tile([(LINE, 1, None, [[(0, 0), (5000, 0)]])], 12, 32)
        with self.assertRaises(PmtError):
            pmt.build_pmt(**{**make_test_vectors.water_minimal(),
                             "levels": [{**make_test_vectors.water_minimal()["levels"][0], "buffer": 512}]})

    def test_random_damage_never_escapes_as_another_error(self):
        rng = random.Random(1)
        for name, make in make_test_vectors.VECTORS.items():
            good = pmt.build_pmt(**make())
            for _ in range(300):
                d = bytearray(good)
                for _ in range(rng.randint(1, 8)):
                    d[rng.randrange(len(d))] = rng.randrange(256)
                d = fix_header_crc(d) if rng.random() < 0.7 else bytes(d)
                try:
                    pf = pmt.PmtFile(d)
                    for i in range(len(pf.levels)):
                        for entry in pf.entries(i):
                            try:
                                pf.tile(i, entry)
                            except PmtError:
                                pass
                except PmtError:
                    pass


if __name__ == "__main__":
    unittest.main()
