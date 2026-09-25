"""The pack builder's feature store: the water features between the passes over an extract and the tile cutting, kept
on disk so that the worker processes that cut regions can memory-map them instead of receiving copies.

A store is a directory:
- lat.i32, lon.i32: every ring's points, in 1e-7 degrees (OSM's own precision), one ring after another;
- ring_start.npy (int64), ring_len.npy (int32), ring_outer.npy (int8): per ring;
- cls.npy (uint8), ring0.npy (int64), nrings.npy (int32), box.npy (int32, lat_min, lat_max, lon_min, lon_max): per
  feature, in the order they were added (the order they are drawn in a tile).
Lines are stores of their own, each line a one-ring feature.
"""

import array
import os
from pathlib import Path

import numpy as np

INT32_MAX = 2**31 - 1
INT32_MIN = -(2**31)


def e7(deg):
    """Degrees (float) to 1e-7 degrees (int32)."""
    return np.rint(np.asarray(deg, np.float64) * 1e7).astype(np.int32)


def degrees(v):
    """1e-7 degrees (int32) to float64 degrees, the same doubles the PBF reader computes (1e-9 * nanodegrees)."""
    return 1e-9 * (np.asarray(v, np.int64) * 100)


class StoreWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._lat = open(self.path / "lat.i32", "wb")
        self._lon = open(self.path / "lon.i32", "wb")
        self.points = 0
        self.ring_start, self.ring_len, self.ring_outer = array.array("q"), array.array("i"), array.array("b")
        self.cls, self.ring0, self.nrings, self.box = array.array("B"), array.array("q"), array.array("i"), array.array("i")

    def add(self, cls, rings):
        """One feature: rings = [(lat, lon, outer)] with lat/lon in 1e-7 degrees."""
        self.ring0.append(len(self.ring_len))
        lat_min = lon_min = INT32_MAX
        lat_max = lon_max = INT32_MIN
        for la, lo, outer in rings:
            la = np.ascontiguousarray(la, np.int32)
            lo = np.ascontiguousarray(lo, np.int32)
            la.tofile(self._lat)
            lo.tofile(self._lon)
            self.ring_start.append(self.points)
            self.ring_len.append(la.size)
            self.ring_outer.append(1 if outer else 0)
            self.points += la.size
            lat_min, lat_max = min(lat_min, int(la.min())), max(lat_max, int(la.max()))
            lon_min, lon_max = min(lon_min, int(lo.min())), max(lon_max, int(lo.max()))
        self.cls.append(cls)
        self.nrings.append(len(rings))
        self.box.extend((lat_min, lat_max, lon_min, lon_max))

    def __len__(self):
        return len(self.cls)

    def close(self):
        self._lat.close()
        self._lon.close()
        for name, arr, dtype in (("ring_start", self.ring_start, np.int64), ("ring_len", self.ring_len, np.int32),
                                 ("ring_outer", self.ring_outer, np.int8), ("cls", self.cls, np.uint8),
                                 ("ring0", self.ring0, np.int64), ("nrings", self.nrings, np.int32),
                                 ("box", self.box, np.int32)):
            np.save(self.path / f"{name}.npy", np.frombuffer(arr, dtype) if len(arr) else np.zeros(0, dtype))


def _load(path):
    return np.load(path, mmap_mode="r")


class Store:
    """A store opened for reading (memory-mapped)."""

    def __init__(self, path):
        p = Path(path)
        n = os.path.getsize(p / "lat.i32") // 4
        self.lat = np.memmap(p / "lat.i32", np.int32, "r", shape=(n,)) if n else np.zeros(0, np.int32)
        self.lon = np.memmap(p / "lon.i32", np.int32, "r", shape=(n,)) if n else np.zeros(0, np.int32)
        self.ring_start = _load(p / "ring_start.npy")
        self.ring_len = _load(p / "ring_len.npy")
        self.ring_outer = _load(p / "ring_outer.npy")
        self.cls = _load(p / "cls.npy")
        self.ring0 = _load(p / "ring0.npy")
        self.nrings = _load(p / "nrings.npy")
        self.box = _load(p / "box.npy").reshape(-1, 4)

    def __len__(self):
        return len(self.cls)

    def select(self, south, west, north, east):
        """Indexes (ascending: the order added) of the features whose bounding box meets the box, in 1e-7 degrees."""
        b = self.box
        return np.flatnonzero((b[:, 1] >= south) & (b[:, 0] <= north) & (b[:, 3] >= west) & (b[:, 2] <= east))

    def feature(self, i):
        """(class, [(lat, lon, outer)]) with float64 degrees."""
        rings = []
        r0 = int(self.ring0[i])
        for r in range(r0, r0 + int(self.nrings[i])):
            a, c = int(self.ring_start[r]), int(self.ring_len[r])
            rings.append((degrees(self.lat[a:a + c]), degrees(self.lon[a:a + c]), bool(self.ring_outer[r])))
        return int(self.cls[i]), rings
