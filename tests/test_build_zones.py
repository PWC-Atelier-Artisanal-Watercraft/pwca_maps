"""Tests for tools/build_zones.py (the FWC zones mapping) on invented GeoJSON."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import build_zones as bz  # noqa: E402
import pmt  # noqa: E402

SQUARE = {"type": "Polygon", "coordinates": [[[-80.1, 26.1], [-80.0, 26.1], [-80.0, 26.0], [-80.1, 26.0],
                                              [-80.1, 26.1]]]}


def attrs_of(rec):
    """{tag: str for the names, int for the numbers}."""
    out = {}
    for tag, value in pmt.decode_attr(rec):
        if tag in (pmt.TAG_NAME, pmt.TAG_SOURCE_REF):
            out[tag] = value.decode("utf-8")
        else:
            out[tag] = pmt.get_varint(value, 0, len(value))[0]
    return out


class Seasons(unittest.TestCase):
    def test_whole_and_partial_months(self):
        self.assertEqual(bz.season("JAN 1 - DEC 31"), (0x0FFF, False))
        bits, partial = bz.season("NOV 15 - MAR 31")  # wraps the year; November counts whole
        self.assertEqual(bits, (1 << 10) | (1 << 11) | 0b111)
        self.assertTrue(partial)
        self.assertEqual(bz.season("SEP 1 - FEB 28"), ((1 << 8) | (1 << 9) | (1 << 10) | (1 << 11) | 0b11, False))
        self.assertEqual(bz.season("MAR - AUG 31")[0], 0b111111 << 2)
        self.assertEqual(bz.season("sometime"), (0x0FFF, True))  # unreadable: all year, flagged


class Classes(unittest.TestCase):
    def test_manatee_codes(self):
        self.assertIsNone(bz.manatee_class("UN"))
        self.assertEqual(bz.manatee_class("IS")[0], 1)
        self.assertEqual(bz.manatee_class("ISAY")[0], 1)
        self.assertEqual(bz.manatee_class("SS")[0], 2)
        self.assertEqual(bz.manatee_class("SS WKND")[3], bz.WEEKEND)
        self.assertEqual(bz.manatee_class("SSAY_25CH")[2], bz.FLAG_CHANNEL)
        self.assertEqual(bz.manatee_class("NE")[0], 4)
        self.assertEqual(bz.manatee_class("MPAY")[0], 4)
        self.assertEqual(bz.manatee_class("25 MPH")[:2], (3, 402))
        self.assertEqual(bz.manatee_class("35 DY/25 N")[:3], (3, 563, bz.FLAG_PARTIAL))
        self.assertEqual(bz.manatee_class("XYZ")[0], 5)  # unknown: kept as a restriction

    def test_boating_restrictions(self):
        self.assertEqual(bz.boating_class("Idle Speed No Wake")[:3], (1, 0, 0))
        self.assertEqual(bz.boating_class("Idle Speed No Wake - Conditional")[2], bz.FLAG_PARTIAL)
        self.assertEqual(bz.boating_class("Slow Speed Minimum Wake")[0], 2)
        self.assertEqual(bz.boating_class("Max Speed 30 MPH / Max Wake 15 Inches / Max Sound 80 dBA")[:2], (3, 483))
        self.assertEqual(bz.boating_class("No Entry - Water Control Structure")[0], 4)
        self.assertIsNone(bz.boating_class("No Anchoring of Sailboats or Other Masted Vessels"))
        self.assertIsNone(bz.boating_class("No Use of External Propellors"))


class Build(unittest.TestCase):
    def test_features_and_pack(self):
        boating = {"features": [
            {"properties": {"RESTRICTION": "Idle Speed No Wake", "AREA_NAME": "Test Basin", "RULE_NUM": "68D-24.999"},
             "geometry": SQUARE},
            {"properties": {"RESTRICTION": "No Use of External Propellors"}, "geometry": SQUARE}]}
        manatee = {"features": [
            {"properties": {"CLASS1_68C": "IS", "DATE1_68C": "NOV 15 - MAR 31", "CLASS2_68C": "SS",
                            "DATE2_68C": "APR 1 - NOV 14", "COUNTY": "Test", "SECT_68C": "68C-22.999"},
             "geometry": SQUARE},
            {"properties": {"CLASS1_68C": "25 MPH", "DATE1_68C": "APR 1 - NOV 15", "CLASS2_68C": "UN",
                            "DATE2_68C": "NOV 16 - MAR 31"}, "geometry": SQUARE}]}
        feats = bz.zone_features(boating, manatee, log=lambda m: None)
        self.assertEqual([f[0] for f in feats], [1, 1, 2, 3])  # propellers and UN left out
        idle = attrs_of(feats[1][1])
        self.assertEqual(idle[pmt.TAG_NAME], "MANATEE ZONE · IDLE SPEED · Test COUNTY")
        self.assertTrue(idle[pmt.TAG_FLAGS] & bz.FLAG_PARTIAL)
        self.assertEqual(idle[pmt.TAG_SOURCE_REF], "FAC 68C-22.999")
        levels, attrs, cells = bz.build(feats, log=lambda m: None)
        data = pmt.build_pmt(layer_kind=pmt.KIND_ZONES, levels=levels, strings=["a", "b", "c", "d", "e", "f"],
                             ids=dict(zip(pmt.ID_FIELDS, range(6))), attrs=attrs)
        pf = pmt.PmtFile(data)
        self.assertEqual(pf.kind, pmt.KIND_ZONES)
        self.assertTrue(cells and all(pf.coverage(1, cx << 4, cy << 4) == 1 for cx, cy in cells))
        self.assertGreater(pf.levels[1].entry_count, 0)


if __name__ == "__main__":
    unittest.main()
