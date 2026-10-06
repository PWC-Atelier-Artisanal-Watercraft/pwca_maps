"""Tests for tools/assemble_pack.py with small packs made from the test vectors."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import assemble_pack  # noqa: E402
from assemble_pack import PackError  # noqa: E402

VECTORS = ROOT / "tests" / "vectors"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_pack(d, pmt_name, vector, license_text=b"licence\n", fmt="PMT 1.0 (FORMAT.md)"):
    """A pack directory as build_pack.py leaves it, around one vector file."""
    d.mkdir(parents=True)
    data = (VECTORS / vector).read_bytes()
    (d / pmt_name).write_bytes(data)
    (d / "LICENSE.txt").write_bytes(license_text)
    (d / "ATTRIBUTION.txt").write_bytes(b"attribution\n")
    info = {"format": fmt, "tool": {"repository": "r", "commit": "abc1234"}, "built_utc": "2026-10-04T00:00:00Z",
            "sources": [{"name": "OpenStreetMap", "file": "x.osm.pbf", "sha256": "00",
                         "replication_timestamp_utc": "2026-09-24T20:21:20Z"}],
            "files": {pmt_name: {"bytes": len(data), "sha256": sha(data), "tiles_per_level": [1]}}}
    (d / "SOURCES.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8", newline="\n")
    lines = [f"{sha(q.read_bytes())}  {q.name}" for q in sorted(d.iterdir())]
    (d / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return d


class Assemble(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.water = make_pack(self.t / "water", "region.pmt", "water_minimal.pmt")
        self.roads = make_pack(self.t / "roads", "region-roads.pmt", "roads.pmt", fmt="PMT 1.1 (FORMAT.md): roads")
        self.quiet = lambda *_: None

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_layers_in_one_pack(self):
        out = self.t / "pack"
        doc = assemble_pack.assemble(out, [self.water, self.roads], self.quiet)
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["ATTRIBUTION.txt", "LICENSE.txt", "MANIFEST.sha256", "SOURCES.json", "region-roads.pmt",
                          "region.pmt"])
        # The layer files are byte for byte the packs' own, and the new manifest matches every file.
        self.assertEqual((out / "region.pmt").read_bytes(), (VECTORS / "water_minimal.pmt").read_bytes())
        self.assertEqual((out / "region-roads.pmt").read_bytes(), (VECTORS / "roads.pmt").read_bytes())
        manifest = assemble_pack.read_manifest(out)
        self.assertEqual(set(manifest), {"ATTRIBUTION.txt", "LICENSE.txt", "SOURCES.json", "region-roads.pmt",
                                         "region.pmt"})
        self.assertNotIn(b"\r", (out / "MANIFEST.sha256").read_bytes())
        # SOURCES.json: each file with its own pack's format and tool, the shared source once; the card installer
        # reads sources[0].replication_timestamp_utc.
        self.assertEqual(doc["files"]["region-roads.pmt"]["format"], "PMT 1.1 (FORMAT.md): roads")
        self.assertEqual(doc["files"]["region.pmt"]["format"], "PMT 1.0 (FORMAT.md)")
        self.assertEqual(doc["files"]["region.pmt"]["tool"]["commit"], "abc1234")
        self.assertEqual(len(doc["sources"]), 1)
        self.assertEqual(json.loads((out / "SOURCES.json").read_text(encoding="utf-8"))["sources"][0]
                         ["replication_timestamp_utc"], "2026-09-24T20:21:20Z")

    def test_files_of_later_kinds_are_counted(self):
        """FORMAT.md 7.7: a pack holds at most 7 files of the kinds a PMT 1.1 reader skips (land cover, kind 6)."""
        fmt = "PMT 1.2 (FORMAT.md 7.7): land cover, layer_kind 6"
        land = [make_pack(self.t / f"land{i}", f"region-land-{i:02d}.pmt", "land_cover.pmt", fmt=fmt) for i in range(8)]
        self.assertEqual(assemble_pack.layer_kind(land[0] / "region-land-00.pmt"), 6)
        self.assertEqual(assemble_pack.layer_kind(self.roads / "region-roads.pmt"), 4)
        out = self.t / "pack"
        with self.assertRaises(PackError):
            assemble_pack.assemble(out, [self.water, self.roads] + land, self.quiet)
        self.assertFalse(out.exists() and any(out.iterdir()))
        doc = assemble_pack.assemble(out, [self.water, self.roads] + land[:7], self.quiet)
        self.assertEqual(len(doc["files"]), 9)
        self.assertEqual((out / "region-land-06.pmt").read_bytes(), (VECTORS / "land_cover.pmt").read_bytes())
        (self.t / "not.pmt").write_bytes(b"something else")
        with self.assertRaises(PackError):
            assemble_pack.layer_kind(self.t / "not.pmt")

    def test_refusals(self):
        out = self.t / "pack"
        with self.assertRaises(PackError):  # the same file name in two packs
            assemble_pack.assemble(out, [self.water, make_pack(self.t / "w2", "region.pmt", "water_levels.pmt")],
                                   self.quiet)
        with self.assertRaises(PackError):  # another licence text
            assemble_pack.assemble(out, [self.water, make_pack(self.t / "r2", "r.pmt", "roads.pmt", b"other\n")],
                                   self.quiet)
        with self.assertRaises(PackError):  # not a built pack
            assemble_pack.assemble(out, [self.t / "nothing"], self.quiet)
        with self.assertRaises(PackError):
            assemble_pack.assemble(out, [], self.quiet)
        self.assertFalse(out.exists() and any(out.iterdir()))  # nothing was written by a refused run
        # A pack whose layer file was changed after its manifest was written.
        data = bytearray((self.roads / "region-roads.pmt").read_bytes())
        data[-1] ^= 1
        (self.roads / "region-roads.pmt").write_bytes(bytes(data))
        with self.assertRaises(PackError):
            assemble_pack.assemble(out, [self.water, self.roads], self.quiet)
        self.assertFalse(out.exists() and any(out.iterdir()))
        # An output directory that already holds something.
        out.mkdir(exist_ok=True)
        (out / "old.txt").write_text("x")
        with self.assertRaises(PackError):
            assemble_pack.assemble(out, [self.water], self.quiet)


if __name__ == "__main__":
    unittest.main()
