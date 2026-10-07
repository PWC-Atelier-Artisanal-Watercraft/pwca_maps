"""Reads the symbols and street names files of a pack back and counts what is in them.

Usage: python tools/check_extras.py <pack_dir> [--json FILE]

Every file of the pack is checked against MANIFEST.sha256. Every .pmt of layer kind 7 (symbols) or 8 (street names)
is opened with the reference reader (tools/pmt.py: every rule of the format, every tile's CRC) and every tile is
decoded. Printed and, with --json, written: the symbols by class, with and without a name; the street name runs per
level and road class and the number of different names. Exit status 1 on the first error.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import pmt

SYMBOL_CLASSES = {1: "fuel", 2: "marina", 3: "boat_ramp", 4: "road_fuel"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def check_file(path, log):
    pf = pmt.open_pmt(path)
    if pf.kind not in (pmt.KIND_SYMBOLS, pmt.KIND_STREETS):
        return None
    total = sum(lv.entry_count for lv in pf.levels)
    res = {"layer_kind": pf.kind, "version": f"{pf.major}.{pf.minor}", "bytes": len(pf.data), "levels": []}
    names_all, done, shown = set(), 0, -1
    for i, lv in enumerate(pf.levels):
        by_class, named, names, feats = {}, 0, set(), 0
        for entry in pf.entries(i):
            for _geom, cls, _attr, parts in pf.tile(i, entry):
                feats += 1
                by_class[cls] = by_class.get(cls, 0) + 1
                name = parts[2] if pf.kind == pmt.KIND_SYMBOLS else parts[0]
                if name is None:
                    raise pmt.PmtError(f"{path.name}: a name that is not drawable text (level {i})")
                if name:
                    named += 1
                    names.add(name)
            done += 1
            pct = 100 * done // max(1, total)
            if pct != shown:
                shown = pct
                log(f"PROGRESS {pct}% of this build (reading {path.name} back {pct}%)")
        names_all |= names
        level = {"tile_zoom": lv.tile_zoom, "zooms": f"{lv.zoom_min}-{lv.zoom_max}", "tiles": lv.entry_count,
                 "features": feats, "with_a_name": named, "different_names": len(names)}
        if pf.kind == pmt.KIND_SYMBOLS:
            level["by_class"] = {SYMBOL_CLASSES.get(c, f"class_{c}"): n for c, n in sorted(by_class.items())}
        else:
            level["runs_by_road_class"] = {str(c): n for c, n in sorted(by_class.items())}
        res["levels"].append(level)
    res["different_names"] = len(names_all)
    return res


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pack_dir")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv[1:])
    pack = Path(a.pack_dir)
    log = lambda msg: print(msg, flush=True)
    try:
        manifest = pack / "MANIFEST.sha256"
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                digest, name = line.split(None, 1)
                if sha256(pack / name.strip()) != digest:
                    raise pmt.PmtError(f"{name.strip()}: its sha256 is not the manifest's")
        log(f"{pack}: every file matches MANIFEST.sha256")
        out = {}
        for path in sorted(pack.glob("*.pmt")):
            res = check_file(path, log)
            if res is None:
                continue
            out[path.name] = res
            last = res["levels"][-1]
            if res["layer_kind"] == pmt.KIND_SYMBOLS:
                log(f"{path.name}: PMT {res['version']} kind 7, every tile decoded. SYMBOLS at the closest level: "
                    f"{last['features']} = {last['by_class']}; {last['with_a_name']} with a name")
                for lv in res["levels"]:
                    log(f"  zooms {lv['zooms']} (tiles cut at {lv['tile_zoom']}): {lv['tiles']} tiles, "
                        f"{lv['features']} symbols {lv['by_class']}")
            else:
                log(f"{path.name}: PMT {res['version']} kind 8, every tile decoded. STREET NAMES: "
                    f"{res['different_names']} different names; {last['features']} runs at the closest level")
                for lv in res["levels"]:
                    log(f"  zooms {lv['zooms']} (tiles cut at {lv['tile_zoom']}): {lv['tiles']} tiles, "
                        f"{lv['features']} runs, {lv['different_names']} different names, by road class "
                        f"{lv['runs_by_road_class']}")
        if not out:
            raise pmt.PmtError(f"{pack}: no symbols or street names file")
        if a.json:
            Path(a.json).write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8", newline="\n")
        log("CHECK PASSED")
    except (pmt.PmtError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
