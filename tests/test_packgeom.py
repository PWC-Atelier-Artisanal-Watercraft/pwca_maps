"""Tests for tools/packgeom.py. Run: python -m unittest discover -s tests"""

import math
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import packgeom as pg  # noqa: E402

TZ, BITS = 12, 8  # small tiles: 256 units per side
SIDE = 1 << BITS


def ring(points):
    a = np.array(points, np.int64)
    return a[:, 0], a[:, 1]


def total_area(tiles):
    return sum(pg.area2(x, y) for rings in tiles.values() for x, y in rings) / 2


class Projection(unittest.TestCase):
    def test_round_trip_and_scale(self):
        lon, lat = np.array([-93.2, 0.0, 179.9]), np.array([44.9, 0.0, -60.0])
        x, y = pg.world_xy(lon, lat, 28)
        lon2, lat2 = pg.lonlat(x, y, 28)
        self.assertTrue(np.allclose(lon, lon2) and np.allclose(lat, lat2))
        # One metre north at 45 degrees is units_per_metre units of y.
        y0 = pg.world_xy(0, 45.0, 28)[1]
        y1 = pg.world_xy(0, 45.0 + 1 / 111_320 * 1000, 28)[1]
        self.assertAlmostEqual(abs(y1 - y0) / 1000, pg.units_per_metre(45.0, 28), delta=0.01 * pg.units_per_metre(45.0, 28))


class Rings(unittest.TestCase):
    def test_orientation(self):
        x, y = ring([(0, 0), (0, 10), (10, 10), (10, 0)])  # counter-clockwise on screen
        self.assertLess(pg.area2(x, y), 0)
        ox, oy = pg.orient(x, y, outer=True)
        self.assertGreater(pg.area2(ox, oy), 0)
        hx, hy = pg.orient(ox, oy, outer=False)
        self.assertLess(pg.area2(hx, hy), 0)

    def test_simplify_keeps_shape(self):
        t = np.linspace(0, 2 * math.pi, 2000, endpoint=False)
        x, y = 1000 * np.cos(t), 1000 * np.sin(t)
        sx, sy = pg.simplify_ring(x, y, 1.0)
        self.assertLess(sx.size, 200)
        self.assertAlmostEqual(abs(pg.area2(sx, sy)) / abs(pg.area2(x, y)), 1.0, delta=0.01)

    def test_clean_drops_degenerate(self):
        self.assertIsNone(pg.clean_ring(np.array([0, 5, 10.0]), np.array([0, 0, 0.0])))
        x, y = pg.clean_ring(np.array([0, 0, 10, 10, 0]), np.array([0, 0, 0, 10, 0]))
        self.assertEqual(x.size, 3)


class Cutting(unittest.TestCase):
    def test_square_over_four_tiles_conserves_area(self):
        x0, y0 = (1000 << BITS) + 100, (2000 << BITS) + 50
        x, y = ring([(x0, y0), (x0 + 300, y0), (x0 + 300, y0 + 400), (x0, y0 + 400)])
        tiles = pg.cut_polygon([(x, y)], TZ, BITS, buffer=0)
        self.assertEqual(sorted(tiles), [(1000, 2000), (1000, 2001), (1001, 2000), (1001, 2001)])
        self.assertEqual(total_area(tiles), 300 * 400)
        for rings in tiles.values():
            for rx, ry in rings:
                self.assertGreater(pg.area2(rx, ry), 0)
                self.assertTrue(rx.min() >= 0 and rx.max() <= SIDE and ry.min() >= 0 and ry.max() <= SIDE)

    def test_hole_survives_cutting(self):
        x0, y0 = 5000 << BITS, 5000 << BITS
        outer = pg.orient(*ring([(x0, y0), (x0 + 600, y0), (x0 + 600, y0 + 600), (x0, y0 + 600)]), True)
        hole = pg.orient(*ring([(x0 + 200, y0 + 200), (x0 + 400, y0 + 200), (x0 + 400, y0 + 400),
                                (x0 + 200, y0 + 400)]), False)
        tiles = pg.cut_polygon([outer, hole], TZ, BITS, buffer=0)
        self.assertEqual(total_area(tiles), 600 * 600 - 200 * 200)

    def test_buffer_bounds_and_full_tiles(self):
        x0, y0 = 300 << BITS, 300 << BITS
        big = pg.orient(*ring([(x0 - 10, y0 - 10), (x0 + 5 * SIDE + 10, y0 - 10), (x0 + 5 * SIDE + 10, y0 + 5 * SIDE + 10),
                               (x0 - 10, y0 + 5 * SIDE + 10)]), True)
        buf = 16
        tiles = pg.cut_polygon([big], TZ, BITS, buffer=buf)
        full = [(t, r) for t, r in tiles.items() if len(r) == 1 and r[0][0].size == 4
                and set(r[0][0]) == {-buf, SIDE + buf} and set(r[0][1]) == {-buf, SIDE + buf}]
        self.assertGreaterEqual(len(full), 9)  # the inner 3 x 3 at least
        for rings in tiles.values():
            for rx, ry in rings:
                self.assertTrue(rx.min() >= -buf and rx.max() <= SIDE + buf)
                self.assertTrue(ry.min() >= -buf and ry.max() <= SIDE + buf)

    def test_random_polygons_conserve_area(self):
        rng = np.random.default_rng(3)
        for _ in range(20):
            n = 40
            t = np.sort(rng.uniform(0, 2 * math.pi, n))
            r = rng.uniform(200, 900, n)
            cx, cy = (777 << BITS) + 128, (555 << BITS) + 128
            x, y = np.rint(cx + r * np.cos(t)), np.rint(cy + r * np.sin(t))
            x, y = pg.orient(x.astype(np.int64), y.astype(np.int64), True)
            tiles = pg.cut_polygon([(x, y)], TZ, BITS, buffer=0)
            self.assertAlmostEqual(total_area(tiles), pg.area2(x, y) / 2, delta=abs(pg.area2(x, y)) * 1e-3 + 50)


class Lines(unittest.TestCase):
    def test_line_cut_keeps_length_and_bounds(self):
        rng = np.random.default_rng(5)
        x0, y0 = 900 << BITS, 900 << BITS
        x = np.rint(x0 + np.cumsum(rng.uniform(-40, 90, 60))).astype(np.int64)
        y = np.rint(y0 + np.cumsum(rng.uniform(-30, 70, 60))).astype(np.int64)
        tiles = pg.cut_line([(x, y)], TZ, BITS, buffer=0)
        length = lambda a, b: float(np.hypot(np.diff(a), np.diff(b)).sum())
        cut = sum(length(px, py) for parts in tiles.values() for px, py in parts)
        self.assertAlmostEqual(cut, length(x, y), delta=0.02 * length(x, y) + 2 * len(tiles))
        for parts in tiles.values():
            for px, py in parts:
                self.assertTrue(px.min() >= 0 and px.max() <= SIDE and py.min() >= 0 and py.max() <= SIDE)

    def test_line_leaving_and_reentering_splits(self):
        parts = pg.clip_line_rect(np.array([0.0, 10, 30, 30, 10]), np.array([5.0, 5, 5, 15, 15]), -1, 0, 20, 20)
        self.assertEqual(len(parts), 2)
        self.assertEqual((parts[0][0].tolist(), parts[1][0].tolist()), ([0, 10, 20], [20, 10]))


if __name__ == "__main__":
    unittest.main()
