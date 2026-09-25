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
from pmt import LINE, POLYGON, PmtError  # noqa: E402

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

    def test_round_trip_and_lookup(self):
        for name, make in make_test_vectors.VECTORS.items():
            spec = make()
            pf = pmt.PmtFile(pmt.build_pmt(**spec))
            for i, lv in enumerate(spec["levels"]):
                for (x, y), feats in lv["tiles"].items():
                    with self.subTest(name=name, level=i, tile=(x, y)):
                        entry = pf.find(i, x, y)
                        self.assertIsNotNone(entry)
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
