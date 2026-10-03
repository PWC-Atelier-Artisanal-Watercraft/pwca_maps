"""Counts the named places of an OSM extract by class, for sizing the places layer (PMT 1.1 PROPOSAL, kind 5).

Usage: python tools/measure_places.py <extract.osm.pbf> [--processes N]

A place is a node with `place` = city, town, village or hamlet and a `name`. Prints the count per class, the name
bytes (UTF-8), the longest name, how many names are over 63 bytes (the proposed limit) and an estimate of the layer's
size (a point and its name cost about 6 bytes plus the name; places shown at more than one level counted once per
level).
"""

import argparse
import os
import sys
import time

import osmpbf

CLASSES = {"city": 1, "town": 2, "village": 3, "hamlet": 4}
# The proposed levels: L0 cities, L1 + towns, L2 + villages, L3 + hamlets (a class is stored at its level and every
# finer one).
LEVELS_OF_CLASS = {1: 4, 2: 3, 3: 2, 4: 1}


def want(tags):
    return tags.get("place") in CLASSES and bool(tags.get("name"))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("extract")
    ap.add_argument("--processes", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    a = ap.parse_args(argv[1:])
    t0 = time.time()
    rows = osmpbf.tagged_nodes_parallel(a.extract, "place", want, a.processes)
    per = {c: [0, 0] for c in CLASSES.values()}
    longest, over, est = 0, 0, 0
    for _id, _la, _lo, tags in rows:
        c = CLASSES[tags["place"]]
        n = len(tags["name"].encode("utf-8"))
        per[c][0] += 1
        per[c][1] += n
        longest = max(longest, n)
        over += n > 63
        est += (6 + min(n, 63)) * LEVELS_OF_CLASS[c]
    for name, c in CLASSES.items():
        print(f"{name}: {per[c][0]} places, {per[c][1]} name bytes")
    print(f"all: {len(rows)} places; longest name {longest} B; over 63 B: {over}")
    print(f"estimated layer size: {est / 1e6:.1f} MB before tile overhead ({time.time() - t0:.0f} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
